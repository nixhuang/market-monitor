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
import subprocess
import sys


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
    "import-audit.json",
})
REQUIRED_INPUTS = frozenset({
    "build_support.py", "requirements.txt", "monitor.py", "holdings.json",
    "settings.json", "groups.json", "official-earnings.json", "symbol-names.json",
    "position-costs.seed.json", "cost_panel.js", "cost_panel.css", "edit.html",
    "run-status.js", "list-import.js",
})
ARTIFACTS = ("index.html", "status.json")
SCHEMA_VERSION = 1


class BuildError(RuntimeError):
    """无法证明输入或产物一致时，拒绝发布。"""


class InputChanged(BuildError):
    """最新目标版本已经变更构建输入。"""


def is_build_input(path):
    """只接收 Git 返回的 POSIX 相对路径；根通配符不会误匹配 tests/。"""
    if path in EXCLUDED_INPUTS:
        return False
    return any(fnmatch.fnmatchcase(path, pattern)
               for pattern in BUILD_INPUT_PATTERNS
               if "/" in pattern or "/" not in path)


def _git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1"),
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
    changed = _git(root, "diff", "--no-renames", "--name-only", "-z", "HEAD", "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    dirty = sorted({p.decode("utf-8") for p in (changed + untracked).split(b"\0")
                    if p and is_build_input(p.decode("utf-8"))})
    if dirty:
        raise BuildError("构建输入含未提交修改：" + ", ".join(dirty))


def snapshot(root, build_id):
    """冻结干净 checkout；source_sha 始终是实际 checkout，而不是随后更新的 main。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", build_id):
        raise BuildError("build_id 必须是 1–128 位字母、数字、点、下划线或连字符")
    _check_clean_inputs(root)
    sha = _source_sha(root, "HEAD")
    files = input_manifest(root, sha)
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
    parser.add_argument("command", choices=("snapshot", "verify", "seal", "verify-bundle", "artifact-state"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--build-id", default=os.environ.get("MM_BUILD_ID", ""))
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--bundle-dir")
    args = parser.parse_args(argv)
    try:
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
