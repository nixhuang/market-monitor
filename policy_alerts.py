"""独立通胀、利率与原油参考：规则型提示，不参与市场综合评分。
纯解析/判定不联网；update_policy 才取数。调用方负责保存缓存。
"""
import html
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

import requests

NY = ZoneInfo("America/New_York")
BJ = timezone(timedelta(hours=8))
BLS_URL = "https://www.bls.gov/news.release/cpi.nr0.htm"
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED_URL = "https://fred.stlouisfed.org/series/CPILFESL"
MONTHS = {name: i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
RULES = ("自定义参考规则：核心CPI季调环比≥0.3%注意、单月≥0.4%偏热，连续两月≥0.4%警惕；"
         "新加息14天内警惕，之后保留收紧提示；年末利率预测高于当前区间中点提示可能进一步收紧。"
         "原油同一实际合约近5交易日涨≥10%注意/≥20%警惕，近20交易日涨≥20%注意/≥30%警惕。"
         "均不是官方风险线、市场概率或交易指令，不推断确定加息或股市跌幅。")


def plain(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", text))).strip()


def moment(value):
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.replace(tzinfo=BJ) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        return None


def clock(now=None):
    return now or datetime.now(BJ)


def fetch_text(url):
    for attempt in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.exceptions.RequestException:
            if attempt or ("r" in locals() and r.status_code in (403, 429)):
                raise


def rounding(value):
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_cpi(text):
    """只认BLS核心季调环比行，不认整体CPI或同比段落。"""
    flat = plain(text)
    month = re.search(r"CONSUMER PRICE INDEX\s*[-–]\s*([A-Z]+)\s+(\d{4})", flat)
    if not month or month[1].title() not in MONTHS:
        raise ValueError("CPI统计月份无法确认")
    y, m = int(month[2]), MONTHS[month[1].title()]
    values = None
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, re.S | re.I):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        if cells and plain(cells[0]).lower().rstrip(".") == "all items less food and energy":
            nums = [plain(c) for c in cells[1:]]
            if len(nums) == 8 and all(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", v) for v in nums):
                values = [float(v) for v in nums[:-1]][-3:]
                break
    if values is None or "Seasonally adjusted changes from preceding month" not in flat:
        raise ValueError("未取得经确认的核心CPI季调环比表格")
    release = re.search(r"8:30\s*a\.m\.\s*\(ET\).*?(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})", flat)
    upcoming = re.search(r"news release for .*?scheduled to be published on.*?(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4}),\s*at 8:30", flat, re.I)
    if not release or not upcoming:
        raise ValueError("CPI发布时间无法确认")
    def stamp(match):
        return datetime(int(match[3]), MONTHS[match[1].title()], int(match[2]), 8, 30, tzinfo=NY).isoformat()
    return {"date": f"{y:04d}-{m:02d}", "mom": values[-1], "previous": values[-2],
            "history": values, "released_at": stamp(release), "next_release": stamp(upcoming),
            "source": "BLS季调核心CPI公告", "url": BLS_URL}


def cpi_from_index(rows, now=None):
    """FRED CPILFESL月度季调指数备用；禁止跨缺月计算。"""
    n = clock(now)
    unique = {}
    for d, v in rows:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt.date() <= n.date() and math.isfinite(v) and v > 0:
                unique[d[:7]] = float(v)
        except (ValueError, TypeError):
            continue
    tail = sorted(unique.items())[-4:]
    if len(tail) < 4:
        raise ValueError("核心CPI连续月份不足")
    for (a, _), (b, _) in zip(tail, tail[1:]):
        ay, am = map(int, a.split("-")); by, bm = map(int, b.split("-"))
        if by * 12 + bm != ay * 12 + am + 1:
            raise ValueError("核心CPI缺月，不能跨月计算")
    changes = [rounding((Decimal(str(b)) / Decimal(str(a)) - 1) * 100)
               for (_, a), (_, b) in zip(tail, tail[1:])]
    return {"date": tail[-1][0], "mom": changes[-1], "previous": changes[-2], "history": changes,
            "source": "BLS / FRED CPILFESL季调指数计算", "url": FRED_URL}


def cpi_warning(data, now=None):
    n = clock(now)
    result = dict(data or {})
    if result.get("fetch_error"):
        return {**result, "level": "gray", "text": "本期通胀数据未取得，不能确认压力是否缓解。"}
    try:
        dt = datetime.strptime(result["date"], "%Y-%m")
        age = n.year * 12 + n.month - dt.year * 12 - dt.month
        value = result["mom"]
        if not valid_number(value):
            raise ValueError("异常值")
        due = moment(result.get("next_release"))
        released = moment(result.get("released_at"))
        # 无精确公布日的指数备用源只允许最近一个月，不能让两个月前的旧值亮绿。
        if age < 1 or age > 2 or (not due and age > 1) or (due and n >= due) or (released and n < released):
            return {**result, "level": "gray", "text": "通胀数据待更新，不能用旧月份判断当前压力。"}
        history = result.get("history") or []
        if not isinstance(history, list) or any(not valid_number(v) for v in history) or (history and history[-1] != value):
            raise ValueError("环比历史与本期数据不一致")
        if len(history) >= 2 and all(v >= .4 for v in history[-2:]):
            lv, text = "red", "核心通胀连续偏热，利率压力上升；不代表美联储必然加息。"
        elif value >= .4:
            lv, text = "yellow", "本月核心通胀偏热，可能增加利率压力，需观察是否持续。"
        elif value >= .3:
            lv, text = "yellow", "核心通胀仍需留意，暂不能把降息当作确定的利好。"
        else:
            lv, text = "green", "本月核心通胀未触发偏热线；不等于整体市场安全。"
        return {**result, "level": lv, "text": text}
    except (KeyError, ValueError, TypeError):
        return {**result, "level": "gray", "text": "未取得有效核心通胀数据，暂不能判断。"}


def parse_calendar(text, now=None):
    n = clock(now)
    meetings = []
    for match in re.finditer(r"(\d{4}) FOMC Meetings", text):
        year = int(match[1])
        section = text[match.end():]
        end = re.search(r"\d{4} FOMC Meetings", section)
        if end:
            section = section[:end.start()]
        months = list(re.finditer(r'<div[^>]*class="[^"]*fomc-meeting__month[^"]*"[^>]*>(.*?)</div>', section, re.S))
        for i, month in enumerate(months):
            name = plain(month[1])
            if name not in MONTHS:
                continue
            block = section[month.end():months[i+1].start() if i+1 < len(months) else len(section)]
            day = re.search(r'class="[^"]*fomc-meeting__date[^"]*"[^>]*>\s*(\d+)(?:\s*[-–]\s*(\d+))?(\*)?', block)
            if not day:
                continue
            dt = datetime(year, MONTHS[name], int(day[2] or day[1]), 14, tzinfo=NY)
            if dt.year < n.year - 1 or dt.year > n.year + 1:
                continue
            statements = re.findall(r'href="(/newsevents/pressreleases/monetary\d{8}a\.htm)"', block)
            projections = re.findall(r'href="(/monetarypolicy/fomcprojtabl\d{8}\.htm)"', block)
            meetings.append({"date": dt.date().isoformat(), "at": dt.isoformat(), "sep": bool(day[3]),
                             "statement": "https://www.federalreserve.gov" + statements[0] if statements else "",
                             "projection": "https://www.federalreserve.gov" + projections[0] if projections else ""})
    # 页面导航有重复年份链接，按会议日期去重。
    meetings = sorted({m["date"]: m for m in meetings}.values(), key=lambda m: m["date"])
    if not meetings:
        raise ValueError("FOMC官方日历解析失败")
    past = [m for m in meetings if moment(m["at"]) <= n]
    future = [m for m in meetings if moment(m["at"]) > n]
    if not past or not future:
        raise ValueError("未取得最新与下一次会议日程")
    seps = [m for m in past if m["sep"]]
    next_seps = [m for m in future if m["sep"]]
    return {"latest": past[-1], "next": future[0], "latest_sep": seps[-1] if seps else None,
            "next_sep": next_seps[0] if next_seps else None}


def rate_number(text):
    text = text.strip()
    if re.fullmatch(r"\d+-\d+/\d+", text):
        whole, fraction = text.split("-"); a, b = fraction.split("/")
        return int(whole) + int(a) / int(b)
    return float(text)


def parse_statement(text):
    flat = plain(text)
    pat = r"decided to (raise|lower|maintain|keep).*?target range for the federal funds rate.*?\b(?:at|to)\s+(\d+(?:\.\d+|-\d+/\d+)?)\s+to\s+(\d+(?:\.\d+|-\d+/\d+)?)\s+percent"
    match = re.search(pat, flat, re.I)
    if not match:
        raise ValueError("声明目标利率区间未识别")
    low, high = rate_number(match[2]), rate_number(match[3])
    if not 0 <= low <= high <= 25 or high-low > 1:
        raise ValueError("声明利率区间异常")
    return {"action": match[1].lower(), "low": low, "high": high}


def parse_projection(text, year=None):
    # 按官方Median年份列识别，跨年读取上年十二月SEP中的本年列。
    columns = {int(plain(value)): key for key, value in re.findall(
        r'<th[^>]*id="(xt1b\d+)"[^>]*>(.*?)</th>', text, re.S)
        if re.fullmatch(r"\d{4}", plain(value))}
    if not columns:
        raise ValueError("SEP中位数年份表头无法确认")
    year = min(columns) if year is None else year
    if year not in columns:
        raise ValueError("SEP缺少本年利率预测列")
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, re.S | re.I)
    for i, row in enumerate(rows):
        if "Federal funds rate" not in plain(row):
            continue
        def median(r):
            for heads, value in re.findall(r'<td[^>]*headers="([^"]*)"[^>]*>(.*?)</td>', r, re.S):
                if {"xt1a2", columns[year]} <= set(heads.split()):
                    number = float(plain(value))
                    if not valid_number(number) or not 0 <= number <= 25:
                        raise ValueError("SEP利率中位数异常")
                    return number
            raise ValueError("SEP利率中位数缺失")
        result = {"year": year, "median": median(row)}
        if i+1 < len(rows) and "projection" in plain(rows[i+1]).lower():
            result["previous_median"] = median(rows[i+1])
        return result
    raise ValueError("SEP利率预测行缺失")


def fed_warning(data, now=None):
    n = clock(now); result = dict(data or {})
    if result.get("fetch_error") or not result.get("date"):
        return {**result, "level": "gray", "text": "美联储数据待更新，暂不能确认利率压力。"}
    dt = moment(result["date"])
    due = moment(result.get("next_release")); sep_due = moment(result.get("next_sep"))
    if not dt or dt > n or (n-dt).days > 75 or (due and n >= due) or (sep_due and n >= sep_due):
        return {**result, "level": "gray", "text": "新会议结果尚未取得，旧判断暂不能代表当前政策。"}
    low, high, median = result.get("low"), result.get("high"), result.get("median")
    if (not valid_number(low) or not valid_number(high) or not 0 <= low <= high <= 25
            or high-low > 1 or not valid_number(median) or result.get("year") != n.year
            or not 0 <= median <= 25 or result.get("action") not in ("raise", "lower", "maintain", "keep")):
        return {**result, "level": "gray", "text": "政策数据或本年预测不完整，暂不能确认利率压力。"}
    midpoint = (low+high)/2
    tightening = median-midpoint > .075
    if result.get("action") == "raise":
        lv = "red" if (n-dt).days <= 14 else "yellow"
        text = "美联储最近一次会议已加息，利率压力仍需留意。"
    elif tightening:
        lv, text = "yellow", "最新利率预测仍指向进一步收紧，不能把降息当作确定利好。"
    else:
        lv, text = "green", "最新政策数据未触发进一步收紧提示；不等于经济风险消失。"
    if tightening:
        text += "年末预测仍高于当前利率，但不是加息承诺。"
    return {**result, "level": lv, "text": text}


def policy_summary(cpi, fed, now=None):
    c = cpi_warning(cpi, now); f = fed_warning(fed, now)
    levels = [c["level"], f["level"]]
    missing = "gray" in levels
    if "red" in levels:
        lv, text = "red", "通胀或政策收紧压力较强，需警惕利率对市场的冲击。"
    elif "yellow" in levels:
        lv, text = "yellow", "通胀／利率压力仍需留意，暂不能把降息当作确定利好。"
    elif missing:
        lv, text = "gray", "关键数据待更新，暂不能确认通胀／利率风险是否缓解。"
    else:
        lv, text = "green", "当前数据未触发新增通胀／利率压力提示，不代表市场安全。"
    if missing and lv != "gray":
        text += "部分数据待更新，判断不完整。"
    return {"level": lv, "text": text, "incomplete": missing}


def update_policy(prev=None, now=None, fred_loader=None):
    n = clock(now); prev = prev or {}
    def update(key, loader):
        old = prev.get(key) or {}
        checked = moment(old.get("checked_at")); due = moment(old.get("next_release"))
        invalid = (cpi_warning(old, n) if key == "cpi" else fed_warning(old, n))["level"] == "gray"
        retry = timedelta(hours=1 if old.get("fetch_error") or invalid else 24)
        year_changed = key == "fed" and old.get("year") != n.year
        if checked and checked <= n and n-checked < retry and (not due or n < due) and not year_changed:
            return old
        try:
            return {**loader(), "checked_at": n.isoformat(), "fetch_error": ""}
        except Exception as exc:
            # 保留原始证据供折叠查看，但不得把失败包装成成功。
            return {**old, "checked_at": n.isoformat(), "fetch_error": str(exc)[:180]}
    def cpi_loader():
        try:
            return parse_cpi(fetch_text(BLS_URL))
        except Exception:
            if not fred_loader:
                raise
            data = cpi_from_index(fred_loader("CPILFESL"), n)
            # 保留已核实的下次发布日期；指数源本身不提供精确发布时间。
            old = prev.get("cpi") or {}
            if old.get("date") == data["date"]:
                for key in ("next_release", "released_at"):
                    if old.get(key): data[key] = old[key]
            return data
    def fed_loader():
        calendar = parse_calendar(fetch_text(FED_URL), n)
        last, nxt, sep = calendar["latest"], calendar["next"], calendar["latest_sep"]
        if not last["statement"] or not sep or not sep["projection"]:
            raise ValueError("最新政策文件尚未提供")
        if (n-moment(sep["at"])).days > 110:
            raise ValueError("季度预测已过期")
        future_sep = (calendar.get("next_sep") or {}).get("at")
        return {**parse_statement(fetch_text(last["statement"])),
                **parse_projection(fetch_text(sep["projection"]), n.year),
                "date": last["at"], "projection_date": sep["date"],
                "next_release": nxt["at"], "next_has_sep": nxt["sep"], "next_sep": future_sep,
                "source": "美联储官方声明／季度预测", "url": last["statement"], "projection_url": sep["projection"]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(update, "cpi", cpi_loader); b = pool.submit(update, "fed", fed_loader)
        cpi, fed = a.result(), b.result()
    return {"cpi": cpi, "fed": fed, "summary": policy_summary(cpi, fed, n), "rules": RULES}


def oil_warning(continuous, contract=None, target=None):
    """主连只显示价格，变动判定必须用同一交割合约；未知换月不猜测。"""
    result = {"level": "gray", "text": "原油数据待更新，暂不能判断成本压力。", "source": "Yahoo NYMEX WTI期货",
              "url": "https://finance.yahoo.com/quote/CL=F/"}
    if not continuous or continuous.get("quote_symbol") != "CL=F" or continuous.get("quote_type") != "FUTURE" or continuous.get("currency") != "USD" or continuous.get("quote_timezone") != "America/New_York":
        return result
    dates, closes = continuous.get("dates") or [], continuous.get("closes") or []
    if not dates or not closes or len(dates) != len(closes) or dates[-1] != target:
        return result
    value = closes[-1]
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return result
    result.update(price=value, date=dates[-1], contract=continuous.get("quote_name"))
    if not contract or contract.get("quote_type") != "FUTURE" or contract.get("currency") != "USD" or contract.get("quote_timezone") != "America/New_York" or not oil_contract_symbol(continuous.get("quote_name")) or contract.get("quote_symbol") != oil_contract_symbol(continuous.get("quote_name")) or contract.get("quote_name") != continuous.get("quote_name"):
        return {**result, "text": "油价已取得，同月份合约未核实；暂不判定涨幅，避免换月误报。"}
    ds, cs = contract.get("dates") or [], contract.get("closes") or []
    if len(cs) < 2 or len(ds) != len(cs) or ds[-1] != target or ds != sorted(set(ds)) or any(not valid_number(v) or v <= 0 for v in cs) or abs(cs[-1]/value-1) > .005:
        return {**result, "text": "同月份原油历史不足或口径不一致，暂不判定涨幅。"}
    # 日涨跌、5日和20日全部使用同一合约；足够两根时仍可显示日变化。
    def change(period):
        return float((Decimal(str(cs[-1])) / Decimal(str(cs[-period-1])) - 1) * 100) if len(cs) > period else None
    day, week, month = change(1), change(5), change(20)
    incomplete = week is None or month is None
    if (week is not None and week >= 20) or (month is not None and month >= 30):
        lv, text = "red", "原油快速上涨，能源与运输成本压力明显增加；不等于必然加息。"
    elif (week is not None and week >= 10) or (month is not None and month >= 20):
        lv, text = "yellow", "原油涨幅偏快，需留意后续通胀和企业成本压力。"
    elif incomplete:
        lv, text = "gray", "日涨跌已核实，5日或20日历史不足，成本压力判断待更新。"
    else:
        lv, text = "green", "原油暂未触发快速上涨警示；下跌也不能直接视为经济利好。"
    if incomplete and lv != "gray":
        text += "部分历史不足，判断不完整。"
    return {**result, "level": lv, "text": text, "day": day, "day_from": ds[-2],
            "week": week, "month": month, "incomplete": incomplete, "contract_symbol": contract["quote_symbol"]}


def oil_contract_symbol(name):
    match = re.fullmatch(r"Crude Oil ([A-Za-z]{3}) (\d{2})", name or "")
    months = {n[:3]: "FGHJKMNQUVXZ"[m-1] for n, m in MONTHS.items()}
    return "CL" + months[match[1]] + match[2] + ".NYM" if match and match[1] in months else None
