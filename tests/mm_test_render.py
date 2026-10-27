# -*- coding: utf-8 -*-
"""离线渲染测试：不联网，用假数据验证首页 UI 改版与三分组逻辑。"""
import re
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import monitor  # noqa: E402
from unittest.mock import patch as _patch, mock_open as _mock_open

# 整页与可选隔离浏览器只接收合成种子，不把本机真实仓位带进测试页面。
_real_cost_seed_json = monitor._cost_seed_json
_seed_patch = _patch.object(monitor, '_cost_seed_json', return_value=monitor.json_script(
    {"asof": "", "cash": 0, "cash_extra": 0, "fixed": 0, "fixed_cost": 0, "items": {}, "rs20": None}))
_seed_patch.start()


def _row_signals(row):
    """主表只输出各指标描述，级别通过 data-signal-level 表达，不添加圆点。"""
    return monitor._signals_html(row)

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
    "schedule": "美东周一至周五20:15；北京时间夏季次日08:15、冬季次日09:15",
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

check('--collapse-bg:rgba(102,134,198,.6)' in re.search(r'#signalCounts\{([^}]+)\}', html)[1]
      and '#signalCounts .collapse-group{color:#fff}' in html
      and 'opacity:' not in re.search(r'\.collapse-group\{([^}]+)\}', html)[1],
      '统计收起背景40%透明，白箭头不使用整体opacity淡化')

# 规则样式只输出一次，且仍在仓位CSS后保持最终覆盖优先级。
check(html.count(monitor.RULES_CSS) == 1, "规则CSS整块只输出一份")
_cost_css, _cost_js = monitor._cost_panel_assets(items)
check(html.index(_cost_css) < html.index(monitor.RULES_CSS), "规则CSS仍在仓位CSS之后覆盖")

# 标题与排
check("<title>MatrixPulse</title>" in html and '<h1 class="brand">MatrixPulse</h1>' in html,
      "标题 = MatrixPulse（带 brand 类，上涨绿）")
check("h1.brand{color:var(--up)}" in html, "站名用上涨绿 --up，不写死颜色")
check("市场自检" not in html, "旧标题已移除")
check("数据时间 2026-10-06 收盘（美东交易日）" in html and "本页数据更新" not in html,
      "休市顶部显示行情收盘日，不重复抓取完成时间")
check("成功 ·2026-10-07 20:40（北京时间）" in html, "静态成功灯只报「成功 ·日期 时:分」，不带秒")
check("自动计划：美东周一至五 20:15（北京 " in html, "第二排自动计划")
check(("夏令时次日 08:15" in html) != ("冬令时次日 09:15" in html), "冬夏令时只出现一个")
check('id="btnRunNow"' in html and 'id="btnCheckStatus"' in html, "第三排两个按钮都在")
check('<table class="stk"><colgroup><col class="c-sym"><col class="c-earn"><col class="c-px"><col class="c-sig"></colgroup>' in html
      and 'class="px-price"' in html and 'class="earn-cell"' in html, "个股表用固定列宽 colgroup，价格/涨跌幅同列，财报独立一栏")
_near = item("NTNX", "position", "red", chg=5.2); _near["earnings"] = {"status": "ok", "date": "2026-10-09", "timing": "post", "kind": "expected"}
_far = item("TEM", "focus", "red", chg=8.0); _far["earnings"] = {"status": "ok", "date": "2026-12-20", "timing": "pre", "kind": "expected"}
from unittest.mock import patch as _patch
with _patch.object(monitor, "_bj_today", return_value="2026-10-07"):   # 财报颜色按北京时间日算，固定「今天」
    _html_e = monitor.render(macro, [_near, _far] + [d for d in items if d["symbol"] not in ("NTNX", "TEM")], 10, snapshot=snapshot, dup_hidden=2)
check('<span class="earn soon"' in _html_e and _html_e.count('<span class="earn soon"') == 1
      and '<span class="earn">' in _html_e, "D 距北京今天 2 天的财报标黄（earn soon），更远的保持蓝色")
check(' title="' not in _html_e and " title='" not in _html_e, "首页不含任何 title 悬停提示（手机/iPad 无法悬停）")
check(".earn.soon{color:#f0c674" in _html_e, "财报标黄样式存在")
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
            check(_card.count(f'<span class="earn{_css}">') == 5
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
                           for term in ('北京今天', '盘前／盘后不平移', '另留昨天红字')))
    check(_window_in_rules or _window_in_glossary,
          _label + ' 保留北京日历窗口正文且不依赖已删除跳转')

_visible_html = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', _html_e, flags=re.S | re.I)
check('行灯' not in _unescape(re.sub(r'<[^>]+>', '', _visible_html)), '全部用户可见说明统一使用报灯，不残留行灯')

_mobile_start = _html_e.index('@media (max-width:600px){') + len('@media (max-width:600px){')
_mobile_depth = 1
_mobile_end = _mobile_start
while _mobile_depth:
    _char = _html_e[_mobile_end]
    _mobile_depth += (_char == '{') - (_char == '}')
    _mobile_end += 1
_mob_css = _html_e[_mobile_start:_mobile_end - 1]
check('display:grid' not in _mob_css and 'grid-column' not in _mob_css and 'tr:not(:has' not in _mob_css,
      "手机端不再把个股行拆成上下三层 grid，保持四栏各自换行")
check('.stk thead{display:none}' not in _mob_css and '.stk colgroup' not in _mob_css and 'table-layout:auto' not in _mob_css,
      "手机端保留表头和固定列宽")
_desktop_css = _html_e.split('@media (max-width:600px){', 1)[0]
for _label, _css, _widths in (("电脑", _desktop_css, (18, 22, 13, 47)),
                              ("手机", _mob_css, (21, 19, 19, 41))):
    check(all('.c-' + col + '{width:' + str(width) + '%}' in _css
              for col, width in zip(('sym', 'earn', 'px', 'sig'), _widths)),
          _label + "端列宽固定，左侧40%，价格宽度不变，警示栏加宽")
    check(sum(_widths) == 100, _label + "端四栏宽度合计100%")
_macro_u = dict(macro, ust10=dict(macro["ust10"], value=4.55, delta_week=0.12, month_ago=4.0, delta_month=0.55))
_html_u = monitor.render(_macro_u, items, 10, snapshot=snapshot, dup_hidden=2)
check('id="ust10Ref"' in _html_u and "一个月急升 +55 bp" in _html_u and '<span class="unit">参考</span>' in _html_u and "不计入综合灯" not in _html_u
      and '<td class="num dim">+12<span class="unit">周变化 bp</span></td>' in _html_u, "10Y美债参考行：一个月急升标黄，周变化按 bp 显示")
check('id="ust10Ref"' in html and "无数据或已过期" in html.split('id="ust10Ref"')[1].split("</tr>")[0],
      "10Y美债缺一个月数据时显示无数据")
_macro_b = dict(macro, breadth={"ok": True, "name": "上涨参与度", "date": "2026-10-06", "value": 47.0,
                               "pct50": 29.3, "unit": "%", "source": "History of Market"})
