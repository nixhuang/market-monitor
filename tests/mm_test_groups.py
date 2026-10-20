"""离线分类模型、去重、备注及全部折叠标题回归。"""
import json
import re
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import monitor

# 测试一律用代码里的默认设置，不读线上 settings.json（线上值一改，测试就会无故失败）
monitor.S.clear()
monitor.S.update(monitor.DEFAULT_SETTINGS)


class TestGroups(unittest.TestCase):
    def setUp(self):
        # 财报测试独立于实际已核验日期文件；默认禁止访问官网，专项测试再注入样本。
        self.addCleanup(patch.stopall)
        patch.object(monitor, 'verified_earnings_events', return_value={}).start()
        self.real_official_fetch = monitor.earnings_sources.fetch_official_earnings
        patch.object(monitor.earnings_sources, 'fetch_official_earnings',
                     return_value={'status': 'unknown', 'reason': 'fixture_empty'}).start()

    def official_fixture(self):
        return {'status': 'ok', 'kind': 'official', 'date': '2030-12-10', 'timing': '',
                'source_url': 'https://investor.costco.com/events-and-presentations/events/event-details/2030/test/default.aspx',
                'title': 'Q1 2031 Earnings Results', 'official_checked': '2030-10-11',
                'source': '公司官方投资者关系'}

    def test_official_event_parser_and_date_boundaries(self):
        import earnings_sources as es
        url = self.official_fixture()['source_url']
        title = '<h1>Q1 2031 Earnings Results</h1>'
        cases = [
            (title + '<div class="module_date">December 10, 2030 01:15 PM PT</div>', '2030-12-10'),
            (title + '<time datetime="2030-12-11T01:15:00Z"></time>', '2030-12-10'),
            ('<script type="application/ld+json">' + json.dumps({'@type': 'Event', 'name': 'Q1 Earnings Results',
              'startDate': '2030-12-10T13:15:00-08:00'}) + '</script>', '2030-12-10')]
        for text, date in cases:
            with self.subTest(text=text):
                e = es.parse_official_event(text, url, '2030-10-11', 'COST')
                self.assertEqual((e['status'], e['date'], e['kind'], e['timing']), ('ok', date, 'official', ''))
        for text in ('<h1>Annual Shareholders Meeting</h1><time>December 10, 2030</time>',
                     '<h1>Monthly Sales Results</h1><time>December 10, 2030</time>',
                     title + '<time>December 10, 2029</time>', title,
                     title + '<time>December 10, 2030</time><time>December 11, 2030</time>'):
            self.assertEqual(es.parse_official_event(text, url, '2030-10-11', 'COST')['status'], 'unknown')
        self.assertEqual(es.parse_official_event(cases[0][0], url, '2030-12-10', 'COST')['status'], 'ok')
        for bad in ('https://evil.test/events', 'http://investor.costco.com/events',
                    'https://investor.costco.com.evil.test/events'):
            self.assertEqual(es.parse_official_event(cases[0][0], bad, '2030-10-11', 'COST')['status'], 'unknown')

    def test_official_source_blocked_no_redirect_or_retry(self):
        import earnings_sources as es
        for code in (301, 403, 429, 500):
            response = MagicMock(status_code=code)
            response.__enter__.return_value = response
            with patch.object(es.requests.Session, 'get', return_value=response) as get:
                e = self.real_official_fetch('COST', '2030-10-11')
            self.assertEqual(e['status'], 'error')
            get.assert_called_once()
            self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_official_fallback_preserves_verified_date_and_provenance(self):
        e = self.official_fixture()
        with patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                patch.object(monitor.earnings_sources, 'fetch_official_earnings', return_value=e):
            got = monitor.resolve_earnings('COST', '2030-10-11')
        self.assertEqual(got['kind'], 'official')
        with patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                patch.object(monitor.earnings_sources, 'fetch_official_earnings',
                             return_value={'status': 'error', 'reason': 'http_429'}):
            kept = monitor.resolve_earnings('COST', '2030-10-20', got)
        self.assertEqual(kept['date'], e['date'])
        self.assertEqual(kept['official_checked'], '2030-10-11')
        self.assertEqual(kept['official_error'], 'http_429')
        self.assertEqual(kept['official_attempted'], '2030-10-20')
        html = monitor._earnings_html({'symbol': 'COST', 'earnings': kept})
        # 2026-10-15 精简：不再挂「官方来源」链接，说明只写「官方 · 待复核」
        self.assertNotIn('官方来源', html)
        self.assertNotIn('<a ', html)
        self.assertIn('官方 · 待复核', html)
        self.assertNotIn('核实于', html)
        self.assertNotIn('发布钟点', html)
        self.assertNotIn('算法估算', html)
        self.assertNotIn('盘后', html)

    def test_official_verified_seed_used_once_without_network(self):
        e = self.official_fixture()
        rows = [{'symbol': 'COST', 'group': 'focus', 'level': 'green'}]
        old = {'COST': {'status': 'unknown', 'fetched': '2030-10-10'}}
        with patch.object(monitor, 'verified_earnings_events', return_value={'COST': e}), \
                patch.object(monitor, 'nasdaq_earnings') as nasdaq, \
                patch.object(monitor.earnings_sources, 'fetch_official_earnings') as official:
            result = monitor.attach_earnings(rows, old, '2030-10-11')
            again = monitor.attach_earnings(rows, result['data'], '2030-10-12')
        nasdaq.assert_not_called(); official.assert_not_called()
        self.assertEqual(result['dated'], 1)
        self.assertEqual(result['fetched'], 0)
        self.assertEqual(again['data']['COST']['official_checked'], '2030-10-11')
        self.assertEqual(old['COST']['status'], 'unknown')

    def test_pending_migration_does_not_repeat_daily(self):
        rows = [{'symbol': 'MU', 'group': 'focus', 'level': 'green'}]
        old = {'MU': {'status': 'unknown', 'fetched': '2030-10-10'}}
        with patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}) as primary, \
                patch.object(monitor.earnings_sources, 'fetch_official_earnings',
                             return_value={'status': 'error', 'reason': 'http_403'}) as official:
            result = monitor.attach_earnings(rows, old, '2030-10-11')
            monitor.attach_earnings(rows, result['data'], '2030-10-12')
        primary.assert_called_once(); official.assert_called_once()
        self.assertEqual(result['data']['MU']['official_attempted'], '2030-10-11')
        self.assertIn('未取得', monitor.earnings_label(rows[0]['earnings'])[1])

    def test_same_day_official_revision_once_and_invalid_results_safe(self):
        original = self.official_fixture()
        old = dict(original, fetched='2030-10-11', official_seed_sha=monitor._official_seed_hash(original))
        revised = dict(original, date='2030-12-17')
        self.assertTrue(monitor._new_official_seed(revised, old, 'COST', '2030-10-11'))
        applied = dict(revised, official_seed_sha=monitor._official_seed_hash(revised))
        self.assertFalse(monitor._new_official_seed(revised, applied, 'COST', '2030-10-11'))
        self.assertFalse(monitor._new_official_seed(original, dict(old, official_checked='2030-10-12'), 'COST', '2030-10-12'))
        for malformed in (None, [], 'bad'):
            with patch.object(monitor, 'nasdaq_earnings', return_value=malformed), \
                    patch.object(monitor.earnings_sources, 'fetch_official_earnings', return_value=malformed):
                e = monitor.resolve_earnings('COST', '2030-10-20', old)
            self.assertEqual(e['date'], original['date'])
            self.assertIn('official_error', e)

    def test_no_new_source_requests_when_primary_date_valid(self):
        primary = {'status': 'ok', 'date': '2030-12-10', 'kind': 'expected', 'timing': 'post'}
        with patch.object(monitor, 'nasdaq_earnings', return_value=primary), \
                patch.object(monitor.earnings_sources, 'fetch_official_earnings') as official:
            result = monitor.resolve_earnings('MU', '2030-10-11')
        official.assert_not_called()
        self.assertEqual(result, primary)
        e = self.official_fixture()
        self.assertFalse(monitor._official_earnings_valid(e, 'COST', '2031-01-01'))
        self.assertFalse(monitor._official_earnings_valid({**e, 'source_url': 'javascript:alert(1)'}, 'COST', '2030-10-11'))

    def test_definitions_and_initial_config(self):
        labels = ['持仓', '重点', '指数基', 'IT软硬Ai', '医疗保健', '金融', '能源',
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

    def test_macro_prefers_official_vix_and_only_credit_spread_from_fred(self):
        from datetime import date, datetime, timedelta  # 固定时钟：NFCI按真实时间判断到期，不能依赖跑测试的时刻
        def bars(alias):
            prices = {'^VIX': 15.08, '^GSPC': 7801.77, '^TNX': 5.3}
            value = prices[alias]
            dates = [(date(2026, 10, 7) - timedelta(days=i)).isoformat() for i in range(39, -1, -1)]
            return {'dates': dates, 'closes': [value]*40, 'highs': [value]*40, 'lows': [value]*40,
                    'volumes': [0]*40, 'price':value, 'prev_close':value,
                    'quote_symbol':alias, 'quote_name':'Treasury Yield 10 Years' if alias == '^TNX' else alias}
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-07T20:15:00-04:00')), \
                patch.object(monitor, 'fred_series', return_value=[('2026-10-06', 3.0)]) as fred, \
                patch.object(monitor, 'cboe_vix_series', return_value=[('2026-10-06', 15.01), ('2026-10-07', 15.08)]) as cboe, \
                patch.object(monitor, 'yahoo_history', side_effect=bars) as yahoo, \
                patch.object(monitor, 'financial_conditions_series', return_value=[('2026-10-02', -0.3)]), \
                patch.object(monitor, 'market_breadth', return_value={'ok':True,'name':'上涨参与度',
                                    'date':'2026-10-06','value':65,'pct50':60}):
            macro = monitor.build_macro()
            self.assertEqual(fred.call_args_list, [unittest.mock.call('BAMLH0A0HYM2')])
            cboe.assert_called_once()
            self.assertEqual([call.args[0] for call in yahoo.call_args_list], ['^GSPC', '^TNX'])
            self.assertEqual((macro['vix']['value'], macro['vix']['date']), (15.08, '2026-10-07'))
            self.assertEqual(macro['vix']['source'], 'Cboe VIX 官方日线')
            cfg = {'index_funds': {'.VIX': {}, '.SPX': {}, 'SPY': {}}, 'group_monitoring':{'index_funds':True}}
            with patch.object(monitor, 'global_dca', return_value=None):
                snap = monitor.build_snapshot(macro, [], cfg)
            page = monitor.render(macro, [], 0, snapshot=snap)
        risk = page.split('id="macroCard"')[1].split('id="group_positions"')[0]
        self.assertIn('15.08', risk)
        self.assertIn('7,801.77', risk)
        self.assertIn('截至 2026-10-07', risk)
        self.assertNotIn('Cboe VIX 官方日线', risk)   # 2026-10-15：指标行不再逐条展示数据来源
        self.assertNotIn('.VIX</td>', page)
        self.assertNotIn('.SPX</td>', page)
        self.assertEqual(snap['risk_quotes']['vix']['value'], 15.08)

    def test_cboe_vix_csv_checks_dates_values_and_target(self):
        from datetime import date, timedelta
        days = [date(2026, 10, 12) - timedelta(days=i) for i in range(55, -1, -1)]
        days = [d for d in days if d.weekday() < 5]
        csv_rows = ['DATE,OPEN,HIGH,LOW,CLOSE'] + [
            f'{d:%m/%d/%Y},15,16,14,{15.21 if d.isoformat() == "2026-10-12" else 15.0}' for d in days]
        csv_rows.append('10/13/2026,15,16,14,99.0')  # 不把测试目标日后的值混进旧快照

        class Response:
            def __init__(self, text):
                self.text = text
            def raise_for_status(self):
                pass

        with patch.object(monitor, 'TARGET_DATE', '2026-10-12'), \
                patch.object(monitor.requests, 'get', return_value=Response('\n'.join(csv_rows))) as get:
            rows = monitor.cboe_vix_series()
        self.assertEqual((len(rows), rows[-1]), (len(days), ('2026-10-12', 15.21)))
        self.assertEqual(get.call_args.args[0], monitor.CBOE_VIX_URL)
        for bad in ('DATE,OPEN,HIGH,LOW,SETTLE',
                    '\n'.join(csv_rows[:-1] + ['10/13/2026,15,16,14,999']),
                    '\n'.join(csv_rows[:-1] + [csv_rows[-2]]),
                    '\n'.join(csv_rows[:-1] + ['02/30/2026,15,16,14,15'])):
            with self.subTest(bad=bad[-40:]), patch.object(monitor, 'TARGET_DATE', '2026-10-12'), \
                    patch.object(monitor.requests, 'get', return_value=Response(bad)):
                self.assertEqual(monitor.cboe_vix_series(), [])
        with patch.object(monitor.requests, 'get', side_effect=monitor.requests.Timeout('offline')):
            self.assertEqual(monitor.cboe_vix_series(), [])

    def test_vix_source_prefers_current_official_then_newer_yahoo(self):
        official = [('2026-10-09', 14.84), ('2026-10-12', 15.21)]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-12'), \
                patch.object(monitor, 'cboe_vix_series', return_value=official), \
                patch.object(monitor, 'market_index_series') as yahoo:
            source = {}
            self.assertEqual(monitor.vix_index_series(source), official)
            self.assertEqual(source['source'], 'Cboe VIX 官方日线')
            yahoo.assert_not_called()
        with patch.object(monitor, 'TARGET_DATE', '2026-10-12'), \
                patch.object(monitor, 'cboe_vix_series', return_value=official[:-1]), \
                patch.object(monitor, 'market_index_series', return_value=official):
            source = {}
            self.assertEqual(monitor.vix_index_series(source), official)
            self.assertEqual(source['source'], 'Yahoo ^VIX')
        with patch.object(monitor, 'TARGET_DATE', '2026-10-12'), \
                patch.object(monitor, 'cboe_vix_series', return_value=official), \
                patch.object(monitor, 'market_index_series', return_value=official[:-1]):
            source = {}
            self.assertEqual(monitor.vix_index_series(source), official)
            self.assertEqual(source['source'], 'Cboe VIX 官方日线')

    def test_nfci_original_series_parser(self):
        class Response:
            text = 'Friday_of_Week,NFCI,ANFCI\n10/02/2026,-0.3,0\n10/09/2026,0.1,0\n'
            def raise_for_status(self):
                pass
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                patch.object(monitor.requests, 'get', return_value=Response()):
            self.assertEqual(monitor.financial_conditions_series(), [('2026-10-02', -0.3)])

    def test_nfci_cache_follows_weekly_release_without_new_timer(self):
        from datetime import datetime
        old = [('2026-09-25', -0.510), ('2026-10-02', -0.494)]
        new = old + [('2026-10-09', -0.480)]
        official_old = [('2026-04-17', -0.49), ('2026-04-24', -0.518)]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-13'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-13T22:30:00+08:00')), \
                patch.object(monitor, 'financial_conditions_series', return_value=official_old) as official, \
                patch.object(monitor, 'fred_series', return_value=old) as fred:
            cache = {}
            rows, source = monitor.cached_financial_conditions({}, cache)
            self.assertEqual(rows[-1], ('2026-10-02', -0.494))
            self.assertEqual(source, 'NFCI周度数据（FRED同步备用）')
            self.assertEqual(cache['result'], 'ok')
            self.assertEqual(len(cache['rows']), 2)
            self.assertEqual((official.call_count, fred.call_count), (1, 1))
        previous = {'nfci_cache': cache}
        with patch.object(monitor, 'TARGET_DATE', '2026-10-14'), \
                patch.object(monitor, 'financial_conditions_series') as official, \
                patch.object(monitor, 'fred_series') as fred:
            for now in ('2026-10-14T08:29:00-04:00', '2026-10-14T08:29:59-04:00'):
                with patch.object(monitor, 'NOW', datetime.fromisoformat(now)):
                    self.assertEqual(monitor.cached_financial_conditions(previous)[0][-1], old[-1])
            official.assert_not_called()
            fred.assert_not_called()
        with patch.object(monitor, 'TARGET_DATE', '2026-10-14'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-14T20:15:00-04:00')), \
                patch.object(monitor, 'financial_conditions_series', return_value=official_old) as official, \
                patch.object(monitor, 'fred_series', return_value=old) as fred:
            pending = {}
            self.assertEqual(monitor.cached_financial_conditions(previous, pending)[0][-1], old[-1])
            self.assertEqual(pending['result'], 'pending')
            self.assertEqual((official.call_count, fred.call_count), (1, 1))
            with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-14T20:45:00-04:00')):
                self.assertEqual(monitor.cached_financial_conditions({'nfci_cache': pending})[0][-1], old[-1])
            self.assertEqual((official.call_count, fred.call_count), (1, 1))
        with patch.object(monitor, 'TARGET_DATE', '2026-10-15'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-15T20:15:00-04:00')), \
                patch.object(monitor, 'financial_conditions_series', return_value=old) as official, \
                patch.object(monitor, 'fred_series', return_value=new) as fred:
            updated = {}
            self.assertEqual(monitor.cached_financial_conditions({'nfci_cache': pending}, updated)[0][-1], new[-1])
            self.assertEqual(updated['result'], 'ok')
            self.assertEqual(updated['source'], 'NFCI周度数据（FRED同步备用）')
            self.assertEqual((official.call_count, fred.call_count), (1, 1))
        with patch.object(monitor, 'TARGET_DATE', '2026-10-16'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-16T20:15:00-04:00')), \
                patch.object(monitor, 'financial_conditions_series') as official, \
                patch.object(monitor, 'fred_series') as fred:
            state = {}
            self.assertEqual(monitor.cached_financial_conditions({'nfci_cache': updated}, state)[0][-1], new[-1])
            official.assert_not_called()
            fred.assert_not_called()

    def test_nfci_cache_historical_target_cannot_use_later_publication(self):
        from datetime import datetime, timedelta
        old = [('2026-10-02', -0.494)]
        new = old + [('2026-10-09', -0.480)]
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-15T20:15:00-04:00')), \
                patch.object(monitor, 'TARGET_DATE', '2026-10-13'), \
                patch.object(monitor, 'financial_conditions_series', return_value=[]), \
                patch.object(monitor, 'fred_series', return_value=new):
            state = {}
            self.assertEqual(monitor.cached_financial_conditions({}, state)[0][-1], old[-1])
            self.assertEqual(state['rows'][-1], old[-1])
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-16T20:15:00-04:00')), \
                patch.object(monitor, 'TARGET_DATE', '2026-07-03'), \
                patch.object(monitor, 'financial_conditions_series', return_value=[]), \
                patch.object(monitor, 'fred_series', return_value=[
                    ((datetime(2026, 5, 1) + timedelta(days=7 * i)).date().isoformat(), -0.5)
                    for i in range(24)]):
            self.assertEqual(monitor.cached_financial_conditions({})[0][-1][0], '2026-06-26')

    def test_nfci_cache_retries_same_day_and_requires_valid_metadata(self):
        from datetime import datetime
        old = [('2026-10-02', -0.494)]
        new = old + [('2026-10-09', -0.480)]
        prior = {'rows': old, 'source': 'NFCI周度数据（FRED同步备用）',
                 'checked_at': '2026-10-14T09:00:00-04:00', 'result': 'pending'}
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-14T20:15:00-04:00')), \
                patch.object(monitor, 'TARGET_DATE', '2026-10-14'), \
                patch.object(monitor, 'financial_conditions_series', return_value=old) as official, \
                patch.object(monitor, 'fred_series', return_value=new) as fred:
            state = {}
            self.assertEqual(monitor.cached_financial_conditions({'nfci_cache': prior}, state)[0][-1], new[-1])
            self.assertEqual((official.call_count, fred.call_count), (1, 1))
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-14T07:00:00-04:00')), \
                patch.object(monitor, 'TARGET_DATE', '2026-10-14'), \
                patch.object(monitor, 'financial_conditions_series', return_value=old) as official, \
                patch.object(monitor, 'fred_series', return_value=[]):
            for bad in (dict(prior, checked_at=''), dict(prior, result='unknown')):
                self.assertEqual(monitor.cached_financial_conditions({'nfci_cache': bad})[0][-1], old[-1])
            self.assertEqual(official.call_count, 2)

    def test_nfci_cache_rejects_bad_state_and_never_makes_stale_data_green(self):
        from datetime import datetime
        old = [('2026-09-25', -0.510), ('2026-10-02', -0.494)]
        prior = {'rows': old, 'source': 'NFCI周度数据（FRED同步备用）',
                 'checked_at': '2026-10-15T20:15:00-04:00', 'result': 'pending'}
        with patch.object(monitor, 'TARGET_DATE', '2026-10-16'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-16T20:15:00-04:00')), \
                patch.object(monitor, 'financial_conditions_series', return_value=[]) as official, \
                patch.object(monitor, 'fred_series', return_value=[]) as fred, \
                patch.object(monitor, 'cboe_vix_series', return_value=[]), \
                patch.object(monitor, 'market_index_series', return_value=[]), \
                patch.object(monitor, 'treasury_yield_series', return_value=[]), \
                patch.object(monitor, 'market_breadth', return_value={'ok': False, 'name': '上涨参与度'}), \
                patch.object(monitor, 'MACRO_RETRY_WAIT', 0):
            state = {}
            macro = monitor.build_macro({'nfci_cache': prior}, state)
            self.assertEqual(official.call_count, 1)
            self.assertEqual(sum(call.args == ('NFCI',) for call in fred.call_args_list), 1)
            self.assertFalse(macro['nfci']['ok'])
            self.assertEqual(state['rows'][-1], old[-1])
            self.assertEqual(state['result'], 'error')
            self.assertIn('金融压力NFCI', monitor.market_risk_summary(macro)['missing'])
        bad = [dict(prior, rows=[['2026-10-16', 0.0]]),
               dict(prior, rows=[[old[-1][0], float('nan')]]),
               dict(prior, source='未核实来源'),
               dict(prior, rows=[['2026-10-09', -0.48]])]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-13'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-13T22:30:00+08:00')), \
                patch.object(monitor, 'financial_conditions_series', return_value=old) as official, \
                patch.object(monitor, 'fred_series', return_value=[]):
            for invalid in bad:
                with self.subTest(invalid=invalid):
                    state = {}
                    self.assertEqual(monitor.cached_financial_conditions({'nfci_cache': invalid}, state)[0][-1], old[-1])
                    self.assertEqual(state['result'], 'ok')
            self.assertEqual(official.call_count, len(bad))

    def test_nfci_new_snapshot_state_survives_existing_build_path(self):
        from datetime import datetime
        rows = [('2026-09-25', -0.510), ('2026-10-02', -0.494)]
        with patch.object(monitor, 'TARGET_DATE', '2026-10-13'), \
                patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-13T22:30:00+08:00')), \
                patch.object(monitor, 'financial_conditions_series', return_value=[]), \
                patch.object(monitor, 'fred_series', return_value=rows) as fred, \
                patch.object(monitor, 'cboe_vix_series', return_value=[]), \
                patch.object(monitor, 'market_index_series', return_value=[]), \
                patch.object(monitor, 'treasury_yield_series', return_value=[]), \
                patch.object(monitor, 'market_breadth', return_value={'ok': False, 'name': '上涨参与度'}), \
                patch.object(monitor, 'MACRO_RETRY_WAIT', 0):
            first_state = {}
            macro = monitor.build_macro({}, first_state)
            snapshot = monitor.build_snapshot(macro, [], {'positions': {}, 'focus': {}})
            snapshot['nfci_cache'] = first_state
            saved = json.loads(json.dumps(snapshot))
            self.assertEqual(saved['nfci_cache']['rows'][-1], ['2026-10-02', -0.494])
            self.assertTrue(saved['market_risk']['valid_count'] >= 1)
            self.assertEqual(macro['nfci']['name'], '金融压力NFCI')
            second_state = {}
            macro2 = monitor.build_macro(saved, second_state)
            self.assertEqual(macro2['nfci']['value'], -0.494)
            self.assertEqual(second_state['rows'][-1], ('2026-10-02', -0.494))
            self.assertEqual(sum(call.args == ('NFCI',) for call in fred.call_args_list), 1)

    def test_nfci_holiday_week_freshness_and_stale_guard(self):
        # 10/12 联邦假日使 10/09 当周数据顺延至 10/15 发布；10/13 的最新已发布周为 10/02。
        original = [('2026-04-17', -0.49), ('2026-04-24', -0.518)]
        backup = [('2026-09-25', -0.510), ('2026-10-02', -0.494)]
        def fred(series, days=400, alias=None, source_info=None):
            return backup if series == 'NFCI' else []
        with patch.object(monitor, 'financial_conditions_series', return_value=original), \
                patch.object(monitor, 'fred_series', side_effect=fred), \
                patch.object(monitor, 'cboe_vix_series', return_value=[]), \
                patch.object(monitor, 'market_index_series', return_value=[]), \
                patch.object(monitor, 'treasury_yield_series', return_value=[]), \
                patch.object(monitor, 'market_breadth', return_value={'ok': False, 'name': '上涨参与度'}), \
                patch.object(monitor, 'MACRO_RETRY_WAIT', 0):
            with patch.object(monitor, 'TARGET_DATE', '2026-10-13'):
                macro = monitor.build_macro()
                self.assertTrue(macro['nfci']['ok'])
                self.assertEqual(macro['nfci']['name'], '金融压力NFCI')
                self.assertEqual(macro['nfci']['date'], '2026-10-02')
                self.assertAlmostEqual(macro['nfci']['value'], -0.494)
                self.assertIn('FRED同步备用', macro['nfci']['source'])
                self.assertEqual(monitor.market_risk_summary(macro)['levels']['nfci'], 'green')
            with patch.object(monitor, 'TARGET_DATE', '2026-10-16'):
                stale = monitor.build_macro()
                self.assertFalse(stale['nfci']['ok'])
                self.assertEqual(monitor.market_risk_summary(stale)['levels']['nfci'], 'gray')

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
                patch.object(monitor, 'yahoo_history', side_effect=lambda code: dict(
                    bars(), quote_symbol=code, quote_type='FUTURE' if code.endswith('=F') else 'INDEX',
                    currency='USD', quote_timezone='America/New_York')) as yahoo, \
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
        index = page.split('id="group_index_funds"', 1)[1].split('<div class="group-bottom">', 1)[0]
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
        # 此用例校验历史收盘快照；运行模式必须显式固定，不能随真实盘中时钟漂移。
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                patch.object(monitor, 'CLOSED_ONLY', True), \
                patch.object(monitor, 'global_dca', return_value=None), patch.object(monitor, 'RISK_IDENTITIES', {'^GSPC', '^VIX'}):
            snap = monitor.build_snapshot({}, [reference, daily], cfg)
        self.assertEqual(snap['summary']['reference_dates'], {'BD#US10Y': '2026-10-06'})
        self.assertEqual(snap['summary']['stale_symbols'], [])
        self.assertEqual((snap['summary']['today_prices'], snap['summary']['prior_prices']), (1, 0))
        self.assertEqual(snap['actual_dates'], {'min': '2026-10-07', 'max': '2026-10-07'})
        self.assertEqual(snap['data_time_text'], '2026-10-07 收盘（美东交易日）')
        with patch.object(monitor, 'RISK_IDENTITIES', {'^GSPC', '^VIX'}):
            page = monitor.render({}, [reference, daily], 2, snapshot=snap)
        self.assertIn('个股行情 2/2 已更新', page)
        self.assertIn('市场风险数据缺失或过期', page)
        self.assertNotIn('抓取成功', page)
        self.assertNotIn('项参考值（截至', page)
        self.assertNotIn('当日行情未取得', page)
        self.assertIn('参考值 · 截至 2026-10-06', page)

    def test_older_reference_fixture_is_independent_of_wall_clock_and_mode(self):
        # 同一历史收盘样本在盘中、盘前、盘后、休市及冬令时均应稳定；
        # 只固定样本模式，不把生产盘中显示强行改成收盘，也不修改全局默认。
        real_datetime = monitor.datetime
        moments = (
            '2026-10-12T21:29:00+08:00',
            '2026-10-12T21:30:00+08:00',
            '2026-10-12T23:21:01+08:00',
            '2026-10-13T04:00:00+08:00',
            '2026-10-10T23:21:01+08:00',
            '2026-12-09T22:29:00+08:00',
            '2026-12-09T22:30:00+08:00',
            '2026-12-10T05:00:00+08:00',
        )
        for iso in moments:
            instant = real_datetime.fromisoformat(iso)

            class FixtureClock(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

            for inherited_mode in (False, True):
                with self.subTest(when=iso, closed_only=inherited_mode), \
                        patch.object(monitor, 'datetime', FixtureClock), \
                        patch.object(monitor, 'NOW', instant), \
                        patch.object(monitor, 'CLOSED_ONLY', inherited_mode):
                    self.test_older_reference_does_not_mark_all_daily_quotes_stale()
                    self.assertEqual(monitor.CLOSED_ONLY, inherited_mode)

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
        with tempfile.TemporaryDirectory() as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                    patch.object(monitor, 'fetch_history', return_value={'price': 100}) as fetch, \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})), \
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
        with tempfile.TemporaryDirectory() as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-02'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', list(bench_rows)), \
                    patch.object(monitor, 'sp500_rows_fallback', return_value=list(fallback_rows or [])) as fallback, \
                    patch.object(monitor, 'fetch_history', side_effect=hist), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})), \
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
        self.assertIn('20日对.SPX', head)
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
        self.assertIn('20日对.SPX', page.split('id="group_technology"', 1)[1].split('</summary>', 1)[0])

    def test_main_relative_strength_falls_back_to_spy_only_when_no_index_data(self):
        rs, page, fetched, fallback = self._run_main_for_rs([], [])
        self.assertEqual(fetched, ['XLK', 'SPY'])
        self.assertEqual(rs['bench_name'], 'SPY')
        head = page.split('id="group_technology"', 1)[1].split('</summary>', 1)[0]
        self.assertIn('20日对SPY', head)                   # 明确标注是 SPY 近似，不冒充指数

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
        with tempfile.TemporaryDirectory() as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
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
        index = page.split('id="group_index_funds"', 1)[1].split('<div class="group-bottom">', 1)[0]
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
        with tempfile.TemporaryDirectory() as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'fetch_history', side_effect=fetch), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})), \
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
        with tempfile.TemporaryDirectory() as directory:
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
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'fetch_history', side_effect=slow_fail), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})), \
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
        self.assertEqual(monitor.earnings_label({'status': 'unknown'})[0], '日期待确认')
        self.assertIn('不代表没有财报', monitor.earnings_label({'status': 'error'})[1])
        text, note = monitor.earnings_label(dict(ok, date='2026-10-30'))
        self.assertIn('美东 10/30 盘后', text)
        self.assertNotIn('财报', text)   # 「财报」二字只写在表头
        self.assertIn('北京 10/31 凌晨', note)   # 2026-10-15 精简：北京时间说明去掉「约」与冗余字
        self.assertNotIn('不含具体钟点', note)
        self.assertNotIn('数据源只给', note)
        self.assertIn('具体时段待定', monitor.earnings_label({'status': 'ok', 'date': '2026-10-30', 'timing': '', 'kind': 'expected'})[0])

    def test_earnings_shown_for_every_fetched_stock_but_not_etf_or_index(self):
        def row(symbol, group, level):
            return {'symbol': symbol, 'group': group, 'level': level, 'price': 10, 'note': ''}
        items = [row('AAPL', 'position', 'green'), row('MSFT', 'focus', 'green'),
                 row('NVDA', 'technology', 'green'), row('AMD', 'technology', 'yellow'),
                 row('TSLA', 'technology', 'green'),
                 row('XLK', 'technology', 'red'), row('SPY', 'position', 'green'),
                 row('.VIX', 'index_funds', 'red')]
        asked = []
        dates = {'NVDA': '2026-12-20', 'TSLA': '2026-10-15'}
        def earnings(symbol):
            asked.append(symbol)
            return {'status': 'ok', 'date': dates.get(symbol, '2026-10-30'), 'timing': 'post', 'kind': 'expected'}
        with patch.object(monitor, 'nasdaq_earnings', side_effect=earnings):
            stat = monitor.attach_earnings(items, None, '2026-10-09')
        self.assertEqual(sorted(asked), ['AAPL', 'AMD', 'MSFT', 'NVDA', 'TSLA'])   # ETF/指数/期货不抓
        self.assertEqual(stat['targets'], 5)
        by_symbol = {d['symbol']: d for d in items}
        for sym in ('AAPL', 'MSFT', 'NVDA', 'AMD', 'TSLA'):
            self.assertIn('earnings', by_symbol[sym], sym)             # 抓到的都显示，不论分组和灯色
        self.assertNotIn('earnings', by_symbol['XLK'])
        self.assertNotIn('earnings', by_symbol['SPY'])
        self.assertNotIn('earnings', by_symbol['.VIX'])
        with patch.object(monitor, 'nasdaq_earnings', side_effect=RuntimeError('boom')):
            stat = monitor.attach_earnings([row('AAPL', 'position', 'green')], None, '2026-10-09')
        self.assertEqual(stat['targets'], 1)

    def test_earnings_refetch_schedule_by_distance(self):
        due = monitor.earnings_refetch_due
        today = '2026-10-10'
        exp = lambda date, fetched: {'status': 'ok', 'kind': 'expected', 'date': date, 'fetched': fetched}
        est = lambda date, fetched: {'status': 'ok', 'kind': 'estimated', 'date': date, 'fetched': fetched}
        # 已确认的日期：远 14 天、近 7 天、3 天内不追抓
        self.assertFalse(due(exp('2026-11-10', '2026-09-27'), today))    # 13 天前
        self.assertTrue(due(exp('2026-11-10', '2026-09-26'), today))     # 满 14 天
        self.assertFalse(due(exp('2026-10-20', '2026-10-04'), today))    # 4~14 天：6 天前
        self.assertTrue(due(exp('2026-10-20', '2026-10-03'), today))     # 满 7 天
        self.assertFalse(due(exp('2026-10-12', '2026-10-08'), today))    # 3 天内：2 天前，不追
        self.assertFalse(due(exp('2026-10-10', '2026-10-09'), today))
        self.assertTrue(due(exp('2026-10-13', '2026-10-07'), today))     # 3 天内但缓存已满 3 天：补查一次
        self.assertTrue(due(exp('2026-10-09', '2026-10-09'), today))     # 财报日已过：立即重抓
        # 算法估算的日期：远 7 天、近 3 天
        self.assertFalse(due(est('2026-11-10', '2026-10-04'), today))
        self.assertTrue(due(est('2026-11-10', '2026-10-03'), today))
        self.assertFalse(due(est('2026-10-20', '2026-10-08'), today))
        self.assertTrue(due(est('2026-10-20', '2026-10-07'), today))
        self.assertTrue(due({'status': 'ok', 'date': '2026-11-10', 'fetched': '2026-10-03'}, today))  # 缺 kind 按估算
        self.assertTrue(due({'status': 'error', 'fetched': today}, today))
        self.assertFalse(due({'status': 'unknown', 'fetched': '2026-10-04'}, today))
        self.assertTrue(due({'status': 'unknown', 'fetched': '2026-10-03'}, today))
        self.assertFalse(due({'status': 'na', 'reason': 'http404', 'fetched': '2026-09-15'}, today))
        self.assertTrue(due({'status': 'na', 'fetched': '2026-09-10'}, today))
        self.assertTrue(due({}, today))
        self.assertTrue(due(None, today))
        self.assertTrue(due({'status': 'ok', 'date': 'bad', 'fetched': today}, today))
        self.assertTrue(due(exp('2026-11-10', '2026-10-20'), today))     # 缓存时间在未来：重抓

    def test_earnings_cache_reuse_and_fallbacks(self):
        def row(symbol, group='technology', level='green'):
            return {'symbol': symbol, 'group': group, 'level': level, 'price': 10, 'note': ''}
        today = '2026-10-10'
        prev = {'AAPL': {'status': 'ok', 'date': '2026-11-20', 'timing': 'post', 'kind': 'expected', 'fetched': '2026-10-08'},
                'MSFT': {'status': 'ok', 'date': '2026-10-12', 'timing': 'post', 'kind': 'expected', 'fetched': '2026-10-06'},
                'AMD': {'status': 'ok', 'date': '2026-10-08', 'timing': 'pre', 'kind': 'expected', 'fetched': '2026-10-01'}}
        asked = []
        def fetch(symbol):
            asked.append(symbol)
            return {'status': 'ok', 'date': '2026-11-03', 'timing': 'pre', 'kind': 'expected'}
        items = [row('AAPL', 'position'), row('MSFT', 'position'), row('AMD', 'position'), row('NEW', 'position')]
        with patch.object(monitor, 'nasdaq_earnings', side_effect=fetch):
            stat = monitor.attach_earnings(items, prev, today)
        self.assertEqual(sorted(asked), ['AMD', 'MSFT', 'NEW'])        # AAPL 命中缓存；临近的、过期的、新的要抓
        self.assertEqual(stat['fetched'], 3)
        by = {d['symbol']: d for d in items}
        self.assertEqual(by['AAPL']['earnings']['date'], '2026-11-20')
        self.assertEqual(by['AMD']['earnings']['date'], '2026-11-03')  # 过期日期不显示，换新的
        self.assertEqual(stat['data']['NEW']['fetched'], today)
        # 失败：日期还有效 → 沿用旧值；没有可用旧值 → 显示失败且不进缓存
        with patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'error'}):
            items = [row('MSFT', 'position'), row('AMD', 'position'), row('NEW', 'position')]
            stat = monitor.attach_earnings(items, prev, today)
        by = {d['symbol']: d for d in items}
        self.assertEqual(by['MSFT']['earnings']['date'], '2026-10-12')
        self.assertEqual(by['AMD']['earnings']['status'], 'error')
        self.assertNotIn('AMD', stat['data'])
        self.assertNotIn('NEW', stat['data'])
        self.assertEqual(stat['failed'], 2)
        # 旧快照缺字段或格式不对不报错
        for bad in (None, [], 'x', {'AAPL': 'x'}, {'AAPL': {}}):
            with patch.object(monitor, 'nasdaq_earnings', side_effect=fetch):
                monitor.attach_earnings([row('AAPL', 'position')], bad, today)

    def test_earnings_fetch_cap_defers_low_priority(self):
        def row(symbol, group, level='green'):
            return {'symbol': symbol, 'group': group, 'level': level, 'price': 10, 'note': ''}
        items = [row('AAA', 'technology'), row('BBB', 'technology', 'yellow'), row('CCC', 'position')]
        asked = []
        def fetch(symbol):
            asked.append(symbol)
            return {'status': 'ok', 'date': '2026-11-03', 'timing': 'pre', 'kind': 'expected'}
        with patch.object(monitor, 'EARN_MAX_FETCH_PER_RUN', 2), patch.object(monitor, 'nasdaq_earnings', side_effect=fetch):
            stat = monitor.attach_earnings(items, None, '2026-10-10')
        self.assertEqual(sorted(asked), ['BBB', 'CCC'])                # 持仓、黄灯优先
        self.assertEqual(stat['deferred'], 1)

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
        self.assertIn('20日对.SPX +1.8%', tech)
        self.assertIn('class="rs-spy up"', tech)
        self.assertLess(tech.index('stat-badge red'), tech.index('rs-spy'))    # 在红黄灯后面
        self.assertIn('XLK 20日 +5.2% − 标普500 +3.4% = +1.8 个百分点', tech)
        self.assertIn('class="rs-spy down"', head('healthcare'))
        self.assertIn('20日对.SPX -3.0%', head('healthcare'))
        self.assertIn('class="rs-spy "', head('financials'))                    # ±0.5 内持平不着色
        self.assertNotIn('rs-spy', head('energy'))
        self.assertIn('20日对.SPX +1.8%', head('industrials'))
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
        # 2026-10-19：定投提醒挪到了顶栏最右侧，不再挂在市场风险标题里
        title = lambda html: html
        quiet = page({**base, 'due': False, 'next_in': 5, 'next_date': '2026-10-15'})
        self.assertIn('<span class="dca-badge" aria-label="定投提醒：还有 5 个交易日（2026-10-15）">定投：5</span>',
                      title(quiet))  # 放在市场风险标题右侧，平时不黄
        self.assertNotIn('class="dca ', quiet)  # 旧的独立提醒行已移除
        due = page({**base, 'due': True, 'date': '2026-10-15', 'times': 2, 'next_in': 0})
        self.assertIn('<span class="dca-badge on" aria-label="定投提醒：今天是定投日（2026-10-15，第 2 次）">定投：0</span>',
                      title(due))
        pending = page({**base, 'pending': True, 'first': '2026-10-20', 'next_in': 3})
        self.assertIn('>定投：3</span>', title(pending))
        self.assertNotIn('class="dca-badge on"', pending)
        for broken in ({**base, 'due': False}, {**base, 'next_in': '5'}, {**base, 'next_in': -1}, None):
            self.assertNotIn('class="dca-badge"', title(page(broken)))  # 天数缺失/异常不显示，不猜
        with patch.object(monitor, 'S', {**monitor.S, 'dca_every': 20, 'dca_start': '2026-10-01'}):
            real = monitor.global_dca('2026-10-13')
            ahead = monitor.global_dca('2026-09-25')  # 起点前：周五→10/01 还差 4 个交易日
            holiday = monitor.global_dca('2026-11-26')  # 感恩节休市，按前一交易日算
        self.assertEqual((real['next_in'], real['next_date']), (12, '2026-10-29'))
        self.assertEqual((ahead['pending'], ahead['next_in'], ahead['first']), (True, 4, '2026-10-01'))
        self.assertEqual(holiday['date'], '2026-11-25')
        import exchange_calendars as xcals
        cal = xcals.get_calendar('XNYS')
        for day in ('2026-09-25', '2026-10-13', '2026-11-26', '2026-12-24', '2027-01-15'):
            with patch.object(monitor, 'S', {**monitor.S, 'dca_every': 20, 'dca_start': '2026-10-01'}):
                d = monitor.global_dca(day)
            self.assertTrue(cal.is_session(d.get('next_date') or d['first']), day)  # 定投日必是美股交易日

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

    def test_homepage_omits_duplicate_indicator_notes_and_keeps_other_notes(self):
        with patch.object(monitor, 'global_dca', return_value=None):
            page = monitor.render({}, [], 0, snapshot={})
        self.assertNotIn('marketRiskNotes', page)
        self.assertNotIn('指标阈值与数据说明', page)
        m = re.search(r'<details class="macro-help" id="marketRiskRules">(.*?)</details>', page, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn(' open', page[m.start():m.start() + 60])
        self.assertIn('综合判断规则', m.group(1))
        self.assertIn(monitor.BREADTH_CREDIT, page)
        self.assertIn('查看完整规则与阈值', page)
        notes = monitor._page_notes({'reference_alerts': {}})
        self.assertIn('id="policyAlertDetails"', notes)
        self.assertNotIn('marketRiskNotes', notes)
        self.assertNotIn(' open', notes)

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
        self.assertIn('季度ROE 23.6%→8.8%', text)           # 基准季含一次性收益：照常比较
        self.assertIn('基准季含一次性收益，仅供参考', text)
        self.assertNotIn('ROE未比较', text)
        self.assertEqual(result['compare'], '2025/9→2026/6')
        self.assertTrue(any('营业利率' in c for c in result['context']))

    def test_fundamentals_cache_by_quarter(self):
        today = '2026-10-10'
        entry = lambda period, fetched: {'fetched': fetched, 'data': {'period': period, 'base_period': '9/30/2025'}}
        due = monitor.fundamentals_refetch_due
        self.assertFalse(due(entry('6/30/2026', '2026-10-01'), today))    # 静默期内沿用
        self.assertTrue(due(entry('6/30/2026', '2026-09-09'), today))     # 放满 30 天：重抓
        self.assertFalse(due(entry('3/31/2026', '2026-10-08'), today))    # 过了静默期：3 天内不问
        self.assertTrue(due(entry('3/31/2026', '2026-10-07'), today))     # 过了静默期：满 3 天问一次
        self.assertTrue(due(None, today))
        self.assertTrue(due({'fetched': today}, today))
        self.assertTrue(due(entry('bad', today), today))
        self.assertTrue(due(entry('6/30/2026', '2026-10-20'), today))     # 时间在未来
        full = {'period': '6/30/2026', 'base_period': '9/30/2025',
                'inc': {'Total Revenue': {'value2': '$1', 'value5': '$2', 'value3': 'x'}, 'Junk': {'value2': '1'}},
                'bs': {}, 'cf': {}, 'rt': {'Gross Margin': {'value2': '50%', 'value5': '55%'}}}
        c = monitor.compact_financials(full)
        self.assertEqual(c['inc'], {'Total Revenue': {'value2': '$1', 'value5': '$2'}})
        self.assertEqual(c['rt']['Gross Margin']['value5'], '55%')
        self.assertIsNone(monitor.compact_financials(None))
        # 精简版结果与完整版一致
        table = lambda rows: {k: dict(zip(('value2', 'value5'), v)) for k, v in rows.items()}
        facts = {'period': '6/30/2026', 'base_period': '9/30/2025',
                 'inc': table({'Total Revenue': ('$700', '$1,000'), 'Income Tax': ('$1', '$1')}),
                 'bs': table({'Total Liabilities': ('$5', '$5'), 'Total Equity': ('$5', '$5')}),
                 'cf': table({'Net Cash Flow-Operating': ('$20', '$20')}),
                 'rt': table({'Operating Margin': ('10%', '10%'), 'Profit Margin': ('9%', '9%')})}
        self.assertEqual(monitor.fundamental_check('UBER', monitor.compact_financials(facts)),
                         monitor.fundamental_check('UBER', facts))
        self.assertIsNone(monitor.fundamental_check('UBER', {}))        # 传空：不联网，直接无结果

    def test_update_fundamentals_reuses_cache_and_falls_back(self):
        today = '2026-10-10'
        fresh = {'period': '6/30/2026', 'base_period': '9/30/2025', 'inc': {}, 'bs': {}, 'cf': {}, 'rt': {}}
        prev = {'AAPL': {'fetched': '2026-10-01', 'data': fresh},
                'MSFT': {'fetched': '2026-09-01', 'data': fresh}}
        asked = []
        def fetch(sym):
            asked.append(sym)
            return None if sym == 'MSFT' else dict(fresh, period='9/30/2026')
        # 备用源也要关掉：这个用例断言的是「Nasdaq 取不到就沿用旧值」，不能让它真去联网兜底
        with patch.object(monitor, 'nasdaq_financials', side_effect=fetch), \
                patch.object(monitor, 'stockanalysis_financials', return_value=None):
            facts, stat = monitor.update_fundamentals(['AAPL', 'MSFT', 'NEW'], prev, today)
        self.assertEqual(sorted(asked), ['MSFT', 'NEW'])                   # AAPL 命中缓存
        self.assertEqual((stat['fetched'], stat['cached']), (2, 1))
        self.assertEqual(stat['data']['NEW']['fetched'], today)
        self.assertEqual(stat['data']['MSFT']['fetched'], '2026-09-01')    # 没取到：沿用旧数据，不刷新时间
        self.assertIn('MSFT', facts)
        for bad in (None, [], 'x', {'AAPL': 'x'}, {'AAPL': {'data': 1}}):
            with patch.object(monitor, 'nasdaq_financials', return_value=None), \
                    patch.object(monitor, 'stockanalysis_financials', return_value=None):
                monitor.update_fundamentals(['AAPL'], bad, today)

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
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, 'settings.json'), 'w', encoding='utf-8') as f:
                json.dump(bad, f)
            with patch.object(monitor, 'BASE', directory):
                loaded = monitor.load_settings()
        self.assertEqual(loaded['boll_n'], 20)
        self.assertEqual(loaded['rsi_low'], 35)
        self.assertEqual(loaded['amp_red'], 8)
        self.assertEqual(loaded['chg_red'], 4.5)
        self.assertEqual(monitor.valid_settings(monitor.DEFAULT_SETTINGS), '')

    def test_moving_average_periods_are_configurable_and_validated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
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
            self.assertTrue(any('250日均线' in x for x in signals), signals)   # 现价在 250 日线 ±1% 内
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

    def test_ma_zero_settings_validate_and_persist_independently(self):
        import tempfile
        legal = [(0, 0), (0, 250), (50, 0), (250, 0), (0, 2), (2, 250), (50, 50)]
        with tempfile.TemporaryDirectory() as directory:
            for short, long in legal:
                values = dict(monitor.DEFAULT_SETTINGS, ma_short=short, ma_long=long)
                self.assertEqual(monitor.valid_settings(values), '', (short, long))
                with open(os.path.join(directory, 'settings.json'), 'w', encoding='utf-8') as f:
                    json.dump(values, f)
                with patch.object(monitor, 'BASE', directory):
                    self.assertEqual(monitor.load_settings(), values)
            for key in ('ma_short', 'ma_long'):
                for bad in (1, -1, 0.5, 50.5, 251, True, None, '', '0', [], {}):
                    values = dict(monitor.DEFAULT_SETTINGS, **{key: bad})
                    self.assertTrue(monitor.valid_settings(values), (key, bad))
                    with open(os.path.join(directory, 'settings.json'), 'w', encoding='utf-8') as f:
                        json.dump(values, f)
                    with patch.object(monitor, 'BASE', directory):
                        self.assertEqual(monitor.load_settings(), monitor.DEFAULT_SETTINGS)
        self.assertTrue(monitor.valid_settings(dict(monitor.DEFAULT_SETTINGS, ma_short=250, ma_long=50)))
        self.assertTrue(monitor.valid_settings(dict(monitor.DEFAULT_SETTINGS, ma_short=0, ma_long=0, boll_n=0)))

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

    def test_ensure_schedule_run_redispatches_when_daily_bars_stale(self):
        """结果存在但当日日线不齐（源端换日空档）→ 保险必须补跑；齐了才跳过。"""
        import tempfile
        from unittest.mock import MagicMock
        real = monitor.datetime

        class At(real):
            @classmethod
            def now(cls, tz=None):
                return real.fromisoformat('2026-10-17T00:45:00+00:00')   # 美东 10-16 20:45

        def run(summary, actual=None):
            get = MagicMock()
            get.return_value.json.return_value = {'workflow_runs': []}
            post = MagicMock()
            snap = {'target_trade_date': '2026-10-16', 'mode': 'closed',
                    'summary': {'total': 100, **summary}}
            if actual:
                snap['actual_dates'] = actual
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(monitor, 'BASE', tmp), patch.object(monitor, 'datetime', At), \
                    patch.object(monitor.requests, 'get', get), patch.object(monitor.requests, 'post', post):
                with open(os.path.join(tmp, 'status.json'), 'w', encoding='utf-8') as f:
                    json.dump(snap, f)
                self.assertEqual(monitor.ensure_schedule_run(), 0)
            return post.call_count
        # 10-17 那次的真实形态：当日 1、较早 99 → 补跑
        self.assertEqual(run({'today_prices': 1, 'prior_prices': 99, 'missing_prices': 0}), 1)
        # 大面积缺价（源全挂）也算不齐
        self.assertEqual(run({'today_prices': 10, 'prior_prices': 0, 'missing_prices': 90}), 1)
        # 齐：少数停牌/不支持代码不影响
        self.assertEqual(run({'today_prices': 97, 'prior_prices': 1, 'missing_prices': 2,
                              'unsupported_symbols': ['X', 'Y']}), 0)
        # 旧快照没有计数字段：看 actual_dates.max
        self.assertEqual(run({}, {'min': '2026-10-16', 'max': '2026-10-16'}), 0)
        self.assertEqual(run({}, {'min': '2026-10-15', 'max': '2026-10-15'}), 1)

    def test_schedule_run_with_stale_bars_does_not_publish(self):
        """定时口径当日日线不齐：不写 index.html/status.json、不推送、输出 publish=no；手动口径照常发布。"""
        import tempfile
        cfg = {'positions': {'AAPL': {}, 'MSFT': {}}}

        def analyzed(symbol, settings, data, group):
            detail = dict(symbol=symbol, note='', price=100, chg=0, level='green',
                          group=group, signals=[], data_date='2026-10-15')   # 停在前一交易日
            return 'green', [], detail

        def run(event):
            with tempfile.TemporaryDirectory() as directory:
                for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                    with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                        json.dump(content, f)
                out_file = os.path.join(directory, 'gh_output')
                with patch.dict(os.environ, {'GITHUB_OUTPUT': out_file}), \
                        patch.object(monitor, 'EVENT', event), patch.object(monitor, 'CLOSED_ONLY', True), \
                        patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-16'), \
                        patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                        patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                        patch.object(monitor, 'build_reference_alerts', return_value={}), \
                        patch.object(monitor, 'build_macro', return_value={}), \
                        patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                        patch.object(monitor, 'fetch_history', return_value={'price': 100}), \
                        patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                        patch.object(monitor, 'fundamental_check', return_value=None), \
                        patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})), \
                        patch.object(monitor, 'update_consensus', return_value=({}, {})), \
                        patch.object(monitor, 'global_dca', return_value=None), \
                        patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan') as notify:
                    self.assertEqual(monitor.main([]), 0)
                gh = open(out_file, encoding='utf-8').read() if os.path.exists(out_file) else ''
                wrote = os.path.exists(os.path.join(directory, 'status.json'))
                status = json.load(open(os.path.join(directory, 'status.json'), encoding='utf-8')) if wrote else None
                return gh, wrote, notify.called, status
        gh, wrote, notified, _ = run('schedule')
        self.assertIn('publish=no', gh)
        self.assertFalse(wrote)
        self.assertFalse(notified)
        gh, wrote, notified, status = run('workflow_dispatch')
        self.assertNotIn('publish=no', gh)
        self.assertTrue(wrote)
        self.assertEqual(status['data_lag']['fresh'], False)
        self.assertEqual((status['data_lag']['today'], status['data_lag']['expected']), (0, 2))

    def test_trim_history_keeps_missing_validity_key_missing(self):
        """源没给 ma_range_valid 时，裁剪不能补空列表，否则 52周区间与日内高低整段判废。

        逐个数据源核对：只有 yahoo_history 输出 ma_range_valid，stooq_history / nasdaq_history 都不带，
        所以只要回退到这两个源，该标的的位置条就会消失（页面只写「数据待更新」，不报错）。
        """
        from datetime import date as _date, timedelta as _td
        n = 400          # 跨度要过 357 天，scope 才是「52周」而不是「可用历史」
        dates = [(_date(2026, 10, 14) - _td(days=i)).isoformat() for i in range(n)][::-1]
        data = {'dates': dates, 'closes': [100.0] * n, 'highs': [101.0] * n,
                'lows': [99.0] * n, 'volumes': [1000] * n, 'price': 100.0,
                'prev_close': 100.0}          # 故意不带 ma_range_valid（stooq / Nasdaq 就是这样）
        out = monitor.trim_history(data, dates[-1])
        self.assertIsNotNone(out)
        self.assertNotIn('ma_range_valid', out)
        r = monitor._range52_from_history(out)
        self.assertIsNotNone(r, "源没给有效位时仍要能算 52 周区间")
        self.assertEqual(r['scope'], '52周')
        x = monitor._analysis_context('T', {}, out, 'technology')
        self.assertEqual(monitor._ma_day_range(x), (99.0, 101.0, True))  # 日内高低也不该被误判缺失
        empty = dict(out, ma_range_valid=[])   # 防御：万一别处补了空列表，下游也当「没给」
        self.assertIsNotNone(monitor._range52_from_history(empty))
        self.assertEqual(monitor._ma_day_range(monitor._analysis_context('T', {}, empty, 'technology')),
                         (99.0, 101.0, True))

    def test_fund_cache_note_and_no_data_backoff(self):
        """源不覆盖这只标的（TSM 那种外国发行人）时：文案不能再写「沿用缓存」，且别天天重取。"""
        note = monitor._fund_cache_note
        self.assertEqual(note({'fetch_error': True, 'fetched': None, 'event_date': '2026-10-15'}),
                         '该源暂无财报数据')
        self.assertNotIn('沿用缓存', note({'fetch_error': True, 'fetched': None}))
        self.assertIn('读取失败，沿用缓存（上次成功取数 2026-10-01）',
                      note({'fetch_error': True, 'fetched': '2026-10-01'}))
        self.assertEqual(note({'event_date': '2026-10-15', 'fetched': '2026-10-01'}),
                         '财报已到期2026-10-01待更新')
        self.assertEqual(note({}), '')
        self.assertEqual(note(None), '')
        # 一次都没取到过：重试间隔内不再取，过了间隔再确认一次
        due = monitor.fundamentals_refetch_due
        empty_err = {'fetch_error': True, 'attempted': '2026-10-10', 'event_date': '2026-10-09'}
        self.assertFalse(due(empty_err, '2026-10-12'))     # 距上次 2 天 < 7 天
        self.assertTrue(due(empty_err, '2026-10-20'))      # 10 天后重新确认
        # 有缓存的失败仍按天重试，不受这条影响
        self.assertTrue(due({'fetch_error': True, 'fetched': '2026-10-01', 'attempted': '2026-10-10',
                             'event_date': '2026-10-09'}, '2026-10-12'))

    def test_stockanalysis_fallback_when_nasdaq_returns_nothing(self):
        """Nasdaq 财报接口不覆盖的标的（外国发行人，如 TSM）由 stockanalysis 季度表兜底。

        单位：stockanalysis 是「百万」，Nasdaq 是「千」（AAPL 实测 109,417 vs $109,417,000），
        所以金额 ×1000；百分比和倍数原样带过去。各表按「期间结束日」对齐，不按列下标。
        """
        from unittest.mock import MagicMock

        dates = ["Jun '26 Jun 30, 2026", "Mar '26 Mar 31, 2026",
                 "Dec '25 Dec 31, 2025", "Sep '25 Sep 30, 2025"]

        def page(rows, extra=None):
            cols = ([extra] + dates) if extra else dates
            out = ['<table>', '<tr><th>Fiscal Quarter</th>' + ''.join(f'<th>{c}</th>' for c in cols) + '</tr>',
                   '<tr><td>Period Ending</td>' + ''.join(f'<td>{c}</td>' for c in cols) + '</tr>']
            for label, vals in rows:
                out.append(f'<tr><td>{label}</td>' + ''.join(f'<td>{v}</td>' for v in vals) + '</tr>')
            return "".join(out) + '</table>'

        pages = {
            'income-statement': page([('Revenue', ['1,270,380', '1,134,103', '1,046,090', '989,918']),
                                      ('Income Tax Expense', ['155,649', '114,998', '107,212', '73,614']),
                                      ('Gross Margin', ['67.72%', '66.25%', '62.33%', '59.45%']),
                                      ('Operating Margin', ['60.34%', '58.10%', '53.92%', '50.58%']),
                                      ('Profit Margin', ['55.62%', '50.48%', '46.41%', '45.69%'])]),
            'balance-sheet': page([('Total Liabilities', ['2,901,184', '2,728,561', '2,536,623', '2,318,529']),
                                   ("Shareholders' Equity", ['6,474,471', '5,932,389', '5,396,219', '5,035,578'])]),
            'cash-flow-statement': page([('Operating Cash Flow', ['783,365', '698,976', '725,509', '426,829'])]),
            # 比率页多一列「当前」快照(10/14)，必须按日期取值而不是按列下标
            'ratios': page([('Current Ratio', ['2.46', '2.46', '2.49', '2.51', '2.69']),
                            ('Return on Equity (ROE)', ['45.57%', '40.45%', '37.20%', '37.44%', '34.50%'])],
                           extra="Oct '26 Oct 14, 2026"),
        }
        monitor._SA_FUND_CACHE.clear()
        self.addCleanup(monitor._SA_FUND_CACHE.clear)

        def get(url, timeout=None):
            resp = MagicMock()
            hit = next((v for k, v in pages.items() if '/' + k + '/' in url), None)
            resp.status_code = 200 if hit else 404
            resp.text = hit or ''
            return resp

        with patch.object(monitor, '_fund_session') as sess, \
                patch.object(monitor, 'nasdaq_financials', return_value=None):
            sess.return_value.get.side_effect = get
            f = monitor.financials_any_source('TSM')
        self.assertIsNotNone(f, "Nasdaq 没数据时必须走备用源")
        self.assertEqual(f['period'], '6/30/2026')
        self.assertEqual(f['base_period'], '9/30/2025')
        self.assertEqual(f['inc']['Total Revenue']['value2'], '1,270,380,000')   # 百万 → 千
        self.assertEqual(f['inc']['Total Revenue']['value5'], '989,918,000')
        self.assertEqual(f['cf']['Net Cash Flow-Operating']['value2'], '783,365,000')
        self.assertEqual(f['bs']['Total Equity']['value2'], '6,474,471,000')
        self.assertEqual(f['rt']['Operating Margin']['value2'], '60.34%')         # 百分比原样
        self.assertEqual(f['rt']['After Tax ROE']['value2'], '40.45%')            # 6/30 那列，不是首列
        self.assertEqual(f['rt']['Current Ratio']['value2'], '2.46')
        # 备用源的数据能直接喂给判定（生产里就是 update_fundamentals 刷新后这样传）
        got = monitor.fundamental_check('TSM', monitor.compact_financials(f))
        self.assertIsNotNone(got)
        self.assertEqual(got['period'], '6/30/2026')
        self.assertEqual(got['level'], 'green')

    def test_stockanalysis_not_consulted_when_nasdaq_has_data(self):
        """Nasdaq 取得到就绝不动用备用源，数字口径保持原样。"""
        from unittest.mock import MagicMock
        monitor._SA_FUND_CACHE.clear()
        self.addCleanup(monitor._SA_FUND_CACHE.clear)
        with patch.object(monitor, 'nasdaq_financials', return_value={'period': '6/27/2026', 'inc': {}}) as nas, \
                patch.object(monitor, 'stockanalysis_financials') as sa:
            monitor.financials_any_source('AAPL')
        nas.assert_called_once()
        sa.assert_not_called()

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
        reference = page.split('id="group_index_funds"', 1)[1].split('<div class="group-bottom">', 1)[0]
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
                patch.object(monitor, 'cboe_vix_series', return_value=[]), \
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
                patch.object(monitor, 'cboe_vix_series', return_value=[]), \
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
        index = page.split('id="group_index_funds"', 1)[1].split('<div class="group-bottom">', 1)[0]
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
        index = page.split('id="group_index_funds"',1)[1].split('<div class="group-bottom">',1)[0]
        self.assertFalse(snap['group_monitoring']['index_funds'])
        self.assertEqual(snap['summary']['total'], 0)
        self.assertEqual(snap['registered_counts']['index_funds'], 1)
        self.assertIn('指数基 (0)', page)          # 括号里是页面实际显示数，不是登记数
        for name in ('10Y美债','美元指数','收益率曲线'):
            self.assertNotIn(name, index)
        # 风险卡里不出现美元指数、收益率曲线；10Y美债只作为「参考」的参考行出现（2026-10-15 去掉「不计入综合灯」）
        for name in ('美元指数','收益率曲线'):
            self.assertNotIn(name, risk)
        self.assertEqual(risk.count('id="ust10Ref"'), 1)
        self.assertIn('<span class="unit">参考</span>', risk)
        self.assertNotIn('不计入综合灯', risk)
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
            self.assertEqual((weak['level'], weak['score'], weak['label']), ('green', 1, '平稳'))
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
                patch.object(monitor, 'cboe_vix_series', return_value=[]), \
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
        self.assertIn('<span class="grp-name">市场风险</span>', summary)
        self.assertNotIn('open', summary)
        self.assertNotIn('红', summary)
        self.assertNotIn('黄', summary)
        macro = {'hy_oas': {'ok': True, 'name': '垃圾债利差', 'value': 450, 'date': '2026-10-06'},
                 'nfci': {'ok': True, 'name': '金融压力', 'value': 0.12, 'date': '2026-10-02'},
                 'breadth': {'ok': True, 'name': '上涨参与度', 'value': 28.0,
                             'pct50': 18.0, 'date': '2026-10-06', 'pressure_confirmed': True}}
        page = monitor.render(macro, [], 0, snapshot={'target_trade_date': '2026-10-09'})
        summary = page.split('id="macroCard"', 1)[1].split('</summary>', 1)[0]
        self.assertIn('id="marketRiskLight" data-level="red"', summary)
        self.assertIn('综合：升高', summary)
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

    def test_run_light_partial_market_risk_does_not_claim_complete_success(self):
        snap = {'event': 'push', 'finished_at_bj': '2026-10-13T12:34:28+08:00',
                'summary': {'red': 11, 'yellow': 32, 'green': 56, 'total': 99,
                            'missing_symbols': [], 'stale_symbols': []},
                'market_risk': {'missing': ['VIX'], 'valid_count': 4}}
        phase, text = monitor._run_light(snap, '2026-10-13 12:34:28')
        self.assertEqual(phase, 'partial')
        self.assertIn('市场风险数据缺失或过期：VIX（有效指标 4/5）', text)
        self.assertIn('个股行情 99/99 已更新', text)
        self.assertNotIn('抓取成功', text)
        page = monitor.render({}, [], 0, snapshot=snap)
        self.assertIn('id="runLight" data-phase="partial"', page)
        self.assertIn('市场风险数据缺失或过期：VIX', page)
        complete = dict(snap, market_risk={'missing': [], 'valid_count': 5})
        self.assertEqual(monitor._run_light(complete, '2026-10-13 12:34:28')[0], 'ok')
        bad_quote = dict(snap, summary=dict(snap['summary'], missing_symbols=['NTNX']))
        self.assertEqual(monitor._run_light(bad_quote, '2026-10-13 12:34:28')[0], 'bad')

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

    def test_other_groups_show_consensus_but_never_alert(self):
        cons = {'s': 'ok', 'avg': 160.0, 'upd': '2026-10-07', 'r': '2026-10-07'}
        for avg in (160.0, 101.0, 500.0):          # 空间大、贴着现价、高得离谱：其他分组都不警示
            with self.subTest(avg=avg):
                level, base, hit, detail = self._tg({'target': 100}, group='technology', cons=dict(cons, avg=avg))
                self.assertEqual((level, hit), (base, []))
                self.assertEqual(detail['target']['kind'], 'consensus')      # 手填值对其他分组无效
                self.assertEqual(detail['target']['price'], avg)
                self.assertAlmostEqual(detail['target']['space'], (avg / 100 - 1) * 100)
                self.assertFalse(detail['target']['hit'])
                self.assertFalse(detail['target']['anomaly'])
                self.assertFalse(detail['purple'])
        level, base, hit, detail = self._tg({}, group='position', cons=cons)
        self.assertEqual(hit, ['共识价 160，空间 +60%'])                    # 持仓里同样的值照常警示
        level, base, hit, detail = self._tg({}, group='technology', cons={'s': 'fail', 'avg': 160.0, 'r': '2026-09-15'})
        self.assertEqual((level, hit), (base, []))
        self.assertTrue(detail['target']['failed'])                          # 取数失败照样标出来
        self.assertIsNone(self._tg({}, group='technology')[3]['target'])     # 没共识价：什么都不显示

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
        # 共识价胶囊不再写「共识」，开头是距现价的百分比
        self.assertEqual(tag({'kind': 'consensus', 'price': 429.47, 'month': 10, 'space': 60.0}),
                         '<span class="tgs"><span class="lvtag tgt"><span>+60%</span> <span>429.47</span> <span>· 10月</span></span></span>')
        self.assertNotIn('共识', tag({'kind': 'consensus', 'price': 429.47, 'month': 10, 'space': 60.0}))
        self.assertIn('lvtag tgt hit', tag({'kind': 'consensus', 'price': 87.56, 'month': 10, 'hit': True}))
        # 手填目标价同样不写「目标」，开头也是距现价的空间百分比（2026-10-15 改）
        self.assertEqual(tag({'kind': 'manual', 'price': 108.0, 'month': 10, 'space': 12.0}),
                         '<span class="tgs"><span class="lvtag tgt"><span>+12%</span> <span>108</span> <span>· 10月</span></span></span>')
        self.assertNotIn('目标', tag({'kind': 'manual', 'price': 108.0, 'month': 10, 'space': 12.0}))
        self.assertEqual(tag({'kind': 'manual', 'price': 500.0, 'month': 12, 'space': 8.0}),
                         '<span class="tgs"><span class="lvtag tgt"><span>+8%</span> <span>500</span> <span>· 12月</span></span></span>')
        self.assertNotIn('月', tag({'kind': 'manual', 'price': 108.0, 'month': None}))
        failed = tag({'kind': 'consensus', 'price': 429.0, 'month': 8, 'failed': True})
        self.assertIn('lvtag tgt bad', failed)
        self.assertIn('<span>429</span>', failed)
        self.assertNotIn('共识', failed)                     # 失败时也不写「共识」
        self.assertIn('<span>获取失败</span>', tag({'kind': 'consensus', 'price': None, 'month': None, 'failed': True}))
        self.assertEqual(tag({'kind': 'consensus', 'price': 87.56, 'month': None}),
                         '<span class="tgs"><span class="lvtag tgt"><span>87.56</span></span></span>')  # 无空间数据只显示价格
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

    def test_priority_and_due_rules(self):
        pr = monitor.consensus_priority
        today = '2026-10-10'
        ok = lambda r, **kw: {'s': 'ok', 'avg': 1.0, 'r': r, **kw}
        self.assertEqual(pr(None, today), 0)                                   # 新加入：最优先
        self.assertEqual(pr({'s': 'weird'}, today), 0)
        self.assertEqual(pr({'s': 'fail', 'try': '2026-10-09'}, today), 1)     # 失败过：次日重试
        self.assertIsNone(pr({'s': 'fail', 'try': today}, today))              # 同一天不重试
        self.assertIsNone(pr(ok('2026-09-27'), today))                         # 13 天：不取
        self.assertEqual(pr(ok('2026-09-26'), today), 3)                       # 满 14 天
        self.assertEqual(pr(ok('2026-10-11'), today), 3)                       # 记录时间在未来：重取
        self.assertEqual(pr(ok('bad'), today), 3)
        self.assertIsNone(pr({'s': 'none', 'reason': 'http404', 'r': '2026-09-12'}, today))         # 无覆盖：28 天不看
        self.assertEqual(pr({'s': 'none', 'r': '2026-09-10'}, today), 4)       # 满 30 天复查
        # 财报日过后提前重取（等 2 天让分析师调完）
        self.assertEqual(pr(ok('2026-10-05', ne='2026-10-08'), today), 2)
        self.assertIsNone(pr(ok('2026-10-05', ne='2026-10-09'), today))        # 才过 1 天
        self.assertEqual(pr(ok('2026-10-09', ne='2026-10-08'), today), 2)     # 财后1天抓取尚未覆盖2天等待窗口
        self.assertEqual(pr(ok('2026-10-08', ne='2026-10-08'), today), 2)      # 财报当天取的：之后再取一次
        self.assertIsNone(pr(ok('2026-10-05', ne='bad'), today))
        self.assertTrue(monitor.consensus_due(None, today))
        self.assertFalse(monitor.consensus_due(ok('2026-10-05'), today))

    def test_wanted_scope_covers_all_groups_but_not_etf_or_index(self):
        w = monitor.consensus_wanted
        self.assertTrue(w('AAPL', {}, 'position'))
        self.assertTrue(w('MSFT', {}, 'focus'))
        self.assertTrue(w('NVDA', {}, 'technology'))           # 其它分组也取
        self.assertTrue(w('NVDA', {}, 'other'))
        self.assertFalse(w('BRK-B', {}, 'position'))           # 伯克希尔不取
        self.assertFalse(w('BRK.B', {}, 'position'))
        self.assertFalse(w('XLK', {}, 'position'))             # ETF 不取
        self.assertFalse(w('XLK', {}, 'technology'))
        self.assertFalse(w('AAPL', {'target': 300}, 'position'))   # 持仓手填了就不取共识
        self.assertTrue(w('NVDA', {'target': 300}, 'technology'))  # 手填对其它分组无效，照取
        self.assertTrue(w('AAPL', {'target': 0}, 'position'))      # 手填无效当没填
        self.assertFalse(w('.VIX', {}, 'position'))
        self.assertFalse(w('00700.HK', {}, 'position'))

    def _update(self, symbols, pages, prev=None, today='2026-10-09', robots='User-agent: *\nDisallow: /e/\n',
                next_earn=None):
        sess = FakeSession(pages, robots=robots)
        sleeps = []
        with patch.object(monitor, 'log'):
            data, stat = monitor.update_consensus(symbols, prev, today, sess=sess, sleep=sleeps.append,
                                                  next_earn=next_earn)
        return data, stat, sess, sleeps

    def test_update_fetches_due_symbols_politely(self):
        pages = {'aapl': spg_page(328.09), 'zs': spg_page(233.38, '2026-09-12'), 'newx': FakeResp(404)}
        data, stat, sess, sleeps = self._update(['AAPL', 'ZS', 'NEWX'], pages)
        self.assertEqual(data['AAPL'], {'s': 'ok', 'avg': 328.09, 'upd': '2026-10-07', 'r': '2026-10-09', 'r_tz': 'America/New_York', 'try': '2026-10-09'})
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
        self.assertEqual(stat['due'], 0)                        # 没满 14 天不再取
        self.assertEqual(sess.calls, [])
        data, stat, sess, _ = self._update(['AAPL'], {'aapl': spg_page(310)}, prev={'data': data}, today='2026-10-15')
        self.assertEqual(data['AAPL']['avg'], 310.0)            # 10-01 取的，满 14 天：再取一次
        self.assertEqual(data['AAPL']['r'], '2026-10-15')

    def test_daily_cap_takes_new_then_failed_then_oldest(self):
        today = '2026-10-10'
        old = lambda r: {'s': 'ok', 'avg': 1.0, 'upd': '', 'r': r, 'try': r}
        prev = {'data': {'OLDA': old('2026-09-01'), 'OLDB': old('2026-09-20'), 'FRESH': old('2026-10-05'),
                         'FAILC': {'s': 'fail', 'r': '2026-09-01', 'try': '2026-09-30'}}}
        pages = {k.lower(): spg_page(50) for k in ('OLDA', 'OLDB', 'FRESH', 'FAILC', 'NEWD', 'NEWE')}
        syms = ['OLDA', 'OLDB', 'FAILC', 'FRESH', 'NEWD', 'NEWE']
        with patch.object(monitor, 'CONSENSUS_MAX_PER_DAY', 3):
            data, stat, sess, _ = self._update(syms, pages, prev=prev, today=today)
            fetched = [u.split('/')[4] for u in sess.calls if 'forecast' in u]
            self.assertEqual(fetched, ['newd', 'newe', 'failc'])                 # 新加入 > 上次失败 > 最久没更新
            self.assertEqual((stat['due'], stat['deferred'], stat['tried']), (5, 2, 3))
            self.assertEqual(data['OLDA']['r'], '2026-09-01')                    # 留到下一轮：旧值保留
            # 同一天再跑：额度已用完，不再取
            data2, stat2, sess2, _ = self._update(syms, pages, prev={'data': data}, today=today)
            self.assertEqual([u for u in sess2.calls if 'forecast' in u], [])
            self.assertEqual((stat2['due'], stat2['deferred']), (2, 2))
            # 第二天：轮到最久没更新的
            data3, stat3, sess3, _ = self._update(syms, pages, prev={'data': data2}, today='2026-10-11')
            self.assertEqual([u.split('/')[4] for u in sess3.calls if 'forecast' in u], ['olda', 'oldb'])
            self.assertEqual(stat3['deferred'], 0)

    def test_next_earnings_is_stored_and_pulls_refetch_forward(self):
        data, _, _, _ = self._update(['AAPL'], {'aapl': spg_page(300)}, today='2026-10-09',
                                     next_earn={'AAPL': '2026-10-12'})
        self.assertEqual(data['AAPL']['ne'], '2026-10-12')
        _, st, sess, _ = self._update(['AAPL'], {}, prev={'data': data}, today='2026-10-13')
        self.assertEqual(st['due'], 0)                                           # 财报才过 1 天，还不取
        d2, st, sess, _ = self._update(['AAPL'], {'aapl': spg_page(310)}, prev={'data': data}, today='2026-10-14')
        self.assertEqual(d2['AAPL']['avg'], 310.0)                               # 财报过后 2 天，提前重取
        self.assertNotIn('ne', d2['AAPL'])                                       # 没传新的财报日就不记
        # 过去的、写错的财报日不记
        for bad in ('2026-10-01', 'bad', None, 5):
            d3, _, _, _ = self._update(['AAPL'], {'aapl': spg_page(300)}, today='2026-10-09', next_earn={'AAPL': bad})
            self.assertNotIn('ne', d3['AAPL'])
        # 失败保留旧财报日
        d4, _, _, _ = self._update(['AAPL'], {'aapl': FakeResp(429)}, prev={'data': data}, today='2026-10-14')
        self.assertEqual(d4['AAPL']['ne'], '2026-10-12')

    def test_next_earnings_dates_from_status(self):
        got = monitor._next_earnings_dates({'data': {'aapl': {'status': 'ok', 'date': '2026-10-28'},
                                                    'MSFT': {'status': 'unknown'}, 'X': 'bad'}})
        self.assertEqual(got, {'AAPL': '2026-10-28'})
        for bad in (None, [], {}, {'data': []}, 'x'):
            self.assertEqual(monitor._next_earnings_dates(bad), {})

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

    def test_parse_failures_keep_old_values_without_batch_none_heuristic(self):
        syms = ['AA', 'BB', 'CC', 'DD', 'EE', 'FF']
        old = {s: {'s': 'ok', 'avg': 10.0, 'upd': '2026-09-12', 'r': '2026-09-15'} for s in syms}
        data, stat, _, _ = self._update(syms, {s.lower(): 'no data here' for s in syms}, prev={'data': old})
        self.assertEqual(stat['stopped'], '')
        self.assertEqual(stat['fail'], 6)
        self.assertEqual(stat['none'], 0)
        self.assertTrue(all(data[s]['s'] == 'fail' and data[s]['avg'] == 10.0 for s in syms))

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
        self.assertIn('<tr class="green pur" id="s-aaa"><td class="sym">AAA', page)
        self.assertIn('<tr class="red pur" id="s-bbb"><td class="sym">BBB', page)
        self.assertIn('<tr class="yellow pur" id="s-ccc"><td class="sym">CCC', page)
        self.assertIn('<td class="sym">DDD', page)
        shown, folded = page.split('无异动 1 只', 1)
        self.assertIn('>AAA', shown)                      # 紫色的绿灯行不折叠
        self.assertNotIn('>DDD', shown)                   # 普通绿灯行仍然折叠
        self.assertIn('>DDD', folded)
        self.assertIn('lvtag tgt hit', page)
        self.assertIn('tr.pur td.sig', page)
        self.assertIn('--purple:', page)
        self.assertIn('共识价来源：stockanalysis.com（S&P Global），每只约 14 天更新一次，财报后提前更新', page)

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
        # 手填目标价胶囊同样不写「目标」，开头是距现价的空间百分比（2026-10-15 改）
        self.assertIn('<span>+8%</span> <span>108</span> <span>· 10月</span>', page)
        self.assertNotIn('<span>目标</span>', page)
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
        self.assertIn('stat-badge purple', with_pur.split('</style>')[1])
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
        with tempfile.TemporaryDirectory() as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with open(os.path.join(directory, 'status.json'), 'w', encoding='utf-8') as f:
                json.dump({'consensus': {'data': {'AAPL': cache['AAPL']}}}, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                    patch.object(monitor, 'fetch_history', return_value={'price': 100}), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'update_consensus', return_value=({'AAPL': cache['AAPL']}, {'ok': 1})) as upd, \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan'):
                self.assertEqual(monitor.main([]), 0)
                # 所有分组里的美股个股都交给抓取；只有持仓/重点关注里手填了目标价的 MSFT 不取
                self.assertEqual(upd.call_args.args[0], ['AAPL', 'ZS', 'NVDA'])
                self.assertEqual(upd.call_args.kwargs['next_earn'], {})   # 旧快照没有财报缓存
                self.assertEqual(upd.call_args.args[1], {'data': {'AAPL': cache['AAPL']}})   # 上次缓存从 status.json 读回
                self.assertEqual(seen['AAPL'], cache['AAPL'])
                self.assertIsNone(seen['NVDA'])                           # 缓存里没有 NVDA：不传
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
        with tempfile.TemporaryDirectory() as directory:
            for name, content in [('holdings.json', cfg), ('settings.json', {})]:
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(content, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                    patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                    patch.object(monitor, 'fetch_history', return_value={'price': 100}), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'update_consensus', side_effect=RuntimeError('boom')), \
                    patch.object(monitor.time, 'sleep'), patch.object(monitor, 'push_serverchan'):
                self.assertEqual(monitor.main([]), 0)
                self.assertTrue(os.path.exists(os.path.join(directory, 'index.html')))


