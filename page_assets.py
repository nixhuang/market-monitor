"""页面静态资源、共用规则模板和可回归的设置页生成片段。"""
import argparse
import hashlib
import html
import json
from pathlib import Path
import re

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
line-37||p|加仓价提醒标记已读 → 灰灯。
line-38||p|减仓价 · 持仓分组：现价≥设置价×${reduce_factor} → 可点红灯。
line-39||p|减仓价提醒标记已读 → 灰灯。
line-40||p|减仓价：重点、其他分组不适用。
line-41|rule-cost-loss|h5|成本浮亏 持仓分组
line-42||p|浮亏≥10% → 可点红灯；
line-43||p|浮亏≥20%加重文字 → 可点红灯。
line-44||p|浮亏提醒标记已读 → 灰灯。
line-45||p|浮亏10%／20%每只标的只计一次。
line-46||p|重点、其他分组不适用。
line-47|rule-target-space|h5|目标空间 所有分组
line-48||p|有效空间≥${target_space_pct}% → 紫虚线空心灯；
target-space-positive||p|≥${target_high_space_pct}% → 紫红虚线空心灯。
line-50|rule-target-reached|h5|达到目标／共识价 持仓分组、重点
target-reached-positive||p|当日上穿／跌破或现价在目标±${target_gap_pct}% → 实心紫灯。"""),
    ("rule-text", r"""line-52||h4|二、不报灯且不折叠
line-53|rule-earnings|h5|财报临近 所有分组
line-54||p|美东财报日期按北京时间：前一日／当日／后一日 → 红字；
earnings-near||p|财报前2～${earn_soon_days}天 → 黄字。
line-56|rule-quotes|h5|缺行情／参考行情 数据缺失 所有分组
line-57||p|缺行情 → 灰灯；参考行情显示参考状态。
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
# 灯级口径：红灯／黄灯／灰灯＝有文字、不折叠；灰灯（折）＝有文字、折叠；仅文字＝折叠且不计灰；
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
    '<td class="cell-gray">灰灯</td></tr>'
    '<tr class="rule-group-end"><td>布林上轨▲ + RSI 3条 ↓超卖（反）</td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr><td>布林下轨▼ + RSI 3条 ↓超卖</td>'
    '<td class="cell-red" colspan="3">红灯</td></tr>'
    '<tr class="rule-group-end"><td>布林下轨▼ + RSI 3条 ↑超买（反）</td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr><td>布林上轨▲ + RSI 2条 ↑超买</td>'
    '<td class="cell-yellow">黄灯</td><td class="cell-yellow">黄灯</td><td>仅文字</td></tr>'
    '<tr class="rule-group-end"><td>布林上轨▲ + RSI 2条 ↓超卖（反）</td>'
    '<td class="cell-yellow">黄灯</td><td class="cell-yellow">黄灯</td>'
    '<td class="cell-gray">灰灯</td></tr>'
    '<tr><td>布林下轨▼ + RSI 2条 ↓超卖</td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr class="rule-group-end"><td>布林下轨▼ + RSI 2条 ↑超买（反）</td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr><td>布林上轨▲ + RSI 1条 ↑超买</td>'
    '<td class="cell-gray">灰灯</td><td class="cell-gray">灰灯</td><td>无提示</td></tr>'
    '<tr class="rule-group-end"><td>布林上轨▲ + RSI 1条 ↓超卖（反）</td>'
    '<td class="cell-gray-fold">灰灯（折）</td><td class="cell-gray-fold">灰灯（折）</td><td>无提示</td></tr>'
    '<tr><td>布林下轨▼ + RSI 1条 ↓超卖</td>'
    '<td class="cell-gray" colspan="3">灰灯</td></tr>'
    '<tr class="rule-group-end"><td>布林下轨▼ + RSI 1条 ↑超买（反）</td>'
    '<td class="cell-gray" colspan="3">灰灯</td></tr>'
    '<tr><th scope="row" class="cat-col" rowspan="6">单独RSI</th>'
    '<td>RSI 3条 ↑超买</td>'
    '<td class="cell-yellow">黄灯</td><td class="cell-yellow">黄灯</td>'
    '<td class="cell-gray-fold">灰灯（折）</td></tr>'
    '<tr class="rule-group-end"><td>RSI 3条 ↓超卖</td>'
    '<td class="cell-red" colspan="3">红灯</td></tr>'
    '<tr><td>RSI 2条 ↑超买</td>'
    '<td class="cell-gray">灰灯</td><td class="cell-gray">灰灯</td><td>无提示</td></tr>'
    '<tr class="rule-group-end"><td>RSI 2条 ↓超卖</td>'
    '<td class="cell-gray" colspan="3">灰灯</td></tr>'
    '<tr><td>RSI 1条 ↑超买</td><td colspan="3">无提示</td></tr>'
    '<tr class="rule-group-end"><td>RSI 1条 ↓超卖</td><td colspan="3">无提示</td></tr>'
    '<tr><th scope="row" class="cat-col" rowspan="2">单独布林</th>'
    '<td>布林上轨▲ + RSI 0条</td><td class="cell-gray">灰灯</td>'
    '<td>无提示</td><td>无提示</td></tr>'
    '<tr class="rule-group-end"><td>布林下轨▼ + RSI 0条</td>'
    '<td class="cell-gray-fold" colspan="3">灰灯（折）</td></tr>'
    '<tr><th scope="row" class="cat-col" rowspan="5">价格与量能</th>'
    '<td>涨跌幅 · 自适应</td>'
    '<td>红灯／黄灯</td><td>红灯／黄灯</td><td class="cell-gray">灰灯</td></tr>'
    '<tr><td>振幅 · 自适应</td><td>红灯／黄灯</td><td>红灯／黄灯</td><td>无效</td></tr>'
    '<tr><td>52周低位（距低点≤${near_52w_low_pct}%）</td>'
    '<td colspan="3">红灯（新进入／跌破／昨日未知）；黄灯（其余持续低位）</td></tr>'
    '<tr><td>均线（短均线穿越；长均线触及±${ma_long_band_pct}%或穿越）</td>'
    '<td class="cell-yellow" colspan="3">黄灯</td></tr>'
    '<tr><td>量比（收盘自适应／盘中固定）</td>'
    '<td colspan="3">红灯（≥${vol_ratio_red_z}σ 或 ≥${vol_ratio_red}×）；'
    '黄灯（≥${vol_ratio_z}σ 或 ≥${vol_ratio}×）</td></tr>'
    '</tbody></table>')

