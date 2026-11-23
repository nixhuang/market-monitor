"""页面静态资源、共用规则模板和可回归的设置页生成片段。"""
import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import re
from urllib.parse import quote

from rules_config import DEFAULT_SETTINGS, browser_schema


RULES_SUMMARY = "预警规则"
GLOSSARY_SUMMARY = "名词解释和抓取"
# 定位 ID 沿用确认后的展示稿；门槛只在渲染时从生效配置替换。
RULE_SECTIONS = (
    ("rule-lamps", r"""line-0||h4|一、报灯规则
lamp-table||table|${lamp_table}
line-35|rule-prices|h5|加仓价／减仓价 须设置有效价位
line-36||p|加仓价 · 所有分组：主表描述区常驻显示「加仓价 N · 需+X%／需-X%」，平时灰字；现价与设置价相差≤3% → 点亮橙色，并列入统计面板「盈亏到价」。
line-38||p|减仓价 · 持仓分组：主表描述区常驻显示「减仓价 N · 需+X%／需-X%」，平时灰字；现价与设置价相差≤3% → 点亮青色，并列入统计面板「盈亏到价」。
line-40||p|减仓价：重点、其他分组不适用。
line-41|rule-cost-loss|h5|持仓盈亏 持仓分组
line-42||p|浮盈或浮亏达到 ±10% → 成本行加淡底色（盈绿、亏红），并列入统计面板「盈亏到价」；只作提示，不再出红灯。
line-46||p|重点、其他分组不适用。
line-47|rule-target-space|h5|目标空间 所有分组
line-48||p|有效空间≥${target_space_pct}% → 紫空心灯；
target-space-positive||p|≥${target_high_space_pct}% → 紫红空心灯。
line-50|rule-target-reached|h5|达到目标／共识价 持仓分组、重点
target-reached-positive||p|当日上穿／跌破、现价高于目标价（超过多少都算）或现价在目标±${target_gap_pct}% → 实心紫灯。
target-mute||p|设置页该股目标价填 0 → 关闭“达到”报灯（空间提醒照旧）；清空即恢复。"""),
    ("rule-text", r"""line-52||h4|二、不报灯且不折叠
line-53|rule-earnings|h5|财报临近 所有分组
line-54||p|美东财报日期按北京时间：前一日／当日／后一日 → 红字；
earnings-near||p|财报前2～${earn_soon_days}天 → 黄字。
earnings-list-note||p|还有 ${earn_far_start}～${earn_list_days}天 → 仅文字，进入统计财报，可折叠。
line-56|rule-quotes|h5|缺行情／参考行情 数据缺失 所有分组
line-57||p|缺行情 → 白灯；参考行情显示参考状态。
line-58||p|两者只保留在主表，不进入“无异动”，不计入统计面板，不因此制造红灯／黄灯警示。"""),
    ("rule-folded", r"""line-64||h4|三、折叠
||h5|目标／共识价
||p|所有分组：
target-inactive||p|目标空间<${target_space_pct}%、未达到目标／共识价、共识获取失败及失败旧价；仅紫灯（空间达标或达到目标价的实心紫）且描述区无其它文字同样折叠。
||h5|财报
||p|所有分组：
earnings-far||p|仅有未来${earn_far_start}～${earn_list_days}天财报名单。"""),
)

# 报灯总表：分组 × 灯级，替代原来按指标逐条罗列的正文。
# 灯级口径：红灯／黄灯／白灯＝有文字、不折叠；白灯（折）＝有文字、折叠；仅文字＝折叠且不计白；
# 无提示＝规则判定后不出声；无效＝该分组下规则不生效。
LAMP_RULE_TABLE = (
    '<table class="rule-table" id="rules-lamp-table">'
    '<colgroup><col class="cat-col"><col class="col-cond"><col span="3"></colgroup>'
    '<thead><tr>'
    '<th scope="col" class="cat-col">分类</th><th scope="col" class="col-cond">条件</th>'
    '<th scope="col">持仓分组</th><th scope="col">重点</th><th scope="col">其他分组</th>'
    '</tr></thead><tbody>'
    '<tr><th scope="row" class="cat-col" rowspan="12">布林+RSI</th>'
    '<td>布林上轨▲ + RSI 3条 ↑超买</td>'
    '<td class="cell-red">红灯</td><td class="cell-red">红灯</td>'
    '<td class="cell-gray">白灯</td></tr>'
    '<tr class="rule-group-end"><td><span class="rule-reverse">布林上轨▲ + RSI 3条 ↓超卖（反）</span></td>'
    '<td class="cell-yellow">黄灯</td><td class="cell-yellow">黄灯</td>'
    '<td class="cell-gray-fold">白灯（折）</td></tr>'
    '<tr><td>布林下轨▼ + RSI 3条 ↓超卖</td>'
    '<td class="cell-red" colspan="3">红灯</td></tr>'
    '<tr class="rule-group-end"><td><span class="rule-reverse">布林下轨▼ + RSI 3条 ↑超买（反）</span></td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr><td>布林上轨▲ + RSI 2条 ↑超买</td>'
    '<td class="cell-yellow">黄灯</td><td class="cell-gray">白灯</td><td>仅文字</td></tr>'
    '<tr class="rule-group-end"><td><span class="rule-reverse">布林上轨▲ + RSI 2条 ↓超卖（反）</span></td>'
    '<td class="cell-yellow">黄灯</td><td class="cell-gray">白灯</td>'
    '<td>仅文字</td></tr>'
    '<tr><td>布林下轨▼ + RSI 2条 ↓超卖</td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr class="rule-group-end"><td><span class="rule-reverse">布林下轨▼ + RSI 2条 ↑超买（反）</span></td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr><td>布林上轨▲ + RSI 1条 ↑超买</td>'
    '<td class="cell-gray">白灯</td><td>仅文字</td><td>无提示</td></tr>'
    '<tr class="rule-group-end"><td><span class="rule-reverse">布林上轨▲ + RSI 1条 ↓超卖（反）</span></td>'
    '<td class="cell-gray">白灯</td><td>仅文字</td><td>无提示</td></tr>'
    '<tr><td>布林下轨▼ + RSI 1条 ↓超卖</td>'
    '<td class="cell-gray" colspan="3">白灯</td></tr>'
    '<tr class="rule-group-end"><td><span class="rule-reverse">布林下轨▼ + RSI 1条 ↑超买（反）</span></td>'
    '<td class="cell-gray" colspan="3">白灯</td></tr>'
    '<tr><th scope="row" class="cat-col" rowspan="6">单独RSI</th>'
    '<td>RSI 3条 ↑超买</td>'
    '<td class="cell-yellow">黄灯</td><td class="cell-gray">白灯</td>'
    '<td class="cell-gray-fold">白灯（折）</td></tr>'
    '<tr class="rule-group-end"><td>RSI 3条 ↓超卖</td>'
    '<td class="cell-red" colspan="3">红灯</td></tr>'
    '<tr><td>RSI 2条 ↑超买</td>'
    '<td class="cell-gray">白灯</td><td>仅文字</td><td>无提示</td></tr>'
    '<tr class="rule-group-end"><td>RSI 2条 ↓超卖</td>'
    '<td class="cell-gray" colspan="3">白灯</td></tr>'
    '<tr><td>RSI 1条 ↑超买</td><td colspan="3">无提示</td></tr>'
    '<tr class="rule-group-end"><td>RSI 1条 ↓超卖</td><td colspan="3">仅文字</td></tr>'
    '<tr><th scope="row" class="cat-col" rowspan="2">单独布林</th>'
    '<td>布林上轨▲ + RSI 0条</td><td class="cell-gray">白灯</td>'
    '<td>无提示</td><td>无提示</td></tr>'
    '<tr class="rule-group-end"><td>布林下轨▼ + RSI 0条</td>'
    '<td class="cell-gray">白灯</td><td class="cell-gray-fold" colspan="2">白灯（折）</td></tr>'
    '<tr><th scope="row" class="cat-col" rowspan="4">价格与量能</th>'
    '<td>涨跌幅 · 自适应</td>'
    '<td colspan="3">红灯／黄灯</td></tr>'
    '<tr><td>52周低位（距低点≤${near_52w_low_pct}%）</td>'
    '<td colspan="3">红灯（新进入／跌破／昨日未知）；黄灯（其余持续低位）</td></tr>'
    '<tr><td>均线（短均线穿越；长均线触及±${ma_long_band_pct}%或穿越）</td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr><td>量比（收盘自适应／盘中分段固定）</td>'
    '<td colspan="3">收盘：达自适应门槛报红／黄；'
    '盘中或分布无效：红≥${vol_ratio_red}×、黄≥${vol_ratio}×'
    '（开盘头两小时按进度放宽）</td></tr>'
    '</tbody></table>')

# 规则正文不再提供内部跳转；方向图标和阈值仍保留。


GLOSSARY_BODY = r"""<div class="rules-body" id="glossaryRulesBody">
<section id="glossary-calculation">
<h4>名词解释与计算</h4>
<h5 id="glossary-chg">涨跌幅 · 计算与阈值</h5>
<p>涨跌幅＝（现价÷昨收−1）×100%；报灯取绝对值。</p>
<p>按这只股票自己的历史日波动分档：&lt;${vol_low_sigma}%低档，&gt;${vol_high_sigma}%高档，其余中档；不足${vol_min_returns}个有效日收益取中档。不是按今天的涨跌选档。</p>
<p>所有分组适用；达到红门槛报红，否则达到黄门槛报黄。</p>
${chg_threshold_table}
<h5 id="glossary-boll">布林逼近 · 自适应</h5>
<p>布林为${boll_n}日、±${boll_k}倍标准差。逼近范围＝历史日波动×${boll_near_factor}（当前${boll_near_band_pct}%设置）；波动越大，范围越宽。关闭自适应或波动无效时固定${boll_near_pct}%。</p>
<p>连续自适应示例，不是固定三档：</p>
<table class="rule-table"><thead><tr><th scope="col">假设平时日波动</th><th scope="col">距上轨／下轨多近算逼近</th></tr></thead>
<tbody><tr><th scope="row">1%</th><td>≤约${boll_example_1}%</td></tr><tr><th scope="row">2%</th><td>≤约${boll_example_2}%</td></tr><tr><th scope="row">3%</th><td>≤约${boll_example_3}%</td></tr></tbody></table>
<p>碰轨为触及，穿轨为越轨。日波动来自历史，不是今天涨跌幅，也不是当天带宽的${boll_near_band_pct}%。</p>
<p id="glossary-boll-dual">双轨命中时，只选带符号距离较小的一轨；越轨距离为负，相等选上轨。布林与RSI的组合、分组限制及文字／报灯条件统一见《预警规则》。</p>
<h5 id="glossary-rsi">RSI周期与条数</h5>
<p>周期${rsi_periods}日；≥${rsi_high}超买，≤${rsi_low}超卖。</p>
<h5 id="glossary-vol">量比 · 自适应／固定切换</h5>
<p>完整日量比＝成交量÷此前${vol_ratio_base}日均量。收盘分布有效、两条自适应参数启用且量比≥1×时，按每只标的自身历史量比连续计算，不套股价波动档；否则用固定门槛。</p>
<p>对数量比的中位数为m、稳健波动尺度为s（1.4826×MAD）；原门槛T＝exp(m＋k×s)，红k＝${vol_ratio_red_z}、黄k＝${vol_ratio_z}。新门槛＝√(T×F)，红F＝${vol_ratio_red}×、黄F＝${vol_ratio}×，在对数空间压缩一半高低差异。达到红门槛优先报红，否则达到黄门槛报黄。</p>
<p>高中低仅为历史量比波动的示意，不是固定三档。假设m＝0、低／中／高的s分别为0.15／0.30／0.50：</p>
${vol_example_table}
<p>表中倍数为假设示例；实际门槛随每只标的自身分布连续变化。k不是均量倍数；有效自适应未命中，不再用固定倍数补灯。</p>
<p>盘中量比＝（当日累计量÷已开盘分钟）÷（${vol_ratio_base}日均量÷全日分钟）。开盘成交偏快、折算值偏高，故盘中门槛按常规交易日等效分钟（已开盘分钟÷当日全日分钟×390）放宽：不足 45／60／90／120 等效分钟约 1.69／1.45／1.30／1.13 倍；半日市按当日实际交易时长折算，之后回到固定门槛（红≥${vol_ratio_red}×、黄≥${vol_ratio}×）；放宽的是门槛，显示仍是原始折算值。</p>
<p>开盘不足${intraday_vol_quiet_min}分钟、非交易时段或盘中值无效时退回最近完整日；收盘分布无效或自适应关闭用固定门槛，完整日量比缺失则不判。</p>
<h5 id="glossary-ma">均线确认 · 短均线／长均线</h5>
<p>短均线（${ma_short_status}）：确认幅度＝历史日波动×0.5，限${ma_confirm_min}%～${ma_confirm_max}%；波动无效取${ma_confirm_default}%。检查近${ma_confirm_lookback}日另一侧记录，历史不足只判简单穿越。</p>
<p>启用后的连续自适应示例：</p>
<table class="rule-table"><thead><tr><th scope="col">假设平时日波动</th><th scope="col">穿越确认幅度</th></tr></thead>
<tbody><tr><th scope="row">1%</th><td>${ma_example_1}%</td></tr><tr><th scope="row">2%</th><td>${ma_example_2}%</td></tr><tr><th scope="row">3%</th><td>${ma_example_3}%</td></tr></tbody></table>
<p>长均线（${ma_long_status}）：触及范围固定±${ma_long_band_pct}%；简单穿越不需额外确认幅度。</p>
<p>周期可在设置页编辑，填0关闭对应均线；两条同周期只计一次，按简单穿越判断。</p>
<h5 id="glossary-target">目标／共识价</h5>
<p>有效手填目标价优先；未手填才用自动平均共识价。空间＝（目标／共识价÷现价−1）×100%。</p>
<p>达到按昨收与现价判断，不用日内高低曾触及代替。失败旧共识不参与，有效手填价不受影响。</p>
<p>空间／达到各自去重、允许重叠；总数取并集，不直接相加。</p>
<h5 id="glossary-earnings">财报与显示</h5>
<p>报灯与名单窗口沿用美东财报日期对比快照北京运行日，盘前／盘后不平移。名单含运行日至+${earn_list_days}天，另留前一日红字；红字保留期结束后才检查下一次。</p>
<p>失败时沿用有效旧日期；“待公布”仅表示本站未取得日期。</p>
<p>显示倒计时以快照北京运行日为基准，不随打开页面日期漂移。已知时段按北京落点计算，盘后顺延一日；时段待定按美东日期计算，未知不猜钟点。有具体时段的财报整条有且只有一次美东日期和一次北京日期：有倒计时时北京日期写在括号里，倒计时隐藏时写在后面的北京说明里；时段待定只写美东日期。财报文字按预警规则变红／黄；无预警时美东时间用蓝色、北京时间（含写北京的倒计时）用灰色。显示换算不改变报灯窗口。</p>
<h5 id="glossary-dedup">标的去重与显示优先级</h5>
<p>显示与报警优先级：市场风险／持仓 → 重点 → 其他。同一实质标的只显示最高级，清单记录不删。</p>
<p>VIX与标普指数仅在市场风险显示。</p>
<p>主表描述按指标级别着色：红字为红灯内容、黄字为黄灯内容、白字为白灯内容；仅文字且不计白灯的内容用灰字。</p>
</section>
<section id="glossary-fetch">
<h4>抓取规则</h4>
<h5 id="fetch-quotes">行情与技术指标：来源</h5>
<p>普通标的收盘日线：${quote_closed_sources}；盘中日线：${quote_intraday_sources}。${quote_closing_patch}指数／期货：Yahoo。布林、RSI、均线由行情计算。</p>
<h5 id="fetch-target">目标／共识价：来源与缓存间隔</h5>
<p>StockAnalysis · S&amp;P Global美元平均目标价；不抓晨星。有效手填价优先，并停止自动抓共识。</p>
<p>所有分组符合条件的美股个股适用；排除ETF名单、指数、期货与伯克希尔。</p>
<table class="rule-table"><thead><tr><th scope="col">缓存状态</th><th scope="col">重查间隔</th></tr></thead>
<tbody><tr><th scope="row">没有缓存</th><td>加入抓取队列</td></tr>
<tr><th scope="row">成功取得共识价</th><td>14个美东自然日；财后刷新可提前</td></tr>
<tr><th scope="row">明确404／零覆盖</th><td>30个美东自然日；财报不提前解除</td></tr>
<tr><th scope="row">连续失败</th><td>依次等1／2／4／7个北京自然日，之后每次7天</td></tr></tbody></table>
<p>北京每天最多12只，失败重试最多3只；每股每天最多1次，请求间隔2秒。</p>
<h5 id="fetch-target-after-earnings">财报后：共识价提前刷新</h5>
<table class="rule-table"><thead><tr><th scope="col">条件／结果</th><th scope="col">处理</th></tr></thead>
<tbody><tr><th scope="row">正常缓存＋已记录财报事件</th><td>财报后2个美东自然日起，尚未完成对应刷新则加入队列；盘前／盘后不平移</td></tr>
<tr><th scope="row">抓取成功</th><td>即使价格没变、来源日期仍早于财报，也算完成，通常再等14天</td></tr>
<tr><th scope="row">抓取失败</th><td>按失败退避与日额度重试；未完成事件继续保留</td></tr></tbody></table>
<p>例：已记录美东20日财报，正常缓存未到期，22日起可提前刷新。</p>
<h5 id="fetch-earnings">财报日期：来源与缓存间隔</h5>
<p>StockAnalysis主源 → Nasdaq补时段、交叉核对 → 均无日期才用Yahoo。已核验官方事件优先，支持官网适配的个股可补查官网。</p>
<p>只抓日期／时段，不抓财报内容或基本面。重查间隔按美东自然日、当前距财报天数选列：</p>
<table class="rule-table"><thead><tr><th scope="col">日期性质</th><th scope="col">距财报≥15天</th><th scope="col">距财报4～14天</th><th scope="col">距财报0～3天</th></tr></thead>
<tbody><tr><th scope="row">供应商确认</th><td>14天</td><td>7天</td><td>3天</td></tr>
<tr><th scope="row">官方核验</th><td>7天</td><td>3天</td><td>3天</td></tr>
<tr><th scope="row">估算日期</th><td>7天</td><td>1天</td><td>1天</td></tr></tbody></table>
<p>未知7天复查，不适用／404为30天。无缓存、日期已过或失败，下次再查；失败保留有效旧日期。</p>
<h5 id="fetch-macro"><span id="dataSourceRules" class="rule-anchor"></span>宏观与补充参考：来源</h5>
<table class="rule-table"><thead><tr><th scope="col">数据</th><th scope="col">来源</th></tr></thead>
<tbody><tr><th scope="row">垃圾债利差</th><td>FRED · BAMLH0A0HYM2</td></tr>
<tr><th scope="row">VIX</th><td>Cboe官方VIX日线优先；官方未更新时，以日期更新的Yahoo ^VIX兜底</td></tr>
<tr><th scope="row">标普500</th><td>Yahoo ^GSPC</td></tr>
<tr><th scope="row">上涨参与度</th><td><a href="https://historyofmarket.com/zh-cn/sp500/sp500-breadth/" rel="noopener noreferrer" target="_blank">History of Market</a>；使用成分股均线参与度，不用ETF价格替代</td></tr>
<tr><th scope="row">金融压力NFCI</th><td>芝加哥联储原始周度数据；过期或不可用时以FRED同一指标同步备用</td></tr>
<tr><th scope="row">10年美债收益率</th><td>Yahoo ^TNX百分比日线；校验标的与收益率序列，不混用债券价格</td></tr>
<tr><th scope="row">WTI原油</th><td>Yahoo NYMEX WTI期货日线；不是实时夜盘报价</td></tr></tbody></table>
<p>来源、观测日期与失效状态看对应数据项；有效期与判定见综合判断与宏观细则。</p>
<h5 id="fetch-reference">补充参考：缓存间隔</h5>
<table class="rule-table"><thead><tr><th scope="col">数据</th><th scope="col">缓存／重试</th></tr></thead>
<tbody><tr><th scope="row">NFCI</th><td>按周度公布时点到期；有旧缓存时，未发布／失败至少隔1小时重试，无有效缓存则下一轮再查</td></tr>
<tr><th scope="row">其他行情型参考</th><td>随行情更新，不用共识价14天缓存</td></tr></tbody></table>
</section>
</div>"""


def rule_flags(s):
    equal = s["ma_short"] == s["ma_long"]
    return {"vol_adaptive": s["vol_ratio_z"] > 0 and s["vol_ratio_red_z"] > 0,
            "boll_adaptive": s["boll_near_band_pct"] > 0,
            "ma_short_on": s["ma_short"] > 0, "ma_long_on": s["ma_long"] > 0,
            "ma_short_distinct": s["ma_short"] > 0 and not equal,
            "ma_long_distinct": s["ma_long"] > 0 and not equal,
            "ma_equal": s["ma_short"] > 0 and equal}


def _text(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return html.escape(str(value), quote=True).replace("&#x27;", "&#39;")


COLORS = {"紫红空心灯": "tone-target-high", "紫空心灯": "lamp-purple",
          "实心紫灯": "lamp-purple", "红灯": "lamp-red", "黄灯": "lamp-yellow",
          "白灯": "nav-gray", "红字": "lamp-red", "黄字": "lamp-yellow",
          "橙色": "nav-orange", "青色": "nav-cyan",
          "持仓分组": "rule-group", "所有分组": "rule-group",
          "其他分组": "rule-group", "重点": "rule-group"}


# 报灯总表自带的字符箭头：布林轨 ▲／▼ 与 RSI ↑／↓ 各用一种颜色，便于区分"轨"与"动能"。
ARROW_COLORS = {"▲": "ar-band-up", "▼": "ar-band-down",
                "↑": "ar-rsi-up", "↓": "ar-rsi-down"}


def _colorize(body):
    """只给纯文本里的灯级／分组词与方向箭头上色，标签与属性原样保留。"""

    def paint(text):
        text = re.sub('|'.join(COLORS),
                      lambda word: f'<span class="{COLORS[word[0]]}">{word[0]}</span>', text)
        return re.sub('|'.join(ARROW_COLORS),
                      lambda word: f'<span class="{ARROW_COLORS[word[0]]}">{word[0]}</span>', text)

    return re.sub(r'<a\b[^>]*>.*?</a>|<[^>]+>|([^<]+)',
                  lambda m: paint(m[1]) if m[1] else m[0], body, flags=re.S)


def _rules_markup(body):
    arrows = {
        "上轨": (18, "M3 3H15 M9 15V3 M5 7L9 3L13 7"),
        "下轨": (18, "M3 15H15 M9 3V15 M5 11L9 15L13 11"),
        "超买": (14, "M7 15V3 M3 7L7 3L11 7"),
        "超卖": (14, "M7 3V15 M3 11L7 15L11 11"),
    }
    aliases = {
        "rule-lamps": "alerts", "rule-text": "display", "rule-folded": "quiet",
        "rule-boll-upper": "upper", "rule-boll-lower": "lower", "rule-vol": "volume",
        "rule-chg": "change", "rule-low52": "low",
        "rule-ma": "ma", "rule-prices": "prices", "rule-cost-loss": "loss",
        "rule-target-space": "space", "rule-target-reached": "reached",
        "rule-earnings": "earnings", "rule-quotes": "missing", "rule-rsi": "single",
        "glossary-calculation": "terms", "glossary-fetch": "fetch",
        "glossary-chg": "change-terms",
        "glossary-boll": "boll-terms",
        "glossary-rsi": "rsi-terms", "glossary-vol": "volume-terms",
        "glossary-ma": "ma-terms", "glossary-target": "target-terms",
        "glossary-earnings": "earnings-terms", "fetch-target": "fetch-consensus",
        "fetch-target-after-earnings": "fetch-after-earnings", "fetch-earnings": "fetch-earnings-cache",
    }
    def arrow(match):
        name = match[0]
        width, path = arrows[name]
        return (f'<span class="signal-term" data-rule-term="{name}">{name}<svg class="dir-arrow" '
                f'viewBox="0 0 {width} 18" width="{width}" height="18" fill="none" '
                'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
                f'stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="{path}"/></svg></span>')

    # 报灯总表自带 ▲▼／↑↓ 字符箭头，不再重复插入方向 SVG。
    blocks = []

    def stash(match):
        blocks.append(match[0])
        return f"\x00{len(blocks) - 1}\x00"

    body = re.sub(r'<table\b[^>]*id="rules-lamp-table"[^>]*>.*?</table>', stash, body, flags=re.S)
    # 属性和弱链接标签不插入 SVG，data-original 保持纯文本。
    body = re.sub(r'<a\b[^>]*>.*?</a>|<[^>]+>|([^<]+)',
                  lambda m: re.sub("上轨|下轨|超买|超卖", arrow, m[1]) if m[1] else m[0],
                  body, flags=re.S)
    body = re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m[1])], body)
    return re.sub(r'(<(?:h5|section) id="([^"]+)"[^>]*>)',
                  lambda m: m[1] + (f'<span id="{aliases[m[2]]}" class="rule-anchor"></span>'
                                    if m[2] in aliases else ""), body)


def _rule_values(settings, constants):
    values = {**DEFAULT_SETTINGS, "rsi_periods": "6／12／24", "vol_ratio_base": 20,
              "intraday_vol_quiet_min": 30, "vol_low_sigma": 1.2, "vol_high_sigma": 2.5,
              "vol_min_returns": 40, "ma_confirm_lookback": 10, "ma_confirm_min": 0.5,
              "ma_confirm_max": 3, "ma_confirm_default": 1, "ma_long_band_pct": 1,
              "boll_sigma_factor": 1.29, "earn_soon_days": 7, "earn_list_days": 15,
              "quote_closed_sources": "Yahoo → stooq → Nasdaq",
              "quote_intraday_sources": "Yahoo → stooq → Nasdaq",
              "quote_closing_patch": "收盘日线缺目标日时，Nasdaq收盘报价可补当日一根，不冒充有效日内高低。",
              **settings, **constants}
    # 兼容 monitor 现有的 ×低 / ×中 / ×高 字符串；新增显式参数优先。
    multipliers = values.get("vol_mult", "×0.6 / ×1 / ×1.4")
    if isinstance(multipliers, str):
        multipliers = dict(zip(("low", "mid", "high"),
                              (float(n) for n in re.findall(r"×\s*([\d.]+)", multipliers))))
    for tier in ("low", "mid", "high"):
        mult = values.get("vol_mult_" + tier, multipliers[tier])
        values["vol_mult_" + tier] = mult
        for kind in ("chg",):
            for level in ("red", "yellow"):
                values[f"{kind}_{tier}_{level}"] = round(values[f"{kind}_{level}"] * mult, 6)
    for period in ("short", "long"):
        n = values["ma_" + period]
        values[f"ma_{period}_status"] = "启用" if n else "关闭"
    values["earn_far_start"] = values["earn_soon_days"] + 1
    factor = values["boll_near_band_pct"] / 100 * 4 * values["boll_sigma_factor"]
    values["boll_near_factor"] = round(factor, 6)
    for sigma in (1, 2, 3):
        values[f"boll_example_{sigma}"] = round(factor * sigma, 2)
        values[f"ma_example_{sigma}"] = min(values["ma_confirm_max"],
            max(values["ma_confirm_min"], 0.5 * sigma))
    return values