_html_b = monitor.render(_macro_b, items, 10, snapshot=snapshot, dup_hidden=2)
check("47.0%<span class=\"unit\">的成分股站上宏观长均线（长期趋势）</span>" in _html_b
      and "站上宏观短均线（短期）：29.3%" in _html_b
      and "宏观均线沿用数据源固定口径，不随个股均线设置变化" in _html_b
      and not re.search(r'\d+日(?:均线|线)', _html_b.split('id="macroCard"', 1)[1].split('</table>', 1)[0]),
      "上涨参与度短长命名，沿用固定宏观口径及原数据值")
# 2026-10-19：顶栏不再重复念灯级计数（统计卡里已有一份），只保留时间＋数据量；「缺失 N」是告警要留
# 2026-10-19：一切正常时只报完成时间；「N 项数据已更新」只在有缺失/不支持代码时才写
check("红2 黄2 绿4" not in html and "10 项数据已更新" not in html
      and "成功 ·2026-10-07 20:40（北京时间） · 缺失 2" in html,
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
check(html.count('class="card group-card"') == 14, "十四个分类均可折叠")
check('id="group_positions">' in html and 'id="group_focus">' in html and ' open>' not in html.split('id="macroCard"', 1)[1][:2], "持仓、重点、市场风险默认折叠")
pos_title = html.split('id="group_positions"', 1)[1].split('</summary>', 1)[0]
check('class="stat-badge red"' in pos_title and 'class="stat-badge missing"' in pos_title and
      '红1' not in pos_title and '缺失1' not in pos_title,
      "分组警示采用灯色圆里带数字的徽章而非颜色文字（2026-10-15）")
# 2026-10-15 再调：名称与灯圈之间不再留 6px，靠得更近
check('.group-card>summary .group-stats{margin-left:0' in html and
      '.group-stats{margin-left:auto' in html, "警示紧贴分组名称右侧左对齐，宏观标题保持原样")
check('window.MM_SETTINGS={"signal_single_open":1}' in html and
      'window.MM_SETTINGS && window.MM_SETTINGS.signal_single_open === 0' in html,
      "设置里的「灯详情只留一个打开」注入页面，默认开")
check('box-sizing:border-box;min-width:17px;height:17px;padding:0 3px;border-radius:999px' in html
      and 'border:1.5px solid var(--dim);background:transparent' in html
      and '.stat-badge.red{border-color:var(--red)}' in html,
      "灯圈改成空心描边圆 17px，数字白色、字号仍是 12.5px")
check('id="group_technology"' in html and 'IT软硬Ai (5)' in html, "IT 分类展示计数")
check('其他关注' not in html, "旧其他关注分类已移除")
check("<h2>个股</h2>" not in html and "<h2>ETF 基金</h2>" not in html, "个股与 ETF 不再分区")
check(all('<thead' not in table and '<th>名称</th>' not in table and 'class="h-earn"' not in table
          for table in re.findall(r'<table class="stk">.*?</table>', html, re.S)),
      "表头「名称｜财报」整行已删除（2026-10-15）")
check(monitor.STK_HEAD == '', "STK_HEAD 不再输出表头")
check("财报 美东" not in html and "下一次财报" not in html.split('id="group_positions"')[1].split('<div class="acts">')[0], "每行不再重复写「财报」二字")
check('无异动 1 只 · 点击查看' in html and 'SPYM' in html, "无异动标的可展开查看")
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
check("盘中运行，北京时间 · 仍为 2026-10-06 日线" in html2,
      "盘中运行若数据仍为前日不能冒充当天行情")
snap_live = dict(snap2, actual_dates={"min":"2026-10-07", "max":"2026-10-07"})
check("数据时间 2026-10-07 23:19:00（盘中快照，北京时间）" in monitor.render(macro, items, 10, snapshot=snap_live),
      "当前交易日盘中数据才显示时间点")
snap_utc = dict(snapshot, finished_at_bj="", finished_at="2026-10-07T12:40:28Z")
check("成功 ·2026-10-07 20:40（北京时间）" in monitor.render(macro, items, 10, snapshot=snap_utc),
      "UTC抓取完成时间正确转为北京时间（只到分钟），行情时间仍为收盘日")
snap_no_finish = {k:v for k,v in snapshot.items() if k not in ('finished_at', 'finished_at_bj')}
check("成功 ·未记录（北京时间）" in monitor.render(macro, items, 10, snapshot=snap_no_finish),
      "缺少完成时间不以开始时间或当前时间冒充")

# 重点关注为空
items3 = [d for d in items if d["group"] != "focus"]
snap3 = dict(snapshot)
snap3["list_counts"] = {"positions": 2, "focus": 0, "technology": 6, "triggers": 0, "dca": 0}
html3 = monitor.render(macro, items3, 8, snapshot=snap3, dup_hidden=0)
check('重点 (0)' in html3 and '暂无标的，去设置页录入或导入 CSV' in html3, "重点关注空态提示")

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
check('rel="apple-touch-icon" href="./icons/apple-touch-icon-180.png"' in head5, "页头有 apple-touch-icon")
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
check(sum(_os.path.getsize(_os.path.join(_root, 'icons', n)) for n in _os.listdir(_os.path.join(_root, 'icons'))) < 400 * 1024, "图标总体积小于 400KB")
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
        monitor._rule_add_price(_al, _x)
        monitor._rule_reduce_price(_al, _x)
        if _extra:
            _al.add('vol_ratio', '量比 5.0×（2026-10-26）', _extra)
        _row = item('BASE' + _side.upper() + str(_extra), 'position', _al.level, chg=0)
        _row.update(signals=_al.signals, signal_marks=_al.marks, trigger=_x.trig, reduce=_x.reduce,
                    target=monitor.evaluate_target(100, 100, {'kind': 'manual', 'price': 101}, 'position', 3))
        _before = _copy.deepcopy(_row)
        _fragment = monitor._stock_row(_row, {})
        _attrs = _fragment.split('><td', 1)[0]
        check('data-base-level="' + (_extra or 'green') + '"' in _attrs
              and 'class="red pur pur-hit"' in _attrs,
              _side + str(_extra) + '发布仍红，基础灯独立且紫状态保留')
        check(_row == _before, _side + str(_extra) + '渲染不改判灯/marks/目标事实')
for _level, _marks in (('red', []), ('red', [('add_price', 'yellow')]),
                       ('yellow', []), ('green', [('boll', 'gray')]), ('gray', [('add_price', 'red')])):
    _row = item('BASELEGACY', 'position', _level, chg=0)
    _row.update(signal_marks=_marks, signals=[], trigger=100)
    check('data-base-level="' + _level + '"' in monitor._stock_row(_row, {}),
          _level + repr(_marks) + '无来源旧灯和缺失灰不能凭已读猜测降绿')

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
      and '今日跌破共识价 190' in _target_panel and _target_panel.count('href="#s-tboth"') == 2,
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
for _level, _width in (('red', 6), ('yellow', 4), ('green', 2)):
    check('tr.' + _level + ' td:first-child{box-shadow:inset ' + str(_width) + 'px 0 0 var(--' + _level in monitor.PAGE_CSS
          and 'tr.' + _level + '+tr.range52-row>td{--lc:var(--' + _level + ');--lw:' + str(_width) + 'px}' in monitor.PAGE_CSS,
          _level + '左条及52周续接原宽度保持' + str(_width) + 'px')
from urllib.parse import unquote as _unquote
import xml.etree.ElementTree as _ET
_target_dash_rule = re.search(r'\.lvtag\.tgt\.low::after,\.lvtag\.tgt\.bad::after\{([^}]*)\}', monitor.PAGE_CSS)
_target_dash_url = re.search(r'url\("(data:image/svg\+xml,[^"]+)"\)', _target_dash_rule[1]) if _target_dash_rule else None
_target_dash_rect = (_ET.fromstring(_unquote(_target_dash_url[1].split(',', 1)[1]))
                     .find('{http://www.w3.org/2000/svg}rect')) if _target_dash_url else None
check(_target_dash_rect is not None and all(_target_dash_rect.get(key) == value for key, value in (
          ('stroke', '#8b93a1'), ('stroke-width', '1'), ('stroke-dasharray', '3 5'), ('rx', '4'), ('fill', 'none')))
      and 'inset:-1px' in _target_dash_rule[1] and 'pointer-events:none' in _target_dash_rule[1],
      'low/bad共用1px灰色SVG短线3px间隔5px的4px圆角边框')
check(all('border:1px solid transparent' in re.search(r'\.lvtag\.tgt\.' + state + r'\{([^}]*)\}', monitor.PAGE_CSS)[1]
          for state in ('low', 'bad'))
      and not re.search(r'\.lvtag\.tgt\.bad::after[^}]*content:none', monitor.PAGE_CSS)
      and '.lvtag.tgt.hit::after{content:none}' in monitor.PAGE_CSS,
      '灰胶囊保留1px占位且SVG虚线不改变达到胶囊')

# 共识只在描述列隐藏；完整信号、空间/达到统计与原胶囊仍保留。
for _row in _target_rows:
    _body = re.search(r'<tr\b[^>]*\bid="' + re.escape(monitor._stock_anchor_id(_row['symbol']))
                      + r'"[^>]*>(.*?)</tr>', _target_page, re.S)[1]
    check('<td class="sig">—</td>' in _body,
          _row['symbol'] + '仅自动共识时描述列保留破折号占位')
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
      and re.search(r'<td class="sym">(.*?)</td>', _legacy_html, re.S)[1].startswith('TFAILOLD')
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
      and '共识价 250，空间 +150.0%' in _description_panel
      and '今日跌破共识价 190' in _description_panel
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
check(_technical_mention['signals'][0] in _row_signals(_technical_mention),
      '自动共识kind也不误删包含关键字的原技术描述')
_runbar = re.search(r'<div class="runbar">(.*?)</div>', _description_page, re.S)[1]
check('href="#signalRules"' not in _runbar and 'shortcut.textContent = "预警规则"' not in monitor.PAGE_INTERACTION_JS,
      '静态及动态顶部均不恢复已删除的预警规则快捷按钮')
check(bool(re.search(r'\.lvtag\.tgt\.high\{[^}]*color:var\(--target-high\)', _description_page)),
      '高空间数字显式跟随紫红外框#dc62b8')

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
        check(re.findall(r'data-signal-kind="([^"]*)" data-signal-level="([^"]*)"', _sig)
              == [('boll_rsi', 'yellow')]
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
_filter_palette = {'red': '#ff8580', 'yellow': '#f0c674', 'gray': 'var(--text)',
                   'purple': 'var(--purple)', 'green': 'var(--green)'}
for _level, _color in _filter_palette.items():
    _selector = f'#signalCounts .signal-filter-condition[data-signal-level="{_level}"]'
    check(monitor.PAGE_CSS.count(_selector + '{color:' + _color + '}') == 1,
          '多选' + _level + '只按自身级别着色一次，不改单选或名称竖条')
_filter_css = re.sub(r'/\*.*?\*/', '', monitor.PAGE_CSS, flags=re.S)
_filter_rules = [(selector.strip(), body) for selector, body in re.findall(r'([^{}]+)\{([^{}]*)\}', _filter_css)
                 if '.signal-filter-condition' in selector]
check(len(_filter_rules) >= len(_filter_palette)
      and all(selector == '#signalCounts .signal-filter-condition'
              or selector in {f'#signalCounts .signal-filter-condition[data-signal-level="{level}"]'
                              for level in _filter_palette}
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
    _sym = re.search(r'<td class="sym">(.*?)</td>', _body, re.S)[1]
    check(_classes == [_color] and 'signal-dot' not in _body
          and _sym.startswith(_row['symbol'])
          and re.findall(r'data-signal-kind="([^"]*)" data-signal-level="([^"]*)"', _sig)
          == [('chg', _color)] + ([(_kind, 'gray')] if _row['symbol'] == 'STXR' else [])
          and re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                         r'(?: data-signal-level="([^"]*)")?>', _sig)
          == [('chg', _color), (_kind, 'gray' if _row['symbol'] == 'STXR' else '')],
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
          and 'signal-dot' not in _html_tail and _html_tail.count('signal-separator') == 1
          and re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                         r'(?: data-signal-level="([^"]*)")?>', _html_tail)
          == [('boll_rsi' if _rsi else 'boll', _level or 'gray'), ('vol_ratio', '')]
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
    _copy_plain = _unescape(re.sub(r'<[^>]+>', '', _copy_html))
    check(_copy_plain == _expected and f'data-signal-kind="{_kind}"' in _copy_html,
          _kind + '个股描述直接展示完整事实，不自动添加指标名称')
    check(_copy_row == _copy_before, _kind + '去标签不修改原始事实、判灯及配置')

# 描述红黄灰无紫稳定排序并安全转义，均线只投影展示，原数字事实与marks保留。
_order_row = item('ORDERESCAPE', 'technology', 'red')
_order_row.update(signals=['手填目标价说明 <b>尾部</b>', '无灯说明 <img onerror=x>',
    'RSI 2条 ↓超卖（≤35）', '日内触及120日均线 ±1%区间', '上穿60日弱均线',
    '振幅 6%', '量比 2.8x', '异动 -12.1%', '距加仓价 90 还差 1%', '另条说明 & 保留'],
    signal_marks=[('target', 'purple'), ('ma_band', 'yellow'), ('ma_cross', 'yellow'),
                  ('amp', 'yellow'), ('vol_ratio', 'yellow'), ('chg', 'red'), ('add_price', 'red')],
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
check(re.findall(r'data-signal-kind="([^"]*)"', _order_sig)
      == ['chg', 'amp', 'add_price', 'ma', 'vol_ratio', 'rsi', '', '']
      and 'signal-dot' not in _order_body
      and re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                     r'(?: data-signal-level="([^"]*)")?(?: data-signal-role="[^"]*")?>', _order_sig)
      == [('chg', 'red'), ('amp', 'yellow'), ('add_price', 'red'), ('ma', 'yellow'),
          ('vol_ratio', 'yellow'), ('rsi', 'gray'), ('', ''), ('', '')]
      and 'data-signal-kind="amp" data-signal-level="yellow" data-signal-role="supplement"' in _order_sig,
      '全页零圆点，价格补充紧跟主要提醒，其余描述稳定排序且均线合并一次级别')
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
      and _order_filter_data['amp']['members'][0]['d'][0]['l'] == 'yellow'
      and _order_filter_data['rsi']['members'][0]['d'][0]['l'] == 'gray'
      and all(_order_filter_data[k]['members'][0]['l'] == 'red' for k in ('chg', 'ma', 'amp', 'rsi')),
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
      and re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                     r'(?: data-signal-level="([^"]*)")?(?: data-signal-role="[^"]*")?>', _order_control_sig)
      == [('chg', 'red'), ('amp', 'yellow'), ('add_price', 'red'), ('ma', 'yellow'),
          ('vol_ratio', 'yellow'), ('', ''), ('rsi', ''), ('', '')]
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
          and re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                         r'(?: data-signal-level="([^"]*)")?>', _sig) == _expected_levels
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
_four_quiet_tables = re.findall(r'<details class="quiet-list">.*?<table class="stk">(.*?)</table>', _four_page, re.S)
_four_quiet_html = ''.join(_four_quiet_tables)
for _row, _kind, _technical_level, _foldable, _overall in _four_cases:
    _classes, _body = rendered_stock_row(_four_page, _row, not _foldable)
    _sig = re.search(r'<td class="sig">(.*?)</td>', _body, re.S)[1]
    _levels = re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                        r'(?: data-signal-level="([^"]*)")?>', _sig)
    # 合成输入为0/3/6%，重算后有浮点误差；同色黄灯保留先出现的技术描述，涨跌红优先。
    # 其他组零条上轨是无提示，不应生成空级布林描述；有RSI事实的无灯组合仍留文字。
    _expected = [] if _kind == 'boll' and not _technical_level else [(_kind, _technical_level)]
    _expected += [('chg', 'red' if round(_row['chg']) == 6 else 'yellow')] if _row['chg'] else []
    _expected.sort(key=lambda pair: ({'red': 0, 'yellow': 1, 'gray': 2, '': 3}[pair[1]],
                                     0 if pair == ('chg', 'red') else 1))
    check(_classes == [_overall] and _row['foldable'] == _foldable and _levels == _expected,
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

# 振幅专项真实分析→快照→红黄/静默；技术灰、条件颜色、折叠资格互不覆盖。
from tests.mm_test_groups import TestAmplitudeChain as _AmplitudeChain
_amp_engine = _AmplitudeChain()
_amp_engine.setUp()
try:
    _amp_rows = [_amp_engine.analyze(symbol='AMPUP', prev=99, high=104, low=98),
                 _amp_engine.analyze(symbol='AMPDOWN', group='healthcare', prev=101, high=103, low=96),
                 _amp_engine.analyze(symbol='AMPZERO', group='index_funds', high=100, low=100),
                 _amp_engine.analyze(symbol='AMPDUAL', high=106, low=94, values=(35, 35, 50)),
                 _amp_engine.analyze(symbol='AMPCOMBO', values=(35, 35, 35), bands=(100, 110, 99)),
                 _amp_engine.analyze(symbol='AMPSTACK', high=106, low=94, prev=100 / 1.06, values=(70, 50, 50),
                                     bands=(100, 110, 99), cfg={'trigger': 100, 'target': 190}, ratio=3)]
    _amp_no_gray_row = _amp_engine.analyze(symbol='AMPDUAL2HIGH', high=106, low=94, values=(70, 70, 50))
    _amp_no_gray_before = _copy.deepcopy(_amp_no_gray_row)
    _amp_no_gray_snap = monitor.build_snapshot({}, [_amp_no_gray_row], {}, group_counts={})
    _amp_no_gray_snap_before = _copy.deepcopy(_amp_no_gray_snap)
    _amp_no_gray_page = monitor.render({}, [_amp_no_gray_row], 1, snapshot=_amp_no_gray_snap)
    _amp_input_before = _copy.deepcopy(_amp_rows)
    _amp_snap = monitor.build_snapshot({}, _amp_rows, {}, group_counts={})
    _amp_snap_before = _copy.deepcopy(_amp_snap)
    _amp_page = monitor.render({}, _amp_rows, len(_amp_rows), snapshot=_amp_snap)
finally:
    _amp_engine.doCleanups()
_amp_filter = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _amp_page)[1]))
check([m['kind'] for m in _amp_snap['text_only']] == ['rsi', 'boll_rsi'],
      '真实振幅全链：只保留2项技术灰，振幅不再计灰')
