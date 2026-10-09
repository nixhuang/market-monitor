# -*- coding: utf-8 -*-
"""离线渲染测试：不联网，用假数据验证首页 UI 改版与三分组逻辑。"""
import re
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import monitor  # noqa: E402

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
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fail.append(msg)

# 标题与排
check("AI监测市场" in html, "标题 = AI监测市场")
check("市场自检" not in html, "旧标题已移除")
check("数据时间 2026-10-06 收盘（美东交易日）" in html and "本页数据更新" not in html,
      "休市顶部显示行情收盘日，不重复抓取完成时间")
check("抓取成功 · 完成于 2026-10-07 20:40:28（北京时间）" in html, "静态成功灯显示真实完成时间")
check("自动计划：美东周一至五 20:30（北京 " in html, "第二排自动计划")
check(("夏令时次日 08:30" in html) != ("冬令时次日 09:30" in html), "冬夏令时只出现一个")
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
check('.c-sym{width:22%}' in _mob_css and '.c-earn{width:25%}' in _mob_css and '.c-px{width:17%}' in _mob_css, "手机端四栏列宽独立设置")
_macro_u = dict(macro, ust10=dict(macro["ust10"], value=4.55, delta_week=0.12, month_ago=4.0, delta_month=0.55))
_html_u = monitor.render(_macro_u, items, 10, snapshot=snapshot, dup_hidden=2)
check('id="ust10Ref"' in _html_u and "一个月急升 +55 bp" in _html_u and "参考 · 不计入综合灯" in _html_u
      and '<td class="num dim">+12</td>' in _html_u, "10Y美债参考行：一个月急升标黄，周变化按 bp 显示")
check('id="ust10Ref"' in html and "无数据或已过期" in html.split('id="ust10Ref"')[1].split("</tr>")[0],
      "10Y美债缺一个月数据时显示无数据")
_macro_b = dict(macro, breadth={"ok": True, "name": "上涨参与度", "date": "2026-10-06", "value": 47.0,
                               "pct50": 29.3, "unit": "%", "source": "History of Market"})
_html_b = monitor.render(_macro_b, items, 10, snapshot=snapshot, dup_hidden=2)
check("47.0%<span class=\"unit\">的成分股站上200日线（长期趋势）</span>" in _html_b
      and "站上50日线（短期）：29.3%" in _html_b and "站上200日均线 · 50日" not in _html_b, "上涨参与度文案已改写")
check("红2 黄2 绿4" in html and "10 项数据已更新" in html, "运行灯含计数与完整数据总数")
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
check('id="group_positions" open' in html and 'id="group_focus" open' in html, "持仓与重点关注默认展开")
pos_title = html.split('id="group_positions"', 1)[1].split('</summary>', 1)[0]
check('class="stat-dot red"' in pos_title and 'class="stat-dot gray"' in pos_title and
      '红1' not in pos_title and '缺失1' not in pos_title, "分组警示采用圆点与计数而非颜色文字")
check('.group-card>summary .group-stats{margin-left:6px' in html and
      '.group-stats{margin-left:auto' in html, "警示在分组名称右侧左对齐，宏观标题保持原样")
check('id="group_technology"' in html and 'IT软硬Ai (5)' in html, "IT 分类展示计数")
check('其他关注' not in html, "旧其他关注分类已移除")
check("<h2>个股</h2>" not in html and "<h2>ETF 基金</h2>" not in html, "个股与 ETF 不再分区")
check(html.count('<thead><tr><th>名称</th><th class="h-earn">财报</th>') >= 3, "表头：名称｜财报")
check("财报 美东" not in html and "下一次财报" not in html.split("<style>")[-1].split("</style>")[-1], "每行不再重复写「财报」二字")
check('无异动 1 只 · 点击查看' in html and 'SPYM' in html, "无异动标的可展开查看")
pos_card = html.split('id="group_positions"')[1].split('id="group_focus"')[0]
check('NTNX' in pos_card and 'SPYM' in pos_card, "持仓包含警示及无异动标的")
focus_card = html.split('id="group_focus"')[1].split('id="group_index_funds"')[0]
check('TEM' in focus_card and 'SOXL' in focus_card, "重点关注仍包含 TEM/SOXL")
tech_card = html.split('id="group_technology"')[1].split('id="group_healthcare"')[0]
check('MCD' in tech_card and 'XLU' in tech_card and 'PG' in tech_card, "IT 分类包含警示与无异动标的")
check('在册：持仓 2 · 重点关注 2 · IT软硬Ai 6' in html, "在册计数更新")
check('已隐藏 2 只重复标的' in html, "跨组去重提示")
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
check('重点关注 (0)' in html3 and '暂无标的，去设置页录入或导入 CSV' in html3, "重点关注空态提示")

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

print()
if fail:
    print(f"{len(fail)} 项失败"); sys.exit(1)
print("全部通过")
