"""规则 schema、页面注入与资源 hash 的纯离线回归；不改用户配置或公开页面。"""
import ast
import hashlib
import html
import json
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
        self.assertEqual(monitor.DEFAULT_SETTINGS["target_space_pct"], 30)
        self.assertEqual(monitor.DEFAULT_SETTINGS["target_high_space_pct"], 70)
        self.assertEqual(monitor.SETTING_LIMITS["target_space_pct"], (30, 30))
        self.assertEqual(monitor.SETTING_LIMITS["target_high_space_pct"], (70, 70))
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
            self.assertEqual((got["target_space_pct"], got["target_high_space_pct"]), (30, 70))
            self.assertEqual(got["trigger_gap_pct"], 7)
            self.assertEqual(warnings, [])
            with patch("builtins.open", mock_open(read_data=json.dumps({"target_space_pct": old}))) as opened, \
                    patch.object(monitor, "log") as warn:
                loaded = monitor.load_settings()
            self.assertEqual((loaded["target_space_pct"], loaded["target_high_space_pct"]), (30, 70))
            opened().write.assert_not_called()
            warn.assert_not_called()
        for key, valid in (("target_space_pct", 30), ("target_high_space_pct", 70)):
            self.assertEqual(rules_config.setting_error(key, valid), "")
            for bad in (valid - 1, valid + 1, True, str(valid), float("nan"), float("inf")):
                with self.subTest(key=key, bad=bad):
                    self.assertTrue(rules_config.setting_error(key, bad))
                    warnings = []
                    got = rules_config.resolve_settings({key: bad}, warnings.append)
                    self.assertEqual(got[key], valid)
                    self.assertEqual(len(warnings), 1)

    def test_target_rules_explain_30_70_boundaries_scope_priority_and_union(self):
        constants = monitor._rule_constants()
        self.assertEqual(constants["target_space_pct"], 30)
        self.assertEqual(constants["target_high_space_pct"], 70)
        dynamic = page_assets.rules_template(monitor.S, constants)
        for label, markup in (("动态规则", dynamic), ("静态模板", monitor.RULES_TEMPLATE),
                              ("渲染页", self.render())):
            with self.subTest(label=label):
                rules = re.search(r'<template id="rulesTpl">(.*?)</template>', markup, re.S)[1]
                glossary = re.search(r'<template id="glossaryTpl">(.*?)</template>', markup, re.S)[1]
                text = plain_text(rules)
                for term in ("目标空间 所有分组", "有效空间≥30% → 紫虚线空心灯",
                             "≥70% → 紫红虚线空心灯", "达到目标／共识价 持仓分组、重点",
                             "当日上穿／跌破或现价在目标±3% → 实心紫灯",
                             "目标空间<30%、未达到目标／共识价、共识获取失败及失败旧价"):
                    self.assertIn(term, text)
                explanation = plain_text(glossary)
                for term in ("有效手填目标价优先", "空间＝（目标／共识价÷现价−1）×100%",
                             "失败旧共识不参与，有效手填价不受影响",
                             "空间／达到各自去重、允许重叠；总数取并集，不直接相加",
                             "所有分组符合条件的美股个股适用", "排除ETF名单、指数、期货与伯克希尔",
                             "达到按昨收与现价判断，不用日内高低曾触及代替"):
                    self.assertIn(term, explanation)
                self.assertNotRegex(text, r'空间[^。；]*[>≥][^。；]*(?:50|100)%|目标空间异常红|红胶囊|仅报数据异常')
                self.assertIn('<span class="lamp-purple">紫虚线空心灯</span>', rules)
                self.assertIn('<span class="tone-target-high">紫红虚线空心灯</span>', rules)
                self.assertIn('<span class="lamp-purple">实心紫灯</span>', rules)

    def test_target_css_preserves_capsule_geometry_and_range52_markers(self):
        css = page_assets.PAGE_CSS
        def declarations(selector):
            return re.search(r'(?:^|\n)' + re.escape(selector) + r'\{([^}]*)\}', css)[1]
        base = declarations('.lvtag')
        for stable in ('margin:0 4px 2px 0', 'padding:0 7px', 'font-size:11px', 'line-height:17px'):
            self.assertIn(stable, base)
        self.assertIn('padding:1px 5px', declarations('.tgs .lvtag'))
        self.assertIn('border:1px solid var(--dim)', declarations('.lvtag.tgt.low'))
        self.assertIn('border:1.5px solid var(--purple)', declarations('.lvtag.tgt.space::after'))
        self.assertIn('border:1.5px solid var(--target-high)', declarations('.lvtag.tgt.high::after'))
        self.assertIn('text-shadow:.2px 0 currentColor,-.2px 0 currentColor', declarations('.lvtag.tgt.high'))
        self.assertNotIn('font-weight', declarations('.lvtag.tgt.high'))
        self.assertIn('text-shadow:none', declarations('.lvtag.tgt.hit'))
        self.assertIn('border:1px dashed var(--dim)', declarations('.lvtag.tgt.bad'))
        self.assertIn('color:var(--dim)', declarations('.lvtag.tgt.bad'))
        self.assertIn('inset:-1px', declarations('.lvtag.tgt.space::after,.lvtag.tgt.high::after'))
        self.assertIn('color:#fff;background:#8957e5', declarations('.lvtag.tgt.hit'))
        self.assertGreater(css.index('.lvtag.tgt.hit{'), css.index('.lvtag.tgt.bad{'))
        self.assertNotRegex(css, r'\.lvtag\.tgt\.[^{]+\{[^}]*font-size')
        self.assertIn('border-top:9px solid #efd9ff', declarations('.range52-marker'))
        self.assertIn('border-top:7px solid #713caf', declarations('.range52-marker::before'))
        self.assertIn('background:#f0c674', declarations('.range52-cost'))
        self.assertIn('.up{color:var(--up)} .down{color:var(--down)}', css)
        for lamp, width in (("red", 6), ("yellow", 4), ("green", 2)):
            self.assertIn(f'--lc:var(--{lamp});--lw:{width}px', css)
        for alias in ('tone-target-high', 'target-space-high'):
            self.assertIn(f'#signalCounts .purple-extra .text-only li>.signal-member-icon.{alias}', css)
            self.assertIn(f'#signalCounts .signal-member-block h5.{alias}', css)
        self.assertNotRegex(css, r'(?:tone-target-high|target-space-high)[^{]*\{[^}]*var\(--red\)')

    def test_target_right_stripes_use_same_state_in_both_rows_and_continuous_dash_phase(self):
        css = page_assets.PAGE_CSS
        def declarations(selector):
            return re.search(r'(?:^|\n)' + re.escape(selector) + r'\{([^}]*)\}', css)[1]
        for state, color, width in (("space", "purple", 2), ("high", "target-high", 3), ("hit", "purple", 2)):
            upper = declarations(f'.group-card tr.pur-{state} td.sig')
            lower = declarations(f'.stk tr.pur-{state}+tr.range52-row>td')
            self.assertEqual(upper, lower)
            self.assertIn(f'--pw:{width}px', upper)
            self.assertIn(f'--pc:var(--{color})', upper)
            self.assertEqual('repeating-linear-gradient' in upper, state != 'hit')
        main = declarations('.stk tr.pur:has(+tr.range52-row)>td.sig')
        tail = declarations('.stk tr.range52-row>td')
        self.assertIn('background-image:var(--pstripe)', main)
        self.assertIn('background-size:var(--pw) calc(85% - 2.1px)', main)
        self.assertIn('background-position:right bottom', main)
        self.assertIn('linear-gradient(var(--lc),var(--lc)),var(--pstripe)', tail)
        self.assertIn('background-size:var(--lw) 18px,var(--pw) 18px', tail)
        self.assertIn('background-position:left top,right top', tail)
        self.assertIn('to top,var(--pc) 0 10px,transparent 10px 18px', css)
        self.assertIn('--target-high:#dc62b8', css)
        self.assertIn('.target-purple>h5{margin:6px 0 3px', css)
        self.assertIn('.target-purple .signal-member-list{padding-bottom:0}', css)
        self.assertIn('.target-purple li{margin:2px 0}', css)
        self.assertIn('.target-purple .target-space-high .signal-member-direction', css)
        self.assertIn('.target-purple .note{display:inline;margin-left:6px}', css)
        self.assertIn('.target-purple .signal-member-direction{color:#c4a3ff}', css)
        self.assertNotIn('.target-purple .target-space-high .note', css)
        self.assertNotIn('.target-purple .target-space-high .signal-member-belong', css)
        self.assertNotIn('.tone-target-high,.target-space-high{', css)
        self.assertNotIn('#signalCounts .tone-target-high,#signalCounts .target-space-high,', css)
        self.assertIn('.group-card tr.pur-high .sig .tp{color:var(--target-high)}', css)
        # 按真实CSS提取周期及下段长度，覆盖主行高度变化和接缝上下两侧。
        tail_height = float(re.search(r'var\(--pw\) ([\d.]+)px', tail)[1])
        stripe = declarations('.group-card tr.pur-space td.sig')
        dash, gap_start, period = map(float, re.search(r'0 ([\d.]+)px,transparent ([\d.]+)px ([\d.]+)px', stripe).groups())
        self.assertEqual(dash, gap_start)
        self.assertEqual(tail_height % period, 0)
        for upper_height in (20, 47.5, 83.2, 150):
            for y in (-8, -5, -3, -1, -0.25, 0.25, 1, 3, 5, 8):
                separate_distance = -y if y < 0 else tail_height - y
                continuous_distance = upper_height + tail_height - (upper_height + y)
                self.assertEqual(separate_distance % period < dash,
                                 continuous_distance % period < dash, (upper_height, y))
        self.assertGreater(css.index('.group-card tr.pur-hit td.sig'), css.index('.group-card tr.pur-high td.sig'))
        self.assertGreater(css.index('.stk tr.pur-hit+tr.range52-row>td'), css.index('.stk tr.pur-high+tr.range52-row>td'))

    def test_positions_green_tints_and_shared_collapse_background(self):
        css = page_assets.PAGE_CSS
        self.assertIn('--signal-bg:#182131; --collapse-bg:var(--signal-bg)', css)
        self.assertIn('#group_positions{background:linear-gradient(rgba(46,160,67,.02),rgba(46,160,67,.02)),var(--card);--collapse-bg:rgba(46,160,67,.15)}', css)
        self.assertIn('#group_positions_long.zone-card{background:rgba(46,160,67,.02)}', css)
        self.assertIn('#signalCounts{border:1px solid #6686c6;box-shadow:inset 3px 0 0 #8fb8f0;background:var(--signal-bg)}', css)
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
        self.assertIn("加仓价提醒标记已读 → 灰灯", text)
        self.assertIn("减仓价提醒标记已读 → 灰灯", text)

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
                self.assertEqual(members['rsi']['red'][0]['rsi_hits'], '3条超卖')
                self.assertEqual(monitor.text_only_members([row]), [])
                card = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
                self.assertIn('data-kind="rsi"', card)
                self.assertIn('aria-label="RSI3条超卖"', card)
                self.assertNotIn('data-kind="rsi_low"', card)

    def test_standalone_three_overbought_uses_yellow_rsi_kind_only_in_active_groups(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            with self.subTest(group=group):
                x = SimpleNamespace(rsi={period: monitor.S['rsi_high'] for period in (6, 12, 24)}, group=group)
                alerts = monitor._Alerts()
                monitor._rule_rsi(alerts, x)
                level = 'yellow' if group in ('position', 'focus') else 'green'
                self.assertEqual(alerts.level, level)
                self.assertEqual(alerts.marks, [('rsi', 'yellow')] if level == 'yellow' else [])
                row = {'symbol': 'OVERBOUGHT', 'group': group, 'level': level,
                       'signals': alerts.signals, 'signal_marks': alerts.marks}
                members = monitor.signal_count_members([row])
                self.assertEqual(monitor.signal_counts([row])['rsi']['yellow'], int(level == 'yellow'))
                if level == 'yellow':
                    self.assertEqual(members['rsi']['yellow'][0]['indicators'],
                                     [{'shape': '▲', 'tone': 'yellow', 'label': 'RSI3条超买'}])
                else:
                    self.assertEqual(monitor.text_only_members([row])[0]['kind'], 'rsi')

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
        self.assertEqual(markup.count('<table '), 9)  # 两项阈值、两项连续示例、五项抓取表。
        for ident in ('glossary-chg-table', 'glossary-amp-table', 'fetch-reference'):
            self.assertEqual(markup.count(f'id="{ident}"'), 1)
        self.assertEqual(re.search(r'<section id="glossary-calculation">(.*?)</section>', markup, re.S)[1].count('<table '), 4)
        self.assertEqual(re.search(r'<section id="glossary-fetch">(.*?)</section>', markup, re.S)[1].count('<table '), 5)

    def test_home_rules_explain_cost_membership_and_boll_upper_without_jargon(self):
        text = page_assets.rules_body(monitor.S, monitor._rule_constants())
        glossary = page_assets.glossary_body(monitor.S, monitor._rule_constants())
        rules_text, glossary_text = plain_text(text), plain_text(glossary)
        self.assertIn('单独RSI三条超卖', rules_text)
        self.assertRegex(rules_text, r'单独RSI三条超卖\s+所有分组 → 红灯')
        self.assertIn('重点、其他分组不适用', rules_text)
        for explanation in ('只撤页面追加的该项红灯，不撤生成红灯或其他原因的灯',
                            '统计由对应红列转灰列', '灰字删除线',
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
        self.assertIn('href="#marketRiskRules"', page)
        self.assertIn('href="#signalRules"', page)
        self.assertIn('href="#glossary-fetch"', page)
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
        self.assertIn('href="#rule-earnings">颜色与保留规则</a>', glossary[1])
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
                self.assertIn("第一条（短期）：自适应确认穿越 → 黄灯", rules_text)
                self.assertIn("第二条（长期）：日内触及±1%或穿越 → 黄灯", rules_text)
                self.assertIn("两条同周期只计一次，按简单穿越判断", glossary)
                self.assertNotIn("均线已关闭", rules_text)
                self.assertNotIn("当前0日", rules_text)
                for name, period in (("第一条（短期）", short), ("第二条（长期）", long)):
                    status = f"当前{period}日" if period else "当前关闭"
                    self.assertRegex(rules_text, re.escape(name) + r'[^\n]*（' + status + r'）')
                    self.assertIn(name[:-1] + "，" + status + "）", glossary)
                x = monitor._analysis_context("OFFLINE", {},
                    {"closes": [100.0] * 60, "price": 100, "prev_close": 100,
                     "highs": [], "lows": [], "dates": []}, "position")
                self.assertEqual([p for p, _ in x.moving_averages], list(dict.fromkeys(p for p in (short, long) if p)))

    def test_volume_adaptive_off_on_and_fixed_fallback(self):
        for yellow, red in ((0, 3.6), (1.8, 0), (0, 0), (1.8, 3.6)):
            with self.subTest(yellow=yellow, red=red):
                monitor.S.update(vol_ratio_z=yellow, vol_ratio_red_z=red, vol_ratio=2, vol_ratio_red=4)
                self.assertFalse(monitor.valid_settings(monitor.S))
                active = yellow > 0 and red > 0
                self.assertEqual(monitor._vol_ratio_z_enabled(), active)
                al = monitor._Alerts()
                monitor._rule_vol_ratio(al, SimpleNamespace(vol_ratio=2.5, vol_ratio_z=5,
                    vol_ratio_date="2026-10-01", vol_ratio_intraday=False))
                self.assertEqual(al.level, "red" if active else "yellow")
                text = page_assets.rules_body(monitor.S, monitor._rule_constants())
                self.assertEqual(page_assets.rule_flags(monitor.S)["vol_adaptive"], active)
                self.assertIn(f"自适应：≥{red:g}σ → 红灯，≥{yellow:g}σ → 黄灯", plain_text(text))
                self.assertIn("固定：≥4× → 红灯，≥2× → 黄灯", plain_text(text))
                glossary = plain_text(page_assets.glossary_body(monitor.S, monitor._rule_constants()))
                self.assertIn("异常值有效、两条σ阈值启用且量比≥1×，用自适应；否则用固定倍数", glossary)
                self.assertIn("自适应未命中，不再用固定倍数补灯", glossary)
                self.assertIn("盘中按已开盘时间折算成交速度，只用固定倍数", glossary)

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
        self.assertIn("布林上轨 + RSI 2条向上超买 → 黄灯", rules)
        self.assertIn("布林下轨 + RSI 3条向下超卖 → 红灯", rules)

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
                     {"target_space_pct": 30, "target_high_space_pct": 70},
                     {"target_space_pct": 29, "target_high_space_pct": 69}]
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
        self.assertIn('布林上轨 + RSI 3条向上超买 → 红灯',
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
        for name in ("monitor.py", "rules_config.py", "page_assets.py", "tests/mm_test_rules.py",
                     "tests/mm_test_groups.py", "tests/mm_test_render.py"):
            ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name, feature_version=(3, 11))


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
