"""美股常规交易时段的纯辅助函数；不取行情、不读时钟、不读写业务文件。"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


US_TZ = ZoneInfo("America/New_York")
QUOTE_MAX_DELAY = timedelta(minutes=5)


def eastern(when):
    if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("时点必须带时区")
    return when.astimezone(US_TZ)


def session_on_or_before(day):
    calendar = xcals.get_calendar("XNYS")
    return calendar.date_to_session(day, direction="previous").date().isoformat()


def previous_session(day):
    return xcals.get_calendar("XNYS").previous_session(day).date().isoformat()


def session_bounds(day):
    """返回该日真实开收盘 UTC 时点；休市返回 None，不回退成别的交易日。"""
    calendar = xcals.get_calendar("XNYS")
    if not calendar.is_session(day):
        return None
    return (calendar.session_open(day).to_pydatetime(),
            calendar.session_close(day).to_pydatetime())


def active_session(when):
    day = eastern(when).date().isoformat()
    bounds = session_bounds(day)
    if bounds and bounds[0] <= when < bounds[1]:
        return day, bounds[0], bounds[1]
    return None


def latest_completed_session(when, target="", grace=timedelta(minutes=15)):
    now = eastern(when)
    latest = session_on_or_before(now.date())
    if now < session_bounds(latest)[1] + grace:
        latest = previous_session(latest)
    if target and target < latest:
        latest = session_on_or_before(target)
    return latest


def preceding_sessions(day, count):
    """不含 day，按旧到新返回此前 count 个交易日。"""
    calendar = xcals.get_calendar("XNYS")
    sessions = []
    for _ in range(count):
        day = calendar.previous_session(day)
        sessions.append(day.date().isoformat())
    return sessions[::-1]


def quote_time(value):
    """接受带时区 ISO 或 Nasdaq 完整日期+钟点+ET/EST/EDT；不猜日期和时区。"""
    try:
        if isinstance(value, datetime):
            return eastern(value)
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        try:
            return eastern(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
        match = re.fullmatch(r"(.+?)\s+(ET|EST|EDT)", value, re.I)
        if not match:
            return None
        raw, zone = match.groups()
        raw = " ".join(raw.split())
        for date_format in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
            for clock_format in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
                try:
                    moment = datetime.strptime(raw, date_format + " " + clock_format).replace(tzinfo=US_TZ)
                except ValueError:
                    continue
                if zone.upper() != "ET" and moment.tzname() != zone.upper():
                    return None
                return moment
    except (ValueError, TypeError, OverflowError):
        return None
    return None


def validated_quote_time(value, when):
    """仅接受当前常规交易日、未超前且最多延迟五分钟的成交时间。"""
    try:
        session = active_session(when)
        quoted = quote_time(value)
        if (session and quoted is not None and quoted.date().isoformat() == session[0]
                and session[1] <= quoted < session[2]
                and timedelta(0) <= when - quoted <= QUOTE_MAX_DELAY):
            return quoted
    except (ValueError, TypeError, KeyError, OverflowError):
        pass
    return None
