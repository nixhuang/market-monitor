"""规则 schema、页面注入与资源 hash 的纯离线回归；不改用户配置或公开页面。"""
import ast
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor
import page_assets
import rules_config


def plain_text(markup):
    return html.unescape(re.sub(r'<[^>]+>', '', markup))


class RulesRegression(unittest.TestCase):
    def setUp(self):
        p = patch.object(monitor, "S", dict(monitor.DEFAULT_SETTINGS))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(monitor, "_cost_seed_json", return_value="{}")
        p.start()
        self.addCleanup(p.stop)

    def render(self):
        return monitor.render({}, [], 0, snapshot={})

    def test_defaults_and_public_compatibility(self):
        self.assertIs(monitor.DEFAULT_SETTINGS, rules_config.DEFAULT_SETTINGS)
        self.assertIs(monitor.SETTING_LIMITS, rules_config.SETTING_LIMITS)
        self.assertIs(monitor.SETTING_ORDER, rules_config.SETTING_ORDER)
        self.assertEqual(monitor.valid_settings(monitor.DEFAULT_SETTINGS), "")
        self.assertEqual(monitor.DEFAULT_SETTINGS["trigger_gap_pct"], 5)
        self.assertEqual(monitor.DEFAULT_SETTINGS["reduce_gap_pct"], 3)
        self.assertEqual(monitor.DEFAULT_SETTINGS["target_space_pct"], 40)
        self.assertEqual(monitor.DEFAULT_SETTINGS["target_high_space_pct"], 90)
        self.assertEqual(monitor.SETTING_LIMITS["target_space_pct"], (40, 40))
        self.assertEqual(monitor.SETTING_LIMITS["target_high_space_pct"], (90, 90))
        self.assertEqual(monitor.DEFAULT_SETTINGS["rsi_low"], 35)
        self.assertEqual(monitor.DEFAULT_SETTINGS["ma_long"], 200)
        self.assertFalse(any(key.startswith("fund_") for key in monitor.DEFAULT_SETTINGS))
        self.assertFalse(any(key.startswith("fund_") for key in monitor.SETTING_LIMITS))
        self.assertIn('<template id="rulesTpl">', monitor.RULES_TEMPLATE)
        self.assertIn(monitor.RULES_BODY, monitor.RULES_TEMPLATE)

    def test_target_space_fixed_schema_and_legacy_50_are_read_only(self):
        for old in (50, 50.0):
            warnings = []
            got = rules_config.resolve_settings({"target_space_pct": old, "trigger_gap_pct": 7}, warnings.append)
            self.assertEqual((got["target_space_pct"], got["target_high_space_pct"]), (40, 90))
            self.assertEqual(got["trigger_gap_pct"], 7)
            self.assertEqual(warnings, [])
            with patch("builtins.open", mock_open(read_data=json.dumps({"target_space_pct": old}))) as opened, \
                    patch.object(monitor, "log") as warn:
                loaded = monitor.load_settings()
            self.assertEqual((loaded["target_space_pct"], loaded["target_high_space_pct"]), (40, 90))
            opened().write.assert_not_called()
            warn.assert_not_called()
        for key, valid in (("target_space_pct", 40), ("target_high_space_pct", 90)):
            self.assertEqual(rules_config.setting_error(key, valid), "")
            for bad in (valid - 1, valid + 1, True, str(valid), float("nan"), float("inf")):
                with self.subTest(key=key, bad=bad):
                    self.assertTrue(rules_config.setting_error(key, bad))
                    warnings = []
                    got = rules_config.resolve_settings({key: bad}, warnings.append)
                    self.assertEqual(got[key], valid)
                    self.assertEqual(len(warnings), 1)

    def test_target_rules_explain_40_90_boundaries_scope_priority_and_union(self):
        constants = monitor._rule_constants()
        self.assertEqual(constants["target_space_pct"], 40)
        self.assertEqual(constants["target_high_space_pct"], 90)
        dynamic = page_assets.rules_template(monitor.S, constants)
        for label, markup in (("动态规则", dynamic), ("静态模板", monitor.RULES_TEMPLATE),
                              ("渲染页", self.render())):
            with self.subTest(label=label):
                rules = re.search(r'<template id="rulesTpl">(.*?)</template>', markup, re.S)[1]
                glossary = re.search(r'<template id="glossaryTpl">(.*?)</template>', markup, re.S)[1]
                text = plain_text(rules)
                for term in ("目标空间 所有分组", "有效空间≥40% → 紫空心灯",
                             "≥90% → 紫红空心灯", "达到目标／共识价 持仓分组、重点",
                             "当日上穿／跌破或现价在目标±3% → 实心紫灯",
                             "目标空间<40%、未达到目标／共识价、共识获取失败及失败旧价"):
                    self.assertIn(term, text)
                explanation = plain_text(glossary)
                self.assertNotRegex(text + explanation, r'灰灯|不计灰|转灰列|紫(?:红)?虚线空心灯')
                for term in ("有效手填目标价优先", "空间＝（目标／共识价÷现价−1）×100%",
                             "失败旧共识不参与，有效手填价不受影响",
                             "空间／达到各自去重、允许重叠；总数取并集，不直接相加",
                             "所有分组符合条件的美股个股适用", "排除ETF名单、指数、期货与伯克希尔",
                             "达到按昨收与现价判断，不用日内高低曾触及代替"):
                    self.assertIn(term, explanation)
                self.assertNotRegex(text, r'空间[^。；]*[>≥][^。；]*(?:50|100)%|目标空间异常红|红胶囊|仅报数据异常')
                self.assertIn('<span class="lamp-purple">紫空心灯</span>', rules)
                self.assertIn('<span class="tone-target-high">紫红空心灯</span>', rules)
                self.assertIn('<span class="lamp-purple">实心紫灯</span>', rules)

    def test_target_css_preserves_capsule_geometry_and_range52_markers(self):
        css = page_assets.PAGE_CSS
        def declarations(selector):
            return re.search(r'(?:^|\n)' + re.escape(selector) + r'\{([^}]*)\}', css)[1]
        base = declarations('.lvtag')
        for stable in ('margin:0 4px 2px 0', 'padding:0 7px', 'font-size:11px', 'line-height:17px'):
            self.assertIn(stable, base)
        self.assertIn('padding:1px 5px', declarations('.tgs .lvtag'))
        self.assertIn('border:1px solid transparent', declarations('.lvtag.tgt.low'))
        overlay = declarations('.lvtag.tgt.low::after,.lvtag.tgt.bad::after')
        self.assertIn('inset:-1px', overlay)
        self.assertIn('border-radius:inherit', overlay)
        from urllib.parse import unquote
        svg = unquote(page_assets.TARGET_DASH_BORDER)
        self.assertIn('stroke="#8b93a1"', svg)
        self.assertIn('stroke-width="1" stroke-dasharray="3 5"', svg)
        self.assertIn('url("' + page_assets.TARGET_DASH_BORDER + '")', overlay)
        self.assertIn('border:1.5px solid var(--purple)', declarations('.lvtag.tgt.space::after'))
        self.assertIn('border:1.5px solid var(--target-high)', declarations('.lvtag.tgt.high::after'))
        self.assertIn('text-shadow:.2px 0 currentColor,-.2px 0 currentColor', declarations('.lvtag.tgt.high'))
        self.assertNotIn('font-weight', declarations('.lvtag.tgt.high'))
        self.assertIn('text-shadow:none', declarations('.lvtag.tgt.hit'))
        self.assertIn('border:1px solid transparent', declarations('.lvtag.tgt.bad'))
        self.assertIn('color:var(--dim)', declarations('.lvtag.tgt.bad'))
        self.assertIn('inset:-1px', declarations('.lvtag.tgt.space::after,.lvtag.tgt.high::after'))
        self.assertIn('color:#fff;background:#8957e5', declarations('.lvtag.tgt.hit'))
        self.assertGreater(css.index('.lvtag.tgt.hit{'), css.index('.lvtag.tgt.bad{'))
        self.assertNotRegex(css, r'\.lvtag\.tgt\.[^{]+\{[^}]*font-size')
        # 2026-10-27：现价三角只收底宽（10.4 → 8.4px），高度仍 9px 不变 → 顶角 60° 变约 50°（更尖锐）；
        # 内层维持 1px 描边，高度仍是原来的 7px，不跟着缩。
        self.assertIn('border-top:9px solid #efd9ff', declarations('.range52-marker'))
        self.assertIn('border-top:7px solid #713caf', declarations('.range52-marker::before'))
        self.assertIn('background:#f0c674', declarations('.range52-cost'))
        self.assertIn('.up{color:var(--up)} .down{color:var(--down)}', css)
        # 2026-10-30：红灯条与黄灯条等宽（6 → 4px），只有颜色不同。
        for lamp, width in (("red", 4), ("yellow", 4), ("green", 2)):
            self.assertIn(f'--lc:var(--{lamp});--lw:{width}px', css)
        self.assertIn('#signalCounts .signal-name-lamp.tone-target-high{--name-lamp:var(--target-high)}', css)
        for alias in ('tone-target-high', 'target-space-high'):
            self.assertIn(f'#signalCounts .signal-member-block h5.{alias}', css)
        self.assertNotRegex(css, r'(?:tone-target-high|target-space-high)[^{]*\{[^}]*var\(--red\)')

    def test_earnings_text_is_never_bold_and_uses_description_red(self):
        css = page_assets.PAGE_CSS
        # 财报不是涨跌方向：红字跟个股描述红 #ff8580 同色，不能用上涨红 var(--red)。
        self.assertIn('#signalCounts .earnings-week-meta.is-red{color:#ff8580;font-weight:400}', css)
        self.assertIn('#signalCounts .earnings-week-meta.is-yellow{color:#f0c674;font-weight:400}', css)
        self.assertIn('.earn.urgent{color:#ff8580;font-weight:400}', css)
        self.assertIn('.earn.soon{color:#f0c674;font-weight:400}', css)
        # 所有位置的财报文字都不加粗（名单 meta、主表财报单元、多选组合条件）。
        for selector, body in re.findall(r'([^{}]*(?:earnings-week-meta|\.earn)[^{}]*)\{([^{}]*)\}', css):
            if 'summary' in selector:  # 「未来15天财报」这类区块标题照旧加粗
                continue
            with self.subTest(selector=selector.strip()):
                self.assertNotIn('font-weight:600', body)
        # 紫灯条件清单的竖条跟个股自身灯级，未知灯级留灰。
        self.assertIn('#signalCounts .signal-name-lamp.unknown{--name-lamp:var(--dim)}', css)

    def test_glossary_headings_centered_and_metric_names_blue(self):
        css = page_assets.PAGE_CSS
        # 名词解释与抓取规则：两个分区标题页面居中；指标名（h5）统一站内强调蓝。
        self.assertIn('#glossaryRulesBody h4{text-align:center}', css)
        self.assertIn('#glossaryRulesBody h5{color:#8fb8f0}', css)
        # 只在这两块生效：报灯规则的小标题仍是白字。
        self.assertIn('.card .rules-body h5{margin:12px 0 4px;color:var(--text);font-size:12px;font-weight:600}', css)
        # 蓝色规则必须写在通用 h5 规则之后（同文件内顺序即层叠顺序），否则会被白字覆盖。
        self.assertLess(css.index('.card .rules-body h5{'), css.index('#glossaryRulesBody h5{'))
        # 成本面板隔离页同步：名词解释块的分区标题也居中，不动报灯规则 h4。
        cost_css = (ROOT / 'cost_panel.css').read_text(encoding='utf-8')
        self.assertIn('#glossaryRulesBody h4{text-align:center}', cost_css)
        self.assertIn('.rules-body h5{margin:8px 0 3px;font-size:12px;color:#8fb8f0;font-weight:600}', cost_css)
        # 两块分区标题就是「名词解释与计算」「抓取规则」，指标名全部是 h5。
        glossary = page_assets.GLOSSARY_BODY
        self.assertEqual(re.findall(r'<h4>(.*?)</h4>', glossary), ['名词解释与计算', '抓取规则'])
        self.assertEqual(len(re.findall(r'<h5 id="(?:glossary|fetch)-', glossary)), 16)

    def test_purple_ring_and_capsule_share_one_variable(self):
        """分组标题紫灯圈与主表紫色胶囊必须同一个紫：都走 --purple，不允许任何一处写死十六进制紫。"""
        css = page_assets.PAGE_CSS
        # 变量是唯一紫源；旧暗紫 #a371f7 与任何 rgb 形式的写死紫都不许再出现。
        self.assertIn('--purple:#b084ff', css)
        self.assertEqual(css.count('#b084ff'), 1)
        for dead in ('#a371f7', '#c4a3ff', '163,113,247', '196,163,255', '176,132,255'):
            self.assertNotIn(dead, css)
        # 灯圈（计数徽章）与胶囊（共识价标签）本体、描边圈同源。
        self.assertIn('.stat-badge.purple{border-color:var(--purple)}', css)
        self.assertIn('.stat-dot.purple{background:var(--purple)}', css)
        self.assertIn('.lvtag.tgt{position:relative;color:var(--purple);border-color:var(--dim)}', css)
        self.assertIn('.lvtag.tgt.space::after{border:1.5px solid var(--purple)}', css)
        # 紫红（空间≥90%）与实心紫（达到）是另外两档，允许独立取值；其余紫规则只能是 var(--purple)。
        allowed = {'var(--purple)', 'var(--target-high)', '#8957e5'}
        bad = []
        for selector, body in re.findall(r'([^{}]*purple[^{}]*)\{([^{}]*)\}', css):
            for decl in re.findall(r'(?:^|;)(?:color|--name-lamp|--lamp|background|background-color|border-color)\s*:\s*([^;{}]+)', body):
                for token in re.findall(r'#[0-9a-fA-F]{3,8}|var\(--[a-z-]+\)', decl):
                    if token not in allowed:
                        bad.append((selector.strip()[-60:], decl.strip(), token))
        self.assertEqual(bad, [])
        # 成本面板隔离页共用主站变量，不许留自己的一套紫。
        cost_css = (ROOT / 'cost_panel.css').read_text(encoding='utf-8')
        for dead in ('#a371f7', '#c4a3ff', '163,113,247', '196,163,255', '176,132,255'):
            self.assertNotIn(dead, cost_css)
        self.assertIn('.rules-body .lamp-purple{color:var(--purple);font-weight:600}', cost_css)

    def test_settings_page_hosts_the_three_rule_blocks_without_recomputing(self):
        """三块说明搬进设置页：首页只留 inert 模板，设置页读取最近一次构建的同名模板。"""
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        markup = source.split('<script src=', 1)[0]
        self.assertIn('<div id="rulesHost">', markup)
        self.assertIn("fetch('./index.html',{cache:'no-store'})", source)
        # 只读取首页模板，不把规则正文复制一份，也不自己按设置重算阈值。
        for dead in ('RULES_SUMMARY', 'glossary_body(', 'rules_body(', 'lamp_table', '${'):
            self.assertNotIn(dead, source)
        # 样式由 page_assets 生成块注入，设置页不另抄一份 CSS。
        block = re.search(r'/\* MM_RULECSS_BEGIN[^\n]*\*/(.*?)/\* MM_RULECSS_END \*/', source, re.S)
        self.assertIsNotNone(block)
        self.assertEqual(block.group(1).strip(), page_assets.RULES_CSS.strip())
        generated = page_assets.sync_edit_html(source, ROOT, monitor._rule_constants())
        self.assertEqual(generated, source)
        # 首页：三块都是模板，且不再挂载成可见区块。
        page = self.render()
        for template in ('rulesTpl', 'glossaryTpl', 'notesTpl'):
            self.assertEqual(page.count('<template id="%s">' % template), 1)
        self.assertNotIn("['rulesTpl','signalRules']", page)
        self.assertEqual(page.count('class="card rule-card"'), 0)

    def test_frames_replace_highlight_fills_on_stats_cost_and_open_groups(self):
        """统计／仓位／展开分组：外框色统一（1.5px #6686c6，仓位绿 #2c8f3e），底色全部去掉。"""
        css = page_assets.PAGE_CSS
        cost = (ROOT / 'cost_panel.css').read_text(encoding='utf-8')
        # 外框色与宽度只有 :root 一处来源，避免又冒出第二套框线
        self.assertIn('--signal-border:#6686c6; --frame-w:1.5px;', css)
        self.assertEqual(css.count('--signal-border:#6686c6'), 1)
        # 统计面板：只剩外框，卡底 / 名单面板底 / 紫灯财报行底全部去掉，左侧蓝条取消
        self.assertIn('border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}', css)
        self.assertIn('#signalCounts .signal-members-panel{padding:10px 12px;border-top:1px solid #465e87;text-align:left}', css)
        self.assertNotIn('background:#121a27', css)
        self.assertIn('--chip-w:74px;position:relative;display:block;padding:8px 12px 8px 5px;border-top:1px solid #465e87}', css)
        # 手机上紫灯／财报胶囊也同宽 74px，不再比信号列那几颗窄一截
        self.assertIn('@media(max-width:520px){#signalCounts .signal-extra-row{--chip-w:74px;', css)
        self.assertNotIn('background:#182130', css)
        # 展开的分组卡：整卡套外框，标题不再铺底色、不再画左条
        self.assertIn('.group-card[open]:not(#signalCounts):not(#costManage),.macro-card[open]{\n'
                      '  border:var(--frame-w) solid var(--signal-border)}', css)
        # 卡中卡「长持仓」单独再细一档，不跟着整卡 1.5px
        self.assertIn('.zone-card[open]{border:1px solid var(--signal-border)}', css)
        # 长持仓框左右外扩 2px（内容补 2px 内边距，灯条位置不动），区内 ▲ 收起条不再溢出子框
        self.assertIn('.zone-card{margin:10px -2px 12px;padding-left:2px;padding-right:2px;\n'
                      '  border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.022)}', css)
        self.assertIn('.zone-card .group-bottom{margin:0 -2px 0}', css)
        # 数字胶囊：个位压在胶囊中线（右边 17px 内边距给三角留位），两位数时十位向左长，不折行；
        # 胶囊仍是 44px。三角维持线上版：16px 的 ▾/▴ 字形，绝对定位贴到胶囊右侧（距右边框 2px）。
        self.assertIn('#signalCounts .signal-count-toggle{box-sizing:border-box;height:38px;width:44px;'
                      'max-width:100%;\n  position:relative;padding:0 17px 0 4px;', css)
        self.assertIn('white-space:nowrap;display:inline-flex;align-items:center;justify-content:flex-end;'
                      'touch-action:manipulation}', css)
        self.assertIn('#signalCounts .signal-count-toggle::after{content:"\u25be";font-size:16px;'
                      'line-height:1;\n  position:absolute;right:2px;top:50%;transform:translateY(-50%)}', css)
        self.assertIn('#signalCounts .signal-count-toggle[aria-expanded="true"]::after{content:"\u25b4"}', css)
        # 市场风险表：固定列宽，不再让 WTI 参考行那一大段说明把状态列撑到 138.6px
        # （短文案「平静」后面空一大截），也不再把指标列挤到 65.8px（「垃圾债利差」逐字折三行）。
        # 四列不换行所需（含左右各 10px 内边距）实测 126/159/90/114：
        # 宽屏 26/33/18/23 → 184.6/234.3/127.8/163.3；≤600px 30/34/15/21 → 112.5/127.5/56.3/78.8。
        # 指标列仍左对齐，其余三列居中；原油「依据与来源」整块删除，不再留 .reference-detail。
        self.assertIn('#macroCard table{table-layout:fixed}', css)
        self.assertIn('#macroCard td:first-child{width:26%} #macroCard td:nth-child(2){width:33%}\n'
                      '#macroCard td:nth-child(3){width:18%} #macroCard td:nth-child(4){width:23%}\n'
                      '#macroCard td{overflow-wrap:anywhere}\n'
                      '@media(max-width:600px){#macroCard td:first-child{width:30%} '
                      '#macroCard td:nth-child(2){width:34%}\n'
                      '  #macroCard td:nth-child(3){width:15%} '
                      '#macroCard td:nth-child(4){width:21%}}', css)
        self.assertIn('#macroCard td:nth-child(2),#macroCard td:nth-child(3),'
                      '#macroCard td:nth-child(4){text-align:center}', css)
        self.assertNotIn('.reference-detail{', css)
        # 手机：整页左右只留 4px，外框贴住屏幕边；上下沿用 body 原值
        media = re.search(r'@media \(max-width:600px\)\{(.*?)\n\}', css, re.S)
        self.assertIsNotNone(media)
        self.assertIn('body{padding-left:4px;padding-right:4px}', media[1])
        self.assertNotIn('linear-gradient(90deg,rgba(143,184,240,.20)', css)
        self.assertNotIn('inset 3px 0 0 #8fb8f0', css)
        # 「信号」列所有胶囊同宽且文字居中
        self.assertIn('#signalCounts td .signal-filter-toggle{width:74px;padding:0 4px}', css)
        # 仓位管理：整卡绿色外框 1.5px，标头不再铺底色
        self.assertIn('--cost-accent:#2c8f3e', cost)
        self.assertIn('#costManage.cost-manage{--collapse-bg:var(--cost-accent-bg);border:1.5px solid var(--cost-accent);\n'
                      '  box-shadow:none}', cost)
        self.assertIn('#costManage.cost-manage>summary,\n#costManage.cost-manage[open]>summary{background:none;box-shadow:none}', cost)
        self.assertNotIn('inset 3px 0 0 var(--cost-accent-bg)', cost)
        # 「无异动」展开条跟分组标题同一口径：不铺底色、不画左条，只提亮文字
        self.assertIn('.quiet-list[open]>summary{background:none;box-shadow:none;color:#bfd5ff}', cost)
        self.assertNotIn('linear-gradient(90deg,rgba(143,184,240,.18)', cost)

    def test_statistics_table_has_no_signal_red_yellow_white_header_row(self):
        """统计表不再有「信号 / 红 / 黄 / 白」表头排。"""
        page = monitor.render({}, [], 0, snapshot={'signal_counts': {'ma': {'label': '均线', 'red': 1, 'yellow': 0}},
                                                   'summary': {'red': 1, 'yellow': 0}})
        table = page.split('id="signalCounts"', 1)[1].split('</table>', 1)[0]
        self.assertNotIn('class="gray mh"', table)
        for token in ('>信号<', '>红</td>', '>黄</td>', '>白</td>'):
            self.assertNotIn(token, table)
        self.assertTrue(table.split('</summary>', 1)[-1].lstrip().startswith('<table><tr><td><button'))

    def test_target_right_stripes_removed_from_main_and_range52_rows(self):
        css = page_assets.PAGE_CSS
        for state in ('space', 'high', 'hit'):
            self.assertNotIn(f'.group-card tr.pur-{state} td.sig{{', css)
            self.assertNotIn(f'.stk tr.pur-{state}+tr.range52-row>td{{', css)
        self.assertNotIn('.group-card tr.pur td.sig{', css)
        self.assertNotIn('.stk tr.pur:has(+tr.range52-row)>td.sig{', css)
        self.assertNotIn('--pstripe', css)
        self.assertNotIn('--pw:', css)
        tail = re.search(r'(?:^|\n)\.stk tr\.range52-row>td\{([^}]*)\}', css)[1]
        self.assertIn('background-image:linear-gradient(var(--lc),var(--lc))', tail)
        self.assertIn('background-size:var(--lw) 14px', tail)
        self.assertIn('background-position:left top', tail)
        self.assertNotIn('right', tail)
        self.assertNotIn('to top,var(--pc) 0 10px,transparent 10px 18px', css)
        self.assertIn('--target-high:#dc62b8', css)
        self.assertIn('.target-purple>h5{margin:6px 0 3px', css)
        self.assertIn('.target-purple .signal-member-list{padding-bottom:0}', css)
        self.assertIn('.target-purple li{margin:8px 0}', css)
        # 无分隔线的清单（红/黄/白与各灯分框、紫灯 text-only）行距 5 → 8px（2026-10-30）
        self.assertIn('#signalCounts .signal-members-panel li{margin:8px 0;', css)
        self.assertIn('#signalCounts .text-only li{margin:8px 0;', css)
        # 有分隔线的财报/多选筛选清单：行距不动（财报仍无 margin，靠 li+li 的 1px 线分隔）
        self.assertIn('#signalCounts .earnings-week li{font-size:13px;padding:3.6px 0', css)
        self.assertIn('#signalCounts .earnings-week li+li{border-top:1px solid var(--line)}', css)
        self.assertIn('#signalCounts .signal-filter li+li{border-top:1px solid var(--line)}', css)
        self.assertIn('.target-purple .target-space-high .signal-member-direction', css)
        self.assertIn('.target-purple .note{display:inline;margin-left:6px}', css)
        self.assertIn('.target-purple .signal-member-direction{color:var(--purple)}', css)
        self.assertIn('--purple:#b084ff', css)
        self.assertNotIn('#a371f7', css)
        self.assertNotIn('.target-purple .target-space-high .note', css)
        self.assertNotIn('.target-purple .target-space-high .signal-member-belong', css)
        self.assertNotIn('.tone-target-high,.target-space-high{', css)
        self.assertNotIn('#signalCounts .tone-target-high,#signalCounts .target-space-high,', css)
        self.assertIn('.group-card tr.pur-high .sig .tp{color:var(--target-high)}', css)
        # 删除右条后不再存在跨行虚线相位；原左侧灯条续行高度仍为18px。
        self.assertIn('background-size:var(--lw) 14px', tail)
        self.assertNotIn('var(--pw)', tail)

    def test_positions_green_tints_and_shared_collapse_background(self):
        css = page_assets.PAGE_CSS
        self.assertIn('--signal-bg:#182131; --collapse-bg:var(--signal-bg)', css)
        self.assertIn('#group_positions{background:linear-gradient(rgba(46,160,67,.02),rgba(46,160,67,.02)),var(--card);--collapse-bg:rgba(46,160,67,.15)}', css)
        self.assertIn('#group_positions_long.zone-card{background:rgba(46,160,67,.02)}', css)
        self.assertIn('#signalCounts{--collapse-bg:rgba(102,134,198,.6);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}', css)
        button = re.search(r'\n\.collapse-group\{([^}]*)\}', css)[1]
        self.assertIn('background:var(--collapse-bg)', button)
        self.assertIn('.collapse-group:hover{color:var(--text);background:var(--collapse-bg)}', css)
        self.assertNotIn('.collapse-group:hover{color:var(--text);background:transparent}', css)

    def test_invalid_fields_warn_and_preserve_other_overrides(self):
        invalid = {"boll_n": 1, "ma_short": 1, "ma_long": 200.5, "use_realtime": True,
                   "trigger_gap_pct": "7", "reduce_gap_pct": float("nan"),
                   "vol_ratio": float("inf"), "dca_start": "2026-02-30",
                   "unknown_option": 8}
        warnings = []
        got = rules_config.resolve_settings(dict(invalid, chg_red=9), warnings.append)
        for key in invalid:
            self.assertTrue(any(key in line for line in warnings), key)
            if key in monitor.DEFAULT_SETTINGS:
                self.assertEqual(got[key], monitor.DEFAULT_SETTINGS[key])
        self.assertEqual(got["chg_red"], 9)
        self.assertEqual(monitor.valid_settings(got), "")

    def test_order_conflicts_only_reset_their_group(self):
        warnings = []
        got = rules_config.resolve_settings({"rsi_low": 90, "rsi_high": 80,
            "chg_yellow": 20, "chg_red": 10, "trigger_gap_pct": 7}, warnings.append)
        for key in ("rsi_low", "rsi_high", "chg_yellow", "chg_red"):
            self.assertEqual(got[key], monitor.DEFAULT_SETTINGS[key])
        self.assertEqual(got["trigger_gap_pct"], 7)
        self.assertEqual(len(warnings), 2)

    def test_load_settings_discards_retired_fund_overrides_and_does_not_write(self):
        data = {"fund_enable": 0, "fund_om_drop": 8, "fund_report_grace_days": 31,
                "trigger_gap_pct": 7, "reduce_gap_pct": 2, "ma_long": 1}
        with patch("builtins.open", mock_open(read_data=json.dumps(data))) as opened, \
                patch.object(monitor, "log") as warning:
            got = monitor.load_settings()
        for key in ("trigger_gap_pct", "reduce_gap_pct"):
            self.assertEqual(got[key], data[key])
        self.assertFalse(any(key.startswith("fund_") for key in got))
        self.assertEqual(got["ma_long"], 200)
        self.assertIn("ma_long", warning.call_args.args[0])
        opened().write.assert_not_called()

    def test_absent_and_malformed_settings_use_safe_defaults(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            self.assertEqual(monitor.load_settings(), monitor.DEFAULT_SETTINGS)
        for raw in ("[]", "null", "{broken"):
            with self.subTest(raw=raw), patch("builtins.open", mock_open(read_data=raw)), \
                    patch.object(monitor, "log") as warn:
                self.assertEqual(monitor.load_settings(), monitor.DEFAULT_SETTINGS)
                self.assertTrue(warn.called)

    def test_complete_nondefault_rules_injected_before_real_cost_script(self):
        monitor.S.update(trigger_gap_pct=7.25, reduce_gap_pct=1.75, signal_single_open=0)
        page = self.render()
        match = re.search(r"window\.__MM_RULES__=(.*?);</script>", page)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match[1]), monitor.S)
        self.assertLess(match.start(), page.index("var RULES=window.__MM_RULES__"))
        self.assertEqual(page.count("window.__MM_RULES__="), 1)
        rules = re.search(r'<template id="rulesTpl">(.*?)</template>', page, re.S)[1]
        self.assertIn("现价≤设置价×1.0725 → 可点红灯", plain_text(rules))
        self.assertIn("现价≥设置价×0.9825 → 可点红灯", plain_text(rules))
        self.assertNotIn("fund_", match[1])
        self.assertIn('window.MM_SETTINGS={"signal_single_open":0}', page)

    def test_render_does_not_use_import_time_rules_snapshot(self):
        first = self.render()
        monitor.S.update(trigger_gap_pct=9, reduce_gap_pct=0, rsi_low=28, rsi_high=82)
        with patch.object(monitor, "RULES_TEMPLATE", "过期模板不能进入页面"):
            second = self.render()
        self.assertNotEqual(first, second)
        self.assertNotIn("过期模板不能进入页面", second)
        rules = re.search(r'<template id="rulesTpl">(.*?)</template>', second, re.S)[1]
        glossary = re.search(r'<template id="glossaryTpl">(.*?)</template>', second, re.S)[1]
        self.assertIn("现价≤设置价×1.09 → 可点红灯", plain_text(rules))
        self.assertIn("现价≥设置价×1 → 可点红灯", plain_text(rules))
        self.assertIn("≥82超买，≤28超卖", plain_text(glossary))
        self.assertNotIn("≥70超买，≤35超卖", plain_text(glossary))

    def test_add_and_reduce_rules_keep_actual_scope_and_distinct_thresholds(self):
        monitor.S.update(trigger_gap_pct=7, reduce_gap_pct=2)
        for group in ("position", "focus", "technology"):
            x = SimpleNamespace(cfg={"trigger": 100, "reduce": 110}, price=106, group=group)
            add, reduce = monitor._Alerts(), monitor._Alerts()
            monitor._rule_add_price(add, x)
            monitor._rule_reduce_price(reduce, x)
            self.assertEqual(add.level, "red")
            self.assertEqual(reduce.level, "green")
            x.price = 109
            monitor._rule_reduce_price(reduce, x)
            self.assertEqual(reduce.level, "red" if group == "position" else "green")
            x.price = 500
            monitor._rule_reduce_price(reduce, x)
            self.assertEqual(reduce.level, "red" if group == "position" else "green")
        text = page_assets.rules_body(monitor.S, monitor._rule_constants())
        text = plain_text(text)
        self.assertIn("加仓价 · 所有分组：现价≤设置价×1.07 → 可点红灯", text)
        self.assertIn("减仓价 · 持仓分组：现价≥设置价×0.98 → 可点红灯", text)
        self.assertIn("减仓价：重点、其他分组不适用", text)
        self.assertIn("加仓价提醒标记已读 → 白灯", text)
        self.assertIn("减仓价提醒标记已读 → 白灯", text)

    def test_current_three_rule_cells_reach_dynamic_and_rendered_templates(self):
        for settings in ({}, {'rsi_low': 28, 'rsi_high': 82}):
            with patch.dict(monitor.S, settings):
                markup = page_assets.rules_template(monitor.S, monitor._rule_constants())
                for label, source in (('动态模板', markup), ('实际渲染页', self.render())):
                    table = re.search(r'<table[^>]*id="rules-lamp-table"[^>]*>.*?</table>', source, re.S)[0]
                    for condition, expected in (
                            ('布林上轨▲ + RSI 1条 ↓超卖（反）', [(' class="cell-gray"', '白灯'), ('', '仅文字'), ('', '无提示')]),
                            ('RSI 2条 ↑超买', [(' class="cell-gray"', '白灯'), ('', '仅文字'), ('', '无提示')]),
                            ('RSI 1条 ↓超卖', [(' colspan="3"', '仅文字')]),
                            ('涨跌幅 · 自适应', [(' colspan="3"', '红灯／黄灯')]),
                            ('振幅 · 自适应', [(' colspan="3"', '红灯／黄灯')])):
                        with self.subTest(source=label, condition=condition, settings=settings):
                            rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', table, re.S)
                                    if '<td' in row and plain_text(re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1]) == condition]
                            self.assertEqual(len(rows), 1)
                            cells = [(attrs, plain_text(body)) for attrs, body in
                                     re.findall(r'<td\b([^>]*)>(.*?)</td>', rows[0], re.S)]
                            self.assertEqual(cells, [('', condition)] + expected)
                    self.assertIn('text-align:center', page_assets.RULES_CSS)

    def test_amplitude_all_groups_adaptive_boundaries_and_independent_marks(self):
        groups = [g['item_group'] for g in monitor.GROUPS] + ['custom_group']
        for group in groups:
            for tier, sigma, mult in (('low', 1, .6), ('mid', 2, 1), ('high', 3, 1.4)):
                for amp, expected in ((0, None), (.01, None), (6 * mult - .001, None),
                                      (6 * mult, 'yellow'), (10 * mult - .001, 'yellow'),
                                      (10 * mult, 'red'), (12 * mult, 'red')):
                    with self.subTest(group=group, tier=tier, amp=amp), \
                            patch.dict(monitor.S, amp_yellow=6, amp_red=10), \
                            patch.object(monitor, 'vol_profile', return_value=(tier, sigma)), \
                            patch.object(monitor, 'AMP_GROUPS', ()):
                        # 同量级价格构造范围，避免在100附近相减丢失边界精度。
                        x = SimpleNamespace(group=group, closes=[], touch_up=2 * amp if amp else 1,
                                            touch_dn=amp if amp else 1, prev=100, foldable=True)
                        monitor._apply_vol_tier(x)
                        al = monitor._Alerts()
                        al.add('chg', '异动 +6.0%', 'red')
                        monitor._rule_amplitude(al, x)
                        actual = 'red' if x.amp >= x.amp_red else 'yellow' if x.amp >= x.amp_yellow else None
                        self.assertEqual(actual, expected)
                        self.assertEqual(al.marks, [('chg', 'red')] + ([('amp', expected)] if expected else []))
                        self.assertEqual(al.level, 'red')
                        self.assertTrue(x.foldable)
                        self.assertAlmostEqual(x.amp, amp)
        for up, down, prev in ((None, 99, 100), (101, None, 100), (101, 99, 0),
                               (99, 101, 100), (float('nan'), 99, 100), (float('inf'), 99, 100),
                               (101, 99, True), (101, 99, float('inf')), (True, True, 100),
                               (101, -1, 100), (101, 99, -100), (101, 99, 1e-320)):
            x = SimpleNamespace(touch_up=up, touch_dn=down, prev=prev,
                                amp_yellow=6, amp_red=10, foldable=True)
            al = monitor._Alerts()
            monitor._rule_amplitude(al, x)
            self.assertIsNone(x.amp)
            self.assertEqual((al.marks, al.signals, x.foldable), ([], [], True))

    def test_standalone_oversold_reuses_rsi_statistics_kind_without_new_interface(self):
        self.assertIn(('rsi', 'RSI'), monitor.SIGNAL_KINDS)
        self.assertNotIn('rsi_low', dict(monitor.SIGNAL_KINDS))
        for group in (g['item_group'] for g in monitor.GROUPS):
            with self.subTest(group=group):
                x = SimpleNamespace(group=group,
                    rsi={period: monitor.S['rsi_low'] for period in (6, 12, 24)})
                alerts = monitor._Alerts()
                monitor._rule_rsi(alerts, x)
                self.assertEqual(alerts.marks, [('rsi', 'red')])
                self.assertEqual(alerts.level, 'red')
                row = {'symbol': 'OVERSOLD', 'group': group, 'level': alerts.level,
                       'signals': alerts.signals, 'signal_marks': alerts.marks}
                counts = monitor.signal_counts([row])
                members = monitor.signal_count_members([row])
                self.assertEqual(list(counts), [kind for kind, _ in monitor.SIGNAL_KINDS])
                self.assertEqual(counts['rsi'], {'label': 'RSI', 'red': 1, 'yellow': 0, 'purple': 0})
                self.assertEqual(members['rsi']['red'][0]['rsi_hits'], 'RSI 3条 ↓超卖')
                self.assertEqual(monitor.text_only_members([row]), [])
                card = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
                self.assertIn('data-kind="rsi"', card)
                self.assertIn('<h5>RSI 3条 <span class="arrow-rsi-down">↓</span>超卖</h5>', card)
                self.assertNotIn('data-kind="rsi_low"', card)

    def test_standalone_single_oversold_adds_only_low_text_before_boll_combination(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for values in ((35, 50, 50), (70, 35, 50), (70, 70, 35), (70, 35, 35)):
                for side in (None, '上轨', '下轨'):
                    with self.subTest(group=group, values=values, side=side):
                        x = SimpleNamespace(group=group, rsi=dict(zip((6, 12, 24), values)),
                                            boll_hits=[] if side is None else [(0, '触及布林' + side)])
                        alerts = monitor._Alerts()
                        monitor._rule_rsi(alerts, x)
                        high, low = values.count(70), values.count(35)
                        expected = ([f'RSI {high}条 ↑超买（≥70）'] if high >= 2 else [])
                        expected += ([f'RSI {low}条 ↓超卖（≤35）'] if low >= 2 else [])
                        if max(high, low) >= 2:
                            expected += (['RSI 1条 ↑超买（≥70）'] if high == 1 else [])
                            expected += (['RSI 1条 ↓超卖（≤35）'] if low == 1 else [])
                        elif side is None:
                            expected += ['RSI 1条 ↓超卖（≤35）']
                        self.assertEqual(alerts.signals, expected)
                        self.assertEqual((alerts.level, alerts.marks), ('green', []))
                        if side:
                            monitor._rule_boll(alerts, x)
                            self.assertEqual(alerts.signals.count('RSI 1条 ↓超卖（≤35）'), int(low == 1))
                            self.assertEqual(alerts.signals.count('RSI 1条 ↑超买（≥70）'), int(high == 1))
                            facts = monitor.classify_technical_facts(group, high, low, side)
                            self.assertEqual(x.foldable, facts['foldable'])
                            self.assertEqual(alerts.marks, [(facts['kind'], facts['level'])]
                                             if facts['level'] in ('red', 'yellow') else [])

    def test_standalone_three_overbought_keeps_position_yellow_and_focus_gray(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            with self.subTest(group=group):
                x = SimpleNamespace(rsi={period: monitor.S['rsi_high'] for period in (6, 12, 24)}, group=group)
                alerts = monitor._Alerts()
                monitor._rule_rsi(alerts, x)
                level = 'yellow' if group == 'position' else 'green'
                self.assertEqual(x.foldable, group != 'focus')
                self.assertEqual(alerts.level, level)
                self.assertEqual(alerts.marks, [('rsi', 'yellow')] if level == 'yellow' else [])
                row = {'symbol': 'OVERBOUGHT', 'group': group, 'level': level,
                       'signals': alerts.signals, 'signal_marks': alerts.marks}
                members = monitor.signal_count_members([row])
                self.assertEqual(monitor.signal_counts([row])['rsi']['yellow'], int(level == 'yellow'))
                if level == 'yellow':
                    self.assertEqual(members['rsi']['yellow'][0]['indicators'],
                                     [{'shape': '↑', 'tone': 'yellow', 'label': 'RSI 3条 ↑超买'}])
                else:
                    self.assertEqual(monitor.text_only_members([row])[0]['kind'], 'rsi')

    def test_focus_upper_two_rsi_is_gray_visible_and_preserves_other_lamps(self):
        for high, low in ((2, 0), (2, 1), (0, 2), (1, 2)):
            self.assertEqual(monitor.classify_technical_facts('focus', high, low, '上轨'),
                             dict(kind='boll_rsi', level='gray', foldable=False))
            self.assertEqual(monitor.classify_technical_facts('position', high, low, '上轨'),
                             dict(kind='boll_rsi', level='yellow', foldable=True))
            self.assertEqual(monitor.classify_technical_facts('focus', high, low, '下轨'),
                             dict(kind='boll_rsi', level='yellow', foldable=True))
            values = [monitor.S['rsi_high']] * high + [monitor.S['rsi_low']] * low + [50] * (3 - high - low)
            for chg in (0, -6, 6):
                with self.subTest(high=high, low=low, chg=chg):
                    x = SimpleNamespace(group='focus', rsi=dict(zip((6, 12, 24), values)),
                                        boll_hits=[(0, '触及布林上轨▲')], chg=chg, chg_red=4, usual='')
                    alerts = monitor._Alerts()
                    monitor._rule_chg_red(alerts, x)
                    monitor._rule_rsi(alerts, x)
                    monitor._rule_boll(alerts, x)
                    self.assertEqual((alerts.level, alerts.marks),
                                     ('red', [('chg', 'red')]) if chg else ('green', []))
                    self.assertFalse(x.foldable)
                    self.assertFalse(any('布林 + RSI →' in text for text in alerts.signals))
                    row = dict(symbol='FOCUSGRAY2', group='focus', price=100, level=alerts.level,
                               signals=alerts.signals, signal_marks=alerts.marks, foldable=x.foldable)
                    self.assertEqual(monitor.text_only_members([row])[0]['kind'], 'boll_rsi')
                    self.assertEqual([(g['kind'], g['level']) for g in monitor._signal_description_groups(row)
                                      if g['kind'] in ('rsi', 'boll', 'boll_rsi')], [('boll_rsi', 'gray')])

    def test_upper_three_oversold_matches_yellow_rule_cells_and_keeps_change_red(self):
        condition = '布林上轨▲ + RSI 3条 ↓超卖（反）'
        rules = page_assets.rules_body(monitor.S, monitor._rule_constants())
        rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', rules, re.S)
                if '<td' in row and plain_text(re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1]) == condition]
        self.assertEqual(len(rows), 1)
        cells = re.findall(r'<td\b([^>]*)>(.*?)</td>', rows[0], re.S)
        self.assertEqual([(attrs, plain_text(text)) for attrs, text in cells[1:]],
                         [(' class="cell-yellow"', '黄灯'), (' class="cell-yellow"', '黄灯'),
                          (' class="cell-gray-fold"', '白灯（折）')])
        self.assertIn('.signal-item[data-signal-level="yellow"]{color:#f0c674}', page_assets.PAGE_CSS)
        for index, group in enumerate(('position', 'focus'), 1):
            facts = monitor.classify_technical_facts(group, 0, 3, '上轨')
            self.assertEqual(facts, dict(kind='boll_rsi', level='yellow', foldable=True))
            self.assertEqual(plain_text(cells[index][1]), '黄灯')
            for chg in (0, -6, 6):
                with self.subTest(group=group, chg=chg):
                    x = SimpleNamespace(group=group, rsi={p: monitor.S['rsi_low'] for p in (6, 12, 24)},
                                        boll_hits=[(0, '触及布林上轨▲')], chg=chg, chg_red=4, usual='')
                    alerts = monitor._Alerts()
                    monitor._rule_chg_red(alerts, x)
                    monitor._rule_rsi(alerts, x)
                    monitor._rule_boll(alerts, x)
                    self.assertEqual(alerts.level, 'red' if chg else 'yellow')
                    self.assertEqual(alerts.marks, ([('chg', 'red')] if chg else []) + [('boll_rsi', 'yellow')])
                    self.assertIn('布林 + RSI → 黄', alerts.signals)
                    self.assertNotIn('布林 + RSI → 红', alerts.signals)
                    self.assertEqual((x.boll_rsi_direction, x.boll_rsi_hits, x.foldable),
                                     ('上轨（RSI超卖，方向不一致）', 3, True))
                    counts = monitor.signal_counts([dict(symbol='UPOS', group=group, price=100,
                        level=alerts.level, signals=alerts.signals, signal_marks=alerts.marks)])
                    self.assertEqual((counts['boll_rsi']['red'], counts['boll_rsi']['yellow']), (0, 1))
                    self.assertEqual(counts['chg']['red'], int(chg != 0))
                    self.assertTrue(all(counts[k][c] == 0 for k in ('rsi', 'boll') for c in ('red', 'yellow')))

    def test_rsi_standalone_lights_are_disabled_for_any_selected_combination(self):
        for side in ('上轨', '下轨'):
            for boundary in ('rsi_low', 'rsi_high'):
                for group in (g['item_group'] for g in monitor.GROUPS):
                    with self.subTest(side=side, boundary=boundary, group=group):
                        x = SimpleNamespace(rsi={period: monitor.S[boundary] for period in (6, 12, 24)},
                                            group=group, boll_hits=[(0, f'触及布林{side}')])
                        alerts = monitor._Alerts()
                        monitor._rule_rsi(alerts, x)
                        self.assertEqual(alerts.marks, [])
                        self.assertEqual(alerts.level, 'green')
                        self.assertEqual(len(alerts.signals), 1)

    def test_refined_glossary_keeps_dual_wording_and_two_approved_changes(self):
        import html as H
        markup = page_assets.glossary_body(monitor.S, monitor._rule_constants())
        text = H.unescape(re.sub(r'<[^>]+>', '', markup))
        for phrase in (
            '双轨命中时，只选带符号距离较小的一轨；越轨距离为负，相等选上轨。',
            '浮亏10%／20%分档记录、每只只计一次',
            '例：已记录美东20日财报，正常缓存未到期，22日起可提前刷新。',
        ):
            self.assertEqual(text.count(phrase), 1)
        self.assertNotIn('21日不因晨星更新而重抓', text)
        self.assertNotIn('晨星新价不代表', text)
        self.assertIn('不抓晨星', text)
        self.assertIn('Cboe官方VIX日线优先', text)
        self.assertIn('标的去重与显示优先级', text)
        self.assertNotRegex(markup, r'\$\{\w+\}')
        self.assertEqual(markup.count('<table '), 10)  # 两项阈值、三项连续示例、五项抓取表。
        for ident in ('glossary-chg-table', 'glossary-amp-table', 'glossary-vol-table', 'fetch-reference'):
            self.assertEqual(markup.count(f'id="{ident}"'), 1)
        self.assertEqual(re.search(r'<section id="glossary-calculation">(.*?)</section>', markup, re.S)[1].count('<table '), 5)
        self.assertEqual(re.search(r'<section id="glossary-fetch">(.*?)</section>', markup, re.S)[1].count('<table '), 5)

    def test_home_rules_explain_cost_membership_and_boll_upper_without_jargon(self):
        text = page_assets.rules_body(monitor.S, monitor._rule_constants())
        glossary = page_assets.glossary_body(monitor.S, monitor._rule_constants())
        rules_text, glossary_text = plain_text(text), plain_text(glossary)
        self.assertIn('单独RSI', rules_text)
        self.assertRegex(rules_text, r'RSI 3条 ↓超卖\s*红灯')
        self.assertIn('重点、其他分组不适用', rules_text)
        self.assertNotIn('只撤页面追加的该项红灯，不撤生成红灯或其他原因的灯', glossary_text)
        for explanation in ('统计由对应红列转白列', '白字删除线',
                            '双轨命中时，只选带符号距离较小的一轨', '越轨距离为负，相等选', '标的去重'):
            self.assertIn(explanation, glossary_text)
            self.assertNotIn(explanation, rules_text)
        self.assertEqual(len(re.findall(r'<h4\b', text)), 3)
        self.assertEqual(len(re.findall(r'<h4\b', glossary)), 2)
        page = self.render()
        self.assertEqual(page.count('<template id="rulesTpl">'), 1)
        self.assertEqual(page.count('<template id="glossaryTpl">'), 1)
        notes = monitor._page_notes({})
        self.assertIn('<summary>综合判断与宏观细则</summary>', notes)
        self.assertEqual(notes.count('同类只取最高分'), 1)
        self.assertNotIn('<details class="macro-help" id="dataSourceRules"', page)
        self.assertNotIn('id="policyAlertDetails"', notes)  # 无正式消息不再制造规则占位块。
        for duplicate in ('财报日期：', '数据来源：', '五指标综合市场风险：'):
            self.assertNotIn(duplicate, notes)
        self.assertIn('id="dataSourceRules"', glossary)  # 旧来源链接锚点迁入新稿。
        self.assertIn('Cboe官方VIX日线优先', glossary)
        self.assertIn(monitor.BREADTH_CREDIT, glossary)
        self.assertNotIn('href="#marketRiskRules"', page)
        self.assertNotIn('href="#signalRules"', page)
        self.assertNotIn('href="#glossary-fetch"', page)
        self.assertNotIn('<div class="foot">', page)
        self.assertNotIn('href="./edit.html?rules=1"', page)

    def test_static_rules_template_and_macro_footer_explain_new_earnings_window(self):
        import html as H
        body = page_assets.rules_body(monitor.S, monitor._rule_constants())
        template = page_assets.rules_template(monitor.S, monitor._rule_constants())
        page = self.render()
        rendered_template = re.search(r'<template id="rulesTpl">(.*?)</template>', page, re.S)
        self.assertIsNotNone(rendered_template)
        glossary = re.search(r'<template id="glossaryTpl">(.*?)</template>', page, re.S)
        self.assertIsNotNone(glossary)
        for label, markup in (('动态规则正文', body), ('静态规则模板', template),
                              ('导入时兼容模板', monitor.RULES_TEMPLATE),
                              ('渲染页 rulesTpl', rendered_template[1]), ('名词解释模板', glossary[1])):
            with self.subTest(label=label):
                text = H.unescape(re.sub(r'<[^>]+>', '', markup))
                self.assertNotRegex(text, r'行灯|两个完整日|两整日|美东今天')
                self.assertNotRegex(text, r'(?:未来\s*)?(?:8\s*[～~至–-]\s*)?14\s*天[^。；\n]*黄|黄[^。；\n]*14\s*天')
                terms = ('前一日／当日／后一日 → 红字', '财报前2～7天 → 黄字', '北京时间')
                if label == '名词解释模板':
                    terms = ('北京今天', '盘前／盘后不平移', '另留昨天红字')
                for term in terms:
                    self.assertIn(term, text)
        plain_glossary = plain_text(glossary[1])
        for term in ('用美东财报日期对比北京今天', '红字保留期结束后才检查下一次',
                     '失败时沿用有效旧日期', '名单含今天至+15天', '待公布', '未知不猜钟点'):
            self.assertIn(term, plain_glossary)
        self.assertNotIn('href="#rule-earnings"', glossary[1])
        self.assertIn('用美东财报日期对比北京今天', plain_glossary)
        self.assertIn('财报前2～7天', H.unescape(re.sub(r'<[^>]+>', '', body)))
        self.assertNotIn('财报日期：', monitor._page_notes({}))
        visible = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', page, flags=re.S | re.I)
        visible = H.unescape(re.sub(r'<[^>]+>', '', visible))
        self.assertNotIn('行灯', visible)
        plain = H.unescape(re.sub(r'<[^>]+>', '', body))
        self.assertRegex(plain, r'8\s*[～~至–-]\s*15\s*天[^。；]*名单')
        self.assertIn('不折叠', plain)
        self.assertIn('无异动', plain)

    def test_ma_zero_and_equal_period_explanations(self):
        for short, long in ((0, 0), (0, 200), (50, 0), (50, 50)):
            with self.subTest(short=short, long=long):
                monitor.S.update(ma_short=short, ma_long=long)
                self.assertFalse(monitor.valid_settings(monitor.S))
                body = page_assets.rules_body(monitor.S, monitor._rule_constants())
                rules_text = plain_text(body)
                glossary = plain_text(page_assets.glossary_body(monitor.S, monitor._rule_constants()))
                self.assertIn("均线（短均线穿越；长均线触及±1%或穿越）", rules_text)
                self.assertRegex(rules_text, r"长均线触及±1%或穿越）\s*黄灯")
                self.assertIn("两条同周期只计一次，按简单穿越判断", glossary)
                self.assertNotIn("均线已关闭", rules_text)
                self.assertNotIn("当前0日", rules_text)
                for name, period in (("短均线", short), ("长均线", long)):
                    status = "启用" if period else "关闭"
                    self.assertIn(name + "（" + status + "）", glossary)
                self.assertNotRegex(rules_text + glossary, r'当前\d+日|\d+日均线|第一条|第二条|2～250')
                x = monitor._analysis_context("OFFLINE", {},
                    {"closes": [100.0] * 60, "price": 100, "prev_close": 100,
                     "highs": [], "lows": [], "dates": []}, "position")
                self.assertEqual([p for p, _ in x.moving_averages], list(dict.fromkeys(p for p in (short, long) if p)))

    def test_volume_slight_tightening_boundaries_and_fixed_rules_unchanged(self):
        current = dict(monitor.DEFAULT_SETTINGS)
        previous = dict(current, vol_ratio_z=1.8, vol_ratio_red_z=3.6)
        self.assertEqual((current['vol_ratio_z'], current['vol_ratio_red_z']), (2, 3.6))
        self.assertEqual((current['vol_ratio'], current['vol_ratio_red']), (1.5, 3))
        with patch.object(monitor, 'BASE', str(ROOT)):
            loaded = monitor.load_settings()
        self.assertEqual((loaded['vol_ratio_z'], loaded['vol_ratio_red_z']), (2, 3.6))
        def level(settings, ratio, distribution=True, intraday=False):
            x = SimpleNamespace(vol_ratio=ratio, vol_ratio_z=999, vol_ratio_date='2026-10-22',
                                vol_ratio_intraday=intraday, rt_vol_ratio=ratio, rt_vol_time='12:00')
            if distribution:
                x.vol_ratio_m, x.vol_ratio_s = 0, 0.3
            alerts = monitor._Alerts()
            with patch.object(monitor, 'S', settings):
                monitor._rule_vol_ratio(alerts, x)
            return alerts.level
        yellow = math.exp((2 * 0.3 + math.log(1.5)) / 2)
        red = math.exp((3.6 * 0.3 + math.log(3)) / 2)
        for ratio, expected in ((yellow * (1 - 1e-12), 'green'), (yellow * (1 + 1e-12), 'yellow'),
                                (red * (1 - 1e-12), 'yellow'), (red * (1 + 1e-12), 'red')):
            self.assertEqual(level(current, ratio), expected)
        severity = {'green': 0, 'yellow': 1, 'red': 2}
        for i in range(10, 101):
            ratio = i / 10
            self.assertLessEqual(severity[level(current, ratio)], severity[level(previous, ratio)])
            self.assertEqual(level(current, ratio) == 'red', level(previous, ratio) == 'red')
        # k 黄由1.8收紧为2仍有效，融合公式使用原设置，而非硬编码档位。
        self.assertEqual((level(previous, 1.62), level(current, 1.62)), ('yellow', 'green'))
        for intraday in (False, True):
            for ratio, expected in ((1.499999, 'green'), (1.5, 'yellow'),
                                    (2.999999, 'yellow'), (3, 'red')):
                self.assertEqual(level(current, ratio, False, intraday), expected)
                self.assertEqual(level(current, ratio, False, intraday), level(previous, ratio, False, intraday))
        text = plain_text(page_assets.rules_body(current, monitor._rule_constants()))
        self.assertIn('收盘：达自适应门槛报红／黄', text)
        self.assertIn('红≥3×、黄≥1.5×', text)
        self.assertIn('红k＝3.6、黄k＝2', plain_text(page_assets.glossary_body(current, monitor._rule_constants())))

    def test_statistics_collapse_bar_shares_border_color_only_in_statistics(self):
        self.assertIn('#signalCounts{--collapse-bg:rgba(102,134,198,.6);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}', page_assets.PAGE_CSS)
        self.assertIn('#signalCounts .collapse-group{color:#fff}', page_assets.PAGE_CSS)
        self.assertIn('--collapse-bg:rgba(46,160,67,.15)', page_assets.PAGE_CSS)
        self.assertNotIn('bar.appendChild(shortcut)', page_assets.RULES_INTERACTION_JS)

    def test_volume_adaptive_off_on_and_fixed_fallback(self):
        for yellow, red in ((0, 3.6), (1.8, 0), (0, 0), (1.8, 3.6)):
            with self.subTest(yellow=yellow, red=red):
                monitor.S.update(vol_ratio_z=yellow, vol_ratio_red_z=red, vol_ratio=2, vol_ratio_red=4)
                self.assertFalse(monitor.valid_settings(monitor.S))
                active = yellow > 0 and red > 0
                self.assertEqual(monitor._vol_ratio_z_enabled(), active)
                al = monitor._Alerts()
                monitor._rule_vol_ratio(al, SimpleNamespace(vol_ratio=2.5, vol_ratio_z=5,
                    vol_ratio_m=-1, vol_ratio_s=0.2,
                    vol_ratio_date="2026-10-01", vol_ratio_intraday=False))
                self.assertEqual(al.level, "red" if active else "yellow")
                text = page_assets.rules_body(monitor.S, monitor._rule_constants())
                self.assertEqual(page_assets.rule_flags(monitor.S)["vol_adaptive"], active)
                self.assertIn('红≥4×、黄≥2×', plain_text(text))
                glossary = plain_text(page_assets.glossary_body(monitor.S, monitor._rule_constants()))
                self.assertIn(f'红k＝{red:g}、黄k＝{yellow:g}', glossary)
                self.assertIn('收盘分布有效、两条自适应参数启用且量比≥1×', glossary)
                self.assertIn('新门槛＝√(T×F)', glossary)
                self.assertIn('自适应未命中，不再用固定倍数补灯', glossary)
                self.assertIn('盘中量比＝（当日累计量÷已开盘分钟）÷', glossary)
                self.assertIn('按已开盘进度放宽', glossary)
                self.assertIn('放宽的是门槛，显示仍是原始折算值', glossary)
                self.assertNotIn('盘中量比＝（当日累计量÷已开盘分钟数）÷', glossary)

    def test_boll_adaptive_matches_actual_formula_and_off_switch(self):
        monitor.S.update(boll_near_band_pct=25, boll_near_pct=0.75, boll_n=30, boll_k=3)
        self.assertAlmostEqual(monitor._boll_near_pct(SimpleNamespace(vol_sigma=2)),
                               0.25 * 4 * monitor._BOLL_SIGMA_FACTOR * 2)
        self.assertEqual(monitor._boll_near_pct(SimpleNamespace(vol_sigma=None)), 0.75)
        body = page_assets.glossary_body(monitor.S, monitor._rule_constants())
        text = plain_text(body)
        factor = 0.25 * 4 * monitor._BOLL_SIGMA_FACTOR
        self.assertIn("布林为30日、±3倍标准差", text)
        self.assertIn(f"历史日波动×{factor:g}（当前25%设置）", text)
        self.assertIn("不是当天带宽的25%", text)
        for sigma in (1, 2, 3):
            self.assertIn(f"≤约{round(factor * sigma, 2):g}%", text)
        monitor.S["boll_k"] = 2
        self.assertEqual(monitor._boll_near_pct(SimpleNamespace(vol_sigma=2)), factor * 2)
        monitor.S["boll_near_band_pct"] = 0
        self.assertEqual(monitor._boll_near_pct(SimpleNamespace(vol_sigma=2)), 0.75)
        body = page_assets.glossary_body(monitor.S, monitor._rule_constants())
        self.assertIn("关闭自适应或波动无效时固定0.75%", plain_text(body))
        self.assertIn("历史日波动×0（当前0%设置）", plain_text(body))
        rules = plain_text(page_assets.rules_body(monitor.S, monitor._rule_constants()))
        self.assertIn("布林上轨▲ + RSI 2条 ↑超买黄灯白灯仅文字", rules)
        self.assertIn("布林上轨▲ + RSI 2条 ↓超卖（反）黄灯白灯仅文字", rules)
        self.assertIn("布林下轨▼ + RSI 3条 ↓超卖红灯", rules)

    def test_retired_fund_settings_and_frontend_are_absent(self):
        warnings = []
        got = rules_config.resolve_settings({"fund_enable": True, "fund_om_drop": "旧值",
            "fund_unknown": 99, "trigger_gap_pct": 7}, warnings.append)
        self.assertFalse(any(key.startswith("fund_") for key in got))
        self.assertEqual(got["trigger_gap_pct"], 7)
        self.assertEqual(warnings, [])
        self.assertEqual(rules_config.setting_error("fund_enable", 1), "未知参数")
        self.assertIn("未知参数", rules_config.valid_settings(dict(got, fund_enable=1)))
        self.assertNotIn("fund_", json.dumps(rules_config.browser_schema()))
        tree = ast.parse((ROOT / "rules_config.py").read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(n, ast.ImportFrom) and n.module == "fundamentals"
                             for n in ast.walk(tree)))
        for text in (page_assets.rules_body(got, monitor._rule_constants()),
                     page_assets.PAGE_CSS, page_assets.PAGE_INTERACTION_JS):
            self.assertNotRegex(text, r"基本面|fund[_-]|FUND_")
        self.assertIn("resetQuietLists", page_assets.PAGE_INTERACTION_JS)
        self.assertIn('mm:ack-change', page_assets.PAGE_INTERACTION_JS)
        self.assertIn("北京时间", page_assets.rules_body(got, monitor._rule_constants()))
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        self.assertNotIn('set_fund_', source)
        self.assertNotIn('基本面', source)
        self.assertFalse(any(key.startswith("fund_") for key in
            json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))))

    def test_quiet_list_reset_and_regular_collapse_still_execute(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const events={},windowEvents={};
class Details {}
const context={HTMLDetailsElement:Details,document:{querySelector:()=>null,
  addEventListener:(k,f)=>(events[k]||=[]).push(f)},
  window:{location:{hash:''},addEventListener:(k,f)=>(windowEvents[k]||=[]).push(f)}};
vm.createContext(context);vm.runInContext(fs.readFileSync(0,'utf8'),context);
const sub={open:true,style:{display:''},offsetHeight:1};
const card=new Details();card.open=false;card.offsetHeight=1;
card.querySelector=()=>sub.open?sub:null;
card.querySelectorAll=()=>sub.open?[sub]:[];
for(const f of events.toggle)f({target:card});
assert.equal(sub.open,false);assert.equal(sub.style.display,'');
sub.open=true;card.open=true;
for(const f of events.toggle)f({target:card});
assert.equal(sub.open,true);
const clickEvents=events.click;
const closed=new Details();closed.open=true;let focused=false,scrolled=false;
closed.querySelector=()=>({focus:()=>{focused=true;},scrollIntoView:()=>{scrolled=true;}});
class Element {closest(s){return s==='.collapse-group'?{closest:()=>closed}:null;}}
context.Element=Element;
for(const f of clickEvents)f({target:new Element()});
assert.equal(closed.open,false);assert.equal(focused,true);assert.equal(scrolled,true);
'''
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/run_all.py"),
            "--offline-node", "-e", script], cwd=ROOT, input=page_assets.PAGE_INTERACTION_JS,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_editor_has_only_closed_settings_and_no_duplicate_rule_or_status_ui(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        markup, script = source.split('<script src=', 1)
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, dict(attrs)))
        elements = Elements()
        elements.feed(markup)
        by_id = {attrs['id']: (tag, attrs) for tag, attrs in elements.tags if 'id' in attrs}
        for ident in ('setToggle', 'displayCard', 'importCard', 'dcaCard'):
            with self.subTest(ident=ident):
                tag, attrs = by_id[ident]
                self.assertEqual(tag, 'details')
                self.assertNotIn('open', attrs)
        for tag, attrs in elements.tags:
            if tag == 'details':
                self.assertNotIn('open', attrs)
        for ident in ('effectiveRuleText', 'ruleDetails', 'sourceDetails', 'targetSpaceNote',
                      'appliedCard', 'appliedBrief', 'appliedState', 'dataTime', 'detailCard', 'runSummary'):
            self.assertNotIn(ident, by_id)
            self.assertNotIn(ident, script)
        for phrase in ('布林判定说明', 'RSI判定说明', '量比判定说明', '目标空间固定分档',
                       '单独布林只显示文字', '保存是否生效', '运行详情', 'monitorNote_'):
            self.assertNotIn(phrase, source)
        labels = re.findall(r'<label(?:\s[^>]*)?>(.*?)</label>', markup, re.S)
        parameter_labels = [plain_text(label) for label in labels if '令牌' not in label]
        self.assertTrue(parameter_labels)
        self.assertTrue(all(len(label) <= 30 for label in parameter_labels), parameter_labels)
        expected = set(rules_config.DEFAULT_SETTINGS) - {'target_space_pct', 'target_high_space_pct'}
        actual = {ident.removeprefix('set_') for ident in by_id if ident.startswith('set_')}
        self.assertEqual(actual, expected)
        for ident in ('btnSave', 'btnSaveRules', 'btnResetRules', 'btnSaveDca', 'btnClearDca', 'btnReload'):
            self.assertEqual(by_id[ident][0], 'button')
            self.assertNotIn('hide', by_id[ident][1].get('class', '').split())
        for ident in ('btnRun', 'btnRunNow', 'btnRuns', 'btnCheckStatus', 'runCard', 'runList'):
            self.assertNotIn(ident, by_id)  # 不为删除的区域增加替代入口。
        self.assertIn('id="authCard"', markup)
        self.assertIn('id="manualCard"', markup)
        self.assertIn('Contents: Read and write', markup)
        self.assertIn('href="./index.html"', markup)
        for tag, attrs in elements.tags:
            if tag == 'a':
                self.assertNotRegex(attrs.get('href', ''), r'(?i)(?:[?&]rules=|#(?:signalRules|rule[-\w]*))')
        # 设置页挂载规则说明（id 由模板配对给出），但不提供任何指向规则的站内跳转或入口。
        self.assertIn("['rulesTpl','signalRules'],['glossaryTpl','glossaryRules']", source)
        self.assertIn("doc.getElementById('notesTpl')", source)
        self.assertNotRegex(source, r'#signalRules\b|#glossaryRules\b|#pageNotes\b')
        self.assertNotRegex(script, r"\.get\(['\"]rules['\"]\)")
        self.assertNotRegex(script, r'location\.(?:replace|assign)\s*\(')
        schema = json.loads(re.search(r'const SET_SCHEMA=(.*?);\n', script)[1])
        self.assertEqual([schema['defaults'][k] for k in ('vol_ratio_z', 'vol_ratio_red_z')], [2, 3.6])
        self.assertEqual([schema['defaults'][k] for k in ('vix_green', 'vix_yellow', 'vix_red')], [20, 30, 40])

    def test_editor_ui_removal_survives_generator_without_regeneration_or_writes(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        generated = page_assets.sync_edit_html(source, ROOT, monitor._rule_constants())
        self.assertEqual(source, generated)
        self.assertEqual(generated, page_assets.sync_edit_html(generated, ROOT, monitor._rule_constants()))
        for phrase in ('targetSpaceNote', 'appliedState', 'runSummary', 'monitorNote_', '判定说明'):
            self.assertNotIn(phrase, generated)
        # 规则样式生成块里的 #rules-lamp-table 等是 CSS 选择器，不是站内跳转锚点。
        without_css = re.sub(r'/\* MM_RULECSS_BEGIN[^\n]*\*/.*?/\* MM_RULECSS_END \*/', '', generated, flags=re.S)
        self.assertNotRegex(without_css, r'(?i)(?:[?&]rules=|#rule[-\w]*|#signalRules\b|#glossaryRules\b)')
        self.assertNotRegex(generated, r'<details\b[^>]*\sopen(?:\s|=|>)')
        # 生成器与保存前校验仍保留全部参数，不因删除展示说明缩减规则。
        self.assertEqual(json.loads(re.search(r'const SET_SCHEMA=(.*?);\n', generated)[1]),
                         json.loads(json.dumps(rules_config.browser_schema())))

    def test_sync_check_does_not_import_monitor(self):
        with patch.object(sys, "argv", ["page_assets.py", "--check"]), \
                patch.dict(sys.modules, {"monitor": None}):
            page_assets.main()

    def test_json_injection_escapes_script_end(self):
        monitor.S["dca_start"] = '</script><script>alert("示例")</script>\u2028'
        _, js = monitor._cost_panel_assets([])
        payload = re.search(r"window\.__MM_RULES__=(.*?);</script>", js)[1]
        self.assertNotIn("<", payload)
        self.assertEqual(json.loads(payload), monitor.S)

    def test_static_assets_remain_public_without_duplicate_definitions(self):
        self.assertIs(monitor.PAGE_CSS, page_assets.PAGE_CSS)
        self.assertIs(monitor.PAGE_INTERACTION_JS, page_assets.PAGE_INTERACTION_JS)
        tree = ast.parse((ROOT / "monitor.py").read_text(encoding="utf-8"))
        assigned = {t.id for node in tree.body if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Name)}
        self.assertNotIn("PAGE_CSS", assigned)
        self.assertNotIn("PAGE_INTERACTION_JS", assigned)
        page = self.render()
        self.assertIn(page_assets.PAGE_CSS.removesuffix(page_assets.RULES_CSS), page)
        self.assertEqual(page.count(page_assets.RULES_CSS), 1)
        cost_css, _ = monitor._cost_panel_assets()
        self.assertLess(page.index(cost_css), page.index(page_assets.RULES_CSS))
        self.assertIn(page_assets.PAGE_INTERACTION_JS, page)

    def test_css_cleanup_keeps_active_features_and_drops_retired_selectors(self):
        css = page_assets.PAGE_CSS
        for retired in ('.reference-alert', '.reference-top', '.reference-conclusion',
                        '.reference-data', '.local-preview-banner', '.dca{', '.dca.on{',
                        '.acts button', '.stat-count{', '.gray-signal-row',
                        '.arrow-band-up{', '.arrow-band-down{', '.lamp-sig-red', '.lamp-sig-yellow',
                        'signal-dot'):
            self.assertNotIn(retired, css)
        for active in ('.oil-state{', '.oil-reference-row', '.dca-badge',
                       '.runbar button', '.signal-filter-condition[data-signal-level=', '.signal-name-lamp,', '.grp-arrow',
                       '.arrow-rsi-up,.arrow-rsi-down', '.rule-table th.cat-col'):
            self.assertIn(active, css)
        self.assertEqual(css.count('.stk tr:has(+tr.range52-row)>td{padding-top:5px;padding-bottom:2px}'), 1)
        for phase in ('busy', 'partial', 'ok', 'bad'):
            self.assertIn(f'.runlight[data-phase="{phase}"]', css)

    def test_multi_condition_colors_are_scoped_and_keep_single_text_and_name_lamps(self):
        css = page_assets.PAGE_CSS
        palette = {'red': '#ff8580', 'yellow': '#f0c674', 'gray': 'var(--text)',
                   'purple': 'var(--purple)', 'green': 'var(--green)'}
        for level, color in palette.items():
            selector = f'#signalCounts .signal-filter-condition[data-signal-level="{level}"]'
            with self.subTest(level=level):
                self.assertEqual(css.count(selector + '{color:' + color + '}'), 1)
        # 财报条件不是交易灯：红/黄/灰跟统计「未来15天财报」名单同一套，不能整条白字。
        for level, color in (('red', '#ff8580'), ('yellow', '#f0c674'), ('gray', 'var(--dim)')):
            selector = ('#signalCounts .signal-filter-condition'
                        f'[data-signal-kind="earnings_week"][data-signal-level="{level}"]')
            with self.subTest(level=level):
                self.assertEqual(css.count(selector + '{color:' + color + '}'), 1)
        self.assertNotIn('signal-dot', css)
        self.assertNotIn('signal-dot', page_assets.PAGE_INTERACTION_JS)
        clean_css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
        for selectors, body in re.findall(r'([^{}]+)\{([^{}]*)\}', clean_css):
            for selector in selectors.split(','):
                if '.signal-filter-condition' not in selector:
                    continue
                selector = selector.strip()
                with self.subTest(selector=selector):
                    if selector == '#signalCounts .signal-filter-condition':
                        colors = re.findall(r'(?:^|;)\s*color\s*:\s*([^;]+)', body)
                        self.assertTrue(all(color.strip() == 'inherit' for color in colors))
                    else:
                        self.assertIn(selector, {
                            f'#signalCounts .signal-filter-condition[data-signal-level="{level}"]'
                            for level in palette} | {
                            '#signalCounts .signal-filter-condition'
                            f'[data-signal-kind="earnings_week"][data-signal-level="{level}"]'
                            for level in ('red', 'yellow', 'gray')})
        for single in (
                '#signalCounts .signal-members-panel .note{display:inline;margin-left:6px}',
                '#signalCounts .signal-member-direction,#signalCounts .signal-member-belong{color:var(--dim);font-size:12px}',
                '#signalCounts .signal-member-metric{margin-left:5px;color:var(--text);font-size:13px}',
                # 名单里的描述性数值跟着这一条条件的等级走（红红字／黄黄字／白仍白字），与主表描述同一套配色
                '#signalCounts .signal-members-panel.tone-red .signal-member-metric{color:#ff8580}',
                '#signalCounts .signal-members-panel.tone-yellow .signal-member-metric{color:#f0c674}',
                '#signalCounts .signal-members-panel.tone-gray .signal-member-metric{color:var(--text)}',
                '#signalCounts .signal-member-jump{color:var(--text);text-decoration:none;border-bottom:1px dotted #465e87}',
                '#signalCounts .signal-members-panel p{margin:4px 0;color:var(--text)}'):
            self.assertIn(single, css, '单选文字原有颜色、字号与间距不随多选改动')
        self.assertIn('#signalCounts .signal-name-lamp,\n'
                      '#signalCounts .signal-members-panel li:not(.signal-member-split)::before,\n'
                      '#signalCounts .earnings-week li::before{content:"";display:inline-block;'
                      'width:3px;height:12px;border-radius:1px;background:var(--name-lamp,var(--dim));'
                      'margin-right:6px;vertical-align:-1px}', css)
        for level in ('red', 'yellow', 'green', 'purple'):
            self.assertRegex(css, re.escape(f'#signalCounts .signal-name-lamp.{level}')
                             + r'[^{}]*\{--name-lamp:var\(--' + level + r'\)\}')
        self.assertIn('#signalCounts .text-only .signal-member-list,#signalCounts .signal-name-lamp.gray{--name-lamp:#9fa3aa}', css)

    def test_content_hash_changes_even_with_same_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-status.js"
            path.write_bytes(b"const flag=1;")
            before = path.stat()
            first = page_assets.asset_url(tmp, path.name)
            path.write_bytes(b"const flag=2;")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = page_assets.asset_url(tmp, path.name)
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith(hashlib.sha256(path.read_bytes()).hexdigest()[:16]))
            with patch.object(monitor, "BASE", tmp), patch.object(monitor, "_cost_panel_assets", return_value=("", "")):
                self.assertIn(second, self.render())
                self.assertNotIn(first, self.render())

    def test_data_only_base_uses_actual_source_asset(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(monitor, "BASE", tmp), \
                patch.object(monitor, "_cost_panel_assets", return_value=("", "")):
            self.assertIn(page_assets.asset_url(ROOT, "run-status.js"), self.render())

    def test_resource_hash_and_generated_block_are_cross_platform(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        windows = source.replace("\n", "\r\n")
        self.assertEqual(page_assets.sync_edit_html(windows, ROOT, monitor._rule_constants()), windows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-status.js"
            path.write_bytes(b"const flag=1;\nconst other=2;\n")
            unix = page_assets.asset_url(tmp, path.name)
            path.write_bytes(b"const flag=1;\r\nconst other=2;\r\n")
            self.assertEqual(page_assets.asset_url(tmp, path.name), unix)

    def test_editor_generated_schema_rules_hash_are_current_and_idempotent(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        expected = page_assets.sync_edit_html(source, ROOT, monitor._rule_constants())
        self.assertEqual(source, expected, "请运行 page_assets.py --sync-edit 同步生成片段")
        self.assertEqual(expected, page_assets.sync_edit_html(expected, ROOT, monitor._rule_constants()))
        schema = json.loads(re.search(r"const SET_SCHEMA=(.*?);\n", source)[1])
        self.assertEqual(schema, json.loads(json.dumps(rules_config.browser_schema())))
        for name in ("run-status.js", "list-import.js"):
            self.assertIn(page_assets.asset_url(ROOT, name), source)
        self.assertNotRegex(source, r'\.js\?v=2026\d{4}(?:-\d+)?"')

    def test_editor_hash_refresh_is_limited_to_generated_parts(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("run-status.js", "list-import.js"):
                (Path(tmp) / name).write_bytes((ROOT / name).read_bytes() + b"\n/* offline */")
            result = page_assets.sync_edit_html(source, tmp, monitor._rule_constants())
            self.assertNotEqual(source, result)
            for name in ("run-status.js", "list-import.js"):
                result = result.replace(page_assets.asset_url(tmp, name), page_assets.asset_url(ROOT, name))
            self.assertEqual(result, source)

    def test_browser_and_python_rules_normalization_and_thresholds_agree(self):
        settings = [dict(monitor.DEFAULT_SETTINGS), dict(monitor.DEFAULT_SETTINGS,
            trigger_gap_pct=7.25, reduce_gap_pct=1.75, ma_short=0, ma_long=0,
            boll_near_band_pct=0, vol_ratio_z=0, vol_ratio_red_z=4,
            rsi_low=26.5, rsi_high=82)]
        settings += [dict(monitor.DEFAULT_SETTINGS, ma_short=s, ma_long=l, trigger_gap_pct=t, reduce_gap_pct=r)
                     for s, l, t, r in ((50, 0, 0, 4), (0, 200, 6, 0), (50, 50, 8, 2))]
        overrides = [{"rsi_low": 26.5, "fund_om_drop": 7, "fund_enable": 0},
                     {"ma_short": 0, "ma_long": 25, "vol_ratio_z": 0, "vol_ratio_red_z": 0.5},
                     {"boll_n": True, "ma_long": 1, "dca_start": "2026-02-30",
                      "rsi_low": 80, "rsi_high": 70, "fund_rev_red": -1, "fund_rev_yellow": -10},
                     {"trigger_gap_pct": "8", "reduce_gap_pct": None, "signal_single_open": 0.5},
                     {"target_space_pct": 50, "trigger_gap_pct": 7},
                     {"target_space_pct": 40, "target_high_space_pct": 90},
                     {"target_space_pct": 39, "target_high_space_pct": 89}]
        payload = {"settings": settings, "overrides": overrides}
        # 真实生成块与真实编辑页函数均在 VM 中执行；Node 本身也经同一个离线入口。
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('edit.html','utf8'),input=JSON.parse(fs.readFileSync(0,'utf8'));
const generated=source.match(/\/\* MM_SCHEMA_BEGIN[^\n]*\*\/[\s\S]*?\/\* MM_SCHEMA_END \*\//)[0];
const validate=source.slice(source.indexOf('const DATE_RE='),source.indexOf('let SET='));
const normalize=source.slice(source.indexOf('function normalizeSettings('),source.indexOf('async function loadSettings('));
const collect=source.slice(source.indexOf('function collectSettings('),source.indexOf('function normalizeSettings('));
const context=vm.createContext({});
vm.runInContext(generated+'\n'+validate+'\nlet SET={...SET_DEFS};let els={};const $=id=>els[id]||null;\n'+normalize+'\n'+collect,context);
context.input=input;
const result=vm.runInContext(`(()=>{
 const normalized=input.overrides.map(o=>normalizeSettings(o));
 const errors=input.settings.map(s=>{
   SET={...s};els={};Object.entries(s).forEach(([k,v])=>{els['set_'+k]={value:String(v)};});
   return settingsError();
 });
 SET={...SET_DEFS,fund_om_drop:7,fund_report_grace_days:45};els={set_trigger_gap_pct:{value:'8'},set_reduce_gap_pct:{value:'2'}};
 collectSettings();const saved=JSON.parse(setJsonText());
 return {normalized,errors,saved};
})()`,context);
const cost=fs.readFileSync('cost_panel.js','utf8');
const bindings=cost.slice(cost.indexOf('var RULES=window.__MM_RULES__'),cost.indexOf('// 清仓通过移组完成'));
result.bindings=input.settings.map(s=>{const c=vm.createContext({window:{__MM_RULES__:s},own:(o,k)=>Object.prototype.hasOwnProperty.call(o,k)});vm.runInContext(bindings,c);return [c.GAP_TRIGGER,c.GAP_REDUCE];});
process.stdout.write(JSON.stringify(result));
'''
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/run_all.py"), "--offline-node", "-e", script],
            cwd=ROOT, input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        got = json.loads(result.stdout)
        self.assertNotIn('id="effectiveRuleText"', (ROOT / "edit.html").read_text(encoding="utf-8"))
        self.assertIn('布林上轨▲ + RSI 3条 ↑超买红灯红灯白灯',
                      plain_text(page_assets.rules_body(settings[0], monitor._rule_constants())))
        self.assertEqual(got["errors"], [""] * len(settings))
        self.assertEqual(got["bindings"], [[s["trigger_gap_pct"], s["reduce_gap_pct"]] for s in settings])
        self.assertFalse(any(key.startswith("fund_") for key in got["saved"]))
        self.assertEqual(got["saved"]["trigger_gap_pct"], 8)
        for raw, actual in zip(overrides, got["normalized"]):
            warnings = []
            self.assertEqual(actual["values"], rules_config.resolve_settings(raw, warnings.append))
            self.assertEqual(bool(actual["warnings"]), bool(warnings))

    def test_changed_modules_parse_as_python_311(self):
        for name in ("monitor.py", "monitor_indicators.py", "monitor_rules.py",
                     "monitor_snapshot.py", "monitor_render.py", "rules_config.py",
                     "page_assets.py", "tests/mm_test_rules.py",
                     "tests/mm_test_groups.py", "tests/mm_test_render.py"):
            ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name, feature_version=(3, 11))