_amp_stats = _amp_page.split('id="signalCounts"', 1)[1].split('id="group_positions"', 1)[0]
check('aria-label="白 2 项"' in _amp_stats and 'aria-label="红 3 项"' in _amp_stats
      and '>白</td>' in _amp_stats and '>灰</td>' not in _amp_stats
      and not re.search(r'aria-label="(?:灰灯|灰 \d+ 项)', _amp_stats),
      '白标题仍按技术gray身份并集2，振幅红与其他红按最终行去重计3')
_amp_stat_row = re.search(r'<tr><td><button[^>]*data-kind="amp".*?</tr>', _amp_stats, re.S)[0]
check([_unescape(re.sub(r'<[^>]+>', '', cell)) for cell in
       re.findall(r'<td\b[^>]*>(.*?)</td>', _amp_stat_row, re.S)] == ['振幅', '2', '2', '—'],
      '振幅统计表精确列序：红2、黄2、灰空')
_amp_panels = {}
for _level in ('red', 'yellow'):
    _panel_id = 'signal-members-' + __import__('hashlib').sha256(b'amp').hexdigest() + '-' + _level
    _amp_panels[_level] = re.search(r'<div class="signal-members-panel tone-' + _level + '" id="' + _panel_id
        + r'" hidden>(.*?)</div>(?=</td>|<div class="signal-members-panel)', _amp_stats, re.S)[1]
