# -*- coding: utf-8 -*-
"""快照投影，不联网或写文件；构造只保存运行依赖，context 仅兼容旧入口，不增加状态。"""
import math
import os
import re

# 各类信号的统计名称（status.json 的 signal_counts 用）；顺序即展示顺序
SIGNAL_KINDS = (
    ("chg", "涨跌幅"), ("amp", "振幅"), ("vol_ratio", "量比"),
    ("rsi", "RSI"), ("boll", "布林"), ("boll_rsi", "布林+RSI"),
    ("ma", "均线"), ("low52", "52周低位"),
    ("add_price", "加仓价"), ("reduce_price", "减仓价"), ("target", "共识价"),
)


_TEXT_RSI_RE = re.compile(r"^RSI [123]条\s*[↑↓]?(超买|超卖)")


_TEXT_BOLL_PREFIX = ("逼近布林", "触及布林", "跌破布林", "突破布林", "盘中突破布林")


_RETIRED_SNAPSHOT_KEYS = frozenset({
    "fund", "fundamental", "fund_cache_note", "fund_hits", "fund_period",
    "fund_enable", "fund_rev_red", "fund_rev_yellow", "fund_om_drop", "fund_gm_drop",
    "fund_de_rise", "fund_cr_drop", "fund_roe_drop", "fund_pm_abnormal", "fund_report_grace_days",
    "target_anomaly_pct",
})


def _member_metric(d, kind):
    """仅把本轮已触发信号的数值带入成员明细；旧快照缺值时不回查行情。"""
    if kind not in ("chg", "amp", "vol_ratio"):
        return ""
    if kind == "vol_ratio" and d.get("vol_ratio_intraday"):
        value = d.get("rt_vol_ratio")
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                or value < 0):
            return ""
        moment = d.get("rt_vol_time")
        moment_text = (f"（截至 {moment} ET）" if isinstance(moment, str)
                       and re.fullmatch(r"\d{2}:\d{2}", moment) else "")
        return f"盘中量比 {value:.1f}×{moment_text}"
    value = d.get({"chg": "chg", "amp": "amp", "vol_ratio": "vol_ratio"}[kind])
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            or (kind != "chg" and value < 0)):
        return ""
    if kind == "chg":
        return f"涨跌幅 {value:+.2f}%"
    if kind == "amp":
        return f"振幅 {value:.1f}%"
    day = d.get("vol_ratio_date")
    date_text = f"（{day}完整日）" if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""
    # σ 不出现在页面上：判灯照常用 z，展示只留「哪天、几倍」
    return f"量比 {value:.1f}×{date_text}"


