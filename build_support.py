"""构建输入与产物的离线证明；仅执行只读 Git 命令，不提交、重置或联网。

CLI snapshot 输出 MM_SOURCE_SHA / MM_INPUT_HASH / MM_BUILD_ID，供 monitor 写入状态。
摘要仅绑定源码、配置和静态资源；缓存、行情响应、运行回执不属于源码输入。
"""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# 与 daily.yml 的 paths 对齐：根目录新增生产模块/资源自动纳入，不逐个列模块名。
BUILD_INPUT_PATTERNS = (
    "*.py", "*.js", "*.mjs", "*.css", "*.html", "*.json", "*.webmanifest",
    "*.svg", "*.png", "*.ico", "*.jpg", "*.jpeg", "*.webp", "*.woff", "*.woff2",
    "*.toml", "*.yaml", "*.yml", "requirements*.txt", "requirements*.lock",
    "uv.lock", "poetry.lock", "assets/**", "static/**", "templates/**",
    "icons/**", "config/**", ".github/workflows/daily.yml",
    ".github/workflows/ensure-morning.yml",
)
EXCLUDED_INPUTS = frozenset({
    "index.html", "status.json", "run-receipt.json", "position-costs.json",
})
REQUIRED_INPUTS = frozenset({
    "build_support.py", "requirements.txt", "monitor.py", "holdings.json",
    "settings.json", "groups.json", "official-earnings.json", "symbol-names.json",
    "position-costs.seed.json", "cost_panel.js", "cost_panel.css", "edit.html",
    "run-status.js",
})
ARTIFACTS = ("index.html", "status.json")
SCHEMA_VERSION = 1


class BuildError(RuntimeError):
    """无法证明输入或产物一致时，拒绝发布。"""


class InputChanged(BuildError):
    """最新目标版本已经变更构建输入。"""


MAX_SCHEDULE_RETRIES = 7
RETRY_DELAY_MINUTES = 30


def generation_failure_reason(exc):
    """只认传输异常和明确可恢复 HTTP 状态，不从异常文本或 cause 猜测。"""
    import requests
    if isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return "blocked"
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return "network"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code in (408, 425, 429, 500, 502, 503, 504):
            return "network"
    return "blocked"


def git_failure_reason(operation, returncode, stderr):
    """Git 无结构化网络错误码；仅在 fetch/push 边界匹配明确诊断，未知错误关闭重试。"""
    if operation not in ("fetch", "push") or returncode == 0:
        return "blocked"
    text = stderr.lower()
    # 授权、证书、仓库配置及保护分支失败优先于其它诊断。
    if any(token in text for token in (
            "authentication failed", "permission denied", "repository not found", "could not read username",
            "requested url returned error: 401", "requested url returned error: 403",
            "ssl certificate", "certificate verify", "protected branch", "pre-receive hook declined",
            "gh006", "gh013", "not a git repository")):
        return "blocked"
    if any(token in text for token in (
            "could not resolve host:", "failed to connect to", "connection timed out",
            "connection reset by peer", "recv failure:", "send failure:",
            "the requested url returned error: 408", "the requested url returned error: 429",
            "the requested url returned error: 500", "the requested url returned error: 502",
            "the requested url returned error: 503", "the requested url returned error: 504",
            "curl 28 ", "curl 56 ", "http/2 stream", "remote end hung up unexpectedly")):
        return "network"
    if operation == "push" and re.search(r"\[rejected\].*\((?:fetch first|non-fast-forward)\)", text):
        return "race"
    return "blocked"


def retry_context(now, attempt="", day="", delay="", *, require_delay=False):
    """跨工作流透传同一美东日期与计数；迟到、跨日及非法输入均不续链。"""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise BuildError("重试时钟必须包含时区")
    local = now.astimezone(ZoneInfo("America/New_York"))
    if attempt == "" and day == "" and delay == "":
        attempt, day = "0", local.date().isoformat()
    if not re.fullmatch(r"[0-7]", attempt) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise BuildError("重试日期或轮数无效")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise BuildError("重试日期无效") from exc
    if (delay and (delay != str(RETRY_DELAY_MINUTES) or attempt == "0")) or (
            require_delay and attempt != "0" and not delay):
        raise BuildError("延迟重试必须为30分钟且携带非零轮数")
    return {"allowed": day == local.date().isoformat() and 20 <= local.hour < 24,
            "retry_day": day, "retry_attempt": int(attempt)}


