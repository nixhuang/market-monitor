"""设置的单一 schema，不读写用户配置。"""
import math
import re
from datetime import date


def _number(default, lo, hi, *, integer=False, exclude=(), order=None):
    return dict(default=default, limits=(lo, hi), integer=integer, exclude=exclude, order=order)


SETTING_SCHEMA = {
    "boll_n": _number(20, 2, 250, integer=True),
    "boll_k": _number(2.0, 0.1, 10),
    "boll_near_pct": _number(0.5, 0, 100),
    "boll_near_band_pct": _number(15.0, 0, 100),
    "chg_red": _number(4.0, 0, 100, order=("chg", 1)),
    "chg_yellow": _number(2.0, 0, 100, order=("chg", 0)),
    "rsi_high": _number(70, 0, 100, order=("rsi", 1)),
    "rsi_low": _number(35, 0, 100, order=("rsi", 0)),
    "near_52w_low_pct": _number(1.0, 0, 100),
    "trigger_gap_pct": _number(5.0, 0, 100),
    "reduce_gap_pct": _number(3.0, 0, 100),
    "target_space_pct": _number(30.0, 30, 30),
    "target_high_space_pct": _number(70.0, 70, 70),
    "target_gap_pct": _number(3.0, 0, 100),
    "vol_ratio": _number(1.5, 0.1, 100, order=("volume", 0)),
    "vol_ratio_red": _number(3.0, 0.1, 100, order=("volume", 1)),
    "vol_ratio_z": _number(2.0, 0, 10, order=("volume_z", 0)),
    "vol_ratio_red_z": _number(3.6, 0, 10, order=("volume_z", 1)),
    "amp_yellow": _number(5.0, 0, 100, order=("amp", 0)),
    "amp_red": _number(8.0, 0, 100, order=("amp", 1)),
    "ma_short": _number(50, 0, 250, integer=True, exclude=(1,), order=("ma", 0)),
    "ma_long": _number(200, 0, 250, integer=True, exclude=(1,), order=("ma", 1)),
    "hy_green": _number(350, 0, 10000, order=("hy", 0)),
    "hy_red": _number(400, 0, 10000, order=("hy", 1)),
    "vix_green": _number(20, 0, 200, order=("vix", 0)),
    "vix_yellow": _number(30, 0, 200, order=("vix", 1)),
    "vix_red": _number(40, 0, 200, order=("vix", 2)),
    "use_realtime": _number(1, 0, 1, integer=True),
    "dca_every": _number(0, 0, 10000, integer=True),
    "dca_start": {"default": "", "type": "date"},
    "signal_single_open": _number(1, 0, 1, integer=True),
}

DEFAULT_SETTINGS = {key: spec["default"] for key, spec in SETTING_SCHEMA.items()}
SETTING_LIMITS = {key: spec["limits"] for key, spec in SETTING_SCHEMA.items() if "limits" in spec}
_groups = {}
for _key, _spec in SETTING_SCHEMA.items():
    if _spec.get("order"):
        _group, _rank = _spec["order"]
        _groups.setdefault(_group, []).append((_rank, _key))
SETTING_RELATIONS = tuple(dict(keys=tuple(key for _, key in sorted(entries)),
    strict=group == "rsi", skip_zero=group in ("ma", "volume_z"))
    for group, entries in _groups.items())
SETTING_ORDER = tuple(rule["keys"] for rule in SETTING_RELATIONS if not rule["strict"])


def setting_error(key, value):
    spec = SETTING_SCHEMA.get(key)
    if spec is None:
        return "未知参数"
    if spec.get("type") == "date":
        if not isinstance(value, str):
            return "必须是 YYYY-MM-DD 字符串或空字符串"
        if not value:
            return ""
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            return "日期无效，必须是 YYYY-MM-DD"
        return ""
    lo, hi = spec["limits"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "必须是数字，不能是布尔值或字符串"
    if not lo <= value <= hi or not math.isfinite(value):
        return f"必须是 {lo:g}～{hi:g} 范围内的有限数"
    if spec["integer"] and value != int(value):
        return "必须是整数"
    if value in spec["exclude"]:
        return "周期只能是 0（关闭）或 2～250"
    return ""


def relation_invalid(values, rule):
    seq = [values[key] for key in rule["keys"]]
    if rule["skip_zero"] and 0 in seq:
        return False
    return any(a >= b if rule["strict"] else a > b for a, b in zip(seq, seq[1:]))


def valid_settings(values):
    if not isinstance(values, dict):
        return "settings 必须是对象"
    for key in values:
        if key not in SETTING_SCHEMA:
            return f"{key}：未知参数"
    for key in SETTING_SCHEMA:
        reason = setting_error(key, values.get(key))
        if reason:
            return key  # 保留 monitor.valid_settings 原有的错误键返回契约。
    for rule in SETTING_RELATIONS:
        if relation_invalid(values, rule):
            return "RSI 上下限" if rule["strict"] else " / ".join(rule["keys"])
    return ""


def resolve_settings(overrides, warn):
    if not isinstance(overrides, dict):
        raise ValueError("settings.json 顶层不是对象")
    values = dict(DEFAULT_SETTINGS)
    for key, value in overrides.items():
        if str(key).startswith("fund_"):
            continue  # 退役配置只忽略，不恢复默认项或触发配置写入。
        if key == "target_space_pct" and not isinstance(value, bool) and value == 50:
            continue  # 旧空间门槛静默忽略；固定30，不触发维护或配置写入。
        if key == "dca_start" and isinstance(value, str):
            value = value.strip()
        reason = setting_error(key, value)
        if reason:
            fallback = f"沿用默认值 {DEFAULT_SETTINGS[key]!r}" if key in DEFAULT_SETTINGS else "已忽略"
            warn(f"  ! 配置 {key} 无效（{reason}），{fallback}")
            continue
        spec = SETTING_SCHEMA[key]
        values[key] = int(value) if spec.get("integer") else value
    for rule in SETTING_RELATIONS:
        if relation_invalid(values, rule):
            warn(f"  ! {'/'.join(rule['keys'])} 阈值顺序冲突，仅该组退回默认值")
            for key in rule["keys"]:
                values[key] = DEFAULT_SETTINGS[key]
    return values


def browser_schema():
    return {"defaults": DEFAULT_SETTINGS, "limits": SETTING_LIMITS,
            "integers": [key for key, spec in SETTING_SCHEMA.items() if spec.get("integer")],
            "excluded": {key: spec["exclude"] for key, spec in SETTING_SCHEMA.items() if spec.get("exclude")},
            "relations": SETTING_RELATIONS}
