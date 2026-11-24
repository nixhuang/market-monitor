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
from monitor_render import strip_css_comments, _amp_display_tone
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
                             "当日上穿／跌破、现价高于目标价（超过多少都算）或现价在目标±3% → 实心紫灯",
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
        prefix = 'table.stk tr[data-symbol]>td.tgs'
        base = declarations(prefix + ' .lvtag.tgt')
        for stable in ('display:grid', 'grid-template-rows:var(--row-height) var(--row-height)',
                       'border:0', 'background:transparent', 'color:var(--purple)'):
            self.assertIn(stable, base)
        # 终稿空间/高位框在TD画，失败框仍在标签画；低空间不得新增框。
        self.assertNotIn(':has(.tgt.low)::before', css)
        overlay = declarations(prefix + ' .lvtag.tgt.bad::after')
        self.assertIn('inset:-1px', overlay)
        self.assertIn('border-radius:inherit', overlay)
        from urllib.parse import unquote
        svg = unquote(page_assets.TARGET_DASH_BORDER)
        self.assertIn('stroke="#8b93a1"', svg)
        self.assertIn('stroke-width="1" stroke-dasharray="3 5"', svg)
        self.assertIn('url("' + page_assets.TARGET_DASH_BORDER + '")', overlay)
        for state in ('space', 'high', 'hit'):
            frame = declarations(prefix + ':has(.tgt.' + state + ')::before')
            for geometry in ('left:-1px', 'right:0', 'top:var(--tgt-frame-top)', 'bottom:var(--tgt-frame-bottom)'):
                self.assertIn(geometry, frame)
            # 2026-11-23：紫灯区右侧的灰竖线整条删除，紫框态也就不再需要「隐藏右线」的规则
            self.assertNotIn(prefix + ':has(.tgt.' + state + ')::after', css)
        self.assertNotIn(prefix + '::after{content:""', css)
        self.assertIn('border:1px solid var(--purple)', declarations(prefix + ':has(.tgt.space)::before'))
        self.assertIn('border-color:var(--target-high)', declarations(prefix + ':has(.tgt.high)::before'))
        hit = declarations(prefix + ':has(.tgt.hit)::before')
        self.assertIn('background:var(--tgt-solid)', hit)
        self.assertIn('border-radius:0', hit)
        self.assertNotIn('border:', hit)
        self.assertIn('--tgt-solid:color-mix(in srgb,var(--purple) 32%,transparent)', css)
        self.assertIn('color:var(--white)', declarations(prefix + ' .lvtag.tgt.hit'))
        self.assertIn('color:var(--dim)', declarations(prefix + ' .lvtag.tgt.bad'))
        self.assertIn('border-top:9px solid var(--range-marker-border)', declarations('.range52-marker'))
        self.assertIn('border-top:7px solid var(--range-marker-fill)', declarations('.range52-marker::before'))
        self.assertIn('background:var(--lamp-yellow)', declarations('.range52-cost'))
        for lamp in ('red', 'yellow', 'green'):
            self.assertIn(f'table.stk tr.{lamp}[data-symbol]{{--lamp-line:var(--{lamp})}}', css)
        self.assertIn('width:4px', declarations('table.stk tr[data-symbol]::before'))
        self.assertIn('#signalCounts .signal-name-lamp.tone-target-high{--name-lamp:var(--target-high)}', css)
        for alias in ('tone-target-high', 'target-space-high'):
            self.assertIn(f'#signalCounts .signal-member-block h5.{alias}', css)
        self.assertNotRegex(css, r'(?:tone-target-high|target-space-high)[^{]*\{[^}]*var\(--red\)')

    def test_earnings_text_is_never_bold_and_uses_description_red(self):
        css = page_assets.PAGE_CSS
        # 财报不是涨跌方向：红字跟个股描述红 #ff8580 同色，不能用上涨红 var(--red)。
        self.assertIn('#signalCounts .earnings-week-meta.is-red{color:var(--lamp-red);font-weight:400;--earn-bj-color:var(--lamp-red)}', css)
        self.assertIn('#signalCounts .earnings-week-meta.is-yellow{color:var(--lamp-yellow);font-weight:400;--earn-bj-color:var(--lamp-yellow)}', css)
        self.assertIn('.earn.urgent{color:var(--lamp-red);--earn-bj-color:var(--lamp-red)}', css)
        self.assertIn('.earn.soon{color:var(--lamp-yellow);--earn-bj-color:var(--lamp-yellow)}', css)
        # 所有位置的财报文字都不加粗（名单 meta、主表财报单元、多选组合条件）。
        for selector, body in re.findall(r'([^{}]*(?:earnings-week-meta|\.earn)[^{}]*)\{([^{}]*)\}', css):
            if 'summary' in selector:  # 「未来15天财报」这类区块标题照旧加粗
                continue
            with self.subTest(selector=selector.strip()):
                self.assertNotIn('font-weight:600', body)
        # 紫灯条件清单的竖条跟个股自身灯级，未知灯级留灰。
        self.assertIn('#signalCounts .signal-name-lamp.unknown{--name-lamp:var(--dim)}', css)

    def test_rule_body_font_matches_settings_macro_and_preserves_warning_table_geometry(self):
        source = (ROOT / 'edit.html').read_text(encoding='utf-8')
        # 综合细则注入时移除macro-help，正文实际继承设置页body的15px，并非首页13px。
        self.assertRegex(source, r'body\{[^}]*font:15px/1\.55')
        self.assertIn("if(mr) mr.className='card fold';", source)
        css = page_assets.RULES_CSS
        for selector in ('.card .rules-body', '.card .rules-body p', '.card .rules-body h4',
                         '.card .rules-body h5', '.card .rules-body .rule-table',
                         '.card .rules-body .rule-table th,.card .rules-body .rule-table td',
                         '.card .rules-body .rule-table th.cat-col'):
            body = re.search(re.escape(selector) + r'\{([^}]*)\}', css)[1]
            self.assertIn('font-size:' + ('15px' if selector == '.card .rules-body' else 'inherit'), body)
        cells = re.search(r'\.rule-table td\{([^}]*)\}', css)[1]
        self.assertIn('padding:6px 7px', cells)
        self.assertIn('line-height:19.8px', cells)
        self.assertIn('line-height:13.2px', re.search(re.escape('.card .rules-body .rule-table th.cat-col') + r'\{([^}]*)\}', css)[1])
        self.assertIn('.card .rules-body .rule-table th.cat-col{line-height:12.1px}', css)
        for stable in ('width:calc(100% + 32px)', 'margin-left:-16px;margin-right:-16px',
                       'width:calc(100% + 40px)', 'margin-left:-20px;margin-right:-20px',
                       'col.cat-col{width:18px}', 'col.col-cond{width:40%}', 'col.col-cond{width:auto}', '#rules-lamp-table col[span]{width:86px}', '#rules-lamp-table td.cell-gray-fold{white-space:nowrap}',
                       '.rule-table td{padding:6px 4px}'):
            self.assertIn(stable, css)

    def test_earnings_date_palette_and_countdown_colors_share_all_display_paths(self):
        css = page_assets.PAGE_CSS
        self.assertIn('--head-blue-on:#8fb8f0', page_assets.PAGE_PALETTE)
        self.assertIn('--dim:#8b93a1', page_assets.PAGE_PALETTE)
        self.assertIn('#signalCounts .earnings-week-meta{display:block;margin:1px 0 0 9px;color:var(--head-blue-on)', css)
        self.assertIn('#signalCounts .earnings-week-bj{color:var(--earn-bj-color,var(--dim))', css)
        selectors = '.earn>.cd,#signalCounts .earnings-week-meta .cd,#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd'
        declarations = re.search(re.escape(selectors) + r'\{([^}]*)\}', css)[1]
        # 23:29 用户「蓝色一直是美东时间的颜色」：颜色按口径分，不按「是不是倒计时」分。
        # 写北京的倒计时（含已出）跟北京说明同灰，只有写「（美N天）」的 .cd.tbd 用美东蓝；
        # 红黄由 --earn-bj-color 统一覆盖，避免写死颜色后倒计时不跟预警变色。
        self.assertIn('color:var(--earn-bj-color,var(--dim))', declarations)
        self.assertNotIn('color:inherit', declarations)
        self.assertNotIn('color:var(--white)', declarations)
        tbd_selectors = ('.earn>.cd.tbd,#signalCounts .earnings-week-meta .cd.tbd,'
                         '#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd.tbd')
        tbd_declarations = re.search(re.escape(tbd_selectors) + r'\{([^}]*)\}', css)[1]
        self.assertIn('color:var(--earn-bj-color,var(--head-blue-on))', tbd_declarations)
        self.assertNotIn('.cd.tbd{color:var(--head-blue-on)}', css)
        self.assertNotIn('.earnings-week-bj.is-countdown{color:var(--white)}', css)
        self.assertIn('.earn .earn-sep,.earn .earn-bj{color:var(--earn-bj-color,var(--dim))}', css)
        self.assertIn('已知时段按北京落点计算', page_assets.GLOSSARY_BODY)
        self.assertIn('财报文字按预警规则变红／黄；无预警时美东时间用蓝色、北京时间（含写北京的倒计时）用灰色', page_assets.GLOSSARY_BODY)
        self.assertIn('不随打开页面日期漂移', page_assets.GLOSSARY_BODY)
        self.assertNotIn('并用白字', page_assets.GLOSSARY_BODY)
        self.assertNotIn('并用蓝字', page_assets.GLOSSARY_BODY)

    def test_earnings_relative_nodes_and_dynamic_filter_metadata_use_frozen_beijing_day(self):
        """2026-11-13：北京具体时段及所有已出都只在倒计时写一次，
        已出写「（北京昨天11月9日已出）」这类相对日＋月日，后面不再重复北京说明；
        tbd 蓝字只剩「时段待定且未发布」。"""
        today = '2026-11-10'
        cases = (
            ('post', '2026-11-20', '（北京11天 11/21 凌晨）', '', 'cd', False, False),
            ('pre',  '2026-11-12', '（北京2天 11/12 晚间）', '', 'cd', False, False),
            (None,   '2026-11-12', '（美2天）', '北京 待定', 'cd tbd', True, False),
            ('post', '2026-11-10', '（北京 明天 凌晨）', '', 'cd', False, True),
            ('post', '2026-11-09', '（北京 今天 凌晨）', '', 'cd', False, True),
            ('pre',  '2026-11-09', '（北京昨天11/09晚间已出）', '', 'cd', False, True),   # 2026-11-20：取到过时段的已出，倒计时里也写时段
            (None,   '2026-11-09', '（北京昨天11/09已出）', '', 'cd', False, True),   # 2026-11-20：没取到时段的已出写 MM/DD
            (None,   '2026-11-10', '（北京今天11/10已出）', '', 'cd', False, True),
        )
        for timing, date, countdown, bj, mark_cls, tbd, relative in cases:
            with self.subTest(timing=timing, date=date):
                event = {'status': 'ok', 'date': date, 'timing': timing}
                entry = {'symbol': 'AAA', 'date': date, 'timing': timing, 'group': 'focus', 'level': 'green'}
                snapshot = {'finished_at_bj': today + 'T18:00:00+08:00',
                            'earnings_week': {'asof': today, 'members': [entry]}}
                main = monitor._earnings_html({'earnings': event}, today)
                panel = monitor._earnings_week_panel(snapshot)
                for markup in (main, panel):
                    self.assertIn(f'<span class="{mark_cls}">{countdown}</span>', markup)
                    has_bj = 'class="earn-bj"' in markup or 'class="earnings-week-bj' in markup
                    self.assertEqual(has_bj, bool(bj))
                self.assertEqual('class="earnings-week-bj is-countdown"' in panel, relative and bool(bj))
                rendered = monitor._signal_counts_card(snapshot)
                data = json.loads(html.unescape(re.search(r'data-filter="([^"]*)"', rendered)[1]))
                part = data['earnings_week']['members'][0]['d'][0]
                label = {'post': '盘后', 'pre': '盘前'}.get(timing, '时段待定')
                # 22:35 已出且源没给时段时，倒计时已写「已出」，筛选结果也不再写「时段待定」。
                if label == '时段待定' and monitor._earnings_display_gap(event, today) <= 0:
                    label = '未获取'
                self.assertEqual(part['t'], f"财报 美 {date[5:].replace('-', '/')} {label}".rstrip())
                self.assertEqual(part['e'], {'c': countdown, 'b': bj, 'tbd': tbd,
                                             'relative': relative and bool(bj)})
        js = page_assets.PAGE_INTERACTION_JS
        pure = js[js.index('function intersectSignals('):js.index('// 多选条件的四种方向符号')]
        script = pure + '\n' + '''
const e={c:'（今）',b:'北京 今出',tbd:false,relative:true};
const member={id:'AAA',s:'AAA',d:[{l:'red',t:'财报 2026-11-09 盘后',e:e}]};
const data={earnings_week:{ok:true,members:[member]},chg:{ok:true,members:[{id:'AAA',d:[{l:'yellow',t:'涨跌幅'}]}]}};
const result=intersectSignals(data,['chg','earnings_week']);
if (JSON.stringify(result.members[0].details[1].e)!==JSON.stringify(e)) throw new Error('冻结时间显示元数据丢失');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cd.textContent = part.e.c;', js)
        # 2026-11-20：指标名「财报」永远在最前，倒计时在其后（与「涨跌幅」等指标名在前一致）
        self.assertLess(js.index('rest.indexOf("财报") === 0'), js.index('cd.textContent = part.e.c;'))
        self.assertIn('bj.textContent = "·" + part.e.b;', js)
        self.assertNotIn('new Date(', js[js.index('function renderSignalFilter('):js.index('document.addEventListener("click"', js.index('function renderSignalFilter('))])

    def test_glossary_headings_centered_and_metric_names_blue(self):
        css = page_assets.PAGE_CSS
        # 名词解释与抓取规则：两个分区标题页面居中；指标名（h5）统一站内强调蓝。
        self.assertIn('#glossaryRulesBody h4{text-align:center}', css)
        self.assertIn('#glossaryRulesBody h5{color:var(--head-blue-on)}', css)
        # 只在这两块生效：报灯规则的小标题仍是白字。
        self.assertIn('.card .rules-body h5{margin:12px 0 4px;color:var(--text);font-size:inherit;font-weight:600}', css)
        # 蓝色规则必须写在通用 h5 规则之后（同文件内顺序即层叠顺序），否则会被白字覆盖。
        self.assertLess(css.index('.card .rules-body h5{'), css.index('#glossaryRulesBody h5{'))
        # 成本隔离样式已格式化；设置页仍从唯一规则模板派生。
        cost_css = (ROOT / 'cost_panel.css').read_text(encoding='utf-8')
        self.assertIn('#glossaryRulesBody h4 { text-align: center; }', cost_css)
        self.assertIn('.rules-body h5 { margin: 8px 0 3px; font-size: 12px; color: var(--head-blue-on); font-weight: 600; }', cost_css)
        source = (ROOT / 'edit.html').read_text(encoding='utf-8')
        self.assertIn('#glossaryRulesBody h4{text-align:center}', source)
        self.assertIn('#glossaryRulesBody h5{color:var(--head-blue-on)}', source)
        # 两块分区标题就是「名词解释与计算」「抓取规则」，指标名全部是 h5。
        glossary = page_assets.GLOSSARY_BODY
        self.assertEqual(re.findall(r'<h4>(.*?)</h4>', glossary), ['名词解释与计算', '抓取规则'])
        self.assertEqual(len(re.findall(r'<h5 id="(?:glossary|fetch)-', glossary)), 14)
        self.assertNotIn('可点红灯与已读', glossary)

    def test_purple_ring_and_capsule_share_one_variable(self):
        """分组标题紫灯圈与主表紫色胶囊必须同一个紫：都走 --purple，不允许任何一处写死十六进制紫。"""
        css = page_assets.PAGE_CSS
        # 变量是唯一紫源；旧暗紫 #a371f7 与任何 rgb 形式的写死紫都不许再出现。
        self.assertIn('--purple:#b084ff', css)
        self.assertEqual(css.count('#b084ff'), 1)
        # 2026-11-19 Demo 的框紫（站名/外框/运行按钮）是另一个变量 --frame-purple：
        # 十六进制与 rgb 三元组各只许在 :root 出现一次，别处一律走变量。
        self.assertEqual(css.count('#a371f7'), 1)
        self.assertEqual(css.count('163,113,247'), 1)
        for dead in ('#c4a3ff', '196,163,255', '176,132,255'):
            self.assertNotIn(dead, css)
        # 灯圈（计数徽章）与胶囊（共识价标签）本体、描边圈同源。
        self.assertIn('.stat-badge.purple{color:var(--purple)}', css)
        self.assertIn('.stat-dot.purple{background:var(--purple)}', css)
        target_rule = re.search(r'td\.tgs \.lvtag\.tgt\{([^}]*)\}', css)[1]
        self.assertIn('color:var(--purple)', target_rule)
        frame = re.search(r'td\.tgs:has\(\.tgt\.space\)::before\{([^}]*)\}', css)[1]
        self.assertIn('border:1px solid var(--purple)', frame)
        # 紫红（空间≥90%）与实心紫（达到）是另外两档，允许独立取值；其余紫规则只能是 var(--purple)。
        allowed = {'var(--purple)', 'var(--target-high)', 'var(--tgt-pos)', 'var(--tgt-neg)'}
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
        self.assertIn('.rules-body .lamp-purple { color: var(--purple); font-weight: 600; }', cost_css)
        self.assertIn('.card .rules-body .lamp-purple{color:var(--purple);font-weight:600}', css)

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
        self.assertEqual(block.group(1).strip(), (page_assets.PAGE_PALETTE + page_assets.RULES_CSS).strip())
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
        # 2026-11-19 Demo：外框改框紫、1px（原 #6686c6 / 1.5px 作废）
        self.assertIn('--signal-border:var(--frame-purple);', css)
        self.assertIn('--frame-w:1px;', css)
        self.assertEqual(css.count('--signal-border:var(--frame-purple)'), 1)
        # 统计面板：只剩外框，卡底 / 名单面板底 / 紫灯财报行底全部去掉，左侧蓝条取消
        self.assertIn('border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}', css)
        self.assertIn('#signalCounts .signal-members-panel{padding:10px 12px;border-top:1px solid var(--list-border);text-align:left}', css)
        self.assertIn('--list-border:#465e87', css)
        self.assertNotIn('background:#121a27', css)
        self.assertIn('#signalCounts .signal-extra-row{--chip-w:84px;--xr:12px;position:relative;display:block;padding:0;border-top:1px solid var(--line)}', css)
        # 两档均74px；三条统计行统一50px内容+1px分隔=51px（与表格各排一致），展开不改变标题高度。
        mobile = css.split('@media(max-width:600px){', 1)[1].split('@media(min-width:601px){', 1)[0]
        self.assertIn('#signalCounts .signal-extra-row{--chip-w:84px;--xr:8px;padding:0}', mobile)
        for kind in ('text-only', 'earnings-week'):
            self.assertIn(f'#signalCounts .signal-extra-row>.{kind}>summary{{position:relative;box-sizing:border-box;min-height:50px;', css)
        self.assertNotIn('background:#182130', css)
        # 展开的分组卡：整卡套外框，标题不再铺底色、不再画左条
        for selector in ('.group-card[open]:where(:not(#signalCounts,#costManage))', '.macro-card[open]'):
            self.assertIn(selector + '{border:var(--frame-w) solid var(--signal-border)}', css)
        # 卡中卡「长持仓」单独再细一档，不跟着整卡 1.5px
        self.assertIn('.zone-card[open]{border:1px solid var(--signal-border)}', css)
        # 子框保留内部4px；终稿收起条向内留4px，不铺到9px圆角弧上。
        self.assertIn('.zone-card{margin:10px -2px 12px;padding-left:4px;padding-right:4px;'
                      'border:1px solid var(--line);border-radius:9px;background:var(--zone-bg)}', css)
        self.assertIn('.zone-card .group-bottom{margin:0 4px 0}', css)
        # 数字胶囊：个位压在胶囊中线（右边 17px 内边距给三角留位），两位数时十位向左长，不折行；
        # 胶囊仍是 44px。三角维持线上版：16px 的 ▾/▴ 字形，绝对定位贴到胶囊右侧（距右边框 2px）。
        self.assertIn('#signalCounts .signal-count-toggle{box-sizing:border-box;height:38px;width:44px;'
                      'max-width:100%;position:relative;padding:0 17px 0 4px;', css)
        self.assertIn('white-space:nowrap;display:inline-flex;align-items:center;justify-content:flex-end;'
                      'touch-action:manipulation}', css)
        self.assertIn('#signalCounts .signal-count-toggle::after{content:"\u25be";font-size:16px;'
                      'line-height:1;position:absolute;right:2px;top:50%;transform:translateY(-50%)}', css)
        self.assertIn('#signalCounts .signal-count-toggle[aria-expanded="true"]::after{content:"\u25b4"}', css)
        # 市场风险表：固定列宽，不再让 WTI 参考行那一大段说明把状态列撑到 138.6px
        # （短文案「平静」后面空一大截），也不再把指标列挤到 65.8px（「垃圾债利差」逐字折三行）。
        # 四列不换行所需（含左右各 10px 内边距）实测 126/159/90/114：
        # 宽屏 26/33/18/23 → 184.6/234.3/127.8/163.3；≤600px 27/28/21/24（11-23 调整）。
        # 指标列仍左对齐，其余三列居中；原油「依据与来源」整块删除，不再留 .reference-detail。
        scope = ':is(#macroCard, .macro-scope)'
        self.assertIn(scope + ' table{table-layout:fixed}', css)
        for col, width in (('first-child',26), ('nth-child(2)',33), ('nth-child(3)',18), ('nth-child(4)',23)):
            self.assertIn(scope + f' td:{col}{{width:{width}%', css)
        # 11-23 手机档：名称收窄，当前列与变化列左移靠近名称列，变化列与评语列一起放宽
        for col, width in (('first-child',27), ('nth-child(2)',28), ('nth-child(3)',21), ('nth-child(4)',24)):
            self.assertIn(scope + f' td:{col}{{width:{width}%}}', mobile)
        for col, width in ((2,33), (3,18), (4,23)):
            self.assertIn(scope + f' td:nth-child({col}){{width:{width}%;text-align:center}}', css)
        self.assertIn(scope + ' td{font-size:var(--macro-body);overflow-wrap:anywhere;', css)
        self.assertNotIn('.reference-detail{', css)
        # 整页左右各加1px；宽屏同步收窄wrap保证实际边距也各增加1px
        # 终稿手机左右各5px，与桌面不共用padding。
        self.assertIn('body{padding-left:5px;padding-right:5px}', mobile)
        self.assertIn('padding:env(safe-area-inset-top) 17px 40px 15px}', css)
        self.assertIn('.wrap{max-width:718px;margin:0 auto}', css)
        # 折叠整卡：普通分组63；当日与风险为53（summary 47 + 内边距2+2 + 边框1+1）
        self.assertIn('.group-card:not([open]){padding-bottom:2px;min-height:63px}', css)
        self.assertIn('.macro-card:not([open]){padding-bottom:2px;min-height:53px}', css)
        self.assertIn('.day-pnl-card:not([open]){padding-bottom:2px;min-height:53px}', css)
        self.assertIn('.group-card>summary.grp{line-height:24px;box-sizing:border-box;min-height:56px;padding-top:0;padding-bottom:0;font-size:18px}', mobile)
        for card in ('macro-card', 'day-pnl-card'):
            self.assertIn(f'.{card}>summary.grp{{line-height:24px;box-sizing:border-box;min-height:47px;padding-top:0;padding-bottom:0;font-size:18px}}', mobile)
        for card in ('group-card','macro-card'):
            self.assertIn(f'.{card}>summary .group-stats{{line-height:24px;font-size:16px;gap:6px}}', mobile)
        # 长/短持仓折叠框 50（summary 48 + 边框2）；桌面同值
        self.assertIn('.zone-card>summary{line-height:22.5px;box-sizing:border-box;min-height:48px;padding-top:0;padding-bottom:0}', mobile)
        self.assertIn('.zone-card>summary{box-sizing:border-box;height:48px;padding-top:0;padding-bottom:0}', css)
        for card in ('macro-card', 'day-pnl-card'):
            self.assertIn(f'.{card}>summary.grp{{box-sizing:border-box;height:47px;padding-top:0;padding-bottom:0}}', css)
        self.assertIn('.stat-badge{font-size:17px;line-height:16px}', mobile)
        self.assertNotIn('linear-gradient(90deg,rgba(143,184,240,', css)
        self.assertNotIn('inset 3px 0 0 #8fb8f0', css)
        # 「信号」列所有胶囊同宽且文字居中
        self.assertIn('#signalCounts td .signal-filter-toggle{width:84px;padding:0 4px}', css)
        # 11-10真实五列编辑器：表头与行共用网格，不回退旧四列；不再复制隐藏标题。
        editor = re.search(r'#costManage\.cost-manage\s*\{([^}]*)\}', cost)[1]
        for decl in ('display: none', 'border: 1.5px solid var(--signal-border)', 'box-shadow: none', 'background: var(--card)'):
            self.assertIn(decl, editor)
        self.assertIn('#costManage.cost-manage.cost-edit-open { display: block; }', cost)
        self.assertIn('#costManage > summary, #costManage > .group-bottom { display: none; }', cost)
        grids = re.findall(r'#costManage \.ep-editor-head, #costManage \.ep-editor-row\s*\{([^}]*)\}', cost)
        self.assertEqual(len(grids), 2)
        self.assertIn('grid-template-columns: 54px 58px 58px 92px 88px;', grids[0])
        self.assertIn('grid-template-columns: 58px 58px 58px 112px 90px;', grids[1])
        self.assertNotIn('inset 3px 0 0 var(--cost-accent-bg)', cost)
        # quiet-list归页面资源统一管理，持仓summary隐藏但静态open另由分组用例保证。
        self.assertIn('.quiet-list[open]>summary{background:none;box-shadow:none;color:var(--head-blue)}', css)
        self.assertNotIn('linear-gradient(90deg,rgba(143,184,240,.18)', cost)

    def test_main_row_signal_area_is_two_columns_on_both_breakpoints(self):
        """11-12改版：主行三区双排（价格区吃下原财报栏），信号静态双列；退休单列/搬家/ACK补丁不保留。"""
        css = page_assets.PAGE_CSS
        self.assertIn('grid-template-areas:"sym px tgs" "cost cost cost" "sig sig sig"', css)
        self.assertIn('grid-template-columns:var(--sym-w) minmax(0,1fr) var(--tgs-w)', css)
        # 2026-11-13「电脑上 7 个中文字宽、手机上 6 个」：桌面 100px（内距 8px，可见 84px＝7 字），
        # 手机 98px（左 6 / 右 2px 内距，可见 90px＝6 字）。灰线由 --vl-x 按 sym 右边界算，随列宽同步。
        # 19:35 恢复左内距 6px（不贴灯条）；20:51 左移灰线 4px 转给紫灯区，note 可见宽仍 90px。
        self.assertIn('--sym-w:100px', css)
        self.assertIn(':root{--status-font-size:14.5px;--sym-w:98px;--tgs-w:108px;--px-gap:5px;--px-col-gap:6px;'
                      '--lamp-top:8px;--lamp-h:36px;--row-pad-top:5px;--tgs-font:16px;'
                      '--tgs-pad-l:0px;--tgs-pad-r:0px;--pos-num-fs:16px}', css)
        self.assertIn('table.stk tr[data-symbol]>td.sym{font-size:17px;padding-left:6px;padding-right:2px}', css)
        self.assertIn('--tgs-w:104px', css)
        self.assertIn('--tgs-w:108px', css)
        self.assertIn('width:100%;max-width:100%;margin:0;padding:0;border:0;background:none', css)
        self.assertIn('white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis', css)
        self.assertEqual(css.count('.sig-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));'), 1)
        self.assertIn('column-gap:var(--sig-col-gap);align-items:start;min-width:0', css)
        self.assertIn('.sig-col{display:flex;flex-direction:column;gap:var(--sig-row-gap);min-width:0}', css)
        self.assertIn('table.stk tr[data-symbol]>td.sig{grid-area:sig;display:block;', css)
        # 描述折行终稿：贪心排满第一行，不做两行等长平衡、不强制单行、按变量借用空白。
        self.assertIn('.sig-cols>.sig-col .signal-item[data-signal-flow="auto"]{width:calc(100% + var(--sig-borrow));max-width:none;align-self:flex-start}', css)
        self.assertIn('--sig-borrow:6px', css)
        self.assertIn('table.stk tr[data-symbol]>td.sig{--sig-borrow:14px}', css)
        self.assertNotIn('text-wrap:balance', css)
        self.assertNotIn('text-wrap:nowrap', css)
        self.assertNotIn('data-signal-kind="chg"]{white-space:nowrap', css)
        for level in ('red', 'yellow', 'green'):
            self.assertIn(f'table.stk tr.{level}[data-symbol]{{--lamp-line:var(--{level})}}', css)
        # 2026-11-13：灯条与价格区两侧（共三条）灰色分隔线共用同一组上下几何，长度一致。
        # 2026-11-23 用户：紫灯区右侧不要灰竖线 → 只剩代码|价格、价格|紫灯两条，td.tgs 不再画右线。
        self.assertIn('--lamp-top:8.5px', css)
        self.assertIn('--lamp-h:31.5px', css)
        self.assertIn('top:var(--lamp-top);width:4px;height:var(--lamp-h);background:var(--lamp-line)', css)
        self.assertIn('--lamp-top:8px;--lamp-h:36px', css)
        self.assertNotIn('table.stk tr[data-symbol]::before{top:7px;height:36px}', css)
        self.assertEqual(css.count('top:var(--lamp-top);height:var(--lamp-h);width:1px;'
                                   'background:var(--section-line)'), 2)
        self.assertIn('td.sym::after{content:"";position:absolute;left:var(--vl-x);top:var(--lamp-top);'
                      'height:var(--lamp-h);width:1px;background:var(--section-line)', css)
        self.assertIn('td.px::after{content:"";position:absolute;right:0;top:var(--lamp-top);'
                      'height:var(--lamp-h);width:1px;background:var(--section-line)', css)
        self.assertNotIn('td.tgs::after', css)
        self.assertNotIn('td.tgs:has(.tgt.space)::after', css)
        self.assertNotIn('var(--vl-top)', css)
        self.assertNotIn('var(--vl-h)', css)
        self.assertIn('font-size:15px;line-height:21.75px', css)
        self.assertIn('width:3px;height:10px;', css)
        for dead in ('cost-warn', 'ack-btn', 'pos-jump', '.lamp-frame', '.sig-lead', 'r58patch'):
            self.assertNotIn(dead, css)
        # 功能事实分左右列，但不能跨列借整股最高灯色，也不能改变输入。
        row = {'symbol':'COLS', 'group':'position', 'price':100, 'chg':0, 'level':'red',
               'signals':['触及布林下轨▼', 'RSI 3条 ↓超卖', '量比 2.8x'],
               'signal_marks':[('boll_rsi','red'), ('vol_ratio','yellow')], 'trigger':97, 'reduce':103}
        before = json.dumps(row, sort_keys=True)
        out = monitor._signals_html(row)
        self.assertEqual(out.count('class="sig-col"'), 2)
        left, right = out.split('<div class="sig-col">')[1:]
        self.assertIn('data-side="boll"', left)
        self.assertIn('data-side="rsi"', right)
        self.assertIn('class="lvtag add signal-item"', left)
        self.assertIn('class="lvtag cut signal-item"', right)
        self.assertEqual(out.count('data-signal-kind="boll_rsi" data-signal-level="red"'), 2)
        # 量比不再写进个股描述：投影层过滤，数据（signals/marks）与统计报灯一点不变。
        self.assertEqual(out.count('data-signal-kind="vol_ratio"'), 0)
        self.assertNotIn('量比 2.8x', out)
        self.assertEqual(json.dumps(row, sort_keys=True), before)
        js = page_assets.PAGE_INTERACTION_JS
        self.assertIn('function balanceDescriptions(row)', js)
        self.assertIn('data-signal-flow="auto"', js)
        self.assertIn("n.matches('[data-signal-flow=\"auto\"],.lvtag.add,.lvtag.cut')", js)

    def test_signal_allocation_uses_final_demo_measurement_without_visible_narrowing(self):
        """冻结终稿的分列测量口径；只在隐藏副本测量，固定侧/加减仓价不参与搬移。"""
        js = page_assets.PAGE_INTERACTION_JS.split('function balanceDescriptions(row)', 1)[1].split('function verticalLines(row)', 1)[0]
        self.assertIn('Number(a.dataset.signalOrder) - Number(b.dataset.signalOrder)', js)
        self.assertIn('const probe = cols.cloneNode(false)', js)
        self.assertIn('visibility:hidden;pointer-events:none', js)
        self.assertIn('font-size:13.5px;line-height:', js)
        self.assertIn("matchMedia('(max-width:600px)').matches ? '1.45' : '1.5'", js)
        self.assertIn('(cols.getBoundingClientRect().width - gap) / 2', js)
        # 隐藏副本按真实描述宽度（列宽 + 借用空白）测量，否则两行会被估成一行。
        self.assertIn("getPropertyValue('--sig-borrow')", js)
        self.assertIn('+ borrow', js)
        self.assertIn('measured[0].getBoundingClientRect().height <= measured[1].getBoundingClientRect().height', js)
        self.assertIn('measured[side].appendChild(n.cloneNode(true))', js)
        self.assertIn('finally { probe.remove(); }', js)
        self.assertIn('planned[index].concat(caps[index])', js)
        self.assertNotIn('cols.style', js)
        self.assertNotIn('row.dataset.symbol', js)
        self.assertNotIn('innerHTML', js)

    def test_combo_only_adjusts_ink_vertical_offset_preserving_shape_and_gap_center(self):
        css = page_assets.PAGE_CSS
        # 加号改为细线绘制：笔画1.1px（原字形放大后约1.7px），仍压在中线上，尺寸/墨迹中心沿用原值。
        self.assertIn('--combo-size:10.5px;--combo-cy:10.6px;--combo-stroke:1.1px;', css)
        self.assertIn('.sig-cols[data-combo="1"]{--combo-size:11.3px;--combo-cy:9.7px}', css)
        self.assertEqual(css.count('--combo-cy:'), 2)
        self.assertIn('content:"";position:absolute;left:50%;top:calc(var(--combo-cy) - var(--combo-size) / 2)', css)
        self.assertIn('center/100% var(--combo-stroke) no-repeat', css)
        self.assertNotIn('content:"+"', css)
        self.assertNotIn('--combo-scale', css)
        self.assertIn('.sig-cols>.sig-col:last-child{padding-left:4px}', css)
        self.assertIn('--sig-col-gap:16px;--sig-borrow:6px', css)
        self.assertIn('gap:11px;height:26px;padding-bottom:13px', css)

    def test_main_row_price_and_target_use_the_same_two_rows(self):
        css = page_assets.PAGE_CSS
        prefix = 'table.stk tr[data-symbol]>'
        # 价格区两行与左侧代码两行同高、同顶内距，现价与代码第一行中心对齐。
        self.assertIn('--px-row-h:21px', css)
        self.assertIn(prefix + 'td.px{grid-area:px;position:relative;display:block;min-width:0;padding:var(--row-pad-top) 6px 0;', css)
        self.assertIn(prefix + 'td.px .px-main{display:grid;grid-template-columns:max-content minmax(0,1fr);grid-template-rows:var(--px-row-h) var(--px-row-h);column-gap:var(--px-col-gap)', css)
        # 左块上排 现价+涨跌额／下排 涨跌幅；振／量恢复上下两排、靠右贴住紫灯框左侧。
        self.assertIn(prefix + 'td.px .px-row{display:flex;align-items:center;gap:var(--px-gap)', css)
        self.assertIn('.px-price,.px-chg,.px-amt,.px-v,.px-k{line-height:var(--px-row-h)}', css)
        self.assertIn(prefix + 'td.px .px-row-main{grid-area:1/1/2/2;gap:0}', css)
        self.assertIn(prefix + 'td.px .px-row-sub{grid-area:2/1/3/2}', css)
        self.assertIn(prefix + 'td.px .px-side{grid-area:1/2/3/3;display:grid;'
                      'grid-template-rows:var(--px-row-h) var(--px-row-h);'
                      'align-items:center;justify-items:end', css)
        self.assertNotIn('--px-side-gap', css)
        self.assertIn('.px-price{font-size:17.5px}', css)
        self.assertIn('.px-chg,.px-amt,.px-v{font-size:16.5px}', css)
        self.assertIn('.px-k{font-size:14px', css)
        # 开／高／低已从价格区删除，配套灰字规则也必须一起删，不留死选择器。
        for dead in ('px-open', 'px-high', 'px-low'):
            self.assertNotIn(dead, css)
        self.assertNotIn('grid-template-columns:repeat(3,minmax(0,1fr))', css)
        self.assertIn('.px-v.vol-red{color:var(--lamp-red)}', css)
        self.assertIn('.px-v.vol-yellow{color:var(--lamp-yellow)}', css)
        # 2026-11-13 19:35：四列成组不变，分隔符独立成 .target-sep 真元素且左右等距（· 在两数中点）；
        # 20:51「· N月」整块定值 13px 灰（--tgs-month-font + var(--dim)），不再随 --tgs-font 联动。
        # 分隔符 .target-sep 的 color:inherit 保持不动，跟着月份一起成为次要灰标注。
        self.assertNotIn('--tgs-month-slot', css)       # 三列两侧锁宽已随数字圈一起退休，不留死变量
        self.assertIn('--tgs-month-gap:2px', css)
        self.assertIn('--tgs-month-font:13px', css)
        self.assertIn('margin-left:var(--tgs-month-gap)', css)
        self.assertIn('margin-right:var(--tgs-month-gap)', css)
        self.assertNotIn('font-size:calc(var(--tgs-font) - 1px)', css)  # 旧联动字号已退休
        self.assertIn('grid-template-columns:minmax(0,1fr) auto auto minmax(0,1fr)', css)
        self.assertIn('align-items:center;justify-items:center;line-height:var(--row-height)', css)
        for cls, area in (('target-space','1/2/2/4'), ('target-price','2/2/3/3'), ('target-month','2/3/3/4')):
            self.assertIn(prefix + f'td.tgs .{cls}{{grid-area:{area}', css)
        self.assertIn('target-month{grid-area:2/3/3/4;justify-self:start;white-space:nowrap;'
                      'margin-left:var(--tgs-month-gap);font-size:var(--tgs-month-font);'
                      'color:var(--dim,#8b93a1)}', css)
        self.assertIn('td.tgs .target-sep{margin-right:var(--tgs-month-gap);'
                      'font-size:inherit;line-height:inherit;color:inherit}', css)
        self.assertNotIn('target-month::before', css)   # 分隔符是真元素，不用伪元素
        self.assertNotIn('.stk td.px .tgs{', css)

    def test_price_rows_drop_ohlc_and_keep_only_five_items(self):
        """2026-11-13：开／高／低下架，五项重排成左块上排 现价+涨跌额$／下排 涨跌幅、
        振／量上下两排靠右贴紫灯框左侧（上振下量）。"""
        d = dict(price=123.45, prev_close=120.0, chg=2.88, group='focus', level='gray',
                 signals=[], signal_marks=[('vol_ratio', 'yellow')],
                 day_open=120.5, day_high=124.0, day_low=119.8, day_amp=7.2, vol_ratio=2.1,
                 week_chg=3.2)
        px = monitor._price_rows_html(d, {'mode': 'closed'})
        left, sep, side = px.partition('<span class="px-side">')
        self.assertTrue(sep)
        self.assertEqual(px.count('class="px-row '), 2)          # 左块仍是两排
        self.assertEqual(px.count('class="px-metric'), 3)        # 振、量 + 周涨跌幅
        for cls in ('px-price', 'px-chg ', 'px-amt '):
            self.assertIn('class="' + cls, left)                 # 三项都在左块
        # 2026-11-13 午：涨跌额与涨跌幅交换位置，上排跟现价同行、下排单独一行。
        main = re.search(r'<span class="px-row px-row-main">(.*?)</span></span>', px, re.S).group(1)
        sub = re.search(r'<span class="px-row px-row-sub">(.*?)</span></span>', px, re.S).group(1)
        self.assertIn('class="px-price', main)
        self.assertIn('class="px-amt', main)
        self.assertIn('+3.45', main)                             # 11-17：涨跌额不再带 $
        self.assertNotIn('$', main)
        self.assertNotIn('class="px-chg', main)
        self.assertIn('class="px-chg', sub)
        self.assertIn('+2.88%', sub)
        self.assertIn('class="px-metric px-week"', sub)         # 周涨跌幅在涨跌幅与量比之间
        self.assertIn('+3.2%', sub)
        self.assertNotIn('class="px-amt', sub)
        for cls in ('px-amp', 'px-vol'):
            self.assertIn(cls, side)                             # 侧列两项
        for label in ('开', '高', '低'):
            self.assertNotIn('>' + label + '</span>', px)
        for dead in ('px-open', 'px-high', 'px-low', 'day_open', 'day_high', 'day_low'):
            self.assertNotIn(dead, px)
            self.assertNotIn(dead, page_assets.PAGE_CSS)        # 灰字规则一并删除，不留死选择器
        self.assertIn('+2.88%', px)
        self.assertIn('7.2%', px)
        self.assertIn('2.1×', px)
        self.assertIn('px-v amp-yellow', px)                    # 振幅与量比染色不受影响
        self.assertIn('px-v vol-yellow', px)

    def test_week_change_gap_and_number_tone_2026_11_17(self):
        """2026-11-17 用户「周涨跌幅和日涨跌幅拉开点距离，周涨跌幅的比例数字用涨跌色，
        『周』颜色不变」：距离由 --px-week-gap 单独给 .px-week 的左外距，叠在下排 --px-gap 上，
        不碰上排现价↔涨跌额的 1ch、不碰振／量；数字只挂全局 up/down（--up/--down，
        与日涨跌幅 .px-chg 同一套），标签仍是 var(--dim) 的 .px-k；零值不染色。"""
        css = page_assets.PAGE_CSS
        self.assertEqual(css.count('--px-week-gap:6px'), 1)
        self.assertIn('.px-week{margin-left:var(--px-week-gap)}', css)
        self.assertIn('.px-metric{display:inline-flex;align-items:baseline;gap:2px}', css)
        self.assertIn('.px-k{font-size:14px;color:var(--dim)}', css)
        self.assertNotIn('.px-week .px-k{color:', css)
        base = dict(price=110.0, prev_close=104.0, chg=1.0, group='focus', level='gray',
                    signals=[], signal_marks=[], day_amp=1.0, vol_ratio=1.0)
        for week, cls in ((3.2, 'up'), (-2.4, 'down'), (0.0, '')):
            px = monitor._price_rows_html(dict(base, week_chg=week), {'mode': 'closed'})
            self.assertIn('<span class="px-metric px-week"><span class="px-k">周</span>'
                          '<span class="px-v%s">%+.1f%%</span></span>'
                          % (' ' + cls if cls else '', week), px)
        self.assertNotIn('px-week', monitor._price_rows_html(base, {'mode': 'closed'}))

    def test_price_change_amount_dollar_sign_variants(self):
        """2026-11-17 涨跌额在显示层去掉 $：正负零与缺价四态，day_change_amount 口径不动。"""
        base = dict(group='focus', level='gray', signals=[], signal_marks=[], chg=1.0,
                    day_amp=1.0, vol_ratio=1.0)
        cases = ((123.45, 120.0, '+3.45', 'up'),
                 (117.6, 120.0, '-2.40', 'down'),
                 (120.0, 120.0, '0.00', ''),
                 (None, 120.0, '—', ''))
        for price, prev, text, cls in cases:
            with self.subTest(price=price, text=text):
                px = monitor._price_rows_html(dict(base, price=price, prev_close=prev), {'mode': 'closed'})
                self.assertIn(f'<span class="px-amt {cls}">{text}</span>', px)
        # 统计名单与其它口径仍不带货币符号。
        from monitor_rules import day_change_amount
        self.assertEqual(day_change_amount(123.45, 120.0), '+3.45')

    def test_week_change_blank_macro_cell_and_fade_symmetry_2026_11_17(self):
        """11-17 定稿：周涨跌幅＝现价相对 5 个交易日前收盘（closes[-6]），只显示不报灯；
        缺值整项留空；变化列不看周变化时空白、看涨跌时挂 data-sign；
        渐变上边缘距单线 5px、横向截止在紫灯区左边界、代码文字位置不动；涨跌额与现价恒隔 1ch。"""
        from monitor_rules import _display_session_values
        css = page_assets.PAGE_CSS
        # 数据层：末根 110 是当日价，closes[-6]=100 是 5 个交易日前 → +10.0%
        data = {'closes': [100, 101, 102, 103, 104, 110], 'ma_range_valid': [False] * 6}
        got = _display_session_values(data, 110.0, 104.0)
        self.assertAlmostEqual(got['week_chg'], 10.0)
        self.assertIsNone(_display_session_values({'closes': [100, 110]}, 110.0, 100.0)['week_chg'])
        # 渲染层：有值出「周 +10.0%」，缺值连标签都不生成（不写「—」）
        base = dict(price=110.0, prev_close=104.0, chg=5.77, group='focus', level='gray',
                    signals=[], signal_marks=[], day_amp=1.0, vol_ratio=1.0)
        px = monitor._price_rows_html(dict(base, week_chg=10.0), {'mode': 'closed'})
        self.assertIn('<span class="px-metric px-week"><span class="px-k">周</span>'
                      '<span class="px-v up">+10.0%</span></span>', px)
        self.assertNotIn('px-week', monitor._price_rows_html(base, {'mode': 'closed'}))
        self.assertNotIn('+$', px)
        # CSS：1ch 间距、渐变载体挪到整行 tr、5px 上留白／右端截止紫灯区、变化列下排灰字
        self.assertIn('.px-amt{margin-left:1ch}', css)
        self.assertNotIn('--sym-fade-crop', css)          # 上下等距口径已退役，不留死变量
        self.assertIn('--sym-fade-top:5px', css)
        self.assertIn('--sym-fade-w:calc(100% - var(--tgs-w))', css)
        self.assertIn('background-size:var(--sym-fade-w) calc(var(--row-pad-top) + var(--row-height) + var(--row-height) - var(--sym-fade-top))', css)
        self.assertIn('background-position:0 var(--sym-fade-top)', css)
        self.assertIn('td.num.dim{font-size:var(--macro-chg);color:var(--text)}', css)
        self.assertIn('td.num.dim[data-sign="up"]{color:var(--up)}', css)
        self.assertIn('td.num.dim[data-sign="down"]{color:var(--down)}', css)
        # 11-17 用户「变化列下排『周』『周 bp』保持灰色」：不跟上排涨跌色跳
        self.assertIn('td.num.dim .unit{font-size:var(--macro-aux);color:var(--dim)}', css)
        self.assertIn('td.num.dim .chg-num{font-size:var(--macro-chg);color:var(--dim)}', css)
        # 2026-11-23 再定稿：标普／纳指变化列两排＝上排百分比＋下排灰字「200均线偏离」，
        # 标普不再写周变化；VIX 两排＝上排均线数值＋下排灰字「200日均线」。
        # 空值整格留空仍只在 chg_cell 一处（精确 HTML 由 mm_test_render.py 守门）。
        src = (ROOT / 'monitor_render.py').read_text(encoding='utf-8')
        self.assertIn('def chg_cell(txt, unit="", sign=""):', src)
        self.assertIn("if not txt:\n                return '<td class=\"num dim\"></td>'", src)
        self.assertIn('<span class="ma-dev">{dev:+.1f}%</span>', src)
        self.assertIn('<span class="unit ma-tag">200均线偏离</span>', src)
        self.assertIn('<span class="ma-val">{it["ma200"]:.2f}</span>', src)
        self.assertIn('<span class="unit ma-tag">200日均线</span>', src)
        self.assertIn('<span class="ma-dev">{nd_dev:+.1f}%</span>', src)
        self.assertIn('nd_chg = chg_cell("")', src)                   # 纳指缺均线数据时整格留空
        self.assertIn('dw_unit = "周" + dw_pct', src)                 # 仅旧快照回退还留着周变化
        # 上排主数字跟「当前」列点位同号同行高；下排「200」不放大，整排沿用 .unit
        self.assertIn('td.num.dim .ma-tag{letter-spacing:-.5px;white-space:nowrap;'
                      'line-height:calc(var(--macro-chg) * 1.15)}', css)
        self.assertNotIn('.ma-tag{font-size:', css)
        self.assertIn('td.num.dim .ma-val{display:block;font-size:var(--macro-body);line-height:1.25}', css)
        # 11-23 上涨参与度「当前」列也是上数字／下灰字两排（td.num，不带 .dim）
        self.assertIn('td.num:not(.dim) .ma-val{display:block;font-size:var(--macro-body);line-height:1.25}', css)
        self.assertIn('td.num:not(.dim) .ma-tag{display:block;letter-spacing:-.5px;white-space:nowrap;', css)
        # 11-23 10Y 美债变化列：月、周各占一排（数字白＋灰字「月 bp」「周 bp」同一排）；两排行盒高度
        # 锁成 18.8／16.1，与 VIX、上涨参与度那类两排行同高，不把这一行多撑出 2~3px
        self.assertIn('td.num.dim .bp-row{display:block;white-space:nowrap;height:18.8px;line-height:18.8px;overflow:visible}', css)
        self.assertIn('td.num.dim .bp-row:last-child:not(:only-child){height:16.1px;line-height:16.1px}', css)
        self.assertIn('td.num.dim .bp-num{font-size:var(--macro-chg);color:var(--text)}', css)
        self.assertIn('td.num.dim .bp-row .unit.bp-unit{display:inline;margin-left:3px;font-size:var(--macro-aux);color:var(--dim);line-height:inherit}', css)
        self.assertIn('<span class="bp-row"><span class="bp-num">', src)
        self.assertIn('bp_row(um, "月 bp") + bp_row(uw, "周 bp")', src)
        self.assertNotIn('<span class="unit-num">200</span>', src)
        # 手机档：名称列收窄，当前列与变化列左移靠近名称列，变化列与评语列一起放宽
        self.assertIn('td:first-child{width:27%}', css)
        self.assertIn('td:nth-child(2){width:28%}', css)
        self.assertIn('td:nth-child(3){width:21%}', css)
        self.assertIn('td:nth-child(4){width:24%}', css)
        self.assertNotIn('.ma-label', css)

    def test_earnings_fallback_has_no_dead_disclaimer_text(self):
        """22:35：没取到日期的财报整条隐藏，源码不许再留失败解释长句或占位提示。"""
        src = (ROOT / "monitor.py").read_text(encoding="utf-8")
        for dead in ("不代表没有财报", "本轮读取失败", "日期暂未取得",
                     'return "日期待确认", "未取得"'):
            self.assertNotIn(dead, src)
        # 行为守门：失败态连整条都不渲染（源码别处的「未取得行情」是行情文案，不受此约束）。
        self.assertIsNone(monitor.earnings_label({"status": "error"}))
        self.assertEqual(monitor._earnings_html({'earnings': {'status': 'error'}}, '2026-11-13'), '')

    def test_volume_lamp_only_on_number_and_earnings_display_thresholds(self):
        """量比只在价格区数字上表达灯级；财报倒计时 >15 天、整条 >30 天不显示（2026-11-12）。"""
        cases = (
            ({'vol_ratio': 4.2, 'signal_marks': [('vol_ratio', 'red')]}, 'closed', 'vol-red'),
            ({'vol_ratio': 2.1, 'signal_marks': [('vol_ratio', 'yellow')]}, 'closed', 'vol-yellow'),
            ({'rt_vol_ratio': 3.3, 'signal_marks': [('vol_ratio', 'yellow')]}, 'intraday', 'vol-yellow'),
            ({'vol_ratio': 2.1, 'signal_marks': []}, 'closed', ''),
            ({'signal_marks': [('vol_ratio', 'yellow')]}, 'closed', ''),   # 静默期数字是「—」，不挂色
        )
        for data, mode, expected in cases:
            with self.subTest(data=data, mode=mode):
                d = dict(price=100.0, chg=1.0, group='focus', level='yellow',
                         signals=['量比 2.1×'], **data)
                px = monitor._price_rows_html(d, {'mode': mode})
                if expected:
                    self.assertIn('px-v ' + expected, px)
                else:
                    self.assertNotIn('px-v vol-', px)
                self.assertNotIn('量比', monitor._signals_html(d))
        today = '2026-10-07'
        ev = lambda day, timing='pre': {'status': 'ok', 'date': day, 'timing': timing, 'kind': 'expected'}
        self.assertEqual(monitor.earnings_countdown(ev('2026-10-22'), today), '（北京15天 10/22 晚间）')
        self.assertEqual(monitor.earnings_countdown(ev('2026-10-23'), today), '')
        self.assertEqual(monitor.earnings_countdown(ev('2026-10-21', 'post'), today), '（北京15天 10/22 凌晨）')
        self.assertEqual(monitor.earnings_countdown(ev('2026-10-22', 'post'), today), '')
        plain = monitor._earnings_html({'earnings': ev('2026-10-23')}, today)
        self.assertIn('美 10/23 盘前', plain)
        self.assertNotIn('class="cd"', plain)
        self.assertNotIn('> 美', plain)   # 没倒计时时不留前导空格
        self.assertIn('美 11/06 盘前', monitor._earnings_html({'earnings': ev('2026-11-06')}, today))
        self.assertEqual(monitor._earnings_html({'earnings': ev('2026-11-07')}, today), '')
        # 两个阈值只管显示：判灯窗口、无异动折叠、统计名单窗口一个都没动。
        self.assertEqual(monitor.earnings_urgency(ev('2026-10-08'), today), 'red')
        self.assertEqual(monitor.earnings_urgency(ev('2026-10-13'), today), 'yellow')
        self.assertTrue(monitor.earnings_within_list(ev('2026-10-22'), today))
        self.assertFalse(monitor.earnings_within_list(ev('2026-10-23'), today))

    def test_amplitude_display_tone_colors_number_without_reporting_lamp(self):
        """振幅显示值 >6% 黄、>10% 红；只染数字，不报灯、不进统计（2026-11-12）。"""
        css = page_assets.PAGE_CSS
        self.assertIn('.px-v.amp-yellow{color:var(--lamp-yellow)}', css)
        self.assertIn('.px-v.amp-red{color:var(--lamp-red)}', css)
        cases = (
            (6.0, '6.0%', ''),
            (6.04, '6.0%', ''),            # 按显示值判色，避免出现「6.0% 却是黄字」
            (6.1, '6.1%', 'amp-yellow'),
            (10.0, '10.0%', 'amp-yellow'),
            (10.1, '10.1%', 'amp-red'),
            (18.6, '18.6%', 'amp-red'),
            (0, '0.0%', ''),
            (None, '—', ''),
        )
        for amp, text, expected in cases:
            with self.subTest(amp=amp):
                d = dict(price=100.0, chg=1.0, group='focus', level='gray',
                         signals=[], signal_marks=[], day_amp=amp)
                px = monitor._price_rows_html(d, {'mode': 'closed'})
                cell = re.search(r'<span class="px-metric px-amp">.*?</span></span>', px, re.S)
                self.assertIsNotNone(cell)
                self.assertIn('>' + text + '</span>', cell.group(0))
                if expected:
                    self.assertIn('px-v ' + expected, px)
                else:
                    self.assertNotIn('px-v amp-', px)
                self.assertNotIn('px-v vol-', px)
                # 纯显示：描述、灯级、信号标记、统计类别、设置项一概不动
                self.assertNotIn('振幅', monitor._signals_html(d))
                self.assertEqual(d['level'], 'gray')
                self.assertEqual(d['signal_marks'], [])
                self.assertNotIn(('amp', '振幅'), monitor.SIGNAL_KINDS)
                self.assertNotIn('amp_yellow', monitor.DEFAULT_SETTINGS)
                self.assertNotIn('amp_red', monitor.DEFAULT_SETTINGS)
        self.assertEqual(_amp_display_tone(10.04), 'yellow')
        self.assertEqual(_amp_display_tone('7.2'), '')

    def test_header_relayout_2026_11_20(self):
        """抬头重排：站名 32px 锁 39px 行盒；定投贴站名；规则/仓位同步上下两排紧贴编辑按钮。"""
        css = page_assets.PAGE_CSS
        self.assertIn('.brandbar h1.brand{margin:0;font-size:32px;line-height:39px;', css)
        self.assertIn('.sync-stack{display:flex;flex-direction:column;justify-content:flex-start;gap:0;flex:none;min-width:0;margin-left:2px;min-height:calc(var(--status-font-size)*2.5)}', css)
        self.assertIn('.sync-stack .runlight{flex:none;white-space:nowrap;line-height:1.25;min-height:calc(var(--status-font-size)*1.25)}', css)
        self.assertIn('.sync-stack .cost-state{min-width:0;white-space:nowrap;text-align:left;font-size:var(--status-font-size);line-height:1.25;min-height:calc(var(--status-font-size)*1.25)}', css)
        # 2026-11-23 用户：「暂无法核验」时只剩一排灰字、被居中到两排之间 → 栈恒占两行高、自上而下排
        self.assertNotIn('.sync-stack{display:flex;flex-direction:column;justify-content:center', css)
        # 2026-11-23 用户：抬头两排里「规则已生效」前的圆灯去掉，两排文字同一条竖线左对齐、已同步与已生效同绿
        self.assertIn('.sync-stack .rulelight .dot{display:none}', css)
        self.assertNotIn('.sync-stack .cost-state{padding-left', css)
        self.assertIn('.sync-stack .cost-state .ok{color:var(--green)}', css)
        self.assertIn('.runlight[data-phase="ok"]{color:var(--green)}', css)
        self.assertIn('.brandbar .dca-badge{font-size:16px;margin-left:10px;margin-bottom:4px}', css)
        self.assertNotIn('.statusrow.flagrow', css)
        self.assertNotIn('.flagrow .rulelight .dot{display:none', css)   # 旧的 flagrow 写法仍不存在（现行规则是 .sync-stack 里的 .rulelight .dot）
        page = self.render()
        self.assertIn('<span class="sync-stack" id="syncStack">', page)
        self.assertNotIn('<div class="statusrow flagrow">', page)
        bar, status = page.split('<div class="statusrow">', 1)
        self.assertIn('id="costEditBtn"', bar)
        self.assertLess(bar.index('id="costEditBtn"'), bar.index('id="syncStack"'))
        self.assertIn('id="ruleLight"', bar)
        self.assertNotIn('id="ruleLight"', status[:status.index('</div>')])
        self.assertIn('id="runLight"', status)
        self.assertIn('<i class="dot"></i><span id="ruleLightTxt">', bar)
        cost_js = (ROOT / 'cost_panel.js').read_text(encoding='utf-8')
        self.assertIn("querySelector('.sync-stack')", cost_js)
        self.assertIn('<span class="warn">仓位待核验</span>', cost_js)
        self.assertNotIn('上传结果待核验', cost_js)
        self.assertNotIn('未填 token</span>', cost_js)

    def test_only_two_breakpoints_phone_600_and_desktop_601(self):
        """只适配三台设备（2026-11-01 定）：手机档 ≤600（按 iPhone 18 Pro Max 竖屏 440 定稿）、
        桌面档 ≥601（iPad mini 744/1133 与 PC 共用）。不允许再冒出更细的断点。"""
        css = page_assets.PAGE_CSS
        cost = (ROOT / 'cost_panel.css').read_text(encoding='utf-8')
        edit = (ROOT / 'edit.html').read_text(encoding='utf-8')
        for name, text in (('PAGE_CSS', css), ('cost_panel.css', cost), ('edit.html', edit)):
            conds = re.findall(r'@media[^{]*?(?:max|min)-width:\s*(\d+)px', text)
            bad = sorted({c for c in conds if c not in ('600', '601')})
            self.assertEqual(bad, [], f'{name} 出现了 600/601 之外的断点：{bad}')
        # 桌面档只认 ≥601，手机档只认 ≤600，两边不能有重叠或空档
        self.assertIn('@media(min-width:601px){', css)
        # 第十三轮只把 .lvtag>span 规则加进已有的 601 块，不新增重复媒体块/第三档断点
        self.assertEqual(css.count('@media(min-width:601px){'), 1)

    def test_mobile_dual_row_open_tightens_gap_2026_11_18(self):
        # 2026-11-18 用户：手机上「当日 / 风险」展开后面板离标题偏远。
        # 桌面档早把展开态压到 50px，手机档漏覆盖仍是基础 63px；现在手机档补 53px，
        # 与折叠态同高：既消掉多出的空白，收起 / 展开两态也不跳高。
        css = page_assets.PAGE_CSS
        mobile = css.split('@media(max-width:600px){', 1)[1].split('@media(min-width:601px){', 1)[0]
        desktop = css.split('@media(min-width:601px){', 1)[1]
        self.assertIn('.dual-row > details[open]{min-height:53px}', mobile)
        self.assertEqual(mobile.count('.dual-row > details[open]{min-height:53px}'), 1)
        self.assertIn('.dual-row > details[open]{min-height:50px}', desktop)
        self.assertIn('.day-pnl-card:not([open]){padding-bottom:2px;min-height:53px}', mobile)
        self.assertIn('.dual-row > details[open]{padding-bottom:2px;min-height:63px', css)

    def test_mobile_stock_numbers_and_main_row_padding_are_compact(self):
        css = page_assets.PAGE_CSS
        mobile = css.split('@media(max-width:600px){', 1)[1].split('@media(min-width:601px){', 1)[0]
        # 手机 17px 代码 + 左 6 / 右 2px 内距：与 --sym-w:98px 成对，中文备注名可见宽度仍 90px＝6 字，
        # 左侧离 2px 灯条留 4px（19:35 恢复后不再动），右侧把省下的 4px 让给灰线左移（20:51）。
        self.assertIn('table.stk tr[data-symbol]>td.sym{font-size:17px;padding-left:6px;padding-right:2px}', mobile)
        self.assertIn('--tgs-w:108px', mobile)
        # 紫灯框字号已统一成 --tgs-font（桌面 13 / 手机 16），且恒定：窄屏压缩不许改字号。
        self.assertIn('--tgs-font:13px;', css)
        self.assertIn('--tgs-font:16px;', mobile)
        self.assertIn('--tgs-pad-l:2px;', css)
        self.assertIn('--tgs-pad-r:3px;', css)
        # 手机档 16px 比 15px 宽约 5px，靠左右内距归零腾出；桌面档内距保持 2/3px。
        self.assertIn('--tgs-pad-l:0px;--tgs-pad-r:0px;--pos-num-fs:16px}', mobile)
        self.assertNotIn('td.tgs .lvtag.tgt{font-size:15px}', css)
        self.assertNotIn("setProperty('--tgs-font'", page_assets.PAGE_INTERACTION_JS)
        # 2026-11-13 用户定值：手机档振/量 15px、涨跌幅/涨跌额 16px；压缩档只收间距、不许改字号。
        for rule in ('table.stk tr[data-symbol] .px-v{font-size:15px}',
                     'table.stk tr[data-symbol] :is(.px-chg,.px-amt){font-size:16px}',
                     'table.stk tr[data-symbol][data-pxfit] .px-v{font-size:15px}',
                     'table.stk tr[data-symbol][data-pxfit] :is(.px-chg,.px-amt){font-size:16px}'):
            self.assertIn(rule, mobile)
        # 桌面档基础仍是 17.5 / 16.5 / 14，基础规则里不在手机档重抄一遍。
        self.assertIn('.px-price{font-size:17.5px}', css)
        self.assertIn('.px-chg,.px-amt,.px-v{font-size:16.5px}', css)
        self.assertIn('.px-k{font-size:14px', css)
        self.assertIn('table.stk tr.range52-row>td:first-child{padding:0 6px 2px 6px}', mobile)
        self.assertIn('table.stk tr[data-symbol]>td.sym{grid-area:sym;position:static;display:block;padding:var(--row-pad-top) 8px 0;', css)
        self.assertNotIn('data-layout="tight"', css)
        self.assertNotRegex(css, r'add-ck|ack-btn|手机（iOS Chrome）最终覆盖')

    def test_mobile_descriptions_15px_and_earnings_two_rows(self):
        css = page_assets.PAGE_CSS
        self.assertIn('table.stk tr[data-symbol]>td.sig{padding:var(--sig-row-gap) 5px 7px;font-size:15px;line-height:20.25px}', css)
        # 2026-11-12 收紧个股行上下留白：描述区上留白与成本条上下间距，桌面/手机两档都要守住。
        # 14:58 的「线组距上下内容各 +3px」不许借 td.sig 下内距实现：紧贴线的上一行是 52 周条，
        # 加在 sig 底部只会整体下推、线到 52 周条的距离不变，还会白多 3px 行高。
        self.assertIn('td.sig{grid-area:sig;display:block;padding:var(--sig-row-gap) 10px 7px;', page_assets.PAGE_CSS)
        self.assertIn('td.cost-line{grid-area:cost;display:flex;align-items:center;justify-content:space-between;gap:6px;padding:2px 8px;margin:var(--sig-row-gap) 0 0;', page_assets.PAGE_CSS)
        self.assertNotIn('padding:9.5px 5px 7px', page_assets.PAGE_CSS)
        self.assertNotIn('padding:3px 8px;margin:5px 0 0', page_assets.PAGE_CSS)
        self.assertIn('.earn{display:block;margin:0;font-size:15px;font-weight:400;line-height:19px', css)
        # 2026-11-13：财报整排字号与描述区正文必须同号；以后任一边单独改字号都在这里拦下。
        earn_fs = re.search(r'\.earn\{[^}]*font-size:([\d.]+)px', css).group(1)
        sig_fs = re.search(r'table\.stk tr\[data-symbol\]>td\.sig\{[^}]*font-size:([\d.]+)px', css).group(1)
        self.assertEqual(earn_fs, sig_fs)
        self.assertIn('.earn-band{display:block;margin:5px 0 0;padding:2px 8px;background:var(--cost-row-bg)', css)
        self.assertIn('.earn .earn-sep,.earn .earn-bj{color:var(--earn-bj-color,var(--dim))}', css)
        self.assertNotIn('.earn>.unit', css)
        self.assertNotIn('.cd.tbd{color:var(--head-blue-on)}', css)
        for cls in ('add', 'cut'):
            body = re.search(r'\.stk td\.sig \.lvtag\.' + cls + r'\{([^}]*)\}', css)[1]
            self.assertIn('font:inherit', body)
            self.assertNotIn('font-size:', body)

    def test_purple_frame_top_even_ink_gaps_and_single_stock_divider(self):
        """2026-11-16 单线定稿：紫灯框上下框线到数字墨迹仍是 5px，框顶两档统一
        --tgt-frame-top:5px、--tgt-frame-bottom:-1px；个股分隔线只剩 1px border-top，
        ::after 第二条线整条删除（不留 display:none 死代码）。
        上方 5px＝range52 的 padding-bottom 2px + 非首行 margin-top 3px；
        下方 5px＝单线下沿（行顶下 1px）到框顶（1px + 5px）。"""
        css = page_assets.PAGE_CSS
        self.assertEqual(css.count('--tgt-frame-top:5px'), 1)
        self.assertIn('--tgt-frame-bottom:-1px', css)
        self.assertNotIn('--tgt-frame-top:12px', css)
        self.assertNotIn('--tgt-frame-top:10px', css)
        self.assertEqual(css.count('top:var(--tgt-frame-top);right:0;bottom:var(--tgt-frame-bottom);left:-1px'), 3)
        self.assertNotIn('top:2px;right:0;bottom:2px', css)
        self.assertEqual(css.count('border-top:1px solid var(--stock-pair-line)'), 1)
        self.assertEqual(css.count('var(--stock-pair-line)'), 1)
        self.assertNotIn('table.stk tr[data-symbol]:not(:first-child)::after', css)
        self.assertNotIn('background:var(--stock-pair-line)', css)
        self.assertNotIn('top:1px;height:1px;background:var(--stock-pair-line)', css)
        self.assertIn('table.stk tr[data-symbol]:not(:first-child){margin-top:3px}', css)
        self.assertIn('position:relative;padding:0 10px 2px 10px;', css)
        self.assertIn('table.stk tr.range52-row>td:first-child{padding:0 6px 2px 6px}', css)
        self.assertIn('--row-pad-top:3px', css)
        self.assertIn('--row-pad-top:5px', css)
        self.assertIn('td.sig{grid-area:sig;display:block;padding:var(--sig-row-gap) 10px 7px;', css)

    def test_first_row_to_next_row_equals_description_indicator_gap_2026_11_16(self):
        """2026-11-16：用户要求「代码/价格/紫灯两排」的下沿到持仓信息条或描述区首行文字的距离，
        等于描述区不同指标描述文字之间的间距。做法是 --sig-row-gap 提到 tr 级（一处同时管
        .sig-col 的 gap 与下一排的上留白），第一排三格下内距归零，间距全部由下一排承担。
        单线定稿后三格上内距改由 --row-pad-top 管（桌面 3 / 手机 5），下一排上留白仍引用
        变量 4px，所以「第一排下沿→下一排内容」不受影响，仍是 4px。"""
        css = page_assets.PAGE_CSS
        self.assertIn('border-top:1px solid var(--stock-pair-line);--lamp-line:var(--line);--sig-row-gap:4px;background-image:', css)
        for rule in ('td.sym{grid-area:sym;position:static;display:block;padding:var(--row-pad-top) 8px 0;',
                     'td.px{grid-area:px;position:relative;display:block;min-width:0;padding:var(--row-pad-top) 6px 0;',
                     'padding:var(--row-pad-top) var(--tgs-pad-r,3px) 0 var(--tgs-pad-l,2px);'):
            self.assertIn(rule, css)
        self.assertIn('padding:2px 8px;margin:var(--sig-row-gap) 0 0;', css)
        self.assertIn('td.sig{grid-area:sig;display:block;padding:var(--sig-row-gap) 10px 7px;', css)
        self.assertIn('td.sig{padding:var(--sig-row-gap) 5px 7px;', css)
        self.assertIn('.sig-col{display:flex;flex-direction:column;gap:var(--sig-row-gap);min-width:0}', css)
        for old in ('td.sig{grid-area:sig;display:block;padding:5px 10px 7px;',
                    'td.sig{padding:5px 5px 7px;',
                    'gap:6px;padding:2px 8px;margin:2px 0 0;',
                    'padding:7px 8px;font-size:14px', 'padding:7px 6px;text-align:left',
                    'padding:10px 8px 0;', 'padding:10px 6px 0;'):
            self.assertNotIn(old, css)
        # 第一排高度＝上内距 + 两排各 --row-height + 下内距 0：桌面 3+42＝45、手机 5+42＝47。
        # 框顶 5px / bottom -1px，两档框高仍是 41/43px，不裁第二行数字。
        row_h = float(re.search(r'--row-height:(\d+(?:\.\d+)?)px', css).group(1))
        pads = sorted(float(x) for x in re.findall(r'--row-pad-top:(\d+(?:\.\d+)?)px', css))
        tops = [float(x) for x in re.findall(r'--tgt-frame-top:(\d+(?:\.\d+)?)px', css)]
        bottom = float(re.search(r'--tgt-frame-bottom:(-?\d+(?:\.\d+)?)px', css).group(1))
        self.assertEqual(pads, [3, 5])
        self.assertEqual(tops, [5])
        self.assertEqual(bottom, -1)
        self.assertEqual([p + 2 * row_h - tops[0] - bottom for p in pads], [41, 43])
        self.assertNotIn('--tgt-frame-bottom:6px', css)

    def test_single_divider_equal_5px_gaps_2026_11_16(self):
        """2026-11-16 用户「单分隔线 1px。单分隔线上面离 52 周价格条数字下边缘和离
        紫灯框上边缘的距离相等为 5px」：
        单线＝1px border-top（::after 第二条线删除），线组下沿在行顶下 1px；
        框顶＝1px + --tgt-frame-top:5px，故「线→框」＝5px（两档统一）。
        上方＝上一行 range52 的 padding-bottom 2px + 本行 margin-top 3px＝5px，
        无财报条时上一行内容盒底就是 52 周价格数字墨迹下沿；带财报条的行
        （AAPL 等）财报条接在轨道下方，仍是「最后一行内容下沿→线」5px，
        52 周数字到线会自然更大，这是既有版式，不改。
        做法是整个第一排上移：三格上内距 10→3/5px、灯条/竖线 --lamp-top
        15.5/13→8.5/8px、框顶 12/10→5px；--tgt-frame-bottom 仍 -1px，
        框高 41/43px 与框内 5/5px 墨迹留白不变，下方到成本条/描述区 4px 不变。"""
        css = page_assets.PAGE_CSS
        self.assertEqual([float(x) for x in re.findall(r'--tgt-frame-top:(\d+(?:\.\d+)?)px', css)], [5])
        self.assertEqual(css.count('border-top:1px solid var(--stock-pair-line)'), 1)
        self.assertNotIn('table.stk tr[data-symbol]:not(:first-child)::after', css)
        self.assertIn('table.stk tr[data-symbol]:not(:first-child){margin-top:3px}', css)
        self.assertIn('position:relative;padding:0 10px 2px 10px;', css)
        self.assertIn('table.stk tr.range52-row>td:first-child{padding:0 6px 2px 6px}', css)
        # 上方 2+3＝5；下方 (1+5)−1＝5。
        pb = float(re.search(r'position:relative;padding:0 10px (\d+(?:\.\d+)?)px 10px;', css).group(1))
        mt = float(re.search(r'not\(:first-child\)\{margin-top:(\d+(?:\.\d+)?)px\}', css).group(1))
        border = float(re.search(r'border-top:(\d+(?:\.\d+)?)px solid var\(--stock-pair-line\)', css).group(1))
        top = float(re.search(r'--tgt-frame-top:(\d+(?:\.\d+)?)px', css).group(1))
        self.assertEqual(pb + mt, 5)
        self.assertEqual(border, 1)
        self.assertEqual(top, 5)
        for rule in ('td.sym{grid-area:sym;position:static;display:block;padding:var(--row-pad-top) 8px 0;',
                     'td.px{grid-area:px;position:relative;display:block;min-width:0;padding:var(--row-pad-top) 6px 0;',
                     'padding:var(--row-pad-top) var(--tgs-pad-r,3px) 0 var(--tgs-pad-l,2px);'):
            self.assertIn(rule, css)
        self.assertIn('--lamp-top:8.5px', css)
        self.assertIn('--lamp-top:8px;--lamp-h:36px', css)
        self.assertIn('--lamp-h:31.5px', css)
        for old in ('--lamp-top:15.5px', '--lamp-top:13px', '--lamp-top:12.5px',
                    '--lamp-top:10px;--lamp-h:36px',
                    '--tgt-frame-top:12px', '--tgt-frame-top:10px', '--tgt-frame-top:9px',
                    '--tgt-frame-top:7px;', '--tgt-frame-bottom:-4px',
                    'padding:10px 8px 0;', 'padding:10px 6px 0;',
                    'padding:7px 8px 0;', 'padding:7px 6px 0;',
                    'top:2px;height:1px;background:var(--stock-pair-line)'):
            self.assertNotIn(old, css)
        self.assertIn('--sig-row-gap:4px;background-image:', css)

    def test_code_column_blue_fade_only_2026_11_16(self):
        """2026-11-16 起「代码区底色渐变 20% 蓝」，2026-11-17 改为从代码区一路铺进价格区、
        到紫灯区左边界淡出：载体必须是整行 tr[data-symbol] 的背景（td 背景画不到格子外）。
        上边缘距本行 1px 单分隔线 5px（默认 padding-box 定位区，5px 是线底到渐变顶的空隙），
        下边缘贴首排底；td.sym / td.px / td.tgs 一律不许再各自画背景渐变。
        色值走 --sym-fade / --sym-fade-mid 两个 :root 变量，避免撞 10-28 退役的
        分组标题高亮字面量（测试禁止那个串回来）。"""
        css = page_assets.PAGE_CSS
        self.assertEqual(css.count('--sym-fade:rgba(var(--frame-purple-rgb),.16)'), 1)
        self.assertEqual(css.count('--sym-fade-mid:rgba(88,166,255,.08)'), 1)
        self.assertNotIn('linear-gradient(90deg,rgba(143,184,240,', css)
        self.assertNotIn('inset 3px 0 0 #8fb8f0', css)
        grad = 'linear-gradient(90deg,var(--sym-fade),var(--sym-fade-mid) 72%,transparent)'
        self.assertEqual(css.count(grad), 1)
        i = css.index(grad)
        brace = css.rfind('{', 0, i)
        sel = css[css.rfind('}', 0, brace) + 1:brace]
        body = css[brace:css.find('}', i)]
        self.assertTrue(sel.strip().endswith('table.stk tr[data-symbol]'))
        self.assertIn('background-image:' + grad, body)
        self.assertIn('background-repeat:no-repeat', body)
        self.assertIn('background-size:var(--sym-fade-w) ', body)
        self.assertIn('background-position:0 var(--sym-fade-top)', body)
        self.assertNotIn('background:' + grad, body)
        # td.sym 只管文字几何，不许再画背景（否则铺到价格区的渐变会被切成两段）
        sym_rule = re.search(r'table\.stk tr\[data-symbol\]>td\.sym\{[^}]*\}', css).group(0)
        self.assertNotIn('background', sym_rule)
        self.assertNotIn('--sym-fade-crop', css)
        self.assertNotIn('td.px', sel)
        self.assertNotIn('td.tgs', sel)
        # 「代码文字高度位置不变」：td.sym 的文字几何三件套原样保留
        self.assertIn('padding:var(--row-pad-top) 8px 0;', sym_rule)
        self.assertIn('font-size:14px;line-height:var(--row-height)', sym_rule)

    def test_code_fade_more_transparent_and_ends_before_target_zone_2026_11_17(self):
        """2026-11-17 用户「左边起始处太亮／更透明」＋「底色渐变右侧加到紫灯区前截止」：
        更透明＝起始不透明度 20% → 14%，RGB(143,184,240) 与色相不动，不换成更深的蓝；
        右端不再按行宽一半（含中途的 +30px），而是整行宽减第三列 --tgs-w，精确落在
        td.tgs 左边缘＝紫灯框所在列起点，六档视口与 fitTgs() 动态收窄都跟随。
        中段 5%／5px 上留白／高度公式／代码文字几何一律不动；background-size
        规则行仍只用 var(--sym-fade-w)，不开第二个变量。"""
        css = page_assets.PAGE_CSS
        self.assertEqual(css.count('--sym-fade:rgba(var(--frame-purple-rgb),.16)'), 1)
        self.assertNotIn('--sym-fade:rgba(143,184,240,.20)', css)
        self.assertEqual(css.count('--sym-fade-mid:rgba(88,166,255,.08)'), 1)
        self.assertIn('--sym-fade-w:calc(100% - var(--tgs-w))', css)
        self.assertNotIn('--sym-fade-w:50%', css)
        self.assertNotIn('calc(50% + 30px)', css)
        self.assertNotIn('--sym-fade-ext', css)
        self.assertIn('--sym-fade-top:5px', css)
        self.assertIn('background-position:0 var(--sym-fade-top)', css)
        self.assertIn('background-size:var(--sym-fade-w) calc(', css)
        self.assertNotIn('linear-gradient(90deg,rgba(143,184,240,', css)

    def test_text_only_gray_description_has_no_lamp_block_2026_11_17(self):
        """2026-11-17 定稿：用户先说「仅文字级别的灰字描述前不要有灯块」，随后纠正
        「仅文字灰字描述要保持缩进，只是不显示前面的方灯块」。规则表口径「仅文字＝
        折叠且不计白」，渲染层落为没有 data-signal-level 的灰字 .signal-item（默认 var(--dim)），
        含布林+RSI 组合里的 .sig-zone 与未点亮的加/减仓价 lvtag。这些项保留 8px 左内距、
        text-indent 归零（文字起点与有灯级描述同为 8px），但连 ::before 的 content 都不生成；
        红/黄/白灯/绿灯描述和 [data-near="1"] 到价点亮态的 3×10px 竖条一条不动。
        旧的「先画再透明」「灰字撤缩进」两条死规则都不许回来。"""
        css = page_assets.PAGE_CSS
        base = ('.stk td.sig .signal-item{display:block;min-width:0;overflow-wrap:anywhere;'
                'padding-left:8px;text-indent:0}')
        self.assertIn(base, css)
        lamp_sel = '.stk td.sig :is(.signal-item[data-signal-level],.signal-item[data-near="1"])'
        self.assertIn(lamp_sel + '{text-indent:-8px}', css)
        lamp = (lamp_sel + '::before{content:"";display:inline-block;width:3px;height:10px;'
                'margin-right:5px;border-radius:1px;background:currentColor;vertical-align:middle}')
        self.assertIn(lamp, css)
        # 组合里竖条只画在有灯级描述的 .sig-zone 上，外层仍有 display:none 兜底；
        # 仅文字组合把缩进落在 .sig-zone 自身，文字起点同样是 8px。
        zone_sel = '.stk td.sig .signal-item[data-signal-level] .sig-zone'
        self.assertIn(zone_sel + '{text-indent:-8px}', css)
        self.assertIn(zone_sel + '::before{content:"";', css)
        self.assertIn('.stk td.sig .sig-zone{display:block;min-width:0;overflow-wrap:anywhere;padding-left:8px;text-indent:0}', css)
        self.assertIn('.stk td.sig .signal-item:has(.sig-zone)::before{display:none}', css)
        # 退役串：无条件画竖条、灰字撤缩进／负缩进、未点亮 lvtag 的透明占位
        for retired in ('.stk td.sig .signal-item::before{',
                        '.stk td.sig .sig-zone::before{',
                        '.stk td.sig .lvtag:not([data-near="1"])::before{background:transparent}',
                        '.stk td.sig .signal-item{display:block;min-width:0;overflow-wrap:anywhere}',
                        '.stk td.sig .sig-zone{display:block;min-width:0;overflow-wrap:anywhere}',
                        '.stk td.sig .signal-item{display:block;min-width:0;overflow-wrap:anywhere;padding-left:8px;text-indent:-8px}',
                        '.stk td.sig .sig-zone{display:block;min-width:0;overflow-wrap:anywhere;padding-left:8px;text-indent:-8px}'):
            self.assertNotIn(retired, css)

    def test_stock_pair_line_hsl_lightness_plus5_2026_11_16(self):
        """2026-11-16 用户「分隔线的 HSL 亮度增加 5%」：以 42% 档 #606876 为基准，
        名义明度 42→47%（+5 个百分点），保持其 H218.18°/S10.28% 不动，只把 L 设 47%
        反算 RGB 再取整 → #6c7484＝(108,116,132)，实得 L47.06%，比旧色 +5.10 个百分点。
        守的是「增量」与「只提明度」：旧色不许回来、52 周横条/设置页行线/数字色一律不连坐。"""
        import colorsys
        css = page_assets.PAGE_CSS
        hex_color = re.search(r'--stock-pair-line:(#[0-9a-f]{6})', css).group(1)
        new = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        old = (0x60, 0x68, 0x76)   # 42% 档 #606876
        h_old, l_old, s_old = colorsys.rgb_to_hls(*[c / 255 for c in old])
        h_new, l_new, s_new = colorsys.rgb_to_hls(*[c / 255 for c in new])
        # 反算出来的必须是这个色，不许随手挑一个「看起来差不多」的灰蓝。
        expect = tuple(round(c * 255) for c in colorsys.hls_to_rgb(h_old, 0.47, s_old))
        self.assertEqual(new, expect)
        self.assertAlmostEqual(l_new * 100, 47.0, delta=0.2)
        self.assertAlmostEqual((l_new - l_old) * 100, 5.0, delta=0.2)
        # 只提明度：H/S 的偏差只来自整数 RGB 量化（色相偏 1.8°、饱和偏 0.28%）。
        self.assertLess(abs(h_new - h_old) * 360, 3)
        self.assertLess(abs(s_new - s_old) * 100, 1)
        self.assertNotIn('--stock-pair-line:#606876', css)
        self.assertNotIn('--stock-pair-line:#8b93a1', css)
        self.assertNotIn('--stock-pair-line:#9fa3aa', css)
        self.assertIn('--range-track-line:#495568', css)
        self.assertIn('--stock-divider:#495568', css)
        self.assertIn('--range-track:#9fa3aa', css)

    def test_stock_pair_line_and_range_track_swap_colors_2026_11_16(self):
        """2026-11-16：先把个股分隔线与 52 周横条互换（横条取原线色 #495568），
        随后线色经 #8b93a1 定为 HSL 明度 42% 的 #606876，同日再 +5 个百分点到 47% → #6c7484
        （保持原色相/饱和度只提明度）；两边价格数字、价位三角、统计名单灰灯与设置页行线
        一律保持原值，不连坐换色。"""
        css = page_assets.PAGE_CSS
        self.assertIn('--stock-pair-line:#6c7484', css)
        self.assertNotIn('--stock-pair-line:#8b93a1', css)
        self.assertNotIn('--stock-pair-line:#9fa3aa', css)
        # 只改明度：从 CSS 取回实际色值，验证 HSL 明度落在 47% ±0.2%（整数 RGB 实得 47.06%）
        import colorsys
        hex_color = re.search(r'--stock-pair-line:(#[0-9a-f]{6})', css).group(1)
        rgb = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        self.assertAlmostEqual(colorsys.rgb_to_hls(*[c / 255 for c in rgb])[1] * 100, 47.0, delta=0.2)
        self.assertIn('--range-track-line:#495568', css)
        self.assertIn('--stock-divider:#495568', css)
        self.assertIn('--range-track:#9fa3aa', css)
        self.assertIn('background:var(--range-track-line);border-radius:99px}', css)
        self.assertIn('border-top:1px solid var(--stock-pair-line)', css)
        self.assertNotIn('background:var(--stock-pair-line)', css)
        self.assertEqual(css.count('var(--stock-pair-line)'), 1)
        self.assertIn('.range52-lo{position:relative;flex:none;font-size:10px;line-height:11px;color:var(--dim)', css)
        self.assertIn('.range52-hi{position:relative;flex:none;font-size:10px;line-height:11px;color:var(--dim)', css)
        self.assertIn('border-top:9px solid var(--range-marker-border);', css)
        self.assertIn('border-top:7px solid var(--range-marker-fill)', css)
        self.assertIn('--name-lamp:var(--range-track)}', css)

    def test_extreme_narrow_screen_compresses_purple_frame_not_price_overlap(self):
        """2026-11-13：极端窄屏自动压缩紫灯框内空间（左右内距 + 列宽），不让振/量数字画进紫灯框。
        用户明确「紫灯框里的数字字号不该变小」：字号恒定 13/16px，只压留白；常规视口零改动，
        列宽收到框内不溢出的极限后仍侵占才放行价格区第 3/4 档与裁切。"""
        css, js = page_assets.PAGE_CSS, page_assets.PAGE_INTERACTION_JS
        self.assertIn('font-size:var(--tgs-font)', css)
        for rule in ('table.stk tr[data-symbol][data-pxfit="3"] .px-row{gap:3px}',
                     'table.stk tr[data-symbol][data-pxfit="3"] .px-main{column-gap:4px}',
                     'table.stk tr[data-symbol][data-pxfit="3"] .px-price{font-size:13.5px}',
                     'table.stk tr[data-symbol][data-pxfit="3"] :is(.px-chg,.px-amt,.px-v){font-size:12.5px}',
                     'table.stk tr[data-symbol][data-pxfit="3"] .px-k{font-size:11.5px}',
                     'table.stk tr[data-symbol][data-pxfit="4"] .px-price{font-size:11.5px}',
                     'table.stk[data-tgs-cut] tr[data-symbol]>td.px{overflow:hidden}',
                     'table.stk[data-tgs-cut]{--tgs-month-gap:0px}'):
            self.assertIn(rule, css)
        for fn in ('function tgsNeed(own)', 'function tgsInnerOver(own)',
                   'function resetTgs()', 'function fitTgs()'):
            self.assertIn(fn, js)
        self.assertIn('const TGS_CLEAR = 3, TGS_MIN_W = 58;', js)
        # 用户 2026-11-13 20:39「不要收拾小于 440 的」：收紧/安全余量一类 <440 专用补丁一律不进真源。
        self.assertNotIn('TGS_INNER_SAFE', js)
        self.assertNotIn('TGS_FRAME_CLEAR', js)
        self.assertIn("table.style.setProperty('--tgs-pad-l', '0px');", js)
        self.assertIn("table.style.setProperty('--tgs-pad-r', '0px');", js)
        self.assertIn("table.dataset.tgsCut = '1';", js)
        self.assertIn('padding:var(--row-pad-top) var(--tgs-pad-r,3px) 0 var(--tgs-pad-l,2px)', css)
        self.assertIn("const mode = row.closest('table.stk').dataset.tgsfit;", js)
        self.assertIn("table.dataset.tgsfit = 'ultra';", js)
        # 每帧先回基准列宽再量 fitPx，最后整体压缩：顺序写死，避免逐帧来回摆。
        self.assertIn('resetTgs();\n      pending.forEach(layout);\n      fitTgs();', js)

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
        # 主表左侧竖灯条已撤（2026-11-03 按图），52 周位置行随之不再有 --lc 续接规则
        self.assertEqual(re.search(r'(?:^|\n)\.stk tr\.range52-row>td\{([^}]*)\}', css)[1], 'display:block;width:100%')
        self.assertNotIn('linear-gradient(var(--lc),var(--lc))', css)
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
        self.assertIn('#signalCounts .signal-filter li.has-separator{border-top:1px solid var(--line)}', css)
        self.assertIn('.target-purple .target-space-high .signal-member-direction', css)
        self.assertIn('.target-purple .note{display:inline;margin-left:6px}', css)
        self.assertIn('.target-purple .signal-member-direction{color:var(--purple)}', css)
        self.assertIn('--purple:#b084ff', css)
        self.assertEqual(css.count('#a371f7'), 1)  # 只在 :root 的 --frame-purple
        self.assertNotIn('.target-purple .target-space-high .note', css)
        self.assertNotIn('.target-purple .target-space-high .signal-member-belong', css)
        self.assertNotIn('.tone-target-high,.target-space-high{', css)
        self.assertNotIn('#signalCounts .tone-target-high,#signalCounts .target-space-high,', css)
        self.assertNotIn('td.tgs .lvtag.tgt.high{', css)   # >=90% 数字沿用普通紫，只有外框紫红
        self.assertNotIn('.sig .tp{', css)
        # 删除右条后不再存在跨行虚线相位；左竖条与它的续行也一并撤掉（2026-11-03）
        self.assertNotIn('var(--lw)', css)
        self.assertNotIn('var(--pw)', css)

    def test_positions_green_tints_and_shared_collapse_background(self):
        css = page_assets.PAGE_CSS
        self.assertIn('--signal-bg:#182131;', css)
        self.assertIn('--collapse-bg:var(--signal-bg);', css)
        # 11-10终稿：父去框但保卡片底和原标题位置；子框仍保持绿色。
        # 2026-11-19 Demo：「仓位」父卡改黄框、可点开合；长/短持仓是它后面两张平级绿框卡。
        pos = re.search(r'#group_positions,#group_positions\[open\]\{([^}]+)\}', css)[1]
        self.assertIn('background:var(--card)', pos)
        self.assertIn('border:var(--frame-w) solid var(--frame-yellow)', pos)
        self.assertIn('padding:3px 0 2px', pos)
        self.assertIn('#group_positions>summary{cursor:pointer;margin-left:5px;margin-right:5px}', css)
        tint = re.search(r'--positions-collapse:rgba\((\d+),(\d+),(\d+),([.\d]+)\)', css)
        self.assertIsNotNone(tint)
        self.assertEqual(tuple(map(int, tint.groups()[:3])), (46, 160, 67))
        self.assertGreater(float(tint[4]), 0)
        self.assertNotIn('#group_positions>summary::before{visibility:hidden}', css)  # 三角恢复可见
        self.assertIn('--frame-green:#2c8f3e', css)
        self.assertNotIn('border-color:', pos)
        # 2026-11-20：长短持仓改成与当日／风险同款并排双卡，绿框专属规则已退休。
        zone = re.search(r'#zoneDualRow\{([^}]+)\}', css)[1]
        self.assertIn('--collapse-bg:var(--positions-collapse)', zone)
        self.assertIn('#zoneDualRow:has(> details[open]) > .dual-body{display:block}', css)
        self.assertIn('height:26px;', re.search(r'\n\.collapse-group\{([^}]*)\}', css)[1])
        self.assertIn('#signalCounts{--collapse-bg:var(--signal-collapse);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}', css)
        button = re.search(r'\n\.collapse-group\{([^}]*)\}', css)[1]
        self.assertIn('background:var(--collapse-bg)', button)
        self.assertIn('.collapse-group:hover{color:var(--text);background:var(--collapse-bg)}', css)
        self.assertNotIn('.collapse-group:hover{color:var(--text);background:transparent}', css)

    def test_day_links_have_natural_name_height_and_visible_border_contract(self):
        css = strip_css_comments((ROOT / 'cost_panel.css').read_text(encoding='utf-8'))
        name = re.search(r'\.cost-contrib \.cn-name\s*\{([^}]*)\}', css)[1]
        self.assertNotRegex(name, r'(?:^|;)\s*(?:height|max-height)\s*:')
        self.assertIn('overflow: hidden', name)
        link = re.search(r'\.dual-body > ul\.cost-contrib\[data-owner="day"\] \.signal-member-jump\s*\{([^}]*)\}', css)[1]
        for declaration in ('display: inline-block', 'line-height: 20px', 'max-width: 100%',
                            'border-bottom: 1px dotted var(--list-border)', 'color: var(--text)'):
            self.assertIn(declaration, link)
        self.assertNotRegex(link, r'(?:^|;)\s*(?:height|max-height)\s*:')
        # 此为源契约，不代替真实边框box未被cn-name或祖先裁切的几何验收。

    def test_description_comma_hangs_without_changing_inline_spacing(self):
        css = strip_css_comments(page_assets.PAGE_CSS)
        # 行内逗号仍占一个字宽（0宽逗号 + 行尾自动消失的空格），行尾放不下时悬挂，不靠压缩字距。
        self.assertIn('.stk td.sig .cm,.stk td.sig .pr{margin-right:-.5em}', css)
        self.assertIn('.stk td.sig .pl{margin-left:-.5em}', css)
        self.assertNotIn('word-spacing', re.search(r'\.stk td\.sig \.cm[^{]*\{[^}]*\}', css)[0])
        # 汉字/数字字距保持正常：描述正文不再有负字距
        self.assertNotRegex(css, r'\.stk td\.sig \.signal-item[^{]*\{[^}]*letter-spacing:-')
        self.assertIn('--sig-col-gap:16px', css)

    def test_description_center_line_is_1px_gray_with_wider_combo_gap(self):
        """2026-11-14：左右分区中线用 1px 灰竖线显示；列距 10→16px，组合加号两边留出空隙。
        线画在 .sig-cols 背景层，不占 grid 格、不改两列宽度，也不新增颜色或窄屏断点。"""
        css = strip_css_comments(page_assets.PAGE_CSS)
        center = re.search(r'\.sig-cols\{([^}]*)\}', css).group(1)
        self.assertIn('background:linear-gradient(var(--section-line),var(--section-line)) '
                      'center/1px 100% no-repeat', center)
        self.assertIn('--section-line:rgba(139,147,161,.32)', css)
        self.assertEqual(css.count('center/1px 100% no-repeat'), 1)
        self.assertIn('--sig-col-gap:16px', css)
        self.assertNotIn('--sig-col-gap:10px', css)
        # 桌面加号 10.5px、手机 11.3px；16px 列距下两侧各留 2.75 / 2.35px，不再贴成一句话。
        desk = float(re.search(r'--combo-size:(\d+(?:\.\d+)?)px', css).group(1))
        mob = float(re.search(r'\.sig-cols\[data-combo="1"\]\{--combo-size:(\d+(?:\.\d+)?)px', css).group(1))
        for size in (desk, mob):
            self.assertGreaterEqual((16 - size) / 2, 2)
        # 桌面／手机描述区与财报同为 15px；行距回到字号应有的比例，不同指标只在行距外多 4px。
        self.assertIn('font-size:15px;line-height:21.75px', css)
        self.assertIn('td.sig{padding:var(--sig-row-gap) 5px 7px;font-size:15px;line-height:20.25px}', css)
        self.assertIn('.earn{display:block;margin:0;font-size:15px;font-weight:400;line-height:19px', css)
        self.assertIn('--sig-row-gap:4px', css)
        self.assertNotIn('--sig-row-gap:7px', css)
        self.assertAlmostEqual(21.75 / 15, 1.45, places=2)
        self.assertAlmostEqual(20.25 / 15, 1.35, places=2)
        # 规则模板说明跟主表 tag 用同一套带方向写法「需+X%／需-X%」，不能只改一处。
        rules = "".join(part for _, part in page_assets.RULE_SECTIONS)
        self.assertIn('「加仓价 N · 需+X%／需-X%」', rules)
        self.assertIn('「减仓价 N · 需+X%／需-X%」', rules)
        self.assertNotIn('需±X%', rules)
        self.assertNotIn('距 X%', rules)

    def test_stat_members_mobile_font_15px_indicator_16px_frame_84px(self):
        """2026-11-15：统计面板清单分三档——描述文字 16→15px、代码保持 16px、
        指标名称（量比/均线/布林+RSI）15→16px；指标名称按钮外框 74→84px，高度不变。
        桌面档 13/12px、个股描述区 15px 一律不动，行高也不动。"""
        css = strip_css_comments(page_assets.PAGE_CSS)
        mobile = css[css.index('@media(max-width:600px){'):css.index('@media(min-width:601px){')]
        # 代码仍是 16px，描述文字从 16px 回落到 15px（行高一律不动）。
        self.assertIn('#signalCounts .signal-member-symbol{font-size:16px;line-height:19.5px}', mobile)
        self.assertIn('#signalCounts .signal-member-metric{font-size:15px;line-height:16.88px}', mobile)
        for selector in ('#signalCounts .signal-filter-detail', '#signalCounts .signal-filter-condition',
                         '#signalCounts .earnings-week-meta', '#signalCounts .signal-member-direction'):
            self.assertIn(selector + '{font-size:15px;line-height:20.25px}', mobile)
        # 描述文字不许残留 14.5px 或 16px；代码不许回落到 15px。
        self.assertNotIn('#signalCounts .signal-member-metric{font-size:14.5px', css)
        self.assertNotIn('#signalCounts .signal-member-metric{font-size:16px', css)
        self.assertNotIn('#signalCounts .signal-member-symbol{font-size:15px', css)
        self.assertIn('#signalCounts .signal-member-jump .note{font-size:14px;line-height:18px}', mobile)
        self.assertIn('#signalCounts .signal-member-belong{font-size:14px;line-height:18px}', mobile)
        # 指标名称按钮：文字 15→16px，外框 +10px，高度 38px / 表格行 51px / summary 50px 全不动。
        self.assertIn('#signalCounts td .signal-filter-toggle{padding:0 4px;font-size:16px;line-height:15.95px}', mobile)
        self.assertIn('#signalCounts .signal-extra-row>.signal-filter-toggle{font-size:16px;line-height:16.68px}', mobile)
        self.assertIn('#signalCounts td .signal-filter-toggle{width:84px;padding:0 4px}', css)
        self.assertNotIn('#signalCounts td .signal-filter-toggle{width:74px', css)
        # 同组胶囊（紫灯/财报/盈亏到价）共用 --chip-w，两档同步 +10px，不许 84/74 混排。
        self.assertIn('#signalCounts .signal-extra-row{--chip-w:84px;--xr:12px;', css)
        self.assertIn('#signalCounts .signal-extra-row{--chip-w:84px;--xr:8px;padding:0}', mobile)
        self.assertNotIn('--chip-w:74px', css)
        self.assertIn('#signalCounts .signal-filter-toggle{box-sizing:border-box;height:38px;', css)
        self.assertIn('#signalCounts tr:not(.signal-members-row)>td{height:51px}', css)
        self.assertIn('min-height:50px;padding:14px var(--xr) 14px calc(var(--chip-w) + 17px)', css)
        # 桌面档指标名称仍是 14px，个股描述区与财报依旧 15px，不能被统计面板的字号带跑。
        self.assertIn('#signalCounts .signal-filter-toggle{box-sizing:border-box;height:38px;max-width:100%;'
                      'padding:0 7px;font:inherit;font-size:14px;', css)
        self.assertIn('td.sig{padding:var(--sig-row-gap) 5px 7px;font-size:15px;line-height:20.25px}', mobile)
        self.assertIn('table.stk tr[data-symbol]>td.sig{grid-area:sig;display:block;padding:var(--sig-row-gap) 10px 7px;'
                      'font-size:15px;line-height:21.75px', css)

    def test_lamp_table_has_only_horizontal_rules_and_five_character_cells_fit(self):
        css = strip_css_comments(page_assets.RULES_CSS)
        table = re.search(r'#rules-lamp-table\{([^}]*)\}', css)[1]
        self.assertIn('border:0', table)
        self.assertIn('border-collapse:collapse', table)
        cell = re.search(r'#rules-lamp-table th,\.card \.rules-body #rules-lamp-table td\{([^}]*)\}', css)[1]
        self.assertIn('border:0;border-bottom:1px solid var(--rules-cell-border)', cell)
        mobile = css[css.index('@media(max-width:600px){.card .rules-body'):]
        self.assertIn('#rules-lamp-table col[span]{width:86px}', mobile)   # 5个15px字 + 内边距
        self.assertIn('#rules-lamp-table td.cell-gray-fold{white-space:nowrap}', mobile)

    def test_settings_mobile_text_never_below_monitor_toggle_and_tools_share_row(self):
        source = (ROOT / 'edit.html').read_text(encoding='utf-8')
        self.assertNotIn('手机上也要改', source)
        # 顶栏只有回看板；清除本机令牌在定投提醒下面；原说明句已删除
        self.assertRegex(source, r'<div class="sub top-bar">\s*<a class="back-board" href="\./index\.html">← 回看板</a>\s*</div>')
        self.assertRegex(source, r'<div class="row token-tools">\s*<button id="btnWipe" class="danger">清除本机令牌</button>\s*</div>')
        self.assertNotIn('自动重跑看板', source.split('<script', 1)[0].split('<h1>', 1)[1])
        self.assertNotIn('btnLink', source)       # 手机用的带令牌链接已删除
        self.assertNotIn('importCard', source)    # CSV/EBK 导入已删除
        self.assertNotIn('list-import', source)
        self.assertIn('.token-tools button{white-space:nowrap}', source)
        mobile = source[source.rindex('@media(max-width:600px){\n.sub,'):]
        for selector in ('.sub', '.note', '.monitor-note', '.pager', '.subsec', 'kbd'):
            self.assertIn(selector, mobile.split('}', 1)[0])
        self.assertIn('font-size:13px', mobile)
        for fragment in re.findall(r'font-size:(\d+(?:\.\d+)?)px', source.split('<body>', 1)[1]):
            self.assertGreaterEqual(float(fragment), 13, '正文内联字号不得小于监测开启按钮的13px')

    def test_group_single_open_switch_is_off_by_default_and_wired_on_both_pages(self):
        self.assertEqual(rules_config.DEFAULT_SETTINGS['group_single_open'], 0)
        source = (ROOT / 'edit.html').read_text(encoding='utf-8')
        self.assertIn('id="set_group_single_open"', source)
        self.assertIn("document.querySelectorAll('.edit-group[open]').forEach(o=>{if(o!==d)o.open=false;});", source)
        js = page_assets.PAGE_INTERACTION_JS
        self.assertIn('window.MM_SETTINGS.group_single_open === 1', js)
        self.assertNotIn('groupSummaryClickAt', js)   # 不再用 toggle 监听（会干扰统计面板的筛选归一化时序）
        # 当日/风险/统计不是 group_ 前缀，不参与；2026-11-19 起「仓位」不再常开锁定，与普通分组同待遇。
        self.assertIn("details[id^='group_'][open]", js)
        self.assertNotIn('data-open-locked', js)
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ev={};class Details{constructor(id,open){this.id=id;this.open=open;this.dataset={};this.top=100;}
  getBoundingClientRect(){return {top:this.top};}}
const mk=(id,open)=>new Details(id,open);
const cards=[mk('group_positions',true),mk('group_positions_long',true),mk('group_positions_short',true),mk('group_focus',false),mk('group_tech',true)];
class Element{constructor(card){this.card=card;}
  closest(q){return q==="details[id^='group_'] > summary"&&this.card?{parentElement:this.card}:null;}}
const sel=s=>s==="details[id^='group_'][open]"?cards.filter(c=>c.open):[];
const frames=[];
const ctx={HTMLDetailsElement:Details,Element,Date,requestAnimationFrame:f=>frames.push(f),
  document:{addEventListener:(k,f)=>(ev[k]||=[]).push(f),querySelectorAll:sel,querySelector:()=>null,getElementById:()=>null},
  window:{innerHeight:600,MM_SETTINGS:{group_single_open:1},addEventListener(){},scrollBy:(x,y)=>{if(typeof x==='object')ctx.smooth=x.top;else ctx.scrolled=y;}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(0,'utf8'),ctx);
const click=(card)=>{ev.click.forEach(f=>f({target:new Element(card)}));};
const flush=()=>{while(frames.length)frames.shift()();};
const focus=cards[3];
// 用户点分组标题并展开：其它分组（含长/短持仓）都收起；2026-11-20 定稿「仓位」豁免，保持展开
click(focus);focus.open=true;focus.top=40;flush();
assert.equal(cards[1].open,false);assert.equal(cards[2].open,false);assert.equal(cards[4].open,false);
assert.equal(cards[0].open,true);assert.equal(focus.open,true);
assert.equal(ctx.scrolled,-60);     // 点开的这组被上拽后拉回原位（top 100→40 补 -60）
assert.equal(ctx.smooth,-13);       // 再平滑滚到 8.75% 高度线：屏高600→线在 round(52.5)=53，当前top 40 → top-线 = -13
// 点「仓位」标题展开：与普通分组一样，其它分组收起
cards[0].open=true;cards[4].open=true;click(cards[0]);flush();assert.equal(cards[4].open,false);
// 点击后其实是在收起：不动其它
cards[4].open=true;focus.open=false;click(focus);flush();assert.equal(cards[4].open,true);
// 开关关闭：不动
focus.open=true;ctx.window.MM_SETTINGS.group_single_open=0;click(focus);flush();assert.equal(cards[4].open,true);
'''
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/run_all.py"), "--offline-node", "-e", script],
                                cwd=ROOT, input=js, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stat_find_box_header_abbreviation_and_status_button_label(self):
        # 代码定位在顶部：站名同一排、靠右；统计栏头部不再有它；财报计数缩写为「报」
        card = monitor._signal_counts_card({'summary': {'red': 1, 'yellow': 2}, 'signal_counts': {}})
        header = card.split('</summary>', 1)[0]
        self.assertNotIn('stat-find', header)
        page = self.render()
        brandbar = re.search(r'<div class="brandbar">(.*?)</div>', page, re.S)[1]
        self.assertLess(brandbar.index('<h1 class="brand">Matrix</h1>'), brandbar.index('class="stat-find"'))
        self.assertIn('id="statFindInput" type="text" maxlength="5"', brandbar)
        self.assertIn('.stat-find input{box-sizing:border-box;width:74px;', page_assets.PAGE_CSS)   # 5 位美股代码墨宽
        self.assertIn('<button type="button" id="statFindBtn">FIND</button>', brandbar)
        self.assertRegex(header, r'<span class="count-total count-earn">报 [—0-9]+</span>')
        # 2026-11-20：标题栏最右是「总 N」＝数据那里抓取的总只数（含没取到的）
        self.assertRegex(header, r'<span class="count-total count-all">总 [—0-9]+</span></span>$')
        full = monitor._signal_counts_card({'summary': {'red': 1, 'yellow': 2, 'total': 99}, 'signal_counts': {}}).split('</summary>', 1)[0]
        self.assertIn('<span class="count-total count-all">总 99</span>', full)
        self.assertIn('#signalCounts>summary .count-all{margin-left:auto}', page_assets.PAGE_CSS)   # 总 N 靠右
        self.assertNotIn('财报 ', header)
        js = page_assets.PAGE_INTERACTION_JS
        self.assertIn('function findStock()', js)
        self.assertIn('target.closest(".stat-find")', js)      # 点 FIND 不触发统计卡「点标题归一化」
        css = page_assets.PAGE_CSS
        self.assertIn('.stat-find{display:inline-flex;align-items:center;gap:6px;flex:none;margin-left:auto}', css)
        self.assertIn('.brandbar h1.brand{margin:0;font-size:32px;line-height:39px;text-align:left;white-space:nowrap}', css)   # 2026-11-20：32px 锁 39px 行盒，FIND 不下移
        self.assertIn('.brandbar{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin:18px 0 4px}', css)   # 搜索与站名底边对齐
        self.assertIn('input.value = "";', js)                 # 点 FIND 后清空输入框
        self.assertIn('background:#1e232d;border:1px solid var(--line)', css)
        self.assertRegex(css, r'\.stat-find input\{[^}]*font-size:16px')   # 16px：iOS 聚焦不自动放大页面
        status = (ROOT / 'run-status.js').read_text(encoding='utf-8')
        self.assertIn("b.textContent = '核对';", status)
        self.assertNotIn('核对中', status)

    def test_settings_expandables_have_a_rotating_chevron(self):
        source = (ROOT / 'edit.html').read_text(encoding='utf-8')
        self.assertIn("details.fold>summary::before,details.set>summary::before{content:'›'", source)
        self.assertIn("details.fold[open]>summary::before,details.set[open]>summary::before{transform:rotate(90deg)}", source)

    def test_single_add_entry_replaces_per_group_add_forms(self):
        source = (ROOT / 'edit.html').read_text(encoding='utf-8')
        self.assertEqual(source.count('class="card add-entry"'), 1)
        for ident in ('newSym', 'newNote', 'newMove', 'btnAddSym'):
            self.assertEqual(source.count('id="' + ident + '"'), 1)
        self.assertNotIn('class="add row"', source)
        self.assertNotIn('add-prices', source)
        self.assertNotRegex(source, r"newSym_|newReduce_|newTarget_|newTrig_|'add_'")
        self.assertIn('.add-entry .add-row{display:grid;grid-template-columns:76px minmax(0,1fr) 104px 52px;', source)

    def test_positions_full_width_rows_preserve_title_inset(self):
        css = page_assets.PAGE_CSS
        parent = re.search(r'#group_positions,#group_positions\[open\]\{([^}]+)\}', css)[1]
        self.assertIn('padding:3px 0 2px', parent)
        self.assertIn('#group_positions>summary{cursor:pointer;margin-left:5px;margin-right:5px}', css)
        cost = strip_css_comments((ROOT / 'cost_panel.css').read_text(encoding='utf-8'))
        self.assertIn('margin: 8px 0 4px', re.search(r'#group_positions \.asset-row\s*\{([^}]*)\}', cost)[1])
        self.assertIn('margin: 4px 0 0', re.search(r'#group_positions \.cashbond-row\s*\{([^}]*)\}', cost)[1])
        # 2026-11-20：并排双卡沿用 .dual-row 的 100% 宽，不再有自己的 width / 左右负边距。
        zone_head = re.search(r'#zoneDualRow\{([^}]+)\}', css)[1]
        self.assertIn('--collapse-bg:var(--positions-collapse)', zone_head)
        self.assertIn('--zone-name-fs:14px', zone_head)
        # 21:2x：仓位栏标题「$总资产 -$盈亏(-x%)」与长短持仓标题「-4.7%」同号，
        # 涨跌额去粗；两档只靠 --pos-num-fs 一个变量，不再各写各的。
        self.assertIn('--pos-num-fs:14px', css)
        self.assertIn('--pos-num-fs:16px', css)
        self.assertIn('.zone-r2 .zone-amt{font-size:var(--pos-num-fs,14px);font-weight:400;', css)
        self.assertIn('.zone-r2 .zone-pct{font-size:var(--pos-num-fs,14px);', css)
        self.assertIn('font-size: var(--pos-num-fs,14px); line-height: 24px;', cost)
        self.assertNotIn('#group_positions_long.zone-card', css)
        self.assertIn('.dual-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))', css)

    def test_risk_state_text_follows_lamp_and_stripes_widen_2026_11_21(self):
        """2026-11-21：风险表状态列文字跟红黄灯变色；风险指标名与主表个股代码左边灯条各 +2px。"""
        css = page_assets.PAGE_CSS
        self.assertIn(':is(#macroCard, .macro-scope) tr.red td:nth-child(4){color:var(--lamp-red)}', css)
        self.assertIn(':is(#macroCard, .macro-scope) tr.yellow td:nth-child(4){color:var(--lamp-yellow)}', css)
        self.assertIn('tr.red td:first-child::before{background:var(--red);width:3.5px}', css)
        self.assertIn('tr.yellow td:first-child::before{background:var(--yellow);width:3.5px}', css)
        self.assertIn('tr.green td:first-child::before{background:var(--green);width:3px}', css)
        self.assertIn('table.stk tr[data-symbol]::before{content:"";position:absolute;left:0;top:var(--lamp-top);width:4px;', css)
        self.assertNotIn('top:var(--lamp-top);width:2px;', css)

    def test_group_dual_colors_spacing_and_float_fold_2026_11_21(self):
        """2026-11-21：标题栏红黄数字＝个股描述区灯色；其它分组外框回紫；
        两排行距拉开但框高不变；▲ 悬浮吸底，且只管仓位以下的分组。"""
        css = page_assets.PAGE_CSS
        # 红黄数字与个股描述文字同源（.signal-item 用的就是 --lamp-red / --lamp-yellow）
        self.assertIn('.stat-badge.red{color:var(--lamp-red)}', css)
        self.assertIn('.stat-badge.yellow{color:var(--lamp-yellow)}', css)
        self.assertIn('#signalCounts .count-red{color:var(--lamp-red)}', css)
        self.assertIn('#signalCounts .count-yellow{color:var(--lamp-yellow)}', css)
        self.assertIn('.signal-item[data-signal-level="red"]{color:var(--lamp-red)}', css)
        self.assertIn('.signal-item[data-signal-level="yellow"]{color:var(--lamp-yellow)}', css)
        # 其它分组展开框 / 清单框回到原来的紫；绿框仍只给仓位与长短持仓
        self.assertIn('.dual-row.group-dual > details[open]{border:var(--frame-w) solid var(--signal-border)}', css)
        self.assertIn('border:var(--frame-w) solid var(--signal-border);border-radius:12px}', css)
        self.assertIn('#zoneDualRow > details[open]{border:var(--frame-w) solid var(--frame-green)}', css)
        self.assertIn('.dual-row.group-dual > details{border:1px solid var(--line)}', css)
        # 行距 +4px，标题框仍锁 47px
        self.assertIn('margin-bottom:4px', re.search(r'\.zone-r1\{([^}]+)\}', css)[1])
        self.assertIn('height:47px;min-height:47px', css)
        # ▲ 条件式悬浮：只有长清单够不着时按钮才吸底，滚到分组底部还原成原来的清单底栏
        self.assertIn('#zoneDualRow .group-bottom,\n.dual-row.group-dual .group-bottom{min-height:26px}', css)
        self.assertIn('#zoneDualRow .collapse-group.is-floating,\n.dual-row.group-dual .collapse-group.is-floating{position:fixed;', css)
        # 2026-11-21：上边缘 vh-46 不变，下边缘往下 8px、同时加高 8px（bottom 12→4／height 34→42）
        self.assertIn('bottom:calc(4px + env(safe-area-inset-bottom,0px))', css)
        self.assertNotIn('bottom:calc(12px + env(safe-area-inset-bottom,0px))', css)
        self.assertIn('height:42px;margin:0;transform:translateX(-50%)', css)
        self.assertNotIn('height:34px;margin:0;transform:translateX(-50%)', css)
        # 悬浮态不能再写死 96px：宽度由 JS 量取清单底部那条 ▲ 的宽度，两态完全一致
        self.assertIn('width:var(--collapse-float-w,96px)', css)
        self.assertNotIn('width:96px;height:34px', css)
        # 固定态仍是线上原色；悬浮态底色压暗一档、紫绿两色统一盖住 70%（.7），
        # 暗 ≠ 更透；「再暗一些」只等比压三元组到 75%，透明度 .7 一个字不动；
        # 描边与分组展开那一圈完全同款：同一变量、同一线宽、实色不带透明度
        self.assertIn('--collapse-bg:rgba(var(--frame-purple-rgb),.2)', css)
        self.assertIn('--collapse-bg:var(--positions-collapse)', css)
        self.assertIn('--collapse-float-purple-rgb:60,38,96', css)
        self.assertIn('--collapse-float-green-rgb:15,53,23', css)
        self.assertIn('--collapse-float-purple:rgba(var(--collapse-float-purple-rgb),.7)', css)
        self.assertIn('--collapse-float-green:rgba(var(--collapse-float-green-rgb),.7)', css)
        self.assertNotIn('--collapse-float-purple-rgb:80,50,128', css)
        self.assertNotIn('--collapse-float-green-rgb:20,70,30', css)
        self.assertNotIn('--collapse-float-purple:rgba(var(--collapse-float-purple-rgb),.55)', css)
        self.assertNotIn('--collapse-float-green:rgba(var(--collapse-float-green-rgb),.5)', css)
        self.assertNotIn('--collapse-float-purple:rgba(var(--frame-purple-rgb)', css)
        self.assertIn('background:var(--collapse-float-bg,var(--collapse-bg));box-shadow:0 4px 16px rgba(0,0,0,.65)', css)
        self.assertIn('.collapse-group.is-floating:hover{color:var(--text);background:var(--collapse-float-bg,var(--collapse-bg))}', css)
        self.assertNotIn('background:color-mix(in srgb,var(--collapse-bg)', css)
        self.assertNotIn('background:var(--signal-bg);box-shadow', css)
        self.assertIn('#zoneDualRow .collapse-group.is-floating{--collapse-float-bg:var(--collapse-float-green);border:var(--frame-w) solid var(--frame-green)}', css)
        self.assertIn('.dual-row.group-dual .collapse-group.is-floating{--collapse-float-bg:var(--collapse-float-purple);border:var(--frame-w) solid var(--signal-border)}', css)
        self.assertNotIn('border:2px solid var(--frame-green)', css)
        self.assertNotIn('.is-floating{border:1px solid', css)
        self.assertNotIn('.group-bottom{position:fixed;', css)
        self.assertNotIn('#signalCounts .group-bottom{position:fixed', css)
        js = page_assets.PAGE_INTERACTION_JS
        self.assertIn("classList.toggle('is-floating'", js)
        self.assertIn("btn.style.setProperty('--collapse-float-w'", js)
        self.assertIn('wrapRect.width.toFixed(1)', js)
        # 2026-11-21 定稿：全站只认一条固定参考线＝长持仓「第一只与第二只个股之间的
        # 分隔线」到「长持仓标题栏下边缘」的距离（DEMO 六档实测 177.75~180.25，写死 180）。
        # 按钮上沿（vh-46）碰到「标题栏下边缘 + 180」就浮、越过就收；不再逐组动态量
        # 个股分隔线（有的分组第一条分隔线离标题很远／很近，会飘），也不再与标题栏重叠。
        # 此前「隔两个个股明细」「贴着标题栏底边」「按本组分隔线实时量」三版全部作废。
        self.assertIn('const FIXED_GAP = 180', js)
        self.assertIn('const floatTop = vh - 46', js)
        self.assertIn("const headId = btn.getAttribute('aria-controls')", js)
        self.assertIn('const sepY = headBottom === null ? null : headBottom + FIXED_GAP', js)
        self.assertIn('sepY <= floatTop', js)
        self.assertIn('passedSep && naturalTop > floatTop', js)
        self.assertNotIn("panel.querySelectorAll('tr[data-symbol]')", js)
        self.assertNotIn('rows[0].getBoundingClientRect().bottom', js)
        self.assertNotIn('rows[1].getBoundingClientRect().top', js)
        self.assertNotIn('headBottom <= floatTop', js)
        self.assertNotIn('headBottom <= gap', js)
        self.assertNotIn('headBottom <= 0', js)
        self.assertNotIn('headTop <= 0', js)
        self.assertNotIn('Math.min(2, rows.length)', js)
        self.assertNotIn('const gap = 2 * (rowH > 0 ? rowH : 60)', js)
        self.assertIn("window.addEventListener('scroll', schedule", js)
        self.assertIn("document.addEventListener('toggle', schedule, true)", js)

    def test_mobile_settings_two_row_fixture_preserves_all_fields_and_dividers(self):
        source = (ROOT / 'edit.html').read_text(encoding='utf-8')
        # 每股单排：列标题和每行共用同一组轨道；价格容器display:contents不增排；代码不再是胶囊框。
        self.assertIn('.edit-group .stock-head,.edit-group .stock-row{display:grid;'
                      'grid-template-columns:54px 100px repeat(var(--price-fields),minmax(0,1fr)) 56px 26px;', source)
        self.assertIn('.edit-group .stock-row~.stock-row{border-top:1px solid var(--stock-divider)}', source)
        self.assertIn('.stock-prices{display:contents}', source)
        self.assertNotIn('grid-row:2;display:grid;', source)
        self.assertIn('.edit-group .stock-row>.sym{width:auto;min-width:0;padding:0;border:0;border-radius:0;background:none;', source)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/run_all.py'), '--offline-node',
                                 str(ROOT / 'tests/mm_test_edit.js'), '--layout-fixture'],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        fixture = json.loads(result.stdout)

        class Rows(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows, self.row, self.depth, self.price_depth = [], None, 0, None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'div' and 'stock-row' in attrs.get('class', '').split():
                    self.row = {'group': attrs['data-k'], 'symbol': attrs['data-s'], 'first': [], 'prices': [], 'move': 0, 'delete': 0}
                    self.rows.append(self.row)
                    self.depth = 0
                if self.row is None:
                    return
                if tag == 'div':
                    self.depth += 1
                    if attrs.get('class') == 'stock-prices':
                        self.price_depth = self.depth
                        self.row['count'] = int(attrs['style'].split(':')[1])
                if tag == 'input':
                    self.row['prices' if self.price_depth is not None else 'first'].append(attrs['data-f'])
                if tag == 'select' and attrs.get('class') == 'move':
                    self.row['move'] += 1
                if tag == 'button' and attrs.get('class') == 'del':
                    self.row['delete'] += 1
            def handle_endtag(self, tag):
                if tag == 'div' and self.row is not None:
                    if self.depth == self.price_depth:
                        self.price_depth = None
                    self.depth -= 1
                    if self.depth == 0:
                        self.row = None
        parser = Rows()
        parser.feed(fixture['html'])
        groups = json.loads((ROOT / 'groups.json').read_text(encoding='utf-8'))
        self.assertEqual(len(parser.rows), len(groups) * 2)
        self.assertEqual(fixture['html'].count('class="stock-head"'), len(groups))   # 每组列表上方一条列标题
        for g in groups:
            rows = [r for r in parser.rows if r['group'] == g['key']]
            self.assertEqual(len(rows), 2)
            fields = ['trigger', 'reduce', 'target'] if g['key'] == 'positions' else ['trigger', 'target'] if g['key'] == 'focus' else ['trigger']
            for row in rows:
                with self.subTest(group=g['key'], symbol=row['symbol']):
                    self.assertEqual(row['first'], ['sym', 'note'])   # 2026-11-18：代码列改为可直接修改的输入框
                    self.assertEqual(row['prices'], fields)
                    self.assertEqual(row['count'], len(fields))
                    self.assertEqual((row['move'], row['delete']), (1, 1))
        self.assertNotIn('<script', fixture['html'])
        self.assertNotIn('mm_gh_token_v1', fixture['html'])

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
        self.assertLess(match.start(), page.index('function nearPrice('))
        self.assertEqual(page.count("window.__MM_RULES__="), 1)
        rules = re.search(r'<template id="rulesTpl">(.*?)</template>', page, re.S)[1]
        self.assertIn("相差≤3% → 点亮橙色", plain_text(rules))
        self.assertIn("相差≤3% → 点亮青色", plain_text(rules))
        self.assertNotIn('可点红灯', plain_text(rules))
        self.assertNotIn("fund_", match[1])
        self.assertIn('window.MM_SETTINGS={"signal_single_open":0,"group_single_open":0}', page)

    def test_render_does_not_use_import_time_rules_snapshot(self):
        first = self.render()
        monitor.S.update(trigger_gap_pct=9, reduce_gap_pct=0, rsi_low=28, rsi_high=82)
        with patch.object(monitor, "RULES_TEMPLATE", "过期模板不能进入页面"):
            second = self.render()
        self.assertNotEqual(first, second)
        self.assertNotIn("过期模板不能进入页面", second)
        rules = re.search(r'<template id="rulesTpl">(.*?)</template>', second, re.S)[1]
        glossary = re.search(r'<template id="glossaryTpl">(.*?)</template>', second, re.S)[1]
        self.assertIn("相差≤3% → 点亮橙色", plain_text(rules))
        self.assertIn("相差≤3% → 点亮青色", plain_text(rules))
        self.assertIn("≥82超买，≤28超卖", plain_text(glossary))
        self.assertNotIn("≥70超买，≤35超卖", plain_text(glossary))

    def test_add_and_reduce_are_resident_notes_not_trade_lamps(self):
        # 终稿统一现价分母±3%；旧配置保留读兼容，但不再绑定已读/红灯功能。
        monitor.S.update(trigger_gap_pct=7, reduce_gap_pct=2)
        for group in ('position', 'focus', 'technology'):
            row = {'symbol':'PRICE', 'group':group, 'price':100, 'chg':0,
                   'level':'green', 'trigger':97, 'reduce':103, 'signals':[], 'signal_marks':[]}
            out = monitor._signals_html(row)
            self.assertIn('class="lvtag add signal-item" data-lv="97" data-near="1"', out)
            self.assertEqual('class="lvtag cut signal-item"' in out, group == 'position')
            self.assertNotIn('data-signal-level="red"', out)
            self.assertEqual(row['level'], 'green')
        for value, near in ((96.999, '0'), (97, '1'), (103, '1'), (103.001, '0')):
            self.assertIn(f'data-near="{near}"', monitor.level_note(value, None, 100))
        text = plain_text(page_assets.rules_body(monitor.S, monitor._rule_constants()))
        for term in ('加仓价 · 所有分组', '减仓价 · 持仓分组', '相差≤3% → 点亮橙色',
                     '相差≤3% → 点亮青色', '减仓价：重点、其他分组不适用'):
            self.assertIn(term, text)
        self.assertNotRegex(text, '已读|可点红灯')

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
                            ('涨跌幅 · 自适应', [(' colspan="3"', '红灯／黄灯')])):
                        with self.subTest(source=label, condition=condition, settings=settings):
                            rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', table, re.S)
                                    if '<td' in row and plain_text(re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1]) == condition]
                            self.assertEqual(len(rows), 1)
                            cells = [(attrs, plain_text(body)) for attrs, body in
                                     re.findall(r'<td\b([^>]*)>(.*?)</td>', rows[0], re.S)]
                            self.assertEqual(cells, [('', condition)] + expected)
                    self.assertIn('text-align:center', page_assets.RULES_CSS)

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
        self.assertIn('.signal-item[data-signal-level="yellow"]{color:var(--lamp-yellow)}', page_assets.PAGE_CSS)
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
            '显示倒计时以快照北京运行日为基准，不随打开页面日期漂移。',
            '例：已记录美东20日财报，正常缓存未到期，22日起可提前刷新。',
        ):
            self.assertEqual(text.count(phrase), 1)
        self.assertNotIn('21日不因晨星更新而重抓', text)
        self.assertNotIn('晨星新价不代表', text)
        self.assertIn('不抓晨星', text)
        self.assertIn('Cboe官方VIX日线优先', text)
        self.assertIn('标的去重与显示优先级', text)
        self.assertNotRegex(markup, r'\$\{\w+\}')
        self.assertEqual(markup.count('<table '), 9)  # 一项阈值、三项连续示例、五项抓取表。
        for ident in ('glossary-chg-table', 'glossary-vol-table', 'fetch-reference'):
            self.assertEqual(markup.count(f'id="{ident}"'), 1)
        self.assertEqual(re.search(r'<section id="glossary-calculation">(.*?)</section>', markup, re.S)[1].count('<table '), 4)
        self.assertEqual(re.search(r'<section id="glossary-fetch">(.*?)</section>', markup, re.S)[1].count('<table '), 5)

    def test_home_rules_explain_cost_membership_and_boll_upper_without_jargon(self):
        text = page_assets.rules_body(monitor.S, monitor._rule_constants())
        glossary = page_assets.glossary_body(monitor.S, monitor._rule_constants())
        rules_text, glossary_text = plain_text(text), plain_text(glossary)
        self.assertIn('单独RSI', rules_text)
        self.assertRegex(rules_text, r'RSI 3条 ↓超卖\s*红灯')
        self.assertIn('重点、其他分组不适用', rules_text)
        self.assertNotIn('只撤页面追加的该项红灯，不撤生成红灯或其他原因的灯', glossary_text)
        self.assertNotRegex(rules_text + glossary_text, '统计由对应红列转白列|白字删除线|浮亏10%／20%分档|可点红灯与已读')
        for explanation in ('双轨命中时，只选带符号距离较小的一轨', '越轨距离为负，相等选', '标的去重'):
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
                    terms = ('快照北京运行日', '盘前／盘后不平移', '另留前一日红字')
                for term in terms:
                    self.assertIn(term, text)
        plain_glossary = plain_text(glossary[1])
        for term in ('沿用美东财报日期对比快照北京运行日', '红字保留期结束后才检查下一次',
                     '失败时沿用有效旧日期', '名单含运行日至+15天', '待公布', '未知不猜钟点'):
            self.assertIn(term, plain_glossary)
        self.assertNotIn('href="#rule-earnings"', glossary[1])
        self.assertIn('沿用美东财报日期对比快照北京运行日', plain_glossary)
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
        self.assertIn('#signalCounts{--collapse-bg:var(--signal-collapse);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}', page_assets.PAGE_CSS)
        self.assertIn('#signalCounts .collapse-group{color:var(--white)}', page_assets.PAGE_CSS)
        zone = re.search(r'#zoneDualRow\{([^}]+)\}', page_assets.PAGE_CSS)[1]
        self.assertIn('--collapse-bg:var(--positions-collapse)', zone)
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
                self.assertIn('常规交易日等效分钟（已开盘分钟÷当日全日分钟×390）', glossary)
                self.assertIn('半日市按当日实际交易时长折算', glossary)
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
        self.assertNotIn('mm:ack-change', page_assets.PAGE_INTERACTION_JS)
        self.assertIn("北京时间", page_assets.rules_body(got, monitor._rule_constants()))
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        self.assertNotIn('set_fund_', source)
        self.assertNotIn('基本面', source)
        self.assertFalse(any(key.startswith("fund_") for key in
            json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))))

    def test_positions_parent_lock_and_cash_are_static_but_rs_remains_dynamic(self):
        page = self.render()
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = {}
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.tags[attrs['id']] = (tag, attrs)
        elements = Elements()
        elements.feed(page)
        tag, parent = elements.tags['group_positions']
        self.assertEqual(tag, 'details')
        # 2026-11-19 Demo：「仓位」默认收起、不再锁常开；长/短持仓是平级卡，同样默认收起
        self.assertNotIn('open', parent)
        self.assertNotIn('data-open-locked', parent)
        for ident, (_, attrs) in elements.tags.items():
            if ident != 'group_positions':
                self.assertNotIn('data-open-locked', attrs, ident)
        self.assertNotIn('open', elements.tags['group_positions_short'][1])
        self.assertIn('<span class="cb-name">Cash</span>', page)
        self.assertNotIn('<span class="cb-name">现金</span>', page)
        # 2026-11-12：持仓「20日对.SPX」已删除；标题行右侧改放总资产，现金行最左放长短持仓合计市值。
        cost = (ROOT / 'cost_panel.js').read_text(encoding='utf-8')
        self.assertIn("#group_positions>summary .group-stats .pos-total", cost)
        self.assertNotIn('REAL.rs20', cost)
        self.assertNotIn('renderRs', cost)
        self.assertNotIn('日对.SPX', cost)

    def test_positions_lock_blocks_parent_activation_not_child_collapse(self):
        # 2026-11-19 Demo：「仓位」父卡不再常开锁定，与其它分组一样点标题开合；常开脚本已整段删除。
        self.assertNotRegex(page_assets.PAGE_INTERACTION_JS, r'lockPositionsOpen')
        self.assertNotIn('data-open-locked', page_assets.PAGE_INTERACTION_JS)

    def test_quiet_list_reset_and_regular_collapse_still_execute(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const events={},windowEvents={};
class Details {}
const Node={DOCUMENT_POSITION_PRECEDING:2,DOCUMENT_POSITION_FOLLOWING:4,
  DOCUMENT_POSITION_CONTAINED_BY:16};
const context={HTMLDetailsElement:Details,Node,
  requestAnimationFrame:()=>0,   // 条件式吸底按钮的排帧调度，离线沙箱里只需登记不执行
  document:{querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener:(k,f)=>(events[k]||=[]).push(f)},
  window:{location:{hash:''},scrollY:1000,addEventListener:(k,f)=>(windowEvents[k]||=[]).push(f)}};
vm.createContext(context);vm.runInContext(fs.readFileSync(0,'utf8'),context);
const sub={open:true,style:{display:''},offsetHeight:1,closest:()=>null};
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
// 点底部 ▲：只收本组；拿「本组之后的第一个元素」当锚点把下方内容拉回原屏幕位置，
// 不再用 scrollIntoView 把整页拉到分组标题。
closed.getBoundingClientRect=()=>({bottom:closed.open?900:500});   // 本组底边：展开900 → 收起500
let scrolledBy=null;
context.window.scrollBy=(x,y)=>{scrolledBy=y;};
class Element {closest(s){return s==='.collapse-group'?{closest:()=>closed}:null;}}
context.Element=Element;
for(const f of clickEvents)f({target:new Element()});
assert.equal(closed.open,false);assert.equal(focused,true);
assert.equal(scrolled,false);      // 不再 scrollIntoView，页面不整体跳到标题
assert.equal(scrolledBy,-400);     // 底边被上拽 400，反向补回原位
// 短持仓是父卡里最后一个子框（没有下一个兄弟）：同一套以本组底边为锚的逻辑，不依赖兄弟/父级。
const shortZone=new Details();shortZone.open=true;shortZone.nextElementSibling=null;shortZone.parentElement={nextElementSibling:null};
shortZone.querySelector=()=>({focus:()=>{}});
shortZone.getBoundingClientRect=()=>({bottom:shortZone.open?2000:1350});
scrolledBy=null;
context.Element=class Element2 {closest(s){return s==='.collapse-group'?{closest:()=>shortZone}:null;}};
for(const f of clickEvents)f({target:new context.Element()});
assert.equal(shortZone.open,false);
assert.equal(scrolledBy,-650);     // 短持仓收起后下方内容同样原地不动
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
        for ident in ('setToggle',):
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
        # 产物里的 CSS 已剥掉注释（构建期压缩），对照时两边用同一口径
        self.assertIn(strip_css_comments(page_assets.PAGE_CSS.removesuffix(page_assets.RULES_CSS)), page)
        self.assertEqual(page.count(strip_css_comments(page_assets.RULES_CSS)), 1)
        cost_css, _ = monitor._cost_panel_assets()
        self.assertLess(page.index(strip_css_comments(cost_css)),
                        page.index(strip_css_comments(page_assets.RULES_CSS)))
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
                       '.runbar button', '.signal-filter-condition[data-signal-level=', '.signal-name-lamp{', '.grp-arrow',
                       '.arrow-rsi-up,.arrow-rsi-down', '.rule-table th.cat-col'):
            self.assertIn(active, css)
        self.assertEqual(css.count('table.stk tr.range52-row>td:first-child{position:relative;padding:0 10px 2px 10px;border:0;box-shadow:none}'), 1)
        self.assertEqual(css.count('table.stk tr.range52-row>td:first-child{padding:0 6px 2px 6px}'), 1)
        for phase in ('busy', 'partial', 'ok', 'bad'):
            self.assertIn(f'.runlight[data-phase="{phase}"]', css)

    def test_multi_condition_colors_are_scoped_and_keep_single_text_and_name_lamps(self):
        css = page_assets.PAGE_CSS
        palette = {'red': 'var(--lamp-red)', 'yellow': 'var(--lamp-yellow)', 'gray': 'var(--text)',
                   'purple': 'var(--purple)', 'green': 'var(--green)'}
        for level, color in palette.items():
            selector = f'#signalCounts .signal-filter-condition[data-signal-level="{level}"]'
            with self.subTest(level=level):
                self.assertEqual(css.count(selector + '{color:' + color + '}'), 1)
        # 财报日期不是交易灯：红黄继续，无预警恢复美东蓝；北京绝对日期跟随红黄或原灰。
        for level, color in (('red', 'var(--lamp-red)'), ('yellow', 'var(--lamp-yellow)'), ('gray', 'var(--head-blue-on)')):
            selector = ('#signalCounts .signal-filter-condition'
                        f'[data-signal-kind="earnings_week"][data-signal-level="{level}"]')
            bj_color = ';--earn-bj-color:' + color if level in ('red', 'yellow') else ''
            with self.subTest(level=level):
                self.assertEqual(css.count(selector + '{color:' + color + bj_color + '}'), 1)
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
                            for level in ('red', 'yellow', 'gray')} | {
                            '#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd',
                            '#signalCounts .signal-filter-condition[data-signal-kind="earnings_week"] .cd.tbd'})
        for single in (
                '#signalCounts .signal-members-panel .note{display:inline;margin-left:6px}',
                '#signalCounts .signal-member-direction{color:var(--dim);font-size:12px}',
                '#signalCounts .signal-member-belong{color:var(--dim);font-size:12px}',
                '#signalCounts .signal-member-metric{margin-left:5px;color:var(--text);font-size:13px}',
                # 名单里的描述性数值跟着这一条条件的等级走（红红字／黄黄字／白仍白字），与主表描述同一套配色
                '#signalCounts .signal-members-panel.tone-red .signal-member-metric{color:var(--lamp-red)}',
                '#signalCounts .signal-members-panel.tone-yellow .signal-member-metric{color:var(--lamp-yellow)}',
                '#signalCounts .signal-members-panel.tone-gray .signal-member-metric{color:var(--text)}',
                '#signalCounts .signal-member-jump{color:var(--text);text-decoration:none;border-bottom:1px dotted var(--list-border)}',
                '#signalCounts .signal-members-panel p{margin:4px 0;color:var(--text)}'):
            self.assertIn(single, css, '单选文字原有颜色、字号与间距不随多选改动')
        self.assertIn('#signalCounts .signal-name-lamp{content:"";display:inline-block;'
                      'width:3px;height:12px;border-radius:1px;background:var(--name-lamp,var(--dim));'
                      'margin-right:6px;vertical-align:-1px}', css)
        for selector in ('#signalCounts .signal-members-panel li:not(.signal-member-split)::before',
                         '#signalCounts .earnings-week li::before'):
            self.assertIn(selector + '{content:"";display:inline-block;width:3px;height:12px;border-radius:1px;'
                          'background:var(--name-lamp,var(--dim));margin-right:6px;vertical-align:-1px}', css)
        for level in ('red', 'yellow', 'green', 'purple'):
            self.assertRegex(css, re.escape(f'#signalCounts .signal-name-lamp.{level}')
                             + r'[^{}]*\{--name-lamp:var\(--' + level + r'\)\}')
        self.assertIn('#signalCounts .signal-name-lamp.gray{--name-lamp:var(--range-track)}', css)
        self.assertIn('--range-track:#9fa3aa', css)

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
        for name in ("run-status.js",):
            self.assertIn(page_assets.asset_url(ROOT, name), source)
        self.assertNotRegex(source, r'\.js\?v=2026\d{4}(?:-\d+)?"')

    def test_editor_hash_refresh_is_limited_to_generated_parts(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("run-status.js",):
                (Path(tmp) / name).write_bytes((ROOT / name).read_bytes() + b"\n/* offline */")
            result = page_assets.sync_edit_html(source, tmp, monitor._rule_constants())
            self.assertNotEqual(source, result)
            for name in ("run-status.js",):
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
result.retiredBindings=!/GAP_TRIGGER|GAP_REDUCE|var RULES=window.__MM_RULES__/.test(cost);
result.fixedNear=/nearPrice\(r\.price, lv, 3\)/.test(cost);
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
        self.assertTrue(got['retiredBindings'])
        self.assertTrue(got['fixedNear'])
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


class ConsensusSanityRegression(unittest.TestCase):
    """2026-11-23 闪迪拆股：缓存里还是拆股前的共识价 2047.38，现价只有 79 → 页面曾显示 +2483.8%。
    共识价与现价差 6 倍以上＝不可信：页面写「待核实」、不算空间/达到，并插队重抓。"""

    def test_implausible_ratio_both_directions_and_missing_data(self):
        f = monitor.consensus_implausible
        self.assertTrue(f(2047.38, 79.24))        # 拆股前的旧共识价
        self.assertTrue(f(10, 100))               # 反方向（合股）也算
        self.assertFalse(f(248.15, 79.24))        # 3.1 倍：真实的高空间，照常显示
        self.assertFalse(f(5.9 * 100, 100))
        self.assertTrue(f(6 * 100, 100))          # 边界：恰好 6 倍就算
        self.assertFalse(f(None, 79.24))
        self.assertFalse(f(248.15, None))
        self.assertFalse(f(0, 79.24))

    def test_evaluate_target_marks_suspect_and_renders_pending_text(self):
        t = monitor.evaluate_target(79.24, 94.4, {"kind": "consensus", "price": 2047.38, "month": 11, "failed": False},
                                    "position", 3)
        self.assertTrue(t["failed"] and t["suspect"])
        self.assertEqual(monitor.target_display_state(t), "failed")
        html = monitor.target_tag(t)
        self.assertIn("待核实", html)
        self.assertNotIn("2047.38", html)
        self.assertNotIn("2483", html)
        self.assertNotIn("target-space", html)
        self.assertNotIn("获取失败", html)
        # 正常的高空间不受影响
        ok = monitor.evaluate_target(79.24, 94.4, {"kind": "consensus", "price": 248.15, "month": 11, "failed": False},
                                     "position", 3)
        self.assertFalse(ok.get("failed"))
        self.assertEqual(monitor.target_display_state(ok), "high")
        # 手填目标不做这道检查（用户自己写的数）
        manual = monitor.evaluate_target(79.24, 94.4, {"kind": "manual", "price": 2047.38, "month": None, "failed": False},
                                         "position", 3)
        self.assertFalse(manual.get("failed"))
        # 普通获取失败仍写「获取失败」
        failed = monitor.evaluate_target(79.24, 94.4, {"kind": "consensus", "price": 90.0, "month": 11, "failed": True},
                                         "position", 3)
        self.assertIn("获取失败", monitor.target_tag(failed))

    def test_suspect_entry_jumps_the_queue_and_is_refetched_with_fresh_value(self):
        from unittest.mock import patch
        old = {"s": "ok", "avg": 2047.38, "upd": "2026-11-11", "r": "2026-11-15", "r_tz": "America/New_York",
               "try": "2026-11-15"}
        fresh = {"s": "ok", "avg": 248.15, "upd": "2026-11-20"}
        calls = []

        def fake_fetch(sess, sym):
            calls.append(sym)
            return dict(fresh)

        class Sess:
            headers = {}

        with patch.object(monitor, "fetch_consensus_one", side_effect=fake_fetch), \
                patch.object(monitor, "consensus_robots_ok", return_value=True), \
                patch.object(monitor, "budget_sleep", return_value=True):
            data, stat = monitor.update_consensus(["SNDK"], {"data": {"SNDK": old}}, "2026-11-23", sess=Sess(),
                                                  quota_day="2026-11-23", prices={"SNDK": 79.24})
            self.assertEqual(calls, ["SNDK"])                       # 才 8 天、本来没到期，也被插队重抓
            self.assertEqual(data["SNDK"]["avg"], 248.15)
            self.assertNotIn("sus", data["SNDK"])
            # 价格正常的同一条旧缓存不重抓（保持 14 天节拍）
            calls.clear()
            monitor.update_consensus(["SNDK"], {"data": {"SNDK": old}}, "2026-11-23", sess=Sess(),
                                     quota_day="2026-11-24", prices={"SNDK": 1900.0})
            self.assertEqual(calls, [])

    def test_still_suspect_after_refetch_is_flagged_and_not_hammered(self):
        from unittest.mock import patch
        old = {"s": "ok", "avg": 2047.38, "upd": "2026-11-11", "r": "2026-11-15", "r_tz": "America/New_York"}

        class Sess:
            headers = {}

        with patch.object(monitor, "fetch_consensus_one", return_value={"s": "ok", "avg": 2000.0, "upd": "2026-11-22"}), \
                patch.object(monitor, "consensus_robots_ok", return_value=True):
            data, _ = monitor.update_consensus(["SNDK"], {"data": {"SNDK": old}}, "2026-11-23", sess=Sess(),
                                               quota_day="2026-11-23", prices={"SNDK": 79.24})
        self.assertTrue(data["SNDK"]["sus"])
        # 带 sus 标记、刚抓过的：次日不再重复请求；满 3 天才再看
        self.assertFalse(monitor._consensus_suspect_due(data["SNDK"], 79.24, "2026-11-24"))
        self.assertTrue(monitor._consensus_suspect_due(data["SNDK"], 79.24, "2026-11-26"))

    def test_main_flow_passes_current_prices(self):
        src = (ROOT / 'monitor.py').read_text(encoding='utf-8')
        self.assertIn('prices={normalize_symbol(d["symbol"]): d.get("price")', src)


class SignalFilterBucketRegression(unittest.TestCase):
    def test_holdings_bucket_follows_group_order_not_full_label(self):
        """2026-11-23：多选结果里持仓个股掉进「其他」——成员组名只截前两字（长持／短持），
        旧代码拿「长持仓／短持仓」全称比对永远对不上。持仓分桶必须只认分组顺序第 0 位。"""
        js = monitor.PAGE_INTERACTION_JS
        self.assertIn("const bucket = order === 0 ? 0 : order === 1 ? 1 : 2;", js)
        self.assertNotIn("includes(member.g)", js)
        # 组名确实被截成前两字：这条若变了，分桶仍不依赖它，但要让人知道
        self.assertEqual(monitor._member_belong({"group": "position", "hold": "long"}), "长持")
        self.assertEqual(monitor._member_belong({"group": "position", "hold": "short"}), "短持")

    def test_signal_filter_groups_tight_and_no_rule_above_first_stock(self):
        """2026-11-23：多选清单分组之间距离缩小；分组名下第一只个股上方不画分隔线（线只在组内个股之间）。"""
        js, css = monitor.PAGE_INTERACTION_JS, monitor.PAGE_CSS
        self.assertIn("records.forEach(function(record, index) {", js)
        self.assertIn('if (index > 0) li.classList.add("has-separator");', js)
        self.assertNotIn('if (!record.first) li.classList.add("has-separator");', js)
        self.assertIn("#signalCounts .signal-filter .signal-member-block+.signal-member-block{margin-top:4px}", css)
        self.assertIn("#signalCounts .signal-member-block{margin:0 0 4px}", css)
        self.assertNotIn("signal-member-block+.signal-member-block{margin-top:10px}", css)
        # 分组名（持仓／重点／其他）字号与个股描述文字同号：这条规则不能再写死 13px，否则手机上被压得比描述小
        self.assertIn("#signalCounts .signal-filter .signal-member-block h5{margin:0 0 4px;color:var(--head-blue);font-weight:600}", css)
        self.assertNotIn("signal-member-block h5{margin:0 0 4px;color:var(--head-blue);font-size:13px", css)
        self.assertIn("#signalCounts .signal-member-block h5{font-size:15px;line-height:16.25px}", css)
        self.assertIn("#signalCounts .signal-filter-detail{font-size:15px;line-height:20.25px}", css)

    def test_position_pnl_fold_skeleton_and_single_open_linkage(self):
        """2026-11-23：仓位里默认折叠的「盈亏明细」（每只个股的总盈亏）——放在「交易记录」入口上一排、标题右侧不写合计、
        不做「已实现盈亏明细」；展开其他分组时，统计子板块和这个盈亏明细一并自动收起。"""
        import monitor_render
        src = (ROOT / 'monitor_render.py').read_text(encoding='utf-8')
        i_fold, i_log = src.index('id="piPnlBlock"'), src.index('class="pi-block pi-records"')
        self.assertLess(i_fold, i_log)                                   # 在交易记录上一排
        self.assertIn('<details class="pi-fold" id="piPnlFold"><summary><span class="pi-fold-name">盈亏明细</span></summary>', src)
        self.assertNotIn('piPnlSum', src)                                # 标题右侧不写合计金额
        self.assertNotIn('piRealizedBlock', src)                         # 不做已实现盈亏明细
        self.assertNotIn('已实现盈亏明细', src)
        self.assertNotIn('<details class="pi-fold" id="piPnlFold" open', src)   # 默认折叠
        js = monitor.PAGE_INTERACTION_JS
        self.assertIn('document.querySelectorAll("#signalCounts details[open], #group_positions .pi-fold[open]")', js)
        self.assertIn("    closeSubFolds();\n    document.querySelectorAll(\"details.card[open]\")", js)
        cost = (ROOT / 'cost_panel.js').read_text(encoding='utf-8')
        self.assertIn(": function (a, b) { return a.amt - b.amt; });", cost)   # 默认从亏损到盈利（2026-11-24 起可点抬头「仓位」改按仓位从大到小）
        self.assertNotIn('piRealizedList', cost)
        self.assertNotIn('piPnlSum', cost)
        css = (ROOT / 'cost_panel.css').read_text(encoding='utf-8')
        self.assertIn('.pi-fold > summary::before { content: ""; flex: none; width: 10px; height: 10px; margin-right: 0;', css)   # 左边展开小三角：CSS 画的 10×10 实心三角，与标题间距走 summary gap 4px
        self.assertIn('.pi-plist { display: grid; grid-template-columns: minmax(max-content,1.6fr) minmax(max-content,1.3fr) minmax(max-content,1fr) minmax(max-content,.7fr);', css)   # 2026-11-24：名称｜盈亏(金额+比一个整体)｜市值｜仓位 四列共用列轨
        self.assertIn('.pi-prow { display: grid; grid-column: 1 / -1; grid-template-columns: subgrid;', css)
        self.assertIn('.pi-prow.pi-phead', css)                # 2026-11-24：抬头行（代码｜盈亏｜金额｜仓位），与数据行同一套列轨
        self.assertIn("data-psort", cost)                       # 点「盈亏」从亏到盈、点「仓位」从大到小
        self.assertIn("PNL_SORT === 'weight'", cost)
        self.assertIn('.pi-pw { text-align: right; }', css)   # 最右的仓位列靠右贴齐页面右边
        self.assertIn('.pi-pname .pi-psec', css)               # 代码后面的行业前两个字
        self.assertIn("'<b class=\"pi-ppnl '", cost)           # 金额与盈亏比同在一个 pi-ppnl 里，算一个整体
        self.assertIn("'<b class=\"pi-pw\">'", cost)            # 市值右边的仓位列
        self.assertIn(".slice(0, 2) : '';", cost)               # 行业只要前两个字
        self.assertIn('.pi-fold[open] > summary::before { clip-path: polygon(0 15%, 100% 15%, 50% 100%); }', css)
        self.assertNotIn('个股总盈亏', src)                                                  # 名称已改为「盈亏明细」

    def test_jump_waits_for_layout_before_scroll_and_spym_is_short_named(self):
        """2026-11-24：当日清单点个股跳转落点偏（收起当日卡后页面才缩短，滚动已按旧高度开始）——
        滚动改为等收起引起的重排落定（toggle 之后再两帧）才开始；SPYM 备注改「标普」。"""
        js = monitor.PAGE_INTERACTION_JS
        i = js.index('function revealStockRow')
        body = js[i:js.index('// 统计面板里的标的名字', i)]
        self.assertIn('requestAnimationFrame(function() { window.requestAnimationFrame(go); })', body)
        self.assertEqual(body.count('scrollIntoView'), 1)           # 只剩延后的那一次，不再同步滚一次
        self.assertLess(body.index('__mmCloseOtherGroups'), body.index('scrollIntoView'))
        import json as _json
        h = _json.load(open(ROOT / 'holdings.json', encoding='utf-8'))['positions']
        n = _json.load(open(ROOT / 'symbol-names.json', encoding='utf-8'))
        self.assertEqual(h['SPYM']['note'], '标普')
        self.assertEqual(h['SPYM']['sector'], 'index_funds')        # 行业分类仍是指数基（页面标签「指数」）
        self.assertEqual(n['SPYM'], '标普')

    def test_day_contrib_sorted_loss_to_profit(self):
        """2026-11-23：当日清单固定从亏损到盈利。"""
        cost = (ROOT / 'cost_panel.js').read_text(encoding='utf-8')
        self.assertIn("return a.share - b.share;", cost)
        self.assertNotIn("return b.share - a.share;", cost)

    def test_rs_spy_title_text_not_bold(self):
        """2026-11-23：标题栏「20日对.SPX ±x%」不加粗，不再继承标题栏的 600。"""
        css = monitor.PAGE_CSS
        self.assertIn(".rs-spy{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--dim);font-weight:400}", css)

    def test_trade_log_fixed_columns_and_two_decimal_price(self):
        """2026-11-23：交易记录两页每列位置一致（固定列宽，不随当页内容漂移）；成交价只显示小数点后两位。"""
        html = (ROOT / 'trade-log.html').read_text(encoding='utf-8')
        self.assertIn('table-layout:fixed', html)
        self.assertIn('<colgroup><col style="width:14%"><col style="width:9%"><col style="width:14%"><col style="width:14%">'
                      '<col style="width:16%"><col style="width:16%"><col style="width:17%"></colgroup>', html)
        self.assertIn('if(i === 2){', html)
        self.assertIn('c = n.toFixed(2);', html)

    def test_signal_filter_clear_button_is_wide_and_short(self):
        css = monitor.PAGE_CSS
        self.assertIn("#signalCounts .signal-filter-clear{margin-left:auto;box-sizing:border-box;height:26px;padding:0 24px;", css)
        self.assertNotIn("signal-filter-clear{margin-left:auto;box-sizing:border-box;height:38px", css)


class CostSeedRegression(unittest.TestCase):
    def test_stock_cost_seed_accepts_finite_zero_negative_and_rejects_invalid(self):
        costs = {"ZERO_INT": 0, "ZERO_FLOAT": 0.0, "NEG_INT": -7,
                 "NEG_FLOAT": -7.25, "POS_INT": 12, "POS_FLOAT": 12.5,
                 "NAN": float("nan"), "INF": float("inf"),
                 "NEG_INF": float("-inf"), "TRUE": True, "FALSE": False,
                 "TEXT": "12.5", "UNKNOWN": None}
        valid = {sym: cost for sym, cost in costs.items()
                 if sym in ("ZERO_INT", "ZERO_FLOAT", "NEG_INT", "NEG_FLOAT",
                            "POS_INT", "POS_FLOAT")}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "holdings.json").write_text(json.dumps({"positions": {
                sym: {"cost": cost} for sym, cost in costs.items()}}), encoding="utf-8")
            with patch.object(monitor, "BASE", tmp):
                data = json.loads(monitor._cost_seed_json(),
                                  parse_constant=lambda value: self.fail("非有限成本泄漏：" + value))
            self.assertEqual(data["items"], {sym: {"cost": cost, "qty": 0}
                                             for sym, cost in valid.items()})
            for qty, expected in ((10, 10), (2.5, 2.5), (0, 0), (-1, 0)):
                seed = {"positions": {sym: qty for sym in costs}}
                (base / "position-costs.seed.json").write_text(json.dumps(seed), encoding="utf-8")
                with self.subTest(qty=qty), patch.object(monitor, "BASE", tmp):
                    data = json.loads(monitor._cost_seed_json(),
                                      parse_constant=lambda value: self.fail("非有限成本泄漏：" + value))
                self.assertEqual(data["items"], {sym: {"cost": cost, "qty": expected}
                                                 for sym, cost in valid.items()})

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
