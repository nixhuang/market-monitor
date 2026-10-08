"""离线回归：RSI 6/12/24 同侧两条黄、三条红。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor


def neutral_data():
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)]
    closes[-2:] = [100, 100]
    return dict(closes=closes, highs=[101] * 60, lows=[99] * 60,
                volumes=[1000] * 60, price=100, prev_close=100,
                dates=['2026-10-08'] * 60, source='test')


class TestRSI(unittest.TestCase):
    def analyze(self, values, cfg=None, data=None, bands=(100, 110, 90), streak=None):
        with patch.object(monitor, 'calc_rsi', side_effect=values) as rsi, \
                patch.object(monitor, 'boll', return_value=bands), \
                patch.object(monitor, 'boll_streak', return_value=streak or {'up': 0, 'dn': 0}):
            result = monitor.analyze_symbol('TEST', cfg or {}, data or neutral_data())
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
        data['prev_close'] = 97.5
        level, signals, _ = self.analyze([50, 50, 50], bands=(100, 101, 90), data=data)
        self.assertEqual(level, 'yellow')
        self.assertTrue(any(s.startswith('波动') for s in signals))
        self.assertFalse(any('双重信号' in s for s in signals))

    def test_other_red_with_bollinger_stays_red(self):
        for cfg, prev in [({}, 94), ({'trigger': 100}, 100)]:
            data = neutral_data()
            data['prev_close'] = prev
            level, signals, _ = self.analyze([50, 50, 50], cfg=cfg, data=data, bands=(100, 101, 90))
            self.assertEqual(level, 'red')
            self.assertFalse(any('双重信号' in s for s in signals))

    def test_other_red_not_downgraded(self):
        data = neutral_data()
        data['prev_close'] = 94
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
