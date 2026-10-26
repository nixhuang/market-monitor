# -*- coding: utf-8 -*-
"""规则计算，不联网或写文件；构造只保存运行依赖，context 仅兼容旧入口，不增加状态。"""
import math
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

TARGET_SPACE_PCT = 40


TARGET_HIGH_SPACE_PCT = 90


CONSENSUS_GROUPS = ("position", "focus")   # 仅达到规则限这两组；空间规则覆盖全部个股


AMP_GROUPS = ("position", "focus")   # 振幅红黄仅持仓/重点，其他有效振幅计灰；涨跌幅对所有分组生效


BOLL_UP_SIGNAL_GROUPS = ("position", "focus")  # 上轨普通过热仅持仓/重点；三超卖红覆盖全组，持仓不分长短。


# 随机游走下，20 日窗口内「收盘价 − MA20」的标准差 ≈ 1.29 × 日收益率标准差。
# 用它把 vol_profile 估出的日波动换算成布林带宽（带宽 = 上下各 2σ ≈ 5.16 × 日σ），
# 逼近门槛就随个股波动自动缩放。刻意不用「当日带宽」：财报、暴跌会让带宽暴涨，
# 门槛跟着变宽等于越危险越迟钝；vol_profile 用的是 60 日中位数绝对偏差，抗异常值。
_BOLL_SIGMA_FACTOR = 1.29


# 方向符号只表示「位置/状态」，不预测后续涨跌：
# ▲ 布林上轨、▼ 布林下轨（绿/红）；↑ RSI 超买、↓ RSI 超卖（青/品红）。
# 布林写轨道上下，RSI 写超买超卖，两者不再混写（不允许出现「RSI 下轨」）。
_ARROW_BAND_UP, _ARROW_BAND_DOWN = "▲", "▼"


_ARROW_RSI_UP, _ARROW_RSI_DOWN = "↑", "↓"


def streak_tail(st, rail="上轨"):
    """把连续天数拼成信号尾巴。连续 1 日不啰嗦，>=2 日才标。"""
    if not st or st["days"] < 2:
        return ""
    # 撞上 max_days 上限说明「从有数据起从没中断过」（货币基金之类），
    # 写「连续第120日」没信息量，改成「≥」如实表达
    n = f"≥{st['days']}" if st["days"] >= 120 else str(st["days"])
    t = f" · 连续第{n}日贴近{rail}"
    if st["cross"]:
        t += f"（其中{st['cross']}日穿越）"
    return t


def dca_text(d):
    if not d:
        return ""
    # 页面只留一句：还有几个交易日（日期）；定投日当天整行变黄（样式 .dca.on）
    if d.get("pending"):
        return f"尚未开始（首个定投日 {d['first']}）"
    if d["due"]:
        return f"今天是定投日（{d['date']}，第 {d['times']} 次）"
    return f"还有 {d['next_in']} 个交易日（{d['next_date']}）"


def _level_num(v):
    """加仓价／减仓价是否可显示：必须是大于 0 的有限数字（布尔、字符串都不算）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _pos_num(v):
    """大于 0 的有限数字（布尔、字符串都不算）→ float；否则 None。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    return float(v)


def _month_of(text):
    """'2026-10-07' / '2026-10' → 10；其它 → None。"""
    m = re.match(r"\d{4}-(0[1-9]|1[0-2])(?!\d)", text) if isinstance(text, str) else None
    return int(m.group(1)) if m else None


def _price_text(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def target_label(t):
    return "目标价" if t.get("kind") == "manual" else "共识价"


def _consensus_description(text, target):
    """只识别自动共识描述；手填目标及原技术文字不按关键字误删。"""
    return (isinstance(target, dict) and target.get("kind") == "consensus" and isinstance(text, str)
            and text.startswith(("共识价", "旧共识价", "共识获取失败",
                                 "今日上穿共识价", "今日跌破共识价", "在共识价")))


def _rule_chg_red(al, x):
    if x.chg is not None and abs(x.chg) >= x.chg_red:
        al.add("chg", f"异动 {x.chg:+.1f}%{x.usual}", "red")


def _rule_chg_yellow(al, x):
    if x.chg is not None and x.chg_yellow <= abs(x.chg) < x.chg_red:
        al.add("chg", f"波动 {x.chg:+.1f}%{x.usual}", "yellow")


def _intraday_extremes(x):
    """盘中触及即算：上轨比当日最高价，下轨比当日最低价；盘中重跑时当前价可能比已有极值更极端。"""
    day_high = (x.data.get("highs") or [None])[-1]
    day_low = (x.data.get("lows") or [None])[-1]
    x.touch_up = x.price if day_high is None else max(day_high, x.price)
    x.touch_dn = x.price if day_low is None else min(day_low, x.price)


def _touch_note(price, touch):
    """触及价和展示的现价不一致时，把触及价写出来，
    否则会出现「现价 54.00 却写着突破上轨 59.15」的观感矛盾：其实是当天盘中最高冲到 60.68 穿过去了，收盘又跌回来。"""
    if touch is None or abs(touch - price) < 1e-9:
        return ""
    return f"（盘中触及 {touch:,.2f}）"


def _rule_amplitude(al, x):
    """日内振幅：涨跌幅只看「收盘 vs 昨收」，盘中冲高又回落、最后收平的票不会响，这条专门抓它。
    用当日最高/最低（盘中重跑时再和当前价取极值）除以昨收。"""
    x.amp = None
    # 其他组的灰灯必须来自真实有效日内范围；布林现价回退不能冒充0%振幅。
    # 复用已对齐收盘序列/有效位的范围校验，不改变持仓/重点原红黄口径。
    if not x.amp_on and hasattr(x, "data") and not _ma_day_range(x)[2]:
        return
    if (not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v > 0 for v in (x.touch_up, x.touch_dn, x.prev))
            or x.touch_dn > x.touch_up):
        return
    x.amp = (x.touch_up - x.touch_dn) / x.prev * 100
    if not math.isfinite(x.amp) or x.amp < 0:
        return
    text = f"振幅 {x.amp:.1f}%（{x.touch_dn:,.2f}~{x.touch_up:,.2f}）"
    if not x.amp_on:
        al.add("amp", text, "gray")
        x.foldable = False
        return
    if x.amp >= x.amp_red:
        al.add("amp", text, "red")
    elif x.amp >= x.amp_yellow:
        al.add("amp", text, "yellow")