check(all(title in _amp_panels['yellow'] for title in ('<h5><span class="grp-arrow arrow-band-up">▲</span> 上涨</h5>',
                                                     '<h5><span class="grp-arrow arrow-band-down">▼</span> 下跌</h5>'))
      and _amp_panels['yellow'].index('上涨</h5>') < _amp_panels['yellow'].index('下跌</h5>')
      and '<h5>平盘</h5>' in _amp_panels['red'],
      '振幅红黄名单仍按涨/跌/平分框，方向结构不变')
check(all(len(re.findall(r'<li\b[^>]*>', panel)) == 2 for panel in _amp_panels.values())
      and '振幅 0.0%' not in ''.join(_amp_panels.values())
      and not re.search(r'aria-label="(?:灰灯|白灯) · 振幅', _amp_stats),
      '振幅仅已触发红黄进入名单，合法0静默，无旧振幅灰面板')
for _row, _amp_level in zip(_amp_rows, ('yellow', 'yellow', None, 'red', None, 'red')):
    _classes, _body = rendered_stock_row(_amp_page, _row, not _row['foldable'])
    _sig = re.search(r'<td class="sig">(.*?)</td>', _body, re.S)[1]
    _kinds_levels = re.findall(r'data-signal-kind="([^"]*)" data-signal-level="([^"]*)"', _sig)
    _expected_classes = [_row['level']] + (['pur', 'pur-high'] if _row['purple'] else [])
    check(_classes == _expected_classes and ('amp', 'gray') not in _kinds_levels
          and _kinds_levels.count(('amp', _amp_level)) == int(_amp_level is not None)
          and ('data-signal-kind="amp"' in _sig) == (_amp_level is not None) and 'signal-dot' not in _sig,
          _row['symbol'] + '振幅只呈现自身红黄颜色，低于黄线无条件文字，主行灯独立')
    _record = next((m for m in _amp_filter['amp']['members'] if m['s'] == _row['symbol']), None)
    if _amp_level:
        _metric = next(m['metric'] for m in _amp_snap['signal_members']['amp'][_amp_level]
                       if m['symbol'] == _row['symbol'])
        check(_record['l'] == _row['level'] and _record['d'] == [{'l': _amp_level, 't': _metric}],
              _row['symbol'] + '振幅筛选保留最终行灯和逐条件红黄事实')
    else:
        check(_record is None, _row['symbol'] + '未达黄线不进入振幅筛选')
