"""公司官网财报事件解析；仅依赖标准库和 requests，不读取或写入缓存。

日期为美东日历日，接受当天及未来的明确财报事件（与主源同口径）。
日期型 today 必须已按美东口径提供；带时区的 datetime 会转换为美东。
会议钟点从不写入 timing。抓取预算是协作式截止时间，不是 DNS 的硬超时。
"""

import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests


SOURCES = {
    "COST": {
        "host": "investor.costco.com",
        "list_url": "https://investor.costco.com/events-and-presentations/default.aspx",
        "company": "Costco Wholesale Corporation",
        "event_paths": ("/events-and-presentations/event-details/",
                        "/events-and-presentations/events/event-details/"),
    },
    "MU": {
        "host": "investors.micron.com",
        "list_url": "https://investors.micron.com/events-and-presentations/default.aspx",
        "company": "Micron Technology, Inc.",
        "event_paths": (
            "/events-and-presentations/event-details/",
            "/events/event-details/",
        ),
    },
    "CIEN": {
        "host": "investor.ciena.com",
        "list_url": "https://investor.ciena.com/events-and-presentations/default.aspx",
        "company": "Ciena Corporation",
        "event_paths": (
            "/events-and-presentations/event-details/",
            "/events-and-presentations/events/event-details/",
        ),
    },
    "SPCX": {
        # 页面事件列表由脚本加载，在线解析多半读不到；已核验日期走 official-earnings.json 快照。
        "host": "ir.spacex.com",
        "list_url": "https://ir.spacex.com/events/default.aspx",
        "company": "Space Exploration Technologies Corp.",
        "event_paths": ("/events/event-details/",),
    },
    "CRDO": {
        "host": "investors.credosemi.com",
        "list_url": "https://investors.credosemi.com/news-events/events/default.aspx",
        "company": "Credo Technology Group Holding Ltd",
        "event_paths": ("/news-events/events/event-details/",),
    },
}

_BUDGET = 20.0
_MAX_BYTES = 2 * 1024 * 1024
_MAX_NODES = 40000
_MAX_DEPTH = 128
_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
_SKIP = {"script", "style", "noscript", "template", "nav", "footer", "aside"}
_EARNINGS = re.compile(
    r"\b(?:earnings|financial\s+results|financial\s+(?:conference\s+)?call|"
    r"results\s+(?:conference\s+)?call)\b", re.I
)
_EXCLUDED = re.compile(
    r"\b(?:annual\s+(?:(?:general|shareholders?|stockholders?)\s+)?meeting|"
    r"sales|investor\s+conference|investors\s+conference)\b", re.I
)
_DATE_CLASS = re.compile(r"(?:^|[\s_-])(?:date|datetime|time)(?:$|[\s_-])", re.I)
_TITLE_CLASS = re.compile(r"(?:^|[\s_-])(?:title|headline)(?:$|[\s_-])", re.I)
_EVENT_CLASS = re.compile(r"(?:^|[\s_-])(?:event|events|card|item)(?:$|[\s_-])", re.I)
_METADATA_CLASS = re.compile(r"(?:^|[\s_-])(?:published|updated|copyright|footer)(?:$|[\s_-])", re.I)
_GENERIC_HEADINGS = {"event details", "events", "events and presentations", "event information",
                     "event date", "date", "time", "webcast", "webcast details"}
_ISO = re.compile(
    r"(?<![\w])\d{4}-\d{2}-\d{2}"
    r"(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:[Zz]|[+-]\d{2}:?\d{2})?)?(?![\w:+-])"
)
_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember|t)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_ENGLISH = re.compile(r"\b(" + _MONTH + r")\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b", re.I)
_DAY_FIRST = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH + r")\s*,?\s*(\d{4})\b", re.I)
_NUMERIC = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4})(?![\d/])")
_CLOCK = re.compile(
    r"^\s*(?:at\s+|@\s*|,\s*)?(\d{1,2})(?::(\d{2}))?"
    r"\s*(am|pm)?\s*(ET|EST|EDT|PT|PST|PDT|UTC|GMT|Z|[+-]\d{2}:?\d{2})(?!\w)", re.I
)


