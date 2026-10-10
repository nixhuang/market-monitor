"""离线分类模型、去重、备注及全部折叠标题回归。"""
import json
import re
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
        self.assertEqual([s for s, _, _ in universe], ['AAPL-US', 'SPY', 'QQQ', 'ESMAIN', '.NDX', 'HYG'])   # BD#US10Y 已在风险参考里，跳过
        self.assertEqual(cfg, original)
        self.assertEqual(counts['positions'], 2)
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
        self.assertEqual(len(universe), 4)
        self.assertEqual(counts['index_funds'], 4)
        self.assertNotIn('.SPX', [s for s, _, _ in universe])
        self.assertNotIn('BD#US10Y', [s for s, _, _ in universe])      # 10Y 美债已在风险参考里，不再重复
        self.assertTrue(all(monitor.quote_supported(s) for s, _, _ in universe[:2]))
        self.assertTrue(all(not monitor.quote_supported(s) for s, _, _ in universe[2:]))
        self.assertTrue(monitor.quote_supported('AAPL'))
        self.assertFalse(monitor.quote_supported('NQmain'))
        self.assertEqual(monitor.normalize_symbol('31#BRK.B'), 'BRK-B')
        rows = [dict(symbol=s, price=None, level='gray') for s, _, _ in universe]
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, rows, cfg)
        self.assertEqual(len(snap['summary']['missing_symbols']), 2)
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
        with patch.object(monitor, 'global_dca', return_value=None), patch.object(monitor, 'RISK_IDENTITIES', {'^GSPC', '^VIX'}):   # 旧的参考行渲染路径：临时放开 ^TNX
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
                patch.object(monitor, 'global_dca', return_value=None), patch.object(monitor, 'RISK_IDENTITIES', {'^GSPC', '^VIX'}):
            snap = monitor.build_snapshot({}, [reference, daily], cfg)
        self.assertEqual(snap['summary']['reference_dates'], {'BD#US10Y': '2026-10-06'})
        self.assertEqual(snap['summary']['stale_symbols'], [])
        self.assertEqual((snap['summary']['today_prices'], snap['summary']['prior_prices']), (1, 0))
        self.assertEqual(snap['actual_dates'], {'min': '2026-10-07', 'max': '2026-10-07'})
        self.assertEqual(snap['data_time_text'], '2026-10-07 收盘（美东交易日）')
        with patch.object(monitor, 'RISK_IDENTITIES', {'^GSPC', '^VIX'}):
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
        self.assertIn('化材金纸 (1)', page)          # 括号里是页面实际显示数（登记 2，监测关闭只显示板块 ETF）
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
                    patch.object(monitor, 'update_consensus', return_value=({}, {})), \
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
                    patch.object(monitor, 'update_consensus', return_value=({}, {})), \
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
                    patch.object(monitor, 'build_macro', return_value=macro), patch.object(monitor, 'RISK_IDENTITIES', {'^GSPC', '^VIX'}), \
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
                    patch.object(monitor, 'update_consensus', return_value=({}, {})), \
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
                    patch.object(monitor, 'update_consensus', return_value=({}, {})), \
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

    def test_dca_text_short_form_and_only_due_day_turns_yellow(self):
        base = {'every': 10, 'start': '2026-10-01'}
        self.assertEqual(monitor.dca_text({**base, 'due': False, 'next_in': 5, 'next_date': '2026-10-15'}),
                         '还有 5 个交易日（2026-10-15）')
        self.assertEqual(monitor.dca_text({**base, 'due': True, 'date': '2026-10-15', 'times': 2}),
                         '今天是定投日（2026-10-15，第 2 次）')
        self.assertEqual(monitor.dca_text({**base, 'pending': True, 'first': '2026-10-20'}),
                         '尚未开始（首个定投日 2026-10-20）')
        def page(d):
            snap = {'dca_reminder': d, 'registered_counts': {}, 'list_counts': {}, 'summary': {}}
            with patch.object(monitor, 'global_dca', return_value=None):
                return monitor.render({}, [], 0, snapshot=snap)
        quiet = page({**base, 'due': False, 'next_in': 5, 'next_date': '2026-10-15'})
        self.assertIn('<div class="dca ">定投提醒：还有 5 个交易日（2026-10-15）</div>', quiet)
        due = page({**base, 'due': True, 'date': '2026-10-15', 'times': 2})
        self.assertIn('<div class="dca on">定投提醒：今天是定投日（2026-10-15，第 2 次）</div>', due)

    def test_group_count_is_displayed_rows_not_registered(self):
        cfg = {'technology': {'AAPL': {}, 'MSFT': {}, 'NVDA': {}}, 'positions': {'AAPL': {}}}
        rows = lambda syms, grp: [dict(symbol=x, note='', price=100, chg=0, level='green', group=grp,
                                       signals=[], data_date='2026-10-02') for x in syms]
        items = rows(['AAPL'], 'position') + rows(['MSFT', 'NVDA'], 'technology')
        snap = monitor.build_snapshot({}, items, cfg)
        page = monitor.render({}, items, 3, snapshot=snap)
        self.assertEqual(snap['registered_counts']['technology'], 3)       # 登记 3，AAPL 去重落在持仓
        self.assertIn('IT软硬Ai (2)', page)
        self.assertIn('持仓 (1)', page)

    def _reduce_signals(self, cfg, group='position', price=100):
        closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)]
        closes[-2:] = [100, 100]
        data = dict(closes=closes, highs=[101] * 60, lows=[99] * 60, volumes=[1000] * 60,
                    price=price, prev_close=100, dates=['2026-10-08'] * 60, source='test')
        with patch.object(monitor, 'log'):
            level, signals, detail = monitor.analyze_symbol('TEST', cfg, data, group=group)
        return level, [x for x in signals if '减仓价' in x], detail

    def test_reduce_price_red_when_crossed_or_within_gap(self):
        # 现价 100：已涨到减仓价以上（不管高出多少）→ 红；还没到但差距 ≤3% → 红
        for reduce, text in [(100, '已涨到减仓价 100.00（高出 0.0%）'),      # 刚好等于
                             (99, '已涨到减仓价 99.00（高出 1.0%）'),
                             (90, '已涨到减仓价 90.00（高出 11.1%）'),       # 已越过很多，照样报
                             (50, '已涨到减仓价 50.00（高出 100.0%）'),
                             (101, '距减仓价 101.00 还差 1.0%'),
                             (103, '距减仓价 103.00 还差 2.9%')]:
            with self.subTest(reduce=reduce):
                level, hit, detail = self._reduce_signals({'reduce': reduce})
                self.assertEqual(level, 'red')
                self.assertEqual(hit, [text])
                self.assertEqual(detail['reduce'], reduce)
        for reduce in (104, 120, 200):                                      # 还差超过 3%：不报
            with self.subTest(far=reduce):
                _, hit, detail = self._reduce_signals({'reduce': reduce})
                self.assertEqual(hit, [])
                self.assertEqual(detail['reduce'], reduce)
        # 旧版遗留的 reduce_dir 不再起作用
        self.assertEqual(self._reduce_signals({'reduce': 99, 'reduce_dir': 'down'})[0], 'red')
        self.assertEqual(self._reduce_signals({'reduce': 120, 'reduce_dir': 'up'})[1], [])
        self.assertNotIn('reduce_dir', self._reduce_signals({'reduce': 99})[2])

    def test_reduce_price_threshold_setting_and_boundary(self):
        with patch.dict(monitor.S, {'reduce_gap_pct': 3.0}):
            self.assertEqual(self._reduce_signals({'reduce': 103.1})[1], [])            # 100/103.1-1 = -3.01%
            self.assertEqual(len(self._reduce_signals({'reduce': 103})[1]), 1)
        with patch.dict(monitor.S, {'reduce_gap_pct': 5.0}):
            self.assertEqual(self._reduce_signals({'reduce': 104.5})[1], ['距减仓价 104.50 还差 4.3%'])
        with patch.dict(monitor.S, {'reduce_gap_pct': 0.0}):
            self.assertEqual(self._reduce_signals({'reduce': 101})[1], [])              # 没越过且阈值 0：不报
            self.assertEqual(len(self._reduce_signals({'reduce': 100})[1]), 1)          # 已到：照报
            self.assertEqual(len(self._reduce_signals({'reduce': 90})[1]), 1)
        self.assertEqual(monitor.DEFAULT_SETTINGS['reduce_gap_pct'], 3.0)
        self.assertEqual(monitor.SETTING_LIMITS['reduce_gap_pct'], (0, 100))

    def test_trigger_price_reports_both_within_gap_and_already_crossed(self):
        def hit(trigger, price=100):
            closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)]
            closes[-2:] = [100, 100]
            data = dict(closes=closes, highs=[101] * 60, lows=[99] * 60, volumes=[1000] * 60,
                        price=price, prev_close=100, dates=['2026-10-08'] * 60, source='test')
            with patch.object(monitor, 'log'):
                with patch.dict(monitor.S, {'trigger_gap_pct': 5.0}):
                    _, sigs, _ = monitor.analyze_symbol('TEST', {'trigger': trigger}, data, group='position')
            return [x for x in sigs if '加仓价' in x]
        self.assertEqual(hit(104), ['已跌破加仓价 104.00'])      # 现价低于加仓价 3.8%
        self.assertEqual(hit(100), ['已跌破加仓价 100.00'])      # 刚好到
        self.assertEqual(hit(110), ['已跌破加仓价 110.00'])      # 跌破很多
        self.assertEqual(hit(97), ['距加仓价 97.00 还差 3.1%'])  # 上方 5% 内
        self.assertEqual(hit(90), [])                            # 还差 11%：不报

    def test_level_note_under_symbol_only_when_set(self):
        self.assertEqual(monitor.level_note(325, None), '<span class="lvs"><span class="lvtag add">加 325</span></span>')
        self.assertEqual(monitor.level_note(None, 400.5), '<span class="lvs"><span class="lvtag cut">减 400.5</span></span>')
        self.assertEqual(monitor.level_note(325, 400), '<span class="lvs"><span class="lvtag add">加 325</span><span class="lvtag cut">减 400</span></span>')
        self.assertEqual(monitor.level_note(12.3456, None), '<span class="lvs"><span class="lvtag add">加 12.3456</span></span>')
        for bad in (None, '', 0, -1, 'abc', True, float('nan'), float('inf')):
            self.assertEqual(monitor.level_note(bad, bad), '')

    def test_page_shows_level_note_under_name_and_clearing_removes_it(self):
        def page_for(trigger, reduce):
            d = dict(symbol='AAPL', note='苹果', price=100.0, chg=0.0, level='green', signals=[],
                     group='position', data_date='2026-10-08', trigger=trigger, reduce=reduce)
            snap = monitor.build_snapshot({}, [d], {'positions': {'AAPL': {'note': '苹果'}}})
            return monitor.render({}, [d], 1, snapshot=snap)
        with patch.object(monitor, 'global_dca', return_value=None):
            both = page_for(325, 400)
            self.assertIn('AAPL<span class="note">苹果</span><span class="lvs"><span class="lvtag add">加 325</span><span class="lvtag cut">减 400</span></span>', both)
            cleared = page_for(None, None)                                  # 设置页清空后：不再显示
            self.assertNotIn('class="lvs"', cleared)
            self.assertNotIn('class="lvtag', cleared)

    def test_market_risk_notes_are_folded_by_default(self):
        with patch.object(monitor, 'global_dca', return_value=None):
            page = monitor.render({}, [], 0, snapshot={})
        m = re.search(r'<details class="macro-help" id="marketRiskNotes">(.*?)</details>', page, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn(' open', page[m.start():m.start() + 60])
        self.assertIn('垃圾债利差：≥', m.group(1))
        self.assertIn('可能有成分股选择偏差', m.group(1))

    def test_reduce_price_only_applies_to_positions_and_bad_values_are_ignored(self):
        for group in ('focus', 'technology', 'index_funds'):
            self.assertEqual(self._reduce_signals({'reduce': 100}, group=group)[1], [])
            self.assertIsNone(self._reduce_signals({'reduce': 100}, group=group)[2]['reduce'])
        for bad in (0, -5, 'abc', '100', True, False, float('nan'), float('inf'), None, '', [], {}):
            with self.subTest(reduce=bad):
                level, hit, detail = self._reduce_signals({'reduce': bad})
                self.assertEqual(hit, [])
                self.assertIsNone(detail['reduce'])

    def test_snapshot_counts_valid_reduce_prices_only_for_positions(self):
        cfg = {'positions': {'AAPL': {'reduce': 90}, 'MSFT': {'reduce': 'x'}, 'NVDA': {'reduce': 120, 'reduce_dir': 'up'},
                             'TSLA': {}}, 'technology': {'AMD': {'reduce': 50}}}
        with patch.object(monitor, 'log'):
            snap = monitor.build_snapshot({}, [], cfg)
        self.assertEqual(snap['list_counts']['reduces'], 2)

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

    def test_schedule_target_follows_nasdaq_23_5_pause_window(self):
        def plan(iso):
            return monitor.plan_run('schedule', monitor.datetime.fromisoformat(iso))
        # 夏令时：2026-10-09(周五)美东20:15/20:45 = 2026-10-10 00:15/00:45 UTC，目标是当晚休盘前的交易日
        for iso in ('2026-10-10T00:15:00+00:00', '2026-10-10T00:45:00+00:00'):
            self.assertEqual(plan(iso)['target'], '2026-10-09')
            self.assertTrue(plan(iso)['closed_only'])
        # 美东19:55 还没到休盘，仍算上一晚
        self.assertEqual(plan('2026-10-09T23:55:00+00:00')['target'], '2026-10-08')
        # 冬令时：2026-12-09(周三)美东20:15 = 12-10 01:15 UTC
        self.assertEqual(plan('2026-12-10T01:15:00+00:00')['target'], '2026-12-09')

    def test_ensure_schedule_run_only_acts_inside_pause_window(self):
        import tempfile
        from unittest.mock import MagicMock
        real = monitor.datetime

        def at(iso):
            class F(real):
                @classmethod
                def now(cls, tz=None):
                    return real.fromisoformat(iso)
            return F

        def run(iso):
            get = MagicMock()
            get.return_value.json.return_value = {'workflow_runs': []}
            post = MagicMock()
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(monitor, 'BASE', tmp), \
                    patch.object(monitor, 'datetime', at(iso)), \
                    patch.object(monitor.requests, 'get', get), \
                    patch.object(monitor.requests, 'post', post):
                self.assertEqual(monitor.ensure_schedule_run(), 0)
            return post.call_count
        # 对的时点：夏令时 00:45 UTC(美东20:45)、冬令时 01:45 UTC(美东20:45) -> 结果缺失才补派发
        self.assertEqual(run('2026-10-10T00:45:00+00:00'), 1)
        self.assertEqual(run('2026-12-10T01:45:00+00:00'), 1)
        # 错季节或被拖过21:00的时点：什么都不做
        self.assertEqual(run('2026-10-10T01:45:00+00:00'), 0)   # 夏令时美东21:45
        self.assertEqual(run('2026-12-10T00:45:00+00:00'), 0)   # 冬令时美东19:45

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
        with patch.object(monitor, 'global_dca', return_value=None), patch.object(monitor, 'RISK_IDENTITIES', {'^GSPC', '^VIX'}):
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

    def test_items_already_in_market_risk_reference_are_skipped_everywhere(self):
        # 标普500、VIX、10Y美债已在「市场风险参考」里：不管写在哪个分组、用哪种写法，都不再单独成行
        cfg = {'positions': {'BD#US10Y': {}, 'AAPL': {}},
               'index_funds': {'.SPX': {}, '^GSPC': {}, '.VIX': {}, '^VIX': {}, 'BD#US10Y': {}, '.TNX': {}, '^TNX': {}, 'HYG': {}},
               'group_monitoring': {'index_funds': True}}
        universe, counts, dups = monitor.grouped_universe(cfg)
        self.assertEqual([s for s, _, _ in universe], ['AAPL', 'HYG'])
        self.assertEqual((counts['positions'], counts['index_funds']), (1, 1))
        self.assertEqual(dups, 8)
        macro = {'ust10': {'ok': True, 'name': '10Y美债', 'value': 4.3, 'date': '2026-10-07'}}
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot(macro, [], cfg)
        page = monitor.render(macro, [], 0, snapshot=snap)
        risk = page.split('id="macroCard"', 1)[1].split('id="group_positions"', 1)[0]
        index = page.split('id="group_index_funds"', 1)[1].split('id="group_technology"', 1)[0]
        self.assertEqual(risk.count('id="ust10Ref"'), 1)      # 风险参考里的 10Y 参考行还在
        self.assertNotIn('BD#US10Y', index)

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
        self.assertIn('指数基 (0)', page)          # 括号里是页面实际显示数，不是登记数
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


class FakeResp:
    def __init__(self, status=200, text=''):
        self.status_code, self.text = status, text


class FakeSession:
    """按网址返回预设内容；记录请求顺序。"""
    def __init__(self, pages, robots='User-agent: *\nDisallow: /e/\n'):
        self.pages, self.robots, self.calls = pages, robots, []
        self.headers = {}

    def get(self, url, timeout=0):
        self.calls.append(url)
        if url.endswith('/robots.txt'):
            return FakeResp(200, self.robots) if self.robots is not None else FakeResp(503)
        for slug, page in self.pages.items():
            if f'/stocks/{slug}/forecast/' in url:
                if isinstance(page, Exception):
                    raise page
                return page if isinstance(page, FakeResp) else FakeResp(200, page)
        return FakeResp(404)


def spg_page(avg, upd='2026-10-07', cur='USD'):
    return ('x targets:{low:300,high:500,count:20,median:420,average:410,updated:"%s"} y '
            'Targets:{source:"spg",currency:"%s",avg:%s,median:420,low:300,high:500,numPriceTargets:20}' % (upd, cur, avg))


class TestTargets(unittest.TestCase):
    def _run(self, cfg, price=100, prev=100, group='position', cons=None):
        closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)]
        closes[-2:] = [prev, price]
        data = dict(closes=closes, highs=[101] * 60, lows=[99] * 60, volumes=[1000] * 60,
                    price=price, prev_close=prev, dates=['2026-10-08'] * 60, source='test')
        cfg = {**cfg, '_consensus': cons} if cons else cfg
        with patch.object(monitor, 'log'):
            return monitor.analyze_symbol('TEST', cfg, data, group=group)

    def _tg(self, cfg, **kw):
        level, signals, detail = self._run(cfg, **kw)
        base = self._run({}, **kw)[0]
        return level, base, [x for x in signals if '目标价' in x or '共识价' in x], detail

    # ---- 警示规则（手填目标价和共识价共用一套）
    def test_within_gap_is_purple_and_does_not_change_level(self):
        for target, text in [(101, '在目标价 101 ±3% 内（偏离 -1.0%）'),
                             (97.5, '在目标价 97.5 ±3% 内（偏离 +2.6%）'),   # 已超过目标价但还在带内：也报
                             (103, '在目标价 103 ±3% 内（偏离 -2.9%）')]:
            with self.subTest(target=target):
                level, base, hit, detail = self._tg({'target': target})
                self.assertEqual(hit, [text])
                self.assertEqual(level, base)                # 紫色不改红黄绿
                self.assertTrue(detail['purple'])
                self.assertTrue(detail['target']['hit'])

    def test_far_from_target_or_far_beyond_is_silent(self):
        for target in (104, 120, 140, 96, 90, 60):           # 远离 / 已远超：都不报
            with self.subTest(target=target):
                level, base, hit, detail = self._tg({'target': target})
                self.assertEqual(hit, [])
                self.assertFalse(detail['purple'])

    def test_same_day_cross_is_purple_even_when_jump_is_big(self):
        _, _, hit, d = self._tg({'target': 100.5}, price=101, prev=100)
        self.assertEqual(hit, ['今日上穿目标价 100.5'])
        _, _, hit, d = self._tg({'target': 100.5}, price=100, prev=101)
        self.assertEqual(hit, ['今日跌破目标价 100.5'])
        self.assertTrue(d['purple'])
        # 跳空一次越过 ±3% 的带：只要跨越就报
        _, _, hit, _ = self._tg({'target': 100}, price=108, prev=95)
        self.assertEqual(hit, ['今日上穿目标价 100'])
        # 跨越之后第二天：还在带内照报（带内一律报），出了带就不报
        self.assertEqual(len(self._tg({'target': 100}, price=102, prev=101)[2]), 1)
        self.assertEqual(self._tg({'target': 100}, price=110, prev=108)[2], [])

    def test_space_over_threshold_is_purple_with_percent_text(self):
        level, base, hit, detail = self._tg({'target': 160})        # 160/100-1 = +60%
        self.assertEqual(hit, ['目标价 160，空间 +60%'])
        self.assertEqual(level, base)
        self.assertTrue(detail['purple'])
        self.assertEqual(self._tg({'target': 150})[2], [])                  # 刚好 50% 不算「超过」
        with patch.dict(monitor.S, {'target_space_pct': 30.0}):
            self.assertEqual(self._tg({'target': 140})[2], ['目标价 140，空间 +40%'])
        with patch.dict(monitor.S, {'target_gap_pct': 5.0}):
            self.assertEqual(len(self._tg({'target': 104})[2]), 1)
        self.assertEqual(monitor.DEFAULT_SETTINGS['target_gap_pct'], 3.0)
        self.assertEqual(monitor.DEFAULT_SETTINGS['target_space_pct'], 50.0)

    def test_over_100_percent_is_anomaly_red_not_purple(self):
        level, base, hit, detail = self._tg({'target': 250})        # +150%
        self.assertEqual(hit, ['目标价 250 疑似数据异常（高出现价 150%）'])
        self.assertEqual(level, 'red')
        self.assertFalse(detail['purple'])
        self.assertTrue(detail['target']['anomaly'])

    def test_only_positions_and_focus_use_targets(self):
        for group in ('position', 'focus'):
            self.assertTrue(self._tg({'target': 101}, group=group)[3]['purple'])
        for group in ('technology', 'materials', 'index_funds'):
            d = self._tg({'target': 101}, group=group)[3]
            self.assertFalse(d['purple'])
            self.assertIsNone(d['target'])

    def test_manual_target_beats_consensus_and_bad_values_ignored(self):
        cons = {'s': 'ok', 'avg': 160.0, 'upd': '2026-10-07', 'r': '2026-10-01'}
        _, _, hit, d = self._tg({'target': 101, 'target_at': '2026-09'}, cons=cons)
        self.assertEqual(d['target']['kind'], 'manual')
        self.assertEqual(d['target']['month'], 9)
        self.assertEqual(hit, ['在目标价 101 ±3% 内（偏离 -1.0%）'])          # 用手填的 101，不是共识的 160
        _, _, hit, d = self._tg({}, cons=cons)
        self.assertEqual(d['target']['kind'], 'consensus')
        self.assertEqual(hit, ['共识价 160，空间 +60%'])
        for bad in (0, -5, 'abc', True, float('nan'), float('inf'), ''):
            with self.subTest(bad=bad):
                d = self._tg({'target': bad})[3]
                self.assertIsNone(d['target'])
                self.assertFalse(d['purple'])
        with patch.object(monitor, 'log'):
            self.assertIsNone(monitor.manual_target({'target': 0}))

    def test_failed_consensus_shows_old_value_but_never_alerts(self):
        cons = {'s': 'fail', 'avg': 160.0, 'upd': '2026-08-17', 'r': '2026-09-15', 'try': '2026-10-09'}
        level, base, hit, d = self._tg({}, cons=cons)
        self.assertEqual(hit, [])
        self.assertEqual(level, base)
        self.assertTrue(d['target']['failed'])
        self.assertEqual(d['target']['month'], 8)
        self.assertFalse(d['purple'])
        d = self._tg({}, cons={'s': 'fail', 'try': '2026-10-09'})[3]       # 没有旧值
        self.assertIsNone(d['target']['price'])
        self.assertIsNone(self._tg({}, cons={'s': 'none', 'r': '2026-10-01'})[3]['target'])   # 无覆盖：不显示

    # ---- 标签样式
    def test_tag_html_variants(self):
        tag = monitor.target_tag
        self.assertEqual(tag(None), '')
        self.assertEqual(tag({'kind': 'consensus', 'price': 429.47, 'month': 10}),
                         '<span class="tgs"><span class="lvtag tgt"><span>共识</span> <span>429.47</span> <span>· 10月</span></span></span>')
        self.assertIn('lvtag tgt hit', tag({'kind': 'consensus', 'price': 87.56, 'month': 10, 'hit': True}))
        self.assertIn('<span>目标</span> <span>108</span> <span>· 10月</span>', tag({'kind': 'manual', 'price': 108.0, 'month': 10}))
        self.assertNotIn('月', tag({'kind': 'manual', 'price': 108.0, 'month': None}))
        failed = tag({'kind': 'consensus', 'price': 429.0, 'month': 8, 'failed': True})
        self.assertIn('lvtag tgt bad', failed)
        self.assertIn('<span>429</span>', failed)
        self.assertIn('<span>共识</span> <span>获取失败</span>', tag({'kind': 'consensus', 'price': None, 'month': None, 'failed': True}))
        # 数据异常：标签不变红
        self.assertNotIn('bad', tag({'kind': 'consensus', 'price': 250.0, 'month': 10, 'anomaly': True}))

    # ---- 抓取
    def test_parse_consensus(self):
        self.assertEqual(monitor.parse_consensus(spg_page(429.47)), {'avg': 429.47, 'upd': '2026-10-07'})
        self.assertIsNone(monitor.parse_consensus(spg_page(429.47, cur='HKD')))
        self.assertIsNone(monitor.parse_consensus(spg_page(0)))
        self.assertIsNone(monitor.parse_consensus('nothing'))
        self.assertIsNone(monitor.parse_consensus(None))
        self.assertEqual(monitor.parse_consensus(spg_page(12, upd='bad'))['upd'], '')

    def test_marker_and_due_rules(self):
        self.assertEqual(monitor.consensus_marker('2026-10-09'), '2026-10-01')
        self.assertEqual(monitor.consensus_marker('2026-10-15'), '2026-10-15')
        self.assertEqual(monitor.consensus_marker('2026-10-31'), '2026-10-15')
        due = monitor.consensus_due
        self.assertTrue(due(None, '2026-10-01', '2026-10-09'))
        self.assertTrue(due({'s': 'ok', 'r': '2026-09-15'}, '2026-10-01', '2026-10-09'))     # 上一个节拍的
        self.assertFalse(due({'s': 'ok', 'r': '2026-10-01'}, '2026-10-01', '2026-10-09'))    # 本节拍已取
        self.assertFalse(due({'s': 'none', 'r': '2026-10-01'}, '2026-10-01', '2026-10-09'))
        self.assertTrue(due({'s': 'fail', 'r': '2026-10-01', 'try': '2026-10-08'}, '2026-10-01', '2026-10-09'))
        self.assertFalse(due({'s': 'fail', 'r': '2026-10-01', 'try': '2026-10-09'}, '2026-10-01', '2026-10-09'))
        self.assertTrue(due({'s': 'weird'}, '2026-10-01', '2026-10-09'))

    def test_wanted_scope_only_positions_and_focus_stocks(self):
        w = monitor.consensus_wanted
        self.assertTrue(w('AAPL', {}, 'position'))
        self.assertTrue(w('MSFT', {}, 'focus'))
        self.assertFalse(w('NVDA', {}, 'technology'))          # 其它分组不取
        self.assertFalse(w('NVDA', {}, 'other'))
        self.assertFalse(w('BRK-B', {}, 'position'))           # 伯克希尔不取
        self.assertFalse(w('BRK.B', {}, 'position'))
        self.assertFalse(w('XLK', {}, 'position'))             # ETF 不取
        self.assertFalse(w('AAPL', {'target': 300}, 'position'))   # 手填了就不取共识
        self.assertTrue(w('AAPL', {'target': 0}, 'position'))      # 手填无效当没填
        self.assertFalse(w('.VIX', {}, 'position'))
        self.assertFalse(w('00700.HK', {}, 'position'))

    def _update(self, symbols, pages, prev=None, today='2026-10-09', robots='User-agent: *\nDisallow: /e/\n'):
        sess = FakeSession(pages, robots=robots)
        sleeps = []
        with patch.object(monitor, 'log'):
            data, stat = monitor.update_consensus(symbols, prev, today, sess=sess, sleep=sleeps.append)
        return data, stat, sess, sleeps

    def test_update_fetches_due_symbols_politely(self):
        pages = {'aapl': spg_page(328.09), 'zs': spg_page(233.38, '2026-09-12'), 'newx': FakeResp(404)}
        data, stat, sess, sleeps = self._update(['AAPL', 'ZS', 'NEWX'], pages)
        self.assertEqual(data['AAPL'], {'s': 'ok', 'avg': 328.09, 'upd': '2026-10-07', 'r': '2026-10-01', 'try': '2026-10-09'})
        self.assertEqual(data['ZS']['upd'], '2026-09-12')
        self.assertEqual(data['NEWX']['s'], 'none')
        self.assertEqual((stat['ok'], stat['none'], stat['fail']), (2, 1, 0))
        self.assertTrue(sess.calls[0].endswith('/robots.txt'))                # 先读 robots
        self.assertEqual(sleeps, [monitor.CONSENSUS_GAP, monitor.CONSENSUS_GAP])   # 每只之间隔开
        self.assertIn('market-monitor/1.0', sess.headers['User-Agent'])
        self.assertIn('github.com/nixhuang/market-monitor', sess.headers['User-Agent'])
        self.assertNotIn('@', sess.headers['User-Agent'])                      # 不留邮箱

    def test_update_only_fetches_missing_or_stale_and_drops_removed(self):
        prev = {'data': {'AAPL': {'s': 'ok', 'avg': 300.0, 'upd': '2026-10-01', 'r': '2026-10-01', 'try': '2026-10-01'},
                         'OLD': {'s': 'ok', 'avg': 9.0, 'upd': '2026-10-01', 'r': '2026-10-01'}}}
        data, stat, sess, _ = self._update(['AAPL', 'ZS'], {'zs': spg_page(233.38)}, prev=prev)
        self.assertEqual([u for u in sess.calls if 'forecast' in u], ['https://stockanalysis.com/stocks/zs/forecast/'])
        self.assertEqual(data['AAPL']['avg'], 300.0)            # 本节拍已取：不重复
        self.assertNotIn('OLD', data)                           # 不在清单里了：清掉
        data, stat, sess, _ = self._update(['AAPL'], {}, prev={'data': data}, today='2026-10-12')
        self.assertEqual(stat['due'], 0)                        # 同一节拍内不再取
        self.assertEqual(sess.calls, [])
        data, stat, sess, _ = self._update(['AAPL'], {'aapl': spg_page(310)}, prev={'data': data}, today='2026-10-15')
        self.assertEqual(data['AAPL']['avg'], 310.0)            # 到 15 号：新节拍，再取一次
        self.assertEqual(data['AAPL']['r'], '2026-10-15')

    def test_failure_keeps_old_value_and_marks_fail(self):
        old = {'s': 'ok', 'avg': 300.0, 'upd': '2026-08-17', 'r': '2026-09-15', 'try': '2026-09-15'}
        data, stat, _, _ = self._update(['AAPL'], {'aapl': FakeResp(429)}, prev={'data': {'AAPL': old}})
        e = data['AAPL']
        self.assertEqual((e['s'], e['avg'], e['upd'], e['r']), ('fail', 300.0, '2026-08-17', '2026-09-15'))
        self.assertEqual(stat['fail'], 1)
        t = monitor.resolve_target({}, e)
        self.assertTrue(t['failed'])
        self.assertEqual((t['price'], t['month']), (300.0, 8))   # 旧值 + 旧月份
        # 同一天不重试；第二天再试
        _, st2, sess2, _ = self._update(['AAPL'], {'aapl': spg_page(1)}, prev={'data': data}, today='2026-10-09')
        self.assertEqual(st2['due'], 0)
        d3, st3, _, _ = self._update(['AAPL'], {'aapl': spg_page(310)}, prev={'data': data}, today='2026-10-10')
        self.assertEqual(d3['AAPL']['s'], 'ok')
        # 网络异常也算失败
        d4, _, _, _ = self._update(['AAPL'], {'aapl': ConnectionError('boom')})
        self.assertEqual(d4['AAPL']['s'], 'fail')
        self.assertNotIn('avg', d4['AAPL'])

    def test_stops_after_three_consecutive_failures(self):
        syms = ['AA', 'BB', 'CC', 'DD', 'EE']
        data, stat, sess, _ = self._update(syms, {s.lower(): FakeResp(403) for s in syms})
        self.assertEqual(stat['stopped'], 'blocked')
        self.assertEqual(stat['tried'], 3)
        self.assertEqual(len([u for u in sess.calls if 'forecast' in u]), 3)
        self.assertNotIn('EE', data)                              # 没试到的留给下次

    def test_robots_disallow_or_unreadable_means_no_fetch(self):
        data, stat, sess, _ = self._update(['AAPL'], {'aapl': spg_page(1)}, robots='User-agent: *\nDisallow: /stocks/\n')
        self.assertEqual(stat['stopped'], 'robots')
        self.assertEqual(data, {})
        self.assertEqual([u for u in sess.calls if 'forecast' in u], [])
        data, stat, sess, _ = self._update(['AAPL'], {'aapl': spg_page(1)}, robots=None)   # robots 读到 503
        self.assertEqual(stat['stopped'], 'robots')

    def test_all_none_in_a_big_batch_is_treated_as_layout_change(self):
        syms = ['AA', 'BB', 'CC', 'DD', 'EE', 'FF']
        old = {s: {'s': 'ok', 'avg': 10.0, 'upd': '2026-09-12', 'r': '2026-09-15'} for s in syms}
        data, stat, _, _ = self._update(syms, {s.lower(): 'no data here' for s in syms}, prev={'data': old})
        self.assertEqual(stat['stopped'], 'layout')
        self.assertEqual(stat['fail'], 6)
        self.assertTrue(all(data[s]['s'] == 'fail' and data[s]['avg'] == 10.0 for s in syms))   # 旧值保留、标红

    # ---- 页面
    def _page(self, rows):
        snap = monitor.build_snapshot({}, rows, {'positions': {}, 'focus': {}})
        with patch.object(monitor, 'global_dca', return_value=None):
            return monitor.render({}, rows, len(rows), snapshot=snap)

    def _row(self, sym, level='green', purple=False, target=None, signals=None, group='position', note=''):
        return dict(symbol=sym, note=note, price=100.0, chg=0.0, level=level, signals=signals or [],
                    group=group, data_date='2026-10-08', purple=purple, target=target)

    def test_page_purple_rows_are_unfolded_and_stripe_class_stacks(self):
        t_hit = {'kind': 'consensus', 'price': 160.0, 'month': 10, 'failed': False, 'hit': True, 'space': 60.0}
        t_ok = {'kind': 'consensus', 'price': 429.47, 'month': 10, 'failed': False, 'hit': False, 'space': 10.0}
        rows = [self._row('AAA', 'green', True, t_hit, ['共识价 160，空间 +60%']),
                self._row('BBB', 'red', True, t_hit, ['异动 +5.0%', '共识价 160，空间 +60%']),
                self._row('CCC', 'yellow', True, t_hit, ['x']),
                self._row('DDD', 'green', False, t_ok)]
        page = self._page(rows)
        self.assertIn('<tr class="green pur"><td class="sym">AAA', page)
        self.assertIn('<tr class="red pur"><td class="sym">BBB', page)
        self.assertIn('<tr class="yellow pur"><td class="sym">CCC', page)
        self.assertIn('<tr class="green"><td class="sym">DDD', page)
        shown, folded = page.split('无异动 1 只', 1)
        self.assertIn('>AAA', shown)                      # 紫色的绿灯行不折叠
        self.assertNotIn('>DDD', shown)                   # 普通绿灯行仍然折叠
        self.assertIn('>DDD', folded)
        self.assertIn('lvtag tgt hit', page)
        self.assertIn('tr.pur td.sig', page)
        self.assertIn('--purple:', page)
        self.assertIn('共识价来源：stockanalysis.com（S&P Global），每月 1、15 日更新', page)

    def test_page_sort_order_not_changed_by_purple(self):
        t_hit = {'kind': 'consensus', 'price': 160.0, 'month': 10, 'failed': False, 'hit': True}
        rows = [self._row('GRN', 'green', True, t_hit), self._row('RED', 'red'), self._row('YEL', 'yellow')]
        page = self._page(rows)
        pos = [page.index(f'>{s}<') if f'>{s}<' in page else page.index(f'>{s}') for s in ('RED', 'YEL', 'GRN')]
        self.assertEqual(pos, sorted(pos))

    def test_page_manual_target_always_shows_space_in_alert_column(self):
        t = {'kind': 'manual', 'price': 108.0, 'month': 10, 'failed': False, 'hit': False, 'anomaly': False, 'space': 8.0}
        page = self._page([self._row('MAN', 'yellow', False, t, ['波动 +2.1%'])])
        self.assertIn('波动 +2.1% · <span class="tnote">目标价 108，空间 +8%</span>', page)
        self.assertIn('<span>目标</span> <span>108</span> <span>· 10月</span>', page)
        self.assertNotIn('共识价来源', page)               # 只有手填：不显示共识来源

    def test_page_target_alert_text_is_purple_last_and_purple_bar_on_right(self):
        t = {'kind': 'consensus', 'price': 160.0, 'month': 10, 'failed': False, 'hit': True}
        page = self._page([self._row('PUR', 'green', True, t, ['在共识价 160 ±3% 内（偏离 +0.0%）', '异动 +2.1%', 'RSI 超买'])])
        # 共识价 / 目标价的警示统一排在最后，并用紫色文字
        self.assertIn('异动 +2.1% · RSI 超买 · <span class="tp">在共识价 160 ±3% 内（偏离 +0.0%）</span>', page)
        css = page.split('<style>')[1]
        # 红黄绿竖条在左、宽度不变（6/4/2px）；紫条改画在整行右侧（警示栏右缘），宽 2px
        self.assertIn('tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red)', css)
        self.assertIn('tr.yellow td:first-child{box-shadow:inset 4px 0 0 var(--yellow)', css)
        self.assertIn('tr.green td:first-child{box-shadow:inset 2px 0 0 var(--green)}', css)
        self.assertIn('.group-card tr.pur td.sig{box-shadow:inset -2px 0 0 var(--purple)', css)
        self.assertNotIn('tr.pur td:first-child', css)

    def test_group_header_shows_purple_count_only_when_there_is_one(self):
        t = {'kind': 'consensus', 'price': 160.0, 'month': 10, 'failed': False, 'hit': True}
        with_pur = self._page([self._row('PUR', 'green', True, t, ['在共识价 160 ±3% 内（偏离 +0.0%）'])])
        self.assertIn('aria-label="紫色警示 1 项"', with_pur)
        self.assertIn('stat-dot purple', with_pur.split('</style>')[1])
        without = self._page([self._row('AAPL', 'red', signals=['异动 +5.0%'])])
        self.assertNotIn('紫色警示', without)
        self.assertNotIn('stat-dot purple', without.split('</style>')[1])

    def test_page_no_target_means_no_tag_and_no_credit(self):
        page = self._page([self._row('AAPL', 'red', signals=['异动 +5.0%'])])
        self.assertNotIn('class="tgs"', page)
        self.assertNotIn('共识价来源', page)

    def test_main_passes_consensus_to_analysis_and_saves_cache(self):
        import tempfile
        cfg = {'positions': {'AAPL': {}, 'MSFT': {'target': 700}}, 'focus': {'ZS': {}},
               'technology': {'NVDA': {}}, 'group_monitoring': {'technology': True}}
        seen = {}
        def analyzed(symbol, settings, data, group):
            seen[symbol] = settings.get('_consensus')
            return 'green', [], dict(symbol=symbol, note='', price=100, chg=0, level='green', group=group,
                                     signals=[], data_date='2026-10-07')
        cache = {'AAPL': {'s': 'ok', 'avg': 328.09, 'upd': '2026-10-07', 'r': '2026-10-01'}}
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with open(os.path.join(directory, 'status.json'), 'w', encoding='utf-8') as f:
                json.dump({'consensus': {'data': {'AAPL': cache['AAPL']}}}, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                    patch.object(monitor, 'fetch_history', return_value={'price': 100}), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'update_consensus', return_value=({'AAPL': cache['AAPL']}, {'ok': 1})) as upd, \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan'):
                self.assertEqual(monitor.main([]), 0)
                # 只把持仓/重点关注里「没手填目标价」的美股个股交给抓取；MSFT 手填了、NVDA 在板块组都不取
                self.assertEqual(upd.call_args.args[0], ['AAPL', 'ZS'])
                self.assertEqual(upd.call_args.args[1], {'data': {'AAPL': cache['AAPL']}})   # 上次缓存从 status.json 读回
                self.assertEqual(seen['AAPL'], cache['AAPL'])
                self.assertIsNone(seen['NVDA'])
                with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                    saved = json.load(f)['consensus']
                self.assertEqual(saved['data'], {'AAPL': cache['AAPL']})
                self.assertIn('S&P Global', saved['source'])

    def test_main_survives_consensus_crash(self):
        import tempfile
        cfg = {'positions': {'AAPL': {}}}
        def analyzed(symbol, settings, data, group):
            return 'green', [], dict(symbol=symbol, note='', price=100, chg=0, level='green', group=group,
                                     signals=[], data_date='2026-10-07')
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                    patch.object(monitor, 'fetch_history', return_value={'price': 100}), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'update_consensus', side_effect=RuntimeError('boom')), \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan'):
                self.assertEqual(monitor.main([]), 0)
                self.assertTrue(os.path.exists(os.path.join(directory, 'index.html')))


if __name__ == '__main__':
    unittest.main()
