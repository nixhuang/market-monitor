"""离线分类模型、去重、备注及全部折叠标题回归。"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import monitor


class TestGroups(unittest.TestCase):
    def test_definitions_and_initial_config(self):
        labels = ['持仓', '重点关注', '指数基', 'IT软硬Ai', '医疗保健', '金融', '能源',
                  '工航防建机', '化材金纸', '公水电气', '必需消费品', '通信娱乐', '可选消费', '房地产']
        self.assertEqual([g['label'] for g in monitor.GROUPS], labels)
        with open(os.path.join(ROOT, 'holdings.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        self.assertNotIn('watch', cfg)
        # 清单由用户在设置页持续编辑（增删、移动分组），这里只校验结构，不再锁定当时的成员数量
        self.assertTrue(cfg['positions'])
        self.assertTrue(all(c.get('note') for c in cfg['positions'].values()))
        self.assertTrue(all(g['key'] in cfg for g in monitor.GROUPS))
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

    def test_instrument_priority_does_not_merge_etfs_with_indices(self):
        import copy
        cfg = {'positions': {'AAPL-US': {}, 'SPY': {}, 'BD#US10Y': {}},
               'focus': {'AAPL': {}, '.SPX': {}, '.VIX': {}, '.TNX': {}, 'QQQ': {}},
               'index_funds': {'^GSPC': {}, '^VIX': {}, '^TNX': {}, 'QQQ-US': {}, 'ESMAIN': {},
                               'ES=F': {}, '.NDX': {}, '^NDX': {}, 'HYG': {}},
               'group_monitoring': {'index_funds': True}}
        original = copy.deepcopy(cfg)
        universe, counts, duplicates = monitor.grouped_universe(cfg)
        self.assertEqual([s for s, _, _ in universe], ['AAPL-US', 'SPY', 'BD#US10Y', 'QQQ', 'ESMAIN', '.NDX', 'HYG'])
        self.assertEqual(cfg, original)
        self.assertEqual(counts['positions'], 3)
        self.assertEqual(counts['focus'], 1)
        self.assertGreater(duplicates, 5)
        self.assertNotEqual(monitor.instrument_identity('SPY'), monitor.instrument_identity('.SPX'))
        self.assertNotEqual(monitor.instrument_identity('ESMAIN'), monitor.instrument_identity('.SPX'))
        self.assertNotEqual(monitor.instrument_identity('HYG'), 'hy_oas')

    def test_macro_uses_new_yahoo_quotes_and_only_credit_spread_from_fred(self):
        from datetime import date, timedelta
        def bars(alias):
            prices = {'^VIX': 15.08, '^GSPC': 7801.77, '^TNX': 5.3}
            value = prices[alias]
            dates = [(date(2026, 10, 7) - timedelta(days=i)).isoformat() for i in range(39, -1, -1)]
            return {'dates': dates, 'closes': [value]*40, 'highs': [value]*40, 'lows': [value]*40,
                    'volumes': [0]*40, 'price':value, 'prev_close':value,
                    'quote_symbol':alias, 'quote_name':'Treasury Yield 10 Years' if alias == '^TNX' else alias}
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                patch.object(monitor, 'fred_series', return_value=[('2026-10-06', 3.0)]) as fred, \
                patch.object(monitor, 'yahoo_history', side_effect=bars) as yahoo, \
                patch.object(monitor, 'financial_conditions_series', return_value=[('2026-10-02', -0.3)]), \
                patch.object(monitor, 'market_breadth', return_value={'ok':True,'name':'上涨参与度',
                                    'date':'2026-10-06','value':65,'pct50':60}):
            macro = monitor.build_macro()
            self.assertEqual(fred.call_args_list, [unittest.mock.call('BAMLH0A0HYM2')])
            self.assertEqual([call.args[0] for call in yahoo.call_args_list], ['^VIX', '^GSPC', '^TNX'])
            self.assertEqual((macro['vix']['value'], macro['vix']['date']), (15.08, '2026-10-07'))
            cfg = {'index_funds': {'.VIX': {}, '.SPX': {}, 'SPY': {}}, 'group_monitoring':{'index_funds':True}}
            with patch.object(monitor, 'global_dca', return_value=None):
                snap = monitor.build_snapshot(macro, [], cfg)
            page = monitor.render(macro, [], 0, snapshot=snap)
        risk = page.split('id="macroCard"')[1].split('id="group_positions"')[0]
        self.assertIn('15.08', risk)
        self.assertIn('7,801.77', risk)
        self.assertIn('截至 2026-10-07', risk)
        self.assertIn('Yahoo ^VIX', risk)
        self.assertNotIn('.VIX</td>', page)
        self.assertNotIn('.SPX</td>', page)
        self.assertEqual(snap['risk_quotes']['vix']['value'], 15.08)

    def test_nfci_original_series_parser(self):
        class Response:
            text = 'Friday_of_Week,NFCI,ANFCI\n10/02/2026,-0.3,0\n10/09/2026,0.1,0\n'
            def raise_for_status(self):
                pass
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                patch.object(monitor.requests, 'get', return_value=Response()):
            self.assertEqual(monitor.financial_conditions_series(), [('2026-10-02', -0.3)])

    def test_special_assets_are_preserved_without_fake_stock_quotes(self):
        cfg = {'index_funds': {s: {'note': s} for s in ['.SPX', 'BD#US10Y', 'ESMAIN', 'CLMAIN', '2USDCNY', '2XAUUSD']},
               'group_monitoring': {'index_funds': True}}
        universe, counts, _ = monitor.grouped_universe(cfg)
        self.assertEqual(len(universe), 5)
        self.assertEqual(counts['index_funds'], 5)
        self.assertNotIn('.SPX', [s for s, _, _ in universe])
        self.assertTrue(all(monitor.quote_supported(s) for s, _, _ in universe[:3]))
        self.assertTrue(all(not monitor.quote_supported(s) for s, _, _ in universe[3:]))
        self.assertTrue(monitor.quote_supported('AAPL'))
        self.assertFalse(monitor.quote_supported('NQmain'))
        self.assertEqual(monitor.normalize_symbol('31#BRK.B'), 'BRK-B')
        rows = [dict(symbol=s, price=None, level='gray') for s, _, _ in universe]
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, rows, cfg)
        self.assertEqual(len(snap['summary']['missing_symbols']), 3)
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
        self.assertIn('BD#US10Y', index)
        self.assertNotIn('.VIX', index)
        self.assertNotIn('.SPX', index)
        self.assertIn('参考值 · 截至 2026-10-07', index)
        self.assertIn('<span class="px-price">4.25%</span>', index)
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
        self.assertIn('2 项数据已更新', page)
        self.assertNotIn('项参考值（截至', page)
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
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                    patch.object(monitor, 'fetch_history', return_value={'price': 100}) as fetch, \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan') as notify:
                self.assertEqual(monitor.main([]), 0)
                # 标普500指数日线和第二来源都取不到时，末尾才多抓一只 SPY 作近似基准；不进页面清单、不计入监测数量
                self.assertEqual([c.args[0] for c in fetch.call_args_list], ['AAPL', 'MSFT', 'XLK', 'XLB', 'SPY'])
                self.assertEqual([r['symbol'] for r in notify.call_args.args[1]], ['AAPL', 'MSFT', 'XLK', 'XLB'])
                with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                    self.assertEqual(json.load(f)['summary']['total'], 4)

    def _run_main_for_rs(self, bench_rows, fallback_rows=None):
        import tempfile
        cfg = {'technology': {'XLK': {}}}
        dates = [f'2026-09-{d:02d}' for d in range(1, 31)] + ['2026-10-01', '2026-10-02']
        fetched_symbols = []
        def hist(symbol, *a, **k):
            fetched_symbols.append(symbol)
            step = {'XLK': 1.0, 'SPY': 0.5}[symbol]
            return {'price': 100, 'dates': dates, 'closes': [100 + i * step for i in range(len(dates))]}
        def analyzed(symbol, settings, data, group):
            detail = dict(symbol=symbol, note='', price=100, chg=0, level='green',
                          group=group, signals=[], data_date='2026-10-02')
            return 'green', [], detail
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-02'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', list(bench_rows)), \
                    patch.object(monitor, 'sp500_rows_fallback', return_value=list(fallback_rows or [])) as fallback, \
                    patch.object(monitor, 'fetch_history', side_effect=hist), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan') as notify:
                self.assertEqual(monitor.main([]), 0)
                with open(os.path.join(directory, 'index.html'), encoding='utf-8') as f:
                    page = f.read()
                return notify.call_args.args[1][0].get('rs_spy'), page, fetched_symbols, fallback

    def test_main_relative_strength_uses_sp500_index_rows_without_fetching_spy(self):
        dates = [f'2026-09-{d:02d}' for d in range(1, 31)] + ['2026-10-01', '2026-10-02']
        index_rows = [(d, 5000 + i * 10) for i, d in enumerate(dates)]     # 标普500指数点位（约 5000 点）
        rs, page, fetched, fallback = self._run_main_for_rs(index_rows)
        self.assertEqual(fetched, ['XLK'])                  # 不再额外抓 SPY
        fallback.assert_not_called()                        # 第一来源可用就不碰第二来源
        self.assertEqual(rs['days'], 20)
        self.assertEqual(rs['bench_name'], '标普500')
        self.assertAlmostEqual(rs['etf'], (131 / 111 - 1) * 100, places=6)
        self.assertAlmostEqual(rs['bench'], (5310 / 5110 - 1) * 100, places=6)
        head = page.split('id="group_technology"', 1)[1].split('</summary>', 1)[0]
        self.assertIn('20日相对标普500', head)
        self.assertNotIn('SPY', head)
        self.assertIn('− 标普500 +3.9%', head)

    def test_main_relative_strength_second_source_when_yahoo_index_missing(self):
        dates = [f'2026-09-{d:02d}' for d in range(1, 31)] + ['2026-10-01']   # 第二来源晚一天
        hom_rows = [(d, 7000 + i * 5) for i, d in enumerate(dates)]
        rs, page, fetched, fallback = self._run_main_for_rs([], hom_rows)
        self.assertEqual(fetched, ['XLK'])
        fallback.assert_called_once()
        self.assertEqual(rs['bench_name'], '标普500')
        self.assertEqual(rs['end'], '2026-10-01')           # 终点退到双方共有的最新日，不按下标错位
        self.assertIn('20日相对标普500', page.split('id="group_technology"', 1)[1].split('</summary>', 1)[0])

    def test_main_relative_strength_falls_back_to_spy_only_when_no_index_data(self):
        rs, page, fetched, fallback = self._run_main_for_rs([], [])
        self.assertEqual(fetched, ['XLK', 'SPY'])
        self.assertEqual(rs['bench_name'], 'SPY')
        head = page.split('id="group_technology"', 1)[1].split('</summary>', 1)[0]
        self.assertIn('20日相对SPY', head)                   # 明确标注是 SPY 近似，不冒充指数

    def test_sp500_rows_fallback_validates_license_order_and_staleness(self):
        def body(rows, lic='CC BY 4.0', **extra):
            b = {'_license': lic, 'series': [{'date': d, 'close': c, 'drawdown': 0.0} for d, c in rows]}
            b.update(extra)
            return b
        def run(payload, target='2026-10-08'):
            resp = MagicMock()
            resp.json.return_value = payload
            with patch.object(monitor.requests, 'get', return_value=resp), patch.object(monitor, 'log'):
                return monitor.sp500_rows_fallback(target, keep=3)
        good = [('2026-10-01', 7700.0), ('2026-10-02', 7710.0), ('2026-10-05', 7720.0), ('2026-10-06', 7730.0), ('2026-10-07', 7740.0)]
        self.assertEqual(run(body(good)), good[-3:])
        self.assertEqual(run(body(good), target='2026-10-05'), good[:3][-3:])   # 不取目标日之后的数据
        self.assertEqual(run(body(good, lic='proprietary')), [])
        self.assertEqual(run(body(good + [('2026-10-07', 7750.0)])), [])         # 日期重复/倒序
        self.assertEqual(run(body(good[:2] + [('2026-10-05', -1.0)])), [])        # 价格异常
        self.assertEqual(run(body(good), target='2026-10-20'), [])                # 过旧（>7 天）
        self.assertEqual(run({'_license': 'CC BY 4.0', 'series': 'x'}), [])
        with patch.object(monitor.requests, 'get', side_effect=monitor.requests.ConnectionError('x')), patch.object(monitor, 'log'):
            self.assertEqual(monitor.sp500_rows_fallback('2026-10-08'), [])

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
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_macro', return_value=macro), \
                    patch.object(monitor, 'fetch_history', return_value=None) as fetch, \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'push_serverchan'):
                self.assertEqual(monitor.main([]), 0)
            self.assertEqual([c.args[0] for c in fetch.call_args_list], ['.NDX'])
            with open(os.path.join(directory, 'index.html'), encoding='utf-8') as f:
                page = f.read()
            with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                snap = json.load(f)
        index = page.split('id="group_index_funds"', 1)[1].split('id="group_technology"', 1)[0]
        self.assertIn('4.25%', index)
        self.assertNotIn('.VIX', index)
        self.assertNotIn('.SPX', index)
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
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
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
            clock = {'t': 1000.0}
            def slow_fail(symbol):
                fetched.append(symbol)
                clock['t'] += 481  # 第一只耗尽整个总预算
                return None
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'fetch_history', side_effect=slow_fail), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'push_serverchan'), \
                    patch.object(monitor.time, 'monotonic', side_effect=lambda: clock['t']):
                self.assertEqual(monitor.main([]), 0)
                with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                    snap = json.load(f)
                with open(os.path.join(directory, 'index.html'), encoding='utf-8') as f:
                    page = f.read()
            self.assertEqual(fetched, ['FIRST'])
            self.assertEqual(snap['summary']['total'], 3)
            self.assertIn('本轮抓取时间预算不足，尚未取数', page)

    def test_concurrent_quotes_keep_order_and_single_symbol_budget(self):
        import time as real_time
        order = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']
        def fetch(symbol):
            real_time.sleep(0.02 if symbol in 'AC' else 0)  # 完成顺序故意打乱
            return {'price': ord(symbol)}
        with patch.object(monitor, 'fetch_history', side_effect=fetch), patch.object(monitor, 'QUOTE_WORKERS', 4):
            result = monitor.collect_quotes(order)
        self.assertEqual(list(result), order)
        self.assertEqual([result[s]['price'] for s in order], [ord(s) for s in order])
        # 单只卡死只吃自己的限额，不吃总预算
        with patch.object(monitor, 'QUOTE_SYMBOL_SECONDS', 5):
            monitor._QUOTE_TLS.deadline = monitor.time.monotonic() + 0.01
            real_time.sleep(0.02)
            with self.assertRaises(TimeoutError):
                monitor.quote_timeout(8)
            monitor._QUOTE_TLS.deadline = None
            self.assertEqual(monitor.quote_timeout(8), 8)

    def test_failed_symbols_retry_once_and_exhausted_is_distinct(self):
        calls = {}
        def fetch(symbol):
            calls[symbol] = calls.get(symbol, 0) + 1
            if symbol == 'FLAKY' and calls[symbol] == 1:
                return None
            return None if symbol == 'DEAD' else {'price': 1}
        with patch.object(monitor, 'fetch_history', side_effect=fetch), patch.object(monitor.time, 'sleep'), \
                patch.object(monitor, 'QUOTE_WORKERS', 2):
            result = monitor.collect_quotes(['OK', 'FLAKY', 'DEAD'])
        self.assertEqual(result['FLAKY'], {'price': 1})
        self.assertIsNone(result['DEAD'])
        self.assertEqual((calls['OK'], calls['FLAKY'], calls['DEAD']), (1, 2, 2))
        with patch.object(monitor, 'QUOTE_DEADLINE', monitor.time.monotonic() - 1):
            self.assertIs(monitor._fetch_quote('X'), monitor.QUOTE_EXHAUSTED)

    def test_source_gate_only_applies_to_batch_stage(self):
        gate = monitor.SourceGate(threshold=2, cooldown=60)
        self.assertFalse(gate.fail())
        self.assertTrue(gate.allow())
        gate.reset(True)
        self.assertFalse(gate.fail())
        self.assertTrue(gate.fail())
        self.assertFalse(gate.allow())
        gate.reset(False)
        self.assertTrue(gate.allow())

    def test_earnings_within_two_weeks_is_highlighted(self):
        soon = lambda d, today='2026-10-09', status='ok': monitor.earnings_soon(
            {'status': status, 'date': d, 'timing': 'post', 'kind': 'expected'}, today)
        self.assertTrue(soon('2026-10-09'))      # 当天
        self.assertTrue(soon('2026-10-23'))      # 第 14 天，含
        self.assertFalse(soon('2026-10-24'))     # 第 15 天
        self.assertFalse(soon('2026-10-08'))     # 已过期不算
        self.assertFalse(soon('2026-10-12', status='unknown'))
        self.assertFalse(monitor.earnings_soon(None, '2026-10-09'))
        self.assertFalse(monitor.earnings_soon({'status': 'error'}, '2026-10-09'))
        self.assertFalse(monitor.earnings_soon({'status': 'ok', 'date': 'bad'}, '2026-10-09'))

    def test_earnings_text_never_uses_past_dates_or_invented_times(self):
        parse = monitor.parse_earnings_text
        ok = parse('Apple Inc. is expected* to report earnings on 10/30/2026 after market close.', '2026-10-09')
        self.assertEqual(ok, {'status': 'ok', 'date': '2026-10-30', 'timing': 'post', 'kind': 'expected'})
        est = parse('Tesla is estimated to report earnings on 10/21/2026 before market open', '2026-10-09')
        self.assertEqual((est['timing'], est['kind']), ('pre', 'estimated'))
        self.assertEqual(parse('X is expected to report earnings on 10/09/2026', '2026-10-09')['status'], 'ok')
        self.assertEqual(parse('X is expected to report earnings on 10/08/2026 after market close', '2026-10-09'),
                         {'status': 'unknown'})
        self.assertEqual(parse('', '2026-10-09'), {'status': 'unknown'})
        self.assertEqual(parse('X will report on 13/45/2026', '2026-10-09'), {'status': 'unknown'})
        self.assertEqual(monitor.earnings_label(None), None)
        self.assertEqual(monitor.earnings_label({'status': 'na'}), None)
        self.assertEqual(monitor.earnings_label({'status': 'unknown'})[0], '时间待公布')
        self.assertIn('不代表没有财报', monitor.earnings_label({'status': 'error'})[1])
        text, note = monitor.earnings_label(dict(ok, date='2026-10-30'))
        self.assertIn('美东 10/30 盘后', text)
        self.assertNotIn('财报', text)   # 「财报」二字只写在表头
        self.assertIn('北京时间约10/31凌晨', note)
        self.assertNotIn('不含具体钟点', note)
        self.assertNotIn('数据源只给', note)
        self.assertIn('具体时段待定', monitor.earnings_label({'status': 'ok', 'date': '2026-10-30', 'timing': '', 'kind': 'expected'})[0])

    def test_earnings_shown_for_holdings_and_focus_but_others_only_when_alerted(self):
        def row(symbol, group, level):
            return {'symbol': symbol, 'group': group, 'level': level, 'price': 10, 'note': ''}
        items = [row('AAPL', 'position', 'green'), row('MSFT', 'focus', 'green'),
                 row('NVDA', 'technology', 'green'), row('AMD', 'technology', 'yellow'),
                 row('XLK', 'technology', 'red'), row('SPY', 'position', 'green'),
                 row('.VIX', 'index_funds', 'red')]
        asked = []
        def earnings(symbol):
            asked.append(symbol)
            return {'status': 'ok', 'date': '2026-10-30', 'timing': 'post', 'kind': 'expected'}
        with patch.object(monitor, 'nasdaq_earnings', side_effect=earnings):
            stat = monitor.attach_earnings(items)
        self.assertEqual(sorted(asked), ['AAPL', 'AMD', 'MSFT'])
        self.assertEqual(stat['targets'], 3)
        by_symbol = {d['symbol']: d for d in items}
        self.assertIn('earnings', by_symbol['AAPL'])
        self.assertNotIn('earnings', by_symbol['NVDA'])
        self.assertNotIn('earnings', by_symbol['XLK'])
        self.assertNotIn('earnings', by_symbol['SPY'])
        self.assertNotIn('earnings', by_symbol['.VIX'])
        with patch.object(monitor, 'nasdaq_earnings', side_effect=RuntimeError('boom')):
            stat = monitor.attach_earnings([row('AAPL', 'position', 'green')])
        self.assertEqual(stat['targets'], 1)

    def test_quiet_holdings_keep_earnings_inside_folded_list(self):
        quiet = {'symbol': 'AAPL', 'note': '苹果', 'price': 100, 'chg': 0.0, 'rsi': {}, 'dist_high': None,
                 'dist_low': None, 'vol_ratio': None, 'trigger': None, 'group': 'position',
                 'data_date': '2026-10-07', 'boll_up': None, 'boll_dn': None, 'signals': [], 'level': 'green',
                 'earnings': {'status': 'ok', 'date': '2026-10-30', 'timing': 'post', 'kind': 'expected'}}
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, [quiet], {'positions': {'AAPL': {}}})
        page = monitor.render({}, [quiet], 1, snapshot=snap)
        position = page.split('id="group_positions"', 1)[1].split('id="group_focus"', 1)[0]
        self.assertIn('无异动 1 只', position)
        folded = position.split('无异动 1 只', 1)[1]
        self.assertIn('美东 10/30 盘后', folded)
        self.assertNotIn('财报 美东', folded)

    def test_etf_and_stocks_share_one_table_sorted_red_yellow_green(self):
        def mk(symbol, level, chg):
            return {'symbol': symbol, 'note': symbol, 'price': 10, 'chg': chg, 'rsi': {}, 'dist_high': None,
                    'dist_low': None, 'vol_ratio': None, 'trigger': None, 'group': 'position',
                    'data_date': '2026-10-07', 'boll_up': None, 'boll_dn': None,
                    'signals': ['x'] if level != 'green' else [], 'level': level}
        items = [mk('AAPL', 'yellow', 3.0), mk('XLK', 'red', 4.0), mk('NVDA', 'red', -9.0), mk('QQQ', 'yellow', 6.0)]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, items, {'positions': {i['symbol']: {} for i in items}})
        page = monitor.render({}, items, 4, snapshot=snap)
        position = page.split('id="group_positions"', 1)[1].split('id="group_focus"', 1)[0]
        self.assertNotIn('<h2>', position)
        self.assertEqual(position.count('<table'), 1)          # 个股和 ETF 同一张表
        # 持仓组：红(NVDA 个股在 XLK ETF 前) → 黄(AAPL 个股在 QQQ ETF 前)；同级 ETF 排最后
        order = [position.index(f'<td class="sym">{s}') for s in ('NVDA', 'XLK', 'AAPL', 'QQQ')]
        self.assertEqual(order, sorted(order))

    def test_position_etf_last_per_level_and_sector_etf_first_only_when_alerting(self):
        def mk(symbol, level, chg, group):
            return {'symbol': symbol, 'note': symbol, 'price': 10, 'chg': chg, 'rsi': {}, 'dist_high': None,
                    'dist_low': None, 'vol_ratio': None, 'trigger': None, 'group': group,
                    'data_date': '2026-10-07', 'boll_up': None, 'boll_dn': None,
                    'signals': ['x'] if level != 'green' else [], 'level': level}
        items = [mk('SPYM', 'yellow', 9.0, 'position'), mk('AAPL', 'yellow', 1.0, 'position'),
                 mk('QQQM', 'red', 8.0, 'position'), mk('NVDA', 'red', 2.0, 'position'),
                 mk('XLK', 'green', 0.1, 'technology'), mk('MU', 'red', 5.0, 'technology'),
                 mk('AMD', 'yellow', 7.0, 'technology'), mk('SNOW', 'green', 0.2, 'technology')]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, items, {'positions': {'SPYM': {}, 'AAPL': {}, 'QQQM': {}, 'NVDA': {}}})
        page = monitor.render({}, items, 8, snapshot=snap)
        pos = page.split('id="group_positions"', 1)[1].split('id="group_focus"', 1)[0]
        order = [pos.index(f'<td class="sym">{s}') for s in ('NVDA', 'QQQM', 'AAPL', 'SPYM')]
        self.assertEqual(order, sorted(order))      # 红：个股 NVDA 在 ETF QQQM 前；黄：AAPL 在 SPYM 前
        tech = page.split('id=\"group_technology\"', 1)[1].split('id=\"group_healthcare\"', 1)[0]
        main, quiet = tech.split('<details class=\"quiet-list\">', 1)
        # 板块 ETF 绿灯无警示：不在主表，照常折叠
        self.assertNotIn('<td class=\"sym\">XLK', main)
        self.assertIn('<td class=\"sym\">XLK', quiet)
        self.assertIn('无异动 2 只', tech)
        # 板块 ETF 有警示时：排在同一预警级别第一位（红里 XLK 在 MU 前，不越过更高级别）
        items2 = [mk('XLK', 'red', 1.0, 'technology'), mk('MU', 'red', 5.0, 'technology'),
                  mk('AMD', 'yellow', 7.0, 'technology'), mk('NVDA2', 'yellow', 9.0, 'technology')]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'global_dca', return_value=None):
            snap2 = monitor.build_snapshot({}, items2, {'positions': {}})
        tech2 = monitor.render({}, items2, 4, snapshot=snap2).split('id=\"group_technology\"', 1)[1].split('id=\"group_healthcare\"', 1)[0]
        order = [tech2.index(f'<td class=\"sym\">{s}') for s in ('XLK', 'MU', 'NVDA2', 'AMD')]
        self.assertEqual(order, sorted(order))
        items3 = [mk('XLK', 'yellow', 0.5, 'technology'), mk('MU', 'red', 5.0, 'technology'), mk('AMD', 'yellow', 7.0, 'technology')]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'global_dca', return_value=None):
            snap3 = monitor.build_snapshot({}, items3, {'positions': {}})
        tech3 = monitor.render({}, items3, 3, snapshot=snap3).split('id=\"group_technology\"', 1)[1].split('id=\"group_healthcare\"', 1)[0]
        order = [tech3.index(f'<td class=\"sym\">{s}') for s in ('MU', 'XLK', 'AMD')]
        self.assertEqual(order, sorted(order))      # 黄档 XLK 排黄档第一，仍在红档 MU 之后
        self.assertNotIn('财报 "', page)             # 手机端不再用 CSS 给每行补「财报」

    def test_relative_strength_vs_sp500_aligned_by_date_and_shown_after_lights(self):
        def hist(start_day, closes):
            dates = [f'2026-09-{d:02d}' if d <= 30 else f'2026-10-{d - 30:02d}' for d in range(start_day, start_day + len(closes))]
            return {'dates': dates, 'closes': closes}
        etf = hist(1, [100.0] * 5 + [100.0 + i for i in range(1, 22)])      # 26 根：起点 101、终点 121
        spy = hist(1, [100.0] * 5 + [100.0 + i * 0.5 for i in range(1, 22)])  # 同日期：起点 100.5、终点 110.5
        rs = monitor.relative_strength(etf, spy)
        self.assertEqual(rs['days'], 20)
        self.assertEqual((rs['start'], rs['end']), ('2026-09-06', '2026-09-26'))
        self.assertAlmostEqual(rs['etf'], (121 / 101 - 1) * 100, places=6)
        self.assertAlmostEqual(rs['bench'], (110.5 / 100.5 - 1) * 100, places=6)
        self.assertEqual(monitor.relative_strength(etf, spy, bench_name='SPY')['bench_name'], 'SPY')
        self.assertAlmostEqual(rs['diff'], rs['etf'] - rs['bench'], places=6)
        # SPY 少最后一天：终点退到双方共有的最新日，不能按下标错位
        short = {'dates': spy['dates'][:-1], 'closes': spy['closes'][:-1]}
        rs2 = monitor.relative_strength(etf, short)
        self.assertEqual(rs2['end'], '2026-09-25')
        self.assertAlmostEqual(rs2['etf'], (120 / 100 - 1) * 100, places=6)
        # 数据不足 / 起点 SPY 缺失 / 价格异常：一律不显示
        self.assertIsNone(monitor.relative_strength(hist(1, [100.0] * 20), spy))
        self.assertIsNone(monitor.relative_strength(etf, {'dates': spy['dates'][8:], 'closes': spy['closes'][8:]}))
        self.assertIsNone(monitor.relative_strength({**etf, 'closes': etf['closes'][:5] + [0.0] + etf['closes'][6:]}, spy))
        self.assertIsNone(monitor.relative_strength(etf, {}))

        def mk(symbol, level, group, **extra):
            d = {'symbol': symbol, 'note': symbol, 'price': 10, 'chg': 0.0, 'rsi': {}, 'dist_high': None,
                 'dist_low': None, 'vol_ratio': None, 'trigger': None, 'group': group,
                 'data_date': '2026-10-07', 'boll_up': None, 'boll_dn': None,
                 'signals': ['x'] if level != 'green' else [], 'level': level}
            d.update(extra)
            return d
        up = {'days': 20, 'etf': 5.2, 'bench': 3.4, 'bench_name': '标普500', 'diff': 1.8, 'start': '2026-09-08', 'end': '2026-10-07'}
        down = {'days': 20, 'etf': -1.0, 'bench': 2.0, 'bench_name': '标普500', 'diff': -3.0, 'start': '2026-09-08', 'end': '2026-10-07'}
        flat = {'days': 20, 'etf': 2.2, 'bench': 2.0, 'bench_name': '标普500', 'diff': 0.2, 'start': '2026-09-08', 'end': '2026-10-07'}
        items = [mk('XLK', 'green', 'technology', rs_spy=up), mk('MU', 'red', 'technology'),
                 mk('XLV', 'green', 'healthcare', rs_spy=down), mk('XLF', 'green', 'financials', rs_spy=flat),
                 mk('XLE', 'green', 'energy'),          # 抓不到基准对比：不显示
                 mk('XLI', 'green', 'position', rs_spy=up)]   # 板块 ETF 因去重落在持仓里：行业分组头仍要显示
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, items, {'positions': {'XLI': {}}})
        page = monitor.render({}, items, len(items), snapshot=snap)
        def head(key):
            return page.split(f'id="group_{key}"', 1)[1].split('</summary>', 1)[0]
        tech = head('technology')
        self.assertIn('20日相对标普500 +1.8%', tech)
        self.assertIn('class="rs-spy up"', tech)
        self.assertLess(tech.index('stat-dot red'), tech.index('rs-spy'))      # 在红黄灯后面
        self.assertIn('XLK 20日 +5.2% − 标普500 +3.4% = +1.8 个百分点', tech)
        self.assertIn('class="rs-spy down"', head('healthcare'))
        self.assertIn('20日相对标普500 -3.0%', head('healthcare'))
        self.assertIn('class="rs-spy "', head('financials'))                    # ±0.5 内持平不着色
        self.assertNotIn('rs-spy', head('energy'))
        self.assertIn('20日相对标普500 +1.8%', head('industrials'))
        self.assertNotIn('rs-spy', head('positions'))                            # 持仓/关注/指数基不是板块组

    def test_us_style_colors_green_up_red_down_but_risk_lights_unchanged(self):
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), patch.object(monitor, 'global_dca', return_value=None):
            page = monitor.render({}, [], 0, snapshot=monitor.build_snapshot({}, [], {}))
        self.assertIn('--up:#3fb950; --down:#f85149;', page)
        self.assertIn('--green:#3fb950; --yellow:#d29922; --red:#f85149;', page)

    def test_fundamental_units_periods_and_oneoff_base(self):
        def table(rows):
            return {k: dict(zip(('value2', 'value5'), v)) for k, v in rows.items()}
        facts = {'period': '6/30/2026', 'base_period': '9/30/2025',
                 'inc': table({'Total Revenue': ('$1,000', '$1,000'), 'Operating Income': ('$100', '$100'),
                               'Gross Profit': ('$500', '$500'), 'Net Income': ('$90', '$900'),
                               'Income Tax': ('$10', '-$4,000')}),
                 'bs': table({'Total Assets': ('$10,000', '$10,000'), 'Total Liabilities': ('$5,000', '$5,000'),
                              'Total Equity': ('$5,000', '$5,000')}),
                 'cf': table({'Net Cash Flow-Operating': ('$200', '$200')}),
                 'rt': table({'Current Ratio': ('84.25%', '114.00%'), 'After Tax ROE': ('8.8%', '23.6%'),
                              'Operating Margin': ('10.0%', '10.0%'), 'Gross Margin': ('50.0%', '50.0%'),
                              'Profit Margin': ('9.0%', '90.0%')})}
        with patch.object(monitor, 'nasdaq_financials', return_value=facts):
            result = monitor.fundamental_check('UBER')
        text = ' '.join(result['hits'])
        self.assertIn('流动比率 1.14→0.84倍', text)
        self.assertIn('2025/9→2026/6', text)
        self.assertNotIn('114→84', text)
        self.assertNotIn('ROE', text.replace('ROE未比较', ''))  # 基准季净利含一次性，ROE不比较
        self.assertIn('ROE未比较', text)
        self.assertEqual(result['compare'], '2025/9→2026/6')
        self.assertTrue(any('营业利率' in c for c in result['context']))

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

    def test_moving_average_periods_are_configurable_and_validated(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for values in ({'ma_short': 20, 'ma_long': 250},
                           {'ma_short': 50.5, 'ma_long': 999},
                           {'ma_short': 200, 'ma_long': 50}):
                with open(os.path.join(directory, 'settings.json'), 'w', encoding='utf-8') as f:
                    json.dump(values, f)
                with patch.object(monitor, 'BASE', directory):
                    loaded = monitor.load_settings()
                expected = (20, 250) if values['ma_short'] == 20 else (50, 200)
                self.assertEqual((loaded['ma_short'], loaded['ma_long']), expected)
        self.assertTrue(monitor.valid_settings(dict(monitor.DEFAULT_SETTINGS, ma_long=200.5)))
        closes = [100.0]*249 + [99.0, 101.0]
        data = {'closes':closes, 'highs':closes[:], 'lows':closes[:], 'volumes':[0]*len(closes),
                'price':101.0, 'prev_close':99.0}
        settings = dict(monitor.S, ma_short=20, ma_long=250)
        with patch.dict(monitor.S, settings):
            _, signals, _ = monitor.analyze_symbol('AAPL', {}, data)
            self.assertIn('上穿20日均线', signals)
            self.assertIn('上穿250日均线', signals)
            self.assertNotIn('上穿200日均线', signals)
        with patch.dict(monitor.S, dict(settings, ma_short=250)):
            _, signals, _ = monitor.analyze_symbol('AAPL', {}, data)
            self.assertEqual(signals.count('上穿250日均线'), 1)
        data['closes'] = closes[-30:]
        data['highs'] = data['lows'] = data['closes'][:]
        data['volumes'] = [0]*30
        with patch.dict(monitor.S, settings):
            _, signals, _ = monitor.analyze_symbol('AAPL', {}, data)
            self.assertNotIn('上穿250日均线', signals)

    def test_longer_history_keeps_52_week_low_window(self):
        from datetime import date, timedelta
        dates = [(date(2026, 10, 7) - timedelta(days=i)).isoformat() for i in range(499, -1, -1)]
        closes = [100]*500
        highs = [100]*500; lows = [100]*500
        highs[0] = 300; lows[0] = 1
        data = {'dates':dates, 'closes':closes, 'highs':highs, 'lows':lows,
                'volumes':[0]*500, 'price':100, 'prev_close':100}
        _, _, detail = monitor.analyze_symbol('AAPL', {}, data)
        self.assertEqual((detail['dist_high'], detail['dist_low']), (0, 0))

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

    def test_macro_retries_yahoo_series_once_before_giving_up(self):
        from datetime import date, timedelta
        rows = [((date(2026, 10, 7) - timedelta(days=i)).isoformat(), 15.0) for i in range(39, -1, -1)]
        attempts = []
        def flaky(alias, days=400):
            attempts.append(alias)
            return [] if attempts.count(alias) == 1 else rows
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'MACRO_RETRY_WAIT', 0), \
                patch.object(monitor, 'fred_series', return_value=[]), \
                patch.object(monitor, 'market_index_series', side_effect=flaky), \
                patch.object(monitor, 'treasury_yield_series', return_value=[]), \
                patch.object(monitor, 'financial_conditions_series', return_value=[]), \
                patch.object(monitor, 'market_breadth', return_value={'ok': False, 'name': '上涨参与度'}):
            macro = monitor.build_macro()
        self.assertEqual(attempts, ['^VIX', '^VIX', '^GSPC', '^GSPC'])
        self.assertTrue(macro['vix']['ok'] and macro['sp500']['ok'])
        self.assertEqual(macro['vix']['date'], '2026-10-07')

    def test_macro_old_values_are_gray_and_yahoo_fallback_uses_real_dates(self):
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'MACRO_RETRY_WAIT', 0), \
                patch.object(monitor, 'fred_series', return_value=[('2026-09-01', 2), ('2026-09-02', 2)]), \
                patch.object(monitor, 'market_index_series', return_value=[]), \
                patch.object(monitor, 'treasury_yield_series', return_value=[]), \
                patch.object(monitor, 'financial_conditions_series', return_value=[]), \
                patch.object(monitor, 'market_breadth', return_value={'ok':False,'name':'上涨参与度'}):
            macro = monitor.build_macro()
        for key in ('hy_oas','vix','sp500','nfci','ust10'):
            self.assertEqual(monitor.macro_status(key, macro[key])[0], 'gray')
        rows = monitor._rows_from_closes([101,102], dates=['2026-10-02','2026-10-05'])
        self.assertEqual(rows, [('2026-10-02',101),('2026-10-05',102)])
        self.assertEqual(monitor._rows_from_closes([101,102], dates=None), [])
        self.assertEqual(monitor._rows_from_closes([101,102], dates=['2026-10-02']), [])

    def test_ust10_month_change_is_reference_only(self):
        # 一个月前取「30 天前（含）最近一个交易日」，不够长返回 None
        rows = [('2026-09-04', 4.00), ('2026-09-05', 4.02), ('2026-09-30', 4.20), ('2026-10-05', 4.55)]
        base, delta = monitor.month_change(rows)
        self.assertEqual(base, 4.02)                      # cutoff=09-05
        self.assertAlmostEqual(delta, 0.53)
        self.assertEqual(monitor.month_change([('2026-10-01', 4.0), ('2026-10-05', 4.1)]), (None, None))
        self.assertEqual(monitor.month_change([('2026-10-05', 4.1)]), (None, None))
        st = lambda dm: monitor.macro_status('ust10', {'ok': True, 'value': 4.5, 'delta_month': dm})
        self.assertEqual(st(0.50)[0], 'yellow')
        self.assertIn('急升', st(0.50)[1])
        self.assertEqual(st(0.49)[0], 'green')
        self.assertIn('偏快', st(0.30)[1])
        self.assertIn('平稳', st(0.29)[1])
        self.assertIn('平稳', st(-0.80)[1])
        self.assertEqual(st(None)[0], 'gray')
        # 参考行不进综合灯：10Y 急升时 risk 的得分、有效指标数都不变
        macro = {k: {'ok': True, 'name': k, 'value': 1.0, 'date': '2026-10-06'}
                 for k in ('hy_oas', 'vix', 'nfci')}
        macro['sp500'] = {'ok': True, 'name': 'sp', 'value': 1.0, 'date': '2026-10-06', 'drawdown': -1.0}
        macro['breadth'] = {'ok': True, 'name': 'b', 'value': 60.0, 'pct50': 60.0, 'date': '2026-10-06'}
        base_risk = monitor.market_risk_summary(macro, '2026-10-06')
        macro['ust10'] = {'ok': True, 'value': 5.5, 'date': '2026-10-06', 'delta_month': 1.0}
        risk = monitor.market_risk_summary(macro, '2026-10-06')
        self.assertEqual((risk['score'], risk['level'], risk['valid_count']),
                         (base_risk['score'], base_risk['level'], 5))
        self.assertNotIn('ust10', risk['levels'])

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
        rows, info, api, csv, quote = fetch(fresh, yahoo())
        self.assertEqual(rows[-1], ('2026-10-07', 4.3))
        api.assert_not_called(); csv.assert_not_called(); quote.assert_called_once()
        rows, info, _, _, _ = fetch(old, yahoo('2026-10-06'))
        self.assertEqual(rows[-1], ('2026-10-06', 4.3))
        self.assertTrue(info['lagging'])
        for data in (None, yahoo(value=43.0),
                     dict(yahoo(), quote_symbol='SPY'), dict(yahoo(), quote_name='S&P 500'),
                     dict(yahoo(), price=float('nan')),
                     dict(yahoo(), dates=['2026-10-07']*40)):
            with self.subTest(data=data):
                rows, info, _, _, _ = fetch(old, data)
                self.assertEqual(rows, [])
                self.assertEqual(info['source'], 'Yahoo ^TNX')
        rows, info, _, _, _ = fetch(old, None, monitor.requests.Timeout('offline'))
        self.assertEqual((rows, info), ([], {}))
        rows, info, _, _, _ = fetch(old, yahoo('2026-10-08'))
        self.assertEqual(rows[-1], ('2026-10-07', 4.3))
        rows, info, _, _, _ = fetch(old, dict(yahoo(), quote_name=''))
        self.assertEqual(info['source'], 'Yahoo ^TNX')
        rows, info, _, _, _ = fetch([], dict(yahoo(), quote_name=''))
        self.assertEqual(info['source'], 'Yahoo ^TNX')
        rows, info, _, _, _ = fetch([], yahoo())
        self.assertEqual(info['source'], 'Yahoo ^TNX')
        rows, info, _, _, _ = fetch([], dict(yahoo(), quote_name='10-Year Bond'))
        self.assertEqual(rows[-1], ('2026-10-07', 4.3))
        self.assertEqual(info['source'], 'Yahoo ^TNX')
        rows, info, _, _, _ = fetch([], None)
        self.assertEqual(rows, [])
        self.assertIn('reason', info)
        rows, info, _, _, _ = fetch([], dict(yahoo(), highs=[1.0]*40))
        self.assertEqual(info['source'], 'Yahoo ^TNX')
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
            self.assertEqual(monitor.macro_status('hy_oas', {'ok': True, 'value': 320, 'delta_week': 30})[0], 'yellow')
            self.assertEqual(monitor.macro_status('hy_oas', {'ok': True, 'value': 320, 'delta_week': 29})[0], 'green')
            self.assertIn('水平不高', monitor.macro_status('hy_oas', {'ok': True, 'value': 320, 'delta_week': 60})[1])
            self.assertNotIn('水平不高', monitor.macro_status('hy_oas', {'ok': True, 'value': 360, 'delta_week': 60})[1])
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
        # 风险卡里不出现美元指数、收益率曲线；10Y美债只作为「参考 · 不计入综合灯」的参考行出现
        for name in ('美元指数','收益率曲线'):
            self.assertNotIn(name, risk)
        self.assertEqual(risk.count('id="ust10Ref"'), 1)
        self.assertIn('参考 · 不计入综合灯', risk)
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
            self.assertEqual(risk(vix={'value': 25})['level'], 'green')   # 情绪黄 1 分：绿灯+轻微提示
            self.assertEqual(risk(vix={'value': 45})['level'], 'yellow')  # 情绪红 2 分
            self.assertEqual(risk(hy_oas={'value': 500}, nfci={'value': 0.1})['level'], 'yellow')
            self.assertEqual(risk(hy_oas={'value': 500}, vix={'value': 25})['level'], 'red')
            # 严重度计分：信用黄2 + 情绪黄1 + 趋势黄1 = 4 → 黄（不再三类各黄就升红）
            self.assertEqual(risk(hy_oas={'value': 380}, vix={'value': 25},
                                  breadth={'value': 40})['level'], 'yellow')
            self.assertEqual(risk(hy_oas={'value': 380}, vix={'value': 25},
                                  breadth={'value': 40})['score'], 4)
            # 信用红4 + 趋势黄1 = 5 → 红；信用红单独4 → 黄
            self.assertEqual(risk(hy_oas={'value': 500}, breadth={'value': 40})['level'], 'red')
            self.assertEqual(risk(hy_oas={'value': 500})['level'], 'yellow')
            # 只有一个情绪/趋势黄（1分）→ 绿灯 + 轻微提示
            weak = risk(breadth={'value': 40})
            self.assertEqual((weak['level'], weak['score'], weak['label']), ('green', 1, '风险平稳'))
            self.assertIn('轻微提示：上涨参与度', weak['text'])
            self.assertEqual(risk(vix={'value': 25})['level'], 'green')
            # 同类只取最高分：垃圾债黄 + 金融压力黄 仍是 2 分
            self.assertEqual(risk(hy_oas={'value': 380}, nfci={'value': 0.2})['score'], 2)
            # 垃圾债一周走阔 30bp → 黄（信用类 2 分 → 综合黄）
            wk = risk(hy_oas={'delta_week': 35})
            self.assertEqual((wk['levels']['hy_oas'], wk['level'], wk['score']), ('yellow', 'yellow', 2))
            self.assertEqual(risk(hy_oas={'delta_week': 29})['level'], 'green')
            self.assertEqual(risk(hy_oas={'delta_week': 55})['levels']['hy_oas'], 'red')
            # 缺失数据最低为黄
            self.assertEqual(risk(nfci={'ok': False})['level'], 'yellow')
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
        self.assertIn('同类只取最高分，不重复计分', page)
        self.assertIn('风险得分 0/8', page)

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
        with patch.object(monitor, 'TARGET_DATE', '2026-10-08'), patch.object(monitor, 'MACRO_RETRY_WAIT', 0), \
                patch.object(monitor, 'fred_series', side_effect=fred), \
                patch.object(monitor, 'market_index_series', return_value=[]), \
                patch.object(monitor, 'treasury_yield_series', return_value=[]), \
                patch.object(monitor, 'financial_conditions_series', side_effect=lambda: fred('NFCI')), \
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
        macro = {'hy_oas': {'ok': True, 'name': '垃圾债利差', 'value': 450, 'date': '2026-10-06'},
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