def _words(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _is_earnings(value):
    value = _words(value)
    return bool(_EARNINGS.search(value)) and not _EXCLUDED.search(value)


def _allowed_url(value, symbol, base=None, detail_only=False):
    config = SOURCES.get(symbol)
    if not config or not isinstance(value, str):
        return None
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(urljoin(base, value) if base else value)
        if parts.scheme != "https" or parts.netloc.lower() != config["host"].lower():
            return None
        path = parts.path
        if parts.query or "%" in path or any(p in {".", ".."} for p in path.split("/")):
            return None
        is_list = path == urlsplit(config["list_url"]).path
        prefixes = config.get("event_paths", ())
        is_detail = any(path.startswith(prefix) and len(path) > len(prefix) for prefix in prefixes)
        if not is_detail and (detail_only or not is_list):
            return None
        return urlunsplit(("https", config["host"].lower(), path, "", ""))
    except (ValueError, TypeError):
        return None


def _eastern_date(value):
    """与监测器一致，使用 IANA 美东时区而非自行维护夏令时规则。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("缺少时区")
    return value.astimezone(ZoneInfo("America/New_York")).date()


def _today(value):
    if isinstance(value, datetime):
        return _eastern_date(value)
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    raise ValueError("today 必须为美东日期或带时区 datetime")


def _result(status, checked, url="", title="", event_date=None, reason=""):
    result = {
        "status": status,
        "date": event_date.isoformat() if event_date else "",
        "kind": "official",
        "timing": "",
        "source_url": url,
        "title": title,
        "source": "公司官方投资者关系",
        "official_checked": checked.isoformat() if checked else "",
    }
    if reason:
        result["reason"] = reason
    return result


def _clock_date(day, tail):
    match = _CLOCK.match(tail)
    if not match:
        return day
    hour, minute, meridiem, zone = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("无效钟点")
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    moment = datetime(day.year, day.month, day.day, hour, minute)
    zone = zone.upper()
    if zone == "ET":
        return day
    if zone == "PT":
        return (moment + timedelta(hours=3)).date()
    offsets = {"EST": -5, "EDT": -4, "PST": -8, "PDT": -7, "UTC": 0, "GMT": 0, "Z": 0}
    if zone in offsets:
        tz = timezone(timedelta(hours=offsets[zone]))
    else:
        digits = zone[1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:])
        if hours > 23 or minutes > 59:
            raise ValueError("无效时区")
        offset = timedelta(hours=hours, minutes=minutes)
        tz = timezone(offset if zone[0] == "+" else -offset)
    return _eastern_date(moment.replace(tzinfo=tz))


def _dates(value):
    """仅提取完整日期；无时区 ISO 钟点不推定为美东时间。"""
    if not isinstance(value, str):
        return set(), True
    found, spans, bad = set(), [], False
    for match in _ISO.finditer(value):
        spans.append(match.span())
        raw = match.group()
        try:
            if len(raw) == 10:
                day = _clock_date(date.fromisoformat(raw), value[match.end():])
            else:
                moment = datetime.fromisoformat(raw.upper().replace("Z", "+00:00"))
                day = _eastern_date(moment)
            found.add(day)
        except (ValueError, OverflowError):
            bad = True
    for pattern in (_ENGLISH, _DAY_FIRST, _NUMERIC):
        for match in pattern.finditer(value):
            if any(match.start() < end and match.end() > start for start, end in spans):
                continue
            spans.append(match.span())
            a, b, year = match.groups()
            try:
                if pattern is _ENGLISH:
                    month, day = _MONTHS[a[:3].lower()], int(b)
                elif pattern is _DAY_FIRST:
                    month, day = _MONTHS[b[:3].lower()], int(a)
                else:
                    month, day = int(a), int(b)
                found.add(_clock_date(date(int(year), month, day), value[match.end():]))
            except (ValueError, OverflowError):
                bad = True
    return found, bad


class _Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > _MAX_NODES or len(self.stack) > _MAX_DEPTH:
            raise ValueError("网页结构超过解析限额")
        node = _Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _hidden(node):
    labels = (node.attrs.get("class") or "") + " " + (node.attrs.get("id") or "")
    return (node.tag in _SKIP or "hidden" in node.attrs
            or node.attrs.get("itemprop") in {"datePublished", "dateModified", "endDate"}
            or bool(_METADATA_CLASS.search(labels))
            or str(node.attrs.get("aria-hidden", "")).lower() == "true")


def _walk(node, visible=True):
    if visible and _hidden(node):
        return
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child, visible)


def _text(node):
    if _hidden(node):
        return ""
    parts = []
    for child in node.children:
        parts.append(_text(child) if isinstance(child, _Node) else child)
    return " ".join(" ".join(parts).split())


def _date_node(node):
    labels = (node.attrs.get("class") or "") + " " + (node.attrs.get("id") or "")
    return node.tag == "time" or node.attrs.get("itemprop") == "startDate" or bool(_DATE_CLASS.search(labels))


def _title_node(node, symbol, url):
    labels = (node.attrs.get("class") or "") + " " + (node.attrs.get("id") or "")
    if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or _TITLE_CLASS.search(labels):
        title = _text(node)
        return bool(title) and _words(title) not in _GENERIC_HEADINGS
    if node.tag != "a":
        return False
    target = _allowed_url(node.attrs.get("href"), symbol, url, True)
    generic_link = _words(_text(node)) in {
        "earnings", "financial results", "financial call", "webcast", "view webcast",
        "details", "view details", "learn more",
    }
    return bool(target) and target != url and not generic_link


def _same_title(left, right):
    left, right = _words(left), _words(right)
    return left == right or (min(len(left.split()), len(right.split())) >= 3
                             and (" " + left + " " in " " + right + " "
                                  or " " + right + " " in " " + left + " "))


def _standalone_dates(value):
    # 无日期标记时，只接受独立日期行，不把正文里的财季截止日当作事件日期。
    value = re.sub(r"^(?:(?:event|start)\s+)?date\s*:?\s*", "", value, flags=re.I)
    value = re.sub(r"^(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\.?\s*,?\s*", "", value, flags=re.I)
    match = next((pattern.match(value) for pattern in (_ISO, _ENGLISH, _DAY_FIRST, _NUMERIC)
                  if pattern.match(value)), None)
    if not match:
        return set(), False
    tail = value[match.end():]
    clock = _CLOCK.match(tail)
    if clock:
        tail = tail[clock.end():]
    if tail.strip(" .,"):
        return set(), False
    return _dates(value)


def _scope_dates(scope):
    found, bad = set(), False
    for node in _walk(scope):
        if _date_node(node):
            for attribute in ("datetime", "content"):
                value = node.attrs.get(attribute)
                if value:
                    days, invalid = _dates(value)
                    found.update(days)
                    bad |= invalid or not days
            days, invalid = _dates(_text(node))
        else:
            days, invalid = _standalone_dates(_text(node))
            # 支持 h1 后直接跟一行日期，但不把标题、正文与日期拼接后扫描。
            direct = " ".join(child for child in node.children if isinstance(child, str)).strip()
            direct_days, direct_bad = _standalone_dates(direct)
            days.update(direct_days)
            invalid |= direct_bad
        found.update(days)
        bad |= invalid
    return found, bad


def _separate_event(scope, heading):
    ancestors = set()
    node = heading
    while node is not None:
        ancestors.add(node)
        node = node.parent
    for node in _walk(scope):
        labels = (node.attrs.get("class") or "") + " " + (node.attrs.get("id") or "")
        if node not in ancestors and not _date_node(node) and (
            node.tag in {"section", "article"} or _EVENT_CLASS.search(labels)
        ):
            if any(_date_node(child) for child in _walk(node)):
                return True
    return False


def _html_records(root, symbol, url):
    records = []
    for heading in _walk(root):
        if heading.tag not in {"h1", "h2"}:
            continue
        title = _text(heading)
        if not _is_earnings(title):
            continue
        if len(records) >= 256:
            raise ValueError("活动数量超过解析限额")
        scope = heading.parent
        days, bad = set(), False
        while scope is not None:
            # 上溯只能到同一活动容器，不能借用相邻活动、侧栏或另一标题的日期。
            others = any(_title_node(node, symbol, url) and not _same_title(_text(node), title)
                         for node in _walk(scope))
            if others or _separate_event(scope, heading):
                break
            days, bad = _scope_dates(scope)
            if days or bad:
                break
            scope = scope.parent
        records.append((title, days, bad, url))
    return records


def _json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)


def _json_records(root, symbol, url):
    records = []
    malformed = False
    for node in _walk(root, visible=False):
        if node.tag != "script" or (node.attrs.get("type") or "").lower().split(";")[0].strip() != "application/ld+json":
            continue
        raw = "".join(child for child in node.children if isinstance(child, str))
        try:
            objects = list(_json_objects(json.loads(raw)))
        except (ValueError, RecursionError):
            malformed = True
            continue
        for obj in objects:
            types = obj.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if not isinstance(types, list) or not any(
                value in {"Event", "https://schema.org/Event", "http://schema.org/Event"}
                for value in types if isinstance(value, str)
            ):
                continue
            if any(value.rstrip("/").rsplit("/", 1)[-1] in {"Organization", "WebPage"}
                   for value in types if isinstance(value, str)):
                continue
            title = obj.get("name")
            if not isinstance(title, str) or not _is_earnings(title):
                continue
            event_url = obj.get("url")
            if isinstance(event_url, dict):
                event_url = event_url.get("@id")
            if event_url is not None:
                event_url = _allowed_url(event_url, symbol, url)
                if not event_url:
                    continue
            state = obj.get("eventStatus", "")
            cancelled = isinstance(state, str) and state.rstrip("/").rsplit("/", 1)[-1] in {
                "EventCancelled", "EventPostponed", "EventRescheduled"
            }
            raw_date = obj.get("startDate")
            days, bad = _dates(raw_date)
            records.append((title.strip(), days, bad or cancelled or not days, event_url or url))
    return records, malformed


def _resolve(records, checked, url, malformed=False):
    if not records:
        return _result("unknown", checked, url, reason="no_clear_earnings_event")
    if len(records) > 256:
        return _result("unknown", checked, url, reason="too_many_events")
    groups = []
    for title, days, bad, source_url in records:
        group = next((group for group in groups if _same_title(title, group[0])), None)
        if group is None:
            group = [title, set(), False, source_url]
            groups.append(group)
        group[1].update(days)
        group[2] |= bad
    # 单页同一事件出现冲突日期时，不静默挑选一个日期或另一项活动。
    if malformed or any(bad or len(days) > 1 for _, days, bad, _ in groups):
        return _result("unknown", checked, url, reason="conflicting_or_ambiguous_event")
    future = [(next(iter(days)), title, source_url) for title, days, _, source_url in groups
              if len(days) == 1 and next(iter(days)) >= checked]
    if not future:
        return _result("unknown", checked, url, reason="no_confirmed_future_event")
    day, title, source_url = min(future)
    return _result("ok", checked, source_url, title, day)


def _parse_page(text, url, checked, symbol, records_out=None):
    try:
        valid_text = isinstance(text, str) and len(text.encode("utf-8")) <= _MAX_BYTES
    except UnicodeError:
        valid_text = False
    if not valid_text:
        return _result("unknown", checked, url, reason="invalid_or_oversized_page"), None
    parser = _Tree()
    try:
        parser.feed(text)
        parser.close()
        html_records = _html_records(parser.root, symbol, url)
        json_records, malformed = _json_records(parser.root, symbol, url)
    except (ValueError, RecursionError):
        return _result("unknown", checked, url, reason="unrecognized_page_structure"), None
    records = html_records + json_records
    is_list = url == _allowed_url(SOURCES[symbol]["list_url"], symbol)
    if not is_list:
        # 详情页不能把推荐活动或另一个 JSON-LD Event 误认作本页活动。
        headings = [_text(node) for node in _walk(parser.root)
                    if node.tag in {"h1", "h2"} and _text(node)]
        titles = [title for title in headings if _is_earnings(title)]
        primary = [title for title in headings if _words(title) not in _GENERIC_HEADINGS]
        if primary and not _is_earnings(primary[0]):
            return _result("unknown", checked, url, reason="unrecognized_detail_title"), parser.root
        records = [record for record in records if record[3] == url]
        if titles:
            records = [record for record in records if any(_same_title(record[0], title) for title in titles)]
        keys = []
        for record in records:
            if not any(_same_title(record[0], key) for key in keys):
                keys.append(record[0])
        if len(keys) > 1:
            return _result("unknown", checked, url, reason="multiple_events_on_detail_page"), parser.root
    if records_out is not None:
        records_out.extend(records)
    return _resolve(records, checked, url, malformed), parser.root


def parse_official_event(text, url, today, symbol):
    """纯解析 API；不发请求、不读写磁盘。失败返回 unknown，绝不从 URL 猜日期。"""
    symbol = str(symbol).upper()
    try:
        checked = _today(today)
    except (ValueError, OverflowError):
        return _result("unknown", None, reason="invalid_today")
    safe_url = _allowed_url(url, symbol)
    if not safe_url:
        return _result("unknown", checked, reason="unsupported_source_url")
    result, _ = _parse_page(text, safe_url, checked, symbol)
    return result


def _detail_links(root, url, symbol):
    links = []
    if root is None:
        return links
    for node in _walk(root):
        if node.tag != "a":
            continue
        target = _allowed_url(node.attrs.get("href"), symbol, url, True)
        if not target or target in links:
            continue
        title = " ".join((_text(node), node.attrs.get("title") or "", node.attrs.get("aria-label") or ""))
        if _is_earnings(title + " " + unquote(urlsplit(target).path)):
            links.append(target)
            if len(links) == 3:
                break
    return links


class _FetchError(Exception):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _download(session, url, timeout, deadline, clock=None):
    clock = clock or time.monotonic
    remaining = deadline - clock()
    if remaining <= 0:
        raise _FetchError("budget_exhausted")
    # trust_env=False 禁止读取 netrc/环境代理；每次请求前清空服务端设置的 Cookie。
    session.cookies.clear()
    phase_timeout = min(timeout, remaining / 2)
    try:
        with session.get(url, timeout=(phase_timeout, phase_timeout),
                         allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise _FetchError("http_" + str(response.status_code), response.status_code)
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type and media_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise _FetchError("unsupported_content_type")
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                if clock() >= deadline:
                    raise _FetchError("budget_exhausted")
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise _FetchError("page_too_large")
                chunks.append(chunk)
            if clock() >= deadline:
                raise _FetchError("budget_exhausted")
            return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException as exc:
        raise _FetchError("request_" + type(exc).__name__) from exc
    except LookupError as exc:
        raise _FetchError("invalid_response_encoding") from exc


def fetch_official_earnings(symbol, today, timeout=8, deadline=None, clock=None):
    """列表一次、至多前三个相关详情；禁跳转/重试/认证，约 20 秒总预算。

    ok 为已收集候选中最近的当天或未来财报事件；unknown 为不覆盖/无日期/冲突/结构不明；
    error 为 HTTP、网络或限额失败。403/429 当即停止，不换入口绕过限制。
    """
    symbol = str(symbol).upper()
    try:
        checked = _today(today)
    except (ValueError, OverflowError):
        return _result("unknown", None, reason="invalid_today")
    if symbol not in SOURCES:
        return _result("unknown", checked, reason="unsupported_symbol")
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("无效超时")
    except (TypeError, ValueError, OverflowError):
        return _result("error", checked, reason="invalid_timeout")
    url = _allowed_url(SOURCES[symbol]["list_url"], symbol)
    if not url:
        return _result("unknown", checked, reason="invalid_source_configuration")
    download_clock = (clock,) if clock is not None else ()
    clock = clock or time.monotonic
    deadline = min(clock() + _BUDGET, deadline if deadline is not None else float("inf"))
    if clock() >= deadline:
        return _result("error", checked, url, reason="budget_exhausted")
    with requests.Session() as session:
        session.trust_env = False
        session.auth = None
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        try:
            text = _download(session, url, timeout, deadline, *download_clock)
        except _FetchError as exc:
            return _result("error", checked, url, reason=exc.reason)
        records = []
        result, root = _parse_page(text, url, checked, symbol, records)
        if result.get("reason") == "conflicting_or_ambiguous_event":
            return result
        last_error = None
        for detail in _detail_links(root, url, symbol):
            if clock() >= deadline:
                return _result("error", checked, url, reason="budget_exhausted")
            try:
                text = _download(session, detail, timeout, deadline, *download_clock)
            except _FetchError as exc:
                last_error = _result("error", checked, detail, reason=exc.reason)
                if exc.status in {403, 429} or exc.reason == "budget_exhausted":
                    return last_error
                continue
            candidate, _ = _parse_page(text, detail, checked, symbol, records)
            if candidate.get("reason") == "conflicting_or_ambiguous_event":
                return candidate
            # 跨详情页也按同一事件归组：过去/未来冲突不能靠筛掉旧日期掩盖。
            selected = _resolve(records, checked, url)
            if selected.get("reason") in {"conflicting_or_ambiguous_event", "too_many_events"}:
                return selected
            result = candidate
        selected = _resolve(records, checked, url)
        if selected["status"] == "ok" or selected.get("reason") in {"conflicting_or_ambiguous_event", "too_many_events"}:
            return selected
        return last_error or result