# 规则正文不再提供内部跳转；方向图标和阈值仍保留。


GLOSSARY_BODY = r"""<div class="rules-body" id="glossaryRulesBody">
<section id="glossary-calculation">
<h4>名词解释与计算</h4>
<h5 id="glossary-ack">可点红灯与已读</h5>
<p>加仓价、减仓价、成本浮亏：点✓已读，再点撤销。已读显示灰字删除线，统计由对应红列转灰列。</p>
<p>已读只撤页面追加的该项红灯，不撤生成红灯或其他原因的灯。是否进入“无异动”，按剩余报灯与保留条件判断。</p>
<p>条件消失即清除已读，再次触发重新提醒。浮亏10%／20%分档记录、每只只计一次；升降档切换提醒，低于10%清除浮亏已读。</p>
<h5 id="glossary-chg">涨跌幅 · 计算与阈值</h5>
<p>涨跌幅＝（现价÷昨收−1）×100%；报灯取绝对值。</p>
<p>两项共用历史日波动档：&lt;${vol_low_sigma}%低档，&gt;${vol_high_sigma}%高档，其余中档；不足${vol_min_returns}个有效日收益取中档。不是按今天的涨跌选档。</p>
<p>所有分组适用；达到红门槛报红，否则达到黄门槛报黄。</p>
${chg_threshold_table}
<h5 id="glossary-amp">振幅 · 计算与阈值</h5>
<p>振幅＝（日高−日低）÷昨收×100%，表示日内摆动宽度。涨跌幅接近0，振幅仍可能很大。</p>
<p>沿用上节波动档；持仓分组、重点报灯，其他分组只显示数值。红门槛优先。</p>
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
<p>“1／2／3条”是同方向达标的周期数，不是连续天数；向上／向下指超买／超卖，不等于今日涨跌。</p>
<h5 id="glossary-vol">量比 · 自适应／固定切换</h5>
<p>完整日量比＝成交量÷此前${vol_ratio_base}日均量。异常值有效、两条σ阈值启用且量比≥1×，用自适应；否则用固定倍数。</p>
<p>σ表示放量异常程度，不是均量倍数，也不套股价波动档。自适应未命中，不再用固定倍数补灯。</p>
<p>盘中按已开盘时间折算成交速度，只用固定倍数；不足${intraday_vol_quiet_min}分钟或盘中值无效，退回最近完整日。完整日量比缺失则不判。</p>
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
<p>只显示个股财报，ETF／指数／期货除外。失败时沿用有效旧日期；“待公布”仅表示本站未取得日期。</p>
<p>已知时段显示盘前／盘后／盘中及北京落点，未知不猜钟点。</p>
<h5 id="glossary-dedup">标的去重与显示优先级</h5>
<p>显示与报警优先级：市场风险／持仓 → 重点 → 其他。同一实质标的只显示最高级，清单记录不删。</p>
<p>VIX与标普指数仅在市场风险显示；指数、跟踪ETF、期货不是同一标的。</p>
<p>文字颜色不等于交易灯。首次只展开统计，不等于标的进入“无异动”的判定。</p>
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
<tr><th scope="row">美联储决议</th><td>美联储官网声明及发布日程</td></tr>
<tr><th scope="row">WTI原油</th><td>Yahoo NYMEX WTI期货日线；不是实时夜盘报价</td></tr></tbody></table>
<p>来源、观测日期与失效状态看对应数据项；有效期与判定见综合判断与宏观细则。</p>
<h5 id="fetch-reference">补充参考：缓存间隔</h5>
<table class="rule-table"><thead><tr><th scope="col">数据</th><th scope="col">缓存／重试</th></tr></thead>
<tbody><tr><th scope="row">美联储决议</th><td>到下次官方公布时点前复用；错误／待更新至少隔1小时重试</td></tr>
<tr><th scope="row">NFCI</th><td>按周度公布时点到期；有旧缓存时，未发布／失败至少隔1小时重试，无有效缓存则下一轮再查</td></tr>
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


COLORS = {"紫红虚线空心灯": "tone-target-high", "紫虚线空心灯": "lamp-purple",
          "实心紫灯": "lamp-purple", "红灯": "lamp-red", "黄灯": "lamp-yellow",
          "灰灯": "nav-gray", "红字": "lamp-red", "黄字": "lamp-yellow",
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
              "intraday_vol_quiet_min": 15, "vol_low_sigma": 1.2, "vol_high_sigma": 2.5,
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
            + "/* 规则说明仅在首页按生效配置生成，设置页只保留可编辑参数。 */\n"
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


# 页面 CSS 与纯展示交互从 monitor 原样迁移，公开名称由 monitor 重导出。
PAGE_CSS = """:root{
  --bg:#0f1115; --card:#171a21; --line:#252a33; --text:#e6e8ec; --dim:#8b93a1;
  --green:#3fb950; --yellow:#d29922; --red:#f85149; --up:#3fb950; --down:#f85149;
  --blue:#2f6bd8; --purple:#a371f7; --target-high:#dc62b8;
  --signal-bg:#182131; --collapse-bg:var(--signal-bg);
  /* 灯（圆点）统一尺寸：首页描述灯、统计名单行首圆点、名单三角同一套大小 */
  --lamp-size:14px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;
  padding:env(safe-area-inset-top) 14px 40px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:19px;margin:18px 0 4px;font-weight:600;text-align:center}
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
.lvtag.tgt{position:relative;color:#c4a3ff;border-color:var(--dim)}
.lvtag.tgt.low{border:1px solid var(--dim);background:transparent}
/* 描边绘在原1px边框内部，文字等宽视觉加粗：分档不增加尺寸、不移动文字。 */
.lvtag.tgt.space,.lvtag.tgt.high{border-color:transparent;background:transparent}
.lvtag.tgt.space::after,.lvtag.tgt.high::after{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none}
.lvtag.tgt.space::after{border:1.5px solid var(--purple)}
.lvtag.tgt.high{color:var(--target-high);text-shadow:.2px 0 currentColor,-.2px 0 currentColor}
.lvtag.tgt.high::after{border:1.5px solid var(--target-high)}
.lvtag.tgt.bad{color:var(--dim);background:transparent;border:1px dashed var(--dim);text-shadow:none}
.lvtag.tgt.bad::after,.lvtag.tgt.hit::after{content:none}
.lvtag.tgt.hit{color:#fff;background:#8957e5;border:1px solid #8957e5;text-shadow:none}
.tnote,.sig .tp{color:#c4a3ff}
.sig{font-size:13.5px;color:var(--text);overflow-wrap:anywhere}
.signal-magenta{color:#ef8de4} .signal-red{color:#ff8580} .signal-yellow{color:#f0c674} .signal-cyan{color:#4dd0e1}
/* 灯统一画在标的名称前面：直径 .34em（14px 字号下 4.76px，与原生 disc 的 4.75px 一致）、
   右边距 .875em（12.25px），与统计名单 li::marker 的间距一致。三处灯只在这一处定尺寸和间距。
   vertical-align:.25em 是实测校准值：与统计名单原生 li::marker 的圆点垂直中心相差 0.00px。
   不能用 display 的双关键字写法（inline 后接 list-item，靠原生 disc 画圆点）：Safari 与
   iOS WebView（含微信内置浏览器）不支持，整条声明被丢弃后灯会整个消失。这里画实心圆点。 */
.signal-dot{display:inline-block;width:.34em;height:.34em;border-radius:50%;background:currentColor;font-size:var(--lamp-size);font-weight:400;margin:0 .875em 0 0;padding:0;vertical-align:.25em;color:var(--dim)}
.signal-dot.red{color:var(--red)} .signal-dot.yellow{color:var(--yellow)}
.signal-dot.purple{color:var(--purple)} .signal-dot.gray{color:#9fa3aa}
.signal-item[data-signal-level="red"]{color:#ff8580}
.signal-item[data-signal-level="yellow"]{color:#f0c674}
.signal-item[data-signal-level="purple"]{color:var(--purple)}
.signal-separator{color:var(--dim);margin:0 2px}
.group-bottom{padding:0;margin:0 -4px -6px;border-top:1px solid var(--line);text-align:center;line-height:0}
.collapse-group{width:100%;height:26px;padding:0;font:inherit;color:var(--dim);
  display:flex;align-items:center;justify-content:center;box-sizing:border-box;
  background:var(--collapse-bg);border:0;border-radius:6px;cursor:pointer;touch-action:manipulation}
.collapse-group .collapse-icon{font-size:16px;line-height:1}
.collapse-group:hover{color:var(--text);background:var(--collapse-bg)}
.collapse-group:focus-visible,.signal-count-toggle:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
#signalCounts{--signal-border:#6686c6;--collapse-bg:var(--signal-border);border:1px solid var(--signal-border);box-shadow:inset 3px 0 0 #8fb8f0;background:var(--signal-bg)}
#signalCounts .collapse-group{color:#fff}
#signalCounts>summary{color:#bfd5ff}
#signalCounts>summary .count-total{white-space:nowrap;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
#signalCounts .count-red{color:var(--red)} #signalCounts .count-yellow{color:var(--yellow)}
#signalCounts .count-earn{color:var(--text)}
#signalCounts table{table-layout:fixed}
#signalCounts td{padding:6.3px 5px;font-size:13px;line-height:1.25;overflow-wrap:anywhere;vertical-align:middle}
#signalCounts td.num{text-align:center}   /* 计数列居中：破折号与展开按钮对齐 */
#signalCounts td:first-child{width:26%}
#signalCounts .signal-count-toggle{box-sizing:border-box;height:38px;min-width:44px;max-width:100%;padding:0 4px;
  color:#bfd5ff;font:inherit;font-size:14px;line-height:1;background:transparent;border:1px solid #4c6591;border-radius:6px;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;gap:3px;touch-action:manipulation}
#signalCounts .signal-count-toggle::after{content:"▾";font-size:10px}
#signalCounts .signal-count-toggle[aria-expanded="true"]::after{content:"▴"}
#signalCounts .signal-members-row>td{padding:0;width:auto;border-top:0}
#signalCounts .signal-members-panel{padding:10px 12px;background:#121a27;border-top:1px solid #465e87;text-align:left}
#signalCounts .signal-members-panel[hidden]{display:none}
#signalCounts .signal-members-panel ul{margin:0;padding-left:18px}
#signalCounts .signal-members-panel li{margin:5px 0;overflow-wrap:anywhere}
/* 名单前面的圆点按这一格的灯色走，不再是默认的白点；灰灯没有灯，用灰点 */
#signalCounts .signal-members-panel li::marker{color:var(--dim);font-size:var(--lamp-size)}
#signalCounts .signal-members-panel.tone-red li::marker{color:var(--red)}
#signalCounts .signal-members-panel.tone-yellow li::marker{color:var(--yellow)}
#signalCounts .text-only .signal-member-list li::marker{color:#9fa3aa}
#signalCounts .signal-members-panel .note{display:inline;margin-left:6px}
#signalCounts .signal-member-direction,#signalCounts .signal-member-belong{color:var(--dim);font-size:12px}
#signalCounts .signal-member-split{list-style:none;height:0;margin:9px 0;border-top:1px solid #465e87}
#signalCounts .signal-member-metric{margin-left:5px;color:var(--text);font-size:13px}
/* 分框展示：方向写在框标题上（「▲ 上涨」/「上轨▲ · RSI 3条 ↑超买」），名单里不再逐条画三角 */
#signalCounts .signal-member-block{margin:0 0 10px}
#signalCounts .signal-member-block:last-child{margin-bottom:0}
#signalCounts .signal-member-block h5{margin:0 0 4px;font-size:13px;font-weight:600;color:#bfd5ff}
#signalCounts .signal-member-block h5:first-letter{letter-spacing:0}
#signalCounts .signal-member-block ul{padding-left:18px}
#signalCounts .signal-member-icon{display:inline-block;margin-left:5px;font-size:var(--lamp-size);font-weight:700;line-height:1;vertical-align:baseline}
#signalCounts .tone-white{color:var(--text)} #signalCounts .tone-yellow{color:#f0c674} #signalCounts .tone-red{color:#ff8580}
#signalCounts .tone-cyan{color:#4dd0e1} #signalCounts .tone-purple{color:#c4a3ff}
#signalCounts .signal-members-panel p{margin:4px 0;color:var(--text)}
/* 点开的是哪个数字胶囊，就亮哪个胶囊本身（不整行高亮） */
#signalCounts .signal-count-toggle[aria-expanded="true"]{background:#2f4f86;border-color:#8fb8f0;color:#fff}
#signalCounts .signal-member-jump{color:var(--text);text-decoration:none;border-bottom:1px dotted #465e87}
#signalCounts .signal-member-jump:hover,.signal-member-jump:focus-visible{color:#bfd5ff;border-bottom-color:#bfd5ff}
#signalCounts .text-only{margin:8px 0 0;border:1px solid #2b3546;border-radius:8px;background:#12161f;text-align:left}
#signalCounts .text-only>summary{cursor:pointer;padding:8px 12px;color:var(--text);font-size:13px}
#signalCounts .text-only[open]>summary{border-bottom:1px solid #2b3546}
#signalCounts .text-only ul{margin:0 0 10px;padding-left:30px}
#signalCounts .text-only li{margin:5px 0;overflow-wrap:anywhere;color:var(--text);font-size:13px}
/* 财报条件 / 紫灯条件：胶囊永远钉在第一排左侧不动，
   标题(summary)始终在胶囊右边同一排，点开后名单只向下展开、占满整行宽度。
   胶囊绝对定位在行内，summary 用左内边距给它让位，所以名单区域不受胶囊影响。 */
#signalCounts .signal-extra-row{--chip-w:72px;position:relative;display:block;padding:8px 12px 8px 5px;border-top:1px solid #465e87;background:#182130}
#signalCounts .signal-extra-row>.signal-filter-toggle{position:absolute;top:8px;left:5px;width:var(--chip-w);padding:0;justify-content:center;text-align:center}
#signalCounts .signal-extra-row>.text-only,#signalCounts .signal-extra-row>.earnings-week{display:block;margin:0;padding:0;border:0;border-radius:0;background:transparent}
#signalCounts .signal-extra-row>.text-only>summary,#signalCounts .signal-extra-row>.earnings-week>summary{box-sizing:border-box;min-height:38px;padding:8px 0 8px calc(var(--chip-w) + 12px);line-height:22px;font-size:13.5px;font-weight:600}
#signalCounts .signal-extra-row>.text-only[open]>summary{border-bottom:0}
#signalCounts .purple-extra .text-only>summary{color:#c4a3ff}
#signalCounts .purple-extra .text-only .signal-member-list{margin:0;padding:0 0 8.1px 18px}
#signalCounts .purple-extra .text-only li{margin:5px 0;list-style:none}
#signalCounts .purple-extra .text-only .signal-member-list{padding-left:2px}
/* 空间/达到只用紧凑段落间距，覆盖浏览器 h5 的默认大外边距。 */
#signalCounts .purple-extra .target-purple>h5{margin:6px 0 3px;font-size:13px;line-height:1.5}
#signalCounts .purple-extra .target-purple .signal-member-list{padding-bottom:0}
#signalCounts .purple-extra .target-purple li{margin:2px 0}
#signalCounts .target-purple .note{display:inline;margin-left:6px}
#signalCounts .target-purple .signal-member-direction{color:#c4a3ff}
#signalCounts .target-purple .target-space-high .signal-member-direction,
.group-card tr.pur-high .sig .tp{color:var(--target-high)}
#signalCounts .purple-extra .text-only li>.signal-member-icon{margin:0 6px 0 0;color:#c4a3ff;font-size:14px}
.tone-target-high{color:var(--target-high)}
#signalCounts .tone-target-high,
#signalCounts .purple-extra .text-only li>.signal-member-icon.tone-target-high,
#signalCounts .purple-extra .text-only li>.signal-member-icon.target-space-high,
#signalCounts .purple-extra .text-only .target-space-high>.signal-member-icon,
#signalCounts .purple-extra .text-only .tone-target-high>.signal-member-icon,
#signalCounts .purple-extra .text-only .text-only-kind.tone-target-high,
#signalCounts .purple-extra .text-only .text-only-kind.target-space-high,
#signalCounts .purple-extra .text-only>summary.tone-target-high,
#signalCounts .purple-extra .text-only>summary.target-space-high,
#signalCounts .signal-member-block h5.tone-target-high,
#signalCounts .signal-member-block h5.target-space-high,
#signalCounts .signal-member-block.tone-target-high h5,
#signalCounts .signal-member-block.target-space-high h5{color:var(--target-high)}
@media(max-width:520px){#signalCounts .signal-extra-row{--chip-w:62px;padding:8px 8px 8px 5px}#signalCounts .signal-extra-row>.signal-filter-toggle{font-size:12px}#signalCounts td .signal-filter-toggle{padding:0 4px;font-size:12px;line-height:1.1}}
tr[id^="s-"]{scroll-margin-top:64px}
/* 闪烁只改 background-color：带 52 周条的行，灯条是用 background-image 画的，
   用 background 简写做动画会把灯条一起清掉（看起来就是高亮盖住了灯）。
   位置条是主行的下一个兄弟行，一起闪才不会断成两截。 */
tr.stk-flash>td, tr.stk-flash+tr.range52-row>td{animation:stkflash 1.6s ease-out 1}
@keyframes stkflash{0%,55%{background-color:#2b3f63}100%{background-color:transparent}}
#signalCounts .earnings-week{padding:0 12px;border-top:1px solid #465e87;background:#182130;color:var(--text)}
/* 折叠态绝对不占高度：个别移动内核在父面板隐藏期间改 open 后不重排，这里用 CSS 兜底 */
#signalCounts .earnings-week:not([open])>*:not(summary){display:none}
#signalCounts .earnings-week summary{padding:8.1px 0;color:var(--text);font-size:13.5px;font-weight:600;cursor:pointer;touch-action:manipulation}
#signalCounts .earnings-week summary:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
#signalCounts .earnings-week ul{list-style:disc;margin:0;padding:0 0 8.1px 18px}
#signalCounts .earnings-week li{font-size:13px;padding:3.6px 0;overflow-wrap:anywhere}
#signalCounts .earnings-week li+li{border-top:1px solid var(--line)}
/* 财报名单的灯也用行首圆点，和 52周低位/布林+RSI 等栏目同一位置同一大小 */
#signalCounts .earnings-week li::marker{color:var(--dim)}
#signalCounts .earnings-week li.lamp-red::marker{color:var(--red)}
#signalCounts .earnings-week li.lamp-yellow::marker{color:var(--yellow)}
#signalCounts .earnings-week li.lamp-green::marker{color:var(--green)}
#signalCounts .earnings-week li.lamp-gray::marker{color:var(--dim)}
#signalCounts .earnings-week .lamp-text{position:absolute;width:1px;height:1px;margin:-1px;padding:0;
  overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
#signalCounts .earnings-week-meta{color:var(--dim);font-size:12px}
#signalCounts .earnings-week-meta.is-red{color:var(--red);font-weight:600}
#signalCounts .earnings-week-meta.is-yellow{color:#f0c674;font-weight:600}
#signalCounts .earnings-week .note{display:inline;margin-left:6px}
#signalCounts .signal-filter-toggle{box-sizing:border-box;height:38px;max-width:100%;padding:0 7px;font:inherit;font-size:14px;line-height:1;color:var(--text);
  display:inline-flex;align-items:center;justify-content:center;white-space:normal;background:transparent;border:1px solid #4c6591;border-radius:6px;cursor:pointer;text-align:center;line-height:1.15;touch-action:manipulation}
#signalCounts .signal-filter-toggle[aria-pressed="true"]{background:#2f6bd8;border-color:#2f6bd8;color:#fff}
#signalCounts .signal-filter-toggle:focus-visible,#signalCounts .signal-filter-clear:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
#signalCounts .signal-filter{padding:8.1px 12px;border-top:1px solid #465e87;background:#121a27;font-size:13px}
#signalCounts .signal-filter[hidden]{display:none}
#signalCounts .signal-filter-head{display:flex;align-items:center;gap:8px;color:#bfd5ff;font-weight:600}
#signalCounts .signal-filter-clear{margin-left:auto;box-sizing:border-box;height:38px;padding:0 10px;font:inherit;font-weight:400;color:var(--text);
  background:transparent;border:1px solid #4c6591;border-radius:6px;cursor:pointer}
#signalCounts .signal-filter p{margin:6px 0 0;color:var(--dim)}
#signalCounts .signal-filter ul{list-style:none;margin:6px 0 0;padding:0}
#signalCounts .signal-filter li{padding:4.5px 0;overflow-wrap:anywhere}
#signalCounts .signal-filter li+li{border-top:1px solid var(--line)}
#signalCounts .signal-filter .note{margin-left:5px}
/* 多选清单与主表共用 .signal-dot 的尺寸和间距；统计名单保留原生 marker。
   三处灯均在名称前，尺寸由 --lamp-size 控制。 */
#signalCounts .signal-filter-detail{display:block;margin-top:2px;color:var(--text)}
.earn{display:block;margin:0;color:#8fb8f0;font-size:12.5px;font-weight:400;line-height:1.5}
.earn .unit{display:block;color:var(--dim);font-size:12px}
.earn.soon{color:#f0c674;font-weight:600}
.earn.urgent{color:var(--red);font-weight:600}
table.stk{table-layout:fixed}
.stk tr:has(+tr.range52-row)>td{padding-top:5px;padding-bottom:2px}  /* 股票行下边距让给位置条，条靠近本股、远离下一只 */
/* 位置条改成 20px 高（左右要放最低/最高价），底内边距相应从 12px 收到 7px，整行高度不变 */
.stk tr.range52-row td{padding:0 12px 7px 18px;border-top:0;box-shadow:none}
/* 个股之间的横线原来是继承全局 td 的 --line(#252a33)，太淡；股票表这里提亮一档（2026-10-20） */
table.stk tr>td{border-top-color:#39424f}
/* 位置条：左端写区间最低价、右端写最高价，中间是灰轨；百分比浮在轨道上方跟随紫方块 */
.range52-rail{position:relative;display:flex;align-items:flex-end;gap:7px;height:20px;
  font-variant-numeric:tabular-nums}
.range52-lo,.range52-hi{flex:none;font-size:10px;line-height:11px;color:var(--dim);white-space:nowrap}
/* 持仓端点第二行：相对现价的下跌／上涨空间。轨道仍对齐第一行价格，保留三角上方间距。 */
.range52-rail.has-endpoint-gaps{height:33px;padding-bottom:13px}
.has-endpoint-gaps .range52-lo,.has-endpoint-gaps .range52-hi{position:relative}
.range52-gap{position:absolute;top:calc(100% + 2px);left:0;line-height:11px;font-size:10px;color:var(--dim)}
.range52-hi .range52-gap{left:auto;right:0}
.range52-track{position:relative;flex:1 1 auto;height:2.1px;background:#9fa3aa;border-radius:99px}
/* 百分比紧贴倒三角的上边缘，但**不能重叠**：三角高 9px，文字底 = 轨道上沿 + 11px（留 2px） */
.range52-percent{position:absolute;bottom:calc(100% + 11px);left:clamp(14px,var(--range-pos),calc(100% - 14px));transform:translateX(-50%);color:var(--text);font-size:10px;line-height:11px;white-space:nowrap}
/* 现价标记：倒三角，尖角 60°（等边：底 10.4px、高 9px），尖角朝下**顶在灰轨上沿**，
   不再压在轨道中间（原来是个 8.4px 的紫方块）。外层浅紫描边 + 内层深紫实心 */
.range52-marker{position:absolute;bottom:100%;width:0;height:0;border-bottom:0;
  border-left:5.2px solid transparent;border-right:5.2px solid transparent;
  border-top:9px solid #efd9ff;transform:translateX(-50%);z-index:1}
.range52-marker::before{content:"";position:absolute;left:-4.2px;top:-8px;width:0;height:0;
  border-left:4.2px solid transparent;border-right:4.2px solid transparent;border-top:7px solid #713caf}
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
  /* 手机：保持电脑端同样的四栏（名称｜财报｜价格涨跌幅｜警示），每栏在自己的区域内换行，不再拆成上下三层 */
  .c-sym{width:21%} .c-earn{width:19%} .c-px{width:19%} .c-sig{width:41%}
  .stk td{padding:8px 5px}
  .stk tr.range52-row td{padding:0 8px 6px 12px}
  table.stk tr td:first-child,.stk th:first-child{padding-left:11px}
  .stk th{padding:7px 5px;font-size:12px}
  .stk th.h-earn,.earn-cell{text-align:left}
  .stk td.sym{font-size:14px}
  .stk td.sym .note{font-size:11.5px;line-height:1.35}
  .stk .earn{font-size:11.5px;line-height:1.4}
  .stk .earn .unit{font-size:11px;line-height:1.35}
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
.group-card tr.pur td.sig{--pc:var(--purple);--pw:2px;--pstripe:linear-gradient(var(--pc),var(--pc));box-shadow:none;padding-right:12px;background-image:var(--pstripe);background-size:var(--pw) 100%;background-position:right bottom;background-repeat:no-repeat}
.group-card tr.pur-space td.sig{--pc:var(--purple);--pw:2px;--pstripe:repeating-linear-gradient(to top,var(--pc) 0 10px,transparent 10px 18px)}
.group-card tr.pur-high td.sig{--pc:var(--target-high);--pw:3px;--pstripe:repeating-linear-gradient(to top,var(--pc) 0 10px,transparent 10px 18px)}
.group-card tr.pur-hit td.sig{--pc:var(--purple);--pw:2px;--pstripe:linear-gradient(var(--pc),var(--pc))}
/* 有52周条的股票：左侧灯条顶部再减去整只股票高度（主行+位置条）的约15%（85%-6.1px，位置条行约22px），延伸覆盖自己的位置条、在条下方4px处结束，与上下股票留出空隙 */
.stk tr:has(+tr.range52-row)>td{--lc:transparent;--lw:0px}
.stk tr.red:has(+tr.range52-row)>td:first-child{--lc:var(--red);--lw:6px}
.stk tr.yellow:has(+tr.range52-row)>td:first-child{--lc:var(--yellow);--lw:4px}
.stk tr.green:has(+tr.range52-row)>td:first-child{--lc:var(--green);--lw:2px}
.stk tr:has(+tr.range52-row)>td:first-child{box-shadow:none;background-image:linear-gradient(var(--lc),var(--lc));background-size:var(--lw) calc(85% - 2.1px);background-position:left bottom;background-repeat:no-repeat}
.stk tr.pur:has(+tr.range52-row)>td.sig{background-image:var(--pstripe);background-size:var(--pw) calc(85% - 2.1px);background-position:right bottom;background-repeat:no-repeat}
/* 长划线10px、间隔8px，参考图的长段节奏；下段18px正好一个周期，与上段同向且锚定底部，跨行接缝不断拍。 */
.stk tr.range52-row>td{--lc:transparent;--lw:0px;--pc:transparent;--pw:0px;--pstripe:linear-gradient(var(--pc),var(--pc));background-image:linear-gradient(var(--lc),var(--lc)),var(--pstripe);background-size:var(--lw) 18px,var(--pw) 18px;background-position:left top,right top;background-repeat:no-repeat}
.stk tr.red+tr.range52-row>td{--lc:var(--red);--lw:6px} .stk tr.yellow+tr.range52-row>td{--lc:var(--yellow);--lw:4px}
.stk tr.green+tr.range52-row>td{--lc:var(--green);--lw:2px} .stk tr.pur+tr.range52-row>td{--pc:var(--purple);--pw:2px}
.stk tr.pur-space+tr.range52-row>td{--pc:var(--purple);--pw:2px;--pstripe:repeating-linear-gradient(to top,var(--pc) 0 10px,transparent 10px 18px)}
.stk tr.pur-high+tr.range52-row>td{--pc:var(--target-high);--pw:3px;--pstripe:repeating-linear-gradient(to top,var(--pc) 0 10px,transparent 10px 18px)}
.stk tr.pur-hit+tr.range52-row>td{--pc:var(--purple);--pw:2px;--pstripe:linear-gradient(var(--pc),var(--pc))}
#macroCard tr.mh td{padding:5px 10px;font-size:12.5px;white-space:nowrap} #macroCard tr.mh td:nth-child(3){padding-left:2px}
.macro-help{margin:8px 12px;color:var(--dim);font-size:13px;line-height:1.7}
.macro-help>summary{cursor:pointer;color:#8fb8f0;padding:8px 0;touch-action:manipulation}
.macro-help p{margin:6px 0}
.macro-help>summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:2px}
.page-notes{margin-top:18px}
.page-notes .macro-help{overflow-wrap:anywhere}
.page-notes a{color:#8fb8f0}
.page-notes .macro-help+ .macro-help{border-top:1px solid var(--line);padding-top:4px}
.unit{display:block;font-size:11px;color:var(--dim);font-weight:400}
.foot{color:var(--dim);font-size:12.5px;text-align:center;margin-top:22px;line-height:1.7}
.foot a{color:#8ab4f8;text-decoration:none}
.foot a:hover,.foot a:focus-visible{text-decoration:underline}
.quiet{color:var(--dim);font-size:13px;padding:8px 12px 12px}
.warn{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);
  color:var(--yellow);border-radius:10px;padding:11px 14px;margin-bottom:14px;font-size:14px}
.acts{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
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
/* 持仓里的「长持仓」区：可折叠，默认展开；下方就是短持仓主体，所以底部要留出间距 */
.zone-card{margin:10px 0 12px;border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.022)}
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
/* 展开的那一组整条标题高亮：左边一条蓝＋向左渐隐的底色提亮，
   一眼就能看出「持仓」和里面的「长持仓」到底有没有打开（两条叠在一起时尤其重要）。
   统计面板本来就有蓝边框＋左侧蓝条＋#bfd5ff 组名，默认又是展开的，再叠一层高亮是多余的，故排除。 */
.group-card[open]:not(#signalCounts)>summary,.macro-card[open]>summary,.zone-card[open]>summary{
  background:linear-gradient(90deg,rgba(143,184,240,.20),rgba(143,184,240,.05) 72%,transparent);
  box-shadow:inset 3px 0 0 #8fb8f0}
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
.quiet-list{margin:8px 0}
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
@media(max-width:600px){.oil-reference-row td{padding:8px 6px}.oil-reference-row .reference-detail{font-size:10.5px}#macroCard td:nth-child(3) .unit{font-size:9px}}
.reference-detail{margin:8px 0 0;font-size:12px;color:var(--dim);line-height:1.7}
.reference-detail summary{cursor:pointer;color:#8fb8f0;padding:3px 0}
.reference-detail a{color:#8fb8f0}
.reference-detail p{margin:5px 0}
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
.card .rules-body .lamp-purple{color:#c4a3ff;font-weight:600}
.card .rules-body .tone-target-high{color:var(--target-high);font-weight:600}
.card .rules-body .rule-table{table-layout:fixed;margin:8px 0 12px;border:1px solid #6b7689;font-size:12px;width:100%;-webkit-text-size-adjust:100%;text-size-adjust:100%}
.card .rules-body .rule-table th,.card .rules-body .rule-table td{padding:6px 7px;border:1px solid #5a6478;text-align:left;vertical-align:top;font-size:12px;line-height:1.65;overflow-wrap:anywhere}
.card .rules-body .rule-table thead th{color:var(--text);font-weight:600;background:rgba(143,184,240,.07);border-bottom:1px solid #6b7689}
/* 报灯总表左右撑满卡片：抵消 .card(4px) 与 .rules-body(12px) 的内边距。 */
#rules-lamp-table{margin-left:-16px;margin-right:-16px;width:calc(100% + 32px);border-left:0;border-right:0}
/* 最左分类列：竖排、固定 18px 单字宽（不用 em，避免手机端字号缩放把列撑宽）。
   overflow:hidden 兜底：万一竖排不生效，横排文字被裁掉也不会把列顶宽。 */
.card .rules-body .rule-table col.cat-col{width:18px}
.card .rules-body .rule-table col.col-cond{width:40%}
.card .rules-body .rule-table th.cat-col{width:18px;min-width:18px;max-width:18px;padding:6px 0;line-height:1.1;font-size:12px;letter-spacing:1px;white-space:nowrap;overflow:hidden;text-align:center;vertical-align:middle;background:rgba(143,184,240,.04);writing-mode:vertical-rl;-webkit-writing-mode:vertical-rl;text-orientation:upright;-webkit-text-orientation:upright}
.card .rules-body .ar-band-up{color:#3fb950}
.card .rules-body .ar-band-down{color:#f85149}
.card .rules-body .ar-rsi-up{color:#3ec9d6}
.card .rules-body .ar-rsi-down{color:#e061c9}
/* RSI 的 ↑↓ 继承文字颜色，仅放大字号；布林 ▲▼ 直接使用正文文字。 */
.arrow-rsi-up,.arrow-rsi-down{color:inherit;font-size:1.15em;line-height:1}
/* 分组位置的上下三角：统一分组蓝，跟框标题同色系，不再绿红黄白各一个样。 */
.grp-arrow{color:#bfd5ff;font-weight:700}
.card .rules-body .rule-table .ar-band-up,.card .rules-body .rule-table .ar-band-down,
.card .rules-body .rule-table .ar-rsi-up,.card .rules-body .rule-table .ar-rsi-down{font-weight:700}
/* 同向／反向两行为一组，组间用更亮的横线分开。 */
.card .rules-body .rule-table tr.rule-group-end>th,.card .rules-body .rule-table tr.rule-group-end>td{border-bottom:2px solid #6f7b91}
/* 同一灯级覆盖多个分组：居中并带灯色底色；混合灯与非灯级不加底色。灰灯（折）比灰灯更淡。 */
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
#glossaryRulesBody p{margin:2px 0;line-height:1.5;color:#dce1e9}
#glossaryRulesBody .rule-table{margin:6px 0 8px}
#glossaryRulesBody .rule-table th,#glossaryRulesBody .rule-table td{line-height:1.5}
#glossary-chg-table thead th:nth-child(2),#glossary-amp-table thead th:nth-child(2){color:#f0c674}
#glossary-chg-table thead th:nth-child(3),#glossary-amp-table thead th:nth-child(3){color:#ff8580}
#glossary-chg-table td,#glossary-amp-table td{font-variant-numeric:tabular-nums;white-space:nowrap}
"""
PAGE_CSS += RULES_CSS

# 单独导出供 monitor 替换旧锚点处理器；PAGE_INTERACTION_JS 已含此块，勿重复追加。
RULES_INTERACTION_JS = ""  # 保留导出兼容；规则跳转、hash监听和高亮计时器已删除。
PAGE_INTERACTION_JS = RULES_INTERACTION_JS + r"""
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
// 财报条件 / 紫灯条件的详情和其他灯的名单同属「同时只开一个」：开一个，收起其余。
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
    return Object.assign({}, m, {details: maps.map(function(map) { return map.get(m.id).d.join(" / "); })});
  });
  return {members: members};
}
// 多选清单里的 ↑↓ 必须和主表描述、统计名单用同一套配色/字号（.arrow-rsi-up / .arrow-rsi-down）。
// 以前这里整段用 textContent 写纯文本，箭头既不上色也不放大，所以跟另外两处对不上。
// 逐字符拆成文本节点 + span，不拼 innerHTML，避免把数据当 HTML 解析。
function appendRsiArrows(parent, text) {
  const UP = "↑", DOWN = "↓";
  let buf = "";
  const flush = function() {
    if (buf) { parent.appendChild(document.createTextNode(buf)); buf = ""; }
  };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === UP || ch === DOWN) {
      flush();
      const span = document.createElement("span");
      span.className = ch === UP ? "arrow-rsi-up" : "arrow-rsi-down";
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
    // 灯统一画在标的名称前面：与主表、统计名单同一元素、同一位置、同一尺寸（--lamp-size）。
    // 旧版这里用 9px 的 <i class="stat-dot"> 排在分组后面，跟另外两处既不同位也不同大。
    if (["red", "yellow", "green", "gray", "purple"].indexOf(m.l) >= 0) {
      const dot = document.createElement("span");
      dot.className = "signal-dot " + m.l;
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
    appendRsiArrows(detail, m.details.join(" / "));
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
  if (singleOpen && opening) closeSignalExtras(null);   // 财报条件 / 紫灯条件的详情也算同一组，点别的就收起
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