def _threshold_table(kind, values):
    rows = []
    for tier, label in (("low", "低"), ("mid", "中"), ("high", "高")):
        rows.append('<tr><th scope="row">' + label + ' ×' + _text(values["vol_mult_" + tier])
                    + '</th><td>≥' + _text(values[f"{kind}_{tier}_yellow"])
                    + '%</td><td>≥' + _text(values[f"{kind}_{tier}_red"]) + '%</td></tr>')
    return (f'<table class="rule-table" id="glossary-{kind}-table"><thead><tr>'
            '<th scope="col">波动档位</th><th scope="col">黄灯</th><th scope="col">红灯</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def _volume_example_table(values):
    """假设分布仅用于解释连续门槛，不参与任何标的的实际判灯。"""
    rows = []
    for label, scale in (("低", 0.15), ("中", 0.30), ("高", 0.50)):
        red = math.sqrt(values["vol_ratio_red"]) * math.exp(values["vol_ratio_red_z"] * scale / 2)
        yellow = math.sqrt(values["vol_ratio"]) * math.exp(values["vol_ratio_z"] * scale / 2)
        rows.append(f'<tr><th scope="row">{label}</th><td>≥约{red:.2f}倍</td>'
                    f'<td>≥约{yellow:.2f}倍</td></tr>')
    return ('<table class="rule-table" id="glossary-vol-table"><thead><tr>'
            '<th scope="col">波动</th><th scope="col">红灯</th><th scope="col">黄灯</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def _rules_source():
    parts = ['<div class="rules-body" id="signalRulesBody">']
    for section, lines in RULE_SECTIONS:
        parts.append(f'<section id="{section}">')
        for line in lines.splitlines():
            ident, heading, tag, text = line.split("|", 3)
            if tag == "table":
                parts.append(f'<div class="rule-table-block" id="nav-{ident}">' + text + '</div>')
                continue
            if not ident:
                cls = "heading" if tag == "h5" else "scope"
                parts.append(f'<{tag} class="rule-layout-{cls}">' + _text(text) + f'</{tag}>')
                continue
            parts.append(f'<div class="rule-entry" id="nav-{ident}" data-original="{_text(text)}">')
            anchor = f' id="{heading}"' if heading else ""
            body = _text(text)
            parts.append(f'<{tag}{anchor} class="rule-original">' + body + f'</{tag}>')
            parts.append('</div>')
        parts.append('</section>')
    parts.append('</div>')
    return _colorize("\n".join(parts))


RULE_BLOCKS = (("", _rules_source()),)


def rules_body(settings, constants):
    values = _rule_values(settings, constants)
    filled = re.sub(r"\$\{(\w+)\}", lambda m: _text(values[m[1]]), LAMP_RULE_TABLE)
    tables = {"lamp_table": _colorize(filled)}
    body = re.sub(r"\$\{(\w+)\}",
                  lambda m: tables[m[1]] if m[1] in tables else _text(values[m[1]]),
                  RULE_BLOCKS[0][1])
    return _rules_markup(body)


def glossary_body(settings, constants):
    values = _rule_values(settings, constants)
    tables = {kind + "_threshold_table": _threshold_table(kind, values) for kind in ("chg",)}
    tables["vol_example_table"] = _volume_example_table(values)
    body = re.sub(r"\$\{(\w+)\}", lambda m: tables[m[1]] if m[1] in tables else _text(values[m[1]]),
                  GLOSSARY_BODY)
    body = re.sub(r'<h5 id="([^"]+)"[^>]*>.*?(?=<h5\b|</section>)',
                  lambda m: f'<div class="glossary-entry" id="glossary-block-{m[1]}">{m[0]}</div>\n',
                  body, flags=re.S)
    return _rules_markup(body)


def rules_template(settings, constants):
    return ('<template id="rulesTpl"><summary>' + RULES_SUMMARY + '</summary>'
            + rules_body(settings, constants) + '</template>'
            + '<template id="glossaryTpl"><summary>' + GLOSSARY_SUMMARY + '</summary>'
            + glossary_body(settings, constants) + '</template>')


def json_script(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def asset_url(base, filename):
    # JS 以 LF 规范化后计算内容 hash，与 Git/部署端保持一致；不受 Windows autocrlf 影响。
    content = (Path(base) / filename).read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(content).hexdigest()[:16]
    return f"./{filename}?v={digest}"


def script_asset(base, filename, attrs=""):
    # attrs 可传 "async" 等：2026-10-31 起首页 run-status.js 用 async —— 它是外链脚本，
    # 同步加载会挡住后面的内联脚本；仓位骨架现已服务端输出，填值/绑定也不应等此请求。
    # 它内部自己处理 DOMContentLoaded，且首页内联脚本不引用 window.MMRunStatus，延后执行安全。
    # 保持 async，不让状态请求延迟 DOMContentLoaded。
    return ('<script src="' + html.escape(asset_url(base, filename), quote=True) + '"'
            + (" " + attrs if attrs else "") + '></script>')


def editor_generated(constants):
    schema = browser_schema()
    return ("/* MM_SCHEMA_BEGIN：由 page_assets 生成，不手改 */\n"
            + "const SET_SCHEMA=" + json_script(schema) + ";\n"
            + "const SET_DEFS=SET_SCHEMA.defaults;\nconst SET_KEYS=Object.keys(SET_DEFS);\n"
            + "const SET_LIMITS=SET_SCHEMA.limits;\n"
            + "/* 规则说明正文不在设置页重算：只展示最近一次构建生成的同一份内容。 */\n"
            + "/* MM_SCHEMA_END */")


def sync_edit_html(source, base, constants):
    """纯文本转换供生成/校验与临时目录测试；不会在 import/render 时改源文件。"""
    pattern = r"/\* MM_SCHEMA_BEGIN[^\n]*\*/.*?/\* MM_SCHEMA_END \*/"
    newline = "\r\n" if "\r\n" in source else "\n"
    generated = editor_generated(constants).replace("\n", newline)
    result, count = re.subn(pattern, lambda _: generated, source, flags=re.S)
    if count != 1:
        raise ValueError("edit.html 必须有且仅有一个 schema 生成区块")
    for name in ("run-status.js",):
        pattern = r'(?<=src=")\./' + re.escape(name) + r'(?:\?v=[^"\s]*)?(?=")'
        result, count = re.subn(pattern, lambda _: asset_url(base, name), result)
        if count != 1:
            raise ValueError(f"edit.html 必须有且仅有一个 {name} 引用")
    # 设置页展示规则说明，样式取自同一份 RULES_CSS，不在 edit.html 里另抄一份。
    pattern = r"/\* MM_RULECSS_BEGIN[^\n]*\*/.*?/\* MM_RULECSS_END \*/"
    generated = ("/* MM_RULECSS_BEGIN：由 page_assets 生成，不手改 */\n"
                 + PAGE_PALETTE + RULES_CSS.rstrip("\n") + "\n/* MM_RULECSS_END */").replace("\n", newline)
    result, count = re.subn(pattern, lambda _: generated, result, flags=re.S)
    if count != 1:
        raise ValueError("edit.html 必须有且仅有一个规则样式生成区块")
    return result


def main():
    parser = argparse.ArgumentParser(description="校验设置页 schema、动态规则和资源内容 hash")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--sync-edit", action="store_true")
    args = parser.parse_args()
    if args.check == args.sync_edit:
        parser.error("请选择 --check 或 --sync-edit")
    base = Path(__file__).resolve().parent
    path = base / "edit.html"
    with path.open(encoding="utf-8", newline="") as stream:
        original = stream.read()
    expected = sync_edit_html(original, base, {})
    if args.check:
        if original != expected:
            raise SystemExit("edit.html 生成片段已过期，请运行 page_assets.py --sync-edit 并提交对应改动")
        print("设置页 schema、规则模板和资源 hash 一致")
    elif original != expected:
        # 修改前再次确认原文件未被另一个编辑者更新，避免覆盖并行改动。
        with path.open(encoding="utf-8", newline="") as stream:
            if stream.read() != original:
                raise SystemExit("edit.html 已被其他编辑者修改，停止同步")
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(expected)
        print("已同步设置页生成片段与内容 hash（未修改用户配置）")


# 原生 dashed 不支持指定间隙；与确认的 DEMO 相同：1px灰线，短线3px、间隔5px。
TARGET_DASH_BORDER = 'data:image/svg+xml,' + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%">'
    '<rect x="0.5" y="0.5" style="width:calc(100% - 1px);height:calc(100% - 1px)" '
    'rx="4" fill="none" stroke="#8b93a1" stroke-width="1" stroke-dasharray="3 5"/>'
    '</svg>', safe='')

# 两档正式页面资源；公共色板也随规则CSS同步至设置页。
PAGE_PALETTE = """:root{
  --bg:#0b0e14;
  --card:#141a24;
  --pos-num-fs:14px;  /* 2026-11-20 用户：仓位栏所有金额／涨跌额／涨跌幅与长短持仓标题「-4.7%」同号。
                          桌面 14px、手机档覆盖 16px；两处共用这一个变量，不会再各写各的导致不齐。 */
  --sym-w:100px;      /* 代码列按用户指的「LMT 那么宽」定死：桌面 100 / 手机 98（代码+中文备注名）。
                         原来是 max-content，各行 40~121px 参差、价格区左边界跟着晃；手机档覆盖 98px。
                         2026-11-13 用户「电脑上 7 个中文字宽、手机上 6 个」：桌面 note 11.5px（单字 12px），
                         100px − 左右各 8px 内距＝84px＝精确 7 字；手机 note 14.5px（单字 15px），
                         98px − 左 6px／右 2px 内距＝90px＝精确 6 字。灰线（td.sym::after，由 --vl-x
                         按 sym 右边界算）随列宽：桌面 100px / 手机 98px；长名照旧 ellipsis。
                         19:35 用户「没让你动代码和中文名的位置啊现在贴到灯条上了」：手机左内距从 2px
                         恢复到 6px（代码/中文名离左侧 2px 灯条重新留 4px），此后左内距不再动。
                         20:51 用户「LMT 六字洛克希德马丁离右灰线还有一点距离，左移一点灰线，
                         把这个宽度空间给到紫灯区」：实测基线文字墨迹→灰线 6px，左移 4px（102→98）
                         后留 2px；同步把右内距 6→2px，note 可见宽仍 90px＝6 字完整不裁切，
                         代码/中文名的左侧位置完全不变。 */
  --tgs-w:104px;      /* 紫灯框宽度：桌面 104px；手机档覆盖 108px（2026-11-13 20:51）。
                         19:35 由 96px 往左加宽 8px 给「共识价 · N月」整组腾地方；20:51 再把代码列
                         左移灰线省下的 4px 转给紫灯区（--sym-w 102→98、--tgs-w 104→108，中间
                         minmax(0,1fr) 价格区净宽不变），四位数共识价（COST/LITE）的「10月」离框右线
                         由 4px 放宽到 7.5px。极端窄屏时 fitTgs() 会在 table 上按实测侵占量临时
                         收窄它（下限 58px），腾给价格区。 */
  --tgs-font:13px;    /* 紫灯框内三列字号（手机档覆盖 16px）。字号恒定：2026-11-13 用户明确「紫灯框里的
                          数字字号不该变小」，极端窄屏只压缩框内留白（列宽 + 左右内距），绝不缩字号。 */
  --tgs-pad-l:2px;    /* 紫灯框左右内距：fitTgs() 在窄屏把这两个归零，腾出 5px 给价格区；
                          改的是框内空白，框线（::before right:0/left:-1px）与数字位置都不受影响。 */
  --tgs-pad-r:3px;
  --px-row-h:21px;    /* 与 --row-height 同高：价格区两行和左侧「代码／中文名」两行逐行同高、同顶层内距。 */
  --px-gap:8px;       /* 下排「日涨跌幅 ↔ 周涨跌幅」的 flex 基础间距；上排现价↔涨跌额
                          已由 gap:0 + .px-amt 的 1ch 接管，手机档只覆盖变量值收紧。 */
  --px-week-gap:6px;  /* 11-17 用户「周涨跌幅和日涨跌幅拉开点距离」：只给 .px-week 的额外
                          左外距，和上面的基础间距叠成一对。单独一个变量，改这一对不会
                          连坐振／量，也不会动上排的 1ch；压缩档沿用同一个变量。 */
  --px-col-gap:12px;  /* 左块与右侧「振／量」的间距；手机档只覆盖变量值收紧。 */
  --lamp-top:8.5px;   /* 左灯条与价格区两侧灰色分隔线共用同一组上下几何（2026-11-13）：
                         竖线不再按代码墨迹动态量高（那会比灯条短一截），直接跟灯条同起同止。
                         2026-11-16 单线定稿：第一排上内距由 10px 收到 3px（手机 5px），整个第一排
                         （代码/价格/紫灯框及数字）上移 7/5px，灯条与三条竖线同步 -7/-5px
                         （桌面 15.5→8.5、手机 13→8），长度 31.5/36 不变，相对中文名墨迹位置不变。
                         这样单线下沿（行顶下 1px）到紫灯框上沿正好 5px。 */
  --lamp-h:31.5px;
  --line:#252a33;
  --text:#e6e8ec;
  --dim:#8b93a1;
  --green:#3fb950;
  --yellow:#d29922;
  --red:#f85149;
  --up:#3fb950;
  --down:#f85149;
  --blue:#2f6bd8;
  --purple:#b084ff;
  --target-high:#dc62b8;
  --signal-bg:#182131;
  --collapse-bg:var(--signal-bg);
  --signal-border:var(--frame-purple);
  --frame-w:1px;
  --head-blue:#bfd5ff;
  --head-blue-on:#8fb8f0;
  --sym-fade:rgba(var(--frame-purple-rgb),.16);     /* 个股首排底色渐变：左端 14% 标题蓝 → 72% 处 5% → 透明。
                           2026-11-16 用户指定起始浓度 20%、只铺代码区；2026-11-17 改为从代码区
                           一路铺进价格区；11-17 傍晚定稿右端在紫灯区左边界截止（见 --sym-fade-w）。随后用户「左边起始处太亮／更透明」：
                           只把起始不透明度 20% → 14%，RGB(143,184,240) 与色相一律不动，
                           是「更透明」不是换成更深的蓝；--sym-fade-mid 的 5% 也不动。
                           载体由 td.sym 换成 tr[data-symbol] 的背景，因为 td 的背景不能画到格子外；
                           背景在内容之下，文字不蒙色。固定蓝，不跟灯级变色。变量化是为了不撞
                           10-28 退役的分组标题高亮字面量（测试禁止那个串回来）。 */
  --sym-fade-mid:rgba(88,166,255,.08);
  --sym-fade-top:5px;      /* 渐变上边缘距「本行上方那条 1px 分隔线」下沿的空隙，两档统一 5px
                             （2026-11-17 用户：原来只有 1px 太贴）。只改背景的 position/size，
                             td.sym 的 padding／line-height／字号一律不动，代码文字高度位置不变。
                             定位区是默认的 padding-box（tr 有 1px border-top），所以
                             background-position:0 本变量 ＝ 行顶 +1px 线 +5px 空隙，正好是线底到渐变顶 5px；
                             别改成 border-box，那会少 1px。下边缘仍贴首排底：高度＝首排高 − 本变量，
                             首排高＝--row-pad-top + 2×--row-height（桌面 45px／手机 47px），
                             故渐变高 40／42px。11-17 上午那版「上下留白等距」的裁切变量已整条退役，
                             连变量名都不留在注释里，避免以后有人照着它改回去。 */
  --sym-fade-w:calc(100% - var(--tgs-w)); /* 渐变右端＝紫灯区左边界。background-size 里的
                                      100% 按 tr 的 padding-box 算＝整行宽，减掉第三列 --tgs-w
                                      （桌面 104 / 手机 108）正好是 td.tgs 左边缘，也就是紫灯框
                                      ::before(left:-1px) 所在那一列的起点；no-repeat，右边不再画。
                                      用同一列变量，六档视口与 fitTgs() 动态收窄的紫灯列都自动跟随；
                                      渐变末点本来就是 transparent，最右 1px 不会给框线染色。
                                      11-17 上午的行宽一半、下午再加固定 30px 两步口径都已退役，
                                      不另开变量（一组参数＝一个变量），background-size 规则行一字不动。 */
  --row-pad-top:3px;     /* 个股第一排（代码/价格/紫灯框）三格共用上内距，手机档覆盖 5px。
                           2026-11-16 单线定稿：第一排整体上移到「线→框顶 5px」；桌面 3 / 手机 5
                           是让 td.tgs 由 stretch 得 45/47px，减框顶 5px 与 bottom -1px 后
                           框高仍是桌面 41px／手机 43px，框内 5/5px 墨迹留白不变、不裁第二行数字。 */
  --lamp-red:#ff8580;
  --lamp-yellow:#f0c674;
  --frame-green:#2c8f3e;
  --frame-purple:#a371f7;      /* 框紫唯一来源；rgb 三元组给半透明场合引用，别处不许再写死 */
  --frame-purple-rgb:163,113,247;
  --frame-yellow:#f0c674;
  --lamp-size:14px;
  --head-inset:64px;
  --row-height:21px;
  --tgt-frame-top:5px;    /* 紫灯框上下框线到数字墨迹都留 5px（2026-11-13）：只动框的 ::before 定位，
                             数字位置不变。2026-11-16 单线定稿：::after 第二条线删除，线组下沿
                             从行顶下 4px 变 1px（只剩 1px border-top）；框顶在行顶下 1px + 本变量，
                             故「线→框顶」＝本变量本身。用户要求上下各 5px，两档统一设 5px：
                             框顶随第一排内容一起上移（桌面 12→5、手机 10→5），5/5px 留白不变。 */
  --tgt-frame-bottom:-1px; /* 空心(space/high)与实心(hit)三种框共用。2026-11-16 三格下内距归零后
                             td.tgs 由 stretch 的 56→49px，框底若仍按 bottom:6px 会随 td 上移 7px、
                             裁掉第二行数字。框底要保持原来的绝对位置（行顶下 50px），bottom 需从
                             6 收进 7 → -1px（越过 td.tgs 底 1px，落进下一排 4px 留白里，不遮文字）。
                             单线定稿第一排上内距 10→3px（手机 5px），td.tgs 由 stretch 自动
                             52→45px（手机 47px），框底随 td 一起上移 7/5px，bottom 仍 -1px，
                             框高仍是桌面 41px／手机 43px，数字与上下框线的墨迹留白原样不变。 */
  --tgs-month-font:13px; /* 「· N月」这一小块的字号：19:35 用户定 14px，20:51 用户「四位数共识价里的
                           月还是压到了紫灯框，把 10月 改为 13px 灰色文字」再收 1px。两档统一，
                           不再用 calc(var(--tgs-font) - 1px) 跟着主字号联动（手机原 15px 会把
                           COST/LITE 的「10月」顶到紫框右线）。一个变量管两档，@media 里不重抄规则。 */
  --tgs-month-gap:2px; /* 分隔符「·」左右各留的等距空白（2026-11-13 19:35 用户「· 要在两个数字中间」）。
                           旧写法把「 · N月」整段塞进同一段文本：span 内的前导空格被行首折叠吞掉，
                           实测价格右缘→· 只有 2px、·→月份却有 1 个空格约 5px，所以看着贴近价格。
                           现在分隔符独立成 .target-sep，左右 margin 都用这一个变量，· 的中心严格落在
                           「共识价右缘 ↔ 月份数字左缘」的中点；压缩态在 table[data-tgs-cut] 上归零。 */
  --section-line:rgba(139,147,161,.32);
  --tgt-solid:color-mix(in srgb,var(--purple) 32%,transparent);
  --tgt-pos:#4dd0e1;
  --tgt-neg:#ff5c8d;
  --tgt-pos-on:#8eecf7;
  --tgt-neg-on:#ffa3c4;
  --orange:#ffa94d;
  --cyan:#4dd0e1;
  --positions-collapse:rgba(46,160,67,.15);
  /* 悬浮 ▲ 内部：2026-11-21 用户「有点亮了，再暗些」——把底色本身压暗一档；
     随后定稿「盖住 70%」：紫、绿两态统一 .7（原来 .55／.5），颜色盖得更满、
     底下表格字不再成片透出，但仍留 30% 半透、不是全实。外框保持分组展开那圈不变。
     再定稿「底色透明度不变、颜色再暗一些」：透明度恒 .7 一个字不动，只把两组
     三元组等比压到原来的 75%（紫 80,50,128→60,38,96；绿 20,70,30→15,53,23），
     三个通道同比例下降，所以色相不变、只是整体更沉，不靠掺黑／混白／改透明度。
     三元组在 :root 单独定义，别处不许写死；固定态保持线上原色不动。 */
  --collapse-float-purple-rgb:60,38,96;
  --collapse-float-green-rgb:15,53,23;
  --collapse-float-purple:rgba(var(--collapse-float-purple-rgb),.7);
  --collapse-float-green:rgba(var(--collapse-float-green-rgb),.7);
  --tag-bg:rgba(107,163,240,.14);
  --tag-border:rgba(107,163,240,.32);
  --quote-bg:rgba(139,147,161,.1);
  --signal-magenta:#ef8de4;
  --signal-collapse:rgba(102,134,198,.6);
  --white:#fff;
  --button-border:#4c6591;
  --list-border:#465e87;
  --stat-rule-soft:#3a4356;
  --range-track:#9fa3aa;
  --range-track-line:#495568; /* 52周价格横条：2026-11-16 与个股双线互换，取原 --stock-divider 值 */
  --count-active:#2f4f86;
  --count-red-border:rgba(255,133,128,.45);
  --count-yellow-border:rgba(240,198,116,.45);
  --count-red-bg:rgba(255,133,128,.16);
  --count-yellow-bg:rgba(240,198,116,.16);
  --count-gray-bg:rgba(201,209,217,.14);
  --count-gray-border:rgba(201,209,217,.45);
  --quiet-border:#2b3546;
  --quiet-bg:#12161f;
  --range-marker-border:#efd9ff;
  --range-marker-fill:#713caf;
  --link-blue:#6ba3f0;
  --footer-link:#8ab4f8;
  --warning-bg:rgba(210,153,34,.12);
  --warning-border:rgba(210,153,34,.35);
  --button-bg:#232833;
  --control-border:#333a47;
  --zone-bg:rgba(255,255,255,.022);
  --quiet-rule:#46525f;
  --status-idle:#4b5563;
  --status-busy-halo:rgba(210,153,34,.18);
  --status-ok-halo:rgba(63,185,80,.18);
  --status-bad-halo:rgba(248,81,73,.18);
  --stock-divider:#495568;
  --stock-pair-line:#6c7484; /* 个股分隔线（11-16 末改回单线 1px；变量名沿用双线时期，不连带改测试）：
                         2026-11-16 先定 HSL 明度 42%（#606876，实得 41.96%），
                         随后用户「分隔线的 HSL 亮度增加 5%」→ 名义 47%（实得 47.06%，比旧色
                         +5.10 个百分点）。做法：取 #606876 的 H218.18°/S10.28% 不动，只把 L 设成
                         47% 反算 RGB 再取整 → #6c7484（108,116,132）；整数 RGB 量化后 H220°/S10%，
                         色相偏 1.8° 肉眼不可辨。独立变量，不连坐 --dim 数字色、--stock-divider
                         设置页行线与 --range-track-line 52 周横条。 */
  --cost-row-bg:rgba(143,184,240,.07);
  --pnl-loss-bg:rgba(248,81,73,.12);
  --pnl-gain-bg:rgba(63,185,80,.12);
  --rules-border:#6b7689;
  --rules-cell-border:#5a6478;
  --rules-group-border:#6f7b91;
  --rules-category-bg:rgba(143,184,240,.04);
  --rsi-up:#3ec9d6;
  --rsi-down:#e061c9;
  --rules-red-bg:rgba(248,81,73,.16);
  --rules-yellow-bg:rgba(210,153,34,.16);
  --rules-gray-bg:rgba(173,181,194,.24);
  --rules-fold-bg:rgba(173,181,194,.09);
  --rules-strong:#e6edf3;
  --rules-copy:#dce1e9;
  --rules-muted:#adb5c2;
  --stock-flash:#2b3f63;
  --status-font-size:13.5px
}
"""

PAGE_CSS = PAGE_PALETTE + """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;padding:env(safe-area-inset-top) 17px 40px 15px}
.wrap{max-width:718px;margin:0 auto}
h1{font-size:19px;margin:18px 0 4px;font-weight:600;text-align:center}
h1.brand{color:var(--frame-purple)}
/* 顶部一排：站名 + 定投靠左、代码定位 FIND 靠右，底边对齐（flex-end） */
/* 2026-11-20 站名 26→32px；行盒锁 39px（＝原 26×1.5），盒顶不动、盒底仍与 FIND 对齐，
   FIND 不下移、整排高度不变。站名改短为 Matrix 并把定投挪到标题右侧后，
   「Matrix + 定投：999 + FIND」三者在手机 440 净宽 398px 内仍零溢出。 */
.brandbar{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin:18px 0 4px}
.brandbar h1.brand{margin:0;font-size:32px;line-height:39px;text-align:left;white-space:nowrap}
/* 2026-11-20 桌面抬头行距收紧：数据时间→按钮 16→8px，与手机档一致；手机档 @media 自带 8px，改这条不影响手机。 */
.sub{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:var(--status-font-size);margin-bottom:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 4px;margin-bottom:14px;overflow:hidden}
/* 父级只留标题内缩，资产行与子框使用完整分组宽度；▲沿用原持仓绿色底。 */
#group_positions,#group_positions[open]{background:var(--card);border:var(--frame-w) solid var(--frame-yellow);box-shadow:none;padding:3px 0 2px;margin-bottom:0;overflow:hidden}
#group_positions>summary{cursor:pointer;margin-left:5px;margin-right:5px}
h2{font-size:13px;color:var(--dim);font-weight:600;margin:10px 12px 8px;letter-spacing:.3px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:9px 10px;border-top:1px solid var(--line);vertical-align:middle}
tr:first-child td{border-top:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.dim{color:var(--dim);font-size:13px}
.sym{font-weight:600}
.note{color:var(--dim);font-weight:400;font-size:12px;margin-left:6px}
.tag{display:inline-block;margin-left:6px;padding:1px 5px;border-radius:4px;font-size:11px;font-weight:600;letter-spacing:.3px;vertical-align:1px;color:var(--head-blue-on);background:var(--tag-bg);border:1px solid var(--tag-border)}
.tag.quote-note{color:var(--dim);background:var(--quote-bg);border-color:var(--line);font-weight:400}
.tnote{color:var(--purple)}
.signal-magenta{color:var(--signal-magenta)}
.signal-red{color:var(--lamp-red)}
.signal-yellow{color:var(--lamp-yellow)}
.signal-cyan{color:var(--tgt-pos)}
.signal-item{color:var(--dim)}
.signal-item[data-signal-level="red"]{color:var(--lamp-red)}
.signal-item[data-signal-level="yellow"]{color:var(--lamp-yellow)}
.signal-item[data-signal-level="gray"]{color:var(--text)}
.signal-item[data-signal-level="purple"]{color:var(--purple)}
.signal-separator{color:var(--dim);margin:0 2px}
.group-bottom{padding:0;margin:0 -4px -6px;border-top:1px solid var(--line);text-align:center;line-height:0}
.collapse-group{width:100%;height:26px;padding:0;font:inherit;color:var(--dim);display:flex;align-items:center;justify-content:center;box-sizing:border-box;background:var(--collapse-bg);border:0;border-radius:6px;cursor:pointer;touch-action:manipulation}
.collapse-group .collapse-icon{font-size:16px;line-height:1}
.collapse-group:hover{color:var(--text);background:var(--collapse-bg)}
.card.group-card[open]>.group-bottom>.collapse-group,.card.group-card[open]>.group-bottom>.collapse-group:hover{background:rgba(var(--frame-purple-rgb),.2)}
.collapse-group:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
.signal-count-toggle:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
#signalCounts{--collapse-bg:var(--signal-collapse);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}
#signalCounts .collapse-group{color:var(--white)}
#signalCounts>summary .count-total{white-space:nowrap;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
/* 统计标题栏的数字也是「分组标题栏上的红黄数字」，同批跟随描述区灯色 */
#signalCounts .count-red{color:var(--lamp-red)}
#signalCounts .count-yellow{color:var(--lamp-yellow)}
#signalCounts .count-earn{color:var(--text)}
#signalCounts .count-all{color:var(--dim)}   /* 总 N：抓取总只数，灰字不抢红黄白紫的注意力 */
#signalCounts>summary .count-all{margin-left:auto}   /* 2026-11-20 用户：总 N 靠右，贴标题栏右缘（group-stats 是占满余宽的 flex，auto 外边距把它推到最右） */
/* 顶部右侧的代码定位：美股代码最长 5 位（GOOGL / BRK-B），输入框按 5 个大写字母墨宽定 74px
   ＝字母约 60px + 左右内距 12px + 边框 2px；字号仍 16px 防止 iOS 聚焦时自动放大页面。 */
.stat-find{display:inline-flex;align-items:center;gap:6px;flex:none;margin-left:auto}
.stat-find input{box-sizing:border-box;width:74px;height:32px;padding:0 6px;font:inherit;font-size:16px;font-weight:600;line-height:30px;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:7px;text-transform:uppercase;text-align:center;outline:none}
.stat-find input::placeholder{color:var(--dim);font-weight:400;text-transform:none}
.stat-find input:focus{border-color:var(--head-blue-on)}
.stat-find input.miss{border-color:var(--lamp-red)}
.stat-find button{box-sizing:border-box;height:32px;padding:0 9px;font-size:13px;font-weight:600;line-height:30px;letter-spacing:.3px;color:var(--head-blue);background:#1e232d;border:1px solid var(--line);border-radius:7px;cursor:pointer;white-space:nowrap}
#signalCounts table{table-layout:fixed}
#signalCounts td{padding:6.25px 5px;font-size:13px;line-height:1.25;overflow-wrap:anywhere;vertical-align:middle}
/* 统计面板每一排统一 51px：胶囊 38 + 上下内边距 12.5 + 合并边线的一半 0.5 = 51；首排没有上边线，用 height 补齐 */
#signalCounts tr:not(.signal-members-row)>td{height:51px}
#signalCounts td.num{text-align:center}
#signalCounts td:first-child{width:26%}
#signalCounts td:first-child:not([colspan]){font-weight:600}
#signalCounts .signal-count-toggle{box-sizing:border-box;height:38px;width:44px;max-width:100%;position:relative;padding:0 17px 0 4px;color:var(--head-blue);font:inherit;font-size:14px;line-height:1;background:transparent;border:1px solid var(--button-border);border-radius:6px;cursor:pointer;white-space:nowrap;display:inline-flex;align-items:center;justify-content:flex-end;touch-action:manipulation}
#signalCounts .signal-count-toggle::after{content:"▾";font-size:16px;line-height:1;position:absolute;right:2px;top:50%;transform:translateY(-50%)}
#signalCounts .signal-count-toggle[aria-expanded="true"]::after{content:"▴"}
#signalCounts .signal-members-row>td{padding:0;width:auto;border-top:0}
#signalCounts .signal-members-panel{padding:10px 12px;border-top:1px solid var(--list-border);text-align:left}
#signalCounts .signal-members-panel[hidden]{display:none}
#signalCounts .signal-members-panel ul{list-style:none;margin:0;padding-left:2px}
#signalCounts .signal-members-panel li{margin:8px 0;overflow-wrap:anywhere}
#signalCounts .signal-member-symbol{font-weight:600}
#signalCounts .signal-name-lamp{content:"";display:inline-block;width:3px;height:12px;border-radius:1px;background:var(--name-lamp,var(--dim));margin-right:6px;vertical-align:-1px}
#signalCounts .signal-members-panel li:not(.signal-member-split)::before{content:"";display:inline-block;width:3px;height:12px;border-radius:1px;background:var(--name-lamp,var(--dim));margin-right:6px;vertical-align:-1px}
#signalCounts .earnings-week li::before{content:"";display:inline-block;width:3px;height:12px;border-radius:1px;background:var(--name-lamp,var(--dim));margin-right:6px;vertical-align:-1px}
#signalCounts .signal-members-panel.tone-red{--name-lamp:var(--red)}
#signalCounts .signal-name-lamp.red{--name-lamp:var(--red)}
#signalCounts .earnings-week li.lamp-red{--name-lamp:var(--red)}
#signalCounts .signal-members-panel.tone-yellow{--name-lamp:var(--yellow)}
#signalCounts .signal-name-lamp.yellow{--name-lamp:var(--yellow)}
#signalCounts .earnings-week li.lamp-yellow{--name-lamp:var(--yellow)}
#signalCounts .signal-name-lamp.green{--name-lamp:var(--green)}
#signalCounts .earnings-week li.lamp-green{--name-lamp:var(--green)}
#signalCounts .text-only .signal-member-list{--name-lamp:var(--range-track)}
#signalCounts .signal-name-lamp.gray{--name-lamp:var(--range-track)}
#signalCounts .signal-name-lamp.unknown{--name-lamp:var(--dim)}
#signalCounts .signal-members-panel li.lamp-red{--name-lamp:var(--red)}
#signalCounts .signal-members-panel li.lamp-yellow{--name-lamp:var(--yellow)}
#signalCounts .signal-members-panel li.lamp-green{--name-lamp:var(--green)}
#signalCounts .signal-members-panel li.lamp-gray{--name-lamp:var(--text)}
#signalCounts .signal-members-panel li.lamp-unknown{--name-lamp:var(--dim)}
#signalCounts .signal-name-lamp.purple{--name-lamp:var(--purple)}
#signalCounts .signal-name-lamp.tone-purple{--name-lamp:var(--purple)}
#signalCounts .signal-name-lamp.tone-target-high{--name-lamp:var(--target-high)}
#signalCounts .signal-members-panel .note{display:inline;margin-left:6px}
#signalCounts .signal-member-direction{color:var(--dim);font-size:12px}
#signalCounts .signal-member-belong{color:var(--dim);font-size:12px}
#signalCounts .signal-member-split{list-style:none;height:0;margin:9px 0;border-top:1px solid var(--list-border)}
#signalCounts .signal-member-metric{margin-left:5px;color:var(--text);font-size:13px}
#signalCounts .signal-members-panel.tone-red .signal-member-metric{color:var(--lamp-red)}
#signalCounts .signal-members-panel.tone-yellow .signal-member-metric{color:var(--lamp-yellow)}
#signalCounts .signal-members-panel.tone-gray .signal-member-metric{color:var(--text)}
#signalCounts .signal-member-block{margin:0 0 4px}
#signalCounts .signal-member-block[hidden]{display:none}
#signalCounts .signal-member-block:last-child{margin-bottom:0}
#signalCounts .signal-member-block h5{margin:0 0 4px;font-size:13px;font-weight:600;color:var(--head-blue);display:flex;align-items:center}
/* 终稿：所有统计清单共用分组标题右侧的淡长线，不只盈亏到价。 */
#signalCounts .signal-member-block h5::after{content:"";flex:1 1 auto;height:1px;margin-left:8px;background:var(--stat-rule-soft,#3a4356)}
#signalCounts .signal-member-block h5:first-letter{letter-spacing:0}
#signalCounts .signal-member-block ul{padding-left:2px}
#signalCounts .signal-member-icon{display:inline-block;margin-left:5px;font-size:var(--lamp-size);font-weight:700;line-height:1;vertical-align:baseline}
#signalCounts .tone-white{color:var(--text)}
#signalCounts .tone-yellow{color:var(--lamp-yellow)}
#signalCounts .tone-red{color:var(--lamp-red)}
#signalCounts .tone-cyan{color:var(--tgt-pos)}
#signalCounts .tone-purple{color:var(--purple)}
#signalCounts .signal-members-panel p{margin:4px 0;color:var(--text)}
#signalCounts .signal-count-toggle[aria-expanded="true"]{background:var(--count-active);border-color:var(--head-blue-on);color:var(--white)}
#signalCounts .signal-count-toggle[data-target$="-red"]{border-color:var(--count-red-border)}
#signalCounts .signal-count-toggle[data-target$="-yellow"]{border-color:var(--count-yellow-border)}
#signalCounts .signal-count-toggle[data-target$="-red"][aria-expanded="true"]{background:var(--count-red-bg)}
#signalCounts .signal-count-toggle[data-target$="-yellow"][aria-expanded="true"]{background:var(--count-yellow-bg)}
#signalCounts .signal-count-toggle[data-target^="signal-members-gray-"][aria-expanded="true"]{background:var(--count-gray-bg);border-color:var(--count-gray-border)}
#signalCounts .signal-member-jump{color:var(--text);text-decoration:none;border-bottom:1px dotted var(--list-border)}
#signalCounts .signal-member-jump:hover{color:var(--head-blue);border-bottom-color:var(--head-blue)}
.signal-member-jump:focus-visible{color:var(--head-blue);border-bottom-color:var(--head-blue)}
#signalCounts .text-only{margin:8px 0 0;border:1px solid var(--quiet-border);border-radius:8px;background:var(--quiet-bg);text-align:left}
#signalCounts .text-only>summary{cursor:pointer;padding:8px 12px;color:var(--text);font-size:13px}
#signalCounts .text-only[open]>summary{border-bottom:1px solid var(--quiet-border)}
#signalCounts .text-only ul{margin:0 0 10px;padding-left:30px}
#signalCounts .text-only li{margin:8px 0;overflow-wrap:anywhere;color:var(--text);font-size:13px}
/* 左侧紫灯/财报是多选按钮，必须盖过后绘制的整行 summary 命中区。 */
#signalCounts .signal-extra-row>.signal-filter-toggle{position:absolute;z-index:1;top:6px;left:5px;width:var(--chip-w);padding:0;justify-content:center;text-align:center;font-weight:600}
#signalCounts .signal-extra-row>.text-only{display:block;margin:0;padding:0;border:0;border-radius:0;background:transparent}
#signalCounts .signal-extra-row>.earnings-week{display:block;margin:0;padding:0;border:0;border-radius:0;background:transparent}
#signalCounts .signal-extra-row>.text-only[open]>summary{border-bottom:0}
#signalCounts .purple-extra .text-only>summary{color:var(--purple)}
#signalCounts .purple-extra .text-only li{margin:8px 0;list-style:none}
#signalCounts .purple-extra .text-only .signal-member-list{margin:0;padding:0 0 8.1px 18px;padding-left:2px}
#signalCounts .purple-extra .target-purple>h5{margin:6px 0 3px;font-size:13px;line-height:1.5}
#signalCounts .purple-extra .target-purple .signal-member-list{padding-bottom:0}
#signalCounts .purple-extra .target-purple li{margin:8px 0}
#signalCounts .target-purple .note{display:inline;margin-left:6px}
#signalCounts .target-purple .signal-member-direction{color:var(--purple)}
/* 空间>=90%：整行仍是普通紫，只有百分比数字加紫红下划线（价格数字不加；2026-11-12 用户） */
#signalCounts .target-purple .target-space-high .signal-member-direction .percent-change{text-decoration:underline;text-decoration-color:var(--target-high);text-decoration-thickness:1.5px;text-underline-offset:3px}
.tone-target-high{color:var(--target-high)}
#signalCounts .tone-target-high{color:var(--target-high)}
#signalCounts .purple-extra .text-only .text-only-kind.tone-target-high{color:var(--target-high)}
#signalCounts .purple-extra .text-only .text-only-kind.target-space-high{color:var(--target-high)}
#signalCounts .purple-extra .text-only>summary.tone-target-high{color:var(--target-high)}
#signalCounts .purple-extra .text-only>summary.target-space-high{color:var(--target-high)}
#signalCounts .signal-member-block h5.tone-target-high{color:var(--target-high)}
#signalCounts .signal-member-block h5.target-space-high{color:var(--target-high)}
#signalCounts .signal-member-block.tone-target-high h5{color:var(--target-high)}
#signalCounts .signal-member-block.target-space-high h5{color:var(--target-high)}
tr[id^="s-"]{scroll-margin-top:64px}
#signalCounts .earnings-week{padding:0 12px;border-top:1px solid var(--list-border);color:var(--text)}
#signalCounts .earnings-week:not([open])>*:not(summary){display:none}
#signalCounts .earnings-week summary{padding:8.1px 0;color:var(--text);font-size:16px;font-weight:400;cursor:pointer;touch-action:manipulation}
#signalCounts .earnings-week summary:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
#signalCounts .earnings-week ul{list-style:none;margin:0;padding:0 0 8.1px 2px}
#signalCounts .earnings-week li{font-size:13px;padding:3.6px 0;overflow-wrap:anywhere}
#signalCounts .earnings-week li+li{border-top:1px solid var(--line)}
#signalCounts .earnings-week .lamp-text{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
#signalCounts .earnings-week-meta.is-red{color:var(--lamp-red);font-weight:400;--earn-bj-color:var(--lamp-red)}
#signalCounts .earnings-week-meta.is-yellow{color:var(--lamp-yellow);font-weight:400;--earn-bj-color:var(--lamp-yellow)}
#signalCounts .earnings-week .note{display:inline;margin-left:6px}
#signalCounts .signal-filter-toggle{box-sizing:border-box;height:38px;max-width:100%;padding:0 7px;font:inherit;font-size:14px;color:var(--text);display:inline-flex;align-items:center;justify-content:center;white-space:normal;background:transparent;border:1px solid var(--button-border);border-radius:6px;cursor:pointer;text-align:center;line-height:1.15;touch-action:manipulation}
/* 指标名称按钮外框（2026-11-15）：字号 15→16px 后给「布林+RSI」这类长名字多留 10px，宽度 74→84px；
   height:38px 不动，表格 51px 行高与 summary min-height:50px 都不跟着变，高度不变。 */
#signalCounts td .signal-filter-toggle{width:84px;padding:0 4px}
#signalCounts .signal-filter-toggle[aria-pressed="true"]{background:var(--blue);border-color:var(--blue);color:var(--white)}
#signalCounts .signal-filter-toggle:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
#signalCounts .signal-filter-clear:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
#signalCounts .signal-filter{padding:8.1px 12px;border-top:1px solid var(--list-border);font-size:13px}
#signalCounts .signal-filter[hidden]{display:none}
#signalCounts .signal-filter-head{display:flex;align-items:center;gap:8px;color:var(--head-blue);font-weight:600}
#signalCounts .signal-filter-clear{margin-left:auto;box-sizing:border-box;height:26px;padding:0 24px;font:inherit;font-weight:400;color:var(--text);background:transparent;border:1px solid var(--button-border);border-radius:6px;cursor:pointer}
#signalCounts .signal-filter p{margin:6px 0 0;color:var(--dim)}
#signalCounts .signal-filter ul{list-style:none;margin:6px 0 0;padding:0}
#signalCounts .signal-filter li{padding:4.5px 0;overflow-wrap:anywhere}
#signalCounts .signal-filter li.has-separator{border-top:1px solid var(--line)}
#signalCounts .signal-filter .note{margin-left:5px}
#signalCounts .signal-filter-detail{display:block;margin-top:2px;color:var(--text)}
#signalCounts .signal-filter-condition[data-signal-level="red"]{color:var(--lamp-red)}
#signalCounts .signal-filter-condition[data-signal-level="yellow"]{color:var(--lamp-yellow)}
#signalCounts .signal-filter-condition[data-signal-level="gray"]{color:var(--text)}
#signalCounts .signal-filter-condition[data-signal-level="purple"]{color:var(--purple)}
#signalCounts .signal-filter-condition[data-signal-level="green"]{color:var(--green)}
#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"][data-signal-level="red"]{color:var(--lamp-red);--earn-bj-color:var(--lamp-red)}
#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"][data-signal-level="yellow"]{color:var(--lamp-yellow);--earn-bj-color:var(--lamp-yellow)}
#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"][data-signal-level="gray"]{color:var(--head-blue-on)}
.range52-rail{position:relative;display:flex;align-items:flex-end;gap:11px;height:26px;padding-bottom:13px;font-variant-numeric:tabular-nums}
.range52-lo{position:relative;flex:none;font-size:10px;line-height:11px;color:var(--dim);white-space:nowrap}
.range52-hi{position:relative;flex:none;font-size:10px;line-height:11px;color:var(--dim);white-space:nowrap}
.range52-track{position:relative;flex:1 1 auto;height:2.1px;background:var(--range-track-line);border-radius:99px}
.range52-gap{position:absolute;top:calc(100% + 2px);left:0;line-height:11px;font-size:10px;color:var(--dim)}
.range52-hi .range52-gap{left:auto;right:0}
.range52-marker{position:absolute;bottom:100%;width:0;height:0;border-bottom:0;border-left:4.2px solid transparent;border-right:4.2px solid transparent;border-top:9px solid var(--range-marker-border);transform:translateX(-50%);z-index:1}
.range52-marker::before{content:"";position:absolute;left:-3.2px;top:-8px;width:0;height:0;border-left:3.2px solid transparent;border-right:3.2px solid transparent;border-top:7px solid var(--range-marker-fill)}
.range52-cost{position:absolute;top:50%;width:2.2px;height:10.5px;border-radius:1px;background:var(--lamp-yellow);transform:translate(-50%,-50%);z-index:2}
.range52-cost.out-lo{left:0;opacity:.85}
.range52-cost.out-hi{left:100%;opacity:.85}
.range52-cost.out-lo::after{content:"";position:absolute;top:50%;transform:translateY(-50%);border-top:3.2px solid transparent;border-bottom:3.2px solid transparent;right:calc(100% + 1px);border-right:4px solid var(--lamp-yellow)}
.range52-cost.out-hi::after{content:"";position:absolute;top:50%;transform:translateY(-50%);border-top:3.2px solid transparent;border-bottom:3.2px solid transparent;left:calc(100% + 1px);border-left:4px solid var(--lamp-yellow)}
.range52-missing{display:block;color:var(--dim);font-size:11px;line-height:15px}
.up{color:var(--up)}
.down{color:var(--down)}
/* 描述/统计里带正负号的百分比数字（+8.3% / -9.8%）：只有数字用涨跌色，文字和灯条仍按条件灯级 */
.num-up{color:var(--up,#3fb950)}
.num-down{color:var(--down,#f85149)}
tr.red td:first-child{box-shadow:inset 4px 0 0 var(--red);padding-left:14px}
tr.yellow td:first-child{box-shadow:inset 4px 0 0 var(--yellow);padding-left:12px}
tr.green td:first-child{box-shadow:inset 2px 0 0 var(--green)}
tr.gray td{color:var(--dim)}
.group-card tr.red td:first-child{box-shadow:inset 4px 0 0 var(--red);padding-left:14px}
.macro-help{margin:8px 12px;color:var(--dim);font-size:13px;line-height:1.7}
.macro-help>summary{cursor:pointer;color:var(--head-blue-on);padding:8px 0;touch-action:manipulation}
.macro-help p{margin:6px 0}
.macro-help>summary:focus-visible{outline:2px solid var(--link-blue);outline-offset:2px}
.page-notes{margin-top:18px}
.page-notes .macro-help{overflow-wrap:anywhere}
#pageNotes>#marketRiskRules{margin-top:0;margin-bottom:0}
#pageNotes>#marketRiskRules>summary{padding:9px 0;line-height:1.5;color:var(--head-blue);font-size:13px;font-weight:600}
.page-notes a{color:var(--head-blue-on)}
.page-notes .macro-help+ .macro-help{border-top:1px solid var(--line);padding-top:4px}
.unit{display:block;font-size:11px;color:var(--dim);font-weight:400}
.foot{color:var(--dim);font-size:12.5px;text-align:center;margin-top:22px;line-height:1.7}
.foot a{color:var(--footer-link);text-decoration:none}
.foot a:hover{text-decoration:underline}
.foot a:focus-visible{text-decoration:underline}
.quiet{color:var(--dim);font-size:13px;padding:8px 12px 12px}
.warn{background:var(--warning-bg);border:1px solid var(--warning-border);color:var(--yellow);border-radius:10px;padding:11px 14px;margin-bottom:14px;font-size:14px}
.acts{display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap;margin-top:16px}
.acts .board-return{display:inline-block;margin:8px;color:var(--link-blue)}
.autoplan{color:var(--dim);font-size:12px;text-align:center;line-height:1.7;margin:2px 0 0}
#runMsg{font-size:13px;color:var(--dim);flex:1;min-width:180px;line-height:1.5;overflow-wrap:anywhere}
#runMsg.ok{color:var(--green)}
#runMsg.err{color:var(--red)}
.runbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.runbar button:not(.cost-edit-btn){padding:6px 13px;font-size:13px;line-height:1.2;align-self:center;box-sizing:border-box;height:34px;color:var(--text);background:var(--button-bg);border:1px solid var(--control-border);border-radius:8px;cursor:pointer;font-family:inherit}
.runbar button:disabled{opacity:.5;cursor:not-allowed}
.runbar button.primary{background:rgba(var(--frame-purple-rgb),.3);border-color:var(--frame-purple);color:var(--white)}
.runbar a.btnlink{padding:6px 12px;font-size:15px;color:var(--text);background:var(--button-bg);border:1px solid var(--control-border);border-radius:8px;text-decoration:none;display:inline-block}
.acts a.btnlink{padding:6px 12px;font-size:15px;color:var(--text);background:var(--button-bg);border:1px solid var(--control-border);border-radius:8px;text-decoration:none;display:inline-block}
/* 2026-11-20 桌面抬头收紧：状态灯上下留白 -4/14 → -6/8（与手机档一致），
   按钮→状态灯 10→8；手机档 @media 已写死 -6/8，改这条对手机零影响。 */
.statusrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:-6px 0 8px;font-size:13.5px;line-height:1.5}
/* 2026-11-20 抬头再排：规则同步、仓位同步压成上下两排，紧贴「编辑」按钮右侧，不再单独占一排。
   两处文案最长 5 字（「规则已生效」「仓位已同步」），同号 var(--status-font-size) 不撑宽按钮排；
   .sync-stack 后代选择器压过 cost_panel.css 里 .cost-state 的 11px 基础值。 */
.sync-stack{display:flex;flex-direction:column;justify-content:center;gap:0;flex:none;min-width:0;margin-left:2px}
.sync-stack .runlight{flex:none;white-space:nowrap;line-height:1.25}
.sync-stack .cost-state{min-width:0;white-space:nowrap;text-align:left;font-size:var(--status-font-size);line-height:1.25}
.sync-stack .cost-state .warn,.sync-stack .cost-state .ok,.sync-stack .cost-state .dim{font-size:inherit}
/* 2026-11-23 用户：「规则已生效」前面的圆灯去掉，让它和下排「仓位已同步」文字左对齐、颜色一致。
   灯只是装饰（状态本来就由文字颜色表达：成功绿／进行中黄／失败红），这里只在抬头两排栈里隐藏，
   .runlight .dot 的规则与别处（页底状态等）不动；隐藏后两排都是 flex 起点，文字天然同一条竖线，
   不需要任何缩进。颜色：已同步用主绿 --green（与上排 ok 态同一个变量），不再用编辑面板那套浅绿
   --cost-ok；「未同步／上传失败」等警示色不变。 */
.sync-stack .rulelight .dot{display:none}
.sync-stack .cost-state .ok{color:var(--green)}
.check-result{padding:10px 12px;margin:0 0 14px;border:1px solid var(--line);border-radius:8px;font-size:13px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text)}
.check-result[data-phase="bad"]{border-color:var(--red)}
.check-result[data-phase="busy"]{border-color:var(--yellow)}
.check-result[hidden]{display:none}
.group-card>summary.grp{display:flex;align-items:center;gap:6px;cursor:pointer;padding:12px 12px 12px 4px;font-size:16px;font-weight:600;list-style:none;touch-action:manipulation}
.macro-card>summary.grp{display:flex;align-items:center;gap:6px;cursor:pointer;padding:12px 12px 12px 4px;font-size:16px;font-weight:600;list-style:none;touch-action:manipulation}
.group-card>summary::-webkit-details-marker{display:none}
.macro-card>summary::-webkit-details-marker{display:none}
.group-card>summary::before{content:"›";display:inline-block;width:10px;text-align:center;flex:none;color:var(--dim);font-size:20px;line-height:1}
.macro-card>summary::before{content:"›";display:inline-block;width:10px;text-align:center;flex:none;color:var(--dim);font-size:20px;line-height:1}
.group-card[open]>summary{border-bottom:1px solid var(--line)}
.macro-card[open]>summary{border-bottom:1px solid var(--line)}
.group-stats{margin-left:auto;color:var(--dim);font-size:14px;font-weight:400}
.group-card>summary .group-stats{margin-left:0;display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-start;text-align:left;flex:1 1 auto}
.group-card>summary .rs-spy,.group-card>summary .pos-total{margin-left:auto}
.zone-card{margin:10px -2px 12px;padding-left:4px;padding-right:4px;border:1px solid var(--line);border-radius:9px;background:var(--zone-bg)}
.zone-card .group-bottom{margin:0 4px 0}
.zone-card>summary{display:flex;align-items:center;gap:8px;cursor:pointer;padding:9px 10px;list-style:none;touch-action:manipulation}
.zone-card>summary::-webkit-details-marker{display:none}
.zone-card>summary::before{content:"›";color:var(--dim);font-size:18px;line-height:1}
.zone-card[open]>summary{border-bottom:1px solid var(--line)}
.zone-card>summary .zone-name{color:var(--text);font-size:15px;font-weight:600;white-space:nowrap;flex:none}
.zone-card>summary .group-stats{margin-left:0;display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-start;text-align:left;flex:1 1 auto}
.zone-card>summary:focus-visible{outline:2px solid var(--link-blue);outline-offset:-2px}
/* 2026-11-20：长短持仓清单搬进 .zone-panel 后不再有 .zone-card 祖先，
   「无异动 N 只」必须继续沿用原 zone-card 内的 12px / 8px 10px 0 / 1.6 行高，
   否则会退回 .quiet 的 13px 与底部 12px 留白，和当日／风险板块不齐。 */
:is(.zone-card,.zone-panel) .quiet{font-size:12px;color:var(--dim);padding:8px 10px 0;line-height:1.6}
.group-card[open]:where(:not(#signalCounts,#costManage)){border:var(--frame-w) solid var(--signal-border)}
.macro-card[open]{border:var(--frame-w) solid var(--signal-border)}
.zone-card[open]{border:1px solid var(--signal-border)}
.group-card>summary:focus:not(:focus-visible){outline:none}
.macro-card>summary:focus:not(:focus-visible){outline:none}
.zone-card>summary:focus:not(:focus-visible){outline:none}
.group-card[open]>summary .grp-name{color:var(--head-blue)}
.macro-card[open]>summary .grp-name{color:var(--head-blue)}
.zone-card[open]>summary .zone-name{color:var(--head-blue)}
.group-card[open]>summary::before{transform:rotate(90deg);color:var(--head-blue-on)}
.macro-card[open]>summary::before{transform:rotate(90deg);color:var(--head-blue-on)}
.zone-card[open]>summary::before{transform:rotate(90deg);color:var(--head-blue-on)}
.zone-card + .group-bottom{margin-top:22px}
.zone-card + .quiet + .group-bottom{margin-top:22px}
/* 所有分组标题的灯数一律不画圈：只留对应等级的颜色数字（2026-11-20 用户定稿）。
   去掉原 24×18 空心圆、描边与固定宽高，数字仍同号不加粗、tabular-nums 对齐。
   白灯/缺失沿用「仅文字灰」，其余各自语义色，颜色只认 :root 色板。 */
.stat-badge{display:inline-flex;align-items:center;justify-content:center;flex:none;box-sizing:border-box;padding:0;border:0;background:transparent;font-size:14px;font-weight:400;line-height:1;font-variant-numeric:tabular-nums;color:var(--white);position:relative;top:2px}
/* 2026-11-21 用户：分组标题栏的红／黄数字＝个股描述区红／黄文字（--lamp-red / --lamp-yellow），
   不再用涨跌色 --red #f85149 / --yellow #d29922——那两个偏暗，和描述区不是一个颜色。 */
.stat-badge.red{color:var(--lamp-red)}
.stat-badge.yellow{color:var(--lamp-yellow)}
.stat-badge.gray{color:var(--dim)}
.stat-badge.missing{color:var(--dim)}
.stat-badge.purple{color:var(--purple)}
.stat-badge.green{color:var(--green)}
.group-card>summary .grp-name{white-space:nowrap;flex:none}
.macro-card>summary .grp-name{white-space:nowrap;flex:none}
.rs-spy{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--dim)}
.pos-total{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--text);font-size:var(--pos-num-fs,14px)}
.rs-spy.up{color:var(--up)}
.rs-spy.down{color:var(--down)}
.stat-dot{width:11px;height:11px;border-radius:50%;display:inline-block;flex:none}
.stat-dot.red{background:var(--red)}
.stat-dot.yellow{background:var(--yellow)}
.stat-dot.gray{background:var(--dim)}
.stat-dot.green{background:var(--green)}
.stat-dot.purple{background:var(--purple)}
/* 11-23 用户：风险标题的红黄数字要紧跟「风险」标题，与其它分组标题同一套排法；
   综合灯仍留在标题栏右端，靠自身 auto 外边距推过去。 */
.macro-card>summary .stat-badge{top:0}
.market-risk-badge{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;margin-left:auto}
.market-risk-badge[data-level="green"]{color:var(--green)}
.macro-card>summary .group-stats{margin-left:0;display:inline-flex;align-items:center;justify-content:flex-start;text-align:left;gap:8px;flex:1 1 auto}
.quiet-list{margin:8px 0;border-top:1px solid var(--quiet-rule);border-bottom:1px solid var(--quiet-rule)}
.quiet-list>summary{padding:8px 12px;color:var(--dim);font-size:13px;cursor:pointer}
.quiet-list[open]>summary{background:none;box-shadow:none;color:var(--head-blue)}
.group-card summary:focus-visible{outline:2px solid var(--link-blue);outline-offset:-2px}
.macro-card summary:focus-visible{outline:2px solid var(--link-blue);outline-offset:-2px}
.runlight{display:flex;align-items:center;gap:7px;font-size:var(--status-font-size);color:var(--dim);line-height:1.4}
.runlight .dot{width:9px;height:9px;border-radius:50%;background:var(--status-idle);flex:none}
.runlight[data-phase="busy"]{color:var(--yellow)}
.runlight[data-phase="partial"]{color:var(--yellow)}
.runlight[data-phase="busy"] .dot{background:var(--yellow);box-shadow:0 0 0 3px var(--status-busy-halo)}
.runlight[data-phase="partial"] .dot{background:var(--yellow);box-shadow:0 0 0 3px var(--status-busy-halo)}
.runlight[data-phase="ok"]{color:var(--green)}
.runlight[data-phase="ok"] .dot{background:var(--green);box-shadow:0 0 0 3px var(--status-ok-halo)}
.runlight[data-phase="bad"]{color:var(--red)}
.runlight[data-phase="bad"] .dot{background:var(--red);box-shadow:0 0 0 3px var(--status-bad-halo)}
/* 2026-11-20：定投从按钮排最右挪到站名右侧；字号沿用桌面 16 / 手机 18，只改位置不改大小。
   底边给 4px，让 18px 小字与 32px 站名的视觉基线对齐（brandbar 是 align-items:flex-end）。 */
.dca-badge{white-space:nowrap;color:var(--text);font:inherit;font-variant-numeric:tabular-nums}
.dca-badge.on{color:var(--yellow)}
.brandbar .dca-badge{font-size:16px;margin-left:10px;margin-bottom:4px}
.oil-reference-row td{font-size:13px;overflow-wrap:anywhere}
.oil-reference-row .num{white-space:nowrap;font-variant-numeric:tabular-nums}
.oil-state{font-size:12px;font-weight:600}
.oil-explanation{margin-top:3px;font-size:12.5px;line-height:1.55}
.oil-reference-row.green .oil-state{color:var(--green)}
.oil-rise{color:var(--up)}
.oil-fall{color:var(--down)}
:is(#macroCard, .macro-scope){--macro-head:14px;--macro-body:15px;--macro-aux:12px;--macro-chg:14px}
:is(#macroCard, .macro-scope) td *{font-size:var(--macro-body)}
:is(#macroCard, .macro-scope) tr.mh td *{font-size:var(--macro-head)}
:is(#macroCard, .macro-scope) td.num.dim .unit{font-size:var(--macro-aux);color:var(--dim)}
:is(#macroCard, .macro-scope) td.num.dim .chg-num{font-size:var(--macro-chg);color:var(--dim)}
:is(#macroCard, .macro-scope) .oil-explanation{font-size:var(--macro-aux)}
#marketRiskReason{font-size:var(--macro-aux)}
:is(#macroCard, .macro-scope) table{table-layout:fixed}
:is(#macroCard, .macro-scope) td:first-child{width:26%}
:is(#macroCard, .macro-scope) td:nth-child(2){width:33%;text-align:center}
:is(#macroCard, .macro-scope) td:nth-child(3){width:18%;text-align:center}
:is(#macroCard, .macro-scope) td:nth-child(4){width:23%;text-align:center}
:is(#macroCard, .macro-scope) tr.mh td:nth-child(3){padding-left:2px}
/* 变化列（11-17）：上排主数值默认白 --text，只有标普500 的点差＋百分比按正负挂
   data-sign 走 --up/--down；垃圾债利差／10Y 是 bp 点差，风险表红绿另有灯义，保持白色。
   11-17 追加：下排「周」「周 bp」及百分比小字保持灰 --dim（用户「变化列下排保持灰色」），
   不跟着上排涨跌色跳；纳指参考行不再有周变化，整格由渲染层留空。
   不看周变化的指标也整格留空（不再写「—」），空白不会被误读成抓取失败。 */
:is(#macroCard, .macro-scope) td.num.dim{font-size:var(--macro-chg);color:var(--text)}
:is(#macroCard, .macro-scope) td.num.dim[data-sign="up"]{color:var(--up)}
:is(#macroCard, .macro-scope) td.num.dim[data-sign="down"]{color:var(--down)}
:is(#macroCard, .macro-scope) tr.mh td{font-size:var(--macro-head);white-space:nowrap;padding:3px 8px}
:is(#macroCard, .macro-scope) td .unit{font-size:var(--macro-aux);line-height:1.15}
:is(#macroCard, .macro-scope) td:nth-child(3) .unit{white-space:nowrap;font-size:var(--macro-aux)}
/* 2026-11-20：风险表小数字统一同号——回撤／短均线的数字与「周 -5.1%」一样 14px，只有标签保持 12px；
   「长均线」标签写到数据前面同一排（行内，不再另起一行）。 */
:is(#macroCard, .macro-scope) td .unit .unit-num{font-size:var(--macro-chg)}
:is(#macroCard, .macro-scope) td .unit.unit-inline{display:inline;margin-right:2px}
/* 2026-11-23 风险表 200 日均线：标普／纳指上排＝相对均线的百分比（涨跌色），
   VIX 上排＝均线数值（白）；下排统一灰字标签，标普／纳指写「200均线偏离」、
   VIX 写「200日均线」。窄屏那条 white-space:normal 会误伤标签，故单独锁住。
   11-23 追加：上排主数字的文字格式与「当前」列点位完全同款（同字号同行高）；
   下排标签整排沿用 .unit 的小灰字，「200」不单独放大、与「均线偏离」同号。
   下排行盒要跟「回撤 -8.2%」那排同高（内含 14px 数字撑到 16.1px），否则两列
   内容高不同，垂直居中会把变化列上排数字整体压低约 2px。 */
:is(#macroCard, .macro-scope) td.num.dim .ma-tag{letter-spacing:-.5px;white-space:nowrap;line-height:calc(var(--macro-chg) * 1.15)}
:is(#macroCard, .macro-scope) td.num.dim .ma-dev,
:is(#macroCard, .macro-scope) td.num.dim .ma-val{display:block;font-size:var(--macro-body);line-height:1.25}
/* 11-23：上涨参与度「当前」列的 200 日均线也拆成上数字／下灰字两排，跟变化列同写法。
   当前列是 td.num（不带 .dim），上排数字沿用本列正文字号与白色，下排标签同 .ma-tag 小灰字。 */
:is(#macroCard, .macro-scope) td.num:not(.dim) .ma-val{display:block;font-size:var(--macro-body);line-height:1.25}
:is(#macroCard, .macro-scope) td.num:not(.dim) .ma-tag{display:block;letter-spacing:-.5px;white-space:nowrap;line-height:calc(var(--macro-chg) * 1.15)}
/* 11-23 10Y 美债变化列：一个月、一周各占一排，「-23 月 bp」「-24 周 bp」——数字白（14px），
   后面紧跟灰色小标签「月 bp」「周 bp」同排；评语列只留评语。
   两排行盒高度与别的两排行（VIX／上涨参与度）完全同款：上排 18.8（数字 14px×1.15 取整后与 .ma-val 同高）、
   下排 16.1，合计 34.9，行高才不会比其它两排行多撑出 2~3px。 */
:is(#macroCard, .macro-scope) td.num.dim .bp-row{display:block;white-space:nowrap;height:18.8px;line-height:18.8px;overflow:visible}
:is(#macroCard, .macro-scope) td.num.dim .bp-row:last-child:not(:only-child){height:16.1px;line-height:16.1px}
:is(#macroCard, .macro-scope) td.num.dim .bp-num{font-size:var(--macro-chg);color:var(--text)}
:is(#macroCard, .macro-scope) td.num.dim .bp-row .unit.bp-unit{display:inline;margin-left:3px;font-size:var(--macro-aux);color:var(--dim);line-height:inherit}
.dual-row > .dual-body{grid-column:1/-1;grid-row:2;min-width:0;margin:0;padding:0;border:0;background:transparent}
.dual-row > .dual-body[hidden]{display:none}
.day-pnl-card > summary.dp-head{display:flex;align-items:center;gap:8px;padding:0 12px 0 4px;list-style:none;cursor:pointer;box-sizing:border-box}
.dp-head::-webkit-details-marker{display:none}
.dp-nums{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;line-height:1.2}
.dp-amt{font-size:15px;font-weight:600}
.dp-pct{font-size:13px;font-weight:400}
.dp-nums .up{color:var(--up)}
.dp-nums .down{color:var(--down)}
/* 2026-11-20 20:4x：只有「当日／风险」的内容区不显示 ▲；长短持仓清单的
   「收起本组」按钮保留（清单已搬进 .zone-panel，见下方 #zoneDualRow 规则）。 */
.dual-row:not(.zone-dual) .group-bottom{display:none}
.dual-row.zone-dual .group-bottom{display:block}
.dual-row tr.mh{display:none}
.dual-row > details{grid-row:1;min-width:0;margin:0;box-sizing:border-box;position:relative;z-index:2}
.dual-row > details[open]{padding-bottom:2px;min-height:63px;border-top:var(--frame-w) solid var(--signal-border);border-left:var(--frame-w) solid var(--signal-border);border-right:var(--frame-w) solid var(--signal-border);border-bottom:0;border-radius:12px 12px 0 0}
.dual-row > .dual-body:not([hidden]){outline-offset:0;position:relative;z-index:1;box-sizing:border-box;padding:0;border:var(--frame-w) solid var(--signal-border);border-top:0;border-radius:0 0 12px 12px;background:var(--card);outline:0}
.dual-row > .dual-body:not([hidden])::before{content:'';position:absolute;top:0;left:0;right:0;z-index:2;height:var(--frame-w);pointer-events:none;background:linear-gradient(to right,
    transparent calc(50% - 4px),
    var(--signal-border) calc(50% - 4px),
    var(--signal-border) calc(50% + 4px),
    transparent calc(50% + 4px))}
.dual-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-items:start;margin:0 0 14px;row-gap:8px;column-gap:8px}
.dual-row:has(> details[open]) > details:not([open]){border-bottom:1px solid var(--line);border-bottom-left-radius:12px;border-bottom-right-radius:12px}
.dual-row > details[open]::after{content:'';position:absolute;left:-1.5px;right:-1.5px;bottom:-8px;height:8px;box-sizing:border-box;z-index:3;pointer-events:none;background:var(--card);border-left:var(--frame-w) solid var(--signal-border);border-right:var(--frame-w) solid var(--signal-border)}
.dual-row:has(> #dayPnlCard[open]) > .dual-body:not([hidden])::before{background:linear-gradient(to right,
    transparent calc(50% - 4px),
    var(--signal-border) calc(50% - 4px))}
.dual-row:has(> :is(#macroCard, .macro-scope)[open]) > .dual-body:not([hidden])::before{background:linear-gradient(to right,
    var(--signal-border) calc(50% + 4px),
    transparent calc(50% + 4px))}
.dual-body > [data-owner][hidden]{display:none}

/* ===== 2026-11-20 长短持仓：与「当日／风险」一模一样的并排双卡 =====
   两枚标题只留 summary，清单放进跨两列的 .dual-body；哪枚展开就显示哪块清单，
   全靠 :has() 由 details.open 驱动，不搬内容、不重算数据；统计跳转只需把对应标题打开。
   20:4x 用户追加：标题字号对齐其它分组（手机更大）、与仓位卡之间留 14px、
   两枚标题与共用清单都用「完整四边框」（收起灰／展开紫，跟其它分组同款，
   不再是被切掉下边框的连通框），清单底部「▲ 收起本组」保留。 */
/* 组名跟个股代码 td.sym 同字号（桌面 14px／手机 17px），其余数字仍按上一轮放大。 */
#zoneDualRow{--collapse-bg:var(--positions-collapse);--zone-name-fs:14px;--zone-badge-fs:14px;--zone-pad-y:2px}
/* 与仓位卡之间留 14px＝普通分组之间的间距；用 class 组合写，避免重复 #zoneDualRow 规则 */
.dual-row.zone-dual{margin-top:14px}
/* 两态同一套 padding／高度链路：展开那枚不再被压矮 5px；框＝完整四边＋12px 圆角 */
/* 21:0x：只有展开的那枚是绿框，收起的回到默认灰框（不带外框色），一眼分辨哪个展开；
   两态共用同一套 padding／min-height／圆角，避免展开时卡片高度跳。清单框恒绿。
   2026-11-23 用户：收起时只留「底边」一条绿线，展开才是完整绿框。做法＝两态边框宽度一致
   （都是 --frame-w），收起态上／左／右三边透明、只给底边上色，所以展开收起卡片高度零跳动；
   不再用灰框 --line。 */
#zoneDualRow > details,#zoneDualRow > details[open]{box-sizing:border-box;min-height:0;padding:var(--zone-pad-y) 4px;border-radius:12px}
#zoneDualRow > details{border:var(--frame-w) solid transparent;border-bottom-color:var(--frame-green)}
#zoneDualRow > details[open]{border:var(--frame-w) solid var(--frame-green)}
#zoneDualRow > details[open]::after{display:none}
/* 绿框左右各外扩 2px、区内补 2px：框线落在持仓框与个股行首灯条正中，不再跟灯条贴住；
   外扩的 4px 正好被 border 2px + padding 4px 吸收，内容宽度不变，表格列宽不动。 */
#zoneDualRow > .dual-body:not([hidden]){margin:0 -2px;padding:0 2px;border:var(--frame-w) solid var(--frame-green);border-radius:12px}
#zoneDualRow > .dual-body:not([hidden])::before{display:none}
#zoneDualRow .group-bottom{margin:0 4px 0}
#zoneDualRow > details > summary.zone-grp{display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:0;box-sizing:border-box;height:47px;min-height:47px;padding:0 12px 0 4px;line-height:1.15}
#zoneDualRow > details > summary.zone-grp::before{display:none}
/* 2026-11-21：两排文字的行距拉开 4px（22+4+20＝46px，仍在固定 47px 标题框内居中）；
   框高、字号、上下内距一律不动。长短持仓与板块分组共用这一条，规格不分裂。 */
.zone-r1{display:flex;align-items:center;gap:5px;min-width:0;white-space:nowrap;line-height:22px;font-size:var(--zone-name-fs);margin-bottom:4px}
.zone-r1 .zone-name{font-size:var(--zone-name-fs);font-weight:600;color:var(--text);flex:none}
.zone-r1 .stat-badge{font-size:var(--zone-badge-fs);top:0}
.zone-r2{display:flex;align-items:baseline;gap:6px;min-width:0;white-space:nowrap;line-height:20px;font-variant-numeric:tabular-nums}
/* 2026-11-20 21:2x：涨跌额不加粗，且与涨跌幅「-4.7%」同号；仓位栏金额也共用 --pos-num-fs。 */
.zone-r2 .zone-amt{font-size:var(--pos-num-fs,14px);font-weight:400;color:var(--text)}
.zone-r2 .zone-pct{font-size:var(--pos-num-fs,14px);color:var(--dim)}
.zone-r2 .zone-amt.up,.zone-r2 .zone-pct.up{color:var(--up)}
.zone-r2 .zone-amt.down,.zone-r2 .zone-pct.down{color:var(--down)}
#zoneDualRow > .dual-body{display:none}
#zoneDualRow:has(> details[open]) > .dual-body{display:block}
#zoneDualRow > .dual-body > .zone-panel{display:none}
#zoneDualRow:has(> #group_positions_long[open]) > .dual-body > [data-owner="long"],
#zoneDualRow:has(> #group_positions_short[open]) > .dual-body > [data-owner="short"]{display:block}
#zoneDualRow:has(> details:only-of-type) > details{grid-column:1/-1}

/* ===== 2026-11-20 23:xx 其它分组两两并排，规格与长短持仓完全一致 =====
   与 #zoneDualRow 平行的一套规则：选择器换成 .group-dual，不动长短持仓那条，
   两边既有的守门断言都不受影响；尺寸／字号／框色一律复用同一批变量，不另抄数值。
   清单显隐按「第几枚标题」对应「第几块面板」，纯 :has() 驱动，不为每个分组写一条。 */
/* 清单底部 ▲ 的底色沿用线上固定态：其它分组紫 rgba(--frame-purple-rgb,.2)，
   长短持仓绿 --positions-collapse。此前这里误写成绿，两列分组的 ▲ 跟着持仓变绿了。 */
.dual-row.group-dual{--zone-name-fs:14px;--zone-badge-fs:14px;--zone-pad-y:2px;--collapse-bg:rgba(var(--frame-purple-rgb),.2)}
.dual-row.group-dual > details,.dual-row.group-dual > details[open]{box-sizing:border-box;min-height:0;padding:var(--zone-pad-y) 4px;border-radius:12px}
.dual-row.group-dual > details{border:1px solid var(--line)}
/* 2026-11-21 用户：其它分组的外框回到原来的紫（--signal-border＝--frame-purple），
   绿框只留给仓位／长短持仓；收起态灰框不变。 */
.dual-row.group-dual > details[open]{border:var(--frame-w) solid var(--signal-border)}
.dual-row.group-dual > details[open]::after{display:none}
.dual-row.group-dual > details > summary.zone-grp{display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:0;box-sizing:border-box;height:47px;min-height:47px;padding:0 12px 0 4px;line-height:1.15;list-style:none;cursor:pointer;touch-action:manipulation}
.dual-row.group-dual > details > summary.zone-grp::before{display:none}
.dual-row.group-dual > details[open] > summary .zone-name{color:var(--head-blue)}
.dual-row.group-dual > .dual-body{display:none}
.dual-row.group-dual:has(> details[open]) > .dual-body{display:block}
.dual-row.group-dual > .dual-body:not([hidden]){margin:0 -2px;padding:0 2px;border:var(--frame-w) solid var(--signal-border);border-radius:12px}
.dual-row.group-dual > .dual-body:not([hidden])::before{display:none}
.dual-row.group-dual .group-bottom{margin:0 4px 0}
.dual-row.group-dual > .dual-body > .group-panel{display:none}
.dual-row.group-dual:has(> details:nth-child(1)[open]) > .dual-body > .group-panel:nth-child(1),
.dual-row.group-dual:has(> details:nth-child(2)[open]) > .dual-body > .group-panel:nth-child(2){display:block}
/* 房地产只有一枚标题，按用户要求仍占左半；清单跨两列，表格列宽不被压窄 */
.dual-row.group-dual.solo-left > details{grid-column:1}
/* 下排「20日对.SPX ±x%」与长短持仓第二排同号；没有相对强弱的分组留足行高对齐 */
.dual-row.group-dual .zone-r2{min-height:20px}

/* ===== 2026-11-21 向上折叠「▲」：长清单够不着时悬浮，滚到分组底部就回原位 =====
   平时它就是清单最底下那条普通的 ▲（跟着分组一起滚）；只有清单比一屏还长、
   ▲ 还没进视口时，JS 才给按钮加 .is-floating 浮到屏幕底部中间，随时可点；
   滚到分组底部、▲ 自己露出来了，立刻去掉这个类还原成原来那条。
   固定的是按钮本身，.group-bottom 用 min-height 继续占位，文档高度不变、回位零跳动；
   仓位和它上面的统计／当日／风险不参与（选择器不覆盖，仓位的 ▲ 本来就隐藏）。
   按钮 DOM 与 aria-controls 一个字不改，点击仍走原有的「只收本组 + 下方内容原地不动」逻辑；
   一次只会有一个这类清单展开（互斥规则），所以不会出现两枚 ▲ 叠在一起。 */
#zoneDualRow .group-bottom,
.dual-row.group-dual .group-bottom{min-height:26px}
/* 悬浮按钮宽度＝清单底部那条 ▲ 的宽度：固定态是 width:100%（即恒占位的 .group-bottom 宽度），
   但 position:fixed 后 100% 会变成整个视口宽，所以由 JS 把量到的宽度写进 --collapse-float-w；
   96px 只是脚本未执行时的兜底，正常永远走变量。box-sizing:border-box 保证描边不占额外宽度。
   2026-11-21 用户「上边缘不变、下边缘往下、加高」：bottom 12→4／height 34→42，两者之和仍是
   46px，所以按钮顶边恒在 vh-46 不动，只是往下长 8px；JS 的「底部 ▲ 还没进视口」临界 vh-46
   因此不用改。追加「再暗些、描边跟展开外框一致」：描边改成分组展开那一圈的完全同款
   （同一个变量、同一条线宽、实色不带透明度），不再用更粗更亮的一圈；固定态保留线上原色
   （紫 .2／绿 .15），悬浮态换成压暗一档但更实心的底色（暗 ≠ 更透），hover 沿用同一色，不混白。 */
#zoneDualRow .collapse-group.is-floating,
.dual-row.group-dual .collapse-group.is-floating{position:fixed;left:50%;bottom:calc(4px + env(safe-area-inset-bottom,0px));z-index:60;width:var(--collapse-float-w,96px);height:42px;margin:0;transform:translateX(-50%);border-radius:10px;color:var(--text);background:var(--collapse-float-bg,var(--collapse-bg));box-shadow:0 4px 16px rgba(0,0,0,.65)}
#zoneDualRow .collapse-group.is-floating{--collapse-float-bg:var(--collapse-float-green);border:var(--frame-w) solid var(--frame-green)}
.dual-row.group-dual .collapse-group.is-floating{--collapse-float-bg:var(--collapse-float-purple);border:var(--frame-w) solid var(--signal-border)}
#zoneDualRow .collapse-group.is-floating:hover,
.dual-row.group-dual .collapse-group.is-floating:hover{color:var(--text);background:var(--collapse-float-bg,var(--collapse-bg))}
.dual-row.group-dual .zone-r2 .rs-spy,.dual-row.group-dual .zone-r2 .monitor-note{font-size:var(--pos-num-fs,14px);line-height:20px;margin-left:0}
.dp-head .dp-nums{margin-left:auto;display:flex;flex-direction:row;align-items:baseline;gap:6px;line-height:1.2;white-space:nowrap}
:is(#macroCard, .macro-scope) td{font-size:var(--macro-body);overflow-wrap:anywhere;padding:4px 8px;line-height:1.25;padding-top:3px;padding-bottom:3px}
:is(#macroCard, .macro-scope) tr:is(.red,.yellow,.green) td:first-child{box-shadow:none;position:relative;padding-left:14px}
/* 2026-11-21 用户：风险指标名左边灯条与主表个股代码左边灯条各加宽 2px
   （风险 1.5/1 → 3.5/3，主表 2 → 4）。left 与 padding-left 不动，加宽后仍在文字左侧留白里。 */
:is(#macroCard, .macro-scope) tr:is(.red,.yellow,.green) td:first-child::before{content:'';position:absolute;left:6px;top:3px;bottom:3px;width:3.5px;border-radius:1px;background:var(--text);pointer-events:none;z-index:1}
:is(#macroCard, .macro-scope) tr.red td:first-child::before{background:var(--red);width:3.5px}
:is(#macroCard, .macro-scope) tr.yellow td:first-child::before{background:var(--yellow);width:3.5px}
:is(#macroCard, .macro-scope) tr.green td:first-child::before{background:var(--green);width:3px}
/* 2026-11-21 用户：风险表「状态」列文字跟本行灯级走，颜色与个股描述红／黄同款；
   绿灯行仍白字、无数据行仍灰字，油价行的 .oil-state 自带同级颜色不冲突。 */
:is(#macroCard, .macro-scope) tr.red td:nth-child(4){color:var(--lamp-red)}
:is(#macroCard, .macro-scope) tr.yellow td:nth-child(4){color:var(--lamp-yellow)}
.oil-reference-row.red .oil-state{color:var(--lamp-red)}
.oil-reference-row.yellow .oil-state{color:var(--lamp-yellow)}
.market-risk-badge[data-level="red"]{color:var(--lamp-red)}
.market-risk-badge[data-level="yellow"]{color:var(--lamp-yellow)}
.dp-head .dp-amt{font-size:17px}
.dp-head .dp-pct{font-size:17px}
#signalCounts .signal-extra-row.pnl-extra>.pnl-chip{position:absolute;top:6px;left:5px;width:var(--chip-w);box-sizing:border-box;height:38px;max-width:100%;padding:0 4px;display:inline-flex;align-items:center;justify-content:center;text-align:center;font:inherit;font-size:14px;font-weight:600;line-height:1.15;color:var(--text);background:transparent;border:1px solid var(--button-border);border-radius:6px;cursor:default;white-space:normal}
#signalCounts .pnl-extra .text-only ul.signal-member-list{margin:0 0 10px;padding-left:2px;list-style:none}
#signalCounts .pnl-extra .text-only ul.signal-member-list>li{list-style:none}
#signalCounts .pnl-extra .text-only ul.signal-member-list>li::before{content:"";display:inline-block;width:3px;height:12px;border-radius:1px;background:var(--name-lamp,var(--dim));margin-right:6px;vertical-align:-1px}
#signalCounts .pnl-extra li.lamp-red{--name-lamp:var(--red)}
#signalCounts .pnl-extra li.lamp-yellow{--name-lamp:var(--yellow)}
#signalCounts .pnl-extra li.lamp-green{--name-lamp:var(--green)}
#signalCounts .pnl-extra li.lamp-gray{--name-lamp:var(--text)}
#signalCounts .pnl-extra .signal-member-metric.up{color:var(--up)}
#signalCounts .pnl-extra .signal-member-metric.down{color:var(--down)}
#signalCounts .pnl-extra .signal-member-metric.add{color:var(--orange)}
#signalCounts .pnl-extra .signal-member-metric.cut{color:var(--cyan)}
#signalCounts .earnings-week-bj{color:var(--earn-bj-color,var(--dim));font-size:inherit;font-weight:400}
table.stk{display:block;width:100%;table-layout:fixed}
table.stk tbody{display:block;width:100%;table-layout:fixed}
table.stk thead{display:block;width:100%;table-layout:fixed}
/* 2026-11-16：--sig-row-gap 提到行级，一处管两个间距 —— 描述区不同指标之间（.sig-col 的 gap）、
   以及「代码/价格/紫灯两排」下沿到紧接的下一排内容（成本条或描述区首行）。用户要求两者相等，
   所以三格下内距归零，间距全部由下一排的上留白引用本变量承担，改一处两处同步。
   值 4px 沿用 11-15 定稿的不同指标间距，不再另开变量。 */
/* 首排底色渐变（2026-11-17）：载体放在整行 tr 的背景上，因为 td 的背景会被裁在各自格子内，
   没法从代码列一路铺进价格区。背景天生在内容之下，不会给文字蒙色；只铺一次（no-repeat），
   宽 --sym-fade-w＝整行宽 − 紫灯列宽（右端截止在紫灯区左边界），高＝首排高 − --sym-fade-top，起点 0/--sym-fade-top：
   上边缘距上方 1px 分隔线 5px，下边缘仍贴首排底（成本条那一排不吃渐变）。
   首排高＝--row-pad-top + 2×--row-height（桌面 45／手机 47），两档由变量自动算，不写字面量。 */
table.stk tr[data-symbol]{position:relative;display:grid;grid-template-columns:var(--sym-w) minmax(0,1fr) var(--tgs-w);grid-template-areas:"sym px tgs" "cost cost cost" "sig sig sig";align-items:stretch;width:100%;border-top:1px solid var(--stock-pair-line);--lamp-line:var(--line);--sig-row-gap:4px;background-image:linear-gradient(90deg,var(--sym-fade),var(--sym-fade-mid) 72%,transparent);background-repeat:no-repeat;background-size:var(--sym-fade-w) calc(var(--row-pad-top) + var(--row-height) + var(--row-height) - var(--sym-fade-top));background-position:0 var(--sym-fade-top)}
table.stk tr[data-symbol]:first-child{border-top:0}
/* 2026-11-16 单线定稿：个股之间只画 1px border-top 一条线，::after 第二条线整条退役
   （不留 display:none 的死代码）。上方 5px＝上一行 range52 的 padding-bottom 2px
   + 本行 margin-top 3px；下方 5px＝--tgt-frame-top 5px（单线下沿在行顶下 1px，
   框顶在 1px+5px，正好 5px）。紧贴线的上一行大多是 52 周条（tr.range52-row），
   所以上方那 2px 只能加在 range52 的 padding-bottom，不能加在 td.sig 底部
   （那样会把描述和 52 周条一起往下推，距离不变还白白多出 3px 行高）。
   首行本来就没有 border-top，单线也一并跳过。颜色仍走 --stock-pair-line（11-16 先定
   HSL 明度 42% → #606876，随后 +5 个百分点到 47% → #6c7484；只提明度，色相/饱和度不动）。 */
table.stk tr[data-symbol]:not(:first-child){margin-top:3px}
table.stk tr[data-symbol]>td{border:0;box-shadow:none;min-width:0;margin:0}
/* 终稿首格清影：保留:first-child特异性，压过旧红行inset而不改变行伪元素灯条。 */
table.stk tr[data-symbol]>td:first-child{box-shadow:none}
table.stk tr.red[data-symbol]{--lamp-line:var(--red)}
table.stk tr.yellow[data-symbol]{--lamp-line:var(--yellow)}
table.stk tr.green[data-symbol]{--lamp-line:var(--green)}
table.stk tr.gray[data-symbol]{--lamp-line:var(--text)}
/* 左灯条下端在「中文备注名」文字下沿之上再收 3px（用户 11-12 定稿），上端保持原值不动：
   CDP 440/744/956/1440 实测中文名墨迹下沿 46（手机）/ 44（桌面）相对行顶，
   对齐值原为手机 39=46-7、桌面 34.5=44-9.5，再各减 3 → 36 / 31.5。
   原 41.7/42.7 会多探出 7.2 / 3.7px。
   2026-11-13：三处分区竖线改用同一组 --lamp-top/--lamp-h，长度与灯条一样，不再各量各的。 */
/* 2026-11-21 用户：个股代码左边灯条加宽 2px（2 → 4），上下几何与颜色一律不动。 */
table.stk tr[data-symbol]::before{content:"";position:absolute;left:0;top:var(--lamp-top);width:4px;height:var(--lamp-h);background:var(--lamp-line);pointer-events:none;z-index:2}
/* 代码格（2026-11-17）：底色渐变已整块挪到 tr 上（见上面 tr 规则），td.sym 自身不再画背景，
   否则会把「延伸到价格区」的渐变切成两段。这里只管文字几何：上内距走 --row-pad-top
   （桌面 3 / 手机 5），手机档 padding-left/right 由后面的规则单独覆盖，不许写死左右内距
   （会把手机档 6px/2px 覆盖掉、中文名贴上灯条）。字号/行高/内距一律不动，
   用户明确要求「代码文字高度位置不变」——改的只有渐变的 position/size。 */
table.stk tr[data-symbol]>td.sym{grid-area:sym;position:static;display:block;padding:var(--row-pad-top) 8px 0;font-size:14px;line-height:var(--row-height)}
table.stk tr[data-symbol]>td.sym>.sym-box{display:grid;grid-template-rows:var(--row-height) var(--row-height);align-items:center;width:100%;max-width:100%;margin:0;padding:0;border:0;background:none;color:var(--text);line-height:var(--row-height);cursor:default}
table.stk tr[data-symbol]>td.sym>.sym-box:has(.tag){color:var(--head-blue-on)}
table.stk tr[data-symbol]>td.sym>.sym-box .tag{display:none}
/* 代码列定宽后极少数超长中文备注名（如「赛默飞世尔科技」）省略显示，完整名仍在 data-full；
   不给它撑宽整列，否则价格区左边界又会跟着晃。 */
table.stk tr[data-symbol]>td.sym>.sym-box .note{display:block;margin:0;font-size:11.5px;line-height:var(--row-height);white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
table.stk tr[data-symbol]>td.sym>.sym-box .unit{display:inline-block;margin-left:4px}
/* 行业标签（2026-11-19）：只在持仓 / 重点两组出现，贴在代码右边，分组名前两个字，灰色 14px。
   sym-box 改两列网格——代码文字（匿名项）在第 1 行第 1 列、标签在第 1 行第 2 列，中文名仍独占第 2 行整行；
   没有标签的行（其它分组）不含 .sec-tag，保持原单列几何，一个像素都不动。 */
table.stk tr[data-symbol]>td.sym>.sym-box:has(.sec-tag){grid-template-columns:auto minmax(0,1fr);column-gap:3px}   /* 手机档代码列 98px：GOOGL(17px)58 + 3 + 「通信」28 ＝89 ≤ 内宽90，gap 再大就挤出 */
/* 2026-11-23 用户：行业名称在代码区里靠右显示，只有手机上距离不足时才向左贴近代码。
   第 2 列本来就是代码右边剩下的全部空间，标签用 justify-self:end 贴到列右端；
   列里放不下时标签按内容宽度收缩（grid 子项 fit-content），自然退回到贴着代码的左侧，
   不需要任何媒体查询。桌面代码区更宽，恒靠右。 */
table.stk tr[data-symbol]>td.sym>.sym-box .sec-tag{grid-row:1;grid-column:2;justify-self:end;text-align:right;color:var(--dim);font-size:14px;font-weight:400;line-height:var(--row-height);white-space:nowrap;overflow:hidden;text-overflow:clip}
table.stk tr[data-symbol]>td.sym>.sym-box:has(.sec-tag) .note{grid-row:2;grid-column:1/-1}
table.stk tr[data-symbol]>td.sym::after{content:"";position:absolute;left:var(--vl-x);top:var(--lamp-top);height:var(--lamp-h);width:1px;background:var(--section-line);pointer-events:none}
/* 价格区两排（2026-11-13 定，11-17 加周涨跌幅）：左块上排「现价 涨跌额（无$）」、
   下排「涨跌幅 周涨跌幅」；「振／量」仍是上下两排（上振下量），但整列靠右贴住
   紫灯框左侧、从右往左铺开：
   grid 两行 + justify-items:end，右端齐平，不再像原来靠左留一大块空白。
   两排行高与左侧代码两行同用 --px-row-h，
   td.px 也补 4px 顶内距，现价与代码第一行中心对齐（原 25px 行高 + 18.5px 字让现价顶部
   比代码高 3.4px 手机 / 6.4px 桌面）。字号收敛到 17.5/16.5/14，只有真的溢出才由 fitPx 收紧。 */
table.stk tr[data-symbol]>td.px{grid-area:px;position:relative;display:block;min-width:0;padding:var(--row-pad-top) 6px 0;text-align:left;white-space:nowrap}
table.stk tr[data-symbol]>td.px .px-main{display:grid;grid-template-columns:max-content minmax(0,1fr);grid-template-rows:var(--px-row-h) var(--px-row-h);column-gap:var(--px-col-gap);align-items:center;min-width:0;margin:0;padding:0 4px 0 0}
table.stk tr[data-symbol]>td.px .px-row{display:flex;align-items:center;gap:var(--px-gap);min-width:0;white-space:nowrap;font-variant-numeric:tabular-nums}
table.stk tr[data-symbol]>td.px .px-row-main{grid-area:1/1/2/2;gap:0}
table.stk tr[data-symbol]>td.px .px-row-sub{grid-area:2/1/3/2}
table.stk tr[data-symbol]>td.px .px-side{grid-area:1/2/3/3;display:grid;grid-template-rows:var(--px-row-h) var(--px-row-h);align-items:center;justify-items:end;min-width:0;white-space:nowrap;font-variant-numeric:tabular-nums}
/* 11-17：涨跌额去掉 $ 后，与现价只隔「一个字符」＝1ch（按涨跌额自身字号算，
   桌面 16.5px→约 9.9px、手机 16px→约 9.6px）。上排 gap 归零由上面那条与这条高特异性
   规则共同守住，压缩档的 .px-row{gap:Npx} 不会再把间距叠成两层；下排仍用 --px-gap。 */
.px-amt{margin-left:1ch}
table.stk tr[data-symbol][data-pxfit] > td.px .px-row-main{gap:0}
.px-price{font-size:17.5px}
.px-chg,.px-amt,.px-v{font-size:16.5px}
.px-k{font-size:14px;color:var(--dim)}
/* 文字框与左侧代码两行同用 21px 行高：现价顶部原本比代码高 3.6px（桌面），统一后近乎齐平。 */
.px-price,.px-chg,.px-amt,.px-v,.px-k{line-height:var(--px-row-h)}
/* 量比只在数字上表达灯级（描述里不再重复这条事实）：红/黄沿用描述字色，未触发保持主文本色。 */
.px-v.vol-red{color:var(--lamp-red)}
.px-v.vol-yellow{color:var(--lamp-yellow)}
/* 振幅同理只在数字上着色：显示值 >6% 黄、>10% 红；纯显示，不报灯、不进统计（2026-11-12）。 */
.px-v.amp-yellow{color:var(--lamp-yellow)}
.px-v.amp-red{color:var(--lamp-red)}
.px-metric{display:inline-flex;align-items:baseline;gap:2px}
/* 11-17 用户「周涨跌幅和日涨跌幅拉开点距离」：整组（含「周」标签）一起右移，
   组内 2px 与「周」的灰色都不动；振／量共用 .px-metric 类，但只在 .px-week 上生效。 */
.px-week{margin-left:var(--px-week-gap)}
table.stk tr[data-symbol][data-pxfit="1"] .px-row{gap:4px}
table.stk tr[data-symbol][data-pxfit="1"] .px-main{column-gap:6px}
table.stk tr[data-symbol][data-pxfit="1"] .px-k{font-size:13px}
table.stk tr[data-symbol][data-pxfit="1"] .px-v{font-size:15px}
table.stk tr[data-symbol][data-pxfit="1"] .px-price{font-size:16.5px}
table.stk tr[data-symbol][data-pxfit="1"] :is(.px-chg,.px-amt){font-size:15.5px}
table.stk tr[data-symbol][data-pxfit="2"] .px-price{font-size:15.5px}
table.stk tr[data-symbol][data-pxfit="2"] :is(.px-chg,.px-amt,.px-v){font-size:14.5px}
table.stk tr[data-symbol][data-pxfit="2"] .px-k{font-size:13px}
/* 第 3 档只在 fitTgs() 把紫灯框压到下限仍挡不住价格区时启用（<=360px 一类极端窄屏）；
   常规视口永远走不到，避免为了兜底把 440px 的设计字号也一起缩小。 */
table.stk tr[data-symbol][data-pxfit="3"] .px-row{gap:3px}
table.stk tr[data-symbol][data-pxfit="3"] .px-main{column-gap:4px}
table.stk tr[data-symbol][data-pxfit="3"] .px-price{font-size:13.5px}
table.stk tr[data-symbol][data-pxfit="3"] :is(.px-chg,.px-amt,.px-v){font-size:12.5px}
table.stk tr[data-symbol][data-pxfit="3"] .px-k{font-size:11.5px}
/* 第 4 档只在 ultra（紫灯框已到下限、第 3 档仍挡不住，约 320px 一类）启用；
   裁切另由 data-tgs-cut 在所有压缩态统一生效：宁可把价格数字尾部截在 td.px 里，
   也不让它画进紫灯框。常规宽度不压缩、不裁切，视觉零改动。 */
table.stk tr[data-symbol][data-pxfit="4"] .px-row{gap:2px}
table.stk tr[data-symbol][data-pxfit="4"] .px-main{column-gap:3px}
table.stk tr[data-symbol][data-pxfit="4"] .px-price{font-size:11.5px}
table.stk tr[data-symbol][data-pxfit="4"] :is(.px-chg,.px-amt,.px-v){font-size:10.5px}
table.stk tr[data-symbol][data-pxfit="4"] .px-k{font-size:10px}
/* 压缩态（fitTgs() 已把紫灯框收到下限）优先保证「共识价 · N月」整组不溢出：把组内的 2px 间隔让回去。
   <440 是项目已声明不适配档，优先「不叠入紫灯框」；设计档 440 与全部桌面档没有 data-tgs-cut，
   --tgs-month-gap 恒 2px。变量在表上覆盖，子元素 .target-month 自动继承，无需改 JS。 */
table.stk[data-tgs-cut]{--tgs-month-gap:0px}
table.stk[data-tgs-cut] tr[data-symbol]>td.px{overflow:hidden}
table.stk tr[data-symbol]>td.px::after{content:"";position:absolute;right:0;top:var(--lamp-top);height:var(--lamp-h);width:1px;background:var(--section-line);pointer-events:none}
/* 2026-11-23 用户：紫灯区右侧不要灰竖线，只留「价格区与紫灯区之间」那一条（由 td.px::after 画）。
   所以 td.tgs 自己不再画右线，原来为紫框态隐藏它的三条 :has(...)::after{display:none} 一并删掉。 */table.stk tr[data-symbol]>td.tgs{grid-area:tgs;position:relative;isolation:isolate;align-self:stretch;display:flex;align-items:flex-start;justify-content:center;padding:var(--row-pad-top) var(--tgs-pad-r,3px) 0 var(--tgs-pad-l,2px);line-height:0;text-align:center;box-sizing:border-box;width:var(--tgs-w);min-width:var(--tgs-w);max-width:var(--tgs-w)}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt{display:grid;grid-template-columns:minmax(0,1fr) auto auto minmax(0,1fr);grid-template-rows:var(--row-height) var(--row-height);flex:1;min-width:0;height:auto;min-height:0;margin:0;padding:0 1px;border:0;background:transparent;column-gap:0;row-gap:0;align-items:center;justify-items:center;line-height:var(--row-height);white-space:nowrap;font-size:var(--tgs-font);font-weight:400;color:var(--purple);font-variant-numeric:tabular-nums}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt>span{height:var(--row-height);line-height:var(--row-height);white-space:nowrap}
table.stk tr[data-symbol]>td.tgs .target-space{grid-area:1/2/2/4}
table.stk tr[data-symbol]>td.tgs .target-price{grid-area:2/2/3/3}
table.stk tr[data-symbol]>td.tgs .target-month{grid-area:2/3/3/4;justify-self:start;white-space:nowrap;margin-left:var(--tgs-month-gap);font-size:var(--tgs-month-font);color:var(--dim,#8b93a1)}
table.stk tr[data-symbol]>td.tgs .target-sep{margin-right:var(--tgs-month-gap);font-size:inherit;line-height:inherit;color:inherit}
/* 2026-11-13 19:35 分隔符归位：原来「 · N月」整段写在一个 span 文本里，span 作为 grid item 时
   前导空格被行首折叠吞掉，· 紧贴 span 左边缘（实测价格右缘→· 只有 2px），而 · 后面还带一个
   空格（约 4px），所以看着「离价格近、离月份远」。现在 · 独立成 .target-sep 真元素，
   左侧由 .target-month 的 margin-left、右侧由 .target-sep 的 margin-right，两侧都用同一个
   --tgs-month-gap：价格右缘→· 中心 = gap + ·宽/2，· 中心→N月 左缘 = ·宽/2 + gap，恒等 ——
   · 严格落在两个数字的几何中点，压缩态 gap 归零也仍然居中。整组比原来少两个空格，
   COST/LITE 的「10月」不再顶到紫框右线。四列成组居中逻辑完全不变：百分比跨 2/4 列居中、
   共识价变长自然往左延伸；20:51 起「· N月」整块定值 13px 灰（--tgs-month-font + var(--dim)），
   不再随主字号联动，四位数共识价那一行的「月」离框右线再退 1.5px。 */
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad{position:relative;color:var(--dim);border-radius:4px;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr)}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad::after{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none;background:url("__TARGET_DASH_BORDER__") center/100% 100% no-repeat}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad .target-price{grid-area:1/2/2/3}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad>span:not(.target-price){grid-area:2/2/3/3}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad:not(:has(.target-price))>span{grid-area:1/2/2/3}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.hit{color:var(--white)}
table.stk tr[data-symbol]>td.tgs:has(.tgt.space)::before{content:"";position:absolute;top:var(--tgt-frame-top);right:0;bottom:var(--tgt-frame-bottom);left:-1px;z-index:-1;pointer-events:none;background:var(--card);border:1px solid var(--purple);border-radius:3px}
table.stk tr[data-symbol]>td.tgs:has(.tgt.high)::before{content:"";position:absolute;top:var(--tgt-frame-top);right:0;bottom:var(--tgt-frame-bottom);left:-1px;z-index:-1;pointer-events:none;background:var(--card);border:1px solid var(--purple);border-radius:3px;border-color:var(--target-high)}
table.stk tr[data-symbol]>td.tgs:has(.tgt.hit)::before{content:"";position:absolute;top:var(--tgt-frame-top);right:0;bottom:var(--tgt-frame-bottom);left:-1px;z-index:-1;pointer-events:none;background:var(--tgt-solid);border-radius:0}
table.stk tr[data-symbol]>td.tgs [data-sign="pos"]{color:var(--tgt-pos)}
#signalCounts .target-purple .percent-change.pos{color:var(--tgt-pos)}
table.stk tr[data-symbol]>td.tgs [data-sign="neg"]{color:var(--tgt-neg)}
#signalCounts .target-purple .percent-change.neg{color:var(--tgt-neg)}
table.stk tr[data-symbol]>td.tgs .tgt.hit [data-sign="pos"]{color:var(--tgt-pos-on)}
table.stk tr[data-symbol]>td.tgs .tgt.hit [data-sign="neg"]{color:var(--tgt-neg-on)}
/* 财报整排：52 周条下方留白改成一行带底色的文字条；倒计时＋美东日期时段＋北京说明全部同行。
   字号 15px（2026-11-14 用户「描述区文字和财报文字改为 15px」）：与个股描述区 td.sig 正文同号，
   两档一起从 14.5px 加到 15px。行高 19px 不动：单行 nowrap + 省略号，15px 字形在 19px 行盒里
   不裁切，整条仍 23px 高，只加大字号不加行距，个股行高不变。 */
.earn-band{display:block;margin:5px 0 0;padding:2px 8px;background:var(--cost-row-bg);border-radius:3px;overflow:hidden}
.earn{display:block;margin:0;font-size:15px;font-weight:400;line-height:19px;white-space:nowrap;color:var(--head-blue-on);overflow:hidden;text-overflow:ellipsis}
/* 颜色只按口径分：蓝色（--head-blue-on）恒为美东时间，灰色（--dim）恒为北京时间。
   23:29 用户「蓝色一直是美东时间的颜色」：括号里写「北京…已出／北京 今天 凌晨」的倒计时
   属于北京口径，必须跟北京说明一起用灰；只有写「（美N天）」的 .cd.tbd 才是美东口径、用蓝。
   命中红黄时 --earn-bj-color 被父级改成红／黄，两种口径的倒计时与整条一起变色。
   --earn-bj-color 与统计名单、筛选条件共用同一套，避免三处口径分叉。 */
.earn.soon{color:var(--lamp-yellow);--earn-bj-color:var(--lamp-yellow)}
.earn.urgent{color:var(--lamp-red);--earn-bj-color:var(--lamp-red)}
.earn .earn-sep,.earn .earn-bj{color:var(--earn-bj-color,var(--dim))}
.earn>.cd,#signalCounts .earnings-week-meta .cd,#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd{font-size:inherit;letter-spacing:0;font-weight:400;color:var(--earn-bj-color,var(--dim));margin-right:-.5em}
.earn>.cd.tbd,#signalCounts .earnings-week-meta .cd.tbd,#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd.tbd{color:var(--earn-bj-color,var(--head-blue-on))}
/* 成本条上下留白（2026-11-12 收紧条内上下 3→2；2026-11-16 上边距 2→var(--sig-row-gap)）：
   两排文字下沿到成本条上边缘＝描述区不同指标之间的间距 4px。条高 26.5px、字号 15px／
   22.5px 行高一律不动。 */
table.stk tr[data-symbol]>td.cost-line{grid-area:cost;display:flex;align-items:center;justify-content:space-between;gap:6px;padding:2px 8px;margin:var(--sig-row-gap) 0 0;background:var(--cost-row-bg);border-radius:3px;color:var(--text);white-space:nowrap;font-size:15px;line-height:22.5px;font-variant-numeric:tabular-nums}
table.stk tr[data-symbol]>td.cost-line[hidden]{display:none}
table.stk tr[data-symbol]>td.cost-line>span{font-size:inherit}
table.stk tr[data-symbol]>td.cost-line[data-pnl="loss"]{background:var(--pnl-loss-bg)}
table.stk tr[data-symbol]>td.cost-line[data-pnl="gain"]{background:var(--pnl-gain-bg)}
/* 描述区共用参数（2026-11-12）：--sig-borrow 是每条描述可再借用的空白，左列借列间空白、
   右列借单元格右内边距，都不越出单元格。窄屏只覆盖 padding，不再重抄整条 grid。
   2026-11-14 用户「加一些中线列距」：列距 10→16px，左右两列各让出 3px 给中线，
   组合加号（10.5px，手机 11.3px）压在线上时两侧各留约 2.75px 空隙，不再像一句话连着。
   描述区字号 14.5→15px（与财报 .earn 同号）；2026-11-15 行距回到 15px 应有的比例：
   桌面 21.75px＝15×1.45 不动，手机 19.57→20.25px＝15×1.35（原 14.5×1.35）；
   不同指标间距 --sig-row-gap 7→4px，即正常折行行距之外只多 4px；2026-11-16 起该变量提到 tr 级，
   同时作为「第一排两排文字下沿 → 下一排内容」的间距，两处永远同值。 */
/* 描述区上留白（2026-11-16）：第一排三格下内距归零后，这一格的上内距就是「两排文字下沿 →
   描述区首行文字」的距离，取 --sig-row-gap。有成本条时它管条→描述，无成本条时管第一排→描述，
   两种情形都是 4px；下留白 7px 不动，避免整块糊在一起。 */
table.stk tr[data-symbol]>td.sig{grid-area:sig;display:block;padding:var(--sig-row-gap) 10px 7px;font-size:15px;line-height:21.75px;--sig-col-gap:16px;--sig-borrow:6px}
/* 左右分区中线（2026-11-14）：原来两列之间只有空白「隐形中线」，现在用 1px 灰线显示出来，
   颜色沿用代码/价格/紫灯区三条分区竖线的 --section-line，不新增颜色。线画在容器背景层，
   不占 grid 格、不改两列宽度与折行测量；组合加号 ::after 仍压在中线上，颜色跟灯级走。 */
.sig-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:var(--sig-col-gap);align-items:start;min-width:0;background:linear-gradient(var(--section-line),var(--section-line)) center/1px 100% no-repeat}
.sig-col{display:flex;flex-direction:column;gap:var(--sig-row-gap);min-width:0}
/* 加号改为1.1px细线绘制（原字形放大后笔画约1.7px偏粗）；尺寸、墨迹中心沿用原字形放大后的位置。 */
.sig-cols[data-combo="1"]{position:relative;--combo-size:10.5px;--combo-cy:10.6px;--combo-stroke:1.1px;--combo-color:var(--dim)}
.sig-cols[data-combo="1"]::after{content:"";position:absolute;left:50%;top:calc(var(--combo-cy) - var(--combo-size) / 2);width:var(--combo-size);height:var(--combo-size);transform:translateX(-50%);background:linear-gradient(var(--combo-color),var(--combo-color)) center/100% var(--combo-stroke) no-repeat,linear-gradient(var(--combo-color),var(--combo-color)) center/var(--combo-stroke) 100% no-repeat;pointer-events:none}
/* 中线右半区比左半区多4px（左9px、右13px）；加号仍压在中线上。 */
.sig-cols>.sig-col:last-child{padding-left:4px}
.sig-cols[data-combo-level="red"]{--combo-color:var(--lamp-red)}
.sig-cols[data-combo-level="yellow"]{--combo-color:var(--lamp-yellow)}
.sig-cols[data-combo-level="gray"]{--combo-color:var(--text)}

.stk td.sig .signal-item{display:block;min-width:0;overflow-wrap:anywhere;padding-left:8px;text-indent:0}
/* 2026-11-17 定稿：用户补充「仅文字灰字描述要保持缩进，只是不显示前面的方灯块」。
   规则表口径「仅文字＝折叠且不计白」，渲染层落为没有 data-signal-level 的灰字
   .signal-item（默认 var(--dim)），含布林+RSI 组合里的 .sig-zone 与未点亮的加/减仓价 lvtag。
   基础行只留 8px 左内距、text-indent 归零，文字起点与有灯级描述完全对齐（8px），
   但不生成 ::before；有灯级与 [data-near="1"] 到价点亮态仍用 8px 悬挂缩进 +
   3×10px 竖条（竖条 3px + margin-right 5px 正好吃掉 -8px，文字同样从 8px 起）。
   改的是现有规则，不追加同选择器覆盖块；原 lvtag 未点亮态的
   background:transparent 随灯块一起退役。 */
.stk td.sig :is(.signal-item[data-signal-level],.signal-item[data-near="1"]){text-indent:-8px}
/* 描述折行（2026-11-12 终稿）：用浏览器默认贪心排——第一行尽量往右放，整块放不下的西文数字串
   才落到第二行。不做两行等长平衡（会把第一行砍短、右半空一大片），也不给任何描述强制不换行
   （强行单行会顶到行外）。每条自动描述再借用 --sig-borrow 的空白，用足可用宽度，实在放不下才换行；
   数据属性驱动，新增同类描述自动生效。 */
.sig-cols>.sig-col .signal-item[data-signal-flow="auto"]{width:calc(100% + var(--sig-borrow));max-width:none;align-self:flex-start}
/* 全角标点只收掉自己字框里的空白半格，汉字/数字间距不动：
   「，」「）」字形在字框左半、「（」在右半，另一半本来就是空的，所以各收0.5字不会盖到邻字。 */
.stk td.sig .cm,.stk td.sig .pr{margin-right:-.5em}
.stk td.sig .pl{margin-left:-.5em}
.stk td.sig :is(.signal-item[data-signal-level],.signal-item[data-near="1"])::before{content:"";display:inline-block;width:3px;height:10px;margin-right:5px;border-radius:1px;background:currentColor;vertical-align:middle}
/* 组合行的缩进落在每个 .sig-zone 上：外层归零，仅文字组合也保持 8px 文字起点 */
.stk td.sig .signal-item:has(.sig-zone){padding-left:0;text-indent:0}
.stk td.sig .signal-item:has(.sig-zone)::before{display:none}
.stk td.sig .sig-zone{display:block;min-width:0;overflow-wrap:anywhere;padding-left:8px;text-indent:0}
.stk td.sig .signal-item[data-signal-level] .sig-zone{text-indent:-8px}
.stk td.sig .signal-item[data-signal-level] .sig-zone::before{content:"";display:inline-block;width:3px;height:10px;margin-right:5px;border-radius:1px;background:currentColor;vertical-align:middle}
.stk td.sig .sig-sep{display:none}
.stk td.sig .signal-separator{display:none}
.stk td.sig .signal-item[data-signal-level="green"]{color:var(--green)}
.stk td.sig .lvtag.add{height:auto;min-height:0;margin:0;border:0;border-radius:0;background:transparent;white-space:normal;color:var(--dim);font:inherit}
.stk td.sig .lvtag.cut{height:auto;min-height:0;margin:0;border:0;border-radius:0;background:transparent;white-space:normal;color:var(--dim);font:inherit}
.stk td.sig .lvtag.add[data-near="1"]{color:var(--orange)}
.stk td.sig .lvtag.cut[data-near="1"]{color:var(--cyan)}
.stk tr.range52-row{display:block;width:100%}
.stk tr.gray:not([data-symbol]){display:block;width:100%}
.stk tr.range52-row>td{display:block;width:100%}
.stk tr.gray:not([data-symbol])>td{display:block;width:100%}
/* 财报条接在轨道下面。2026-11-16 单线定稿：这一格 padding-bottom 6→2px，
   加上非首行个股行 margin-top:3px，合计「上一行内容下边缘 → 单线」正好 5px，
   与「单线 → 紫灯框上边缘」5px 相等。桌面/手机两档同一个 2px。 */
table.stk tr.range52-row>td:first-child{position:relative;padding:0 10px 2px 10px;border:0;box-shadow:none}
.stk th{font-size:13px;color:var(--dim);font-weight:600;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
#group_positions[open]{padding-bottom:2px}
#group_positions>.group-bottom{display:none}
#runLightTxt .run-timestamp{font-size:calc(1em + 1px);line-height:1}
#runLightTxt[data-expandable]{cursor:pointer}   /* 失败名单超过 3 只时折进「…」，点一下展开 */
#signalCounts .signal-extra-row{--chip-w:84px;--xr:12px;position:relative;display:block;padding:0;border-top:1px solid var(--line)}
#signalCounts .signal-extra-row>.text-only>summary{position:relative;box-sizing:border-box;min-height:50px;padding:14px var(--xr) 14px calc(var(--chip-w) + 17px);line-height:22px;font-size:16px;font-weight:400}
#signalCounts .signal-extra-row>.earnings-week>summary{position:relative;box-sizing:border-box;min-height:50px;padding:14px var(--xr) 14px calc(var(--chip-w) + 17px);line-height:22px;font-size:16px;font-weight:400}
#signalCounts .signal-extra-row>.text-only[open]>summary::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:var(--list-border);pointer-events:none}
#signalCounts .signal-extra-row>.earnings-week[open]>summary::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:var(--list-border);pointer-events:none}
#signalCounts .signal-extra-row>.text-only>summary~*{margin-left:5px;margin-right:var(--xr)}
#signalCounts .signal-extra-row>.earnings-week>summary~*{margin-left:5px;margin-right:var(--xr)}
#signalCounts .signal-extra-row>.text-only>summary+*{margin-top:11px}
#signalCounts .signal-extra-row>.earnings-week>summary+*{margin-top:11px}
#signalCounts .signal-extra-row>.text-only[open]{padding-bottom:8px}
#signalCounts .signal-extra-row>.earnings-week[open]{padding-bottom:8px}
#signalCounts .earnings-week-meta{display:block;margin:1px 0 0 9px;color:var(--head-blue-on);font-size:12px}
#signalCounts .signal-filter .signal-member-block h5{margin:0 0 4px;color:var(--head-blue);font-weight:600}   /* 字号不在这里写死：桌面 13px、手机 15px，与下面个股描述文字同号（见手机档 h5 规则） */
#signalCounts .signal-filter .signal-filter-head+.signal-member-block h5{margin-top:6px}
#signalCounts .signal-filter .signal-member-block+.signal-member-block{margin-top:4px}   /* 分组之间间距缩小（原 10px），第一只个股上方也不再画线 */
#signalCounts .signal-filter .signal-member-block ul{margin:0}
#signalCounts .signal-filter .signal-member-belong:empty{display:none}
@media(max-width:600px){
/* 手机档：代码列 98px / 紫灯列 108px（2026-11-13 20:51 用户「LMT 六字离右灰线还有一点距离，
   左移一点灰线，把这个宽度空间给到紫灯区」）。19:35 恢复的左内距 6px（中文名不贴灯条）不动，
   只把右内距 6→2px：note 可见宽仍是 98−6−2＝90px＝精确 6 个中文字，文字墨迹→灰线由 6px 收到 2px。
   灰线左移 4px 全部转给 --tgs-w（104→108），中间 minmax(0,1fr) 价格区净宽不变，98 行仍全 0 档、
   零侵占。四位数共识价的「10月」离紫框右线由 4px 放宽到 7.5px。其余只覆盖变量值。 */
:root{--status-font-size:14.5px;--sym-w:98px;--tgs-w:108px;--px-gap:5px;--px-col-gap:6px;--lamp-top:8px;--lamp-h:36px;--row-pad-top:5px;--tgs-font:16px;--tgs-pad-l:0px;--tgs-pad-r:0px;--pos-num-fs:16px}
.sig-cols[data-combo="1"]{--combo-size:11.3px;--combo-cy:9.7px}
body{padding-left:5px;padding-right:5px}
.oil-reference-row td{padding:8px 6px}
.runbar{padding-left:16px;padding-right:16px;gap:12px}
.brandbar{padding-left:16px;padding-right:16px}
.acts{gap:26px}
.statusrow{margin:-6px 0 8px;gap:4px;padding-left:16px;padding-right:16px}
.statusrow .runlight{line-height:16.88px}
.group-card{padding-top:2px}
.macro-card{padding-top:2px}
.group-card:not([open]){padding-bottom:2px;min-height:63px}
.macro-card:not([open]){padding-bottom:2px;min-height:53px}
.day-pnl-card:not([open]){padding-bottom:2px;min-height:53px}   /* 当日与风险折叠态同为53，比其它分组(63)矮；放在 .group-card 规则之后以覆盖 */
/* 2026-11-18 用户「手机上当日/风险展开后，面板离标题有点远，拉近些」：桌面档已经把展开态
   压到 50px，手机档漏了覆盖，仍是基础规则的 63px —— summary 只有 47px，多出的 16px 空白
   再加 8px row-gap，面板看着离标题 21px（桌面 11px）。这里补回与折叠态同高的 53px，
   既消去空白又保证折叠/展开两态不跳高；基础规则与桌面档的 50px 一律不动。 */
.dual-row > details[open]{min-height:53px}
.zone-card>summary{line-height:22.5px;box-sizing:border-box;min-height:48px;padding-top:0;padding-bottom:0}
.group-card>summary.grp{line-height:24px;box-sizing:border-box;min-height:56px;padding-top:0;padding-bottom:0;font-size:18px}
.macro-card>summary.grp{line-height:24px;box-sizing:border-box;min-height:47px;padding-top:0;padding-bottom:0;font-size:18px}
.day-pnl-card>summary.grp{line-height:24px;box-sizing:border-box;min-height:47px;padding-top:0;padding-bottom:0;font-size:18px}
.group-card>summary .group-stats{line-height:24px;font-size:16px;gap:6px}
.macro-card>summary .group-stats{line-height:24px;font-size:16px;gap:6px}
.zone-card>summary .group-stats{font-size:14px;line-height:22.5px;gap:6px}
/* 11-23 手机档：名称列收窄到 27%，当前列与变化列整体左移靠近名称列；变化列和
   评语列一起放宽到 21／24，两列文字之间不再贴在一起。 */
:is(#macroCard, .macro-scope) td:first-child{width:27%}
:is(#macroCard, .macro-scope) td:nth-child(2){width:28%}
:is(#macroCard, .macro-scope) td:nth-child(3){width:21%}
:is(#macroCard, .macro-scope) td:nth-child(4){width:24%}
:is(#macroCard, .macro-scope) td{padding:3px 6px}
:is(#macroCard, .macro-scope) tr.mh td{padding:3px 6px}
:is(#macroCard, .macro-scope) td:nth-child(3) .unit{white-space:normal}
:is(#macroCard, .macro-scope) .oil-reference-row td{padding-top:6px;padding-bottom:6px}
:is(#macroCard, .macro-scope) .oil-reference-row td:last-child{line-height:20.25px}
:is(#macroCard, .macro-scope) .oil-reference-row td:first-child::before{top:6px;bottom:6px}
table.stk tr[data-symbol]>td.sym{font-size:17px;padding-left:6px;padding-right:2px}
table.stk tr[data-symbol]>td.sym>.sym-box{line-height:20.8px}
table.stk tr[data-symbol]>td.sym>.sym-box .note{font-size:14.5px}
/* 2026-11-13 用户定：手机档三组数字字号恒定 —— 紫灯框 16px（:root 的 --tgs-font）、振/量 15px、
   涨跌幅/涨跌额 16px。16px 比原 15px 宽约 5px，靠 --tgs-pad-l/r 归零腾出（只动框内空白，
   框线 ::before 与数字居中/靠右关系都不变），CDP 实测框内溢出由 5px 回到 0。
   下面四条高特异性规则把 data-pxfit 压缩档也锁死：压缩档只准收间距，不准再改这三组字号 ——
   本地 demo 里 COST 曾因 1,040.55 单独进第 1 档变 15/15.5px，而用户手机上（线上旧版无 fitPx）
   看到的却是全不压缩；定值后字号变小自带余量，COST 自动回到 0 档，98 行全 0 档，数字整齐一致。 */
table.stk tr[data-symbol] .px-v{font-size:15px}
table.stk tr[data-symbol] :is(.px-chg,.px-amt){font-size:16px}
table.stk tr[data-symbol][data-pxfit] .px-v{font-size:15px}
table.stk tr[data-symbol][data-pxfit] :is(.px-chg,.px-amt){font-size:16px}
/* 2026-11-14 用户「描述区文字和财报文字改为 15px」：手机档同样 15px，与 .earn 同号。
   2026-11-15 行距回到 15×1.35＝20.25px（原 14.5×1.35＝19.57px），桌面 21.75px＝15×1.45 不动；
   两档各自保持原有松紧比例，只动行距不动字号。 */
table.stk tr[data-symbol]>td.sig{padding:var(--sig-row-gap) 5px 7px;font-size:15px;line-height:20.25px}
table.stk tr.range52-row>td:first-child{padding:0 6px 2px 6px}
.stk th{padding:7px 5px;font-size:12px}
#signalCounts .signal-extra-row{--chip-w:84px;--xr:8px;padding:0}
.grp-name{font-size:20px;line-height:24px}
.runbar button:not(.cost-edit-btn){padding:8px 13px;font-size:16px;line-height:1.08;height:40px}
.sub{padding-left:16px;padding-right:16px;margin-bottom:8px;line-height:18px}
.runlight{line-height:16.88px}
.stat-badge{font-size:17px;line-height:16px}
/* 2026-11-20 20:4x／20:5x：手机上标题放大，但组名须跟个股代码 td.sym 同为 17px；
   只覆盖变量值，不重抄整条规则。 */
#zoneDualRow,.dual-row.group-dual{--zone-name-fs:17px;--zone-badge-fs:17px;--zone-pad-y:6px}
#signalCounts .signal-count-toggle{font-size:17px;line-height:16px}
.rs-spy,.pos-total{font-size:var(--pos-num-fs,16px);line-height:24px}
.zone-card>summary .zone-name{font-size:16px;line-height:22.5px}
.zone-card>summary .zone-num{font-size:16px;line-height:22.5px}
.dual-body>.cost-contrib .cnsym{font-size:15px;line-height:15.75px}
.dual-body>.cost-contrib .cncn{font-size:14px;line-height:15.75px}
.dual-body>.cost-contrib .ca,.dual-body>.cost-contrib .cv{font-size:14px;line-height:15.75px}
/* 2026-11-15 用户「统计清单描述文字 16→15px、指标名称 +1px 到 16px、外框加宽 10px 高度不变」：
   只动手机档这几条，桌面档（13px/12px）与个股描述区（15px）都不变；行高一律不动。
   清单「描述文字」＝跟在代码后面的数值/条件/财报说明/方向：14.5→16（上一版）→15px（本版），
   代码 .signal-member-symbol 保持 16px；指标名称 .signal-filter-toggle 15→16px（加大 1px）。 */
#signalCounts .signal-member-symbol{font-size:16px;line-height:19.5px}
#signalCounts .signal-member-jump .note{font-size:14px;line-height:18px}
#signalCounts .signal-member-list .signal-member-symbol{line-height:16.25px}
#signalCounts .signal-member-list .note{line-height:15px}
#signalCounts .signal-member-belong{font-size:14px;line-height:18px}
#signalCounts .signal-member-list .signal-member-belong{line-height:15px}
#signalCounts .target-purple .signal-member-symbol{line-height:19.5px}
#signalCounts .target-purple .note,#signalCounts .target-purple .signal-member-belong{line-height:18px}
#signalCounts .signal-member-metric{font-size:15px;line-height:16.88px}
#signalCounts .signal-filter-detail{font-size:15px;line-height:20.25px}
#signalCounts .signal-filter-condition{font-size:15px;line-height:20.25px}
#signalCounts .earnings-week-meta{font-size:15px;line-height:20.25px}
#signalCounts .signal-member-direction{font-size:15px;line-height:20.25px}
#signalCounts .signal-filter-head>span{font-size:13px;line-height:19.5px}
#signalCounts .signal-filter-clear{font-size:13px;line-height:24px}   /* 清除钮：加宽变矮（高 26、横向内距 24），多选结果头一排不再占那么多上下空间 */
#signalCounts td .signal-filter-toggle{padding:0 4px;font-size:16px;line-height:15.95px}
#signalCounts .signal-extra-row>.signal-filter-toggle{font-size:16px;line-height:16.68px}
#signalCounts .signal-extra-row>details>summary{font-size:16px;line-height:22px}
#signalCounts .signal-member-block h5{font-size:15px;line-height:16.25px}
#signalCounts .target-purple>h5{font-size:15px;line-height:16.25px}
#signalCounts .pnl-extra h5{font-size:15px;line-height:16.25px}
#signalCounts>summary .count-total{font-size:16px;line-height:24px}
.market-risk-badge{font-size:16px;line-height:24px}
.monitor-note{font-size:16px;line-height:24px}
.brandbar .dca-badge{margin-left:8px;margin-bottom:4px;font-size:18px;line-height:24px}
.range52-hi{font-size:13px;line-height:11px}
.range52-lo{font-size:13px;line-height:11px}
.range52-gap{font-size:13px;line-height:11px}
.quiet-list>summary{font-size:15px;line-height:19.5px}
.acts .board-return{color:var(--link-blue);background:transparent;border:1px solid var(--control-border);border-radius:8px;padding:7px 15px;margin:0;font-size:16px;line-height:24px}
.acts a.btnlink{color:var(--link-blue);background:transparent;border:1px solid var(--control-border);border-radius:8px;padding:7px 15px;margin:0;font-size:16px;line-height:24px}
.autoplan{font-size:16px;line-height:23.8px}
}
@media(min-width:601px){
/* 桌面列宽更富余：长均线那类长描述多借 8px 空白，避免第二行只剩一个孤字；手机仍用基础 6px。
   全档只覆盖这一个变量值，不重抄 grid 或字号。 */
table.stk tr[data-symbol]>td.sig{--sig-borrow:14px}
.group-card{padding-top:2px}
.macro-card{padding-top:2px}
.group-card:not([open]){padding-bottom:2px}
.macro-card:not([open]){padding-bottom:2px}
.group-card>summary.grp{box-sizing:border-box;height:44px;padding-top:0;padding-bottom:0}
.macro-card>summary.grp{box-sizing:border-box;height:47px;padding-top:0;padding-bottom:0}
.day-pnl-card>summary.grp{box-sizing:border-box;height:47px;padding-top:0;padding-bottom:0}
.zone-card>summary{box-sizing:border-box;height:48px;padding-top:0;padding-bottom:0}
.sub{padding-left:var(--head-inset);padding-right:var(--head-inset)}
.runbar{padding-left:var(--head-inset);padding-right:var(--head-inset)}
.brandbar{padding-left:var(--head-inset);padding-right:var(--head-inset)}
.statusrow{padding-left:var(--head-inset);padding-right:var(--head-inset)}
.check-result{padding-left:var(--head-inset);padding-right:var(--head-inset)}
.dual-row > details[open]{min-height:50px}
#signalCounts .target-purple h5,#signalCounts .pnl-extra h5{font-size:14px;line-height:17.5px}
#signalCounts .target-purple .signal-member-direction{font-size:13px;line-height:16.25px}
#signalCounts .pnl-extra .signal-member-metric{font-size:13px;line-height:16.25px}
.stk td.sig .lvtag.add,.stk td.sig .lvtag.cut{line-height:17px}
}
tr.stk-flash>td,tr.stk-flash+tr.range52-row>td{animation:stkflash 1.8s ease-out}
@keyframes stkflash{0%,55%{background-color:var(--stock-flash)}100%{background-color:transparent}}
"""

# 主页面将此块放在现有 rules/cost CSS 之后，避免旧规则样式覆盖紧凑版。
RULES_CSS = """.nav-orange{color:var(--orange)}
.nav-cyan{color:var(--cyan)}
#fedRules>summary,#fedGlossary>summary,details.card:has(>.rules-body)>summary{cursor:pointer;padding:9px 12px;color:var(--head-blue);font-size:13px;font-weight:600;touch-action:manipulation}
#fedRules[open]>summary,#fedGlossary[open]>summary,details.card[open]:has(>.rules-body)>summary{border-bottom:1px solid var(--line)}
#fedRules>summary:focus-visible,#fedGlossary>summary:focus-visible,details.card:has(>.rules-body)>summary:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:-2px}
.card .rules-body{padding:8px 12px 12px;color:var(--dim);font-size:15px;line-height:1.65;overflow-wrap:anywhere}
.card .rules-body section+section{margin-top:16px;padding-top:4px;border-top:1px solid var(--line)}
.card .rules-body h4{margin:6px 0 8px;color:var(--head-blue);font-size:inherit;font-weight:600}
.card .rules-body h5{margin:12px 0 4px;color:var(--text);font-size:inherit;font-weight:600}
.card .rules-body p{margin:4px 0;color:var(--dim);font-size:inherit;line-height:1.65}
.card .rules-body .rule-group{color:var(--head-blue-on)}
.card .rules-body a{color:var(--head-blue-on);text-underline-offset:3px}
.card .rules-body a:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
.card .rules-body .lamp-red{color:var(--lamp-red);font-weight:600}
.card .rules-body .lamp-yellow{color:var(--lamp-yellow);font-weight:600}
.card .rules-body .lamp-purple{color:var(--purple);font-weight:600}
.card .rules-body .tone-target-high{color:var(--target-high);font-weight:600}
.card .rules-body .rule-table{table-layout:fixed;margin:8px 0 12px;border:1px solid var(--rules-border);font-size:inherit;width:100%;-webkit-text-size-adjust:100%;text-size-adjust:100%}
/* 预警表原行盒12px×1.65=19.8px；只放大字，不随字号放大行距。 */
.card .rules-body .rule-table th,.card .rules-body .rule-table td{padding:6px 7px;border:1px solid var(--rules-cell-border);text-align:left;vertical-align:top;font-size:inherit;line-height:19.8px;overflow-wrap:anywhere}
.card .rules-body .rule-table thead th{color:var(--text);font-weight:600;background:var(--cost-row-bg);border-bottom:1px solid var(--rules-border)}
#rules-lamp-table{margin-left:-16px;margin-right:-16px;width:calc(100% + 32px);border:0;border-collapse:collapse}
/* 报灯表只留横向细线：不再有外框和逐格竖框，分组末行用较亮的同宽细线分段。 */
.card .rules-body #rules-lamp-table th,.card .rules-body #rules-lamp-table td{text-align:center;border:0;border-bottom:1px solid var(--rules-cell-border)}
.card .rules-body #rules-lamp-table .rule-reverse{color:var(--dim)}
.card .rules-body .rule-table col.cat-col{width:18px}
.card .rules-body .rule-table col.col-cond{width:40%}
.card .rules-body .rule-table th.cat-col{width:18px;min-width:18px;max-width:18px;padding:6px 0;line-height:13.2px;font-size:inherit;letter-spacing:1px;white-space:nowrap;overflow:hidden;text-align:center;vertical-align:middle;background:var(--rules-category-bg);writing-mode:vertical-rl;-webkit-writing-mode:vertical-rl;text-orientation:upright;-webkit-text-orientation:upright}
.card .rules-body .ar-band-up{color:var(--green)}
.card .rules-body .ar-band-down{color:var(--red)}
.card .rules-body .ar-rsi-up{color:var(--rsi-up)}
.card .rules-body .ar-rsi-down{color:var(--rsi-down)}
#rules-lamp-table tbody td:first-of-type .ar-band-up,
#rules-lamp-table tbody td:first-of-type .ar-band-down,
#rules-lamp-table tbody td:first-of-type .ar-rsi-up,
#rules-lamp-table tbody td:first-of-type .ar-rsi-down{color:var(--white)}
.arrow-rsi-up,.arrow-rsi-down{color:inherit;font-size:1.15em;line-height:1}
.grp-arrow{font-weight:700}
.stk .sig .arrow-band-up,.stk .sig .arrow-rsi-up,
#signalCounts .arrow-band-up,#signalCounts .arrow-rsi-up{color:var(--up)}
.stk .sig .arrow-band-down,.stk .sig .arrow-rsi-down,
#signalCounts .arrow-band-down,#signalCounts .arrow-rsi-down{color:var(--down)}
.card .rules-body .rule-table .ar-band-up,.card .rules-body .rule-table .ar-band-down,
.card .rules-body .rule-table .ar-rsi-up,.card .rules-body .rule-table .ar-rsi-down{font-weight:700}
.card .rules-body #rules-lamp-table tr.rule-group-end>th,.card .rules-body #rules-lamp-table tr.rule-group-end>td{border-bottom:1px solid var(--rules-group-border)}
.card .rules-body .rule-table td.cell-red{background:var(--rules-red-bg);text-align:center}
.card .rules-body .rule-table td.cell-yellow{background:var(--rules-yellow-bg);text-align:center}
.card .rules-body .rule-table td.cell-gray{background:var(--rules-gray-bg);text-align:center}
.card .rules-body .rule-table td.cell-gray-fold{background:var(--rules-fold-bg);text-align:center}
.card .rules-body #rules-lamp-table thead th.col-cond{color:var(--white)}
.card .rules-body #rules-lamp-table tbody td:first-of-type{color:var(--rules-strong)}
.card .rules-body [id],#fedRules,#fedGlossary{scroll-margin-top:14px}
.card .rules-body .rule-anchor{display:block;height:0}
@media(max-width:600px){.card .rules-body{padding:8px 8px 12px}.card .rules-body .rule-table th,.card .rules-body .rule-table td{padding:6px 4px}#rules-lamp-table{margin-left:-20px;margin-right:-20px;width:calc(100% + 40px)}.card .rules-body .rule-table col.col-cond{width:auto}#rules-lamp-table col[span]{width:86px}#rules-lamp-table td.cell-gray-fold{white-space:nowrap}.card .rules-body .rule-table th.cat-col{line-height:12.1px}}
#signalRulesBody .rule-entry,#glossaryRulesBody .glossary-entry{border-radius:5px;scroll-margin-top:44px;padding:0 8px;margin:0 -8px;transition:background-color .2s,box-shadow .2s}
#signalRulesBody .rule-original{white-space:pre-wrap;overflow-wrap:anywhere}
#signalRulesBody p.rule-original{margin:1px 0;line-height:1.5;color:var(--rules-copy)}
#signalRulesBody h5.rule-original,#signalRulesBody .rule-layout-heading,#glossaryRulesBody h5{margin:14px 0 4px}
#signalRulesBody .rule-layout-scope{margin:1px 0;line-height:1.5;color:var(--rules-copy)}
#signalRulesBody h4.rule-original{margin:18px 0 7px}
#signalRulesBody section:first-of-type h4.rule-original{margin-top:6px}
.card .rules-body .signal-term{display:inline-flex;align-items:center;vertical-align:baseline;white-space:nowrap;overflow-wrap:normal;gap:1px}
.card .rules-body .dir-arrow{display:inline-block;flex:none;width:auto;height:18px;vertical-align:middle;margin-left:2px;overflow:visible}
#signalRulesBody .nav-gray{color:var(--rules-muted)}
#glossaryRulesBody h4{text-align:center}
#glossaryRulesBody h5{color:var(--head-blue-on)}
#glossaryRulesBody p{margin:2px 0;line-height:1.5;color:var(--rules-copy)}
#glossaryRulesBody .rule-table{margin:6px 0 8px}
#glossaryRulesBody .rule-table th,#glossaryRulesBody .rule-table td{line-height:1.5}
#glossary-chg-table thead th:nth-child(2),#glossary-amp-table thead th:nth-child(2){color:var(--lamp-yellow)}
#glossary-chg-table thead th:nth-child(3),#glossary-amp-table thead th:nth-child(3){color:var(--lamp-red)}
#glossary-vol-table thead th:nth-child(2){color:var(--lamp-red)}
#glossary-vol-table thead th:nth-child(3){color:var(--lamp-yellow)}
#glossary-chg-table td,#glossary-vol-table td{font-variant-numeric:tabular-nums;white-space:nowrap}
"""
PAGE_CSS = PAGE_CSS.replace('__TARGET_DASH_BORDER__', TARGET_DASH_BORDER)
PAGE_CSS += RULES_CSS

# 仓位骨架直接随首屏 HTML 输出；不能等页尾 JS 下载、解析完才插卡。
# 仅放结构，不复制计算或云端逻辑。
# 「编辑」按钮 2026-11-02 从这行搬到顶栏（monitor 输出的 .runbar 里「刷新」右边），
# id 仍是 costEditBtn、仍由 cost_panel.js 绑定，只是不再属于这张卡 —— JS 改按 document 查找。
COST_PANEL_HTML = r'''<details class="edit-panel cost-manage" id="costManage">
<summary class="grp"><span class="grp-name">仓位</span><span class="group-stats"><span class="cost-state" id="costState"></span><span class="cost-sum"></span></span></summary>
<div class="cost-body"><div class="cost-acts" id="costActs" hidden><span class="cost-notice" id="costNotice" role="status" hidden></span><button type="button" data-cost-add hidden>＋新增仓位</button><button type="button" class="primary" id="costSave">保存并上传</button><button type="button" id="costCancel">取消</button></div><div class="signal-members-panel" id="costMembers"></div><input type="file" id="costFile" accept=".json" hidden><div class="cost-foot" id="costFoot"></div></div>
<div class="group-bottom"><button type="button" class="collapse-group" aria-controls="costManage" aria-label="收起仓位"><span class="collapse-icon" aria-hidden="true">▲</span></button></div>
</details>'''

# 单独导出供 monitor 替换旧锚点处理器；PAGE_INTERACTION_JS 已含此块，勿重复追加。
RULES_INTERACTION_JS = ""  # 保留导出兼容；规则跳转、hash监听和高亮计时器已删除。
PAGE_INTERACTION_JS = RULES_INTERACTION_JS + r"""

// 2026-11-20 用户定稿：除「统计」和「仓位」可以与任何分组一起展开，其它所有分组
// 任何情况下一次只能开一个。覆盖点标题、程序化打开、统计名单跳转、FIND 定位四条路径。
(function enforceSingleOpen() {
  function isFree(card) {
    return !card || card.id === "signalCounts" || card.id === "group_positions";
  }
  // 2026-11-23：展开其他分组时，统计里展开的子板块（空间/财报/盈亏到价）和仓位里展开的盈亏明细
  // （盈亏明细）也一并收起；统计、仓位两个大栏本身仍保持展开。
  function closeSubFolds() {
    document.querySelectorAll("#signalCounts details[open], #group_positions .pi-fold[open]").forEach(function(d) {
      d.open = false;
    });
  }
  function closeOthers(keep) {
    if (isFree(keep)) return;
    closeSubFolds();
    document.querySelectorAll("details.card[open]").forEach(function(other) {
      if (other === keep || isFree(other)) return;
      other.open = false;
    });
  }
  window.__mmCloseOtherGroups = closeOthers;   // 统计名单跳转 / FIND 定位时显式调用
  // click 捕获阶段同步关：晚一帧再关会让页面高度在滚动定位之后才变，视口落到清单半中间。
  document.addEventListener("click", function(event) {
    const sm = event.target instanceof Element ? event.target.closest("details.card > summary") : null;
    const card = sm && sm.parentElement;
    if (!card || card.open) return;          // 收起这一下不动别人
    closeOthers(card);
  }, true);
})();
// 当日/风险共用一处常驻详情区；只切换hidden，不移动内容或重算数据。
(function bindDualCards() {
  document.querySelectorAll('.dual-row').forEach(function(row) {
    const day = row.querySelector(':scope > #dayPnlCard');
    const macro = row.querySelector(':scope > #macroCard');
    const body = row.querySelector(':scope > .dual-body');
    if (!day || !macro || !body) return;
    function sync(preferred) {
      if (preferred && preferred.open) (preferred === day ? macro : day).open = false;
      const owner = day.open ? 'day' : macro.open ? 'macro' : '';
      body.hidden = !owner;
      body.dataset.owner = owner;
      Array.from(body.children).forEach(function(child) {
        if (child.dataset.owner) child.hidden = child.dataset.owner !== owner;
      });
    }
    row.addEventListener('click', function(event) {
      const summary = event.target.closest && event.target.closest('summary');
      const card = summary && summary.parentElement;
      if (card !== day && card !== macro) return;
      event.preventDefault();
      card.open = !card.open;
      sync(card);
    });
    row.addEventListener('toggle', function(event) {
      if (event.target === day || event.target === macro) sync(event.target);
    }, true);
    sync(day.open ? day : macro);
  });
})();

// 仅测量实际字体与列宽：静态文案、灯级和用户仓位不在这里重新计算。
(function installTableLayout() {
  const rows = Array.from(document.querySelectorAll('table.stk tr[data-symbol]'));
  let frame = 0;
  const dirty = new Set();
  // 两排五项按内容排成「左块 + 右侧上下两排、靠右铺开的振/量」，只有物理上真的顶到列边界才逐档收紧，
  // 静态文案与灯级一律不重算。逐项量右边界，侧列也一并守着。
  function fitPx(row) {
    const main = row.querySelector('td.px .px-main');
    if (!main || !main.getBoundingClientRect().width) return;
    row.dataset.pxfit = '0';
    const items = Array.from(main.querySelectorAll('.px-price,.px-chg,.px-amt,.px-metric'));
    const overflow = function() {
      const right = main.getBoundingClientRect().right;
      return items.some(function(el) { return el.getBoundingClientRect().right > right + .5; });
    };
    const mode = row.closest('table.stk').dataset.tgsfit;
    if (!overflow()) return;
    row.dataset.pxfit = '1';
    if (!overflow()) return;
    row.dataset.pxfit = '2';
    // 第 3/4 档只在紫灯框已压到下限、仍挡不住价格区时由 fitTgs() 逐档放行；常规视口不进这两档。
    if (!overflow() || (mode !== 'tight' && mode !== 'ultra')) return;
    row.dataset.pxfit = '3';
    if (!overflow() || mode !== 'ultra') return;
    row.dataset.pxfit = '4';
  }
  function balanceDescriptions(row) {
    const cols = row.querySelector('td.sig > .sig-cols');
    if (!cols) return;
    const columns = Array.from(cols.children).filter(function(n) { return n.classList.contains('sig-col'); });
    if (columns.length !== 2) return;
    const automatic = Array.from(cols.querySelectorAll('.signal-item[data-signal-flow="auto"]'))
      .sort(function(a, b) {
        // 触及长均线是主条件，优先分到左列先读到；其余条件沿用渲染时的 signalOrder 原序。
        // 优先级表放这里，以后要提前别的条件只加一行，不用改静态 HTML。
        const rank = function(n) { return ({ma: -1})[n.dataset.signalKind] || 0; };
        return rank(a) - rank(b) || Number(a.dataset.signalOrder) - Number(b.dataset.signalOrder);
      });
    if (!automatic.length) return;
    const caps = columns.map(function(c) { return Array.from(c.children).filter(function(n) { return n.matches('.lvtag.add,.lvtag.cut'); }); });
    const planned = columns.map(function(c) { return Array.from(c.children).filter(function(n) {
      return !n.matches('[data-signal-flow="auto"],.lvtag.add,.lvtag.cut');
    }); });
    // 终稿 reflowSignalColumns 在父 td.sig 仍为双列 grid、fontPatch 尚未执行时
    // 按列高分配：13.5px，手机1.45/桌面1.5。只在不可见副本复现这一测量坐标系；
    // 不使真实描述闪回窄列或重引演示补丁。
    // 2026-11-12 修：旧代码把「一列宽」当成整个两列 grid 的宽度，副本里每列只有真实列宽的一半，
    // 首屏第一行被误判成两行，auto 描述被连续塞进同一列（长均线跑右列、右列折行、左列留白）。
    // 副本改 display:block，两个 measured 各自按真实列宽垂直测量，高度才是真实行数。
    const probe = cols.cloneNode(false);
    probe.removeAttribute('data-combo');
    const gap = parseFloat(getComputedStyle(cols).columnGap) || 0;
    // 真实描述宽度 = 列宽 + 借用空白；副本不加这 6px 会把两行估成一行，分栏又会失衡。
    const borrow = parseFloat(getComputedStyle(cols).getPropertyValue('--sig-borrow')) || 0;
    const colWidth = Math.max(0, (cols.getBoundingClientRect().width - gap) / 2) + borrow;
    probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;left:0;top:0;display:block;font-size:13.5px;line-height:'
      + (matchMedia('(max-width:600px)').matches ? '1.45' : '1.5')
      + ';width:' + colWidth + 'px';
    const measured = columns.map(function() { const c = document.createElement('div'); c.className = 'sig-col'; probe.appendChild(c); return c; });
    planned.forEach(function(nodes, side) { nodes.forEach(function(n) { measured[side].appendChild(n.cloneNode(true)); }); });
    cols.parentElement.appendChild(probe);
    try {
      automatic.forEach(function(n) {
        const side = measured[0].getBoundingClientRect().height <= measured[1].getBoundingClientRect().height ? 0 : 1;
        measured[side].appendChild(n.cloneNode(true));
        planned[side].push(n);
      });
    } finally { probe.remove(); }
    columns.forEach(function(c, index) {
      planned[index].concat(caps[index]).forEach(function(n, position) {
        if (c.children[position] !== n) c.insertBefore(n, c.children[position] || null);
      });
    });
  }
  function verticalLines(row) {
    const cell = row.querySelector('td.sym');
    if (!cell) return;
    const root = row.getBoundingClientRect(), rect = cell.getBoundingClientRect();
    // 竖线上下几何统一走 CSS 的 --lamp-top/--lamp-h，和左灯条同起同止（2026-11-13）；
    // 这里只量水平位置，不再按代码文字墨迹量高，免得三条线比灯条短一截。
    row.style.setProperty('--vl-x', (rect.right - root.left).toFixed(2) + 'px');
  }
  // 2026-11-13：极端窄屏/极端长价格时，主动压缩「紫灯框内空间」（左右内距 + 列宽），
  // 把宽度让给价格区，不让振/量数字画进框里。按实测「最挤的一行」整张表统一压缩，所有行列线仍对齐。
  // 字号恒定 13/15px（用户明确不允许紫灯框数字变小），列宽最多收到框内三列刚好不被挤出；
  // 收到底仍侵占的部分交给价格区第 3/4 档与裁切，绝不动紫灯框数字。
  // 二分判据只认 tag 自身溢出。<440 属项目已声明不适配档（2026-11-01 定的两档断点），
  // 不为它加安全余量或额外几何判据；设计档 440 与桌面档根本不走这段。
  const TGS_CLEAR = 3, TGS_MIN_W = 58;
  function tgsNeed(own) {
    let need = 0;
    own.forEach(function(row) {
      if (!row.getBoundingClientRect().width) return; // 折叠行不参与测量。
      const px = row.querySelector('td.px'), tgs = row.querySelector('td.tgs');
      if (!px || !tgs) return;
      const limit = tgs.getBoundingClientRect().left - 1 - TGS_CLEAR; // 框左线 ::before 在 tgs.left-1
      px.querySelectorAll('.px-main .px-price,.px-main .px-chg,.px-main .px-amt,.px-main .px-metric')
        .forEach(function(el) {
          const over = el.getBoundingClientRect().right - limit;
          if (over > need) need = over;
        });
    });
    return need;
  }
  function tgsInnerOver(own) {
    let over = 0;
    own.forEach(function(row) {
      const tag = row.querySelector('td.tgs .lvtag.tgt');
      if (!tag || !tag.getBoundingClientRect().width) return;
      const d = tag.scrollWidth - tag.clientWidth;
      if (d > over) over = d;
    });
    return over;
  }
  function resetTgs() {
    document.querySelectorAll('table.stk').forEach(function(table) {
      table.style.removeProperty('--tgs-w');
      table.style.removeProperty('--tgs-pad-l');
      table.style.removeProperty('--tgs-pad-r');
      delete table.dataset.tgsfit;
      delete table.dataset.tgsCut;
    });
  }
  function fitTgs() {
    document.querySelectorAll('table.stk').forEach(function(table) {
      const own = rows.filter(function(row) { return table.contains(row); });
      if (!own.length) return;
      const baseW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--tgs-w')) || 96;
      const apply = function(w) {
        table.style.setProperty('--tgs-w', w + 'px');
        table.style.setProperty('--tgs-pad-l', '0px');
        table.style.setProperty('--tgs-pad-r', '0px');
      };
      apply(baseW);
      let need = tgsNeed(own);
      if (need <= 0) {                           // 常规宽度零改动，视觉与改前完全一致。
        table.style.removeProperty('--tgs-pad-l');
        table.style.removeProperty('--tgs-pad-r');
        return;
      }
      // 压缩态统一在 td.px 内裁切：列宽收到框内不溢出的极限后若还有几 px 侵占，
      // 也只截在价格区里，绝不画到紫灯框上（用户要的是「不叠入」，不是把框压变形）。
      table.dataset.tgsCut = '1';
      // 只压缩「框内空间」：把左右内距归零 + 收窄列宽（两侧 1fr 弹性留白自然被压到 1em）。
      // 字号恒定 13/15px 不动（用户 2026-11-13 明确不允许紫灯框数字变小）。
      // 收窄下限由「框内三列不被挤出」决定：二分找最小的不溢出列宽，再窄就把百分比/价格/
      // 月份圈压出框了，属于压缩过头。剩余的侵占交给价格区第 3/4 档与裁切，不动紫灯框。
      const target = Math.max(TGS_MIN_W, Math.round(baseW - need));
      let lo = target, hi = baseW;
      while (lo < hi) {
        const mid = Math.floor((lo + hi) / 2);
        apply(mid);
        if (tgsInnerOver(own) > .5) lo = mid + 1; else hi = mid;
      }
      apply(lo);
      if (tgsInnerOver(own) > .5) apply(Math.min(baseW, lo + 1));
      if (tgsNeed(own) > 0) {                   // 收到内容极限仍侵占：价格区第 3 档兜底。
        table.dataset.tgsfit = 'tight';
        own.forEach(fitPx);
        if (tgsNeed(own) > 0) {                // 第 3 档还不够（约 320px）：第 4 档 + 裁切硬兜底。
          table.dataset.tgsfit = 'ultra';
          own.forEach(fitPx);
        }
      }
    });
  }
  function layout(row) {
    if (!row.getBoundingClientRect().width) return; // 折叠行展开后才测量。
    fitPx(row);
    balanceDescriptions(row);
    verticalLines(row);
  }
  function schedule(scope) {
    rows.forEach(function(row) { if (!scope || scope === row || scope.contains(row)) dirty.add(row); });
    if (!dirty.size || frame) return;
    frame = requestAnimationFrame(function() {
      frame = 0;
      const pending = Array.from(dirty); dirty.clear();
      // 先回到基准列宽再让 fitPx 量，避免上一帧的压缩值参与这一档判定（否则会逐帧来回摆）。
      resetTgs();
      pending.forEach(layout);
      fitTgs();
    });
  }
  if (window.ResizeObserver) {
    const widths = new WeakMap();
    const observer = new ResizeObserver(function(entries) {
      entries.forEach(function(entry) {
        const width = entry.contentRect.width;
        if (widths.get(entry.target) !== width) {
          widths.set(entry.target, width);
          schedule(entry.target);
        }
      });
    });
    document.querySelectorAll('table.stk').forEach(function(table) { observer.observe(table); });
  }
  window.addEventListener('resize', function() { schedule(); });
  window.addEventListener('pageshow', function() { schedule(); });
  document.addEventListener('toggle', function(event) {
    if (event.target instanceof HTMLDetailsElement && event.target.open) schedule(event.target);
  }, true);
  document.addEventListener('cost:painted', function() { schedule(); });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function() { schedule(); });
  schedule();
})();
// 仅页面内明确刷新复位；普通股票定位和之后的往返缓存不改变用户展开状态。
(function resetBoardOnRefresh() {
  var url;
  try { url = new URL(window.location.href); } catch (_) { return; }
  if (url.searchParams.get('_mm_reset_view') !== '1') return;
  var previousRestoration;
  try {
    previousRestoration = history.scrollRestoration;
    history.scrollRestoration = 'manual';
  } catch (_) {}
  function resetView() {
    document.querySelectorAll('details.card,details.cost-manage').forEach(function(card) {
      card.open = card.id === 'signalCounts';
    });
    // 明确刷新与初载同版面：持仓父组展开，长/短子区折叠；不碰无异动列表。
    document.querySelectorAll('#group_positions_long,#group_positions_short').forEach(function(card) {
      card.open = false;
    });
    var stats = document.getElementById('signalCounts');
    if (stats) normalizeSignalCard(stats);
    window.scrollTo(0, 0);
  }
  resetView();
  // pageshow 后再复位一次，覆盖浏览器在加载末尾恢复的滚动/details 状态。
  window.addEventListener('pageshow', function finishRefreshReset() {
    resetView();
    url.searchParams.delete('_mm_reset_view');
    try { history.replaceState(history.state, '', url.href); } catch (_) {}
    requestAnimationFrame(function() {
      window.scrollTo(0, 0);
      try {
        if (previousRestoration) history.scrollRestoration = previousRestoration;
      } catch (_) {}
    });
  }, {once: true});
})();
// 屏幕最上面 8.75% 留给 iOS 灵动岛/状态栏：展开的分组、清单一律从这条线开始显示，不再贴着屏幕顶边。
// （2026-11-11 先 1/6=16.7%，再 1/8=12.5%，再按用户要求「在 1/8 基础上上提 4 成」= 7.5%，最后改为「上提 3 成」= 12.5%×0.7 = 8.75%；要改只改这一个系数。）
function topReserve() {
  var vh = window.innerHeight || document.documentElement.clientHeight || 0;
  return Math.round(vh * 0.0875);
}
function revealSignalPanel(panel) {
  // 把刚展开的清单带进视口：能整块放下就整块露出，放不下就从顶部 8.75% 线开始对齐。
  var pad = 12;
  var top = topReserve();
  var vh = window.innerHeight || document.documentElement.clientHeight || 0;
  if (!vh) return;
  var rect = panel.getBoundingClientRect();
  if (rect.top >= top && rect.bottom <= vh - pad) return;   // 已经完整可见（且不在灵动岛区）就不动，避免无谓跳动
  var y = window.pageYOffset || document.documentElement.scrollTop || 0;
  var target = (rect.height + top + pad <= vh && rect.bottom > vh - pad)
    ? y + rect.bottom - (vh - pad)                          // 整块往上推
    : y + rect.top - top;                                   // 其余：从顶部 8.75% 线开始看起
  window.scrollTo({ top: target < 0 ? 0 : target, behavior: "smooth" });
}
function normalizeSignalCard(card) {
  // 统计面板内部全部回到初始折叠态：明细面板收起、计数按钮回位、一周财报关掉、筛选清空。
  card.querySelectorAll(".signal-members-panel").forEach(function(panel) {
    panel.hidden = true;
  });
  card.querySelectorAll(".signal-count-toggle").forEach(function(button) {
    button.setAttribute("aria-expanded", "false");
  });
  // 紫灯、盈亏到价（.text-only）和未来15天财报（.earnings-week）三块同类：
  // 移动端（iOS Safari / 部分安卓内核）在父面板不可见期间改 open，偶尔不重算子元素
  // 高度，再展开就会留一块空白（紫灯展开时折叠统计、再展开尤其明显）。
  // 这里对三块都显式绕一次 display，逼它们按折叠态重排。
  card.querySelectorAll(".text-only, .earnings-week").forEach(function(panel) {
    panel.open = false;
    panel.style.display = "none";
    void panel.offsetHeight;
    panel.style.display = "";
  });
  // 外面那一层「信号行」也一并重排：iOS WebKit 可能让行容器还记着展开时的旧高度。
  card.querySelectorAll(":scope > .signal-extra-row, :scope > .group-bottom").forEach(function(row) {
    row.style.display = "none";
    void row.offsetHeight;
    row.style.display = "";
  });
  card.querySelectorAll(".signal-filter-toggle").forEach(function(button) {
    button.setAttribute("aria-pressed", "false");
  });
  renderSignalFilter();
  void card.offsetHeight;   // 强制重排，确保恢复出来的高度按折叠态重算
}
// 紫灯 / 财报两块（紫灯在上、财报在下）的详情和其他灯的名单同属「同时只开一个」：开一个，收起其余。
function closeSignalExtras(keep) {
  document.querySelectorAll("#signalCounts .signal-extra-row > details[open]").forEach(function(d) {
    if (d !== keep) d.open = false;
  });
}
document.addEventListener("toggle", function(event) {
  const d = event.target;
  if (!(d instanceof HTMLDetailsElement) || !d.open || !d.parentElement
      || !d.parentElement.classList.contains("signal-extra-row")) return;
  if (window.MM_SETTINGS && window.MM_SETTINGS.signal_single_open === 0) return;
  const topBefore = d.getBoundingClientRect().top;
  closeSignalExtras(d);
  document.querySelectorAll("#signalCounts .signal-count-toggle[aria-expanded='true']").forEach(function(b) {
    const p = document.getElementById(b.dataset.target);
    if (p) p.hidden = true;
    b.setAttribute("aria-expanded", "false");
  });
  // 收起上一个会把页面拉上去：先把刚点的这行拉回原来的屏幕位置，再把整块带进视口
  const delta = d.getBoundingClientRect().top - topBefore;
  if (Math.abs(delta) > 1) window.scrollBy(0, delta);
  requestAnimationFrame(function() { revealSignalPanel(d); });
}, true);
// 设置「首页分组单个展开」=1：用户点标题展开某个分组（含长/短持仓）时自动收起其它分组；
// 当日、风险、统计不参与（2026-11-19 起「仓位」不再常开，同普通分组）。只响应点标题（页面加载时的默认展开不触发）。
document.addEventListener("click", function(event) {
  const summary = event.target instanceof Element ? event.target.closest("details[id^='group_'] > summary") : null;
  if (!summary || !(window.MM_SETTINGS && window.MM_SETTINGS.group_single_open === 1)) return;
  const d = summary.parentElement;
  if (!d) return;
  const topBefore = d.getBoundingClientRect().top;
  requestAnimationFrame(function() {
    if (!d.open) return;
    document.querySelectorAll("details[id^='group_'][open]").forEach(function(other) {
      // 2026-11-20 定稿：「仓位」可与任何分组一起展开，不受「一次只开一个」影响。
      if (other !== d && other.id !== 'group_positions') other.open = false;
    });
    // 收起上面的分组会把页面拉上去：先瞬间拉回原来的屏幕位置，再平滑滚到顶部 8.75% 高度线（上面留给灵动岛）。
    const delta = d.getBoundingClientRect().top - topBefore;
    if (Math.abs(delta) > 1) window.scrollBy(0, delta);
    const dy = d.getBoundingClientRect().top - topReserve();
    if (Math.abs(dy) > 1) window.scrollBy({top: dy, behavior: "smooth"});
  });
}, true);
document.addEventListener("toggle", function(event) {
  const card = event.target;
  if (!(card instanceof HTMLDetailsElement) || card.id !== "signalCounts") return;
  // 展开和收起都归一化一次：只在收起时做的话，内层「未来15天财报」是在面板已经不可见
  // 的状态下被脚本关掉的，部分移动端浏览器会留下旧高度，再展开就多出一块空白。
  normalizeSignalCard(card);
  // 展开后再延迟刷新一次：个别手机内核在展开动画/滚动恢复之后才把旧高度算回来。
  if (card.open) window.setTimeout(function() { if (card.open) normalizeSignalCard(card); }, 350);
}, true);
// 兜底一：少数移动内核的 toggle 事件不稳，点标题后用下一帧再归一化一次。
document.addEventListener("click", function(event) {
  const target = event.target;
  if (!target || !target.closest) return;
  if (!target.closest("#signalCounts > summary") || target.closest(".stat-find")) return;
  const card = document.getElementById("signalCounts");
  if (!card) return;
  // 即将收起（此刻点击发生在默认的折叠动作之前、面板仍可见）：先在可见状态下把三块关好并重排，
  // 避免它们是在父面板已不可见时才被关掉的。
  if (card.open) normalizeSignalCard(card);
  requestAnimationFrame(function() { normalizeSignalCard(card); });
}, true);
// 兜底二：iOS Safari 往返缓存（bfcache）会原样恢复 details 状态，回到页面时统一复位。
window.addEventListener("pageshow", function() {
  const card = document.getElementById("signalCounts");
  if (card) normalizeSignalCard(card);
});
// 卡片收起时，里面的「无异动 N 只」跟着回到默认折叠。否则浏览器会记住 details 的 open 状态，
// 下次再展开刚点开的那份清单还是开着的。2026-11-16 持仓已合并成一张表，不再有常开清单。
function resetQuietLists(card) {
  card.querySelectorAll("details.quiet-list[open]").forEach(function(sub) {
    sub.open = false;
    // 同统计面板的处理：父容器不可见期间改 open，部分移动端内核不重算高度，会留一块空白
    sub.style.display = "none";
    void sub.offsetHeight;
    sub.style.display = "";
  });
  void card.offsetHeight;
}
document.addEventListener("toggle", function(event) {
  const card = event.target;
  if (!(card instanceof HTMLDetailsElement) || card.open) return;
  // 只处理装着「无异动」清单的卡片；长/短持仓子区自身不在这里复位（两区默认展开是定好的）
  if (!card.querySelector("details.quiet-list[open]")) return;
  resetQuietLists(card);
}, true);
function signalFilterData() {
  const node = document.getElementById("signalFilterResult");
  if (!node || !node.dataset.filter) return null;
  try { return JSON.parse(node.dataset.filter); } catch (error) { return null; }
}
function intersectSignals(data, kinds) {
  data = mergeMaFilters(data);
  kinds = Array.from(new Set(kinds.map(k => k === 'ma_cross' || k === 'ma_band' ? 'ma' : k)));
  // 返回 {pending: [信号名...]} 或 {members: [...]}；任一条件名单缺失就不拼残缺结果
  const pending = kinds.filter(function(kind) { return !data[kind] || !data[kind].ok; })
    .map(function(kind) { return data[kind] ? data[kind].label : kind; });
  if (pending.length) return {pending: pending};
  const maps = kinds.map(function(kind) {
    const map = new Map();
    data[kind].members.forEach(function(m) { map.set(m.id, m); });
    return map;
  });
  const members = data[kinds[0]].members.filter(function(m) {
    return maps.every(function(map) { return map.has(m.id); });
  }).map(function(m) {
    const details = [];
    maps.forEach(function(map, index) {
      const member = map.get(m.id), kind = kinds[index];
      (member.d || []).forEach(function(part) {
        if (!part || typeof part !== "string" && typeof part !== "object") return;
        // 新数据逐项携带真实灯级；旧字符串只在读取时兼容，不用整行最高级覆盖新条件。
        let text = typeof part === "string" ? part : part.t;
        if (typeof text !== "string" || !text) return;
        let level = typeof part === "string" ? member.l : part.l;
        if (typeof part === "string") {
          const old = text.match(/^(红灯|黄灯|白灯|灰灯|紫灯|绿灯)\s*·\s*/);
          if (old) {
            level = {红灯:"red",黄灯:"yellow",白灯:"gray",灰灯:"gray",紫灯:"purple",绿灯:"green"}[old[1]];
            text = text.slice(old[0].length);
            const prefix = data[kind].label + " · ";
            if (text.startsWith(prefix)) text = text.slice(prefix.length);
          }
          text = text.replace(/^布林\+RSI\s*/, "布林");
          if (kind === "boll_rsi" && /^(上轨|下轨)/.test(text)) text = "布林" + text;
        }
        const detail = {k:kind, l:level || "", t:text};
        if (kind === "earnings_week" && part.e) detail.e = part.e;
        details.push(detail);
      });
    });
    return Object.assign({}, m, {details: details});
  });
  return {members: members};
}
// 兼容旧快照的均线别名，归并属于统计层，不依赖成本面板或已读链路。
function signalIdentity(symbol) {
  let s = String(symbol || '').trim().toUpperCase().replace(/^31#/, '');
  if (s === 'BRK.B') s = 'BRK-B';
  const markets = {US:null, HK:'.HK', SH:'.SS', SS:'.SS', SZ:'.SZ'};
  ['-', '.'].some(sep => {
    const at = s.lastIndexOf(sep), code = s.slice(0, at), market = s.slice(at + 1);
    if (at <= 0 || !Object.prototype.hasOwnProperty.call(markets, market)) return false;
    s = market === 'US' ? code.replace(/\./g, '-') : market === 'HK' ?
      (code.replace(/^0+/, '') || '0').padStart(4, '0') + '.HK' : code + markets[market];
    return true;
  });
  const aliases = {'.SPX':'^GSPC', '^SPX':'^GSPC', '.VIX':'^VIX', '.NDX':'^NDX',
    '.VXN':'^VXN', '.SOX':'^SOX', 'BD#US10Y':'^TNX', '.TNX':'^TNX', 'ESMAIN':'ES=F', 'CLMAIN':'CL=F'};
  return (aliases[s] || s).replace(/\//g, '-');
}
function mergeMaFilters(data) {
  const out = Object.assign({}, data), order = ['red','yellow','green','gray'];
  const keys = ['ma','ma_cross','ma_band'].filter(k => Object.prototype.hasOwnProperty.call(out, k));
  if (!keys.length) return out;
  const members = new Map(); let ok = true;
  keys.forEach(k => {
    const source = out[k];
    if (!source || !source.ok || !Array.isArray(source.members)) { ok = false; return; }
    source.members.forEach(m => {
      const id = signalIdentity(m.s || m.id), previous = members.get(id);
      let details = (Array.isArray(m.d) ? m.d : []).slice();
      if (previous) {
        details = previous.d.concat(details.filter(t => !previous.d.some(old => JSON.stringify(old) === JSON.stringify(t))));
        if (order.indexOf(m.l) < 0 || order.indexOf(previous.l) >= 0 && order.indexOf(previous.l) <= order.indexOf(m.l)) m = previous;
      }
      members.set(id, Object.assign({}, m, {id:id, d:details}));
    });
  });
  keys.forEach(k => delete out[k]);
  out.ma = {label:'均线', ok:ok, members:ok ? Array.from(members.values()) : []};
  return out;
}
// 多选条件的四种方向符号与主表/单选统计共用配色，正文灯色不变。
// 保留原函数名兼容旧调用；逐字符生成安全文本节点，不解析数据中的 HTML。
function appendRsiArrows(parent, text) {
  const classes = {"▲": "arrow-band-up", "▼": "arrow-band-down",
    "↑": "arrow-rsi-up", "↓": "arrow-rsi-down"};
  // 带正负号的百分比数字（+8.31% / -5.47%）用涨跌色；「±3.1%」不匹配，0 不上色。
  const pctRe = /(?<![±\d.])([+\-\u2212])(\d[\d,]*(?:\.\d+)?)%/y;
  const amtRe = /([+\-\u2212]?)(\d[\d,]*\.\d+)(?![\d%])/y;   // 跟在 U+2002 后面的涨跌金额（不带货币符号）
  let buf = "";
  const flush = function() {
    if (buf) { parent.appendChild(document.createTextNode(buf)); buf = ""; }
  };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (Object.prototype.hasOwnProperty.call(classes, ch)) {
      flush();
      const span = document.createElement("span");
      span.className = classes[ch];
      span.textContent = ch;
      parent.appendChild(span);
      continue;
    }
    if (ch === "+" || ch === "-" || ch === "\u2212") {
      pctRe.lastIndex = i;
      const m = pctRe.exec(text);
      if (m && Number(m[2].replace(/,/g, "")) !== 0) {
        flush();
        const span = document.createElement("span");
        span.className = m[1] === "+" ? "num-up" : "num-down";
        span.textContent = m[0];
        parent.appendChild(span);
        i += m[0].length - 1;
        continue;
      }
    }
    if (ch === "\u2002") {
      amtRe.lastIndex = i + 1;
      const m = amtRe.exec(text);
      if (m && m[1] && Number(m[2].replace(/,/g, "")) !== 0) {
        buf += ch;
        flush();
        const span = document.createElement("span");
        span.className = m[1] === "+" ? "num-up" : "num-down";
        span.textContent = m[0];
        parent.appendChild(span);
        i += m[0].length;
        continue;
      }
    }
    buf += ch;
  }
  flush();
}

// 多选清单按持仓/重点/其他分组，使用真实条件数据排序，不读取或重写已生成的展示节点。
function signalMemberKey(member) {
  const weights = {red:5, yellow:4, gray:3, white:3, purple:2, green:1};
  const order = member.o == null ? 9 : Number(member.o);
  // 持仓＝分组顺序第 0 位（不分长短）。成员所属组名自 11-15 起只截前两个字（长持／短持），
  // 旧写法拿「长持仓／短持仓」全称比对，永远对不上，持仓个股全掉进「其他」；改认顺序位，名称写法再变也不受影响。
  const bucket = order === 0 ? 0 : order === 1 ? 1 : 2;
  let red = 0, score = 0, count = 0, magnitude = 0;
  member.details.forEach(function(part) {
    red += part.l === 'red' ? 1 : 0;
    score += weights[part.l] || 0;
    for (const match of part.t.matchAll(/(\d+)\s*条/g)) count = Math.max(count, Number(match[1]));
    for (const match of part.t.matchAll(/(-?\d+(?:\.\d+)?)\s*[%×]/g)) magnitude = Math.max(magnitude, Math.abs(Number(match[1])));
  });
  return [bucket, -(weights[member.l] || 0), -red, -score, -count, -magnitude, order, member.s];
}

function renderSignalFilter() {
  const box = document.getElementById("signalFilterResult");
  if (!box) return;
  const buttons = Array.from(document.querySelectorAll(".signal-filter-toggle[aria-pressed='true']"));
  box.textContent = "";
  if (!buttons.length) { box.hidden = true; return; }
  const data = signalFilterData() || {};
  const kinds = buttons.map(function(b) { return b.dataset.kind; });
  const names = kinds.map(function(kind) { return data[kind] ? data[kind].label : kind; });
  const head = document.createElement("div");
  head.className = "signal-filter-head";
  const title = document.createElement("span");
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "signal-filter-clear";
  clear.textContent = "清除";
  head.append(title, clear);
  box.append(head);
  box.hidden = false;
  if (kinds.length < 2) {
    title.textContent = "已选：" + names[0] + " · 再点一项信号，查看同时满足的标的";
    return;
  }
  const result = intersectSignals(data, kinds);
  if (result.pending) {
    title.textContent = "同时满足：" + names.join(" + ");
    const p = document.createElement("p");
    p.textContent = result.pending.join("、") + " 明细待下次更新，暂不计算";
    box.append(p);
    return;
  }
  title.textContent = "同时满足：" + names.join(" + ") + " · " + result.members.length + " 只";
  if (!result.members.length) {
    const p = document.createElement("p");
    p.textContent = "无同时满足的标的";
    box.append(p);
    return;
  }
  const groups = [[], [], []];
  result.members.map(function(member, index) {
    return {member: member, key: signalMemberKey(member), first: index === 0};
  }).sort(function(a, b) {
    for (let i = 0; i < a.key.length; i++) {
      if (a.key[i] < b.key[i]) return -1;
      if (a.key[i] > b.key[i]) return 1;
    }
    return 0;
  }).forEach(function(record) { groups[record.key[0]].push(record); });
  groups.forEach(function(records, bucket) {
    if (!records.length) return;
    const block = document.createElement("div");
    block.className = "signal-member-block";
    const heading = document.createElement("h5");
    heading.textContent = ["持仓", "重点", "其他"][bucket];
    const list = document.createElement("ul");
    records.forEach(function(record, index) {
    const m = record.member;
    const li = document.createElement("li");
    // 多选名称前保留对应原灯色竖条；描述内每条条件只用自身文字颜色。
    if (["red", "yellow", "green", "gray", "purple"].indexOf(m.l) >= 0) {
      const dot = document.createElement("span");
      dot.className = "signal-name-lamp " + m.l;
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label",
        {red: "红灯", yellow: "黄灯", green: "绿灯", gray: "缺失", purple: "紫灯"}[m.l]);
      li.append(dot);
    }
    const link = document.createElement("a");
    link.className = "signal-member-jump";
    link.setAttribute("href", "#" + m.a);
    const sym = document.createElement("strong");
    sym.className = "signal-member-symbol";
    sym.textContent = m.s;
    link.append(sym);
    if (m.n) {
      const note = document.createElement("span");
      note.className = "note";
      note.textContent = m.n;
      link.append(note);
    }
    const group = document.createElement("span");
    group.className = "signal-member-belong";
    group.textContent = "（" + m.g + "）";
    li.append(link, group);
    const detail = document.createElement("span");
    detail.className = "signal-filter-detail";
    m.details.forEach(function(part, index) {
      if (index) detail.appendChild(document.createTextNode(" / "));
      const condition = document.createElement("span");
      condition.className = "signal-filter-condition";
      condition.dataset.signalKind = part.k;
      condition.dataset.signalLevel = part.l;
      if (part.k === "earnings_week" && part.e) {
        // 指标名「财报」永远在最前（与「涨跌幅」等一致），其后才是北京倒计时，再是美东日期时段、北京说明。
        let rest = part.t;
        const nameEnd = rest.indexOf(" ");
        if (rest.indexOf("财报") === 0 && nameEnd > 0) {
          condition.appendChild(document.createTextNode(rest.slice(0, nameEnd + 1)));
          rest = rest.slice(nameEnd + 1);
        }
        if (part.e.c) {
          const cd = document.createElement("span");
          cd.className = part.e.tbd ? "cd tbd" : "cd";
          cd.textContent = part.e.c;
          condition.append(cd);
        }
        condition.appendChild(document.createTextNode(rest));
        if (part.e.b) {
          const bj = document.createElement("span");
          bj.className = part.e.relative ? "earnings-week-bj is-countdown" : "earnings-week-bj";
          bj.textContent = "·" + part.e.b;
          condition.append(bj);
        }
      } else {
        appendRsiArrows(condition, part.t);
      }
      detail.appendChild(condition);
    });
    li.append(detail);
    // 分隔线只画在「同一分组内」个股之间：每个分组名下第一只个股上方不画线（2026-11-23 用户）。
    // 旧写法按全局第一只判断，每个分组的第一只都会多出一条线。
    if (index > 0) li.classList.add("has-separator");
    list.append(li);
    });
    block.append(heading, list);
    box.append(block);
  });
}
/* ==== MM_FLOAT_FOLD_BEGIN ==== */
/* 2026-11-21：▲ 平时就是清单最底下那条普通按钮，跟着分组一起滚。
   只有「清单比一屏还长、底部那条 ▲ 还没进视口」时才浮到屏幕底部中间；
   滚到分组底部、▲ 自己露出来了，立刻去掉 is-floating 还原原样。
   wrap 是 static 占位，它的 top 就是 ▲ 在文档流里的自然位置，不受 fixed 影响。
   边界（2026-11-21 定稿；此前「隔两个个股明细」「贴着标题栏底边」「按本组第一／
   第二只个股之间分隔线实时量」三版全部作废）：全站只认一条固定参考线，取
   DEMO 里长持仓「第一只与第二只个股之间的分隔线」到「长持仓标题栏下边缘」的距离，
   六档实测 177.75~180.25px，统一写死 180。任何展开分组都把
   「自己的标题栏下边缘 + 180」当成这条线：按钮上沿（bottom 4 + 高 42 = vh-46）
   碰到它就浮，往回滚越过它就收。因此不再逐组量个股行高，按钮浮着时标题栏
   始终留在按钮上方约一只个股的距离，绝不会与标题栏重叠一部分才消失。 */
(function () {
  const SEL = '#zoneDualRow .group-bottom .collapse-group, .dual-row.group-dual .group-bottom .collapse-group';
  // 固定基准，不随分组／视口变化：来源是长持仓 DEMO 实测，见上方注释。
  const FIXED_GAP = 180;
  function update() {
    const vh = window.innerHeight;
    document.querySelectorAll(SEL).forEach(function (btn) {
      const wrap = btn.parentElement;
      const panel = btn.closest('.zone-panel[data-owner],.group-panel[data-owner]');
      const wrapRect = wrap.getBoundingClientRect();
      const shown = !!panel && getComputedStyle(panel).display !== 'none'
        && wrapRect.height > 0;
      const naturalTop = wrapRect.top;
      const longList = !!panel && panel.getBoundingClientRect().height > vh * 0.7;
      // 固定态按钮是 width:100%，宽度就是恒占位的 .group-bottom 的宽度；浮起来后
      // fixed 的 100% 会变成视口宽，所以先量好写进变量，让两态宽度完全一致。
      // 每次滚动/缩放都重算，横竖屏切换或列宽变化后也不会留着旧宽度。
      if (wrapRect.width > 0) {
        btn.style.setProperty('--collapse-float-w', wrapRect.width.toFixed(1) + 'px');
      }
      // 标题栏靠按钮自带的 aria-controls 找回，不受清单搬进共用面板影响。
      const floatTop = vh - 46;
      const headId = btn.getAttribute('aria-controls');
      const head = headId ? document.getElementById(headId) : null;
      const headBottom = head ? head.getBoundingClientRect().bottom : null;
      const sepY = headBottom === null ? null : headBottom + FIXED_GAP;
      const passedSep = sepY !== null && sepY <= floatTop;
      btn.classList.toggle('is-floating', shown && longList && passedSep && naturalTop > floatTop);
    });
  }
  let frame = 0;
  function schedule() {
    if (frame) return;
    frame = requestAnimationFrame(function () { frame = 0; update(); });
  }
  window.addEventListener('scroll', schedule, {passive: true});
  window.addEventListener('resize', schedule);
  window.addEventListener('pageshow', schedule);
  document.addEventListener('toggle', schedule, true);
  document.addEventListener('cost:painted', schedule);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(schedule);
  schedule();
})();
/* ==== MM_FLOAT_FOLD_END ==== */
document.addEventListener("click", function(event) {
  if (!(event.target instanceof Element)) return;
  const closeButton = event.target.closest(".collapse-group");
  if (closeButton) {
    let card = closeButton.closest("details");
    // 2026-11-20：长短持仓清单搬进 .dual-body > .zone-panel 后，▲ 已不在标题 details 内；
    // 用按钮自带的 aria-controls 找回对应标题卡，其它分组仍走 closest("details")。
    const zonePanel = card ? null : (closeButton.closest ? closeButton.closest(".zone-panel[data-owner]") : null);
    if (zonePanel) {
      const controls = closeButton.getAttribute("aria-controls");
      card = controls ? document.getElementById(controls) : null;
    }
    // 2026-11-20 23:xx：其它分组的清单也搬进 .dual-body > .group-panel 后，
    // ▲ 同样不在标题 details 内；按 aria-controls 找回标题卡，以「本行之后第一个元素」
    // 的上沿补回滚动，与长短持仓同款，不用 scrollIntoView 拉动整页。
    const groupPanel = card ? null : (closeButton.closest ? closeButton.closest(".group-panel[data-owner]") : null);
    if (groupPanel) {
      const controls = closeButton.getAttribute("aria-controls");
      const head = controls ? document.getElementById(controls) : null;
      if (!head) return;
      const row = head.closest(".dual-row.group-dual");
      const next = row ? row.nextElementSibling : null;
      const topBefore = next ? next.getBoundingClientRect().top : null;
      head.open = false;
      if (next && topBefore !== null) {
        const delta = next.getBoundingClientRect().top - topBefore;
        // 2026-11-21：从长清单中部点悬浮 ▲ 时，收起后文档变矮，补回来的目标滚动位置可能是负数；
        // 硬补会被浏览器钳到 0，反而把整页甩到页首。补不了就保持当前滚动位置，
        // 用户看到的是刚收起的分组标题和后面的内容，不会再莫名其妙回到最顶。
        if (delta > 1 || (delta < -1 && window.scrollY + delta >= 0)) window.scrollBy(0, delta);
      }
      const heading = head.querySelector(":scope > summary");
      if (heading) heading.focus({preventScroll: true});
      return;
    }
    if (!card) return;
    if (zonePanel) {
      // 清单收起后整段消失，隐藏的 .zone-panel 不能当锚点；用「本组之后的第一个元素」
      // 的上沿前后差补回滚动，下方内容原地不动，也不把整页拉到分组标题。
      const dual = document.getElementById("zoneDualRow");
      const next = dual ? dual.nextElementSibling : null;
      const topBefore = next ? next.getBoundingClientRect().top : null;
      card.open = false;
      if (next && topBefore !== null) {
        const delta = next.getBoundingClientRect().top - topBefore;
        // 2026-11-21：从长清单中部点悬浮 ▲ 时，收起后文档变矮，补回来的目标滚动位置可能是负数；
        // 硬补会被浏览器钳到 0，反而把整页甩到页首。补不了就保持当前滚动位置，
        // 用户看到的是刚收起的分组标题和后面的内容，不会再莫名其妙回到最顶。
        if (delta > 1 || (delta < -1 && window.scrollY + delta >= 0)) window.scrollBy(0, delta);
      }
      const zoneHeading = card.querySelector(":scope > summary");
      if (zoneHeading) zoneHeading.focus({preventScroll: true});
      return;
    }
    // 只收本组：按钮以上整段塌掉，视觉上向下折进按钮所在的这一行（不再用 scrollIntoView 把整页拉到分组标题）。
    // 长持仓、短持仓和其它分组同为分组卡（只多了「持仓」标题和底色），统一处理：
    // 本组底边（▲按钮所在的那一行）就是锚点——文档流里它下面的一切都随它一起移动，
    // 收起后把底边拉回原来的屏幕位置，下面的内容就原地不动；不用找兄弟/父级。
    const bottomBefore = card.getBoundingClientRect().bottom;
    card.open = false;
    const delta = card.getBoundingClientRect().bottom - bottomBefore;
    if (Math.abs(delta) > 1) window.scrollBy(0, delta);
    const heading = card.querySelector(":scope > summary");
    if (heading) heading.focus({preventScroll: true});
    return;
  }
  const filterButton = event.target.closest(".signal-filter-toggle");
  if (filterButton) {
    const selecting = filterButton.getAttribute("aria-pressed") !== "true";
    const technicalKinds = ["rsi", "boll", "boll_rsi"];
    if (selecting && technicalKinds.includes(filterButton.dataset.kind)) {
      document.querySelectorAll(".signal-filter-toggle").forEach(function(button) {
        if (button !== filterButton && technicalKinds.includes(button.dataset.kind)) {
          button.setAttribute("aria-pressed", "false");
        }
      });
    }
    filterButton.setAttribute("aria-pressed", String(selecting));
    renderSignalFilter();
    return;
  }
  if (event.target.closest(".signal-filter-clear")) {
    document.querySelectorAll(".signal-filter-toggle").forEach(function(b) { b.setAttribute("aria-pressed", "false"); });
    renderSignalFilter();
    return;
  }
  const button = event.target.closest(".signal-count-toggle[data-target]");
  if (!button) return;
  const panel = document.getElementById(button.dataset.target);
  if (!panel || !panel.classList.contains("signal-members-panel")) return;
  const owner = button.closest("tr");
  const opening = panel.hidden;
  const buttonTopBefore = button.getBoundingClientRect().top;
  // 设置里的 signal_single_open=1（默认）时，展开新的灯详情会收起整块统计面板里已打开的那些；
  // 关掉后只收同一行的，其它行的可以同时开着对比。
  const singleOpen = !(window.MM_SETTINGS && window.MM_SETTINGS.signal_single_open === 0);
  if (singleOpen && opening) closeSignalExtras(null);   // 紫灯 / 财报两块（紫灯在上、财报在下）的详情也算同一组，点别的就收起
  const scope = (singleOpen && button.closest("#signalCounts")) || owner;
  if (scope) {
    scope.querySelectorAll(".signal-count-toggle[data-target]").forEach(function(other) {
      const otherPanel = document.getElementById(other.dataset.target);
      if (otherPanel) otherPanel.hidden = true;
      other.setAttribute("aria-expanded", "false");
    });
  }
  panel.hidden = !opening;
  // 胶囊高亮完全靠 CSS 认 aria-expanded，这里不用额外加类
  button.setAttribute("aria-expanded", String(opening));
  if (opening) {
    // 展开的清单往往比一屏长，且「同时只开一个」会先收起上一个、把整页拉上去，
    // 结果新打开的清单跳到别处。这里两步定位：先把刚点的按钮拉回原来的屏幕位置，
    // 再把新清单整块带进视口（放不下就从开头对齐）。
    requestAnimationFrame(function() {
      const delta = button.getBoundingClientRect().top - buttonTopBefore;
      if (Math.abs(delta) > 1) window.scrollBy(0, delta);
      revealSignalPanel(panel);
    });
  }
});

function revealStockRow(target, hash) {
  let node = target, keep = null;
  while (node && node !== document.body) {   // 从里到外把所有祖先 details 打开
    if (node.tagName === "DETAILS") {
      node.open = true;
      if (!keep && node.matches && node.matches("details.card")) keep = node;
    }
    node = node.parentElement;
  }
  // 2026-11-20：长短持仓清单住在并排双卡共用的 .dual-body 里，不是标题 details 的后代；
  // 跳转时按 data-owner 把对应标题打开，并把它当作互斥里要保留的那一个。
  const zonePanel = target.closest ? target.closest(".zone-panel[data-owner]") : null;
  if (zonePanel) {
    const zoneCard = document.getElementById(zonePanel.dataset.owner === "long"
      ? "group_positions_long" : "group_positions_short");
    if (zoneCard) { zoneCard.open = true; keep = zoneCard; }
  }
  // 2026-11-20 23:xx：其它分组清单也住在并排双卡共用的 .dual-body 里，
  // 统计名单跳转／FIND 定位时按 data-owner 打开对应标题，并作为互斥里保留的那一个。
  const groupPanel = target.closest ? target.closest(".group-panel[data-owner]") : null;
  if (groupPanel) {
    const groupCard = document.getElementById("group_" + groupPanel.dataset.owner);
    if (groupCard) { groupCard.open = true; keep = groupCard; }
  }
  if (window.__mmCloseOtherGroups) window.__mmCloseOtherGroups(keep);
  target.classList.remove("stk-flash");
  void target.offsetWidth;                   // 强制重排，保证连点同一个名字也能再闪一次
  target.classList.add("stk-flash");
  target.scrollIntoView({block: "center", behavior: "smooth"});
  if (hash && history.replaceState) history.replaceState(null, "", hash);
  window.setTimeout(function() { target.classList.remove("stk-flash"); }, 1800);
}
// 统计面板里的标的名字 → 跳到首页该股所在行。
// 个股可能躺在两层折叠里（分组卡片 + 「无异动 N 只」列表），跳转前必须逐层撑开祖先 details。
document.addEventListener("click", function(event) {
  if (!(event.target instanceof Element)) return;
  const link = event.target.closest("a.signal-member-jump");
  if (!link) return;
  const hash = link.getAttribute("href") || "";
  if (hash.charAt(0) !== "#") return;
  const target = document.getElementById(hash.slice(1));
  if (!target) return;                       // 目标不在本页（比如该股这轮没进表），交给浏览器默认行为
  event.preventDefault();
  revealStockRow(target, hash);
});
// 统计栏右侧「FIND」：按代码定位到首页该股所在行（精确优先，其次前缀）。
// 「首页分组单个展开」打开时，先收起不含目标的其它分组，再展开目标所在分组。
function findStock() {
  const input = document.getElementById("statFindInput");
  if (!input) return;
  const raw = input.value;
  input.value = "";                          // 点 FIND（或回车）后立刻清空输入框，不论找到与否
  const q = raw.trim().toUpperCase().replace(/^\$/, "").replace(/[./]/g, "-");
  if (!q) return;
  let exact = null, prefix = null;
  document.querySelectorAll("tr[data-symbol]").forEach(function(tr) {
    const s = String(tr.getAttribute("data-symbol")).toUpperCase();
    if (!exact && s === q) exact = tr;
    else if (!prefix && s.indexOf(q) === 0) prefix = tr;
  });
  const target = exact || prefix;
  input.classList.remove("miss");
  if (!target || !target.id) {
    void input.offsetWidth;
    input.classList.add("miss");
    window.setTimeout(function() { input.classList.remove("miss"); }, 1500);
    return;
  }
  if (window.MM_SETTINGS && window.MM_SETTINGS.group_single_open === 1) {
    document.querySelectorAll("details[id^='group_'][open]").forEach(function(d) {
      // 同上：FIND 定位也不顺手收掉「仓位」。
      if (d.id !== 'group_positions' && !d.contains(target)) d.open = false;
    });
  }
  revealStockRow(target, "#" + target.id);
  input.blur();
}
document.addEventListener("click", function(event) {
  if (!(event.target instanceof Element)) return;
  const box = event.target.closest(".stat-find");
  if (!box) return;
  event.preventDefault();                    // 在统计栏标题里点输入框/按钮，不能顺带把统计卡折叠
  if (event.target.closest("#statFindBtn")) findStock();
}, true);
document.addEventListener("keydown", function(event) {
  if (event.key === "Enter" && event.target && event.target.id === "statFindInput") {
    event.preventDefault();
    findStock();
  }
}, true);
"""

if __name__ == "__main__":
    main()
