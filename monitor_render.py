# -*- coding: utf-8 -*-
"""HTML 渲染，仅允许读取资源、不联网或写文件；构造只保存运行依赖，context 仅兼容旧入口，不增加状态。"""
import hashlib
import html as html_lib
import json
import math
import os
import re
from datetime import timedelta
from monitor_rules import _ARROW_RSI_UP, _ARROW_RSI_DOWN

# 「强制刷新」按钮：清掉 Cache Storage / Service Worker，再带时间戳参数重新加载，绕开浏览器和 CDN 缓存
HARD_REFRESH_JS = (
    "(function(){var b=document.getElementById('btnHardRefresh');if(!b)return;"
    "b.onclick=async function(){b.disabled=true;b.textContent='刷新中…';"
    "try{if(window.caches){var ks=await caches.keys();await Promise.all(ks.map(function(k){return caches.delete(k);}));}"
    "if(navigator.serviceWorker){var rs=await navigator.serviceWorker.getRegistrations();"
    "await Promise.all(rs.map(function(r){return r.unregister();}));}}catch(e){}"
    "var u=new URL(location.href);u.hash='';u.searchParams.delete('focus');u.searchParams.delete('edit');"
    "u.searchParams.set('_mm_reset_view','1');u.searchParams.set('_r',String(Date.now()));location.replace(u.toString());};})();"
)


# 个股/ETF 表统一用固定列宽，三张表（警示 / ETF / 折叠的无异动）栏位才能上下对齐
STK_COLS = ('<colgroup><col class="c-sym"><col class="c-earn"><col class="c-px"><col class="c-sig"></colgroup>')


# 表头已于 2026-10-15 按用户要求整行删除：各行自己写「美东 10/22 盘后」，不需要「名称｜财报」提示
STK_HEAD = ''


# 方向提到「框标题」上的栏目：名单里不再逐条画三角，▲/▼ 只出现在每个框的标题里。
# 量比按涨跌、布林+RSI 按上下轨、均线穿越按上穿/跌破、长均线触及按现价在均线哪一侧。
# 只改这几栏——涨跌幅、振幅等栏目行内三角照旧，别把没要求的栏目一起改了。
# 所有按方向分组的栏目都分框展示：三角只出现在框标题（也就是名单最前面），
# 单独指标和组合条件同一套位置、同一套大小，不再一个在名称前一个在名称后。
_BLOCK_PANEL_KINDS = ("vol_ratio", "boll_rsi", "ma", "ma_cross", "ma_band",
                      "rsi", "boll", "chg", "amp")


# 框标题已经写明方向的栏目，行尾不再重复同一句话（布林+RSI 曾经标题行尾都写
# 「上轨▲ · RSI 2条 ↑超买」）。量比那种带数值的照旧保留。
_NO_ROW_METRIC_KINDS = ("boll_rsi", "rsi")


# 主表描述与统计面板：▲/↑ 使用上涨数值绿，▼/↓ 使用下跌数值红。
# 箭头独立着色，正文自身的指标灯色不变；规则表使用自己的 ar-* 类。
_ARROW_CLASSES = (("▲", "arrow-band-up"), ("▼", "arrow-band-down"),
                  (_ARROW_RSI_UP, "arrow-rsi-up"), (_ARROW_RSI_DOWN, "arrow-rsi-down"))


# 描述分类只用于排版，灯色只认对应指标 marks，不从整行灯色或文案猜灯。
_SIGNAL_DESCRIPTION_RULES = (
    ("rsi", re.compile(r"^RSI\b")),
    ("boll", re.compile(r"^(?:盘中突破|突破|跌破|触及|逼近)布林[上下]轨")),
    ("boll_rsi", re.compile(r"^布林\s*\+\s*RSI")),
    ("chg", re.compile(r"^(?:异动|波动)\s*[+-]?\d")),
    ("amp", re.compile(r"^(?:日内)?振幅")),
    ("vol_ratio", re.compile(r"^(?:盘中|最近完整日)?量比")),
    ("ma", re.compile(r"^(?:日内触及|现价贴近|贴近|触及|上穿|跌破)\d+日(?:弱)?均线")),
    ("low52", re.compile(r"^(?:触及52周新低|仍在52周低位|距52周低点)")),
    ("add_price", re.compile(r"^(?:已跌破|距)加仓价")),
    ("reduce_price", re.compile(r"^(?:已涨到|距)减仓价")),
    ("target", re.compile(r"^(?:距目标价|目标价|手填目标价|今日上穿目标价|今日跌破目标价|在目标价)")),
)


POSITION_ZONES = (("long", "长持仓"), ("short", "短持仓"))


_GRAY_RSI_RE = re.compile(r"RSI ([123])条\s*[↑↓]?(超买|超卖)")


def _reverse_rsi_description(text, side):
    """只为与选中布林轨反向的RSI显示事实加（反），不修改判灯或原始文本。"""
    if side not in ("上轨", "下轨"):
        return text
    opposite = "超卖" if side == "上轨" else "超买"

    def mark(match):
        value = match[0]
        if match[1] != opposite or value.endswith("（反）"):
            return value
        return value + "（反）"

    return re.sub(r"RSI\s+[123]条\s*[↑↓]?(超买|超卖)(?:（[^）]*）)*", mark, text)


# ---------------------------------------------------------------- HTML

def fmt(v, unit="", nd=2):
    if v is None:
        return "—"
    return f"{v:,.{nd}f}{unit}"


def _block_head(shape, title):
    """框标题：文案里已经写了方向三角（如「上轨▲ · RSI 2条 ↑超买」）就不再重复前缀三角。"""
    title = str(title or "")
    return title if shape and shape in title else f"{shape} {title}".strip()


def _count_badge(level, count, label):
    """灯级计数徽章：数字直接放在灯色圆里（比「圆点 + 数字」省版面，手机上一眼看到数）。"""
    return (f'<span class="stat-badge {level}" aria-label="{html_lib.escape(label)} {count} 项">'
            f'{count}</span>')


def _collapse_button(card_id, label):
    """只显示小三角，保留触控区域及读屏名称。"""
    card_id = html_lib.escape(str(card_id), quote=True)
    label = html_lib.escape(str(label), quote=True)
    return (f'<div class="group-bottom"><button type="button" class="collapse-group" '
            f'aria-controls="{card_id}" aria-label="收起{label}">'
            f'<span class="collapse-icon" aria-hidden="true">▲</span></button></div>')


def _position_zone(d):
    """持仓内部按 hold 分两区；没标的一律算短持仓，规则不缩水。"""
    return "long" if str(d.get("hold") or "short").lower() == "long" else "short"


def _run_light(snapshot, finished_txt):
    """首页状态灯（第三排）：个股行情失败、市场风险部分缺失、空清单或完整成功。"""
    summary = snapshot.get("summary", {})
    registered = snapshot.get("registered_counts", {})
    # 无数据状态改叫「缺失」，避免和统计面板里「只出文字不报灯」的白灯撞名
    # （具体文案在下面按「有没有异常」拼，不再单独拼一段）
    bad = list(dict.fromkeys(summary.get("stale_symbols", []) + summary.get("missing_symbols", [])))
    if bad:
        reason = "取数失败" if not summary.get("stale_symbols") else "当日行情未取得"
        return "bad", (f"{reason} {len(bad)} 只：{html_lib.escape('、'.join(bad[:6]))}"
                       + (" 等" if len(bad) > 6 else ""))
    total = summary.get("total", 0)
    unsupported_count = len(summary.get("unsupported_symbols", []))
    if not total:
        has_registered = any(registered.values())
        return "idle", ("没有开启的监测标的，未抓取报价" if has_registered else "清单为空，未抓取报价")
    if unsupported_count >= total:
        return "idle", f"清单中 {unsupported_count} 个特殊代码暂不支持报价，未抓取报价"
    risk = snapshot.get("market_risk") or {}
    missing_risk = risk.get("missing") or []
    kind = {"schedule": "自动", "workflow_dispatch": "手动", "push": "保存后"}.get(snapshot.get("event"), "")
    # 仅列出缺失或不支持的项目，不重复显示完整数据的已更新计数。
    deficits = []
    if summary.get("gray"):
        deficits.append(f"缺失 {html_lib.escape(str(summary.get('gray')))}")
    if unsupported_count:
        deficits.append(f"{unsupported_count} 个特殊代码暂不支持报价")
    if isinstance(missing_risk, list) and missing_risk:
        names = "、".join(html_lib.escape(str(name)) for name in missing_risk)
        valid = risk.get("valid_count", 5 - len(missing_risk))
        parts = [f"{kind}运行已完成",
                 f"市场风险数据缺失：{names}（有效指标 {html_lib.escape(str(valid))}/5）"]
        parts.extend(deficits)
        parts.append(f"完成于 {finished_txt}（北京时间）")
        return "partial", " · ".join(parts)
    # 正常只报「成功 ·YYYY-MM-DD HH:MM（北京）」：统计卡里已有一份计数，
    # 再写「保存后抓取成功 · 完成于 …」是废话，手机上还会被撑成两行（2026-10-27）。
    # 括号里只写「北京」不写「北京时间」：手机上这排要跟「已生效」挨着，省两个字（2026-10-29）。
    parts = [f"{finished_txt}（北京）"] + deficits
    return "ok", "成功 ·" + " · ".join(parts)


def _gray_by_kind(snapshot):
    """白灯成员仅按 rsi / boll / boll_rsi 归类。"""
    rows = snapshot.get("text_only") or []
    out = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        if (isinstance(r, dict) and str(r.get("kind") or "") in ("rsi", "boll", "boll_rsi")
                and isinstance(r.get("symbol"), str) and r["symbol"].strip()):
            out.setdefault(r["kind"], []).append(r)
    return out


