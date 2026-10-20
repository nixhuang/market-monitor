"""页面静态资源、共用规则模板和可回归的设置页生成片段。"""
import argparse
import hashlib
import html
import json
from pathlib import Path
import re

from rules_config import DEFAULT_SETTINGS, browser_schema


RULES_SUMMARY = "完整报灯规则 · 全局 + 分组差异"
# 规则定义始终保留；条件仅补充当前启用参数，不用关闭状态替代规则。
RULE_BLOCKS = (
    ("", '<div class="rules-body"><h4>一、报灯 · 所有分组</h4><ul>'),
    ("", '<li><b>行灯取最严重一项</b>：<span class="lamp-red">红</span> 优先于 <span class="lamp-yellow">黄</span>；无报警为 <span class="lamp-green">绿</span>。<span class="lamp-purple">紫色目标提示</span>独立叠加，不替代红黄绿；基本面另挂灯。</li>'),
    ("", '<li><b>量比</b>：盘中 ≥ <b>${vol_ratio_red}×</b> <span class="lamp-red">红</span>、≥ <b>${vol_ratio}×</b> <span class="lamp-yellow">黄</span>；开盘 ${intraday_vol_quiet_min} 分钟内沿用最近完整日。收盘优先按本股放量异常程度报灯，自适应不可用时退回固定倍数；指数、期货不报股票量比。</li>'),
    ("vol_adaptive", '<li class="rule-setting">量比自适应阈值：≥ <b>${vol_ratio_red_z}σ</b> <span class="lamp-red">红</span>、≥ <b>${vol_ratio_z}σ</b> <span class="lamp-yellow">黄</span>；量比至少 1× 才使用。</li>'),
    ("!vol_adaptive", '<li class="rule-setting">当前量比采用固定倍数；重新启用自适应后，按自身异常程度的红、黄阈值判定。</li>'),
    ("", '<li><b>52 周低位</b>：距低点 ≤ <b>${near_52w_low_pct}%</b>，刚进入或再创新低 → <span class="lamp-red">红</span>；持续低位、未创新低 → <span class="lamp-yellow">黄</span>；离开后不报。</li>'),
    ("", '<li><b>第一条均线</b>：确认上穿或跌破 → <span class="lamp-yellow">黄</span>；仅触及不报。确认幅度随平时波动调整，历史不足时按简单穿越判断。</li>'),
    ("", '<li><b>第二条均线</b>：日内触及均线 ±<b>${ma_long_band_pct}%</b>，或上穿、跌破 → <span class="lamp-yellow">黄</span>；触及与穿越可同时提示。缺日内高低时只按现价并注明；旧日线不重报。同周期两条去重后只判简单穿越。</li>'),
    ("ma_short_on", '<li class="rule-setting">第一条当前周期：<b>${ma_short} 日</b>。</li>'),
    ("ma_long_on", '<li class="rule-setting">第二条当前周期：<b>${ma_long} 日</b>。</li>'),
    ("", '<li><b>布林 + RSI</b>：进布林报警区且同方向 RSI <b>2 条</b> → <span class="lamp-yellow">黄</span>、<b>3 条</b> → <span class="lamp-red">红</span>；仅反方向至少 2 条 → <span class="lamp-yellow">黄</span>。同日两轨都命中时文字均显示，组合按最贴近的一轨判断。</li>'),
    ("", '<li class="rule-setting">布林 <b>${boll_n} 日 / ${boll_k} 倍</b>；RSI <b>${rsi_periods}</b>，超买 ≥ <b>${rsi_high}</b>、超卖 ≤ <b>${rsi_low}</b>。日内触轨也算；逼近门槛随平时波动调整，自适应关闭或不可用时用 <b>${boll_near_pct}%</b>。</li>'),
    ("boll_adaptive", '<li class="rule-setting">布林自适应门槛：归一化带宽的 <b>${boll_near_band_pct}%</b>，不是当日实际带宽，不随标准差倍数改变。</li>'),
    ("", '<li><b>加仓价</b>：所有分组，有设置价才判断；已到价或跌破，或现价仅高出 ≤ <b>${trigger_gap_pct}%</b> → <span class="lamp-red">红</span>。</li></ul>'),
    ("", '<h4>二、报灯 · 分组差异</h4><ul>'),
    ("", '<li><b>涨跌幅、振幅</b>：仅持仓、重点关注报灯。中档涨跌幅绝对值 ≥ <b>${chg_red}%</b> <span class="lamp-red">红</span>、≥ <b>${chg_yellow}%</b> <span class="lamp-yellow">黄</span>；振幅 ≥ <b>${amp_red}%</b> <span class="lamp-red">红</span>、≥ <b>${amp_yellow}%</b> <span class="lamp-yellow">黄</span>。低、中、高波动档按 <b>${vol_mult}</b> 缩放，历史不足按中档。</li>'),
    ("", '<li><b>布林上轨</b>：长持仓只有 3 条 RSI 都超买才报红；其他上轨组合不报灯，但保留触轨文字。短持仓、重点关注按通用规则；其他分组上轨只出文字。<b>下轨所有分组照常</b>。</li>'),
    ("", '<li><b>减仓价</b>：持仓已到价或高于价位，或低于价位但还差 ≤ <b>${reduce_gap_pct}%</b> → <span class="lamp-red">红</span>；高出多少都算，不是正负区间。重点关注若已有减仓胶囊，页面也会补同类红色提醒。</li>'),
    ("", '<li><b>目标价、共识价</b>：持仓、重点关注当日上穿或跌破、进入 ±<b>${target_gap_pct}%</b> 区间、或上涨空间 &gt; <b>${target_space_pct}%</b> → <span class="lamp-purple">紫</span>；空间 &gt; <b>${target_anomaly_pct}%</b> → 数据异常 <span class="lamp-red">红</span>。其他分组仅展示共识价，不报目标灯。</li>'),
    ("", '<li><b>基本面</b>：仅持仓、重点关注的非 ETF，单独提示、不改行情行灯；最新季对 <b>3 个季度前</b>。营收变化 ≤ <b>${fund_rev_red}%</b>、营业利润率为负、经营现金流由正转负 → <span class="lamp-red">红</span>。</li>'),
    ("", '<li><b>基本面黄灯</b>：营收变化 ≤ <b>${fund_rev_yellow}%</b>、营业 / 毛利率下降 ≥ <b>${fund_om_drop}</b> / <b>${fund_gm_drop}</b> 个百分点、经营现金流为负；或负债权益比相对升 ≥ <b>${fund_de_rise}%</b> 且负债额升、流动比率相对降 ≥ <b>${fund_cr_drop}%</b>、季度 ROE 相对降 ≥ <b>${fund_roe_drop}%</b> → <span class="lamp-yellow">黄</span>，红优先。</li>'),
    ("", '<li><b>基本面待更新</b>：报告过期（报告周期外另给 <b>${fund_report_grace_days}</b> 天宽限）、缺值或不可比 → <span class="lamp-none">灰字</span>，不当作绿。已知风险仍保留红黄；一次性损益污染时不据 ROE 升灯。</li>'),
    ("!fund_on", '<li class="rule-setting">当前基本面监测未启用；以上规则保留，启用后生效。</li></ul>'),
    ("fund_on", '</ul>'),
    ("", '<h4>三、异色文字与图形</h4><ul>'),
    ("", '<li><b>RSI 单独命中</b>：同侧至少 2 条才出文字；超卖 <span class="lamp-magenta">粉紫字</span>、超买普通字，<b>不单独报灯</b>。</li>'),
    ("", '<li><b>布林单独命中</b>：下轨 <span class="lamp-yellow">黄字</span>、上轨普通字，<b>不单独报灯</b>；连续贴轨只补天数。组合文字按实际等级显示 <span class="lamp-sig-red">红字</span> / <span class="lamp-sig-yellow">黄字</span>。</li>'),
    ("", '<li><b>均线文字</b>：第二条周期的触及、贴近、穿越为 <span class="lamp-cyan">青字</span>，其他均线为普通字。涨跌幅、振幅、量比的信号文字不随行灯自动染红黄。</li>'),
    ("", '<li><b>目标提示</b>：<span class="lamp-purple">紫字、紫胶囊</span>；命中时实心紫并加行右侧紫条，未命中为空心。取数失败为 <span class="lamp-red">红胶囊</span>，本身不升行灯；仅紫字不代表命中。</li>'),
    ("", '<li><b>财报日期</b>：未来 <b>${earn_soon_days} 天</b>内 <span class="lamp-yellow">黄字</span>；未来 <b>7 天</b>另列统计。日期不升行灯，待公布、取数失败不当作临近。</li>'),
    ("", '<li><b>涨跌与盈亏</b>：上涨 / 盈利 <span class="lamp-green">绿字</span>、下跌 / 亏损 <span class="lamp-red">红字</span>。仓位面板红绿仅用于盈亏列，行首圆点表示灯级，不表示盈亏。</li>'),
    ("", '<li><b>定投提醒</b>：顶栏显示还差几个交易日；仅到期当天 <span class="lamp-yellow">黄字</span>，未启用或日期无效不显示，不影响个股灯级。</li>'),
    ("", '<li><b>图形</b>：▲ 上穿 / 上方，▼ 跌破 / 下方，■ 命中区间 / 恰在均线，□ 目标空间提示；量比、布林组合、均线方向写在统计框标题。52 周条的紫三角是现价、<span class="lamp-yellow">黄竖线是成本</span>，两者都不单独报灯。持仓两端价格下的百分比表示<b>从现价跌到低点 / 涨到高点的幅度</b>，不改变灯级。</li></ul>'),
    ("", '<h4>四、展开、折叠与“无异动”</h4><ul>'),
    ("", '<li><b>首次打开</b>：只展开统计；仓位管理、各分组、规则默认收起。持仓展开后，上方为可收起的长持仓，下方直接短持仓表。</li>'),
    ("", '<li><b>不放进“无异动”</b>：生成页面时，行情红 / 黄 / 灰、基本面红 / 黄、参考行情、目标紫色命中、未来 <b>7 天</b>财报，任一满足即在本组主表显示。<b>不是强制打开整个分组</b>。</li>'),
    ("", '<li><b>进入“无异动”</b>：行情绿且没有上述条件。单独 RSI、布林文字、未命中紫字、未来 8～14 天财报黄字，不阻止折叠。浏览器补算的浮亏红灯只改变灯色，<b>不自动搬出原折叠表</b>。</li>'),
    ("", '<li><b>统计灰灯</b>：收纳最终为绿但有 RSI / 布林文字的标的，不等于行情缺失；缺行情、52 周 / 基本面待更新各自标注。信号有计数才显示，点击计数展开名单；默认切换时关闭其他名单，设置允许多开后可跨信号对比，同一行仍互斥。</li>'),
    ("", '<li><b>收起与编辑</b>：收起统计时内部名单复位；收起分组时“无异动”复位，长持仓子区保留状态。当日盈亏贡献默认折叠；收起仓位管理退出编辑，保留已填数字。</li></ul>'),
    ("", '<h4>五、仓位数字与已读提醒</h4><ul>'),
    ("", '<li><b>仓位 &gt; 10%</b>：仅个股仓位数字 <span class="lamp-yellow">黄字</span>，不因此升行灯；现金、固定收益不标黄。名称靠左，数值与输入框居中。</li>'),
    ("", '<li><b>成本价警示：浮亏 ≥ 10%</b>：提醒文字 <span class="lamp-red">红字</span>并附加行级 <span class="lamp-red">红灯</span>；≥ <b>20%</b> 加重提示，仍只显示一条。仅当前持仓且成本、数量均大于零时提醒。</li>'),
    ("", '<li><b>点击已读</b>：加仓价、减仓价、浮亏提醒保留为 <span class="lamp-none">灰字 + 删除线</span>；再次点击撤销，条件消失自动清除。10% 与 20% 分档，跌至 20% 会重新提醒。<b>只撤页面附加红灯，原行情红灯及其他信号保留</b>。</li>'),
    ("", '<li><b>数量与移组</b>：数量为 0 可保存，不计持仓金额、不触发浮亏提醒；移出持仓后不计市值、权重、贡献或亏损，旧成本数量保留，不会自动复活持仓。加减仓、目标及行情规则仍按所在分组判断。</li></ul></div>'),
)


