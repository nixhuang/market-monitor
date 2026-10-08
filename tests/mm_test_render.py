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
check("红2 黄2 绿4" in html and "10 只全部更新" in html, "运行灯含计数与抓取结果")
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
check(html.count("<h2>个股</h2>") == 3 and html.count("<h2>ETF 基金</h2>") == 2, "分类内警示个股与 ETF 分区")
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
check('RSI 6 / 12 / 24' in html5 and '同侧两条黄、三条红' in html5, "页脚说明三线 RSI 分级")
check('逼近、触碰、穿越均为黄' in html5 and '布林信号 + RSI 至少两条同侧达到或越过阈值 → 红' in html5,
      "页脚说明布林独立黄与RSI组合红")
by_sym(items, "NTNX")["fund"]["hits"] = ['利润 < 0 & "下降" <script>alert(1)</script>']
html6 = monitor.render(macro, items, 10, snapshot=snapshot)
check('&lt;script&gt;alert(1)&lt;/script&gt;' in html6 and '<script>alert(1)</script>' not in html6,
      "基本面展开内容保留 HTML 转义")

print()
if fail:
    print(f"{len(fail)} 项失败"); sys.exit(1)
print("全部通过")
