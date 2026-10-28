"""美联储正式利率决议信息与独立原油参考，不参与市场综合评分。
纯解析/渲染不联网；update_policy 才取官方文本，调用方负责保存缓存。
"""
import html
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse, urljoin
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
BJ = timezone(timedelta(hours=8))
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
MONTHS = {name: i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
RULES = ("美联储仅展示最近已核实的正式利率决议、目标区间、发布日期和官方来源；"
         "调整基点仅在声明明确且可核实时显示，不从区间或其他数据猜测。"
         "决议信息为neutral中性状态，不作风险灯或买卖解读。成功后持久缓存，"
         "日常仅本地比较下次公布时间，到期随下一次监测运行查询新决议，不按24小时重复抓取。"
         "失败或待更新后至少1小时重试，到期不绕过错误限速；日程缺失仅补查日历，"
         "已核实的同一声明不重复下载。仅按例行日程更新，临时决议可能延迟发现。"
         "旧正式决议不按天数或跨年删除，新决议到期或查询失败明确待更新并保留旧日期。"
         "原油同一实际合约近5交易日涨≥10%注意/≥20%警惕，近20交易日涨≥20%注意/≥30%警惕。"
         "原油参考不计入综合灯，不是官方风险线、市场概率或交易指令。")


def plain(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", text))).strip()


def moment(value):
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.replace(tzinfo=BJ) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        return None


def clock(now=None):
    n = now or datetime.now(BJ)
    return n.replace(tzinfo=BJ) if n.tzinfo is None else n


def official_statement_url(url):
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.federalreserve.gov"
            or parsed.query or parsed.fragment):
        return None
    return re.fullmatch(r"/newsevents/pressreleases/monetary(\d{8})a\.htm", parsed.path)


def fetch_text(url):
    if url != FED_URL and not official_statement_url(url):
        raise ValueError("仅允许美联储官方日历和正式声明链接")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, allow_redirects=False)
    r.raise_for_status()
    if r.status_code != 200:
        raise ValueError("官方响应不是完整正文")
    r.encoding = "utf-8"
    return r.text


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def class_elements(text, name):
    # 支持属性顺序、单双引号、附加class、嵌套行内标签和大小写。
    pattern = (r"<(?P<tag>[a-z][\w]*)\b[^>]*\bclass\s*=\s*(?P<quote>[\"'])"
               r"[^\"']*\b" + re.escape(name) + r"\b[^\"']*(?P=quote)[^>]*>"
               r"(?P<body>.*?)</(?P=tag)\s*>")
    return list(re.finditer(pattern, text, re.S | re.I))


def parse_calendar(text, now=None):
    n = clock(now)
    # 标题可含行内标签；保留会议中的真实href，不构造声明地址。
    normalized = text
    headings = list(re.finditer(r"\b(\d{4})(?:\s|<[^>]*>)+FOMC(?:\s|<[^>]*>)+Meetings\b", normalized, re.I))
    meetings = {}
    for i, heading in enumerate(headings):
        year = int(heading[1])
        section = normalized[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(normalized)]
        # 月份元素在较深的div中，不能用一个外层div正则吞掉整个会议行。
        months = list(re.finditer(r"<div\b[^>]*\bclass\s*=\s*([\"'])[^\"']*\bfomc-meeting__month\b[^\"']*\1[^>]*>(.*?)</div\s*>", section, re.S | re.I))
        for j, month in enumerate(months):
            names = [v.title() for v in re.findall(r"[A-Za-z]+", plain(month[2])) if v.title() in MONTHS]
            if not names:
                continue
            block = section[month.end():months[j + 1].start() if j + 1 < len(months) else len(section)]
            dates = class_elements(block, "fomc-meeting__date")
            if not dates:
                continue
            day = re.match(r"(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?(?=\D|$)", plain(dates[0]["body"]))
            if not day:
                continue
            try:
                dt = datetime(year, MONTHS[names[-1]], int(day[2] or day[1]), 14, tzinfo=NY)
            except ValueError:
                continue
            links = re.findall(r"\bhref\s*=\s*[\"']([^\"']+)[\"']", block, re.I)
            statements = []
            for link in links:
                url = urljoin(FED_URL, html.unescape(link))
                match = official_statement_url(url)
                if match and match[1] == dt.strftime("%Y%m%d"):
                    statements.append(url)
            unscheduled = bool(re.search(r"unscheduled|notation vote|conference call", plain(month[2]) + " " + plain(dates[0]["body"]), re.I))
            if unscheduled and not statements:
                continue
            meeting = {"date": dt.date().isoformat(), "at": dt.isoformat(),
                       "statement": statements[0] if statements else "", "unscheduled": unscheduled}
            # 临时会议的发布时间必须从真实声明确认，不能假定也是下午两点。
            past = dt.date() <= n.astimezone(NY).date() if unscheduled else dt <= n
            meeting["past"] = past
            old = meetings.get(meeting["date"])
            if old is None or meeting["statement"]:
                meetings[meeting["date"]] = meeting
    ordered = sorted(meetings.values(), key=lambda m: m["date"])
    past = [m for m in ordered if m.pop("past")]
    future = [m for m in ordered if m not in past]
    statements = [m for m in past if m["statement"]]
    if not past:
        raise ValueError("FOMC官方日历未取得已到期的会议")
    return {"latest": past[-1], "latest_statement": statements[-1] if statements else None,
            "next": future[0] if future else None}


