# -*- coding: utf-8 -*-
"""指标计算，不联网或写文件；构造只保存运行依赖，context 仅兼容旧入口，不增加状态。"""
import math
import statistics
from datetime import timedelta

RS_BENCH_NAME = "标普500"


RSI_PERIODS = (6, 12, 24)


RS_DAYS = 20          # 板块相对标普500强弱的回看交易日数（约一个月）


RS_NEUTRAL_PP = 0.5   # 相对强弱绝对值不足该百分点，按「持平」显示（不着色）


MA_CONFIRM_LOOKBACK = 10   # 50 日线确认穿越：最近多少个交易日内收盘价曾在均线另一侧


MA_CONFIRM_MIN, MA_CONFIRM_MAX, MA_CONFIRM_DEFAULT = 0.5, 3.0, 1.0


MA_LONG_BAND_PCT = 1.0     # 第二条均线：当日日线高低范围触及本轮SMA±1%独立亮黄；穿越独立判断


# 涨跌 / 振幅阈值按个股自己的平时波动分档：设置页里的数字是「中档」，低档更灵敏、高档更迟钝。
VOL_LOOKBACK = 60          # 取最近多少个交易日算平时波动


VOL_MIN_RETURNS = 40       # 少于这么多个日收益（新上市）就按中档


VOL_LOW_SIGMA = 1.2        # 日收益标准差 < 此值 → 低档


VOL_HIGH_SIGMA = 2.5       # 日收益标准差 > 此值 → 高档


VOL_RATIO_BASE = 20    # 量比的均量窗口：最近完整日量 ÷ 此前 20 个完整交易日均量


VOL_Z_WINDOW = 60      # 量比 z 值的回看长度：这只股自己 60 个交易日的放量分布


INTRADAY_VOL_QUIET_MIN = 15   # 开盘后这段分钟数内不报盘中量比：分母太小会把比值放大到没有意义


def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 50.0 if ag == 0 else 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def relative_strength(etf, bench, days=RS_DAYS, bench_name=RS_BENCH_NAME):
    """板块 ETF 相对基准（默认标普500指数）的强弱：两者近 days 个交易日涨幅之差（单位：百分点）。

    按日期对齐而不是按下标：两边任何一端缺一天（或盘中补了实时价的那一根不同步），
    下标就会错位一天。最近 3 根里找双方共有的最新日期作终点，起点必须是 ETF 序列里
    终点往前数第 days 根、且基准也有该日；对不上就返回 None，页面不显示，绝不拿错位数据凑数。
    """
    try:
        ed, ec = list(etf.get("dates") or []), list(etf.get("closes") or [])
        sd, sc = list(bench.get("dates") or []), list(bench.get("closes") or [])
        if not ed or len(ed) != len(ec) or len(sd) != len(sc) or len(ec) <= days:
            return None
        bench_close = dict(zip(sd, sc))
        end_i = next((i for i in range(len(ed) - 1, max(len(ed) - 4, -1), -1) if ed[i] in bench_close), None)
        if end_i is None or end_i - days < 0 or ed[end_i - days] not in bench_close:
            return None
        start_i = end_i - days
        e0, e1 = float(ec[start_i]), float(ec[end_i])
        s0, s1 = float(bench_close[ed[start_i]]), float(bench_close[ed[end_i]])
        if min(e0, e1, s0, s1) <= 0 or not all(math.isfinite(x) for x in (e0, e1, s0, s1)):
            return None
        etf_ret, bench_ret = (e1 / e0 - 1) * 100, (s1 / s0 - 1) * 100
        return {"days": days, "etf": etf_ret, "bench": bench_ret, "bench_name": bench_name, "diff": etf_ret - bench_ret,
                "start": ed[start_i], "end": ed[end_i]}
    except (ValueError, TypeError, OverflowError, AttributeError):
        return None


def boll(closes, n=20, k=2.0):
    """布林带：返回 (中轨, 上轨, 下轨)，数据不足返回三个 None"""
    if len(closes) < n:
        return None, None, None
    win = closes[-n:]
    mid = sum(win) / n
    sd = (sum((x - mid) ** 2 for x in win) / n) ** 0.5
    return mid, mid + k * sd, mid - k * sd


