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
    def analyze(self, values, cfg=None, data=None):
        with patch.object(monitor, 'calc_rsi', side_effect=values) as rsi, \
                patch.object(monitor, 'boll', return_value=(100, 110, 90)), \
                patch.object(monitor, 'boll_streak', return_value={'up': 0, 'dn': 0}):
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
