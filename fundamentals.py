# -*- coding: utf-8 -*-
"""基本面纯规则与质量门槛；不取数、不读写配置或缓存。"""
import math
from copy import deepcopy
from datetime import date, datetime


FUND_DEFAULT = {
    "fund_enable": 1,
    "fund_rev_red": -20.0,       # 最新季对三季前，不是同比
    "fund_rev_yellow": -10.0,
    "fund_om_drop": 3.0,
    "fund_gm_drop": 3.0,
    "fund_de_rise": 30.0,
    "fund_cr_drop": 20.0,
    "fund_roe_drop": 30.0,
    "fund_pm_abnormal": 60.0,
    "fund_report_grace_days": 90,  # 一个报告周期外的披露宽限；不是抓取缓存有效期
}
FUND_LIMITS = {
    "fund_enable": (0, 1), "fund_rev_red": (-100, 0), "fund_rev_yellow": (-100, 0),
    "fund_om_drop": (0, 100), "fund_gm_drop": (0, 100),
    "fund_de_rise": (0, 10000), "fund_cr_drop": (0, 100),
    "fund_roe_drop": (0, 10000), "fund_pm_abnormal": (0, 10000),
    "fund_report_grace_days": (0, 366),
}
FREQUENCY_DAYS = {"quarterly": 92, "semiannual": 184, "annual": 366}
MARGINS = {"Gross Margin", "Operating Margin", "After Tax ROE", "Profit Margin"}
TABLES = ("inc", "bs", "cf", "rt")


def failure_outcome(outcomes):
    """两源均明确无覆盖才允许长退避；传输失败优先于质量错误，不凭空表猜覆盖。"""
    sources = [dict(item) for item in outcomes]
    for kind in ("timeout", "http_error", "transient", "quality_invalid"):
        if any(item.get("kind") == kind for item in sources):
            return {"kind": kind, "sources": sources}
    kind = "no_coverage" if sources and all(item.get("kind") == "no_coverage" for item in sources) else "transient"
    return {"kind": kind, "sources": sources}


def settings_overrides(values, warn):
    out = dict(FUND_DEFAULT)
    for key, value in values.items():
        if not str(key).startswith("fund_"):
            continue
        if key not in FUND_DEFAULT:
            warn(f"  ! 未知基本面配置 {key}，已忽略")
            continue
        lo, hi = FUND_LIMITS[key]
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and number(value) is not None and lo <= value <= hi)
        if valid and key in ("fund_enable", "fund_report_grace_days"):
            valid = value == int(value)
        if not valid:
            warn(f"  ! 基本面配置 {key} 无效，沿用默认值 {FUND_DEFAULT[key]}")
            continue
        out[key] = type(FUND_DEFAULT[key])(value)
    if out["fund_rev_red"] > out["fund_rev_yellow"]:
        warn("  ! fund_rev_red/fund_rev_yellow 阈值顺序冲突，仅该组退回默认值")
        for key in ("fund_rev_red", "fund_rev_yellow"):
            out[key] = FUND_DEFAULT[key]
    return out


