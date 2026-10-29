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
line-34|rule-clickable|h5|可点红灯 可点✓ 标记已读
line-35|rule-prices|h5|加仓价／减仓价 须设置有效价位
line-36||p|加仓价 · 所有分组：现价≤设置价×${add_factor} → 可点红灯。
line-37||p|加仓价提醒标记已读 → 白灯。
line-38||p|减仓价 · 持仓分组：现价≥设置价×${reduce_factor} → 可点红灯。
line-39||p|减仓价提醒标记已读 → 白灯。
line-40||p|减仓价：重点、其他分组不适用。
line-41|rule-cost-loss|h5|成本浮亏 持仓分组
line-42||p|浮亏≥10% → 可点红灯；
line-43||p|浮亏≥20%加重文字 → 可点红灯。
line-44||p|浮亏提醒标记已读 → 白灯。
line-45||p|浮亏10%／20%每只标的只计一次。
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
<h5 id="glossary-ack">可点红灯与已读</h5>
<p>加仓价、减仓价、成本浮亏：点✓已读，再点撤销。已读显示白字删除线，统计由对应红列转白列。</p>
<p>条件消失即清除已读，再次触发重新提醒。浮亏10%／20%分档记录、每只只计一次；升降档切换提醒，低于10%清除浮亏已读。</p>
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
<p>盘中量比＝（当日累计量÷已开盘分钟）÷（${vol_ratio_base}日均量÷全日分钟）。开盘成交偏快、折算值偏高，故盘中门槛按已开盘进度放宽：不足 45／60／90／120 分钟约 1.69／1.45／1.30／1.13 倍，之后回到固定门槛（红≥${vol_ratio_red}×、黄≥${vol_ratio}×）；放宽的是门槛，显示仍是原始折算值。</p>
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
<p>用美东财报日期对比北京今天，盘前／盘后不平移。名单含今天至+${earn_list_days}天，另留昨天红字；红字保留期结束后才检查下一次。</p>
<p>失败时沿用有效旧日期；“待公布”仅表示本站未取得日期。</p>
<p>已知时段显示盘前／盘后／盘中及北京落点，未知不猜钟点。</p>
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
        "glossary-ack": "read-terms", "glossary-chg": "change-terms",
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
    values["add_factor"] = round(1 + values["trigger_gap_pct"] / 100, 6)
    values["reduce_factor"] = round(1 - values["reduce_gap_pct"] / 100, 6)
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


def script_asset(base, filename):
    return '<script src="' + html.escape(asset_url(base, filename), quote=True) + '"></script>'


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
    for name in ("run-status.js", "list-import.js"):
        pattern = r'(?<=src=")\./' + re.escape(name) + r'(?:\?v=[^"\s]*)?(?=")'
        result, count = re.subn(pattern, lambda _: asset_url(base, name), result)
        if count != 1:
            raise ValueError(f"edit.html 必须有且仅有一个 {name} 引用")
    # 设置页展示规则说明，样式取自同一份 RULES_CSS，不在 edit.html 里另抄一份。
    pattern = r"/\* MM_RULECSS_BEGIN[^\n]*\*/.*?/\* MM_RULECSS_END \*/"
    generated = ("/* MM_RULECSS_BEGIN：由 page_assets 生成，不手改 */\n"
                 + RULES_CSS.rstrip("\n") + "\n/* MM_RULECSS_END */").replace("\n", newline)
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

