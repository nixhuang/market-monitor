# -*- coding: utf-8 -*-
"""离线渲染测试：不联网，用假数据验证首页 UI 改版与三分组逻辑。"""
import re
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import monitor  # noqa: E402
from monitor_render import strip_css_comments, slim_snapshot  # noqa: E402
from unittest.mock import patch as _patch, mock_open as _mock_open

# 整页与可选隔离浏览器只接收合成种子，不把本机真实仓位带进测试页面。
_real_cost_seed_json = monitor._cost_seed_json
_seed_patch = _patch.object(monitor, '_cost_seed_json', return_value=monitor.json_script(
    {"asof": "", "cash": 0, "cash_extra": 0, "fixed": 0, "fixed_cost": 0, "items": {}, "rs20": None}))
_seed_patch.start()


def _row_signals(row):
    """主表只输出各指标描述，级别通过 data-signal-level 表达，不添加圆点。"""
    return monitor._signals_html(row)

# 终稿将组合分到两列；语义断言读真实节点，不以HTML属性排列或列遍历次序代表事实顺序。
from html.parser import HTMLParser
class _SignalParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.active = None; self.depth = 0
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.active is not None:
            self.depth += 1
        elif 'signal-item' in attrs.get('class', '').split() and 'data-signal-kind' in attrs:
            self.active = {'attrs': attrs, 'text': ''}; self.depth = 1
    def handle_endtag(self, tag):
        if self.active is not None:
            self.depth -= 1
            if self.depth == 0:
                self.parts.append(self.active); self.active = None
    def handle_data(self, text):
        if self.active is not None: self.active['text'] += text

def _signal_parts(markup):
    parser = _SignalParser(); parser.feed(markup); return parser.parts

def _signal_pairs(markup):
    return {(p['attrs']['data-signal-kind'], p['attrs'].get('data-signal-level', '')) for p in _signal_parts(markup)}

def _signal_text(markup):
    parts = _signal_parts(markup)
    combo = [p['text'] for p in parts if p['attrs']['data-signal-kind'] == 'boll_rsi']
    rest = [p for p in parts if p['attrs']['data-signal-kind'] != 'boll_rsi']
    rest.sort(key=lambda p: int(p['attrs'].get('data-signal-order', '-1')))
    return ' / '.join(([' · '.join(combo)] if combo else []) + [p['text'] for p in rest])

# 测试一律用代码里的默认设置，不读线上 settings.json（线上值一改，测试就会无故失败）
monitor.S.clear()
monitor.S.update(monitor.DEFAULT_SETTINGS)

def item(sym, group, level, chg=1.0, etf=False, note=""):
    return {"symbol": sym, "note": note, "price": 100.0, "chg": chg, "rsi": {6: 50.0, 12: 50.0, 24: 50.0},
            "dist_high": -5.0, "dist_low": 10.0, "vol_ratio": 1.0,
            "trigger": None, "source": "yahoo", "data_date": "2026-10-06",
            "realtime": False, "rt_ts": "", "group": group,
            "boll_up": 101.0, "boll_dn": 99.0, "etf": etf,
            "signals": ["测试信号"], "level": level}

macro = {k: {"ok": True, "name": n, "unit": "", "value": 1.0, "date": "2026-10-06",
             "prev": 1.0, "week_ago": 1.0, "delta_week": 0.0}
         for k, n in (("hy_oas", "垃圾债利差"), ("vix", "VIX"), ("sp500", "标普500"),
                      ("ust10", "10Y美债"), ("curve", "收益率曲线"), ("dxy", "美元指数"))}
macro["sp500"]["drawdown"] = -5.0

snapshot = {
    "dca_reminder": None, "run_id": "test", "request_id": "", "source_sha": "",
    "started_at": "2026-10-07T12:38:41+00:00",
    "started_at_bj": "2026-10-07T20:38:41+08:00",
    "finished_at": "2026-10-07T20:40:28+08:00",
    "finished_at_bj": "2026-10-07T20:40:28+08:00",
    "event": "push", "target_trade_date": "2026-10-06", "mode": "closed",
    "schedule": "美东周一至周五20:30；北京时间夏季次日08:30、冬季次日09:30",
    "config_files": {}, "effective_settings": {},
    "list_counts": {"positions": 2, "focus": 2, "technology": 6, "triggers": 0, "dca": 0},
    "summary": {"red": 2, "yellow": 2, "green": 4, "gray": 2, "total": 10,
                "macro_ok": 6, "stale_symbols": [], "missing_symbols": []},
    "actual_dates": {"min": "2026-10-06", "max": "2026-10-06"},
    "macro_dates": {}, "coverage": "",
}

items = [
    item("NTNX", "position", "red", chg=5.2),
    item("SPYM", "position", "green", chg=0.1, etf=True),
    item("CRSP", "position", "gray", chg=0.0),
    item("TEM", "focus", "red", chg=8.0),
    item("SOXL", "focus", "yellow", chg=3.0, etf=True),
    item("MCD", "technology", "red", chg=-4.5),
    item("XLU", "technology", "yellow", chg=2.5, etf=True),
    item("PG", "technology", "green", chg=0.3),
    item("BAC", "technology", "gray", chg=0.0),
    item("JNJ", "technology", "green", chg=0.2),
]

html = monitor.render(macro, items, 10, snapshot=snapshot, dup_hidden=2)

fail = []
checks_total = 0
def check(cond, msg):
    global checks_total
    checks_total += 1
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fail.append(msg)

check('--collapse-bg:var(--signal-collapse)' in re.search(r'#signalCounts\{([^}]+)\}', html)[1]
      and '--signal-collapse:rgba(102,134,198,.6)' in html
      and '#signalCounts .collapse-group{color:var(--white)}' in html
      and 'opacity:' not in re.search(r'\.collapse-group\{([^}]+)\}', html)[1],
      '统计收起背景40%透明，白箭头不使用整体opacity淡化')

# 空盈亏分组SSR即隐藏；长线共用统计标题选择器，不能局限于盈亏区。
_pnl_blocks = re.findall(r'<div class="signal-member-block" hidden><h5>([^<]+)</h5>'
                         r'<ul class="signal-member-list" data-g="([^"]+)"></ul></div>', html)
check(_pnl_blocks == [('浮盈', 'gain'), ('浮亏', 'loss'), ('减仓价', 'cut'), ('加仓价', 'add')],
      '盈亏到价四组保留静态骨架但空分类初始不显示标题')
_stat_heading = re.search(r'#signalCounts \.signal-member-block h5\{([^}]+)\}', monitor.PAGE_CSS)[1]
_stat_line = re.search(r'#signalCounts \.signal-member-block h5::after\{([^}]+)\}', monitor.PAGE_CSS)[1]
check('display:flex' in _stat_heading and 'align-items:center' in _stat_heading
      and all(v in _stat_line for v in ('content:""', 'flex:1 1 auto', 'height:1px', 'margin-left:8px',
                                       'background:var(--stat-rule-soft,#3a4356)'))
      and '--stat-rule-soft:#3a4356' in monitor.PAGE_CSS,
      '全部统计分组标题恢复终稿右侧1px长线，颜色仅定义于统一色板')

# 规则样式只输出一次，且仍在仓位CSS后保持最终覆盖优先级。
check(html.count(strip_css_comments(monitor.RULES_CSS)) == 1, "规则CSS整块只输出一份")
_cost_css, _cost_js = monitor._cost_panel_assets(items)
check(html.index(strip_css_comments(_cost_css)) < html.index(strip_css_comments(monitor.RULES_CSS)),
      "规则CSS仍在仓位CSS之后覆盖")

# 标题与排
check("<title>NIX-Matrix</title>" in html and '<h1 class="brand">NIX-Matrix</h1>' in html
      and "MatrixPulse" not in html,
      "标题 = NIX-Matrix（带 brand 类，上涨绿，旧名 MatrixPulse 全站清除）")
check("h1.brand{color:var(--up)}" in html, "站名用上涨绿 --up，不写死颜色")
check("市场自检" not in html, "旧标题已移除")
check("数据时间 2026-10-06 收盘（美东交易日）" in html and "本页数据更新" not in html,
      "休市顶部显示行情收盘日，不重复抓取完成时间")
check("成功 ·2026-10-07 20:40（北京）" in re.sub(r'<[^>]+>', '', html), "静态成功灯只报「成功 ·日期 时:分」，不带秒")
check("自动计划：美东周一至五 20:30（北京 " in html, "第二排自动计划")
check(("夏令时次日 08:30" in html) != ("冬令时次日 09:30" in html), "冬夏令时只出现一个")
check('id="btnRunNow"' in html and 'id="btnCheckStatus"' in html, "第三排两个按钮都在")
check('<table class="stk"><colgroup><col class="c-sym"><col class="c-px"><col class="c-target"><col class="c-earn"><col class="c-sig"></colgroup>' in html
      and 'class="px-price"' in html and 'class="lamp-rule"' not in html
      and '<td class="earn-cell">' not in html, "个股表用固定列宽 colgroup，价格/涨跌幅同列；第一排＝描边框＋灯色横线（财报独立一栏已撤）")
_near = item("NTNX", "position", "red", chg=5.2); _near["earnings"] = {"status": "ok", "date": "2026-10-09", "timing": "post", "kind": "expected"}
_far = item("TEM", "focus", "red", chg=8.0); _far["earnings"] = {"status": "ok", "date": "2026-12-20", "timing": "pre", "kind": "expected"}
from unittest.mock import patch as _patch
with _patch.object(monitor, "_bj_today", return_value="2026-10-07"):   # 财报颜色按北京时间日算，固定「今天」
    _html_e = monitor.render(macro, [_near, _far] + [d for d in items if d["symbol"] not in ("NTNX", "TEM")], 10, snapshot=snapshot, dup_hidden=2)
check('<span class="earn soon"' in _html_e and _html_e.count('<span class="earn soon"') == 1
      and '<span class="earn" data-main=' in _html_e
      and '<td class="lamp-earn"><span class="earn soon"' in _html_e,
      "D 距北京今天 2 天的财报标黄（earn soon），更远的保持蓝色；财报包在 .lamp-earn 里叠在灯色横线上方")
check(' title="' not in _html_e and " title='" not in _html_e, "首页不含任何 title 悬停提示（手机/iPad 无法悬停）")
check(".earn.soon{color:var(--lamp-yellow)" in _html_e, "财报标黄样式存在")
# 整页验证冬夏令时北京午夜：同一 D 的时段文字仍不同，但红黄窗口必须完全一致。
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
for _d in ('2026-01-15', '2026-07-15', '2026-10-11', '2027-01-01'):
    for _offset, _urgency in ((-8, ''), (-7, 'yellow'), (-2, 'yellow'), (-1, 'red'),
                             (0, 'red'), (1, 'red'), (2, '')):
        _day = _date.fromisoformat(_d) + _timedelta(days=_offset)
        for _time in ('00:00:00', '23:59:59'):
            _rows = []
            for _index, _timing in enumerate(('pre', 'post', 'during', '', 'unknown')):
                _row = item('EARN' + str(_index), 'technology', 'green', chg=0)
                _row.update(signals=[], earnings={'status': 'ok', 'date': _d, 'timing': _timing, 'kind': 'expected'})
                _rows.append(_row)
            with _patch.object(monitor, 'NOW', _datetime.fromisoformat(f'{_day}T{_time}+08:00')):
                _page = monitor.render({}, _rows, len(_rows), snapshot={})
            _card = _page.split('id="group_technology"', 1)[1].split('id="group_energy"', 1)[0]
            _css = {'red': ' urgent', 'yellow': ' soon', '': ''}[_urgency]
            check(_card.count(f'<span class="earn{_css}" ') == 5
                  and ('无异动 5 只' not in _card if _urgency else '无异动 5 只' in _card)
                  and _card.count('<tr class="green"') == 5
                  and all(_row['level'] == 'green' for _row in _rows),
                  f'北京 {_day} {_time} / D={_d}：五种时段同窗 {_urgency or "普通"}，不改行情绿灯及正确折叠')

# rulesTpl 与页底宏观说明都不能残留旧日期口径或“行灯”。
from html import unescape as _unescape
for _label, _fragment in (
        ('静态 rulesTpl', re.search(r'<template id="rulesTpl">(.*?)</template>', _html_e, re.S)[1]),
        ('名词解释模板', re.search(r'<template id="glossaryTpl">(.*?)</template>', _html_e, re.S)[1])):
    _plain = _unescape(re.sub(r'<[^>]+>', '', _fragment))
    check(not re.search(r'行灯|两个完整日|两整日|美东今天|14\s*天[^。；\n]*黄|黄[^。；\n]*14\s*天', _plain),
          _label + ' 不含行灯、两日、美东今天或14天黄旧口径')
    _window_in_rules = all(term in _plain for term in ('前一日', '当日', '后一日', '北京'))
    _window_in_glossary = (_label == '名词解释模板' and all(term in _plain
                           for term in ('快照北京运行日', '盘前／盘后不平移', '另留前一日红字')))
    check(_window_in_rules or _window_in_glossary,
          _label + ' 保留北京日历窗口正文且不依赖已删除跳转')

_visible_html = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', _html_e, flags=re.S | re.I)
check('行灯' not in _unescape(re.sub(r'<[^>]+>', '', _visible_html)), '全部用户可见说明统一使用报灯，不残留行灯')

# 终稿两端共用四区网格，成本和描述独立整行；不再沿用旧四列百分比表格。
check('grid-template-areas:"sym px tgs earn" "cost cost cost cost" "sig sig sig sig"' in _html_e,
      '主行四区及成本/描述两整行网格齐全')
check('grid-template-columns:max-content max-content max-content minmax(0,1fr)' in _html_e,
      '四区按真实内容分配空间，财报获得剩余宽度')
check('.sig-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))' in _html_e,
      '两端均保留双列描述网格')
check('data-layout="tight"' in _html_e and 'max-width:600px' in _html_e,
      '手机断点与真实量测紧凑状态存在')
_macro_u = dict(macro, ust10=dict(macro["ust10"], value=4.55, delta_week=0.12, month_ago=4.0, delta_month=0.55))
_html_u = monitor.render(_macro_u, items, 10, snapshot=snapshot, dup_hidden=2)
check('id="ust10Ref"' in _html_u and "急升 +55 bp" in _html_u and '<span class="unit">参考</span>' not in _html_u and "不计入综合灯" not in _html_u
      and '<td class="num dim">+12<span class="unit">周 bp</span></td>' in _html_u, "10Y美债参考行：一个月急升标黄，周变化按 bp 显示")
check('id="ust10Ref"' in html and "无数据或已过期" in html.split('id="ust10Ref"')[1].split("</tr>")[0],
      "10Y美债缺一个月数据时显示无数据")
_macro_b = dict(macro, breadth={"ok": True, "name": "上涨参与度", "date": "2026-10-06", "value": 47.0,
                               "pct50": 29.3, "unit": "%", "source": "History of Market"})
_html_b = monitor.render(_macro_b, items, 10, snapshot=snapshot, dup_hidden=2)
check("47.0%<span class=\"unit\">长均线</span>" in _html_b
      and "短均线：29.3%" in _html_b
      and "上宏观" not in _html_b.split('id="macroCard"', 1)[1].split('</table>', 1)[0]
      and "宏观均线沿用数据源固定口径，不随个股均线设置变化" in _html_b
      and not re.search(r'\d+日(?:均线|线)', _html_b.split('id="macroCard"', 1)[1].split('</table>', 1)[0]),
      "上涨参与度短长命名（去掉「上宏观」前缀），沿用固定宏观口径及原数据值")
# ===== 2026-10-30 市场风险表：极致压缩 + 文案删减 + 新增纳斯达克指数参考行 =====
_mcard = lambda h: h.split('id="macroCard"', 1)[1].split('</table>', 1)[0]
check(':is(#macroCard, .macro-scope) td{' in monitor.PAGE_CSS
      and 'padding-top:3px;padding-bottom:3px' in monitor.PAGE_CSS
      and ':is(#macroCard, .macro-scope) td .unit{font-size:var(--macro-aux);line-height:1.15}' in monitor.PAGE_CSS,
      "市场风险展开体macro-scope采用终稿统一字号及留白")
# 第十三轮：td.sig 重排成「首排容器 + 两个独立列容器」的守门断言（检查 CSS/JS 真源，不依赖 Temp/outputs）
check('.sig-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))' in monitor.PAGE_CSS
      and 'data-signal-flow' in monitor.PAGE_INTERACTION_JS
      and "document.addEventListener('cost:painted'" in monitor.PAGE_INTERACTION_JS
      and 'data-reflowed' not in monitor.PAGE_INTERACTION_JS,
      "终稿双列使用静态sig-cols和唯一cost:painted调度，不留旧后处理状态")
check('截至' not in _mcard(html) and '较近一年高点' not in _mcard(html) and '上宏观' not in _mcard(html)
      and 'CLmain' not in _mcard(html) and 'oil-explanation' not in _mcard(html),
      "市场风险表删掉「截至／较近一年高点／上宏观／CLmain · 参考／原油解释句」")
check('id="nasdaqRef"' in html and "无数据或已过期" in html.split('id="nasdaqRef"')[1].split("</tr>")[0],
      "纳斯达克指数参考行在无数据时显示「无数据或已过期」，不冒充安全信号")
_macro_n = dict(macro, nasdaq={"ok": True, "name": "纳斯达克指数", "value": 23918.44,
                               "date": "2026-10-06", "delta_week": 312.4, "week_ago": 23606.04,
                               "source": "Yahoo ^IXIC"})
_html_n = monitor.render(_macro_n, items, 10, snapshot=snapshot, dup_hidden=2)
check('<tr class="green" id="nasdaqRef">' in _html_n and '23,918.44' in _html_n
      and '<td class="num dim">+312<span class="unit">周 <span class="chg-num">+1.3%</span></span></td>' in _html_n
      and '<td>一周平稳</td>' in _html_n
      and '纳斯达克指数<span class="unit">2026-10-06</span>' in _html_n
      and '<span class="unit">参考</span>' not in _html_n,
      "纳斯达克指数参考行：点位两位小数、周变化＝点差＋百分比小字、上涨给「一周平稳」")
# 一周下跌分档：>=2% 黄「一周走弱」、>=4% 红「一周急跌」；上涨不出警
_nrow = lambda nd: re.search(r'<tr class="(\w+)" id="nasdaqRef".*?</tr>',
                             monitor.render(dict(macro, nasdaq=nd), items, 10,
                                            snapshot=snapshot, dup_hidden=2), re.S)
for _dw, _lv, _txt in ((312.4, "green", "一周平稳"), (-480.0, "yellow", "一周走弱"),
                       (-960.0, "red", "一周急跌")):
    _nd = {"ok": True, "name": "纳斯达克指数", "value": 23606.04 + _dw, "date": "2026-10-06",
           "week_ago": 23606.04, "delta_week": _dw}
    _m = _nrow(_nd)
    check(_m is not None and _m.group(1) == _lv and f'<td>{_txt}</td>' in _m.group(0)
          and monitor.macro_status("nasdaq", _nd)[0] == _lv,
          "纳斯达克一周 %+.1f%% → %s「%s」" % (_dw / 23606.04 * 100, _lv, _txt))
check(monitor.macro_status("nasdaq", {"ok": True, "value": 23918.44, "delta_week": None})[0] == "gray"
      and monitor.macro_status("nasdaq", {"ok": True, "value": 23918.44, "delta_week": 10,
                                          "week_ago": 0})[0] == "gray",
      "纳斯达克缺一周前数据时灰灯「一周前数据不足」，不当成安全信号")
# 标普500 补周变化：点差作主数、百分比进小字，与纳斯达克参考行同口径
_macro_s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in macro.items()}
_macro_s["sp500"] = dict(macro["sp500"], value=6800.0, week_ago=6700.0, delta_week=100.0)
_html_s = monitor.render(_macro_s, items, 10, snapshot=snapshot, dup_hidden=2)
check('<td class="num dim">+100<span class="unit">周 <span class="chg-num">+1.5%</span></span></td>' in _html_s
      and '<td class="num dim">—<span class="unit">周</span></td>' in _html_s,
      "标普500 显示一周点差＋百分比；VIX／参与度等不看周变化的仍留「—」")
# 参考行不能影响综合灯与「有效指标 N/5」：它不在 RISK_INDICATORS 里
_vc = lambda h: re.search(r'有效指标 (\d+)/5', h).group(1)
check(_vc(html) == _vc(_html_n) and monitor.RISK_INDICATORS == ("hy_oas", "vix", "sp500", "breadth", "nfci"),
      "纳斯达克只作参考，不进五项综合计分、不改有效指标数")
for _bad in ({"ok": True, "name": "纳斯达克指数", "value": float("nan"), "date": "2026-10-06"},
             {"ok": True, "name": "纳斯达克指数"}, {"ok": False, "name": "纳斯达克指数"},
             {"ok": "yes", "name": "纳斯达克指数", "value": 100, "date": "2026-10-06"}):
    check("无数据或已过期" in monitor.render(dict(macro, nasdaq=_bad), items, 10,
                                          snapshot=snapshot, dup_hidden=2).split('id="nasdaqRef"')[1].split("</tr>")[0],
          "纳斯达克值非法（%s）时按无数据处理" % str(_bad.get("value")))
# 2026-10-19：顶栏不再重复念灯级计数（统计卡里已有一份），只保留时间＋数据量；「缺失 N」是告警要留
# 2026-10-19：一切正常时只报完成时间；「N 项数据已更新」只在有缺失/不支持代码时才写
check("红2 黄2 绿4" not in html and "10 项数据已更新" not in html
      and "成功 ·2026-10-07 20:40（北京） · 缺失 2" in re.sub(r'<[^>]+>', '', html),
      "运行灯只报完成时间与缺失，正常时不写数据量与灯级计数")
check('id="runLight" data-phase="ok"' in html, "运行灯初始为绿")
check('id="ruleLight" data-phase="idle"' in html and 'id="ruleLightTxt"' in html,
      "独立规则灯初始待核验，不预先宣称最新规则生效")
check("规则按当前设置生效" not in html, "未查最新设置前没有假绿灯")
check("运行详情" not in html, "首页无运行详情")
check('class="card sumcard"' not in html and "sumline" not in html, "首页摘要卡已移除")
check('id="checkResult"' in html and html.index('id="checkResult"')<html.index('id="group_positions"'),
      "核对结果面板位于按钮附近，不在页尾")

# 分组
check(len(re.findall(r'<details[^>]*class="[^"]*group-card[^"]*"[^>]*id="group_', html)) == 14, "十四个分类均可折叠")
check(re.search(r'<details[^>]*id="group_positions"\s+open\s+data-open-locked="1">', html) is not None and 'id="group_focus">' in html and ' open>' not in html.split('id="macroCard"', 1)[1][:2], "终稿持仓父组固定初载展开，重点、市场风险默认折叠")
pos_title = html.split('id="group_positions"', 1)[1].split('</summary>', 1)[0]
check('class="stat-badge red"' in pos_title and 'class="stat-badge missing"' in pos_title and
      '红1' not in pos_title and '缺失1' not in pos_title,
      "分组警示采用灯色圆里带数字的徽章而非颜色文字（2026-10-15）")
# 2026-10-15 再调：名称与灯圈之间不再留 6px，靠得更近
check('.group-card>summary .group-stats{margin-left:0' in html and
      '.group-stats{margin-left:auto' in html, "警示紧贴分组名称右侧左对齐，宏观标题保持原样")
check('window.MM_SETTINGS={"signal_single_open":1,"group_single_open":0}' in html and
      'window.MM_SETTINGS && window.MM_SETTINGS.signal_single_open === 0' in html,
      "设置里的「灯详情只留一个打开」注入页面，默认开")
check('box-sizing:border-box;width:24px;min-width:24px;height:18px;padding:0 1px;border-radius:999px' in html
      and 'border:1.5px solid var(--dim);background:transparent' in html
      and '.stat-badge.red{border-color:var(--red)}' in html,
      "终稿空心灯圈双端24×18px，保留1.5px语义描边和白色数字")
check('id="group_technology"' in html and 'IT · AI (5)' in html, "IT 分类展示计数")
check('其他关注' not in html, "旧其他关注分类已移除")
check("<h2>个股</h2>" not in html and "<h2>ETF 基金</h2>" not in html, "个股与 ETF 不再分区")
check(all('<thead' not in table and '<th>名称</th>' not in table and 'class="h-earn"' not in table
          for table in re.findall(r'<table class="stk">.*?</table>', html, re.S)),
      "表头「名称｜财报」整行已删除（2026-10-15）")
check(monitor.STK_HEAD == '', "STK_HEAD 不再输出表头")
check("财报 美东" not in html and "下一次财报" not in html.split('id="group_positions"')[1].split('<div class="acts">')[0], "每行不再重复写「财报」二字")
check('无异动 1 只' in html and 'SPYM' in html, "无异动标的可展开查看")
pos_card = html.split('id="group_positions"')[1].split('id="group_focus"')[0]
check('NTNX' in pos_card and 'SPYM' in pos_card, "持仓包含警示及无异动标的")
focus_card = html.split('id="group_focus"')[1].split('id="group_technology"')[0]
check('TEM' in focus_card and 'SOXL' in focus_card, "重点关注仍包含 TEM/SOXL")
tech_card = html.split('id="group_technology"')[1].split('id="group_healthcare"')[0]
check('MCD' in tech_card and 'XLU' in tech_card and 'PG' in tech_card, "IT 分类包含警示与无异动标的")
check('在册：' not in html and '已隐藏' not in html and '本轮监测' not in html,
      "首页不再展示在册计数与去重说明（2026-10-15 整行删除）")
check('BAC' in tech_card, "灰色取数失败仍展示")

# 盘中口径
snap2 = dict(snapshot)
snap2["mode"] = "manual_or_config"
snap2["finished_at_bj"] = "2026-10-07T23:19:00+08:00"
html2 = monitor.render(macro, items, 10, snapshot=snap2, dup_hidden=0)
check("盘中，北京 · 仍为 2026-10-06 日线" in html2,
      "盘中运行若数据仍为前日不能冒充当天行情")
snap_live = dict(snap2, actual_dates={"min":"2026-10-07", "max":"2026-10-07"})
check("数据时间 2026-10-07 23:19:00（盘中，北京）" in monitor.render(macro, items, 10, snapshot=snap_live),
      "当前交易日盘中数据才显示时间点")
snap_utc = dict(snapshot, finished_at_bj="", finished_at="2026-10-07T12:40:28Z")
check("成功 ·2026-10-07 20:40（北京）" in re.sub(r'<[^>]+>', '', monitor.render(macro, items, 10, snapshot=snap_utc)),
      "UTC抓取完成时间正确转为北京时间（只到分钟），行情时间仍为收盘日")
snap_no_finish = {k:v for k,v in snapshot.items() if k not in ('finished_at', 'finished_at_bj')}
check("成功 ·未记录（北京）" in monitor.render(macro, items, 10, snapshot=snap_no_finish),
      "缺少完成时间不以开始时间或当前时间冒充")

# 重点关注为空
items3 = [d for d in items if d["group"] != "focus"]
snap3 = dict(snapshot)
snap3["list_counts"] = {"positions": 2, "focus": 0, "technology": 6, "triggers": 0, "dca": 0}
html3 = monitor.render(macro, items3, 8, snapshot=snap3, dup_hidden=0)
check('重点 (0)' in html3 and '暂无标的，去设置页录入' in html3, "重点关注空态提示")

# 抓取失败文案
snap4 = dict(snapshot)
snap4["summary"] = dict(snapshot["summary"], stale_symbols=["NTNX", "MCD"])
html4 = monitor.render(macro, items, 10, snapshot=snap4)
check("当日行情未取得 2 只：NTNX、MCD" in html4 and "规则未完全生效" not in html4, "旧日线不被误报为完全没数据")
check('id="runLight" data-phase="bad"' in html4, "运行灯初始为红")
check('id="ruleLight" data-phase="idle"' in html4, "行情失败时规则灯仍独立待核验")

# 旧基本面输入必须完全忽略，不改变行情灯、排序或无异动折叠。
import copy as _copy
_clean_items = _copy.deepcopy(items)
_legacy_items = _copy.deepcopy(items)
for _row in _legacy_items:
    _row['fund'] = {'level': 'red', 'hits': ['旧风险 <script>alert(1)</script>'], 'period': '6/30/2026'}
    _row['fund_cache_note'] = '旧缓存基本面待更新'
