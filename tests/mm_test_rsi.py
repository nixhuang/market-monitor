"""离线回归：布林+RSI与单独RSI互斥；单独三超卖全组红、三超买仅持仓黄。

组合按选中轨道及分组判灯；上轨三反向超卖持仓/重点黄，技术灰可与独立涨跌灯共存。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor

# 测试一律用代码里的默认设置，不读线上 settings.json（线上值一改，测试就会无故失败）
monitor.S.clear()
monitor.S.update(monitor.DEFAULT_SETTINGS)

# 关掉与布林/RSI 无关的灯，避免均线、52 周低位等干扰本文件的判定
QUIET = {'near_52w_low_pct': 0, 'ma_short': 0, 'ma_long': 0}


def neutral_data():
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)]
    closes[-2:] = [100, 100]
    return dict(closes=closes, highs=[101] * 60, lows=[99] * 60,
                volumes=[1000] * 60, price=100, prev_close=100,
                dates=['2026-10-08'] * 60, source='test')


class TestRSI(unittest.TestCase):
    def analyze(self, values, cfg=None, data=None, bands=(100, 110, 90), streak=None,
                group='position', sigma=None):
        with patch.dict(monitor.S, QUIET), \
                patch.object(monitor, 'calc_rsi', side_effect=values) as rsi, \
                patch.object(monitor, 'vol_profile', return_value=('mid', sigma)), \
                patch.object(monitor, 'boll', return_value=bands), \
                patch.object(monitor, 'boll_streak', return_value=streak or {'up': 0, 'dn': 0}), \
                patch.object(monitor, '_rule_amplitude', side_effect=lambda al, x: setattr(x, 'amp', None)):
            result = monitor.analyze_symbol('TEST', cfg or {}, data or neutral_data(), group=group)
            self.assertEqual([call.args[1] for call in rsi.call_args_list], [6, 12, 24])
        return result

    # ---------------------------------------------------------------- 单独 RSI 分组判灯

    def test_standalone_rsi_boundaries_and_lights(self):
        """单超卖仅文字、单超买无提醒、两条按组判、三超卖红、三超买持仓黄。"""
        cases = [([50, 50, 50], 0), ([70, 69.999, 50], 0), ([36, 36.001, 50], 0),
                 ([70, 70, 50], 1), ([80, 70, 50], 1), ([35, 35, 50], 1),
                 ([70, 70, 70], 1), ([35, 35, 35], 1), ([80, 20, 50], 1),
                 ([None, 70, 70], 1), ([None, 34, 34], 1), ([None, None, 80], 0),
                 ([float('nan'), 50, 80], 0)]
        for values, expected_texts in cases:
            with self.subTest(values=values):
                level, signals, detail = self.analyze(values)   # 默认 bands 不触轨
                expected = 'red' if values == [35, 35, 35] else 'yellow' if values == [70, 70, 70] else 'green'
                self.assertEqual(level, expected)
                self.assertEqual(detail['signal_marks'], [('rsi', expected)] if expected != 'green' else [])
                texts = [s for s in signals if s.startswith('RSI')]
                self.assertEqual(len(texts), expected_texts, signals)
                for text in texts:
                    self.assertNotIn('RSI6=', text)
                self.assertEqual(set(detail['rsi']), {6, 12, 24})

    def test_single_oversold_text_uses_configured_boundary_and_ignores_invalid_periods(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for invalid in (None, float('nan'), float('inf'), -float('inf')):
                for index in range(3):
                    values = [invalid, invalid, invalid]
                    values[index] = 28
                    with self.subTest(group=group, invalid=invalid, index=index), \
                            patch.dict(monitor.S, {'rsi_low': 28, 'rsi_high': 82}):
                        level, signals, detail = self.analyze(values, group=group)
                        self.assertEqual((level, detail['foldable'], detail['signal_marks']), ('green', True, []))
                        self.assertEqual(signals, ['RSI 1条 ↓超卖（≤28）'])
                        self.assertEqual(monitor.text_only_members([detail]), [])
                        self.assertEqual(monitor._signal_description_groups(detail),
                                         [{'kind': 'rsi', 'level': None, 'texts': signals}])
                        values[index] = 28.001
                        self.assertEqual(self.analyze(values, group=group)[1], [])
                        values[index] = 82
                        self.assertEqual(self.analyze(values, group=group)[1], [])

    def test_focus_two_overbought_with_invalid_or_opposite_period_stays_unlit(self):
        for third in (50, 28, None, float('nan'), float('inf'), -float('inf')):
            for index in range(3):
                values = [82, 82, 82]
                values[index] = third
                with self.subTest(third=third, index=index), \
                        patch.dict(monitor.S, {'rsi_low': 28, 'rsi_high': 82}):
                    level, signals, detail = self.analyze(values, group='focus')
                    expected = ['RSI 2条 ↑超买（≥82）'] + (['RSI 1条 ↓超卖（≤28）'] if third == 28 else [])
                    self.assertEqual(signals, expected)
                    self.assertEqual((level, detail['foldable'], detail['signal_marks']), ('green', True, []))
                    self.assertEqual(monitor.text_only_members([detail]), [])
                    self.assertEqual(monitor._signal_description_groups(detail),
                                     [{'kind': 'rsi', 'level': None, 'texts': expected}])

    def test_rsi_text_shows_boundary_and_count_only(self):
        level, signals, _ = self.analyze([80, 75, 50])
        text = next(s for s in signals if s.startswith('RSI'))
        self.assertEqual(text, 'RSI 2条 ↑超买（≥70）')
        level, signals, _ = self.analyze([20, 30, 34])
        text = next(s for s in signals if s.startswith('RSI'))
        self.assertEqual(text, 'RSI 3条 ↓超卖（≤35）')

    # ---------------------------------------------------------------- 组合灯级

    def test_combined_two_yellow_three_red_on_lower_band(self):
        """下轨：同侧 2 条黄、3 条红；RSI 不够 2 条则只有文字。"""
        cases = [([50, 50, 50], 'green', None),
                 ([20, 30, 50], 'yellow', '下轨（超卖）'),
                 ([10, 20, 34], 'red', '下轨（超卖）'),
                 ([70, 70, 50], 'yellow', '下轨（RSI超买，方向不一致）'),
                 ([80, 80, 80], 'yellow', '下轨（RSI超买，方向不一致）')]
        for values, expected, direction in cases:
            with self.subTest(values=values):
                level, signals, detail = self.analyze(values, bands=(100, 110, 99))
                self.assertEqual(level, expected)
                self.assertEqual(detail['level'], expected)
                if direction:
                    self.assertEqual(detail['boll_rsi_direction'], direction)
                    self.assertIn(('boll_rsi', expected), detail['signal_marks'])
                    self.assertIn(f'布林 + RSI → {"红" if expected == "red" else "黄"}', signals)
                else:
                    self.assertFalse(detail['signal_marks'])
                self.assertTrue(any('布林下轨' in s for s in signals))

    def test_combined_two_yellow_three_red_on_upper_band(self):
        """上轨：同侧 2 条黄、3 条红（持仓组，hold 缺省按短持仓）。"""
        cases = [([50, 50, 50], 'green'), ([80, 80, 50], 'yellow'),
                 ([80, 80, 75], 'red'), ([20, 20, 50], 'yellow')]
        for values, expected in cases:
            with self.subTest(values=values):
                level, signals, detail = self.analyze(values, bands=(100, 101, 90))
                self.assertEqual(level, expected)
                if expected != 'green':
                    self.assertIn(('boll_rsi', expected), detail['signal_marks'])
        # 3 条同侧才算红，2 条只是黄
        _, _, two = self.analyze([80, 80, 50], bands=(100, 101, 90))
        _, _, three = self.analyze([80, 80, 75], bands=(100, 101, 90))
        self.assertEqual(two['boll_rsi_hits'], 2)
        self.assertEqual(three['boll_rsi_hits'], 3)

    def test_direction_mismatch_respects_explicit_upper_and_lower_rules(self):
        """上轨三超卖持仓/重点黄，其他技术灰可折叠；下轨三超买仍反向黄。"""
        for group in (g['item_group'] for g in monitor.GROUPS):
            with self.subTest(group=group):
                level, signals, detail = self.analyze([20, 25, 30], bands=(100, 101, 90), group=group)
                active = group in monitor.BOLL_UP_SIGNAL_GROUPS
                self.assertEqual(level, 'yellow' if active else 'green')
                self.assertEqual(detail['signal_marks'], [('boll_rsi', 'yellow')] if active else [])
                self.assertTrue(detail['foldable'])
                if active:
                    self.assertEqual(detail['boll_rsi_direction'], '上轨（RSI超卖，方向不一致）')
                    self.assertEqual(detail['boll_rsi_hits'], 3)
                    self.assertIn('布林 + RSI → 黄', signals)
                    self.assertNotIn('布林 + RSI → 红', signals)
                    self.assertEqual(monitor.text_only_members([detail]), [])
                else:
                    members = monitor.text_only_members([detail])
                    self.assertEqual([(m['kind'], m['side'], m['rsi_n'], m['rsi_side']) for m in members],
                                     [('boll_rsi', '上轨', 3, '超卖')])
        level, signals, detail = self.analyze([80, 85, 90], bands=(100, 110, 99))
        self.assertEqual(level, 'yellow')
        self.assertEqual(detail['boll_rsi_direction'], '下轨（RSI超买，方向不一致）')

    # ---------------------------------------------------------------- 上轨只对持仓/重点

    def test_upper_band_combo_only_in_positions_and_focus(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            with self.subTest(group=group):
                active = group in ('position', 'focus')
                # 上轨 + 2 条超买
                level, signals, detail = self.analyze([80, 80, 50], bands=(100, 101, 90), group=group)
                self.assertEqual(level, 'yellow' if group == 'position' else 'green')
                self.assertEqual(('boll_rsi', 'yellow') in detail['signal_marks'], group == 'position')
                self.assertEqual(detail['foldable'], group != 'focus')
                if group == 'focus':
                    self.assertEqual(monitor.text_only_members([detail])[0]['kind'], 'boll_rsi')
                # 状态文字任何分组都要显示
                self.assertTrue(any('布林上轨' in s for s in signals), signals)
                # 上轨 + 3 条超买：其他分组照样只展示，不参与灯级
                level, signals, detail = self.analyze([80, 80, 75], bands=(100, 101, 90), group=group)
                self.assertEqual(level, 'red' if active else 'green')
                # 下轨方向所有分组一致
                level, _, detail = self.analyze([20, 30, 50], bands=(100, 110, 99), group=group)
                self.assertEqual(level, 'yellow')
                self.assertIn(('boll_rsi', 'yellow'), detail['signal_marks'])

    def test_bollinger_text_never_carries_a_light(self):
        """布林单独命中只展示文字：不进 marks、不进计数、不改灯级。"""
        for bands in ((100, 101.5, 90), (100, 101, 90), (100, 110, 98.6), (100, 110, 99)):
            with self.subTest(bands=bands):
                level, signals, detail = self.analyze([50, 50, 50], bands=bands)
                self.assertEqual(level, 'green')
                self.assertFalse(detail['signal_marks'])
                self.assertTrue(any('布林' in s for s in signals), signals)
                counts = monitor.signal_counts([detail])
                self.assertEqual(counts['boll'], {'label': '布林', 'red': 0, 'yellow': 0, 'purple': 0})
                self.assertEqual(counts['rsi']['yellow'], 0)
                self.assertEqual(counts['boll_rsi']['red'], 0)

    # ---------------------------------------------------------------- 持仓规则不分长短

    def test_position_rules_ignore_long_short_for_alerts(self):
        """长短持仓统一为原短持仓规则，hold 字段只保留展示归属。"""
        cases = (([80, 80, 50], (100, 101, 90), 'yellow'),
                 ([20, 30, 50], (100, 101, 90), 'yellow'),
                 ([80, 80, 75], (100, 101, 90), 'red'),
                 ([20, 30, 50], (100, 110, 99), 'yellow'),
                 ([10, 20, 34], (100, 110, 99), 'red'))
        for values, bands, expected in cases:
            for cfg in ({'hold': 'long'}, {'hold': 'short'}, {'hold': 'LONG'}, {}):
                with self.subTest(values=values, bands=bands, cfg=cfg):
                    level, _, detail = self.analyze(values, cfg=cfg, bands=bands)
                    self.assertEqual(level, expected)
                    self.assertIn(('boll_rsi', expected), detail['signal_marks'])
                    self.assertTrue(detail['foldable'])
                    self.assertIn('data-quiet-keep="false"', monitor._stock_row(detail, {}))

    def test_long_position_only_applies_to_positions(self):
        """同标的长持仓标记只在持仓组生效；重点组不看 hold。"""
        level, _, detail = self.analyze([80, 80, 50], cfg={'hold': 'long'}, bands=(100, 101, 90),
                                        group='focus')
        self.assertEqual(level, 'green')
        self.assertFalse(detail['foldable'])
        self.assertEqual(monitor.text_only_members([detail])[0]['kind'], 'boll_rsi')

    # ---------------------------------------------------------------- 回归护栏

    def test_boll_near_threshold_follows_volatility(self):
        """逼近门槛按个股平时波动缩放：同一 gap，波动大的算贴近、算不出波动的算没贴近。"""
        # gap_up = (102.03 - 101) / 102.03 ≈ 1.0%
        bands = (100, 102.03, 90)
        # 平时日波动 2% → 门槛 = 15% × 4 × 1.29 × 2.0 ≈ 1.55%，1.0% 算贴近
        level, signals, _ = self.analyze([80, 80, 50], bands=bands, sigma=2.0)
        self.assertEqual(level, 'yellow')
        self.assertTrue(any('布林上轨' in s for s in signals), signals)
        # 算不出波动 → 回退固定 0.5%，1.0% 不算贴近
        level, _, _ = self.analyze([80, 80, 50], bands=bands, sigma=None)
        self.assertEqual(level, 'green')

    def test_lower_band_exact_touch_is_not_labeled_as_breakthrough(self):
        level, signals, detail = self.analyze([50, 50, 50], bands=(100, 110, 99))
        self.assertEqual(level, 'green')
        self.assertTrue(any(s.startswith('触及布林下轨▼') for s in signals), signals)
        self.assertIn('signal-yellow', monitor._signal_text_html(next(s for s in signals if '布林' in s)))

    def test_consecutive_bollinger_hits_do_not_turn_red(self):
        streak = {'up': {'days': 6, 'cross': 2}, 'dn': {'days': 0, 'cross': 0}}
        level, signals, _ = self.analyze([50, 50, 50], bands=(100, 101, 90), streak=streak)
        self.assertEqual(level, 'green')
        self.assertTrue(any('连续第6日' in s for s in signals))
        self.assertFalse(any('布林 + RSI' in s for s in signals))

    def test_other_yellow_is_not_a_boll_signal(self):
        data = neutral_data()
        data['prev_close'] = data['price'] / (1 + (monitor.S['chg_yellow'] + 0.5) / 100)
        level, signals, _ = self.analyze([50, 50, 50], bands=(100, 101, 90), data=data)
        self.assertEqual(level, 'yellow')
        self.assertTrue(any(s.startswith('波动') for s in signals))
        self.assertFalse(any('布林 + RSI' in s for s in signals))

    def test_other_red_with_bollinger_stays_red(self):
        for cfg, prev in [({}, 100 / (1 + (monitor.S['chg_red'] + 0.5) / 100)), ({'trigger': 100}, 100)]:
            data = neutral_data()
            data['prev_close'] = prev
            level, signals, _ = self.analyze([50, 50, 50], cfg=cfg, data=data, bands=(100, 101, 90))
            self.assertEqual(level, 'red')
            self.assertFalse(any('布林 + RSI' in s for s in signals))

    def test_other_red_not_downgraded(self):
        data = neutral_data()
        data['prev_close'] = data['price'] / (1 + (monitor.S['chg_red'] + 0.5) / 100)
        level, signals, detail = self.analyze([70, 70, 50], data=data)
        self.assertEqual(level, 'red')
        self.assertTrue(any(s.startswith('RSI') for s in signals))
        self.assertTrue(any(s.startswith('异动') for s in signals))
        self.assertEqual([m for m in detail['signal_marks'] if m[0] in ('rsi', 'boll')], [])

    def test_algorithm_periods_and_flat(self):
        for period in (6, 12, 24):
            self.assertIsNone(monitor.calc_rsi(list(range(period)), period))
            self.assertEqual(monitor.calc_rsi(list(range(period + 1)), period), 100)
            self.assertEqual(monitor.calc_rsi(list(range(period, -1, -1)), period), 0)
            self.assertEqual(monitor.calc_rsi([100] * (period + 2), period), 50)
            prices = [100 + i % 2 for i in range(period + 1)]
            self.assertAlmostEqual(monitor.calc_rsi(prices, period), 50)
            prices.append(prices[-1] + 1)
            self.assertAlmostEqual(monitor.calc_rsi(prices, period),
                                   100 * (0.5 * (period - 1) + 1) / period)

    def test_insufficient_long_period(self):
        data = neutral_data()
        for key in ('closes', 'highs', 'lows', 'volumes', 'dates'):
            data[key] = data[key][-13:]
        _, _, detail = monitor.analyze_symbol('TEST', {}, data)
        self.assertIsNotNone(detail['rsi'][6])
        self.assertIsNotNone(detail['rsi'][12])
        self.assertIsNone(detail['rsi'][24])


if __name__ == '__main__':
    unittest.main()
