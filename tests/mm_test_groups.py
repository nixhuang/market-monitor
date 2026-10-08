"""离线分类模型、去重、备注及全部折叠标题回归。"""
import json
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import monitor


class TestGroups(unittest.TestCase):
    def test_definitions_and_initial_config(self):
        labels = ['持仓', '重点关注', '指数基', 'IT软硬Ai', '医疗保健', '金融', '能源',
                  '工航防建机', '化材金纸', '公水电气', '必需消费品', '通信娱乐', '非车酒奢', '房地产']
        self.assertEqual([g['label'] for g in monitor.GROUPS], labels)
        with open(os.path.join(ROOT, 'holdings.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        self.assertNotIn('watch', cfg)
        self.assertEqual(len(cfg['positions']), 14)
        self.assertTrue(all(c.get('note') for c in cfg['positions'].values()))
        with open(os.path.join(ROOT, 'import-audit.json'), encoding='utf-8') as f:
            audit = json.load(f)
        self.assertEqual({g['label']: len(cfg[g['key']]) for g in monitor.GROUPS}, audit['final_counts'])
        self.assertTrue(all(c.get('note') for g in monitor.GROUPS for c in cfg[g['key']].values()))

    def test_dedup_priority_and_old_watch_excluded(self):
        cfg = {'positions': {'BRK.B': {'note': '持仓'}},
               'focus': {'BRK-B': {}, 'MSFT': {}},
               'technology': {'MSFT': {}, 'NVDA': {}},
               'healthcare': {'NVDA': {}, 'UNH': {}}, 'watch': {'SHOULDIGNORE': {}},
               'group_monitoring': {'technology': True, 'healthcare': True}}
        universe, counts, duplicates = monitor.grouped_universe(cfg)
        self.assertEqual([s for s, _, _ in universe], ['BRK.B', 'MSFT', 'NVDA', 'UNH'])
        self.assertEqual([g for _, _, g in universe], ['position', 'focus', 'technology', 'healthcare'])
        self.assertEqual(duplicates, 3)
        self.assertEqual(sum(counts.values()), 4)
        self.assertNotIn('watch', counts)

    def test_special_assets_are_preserved_without_fake_stock_quotes(self):
        cfg = {'index_funds': {s: {'note': s} for s in ['.SPX', 'BD#US10Y', 'ESMAIN', 'CLMAIN', '2USDCNY', '2XAUUSD']},
               'group_monitoring': {'index_funds': True}}
        universe, counts, _ = monitor.grouped_universe(cfg)
        self.assertEqual(len(universe), 6)
        self.assertEqual(counts['index_funds'], 6)
        self.assertTrue(all(monitor.quote_supported(s) for s, _, _ in universe[:4]))
        self.assertTrue(all(not monitor.quote_supported(s) for s, _, _ in universe[4:]))
        self.assertTrue(monitor.quote_supported('AAPL'))
        self.assertFalse(monitor.quote_supported('NQmain'))
        self.assertEqual(monitor.normalize_symbol('31#BRK.B'), 'BRK-B')
        rows = [dict(symbol=s, price=None, level='gray') for s, _, _ in universe]
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, rows, cfg)
        self.assertEqual(len(snap['summary']['missing_symbols']), 4)
        self.assertEqual(len(snap['summary']['unsupported_symbols']), 2)

    def test_index_aliases_use_daily_index_or_futures_not_stock_quotes(self):
        from datetime import date, timedelta
        dates = [(date(2026, 10, 7) - timedelta(days=i)).isoformat() for i in range(31, -1, -1)]
        def bars():
            return {'dates': dates[:], 'closes': [100.0] * 32, 'highs': [100.0] * 32,
                    'lows': [100.0] * 32, 'volumes': [0] * 32,
                    'price': 100.0, 'prev_close': 100.0}
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'CLOSED_ONLY', False), \
                patch.object(monitor, 'yahoo_history', side_effect=lambda _: bars()) as yahoo, \
                patch.object(monitor, 'nasdaq_realtime') as realtime, \
                patch.object(monitor, 'stooq_history') as stooq, patch.object(monitor, 'nasdaq_history') as nasdaq:
            for code, alias in monitor.INDEX_YAHOO_ALIAS.items():
                data = monitor.fetch_history(code)
                self.assertEqual((data['price'], data['source']), (100.0, 'yahoo'))
                self.assertEqual(yahoo.call_args.args[0], alias)
            self.assertIsNone(monitor.fetch_history('BD#US10Y'))
            stooq.assert_not_called(); nasdaq.assert_not_called(); realtime.assert_not_called()

    def test_existing_reference_quotes_have_dates_without_fake_signals(self):
        cfg = {'index_funds': {'.VIX': {'note': '波动率'}, '.SPX': {}, 'BD#US10Y': {}},
               'group_monitoring': {'index_funds': True}}
        macro = {k: {'ok': True, 'name': monitor.FRED_SERIES[k]['name'], 'value': value,
                     'prev': value - 1, 'date': '2026-10-07'}
                 for k, value in [('vix', 21.3), ('sp500', 6700.5), ('ust10', 4.25)]}
        rows = [monitor.macro_index_quote(s, cfg['index_funds'][s], 'index_funds', macro)
                for s in cfg['index_funds']]
        self.assertEqual([d['price'] for d in rows], [21.3, 6700.5, 4.25])
        self.assertTrue(all(d['level'] == 'green' and d['reference_only'] for d in rows))
        self.assertIsNone(monitor.macro_index_quote('.NDX', {}, 'index_funds', macro))
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot(macro, rows, cfg)
        page = monitor.render(macro, rows, len(rows), snapshot=snap)
        index = page.split('id="group_index_funds"', 1)[1].split('id="group_technology"', 1)[0]
        for code in cfg['index_funds']:
            self.assertIn(code, index)
        self.assertIn('参考值 · 截至 2026-10-07', index)
        self.assertIn('4.25%</td>', index)
        self.assertEqual(index.count('BD#US10Y'), 1)
        self.assertNotIn('市场参考（独立指标，不属于清单标的）', index)
        self.assertIn('不计算交易警示', index)
        self.assertNotIn('无异动 3 只', index)
        self.assertEqual(snap['summary']['missing_prices'], 0)

    def test_older_reference_does_not_mark_all_daily_quotes_stale(self):
        cfg = {'index_funds': {'BD#US10Y': {}, 'HYG': {}},
               'group_monitoring': {'index_funds': True}}
        reference = monitor.macro_index_quote('BD#US10Y', {}, 'index_funds',
                                               {'ust10': {'ok': True, 'value': 4.25,
                                                          'date': '2026-10-06'}})
        daily = {'symbol': 'HYG', 'note': '', 'price': 77.0, 'chg': 0.0,
                 'rsi': {}, 'dist_high': None, 'dist_low': None, 'vol_ratio': None,
                 'trigger': None, 'group': 'index_funds', 'data_date': '2026-10-07',
                 'boll_up': None, 'boll_dn': None, 'signals': [], 'level': 'green'}
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, [reference, daily], cfg)
        self.assertEqual(snap['summary']['reference_dates'], {'BD#US10Y': '2026-10-06'})
        self.assertEqual(snap['summary']['stale_symbols'], [])
        self.assertEqual((snap['summary']['today_prices'], snap['summary']['prior_prices']), (1, 0))
        self.assertEqual(snap['actual_dates'], {'min': '2026-10-07', 'max': '2026-10-07'})
        self.assertEqual(snap['data_time_text'], '2026-10-07 收盘（美东交易日）')
        page = monitor.render({}, [reference, daily], 2, snapshot=snap)
        self.assertIn('1 只日线已更新 · 1 项参考值（截至 2026-10-06）', page)
        self.assertNotIn('当日行情未取得', page)
        self.assertIn('参考值 · 截至 2026-10-06', page)

    def test_snapshot_all_groups(self):
        with patch.object(monitor, 'global_dca', return_value=None):
            snapshot = monitor.build_snapshot({}, [], {'technology': {'NVDA': {}},
                                                       'group_monitoring': {'technology': True}})
        self.assertEqual(snapshot['list_counts']['technology'], 1)
        self.assertEqual(snapshot['groups'], monitor.GROUPS)
        self.assertNotIn('watch', snapshot['list_counts'])

    def test_switch_defaults_and_sector_exceptions(self):
        cfg = {'positions': {'AAPL': {}}, 'focus': {'MSFT': {}}, 'index_funds': {'HYG': {}}}
        for group in monitor.GROUPS:
            if group.get('sector_etf'):
                cfg[group['key']] = {group['sector_etf']: {}, 'ORDINARY': {}}
        original = json.loads(json.dumps(cfg))
        universe, _, _ = monitor.grouped_universe(cfg)
        expected = ['AAPL', 'MSFT'] + [g['sector_etf'] for g in monitor.GROUPS if g.get('sector_etf')]
        self.assertEqual([s for s, _, _ in universe], expected)
        self.assertEqual(cfg, original)
        cfg['materials'].pop('XLB')
        self.assertNotIn('XLB', [s for s, _, _ in monitor.grouped_universe(cfg)[0]])
        cfg['group_monitoring'] = {'positions': False, 'focus': False, 'materials': True}
        symbols = [s for s, _, _ in monitor.grouped_universe(cfg)[0]]
        self.assertIn('AAPL', symbols)
        self.assertIn('MSFT', symbols)
        self.assertTrue(monitor.group_monitoring(cfg)['positions'])
        self.assertTrue(monitor.group_monitoring(cfg)['focus'])
        self.assertIn('ORDINARY', symbols)

    def test_closed_groups_do_not_take_dedup_priority(self):
        cfg = {'positions': {'BRK.B': {}}, 'focus': {'MSFT': {}},
               'technology': {'MSFT': {}, 'BRK-B': {}, 'XLK': {}, 'NVDA': {}},
               'group_monitoring': {'positions': False, 'technology': True}}
        universe, _, duplicates = monitor.grouped_universe(cfg)
        self.assertEqual([s for s, _, _ in universe], ['BRK.B', 'MSFT', 'XLK', 'NVDA'])
        self.assertEqual(duplicates, 2)
        cfg['group_monitoring']['technology'] = False
        self.assertEqual([s for s, _, _ in monitor.grouped_universe(cfg)[0]], ['BRK.B', 'MSFT', 'XLK'])

    def test_monitoring_snapshot_and_sector_display(self):
        cfg = {'materials': {'XLB': {}, 'DD': {}}}
        row = dict(symbol='XLB', note='板块基金', price=100, chg=0, signals=['异动测试'],
                   level='yellow', group='materials')
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, [row], cfg)
        self.assertEqual(snap['registered_counts']['materials'], 2)
        self.assertEqual(snap['list_counts']['materials'], 1)
        self.assertFalse(snap['group_monitoring']['materials'])
        self.assertEqual(snap['summary']['missing_symbols'], [])
        page = monitor.render({}, [row], 1, snapshot=snap)
        self.assertIn('化材金纸 (2)', page)
        self.assertIn('仅板块ETF', page)
        self.assertIn('XLB 仍监测', page)
        self.assertIn('异动测试', page)
        self.assertNotIn('DD</td>', page)

    def test_main_skips_closed_stocks_but_alerts_sector_etfs(self):
        import tempfile
        cfg = {'positions': {'AAPL': {}}, 'focus': {'MSFT': {}},
               'materials': {'XLB': {}, 'DD': {}}, 'technology': {'XLK': {}, 'NVDA': {}}}
        def analyzed(symbol, settings, data, group):
            detail = dict(symbol=symbol, note='', price=100, chg=0, level='yellow',
                          group=group, signals=['板块异动'], data_date='2026-10-07')
            return 'yellow', detail['signals'], detail
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'fetch_history', return_value={'price': 100}) as fetch, \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan') as notify:
                self.assertEqual(monitor.main([]), 0)
                self.assertEqual([c.args[0] for c in fetch.call_args_list], ['AAPL', 'MSFT', 'XLK', 'XLB'])
                self.assertEqual([r['symbol'] for r in notify.call_args.args[1]], ['AAPL', 'MSFT', 'XLK', 'XLB'])
                with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                    self.assertEqual(json.load(f)['summary']['total'], 4)

    def test_main_keeps_index_reference_prices_if_yahoo_unavailable(self):
        import tempfile
        cfg = {'index_funds': {s: {} for s in ('.VIX', '.SPX', 'BD#US10Y', '.NDX')},
               'group_monitoring': {'index_funds': True}}
        macro = {k: {'ok': True, 'name': monitor.FRED_SERIES[k]['name'], 'value': v,
                     'prev': v, 'date': '2026-10-07'}
                 for k, v in (('vix', 21.3), ('sp500', 6700.5), ('ust10', 4.25))}
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'build_macro', return_value=macro), \
                    patch.object(monitor, 'fetch_history', return_value=None) as fetch, \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'push_serverchan'):
                self.assertEqual(monitor.main([]), 0)
            self.assertEqual([c.args[0] for c in fetch.call_args_list], ['.VIX', '.SPX', 'BD#US10Y', '.NDX'])
            with open(os.path.join(directory, 'index.html'), encoding='utf-8') as f:
                page = f.read()
            with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                snap = json.load(f)
        index = page.split('id="group_index_funds"', 1)[1].split('id="group_technology"', 1)[0]
        for price in ('21.30', '6,700.50', '4.25%'):
            self.assertIn(price, index)
        self.assertEqual(snap['summary']['missing_symbols'], ['.NDX'])
        self.assertEqual(snap['summary']['unsupported_symbols'], [])
        self.assertEqual(snap['summary']['missing_prices'], 1)

    def test_three_failures_do_not_skip_fourth_symbol(self):
        import tempfile
        cfg = {'positions': {s: {} for s in ('FAIL1', 'FAIL2', 'FAIL3', 'RECOVER')}}
        fetched = []
        def fetch(symbol):
            fetched.append(symbol)
            return {'price': 100, 'data_date': '2026-10-07'} if symbol == 'RECOVER' else None
        def analyzed(symbol, settings, data, group):
            d = dict(symbol=symbol, note='', price=100, chg=0, level='green',
                     group=group, signals=[], data_date='2026-10-07')
            return 'green', [], d
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'fetch_history', side_effect=fetch), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan'):
                self.assertEqual(monitor.main([]), 0)
                self.assertEqual(fetched, ['FAIL1', 'FAIL2', 'FAIL3', 'RECOVER'])
                with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                    snap = json.load(f)
                self.assertEqual(snap['summary']['total'], 4)
                self.assertEqual(snap['summary']['missing_prices'], 3)
                self.assertEqual(snap['summary']['green'], 1)

    def test_deadline_marks_only_unattempted_rows(self):
        import tempfile
        cfg = {'positions': {s: {} for s in ('FIRST', 'SECOND', 'THIRD')}}
        fetched = []
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            clock = iter([0, 0, 481, 481, 481, 481, 481])
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'fetch_history', side_effect=lambda s: fetched.append(s) or None), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'push_serverchan'), patch.object(monitor.time, 'monotonic', side_effect=clock):
                self.assertEqual(monitor.main([]), 0)
                with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                    snap = json.load(f)
                with open(os.path.join(directory, 'index.html'), encoding='utf-8') as f:
                    page = f.read()
            self.assertEqual(fetched, ['FIRST'])
            self.assertEqual(snap['summary']['total'], 3)
            self.assertIn('本轮抓取时间预算不足，尚未取数', page)

    def test_daily_source_validation_and_intraday_fallback(self):
        from datetime import datetime, timedelta
        def bars(last, price=100, prev=100, high=None):
            end = datetime.fromisoformat(last)
            dates = [(end - timedelta(days=i)).date().isoformat() for i in range(31, -1, -1)]
            closes = [prev] * 31 + [price]
            return {'dates': dates, 'closes': closes, 'price': price, 'prev_close': prev,
                    'highs': [prev] * 31 + [high or max(price, prev)],
                    'lows': [prev] * 31 + [min(price, prev)], 'volumes': [100] * 32}
        old = bars('2026-10-07')
        today = bars('2026-10-08', 102)
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'CLOSED_ONLY', False), \
                patch.object(monitor, 'yahoo_history', return_value=old), \
                patch.object(monitor, 'stooq_history', return_value=today), \
                patch.object(monitor, 'nasdaq_history', return_value=None), \
                patch.object(monitor, 'nasdaq_realtime', return_value=None):
            data = monitor.fetch_history('AAPL')
        self.assertEqual((data['dates'][-1], data['source'], data['price']), ('2026-10-08', 'stooq', 102))
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'CLOSED_ONLY', False), \
                patch.object(monitor, 'yahoo_history', return_value=old), \
                patch.object(monitor, 'stooq_history', return_value=None), \
                patch.object(monitor, 'nasdaq_history', return_value=None), \
                patch.object(monitor, 'nasdaq_realtime', return_value=None):
            self.assertEqual(monitor.fetch_history('AAPL')['dates'][-1], '2026-10-07')
        self.assertFalse(monitor.valid_history(bars('2026-10-08', 105, high=102)))
        broken = bars('2026-10-08'); broken['closes'][-1] = float('nan')
        self.assertFalse(monitor.valid_history(broken))
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'CLOSED_ONLY', True), \
                patch.object(monitor, 'yahoo_history', return_value=bars('2026-10-08', 190)), \
                patch.object(monitor, 'stooq_history', return_value=None), \
                patch.object(monitor, 'nasdaq_history', return_value=None):
            self.assertIsNone(monitor.fetch_history('AAPL'))
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'CLOSED_ONLY', True), \
                patch.object(monitor, 'yahoo_history', return_value=bars('2026-10-08', 190)), \
                patch.object(monitor, 'stooq_history', return_value=bars('2026-10-08', 191)), \
                patch.object(monitor, 'nasdaq_history', return_value=None):
            self.assertEqual(monitor.fetch_history('AAPL')['source'], 'yahoo')
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'CLOSED_ONLY', True), \
                patch.object(monitor, 'yahoo_history', return_value=bars('2026-10-08', 104, high=103)), \
                patch.object(monitor, 'stooq_history', return_value=bars('2026-10-08', 102)), \
                patch.object(monitor, 'nasdaq_history', return_value=None):
            self.assertEqual(monitor.fetch_history('AAPL')['source'], 'stooq')

    def test_realtime_preserves_intraday_high_low_for_bollinger(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        class TradingClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 8, 11, 0, tzinfo=ZoneInfo('America/New_York')).astimezone(tz)
        dates = [(datetime(2026, 10, 8) - timedelta(days=i)).date().isoformat() for i in range(31, -1, -1)]
        data = {'dates': dates, 'closes': [100]*31+[102], 'highs':[100]*31+[110],
                'lows':[100]*31+[95], 'volumes':[100]*32, 'price':102, 'prev_close':100}
        with patch.object(monitor, 'datetime', TradingClock), patch.object(monitor, 'CLOSED_ONLY', False), \
                patch.object(monitor, 'nasdaq_realtime', return_value={'price':101,'ts':'today'}):
            result = monitor.apply_realtime('AAPL', data)
        self.assertEqual((result['price'],result['closes'][-1],result['highs'][-1],result['lows'][-1]),
                         (101,101,110,95))
        self.assertTrue(result['realtime'])

    def test_settings_reject_bad_values_but_keep_valid_config(self):
        import tempfile
        bad = dict(monitor.DEFAULT_SETTINGS, boll_n=0, rsi_low=80, rsi_high=70,
                   amp_red=3, amp_yellow=5, chg_red=4.5)
        self.assertEqual(monitor.valid_settings(bad), 'boll_n')
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            with open(os.path.join(directory, 'settings.json'), 'w', encoding='utf-8') as f:
                json.dump(bad, f)
            with patch.object(monitor, 'BASE', directory):
                loaded = monitor.load_settings()
        self.assertEqual(loaded['boll_n'], 20)
        self.assertEqual(loaded['rsi_low'], 30)
        self.assertEqual(loaded['amp_red'], 8)
        self.assertEqual(loaded['chg_red'], 4.5)
        self.assertEqual(monitor.valid_settings(monitor.DEFAULT_SETTINGS), '')

    def test_market_time_uses_data_session_not_fetch_clock(self):
        def snap(when, date='2026-10-07', mode='manual_or_config'):
            return {'finished_at_bj': when, 'mode': mode, 'actual_dates': {'min': date, 'max': date}}
        for when in ['2026-10-08T13:55:28+08:00', '2026-10-08T21:29:00+08:00',
                     '2026-10-08T04:00:00+08:00', '2026-10-10T23:00:00+08:00']:
            self.assertEqual(monitor.market_data_time(snap(when)), '2026-10-07 收盘（美东交易日）')
        live = snap('2026-10-08T21:30:00+08:00', '2026-10-08')
        self.assertIn('2026-10-08 21:30:00（盘中快照，北京时间）', monitor.market_data_time(live))
        self.assertNotIn('盘中快照', monitor.market_data_time(snap('2026-10-08T21:30:00+08:00')))
        self.assertNotIn('盘中快照', monitor.market_data_time(snap('2026-10-08T21:30:00+08:00', '2026-10-08', 'closed')))
        holiday = monitor.plan_run('workflow_dispatch', monitor.datetime.fromisoformat('2026-12-25T15:00:00-05:00'))
        self.assertTrue(holiday['closed_only'])
        self.assertNotEqual(holiday['target'], '2026-12-25')
        self.assertEqual(monitor.market_data_time({}), '未取得行情')

    def test_intraday_summary_and_per_stock_fallback_labels(self):
        rows = [dict(symbol=s, note=s, group='position', price=price, chg=0, level=level,
                     source='yahoo', data_date=date, signals=[]) for s, price, level, date in [
                         ('TODAY', 101, 'green', '2026-10-08'),
                         ('OLD', 100, 'yellow', '2026-10-07'),
                         ('MISSING', None, 'gray', '')]]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), \
                patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, rows, {'positions': {r['symbol']: {} for r in rows}})
            snap['mode'] = 'manual_or_config'
            snap['finished_at_bj'] = '2026-10-08T22:35:12+08:00'
            page = monitor.render({}, rows, 3, snapshot=snap)
        self.assertEqual([snap['summary'][k] for k in ('today_prices','prior_prices','missing_prices')], [1,1,1])
        self.assertIn('2026-10-08 22:35:12（盘中快照，北京时间） · 当日价 1 只 / 较早日线 1 只 / 无数据 1 只',page)
        self.assertIn('日线 2026-10-07 收盘', page)
        self.assertIn('class="tag quote-note">无数据</span>', page)
        self.assertNotIn('日线 2026-10-08 收盘', page)
        none = dict(snap, actual_dates={'min':'','max':''}, summary={'missing_prices':3})
        self.assertIn('22:35:12（盘中运行，北京时间 · 未取得行情） · 无数据 3 只',
                      monitor.market_data_time(none))

    def test_render_is_side_effect_free_and_layout(self):
        rows = [dict(symbol=s, note=s, price=100, chg=chg, signals=[], level=lv,
                     source='test', group='position')
                for s, chg, lv in [('FIRST', 0, 'green'), ('SECOND', 5, 'red')]]
        page = monitor.render({'curve': {'ok': True, 'name': '收益率曲线', 'value': 0.51}}, rows, 2)
        self.assertEqual([r['symbol'] for r in rows], ['FIRST', 'SECOND'])
        self.assertNotIn('class="overall', page)
        self.assertNotIn('绿框 · 不用动', page)
        self.assertIn('.group-card tr.red td:first-child{box-shadow:inset 6px', page)
        for color, width in [('red', 6), ('yellow', 4), ('green', 2)]:
            self.assertIn(f'tr.{color} td:first-child{{box-shadow:inset {width}px', page)
        self.assertNotIn('0.51 个百分点', page)
        risk = page.split('id="macroCard"', 1)[1].split('id="group_positions"', 1)[0]
        reference = page.split('id="group_index_funds"', 1)[1].split('id="group_technology"', 1)[0]
        self.assertNotIn('收益率曲线', risk)
        self.assertNotIn('收益率曲线', reference)
        self.assertNotIn('id="yieldCurveHelp"', page)
        self.assertNotIn('10年期国债收益率 − 2年期国债收益率', page)

    def test_macro_old_values_are_gray_and_yahoo_fallback_uses_real_dates(self):
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), \
                patch.object(monitor, 'fred_series', return_value=[('2026-09-01', 2), ('2026-09-02', 2)]), \
                patch.object(monitor, 'market_breadth', return_value={'ok':False,'name':'上涨参与度'}):
            macro = monitor.build_macro()
        for key in ('hy_oas','vix','sp500','nfci','ust10','dxy','curve'):
            self.assertEqual(monitor.macro_status(key, macro[key])[0], 'gray')
        rows = monitor._rows_from_closes([101,102], dates=['2026-10-02','2026-10-05'])
        self.assertEqual(rows, [('2026-10-02',101),('2026-10-05',102)])
        self.assertEqual(monitor._rows_from_closes([101,102], dates=None), [])
        self.assertEqual(monitor._rows_from_closes([101,102], dates=['2026-10-02']), [])

    def test_treasury_uses_newer_valid_yield_and_preserves_source_dates(self):
        from datetime import date, timedelta
        from contextlib import ExitStack
        def series(last, value=4.2):
            end = date.fromisoformat(last)
            return [((end - timedelta(days=i)).isoformat(), value) for i in range(39, -1, -1)]
        def yahoo(last='2026-10-07', value=4.3):
            rows = series(last, value)
            return {'dates': [d for d, _ in rows], 'closes': [v for _, v in rows],
                    'price': value, 'prev_close': value, 'highs': [value]*40,
                    'lows': [value]*40, 'volumes': [0]*40,
                    'quote_symbol': '^TNX', 'quote_name': 'Treasury Yield 10 Years'}
        old = series('2026-10-06')
        def fetch(fred_rows, data, error=None):
            info = {}
            with ExitStack() as stack:
                stack.enter_context(patch.object(monitor, 'TARGET_DATE', '2026-10-07'))
                stack.enter_context(patch.object(monitor, 'FRED_API_KEY', 'test-key'))
                api = stack.enter_context(patch.object(monitor, 'fred_api', return_value=fred_rows))
                csv = stack.enter_context(patch.object(monitor, 'fred_csv', return_value=[]))
                quote = stack.enter_context(patch.object(monitor, 'yahoo_history', return_value=data, side_effect=error))
                rows = monitor.fred_series('DGS10', alias='^TNX', source_info=info)
            return rows, info, api, csv, quote
        rows, info, _, csv, quote = fetch(old, yahoo())
        self.assertEqual(rows[-1], ('2026-10-07', 4.3))
        self.assertTrue(all(v == 4.3 for _, v in rows))
        self.assertEqual(info, {'source':'Yahoo ^TNX', 'unit':'%', 'date':'2026-10-07', 'lagging':False})
        csv.assert_not_called(); quote.assert_called_once_with('^TNX')
        fresh = series('2026-10-07')
        rows, info, _, _, quote = fetch(fresh, yahoo())
        self.assertEqual(rows, fresh)
        quote.assert_not_called()
        for data in (None, yahoo('2026-10-06'), yahoo('2026-10-05'), yahoo(value=43.0),
                     dict(yahoo(), quote_symbol='SPY'), dict(yahoo(), quote_name='S&P 500'),
                     dict(yahoo(), price=float('nan')),
                     dict(yahoo(), dates=['2026-10-07']*40)):
            with self.subTest(data=data):
                rows, info, _, _, _ = fetch(old, data)
                self.assertEqual(rows, old)
                self.assertEqual(info['source'], 'FRED DGS10')
                self.assertTrue(info['lagging'])
        rows, info, _, _, _ = fetch(old, None, monitor.requests.Timeout('offline'))
        self.assertEqual(rows, old)
        self.assertTrue(info['lagging'])
        rows, info, _, _, _ = fetch(old, yahoo('2026-10-08'))
        self.assertEqual(rows[-1], ('2026-10-07', 4.3))
        rows, info, _, _, _ = fetch(old, dict(yahoo(), quote_name=''))
        self.assertEqual(info['source'], 'Yahoo ^TNX')
        rows, info, _, _, _ = fetch([], dict(yahoo(), quote_name=''))
        self.assertEqual((rows, info), ([], {}))
        rows, info, _, _, _ = fetch([], yahoo())
        self.assertEqual(info['source'], 'Yahoo ^TNX')
        rows, info, _, _, _ = fetch([], None)
        self.assertEqual((rows, info), ([], {}))
        reference = monitor.macro_index_quote('BD#US10Y', {}, 'index_funds',
            {'ust10': {'ok':True, 'value':4.2, 'date':'2026-10-06', 'source':'FRED DGS10', 'lagging':True}})
        cfg = {'index_funds': {'BD#US10Y': {}}, 'group_monitoring': {'index_funds':True}}
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, [reference], cfg)
        page = monitor.render({}, [reference], 1, snapshot=snap)
        self.assertIn('参考值 · 截至 2026-10-06', page)
        self.assertIn('来源 FRED DGS10 · 收益率百分比', page)
        self.assertIn('保留最近参考值', page)

    def test_credit_spread_thresholds_are_unambiguous(self):
        original = monitor.S.copy()
        try:
            self.assertNotIn('hy_yellow', monitor.DEFAULT_SETTINGS)
            self.assertNotIn('hy_yellow', monitor.S)
            for value, expected in ((349, 'green'), (350, 'yellow'), (399, 'yellow'),
                                    (400, 'red'), (500, 'red')):
                with self.subTest(value=value):
                    self.assertEqual(monitor.macro_status('hy_oas', {'ok': True, 'value': value})[0], expected)
            self.assertEqual(monitor.macro_status('hy_oas', {'ok': True, 'value': 320,
                                                             'delta_week': 50})[0], 'red')
        finally:
            monitor.S.clear();monitor.S.update(original)

    def test_index_reference_does_not_change_holdings_or_macro_alerts(self):
        cfg = {'index_funds': {'HYG': {'note':'保留的原清单'}}}
        macro = {key: {'ok': True, 'name': monitor.FRED_SERIES[key]['name'], 'value': value,
                       'date': '2026-10-07'} for key, value in
                 (('ust10', 4.3), ('dxy', 100.4), ('curve', -0.25))}
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot(macro, [], cfg)
        page = monitor.render(macro, [], 0, snapshot=snap)
        risk = page.split('id="macroCard"',1)[1].split('id="group_positions"',1)[0]
        index = page.split('id="group_index_funds"',1)[1].split('id="group_technology"',1)[0]
        self.assertFalse(snap['group_monitoring']['index_funds'])
        self.assertEqual(snap['summary']['total'], 0)
        self.assertEqual(snap['registered_counts']['index_funds'], 1)
        self.assertIn('指数基 (1)', page)
        for name in ('10Y美债','美元指数','收益率曲线'):
            self.assertNotIn(name, index)
            self.assertNotIn(name, risk)
        self.assertNotIn('市场参考（独立指标，不属于清单标的）', page)
        self.assertNotIn('index-reference', page)
        self.assertNotIn('原指数基成员及监测开关保持不变', index)
        self.assertEqual(list(cfg['index_funds']), ['HYG'])
        self.assertIn('id="marketRiskLight" data-level="yellow"', risk)
        self.assertIn('综合：数据不足', risk)

    def test_five_indicator_summary_requires_cross_category_confirmation(self):
        import copy
        base = {k: {'ok': True, 'date': '2026-10-07', 'value': value}
                for k, value in (('hy_oas', 300), ('vix', 15), ('sp500', 6700),
                                 ('breadth', 65), ('nfci', -0.3))}
        base['sp500']['drawdown'] = -2
        base['breadth']['pct50'] = 60
        def risk(**changes):
            macro = copy.deepcopy(base)
            for key, update in changes.items():
                macro[key].update(update)
            return monitor.market_risk_summary(macro)
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'):
            self.assertEqual(risk()['level'], 'green')
            self.assertEqual(risk()['valid_count'], 5)
            self.assertEqual(risk(vix={'value': 25})['level'], 'yellow')
            self.assertEqual(risk(vix={'value': 45})['level'], 'yellow')
            self.assertEqual(risk(hy_oas={'value': 500}, nfci={'value': 0.1})['level'], 'yellow')
            self.assertEqual(risk(hy_oas={'value': 500}, vix={'value': 25})['level'], 'red')
            self.assertEqual(risk(hy_oas={'value': 380}, vix={'value': 25},
                                  breadth={'value': 40})['level'], 'red')
            self.assertEqual(risk(vix={'value': 25}, sp500={'drawdown': -12})['level'], 'yellow')
            self.assertEqual(risk(nfci={'ok': False})['level'], 'yellow')
            self.assertEqual(risk(nfci={'ok': False})['valid_count'], 4)
            self.assertEqual(risk(vix={'value': float('nan')})['level'], 'yellow')
            self.assertIn('VIX', risk(vix={'date': '2026-09-01'})['missing'])
            self.assertEqual(risk(nfci={'date': '2026-10-02'})['level'], 'green')
            self.assertEqual(monitor.market_risk_summary({})['label'], '数据不足')
            self.assertEqual(risk(hy_oas={'value': 500}, vix={'value': 25},
                                  nfci={'ok': False})['level'], 'red')
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), \
                patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot(base, [], {})
        self.assertEqual(snap['market_risk']['level'], 'green')
        page = monitor.render(base, [], 0, snapshot=snap)
        self.assertIn('id="marketRiskLight" data-level="green"', page)
        self.assertIn('有效指标 5/5', page)
        self.assertIn('id="marketRiskRules"', page)
        self.assertIn('同类指标不重复算作跨类确认', page)

    def test_market_breadth_validation_and_simple_risk_language(self):
        body = {'_canonical': monitor.BREADTH_URL,
                '_license': 'Data (/api/**.json): CC BY 4.0',
                'source': 'Member daily closes, current constituents', 'members': 503,
                'updated': '2026-10-06',
                'latest': {'date': '2026-10-06', 'pct50': 28.0, 'pct200': 29.0}}
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return body
        with patch.object(monitor.requests, 'get', return_value=Response()) as req:
            breadth = monitor.market_breadth('2026-10-08')
        self.assertEqual(req.call_count, 1)
        self.assertEqual(monitor.macro_status('breadth', breadth)[0], 'yellow')
        self.assertIn('过半股票跌破长期均线', monitor.macro_status('breadth', breadth)[1])
        breadth['pressure_confirmed'] = True
        self.assertEqual(monitor.macro_status('breadth', breadth)[0], 'red')
        body['latest']['pct200'] = 65
        with patch.object(monitor.requests, 'get', return_value=Response()):
            self.assertIn('短期上涨只由少数股票支撑',
                          monitor.macro_status('breadth', monitor.market_breadth('2026-10-08'))[1])
            self.assertEqual(monitor.macro_status('breadth', monitor.market_breadth('2026-10-08'))[0], 'yellow')
            self.assertFalse(monitor.market_breadth('2026-10-20')['ok'])
            body['latest']['pct50'] = 101
            self.assertFalse(monitor.market_breadth('2026-10-08')['ok'])
            body['latest']['pct50'] = 28
            body['updated'] = '2026-10-07'
            self.assertFalse(monitor.market_breadth('2026-10-08')['ok'])
            body['updated'] = '2026-10-06'
            body['_license'] = 'not reusable'
            self.assertFalse(monitor.market_breadth('2026-10-08')['ok'])
        with patch.object(monitor.requests, 'get', side_effect=monitor.requests.Timeout('offline')):
            self.assertEqual(monitor.macro_status('breadth', monitor.market_breadth('2026-10-08'))[0], 'gray')

    def test_macro_build_confirms_pressure_only_with_valid_indicators(self):
        body = {'_canonical': monitor.BREADTH_URL, '_license': 'CC BY 4.0',
                'source': 'Member daily closes, current constituents', 'members': 503,
                'updated': '2026-10-06',
                'latest': {'date': '2026-10-06', 'pct50': 35, 'pct200': 29}}
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return body
        nfci_date = '2026-10-02'
        def fred(series, days=400, alias=None, source_info=None):
            value = 0.1 if series == 'NFCI' else 3.0
            return [('2026-09-25', value), (nfci_date, value)]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), \
                patch.object(monitor, 'fred_series', side_effect=fred), \
                patch.object(monitor.requests, 'get', return_value=Response()):
            macro = monitor.build_macro()
            self.assertTrue(macro['breadth']['pressure_confirmed'])
            self.assertEqual(monitor.macro_status('breadth', macro['breadth'])[0], 'red')
            self.assertEqual(monitor.macro_status('nfci', macro['nfci'])[0], 'yellow')
            body['updated'] = body['latest']['date'] = '2026-10-02'
            delayed = monitor.build_macro()
            self.assertTrue(delayed['breadth']['ok'])
            self.assertFalse(delayed['breadth']['pressure_confirmed'])
            body['updated'] = body['latest']['date'] = '2026-10-06'
            nfci_date = '2026-09-01'
            stale = monitor.build_macro()
            self.assertFalse(stale['nfci']['ok'])
            self.assertFalse(stale['breadth']['pressure_confirmed'])
            self.assertEqual(monitor.macro_status('breadth', stale['breadth'])[0], 'yellow')

    def test_macro_collapses_and_shows_only_existing_alert_counts(self):
        quiet = monitor.render({}, [], 0)
        summary = quiet.split('id="macroCard"', 1)[1].split('</summary>', 1)[0]
        self.assertIn('<span>市场风险参考</span>', summary)
        self.assertNotIn('open', summary)
        self.assertNotIn('红', summary)
        self.assertNotIn('黄', summary)
        macro = {'hy_oas': {'ok': True, 'name': '垃圾债利差', 'value': 300, 'date': '2026-10-06'},
                 'nfci': {'ok': True, 'name': '金融压力', 'value': 0.12, 'date': '2026-10-02'},
                 'breadth': {'ok': True, 'name': '上涨参与度', 'value': 28.0,
                             'pct50': 18.0, 'date': '2026-10-06', 'pressure_confirmed': True}}
        page = monitor.render(macro, [], 0)
        summary = page.split('id="macroCard"', 1)[1].split('</summary>', 1)[0]
        self.assertIn('id="marketRiskLight" data-level="red"', summary)
        self.assertIn('综合：风险升高', summary)
        self.assertNotIn('红1 · 黄1', summary)
        self.assertIn('多数股票走弱，且信用或金融压力也在升高', page)
        self.assertIn('截至 2026-10-02', page)
        self.assertIn('History of Market (historyofmarket.com)', page)
        self.assertIn(monitor.BREADTH_CREDIT, page)

    def test_run_light_empty_and_unsupported(self):
        empty = monitor.render({}, [], 0, snapshot={'summary': {'total': 0}})
        self.assertIn('清单为空，未抓取报价', empty)
        self.assertIn('id="runLight" data-phase="idle"', empty)
        unsupported = monitor.render({}, [], 0, snapshot={'summary': {'total': 1, 'unsupported_symbols': ['.SPX']}})
        self.assertIn('1 个特殊代码暂不支持报价，未抓取报价', unsupported)
        self.assertNotIn('抓取成功', unsupported)
        duplicate_failure = monitor.render({}, [], 0, snapshot={'summary': {
            'total': 1, 'missing_symbols': ['AAPL'], 'stale_symbols': ['AAPL']}})
        self.assertIn('当日行情未取得 1 只：AAPL', duplicate_failure)

    def test_empty_render_and_safe_names(self):
        row = dict(symbol='MSFT', note='<公司 & 名称>', price=100, chg=0,
                   signals=[], level='green', source='test', group='technology')
        page = monitor.render({}, [row], 1)
        self.assertEqual(page.count('class="card group-card"'), 14)
        self.assertIn('IT软硬Ai (1)', page)
        self.assertIn('房地产 (0)', page)
        self.assertIn('&lt;公司 &amp; 名称&gt;', page)
        self.assertIn('无异动 1 只 · 点击查看', page)
        self.assertNotIn('其他关注', page)


if __name__ == '__main__':
    unittest.main()