_legacy_before = _copy.deepcopy(_legacy_items)
_baseline = monitor.render(macro, _clean_items, 10, snapshot=snapshot, dup_hidden=0)
html5 = monitor.render(macro, _legacy_items, 10, snapshot=snapshot, dup_hidden=0)
check(html5 == _baseline, "旧基本面红灯及缓存说明不改变全页输出与排序")
check(_legacy_items == _legacy_before, "渲染旧基本面输入不突变原数据")
check(re.findall(r"无异动 (\d+) 只", html5) == re.findall(r"无异动 (\d+) 只", _baseline),
      "旧基本面红灯不改变无异动折叠计数")
_legacy_visible = re.sub(r'<template\b[^>]*>.*?</template>', '', html5, flags=re.S)
check(not any(_text in _legacy_visible for _text in ('基本面', 'fund-detail', 'fund-data-note', 'data-fund-', '旧风险', 'alert(1)')),
      "旧基本面内容、徽章、已读属性及脚本均不回显")
check('鼠标悬停徽章' not in html5, "页脚不使用悬停说明")
check(html5.count('<tr class="red"') == html.count('<tr class="red"'), "旧基本面红灯不影响行情红行数量")
check('id="runLight" data-phase="ok"' in html5, "旧基本面红灯不影响运行状态灯")
check('垃圾债利差' in html5 and '高收益债利差' not in html5 and '跑路价签' not in html5,
      "正式生成首页仅使用垃圾债利差新名称")
check('<div class="foot">' not in html5 and 'href="#signalRules"' not in html5,
      '页底重复规则入口已删除，完整规则模板仍保留')
head5 = html5.split('</head>', 1)[0]
check('rel="icon"' in head5 and './icons/favicon-32.png' in head5, "页头有网页图标（相对路径）")
check('rel="apple-touch-icon" href="./icons/apple-touch-icon-180.png?v=3"' in head5, "页头有 apple-touch-icon（带版本号，换图后手机不吃旧缓存）")
check('rel="manifest" href="./manifest.webmanifest"' in head5 and 'name="theme-color"' in head5, "页头有 manifest 与 theme-color")
import json as _json, os as _os
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_mf = _json.load(open(_os.path.join(_root, 'manifest.webmanifest'), encoding='utf-8'))
check(all(_os.path.exists(_os.path.join(_root, i['src'].split('?', 1)[0])) for i in _mf['icons']),
      "manifest 里的图标文件都存在")
check(any('maskable' in (i.get('purpose') or '') for i in _mf['icons']),
      "manifest 提供 Android 自适应图标（maskable）")
check(all(_os.path.exists(_os.path.join(_root, 'icons', n)) for n in ('favicon-32.png', 'apple-touch-icon-180.png')), "页头引用的图标文件存在")
# iOS 的「添加到主屏幕」只认 apple-touch-icon，且对调色板/带 alpha 通道的 PNG 会失效退回兜底字母图标。
# 直接读 PNG IHDR 的 color type：2=真彩 RGB、3=调色板、6=RGBA；必须是 2，否则压缩图标时会被拦下。
def _png_color_type(path):
    with open(path, 'rb') as _f:
        return _f.read(26)[25]
check(_png_color_type(_os.path.join(_root, 'icons', 'apple-touch-icon-180.png')) == 2,
      "apple-touch-icon 必须是无 alpha 的真彩 PNG（iOS 主屏图标不认调色板/透明 PNG）")
_icons_dir = _os.path.join(_root, 'icons')
_bad = [n for n in _os.listdir(_icons_dir) if n.endswith('.png') and _png_color_type(_os.path.join(_icons_dir, n)) != 2]
check(not _bad, "所有图标都必须是无 alpha 的真彩 PNG，调色板/透明 PNG 会让 iOS 主屏图标退回字母：" + ",".join(_bad))
check(sum(_os.path.getsize(_os.path.join(_root, 'icons', n)) for n in _os.listdir(_os.path.join(_root, 'icons'))) < 400 * 1024, "图标总体积小于 400KB")
_cost_css = open(_os.path.join(_root, 'cost_panel.css'), encoding='utf-8').read()
check('.quiet-list{margin:8px 0;border-top:1px solid var(--quiet-rule);border-bottom:1px solid var(--quiet-rule)}' in monitor.PAGE_CSS,
      "「无异动 N 只」整体作为可点开的条目，上下各有一条细横线")
check('id="dayPnlCard"' in html and 'class="dual-body macro-scope"' in html and 'data-owner="day"' in html,
      "当日盈亏卡与展开清单使用终稿静态dualRow骨架")
check('.quiet-list .stk tr[id^="s-"]' not in _cost_css
      and not re.search(r'\.cost-contrib li\s*\{[^}]*\bborder(?:-(?:top|right|bottom|left))?\s*:', _cost_css, re.S),
      "细框不再加在清单内部的股票行／盈亏贡献的每一行上（那是条目内部，不是可展开范围）")
check('<div class="foot">' not in html5 and 'href="#marketRiskRules"' not in html5
      and 'href="#glossary-fetch"' not in html5,
      '页底三个重复入口已删，规则与来源仍在折叠正文')
_glossary5 = re.search(r'<template id="glossaryTpl">(.*?)</template>', html5, re.S)[1]
check(all(term in _glossary5 for term in ('Yahoo → stooq → Nasdaq', 'BAMLH0A0HYM2',
      'Cboe官方VIX日线优先', '芝加哥联储原始周度数据', 'History of Market')),
      "旧来源迁入新稿且VIX来源与当前实现一致")
# 页面不再展示利率决议；旧helper及后台缓存接口保留兼容，不影响风险灯。
import copy as _copy
from unittest.mock import patch as _patch
import policy_alerts as _policy
_ref_now = _policy.clock(_policy.moment("2026-10-11T12:00:00+08:00"))
_fed = {"action": "maintain", "low": 3.5, "high": 3.75,
        "date": "2026-09-16T14:00:00-04:00", "published_date": "2026-09-16",
        "released_at": "2026-09-16T14:00:00-04:00", "next_release": "2026-10-28T14:00:00-04:00",
        "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm",
        "checked_at": _ref_now.isoformat(), "fetch_error": ""}
_refsnap = _copy.deepcopy(snapshot)
_refsnap["reference_alerts"] = {"policy": {"fed": _fed}, "assessed_at": _ref_now.isoformat(),
                               "oil": {"level": "gray", "text": "原油数据待更新"}}
_refhtml = monitor.render(macro, _copy.deepcopy(items), 10, snapshot=_refsnap)
_refcard = monitor._policy_details(_refsnap)
check('data-status="available"' in _refcard and "维持不变" in _refcard
      and "2026-09-16" in _refcard and "stat-dot" not in _refcard, "折叠详情保留决议动作和日期，不显示风险灯")
check(all(term not in _refhtml for term in ('id="policyAlert"', 'id="referenceAlerts"',
      '美联储最近利率决议', '正式消息', '.policy-reference')),
      "真实全页移除常驻决议卡片、正式消息标签及专属样式")
check(not re.search(r'<details[^>]*id="policyAlertDetails"[^>]*\bopen\b', _refhtml),
      "决议详情无open属性，不常驻展示正文")
check(not any(term in _refhtml for term in ("核心CPI", "SEP", "季度预测", "点阵图", "通胀／利率")), "页面移除CPI与预测依赖及旧标签")
check('id="policyAlertDetails"' not in _refhtml, "页面规则不再插入美联储决议详情，旧helper保留兼容")
check("3.50%–3.75%" in _refcard and "2026-09-17 02:00" in _refcard
      and "到下次官方公布时点前复用" not in _refhtml and 'href="#fetch-reference"' not in _refhtml
      and "正常缓存24小时" not in _refhtml and "临时决议可能延迟发现" not in _refhtml,
      "旧helper数据不变，规则正文已移除决议来源缓存与日程说明")
check(_refsnap["summary"] == snapshot["summary"] and 'id="oilAlert"' in _refhtml, "原计数不变，WTI保留")
_pending = _copy.deepcopy(_refsnap)
_pending["reference_alerts"]["policy"]["fed"]["fetch_error"] = "模拟解析错误 <script>bad()</script>"
check('data-status="pending"' in monitor._policy_details(_pending) and "2026-09-16" in monitor._policy_details(_pending),
      "失败仍保留旧决议日期并显示待更新")
check("&lt;script&gt;bad()&lt;/script&gt;" in monitor._policy_details(_pending), "错误详情安全转义")
_empty = {"reference_alerts": {"policy": {"fed": {}}}}
check('data-status="unavailable"' in monitor._policy_details(_empty), "首次无数据不伪造消息")
with _patch.object(monitor.policy_alerts, "update_policy", return_value={"fed": _fed}) as _load, \
     _patch.object(monitor, "fetch_history", return_value=None), \
     _patch.object(monitor, "fred_series", side_effect=AssertionError("决议不能依赖FRED")):
    _built = monitor.build_reference_alerts(_refsnap["reference_alerts"])
    check(_load.call_args.args == (_refsnap["reference_alerts"]["policy"], monitor.NOW)
          and not _load.call_args.kwargs and "cpi" not in _built["policy"], "取数入口使用新接口且不抓CPI/FRED")

# 损坏缓存必须经过全页渲染验证，不能只测局部卡片；所有样本均为离线假数据。
_oil = {"level": "green", "price": 70.0, "day": 1.0, "week": 2.0, "month": 3.0,
        "date": "2026-10-09", "text": "原油暂未触发快速上涨警示。"}
_bad_values = (None, False, True, 0, 1, "", "损坏", [], ["损坏"])


def check_reference_render(refs, label, status="unavailable", oil_level="gray"):
    snap = _copy.deepcopy(snapshot)
    snap["reference_alerts"] = refs
    render_macro, render_items = _copy.deepcopy(macro), _copy.deepcopy(items)
    before = _copy.deepcopy((snap, render_macro, render_items))
    page = monitor.render(render_macro, render_items, 10, snapshot=snap)
    details = monitor._policy_details(snap)
    oil_row = page.split('id="oilAlert"', 1)[1].split('</tr>', 1)[0]
    check(f'data-status="{status}"' in details
          and 'id="policyAlert"' not in page and 'id="referenceAlerts"' not in page
          and not re.search(r'<details[^>]*\bopen\b', details)
          and f'data-level="{oil_level}"' in oil_row
          and (status == "available" or "待更新" in details or "尚未取得" in details)
          and (oil_level != "gray" or "待更新" in oil_row)
          and 'id="policyAlertDetails"' not in page and '美联储' not in monitor._page_notes(snap)
          and 'id="group_positions"' in page
          and page.endswith('</body></html>')
          and (snap, render_macro, render_items) == before,
          label + "：全页完成、状态保守且输入不突变")
    return page


_corrupt_refs = []
for _layer in ("reference_alerts", "policy", "fed", "oil"):
    for _bad in (*_bad_values, {}):
        _refs = _copy.deepcopy(_refsnap["reference_alerts"])
        if _layer == "reference_alerts":
            _refs = _copy.deepcopy(_bad)
        elif _layer == "fed":
            _refs["policy"]["fed"] = _copy.deepcopy(_bad)
        else:
            _refs[_layer] = _copy.deepcopy(_bad)
        _label = f"损坏 {_layer}={_bad!r}"
        _status = "available" if _layer == "oil" else "unavailable"
        check_reference_render(_refs, _label, _status)
        _corrupt_refs.append((_label, _refs, _status))

for _layer in ("policy", "fed", "oil"):
    _refs = _copy.deepcopy(_refsnap["reference_alerts"])
    del (_refs["policy"] if _layer == "fed" else _refs)[_layer]
    check_reference_render(_refs, f"缺失 {_layer}", "available" if _layer == "oil" else "unavailable")

for _action in (*_bad_values, {}, {"action": "maintain"}, ["maintain"]):
    _refs = _copy.deepcopy(_refsnap["reference_alerts"])
    _refs["policy"]["fed"].update(action=_action, checked_at="")
    _label = f"损坏 action={_action!r}"
    check_reference_render(_refs, _label)
    _corrupt_refs.append((_label, _refs, "unavailable"))

for _field in ("action", "low", "high", "date", "url"):
    for _bad in (None, [], {}, True, "损坏"):
        _refs = _copy.deepcopy(_refsnap["reference_alerts"])
        _refs["policy"]["fed"][_field] = _bad
        check_reference_render(_refs, f"损坏 fed.{_field}={_bad!r}")
    _refs = _copy.deepcopy(_refsnap["reference_alerts"])
    del _refs["policy"]["fed"][_field]
    check_reference_render(_refs, f"缺失 fed.{_field}")

for _field in ("level", "price", "day", "week", "month", "date"):
    for _bad in (*_bad_values, {}):
        # 0/1 是有效涨幅；价格为正、日期可解析才可用。
        if _field in ("price", "day", "week", "month") and type(_bad) in (int, float) and (_field != "price" or _bad > 0):
            continue
        _refs = _copy.deepcopy(_refsnap["reference_alerts"])
        _refs["oil"] = dict(_oil, **{_field: _bad})
        _page = check_reference_render(_refs, f"损坏 oil.{_field}={_bad!r}", "available")
        _row = _page.split('id="oilAlert"', 1)[1].split('</tr>', 1)[0]
        check("未触发" not in _row, f"损坏 oil.{_field} 不保留旧安全文案")
    _refs = _copy.deepcopy(_refsnap["reference_alerts"])
    _refs["oil"] = {k: v for k, v in _oil.items() if k != _field}
    check_reference_render(_refs, f"缺失 oil.{_field}", "available")

for _level, _week, _month in (("green", 2.0, 3.0), ("yellow", 12.0, 22.0), ("red", 25.0, None)):
    _refs = _copy.deepcopy(_refsnap["reference_alerts"])
    _refs["oil"] = dict(_oil, level=_level, week=_week, month=_month)
    check_reference_render(_refs, f"有效原油 {_level} 保持原灯色", "available", _level)

# 用真实 update_policy + mock 抓取，覆盖 unavailable 正常返回和入口异常两条路径。
with _patch.object(monitor, "NOW", _ref_now), \
     _patch.object(_policy, "fetch_text", side_effect=OSError("模拟离线失败")) as _fetch_text, \
     _patch.object(monitor, "fetch_history", return_value=None), \
     _patch.object(monitor, "yahoo_history", side_effect=AssertionError("不应抓取原油合约")), \
     _patch.object(monitor, "log") as _log:
    for _label, _refs, _status in _corrupt_refs:
        _before = _copy.deepcopy(_refs)
        _fetch_count = _fetch_text.call_count
        _built = monitor.build_reference_alerts(_refs)
        check(_refs == _before and _built["policy"]["summary"]["status"] == _status
              and (not _label.startswith("损坏 action=") or _fetch_text.call_count == _fetch_count + 1),
              "build " + _label + "：输入不突变、判定正确、损坏动作实际尝试更新")
        check_reference_render(_built, "build→render " + _label, _status)
    check(_fetch_text.call_count > 0 and not _log.called,
          "真实 update_policy 消化取数错误并正常返回，不依靠入口异常兜底掩盖问题")

    _fetch_text.reset_mock()
    _old = _copy.deepcopy(_refsnap["reference_alerts"])
    _old["policy"]["fed"]["checked_at"] = "2026-09-17T12:00:00+08:00"
    _before = _copy.deepcopy(_old)
    _built = monitor.build_reference_alerts(_old)
    check(_fetch_text.call_count == 0 and _old == _before,
          "有效旧缓存未到公布时间仍零网络请求，不改变持久缓存策略")
    check_reference_render(_built, "build→render 有效旧缓存", "available")

    _old["policy"]["fed"]["next_release"] = "2026-10-10T14:00:00-04:00"
    _before = _copy.deepcopy(_old)
    _built = monitor.build_reference_alerts(_old)
    _page = check_reference_render(_built, "build→render 到期抓取失败", "pending")
    _retained = monitor._policy_details({'reference_alerts': _built})
    check(_fetch_text.call_count == 1 and _old == _before and "维持不变" in _retained
          and "3.50%–3.75%" in _retained and "2026-09-16" in _retained,
          "到期失败保留有效旧消息、日期和区间，不突变原缓存")
    _before = _copy.deepcopy(_built)
    _retry = monitor.build_reference_alerts(_built)
    check(_fetch_text.call_count == 1 and _built == _before,
          "失败后一小时内继续复用缓存，不因防护修改而重复抓取")
    check_reference_render(_retry, "build→render 失败重试窗口", "pending")

    for _label, _refs, _status in [*_corrupt_refs, ("有效旧消息", _old, "available")]:
        _before = _copy.deepcopy(_refs)
        with _patch.object(_policy, "update_policy", side_effect=RuntimeError("模拟入口异常")), \
             _patch.object(monitor, "log"):
            _built = monitor.build_reference_alerts(_refs)
        _expected = "pending" if _status == "available" else "unavailable"
        check(_refs == _before, "入口异常 " + _label + "：原缓存不突变")
        _page = check_reference_render(_built, "入口异常→render " + _label, _expected)
        if _status == "available":
            _retained = monitor._policy_details({'reference_alerts': _built})
            check("维持不变" in _retained and "3.50%–3.75%" in _retained and "2026-09-16" in _retained,
                  "入口异常 " + _label + "：保留有效旧消息和来源日期")

# 完整捕获主行属性，允许新增属性；保留原因及报价必须各出现一次且值精确一致。
def rendered_stock_row(page, row, keep):
    matches = re.findall(r'<tr\b([^>]*\bid="' + re.escape(monitor._stock_anchor_id(row['symbol']))
                         + r'"[^>]*)>(.*?)</tr>', page, re.S)
    check(len(matches) == 1, row['symbol'] + '主表行唯一且完整捕获')
    attrs, body = matches[0] if len(matches) == 1 else ('', '')
    for name, value in (('data-quiet-keep', str(keep).lower()),
                        ('data-price', '' if row.get('price') is None else str(row['price'])),
                        ('data-prev-close', '' if row.get('prev_close') is None else str(row['prev_close']))):
        check(re.findall(r'\b' + name + r'="([^"]*)"', attrs) == [value],
              row['symbol'] + '主行' + name + '精确匹配' + repr(value))
    classes = re.findall(r'\bclass="([^"]*)"', attrs)
    check(len(classes) == 1, row['symbol'] + '主行class属性唯一')
    check('signal-dot' not in body, row['symbol'] + '完整主表行零圆点')
    return classes[0].split() if len(classes) == 1 else [], body


# 发布行基础灯只排除可已读加减仓贡献，原marks/灯及紫/折叠保持完整。
for _side in ('add', 'cut', 'both'):
    for _extra in (None, 'yellow', 'red'):
        _al = monitor._Alerts()
        _x = monitor.SimpleNamespace(cfg={'trigger': 100 if _side != 'cut' else None,
                                         'reduce': 100 if _side != 'add' else None},
                                     price=100, group='position')
        # 终稿价位常驻描述不再调用或生成加减价技术灯。
        _x.trig, _x.reduce = _x.cfg['trigger'], _x.cfg['reduce']
        if _extra:
            _al.add('vol_ratio', '量比 5.0×（2026-10-26）', _extra)
        _row = item('BASE' + _side.upper() + str(_extra), 'position', _al.level, chg=0)
        _row.update(signals=_al.signals, signal_marks=_al.marks, trigger=_x.trig, reduce=_x.reduce,
                    target=monitor.evaluate_target(100, 100, {'kind': 'manual', 'price': 101}, 'position', 3))
        _before = _copy.deepcopy(_row)
        _fragment = monitor._stock_row(_row, {})
        _attrs = _fragment.split('><td', 1)[0]
        check('data-base-level=' not in _attrs
              and 'class="' + (_extra or 'green') + ' pur pur-hit"' in _attrs,
              _side + str(_extra) + '价位不造红，技术灯独立且紫状态保留')
        check(_row == _before, _side + str(_extra) + '渲染不改判灯/marks/目标事实')
for _level, _marks in (('red', []), ('red', [('add_price', 'yellow')]),
                       ('yellow', []), ('green', [('boll', 'gray')]), ('gray', [('add_price', 'red')])):
    _row = item('BASELEGACY', 'position', _level, chg=0)
    _row.update(signal_marks=_marks, signals=[], trigger=100)
    check('class="' + _level + '"' in monitor._stock_row(_row, {}),
          _level + repr(_marks) + '无来源旧灯和缺失灰不能凭价位猜测降绿')

# 目标/共识40/90：状态与主表折叠分别验证，旧基本面及财报三日回归保持不动。
_target_cases = []
for _symbol, _group, _price, _prev, _state in (
        ('TLOW', 'technology', 139.999, 100, 'low'),
        ('TSPACE', 'technology', 140, 100, 'space'),
        ('TMID', 'materials', 189.999, 100, 'space'),
        ('THIGH', 'healthcare', 190, 100, 'high'),
        ('TOVER', 'energy', 250, 100, 'high'),
        ('THIT', 'position', 101, 100, 'hit'),
        ('TBOTH', 'focus', 190, 200, 'hit')):
    _row = item(_symbol, _group, 'green', chg=0)
    _row.update(data_date='2026-10-20', prev_close=_prev, signals=[], signal_marks=[], range52={'low': 50, 'high': 150, 'position': 50, 'scope': '52周',
                'from': '2025-10-20', 'to': '2026-10-20'},
                target=monitor.evaluate_target(100, _prev,
                    {'kind': 'consensus', 'price': _price, 'failed': False}, _group, 3))
    _row['purple'] = bool(_row['target']['hit'] or _row['target']['space_hit'])
    _target_cases.append((_row, _state))
_target_rows = [row for row, _ in _target_cases]
for _symbol, _target, _price, _level in (
        ('TFAILOLD', {'kind': 'consensus', 'price': 250, 'failed': True}, 100, 'green'),
        ('TFAILNEW', {'kind': 'consensus', 'price': None, 'failed': True}, 100, 'green'),
        ('TNOQUOTE', {'kind': 'consensus', 'price': 250, 'failed': False}, None, 'gray')):
    _row = item(_symbol, 'technology', _level, chg=0)
    _row.update(price=_price, prev_close=100, signals=[], signal_marks=[], purple=False,
                target=monitor.evaluate_target(_price, 100, _target, 'technology', 3))
    _target_rows.append(_row)
_target_before = _copy.deepcopy(_target_rows)
with _patch.object(monitor, 'global_dca', return_value=None):
    _target_snapshot = monitor.build_snapshot({}, _target_rows, {}, group_counts={})
    _target_page = monitor.render({}, _target_rows, len(_target_rows), snapshot=_target_snapshot)

for _row, _state in _target_cases:
    _classes, _fragment = rendered_stock_row(_target_page, _row, _state != 'low')
    check('lvtag tgt ' + _state in _fragment,
          '40/90胶囊状态 ' + _row['symbol'] + '=' + _state)
    _target_class = {'space': 'pur-space', 'high': 'pur-high', 'hit': 'pur-hit'}.get(_state)
    check('green' in _classes and (_target_class in _classes if _target_class else 'pur' not in _classes),
          '目标行独立状态 ' + _row['symbol'] + '=' + (_target_class or '无'))

for _row in _target_rows[-3:]:
    _classes, _fragment = rendered_stock_row(_target_page, _row, False)
    check(_row['level'] in _classes and 'pur' not in _classes,
          _row['symbol'] + '失败不新增紫色状态或改变灯级')
    check('lvtag tgt bad' in _fragment and '获取失败' in _fragment and '+150%' not in _fragment,
          _row['symbol'] + '失败灰胶囊不展示旧空间')
_tech = _target_page.split('id="group_technology"', 1)[1].split('id="group_energy"', 1)[0]
_tech_main, _tech_quiet = _tech.split('<details class="quiet-list">', 1)
check('id="s-tspace"' in _tech_main and 'id="s-tlow"' in _tech_quiet
      and 'id="s-tfailold"' in _tech_quiet and 'id="s-tfailnew"' in _tech_quiet,
      '跨组空间>=40留主表，低空间及失败不提升可见性')
check(_target_rows == _target_before, '目标全页渲染不突变输入')
check(_target_snapshot['summary']['purple'] == 6
      and _target_snapshot['signal_counts']['target']['purple'] == 6,
      '空间5达到2的union紫计数是6而非7')
check({r['symbol'] for r in _target_snapshot['target_overlays']['space']}
      == {'TSPACE', 'TMID', 'THIGH', 'TOVER', 'TBOTH'}
      and {r['symbol'] for r in _target_snapshot['target_overlays']['reached']} == {'THIT', 'TBOTH'},
      '全页快照空间和达到名单成员正确且允许重叠')
_target_panel = _target_page.split('class="text-only target-purple"', 1)[1].split('</details>', 1)[0]
check(bool(re.search(r'空间\s*5\s*只[\s　]*达到\s*2\s*只', _target_panel))
      and not re.search(r'紫灯\s*\d+\s*只', _target_panel), '空间/达到双计数标题替代旧紫灯标题')
check(_target_panel.count('<ul') == 2 and _target_panel.count('class="signal-name-lamp ') == 7
      and 'signal-member-icon' not in _target_panel
      and '跌破 190' in _target_panel and _target_panel.count('href="#s-tboth"') == 2,
      '展开两段竖条名单保留原紫色/紫红、具体达到方向及重叠跳转')
check('data-kind="target_purple"' in _target_page and '>目标价</button>' not in _target_page
      and '>共识价</button>' not in _target_page, '底部union按钮保留而目标红黄表行移除')
check('疑似数据异常' not in _target_panel and 'lvtag tgt high' in _target_page,
      '>100只作高空间不复活异常红')
for _state in ('space', 'high', 'hit'):
    check(not re.search(r'tr\.pur-' + _state
                        + r'(?::has\([^}]*\))?\s*(?:td\.sig|\+tr\.range52-row>td)\{', monitor.PAGE_CSS)
          and '--pstripe' not in monitor.PAGE_CSS,
          _state + '主行及52周续行均不再绘制右紫条')
# 2026-10-30：红灯条宽度跟黄灯条对齐（6 → 4px），左内边距保持 14px 不动（文字左缘不变）。
# 终稿：主表左灯条固定双端几何，不和墨迹测量分区线共用变量，也不延伸至52周行；
# 统计面板名单仍用全局 tr.red/yellow/green td:first-child 的 inset 灯条。
for _level, _width in (('red', 4), ('yellow', 4), ('green', 2)):
    check('table.stk tr.' + _level + '[data-symbol]{--lamp-line:var(--' + _level + ')}' in monitor.PAGE_CSS
          and 'tr.' + _level + '+tr.range52-row>td{--lc' not in monitor.PAGE_CSS
          and 'table.stk tr[data-symbol]::before{content:"";position:absolute;left:0;top:9.5px;width:2px;height:41.7px;' in monitor.PAGE_CSS
          and 'table.stk tr[data-symbol]::before{top:7px;height:42.7px}' in monitor.PAGE_CSS
          and 'top:var(--vl-top);height:var(--vl-h);width:1px;background:var(--section-line)' in monitor.PAGE_CSS,
          _level + '主表左灯条保留固定双端几何，与分区线独立且不延伸52周行')
# 2026-10-30：成本价搬到「代码 + 备注名」下面那一排，跟加／减仓价胶囊共用 .lvs 容器，
# 只出文字不做胶囊 —— 规则里不能出现 border / border-radius，否则又变成一颗胶囊。
_cost_inline = re.search(r'\.cost-inline\{([^}]*)\}', monitor.PAGE_CSS)
check(_cost_inline is None and '.ep-grid' in _cost_css,
      '成本价只在终稿五列交易编辑器维护，不恢复主表旧cost-inline')
check('table.stk tr[data-symbol]>td.cost-line{' in monitor.PAGE_CSS
      and '[data-pnl="loss"]{background:var(--pnl-loss-bg)}' in monitor.PAGE_CSS
      and '[data-pnl="gain"]{background:var(--pnl-gain-bg)}' in monitor.PAGE_CSS,
      '主表盈亏淡底使用独立cost-line，不改变基础灯级')
from urllib.parse import unquote as _unquote
import xml.etree.ElementTree as _ET
# 2026-11-03：low（未达到任何紫灯标准）只留紫字、不画虚线外框，虚线只留给 bad（获取失败）。
_target_dash_rule = re.search(r'\.lvtag\.tgt\.bad::after\{([^}]*)\}', monitor.PAGE_CSS)
_target_dash_url = re.search(r'url\("(data:image/svg\+xml,[^"]+)"\)', _target_dash_rule[1]) if _target_dash_rule else None
_target_dash_rect = (_ET.fromstring(_unquote(_target_dash_url[1].split(',', 1)[1]))
                     .find('{http://www.w3.org/2000/svg}rect')) if _target_dash_url else None