class ContinuousVolumeRegression(unittest.TestCase):
    """连续融合门槛、显式分布接口及异常回退的纯 Python 离线覆盖。"""

    def setUp(self):
        self.enterContext(patch.object(monitor, 'S', dict(monitor.DEFAULT_SETTINGS)))

    def alerts(self, ratio, m=0, s=0.3, **fields):
        x = SimpleNamespace(vol_ratio=ratio, vol_ratio_m=m, vol_ratio_s=s,
                            vol_ratio_z=None, vol_ratio_date='2026-10-22', vol_ratio_intraday=False)
        vars(x).update(fields)
        alerts = monitor._Alerts()
        monitor._rule_vol_ratio(alerts, x)
        return alerts

    def test_examples_are_formula_only_and_intermediate_scales_are_continuous(self):
        for s, example_y, example_r in ((0.15, 1.42, 2.27), (0.3, 1.65, 2.97), (0.5, 2.02, 4.26)):
            self.assertEqual(round(math.exp((2 * s + math.log(1.5)) / 2), 2), example_y)
            self.assertEqual(round(math.exp((3.6 * s + math.log(3)) / 2), 2), example_r)
        for m in (-0.1, 0, 0.27):
            for s in (0.15, 0.219, 0.3, 0.417, 0.5):
                yellow = math.sqrt(math.exp(m + 2 * s) * 1.5)
                red = math.sqrt(math.exp(m + 3.6 * s) * 3)
                for ratio, expected in ((yellow * (1 - 1e-12), 'green'), (yellow * (1 + 1e-12), 'yellow'),
                                        (red * (1 - 1e-12), 'yellow'), (red * (1 + 1e-12), 'red')):
                    with self.subTest(m=m, s=s, ratio=ratio):
                        self.assertEqual(self.alerts(ratio, m, s).level, expected)

    def test_exact_log_boundaries_are_inclusive(self):
        # 将两条融合门槛精确构造为1.5及3，覆盖等于与相邻浮点数。
        for boundary, k, fixed, expected, below in ((1.5, 2, 1.5, 'yellow', 'green'),
                                                   (3, 3.6, 3, 'red', 'yellow')):
            s = math.log(fixed) / k
            self.assertEqual(self.alerts(math.nextafter(boundary, 0), s=s).level, below)
            self.assertEqual(self.alerts(boundary, s=s).level, expected)
            self.assertEqual(self.alerts(math.nextafter(boundary, math.inf), s=s).level, expected)

    def test_nondefault_settings_drive_both_thresholds(self):
        monitor.S.update(vol_ratio=2.2, vol_ratio_red=4.4, vol_ratio_z=2.1, vol_ratio_red_z=4.2)
        m, s = 0.17, 0.23
        for k, fixed, expected in ((2.1, 2.2, 'yellow'), (4.2, 4.4, 'red')):
            threshold = math.sqrt(math.exp(m + k * s) * fixed)
            self.assertEqual(self.alerts(threshold * (1 + 1e-12), m, s).level, expected)
        for overrides in ({'vol_ratio_z': 0}, {'vol_ratio_red_z': 0},
                          {'vol_ratio_z': 0, 'vol_ratio_red_z': 0}):
            with patch.dict(monitor.S, overrides):
                self.assertEqual(self.alerts(2.2, m=-100).level, 'yellow')
                self.assertEqual(self.alerts(4.4, m=-100).level, 'red')

    def test_valid_distribution_never_gets_fixed_supplement_or_uses_z(self):
        for z in (None, True, float('nan'), float('inf'), -100, 100):
            self.assertEqual(self.alerts(3, s=1, vol_ratio_z=z).marks, [])
            self.assertEqual(self.alerts(1.45, s=0.15, vol_ratio_z=z).marks, [('vol_ratio', 'yellow')])
        self.assertEqual(self.alerts(3, s=0.5).marks, [('vol_ratio', 'yellow')])
        for group in (g['item_group'] for g in monitor.GROUPS):
            self.assertEqual(self.alerts(3, s=1, group=group).marks, [])

    def test_missing_invalid_distribution_and_legacy_z_only_use_fixed(self):
        for invalid in (None, True, '0.3', float('nan'), float('inf'), -float('inf')):
            for m, s in ((invalid, 0.3), (0, invalid)):
                with self.subTest(m=m, s=s):
                    self.assertEqual(self.alerts(1.5, m, s, vol_ratio_z=100).level, 'yellow')
                    self.assertEqual(self.alerts(3, m, s, vol_ratio_z=-100).level, 'red')
        for s in (0, -0.1):
            self.assertEqual(self.alerts(1.49, s=s).level, 'green')
            self.assertEqual(self.alerts(3, s=s).level, 'red')
        x = SimpleNamespace(vol_ratio=2, vol_ratio_z=100, vol_ratio_date='2026-10-22')
        alerts = monitor._Alerts()
        monitor._rule_vol_ratio(alerts, x)
        self.assertEqual(alerts.marks, [('vol_ratio', 'yellow')])

    def test_ratio_gate_and_log_comparison_do_not_overflow(self):
        for ratio in (None, True, '3', float('nan'), float('inf'), -1, 0, 0.999999):
            self.assertEqual(self.alerts(ratio, m=-100, s=0.1).marks, [])
        self.assertEqual(self.alerts(1, m=-100, s=0.1).level, 'red')
        self.assertEqual(self.alerts(1e308, m=1600, s=0.1).level, 'green')
        self.assertEqual(self.alerts(1e308, m=1400, s=0.1).level, 'red')
        self.assertEqual(self.alerts(3, m=1e308, s=1e308).level, 'green')
        self.assertEqual(self.alerts(1, m=-1e308, s=0.1).level, 'red')

    def test_sub_one_ratio_retains_configurable_fixed_fallback(self):
        # 仅自适应分支有1倍门控；合法自定义固定阈值仍按原口径生效。
        with patch.dict(monitor.S, {'vol_ratio': 0.3, 'vol_ratio_red': 0.7}):
            self.assertEqual(self.alerts(0.5, m=-100, s=0.1).level, 'yellow')
            self.assertEqual(self.alerts(0.8, m=-100, s=0.1).level, 'red')

    def test_intraday_uses_fixed_and_never_adds_completed_day(self):
        for ratio, expected in ((1.499999, 'green'), (1.5, 'yellow'), (2.999999, 'yellow'), (3, 'red')):
            alerts = self.alerts(100, m=-100, s=0.01, vol_ratio_intraday=True,
                                 rt_vol_ratio=ratio, rt_vol_time='12:00')
            self.assertEqual(alerts.level, expected)
            if alerts.signals:
                self.assertEqual(alerts.signals, [f'盘中量比 {ratio:.1f}×（截至 12:00 ET）'])

    def test_distribution_preserves_original_z_definition_and_window(self):
        import statistics
        import monitor_indicators as indicators
        volumes = [1000 * (1 + (i % 7 - 3) / 20) for i in range(81)]
        logs = [math.log(volumes[t] / (sum(volumes[t-20:t]) / 20)) for t in range(20, 81)]
        m = statistics.median(logs)
        s = 1.4826 * statistics.median(abs(v - m) for v in logs)
        distribution = indicators._volume_ratio_distribution(volumes, 80)
        self.assertAlmostEqual(distribution['m'], m, places=12)
        self.assertAlmostEqual(distribution['s'], s, places=12)
        self.assertAlmostEqual(monitor._volume_ratio_zscore(volumes, 80), (logs[-1] - m) / s, places=12)
        self.assertEqual(distribution, indicators._volume_ratio_distribution(volumes + [float('nan')], 80))
        self.assertEqual(monitor._volume_ratio_zscore(volumes, 80), distribution['z'])

    def test_distribution_rejects_insufficient_and_damaged_samples_but_handles_extremes(self):
        import monitor_indicators as indicators
        base = [1000 * (1 + (i % 7 - 3) / 20) for i in range(81)]
        self.assertIsNone(indicators._volume_ratio_distribution(base[:-1], 79))
        self.assertIsNone(indicators._volume_ratio_distribution([1000] * 80 + [3000], 80))
        for invalid in (0, -1, True, '1000', None, float('nan'), float('inf')):
            for index in (0, 40, 80):
                sample = list(base)
                sample[index] = invalid
                self.assertIsNone(indicators._volume_ratio_distribution(sample, 80))
        extremes = [(1e-300 if i % 2 else 1e300) * (1 + (i % 7) / 20) for i in range(81)]
        for sample in ([v / 1000 * 1e308 for v in base], extremes):
            distribution = indicators._volume_ratio_distribution(sample, 80)
            self.assertIsNotNone(distribution)
            self.assertTrue(all(math.isfinite(distribution[k]) for k in ('m', 's', 'z')))
            self.assertGreater(distribution['s'], 0)

    def test_public_distribution_api_and_old_context_mock_compatibility(self):
        import monitor_indicators as indicators
        import monitor_rules
        data = {'dates': monitor.market_sessions.preceding_sessions('2026-10-26', 80) + ['2026-10-26'],
                'volumes': [1000 * (1 + (i % 7 - 3) / 20) for i in range(81)]}
        distribution_api = monitor.completed_day_volume_distribution
        with patch.object(monitor, '_latest_completed_session_index', return_value=80) as lookup:
            distribution = distribution_api('AAPL', data)
            lookup.assert_called_once_with(data['dates'], None)
            self.assertEqual(set(distribution), {'m', 's'})
            self.assertEqual(distribution_api('^VIX', data), None)
            self.assertEqual(distribution_api('ES=F', data), None)
        old_context = SimpleNamespace(instrument_identity=lambda sym: sym,
                                      _latest_completed_session_index=lambda dates, asof: 80)
        self.assertEqual(indicators.IndicatorCalculator(old_context).completed_day_volume_distribution('AAPL', data),
                         distribution)
        # context 未新增分布方法仍可使用旧分析入口；保留旧 z mock 原值，不据它反推分布。
        context = SimpleNamespace(**{key: getattr(monitor, key) for key in (
            'cost_rule', 'calc_rsi', 'RSI_PERIODS', 'datetime', '_range52_from_history', 'S', 'sma', 'boll')},
            completed_day_volume_ratio=lambda sym, data: (2, '2026-10-22'),
            completed_day_volume_z=lambda sym, data: 100,
            intraday_volume_ratio=lambda sym, data: (None, ''))
        quote = dict(closes=[100] * 60, price=100, prev_close=100, highs=[], lows=[], dates=[])
        x = monitor_rules.RuleEngine(context)._analysis_context('AAPL', {}, quote, 'focus')
        self.assertEqual((x.vol_ratio_m, x.vol_ratio_s, x.vol_ratio_z), (None, None, 100))
        with patch.object(monitor, 'completed_day_volume_distribution', return_value=distribution) as measure:
            x = monitor._analysis_context('AAPL', {}, quote, 'focus')
            measure.assert_called_once_with('AAPL', quote)
        self.assertEqual((x.vol_ratio_m, x.vol_ratio_s), (distribution['m'], distribution['s']))

    def test_old_volume_context_uses_live_window_constants_and_session_module(self):
        import monitor_indicators as indicators
        dates = monitor.market_sessions.preceding_sessions('2026-10-26', 80) + ['2026-10-26']
        data = {'dates': dates, 'volumes': [1000 * (1 + (i % 7 - 3) / 20) for i in range(81)]}
        context = SimpleNamespace(instrument_identity=lambda sym: sym,
            _latest_completed_session_index=lambda days, asof: len(days) - 1,
            _volume_ratio_zscore=lambda volumes, index: 123)
        calculator = indicators.IndicatorCalculator(context)
        original = dict(vars(context))
        self.assertEqual(calculator.completed_day_volume_z('AAPL', data), 123)
        self.assertIsNotNone(calculator.completed_day_volume_distribution('AAPL', data))
        with patch.object(indicators, 'VOL_Z_WINDOW', 61):
            self.assertIsNone(calculator.completed_day_volume_z('AAPL', data))
            self.assertIsNone(calculator.completed_day_volume_distribution('AAPL', data))
        with patch.object(indicators, 'VOL_RATIO_BASE', 21):
            self.assertIsNone(calculator.completed_day_volume_distribution('AAPL', data))
        with patch.object(indicators.market_sessions, 'preceding_sessions', return_value=[]) as sessions:
            self.assertIsNone(calculator.completed_day_volume_distribution('AAPL', data))
            sessions.assert_called_once_with('2026-10-26', 80)
        context.market_sessions = SimpleNamespace(preceding_sessions=lambda day, count: dates[-count-1:-1])
        with patch.object(indicators.market_sessions, 'preceding_sessions', side_effect=AssertionError('不得替换 context')):
            self.assertEqual(calculator.completed_day_volume_z('AAPL', data), 123)
        del context.market_sessions
        self.assertEqual(vars(context), original)
        self.assertEqual(vars(calculator), {'_context': context})

    def test_new_fields_flow_through_analysis_detail_and_snapshot(self):
        data = dict(closes=[100] * 60, price=100, prev_close=100, highs=[], lows=[], dates=[])
        with patch.object(monitor, 'completed_day_volume_distribution', return_value={'m': 0.2, 's': 0.4}), \
                patch.object(monitor, 'completed_day_volume_z', return_value=123), \
                patch.object(monitor, 'global_dca', return_value=None):
            detail = monitor.analyze_symbol('AAPL', {}, data, 'focus')[2]
            snapshot = monitor._sanitize_snapshot({'items': [detail]})
        row = snapshot['items'][0]
        self.assertEqual((row['vol_ratio_m'], row['vol_ratio_s'], row['vol_ratio_z']), (0.2, 0.4, 123))
        self.assertEqual((detail['vol_ratio_m'], detail['vol_ratio_s']), (0.2, 0.4))


