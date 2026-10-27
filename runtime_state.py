"""运行辅助状态：不缓存行情快照，不将生成回执当作发布证明。"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid


SCHEMA_VERSION = 2
ENTRY_SECTIONS = frozenset(("earnings", "consensus"))
EPOCH = "1970-01-01T00:00:00+00:00"
ATTEMPT_FIELDS = {
    "earnings": {"status", "attempted", "official_attempted", "official_error", "error", "next_retry", "retry"},
    "consensus": {"s", "try", "why", "failures", "next_retry", "kind", "retryable_block"},
}
EVENT_FIELDS = {
    "earnings": set(),
    "consensus": {"ne", "earn_pending"},
}
AUX_FIELDS = {
    "nfci_cache": ("rows", "source", "checked_at", "result"),
    "earnings": ("data", "events"),
    "consensus": ("source", "data", "quota"),
    # oil 含当轮行情，不能随辅助缓存回灌。
    "reference_alerts": ("policy",),
}


def build_identity():
    return {key: os.environ.get("MM_" + key.upper(), "")
            for key in ("source_sha", "input_hash", "build_id")}


def atomic_write_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def atomic_write_json(path, value):
    atomic_write_bytes(path, _json_bytes(value))


def write_artifacts(base, html, snapshot, *, receipt=None):
    # 先完成全部序列化；普通写入异常恢复已替换文件，JSON 本身始终原子替换。
    files = [(Path(base) / "index.html", html.encode("utf-8")),
             (Path(base) / "status.json", _json_bytes(snapshot))]
    if receipt is not None:
        files.append((Path(base) / "run-receipt.json", _json_bytes(receipt)))
    originals = {path: path.read_bytes() if path.exists() else None for path, _ in files}
    replaced = []
    try:
        for path, content in files:
            atomic_write_bytes(path, content)
            replaced.append(path)
    except Exception:
        for path in reversed(replaced):
            if originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(path, originals[path])
        raise


def _read_object(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _auxiliary(name, value):
    if not isinstance(value, dict):
        return {}
    out = {key: copy.deepcopy(value[key]) for key in AUX_FIELDS[name] if key in value}
    for key in ("data", "events", "policy", "quota"):
        if key in out and not isinstance(out[key], dict):
            del out[key]
    if name == "consensus" and "quota" not in out:
        stat = value.get("stat")
        if isinstance(stat, dict) and isinstance(stat.get("quota"), dict):
            out["quota"] = copy.deepcopy(stat["quota"])
    return out


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _version(value):
    return type(value) is int and value in (1, SCHEMA_VERSION)


def _date(value):
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            day = datetime.strptime(value, fmt).date()
            return day if day <= datetime.now(timezone.utc).date() else None
        except ValueError:
            pass
    return None


def _quota_date(value):
    """配额按北京时间的台账日校验，不放宽其他字段的 UTC 日期边界。"""
    if not isinstance(value, str):
        return None
    today = datetime.now(timezone(timedelta(hours=8))).date()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            day = datetime.strptime(value, fmt).date()
            return day if day <= today else None
        except ValueError:
            pass
    return None


def _entry_stamp(value, *, beijing_day=False):
    stamp = _stamp(value)
    if stamp is not None:
        return stamp if stamp <= datetime.now(timezone.utc) else None
    day = _quota_date(value) if beijing_day else _date(value)
    zone = timezone(timedelta(hours=8)) if beijing_day else timezone.utc
    return datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc) if day else None


def _parts(name, entry):
    attempt = ATTEMPT_FIELDS[name]
    events = EVENT_FIELDS[name]
    return {"facts": {k: v for k, v in entry.items() if k not in attempt | events},
            "attempt": {k: v for k, v in entry.items() if k in attempt},
            "events": {k: v for k, v in entry.items() if k in events}}


def _own_stamp(name, aspect, entry):
    if aspect == "events":
        keys = ()
    elif aspect == "facts":
        keys = ("r",) if name == "consensus" else ("fetched", "official_checked")
    else:
        keys = {"earnings": ("attempted", "official_attempted"),
                "consensus": ("try",)}[name]
        # consensus.try 是北京台账日；带时区钟点仍按严格的 UTC 时点校验。
        explicit = [_entry_stamp(entry.get(k), beijing_day=name == "consensus") for k in keys]
        if any(s is not None for s in explicit):
            return max(s for s in explicit if s is not None)
        failed = (entry.get("official_error") or entry.get("s") == "fail"
                  or entry.get("status") == "error")
        # 失败记录的 fetched/r 是上次成功时间，不能拿来压低本轮失败版本。
        # 有新元数据时用本轮观测；旧缓存缺尝试时间时不凭 section 时间猜测。
        keys = () if failed else (("r",) if name == "consensus" else ("fetched",))
    stamps = [_entry_stamp(entry.get(k)) for k in keys]
    return max((s for s in stamps if s is not None), default=None)


def _entry_meta(name, entry, metadata=None):
    metadata = metadata if isinstance(metadata, dict) else {}
    out = {}
    for aspect, part in _parts(name, entry).items():
        old = metadata.get(aspect)
        digest = _digest(part)
        # 绑定对应内容，避免 updater 复制旧元数据后让新内容冒用旧时间。
        if (isinstance(old, dict) and old.get("digest") == digest and "at" in old
                and (old["at"] is None or _entry_stamp(old["at"]) is not None)
                and _entry_stamp(old.get("asof")) is not None):
            out[aspect] = copy.deepcopy(old)
        else:
            stamp = _own_stamp(name, aspect, entry)
            out[aspect] = {"asof": stamp.isoformat() if stamp else EPOCH,
                           "at": stamp.isoformat() if stamp else None, "digest": digest}
    return out


def _rank(aspect, metadata):
    observed = _entry_stamp(metadata[aspect]["asof"]) or _stamp(EPOCH)
    # at 随组件来源保留，不能从合并后的另一组件（例如 fetched）重新推算。
    return (_entry_stamp(metadata[aspect].get("at")) or observed, observed)


def _dates(values):
    return {d.isoformat() for v in values if (d := _date(v)) is not None} if isinstance(values, list) else set()


def _merge_events(name, left, right, preferred):
    out = copy.deepcopy(preferred)
    if name == "consensus":
        pending = _dates(left.get("earn_pending")) | _dates(right.get("earn_pending"))
        if pending:
            out["earn_pending"] = sorted(pending)
    return out


def _merge_entry(name, left, right, left_meta=None, right_meta=None):
    lm, rm = _entry_meta(name, left, left_meta), _entry_meta(name, right, right_meta)
    lp, rp = _parts(name, left), _parts(name, right)
    merged, metadata = {}, {}
    for aspect in ("facts", "attempt", "events"):
        use_right = _rank(aspect, rm) >= _rank(aspect, lm)
        if aspect == "facts":
            # 缺少成功内容的失败记录不能删除旧事实。
            if not rp[aspect] and lp[aspect]:
                use_right = False
            elif not lp[aspect] and rp[aspect]:
                use_right = True
        part = copy.deepcopy(rp[aspect] if use_right else lp[aspect])
        meta = copy.deepcopy(rm[aspect] if use_right else lm[aspect])
        if aspect == "events":
            part = _merge_events(name, lp[aspect], rp[aspect], part)
            meta["digest"] = _digest(part)
        merged.update(part)
        metadata[aspect] = meta
    return merged, metadata


def _merge_auxiliary(older, newer):
    out = {**copy.deepcopy(older), **copy.deepcopy(newer)}
    events = {}
    for value in (older, newer):
        for symbol, dates in (value.get("events") or {}).items():
            events[symbol] = sorted(set(events.get(symbol, [])) | _dates(dates))
    if "events" in older or "events" in newer:
        out["events"] = events
    a, b = older.get("quota"), newer.get("quota")
    day_a = _quota_date(a.get("date")) if isinstance(a, dict) else None
    day_b = _quota_date(b.get("date")) if isinstance(b, dict) else None
    # 配额按北京台账日合并；section 更新时间不能把今天台账换回昨天。
    if day_a and (not day_b or day_a > day_b):
        out["quota"] = copy.deepcopy(a)
    elif day_b:
        out["quota"] = copy.deepcopy(b)
        if day_a == day_b:
            for key in ("symbols", "retry_symbols"):
                out["quota"][key] = sorted({v for q in (a, b) if isinstance(q.get(key), list)
                                             for v in q[key] if isinstance(v, str)})
    else:
        out.pop("quota", None)
    return out


def _merge_section(name, left, right, lm=None, rm=None, *, prefer_left=False):
    out = _merge_auxiliary(right, left) if prefer_left else _merge_auxiliary(left, right)
    metadata = {}
    if name in ENTRY_SECTIONS:
        a, b = left.get("data", {}), right.get("data", {})
        lm, rm = lm or {}, rm or {}
        data = {}
        for symbol in a.keys() | b.keys():
            old, new = a.get(symbol), b.get(symbol)
            if isinstance(old, dict) and isinstance(new, dict):
                data[symbol], metadata[symbol] = _merge_entry(name, old, new, lm.get(symbol), rm.get(symbol))
            elif isinstance(new, dict):
                data[symbol] = copy.deepcopy(new)
                metadata[symbol] = _entry_meta(name, new, rm.get(symbol))
            elif isinstance(old, dict):
                data[symbol] = copy.deepcopy(old)
                metadata[symbol] = _entry_meta(name, old, lm.get(symbol))
        if "data" in left or "data" in right:
            out["data"] = data
    return out, metadata


def verify_checkpoint(base, checkpoint_id, identity=None):
    """只读验证本生成步骤输出的 nonce、冻结身份、版本及缓存内容摘要。"""
    identity = build_identity() if identity is None else identity
    if not checkpoint_id or not all(identity.get(k) for k in ("source_sha", "input_hash", "build_id")):
        return False
    cache = _read_object(Path(base) / ".cache" / "market-monitor.json")
    proof, sections = cache.get("checkpoint"), cache.get("sections")
    if (type(cache.get("schema_version")) is not int or cache["schema_version"] != SCHEMA_VERSION
            or not isinstance(proof, dict)
            or not isinstance(sections, dict) or not sections or set(sections) - AUX_FIELDS.keys()
            or proof.get("id") != checkpoint_id or _entry_stamp(proof.get("asof")) is None
            or cache.get("asof") != proof.get("asof")
            or any(proof.get(k) != identity[k] for k in ("source_sha", "input_hash", "build_id"))):
        return False
    try:
        for name, section in sections.items():
            if (not isinstance(section, dict) or type(section.get("schema_version")) is not int
                    or section["schema_version"] != SCHEMA_VERSION or _stamp(section.get("asof")) is None
                    or not isinstance(section.get("data"), dict)
                    or section["data"] != _auxiliary(name, section["data"])):
                return False
            if name in ENTRY_SECTIONS:
                entries, metadata = section["data"].get("data", {}), section.get("entry_meta")
                if not isinstance(metadata, dict) or entries.keys() != metadata.keys():
                    return False
                if any(not isinstance(entry, dict) or metadata[symbol] != _entry_meta(name, entry, metadata[symbol])
                       for symbol, entry in entries.items()):
                    return False
        return proof.get("sections_sha256") == _digest(sections)
    except (ValueError, TypeError):
        return False


class RuntimeState:
    def __init__(self, base, *, target="", event="local"):
        self.base = Path(base)
        self.cache_path = self.base / ".cache" / "market-monitor.json"
        self.identity = build_identity()
        self.target, self.event = target, event
        self.fresh, self.today, self.expected = None, None, None
        self.sections = {}

    def load_previous(self, status):
        status = status if isinstance(status, dict) else {}
        previous = copy.deepcopy(status)
        previous.pop("fundamental", None)
        cache = _read_object(self.cache_path)
        compatible_cache = _version(cache.get("schema_version")) and _stamp(cache.get("asof"))
        cached = cache.get("sections") if compatible_cache else None
        cached = cached if isinstance(cached, dict) else {}
        for name in AUX_FIELDS:
            saved = status.get(name)
            saved = saved if isinstance(saved, dict) else {}
            entry = cached.get(name)
            entry = entry if isinstance(entry, dict) else {}
            cached_at = _stamp(entry.get("asof"))
            compatible = _version(entry.get("schema_version"))
            if not compatible or not cached_at or not isinstance(entry.get("data"), dict):
                entry, cached_at = {}, None
            old = _auxiliary(name, entry.get("data"))
            current = _auxiliary(name, saved)
            saved_at = _stamp(saved.get("asof")) or _stamp(status.get("finished_at"))
            saved_version = saved.get("schema_version", 1)
            if not _version(saved_version):
                continue
            cache_wins = bool(cached_at and (not saved_at or cached_at > saved_at))
            lm = entry.get("entry_meta") if entry.get("schema_version") == SCHEMA_VERSION else None
            rm = saved.get("entry_meta") if saved_version == SCHEMA_VERSION else None
            value, metadata = _merge_section(name, old, current,
                lm if isinstance(lm, dict) else {}, rm if isinstance(rm, dict) else {}, prefer_left=cache_wins)
            asof = entry.get("asof") if cache_wins else (saved.get("asof") if _stamp(saved.get("asof")) else status.get("finished_at"))
            if not value:
                continue
            asof = asof if _stamp(asof) else EPOCH
            self.sections[name] = {"schema_version": SCHEMA_VERSION, "asof": asof,
                                   "data": value, "entry_meta": metadata}
            # 旧取数接口不注入元字段；已有元字段则同步为实际合并结果。
            previous[name] = {**saved, **copy.deepcopy(value)}
            if "entry_meta" in saved:
                previous[name]["entry_meta"] = copy.deepcopy(metadata)
        return previous

    def checkpoint(self, name, value):
        auxiliary = _auxiliary(name, value)
        prior = self.sections.get(name, {})
        old, old_meta = prior.get("data", {}), prior.get("entry_meta", {})
        asof = datetime.now(timezone.utc).isoformat()
        incoming_meta = {}
        if name in ENTRY_SECTIONS:
            for symbol, entry in auxiliary.get("data", {}).items():
                if not isinstance(entry, dict):
                    continue
                before = old.get("data", {}).get(symbol, {})
                metadata = _entry_meta(name, before, old_meta.get(symbol))
                before_parts = _parts(name, before)
                for aspect, part in _parts(name, entry).items():
                    # 相同事实、相同退避及未参与本轮的标的，绝不随 section 晋升版本。
                    stamp = _own_stamp(name, aspect, entry)
                    if part != before_parts[aspect] or stamp != _own_stamp(name, aspect, before):
                        metadata[aspect] = {"asof": asof, "at": stamp.isoformat() if stamp else None,
                                            "digest": _digest(part)}
                incoming_meta[symbol] = metadata
        merged, metadata = _merge_section(name, old, auxiliary, old_meta, incoming_meta)
        sections = {**self.sections, name: {"schema_version": SCHEMA_VERSION, "asof": asof,
                                            "data": merged, "entry_meta": metadata}}
        checkpoint_id = uuid.uuid4().hex
        proof = {"id": checkpoint_id, "asof": asof, **self.identity,
                 "sections_sha256": _digest(sections)}
        atomic_write_json(self.cache_path, {"schema_version": SCHEMA_VERSION, "asof": asof,
                                           "sections": sections, "checkpoint": proof})
        self.sections = sections
        # 文件原子落盘后才出具本轮凭证。失败的后续 checkpoint 不会撤销此前有效凭证。
        output = os.environ.get("GITHUB_OUTPUT")
        if output and all(self.identity.values()):
            with open(output, "a", encoding="utf-8") as stream:
                stream.write(f"cache_checkpoint_id={checkpoint_id}\ncache_ready=true\n")
        result = {**value, **copy.deepcopy(merged), "schema_version": SCHEMA_VERSION, "asof": asof}
        if name in ENTRY_SECTIONS:
            keys = auxiliary.get("data", {}).keys()
            result["data"] = {k: copy.deepcopy(merged["data"][k]) for k in keys if k in merged.get("data", {})}
            result["entry_meta"] = {k: copy.deepcopy(metadata[k]) for k in result["data"]}
        return result

    def receipt_data(self, result, reason, **details):
        if result not in ("generated", "skipped", "failed"):
            raise ValueError("未知运行回执状态")
        return {"schema_version": 1, "result": result,
                 "target_trade_date": self.target, "fresh": self.fresh,
                 "today": self.today, "expected": self.expected,
                 **self.identity, "reason": reason, "event": self.event,
                 "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                 "request_id": os.environ.get("MM_REQUEST_ID", ""),
                 "finished_at": datetime.now(timezone.utc).isoformat(), **details}

    def receipt(self, result, reason, **details):
        value = self.receipt_data(result, reason, **details)
        atomic_write_json(self.base / "run-receipt.json", value)
        return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读验证本轮辅助缓存，不运行监控或联网")
    parser.add_argument("command", choices=("verify-cache",))
    parser.add_argument("--base", default=".")
    parser.add_argument("--checkpoint-id", required=True)
    args = parser.parse_args(argv)
    return 0 if verify_checkpoint(args.base, args.checkpoint_id) else 1


if __name__ == "__main__":
    raise SystemExit(main())
