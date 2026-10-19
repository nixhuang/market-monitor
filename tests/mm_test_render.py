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
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fail.append(msg)

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
_near = item("NTNX", "position", "red", chg=5.2); _near["earnings"] = {"status": "ok", "date": "2026-10-20", "timing": "post", "kind": "expected"}
_far = item("TEM", "focus", "red", chg=8.0); _far["earnings"] = {"status": "ok", "date": "2026-12-20", "timing": "pre", "kind": "expected"}
_html_e = monitor.render(macro, [_near, _far] + [d for d in items if d["symbol"] not in ("NTNX", "TEM")], 10, snapshot=snapshot, dup_hidden=2)
check('<span class="earn soon"' in _html_e and _html_e.count('<span class="earn soon"') == 1
      and '<span class="earn">' in _html_e, "两周内的财报标黄（earn soon），更远的保持蓝色")
check(' title="' not in _html_e and " title='" not in _html_e, "首页不含任何 title 悬停提示（手机/iPad 无法悬停）")
check(".earn.soon{color:#f0c674" in _html_e, "财报标黄样式存在")
_mob_css = _html_e.split('@media (max-width:600px){', 1)[1].split('.fund-context{', 1)[0]
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
check("红2 黄2 绿4" not in html and "10 项数据已更新" in html,
      "运行灯不再重复灯级计数，只报完成时间与数据量")
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
check('class="stat-badge red"' in pos_title and 'class="stat-badge gray"' in pos_title and
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
check('<thead' not in html and '<th>名称</th>' not in html and 'class="h-earn"' not in html,
      "表头「名称｜财报」整行已删除（2026-10-15）")
check(monitor.STK_HEAD == '', "STK_HEAD 不再输出表头")
check("财报 美东" not in html and "下一次财报" not in html.split("<style>")[-1].split("</style>")[-1], "每行不再重复写「财报」二字")
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

# ---- 基本面徽章 ----
# render() 会就地 sort items，前面几次 render 已经打乱顺序，必须按代码定位，不能用索引
def by_sym(lst, s):
    return next(d for d in lst if d["symbol"] == s)


for d in items:
    d.pop("fund", None)
by_sym(items, "NTNX")["fund"] = {"level": "red", "hits": ["营收同比 -22%"],
                                 "period": "6/30/2026", "oneoff": False}
by_sym(items, "TEM")["fund"] = {"level": "yellow", "hits": ["营业利率 12.3→8.1%", "流动比率 115→84"],
                                "period": "6/30/2026", "oneoff": False}
by_sym(items, "MCD")["fund"] = {"level": "green", "hits": [], "period": "6/30/2026", "oneoff": True}
# 行情绿灯但基本面亮红的行也必须上表，否则警示永远藏在「无异动 N 只」里
by_sym(items, "JNJ")["fund"] = {"level": "red", "hits": ["经营现金流转负"],
                                "period": "6/30/2026", "oneoff": False}
html5 = monitor.render(macro, items, 10, snapshot=snapshot, dup_hidden=0)
check('<td class="sym">JNJ' in html5, "基本面红的绿灯行也会上表（不再藏在无异动里）")
check("经营现金流转负" in html5, "该行徽章 title 正确")
quiet_nums = re.findall(r"无异动 (\d+) 只", html5)
check("1" in quiet_nums and "2" not in quiet_nums,
      f"无异动计数已扣掉基本面警示的那只：{quiet_nums}")
check(html5.count('summary class="tag f-red"') == 2, "红色基本面可点击标签 2 个")
check(html5.count('summary class="tag f-yellow"') == 1, "黄色基本面可点击标签 1 个")
check('<li>营收同比 -22%</li>' in html5, "红标签展开内容带命中原因")
check('<li>流动比率 115→84</li>' in html5, "黄标签展开内容带命中原因")
check("6/30/2026 报告期" in html5, "展开内容带报告期")
check(html5.count('class="fund-detail"') == 3, "绿色不亮徽章，共 3 个折叠详情")
check('aria-label="查看 JNJ 基本面详情"' in html5, "标签有可访问名称")
check('border-top-color:currentColor' in html5 and 'content:" · 展开"' not in html5,
      "基本面收起时显示向下三角，展开时箭头翻转且不再显示展开文字")
check('鼠标悬停徽章' not in html5, "页脚更新为点击查看说明")
# 基本面不改变原有红黄绿灯：NTNX 本来就是 red，加 fund 不改变行数与灯色
check(html5.count('<tr class="red"') == html.count('<tr class="red"'), "基本面不影响红行数量")
check('data-phase="ok"' in html5, "基本面不影响状态灯（仍为绿）")
check('垃圾债利差' in html5 and '高收益债利差' not in html5 and '跑路价签' not in html5,
      "正式生成首页仅使用垃圾债利差新名称")
footer = html5.split('<div class="foot">', 1)[1].split('</div>', 1)[0]
check('./edit.html?rules=1' in footer and '查看完整规则与阈值' in footer, "页脚指向设置页完整规则")
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
check('RSI 6 / 12 / 24' not in footer and '布林信号 + RSI' not in footer and 'Nasdaq' in footer,
      "页脚仅保留数据来源，不再重复长篇规则")
by_sym(items, "NTNX")["fund"]["hits"] = ['利润 < 0 & "下降" <script>alert(1)</script>']
html6 = monitor.render(macro, items, 10, snapshot=snapshot)
check('&lt;script&gt;alert(1)&lt;/script&gt;' in html6 and '<script>alert(1)</script>' not in html6,
      "基本面展开内容保留 HTML 转义")

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
      and "成功后持久缓存" in _refhtml and "不按24小时重复抓取" in _refhtml
      and "正常缓存24小时" not in _refhtml and "临时决议可能延迟发现" in _refhtml,
      "详情包含目标区间、双时区与按公布时间更新说明")
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

print()
if fail:
    print(f"{len(fail)} 项失败"); sys.exit(1)
print("全部通过")