class ModuleBoundaryRegression(unittest.TestCase):
    def test_modules_import_without_runtime_and_join_build_inputs(self):
        import importlib
        import build_support
        modules = (("monitor_indicators", "IndicatorCalculator"),
                   ("monitor_rules", "RuleEngine"),
                   ("monitor_snapshot", "SnapshotBuilder"),
                   ("monitor_render", "PageRenderer"))
        for name, class_name in modules:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                context = SimpleNamespace()
                service = getattr(module, class_name)(context)
                self.assertIs(service._context, context)
                self.assertEqual(set(vars(service)), {"_context"})
                self.assertTrue(build_support.is_build_input(name + ".py"))
                tree = ast.parse((ROOT / (name + ".py")).read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertNotIn("monitor", [a.name for a in node.names])
                    elif isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, "monitor")

    def test_existing_bound_interfaces_read_later_callable_patches(self):
        resolve = monitor.resolve_target
        evaluate = monitor.evaluate_target
        with patch.object(monitor, "manual_target", return_value=(99, None)) as manual:
            self.assertEqual(resolve({}, None),
                             {"kind": "manual", "price": 99, "month": None, "failed": False})
            manual.assert_called_once_with({})
        target = {"kind": "manual", "price": 100, "month": None, "failed": False}
        with patch.object(monitor, "target_alerts", return_value=["patched"]) as alerts:
            self.assertEqual(evaluate(100, 99, target, "position", 3)["reached_detail"], "patched")
            alerts.assert_called_once()
        self.assertIsNone(resolve({}, None))

    def test_indicator_internal_lookup_keeps_old_patch_surface(self):
        ratio = monitor.completed_day_volume_ratio
        data = {"dates": monitor.market_sessions.preceding_sessions('2026-10-26', 20) + ['2026-10-26'],
                "volumes": [100] * 21}
        with patch.object(monitor, "_latest_completed_session_index", return_value=20) as index:
            self.assertEqual(ratio("AAPL", data), (1.0, "2026-10-26"))
            index.assert_called_once_with(data["dates"], None)

    def test_statistics_internal_lookup_keeps_old_patch_surface(self):
        members = {kind: {level: [] for level in ("red", "yellow", "purple")}
                   for kind, _ in monitor.SIGNAL_KINDS}
        members["chg"]["red"] = [{"symbol": "AAPL"}]
        count = monitor.signal_counts
        with patch.object(monitor, "signal_count_members", return_value=members) as collect:
            self.assertEqual(count([])["chg"]["red"], 1)
            collect.assert_called_once_with([])

    def test_rule_and_projection_read_same_rebound_constants(self):
        evaluate = monitor.evaluate_target
        project = monitor._apply_target
        target = {"kind": "manual", "price": 100, "month": None, "failed": False}
        with patch.object(monitor, "CONSENSUS_GROUPS", ()):
            resolved = evaluate(100, 99, target, "position", 3)
            self.assertFalse(resolved["hit"])
            projected = project({"symbol": "AAPL", "group": "position", "price": 100,
                                 "prev_close": 99, "target": resolved, "level": "green",
                                 "signal_marks": [], "signals": []})
            self.assertFalse(projected["target"]["hit"])
            self.assertFalse(projected["purple"])
        with patch.object(monitor, "TARGET_SPACE_PCT", 50), \
                patch.object(monitor, "TARGET_HIGH_SPACE_PCT", 100):
            self.assertEqual(evaluate(100, 99, dict(target, price=145), "technology", 3)["band"], "low")

    def test_services_read_rebound_settings_and_restore(self):
        near = monitor._boll_near_pct
        with patch.object(monitor, "S", dict(monitor.DEFAULT_SETTINGS,
                                            boll_near_band_pct=0, boll_near_pct=0.75)):
            self.assertEqual(near(SimpleNamespace(vol_sigma=2)), 0.75)
            with patch.dict(monitor.S, {"boll_near_pct": 1.25}):
                self.assertEqual(near(SimpleNamespace(vol_sigma=2)), 1.25)
            self.assertEqual(near(SimpleNamespace(vol_sigma=2)), 0.75)