def rule_flags(s):
    equal = s["ma_short"] == s["ma_long"]
    return {"vol_adaptive": s["vol_ratio_z"] > 0 and s["vol_ratio_red_z"] > 0,
            "boll_adaptive": s["boll_near_band_pct"] > 0, "fund_on": s["fund_enable"] > 0,
            "ma_short_on": s["ma_short"] > 0, "ma_long_on": s["ma_long"] > 0,
            "ma_short_distinct": s["ma_short"] > 0 and not equal,
            "ma_long_distinct": s["ma_long"] > 0 and not equal,
            "ma_equal": s["ma_short"] > 0 and equal}


def _text(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return html.escape(str(value), quote=True).replace("&#x27;", "&#39;")


def rules_body(settings, constants):
    values = {**DEFAULT_SETTINGS, **settings, **constants}
    flags = rule_flags(values)
    parts = []
    for condition, body in RULE_BLOCKS:
        if condition and (flags[condition.lstrip("!")] == condition.startswith("!")):
            continue
        parts.append(re.sub(r"\$\{(\w+)\}", lambda m: _text(values[m[1]]), body))
    return "\n".join(parts)


def rules_template(settings, constants):
    return ('<template id="rulesTpl"><summary>' + RULES_SUMMARY + '</summary>'
            + rules_body(settings, constants) + '</template>')


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
    import monitor
    path = Path(monitor.BASE) / "edit.html"
    with path.open(encoding="utf-8", newline="") as stream:
        original = stream.read()
    expected = sync_edit_html(original, monitor.BASE, monitor._rule_constants())
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
  --blue:#2f6bd8; --purple:#a371f7;
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
.tag.f-red{color:#ff9b95;background:rgba(248,81,73,.16);border-color:rgba(248,81,73,.42)}
.tag.f-yellow{color:#f0c674;background:rgba(210,153,34,.16);border-color:rgba(210,153,34,.42)}
.tag.quote-note{color:var(--dim);background:rgba(139,147,161,.1);border-color:var(--line);font-weight:400}
.lvs{display:block;margin:4px 0 0;line-height:1.5}
.lvtag{display:inline-block;margin:0 4px 2px 0;padding:0 7px;border-radius:999px;border:1px solid;
  background:transparent;font-size:11px;font-weight:400;line-height:17px;white-space:nowrap;font-variant-numeric:tabular-nums}
.lvtag.add{color:#ffa94d;border-color:rgba(255,169,77,.6)}
.lvtag.cut{color:#4dd0e1;border-color:rgba(77,208,225,.6)}
.tgs{display:block;margin:3px 0 0;line-height:1.4;white-space:normal}
.tgs .lvtag{margin:0;padding:1px 5px;border-radius:4px;line-height:1.35;white-space:normal;text-align:center}
.tgs .lvtag span{display:inline-block;white-space:nowrap}
.lvtag.tgt{color:#c4a3ff;border-color:rgba(163,113,247,.65)}
.lvtag.tgt.hit{color:#fff;background:#8957e5;border-color:#8957e5}
.lvtag.tgt.bad{color:#ff9b95;border-color:rgba(248,81,73,.7)}
.tnote,.sig .tp{color:#c4a3ff}
.fund-detail{margin-top:6px}
.fund-detail summary{margin-left:0;padding:7px 9px;cursor:pointer;list-style:none;touch-action:manipulation}
.fund-detail summary::-webkit-details-marker{display:none}
.fund-detail summary::after{content:"";display:inline-block;margin-left:6px;
  border:4px solid transparent;border-top-color:currentColor;transform:translateY(2px)}
.fund-detail[open] summary::after{transform:translateY(-2px) rotate(180deg)}
.fund-detail summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:3px}
.fund-body{margin-top:6px;padding:8px;border:1px solid var(--line);border-radius:6px;
  font-size:12px;font-weight:400;line-height:1.6;overflow-wrap:anywhere}
.fund-body ul{margin:4px 0 0;padding-left:16px}
.sig{font-size:13.5px;color:var(--text);overflow-wrap:anywhere}
.signal-magenta{color:#ef8de4} .signal-red{color:#ff8580} .signal-yellow{color:#f0c674} .signal-cyan{color:#4dd0e1}
.group-bottom{padding:0;margin:0 -4px -6px;border-top:1px solid var(--line);text-align:center;line-height:0}
.collapse-group{width:100%;height:26px;padding:0;font:inherit;color:var(--dim);
  display:flex;align-items:center;justify-content:center;box-sizing:border-box;
  background:transparent;border:0;border-radius:6px;cursor:pointer;touch-action:manipulation}
.collapse-group .collapse-icon{font-size:16px;line-height:1}
.collapse-group:hover{color:var(--text);background:transparent}
.collapse-group:focus-visible,.signal-count-toggle:focus-visible{outline:2px solid #8fb8f0;outline-offset:2px}
#signalCounts{border:1px solid #6686c6;box-shadow:inset 3px 0 0 #8fb8f0;background:#182131}
#signalCounts>summary{color:#bfd5ff}
#signalCounts>summary .count-total{white-space:nowrap;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
#signalCounts .count-red{color:var(--red)} #signalCounts .count-yellow{color:var(--yellow)}
#signalCounts .count-purple{color:var(--purple)} #signalCounts .count-earn{color:var(--text)}
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
#signalCounts .signal-members-panel h4{font-size:13px;margin:12px 0 5px;color:#bfd5ff}
#signalCounts .signal-members-panel ul{margin:0;padding-left:18px}
#signalCounts .signal-members-panel li{margin:5px 0;overflow-wrap:anywhere}
/* 名单前面的圆点按这一格的灯色走，不再是默认的白点；灰灯没有灯，用灰点 */
#signalCounts .signal-members-panel li::marker{color:var(--dim)}
#signalCounts .signal-members-panel.tone-red li::marker{color:var(--red)}
#signalCounts .signal-members-panel.tone-yellow li::marker{color:var(--yellow)}
#signalCounts .signal-members-panel.tone-purple li::marker{color:var(--purple)}
/* 基本面也用上面那套 marker 圆点，不再单独画一个大的：整张统计表只有一种灯 */
#signalCounts .text-only .signal-member-list li::marker{color:#9fa3aa}
#signalCounts .signal-members-panel .note{display:inline;margin-left:6px}
#signalCounts .signal-member-direction{color:var(--dim);font-size:12px}
#signalCounts .signal-member-belong{color:var(--dim);font-size:12px}
#signalCounts .signal-member-split{list-style:none;height:0;margin:9px 0;border-top:1px solid #465e87}
#signalCounts .signal-member-metric{margin-left:5px;color:var(--text);font-size:13px}
/* 分框展示：方向写在框标题上（「▲ 上涨」/「▲ 上轨 · RSI 3条过界」），名单里不再逐条画三角 */
#signalCounts .signal-member-block{margin:0 0 10px}
#signalCounts .signal-member-block:last-child{margin-bottom:0}
#signalCounts .signal-member-block h5{margin:0 0 4px;font-size:13px;font-weight:600;color:#bfd5ff}
#signalCounts .signal-member-block h5:first-letter{letter-spacing:0}
#signalCounts .signal-member-block ul{padding-left:18px}
#signalCounts .signal-member-icon{display:inline-block;margin-left:5px;font-size:14px;font-weight:700;line-height:1;vertical-align:baseline}
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
#signalCounts .text-only .text-only-kind{margin:10px 0 4px;padding:0 12px;color:#bfd5ff;font-size:12.5px}
#signalCounts .text-only ul{margin:0 0 10px;padding-left:30px}
#signalCounts .text-only li{margin:5px 0;overflow-wrap:anywhere;color:var(--text);font-size:13px}
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
#signalCounts .earnings-week-meta{color:var(--text);margin-left:5px}
#signalCounts .earnings-week-group{color:#8fb8f0}
#signalCounts .signal-filter-toggle{box-sizing:border-box;height:38px;max-width:100%;padding:0 7px;font:inherit;font-size:14px;line-height:1;color:var(--text);
  display:inline-flex;align-items:center;white-space:nowrap;background:transparent;border:1px solid #4c6591;border-radius:6px;cursor:pointer;text-align:left;touch-action:manipulation}
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
#signalCounts .signal-filter-group{color:#8fb8f0}
#signalCounts .signal-filter .stat-dot{width:9px;height:9px;margin-left:6px;vertical-align:-1px}
#signalCounts .signal-filter-detail{display:block;margin-top:2px;color:var(--text)}
.earn{display:block;margin:0;color:#8fb8f0;font-size:12.5px;font-weight:400;line-height:1.5}
.earn .unit{display:block;color:var(--dim);font-size:12px}
.earn.soon{color:#f0c674;font-weight:600}
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
  .stk tr:has(+tr.range52-row)>td{padding-top:5px;padding-bottom:2px}
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
  .stk .fund-detail summary{padding:6px 7px}
  .group-card>summary.grp,.macro-card>summary.grp{gap:5px;padding:12px 10px 12px 3px}
  .group-card>summary .group-stats,.macro-card>summary .group-stats{gap:6px}
  .zone-card{margin:8px 0 10px}
  .zone-card>summary{padding:8px 8px}
  .zone-card>summary .zone-name{font-size:14px}
  .stat-badge{min-width:16px;height:16px;padding:0 2px;font-size:12px}
}
.fund-context{margin-top:6px;color:var(--dim)}
.up{color:var(--up)} .down{color:var(--down)}
tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red);padding-left:14px}
tr.yellow td:first-child{box-shadow:inset 4px 0 0 var(--yellow);padding-left:12px}
tr.green td:first-child{box-shadow:inset 2px 0 0 var(--green)}
tr.gray td{color:var(--dim)}
.group-card tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red);padding-left:14px}
.group-card tr.pur td.sig{box-shadow:inset -2px 0 0 var(--purple);padding-right:12px}
/* 有52周条的股票：左侧灯条顶部再减去整只股票高度（主行+位置条）的约15%（85%-6.1px，位置条行约22px），延伸覆盖自己的位置条、在条下方4px处结束，与上下股票留出空隙 */
.stk tr:has(+tr.range52-row)>td{--lc:transparent;--lw:0px}
.stk tr.red:has(+tr.range52-row)>td:first-child{--lc:var(--red);--lw:6px}
.stk tr.yellow:has(+tr.range52-row)>td:first-child{--lc:var(--yellow);--lw:4px}
.stk tr.green:has(+tr.range52-row)>td:first-child{--lc:var(--green);--lw:2px}
.stk tr:has(+tr.range52-row)>td:first-child{box-shadow:none;background-image:linear-gradient(var(--lc),var(--lc));background-size:var(--lw) calc(85% - 2.1px);background-position:left bottom;background-repeat:no-repeat}
.stk tr.pur:has(+tr.range52-row)>td.sig{box-shadow:none;background-image:linear-gradient(var(--purple),var(--purple));background-size:2px calc(85% - 2.1px);background-position:right bottom;background-repeat:no-repeat}
.stk tr.range52-row>td{--lc:transparent;--lw:0px;--pc:transparent;--pw:0px;background-image:linear-gradient(var(--lc),var(--lc)),linear-gradient(var(--pc),var(--pc));background-size:var(--lw) 18px,var(--pw) 18px;background-position:left top,right top;background-repeat:no-repeat}
.stk tr.red+tr.range52-row>td{--lc:var(--red);--lw:6px} .stk tr.yellow+tr.range52-row>td{--lc:var(--yellow);--lw:4px}
.stk tr.green+tr.range52-row>td{--lc:var(--green);--lw:2px} .stk tr.pur+tr.range52-row>td{--pc:var(--purple);--pw:2px}
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
.quiet{color:var(--dim);font-size:13px;padding:8px 12px 12px}
.warn{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);
  color:var(--yellow);border-radius:10px;padding:11px 14px;margin-bottom:14px;font-size:14px}
.acts{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
.acts button{padding:8px 14px;font-size:13px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;cursor:pointer;font-family:inherit}
.acts button:disabled{opacity:.5;cursor:not-allowed}
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
.stat-count{display:inline-flex;align-items:center;gap:5px;font-variant-numeric:tabular-nums}
/* 持仓里的「长持仓」区：可折叠，默认展开；下方就是短持仓主体，所以底部要留出间距 */
.zone-card{margin:10px 0 12px;border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.02)}
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
.runlight[data-phase="busy"]{color:var(--yellow)}
.runlight[data-phase="busy"] .dot{background:var(--yellow);box-shadow:0 0 0 3px rgba(210,153,34,.18)}
.runlight[data-phase="partial"]{color:var(--yellow)}
.runlight[data-phase="partial"] .dot{background:var(--yellow);box-shadow:0 0 0 3px rgba(210,153,34,.18)}
.runlight[data-phase="ok"]{color:var(--green)}
.runlight[data-phase="ok"] .dot{background:var(--green);box-shadow:0 0 0 3px rgba(63,185,80,.18)}
.runlight[data-phase="bad"]{color:var(--red)}
.runlight[data-phase="bad"] .dot{background:var(--red);box-shadow:0 0 0 3px rgba(248,81,73,.18)}
.dca{background:rgba(139,147,161,.12);border:1px solid var(--line);border-radius:10px;
  padding:10px 14px;margin-bottom:14px;font-size:14px;color:var(--dim)}
.dca b{color:var(--text)}
.dca.on{background:rgba(210,153,34,.14);border-color:rgba(210,153,34,.4);color:var(--yellow)}
.dca.on b{color:var(--yellow)}
.dca-badge{margin-left:auto;white-space:nowrap;color:var(--text);font:inherit;font-variant-numeric:tabular-nums}
.dca-badge.on{color:var(--yellow)}
/* 定投提醒挂在顶栏最右侧，字号跟顶栏按钮对齐 */
.runbar .dca-badge{font-size:14px;margin-left:auto}
.policy-reference{border-top:1px solid var(--line);margin-top:0}
.policy-reference .reference-alert{padding:7px 12px}
.policy-reference .reference-top{gap:6px;font-size:12.5px}
.policy-reference .stat-dot{width:8px;height:8px}
.policy-reference .reference-status{font-size:11.5px}
.policy-reference [data-status="pending"] .reference-status,.policy-reference [data-status="unavailable"] .reference-status{color:var(--dim)}
.policy-reference [data-status="available"] .reference-status{color:var(--text)}
.policy-reference .reference-conclusion{font-size:13px;line-height:1.55;margin:3px 0 0}
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
.reference-alert{padding:13px 14px;border-bottom:1px solid var(--line)}
.reference-alert:last-of-type{border-bottom:0}
.reference-top{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}
.reference-top .reference-status{margin-left:auto;color:var(--dim);font-size:12px}
.reference-alert[data-level="red"] .reference-status{color:var(--red)}
.reference-alert[data-level="yellow"] .reference-status{color:var(--yellow)}
.reference-alert[data-level="green"] .reference-status{color:var(--green)}
.reference-conclusion{font-size:14px;line-height:1.65;margin:7px 0;color:var(--text)}
.reference-data{font-size:12px;line-height:1.65;color:var(--dim);overflow-wrap:anywhere}
.reference-detail{margin:8px 0 0;font-size:12px;color:var(--dim);line-height:1.7}
.reference-detail summary{cursor:pointer;color:#8fb8f0;padding:3px 0}
.reference-detail a{color:#8fb8f0}
.reference-detail p{margin:5px 0}
.reference-alert[data-level="gray"] .reference-conclusion{color:var(--dim)}
.local-preview-banner{padding:11px 14px;border:1px solid rgba(107,163,240,.35);border-radius:10px;background:rgba(107,163,240,.08);color:#8fb8f0;font-size:13px;margin:14px 0}
"""
PAGE_INTERACTION_JS = r"""
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
document.addEventListener("toggle", function(event) {
  const card = event.target;
  if (!(card instanceof HTMLDetailsElement) || card.id !== "signalCounts") return;
  // 展开和收起都归一化一次：只在收起时做的话，内层「未来一周财报」是在面板已经不可见
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
    const sym = document.createElement("strong");
    sym.textContent = m.s;
    li.append(sym);
    if (m.n) {
      const note = document.createElement("span");
      note.className = "note";
      note.textContent = m.n;
      li.append(note);
    }
    const group = document.createElement("span");
    group.className = "signal-filter-group";
    group.textContent = m.g;
    li.append(" ", group);
    if (["red", "yellow", "green", "gray"].indexOf(m.l) >= 0) {
      const dot = document.createElement("i");
      dot.className = "stat-dot " + m.l;
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label", {red: "红灯", yellow: "黄灯", green: "绿灯", gray: "缺失"}[m.l]);
      li.append(dot);
    }
    const detail = document.createElement("span");
    detail.className = "signal-filter-detail";
    detail.textContent = m.details.join(" · ");
    li.append(detail);
    list.append(li);
  });
  box.append(list);
}
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
    filterButton.setAttribute("aria-pressed", String(filterButton.getAttribute("aria-pressed") !== "true"));
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