# 页面 CSS 与纯展示交互从 monitor 原样迁移，公开名称由 monitor 重导出。
PAGE_CSS = """:root{
  --bg:#0f1115; --card:#171a21; --line:#252a33; --text:#e6e8ec; --dim:#8b93a1;
  --green:#3fb950; --yellow:#d29922; --red:#f85149; --up:#3fb950; --down:#f85149;
  --blue:#2f6bd8; --purple:#b084ff; --target-high:#dc62b8;
  --signal-bg:#182131; --collapse-bg:var(--signal-bg);
  /* 统计面板与「展开的分组卡」共用的外框色：两边同色同宽，不再各写一份 */
  --signal-border:#6686c6; --frame-w:1.5px;
  /* 统计名单方向三角尺寸；指标描述使用自身条件文字颜色 */
  --lamp-size:14px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;
  padding:env(safe-area-inset-top) 14px 40px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:19px;margin:18px 0 4px;font-weight:600;text-align:center}
/* 站名：用「上涨绿」--up，与涨跌箭头同色系；只这一处，其它标题颜色不动 */
h1.brand{color:var(--up)}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:6px 4px;margin-bottom:14px;overflow:hidden}
/* 持仓卡：2%绿叠层；长持仓再叠2%（合成约4%），不影响文字和灯级。 */
#group_positions{background:linear-gradient(rgba(46,160,67,.02),rgba(46,160,67,.02)),var(--card);--collapse-bg:rgba(46,160,67,.15)}
#group_positions_long.zone-card{background:rgba(46,160,67,.02)}
h2{font-size:13px;color:var(--dim);font-weight:600;margin:10px 12px 8px;letter-spacing:.3px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:9px 10px;border-top:1px solid var(--line);vertical-align:middle}
tr:first-child td{border-top:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.dim{color:var(--dim);font-size:13px}
.sym{font-weight:600}
.note{color:var(--dim);font-weight:400;font-size:12px;margin-left:6px}
.tag{display:inline-block;margin-left:6px;padding:1px 5px;border-radius:4px;
  font-size:11px;font-weight:600;letter-spacing:.3px;vertical-align:1px;
  color:#8fb8f0;background:rgba(107,163,240,.14);border:1px solid rgba(107,163,240,.32)}
.tag.quote-note{color:var(--dim);background:rgba(139,147,161,.1);border-color:var(--line);font-weight:400}
.lvs{display:block;margin:4px 0 0;line-height:1.5}
.lvtag{display:inline-block;margin:0 4px 2px 0;padding:0 7px;border-radius:999px;border:1px solid;
  background:transparent;font-size:11px;font-weight:400;line-height:17px;white-space:nowrap;font-variant-numeric:tabular-nums}
.lvtag.add{color:#ffa94d;border-color:rgba(255,169,77,.6)}
.lvtag.cut{color:#4dd0e1;border-color:rgba(77,208,225,.6)}
.tgs{display:block;margin:3px 0 0;line-height:1.4;white-space:normal}
.tgs .lvtag{margin:0;padding:1px 5px;border-radius:4px;line-height:1.35;white-space:normal;text-align:center}
.tgs .lvtag span{display:inline-block;white-space:nowrap}
.lvtag.tgt{position:relative;color:var(--purple);border-color:var(--dim)}
.lvtag.tgt.low{border:1px solid transparent;background:transparent}
.lvtag.tgt.low::after,.lvtag.tgt.bad::after{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none;background:url("__TARGET_DASH_BORDER__") center/100% 100% no-repeat}
/* 描边绘在原1px边框内部，文字等宽视觉加粗：分档不增加尺寸、不移动文字。 */
.lvtag.tgt.space,.lvtag.tgt.high{border-color:transparent;background:transparent}
.lvtag.tgt.space::after,.lvtag.tgt.high::after{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none}
.lvtag.tgt.space::after{border:1.5px solid var(--purple)}
.lvtag.tgt.high{color:var(--target-high);text-shadow:.2px 0 currentColor,-.2px 0 currentColor}
.lvtag.tgt.high::after{border:1.5px solid var(--target-high)}
.lvtag.tgt.bad{color:var(--dim);background:transparent;border:1px solid transparent;text-shadow:none}
.lvtag.tgt.hit::after{content:none}
.lvtag.tgt.hit{color:#fff;background:#8957e5;border:1px solid #8957e5;text-shadow:none}
.tnote,.sig .tp{color:var(--purple)}
.sig{font-size:13.5px;color:var(--text);overflow-wrap:anywhere}
.signal-magenta{color:#ef8de4} .signal-red{color:#ff8580} .signal-yellow{color:#f0c674} .signal-cyan{color:#4dd0e1}
.signal-item{color:var(--dim)}
.signal-item[data-signal-level="red"]{color:#ff8580}
.signal-item[data-signal-level="yellow"]{color:#f0c674}
.signal-item[data-signal-level="gray"]{color:var(--text)}
.signal-item[data-signal-level="purple"]{color:var(--purple)}
.signal-separator{color:var(--dim);margin:0 2px}
.group-bottom{padding:0;margin:0 -4px -6px;border-top:1px solid var(--line);text-align:center;line-height:0}
.collapse-group{width:100%;height:26px;padding:0;font:inherit;color:var(--dim);
  display:flex;align-items:center;justify-content:center;box-sizing:border-box;
  background:var(--collapse-bg);border:0;border-radius:6px;cursor:pointer;touch-action:manipulation}
.collapse-group .collapse-icon{font-size:16px;line-height:1}
.collapse-group:hover{color:var(--text);background:var(--collapse-bg)}
.collapse-group:focus-visible,.signal-count-toggle:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
/* 统计面板：只剩一圈外框，不再铺底色（卡片底、名单展开面板底、紫灯/财报行底全部去掉）。
   外框四边同宽 1.5px，左侧蓝条随之取消（手机上外框左右各留 4px 贴着屏幕边）。 */
#signalCounts{--collapse-bg:rgba(102,134,198,.6);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}
#signalCounts .collapse-group{color:#fff}
#signalCounts>summary{color:#bfd5ff}
#signalCounts>summary .count-total{white-space:nowrap;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
#signalCounts .count-red{color:var(--red)} #signalCounts .count-yellow{color:var(--yellow)}
#signalCounts .count-earn{color:var(--text)}
#signalCounts table{table-layout:fixed}
#signalCounts td{padding:6.3px 5px;font-size:13px;line-height:1.25;overflow-wrap:anywhere;vertical-align:middle}
#signalCounts td.num{text-align:center}   /* 计数列居中：破折号与展开按钮对齐 */
#signalCounts td:first-child{width:26%}
/* 数字胶囊 44px 写死：宽度不跟着位数变。数字右对齐到「个位正好压在胶囊中线」的位置 ——
   个位右缘固定在 x=26（= 中线 22 + 半个字宽 4），两位数时十位向左长、个位仍停在中线上。
   右边 17px 内边距就是给三角预留的位置；white-space:nowrap 保证任何情况下都不折行。 */
#signalCounts .signal-count-toggle{box-sizing:border-box;height:38px;width:44px;max-width:100%;
  position:relative;padding:0 17px 0 4px;
  color:#bfd5ff;font:inherit;font-size:14px;line-height:1;background:transparent;border:1px solid #4c6591;border-radius:6px;cursor:pointer;
  white-space:nowrap;display:inline-flex;align-items:center;justify-content:flex-end;touch-action:manipulation}
/* 三角字号仍是 16px（不放大也不缩小），只从「跟在数字后面」改成绝对定位贴到胶囊右侧
   （距右边框 2px）：数字回到中线，三角去右边，左右留白都被用上，胶囊仍是 44px。 */
#signalCounts .signal-count-toggle::after{content:"▾";font-size:16px;line-height:1;
  position:absolute;right:2px;top:50%;transform:translateY(-50%)}
#signalCounts .signal-count-toggle[aria-expanded="true"]::after{content:"▴"}
#signalCounts .signal-members-row>td{padding:0;width:auto;border-top:0}
#signalCounts .signal-members-panel{padding:10px 12px;border-top:1px solid #465e87;text-align:left}
#signalCounts .signal-members-panel[hidden]{display:none}
#signalCounts .signal-members-panel ul{list-style:none;margin:0;padding-left:2px}
#signalCounts .signal-members-panel li{margin:5px 0;overflow-wrap:anywhere}
/* 名称前竖条共用尺寸与6px间距；单条件用伪元素，多选与紫灯用同样的span。 */
#signalCounts .signal-name-lamp,
#signalCounts .signal-members-panel li:not(.signal-member-split)::before,
#signalCounts .earnings-week li::before{content:"";display:inline-block;width:3px;height:12px;border-radius:1px;background:var(--name-lamp,var(--dim));margin-right:6px;vertical-align:-1px}
#signalCounts .signal-members-panel.tone-red,#signalCounts .signal-name-lamp.red,#signalCounts .earnings-week li.lamp-red{--name-lamp:var(--red)}
#signalCounts .signal-members-panel.tone-yellow,#signalCounts .signal-name-lamp.yellow,#signalCounts .earnings-week li.lamp-yellow{--name-lamp:var(--yellow)}
#signalCounts .signal-name-lamp.green,#signalCounts .earnings-week li.lamp-green{--name-lamp:var(--green)}
#signalCounts .text-only .signal-member-list,#signalCounts .signal-name-lamp.gray{--name-lamp:#9fa3aa}
#signalCounts .signal-name-lamp.unknown{--name-lamp:var(--dim)}
/* 单选名单标的竖条跟随主表整股灯，不继承所在红/黄/白分类；原尺寸与面板底色不变。 */
#signalCounts .signal-members-panel li.lamp-red{--name-lamp:var(--red)}
#signalCounts .signal-members-panel li.lamp-yellow{--name-lamp:var(--yellow)}
#signalCounts .signal-members-panel li.lamp-green{--name-lamp:var(--green)}
#signalCounts .signal-members-panel li.lamp-gray{--name-lamp:var(--text)}
#signalCounts .signal-members-panel li.lamp-unknown{--name-lamp:var(--dim)}
#signalCounts .signal-name-lamp.purple{--name-lamp:var(--purple)}
#signalCounts .signal-name-lamp.tone-purple{--name-lamp:var(--purple)}
#signalCounts .signal-name-lamp.tone-target-high{--name-lamp:var(--target-high)}
#signalCounts .signal-members-panel .note{display:inline;margin-left:6px}
#signalCounts .signal-member-direction,#signalCounts .signal-member-belong{color:var(--dim);font-size:12px}
#signalCounts .signal-member-split{list-style:none;height:0;margin:9px 0;border-top:1px solid #465e87}
#signalCounts .signal-member-metric{margin-left:5px;color:var(--text);font-size:13px}
/* 名单里的描述性数值跟着「这一条条件」的等级走，和主表描述同一套配色：
   红名单红字 #ff8580、黄名单黄字 #f0c674、白名单（灰灯）仍白字；
   ▲▼ 三角自带涨跌色，不跟着条件等级变，和主表保持一致。 */
#signalCounts .signal-members-panel.tone-red .signal-member-metric{color:#ff8580}
#signalCounts .signal-members-panel.tone-yellow .signal-member-metric{color:#f0c674}
#signalCounts .signal-members-panel.tone-gray .signal-member-metric{color:var(--text)}
/* 分框展示：方向写在框标题上（「▲ 上涨」/「上轨▲ · RSI 3条 ↑超买」），名单里不再逐条画三角 */
#signalCounts .signal-member-block{margin:0 0 10px}
#signalCounts .signal-member-block:last-child{margin-bottom:0}
#signalCounts .signal-member-block h5{margin:0 0 4px;font-size:13px;font-weight:600;color:#bfd5ff}
#signalCounts .signal-member-block h5:first-letter{letter-spacing:0}
#signalCounts .signal-member-block ul{padding-left:2px}
#signalCounts .signal-member-icon{display:inline-block;margin-left:5px;font-size:var(--lamp-size);font-weight:700;line-height:1;vertical-align:baseline}
#signalCounts .tone-white{color:var(--text)} #signalCounts .tone-yellow{color:#f0c674} #signalCounts .tone-red{color:#ff8580}
#signalCounts .tone-cyan{color:#4dd0e1} #signalCounts .tone-purple{color:var(--purple)}
#signalCounts .signal-members-panel p{margin:4px 0;color:var(--text)}
/* 点开的是哪个数字胶囊，就亮哪个胶囊本身（不整行高亮） */
#signalCounts .signal-count-toggle[aria-expanded="true"]{background:#2f4f86;border-color:#8fb8f0;color:#fff}
#signalCounts .signal-count-toggle[data-target$="-red"]{border-color:rgba(255,133,128,.45)}
#signalCounts .signal-count-toggle[data-target$="-yellow"]{border-color:rgba(240,198,116,.45)}
#signalCounts .signal-count-toggle[data-target$="-red"][aria-expanded="true"]{background:rgba(255,133,128,.16)}
#signalCounts .signal-count-toggle[data-target$="-yellow"][aria-expanded="true"]{background:rgba(240,198,116,.16)}
#signalCounts .signal-count-toggle[data-target^="signal-members-gray-"][aria-expanded="true"]{background:rgba(201,209,217,.14);border-color:rgba(201,209,217,.45)}
#signalCounts .signal-member-jump{color:var(--text);text-decoration:none;border-bottom:1px dotted #465e87}
#signalCounts .signal-member-jump:hover,.signal-member-jump:focus-visible{color:#bfd5ff;border-bottom-color:#bfd5ff}
#signalCounts .text-only{margin:8px 0 0;border:1px solid #2b3546;border-radius:8px;background:#12161f;text-align:left}
#signalCounts .text-only>summary{cursor:pointer;padding:8px 12px;color:var(--text);font-size:13px}
#signalCounts .text-only[open]>summary{border-bottom:1px solid #2b3546}
#signalCounts .text-only ul{margin:0 0 10px;padding-left:30px}
#signalCounts .text-only li{margin:5px 0;overflow-wrap:anywhere;color:var(--text);font-size:13px}
/* 紫灯 / 财报两块（紫灯在上、财报在下）：胶囊永远钉在第一排左侧不动，
   标题(summary)始终在胶囊右边同一排，点开后名单只向下展开、占满整行宽度。
   胶囊绝对定位在行内，summary 用左内边距给它让位，所以名单区域不受胶囊影响。 */
/* 2026-10-28：紫灯／财报这两行的胶囊宽度改成和「信号」列胶囊完全一致（74px），
   手机上不再比上面那几颗窄一截；summary 的让位内边距跟着 --chip-w 走，文字仍单行。 */
#signalCounts .signal-extra-row{--chip-w:74px;position:relative;display:block;padding:8px 12px 8px 5px;border-top:1px solid #465e87}
#signalCounts .signal-extra-row>.signal-filter-toggle{position:absolute;top:8px;left:5px;width:var(--chip-w);padding:0;justify-content:center;text-align:center}
#signalCounts .signal-extra-row>.text-only,#signalCounts .signal-extra-row>.earnings-week{display:block;margin:0;padding:0;border:0;border-radius:0;background:transparent}
#signalCounts .signal-extra-row>.text-only>summary,#signalCounts .signal-extra-row>.earnings-week>summary{box-sizing:border-box;min-height:38px;padding:8px 0 8px calc(var(--chip-w) + 12px);line-height:22px;font-size:13.5px;font-weight:600}
#signalCounts .signal-extra-row>.text-only[open]>summary{border-bottom:0}
#signalCounts .purple-extra .text-only>summary{color:var(--purple)}
#signalCounts .purple-extra .text-only .signal-member-list{margin:0;padding:0 0 8.1px 18px}
#signalCounts .purple-extra .text-only li{margin:5px 0;list-style:none}
#signalCounts .purple-extra .text-only .signal-member-list{padding-left:2px}
/* 空间/达到只用紧凑段落间距，覆盖浏览器 h5 的默认大外边距。 */
#signalCounts .purple-extra .target-purple>h5{margin:6px 0 3px;font-size:13px;line-height:1.5}
#signalCounts .purple-extra .target-purple .signal-member-list{padding-bottom:0}
#signalCounts .purple-extra .target-purple li{margin:2px 0}
#signalCounts .target-purple .note{display:inline;margin-left:6px}
#signalCounts .target-purple .signal-member-direction{color:var(--purple)}
#signalCounts .target-purple .target-space-high .signal-member-direction,
.group-card tr.pur-high .sig .tp{color:var(--target-high)}
.tone-target-high{color:var(--target-high)}
#signalCounts .tone-target-high,
#signalCounts .purple-extra .text-only .text-only-kind.tone-target-high,
#signalCounts .purple-extra .text-only .text-only-kind.target-space-high,
#signalCounts .purple-extra .text-only>summary.tone-target-high,
#signalCounts .purple-extra .text-only>summary.target-space-high,
#signalCounts .signal-member-block h5.tone-target-high,
#signalCounts .signal-member-block h5.target-space-high,
#signalCounts .signal-member-block.tone-target-high h5,
#signalCounts .signal-member-block.target-space-high h5{color:var(--target-high)}
/* 手机：统计面板里每个「指标名称」胶囊的**框**（74px 宽 × 38px 高）一个像素不动，
   只把框里的文字从 12px 提到 13px（「布林+RSI」「52周低位」在 74px 里仍是单行）。 */
@media(max-width:520px){#signalCounts .signal-extra-row{--chip-w:74px;padding:8px 8px 8px 5px}#signalCounts .signal-extra-row>.signal-filter-toggle{font-size:13px}#signalCounts td .signal-filter-toggle{padding:0 4px;font-size:13px;line-height:1.1}}
tr[id^="s-"]{scroll-margin-top:64px}
/* 闪烁只改 background-color：带 52 周条的行，灯条是用 background-image 画的，
   用 background 简写做动画会把灯条一起清掉（看起来就是高亮盖住了灯）。
   位置条是主行的下一个兄弟行，一起闪才不会断成两截。 */
tr.stk-flash>td, tr.stk-flash+tr.range52-row>td{animation:stkflash 1.6s ease-out 1}
@keyframes stkflash{0%,55%{background-color:#2b3f63}100%{background-color:transparent}}
#signalCounts .earnings-week{padding:0 12px;border-top:1px solid #465e87;color:var(--text)}
/* 折叠态绝对不占高度：个别移动内核在父面板隐藏期间改 open 后不重排，这里用 CSS 兜底 */
#signalCounts .earnings-week:not([open])>*:not(summary){display:none}
#signalCounts .earnings-week summary{padding:8.1px 0;color:var(--text);font-size:13.5px;font-weight:600;cursor:pointer;touch-action:manipulation}
#signalCounts .earnings-week summary:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
#signalCounts .earnings-week ul{list-style:none;margin:0;padding:0 0 8.1px 2px}
#signalCounts .earnings-week li{font-size:13px;padding:3.6px 0;overflow-wrap:anywhere}
#signalCounts .earnings-week li+li{border-top:1px solid var(--line)}
/* 财报名称前竖条复用统计名单样式，颜色仍来自该股原灯级。 */
#signalCounts .earnings-week .lamp-text{position:absolute;width:1px;height:1px;margin:-1px;padding:0;
  overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
#signalCounts .earnings-week-meta{color:var(--dim);font-size:12px}
#signalCounts .earnings-week-meta.is-red{color:#ff8580;font-weight:400}
#signalCounts .earnings-week-meta.is-yellow{color:#f0c674;font-weight:400}
#signalCounts .earnings-week .note{display:inline;margin-left:6px}
#signalCounts .signal-filter-toggle{box-sizing:border-box;height:38px;max-width:100%;padding:0 7px;font:inherit;font-size:14px;line-height:1;color:var(--text);
  display:inline-flex;align-items:center;justify-content:center;white-space:normal;background:transparent;border:1px solid #4c6591;border-radius:6px;cursor:pointer;text-align:center;line-height:1.15;touch-action:manipulation}
/* 「信号」列的所有胶囊按钮同宽（＝最宽的「布林+RSI」这一档，取 74px 让「52周低位」也不折行），
   文字／内容居中；紫灯、财报两行的胶囊是另一套定位（--chip-w），不受这条影响。 */
#signalCounts td .signal-filter-toggle{width:74px;padding:0 4px}
#signalCounts .signal-filter-toggle[aria-pressed="true"]{background:#2f6bd8;border-color:#2f6bd8;color:#fff}
#signalCounts .signal-filter-toggle:focus-visible,#signalCounts .signal-filter-clear:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
#signalCounts .signal-filter{padding:8.1px 12px;border-top:1px solid #465e87;font-size:13px}
#signalCounts .signal-filter[hidden]{display:none}
#signalCounts .signal-filter-head{display:flex;align-items:center;gap:8px;color:#bfd5ff;font-weight:600}
#signalCounts .signal-filter-clear{margin-left:auto;box-sizing:border-box;height:38px;padding:0 10px;font:inherit;font-weight:400;color:var(--text);
  background:transparent;border:1px solid #4c6591;border-radius:6px;cursor:pointer}
#signalCounts .signal-filter p{margin:6px 0 0;color:var(--dim)}
#signalCounts .signal-filter ul{list-style:none;margin:6px 0 0;padding:0}
#signalCounts .signal-filter li{padding:4.5px 0;overflow-wrap:anywhere}
#signalCounts .signal-filter li+li{border-top:1px solid var(--line)}
#signalCounts .signal-filter .note{margin-left:5px}
/* 仅多选组合描述按每条条件着色；不借用名称灯色，不影响单选名单。 */
#signalCounts .signal-filter-detail{display:block;margin-top:2px;color:var(--text)}
#signalCounts .signal-filter-condition[data-signal-level="red"]{color:#ff8580}
#signalCounts .signal-filter-condition[data-signal-level="yellow"]{color:#f0c674}
#signalCounts .signal-filter-condition[data-signal-level="gray"]{color:var(--text)}
#signalCounts .signal-filter-condition[data-signal-level="purple"]{color:var(--purple)}
#signalCounts .signal-filter-condition[data-signal-level="green"]{color:var(--green)}
/* 财报不是交易灯：字色跟统计「未来15天财报」名单一致，红＝近3日、黄＝7日内、灰＝其余。 */
#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"][data-signal-level="red"]{color:#ff8580}
#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"][data-signal-level="yellow"]{color:#f0c674}
#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"][data-signal-level="gray"]{color:var(--dim)}
.earn{display:block;margin:0;color:#8fb8f0;font-size:12.5px;font-weight:400;line-height:1.5}
.earn .unit{display:block;color:var(--dim);font-size:12px}
.earn.soon{color:#f0c674;font-weight:400}
.earn.urgent{color:#ff8580;font-weight:400}
table.stk{table-layout:fixed}
.stk tr:has(+tr.range52-row)>td{padding-top:5px;padding-bottom:2px}  /* 股票行下边距让给位置条，条靠近本股、远离下一只 */
/* 位置条下方只留 4px 就把位置条交给下一行（成本/市值行或下一只股票）；
   条本身的高度由 .range52-rail 控制，三角之上不再写百分比后已把那段空白回收 */
.stk tr.range52-row td{padding:0 12px 4px 18px;border-top:0;box-shadow:none}
/* 个股之间的横线原来是继承全局 td 的 --line(#252a33)，太淡；股票表这里提亮一档（2026-10-20） */
table.stk tr>td{border-top-color:#39424f}
/* 位置条：左端写区间最低价、右端写最高价，中间是灰轨；百分比浮在轨道上方跟随紫方块 */
/* 高度 26px = 上 13px 放现价三角（三角 9px + 灰轨 2.1px，顶上再留约 1.9px 不顶到上一只股票）
   + 下 13px 放两端「到低点／到高点」两行。三角上方原来那行百分比删掉后，
   这里从 33px 收到 26px，把那段空白整个回收掉，同时保证三角不会碰到上一行文字。 */
.range52-rail{position:relative;display:flex;align-items:flex-end;gap:7px;height:26px;padding-bottom:13px;
  font-variant-numeric:tabular-nums}
.range52-lo,.range52-hi{position:relative;flex:none;font-size:10px;line-height:11px;color:var(--dim);white-space:nowrap}
.range52-track{position:relative;flex:1 1 auto;height:2.1px;background:#9fa3aa;border-radius:99px}
/* 两端第二行＝从本轮价到低／高点的涨跌空间，以本轮价为基数；全组生效，灰字仅文字层级。
   轨道仍对齐第一行价格，第二行占下面 13px，不与三角或灰轨重叠。 */
.range52-gap{position:absolute;top:calc(100% + 2px);left:0;line-height:11px;font-size:10px;color:var(--dim)}
.range52-hi .range52-gap{left:auto;right:0}
/* 现价标记：倒三角，尖角朝下**顶在灰轨上沿**；三角之上不再写百分比——位置由三角本身表达，
   涨跌空间改到两端价格下方。左右收窄 = 只收底的宽度（10.4 → 8.4px），**高度仍 9px 不变**，
   于是顶角从 60° 变成约 50°（更尖锐）；内层维持四周 1px 的描边厚度，高度仍是原来的 7px，不跟着缩。
   外层浅紫描边 + 内层深紫实心。 */
.range52-marker{position:absolute;bottom:100%;width:0;height:0;border-bottom:0;
  border-left:4.2px solid transparent;border-right:4.2px solid transparent;
  border-top:9px solid #efd9ff;transform:translateX(-50%);z-index:1}
.range52-marker::before{content:"";position:absolute;left:-3.2px;top:-8px;width:0;height:0;
  border-left:3.2px solid transparent;border-right:3.2px solid transparent;border-top:7px solid #713caf}
/* 成本价：黄色竖线，横跨灰轨（2026-10-19 改；原来是条下方的白三角）。
   高度 10.5px 上下各露约 4px；z-index 高于现价标记，成本≈现价时黄压紫，成本价优先可见 */
.range52-cost{position:absolute;top:50%;width:2.2px;height:10.5px;border-radius:1px;background:#f0c674;transform:translate(-50%,-50%);z-index:2}
/* 成本价落在 52 周区间之外（比低点还低 / 比高点还高）：竖线贴到对应端点并稍淡，
   端点外侧补一个朝外的小箭头，避免被当成「成本刚好等于最高价／最低价」 */
.range52-cost.out-lo{left:0;opacity:.85}
.range52-cost.out-hi{left:100%;opacity:.85}
.range52-cost.out-lo::after,.range52-cost.out-hi::after{content:"";position:absolute;top:50%;
  transform:translateY(-50%);border-top:3.2px solid transparent;border-bottom:3.2px solid transparent}
.range52-cost.out-lo::after{right:calc(100% + 1px);border-right:4px solid #f0c674}
.range52-cost.out-hi::after{left:calc(100% + 1px);border-left:4px solid #f0c674}
.range52-missing{display:block;color:var(--dim);font-size:11px;line-height:15px}
.c-sym{width:18%} .c-earn{width:22%} .c-px{width:13%} .c-sig{width:47%}
.earn-cell{vertical-align:middle;overflow-wrap:anywhere}
table.stk tr td:first-child{padding-left:14px}
.stk td.sym{overflow-wrap:anywhere}
.stk th{font-size:13px;color:var(--dim);font-weight:600;letter-spacing:.3px;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
.stk th:first-child{padding-left:14px}
.stk th.h-earn,.earn-cell{text-align:center}
.px{white-space:nowrap}
/* 价格列改为居中显示（与共识价胶囊一致），不再靠右 */
td.px{text-align:center}
.px-price,.px-chg{display:block}
.px-chg{font-size:13.5px}
@media (max-width:600px){
  /* 手机：整页左右只留 4px（原来 14px），外框基本贴住屏幕边，卡片里的内容可用宽度最大。
     只覆盖左右，上（安全区）下（40px）沿用 body 原值。 */
  body{padding-left:4px;padding-right:4px}
  /* 手机：保持电脑端同样的四栏（名称｜财报｜价格涨跌幅｜警示），每栏在自己的区域内换行，不再拆成上下三层 */
  .c-sym{width:21%} .c-earn{width:19%} .c-px{width:19%} .c-sig{width:41%}
  .stk td{padding:8px 5px}
  .stk tr.range52-row td{padding:0 8px 4px 12px}
  table.stk tr td:first-child,.stk th:first-child{padding-left:11px}
  .stk th{padding:7px 5px;font-size:12px}
  .stk th.h-earn,.earn-cell{text-align:left}
  .stk td.sym{font-size:14px}
  .stk td.sym .note{font-size:11.5px;line-height:1.35}
  /* 财报列：美东时间跟「指标描述」同字号（12.5px），下面的北京时间跟原来美东时间同字号（11.5px） */
  .stk .earn{font-size:12.5px;line-height:1.4}
  .stk .earn .unit{font-size:11.5px;line-height:1.35}
  .stk .px-price{font-size:14px}
  .stk .px-chg{font-size:12.5px} .stk .tgs .lvtag{padding:1px 3px;font-size:10.5px;max-width:100%;box-sizing:border-box} .stk .tgs .lvtag span{white-space:normal;overflow-wrap:anywhere}
  .stk td.sig{font-size:12.5px;line-height:1.45}
  .group-card>summary .group-stats,.macro-card>summary .group-stats{gap:6px}
}
.up{color:var(--up)} .down{color:var(--down)}
tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red);padding-left:14px}
tr.yellow td:first-child{box-shadow:inset 4px 0 0 var(--yellow);padding-left:12px}
tr.green td:first-child{box-shadow:inset 2px 0 0 var(--green)}
tr.gray td{color:var(--dim)}
.group-card tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red);padding-left:14px}
/* 目标状态只由胶囊和统计展示；主表描述右侧不再绘制紫色灯条。 */
/* 有52周条的股票：左侧灯条顶部再减去整只股票高度（主行+位置条）的约15%（85%-6.1px，位置条行约22px），延伸覆盖自己的位置条、在条下方4px处结束，与上下股票留出空隙 */
.stk tr:has(+tr.range52-row)>td{--lc:transparent;--lw:0px}
.stk tr.red:has(+tr.range52-row)>td:first-child{--lc:var(--red);--lw:6px}
.stk tr.yellow:has(+tr.range52-row)>td:first-child{--lc:var(--yellow);--lw:4px}
.stk tr.green:has(+tr.range52-row)>td:first-child{--lc:var(--green);--lw:2px}
.stk tr:has(+tr.range52-row)>td:first-child{box-shadow:none;background-image:linear-gradient(var(--lc),var(--lc));background-size:var(--lw) calc(85% - 2.1px);background-position:left bottom;background-repeat:no-repeat}
/* 52周位置行只延续原左侧红黄绿灯条，不再延续右侧紫条。 */
.stk tr.range52-row>td{--lc:transparent;--lw:0px;background-image:linear-gradient(var(--lc),var(--lc));background-size:var(--lw) 14px;background-position:left top;background-repeat:no-repeat}
.stk tr.red+tr.range52-row>td{--lc:var(--red);--lw:6px} .stk tr.yellow+tr.range52-row>td{--lc:var(--yellow);--lw:4px}
.stk tr.green+tr.range52-row>td{--lc:var(--green);--lw:2px}
/* 市场风险表改固定列宽：WTI 参考行最后一列原本是一大段说明，自动列宽会按「整段不换行有多宽」
   去分列，结果状态列被撑到 138.6px（短文案「平静」后面空掉一大截），指标列被挤到 65.8px
   （「垃圾债利差」逐字折成三行）。文案精简后自动列宽虽已回到 98/130/59/88，但仍按内容多少
   天天变，所以固定按比例分配，四列空余均匀、结果可预期。
   四列不换行所需（含左右各 10px 内边距）实测 126/159/90/114。
   宽屏（表宽 710）26/33/18/23 → 184.6/234.3/127.8/163.3，四列空余各 40～60px；
   ≤600px（表宽 375）30/34/15/21 → 112.5/127.5/56.3/78.8，行高合计 574 → 487，
   且 360/600/768/1280 各视口均无横向溢出、无单元格内溢出。 */
#macroCard table{table-layout:fixed}
#macroCard td:first-child{width:26%} #macroCard td:nth-child(2){width:33%}
#macroCard td:nth-child(3){width:18%} #macroCard td:nth-child(4){width:23%}
#macroCard td{overflow-wrap:anywhere}
@media(max-width:600px){#macroCard td:first-child{width:30%} #macroCard td:nth-child(2){width:34%}
  #macroCard td:nth-child(3){width:15%} #macroCard td:nth-child(4){width:21%}}
/* 指标列仍左对齐，当前／变化／状态三列居中（.num 默认右对齐，这里按列覆盖） */
#macroCard td:nth-child(2),#macroCard td:nth-child(3),#macroCard td:nth-child(4){text-align:center}
#macroCard tr.mh td{padding:5px 10px;font-size:12.5px;white-space:nowrap} #macroCard tr.mh td:nth-child(3){padding-left:2px}
.macro-help{margin:8px 12px;color:var(--dim);font-size:13px;line-height:1.7}
.macro-help>summary{cursor:pointer;color:#8fb8f0;padding:8px 0;touch-action:manipulation}
.macro-help p{margin:6px 0}
.macro-help>summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:2px}
.page-notes{margin-top:18px}
.page-notes .macro-help{overflow-wrap:anywhere}
/* 页底综合细则标头与预警规则同高同色，正文及其他宏观折叠保持原样。 */
#pageNotes>#marketRiskRules{margin-top:0;margin-bottom:0}
#pageNotes>#marketRiskRules>summary{padding:9px 0;line-height:1.5;color:#bfd5ff;font-size:13px;font-weight:600}
.page-notes a{color:#8fb8f0}
.page-notes .macro-help+ .macro-help{border-top:1px solid var(--line);padding-top:4px}
.unit{display:block;font-size:11px;color:var(--dim);font-weight:400}
.foot{color:var(--dim);font-size:12.5px;text-align:center;margin-top:22px;line-height:1.7}
.foot a{color:#8ab4f8;text-decoration:none}
.foot a:hover,.foot a:focus-visible{text-decoration:underline}
.quiet{color:var(--dim);font-size:13px;padding:8px 12px 12px}
.warn{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);
  color:var(--yellow);border-radius:10px;padding:11px 14px;margin-bottom:14px;font-size:14px}
.acts{display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap;margin-top:16px}
.acts .board-return{display:inline-block;margin:8px;color:#6ba3f0}
/* 自动计划从第二排数据时间挪到页底返回看板下方；与原 sub 行同一档灰字，不再挤在头部。 */
.autoplan{color:var(--dim);font-size:12px;text-align:center;line-height:1.7;margin:2px 0 0}
#runMsg{font-size:13px;color:var(--dim);flex:1;min-width:180px;line-height:1.5;overflow-wrap:anywhere}
#runMsg.ok{color:var(--green)}
#runMsg.err{color:var(--red)}
.runbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.runbar button{padding:6px 12px;font-size:15px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;cursor:pointer;font-family:inherit}
.runbar button:disabled{opacity:.5;cursor:not-allowed}
.runbar button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
.runbar a.btnlink{padding:6px 12px;font-size:15px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;text-decoration:none;display:inline-block}
.statusrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin:-4px 0 14px;font-size:13.5px;line-height:1.5}
.check-result{padding:10px 12px;margin:0 0 14px;border:1px solid var(--line);border-radius:8px;
  font-size:13px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text)}
.check-result[data-phase="bad"]{border-color:var(--red)}
.check-result[data-phase="busy"]{border-color:var(--yellow)}
.check-result[hidden]{display:none}
.group-card>summary.grp,.macro-card>summary.grp{display:flex;align-items:center;gap:6px;cursor:pointer;
  /* 2026-10-20：左内边距 12px → 4px —— 展开箭头和分类名整体往左挪，贴着卡片左边缘 */
  padding:12px 12px 12px 4px;font-size:16px;font-weight:600;list-style:none;touch-action:manipulation}
.group-card>summary::-webkit-details-marker,.macro-card>summary::-webkit-details-marker{display:none}
/* 箭头给固定宽度并居中：左内边距收到 4px 后，展开状态旋转 90° 的「›」会贴到卡片边缘被裁掉 */
.group-card>summary::before,.macro-card>summary::before{content:"›";display:inline-block;width:10px;
  text-align:center;flex:none;color:var(--dim);font-size:20px;line-height:1}
.group-card[open]>summary::before,.macro-card[open]>summary::before{transform:rotate(90deg)}
.group-card[open]>summary,.macro-card[open]>summary{border-bottom:1px solid var(--line)}
.group-stats{margin-left:auto;color:var(--dim);font-size:14px;font-weight:400}
.group-card>summary .group-stats{margin-left:0;display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-start;text-align:left;flex:1 1 auto}
.group-card>summary .rs-spy{margin-left:auto}
/* 持仓里的「长持仓」区：可折叠，默认展开；下方就是短持仓主体，所以底部要留出间距。
   框左右各外扩 2px、内部补 2px 内边距 —— 内容（含主行左侧红黄绿灯条）位置一像素不动，
   框正好落在「持仓卡片边框」与「灯条」的正中间，不再压住灯条。 */
.zone-card{margin:10px -2px 12px;padding-left:2px;padding-right:2px;
  border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.022)}
/* 区内那条 ▲ 收起条：原来是按「贴外层卡片」的 -4px 拉宽的，套上子框后会溢出边框 3px，
   底色看着和框错位。改成只抵消本区的 2px 内边距，底色左右正好贴到子框内侧，底部也不再溢出。 */
.zone-card .group-bottom{margin:0 -2px 0}
.zone-card>summary{display:flex;align-items:center;gap:8px;cursor:pointer;padding:9px 10px;
  list-style:none;touch-action:manipulation}
.zone-card>summary::-webkit-details-marker{display:none}
.zone-card>summary::before{content:"›";color:var(--dim);font-size:18px;line-height:1}
.zone-card[open]>summary::before{transform:rotate(90deg)}
.zone-card[open]>summary{border-bottom:1px solid var(--line)}
.zone-card>summary .zone-name{color:var(--text);font-size:15px;font-weight:600;white-space:nowrap;flex:none}
.zone-card>summary .group-stats{margin-left:0;display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;
  justify-content:flex-start;text-align:left;flex:1 1 auto}
.zone-card>summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:-2px}
.zone-card .quiet{font-size:12px;color:var(--dim);padding:8px 10px 0;line-height:1.6}
/* 展开的那一组：整张卡片套上与统计面板同色（#6686c6）同宽（2px）的外框，
   标题不再铺底色、不再画左侧蓝条 —— 一眼就能看出「持仓」和里面的「长持仓」有没有打开。
   折叠后没有 [open]，卡片自动回到默认灰框，不显示外框色。
   统计面板自己恒带外框、仓位管理用自己的绿色外框，两者都不进这条（:not 里排除）。
   持仓内的「长持仓」是卡中卡，单独再细一档（1px），见下一条。 */
.group-card[open]:not(#signalCounts):not(#costManage),.macro-card[open]{
  border:var(--frame-w) solid var(--signal-border)}
.zone-card[open]{border:1px solid var(--signal-border)}
/* 鼠标／触摸点标题时不要再套一圈蓝描边：那条 2px 线会被误当成高亮，
   而且点完失焦就消失，看着像「闪一下」。键盘 Tab 过去的 :focus-visible 照旧显示。 */
.group-card>summary:focus:not(:focus-visible),.macro-card>summary:focus:not(:focus-visible),
.zone-card>summary:focus:not(:focus-visible){outline:none}
.group-card[open]:not(#signalCounts)>summary .grp-name,.macro-card[open]>summary .grp-name,
.zone-card[open]>summary .zone-name{color:#bfd5ff}
.group-card[open]:not(#signalCounts)>summary::before,.macro-card[open]>summary::before,
.zone-card[open]>summary::before{color:#8fb8f0}
/* 子区自己的收起条和外层「持仓」卡底部收起条之间拉开距离，两条 ▲ 挨太近会误点 */
.zone-card + .group-bottom,.zone-card + .quiet + .group-bottom{margin-top:22px}
/* 灯级计数徽章：空心圈刚好套住数字，圈用灯色描边，数字固定白色（底色是深色页） */
.stat-badge{display:inline-flex;align-items:center;justify-content:center;flex:none;
  box-sizing:border-box;min-width:17px;height:17px;padding:0 3px;border-radius:999px;
  border:1.5px solid var(--dim);background:transparent;
  font-size:12.5px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums;color:#fff;
  position:relative;top:2px}   /* 与分组名文字底部对齐（圆底原本比文字底高 2px） */
.stat-badge.red{border-color:var(--red)}
.stat-badge.yellow{border-color:var(--yellow)}
.stat-badge.gray{border-color:var(--dim)}
.stat-badge.missing{border-color:var(--dim);border-style:dashed}
.stat-badge.purple{border-color:var(--purple)}
.stat-badge.green{border-color:var(--green)}
/* 分组标题：名称不参与挤压，避免手机上「IT软硬Ai (27)」被拆成两行 */
.group-card>summary .grp-name,.macro-card>summary .grp-name{white-space:nowrap;flex:none}
.rs-spy{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--dim)}
.rs-spy.up{color:var(--up)} .rs-spy.down{color:var(--down)}
.stat-dot{width:11px;height:11px;border-radius:50%;display:inline-block;flex:none}
.stat-dot.red{background:var(--red)} .stat-dot.yellow{background:var(--yellow)}
.stat-dot.gray{background:var(--dim)} .stat-dot.green{background:var(--green)} .stat-dot.purple{background:var(--purple)}
.market-risk-badge{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.market-risk-badge[data-level="red"]{color:var(--red)}
.market-risk-badge[data-level="yellow"]{color:var(--yellow)}
.market-risk-badge[data-level="green"]{color:var(--green)}
.macro-card>summary .group-stats{margin-left:0;text-align:left}
/* 「无异动 N 只」自己就是一个可点开的条目（不再写「点击查看」）：上下各一条细横线，
   把「这一整块点下去会展开」的范围圈出来；清单内部股票行之间的线照旧，两者不混。 */
.quiet-list{margin:8px 0;border-top:1px solid #46525f;border-bottom:1px solid #46525f}
.quiet-list>summary{padding:8px 12px;color:var(--dim);font-size:13px;cursor:pointer}
.sym .note{display:block;margin:3px 0 0;overflow-wrap:anywhere}
.group-card summary:focus-visible,.macro-card summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:-2px}
.runlight{display:flex;align-items:center;gap:7px;font-size:13.5px;color:var(--dim);line-height:1.4}
.runlight .dot{width:9px;height:9px;border-radius:50%;background:#4b5563;flex:none}
.runlight[data-phase="busy"],.runlight[data-phase="partial"]{color:var(--yellow)}
.runlight[data-phase="busy"] .dot,.runlight[data-phase="partial"] .dot{background:var(--yellow);box-shadow:0 0 0 3px rgba(210,153,34,.18)}
.runlight[data-phase="ok"]{color:var(--green)}
.runlight[data-phase="ok"] .dot{background:var(--green);box-shadow:0 0 0 3px rgba(63,185,80,.18)}
.runlight[data-phase="bad"]{color:var(--red)}
.runlight[data-phase="bad"] .dot{background:var(--red);box-shadow:0 0 0 3px rgba(248,81,73,.18)}
.dca-badge{margin-left:auto;white-space:nowrap;color:var(--text);font:inherit;font-variant-numeric:tabular-nums}
.dca-badge.on{color:var(--yellow)}
/* 定投提醒挂在顶栏最右侧，字号跟顶栏按钮对齐 */
.runbar .dca-badge{font-size:14px;margin-left:auto}
.oil-reference-row td{font-size:13px;overflow-wrap:anywhere}
.oil-reference-row .num{white-space:nowrap;font-variant-numeric:tabular-nums}
.oil-state{font-size:12px;font-weight:600}
.oil-explanation{margin-top:3px;font-size:12.5px;line-height:1.55}
.oil-reference-row.red .oil-state{color:var(--red)}
.oil-reference-row.yellow .oil-state{color:var(--yellow)}
.oil-reference-row.green .oil-state{color:var(--green)}
.oil-rise{color:var(--up)} .oil-fall{color:var(--down)}
#macroCard td:nth-child(3) .unit{white-space:nowrap}
@media(max-width:600px){.oil-reference-row td{padding:8px 6px}#macroCard td:nth-child(3) .unit{font-size:9px}}
/* ===== 手机（iOS Chrome）文字加大：只加字号，外框上下高度一像素不动 =====
   这一段必须放在 PAGE_CSS 的**最后**：里面几条选择器和上面的基础规则同权重
   （.group-card>summary.grp、.stat-badge…），写在前面会被后面的基础规则盖掉。 */
@media (max-width:600px){
  /* 分块折叠栏标题（统计 / 仓位 / 持仓 / 市场风险）：16 → 17px，
     行高写死 24px（＝原来 16px×1.5 的行盒）→ 标题行与卡片外框上下高度完全不变 */
  .group-card>summary.grp,.macro-card>summary.grp{font-size:17px;line-height:24px}
  /* 标题行右侧的统计数字、灯级计数、财报 N：14 → 15px（行盒仍 24px） */
  .group-card>summary .group-stats,.macro-card>summary .group-stats{font-size:15px;line-height:24px}
  #signalCounts>summary .count-total{font-size:15px}
  /* 持仓里的「长持仓 / 短持仓」子框：15 → 16px，行高压回原来的 22.5px */
  .zone-card>summary{line-height:22.5px}
  .zone-card>summary .zone-name{font-size:16px}
  .zone-card>summary .group-stats{font-size:14px;line-height:22.5px}
  /* 数字灯圈：圈 17 → 20px、数字 12.5 → 14px。它是 group-stats 里的 flex item，
     20px 仍小于标题行 24px 的行盒，所以折叠栏高度不变。 */
  .stat-badge{min-width:20px;height:20px;padding:0 4px;font-size:14px}
  /* 顶栏「定投：N」原本被 margin-left:auto 顶到最右侧，手机上往左挪 10px，
     跟左边的「设置」按钮挨得近一点（不再贴着屏幕边）；字号 14 → 16px（2026-10-29）。 */
  .runbar .dca-badge{font-size:16px;margin-left:auto;margin-right:10px}
  /* 「数据时间 …」那一排改成 flex：手机上的「已同步 / 未同步」胶囊（见 cost_panel.js 的
     syncMirrorEl）用 margin-left:auto 顶到这一排最右侧，与左边的日期同一行（2026-10-29）。
     胶囊本身的字号/颜色在 cost_panel.css 的手机档里（跟其他 .cost-state 规则放一起）。 */
  .sub{display:flex;align-items:center;gap:8px}
  /* 主表各组的个股代码 + 备注名（「ZS」/「云安全」）：代码 14 → 15.5px、
     备注名 11.5 → 13px。**行高写死成原来的 21px / 15.5px**，跟 td 里并排的
     「长/短」角标、灯级角标同处一行，行的上下高度一像素不变（2026-10-29）。 */
  .stk td.sym{font-size:16px;line-height:21px}
  .stk td.sym .note{font-size:13.5px;line-height:15.5px}
}
/* 注：345 这类极窄屏整行会比线上高 6.24px —— A/B 实测过，元凶是上一轮把财报列
   「美东/北京」加大（.stk .earn），**不是**代码/备注名：把 sym 压回 14/11.5px 高度纹丝不动，
   把 .earn 一起压回才降到 96.27px。375 及以上视口不受影响，行的高度与线上完全一致。 */
"""