check(_target_dash_rect is not None and all(_target_dash_rect.get(key) == value for key, value in (
          ('stroke', '#8b93a1'), ('stroke-width', '1'), ('stroke-dasharray', '3 5'), ('rx', '4'), ('fill', 'none')))
      and 'inset:-1px' in _target_dash_rule[1] and 'pointer-events:none' in _target_dash_rule[1],
      "bad（获取失败）保留1px灰色SVG短线3px间隔5px的4px圆角边框；low 已改为无框（见下条）",)
_target_base = re.search(r'\.lvtag\.tgt\{([^}]*)\}', monitor.PAGE_CSS)
_bad_after = re.search(r'\.lvtag\.tgt\.bad::after\{([^}]*)\}', monitor.PAGE_CSS)
check(_target_base is not None and 'border:0' in _target_base[1]
      and 'background:transparent' in _target_base[1] and 'var(--purple)' in _target_base[1]
      and not re.search(r'\.tgt\.low::after\{[^}]*background:url', monitor.PAGE_CSS),
      '终稿low继承无框紫字，不再需要历史覆盖规则')
check(_bad_after is not None and 'background:url(' in _bad_after[1]
      and 'pointer-events:none' in _bad_after[1]
      and 'td.tgs:has(.tgt.hit)::before' in monitor.PAGE_CSS,
      '终稿只有失败目标用SVG虚线，达到目标由独立整区伪元素绘制')

# 共识只在描述列隐藏；完整信号、空间/达到统计与原胶囊仍保留。
for _row in _target_rows:
    _body = re.search(r'<tr\b[^>]*\bid="' + re.escape(monitor._stock_anchor_id(_row['symbol']))
                      + r'"[^>]*>(.*?)</tr>', _target_page, re.S)[1]
    check('<td class="sig"></td>' in _body and 'data-signal-kind=' not in _body,
          _row['symbol'] + '仅自动共识时描述列保留静态空列，不伪造信号')
_description_rows = _copy.deepcopy(_target_rows)
_technical_texts = ['触及布林下轨▼ → 黄', 'RSI 三条超卖', '贴近200日均线']
for _row in _description_rows:
    _target = _row['target']
    _row['signals'] = _technical_texts + [
        text for text in (_target.get('reached_detail'), _target.get('space_detail')) if text]
    if _target['failed']:
        _row['signals'] += ['共识价 250，空间 +150%', '今日上穿共识价 250',
                            '旧共识价 250，获取失败', '共识获取失败']
    _row['signal_marks'] = [('boll', 'yellow'), ('rsi', 'red'), ('ma', 'yellow')]
    if _row['purple']:
        _row['signal_marks'].append(('target', 'purple'))
    _row['level'] = 'red'
    _direct_before = _copy.deepcopy(_row)
    _sig = monitor._signals_html(_row)
    _expected_sig = monitor._signals_html({**_row, 'signals': _technical_texts, 'target': {}})
    check(_sig == _expected_sig and _row == _direct_before,
          _row['symbol'] + '成功/失败共识仅从描述过滤，原技术文字及输入不变')
    if _row['purple']:
        _applied = monitor._apply_target(_row)
        check(_applied['signals'] == _row['signals']
              and _applied['signal_marks'] == _row['signal_marks'],
              _row['symbol'] + '有效共识内部signals/marks完整保留')

_legacy_consensus = _copy.deepcopy(_description_rows[-3])
_legacy_consensus.update(target={'kind': 'consensus', 'price': 250, 'failed': True,
                                  'space': 150, 'hit': True, 'month': 10}, purple=True)
_legacy_before = _copy.deepcopy(_legacy_consensus)
_legacy_html = monitor._stock_row(_legacy_consensus, {})
check(re.search(r'<td class="sig">(.*?)</td>', _legacy_html, re.S)[1] == _expected_sig
      and re.search(r'<td class="sym">(?:<span class="sym-box \w+">)?(.*?)</td>', _legacy_html, re.S)[1]
      .startswith('TFAILOLD')
      and 'signal-dot' not in _legacy_html and monitor._signal_lamps(_legacy_consensus) == ''
      and 'lvtag tgt bad' in _legacy_html and 'pur-high' not in _legacy_html
      and 'pur-hit' not in _legacy_html,
      '失效旧共识经过stock_row迁移后仅保留原技术描述与失败灰胶囊')
check(_legacy_consensus == _legacy_before, '失效旧共识渲染不突变原signals/marks或旧目标')

_manual_rows = []
for _symbol, _group, _target_price in (('MANHIGH', 'technology', 250),
                                       ('MANLOW', 'technology', 120),
                                       ('MANHIT', 'position', 101)):
    _row = item(_symbol, _group, 'green', chg=0)
    _row.update(prev_close=100, signal_marks=[],
                target=monitor.evaluate_target(100, 100,
                    {'kind': 'manual', 'price': _target_price, 'month': 10, 'failed': False}, _group, 3))
    _row['purple'] = bool(_row['target']['hit'] or _row['target']['space_hit'])
    _manual_note = '手填目标价说明，与共识价交叉核对 <b>保留</b>'
    _row['signals'] = [_technical_texts[0], _manual_note] + [
        text for text in (_row['target']['reached_detail'], _row['target']['space_detail']) if text]
    _manual_rows.append(_row)
_description_rows += _manual_rows
_description_before = _copy.deepcopy(_description_rows)
with _patch.object(monitor, 'global_dca', return_value=None):
    _description_snapshot = monitor.build_snapshot({}, _description_rows, {}, group_counts={})
    _description_snapshot_before = _copy.deepcopy(_description_snapshot)
    _description_page = monitor.render({}, _description_rows, len(_description_rows), snapshot=_description_snapshot)

_long_hint = monitor._stock_row({**item('LONGHINT', 'position', 'green'), 'hold': 'long'}, {})
check('data-symbol="LONGHINT"' in _long_hint and '不看一般过热' not in _long_hint and '测试信号' in _long_hint,
      '长持仓行保留真实信号，不恢复已删除的周期提示或屏蔽普通过热声明')
check('常规交易日等效分钟（已开盘分钟÷当日全日分钟×390）' in _description_page
      and '半日市按当日实际交易时长折算' in _description_page,
      '量比说明区分实际分钟与半日市等效分钟')

# 刷新首屏必须已有真仓位骨架，不依赖页尾脚本和云端请求。
from page_assets import COST_PANEL_HTML as _cost_shell
check(_description_page.count(_cost_shell) == 1
      and _description_page.index(_cost_shell) < _description_page.index('id="signalCounts"')
      and _description_page.index(_cost_shell) < re.search(r'var\s+REAL\s*=', _description_page).start(),
      '刷新首屏仓位骨架在统计和页尾脚本之前，且只输出一次')
# 2026-11-02：「编辑」(#costEditBtn) 从骨架搬到顶栏 .runbar（monitor 输出，「刷新」右边），
# 骨架本身不再含这颗按钮；页面里它仍必须是「绑定前禁用」。
check('id="costEditBtn"' not in _cost_shell and '<span class="cost-sum"></span>' in _cost_shell
      and ' open' not in _cost_shell.split('>', 1)[0]
      and ' disabled>编辑</button>' in _description_page
      and _description_page.index('btnHardRefresh') < _description_page.index('costEditBtn'),
      '首屏骨架默认折叠且无伪造金额；「编辑」在顶栏刷新右边且绑定前禁用')
with _patch.object(monitor, '_cost_panel_assets', return_value=('', '')):
    _no_cost_page = monitor.render({}, _description_rows, len(_description_rows), snapshot=_description_snapshot)
check('id="costManage"' not in _no_cost_page, '资源缺失不留下无法使用的仓位空壳')

for _row in _description_rows:
    _body = re.search(r'<tr\b[^>]*\bid="' + re.escape(monitor._stock_anchor_id(_row['symbol']))
                      + r'"[^>]*>(.*?)</tr>', _description_page, re.S)[1]
    _sig = re.search(r'<td class="sig">(.*?)</td>', _body, re.S)[1]
    if _row['target']['kind'] == 'consensus':
        check(_sig == _expected_sig, _row['symbol'] + '全页td.sig无自动共识且原技术描述完全一致')
    else:
        _expected_manual = monitor._signals_html({**_row, 'signals': [_technical_texts[0]], 'target': {}})
        check(_sig == _expected_manual and '目标价' not in _sig and 'tnote' not in _sig,
              _row['symbol'] + '手填高/低/达到只显示胶囊，不重复进入描述')
    check(monitor.target_tag(_row['target']) in _body,
          _row['symbol'] + '原胶囊内容与属性原样保留')

check(_description_rows == _description_before
      and _description_snapshot == _description_snapshot_before,
      '描述过滤不突变内部输入或快照')
check(_description_snapshot['summary']['purple'] == 8
      and _description_snapshot['signal_counts']['target']['purple'] == 8
      and len(_description_snapshot['signal_members']['target']['purple']) == 8,
      '共识6加手填2的union统计及成员不丢失')
check({r['symbol'] for r in _description_snapshot['target_overlays']['space']}
      == {'TSPACE', 'TMID', 'THIGH', 'TOVER', 'TBOTH', 'MANHIGH'}
      and {r['symbol'] for r in _description_snapshot['target_overlays']['reached']}
      == {'THIT', 'TBOTH', 'MANHIT'},
      '隐藏描述后空间6、达到3名单与重叠精确保持')
_description_panel = _description_page.split('class="text-only target-purple"', 1)[1].split('</details>', 1)[0]
check(bool(re.search(r'空间\s*6\s*只[\s　]*达到\s*3\s*只', _description_panel))
      and '250，+150.0%' in re.sub(r'<[^>]+>', '', _description_panel)
      and '<span class="percent-change pos">+150.0%</span>' in _description_panel
      and '共识价 250，空间' not in re.sub(r'<[^>]+>', '', _description_panel)
      and '跌破 190' in _description_panel
      and _description_panel.count('href="#s-tboth"') == 2,
      '统计区共识详情与两段重叠跳转不丢失')
_both = next(row for row in _description_rows if row['symbol'] == 'TBOTH')
_both_html = monitor._stock_row(_both, {})
check(_both['target']['band'] == 'high' and _both['target']['hit']
      and 'lvtag tgt hit' in _both_html and 'pur-hit' in _both_html
      and 'tgt high' not in _both_html and 'pur-high' not in _both_html,
      '达到优先于高空间：实心紫胶囊和达到条纹不被紫红覆盖')
for _kind in ('manual', None):
    _row = item('KINDKEEP', 'technology', 'green')
    _row.update(signals=['今日上穿共识价 250'], target={'kind': _kind})
    check('今日上穿共识价 250' in _row_signals(_row),
          'kind=' + repr(_kind) + '不靠文字误判为自动共识')
_technical_mention = item('TECHKEEP', 'technology', 'green')
_technical_mention.update(target={'kind': 'consensus'}, signals=['RSI 原技术描述，与共识价交叉核对'])
check(_technical_mention['signals'][0] in _row_signals(_technical_mention).replace('<span class="cm">，</span>', '，'),
      '自动共识kind也不误删包含关键字的原技术描述')
_runbar = re.search(r'<div class="runbar">(.*?)</div>', _description_page, re.S)[1]
check('href="#signalRules"' not in _runbar and 'shortcut.textContent = "预警规则"' not in monitor.PAGE_INTERACTION_JS,
      '静态及动态顶部均不恢复已删除的预警规则快捷按钮')
check(not re.search(r'\.lvtag\.tgt\.high\{[^}]*color:', _description_page),
      '高空间(>=90%)数字沿用普通紫，不再单独紫红；只有外框紫红#dc62b8')

# 最新判灯：技术灰仍保留主表绿灯；组合不重复RSI/布林，缺/参考不进统计。
from tests.mm_test_groups import TestBollRsiRules as _BollRules
_business = _BollRules()
_business.setUp()
try:
    _business_rows = [
        _business.analyze([35, 35, 35], 'technology', bands=(100, 110, 99), symbol='COMBORED'),
        _business.analyze([70, 70, 70], 'position', symbol='RSIYELLOW'),
        _business.analyze([70, 70, 70], 'technology', symbol='RSIGRAY'),
        _business.analyze([70, 50, 50], 'technology', bands=(100, 110, 99), symbol='COMBOGRAY'),
        _business.analyze([70, 50, 50], 'technology', symbol='RSISILENT'),
        _business.analyze([50, 50, 50], 'focus', bands=(100, 101, 90), symbol='UPSILENT'),
        _business.analyze([50, 50, 50], 'technology', high=120, low=90, symbol='DUALSILENT')]
    _oversold_rows = []
    for _index, _group in enumerate(g['item_group'] for g in monitor.GROUPS):
        for _side, _bands, _prefix in ((None, (100, 110, 90), 'OSNONE'),
                                       ('上轨', (100, 101, 90), 'OSUP'),
                                       ('下轨', (100, 110, 99), 'OSDOWN')):
            _row = _business.analyze([35, 30, 20], _group, bands=_bands, symbol=_prefix + str(_index))
            _oversold_rows.append((_row, _side))
finally:
    _business.doCleanups()
_excluded = item('QUOTEMISSING', 'technology', 'gray', chg=0)
_excluded.update(price=None, signals=['缺行情'], signal_marks=[])
_reference = item('QUOTE_REFERENCE', 'technology', 'green', chg=0)
_reference.update(reference_only=True, signals=[], signal_marks=[])
_business_rows += [_excluded, _reference]
with _patch.object(monitor, 'global_dca', return_value=None):
    _business_snap = monitor.build_snapshot({}, _business_rows, {}, group_counts={})
    _business_page = monitor.render({}, _business_rows, len(_business_rows), snapshot=_business_snap)
check(_business_snap['signal_counts']['boll_rsi']['red'] == 1
      and _business_snap['signal_counts']['rsi']['red'] == 0
      and _business_snap['signal_counts']['boll']['red'] == 0,
      '下轨三超卖只归布林+RSI红，不重复单独RSI或布林')
check(_business_snap['signal_counts']['rsi']['yellow'] == 1
      and _business_snap['summary']['red'] == 1 and _business_snap['summary']['yellow'] == 1,
      '单独三超买持仓黄，其他组文字灰，顶部按最终灯去重')
check({r['symbol'] for r in _business_snap['text_only']} == {'RSIGRAY', 'COMBOGRAY'},
      '单条RSI、重点零条上轨、受限制双轨、缺/参考均不制造技术灰')
for _sym in ('RSIGRAY', 'COMBOGRAY', 'RSISILENT', 'UPSILENT', 'DUALSILENT'):
    _row = next(row for row in _business_rows if row['symbol'] == _sym)
    _classes, _fragment = rendered_stock_row(_business_page, _row, _sym == 'COMBOGRAY')
    check(_classes == ['green'], _sym + '技术文字或无提醒均不把主表绿灯改灰')
check('<h5>RSI 3条 <span class="arrow-rsi-up">↑</span>超买</h5>' in _business_page,
      '单独三超买：统计框标题写「RSI 3条 ↑超买」，不跟其他红灯染红')
_business_plain = re.sub(r'<[^>]+>', '', _business_page)
check('盘中突破布林上轨▲' in _business_plain and '触及布林下轨▼' in _business_plain,
      '双轨保留双方事实但限制后不换边统计')
check('id="' + monitor._stock_anchor_id('QUOTEMISSING') + '"' in _business_page
      and 'id="' + monitor._stock_anchor_id('QUOTE_REFERENCE') + '"' in _business_page
      and all(r['symbol'] not in ('QUOTEMISSING', 'QUOTE_REFERENCE')
              for levels in _business_snap['signal_members'].values() for members in levels.values() for r in members),
      '缺/参考仍在主表，所有指标统计成员均排除')

# 三超卖：无轨与下轨全组红；上轨持仓/重点黄，其他技术灰且可折叠。
with _patch.object(monitor, 'global_dca', return_value=None):
    _oversold_items = [row for row, _ in _oversold_rows]
    _oversold_snap = monitor.build_snapshot({}, _oversold_items, {}, group_counts={})
    _oversold_page = monitor.render({}, _oversold_items, len(_oversold_items), snapshot=_oversold_snap)
for _row, _side in _oversold_rows:
    _kind = 'rsi' if _side is None else 'boll_rsi'
    _facts = monitor.classify_technical_facts(_row['group'], 0, 3, _side)
    _gray = _side == '上轨' and _row['group'] not in monitor.BOLL_UP_SIGNAL_GROUPS
    _color = 'gray' if _gray else 'yellow' if _side == '上轨' else 'red'
    check(_facts == {'kind': _kind, 'level': _color, 'foldable': True}
          and _row['signal_marks'] == ([] if _gray else [(_kind, _color)]),
          _row['symbol'] + '纯事实与生产分析一致，上轨持仓/重点黄、其他技术灰')
    _classes, _fragment = rendered_stock_row(_oversold_page, _row, False)
    check(_classes == ['green' if _gray else _color], _row['symbol'] + '主表灯不混淆技术灰与红黄灯')
    check(_row['symbol'] in {m['symbol'] for m in (_oversold_snap['text_only'] if _gray
                                                 else _oversold_snap['signal_members'][_kind][_color])},
          _row['symbol'] + '快照统计成员归属正确')
    if _color == 'yellow':
        _sig = re.search(r'<td class="sig">(.*?)</td>', _fragment, re.S)[1]
        _sig_plain = _unescape(re.sub(r'<[^>]+>', '', _sig))
        check(_signal_pairs(_sig) == {('boll_rsi', 'yellow')}
              and all(term in _sig_plain for term in ('布林上轨▲', 'RSI 3条 ↓超卖'))
              and '布林 + RSI → 黄' in _row['signals']
              and '布林 + RSI → 红' not in _row['signals'],
              _row['symbol'] + '上轨三反向超卖描述仅组合黄字，不重复单独布林或RSI')
_oversold_gray_symbols = {row['symbol'] for row, side in _oversold_rows
                         if side == '上轨' and row['group'] not in monitor.BOLL_UP_SIGNAL_GROUPS}
_oversold_yellow_symbols = {row['symbol'] for row, side in _oversold_rows
                           if side == '上轨' and row['group'] in monitor.BOLL_UP_SIGNAL_GROUPS}
check(_oversold_snap['summary']['red'] == 2 * len(monitor.GROUPS)
      and _oversold_snap['summary']['yellow'] == len(_oversold_yellow_symbols)
      and {m['symbol'] for m in _oversold_snap['text_only']} == _oversold_gray_symbols,
      '三超卖无轨/下轨红、上轨持仓/重点黄、其他上轨仅技术灰')
check(_oversold_snap['signal_counts']['boll_rsi']['red'] == len(monitor.GROUPS)
      and _oversold_snap['signal_counts']['boll_rsi']['yellow'] == len(_oversold_yellow_symbols)
      and {m['symbol'] for m in _oversold_snap['signal_members']['boll_rsi']['yellow']} == _oversold_yellow_symbols
      and _oversold_snap['signal_counts']['rsi']['red'] == len(monitor.GROUPS)
      and _oversold_snap['signal_counts']['boll']['red'] == 0,
      '上轨持仓/重点组合统计由红转黄，无轨与下轨不变且不重复计布林')

# STX/NEE 型：技术灰与涨跌红黄全页共存，交集保留最终行灯而非伪装成绿色。
_business.setUp()
try:
    with _patch.dict(monitor.S, {'chg_yellow': 2, 'chg_red': 4}):
        _coexist_rows = [
            _business.analyze([70, 70, 50], 'technology', symbol='STXY', chg=3),
            _business.analyze([35, 35, 50], 'technology', symbol='STXR', chg=-5),
            _business.analyze([70, 50, 50], 'technology', bands=(100, 101, 90), symbol='NEEY', chg=3)]
finally:
    _business.doCleanups()
_coexist_before = _copy.deepcopy(_coexist_rows)
with _patch.object(monitor, 'global_dca', return_value=None):
    _coexist_snap = monitor.build_snapshot({}, _coexist_rows, {}, group_counts={})
    _coexist_snap_before = _copy.deepcopy(_coexist_snap)
    _coexist_page = monitor.render({}, _coexist_rows, len(_coexist_rows), snapshot=_coexist_snap)
_coexist_data = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _coexist_page)[1]))
_group_headers = re.findall(r'<details\b[^>]*\bid="group_[^"]*"[^>]*>\s*<summary\b[^>]*>(.*?)</summary>', _coexist_page, re.S)
check(_group_headers and all('stat-badge gray' not in header for header in _group_headers)
      and 'stat-badge gray' in _coexist_page,
      '技术灰数字只在统计面板，不出现在个股和长持仓表头')
check('signal-dot' not in monitor.PAGE_CSS and 'signal-dot' not in monitor.PAGE_INTERACTION_JS
      and '--lamp-size:14px' in monitor.PAGE_CSS
      and 'display:inline list-item' not in monitor.PAGE_CSS,
      '多选删除全部圆点CSS与创建节点逻辑，其他灯共用字号和Safari兼容性不变')
_filter_palette = {'red': 'var(--lamp-red)', 'yellow': 'var(--lamp-yellow)', 'gray': 'var(--text)',
                   'purple': 'var(--purple)', 'green': 'var(--green)'}
for _level, _color in _filter_palette.items():
    _selector = f'#signalCounts .signal-filter-condition[data-signal-level="{_level}"]'
    check(monitor.PAGE_CSS.count(_selector + '{color:' + _color + '}') == 1,
          '多选' + _level + '只按自身级别着色一次，不改单选或名称竖条')
_filter_css = re.sub(r'/\*.*?\*/', '', monitor.PAGE_CSS, flags=re.S)
_filter_rules = [(selector.strip(), body) for selectors, body in re.findall(r'([^{}]+)\{([^{}]*)\}', _filter_css)
                 for selector in selectors.split(',') if '.signal-filter-condition' in selector]
check(len(_filter_rules) >= len(_filter_palette)
      and all(selector == '#signalCounts .signal-filter-condition'
              or selector in {f'#signalCounts .signal-filter-condition[data-signal-level="{level}"]'
                              for level in _filter_palette}
              or selector in {'#signalCounts .signal-filter-condition'
                              f'[data-signal-kind="earnings_week"][data-signal-level="{level}"]'
                              for level in ('red', 'yellow', 'gray')}
              or selector in {'#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd',
                              '#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd.tbd'}
              for selector, _ in _filter_rules)
      and all(not re.search(r'(?:^|;)\s*color\s*:', body)
              or bool(re.search(r'(?:^|;)\s*color\s*:\s*inherit\s*(?:;|$)', body))
              for selector, body in _filter_rules if selector == '#signalCounts .signal-filter-condition'),
      '多选条件CSS仅限统计区域，未定级含财报空级继承描述色且不补伪元素圆点')
check((_coexist_snap['summary']['red'], _coexist_snap['summary']['yellow']) == (1, 2)
      and {m['symbol']: m['level'] for m in _coexist_snap['text_only']}
      == {'STXR': 'red'},
      '只保留合法两超卖技术灰，其他组两超买及上轨单超买不计灰，独立红黄汇总不变')
for _row, _color, _kind in zip(_coexist_rows, ('yellow', 'red', 'yellow'), ('rsi', 'rsi', 'boll_rsi')):
    _classes, _body = rendered_stock_row(_coexist_page, _row, not _row.get('foldable', True))
    _sig = re.search(r'<td class="sig">(.*?)</td>', _body, re.S)[1]
    _sym = re.search(r'<td class="sym">(?:<span class="sym-box \w+">)?(.*?)</td>', _body, re.S)[1]
    check(_classes == [_color] and 'signal-dot' not in _body
          and _sym.startswith(_row['symbol'])
          and re.findall(r'data-signal-kind="([^"]*)" data-signal-level="([^"]*)"', _sig)
          == [('chg', _color)] + ([(_kind, 'gray')] if _row['symbol'] == 'STXR' else [])
          and _signal_pairs(_sig)
          == {('chg', _color), (_kind, 'gray' if _row['symbol'] == 'STXR' else '')},
          _row['symbol'] + '主表独立红黄保留，合法技术灰白字，无灯技术仅文字')
    check([m['l'] for m in _coexist_data.get(_kind, {}).get('members', []) if m['s'] == _row['symbol']]
          == ([_color] if _row['symbol'] == 'STXR' else [])
          and all(_coexist_snap['signal_counts'][kind][level] == 0
                  for kind in ('rsi', 'boll', 'boll_rsi') for level in ('red', 'yellow')),
          _row['symbol'] + '灰名单仅含规则允许项，独立行灯不制造技术灰或重复技术统计')
check(_coexist_rows == _coexist_before and _coexist_snap == _coexist_snap_before,
      '技术灰共存全页渲染不突变输入与快照')

# 旧中点解析只恢复已知的同轨布林连续天数尾句；绝不追加独立布林灯。
_tail = '连续第2日贴近下轨'
_plain = '逼近布林下轨▼ 还差0.15%（盘中触及 293.63）'
for _level, _rsi in ((None, None), ('yellow', 'RSI 2条 ↓超卖（≤35）'),
                     ('red', 'RSI 3条 ↓超卖（≤35）')):
    _tail_row = item('TAIL', 'technology', _level or 'green')
    _tail_row.update(signals=([_rsi] if _rsi else []) + [_plain, _tail, '量比 1.2x'],
                     signal_marks=[('boll_rsi', _level)] if _level else [])
    _before_tail = _copy.deepcopy(_tail_row)
    _html_tail = _row_signals(_tail_row)
    _groups_tail = monitor._signal_description_groups(_tail_row)
    _technical = next(g for g in _groups_tail if g['kind'] in ('boll', 'boll_rsi'))
    check(_technical['kind'] == ('boll_rsi' if _rsi else 'boll')
          and _technical['level'] == (_level or 'gray')
          and _plain + '，' + _tail in _technical['texts']
          and 'signal-dot' not in _html_tail and _html_tail.count('signal-separator') == 0
          and _signal_pairs(_html_tail)
          == {('boll_rsi' if _rsi else 'boll', _level or 'gray'), ('vol_ratio', '')}
          and _groups_tail[-1]['kind'] == 'vol_ratio' and _groups_tail[-1]['level'] is None,
          str(_level) + '连续天数留同一指标一次级别，斜线后独立量比无灯灰字')
    check(_tail_row == _before_tail, '尾句恢复不改原始signals/marks')
check(monitor._description_signals([_tail]) == [_tail]
      and monitor._description_signals(['突破布林上轨▲', _tail]) == ['突破布林上轨▲', _tail]
      and monitor._description_signals([_plain, '量比 1.2x', _tail]) == [_plain, '量比 1.2x', _tail],
      '缺前句、不同轨、已切指标时不凭连续天数猜归属或新增布林灯')
check(monitor._description_signals(['跌破布林下轨▼', '连续第≥120日贴近下轨（其中3日穿越）'])
      == ['跌破布林下轨▼，连续第≥120日贴近下轨（其中3日穿越）'], '连续天数含上限/穿越尾句安全恢复')
_ma_inline = item('MATEXT', 'technology', 'yellow')
_ma_inline.update(signals=['上穿50日均线 / 日内触及200日均线，现价在均线下方'],
                  signal_marks=[('ma', 'yellow')], ma_settings={'ma_short': 50, 'ma_long': 200})
check('signal-separator' not in _row_signals(_ma_inline)
      and ' / ' not in _row_signals(_ma_inline), '同一均线指标内部合并事实用逗号不冒充切指标')

# 个股描述不自动补指标标签：单独布林、组合、均线、异动与RSI统一直接展示事实。
for _kind, _signals, _marks, _expected in (
        ('boll', ['逼近布林上轨▲'], [], '逼近布林上轨▲'),
        ('boll', ['跌破布林下轨▼（盘中触及 66.33）', '连续第4日贴近下轨（其中2日穿越）'], [],
         '盘中跌破布林下轨▼后收回，连续第4日贴近下轨（其中2日穿越）'),
        ('boll_rsi', ['跌破布林下轨▼（盘中触及 66.33）', '连续第4日贴近下轨（其中2日穿越）',
                      'RSI 1条 ↓超卖（≤35）'], [],
         '盘中跌破布林下轨▼后收回，连续第4日贴近下轨（其中2日穿越） · RSI 1条 ↓超卖'),
        ('ma', ['日内触及120日均线 ±1%区间'], [('ma_band', 'yellow')], '触及长均线 ±1%区间'),
        ('ma', ['上穿60日弱均线'], [('ma_cross', 'yellow')], '上穿短均线'),
        ('chg', ['异动 -21.9%'], [('chg', 'red')], '异动 -21.9%'),
        ('rsi', ['RSI 1条 ↓超卖（≤35）'], [], 'RSI 1条 ↓超卖')):
    _copy_row = item('DIRECTFACT', 'technology', 'green')
    _copy_row.update(signals=_signals, signal_marks=_marks, ma_settings={'ma_short': 60, 'ma_long': 120})
    _copy_before = _copy.deepcopy(_copy_row)
    _copy_html = _row_signals(_copy_row)
    _copy_plain = _signal_text(_copy_html)
    check(_copy_plain == _expected.replace('连续第', '第').replace('（其中', '（') and f'data-signal-kind="{_kind}"' in _copy_html,
          _kind + '个股描述直接展示完整事实，不自动添加指标名称')
    check(_copy_row == _copy_before, _kind + '去标签不修改原始事实、判灯及配置')