class SnapshotBuilder:
    def __init__(self, context):
        self._context = context

    def _apply_target(self, d):
        """在副本中迁移旧条目；只用真实目标与行情重算，不相信旧紫计数或异常灯。"""
        out = dict(d)
        marks = list(d.get("signal_marks") or [])
        target = d.get("target")
        if isinstance(target, dict) and self._context._target_stock(d):
            if not all(key in target for key in ("band", "space_hit", "space_detail", "reached_detail")):
                prev = self._context._pos_num(d.get("prev_close")) or self._context._pos_num(d.get("prev"))
                chg = d.get("chg")
                if (prev is None and self._context._pos_num(d.get("price")) is not None
                        and type(chg) in (int, float) and math.isfinite(chg) and chg > -100):
                    prev = d["price"] / (1 + chg / 100)
                target = self._context.evaluate_target(d.get("price"), prev, target, d.get("group"), self._context.S["target_gap_pct"])
            else:
                target = {key: value for key, value in target.items() if key != "anomaly"}
                if target.get("failed") or self._context._pos_num(d.get("price")) is None:
                    target = self._context.evaluate_target(d.get("price"), None, target, d.get("group"), self._context.S["target_gap_pct"])
                elif d.get("group") not in self._context.CONSENSUS_GROUPS:
                    target.update(hit=False, reached_detail="")
        else:
            target = None
        out["target"] = target
        out["purple"] = self._context._target_stock(out) and self._context.target_display_state(target) in ("reached", "high", "space")
        out["signal_marks"] = [mark for mark in marks if mark[0] != "target"]
        if (any(kind == "target" and level in ("red", "yellow") for kind, level in marks)
                or isinstance(d.get("target"), dict) and d["target"].get("anomaly")):
            remaining = {level for _, level in out["signal_marks"]}
            out["level"] = ("red" if "red" in remaining else "yellow" if "yellow" in remaining
                            else "green" if self._context._pos_num(d.get("price")) is not None else "gray")
        out["signals"] = [text for text in d.get("signals") or []
                          if not self._context._consensus_description(text, d.get("target") or {})
                          and not (not d.get("target") and any(kind == "target" for kind, _ in marks)
                                   and text == "目标价疑似数据异常")]
        if out["purple"]:
            for field in ("reached_detail", "space_detail"):
                if target.get(field) and target[field] not in out["signals"]:
                    out["signals"].append(target[field])
            out["signal_marks"].append(("target", "purple"))
        return out


    def target_overlay_members(self, items):
        """空间与达到分别去重，沿用首页身份优先级；两段允许同一身份重叠。"""
        priority = {g["item_group"]: i for i, g in enumerate(self._context.GROUPS)}
        seen = set(self._context.RISK_IDENTITIES)
        out = {"space": [], "reached": []}
        for item in sorted(items, key=lambda d: priority.get(d.get("group"), len(self._context.GROUPS))):
            symbol = self._context.normalize_symbol(item.get("symbol"))
            identity = self._context.instrument_identity(symbol)
            if identity in seen:
                continue
            seen.add(identity)
            d = self._context._apply_target(item)
            if not self._context._target_active(d):
                continue
            target = d["target"]
            record = {"symbol": symbol, "note": d.get("note") or "", "group": d.get("group") or "",
                      "hold": str(d.get("hold") or ""), "level": d.get("level") or "",
                      "space": target["space"], "band": target["band"], "price": target["price"],
                      "kind": target.get("kind") or ""}
            if target.get("space_hit"):
                out["space"].append({**record, "detail": target["space_detail"]})
            if target.get("hit") and d.get("group") in self._context.CONSENSUS_GROUPS:
                out["reached"].append({**record, "detail": target["reached_detail"]})
        return out


    def _target_overlay_union(self, overlays):
        """旧紫成员接口使用两段的身份并集，并保留重叠标的的两条具体说明。"""
        members = {}
        for section in ("space", "reached"):
            for record in overlays.get(section) or []:
                identity = self._context.instrument_identity(record["symbol"])
                if identity not in members:
                    members[identity] = dict(record)
                elif record.get("detail"):
                    members[identity]["detail"] += " / " + record["detail"]
        return list(members.values())


    def purple_counts(self, items):
        return len(self._context._target_overlay_union(self._context.target_overlay_members(items)))


    def signal_counts(self, items):
        """各类信号的触发数量，写进 status.json，方便看「黄灯到底是被哪条规则点亮的」。
    red / yellow：这一类信号以红 / 黄级别触发的标的数；purple：目标价提示数。
    只出文字不报灯的标的（灰灯）不在这里统计，单独走 text_only_members()。"""
        members = self._context.signal_count_members(items)
        return {kind: {"label": label, **{level: len(members[kind][level])
                                        for level in ("red", "yellow", "purple")}}
                for kind, label in self._context.SIGNAL_KINDS}


    def _text_only_kind(self, signals):
        """技术灰归因：单独 RSI 至少两条才归 rsi；只有布林文字归 boll；
    两者都有归 boll_rsi（含单条 RSI），不重复归入单独指标。"""
        texts = [str(s) for s in (signals or []) if isinstance(s, str)]
        has_rsi = any(self._context._TEXT_RSI_RE.match(t) for t in texts)
        has_boll = any(t.startswith(self._context._TEXT_BOLL_PREFIX) for t in texts)
        if has_rsi and has_boll:
            return "boll_rsi"
        if has_rsi and any(re.match(r"^RSI [23]条\s*[↑↓]?(超买|超卖)", t) for t in texts):
            return "rsi"
        if has_boll:
            return "boll"
        return ""


    def _text_only_shape(self, signals):
        """灰灯名单里的方向：优先看布林轨道，没有布林文字才看 RSI 的买卖方向。"""
        texts = [str(s) for s in (signals or []) if isinstance(s, str)]
        boll = next((t for t in texts if t.startswith(self._context._TEXT_BOLL_PREFIX)), "")
        if boll:
            return "▲" if "上轨" in boll else "▼"
        rsi = next((self._context._TEXT_RSI_RE.match(t) for t in texts if self._context._TEXT_RSI_RE.match(t)), None)
        return ("▲" if rsi.group(1) == "超买" else "▼") if rsi else ""


    def text_only_members(self, items):
        """技术灰与同股其它指标红黄并存；技术组合与单独指标互斥。
    主表最高灯级不变；重复身份沿用首页分类优先级，只处理优先条目。"""
        priority = {g["item_group"]: i for i, g in enumerate(self._context.GROUPS)}
        seen = set(self._context.RISK_IDENTITIES)
        out = []
        for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(self._context.GROUPS))):
            symbol = self._context.normalize_symbol(d.get("symbol"))
            identity = self._context.instrument_identity(symbol)
            if identity in seen:
                continue
            seen.add(identity)
            if (d.get("level") == "gray" or d.get("reference_only")
                    or "price" in d and (not isinstance(d["price"], (int, float))
                        or isinstance(d["price"], bool) or not math.isfinite(d["price"]) or d["price"] <= 0)):
                continue
            kind = self._context._text_only_kind(d.get("signals"))
            technical_marks = {k for k, level in d.get("signal_marks") or []
                               if k in ("boll", "rsi", "boll_rsi") and level in ("red", "yellow")}
            # 同一组技术事实已有红黄就不能再计灰；涨跌幅/均线等不影响技术灰资格。
            if not kind or technical_marks:
                continue
            texts = [str(s) for s in (d.get("signals") or []) if isinstance(s, str)]
            selected = d.get("boll_alert_text")
            if isinstance(selected, str) and selected in texts and selected.startswith(self._context._TEXT_BOLL_PREFIX):
                texts = [selected] + [text for text in texts if text != selected]
            # 只留主句：括号里的「盘中触及 / 连续第几日贴近」太长，占满一行反而不好看。
            # 组合被挡下的（boll_rsi）优先说轨道，因为那才是被规则拦下的原因。
            pool = [t for t in texts if t.startswith(self._context._TEXT_BOLL_PREFIX)]
            if kind != "boll_rsi":
                pool = [t for t in texts
                        if self._context._TEXT_RSI_RE.match(t) or t.startswith(self._context._TEXT_BOLL_PREFIX)]
            detail = re.split(r"（", pool[0])[0].strip() if pool else ""
            rsi_texts = [t for t in texts if self._context._TEXT_RSI_RE.match(t)]
            rsi_main = " / ".join(dict.fromkeys(re.split(r"（", t)[0].strip() for t in rsi_texts))
            if kind == "boll_rsi" and rsi_main:
                detail = f"{detail} · {rsi_main}" if detail else rsi_main
            elif kind == "rsi":
                detail = rsi_main
            boll_text = next((t for t in texts if t.startswith(self._context._TEXT_BOLL_PREFIX)), "")
            # 灰统计只接受共享分类器确认的技术灰，不再把“有技术文字且无红黄”当灰。
            # 兼容旧快照时从已保存的事实读条数；双轨仍用已选轨，不借另一轨补灯。
            hits = {"超买": 0, "超卖": 0}
            for text in rsi_texts:
                for match in re.finditer(r"([123])条\s*[↑↓]?(超买|超卖)", text):
                    hits[match[2]] = max(hits[match[2]], int(match[1]))
            side = "上轨" if "上轨" in boll_text else "下轨" if "下轨" in boll_text else None
            facts = self._context.classify_technical_facts(d.get("group"), hits["超买"], hits["超卖"], side)
            if facts["level"] != "gray" or facts["kind"] != kind:
                continue
            rsi_match = max((self._context._TEXT_RSI_RE.match(t) for t in rsi_texts),
                            key=lambda match: int(match.group(0)[4]), default=None)
            out.append({"symbol": symbol, "kind": kind, "shape": self._context._text_only_shape(texts),
                        "side": ("上轨" if "上轨" in boll_text else "下轨") if boll_text else "",
                        "rsi_n": int(rsi_match.group(0)[4]) if rsi_match else 0,
                        "rsi_side": rsi_match.group(1) if rsi_match else "",
                        "detail": detail, "group": d.get("group") or "",
                        "level": d.get("level") if d.get("level") in ("red", "yellow", "green") else "green",
                        "hold": str(d.get("hold") or ""),
                        "note": str(d.get("note") or "")})
        return out


    def _ma_display_text(self, text, settings=None):
        """均线展示投影：使用该事实的配置归属，不改变内部周期、原始信号或计算。
    历史周期不匹配时明确待确认；同周期共用短/长身份，绝不拆成两条计数。"""
        config = settings if isinstance(settings, dict) else self._context.S
        if " / " in str(text):
            return " / ".join(self._context._ma_display_text(part, config) for part in str(text).split(" / "))
        def name(match):
            period = int(match[1])
            short, long = period == config.get("ma_short"), period == config.get("ma_long")
            return "短均线/长均线" if short and long else "短均线" if short else "长均线" if long else "均线（归属待确认）"
        output = re.sub(r"(\d+)日(?:弱)?均线", name, str(text))
        output = output.replace("日内触及长均线", "触及长均线")
        named = set(re.findall(r"(?:短|长)均线", output))
        if len(named) == 1:
            label = next(iter(named))
            output = output.replace("现价在均线", "现价在" + label).replace("现价与均线", "现价与" + label)
        return output


    def _member_direction_markers(self, d, kind):
        """仅从本轮已经触发的信号取方向；图形只描述触轨/穿越位置，不预测后续涨跌。"""
        signals = [s for s in (d.get("signals") or []) if isinstance(s, str)]
        markers = []
        def add(shape, tone, label):
            marker = {"shape": shape, "tone": tone, "label": label}
            if marker not in markers:
                markers.append(marker)
        if kind == "rsi":
            for s in signals:
                if not s.startswith("RSI "):
                    continue
                for match in re.finditer(r"([123])条\s*([↑↓])?(超买|超卖)", s):
                    count, side = int(match[1]), match[3]
                    # ▲▼ 只表示布林上下轨；RSI 的方向标记是 ↑超买 / ↓超卖
                    add(self._context._ARROW_RSI_UP if side == "超买" else self._context._ARROW_RSI_DOWN,
                        "red" if count == 3 and side == "超卖" else "yellow",
                        self._context.rsi_hits_text(count, side))
        elif kind in ("chg", "amp", "vol_ratio"):
            chg = d.get("chg")
            if isinstance(chg, (int, float)) and not isinstance(chg, bool) and math.isfinite(chg) and chg != 0:
                day = d.get("data_date")
                day = day if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else "本轮行情日"
                add("▲" if chg > 0 else "▼", "white",
                    f"{day}股价{'上涨' if chg > 0 else '下跌'}{abs(chg):.2f}%")
        elif kind == "boll":
            # 新快照只取真正触发黄灯的那条；上轨仅作说明时不制造黄灯图标。
            if "boll_alert_text" in d:
                signals = [d["boll_alert_text"]] if d["boll_alert_text"] else []
            for s in signals:
                for prefix, shape, tone, label in (
                        ("盘中突破布林上轨", "▲", "yellow", "盘中突破上轨"),
                        ("突破布林上轨", "▲", "yellow", "突破上轨"),
                        ("跌破布林下轨", "▼", "yellow", "盘中跌破下轨"),
                        ("触及布林上轨", "▲", "white", "触及上轨"),
                        ("逼近布林上轨", "▲", "white", "逼近上轨"),
                        ("触及布林下轨", "▼", "white", "触及下轨"),
                        ("逼近布林下轨", "▼", "white", "逼近下轨")):
                    if s.startswith(prefix):
                        add(shape, tone, label)
                        break
        elif kind == "ma":
            marks = {k for k, _ in d.get("signal_marks") or []}
            if marks & {"ma", "ma_cross"}:
                markers.extend(self._context._member_direction_markers(d, "ma_cross"))
            if marks & {"ma", "ma_band"}:
                side = d.get("ma_band_direction")
                for s in signals:
                    match = re.match(r"^(日内触及|现价贴近)(\d+)日(?:弱)?均线", s)
                    if match and side in ("up", "down", "level"):
                        label = f"{match[1]}{match[2]}日均线，" + {
                            "up": "现价在均线上方", "down": "现价在均线下方", "level": "现价与均线相等"}[side]
                        add({"up": "▲", "down": "▼", "level": "■"}[side], "cyan", label)
        elif kind == "ma_cross":
            for s in signals:
                match = re.match(r"^(上穿|跌破)(\d+)日(?:弱)?均线", s)
                if match:
                    add("▲" if match[1] == "上穿" else "▼", "cyan",
                        f"{match[1]}{match[2]}日均线")
        elif kind == "ma_band":
            side = d.get("ma_band_direction")
            if side in ("up", "down", "level"):
                shape = {"up": "▲", "down": "▼", "level": "■"}[side]
                for text in signals:
                    match = re.match(r"^(日内触及|现价贴近)(\d+)日(?:弱)?均线", text)
                    if match:
                        position = {"up": "现价在均线上方", "down": "现价在均线下方",
                                    "level": "现价与均线相等"}[side]
                        add(shape, "cyan", f"{match[1]}{match[2]}日均线，{position}")
        elif kind == "target":
            for s in signals:
                if s.startswith(("今日上穿目标价", "今日上穿共识价")):
                    add("▲", "purple", "当日上穿目标价或共识价")
                elif s.startswith(("今日跌破目标价", "今日跌破共识价")):
                    add("▼", "purple", "当日跌破目标价或共识价")
                elif s.startswith(("在目标价", "在共识价")) and " ±" in s:
                    add("■", "purple", "现价在目标价或共识价范围内")
                elif s.startswith(("目标价 ", "共识价 ")) and "，空间 +" in s:
                    pct = re.search(r"，空间 \+(\d+(?:\.\d+)?)%", s)
                    add("□", "purple", f"目标价或共识价上涨空间达到 {pct[1]}%" if pct
                        else "目标价或共识价上涨空间达到提示标准")
        return markers


    def _member_signal_details(self, d, kind, member, level):
        indicators = self._context._member_direction_markers(d, kind)
        if kind == "rsi":
            # 同只股票可能另有红灯；计数格只展示与本格告警等级相符的 RSI 条数。
            indicators = [marker for marker in indicators if marker["tone"] == level]
        if indicators:
            member["indicators"] = indicators
        if kind == "ma":
            settings = d.get("ma_settings") if isinstance(d.get("ma_settings"), dict) else self._context.S
            member["ma_settings"] = {key: settings.get(key) for key in ("ma_short", "ma_long")}
            marks = {k for k, _ in d.get("signal_marks") or []}
            facts = []
            for text in d.get("signals") or []:
                if not isinstance(text, str):
                    continue
                fact_kind = ("ma_cross" if re.match(r"^(上穿|跌破)\d+日(?:弱)?均线", text)
                             else "ma_band" if re.match(r"^(日内触及|现价贴近)\d+日(?:弱)?均线", text) else "")
                if fact_kind and (fact_kind in marks or "ma" in marks):
                    fact = {"kind": fact_kind, "text": text}
                    if fact not in facts:
                        facts.append(fact)
            if facts:
                member["ma_facts"] = facts
        metric = self._context._member_metric(d, kind)
        if metric:
            member["metric"] = metric
        if kind == "low52":
            # 52周低位那一栏把低点价本身写出来：只说「距低点 +2.6%」看不见价格
            low = d.get("low52")
            if (isinstance(low, (int, float)) and not isinstance(low, bool)
                    and math.isfinite(low) and low > 0):
                member["low52_price"] = f"{low:,.2f}"
        if kind == "rsi" and indicators:
            member["rsi_hits"] = " / ".join(str(marker["label"]) for marker in indicators)
        if kind == "boll" and indicators:
            member["boll_state"] = " / ".join(marker["label"] for marker in indicators)
        if kind in ("chg", "amp", "vol_ratio") and d.get("chg") == 0 and not isinstance(d.get("chg"), bool):
            member["flat"] = True


    def _merge_ma_members(self, candidates):
        """均线按真实身份取最高信号等级，所有已触发事实保留在同一成员中。"""
        levels = ("red", "yellow", "purple")
        priority = {g["item_group"]: i for i, g in enumerate(self._context.GROUPS)}
        unique = {}
        for level, source in sorted(candidates, key=lambda pair: priority.get(pair[1].get("group"), len(self._context.GROUPS))):
            identity = self._context.instrument_identity(source["symbol"])
            if identity in self._context.RISK_IDENTITIES:
                continue
            entry = {**source, "symbol": self._context.normalize_symbol(source["symbol"])}
            previous = unique.get(identity)
            if previous:
                old_level, old = previous
                chosen_level = min((old_level, level), key=levels.index)
                entry = dict(old if chosen_level == old_level else entry)
                for field in ("indicators", "ma_facts"):
                    values = list(old.get(field) or [])
                    values += [value for value in source.get(field) or [] if value not in values]
                    if values:
                        entry[field] = values
                unique[identity] = (chosen_level, entry)
            else:
                unique[identity] = (level, entry)
        return {level: [entry for chosen, entry in unique.values() if chosen == level] for level in levels}


    def signal_count_members(self, items):
        """原规则口径不变；均线合并后按真实身份最高等级统计，事实不互相覆盖。"""
        levels = ("red", "yellow", "purple")
        out = {kind: {level: [] for level in levels} for kind, _ in self._context.SIGNAL_KINDS}
        priority = {g["item_group"]: i for i, g in enumerate(self._context.GROUPS)}
        seen = set(self._context.RISK_IDENTITIES)
        ma_candidates = []
        for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(self._context.GROUPS))):
            if (d.get("reference_only") or d.get("level") == "gray"
                    or "price" in d and d["price"] is None):
                continue
            ma_levels = {level for kind, level in d.get("signal_marks") or []
                         if kind in ("ma", "ma_cross", "ma_band") and level in levels}
            if ma_levels:
                level = min(ma_levels, key=levels.index)
                entry = {"symbol": self._context.normalize_symbol(d.get("symbol")), "note": d.get("note") or "",
                         "group": d.get("group") or "", "hold": str(d.get("hold") or ""),
                         "level": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else ""}
                self._context._member_signal_details(d, "ma", entry, level)
                ma_candidates.append((level, entry))
        out["ma"] = self._context._merge_ma_members(ma_candidates)
        for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(self._context.GROUPS))):
            symbol = self._context.normalize_symbol(d.get("symbol"))
            identity = self._context.instrument_identity(symbol)
            if identity in seen:
                continue
            seen.add(identity)
            if (d.get("reference_only") or d.get("level") == "gray"
                    or "price" in d and d["price"] is None):
                continue
            member = {"symbol": symbol, "note": d.get("note") or "", "group": d.get("group") or "",
                      # hold 只给持仓组用：统计面板要在名单里标「长持仓 / 短持仓」
                      "hold": str(d.get("hold") or ""),
                      "level": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else ""}
            marks = d.get("signal_marks") or []
            if any(kind == "boll_rsi" for kind, _ in marks) or self._context._text_only_kind(d.get("signals")) == "boll_rsi":
                marks = [(kind, level) for kind, level in marks if kind not in ("boll", "rsi")]
            for kind in {k for k, _ in marks}:
                if kind not in out or kind in ("ma", "target"):
                    continue
                for level in levels:
                    if any(k == kind and m == level for k, m in marks):
                        entry = dict(member)
                        # 加减仓价：名单里把价位本身写出来，光看代码不知道挂在哪一档。
                        # 用「加 325」这套跟个股行胶囊一致的缩写，不再写「加仓价」长名。
                        if kind in ("add_price", "reduce_price"):
                            price = self._context._level_num(d.get("trigger" if kind == "add_price" else "reduce"))
                            if price:
                                entry["level_price"] = ("加" if kind == "add_price" else "减") + f" {price}"
                        # 组合信号现在红黄两档都有，方向说明不能只给红
                        if kind == "boll_rsi" and level in ("red", "yellow") and d.get("boll_rsi_direction"):
                            entry["direction"] = d["boll_rsi_direction"]
                            if type(d.get("boll_rsi_hits")) is int and d["boll_rsi_hits"] in (2, 3):
                                entry["boll_rsi_hits"] = d["boll_rsi_hits"]
                        if kind != "target" or level == "purple":
                            self._context._member_signal_details(d, kind, entry, level)
                        out[kind][level].append(entry)
        out["target"]["purple"] = self._context._target_overlay_union(self._context.target_overlay_members(items))
        return out


    def earnings_week_members(self, items, today):
        """独立信息名单，复用首页分类优先级去重，不进入警示信号或灯级。"""
        priority = {group["item_group"]: index for index, group in enumerate(self._context.GROUPS)}
        seen = set(self._context.RISK_IDENTITIES)
        members = []
        for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(self._context.GROUPS))):
            symbol = self._context.normalize_symbol(d.get("symbol"))
            identity = self._context.instrument_identity(symbol)
            if identity in seen:
                continue
            seen.add(identity)
            if (d.get("reference_only") or d.get("etf") or self._context.is_etf(symbol)
                    or not self._context.earnings_within_list(d.get("earnings"), today)):
                continue
            e = d["earnings"]
            members.append({"symbol": symbol, "note": d.get("note") or "", "group": d.get("group") or "",
                            "level": d.get("level") or "", "hold": str(d.get("hold") or ""), "date": e["date"],
                            "timing": e.get("timing") or "", "kind": e.get("kind") or ""})
        return sorted(members, key=lambda m: (m["date"], priority.get(m["group"], len(self._context.GROUPS)), m["symbol"]))


    def _sanitize_snapshot_fields(self, value):
        """浅复制并过滤已知退役键；其它字段保持原值。"""
        out = {key: item for key, item in value.items() if key not in self._context._RETIRED_SNAPSHOT_KEYS}
        if "signal_marks" in out:
            out["signal_marks"] = [mark for mark in out["signal_marks"] or [] if mark[0] != "fund"]
            retired_amp = any(kind == "amp" and level == "gray" for kind, level in out["signal_marks"])
            if retired_amp:
                out["signal_marks"] = [mark for mark in out["signal_marks"] if mark != ("amp", "gray") and mark != ["amp", "gray"]]
                if not any(kind == "amp" and level in ("red", "yellow") for kind, level in out["signal_marks"]):
                    out["signals"] = [text for text in out.get("signals") or []
                                      if not isinstance(text, str) or not text.startswith("振幅 ")]
                # 旧振幅灰曾强制不折叠；仅按已保存技术事实恢复资格，不回查行情或重判振幅。
                texts = [text for text in out.get("signals") or [] if isinstance(text, str)]
                selected = out.get("boll_alert_text") or next(
                    (text for text in texts if text.startswith(self._context._TEXT_BOLL_PREFIX)), "")
                side = ("上轨" if "上轨" in selected else "下轨" if "下轨" in selected else None) if isinstance(selected, str) else None
                hits = {"超买": 0, "超卖": 0}
                for text in texts:
                    match = re.match(r"^RSI ([123])条\s*[↑↓]?(超买|超卖)", text)
                    if match:
                        hits[match[2]] = max(hits[match[2]], int(match[1]))
                facts = self._context.classify_technical_facts(out.get("group"), hits["超买"], hits["超卖"], side)
                technical_marks = {kind for kind, _ in out["signal_marks"] if kind in ("rsi", "boll", "boll_rsi")}
                known = isinstance(selected, str) and (not selected or side is not None) and all(
                    not text.startswith("RSI") or re.match(r"^RSI ([123])条\s*[↑↓]?(超买|超卖)", text)
                    for text in texts)
                if facts and known and (not technical_marks or technical_marks == {facts["kind"]}):
                    out["foldable"] = facts["foldable"]
        if "symbol" in out and any(key in out for key in ("target", "purple", "signal_marks", "signals")):
            out = self._context._apply_target(out)
        return out


    def _merge_ma_snapshot(self, snapshot):
        """兼容旧双均线快照；名单可核验才计算并集，缺名单时不猜重叠数。"""
        counts = snapshot.get("signal_counts") or {}
        members = snapshot.get("signal_members") or {}
        keys = [kind for kind in ("ma", "ma_cross", "ma_band") if kind in counts or kind in members]
        if not keys or keys == ["ma"] and "ma" not in members:
            return snapshot
        candidates, valid = [], True
        for kind in keys:
            source_counts = counts.get(kind) or {}
            source_members = members.get(kind) or {}
            if not isinstance(source_counts, dict) or not isinstance(source_members, dict):
                valid = False
                continue
            for level in ("red", "yellow", "purple"):
                expected = source_counts.get(level, 0)
                entries = source_members.get(level, [])
                if (not isinstance(entries, list) or type(expected) is not int or expected < 0
                        or kind in counts and len(entries) != expected
                        or any(not isinstance(d, dict) or not isinstance(d.get("symbol"), str)
                               or not d["symbol"].strip() for d in entries)):
                    valid = False
                    continue
                candidates.extend((level, self._context._snapshot_ma_member(d, kind, snapshot.get("effective_settings") or {})) for d in entries)
        merged = self._context._merge_ma_members(candidates) if valid else {level: [] for level in ("red", "yellow", "purple")}
        ma_counts = {"label": "均线", **{level: len(merged[level]) if valid else None for level in merged}}
        if not valid:
            ma_counts["pending"] = True
        out = dict(snapshot)
        out["signal_members"] = {kind: value for kind, value in members.items() if kind not in keys}
        # 放回原均线的位置，不改变其它信号的展示顺序。
        out["signal_counts"] = {kind: (ma_counts if kind == "ma" else counts[kind])
                                for kind in dict.fromkeys("ma" if kind in keys else kind for kind in counts)}
        out["signal_counts"].setdefault("ma", ma_counts)
        out["signal_members"]["ma"] = merged
        return out


    def _snapshot_ma_member(self, entry, kind, settings):
        """旧均线成员补展示归属；数字原事实保留，不用当前设置解释历史周期。"""
        if not isinstance(entry, dict):
            return entry
        out = self._context._sanitize_snapshot_fields(entry)
        if kind not in ("ma", "ma_cross", "ma_band"):
            return out
        config = out.get("ma_settings") if isinstance(out.get("ma_settings"), dict) else settings
        out["ma_settings"] = {key: config.get(key) for key in ("ma_short", "ma_long")}
        if kind == "ma_band":
            markers = []
            for marker in out.get("indicators") or []:
                if not isinstance(marker, dict):
                    markers.append(marker)
                    continue
                label = str(marker.get("label") or "")
                if label.startswith(("现价在均线", "现价与均线")) and "日内触及" in label:
                    period = config.get("ma_long")
                    name = f"{period}日均线" if type(period) is int and period > 0 else "均线（归属待确认）"
                    label = f"日内触及{name}，" + label.replace("，日内触及", "")
                markers.append({**marker, "label": label})
            if markers:
                out["indicators"] = markers
        return out


    def _sanitize_snapshot(self, snapshot):
        """旧快照只在渲染副本中清理，原对象与磁盘保存快照不变。"""
        out = self._context._sanitize_snapshot_fields(snapshot)
        old_target_counts = (snapshot.get("signal_counts") or {}).get("target") or {}
        old_target_light = bool(old_target_counts.get("red") or old_target_counts.get("yellow"))
        for field in ("summary", "list_counts", "signal_counts", "effective_settings"):
            if isinstance(out.get(field), dict):
                out[field] = self._context._sanitize_snapshot_fields(out[field])
        if isinstance(out.get("signal_members"), dict):
            out["signal_members"] = {
                kind: {level: [self._context._snapshot_ma_member(entry, kind, out.get("effective_settings") or {})
                               for entry in entries] if isinstance(entries, list) else entries
                       for level, entries in members.items()} if isinstance(members, dict) else members
                for kind, members in out["signal_members"].items() if kind != "fund"
            }
        for field in ("items", "stocks", "text_only"):
            if isinstance(out.get(field), list):
                out[field] = [self._context._sanitize_snapshot_fields(item) if isinstance(item, dict) else item
                              for item in out[field]
                              if not (field == "text_only" and isinstance(item, dict) and item.get("kind") == "amp")]
                if field in ("items", "stocks"):
                    out[field] = [{**item, "ma_settings": item.get("ma_settings")
                                   if isinstance(item.get("ma_settings"), dict) else
                                   {key: (out.get("effective_settings") or {}).get(key) for key in ("ma_short", "ma_long")}}
                                  if isinstance(item, dict) else item for item in out[field]]
        rows = out.get("items") if isinstance(out.get("items"), list) else out.get("stocks")
        if isinstance(rows, list):
            out["text_only"] = self._context.text_only_members(rows)
            out["target_overlays"] = self._context.target_overlay_members(rows)
            if isinstance(out.get("summary"), dict):
                out["summary"].update({level: sum(d.get("level") == level for d in rows)
                                       for level in ("red", "yellow", "green", "gray")})
        overlays = out.get("target_overlays")
        union = self._context._target_overlay_union(overlays) if isinstance(overlays, dict) else []
        if isinstance(out.get("signal_counts"), dict):
            out["signal_counts"]["target"] = {"label": dict(self._context.SIGNAL_KINDS)["target"],
                                                "red": 0, "yellow": 0, "purple": len(union)}
        if isinstance(out.get("signal_members"), dict):
            out["signal_members"]["target"] = {"red": [], "yellow": [], "purple": union}
        if isinstance(out.get("summary"), dict):
            out["summary"]["purple"] = len(union)
            if old_target_light and not isinstance(rows, list):
                for level in ("red", "yellow"):
                    entries = out.get("signal_members") or {}
                    counts = out.get("signal_counts") or {}
                    if isinstance(entries, dict) and counts and all(
                            not value.get(level) or isinstance(entries.get(kind), dict)
                            and isinstance(entries[kind].get(level), list)
                            and len(entries[kind][level]) == value[level]
                            for kind, value in counts.items() if kind != "target"):
                        out["summary"][level] = len({self._context.instrument_identity(d["symbol"])
                            for kind, levels in entries.items() if kind != "target" and isinstance(levels, dict)
                            for d in levels.get(level) or [] if isinstance(d, dict) and d.get("symbol")})
                    else:
                        out["summary"][level] = None
        if isinstance(rows, list) and isinstance(out.get("signal_counts"), dict):
            ma_members = self._context.signal_count_members(rows)["ma"]
            out["signal_counts"] = {kind: value for kind, value in out["signal_counts"].items()
                                    if kind not in ("ma_cross", "ma_band")}
            out["signal_counts"]["ma"] = {"label": "均线", **{level: len(entries) for level, entries in ma_members.items()}}
            out["signal_members"] = {**(out.get("signal_members") or {}), "ma": ma_members}
            for kind in ("ma_cross", "ma_band"):
                out["signal_members"].pop(kind, None)
        return self._context._merge_ma_snapshot(out)


    def build_snapshot(self, macro, items, cfg, group_counts=None):
        ma_members = self._context.signal_count_members(items)["ma"]
        priority = {g["item_group"]: i for i, g in enumerate(self._context.GROUPS)}
        unique = {}
        for item in sorted(items, key=lambda d: priority.get(d.get("group"), len(self._context.GROUPS))):
            identity = self._context.instrument_identity(item["symbol"])
            if identity not in self._context.RISK_IDENTITIES:
                unique.setdefault(identity, item)
        items = [self._context._sanitize_snapshot_fields(item) for item in unique.values()]
        dates = sorted({d.get("data_date") for d in items if d.get("data_date") and not d.get("reference_only")})
        references = {self._context.normalize_symbol(d["symbol"]): d["data_date"] for d in items
                      if d.get("reference_only") and d.get("data_date")}
        unsupported = [self._context.normalize_symbol(d["symbol"]) for d in items if not self._context.quote_supported(d["symbol"])]
        missing = [self._context.normalize_symbol(d["symbol"]) for d in items
                   if d.get("price") is None and self._context.quote_supported(d["symbol"])]
        stale = [self._context.normalize_symbol(d["symbol"]) for d in items
                 if not d.get("reference_only") and d.get("data_date") and self._context.TARGET_DATE and d["data_date"] < self._context.TARGET_DATE]
        monitoring = self._context.group_monitoring(cfg)
        positions = cfg.get("positions", {}) if monitoring["positions"] else {}
        counts = group_counts if group_counts is not None else self._context.grouped_universe(cfg)[1]
        dca = self._context.global_dca(self._context.TARGET_DATE)
        earnings_asof = self._context._bj_today()
        snapshot = {
            "dca_reminder": dca,
            "run_id": str(os.environ.get("GITHUB_RUN_ID") or "local-" + self._context.NOW.strftime("%Y%m%d%H%M%S")),
            "request_id": os.environ.get("MM_REQUEST_ID", ""),
            **self._context.build_identity(),
            "started_at": os.environ.get("MM_STARTED_AT", self._context.NOW.isoformat()),
            "started_at_bj": self._context.beijing_iso(os.environ.get("MM_STARTED_AT", self._context.NOW.isoformat())),
            "finished_at": self._context.datetime.now(self._context.TZ).isoformat(),
            "finished_at_bj": self._context.datetime.now(self._context.TZ).isoformat(timespec="seconds"),
            "event": self._context.EVENT,
            "target_trade_date": self._context.TARGET_DATE,
            "mode": "closed" if self._context.CLOSED_ONLY else "manual_or_config",
            "schedule": "美东周一至周五20:15；北京时间夏季次日08:15、冬季次日09:15",
            "config_files": {f: self._context.config_hash(f) for f in ("holdings.json", "settings.json")},
            "effective_settings": self._context._sanitize_snapshot_fields(self._context.S),
            "groups": self._context.GROUPS,
            "group_monitoring": monitoring,
            "registered_counts": {g["key"]: len(cfg.get(g["key"]) or {}) for g in self._context.GROUPS},
            "list_counts": {**counts,
                            "triggers": sum(bool(c.get("trigger")) for c in positions.values()),
                            "reduces": sum(bool(self._context.reduce_rule(c)) for c in positions.values()),
                            "dca": 1 if dca else 0},
            "summary": {**{lv: sum(d["level"] == lv and (lv not in ("red", "yellow")
                                  or not d.get("reference_only") and ("price" not in d or d["price"] is not None))
                                  for d in items) for lv in ("red", "yellow", "green", "gray")},
                        "purple": self._context.purple_counts(items),
                        "total": len(items), "macro_ok": sum(bool(m.get("ok")) for m in macro.values()),
                        "stale_symbols": stale, "missing_symbols": missing,
                        "unsupported_symbols": unsupported, "reference_dates": references,
                        "today_prices": sum(not d.get("reference_only") and d.get("price") is not None and d.get("data_date") == self._context.TARGET_DATE for d in items),
                        "prior_prices": sum(not d.get("reference_only") and d.get("price") is not None and d.get("data_date") != self._context.TARGET_DATE for d in items),
                        "missing_prices": sum(d.get("price") is None for d in items)},
            "signal_counts": self._context.signal_counts(items),
            "signal_members": self._context.signal_count_members(items),
            "target_overlays": self._context.target_overlay_members(items),
            "text_only": self._context.text_only_members(items),
            "earnings_week": {"asof": earnings_asof, "members": self._context.earnings_week_members(items, earnings_asof)},
            "actual_dates": {"min": dates[0] if dates else "", "max": dates[-1] if dates else ""},
            "macro_dates": {k: m.get("date", "") for k, m in macro.items()},
            "treasury_reference": {key: macro.get("ust10", {}).get(key) for key in
                                   ("ok", "value", "date", "source", "unit", "lagging", "reason", "quote_name", "observed_date", "observed_value", "delta_month")},
            "coverage": "数据源日线；完整23小时夜盘/日盘覆盖尚未验证。自动日报不并入实时价；手动常规盘中按当前报价重算。",
        }
        snapshot["signal_members"]["ma"] = ma_members
        snapshot["signal_counts"]["ma"] = {"label": "均线", **{level: len(entries) for level, entries in ma_members.items()}}
        snapshot["data_time_text"] = self._context.market_data_time(snapshot)
        snapshot["market_risk"] = self._context.market_risk_summary(macro)
        snapshot["risk_quotes"] = {key: {field: (macro.get(key) or {}).get(field) for field in
                                 ('ok', 'value', 'date', 'source', 'drawdown')} for key in ('vix', 'sp500')}
        return snapshot