class TestCacheAudit(unittest.TestCase):
    """最终审核反例：多轮状态回放、业务失败、排队、改期、配额与财后事件。"""

    def _cons(self, symbols, pages=None, prev=None, today='2026-10-10', **kwargs):
        sess = FakeSession(pages or {})
        with patch.object(monitor, 'log'):
            data, stat = monitor.update_consensus(symbols, prev, today, sess=sess,
                                                  sleep=lambda _: None, **kwargs)
        return {'data': data, 'quota': stat['quota'], 'stat': stat}, sess

    @staticmethod
    def _old(r='2026-10-05', **extra):
        return {'s': 'ok', 'avg': 100.0, 'r': r, 'r_tz': 'America/New_York', 'try': r, **extra}

    @staticmethod
    def _earn(date='2026-10-12', fetched='2026-10-06'):
        return {'status': 'ok', 'date': date, 'kind': 'expected', 'timing': 'post', 'fetched': fetched}

    @staticmethod
    def _row(symbol):
        return {'symbol': symbol, 'group': 'technology', 'level': 'green', 'price': 100}

    def test_late_earnings_date_syncs_without_price_fetch(self):
        prev = {'data': {'AAA': self._old()}}
        synced, sess = self._cons(['AAA'], prev=prev, today='2026-10-06', next_earn={'AAA': '2026-10-08'})
        self.assertEqual(sess.calls, [])
        self.assertEqual(synced['data']['AAA']['ne'], '2026-10-08')
        self.assertNotIn('ne', prev['data']['AAA'])  # 不原地污染传入快照
        final, _ = self._cons(['AAA'], {'aaa': spg_page(110)}, synced, next_earn={'AAA': '2026-10-08'})
        self.assertEqual(final['stat']['tried'], 1)
        self.assertEqual(final['data']['AAA']['avg'], 110)
        self.assertNotIn('earn_pending', final['data']['AAA'])

    def test_upcoming_earnings_reschedule_replaces_old_date(self):
        prev = {'data': {'AAA': self._old(ne='2026-10-12')}}
        state, _ = self._cons(['AAA'], prev=prev, next_earn={'AAA': '2026-10-20'})
        state, _ = self._cons(['AAA'], prev=state, today='2026-10-14')
        self.assertEqual(state['stat']['tried'], 0)
        self.assertEqual(state['data']['AAA']['ne'], '2026-10-20')

    def test_next_quarter_date_does_not_erase_pending_event(self):
        prev = {'data': {'AAA': self._old(ne='2026-10-08')}}
        state, _ = self._cons(['AAA'], prev=prev, today='2026-10-09', next_earn={'AAA': '2027-01-15'})
        self.assertEqual(state['data']['AAA']['earn_pending'], ['2026-10-08'])
        state, _ = self._cons(['AAA'], {'aaa': spg_page(120)}, state)
        self.assertEqual(state['stat']['tried'], 1)
        self.assertEqual(state['data']['AAA']['ne'], '2027-01-15')
        state, _ = self._cons(['AAA'], prev=state, today='2026-10-11', earnings_events={'AAA': ['2026-10-08']})
        self.assertEqual(state['stat']['tried'], 0)

    def test_refresh_before_lag_does_not_satisfy_post_earnings_refresh(self):
        prev = {'data': {'AAA': self._old(r='2026-09-25', ne='2026-10-08')}}
        state, _ = self._cons(['AAA'], {'aaa': spg_page(110)}, prev, today='2026-10-09')
        self.assertEqual(state['stat']['tried'], 1)  # 常规到期恰落在财后第一天
        state, _ = self._cons(['AAA'], {'aaa': spg_page(120)}, state)
        self.assertEqual(state['stat']['tried'], 1)  # 财后第二天仍要补取

    def test_quota_zero_still_saves_new_event(self):
        prev = {'data': {'AAA': self._old()}, 'quota': {'date': '2026-10-10', 'symbols': ['USED'], 'retry_symbols': []}}
        with patch.object(monitor, 'CONSENSUS_MAX_PER_DAY', 1):
            state, sess = self._cons(['AAA'], prev=prev, next_earn={'AAA': '2026-10-12'})
        self.assertEqual(sess.calls, [])
        self.assertEqual(state['data']['AAA']['ne'], '2026-10-12')

    def test_404_batch_stays_none_and_leaves_next_day_for_normal_symbols(self):
        syms = ['X' + chr(65 + i) for i in range(12)]
        state, _ = self._cons(syms)
        self.assertEqual(state['stat']['none'], 12)
        self.assertTrue(all(e['s'] == 'none' for e in state['data'].values()))
        state['data']['WAIT'] = self._old('2026-09-01')
        state, _ = self._cons(syms + ['WAIT'], {'wait': spg_page(110)}, state, today='2026-10-11')
        self.assertEqual(state['stat']['tried'], 1)
        self.assertEqual(state['data']['WAIT']['r'], '2026-10-11')

    def test_parse_failure_is_not_negative_coverage_cache(self):
        state, _ = self._cons(['AAA'], {'aaa': '<html>challenge or changed layout</html>'},
                              {'data': {'AAA': self._old('2026-09-01')}})
        e = state['data']['AAA']
        self.assertEqual((e['s'], e['avg']), ('fail', 100))
        self.assertEqual(e['next_retry'], '2026-10-11')
        state, _ = self._cons(['AAA'], {'aaa': spg_page(0).replace('numPriceTargets:20', 'numPriceTargets:0')})
        self.assertEqual(state['data']['AAA']['s'], 'none')

    def test_retry_backoff_and_reserved_normal_capacity(self):
        fails = ['X' + chr(65 + i) for i in range(12)]
        old = {s: {'s': 'fail', 'r': '2026-09-01', 'try': '2026-10-09'} for s in fails}
        old['WAIT'] = self._old('2026-09-01')
        pages = {s.lower(): 'invalid body' for s in fails}
        pages['wait'] = spg_page(110)
        state, _ = self._cons(fails + ['WAIT'], pages, {'data': old})
        self.assertEqual(state['stat']['fail'], 3)
        self.assertEqual(state['data']['WAIT']['r'], '2026-10-10')
        first = state['data']['XA']
        failed = monitor._consensus_failed(first, '2026-10-11', 'again')
        self.assertEqual(failed['next_retry'], '2026-10-13')
        self.assertIsNone(monitor.consensus_priority(failed, '2026-10-12'))

    def test_daily_quota_survives_remove_clear_readd_and_force(self):
        first = ['X' + chr(65 + i) for i in range(12)]
        second = ['Y' + chr(65 + i) for i in range(12)]
        state, _ = self._cons(first, {s.lower(): spg_page(100) for s in first})
        state, _ = self._cons([], prev=state)
        self.assertEqual(state['data'], {})
        self.assertEqual(len(state['quota']['symbols']), 12)
        state, sess = self._cons(second, prev=state, force=True)
        self.assertEqual(sess.calls, [])
        self.assertEqual(state['stat']['tried'], 0)
        state, _ = self._cons(second, prev=state, today='2026-10-11')
        self.assertEqual(state['stat']['tried'], 12)

    def test_legacy_quota_counts_removed_symbols_before_pruning(self):
        old = {'data': {'OLD': self._old('2026-10-10')}}
        with patch.object(monitor, 'CONSENSUS_MAX_PER_DAY', 1):
            state, sess = self._cons(['NEW'], prev=old)
        self.assertEqual(sess.calls, [])
        self.assertIn('OLD', state['quota']['symbols'])
        self.assertNotIn('OLD', state['data'])

    def test_event_day_and_quota_day_are_independent(self):
        prev = {'data': {'AAA': self._old(ne='2026-10-08')}}
        state, _ = self._cons(['AAA'], prev=prev, today='2026-10-09', quota_day='2026-10-10')
        self.assertEqual(state['stat']['tried'], 0)  # 北京已10日，美东才9日
        state, _ = self._cons(['AAA'], {'aaa': spg_page(110)}, state,
                              today='2026-10-10', quota_day='2026-10-11')
        self.assertEqual(state['data']['AAA']['r'], '2026-10-10')
        self.assertEqual(state['data']['AAA']['try'], '2026-10-11')

    def test_us_calendar_clock_summer_winter_and_weekend(self):
        from datetime import datetime
        for bj, us in [('2026-10-10T08:15:00+08:00', '2026-10-09'),
                       ('2026-12-10T09:15:00+08:00', '2026-12-09'),
                       ('2026-11-01T18:00:00+08:00', '2026-11-01')]:
            with self.subTest(bj=bj), patch.object(monitor, 'NOW', datetime.fromisoformat(bj)), \
                    patch.object(monitor, 'TARGET_DATE', '2026-10-01'):
                self.assertEqual(monitor._earn_today(), us)

    def _nasdaq(self, body, http=200):
        response = MagicMock(status_code=http)
        response.json.return_value = body
        session = MagicMock()
        session.get.return_value = response
        with patch.object(monitor, '_EARN_CACHE', {}), patch.object(monitor, '_fund_session', return_value=session), \
                patch.object(monitor, '_earn_today', return_value='2026-10-10'):
            return monitor.nasdaq_earnings('AAA')

    def test_nasdaq_business_error_null_and_missing_fields_are_retryable(self):
        for body in [None, [], {}, {'data': None, 'status': {'rCode': 500}},
                     {'data': None, 'status': {'rCode': 200}},
                     {'data': {'reportText': 'expected to report earnings on 10/20/2026'}},
                     {'data': {'reportText': ''}, 'status': {'rCode': 200}},
                     {'data': {}, 'status': {'rCode': 200}},
                     {'data': {'reportText': 'pending'}, 'status': {'rCode': 200, 'bCodeMessage': ['error']}}]:
            with self.subTest(body=body):
                self.assertEqual(self._nasdaq(body)['status'], 'error')
        self.assertEqual(self._nasdaq({}, 429)['status'], 'error')
        self.assertEqual(self._nasdaq({}, 404)['status'], 'na')
        good = {'data': {'reportText': 'expected to report earnings on 10/20/2026 after market close'},
                'status': {'rCode': 200, 'bCodeMessage': None, 'developerMessage': None}}
        self.assertEqual(self._nasdaq(good)['date'], '2026-10-20')

    def test_business_error_preserves_old_date_and_retries(self):
        bad = self._nasdaq({'data': None, 'status': {'rCode': 500}})
        items = [self._row('AAA')]
        old = {'AAA': self._earn()}
        with patch.object(monitor, 'nasdaq_earnings', return_value=bad), patch.object(monitor, 'log'):
            state = monitor.attach_earnings(items, old, '2026-10-10')
        self.assertEqual(state['data']['AAA'], old['AAA'])
        self.assertEqual(items[0]['earnings']['date'], '2026-10-12')
        self.assertTrue(monitor.earnings_refetch_due(state['data']['AAA'], '2026-10-11'))

    def test_deferred_keeps_valid_old_cache_and_fetch_timestamp(self):
        items = [self._row(s) for s in ('AAA', 'BBB', 'CCC')]
        old = {s: self._earn() for s in ('AAA', 'BBB', 'CCC')}
        with patch.object(monitor, 'EARN_MAX_FETCH_PER_RUN', 2), patch.object(monitor, 'log'), \
                patch.object(monitor, 'nasdaq_earnings', return_value=self._earn()):
            state = monitor.attach_earnings(items, old, '2026-10-10')
        self.assertEqual(state['deferred'], 1)
        self.assertEqual(state['data']['CCC'], old['CCC'])
        self.assertEqual(items[2]['earnings']['date'], '2026-10-12')
        with patch.object(monitor, 'nasdaq_earnings', return_value=self._earn()) as fetch, patch.object(monitor, 'log'):
            monitor.attach_earnings([self._row(s) for s in old], state['data'], '2026-10-11')
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ['CCC'])

    def test_earnings_history_survives_unknown_and_next_quarter(self):
        prev = {'AAA': self._earn('2026-10-08', '2026-10-01')}
        with patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}), patch.object(monitor, 'log'):
            st = monitor.attach_earnings([self._row('AAA')], prev, '2026-10-09')
            self.assertEqual(st['events']['AAA'], ['2026-10-08'])
            st2 = monitor.attach_earnings([self._row('AAA')], st['data'], '2026-10-10', st['events'])
        self.assertEqual(st2['events']['AAA'], ['2026-10-08'])
        with patch.object(monitor, 'nasdaq_earnings', return_value=self._earn('2027-01-10')), patch.object(monitor, 'log'):
            st3 = monitor.attach_earnings([self._row('AAA')], st2['data'], '2026-10-17', st2['events'])
        self.assertEqual(st3['events']['AAA'], ['2026-10-08'])
        self.assertEqual(st3['data']['AAA']['date'], '2027-01-10')

    def test_deferred_past_date_is_not_shown_as_next(self):
        rows = [self._row('AAA')]
        with patch.object(monitor, 'EARN_MAX_FETCH_PER_RUN', 0), patch.object(monitor, 'log'):
            st = monitor.attach_earnings(rows, {'AAA': self._earn('2026-10-08')}, '2026-10-10')
        self.assertEqual(rows[0]['earnings']['status'], 'unknown')
        self.assertEqual(st['events']['AAA'], ['2026-10-08'])
        self.assertIn('AAA', st['data'])

    @staticmethod
    def _financials(period='6/30/2026'):
        return {'period': period, 'base_period': '9/30/2025',
                'inc': {'Total Revenue': {'value2': '$100', 'value5': '$120'}}, 'bs': {}, 'cf': {}, 'rt': {}}

    def test_fundamental_event_overrides_quiet_period_until_period_advances(self):
        prev = {'AAA': {'fetched': '2026-10-01', 'data': self._financials()}}
        events = {'AAA': ['2026-10-09']}
        with patch.object(monitor, 'nasdaq_financials', return_value=self._financials()) as fetch:
            _, st = monitor.update_fundamentals(['AAA'], prev, '2026-10-10', events)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(st['data']['AAA']['event_date'], '2026-10-09')
            _, same = monitor.update_fundamentals(['AAA'], st['data'], '2026-10-10', events)
            self.assertEqual(same['fetched'], 0)
            _, next_day = monitor.update_fundamentals(['AAA'], st['data'], '2026-10-11', events)
            self.assertEqual(next_day['fetched'], 1)
        with patch.object(monitor, 'nasdaq_financials', return_value=self._financials('9/30/2026')):
            _, advanced = monitor.update_fundamentals(['AAA'], next_day['data'], '2026-10-12', events)
            self.assertNotIn('event_date', advanced['data']['AAA'])
            self.assertEqual(advanced['data']['AAA']['event_done'], '2026-10-09')
            _, final = monitor.update_fundamentals(['AAA'], advanced['data'], '2026-10-13', events)
            self.assertEqual(final['fetched'], 0)

    def test_fundamental_failure_keeps_event_and_old_timestamp(self):
        prev = {'AAA': {'fetched': '2026-10-01', 'data': self._financials()}}
        # 备用源也关掉：这个用例要的是「Nasdaq 取不到 → 保留旧值和事件」
        with patch.object(monitor, 'nasdaq_financials', return_value=None), \
                patch.object(monitor, 'stockanalysis_financials', return_value=None):
            facts, st = monitor.update_fundamentals(['AAA'], prev, '2026-10-10', {'AAA': ['2026-10-09']})
        self.assertEqual(facts['AAA']['period'], '6/30/2026')
        entry = st['data']['AAA']
        self.assertTrue(entry['fetch_error'])
        self.assertEqual(entry['fetched'], '2026-10-01')
        self.assertTrue(monitor.fundamentals_refetch_due(entry, '2026-10-11'))

    def test_fundamental_one_bad_symbol_does_not_drop_other_results(self):
        def fetch(sym):
            return {'inc': 'bad'} if sym == 'BAD' else self._financials()
        with patch.object(monitor, 'nasdaq_financials', side_effect=fetch), patch.object(monitor, 'log'):
            facts, st = monitor.update_fundamentals(['BAD', 'GOOD'], today='2026-10-10')
        self.assertIn('GOOD', facts)
        self.assertEqual(st['failed'], 1)

    def test_fundamental_report_regression_keeps_old_data(self):
        prev = {'AAA': {'fetched': '2026-09-01', 'data': self._financials()}}
        with patch.object(monitor, 'nasdaq_financials', return_value=self._financials('3/31/2026')):
            facts, st = monitor.update_fundamentals(['AAA'], prev, '2026-10-10')
        self.assertEqual(facts['AAA']['period'], '6/30/2026')
        self.assertTrue(st['data']['AAA']['fetch_error'])

    def test_fund_cache_warning_visible_even_without_red_yellow(self):
        row = self._row('AAA')
        row['fund_cache_note'] = '读取失败，沿用缓存 <旧>'
        # 2026-10-15：基本面状态挪到财报列显示，名称列只留名称/备注/加减仓标签
        markup = monitor._earnings_html(row)
        self.assertIn('读取失败，沿用缓存', markup)
        self.assertIn('&lt;旧&gt;', markup)
        self.assertIn('基本面：', markup)
        self.assertNotIn('基本面', monitor._row_tags(row, {}))

    def test_first_post_event_fetch_establishes_baseline_not_completion(self):
        events = {'AAA': ['2026-10-09']}
        with patch.object(monitor, 'nasdaq_financials', return_value=self._financials()):
            _, first = monitor.update_fundamentals(['AAA'], today='2026-10-10', earnings_events=events)
        self.assertEqual(first['data']['AAA']['event_base'], '2026-06-30')
        self.assertNotIn('event_done', first['data']['AAA'])
        with patch.object(monitor, 'nasdaq_financials', return_value=self._financials('9/30/2026')) as fetch:
            _, second = monitor.update_fundamentals(['AAA'], first['data'], '2026-10-11', events)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second['data']['AAA']['event_done'], '2026-10-09')

    def test_two_unfinished_quarter_events_are_completed_in_order(self):
        old = {'AAA': {'fetched': '2026-07-02', 'data': self._financials('3/31/2026'),
                       'event_date': '2026-07-01', 'event_base': '2026-03-31', 'event_checked': '2026-07-02'}}
        events = {'AAA': ['2026-07-01', '2026-10-09']}
        with patch.object(monitor, 'nasdaq_financials', return_value=self._financials()):
            _, first = monitor.update_fundamentals(['AAA'], old, '2026-10-10', events)
            self.assertEqual(first['data']['AAA']['event_done'], '2026-07-01')
            _, second = monitor.update_fundamentals(['AAA'], first['data'], '2026-10-11', events)
        self.assertEqual(second['data']['AAA']['event_date'], '2026-10-09')
        self.assertEqual(second['data']['AAA']['event_base'], '2026-06-30')
        self.assertEqual(second['data']['AAA']['event_done'], '2026-07-01')

    def test_legacy_untrusted_negative_caches_get_one_time_recheck(self):
        self.assertTrue(monitor.earnings_refetch_due({'status': 'na', 'fetched': '2026-10-09'}, '2026-10-10'))
        old = {'data': {'AAA': {'s': 'none', 'r': '2026-10-09'}}}
        state, _ = self._cons(['AAA'], {'aaa': spg_page(110)}, old)
        self.assertEqual(state['stat']['tried'], 1)
        self.assertEqual(state['data']['AAA']['s'], 'ok')

    def test_legacy_beijing_fetch_day_cannot_falsely_complete_us_event(self):
        old = {'data': {'AAA': {'s': 'ok', 'avg': 100, 'r': '2026-10-10', 'try': '2026-10-10', 'ne': '2026-10-08'}}}
        state, _ = self._cons(['AAA'], {'aaa': spg_page(110)}, old,
                              today='2026-10-10', quota_day='2026-10-11')
        self.assertEqual(state['stat']['tried'], 1)
        self.assertEqual(state['data']['AAA']['r_tz'], 'America/New_York')
        state, _ = self._cons(['AAA'], prev=state, today='2026-10-11', quota_day='2026-10-12',
                              earnings_events={'AAA': ['2026-10-08']})
        self.assertEqual(state['stat']['tried'], 0)

    def test_main_multirun_earnings_then_consensus_and_persisted_quota(self):
        import tempfile
        from datetime import datetime
        cfg = {'positions': {'AAA': {}}}
        def analyzed(symbol, settings, data, group):
            return 'green', [], dict(symbol=symbol, note='', price=100, chg=0, level='green', group=group,
                                     signals=[], data_date='2026-10-09')
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, 'holdings.json'), 'w', encoding='utf-8') as f:
                json.dump(cfg, f)
            with open(os.path.join(directory, 'settings.json'), 'w', encoding='utf-8') as f:
                json.dump({}, f)
            with patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-09'), \
                    patch.object(monitor, 'build_reference_alerts', return_value={}), \
                    patch.object(monitor, 'build_macro', return_value={}), \
                    patch.object(monitor, 'collect_quotes', return_value={'AAA': {'price': 100}}), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
                    patch.object(monitor, 'global_dca', return_value=None), \
                    patch.object(monitor, 'update_fundamentals', return_value=({}, {'fetched': 0, 'cached': 0, 'data': {}})) as fund, \
                    patch.object(monitor, 'fundamental_check', return_value=None), \
                    patch.object(monitor, 'consensus_robots_ok', return_value=True), \
                    patch.object(monitor, 'fetch_consensus_one', return_value={'s': 'ok', 'avg': 110, 'upd': '2026-10-05'}) as cons, \
                    patch.object(monitor, 'push_serverchan'), patch.object(monitor, 'log'):
                def run(day, earnings):
                    with patch.object(monitor, 'NOW', datetime.fromisoformat(day)), \
                            patch.object(monitor, 'nasdaq_earnings', return_value=earnings):
                        self.assertEqual(monitor.main([]), 0)
                    with open(os.path.join(directory, 'status.json'), encoding='utf-8') as f:
                        return json.load(f)
                first = run('2026-10-05T20:15:00-04:00', self._earn('2026-10-08'))
                self.assertEqual(first['consensus']['data']['AAA']['ne'], '2026-10-08')
                self.assertEqual(first['consensus']['quota']['date'], '2026-10-06')
                second = run('2026-10-09T20:15:00-04:00', {'status': 'unknown'})
                self.assertEqual(second['earnings']['events']['AAA'], ['2026-10-08'])
                self.assertEqual(cons.call_count, 1)  # 财后第一天，不提前
                third = run('2026-10-10T20:15:00-04:00', {'status': 'unknown'})
                self.assertEqual(cons.call_count, 2)
                self.assertEqual(third['consensus']['data']['AAA']['r'], '2026-10-10')
                self.assertEqual(fund.call_args.kwargs['earnings_events']['AAA'], ['2026-10-08'])
                # 关闭检查不许再触及批量抓取入口，已有缓存必须保留。
                third['fundamental']['data'] = {'AAA': {'fetched': '2026-10-01', 'data': self._financials()}}
                with open(os.path.join(directory, 'status.json'), 'w', encoding='utf-8') as f:
                    json.dump(third, f)
                count = fund.call_count
                with patch.dict(monitor.S, {'fund_enable': 0}):
                    disabled = run('2026-10-11T20:15:00-04:00', {'status': 'unknown'})
                self.assertEqual(fund.call_count, count)
                self.assertEqual(disabled['fundamental']['data'], third['fundamental']['data'])