class PageRenderer:
    def __init__(self, context):
        self._context = context

    def dca_badge(self, d):
        """顶栏最右侧的短提醒「定投：N」（N=距定投日还差几个美股交易日）；仅定投日当天黄字。
    完整日期放读屏标签；天数缺失或异常时不显示，避免给出错误天数。"""
        if not isinstance(d, dict):
            return ""
        n = d.get("next_in")
        if type(n) is not int or n < 0:
            return ""
        due = d.get("due") is True and not d.get("pending")
        try:
            detail = f"定投提醒：{self._context.dca_text(d)}"
        except (KeyError, TypeError):
            detail = "定投提醒"
        return (f'<span class="dca-badge{" on" if due else ""}" '
                f'aria-label="{html_lib.escape(detail, quote=True)}">定投：{n}</span>')


    def level_note(self, trigger, reduce):
        """标的名称下面的小标签：加仓价 = 橙色「加 325」，减仓价 = 青色「减 400」，都有就并排；都没有返回空串。
    样式是空心胶囊，和 ETF 的实心蓝色小方标签区分开。"""
        parts = []
        t, r = self._context._level_num(trigger), self._context._level_num(reduce)
        if t:
            parts.append(f'<span class="lvtag add">加 {t}</span>')
        if r:
            parts.append(f'<span class="lvtag cut">减 {r}</span>')
        if not parts:
            return ""
        return f'<span class="lvs">{"".join(parts)}</span>'


    def target_tag(self, t):
        """目标标签：低/中/高空心，达到实心，失败灰虚线且不显示旧空间。"""
        if not t:
            return ""
        state = self._context.target_display_state(t)
        cls = "tgt " + {"failed": "bad", "reached": "hit", "high": "high",
                        "space": "space", "low": "low"}[state]
        parts = []
        if state != "failed":
            # 30/70边界附近保留小数，避免69.9%被四舍五入显示为高档70%。
            space_text = f'{t["space"]:+.1f}'.rstrip('0').rstrip('.')
            parts.append(f'<span>{space_text}%</span>')
        if self._context._pos_num(t.get("price")) is not None:
            parts.append(f'<span>{self._context._price_text(t["price"])}</span>')
        if state != "failed" and t.get("month"):
            parts.append(f'<span>· {html_lib.escape(str(t["month"]))}月</span>')
        if state == "failed":
            parts.append('<span>获取失败</span>')
        body = " ".join(parts)
        return f'<span class="tgs"><span class="lvtag {cls}">{body}</span></span>'


    def _grp_arrow(self, shape):
        """分组方向符号与正文箭头一致：▲/↑ 涨跌绿，▼/↓ 涨跌红。"""
        if shape == "▲":
            return '<span class="grp-arrow arrow-band-up">▲</span>'
        if shape == "▼":
            return '<span class="grp-arrow arrow-band-down">▼</span>'
        if shape == self._context._ARROW_RSI_UP:
            return f'<span class="arrow-rsi-up">{self._context._ARROW_RSI_UP}</span>'
        if shape == self._context._ARROW_RSI_DOWN:
            return f'<span class="arrow-rsi-down">{self._context._ARROW_RSI_DOWN}</span>'
        return html_lib.escape(str(shape)) if shape else ""   # 均线「■」之类原样保留，不丢三角


    def _shape_rank(self, shape):
        return self._context._SHAPE_RANK.get(shape, 2)


    def _block_title_html(self, kind, shape, title):
        """框标题和正文方向符号统一独立着色，标题文字本身保留原颜色。"""
        head = self._context._block_head(shape, title)
        if kind not in ("boll", "rsi", "boll_rsi", "chg", "amp", "vol_ratio",
                        "ma", "ma_cross", "ma_band"):
            return html_lib.escape(head)
        prefix = f"{shape} " if shape and head.startswith(f"{shape} ") else ""
        lead = self._context._grp_arrow(shape) + " " if prefix else ""
        return lead + self._context.arrow_html(html_lib.escape(head[len(prefix):]))


    def arrow_html(self, escaped_text):
        """给已转义文本里的方向符号套上配色 span；输入必须是转义后的文本。"""
        text = str(escaped_text)
        for char, cls in self._context._ARROW_CLASSES:
            if char in text:
                text = text.replace(char, f'<span class="{cls}">{char}</span>')
        return text


    def _member_block(self, d, kind):
        """分框栏目里这一行归到哪个框，返回 (图标, 框标题)。

    量比按当天股价涨跌分上下；布林+RSI 按轨道 + RSI 条数分上下；
    方向算不出来时给「平盘 / 方向待下次更新」，不硬塞进 ▲ 或 ▼。"""
        if kind == "vol_ratio":
            # 名单里存的是已经算好的方向标记，不回查 chg（成员字典里没有行情字段）
            for marker in d.get("indicators") or []:
                if isinstance(marker, dict) and marker.get("shape") in ("▲", "▼"):
                    return (marker["shape"], "上涨" if marker["shape"] == "▲" else "下跌")
            return ("", "平盘")   # 平盘或旧快照缺方向
        if kind == "boll_rsi":
            state = str(d.get("direction") or "")
            hits = d.get("boll_rsi_hits")
            side = next((s for s in ("上轨", "下轨") if state.startswith(s)), "")
            if side and type(hits) is int and hits in (2, 3):
                return ("▲" if side == "上轨" else "▼", self._context.boll_rsi_text(state, hits))
            return ("", "方向待下次更新")
        if kind == "ma":
            settings = d.get("ma_settings")
            markers = [m for m in d.get("indicators") or [] if isinstance(m, dict)
                       and m.get("shape") in ("▲", "▼", "■") and m.get("label")]
            if not markers:
                facts = [f.get("text") for f in d.get("ma_facts") or []
                         if isinstance(f, dict) and isinstance(f.get("text"), str)]
                return ("", self._context._ma_stat_text(facts, settings) or "方向待下次更新")
            shapes = {m["shape"] for m in markers}
            labels = []
            for m in markers:
                label = self._context._ma_stat_label(m["label"], settings)
                labels.append((f'{m["shape"]} ' if len(shapes) > 1 else "") + label)
            return (markers[0]["shape"] if len(shapes) == 1 else "", "，".join(dict.fromkeys(labels)))
        if kind in ("ma_cross", "ma_band"):
            # 旧成员仍可读取；对外统计统一使用 ma。
            for marker in d.get("indicators") or []:
                if not (isinstance(marker, dict) and marker.get("shape") in ("▲", "▼", "■")):
                    continue
                label = str(marker.get("label") or "")
                match = re.match(r"^(上穿|跌破)(\d+)日均线$", label)
                if match:
                    # 标题说「250日均线上穿」，跟用户口头说法一致
                    label = f"{match.group(2)}日均线{match.group(1)}"
                return (marker["shape"], self._context._ma_display_text(label, d.get("ma_settings")) or ("均线穿越" if kind == "ma_cross" else "触及长均线"))
            return ("", "方向待下次更新")
        if kind in ("chg", "amp"):
            for marker in d.get("indicators") or []:
                if isinstance(marker, dict) and marker.get("shape") in ("▲", "▼"):
                    return (marker["shape"], "上涨" if marker["shape"] == "▲" else "下跌")
            return ("", "平盘")
        if kind == "rsi":
            texts = [str(x) for x in (d.get("signals") or []) if isinstance(x, str)]
            texts += [str(m.get("label")) for m in (d.get("indicators") or [])
                      if isinstance(m, dict) and m.get("label")]
            hits = d.get("rsi_hits")
            if isinstance(hits, str) and hits:
                texts.append(hits if hits.startswith("RSI") else f"RSI {hits}")
            for text in texts:
                match = re.search(r"RSI\s*([123])条\s*[↑↓]?(超买|超卖)", text)
                if match:
                    n, rs = int(match[1]), match[2]
                    return (self._context._ARROW_RSI_UP if rs == "超买" else self._context._ARROW_RSI_DOWN, self._context.rsi_hits_text(n, rs))
            return ("", "条数待下次更新")
        if kind == "boll":
            text = str(d.get("boll_alert_text") or d.get("boll_state") or "")
            if not text:
                text = next((str(x) for x in (d.get("signals") or [])
                             if isinstance(x, str) and "布林" in x), "")
            side = "上轨" if "上轨" in text else "下轨" if "下轨" in text else ""
            if side:
                return ("▲" if side == "上轨" else "▼",
                        self._context._member_metric_text(d, "boll") or self._context.boll_side_text(side))
            return ("", "状态待下次更新")
        return None


    def _member_belong(self, d):
        """名单里的分组标识，写在代码后面；持仓内部按 hold 分长短，跟首页两区一致。"""
        group = str(d.get("group") or "")
        if group == "position":
            return "长持仓" if str(d.get("hold") or "short").lower() == "long" else "短持仓"
        return self._context.GROUP_LABELS.get(group) or "未分类"


    def _member_metric_text(self, d, kind):
        """成员行里跟在代码后面的数值；只带本轮已算出来的值，缺就留空不编。"""
        if kind in ("chg", "amp", "vol_ratio"):
            return d.get("metric") or "数值待下次更新"
        if kind == "rsi":
            return d.get("rsi_hits") or "条数待下次更新"
        if kind == "boll":
            return d.get("boll_state") or "状态待下次更新"
        if kind == "boll_rsi":
            # 轨道方向要写成可见文字（手机没有悬停提示），不能只靠箭头上的 title
            state = str(d.get("direction") or "")
            hits = d.get("boll_rsi_hits")
            side = next((s for s in ("上轨", "下轨") if state.startswith(s)), "")
            if side and type(hits) is int and hits in (2, 3):
                return self._context.boll_rsi_text(state, hits)
            return "条数待下次更新"
        if kind == "low52":
            # 52周低位把低点价本身写出来：只说「距低点 +2.6%」看不见价格
            price = d.get("low52_price")
            return f"52周低 {price}" if price else ""
        if kind in ("add_price", "reduce_price"):
            return d.get("level_price") or ""
        return ""


    def _macro_rows(self, macro, risk):
        """市场风险参考表的各行（五项指标 + 10Y 美债参考行）。"""
        rows_macro = []
        for key in self._context.RISK_INDICATORS:
            it = macro.get(key, {})
            if risk["levels"][key] == "gray":
                label = html_lib.escape(str(it.get("name") or self._context.RISK_LABELS[key]))
                rows_macro.append(f'<tr class="gray"><td>{label}</td><td colspan="3">无数据或已过期</td></tr>')
                continue
            lv, txt = self._context.macro_status(key, it)
            if key == "sp500":
                # 2026-10-30：「较近一年高点」六个字删掉，只留「回撤 -1.3%」——同一格还有点位，
                # 前缀太长会把数字挤到第二行，行高也压不下来。
                val = f'{it["value"]:,.2f}<span class="unit">回撤 {it["drawdown"]:.1f}%</span>' if it.get("drawdown") is not None else "—"
            elif key == "hy_oas":
                val = f'{it["value"]:.0f} bp'
            elif key == "vix":
                val = f'{it["value"]:.2f}'
            elif key == "ust10":
                val = f'{it["value"]:.2f}%'
            elif key == "curve":
                val = f'{it["value"]:.2f}<span class="unit">个百分点</span>'
            elif key == "breadth":
                # 2026-10-30：「上宏观」三字删掉（长／短均线本身就是宏观口径，不必重复）
                val = (f'{it["value"]:.1f}%<span class="unit">长均线</span>'
                       f'<span class="unit">短均线：{it["pct50"]:.1f}%</span>')
            elif key == "nfci":
                val = f'{it["value"]:+.2f}'
            else:
                val = f'{it["value"]:.2f}'

            dw = it.get("delta_week")
            dw_txt = f'{dw:+.0f}' if (key == "hy_oas" and dw is not None) else "—"
            label = html_lib.escape(str(it.get("name") or self._context.RISK_LABELS[key]))
            # 2026-10-30：「截至」二字删掉，只留日期（单独一行小字，谁都看得出是数据日期）
            label += f'<span class="unit">{html_lib.escape(it["date"])}</span>'
            # 数据来源（FRED / Cboe / Yahoo …）按 2026-10-15 要求不再在指标行里展示，页底仍保留整体来源说明
            rows_macro.append(
                f'<tr class="{lv}"><td>{label}</td><td class="num">{val}</td>'
                f'<td class="num dim">{dw_txt}<span class="unit">周变化</span></td><td>{txt}</td></tr>'
            )

        # 参考行：10Y 美债一个月变化。只看利率冲击，不进 RISK_INDICATORS，不影响综合灯和有效指标数
        ut = macro.get("ust10") or {}
        if ut.get("ok") and ut.get("delta_month") is not None:
            ulv, utxt = self._context.macro_status("ust10", ut)
            uw = ut.get("delta_week")
            uw_txt = f'{uw * 100:+.0f}' if uw is not None else "—"
            ulabel = (f'{html_lib.escape(str(ut.get("name") or "10Y美债"))}'
                      f'<span class="unit">参考</span>'
                      f'<span class="unit">{html_lib.escape(ut["date"])}</span>')
            rows_macro.append(
                f'<tr class="{ulv}" id="ust10Ref"><td>{ulabel}</td><td class="num">{ut["value"]:.2f}%</td>'
                f'<td class="num dim">{uw_txt}<span class="unit">周变化 bp</span></td><td>{utxt}</td></tr>')
        else:
            rows_macro.append('<tr class="gray" id="ust10Ref"><td>10Y美债<span class="unit">参考</span></td>'
                              '<td colspan="3">无数据或已过期</td></tr>')
        # 纳斯达克指数参考行（2026-10-30）：纯展示点位与周变化，不带灯级、不计分。
        # 指标格沿用 10Y美债的写法：「纳斯达克指数 + 参考 + 日期」三行小字。
        nd = macro.get("nasdaq") or {}
        nd_val = self._context._pos_num(nd.get("value"))
        nd_week = nd.get("delta_week")
        nd_week = (nd_week if isinstance(nd_week, (int, float)) and not isinstance(nd_week, bool)
                   and nd_week == nd_week and nd_week not in (float("inf"), float("-inf")) else None)
        if nd.get("ok") is True and nd_val is not None:
            ndw_txt = f'{nd_week:+,.0f}' if nd_week is not None else "—"
            rows_macro.append(
                f'<tr id="nasdaqRef"><td>纳斯达克指数<span class="unit">参考</span>'
                f'<span class="unit">{html_lib.escape(str(nd.get("date") or ""))}</span></td>'
                f'<td class="num">{nd_val:,.2f}</td>'
                f'<td class="num dim">{ndw_txt}<span class="unit">周变化</span></td><td>参考</td></tr>')
        else:
            rows_macro.append('<tr class="gray" id="nasdaqRef"><td>纳斯达克指数<span class="unit">参考</span></td>'
                              '<td colspan="3">无数据或已过期</td></tr>')
        return "".join(rows_macro)


    def _sort_key(self, d):
        return (self._context._LEVEL_ORDER[d["level"]], -(abs(d["chg"]) if d["chg"] else 0))


    def _dedupe_items(self, items):
        """同一只标的只留一行：按分组优先级（风险参考／持仓优先，其次重点关注，最后其他）保留第一次出现的。"""
        priority = {g['item_group']: i for i, g in enumerate(self._context.GROUPS)}
        seen = set(self._context.RISK_IDENTITIES)
        unique_items = []
        for item in sorted(items, key=lambda d: priority.get(d.get('group'), len(self._context.GROUPS))):
            identity = self._context.instrument_identity(item['symbol'])
            if identity not in seen:
                seen.add(identity)
                unique_items.append(item)
        return sorted(unique_items, key=self._context._sort_key)


    def _row_tags(self, d, snapshot):
        """名称旁的小标签：ETF、无数据、参考值、日线日期。"""
        # ETF / 基金类标一个小标签，和个股区分开；抓不到数据时用静态表兜底判断
        tag = '<span class="tag">ETF</span>' if (d.get("etf") or self._context.is_etf(d["symbol"])) else ""
        if d.get("price") is None:
            tag += '<span class="tag quote-note">无数据</span>'
        elif d.get("reference_only"):
            date = html_lib.escape(str(d.get("data_date") or "日期未知"))
            tag += f'<span class="tag quote-note">参考值 · 截至 {date}</span>'
            if d.get("source") and d["source"] != "市场参考":
                tag += f'<span class="unit">来源 {html_lib.escape(str(d["source"]))} · 收益率百分比</span>'
            if d.get("reference_lagging"):
                tag += '<span class="unit">来源尚未提供目标日有效值，保留最近参考值</span>'
        elif snapshot.get("mode") == "manual_or_config" and d.get("data_date") != snapshot.get("target_trade_date"):
            date = html_lib.escape(str(d.get("data_date") or "日期未知"))
            tag += f'<span class="tag quote-note">日线 {date} 收盘</span>'
        return tag


    def _earnings_html(self, d):
        """财报列只写美东日期/时段与北京时间，不混入来源说明。"""
        earn = self._context.earnings_label(d.get("earnings"))
        if not earn:
            return ""
        urgency = self._context.earnings_urgency(d.get("earnings"))
        soon = {"red": " urgent", "yellow": " soon"}.get(urgency, "")
        main = html_lib.escape(earn[0])
        sub = html_lib.escape(earn[1])
        unit = f'<span class="unit">{sub}</span>' if sub else ""
        return f'<span class="earn{soon}">{main}{unit}</span>'


    def _signal_text_html(self, text):
        """只为提示文字着色，不改变信号等级、计数或公式；组合信号优先。"""
        text = str(text)
        if "共识价" in text or "目标价" in text:
            cls = "tp"
        elif "布林" in text and "RSI" in text:
            # 组合信号现在红黄两档都有，文案里带「→ 红 / → 黄」，按文案分色，别一律染红
            cls = "signal-red" if "→ 红" in text else "signal-yellow"
        elif text.startswith("RSI") and "超卖" in text:
            cls = "signal-magenta"
        elif any(text.startswith(prefix) for prefix in ("逼近布林下轨", "触及布林下轨", "跌破布林下轨")):
            cls = "signal-yellow"
        elif "均线" in text and ("触及" in text or "贴近" in text or "上穿" in text or "跌破" in text):
            period = re.search(r"(\d+)日(?:弱)?均线", text)
            cls = "signal-cyan" if period and int(period.group(1)) == self._context.S.get("ma_long") and self._context.S.get("ma_long", 0) > 0 else ""
        else:
            cls = ""
        escaped = self._context.arrow_html(html_lib.escape(text))
        return f'<span class="{cls}">{escaped}</span>' if cls else escaped


    def _description_signals(self, signals):
        """仅恢复旧描述中被中点拆开的布林尾句；不新增信号、判灯或突变输入。"""
        result = []
        for value in signals or []:
            text = str(value)
            tail = re.fullmatch(r"连续第≥?\d+日贴近([上下]轨)(?:（其中\d+日穿越）)?", text)
            if (tail and result and result[-1].startswith(self._context._TEXT_BOLL_PREFIX)
                    and "布林" + tail[1] in result[-1]):
                result[-1] += "，" + text
            else:
                result.append(text)
        return result


    def _signal_description_groups(self, d):
        """聚合同一指标的事实，布林+RSI互斥显示，绝不修改原 signals/marks。"""
        tg = d.get("target") or {}
        signals = [x for x in self._context._description_signals(d.get("signals")) if not self._context._consensus_description(x, tg)]
        levels = {}
        rank = {"gray": 0, "purple": 1, "yellow": 2, "red": 3}
        for kind, level in d.get("signal_marks") or []:
            kind = "ma" if kind in ("ma_cross", "ma_band") else kind
            if level in rank and rank[level] > rank.get(levels.get(kind), -1):
                levels[kind] = level
        for member in self._context.text_only_members([d]):
            levels.setdefault(member["kind"], "gray")
        combo = "boll_rsi" in levels or self._context._text_only_kind(signals) == "boll_rsi"
        groups = {}
        for index, text in enumerate(signals):
            kind = next((kind for kind, pattern in self._context._SIGNAL_DESCRIPTION_RULES if pattern.match(text)), "")
            if combo and kind in ("rsi", "boll"):
                kind = "boll_rsi"
            if kind == "boll_rsi" and re.fullmatch(r"布林\s*\+\s*RSI\s*→\s*[红黄]", text):
                # 原总结只重复 marks；描述字色表达级别，事实不删除。
                continue
            text = re.sub(r"\s*·\s*", "，", text)
            if kind == "ma":
                text = (self._context._ma_display_text(text, d.get("ma_settings"))
                        .replace(" / ", "，").replace("短均线/长均线", "短均线与长均线"))
            key = kind or f"context-{index}"
            entry = groups.setdefault(key, {"kind": kind, "level": levels.get(kind), "texts": []})
            if text not in entry["texts"]:
                entry["texts"].append(text)
        order = {"red": 0, "yellow": 1, "gray": 2}
        return sorted(groups.values(), key=lambda g: 4 if g["kind"] == "target" or g["level"] == "purple"
                      else order.get(g["level"], 3))


    def _signal_lamps(self, d):
        """保留旧调用接口；主表不再添加圆点，独立指标级别由描述字色表达。"""
        return ""


    def _main_description_text(self, text, kind, d):
        """仅投影主表文案；保留原事实、统计详情及判灯输入不变。"""
        valid_price = lambda value: (isinstance(value, (int, float)) and not isinstance(value, bool)
                                     and math.isfinite(value) and value > 0)
        if kind in ("rsi", "boll_rsi"):
            text = re.sub(r"（[≤≥][+-]?\d+(?:\.\d+)?）", "", text)
        if kind in ("boll", "boll_rsi"):
            text = re.sub(r"（盘中触及\s+[^）]+）", "", text)
            price, upper, lower = d.get("price"), d.get("boll_up"), d.get("boll_dn")
            if "突破布林上轨▲" in text and valid_price(price) and valid_price(upper):
                state = ("突破布林上轨▲，上轨外" if price > upper
                         else "盘中突破布林上轨▲后收回")
                text = re.sub(r"(?:盘中)?突破布林上轨▲", lambda _: state, text)
            if "跌破布林下轨▼" in text and valid_price(price) and valid_price(lower):
                state = ("跌破布林下轨▼，下轨外" if price < lower
                         else "盘中跌破布林下轨▼后收回")
                text = re.sub(r"(?:盘中)?跌破布林下轨▼", lambda _: state, text)
        if kind == "ma" and "触及" in text and "均线" in text:
            settings = d.get("ma_settings") or {}
            period = settings.get("ma_long") if "长均线" in text else settings.get("ma_short") if "短均线" in text else None
            numeric = re.search(r"(\d+)日(?:弱)?均线", text)
            if period is None and numeric:
                period = int(numeric[1])
            values = d.get("ma_values") or {}
            value = values.get(str(period)) if isinstance(values, dict) else None
            label = f"（均线价：{value:,.2f}）" if valid_price(value) else "（均线价：—）"
            if re.search(r"（日内 [^）]*，现价偏离 [^）]*）", text):
                text = re.sub(r"（日内 [^）]*，现价偏离 [^）]*）", label, text)
            elif valid_price(value) and "均线价：" not in text:
                text += label
        if kind == "low52" and text.startswith("触及52周新低") and "低点价：" not in text:
            value = d.get("low52")
            if valid_price(value):
                text += f"（低点价：{value:,.2f}）"
        return text


    def _signals_html(self, d):
        """主表价格提醒分主次但各自按真实灯级着色；统计事实不变，不添加圆点。"""
        # 手填价仅替换共识价；所有目标状态只投影到胶囊，统计仍消费原signals/marks。
        groups = [g for g in self._context._signal_description_groups(d) if g["kind"] != "target"]
        prices = {g["kind"]: g for g in groups
                  if g["kind"] in ("chg", "amp") and g["level"] in ("red", "yellow")}
        primary = supplement = None
        if len(prices) == 2:
            primary = "amp" if prices["amp"]["level"] == "red" and prices["chg"]["level"] == "yellow" else "chg"
            supplement = "chg" if primary == "amp" else "amp"
            # 只重排主表副本，补充事实紧跟主要提醒，不改共享分组及原marks。
            groups.remove(prices[supplement])
            groups.insert(groups.index(prices[primary]) + 1, prices[supplement])
        parts = []
        for group in groups:
            kind, level, texts = group["kind"], group["level"], group["texts"]
            # 个股描述直接展示事实，不额外前置指标名称；统计分类标签保持原样。
            separator = " · " if kind == "boll_rsi" else "，"
            display = separator.join(self._main_description_text(text, kind, d) for text in texts)
            if kind == "boll_rsi":
                selected = str(d.get("boll_alert_text") or next((text for text in texts if "布林" in text), ""))
                side = "上轨" if "上轨" in selected else "下轨" if "下轨" in selected else ""
                display = _reverse_rsi_description(display, side)
            body = self._context.arrow_html(html_lib.escape(display))
            role = "primary" if kind == primary else "supplement" if kind == supplement else None
            if parts:
                parts.append(' <span class="signal-separator">' + ('，' if role == "supplement" else '/') + '</span> ')
            parts.append(f'<span class="signal-item" data-signal-kind="{kind}"'
                         + (f' data-signal-level="{level}"' if level else "")
                         + (f' data-signal-role="{role}"' if role else "") + f'>{body}</span>')
        return ''.join(parts) if parts else "—"


    def _range52_row(self, d):
        """每只标的下面显示当前价在已核验的日线高低区间的位置；旧快照绝不推算极值。"""
        if d.get("reference_only"):
            return ""
        value = d.get("range52")
        price = d.get("price")
        valid = lambda n: isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) and n > 0
        if not isinstance(value, dict):
            return '<tr class="range52-row"><td colspan="4"><span class="range52-missing">52周区间：数据待更新</span></td></tr>'
        low, high = value.get("low"), value.get("high")
        if (not all(valid(n) for n in (price, low, high)) or not low < high or not low <= price <= high
                or value.get("scope") not in ("52周", "可用历史")
                or not all(isinstance(value.get(key), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[key])
                           for key in ("from", "to"))
                or value["from"] > value["to"] or value["to"] != d.get("data_date")
                or value.get("position") is None or type(value["position"]) not in (int, float)
                or not math.isfinite(value["position"])):
            return '<tr class="range52-row"><td colspan="4"><span class="range52-missing">52周区间：数据待更新</span></td></tr>'
        pos = (price - low) / (high - low) * 100
        scope = value["scope"]
        caption = (f'{scope}低 {self._context.fmt(low)}，本轮价 {self._context.fmt(price)} 位于区间 {pos:.1f}%，'
                   f'{scope}高 {self._context.fmt(high)}；{value["from"]} 至 {value["to"]} 日线高低价')
        # 成本价：紫方块是本轮价，黄竖线是成本价，形状区分开。
        # 成本价落在 52 周区间外时不再按 0~100% 夹取了事：竖线贴到对应端点，
        # 再加一个朝外的小箭头（class out-lo / out-hi），读屏标签里写清实际数值与方向
        cost_html = ""
        if valid(d.get("cost")):
            cost = d["cost"]
            cpos = (cost - low) / (high - low) * 100
            if cpos < 0:
                cost_html = '<span class="range52-cost out-lo"></span>'
                caption += f'；成本价 {self._context.fmt(cost)} 低于{scope}低点 {self._context.fmt(low)}'
            elif cpos > 100:
                cost_html = '<span class="range52-cost out-hi"></span>'
                caption += f'；成本价 {self._context.fmt(cost)} 高于{scope}高点 {self._context.fmt(high)}'
            else:
                cost_html = f'<span class="range52-cost" style="left:{cpos:.1f}%"></span>'
                caption += f'；成本价 {self._context.fmt(cost)} 位于区间 {cpos:.1f}%'
        # 两端第二行＝从本轮价到低／高点的变化幅度，以本轮价为基数；不是区间位置。
        # 全组生效（不再只有持仓），仅展示，不参与报灯、评分或折叠判定；等于端点时写 0.0%，不带负号。
        low_gap = (low / price - 1) * 100
        high_gap = (high / price - 1) * 100
        gap_text = lambda n: "0.0%" if abs(n) < 0.05 else f"{n:+.1f}%"
        low_text, high_text = gap_text(low_gap), gap_text(high_gap)
        low_gap_html = f'<span class="range52-gap">{low_text}</span>'
        high_gap_html = f'<span class="range52-gap">{high_text}</span>'
        caption += f'；从本轮价到低点 {low_text}、到高点 {high_text}（均以本轮价为基数）'
        # 现价三角之上不再写百分比：位置由三角在灰轨上的位置表达，端点下方两行给出涨跌空间。
        return (f'<tr class="range52-row"><td colspan="4">'
                f'<div class="range52-rail" role="img" aria-label="{caption}" style="--range-pos:{pos:.1f}%">'
                f'<span class="range52-lo" aria-hidden="true">{self._context.fmt(low)}{low_gap_html}</span>'
                f'<div class="range52-track">'
                f'<span class="range52-marker" style="left:{pos:.1f}%"></span>'
                f'{cost_html}</div>'
                f'<span class="range52-hi" aria-hidden="true">{self._context.fmt(high)}{high_gap_html}</span>'
                f'</div></td></tr>')


    def _stock_anchor_id(self, symbol):
        """个股行的锚点 id；同身份的标的只会在优先分组出现一次，不会撞 id。"""
        ident = self._context.instrument_identity(self._context.normalize_symbol(symbol))
        return "s-" + re.sub(r"[^0-9A-Za-z]+", "-", str(ident)).strip("-").lower()


    def _quiet_keep(self, d, earnings_week):
        """独立于红黄灯的主表保留原因；已读降绿后仍适用。"""
        return bool(d.get("reference_only") or not d.get("foldable", True)
                    or self._context._target_active(d) or self._context.earnings_soon(d.get("earnings"), earnings_week))


    def _stock_row(self, d, snapshot, earnings_week=None):
        if d.get("target") or d.get("purple") or any(kind == "target" for kind, _ in d.get("signal_marks") or []):
            d = self._context._apply_target(d)
        earnings_week = earnings_week or (snapshot.get("earnings_week") or {}).get("asof") or self._context._bj_today()
        quiet_keep = "true" if self._context._quiet_keep(d, earnings_week) else "false"
        state = self._context.target_display_state(d.get("target"))
        stripe = {"reached": "pur-hit", "space": "pur-space", "high": "pur-high"}.get(state)
        cls = d["level"] + (" pur " + stripe if self._context._target_active(d) and stripe else "")
        # 已读只撤加/减仓价的贡献；基础灯来自结构化marks，不能从描述或紫灯反推。
        # 旧快照的无来源红黄/缺失灰保持原样，避免把不明信号误降绿。
        base_level = d["level"]
        marks = d.get("signal_marks") or []
        if base_level in ("red", "yellow") and any(
                kind in ("add_price", "reduce_price") and level == base_level for kind, level in marks):
            remaining = {level for kind, level in marks if kind not in ("add_price", "reduce_price")}
            base_level = "red" if "red" in remaining else "yellow" if "yellow" in remaining else "green"
        chg_cls = "up" if (d["chg"] or 0) > 0 else (
            "down" if (d["chg"] or 0) < 0 else "")
        chg_txt = f'{d["chg"]:+.2f}%' if d["chg"] is not None else "—"
        note = f'<span class="note">{html_lib.escape(str(d["note"]))}</span>' if d["note"] else ""
        lv_html = self._context.level_note(d.get("trigger"), d.get("reduce"))
        tg_html = self._context.target_tag(d.get("target") or {})
        # 持仓内部分长短：长持仓屏蔽上轨普通过热，页面上标出来，不然看不出区别
        hold_html = ('<span class="unit" title="长持仓：不看一般过热，RSI 三条全超卖才提醒">长</span>'
                     if d.get("group") == "position" and str(d.get("hold") or "") == "long" else "")
        return (
            f'<tr class="{html_lib.escape(cls, quote=True)}" id="{self._context._stock_anchor_id(d["symbol"])}" data-quiet-keep="{quiet_keep}" '
            f'data-base-level="{html_lib.escape(base_level, quote=True)}" '
            f'data-price="{html_lib.escape(str(d.get("price") or ""), quote=True)}" '
            f'data-prev-close="{html_lib.escape(str(d.get("prev_close") or ""), quote=True)}"><td class="sym">'
            f'{html_lib.escape(self._context.normalize_symbol(d["symbol"]))}{note}{hold_html}'
            f'{lv_html}{self._context._row_tags(d, snapshot)}</td>'
            f'<td class="earn-cell">{self._context._earnings_html(d)}</td>'
            f'<td class="num px"><span class="px-price">{self._context.fmt(d["price"])}{html_lib.escape(str(d.get("unit") or ""))}</span>'
            f'<span class="px-chg {chg_cls}">{chg_txt}</span>{tg_html}</td>'
            f'<td class="sig">{self._context._signals_html(d)}</td></tr>'
            + self._context._range52_row(d)
        )


    def _stock_rows(self, lst, snapshot, earnings_week=None):
        out = "".join(self._context._stock_row(d, snapshot, earnings_week) for d in lst)
        return out or '<tr class="gray"><td colspan="4">今晚无异动，不用盯</td></tr>'


    def _group_stats_html(self, g, group, items_by_symbol, enabled):
        """分组标题仅计红、黄、缺失、紫；技术灰只在统计面板计数。"""
        stats = ''.join(self._context._count_badge(lv, count, label)
                        for lv, label in (("red", "红色警示"), ("yellow", "黄色警示"))
                        if (count := sum(d["level"] == lv for d in g)))
        # 技术灰仍进入统计面板及描述，不在个股/持仓分组标题另加数字。
        # 无数据（缺失）单独用虚线圈，避免和白灯撞名
        if (n_miss := sum(d["level"] == "gray" for d in g)):
            stats += self._context._count_badge("missing", n_miss, "缺失")
        # 紫色（目标价 / 共识价警示）：条数跟在红黄灯后面；一条都没有就不显示
        if (n_pur := self._context.purple_counts(g)):
            stats += self._context._count_badge("purple", n_pur, "紫色警示")
        # 板块组：红黄灯后面跟「板块 ETF 近 20 日相对标普500的强弱」，折叠时也能看到
        rs = (items_by_symbol.get(group["sector_etf"]) or {}).get("rs_spy") if group.get("sector_etf") else None
        if rs:
            diff = rs["diff"]
            rs_cls = "up" if diff >= self._context.RS_NEUTRAL_PP else "down" if diff <= -self._context.RS_NEUTRAL_PP else ""
            bn = rs.get("bench_name") or self._context.RS_BENCH_NAME
            rs_tip = html_lib.escape(
                f'{group["sector_etf"]} {rs["days"]}日 {rs["etf"]:+.1f}% − {bn} {rs["bench"]:+.1f}% = {diff:+.1f} 个百分点'
                f'（{rs["start"]} → {rs["end"]}）')
            stats += (f'<span class="rs-spy {rs_cls}" aria-label="{rs_tip}">'
                      f'{html_lib.escape(str(rs["days"]))}日对{".SPX" if bn == self._context.RS_BENCH_NAME else html_lib.escape(bn)} {diff:+.1f}%</span>')
        if not enabled:
            note = "仅板块ETF" if g else "监测关闭"
            stats = f'<span class="monitor-note">{note}</span>' + stats
        return stats


    def _sort_group_items(self, g, group):
        etf_row = lambda d: bool(d.get("etf")) or self._context.is_etf(d["symbol"])
        if group["item_group"] in ("position", "focus"):
            # 持仓 / 重点关注：同一预警级别里 ETF 排在个股后面（稳定排序，级别和涨跌幅顺序不变）
            return sorted(g, key=lambda d: (self._context._sort_key(d)[0], etf_row(d)))
        if group.get("sector_etf"):
            # 行业分类：本行业板块 ETF（XLK、XLV…）只在同一预警级别内排第一；无警示（绿灯）时照常折叠
            return sorted(g, key=lambda d: (self._context._sort_key(d)[0],
                                            self._context.normalize_symbol(d["symbol"]) != group["sector_etf"],
                                            self._context._sort_key(d)[1]))
        return g


    def _display_groups(self):
        """首页指数基置底；不改变 GROUPS 的清单去重、抓取与监测优先级。"""
        return [g for g in self._context.GROUPS if g["key"] != "index_funds"] + [g for g in self._context.GROUPS if g["key"] == "index_funds"]


    def _group_body(self, g, group, snapshot, earnings_week, enabled, collapse_id, collapse_label, empty_note=""):
        """分组（或持仓内部某个区）的展开内容：空提示 + 警示表 + 「无异动 N 只」折叠表 + 底部收起条。"""
        earnings_week = earnings_week or (snapshot.get("earnings_week") or {}).get("asof") or self._context._bj_today()
        g = [dict(d, group=group["item_group"]) if d.get("group") != group["item_group"] else d for d in g]
        g = [self._context._apply_target(d) if d.get("target") or d.get("purple")
             or any(kind == "target" for kind, _ in d.get("signal_marks") or []) else d for d in g]
        shown = [d for d in g if d["level"] in ("red", "yellow", "gray") or self._context._quiet_keep(d, earnings_week)]
        quiet = [d for d in g if d["level"] == "green" and not self._context._quiet_keep(d, earnings_week)]
        parts = []
        if empty_note:
            parts.append(f'<div class="quiet">{empty_note}</div>')
        if shown:
            parts.append(f'<table class="stk">{self._context.STK_COLS}{self._context.STK_HEAD}{self._context._stock_rows(shown, snapshot, earnings_week)}</table>')
        if quiet:
            parts.append(f'<details class="quiet-list"><summary>无异动 {len(quiet)} 只</summary>'
                         f'<table class="stk">{self._context.STK_COLS}{self._context.STK_HEAD}{self._context._stock_rows(quiet, snapshot, earnings_week)}</table></details>')
        parts.append(self._context._collapse_button(collapse_id, collapse_label))
        return "\n".join(parts)


    def _position_long_zone_card(self, g, group, items_by_symbol, snapshot, earnings_week, enabled):
        """持仓里只有「长持仓」单独成一块可折叠区；没有长持仓就不占位。

    短持仓不再单包一层——展开「持仓」直接就是短持仓的表，少一次多余折叠。
    """
        zlabel = dict(self._context.POSITION_ZONES)["long"]
        zg = [d for d in g if self._context._position_zone(d) == "long"]
        if not zg:
            return ""
        zid = "group_positions_long"
        zbody = self._context._group_body(zg, group, snapshot, earnings_week, enabled, zid, zlabel)
        zstats = self._context._group_stats_html(zg, group, items_by_symbol, enabled)
        return (f'<details class="zone-card" id="{zid}" open>'
                f'<summary><span class="zone-name">{zlabel} ({len(zg)})</span>'
                f'<span class="group-stats">{zstats}</span></summary>{zbody}</details>')


    def _group_card(self, group, g, items_by_symbol, snapshot):
        """一个分组的折叠卡片：展开的警示表 + 「无异动 N 只」折叠表。持仓内部再分长/短两区。"""
        monitoring = snapshot.get("group_monitoring", {})
        registered = snapshot.get("registered_counts", {})
        g = self._context._sort_group_items(g, group)
        earnings_week = (snapshot.get("earnings_week") or {}).get("asof") or self._context._bj_today()
        parts = []
        enabled = monitoring.get(group["key"], True)
        empty_note = ''
        if not g and enabled:
            empty_note = ('本组标的已在优先分组展示，避免重复信号' if registered.get(group["key"], 0)
                          else '暂无标的，去设置页录入或导入 CSV / EBK')
        if group["key"] == "positions":
            # 上方是「长持仓」可折叠区（没有就不出现），下方主体就是短持仓，不再多包一层
            parts.append(self._context._position_long_zone_card(g, group, items_by_symbol, snapshot, earnings_week, enabled))
            short_g = [d for d in g if self._context._position_zone(d) == "short"]
            parts.append(self._context._group_body(short_g, group, snapshot, earnings_week, enabled,
                                     f'group_{group["key"]}', group["label"], empty_note))
        else:
            parts.append(self._context._group_body(g, group, snapshot, earnings_week, enabled,
                                     f'group_{group["key"]}', group["label"], empty_note))
        body = "\n".join(parts)
        stats = self._context._group_stats_html(g, group, items_by_symbol, enabled)
        # 打开网页时只展开「统计」；所有分组（含持仓、重点）默认折叠。持仓点开后长/短两区默认展开。
        return (f'<details class="card group-card" id="group_{html_lib.escape(str(group["key"]), quote=True)}">'
                f'<summary class="grp"><span class="grp-name">{html_lib.escape(str(group["label"]))} ({len(g)})</span>'
                f'<span class="group-stats">{stats}</span></summary>{body}</details>\n')


    def _member_icons(self, d, kind, level):
        """只渲染已验证的方向标记；旧快照缺方向时显示待更新，绝不按当前报价反推。"""
        esc = lambda value: html_lib.escape(str(value), quote=True)
        # 组合信号现在红黄两档都有，箭头不能只给红档
        if kind == "boll_rsi" and level in ("red", "yellow"):
            direction = d.get("direction") if isinstance(d.get("direction"), str) else ""
            hits = d.get("boll_rsi_hits")
            if direction.startswith(("上轨", "下轨")) and type(hits) is int and hits in (2, 3):
                shape = "▲" if direction.startswith("上轨") else "▼"
                markers = [{"shape": shape, "tone": "red" if level == "red" else "yellow",
                            "label": self._context.boll_rsi_text(direction, hits)}]
            else:
                known = ("上轨（超买）", "下轨（超卖）", "上轨（RSI超卖，方向不一致）",
                         "下轨（RSI超买，方向不一致）", "上轨（RSI超买/超卖均命中）",
                         "下轨（RSI超买/超卖均命中）")
                # 旧快照缺条数时也要按新措辞显示方向，不再回显「下轨（超卖）」这种裸字段
                value = self._context.boll_rsi_text(direction, hits) if direction.startswith(("上轨", "下轨")) \
                    else ("方向待下次更新" if direction in known or not direction else direction)
                return f' <span class="signal-member-direction">{esc(value)}</span>'
        elif kind in ("rsi", "boll", "ma", "ma_cross", "ma_band", "chg", "amp", "vol_ratio") or (kind == "target" and level == "purple"):
            markers = d.get("indicators")
            if kind in ("chg", "amp", "vol_ratio") and d.get("flat") is True:
                return ' <span class="signal-member-direction">平盘</span>'
            if not isinstance(markers, list) or not markers:
                return ' <span class="signal-member-direction">方向待下次更新</span>'
            if kind in ("ma", "ma_cross") and {"▲", "▼"} <= {m.get("shape") for m in markers
                                                     if isinstance(m, dict)}:
                # 不同周期均线方向相反（如站上短均线却跌破长均线）：同格上下箭头会互相打架，
                # 跟布林+RSI 方向不一致时一样改成文字，不画三角。
                labels = [self._context._ma_stat_label(m["label"], d.get("ma_settings")) for m in markers
                          if isinstance(m, dict) and isinstance(m.get("label"), str) and m["label"]]
                return (f' <span class="signal-member-direction">'
                        f'{esc("，".join(labels) or "多周期均线方向不一致")}</span>')
        else:
            return ""
        icons = []
        for marker in markers[:4]:
            if not isinstance(marker, dict):
                continue
            shape, tone, label = marker.get("shape"), marker.get("tone"), marker.get("label")
            if shape not in ("▲", "▼", "■", "□", self._context._ARROW_RSI_UP, self._context._ARROW_RSI_DOWN) or tone not in ("white", "yellow", "red", "cyan", "purple"):
                continue
            allowed = {"chg": ("white",), "amp": ("white",), "vol_ratio": ("white",),
                       "rsi": ("yellow", "red"), "boll": ("white", "yellow"),
                       "boll_rsi": ("yellow", "red"), "ma": ("cyan",), "ma_cross": ("cyan",),
                       "ma_band": ("cyan",), "target": ("purple",)}
            if tone not in allowed.get(kind, ()):
                continue
            if shape not in ("▲", "▼") and kind not in ("target", "ma", "ma_band", "rsi"):
                continue
            if shape == "□" and (kind != "target" or level != "purple" or tone != "purple"):
                continue
            if not isinstance(label, str) or not label or len(label) > 100:
                continue
            if kind in ("ma", "ma_cross", "ma_band"):
                label = self._context._ma_display_text(label, d.get("ma_settings"))
            if shape in (self._context._ARROW_RSI_UP, self._context._ARROW_RSI_DOWN):
                cls = "arrow-rsi-up" if shape == self._context._ARROW_RSI_UP else "arrow-rsi-down"
                icons.append(f'<span class="signal-member-icon {cls}" role="img" '
                             f'aria-label="{esc(label)}">{shape}</span>')
                continue
            direction_class = " arrow-band-up" if shape == "▲" else " arrow-band-down" if shape == "▼" else ""
            icons.append(f'<span class="signal-member-icon tone-{tone}{direction_class}" role="img" '
                         f'aria-label="{esc(label)}">{shape}</span>')
        return "".join(icons) or ' <span class="signal-member-direction">方向待下次更新</span>'


    def _filter_detail(self, d, kind, label):
        """同时满足筛选里每个条件的简短数值，与计数明细口径一致；缺值时只写信号名。"""
        if kind in ("chg", "amp", "vol_ratio") and isinstance(d.get("metric"), str) and d["metric"]:
            return d["metric"]
        if kind == "rsi" and isinstance(d.get("rsi_hits"), str) and d["rsi_hits"]:
            return d["rsi_hits"]   # 已经是「RSI 2条 ↑超买」完整写法，不再补一次前缀
        if kind == "boll" and isinstance(d.get("boll_state"), str) and d["boll_state"]:
            return f"布林{d['boll_state']}"
        if kind == "boll_rsi" and isinstance(d.get("direction"), str) and d["direction"]:
            side, _, _ = self._context._parse_boll_rsi_direction(d["direction"])
            detail = f"布林{self._context.boll_rsi_text(d['direction'], d.get('boll_rsi_hits'))}"
            detail = re.sub(r" · (?:反向|方向不一致)$", "", detail)
            return _reverse_rsi_description(detail, side)
        marks = [m.get("label") for m in d.get("indicators") or []
                 if isinstance(m, dict) and isinstance(m.get("label"), str) and m.get("label")]
        if kind in ("ma", "ma_cross", "ma_band"):
            # 与分框标题同一套措辞（穿越倒装），同一指标内部用「，」而不是「 / 」。
            return self._context._ma_stat_text(marks, d.get("ma_settings")) or label
        return " / ".join(marks) if marks else label


    def _earnings_week_panel(self, snapshot, count_only=False):
        """计数表下方的未来15天（美东今天起至第15天，含）财报名单，不是交易信号，不挤进红黄紫列。
    count_only=True 时只返回已核验名单条数；快照缺失或无效返回 None，不当作 0。"""
        pending = None if count_only else '<details class="earnings-week"><summary>未来15天财报 · 名单待下次更新</summary></details>'
        data = snapshot.get("earnings_week")
        if not isinstance(data, dict):
            return None if count_only else ""
        asof, members = data.get("asof"), data.get("members")
        if not isinstance(asof, str) or not isinstance(members, list):
            return pending
        try:
            asof = self._context.datetime.strptime(asof, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return pending
        esc = lambda value: html_lib.escape(str(value), quote=True)
        lines = []
        for entry in members:
            if (not isinstance(entry, dict) or not isinstance(entry.get("symbol"), str)
                    or not entry["symbol"].strip() or not isinstance(entry.get("note", ""), str)
                    or not self._context.earnings_within_list({"status": "ok", "date": entry.get("date")}, asof)):
                return pending
            date = entry["date"]
            timing = {"pre": "盘前", "post": "盘后", "during": "盘中"}.get(entry.get("timing"), "时段待定")
            kind = {"official": "官方活动日期", "expected": "预计", "estimated": "估算"}.get(entry.get("kind"), "来源待确认")
            note = f'<span class="note">{esc(entry["note"])}</span>' if entry.get("note") else ""
            belong = self._context._member_belong(entry)   # 跟其它名单一样：分组写在代码后面的括号里，持仓分长/短
            level = entry.get("level")
            lamp_name = {"red": "红灯", "yellow": "黄灯", "green": "绿灯", "gray": "缺失"}.get(level)
            # 名称前共享竖条，颜色跟着这只股自己的灯级。
            # CSS竖条读屏读不到，保留不可见文字，别让灯级信息只靠颜色传达。
            lamp_cls = f' class="lamp-{level}"' if lamp_name else ""
            lamp_text = f'<span class="lamp-text">（{lamp_name}）</span>' if lamp_name else ""
            # 名字（含中文备注）整体可点：跳到首页该股所在行，和其它灯的名单同一套 signal-member-jump
            anchor = self._context._stock_anchor_id(entry["symbol"])
            urg = self._context.earnings_urgency({"status": "ok", "date": date, "timing": entry.get("timing")}, asof)
            urg_cls = {"red": " is-red", "yellow": " is-yellow"}.get(urg, "")
            lines.append(f'<li{lamp_cls}><a class="signal-member-jump" href="#{anchor}">'
                         f'<span class="signal-member-symbol">{esc(entry["symbol"])}</span>{note}</a>'
                         f'<span class="signal-member-belong">（{esc(belong)}）</span> '
                         f'<span class="earnings-week-meta{urg_cls}">美东 {esc(date[5:].replace("-", "/"))} {timing} · {kind}</span>'
                         f'{lamp_text}</li>')
        if count_only:
            return len(lines)
        if not lines:
            return ""
        summary = f'未来15天财报 · {len(lines)} 只'
        return (f'<div class="signal-extra-row earnings-extra">'
                f'<button type="button" class="signal-filter-toggle" data-kind="earnings_week" '
                f'aria-pressed="false" aria-label="选择财报叠加条件">财报</button>'
                f'<details class="earnings-week"><summary>{summary}</summary>'
                f'<ul>{"".join(lines)}</ul></details></div>')


    def _signal_counts_card(self, snapshot):
        """各信号计数及分组明细；空计数不显示，缺失或不一致的成员等待下次更新。"""
        snapshot = self._context._merge_ma_snapshot(snapshot)
        counts = snapshot.get("signal_counts") or {}
        members = snapshot.get("signal_members") or {}
        levels = (("red", "红色信号"), ("yellow", "黄色信号"))
        group_order = {g["item_group"]: i for i, g in enumerate(self._context._display_groups())}
        group_labels = {g["item_group"]: g["label"] for g in self._context.GROUPS}
        esc = lambda value: html_lib.escape(str(value), quote=True)
        rows = ""
        filters = {}  # 同时满足筛选：每类信号的已核验成员（红/黄/紫合并）
        gray_by_kind = self._context._gray_by_kind(snapshot)
        kinds_iter = [(kind, value) for kind, value in counts.items() if kind not in ("fund", "target")]
        for gkind in ("rsi", "boll", "boll_rsi"):
            if gray_by_kind.get(gkind) and gkind not in counts:
                kinds_iter.append((gkind, {"label": dict(self._context.SIGNAL_KINDS)[gkind]}))
        # 技术三栏与规则表格同序：布林+RSI（组合）排在单独 RSI、单独布林之前，
        # 组合条件触发的标的不再出现在单独条件后面。
        kinds_iter.sort(key=lambda item: self._context._KIND_PANEL_ORDER.get(item[0], len(self._context.SIGNAL_KINDS) * 2))
        for kind, v in kinds_iter:
            # 只显示这一轮真的有计数的信号：全 0 的行不占位。
            # 布林和 RSI 现在都不再单独报灯，计数恒为 0，这两行会自然消失——
            # 不需要在这里写死排除，免得以后规则再变时要改两处。
            if v.get("pending") and kind == "ma":
                filters[kind] = {"label": "均线", "ok": False, "members": {}}
                rows += ('<tr><td><button type="button" class="signal-filter-toggle" data-kind="ma" '
                         'aria-pressed="false">均线</button></td><td colspan="3">明细待下次更新</td></tr>')
                continue
            if not any(v.get(level) for level, _ in levels) and not gray_by_kind.get(kind):
                continue
            raw_label = str(v.get("label", ""))
            label = esc(raw_label)
            flt = filters.setdefault(kind, {"label": raw_label, "ok": True, "members": {}})
            kind_id = hashlib.sha256(str(kind).encode("utf-8")).hexdigest()
            cells, panels = [], []
            kind_members = members.get(kind) if isinstance(members, dict) else None
            for level, level_name in levels:
                n = v.get(level, 0)
                if not n:
                    cells.append('<td class="num">—</td>')
                    continue
                panel_id = f"signal-members-{kind_id}-{level}"
                cells.append(f'<td class="num"><button type="button" class="signal-count-toggle" '
                             f'data-target="{panel_id}" aria-controls="{panel_id}" aria-expanded="false" '
                             f'aria-label="{label} · {level_name} · {esc(n)} 只，展开或收起标的">{esc(n)}</button></td>')
                entries = kind_members.get(level) if isinstance(kind_members, dict) else None
                valid = (isinstance(entries, list) and len(entries) == n
                         and all(isinstance(d, dict) and isinstance(d.get("symbol"), str)
                                 and d["symbol"].strip() for d in entries))
                if valid:
                    valid = len({self._context.instrument_identity(d["symbol"]) for d in entries}) == n
                if not valid:
                    flt["ok"] = False
                else:
                    for d in entries:
                        rec = flt["members"].setdefault(self._context.instrument_identity(d["symbol"]), {
                            "s": d["symbol"], "n": d.get("note") if isinstance(d.get("note"), str) else "",
                            "g": self._context._member_belong(d),
                            "o": group_order.get(str(d.get("group") or ""), len(self._context.GROUPS)),
                            "l": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else "",
                            "d": []})
                        part = {"l": level, "t": self._context._filter_detail(d, kind, raw_label)}
                        if part not in rec["d"]:
                            rec["d"].append(part)
                if not valid:
                    content = '<p>明细待下次更新：当前快照缺少成员或成员人数与计数不一致，暂不展示名单。</p>'
                else:
                    # 明细按方向分两段：先全部 ▲，一条横线，再全部 ▼，最后是没有三角的
                    # （文字方向/方向待更新）。不写分组标题，也不写分组名——点名字直接跳过去。
                    # 中文备注名保留，跟在代码后面，跟首页个股行一致。
                    def _member_row(d, icon=True):
                        direction = self._context._member_icons(d, kind, level).strip() if icon else ""
                        # 分框栏目的框标题已经写了方向，行尾不再重复同一句
                        metric = "" if kind in self._context._NO_ROW_METRIC_KINDS else self._context._member_metric_text(d, kind)
                        value = f' <span class="signal-member-metric">{self._context.arrow_html(esc(metric))}</span>' if metric else ""
                        note = (f'<span class="note">{esc(d["note"])}</span>'
                                if isinstance(d.get("note"), str) and d["note"].strip() else "")
                        # 分组写在代码后面的括号里：持仓内部标长/短，跟首页分区对得上
                        belong = f'<span class="signal-member-belong">（{esc(self._context._member_belong(d))}）</span>'
                        # 分类按指标级别；名称前竖条独立使用主表整股灯级，未知不借分类灯。
                        lamp = d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else "unknown"
                        return (f'<li class="lamp-{lamp}">'
                                f'<a class="signal-member-jump" href="#{self._context._stock_anchor_id(d["symbol"])}">'
                                f'<span class="signal-member-symbol">{esc(d["symbol"])}</span>{note}</a>'
                                f'{belong}{direction}{value}</li>')

                    ordered = sorted(entries, key=lambda x: group_order.get(str(x.get("group") or ""), len(self._context.GROUPS)))
                    if kind in self._context._BLOCK_PANEL_KINDS:
                        # 方向放到框标题上：▲ 一框、▼ 一框，名单里不再逐条画三角
                        blocks, order = {}, []
                        for d in ordered:
                            shape, title = self._context._member_block(d, kind)
                            if (shape, title) not in blocks:
                                blocks[(shape, title)] = []
                                order.append((shape, title))
                            blocks[(shape, title)].append(_member_row(d, icon=False))
                        order.sort(key=lambda k: self._context._shape_rank(k[0]))
                        content = '<div class="signal-member-groups">' + "".join(
                            f'<div class="signal-member-block"><h5>{self._context._block_title_html(kind, shape, title)}</h5>'
                            f'<ul class="signal-member-list">{"".join(blocks[(shape, title)])}</ul></div>'
                            for shape, title in order) + '</div>'
                    else:
                        buckets = {"up": [], "down": [], "other": []}
                        for d in ordered:
                            row = _member_row(d)
                            buckets["up" if "▲" in row else "down" if "▼" in row else "other"].append(row)
                        parts = ["".join(buckets[name]) for name in ("up", "down", "other") if buckets[name]]
                        # 分隔线必须单独成变量：写成相邻字符串字面量会先被隐式拼接，
                        # 结果把 <div>/<ul> 一起当成连接符插进每一段之间。
                        divider = '<li class="signal-member-split" role="separator"></li>'
                        content = ('<div class="signal-member-groups"><ul class="signal-member-list">'
                                   + divider.join(parts) + '</ul></div>')
                # 保留分类面板tone样式；名称前竖条由每个li的整股lamp级别覆盖。
                panels.append(f'<div class="signal-members-panel tone-{level}" id="{panel_id}" hidden>{content}</div>')
            gray_rows = gray_by_kind.get(kind) or []
            if gray_rows:
                g_cell, g_panel, g_ok = self._context._gray_cell(kind, gray_rows, raw_label, flt["members"], group_labels, group_order)
                cells.append(g_cell)
                panels.append(g_panel)
                if not g_ok:
                    flt["ok"] = False
            else:
                cells.append('<td class="num">—</td>')
            rows += (f'<tr><td><button type="button" class="signal-filter-toggle" data-kind="{esc(kind)}" '
                     f'aria-pressed="false">{label}</button></td>{"".join(cells)}</tr>'
                     f'<tr class="signal-members-row"><td colspan="4">{"".join(panels)}</td></tr>')
        earnings_panel = self._context._earnings_week_panel(snapshot)
        purple_panel = self._context._target_purple_panel(snapshot)
        if not rows and not earnings_panel and not purple_panel and (
                "target_overlays" in snapshot or not any(
                    type((snapshot.get("summary") or {}).get(level)) is int
                    and snapshot["summary"][level] > 0 for level in ("red", "yellow"))):
            return ""
        group_order = {g["item_group"]: i for i, g in enumerate(self._context._display_groups())}
        group_labels = {g["item_group"]: g["label"] for g in self._context.GROUPS}

        def add_overlay_filter(kind, label, entries, expected, detail_fn, level_fn=None):
            valid = (isinstance(entries, list) and type(expected) is int and expected >= 0
                     and len(entries) == expected
                     and all(isinstance(d, dict) and isinstance(d.get("symbol"), str) and d["symbol"].strip()
                             for d in entries))
            if valid:
                valid = len({self._context.instrument_identity(d["symbol"]) for d in entries}) == expected
            rec = {"label": label, "ok": valid, "members": {}}
            if valid:
                for d in entries:
                    symbol = self._context.normalize_symbol(d["symbol"])
                    identity = self._context.instrument_identity(symbol)
                    group = str(d.get("group") or "")
                    rec["members"][identity] = {
                        "s": symbol,
                        "n": d.get("note") if isinstance(d.get("note"), str) else "",
                        "g": self._context._member_belong(d),
                        "o": group_order.get(group, len(self._context.GROUPS)),
                        "l": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else "",
                        # 逐条条件的字色只跟着这条条件自身的等级：紫灯紫字；
                        # 财报跟财报名单同一套红/黄/灰，不再整条白字。
                        "d": [{"l": "purple" if kind == "target_purple"
                               else (level_fn(d) if level_fn else ""), "t": detail_fn(d)}],
                    }
            filters[kind] = rec

        overlays = snapshot.get("target_overlays")
        if isinstance(overlays, dict):
            purple_entries = self._context._target_overlay_union(overlays)
            if purple_entries:
                add_overlay_filter("target_purple", "空间 / 达到", purple_entries, len(purple_entries),
                                   lambda d: str(d.get("detail") or "空间 / 达到"))

        earnings = snapshot.get("earnings_week")
        if isinstance(earnings, dict):
            earn_members = earnings.get("members")
            asof = earnings.get("asof")
            valid_earnings = isinstance(asof, str) and isinstance(earn_members, list)
            if valid_earnings:
                try:
                    asof = self._context.datetime.strptime(asof, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    valid_earnings = False
            if valid_earnings:
                valid_earnings = all(isinstance(d, dict) and isinstance(d.get("symbol"), str)
                                     and d["symbol"].strip()
                                     and self._context.earnings_within_list({"status": "ok", "date": d.get("date")}, asof)
                                     for d in earn_members)
            if valid_earnings and earn_members:
                timing_labels = {"pre": "盘前", "post": "盘后", "during": "盘中"}
                kind_labels = {"official": "官方日期", "expected": "预计", "estimated": "估算"}
                def earn_level(d):
                    """财报的字色＝财报名单同一套：近 3 日红、7 日内黄、其余灰。"""
                    urg = self._context.earnings_urgency(
                        {"status": "ok", "date": d.get("date"), "timing": d.get("timing")}, asof)
                    return urg if urg in ("red", "yellow") else "gray"

                add_overlay_filter("earnings_week", "未来15天财报", earn_members, len(earn_members),
                                   lambda d: f"财报 {d.get('date', '')} "
                                   f"{timing_labels.get(d.get('timing'), '时段待定')} · "
                                   f"{kind_labels.get(d.get('kind'), '来源待确认')}",
                                   earn_level)

        filter_data = {kind: {"label": f["label"], "ok": f["ok"],
                              "members": sorted(({"id": key, "a": self._context._stock_anchor_id(rec["s"]), **rec}
                                                 for key, rec in f["members"].items()),
                                                key=lambda m: (m["o"], m["s"]))}
                       for kind, f in filters.items()}
        filter_json = json.dumps(filter_data, ensure_ascii=False, separators=(",", ":"))
        filter_html = (f'<div class="signal-filter" id="signalFilterResult" role="status" '
                       f'data-filter="{esc(filter_json)}" hidden></div>') if filter_data else ""
        summary = snapshot.get("summary") or {}
        whole = lambda n: n if type(n) is int and n >= 0 else None
        gray_count = len({self._context.instrument_identity(m["symbol"])
                          for entries in gray_by_kind.values() for m in entries})
        totals = (("red", "红", whole(summary.get("red"))), ("yellow", "黄", whole(summary.get("yellow"))),
                  ("gray", "白", gray_count), ("purple", "紫", whole(summary.get("purple"))),
                  ("earn", "财报", self._context._earnings_week_panel(snapshot, count_only=True)))
        stats = "".join(
            (self._context._count_badge(key, n, name) if n is not None and key in ("red", "yellow", "gray", "purple")
             else f'<span class="count-total count-{key}">{name} {"—" if n is None else n}</span>')
            for key, name, n in totals)
        return (f'<details class="card group-card" id="signalCounts" open><summary class="grp"><span class="grp-name">统计</span>'
                f'<span class="group-stats">{stats}</span></summary>'
                f'{f"<table>{rows}</table>" if rows else ""}{purple_panel}'
                f'{earnings_panel}{filter_html}'
                f'{self._context._collapse_button("signalCounts", "统计")}</details>\n')


    def _gray_title(self, kind, r):
        """白灯名单的框标题 (图形, 文字, RSI条数)：只分上轨 / 下轨和 RSI 条数，不分突破还是逼近。
    旧快照没有结构化字段时，从 detail 文字里解析。"""
        detail = str(r.get("detail") or "")
        side = r.get("side") or ("上轨" if "上轨" in detail else "下轨" if "下轨" in detail else "")
        n, rs = r.get("rsi_n") or 0, r.get("rsi_side") or ""
        if not n:
            m = self._context._GRAY_RSI_RE.search(detail)
            if m:
                n, rs = int(m.group(1)), m.group(2)
        rsi = self._context.rsi_hits_text(n, rs) if n and rs in ("超买", "超卖") else ""
        if kind == "rsi":
            # ▲▼ 只表示布林上下轨，单独 RSI 用 ↑超买 / ↓超卖
            return (self._context._ARROW_RSI_UP if rs == "超买" else self._context._ARROW_RSI_DOWN if rs == "超卖" else "",
                    rsi or "方向待下次更新", n)
        if not side:
            return ("", "方向待下次更新", n)
        shape = "▲" if side == "上轨" else "▼"
        # 组合与单独同一套措辞：布林只写上下轨，RSI 只写超买超卖
        title = self._context.boll_side_text(side) if kind == "boll" else (
            f"{self._context.boll_side_text(side)} · {rsi}" if rsi else self._context.boll_side_text(side))
        return (shape, title, n)


    def _gray_cell(self, kind, picked, label, filter_members, group_labels, group_order):
        """灰列单元格 + 展开名单；名单按框标题分组（▲ 在上、▼ 在下），写法同其他分框栏目。
    同时把成员并进该行的「同时满足」筛选数据。返回 (单元格, 名单面板, 是否有效)。"""
        esc = lambda v: html_lib.escape(str(v), quote=True)
        panel_id = "signal-members-gray-" + hashlib.sha256(kind.encode("utf-8")).hexdigest()
        ok = len({self._context.instrument_identity(r["symbol"]) for r in picked}) == len(picked)
        if ok:
            blocks, order = {}, []
            for r in picked:
                shape, title, n = self._context._gray_title(kind, r)
                key = (shape, title)
                if key not in blocks:
                    blocks[key] = []
                    order.append((shape, title, n))
                note = f'<span class="note">{esc(r.get("note"))}</span>' if str(r.get("note") or "").strip() else ""
                belong = f'<span class="signal-member-belong">（{esc(self._context._member_belong(r))}）</span>'
                lamp = r.get("level") if r.get("level") in ("red", "yellow", "green", "gray") else "unknown"
                blocks[key].append((group_order.get(str(r.get("group") or ""), len(self._context.GROUPS)),
                                    f'<li class="lamp-{lamp}"><a class="signal-member-jump" href="#{self._context._stock_anchor_id(r["symbol"])}">'
                                    f'<span class="signal-member-symbol">{esc(r["symbol"])}</span>{note}</a>{belong}</li>'))
                rec = filter_members.setdefault(self._context.instrument_identity(r["symbol"]), {
                    "s": r["symbol"], "n": r.get("note") if isinstance(r.get("note"), str) else "",
                    "g": self._context._member_belong(r),
                    "o": group_order.get(str(r.get("group") or ""), len(self._context.GROUPS)),
                    "l": r.get("level") if r.get("level") in ("red", "yellow", "green") else "green", "d": []})
                detail = f"布林{title}" if kind in ("boll", "boll_rsi") else title
                if kind == "boll_rsi":
                    side = "上轨" if shape == "▲" else "下轨" if shape == "▼" else ""
                    detail = _reverse_rsi_description(detail, side)
                part = {"l": "gray", "t": detail}
                if part not in rec["d"]:
                    rec["d"].append(part)
            order.sort(key=lambda k: (self._context._shape_rank(k[0]), -k[2], k[1]))
            content = '<div class="signal-member-groups">' + "".join(
                f'<div class="signal-member-block"><h5>{self._context._block_title_html(kind, shape, title)}</h5><ul class="signal-member-list">'
                + "".join(li for _, li in sorted(blocks[(shape, title)], key=lambda x: x[0])) + '</ul></div>'
                for shape, title, _ in order) + '</div>'
        else:
            content = '<p>明细待下次更新：名单中有重复标的，暂不展示。</p>'
        cell = (f'<td class="num"><button type="button" class="signal-count-toggle" data-target="{panel_id}" '
                f'aria-controls="{panel_id}" aria-expanded="false" '
                f'aria-label="白灯 · {esc(label)} · {len(picked)} 只，展开或收起标的">{len(picked)}</button></td>')
        panel = f'<div class="signal-members-panel tone-gray" id="{panel_id}" hidden>{content}</div>'
        return cell, panel, ok


    def _target_purple_panel(self, snapshot):
        """展开空间/达到两段；高空间紫红空心优先，达到一律紫色实心并写具体事件。"""
        overlays = snapshot.get("target_overlays")
        if not isinstance(overlays, dict):
            return ""
        space, reached = overlays.get("space"), overlays.get("reached")
        if not isinstance(space, list) or not isinstance(reached, list) or not (space or reached):
            return ""
        valid = all(isinstance(d, dict) and isinstance(d.get("symbol"), str) and d["symbol"].strip()
                    and isinstance(d.get("detail"), str) and d["detail"]
                    for entries in (space, reached) for d in entries)
        valid = valid and all(len({self._context.instrument_identity(d["symbol"]) for d in entries}) == len(entries)
                              for entries in (space, reached))
        esc = lambda v: html_lib.escape(str(v), quote=True)
        order = {g["item_group"]: i for i, g in enumerate(self._context._display_groups())}
        parts = []
        if valid:
            def space_key(d):
                value = d.get("space")
                # 使用原始空间值，不读四舍五入后的文案；旧快照缺值在同档末尾。
                descending = -value if type(value) in (int, float) and math.isfinite(value) else math.inf
                return (0 if d.get("band") == "high" else 1, descending, d["symbol"])

            for section, entries, label in (("space", space, "空间"), ("reached", reached, "达到")):
                lines = []
                ordered = sorted(entries, key=space_key if section == "space" else
                                 lambda d: (order.get(d.get("group"), len(order)), d["symbol"]))
                for d in ordered:
                    symbol = self._context.normalize_symbol(d["symbol"])
                    note = f'<span class="note">{esc(d.get("note"))}</span>' if str(d.get("note") or "").strip() else ""
                    # 竖条跟这支个股自身的灯级（红/黄/绿/灰），不因进了紫灯就染成紫色。
                    lamp = d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else "unknown"
                    lead = f'<span class="signal-name-lamp {lamp}" aria-hidden="true"></span>'
                    high_class = ' class="target-space-high"' if section == "space" and d.get("band") == "high" else ""
                    lines.append(f'<li{high_class}>{lead}<a class="signal-member-jump" href="#{self._context._stock_anchor_id(symbol)}">'
                                 f'<span class="signal-member-symbol">{esc(symbol)}</span>{note}</a>'
                                 f'<span class="signal-member-belong">（{esc(self._context._member_belong(d))}）</span>'
                                 f' <span class="signal-member-direction">{self._context.arrow_html(esc(d["detail"]))}</span></li>')
                parts.append(f'<h5>{label}</h5><ul class="signal-member-list">{"".join(lines)}</ul>')
            content = "".join(parts)
        else:
            content = '<p>明细待下次更新：当前快照名单无效或有重复标的，暂不展示。</p>'
        return (f'<div class="signal-extra-row purple-extra">'
                f'<button type="button" class="signal-filter-toggle" data-kind="target_purple" '
                f'aria-pressed="false" aria-label="选择紫灯叠加条件">紫灯</button>'
                f'<details class="text-only target-purple"><summary>空间{len(space)}只　达到{len(reached)}只</summary>'
                f'{content}</details></div>')


    def _policy_details(self, snapshot):
        """决议信息仅保留在页底折叠详情中，不生成常驻卡片或接入风险评分。"""
        if "reference_alerts" not in snapshot:
            return ""
        refs = snapshot.get("reference_alerts")
        refs = refs if isinstance(refs, dict) else {}
        policy = refs.get("policy")
        policy = policy if isinstance(policy, dict) else {}
        fed = policy.get("fed")
        fed = fed if isinstance(fed, dict) else {}
        assessment_time = self._context.policy_alerts.moment(refs.get("assessed_at"))
        decision = self._context.policy_alerts.fed_decision(fed, assessment_time)
        esc = lambda value: html_lib.escape(str(value))
        status = decision["status"]
        published = self._context.policy_alerts.moment(fed.get("date"))
        valid = status != "unavailable"
        action = fed.get("action")
        action = action if isinstance(action, str) else ""
        label = {"raise": "加息", "lower": "降息", "maintain": "维持不变", "keep": "维持不变"}.get(action, "待更新")
        bps = fed.get("change_bps")
        if valid and action in ("raise", "lower") and self._context.policy_alerts.valid_number(bps) and 0 < bps <= 1000:
            label += f" {bps:g} 个基点"
        message = f'{label} · {published.date().isoformat()}（美东公布日）' if valid else "尚未取得可核实的正式决议"
        if status == "pending":
            message += " · 新决议信息待更新，以上为上次已核实消息"
        details = f'<p>{esc(message)}</p>'
        if valid:
            details += f'<p>联邦基金目标利率区间：{fed["low"]:.2f}%–{fed["high"]:.2f}%。</p>'
        released = self._context.policy_alerts.moment(fed.get("released_at"))
        if valid and released:
            details += (f'<p>正式公布：{released.astimezone(self._context.policy_alerts.NY).strftime("%Y-%m-%d %H:%M %Z")}（美东）'
                        f'／{released.astimezone(self._context.TZ).strftime("%Y-%m-%d %H:%M")}（北京时间）。</p>')
        next_fed = self._context.policy_alerts.moment(fed.get("next_release"))
        if next_fed:
            details += (f'<p>下次计划决议：{next_fed.astimezone(self._context.policy_alerts.NY).strftime("%Y-%m-%d %H:%M %Z")}（美东）'
                        f'／{next_fed.astimezone(self._context.TZ).strftime("%Y-%m-%d %H:%M")}（北京时间）；以官方实际发布为准。</p>')
        if fed.get("fetch_error"):
            details += f'<p>最近查询未成功：{esc(fed["fetch_error"])}。旧消息保留日期，不冒充最新结果。</p>'
        if fed.get("schedule_error"):
            details += f'<p>日程待更新：{esc(fed["schedule_error"])}。最近已核实决议继续保留。</p>'
        if valid:
            details += (f'<p><a href="{esc(fed["url"])}" target="_blank" rel="noopener noreferrer">美联储官方决议声明</a></p>')
        details += (f'<p><a href="{self._context.policy_alerts.FED_URL}" target="_blank" rel="noopener noreferrer">美联储官方会议日历</a></p>')
        checked = self._context.policy_alerts.moment(fed.get("checked_at"))
        if checked:
            details += f'<p>最近查询：{checked.astimezone(self._context.TZ).strftime("%Y-%m-%d %H:%M")}（北京时间）。</p>'
        details += ('<p>抓取来源与缓存间隔见名词解释和抓取。'
                    '仅按例行会议日程更新，临时决议可能延迟发现。</p>')
        return (f'<details class="macro-help" data-status="{status}" id="policyAlertDetails">'
                '<summary>美联储利率决议 · 查看数据与来源</summary>' + details + '</details>')


    def _oil_reference_row(self, snapshot):
        """风险表中的WTI报价行；日变化与风险灯分开，不进入综合评分。"""
        if "reference_alerts" not in snapshot:
            return ""
        refs = snapshot.get("reference_alerts")
        refs = refs if isinstance(refs, dict) else {}
        oil = refs.get("oil")
        oil = oil if isinstance(oil, dict) else {}
        valid = self._context.policy_alerts.valid_number
        level = oil.get("level", "gray")
        names = {"red": "警惕", "yellow": "注意", "green": "未触发", "gray": "待更新"}
        price, day = oil.get("price"), oil.get("day")
        if (not isinstance(level, str) or level not in names or oil.get("fetch_error")
                or not valid(price) or price <= 0 or not self._context.policy_alerts.moment(oil.get("date"))
                or (level == "green" and (oil.get("incomplete")
                    or not all(valid(oil.get(key)) for key in ("day", "week", "month"))))):
            level = "gray"
        value = f'{price:.2f}<span class="unit">美元／桶</span>' if valid(price) else '—<span class="unit">美元／桶</span>'
        day_class = "oil-rise" if valid(day) and day > 0 else "oil-fall" if valid(day) and day < 0 else "dim"
        change = f'{day:+.2f}%' if valid(day) else "—"
        # 2026-10-30：指标格的「CLmain · 参考」「日线待更新」两行小字、状态格下面那句
        # 「原油数据待更新，暂不能判断成本压力。」全部删掉 —— 状态列只留灯名（「待更新」/「未触发」…）。
        # 灯名本身就是结论，那句话只是把灯名复述一遍，白白把这一行撑高一倍。
        return (f'<tr class="{level} oil-reference-row" id="oilAlert" data-level="{level}">'
                f'<td>WTI原油</td>'
                f'<td class="num">{value}</td><td class="num {day_class}">{change}<span class="unit">日涨跌</span></td>'
                f'<td><b class="oil-state">{names[level]}</b></td></tr>')


    def _page_notes(self, snapshot):
        """页底集中收纳说明；不改变任何指标阈值和判断规则。"""
        return f'''<section class="card page-notes" id="pageNotes" aria-label="综合判断与宏观细则">
<details class="macro-help" id="marketRiskRules"><summary>综合判断与宏观细则</summary>
<h5>综合评分</h5>
<p>{html_lib.escape(self._context.RISK_RULE_TEXT)}</p>
<h5>宏观指标细则</h5>
<p>垃圾债利差：低于黄线为绿、达到黄线为黄、达到红线为红；即使未到黄线，一周扩大至少 {self._context.HY_WEEK_YELLOW} bp 为黄、至少 {self._context.HY_WEEK_RED} bp 为红（水平仍低于黄线也照常报灯，说明另提醒观察是否持续）；达到 500 bp 会标注“压力很高”。红灯代表信用风险升温，不等于股市必跌。</p>
<p>VIX：低于绿线为绿；从绿线起为黄（紧张），从黄线起仍为黄（恐慌）；达到红线才为红。标普500距近一段高点回撤不足10%为绿、达到10%或15%为黄、达到20%为红，这些是已发生的跌幅。</p>
<p>上涨参与度：站上宏观长均线的股票不足50%为黄；不足30%时，还需近期垃圾债利差已为红，或金融压力NFCI达到或高于0，才升级红；站上宏观短均线的股票不足30%也为黄。宏观均线沿用数据源固定口径，不随个股均线设置变化。NFCI为周度：达到或高于0为黄，低于0为绿。没有可用数据时显示“无数据”，不当作安全信号。</p>
<h5>数据有效性</h5>
<p>综合判定按本轮目标交易日检查有效性：垃圾债利差、VIX、标普500最多落后7个自然日；上涨参与度最多5日；周度金融压力NFCI最多13日。未来日期、缺值或非有限数无效，缺失不当作绿。上涨参与度不足30%升级红还要求自身最多落后5日，且垃圾债利差最多落后5日并已为红，或NFCI最多落后13日且达到或高于0；垃圾债利差仅为黄不足以确认。</p>
<h5>补充参考</h5>
<p>WTI原油、10年美债收益率仅作参考，不计入五指标综合分或有效指标数。</p>
<p>10年美债收益率：相对约30个自然日前可用观测，上行至少{self._context.UST_MONTH_YELLOW} bp为参考黄，至少{self._context.UST_MONTH_WATCH}但不足{self._context.UST_MONTH_YELLOW} bp只提示、仍绿；缺历史则不判断。</p>
</details>
</section>'''


    def _position_symbols(self):
        """持仓标的符号（holdings.json 的 positions 键）。抓数时据此给持仓个股也算 20 日收益率。"""
        try:
            import io as _io
            import json as _json
            with _io.open(os.path.join(self._context.BASE, "holdings.json"), encoding="utf-8") as stream:
                h = _json.load(stream)
            return [str(s) for s in (h.get("positions") or {}).keys()]
        except Exception:
            return []


    def _cost_seed_json(self, fetched=None):
        """页面里的持仓种子：成本价来自 holdings.json 的 cost（本来就公开）；
    数量 / 现金 / 固定收益来自 position-costs.seed.json（公开入库，构建环境也读得到；
    旧名 position-costs.local.json 仍兼容）。只是「第一次打开时的兜底值」——
    本机 localStorage 和云端 position-costs.json 都优先于它。"""
        import json as _json
        items, cash, cash_extra, fixed, fixed_cost = {}, 0, 0, 0, 0
        try:
            import io as _io
            with _io.open(os.path.join(self._context.BASE, "holdings.json"), encoding="utf-8") as stream:
                h = _json.load(stream)
            for sym, c in (h.get("positions") or {}).items():
                cost = (c or {}).get("cost")
                if isinstance(cost, (int, float)) and cost > 0:
                    items[sym] = {"cost": cost, "qty": 0}
        except Exception as e:
            self._context.log(f"  ! 持仓成本种子读取失败：{e}")
        asof = ""
        p = os.path.join(self._context.BASE, "position-costs.seed.json")
        if not os.path.exists(p):
            p = os.path.join(self._context.BASE, "position-costs.local.json")
        if os.path.exists(p):
            try:
                import io as _io
                with _io.open(p, encoding="utf-8") as stream:
                    loc = _json.load(stream)
                asof = str(loc.get("asof") or "")[:10]
                for sym, q in (loc.get("positions") or {}).items():
                    if sym in items and isinstance(q, (int, float)) and q > 0:
                        items[sym]["qty"] = q
                c = loc.get("cash")
                cash = c if isinstance(c, (int, float)) else ((c or {}).get("cash") or 0)
                extra = loc.get("cash_extra", 0)
                if (isinstance(extra, (int, float)) and not isinstance(extra, bool)
                        and math.isfinite(extra) and 0 <= extra <= cash):
                    cash_extra = extra
                fi = loc.get("fixed_income") or {}
                fixed, fixed_cost = fi.get("market") or 0, fi.get("cost") or 0
            except Exception as e:
                self._context.log(f"  ! 持仓数量种子读取失败（只用成本价）：{e}")
        # rs20：持仓组合「20日对.SPX」用的快照——每只持仓自己的 20 日涨跌 + 同期基准涨跌。
        # 只提供收益率，市值加权在浏览器里按本机数量即时算（改数量不用重跑抓数）。
        rs20 = None
        try:
            pos = {str(s).upper() for s in self._context._position_symbols()}
            rs_items, bench, meta = {}, None, None
            for it in fetched or []:
                rs = (it or {}).get("rs_spy") or {} if isinstance(it, dict) else {}
                if isinstance(rs.get("bench"), (int, float)) and rs.get("days"):
                    bench, meta = rs["bench"], rs
                sym = str(it.get("symbol") or "").upper()
                if sym in pos and isinstance(rs.get("etf"), (int, float)):
                    rs_items[sym] = round(float(rs["etf"]), 4)
            if bench is not None and meta and rs_items:
                rs20 = {"asof": meta.get("end") or "", "start": meta.get("start") or "",
                        "end": meta.get("end") or "", "days": meta.get("days") or 20,
                        "bench": round(float(bench), 4),
                        "bench_name": meta.get("bench_name") or "标普500", "items": rs_items}
                # 这里刻意不打日志：本函数在页面渲染路径上执行，
                # render 期间调 log 会破坏离线测试契约（tests/mm_test_render 断言 log 未被调用）
                pass
        except Exception:
            rs20 = None
        # asof＝内置快照的日期（种子文件里的券商截图日期）：换设备取不到云端时页面要拿它说明数据来源
        return self._context.json_script({"asof": asof, "cash": cash, "cash_extra": cash_extra, "fixed": fixed,
                                          "fixed_cost": fixed_cost, "items": items, "rs20": rs20})


    def _cost_panel_assets(self, items=None):
        """仓位管理面板：CSS / JS 放在仓库根的 cost_panel.css / cost_panel.js 两个独立文件里，
    构建时内联进 index.html。文件缺失就跳过这块，页面其它部分照常生成。"""
        css = js = ""
        try:
            import io as _io
            import re as _re
            with _io.open(os.path.join(self._context.BASE, "cost_panel.css"), encoding="utf-8") as stream:
                css = stream.read()
            # 这个文件自带 <style>…</style>，但这里是插进页面已有的 <style> 里，必须剥掉标签，
            # 否则 HTML 里出现嵌套 <style>，浏览器会把后面的 </style> 当正文渲染出来
            css = _re.sub(r"</?style[^>]*>", "", css)
            with _io.open(os.path.join(self._context.BASE, "cost_panel.js"), encoding="utf-8") as stream:
                js = stream.read()
        except Exception as e:
            self._context.log(f"  ! 仓位面板资源缺失，跳过：{e}")
            return "", ""
        effective = self._context.json_script({**self._context.DEFAULT_SETTINGS, **self._context.S})
        rules_js = f"<script>window.__MM_RULES__={effective};</script>\n"
        return css, rules_js + js.replace("__MM_REAL__", self._context._cost_seed_json(items))


    def render(self, macro, items, watch_count, data_down=False, snapshot=None, dup_hidden=0):
        legacy_target_light = (any(kind == "target" and level in ("red", "yellow")
                                   for d in items for kind, level in d.get("signal_marks") or [])
                               or any(isinstance(d.get("target"), dict) and d["target"].get("anomaly")
                                      for d in items))
        snapshot = self._context._sanitize_snapshot(snapshot or {})
        items = [self._context._sanitize_snapshot_fields(item) for item in items]
        overlays = self._context.target_overlay_members(items)
        union = self._context._target_overlay_union(overlays)
        snapshot["target_overlays"] = overlays
        snapshot["signal_counts"] = {**(snapshot.get("signal_counts") or {}),
            "target": {"label": dict(self._context.SIGNAL_KINDS)["target"], "red": 0, "yellow": 0, "purple": len(union)}}
        snapshot["signal_members"] = {**(snapshot.get("signal_members") or {}),
            "target": {"red": [], "yellow": [], "purple": union}}
        snapshot["summary"] = {**(snapshot.get("summary") or {}), "purple": len(union)}
        if legacy_target_light:
            snapshot["summary"].update({level: sum(d.get("level") == level for d in items)
                                        for level in ("red", "yellow", "green", "gray")})
        risk = self._context.market_risk_summary(macro, snapshot.get("target_trade_date"))
        rows_macro = self._context._macro_rows(macro, risk)

        items = self._context._dedupe_items(items)
        signal_card = self._context._signal_counts_card(snapshot)
        items_by_group = {g["item_group"]: [] for g in self._context.GROUPS}
        for item in items:
            if item.get("group") in items_by_group:
                items_by_group[item["group"]].append(item)
        # 板块 ETF 可能因去重落在持仓／重点关注里，按代码在全部条目里找，不限本组
        items_by_symbol = {self._context.normalize_symbol(d["symbol"]): d for d in items}
        group_cards = "".join(self._context._group_card(group, items_by_group[group["item_group"]], items_by_symbol, snapshot)
                              for group in self._context._display_groups())

        # 来源与抓取间隔集中到独立名词／抓取区块；页脚只保留导航入口。
        risk_title = html_lib.escape(f"综合判断：{risk['label']} · 有效指标 {risk['valid_count']}/5")
        macro_stats = (f'<span class="market-risk-badge" id="marketRiskLight" data-level="{risk["level"]}" '
                       f'role="status" aria-label="{risk_title}">'
                       f'<i class="stat-dot {risk["level"]}" aria-hidden="true"></i>'
                       f'综合：{html_lib.escape(risk["label"])}</span>')
        dca_title = self._context.dca_badge(snapshot.get("dca_reminder"))  # 顶栏最右侧（2026-10-19：从市场风险标题挪过来）
        snapshot_json = self._context.json_script(snapshot)
        finished_iso = self._context.beijing_iso(snapshot.get("finished_at_bj") or snapshot.get("finished_at"))
        # 只到分钟：秒对看板没有意义，还把状态灯那一行撑长（2026-10-27）。
        finished_txt = finished_iso[:16].replace("T", " ") if finished_iso else "未记录"
        # 一行摘要升级为状态灯文案（首页第三排）
        init_light_phase, init_light = self._context._run_light(snapshot, finished_txt)
        # 第二排：数据时间 + 自动计划。冬夏令时只显示当日适用的那条（以当天美东是否夏令时为准）。
        ny_now = self._context.datetime.now(self._context.US_TZ)
        bj_auto = "夏令时次日 08:15" if ny_now.dst() != timedelta(0) else "冬令时次日 09:15"
        sub_line = f"数据时间 {self._context.market_data_time(snapshot)}"
        # 自动计划从头部第二排挪到页底返回看板下方；措辞与时点口径不变。
        autoplan_line = f"自动计划：美东周一至五 20:15（北京 {bj_auto}）"
        # 「在册：…」一整行按 2026-10-15 要求删除，不再在首页展示分组在册数与去重说明。

        cost_css, cost_js = self._context._cost_panel_assets(items)
        current_rules = self._context.rules_template(self._context.S, self._context._rule_constants())
        # 三块说明（预警规则／名词解释和抓取／综合判断与宏观细则）只在设置页展示；
        # 首页只留 inert 模板，供设置页按最近一次构建的原样内容读取，不另起一份措辞。
        notes_tpl = '<template id="notesTpl">' + self._context._page_notes(snapshot) + '</template>'
        # 离线/回放可把 BASE 指向仅含配置的数据目录；静态脚本仍来自实际源码目录。
        asset_base = self._context.BASE if os.path.isfile(os.path.join(self._context.BASE, "run-status.js")) else os.path.dirname(__file__)
        run_js = self._context.script_asset(asset_base, "run-status.js")

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0f1115">
<link rel="icon" type="image/png" sizes="32x32" href="./icons/favicon-32.png?v=2">
<link rel="icon" type="image/png" sizes="192x192" href="./icons/icon-192.png?v=2">
<link rel="apple-touch-icon" href="./icons/apple-touch-icon-180.png">
<link rel="manifest" href="./manifest.webmanifest">
<title>NIX-Matrix</title>
<style>
{self._context.PAGE_CSS.removesuffix(self._context.RULES_CSS)}
{cost_css}
{self._context.RULES_CSS}</style>
</head>
<body>{current_rules}<div class="wrap">
<h1 class="brand">NIX-Matrix</h1>
<div class="sub">{html_lib.escape(sub_line)}</div>

<div class="runbar">
  <button class="primary" id="btnRunNow">运行</button>
  <button id="btnCheckStatus">状态</button>
  <button id="btnHardRefresh">刷新</button>
  {dca_title}
  <script>{self._context.HARD_REFRESH_JS}</script>
</div>
<div class="statusrow">
  <div class="runlight" id="runLight" data-phase="{init_light_phase}"><i class="dot"></i><span id="runLightTxt">{init_light}</span></div>
  <div class="runlight rulelight" id="ruleLight" data-phase="idle"><i class="dot"></i><span id="ruleLightTxt">暂无法核验</span></div>
</div>
<div id="checkResult" class="check-result" role="status" hidden></div>

<div class="script-data" hidden><script id="snapshotData" type="application/json">{snapshot_json}</script></div>

{('<div class="warn">本轮标的均未取得可用报价；可能是接口故障、异常行情或抓取时间预算不足，请查看个股原因。宏观数据另行展示。</div>' if data_down else "")}

{signal_card}
<details class="card macro-card" id="macroCard">
<summary class="grp"><span class="grp-name">市场风险</span><span class="group-stats">{macro_stats}</span></summary>
<p class="macro-help" id="marketRiskReason">{html_lib.escape(risk['text'])} · 有效指标 {risk['valid_count']}/5</p>
<table>
<tr class="gray mh"><td style="color:var(--dim)">指标</td><td class="num" style="color:var(--dim)">当前</td><td class="num" style="color:var(--dim)">变化</td><td style="color:var(--dim)">状态</td></tr>
{rows_macro}
{self._context._oil_reference_row(snapshot)}
</table>
{self._context._collapse_button("macroCard", "市场风险")}
</details>

{group_cards}

<div class="acts">
<a class="board-return" id="btnReturnBoard" href="">返回看板</a>
<a class="btnlink" href="./edit.html">设置</a>
</div>
<div class="autoplan">{autoplan_line}</div>
{notes_tpl}
{run_js}
<script>window.MM_SETTINGS={{"signal_single_open":{int(self._context.S.get("signal_single_open", 1))}}};</script>
<script>{self._context.PAGE_INTERACTION_JS}</script>
{cost_js}
</div></body></html>"""