class CostSeedRegression(unittest.TestCase):
    def test_cash_split_is_preserved_and_legacy_cash_is_not_split_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "holdings.json").write_text(json.dumps({"positions": {
                "PDD": {"cost": 79}, "APH": {"cost": 78.94}}}), encoding="utf-8")
            for extra, expected in ((23626, 23626), (None, 0), (-1, 0),
                                    (23675, 0), (True, 0), ("23626", 0)):
                seed = {"asof": "2026-10-22", "positions": {"PDD": 10, "APH": 30},
                        "cash": 23674.25, "cash_breakdown": {"money_market_etf": 23626},
                        "fixed_income": {"market": 16829.03, "cost": 16916.97}}
                if extra is not None:
                    seed["cash_extra"] = extra
                (base / "position-costs.seed.json").write_text(json.dumps(seed), encoding="utf-8")
                with self.subTest(extra=extra), patch.object(monitor, "BASE", tmp):
                    data = json.loads(monitor._cost_seed_json())
                    self.assertEqual(data["cash"], 23674.25)
                    self.assertEqual(data["cash_extra"], expected)
                    self.assertEqual(data["asof"], "2026-10-22")
                    self.assertEqual(data["items"], {"PDD": {"cost": 79, "qty": 10},
                                                     "APH": {"cost": 78.94, "qty": 30}})
                    self.assertEqual((data["fixed"], data["fixed_cost"]), (16829.03, 16916.97))
                    self.assertNotIn("SGOV", data["items"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