class TestCompletedDayVolume(unittest.TestCase):
    """量比只比较同源的已完整日线，不把盘中/补报价的零成交量冒充完整日。"""

    def history(self, end):
        import exchange_calendars as xcals
        cal = xcals.get_calendar('XNYS')
        sessions = cal.sessions_in_range('2026-09-01', end)[-23:]
        dates = [d.date().isoformat() for d in sessions]
        volumes = [1000] * 23
        volumes[-2], volumes[-1] = 2000, 4000
        return {'dates': dates, 'volumes': volumes, 'closes': [100] * 23,
                'highs': [100] * 23, 'lows': [100] * 23,
                'price': 100, 'prev_close': 100}

    def measure(self, data, time, target):
        from datetime import datetime
        with patch.object(monitor, 'NOW', datetime.fromisoformat(time)), \
                patch.object(monitor, 'TARGET_DATE', target):
            return monitor.completed_day_volume_ratio('AAPL', data)

    def test_summer_intraday_and_after_close_only_use_complete_day(self):
        from datetime import datetime
        data = self.history('2026-10-13')
        previous, latest = data['dates'][-2:]
        for time in ('2026-10-13T09:31:00-04:00', '2026-10-13T12:00:00-04:00',
                     '2026-10-13T16:10:00-04:00'):
            with self.subTest(time=time):
                self.assertEqual(self.measure(data, time, latest), (2.0, previous))
        ratio, day = self.measure(data, '2026-10-13T16:16:00-04:00', latest)
        self.assertEqual(day, latest)
        self.assertAlmostEqual(ratio, 4000 / 1050)
        self.assertEqual(self.measure(data, '2026-10-13T20:15:00-04:00', latest)[1], latest)
        # CLOSED_ONLY 是整页口径，不可令盘中尚未完结的量混入量比。
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-13T12:00:00-04:00')), \
                patch.object(monitor, 'TARGET_DATE', latest), patch.object(monitor, 'CLOSED_ONLY', True):
            self.assertEqual(monitor._analysis_context('AAPL', {}, data, 'technology').vol_ratio_date, previous)

    def test_winter_half_day_holiday_and_incomplete_source(self):
        data = self.history('2026-11-27')  # 感恩节次日13:00美东提前收盘
        previous, latest = data['dates'][-2:]
        self.assertEqual(self.measure(data, '2026-11-27T13:14:00-05:00', latest), (2.0, previous))
        self.assertEqual(self.measure(data, '2026-11-27T13:16:00-05:00', latest)[1], latest)
        self.assertEqual(self.measure(data, '2026-11-26T20:15:00-05:00', latest)[1], previous)
        self.assertEqual(self.measure(data, '2026-11-27T20:15:00-05:00', previous)[1], previous)
        for bad in (dict(data, volumes=data['volumes'][:-1] + [0]),
                    dict(data, volumes=data['volumes'][:-2] + [0, 4000]),
                    dict(data, volumes=data['volumes'][:-2] + [None, 4000]),
                    dict(data, dates=data['dates'][:-1], volumes=data['volumes'][:-1])):
            with self.subTest(bad=bad):
                self.assertEqual(self.measure(bad, '2026-11-27T20:15:00-05:00', latest), (None, ''))
        self.assertEqual(monitor.completed_day_volume_ratio('ESMAIN', data), (None, ''))
        self.assertEqual(monitor.completed_day_volume_ratio('^VIX', data), (None, ''))

    def test_alert_uses_completed_day_and_date_without_inventing_current_volume(self):
        data = self.history('2026-10-13')
        latest = data['dates'][-1]
        from datetime import datetime
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-13T12:00:00-04:00')), \
                patch.object(monitor, 'TARGET_DATE', latest):
            level, signals, detail = monitor.analyze_symbol('AAPL', {}, data)
            self.assertEqual(detail['vol_ratio_date'], data['dates'][-2])
            self.assertAlmostEqual(detail['vol_ratio'], 2.0)
            self.assertTrue(any('最近完整日量比' in text and data['dates'][-2] in text
                                for text in signals), signals)
            self.assertIn(('vol_ratio', 'yellow'), detail['signal_marks'])
            with patch.object(monitor, 'S', {**monitor.S, 'vol_ratio_red': 2.0}):  # 达到红线报红，所有分组
                for group in ('position', 'technology'):
                    red_level, _, red = monitor.analyze_symbol('AAPL', {}, data, group)
                    self.assertIn(('vol_ratio', 'red'), red['signal_marks'])
                    self.assertEqual(red_level, 'red')
            with patch.object(monitor, 'S', {**monitor.S, 'vol_ratio': 2.5, 'vol_ratio_red': 3.0}):
                _, quiet_signals, below = monitor.analyze_symbol('AAPL', {}, data)
                self.assertFalse(any(k == 'vol_ratio' for k, _ in below['signal_marks']))
            self.assertEqual(monitor.DEFAULT_SETTINGS['vol_ratio_red'], 3.0)
            self.assertEqual(monitor.valid_settings({**monitor.DEFAULT_SETTINGS, 'vol_ratio': 3.5}), 'vol_ratio / vol_ratio_red')
            self.assertFalse(hasattr(monitor, '_quiet_result'))
            self.assertNotIn('quiet_chg', monitor.DEFAULT_SETTINGS)
            data['volumes'][-2] = 0
            _, signals, detail = monitor.analyze_symbol('AAPL', {}, data)
            self.assertIsNone(detail['vol_ratio'])
            self.assertFalse(any('量比' in text for text in signals))