def _ma_day_range(x):
    """只用与本轮收盘序列对齐的最后一根高低价；缺失/损坏退回现价，不冒充完整日内范围。"""
    data = getattr(x, "data", {})
    highs, lows = data.get("highs"), data.get("lows")
    validity = data.get("ma_range_valid") or None   # 空列表当「源没给有效位」，与缺失同处理
    if validity is not None and (not isinstance(validity, (list, tuple))
                                 or len(validity) != len(x.closes) or not validity or validity[-1] is not True):
        return x.price, x.price, False
    if (isinstance(highs, (list, tuple)) and isinstance(lows, (list, tuple))
            and len(highs) == len(lows) == len(x.closes) and highs and lows):
        high, low = highs[-1], lows[-1]
        if (all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v > 0 for v in (high, low)) and low <= high):
            return min(low, x.price), max(high, x.price), True
    return x.price, x.price, False


def _parse_boll_rsi_direction(direction):
    """把组合条件的数据字段拆成 (轨道, 超买/超卖, 尾注)；旧快照格式同样能读。"""
    if not isinstance(direction, str):
        return "", "", ""
    match = re.match(r"^(上轨|下轨)（(.*)）$", direction.strip())
    if not match:
        return "", "", ""
    side, inner = match.group(1), match.group(2)
    tail = "方向不一致" if "不一致" in inner else "双向命中" if "均命中" in inner else ""
    tag_match = re.search(r"(超买|超卖)", inner)
    return side, (tag_match.group(1) if tag_match else ""), tail