# 主页面将此块放在现有 rules/cost CSS 之后，避免旧规则样式覆盖紧凑版。
RULES_CSS = """#fedRules>summary,#fedGlossary>summary,details.card:has(>.rules-body)>summary{cursor:pointer;padding:9px 12px;color:#bfd5ff;font-size:13px;font-weight:600;touch-action:manipulation}
#fedRules[open]>summary,#fedGlossary[open]>summary,details.card[open]:has(>.rules-body)>summary{border-bottom:1px solid var(--line)}
#fedRules>summary:focus-visible,#fedGlossary>summary:focus-visible,details.card:has(>.rules-body)>summary:focus-visible{outline:2px solid #8fb8f0;outline-offset:-2px}
.card .rules-body{padding:8px 12px 12px;color:var(--dim);font-size:12px;line-height:1.65;overflow-wrap:anywhere}
.card .rules-body section+section{margin-top:16px;padding-top:4px;border-top:1px solid var(--line)}
.card .rules-body h4{margin:6px 0 8px;color:#bfd5ff;font-size:12.5px;font-weight:600}
.card .rules-body h5{margin:12px 0 4px;color:var(--text);font-size:12px;font-weight:600}
.card .rules-body p{margin:4px 0;color:var(--dim);font-size:12px;line-height:1.65}
.card .rules-body .rule-group{color:#8fb8f0}
.card .rules-body a{color:#8fb8f0;text-underline-offset:3px}
.card .rules-body a:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
.card .rules-body .lamp-red{color:#ff8580;font-weight:600}
.card .rules-body .lamp-yellow{color:#f0c674;font-weight:600}
.card .rules-body .lamp-purple{color:var(--purple);font-weight:600}
.card .rules-body .tone-target-high{color:var(--target-high);font-weight:600}
.card .rules-body .rule-table{table-layout:fixed;margin:8px 0 12px;border:1px solid #6b7689;font-size:12px;width:100%;-webkit-text-size-adjust:100%;text-size-adjust:100%}
.card .rules-body .rule-table th,.card .rules-body .rule-table td{padding:6px 7px;border:1px solid #5a6478;text-align:left;vertical-align:top;font-size:12px;line-height:1.65;overflow-wrap:anywhere}
.card .rules-body .rule-table thead th{color:var(--text);font-weight:600;background:rgba(143,184,240,.07);border-bottom:1px solid #6b7689}
/* 报灯总表左右撑满卡片：抵消 .card(4px) 与 .rules-body(12px) 的内边距。 */
#rules-lamp-table{margin-left:-16px;margin-right:-16px;width:calc(100% + 32px);border-left:0;border-right:0}
.card .rules-body #rules-lamp-table th,.card .rules-body #rules-lamp-table td{text-align:center}
.card .rules-body #rules-lamp-table .rule-reverse{color:var(--dim)}
/* 分类列四块之间的三条边界加粗；条件、灯级列与组内分隔线不变。 */
#rules-lamp-table tbody tr:not(:first-child)>th.cat-col{border-top:3px solid #6f7b91}
/* 最左分类列：竖排、固定 18px 单字宽（不用 em，避免手机端字号缩放把列撑宽）。
   overflow:hidden 兜底：万一竖排不生效，横排文字被裁掉也不会把列顶宽。 */
.card .rules-body .rule-table col.cat-col{width:18px}
.card .rules-body .rule-table col.col-cond{width:40%}
.card .rules-body .rule-table th.cat-col{width:18px;min-width:18px;max-width:18px;padding:6px 0;line-height:1.1;font-size:12px;letter-spacing:1px;white-space:nowrap;overflow:hidden;text-align:center;vertical-align:middle;background:rgba(143,184,240,.04);writing-mode:vertical-rl;-webkit-writing-mode:vertical-rl;text-orientation:upright;-webkit-text-orientation:upright}
.card .rules-body .ar-band-up{color:#3fb950}
.card .rules-body .ar-band-down{color:#f85149}
.card .rules-body .ar-rsi-up{color:#3ec9d6}
.card .rules-body .ar-rsi-down{color:#e061c9}
/* 仅报灯总表条件列的四种方向符号统一白色；其它区块配色与反向灰字不变。 */
#rules-lamp-table tbody td:first-of-type .ar-band-up,
#rules-lamp-table tbody td:first-of-type .ar-band-down,
#rules-lamp-table tbody td:first-of-type .ar-rsi-up,
#rules-lamp-table tbody td:first-of-type .ar-rsi-down{color:#fff}
/* 保留 RSI ↑↓ 的原字号；目标区域的方向颜色由下方限域规则覆盖继承色。 */
.arrow-rsi-up,.arrow-rsi-down{color:inherit;font-size:1.15em;line-height:1}
/* 保留方向符号的原字号/粗细；仅主表描述与统计面板独立使用涨跌数值色。 */
.grp-arrow{font-weight:700}
.stk .sig .arrow-band-up,.stk .sig .arrow-rsi-up,
#signalCounts .arrow-band-up,#signalCounts .arrow-rsi-up{color:var(--up)}
.stk .sig .arrow-band-down,.stk .sig .arrow-rsi-down,
#signalCounts .arrow-band-down,#signalCounts .arrow-rsi-down{color:var(--down)}
.card .rules-body .rule-table .ar-band-up,.card .rules-body .rule-table .ar-band-down,
.card .rules-body .rule-table .ar-rsi-up,.card .rules-body .rule-table .ar-rsi-down{font-weight:700}
/* 同向／反向两行为一组，组间用更亮的横线分开。 */
.card .rules-body .rule-table tr.rule-group-end>th,.card .rules-body .rule-table tr.rule-group-end>td{border-bottom:2px solid #6f7b91}
/* 同一灯级覆盖多个分组：居中并带灯色底色；混合灯与非灯级不加底色。白灯（折）比白灯更淡。 */
.card .rules-body .rule-table td.cell-red{background:rgba(248,81,73,.16);text-align:center}
.card .rules-body .rule-table td.cell-yellow{background:rgba(210,153,34,.16);text-align:center}
.card .rules-body .rule-table td.cell-gray{background:rgba(173,181,194,.24);text-align:center}
.card .rules-body .rule-table td.cell-gray-fold{background:rgba(173,181,194,.09);text-align:center}
/* 条件列：表头与条件文字用白字，便于与右侧灯级列区分。 */
.card .rules-body #rules-lamp-table thead th.col-cond{color:#fff}
.card .rules-body #rules-lamp-table tbody td:first-of-type{color:#e6edf3}
.card .rules-body .signal-term{display:inline-block;white-space:nowrap;overflow-wrap:normal}
.card .rules-body .dir-arrow{display:inline-block;vertical-align:-4px;margin-left:2px;overflow:visible}
.card .rules-body [id],#fedRules,#fedGlossary{scroll-margin-top:14px}
.card .rules-body .rule-anchor{display:block;height:0}
@media(max-width:520px){.card .rules-body{padding:8px 8px 12px}.card .rules-body .rule-table th,.card .rules-body .rule-table td{padding:6px 4px}#rules-lamp-table{margin-left:-12px;margin-right:-12px;width:calc(100% + 24px)}.card .rules-body .rule-table col.col-cond{width:38%}.card .rules-body .rule-table th.cat-col{font-size:11px}}
#signalRulesBody .rule-entry,#glossaryRulesBody .glossary-entry{border-radius:5px;scroll-margin-top:44px;padding:0 8px;margin:0 -8px;transition:background-color .2s,box-shadow .2s}
#signalRulesBody .rule-original{white-space:pre-wrap;overflow-wrap:anywhere}
#signalRulesBody p.rule-original{margin:1px 0;line-height:1.5;color:#dce1e9}
#signalRulesBody h5.rule-original,#signalRulesBody .rule-layout-heading,#glossaryRulesBody h5{margin:14px 0 4px}
#signalRulesBody .rule-layout-scope{margin:1px 0;line-height:1.5;color:#dce1e9}
#signalRulesBody h4.rule-original{margin:18px 0 7px}
.card .rules-body .signal-term{display:inline-flex;align-items:center;vertical-align:baseline;white-space:nowrap;gap:1px}
.card .rules-body .dir-arrow{display:inline-block;flex:none;width:auto;height:18px;vertical-align:middle}
#signalRulesBody .nav-gray{color:#adb5c2}
/* 名词解释／抓取规则：两个分区标题页面居中；指标名（h5）统一用站内强调蓝，
   与页底链接、分组词同色，覆盖上面 .card .rules-body h5 的白字（只在这两块生效）。 */
#glossaryRulesBody h4{text-align:center}
#glossaryRulesBody h5{color:#8fb8f0}
#glossaryRulesBody p{margin:2px 0;line-height:1.5;color:#dce1e9}
#glossaryRulesBody .rule-table{margin:6px 0 8px}
#glossaryRulesBody .rule-table th,#glossaryRulesBody .rule-table td{line-height:1.5}
#glossary-chg-table thead th:nth-child(2),#glossary-amp-table thead th:nth-child(2){color:#f0c674}
#glossary-chg-table thead th:nth-child(3),#glossary-amp-table thead th:nth-child(3){color:#ff8580}
#glossary-vol-table thead th:nth-child(2){color:#ff8580}
#glossary-vol-table thead th:nth-child(3){color:#f0c674}
#glossary-chg-table td,#glossary-amp-table td,#glossary-vol-table td{font-variant-numeric:tabular-nums;white-space:nowrap}
"""
PAGE_CSS = PAGE_CSS.replace('__TARGET_DASH_BORDER__', TARGET_DASH_BORDER)
PAGE_CSS += RULES_CSS