# 2026-11-04 晚：布林+RSI 拆成左（布林）／右（RSI）两个独立换行的区域，
# 但外层条目数、data-signal-kind/level、以及去标签后的事实文本必须逐字不变。
_zone_row = item('ZONESPLIT', 'technology', 'yellow')
_zone_row.update(signals=['跌破布林下轨▼（盘中触及 66.33）', '连续第4日贴近下轨（其中2日穿越）',
                          'RSI 1条 ↓超卖（≤35）'], signal_marks=[])
_zone_html = _row_signals(_zone_row)
check(_zone_html.count('class="signal-item"') == 2
      and 'class="sig-zone" data-side="boll"' in _zone_html
      and 'class="sig-zone" data-side="rsi"' in _zone_html
      and 'class="sig-sep"' not in _zone_html
      and 'data-signal-kind="boll_rsi"' in _zone_html
      and _signal_text(_zone_html)
      == '盘中跌破布林下轨▼后收回，第4日贴近下轨（2日穿越） · RSI 1条 ↓超卖',
      '组合条件分左右两区，事实文本与外层条目契约不变')

# 描述红黄灰无紫稳定排序并安全转义，均线只投影展示，原数字事实与marks保留。
_order_row = item('ORDERESCAPE', 'technology', 'red')
_order_row.update(signals=['手填目标价说明 <b>尾部</b>', '无灯说明 <img onerror=x>',
    'RSI 2条 ↓超卖（≤35）', '日内触及120日均线 ±1%区间', '上穿60日弱均线',
    '量比 2.8x', '异动 -12.1%', '距加仓价 90 还差 1%', '另条说明 & 保留'],
    signal_marks=[('target', 'purple'), ('ma_band', 'yellow'), ('ma_cross', 'yellow'),
                  ('vol_ratio', 'yellow'), ('chg', 'red'), ('add_price', 'red')],
    ma_band_direction='up', ma_settings={'ma_short': 60, 'ma_long': 120}, trigger=90,
    target=monitor.evaluate_target(100, 100, {'kind': 'manual', 'price': 150}, 'technology', 3))
_order_no_gray = _copy.deepcopy(_order_row)
_order_no_gray.update(symbol='ORDER2HIGH', signals=[
    'RSI 2条 ↑超买（≥70）' if text == 'RSI 2条 ↓超卖（≤35）' else text
    for text in _order_row['signals']])
_order_before = _copy.deepcopy(_order_row)
_order_no_gray_before = _copy.deepcopy(_order_no_gray)
with _patch.dict(monitor.S, {'ma_short': 120, 'ma_long': 60}), \
        _patch.object(monitor, 'global_dca', return_value=None):
    _order_snap = monitor.build_snapshot({}, [_order_row, _order_no_gray], {}, group_counts={})
    _order_snap_before = _copy.deepcopy(_order_snap)
    _order_page = monitor.render({}, [_order_row, _order_no_gray], 2, snapshot=_order_snap)
_order_body = re.search(r'<tr\b[^>]*\bid="s-orderescape"[^>]*>(.*?)</tr>', _order_page, re.S)[1]
_order_sig = re.search(r'<td class="sig">(.*?)</td>', _order_body, re.S)[1]
check(_signal_pairs(_order_sig) == {('chg', 'red'), ('ma', 'yellow'),
          ('vol_ratio', 'yellow'), ('rsi', 'gray'), ('', '')}
      and 'signal-dot' not in _order_body
      and 'class="lvtag add signal-item" data-lv="90" data-near="0"' in _order_sig
      and _order_sig.count('data-signal-kind="ma"') == 1
      and 'data-signal-role=' not in _order_sig,
      '全页零圆点，其余描述稳定排序且均线合并一次级别（振幅已删除，不再有主次角色）')
check(all(text in _order_sig for text in ('&lt;img onerror=x&gt;', '&amp; 保留'))
      and '目标价' not in _order_sig
      and '<img' not in _order_sig and '<b>' not in _order_sig,
      '排序聚合后描述仍安全HTML转义')
_order_member = _order_snap['signal_members']['ma']['yellow'][0]
_order_stats = _order_page.split('id="signalCounts"', 1)[1].split('id="macroCard"', 1)[0]
_order_filter = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _order_page)[1]))['ma']['members'][0]['d'][0]['t']
for _label, _output in (('主表描述', _order_sig), ('统计区', _order_stats),
                       ('交集详情', _order_filter), ('均线图标', monitor._member_icons(_order_member, 'ma', 'yellow'))):
    check('短均线' in _output and '长均线' in _output
          and not re.search(r'\d+日(?:弱)?均线', _output), _label + '采用事实配置短长名称而非当前反置配置或数字')
check('现价在长均线上方' in _order_stats and '现价在长均线上方' in _order_filter,
      '均线触及与穿越共存时保留同事实长线现价侧文字')
_order_filter_data = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _order_page)[1]))
check(_order_filter_data['chg']['members'][0]['d'][0]['l'] == 'red'
      and _order_filter_data['ma']['members'][0]['d'][0]['l'] == 'yellow'
      and _order_filter_data['rsi']['members'][0]['d'][0]['l'] == 'gray'
      and all(_order_filter_data[k]['members'][0]['l'] == 'red' for k in ('chg', 'ma', 'rsi')),
      '全页筛选同股红黄灰事实颜色独立取统计条件，不沿用整股红灯')
check(all(set(fact) == {'l', 't'} and not re.search(r'灰灯|白灯|红灯|黄灯|布林\+RSI', fact['t'])
          for condition in _order_filter_data.values() for member in condition['members'] for fact in member['d']),
      '全页紧凑事实均为l/t对象且不含可见灯级文字或组合重复标签')
check(_order_row == _order_before and _order_snap == _order_snap_before
      and _order_member['ma_settings'] == {'ma_short': 60, 'ma_long': 120}
      and any(f['text'] == '上穿60日弱均线' for f in _order_member['ma_facts'])
      and _order_member['indicators'] == monitor._member_direction_markers(_order_row, 'ma'),
      '展示投影不改内部数字signals、marks、方向markers及快照')
_order_control_classes, _order_control_body = rendered_stock_row(_order_page, _order_no_gray, True)
_order_control_sig = re.search(r'<td class="sig">(.*?)</td>', _order_control_body, re.S)[1]
check(_order_control_classes[0] == 'red'
      and _signal_pairs(_order_control_sig)
      == {('chg', 'red'), ('ma', 'yellow'),
          ('vol_ratio', 'yellow'), ('', ''), ('rsi', '')}
      and 'RSI 2条' in _unescape(re.sub(r'<[^>]+>', '', _order_control_sig))
      and '↑超买' in _unescape(re.sub(r'<[^>]+>', '', _order_control_sig)),
      '原排序2high控制仅文字，独立红黄紫灯及无灯首次出现排序不变')
check({member['symbol'] for member in _order_snap['text_only']} == {'ORDERESCAPE'}
      and all(member['s'] != 'ORDER2HIGH' for member in _order_filter_data.get('rsi', {}).get('members', []))
      and _order_no_gray == _order_no_gray_before,
      '合法2low仍进灰名单，原2high控制不进灰名单且渲染不突变输入')

# 上轨单条两个方向：持仓技术灰，重点/其他仅文字；独立涨跌灯与既有折叠不变。
_business.setUp()
try:
    _focus_text_rows = []
    for _group in ('focus', 'position', 'technology'):
        for _values, _direction in (([70, 50, 50], '↑超买'), ([35, 50, 50], '↓超卖')):
            for _chg in (0, 6):
                _symbol = 'FT' + _group.upper() + ('UP' if _direction == '↑超买' else 'DOWN') + str(_chg)
                _focus_text_rows.append((_business.analyze(_values, _group, bands=(100, 101, 90),
                                                          symbol=_symbol, chg=_chg), _direction))
finally:
    _business.doCleanups()
_focus_items = [row for row, _ in _focus_text_rows]
_focus_before = _copy.deepcopy(_focus_items)
with _patch.object(monitor, 'global_dca', return_value=None):
    _focus_snap = monitor.build_snapshot({}, _focus_items, {}, group_counts={})
    _focus_snap_before = _copy.deepcopy(_focus_snap)
    _focus_page = monitor.render({}, _focus_items, len(_focus_items), snapshot=_focus_snap)
_focus_filter = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _focus_page)[1]))
_focus_card = _focus_page.split('id="group_focus"', 1)[1].split('id="group_technology"', 1)[0]
_focus_shown, _focus_quiet = _focus_card.split('<details class="quiet-list">', 1)
for _row, _direction in _focus_text_rows:
    _keep = _row['group'] == 'position' and _direction == '↓超卖'
    _classes, _body = rendered_stock_row(_focus_page, _row, _keep)
    _sig = re.search(r'<td class="sig">(.*?)</td>', _body, re.S)[1]
    _focus_only = _row['group'] == 'focus'
    _expected_levels = ([('chg', 'red')] if _row['chg'] else []) + [('boll_rsi', 'gray' if _row['group'] == 'position' else '')]
    check(_classes == ['red' if _row['chg'] else 'green'] and _row['foldable'] == (not _keep)
      and _signal_pairs(_sig) == set(_expected_levels)
          and '布林上轨▲' in _unescape(re.sub(r'<[^>]+>', '', _sig))
          and 'RSI 1条 ' + _direction in _unescape(re.sub(r'<[^>]+>', '', _sig)),
          _row['symbol'] + '原方向事实保留，持仓技术灰白字、重点/其他仅文字灰字且不继承涨跌红')
    if _focus_only:
        _anchor = 'id="' + monitor._stock_anchor_id(_row['symbol']) + '"'
        check(_anchor in (_focus_shown if _row['chg'] else _focus_quiet)
              and _anchor not in (_focus_quiet if _row['chg'] else _focus_shown),
              _row['symbol'] + '仅文字绿行可折叠，独立涨跌红仍留主表')
    if _row['group'] != 'position':
        check(all(member['s'] != _row['symbol'] for kind in ('rsi', 'boll', 'boll_rsi')
                  for member in _focus_filter.get(kind, {}).get('members', [])),
              _row['symbol'] + '不进入任何技术灰筛选名单')
check({row['symbol'] for row in _focus_snap['text_only']}
      == {row['symbol'] for row in _focus_items if row['group'] == 'position'}
      and _focus_snap['signal_counts']['chg']['red'] == 6
      and _focus_snap['summary']['red'] == 6,
      '重点/其他上轨两方向单条不计灰，持仓技术灰及六个独立涨跌红完整保留')
check(_focus_items == _focus_before and _focus_snap == _focus_snap_before,
      '仅文字全页渲染不突变事实或快照')
for _page_label, _page in (('基础页', html), ('混合红黄灰无灯页', _order_page), ('focus仅文字页', _focus_page)):
    _tables = re.findall(r'<table class="stk">.*?</table>', _page, re.S)
    check(bool(_tables) and all('signal-dot' not in table for table in _tables),
          _page_label + '全部个股表含无异动折叠表均零圆点')
    _visible = _unescape(re.sub(r'<[^>]+>', '', re.sub(
        r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', _page, flags=re.S)))
    check(not re.search(r'灰灯|不计灰|转灰列|紫(?:红)?虚线空心灯', _visible),
          _page_label + '可见文案无旧灯显示名，脚本兼容前缀不误判为显示')
check('.signal-item{color:var(--dim)}' in _focus_page
      and '.signal-item[data-signal-level="gray"]{color:var(--text)}' in _focus_page,
      '整页保留无灯灰字和技术灰白字两条独立CSS')
_rules_table = re.search(r'<table[^>]*id="rules-lamp-table"[^>]*>.*?</table>', _focus_page, re.S)[0]
for _condition, _position in (('布林上轨▲ + RSI 1条 ↑超买', '白灯'),
                             ('布林上轨▲ + RSI 1条 ↓超卖（反）', '白灯')):
    _rule_rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', _rules_table, re.S)
                  if '<td' in row and _unescape(re.sub(r'<[^>]+>', '',
                      re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1])) == _condition]
    check(len(_rule_rows) == 1, _condition + '全页规则行唯一')
    _cells = re.findall(r'<td\b([^>]*)>(.*?)</td>', _rule_rows[0] if _rule_rows else '', re.S)
    check([_unescape(re.sub(r'<[^>]+>', '', text)) for _, text in _cells]
          == [_condition, _position, '仅文字', '无提示']
          and _cells[2][0] == '' and '<td>仅文字</td>' in _rule_rows[0],
          _condition + '全页规则重点列仅文字且无白灯样式')

# 四项授权全页专项：独立涨跌灯不改变技术描述、统计归属或折叠资格。
_business.setUp()
try:
    _four_cases = []
    for _group in (g['item_group'] for g in monitor.GROUPS):
        _active = _group in ('position', 'focus')
        _cases = [([50, 50, 50], (100, 110, 99), 'boll', 'gray', _group != 'position'),
                  ([70, 70, 70], (100, 110, 90), 'rsi',
                   'yellow' if _group == 'position' else 'gray', _group != 'focus'),
                  ([35, 35, 50], (100, 101, 90), 'boll_rsi', 'gray' if _group == 'focus' else 'yellow' if _active else '', _group != 'focus'),
                  ([70, 35, 35], (100, 101, 90), 'boll_rsi', 'gray' if _group == 'focus' else 'yellow' if _active else '', _group != 'focus'),
                  ([70, 70, 50], (100, 101, 90), 'boll_rsi', 'gray' if _group == 'focus' else 'yellow' if _active else '', _group != 'focus'),
                  ([70, 70, 35], (100, 101, 90), 'boll_rsi', 'gray' if _group == 'focus' else 'yellow' if _active else '', _group != 'focus'),
                  ([35, 35, 35], (100, 101, 90), 'boll_rsi', 'yellow' if _active else 'gray', True)]
        _cases += [([35, 50, 50], (100, 110, 90), 'rsi', '', True),
                   ([70, 35, 50], (100, 110, 90), 'rsi', '', True),
                   ([70, 70, 50], (100, 110, 90), 'rsi', 'gray' if _group == 'position' else '', True),
                   ([70, 70, 35], (100, 110, 90), 'rsi', 'gray' if _group == 'position' else '', True),
                   ([70, 35, 35], (100, 110, 90), 'rsi', 'gray', True),
                   ([35, 50, 50], (100, 101, 90), 'boll_rsi', 'gray' if _group == 'position' else '', _group != 'position'),
                   ([70, 35, 50], (100, 101, 90), 'boll_rsi', 'gray' if _active else '', _group != 'position'),
                   ([70, 50, 50], (100, 101, 90), 'boll_rsi', 'gray' if _group == 'position' else '', True)]
        if not _active:
            _cases += [([50, 50, 50], (100, 101, 90), 'boll', '', True),
                       ([70, 70, 70], (100, 101, 90), 'boll_rsi', 'gray', True)]
        for _case_index, (_values, _bands, _kind, _technical_level, _foldable) in enumerate(_cases):
            for _chg in (0, 3, 6):
                _symbol = 'AUTH' + _group.upper() + str(_case_index) + str(_chg)
                _row = _business.analyze(_values, _group, bands=_bands, chg=_chg, symbol=_symbol)
                _overall = ('red' if _technical_level == 'red' or _chg == 6 else 'yellow'
                            if _technical_level == 'yellow' or _chg else 'green')
                _four_cases.append((_row, _kind, _technical_level, _foldable, _overall))
    # 双轨保持双方事实：较负距离优先、相等上轨；不得用未选下轨补灯或补灰。
    for _group in (g['item_group'] for g in monitor.GROUPS if g['item_group'] not in monitor.BOLL_UP_SIGNAL_GROUPS):
        for _low_n in (2, 3):
            for _case_index, (_high, _low, _side) in enumerate(((120, 89, '上轨'), (111, 80, '下轨'),
                                                               (120, 80, '下轨'), (110, 90, '上轨'))):
                _values = [35] * _low_n + [50] * (3 - _low_n)
                _symbol = 'DUAUTH' + _group.upper() + str(_low_n) + str(_case_index)
                _row = _business.analyze(_values, _group, high=_high, low=_low, symbol=_symbol)
                _technical_level = ('red' if _low_n == 3 else 'yellow') if _side == '下轨' else ('gray' if _low_n == 3 else '')
                _overall = _technical_level if _technical_level in ('red', 'yellow') else 'green'
                check('布林' + _side in _row['boll_alert_text']
                      and all(any('布林' + side in text for text in _row['signals']) for side in ('上轨', '下轨')),
                      _symbol + '带符号距离选轨且保留双轨事实')
                _four_cases.append((_row, 'boll_rsi', _technical_level, True, _overall))
finally:
    _business.doCleanups()
_four_items = [row for row, *_ in _four_cases]
_four_before = _copy.deepcopy(_four_items)
with _patch.object(monitor, 'global_dca', return_value=None):
    _four_snap = monitor.build_snapshot({}, _four_items, {}, group_counts={})
    _four_snap_before = _copy.deepcopy(_four_snap)
    _four_page = monitor.render({}, _four_items, len(_four_items), snapshot=_four_snap)
_four_filter = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _four_page)[1]))
_four_gray = {row['symbol'] for row, _, level, _, _ in _four_cases if level == 'gray'}
_four_quiet_tables = re.findall(r'<details class="quiet-list"(?: open)?>.*?<table class="stk">(.*?)</table>', _four_page, re.S)
_four_quiet_html = ''.join(_four_quiet_tables)
for _row, _kind, _technical_level, _foldable, _overall in _four_cases:
    _classes, _body = rendered_stock_row(_four_page, _row, not _foldable)
    _sig = re.search(r'<td class="sig">(.*?)</td>', _body, re.S)[1]
    _levels = re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                        r'(?: data-signal-level="([^"]*)")?[^>]*>', _sig)
    # 合成输入为0/3/6%，重算后有浮点误差；同色黄灯保留先出现的技术描述，涨跌红优先。
    # 其他组零条上轨是无提示，不应生成空级布林描述；有RSI事实的无灯组合仍留文字。
    _expected = [] if _kind == 'boll' and not _technical_level else [(_kind, _technical_level)]
    _expected += [('chg', 'red' if round(_row['chg']) == 6 else 'yellow')] if _row['chg'] else []
    _expected.sort(key=lambda pair: ({'red': 0, 'yellow': 1, 'gray': 2, '': 3}[pair[1]],
                                     0 if pair == ('chg', 'red') else 1))
    check(_classes == [_overall] and _row['foldable'] == _foldable and set(_levels) == set(_expected) and all(_levels.count(pair) <= (2 if pair[0] == 'boll_rsi' else 1) for pair in _levels),
          _row['symbol'] + '技术灰白字、仅文字灰字、独立红黄和主行竖条各自保留')
    _anchor = 'id="' + monitor._stock_anchor_id(_row['symbol']) + '"'
    check((_anchor in _four_quiet_html) == (_foldable and _overall == 'green'),
          _row['symbol'] + '可折叠绿行进无异动；不折叠白灯和独立红黄留主表')
    _members = {m['s']: m for m in _four_filter.get(_kind, {}).get('members', [])}
    check((_row['symbol'] in _members) == bool(_technical_level),
          _row['symbol'] + '仅文字不统计，技术灰或红黄进对应筛选名单')
    if _technical_level:
        check(_members.get(_row['symbol'], {}).get('l') == _overall,
              _row['symbol'] + '统计交集保留最终行灯而非强制降绿')
    check(all(_row['symbol'] not in {m['s'] for m in _four_filter.get(kind, {}).get('members', [])}
              for kind in ('rsi', 'boll', 'boll_rsi') if kind != _kind),
          _row['symbol'] + '技术归属互斥，不重复单独布林或RSI')
check({m['symbol'] for m in _four_snap['text_only']} == _four_gray,
      '独立矩阵灰名单准确：其他上轨0/1/2及非持仓无轨2high无灰，三高/三低与合法灰保留')
for _kind in ('rsi', 'boll', 'boll_rsi'):
    for _color in ('red', 'yellow'):
        _expected_count = sum(kind == _kind and level == _color for _, kind, level, _, _ in _four_cases)
        check(_four_snap['signal_counts'][_kind][_color] == _expected_count,
              _kind + _color + '授权四项仅迁移对应技术统计，不继承其他灯')
for _color in ('red', 'yellow'):
    check(_four_snap['summary'][_color] == sum(overall == _color for *_, overall in _four_cases),
          _color + '总计按最终灯去重')
check(_four_items == _four_before and _four_snap == _four_snap_before,
      '四项授权全页渲染不突变分析结果或快照')
_four_rules = re.search(r'<table[^>]*id="rules-lamp-table"[^>]*>.*?</table>', _four_page, re.S)[0]
for _condition, _expected in (
        ('布林下轨▼ + RSI 0条', ['白灯', '白灯（折）']),
        ('RSI 3条 ↑超买', ['黄灯', '白灯', '白灯（折）']),
        ('布林上轨▲ + RSI 2条 ↑超买', ['黄灯', '白灯', '仅文字']),
        ('布林上轨▲ + RSI 2条 ↓超卖（反）', ['黄灯', '白灯', '仅文字']),
        ('布林上轨▲ + RSI 3条 ↓超卖（反）', ['黄灯', '黄灯', '白灯（折）']),
        ('布林上轨▲ + RSI 1条 ↓超卖（反）', ['白灯', '仅文字', '无提示']),
        ('RSI 2条 ↑超买', ['白灯', '仅文字', '无提示']),
        ('RSI 1条 ↓超卖', ['仅文字'])):
    _rule_rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', _four_rules, re.S)
                  if '<td' in row and _unescape(re.sub(r'<[^>]+>', '',
                      re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1])) == _condition]
    check(len(_rule_rows) == 1 and [_unescape(re.sub(r'<[^>]+>', '', text)) for text in
          re.findall(r'<td\b[^>]*>(.*?)</td>', _rule_rows[0] if _rule_rows else '', re.S)] == [_condition] + _expected,
          _condition + '全页规则与实际分类一致，持仓重点上轨三反向超卖均黄')

# 四箭头只独立着色；使用既有生产分析样本，不改变灯规则与输入事实。
for _row, _side in _oversold_rows:
    _arrow_before = _copy.deepcopy(_row)
    _arrow_level = 'gray' if _side == '上轨' and _row['group'] not in monitor.BOLL_UP_SIGNAL_GROUPS else (
        'yellow' if _side == '上轨' else 'red')
    _arrow_kind = 'rsi' if _side is None else 'boll_rsi'
    _classes, _arrow_fragment = rendered_stock_row(_oversold_page, _row, False)
    _arrow_sig = re.search(r'<td class="sig">(.*?)</td>', _arrow_fragment, re.S)[1]
    _arrow_expected = [('arrow-rsi-down', '↓')] + ([('arrow-band-up', '▲')] if _side == '上轨' else
                                                    [('arrow-band-down', '▼')] if _side == '下轨' else [])
    check(_signal_pairs(_arrow_sig) == {(_arrow_kind, _arrow_level)}
          and sorted(re.findall(r'<span class="(arrow-[a-z-]+)">([▲▼↑↓])</span>', _arrow_sig)) == sorted(_arrow_expected)
          and _row == _arrow_before,
          _row['symbol'] + '主表红黄灰正文层级不变，轨道和RSI箭头有独立方向wrapper且不改事实')
_rsi_yellow = next(row for row in _business_rows if row['symbol'] == 'RSIYELLOW')
_, _rsi_up_fragment = rendered_stock_row(_business_page, _rsi_yellow, False)
check('<span class="signal-item" data-signal-kind="rsi" data-signal-level="yellow">'
      'RSI 3条 <span class="arrow-rsi-up">↑</span>超买</span>' in _rsi_up_fragment,
      '上涨RSI箭头独立wrapper，正文仍为原黄色条件而非上涨绿')

_arrow_css = re.sub(r'/\*.*?\*/', '', monitor.PAGE_CSS, flags=re.S)
check('--up:#3fb950' in _arrow_css and '--down:#f85149' in _arrow_css,
      '方向色变量继续使用页面上涨绿#3fb950、下跌红#f85149')
for _direction in ('up', 'down'):
    _expected_selectors = {f'{scope} .arrow-{kind}-{_direction}'
                           for scope in ('.stk .sig', '#signalCounts') for kind in ('band', 'rsi')}
    _direction_rules = [(set(selector.strip() for selector in selectors.split(',')), body.strip())
                        for selectors, body in re.findall(r'([^{}]+)\{([^{}]+)\}', _arrow_css)
                        if '.arrow-' in selectors and f'color:var(--{_direction})' in body]
    check(_direction_rules == [(_expected_selectors, f'color:var(--{_direction})')],
          _direction + '四类箭头涨跌色仅限主表描述与统计卡，未添加全局颜色覆盖')
for _level, _body_color in (('red', 'var(--lamp-red)'), ('yellow', 'var(--lamp-yellow)'), ('gray', 'var(--text)')):
    check(f'.signal-item[data-signal-level="{_level}"]{{color:{_body_color}}}' in _arrow_css,
          _level + '主表条件正文继续使用原色，与箭头涨跌色独立')
check('.signal-item{color:var(--dim)}' in _arrow_css
      and '.arrow-rsi-up,.arrow-rsi-down{color:inherit;font-size:1.15em;line-height:1}' in _arrow_css,
      '无灯正文灰字与RSI继承色/字号基础规则均保持')
check(not re.search(r'\.stk td\.sig \.(?:signal-item|sig-zone)[^{]*\{[^}]*letter-spacing', _arrow_css)
      and '.stk td.sig .cm,.stk td.sig .pr{margin-right:-.5em}' in _arrow_css
      and '.stk td.sig .pl{margin-left:-.5em}' in _arrow_css,
      '描述正文字距恢复正常，只有全角逗号/括号收掉自己字框里的空白半格')

_rules_arrow_table = re.search(r'<table\b[^>]*id="rules-lamp-table"[^>]*>.*?</table>',
                              re.search(r'<template id="rulesTpl">(.*?)</template>', html, re.S)[1], re.S)[0]
check(all(f'<span class="{cls}">{symbol}</span>' in _rules_arrow_table
          for cls, symbol in (('ar-band-up', '▲'), ('ar-band-down', '▼'),
                              ('ar-rsi-up', '↑'), ('ar-rsi-down', '↓')))
      and not re.search(r'\b(?:arrow-band|arrow-rsi|grp-arrow)\b', _rules_arrow_table),
      '规则表四箭头仍使用独立ar-* wrapper，不套主表或统计方向类')
_white_rule_selectors = ','.join(f'#rules-lamp-table tbody td:first-of-type .{cls}'
                               for cls in ('ar-band-up', 'ar-band-down', 'ar-rsi-up', 'ar-rsi-down'))
check(_white_rule_selectors + '{color:var(--white)}' in re.sub(r'\s+', ' ', _arrow_css).replace(', ', ',')
      and '.card .rules-body #rules-lamp-table .rule-reverse{color:var(--dim)}' in _arrow_css,
      '规则表条件列四白箭头与反向灰字仍原样保留，不受涨跌色覆盖')

# 主表投影贯通完整渲染；统计单选、多选数据及原始技术事实保持原样。
_projection_row = item('PROJECTION', 'position', 'red')
_projection_row.update(price=1290.123, boll_dn=1300, low52=1230.126, dist_low=2.6,
    signals=['跌破布林下轨▼（盘中触及 1,280.25）', '连续第4日贴近下轨（其中2日穿越）',
             'RSI 3条 ↓超卖（≤28.5）',
             '日内触及120日均线 ±1%区间（日内 1,230.00~1,300.00，现价偏离 +0.1%）',
             '触及52周新低（距低点 +2.6%）'],
    signal_marks=[('boll_rsi', 'red'), ('ma_band', 'yellow'), ('low52', 'red')],
    ma_settings={'ma_short': 60, 'ma_long': 120}, ma_values={'60': 1111.111, '120': 1234.567})
