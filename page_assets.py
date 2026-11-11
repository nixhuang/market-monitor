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
line-36||p|加仓价 · 所有分组：主表描述区常驻显示「加仓价 N · 距 X%」，平时灰字；现价与设置价相差≤3% → 点亮橙色，并列入统计面板「盈亏到价」。
line-38||p|减仓价 · 持仓分组：主表描述区常驻显示「减仓价 N · 距 X%」，平时灰字；现价与设置价相差≤3% → 点亮青色，并列入统计面板「盈亏到价」。
line-40||p|减仓价：重点、其他分组不适用。
line-41|rule-cost-loss|h5|持仓盈亏 持仓分组
line-42||p|浮盈或浮亏达到 ±10% → 成本行加淡底色（盈绿、亏红），并列入统计面板「盈亏到价」；只作提示，不再出红灯。
line-46||p|重点、其他分组不适用。
line-47|rule-target-space|h5|目标空间 所有分组
line-48||p|有效空间≥${target_space_pct}% → 紫空心灯；
target-space-positive||p|≥${target_high_space_pct}% → 紫红空心灯。
line-50|rule-target-reached|h5|达到目标／共识价 持仓分组、重点
target-reached-positive||p|当日上穿／跌破或现价在目标±${target_gap_pct}% → 实心紫灯。"""),
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
target-inactive||p|目标空间<${target_space_pct}%、未达到目标／共识价、共识获取失败及失败旧价。
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
    '<tr><th scope="row" class="cat-col" rowspan="5">价格与量能</th>'
    '<td>涨跌幅 · 自适应</td>'
    '<td colspan="3">红灯／黄灯</td></tr>'
    '<tr><td>振幅 · 自适应</td><td colspan="3">红灯／黄灯</td></tr>'
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
<p>两项共用历史日波动档：&lt;${vol_low_sigma}%低档，&gt;${vol_high_sigma}%高档，其余中档；不足${vol_min_returns}个有效日收益取中档。不是按今天的涨跌选档。</p>
<p>所有分组适用；达到红门槛报红，否则达到黄门槛报黄。</p>
${chg_threshold_table}
<h5 id="glossary-amp">振幅 · 计算与阈值</h5>
<p>振幅＝（日高−日低）÷昨收×100%，表示日内摆动宽度。涨跌幅接近0，振幅仍可能很大。</p>
<p>所有分组沿用上节波动档，达到红门槛优先报红，否则达到黄门槛报黄。有效0%保留数值；低于黄门槛不提示、不计白，也不因振幅强制不折叠。日内高低缺失、错位、无效或仅由现价填补时，不计算振幅。</p>
${amp_threshold_table}
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
<p>显示倒计时以快照北京运行日为基准，不随打开页面日期漂移。已知时段按北京落点计算并用白字，盘后顺延一日；时段待定按美东日期计算并用蓝字，未知不猜钟点。显示换算不改变报灯窗口。</p>
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
        "rule-chg": "change", "rule-amp": "amplitude", "rule-low52": "low",
        "rule-ma": "ma", "rule-prices": "prices", "rule-cost-loss": "loss",
        "rule-target-space": "space", "rule-target-reached": "reached",
        "rule-earnings": "earnings", "rule-quotes": "missing", "rule-rsi": "single",
        "glossary-calculation": "terms", "glossary-fetch": "fetch",
        "glossary-chg": "change-terms",
        "glossary-amp": "amplitude-terms", "glossary-boll": "boll-terms",
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
        for kind in ("chg", "amp"):
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
    tables = {kind + "_threshold_table": _threshold_table(kind, values) for kind in ("chg", "amp")}
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
  --bg:#0f1115;
  --card:#171a21;
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
  --signal-border:#6686c6;
  --frame-w:1.5px;
  --head-blue:#bfd5ff;
  --head-blue-on:#8fb8f0;
  --lamp-red:#ff8580;
  --lamp-yellow:#f0c674;
  --frame-green:#2c8f3e;
  --lamp-size:14px;
  --head-inset:64px;
  --row-height:21px;
  --section-line:rgba(139,147,161,.32);
  --tgt-solid:#5a4797;
  --tgt-pos:#4dd0e1;
  --tgt-neg:#ff5c8d;
  --tgt-pos-on:#c2f6ff;
  --tgt-neg-on:#ffd0de;
  --orange:#ffa94d;
  --cyan:#4dd0e1;
  --positions-collapse:rgba(46,160,67,.15);
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
  --stock-divider:#414b5a;
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
h1.brand{color:var(--up)}
/* 顶部一排：站名靠左（比其它标题大2px=21px），代码定位 FIND 靠右 */
/* 站名放大到 26px，FIND 与站名底边对齐（flex-end）：整排被站名撑高，搜索框随之下移，避开 iOS 灵动岛/状态栏顶部的黑色渐变 */
.brandbar{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin:18px 0 4px}
.brandbar h1.brand{margin:0;font-size:26px;line-height:1.5;text-align:left;white-space:nowrap}
.sub{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:var(--status-font-size);margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 4px;margin-bottom:14px;overflow:hidden}
/* 父级只留标题内缩，资产行与子框使用完整分组宽度；▲沿用原持仓绿色底。 */
#group_positions{background:var(--card);--collapse-bg:var(--positions-collapse);--signal-border:var(--frame-green);border:0;box-shadow:none;padding:3px 0 2px;overflow:hidden}
#group_positions>summary{cursor:default;margin-left:5px;margin-right:5px}
#group_positions>summary::before{visibility:hidden}
#group_positions_long.zone-card{box-sizing:border-box;width:100%;background:none;border:1px solid var(--frame-green);border-radius:9px;margin:9px 0 9px}
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
.collapse-group:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
.signal-count-toggle:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
#signalCounts{--collapse-bg:var(--signal-collapse);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}
#signalCounts .collapse-group{color:var(--white)}
#signalCounts>summary .count-total{white-space:nowrap;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
#signalCounts .count-red{color:var(--red)}
#signalCounts .count-yellow{color:var(--yellow)}
#signalCounts .count-earn{color:var(--text)}
/* 顶部右侧的代码定位：5 位代码 + FIND；字号 16px 防止 iOS 聚焦时自动放大页面 */
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
#signalCounts .signal-member-block{margin:0 0 10px}
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
#signalCounts .target-purple .target-space-high .signal-member-direction{color:var(--target-high)}
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
#signalCounts td .signal-filter-toggle{width:74px;padding:0 4px}
#signalCounts .signal-filter-toggle[aria-pressed="true"]{background:var(--blue);border-color:var(--blue);color:var(--white)}
#signalCounts .signal-filter-toggle:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
#signalCounts .signal-filter-clear:focus-visible{outline:2px solid var(--head-blue-on);outline-offset:2px}
#signalCounts .signal-filter{padding:8.1px 12px;border-top:1px solid var(--list-border);font-size:13px}
#signalCounts .signal-filter[hidden]{display:none}
#signalCounts .signal-filter-head{display:flex;align-items:center;gap:8px;color:var(--head-blue);font-weight:600}
#signalCounts .signal-filter-clear{margin-left:auto;box-sizing:border-box;height:38px;padding:0 10px;font:inherit;font-weight:400;color:var(--text);background:transparent;border:1px solid var(--button-border);border-radius:6px;cursor:pointer}
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
.range52-track{position:relative;flex:1 1 auto;height:2.1px;background:var(--range-track);border-radius:99px}
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
.runbar button:not(.cost-edit-btn){padding:6px 12px;font-size:15px;color:var(--text);background:var(--button-bg);border:1px solid var(--control-border);border-radius:8px;cursor:pointer;font-family:inherit}
.runbar button:disabled{opacity:.5;cursor:not-allowed}
.runbar button.primary{background:var(--blue);border-color:var(--blue);color:var(--white)}
.runbar a.btnlink{padding:6px 12px;font-size:15px;color:var(--text);background:var(--button-bg);border:1px solid var(--control-border);border-radius:8px;text-decoration:none;display:inline-block}
.acts a.btnlink{padding:6px 12px;font-size:15px;color:var(--text);background:var(--button-bg);border:1px solid var(--control-border);border-radius:8px;text-decoration:none;display:inline-block}
.statusrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:-4px 0 14px;font-size:13.5px;line-height:1.5}
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
.group-card>summary .rs-spy{margin-left:auto}
.zone-card{margin:10px -2px 12px;padding-left:4px;padding-right:4px;border:1px solid var(--line);border-radius:9px;background:var(--zone-bg)}
.zone-card .group-bottom{margin:0 4px 0}
.zone-card>summary{display:flex;align-items:center;gap:8px;cursor:pointer;padding:9px 10px;list-style:none;touch-action:manipulation}
.zone-card>summary::-webkit-details-marker{display:none}
.zone-card>summary::before{content:"›";color:var(--dim);font-size:18px;line-height:1}
.zone-card[open]>summary{border-bottom:1px solid var(--line)}
.zone-card>summary .zone-name{color:var(--text);font-size:15px;font-weight:600;white-space:nowrap;flex:none}
.zone-card>summary .group-stats{margin-left:0;display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-start;text-align:left;flex:1 1 auto}
.zone-card>summary:focus-visible{outline:2px solid var(--link-blue);outline-offset:-2px}
.zone-card .quiet{font-size:12px;color:var(--dim);padding:8px 10px 0;line-height:1.6}
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
.stat-badge{display:inline-flex;align-items:center;justify-content:center;flex:none;box-sizing:border-box;width:24px;min-width:24px;height:18px;padding:0 1px;border-radius:999px;border:1.5px solid var(--dim);background:transparent;font-size:14px;font-weight:400;line-height:1;font-variant-numeric:tabular-nums;color:var(--white);position:relative;top:2px}
.stat-badge.red{border-color:var(--red)}
.stat-badge.yellow{border-color:var(--yellow)}
.stat-badge.gray{border-color:var(--dim)}
.stat-badge.missing{border-color:var(--dim);border-style:dashed}
.stat-badge.purple{border-color:var(--purple)}
.stat-badge.green{border-color:var(--green)}
.group-card>summary .grp-name{white-space:nowrap;flex:none}
.macro-card>summary .grp-name{white-space:nowrap;flex:none}
.rs-spy{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--dim)}
.rs-spy.up{color:var(--up)}
.rs-spy.down{color:var(--down)}
.stat-dot{width:11px;height:11px;border-radius:50%;display:inline-block;flex:none}
.stat-dot.red{background:var(--red)}
.stat-dot.yellow{background:var(--yellow)}
.stat-dot.gray{background:var(--dim)}
.stat-dot.green{background:var(--green)}
.stat-dot.purple{background:var(--purple)}
.macro-card>summary .stat-badge{top:0;margin-right:2px}
.market-risk-badge{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.market-risk-badge[data-level="green"]{color:var(--green)}
.macro-card>summary .group-stats{margin-left:auto;display:inline-flex;align-items:center;justify-content:flex-end;text-align:right;gap:0;flex:0 1 auto}
#group_positions_long.zone-card>summary .zone-stats,#group_positions_short.zone-card>summary .zone-stats{margin-left:auto;justify-content:flex-end;margin-right:-1px}
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
.dca-badge{margin-left:auto;white-space:nowrap;color:var(--text);font:inherit;font-variant-numeric:tabular-nums}
.dca-badge.on{color:var(--yellow)}
.runbar .dca-badge{font-size:16px;margin-left:auto}
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
:is(#macroCard, .macro-scope) td.num.dim .unit{font-size:var(--macro-aux)}
:is(#macroCard, .macro-scope) td.num.dim .chg-num{font-size:var(--macro-chg)}
:is(#macroCard, .macro-scope) .oil-explanation{font-size:var(--macro-aux)}
#marketRiskReason{font-size:var(--macro-aux)}
:is(#macroCard, .macro-scope) table{table-layout:fixed}
:is(#macroCard, .macro-scope) td:first-child{width:26%}
:is(#macroCard, .macro-scope) td:nth-child(2){width:33%;text-align:center}
:is(#macroCard, .macro-scope) td:nth-child(3){width:18%;text-align:center}
:is(#macroCard, .macro-scope) td:nth-child(4){width:23%;text-align:center}
:is(#macroCard, .macro-scope) tr.mh td:nth-child(3){padding-left:2px}
:is(#macroCard, .macro-scope) td.num.dim{font-size:var(--macro-chg)}
:is(#macroCard, .macro-scope) tr.mh td{font-size:var(--macro-head);white-space:nowrap;padding:3px 8px}
:is(#macroCard, .macro-scope) td .unit{font-size:var(--macro-aux);line-height:1.15}
:is(#macroCard, .macro-scope) td:nth-child(3) .unit{white-space:nowrap;font-size:var(--macro-aux)}
.dual-row > .dual-body{grid-column:1/-1;grid-row:2;min-width:0;margin:0;padding:0;border:0;background:transparent}
.dual-row > .dual-body[hidden]{display:none}
.day-pnl-card > summary.dp-head{display:flex;align-items:center;gap:8px;padding:0 12px 0 4px;list-style:none;cursor:pointer;box-sizing:border-box}
.dp-head::-webkit-details-marker{display:none}
.dp-nums{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;line-height:1.2}
.dp-amt{font-size:15px;font-weight:600}
.dp-pct{font-size:13px;font-weight:400}
.dp-nums .up{color:var(--up)}
.dp-nums .down{color:var(--down)}
.dual-row .group-bottom{display:none}
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
.dp-head .dp-nums{margin-left:auto;display:flex;flex-direction:row;align-items:baseline;gap:6px;line-height:1.2;white-space:nowrap}
:is(#macroCard, .macro-scope) td{font-size:var(--macro-body);overflow-wrap:anywhere;padding:4px 8px;line-height:1.25;padding-top:3px;padding-bottom:3px}
:is(#macroCard, .macro-scope) tr:is(.red,.yellow,.green) td:first-child{box-shadow:none;position:relative;padding-left:14px}
:is(#macroCard, .macro-scope) tr:is(.red,.yellow,.green) td:first-child::before{content:'';position:absolute;left:6px;top:3px;bottom:3px;width:1.5px;border-radius:1px;background:var(--text);pointer-events:none;z-index:1}
:is(#macroCard, .macro-scope) tr.red td:first-child::before{background:var(--red);width:1.5px}
:is(#macroCard, .macro-scope) tr.yellow td:first-child::before{background:var(--yellow);width:1.5px}
:is(#macroCard, .macro-scope) tr.green td:first-child::before{background:var(--green);width:1px}
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
table.stk tr[data-symbol]{position:relative;display:grid;grid-template-columns:max-content max-content max-content minmax(0,1fr);grid-template-areas:"sym px tgs earn" "cost cost cost cost" "sig sig sig sig";align-items:stretch;width:100%;border-top:1px solid var(--stock-divider);--lamp-line:var(--line)}
table.stk tr[data-symbol]:first-child{border-top:0}
table.stk tr[data-symbol]>td{border:0;box-shadow:none;min-width:0;margin:0}
/* 终稿首格清影：保留:first-child特异性，压过旧红行inset而不改变行伪元素灯条。 */
table.stk tr[data-symbol]>td:first-child{box-shadow:none}
table.stk tr.red[data-symbol]{--lamp-line:var(--red)}
table.stk tr.yellow[data-symbol]{--lamp-line:var(--yellow)}
table.stk tr.green[data-symbol]{--lamp-line:var(--green)}
table.stk tr.gray[data-symbol]{--lamp-line:var(--text)}
table.stk tr[data-symbol]::before{content:"";position:absolute;left:0;top:9.5px;width:2px;height:41.7px;background:var(--lamp-line);pointer-events:none;z-index:2}
table.stk tr[data-symbol]>td.sym{grid-area:sym;position:static;display:block;padding:4px 8px;font-size:14px;line-height:var(--row-height)}
table.stk tr[data-layout="tight"]>td.sym{padding-right:2px}
table.stk tr[data-symbol]>td.sym>.sym-box{display:grid;grid-template-rows:var(--row-height) var(--row-height);align-items:center;width:max-content;max-width:100%;margin:0;padding:0;border:0;background:none;color:var(--text);line-height:var(--row-height);cursor:default}
table.stk tr[data-symbol]>td.sym>.sym-box:has(.tag){color:var(--head-blue-on)}
table.stk tr[data-symbol]>td.sym>.sym-box .tag{display:none}
table.stk tr[data-symbol]>td.sym>.sym-box .note{display:block;margin:0;font-size:11.5px;line-height:var(--row-height);white-space:nowrap}
table.stk tr[data-symbol]>td.sym>.sym-box .unit{display:inline-block;margin-left:4px}
table.stk tr[data-symbol]>td.sym::after{content:"";position:absolute;left:var(--vl-x);top:var(--vl-top);height:var(--vl-h);width:1px;background:var(--section-line);pointer-events:none}
table.stk tr[data-symbol]>td.px{grid-area:px;position:relative;display:block;padding:4px 6px;text-align:left;white-space:nowrap}
table.stk tr[data-layout="tight"]>td.px{padding-left:3px;padding-right:3px}
table.stk tr[data-symbol]>td.px .px-main{display:grid;grid-template-rows:var(--row-height) var(--row-height);align-items:center;margin:0;padding:0 6px 0 0;min-width:0}
table.stk tr[data-symbol]>td.px .px-price{display:block;margin:0;height:var(--row-height);line-height:var(--row-height)}
table.stk tr[data-symbol]>td.px .px-chg{display:block;margin:0;height:var(--row-height);line-height:var(--row-height)}
.px-price{font-size:15px}
.px-chg{font-size:14.5px}
table.stk tr[data-symbol]>td.px::after{content:"";position:absolute;right:0;top:var(--vl-top);height:var(--vl-h);width:1px;background:var(--section-line);pointer-events:none}
table.stk tr[data-symbol]>td.tgs::after{content:"";position:absolute;right:0;top:var(--vl-top);height:var(--vl-h);width:1px;background:var(--section-line);pointer-events:none}
table.stk tr[data-symbol]>td.tgs{grid-area:tgs;position:relative;isolation:isolate;align-self:stretch;display:flex;align-items:flex-start;justify-content:center;padding:4px 3px 4px 2px;line-height:0;text-align:center;width:auto;max-width:none}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt{display:grid;grid-template-columns:auto 1fr;grid-template-rows:var(--row-height) var(--row-height);height:auto;min-height:0;margin:0;padding:0 1px;border:0;background:transparent;column-gap:0;row-gap:0;align-items:center;justify-items:start;line-height:var(--row-height);white-space:nowrap;font-size:13px;font-weight:400;color:var(--purple);font-variant-numeric:tabular-nums}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt>span{height:var(--row-height);line-height:var(--row-height);white-space:nowrap}
table.stk tr[data-symbol]>td.tgs .target-space{grid-area:1/1/2/3}
table.stk tr[data-symbol]>td.tgs .target-price{grid-area:2/1/3/2}
table.stk tr[data-symbol]>td.tgs .target-month{grid-area:2/2/3/3;margin-left:3px}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.high{color:var(--target-high);text-shadow:.2px 0 currentColor,-.2px 0 currentColor}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad{position:relative;color:var(--dim);border-radius:4px}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad::after{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none;background:url("__TARGET_DASH_BORDER__") center/100% 100% no-repeat}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad .target-price{grid-area:1/1/2/3}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad>span:not(.target-price){grid-area:2/1/3/3}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.bad:not(:has(.target-price))>span{grid-area:1/1/2/3}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt.hit{color:var(--white)}
table.stk tr[data-symbol]>td.tgs:has(.tgt.space)::after{display:none}
table.stk tr[data-symbol]>td.tgs:has(.tgt.high)::after{display:none}
table.stk tr[data-symbol]>td.tgs:has(.tgt.hit)::after{display:none}
table.stk tr[data-symbol]>td.tgs:has(.tgt.space)::before{content:"";position:absolute;top:2px;right:0;bottom:2px;left:-1px;z-index:-1;pointer-events:none;background:var(--card);border:1px solid var(--purple);border-radius:3px}
table.stk tr[data-symbol]>td.tgs:has(.tgt.high)::before{content:"";position:absolute;top:2px;right:0;bottom:2px;left:-1px;z-index:-1;pointer-events:none;background:var(--card);border:1px solid var(--purple);border-radius:3px;border-color:var(--target-high)}
table.stk tr[data-symbol]>td.tgs:has(.tgt.hit)::before{content:"";position:absolute;top:2px;right:0;bottom:2px;left:-1px;z-index:-1;pointer-events:none;background:var(--tgt-solid);border-radius:0}
table.stk tr[data-symbol]>td.tgs [data-sign="pos"]{color:var(--tgt-pos)}
#signalCounts .target-purple .percent-change.pos{color:var(--tgt-pos)}
table.stk tr[data-symbol]>td.tgs [data-sign="neg"]{color:var(--tgt-neg)}
#signalCounts .target-purple .percent-change.neg{color:var(--tgt-neg)}
table.stk tr[data-symbol]>td.tgs .tgt.hit [data-sign="pos"]{color:var(--tgt-pos-on)}
table.stk tr[data-symbol]>td.tgs .tgt.hit [data-sign="neg"]{color:var(--tgt-neg-on)}
table.stk tr[data-symbol]>td.lamp-earn{grid-area:earn;position:static;display:block;width:auto;max-width:100%;white-space:nowrap;text-align:right;padding:4px 0;align-self:stretch}
.earn{display:block;margin:0;font-size:15px;font-weight:400;line-height:var(--row-height);white-space:nowrap;color:var(--head-blue-on);overflow:hidden;text-overflow:ellipsis;--timing-color:var(--head-blue-on)}
.earn.soon{color:var(--lamp-yellow);--timing-color:var(--lamp-yellow)}
.earn.urgent{color:var(--lamp-red);--timing-color:var(--lamp-red)}
.earn>.unit{display:block;margin-left:0;height:var(--row-height);line-height:var(--row-height);white-space:nowrap;letter-spacing:0;font-size:15px;color:var(--dim)}
.earn>.unit::before{content:attr(data-timing);color:var(--timing-color)}
.earn>.cd,#signalCounts .earnings-week-meta .cd,#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd{font-size:inherit;letter-spacing:0;font-weight:400;color:var(--white);margin-right:-.5em}
.earn>.cd.tbd,#signalCounts .earnings-week-meta .cd.tbd,#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd.tbd{color:var(--head-blue-on)}
.earn>.unit.is-countdown,#signalCounts .earnings-week-bj.is-countdown{color:var(--white)}
table.stk tr[data-symbol]>td.cost-line{grid-area:cost;display:flex;align-items:center;justify-content:space-between;gap:6px;padding:3px 8px;margin:5px 0 0;background:var(--cost-row-bg);border-radius:3px;color:var(--text);white-space:nowrap;font-size:15px;line-height:22.5px;font-variant-numeric:tabular-nums}
table.stk tr[data-symbol]>td.cost-line[hidden]{display:none}
table.stk tr[data-symbol]>td.cost-line>span{font-size:inherit}
table.stk tr[data-symbol]>td.cost-line[data-pnl="loss"]{background:var(--pnl-loss-bg)}
table.stk tr[data-symbol]>td.cost-line[data-pnl="gain"]{background:var(--pnl-gain-bg)}
table.stk tr[data-symbol]>td.sig{grid-area:sig;display:block;padding:10.25px 10px 7px;font-size:14.5px;line-height:21.75px;--sig-col-gap:18px;--sig-row-gap:7px}
.sig-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:var(--sig-col-gap);align-items:start;min-width:0}
.sig-col{display:flex;flex-direction:column;gap:var(--sig-row-gap);min-width:0}
/* 加号改为1.1px细线绘制（原字形放大后笔画约1.7px偏粗）；尺寸、墨迹中心沿用原字形放大后的位置。 */
.sig-cols[data-combo="1"]{position:relative;--combo-size:10.5px;--combo-cy:10.6px;--combo-stroke:1.1px;--combo-color:var(--dim)}
.sig-cols[data-combo="1"]::after{content:"";position:absolute;left:50%;top:calc(var(--combo-cy) - var(--combo-size) / 2);width:var(--combo-size);height:var(--combo-size);transform:translateX(-50%);background:linear-gradient(var(--combo-color),var(--combo-color)) center/100% var(--combo-stroke) no-repeat,linear-gradient(var(--combo-color),var(--combo-color)) center/var(--combo-stroke) 100% no-repeat;pointer-events:none}
/* 中线右半区比左半区多4px（左9px、右13px）；加号仍压在中线上。 */
.sig-cols>.sig-col:last-child{padding-left:4px}
.sig-cols[data-combo-level="red"]{--combo-color:var(--lamp-red)}
.sig-cols[data-combo-level="yellow"]{--combo-color:var(--lamp-yellow)}
.sig-cols[data-combo-level="gray"]{--combo-color:var(--text)}

.stk td.sig .signal-item{display:block;min-width:0;overflow-wrap:anywhere;padding-left:8px;text-indent:-8px}
/* 全角标点只收掉自己字框里的空白半格，汉字/数字间距不动：
   「，」「）」字形在字框左半、「（」在右半，另一半本来就是空的，所以各收0.5字不会盖到邻字。 */
.stk td.sig .cm,.stk td.sig .pr{margin-right:-.5em}
.stk td.sig .pl{margin-left:-.5em}
.stk td.sig .signal-item::before{content:"";display:inline-block;width:3px;height:10px;margin-right:5px;border-radius:1px;background:currentColor;vertical-align:middle}
.stk td.sig .signal-item:has(.sig-zone){padding-left:0;text-indent:0}
.stk td.sig .signal-item:has(.sig-zone)::before{display:none}
.stk td.sig .sig-zone{display:block;min-width:0;overflow-wrap:anywhere;padding-left:8px;text-indent:-8px}
.stk td.sig .sig-zone::before{content:"";display:inline-block;width:3px;height:10px;margin-right:5px;border-radius:1px;background:currentColor;vertical-align:middle}
.stk td.sig .sig-sep{display:none}
.stk td.sig .signal-separator{display:none}
.stk td.sig .signal-item[data-signal-level="green"]{color:var(--green)}
.stk td.sig .lvtag.add{height:auto;min-height:0;margin:0;border:0;border-radius:0;background:transparent;white-space:normal;color:var(--dim);font:inherit}
.stk td.sig .lvtag.cut{height:auto;min-height:0;margin:0;border:0;border-radius:0;background:transparent;white-space:normal;color:var(--dim);font:inherit}
.stk td.sig .lvtag:not([data-near="1"])::before{background:transparent}
.stk td.sig .lvtag.add[data-near="1"]{color:var(--orange)}
.stk td.sig .lvtag.cut[data-near="1"]{color:var(--cyan)}
.stk tr.range52-row{display:block;width:100%}
.stk tr.gray:not([data-symbol]){display:block;width:100%}
.stk tr.range52-row>td{display:block;width:100%}
.stk tr.gray:not([data-symbol])>td{display:block;width:100%}
table.stk tr.range52-row>td:first-child{position:relative;padding:0 10px 12px 10px;border:0;box-shadow:none}
/* 保留终稿非持仓范围条下5px留白，不恢复旧空cost-info节点。 */
table.stk tr[data-symbol]:not(:has(>td.cost-line:not([hidden])))+tr.range52-row>td:first-child{padding-bottom:17px}
.stk th{font-size:13px;color:var(--dim);font-weight:600;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
#group_positions[open]{padding-bottom:2px}
#group_positions>.group-bottom{display:none}
#group_positions details.quiet-list>summary{display:none}
#group_positions_short.zone-card{box-sizing:border-box;width:100%;background:none;border:1px solid var(--frame-green);border-radius:9px;margin:9px 0 0}
#runLightTxt .run-timestamp{font-size:calc(1em + 1px);line-height:1}
#signalCounts .signal-extra-row{--chip-w:74px;--xr:12px;position:relative;display:block;padding:0;border-top:1px solid var(--line)}
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
#signalCounts .signal-filter .signal-member-block h5{margin:6px 0 4px;color:var(--head-blue);font-size:13px;font-weight:600}
#signalCounts .signal-filter .signal-member-block+.signal-member-block{margin-top:10px}
#signalCounts .signal-filter .signal-member-block ul{margin:0}
#signalCounts .signal-filter .signal-member-belong:empty{display:none}
@media(max-width:600px){
:root{--status-font-size:14.5px}
.sig-cols[data-combo="1"]{--combo-size:11.3px;--combo-cy:9.7px}
.stk td.sig .signal-item[data-signal-kind="amp"]{white-space:nowrap;overflow-wrap:normal}
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
.zone-card>summary{line-height:22.5px;box-sizing:border-box;min-height:48px;padding-top:0;padding-bottom:0}
.group-card>summary.grp{line-height:24px;box-sizing:border-box;min-height:56px;padding-top:0;padding-bottom:0;font-size:18px}
.macro-card>summary.grp{line-height:24px;box-sizing:border-box;min-height:47px;padding-top:0;padding-bottom:0;font-size:18px}
.day-pnl-card>summary.grp{line-height:24px;box-sizing:border-box;min-height:47px;padding-top:0;padding-bottom:0;font-size:18px}
.group-card>summary .group-stats{line-height:24px;font-size:16px;gap:6px}
.macro-card>summary .group-stats{line-height:24px;font-size:16px;gap:6px}
.zone-card>summary .group-stats{font-size:14px;line-height:22.5px;gap:6px}
:is(#macroCard, .macro-scope) td:first-child{width:29%}
:is(#macroCard, .macro-scope) td:nth-child(2){width:32%}
:is(#macroCard, .macro-scope) td:nth-child(3){width:18%}
:is(#macroCard, .macro-scope) td:nth-child(4){width:21%}
:is(#macroCard, .macro-scope) td{padding:3px 6px}
:is(#macroCard, .macro-scope) tr.mh td{padding:3px 6px}
:is(#macroCard, .macro-scope) td:nth-child(3) .unit{white-space:normal}
:is(#macroCard, .macro-scope) .oil-reference-row td{padding-top:6px;padding-bottom:6px}
:is(#macroCard, .macro-scope) .oil-reference-row td:last-child{line-height:20.25px}
:is(#macroCard, .macro-scope) .oil-reference-row td:first-child::before{top:6px;bottom:6px}
table.stk tr[data-symbol]>td.sym{font-size:17px}
table.stk tr[data-symbol]>td.sym>.sym-box{line-height:20.8px}
table.stk tr[data-symbol]>td.sym>.sym-box .note{font-size:14.5px}
table.stk tr[data-symbol]>td.px .px-price{font-size:16px}
table.stk tr[data-symbol]>td.px .px-chg{font-size:16px}
table.stk tr[data-symbol]>td.tgs .lvtag.tgt{font-size:15px}
table.stk tr[data-symbol]>td.sig{padding:9.5px 5px 7px;font-size:14.5px;line-height:19.57px}
table.stk tr.range52-row>td:first-child{padding:0 6px 12px 6px}
table.stk tr[data-symbol]:not(:has(>td.cost-line:not([hidden])))+tr.range52-row>td:first-child{padding-bottom:19px}
table.stk tr[data-symbol]::before{top:7px;height:42.7px}
.stk th{padding:7px 5px;font-size:12px}
#signalCounts .signal-extra-row{--chip-w:74px;--xr:8px;padding:0}
.grp-name{font-size:20px;line-height:24px}
.runbar button:not(.cost-edit-btn){padding:8px 14px;font-size:20px;line-height:21.6px}
.sub{padding-left:16px;padding-right:16px;margin-bottom:8px;line-height:18px}
.runlight{line-height:16.88px}
.stat-badge{width:24px;min-width:24px;height:18px;padding:0 1px;font-size:17px;line-height:16px}
#signalCounts .signal-count-toggle{font-size:17px;line-height:16px}
.rs-spy{font-size:16px;line-height:24px}
.zone-card>summary .zone-name{font-size:16px;line-height:22.5px}
.zone-card>summary .zone-num{font-size:16px;line-height:22.5px}
.dual-body>.cost-contrib .cnsym{font-size:15px;line-height:15.75px}
.dual-body>.cost-contrib .cncn{font-size:14px;line-height:15.75px}
.dual-body>.cost-contrib .ca,.dual-body>.cost-contrib .cv{font-size:14px;line-height:15.75px}
#group_positions>.asset-row .cost-legend b{font-size:14px;line-height:19.2px}
#signalCounts .signal-member-symbol{font-size:15px;line-height:19.5px}
#signalCounts .signal-member-jump .note{font-size:14px;line-height:18px}
#signalCounts .signal-member-list .signal-member-symbol{line-height:16.25px}
#signalCounts .signal-member-list .note{line-height:15px}
#signalCounts .signal-member-belong{font-size:14px;line-height:18px}
#signalCounts .signal-member-list .signal-member-belong{line-height:15px}
#signalCounts .target-purple .signal-member-symbol{line-height:19.5px}
#signalCounts .target-purple .note,#signalCounts .target-purple .signal-member-belong{line-height:18px}
#signalCounts .signal-member-metric{font-size:14.5px;line-height:16.88px}
#signalCounts .signal-filter-detail{font-size:14.5px;line-height:20.25px}
#signalCounts .signal-filter-condition{font-size:14.5px;line-height:20.25px}
#signalCounts .earnings-week-meta{font-size:14.5px;line-height:20.25px}
#signalCounts .signal-member-direction{font-size:14.5px;line-height:20.25px}
#signalCounts .signal-filter-head>span{font-size:13px;line-height:19.5px}
#signalCounts .signal-filter-clear{font-size:13px;line-height:19.5px}
#signalCounts td .signal-filter-toggle{padding:0 4px;font-size:15px;line-height:15.95px}
#signalCounts .signal-extra-row>.signal-filter-toggle{font-size:15px;line-height:16.68px}
#signalCounts .signal-extra-row>details>summary{font-size:16px;line-height:22px}
#signalCounts .signal-member-block h5{font-size:15px;line-height:16.25px}
#signalCounts .target-purple>h5{font-size:15px;line-height:16.25px}
#signalCounts .pnl-extra h5{font-size:15px;line-height:16.25px}
#signalCounts>summary .count-total{font-size:16px;line-height:24px}
.market-risk-badge{font-size:16px;line-height:24px}
.monitor-note{font-size:16px;line-height:24px}
.runbar .dca-badge{margin-left:auto;font-size:18px;line-height:24px}
.range52-hi{font-size:13px;line-height:11px}
.range52-lo{font-size:13px;line-height:11px}
.range52-gap{font-size:13px;line-height:11px}
.quiet-list>summary{font-size:15px;line-height:19.5px}
.acts .board-return{color:var(--link-blue);background:transparent;border:1px solid var(--control-border);border-radius:8px;padding:7px 15px;margin:0;font-size:16px;line-height:24px}
.acts a.btnlink{color:var(--link-blue);background:transparent;border:1px solid var(--control-border);border-radius:8px;padding:7px 15px;margin:0;font-size:16px;line-height:24px}
.autoplan{font-size:16px;line-height:23.8px}
}
@media(min-width:601px){
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
.dual-body>.cost-contrib li{grid-template-columns:minmax(84px,.5fr) 66px 62px minmax(0,1fr);column-gap:10px;padding-right:32px}
.dual-body>.cost-contrib li>.ca{text-align:left}
.dual-body>.cost-contrib .cbar{margin-left:31px;max-width:300px}
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
#glossary-chg-table td,#glossary-amp-table td,#glossary-vol-table td{font-variant-numeric:tabular-nums;white-space:nowrap}
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
// 持仓父组常驻展开；只锁父summary，不干预长短子框或它们的收起滚动补偿。
(function lockPositionsOpen() {
  document.querySelectorAll('#group_positions[data-open-locked="1"]').forEach(function(card) {
    card.open = true;
    const summary = card.querySelector(':scope > summary');
    if (summary) {
      function keepOpen(event) {
        if (event.type === 'keydown' && !['Enter', ' ', 'Spacebar'].includes(event.key)) return;
        event.preventDefault();
        card.open = true;
      }
      summary.addEventListener('click', keepOpen);
      summary.addEventListener('keydown', keepOpen);
    }
    card.addEventListener('toggle', function(event) {
      if (event.target === card && !card.open) card.open = true;
    });
  });
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
  function note(row, tight) {
    const node = row.querySelector('td.sym .note[data-full]');
    if (!node) return;
    const full = node.dataset.full, chars = Array.from(full);
    const cut = tight && chars.length > 4;
    const text = cut ? chars.slice(0, 4).join('') + '…' : full;
    if (node.textContent !== text) node.textContent = text;
    if (cut) node.title = full;
    else node.removeAttribute('title');
  }
  function layoutEarn(earn, cell) {
    const unit = earn.querySelector(':scope > .unit');
    const countdown = earn.querySelector(':scope > .cd');
    const text = Array.from(earn.childNodes).find(function(n) {
      return n.nodeType === Node.TEXT_NODE && n.textContent.trim();
    });
    if (!unit || !text || !cell.clientWidth) return 'loose';
    const main = earn.dataset.main;
    const matched = main.match(/^(.*?)\s*(时段待定|盘前|盘后|盘中)\s*$/);
    const base = matched ? matched[1].trimEnd() : main;
    const timing = matched ? matched[2] : '';
    const cd = countdown ? countdown.textContent : '';
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;left:0;top:0;letter-spacing:0';
    earn.appendChild(probe);
    const width = function(value) { probe.textContent = value; return probe.getBoundingClientRect().width; };
    const available = cell.clientWidth - (parseFloat(getComputedStyle(cell).paddingLeft) || 0) - .5;
    const adjust = countdown ? parseFloat(getComputedStyle(countdown).marginRight) || 0 : 0;
    let fullWidth = width(cd + main) + adjust;
    const baseWidth = width(cd + base) + adjust;
    const unitWidth = width(unit.textContent);
    const movedWidth = width(timing ? timing + '·' + unit.textContent : unit.textContent);
    probe.remove();
    let compressed = false;
    if (fullWidth > available && unitWidth <= available && fullWidth - .25 * main.length <= available) {
      compressed = true;
      fullWidth -= .25 * main.length;
    }
    const mode = fullWidth <= available && unitWidth <= available ? 'full'
      : timing && baseWidth <= available && movedWidth <= available ? 'move' : 'drop';
    earn.style.letterSpacing = mode === 'full' && compressed ? '-.25px' : '';
    text.textContent = mode === 'full' ? main : base;
    unit.dataset.timing = mode === 'move' ? timing + '·' : '';
    earn.dataset.mode = mode;
    return mode === 'full' && !compressed ? 'loose' : 'tight';
  }
  function balanceDescriptions(row) {
    const cols = row.querySelector('td.sig > .sig-cols');
    if (!cols) return;
    const columns = Array.from(cols.children).filter(function(n) { return n.classList.contains('sig-col'); });
    if (columns.length !== 2) return;
    const automatic = Array.from(cols.querySelectorAll('.signal-item[data-signal-flow="auto"]'))
      .sort(function(a, b) { return Number(a.dataset.signalOrder) - Number(b.dataset.signalOrder); });
    if (!automatic.length) return;
    const caps = columns.map(function(c) { return Array.from(c.children).filter(function(n) { return n.matches('.lvtag.add,.lvtag.cut'); }); });
    const planned = columns.map(function(c) { return Array.from(c.children).filter(function(n) {
      return !n.matches('[data-signal-flow="auto"],.lvtag.add,.lvtag.cut');
    }); });
    // 终稿 reflowSignalColumns 在父 td.sig 仍为双列 grid、fontPatch 尚未执行时
    // 按列高分配：测量宽度=(最终内容宽度-列间距)/2，13.5px，手机1.45/桌面1.5。
    // 只在不可见副本复现这一测量坐标系；不使真实描述闪回窄列或重引演示补丁。
    const probe = cols.cloneNode(false);
    probe.removeAttribute('data-combo');
    const gap = parseFloat(getComputedStyle(cols).columnGap) || 0;
    probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;left:0;top:0;font-size:13.5px;line-height:'
      + (matchMedia('(max-width:600px)').matches ? '1.45' : '1.5')
      + ';width:' + Math.max(0, (cols.getBoundingClientRect().width - gap) / 2) + 'px';
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
    const cell = row.querySelector('td.sym'), box = cell && cell.querySelector('.sym-box');
    if (!box) return;
    const code = Array.from(box.childNodes).find(function(n) { return n.nodeType === Node.TEXT_NODE && n.textContent.trim(); });
    const name = box.querySelector('.note');
    if (!code || !name) return;
    const ink = function(n) { const range = document.createRange(); range.selectNodeContents(n); return range.getBoundingClientRect(); };
    const a = ink(code), b = ink(name), root = row.getBoundingClientRect(), rect = cell.getBoundingClientRect();
    if (!a.height || !b.height) return;
    const top = Math.min(a.top, b.top) - rect.top;
    row.style.setProperty('--vl-x', (rect.right - root.left).toFixed(2) + 'px');
    row.style.setProperty('--vl-top', top.toFixed(2) + 'px');
    row.style.setProperty('--vl-h', (Math.max(a.bottom, b.bottom) - Math.min(a.top, b.top)).toFixed(2) + 'px');
  }
  function layout(row) {
    if (!row.getBoundingClientRect().width) return; // 折叠行展开后才测量。
    row.dataset.layout = 'loose';
    note(row, false);
    const cell = row.querySelector(':scope > td.lamp-earn');
    const earn = cell && cell.querySelector('.earn[data-main]');
    if (earn && layoutEarn(earn, cell) === 'tight') {
      row.dataset.layout = 'tight';
      note(row, true);
      layoutEarn(earn, cell);
    }
    balanceDescriptions(row);
    verticalLines(row);
  }
  function schedule(scope) {
    rows.forEach(function(row) { if (!scope || scope === row || scope.contains(row)) dirty.add(row); });
    if (!dirty.size || frame) return;
    frame = requestAnimationFrame(function() {
      frame = 0;
      const pending = Array.from(dirty); dirty.clear();
      pending.forEach(layout);
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
      card.open = card.id === 'signalCounts' || card.id === 'group_positions';
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
// 屏幕最上面 1/6 留给 iOS 灵动岛/状态栏：展开的分组、清单一律从 1/6 高度线开始显示，不再贴着屏幕顶边。
function topReserve() {
  var vh = window.innerHeight || document.documentElement.clientHeight || 0;
  return Math.round(vh / 6);
}
function revealSignalPanel(panel) {
  // 把刚展开的清单带进视口：能整块放下就整块露出，放不下就从 1/6 线开始对齐。
  var pad = 12;
  var top = topReserve();
  var vh = window.innerHeight || document.documentElement.clientHeight || 0;
  if (!vh) return;
  var rect = panel.getBoundingClientRect();
  if (rect.top >= top && rect.bottom <= vh - pad) return;   // 已经完整可见（且不在灵动岛区）就不动，避免无谓跳动
  var y = window.pageYOffset || document.documentElement.scrollTop || 0;
  var target = (rect.height + top + pad <= vh && rect.bottom > vh - pad)
    ? y + rect.bottom - (vh - pad)                          // 整块往上推
    : y + rect.top - top;                                   // 其余：从 1/6 线开始看起
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
// 当日、风险、统计和固定展开的「持仓」父卡不参与。只响应点标题（页面加载时的默认展开不触发）。
document.addEventListener("click", function(event) {
  const summary = event.target instanceof Element ? event.target.closest("details[id^='group_'] > summary") : null;
  if (!summary || !(window.MM_SETTINGS && window.MM_SETTINGS.group_single_open === 1)) return;
  const d = summary.parentElement;
  if (!d || d.dataset.openLocked) return;
  const topBefore = d.getBoundingClientRect().top;
  requestAnimationFrame(function() {
    if (!d.open) return;
    document.querySelectorAll("details[id^='group_'][open]:not([data-open-locked])").forEach(function(other) {
      if (other !== d) other.open = false;
    });
    // 收起上面的分组会把页面拉上去：先瞬间拉回原来的屏幕位置，再平滑滚到 1/6 高度线（上面留给灵动岛）。
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
// 下次再展开（长/短持仓区尤其明显：关掉区再打开）刚点开的那份清单还是开着的。
function resetQuietLists(card) {
  card.querySelectorAll("details.quiet-list[open]").forEach(function(sub) {
    if (sub.closest('#group_positions')) return; // 持仓静态常开，只有外层长/短区可折叠。
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
    } else { buf += ch; }
  }
  flush();
}

// 多选清单按持仓/重点/其他分组，使用真实条件数据排序，不读取或重写已生成的展示节点。
function signalMemberKey(member) {
  const weights = {red:5, yellow:4, gray:3, white:3, purple:2, green:1};
  const order = member.o == null ? 9 : Number(member.o);
  const bucket = order === 0 && ['长持仓', '短持仓'].includes(member.g) ? 0 : order === 1 ? 1 : 2;
  let red = 0, score = 0, count = 0, amplitude = 0;
  member.details.forEach(function(part) {
    red += part.l === 'red' ? 1 : 0;
    score += weights[part.l] || 0;
    for (const match of part.t.matchAll(/(\d+)\s*条/g)) count = Math.max(count, Number(match[1]));
    for (const match of part.t.matchAll(/(-?\d+(?:\.\d+)?)\s*[%×]/g)) amplitude = Math.max(amplitude, Math.abs(Number(match[1])));
  });
  return [bucket, -(weights[member.l] || 0), -red, -score, -count, -amplitude, order, member.s];
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
    records.forEach(function(record) {
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
        if (part.e.c) {
          const cd = document.createElement("span");
          cd.className = part.e.tbd ? "cd tbd" : "cd";
          cd.textContent = part.e.c;
          condition.append(cd);
        }
        condition.appendChild(document.createTextNode(part.t));
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
    // 保留扁平清单原有的分隔线；拆组后不靠DOM重排回补computed样式。
    if (!record.first) li.classList.add("has-separator");
    list.append(li);
    });
    block.append(heading, list);
    box.append(block);
  });
}
document.addEventListener("click", function(event) {
  if (!(event.target instanceof Element)) return;
  const closeButton = event.target.closest(".collapse-group");
  if (closeButton) {
    const card = closeButton.closest("details");
    if (!card) return;
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
  let node = target;
  while (node && node !== document.body) {   // 从里到外把所有祖先 details 打开
    if (node.tagName === "DETAILS") node.open = true;
    node = node.parentElement;
  }
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
    document.querySelectorAll("details[id^='group_'][open]:not([data-open-locked])").forEach(function(d) {
      if (!d.contains(target)) d.open = false;
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