def rate_number(text):
    text = text.strip().replace("–", "-").replace("−", "-")
    mixed = re.fullmatch(r"(\d+)[ -](\d+)/(\d+)", text)
    fraction = re.fullmatch(r"(\d+)/(\d+)", text)
    if mixed or fraction:
        match = mixed or fraction
        a, b = int(match[2 if mixed else 1]), int(match[3 if mixed else 2])
        if not b or a >= b:
            raise ValueError("利率分数异常")
        return (int(match[1]) if mixed else 0) + a / b
    return float(text)


def parse_statement(text, expected_date):
    expected = moment(expected_date)
    dates = class_elements(text, "article__time")
    if not expected or len(dates) != 1:
        raise ValueError("声明发布日期无法确认")
    try:
        published = datetime.strptime(plain(dates[0]["body"]), "%B %d, %Y").date()
    except ValueError as exc:
        raise ValueError("声明发布日期无法确认") from exc
    if published != expected.date():
        raise ValueError("声明发布日期与官方日历不匹配")
    numbers = r"(?:\d+[- ]\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
    pattern = (r"\bThe Committee (?:has )?decided to (raise|lower|maintain|keep)\s+"
               r"the target range for the federal funds rate\s*"
               r"((?:[^.!?;]|\.(?=\d))*?)\b(at|to)\s+(" + numbers + r")\s+to\s+(" + numbers + r")\s+percent\b")
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p\s*>", text, re.S | re.I)
    decisions = []
    for paragraph in paragraphs:
        flat = plain(paragraph).replace("–", "-")
        # 不在投票/异议段落取数；句子边界保留小数点，绝不跨句寻找目标区间。
        if re.search(r"\bdissent\w*\b|voting against|\bpreferred\b|would have", flat, re.I):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat):
            match = re.search(pattern, sentence, re.I)
            if not match:
                continue
            action, adjustment, preposition = match[1].lower(), match[2].strip(), match[3].lower()
            low, high = rate_number(match[4]), rate_number(match[5])
            if not 0 <= low < high <= 25 or high - low > 1:
                raise ValueError("声明利率区间异常")
            if ((action in ("maintain", "keep") and (adjustment or preposition != "at"))
                    or (action in ("raise", "lower") and (preposition != "to" or (adjustment and not adjustment.lower().startswith("by "))))):
                raise ValueError("声明动作与目标区间表述不一致")
            decision = {"action": action, "low": low, "high": high, "published_date": published.isoformat()}
            amount = re.fullmatch(r"by\s+(\d+(?:\.\d+|/\d+)?|one-quarter|one-half|¼|½)\s+(percentage points?|basis points?)", adjustment, re.I)
            if amount:
                value = {"one-quarter": .25, "one-half": .5, "¼": .25, "½": .5}.get(amount[1].lower())
                value = rate_number(amount[1]) if value is None else value
                bps = value * (100 if amount[2].lower().startswith("percentage") else 1)
                if not 0 < bps <= 1000:
                    raise ValueError("声明调整幅度异常")
                decision["change_bps"] = bps
            decisions.append(decision)
    if not decisions or any(d != decisions[0] for d in decisions[1:]):
        raise ValueError("正式声明动作和目标利率区间无法唯一确认")
    result = decisions[0]
    release = re.search(r"For release at (\d{1,2}):(\d{2})\s*(a|p)\.m\.\s*(EST|EDT)", plain(text), re.I)
    if release:
        hour = int(release[1]) % 12 + (12 if release[3].lower() == "p" else 0)
        if not 1 <= int(release[1]) <= 12 or not 0 <= int(release[2]) < 60:
            raise ValueError("声明发布时间异常")
        at = datetime(published.year, published.month, published.day, hour, int(release[2]), tzinfo=NY)
        if at.tzname() != release[4].upper():
            raise ValueError("声明发布时间时区不一致")
        result["released_at"] = at.isoformat()
    return result