class TestIntradayVolumeRatio(unittest.TestCase):
    """盘中量比：当日累计量按已开盘分钟数折算；静默期/非交易时段/缺量不报，收盘后回落完整日口径。"""

    def history(self, end):
        import exchange_calendars as xcals
        cal = xcals.get_calendar('XNYS')
        sessions = cal.sessions_in_range('2026-09-01', end)[-23:]
        dates = [d.date().isoformat() for d in sessions]
        volumes = [1000] * 23
        volumes[-1] = 0          # 今天这根未定稿，不能进均量样本
        return {'dates': dates, 'volumes': volumes, 'closes': [100] * 23,
                'highs': [100] * 23, 'lows': [100] * 23,
                'price': 100, 'prev_close': 100}

    def measure(self, data, time, realtime_volume=None):
        from datetime import datetime
        if realtime_volume is not None:
            data['rt_volume'] = realtime_volume
        with patch.object(monitor, 'NOW', datetime.fromisoformat(time)):
            return monitor.intraday_volume_ratio('AAPL', data)

    def test_midday_uses_elapsed_minutes(self):
        data = self.history('2026-10-13')
        # 20 日均量 1000/全日 390 分钟 = 2.5641/分钟；12:00 已开盘 150 分钟，1500 股 → 10/分钟
        ratio, moment = self.measure(data, '2026-10-13T12:00:00-04:00', 1500)
        self.assertEqual(moment, '12:00')
        self.assertAlmostEqual(ratio, (1500 / 150) / (1000 / 390), places=4)

    def test_quiet_window_and_closed_hours_report_nothing(self):
        data = self.history('2026-10-13')
        for time in ('2026-10-13T09:31:00-04:00', '2026-10-13T09:44:00-04:00',
                     '2026-10-13T16:05:00-04:00', '2026-10-13T20:15:00-04:00',
                     '2026-10-11T12:00:00-04:00'):   # 周日
            with self.subTest(time=time):
                self.assertEqual(self.measure(data, time, 1500), (None, ''))

    def test_missing_or_invalid_realtime_volume(self):
        data = self.history('2026-10-13')
        self.assertEqual(self.measure(data, '2026-10-13T12:00:00-04:00'), (None, ''))
        for bad in (0, -5, None, '3000'):
            with self.subTest(bad=bad):
                self.assertEqual(self.measure(dict(data), '2026-10-13T12:00:00-04:00', bad), (None, ''))
        # 均量样本里出现 0/None 也不硬算
        broken = dict(data, volumes=data['volumes'][:-21] + [0] + data['volumes'][-20:])
        self.assertEqual(self.measure(broken, '2026-10-13T12:00:00-04:00', 1500), (None, ''))

    def test_futures_and_index_not_supported(self):
        data = self.history('2026-10-13')
        from datetime import datetime
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-13T12:00:00-04:00')):
            self.assertEqual(monitor.intraday_volume_ratio('ESMAIN', dict(data, rt_volume=1500)), (None, ''))
            self.assertEqual(monitor.intraday_volume_ratio('^VIX', dict(data, rt_volume=1500)), (None, ''))

    def test_half_day_uses_shortened_session(self):
        data = self.history('2026-11-27')     # 感恩节次日 13:00 提前收盘 → 全日 210 分钟
        ratio, _ = self.measure(data, '2026-11-27T11:00:00-05:00', 900)   # 已开盘 90 分钟
        self.assertAlmostEqual(ratio, (900 / 90) / (1000 / 210), places=4)

    def test_alert_prefers_intraday_over_completed_day(self):
        data = self.history('2026-10-13')
        data['volumes'][-2] = 2600            # 昨日放量 → 20 日均量 1080
        data['rt_volume'] = 1500              # 盘中折算 3.6×（也应报黄）
        average = (2600 + 19 * 1000) / 20
        from datetime import datetime
        # 两段都必须固定 NOW/TARGET_DATE：否则第二次调用会读真实时钟，
        # 收盘后跑测试时盘中量比根本不触发（2026-10-12 踩过同样的坑）。
        midday = datetime.fromisoformat('2026-10-13T12:00:00-04:00')
        with patch.object(monitor, 'NOW', midday), \
                patch.object(monitor, 'TARGET_DATE', data['dates'][-1]):
            _, signals, detail = monitor.analyze_symbol('AAPL', {}, data)
        self.assertTrue(detail['vol_ratio_intraday'])
        self.assertAlmostEqual(detail['rt_vol_ratio'], (1500 / 150) / (average / 390), places=4)
        self.assertTrue(any('盘中量比（截至 12:00 ET）' in text for text in signals), signals)
        self.assertFalse(any('最近完整日量比' in text for text in signals), signals)
        self.assertIn(('vol_ratio', 'red'), detail['signal_marks'])     # 3.6× ≥ 默认红线 3.0
        self.assertEqual(monitor._member_metric(detail, 'vol_ratio'), '盘中量比 3.6×（截至 12:00 ET）')
        with patch.object(monitor, 'NOW', midday), \
                patch.object(monitor, 'TARGET_DATE', data['dates'][-1]), \
                patch.object(monitor, 'S', {**monitor.S, 'vol_ratio_red': 4.0}):
            _, _, y_detail = monitor.analyze_symbol('AAPL', {}, data)
        self.assertIn(('vol_ratio', 'yellow'), y_detail['signal_marks'])  # 只到黄线时报黄

    def test_after_close_falls_back_to_completed_day(self):
        data = self.history('2026-10-13')
        data['volumes'][-2] = 2600
        data['volumes'][-1] = 2000            # 今日已收盘，日线给出完整量 → 完整日量比 1.9×
        data['rt_volume'] = 1500              # 已收盘：实时量不该再参与
        from datetime import datetime
        with patch.object(monitor, 'NOW', datetime.fromisoformat('2026-10-13T18:00:00-04:00')), \
                patch.object(monitor, 'TARGET_DATE', data['dates'][-1]):
            _, signals, detail = monitor.analyze_symbol('AAPL', {}, data)
        self.assertFalse(detail['vol_ratio_intraday'])
        self.assertAlmostEqual(detail['vol_ratio'], 2000 / ((2600 + 19 * 1000) / 20), places=4)
        self.assertTrue(any('最近完整日量比' in text for text in signals), signals)
        self.assertFalse(any('盘中量比' in text for text in signals), signals)


class TestVolumeRatioZscore(unittest.TestCase):
    """收盘后量比改按这只股自己的放量分布（z 值）判灯；z 算不出就退回固定倍数，盘中口径完全不动。"""

    CLOSE = '2026-10-13T20:15:00-04:00'

    def history(self, last=None, days=120, flat=False):
        import random
        import exchange_calendars as xcals
        cal = xcals.get_calendar('XNYS')
        sessions = cal.sessions_in_range('2026-04-01', '2026-10-13')[-days:]
        rng = random.Random(11)
        volumes = ([1000.0] * days if flat
                   else [1000 * (1 + rng.uniform(-0.2, 0.2)) for _ in range(days)])
        if last is not None:
            volumes[-1] = last
        return {'dates': [d.date().isoformat() for d in sessions], 'volumes': volumes,
                'closes': [100.0] * days, 'highs': [100.0] * days, 'lows': [100.0] * days,
                'price': 100.0, 'prev_close': 100.0}

    def measure(self, data):
        from datetime import datetime
        with patch.object(monitor, 'NOW', datetime.fromisoformat(self.CLOSE)), \
                patch.object(monitor, 'TARGET_DATE', None):
            return (monitor.completed_day_volume_ratio('AAPL', data),
                    monitor.completed_day_volume_z('AAPL', data))

    def context(self, ratio=1.3, z=1.6, intraday=False, rt=0.0):
        from types import SimpleNamespace
        return SimpleNamespace(vol_ratio=ratio, vol_ratio_z=z, vol_ratio_date='2026-10-13',
                               vol_ratio_intraday=intraday, rt_vol_ratio=rt, rt_vol_time='12:00')

    @staticmethod
    def run_rule(x, **overrides):
        al = monitor._Alerts()
        with patch.object(monitor, 'S', {**monitor.S, **overrides}):
            monitor._rule_vol_ratio(al, x)
        return al

    def base_mean(self, data):
        return sum(data['volumes'][-21:-1]) / 20

    def test_z_grows_with_volume_and_flat_history_has_no_scale(self):
        normal_z = self.measure(self.history())[1]
        big = self.history(last=self.base_mean(self.history()) * 3)
        (ratio, _), big_z = self.measure(big)
        self.assertIsInstance(normal_z, float)
        self.assertLess(abs(normal_z), 1.5)          # 平时的量不该被判成异常
        self.assertGreater(big_z, normal_z)
        self.assertGreater(big_z, 2.0)               # 3 倍量在这条波动序列上远超 2σ
        self.assertAlmostEqual(ratio, 3.0, places=6)
        # 量恒定 → 分布尺度为 0，无法定门槛，只能退回固定倍数
        flat = self.history(flat=True, last=1300.0)
        self.assertIsNone(self.measure(flat)[1])
        self.assertAlmostEqual(self.measure(flat)[0][0], 1.3, places=6)
        # 历史不足 80 根日线也算不出
        self.assertIsNone(self.measure(self.history(days=40))[1])

    def test_rule_uses_z_when_available(self):
        # 默认黄 1.8σ / 红 3.6σ（按真实日线标定，命中率与旧的 1.5× / 3× 相当）
        for z, level in ((0.5, None), (1.7, None), (1.8, 'yellow'), (2.5, 'yellow'),
                         (3.6, 'red'), (5.0, 'red')):
            with self.subTest(z=z):
                al = self.run_rule(self.context(z=z))
                if level is None:
                    self.assertEqual(al.marks, [])
                else:
                    self.assertEqual(al.marks, [('vol_ratio', level)])
                    # σ 只是内部判定尺度，页面文案里不出现
                    self.assertEqual(al.signals[0], '最近完整日量比（2026-10-13）1.3x')
        # 阈值本身可调
        self.assertEqual(self.run_rule(self.context(z=1.6), vol_ratio_z=2.0, vol_ratio_red_z=3.0).marks, [])
        self.assertEqual(self.run_rule(self.context(z=2.5), vol_ratio_red_z=3.0).marks,
                         [('vol_ratio', 'yellow')])

    def test_falls_back_to_fixed_multiple_when_z_unusable_or_disabled(self):
        # z 缺失（历史不足 / 量恒定）→ 固定倍数
        al = self.run_rule(self.context(ratio=1.6, z=None))
        self.assertEqual(al.marks, [('vol_ratio', 'yellow')])
        self.assertNotIn('自身波动', al.signals[0])
        # 任一条 z 阈值设为 0 = 关闭自适应
        for disabled in ({'vol_ratio_z': 0}, {'vol_ratio_red_z': 0}, {'vol_ratio_z': 0, 'vol_ratio_red_z': 0}):
            with self.subTest(disabled=disabled):
                al = self.run_rule(self.context(ratio=1.6, z=4.0), **disabled)
                self.assertEqual(al.marks, [('vol_ratio', 'yellow')])
                self.assertNotIn('自身波动', al.signals[0])
                # 关掉自适应后，1.3× 这种低于固定黄线的不再报
                self.assertEqual(self.run_rule(self.context(ratio=1.3, z=4.0), **disabled).marks, [])

    def test_shrinking_volume_never_lights_even_with_extreme_z(self):
        # 缩量方向不报灯：z 只回答「放得有多异常」，不把地量当放量
        al = self.run_rule(self.context(ratio=0.8, z=3.0))
        self.assertEqual(al.marks, [])
        self.assertEqual(self.run_rule(self.context(ratio=1.0, z=0.2)).marks, [])

    def test_intraday_keeps_fixed_multiple_and_shows_no_sigma(self):
        # 盘中口径一点不动：仍按固定倍数、仍带时刻、不叠加昨日完整日
        al = self.run_rule(self.context(intraday=True, rt=1.6, z=9.9))
        self.assertEqual(al.marks, [('vol_ratio', 'yellow')])
        self.assertIn('盘中量比（截至 12:00 ET）1.6x', al.signals[0])
        self.assertNotIn('自身波动', al.signals[0])
        self.assertEqual(len(al.signals), 1)
        self.assertEqual(self.run_rule(self.context(intraday=True, rt=1.2, z=9.9)).marks, [])

    def test_detail_carries_z_but_page_never_prints_sigma(self):
        d = {'vol_ratio': 1.3, 'vol_ratio_z': 1.6, 'vol_ratio_date': '2026-10-13'}
        self.assertEqual(monitor._member_metric(d, 'vol_ratio'), '量比 1.3×（2026-10-13完整日）')
        with patch.object(monitor, 'S', {**monitor.S, 'vol_ratio_z': 0}):
            self.assertEqual(monitor._member_metric(d, 'vol_ratio'), '量比 1.3×（2026-10-13完整日）')
        # 自适应开着也不把 σ 写出来：用户看不懂，判定照用
        self.assertNotIn('σ', monitor._member_metric(d, 'vol_ratio'))
        self.assertEqual(monitor._member_metric(dict(d, vol_ratio_z=None), 'vol_ratio'),
                         '量比 1.3×（2026-10-13完整日）')

    def test_end_to_end_after_close_lights_by_own_swing(self):
        from datetime import datetime
        data = self.history()
        data = dict(data, volumes=data['volumes'][:-1] + [self.base_mean(data) * 1.5])
        with patch.object(monitor, 'NOW', datetime.fromisoformat(self.CLOSE)), \
                patch.object(monitor, 'TARGET_DATE', None):
            _, signals, detail = monitor.analyze_symbol('AAPL', {}, data, 'technology')
        self.assertIsInstance(detail.get('vol_ratio_z'), float)
        self.assertGreater(detail['vol_ratio_z'], 1.8)   # 这条波动序列上 1.5× 已超过 1.8σ
        self.assertIn(('vol_ratio', 'yellow'), detail['signal_marks'])
        self.assertTrue(any(text == '最近完整日量比（2026-10-13）1.5x' for text in signals), signals)
        # 同一只股放到 3× 量：z 更高，直接红
        huge = dict(data, volumes=data['volumes'][:-1] + [self.base_mean(data) * 3])
        with patch.object(monitor, 'NOW', datetime.fromisoformat(self.CLOSE)), \
                patch.object(monitor, 'TARGET_DATE', None):
            _, _, huge_detail = monitor.analyze_symbol('AAPL', {}, huge, 'technology')
        self.assertGreater(huge_detail['vol_ratio_z'], 3.6)
        self.assertIn(('vol_ratio', 'red'), huge_detail['signal_marks'])
        # 量恒定 + 1.3×：算不出 σ，固定倍数也没到线，不报灯也不硬写 σ
        flat = self.history(flat=True, last=1300.0)
        with patch.object(monitor, 'NOW', datetime.fromisoformat(self.CLOSE)), \
                patch.object(monitor, 'TARGET_DATE', None):
            _, flat_signals, flat_detail = monitor.analyze_symbol('AAPL', {}, flat, 'technology')
        self.assertIsNone(flat_detail.get('vol_ratio_z'))
        self.assertFalse(any(k == 'vol_ratio' for k, _ in flat_detail['signal_marks']))
        self.assertFalse(any('量比' in text for text in flat_signals), flat_signals)

    def test_settings_zero_disables_without_being_overridden(self):
        self.assertFalse(monitor.valid_settings(
            {**monitor.DEFAULT_SETTINGS, 'vol_ratio_z': 0, 'vol_ratio_red_z': 0}))
        self.assertFalse(monitor.valid_settings({**monitor.DEFAULT_SETTINGS}))
        self.assertEqual(monitor.valid_settings(
            {**monitor.DEFAULT_SETTINGS, 'vol_ratio_z': 3.0, 'vol_ratio_red_z': 2.0}),
            'vol_ratio_z / vol_ratio_red_z')
        self.assertEqual(monitor.valid_settings({**monitor.DEFAULT_SETTINGS, 'vol_ratio_z': 11}),
                         'vol_ratio_z')
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'settings.json'), 'w', encoding='utf-8') as f:
                json.dump({'vol_ratio_z': 0, 'vol_ratio_red_z': 0}, f)
            with patch.object(monitor, 'BASE', tmp):
                loaded = monitor.load_settings()
        self.assertEqual((loaded['vol_ratio_z'], loaded['vol_ratio_red_z']), (0, 0))