def schedule_retry_plan(now, *, event, started_at, attempt, day, test_result, steps):
    """失败默认关闭；只有明确缺数或生成/提交传输失败可安排下一轮。"""
    denied = {"allowed": False}
    if event != "schedule" or test_result != "success":
        return denied
    context = retry_context(now, attempt, day)
    if not context["allowed"] or context["retry_attempt"] >= MAX_SCHEDULE_RETRIES:
        return denied
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return denied
    if started.utcoffset() is None or started.astimezone(ZoneInfo("America/New_York")).date().isoformat() != context["retry_day"]:
        return denied
    due = now + timedelta(minutes=RETRY_DELAY_MINUTES)
    if not retry_context(due, str(context["retry_attempt"]), context["retry_day"])["allowed"]:
        return denied
    if any(steps.get(key, {}).get("outcome") != "success" for key in ("inputs", "prepare", "receipt")):
        return denied
    # 包括缓存凭证、封存、上传及其它准备步骤；不能借网络错误绕过任何失败门槛。
    if any(step.get("outcome") in ("failure", "cancelled")
           for key, step in steps.items() if key not in ("gen", "commit")):
        return denied
    gen, commit = steps.get("gen", {}), steps.get("commit", {})
    if (gen.get("outputs", {}).get("cache_ready") == "true"
            and steps.get("cache-proof", {}).get("outcome") != "success"):
        return denied
    gen_reason = gen.get("outputs", {}).get("retry_reason")
    commit_reason = commit.get("outputs", {}).get("retry_reason")
    if gen_reason == "blocked":
        return denied
    if gen.get("outcome") == "failure" and gen_reason == "network" and commit.get("outcome") == "skipped":
        reason = "generation-network"
    elif (gen.get("outcome") == "success" and gen_reason == "missing-data"
          and gen.get("outputs", {}).get("publish") == "no" and commit.get("outcome") == "skipped"):
        reason = "missing-data"
    elif (gen.get("outcome") == "success" and gen_reason == "none"
          and gen.get("outputs", {}).get("publish") != "no"
          and steps.get("seal", {}).get("outcome") == "success"
          and commit.get("outcome") == "failure" and commit_reason == "network"):
        reason = "commit-network"
    else:
        return denied
    return {"allowed": True, "retry_day": context["retry_day"],
            "retry_attempt": context["retry_attempt"] + 1,
            "delay_minutes": RETRY_DELAY_MINUTES, "reason": reason}


def is_build_input(path):
    """只接收 Git 返回的 POSIX 相对路径；根通配符不会误匹配 tests/。"""
    if path in EXCLUDED_INPUTS:
        return False
    return any(fnmatch.fnmatchcase(path, pattern)
               for pattern in BUILD_INPUT_PATTERNS
               if "/" in pattern or "/" not in path)