check(_amp_rows[2]['amp'] == 0 and _amp_rows[2]['foldable'] and _amp_rows[2]['signals'] == []
      and monitor._stock_anchor_id('AMPZERO') in ''.join(re.findall(
          r'<details class="quiet-list">.*?<table class="stk">(.*?)</table>', _amp_page, re.S)),
      '合法0保存数值，静默且可进入无异动，不强制不折叠')
check('data-signal-kind="rsi" data-signal-level="gray"' in monitor._signals_html(_amp_rows[3])
      and 'data-signal-kind="amp" data-signal-level="red"' in monitor._signals_html(_amp_rows[3]),
      '同股合法2low技术灰+振幅红分别白字/红字，不继承整行灯色')
_amp_control_classes, _amp_control_body = rendered_stock_row(_amp_no_gray_page, _amp_no_gray_row, False)
_amp_control_sig = re.search(r'<td class="sig">(.*?)</td>', _amp_control_body, re.S)[1]
_amp_control_filter = _json.loads(_unescape(re.search(r'data-filter="([^"]*)"', _amp_no_gray_page)[1]))
check(_amp_control_classes == ['red'] and _amp_no_gray_row['foldable']
      and _amp_no_gray_row['signal_marks'] == [('amp', 'red')]
      and re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                     r'(?: data-signal-level="([^"]*)")?>', _amp_control_sig)
      == [('amp', 'red'), ('rsi', '')]
      and '↑超买' in _unescape(re.sub(r'<[^>]+>', '', _amp_control_sig)),
      '原振幅2high控制保留无灯技术文字、独立振幅红与既有可折叠资格')
check(_amp_no_gray_snap['text_only'] == [] and _amp_no_gray_snap['summary']['red'] == 1
      and _amp_no_gray_snap['signal_counts']['amp']['red'] == 1
      and all(not members for members in _amp_no_gray_snap['signal_members']['rsi'].values())
      and not _amp_control_filter.get('rsi', {}).get('members', [])
      and _amp_control_filter['amp']['members'][0]['l'] == 'red'
      and _amp_no_gray_row == _amp_no_gray_before and _amp_no_gray_snap == _amp_no_gray_snap_before,
      '原振幅2high控制不进灰名单或RSI筛选，振幅红统计保留且事实快照不突变')
check('data-signal-kind="boll_rsi" data-signal-level="red"' in monitor._signals_html(_amp_rows[4])
      and monitor.text_only_members([_amp_rows[4]]) == []
      and _amp_snap['signal_counts']['boll_rsi']['red'] == 1
      and all(_amp_snap['signal_counts'][k][c] == 0 for k in ('boll', 'rsi') for c in ('red', 'yellow')),
      '组合红时低振幅静默，不重复单独布林RSI或技术灰')
check(all((kind, 'red') in _amp_rows[-1]['signal_marks'] for kind in ('chg', 'add_price', 'vol_ratio', 'amp'))
      and _amp_rows[-1]['purple'] and not _amp_rows[-1]['foldable']
      and [m['kind'] for m in monitor.text_only_members([_amp_rows[-1]])] == ['boll_rsi'],
      '振幅红+技术灰+涨跌/加仓/量比红+目标紫独立，技术不折叠资格不变')
check('.signal-item[data-signal-level="gray"]{color:var(--text)}' in monitor.PAGE_CSS
      and _amp_rows == _amp_input_before and _amp_snap == _amp_snap_before,
      '生产技术灰仍白字且完整渲染不突变分析或快照')

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
    check(re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                     r'(?: data-signal-level="([^"]*)")?>', _arrow_sig) == [(_arrow_kind, _arrow_level)]
          and re.findall(r'<span class="(arrow-[a-z-]+)">([▲▼↑↓])</span>', _arrow_sig) == _arrow_expected
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
for _level, _body_color in (('red', '#ff8580'), ('yellow', '#f0c674'), ('gray', 'var(--text)')):
    check(f'.signal-item[data-signal-level="{_level}"]{{color:{_body_color}}}' in _arrow_css,
          _level + '主表条件正文继续使用原色，与箭头涨跌色独立')
check('.signal-item{color:var(--dim)}' in _arrow_css
      and '.arrow-rsi-up,.arrow-rsi-down{color:inherit;font-size:1.15em;line-height:1}' in _arrow_css,
      '无灯正文灰字与RSI继承色/字号基础规则均保持')

_rules_arrow_table = re.search(r'<table\b[^>]*id="rules-lamp-table"[^>]*>.*?</table>',
                              re.search(r'<template id="rulesTpl">(.*?)</template>', html, re.S)[1], re.S)[0]
check(all(f'<span class="{cls}">{symbol}</span>' in _rules_arrow_table
          for cls, symbol in (('ar-band-up', '▲'), ('ar-band-down', '▼'),
                              ('ar-rsi-up', '↑'), ('ar-rsi-down', '↓')))
      and not re.search(r'\b(?:arrow-band|arrow-rsi|grp-arrow)\b', _rules_arrow_table),
      '规则表四箭头仍使用独立ar-* wrapper，不套主表或统计方向类')
_white_rule_selectors = ','.join(f'#rules-lamp-table tbody td:first-of-type .{cls}'
                               for cls in ('ar-band-up', 'ar-band-down', 'ar-rsi-up', 'ar-rsi-down'))