class TestMaCrossConflict(unittest.TestCase):
    """同一格出现上下箭头时（不同周期均线方向相反），跟布林+RSI 方向不一致一样改用文字。"""

    def row(self, markers):
        return {'symbol': 'MIX', 'indicators': markers}

    def test_conflicting_directions_render_text(self):
        mixed = self.row([{'shape': '▲', 'tone': 'cyan', 'label': '上穿60日均线'},
                          {'shape': '▼', 'tone': 'cyan', 'label': '跌破250日均线'}])
        icons = monitor._member_icons(mixed, 'ma_cross', 'yellow')
        self.assertNotIn('▲', icons)
        self.assertNotIn('▼', icons)
        self.assertIn('上穿60日均线', icons)
        self.assertIn('跌破250日均线', icons)

    def test_single_direction_still_draws_triangle(self):
        up = self.row([{'shape': '▲', 'tone': 'cyan', 'label': '上穿250日均线'}])
        self.assertIn('▲', monitor._member_icons(up, 'ma_cross', 'yellow'))
        self.assertIn('上穿250日均线', monitor._member_icons(up, 'ma_cross', 'yellow'))

    def test_other_kinds_keep_icons(self):
        # 只有均线穿越做这条防冲突处理；布林/RSI 本身不会产生同格反向标记
        boll = self.row([{'shape': '▼', 'tone': 'yellow', 'label': '盘中跌破下轨'}])
        self.assertIn('▼', monitor._member_icons(boll, 'boll', 'yellow'))


class TestMemberDetailDirectionSplit(unittest.TestCase):
    """统计面板明细改版：▲ 一段 → 一条横线 → ▼ 一段；无分组小标题、无中文备注名、
    分组名放行尾括号。同向内部仍按分组展示顺序稳定排列。"""

    @staticmethod
    def _entry(symbol, group, shape, note=''):
        return {'symbol': symbol, 'group': group, 'note': note, 'level': 'red',
                'indicators': [{'shape': shape, 'tone': 'white', 'label': '方向'}]}

    def _panel(self, entries, kind='chg', level='red'):
        import hashlib
        output = monitor._signal_counts_card(
            {'signal_counts': {kind: {'label': '涨跌幅', level: len(entries)}},
             'signal_members': {kind: {level: entries}}})
        panel_id = f'signal-members-{hashlib.sha256(kind.encode("utf-8")).hexdigest()}-{level}'
        match = re.search(r'<div class="signal-members-panel tone-' + level + r'" id="' + panel_id
                          + r'" hidden>(.*?)</div>(?=<div class="signal-members-panel|</td>)',
                          output, re.S)
        self.assertIsNotNone(match, output)
        return match.group(1)

    def test_up_block_then_divider_then_down_block(self):
        entries = [self._entry('DN_FOCUS', 'focus', '▼', '下备注'),
                   self._entry('UP_FOCUS', 'focus', '▲', '上备注'),
                   self._entry('DN_POS', 'position', '▼'),
                   self._entry('UP_POS', 'position', '▲')]
        body = self._panel(entries)
        self.assertNotIn('<h4>', body)                       # 没有分组小标题
        self.assertEqual(body.count('class="signal-member-split"'), 1)
        head, tail = body.split('class="signal-member-split"', 1)
        for sym in ('UP_POS', 'UP_FOCUS'):
            self.assertIn(sym, head, body)
            self.assertNotIn(sym, tail, body)
        for sym in ('DN_POS', 'DN_FOCUS'):
            self.assertIn(sym, tail, body)
            self.assertNotIn(sym, head, body)
        self.assertLess(head.index('UP_POS'), head.index('UP_FOCUS'))   # 同向按分组顺序
        self.assertLess(tail.index('DN_POS'), tail.index('DN_FOCUS'))
        # 中文备注名跟在代码后面（放在跳转链接内），分组名仍然不写
        self.assertIn('>UP_FOCUS</span><span class="note">上备注</span></a>', body)
        self.assertIn('>DN_FOCUS</span><span class="note">下备注</span></a>', body)
        self.assertIn('class="signal-member-jump"', body)     # 名字可点跳转，不再写分组名
        self.assertEqual(body.count('class="signal-member-list"'), 1)

    def test_single_direction_has_no_divider(self):
        body = self._panel([self._entry('UP_A', 'position', '▲'),
                            self._entry('UP_B', 'focus', '▲')])
        self.assertNotIn('class="signal-member-split"', body)
        self.assertEqual(body.count('class="signal-member-symbol"'), 2)

    def test_text_direction_falls_into_trailing_block(self):
        entries = [self._entry('UP_A', 'position', '▲'),
                   {'symbol': 'TXT', 'group': 'focus', 'note': '', 'level': 'red'},
                   self._entry('DN_B', 'position', '▼')]
        body = self._panel(entries)
        # 顺序固定为 ▲ 段 → 横线 → ▼ 段 → 横线 → 文字段
        self.assertEqual(body.count('class="signal-member-split"'), 2)
        up, rest = body.split('class="signal-member-split"', 1)
        down, text = rest.split('class="signal-member-split"', 1)
        self.assertIn('UP_A', up)
        self.assertNotIn('方向待下次更新', up)
        self.assertIn('DN_B', down)
        self.assertIn('TXT', text)
        self.assertIn('方向待下次更新', text)


class TestFuturesIdentity(unittest.TestCase):
    """期货恢复：不冒充现金指数/ETF，不把过期数据当作当日行情。"""

    def bars(self, end='2026-10-09', **extra):
        from datetime import date, timedelta
        dates = [(date.fromisoformat(end) - timedelta(days=i)).isoformat()
                 for i in range(34, -1, -1)]
        return dict(dates=dates, closes=[7800.0] * 35, highs=[7810.0] * 35,
                    lows=[7790.0] * 35, volumes=[1000.0] * 35, price=7800.0,
                    prev_close=7800.0, quote_symbol='ES=F', quote_type='FUTURE',
                    currency='USD', quote_timezone='America/New_York',
                    quote_name='E-Mini S&P 500 Dec 26', **extra)

    def fetch(self, bars, symbol='ESMAIN', closed=True):
        with patch.object(monitor, 'yahoo_history', return_value=bars) as yahoo, \
                patch.object(monitor, 'stooq_history') as stooq, \
                patch.object(monitor, 'nasdaq_history') as nasdaq, \
                patch.object(monitor, 'nasdaq_realtime') as realtime, \
                patch.object(monitor, 'TARGET_DATE', '2026-10-09'), \
                patch.object(monitor, 'CLOSED_ONLY', closed), patch.object(monitor, 'log'):
            result = monitor.fetch_history(symbol)
        yahoo.assert_called_once_with('ES=F')
        stooq.assert_not_called(); nasdaq.assert_not_called(); realtime.assert_not_called()
        return result

    def test_alias_and_direct_future_use_only_verified_future(self):
        for sym in ('ESMAIN', 'ESmain', 'ES=F'):
            for closed in (True, False):
                with self.subTest(symbol=sym, closed=closed):
                    got = self.fetch(self.bars(), sym, closed)
                    self.assertEqual((got['price'], got['source'], got['quote_symbol']),
                                     (7800.0, 'yahoo', 'ES=F'))

    def test_wrong_symbol_type_currency_or_timezone_is_rejected(self):
        changes = ({'quote_symbol': 'SPY'}, {'quote_symbol': '^GSPC'},
                   {'quote_symbol': 'MES=F'}, {'quote_type': 'ETF'}, {'quote_type': ''},
                   {'currency': 'CNY'}, {'quote_timezone': 'UTC'},
                   {'quote_name': 'S&P 500 CFD'})
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.fetch(dict(self.bars(), **change)))

    def test_stale_future_never_becomes_current_signal(self):
        for closed in (True, False):
            self.assertIsNone(self.fetch(self.bars(end='2026-10-08'), closed=closed))

    def test_future_bar_is_trimmed_for_closed_report_only(self):
        got = self.fetch(self.bars(end='2026-10-10'))
        self.assertEqual(got['dates'][-1], '2026-10-09')
        self.assertIsNone(self.fetch(self.bars(end='2026-10-10'), closed=False))

    def test_missing_and_corrupted_history_fail_closed(self):
        for data in (None, dict(self.bars(), highs=[1] * 35),
                     dict(self.bars(), closes=[float('nan')] * 35)):
            self.assertIsNone(self.fetch(data))

    def test_future_has_no_earnings_or_consensus_request(self):
        self.assertFalse(monitor.is_etf('ESMAIN'))
        self.assertFalse(monitor.consensus_wanted('ESMAIN', {}, 'index_funds'))
        with patch.object(monitor, 'nasdaq_earnings') as api, patch.object(monitor, 'log'):
            stat = monitor.attach_earnings([{'symbol': 'ESMAIN', 'group': 'index_funds',
                                             'level': 'green'}], today='2026-10-10')
        api.assert_not_called()
        self.assertEqual(stat['targets'], 0)

    def test_yahoo_parser_preserves_identity_and_exchange_day(self):
        from datetime import datetime
        base = self.bars()
        ticks = [int(datetime.fromisoformat(d + 'T00:00:00-04:00').timestamp())
                 for d in base['dates']]
        payload = {'chart': {'result': [{'meta': {'symbol': 'ES=F', 'instrumentType': 'FUTURE',
                    'currency': 'USD', 'exchangeName': 'CME', 'exchangeTimezoneName': 'America/New_York',
                    'shortName': 'E-Mini S&P 500 Dec 26'}, 'timestamp': ticks,
                    'indicators': {'quote': [{'close': base['closes'], 'high': base['highs'],
                                              'low': base['lows'], 'volume': base['volumes']}]}}]}}
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        with patch.object(monitor.requests, 'get', return_value=response), \
                patch.object(monitor, 'YAHOO_GATE', monitor.SourceGate()):
            got = monitor.yahoo_history('ES=F')
        self.assertTrue(monitor.valid_futures_identity(got, 'ES=F'))
        self.assertEqual(got['quote_exchange'], 'CME')
        self.assertEqual(got['dates'], base['dates'])


class TestVolTier(unittest.TestCase):
    def setUp(self):
        # 固定中档阈值，不依赖线上 settings.json
        patcher = patch.dict(monitor.S, {'chg_yellow': 2.0, 'chg_red': 4.0, 'amp_yellow': 5.0, 'amp_red': 8.0})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def series(amp, n=120):
        """固定种子的正态日收益序列，日波动标准差 ≈ amp%。"""
        import random
        rnd = random.Random(7)
        out, c = [], 100.0
        for _ in range(n):
            c *= 1 + rnd.gauss(0, amp) / 100
            out.append(c)
        return out

    @staticmethod
    def data(closes, last_chg, hi_pct=0.0, lo_pct=0.0):
        closes = list(closes)
        prev = closes[-1]
        price = prev * (1 + last_chg / 100)
        closes.append(price)
        n = len(closes)
        return {'closes': closes, 'highs': [c * 1.002 for c in closes[:-1]] + [price * (1 + hi_pct / 100)],
                'lows': [c * 0.998 for c in closes[:-1]] + [price * (1 - lo_pct / 100)],
                'volumes': [1e6] * n, 'price': price, 'prev_close': prev,
                'dates': [f'2026-01-{i % 28 + 1:02d}' for i in range(n)], 'source': 't', 'realtime': False}

    def test_tier_boundaries(self):
        self.assertEqual(monitor.vol_profile(self.series(0.5))[0], 'low')
        self.assertEqual(monitor.vol_profile(self.series(1.8))[0], 'mid')
        self.assertEqual(monitor.vol_profile(self.series(4.0))[0], 'high')

    def test_new_listing_defaults_to_mid(self):
        tier, sigma = monitor.vol_profile(self.series(0.5, n=30))
        self.assertEqual((tier, sigma), ('mid', None))

    def test_single_spike_does_not_decide_tier(self):
        closes = self.series(0.8)
        closes[60] *= 2.7          # 一次 +170% 的财报/数据异常
        for i in range(61, len(closes)):
            closes[i] *= 2.7
        self.assertEqual(monitor.vol_profile(closes)[0], 'low')

    def test_today_move_does_not_raise_its_own_threshold(self):
        closes = self.series(0.5)
        self.assertEqual(monitor.vol_profile(closes + [closes[-1] * 1.30])[0], 'low')

    def test_same_move_hits_differently_per_tier(self):
        # 同样涨 2.5%：低波动股（门槛 1.2/2.4）报红，中波动股（2/4）报黄，高波动股（2.8/5.6）不报
        low = monitor.analyze_symbol('L', {}, self.data(self.series(0.5), 2.5), group='position')
        mid = monitor.analyze_symbol('M', {}, self.data(self.series(1.8), 2.5), group='position')
        high = monitor.analyze_symbol('H', {}, self.data(self.series(4.0), 2.5), group='position')
        self.assertTrue(any('异动 +2.5%' in x for x in low[1]), low[1])
        self.assertTrue(any('波动 +2.5%' in x for x in mid[1]), mid[1])
        self.assertFalse(any(('异动' in x or '波动' in x) for x in high[1]), high[1])
        self.assertEqual((low[2]['vol_tier'], mid[2]['vol_tier'], high[2]['vol_tier']), ('low', 'mid', 'high'))

    def test_signal_text_shows_usual_range(self):
        _, sig, _ = monitor.analyze_symbol('M', {}, self.data(self.series(1.8), 3.0), group='position')
        self.assertTrue(any('平时约 ±' in x for x in sig), sig)
        _, sig2, _ = monitor.analyze_symbol('N', {}, self.data(self.series(1.8, n=30), 3.0), group='position')
        self.assertFalse(any('平时约' in x for x in sig2), sig2)

    def test_amplitude_scales_with_tier(self):
        # 振幅 7%：低档(红 6%)红；中档(黄 5% 红 8%)黄；高档(黄 7%，刚好命中)黄
        for amp, expect in ((0.5, 'red'), (1.8, 'yellow')):
            lv, sig, _ = monitor.analyze_symbol('A', {}, self.data(self.series(amp), 0.0, hi_pct=3.5, lo_pct=3.5), group='position')
            self.assertTrue(any('振幅' in x for x in sig), sig)
            self.assertEqual(lv, expect, (amp, sig))
        _, sig, _ = monitor.analyze_symbol('A', {}, self.data(self.series(4.0), 0.0, hi_pct=2.5, lo_pct=2.5), group='position')
        self.assertFalse(any('振幅' in x for x in sig), sig)

    def test_change_and_amplitude_only_apply_to_position_and_focus(self):
        big = self.data(self.series(1.8), 9.0, hi_pct=6.0, lo_pct=6.0)   # 涨 9%、振幅很大
        for grp in ('position', 'focus'):
            lv, sig, _ = monitor.analyze_symbol('X', {}, big, group=grp)
            self.assertEqual(lv, 'red', (grp, sig))
            self.assertTrue(any('异动' in x for x in sig), sig)
            self.assertTrue(any('振幅' in x for x in sig), sig)
        for grp in ('technology', 'index_funds', 'healthcare'):
            lv, sig, _ = monitor.analyze_symbol('X', {}, big, group=grp)
            self.assertFalse(any(('异动' in x or '波动' in x or '振幅' in x) for x in sig), (grp, sig))


class TestMaConfirm(unittest.TestCase):
    """第一条确认穿越，第二条日内±1%触及与简单穿越独立；0分别关闭。"""

    def setUp(self):
        target = patch.object(monitor, 'TARGET_DATE', '')
        target.start()
        self.addCleanup(target.stop)

    @staticmethod
    def flat(n=300, last=(100, 100)):
        closes = [100.0] * n
        closes[-2:] = list(last)
        return closes

    def test_confirmed_cross_up_needs_band_and_prior_side(self):
        c = [100.0] * 300
        for i in range(-6, -1):
            c[i] = 95.0                       # 最近几天在均线下方
        self.assertEqual(monitor.ma_confirmed_cross(c, 101.5, 50, 1.0), 'up')
        self.assertIsNone(monitor.ma_confirmed_cross(c, 99.9, 50, 1.0))     # 没站上带外
        flat = [100.0] * 300
        self.assertIsNone(monitor.ma_confirmed_cross(flat, 101.5, 50, 1.0)) # 10 日内没在另一侧

    def test_confirmed_cross_down(self):
        c = [100.0] * 300
        for i in range(-6, -1):
            c[i] = 105.0
        self.assertEqual(monitor.ma_confirmed_cross(c, 98.0, 50, 1.0), 'down')

    def test_short_history_falls_back(self):
        self.assertEqual(monitor.ma_confirmed_cross([100.0] * 55, 101, 50, 1.0), 'n/a')

    def test_threshold_follows_volatility(self):
        self.assertEqual(monitor.ma_confirm_pct(None), monitor.MA_CONFIRM_DEFAULT)
        self.assertEqual(monitor.ma_confirm_pct(0.2), monitor.MA_CONFIRM_MIN)
        self.assertEqual(monitor.ma_confirm_pct(2.0), 1.0)
        self.assertEqual(monitor.ma_confirm_pct(20), monitor.MA_CONFIRM_MAX)

    @staticmethod
    def run_sym(closes, shorts=50, longs=250):
        price = closes[-1]
        data = {'closes': closes, 'highs': [c for c in closes], 'lows': [c * 0.999 for c in closes],
                'volumes': [1000] * len(closes), 'price': price, 'prev_close': closes[-2],
                'dates': ['2026-10-09'] * len(closes), 'source': 't', 'realtime': False}
        with patch.dict(monitor.S, {'ma_short': shorts, 'ma_long': longs, 'near_52w_low_pct': 0.0,
                                      'chg_yellow': 5.0, 'chg_red': 8.0, 'amp_yellow': 5.0, 'amp_red': 8.0}), \
                patch.object(monitor, 'vol_profile', return_value=('mid', None)), \
                patch.object(monitor, 'calc_rsi', return_value=50), \
                patch.object(monitor, 'boll', return_value=(None, None, None)):
            return monitor.analyze_symbol('T', {}, data, group='watch')

    def test_marginal_cross_only_hits_long_ma(self):
        # 昨收略低、今收略高于均线：250 日线在 ±1% 带内亮黄，50 日线不报穿越
        level, signals, _ = self.run_sym(self.flat(last=(99.9, 100.05)))
        self.assertTrue(any('日内触及250日均线' in x and '现价偏离 +0.1%' in x for x in signals), signals)
        self.assertIn('上穿250日均线', signals)
        self.assertEqual(level, 'yellow')
        self.assertFalse(any(('上穿50日' in x or '跌破50日' in x) for x in signals), signals)

    def test_long_ma_band_is_plus_minus_one_percent(self):
        for last, hit in ((100.9, True), (99.1, True), (101.5, False), (98.5, False)):
            with self.subTest(price=last):
                lv, signals, detail = self.run_sym(self.flat(last=(100.0, last)))
                self.assertEqual(any('日内触及250日均线' in x for x in signals), hit, signals)
                if hit:
                    self.assertEqual(lv, 'yellow')
                    self.assertEqual(detail['ma_band_direction'], 'up' if last > 100 else 'down')
                else:
                    self.assertEqual(detail['ma_band_direction'], '')

    def test_long_ma_big_gap_cross_alerts_outside_band(self):
        for last, expected in (((99.0, 105.0), '上穿250日均线'),
                               ((101.0, 95.0), '跌破250日均线')):
            with self.subTest(last=last):
                _, signals, _ = self.run_sym(self.flat(last=last))
                self.assertIn(expected, signals)
                self.assertFalse(any('日内触及250日' in s for s in signals), signals)

    def test_long_ma_price_staying_on_same_side_does_not_cross(self):
        for last in ((103, 105), (97, 95)):
            _, signals, _ = self.run_sym(self.flat(last=last), shorts=0)
            self.assertFalse(any('上穿250日' in s or '跌破250日' in s for s in signals), signals)

    def test_long_ma_history_requires_previous_average_for_cross_only(self):
        for n in (249, 250, 251):
            with self.subTest(n=n):
                _, signals, _ = self.run_sym(self.flat(n=n, last=(99.9, 100.05)), shorts=0)
                self.assertEqual(any('日内触及250日' in s for s in signals), n >= 250, signals)
                self.assertEqual('上穿250日均线' in signals, n >= 251, signals)

    def test_long_ma_uses_previous_day_average_and_exact_boundaries(self):
        from types import SimpleNamespace
        cases = [
            (99, 100, 101, 100, '上穿250日均线', True),
            (101, 100, 99, 100, '跌破250日均线', True),
            (100, 100, 101, 100, None, True),
            (100, 100, 99, 100, None, True),
            (99, 100, 100, 100, '上穿250日均线', True),
            (101, 100, 100, 100, '跌破250日均线', True),
            (100.5, 100, 102, 101, None, True),
            (99.5, 100, 98, 99, None, False),
        ]
        for prev, prev_ma, price, current_ma, cross, band in cases:
            with self.subTest(prev=prev, prev_ma=prev_ma, price=price, current_ma=current_ma):
                x = SimpleNamespace(closes=[100.0] * 249 + [prev, price], price=price,
                                    moving_averages=[(250, current_ma)], vol_sigma=None)
                al = monitor._Alerts()
                with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}), \
                        patch.object(monitor, 'sma', return_value=prev_ma) as sma_mock:
                    monitor._rule_moving_averages(al, x)
                sma_mock.assert_called_once_with(x.closes[:-1], 250)
                actual = [s for s in al.signals if '上穿' in s or '跌破' in s]
                self.assertEqual(actual, [cross] if cross else [])
                self.assertEqual(any('现价贴近' in s for s in al.signals), band)

    def test_long_ma_intraday_range_overlap_and_reset(self):
        from types import SimpleNamespace
        cases = [(102, 101, 104, True), (98, 96, 99, True),
                 (104, 98, 104, True), (102, 101.0001, 104, False),
                 (98, 96, 98.9999, False), (100, 100, 100, True)]
        for price, low, high, hit in cases:
            with self.subTest(price=price, low=low, high=high):
                closes = [100.0] * 249 + [102, price]
                x = SimpleNamespace(closes=closes, price=price,
                                    moving_averages=[(250, 100)], vol_sigma=None,
                                    data={'highs': [100.0] * 250 + [high],
                                          'lows': [100.0] * 250 + [low]})
                al = monitor._Alerts()
                with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}), \
                        patch.object(monitor, 'sma', return_value=100):
                    monitor._rule_moving_averages(al, x)
                self.assertEqual(any('日内触及250日' in s for s in al.signals), hit)
                self.assertFalse(any(re.search(r'(?<!\d)50日', s) for s in al.signals))
        # 昨日曾触及不延续到今天：只取最后一根，今天范围完全在上方。
        x.data['highs'][-2:] = [104, 105]
        x.data['lows'][-2:] = [98, 103]
        x.price = 104
        x.closes[-1] = 104
        al = monitor._Alerts()
        with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}):
            monitor._rule_moving_averages(al, x)
        self.assertFalse(any('日内触及' in s for s in al.signals))

    def test_long_ma_bad_or_missing_ranges_only_use_current_price(self):
        from types import SimpleNamespace
        n = 251
        invalid = [{}, {'highs': [], 'lows': []},
                   {'highs': [104], 'lows': [98]},
                   {'highs': [104] * n, 'lows': [98] * (n - 1)},
                   {'highs': [float('nan')] * n, 'lows': [98] * n},
                   {'highs': [104] * n, 'lows': [float('inf')] * n},
                   {'highs': [True] * n, 'lows': [98] * n},
                   {'highs': [97] * n, 'lows': [98] * n},
                   {'highs': [104] * n, 'lows': [0] * n}]
        for data in invalid:
            for price, hit in ((100, True), (104, False)):
                x = SimpleNamespace(closes=[100.0] * n, price=price,
                                    moving_averages=[(250, 100)], data=data, vol_sigma=None)
                al = monitor._Alerts()
                with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}):
                    monitor._rule_moving_averages(al, x)
                bands = [s for s in al.signals if '日内高低缺失，仅按现价' in s]
                self.assertEqual(bool(bands), hit)
                self.assertFalse(any('日内触及' in s for s in al.signals))

    def test_long_ma_old_or_missing_date_does_not_replay_events(self):
        from types import SimpleNamespace
        for date in ('2026-10-08', '', '2026-10-09'):
            x = SimpleNamespace(closes=[100] * 249 + [99, 105], price=105,
                                moving_averages=[(250, 100)], vol_sigma=None,
                                data={'dates': [date] * 251, 'highs': [105] * 251, 'lows': [98] * 251})
            al = monitor._Alerts()
            with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}), \
                    patch.object(monitor, 'TARGET_DATE', '2026-10-09'), \
                    patch.object(monitor, 'sma', return_value=100):
                monitor._rule_moving_averages(al, x)
            self.assertEqual(len(al.signals), 2 if date == '2026-10-09' else 0)

    def test_yahoo_missing_extremes_preserved_through_trim(self):
        from datetime import datetime, timedelta
        dates = [(datetime(2026, 1, 1) + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(251)]
        ticks = [int(datetime.fromisoformat(d + 'T12:00:00-04:00').timestamp()) for d in dates]
        for key in ('high', 'low'):
            quote = {'close': [100] * 251, 'high': [101] * 251, 'low': [99] * 251, 'volume': [1000] * 251}
            quote[key][-1] = None
            payload = {'chart': {'result': [{'timestamp': ticks, 'meta': {},
                                            'indicators': {'quote': [quote]}}]}}
            response = MagicMock(status_code=200)
            response.json.return_value = payload
            with patch.object(monitor.requests, 'get', return_value=response), \
                    patch.object(monitor, 'YAHOO_GATE', monitor.SourceGate()):
                got = monitor.yahoo_history('T')
            self.assertTrue(monitor.valid_history(got))
            got = monitor.trim_history(got, dates[-1])
            x = monitor._analysis_context('T', {}, got, 'technology')
            self.assertEqual(monitor._ma_day_range(x), (100, 100, False))
            previous = monitor.trim_history(got, dates[-2])
            x = monitor._analysis_context('T', {}, previous, 'technology')
            self.assertEqual(monitor._ma_day_range(x), (99, 101, True))

    def test_long_ma_intraday_touch_and_cross_integrate_without_new_red(self):
        closes = self.flat(last=(99, 105))
        data = {'closes': closes, 'highs': closes[:], 'lows': closes[:],
                'volumes': [1000] * len(closes), 'price': 105, 'prev_close': 99}
        data['lows'][-1] = 100
        with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}), \
                patch.object(monitor, 'boll', return_value=(None, None, None)), \
                patch.object(monitor, 'calc_rsi', return_value=50):
            level, signals, detail = monitor.analyze_symbol('T', {}, data, group='technology')
        self.assertEqual(level, 'yellow')
        self.assertIn('上穿250日均线', signals)
        self.assertTrue(any('日内触及250日' in s for s in signals), signals)
        counts = monitor.signal_counts([detail])
        self.assertEqual(counts['ma_cross']['yellow'], 1)
        self.assertEqual(counts['ma_band']['yellow'], 1)
        self.assertEqual(counts['ma_band']['label'], '长均线触及')

    def test_long_ma_dual_signals_count_independently_without_double_counting_stock(self):
        level, signals, detail = self.run_sym(self.flat(last=(99.9, 100.05)), shorts=0)
        self.assertEqual(level, 'yellow')
        self.assertIn('上穿250日均线', signals)
        self.assertTrue(any('日内触及250日' in s for s in signals))
        counts = monitor.signal_counts([detail])
        self.assertEqual(counts['ma_cross']['yellow'], 1)
        self.assertEqual(counts['ma_band']['yellow'], 1)
        self.assertEqual(counts['ma_cross']['label'], '均线穿越')

    def test_long_ma_follows_configured_period(self):
        for period in (2, 50, 200, 250):
            _, signals, _ = self.run_sym(self.flat(last=(99, 105)), shorts=0, longs=period)
            self.assertIn(f'上穿{period}日均线', signals)
            if period != 50:
                self.assertFalse(any(re.search(r'(?<!\d)50日均线', s) for s in signals))

    def test_decisive_cross_hits_short_ma(self):
        c = [100.0] * 300
        for i in range(-6, -1):
            c[i] = 95.0
        c[-1] = 104.0
        _, signals, _ = self.run_sym(c)
        self.assertTrue(any('上穿50日均线' in x for x in signals), signals)

    def test_single_ma_keeps_old_rule(self):
        _, signals, _ = self.run_sym(self.flat(last=(99.9, 100.05)), shorts=50, longs=50)
        self.assertTrue(any('上穿50日均线' in x for x in signals), signals)

    def test_zero_disables_only_its_own_rule(self):
        scenarios = [self.flat(last=(99.9, 100.05)), self.flat(last=(100, 100.9)),
                     self.flat(last=(100, 99.1)), self.flat(last=(99, 105))]
        for previous, price in ((95, 104), (105, 96)):
            c = self.flat()
            c[-6:-1] = [previous] * 5
            c[-1] = price
            scenarios.append(c)
        for closes in scenarios:
            _, all_signals, _ = self.run_sym(closes)
            for short, long in ((0, 250), (50, 0), (0, 0)):
                with self.subTest(price=closes[-1], short=short, long=long):
                    _, signals, _ = self.run_sym(closes, shorts=short, longs=long)
                    expected = [s for s in all_signals
                                if not (short == 0 and re.search(r'(?<!\d)50日均线', s))
                                and not (long == 0 and re.search(r'(?<!\d)250日均线', s))]
                    self.assertEqual(signals, expected)
                    self.assertFalse(any('0日均线' in s and not ('50日' in s or '250日' in s) for s in signals))
        _, signals, _ = self.run_sym(scenarios[0], shorts=50, longs=0)
        self.assertNotIn('上穿50日均线', signals, '关闭第二条不能把第一条改成简单穿越')
        _, signals, _ = self.run_sym(scenarios[0], shorts=0, longs=250)
        self.assertTrue(any('日内触及250日均线' in s for s in signals))
        self.assertIn('上穿250日均线', signals)

    def test_disabled_mas_never_compute_zero_and_bollinger_is_unchanged(self):
        closes = self.flat(last=(99, 104))
        data = {'closes': closes, 'highs': closes[:], 'lows': closes[:], 'volumes': [1000]*len(closes),
                'price': closes[-1], 'prev_close': closes[-2]}
        with patch.dict(monitor.S, {'ma_short': 50, 'ma_long': 250}):
            _, baseline, detail = monitor.analyze_symbol('X', {}, data)
        original_sma = monitor.sma
        def guarded_sma(vals, period):
            self.assertGreater(period, 0)
            return original_sma(vals, period)
        with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 0}), \
                patch.object(monitor, 'sma', side_effect=guarded_sma):
            _, signals, disabled = monitor.analyze_symbol('X', {}, data)
        self.assertEqual(signals, [s for s in baseline if '日均线' not in s])
        self.assertTrue(any('布林' in s for s in signals))
        for key in ('rsi', 'boll_up', 'boll_dn', 'price', 'chg', 'vol_ratio'):
            self.assertEqual(disabled[key], detail[key], key)