# 单独导出供 monitor 替换旧锚点处理器；PAGE_INTERACTION_JS 已含此块，勿重复追加。
RULES_INTERACTION_JS = ""  # 保留导出兼容；规则跳转、hash监听和高亮计时器已删除。
PAGE_INTERACTION_JS = RULES_INTERACTION_JS + r"""
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
function revealSignalPanel(panel) {
  // 把刚展开的清单带进视口：能整块放下就整块露出，放不下就从清单开头对齐。
  var pad = 12;
  var vh = window.innerHeight || document.documentElement.clientHeight || 0;
  if (!vh) return;
  var rect = panel.getBoundingClientRect();
  if (rect.top >= pad && rect.bottom <= vh - pad) return;   // 已经完整可见就不动，避免无谓跳动
  var y = window.pageYOffset || document.documentElement.scrollTop || 0;
  var target = (rect.height + pad * 2 <= vh && rect.bottom > vh - pad)
    ? y + rect.bottom - (vh - pad)                          // 整块往上推
    : y + rect.top - pad;                                   // 太高，从开头看起
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
  card.querySelectorAll(".text-only").forEach(function(panel) { panel.open = false; });
  const earnings = card.querySelector(".earnings-week");
  if (earnings) {
    earnings.open = false;
    // 移动端（iOS Safari / 部分安卓内核）在父面板不可见期间改 open，偶尔不重算子元素
    // 高度，再展开就会留一块空白。这里显式绕一次 display，逼它按折叠态重排。
    earnings.style.display = "none";
    void earnings.offsetHeight;
    earnings.style.display = "";
  }
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
  if (!target.closest("#signalCounts > summary")) return;
  const card = document.getElementById("signalCounts");
  if (!card) return;
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
        details.push({k:kind, l:level || "", t:text});
      });
    });
    return Object.assign({}, m, {details: details});
  });
  return {members: members};
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
  const list = document.createElement("ul");
  result.members.forEach(function(m) {
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
      let level = part.l;
      // 加减仓价已读只改变这条条件的字色，不借用标的其它红黄灯。
      if (["add_price", "reduce_price"].includes(part.k)) {
        const row = document.getElementById(m.a);
        const warning = row && row.querySelector('.cost-warn[data-signal-kind="' + part.k + '"]');
        if (warning && warning.style.display !== "none") level = warning.dataset.signalLevel || level;
      }
      condition.dataset.signalLevel = level;
      appendRsiArrows(condition, part.t);
      detail.appendChild(condition);
    });
    li.append(detail);
    list.append(li);
  });
  box.append(list);
}
window.addEventListener("mm:ack-change", renderSignalFilter);
document.addEventListener("click", function(event) {
  if (!(event.target instanceof Element)) return;
  const closeButton = event.target.closest(".collapse-group");
  if (closeButton) {
    const card = closeButton.closest("details");
    if (!card) return;
    card.open = false;
    const heading = card.querySelector(":scope > summary");
    if (heading) {
      heading.focus({preventScroll: true});
      heading.scrollIntoView({block: "start", behavior: "auto"});
    }
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
  let node = target;
  while (node && node !== document.body) {   // 从里到外把所有祖先 details 打开
    if (node.tagName === "DETAILS") node.open = true;
    node = node.parentElement;
  }
  target.classList.remove("stk-flash");
  void target.offsetWidth;                   // 强制重排，保证连点同一个名字也能再闪一次
  target.classList.add("stk-flash");
  target.scrollIntoView({block: "center", behavior: "smooth"});
  if (history.replaceState) history.replaceState(null, "", hash);
  window.setTimeout(function() { target.classList.remove("stk-flash"); }, 1800);
});
"""

if __name__ == "__main__":
    main()