def boll_streak(closes, n=20, k=2.0, near_pct=0.5, max_days=120, highs=None, lows=None):
    """连续贴近布林上轨 / 下轨的天数，从今天往回数。

    两条口径（2026-10-07 修正，此前与图上对不上就是这两点）：
    1. 窗口包含当天：win = closes[i-n+1 : i+1]，和 boll() 完全一致。
       修正前这里用 closes[i-n:i]（不含当天），等于拿「昨天的轨道」比「今天的价格」，
       和看板主信号、和图上画出来的轨道都差一天。
    2. 盘中触及即算：上轨用当日最高价、下轨用当日最低价，不用收盘价。
       用户要的规则是「盘中穿过就算」，收盘价会把「冲上去又回落」的那天漏掉。

    只要某天距轨道超过 near_pct 就中断计数 —— 「远离一天就不算连续，再贴近重新从第一日算」。

    返回 {"up": {"days": int, "cross": int}, "dn": {...}}
      days  连续贴合天数（距轨道 <= near_pct）
      cross 其中真正穿越轨道的天数（距轨道 <= 0）
    """
    out = {"up": {"days": 0, "cross": 0}, "dn": {"days": 0, "cross": 0}}
    if len(closes) < n:
        return out
    # 没有日内高低价时退回收盘价，不至于整只股失去这条信号
    hi = highs if (highs and len(highs) == len(closes)) else closes
    lo = lows if (lows and len(lows) == len(closes)) else closes
    for side in ("up", "dn"):
        days = 0
        cross = 0
        i = len(closes) - 1
        while i >= n - 1 and days < max_days:
            win = closes[i - n + 1:i + 1]  # 含第 i 天本身，与 boll() 同口径
            mid = sum(win) / n
            sd = (sum((x - mid) ** 2 for x in win) / n) ** 0.5
            up = mid + k * sd
            dn = mid - k * sd
            px = hi[i] if side == "up" else lo[i]
            gap = (up - px) / up * 100 if side == "up" else (px - dn) / dn * 100
            if gap > near_pct:             # 这一天远离了，连续性断掉
                break
            days += 1
            if gap <= 0:
                cross += 1
            i -= 1
        out[side] = {"days": days, "cross": cross}
    return out


def ma_confirmed_cross(closes, price, period, th_pct, lookback=MA_CONFIRM_LOOKBACK):
    """短均线「带宽确认」穿越：今天首次收到均线 ±th_pct 之外，昨天还没在带外，且最近 lookback 日内
    收盘价曾在均线另一侧。返回 "up" / "down" / None；数据不够返回 "n/a"（调用方退回旧规则）。
    比「昨收在一侧、今收在另一侧」多一道幅度门槛，价格贴着均线来回晃时不再每次都响。"""
    series = list(closes)
    if not series:
        return "n/a"
    series[-1] = price
    n = len(series)
    if n < period + lookback + 1:
        return "n/a"

    def ma_at(i):
        return sum(series[i - period + 1:i + 1]) / period

    t = n - 1
    th = th_pct / 100.0
    ma_t, ma_y = ma_at(t), ma_at(t - 1)
    if series[t] >= ma_t * (1 + th) and series[t - 1] < ma_y * (1 + th):
        if any(series[j] < ma_at(j) for j in range(t - lookback, t)):
            return "up"
    if series[t] <= ma_t * (1 - th) and series[t - 1] > ma_y * (1 - th):
        if any(series[j] > ma_at(j) for j in range(t - lookback, t)):
            return "down"
    return None


def _volume_ratio_distribution(volumes, index, window=VOL_Z_WINDOW, base=VOL_RATIO_BASE):
    """返回原量比 z 分布的 m/s/z；窗口及目标日参与统计的口径保持不变。"""
    start = index - window
    if start - base < 0:
        return None
    window_vols = volumes[start - base:index + 1]
    if len(window_vols) != window + base + 1:
        return None
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0
           for v in window_vols):
        return None
    logs = []
    for t in range(base, len(window_vols)):
        # 先除后加避免均量求和溢出；对数作差避免量比本身上溢或下溢。
        mean_prev = sum(v / base for v in window_vols[t - base:t])
        if not math.isfinite(mean_prev) or mean_prev <= 0:
            return None
        logs.append(math.log(window_vols[t]) - math.log(mean_prev))
    median = statistics.median(logs)
    scale = 1.4826 * statistics.median([abs(value - median) for value in logs])
    if not math.isfinite(median) or not math.isfinite(scale) or scale <= 0:
        return None
    return {"m": median, "s": scale, "z": (logs[-1] - median) / scale}


def _volume_ratio_zscore(volumes, index, window=VOL_Z_WINDOW, base=VOL_RATIO_BASE):
    """把 index 那天的量比换算成这只股自己的「放量 z 值」。

    样本：此前 window 个完整交易日各自的 ln(当日量 ÷ 此前 base 日均量)。
    尺度用中位数与 MAD（1.4826×MAD ≈ 标准差），单日天量不会把门槛整体抬走；
    平稳的票 1.3× 也可能是异常，常年巨量的票 2× 也未必异常，门槛随股而异。
    数据不足、量非正、分布尺度为 0（天天一模一样）时返回 None，调用方退回固定倍数。
    """
    distribution = _volume_ratio_distribution(volumes, index, window, base)
    return distribution["z"] if distribution is not None else None