def period_date(value):
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def number(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        result = float(text)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def source_metadata(facts, source, fetched_at, table_periods, cell_periods=None):
    """仅在已知源边界归一流动比率；旧缓存未知源的裸数字绝不猜单位。"""
    out = deepcopy(facts)
    out.update(source=source, frequency="quarterly", fetched_at=fetched_at,
               schema_version=2, table_periods=deepcopy(table_periods),
               units={"amount": "thousands", "currency": None,
                      "margin": "percentage_points", "current_ratio": "times"})
    if cell_periods is not None:
        out["cell_periods"] = deepcopy(cell_periods)
    if source == "nasdaq":
        row = (out.get("rt") or {}).get("Current Ratio") or {}
        for col in ("value2", "value5"):
            value = number(row.get(col))
            row[col] = value / 100.0 if value is not None else None
    return out


def freshness(facts, today, grace_days=90):
    facts = facts if isinstance(facts, dict) else {}
    period = period_date(facts.get("period"))
    now = period_date(today)
    frequency = facts.get("frequency") if isinstance(facts.get("frequency"), str) else "unknown"
    # 旧缓存来自季度请求：仅用季度窗口保守拦过期，不把缺失频率伪装成已验证。
    limit = FREQUENCY_DAYS.get(frequency, FREQUENCY_DAYS["quarterly"]) + grace_days
    age = (date.fromisoformat(now) - date.fromisoformat(period)).days if now and period else None
    if age is None:
        status, message = "invalid_period", "报告期无效，待更新"
    elif age < 0:
        status, message = "future", "报告期在未来，待核实"
    elif age > limit:
        status, message = "stale", f"报告期 {period} 已过期，待更新"
    else:
        status, message = "current", ""
    return {"status": status, "current": status == "current", "period": period,
            "frequency": frequency, "age_days": age, "max_age_days": limit,
            "checked_at": today, "message": message}


def evaluate(facts, today, rule):
    f = facts if isinstance(facts, dict) else {}
    quality = freshness(f, today, rule("fund_report_grace_days"))
    period, base_period = period_date(f.get("period")), period_date(f.get("base_period"))
    label = lambda p: f"{p[:4]}/{int(p[5:7])}" if p else ""
    span = f"{label(base_period)}→{label(period)}" if period and base_period else ""
    result = {"period": f.get("period"), "base_period": f.get("base_period"),
              "compare": span, "context": [], "oneoff": False, "level": "gray", "hits": [],
              "source": f.get("source", "unknown"), "frequency": quality["frequency"],
              "fetched_at": f.get("fetched_at"), "quality": quality, "checks": {}}
    checks, context, hits = result["checks"], result["context"], result["hits"]
    names = ("revenue", "operating_margin", "operating_cash_flow", "gross_margin",
             "debt_equity", "current_ratio", "roe")
    if not quality["current"]:
        context.append(quality["message"])
        checks.update({name: {"status": "incomparable", "reason": quality["message"]} for name in names})
        quality.update(complete=False, missing=[], incomparable=list(names))
        return result

    units = f.get("units") if isinstance(f.get("units"), dict) else {}
    table_periods = f.get("table_periods") if isinstance(f.get("table_periods"), dict) else None
    cell_periods = f.get("cell_periods") if isinstance(f.get("cell_periods"), dict) else {}
    legacy = table_periods is None
    if legacy:
        context.append("旧缓存缺少分表日期元数据，按原报告期保留可识别风险，完整性待核实")
    if quality["frequency"] not in FREQUENCY_DAYS:
        context.append("报告频率未确认，暂按季度窗口拦截过期数据")
    delta = ((date.fromisoformat(period) - date.fromisoformat(base_period)).days
             if period and base_period else None)
    comparable = (quality["frequency"] in ("quarterly", "unknown")
                  and delta is not None and 240 <= delta <= 310)

    def cell(table, row, col):
        tbl = f.get(table) if isinstance(f.get(table), dict) else {}
        values = tbl.get(row) if isinstance(tbl.get(row), dict) else {}
        raw = values.get(col)
        value = number(raw)
        if value is None:
            return None, "missing"
        row_periods = cell_periods.get(table) if isinstance(cell_periods.get(table), dict) else {}
        periods = row_periods.get(row)
        if periods is None:
            periods = (table_periods or {}).get(table) if not legacy else {"value2": period, "value5": base_period}
        expected = period if col == "value2" else base_period
        if not isinstance(periods, dict) or not expected or period_date(periods.get(col)) != expected:
            return None, "incomparable"
        if col == "value5" and not comparable:
            return None, "incomparable"
        if table == "rt":
            if row == "Current Ratio":
                if units.get("current_ratio") == "times":
                    pass
                elif "%" in str(raw):
                    value /= 100.0
                else:
                    return None, "incomparable"
            elif row in MARGINS and units.get("margin") != "percentage_points" and "%" not in str(raw):
                return None, "incomparable"
        return value, "checked"

    def pair(table, row):
        return cell(table, row, "value2"), cell(table, row, "value5")

    def record(name, values, valid=True, reason=""):
        states = [item[1] for item in values]
        state = "incomparable" if "incomparable" in states or not valid else ("missing" if "missing" in states else "checked")
        checks[name] = {"status": state}
        if state != "checked":
            checks[name]["reason"] = reason or ("日期或单位不可比较" if state == "incomparable" else "缺少所需单元格")

    red = False
    def add(text, severe=False):
        nonlocal red
        hits.append(text)
        red = red or severe

    rev, om, cfo = pair("inc", "Total Revenue"), pair("rt", "Operating Margin"), pair("cf", "Net Cash Flow-Operating")
    gm, cr, roe = pair("rt", "Gross Margin"), pair("rt", "Current Ratio"), pair("rt", "After Tax ROE")
    tl, te = pair("bs", "Total Liabilities"), pair("bs", "Total Equity")
    pm, tax = pair("rt", "Profit Margin"), pair("inc", "Income Tax")
    r0, r4 = rev[0][0], rev[1][0]
    om0, om4 = om[0][0], om[1][0]
    c0, c4 = cfo[0][0], cfo[1][0]
    gm0, gm4 = gm[0][0], gm[1][0]
    cr0, cr4 = cr[0][0], cr[1][0]
    roe0, roe4 = roe[0][0], roe[1][0]
    tag = f"（{span}）" if span else ""

    record("revenue", rev, r4 is None or r4 > 0, "营收基期缺失或不为正，无法计算降幅")
    if r0 is not None and r4 is not None and r4 > 0:
        change = (r0 / r4 - 1) * 100
        if change <= rule("fund_rev_red"):
            add(f"营收较{label(base_period) or '3季前'} {change:+.0f}%", True)
        elif change <= rule("fund_rev_yellow"):
            add(f"营收较{label(base_period) or '3季前'} {change:+.0f}%")
    record("operating_margin", om)
    if om0 is not None:
        if om0 < 0:
            add(f"营业亏损 {om0:.0f}%", True)
        elif om4 is not None and om0 - om4 <= -rule("fund_om_drop"):
            add(f"营业利率 {om4:.1f}%→{om0:.1f}%{tag}")
        context.append(f"本季营业利率 {om0:.1f}%")
    record("operating_cash_flow", cfo)
    if c0 is not None:
        if c0 < 0 and c4 is not None and c4 > 0:
            add("经营现金流转负", True)
        elif c0 < 0:
            # 缺失金额单位时只能报告符号，不能把源未知的数字冒充百万。
            amount = f" {c0 / 1000:.0f}M" if units.get("amount") == "thousands" else ""
            add("经营现金流为负" + amount)
        context.append("本季经营现金流为" + ("正" if c0 > 0 else "负" if c0 < 0 else "零"))
    record("gross_margin", gm)
    if gm0 is not None and gm4 is not None and gm0 - gm4 <= -rule("fund_gm_drop"):
        add(f"毛利率 {gm4:.1f}%→{gm0:.1f}%{tag}")
    de0 = tl[0][0] / te[0][0] if tl[0][0] is not None and te[0][0] else None
    de4 = tl[1][0] / te[1][0] if tl[1][0] is not None and te[1][0] else None
    record("debt_equity", (*tl, *te), te[0][0] != 0 and te[1][0] != 0 and (de4 is None or de4 > 0))
    if de0 is not None and de4 is not None and de4 > 0 and de0 / de4 - 1 >= rule("fund_de_rise") / 100.0 and tl[0][0] > tl[1][0]:
        add(f"负债/权益 {de4:.2f}→{de0:.2f}倍{tag}")
    record("current_ratio", cr, cr4 is None or cr4 > 0)
    if cr0 is not None and cr4 is not None and cr4 > 0 and cr0 / cr4 - 1 <= -rule("fund_cr_drop") / 100.0:
        add(f"流动比率 {cr4:.2f}→{cr0:.2f}倍{tag}")

    def distorted(index):
        p, o, t = pm[index][0], om[index][0], tax[index][0]
        return ((p is not None and abs(p) > rule("fund_pm_abnormal")) or (t is not None and t < 0)
                or (p is not None and o is not None and p - o > 20))
    oneoff, base_oneoff = distorted(0), distorted(1)
    result["oneoff"] = oneoff
    if oneoff:
        checks["roe"] = {"status": "checked", "reason": "本季一次性损益污染，按既定规则排除ROE"}
    else:
        record("roe", (*roe, *pm, *tax, *om), roe4 is None or roe4 > 0)
        # 净利污染检查缺失时，不以未核实的ROE单独报黄。
        if checks["roe"]["status"] == "checked" and roe4 > 0 and roe0 / roe4 - 1 <= -rule("fund_roe_drop") / 100.0:
            ref = "（基准季含一次性收益，仅供参考）" if base_oneoff else ""
            add(f"季度ROE {roe4:.1f}%→{roe0:.1f}%{tag}{ref}")
    if hits and oneoff:
        hits.append("本季净利含一次性损益，ROE未参与判定")
    missing = [name for name in names if checks[name]["status"] == "missing"]
    incomparable = [name for name in names if checks[name]["status"] == "incomparable"]
    complete = not missing and not incomparable and not legacy and quality["frequency"] == "quarterly"
    quality.update(complete=complete, missing=missing, incomparable=incomparable)
    if missing:
        context.append("数据不足：" + "、".join(missing))
    if incomparable:
        context.append("日期或单位不可比较：" + "、".join(incomparable))
    if not complete:
        quality["status"] = "partial"
    result["level"] = ("red" if red else "yellow") if hits else ("green" if complete else "gray")
    return result