def fed_decision(data, now=None):
    n = clock(now)
    result = dict(data) if isinstance(data, dict) else {}
    dt = moment(result.get("date"))
    released = moment(result.get("released_at"))
    low, high, action = result.get("low"), result.get("high"), result.get("action")
    url_date = official_statement_url(result.get("url"))
    valid = (dt is not None and dt <= n and dt.date() <= n.astimezone(NY).date()
             and (not released or (released <= n and released.astimezone(NY).date() == dt.date()))
             and valid_number(low) and valid_number(high) and 0 <= low < high <= 25 and high - low <= 1
             and action in ("raise", "lower", "maintain", "keep")
             and url_date is not None and url_date[1] == dt.strftime("%Y%m%d")
             and result.get("published_date", dt.date().isoformat()) == dt.date().isoformat())
    if result.get("released_at") and released is None:
        valid = False
    due = moment(result.get("next_release"))
    pending = bool(result.get("fetch_error") or result.get("pending_update") or result.get("schedule_error")
                   or result.get("verification_pending") or (due and n >= due))
    if not valid:
        return {**result, "level": "neutral", "status": "unavailable", "incomplete": True,
                "text": "美联储正式利率决议待更新：未取得可核实的动作、目标区间、日期和官方来源。"}
    label = {"raise": "加息", "lower": "降息", "maintain": "维持", "keep": "维持"}[action]
    amount = result.get("change_bps")
    adjustment = f"{amount:g}个基点" if action in ("raise", "lower") and valid_number(amount) and 0 < amount <= 1000 else ""
    text = f"最近已核实正式决议（{dt.date().isoformat()}）：{label}{adjustment}，目标区间{low:g}%–{high:g}%。"
    if pending:
        text += "新决议信息待更新；保留上述旧决议日期，不代表新会议结果已核实。"
    return {**result, "level": "neutral", "status": "pending" if pending else "available",
            "incomplete": pending, "text": text}


