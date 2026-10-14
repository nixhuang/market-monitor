"""离线回归：RSI 6/12/24 同侧两条黄、三条红。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor

# 测试一律用代码里的默认设置，不读线上 settings.json（线上值一改，测试就会无故失败）
monitor.S.clear()
monitor.S.update(monitor.DEFAULT_SETTINGS)


def neutral_data():
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)]
    closes[-2:] = [100, 100]
    return dict(closes=closes, highs=[101] * 60, lows=[99] * 60,
                volumes=[1000] * 60, price=100, prev_close=100,
                dates=['2026-10-08'] * 60, source='test')


class TestRSI(unittest.TestCase):
    def analyze(self, values, cfg=None, data=None, bands=(100, 110, 90), streak=None):
        with patch.object(monitor, 'calc_rsi', side_effect=values) as rsi, \
                patch.object(monitor, 'vol_profile', return_value=('mid', None)), \
                patch.object(monitor, 'boll', return_value=bands), \
                patch.object(monitor, 'boll_streak', return_value=streak or {'up': 0, 'dn': 0}):
            result = monitor.analyze_symbol('TEST', cfg or {}, data or neutral_data(), group='position')
            self.assertEqual([call.args[1] for call in rsi.call_args_list], [6, 12, 24])
        return result

    def test_same_side_levels(self):
        cases = [
            ([50, 50, 50], 'green', None),
            ([70, 69.999, 50], 'green', None),
            ([30, 30.001, 50], 'green', None),
            ([70, 70, 50], 'yellow', '超买'),
            ([80, 70, 50], 'yellow', '超买'),
            ([30, 30, 50], 'yellow', '超卖'),
            ([20, 30, 50], 'yellow', '超卖'),
            ([70, 70, 70], 'red', '超买'),
            ([90, 80, 71], 'red', '超买'),
            ([30, 30, 30], 'red', '超卖'),
            ([10, 20, 29], 'red', '超卖'),
            ([80, 20, 50], 'green', None),
            ([80, 80, 20], 'yellow', '超买'),
            ([80, 20, 20], 'yellow', '超卖'),
            ([None, 70, 70], 'yellow', '超买'),
            ([None, 30, 30], 'yellow', '超卖'),
            ([None, None, 80], 'green', None),
            ([float('nan'), 50, 80], 'green', None),
        ]
        for values, expected, tag in cases:
            with self.subTest(values=values):
                level, signals, detail = self.analyze(values)
                self.assertEqual(level, expected)
                rsi_signals = [s for s in signals if s.startswith('RSI')]
                self.assertEqual(len(rsi_signals), 1 if tag else 0)
                self.assertEqual(set(detail['rsi']), {6, 12, 24})
                if tag:
                    self.assertIn(tag, rsi_signals[0])
                    self.assertIn('RSI6=', rsi_signals[0])
                    self.assertIn('RSI12=', rsi_signals[0])
                    self.assertIn('RSI24=', rsi_signals[0])

    def test_bollinger_independent_yellow_and_combined_red(self):
        bands_cases = [(100, 101.5, 90), (100, 101, 90), (100, 100.8, 90),
                       (100, 110, 98.6), (100, 110, 99), (100, 110, 99.2),
                       (100, 101, 99)]
        rsi_cases = [([50, 50, 50], 'yellow', False), ([70, 50, 50], 'yellow', False),
                     ([80, 20, 50], 'yellow', False), ([70, 70, 50], 'red', True),
                     ([30, 30, 50], 'red', True), ([70, 70, 70], 'red', True),
                     ([30, 30, 30], 'red', True), ([None, 70, 70], 'red', True)]
        for bands in bands_cases:
            for values, expected, combined in rsi_cases:
                with self.subTest(bands=bands, rsi=values):
                    level, signals, detail = self.analyze(values, bands=bands)
                    self.assertEqual(level, expected)
                    self.assertEqual(detail['level'], expected)
                    self.assertEqual(sum('布林' in s and '双重' not in s for s in signals), 1)
                    self.assertEqual('布林 + RSI 双重信号 → 红' in signals, combined)

    def test_combined_direction_matches_selected_band_and_rsi_side(self):
        cases = [((100, 101, 90), [80, 80, 50], '上轨（超买）'),
                 ((100, 110, 99), [20, 20, 50], '下轨（超卖）'),
                 ((100, 101, 90), [20, 20, 50], '上轨（RSI超卖，方向不一致）'),
                 ((100, 110, 99), [80, 80, 50], '下轨（RSI超买，方向不一致）')]
        for bands, values, direction in cases:
            with self.subTest(bands=bands, values=values):
                level, signals, detail = self.analyze(values, bands=bands)
                self.assertEqual(level, 'red')
                self.assertEqual(detail['boll_rsi_direction'], direction)
                self.assertIn(('boll_rsi', 'red'), detail['signal_marks'])
                self.assertIn('布林 + RSI 双重信号 → 红', signals)

    def test_lower_band_exact_touch_is_not_labeled_as_breakthrough(self):
        level, signals, detail = self.analyze([50, 50, 50], bands=(100, 110, 99))
        self.assertEqual(level, 'yellow')
        self.assertTrue(any(s.startswith('触及布林下轨') for s in signals), signals)
        self.assertNotIn('boll_rsi_direction', {k for k, v in detail.items() if v})
        self.assertIn('signal-yellow', monitor._signal_text_html(next(s for s in signals if '布林' in s)))

    def test_upper_band_yellow_only_in_positions_and_focus(self):
        data = neutral_data()
        data['highs'] = data['highs'][:-1] + [103]  # 上轨盘中冲过后收回
        def analyze(group, bands, rsi):
            with patch.dict(monitor.S, {'near_52w_low_pct': 0, 'ma_short': 0, 'ma_long': 0}), \
                    patch.object(monitor, 'calc_rsi', side_effect=rsi), \
                    patch.object(monitor, 'vol_profile', return_value=('mid', None)), \
                    patch.object(monitor, 'boll', return_value=bands), \
                    patch.object(monitor, 'boll_streak', return_value={'up': 0, 'dn': 0}):
                return monitor.analyze_symbol('T', {}, data, group=group)

        # 其他组上轨也显示状态，但只有持仓/重点关注产生独立黄灯、计数和图标。
        for group in (g['item_group'] for g in monitor.GROUPS):
            for bands, prefix in (((100, 103.3, 90), '逼近布林上轨'),
                                  ((100, 103, 90), '触及布林上轨'),
                                  ((100, 101, 90), '盘中突破布林上轨')):
                with self.subTest(group=group, bands=bands):
                    level, signals, detail = analyze(group, bands, [50, 50, 50])
                    active = group in ('position', 'focus')
                    upper = [s for s in signals if '布林上轨' in s]
                    self.assertEqual(len(upper), 1, signals)
                    self.assertTrue(upper[0].startswith(prefix), upper)
                    self.assertIn(upper[0], monitor._signals_html(detail))
                    self.assertEqual(('boll', 'yellow') in detail['signal_marks'], active)
                    self.assertEqual(detail['boll_alert_text'], upper[0] if active else '')
                    self.assertEqual(level, 'yellow' if active else 'green')
                    counts = monitor.signal_counts([detail])
                    self.assertEqual(counts['boll']['yellow'], int(active))
                    self.assertEqual(counts['boll']['sole_yellow'], int(active))
                    self.assertEqual(counts['boll_rsi']['red'], 0)
                    style = {'逼近布林上轨': ('white', '逼近上轨'),
                             '触及布林上轨': ('white', '触及上轨'),
                             '盘中突破布林上轨': ('yellow', '盘中突破上轨')}[prefix]
                    self.assertEqual(monitor._member_direction_markers(detail, 'boll'),
                                     [{'shape': '▲', 'tone': style[0], 'label': style[1]}] if active else [])

            # 下轨的黄灯和方向图标在所有分组保持不变。
            with self.subTest(group=group, band='lower'):
                level, signals, detail = analyze(group, (100, 110, 99.6), [50, 50, 50])
                self.assertEqual(level, 'yellow')
                self.assertEqual(detail['signal_marks'].count(('boll', 'yellow')), 1)
                self.assertTrue(any('布林下轨' in s for s in signals))
                self.assertEqual(monitor.signal_counts([detail])['boll']['sole_yellow'], 1)
                self.assertEqual(monitor._member_direction_markers(detail, 'boll')[0]['shape'], '▼')

            # 上轨＋两条 RSI：组合红和计数不变；非核心组明确显示具体上轨状态。
            with self.subTest(group=group, band='upper + RSI'):
                level, signals, detail = analyze(group, (100, 101, 90), [80, 80, 50])
                self.assertEqual(level, 'red')
                self.assertIn(('boll_rsi', 'red'), detail['signal_marks'])
                self.assertEqual(detail['boll_rsi_direction'], '上轨（超买）')
                self.assertTrue(any(s.startswith('盘中突破布林上轨') for s in signals), signals)
                self.assertIn('盘中突破布林上轨', monitor._signals_html(detail))
                self.assertIn('布林 + RSI 双重信号 → 红', signals)
                active = group in ('position', 'focus')
                self.assertEqual(('boll', 'yellow') in detail['signal_marks'], active)
                self.assertEqual(monitor.signal_counts([detail])['boll']['yellow'], int(active))
                self.assertEqual(monitor.signal_counts([detail])['boll_rsi']['red'], 1)

        # 已回落至轨内的日内越轨不能写成当前仍在轨外；现价越轨则用突破。
        above = neutral_data()
        above['price'] = 104
        above['closes'][-1] = 104
        above['highs'][-1] = 104
        with patch.dict(monitor.S, {'near_52w_low_pct': 0, 'ma_short': 0, 'ma_long': 0}), \
                patch.object(monitor, 'calc_rsi', side_effect=[50, 50, 50]), \
                patch.object(monitor, 'vol_profile', return_value=('mid', None)), \
                patch.object(monitor, 'boll', return_value=(100, 103, 90)), \
                patch.object(monitor, 'boll_streak', return_value={'up': 0, 'dn': 0}):
            level, signals, detail = monitor.analyze_symbol('T', {}, above, group='technology')
        self.assertEqual(level, 'green')
        self.assertTrue(any(s.startswith('突破布林上轨') for s in signals), signals)
        self.assertFalse(detail['signal_marks'])

        # 极窄布林带：原来上轨更近时，非核心组的下轨黄灯不应被上轨遮蔽。
        for rsi in ([50, 50, 50], [80, 80, 50]):
            level, signals, detail = analyze('technology', (100, 101, 99.6), rsi)
            self.assertEqual(sum('布林下轨' in s for s in signals), 1)
            self.assertEqual(sum('布林上轨' in s for s in signals), 1)
            self.assertEqual(detail['signal_marks'].count(('boll', 'yellow')), 1)
            self.assertIn('布林下轨', detail['boll_alert_text'])
            self.assertEqual(monitor._member_direction_markers(detail, 'boll'),
                             [{'shape': '▼', 'tone': 'yellow', 'label': '盘中跌破下轨'}])
            self.assertEqual(monitor.signal_counts([detail])['boll']['yellow'], 1)
            members = monitor.signal_count_members([detail])['boll']['yellow']
            self.assertEqual(len(members), 1)
            self.assertEqual([icon['shape'] for icon in members[0]['indicators']], ['▼'])
            if rsi[0] == 80:
                self.assertEqual(level, 'red')
                self.assertEqual(detail['boll_rsi_direction'], '上轨（超买）')
                self.assertIn(('boll_rsi', 'red'), detail['signal_marks'])

    def test_bollinger_misses_do_not_upgrade_rsi(self):
        for bands in [(100, 102, 90), (100, 110, 98), (None, None, None)]:
            level, signals, _ = self.analyze([70, 70, 50], bands=bands)
            self.assertEqual(level, 'yellow')
            self.assertFalse(any('布林' in s for s in signals))

    def test_consecutive_bollinger_hits_do_not_turn_red(self):
        streak = {'up': {'days': 6, 'cross': 2}, 'dn': {'days': 0, 'cross': 0}}
        level, signals, _ = self.analyze([50, 50, 50], bands=(100, 101, 90), streak=streak)
        self.assertEqual(level, 'yellow')
        self.assertTrue(any('连续第6日' in s for s in signals))
        self.assertFalse(any('双重信号' in s for s in signals))

    def test_other_yellow_is_not_an_rsi_signal(self):
        data = neutral_data()
        data['prev_close'] = data['price'] / (1 + (monitor.S['chg_yellow'] + 0.5) / 100)
        level, signals, _ = self.analyze([50, 50, 50], bands=(100, 101, 90), data=data)
        self.assertEqual(level, 'yellow')
        self.assertTrue(any(s.startswith('波动') for s in signals))
        self.assertFalse(any('双重信号' in s for s in signals))

    def test_other_red_with_bollinger_stays_red(self):
        for cfg, prev in [({}, 100 / (1 + (monitor.S['chg_red'] + 0.5) / 100)), ({'trigger': 100}, 100)]:
            data = neutral_data()
            data['prev_close'] = prev
            level, signals, _ = self.analyze([50, 50, 50], cfg=cfg, data=data, bands=(100, 101, 90))
            self.assertEqual(level, 'red')
            self.assertFalse(any('双重信号' in s for s in signals))

    def test_other_red_not_downgraded(self):
        data = neutral_data()
        data['prev_close'] = data['price'] / (1 + (monitor.S['chg_red'] + 0.5) / 100)
        level, signals, _ = self.analyze([70, 70, 50], data=data)
        self.assertEqual(level, 'red')
        self.assertTrue(any(s.startswith('RSI') for s in signals))
        self.assertTrue(any(s.startswith('异动') for s in signals))

    def test_quiet_exemption(self):
        for prev, expected in [(100, 'green'), (95, 'yellow')]:
            data = neutral_data()
            data['prev_close'] = prev
            level, signals, detail = self.analyze([90, 90, 90], cfg={'quiet': True}, data=data)
            self.assertEqual(level, expected)
            self.assertFalse(any(s.startswith('RSI') for s in signals))
            self.assertEqual(detail['rsi'], {6: None, 12: None, 24: None})

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