_projection_before = _copy.deepcopy(_projection_row)
_projection_groups = monitor._signal_description_groups(_projection_row)
_projection_snapshot = monitor.build_snapshot({}, [_projection_row], {}, group_counts={})
_projection_snapshot_before = _copy.deepcopy(_projection_snapshot)
_projection_card = monitor._signal_counts_card(_projection_snapshot)
_projection_filters = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _projection_card)[1]))
_projection_page = monitor.render({}, [_projection_row], 1, snapshot=_projection_snapshot)
_, _projection_stock = rendered_stock_row(_projection_page, _projection_row, False)
_projection_sig = re.search(r'<td class="sig">(.*?)</td>', _projection_stock, re.S)[1]
check(_signal_text(_projection_sig) ==
      '跌破布林下轨▼，下轨外，第4日贴近下轨（2日穿越） · RSI 3条 ↓超卖 / '
      '触及52周新低（距低点 +2.6%）（低点价：1,230.13） / '
      '触及长均线 ±1%区间（均线价：1,234.57）',
      '完整主表仅投影阈值、触及价、下轨状态、真实均线价与52周低点')
check(_signal_pairs(_projection_sig) ==
      {('boll_rsi', 'red'), ('low52', 'red'), ('ma', 'yellow')}
      and re.findall(r'<span class="(arrow-[a-z-]+)">([▲▼↑↓])</span>', _projection_sig) ==
      [('arrow-band-down', '▼'), ('arrow-rsi-down', '↓')]
      and 'signal-dot' not in _projection_stock,
      '新投影仍使用原指标红黄级别及独立轨道/RSI箭头class')
check(_projection_card in _projection_page
      and _projection_filters == _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _projection_page)[1]))
      and '52周低 1,230.13' in _projection_card
      and _projection_snapshot['signal_members']['low52']['red'][0]['low52_price'] == '1,230.13'
      and _projection_snapshot['signal_members']['ma']['yellow'][0]['ma_facts'][0]['text'] ==
      _projection_row['signals'][3],
      '完整统计与单选多选数据不走主表投影，低点数字同源且原均线日内/偏离事实不丢')
check(monitor._signal_description_groups(_projection_row) == _projection_groups
      and _projection_row == _projection_before and _projection_snapshot == _projection_snapshot_before,
      '投影不突变signals/marks、详细描述分组或统计快照')
for _projection_price, _projection_state in (
        (1299.999999999, '跌破布林下轨▼，下轨外'),
        (1300, '盘中跌破布林下轨▼后收回'),
        (1300.000000001, '盘中跌破布林下轨▼后收回'),
        (None, '跌破布林下轨▼'), (0, '跌破布林下轨▼'), (float('nan'), '跌破布林下轨▼')):
    _projection_control = dict(_projection_row, price=_projection_price,
        signals=_projection_row['signals'][:3], signal_marks=[('boll_rsi', 'red')])
    check(_signal_text(monitor._signals_html(_projection_control)) ==
          _projection_state + '，第4日贴近下轨（2日穿越） · RSI 3条 ↓超卖',
          repr(_projection_price) + '下轨严格高低/等于收回，缺失或无效不猜状态')
for _upper_price, _upper_state in (
        (1300.000000001, '突破布林上轨▲，上轨外'),
        (1300, '盘中突破布林上轨▲后收回'),
        (1299.999999999, '盘中突破布林上轨▲后收回'),
        (None, '盘中突破布林上轨▲'), (0, '盘中突破布林上轨▲'),
        (float('nan'), '盘中突破布林上轨▲')):
    _upper_row = item('UPPROJECTION', 'position', 'yellow')
    _upper_row.update(price=_upper_price, boll_up=1300,
        signals=['盘中突破布林上轨▲（盘中触及 1,320.25）', 'RSI 2条 ↑超买（≥70）'],
        signal_marks=[('boll_rsi', 'yellow')])
    _upper_before = _copy.deepcopy(_upper_row)
    _upper_html = monitor._signals_html(_upper_row)
    check(_signal_text(_upper_html) == _upper_state + ' · RSI 2条 ↑超买'
          and _upper_html.count('<span class="arrow-band-up">▲</span>') == 1
          and 'data-signal-level="yellow"' in _upper_html and _upper_row == _upper_before,
          repr(_upper_price) + '上轨外不加括号/等于及回落统一后收回，原事实和箭头级别不变')
for _projection_values, _projection_ma_label in (({'120': 1234.567}, '1,234.57'),
        ({}, '—'), ({'120': 0}, '—'), ({'120': float('nan')}, '—')):
    _projection_control = dict(_projection_row, ma_values=_projection_values,
        signals=[_projection_row['signals'][3]], signal_marks=[('ma_band', 'yellow')])
    check(_signal_text(monitor._signals_html(_projection_control)) ==
          '触及长均线 ±1%区间（均线价：' + _projection_ma_label + '）',
          repr(_projection_values) + '主表均线用真实值，缺失/0/NaN留—且不从现价或偏离反推')
check(not hasattr(monitor, '_main_description_text'), '主表投影helper不新增monitor导出')

# 输出上下文回归：只用合成字段，既不能闭合属性/脚本，也不能双转义显示文本。
from html.parser import HTMLParser as _HTMLParser

class _OutputProbe(_HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.text = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)

# data-symbol 保存规范化代码，不能从已丢失标点/大小写的锚点反推代码。
for _raw_symbol, _normalized_symbol, _anchor in (
        (' aapl-us ', 'AAPL', 's-aapl'),
        ('31#00700-HK', '0700.HK', 's-0700-hk'),
        ('.ndx', '.NDX', 's-ndx'),
        ('a&b"\'<x>', 'A&B"\'<X>', 's-a-b-x')):
    _symbol_html = monitor._stock_row(item(_raw_symbol, 'position', 'green'), snapshot)
    _symbol_probe = _OutputProbe(_symbol_html)
    _tag, _attrs = _symbol_probe.tags[0]
    check(_tag == 'tr' and _attrs.get('data-symbol') == _normalized_symbol
          and _attrs.get('id') == _anchor == monitor._stock_anchor_id(_raw_symbol)
          and _attrs['data-symbol'] != _attrs['id']
          and _symbol_html.split('>', 1)[0].count('data-symbol=') == 1,
          repr(_raw_symbol) + '主行情行保留规范化代码，锚点不变且与代码分离')
    if _raw_symbol.startswith('a&b'):
        check('data-symbol="A&amp;B&quot;&#x27;&lt;X&gt;"' in _symbol_html
              and not any(tag == 'x' or any(key.startswith('on') for key in attrs)
                          for tag, attrs in _symbol_probe.tags),
              'data-symbol 的单双引号、&及尖括号按属性上下文单次转义且可无损解析')

_inject = '</script><script>globalThis.__mmInjected=1</script><img src=x onerror="bad()">'
_symbol = '<img src=x onerror="bad()">'
_escaped_row = item(_symbol, 'position', 'green', note='中文 & "引号" <b>备注</b>')
_escaped_row['signals'] = ['普通描述 ' + _inject]
_row_html = monitor._stock_row(_escaped_row, snapshot)
_probe = _OutputProbe(_row_html)
check(not any(tag in ('img', 'script') or any(key.startswith('on') for key in attrs)
              for tag, attrs in _probe.tags)
      and monitor.normalize_symbol(_symbol) in ''.join(_probe.text)
      and _escaped_row['note'] in ''.join(_probe.text),
      '主表代码、备注、描述作为文字显示，单次转义且不新增标签或事件属性')
_attr_row = dict(_escaped_row, symbol='ATTR', level='green" onmouseover="bad()')
_attr_probe = _OutputProbe(monitor._stock_row(_attr_row, snapshot))
check(_attr_probe.tags[0][1]['class'] == _attr_row['level']
      and not any(key.startswith('on') for _, attrs in _attr_probe.tags for key in attrs),
      '行级class字符串不能逃逸双引号属性')
_unsafe_group = dict(monitor.GROUPS[2], key='test" onfocus="bad()', label='中文 & <b>分组</b>')
_group_html = monitor._group_card(_unsafe_group, [], {}, snapshot)
_group_probe = _OutputProbe(_group_html)
check(_group_probe.tags[0][1]['id'] == 'group_' + _unsafe_group['key']
      and _unsafe_group['label'] in ''.join(_group_probe.text)
      and not any(tag == 'b' or any(key.startswith('on') for key in attrs) for tag, attrs in _group_probe.tags),
      '分组名称和id按各自上下文转义，不注入标签或事件')
_plain_direction = 'A&B <待确认>'
_direction_html = monitor._member_icons({'direction': _plain_direction}, 'boll_rsi', 'red')
check(''.join(_OutputProbe(_direction_html).text).strip() == _plain_direction,
      '旧方向兜底文字只转义一次，不显示&amp;等二次编码')
_unsafe_snapshot = dict(snapshot, probe=_inject + '\u2028\u2029',
                        actual_dates={'min': '2026-10-06', 'max': _inject})
_unsafe_page = monitor.render({}, [item('SAFE', 'position', 'green')], 1, snapshot=_unsafe_snapshot)
_embedded = re.search(r'<script id="snapshotData" type="application/json">(.*?)</script>', _unsafe_page, re.S)[1]
check('<' not in _embedded and '\u2028' not in _embedded and '\u2029' not in _embedded
      and _json.loads(_embedded)['probe'] == _unsafe_snapshot['probe']
      and not any(tag == 'img' for tag, _ in _OutputProbe(_unsafe_page).tags),
      '快照统一使用脚本安全JSON，日期文案也不能注入HTML且JSON无损往返')
_seed_payload = {'positions': {_inject: {'cost': 25}}}
with _patch('io.open', _mock_open(read_data=_json.dumps(_seed_payload))), \
        _patch('os.path.exists', return_value=False), \
        _patch.object(monitor, '_position_symbols', return_value=[_inject]):
    _seed_text = _real_cost_seed_json([{'symbol': _inject, 'rs_spy': {
        'bench': 1, 'etf': 2, 'days': 20, 'end': '2026-10-06', 'start': '2026-09-08',
        'bench_name': _inject + '\u2028\u2029'}}])
check('<' not in _seed_text and '\u2028' not in _seed_text and '\u2029' not in _seed_text
      and _json.loads(_seed_text)['items'][_inject]['cost'] == 25
      and 'rs20' not in _json.loads(_seed_text),
      '成本种子代码安全JSON序列化，不允许script结束标签且保留原值，且不再带 rs20')
with _patch.object(monitor, '_cost_seed_json', return_value=_seed_text):
    _safe_cost_css, _safe_cost_js = monitor._cost_panel_assets([])
    _unsafe_page = monitor.render({}, [_escaped_row], 1, snapshot=_unsafe_snapshot)
check(_inject not in _safe_cost_js and '__MM_REAL__' not in _safe_cost_js
      and _safe_cost_js.count('<script>') == _safe_cost_js.count('</script>'),
      '合成恶意种子内联到实际成本脚本后不产生额外script标签')

