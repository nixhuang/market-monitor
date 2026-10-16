"""离线回归：RSI 不再单独报灯，灯级只由「进布林报警区间 + RSI 同侧条数」产生。

   进区间且 2 条同侧 → 黄；3 条同侧 → 红；方向不一致（下轨配超买 / 上轨配超卖）→ 黄。
   不进区间时 RSI 几条过界都只是文字，不亮灯也不进统计。
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
                patch.object(monitor, 'boll_streak', return_value=streak or {'up': 0, 'dn': 0}):
            result = monitor.analyze_symbol('TEST', cfg or {}, data or neutral_data(), group=group)
            self.assertEqual([call.args[1] for call in rsi.call_args_list], [6, 12, 24])
        return result

    # ---------------------------------------------------------------- 单独 RSI 不报灯

    def test_rsi_alone_never_lights(self):
        """没进布林区间时，RSI 几条同侧过界都只留文字，不亮灯、不进计数。"""
        cases = [([50, 50, 50], 0), ([70, 69.999, 50], 0), ([36, 36.001, 50], 0),
                 ([70, 70, 50], 1), ([80, 70, 50], 1), ([35, 35, 50], 1),
                 ([70, 70, 70], 1), ([35, 35, 35], 1), ([80, 20, 50], 0),
                 ([None, 70, 70], 1), ([None, 34, 34], 1), ([None, None, 80], 0),
                 ([float('nan'), 50, 80], 0)]
        for values, expected_texts in cases:
            with self.subTest(values=values):
                level, signals, detail = self.analyze(values)   # 默认 bands 不触轨
                self.assertEqual(level, 'green')
                self.assertFalse(detail['signal_marks'])
                texts = [s for s in signals if s.startswith('RSI')]
                self.assertEqual(len(texts), expected_texts, signals)
                for text in texts:
                    self.assertNotIn('RSI6=', text)
                self.assertEqual(set(detail['rsi']), {6, 12, 24})

    def test_rsi_text_shows_boundary_and_count_only(self):
        level, signals, _ = self.analyze([80, 75, 50])
        text = next(s for s in signals if s.startswith('RSI'))
        self.assertEqual(text, 'RSI 2条超买（≥70）')
        level, signals, _ = self.analyze([20, 30, 34])
        text = next(s for s in signals if s.startswith('RSI'))
        self.assertEqual(text, 'RSI 3条超卖（≤35）')

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

    def test_direction_mismatch_never_red(self):
        """方向不一致降为黄：3 条 RSI 与轨道反向也不升级成红。"""
        level, signals, detail = self.analyze([20, 25, 30], bands=(100, 101, 90))
        self.assertEqual(level, 'yellow')
        self.assertEqual(detail['boll_rsi_direction'], '上轨（RSI超卖，方向不一致）')
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
                self.assertEqual(level, 'yellow' if active else 'green')
                self.assertEqual(('boll_rsi', 'yellow') in detail['signal_marks'], active)
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

    # ---------------------------------------------------------------- 长持仓

    def test_long_position_shields_upper_but_keeps_three(self):
        """长持仓屏蔽上轨的普通过热，RSI 三条全过界仍报红；下轨不受影响。"""
        long_cfg, short_cfg = {'hold': 'long'}, {'hold': 'short'}
        # 上轨 + 2 条 → 长持仓不报，短持仓报黄
        self.assertEqual(self.analyze([80, 80, 50], cfg=long_cfg, bands=(100, 101, 90))[0], 'green')
        self.assertEqual(self.analyze([80, 80, 50], cfg=short_cfg, bands=(100, 101, 90))[0], 'yellow')
        # 上轨 + 3 条 → 两边都报红
        level, _, detail = self.analyze([80, 80, 75], cfg=long_cfg, bands=(100, 101, 90))
        self.assertEqual(level, 'red')
        self.assertEqual(detail['boll_rsi_direction'], '上轨（超买）')
        # 下轨 2 条 / 3 条 → 长持仓照常
        self.assertEqual(self.analyze([20, 30, 50], cfg=long_cfg, bands=(100, 110, 99))[0], 'yellow')
        self.assertEqual(self.analyze([10, 20, 34], cfg=long_cfg, bands=(100, 110, 99))[0], 'red')
        # 没写 hold 字段按短持仓处理，规则不缩水
        self.assertEqual(self.analyze([80, 80, 50], cfg={}, bands=(100, 101, 90))[0], 'yellow')
        self.assertEqual(self.analyze([80, 80, 50], cfg={'hold': 'LONG'}, bands=(100, 101, 90))[0], 'green')

    def test_long_position_only_applies_to_positions(self):
        """同标的长持仓标记只在持仓组生效；重点组不看 hold。"""
        level, _, _ = self.analyze([80, 80, 50], cfg={'hold': 'long'}, bands=(100, 101, 90),
                                   group='focus')
        self.assertEqual(level, 'yellow')

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
        self.assertTrue(any(s.startswith('触及布林下轨') for s in signals), signals)
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