class IndicatorCalculator:
    def __init__(self, context):
        self._context = context

    def ma_confirm_pct(self, vol_sigma):
        """短均线确认穿越所需的偏离幅度（%）：取这只股平时日波动的一半，限制在 0.5~3；新股没有波动数据按 1。"""
        if vol_sigma is None:
            return self._context.MA_CONFIRM_DEFAULT
        return min(self._context.MA_CONFIRM_MAX, max(self._context.MA_CONFIRM_MIN, 0.5 * vol_sigma))


    def vol_profile(self, closes):
        """返回 (档位, 平时日波动% 或 None)。不含最后一根（避免今天的异动把自己的门槛抬高）。"""
        series = [c for c in (closes or [])[:-1] if c]
        series = series[-(self._context.VOL_LOOKBACK + 1):]
        rets = [(b / a - 1) * 100 for a, b in zip(series, series[1:]) if a]
        if len(rets) < self._context.VOL_MIN_RETURNS:
            return "mid", None
        # 用中位数绝对偏差(MAD)估算，单日财报跳空 / 数据异常不会把整档拉高
        ordered = sorted(rets)
        mid = len(ordered) // 2
        med = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        devs = sorted(abs(r - med) for r in rets)
        dmid = len(devs) // 2
        mad = devs[dmid] if len(devs) % 2 else (devs[dmid - 1] + devs[dmid]) / 2
        sigma = 1.4826 * mad
        if sigma < self._context.VOL_LOW_SIGMA:
            return "low", sigma
        if sigma > self._context.VOL_HIGH_SIGMA:
            return "high", sigma
        return "mid", sigma


    def _latest_completed_session_index(self, dates, asof=None):
        """最近一个已结束的美股交易日在日线序列里的下标；不可用返回 None。

    盘中排除今天未完结的日线，收盘后留 15 分钟给日线汇总；历史锚点不向后读未来日线。
    """
        if not isinstance(dates, list) or not dates:
            return None
        try:
            expected = self._context.market_sessions.latest_completed_session(asof or self._context.NOW, self._context.TARGET_DATE)
            return dates.index(expected) if dates.count(expected) == 1 else None
        except (ValueError, TypeError, KeyError, OverflowError):
            return None


    def completed_day_volume_ratio(self, sym, data, asof=None):
        """最近一个已结束的美股交易日成交量 / 此前20个完整交易日均量。

    盘中排除今天未完结的日线，收盘后留15分钟给日线汇总；行情源缺量、
    落后于最近完整交易日或历史不足时不报放量。期货/指数没有同一股票日历口径。
    """
        if self._context.instrument_identity(sym).startswith('^') or self._context.instrument_identity(sym).endswith('=F'):
            return None, ''
        dates, volumes = data.get('dates') or [], data.get('volumes') or []
        if len(dates) != len(volumes) or len(dates) < self._context.VOL_RATIO_BASE + 1:
            return None, ''
        index = self._context._latest_completed_session_index(dates, asof)
        if index is None:
            return None, ''
        # 不跳过缺量日凑均值：分子与此前20根日线必须来自同一行情源且有效。
        sample = volumes[index-self._context.VOL_RATIO_BASE:index+1]
        if len(sample) != self._context.VOL_RATIO_BASE + 1 or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                                                    or not math.isfinite(v) or v <= 0 for v in sample):
            return None, ''
        average = sum(sample[:-1]) / self._context.VOL_RATIO_BASE
        return (sample[-1] / average, dates[index]) if average > 0 else (None, '')


    def completed_day_volume_z(self, sym, data, asof=None):
        """最近完整日量比的「自身波动 z 值」；算不出返回 None，由调用方退回固定倍数。"""
        if self._context.instrument_identity(sym).startswith('^') or self._context.instrument_identity(sym).endswith('=F'):
            return None
        dates, volumes = data.get('dates') or [], data.get('volumes') or []
        if not isinstance(dates, list) or not isinstance(volumes, list) or len(dates) != len(volumes):
            return None
        index = self._context._latest_completed_session_index(dates, asof)
        if index is None:
            return None
        try:
            return self._context._volume_ratio_zscore(volumes, index)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            return None


    def completed_day_volume_distribution(self, sym, data, asof=None):
        """最近完整日原 z 分布的 ln 量比中位数 m 与 1.4826×MAD 尺度 s；无效返回 None。"""
        identity = self._context.instrument_identity(sym)
        if identity.startswith('^') or identity.endswith('=F'):
            return None
        dates, volumes = data.get('dates') or [], data.get('volumes') or []
        if not isinstance(dates, list) or not isinstance(volumes, list) or len(dates) != len(volumes):
            return None
        index = self._context._latest_completed_session_index(dates, asof)
        if index is None:
            return None
        try:
            distribution = _volume_ratio_distribution(volumes, index)
            return {key: distribution[key] for key in ("m", "s")} if distribution is not None else None
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            return None


    def intraday_volume_ratio(self, sym, data, asof=None):
        """盘中量比 =（当日累计成交量 ÷ 已开盘分钟数）÷（此前20个完整交易日均量 ÷ 当日总交易分钟数）。

    用「每分钟成交速度」相比，时间进度天然被约掉，所以盘中不用等到收盘也能算。
    /info 的累计量实测实时递增且不含盘前；均量窗口沿用 completed_day_volume_ratio 的 20 日。
    期货/指数不走同一股票日历口径；静默期内、非交易时段、源没给量或历史不足时一律不报。
    rt_ts 必须通过时效校验，进度与返回标签均取成交时间；此前20个日期必须连续且完整。
    """
        if self._context.instrument_identity(sym).startswith('^') or self._context.instrument_identity(sym).endswith('=F'):
            return None, ''
        vol = data.get("rt_volume")
        if not isinstance(vol, (int, float)) or isinstance(vol, bool) or not math.isfinite(vol) or vol <= 0:
            return None, ''
        dates, volumes = data.get('dates') or [], data.get('volumes') or []
        if (not isinstance(dates, list) or not isinstance(volumes, list)
                or len(dates) != len(volumes) or len(dates) < self._context.VOL_RATIO_BASE + 1):
            return None, ''
        try:
            # 实时量以成交时点计进度；NOW 是任务启动时间，不能拿它误判抓取后的报价为未来。
            now = asof or self._context.datetime.now(self._context.US_TZ)
            session = self._context.market_sessions.active_session(now)
            quoted = self._context.market_sessions.validated_quote_time(data.get("rt_ts"), now)
            if (not session or quoted is None or dates[-1] != session[0]
                    or (self._context.TARGET_DATE and self._context.TARGET_DATE != session[0])):
                return None, ''
            if dates[-self._context.VOL_RATIO_BASE-1:-1] != self._context.market_sessions.preceding_sessions(session[0], self._context.VOL_RATIO_BASE):
                return None, ''
            elapsed = (quoted - session[1]).total_seconds() / 60
            total = (session[2] - session[1]).total_seconds() / 60
            if elapsed < self._context.INTRADAY_VOL_QUIET_MIN or total <= 0:
                return None, ''
            sample = volumes[-self._context.VOL_RATIO_BASE-1:-1]
            if any(not isinstance(v, (int, float)) or isinstance(v, bool)
                   or not math.isfinite(v) or v <= 0 for v in sample):
                return None, ''
            average = sum(sample) / self._context.VOL_RATIO_BASE
            if average <= 0:
                return None, ''
            return (vol / elapsed) / (average / total), quoted.strftime("%H:%M")
        except (ValueError, TypeError, KeyError, OverflowError):
            return None, ''


    def _range52_from_history(self, data):
        """仅用同一日线序列的真实高低价给价格位置条供数；缺失/错位不拿收盘价冒充极值。"""
        dates, highs, lows = (data.get(key) for key in ("dates", "highs", "lows"))
        validity = data.get("ma_range_valid") or None   # 空列表当「源没给有效位」，不当成整段无效
        price = data.get("price")
        valid_num = lambda value: (isinstance(value, (int, float)) and not isinstance(value, bool)
                                   and math.isfinite(value) and value > 0)
        if (not isinstance(dates, list) or len(dates) < 30 or any(not isinstance(day, str) for day in dates)
                or not isinstance(highs, list) or not isinstance(lows, list)
                or len(dates) != len(highs) or len(dates) != len(lows)
                or not valid_num(price)
                or (validity is not None and (not isinstance(validity, list)
                    or len(validity) != len(dates)))):
            return None
        try:
            end = self._context.datetime.fromisoformat(dates[-1]).date()
            cutoff = (end - timedelta(weeks=52)).isoformat()
            start = next((i for i, day in enumerate(dates) if day >= cutoff), len(dates))
            if (start >= len(dates) or (validity is not None and not all(v is True for v in validity[start:]))
                    or any(not valid_num(high) or not valid_num(low) or low > high
                           for high, low in zip(highs[start:], lows[start:]))):
                return None
            low, high = min(lows[start:]), max(highs[start:])
            if low >= high or not low <= price <= high:
                return None
            first = self._context.datetime.fromisoformat(dates[start]).date()
            scope = "52周" if (end - first).days >= 357 else "可用历史"
            return {"low": low, "high": high, "position": (price - low) / (high - low) * 100,
                    "scope": scope, "from": dates[start], "to": dates[-1]}
        except (ValueError, TypeError, OverflowError):
            return None