# 显式 --chrome 才启动全新隔离实例；HTML 经 CDP 放入 about:blank 内存页。
if '--chrome' in sys.argv:
    # 生产渲染合成红/黄价格配对，并保留白灯及仅文字同股样本；不读取真实仓位。
    _color_rows = [_copy.deepcopy(_order_row)]
    for _chg_level in ('red', 'yellow'):
        _color_row = item('COLOR' + _chg_level.upper(), 'technology', _chg_level)
        _color_row.update(signals=[('异动 -12.0%' if _chg_level == 'red' else '波动 +8.2%')],
                          signal_marks=[('chg', _chg_level)])
        _color_rows.append(_color_row)
    with _patch.object(monitor, 'global_dca', return_value=None):
        _color_snapshot = monitor.build_snapshot({}, _color_rows, {}, group_counts={})
        _color_page = monitor.render({}, _color_rows, len(_color_rows), snapshot=_color_snapshot)
    _filter_snapshot = _copy.deepcopy(_description_snapshot)
    _filter_snapshot['target_trade_date'] = '2026-10-26'
    _filter_snapshot['earnings_week'] = {'asof': '2026-10-26', 'members': [
        {'symbol': 'TBOTH', 'note': '合成财报', 'date': '2026-10-27', 'timing': 'post', 'group': 'position', 'level': 'green'},
        {'symbol': 'TSPACE', 'note': '合成财报', 'date': '2026-10-29', 'timing': 'pre', 'group': 'technology', 'level': 'green'}]}
    with _patch.object(monitor, 'global_dca', return_value=None):
        _filter_page = monitor.render({}, _description_rows, len(_description_rows), snapshot=_filter_snapshot)
    # 固定北京运行日的财报显示样本，覆盖红/黄/无警及ET待定；不读真实财报或改变算法。
    _time_cases = [('EPREV', '2026-10-25', 'pre'), ('ENOW', '2026-10-26', 'pre'),
                   ('ERED', '2026-10-27', 'post'), ('EYELLOW', '2026-10-29', 'pre'),
                   ('EFAR', '2026-11-05', 'post'), ('ETBD', '2026-11-06', 'unknown')]
    _time_rows = []
    for _sym, _date, _timing in _time_cases:
        _row = item(_sym, 'technology', 'green')
        _row.update(earnings={'status': 'ok', 'date': _date, 'timing': _timing, 'kind': 'expected'})
        _time_rows.append(_row)
    _time_snapshot = _copy.deepcopy(snapshot)
    _time_snapshot.update(finished_at_bj='2026-10-26T20:00:00+08:00',
                          finished_at='2026-10-26T12:00:00+00:00', target_trade_date='2026-10-26',
                          earnings_week={'asof': '2026-10-26', 'members': [
                              {'symbol': sym, 'date': date, 'timing': timing, 'group': 'technology', 'level': 'green'}
                              for sym, date, timing in _time_cases]})
    with _patch.object(monitor, 'global_dca', return_value=None):
        _time_page = monitor.render({}, _time_rows, len(_time_rows), snapshot=_time_snapshot)
    # 真实生成设置页CSS + 正式renderer说明模板；去脚本后本地挂载，绝不抓线上说明。
    with open(os.path.join(ROOT, 'edit.html'), encoding='utf-8') as _stream:
        _rules_page = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', _stream.read(), flags=re.I)
    _rules_markup = ''
    for _tid, _host in (('rulesTpl', 'signalRules'), ('glossaryTpl', 'glossaryRules')):
        _content = re.search(r'<template id="' + _tid + r'">([\s\S]*?)</template>', _description_page)[1]
        _rules_markup += '<details class="card fold rule-card" id="' + _host + '" open>' + _content + '</details>'
    _notes = re.search(r'<template id="notesTpl">([\s\S]*?)</template>', _description_page)[1]
    _notes = re.sub(r'^\s*<section\b[^>]*>|</section>\s*$', '', _notes)
    _rules_markup += _notes.replace('class="macro-help"', 'class="card fold"', 1)
    _rules_page = _rules_page.replace('<div id="rulesHost">', '<div id="rulesHost">' + _rules_markup, 1)
    _range_rows = []
    for _sym, _low, _price, _high in (('RANGENORM', 100.0, 150.0, 200.0), ('RANGEHI', 420.0, 688.0, 692.0)):
        _range_row = item(_sym, 'position', 'green')
        _range_row.update(price=_price, data_date='2026-10-26',
                          range52={'low': _low, 'high': _high, 'scope': '52周', 'from': '2025-10-27',
                                   'to': '2026-10-26', 'position': (_price - _low) / (_high - _low) * 100})
        _range_rows.append(_range_row)
    # 通过正式渲染入口注入两行合成持仓，不对JS源码的空白/换行做替换。
    _range_seed = {"asof": "2026-10-26", "cash": 0, "cash_extra": 0, "fixed": 0, "fixed_cost": 0,
                   "items": {"RANGENORM": {"cost": 125.0, "qty": 10}, "RANGEHI": {"cost": 600.0, "qty": 5}},
                   "rs20": None}
    with _patch.object(monitor, 'global_dca', return_value=None), \
            _patch.object(monitor, '_cost_seed_json', return_value=monitor.json_script(_range_seed)):
        _range_snapshot = monitor.build_snapshot({}, _range_rows, {}, group_counts={})
        _range_page = monitor.render({}, _range_rows, len(_range_rows), snapshot=_range_snapshot)
    # 独立交易/布局样本：GOOGL完整代码、真实rs20与长短两区；所有金额均为合成测试输入。
    _trade_rows = [item('GOOGL', 'position', 'green', note='合成长持仓'),
                   item('HII', 'position', 'yellow', note='合成短持仓')]
    for _row, _hold in zip(_trade_rows, ('long', 'short')):
        _row.update(hold=_hold, data_date='2026-10-26', price=100.0, chg=1.0)
    _trade_seed = {'asof': '2026-10-26', 'cash': 300, 'cash_extra': 100, 'fixed': 200, 'fixed_cost': 180,
                   'items': {'GOOGL': {'cost': 100, 'qty': 10}, 'HII': {'cost': 80, 'qty': 5}},
                   'rs20': {'bench': 1, 'items': {'GOOGL': 10, 'HII': -5}, 'days': 20, 'end': '2026-10-26',
                            'start': '2026-09-28', 'bench_name': '合成基准'}}
    with _patch.object(monitor, 'global_dca', return_value=None), \
            _patch.object(monitor, '_cost_seed_json', return_value=monitor.json_script(_trade_seed)):
        _trade_snapshot = monitor.build_snapshot({}, _trade_rows, {}, group_counts={})
        _trade_page = monitor.render({}, _trade_rows, len(_trade_rows), snapshot=_trade_snapshot)
    _negative_seed = _json.loads(_json.dumps(_trade_seed))
    _negative_seed['items']['GOOGL']['cost'] = -10
    _negative_seed['items']['HII']['cost'] = 0
    with _patch.object(monitor, 'global_dca', return_value=None), \
            _patch.object(monitor, '_cost_seed_json', return_value=monitor.json_script(_negative_seed)):
        _negative_page = monitor.render({}, _trade_rows, len(_trade_rows), snapshot=_trade_snapshot)
    # 仅显式本地验收加载唯一用户终稿。CI门禁不依赖未跟踪outputs；绝不执行其中真实持仓种子。
    _final_demo = None
    if '--final-demo' in sys.argv:
        _final_path = os.path.join(ROOT, 'outputs', '终稿移植验收-2026-11-09',
            '完整页面离线DEMO-手机当日面板调整-字号跳转版-持仓标题修正版-2026-11-10.html')
        with open(_final_path, encoding='utf-8') as _stream:
            _final_demo = _stream.read()
        _decl = list(re.finditer(r'\bvar\s+REAL\s*=\s*', _final_demo))
        if len(_decl) != 1:
            raise AssertionError('唯一终稿参考必须有且仅有一份REAL声明')
        _start = _decl[0].end()
        _, _end = _json.JSONDecoder().raw_decode(_final_demo[_start:])
        _final_demo = _final_demo[:_start] + monitor.json_script(_trade_seed) + _final_demo[_start + _end:]
        # 参考保留全部样式/脚本；仅内存将两标的原始行情对齐生产合成fixture，避免历史涨跌颜色假差异。
        for _market_row in _trade_rows:
            _sym = re.escape(_market_row['symbol'])
            def _synthetic_market_tag(match):
                tag = match.group(0)
                for name, value in [('data-price', 100.0), ('data-prev-close', 100.0 / 1.01)]:
                    tag = re.sub(r'\s' + name + r'="[^"]*"', '', tag)
                    tag = tag[:-1] + ' ' + name + '="' + str(value) + '">'
                return tag
            _final_demo, _matches = re.subn(r'<tr\b[^>]*\bdata-symbol="' + _sym + r'"[^>]*>', _synthetic_market_tag, _final_demo)
            if _matches != 1:
                raise AssertionError('终稿合成行情匹配必须唯一：' + _market_row['symbol'])
    _expected_browser_seed = {
        "asof": "", "cash": 0, "cash_extra": 0, "fixed": 0, "fixed_cost": 0, "items": {}, "rs20": None}

    def _assert_browser_seed(page, expected, label):
        declarations = list(re.finditer(r'\bvar\s+REAL\s*=\s*', page))
        if len(declarations) != 1:
            raise AssertionError(label + '必须且只能包含一个合成成本种子，拒绝启动浏览器')
        tail = page[declarations[0].end():]
        seed, end = _json.JSONDecoder().raw_decode(tail)
        if not tail[end:].lstrip().startswith(';') or seed != expected:
            raise AssertionError(label + '成本种子不是预期合成JSON，拒绝启动浏览器')

    for _seed_declaration in ('var REAL=', '    var REAL = ', 'var\tREAL\n=\n'):
        _assert_browser_seed(_seed_declaration + monitor.json_script(_expected_browser_seed) + ' ;',
                             _expected_browser_seed, '合法空白回归')
    check(True, '种子验证兼容合法缩进、空格及换行，不依赖源码格式')
    for _invalid_seed_source in ('', 'var REAL={};', 'var REAL={} var next=1;',
                                 'var REAL={};var REAL={};'):
        try:
            _assert_browser_seed(_invalid_seed_source, _expected_browser_seed, '拒绝回归')
        except (AssertionError, ValueError):
            pass
        else:
            raise AssertionError('种子缺失/不匹配/重复或缺少分号未被拒绝')
    check(True, '种子缺失、不匹配、重复及非法声明仍严格拒绝')

    for _browser_page, _browser_seed, _browser_label in (
            (_description_page, _expected_browser_seed, '目标/刷新页'),
            (_color_page, _expected_browser_seed, '颜色页'),
            (_filter_page, _expected_browser_seed, '紫灯财报多选页'),
            (_time_page, _expected_browser_seed, '财报时间颜色页'),
            (_range_page, _range_seed, '52周页'),
            (_trade_page, _trade_seed, '五列交易与持仓框页'),
            (_negative_page, _negative_seed, '零负成本归一化与交易页'),
            (_unsafe_page, _json.loads(_seed_text), '注入安全页')):
        _assert_browser_seed(_browser_page, _browser_seed, _browser_label)
        check(True, _browser_label + '成本种子JSON与预期合成输入完全一致，不嵌入真实仓位')
    import subprocess as _subprocess
    import tempfile as _tempfile
    _chrome = os.environ.get('MM_TEST_CHROME', 'C:/Program Files/Google/Chrome/Application/chrome.exe')
    # 真实设置render的合成两股/每组fixture，无脚本、授权、网络或真实仓位。
    _settings_result = _subprocess.run([sys.executable, '-B', os.path.join(ROOT, 'tests/run_all.py'),
        '--offline-node', os.path.join(ROOT, 'tests/mm_test_edit.js'), '--layout-fixture'],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    if _settings_result.returncode:
        raise AssertionError('设置两排fixture生成失败：' + _settings_result.stderr)
    _settings_fixture = _json.loads(_settings_result.stdout)
    _browser_script = r'''
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
// 使用正式无Web Locks隔离分支，而非已退役的REAL.preview。每份文档独立内存存储，
// 不包装fetch：任何意外云请求仍由CDP拦截并使末尾网络门禁失败。
const isolateScript = `<script>(()=>{
  const memory=()=>{const m=new Map();return {getItem:k=>m.has(k)?m.get(k):null,setItem:(k,v)=>m.set(k,String(v)),removeItem:k=>m.delete(k),clear:()=>m.clear(),key:i=>[...m.keys()][i]||null,get length(){return m.size;}};};
  Object.defineProperty(window,'localStorage',{configurable:true,value:memory()});
  Object.defineProperty(window,'sessionStorage',{configurable:true,value:memory()});
  Object.defineProperty(navigator,'locks',{configurable:true,value:undefined});
  window.__testCostPainted=0;
  document.addEventListener('cost:painted',()=>window.__testCostPainted++);
})();</script>`;
const syntheticPage = html => html.replace(/<script\b[^>]*\bsrc=[^>]*>[\s\S]*?<\/script>/gi,'')
  .replace(/<link\b[^>]*>/gi,'').replace(/<head\b[^>]*>/i, match=>match+isolateScript)
  .replace('</head>','<link rel="icon" href="data:,"></head>');
// 四组浏览器回归共享手机/平板横竖屏及桌面视口，禁止各自维护宽度。
const VIEWPORTS = Object.freeze([
  [440, 956], [956, 440], [744, 1133], [1133, 744], [1440, 1000], [1920, 1080]
].map(pair => Object.freeze(pair)));
const assertions = [{
  ok: VIEWPORTS.map(([width]) => width).join(',') === '440,956,744,1133,1440,1920',
  name: '共享浏览器目标宽度列表及顺序固定为六档，防止覆盖范围漂移', actual: VIEWPORTS
}];
const failedRequests = [];
const requestUrls = new Set();
let safetySeed=null;
const safetyReads=new Set(),safetyRequests=[];
const chrome = spawn(input.chrome, [
  // 仅一次性 GitHub hosted Linux CI 的合成内存页允许绕过不可用的 userns sandbox。
  ...(input.ciNoSandbox ? ['--no-sandbox'] : []),
  '--headless=new', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
  '--user-data-dir=' + input.profile, '--no-first-run', '--no-default-browser-check',
  '--disable-background-networking', '--disable-component-update', '--disable-sync',
  '--disable-extensions', '--disable-domain-reliability', '--no-proxy-server',
  '--host-resolver-rules=MAP * ~NOTFOUND',
  '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication', 'about:blank'
], {stdio: ['ignore', 'ignore', 'pipe']});
let ws, call, session, diagnostic = '';
const started = performance.now();
const pending = new Map();
let phase = '启动浏览器';
const stage = name => {
  phase = name;
  process.stderr.write(`[Chrome ${((performance.now()-started)/1000).toFixed(2)}s] ${name}；已完成断言 ${assertions.length}\n`);
};
const deadline = setTimeout(() => {
  const waiting = [...pending.values()].map(job => ({method:job.method,
    seconds:+((performance.now()-job.started)/1000).toFixed(2)}));
  process.stderr.write('Chromium 合成测试超过60秒，终止独立实例\n'
    + JSON.stringify({phase, assertions:assertions.length, waiting}) + '\n' + diagnostic);
  chrome.kill(); process.exit(2);
}, 60000);
(async () => {
  try {
    const endpoint = await new Promise((resolve, reject) => {
      chrome.once('error', reject);
      chrome.once('exit', code => reject(new Error('Chrome提前退出：' + code + diagnostic)));
      chrome.stderr.on('data', chunk => {
        diagnostic += chunk.toString();
        const match = diagnostic.match(/DevTools listening on (ws:\/\/127\.0\.0\.1:[^\s]+)/);
        if (match) resolve(match[1]);
      });
    });
    stage('DevTools入口就绪，连接WebSocket');
    ws = new WebSocket(endpoint);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, {once: true});
      ws.addEventListener('error', reject, {once: true});
    });
    stage('WebSocket就绪，初始化CDP');
    let seq = 0;
    let refreshDocument = null;
    const refreshBase = 'https://mm-refresh.test/market-monitor/index.html';
    call = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, {resolve, reject, method, started:performance.now()});
      try {
        ws.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
      } catch (error) { pending.delete(id); reject(error); }
    });
    ws.addEventListener('message', event => {
      const msg = JSON.parse(event.data);
      if (pending.has(msg.id)) {
        const job = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) job.reject(new Error(JSON.stringify(msg.error)));
        else job.resolve(msg.result);
      } else if (msg.method === 'Network.requestWillBeSent') {
        requestUrls.add(msg.params.request.url);
      } else if (msg.method === 'Fetch.requestPaused') {
        requestUrls.add(msg.params.request.url);
        if (refreshDocument && msg.params.resourceType === 'Document'
            && msg.params.request.url.split(/[?#]/)[0] === refreshBase) {
          call('Fetch.fulfillRequest', {requestId: msg.params.requestId, responseCode: 200,
            responseHeaders: [{name:'Content-Type',value:'text/html; charset=utf-8'}],
            body:Buffer.from(refreshDocument).toString('base64')}, msg.sessionId).catch(() => {});
        } else if(safetySeed&&msg.params.request.method==='GET'&&['Fetch','XHR'].includes(msg.params.resourceType)
            &&msg.params.request.url.startsWith('https://raw.githubusercontent.com/nixhuang/market-monitor/data/position-costs.json?')){
          // 显式无Token真实保存门：仅此合成GET响应，绝不转发仓位请求或允许PUT。
          safetyReads.add(msg.params.request.url);safetyRequests.push({url:msg.params.request.url,method:msg.params.request.method});
          call('Fetch.fulfillRequest',{requestId:msg.params.requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:'application/json'},{name:'Access-Control-Allow-Origin',value:'*'}],body:Buffer.from(JSON.stringify(safetySeed)).toString('base64')},msg.sessionId).catch(()=>{});
        } else {
          if(safetySeed)safetyRequests.push({url:msg.params.request.url,method:msg.params.request.method});
          call('Fetch.failRequest', {requestId: msg.params.requestId, errorReason: 'InternetDisconnected'}, msg.sessionId)
            .catch(() => {});
        }
      } else if (msg.method === 'Network.loadingFailed') {
        failedRequests.push(msg.params.errorText);
      }
    });
    ws.addEventListener('close', () => {
      for (const job of pending.values()) job.reject(new Error('CDP连接提前关闭：'+job.method));
      pending.clear();
    });
    const target = await call('Target.createTarget', {url: 'about:blank'});
    session = (await call('Target.attachToTarget', {targetId: target.targetId, flatten: true})).sessionId;
    const cdp = (method, params) => call(method, params, session);
    await cdp('Page.enable');
    await cdp('Runtime.enable');
    await cdp('Network.enable');
    await cdp('Network.setBypassServiceWorker', {bypass: true});
    await cdp('Network.emulateNetworkConditions', {offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0});
    await cdp('Fetch.enable', {patterns: [{urlPattern: '*'}]});
    const tree = await cdp('Page.getFrameTree');
    await cdp('Page.setDocumentContent', {frameId: tree.frameTree.frame.id, html: syntheticPage(input.html)});
    const evaluate = async expression => {
      const result = await cdp('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const settled = () => evaluate(`(async () => {
      await document.fonts.ready;
      const start=performance.now();let previous='',since=start;
      while(performance.now()-start<5000){
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const current=JSON.stringify([...document.querySelectorAll('table.stk tr[data-symbol],.range52-rail,.cashbond-row')].map(e=>{
          const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height,e.textContent];}));
        if(current!==previous){previous=current;since=performance.now();}
        if(window.__testCostPainted>0&&performance.now()-since>=100)return;
      }
      throw Error('cost:painted/字体/布局未在5秒内稳定');
    })()`);
    stage('首份合成页已注入，等待字体与布局');
    await settled();
    stage('字体与布局就绪，验证网络隔离');
    const isolation = await evaluate(`(async () => {
      let blocked = false;
      try { await fetch('http://127.0.0.1:65530/offline-probe'); } catch (_) { blocked = true; }
      return {href: location.href, origin: location.origin, blocked,
        isolated:!(navigator.locks&&navigator.locks.request),painted:window.__testCostPainted,
        tokenPresent:localStorage.getItem('mm_gh_token_v1')!==null};
    })()`);
    isolation.requestFailures = failedRequests;
    assertions.push({ok: isolation.href === 'about:blank' && isolation.origin === 'null' && isolation.blocked
      && isolation.isolated && isolation.painted>0 && !isolation.tokenPresent
      && failedRequests.includes('net::ERR_INTERNET_DISCONNECTED'),
      name: '独立Chrome内存页与请求阻断', actual: isolation});
    const clickSummary = async () => {
      const point = await evaluate(`(() => {
        const summary = document.querySelector('#signalCounts > summary');
        summary.scrollIntoView({block:'center'});
        const rect = summary.getBoundingClientRect();
        return {x:rect.left + Math.min(rect.width / 2, 80), y:rect.top + rect.height / 2};
      })()`);
      await cdp('Input.dispatchMouseEvent', {type:'mousePressed', ...point, button:'left', clickCount:1});
      await cdp('Input.dispatchMouseEvent', {type:'mouseReleased', ...point, button:'left', clickCount:1});
      return evaluate(`new Promise(resolve => requestAnimationFrame(() => resolve(document.getElementById('signalCounts').open)))`);
    };
    for (const [width, height] of VIEWPORTS) {
      await cdp('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
      await settled();
      const openedBefore = await evaluate(`document.getElementById('signalCounts').open`);
      const openedAfterClick = await clickSummary();
      const openedAfterSecondClick = await clickSummary();
      assertions.push({ok: openedAfterClick !== openedBefore && openedAfterSecondClick === openedBefore,
        name: width + 'px真实鼠标点击统计标题可收起并重新展开',
        actual: {openedBefore, openedAfterClick, openedAfterSecondClick}});
      const header = await evaluate(`(() => {
        const stats = document.querySelector('#signalCounts > summary .group-stats');
        const purple = stats.querySelector('.stat-badge.purple');
        const snapshot = JSON.parse(document.getElementById('snapshotData').textContent);
        return {order: [...stats.children].map(el => el.className),
          count: purple.textContent, expected: snapshot.summary.purple,
          border: getComputedStyle(purple).borderTopColor,
          overflow: document.documentElement.scrollWidth > innerWidth};
      })()`);
      assertions.push({ok: JSON.stringify(header.order) === JSON.stringify([
        'stat-badge red','stat-badge yellow','stat-badge gray','stat-badge purple','count-total count-earn'])
        && header.count === String(header.expected) && header.border === 'rgb(176, 132, 255)' && !header.overflow,
        name: width + 'px统计表头红黄灰紫财报顺序、紫灯同源及折叠重开后保留', actual: header});
      const statLines = await evaluate(`(() => {
        const card=document.getElementById('signalCounts'),extra=card.querySelector('.pnl-extra');
        const empty=[...extra.querySelectorAll('.signal-member-block')].map(block=>({
          hidden:block.hidden,display:getComputedStyle(block).display,count:block.querySelectorAll('li').length}));
        const buttons=[...card.querySelectorAll('.signal-filter-toggle')],pressed=buttons.map(b=>b.getAttribute('aria-pressed'));
        const data=signalFilterData(),keys=buttons.map(b=>b.dataset.kind);let pair=null;
        for(let i=0;i<keys.length&&!pair;i++)for(let j=i+1;j<keys.length;j++){
          const result=intersectSignals(data,[keys[i],keys[j]]);
          if(!result.pending&&result.members.length){pair=[keys[i],keys[j]];break;}
        }
        buttons.forEach(b=>b.setAttribute('aria-pressed',pair&&pair.includes(b.dataset.kind)?'true':'false'));
        renderSignalFilter();
        const hidden=[...card.querySelectorAll('[hidden]')].map(el=>[el,el.hidden]);
        const opened=[card,...card.querySelectorAll('details')].map(el=>[el,el.open]);
        hidden.forEach(([el])=>el.hidden=false);opened.forEach(([el])=>el.open=true);
        const headings=[...card.querySelectorAll('.signal-member-block h5')].map(h=>{
          const s=getComputedStyle(h),line=getComputedStyle(h,'::after');
          return {title:h.textContent,filter:!!h.closest('.signal-filter'),pnl:!!h.closest('.pnl-extra'),
            display:s.display,align:s.alignItems,content:line.content,height:line.height,width:parseFloat(line.width),
            gap:line.marginLeft,color:line.backgroundColor,headWidth:h.getBoundingClientRect().width};
        });
        hidden.forEach(([el,value])=>el.hidden=value);opened.forEach(([el,value])=>el.open=value);
        buttons.forEach((b,i)=>pressed[i]===null?b.removeAttribute('aria-pressed'):b.setAttribute('aria-pressed',pressed[i]));
        renderSignalFilter();
        return {empty,headings,pair};
      })()`);
      assertions.push({ok:statLines.empty.length===4&&statLines.empty.every(b=>b.hidden&&b.display==='none'&&b.count===0)
        &&statLines.pair&&statLines.headings.some(h=>h.filter)&&statLines.headings.filter(h=>h.pnl).length===4
        &&statLines.headings.some(h=>!h.filter&&!h.pnl)&&statLines.headings.every(h=>h.display==='flex'&&h.align==='center'
          &&h.content==='""'&&h.height==='1px'&&h.width>0&&h.width<h.headWidth&&h.gap==='8px'&&h.color==='rgb(58, 67, 86)'),
        name:width+'px统计空盈亏分类不占位，静态名单及真实多选交集标题均有右侧淡长线',actual:statLines});
      await evaluate(`document.querySelectorAll('details.group-card,details.quiet-list').forEach(el => el.open = true)`);
      await settled();
      const actual = await evaluate(`(() => {
        const high = document.querySelector('#s-thigh .lvtag.tgt.high');
        const space = document.querySelector('#s-tspace .lvtag.tgt.space');
        const hit = document.querySelector('#s-tboth .lvtag.tgt.hit');
        const bad = document.querySelector('#s-tfailold .lvtag.tgt.bad');
        const low = document.querySelector('#s-tlow .lvtag.tgt.low');
        const style = el => getComputedStyle(el);
        const rect = el => { const r = el.getBoundingClientRect(); return [r.width, r.height]; };
        const originalSize = rect(high);
        high.style.color = '#b084ff';
        const priorSize = rect(high);
        high.style.removeProperty('color');
        const sparseBorder = el => {
          const after = getComputedStyle(el, '::after');
          const url = after.backgroundImage.match(/url\\(["']?(data:image\\/svg\\+xml,[^"')]+)["']?\\)/);
          const svg = url ? decodeURIComponent(url[1].split(',').slice(1).join(',')) : '';
          const borderRect = new DOMParser().parseFromString(svg, 'image/svg+xml').querySelector('rect');
          const original = rect(el);
          el.style.borderStyle = 'dashed';
          const nativeSize = rect(el);
          el.style.removeProperty('border-style');
          return {color: style(el).color, border: style(el).borderTopColor,
            width: style(el).borderTopWidth, radius: style(el).borderTopLeftRadius,
            afterContent: after.content, afterInset: [after.top, after.right, after.bottom, after.left],
            pointerEvents: after.pointerEvents, backgroundSize: after.backgroundSize,
            stroke: borderRect?.getAttribute('stroke'), strokeWidth: borderRect?.getAttribute('stroke-width'),
            dash: borderRect?.getAttribute('stroke-dasharray'), rx: borderRect?.getAttribute('rx'),
            original, nativeSize};
        };
        const stripes = [];
        for (const [state, capsule] of [['space', space], ['high', high], ['hit', hit]]) {
          const row = capsule.closest('tr');
          const originalClass = row.className;
          for (const [level, width, color] of [['red', 2, 'rgb(248, 81, 73)'],
              ['yellow', 2, 'rgb(210, 153, 34)'], ['green', 2, 'rgb(63, 185, 80)']]) {
            row.className = originalClass.replace(/\\b(red|yellow|green|gray)\\b/, level);
            const sig = style(row.querySelector('td.sig'));
            // 灯条 2026-11-03 起画在整行的 ::before 上（卡片版式里 td.sym 只有头部行那一格高，
            // 且格子边框会把行背景切成两段，只有绝对定位的伪元素能一条到底）
            const bar = getComputedStyle(row, '::before');
            const left = style(row.firstElementChild);
            const continuation = style(row.nextElementSibling.firstElementChild);
            stripes.push({state, level, width, color, sigImage: sig.backgroundImage,
              sigShadow: sig.boxShadow, rowColor: bar.backgroundColor, rowWidth: bar.width,
              rowContent: bar.content, rowTop:bar.top, rowHeight:bar.height,
              leftImage: left.backgroundImage, leftShadow: left.boxShadow,
              rangeImage: continuation.backgroundImage, rangeSize: continuation.backgroundSize,
              rangePosition: continuation.backgroundPosition});
          }
          row.className = originalClass;
        }
        return {
          stripes, sparseBorders: [sparseBorder(low), sparseBorder(bad)],
          highColor: style(high).color,
          highNumbers: [...high.querySelectorAll('.target-price,.target-month')].map(el => style(el).color),
          highPercent:style(high.querySelector('.target-space')).color,
          highSign:high.querySelector('.target-space').dataset.sign,
          highBorder: getComputedStyle(high.closest('td.tgs'), '::before').borderTopColor,
          highBorderWidth: getComputedStyle(high.closest('td.tgs'), '::before').borderTopWidth,
          highFrame:{inset:['top','right','bottom','left'].map(k=>getComputedStyle(high.closest('td.tgs'),'::before')[k]),radius:getComputedStyle(high.closest('td.tgs'),'::before').borderTopLeftRadius},
          highShadow: style(high).textShadow,
          originalSize, priorSize,
          spaceColor: style(space).color, spaceBorder: getComputedStyle(space.closest('td.tgs'), '::before').borderTopColor,
          hitColor: style(hit).color, hitBackground: getComputedStyle(hit.closest('td.tgs'), '::before').backgroundColor,
          hitFrame: {border:getComputedStyle(hit.closest('td.tgs'),'::before').borderTopWidth,radius:getComputedStyle(hit.closest('td.tgs'),'::before').borderTopLeftRadius},
          hitAfter: getComputedStyle(hit, '::after').content,
          hitClass: hit.closest('tr').className,
          badColor: style(bad).color, badBorderStyle: style(bad).borderTopStyle,
          lowBorder: style(low).borderTopColor,
          sig: document.querySelector('#s-thigh td.sig').textContent,
          manual: document.querySelector('#s-manhigh td.sig').textContent,
          runbarRuleLinks: document.querySelectorAll('.runbar a[href="#signalRules"]').length
        };
      })()`);
      const pink = 'rgb(220, 98, 184)', purple = 'rgb(176, 132, 255)';
      // 2026-11-11 用户：空间>=90% 只有外框紫红，数字沿用普通紫、不加粗影
      assertions.push({ok: actual.highColor === purple && actual.highBorder === pink
        && actual.highNumbers.length >= 1 && actual.highNumbers.every(color => color === purple)
        && actual.highSign === 'pos' && actual.highPercent === 'rgb(77, 208, 225)'
        && actual.highShadow === 'none' && actual.highBorderWidth === '1px'
        && JSON.stringify(actual.highFrame.inset) === JSON.stringify(['2px','0px','2px','-1px'])
        && actual.highFrame.radius === '3px',
        name: width + 'px终稿高空间框位于tgs、目标价格沿用普通紫、正空间百分比青色', actual});
      assertions.push({ok: actual.originalSize[0] > 0 && actual.originalSize[1] > 0
        && JSON.stringify(actual.originalSize) === JSON.stringify(actual.priorSize)
        && actual.spaceColor === 'rgb(176, 132, 255)' && actual.spaceBorder === 'rgb(176, 132, 255)'
        && actual.hitColor === 'rgb(255, 255, 255)' && actual.hitBackground === 'color(srgb 0.690196 0.517647 1 / 0.32)'
        && actual.hitFrame.border === '0px' && actual.hitFrame.radius === '0px'
        && actual.hitAfter === 'none' && actual.hitClass.includes('pur-hit') && !actual.hitClass.includes('pur-high')
        && actual.badColor === 'rgb(139, 147, 161)' && actual.badBorderStyle === 'none'
        && actual.sparseBorders[0].width === '0px' && actual.sparseBorders[0].afterContent === 'none'
        && !actual.sig.includes('共识') && !actual.manual.includes('目标价') && !actual.manual.includes('共识价')
        && actual.runbarRuleLinks === 0,
        name: width + 'px尺寸不变、达到优先、失败灰虚线及描述过滤', actual: {
          size: actual.originalSize, priorSize: actual.priorSize,
          hitColor: actual.hitColor, hitBackground: actual.hitBackground, badColor: actual.badColor,
          sig: actual.sig, manual: actual.manual, runbarRuleLinks: actual.runbarRuleLinks}});
      assertions.push({ok: actual.stripes.length === 9 && actual.stripes.every(s =>
        s.sigImage === 'none' && s.sigShadow === 'none'
        && s.rowContent === '""' && s.rowWidth === '2px' && s.rowColor === s.color
        && Math.abs(parseFloat(s.rowTop)-(width<=600?7:9.5))<0.02
        && Math.abs(parseFloat(s.rowHeight)-(width<=600?42.7:41.7))<0.02
        && s.leftImage === 'none' && s.leftShadow === 'none'
        && s.rangeImage === 'none' && s.rangeSize === 'auto'),
        name: width + 'px终稿目标状态保留2px固定左灯条、首格无重复灯条且52周不续接', actual: actual.stripes});
      assertions.push({ok: actual.sparseBorders.length === 2 && actual.sparseBorders.every((s, i) =>
        s.color === (i === 0 ? 'rgb(176, 132, 255)' : 'rgb(139, 147, 161)') && s.width === '0px'
        && (i === 0 ? s.afterContent === 'none' && s.radius === '0px'
          : s.radius === '4px' && s.afterContent !== 'none' && s.afterInset.every(value => value === '-1px')
            && s.pointerEvents === 'none' && s.backgroundSize === '100% 100%'
            && s.stroke === '#8b93a1' && s.strokeWidth === '1' && s.dash === '3 5' && s.rx === '4')
        && s.original[0] > 0 && s.original[1] > 0
        && JSON.stringify(s.original) === JSON.stringify(s.nativeSize)),
        name: width + 'px终稿low无边框、bad独有稀疏灰SVG虚线，尺寸与独立字色保留', actual: actual.sparseBorders});
    }
    // 真实导航/刷新仅由 CDP 提供合成文档；其他请求仍全部拦截，绝不读取云端仓位。
    refreshDocument = syntheticPage(input.html);
    await cdp('Network.emulateNetworkConditions', {offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
    const waitLoad = () => new Promise(resolve => {
      const onLoad = event => {
        const msg = JSON.parse(event.data);
        if (msg.method === 'Page.loadEventFired' && msg.sessionId === session) {
          ws.removeEventListener('message',onLoad); resolve();
        }
      };
      ws.addEventListener('message',onLoad);
    });
    for (const [width, height] of VIEWPORTS) {
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      let loaded=waitLoad();
      await cdp('Page.navigate',{url:refreshBase+'?keep=1&token=synthetic'}); await loaded;
      await evaluate(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
      const header=await evaluate(`(() => {
        const sub=document.querySelector('.sub'),mirror=document.getElementById('costSyncState');
        sub.firstChild.textContent='数据时间 2026-11-06 收盘（美东交易日）';
        const rule=document.getElementById('ruleLightTxt');rule.textContent='规则已生效';
        const font=el=>getComputedStyle(el).fontSize;
        let dca=document.querySelector('.runbar .dca-badge');
        if(!dca){dca=document.createElement('span');dca.className='dca-badge';document.querySelector('.runbar').appendChild(dca);}
        dca.textContent='定投：9';
        const states=['本页只读','仓位已同步'].map(text=>{
          mirror.innerHTML='<span class="dim"></span>';mirror.firstElementChild.textContent=text;
          return {text,font:font(mirror.firstElementChild),mirrorFont:font(mirror),overflow:sub.scrollWidth>sub.clientWidth+1};
        });
        return {sub:font(sub),rule:font(rule),dca:font(dca),states,overflow:document.documentElement.scrollWidth>innerWidth+1};
      })()`);
      assertions.push({ok:header.sub===(width<=600?'14.5px':'13.5px') && header.sub===header.rule
        && header.states.every(s=>s.font===header.rule && s.mirrorFont===header.rule && !s.overflow)
        && header.dca===(width<=600?'18px':'16px') && !header.overflow,
        name:width+'px数据时间/只读/同步与规则同字号、定投加2px且无横向溢出',actual:header});
      const before = await evaluate(`(() => {
        const link=document.querySelector('a.signal-member-jump[href="#s-thigh"]');
        if(!link) throw Error('missing synthetic stock jump');
        link.click();
        document.querySelectorAll('#group_positions_long,#group_positions_short').forEach(card=>card.open=true);
        document.getElementById('group_positions').open=false;
        const u=new URL(location.href);u.searchParams.set('focus','pos');u.searchParams.set('edit','1');
        history.replaceState(null,'',u.href);
        return {hash:location.hash,open:document.getElementById('s-thigh').closest('details.group-card').open};
      })()`);
      loaded=waitLoad();await evaluate(`document.getElementById('btnHardRefresh').click()`);await loaded;
      await evaluate(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
      const after=await evaluate(`(() => {
        const u=new URL(location.href);
        return {hash:u.hash,focus:u.searchParams.has('focus'),edit:u.searchParams.has('edit'),
          marker:u.searchParams.has('_mm_reset_view'),keep:u.searchParams.get('keep'),token:u.searchParams.get('token'),
          zones:[...document.querySelectorAll('#group_positions_long,#group_positions_short')].map(card=>card.open),
          open:[...document.querySelectorAll('details.card,details.cost-manage')].filter(d=>d.open&&getComputedStyle(d).display!=='none').map(d=>d.id),
          editorHidden:getComputedStyle(document.getElementById('costManage')).display==='none',
          y:scrollY,restoration:history.scrollRestoration};
      })()`);
      assertions.push({ok:before.hash==='#s-thigh' && before.open && after.hash==='' && !after.focus && !after.edit
        && !after.marker && after.keep==='1' && after.token==='synthetic'
        && JSON.stringify(after.open)===JSON.stringify(['signalCounts','group_positions'])
        && after.zones.length>0 && after.zones.every(open=>!open) && after.editorHidden && after.y===0,
        name:width+'px真实刷新清旧定位、统计与持仓父组展开/子区折叠及回顶部',actual:{before,after}});
      const jumpAfter=await evaluate(`(() => {
        document.querySelector('a.signal-member-jump[href="#s-thigh"]').click();
        window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
        return new Promise(resolve=>requestAnimationFrame(()=>resolve({hash:location.hash,
          open:document.getElementById('s-thigh').closest('details.group-card').open})));
      })()`);
      assertions.push({ok:jumpAfter.hash==='#s-thigh' && jumpAfter.open,
        name:width+'px刷新后普通股票跳转仍展开、后续pageshow不反复全页复位',actual:jumpAfter});
    }
    refreshDocument=null;
    await cdp('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
    const colorTree=await cdp('Page.getFrameTree');
    stage('颜色与标题交互');
    const colorHTML=syntheticPage(input.colorPage);
    await cdp('Page.setDocumentContent', {frameId: colorTree.frameTree.frame.id, html: colorHTML});
    for (const [width, height] of VIEWPORTS) {
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      const colors=await evaluate(`(() => {
        const rows=[...document.querySelectorAll('tr[id^="s-color"],#s-orderescape')];
        const expected={red:'rgb(255, 133, 128)',yellow:'rgb(240, 198, 116)',
          gray:'rgb(230, 232, 236)','':'rgb(139, 147, 161)'};
        return rows.map(row=>({id:row.id,items:[...row.querySelectorAll('.signal-item:not(.cost-warn)')]
          .map(el=>({kind:el.dataset.signalKind,level:el.dataset.signalLevel||'',
            role:el.dataset.signalRole||'',color:getComputedStyle(el).color,
            expected:expected[el.dataset.signalLevel||'']}))}));
      })()`);
      const all=colors.flatMap(row=>row.items);
      assertions.push({ok:colors.length===3 && all.length>0 && all.every(el=>el.color===el.expected)
        && ['red','yellow','gray',''].every(level=>all.some(el=>el.level===level))
        && colors.every(row=>row.items.every(el=>el.role==='')),
        name:width+'px涨跌幅红/黄及同股白灯、仅文字均按自身等级着色（振幅已删除，不再有主次配对）',actual:colors});
      const lamps=await evaluate(`(() => {
        const levels=['red','yellow','green','gray'];
        const expected={red:'rgb(248, 81, 73)',yellow:'rgb(210, 153, 34)',
          green:'rgb(63, 185, 80)',gray:'rgb(230, 232, 236)'};
        return [...document.querySelectorAll('#signalCounts .signal-members-panel')]
          .filter(panel=>['tone-red','tone-yellow','tone-gray'].some(c=>panel.classList.contains(c)))
          .flatMap(panel=>[...panel.querySelectorAll('a.signal-member-jump')].map(link=>{
            const li=link.closest('li'),row=document.getElementById(link.getAttribute('href').slice(1));
            const level=levels.find(c=>row && row.classList.contains(c)),before=getComputedStyle(li,'::before');
            // 主表灯条 2026-11-03 起画在整行的 ::before 上（卡片版式里 td.sym 只占头部行一格）
            const rowStyle=row?getComputedStyle(row,'::before'):null;
            const rowLamp=rowStyle?rowStyle.backgroundColor:'';
            return {id:row?row.id:'',tone:['red','yellow','gray'].find(c=>panel.classList.contains('tone-'+c)),level,
              color:before.backgroundColor,expected:expected[level],width:before.width,height:before.height,
              gap:before.marginRight,content:before.content,spans:li.querySelectorAll('.signal-name-lamp').length,
              rowLamp};
          }));
      })()`);
      assertions.push({ok:lamps.length>0 && lamps.every(m=>m.color===m.expected && m.width==='3px'
          && m.height==='12px' && m.gap==='6px' && m.content==='\"\"' && m.spans===0
          && (!['red','yellow','green'].includes(m.level) || m.rowLamp.includes(m.expected)))
          && ['yellow','gray'].every(tone=>lamps.some(m=>m.tone===tone && m.level==='red')),
        name:width+'px红黄白分类不变，名单唯一3px竖条与主表灯同色，黄白名单中的红主行仍红',actual:lamps});
    }
    const filterTree=await cdp('Page.getFrameTree');
    await cdp('Page.setDocumentContent',{frameId:filterTree.frameTree.frame.id,html:syntheticPage(input.filterPage)});
    await settled();
    for(const [width,height] of VIEWPORTS){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      await evaluate(`document.getElementById('signalCounts').open=true;document.querySelectorAll('.signal-filter-toggle').forEach(b=>b.setAttribute('aria-pressed','false'));renderSignalFilter()`);
      const clicks=[];
      for(const kind of ['target_purple','earnings_week']){
        const point=await evaluate(`(()=>{
          const b=document.querySelector('.signal-filter-toggle[data-kind="${kind}"]');b.scrollIntoView({block:'center'});
          const r=b.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
          return {x,y,hit:document.elementFromPoint(x,y)===b,z:getComputedStyle(b).zIndex};
        })()`);
        await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1});
        await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x,y:point.y,button:'left',clickCount:1});
        clicks.push({kind,...point});
      }
      const selected=await evaluate(`(()=>{
        const kinds=[...document.querySelectorAll('.signal-filter-toggle[aria-pressed="true"]')].map(b=>b.dataset.kind);
        const result=intersectSignals(signalFilterData(),kinds);
        return {kinds,symbols:result.members.map(m=>m.s),text:document.getElementById('signalFilterResult').textContent,
          pnlSelectable:!!document.querySelector('.pnl-extra .signal-filter-toggle'),
          panelOpen:[...document.querySelectorAll('.purple-extra details,.earnings-extra details')].some(d=>d.open)};
      })()`);
      assertions.push({ok:clicks.every(p=>p.hit&&p.z==='1')&&JSON.stringify(selected.kinds)===JSON.stringify(['target_purple','earnings_week'])
        &&JSON.stringify(selected.symbols.sort())===JSON.stringify(['TBOTH','TSPACE'])&&!selected.pnlSelectable&&!selected.panelOpen,
        name:width+'px真实鼠标命中紫灯和财报按钮，二者参与交集且仅盈亏到价排除',actual:{clicks,selected}});
      await evaluate(`document.querySelector('.signal-filter-toggle[data-kind="ma"]').click()`);
      const triple=await evaluate(`(()=>{const kinds=[...document.querySelectorAll('.signal-filter-toggle[aria-pressed="true"]')].map(b=>b.dataset.kind);const r=intersectSignals(signalFilterData(),kinds);return {kinds,symbols:r.members.map(m=>m.s),text:document.getElementById('signalFilterResult').textContent};})()`);
      assertions.push({ok:triple.kinds.length===3&&['target_purple','earnings_week','ma'].every(k=>triple.kinds.includes(k))
        &&triple.symbols.length===2&&triple.text.includes('同时满足'),name:width+'px紫灯财报和技术条件三选交集保持',actual:triple});
    }
    const timeTree=await cdp('Page.getFrameTree');
    await cdp('Page.setDocumentContent',{frameId:timeTree.frameTree.frame.id,html:syntheticPage(input.timePage)});
    for(const [width,height] of VIEWPORTS){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      const times=await evaluate(`(async()=>{
        document.querySelectorAll('details').forEach(d=>d.open=true);await document.fonts.ready;
        const symbols=['EPREV','ENOW','ERED','EYELLOW','EFAR','ETBD'];
        const sample=(sym,where)=>{
          const el=where==='main'?document.querySelector('#s-'+sym.toLowerCase()+' .earn'):
            [...document.querySelectorAll('#signalCounts .earnings-week li')].find(li=>li.querySelector('.signal-member-symbol')?.textContent===sym)?.querySelector('.earnings-week-meta');
          if(!el)return {sym,where,missing:true};const cd=el.querySelector('.cd'),bj=el.querySelector('.unit,.earnings-week-bj');
          return {sym,where,color:getComputedStyle(el).color,cd:cd?getComputedStyle(cd).color:null,
            tbd:!!cd?.classList.contains('tbd'),bj:bj?getComputedStyle(bj).color:null,relative:!!bj?.classList.contains('is-countdown')};
        };
        const list=symbols.flatMap(s=>[sample(s,'main'),sample(s,'list')]);
        const box=document.getElementById('signalFilterResult'),data=signalFilterData();
        // 第二项为同六股合成均线条件，只验证真实交集渲染，不伪造生产行情/时间字段。
        data.ma={ok:true,label:'合成均线',members:data.earnings_week.members.map(m=>({...m,d:[{l:'yellow',t:'合成均线条件'}]}))};
        box.dataset.filter=JSON.stringify(data);
        if(!document.querySelector('.signal-filter-toggle[data-kind="ma"]')){
          const button=document.createElement('button');button.className='signal-filter-toggle';button.dataset.kind='ma';
          document.getElementById('signalCounts').append(button);
        }
        document.querySelectorAll('.signal-filter-toggle').forEach(b=>b.setAttribute('aria-pressed',['earnings_week','ma'].includes(b.dataset.kind)?'true':'false'));
        renderSignalFilter();
        const filtered=[...box.querySelectorAll('[data-signal-kind="earnings_week"]')].map(e=>{
          const sym=e.closest('li').querySelector('.signal-member-symbol').textContent,cd=e.querySelector('.cd'),bj=e.querySelector('.earnings-week-bj');
          return {sym,where:'filter',color:getComputedStyle(e).color,cd:cd?getComputedStyle(cd).color:null,
            tbd:!!cd?.classList.contains('tbd'),bj:bj?getComputedStyle(bj).color:null,relative:!!bj?.classList.contains('is-countdown')};
        });return {list,filtered};
      })()`);
      const expected={EPREV:'rgb(255, 133, 128)',ENOW:'rgb(255, 133, 128)',ERED:'rgb(255, 133, 128)',
        EYELLOW:'rgb(240, 198, 116)',EFAR:'rgb(143, 184, 240)',ETBD:'rgb(143, 184, 240)'};
      assertions.push({ok:times.list.length===12&&times.filtered.length===6&&times.list.concat(times.filtered).every(t=>
        !t.missing&&t.color===expected[t.sym]&&t.cd===(t.tbd?'rgb(143, 184, 240)':'rgb(255, 255, 255)')
        &&(!t.relative||t.bj==='rgb(255, 255, 255)')
        &&(t.where==='main'||t.relative||t.bj===(['EFAR','ETBD'].includes(t.sym)?'rgb(139, 147, 161)':expected[t.sym]))),
        name:width+'px主表、财报名单及多选交集保留红黄日期，无警ET蓝/BJ原色，北京倒计时白且ET待定蓝',actual:times});
    }
    const rulesTree=await cdp('Page.getFrameTree');
    await cdp('Page.setDocumentContent',{frameId:rulesTree.frameTree.frame.id,html:syntheticPage(input.rulesPage)});
    for(const [width,height] of VIEWPORTS){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      const rules=await evaluate(`(async()=>{
        document.querySelectorAll('#rulesHost details').forEach(d=>d.open=true);await document.fonts.ready;
        const base=getComputedStyle(document.querySelector('#marketRiskRules p')).fontSize;
        const text=[...document.querySelectorAll('#signalRules .rules-body p,#signalRules .rules-body h4,#signalRules .rules-body h5,#glossaryRules .rules-body p,#glossaryRules .rules-body h4,#glossaryRules .rules-body h5,#rulesHost .rule-table th,#rulesHost .rule-table td')].map(e=>getComputedStyle(e).fontSize);
        const table=document.querySelector('#rules-lamp-table'),s=getComputedStyle(table);
        const cell=table.querySelector('td'),cs=getComputedStyle(cell),cat=getComputedStyle(table.querySelector('.cat-col'));
        return {base,text,width:s.width,cellPadding:cs.padding,lineHeight:cs.lineHeight,catWidth:cat.width,
          overflow:document.documentElement.scrollWidth>innerWidth};
      })()`);
      assertions.push({ok:rules.base==='15px'&&rules.text.length>20&&rules.text.every(f=>f===rules.base)
        &&rules.cellPadding===(width<=600?'6px 4px':'6px 7px')&&rules.lineHeight==='19.8px'&&!rules.overflow,
        name:width+'px实际设置页规则字号对齐宏观15px，保留表格padding与原行距且无横向溢出',actual:rules});
    }
    const rangeTree=await cdp('Page.getFrameTree');
    stage('52周条几何与交互');
    const rangeHTML=syntheticPage(input.rangePage);
    await cdp('Page.setDocumentContent', {frameId: rangeTree.frameTree.frame.id, html: rangeHTML});
    for (const [width, height] of VIEWPORTS) {
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      await evaluate(`document.querySelectorAll('details.group-card,details.zone-card,details.quiet-list').forEach(el=>el.open=true)`);
      await settled();
      const bars=await evaluate(`(() => {
        const rows=[...document.querySelectorAll('tr[id^="s-range"]')];
        return rows.map(row=>{
          const rail=row.nextElementSibling?row.nextElementSibling.querySelector('.range52-rail'):null;
          if(!rail)return {id:row.id,missing:true};
          const track=rail.querySelector('.range52-track').getBoundingClientRect();
          const lo=rail.querySelector('.range52-lo').getBoundingClientRect();
          const hi=rail.querySelector('.range52-hi').getBoundingClientRect();
          const spans=[...rail.querySelectorAll('.range52-gap')];
          const gaps=spans.map(g=>g.getBoundingClientRect());
          const marker=rail.querySelector('.range52-marker').getBoundingClientRect();
          const rb=rail.getBoundingClientRect();
          const insideRail=g=>g.left>=rb.left-0.5&&g.right<=rb.right+0.5;
          return {id:row.id,text:spans.map(g=>g.textContent).join(' / '),
            height:rail.getBoundingClientRect().height,
            percent:rail.querySelectorAll('.range52-percent').length,gaps:gaps.length,
            // 两端第二行必须贴在对应端点价格的正下方，且不越出整条 52 周条
            underLo:gaps[0]?gaps[0].top>=lo.bottom-0.5&&gaps[0].top<=rb.bottom+0.5&&insideRail(gaps[0]):false,
            underHi:gaps[1]?gaps[1].top>=hi.bottom-0.5&&gaps[1].top<=rb.bottom+0.5&&insideRail(gaps[1]):false,
            // 三角上方不再有百分比；第二行不能压到灰轨
            overlapsTrack:gaps.some(g=>!(g.bottom<=track.top+0.5||g.top>=track.bottom-0.5)),
            markerWidth:+(marker.width).toFixed(1),markerHeight:+(marker.height).toFixed(1),
            // 现价三角底宽 8.4px，贴端点时允许露出半边（4.2px）
            markerInTrack:marker.left>=track.left-5&&marker.right<=track.right+5};
        });
      })()`);
      // 终稿编辑入口仍位于刷新右侧；资产金额移到真实cashbond行，成本卡仅作为隐藏编辑器。
      // 验证按钮几何、资产金额同源及总额/Cash/固收顺序；五列交易只形成草稿，不恢复红叉/市值列。
      const edit=await evaluate(`(() => {
        const card=document.getElementById('costManage'),btn=document.getElementById('costEditBtn');
        const cashbond=document.querySelector('#group_positions>.cashbond-row');
        const asset=document.querySelector('#group_positions>.asset-row');
        if(!card||!btn||!cashbond||!asset)return {exists:false};
        const bar=document.querySelector('.runbar'),refresh=document.getElementById('btnHardRefresh');
        const b=btn.getBoundingClientRect(),rb=refresh&&refresh.getBoundingClientRect();
        const cb=cashbond.getBoundingClientRect();
        const sum=cashbond.querySelector('.cb-total'),sb=sum.getBoundingClientRect();
        const members=[...cashbond.children].map(e=>e.getBoundingClientRect());
        const memberGaps=members.slice(1).map((r,i)=>+(r.left-members[i].right).toFixed(1));
        return {exists:true,
          inRunbar:!!(bar&&bar.contains(btn)),
          afterRefresh:!!(refresh&&(refresh.compareDocumentPosition(btn)&Node.DOCUMENT_POSITION_FOLLOWING)),
          sameRowAsRefresh:!!rb&&Math.abs((b.top+b.bottom)/2-(rb.top+rb.bottom)/2)<=2,
          gapToRefresh:rb?+(b.left-rb.right).toFixed(1):null,
          height:+b.height.toFixed(1),
          headH:+cb.height.toFixed(1),
          sumSameRow:members.every(r=>Math.abs((r.top+r.bottom)/2-(sb.top+sb.bottom)/2)<=2),
          sumLeftGap:+(sb.left-cb.left).toFixed(1),cashPaddingLeft:parseFloat(getComputedStyle(cashbond).paddingLeft),memberGaps,
          order:[...cashbond.children].map(e=>e.className),
          total:cashbond.querySelector('.cb-total-amt').textContent,
          editorHidden:getComputedStyle(card).display==='none',
          assetVisible:asset.getBoundingClientRect().height>0,
          fontSize:getComputedStyle(btn).fontSize,radius:getComputedStyle(btn).borderTopLeftRadius,
          overflow:document.documentElement.scrollWidth>innerWidth+1};
      })()`);
      assertions.push({ok:bars.length===2 && bars.every(b=>!b.missing && b.percent===0 && b.gaps===2
          && b.underLo && b.underHi && !b.overlapsTrack && b.markerInTrack && b.markerWidth<=8.6
          // 只收底宽、高度不变（9px）→ 顶角约 50°，比原来的 60° 更尖锐
          && b.markerHeight>=8.6 && Math.round(b.height)===26)   // 三角之上不再写百分比，整条从 33px 回收到 26px
          && bars.some(b=>b.text==='-33.3% / +33.3%') && bars.some(b=>b.text==='-39.0% / +0.6%')
          && edit.exists && edit.inRunbar && edit.afterRefresh && edit.sameRowAsRefresh
          && edit.gapToRefresh>=8 && edit.gapToRefresh<=12 && edit.height>=24 && !edit.overflow
          && edit.radius==='7px' && edit.fontSize===(width<=600?'16px':'13px')
          // 总额(含盈亏)、现金、固收三组铺满整行、组间距相等（space-between），不再是固定12px间隔。
          && edit.sumSameRow && edit.headH<=56 && Math.abs(edit.sumLeftGap-edit.cashPaddingLeft)<=0.5
          && edit.memberGaps.length===2 && edit.memberGaps[0]>=12 && Math.abs(edit.memberGaps[0]-edit.memberGaps[1])<=1.5
          && JSON.stringify(edit.order)===JSON.stringify(['cb-total','cb-item cb-cash','cb-item cb-bond'])
          && edit.total.replace(/[^0-9]/g,'')==='4940' && edit.editorHidden && edit.assetVisible,
        name:width+'px52周条几何和数值不变；顶栏编辑按钮、真实资产4940及总额/现金/固收顺序',actual:{bars,edit}});
      const editor=await evaluate(`(() => {
        document.getElementById('costEditBtn').click();
        const panel=document.getElementById('costManage'),grid=panel.querySelector('.ep-grid');
        const row=grid.querySelector('[data-cost-row][data-sym="RANGENORM"]');
        const result={visible:getComputedStyle(panel).display!=='none',columns:getComputedStyle(row).gridTemplateColumns.split(/\\s+/).length,
          headings:[...grid.querySelectorAll('.ep-editor-head>span')].map(e=>e.textContent),
          order:[...row.children].map(e=>e.dataset.tradeAction||e.dataset.f||'code'),
          symbols:[...grid.querySelectorAll('[data-cost-row][data-sym]')].map(e=>e.dataset.sym).sort(),
          cost:panel.querySelector('input[data-sym="RANGENORM"][data-f="cost"]').value,
          qty:panel.querySelector('input[data-sym="RANGEHI"][data-f="qty"]').value,
          retiredVisible:[...grid.querySelectorAll('[data-cost-clear],.ep-val,.ep-clear')].filter(e=>e.getBoundingClientRect().height>0).length};
        const sell=row.querySelector('[data-trade-action="sell"]');
        result.sellColor=getComputedStyle(sell).color;result.sellText=sell.textContent;
        const cr=sell.getBoundingClientRect();result.hitWidth=cr.width;result.hitHeight=cr.height;
        result.fixedFields=panel.querySelectorAll('input[data-sym="BOND"]').length;
        result.fixedCost=!!panel.querySelector('input[data-sym="BOND"][data-f="fixed_cost"]');
        result.editorFont=getComputedStyle(row.querySelector('.ep-code')).fontSize;
        result.inputFont=getComputedStyle(panel.querySelector('input[data-f="cost"]')).fontSize;
        result.noOverflow=document.documentElement.scrollWidth<=innerWidth;
        const add=panel.querySelector('[data-cost-add]'),cancel=document.getElementById('costCancel'),save=document.getElementById('costSave');
        const ar=add.getBoundingClientRect(),xr=cancel.getBoundingClientRect(),sr=save.getBoundingClientRect();
        result.addInActs=add.parentElement.id==='costActs'&&panel.querySelectorAll('[data-cost-add]').length===1;
        result.actsSameRow=Math.abs(ar.top-xr.top)<1&&Math.abs(xr.top-sr.top)<1&&ar.right<xr.left&&xr.right<sr.left;
        sell.click();const dialog=panel.querySelector('.cost-trade-dialog');
        result.dialogOpen=dialog.open;result.destinations=[...dialog.querySelectorAll('input[name="tradeDestination"]')].map(i=>i.value);
        result.dialogWidth=dialog.getBoundingClientRect().width;result.hasCash=!!dialog.querySelector('[data-f="cash"]');
        result.sellNote=dialog.querySelector('[data-trade-note]').textContent;
        result.dialogFont=getComputedStyle(dialog.querySelector('h3')).fontSize;
        result.labelFont=getComputedStyle(dialog.querySelector('label')).fontSize;
        const choices=[...dialog.querySelectorAll('.cost-clear-choice label')].map(e=>e.getBoundingClientRect());
        result.choicesSameRow=choices.length===2&&Math.abs(choices[0].top-choices[1].top)<1&&choices[0].right<choices[1].left;
        dialog.querySelector('[data-trade-cancel]').click();result.dialogClosed=!dialog.open;
        document.getElementById('costCancel').click();result.cancelHidden=getComputedStyle(panel).display==='none';
        return result;
      })()`);
      assertions.push({ok:editor.visible&&editor.columns===5&&editor.cancelHidden
        &&JSON.stringify(editor.headings)===JSON.stringify(['代码','买入','卖出','成本价','数量'])
        &&JSON.stringify(editor.order)===JSON.stringify(['code','buy','sell','cost','qty'])
        && JSON.stringify(editor.symbols)===JSON.stringify(['RANGEHI','RANGENORM'])&&editor.cost==='125'&&editor.qty==='5'
        &&editor.retiredVisible===0&&editor.sellColor==='rgb(255, 133, 128)'&&editor.sellText==='SELL'
        &&editor.dialogOpen&&editor.dialogClosed&&JSON.stringify(editor.destinations)===JSON.stringify(['default','focus'])
        &&editor.dialogWidth>0&&editor.dialogWidth<=width-24&&!editor.hasCash
        &&editor.fixedFields===1&&editor.fixedCost&&editor.editorFont==='16px'&&editor.inputFont==='16px'
        &&editor.noOverflow&&editor.sellNote.includes('卖出价必填')&&editor.dialogFont==='18px'&&editor.labelFont==='16px'
        &&editor.addInActs&&editor.actsSameRow&&editor.choicesSameRow&&editor.hitWidth===58&&editor.hitHeight===34,
        name:width+'px真实五列代码/BUY/SELL/成本/数量、退休红叉市值列不显示及取消不变',actual:editor});
      const clearPoint=await evaluate(`(()=>{
        document.getElementById('costEditBtn').click();
        const b=document.querySelector('[data-trade-action="sell"][data-sym="RANGENORM"]');b.scrollIntoView({block:'center'});
        const r=b.getBoundingClientRect(),x=r.left+3,y=r.top+3;
        const next=document.querySelector('input[data-sym="RANGENORM"][data-f="cost"]').getBoundingClientRect();
        return {x,y,hit:document.elementFromPoint(x,y)===b,separate:r.right<=next.left+0.5};
      })()`);
      await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:clearPoint.x,y:clearPoint.y,button:'left',clickCount:1});
      await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:clearPoint.x,y:clearPoint.y,button:'left',clickCount:1});
      const clearHit=await evaluate(`(()=>{
        const d=document.querySelector('#costManage .cost-trade-dialog'),open=!!d&&d.open;
        if(open)d.querySelector('[data-trade-cancel]').click();document.getElementById('costCancel').click();return open;
      })()`);
      assertions.push({ok:clearPoint.hit&&clearPoint.separate&&clearHit,
        name:width+'pxSELL按钮边缘真实点击打开弹窗且不覆盖相邻成本输入',actual:{...clearPoint,open:clearHit}});
      const clearDraft=await evaluate(`(async()=>{
        document.getElementById('costEditBtn').click();
        const panel=document.getElementById('costManage');
        const field=(sym,f)=>panel.querySelector('input[data-sym="'+sym+'"][data-f="'+f+'"]');
        const original={cost:field('RANGEHI','cost').value,qty:field('RANGENORM','qty').value};
        const choose=(sym,value)=>{
          const quantity=field(sym,'qty').value;
          panel.querySelector('[data-trade-action="sell"][data-sym="'+sym+'"]').click();
          const d=panel.querySelector('.cost-trade-dialog');
          d.querySelector('[data-trade-qty]').value=quantity;d.querySelector('[data-trade-price]').value='150';
          d.querySelector('input[value="'+value+'"]').checked=true;
          d.querySelector('[data-trade-confirm]').click();
          return !d.open;
        };
        const firstClosed=choose('RANGENORM','focus');
        const input=field('RANGEHI','cost');input.value='123';input.dispatchEvent(new Event('input',{bubbles:true}));
        await new Promise(r=>requestAnimationFrame(r));
        const editable=field('RANGEHI','cost').value==='123'&&!field('RANGEHI','cost').disabled;
        const secondClosed=choose('RANGEHI','default');
        panel.querySelector('[data-trade-action="sell"][data-sym="RANGENORM"]').click();
        const dialog=panel.querySelector('.cost-trade-dialog');
        const retained=dialog.querySelector('input[value="focus"]').checked;
        const deferred=field('RANGENORM','qty').value==='0'&&field('RANGENORM','cost').value==='0'
          &&document.getElementById('s-rangenorm').closest('#group_positions')!==null;
        dialog.querySelector('[data-trade-cancel]').click();
        document.getElementById('costCancel').click();
        document.getElementById('costEditBtn').click();
        const restored=field('RANGEHI','cost').value===original.cost&&field('RANGENORM','qty').value===original.qty;
        panel.querySelector('[data-trade-action="sell"][data-sym="RANGENORM"]').click();
        const cancelled=dialog.querySelector('input[value="default"]').checked;
        dialog.querySelector('[data-trade-cancel]').click();document.getElementById('costCancel').click();
        return {firstClosed,secondClosed,editable,retained,deferred,restored,cancelled};
      })()`);
      assertions.push({ok:Object.values(clearDraft).every(Boolean),
        name:width+'px全卖沿清仓草稿归零不提前移组、允许继续输入及多股标记、取消统一撤销',actual:clearDraft});
      const addDraft=await evaluate(`(async()=>{
        document.getElementById('costEditBtn').click();const panel=document.getElementById('costManage');
        const add=panel.querySelector('[data-cost-add]');add.click();const d=panel.querySelector('.cost-add-dialog');
        const initial=d.open&&[...d.querySelectorAll('p')].every(p=>p.hidden||!p.textContent.trim())&&!d.querySelector('[data-f="cash"]');
        const widthOK=d.getBoundingClientRect().width<=innerWidth-24;
        const holdRects=[...d.querySelectorAll('[data-add-hold]')].map(e=>e.closest('label').getBoundingClientRect());
        const holdSameRow=holdRects.length===2&&Math.abs(holdRects[0].top-holdRects[1].top)<1&&holdRects[0].right<=holdRects[1].left;
        const enlarged=getComputedStyle(d.querySelector('h3')).fontSize==='18px'
          &&getComputedStyle(d.querySelector('label')).fontSize==='16px'
          &&getComputedStyle(d.querySelector('[data-add-symbol]')).fontSize==='16px';
        d.querySelector('[data-add-symbol]').value=' msft ';
        d.querySelector('[data-add-hold][value="short"]').checked=true;
        d.querySelector('[data-add-qty]').value='2';d.querySelector('[data-add-cost]').value='100';
        d.querySelector('[data-add-confirm]').click();await new Promise(r=>requestAnimationFrame(r));
        const row=()=>panel.querySelector('[data-cost-row][data-sym="MSFT"]');
        const added=!d.open&&!!row()&&row().textContent.includes('MSFT')&&row().querySelector('[data-trade-action="buy"]')!==null;
        const qty=panel.querySelector('input[data-sym="MSFT"][data-f="qty"]');
        const editable=!!qty&&!qty.disabled&&qty.value==='2';
        const noOverflow=document.documentElement.scrollWidth<=innerWidth;
        const unknownValue=!!qty&&row().querySelector('.ep-val')===null;
        row().querySelector('[data-trade-action="sell"]').click();const trade=panel.querySelector('.cost-trade-dialog');
        trade.querySelector('[data-trade-qty]').value='2';trade.querySelector('[data-trade-price]').value='100';
        trade.querySelector('[data-trade-confirm]').click();await new Promise(r=>requestAnimationFrame(r));
        const removed=!row();panel.querySelector('[data-cost-add]').click();d.querySelector('[data-add-symbol]').value='RANGEHI';
        d.querySelector('[data-add-confirm]').click();const duplicate=d.open&&!!d.querySelector('[data-add-message]').textContent;
        d.querySelector('[data-add-symbol]').value='MSFT';d.querySelector('[data-add-confirm]').click();
        await new Promise(r=>requestAnimationFrame(r));document.getElementById('costCancel').click();document.getElementById('costEditBtn').click();
        const cancelled=!row();document.getElementById('costCancel').click();
        return {initial,widthOK,holdSameRow,enlarged,added,editable,noOverflow,unknownValue,removed,duplicate,cancelled};
      })()`);
      assertions.push({ok:Object.values(addDraft).every(Boolean),
        name:width+'px新增仓位弹窗、代码规范化、可编辑撤销、重复拦截与统一取消',actual:addDraft});
    }
    // 今日唯一终稿：独立合成交易输入，实际生产renderer/JS，不复制DEMO内存补丁到源。
    const scriptErrors=[];
    ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.method==='Runtime.exceptionThrown')scriptErrors.push(m.params.exceptionDetails);});
    const freshPage=async html=>{
      const loaded=waitLoad();await cdp('Page.navigate',{url:'about:blank'});await loaded;
      const tree=await cdp('Page.getFrameTree');scriptErrors.length=0;
      await cdp('Page.setDocumentContent',{frameId:tree.frameTree.frame.id,html:syntheticPage(html)});await settled();
    };
    stage('六档五列交易及持仓终稿');
    const productionLayouts=[];
    const readLayout=()=>evaluate(`(async()=>{
      const parent=document.getElementById('group_positions'),head=parent.querySelector(':scope>summary');
      const styles=e=>{const s=getComputedStyle(e);return Object.fromEntries(['fontSize','fontWeight','lineHeight','padding','margin','color','backgroundColor','borderWidth','borderRadius','visibility','display','direction','textAlign'].map(k=>[k,k==='textAlign'&&['start','end'].includes(s[k])?((s[k]==='start')===(s.direction==='ltr')?'left':'right'):s[k]]));};
      const rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
      const p=rect(parent),hs=styles(head),triangle=getComputedStyle(head,'::before');
      const zones=['long','short'].map(k=>{const z=document.getElementById('group_positions_'+k);return {rect:rect(z),style:styles(z),head:styles(z.querySelector('summary'))};});
      const cash=parent.querySelector('.cashbond-row'),asset=parent.querySelector(':scope>.asset-row');
      const topGroups=[...document.querySelectorAll('details.card.group-card[id^="group_"]')].filter(e=>e!==parent&&rect(e).width>0).map(e=>({id:e.id,rect:rect(e)}));
      const sameEdges=(a,b)=>Math.abs(a.left-b.left)<=0.5&&Math.abs(a.right-b.right)<=0.5;
      const cashRect=rect(cash),assetRect=rect(asset);
      const fullWidth=topGroups.length>=2&&topGroups.every(g=>[p,cashRect,assetRect,...zones.map(z=>z.rect)].every(r=>sameEdges(r,g.rect)));
      const state={parent:styles(parent),parentRect:p,head:hs,triangle:{visibility:triangle.visibility,width:triangle.width,display:triangle.display},zones,cash:styles(cash),cashRect,assetRect,topGroups,fullWidth,cashText:cash.querySelector('.cb-cash .cb-name').textContent,rs:parent.querySelector('.rs-spy')?.textContent||'',total:parent.querySelector('.pos-total')?.textContent||''};
      head.click();state.clickKeepsOpen=parent.open;
      const alpha=color=>{const parts=color.match(/[\\d.]+/g)||[];return color==='transparent'?0:parts.length===4?Number(parts[3]):parts.length===3?1:0;};
      state.collapse=[];
      for(const key of ['long','short']){
        const zone=document.getElementById('group_positions_'+key),wasOpen=zone.open;zone.open=true;
        await new Promise(r=>requestAnimationFrame(r));
        const button=zone.querySelector('.collapse-group'),s=getComputedStyle(button),b=rect(button);
        state.collapse.push({key,background:s.backgroundColor,alpha:alpha(s.backgroundColor),rect:b,visible:b.width>0&&b.height===26&&s.visibility==='visible'&&Number(s.opacity)>0});
        zone.open=wasOpen;
      }
      await new Promise(r=>requestAnimationFrame(r));
      const day=document.getElementById('dayPnlCard');day.open=true;await new Promise(r=>requestAnimationFrame(r));
      const dayRoot=document.querySelector('#dualRow>.dual-body>ul.cost-contrib[data-owner="day"]');
      const dayRow=dayRoot?.querySelector('li'),dayLink=dayRow?.querySelector('.signal-member-jump');
      state.day={amount:styles(day.querySelector('.dp-amt')),percent:styles(day.querySelector('.dp-pct')),
        code:dayRow?styles(dayRow.querySelector('.cnsym')):null,name:dayRow?styles(dayRow.querySelector('.cncn')):null,
        pnl:dayRow?styles(dayRow.querySelector('.ca')):null,ratio:dayRow?styles(dayRow.querySelector('.cv')):null,
        link:dayLink?styles(dayLink):null,bar:dayRow?styles(dayRow.querySelector('.cbar')):null,
        linkStatic:dayLink?getComputedStyle(dayLink).borderBottomStyle:null,
        barFits:dayRoot?[...dayRoot.querySelectorAll('.cbar')].every(b=>rect(b).right<=rect(b.closest('li')).right+0.5):false};
      state.day.links=dayRoot?[...dayRoot.querySelectorAll('.cn-name .signal-member-jump')].map(link=>{
        const s=getComputedStyle(link),b=rect(link),nameBox=link.closest('.cn-name'),clips=[];
        for(let node=nameBox;node&&node!==document.body;node=node.parentElement){
          const style=getComputedStyle(node),r=rect(node);
          if(node===nameBox||style.overflowX!=='visible'||style.overflowY!=='visible')clips.push({tag:node.className,left:r.left+node.clientLeft,right:r.left+node.clientLeft+node.clientWidth,top:r.top+node.clientTop,bottom:r.top+node.clientTop+node.clientHeight});
        }
        const contains=(r,clip)=>r.left>=clip.left-0.5&&r.right<=clip.right+0.5&&r.top>=clip.top-0.5&&r.bottom<=clip.bottom+0.5;
        const border={left:b.left,right:b.right,top:b.bottom-parseFloat(s.borderBottomWidth),bottom:b.bottom};
        const name=link.querySelector('.cncn'),nameRange=document.createRange();if(name)nameRange.selectNodeContents(name);
        const nr=name?nameRange.getBoundingClientRect():null;
        return {href:link.getAttribute('href'),text:link.textContent,rect:b,border,clips,borderWidth:parseFloat(s.borderBottomWidth),borderStyle:s.borderBottomStyle,borderColor:s.borderBottomColor,
          borderFits:clips.every(c=>contains(border,c)),nameText:name?.textContent||'',nameRect:nr?{left:nr.left,right:nr.right,top:nr.top,bottom:nr.bottom,width:nr.width,height:nr.height}:null,
          nameVisible:!!nr&&nr.width>0&&nr.height>0&&getComputedStyle(name).visibility==='visible'
            // 文字范围是字体内容区（ascent+descent，比字形墨迹高约3px），竖向允许1.5px浮动；左右仍须完全在裁切盒内。
            &&clips.every(c=>nr.left>=c.left-0.5&&nr.right<=c.right+0.5&&nr.top>=c.top-1.5&&nr.bottom<=c.bottom+1.5),
          visible:b.width>0&&b.height>0&&s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>0&&alpha(s.borderBottomColor)>0};
      }):[];
      state.day.allLinksVisible=state.day.links.length>=2&&state.day.links.length===dayRoot.querySelectorAll('li').length
        &&state.day.links.every(l=>l.visible&&Number.isFinite(l.borderWidth)&&l.borderWidth>0&&l.borderStyle==='dotted'&&l.borderFits&&l.nameVisible);
      const editButton=document.getElementById('costEditBtn');
      if(editButton.getAttribute('aria-pressed')!=='true')editButton.click();
      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      const panel=document.getElementById('costManage'),row=panel.querySelector('.ep-editor-row[data-sym="GOOGL"],.demo-editor-row[data-sym="GOOGL"]');
      if(!row)throw new Error('GOOGL五列缺失：'+JSON.stringify({pressed:editButton.getAttribute('aria-pressed'),rows:[...panel.querySelectorAll('[data-sym]')].map(e=>({tag:e.tagName,cls:e.className,sym:e.dataset.sym}))}));
      const qty=row.querySelector('input[data-f="qty"]');
      const code=row.querySelector('.ep-code,.demo-editor-code'),grid=panel.querySelector('.ep-editor-head,.demo-editor-head');
      const extra=panel.querySelector('input[data-f="cash_extra"]');
      const cashLabel=extra.closest('label'),cashTotal=panel.querySelector('.ep-cash-total'),bond=panel.querySelector('input[data-f="fixed_cost"]').closest('label');
      qty.value='9999.9999';const q=rect(qty),c=rect(code),e=rect(extra),b=rect(bond),t=rect(cashTotal),rr=rect(row);
      const s=getComputedStyle(qty),canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');ctx.font=s.font;
      const codeRange=document.createRange();codeRange.selectNodeContents(code);
      const ink=codeRange.getBoundingClientRect(),buy=row.querySelector('[data-trade-action="buy"]');
      const codeStyle=getComputedStyle(code),codeVisible=ink.right<=c.right+1||codeStyle.overflowX==='visible';
      const codeFits=codeVisible&&ink.width>0&&ink.height>0&&codeStyle.visibility==='visible'
        &&codeStyle.display!=='none'&&ink.left>=rr.left&&ink.top>=rr.top&&ink.bottom<=rr.bottom
        &&ink.right<=rect(buy).left-1&&rect(buy).right<=rect(panel).right
        &&rr.right<=rect(panel).right&&codeStyle.textOverflow!=='ellipsis';
      state.editor={panel:styles(panel),row:styles(row),head:styles(grid),headingStyles:[...grid.children].map(styles),code:styles(code),quantity:styles(qty),cashExtra:styles(extra),
        columns:getComputedStyle(row).gridTemplateColumns.split(/\\s+/),codeText:code.textContent,codeFits,
        codeInkWidth:ink.width,codeToBuyGap:rect(buy).left-ink.right,codeOverflow:codeStyle.overflowX,
        codeFontFamily:codeStyle.fontFamily,codeScrollWidth:code.scrollWidth,codeClientWidth:code.clientWidth,
        rightAligned:Math.abs(q.right-e.right)<1,quantityTextFits:ctx.measureText(qty.value).width<=q.width-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight)-2,
        buttonsSameRow:[...row.querySelectorAll('[data-trade-action]')].every(x=>Math.abs(rect(x).top-(rr.top+rr.bottom-rect(x).height)/2)<=1),
        bondTotalSameRow:Math.abs((b.top+b.bottom)-(t.top+t.bottom))<=2,
        codeQuantityEdges:[c.left,q.right],quantityWidth:q.width,cashExtraWidth:e.width,fieldsSameRow:Math.abs(rect(cashLabel).top-rect(panel.querySelector('input[data-f="cash"]').closest('label')).top)<1};
      document.getElementById('costCancel').click();return state;
    })()`);
    for(const [width,height] of VIEWPORTS){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await freshPage(input.tradePage);
      const layout=await readLayout();productionLayouts.push({width,layout});
      const zoneWidth=layout.zones[0].rect.width;
      assertions.push({ok:layout.clickKeepsOpen&&layout.parent.borderWidth==='0px'&&layout.parent.backgroundColor==='rgb(23, 26, 33)'
        &&layout.triangle.visibility==='hidden'&&layout.triangle.width==='10px'&&layout.triangle.display!=='none'
        &&layout.fullWidth&&Math.abs(zoneWidth-layout.zones[1].rect.width)<0.1&&layout.zones.every(z=>z.rect.left>=layout.parentRect.left&&z.rect.right<=layout.parentRect.right
          &&parseFloat(z.style.borderWidth)>0&&parseFloat(z.style.borderRadius)>0)
        &&layout.collapse.length===2&&layout.collapse.every(c=>c.visible&&c.alpha>0)
        &&layout.cashText==='Cash'&&layout.rs===''&&/^\$[\d,]+$/.test(layout.total)&&layout.editor.columns.length===5
        &&layout.editor.headingStyles.length===5&&layout.editor.headingStyles.every(s=>s.fontSize==='13px'&&s.lineHeight==='19.5px'&&s.color==='rgb(139, 147, 161)')
        &&layout.editor.codeText==='GOOGL'&&layout.editor.codeFits&&layout.editor.rightAligned&&layout.editor.quantityTextFits
        &&layout.editor.buttonsSameRow&&layout.editor.bondTotalSameRow&&layout.editor.quantity.textAlign==='right'&&layout.editor.cashExtra.textAlign==='right',
        name:width+'px持仓/资产/现金/长短外缘与其它顶层分组同全宽，父不可折叠、子▲有底色及交易容量',actual:layout});
      assertions.push({ok:layout.day.amount.fontSize==='16px'&&layout.day.percent.fontSize==='16px'&&layout.day.amount.fontWeight==='400'&&layout.day.percent.fontWeight==='400'
        &&layout.day.code?.fontSize==='16px'&&layout.day.name?.fontSize==='15px'&&layout.day.pnl?.fontSize==='15px'
        &&layout.day.ratio?.fontSize==='15px'&&layout.day.ratio.textAlign==='center'&&layout.day.linkStatic==='dotted'&&layout.day.barFits
        &&layout.day.allLinksVisible,
        name:width+'px当日每股名称可见及跳转边框完整位于cn-name与祖先裁切盒内，标题16不加粗/列表16/15和比例条不越界',actual:layout.day});
      const trade=await evaluate(`(async()=>{
        const panel=document.getElementById('costManage'),edit=()=>document.getElementById('costEditBtn').click(),cancel=()=>document.getElementById('costCancel').click();
        const field=f=>panel.querySelector('input[data-sym="GOOGL"][data-f="'+f+'"]');
        const values=()=>({cost:field('cost').value,qty:field('qty').value,cash:panel.querySelector('input[data-f="cash"]').value,extra:panel.querySelector('input[data-f="cash_extra"]').value});
        const open=side=>{panel.querySelector('[data-trade-action="'+side+'"][data-sym="GOOGL"]').click();return panel.querySelector('.cost-trade-dialog');};
        const submit=(side,q,p)=>{const d=open(side);d.querySelector('[data-trade-qty]').value=q;d.querySelector('[data-trade-price]').value=p;d.querySelector('[data-trade-confirm]').click();return d;};
        edit();const original=values();let d=open('buy');const buyOpen=d.open;d.querySelector('[data-trade-qty]').value='3';d.querySelector('[data-trade-price]').value='200';d.querySelector('[data-trade-cancel]').click();
        const cancelled=JSON.stringify(original)===JSON.stringify(values());
        d=submit('buy','10','');const split=!d.open&&field('qty').value==='20'&&field('cost').value==='50';
        d=submit('sell','5','100');const sold=!d.open&&field('qty').value==='15'&&field('cost').value==='33.3333';
        let c=field('cost');c.value='77';c.dispatchEvent(new Event('input',{bubbles:true}));
        d=submit('buy','5','100');const manualBeforeTrade=!d.open&&field('qty').value==='20'&&field('cost').value==='82.75';
        c=field('cost');c.value='88';c.dispatchEvent(new Event('input',{bubbles:true}));await new Promise(r=>requestAnimationFrame(r));const manualLast=field('cost').value==='88';
        const beforeSell=values();d=submit('sell','21','100');const oversell=d.open&&d.querySelector('[data-trade-error]').textContent.includes('超过')&&JSON.stringify(beforeSell)===JSON.stringify(values());d.querySelector('[data-trade-cancel]').click();
        d=submit('sell','1','');const sellPriceRequired=d.open&&d.querySelector('[data-trade-error]').textContent.includes('成交价');d.querySelector('[data-trade-cancel]').click();
        d=submit('sell','20','100');const fullSell=!d.open&&field('qty').value==='0'&&field('cost').value==='0'&&document.getElementById('s-googl').closest('#group_positions')!==null;
        const unchangedCash=values().cash===original.cash&&values().extra===original.extra;
        cancel();edit();const restored=JSON.stringify(values())===JSON.stringify(original);
        d=submit('buy','2','200');document.getElementById('costSave').click();await new Promise(r=>requestAnimationFrame(r));
        const saved=JSON.parse(sessionStorage.getItem('mm_position_costs_preview_v2'));
        const isolatedSave=!!saved&&saved.items.GOOGL.qty===12&&Math.abs(saved.items.GOOGL.cost-(10*100+2*200)/12)<1e-10&&saved.cash===300&&saved.cash_extra===100
          &&localStorage.getItem('mm_position_costs_local_v1')===null&&localStorage.getItem('mm_gh_token_v1')===null;
        return {buyOpen,cancelled,split,sold,manualBeforeTrade,manualLast,oversell,sellPriceRequired,fullSell,unchangedCash,restored,isolatedSave,saved};
      })()`);
      assertions.push({ok:['buyOpen','cancelled','split','sold','manualBeforeTrade','manualLast','oversell','sellPriceRequired','fullSell','unchangedCash','restored','isolatedSave'].every(k=>trade[k])&&scriptErrors.length===0,
        name:width+'pxBUY/SELL真实交互顺序、拆股/净投入成本/末次手填、超卖拒绝、全卖归零取消、隔离保存无云写',actual:{trade,scriptErrors:scriptErrors.slice()}});
    }
    // 600/601实际生产断点额外检查；不改变固定六档，也不把参考700断点静默改写。
    for(const width of [600,601]){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height:956,deviceScaleFactor:1,mobile:false});await freshPage(input.tradePage);
      const layout=await readLayout();
      assertions.push({ok:JSON.stringify(layout.editor.columns)===JSON.stringify(width===600?['54px','58px','58px','92px','88px']:['58px','58px','58px','112px','90px'])
        &&layout.editor.rightAligned&&layout.editor.quantityTextFits&&layout.editor.codeFits&&layout.editor.buttonsSameRow&&layout.editor.bondTotalSameRow,
        name:width+'px五列手机桌面断点、GOOGL/9999.9999完整及现金2右齐',actual:layout.editor});
    }
    // 设置页实际render输出：每股一排、列标题与各列对齐、全部字段/分隔线；价格容器全宽度display:contents。
    for(const width of [440,600,601]){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height:956,deviceScaleFactor:1,mobile:false});
      const loaded=waitLoad();await cdp('Page.navigate',{url:'about:blank'});await loaded;
      const tree=await cdp('Page.getFrameTree');
      await cdp('Page.setDocumentContent',{frameId:tree.frameTree.frame.id,html:syntheticPage(input.settingsFixture.html)});
      const settings=await evaluate(`(async()=>{
        await document.fonts.ready;await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
        const rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
        const rows=[...document.querySelectorAll('.stock-row')].map(row=>{
          const r=rect(row),first=[row.querySelector(':scope>.sym'),row.querySelector(':scope>input.n'),row.querySelector(':scope>.move'),row.querySelector(':scope>.del')],prices=row.querySelector('.stock-prices'),pr=rect(prices),priceFields=[...prices.querySelectorAll('input[data-f]')];
          const boxes=first.map(rect),pb=priceFields.map(rect),style=getComputedStyle(row),previous=[...row.parentElement.querySelectorAll('.stock-row')].indexOf(row)>0;
          const headCells=[...row.parentElement.querySelectorAll(':scope>.stock-head>span')].map(rect);
          const ordered=[boxes[0],boxes[1],...pb,boxes[2],boxes[3]];
          const fields=[...row.querySelectorAll('input[data-f]')].map(e=>e.dataset.f),tint=style.borderTopColor.match(/[\\d.]+/g)||[];
          const borderAlpha=tint.length===4?Number(tint[3]):tint.length===3?1:0;
          return {group:row.dataset.k,symbol:row.dataset.s,fields,priceFields:priceFields.map(e=>e.dataset.f),priceCount:Number(prices.style.getPropertyValue('--price-fields')),
            rect:r,first:boxes,prices:pb,priceDisplay:getComputedStyle(prices).display,
            oneRow:ordered.every(b=>Math.abs((b.top+b.bottom)/2-(r.top+r.bottom)/2)<=4),
            headAligned:headCells.length===ordered.length&&ordered.every((b,i)=>Math.abs(headCells[i].left-b.left)<=1.5),
            smallestFont:Math.min(...[...row.querySelectorAll('input,select,button')].map(e=>parseFloat(getComputedStyle(e).fontSize))),
            codePlain:getComputedStyle(row.querySelector(':scope>.sym')).borderTopWidth==='0px'&&parseFloat(getComputedStyle(row.querySelector(':scope>.sym')).paddingLeft)===0,
            fieldsInside:[...boxes,...pb].every(b=>b.width>0&&b.height>0&&b.left>=r.left-0.5&&b.right<=r.right+0.5&&b.bottom<=r.bottom+0.5),
            previous,divider:parseFloat(style.borderTopWidth)>0&&style.borderTopStyle==='solid'&&borderAlpha>0,
            selectors:first.every(e=>!!e)&&first[3].textContent==='×'};
        });
        return {rows,overflow:document.documentElement.scrollWidth>innerWidth+1,scriptCount:document.scripts.length};
      })()`);
      const expected=input.settingsFixture.expected;
      assertions.push({ok:settings.rows.length===expected.length&&settings.scriptCount===1&&settings.rows.every(row=>{
        const e=expected.find(e=>e.group===row.group&&e.symbol===row.symbol);
        return !!e&&JSON.stringify(row.fields)===JSON.stringify(['note',...e.fields])&&JSON.stringify(row.priceFields)===JSON.stringify(e.fields)&&row.priceCount===e.fields.length&&row.selectors
          &&row.priceDisplay==='contents'&&row.oneRow&&row.headAligned&&row.fieldsInside&&row.codePlain&&row.smallestFont>=13
          &&(!row.previous||row.divider);
      })&&!settings.overflow,name:width+'px真实设置renderer每股一排：列标题与各列对齐、代码纯文字、字号不小于13px、全部字段/删除/移动与相邻分隔线',actual:settings});
    }
    await freshPage(input.negativePage);
    const negative=await evaluate(`(async()=>{
      const p=document.getElementById('costManage'),edit=document.getElementById('costEditBtn');
      const line=s=>document.getElementById('s-'+s.toLowerCase());
      const initial={cost:line('GOOGL').querySelector('.cost-price').textContent,pnl:line('GOOGL').querySelector('.cost-pnl').textContent,zero:line('HII').querySelector('.cost-price').textContent};
      edit.click();const field=f=>p.querySelector('input[data-sym="GOOGL"][data-f="'+f+'"]');
      const normalized=field('cost').value==='-10'&&p.querySelector('input[data-sym="HII"][data-f="cost"]').value==='0';
      const submit=(side,q,price)=>{p.querySelector('[data-trade-action="'+side+'"][data-sym="GOOGL"]').click();const d=p.querySelector('.cost-trade-dialog');d.querySelector('[data-trade-qty]').value=q;d.querySelector('[data-trade-price]').value=price;d.querySelector('[data-trade-confirm]').click();return !d.open;};
      const buy=submit('buy','2','5')&&field('qty').value==='12'&&field('cost').value==='-7.5';
      const sell=submit('sell','2','10')&&field('qty').value==='10'&&field('cost').value==='-11';
      document.getElementById('costCancel').click();edit.click();const restored=field('cost').value==='-10'&&field('qty').value==='10';document.getElementById('costCancel').click();
      return {initial,normalized,buy,sell,restored};
    })()`);
    assertions.push({ok:negative.initial.cost==='-10'&&negative.initial.zero==='0'&&negative.initial.pnl.includes('+')&&negative.initial.pnl.replace(/[^0-9]/g,'').startsWith('1100')
      &&negative.normalized&&negative.buy&&negative.sell&&negative.restored&&scriptErrors.length===0,
      name:'真实Chrome零负成本seed经过normalize显示及买卖回算不变未知或0，取消恢复',actual:negative});
    // 真实正常保存分支的无Token/只读门禁：只模拟锁和唯一GET，fetch本身不覆盖。
    for(const readonly of [false,true]){
      const loaded=waitLoad();await cdp('Page.navigate',{url:'about:blank'});await loaded;
      const tree=await cdp('Page.getFrameTree');scriptErrors.length=0;safetySeed=input.tradeSeed;safetyRequests.length=0;
      const lockFixture='<script>Object.defineProperty(navigator,"onLine",{configurable:true,value:true});Object.defineProperty(navigator,"locks",{configurable:true,value:{request:(name,opts,cb)=>Promise.resolve(cb('+ (readonly?'null':'{name}') +'))}});</script>';
      await cdp('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
      await cdp('Page.setDocumentContent',{frameId:tree.frameTree.frame.id,html:syntheticPage(input.tradePage).replace(isolateScript,isolateScript+lockFixture)});
      await settled();
      if(!readonly)await evaluate(`new Promise(resolve=>{const t=setInterval(()=>{if(document.getElementById('costSyncState')?.textContent.includes('仓位已同步')){clearInterval(t);resolve();}},10);setTimeout(()=>{clearInterval(t);resolve();},1500);})`);
      const gate=await evaluate(`(async()=>{
        const p=document.getElementById('costManage');document.getElementById('costEditBtn').click();await new Promise(r=>requestAnimationFrame(r));
        if(${!readonly}){
          const qty=p.querySelector('input[data-sym="GOOGL"][data-f="qty"]');qty.value='11';qty.dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('costSave').click();
          await new Promise(r=>setTimeout(r,120));
        }
        return {readonly:${readonly},pressed:document.getElementById('costEditBtn').getAttribute('aria-pressed'),hidden:getComputedStyle(p).display==='none',state:document.getElementById('costSyncState').textContent,
          notice:document.getElementById('costNotice').textContent,token:localStorage.getItem('mm_gh_token_v1'),shared:localStorage.getItem('mm_position_costs_local_v1')};
      })()`);
      assertions.push({ok:gate.token===null&&scriptErrors.length===0&&safetyRequests.every(r=>r.method==='GET'&&safetyReads.has(r.url))
        &&(readonly?gate.hidden&&gate.pressed==='false'&&gate.state==='本页只读'&&safetyRequests.length===0:
          safetyRequests.length>=1&&gate.state==='未同步 · 未填 token'&&gate.shared!==null
          &&JSON.parse(gate.shared)._local.dirty===true&&JSON.parse(gate.shared).items.GOOGL.qty===11),
        name:(readonly?'只读':'无Token')+'真实授权与保存路径不能PUT或假同步，锁fixture/合成GET严格隔离',actual:{gate,safetyRequests:safetyRequests.slice(),scriptErrors:scriptErrors.slice()}});
      safetySeed=null;
    }
    await cdp('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
    // 仅本地--final-demo执行：读取用户指定唯一DEMO，替换合成种子；页面自己的补丁不复制进生产。
    stage('保存权限与零负成本已核验');
    if(input.finalDemo){
      for(const [width,height] of VIEWPORTS){
        await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await freshPage(input.finalDemo);
        const expected=await readLayout(),actual=productionLayouts.find(p=>p.width===width).layout,diffs=[];
        const compare=(a,b,key)=>{for(const k of Object.keys(a))if(a[k]!==b[k])diffs.push({key:key+'.'+k,demo:a[k],production:b[k]});};
        compare(expected.parent,actual.parent,'parent');compare(expected.head,actual.head,'head');compare(expected.triangle,actual.triangle,'triangle');compare(expected.cash,actual.cash,'cash');
        expected.zones.forEach((z,i)=>{compare(z.style,actual.zones[i].style,'zone'+i);compare(z.head,actual.zones[i].head,'zoneHead'+i);});
        for(const k of ['row','head','code','quantity','cashExtra'])compare(expected.editor[k],actual.editor[k],'editor.'+k);
        expected.editor.headingStyles.forEach((s,i)=>compare(s,actual.editor.headingStyles[i],'editor.heading'+i));
        for(const k of ['amount','percent','code','name','pnl','ratio','link','bar'])if(expected.day[k]&&actual.day[k])compare(expected.day[k],actual.day[k],'day.'+k);
        assertions.push({ok:diffs.length===0&&scriptErrors.length===0,name:width+'px唯一11-10终稿DEMO与生产合成页父子框/Cash/五列字号几何契约逐项对照',actual:{diffs,scriptErrors:scriptErrors.slice()}});
      }
    }
    const injectionTree=await cdp('Page.getFrameTree');
    const injectionHTML=syntheticPage(input.injectionPage);
    await cdp('Page.setDocumentContent', {frameId: injectionTree.frameTree.frame.id, html: injectionHTML});
    const escaped = await evaluate(`(() => {
      const snapshot = JSON.parse(document.getElementById('snapshotData').textContent);
      return {injected: globalThis.__mmInjected === 1,
        images: document.querySelectorAll('img').length,
        events: document.querySelectorAll('[onerror],[onmouseover],[onfocus]').length,
        symbol: document.querySelector('td.sym').textContent, probe: snapshot.probe};
    })()`);
    assertions.push({ok: !escaped.injected && escaped.images === 0 && escaped.events === 0
      && escaped.symbol.includes('<IMG SRC=X ONERROR="BAD()">')
      && escaped.probe === input.injectionProbe,
      name: '真实浏览器验证HTML属性与script上下文无注入且字符串无损', actual: escaped});
    assertions.push({ok: [...requestUrls].every(url =>
      url === 'http://127.0.0.1:65530/offline-probe' || url.startsWith('data:') || url === 'about:blank'
      || url.split(/[?#]/)[0] === refreshBase || safetyReads.has(url)),
      name:'合成页面不尝试任何云端仓位或外网读写', actual:{requestUrls:[...requestUrls]}});
    stage('全部合成页面断言完成');
    process.stdout.write(JSON.stringify(assertions));
  } catch (error) {
    process.stderr.write(String(error.stack || error));
    process.exitCode = 1;
  } finally {
    if (ws && ws.readyState === WebSocket.OPEN) {
      await call('Browser.close').catch(() => {});
      ws.close();
    } else { chrome.kill(); }
    clearTimeout(deadline);
  }
})();
'''
    with _tempfile.TemporaryDirectory(prefix='mm-target-chrome-') as _profile:
        # 脚本增长后会超过 Windows 命令行长度上限；只在独立测试目录落地执行。
        _browser_file = os.path.join(_profile, 'browser-regression.cjs')
        with open(_browser_file, 'w', encoding='utf-8') as _stream:
            _stream.write(_browser_script)
        _browser_result = _subprocess.run(['node', _browser_file],
            input=_json.dumps({'chrome': _chrome, 'profile': _profile, 'html': _description_page,
                              'colorPage': _color_page, 'rangePage': _range_page, 'filterPage': _filter_page,
                              'timePage': _time_page, 'rulesPage': _rules_page,
                              'tradePage': _trade_page, 'negativePage': _negative_page, 'tradeSeed': _trade_seed, 'finalDemo': _final_demo,
                              'settingsFixture': _settings_fixture,
                              'ciNoSandbox': sys.platform == 'linux' and os.environ.get('GITHUB_ACTIONS') == 'true'
                                  and os.environ.get('MM_TEST_CHROME_NO_SANDBOX') == '1',
                              'injectionPage': _unsafe_page, 'injectionProbe': _unsafe_snapshot['probe']}),
            capture_output=True, text=True, encoding='utf-8', timeout=90)
    check(_browser_result.returncode == 0,
          '独立Chrome样式验证执行成功' + (('：' + _browser_result.stderr) if _browser_result.returncode else ''))
    if _browser_result.returncode == 0:
        print(_browser_result.stderr, end='')
        for _assertion in _json.loads(_browser_result.stdout):
            check(_assertion['ok'], _assertion['name'] + '：'
                  + _json.dumps(_assertion['actual'], ensure_ascii=False, separators=(',', ':')))

# 产物瘦身保留快照黑名单与 CSS 注释剥离；JS 原文内联，不做词法改写。
_slim = slim_snapshot({'run_id': '1', 'request_id': 'r', 'config_files': {'a': 'x'}, 'source_sha': 's',
                       'consensus': {'AAPL': 1}, 'earnings': [1, 2], 'signal_members': {'red': []},
                       'target_overlays': [], 'earnings_week': {}, 'reference_alerts': []})
check(_slim['run_id'] == '1' and _slim['config_files'] == {'a': 'x'} and _slim['source_sha'] == 's',
      '瘦身保留状态核验需要的 run_id / config_files / source_sha')
check(not any(k in _slim for k in ('consensus', 'earnings', 'signal_members', 'target_overlays',
                                   'earnings_week', 'reference_alerts')),
      '瘦身剔除前端从不读取的服务端渲染输入（黑名单）')
check(slim_snapshot({'groups': [1], 'new_field': 2}) == {'groups': [1], 'new_field': 2},
      '瘦身用黑名单：新字段自动带进内联快照，不会被静默丢掉')
_css_asset = '<style>/* 合成 CSS 注释 */.asset-probe{color:red}</style>'
_js_asset = r'''<script>
var REAL=__MM_REAL__;
// 整行注释也必须保留
function separated(x) { return/* 保留词法边界 */x; }
function asi(x) { return/*
保留自动分号换行
*/x; }
var url='https://example.test/a//b';
var regex=/\/\*a\//g;
var template=`模板 /* 文本 */ ${'// 字符串'}`;
</script>'''
_asset_seed = monitor.json_script({'items': {}, 'probe': '</script>'})
_asset_files = {'cost_panel.css': _css_asset, 'cost_panel.js': _js_asset}
_original_asset_open = __import__('io').open


def _open_asset_probe(file, *args, **kwargs):
    asset = _asset_files.get(os.path.basename(str(file)))
    return (_mock_open(read_data=asset)() if asset is not None
            else _original_asset_open(file, *args, **kwargs))


with _patch('io.open', side_effect=_open_asset_probe), \
        _patch.object(monitor, '_cost_seed_json', return_value=_asset_seed):
    _, _asset_js = monitor._cost_panel_assets([])
    _asset_page = monitor.render({}, [], 0, snapshot={})
_expected_asset_js = ('<script>window.__MM_RULES__='
                      + monitor.json_script({**monitor.DEFAULT_SETTINGS, **monitor.S})
                      + ';</script>\n' + _js_asset.replace('__MM_REAL__', _asset_seed))
check(_asset_js == _expected_asset_js and _asset_page.count(_expected_asset_js) == 1,
      'JS 仅替换成本种子并前置规则，return 块注释边界、换行、URL、正则和模板原文完整内联')
check('合成 CSS 注释' not in _asset_page and '.asset-probe{color:red}' in _asset_page,
      'JS 保留注释不影响原有 CSS 注释剥离')

_seed_patch.stop()
print()
if fail:
    print(f"{checks_total} 项检查中 {len(fail)} 项失败"); sys.exit(1)
print(f"全部通过（{checks_total} 项检查）")