def _git(root, *args, input=None):
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "diff.autoRefreshIndex=false", "-C", str(root), *args],
        input=input, capture_output=True, check=False,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0"),
    )
    if result.returncode:
        raise BuildError("Git 校验失败：" + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(files):
    return hashlib.sha256(_json_bytes({"schema_version": SCHEMA_VERSION, "files": files})).hexdigest()


def _source_sha(root, ref):
    return _git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def input_manifest(root, ref="HEAD"):
    """返回指定不可变 Git 提交的输入路径、文件模式及内容对象 ID（排序稳定）。"""
    sha = _source_sha(root, ref)
    files = {}
    for entry in _git(root, "ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if not is_build_input(path):
            continue
        mode, kind, oid = metadata.decode("ascii").split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise BuildError("构建输入必须是普通文件，不能是符号链接或子模块：" + path)
        files[path] = {"mode": mode, "oid": oid}
    missing = REQUIRED_INPUTS - files.keys()
    if missing:
        raise BuildError("缺少必要构建输入：" + ", ".join(sorted(missing)))
    return dict(sorted(files.items()))


def _check_clean_inputs(root):
    files = input_manifest(root, "HEAD")
    # 清空所有 clean/process 外部命令；依赖外部过滤器的构建输入另外拒绝，不能静默忽略。
    config_keys = _git(root, "config", "--null", "--name-only", "--list").decode("utf-8").split("\0")
    drivers = {match[1] for key in config_keys
               if (match := re.fullmatch(r"filter\.(.+)\.(?:clean|process|required)", key))}
    filter_args = [arg for driver in sorted(drivers)
                   for key, value in (("clean", ""), ("process", ""), ("required", "false"))
                   for arg in ("-c", f"filter.{driver}.{key}={value}")]
    attributes = _git(root, "check-attr", "-z", "--stdin", "filter",
                      input=b"".join(name.encode("utf-8") + b"\0" for name in files)).split(b"\0")[:-1]
    if len(attributes) != 3 * len(files):
        raise BuildError("构建输入过滤属性校验不完整")
    for name, attribute, value in zip(attributes[::3], attributes[1::3], attributes[2::3]):
        if attribute != b"filter" or value not in (b"unspecified", b"unset"):
            raise BuildError("构建输入依赖外部过滤器，拒绝执行：" + name.decode("utf-8"))
    # 这里只比索引与 HEAD，不读取工作树 stat，也不刷新索引；工作树内容由下方 hash 单独证明。
    changed = _git(root, *filter_args, "diff", "--cached", "--no-ext-diff", "--no-textconv",
                   "--no-renames", "--name-only", "-z", "HEAD", "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    dirty = sorted({p.decode("utf-8") for p in (changed + untracked).split(b"\0")
                    if p and is_build_input(p.decode("utf-8"))})
    if dirty:
        raise BuildError("构建输入含未提交修改：" + ", ".join(dirty))
    check_mode = _git(root, "config", "--type=bool", "--default=true", "--get", "core.filemode").strip() == b"true"
    for name, item in files.items():
        path = Path(root) / name
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise BuildError("构建输入缺少普通文件：" + name)
            # Git 只追踪所有者执行位；Windows 等 core.filemode=false 的 checkout 不比较该位。
            if check_mode and ("100755" if mode & stat.S_IXUSR else "100644") != item["mode"]:
                raise BuildError("构建输入含未提交模式修改：" + name)
            path.read_bytes()
        except OSError as exc:
            raise BuildError("构建输入无法读取：" + name) from exc
    # 不信 stat 缓存；按各路径的 attributes/autocrlf/binary 规则批量重新读取和清洗，绝不写对象库。
    paths = "".join(json.dumps(name, ensure_ascii=False) + "\n" for name in files).encode("utf-8")
    hashes = _git(root, *filter_args, "hash-object", "--stdin-paths", input=paths).decode("ascii").splitlines()
    if len(hashes) != len(files):
        raise BuildError("构建输入内容校验不完整")
    for (name, item), digest in zip(files.items(), hashes):
        if digest != item["oid"]:
            raise BuildError("构建输入含未提交内容修改：" + name)


def snapshot(root, build_id):
    """冻结干净 checkout；source_sha 始终是实际 checkout，而不是随后更新的 main。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", build_id):
        raise BuildError("build_id 必须是 1–128 位字母、数字、点、下划线或连字符")
    _check_clean_inputs(root)
    sha = _source_sha(root, "HEAD")
    files = input_manifest(root, sha)
    for path in files:
        if path.endswith(".json"):
            try:
                config = json.loads(_git(root, "show", f"{sha}:{path}"))
            except (ValueError, UnicodeError) as exc:
                raise BuildError("配置 JSON 损坏：" + path) from exc
            if path in ("settings.json", "holdings.json") and not isinstance(config, dict):
                raise BuildError("配置必须是对象：" + path)
            if path == "groups.json" and not isinstance(config, list):
                raise BuildError("分组配置必须是数组：" + path)
    return {"schema_version": SCHEMA_VERSION, "source_sha": sha,
            "input_hash": _digest(files), "build_id": build_id, "files": files}


def _validate_snapshot(value):
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise BuildError("构建快照格式或版本无效")
    files = value.get("files")
    if not isinstance(files, dict) or not REQUIRED_INPUTS.issubset(files):
        raise BuildError("构建快照缺少输入清单")
    for path, item in files.items():
        if (not isinstance(path, str) or not is_build_input(path)
                or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or not isinstance(item, dict) or item.get("mode") not in ("100644", "100755")
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(item.get("oid", "")))):
            raise BuildError("构建快照输入项无效")
    if value.get("input_hash") != _digest(files):
        raise BuildError("构建快照摘要不匹配")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(value.get("source_sha", ""))):
        raise BuildError("source_sha 无效")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(value.get("build_id", ""))):
        raise BuildError("build_id 无效")


def verify_inputs(root, frozen, ref="origin/main"):
    """枚举目标提交的完整同规则集合；新增/删除输入也拒绝，不只比较旧路径。"""
    _validate_snapshot(frozen)
    current = input_manifest(root, ref)
    if current != frozen["files"]:
        paths = sorted(p for p in current.keys() | frozen["files"].keys()
                       if current.get(p) != frozen["files"].get(p))
        raise InputChanged("输入已变更，废弃本轮产物：" + ", ".join(paths))
    return frozen["input_hash"]


def _artifact_bytes(directory, frozen):
    _validate_snapshot(frozen)
    data = {}
    for name in ARTIFACTS:
        path = Path(directory) / name
        if path.is_symlink() or not path.is_file():
            raise BuildError("缺少普通产物文件：" + name)
        data[name] = path.read_bytes()
        if not data[name]:
            raise BuildError("产物为空：" + name)
    status = json.loads(data["status.json"])
    if not isinstance(status, dict) or any(status.get(key) != frozen[key]
                                          for key in ("source_sha", "input_hash", "build_id")):
        raise BuildError("status.json 未绑定本轮 source_sha/input_hash/build_id，禁止复用旧产物")
    return data


def seal_artifacts(root, frozen, bundle_dir):
    """生成完成后封存产物；不替产物补写版本字段，以免给旧产物贴新标签。"""
    if _source_sha(root, "HEAD") != frozen["source_sha"]:
        raise BuildError("生成期间 checkout 已改变")
    _check_clean_inputs(root)
    verify_inputs(root, frozen, "HEAD")
    data = _artifact_bytes(root, frozen)
    destination = Path(bundle_dir)
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in data.items():
        (destination / name).write_bytes(content)
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in data.items()}
    (destination / "artifact-hashes.json").write_bytes(_json_bytes(hashes))
    return hashes


def verify_bundle(bundle_dir, frozen):
    """校验封存后未被修改的产物，供每次重试使用。"""
    data = _artifact_bytes(bundle_dir, frozen)
    hashes = json.loads((Path(bundle_dir) / "artifact-hashes.json").read_bytes())
    if hashes != {name: hashlib.sha256(content).hexdigest() for name, content in data.items()}:
        raise BuildError("封存产物摘要不匹配")
    return data


def artifacts_changed(root, frozen, bundle_dir, ref="HEAD"):
    """比较已校验产物与目标树；仅完全相同才判定 no-change，Git 错误不当作相同。"""
    sha = _source_sha(root, ref)
    data = verify_bundle(bundle_dir, frozen)
    paths = set(_git(root, "ls-tree", "-r", "--name-only", "-z", sha).split(b"\0"))
    return any(name.encode() not in paths or _git(root, "show", f"{sha}:{name}") != data[name]
               for name in ARTIFACTS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "verify", "seal", "verify-bundle", "artifact-state",
                                            "git-failure", "retry-context", "retry-plan"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--snapshot")
    parser.add_argument("--operation", choices=("fetch", "push"))
    parser.add_argument("--returncode", type=int)
    parser.add_argument("--build-id", default=os.environ.get("MM_BUILD_ID", ""))
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--bundle-dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "git-failure":
            if not args.operation or args.returncode is None:
                raise BuildError("Git 分类需要操作及返回码")
            print(git_failure_reason(args.operation, args.returncode, sys.stdin.read()))
            return 0
        if args.command in ("retry-context", "retry-plan"):
            now = datetime.now(timezone.utc)
            attempt, day = os.environ.get("MM_RETRY_ATTEMPT", ""), os.environ.get("MM_RETRY_DAY", "")
            if args.command == "retry-context":
                result = retry_context(now, attempt, day, os.environ.get("MM_RETRY_DELAY", ""),
                                       require_delay="MM_RETRY_DELAY" in os.environ)
            else:
                result = schedule_retry_plan(now, event=os.environ.get("MM_EVENT", ""),
                    started_at=os.environ.get("MM_STARTED_AT", ""), attempt=attempt, day=day,
                    test_result=os.environ.get("MM_TEST_RESULT", ""),
                    steps=json.loads(os.environ.get("MM_STEP_RESULTS", "{}")))
            for key, value in result.items():
                print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
            return 0
        if not args.snapshot:
            raise BuildError("构建证明命令必须提供 --snapshot")
        if args.command == "snapshot":
            value = snapshot(args.root, args.build_id)
            # 独占创建：快照文件放 RUNNER_TEMP，生成后不再覆盖。
            with Path(args.snapshot).open("xb") as stream:
                stream.write(_json_bytes(value))
            for key in ("source_sha", "input_hash", "build_id"):
                print(f"MM_{key.upper()}={value[key]}")
        else:
            value = json.loads(Path(args.snapshot).read_bytes())
            _validate_snapshot(value)
            if args.command == "verify":
                verify_inputs(args.root, value, args.ref)
            else:
                if not args.bundle_dir:
                    raise BuildError("该命令必须提供 --bundle-dir")
                if args.command == "seal":
                    seal_artifacts(args.root, value, args.bundle_dir)
                elif args.command == "verify-bundle":
                    verify_bundle(args.bundle_dir, value)
                else:
                    print("changed" if artifacts_changed(args.root, value, args.bundle_dir, args.ref) else "no-change")
        return 0
    except (BuildError, OSError, ValueError) as exc:
        print("拒绝发布：" + str(exc), file=sys.stderr)
        return 3 if isinstance(exc, InputChanged) else 1


if __name__ == "__main__":
    sys.exit(main())