class Test52wLow(unittest.TestCase):
    """52 周新低：第一天/再创新低红 → 在低位区没再创新低黄 → 远离后不报。"""

    @staticmethod
    def run_sym(closes, lows=None, price=None):
        n = len(closes)
        price = closes[-1] if price is None else price
        series = list(closes)
        series[-1] = price
        lows = lows or [c * 0.999 for c in series]
        data = {'closes': series, 'highs': [c * 1.001 for c in series], 'lows': lows,
                'volumes': [1000] * n, 'price': price, 'prev_close': series[-2],
                'dates': [(__import__('datetime').date(2026, 1, 1) + __import__('datetime').timedelta(days=i)).isoformat()
                          for i in range(n)], 'source': 't', 'realtime': False}
        with patch.dict(monitor.S, {'near_52w_low_pct': 3.0}), \
                patch.object(monitor, 'boll', return_value=(None, None, None)):
            return monitor.analyze_symbol('T', {}, data, group='watch')

    def base(self):
        return [100.0 - i * 0.2 for i in range(200)]   # 缓慢下行，最后一天附近就是低点

    def test_first_day_into_zone_is_red(self):
        c = [100.0] * 150 + [80.0] * 20 + [90.0] * 29 + [82.0]   # 昨天离低点(80)+12%，今天回到+2.5%
        lv, sig, _ = self.run_sym(c)
        self.assertEqual(lv, 'red')
        self.assertTrue(any('触及52周新低' in x for x in sig), sig)

    def test_new_low_again_is_red(self):
        c = self.base()
        lv, sig, _ = self.run_sym(c)          # 每天都在创新低
        self.assertEqual(lv, 'red')
        self.assertTrue(any('触及52周新低' in x for x in sig), sig)

    def test_staying_in_zone_without_new_low_is_yellow(self):
        c = [100.0] * 150 + [80.0] * 20 + [81.0] * 29 + [81.5]   # 昨天已在低位区，今天没破 80
        lv, sig, _ = self.run_sym(c, lows=[x * 0.999 for x in c[:-1]] + [81.0])
        # lows 里 80*0.999=79.92 是前低；今天最低 81 没破
        self.assertEqual(lv, 'yellow')
        self.assertTrue(any('仍在52周低位' in x for x in sig), sig)
        self.assertFalse(any('触及52周新低' in x for x in sig), sig)

    def test_far_from_low_is_silent(self):
        c = [100.0] * 150 + [80.0] * 20 + [95.0] * 30
        lv, sig, _ = self.run_sym(c)
        self.assertFalse(any('52周' in x for x in sig), sig)


class Test52wRangeBar(unittest.TestCase):
    """区间条与旧52周低位报警相互独立，只渲染有真实日内高低价的快照。"""

    @staticmethod
    def history():
        from datetime import date, timedelta
        dates = [date(2025, 9, 30).isoformat()] + [
            (date(2025, 10, 14) + timedelta(weeks=i)).isoformat() for i in range(53)]
        count = len(dates)
        return {'dates': dates, 'highs': [500.0] + [200.0] * (count - 1),
                'lows': [1.0] + [100.0] * (count - 1), 'price': 150.0,
                'ma_range_valid': [True] * count}

    def test_52_week_high_low_position_uses_aligned_intraday_extremes(self):
        data = self.history()
        result = monitor._range52_from_history(data)
        self.assertEqual((result['low'], result['high'], result['scope']), (100.0, 200.0, '52周'))
        self.assertEqual(result['position'], 50.0)
        row = monitor._range52_row({'range52': result, 'price': 150.0, 'data_date': result['to']})
        self.assertIn('style="--range-pos:50.0%"', row)
        self.assertIn('<span class="range52-percent" aria-hidden="true">50.0%</span>', row)
        self.assertIn('style="left:50.0%"', row)
        self.assertIn('.range52-track{position:relative;flex:1 1 auto;height:2.1px;', monitor.PAGE_CSS)
        # 2026-10-20：现价标记从「压在轨道中间的紫方块」改成「尖角朝下、顶在灰轨上沿的倒三角」
        self.assertIn('.range52-marker{position:absolute;bottom:100%;width:0;height:0;', monitor.PAGE_CSS)
        self.assertIn('border-top:9px solid #efd9ff;', monitor.PAGE_CSS)   # 顶角 60°（底 10.4 / 高 9）
        self.assertIn('.range52-marker::before{', monitor.PAGE_CSS)
        self.assertNotIn('width:8.4px;height:8.4px;border-radius:2px;background:#713caf', monitor.PAGE_CSS)
        self.assertIn('height:2.1px;background:#9fa3aa;border-radius:99px}', monitor.PAGE_CSS)
        visible = re.sub(r' aria-label="[^"]*"', '', row)
        for text in ('52周低 100.00', '52周高 200.00', '区间位置 50.0%',
                     '本轮价 150.00', '日线盘中高低价', '未满52周'):
            self.assertNotIn(text, visible)
        self.assertNotIn('500.00', row)  # 窗口外旧高点不能污染标尺
        old_invalid = dict(data, ma_range_valid=[False] + data['ma_range_valid'][1:])
        self.assertEqual(monitor._range52_from_history(old_invalid)['position'], 50.0)
        self.assertNotIn('title=', row)
        self.assertEqual(monitor._range52_row({'range52': result, 'price': 100.0, 'data_date': result['to']}).count('left:0.0%'), 1)
        self.assertEqual(monitor._range52_row({'range52': result, 'price': 200.0, 'data_date': result['to']}).count('left:100.0%'), 1)
        self.assertEqual(monitor._range52_row({'reference_only': True, 'range52': result, 'price': 150.0}), '')

    def test_cost_price_draws_a_white_triangle_without_touching_the_range_bar(self):
        """成本价只在位置条上多一个白三角：紫方块是本轮价（条上），白三角是成本价（条下）。
        成本价不参与灯级，写错或没写都不能画出来。"""
        result = monitor._range52_from_history(self.history())   # 低 100 / 高 200 / 本轮价 150
        base = {'range52': result, 'price': 150.0, 'data_date': result['to']}
        row = monitor._range52_row({**base, 'cost': 125.0})
        self.assertIn('<span class="range52-cost" style="left:25.0%"></span>', row)
        self.assertIn('成本价 125.00 位于区间 25.0%', row)
        self.assertNotIn('低于区间', row)
        self.assertIn('style="left:50.0%"', row)          # 本轮价的紫方块位置不变
        self.assertIn('>50.0%</span>', row)               # 百分比文字仍然是本轮价
        below = monitor._range52_row({**base, 'cost': 40.0})
        self.assertIn('<span class="range52-cost out-lo"></span>', below)   # 区间外：朝外箭头，不再夹成 0% 假位置
        self.assertIn('range52-cost out-lo', below)
        self.assertIn('成本价 40.00 低于52周低点 100.00', below)
        above = monitor._range52_row({**base, 'cost': 260.0})
        self.assertIn('<span class="range52-cost out-hi"></span>', above)   # 高于区间：朝外箭头，不再夹成 100% 假位置
        self.assertIn('成本价 260.00 高于52周高点 200.00', above)
        self.assertEqual(monitor.cost_rule({'cost': 288.5}), 288.5)
        for bad in (0, -1, '300', True, None, float('nan'), float('inf'), ''):
            with self.subTest(bad=bad):
                self.assertIsNone(monitor.cost_rule({'cost': bad}))
                self.assertNotIn('range52-cost', monitor._range52_row({**base, 'cost': bad}))
        plain = monitor._range52_row(base)
        self.assertNotIn('range52-cost', plain)
        self.assertIn('.range52-cost{position:absolute;top:50%;width:2.2px;height:10.5px;', monitor.PAGE_CSS)
        self.assertIn('background:#f0c674', monitor.PAGE_CSS)

    def test_short_history_and_missing_or_filled_range_do_not_claim_52_weeks(self):
        data = self.history()
        short = {**data, **{key: data[key][-30:] for key in ('dates', 'highs', 'lows', 'ma_range_valid')}}
        result = monitor._range52_from_history(short)
        self.assertEqual(result['scope'], '可用历史')
        short_row = monitor._range52_row({'range52': result, 'price': 150.0, 'data_date': result['to']})
        self.assertIn('aria-label="可用历史低', short_row)
        self.assertIn('>50.0%</span>', short_row)
        self.assertNotIn('未满52周', short_row)
        self.assertIn('数据待更新', monitor._range52_row({'range52': result, 'price': 150.0,
                                                           'data_date': '2026-10-14'}))
        forged = {**result, 'position': '50%;color:red'}
        self.assertIn('数据待更新', monitor._range52_row({'range52': forged, 'price': 150.0,
                                                           'data_date': result['to']}))
        invalid = [dict(data, ma_range_valid=data['ma_range_valid'][:-1] + [False]),
                   dict(data, highs=data['highs'][:-1]), dict(data, lows=[]),
                   dict(data, price=201.0), dict(data, price=float('nan')),
                   dict(data, dates=data['dates'][:-1] + [None])]
        for sample in invalid:
            with self.subTest(sample=sample.get('price'), keys=len(sample.get('lows', []))):
                self.assertIsNone(monitor._range52_from_history(sample))
        self.assertIn('数据待更新', monitor._range52_row({'price': None}))
        self.assertNotIn('range52-track', monitor._range52_row({'price': None}))
        self.assertIn('数据待更新', monitor._range52_row({'price': 150,
            'range52': {**result, 'high': '<script>'}}))

    def test_stock_row_uses_one_full_width_bar_without_changing_score_or_counts(self):
        interval = monitor._range52_from_history(self.history())
        row = {'symbol': 'HII', 'note': '', 'price': 150.0, 'chg': -2.34,
               'level': 'yellow', 'signals': [], 'range52': interval, 'data_date': interval['to']}
        output = monitor._stock_row(row, {})
        self.assertIn('HII', output)
        self.assertEqual(output.count('class="range52-row"'), 1)
        self.assertIn('.stk tr:has(+tr.range52-row)>td{padding-top:5px;padding-bottom:2px}', monitor.PAGE_CSS)  # 位置条贴近本股
        self.assertIn('.stk tr.range52-row td{padding:0 12px 7px 18px;', monitor.PAGE_CSS)  # 与下一只股票拉开
        self.assertIn('.stk tr.red+tr.range52-row>td{--lc:var(--red);--lw:6px}', monitor.PAGE_CSS)  # 灯条延伸到本股位置条
        self.assertIn('background-size:var(--lw) calc(85% - 2.1px);background-position:left bottom', monitor.PAGE_CSS)  # 灯条顶部下移
        self.assertIn('</tr><tr class="range52-row"><td colspan="4">', output)
        self.assertEqual(monitor.signal_counts([row])['low52']['red'], 0)


class TestDisplayInteractions(unittest.TestCase):
    """展示改动不改变业务信号；颜色、局部收起与计数位置均覆盖。"""

    def test_signal_color_priority_and_html_escape(self):
        cases = [
            ('RSI 2条超卖（≤30）：RSI6=20', 'signal-magenta'),
            ('RSI 3条超卖（≤30）：RSI6=20', 'signal-magenta'),
            ('RSI 2条超买（≥70）：RSI6=80', ''),
            ('贴近布林上轨（距 0.2%）', ''),
            ('逼近布林上轨 还差0.29%', ''),
            ('触及布林上轨 103.00', ''),
            ('盘中突破布林上轨 101.00（盘中触及 103.00）', ''),
            ('突破布林上轨 101.00', ''),
            ('逼近布林下轨 还差0.30%', 'signal-yellow'),
            ('逼近布林下轨 还差0.08%（盘中触及 50.04）', 'signal-yellow'),
            ('触及布林下轨 99.00', 'signal-yellow'),
            ('跌破布林下轨', 'signal-yellow'),
            ('布林 + RSI → 红', 'signal-red'),
            ('日内触及250日均线±1%（现价偏离 +3.0%）', 'signal-cyan'),
            ('上穿250日均线', 'signal-cyan'),
            ('跌破250日均线', 'signal-cyan'),
            ('上穿50日均线', ''),
            ('距目标价 100 还差 1%', 'tp'),
            ('其他<script>alert(1)</script>', ''),
        ]
        with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}):
            for text, cls in cases:
                with self.subTest(text=text):
                    rendered = monitor._signal_text_html(text)
                    self.assertNotIn('<script>', rendered)
                    if cls:
                        self.assertTrue(rendered.startswith(f'<span class="{cls}">'), rendered)
                    else:
                        self.assertNotIn('<span', rendered)
        with patch.dict(monitor.S, {'ma_long': 0}):
            self.assertNotIn('signal-cyan', monitor._signal_text_html('上穿250日均线'))

    def test_lower_boll_approach_renders_yellow_text_without_changing_level(self):
        row = {'signals': ['逼近布林下轨 还差0.08%（盘中触及 50.04）'],
               'target': {}, 'level': 'yellow', 'signal_marks': [('boll', 'yellow')]}
        self.assertIn('<span class="signal-yellow">逼近布林下轨', monitor._signals_html(row))
        self.assertEqual(row['level'], 'yellow')
        self.assertEqual(row['signal_marks'], [('boll', 'yellow')])

    def test_signal_text_order_and_original_data_are_unchanged(self):
        import copy
        row = {'signals': ['距目标价 100 还差 1%', '跌破布林下轨', 'RSI 2条超卖'],
               'target': {}, 'level': 'yellow', 'signal_marks': [('boll', 'yellow')]}
        old = copy.deepcopy(row)
        output = monitor._signals_html(row)
        self.assertLess(output.index('跌破布林'), output.index('距目标价'))
        self.assertEqual(row, old)

    def test_render_moves_index_funds_below_real_estate_without_changing_groups(self):
        import copy
        original = copy.deepcopy(monitor.GROUPS)
        original_keys = ['positions', 'focus', 'index_funds', 'technology', 'healthcare',
                         'financials', 'energy', 'industrials', 'materials', 'utilities',
                         'consumer_staples', 'communications', 'consumer_discretionary', 'real_estate']
        self.assertEqual([g['key'] for g in monitor.GROUPS], original_keys)
        expected = [key for key in original_keys if key != 'index_funds'] + ['index_funds']
        self.assertEqual([g['key'] for g in monitor._display_groups()], expected)
        page = monitor.render({}, [], 0, snapshot={})
        rendered = re.findall(r'<details class="card group-card" id="group_([^"]+)"', page)
        self.assertEqual(rendered, expected)
        self.assertEqual(rendered[-2:], ['real_estate', 'index_funds'])
        self.assertEqual([key for key in rendered if key != 'index_funds'],
                         [key for key in original_keys if key != 'index_funds'])
        self.assertEqual(monitor.GROUPS, original)

    def test_every_group_has_local_bottom_collapse_button(self):
        self.assertEqual(len(monitor.GROUPS), 14)
        for group in monitor.GROUPS:
            with self.subTest(group=group['key']):
                with patch.object(monitor, '_collapse_button', wraps=monitor._collapse_button) as helper:
                    output = monitor._group_card(group, [], {}, {})
                card_id = f'group_{group["key"]}'
                helper.assert_called_once_with(card_id, group['label'])
                buttons = re.findall(r'<button\b([^>]*)>(.*?)</button>', output, re.S)
                self.assertEqual(len(buttons), 1)
                attrs, content = buttons[0]
                self.assertIn('class="collapse-group"', attrs)
                self.assertIn(f'aria-controls="{card_id}"', attrs)
                self.assertIn('type="button"', attrs)
                self.assertIn(f'aria-label="收起{group["label"]}"', attrs)
                self.assertNotIn('title=', attrs)
                self.assertEqual(content, '<span class="collapse-icon" aria-hidden="true">▲</span>')
                self.assertTrue(output.endswith(monitor._collapse_button(card_id, group['label'])
                                                + '</details>\n'))
        self.assertIn('card.open = false', monitor.PAGE_INTERACTION_JS)
        self.assertIn('heading.focus({preventScroll: true})', monitor.PAGE_INTERACTION_JS)
        # 统计面板：展开和收起都要归一化一次（修掉「收起后重开、内层依旧占位」的留白）
        self.assertIn('card.id !== "signalCounts") return;', monitor.PAGE_INTERACTION_JS)
        self.assertNotIn('card.id !== "signalCounts" || card.open', monitor.PAGE_INTERACTION_JS)
        self.assertIn('normalizeSignalCard(card);', monitor.PAGE_INTERACTION_JS)
        self.assertIn('void card.offsetHeight;', monitor.PAGE_INTERACTION_JS)
        # 移动端兜底：点标题后下一帧再归一化一次 + bfcache 返回时复位（2026-10-15 手机端仍留白）
        self.assertIn('target.closest("#signalCounts > summary")', monitor.PAGE_INTERACTION_JS)
        self.assertIn('requestAnimationFrame(function() { normalizeSignalCard(card); });', monitor.PAGE_INTERACTION_JS)
        self.assertIn('window.addEventListener("pageshow"', monitor.PAGE_INTERACTION_JS)
        self.assertIn('card.querySelectorAll(".signal-members-panel")', monitor.PAGE_INTERACTION_JS)
        self.assertIn('card.querySelectorAll(".signal-count-toggle")', monitor.PAGE_INTERACTION_JS)
        self.assertNotIn('fetch(', monitor.PAGE_INTERACTION_JS)
        self.assertNotIn('localStorage', monitor.PAGE_INTERACTION_JS)

    def test_opening_a_signal_list_keeps_it_in_view(self):
        # 「同时只开一个」会先收起上一个清单、把整页拉上去，新点开的清单容易跳到视口外。
        # 展开后必须：先把被点按钮拉回原屏幕位置，再把新清单整块带进视口。
        js = monitor.PAGE_INTERACTION_JS
        self.assertIn('const buttonTopBefore = button.getBoundingClientRect().top;', js)
        self.assertIn('revealSignalPanel(panel);', js)
        self.assertIn('window.scrollBy(0, delta);', js)
        self.assertIn('window.scrollTo({ top: target < 0 ? 0 : target, behavior: "smooth" });', js)
        # 只在展开时定位，收起时不该乱滚
        self.assertIn('if (opening) {\n    //', js)
        self.assertIn('function revealSignalPanel(panel) {', js)

    def test_collapse_button_css_has_full_width_short_transparent_strip(self):
        button_rule = re.search(r'\.collapse-group\{([^}]+)\}', monitor.PAGE_CSS)
        self.assertIsNotNone(button_rule)
        for declaration in ('width:100%', 'height:26px', 'padding:0', 'background:transparent', 'border:0',
                            'display:flex', 'align-items:center', 'justify-content:center', 'box-sizing:border-box'):
            self.assertIn(declaration, button_rule.group(1))
        self.assertNotIn('width:44px', button_rule.group(1))
        self.assertIn('.collapse-group .collapse-icon{font-size:16px;line-height:1}', monitor.PAGE_CSS)
        strip_rule = re.search(r'\.group-bottom\{([^}]+)\}', monitor.PAGE_CSS)
        self.assertRegex(strip_rule.group(1), r'(?:^|;)padding:0(?:;|$)')
        self.assertIn('margin:0 -4px -6px', strip_rule.group(1))
        self.assertIn('line-height:0', strip_rule.group(1))
        self.assertIn('.collapse-group:hover{color:var(--text);background:transparent}', monitor.PAGE_CSS)

    def test_jump_flash_never_overwrites_the_lamp_bar(self):
        """闪烁只能用 background-color：带 52 周条的行灯条是 background-image 画的，
        background 简写会把灯条一起清掉，看起来就是高亮盖住了灯条。"""
        rule = re.search(r'tr\.stk-flash[^{]*\{([^}]+)\}', monitor.PAGE_CSS)
        self.assertIsNotNone(rule)
        self.assertIn('animation:stkflash', rule.group(1))
        # 位置条是主行的下一个兄弟行，一起闪才不会断成两截
        self.assertIn('tr.stk-flash+tr.range52-row>td', monitor.PAGE_CSS)
        frames = re.search(r'@keyframes stkflash\{(.*?)\}\s', monitor.PAGE_CSS + ' ', re.S)
        self.assertIsNotNone(frames)
        self.assertIn('background-color:#2b3f63', frames.group(1))
        self.assertIn('background-color:transparent', frames.group(1))
        for bad in ('background:#2b3f63', 'background:transparent'):   # 简写会清掉灯条的 background-image
            self.assertNotIn(bad, frames.group(1))

    def test_counts_panel_precedes_market_risk_and_has_distinct_frame(self):
        snap = {'signal_counts': {'ma_band': {'label': '长均线触及', 'red': 0,
                 'yellow': 5, 'purple': 0}}, 'summary': {'yellow': 5}}
        page = monitor.render({}, [], 0, snapshot=snap)
        self.assertLess(page.index('id="signalCounts"'), page.index('id="macroCard"'))
        self.assertIn('#signalCounts{border:1px solid #6686c6', page)
        self.assertIn(monitor.PAGE_INTERACTION_JS, page)
        controls = re.findall(r'<button type="button" class="collapse-group" aria-controls="([^"]+)"', page)
        self.assertEqual(controls, ['signalCounts', 'macroCard'] + [f'group_{g["key"]}' for g in monitor._display_groups()])
        self.assertEqual(len(controls), 16)
        # 只看真正的 HTML：仓位面板的收起条是运行时 JS 建的，不能把 <script> 源码里的同一串也算进去
        self.assertEqual(re.sub(r"<script.*?</script>", "", page, flags=re.S)
                         .count('<span class="collapse-icon" aria-hidden="true">▲</span>'), 16)

    def test_market_risk_has_local_bottom_button_after_reference_content(self):
        page = monitor.render({}, [], 0, snapshot={})
        macro = page.split('id="macroCard"', 1)[1].split('<details class="card group-card"', 1)[0]
        button = monitor._collapse_button('macroCard', '市场风险')
        self.assertEqual(macro.count('class="collapse-group"'), 1)
        self.assertIn(button + '\n</details>', macro)
        self.assertGreater(macro.index(button), macro.index('</table>'))
        self.assertIn('aria-label="收起市场风险"', button)
        self.assertNotIn('title=', button)


class TestPositionZones(unittest.TestCase):
    """首页持仓：内部按 hold 拆成「长持仓」「短持仓」两个可折叠区，各自统计、各自收起。"""

    class _Row(dict):
        """个股行字段很多，测试只关心 level / hold，缺失字段按空值处理。"""
        def __missing__(self, key):
            return ''

    @classmethod
    def item(cls, symbol, level, hold=None):
        row = cls._Row(symbol=symbol, level=level, group='position', chg=0.0, price=100.0, signal_marks=[])
        if hold:
            row['hold'] = hold
        return row

    @staticmethod
    def positions_group():
        return next(g for g in monitor.GROUPS if g['key'] == 'positions')

    def card(self, items):
        return monitor._group_card(self.positions_group(), items, {}, {})

    def test_only_the_long_zone_is_a_sub_section(self):
        """用户 2026-10-16 定稿：短持仓不再单包一层，展开「持仓」直接就是短持仓的表，
        上方只多一条可展开的「长持仓」。"""
        out = self.card([self.item('AAA', 'green', 'long'),
                         self.item('BBB', 'red', 'short'),
                         self.item('CCC', 'yellow')])
        self.assertEqual(re.findall(r'<details class="zone-card" id="([^"]+)" open>', out),
                         ['group_positions_long'])
        self.assertEqual(re.findall(r'<span class="zone-name">([^<]+)</span>', out), ['长持仓 (1)'])
        self.assertNotIn('短持仓 (', out)
        self.assertNotIn('group_positions_short', out)
        # 长持仓区一条收起条 + 外层「持仓」卡一条
        self.assertEqual(re.findall(r'aria-controls="([^"]+)"', out),
                         ['group_positions_long', 'group_positions'])
        long_part = out.split('</details>', 1)[0] if False else out.split('<details class="zone-card"', 1)[1]
        long_zone = long_part.split('</details>', 1)[0]
        self.assertIn('AAA', long_zone)
        self.assertNotIn('BBB', long_zone)
        # 短持仓在长持仓区之后、直接作为卡片主体
        self.assertIn('BBB', out.split('</details>', 1)[1])
        self.assertTrue(out.rstrip().endswith(monitor._collapse_button('group_positions', '持仓')
                                              + '</details>'))

    def test_empty_zone_is_not_rendered(self):
        out = self.card([self.item('BBB', 'red', 'short')])
        self.assertNotIn('group_positions_long', out)
        self.assertNotIn('zone-card', out)

    def test_unmarked_positions_count_as_short(self):
        # 没标 hold 的算短持仓，因此不会出现长持仓区，标的落在卡片主体表里
        out = self.card([self.item('CCC', 'green')])
        self.assertNotIn('长持仓 (', out)
        self.assertNotIn('zone-card', out)
        self.assertIn('CCC', out)

    def test_long_zone_does_not_repeat_the_rule_text(self):
        # 用户 2026-10-16 定稿：首页长持仓区不再挂那段规则说明（规则只在设置页和页底折叠里讲一次）
        out = self.card([self.item('AAA', 'green', 'long')])
        self.assertNotIn('长持仓不看一般过热', out)
        self.assertNotIn('class="quiet"', out.split('zone-card', 1)[-1].split('<table', 1)[0])

    def test_quiet_list_starts_collapsed_and_resets_when_the_zone_closes(self):
        # 用户 2026-10-16 定稿：区里的「无异动 N 只」默认折叠；关掉区再打开时不该还留着上次的展开态。
        out = self.card([self.item('AAA', 'red', 'long'),
                         self.item('BBB', 'green', 'long'),
                         self.item('CCC', 'green', 'short')])
        # 渲染时不带 open 属性 —— 第一次进来就是折叠的
        self.assertNotIn('<details class="quiet-list" open>', out)
        self.assertIn('<details class="quiet-list"><summary>无异动 1 只 · 点击查看</summary>', out)
        # 收起卡片时把内部已展开的无异动清单一并复位（浏览器不会自己忘掉 details 的 open）
        js = monitor.PAGE_INTERACTION_JS
        self.assertIn('function resetQuietLists(card) {', js)
        self.assertIn('card.querySelectorAll("details.quiet-list[open]")', js)
        self.assertIn('if (!card.querySelector("details.quiet-list[open]")) return;', js)
        self.assertIn('resetQuietLists(card);', js)

    def test_zone_collapse_bar_is_spaced_from_the_outer_one(self):
        # 子区收起条紧贴外层「持仓」卡收起条时会误点，两条之间必须有间距
        self.assertIn('.zone-card + .group-bottom,.zone-card + .quiet + .group-bottom{margin-top:22px}',
                      monitor.PAGE_CSS)

    def test_other_groups_keep_one_body(self):
        group = next(g for g in monitor.GROUPS if g['key'] == 'focus')
        self.assertNotIn('zone-card', monitor._group_card(group, [], {}, {}))