class RuleEngine:
    def __init__(self, context):
        self._context = context

    def global_dca(self, target_date):
        """全局定投提醒：按交易所日历数「起点日 → 数据日」经过了几个交易日。

    返回 None（没开启）或 dict：
      every, start, count(已过交易日数), times(第几次定投), due(数据日是否定投日),
      next_in(距下次还差几个交易日), next_date(下一个定投日，YYYY-MM-DD)
    """
        every, start = int(self._context.S.get("dca_every") or 0), (self._context.S.get("dca_start") or "").strip()
        if every <= 0 or not start or not target_date:
            return None
        try:
            import exchange_calendars as xcals
            cal = xcals.get_calendar("XNYS")
            first = cal.date_to_session(start, direction="next")
            last = cal.date_to_session(target_date, direction="previous")
            if first > last:
                # 尚未开始：距首个定投日还差几个交易日（last 本身是交易日，不计入）
                return {"every": every, "start": start, "pending": True,
                        "first": first.date().isoformat(),
                        "next_in": len(cal.sessions_in_range(last, first)) - 1}
            count = len(cal.sessions_in_range(first, last))
            rem = (-(count - 1)) % every
            upcoming = cal.sessions_window(last, rem + 1)
            next_date = upcoming[-1].date().isoformat()
            return {"every": every, "start": start, "count": count, "date": last.date().isoformat(),
                    "times": (count - 1) // every + 1, "due": rem == 0,
                    "next_in": rem, "next_date": next_date}
        except Exception as e:
            self._context.log(f"  ! 定投计算失败：{e}")
            return None


    # ---------------------------------------------------------------- 筛选

    def reduce_rule(self, cfg):
        """读持仓条目的减仓价，返回价格；没填或写错一律返回 None。

    reduce 必须是大于 0 的有限数字（布尔、字符串都不算），写错就忽略并记一条日志。"""
        v = cfg.get("reduce")
        if v is None or v == "":
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            self._context.log(f"  ! 减仓价无效，已忽略：{v!r}")
            return None
        return float(v)


    def cost_rule(self, cfg):
        """读成本价，返回价格；没填或写错一律返回 None。

    cost 必须是大于 0 的有限数字（布尔、字符串都不算），写错就忽略并记一条日志。
    成本价只在 52 周位置条上画一条黄色竖线（在区间外则贴端点并加朝外箭头），
    不参与任何灯级与统计计数。"""
        v = cfg.get("cost")
        if v is None or v == "":
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            self._context.log(f"  ! 成本价无效，已忽略：{v!r}")
            return None
        return float(v)


    def manual_target(self, entry, quiet=False):
        """设置页手填的目标价 → (价格, 月份 或 None)；没填或写错返回 None（写错会记一条日志）。"""
        if not isinstance(entry, dict):
            return None
        v = entry.get("target")
        price = self._context._pos_num(v)
        if price is None:
            if v not in (None, "") and not quiet:
                self._context.log(f"  ! 目标价无效，已忽略：{v!r}")
            return None
        return price, self._context._month_of(entry.get("target_at"))


    def resolve_target(self, cfg, cons):
        """手填优先于共识价。返回 {"kind","price","month","failed"}；都没有返回 None。
    共识价获取失败但有旧值：price 是旧值、failed=True（只显示标签，不参与警示）；没有旧值：price=None。"""
        m = self._context.manual_target(cfg)
        if m:
            return {"kind": "manual", "price": m[0], "month": m[1], "failed": False}
        if not isinstance(cons, dict) or cons.get("s") not in ("ok", "fail"):
            return None
        month = self._context._month_of(cons.get("upd")) or self._context._month_of(cons.get("r"))
        if cons["s"] == "ok":
            price = self._context._pos_num(cons.get("avg"))
            return {"kind": "consensus", "price": price, "month": month, "failed": False} if price else None
        return {"kind": "consensus", "price": self._context._pos_num(cons.get("avg")), "month": month, "failed": True}


    def target_alerts(self, price, prev, target, label, gap_pct):
        """仅计算达到，返回具体上穿/跌破/区间文字；空间不参与达到条件。"""
        if self._context._pos_num(price) is None or self._context._pos_num(target) is None:
            return []
        tstr = self._context._price_text(target)
        if self._context._pos_num(prev) is not None and prev < target <= price:
            return [f"今日上穿{label} {tstr}"]
        if self._context._pos_num(prev) is not None and prev > target >= price:
            return [f"今日跌破{label} {tstr}"]
        p, t = Decimal(str(price)), Decimal(str(target))
        dev = (p / t - 1) * 100
        if abs(p - t) * 100 <= Decimal(str(gap_pct)) * t:
            return [f"在{label} {tstr} ±{gap_pct:g}% 内（偏离 {dev:+.1f}%）"]
        return []


    def evaluate_target(self, price, prev, target, group, gap_pct):
        """返回新目标字典（或 None）；调用方先确认个股身份，不修改输入。"""
        if not target:
            return None
        t = {key: value for key, value in target.items() if key != "anomaly"}
        t.update(space=None, hit=False, space_hit=False, band=None,
                 space_detail="", reached_detail="")
        if t.get("failed") or self._context._pos_num(t.get("price")) is None or self._context._pos_num(price) is None:
            t["failed"] = True
            return t
        p, tp = Decimal(str(price)), Decimal(str(t["price"]))
        space = float((tp / p - 1) * 100)
        # 十进制交叉相乘判边界，避免 1.3/1.7 的二进制浮点误差。
        band = ("high" if tp * 100 >= p * (100 + self._context.TARGET_HIGH_SPACE_PCT)
                else "medium" if tp * 100 >= p * (100 + self._context.TARGET_SPACE_PCT) else "low")
        for boundary in (self._context.TARGET_SPACE_PCT, self._context.TARGET_HIGH_SPACE_PCT):
            if tp * 100 == p * (100 + boundary):
                space = float(boundary)
        reached = (self._context.target_alerts(price, prev, t["price"], self._context.target_label(t), gap_pct)
                   if group in self._context.CONSENSUS_GROUPS else [])
        space_hit = band in ("medium", "high")
        detail = (f"{self._context.target_label(t)} {self._context._price_text(t['price'])}，"
                  f"空间 {space:+.1f}%") if space_hit else ""
        t.update(failed=False, space=space, hit=bool(reached), space_hit=space_hit, band=band,
                 space_detail=detail, reached_detail=" / ".join(reached))
        return t


    def target_display_state(self, t):
        """纯函数：failed/reached/high/space/low；达到优先，缺行情不推断状态。"""
        if not isinstance(t, dict) or t.get("failed") or self._context._pos_num(t.get("price")) is None:
            return "failed"
        space = t.get("space")
        if isinstance(space, bool) or not isinstance(space, (int, float)) or not math.isfinite(space):
            return "failed"
        if t.get("hit"):
            return "reached"
        if t.get("band") == "high" and t.get("space_hit"):
            return "high"
        if t.get("band") == "medium" and t.get("space_hit"):
            return "space"
        return "low"


    def _target_stock(self, d):
        symbol = d.get("symbol")
        identity = self._context.instrument_identity(symbol)
        asset = str(d.get("asset") or "").lower()
        return bool(symbol and self._context.quote_supported(symbol) and not d.get("reference_only")
                    and not d.get("etf") and not self._context.is_etf(symbol)
                    and asset not in ("etf", "index", "future", "futures")
                    and not identity.startswith("^") and not identity.endswith("=F"))


    def _target_active(self, d):
        return self._context._target_stock(d) and self._context.target_display_state(d.get("target")) in ("reached", "high", "space")


    def _analysis_context(self, sym, cfg, data, group):
        """把一只标的需要的指标一次算好，各条规则只读不算。"""
        closes = data["closes"]
        price = data["price"]
        prev = data["prev_close"]
        x = SimpleNamespace(sym=sym, cfg=cfg, data=data, group=group,
                            closes=closes, price=price, prev=prev)
        x.hold = str((cfg or {}).get("hold") or "short").lower()
        x.cost = self._context.cost_rule(cfg or {})   # 成本价：只在位置条上画黄竖线，不参与报灯
        x.chg = (price / prev - 1) * 100 if prev else None
        x.rsi = {period: self._context.calc_rsi(closes, period) for period in self._context.RSI_PERIODS}
        x.dates = data.get("dates") or []
        start52 = 0
        if x.dates:
            cutoff = (self._context.datetime.fromisoformat(x.dates[-1]) - timedelta(weeks=52)).date().isoformat()
            start52 = next((i for i, date in enumerate(x.dates) if date >= cutoff), 0)
        high52 = max((data["highs"] or closes)[start52:])
        low52 = min((data["lows"] or closes)[start52:])
        x.dist_high = (price / high52 - 1) * 100
        x.dist_low = (price / low52 - 1) * 100
        x.low52 = low52          # 52 周区间最低价本身：统计名单里要写出这个价格
        x.range52 = self._context._range52_from_history(data)
        x.vol_ratio, x.vol_ratio_date = self._context.completed_day_volume_ratio(sym, data)
        x.vol_ratio_z = self._context.completed_day_volume_z(sym, data)
        distribution_fn = getattr(self._context, "completed_day_volume_distribution", None)
        distribution = distribution_fn(sym, data) if callable(distribution_fn) else None
        x.vol_ratio_m = distribution.get("m") if isinstance(distribution, dict) else None
        x.vol_ratio_s = distribution.get("s") if isinstance(distribution, dict) else None
        x.rt_vol_ratio, x.rt_vol_time = self._context.intraday_volume_ratio(sym, data)
        x.vol_ratio_intraday = bool(x.rt_vol_ratio)
        ma_periods = (int(self._context.S["ma_short"]), int(self._context.S["ma_long"]))
        x.moving_averages = [(period, self._context.sma(closes, period)) for period in dict.fromkeys(ma_periods) if period > 0]
        x.boll_mid, x.boll_up, x.boll_dn = self._context.boll(closes, int(self._context.S["boll_n"]), float(self._context.S["boll_k"]))
        return x


    def _apply_vol_tier(self, x):
        """涨跌 / 振幅阈值：设置页的值是中档，按这只股自己的平时波动缩放；
    涨跌幅对所有分组生效；振幅仅「持仓」和「重点关注」报红黄，其他有效振幅无阈值计灰。"""
        x.vol_tier, x.vol_sigma = self._context.vol_profile(x.closes)
        mult = self._context.VOL_MULT[x.vol_tier]
        x.chg_red, x.chg_yellow = self._context.S["chg_red"] * mult, self._context.S["chg_yellow"] * mult
        x.amp_red, x.amp_yellow = self._context.S["amp_red"] * mult, self._context.S["amp_yellow"] * mult
        x.usual = f"（平时约 ±{x.vol_sigma:.1f}%）" if x.vol_sigma is not None else ""
        x.amp_on = x.group in self._context.AMP_GROUPS


    def classify_technical_facts(self, group, high_hits, low_hits, selected_side):
        """纯事实分类，不计算 RSI 或轨距，也不推断选中轨。

    selected_side 必须明确传 None / 上轨 / 下轨。命中条数为 0..3 整数且和不超过 3。
    返回 kind、level、foldable：kind=None / level=green 表示不计技术灯（可保留文字）；gray 表示技术灰，
    不改变主表整体绿灯；foldable 是独立于红黄灯的折叠资格。
    """
        if not isinstance(group, str) or not group:
            raise ValueError("group 必须是非空分组名称")
        if (type(high_hits) is not int or type(low_hits) is not int
                or not 0 <= high_hits <= 3 or not 0 <= low_hits <= 3
                or high_hits + low_hits > 3):
            raise ValueError("RSI 命中条数必须为 0..3 整数，且超买与超卖条数之和不超过 3")
        if selected_side not in (None, "上轨", "下轨"):
            raise ValueError("selected_side 必须明确为 None、上轨或下轨")
        active_up = group in self._context.BOLL_UP_SIGNAL_GROUPS
        count = max(high_hits, low_hits)
        if selected_side is None:
            if count < 2 or group == "focus" and high_hits == 2:
                return {"kind": None, "level": "green", "foldable": True}
            if group == "focus" and high_hits == 3:
                return {"kind": "rsi", "level": "gray", "foldable": False}
            level = "red" if low_hits == 3 else "yellow" if high_hits == 3 and active_up else "gray"
            return {"kind": "rsi", "level": level, "foldable": True}
        up = selected_side == "上轨"
        if up and group == "focus" and high_hits + low_hits == 1:
            return {"kind": None, "level": "green", "foldable": True}
        if up and not active_up and low_hits >= 2:
            return {"kind": None if low_hits == 2 else "boll_rsi",
                    "level": "green" if low_hits == 2 else "gray", "foldable": True}
        if count == 0:
            if up and group != "position":
                return {"kind": None, "level": "green", "foldable": True}
            return {"kind": "boll", "level": "gray",
                    "foldable": not (group == "position" and not up)}
        # 重点上轨两条同侧 RSI：灰灯保留可见，第三条反向事实不改变主条件。
        if up and group == "focus" and count == 2:
            return {"kind": "boll_rsi", "level": "gray", "foldable": False}
        foldable = not (not up and count == 1
                        or up and group == "position" and count == low_hits == 1)
        # 上轨反向三超卖：持仓/重点黄，其他已在前置分支归灰；下轨三超卖仍红。
        if low_hits == 3:
            level = "yellow" if up else "red"
        elif up and active_up and (high_hits >= 2 or low_hits == 2):
            level = "red" if high_hits == 3 else "yellow"
        elif not up and count >= 2:
            level = "yellow"
        else:
            level = "gray"
        return {"kind": "boll_rsi", "level": level, "foldable": foldable}


    def _rule_rsi(self, al, x):
        """无轨用纯事实 API 判灯；有轨只保留 RSI 事实，由布林选轨后统一归组合。"""
        x.high_hits = sum(v is not None and math.isfinite(v) and v >= self._context.S["rsi_high"] for v in x.rsi.values())
        x.low_hits = sum(v is not None and math.isfinite(v) and v <= self._context.S["rsi_low"] for v in x.rsi.values())
        facts = (self._context.classify_technical_facts(x.group, x.high_hits, x.low_hits, None)
                 if not getattr(x, "boll_hits", []) else None)
        if facts:
            x.foldable = facts["foldable"]
        for count, tag, boundary in ((x.high_hits, "超买", f"≥{self._context.S['rsi_high']:g}"),
                                     (x.low_hits, "超卖", f"≤{self._context.S['rsi_low']:g}")):
            if count >= 2:
                text = self._context.rsi_hits_text(count, tag, boundary)
                if facts and facts["level"] in ("red", "yellow"):
                    al.add(facts["kind"], text, facts["level"])
                else:
                    al.signals.append(text)
        # 无轨单条超卖仅留文字；有轨由布林组合保留事实，避免重复或改变组合规则。
        if facts and x.low_hits == 1 and max(x.high_hits, x.low_hits) < 2:
            al.signals.append(self._context.rsi_hits_text(1, "超卖", f"≤{self._context.S['rsi_low']:g}"))
        # 已有两条同侧提醒时，补齐反向单条事实；单独一条超买仍无提醒。
        if max(x.high_hits, x.low_hits) >= 2:
            for count, tag, boundary in ((x.high_hits, "超买", f"≥{self._context.S['rsi_high']:g}"),
                                         (x.low_hits, "超卖", f"≤{self._context.S['rsi_low']:g}")):
                if count == 1:
                    al.signals.append(self._context.rsi_hits_text(1, tag, boundary))


    def _fresh_52w_low(self, x):
        """已处在低位区时，判断是不是「刚进入 / 再创新低」。算不出昨天的情况就当作新的。"""
        closes, dates, data, price = x.closes, x.dates, x.data, x.price
        if len(dates) < 2 or len(closes) < 2:
            return True
        try:
            cut_y = (self._context.datetime.fromisoformat(dates[-2]) - timedelta(weeks=52)).date().isoformat()
            sy = next((i for i, d_ in enumerate(dates) if d_ >= cut_y), 0)
            lows_all = data["lows"] or closes
            prior_low = min(lows_all[sy:-1])
            yesterday_dist = (closes[-2] / prior_low - 1) * 100
            new_low = min(price, lows_all[-1]) <= prior_low
            return new_low or yesterday_dist > self._context.S["near_52w_low_pct"]
        except (ValueError, TypeError, ZeroDivisionError):
            return True


    def _rule_52w_low(self, al, x):
        """52 周新低：刚进入低位区、或再创新低 → 红；已在低位区且没再创新低 → 黄；远离后不报。"""
        if x.dist_low > self._context.S["near_52w_low_pct"]:
            return
        if self._context._fresh_52w_low(x):
            al.add("low52", "触及52周新低", "red")
        else:
            al.add("low52", f"仍在52周低位（距低点 +{x.dist_low:.1f}%）", "yellow")


    def _rule_add_price(self, al, x):
        """距加仓触发价。"""
        trig = x.cfg.get("trigger")
        x.trig = trig
        if not trig:
            return
        price, target = Decimal(str(x.price)), Decimal(str(trig))
        distance = price - target
        tstr = f"{trig:,.2f}"
        if distance <= 0:
            al.add("add_price", f"已跌破加仓价 {tstr}", "red")
        elif distance * 100 <= Decimal(str(self._context.S["trigger_gap_pct"])) * target:
            gap = distance * 100 / target
            al.add("add_price", f"距加仓价 {tstr} 还差 {gap:.1f}%", "red")


    def _rule_reduce_price(self, al, x):
        """减仓价：只对「持仓」分组生效，和加仓价对称：
    现价已涨到减仓价以上（不管高出多少）→ 红；还没到但距离不足 x%（按减仓价算）→ 红。"""
        x.reduce = self._context.reduce_rule(x.cfg) if x.group == "position" else None
        if not x.reduce:
            return
        price, target = Decimal(str(x.price)), Decimal(str(x.reduce))
        distance = target - price
        rstr = f"{x.reduce:,.2f}"
        if distance <= 0:
            rgap = -distance * 100 / target
            al.add("reduce_price", f"已涨到减仓价 {rstr}（高出 {rgap:.1f}%）", "red")
        elif distance * 100 <= Decimal(str(self._context.S["reduce_gap_pct"])) * target:
            gap = distance * 100 / target
            al.add("reduce_price", f"距减仓价 {rstr} 还差 {gap:.1f}%", "red")


    def _rule_target(self, al, x):
        """所有分组个股计算空间；达到仅持仓/重点，二者只叠加紫色，不改原灯级。"""
        stock = {"symbol": x.sym, "asset": x.data.get("asset") or x.cfg.get("asset"),
                 "etf": x.cfg.get("etf"), "reference_only": x.data.get("reference_only")}
        x.tgt = (self._context.evaluate_target(x.price, x.prev, self._context.resolve_target(x.cfg, x.cfg.get("_consensus")),
                                 x.group, self._context.S["target_gap_pct"]) if self._context._target_stock(stock) else None)
        if not x.tgt or x.tgt.get("failed"):
            return
        for field in ("reached_detail", "space_detail"):
            if x.tgt.get(field):
                al.add("target", x.tgt[field], "purple")


    def _boll_near_pct(self, x):
        """本只标的的「算不算贴近轨道」门槛（%）。
    优先按个股平时波动归一化后的布林带宽取比例；算不出波动（新股、样本不足）才回退固定百分比。"""
        band_pct = float(self._context.S.get("boll_near_band_pct") or 0)
        sigma = getattr(x, "vol_sigma", None)
        if band_pct > 0 and isinstance(sigma, (int, float)) and not isinstance(sigma, bool) \
                and math.isfinite(sigma) and sigma > 0:
            return band_pct / 100 * 4 * self._context._BOLL_SIGMA_FACTOR * sigma
        return float(self._context.S["boll_near_pct"])


    def _boll_hits(self, x):
        """布林上下轨命中情况 → [(还差百分之多少, 文字)]；<=0 表示已经穿过去了。
    另外标出「连续第几日贴近轨道」——远离一天就断，再贴近重新从第一日算。"""
        near = self._context._boll_near_pct(x)
        streak = self._context.boll_streak(x.closes, int(self._context.S["boll_n"]), float(self._context.S["boll_k"]), near,
                             highs=x.data.get("highs"), lows=x.data.get("lows"))
        hits = []
        if x.boll_up is not None:
            gap_up = (x.boll_up - x.touch_up) / x.boll_up * 100
            if gap_up <= near:
                if gap_up < 0:
                    action = "突破" if x.price >= x.boll_up else "盘中突破"
                    text = f"{action}布林{self._context.boll_side_text('上轨')}"
                elif gap_up == 0:
                    text = f"触及布林{self._context.boll_side_text('上轨')}"
                else:
                    text = f"逼近布林{self._context.boll_side_text('上轨')} 还差{gap_up:.2f}%"
                hits.append((gap_up, text + self._context._touch_note(x.price, x.touch_up) + self._context.streak_tail(streak["up"], "上轨")))
        if x.boll_dn is not None:
            gap_dn = (x.touch_dn - x.boll_dn) / x.boll_dn * 100
            if gap_dn <= near:
                text = (f"跌破布林{self._context.boll_side_text('下轨')}" if gap_dn < 0
                        else f"触及布林{self._context.boll_side_text('下轨')}" if gap_dn == 0
                        else f"逼近布林{self._context.boll_side_text('下轨')} 还差{gap_dn:.2f}%")
                hits.append((gap_dn, text + self._context._touch_note(x.price, x.touch_dn) + self._context.streak_tail(streak["dn"], "下轨")))
        return hits


    def _rule_boll(self, al, x):
        """先按带符号距离选轨（相等上轨），再按分组限制；双轨保留双方事实，不换边。"""
        hits = x.boll_hits if hasattr(x, "boll_hits") else self._context._boll_hits(x)
        if not hits:
            return
        best = min(hits, key=lambda h: h[0])
        up = "布林上轨" in best[1]
        side = "上轨" if up else "下轨"
        facts = self._context.classify_technical_facts(x.group, x.high_hits, x.low_hits, side)
        x.foldable = facts["foldable"]
        if len(hits) == 1 and facts["kind"] is None and x.high_hits == x.low_hits == 0:
            return
        x.boll_alert_text = best[1]
        al.signals.extend(hit[1] for hit in hits)
        same_hits = x.high_hits if up else x.low_hits
        oppo_hits = x.low_hits if up else x.high_hits
        # 单条 RSI 仅在触轨时补文字；不把单独 RSI 1 条变成提醒。
        for count, tag, boundary in ((x.high_hits, "超买", f"≥{self._context.S['rsi_high']:g}"),
                                     (x.low_hits, "超卖", f"≤{self._context.S['rsi_low']:g}")):
            if count == 1:
                text = self._context.rsi_hits_text(1, tag, boundary)
                if text not in al.signals:
                    al.signals.append(text)
        level = facts["level"]
        if level not in ("red", "yellow"):
            return
        if same_hits >= 2:
            x.boll_rsi_direction = f"{side}（{'超买' if up else '超卖'}）"
            x.boll_rsi_hits = same_hits
        else:
            x.boll_rsi_direction = f"{side}（RSI{'超卖' if up else '超买'}，方向不一致）"
            x.boll_rsi_hits = oppo_hits
        al.add(facts["kind"], f"布林 + RSI → {'红' if level == 'red' else '黄'}", level)


    def _vol_ratio_z_enabled(self):
        """收盘后的「自身波动」自适应开关：两条 z 阈值都大于 0 才生效，否则退回固定倍数。"""
        return self._context.S.get("vol_ratio_z", 0) > 0 and self._context.S.get("vol_ratio_red_z", 0) > 0


    def _rule_vol_ratio(self, al, x):
        """量比：盘中用当日累计量按已开盘分钟数折算的「盘中量比」，收盘后回落到最近完整日量比。
    收盘后用 sqrt(exp(m+k*s)*F) 连续门槛；显式分布缺失或无效才退回固定倍数。
    不从量比与 z 反推分布；盘中始终用固定倍数。
    达到红线报红、达到黄线报黄，所有分组适用；不分涨跌方向。"""
        if getattr(x, "vol_ratio_intraday", False):
            ratio = x.rt_vol_ratio
            level = "red" if ratio >= self._context.S["vol_ratio_red"] else "yellow" if ratio >= self._context.S["vol_ratio"] else ""
            if level:
                al.add("vol_ratio", f"盘中量比（截至 {x.rt_vol_time} ET）{ratio:.1f}x", level)
            return   # 盘中已拿到当时量比，不再叠加昨天的完整日口径
        ratio = getattr(x, "vol_ratio", None)
        if (not isinstance(ratio, (int, float)) or isinstance(ratio, bool)
                or not math.isfinite(ratio) or ratio <= 0):
            return
        m, s = getattr(x, "vol_ratio_m", None), getattr(x, "vol_ratio_s", None)
        if (self._context._vol_ratio_z_enabled() and ratio >= 1.0
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                        for v in (m, s)) and s > 0):
            # log(sqrt(T*F)) = m/2 + k*s/2 + log(F)/2，不展开可能溢出的指数或乘积。
            log_ratio = math.log(ratio)
            yellow = m / 2 + (s / 2) * self._context.S["vol_ratio_z"] + math.log(self._context.S["vol_ratio"]) / 2
            red = m / 2 + (s / 2) * self._context.S["vol_ratio_red_z"] + math.log(self._context.S["vol_ratio_red"]) / 2
            level = "red" if log_ratio >= red else "yellow" if log_ratio >= yellow else ""
            if level:
                al.add("vol_ratio", f"最近完整日量比（{x.vol_ratio_date}）{ratio:.1f}x", level)
            return   # 有效自适应未达标，不按固定倍数补灯
        level = "red" if ratio >= self._context.S["vol_ratio_red"] else "yellow" if ratio >= self._context.S["vol_ratio"] else ""
        if level:
            al.add("vol_ratio", f"最近完整日量比（{x.vol_ratio_date}）{ratio:.1f}x", level)


    def _rule_moving_averages(self, al, x):
        """第一条确认穿越，第二条日内范围触及加简单穿越；0独立关闭。
    第二条以昨日收盘/昨日SMA和现价/本轮SMA比较，不要求带宽确认。
    两条启用且同周期时，保留原先去重后的简单穿越规则。"""
        closes, price, mas = x.closes, x.price, x.moving_averages
        if len(closes) < 2:
            return
        prev_c = closes[-2]
        for period, ma_val in mas:
            ma_name = f"{period}日"
            if not ma_val:
                continue
            weak = period == self._context.S["ma_short"] and self._context.S["ma_short"] != self._context.S["ma_long"]
            strong = period == self._context.S["ma_long"] and self._context.S["ma_short"] != self._context.S["ma_long"]
            if strong:
                # 源降级可能返回旧日线：不要把昨日触及/穿越重复说成本轮目标日事件。
                dates = getattr(x, "data", {}).get("dates") or []
                if self._context.TARGET_DATE and (not dates or dates[-1] != self._context.TARGET_DATE):
                    continue
                gap_ma = (price / ma_val - 1) * 100
                day_low, day_high, has_range = self._context._ma_day_range(x)
                band = self._context.MA_LONG_BAND_PCT / 100.0
                eps = ma_val * 1e-11
                if day_low <= ma_val * (1 + band) + eps and day_high >= ma_val * (1 - band) - eps:
                    if has_range:
                        text = (f"日内触及{ma_name}均线 ±{self._context.MA_LONG_BAND_PCT:g}%区间"
                                f"（日内 {day_low:,.2f}~{day_high:,.2f}，现价偏离 {gap_ma:+.1f}%）")
                    else:
                        text = f"现价贴近{ma_name}均线 {gap_ma:+.1f}%（日内高低缺失，仅按现价）"
                    x.ma_band_direction = "up" if price > ma_val else "down" if price < ma_val else "level"
                    al.add("ma_band", text, "yellow")
                prev_ma = self._context.sma(closes[:-1], period)
                if prev_ma:
                    if prev_c < prev_ma and price >= ma_val:
                        al.add("ma_cross", f"上穿{ma_name}均线", "yellow")
                    elif prev_c > prev_ma and price <= ma_val:
                        al.add("ma_cross", f"跌破{ma_name}均线", "yellow")
                continue
            confirmed = self._context.ma_confirmed_cross(closes, price, period, self._context.ma_confirm_pct(x.vol_sigma)) if weak else "n/a"
            if confirmed == "up" or (confirmed == "n/a" and prev_c < ma_val <= price):
                al.add("ma_cross", f"上穿{ma_name}均线", "yellow")
            elif confirmed == "down" or (confirmed == "n/a" and prev_c > ma_val >= price):
                al.add("ma_cross", f"跌破{ma_name}均线", "yellow")


    def _analysis_detail(self, x, al):
        return {
            "symbol": x.sym,
            "note": x.cfg.get("note", ""),
            "price": x.price,
            "chg": x.chg,
            "rsi": x.rsi,
            "dist_high": x.dist_high,
            "dist_low": x.dist_low,
            "low52": getattr(x, "low52", None),
            "range52": x.range52,
            "vol_ratio": x.vol_ratio,
            "vol_ratio_z": getattr(x, "vol_ratio_z", None),
            "vol_ratio_m": getattr(x, "vol_ratio_m", None),
            "vol_ratio_s": getattr(x, "vol_ratio_s", None),
            "vol_ratio_date": x.vol_ratio_date,
            "rt_vol_ratio": x.rt_vol_ratio,
            "rt_vol_time": x.rt_vol_time,
            "vol_ratio_intraday": x.vol_ratio_intraday,
            "amp": x.amp,
            "vol_tier": x.vol_tier,
            "vol_sigma": x.vol_sigma,
            "trigger": x.trig,
            "reduce": x.reduce,
            "cost": getattr(x, "cost", None),
            "target": x.tgt,
            "purple": bool(x.tgt and (x.tgt.get("hit") or x.tgt.get("space_hit"))),
            "prev_close": x.prev,
            "asset": x.data.get("asset") or x.cfg.get("asset"),
            "source": x.data.get("source", ""),
            "data_date": (x.data.get("dates") or [""])[-1],
            "realtime": bool(x.data.get("realtime")),
            "rt_ts": x.data.get("rt_ts", ""),
            "group": x.group,
            "hold": getattr(x, "hold", ""),
            "boll_up": x.boll_up,
            "boll_dn": x.boll_dn,
            "boll_rsi_direction": getattr(x, "boll_rsi_direction", ""),
            "boll_rsi_hits": getattr(x, "boll_rsi_hits", None),
            "boll_alert_text": getattr(x, "boll_alert_text", ""),
            "ma_band_direction": getattr(x, "ma_band_direction", ""),
            "ma_settings": {key: self._context.S.get(key) for key in ("ma_short", "ma_long")},
            # ETF 标签：静态表命中，或行情源明确给出 assetclass=etf
            "etf": self._context.is_etf(x.sym) or x.data.get("asset") == "etf",
            "signals": al.signals,
            "signal_marks": al.marks,
            "level": al.level,
            "foldable": getattr(x, "foldable", True),
        }


    def analyze_symbol(self, sym, cfg, data, group="technology"):
        """返回等级、信号和详情，分组定义见 groups.json。
    规则按「红 → 加减仓价/目标价 → 布林 → 黄」的顺序依次跑，顺序也是页面上警示文字的顺序。"""
        x = self._context._analysis_context(sym, cfg, data, group)
        al = self._context._Alerts()
        self._context._apply_vol_tier(x)
        self._context._intraday_extremes(x)
        x.boll_hits = self._context._boll_hits(x)
        self._context._rule_chg_red(al, x)
        self._context._rule_rsi(al, x)
        self._context._rule_52w_low(al, x)
        self._context._rule_add_price(al, x)
        self._context._rule_reduce_price(al, x)
        self._context._rule_target(al, x)
        self._context._rule_boll(al, x)
        self._context._rule_chg_yellow(al, x)
        self._context._rule_vol_ratio(al, x)
        self._context._rule_amplitude(al, x)
        self._context._rule_moving_averages(al, x)
        return al.level, al.signals, self._context._analysis_detail(x, al)


    def boll_side_text(self, side):
        """布林轨道文字：上轨▲ / 下轨▼。side 必须是「上轨」或「下轨」。"""
        if side not in ("上轨", "下轨"):
            return str(side or "")
        return side + (self._context._ARROW_BAND_UP if side == "上轨" else self._context._ARROW_BAND_DOWN)


    def rsi_hits_text(self, hits, tag, boundary=None):
        """RSI 条数文字：RSI 2条 ↑超买（≥70）。tag 必须是「超买」或「超卖」。"""
        arrow = self._context._ARROW_RSI_UP if tag == "超买" else self._context._ARROW_RSI_DOWN if tag == "超卖" else ""
        text = f"RSI {hits}条 {arrow}{tag}".rstrip()
        return f"{text}（{boundary}）" if boundary else text


    def boll_rsi_text(self, direction, hits=None):
        """组合条件统一文案：上轨▲ · RSI 2条 ↑超买（必要时带「反向／双向命中」）。

    组合与单独用同一套措辞：布林只写上下轨，RSI 只写超买超卖，中间用「 · 」隔开。"""
        side, tag, tail = self._context._parse_boll_rsi_direction(direction)
        if tail == "方向不一致":
            tail = "反向"
        if not side:
            return "方向待下次更新"
        parts = [self._context.boll_side_text(side)]
        if type(hits) is int and hits in (1, 2, 3):
            parts.append(self._context.rsi_hits_text(hits, tag) if tag else f"RSI {hits}条")
        text = " · ".join(parts)
        return f"{text} · {tail}" if tail else text
