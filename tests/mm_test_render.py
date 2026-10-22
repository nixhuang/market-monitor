# -*- coding: utf-8 -*-
"""离线渲染测试：不联网，用假数据验证首页 UI 改版与三分组逻辑。"""
import re
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import monitor  # noqa: E402

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

# 规则样式只输出一次，且仍在仓位CSS后保持最终覆盖优先级。
check(html.count(monitor.RULES_CSS) == 1, "规则CSS整块只输出一份")
_cost_css, _cost_js = monitor._cost_panel_assets(items)
check(html.index(_cost_css) < html.index(monitor.RULES_CSS), "规则CSS仍在仓位CSS之后覆盖")

# 标题与排
check("<title>AI监测</title>" in html and "<h1>AI监测</h1>" in html, "标题 = AI监测")
check("市场自检" not in html, "旧标题已移除")
check("数据时间 2026-10-06 收盘（美东交易日）" in html and "本页数据更新" not in html,
      "休市顶部显示行情收盘日，不重复抓取完成时间")
check("抓取成功 · 完成于 2026-10-07 20:40:28（北京时间）" in html, "静态成功灯显示真实完成时间")
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
    _window_by_link = (_label == '名词解释模板' and '北京今天' in _plain
                       and 'href="#rule-earnings"' in _fragment)
    check(_window_in_rules or _window_by_link,
          _label + ' 保留北京日历红窗正文或指向规则正文的有效链接')

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
check("47.0%<span class=\"unit\">的成分股站上200日线（长期趋势）</span>" in _html_b
      and "站上50日线（短期）：29.3%" in _html_b and "站上200日均线 · 50日" not in _html_b, "上涨参与度文案已改写")