def update_policy(prev=None, now=None):
    n = clock(now)
    prev = prev if isinstance(prev, dict) else {}
    cached = prev.get("fed")
    fields = ("date", "action", "low", "high", "change_bps", "published_date", "released_at",
              "next_release", "pending_release", "pending_update", "source", "url", "calendar_url",
              "checked_at", "fetch_error", "schedule_error", "verification_pending")
    old = {k: v for k, v in cached.items() if k in fields} if isinstance(cached, dict) else {}
    # 首次从旧CPI/SEP组合迁移，立即核实正式决议，不能继承旧解析失败的等待窗口。
    legacy = "cpi" in prev or (isinstance(cached, dict) and any(k in cached for k in ("median", "projection_url", "year")))
    if legacy:
        # 迁移失败后也保留需核实正文的标记，不能把旧组合字段误认成已核实缓存。
        old["verification_pending"] = True
    checked = None if legacy else moment(old.get("checked_at"))
    due = moment(old.get("next_release"))
    invalid = fed_decision(old, n)["status"] == "unavailable"
    old_date = moment(old.get("date"))
    schedule_valid = due is not None and old_date is not None and due > old_date
    if not invalid and not schedule_valid:
        old["schedule_error"] = "下次计划决议时间缺失或无效，待补查官方日历"
    limited = bool(old.get("fetch_error") or old.get("pending_update") or old.get("schedule_error")
                   or old.get("verification_pending") or invalid or not schedule_valid)
    fresh = checked is not None and checked <= n
    # 成功消息不按缓存年龄过期，未到公布时间只在本地比较，零网络请求。
    # 缺少有效日程需补查日历；错误/待更新必须先限速，到期不绕过一小时窗口。
    reuse = not legacy and ((fresh and limited and n - checked < timedelta(hours=1))
                            or (not limited and n < due))
    fed = old
    if not reuse:
        calendar = None
        try:
            calendar = parse_calendar(fetch_text(FED_URL), n)
            last, latest, nxt = calendar["latest_statement"], calendar["latest"], calendar["next"]
            if not last:
                raise ValueError("官方日历尚无可用正式声明链接")
            if not invalid and old_date and last["date"] < old_date.date().isoformat():
                raise ValueError("官方日历返回更旧决议，保留已核实缓存")
            same_statement = (not old.get("verification_pending") and not invalid and last["statement"] == old.get("url")
                              and last["date"] == old_date.date().isoformat())
            if same_statement:
                # 日历尚未挂新声明，或仅补全未来日程，不重新下载已核实正文。
                parsed = {k: old[k] for k in ("action", "low", "high", "change_bps", "published_date", "released_at") if k in old}
            else:
                parsed = parse_statement(fetch_text(last["statement"]), last["date"])
            if last["unscheduled"] and not parsed.get("released_at") and last["date"] == n.astimezone(NY).date().isoformat():
                raise ValueError("临时会议当日发布时间尚未确认")
            if parsed.get("released_at") and moment(parsed["released_at"]) > n:
                raise ValueError("正式声明发布时间尚未到达")
            pending_at = latest["at"] if latest["date"] > last["date"] else None
            # 日历暂时漏列/撤下到期会议也不能把旧消息重新标为已更新。
            for expected in (moment(old.get("pending_release")), due if schedule_valid else None):
                if expected and expected <= n and last["date"] < expected.astimezone(NY).date().isoformat():
                    if not pending_at or expected > moment(pending_at):
                        pending_at = expected.isoformat()
            pending = pending_at is not None
            fed = {**parsed, "date": old["date"] if same_statement else parsed.get("released_at", last["date"]),
                   "next_release": nxt["at"] if nxt else None,
                   "pending_release": pending_at, "pending_update": pending,
                   "source": "美联储官方正式利率决议声明", "url": last["statement"], "calendar_url": FED_URL,
                   "checked_at": n.isoformat(), "fetch_error": "",
                   "schedule_error": "" if nxt else "官方日历未提供下次计划决议时间，待补查日程"}
        except Exception as exc:
            fed = {**old, "checked_at": n.isoformat(), "fetch_error": str(exc)[:180] or type(exc).__name__,
                   "pending_update": True}
            if calendar:
                latest, nxt = calendar["latest"], calendar["next"]
                old_date = moment(old.get("date"))
                if not old_date or latest["date"] > old_date.date().isoformat():
                    fed["pending_release"] = latest["at"]
                fed["next_release"] = nxt["at"] if nxt else None
            # 即使日历/声明异常，也保存尚未兑现的到期会议，不能被未来日程冲掉。
            for expected in (moment(old.get("pending_release")), due if schedule_valid else None):
                if expected and expected <= n and (invalid or old_date.date() < expected.astimezone(NY).date()):
                    current = moment(fed.get("pending_release"))
                    if not current or expected > current:
                        fed["pending_release"] = expected.isoformat()
    rendered = fed_decision(fed, n)
    summary = {k: rendered[k] for k in ("level", "status", "text", "incomplete")}
    return {"fed": fed, "summary": summary, "rules": RULES}


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
        return {**result, "text": "同月合约未核实，未判定涨幅"}
    ds, cs = contract.get("dates") or [], contract.get("closes") or []
    if len(cs) < 2 or len(ds) != len(cs) or ds[-1] != target or ds != sorted(set(ds)) or any(not valid_number(v) or v <= 0 for v in cs) or abs(cs[-1]/value-1) > .005:
        return {**result, "text": "历史不足或口径不一致，未判定涨幅"}
    # 日涨跌、5日和20日全部使用同一合约；足够两根时仍可显示日变化。
    def change(period):
        return float((Decimal(str(cs[-1])) / Decimal(str(cs[-period-1])) - 1) * 100) if len(cs) > period else None
    day, week, month = change(1), change(5), change(20)
    incomplete = week is None or month is None
    if (week is not None and week >= 20) or (month is not None and month >= 30):
        lv, text = "red", "快速上涨，成本压力上升"
    elif (week is not None and week >= 10) or (month is not None and month >= 20):
        lv, text = "yellow", "涨幅偏快，留意成本"
    elif incomplete:
        lv, text = "gray", "历史不足，成本压力判断待更新"
    else:
        lv, text = "green", "未快速上涨"
    if incomplete and lv != "gray":
        text += "，判断不完整"
    return {**result, "level": lv, "text": text, "day": day, "day_from": ds[-2],
            "week": week, "month": month, "incomplete": incomplete, "contract_symbol": contract["quote_symbol"]}


def oil_contract_symbol(name):
    match = re.fullmatch(r"Crude Oil ([A-Za-z]{3}) (\d{2})", name or "")
    months = {n[:3]: "FGHJKMNQUVXZ"[m-1] for n, m in MONTHS.items()}
    return "CL" + months[match[1]] + match[2] + ".NYM" if match and match[1] in months else None