class TestSignalCounts(unittest.TestCase):
    """status.json 的 signal_counts：各类信号各触发多少、哪些黄灯只由一类信号造成。"""

    @staticmethod
    def row(level, *marks):
        return {'symbol': 'T', 'level': level, 'signal_marks': list(marks)}

    def test_counts_by_kind_and_level(self):
        rows = [self.row('red', ('chg', 'red'), ('rsi', 'yellow')),
                self.row('yellow', ('boll', 'yellow')),
                self.row('yellow', ('boll', 'yellow'), ('vol_ratio', 'yellow')),
                self.row('green', ('target', 'purple'))]
        out = monitor.signal_counts(rows)
        self.assertEqual((out['chg']['red'], out['chg']['yellow']), (1, 0))
        self.assertEqual(out['rsi']['yellow'], 1)
        self.assertEqual(out['boll']['yellow'], 2)
        self.assertEqual(out['target']['purple'], 1)
        # 「只出文字不报灯」的灰灯不再按信号类分列，单独走 text_only_members()；
        # 这三行都没有 RSI / 布林文字，灰灯名单应为空
        self.assertEqual(monitor.text_only_members(rows), [])

    def test_one_stock_counts_once_per_kind(self):
        out = monitor.signal_counts([self.row('yellow', ('ma_cross', 'yellow'), ('ma_cross', 'yellow'))])
        self.assertEqual(out['ma_cross']['yellow'], 1)

    def test_every_kind_is_listed_even_when_zero(self):
        out = monitor.signal_counts([])
        self.assertEqual(list(out), [k for k, _ in monitor.SIGNAL_KINDS])
        self.assertTrue(all(v['red'] == v['yellow'] == v['purple'] == 0 for v in out.values()))

    def test_unknown_kind_is_ignored(self):
        out = monitor.signal_counts([self.row('yellow', ('mystery', 'yellow'))])
        self.assertNotIn('mystery', out)

    def test_analyze_symbol_records_marks_that_match_signals(self):
        closes = [100.0] * 60
        data = {'closes': closes + [110.0], 'highs': [c * 1.001 for c in closes] + [110.2],
                'lows': [c * 0.999 for c in closes] + [109.8], 'volumes': [1e6] * 61,
                'price': 110.0, 'prev_close': 100.0, 'dates': [f'2026-01-{i % 28 + 1:02d}' for i in range(61)],
                'source': 't', 'realtime': False}
        with patch.object(monitor, 'vol_profile', return_value=('mid', None)):
            lv, sigs, det = monitor.analyze_symbol('T', {}, data, 'position')
        marks = det['signal_marks']
        # marks 只记录会亮灯的信号；RSI 条数与布林轨道状态现在只是文字，不进 marks，
        # 所以 signals 会比 marks 多。这里保证「有灯必有对应条目」，不再要求一一相等。
        self.assertTrue(0 < len(marks) <= len(sigs), (marks, sigs))
        self.assertIn(('chg', 'red'), marks)

    def test_signal_counts_card_on_homepage(self):
        rows = [{'symbol': 'AAA', 'level': 'yellow', 'signal_marks': [('boll', 'yellow')]},
                {'symbol': 'BBB', 'level': 'red', 'signal_marks': [('chg', 'red'), ('rsi', 'yellow')]}]
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        page = monitor.render({}, [], 0, snapshot=snap)
        self.assertIn('id="signalCounts"', page)
        self.assertIn('<span class="grp-name">统计</span>', page)
        self.assertNotIn('仅此一条黄灯', page)
        self.assertLess(page.index('id="signalCounts"'), page.index('class="foot"'))
        # 这一轮一条都没触发：整块统计不出现，只有旧快照完全没有该字段时才同样不显示
        quiet = monitor.render({}, [], 0, snapshot=monitor.build_snapshot({}, [], {}, group_counts={}))
        self.assertNotIn('id="signalCounts"', quiet)
        # 旧快照完全没有 signal_counts 字段时才整块不显示
        self.assertNotIn('id="signalCounts"', monitor.render({}, [], 0, snapshot={}))

    def test_counts_card_has_only_triangle_after_table_and_no_long_help(self):
        snap = {'signal_counts': monitor.signal_counts([self.row('yellow', ('boll', 'yellow'))])}
        with patch.object(monitor, '_collapse_button', wraps=monitor._collapse_button) as helper:
            output = monitor._signal_counts_card(snap)
        helper.assert_called_once_with('signalCounts', '统计')
        self.assertNotIn('macro-help', output)
        self.assertIn('</table><div class="signal-filter" id="signalFilterResult" role="status" data-filter="', output)
        self.assertTrue(output.endswith('" hidden></div>' + monitor._collapse_button('signalCounts', '统计')
                                        + '</details>\n'))
        buttons = re.findall(r'<button\b([^>]*\bclass="collapse-group"[^>]*)>(.*?)</button>', output, re.S)
        self.assertEqual(len(buttons), 1)
        attrs, content = buttons[0]
        for attribute in ('type="button"', 'aria-controls="signalCounts"',
                          'aria-label="收起统计"'):
            self.assertIn(attribute, attrs)
        self.assertEqual(content, '<span class="collapse-icon" aria-hidden="true">▲</span>')

    def test_earnings_is_not_added_to_signal_kinds_counts_or_members(self):
        expected = ['chg', 'amp', 'vol_ratio', 'rsi', 'boll', 'boll_rsi',
                    'ma_cross', 'ma_band', 'low52', 'add_price', 'reduce_price', 'target',
                    'fund']
        self.assertEqual([kind for kind, _ in monitor.SIGNAL_KINDS], expected)
        rows = [self.member_row('AAA', 'position', 'red', *((kind, 'red') for kind in expected)),
                self.member_row('BBB', 'focus', 'yellow', ('earnings', 'yellow'))]
        # 日期必须落在「美东今天起一周内」才会进「未来一周财报」，写死日期会随当天时间失效
        soon = (monitor.NOW.astimezone(monitor.TZ).date() + timedelta(days=2)).isoformat()
        rows[1].update(earnings={'status': 'ok', 'date': soon}, signals=['财报临近'])
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(list(snap['signal_counts']), expected)
        self.assertEqual(list(snap['signal_members']), expected)
        for kind in expected:
            with self.subTest(kind=kind):
                self.assertEqual(snap['signal_counts'][kind]['red'], 1)
                self.assertEqual(snap['signal_counts'][kind]['yellow'], 0)
                self.assertEqual([d['symbol'] for d in snap['signal_members'][kind]['red']], ['AAA'])
        output = monitor._signal_counts_card(snap)
        self.assertEqual(output.count('class="signal-members-row"'), len(expected))
        self.assertNotIn('<td>财报</td>', output)  # 不混入红黄紫交易信号
        self.assertIn('未来一周财报', output)
        self.assertIn('<strong>BBB</strong>', output)
        self.assertNotIn('>BBB</span>', output.split('earnings-week', 1)[0])

    def test_next_week_earnings_members_use_us_calendar_day_and_keep_green_visible(self):
        with patch.object(monitor, '_earn_today', return_value='2026-10-14'):
            def item(symbol, group, date, level='green', status='ok', kind='expected'):
                row = self.member_row(symbol, group, level)
                row.update(price=100.0, chg=0.0, signals=[], earnings={
                    'status': status, 'date': date, 'timing': 'post', 'kind': kind},
                    data_date='2026-10-13')
                return row
            rows = [item('AAPL', 'technology', '2026-10-14'),
                    item('BRK.B-US', 'position', '2026-10-21', kind='official'),
                    item('BRK-B', 'technology', '2026-10-21'),  # 持仓优先，不能重复
                    item('LATE', 'focus', '2026-10-22'),
                    item('PAST', 'position', '2026-10-13'),
                    item('UNKNOWN', 'technology', '2026-10-15', status='unknown')]
            snap = monitor.build_snapshot({}, rows, {'group_monitoring': {'technology': True}}, group_counts={})
            self.assertEqual(snap['earnings_week']['asof'], '2026-10-14')
            self.assertEqual([m['symbol'] for m in snap['earnings_week']['members']], ['AAPL', 'BRK-B'])
            self.assertEqual(snap['signal_counts']['chg']['red'], 0)
            self.assertEqual(snap['summary']['green'], 6)  # 财报只提升可见性，绿灯不改黄
            panel = monitor._signal_counts_card(snap)
            self.assertIn('<details class="earnings-week"><summary>未来一周财报 · 2 只</summary>', panel)
            self.assertNotIn('<details class="earnings-week" open>', panel)
            self.assertIn('<summary class="grp"><span class="grp-name">统计</span><span class="group-stats">'
                          '<span class="stat-badge red" aria-label="红 0 项">0</span>'
                          '<span class="stat-badge yellow" aria-label="黄 0 项">0</span>', panel)
            self.assertIn('紫 0', panel)          # 无紫灯总数时按目标价紫色计数补
            self.assertIn('财报 2', panel)
            old = monitor._signal_counts_card({'summary': {'red': 4, 'yellow': 32},
                                               'signal_counts': {'target': {'label': '目标价', 'purple': 3}}})
            for text in ('红 4', '黄 32', '紫 3', '财报 —'):  # 旧快照无紫总数/财报名单时不冒充0
                self.assertIn(text, old)
            self.assertNotIn('美东 2026-10-14 至 2026-10-21', panel)
            self.assertNotIn('预计或估算日期可能调整', panel)
            self.assertIn('earnings.open = false;', monitor.PAGE_INTERACTION_JS)
            # 折叠态绝对不占高度：移动端内核不重排时用 CSS 兜底
            self.assertIn('#signalCounts .earnings-week:not([open])>*:not(summary){display:none}', monitor.PAGE_CSS)
            self.assertIn('#signalCounts .earnings-week summary{padding:8.1px 0;color:var(--text)', monitor.PAGE_CSS)
            self.assertIn('#signalCounts td{padding:6.3px 5px;', monitor.PAGE_CSS)  # 统计面板压缩上下留白
            self.assertIn('.signal-count-toggle{box-sizing:border-box;height:38px;min-width:44px;', monitor.PAGE_CSS)  # 数字胶囊扁长方形
            self.assertIn('.signal-filter-toggle{box-sizing:border-box;height:38px;', monitor.PAGE_CSS)  # 与信号名胶囊同高对齐
            # 财报的灯跟统计表其它栏目一致：行首圆点（marker），不再在行尾挂一个 stat-dot
            self.assertIn('<li class="lamp-green"><strong>AAPL</strong>', panel)
            self.assertIn('10/14 盘后 · 预计 · <span class="earnings-week-group">IT软硬Ai</span>'
                          '</span><span class="lamp-text">（绿灯）</span>', panel)
            self.assertIn('10/21 盘后 · 官方活动日期 · <span class="earnings-week-group">持仓</span>'
                          '</span><span class="lamp-text">（绿灯）</span>', panel)
            self.assertNotIn('stat-dot', panel)
            self.assertNotIn('· 绿灯', panel)
            self.assertIn('#signalCounts .earnings-week-group{color:#8fb8f0}', monitor.PAGE_CSS)
            red = monitor._earnings_week_panel({'earnings_week': {'asof': '2026-10-14', 'members': [
                {'symbol': 'NOC', 'group': 'position', 'level': 'red', 'date': '2026-10-20'},
                {'symbol': 'BAD', 'group': 'position', 'level': '<x>', 'date': '2026-10-20'}]}})
            self.assertIn('<li class="lamp-red"><strong>NOC</strong>', red)
            self.assertIn('<li><strong>BAD</strong>', red)          # 未知灯级不画灯、不注入
            self.assertEqual(red.count('class="lamp-red"'), 1)   # 只有 NOC 有灯
            self.assertNotIn('<strong>LATE</strong>', panel)
            self.assertNotIn('<table>', panel)  # 只有财报名单时不画信号计数表，不伪造信号
            page = monitor.render({}, rows, len(rows), snapshot=snap)
            technology = page.split('id="group_technology"', 1)[1].split('id="group_energy"', 1)[0]
            self.assertTrue(technology.startswith('>'))  # 近期财报不自动展开分类，用户点开后才看到
            self.assertEqual(re.findall(r'<details class="card[^"]*" id="(\w+)" open>', page), ['signalCounts'])  # 只默认展开统计
            self.assertIn('<td class="sym">AAPL', technology.split('<details class="quiet-list">')[0])
            self.assertIn('无异动 1 只', technology)
            position = page.split('id="group_positions"', 1)[1].split('id="group_focus"', 1)[0]
            self.assertIn('<td class="sym">BRK-B', position.split('<details class="quiet-list">')[0])

    def test_earnings_week_rejects_bad_date_missing_and_expired(self):
        with patch.object(monitor, '_earn_today', return_value='2026-10-14'):
            rows = [self.member_row(name, 'position', 'green') for name in ('UNKNOWN', 'BROKEN', 'OLD', 'FUTURE')]
            for row, e in zip(rows, ({'status': 'unknown', 'date': '2026-10-15'},
                                     {'status': 'ok', 'date': '2026-10-50'},
                                     {'status': 'ok', 'date': '2026-10-13'},
                                     {'status': 'ok', 'date': '2026-10-22'})):
                row.update(price=100.0, chg=0.0, signals=[], earnings=e)
            self.assertEqual(monitor.earnings_week_members(rows, '2026-10-14'), [])
            self.assertNotIn('未来一周财报', monitor._signal_counts_card(
                monitor.build_snapshot({}, rows, {}, group_counts={})))

    def test_snapshot_carries_signal_counts(self):
        snap = monitor.build_snapshot({}, [self.row('red', ('chg', 'red'))], {}, group_counts={})
        self.assertEqual(snap['signal_counts']['chg']['red'], 1)
        self.assertEqual(snap['signal_members']['chg']['red'],
                         [{'symbol': 'T', 'note': '', 'group': '', 'hold': '', 'level': 'red'}])

    @staticmethod
    def member_row(symbol, group, level, *marks, note=''):
        return {'symbol': symbol, 'group': group, 'note': note,
                'level': level, 'signal_marks': list(marks)}

    def test_filter_buttons_and_intersection_data_without_changing_counts(self):
        import html as H
        rows = [self.member_row('AAA', 'position', 'red', ('vol_ratio', 'red'), ('boll', 'yellow')),
                self.member_row('BBB', 'technology', 'yellow', ('vol_ratio', 'yellow')),
                self.member_row('CCC', 'focus', 'red', ('vol_ratio', 'yellow'), ('boll_rsi', 'red')),
                self.member_row('DDD', 'technology', 'yellow', ('boll', 'yellow'))]
        snap = {'signal_counts': monitor.signal_counts(rows), 'signal_members': monitor.signal_count_members(rows)}
        output = monitor._signal_counts_card(snap)
        for kind in ('vol_ratio', 'boll', 'boll_rsi'):
            self.assertIn(f'class="signal-filter-toggle" data-kind="{kind}" aria-pressed="false">', output)
        data = json.loads(H.unescape(re.search(r'data-filter="([^"]*)"', output).group(1)))
        ids = lambda kind: [m['id'] for m in data[kind]['members']]
        self.assertEqual(ids('vol_ratio'), ['AAA', 'CCC', 'BBB'])  # 按首页分组顺序：持仓、重点、IT
        self.assertEqual(sorted(ids('boll')), ['AAA', 'CCC', 'DDD'])  # 布林含布林+RSI组合
        self.assertEqual(ids('boll_rsi'), ['CCC'])
        aaa = next(m for m in data['vol_ratio']['members'] if m['id'] == 'AAA')
        self.assertEqual((aaa['g'], aaa['l']), ('持仓', 'red'))
        self.assertEqual(snap['signal_counts']['boll']['yellow'], 2)  # 合并只用于筛选，不改计数
        broken = dict(snap, signal_members={**snap['signal_members'], 'boll': {'yellow': []}})
        bad = json.loads(H.unescape(re.search(r'data-filter="([^"]*)"', monitor._signal_counts_card(broken)).group(1)))
        self.assertFalse(bad['boll']['ok'])  # 名单与计数不符时筛选显示待更新，不拼残缺结果
        node = os.environ.get('NODE')
        if node:
            import subprocess
            script = (monitor.PAGE_INTERACTION_JS.split('function intersectSignals', 1)[1].split('function renderSignalFilter', 1)[0])
            code = ('function intersectSignals' + script + ';const d=' + json.dumps(data) + ';'
                    'const r=intersectSignals(d,["vol_ratio","boll"]);'
                    'const p=intersectSignals(Object.assign({},d,{boll:Object.assign({},d.boll,{ok:false})}),["vol_ratio","boll"]);'
                    'process.stdout.write(JSON.stringify([r.members.map(m=>m.id),r.members[1].details,p.pending]));')
            out = json.loads(subprocess.run([node, '-e', code], capture_output=True, text=True, encoding='utf-8', check=True).stdout)
            self.assertEqual(out[0], ['AAA', 'CCC'])
            self.assertEqual(out[1][1], '布林+RSI')
            self.assertEqual(out[2], ['布林'])
        self.assertIn('button.setAttribute("aria-pressed", "false");', monitor.PAGE_INTERACTION_JS)  # 收起统计时清空选择

    def panel(self, output, kind, level):
        import hashlib
        panel_id = f'signal-members-{hashlib.sha256(kind.encode("utf-8")).hexdigest()}-{level}'
        match = re.search(r'<div class="signal-members-panel tone-' + level + r'" id="' + panel_id
                          + r'" hidden>(.*?)</div>(?=<div class="signal-members-panel|</td>)',
                          output, re.S)
        self.assertIsNotNone(match, output)
        return match.group(1)

    def test_fund_is_counted_from_fund_field_and_deduped(self):
        """基本面不改变股价灯级，所以走 d['fund'] 单独统计；重复分组只算一只。"""
        rows = [self.member_row('AAA', 'position', 'green', ('target', 'purple')),
                self.member_row('BBB', 'focus', 'green'),
                self.member_row('CCC', 'technology', 'green')]
        rows[0]['fund'] = {'level': 'red', 'hits': ['营收较基准下降 24%'], 'period': '2026/6'}
        rows[1]['fund'] = {'level': 'yellow',
                           'hits': ['毛利率下滑 3.4 个百分点', '经营现金流为负']}
        rows[2]['fund'] = {'level': 'green', 'hits': []}
        counts = monitor.signal_counts(rows)
        members = monitor.signal_count_members(rows)
        self.assertEqual((counts['fund']['red'], counts['fund']['yellow']), (1, 1))
        self.assertEqual([d['symbol'] for d in members['fund']['red']], ['AAA'])
        self.assertEqual([d['symbol'] for d in members['fund']['yellow']], ['BBB'])
        # 同一只票同时出现在持仓和其他分类时，统计和名单都只算一次
        duplicated = [dict(rows[0]), dict(rows[0], group='technology')]
        self.assertEqual(monitor.signal_counts(duplicated)['fund']['red'], 1)
        self.assertEqual(len(monitor.signal_count_members(duplicated)['fund']['red']), 1)
        output = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
        self.assertIn('>基本面</button>', output)
        red_panel = self.panel(output, 'fund', 'red')
        self.assertIn('>AAA</span>', red_panel)
        # 命中项不在统计名单里复述（个股行自己有点开的折叠详情），只在行首挂灯色圆点
        self.assertNotIn('营收较基准下降 24%', red_panel)
        self.assertNotIn('毛利率下滑 3.4 个百分点', self.panel(output, 'fund', 'yellow'))
        # 灯就是其它栏目那个行首圆点，基本面不再自己画一个大的
        self.assertNotIn('signal-member-dot', output)
        self.assertIn('<div class="signal-members-panel tone-red"', output)
        self.assertIn('<div class="signal-members-panel tone-yellow"', output)
        self.assertNotIn('CCC', output)  # 基本面绿灯不进统计
        # 这一轮没有基本面警示：整块统计也不该出现
        quiet = [self.member_row('ZZZ', 'position', 'green')]
        self.assertEqual(monitor._signal_counts_card(
            {'signal_counts': monitor.signal_counts(quiet),
             'signal_members': monitor.signal_count_members(quiet)}), '')

    def test_fund_detail_lists_hits_without_any_lights(self):
        """个股行折叠里的基本面详情就是一份清单：不挂红/黄圆点，也不猜每条命中的等级。"""
        fund = {'level': 'red', 'period': '2026/6',
                'hits': ['营收较2025/9 -24%', '毛利率 50.0%→46.1%', '本季净利含一次性损益，ROE未参与判定']}
        html = monitor._fund_detail_html({'symbol': 'AAA', 'fund': fund})
        self.assertIn('<li>营收较2025/9 -24%</li>', html)
        self.assertIn('<li>毛利率 50.0%→46.1%</li>', html)
        self.assertNotIn('signal-member-dot', html)   # 详情里没有灯
        self.assertNotIn('hit_levels', html)

    def test_add_and_reduce_price_members_print_the_price(self):
        """统计面板的加仓价/减仓价名单里补上价位，写法跟个股行胶囊一致（加 325 / 减 400）。"""
        rows = [self.member_row('AAA', 'position', 'red', ('add_price', 'red')),
                self.member_row('BBB', 'position', 'red', ('reduce_price', 'red')),
                self.member_row('CCC', 'position', 'red', ('add_price', 'red'))]
        rows[0].update(trigger=325)
        rows[1].update(reduce=400.5)
        rows[2].update(trigger='写坏了')      # 无效价不占位，也不写空标签
        members = monitor.signal_count_members(rows)
        output = monitor._signal_counts_card({'signal_counts': monitor.signal_counts(rows),
                                              'signal_members': members})
        add_panel = self.panel(output, 'add_price', 'red')
        self.assertIn('>加 325</span>', add_panel)
        self.assertNotIn('加仓价 325', add_panel)      # 不在统计里另写长名
        self.assertIn('>减 400.5</span>', self.panel(output, 'reduce_price', 'red'))
        self.assertNotIn('待下次更新', add_panel)

    def test_low52_members_print_the_52_week_low_price(self):
        """52周低位那一栏把低点价本身写出来：只说「距低点 +2.6%」看不见价格。"""
        rows = [self.member_row('LOW', 'position', 'red', ('low52', 'red'))]
        rows[0].update(low52=12.3456)
        members = monitor.signal_count_members(rows)
        self.assertEqual(members['low52']['red'][0]['low52_price'], '12.35')
        panel = self.panel(monitor._signal_counts_card(
            {'signal_counts': monitor.signal_counts(rows), 'signal_members': members}), 'low52', 'red')
        self.assertIn('52周低 12.35', panel)
        # 旧快照没有低点价就不硬写
        old = [self.member_row('OLD', 'position', 'red', ('low52', 'red'))]
        old_panel = self.panel(monitor._signal_counts_card(
            {'signal_counts': monitor.signal_counts(old),
             'signal_members': monitor.signal_count_members(old)}), 'low52', 'red')
        self.assertNotIn('52周低', old_panel)

    def test_add_and_reduce_price_stay_two_rows_so_buy_and_sell_never_mix(self):
        """加仓价（该买）与减仓价（该卖）动作相反，统计面板各占一行、各带自己的价位。

        曾试过合并成「加/减仓价」一行：计数从标的数变成命中次数，同一只票两端都命中时
        表头会多算一只，而且「同时满足」筛选项也没法再单独挑减仓价，故改回两行。"""
        rows = [self.member_row('AAA', 'position', 'red', ('add_price', 'red'), ('reduce_price', 'red'))]
        rows[0].update(trigger=325, reduce=400.5)
        counts = monitor.signal_counts(rows)
        output = monitor._signal_counts_card({'signal_counts': counts,
                                              'signal_members': monitor.signal_count_members(rows)})
        self.assertIn('>加仓价</button>', output)
        self.assertIn('>减仓价</button>', output)
        self.assertEqual(counts['add_price']['red'], 1)
        self.assertEqual(counts['reduce_price']['red'], 1)
        self.assertIn('>加 325</span>', self.panel(output, 'add_price', 'red'))
        self.assertIn('>减 400.5</span>', self.panel(output, 'reduce_price', 'red'))
        self.assertEqual(output.count('class="signal-member-block"'), 0)   # 这两栏不分框

    def test_fund_members_show_a_light_dot_at_the_front_instead_of_the_hits(self):
        """基本面那一栏：行首挂灯色圆点（与其它栏目的方向图标位置一致），
        不再把命中项复述在代码后面。"""
        # 基本面只走 d['fund']，不进 signal_marks（进了会被算两遍，名单与计数对不上）
        rows = [self.member_row('AAA', 'position', 'red', note='甲'),
                self.member_row('BBB', 'position', 'yellow')]
        rows[0]['fund'] = {'level': 'red', 'hits': ['营收较基准下降 24%'], 'period': '2026/6'}
        rows[1]['fund'] = {'level': 'yellow', 'hits': ['毛利率下滑 3.4 个百分点'], 'period': '2026/6'}
        output = monitor._signal_counts_card({'signal_counts': monitor.signal_counts(rows),
                                              'signal_members': monitor.signal_count_members(rows)})
        red_panel = self.panel(output, 'fund', 'red')
        # 行首圆点就是灯，和 52周低位 / 布林+RSI 那几栏同一个位置同一个大小，不再单画
        self.assertIn('<li><a class="signal-member-jump" href="#s-aaa">', red_panel)
        self.assertNotIn('signal-member-dot', red_panel)
        self.assertNotIn('signal-member-fund', red_panel)
        self.assertNotIn('营收较基准下降 24%', red_panel)   # 命中项不在统计名单里复述
        self.assertNotIn('毛利率下滑 3.4 个百分点', self.panel(output, 'fund', 'yellow'))
        self.assertIn('#signalCounts .signal-members-panel.tone-red li::marker{color:var(--red)}', monitor.PAGE_CSS)
        self.assertIn('#signalCounts .earnings-week li.lamp-red::marker{color:var(--red)}', monitor.PAGE_CSS)

    def test_member_panels_are_toned_so_bullets_follow_the_light(self):
        """名单前的圆点靠面板上的 tone-* 上色：这一格红档，圆点就是红的。"""
        rows = [self.member_row('AAA', 'position', 'red', ('chg', 'red'), ('rsi', 'yellow')),
                self.member_row('BBB', 'focus', 'green', ('target', 'purple'))]
        output = monitor._signal_counts_card(monitor.build_snapshot({}, rows, {}, group_counts={}))
        self.assertIn('<div class="signal-members-panel tone-red" id="'
                      + self.panel_id('chg') + '-red" hidden>', output)
        self.assertIn('<div class="signal-members-panel tone-yellow" id="'
                      + self.panel_id('rsi') + '-yellow" hidden>', output)
        self.assertIn('<div class="signal-members-panel tone-purple" id="'
                      + self.panel_id('target') + '-purple" hidden>', output)
        self.assertIn('#signalCounts .signal-members-panel.tone-red li::marker{color:var(--red)}', monitor.PAGE_CSS)
        self.assertIn('#signalCounts .text-only .signal-member-list li::marker{color:#9fa3aa}', monitor.PAGE_CSS)

    def test_expanding_a_count_lights_only_that_count_capsule(self):
        """点开哪一格，就只亮那一格的数字胶囊（认 aria-expanded），不整行高亮。"""
        css = monitor.PAGE_CSS
        self.assertIn('#signalCounts .signal-count-toggle[aria-expanded="true"]', css)
        for dropped in ('tr.signal-open', 'tr.signal-picked'):
            self.assertNotIn(dropped, css)
            self.assertNotIn(dropped, monitor.PAGE_INTERACTION_JS)
        # 收起整块统计时把 aria-expanded 复位，胶囊的亮色跟着灭
        self.assertIn('button.setAttribute("aria-expanded", "false");', monitor.PAGE_INTERACTION_JS)

    @staticmethod
    def panel_id(kind):
        import hashlib
        return f'signal-members-{hashlib.sha256(kind.encode("utf-8")).hexdigest()}'

    def test_member_counts_match_every_kind_and_level(self):
        rows = [self.member_row('AAA', 'position', 'red', ('chg', 'red'), ('rsi', 'yellow')),
                self.member_row('BBB', 'focus', 'yellow', ('boll', 'yellow')),
                self.member_row('CCC', 'technology', 'yellow', ('boll', 'yellow'), ('vol_ratio', 'yellow')),
                self.member_row('DDD', 'focus', 'green', ('target', 'purple'))]
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(list(snap['signal_members']), [k for k, _ in monitor.SIGNAL_KINDS])
        for kind, counts in snap['signal_counts'].items():
            for level in ('red', 'yellow', 'purple'):
                with self.subTest(kind=kind, level=level):
                    self.assertEqual(len(snap['signal_members'][kind][level]), counts[level])
        output = monitor._signal_counts_card(snap)
        self.assertNotIn('明细待下次更新', output)
        for kind, level in (('chg', 'red'), ('rsi', 'yellow'), ('boll', 'yellow')):
            panel = self.panel(output, kind, level)
            self.assertNotRegex(panel, r'信号 · .*? 只|仅此一条黄灯 · \d+ 只')
        self.assertIn('signal-member-symbol">AAA', self.panel(output, 'rsi', 'yellow'))

    def test_member_icons_cover_single_indicators_cross_touch_and_target(self):
        rows = [
            self.member_row('UP', 'position', 'yellow', ('rsi', 'yellow'), ('boll', 'yellow'),
                            ('ma_cross', 'yellow'), ('ma_band', 'yellow'), ('target', 'purple')),
            self.member_row('DOWN', 'focus', 'red', ('rsi', 'red'), ('boll', 'yellow'),
                            ('ma_cross', 'yellow'), ('ma_band', 'yellow'), ('target', 'purple')),
            self.member_row('LEVEL', 'technology', 'yellow', ('ma_band', 'yellow')),
            self.member_row('SPACE', 'focus', 'green', ('target', 'purple')),
            self.member_row('CROSS', 'focus', 'green', ('target', 'purple')),
        ]
        rows[0].update(signals=['RSI 2条超买（≥70）：RSI6=80', '突破布林上轨 100',
                                '上穿60日均线', '日内触及120日均线 ±1%区间',
                                '在共识价 200 ±3% 内（偏离 +1%）'],
                       ma_band_direction='up')
        rows[1].update(signals=['RSI 3条超卖（≤30）：RSI6=20', '触及布林下轨 90',
                                '跌破120日均线', '现价贴近120日均线 -0.1%（仅按现价）',
                                '今日跌破目标价 90'], ma_band_direction='down')
        rows[2].update(signals=['日内触及120日均线 ±1%区间'], ma_band_direction='level')
        rows[3].update(signals=['共识价 200，空间 +55%'])
        rows[4].update(signals=['今日上穿共识价 200'])
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        cases = {
            ('rsi', 'yellow'): [('RSI2条超买', 'tone-yellow', '▲')],
            ('rsi', 'red'): [('RSI3条超卖', 'tone-red', '▼')],
            ('boll', 'yellow'): [('突破上轨', 'tone-yellow', '▲'), ('触及下轨', 'tone-white', '▼')],
            ('target', 'purple'): [('现价在目标价或共识价范围内', 'tone-purple', '■'),
                                   ('目标价或共识价上涨空间达到提示标准', 'tone-purple', '□'),
                                   ('当日上穿目标价或共识价', 'tone-purple', '▲'),
                                   ('当日跌破目标价或共识价', 'tone-purple', '▼')],
        }
        output = monitor._signal_counts_card(snap)
        for (kind, level), expected in cases.items():
            panel = self.panel(output, kind, level)
            self.assertNotRegex(panel, r'<p><strong>.*?信号.*?</strong></p>')
            for label, tone, shape in expected:
                with self.subTest(kind=kind, label=label):
                    self.assertRegex(panel, f'class="signal-member-icon {tone}"[^>]*aria-label="{re.escape(label)}[^\"]*"[^>]*>{shape}</span>')
        # 均线两栏改成跟量比一样的分框：周期和方向写在框标题上，行内不再逐个画三角
        for kind, titles in (('ma_cross', ['▲ 60日均线上穿', '▼ 120日均线跌破']),
                             ('ma_band', ['▲ 现价在均线上方，日内触及',
                                          '▼ 现价在均线下方，日内触及',
                                          '■ 现价与均线相等，日内触及'])):
            panel = self.panel(output, kind, 'yellow')
            with self.subTest(kind=kind):
                self.assertEqual(panel.count('class="signal-member-block"'), len(titles))
                for title in titles:
                    self.assertIn(f'<h5>{title}</h5>', panel)
                self.assertNotIn('signal-member-icon', panel)
        self.assertEqual(snap['signal_counts']['target']['purple'], 4)
        target = self.panel(output, 'target', 'purple')
        self.assertEqual(target.count('tone-purple'), 4)
        self.assertNotIn('▲ 上穿 · ▼ 跌破 · ■ 附近 · □ 空间达标', target)
        self.assertNotIn('signal-icon-key', output)
        self.assertNotIn('signal-member-help', output)
        self.assertNotIn('title=', output)  # 手机不用悬停，读屏仍能使用 aria-label

    def test_amplitude_and_volume_members_show_value_and_price_change_direction(self):
        rows = [self.member_row('UP', 'position', 'yellow', ('amp', 'yellow'), ('vol_ratio', 'yellow')),
                self.member_row('DOWN', 'focus', 'red', ('amp', 'red'), ('vol_ratio', 'yellow')),
                self.member_row('FLAT', 'position', 'yellow', ('amp', 'yellow'), ('vol_ratio', 'yellow'))]
        rows[0].update(chg=1.6, amp=5.37, vol_ratio=2.43,
                       vol_ratio_date='2026-10-13', data_date='2026-10-14')
        rows[1].update(chg=-3.2, amp=7.14, vol_ratio=1.61,
                       vol_ratio_date='2026-10-13', data_date='2026-10-14')
        rows[2].update(chg=0, amp=5.01, vol_ratio=1.52, vol_ratio_date='2026-10-13')
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(snap['signal_counts']['amp']['red'], 1)
        self.assertEqual(snap['signal_counts']['amp']['yellow'], 2)
        self.assertEqual(snap['signal_counts']['vol_ratio']['yellow'], 3)
        self.assertEqual(snap['signal_members']['amp']['red'][0]['metric'], '振幅 7.1%')
        self.assertEqual(snap['signal_members']['vol_ratio']['yellow'][0]['metric'],
                         '量比 2.4×（2026-10-13完整日）')
        output = monitor._signal_counts_card(snap)
        amp_yellow = self.panel(output, 'amp', 'yellow')
        amp_red = self.panel(output, 'amp', 'red')
        volume = self.panel(output, 'vol_ratio', 'yellow')
        # 量比的方向放到框标题上，名单里不再逐条画三角
        self.assertIn('tone-white" role="img" aria-label="2026-10-14股价上涨1.60%">▲', amp_yellow)
        self.assertIn('tone-white" role="img" aria-label="2026-10-14股价下跌3.20%">▼', amp_red)
        self.assertIn('<h5>▲ 上涨</h5>', volume)
        self.assertIn('<h5>▼ 下跌</h5>', volume)
        self.assertIn('<h5>平盘</h5>', volume)
        self.assertNotIn('signal-member-icon', volume)
        self.assertIn('振幅 5.4%', amp_yellow)
        self.assertIn('振幅 7.1%', amp_red)
        self.assertIn('量比 2.4×（2026-10-13完整日）', volume)
        self.assertIn('量比 1.6×（2026-10-13完整日）', volume)
        self.assertNotIn('量比数字取标注的完整交易日，不一定与股价三角同日', volume)
        self.assertNotIn('振幅为本轮行情日高低价之差占昨收的比例', amp_yellow)
        self.assertIn('tone-white" role="img" aria-label="2026-10-14股价下跌3.20%">▼', amp_red)
        self.assertNotIn('title=', output)

    def test_change_members_show_signed_percent_and_white_direction(self):
        rows = [self.member_row('UP', 'position', 'red', ('chg', 'red')),
                self.member_row('DOWN', 'focus', 'yellow', ('chg', 'yellow'))]
        rows[0].update(chg=6.237, data_date='2026-10-14')
        rows[1].update(chg=-1.45, data_date='2026-10-14')
        page = monitor._signal_counts_card(monitor.build_snapshot({}, rows, {}, group_counts={}))
        red, yellow = self.panel(page, 'chg', 'red'), self.panel(page, 'chg', 'yellow')
        self.assertIn('tone-white" role="img" aria-label="2026-10-14股价上涨6.24%">▲', red)
        self.assertIn('涨跌幅 +6.24%', red)
        self.assertIn('tone-white" role="img" aria-label="2026-10-14股价下跌1.45%">▼', yellow)
        self.assertIn('涨跌幅 -1.45%', yellow)
        self.assertNotIn('tone-red', red)
        self.assertNotIn('白▲ / 白▼', yellow)

    def test_boll_range_entry_is_white_and_intraday_break_is_yellow(self):
        cases = [('逼近布林上轨 还差0.4%', 'tone-white', '▲', '逼近上轨'),
                 ('触及布林上轨 100', 'tone-white', '▲', '触及上轨'),
                 ('盘中突破布林上轨 100（盘中触及 102）', 'tone-yellow', '▲', '盘中突破上轨'),
                 ('突破布林上轨 100', 'tone-yellow', '▲', '突破上轨'),
                 ('逼近布林下轨 还差0.4%', 'tone-white', '▼', '逼近下轨'),
                 ('触及布林下轨 90', 'tone-white', '▼', '触及下轨'),
                 ('跌破布林下轨 90（盘中触及 89）', 'tone-yellow', '▼', '盘中跌破下轨')]
        for text, tone, shape, name in cases:
            with self.subTest(text=text):
                row = self.member_row('BOLL', 'position', 'yellow', ('boll', 'yellow'))
                row.update(boll_alert_text=text, signals=[text])
                page = self.panel(monitor._signal_counts_card(monitor.build_snapshot({}, [row], {}, group_counts={})),
                                  'boll', 'yellow')
                self.assertIn(f'class="signal-member-icon {tone}" role="img" aria-label="{name}">{shape}', page)
                self.assertIn(f'<span class="signal-member-metric">{name}</span>', page)
                self.assertNotIn('tone-red', page)
        row = self.member_row('INFO', 'technology', 'green', ('boll', 'yellow'))
        row.update(boll_alert_text='', signals=['盘中突破布林上轨 100'])
        member = monitor.signal_count_members([row])['boll']['yellow'][0]
        self.assertNotIn('indicators', member)  # 其他组只有上轨说明，不能伪造黄灯图标

    def test_rsi_icon_tone_depends_on_hit_count_not_side_or_overall_lamp(self):
        rows = [self.member_row('BX', 'position', 'red', ('rsi', 'yellow'), ('chg', 'red')),
                self.member_row('TWO_HIGH', 'focus', 'yellow', ('rsi', 'yellow')),
                self.member_row('THREE_LOW', 'focus', 'red', ('rsi', 'red')),
                self.member_row('THREE_HIGH', 'technology', 'red', ('rsi', 'red'))]
        rows[0]['signals'] = ['RSI 2条超卖（≤30）：RSI6=22 / RSI12=27 / RSI24=33']
        rows[1]['signals'] = ['RSI 2条超买（≥70）：RSI6=81 / RSI12=72 / RSI24=67']
        rows[2]['signals'] = ['RSI 3条超卖（≤30）：RSI6=19 / RSI12=24 / RSI24=29']
        rows[3]['signals'] = ['RSI 3条超买（≥70）：RSI6=85 / RSI12=79 / RSI24=71']
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual((snap['signal_counts']['rsi']['yellow'], snap['signal_counts']['rsi']['red']), (2, 2))
        self.assertEqual(snap['signal_members']['rsi']['yellow'][0]['rsi_hits'], '2条超卖')
        output = monitor._signal_counts_card(snap)
        yellow = self.panel(output, 'rsi', 'yellow')
        red = self.panel(output, 'rsi', 'red')
        # 分组跟在代码后面，方向图标排在其后
        self.assertIn('>BX</span></a><span class="signal-member-belong">（短持仓）</span>'
                      '<span class="signal-member-icon tone-yellow" role="img" aria-label="RSI2条超卖">▼', yellow)
        self.assertIn('>TWO_HIGH</span></a><span class="signal-member-belong">（重点）</span>'
                      '<span class="signal-member-icon tone-yellow" role="img" aria-label="RSI2条超买">▲', yellow)
        self.assertIn('>THREE_LOW</span></a><span class="signal-member-belong">（重点）</span>'
                      '<span class="signal-member-icon tone-red" role="img" aria-label="RSI3条超卖">▼', red)
        self.assertIn('>THREE_HIGH</span></a><span class="signal-member-belong">（IT软硬Ai）</span>'
                      '<span class="signal-member-icon tone-red" role="img" aria-label="RSI3条超买">▲', red)
        self.assertIn('2条超卖', yellow)
        self.assertIn('3条超买', red)
        self.assertNotIn('2条过界为黄，3条过界为红', yellow)
        self.assertNotIn('tone-red" role="img" aria-label="RSI2条超卖"', yellow)

    def test_metric_fields_do_not_invent_values_from_old_or_invalid_snapshots(self):
        rows = [self.member_row('OLD', 'position', 'yellow', ('amp', 'yellow'), ('vol_ratio', 'yellow'),
                                ('rsi', 'yellow'))]
        rows[0].update(chg=float('nan'), amp=float('inf'), vol_ratio='<script>',
                       signals=[], vol_ratio_date='<script>')
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        page = monitor._signal_counts_card(snap)
        for kind in ('amp', 'vol_ratio'):
            panel = self.panel(page, kind, 'yellow')
            self.assertIn('数值待下次更新', panel)
            self.assertNotIn('<script>', panel)
        self.assertIn('方向待下次更新', self.panel(page, 'amp', 'yellow'))
        self.assertIn('<h5>平盘</h5>', self.panel(page, 'vol_ratio', 'yellow'))  # 量比方向不明归「平盘」框
        self.assertIn('条数待下次更新', self.panel(page, 'rsi', 'yellow'))

    def test_target_same_symbol_can_show_solid_and_hollow_squares_without_double_count(self):
        row = self.member_row('BOTH', 'focus', 'green', ('target', 'purple'), ('target', 'purple'))
        row['signals'] = ['在目标价 120 ±55% 内（偏离 -36.7%）', '目标价 120，空间 +58%']
        counts = monitor.signal_counts([row])
        members = monitor.signal_count_members([row])
        self.assertEqual(counts['target']['purple'], 1)
        self.assertEqual(len(members['target']['purple']), 1)
        output = self.panel(monitor._signal_counts_card({'signal_counts': counts,
                            'signal_members': members}), 'target', 'purple')
        self.assertIn('tone-purple" role="img" aria-label="现价在目标价或共识价范围内">■', output)
        self.assertIn('tone-purple" role="img" aria-label="目标价或共识价上涨空间达到提示标准">□', output)
        self.assertNotIn('title=', output)
        self.assertEqual(output.count('signal-member-icon'), 2)

    def test_old_snapshot_missing_icons_does_not_infer_direction(self):
        row = self.member_row('OLDER', 'position', 'yellow', ('rsi', 'yellow'), ('boll', 'yellow'))
        snap = monitor.build_snapshot({}, [row], {}, group_counts={})
        output = monitor._signal_counts_card(snap)
        for kind in ('rsi', 'boll'):
            panel = self.panel(output, kind, 'yellow')
            self.assertIn('方向待下次更新', panel)
            self.assertNotIn('signal-member-icon', panel)

    def test_icon_data_is_whitelisted_and_escaped(self):
        counts = {'rsi': {'label': 'RSI', 'yellow': 1}}
        member = {'symbol': 'X', 'note': '', 'group': 'focus', 'indicators': [
            {'shape': '▲', 'tone': 'yellow', 'label': '<img src=x onerror=alert(1)>'},
            {'shape': '<svg>', 'tone': 'red', 'label': 'bad'},
            {'shape': '□', 'tone': 'purple', 'label': '只允许目标价空间使用'},
            {'shape': '■', 'tone': '\" onclick=alert(1)', 'label': 'bad'},
        ]}
        panel = self.panel(monitor._signal_counts_card({'signal_counts': counts,
            'signal_members': {'rsi': {'yellow': [member]}}}), 'rsi', 'yellow')
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', panel)
        self.assertNotIn('<img ', panel)
        self.assertNotIn('<svg>', panel)
        self.assertNotIn('onclick=', panel)
        self.assertEqual(panel.count('signal-member-icon'), 1)

    def test_combined_red_members_show_direction_without_guessing_old_snapshot(self):
        rows = [self.member_row('UP', 'position', 'red', ('boll_rsi', 'red'), note='上方'),
                self.member_row('DOWN', 'focus', 'red', ('boll_rsi', 'red')),
                self.member_row('LEGACY', 'technology', 'red', ('boll_rsi', 'red')),
                self.member_row('CONFLICT', 'energy', 'red', ('boll_rsi', 'red')),
                self.member_row('MILD', 'healthcare', 'yellow', ('boll_rsi', 'yellow'))]
        rows[0].update(boll_rsi_direction='上轨（超买）', boll_rsi_hits=2)
        rows[1].update(boll_rsi_direction='下轨（超卖）', boll_rsi_hits=3)
        rows[3].update(boll_rsi_direction='上轨（RSI超卖，方向不一致）', boll_rsi_hits=3)
        rows[4].update(boll_rsi_direction='下轨（超卖）', boll_rsi_hits=2)
        counts = monitor.signal_counts(rows)
        members = monitor.signal_count_members(rows)
        self.assertEqual(counts['boll_rsi']['red'], 4)
        self.assertEqual(len(members['boll_rsi']['red']), 4)
        output = self.panel(monitor._signal_counts_card({'signal_counts': counts,
                                                            'signal_members': members}), 'boll_rsi', 'red')
        # 轨道 + RSI 条数写在框标题上，名单里不再逐条画三角
        for label in ('<h5>▲ 上轨 · RSI 2条过界</h5>', '<h5>▼ 下轨 · RSI 3条过界</h5>',
                      '<h5>▲ 上轨 · RSI 3条过界 · 方向不一致</h5>', '<h5>方向待下次更新</h5>'):
            self.assertIn(label, output)
        # 黄档同样分框
        yellow = self.panel(monitor._signal_counts_card({'signal_counts': counts,
                                                         'signal_members': members}), 'boll_rsi', 'yellow')
        self.assertIn('<h5>▼ 下轨 · RSI 2条过界</h5>', yellow)
        self.assertIn('RSI 2条过界', output)
        self.assertIn('RSI 3条过界', output)
        self.assertIn('>UP</span><span class="note">上方</span></a>', output)  # 备注名回到代码后面
        self.assertIn('class="signal-member-jump"', output)  # 名字可点跳转，不再写分组名
        # 方向全在框标题上：名单行里既没有三角图标，也没有方向文字
        self.assertEqual(output.count('class="signal-member-direction"'), 0)
        self.assertEqual(output.count('class="signal-member-icon'), 0)

    def test_combination_counts_follow_matching_rsi_side_but_red_lamp_stays_unchanged(self):
        # 三角按轨道方向画，颜色跟灯级走：红档红、黄档黄（方向不一致 3 条也是黄）
        cases = [('red', '上轨（超买）', 2, 'tone-red', '▲'),
                 ('red', '上轨（超买）', 3, 'tone-red', '▲'),
                 ('red', '下轨（超卖）', 2, 'tone-red', '▼'),
                 ('red', '下轨（超卖）', 3, 'tone-red', '▼'),
                 ('yellow', '上轨（超买）', 2, 'tone-yellow', '▲'),
                 ('yellow', '下轨（超卖）', 2, 'tone-yellow', '▼'),
                 ('yellow', '下轨（RSI超买，方向不一致）', 3, 'tone-yellow', '▼')]
        for level, direction, hits, tone, shape in cases:
            with self.subTest(level=level, direction=direction, hits=hits):
                row = self.member_row('MATCH', 'position', level, ('boll_rsi', level))
                row.update(boll_rsi_direction=direction, boll_rsi_hits=hits)
                snap = monitor.build_snapshot({}, [row], {}, group_counts={})
                page = self.panel(monitor._signal_counts_card(snap), 'boll_rsi', level)
                tail = ' · 方向不一致' if '不一致' in direction else ''
                head = ('上轨' if direction.startswith('上轨') else '下轨') + f' · RSI {hits}条过界' + tail
                self.assertIn(f'<h5>{shape} {head}</h5>', page)   # 方向 + 条数写在框标题上
                self.assertIn(f'RSI {hits}条过界', page)
                self.assertIn('上轨' if direction.startswith('上轨') else '下轨', page)  # 轨道方向要可见
                self.assertNotIn('signal-member-icon', page)      # 名单里不再逐条画三角
                self.assertEqual(snap['signal_counts']['boll_rsi'][level], 1)
        conflict = self.member_row('CONFLICT', 'position', 'red', ('boll_rsi', 'red'))
        conflict.update(boll_rsi_direction='下轨（RSI超买，方向不一致）', boll_rsi_hits=3)
        page = self.panel(monitor._signal_counts_card(monitor.build_snapshot({}, [conflict], {}, group_counts={})),
                          'boll_rsi', 'red')
        self.assertIn('<h5>▼ 下轨 · RSI 3条过界 · 方向不一致</h5>', page)  # 轨道方向明确，照样分框
        legacy = dict(conflict, boll_rsi_direction='上轨（超买）')
        legacy.pop('boll_rsi_hits')
        page = self.panel(monitor._signal_counts_card(monitor.build_snapshot({}, [legacy], {}, group_counts={})),
                          'boll_rsi', 'red')
        self.assertIn('<h5>方向待下次更新</h5>', page)
        self.assertNotIn('class="signal-member-icon', page)
        # 框标题已经说了「方向待下次更新」，行尾不再重复同一句
        self.assertNotIn('条数待下次更新', page)

    def test_ma_panels_split_up_and_down_with_group_labels_on_every_member(self):
        """均线穿越 / 长均线触及跟量比一样按方向分框：周期与方向写在框标题上，
        行内不再逐个重复；每只标的后面用括号标出分组，持仓再分长 / 短。"""
        rows = [self.member_row('CROSSUP', 'position', 'yellow', ('ma_cross', 'yellow')),
                self.member_row('CROSSDN', 'focus', 'yellow', ('ma_cross', 'yellow')),
                self.member_row('TOUCHUP', 'position', 'yellow', ('ma_band', 'yellow')),
                self.member_row('TOUCHDN', 'financials', 'yellow', ('ma_band', 'yellow'))]
        rows[0].update(signals=['上穿250日均线'], hold='long')
        rows[1].update(signals=['跌破120日均线'])
        rows[2].update(signals=['日内触及250日均线 ±1%区间'], ma_band_direction='up', hold='short')
        rows[3].update(signals=['日内触及250日均线 ±1%区间'], ma_band_direction='down')
        output = monitor._signal_counts_card({'signal_counts': monitor.signal_counts(rows),
                                              'signal_members': monitor.signal_count_members(rows)})
        cross = self.panel(output, 'ma_cross', 'yellow')
        self.assertIn('<h5>▲ 250日均线上穿</h5>', cross)
        self.assertIn('<h5>▼ 120日均线跌破</h5>', cross)
        self.assertNotIn('signal-member-icon', cross)   # 方向已经写在框标题上
        self.assertIn('<span class="signal-member-belong">（长持仓）</span>', cross)
        self.assertIn('<span class="signal-member-belong">（重点）</span>', cross)
        band = self.panel(output, 'ma_band', 'yellow')
        self.assertIn('<h5>▲ 现价在均线上方，日内触及</h5>', band)
        self.assertIn('<h5>▼ 现价在均线下方，日内触及</h5>', band)
        self.assertIn('<span class="signal-member-belong">（短持仓）</span>', band)
        self.assertIn('<span class="signal-member-belong">（金融）</span>', band)
        self.assertNotIn('signal-member-icon', band)

    def test_boll_rsi_states_the_direction_only_in_the_block_title(self):
        """布林+RSI 的框标题已经写了「下轨 · RSI 2条过界」，行尾不再重复同一句。"""
        row = self.member_row('BOTH', 'position', 'yellow', ('boll_rsi', 'yellow'))
        row.update(boll_rsi_direction='下轨（超卖）', boll_rsi_hits=2, hold='long')
        output = monitor._signal_counts_card({'signal_counts': monitor.signal_counts([row]),
                                              'signal_members': monitor.signal_count_members([row])})
        page = self.panel(output, 'boll_rsi', 'yellow')
        self.assertIn('<h5>▼ 下轨 · RSI 2条过界</h5>', page)
        self.assertEqual(page.count('RSI 2条过界'), 1)   # 只有框标题一处
        self.assertIn('<span class="signal-member-belong">（长持仓）</span>', page)

    def test_members_deduplicate_marks_not_cross_kind_or_level(self):
        row = self.member_row('AAA', 'position', 'red', ('rsi', 'red'), ('rsi', 'red'),
                              ('rsi', 'yellow'), ('rsi', 'yellow'), ('boll', 'yellow'))
        members = monitor.signal_count_members([row])
        counts = monitor.signal_counts([row])
        for kind, level in (('rsi', 'red'), ('rsi', 'yellow'), ('boll', 'yellow')):
            self.assertEqual(members[kind][level], [{'symbol': 'AAA', 'note': '', 'group': 'position',
                                                     'hold': '', 'level': 'red'}])
            self.assertEqual(counts[kind][level], 1)
        output = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
        self.assertEqual(output.count('class="signal-member-symbol">AAA'), 3)
        self.assertNotIn('各计数不能相加当作唯一标的总数', output)
        self.assertNotIn('不代表标的综合灯色', output)

    def test_text_only_members_cover_text_signals_without_a_light(self):
        """灰灯 = 最终是绿灯、但页面上有 RSI / 布林文字的标的：不进红黄紫计数，
        按归因分类（只有 RSI 文字→rsi，只有布林文字→boll，两者都有→boll_rsi）。"""
        rows = [{'symbol': 'RSIONLY', 'group': 'position', 'level': 'green',
                 'signal_marks': [], 'signals': ['RSI 2条超买（≥70）']},
                {'symbol': 'BOLLONLY', 'group': 'focus', 'level': 'green',
                 'signal_marks': [], 'signals': ['逼近布林下轨 还差0.30%']},
                {'symbol': 'BOTH', 'group': 'technology', 'level': 'green',
                 'signal_marks': [], 'signals': ['RSI 2条超买（≥70）', '触及布林上轨']},
                {'symbol': 'LIT', 'group': 'position', 'level': 'yellow',
                 'signal_marks': [('boll', 'yellow')], 'signals': ['逼近布林下轨 还差0.10%']},
                {'symbol': 'QUIET', 'group': 'technology', 'level': 'green',
                 'signal_marks': [], 'signals': []}]
        members = monitor.text_only_members(rows)
        self.assertEqual([(m['symbol'], m['kind'], m['shape']) for m in members],
                         [('RSIONLY', 'rsi', '▲'), ('BOLLONLY', 'boll', '▼'), ('BOTH', 'boll_rsi', '▲')])
        # 灰灯三行不进红黄紫计数；LIT 行是真黄灯，本来就该计进 boll.yellow
        counts = monitor.signal_counts(rows)
        self.assertEqual([(k, v['yellow']) for k, v in counts.items() if v['yellow']], [('boll', 1)])
        output = monitor._signal_counts_card({'signal_counts': counts,
                                              'signal_members': monitor.signal_count_members(rows),
                                              'text_only': members})
        self.assertNotIn('仅此一条黄灯', output)
        self.assertIn('灰灯 3 只', output)
        # 默认折叠，展开后按归因分类，每组先 ▲ 后 ▼
        self.assertIn('class="text-only"', output)
        self.assertLess(output.index('RSI · 1 只'), output.index('布林 · 1 只'))
        self.assertIn('signal-member-jump', output)
        self.assertNotIn('(持仓)', output)
        self.assertNotIn('(IT软硬Ai)', output)

    def test_purple_only_and_gray_members_keep_original_levels(self):
        rows = [self.member_row('PURPLE', 'focus', 'green', ('target', 'purple'), ('target', 'purple')),
                self.member_row('GRAY', 'position', 'gray', ('target', 'purple'))]
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(snap['signal_counts']['target']['purple'], 2)
        self.assertEqual((snap['summary']['green'], snap['summary']['gray'], snap['summary']['yellow']), (1, 1, 0))
        output = monitor._signal_counts_card(snap)
        panel = self.panel(output, 'target', 'purple')
        self.assertNotIn('紫色提示 · 2 只', panel)
        self.assertIn('>GRAY</span>', panel)
        self.assertIn('>PURPLE</span>', panel)
        self.assertEqual(output.count('class="signal-count-toggle"'), 1)
        # 只有目标价行有计数：红、黄两格为空
        self.assertEqual(output.count('<td class="num">—</td>'), 2)
        self.assertNotIn('不改变综合灯色', output)

    def test_groups_use_item_group_order_and_show_group_sizes(self):
        rows = [self.member_row(f'GROUP{i}', group['item_group'], 'red', ('chg', 'red'), note=f'备注{i}')
                for i, group in reversed(list(enumerate(monitor.GROUPS)))]
        counts = monitor.signal_counts(rows)
        members = monitor.signal_count_members(rows)
        self.assertEqual([d['group'] for d in members['chg']['red']],
                         [g['item_group'] for g in monitor.GROUPS])
        # 成员去重仍按 GROUPS 优先级；乱序快照的明细标题按 _display_groups 展示顺序。
        members['chg']['red'].reverse()
        members['chg']['red'].append({'symbol': 'EXTRA', 'group': 'position', 'note': '同组备注'})
        counts['chg']['red'] += 1
        output = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
        panel = self.panel(output, 'chg', 'red')
        # 明细不再按分组切块：没有小标题、只有一个列表
        self.assertEqual(re.findall(r'<h4>(.*?)</h4>', panel), [])
        self.assertEqual(panel.count('class="signal-member-group"'), 0)
        self.assertEqual(panel.count('class="signal-member-list"'), 1)
        self.assertEqual(panel.count('class="signal-member-symbol"'), len(rows) + 1)
        # 中文备注名保留在代码后面，分组名仍然不写
        self.assertIn('<span class="note">同组备注</span>', panel)
        self.assertIn('class="signal-member-jump"', panel)   # 名单不再写分组名，改点名字跳转
        self.assertNotIn(f'红色信号 · {len(rows) + 1} 只', panel)

    def test_same_identity_keeps_priority_group_and_first_note(self):
        rows = [self.member_row('BRK-B', 'technology', 'yellow', ('boll', 'yellow'), note='行业备注'),
                self.member_row('BRK.B', 'focus', 'yellow', ('rsi', 'yellow'), note='关注备注'),
                self.member_row('BRK.B-US', 'position', 'yellow', ('ma_cross', 'yellow'), note='持仓备注'),
                self.member_row('BRK-B', 'position', 'yellow', ('chg', 'yellow'), note='后来备注')]
        members = monitor.signal_count_members(rows)
        expected = [{'symbol': 'BRK-B', 'note': '持仓备注', 'group': 'position', 'hold': '',
                     'level': 'yellow'}]
        self.assertEqual(members['ma_cross']['yellow'], expected)
        for kind in ('boll', 'rsi', 'chg'):
            self.assertEqual(members[kind]['yellow'], [])
        # 旧统计逐行计算，不为了成员身份去重而改变统计口径。
        counts = monitor.signal_counts(rows)
        self.assertEqual([counts[k]['yellow'] for k in ('boll', 'rsi', 'ma_cross', 'chg')], [1, 1, 1, 1])
        output = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
        self.assertIn('class="signal-member-jump"', self.panel(output, 'ma_cross', 'yellow'))
        self.assertNotIn('行业备注', output)
        self.assertIn('明细待下次更新', self.panel(output, 'boll', 'yellow'))

    def test_every_nonzero_cell_has_accessible_hidden_adjacent_panel(self):
        rows = [self.member_row('AAA', 'position', 'red', ('rsi', 'red'), ('rsi', 'yellow'), ('target', 'purple')),
                self.member_row('BBB', 'focus', 'yellow', ('rsi', 'yellow'))]
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        output = monitor._signal_counts_card(snap)
        buttons = re.findall(r'<button type="button" class="signal-count-toggle" data-target="([^"]+)" '
                             r'aria-controls="([^"]+)" aria-expanded="false" aria-label="([^"]+)">(\d+)</button>', output)
        expected_buttons = sum(bool(v[level]) for v in snap['signal_counts'].values()
                               for level in ('red', 'yellow', 'purple'))
        self.assertEqual(len(buttons), expected_buttons)
        self.assertEqual(len({b[0] for b in buttons}), expected_buttons)
        panels = re.findall(r'<div class="signal-members-panel tone-\w+" id="([^"]+)" hidden>', output)
        self.assertEqual([b[0] for b in buttons], panels)
        for target, controls, label, n in buttons:
            self.assertEqual(target, controls)
            self.assertIn(f'{n} 只', label)
        # 只有有计数的行才出现（RSI 行 + 目标价行）
        pairs = re.findall(r'<tr><td>.*?</tr><tr class="signal-members-row"><td colspan="4">.*?</td></tr>', output, re.S)
        self.assertEqual(len(pairs), 2)
        self.assertIn('button.closest("tr")', monitor.PAGE_INTERACTION_JS)
        self.assertIn('scope.querySelectorAll(".signal-count-toggle[data-target]")', monitor.PAGE_INTERACTION_JS)
        # 2026-10-15：展开范围受设置控制——默认关掉整块统计面板里已打开的，关掉开关只收同一行的
        self.assertIn('window.MM_SETTINGS && window.MM_SETTINGS.signal_single_open === 0', monitor.PAGE_INTERACTION_JS)
        self.assertIn('const scope = (singleOpen && button.closest("#signalCounts")) || owner;',
                      monitor.PAGE_INTERACTION_JS)
        self.assertIn('panel.hidden = !opening', monitor.PAGE_INTERACTION_JS)
        self.assertIn('button.setAttribute("aria-expanded", String(opening))', monitor.PAGE_INTERACTION_JS)
        self.assertIn('.signal-members-panel[hidden]{display:none}', monitor.PAGE_CSS)

    def test_panel_ids_are_stable_unique_and_do_not_embed_user_kind(self):
        kind = '\"><script>alert(1)</script>'
        member = {'symbol': 'AAA', 'note': '', 'group': 'position'}
        counts = {kind: {'label': '自定义', 'red': 1}, 'safe': {'label': '另一类', 'red': 1}}
        members = {k: {'red': [member]} for k in counts}
        output = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
        reverse = monitor._signal_counts_card({'signal_counts': dict(reversed(list(counts.items()))),
                                               'signal_members': members})
        ids = re.findall(r'id="(signal-members-[^"]+)"', output)
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(set(ids), set(re.findall(r'id="(signal-members-[^"]+)"', reverse)))
        self.assertTrue(all(re.fullmatch(r'signal-members-[0-9a-f]{64}-red', value) for value in ids))
        self.assertNotIn(kind, output)
        self.assertNotIn('<script>', output)

    def test_html_escapes_labels_groups_symbols_notes_and_summary(self):
        import html
        label = '<img src=x onerror="alert(1)"> & \'信号\''
        group = '<svg onload="alert(2)"> & \'分类\''
        symbol = '<script>alert("代码")</script>&'
        note = '<img src=x onerror="alert(3)"> & \'备注\''
        group_label = '<b onclick="alert(4)">持仓</b>&'
        entries = [{'symbol': symbol, 'note': note, 'group': 'position'},
                   {'symbol': 'BBB', 'note': '', 'group': group},
                   {'symbol': 'CCC', 'note': '', 'group': ''}]
        groups = [dict(g, label=group_label) if g['item_group'] == 'position' else g for g in monitor.GROUPS]
        with patch.object(monitor, 'GROUPS', groups):
            output = monitor._signal_counts_card({'signal_counts': {'chg': {'label': label, 'red': 3}},
                                                  'signal_members': {'chg': {'red': entries}},
                                                  'summary': {'yellow': '<svg onload="alert(5)">'}})
        for value in (label, symbol):
            self.assertIn(html.escape(value, quote=True), output)
            self.assertNotIn(value, output)
        # 分组名 / 分组标签不再写进可见名单，只能出现在 data-filter 的 JSON 里（经 JSON + HTML 双重编码）
        for value in (group, group_label):
            self.assertNotIn(value, output)
            self.assertNotIn(value, html.unescape(output.split('data-filter="')[1].split('"', 1)[0]))
        self.assertNotIn('alert(2)', output.split('data-filter=')[0])
        # 备注名回到名单里，但必须完整转义，不能变成真的标签
        self.assertIn(html.escape(note, quote=True), output.split('data-filter=')[0])
        self.assertNotIn('alert(5)', output)  # 统计标题只接受非负整数，异常值显示 —
        self.assertIn('<span class="count-total count-yellow">黄 —</span>', output)
        self.assertNotRegex(output, r'<(?:script|img|svg)\b|<b onclick=')
        self.assertNotIn('<h4>', output)
        self.assertEqual(output.count('class="note"'), 1)
        # 分组回到名单里，且必须完整转义（不能变成真的标签）。
        # 持仓组写「长持仓 / 短持仓」而不是配置里的分组名，所以原始标签不出现在可见部分。
        visible = output.split('data-filter=')[0]
        self.assertIn('class="signal-member-belong"', visible)
        self.assertIn('（短持仓）', visible)
        self.assertNotIn(group_label, visible)

    def test_old_snapshot_keeps_counts_but_does_not_invent_members(self):
        rows = [self.member_row('SECRET', 'position', 'yellow', ('boll', 'yellow'), note='不可拼名单')]
        snap = {'signal_counts': monitor.signal_counts(rows), 'items': rows}
        output = monitor._signal_counts_card(snap)
        self.assertEqual(output.count('class="signal-count-toggle"'), 1)
        for level in ('yellow',):
            panel = self.panel(output, 'boll', level)
            self.assertIn('明细待下次更新', panel)
            self.assertNotIn('布林 · ', panel)
        self.assertNotIn('SECRET', output)
        self.assertNotIn('不可拼名单', output)
        self.assertNotIn('class="signal-member-symbol"', output)

    def test_member_count_mismatch_and_duplicate_identity_degrade_locally(self):
        counts = {'chg': {'label': '涨跌幅', 'red': 2}, 'target': {'label': '目标价', 'purple': 1}}
        good = {'symbol': 'GOOD', 'note': '有效备注', 'group': 'focus'}
        invalid_lists = (None, [], [{'symbol': 'ONE', 'note': '不可信', 'group': 'position'}],
                         [{'symbol': 'A'}, {'symbol': 'B'}, {'symbol': 'C'}],
                         [{'symbol': 'BRK.B'}, {'symbol': 'BRK-B'}],
                         [{'symbol': ''}, {'symbol': 'B'}], ['A', 'B'])
        for entries in invalid_lists:
            with self.subTest(entries=entries):
                output = monitor._signal_counts_card({'signal_counts': counts,
                           'signal_members': {'chg': {'red': entries}, 'target': {'purple': [good]}}})
                panel = self.panel(output, 'chg', 'red')
                self.assertNotIn('红色信号 · 2 只', panel)
                self.assertIn('明细待下次更新', panel)
                self.assertNotIn('class="signal-member-symbol"', panel)
                self.assertNotIn('不可信', output)
                target = self.panel(output, 'target', 'purple')
                self.assertIn('>GOOD</span>', target)
                self.assertNotIn('明细待下次更新', target)

    def test_zero_counts_hide_card_and_nonzero_still_expands(self):
        # 整块统计只在有计数时出现：旧快照、空对象、全 0 都不显示
        for snap in ({}, {'signal_counts': {}}, {'signal_counts': monitor.signal_counts([])}):
            self.assertEqual(monitor._signal_counts_card(snap), '')
        output = monitor._signal_counts_card({'signal_counts': {'boll': {'label': '布林', 'yellow': 1}}})
        self.assertEqual(output.count('class="signal-count-toggle"'), 1)
        self.assertEqual(output.count('<td class="num">—</td>'), 2)
        self.assertIn('明细待下次更新', self.panel(output, 'boll', 'yellow'))


if __name__ == '__main__':
    unittest.main()