check(_white_rule_selectors + '{color:#fff}' in re.sub(r'\s+', ' ', _arrow_css).replace(', ', ',')
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
check(_unescape(re.sub(r'<[^>]+>', '', _projection_sig)) ==
      '跌破布林下轨▼，下轨外，连续第4日贴近下轨（其中2日穿越） · RSI 3条 ↓超卖 / '
      '触及52周新低（距低点 +2.6%）（低点价：1,230.13） / '
      '触及长均线 ±1%区间（均线价：1,234.57）',
      '完整主表仅投影阈值、触及价、下轨状态、真实均线价与52周低点')
check(re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                 r'(?: data-signal-level="([^"]*)")?>', _projection_sig) ==
      [('boll_rsi', 'red'), ('low52', 'red'), ('ma', 'yellow')]
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
    check(_unescape(re.sub(r'<[^>]+>', '', monitor._signals_html(_projection_control))) ==
          _projection_state + '，连续第4日贴近下轨（其中2日穿越） · RSI 3条 ↓超卖',
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
    check(_unescape(re.sub(r'<[^>]+>', '', _upper_html)) == _upper_state + ' · RSI 2条 ↑超买'
          and _upper_html.count('<span class="arrow-band-up">▲</span>') == 1
          and 'data-signal-level="yellow"' in _upper_html and _upper_row == _upper_before,
          repr(_upper_price) + '上轨外不加括号/等于及回落统一后收回，原事实和箭头级别不变')
for _projection_values, _projection_ma_label in (({'120': 1234.567}, '1,234.57'),
        ({}, '—'), ({'120': 0}, '—'), ({'120': float('nan')}, '—')):
    _projection_control = dict(_projection_row, ma_values=_projection_values,
        signals=[_projection_row['signals'][3]], signal_marks=[('ma_band', 'yellow')])
    check(_unescape(re.sub(r'<[^>]+>', '', monitor._signals_html(_projection_control))) ==
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
      and _json.loads(_seed_text)['rs20']['bench_name'] == _inject + '\u2028\u2029',
      '成本种子代码与基准名安全JSON序列化，不允许script结束标签且保留原值')
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
    for _chg_level, _amp_level in (('red', 'red'), ('red', 'yellow'), ('yellow', 'red'), ('yellow', 'yellow')):
        _color_row = item('COLOR' + _chg_level.upper() + _amp_level.upper(), 'technology',
                          'red' if 'red' in (_chg_level, _amp_level) else 'yellow')
        _color_row.update(signals=[('异动 -12.0%' if _chg_level == 'red' else '波动 +8.2%'),
                                   '振幅 11.2%（756.00~840.47）'],
                          signal_marks=[('chg', _chg_level), ('amp', _amp_level)])
        _color_rows.append(_color_row)
    with _patch.object(monitor, 'global_dca', return_value=None):
        _color_snapshot = monitor.build_snapshot({}, _color_rows, {}, group_counts={})
        _color_page = monitor.render({}, _color_rows, len(_color_rows), snapshot=_color_snapshot)
    _range_rows = []
    for _sym, _low, _price, _high in (('RANGENORM', 100.0, 150.0, 200.0), ('RANGEHI', 420.0, 688.0, 692.0)):
        _range_row = item(_sym, 'position', 'green')
        _range_row.update(price=_price, data_date='2026-10-26',
                          range52={'low': _low, 'high': _high, 'scope': '52周', 'from': '2025-10-27',
                                   'to': '2026-10-26', 'position': (_price - _low) / (_high - _low) * 100})
        _range_rows.append(_range_row)
    with _patch.object(monitor, 'global_dca', return_value=None):
        _range_snapshot = monitor.build_snapshot({}, _range_rows, {}, group_counts={})
        _range_page = monitor.render({}, _range_rows, len(_range_rows), snapshot=_range_snapshot)
    # 52周页顺带带上成本面板：给两行合成持仓（成本/数量都是假数），
    # 这样同一页还能验「编辑」胶囊加大、贴右、手机上不错行；仍是纯合成数据，不碰本机或云端。
    _range_seed = {"asof": "2026-10-26", "cash": 0, "cash_extra": 0, "fixed": 0, "fixed_cost": 0,
                   "items": {"RANGENORM": {"cost": 125.0, "qty": 10}, "RANGEHI": {"cost": 600.0, "qty": 5}},
                   "rs20": None}
    _range_page = re.sub(r'var REAL=.*?;\n',
                         lambda m: 'var REAL=' + monitor.json_script(_range_seed) + ';\n', _range_page, count=1)
    if 'var REAL=' + monitor.json_script(_range_seed) + ';' not in _range_page:
        raise AssertionError('52周页缺少合成成本种子，拒绝启动浏览器')
    _expected_browser_seed = monitor.json_script(
        {"asof": "", "cash": 0, "cash_extra": 0, "fixed": 0, "fixed_cost": 0, "items": {}, "rs20": None})
    if 'var REAL=' + _expected_browser_seed + ';' not in _description_page:
        raise AssertionError('浏览器输入缺少合成仓位种子，拒绝启动')
    check(True, '浏览器输入只包含空合成仓位种子，不嵌入本机或云端仓位')
    import subprocess as _subprocess
    import tempfile as _tempfile
    _chrome = os.environ.get('MM_TEST_CHROME', 'C:/Program Files/Google/Chrome/Application/chrome.exe')
    _browser_script = r'''
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const assertions = [];
const failedRequests = [];
const requestUrls = new Set();
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
const deadline = setTimeout(() => {
  process.stderr.write('Chromium 合成测试超过60秒，终止独立实例\n' + diagnostic);
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
    ws = new WebSocket(endpoint);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, {once: true});
      ws.addEventListener('error', reject, {once: true});
    });
    let seq = 0;
    const pending = new Map();
    let refreshDocument = null;
    const refreshBase = 'https://mm-refresh.test/market-monitor/index.html';
    call = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, {resolve, reject});
      ws.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
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
        } else {
          call('Fetch.failRequest', {requestId: msg.params.requestId, errorReason: 'InternetDisconnected'}, msg.sessionId)
            .catch(() => {});
        }
      } else if (msg.method === 'Network.loadingFailed') {
        failedRequests.push(msg.params.errorText);
      }
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
    await cdp('Page.setDocumentContent', {frameId: tree.frameTree.frame.id, html: input.html});
    const evaluate = async expression => {
      const result = await cdp('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const isolation = await evaluate(`(async () => {
      let blocked = false;
      try { await fetch('http://127.0.0.1:65530/offline-probe'); } catch (_) { blocked = true; }
      return {href: location.href, origin: location.origin, blocked};
    })()`);
    isolation.requestFailures = failedRequests;
    assertions.push({ok: isolation.href === 'about:blank' && isolation.origin === 'null' && isolation.blocked
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
    for (const [width, height] of [[1440, 1000], [820, 1180], [390, 844]]) {
      await cdp('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
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
        && header.count === String(header.expected) && header.border === 'rgb(163, 113, 247)' && !header.overflow,
        name: width + 'px统计表头红黄灰紫财报顺序、紫灯同源及折叠重开后保留', actual: header});
      const actual = await evaluate(`(() => {
        document.querySelectorAll('details.group-card,details.quiet-list').forEach(el => el.open = true);
        const high = document.querySelector('#s-thigh .lvtag.tgt.high');
        const space = document.querySelector('#s-tspace .lvtag.tgt.space');
        const hit = document.querySelector('#s-tboth .lvtag.tgt.hit');
        const bad = document.querySelector('#s-tfailold .lvtag.tgt.bad');
        const low = document.querySelector('#s-tlow .lvtag.tgt.low');
        const style = el => getComputedStyle(el);
        const rect = el => { const r = el.getBoundingClientRect(); return [r.width, r.height]; };
        const originalSize = rect(high);
        high.style.color = '#c4a3ff';
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
          for (const [level, width, color] of [['red', 6, 'rgb(248, 81, 73)'],
              ['yellow', 4, 'rgb(210, 153, 34)'], ['green', 2, 'rgb(63, 185, 80)']]) {
            row.className = originalClass.replace(/\\b(red|yellow|green|gray)\\b/, level);
            const sig = style(row.querySelector('td.sig'));
            const left = style(row.firstElementChild);
            const continuation = style(row.nextElementSibling.firstElementChild);
            stripes.push({state, level, width, color, sigImage: sig.backgroundImage,
              sigShadow: sig.boxShadow, leftImage: left.backgroundImage, leftSize: left.backgroundSize,
              rangeImage: continuation.backgroundImage, rangeSize: continuation.backgroundSize,
              rangePosition: continuation.backgroundPosition});
          }
          row.className = originalClass;
        }
        return {
          stripes, sparseBorders: [sparseBorder(low), sparseBorder(bad)],
          highColor: style(high).color,
          highNumbers: [...high.querySelectorAll('span')].map(el => style(el).color),
          highBorder: getComputedStyle(high, '::after').borderTopColor,
          highBorderWidth: getComputedStyle(high, '::after').borderTopWidth,
          highShadow: style(high).textShadow,
          originalSize, priorSize,
          spaceColor: style(space).color, spaceBorder: getComputedStyle(space, '::after').borderTopColor,
          hitColor: style(hit).color, hitBackground: style(hit).backgroundColor,
          hitAfter: getComputedStyle(hit, '::after').content,
          hitClass: hit.closest('tr').className,
          badColor: style(bad).color, badBorderStyle: style(bad).borderTopStyle,
          lowBorder: style(low).borderTopColor,
          sig: document.querySelector('#s-thigh td.sig').textContent,
          manual: document.querySelector('#s-manhigh td.sig').textContent,
          runbarRuleLinks: document.querySelectorAll('.runbar a[href="#signalRules"]').length
        };
      })()`);
      const pink = 'rgb(220, 98, 184)';
      assertions.push({ok: actual.highColor === pink && actual.highBorder === pink
        && actual.highNumbers.length >= 2 && actual.highNumbers.every(color => color === pink)
        && actual.highShadow.includes(pink) && parseFloat(actual.highBorderWidth) > 0,
        name: width + 'px真实CSS高空间外框、数字及等宽加重同色#dc62b8', actual});
      assertions.push({ok: actual.originalSize[0] > 0 && actual.originalSize[1] > 0
        && JSON.stringify(actual.originalSize) === JSON.stringify(actual.priorSize)
        && actual.spaceColor === 'rgb(196, 163, 255)' && actual.spaceBorder === 'rgb(163, 113, 247)'
        && actual.hitColor === 'rgb(255, 255, 255)' && actual.hitBackground === 'rgb(137, 87, 229)'
        && actual.hitAfter === 'none' && actual.hitClass.includes('pur-hit') && !actual.hitClass.includes('pur-high')
        && actual.badColor === 'rgb(139, 147, 161)' && actual.badBorderStyle === 'solid'
        && actual.lowBorder === 'rgba(0, 0, 0, 0)'
        && !actual.sig.includes('共识') && !actual.manual.includes('目标价') && !actual.manual.includes('共识价')
        && actual.runbarRuleLinks === 0,
        name: width + 'px尺寸不变、达到优先、失败灰虚线及描述过滤', actual: {
          size: actual.originalSize, priorSize: actual.priorSize,
          hitColor: actual.hitColor, hitBackground: actual.hitBackground, badColor: actual.badColor,
          sig: actual.sig, manual: actual.manual, runbarRuleLinks: actual.runbarRuleLinks}});
      assertions.push({ok: actual.stripes.length === 9 && actual.stripes.every(s =>
        s.sigImage === 'none' && s.sigShadow === 'none'
        && s.leftImage === 'linear-gradient(' + s.color + ', ' + s.color + ')'
        && s.leftSize.startsWith(s.width + 'px ')
        && s.rangeImage === s.leftImage && s.rangeSize === s.width + 'px 18px'
        && s.rangePosition === '0% 0%'),
        name: width + 'px普通/高/达到目标均无右紫条，红黄绿左条及52周续接保留', actual: actual.stripes});
      assertions.push({ok: actual.sparseBorders.length === 2 && actual.sparseBorders.every((s, i) =>
        s.color === (i === 0 ? 'rgb(196, 163, 255)' : 'rgb(139, 147, 161)')
        && s.border === 'rgba(0, 0, 0, 0)' && s.width === '1px' && s.radius === '4px'
        && s.afterContent !== 'none' && s.afterInset.every(value => value === '-1px')
        && s.pointerEvents === 'none' && s.backgroundSize === '100% 100%'
        && s.stroke === '#8b93a1' && s.strokeWidth === '1' && s.dash === '3 5' && s.rx === '4'
        && s.original[0] > 0 && s.original[1] > 0
        && JSON.stringify(s.original) === JSON.stringify(s.nativeSize)),
        name: width + 'pxlow/bad实际CSS为稀疏灰SVG虚线且尺寸、字色、4px圆角不变', actual: actual.sparseBorders});
    }
    // 真实导航/刷新仅由 CDP 提供合成文档；其他请求仍全部拦截，绝不读取云端仓位。
    refreshDocument = input.html.replace(/var REAL=(.*?);/, (match,json) => {
      const seed=JSON.parse(json);seed.preview=true;return 'var REAL='+JSON.stringify(seed)+';';
    }).replace(/<script\b[^>]*\bsrc=[^>]*>[\s\S]*?<\/script>/gi,'')
      .replace(/<link\b[^>]*>/gi,'').replace('</head>','<link rel="icon" href="data:,"></head>');
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
    for (const [width,height] of [[1440,1000],[820,1180],[390,844]]) {
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      let loaded=waitLoad();
      await cdp('Page.navigate',{url:refreshBase+'?keep=1&token=synthetic'}); await loaded;
      await evaluate(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
      const before = await evaluate(`(() => {
        const link=document.querySelector('a.signal-member-jump[href="#s-thigh"]');
        if(!link) throw Error('missing synthetic stock jump');
        link.click();
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
          open:[...document.querySelectorAll('details.card,details.cost-manage')].filter(d=>d.open).map(d=>d.id),
          y:scrollY,restoration:history.scrollRestoration};
      })()`);
      assertions.push({ok:before.hash==='#s-thigh' && before.open && after.hash==='' && !after.focus && !after.edit
        && !after.marker && after.keep==='1' && after.token==='synthetic'
        && JSON.stringify(after.open)===JSON.stringify(['signalCounts']) && after.y===0,
        name:width+'px真实股票跳转后刷新清旧定位、分组折叠/统计打开及回顶部',actual:{before,after}});
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
    // 前面的合成导航已建立HTTPS origin；显式preview防止空种子走正式仓位读取路径。
    const colorHTML=input.colorPage.replace(/var REAL=(.*?);/, (match,json) => {
      const seed=JSON.parse(json);seed.preview=true;return 'var REAL='+JSON.stringify(seed)+';';
    }).replace(/<script\b[^>]*\bsrc=[^>]*>[\s\S]*?<\/script>/gi,'')
      .replace(/<link\b[^>]*>/gi,'').replace('</head>','<link rel="icon" href="data:,"></head>');
    await cdp('Page.setDocumentContent', {frameId: colorTree.frameTree.frame.id, html: colorHTML});
    for (const [width,height] of [[1440,1000],[820,1180],[390,844]]) {
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
      assertions.push({ok:colors.length===5 && all.length>0 && all.every(el=>el.color===el.expected)
        && ['red','yellow','gray',''].every(level=>all.some(el=>el.level===level))
        && colors.every(row=>row.items.filter(el=>el.role==='primary').length===1
          && row.items.filter(el=>el.role==='supplement').length===1),
        name:width+'px红红/红黄/黄红/黄黄配对及同股白灯、仅文字均按自身等级着色，补充不再变灰',actual:colors});
      const lamps=await evaluate(`(() => {
        const levels=['red','yellow','green','gray'];
        const expected={red:'rgb(248, 81, 73)',yellow:'rgb(210, 153, 34)',
          green:'rgb(63, 185, 80)',gray:'rgb(230, 232, 236)'};
        return [...document.querySelectorAll('#signalCounts .signal-members-panel')]
          .filter(panel=>['tone-red','tone-yellow','tone-gray'].some(c=>panel.classList.contains(c)))
          .flatMap(panel=>[...panel.querySelectorAll('a.signal-member-jump')].map(link=>{
            const li=link.closest('li'),row=document.getElementById(link.getAttribute('href').slice(1));
            const level=levels.find(c=>row && row.classList.contains(c)),before=getComputedStyle(li,'::before');
            const rowStyle=row?getComputedStyle(row.querySelector('td.sym')):null;
            const rowLamp=rowStyle?rowStyle.backgroundImage+' '+rowStyle.boxShadow:'';
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
    const rangeTree=await cdp('Page.getFrameTree');
    const rangeHTML=input.rangePage.replace(/var REAL=(.*?);/, (match,json) => {
      const seed=JSON.parse(json);seed.preview=true;return 'var REAL='+JSON.stringify(seed)+';';
    }).replace(/<script\b[^>]*\bsrc=[^>]*>[\s\S]*?<\/script>/gi,'')
      .replace(/<link\b[^>]*>/gi,'').replace('</head>','<link rel="icon" href="data:,"></head>');
    await cdp('Page.setDocumentContent', {frameId: rangeTree.frameTree.frame.id, html: rangeHTML});
    for (const [width,height] of [[1440,1000],[820,1180],[390,844]]) {
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
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
      const edit=await evaluate(`(() => {
        const card=document.getElementById('costManage'),btn=document.getElementById('costEditBtn');
        const name=card&&card.querySelector('.grp-name');
        if(!card||!btn||!name)return {exists:false};
        const cb=card.getBoundingClientRect(),b=btn.getBoundingClientRect(),nb=name.getBoundingClientRect();
        const sum=card.querySelector('.cost-sum'),sb=sum&&sum.getBoundingClientRect();
        const grp=card.querySelector('summary.grp');
        return {exists:true,sameRow:Math.abs((b.top+b.bottom)/2-(nb.top+nb.bottom)/2)<=8,
          rightGap:+(cb.right-b.right).toFixed(1),height:+b.height.toFixed(1),
          // 2026-10-27：标题行必须排成一行（跟其它分组卡一样高），
          // 总资产·盈亏不能掉到第二行把卡片撑高；按钮也不许掉行
          headH:+grp.getBoundingClientRect().height.toFixed(1),
          sumSameRow:!!sb&&Math.abs((sb.top+sb.bottom)/2-(nb.top+nb.bottom)/2)<=10,
          fontSize:getComputedStyle(btn).fontSize,radius:getComputedStyle(btn).borderTopLeftRadius,
          overflow:document.documentElement.scrollWidth>innerWidth+1};
      })()`);
      assertions.push({ok:bars.length===2 && bars.every(b=>!b.missing && b.percent===0 && b.gaps===2
          && b.underLo && b.underHi && !b.overlapsTrack && b.markerInTrack && b.markerWidth<=8.6
          // 只收底宽、高度不变（9px）→ 顶角约 50°，比原来的 60° 更尖锐
          && b.markerHeight>=8.6 && Math.round(b.height)===33)
          && bars.some(b=>b.text==='-33.3% / +33.3%') && bars.some(b=>b.text==='-39.0% / +0.6%')
          && edit.exists && edit.sameRow && edit.sumSameRow && edit.headH<=56
          && edit.rightGap<=6 && edit.height>=24 && !edit.overflow
          && edit.radius==='7px',
        name:width+'px52周条两端第二行为到低/高点、三角底收窄高度不变；仓位管理标题行单行、编辑按钮保持原圆角且贴右不错行',actual:{bars,edit}});
    }
    const injectionTree=await cdp('Page.getFrameTree');
    const injectionHTML=input.injectionPage.replace(/<script\b[^>]*\bsrc=[^>]*>[\s\S]*?<\/script>/gi,'')
      .replace(/<link\b[^>]*>/gi,'').replace('</head>','<link rel="icon" href="data:,"></head>');
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
      || url.split(/[?#]/)[0] === refreshBase),
      name:'合成页面不尝试任何云端仓位或外网读写', actual:{requestUrls:[...requestUrls]}});
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
        _browser_result = _subprocess.run(['node', '-e', _browser_script],
            input=_json.dumps({'chrome': _chrome, 'profile': _profile, 'html': _description_page,
                              'colorPage': _color_page, 'rangePage': _range_page,
                              'ciNoSandbox': sys.platform == 'linux' and os.environ.get('GITHUB_ACTIONS') == 'true'
                                  and os.environ.get('MM_TEST_CHROME_NO_SANDBOX') == '1',
                              'injectionPage': _unsafe_page, 'injectionProbe': _unsafe_snapshot['probe']}),
            capture_output=True, text=True, encoding='utf-8', timeout=90)
    check(_browser_result.returncode == 0,
          '独立Chrome样式验证执行成功' + (('：' + _browser_result.stderr) if _browser_result.returncode else ''))
    if _browser_result.returncode == 0:
        for _assertion in _json.loads(_browser_result.stdout):
            check(_assertion['ok'], _assertion['name'] + '：'
                  + _json.dumps(_assertion['actual'], ensure_ascii=False, separators=(',', ':')))

_seed_patch.stop()
print()
if fail:
    print(f"{checks_total} 项检查中 {len(fail)} 项失败"); sys.exit(1)
print(f"全部通过（{checks_total} 项检查）")