# 2026-10-19：顶栏不再重复念灯级计数（统计卡里已有一份），只保留时间＋数据量；「缺失 N」是告警要留
# 2026-10-19：一切正常时只报完成时间；「N 项数据已更新」只在有缺失/不支持代码时才写
check("红2 黄2 绿4" not in html and "10 项数据已更新" not in html
      and "抓取成功 · 完成于 2026-10-07 20:40:28（北京时间） · 缺失 2" in html,
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
check("抓取成功 · 完成于 2026-10-07 20:40:28（北京时间）" in monitor.render(macro, items, 10, snapshot=snap_utc),
      "UTC抓取完成时间正确转为北京时间，行情时间仍为收盘日")
snap_no_finish = {k:v for k,v in snapshot.items() if k not in ('finished_at', 'finished_at_bj')}
check("完成于 未记录" in monitor.render(macro, items, 10, snapshot=snap_no_finish),
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
footer = html5.split('<div class="foot">', 1)[1].split('</div>', 1)[0]
check('href="#signalRules"' in footer and '查看完整规则与阈值' in footer, "页脚指向独立预警规则")
head5 = html5.split('</head>', 1)[0]
check('rel="icon"' in head5 and './icons/favicon-32.png' in head5, "页头有网页图标（相对路径）")
check('rel="apple-touch-icon" href="./icons/apple-touch-icon.png"' in head5, "页头有 apple-touch-icon")
check('rel="manifest" href="./manifest.webmanifest"' in head5 and 'name="theme-color"' in head5, "页头有 manifest 与 theme-color")
import json as _json, os as _os
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_mf = _json.load(open(_os.path.join(_root, 'manifest.webmanifest'), encoding='utf-8'))
check(all(_os.path.exists(_os.path.join(_root, i['src'])) for i in _mf['icons']), "manifest 里的图标文件都存在")
check(all(_os.path.exists(_os.path.join(_root, 'icons', n)) for n in ('favicon-32.png', 'apple-touch-icon.png')), "页头引用的图标文件存在")
check(sum(_os.path.getsize(_os.path.join(_root, 'icons', n)) for n in _os.listdir(_os.path.join(_root, 'icons'))) < 400 * 1024, "图标总体积小于 400KB")
check('RSI 6 / 12 / 24' not in footer and '布林信号 + RSI' not in footer
      and '行情：' not in footer and '垃圾债利差：' not in footer
      and 'href="#marketRiskRules"' in footer and 'href="#glossary-fetch"' in footer,
      "页脚仅保留三类入口，来源统一收进名词解释和抓取")
_glossary5 = re.search(r'<template id="glossaryTpl">(.*?)</template>', html5, re.S)[1]
check(all(term in _glossary5 for term in ('Yahoo → stooq → Nasdaq', 'BAMLH0A0HYM2',
      'Cboe官方VIX日线优先', '芝加哥联储原始周度数据', 'History of Market')),
      "旧来源迁入新稿且VIX来源与当前实现一致")
# 正式利率决议仅为中性消息，不影响风险灯、个股信号或原油行。
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
_refcard = monitor._reference_card(_refsnap)
check('data-level="neutral" data-status="available"' in _refcard and "维持不变" in _refcard
      and "2026-09-16" in _refcard and "stat-dot" not in _refcard, "决议中性展示动作和日期，不显示风险灯")
check(not any(term in _refhtml for term in ("核心CPI", "SEP", "季度预测", "点阵图", "通胀／利率")), "页面移除CPI与预测依赖及旧标签")
check('id="policyAlertDetails"><summary>美联储利率决议' in _refhtml
      and '<details class="macro-help" id="policyAlertDetails" open' not in _refhtml, "决议来源位于页底并默认折叠")
check("3.50%–3.75%" in _refhtml and "2026-09-17 02:00" in _refhtml
      and "到下次官方公布时点前复用" in _refhtml and 'href="#fetch-reference"' in _refhtml
      and "正常缓存24小时" not in _refhtml and "临时决议可能延迟发现" in _refhtml,
      "详情保留区间与双时区，缓存说明集中到新抓取区块")
check(_refsnap["summary"] == snapshot["summary"] and 'id="oilAlert"' in _refhtml, "原计数不变，WTI保留")
_pending = _copy.deepcopy(_refsnap)
_pending["reference_alerts"]["policy"]["fed"]["fetch_error"] = "模拟解析错误 <script>bad()</script>"
check('data-status="pending"' in monitor._reference_card(_pending) and "2026-09-16" in monitor._reference_card(_pending),
      "失败仍保留旧决议日期并显示待更新")
check("&lt;script&gt;bad()&lt;/script&gt;" in monitor._reference_card(_pending, details_only=True), "错误详情安全转义")
_empty = {"reference_alerts": {"policy": {"fed": {}}}}
check('data-status="unavailable"' in monitor._reference_card(_empty), "首次无数据不伪造消息")
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
    card = page.split('id="policyAlert"', 1)[1].split('</div></div>', 1)[0]
    oil_row = page.split('id="oilAlert"', 1)[1].split('</tr>', 1)[0]
    check(f'data-status="{status}"' in card and 'data-level="neutral"' in card
          and f'data-level="{oil_level}"' in oil_row
          and (status == "available" or "待更新" in card)
          and (oil_level != "gray" or "待更新" in oil_row)
          and 'id="policyAlertDetails"' in page and 'id="group_positions"' in page
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
    check(_fetch_text.call_count == 1 and _old == _before and "维持不变" in _page
          and "3.50%–3.75%" in _page and "2026-09-16" in _page,
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
            check("维持不变" in _page and "3.50%–3.75%" in _page and "2026-09-16" in _page,
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
    return classes[0].split() if len(classes) == 1 else [], body


# 目标/共识30/70：状态与主表折叠分别验证，旧基本面及财报三日回归保持不动。
_target_cases = []
for _symbol, _group, _price, _prev, _state in (
        ('TLOW', 'technology', 129.999, 100, 'low'),
        ('TSPACE', 'technology', 130, 100, 'space'),
        ('TMID', 'materials', 169.999, 100, 'space'),
        ('THIGH', 'healthcare', 170, 100, 'high'),
        ('TOVER', 'energy', 250, 100, 'high'),
        ('THIT', 'position', 101, 100, 'hit'),
        ('TBOTH', 'focus', 170, 180, 'hit')):
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
          '30/70胶囊状态 ' + _row['symbol'] + '=' + _state)
    _stripe = {'space': 'pur-space', 'high': 'pur-high', 'hit': 'pur-hit'}.get(_state)
    check('green' in _classes and (_stripe in _classes if _stripe else 'pur' not in _classes),
          '目标行独立条纹 ' + _row['symbol'] + '=' + (_stripe or '无'))

for _row in _target_rows[-3:]:
    _classes, _fragment = rendered_stock_row(_target_page, _row, False)
    check(_row['level'] in _classes and 'pur' not in _classes,
          _row['symbol'] + '失败不新增紫色条纹或改变灯级')
    check('lvtag tgt bad' in _fragment and '获取失败' in _fragment and '+150%' not in _fragment,
          _row['symbol'] + '失败灰胶囊不展示旧空间')
_tech = _target_page.split('id="group_technology"', 1)[1].split('id="group_energy"', 1)[0]
_tech_main, _tech_quiet = _tech.split('<details class="quiet-list">', 1)
check('id="s-tspace"' in _tech_main and 'id="s-tlow"' in _tech_quiet
      and 'id="s-tfailold"' in _tech_quiet and 'id="s-tfailnew"' in _tech_quiet,
      '跨组空间>=30留主表，低空间及失败不提升可见性')
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
check(_target_panel.count('<ul') == 2 and '□' in _target_panel and '■' in _target_panel
      and '今日跌破共识价 170' in _target_panel and _target_panel.count('href="#s-tboth"') == 2,
      '展开两段方块名单保留具体达到方向及重叠跳转')
check('data-kind="target_purple"' in _target_page and '>目标价</button>' not in _target_page
      and '>共识价</button>' not in _target_page, '底部union按钮保留而目标红黄表行移除')
check('疑似数据异常' not in _target_panel and 'lvtag tgt high' in _target_page,
      '>100只作高空间不复活异常红')
for _state in ('space', 'high', 'hit'):
    check('tr.pur-' + _state + ' td.sig{' in _target_page
          and 'tr.pur-' + _state + '+tr.range52-row>td{' in _target_page,
          _state + '右侧条纹贯通52周行的CSS存在')
check(bool(re.search(r'\.lvtag\.tgt\.bad\{[^}]*color:var\(--dim\)[^}]*border:1px dashed', _target_page))
      and '.lvtag.tgt.low{' in _target_page and '.lvtag.tgt.hit{' in _target_page,
      '失败灰虚线及低空间/达到胶囊CSS存在')

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
check('aria-label="RSI3条超买">▲' in _business_page
      and 'tone-yellow" role="img" aria-label="RSI3条超买"' in _business_page,
      '单独三超买统计方向三角标黄，不跟其他红灯染红')
check('盘中突破布林上轨' in _business_page and '触及布林下轨' in _business_page,
      '双轨保留双方事实但限制后不换边统计')
check('id="' + monitor._stock_anchor_id('QUOTEMISSING') + '"' in _business_page
      and 'id="' + monitor._stock_anchor_id('QUOTE_REFERENCE') + '"' in _business_page
      and all(r['symbol'] not in ('QUOTEMISSING', 'QUOTE_REFERENCE')
              for levels in _business_snap['signal_members'].values() for members in levels.values() for r in members),
      '缺/参考仍在主表，所有指标统计成员均排除')

# 三超卖全覆盖：实际分析→快照→整页，所有分组上/下轨组合红、无轨RSI红。
with _patch.object(monitor, 'global_dca', return_value=None):
    _oversold_items = [row for row, _ in _oversold_rows]
    _oversold_snap = monitor.build_snapshot({}, _oversold_items, {}, group_counts={})
    _oversold_page = monitor.render({}, _oversold_items, len(_oversold_items), snapshot=_oversold_snap)
for _row, _side in _oversold_rows:
    _kind = 'rsi' if _side is None else 'boll_rsi'
    _facts = monitor.classify_technical_facts(_row['group'], 0, 3, _side)
    check(_facts == {'kind': _kind, 'level': 'red', 'foldable': True}
          and _row['signal_marks'] == [(_kind, 'red')],
          _row['symbol'] + '纯事实与生产分析同为独占红灯')
    _classes, _fragment = rendered_stock_row(_oversold_page, _row, False)
    check(_classes == ['red'], _row['symbol'] + '红灯主表可见但不因原红永久保留')
    check(_row['symbol'] in {m['symbol'] for m in _oversold_snap['signal_members'][_kind]['red']},
          _row['symbol'] + '快照统计成员归属正确')
check(_oversold_snap['summary']['red'] == len(_oversold_items)
      and _oversold_snap['summary']['yellow'] == 0 and not _oversold_snap['text_only'],
      '三超卖全组全路径最终红，不进入技术灰名单')
check(_oversold_snap['signal_counts']['boll_rsi']['red'] == 2 * len(monitor.GROUPS)
      and _oversold_snap['signal_counts']['rsi']['red'] == len(monitor.GROUPS)
      and _oversold_snap['signal_counts']['boll']['red'] == 0,
      '有轨三超卖仅组合红，无轨仅RSI红，不重复计布林')

print()
if fail:
    print(f"{checks_total} 项检查中 {len(fail)} 项失败"); sys.exit(1)
print(f"全部通过（{checks_total} 项检查）")
