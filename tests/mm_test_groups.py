"""离线分类模型、去重、备注及全部折叠标题回归。"""
import json
import re
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import monitor


def _row_signals(row):
    """主表只输出各指标描述，级别通过 data-signal-level 表达，不添加圆点。"""
    return monitor._signals_html(row)


def plain_text(html):
    """去掉标签与方向符号配色 span，只留可见文字，便于断言统一文案。"""
    return re.sub(r'<[^>]+>', '', str(html))

# 测试一律用代码里的默认设置，不读线上 settings.json（线上值一改，测试就会无故失败）
monitor.S.clear()
monitor.S.update(monitor.DEFAULT_SETTINGS)
# 财报日的外部来源默认全关，任何用例都不会真的联网；专项测试自己打开并用 mock 注入结果。
# 用普通属性而不是 patch.start()：TestGroups.setUp 里的 patch.stopall() 会把模块级 patch 一起停掉。
monitor.EARN_CROSS_CHECK = False    # 第三来源（Yahoo）
monitor.EARN_SA_PRIMARY = False     # 主源（stockanalysis）


class TestPriceDecimalBoundaries(unittest.TestCase):
    def check_price(self, kind, target, price, threshold, expected):
        from types import SimpleNamespace
        field = 'trigger' if kind == 'add_price' else 'reduce'
        setting = 'trigger_gap_pct' if kind == 'add_price' else 'reduce_gap_pct'
        rule = monitor._rule_add_price if kind == 'add_price' else monitor._rule_reduce_price
        alerts = monitor._Alerts()
        context = SimpleNamespace(cfg={field: target}, price=price, group='position')
        with patch.dict(monitor.S, {setting: threshold}):
            rule(alerts, context)
        self.assertEqual(alerts.marks, [(kind, 'red')] if expected else [])
        self.assertEqual(alerts.level, 'red' if expected else 'green')

    def test_three_percent_exact_boundary_and_adjacent_values(self):
        for kind, cases in (
            ('add_price', ((102.999999999999, True), (103, True), (103.000000000001, False))),
            ('reduce_price', ((97.000000000001, True), (97, True), (96.999999999999, False))),
        ):
            for price, expected in cases:
                with self.subTest(kind=kind, price=price):
                    self.check_price(kind, 100, price, 3, expected)

    def test_noninteger_price_target_and_threshold_boundaries(self):
        for kind, cases in (
            ('add_price', ((12.658749999999, True), (12.65875, True), (12.658750000001, False))),
            ('reduce_price', ((12.041250000001, True), (12.04125, True), (12.041249999999, False))),
        ):
            for price, expected in cases:
                with self.subTest(kind=kind, price=price):
                    self.check_price(kind, 12.35, price, 2.5, expected)

    def test_zero_threshold_only_accepts_equal_or_crossed_prices(self):
        for target, below, above in ((100, 99.999999999999, 100.000000000001),
                                     (12.35, 12.349999999999, 12.350000000001),
                                     (1e-7, 0.999999999999e-7, 1.000000000001e-7)):
            for kind in ('add_price', 'reduce_price'):
                for price in (below, target, above):
                    with self.subTest(kind=kind, target=target, price=price):
                        self.check_price(kind, target, price, 0,
                                         price <= target if kind == 'add_price' else price >= target)

    def test_small_decimal_threshold_and_scientific_notation_do_not_expand(self):
        for kind, cases in (
            ('add_price', ((1.000099999999e-7, True), (1.0001e-7, True), (1.000100000001e-7, False))),
            ('reduce_price', ((0.999900000001e-7, True), (0.9999e-7, True), (0.999899999999e-7, False))),
        ):
            for price, expected in cases:
                with self.subTest(kind=kind, price=price):
                    self.check_price(kind, 1e-7, price, 0.01, expected)

    def test_analysis_includes_only_the_expected_price_mark_at_boundary(self):
        for field, price in (('trigger', 103), ('reduce', 97)):
            for hold in ('short', 'long'):
                closes = [100.0] * 59 + [price]
                data = dict(closes=closes, highs=closes[:], lows=closes[:], volumes=[1000] * 60,
                            price=price, prev_close=100, dates=['2026-10-08'] * 60)
                with self.subTest(field=field, hold=hold), \
                        patch.object(monitor, 'S', dict(monitor.DEFAULT_SETTINGS, trigger_gap_pct=3,
                            reduce_gap_pct=3, near_52w_low_pct=0, ma_short=0, ma_long=0)), \
                        patch.object(monitor, 'calc_rsi', return_value=50), \
                        patch.object(monitor, 'boll', return_value=(None, None, None)):
                    row = monitor.analyze_symbol('TEST', {field: 100, 'hold': hold}, data, 'position')[2]
                kind = 'add_price' if field == 'trigger' else 'reduce_price'
                self.assertIn((kind, 'red'), row['signal_marks'])
                self.assertTrue(row['foldable'])
                self.assertIn('data-quiet-keep="false"', monitor._stock_row(row, {}))


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
                self.assertIn('具体时段待定', monitor.earnings_label(e)[0])
                self.assertNotRegex(monitor._earnings_html({'earnings': e}), r'盘前|盘后|盘中|\d{1,2}:\d{2}')
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
        # 财报列不写来源，未知时段与复查状态仍如实保留。
        self.assertNotIn('官方来源', html)
        self.assertNotIn('<a ', html)
        self.assertIn('北京 待定 · 待复核', html)
        self.assertNotIn('官方', html)
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

    def test_removed_index_entry_leaves_universe_and_snapshot_counts(self):
        cfg = {'index_funds': {'ESMAIN': {'note': '期货'}, 'IBIT': {'note': 'ETF'}},
               'group_monitoring': {'index_funds': True}}
        before, _, _ = monitor.grouped_universe(cfg)
        self.assertIn('ESMAIN', [symbol for symbol, _, _ in before])
        cfg['index_funds'].pop('ESMAIN')
        universe, counts, _ = monitor.grouped_universe(cfg)
        self.assertEqual([symbol for symbol, _, _ in universe], ['IBIT'])
        rows = [{'symbol': symbol, 'group': group, 'price': 100, 'prev_close': 100,
                 'level': 'green', 'signals': [], 'signal_marks': [], 'data_date': '2030-10-11'}
                for symbol, _, group in universe]
        with patch.object(monitor, 'global_dca', return_value=None), \
                patch.object(monitor, '_bj_today', return_value='2030-10-11'):
            snap = monitor.build_snapshot({}, rows, cfg, group_counts=counts)
        self.assertEqual(snap['summary']['total'], 1)
        self.assertEqual(snap['list_counts']['index_funds'], 1)
        self.assertEqual(snap['registered_counts']['index_funds'], 1)
        self.assertNotIn('ESMAIN', json.dumps(snap['signal_members']))
        self.assertNotIn('ESMAIN', json.dumps(snap['text_only']))

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
        self.assertNotIn('个股行情 2/2 已更新', page)
        run_text = re.search(r'<span id="runLightTxt">(.*?)</span>', page, re.S)[1]
        self.assertIn('市场风险数据缺失', run_text)
        self.assertNotIn('抓取成功', run_text)
        self.assertIn('id="runLight" data-phase="partial"', page)
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
        self.assertNotIn('XLB 仍监测', page)
        self.assertNotIn('行业监测关闭', page)
        self.assertNotIn('清单保留，普通标的暂停抓取和报警', page)
        self.assertIn('异动测试', page)
        self.assertNotIn('DD</td>', page)

    def test_sector_note_sources_and_rsi_glossary_are_concise(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        names = json.loads((root / 'symbol-names.json').read_text(encoding='utf-8'))
        holdings = json.loads((root / 'holdings.json').read_text(encoding='utf-8'))
        expected = {'XLK': '科技行业', 'XLV': '医疗保健行业', 'XLF': '金融行业',
                    'XLE': '能源行业', 'XLI': '工业行业', 'XLB': '原材料行业',
                    'XLU': '公用事业行业', 'XLP': '必需消费行业', 'XLC': '通信服务行业',
                    'XLY': '非必需消费行业', 'XLRE': '房地产行业'}
        for symbol, label in expected.items():
            self.assertEqual(names[symbol], label)
        for group in monitor.GROUPS:
            for symbol, entry in holdings.get(group['key'], {}).items():
                if symbol in expected:
                    self.assertEqual(entry['note'], expected[symbol])
        self.assertNotIn('精选行业ETF', json.dumps(names, ensure_ascii=False))
        self.assertNotIn('精选行业ETF', json.dumps(holdings, ensure_ascii=False))
        page = monitor.render({}, [], 0, snapshot=monitor.build_snapshot({}, [], {}))
        self.assertNotIn('同方向达标的周期数', page)
        self.assertNotIn('不等于今日涨跌', page)
        self.assertIn('RSI', page)

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
                    patch.object(monitor, 'build_macro', side_effect=lambda *args: (monitor.SP500_RS_ROWS.extend(bench_rows) or {})), \
                    patch.object(monitor, 'SP500_RS_ROWS', []), \
                    patch.object(monitor, 'sp500_rows_fallback', return_value=list(fallback_rows or [])) as fallback, \
                    patch.object(monitor, 'fetch_history', side_effect=hist), \
                    patch.object(monitor, 'analyze_symbol', side_effect=analyzed), \
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

    def test_earnings_urgency_uses_beijing_days(self):
        """直接用美东日期 D 与北京今天比较：D-1/D/D+1 红，D-7..D-2 黄。"""
        from datetime import date
        today = '2026-10-20'
        for timing in ('pre', 'post', 'during', '', 'unknown'):
            for gap, urgency in ((-2, ''), (-1, 'red'), (0, 'red'), (1, 'red'),
                                 (2, 'yellow'), (6, 'yellow'), (7, 'yellow'),
                                 (8, ''), (14, ''), (15, ''), (16, '')):
                with self.subTest(timing=timing, gap=gap):
                    d = (date.fromisoformat(today) + timedelta(days=gap)).isoformat()
                    e = {'status': 'ok', 'date': d, 'timing': timing, 'kind': 'expected'}
                    self.assertEqual(monitor.earnings_urgency(e, today), urgency)
                    self.assertEqual(monitor.earnings_within_week(e, today), bool(urgency))
                    self.assertEqual(monitor.earnings_soon(e, today), bool(urgency))
                    self.assertEqual(monitor.earnings_within_list(e, today), -1 <= gap <= 15)
                    with patch.object(monitor, '_bj_today', return_value=today):
                        css = {'red': ' urgent', 'yellow': ' soon', '': ''}[urgency]
                        self.assertIn(f'class="earn{css}"', monitor._earnings_html({'earnings': e}))
        for e in (None, {}, [], '损坏', {'status': 'unknown', 'date': today},
                  {'status': 'error', 'date': today}, {'status': 'na', 'date': today},
                  {'status': 'ok'}, {'status': 'ok', 'date': 'bad'},
                  {'status': 'ok', 'date': '2026-02-30'}, {'status': 'ok', 'date': None}):
            with self.subTest(invalid=e):
                self.assertEqual(monitor.earnings_urgency(e, today), '')
                self.assertFalse(monitor.earnings_within_week(e, today))
                self.assertFalse(monitor.earnings_soon(e, today))
                self.assertFalse(monitor.earnings_within_list(e, today))

    def test_earnings_beijing_midnight_summer_winter_weekend_and_year_boundary(self):
        from datetime import date, datetime
        # 包含冬夏令时及切换日、周末和跨年；行情交易日不得让财报自然日回退。
        for d in ('2026-01-15', '2026-07-15', '2026-03-08', '2026-11-01',
                  '2026-10-11', '2027-01-01'):
            event_day = date.fromisoformat(d)
            for offset, urgency in ((-8, ''), (-7, 'yellow'), (-2, 'yellow'),
                                    (-1, 'red'), (0, 'red'), (1, 'red'), (2, '')):
                bj_day = event_day + timedelta(days=offset)
                for time in ('00:00:00', '23:59:59'):
                    moment = datetime.fromisoformat(f'{bj_day}T{time}+08:00')
                    with self.subTest(date=d, offset=offset, time=time), \
                            patch.object(monitor, 'NOW', moment), \
                            patch.object(monitor, 'TARGET_DATE', '2026-01-02'):
                        self.assertEqual(monitor._bj_today(), bj_day.isoformat())
                        self.assertEqual(monitor._earn_today(), moment.astimezone(monitor.US_TZ).date().isoformat())
                        for timing in ('pre', 'post', 'during', '', 'unknown'):
                            e = {'status': 'ok', 'date': d, 'timing': timing}
                            self.assertEqual(monitor.earnings_urgency(e), urgency)
                            self.assertEqual(monitor.earnings_within_week(e), bool(urgency))
                            self.assertEqual(monitor.earnings_within_list(e), -15 <= offset <= 1)

    def test_official_reschedule_in_red_window_replaces_date_without_stale_event(self):
        old = dict(self.official_fixture(), fetched='2030-12-06', official_checked='2030-12-06')
        revised = dict(old, date='2030-12-17', official_checked='2030-12-09')
        for seeded, day, urgency in ((False, '2030-12-09', ''), (False, '2030-12-10', 'yellow'),
                                     (True, '2030-12-09', ''), (True, '2030-12-10', 'yellow'),
                                     (True, '2030-12-11', 'yellow')):
            latest = dict(revised, official_checked=day)
            with self.subTest(seeded=seeded, day=day), ExitStack() as stack:
                for p in (patch.object(monitor, '_bj_today', return_value=day),
                          patch.object(monitor, 'verified_earnings_events', return_value={'COST': latest} if seeded else {}),
                          patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}),
                          patch.object(monitor.earnings_sources, 'fetch_official_earnings', return_value=latest),
                          patch.object(monitor, 'log')):
                    stack.enter_context(p)
                rows = [{'symbol': 'COST', 'group': 'focus', 'level': 'green'}]
                stat = monitor.attach_earnings(rows, {'COST': old}, day)
                self.assertEqual(rows[0]['earnings']['date'], '2030-12-17')
                self.assertEqual(stat['data']['COST']['date'], '2030-12-17')
                self.assertEqual(monitor.earnings_urgency(rows[0]['earnings']), urgency)
                if day < '2030-12-10':
                    self.assertNotIn('2030-12-10', stat['events'].get('COST', []))
                self.assertEqual(old['date'], '2030-12-10')

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

    def test_earnings_labels_preserve_known_timing_without_source_notes(self):
        date = '2026-10-30'
        year = monitor.datetime.now(monitor.US_TZ).year
        date_txt = '10/30' if year == 2026 else '2026/10/30'
        for kind in ('official', 'expected', 'estimated'):
            for timing, label, bj in (
                    ('pre', '盘前', '北京 10/30 晚间'),
                    ('post', '盘后', '北京 10/31 凌晨'),
                    ('during', '盘中', '北京 10/30 夜–10/31 凌晨'),
                    ('', '具体时段待定', '北京 待定'),
                    (None, '具体时段待定', '北京 待定'),
                    ('unknown', '具体时段待定', '北京 待定')):
                for error in ('', 'http_429'):
                    with self.subTest(kind=kind, timing=timing, error=error):
                        e = dict(self.official_fixture(), date=date, kind=kind,
                                 timing=timing, official_error=error)
                        before = dict(e)
                        note = bj
                        if kind == 'official' and error:
                            note += ' · 待复核'
                        main = f'美东 {date_txt} {label}'
                        self.assertEqual(monitor.earnings_label(e), (main, note))
                        with patch.object(monitor, '_bj_today', return_value='2026-10-20'):
                            self.assertEqual(monitor._earnings_html({'earnings': e}),
                                             f'<span class="earn">{main}<span class="unit">{note}</span></span>')
                        self.assertEqual(e, before)

    def test_earnings_beijing_note_is_separate_from_fund_cache_warning(self):
        for kind in ('official', 'expected', 'estimated'):
            with self.subTest(kind=kind):
                e = {'status': 'ok', 'date': '2026-12-31', 'kind': kind, 'timing': 'post'}
                main, note = monitor.earnings_label(e)
                self.assertIn('北京 01/01 凌晨', note)
                markup = monitor._earnings_html({
                    'earnings': e, 'fund_cache_note': '读取失败 <旧>'})
                self.assertIn(main, markup)
                self.assertIn(note, markup)
                for text in ('基本面', '读取失败', '官方', '预计', '估算'):
                    self.assertNotIn(text, markup)
        self.assertEqual(monitor._earnings_html({'fund_cache_note': '读取失败 <旧>'}), '')

    def test_earnings_date_conflict_remains_visible_without_source(self):
        e = {'status': 'ok', 'date': '2026-10-30', 'kind': 'estimated', 'timing': 'post',
             'cross': 'conflict', 'alt_date': '2026-10-31'}
        main, note = monitor.earnings_label(e)
        self.assertIn('盘后', main)
        self.assertIn('日期待核实（10/31）', note)
        self.assertNotIn('来源', note)
        self.assertNotIn('估算', note)

    def test_earnings_cross_check_yahoo(self):
        from datetime import datetime
        today = '2026-10-16'
        ts = lambda d, h=20: int(monitor.US_TZ.localize(datetime.fromisoformat(d + f'T{h:02d}:30:00')).timestamp()) \
            if hasattr(monitor.US_TZ, 'localize') else int(datetime.fromisoformat(d + f'T{h:02d}:30:00').replace(tzinfo=monitor.US_TZ).timestamp())
        body = lambda dates, est: {'quoteSummary': {'result': [{'calendarEvents': {'earnings': {
            'earningsDate': [{'raw': ts(d)} for d in dates], 'isEarningsDateEstimate': est}}}]}}
        one = monitor.parse_yahoo_earnings(body(['2026-10-20'], False), today)
        self.assertEqual((one['status'], one['date'], one['kind']), ('ok', '2026-10-20', 'expected'))
        rng = monitor.parse_yahoo_earnings(body(['2026-10-20', '2026-10-25'], False), today)
        self.assertEqual((rng['date'], rng['kind']), ('2026-10-20', 'estimated'))
        self.assertEqual(monitor.parse_yahoo_earnings(body(['2026-10-10'], False), today), {'status': 'unknown'})
        self.assertEqual(monitor.parse_yahoo_earnings({}, today), {'status': 'unknown'})

        est = {'status': 'ok', 'date': '2026-11-03', 'timing': '', 'kind': 'estimated'}
        yh = {'status': 'ok', 'date': '2026-10-20', 'timing': '', 'kind': 'expected'}
        cc = monitor.cross_check_earnings
        # 确认 vs 估算：取确认，并记下另一来源日期
        out = cc(est, yh, today)
        self.assertEqual((out['date'], out['cross'], out['alt_date'], out['source2']), ('2026-10-20', 'conflict', '2026-11-03', 'yahoo'))
        # 同类不同日：取较早
        out = cc(dict(est, date='2026-10-30'), dict(yh, kind='estimated', date='2026-10-22'), today)
        self.assertEqual((out['date'], out['alt_date']), ('2026-10-22', '2026-10-30'))
        # 一致
        self.assertEqual(cc(est, dict(yh, date='2026-11-03'), today)['cross'], 'agree')
        # 主源失败采用第二源；第二源失败原样返回主源
        self.assertEqual(cc({'status': 'error'}, yh, today)['source2'], 'yahoo')
        self.assertEqual(cc(est, {'status': 'error'}, today), est)
        self.assertEqual(cc(est, dict(yh, date='2026-10-01'), today), est)
        # 冲突在标签里提示待核实
        text, note = monitor.earnings_label(dict(est, date='2026-10-20', cross='conflict', alt_date='2026-11-03'))
        self.assertIn('待核实', note)
        self.assertIn('11/03', note)
        # 门控：确认日期不再多请求；估算才问第二来源；开关关闭时原样返回
        with patch.object(monitor, 'EARN_CROSS_CHECK', True), \
                patch.object(monitor, 'yahoo_earnings', return_value=yh) as y:
            conf = {'status': 'ok', 'date': '2026-10-30', 'timing': 'post', 'kind': 'expected'}
            self.assertEqual(monitor._with_cross_check('AAPL', conf, today), conf)
            self.assertEqual(y.call_count, 0)
            self.assertEqual(monitor._with_cross_check('SPCX', est, today)['date'], '2026-10-20')
            self.assertEqual(y.call_count, 1)
        with patch.object(monitor, 'yahoo_earnings', return_value=yh) as y:
            self.assertEqual(monitor._with_cross_check('SPCX', est, today), est)
            self.assertEqual(y.call_count, 0)
        # 估算日期窗口内每次运行都重查
        self.assertEqual(monitor.EARN_REFETCH_DAYS['estimated'][-1], (0, 1))

    def test_earnings_stockanalysis_primary_and_timing_from_nasdaq(self):
        """stockanalysis 作主源：日期 + 估算/确认由它给，盘前/盘后仍从 Nasdaq 借；
        两源不一致按「确认优先、同类取较早」合并，并在标签里标待核实。"""
        today = '2026-10-16'
        page = ('...,eps:"2.40",epsGrowth:-40.53,peRatio:"159.33",forwardPE:"42.96",'
                'earningsDate:"Oct 20, 2026",earningsDateLabel:"Earnings Date",description:"x"')
        got = monitor.parse_stockanalysis_earnings(page, today)
        self.assertEqual((got['status'], got['date'], got['kind']), ('ok', '2026-10-20', 'expected'))
        est = monitor.parse_stockanalysis_earnings(
            'earningsDate:"Nov 3, 2026",earningsDateLabel:"Est. Earnings"', today)
        self.assertEqual((est['date'], est['kind']), ('2026-11-03', 'estimated'))
        self.assertEqual(monitor.parse_stockanalysis_earnings('earningsDate:"Oct 1, 2026"', today),
                         {'status': 'unknown'})      # 停在上一期，不冒充下一次
        self.assertEqual(monitor.parse_stockanalysis_earnings('', today), {'status': 'unknown'})
        self.assertEqual(monitor.parse_stockanalysis_earnings('earningsDate:"Foo 3, 2026"', today),
                         {'status': 'unknown'})

        sa = {'status': 'ok', 'date': '2026-10-20', 'timing': '', 'kind': 'expected'}
        nd = {'status': 'ok', 'date': '2026-10-20', 'timing': 'post', 'kind': 'expected'}
        with patch.object(monitor, 'EARN_SA_PRIMARY', True), \
                patch.object(monitor, 'stockanalysis_earnings', return_value=sa), \
                patch.object(monitor, 'nasdaq_earnings', return_value=nd), \
                patch.object(monitor, 'yahoo_earnings', return_value={'status': 'error'}) as y:
            out = monitor.resolve_earnings_sources('SPCX', today)
            self.assertEqual((out['date'], out['timing'], out['cross']), ('2026-10-20', 'post', 'agree'))
            self.assertEqual(y.call_count, 0)      # 日期一致时不再打第三源
            # 两源日期不同：确认优先
            nd2 = {'status': 'ok', 'date': '2026-11-03', 'timing': 'post', 'kind': 'estimated'}
            with patch.object(monitor, 'nasdaq_earnings', return_value=nd2):
                out = monitor.resolve_earnings_sources('SPCX', today)
                self.assertEqual((out['date'], out['kind'], out['cross']), ('2026-10-20', 'expected', 'conflict'))
                self.assertEqual(out['alt_date'], '2026-11-03')
            # Nasdaq 的确认日期胜过主源估算时，来源不能误标 Yahoo。
            with patch.object(monitor, 'stockanalysis_earnings', return_value=dict(sa, kind='estimated')), \
                    patch.object(monitor, 'nasdaq_earnings', return_value=dict(nd, date='2026-10-19')):
                out = monitor.resolve_earnings_sources('SPCX', today)
                self.assertEqual((out['date'], out['source2']), ('2026-10-19', 'nasdaq'))
            # 主源没给：Nasdaq 有就直接用，不额外打第三源
            with patch.object(monitor, 'stockanalysis_earnings', return_value={'status': 'unknown'}):
                self.assertEqual(monitor.resolve_earnings_sources('SPCX', today)['date'], '2026-10-20')
                self.assertEqual(y.call_count, 0)
            # 主源与 Nasdaq 都没给：EARN_CROSS_CHECK 关闭时不打第三源（默认关闭，避免测试联网）
            with patch.object(monitor, 'stockanalysis_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'error'}):
                out = monitor.resolve_earnings_sources('SPCX', today)
                self.assertEqual(out['status'], 'error')
                self.assertEqual(y.call_count, 0)
            # 打开开关后才兜底问第三源；Yahoo 熔断计数在别的用例里可能已满，这里用新门控
            with patch.object(monitor, 'EARN_CROSS_CHECK', True), \
                    patch.object(monitor, 'YAHOO_GATE', monitor.SourceGate(threshold=3, cooldown=90)), \
                    patch.object(monitor, 'stockanalysis_earnings', return_value={'status': 'unknown'}), \
                    patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'error'}):
                monitor.resolve_earnings_sources('SPCX', today)
                self.assertEqual(y.call_count, 1)

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
        # 算法估算的日期：远 7 天、15 天内每次运行重查
        self.assertFalse(due(est('2026-11-10', '2026-10-04'), today))
        self.assertTrue(due(est('2026-11-10', '2026-10-03'), today))
        self.assertFalse(due(est('2026-10-20', '2026-10-10'), today))    # 15 天内的估算：当天已抓不重复
        self.assertTrue(due(est('2026-10-20', '2026-10-09'), today))     # 隔天即重查（估算日期最容易变）
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
        # 固定完整渲染时钟，包括财报标签的年份；不能只固定快照日期。
        real_datetime = monitor.datetime
        with patch.object(monitor, 'TARGET_DATE', '2026-10-07'), \
                patch.object(monitor, '_bj_today', return_value='2026-10-07'), \
                patch.object(monitor, 'datetime', wraps=real_datetime) as clock, \
                patch.object(monitor, 'global_dca', return_value=None):
            clock.now.return_value = real_datetime(2026, 10, 7, tzinfo=monitor.US_TZ)
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
        self.assertNotIn('规则与数据说明', page)
        # 三块说明只以 inert 模板留在首页，供设置页读取；首页不再渲染成可见区块。
        self.assertIn('<template id="notesTpl"><section class="card page-notes" id="pageNotes" '
                      'aria-label="综合判断与宏观细则">', page)
        self.assertEqual(page.count('id="pageNotes"'), 1)
        self.assertNotIn('class="card rule-card"', page)
        self.assertEqual(page.count('id="btnReturnBoard"'), 1)
        self.assertIn('id="btnReturnBoard" href="">返回看板</a>', page)
        self.assertNotIn('id="runMsg"', page)
        self.assertIn('id="runLightTxt"', page)
        self.assertIn('id="ruleLightTxt"', page)
        self.assertLess(page.index('id="btnReturnBoard"'), page.index('id="pageNotes"'))
        # 自动计划从头部第二排挪到页底返回看板下方；头部只留数据时间。
        self.assertIn('<div class="autoplan">自动计划：美东周一至五 20:15（北京 ', page)
        sub = re.search(r'<div class="sub">([^<]*)</div>', page)[1]
        self.assertIn('数据时间', sub)
        self.assertNotIn('自动计划', sub)
        self.assertLess(page.index('id="btnReturnBoard"'), page.index('class="autoplan"'))
        m = re.search(r'<details class="macro-help" id="marketRiskRules">(.*?)</details>', page, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn(' open', page[m.start():m.start() + 60])
        self.assertIn('综合判断与宏观细则', m.group(1))
        self.assertEqual(m.group(1).count('同类只取最高分'), 1)
        self.assertIn('宏观指标细则', m.group(1))
        self.assertNotIn('财报日期：', m.group(1))
        self.assertNotIn('数据来源：', m.group(1))
        self.assertIn(monitor.BREADTH_CREDIT, page)
        self.assertNotIn('查看完整规则与阈值', page)
        self.assertIn('<template id="rulesTpl">', page)
        notes = monitor._page_notes({'reference_alerts': {}})
        self.assertNotIn('id="policyAlertDetails"', notes)
        self.assertNotIn('美联储', notes)
        self.assertNotIn('id="dataSourceRules"', notes)
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
        from copy import deepcopy
        from datetime import datetime
        import exchange_calendars as xcals
        when = datetime.fromisoformat('2026-10-08T11:00:00-04:00')
        dates = [d.date().isoformat() for d in
                 xcals.get_calendar('XNYS').sessions_in_range('2026-08-01', '2026-10-08')[-32:]]
        data = {'dates': dates, 'closes': [100]*31+[102], 'highs':[100]*31+[110],
                'lows':[100]*31+[95], 'volumes':[100]*32, 'price':102, 'prev_close':100}
        original = deepcopy(data)
        quote = {'price': 101, 'ts': when.isoformat(), 'volume': 1500}
        with patch.object(monitor, 'CLOSED_ONLY', False), \
                patch.object(monitor, 'TARGET_DATE', '2026-10-08'), \
                patch.dict(monitor.S, {'use_realtime': 1}):
            with patch.object(monitor, 'nasdaq_realtime', return_value=quote) as realtime:
                result = monitor.apply_realtime('AAPL', data, asof=when)
            realtime.assert_called_once_with('AAPL', asof=when)
            self.assertEqual((result['price'], result['closes'][-1], result['highs'][-1], result['lows'][-1]),
                             (101, 101, 110, 95))
            self.assertTrue(result['realtime'])
            self.assertEqual(result['prev_close'], 100)
            self.assertEqual((result['rt_ts'], result['rt_volume']), (quote['ts'], 1500))
            self.assertTrue(monitor.valid_history(result))
            self.assertIsNot(result, data)
            for key in ('dates', 'closes', 'highs', 'lows', 'volumes'):
                self.assertIsNot(result[key], data[key])
            # 缺失、无时区、昨天、过期和未来成交时间均不得覆盖日线。
            for stamp in (None, 'today', '2026-10-08', '2026-10-08T11:00:00',
                          '2026-10-07T11:00:00-04:00', '2026-10-08T10:54:59-04:00',
                          '2026-10-08T11:00:01-04:00', 'Oct 08, 2026 11:00 AM EST'):
                with self.subTest(stamp=stamp), \
                        patch.object(monitor, 'nasdaq_realtime', return_value=dict(quote, ts=stamp)):
                    self.assertIs(monitor.apply_realtime('AAPL', data, asof=when), data)
            # 删掉上一交易日，既测替换今日 bar，也测从更早历史追加的拒绝路径。
            for replace_today in (False, True):
                broken = {**data, **{k: v[:-2] + (v[-1:] if replace_today else [])
                                    for k, v in data.items() if isinstance(v, list)}}
                broken['price'] = broken['closes'][-1]
                self.assertTrue(monitor.valid_history(broken))
                before = deepcopy(broken)
                with patch.object(monitor, 'nasdaq_realtime', return_value=quote) as realtime:
                    self.assertIs(monitor.apply_realtime('AAPL', broken, asof=when), broken)
                realtime.assert_not_called()
                self.assertEqual(broken, before)
        self.assertEqual(data, original)

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
                        self.assertEqual(monitor.load_settings(),
                                         monitor.DEFAULT_SETTINGS)
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

    def test_closing_bar_patch_when_daily_bars_lag(self):
        """美东收盘后日线源暂时缺当日一根：Yahoo meta 收盘 / Nasdaq 收盘报价补上；不自洽一律不补。"""
        from datetime import datetime as _dt, timezone as _tz
        dates = ['2026-10-16', '2026-10-19']
        closes, vols, highs, lows, rv = [100.0, 101.0], [10, 11], [102.0, 103.0], [99.0, 100.0], [True, True]
        ts = int(_dt(2026, 10, 20, 20, 0, 5, tzinfo=_tz.utc).timestamp())    # 美东 16:00:05 收盘
        meta = {'regularMarketPrice': 102.5, 'regularMarketTime': ts,
                'regularMarketDayHigh': 104.0, 'regularMarketDayLow': 100.5, 'regularMarketVolume': 12}
        out = monitor._yahoo_meta_close(meta, dates, closes, vols, highs, lows, rv)
        self.assertTrue(out[6])
        self.assertEqual((out[0][-1], out[1][-1], out[2][-1], out[3][-1], out[4][-1], out[5][-1]),
                         ('2026-10-20', 102.5, 12, 104.0, 100.5, True))
        # 盘中（收盘前）不补；日线已有当日不补；隔了交易日不补；高低缺失时补但范围记为无效
        intraday = dict(meta, regularMarketTime=ts - 3600)
        self.assertFalse(monitor._yahoo_meta_close(intraday, dates, closes, vols, highs, lows, rv)[6])
        self.assertFalse(monitor._yahoo_meta_close(meta, dates[:1] + ['2026-10-20'], closes, vols, highs, lows, rv)[6])
        self.assertFalse(monitor._yahoo_meta_close(meta, ['2026-10-15', '2026-10-16'], closes, vols, highs, lows, rv)[6])
        nohl = monitor._yahoo_meta_close({k: v for k, v in meta.items() if 'Day' not in k}, dates, closes, vols, highs, lows, rv)
        self.assertTrue(nohl[6]); self.assertEqual((nohl[3][-1], nohl[5][-1]), (102.5, False))
        self.assertFalse(monitor._yahoo_meta_close(dict(meta, regularMarketPrice=300.0), dates, closes, vols, highs, lows, rv)[6])

        hist = {'dates': ['2026-09-%02d' % d for d in range(1, 31)] + ['2026-10-19'],
                'closes': [100.0] * 30 + [101.0], 'volumes': [10] * 31, 'highs': [102.0] * 31, 'lows': [99.0] * 31}
        hist['dates'] = [d for d in hist['dates']]
        hist.update(price=101.0, prev_close=100.0)
        after = _dt(2026, 10, 21, 0, 46, tzinfo=_tz.utc)
        class Now(monitor.datetime):
            @classmethod
            def now(cls, tz=None):
                return after
        with patch.object(monitor, 'CLOSED_ONLY', True), patch.object(monitor, 'TARGET_DATE', '2026-10-20'), \
                patch.object(monitor, 'datetime', Now), patch.object(monitor, 'valid_history', return_value=True), \
                patch.object(monitor, 'nasdaq_closing_quote', return_value={'price': 102.0, 'volume': 15}) as q:
            got = monitor.patch_closing_bar('AAPL', hist)
            self.assertEqual((got['dates'][-1], got['price'], got['prev_close'], got['volumes'][-1], got['close_patch']),
                             ('2026-10-20', 102.0, 101.0, 15, 'nasdaq-quote'))
            self.assertEqual(got['ma_range_valid'][-1], False)
            self.assertEqual(q.call_args.args, ('AAPL', '2026-10-20', 101.0))
            self.assertEqual(hist['dates'][-1], '2026-10-19')        # 原数据不被改
            # 已经是目标日、或报价拿不到：原样返回
            q.return_value = None
            self.assertIs(monitor.patch_closing_bar('AAPL', hist), hist)
        with patch.object(monitor, 'CLOSED_ONLY', False):
            self.assertIs(monitor.patch_closing_bar('AAPL', hist), hist)

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
        # 20:00 之前、美东零点之后：什么都不做；20:00–24:00 内的延迟重试照常补派发
        self.assertEqual(run('2026-10-10T01:45:00+00:00'), 1)   # 夏令时美东21:45（延迟重试）
        self.assertEqual(run('2026-10-10T03:50:00+00:00'), 1)   # 夏令时美东23:50
        self.assertEqual(run('2026-12-10T00:45:00+00:00'), 0)   # 冬令时美东19:45
        self.assertEqual(run('2026-10-10T04:30:00+00:00'), 0)   # 夏令时美东次日00:30

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
                # 用 ExitStack：CI 的 Python 3.11 限制一条 with 最多 20 层（3.13 本地不报），补丁太多会 SyntaxError
                from contextlib import ExitStack
                with ExitStack() as stack:
                    for p in (patch.dict(os.environ, {'GITHUB_OUTPUT': out_file}),
                              patch.object(monitor, 'EVENT', event), patch.object(monitor, 'CLOSED_ONLY', True),
                              patch.object(monitor, 'BASE', directory), patch.object(monitor, 'TARGET_DATE', '2026-10-16'),
                              patch.object(monitor, 'QUOTE_WORKERS', 1), patch.object(monitor, 'QUOTE_RETRY_PASSES', 0),
                              patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}),
                              patch.object(monitor, 'build_reference_alerts', return_value={}),
                              patch.object(monitor, 'build_macro', return_value={}),
                              patch.object(monitor, 'SP500_RS_ROWS', []), patch.object(monitor, 'sp500_rows_fallback', return_value=[]),
                              patch.object(monitor, 'fetch_history', return_value={'price': 100}),
                              patch.object(monitor, 'analyze_symbol', side_effect=analyzed),
                              patch.object(monitor, 'update_consensus', return_value=({}, {})),
                              patch.object(monitor, 'global_dca', return_value=None),
                              patch.object(monitor.time, 'sleep')):
                        stack.enter_context(p)
                    notify = stack.enter_context(patch.object(monitor, 'push_serverchan'))
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
            self.assertIn('待观察', monitor.macro_status('hy_oas', {'ok': True, 'value': 320, 'delta_week': 60})[1])
            self.assertNotIn('待观察', monitor.macro_status('hy_oas', {'ok': True, 'value': 360, 'delta_week': 60})[1])
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
        self.assertIn('过半跌破长均线', monitor.macro_status('breadth', breadth)[1])
        breadth['pressure_confirmed'] = True
        self.assertEqual(monitor.macro_status('breadth', breadth)[0], 'red')
        body['latest']['pct200'] = 65
        with patch.object(monitor.requests, 'get', return_value=Response()):
            self.assertIn('短期仅少数支撑',
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
        self.assertIn('走弱，压力同步升高', page)
        self.assertIn('截至 2026-10-02', page)
        glossary = re.search(r'<template id="glossaryTpl">(.*?)</template>', page, re.S)[1]
        self.assertRegex(glossary, '<a href="' + re.escape(monitor.BREADTH_CREDIT)
                         + '"[^>]*>History of Market</a>')
        self.assertIn('使用成分股均线参与度，不用ETF价格替代', glossary)

    def test_run_light_empty_and_unsupported(self):
        empty = monitor.render({}, [], 0, snapshot={'summary': {'total': 0}})
        self.assertIn('清单为空，未抓取报价', empty)
        self.assertIn('id="runLight" data-phase="idle"', empty)
        unsupported = monitor.render({}, [], 0, snapshot={'summary': {'total': 1, 'unsupported_symbols': ['.SPX']}})
        self.assertIn('1 个特殊代码暂不支持报价，未抓取报价', unsupported)
        run_text = re.search(r'<span id="runLightTxt">(.*?)</span>', unsupported, re.S)[1]
        self.assertNotIn('抓取成功', run_text)
        self.assertIn('1 个特殊代码暂不支持报价，未抓取报价', run_text)
        self.assertIn('id="runLight" data-phase="idle"', unsupported)
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
        self.assertIn('市场风险数据缺失：VIX（有效指标 4/5）', text)
        self.assertNotIn('已更新', text)
        self.assertNotIn('或过期', text)
        self.assertNotIn('抓取成功', text)
        page = monitor.render({}, [], 0, snapshot=snap)
        self.assertIn('id="runLight" data-phase="partial"', page)
        self.assertIn('市场风险数据缺失：VIX', page)
        complete = dict(snap, market_risk={'missing': [], 'valid_count': 5})
        self.assertEqual(monitor._run_light(complete, '2026-10-13 12:34:28')[0], 'ok')
        bad_quote = dict(snap, summary=dict(snap['summary'], missing_symbols=['NTNX']))
        self.assertEqual(monitor._run_light(bad_quote, '2026-10-13 12:34:28')[0], 'bad')
        example = dict(snap, event='schedule', market_risk={'missing': ['垃圾债利差'], 'valid_count': 4})
        self.assertEqual(monitor._run_light(example, '2026-10-22 08:18:36'),
                         ('partial', '自动运行已完成 · 市场风险数据缺失：垃圾债利差（有效指标 4/5） · '
                          '完成于 2026-10-22 08:18:36（北京时间）'))
        for risk in (example['market_risk'], {'missing': [], 'valid_count': 5}):
            unsupported = dict(example, market_risk=risk,
                               summary=dict(snap['summary'], unsupported_symbols=['SPECIAL']))
            _, unsupported_text = monitor._run_light(unsupported, '2026-10-22 08:18:36')
            self.assertIn('1 个特殊代码暂不支持报价', unsupported_text)
            self.assertNotIn('已更新', unsupported_text)

    def test_empty_render_and_safe_names(self):
        row = dict(symbol='MSFT', note='<公司 & 名称>', price=100, chg=0,
                   signals=[], level='green', source='test', group='technology')
        page = monitor.render({}, [row], 1)
        self.assertEqual(page.count('class="card group-card"'), 14)
        self.assertIn('IT软硬Ai (1)', page)
        self.assertIn('房地产 (0)', page)
        self.assertIn('&lt;公司 &amp; 名称&gt;', page)
        self.assertIn('无异动 1 只', page)
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
    def _run(self, cfg, price=100, prev=100, group='position', cons=None, symbol='TEST'):
        closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)]
        closes[-2:] = [prev, price]
        data = dict(closes=closes, highs=[101] * 60, lows=[99] * 60, volumes=[1000] * 60,
                    price=price, prev_close=prev, dates=['2026-10-08'] * 60, source='test')
        cfg = {**cfg, '_consensus': cons} if cons else cfg
        with patch.object(monitor, 'log'):
            return monitor.analyze_symbol(symbol, cfg, data, group=group)

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
        for target in (104, 120, 139.999, 96, 90, 60):      # 低空间且未达到：都不报
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

    def test_space_at_40_is_purple_but_not_reached(self):
        for price in (140, 150, 160, 190, 250):
            with self.subTest(price=price):
                level, base, texts, detail = self._tg({'target': price})
                self.assertEqual(level, base)
                self.assertTrue(detail['purple'])
                self.assertTrue(detail['target']['space_hit'])
                self.assertFalse(detail['target']['hit'])
                self.assertTrue(any('空间' in text for text in texts))
        with patch.dict(monitor.S, {'target_gap_pct': 5.0}):
            self.assertTrue(self._tg({'target': 104})[3]['target']['hit'])
        self.assertEqual(monitor.DEFAULT_SETTINGS['target_gap_pct'], 3.0)
        self.assertEqual(monitor.TARGET_SPACE_PCT, 40)
        self.assertEqual(monitor.TARGET_HIGH_SPACE_PCT, 90)

    def test_over_100_percent_is_high_space_without_anomaly_red(self):
        level, base, texts, detail = self._tg({'target': 250})
        self.assertEqual(level, base)
        self.assertTrue(detail['purple'])
        self.assertEqual(detail['target']['band'], 'high')
        self.assertFalse(detail['target']['hit'])
        self.assertFalse(detail['target'].get('anomaly', False))
        self.assertNotIn(('target', 'red'), detail['signal_marks'])
        self.assertNotIn(('target', 'yellow'), detail['signal_marks'])
        self.assertTrue(any('空间' in text for text in texts))
        self.assertFalse(any('异常' in text for text in texts))

    def test_manual_targets_all_stocks_but_reached_only_positions_and_focus(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            with self.subTest(group=group):
                near = self._tg({'target': 101}, group=group)[3]
                self.assertEqual(near['target']['kind'], 'manual')
                self.assertEqual(near['purple'], group in ('position', 'focus'))
                self.assertEqual(near['target']['hit'], group in ('position', 'focus'))
                space = self._tg({'target': 190}, group=group)[3]
                self.assertEqual(space['target']['kind'], 'manual')
                self.assertTrue(space['purple'])
                self.assertFalse(space['target']['hit'])

    def test_other_groups_use_space_but_do_not_use_reached(self):
        cons = {'s': 'ok', 'avg': 160.0, 'upd': '2026-10-07', 'r': '2026-10-07'}
        for group in (g['item_group'] for g in monitor.GROUPS if g['item_group'] not in ('position', 'focus')):
            for avg in (139.999, 140.0, 160.0, 190.0, 101.0, 500.0):
                with self.subTest(group=group, avg=avg):
                    level, base, texts, detail = self._tg({}, group=group, cons=dict(cons, avg=avg))
                    self.assertEqual(level, base)
                    self.assertEqual(detail['target']['kind'], 'consensus')
                    self.assertEqual(detail['target']['price'], avg)
                    self.assertAlmostEqual(detail['target']['space'], avg - 100)
                    self.assertFalse(detail['target']['hit'])
                    self.assertFalse(detail['target'].get('anomaly', False))
                    self.assertEqual(detail['purple'], avg >= 140)
                    self.assertEqual(bool(texts), avg >= 140)
        level, base, texts, detail = self._tg({}, group='technology', cons={'s': 'fail', 'avg': 160.0, 'r': '2026-09-15'})
        self.assertEqual((level, texts), (base, []))
        self.assertTrue(detail['target']['failed'])
        self.assertIsNone(self._tg({}, group='technology')[3]['target'])

    def test_manual_target_beats_consensus_and_bad_values_ignored(self):
        cons = {'s': 'ok', 'avg': 160.0, 'upd': '2026-10-07', 'r': '2026-10-01'}
        _, _, hit, d = self._tg({'target': 101, 'target_at': '2026-09'}, cons=cons)
        self.assertEqual(d['target']['kind'], 'manual')
        self.assertEqual(d['target']['month'], 9)
        self.assertEqual(hit, ['在目标价 101 ±3% 内（偏离 -1.0%）'])          # 用手填的 101，不是共识的 160
        _, _, hit, d = self._tg({}, cons=cons)
        self.assertEqual(d['target']['kind'], 'consensus')
        self.assertTrue(any('共识价 160' in text and '空间' in text for text in hit))
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
        for kind in ('manual', 'consensus'):
            for space, band, hit, css in ((12, 'low', False, 'low'), (60, 'medium', False, 'space'),
                                          (150, 'high', False, 'high'), (150, 'high', True, 'hit')):
                with self.subTest(kind=kind, css=css):
                    target = {'kind': kind, 'price': 429.47, 'month': 10, 'failed': False,
                              'space': space, 'band': band, 'space_hit': space >= 40, 'hit': hit}
                    self.assertEqual(tag(target), '<span class="tgs"><span class="lvtag tgt ' + css
                                     + '"><span>+' + str(space) + '%</span> <span>429.47</span> '
                                     + '<span>· 10月</span></span></span>')
                    self.assertNotIn('共识', tag(target))
                    self.assertNotIn('目标', tag(target))
        failed = tag({'kind': 'consensus', 'price': 429.0, 'month': 8, 'failed': True,
                      'space': 150, 'band': 'high', 'hit': True, 'space_hit': True})
        self.assertIn('lvtag tgt bad', failed)
        self.assertIn('<span>429</span>', failed)
        self.assertIn('<span>获取失败</span>', failed)
        self.assertNotIn('月', failed)
        self.assertNotIn('月', tag({'kind': 'consensus', 'price': None, 'month': 10, 'failed': True}))
        self.assertNotIn('+150%', failed)
        self.assertIn('<span>获取失败</span>', tag({'kind': 'consensus', 'price': None, 'failed': True}))
        missing = tag({'kind': 'consensus', 'price': 87.56, 'month': None, 'hit': True})
        self.assertIn('lvtag tgt bad', missing)
        self.assertNotIn('月', missing)
        self.assertIn('获取失败', missing)

    def test_exact_space_boundaries_manual_and_consensus(self):
        for kind in ('manual', 'consensus'):
            for price, band, active, state in ((139.999, 'low', False, 'low'),
                                               (140, 'medium', True, 'space'),
                                               (189.999, 'medium', True, 'space'),
                                               (190, 'high', True, 'high')):
                with self.subTest(kind=kind, price=price):
                    cfg = {'target': price} if kind == 'manual' else {}
                    cons = {'s': 'ok', 'avg': price} if kind == 'consensus' else None
                    level, base, _, detail = self._tg(cfg, cons=cons)
                    target = detail['target']
                    self.assertEqual((target['band'], target['space_hit'], target['hit']), (band, active, False))
                    self.assertAlmostEqual(target['space'], price - 100, places=10)
                    self.assertEqual(monitor.target_display_state(target), state)
                    self.assertEqual(detail['purple'], active)
                    self.assertEqual(level, base)

    def test_reached_gap_has_exact_inclusive_boundaries_and_uses_settings(self):
        for price, reached in ((96.999, False), (97, True), (103, True), (103.001, False)):
            with self.subTest(price=price):
                # 无昨收：只验证带内，不能伪造今日跨越。
                target = monitor.evaluate_target(price, None,
                    {'kind': 'manual', 'price': 100}, 'position', 3)
                self.assertEqual(target['hit'], reached)
                self.assertFalse(target['space_hit'])
        with patch.dict(monitor.S, {'target_gap_pct': 5}):
            self.assertTrue(self._tg({'target': 104})[3]['target']['hit'])
        with patch.dict(monitor.S, {'target_gap_pct': 0}):
            self.assertFalse(self._tg({'target': 101})[3]['target']['hit'])
            self.assertTrue(self._tg({'target': 100})[3]['target']['hit'])

    def test_decimal_price_boundaries_do_not_round_into_wrong_band(self):
        for price, target, band in ((0.1, 0.14, 'medium'), (0.1, 0.19, 'high'),
                                     (10, 13.9999, 'low'), (10, 18.9999, 'medium')):
            with self.subTest(price=price, target=target):
                got = monitor.evaluate_target(price, price, {'kind': 'manual', 'price': target}, 'position', 3)
                self.assertEqual(got['band'], band)
        self.assertEqual(monitor.evaluate_target(100, 100, {'kind': 'manual', 'price': 140}, 'position', 3)['space'], 40)
        self.assertEqual(monitor.evaluate_target(100, 100, {'kind': 'manual', 'price': 190}, 'position', 3)['space'], 90)

    def test_reached_overrides_high_space_and_can_overlap_on_down_cross(self):
        level, base, texts, detail = self._tg({'target': 190}, price=100, prev=200)
        target = detail['target']
        self.assertEqual((target['hit'], target['space_hit'], target['band']), (True, True, 'high'))
        self.assertEqual(monitor.target_display_state(target), 'reached')
        self.assertIn('今日跌破目标价 190', texts)
        self.assertIn('lvtag tgt hit', monitor.target_tag(target))
        self.assertEqual(level, base)
        self.assertTrue(detail['purple'])

    def test_space_keeps_strong_non_target_signals(self):
        level, signals, detail = self._run({'target': 250, 'trigger': 120}, price=100, prev=120)
        base, base_signals, base_detail = self._run({'trigger': 120}, price=100, prev=120)
        self.assertEqual(level, base)
        self.assertEqual([m for m in detail['signal_marks'] if m[0] != 'target'], base_detail['signal_marks'])
        self.assertTrue(set(base_signals).issubset(signals))
        self.assertTrue(any(lv == 'red' for _, lv in base_detail['signal_marks']))
        self.assertTrue(detail['purple'])

    def test_cross_group_etf_index_futures_never_get_target_overlay(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for symbol, cfg in (('XLK', {}), ('ETFTEST', {'asset': 'etf'}),
                                 ('INDEXTEST', {'asset': 'index'}), ('^GSPC', {}),
                                 ('.VIX', {}), ('CL=F', {}), ('FUTTEST', {'asset': 'futures'})):
                with self.subTest(group=group, symbol=symbol):
                    _, _, detail = self._run(dict(cfg, target=250), group=group, symbol=symbol,
                                             cons={'s': 'ok', 'avg': 250})
                    self.assertFalse(detail['purple'])
                    self.assertFalse(any(k == 'target' for k, _ in detail['signal_marks']))
                    self.assertFalse(monitor._target_stock(dict(detail, **cfg)))

    def test_missing_quote_never_infers_reached_or_space(self):
        for price in (None, 0, True, float('nan'), float('inf')):
            with self.subTest(price=price):
                target = monitor.evaluate_target(price, 180, {'kind': 'manual', 'price': 190,
                    'hit': True, 'space_hit': True, 'space': 90, 'band': 'high'}, 'position', 3)
                self.assertEqual(monitor.target_display_state(target), 'failed')
                self.assertFalse(target['hit'])
                self.assertFalse(target['space_hit'])
                self.assertIsNone(target['space'])
                self.assertNotIn('+90%', monitor.target_tag(target))
        self.assertEqual(monitor.target_alerts(100, None, 190, '目标价', 3), [])

    def test_failure_with_or_without_old_price_never_enters_overlay(self):
        for avg in (None, 101, 190, 250):
            with self.subTest(avg=avg):
                _, _, texts, detail = self._tg({}, cons={'s': 'fail', 'avg': avg})
                target = detail['target']
                self.assertEqual(texts, [])
                self.assertEqual(monitor.target_display_state(target), 'failed')
                self.assertFalse(target['hit'])
                self.assertFalse(target['space_hit'])
                self.assertFalse(detail['purple'])
                self.assertIsNone(target['space'])

    def test_manual_override_still_valid_when_consensus_failed(self):
        for group in ('position', 'focus', 'technology', 'index_funds'):
            for manual, state in ((101, 'reached'), (140, 'space'), (190, 'high')):
                with self.subTest(group=group, manual=manual):
                    _, _, _, detail = self._tg({'target': manual}, group=group, cons={'s': 'fail', 'avg': 500})
                    expected = 'low' if state == 'reached' and group not in ('position', 'focus') else state
                    self.assertEqual(detail['target']['kind'], 'manual')
                    self.assertFalse(detail['target']['failed'])
                    self.assertEqual(monitor.target_display_state(detail['target']), expected)
                    self.assertEqual(detail['purple'], expected != 'low')

    def test_display_helper_is_pure_and_failure_has_first_priority(self):
        import copy
        for target, state in ((None, 'failed'), ({}, 'failed'),
            ({'price': 190, 'space': 90, 'band': 'high', 'space_hit': True, 'hit': True}, 'reached'),
            ({'price': 190, 'space': 90, 'band': 'high', 'space_hit': True, 'failed': True, 'hit': True}, 'failed'),
            ({'price': 170, 'space': None, 'band': 'high', 'space_hit': True, 'hit': True}, 'failed')):
            with self.subTest(state=state):
                before = copy.deepcopy(target)
                self.assertEqual(monitor.target_display_state(target), state)
                self.assertEqual(target, before)

    def test_evaluate_target_drops_legacy_anomaly_without_mutating_input(self):
        import copy
        target = {'kind': 'consensus', 'price': 250, 'anomaly': True, 'failed': False}
        before = copy.deepcopy(target)
        evaluated = monitor.evaluate_target(100, 100, target, 'technology', 3)
        self.assertEqual(target, before)
        self.assertFalse(evaluated.get('anomaly', False))
        self.assertEqual(monitor.target_display_state(evaluated), 'high')

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
        self.assertFalse(w('NVDA', {'target': 300}, 'technology'))  # 全组个股手填优先，不取共识
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
        evaluated = monitor.evaluate_target(100, 100, target, group, monitor.S['target_gap_pct']) if target else None
        return dict(symbol=sym, note=note, price=100.0, chg=0.0, level=level, signals=signals or [],
                    group=group, data_date='2026-10-08', purple=purple, target=evaluated)

    def test_page_purple_rows_are_unfolded_and_state_class_stacks(self):
        t_hit = {'kind': 'consensus', 'price': 160.0, 'month': 10, 'failed': False, 'hit': False,
                 'space': 60.0, 'space_hit': True, 'band': 'medium'}
        t_ok = {'kind': 'consensus', 'price': 110.0, 'month': 10, 'failed': False, 'hit': False, 'space': 10.0}
        rows = [self._row('AAA', 'green', True, t_hit, ['共识价 160，空间 +60%']),
                self._row('BBB', 'red', True, t_hit, ['异动 +5.0%', '共识价 160，空间 +60%']),
                self._row('CCC', 'yellow', True, t_hit, ['x']),
                self._row('DDD', 'green', False, t_ok)]
        for row in rows:
            row['prev_close'] = 99.0
        page = self._page(rows)
        for symbol, level in (('AAA', 'green'), ('BBB', 'red'), ('CCC', 'yellow')):
            with self.subTest(symbol=symbol):
                matches = re.findall(r'<tr\b([^>]*\bid="s-' + symbol.lower()
                                     + r'"[^>]*)>(.*?)</tr>', page, re.S)
                self.assertEqual(len(matches), 1)
                attrs, body = matches[0]
                self.assertEqual(re.findall(r'\bclass="([^"]*)"', attrs), [level + ' pur pur-space'])
                for name, value in (('data-quiet-keep', 'true'), ('data-price', '100.0'),
                                    ('data-prev-close', '99.0')):
                    self.assertEqual(re.findall(r'\b' + name + r'="([^"]*)"', attrs), [value])
                self.assertTrue(body.startswith('<td class="sym">' + symbol))
        self.assertIn('<td class="sym">DDD', page)
        shown, folded = page.split('无异动 1 只', 1)
        self.assertIn('>AAA', shown)                      # 紫色的绿灯行不折叠
        self.assertNotIn('>DDD', shown)                   # 普通绿灯行仍然折叠
        self.assertIn('>DDD', folded)
        self.assertIn('lvtag tgt space', page)
        self.assertNotIn('tr.pur td.sig', page)
        self.assertIn('--purple:', page)
        glossary = re.search(r'<template id="glossaryTpl">(.*?)</template>', page, re.S)[1]
        self.assertIn('StockAnalysis · S&amp;P Global美元平均目标价；不抓晨星', glossary)
        self.assertIn('14个美东自然日；财后刷新可提前', glossary)
        self.assertNotIn('href="#glossary-fetch"', page)
        self.assertNotIn('共识价来源：stockanalysis.com（S&P Global），每只约', page)

    def test_page_sort_order_not_changed_by_purple(self):
        t_hit = {'kind': 'consensus', 'price': 101.0, 'month': 10, 'failed': False, 'hit': True,
                 'space': 1.0, 'space_hit': False, 'band': 'low'}
        rows = [self._row('GRN', 'green', True, t_hit), self._row('RED', 'red'), self._row('YEL', 'yellow')]
        page = self._page(rows)
        # 统计名单也含代码；仅核对个股主表顺序，不拿统计中首次出现的位置代替。
        pos = [page.index(f'id="s-{s.lower()}"') for s in ('RED', 'YEL', 'GRN')]
        self.assertEqual(pos, sorted(pos))

    def test_page_manual_target_only_shows_space_in_capsule(self):
        t = {'kind': 'manual', 'price': 108.0, 'month': 10, 'failed': False, 'hit': False, 'anomaly': False, 'space': 8.0}
        page = self._page([self._row('MAN', 'yellow', False, t, ['波动 +2.1%'])])
        body = re.search(r'<tr\b[^>]*\bid="s-man"[^>]*>(.*?)</tr>', page, re.S)[1]
        sig = re.search(r'<td class="sig">(.*?)</td>', body, re.S)[1]
        self.assertIn('波动 +2.1%', sig)
        self.assertNotIn('目标价', sig)
        self.assertNotIn('data-signal-kind="target"', sig)
        self.assertNotIn('<span class="tnote">', page)
        # 手填目标价胶囊同样不写「目标」，开头是距现价的空间百分比（2026-10-15 改）
        self.assertIn('<span>+8%</span> <span>108</span> <span>· 10月</span>', page)
        self.assertNotIn('<span>目标</span>', page)
        self.assertNotIn('共识价来源', page)               # 只有手填：不显示共识来源

    def test_page_consensus_hidden_and_no_right_purple_bar(self):
        t = {'kind': 'consensus', 'price': 101.0, 'month': 10, 'failed': False, 'hit': True,
             'space': 1.0, 'space_hit': False, 'band': 'low'}
        page = self._page([self._row('PUR', 'green', True, t, ['在共识价 101 ±3% 内（偏离 -1.0%）', '异动 +2.1%', 'RSI 超买'])])
        # 仅从个股描述隐藏共识；技术提醒与紫色达到状态保持不变。
        self.assertIn('异动 +2.1%</span> <span class="signal-separator">/</span> <span class="signal-item" data-signal-kind="rsi">RSI 超买', page)
        self.assertNotIn('<span class="tp">在共识价 101 ±3% 内（偏离 -1.0%）</span>', page)
        css = page.split('<style>')[1]
        # 红黄绿左条仍为6/4/2px；主行和52周续行不再绘制右侧紫条。
        self.assertIn('tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red)', css)
        self.assertIn('tr.yellow td:first-child{box-shadow:inset 4px 0 0 var(--yellow)', css)
        self.assertIn('tr.green td:first-child{box-shadow:inset 2px 0 0 var(--green)}', css)
        self.assertNotRegex(css, r'tr\.pur(?:-(?:space|high|hit))?(?::has\([^}]*\))?\s*(?:td\.sig|\+tr\.range52-row>td)\{')
        self.assertNotIn('--pstripe', css)
        self.assertNotIn('tr.pur td:first-child', css)

    def test_target_states_keep_lamps_and_range52_left_stripes_without_right_bar(self):
        css = monitor.PAGE_CSS
        for level, width in (('red', 6), ('yellow', 4), ('green', 2)):
            self.assertIn('tr.' + level + ':has(+tr.range52-row)>td:first-child{--lc:var(--'
                          + level + ');--lw:' + str(width) + 'px}', css)
            self.assertIn('tr.' + level + '+tr.range52-row>td{--lc:var(--'
                          + level + ');--lw:' + str(width) + 'px}', css)
            for target_price, state in ((150, 'space'), (200, 'high'), (101, 'hit')):
                with self.subTest(level=level, state=state):
                    row = self._row('NOPUR', level, True,
                                    {'kind': 'consensus', 'price': target_price, 'failed': False},
                                    ['异动 +2.1%'])
                    row['range52'] = {'low': 50, 'high': 150, 'position': 50, 'scope': '52周',
                                      'from': '2025-10-20', 'to': '2026-10-20'}
                    rendered = monitor._stock_row(row, {})
                    self.assertIn('class="' + level + ' pur pur-' + state + '"', rendered)
                    self.assertIn('data-base-level="' + level + '"', rendered)
                    self.assertIn('lvtag tgt ' + state, rendered)
                    self.assertIn('class="range52-row"', rendered)
                    self.assertNotRegex(rendered, r'\bstyle="[^"]*(?:--pstripe|background-image)')
        self.assertNotRegex(css, r'tr\.pur(?:-(?:space|high|hit))?(?::has\([^}]*\))?\s*(?:td\.sig|\+tr\.range52-row>td)\{')
        self.assertNotIn('--pstripe', css)
        continuation = re.search(r'\.stk tr\.range52-row>td\{([^}]*)\}', css)[1]
        self.assertIn('background-image:linear-gradient(var(--lc),var(--lc))', continuation)
        self.assertIn('background-size:var(--lw) 14px', continuation)
        self.assertIn('background-position:left top', continuation)
        self.assertNotIn('right', continuation)

    def test_low_and_failed_target_capsules_use_sparse_gray_svg_border(self):
        from urllib.parse import unquote
        import xml.etree.ElementTree as ET

        css = monitor.PAGE_CSS
        overlay = re.search(r'\.lvtag\.tgt\.low::after,\.lvtag\.tgt\.bad::after\{([^}]*)\}', css)[1]
        encoded = re.search(r'url\("(data:image/svg\+xml,[^"]+)"\)', overlay)[1]
        rect = ET.fromstring(unquote(encoded.split(',', 1)[1])).find('{http://www.w3.org/2000/svg}rect')
        self.assertIsNotNone(rect)
        for key, value in (('stroke', '#8b93a1'), ('stroke-width', '1'),
                           ('stroke-dasharray', '3 5'), ('rx', '4'), ('fill', 'none')):
            self.assertEqual(rect.attrib[key], value)
        self.assertIn('inset:-1px', overlay)
        self.assertIn('border-radius:inherit', overlay)
        self.assertIn('pointer-events:none', overlay)
        self.assertIn('center/100% 100% no-repeat', overlay)
        # 紫灯文字与紫灯胶囊外框同色（统一走 --purple，#b084ff 为该变量的取值）。
        self.assertIn('color:var(--purple)', re.search(r'\.lvtag\.tgt\{([^}]*)\}', css)[1])
        self.assertIn('--purple:#b084ff', css)
        for dead in ('#a371f7', '#c4a3ff'):
            self.assertNotIn(dead, css)
        self.assertIn('padding:1px 5px;border-radius:4px;line-height:1.35', css)
        for state in ('low', 'bad'):
            rule = re.search(r'\.lvtag\.tgt\.' + state + r'\{([^}]*)\}', css)[1]
            self.assertIn('border:1px solid transparent', rule)
            self.assertNotIn('dashed', rule)
            self.assertNotIn('padding:', rule)
        self.assertIn('color:var(--dim)', re.search(r'\.lvtag\.tgt\.bad\{([^}]*)\}', css)[1])
        self.assertNotRegex(css, r'\.lvtag\.tgt\.bad::after[^}]*content:none')

    def test_group_header_shows_purple_count_only_when_there_is_one(self):
        t = {'kind': 'consensus', 'price': 101.0, 'month': 10, 'failed': False, 'hit': True,
             'space': 1.0, 'space_hit': False, 'band': 'low'}
        with_pur = self._page([self._row('PUR', 'green', True, t, ['在共识价 101 ±3% 内（偏离 -1.0%）'])])
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
            # Python 3.11 将同一 with 的每个上下文分别计入 20 层限制；用 ExitStack 保留隔离而不堆嵌套。
            with ExitStack() as stack:
                for mocked in (
                        patch.object(monitor, 'BASE', directory),
                        patch.object(monitor, 'TARGET_DATE', '2026-10-07'),
                        patch.object(monitor, 'QUOTE_WORKERS', 1),
                        patch.object(monitor, 'QUOTE_RETRY_PASSES', 0),
                        patch.object(monitor, 'nasdaq_earnings', return_value={'status': 'unknown'}),
                        patch.object(monitor, 'stockanalysis_earnings', return_value={'status': 'unknown'}),
                        patch.object(monitor, 'yahoo_earnings', return_value={'status': 'error'}),
                        patch.object(monitor, 'build_reference_alerts', return_value={}),
                        patch.object(monitor, 'build_macro', return_value={}),
                        patch.object(monitor, 'SP500_RS_ROWS', []),
                        patch.object(monitor, 'sp500_rows_fallback', return_value=[]),
                        patch.object(monitor, 'fetch_history', return_value={'price': 100}),
                        patch.object(monitor, 'analyze_symbol', side_effect=analyzed),
                        patch.object(monitor, 'global_dca', return_value=None),
                        patch.object(monitor.time, 'sleep'),
                        patch.object(monitor, 'push_serverchan')):
                    stack.enter_context(mocked)
                upd = stack.enter_context(patch.object(
                    monitor, 'update_consensus', return_value=({'AAPL': cache['AAPL']}, {'ok': 1})))
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
        with patch.object(monitor, '_EARN_CACHE', {}), patch.object(monitor, '_earn_session', return_value=session), \
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

    def test_report_stays_red_through_d_plus_one_and_clears_on_d_plus_two(self):
        # 美东跨到 10/21（北京 10/21 中午）不得提前把 10/20 财报换成下一季或 unknown。
        for timing in ('pre', 'post', 'during', '', 'unknown'):
            for future in ({'status': 'unknown'}, {'status': 'error'}, self._earn('2027-01-20')):
                with self.subTest(timing=timing, future=future):
                    old = {'AAA': dict(self._earn('2026-10-20', '2026-10-19'), timing=timing)}
                    rows = [self._row('AAA')]
                    with patch.object(monitor, '_bj_today', return_value='2026-10-21'), \
                            patch.object(monitor, 'nasdaq_earnings', return_value=future) as fetch, \
                            patch.object(monitor, 'log'):
                        st = monitor.attach_earnings(rows, old, '2026-10-21')
                    fetch.assert_not_called()
                    self.assertEqual(rows[0]['earnings'], old['AAA'])
                    self.assertEqual(monitor.earnings_urgency(rows[0]['earnings'], '2026-10-21'), 'red')
                    self.assertTrue(monitor.earnings_within_list(rows[0]['earnings'], '2026-10-21'))
                    self.assertTrue(monitor.earnings_within_week(rows[0]['earnings'], '2026-10-21'))
                    self.assertEqual(st['data']['AAA'], old['AAA'])
                    self.assertEqual(st['events']['AAA'], ['2026-10-20'])
                    with patch.object(monitor, '_bj_today', return_value='2026-10-22'), \
                            patch.object(monitor, 'nasdaq_earnings', return_value=future) as fetch, \
                            patch.object(monitor, 'log'):
                        st2 = monitor.attach_earnings(rows, st['data'], '2026-10-21', st['events'])
                    fetch.assert_called_once()
                    self.assertNotEqual(rows[0]['earnings'].get('date'), '2026-10-20')
                    self.assertEqual(st2['events']['AAA'], ['2026-10-20'])

    def test_red_cache_survives_primary_next_quarter_unknown_and_failure(self):
        import copy
        from datetime import datetime
        for timing in ('pre', 'post', 'during', '', 'unknown'):
            for primary in ({'status': 'unknown'}, {'status': 'error'}, self._earn('2027-01-20')):
                with self.subTest(timing=timing, primary=primary), ExitStack() as stack:
                    for p in (patch.object(monitor, 'EARN_SA_PRIMARY', True),
                              patch.object(monitor, 'EARN_CROSS_CHECK', False),
                              patch.object(monitor, 'verified_earnings_events', return_value={}),
                              patch.object(monitor, 'stockanalysis_earnings', return_value=primary),
                              patch.object(monitor, 'nasdaq_earnings', return_value=primary),
                              patch.object(monitor.earnings_sources, 'fetch_official_earnings', return_value={'status': 'unknown'}),
                              patch.object(monitor, 'log')):
                        stack.enter_context(p)
                    cache = {'AAA': dict(self._earn('2026-10-20', '2026-10-10'),
                                         timing=timing, kind='estimated')}
                    events = {}
                    for bj_day in ('2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22'):
                        for time in ('00:00:00', '23:59:59'):
                            now = datetime.fromisoformat(f'{bj_day}T{time}+08:00')
                            rows = [dict(self._row('AAA'), chg=0, signals=[], note='')]
                            before = copy.deepcopy((cache, events))
                            with patch.object(monitor, 'NOW', now):
                                state = monitor.attach_earnings(rows, cache, prev_events=events)
                                shown = rows[0]['earnings']
                                self.assertEqual((cache, events), before)
                                if bj_day < '2026-10-22':
                                    self.assertEqual(shown.get('date'), '2026-10-20', bj_day + ' ' + time)
                                    self.assertEqual(monitor.earnings_urgency(shown), 'red')
                                    self.assertTrue(monitor.earnings_within_list(shown))
                                    self.assertTrue(monitor.earnings_within_week(shown))
                                    self.assertIn('class="earn urgent"', monitor._earnings_html(rows[0]))
                                    body = monitor._group_body(rows, {'item_group': 'technology'}, {}, bj_day, True, 'group_technology', '科技')
                                    self.assertNotIn('无异动 1 只', body)
                                else:
                                    self.assertNotEqual(shown.get('date'), '2026-10-20')
                                    self.assertEqual(monitor.earnings_urgency(shown), '')
                                    self.assertFalse(monitor.earnings_within_list(shown))
                                    self.assertFalse(monitor.earnings_within_week(shown))
                                    self.assertEqual(state['events']['AAA'], ['2026-10-20'])
                            cache, events = state['data'], state['events']

    def test_deferred_red_cache_keeps_date_and_fetch_timestamp(self):
        for day in ('2026-10-19', '2026-10-20', '2026-10-21'):
            with self.subTest(day=day), ExitStack() as stack:
                api = stack.enter_context(patch.object(monitor, 'nasdaq_earnings'))
                for p in (patch.object(monitor, '_bj_today', return_value=day),
                          patch.object(monitor, 'verified_earnings_events', return_value={}),
                          patch.object(monitor, 'EARN_MAX_FETCH_PER_RUN', 0),
                          patch.object(monitor, 'log')):
                    stack.enter_context(p)
                old = {'AAA': dict(self._earn('2026-10-20', '2026-10-10'), kind='estimated')}
                rows = [self._row('AAA')]
                stat = monitor.attach_earnings(rows, old, day)
                api.assert_not_called()
                self.assertEqual(rows[0]['earnings'], old['AAA'])
                self.assertEqual(stat['data']['AAA'], old['AAA'])
                self.assertEqual(monitor.earnings_urgency(rows[0]['earnings']), 'red')

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



    def test_legacy_fund_does_not_prevent_quiet_folding_or_hide_real_alerts(self):
        base = dict(symbol='AAA', group='technology', level='green', price=100, chg=0,
                    note='', signals=[])
        def body(row):
            return monitor._group_body([row], {'item_group': 'technology'}, {}, '2026-10-21', True, 'group_technology', '科技')
        for level in (None, 'green', 'gray', 'yellow', 'red'):
            with self.subTest(legacy_level=level):
                legacy = dict(base, fund={'level': level, 'hits': ['旧基本面风险']},
                              fund_cache_note='旧基本面数据不全')
                markup = body(legacy)
                self.assertEqual(markup, body(base))
                self.assertIn('<details class="quiet-list"><summary>无异动 1 只', markup)
                self.assertNotIn('<details class="quiet-list" open>', markup)
                self.assertNotIn('基本面', markup)
                self.assertNotIn('data-fund-', markup)
        exceptions = [
            {'level': 'red'}, {'level': 'yellow'},
            {'purple': True, 'target': monitor.evaluate_target(
                100, 100, {'kind': 'consensus', 'price': 140, 'failed': False}, 'technology', 3)},
            {'earnings': {'status': 'ok', 'date': '2026-10-20', 'timing': 'post'}},
            {'earnings': {'status': 'ok', 'date': '2026-10-26', 'timing': 'pre'}}]
        for change in exceptions:
            with self.subTest(active_alert=change):
                row = {**base, **change}
                markup = body(dict(row, fund={'level': 'red', 'hits': ['旧基本面风险']},
                                   fund_cache_note='旧基本面待更新'))
                self.assertEqual(markup, body(row))
                self.assertNotIn('class="quiet-list"', markup)
                self.assertNotIn('基本面', markup)


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
                    patch.object(monitor, 'SP500_RS_ROWS', []), \
                    patch.object(monitor, 'sp500_rows_fallback', return_value=[]), \
                    patch.object(monitor, 'fetch_history', return_value=None), \
                    patch.object(monitor.time, 'sleep'), \
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
                self.assertEqual(third['consensus']['quota'],
                                 {'date': '2026-10-11', 'symbols': ['AAA'], 'retry_symbols': []})
                fourth = run('2026-10-10T21:15:00-04:00', {'status': 'unknown'})
                self.assertEqual(cons.call_count, 2)  # 同一北京配额日不重复抓取
                self.assertEqual(fourth['consensus']['quota'], third['consensus']['quota'])
                fifth = run('2026-10-11T20:15:00-04:00', {'status': 'unknown'})
                self.assertEqual(cons.call_count, 2)  # 财后刷新完成，次日仍复用有效缓存
                self.assertEqual(fifth['earnings']['events']['AAA'], ['2026-10-08'])
                for state in (first, second, third, fourth, fifth):
                    self.assertNotIn('fundamental', state)
                    self.assertNotIn('fund', state['signal_counts'])
                    self.assertNotIn('fund', state['signal_members'])
                    self.assertFalse(any(key.startswith('fund_') for key in state['effective_settings']))
                with open(os.path.join(directory, 'index.html'), encoding='utf-8') as f:
                    page = f.read()
                # 新抓取稿仅有这一句明确声明不抓基本面，不能据此误判功能残留。
                retired_note = '只抓日期／时段，不抓财报内容或基本面。'
                self.assertEqual(page.count(retired_note), 1)
                self.assertNotIn('基本面', page.replace(retired_note, ''))
                self.assertNotRegex(page, r'data-fund-|fund[_-]|FUND_|id="fund')

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
            self.assertIn(f"量比 2.0×（{data['dates'][-2]}）", signals)
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
        data = dict(data)
        data.setdefault('rt_ts', time)
        if realtime_volume is not None:
            data['rt_volume'] = realtime_volume
        with patch.object(monitor, 'TARGET_DATE', data['dates'][-1]):
            return monitor.intraday_volume_ratio('AAPL', data, asof=datetime.fromisoformat(time))

    def test_midday_uses_elapsed_minutes(self):
        data = self.history('2026-10-13')
        # 20 日均量 1000/全日 390 分钟 = 2.5641/分钟；12:00 已开盘 150 分钟，1500 股 → 10/分钟
        ratio, moment = self.measure(data, '2026-10-13T12:00:00-04:00', 1500)
        self.assertEqual(moment, '12:00')
        self.assertAlmostEqual(ratio, (1500 / 150) / (1000 / 390), places=4)
        delayed = dict(data, rt_ts='2026-10-13T11:55:00-04:00')
        ratio, moment = self.measure(delayed, '2026-10-13T12:00:00-04:00', 1500)
        self.assertEqual(moment, '11:55')
        self.assertAlmostEqual(ratio, (1500 / 145) / (1000 / 390), places=4)
        for stamp in (None, '', '2026-10-13', '2026-10-13T12:00:00',
                      '2026-10-12T12:00:00-04:00', '2026-10-13T11:54:59-04:00',
                      '2026-10-13T12:00:01-04:00'):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.measure(dict(data, rt_ts=stamp), '2026-10-13T12:00:00-04:00', 1500),
                                 (None, ''))
        # 长度和成交量仍然有效，单独验证缺日、重复日、休市日不能凑足20日均量。
        for day in ('2026-10-09', '2026-10-11', '2026-10-13'):
            with self.subTest(previous=day):
                broken = dict(data, dates=data['dates'][:-2] + [day, data['dates'][-1]])
                self.assertEqual(self.measure(broken, '2026-10-13T12:00:00-04:00', 1500), (None, ''))
        self.assertNotIn('rt_volume', data)
        self.assertNotIn('rt_ts', data)

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
        for bad in (0, -5, None, '3000', True, float('nan'), float('inf')):
            with self.subTest(bad=bad):
                self.assertEqual(self.measure(dict(data), '2026-10-13T12:00:00-04:00', bad), (None, ''))
        # 均量样本里出现 0/None 也不硬算
        broken = dict(data, volumes=data['volumes'][:-21] + [0] + data['volumes'][-20:])
        self.assertEqual(self.measure(broken, '2026-10-13T12:00:00-04:00', 1500), (None, ''))

    def test_futures_and_index_not_supported(self):
        data = dict(self.history('2026-10-13'), rt_volume=1500, rt_ts='2026-10-13T12:00:00-04:00')
        from datetime import datetime
        when = datetime.fromisoformat(data['rt_ts'])
        with patch.object(monitor, 'TARGET_DATE', '2026-10-13'):
            self.assertIsNotNone(monitor.intraday_volume_ratio('AAPL', data, asof=when)[0])
            self.assertEqual(monitor.intraday_volume_ratio('ESMAIN', data, asof=when), (None, ''))
            self.assertEqual(monitor.intraday_volume_ratio('^VIX', data, asof=when), (None, ''))

    def test_half_day_uses_shortened_session(self):
        data = self.history('2026-11-27')     # 感恩节次日 13:00 提前收盘 → 全日 210 分钟
        self.assertEqual(data['dates'][-2:], ['2026-11-25', '2026-11-27'])
        self.assertNotIn('2026-11-26', data['dates'])
        ratio, moment = self.measure(data, '2026-11-27T11:00:00-05:00', 900)   # 已开盘 90 分钟
        self.assertEqual(moment, '11:00')
        self.assertAlmostEqual(ratio, (900 / 90) / (1000 / 210), places=4)
        last_quote = dict(data, rt_ts='2026-11-27T12:59:00-05:00')
        self.assertAlmostEqual(self.measure(last_quote, '2026-11-27T12:59:00-05:00', 900)[0],
                               (900 / 209) / (1000 / 210), places=4)
        for time in ('2026-11-27T13:00:00-05:00', '2026-11-27T14:00:00-05:00',
                     '2026-11-26T11:00:00-05:00'):
            with self.subTest(time=time):
                self.assertEqual(self.measure(last_quote, time, 900), (None, ''))

    def test_alert_prefers_intraday_over_completed_day(self):
        data = self.history('2026-10-13')
        data['volumes'][-2] = 2600            # 昨日放量 → 20 日均量 1080
        data['rt_volume'] = 1500              # 盘中折算 3.6×，超过默认红线
        data['rt_ts'] = '2026-10-13T12:00:00-04:00'
        average = (2600 + 19 * 1000) / 20
        from datetime import datetime
        midday = datetime.fromisoformat(data['rt_ts'])
        class TradingClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return midday.astimezone(tz)
        # 完整日使用任务时钟 NOW；实时量使用当前时钟，两者和目标日都要固定。
        with patch.object(monitor, 'NOW', midday), patch.object(monitor, 'datetime', TradingClock), \
                patch.object(monitor, 'TARGET_DATE', data['dates'][-1]):
            _, signals, detail = monitor.analyze_symbol('AAPL', {}, data)
        self.assertTrue(detail['vol_ratio_intraday'])
        self.assertAlmostEqual(detail['rt_vol_ratio'], (1500 / 150) / (average / 390), places=4)
        self.assertIn('盘中量比 3.6×（截至 12:00 ET）', signals)
        self.assertFalse(any(text.startswith('量比 ') for text in signals), signals)
        self.assertIn(('vol_ratio', 'red'), detail['signal_marks'])     # 3.6× ≥ 默认红线 3.0
        self.assertEqual(monitor._member_metric(detail, 'vol_ratio'), '盘中量比 3.6×（截至 12:00 ET）')
        with patch.object(monitor, 'NOW', midday), patch.object(monitor, 'datetime', TradingClock), \
                patch.object(monitor, 'TARGET_DATE', data['dates'][-1]), \
                patch.object(monitor, 'S', {**monitor.S, 'vol_ratio_red': 4.0}):
            _, _, y_detail = monitor.analyze_symbol('AAPL', {}, data)
        self.assertIn(('vol_ratio', 'yellow'), y_detail['signal_marks'])  # 只到黄线时报黄

    def test_after_close_falls_back_to_completed_day(self):
        data = self.history('2026-10-13')
        data['volumes'][-2] = 2600
        data['volumes'][-1] = 2000            # 今日已收盘，日线给出完整量 → 完整日量比 1.9×
        data['rt_volume'] = 1500              # 已收盘：实时量不该再参与
        data['rt_ts'] = '2026-10-13T15:59:00-04:00'
        from datetime import datetime
        closed = datetime.fromisoformat('2026-10-13T18:00:00-04:00')
        class ClosedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return closed.astimezone(tz)
        with patch.object(monitor, 'NOW', closed), patch.object(monitor, 'datetime', ClosedClock), \
                patch.object(monitor, 'TARGET_DATE', data['dates'][-1]):
            _, signals, detail = monitor.analyze_symbol('AAPL', {}, data)
        self.assertFalse(detail['vol_ratio_intraday'])
        self.assertAlmostEqual(detail['vol_ratio'], 2000 / ((2600 + 19 * 1000) / 20), places=4)
        self.assertIn(monitor._member_metric(detail, 'vol_ratio'), signals)
        self.assertFalse(any('盘中量比' in text for text in signals), signals)


class TestIntradayVolumeThresholds(unittest.TestCase):
    """盘中量比的门槛随开盘进度放宽：抵消线性折算在早盘的系统性高估，量比数值本身不变。"""

    def test_bands_widen_early_and_settle_afternoon(self):
        base = (1.5, 3.0)
        cases = ((30, 390, 1.69), (44, 390, 1.69), (50, 390, 1.45), (75, 390, 1.30),
                 (100, 390, 1.13), (150, 390, 1.0), (380, 390, 1.0))
        for elapsed, total, factor in cases:
            with self.subTest(elapsed=elapsed, total=total):
                yellow, red = monitor.intraday_volume_thresholds(elapsed, total, *base)
                self.assertAlmostEqual(yellow, 1.5 * factor, places=6)
                self.assertAlmostEqual(red, 3.0 * factor, places=6)

    def test_half_day_converts_to_equivalent_minutes(self):
        # 半日市全天更短，同一时刻的线性进度更大、高估更少 → 等效分钟更长、门槛更窄
        early_full = monitor.intraday_volume_thresholds(30, 390, 1.5, 3.0)
        early_half = monitor.intraday_volume_thresholds(30, 210, 1.5, 3.0)
        self.assertLess(early_half[0], early_full[0])
        self.assertEqual(early_half, monitor.intraday_volume_thresholds(30 * 390 / 210, 390, 1.5, 3.0))

    def test_invalid_inputs_fall_back_to_given_thresholds(self):
        for args in ((None, 390, 1.5, 3.0), (30, 0, 1.5, 3.0), (30, 390, 0, 3.0),
                     (float('nan'), 390, 1.5, 3.0), (30, 390, float('inf'), 3.0)):
            with self.subTest(args=args):
                self.assertEqual(monitor.intraday_volume_thresholds(*args), (args[2], args[3]))

    def test_custom_thresholds_are_scaled_not_overridden(self):
        yellow, red = monitor.intraday_volume_thresholds(31, 390, 2.0, 4.0)
        self.assertAlmostEqual(yellow, 2.0 * 1.69, places=6)
        self.assertAlmostEqual(red, 4.0 * 1.69, places=6)

    def test_quiet_minutes_extended_to_thirty(self):
        self.assertEqual(monitor.INTRADAY_VOL_QUIET_MIN, 30)

    def _analyze(self, volume, moment):
        from datetime import datetime
        import exchange_calendars as xcals
        cal = xcals.get_calendar('XNYS')
        sessions = cal.sessions_in_range('2026-04-01', '2026-10-13')[-23:]
        data = {'dates': [d.date().isoformat() for d in sessions],
                'volumes': [1000] * 22 + [0], 'closes': [100] * 23,
                'highs': [100] * 23, 'lows': [100] * 23, 'price': 100, 'prev_close': 100,
                'rt_volume': volume, 'rt_ts': moment}
        at = datetime.fromisoformat(moment)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return at.astimezone(tz)
        with patch.object(monitor, 'NOW', at), patch.object(monitor, 'datetime', Clock), \
                patch.object(monitor, 'TARGET_DATE', data['dates'][-1]):
            return monitor.analyze_symbol('AAPL', {}, data)

    def test_same_ratio_ignored_early_and_flagged_at_noon(self):
        # 同一 2.0× 的折算量比：开盘 31 分钟被抬高到 2.535× 而不报，午间仍是黄灯
        _, early_signals, early = self._analyze(159, '2026-10-13T10:01:00-04:00')
        _, noon_signals, noon = self._analyze(769, '2026-10-13T12:00:00-04:00')
        self.assertTrue(early['vol_ratio_intraday'])
        self.assertAlmostEqual(early['rt_vol_ratio'], 2.0, places=2)
        self.assertAlmostEqual(noon['rt_vol_ratio'], 2.0, places=2)
        self.assertEqual(early['rt_vol_elapsed'], 31)
        self.assertEqual(noon['rt_vol_elapsed'], 150)
        self.assertFalse(any('量比' in text for text in early_signals), early_signals)
        self.assertIn(('vol_ratio', 'yellow'), noon['signal_marks'])

    def test_early_red_requires_much_higher_ratio(self):
        # 早盘红线抬到 5.07×：3.0× 不报红，5.2× 才报红
        _, below_signals, below = self._analyze(238, '2026-10-13T10:01:00-04:00')     # ≈3.0×
        _, above_signals, above = self._analyze(413, '2026-10-13T10:01:00-04:00')     # ≈5.2×
        self.assertNotIn(('vol_ratio', 'red'), below['signal_marks'])          # 3.0× < 早盘红线 5.07×
        self.assertIn(('vol_ratio', 'yellow'), below['signal_marks'])         # 但已过抬高的黄线 2.535×
        self.assertIn('盘中量比 5.2×（截至 10:01 ET）', above_signals)
        self.assertIn(('vol_ratio', 'red'), above['signal_marks'])


class TestVolumeRatioZscore(unittest.TestCase):
    """保留原 z 指标，收盘按显式分布的连续融合门槛判灯；缺失回退固定，盘中不动。"""

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

    def test_rule_uses_explicit_distribution_not_z_when_available(self):
        import math
        for ratio, level in ((1.3, None), (1.64, None), (1.66, 'yellow'),
                             (2.96, 'yellow'), (2.98, 'red'), (4, 'red')):
            for z in (None, -100, 100):
                with self.subTest(ratio=ratio, z=z):
                    x = self.context(ratio=ratio, z=z)
                    x.vol_ratio_m, x.vol_ratio_s = 0, 0.30
                    al = self.run_rule(x)
                    self.assertEqual(al.marks, [] if level is None else [('vol_ratio', level)])
                    if level:
                        self.assertEqual(al.signals[0], f'量比 {ratio:.1f}×（2026-10-13）')
        x = self.context(ratio=1.7)
        x.vol_ratio_m, x.vol_ratio_s = 0, 0.3
        self.assertEqual(self.run_rule(x, vol_ratio_z=3).marks, [])
        x.vol_ratio = math.exp((0.3 * 3 + math.log(3)) / 2) * (1 + 1e-12)
        self.assertEqual(self.run_rule(x, vol_ratio_red_z=3).marks, [('vol_ratio', 'red')])

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
        self.assertIn('盘中量比 1.6×（截至 12:00 ET）', al.signals[0])
        self.assertNotIn('自身波动', al.signals[0])
        self.assertEqual(len(al.signals), 1)
        self.assertEqual(self.run_rule(self.context(intraday=True, rt=1.2, z=9.9)).marks, [])

    def test_volume_text_matches_main_and_statistics_in_all_modes(self):
        import copy
        import html
        import re
        cases = (
            (True, 2.1, False, 'yellow', '盘中量比 2.1×（截至 10:30 ET）'),
            (True, 4.0, False, 'red', '盘中量比 4.0×（截至 10:30 ET）'),
            (False, 2.1, False, 'yellow', '量比 2.1×（2026-10-26）'),
            (False, 4.0, False, 'red', '量比 4.0×（2026-10-26）'),
            (False, 2.1, True, 'yellow', '量比 2.1×（2026-10-26）'),
            (False, 4.0, True, 'red', '量比 4.0×（2026-10-26）'),
        )
        for intraday, ratio, adaptive, level, expected in cases:
            with self.subTest(intraday=intraday, ratio=ratio, adaptive=adaptive):
                x = self.context(ratio=ratio, intraday=intraday, rt=ratio)
                x.rt_vol_time, x.vol_ratio_date = '10:30', '2026-10-26'
                if adaptive:
                    x.vol_ratio_m, x.vol_ratio_s = 0.0, 0.30
                al = self.run_rule(x, vol_ratio=1.5, vol_ratio_red=3,
                                   vol_ratio_z=2, vol_ratio_red_z=3.6)
                self.assertEqual(al.signals, [expected])
                self.assertEqual(al.marks, [('vol_ratio', level)])
                d = dict(vars(x), symbol='VOLTEXT', group='focus', price=100.0, chg=1.0,
                         level=al.level, signals=al.signals, signal_marks=al.marks)
                before = copy.deepcopy(d)
                main = monitor._signals_html(d)
                self.assertEqual(html.unescape(re.sub(r'<[^>]+>', '', main)), expected)
                self.assertIn(f'data-signal-level="{level}"', main)
                self.assertEqual(monitor._member_metric(d, 'vol_ratio'), expected)
                snap = monitor.build_snapshot({}, [d], {}, group_counts={})
                member = snap['signal_members']['vol_ratio'][level][0]
                self.assertEqual(member['metric'], expected)
                self.assertIn(expected, monitor._signal_counts_card(snap))
                self.assertEqual(snap['signal_counts']['vol_ratio'][level], 1)
                self.assertEqual(d, before)

    def test_detail_carries_z_but_page_never_prints_sigma(self):
        d = {'vol_ratio': 1.3, 'vol_ratio_z': 1.6, 'vol_ratio_date': '2026-10-13'}
        self.assertEqual(monitor._member_metric(d, 'vol_ratio'), '量比 1.3×（2026-10-13）')
        with patch.object(monitor, 'S', {**monitor.S, 'vol_ratio_z': 0}):
            self.assertEqual(monitor._member_metric(d, 'vol_ratio'), '量比 1.3×（2026-10-13）')
        # 自适应开着也不把 σ 写出来：用户看不懂，判定照用
        self.assertNotIn('σ', monitor._member_metric(d, 'vol_ratio'))
        self.assertEqual(monitor._member_metric(dict(d, vol_ratio_z=None), 'vol_ratio'),
                         '量比 1.3×（2026-10-13）')

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
        self.assertTrue(any(text == '量比 1.5×（2026-10-13）' for text in signals), signals)
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
        return {'symbol': 'MIX', 'indicators': markers, 'ma_settings': {'ma_short': 60, 'ma_long': 250}}

    def test_conflicting_directions_render_text(self):
        mixed = self.row([{'shape': '▲', 'tone': 'cyan', 'label': '上穿60日均线'},
                          {'shape': '▼', 'tone': 'cyan', 'label': '跌破250日均线'}])
        icons = monitor._member_icons(mixed, 'ma_cross', 'yellow')
        self.assertNotIn('▲', icons)
        self.assertNotIn('▼', icons)
        self.assertIn('短均线上穿，长均线跌破', icons)
        self.assertNotIn(' / ', icons)
        self.assertNotRegex(icons, r'\d+日(?:弱)?均线')

    def test_single_direction_still_draws_triangle(self):
        up = self.row([{'shape': '▲', 'tone': 'cyan', 'label': '上穿250日均线'}])
        self.assertEqual(monitor._member_icons(up, 'ma_cross', 'yellow'),
                         '<span class="signal-member-icon tone-cyan arrow-band-up" role="img" '
                         'aria-label="上穿长均线">▲</span>')

    def test_other_kinds_keep_icons(self):
        # 只有均线穿越做这条防冲突处理；布林/RSI 本身不会产生同格反向标记
        boll = self.row([{'shape': '▼', 'tone': 'yellow', 'label': '盘中跌破下轨'}])
        self.assertEqual(monitor._member_icons(boll, 'boll', 'yellow'),
                         '<span class="signal-member-icon tone-yellow arrow-band-down" role="img" '
                         'aria-label="盘中跌破下轨">▼</span>')


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

    def test_group_titles_and_member_icons_share_direction_classes_without_recoloring_tones(self):
        import copy
        for shape, cls in (('▲', 'arrow-band-up'), ('▼', 'arrow-band-down')):
            with self.subTest(shape=shape):
                self.assertEqual(monitor._grp_arrow(shape), f'<span class="grp-arrow {cls}">{shape}</span>')
                entry = self._entry('ARROW', 'position', shape)
                before = copy.deepcopy(entry)
                for kind in ('chg', 'amp', 'vol_ratio'):
                    self.assertEqual(monitor._member_icons(entry, kind, 'red'),
                                     f'<span class="signal-member-icon tone-white {cls}" role="img" '
                                     f'aria-label="方向">{shape}</span>')
                self.assertEqual(entry, before)
        for shape, cls, tone in (('↑', 'arrow-rsi-up', 'yellow'), ('↓', 'arrow-rsi-down', 'red')):
            entry = {'indicators': [{'shape': shape, 'tone': tone, 'label': 'RSI方向'}]}
            self.assertEqual(monitor._grp_arrow(shape), f'<span class="{cls}">{shape}</span>')
            self.assertEqual(monitor._member_icons(entry, 'rsi', tone),
                             f'<span class="signal-member-icon {cls}" role="img" '
                             f'aria-label="RSI方向">{shape}</span>')
        neutral = {'indicators': [{'shape': '■', 'tone': 'cyan', 'label': '触及长均线'}]}
        self.assertEqual(monitor._member_icons(neutral, 'ma_band', 'yellow'),
                         '<span class="signal-member-icon tone-cyan" role="img" '
                         'aria-label="触及长均线">■</span>')
        target = {'indicators': [{'shape': '□', 'tone': 'purple', 'label': '目标价空间'}]}
        self.assertEqual(monitor._member_icons(target, 'target', 'purple'),
                         '<span class="signal-member-icon tone-purple" role="img" '
                         'aria-label="目标价空间">□</span>')

    def test_up_block_then_divider_then_down_block(self):
        entries = [self._entry('DN_FOCUS', 'focus', '▼', '下备注'),
                   self._entry('UP_FOCUS', 'focus', '▲', '上备注'),
                   self._entry('DN_POS', 'position', '▼'),
                   self._entry('UP_POS', 'position', '▲')]
        body = self._panel(entries)
        self.assertNotIn('<h4>', body)                       # 没有分组小标题
        # 方向统一放到框标题（上涨绿／下跌红三角）：上涨一框在前、下跌一框在后
        self.assertNotIn('class="signal-member-split"', body)
        self.assertEqual(body.count('class="signal-member-block"'), 2)
        head, tail = body.split('<h5><span class="grp-arrow arrow-band-down">▼</span> 下跌</h5>', 1)
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
        self.assertEqual(body.count('class="signal-member-list"'), 2)   # 上涨一框 + 下跌一框

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
        # 顺序固定为 ▲ 框 → ▼ 框 → 方向不明的「平盘」框垫底
        self.assertEqual(body.count('class="signal-member-block"'), 3)
        up, rest = body.split('<h5><span class="grp-arrow arrow-band-down">▼</span> 下跌</h5>', 1)
        down, text = rest.split('<h5>平盘</h5>', 1)
        self.assertIn('UP_A', up)
        self.assertIn('DN_B', down)
        self.assertIn('TXT', text)
        self.assertIn('<h5>平盘</h5>', body)


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


class TestBollRsiRules(unittest.TestCase):
    """新版布林/RSI 报灯、文字折叠、灰名单与 signed gap 选轨的纯离线回归。"""

    def setUp(self):
        settings = patch.object(monitor, 'S', dict(monitor.DEFAULT_SETTINGS,
            near_52w_low_pct=0, ma_short=0, ma_long=0, boll_near_band_pct=0))
        settings.start()
        self.addCleanup(settings.stop)

    def analyze(self, values, group='position', hold='short', bands=(100, 110, 90),
                high=101, low=99, symbol='TEST', cfg=None, chg=0):
        closes = [100 + (.5 if i % 2 else -.5) for i in range(60)]
        previous = 100 / (1 + chg / 100)
        closes[-2:] = [previous, 100]
        data = dict(closes=closes, highs=[101] * 59 + [high], lows=[99] * 59 + [low],
                    volumes=[1000] * 60, price=100, prev_close=previous,
                    dates=['2026-10-08'] * 60, source='test')
        with patch.object(monitor, 'calc_rsi', side_effect=values), \
                patch.object(monitor, 'vol_profile', return_value=('mid', None)), \
                patch.object(monitor, 'boll', return_value=bands), \
                patch.object(monitor, 'boll_streak', return_value={'up': 0, 'dn': 0}), \
                patch.object(monitor, '_rule_amplitude', side_effect=lambda al, x: setattr(x, 'amp', None)):
            # 技术fixture仅隔离独立振幅，不放宽布林/RSI互斥或折叠断言。
            return monitor.analyze_symbol(symbol, {'hold': hold, **(cfg or {})}, data, group)[2]

    def test_three_oversold_standalone_and_combinations_are_exclusive_in_every_group(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for hold in ('short', 'long', 'LONG'):
                for bands in ((100, 110, 90), (100, 101, 90), (100, 110, 99),
                              (None, None, None)):
                    with self.subTest(group=group, hold=hold, bands=bands):
                        row = self.analyze([35, 30, 20], group, hold, bands)
                        upper, lower = bands[1] == 101, bands[2] == 99
                        expected = ('yellow' if group in monitor.BOLL_UP_SIGNAL_GROUPS else 'green') if upper else 'red'
                        kind = 'boll_rsi' if upper or lower else 'rsi'
                        self.assertEqual(row['level'], expected)
                        self.assertEqual(row['signal_marks'], [] if expected == 'green' else [(kind, expected)])
                        self.assertEqual(row['signals'].count('RSI 3条 ↓超卖（≤35）'), 1)
                        counts = monitor.signal_counts([row])
                        members = monitor.signal_count_members([row])
                        for indicator in ('rsi', 'boll', 'boll_rsi'):
                            for level in ('red', 'yellow'):
                                n = int(indicator == kind and expected == level)
                                self.assertEqual(counts[indicator][level], n)
                                self.assertEqual(len(members[indicator][level]), n)
                        self.assertEqual(bool(monitor.text_only_members([row])), expected == 'green')
                        if kind == 'rsi':
                            member = members['rsi']['red'][0]
                            self.assertEqual(member['rsi_hits'], 'RSI 3条 ↓超卖')
                            self.assertEqual(member['indicators'], [
                                {'shape': '↓', 'tone': 'red', 'label': 'RSI 3条 ↓超卖'}])

    def test_single_rsi_counts_respect_current_threshold_and_invalid_periods(self):
        monitor.S['rsi_low'] = 28
        for values, red in (([28, 28, 28], True), ([28.001, 28, 28], False),
                            ([None, 28, 28], False), ([float('nan'), 28, 28], False),
                            ([float('inf'), 28, 28], False)):
            with self.subTest(values=values):
                row = self.analyze(values)
                self.assertEqual(row['level'], 'red' if red else 'green')
                self.assertEqual(('rsi', 'red') in row['signal_marks'], red)

    def test_boll_matrix_and_only_designated_text_combinations_stay_visible(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for hold in ('short', 'long'):
                active_up = group in ('position', 'focus')
                for side, bands in (('上轨', (100, 101, 90)), ('下轨', (100, 110, 99))):
                    for high_n in range(4):
                        for low_n in range(4 - high_n):
                            values = [70] * high_n + [35] * low_n + [50] * (3 - high_n - low_n)
                            marks, kept = [], False
                            if side == '下轨':
                                if low_n >= 2:
                                    marks.append(('boll_rsi', 'red' if low_n == 3 else 'yellow'))
                                elif high_n >= 2:
                                    marks.append(('boll_rsi', 'yellow'))
                                kept = max(high_n, low_n) == 1 or group == 'position' and high_n == low_n == 0
                            else:
                                kept = (group == 'position' and low_n == 1 and max(high_n, low_n) == 1
                                        or group == 'focus' and max(high_n, low_n) == 2)
                                if low_n == 3 and active_up:
                                    marks.append(('boll_rsi', 'yellow'))
                                elif active_up and (high_n >= 2 or low_n == 2) and not (group == 'focus' and max(high_n, low_n) == 2):
                                    marks.append(('boll_rsi', 'red' if high_n == 3 else 'yellow'))
                            level = ('red' if any(lv == 'red' for _, lv in marks)
                                     else 'yellow' if marks else 'green')
                            with self.subTest(group=group, hold=hold, side=side,
                                              high_n=high_n, low_n=low_n):
                                row = self.analyze(values, group, hold, bands)
                                self.assertEqual(row['signal_marks'], marks)
                                self.assertEqual(row['level'], level)
                                counts = monitor.signal_counts([row])
                                members = monitor.signal_count_members([row])
                                for indicator in ('rsi', 'boll', 'boll_rsi'):
                                    for color in ('red', 'yellow'):
                                        n = int((indicator, color) in marks)
                                        self.assertEqual(counts[indicator][color], n)
                                        self.assertEqual(len(members[indicator][color]), n)
                                self.assertEqual(row['foldable'], not kept)
                                silent = side == '上轨' and group != 'position' and high_n == low_n == 0
                                self.assertEqual(any('布林' + side in text for text in row['signals']), not silent)
                                gray = monitor.text_only_members([row])
                                text_only = side == '上轨' and (
                                    group == 'focus' and high_n + low_n == 1
                                    or not active_up and max(high_n, low_n) in (1, 2))
                                if level == 'green' and not silent and not text_only:
                                    self.assertEqual(gray[0]['kind'], 'boll_rsi' if high_n or low_n else 'boll')
                                    self.assertEqual(gray[0]['side'], side)
                                    self.assertEqual(gray[0]['rsi_n'], max(high_n, low_n))
                                else:
                                    self.assertEqual(gray, [])

    def test_long_short_positions_have_identical_technical_rules(self):
        for bands in ((100, 110, 90), (100, 101, 90), (100, 110, 99), (None, None, None)):
            for high_n in range(4):
                for low_n in range(4 - high_n):
                    values = [70] * high_n + [35] * low_n + [50] * (3 - high_n - low_n)
                    short = self.analyze(values, hold='short', bands=bands)
                    for hold in ('long', 'LONG'):
                        with self.subTest(bands=bands, values=values, hold=hold):
                            row = self.analyze(values, hold=hold, bands=bands)
                            self.assertEqual(row['hold'], hold.lower())
                            for key in ('level', 'signals', 'signal_marks', 'foldable', 'boll_alert_text',
                                        'boll_rsi_direction', 'boll_rsi_hits'):
                                self.assertEqual(row.get(key), short.get(key), key)
                            self.assertEqual(monitor.signal_counts([row]), monitor.signal_counts([short]))
                            # 归属名称仍保留长短，只比较提醒与统计分类，不要求展示元数据相同。
                            gray = [{k: v for k, v in m.items() if k != 'hold'}
                                    for m in monitor.text_only_members([row])]
                            short_gray = [{k: v for k, v in m.items() if k != 'hold'}
                                          for m in monitor.text_only_members([short])]
                            self.assertEqual(gray, short_gray)

    def test_plain_upper_only_alerts_positions_and_plain_lower_stays_all_groups(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for hold in ('short', 'long'):
                with self.subTest(group=group, hold=hold):
                    row = self.analyze([50, 50, 50], group, hold, bands=(100, 101, 90))
                    members = monitor.text_only_members([row])
                    self.assertEqual(row['level'], 'green')
                    self.assertTrue(row['foldable'])
                    self.assertEqual(row['signal_marks'], [])
                    if group == 'position':
                        self.assertTrue(any('布林上轨' in text for text in row['signals']))
                        self.assertEqual((members[0]['kind'], members[0]['side']), ('boll', '上轨'))
                    else:
                        self.assertEqual(row['signals'], [])
                        self.assertEqual(row['boll_alert_text'], '')
                        self.assertEqual(members, [])
                    lower = self.analyze([50, 50, 50], group, hold, bands=(100, 110, 99))
                    lower_member = monitor.text_only_members([lower])[0]
                    self.assertEqual((lower_member['kind'], lower_member['side']), ('boll', '下轨'))

    def test_plain_dual_tracks_keep_facts_but_do_not_switch_after_upper_restriction(self):
        for group in ('focus', 'technology'):
            # 只隔离振幅灯，不改变选轨距离与分组限制。
            for high, low, side in ((120, 90, '上轨'), (110, 80, '下轨'), (110, 90, '上轨')):
                with self.subTest(group=group, high=high, low=low), \
                        patch.dict(monitor.S, {'amp_yellow': 1000, 'amp_red': 2000}):
                    row = self.analyze([50, 50, 50], group, high=high, low=low)
                self.assertTrue(any('布林上轨' in text for text in row['signals']))
                self.assertTrue(any('布林下轨' in text for text in row['signals']))
                self.assertIn('布林' + side, row['boll_alert_text'])
                gray = monitor.text_only_members([row])
                self.assertEqual([m['side'] for m in gray], ['下轨'] if side == '下轨' else [])
        # 有单条 RSI 的上轨仍是组合文字，不被“单独上轨零条”规则误删。
        for values in ([70, 50, 50], [35, 50, 50], [70, 35, 50]):
            for group in ('focus', 'technology'):
                row = self.analyze(values, group, bands=(100, 101, 90))
                self.assertTrue(any('布林上轨' in text for text in row['signals']))
                gray = monitor.text_only_members([row])
                self.assertEqual([m['kind'] for m in gray],
                                 ['boll_rsi'] if group == 'focus' and values == [70, 35, 50] else [])

    def test_rule_template_groups_positions_without_changing_position_layout(self):
        from page_assets import RULE_BLOCKS, LAMP_RULE_TABLE
        body = ''.join(part for _, part in RULE_BLOCKS)
        self.assertNotIn('长持仓', body)
        self.assertNotIn('短持仓', body)
        self.assertIn('${lamp_table}', body)
        self.assertIn('布林上轨▲ + RSI 2条 ↑超买', LAMP_RULE_TABLE)
        self.assertIn('布林上轨▲ + RSI 2条 ↓超卖（反）', LAMP_RULE_TABLE)
        self.assertIn('布林上轨▲ + RSI 0条', LAMP_RULE_TABLE)
        self.assertNotIn('RSI 下轨', LAMP_RULE_TABLE + body)

    def test_rsi_alone_and_plain_boll_remain_foldable(self):
        for values in ([50, 50, 50], [70, 50, 50], [35, 50, 50], [70, 35, 50],
                       [70, 70, 50], [35, 35, 50]):
            with self.subTest(values=values):
                row = self.analyze(values)
                self.assertEqual(row['level'], 'green')
                self.assertTrue(row['foldable'])
                self.assertEqual(row['signal_marks'], [])
                if max(values.count(70), values.count(35)) < 2:
                    self.assertEqual(row['signals'], ['RSI 1条 ↓超卖（≤35）'] if 35 in values else [])
        for bands in ((100, 101, 90), (100, 110, 99)):
            row = self.analyze([50, 50, 50], bands=bands)
            self.assertEqual(row['foldable'], bands[2] != 99)
            self.assertEqual(monitor.text_only_members([row])[0]['kind'], 'boll')

    def test_dual_tracks_keep_both_texts_and_use_signed_not_absolute_gap(self):
        cases = ((120, 89, '上轨', 'green', True),
                 (111, 80, '下轨', 'yellow', True),
                 (120, 80, '下轨', 'yellow', True),
                 (110, 90, '上轨', 'green', True))
        for high, low, selected, level, foldable in cases:
            with self.subTest(high=high, low=low):
                row = self.analyze([35, 35, 50], 'technology', high=high, low=low)
                self.assertTrue(any('布林上轨' in text for text in row['signals']))
                self.assertTrue(any('布林下轨' in text for text in row['signals']))
                self.assertIn('布林' + selected, row['boll_alert_text'])
                self.assertEqual((row['level'], row['foldable']), (level, foldable))
                self.assertEqual(('boll_rsi', 'yellow') in row['signal_marks'], selected == '下轨')
        # 被选上轨屏蔽的三超买组合，不用另一条下轨补黄；单条也不借下轨取消折叠。
        for values in ([70, 70, 70], [70, 50, 50]):
            row = self.analyze(values, 'technology', high=110, low=90)
            self.assertEqual(row['level'], 'green')
            self.assertTrue(row['foldable'])
            self.assertEqual(row['signal_marks'], [])

    def test_gray_combination_uses_selected_lower_track_and_single_rsi_title(self):
        row = self.analyze([70, 50, 50], 'technology', high=111, low=80)
        self.assertTrue('布林上轨' in row['signals'][0])
        member = monitor.text_only_members([row])[0]
        self.assertEqual((member['kind'], member['shape'], member['side'], member['rsi_n'], member['rsi_side']),
                         ('boll_rsi', '▼', '下轨', 1, '超买'))
        self.assertIn('跌破布林下轨▼ · RSI 1条 ↑超买', member['detail'])
        self.assertEqual(monitor._gray_title('boll_rsi', member), ('▼', '下轨▼ · RSI 1条 ↑超买', 1))
        self.assertEqual(monitor._gray_title('boll_rsi', {'detail': member['detail']}),
                         ('▼', '下轨▼ · RSI 1条 ↑超买', 1))

    def test_technical_keep_is_independent_of_other_red_price_signal(self):
        for values, bands, keep in (([70, 50, 50], (100, 110, 99), True),
                                    ([35, 35, 50], (100, 101, 90), False),
                                    ([35, 35, 50], (100, 110, 99), False),
                                    ([50, 50, 50], (100, 110, 90), False)):
            with self.subTest(values=values, bands=bands):
                row = self.analyze(values, 'technology', bands=bands, cfg={'trigger': 100})
                self.assertEqual(row['level'], 'red')
                self.assertIn(('add_price', 'red'), row['signal_marks'])
                self.assertEqual(row['foldable'], not keep)
                self.assertIn(f'data-quiet-keep="{str(keep).lower()}"', monitor._stock_row(row, {}))

    def test_group_body_keeps_designated_gray_rows_but_folds_other_text(self):
        group = next(g for g in monitor.GROUPS if g['item_group'] == 'technology')
        rows = [self.analyze([35, 35, 50], 'technology', bands=(100, 101, 90), symbol='UPLOW'),
                self.analyze([70, 50, 50], 'technology', bands=(100, 110, 99), symbol='DOWNONE'),
                self.analyze([70, 70, 50], 'technology', bands=(100, 101, 90), symbol='UPHIGH'),
                self.analyze([50, 50, 50], 'technology', bands=(100, 110, 99), symbol='PLAIN'),
                self.analyze([35, 35, 50], 'technology', symbol='RSIONLY')]
        body = monitor._group_body(rows, group, {}, {}, True, 'group_technology', group['label'])
        shown, quiet = body.split('<details class="quiet-list">', 1)
        for symbol in ('DOWNONE',):
            self.assertIn(f'id="s-{symbol.lower()}"', shown)
            self.assertNotIn(f'id="s-{symbol.lower()}"', quiet)
        self.assertIn('无异动 4 只', quiet)
        for symbol in ('UPLOW', 'UPHIGH', 'PLAIN', 'RSIONLY'):
            self.assertNotIn(f'id="s-{symbol.lower()}"', shown)
            self.assertIn(f'id="s-{symbol.lower()}"', quiet)
        counts = monitor.signal_counts(rows)
        self.assertTrue(all(v['red'] == v['yellow'] == v['purple'] == 0 for v in counts.values()))
        self.assertEqual({m['symbol'] for m in monitor.text_only_members(rows)},
                         {'DOWNONE', 'PLAIN', 'RSIONLY'})
        stats = monitor._group_stats_html(rows, group, {}, True)
        self.assertNotIn('stat-badge gray', stats)
        self.assertNotRegex(stats, r'(?:灰灯|白灯) 5 项')
        card = monitor._signal_counts_card({'signal_counts': counts,
            'signal_members': monitor.signal_count_members(rows), 'text_only': monitor.text_only_members(rows)})
        self.assertIn('<h5>下轨<span class="arrow-band-down">▼</span> · RSI 1条 <span class="arrow-rsi-up">↑</span>超买</h5>', card)
        self.assertNotIn('<h5>上轨<span class="arrow-band-up">▲</span> · RSI 2条 <span class="arrow-rsi-down">↓</span>超卖</h5>', card)

    def test_standalone_rsi_complete_group_matrix_includes_mixed_and_single_silence(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for hold in ('short', 'long'):
                for high_n in range(4):
                    for low_n in range(4 - high_n):
                        values = [70] * high_n + [35] * low_n + [50] * (3 - high_n - low_n)
                        expected = ('red' if low_n == 3 else 'yellow'
                                    if high_n == 3 and group == 'position' else 'green')
                        with self.subTest(group=group, hold=hold, high_n=high_n, low_n=low_n):
                            row = self.analyze(values, group, hold)
                            self.assertEqual(row['level'], expected)
                            self.assertEqual(row['signal_marks'], [] if expected == 'green' else [('rsi', expected)])
                            self.assertEqual(row['foldable'], not (group == 'focus' and high_n == 3))
                            counts, members = monitor.signal_counts([row]), monitor.signal_count_members([row])
                            for kind in ('rsi', 'boll', 'boll_rsi'):
                                for color in ('red', 'yellow'):
                                    n = int(kind == 'rsi' and color == expected)
                                    self.assertEqual(counts[kind][color], n)
                                    self.assertEqual(len(members[kind][color]), n)
                            gray = monitor.text_only_members([row])
                            gray_expected = (expected == 'green' and max(high_n, low_n) >= 2
                                             and not (group != 'position' and high_n == 2))
                            self.assertEqual(bool(gray), gray_expected)
                            if max(high_n, low_n) < 2:
                                self.assertEqual(row['signals'], ['RSI 1条 ↓超卖（≤35）'] if low_n else [])
                            if high_n and low_n and max(high_n, low_n) == 2:
                                directions = monitor._member_direction_markers(row, 'rsi')
                                self.assertEqual({m['shape'] for m in directions}, {'↑', '↓'})
                                self.assertEqual(len(directions), 2)
                                self.assertEqual(len(row['signals']), 2)
                                if gray_expected:
                                    self.assertIn('超买', gray[0]['detail'])
                                    self.assertIn('超卖', gray[0]['detail'])

    def test_dual_rail_full_group_matrix_keeps_facts_and_never_reselects(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for hold in ('short', 'long'):
                for high, low, side in ((120, 90, '上轨'), (110, 80, '下轨'), (110, 90, '上轨')):
                    for high_n in range(4):
                        for low_n in range(4 - high_n):
                            values = [70] * high_n + [35] * low_n + [50] * (3 - high_n - low_n)
                            expected = 'green'
                            if low_n == 3 and (side == '下轨' or group in ('position', 'focus')):
                                expected = 'yellow' if side == '上轨' else 'red'
                            elif side == '下轨':
                                expected = 'yellow' if max(high_n, low_n) >= 2 else 'green'
                            elif group in ('position', 'focus'):
                                expected = ('red' if high_n == 3 else 'yellow'
                                            if group == 'position' and (high_n == 2 or low_n == 2) else 'green')
                            with self.subTest(group=group, hold=hold, side=side,
                                              high_n=high_n, low_n=low_n, high=high, low=low), \
                                    patch.dict(monitor.S, {'amp_yellow': 1000, 'amp_red': 2000}):
                                row = self.analyze(values, group, hold, high=high, low=low)
                            self.assertEqual(row['level'], expected)
                            self.assertEqual(row['signal_marks'], [] if expected == 'green' else [('boll_rsi', expected)])
                            self.assertIn('布林' + side, row['boll_alert_text'])
                            self.assertTrue(any('布林上轨' in s for s in row['signals']))
                            self.assertTrue(any('布林下轨' in s for s in row['signals']))
                            gray = monitor.text_only_members([row])
                            gray_expected = (expected == 'green' and not
                                             (side == '上轨' and group != 'position' and high_n == low_n == 0)
                                             and not (side == '上轨' and group == 'focus' and high_n + low_n == 1)
                                             and not (side == '上轨' and group not in ('position', 'focus')
                                                      and max(high_n, low_n) in (1, 2)))
                            self.assertEqual(bool(gray), gray_expected)
                            if gray_expected:
                                self.assertEqual(gray[0]['side'], side)
                            counts = monitor.signal_counts([row])
                            self.assertTrue(all(not counts[k][c] for k in ('rsi', 'boll') for c in ('red', 'yellow')))
                            self.assertEqual(counts['boll_rsi']['red'], int(expected == 'red'))
                            self.assertEqual(counts['boll_rsi']['yellow'], int(expected == 'yellow'))

    def test_direction_markers_keep_mixed_rsi_in_separate_or_shared_text(self):
        for signals in (['RSI 2条 ↑超买（≥70）', 'RSI 1条 ↓超卖（≤35）'],
                        ['RSI 2条 ↑超买 / 1条 ↓超卖'],
                        ['RSI 1条 ↑超买（≥70）', 'RSI 2条 ↓超卖（≤35）']):
            with self.subTest(signals=signals):
                markers = monitor._member_direction_markers({'signals': signals}, 'rsi')
                self.assertEqual({m['shape'] for m in markers}, {'↑', '↓'})
                self.assertEqual({m['tone'] for m in markers}, {'yellow'})
        for side, color, shape in (('超买', 'yellow', '↑'), ('超卖', 'red', '↓')):
            row = {'signals': [f'RSI 3条{side}', f'RSI 3条{side}'], 'level': 'red'}
            self.assertEqual(monitor._member_direction_markers(row, 'rsi'),
                             [{'shape': shape, 'tone': color,
                               'label': monitor.rsi_hits_text(3, side)}])

    def test_cross_group_combinations_never_duplicate_boll_or_rsi_counts(self):
        groups = [g['item_group'] for g in monitor.GROUPS]
        for high_n, low_n in ((3, 0), (0, 3), (2, 1), (1, 2), (1, 1)):
            for bands in ((100, 101, 90), (100, 110, 99)):
                values = [70] * high_n + [35] * low_n + [50] * (3 - high_n - low_n)
                for index, primary in enumerate(groups):
                    with self.subTest(values=values, bands=bands, primary=primary):
                        first = self.analyze(values, primary, 'long', bands, symbol='BRK.B-US')
                        first['note'] = '优先归属'
                        later = self.analyze(values, primary, 'short', bands, symbol='BRK-B')
                        later['note'] = '同组后记录'
                        rows = [self.analyze(values, group, bands=bands, symbol='BRK.B')
                                for group in groups[index + 1:]]
                        rows += [first, later]
                        counts, members = monitor.signal_counts(rows), monitor.signal_count_members(rows)
                        self.assertEqual(counts, monitor.signal_counts([first]))
                        self.assertEqual(members, monitor.signal_count_members([first]))
                        self.assertTrue(all(counts[kind][color] == 0 for kind in ('rsi', 'boll')
                                            for color in ('red', 'yellow')))
                        self.assertEqual(monitor.text_only_members(rows), monitor.text_only_members([first]))
                        with patch.object(monitor, 'global_dca', return_value=None):
                            snap = monitor.build_snapshot({}, rows, {}, group_counts={})
                        self.assertEqual(snap['summary']['total'], 1)
                        self.assertEqual(snap['summary']['red'], int(first['level'] == 'red'))
                        self.assertEqual(snap['summary']['yellow'], int(first['level'] == 'yellow'))

    def test_combination_normalization_and_other_indicators_remain_independent(self):
        import copy
        row = self.analyze([35, 35, 35], 'position', bands=(100, 110, 99), symbol='STACK')
        row.update(price=100, prev_close=200, target=monitor.evaluate_target(
            100, 200, {'kind': 'manual', 'price': 190}, 'position', 3))
        row['signal_marks'] += [('rsi', 'red'), ('boll', 'yellow'), ('chg', 'red'),
                               ('amp', 'yellow'), ('vol_ratio', 'red'), ('ma_cross', 'yellow'),
                               ('ma_band', 'yellow'), ('ma_cross', 'yellow')]
        row['signals'] += ['上穿250日均线', '日内触及250日均线 ±1%区间']
        row['ma_band_direction'] = 'up'
        before = copy.deepcopy(row)
        with patch.object(monitor, 'global_dca', return_value=None):
            snap = monitor.build_snapshot({}, [row, dict(row, group='technology')], {}, group_counts={})
        self.assertEqual((snap['summary']['red'], snap['summary']['yellow'], snap['summary']['purple']), (1, 0, 1))
        for kind, color in (('boll_rsi', 'red'), ('chg', 'red'), ('amp', 'yellow'),
                            ('vol_ratio', 'red'), ('ma', 'yellow'), ('target', 'purple')):
            self.assertEqual(snap['signal_counts'][kind][color], 1)
            self.assertEqual(len(snap['signal_members'][kind][color]), 1)
        self.assertEqual(snap['signal_counts']['rsi']['red'], 0)
        self.assertEqual(snap['signal_counts']['boll']['yellow'], 0)
        self.assertEqual(len(snap['target_overlays']['space']), 1)
        self.assertEqual(len(snap['target_overlays']['reached']), 1)
        self.assertEqual(snap['text_only'], [])
        self.assertEqual(row, before)

    def test_gray_coexists_with_other_indicators_but_excludes_technical_lamps_and_bad_data(self):
        row = self.analyze([35, 35, 50], 'technology', symbol='TEXT')
        old_overbought = self.analyze([70, 70, 50], 'technology', symbol='OLDHIGH')
        self.assertEqual(monitor.text_only_members([row])[0]['kind'], 'rsi')
        # 技术灰与其他指标红黄及目标紫并存，成员携带最终行灯供交集使用。
        for color, marks in (('green', [('target', 'purple')]),
                             ('red', [('chg', 'red')]),
                             ('yellow', [('ma_band', 'yellow')]),
                             ('yellow', [('vol_ratio', 'yellow')])):
            with self.subTest(color=color, marks=marks):
                member, = monitor.text_only_members([dict(row, level=color, signal_marks=marks)])
                self.assertEqual((member['kind'], member['level']), ('rsi', color))
                control = dict(old_overbought, level=color, signal_marks=marks)
                self.assertEqual(monitor.text_only_members([control]), [])
                self.assertEqual([g['level'] for g in monitor._signal_description_groups(control)
                                  if g['kind'] == 'rsi'], [None])
                self.assertIn('RSI 2条 ↑超买（≥70）', control['signals'])
        for kind in ('boll', 'rsi', 'boll_rsi'):
            for color in ('red', 'yellow'):
                with self.subTest(kind=kind, color=color):
                    self.assertEqual(monitor.text_only_members([
                        dict(row, level=color, signal_marks=[(kind, color)])]), [])
        for changes in ({'price': None}, {'price': 0}, {'price': -1}, {'price': True},
                        {'price': float('nan')}, {'price': float('inf')},
                        {'level': 'gray'}, {'reference_only': True}):
            with self.subTest(changes=changes):
                self.assertEqual(monitor.text_only_members([dict(row, **changes)]), [])
        single = dict(row, signals=['RSI 1条 ↑超买（≥70）'])
        self.assertEqual(monitor.text_only_members([single]), [])
        for changes in ({'price': None}, {'level': 'gray'}, {'reference_only': True}):
            with self.subTest(changes=changes):
                excluded = dict(row, signal_marks=[('chg', 'red'), ('rsi', 'yellow')], **changes)
                self.assertTrue(all(not v[color] for v in monitor.signal_counts([excluded]).values()
                                    for color in ('red', 'yellow', 'purple')))
                self.assertTrue(all(not v[color] for v in monitor.signal_count_members([excluded]).values()
                                    for color in ('red', 'yellow', 'purple')))

    def test_stx_and_nee_gray_survive_real_change_lamps_and_filter_intersections(self):
        import copy
        import subprocess
        rows = []
        with patch.dict(monitor.S, {'chg_yellow': 2, 'chg_red': 4}):
            for symbol, values, bands, chg, color, kind in (
                ('STXY', [70, 70, 50], (100, 110, 90), 3, 'yellow', None),
                ('STXR', [35, 35, 50], (100, 110, 90), -5, 'red', 'rsi'),
                ('NEEY', [70, 50, 50], (100, 101, 90), 3, 'yellow', None),
            ):
                row = self.analyze(values, 'technology', bands=bands, symbol=symbol, chg=chg)
                self.assertEqual(row['level'], color)
                self.assertEqual(row['signal_marks'], [('chg', color)])
                self.assertEqual([(m['kind'], m['level']) for m in monitor.text_only_members([row])],
                                 [(kind, color)] if kind else [])
                technical = [g for g in monitor._signal_description_groups(row)
                             if g['kind'] in ('rsi', 'boll_rsi')]
                self.assertEqual([g['level'] for g in technical], ['gray' if kind else None])
                self.assertTrue(any('RSI' in s for s in row['signals']))
                rows.append(row)
        before = copy.deepcopy(rows)
        with patch.object(monitor, 'global_dca', return_value=None):
            snapshot = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual((snapshot['summary']['red'], snapshot['summary']['yellow']), (1, 2))
        self.assertEqual([m['symbol'] for m in snapshot['text_only']], ['STXR'])
        output = monitor._signal_counts_card(snapshot)
        data = TestMaMergedStatistics.filters(output)
        self.assertEqual({m['s']: m['l'] for m in data['chg']['members']},
                         {'STXY': 'yellow', 'STXR': 'red', 'NEEY': 'yellow'})
        self.assertEqual({m['s']: m['l'] for m in data['rsi']['members']}, {'STXR': 'red'})
        self.assertNotIn('boll_rsi', data)
        for kind in ('rsi', 'boll_rsi'):
            for color in ('red', 'yellow'):
                self.assertEqual(snapshot['signal_counts'][kind][color], 0)
        source = monitor.PAGE_INTERACTION_JS
        intersect = source[source.index('function intersectSignals('):source.index('function renderSignalFilter(')]
        code = intersect + ';const data=' + json.dumps(data, ensure_ascii=False) + ';'
        code += ('const results=[["rsi","chg"],["chg","rsi"],["boll_rsi","chg"],["chg","boll_rsi"]]'
                 '.map(kinds=>intersectSignals(data,kinds));process.stdout.write(JSON.stringify(results));')
        results = json.loads(subprocess.run(['node', '-e', code], capture_output=True,
                                           text=True, encoding='utf-8', check=True).stdout)
        self.assertEqual(results[2:], [{'pending': ['boll_rsi']}, {'pending': ['boll_rsi']}])
        for result, expected in zip(results[:2], ({'STXR': 'red'}, {'STXR': 'red'})):
            self.assertEqual({m['s']: m['l'] for m in result['members']}, expected)
            for member in result['members']:
                gray, = [d for d in member['details'] if d['l'] == 'gray']
                colored, = [d for d in member['details'] if d['k'] == 'chg']
                self.assertIn(gray['k'], ('rsi', 'boll_rsi'))
                self.assertEqual(colored['l'], expected[member['s']])
                self.assertNotRegex(gray['t'], r'灰灯|白灯|红灯|黄灯|布林\+RSI')
        self.assertEqual(rows, before)

    def test_combination_red_yellow_never_add_gray_or_duplicate_description_dots(self):
        for values, bands, color in (([35, 35, 35], (100, 110, 99), 'red'),
                                      ([35, 35, 50], (100, 110, 99), 'yellow')):
            with self.subTest(color=color):
                row = self.analyze(values, 'technology', bands=bands)
                self.assertEqual(row['signal_marks'], [('boll_rsi', color)])
                # 旧输入残留单独marks也只能投影为组合，不重复灯点或统计。
                row['signal_marks'] += [('rsi', color), ('boll', 'yellow')]
                self.assertEqual(monitor.text_only_members([row]), [])
                counts = monitor.signal_counts([row])
                self.assertEqual(counts['boll_rsi'][color], 1)
                for kind in ('rsi', 'boll'):
                    self.assertEqual((counts[kind]['red'], counts[kind]['yellow']), (0, 0))
                output = _row_signals(row)
                self.assertNotIn('signal-dot', output)
                self.assertEqual(re.findall(r'data-signal-level="([^"]+)"', output), [color])
                self.assertEqual(output.count('data-signal-kind="boll_rsi"'), 1)
                self.assertNotIn('data-signal-kind="rsi"', output)
                self.assertNotIn('data-signal-kind="boll"', output)
                self.assertNotIn('data-signal-level="gray"', output)

    def test_legacy_row_without_foldable_keeps_existing_folding(self):
        group = next(g for g in monitor.GROUPS if g['item_group'] == 'technology')
        row = self.analyze([35, 35, 50], 'technology', bands=(100, 101, 90))
        row.pop('foldable')
        body = monitor._group_body([row], group, {}, {}, True, 'group_technology', group['label'])
        self.assertIn('无异动 1 只', body)


class TestTechnicalFacts(unittest.TestCase):
    """纯事实契约、独立规则矩阵，以及生产 analyze_symbol 的真实分派/选轨。"""

    def setUp(self):
        self.analyzer = TestBollRsiRules()
        self.analyzer.setUp()
        self.addCleanup(self.analyzer.doCleanups)

    @staticmethod
    def expected(group, high, low, side):
        # 灯资格按 LAMP_RULE_TABLE 独立列举，折叠值沿用原契约，不读取生产分类结果。
        active = group in ('position', 'focus')
        none = (None, 'green', True)
        rsi_gray = ('rsi', 'gray', True)
        combo_gray = ('boll_rsi', 'gray', True)
        combo_keep = ('boll_rsi', 'gray', False)
        combo_yellow = ('boll_rsi', 'yellow', True)
        combo_red = ('boll_rsi', 'red', True)
        if side is None:
            table = {(0, 0): none, (1, 0): none, (0, 1): none, (1, 1): none,
                     (2, 0): rsi_gray if group == 'position' else none,
                     (2, 1): rsi_gray if group == 'position' else none,
                     (0, 2): rsi_gray, (1, 2): rsi_gray,
                     (3, 0): ('rsi', 'yellow' if group == 'position' else 'gray', group != 'focus'),
                     (0, 3): ('rsi', 'red', True)}
        elif side == '上轨':
            table = {(0, 0): ('boll', 'gray', True) if group == 'position' else none,
                     (1, 0): combo_gray if group == 'position' else none,
                     (0, 1): combo_keep if group == 'position' else none,
                     (1, 1): combo_keep if group == 'position' else combo_gray if group == 'focus' else none,
                     (2, 0): combo_keep if group == 'focus' else combo_yellow if active else none,
                     (2, 1): combo_keep if group == 'focus' else combo_yellow if active else none,
                     (0, 2): combo_keep if group == 'focus' else combo_yellow if active else none,
                     (1, 2): combo_keep if group == 'focus' else combo_yellow if active else none,
                     (3, 0): combo_red if active else combo_gray,
                     (0, 3): combo_yellow if active else combo_gray}
        else:
            table = {(0, 0): ('boll', 'gray', group != 'position'),
                     (1, 0): combo_keep, (0, 1): combo_keep, (1, 1): combo_keep,
                     (2, 0): combo_yellow, (2, 1): combo_yellow, (0, 2): combo_yellow,
                     (1, 2): combo_yellow, (3, 0): combo_yellow, (0, 3): combo_red}
        kind, level, foldable = table[(high, low)]
        return dict(kind=kind, level=level, foldable=foldable)

    def test_existing_lamp_rule_table_all_20_technical_conditions_match_every_group(self):
        import copy
        from html import unescape
        from page_assets import LAMP_RULE_TABLE

        # 直接取现有表格的灯资格并展开 colspan；表中灰灯用语不反推折叠契约。
        body = re.search(r'<tbody>(.*?)</tbody>', LAMP_RULE_TABLE, re.S).group(1)
        table_rows, category = [], ''
        for source in re.findall(r'<tr\b[^>]*>(.*?)</tr>', body, re.S)[:20]:
            heading = re.search(r'<th\b[^>]*>(.*?)</th>', source, re.S)
            if heading:
                category = unescape(plain_text(heading.group(1))).strip()
            self.assertIn(category, ('布林+RSI', '单独RSI', '单独布林'))
            cells = re.findall(r'<td\b([^>]*)>(.*?)</td>', source, re.S)
            condition = unescape(plain_text(cells[0][1])).strip()
            lamps = []
            for attrs, content in cells[1:]:
                span = re.search(r'\bcolspan="(\d+)"', attrs)
                lamps.extend([unescape(plain_text(content)).strip()] * (int(span[1]) if span else 1))
            self.assertEqual(len(lamps), 3, condition)
            table_rows.append((category, condition, lamps))
        self.assertEqual(len(table_rows), 20)
        self.assertEqual([category for category, _, _ in table_rows],
                         ['布林+RSI'] * 12 + ['单独RSI'] * 6 + ['单独布林'] * 2)
        self.assertEqual(len({condition for _, condition, _ in table_rows}), 20)

        groups = [g['item_group'] for g in monitor.GROUPS] + ['custom_group']
        self.assertEqual(len(groups), len(set(groups)))
        lamp_levels = {'红灯': 'red', '黄灯': 'yellow', '白灯': 'gray',
                       '白灯（折）': 'gray', '仅文字': 'green', '无提示': 'green'}
        bands = {None: (100, 110, 90), '上轨': (100, 101, 90), '下轨': (100, 110, 99)}
        checked, projections = 0, 0
        for category, condition, lamps in table_rows:
            count = int(re.search(r'RSI ([0-3])条', condition)[1])
            high = count if '超买' in condition else 0
            low = count if '超卖' in condition else 0
            side = '上轨' if '布林上轨' in condition else '下轨' if '布林下轨' in condition else None
            kind = {'布林+RSI': 'boll_rsi', '单独RSI': 'rsi', '单独布林': 'boll'}[category]
            values = [70] * high + [35] * low + [50] * (3 - high - low)
            for group in groups:
                column = 0 if group == 'position' else 1 if group == 'focus' else 2
                expected_level = lamp_levels[lamps[column]]
                with self.subTest(condition=condition, group=group, lamp=lamps[column]):
                    checked += 1
                    self.assertEqual(self.expected(group, high, low, side)['level'], expected_level)
                    self.assertEqual(monitor.classify_technical_facts(group, high, low, side)['level'],
                                     expected_level)
                    row = self.analyzer.analyze(values, group, bands=bands[side], symbol='TABLEFACT')
                    expected_marks = [(kind, expected_level)] if expected_level in ('red', 'yellow') else []
                    self.assertEqual(row['signal_marks'], expected_marks)
                    self.assertEqual(row['level'], expected_level if expected_marks else 'green')
                    has_description = (count > 0 if side else count >= 2 or low == 1)
                    if side and count == 0:
                        has_description = side == '下轨' or group == 'position'
                    if count and has_description:
                        self.assertTrue(any(f'RSI {count}条' in text for text in row['signals']))
                    if side and has_description:
                        self.assertTrue(any('布林' + side in text for text in row['signals']))
                    for independent in ('green', 'yellow', 'red'):
                        overall = ('red' if 'red' in (expected_level, independent) else 'yellow'
                                   if 'yellow' in (expected_level, independent) else 'green')
                        marks = expected_marks + ([('chg', independent)] if independent != 'green' else [])
                        signals = row['signals'] + ([f'异动 +{6 if independent == "red" else 3}%']
                                                    if independent != 'green' else [])
                        current = dict(row, level=overall, signal_marks=marks, signals=signals)
                        legacy = {k: v for k, v in current.items() if k not in ('foldable', 'boll_alert_text')}
                        for variant, item in (('current', current), ('legacy', legacy)):
                            with self.subTest(independent=independent, variant=variant):
                                projections += 1
                                before = copy.deepcopy(item)
                                self.assertEqual([(m['symbol'], m['kind'], m['level'])
                                                  for m in monitor.text_only_members([item])],
                                                 [('TABLEFACT', kind, overall)] if expected_level == 'gray' else [])
                                descriptions = monitor._signal_description_groups(item)
                                technical = [g for g in descriptions if g['kind'] in ('rsi', 'boll', 'boll_rsi')]
                                self.assertEqual([(g['kind'], g['level']) for g in technical],
                                                 [(kind, None if expected_level == 'green' else expected_level)]
                                                 if has_description else [])
                                self.assertEqual([(g['kind'], g['level']) for g in descriptions if g['kind'] == 'chg'],
                                                 [('chg', independent)] if independent != 'green' else [])
                                rendered = re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                                                      r'(?: data-signal-level="([^"]*)")?>', _row_signals(item))
                                self.assertEqual([(k, level) for k, level in rendered
                                                  if k in ('rsi', 'boll', 'boll_rsi')],
                                                 [(kind, '' if expected_level == 'green' else expected_level)]
                                                 if has_description else [])
                                self.assertEqual(item, before)
        self.assertEqual(checked, 20 * len(groups))
        self.assertEqual(projections, 20 * len(groups) * 6)

    def test_current_three_changes_preserve_independent_lamps_and_legacy_gray_projection(self):
        import copy
        for group in (g['item_group'] for g in monitor.GROUPS):
            cases = [([35, 50, 50], None), ([70, 35, 50], None),
                     ([70, 70, 50], None), ([70, 70, 35], None),
                     ([70, 35, 35], None), ([35, 50, 50], '上轨'),
                     ([70, 35, 50], '上轨')]
            for values, side in cases:
                high, low = values.count(70), values.count(35)
                facts = self.expected(group, high, low, side)
                bands = (100, 101, 90) if side else (100, 110, 90)
                for chg in (0, 3, 6):
                    for trigger in (None, 100):
                        with self.subTest(group=group, values=values, side=side, chg=chg, trigger=trigger):
                            self.assertEqual(monitor.classify_technical_facts(group, high, low, side), facts)
                            row = self.analyzer.analyze(values, group, bands=bands, chg=chg,
                                                        cfg={'trigger': trigger}, symbol='THREECHANGE')
                            before = copy.deepcopy(row)
                            overall = 'red' if trigger or chg == 6 else 'yellow' if chg else 'green'
                            self.assertEqual((row['level'], row['foldable']), (overall, facts['foldable']))
                            technical_marks = [m for m in row['signal_marks'] if m[0] in ('rsi', 'boll', 'boll_rsi')]
                            self.assertEqual(technical_marks, [])
                            rsi_texts = [s for s in row['signals'] if s.startswith('RSI ')]
                            self.assertEqual(len(rsi_texts), 1 if side is None and max(high, low) < 2
                                             else int(high > 0) + int(low > 0))
                            if side is None and max(high, low) < 2:
                                self.assertEqual(rsi_texts, ['RSI 1条 ↓超卖（≤35）'])
                                self.assertFalse(any('超买' in s for s in row['signals']))
                            self.assertEqual(row['signals'].count('RSI 1条 ↓超卖（≤35）'), int(low == 1))
                            gray = monitor.text_only_members([row])
                            gray_expected = facts['level'] == 'gray'
                            self.assertEqual([(m['kind'], m['level']) for m in gray],
                                             [(facts['kind'], overall)] if gray_expected else [])
                            legacy = {k: v for k, v in row.items() if k not in ('foldable', 'boll_alert_text')}
                            self.assertEqual(bool(monitor.text_only_members([legacy])), gray_expected)
                            with patch.object(monitor, 'global_dca', return_value=None):
                                snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
                            self.assertEqual(snapshot['text_only'], gray)
                            for kind in ('rsi', 'boll', 'boll_rsi'):
                                self.assertTrue(all(snapshot['signal_counts'][kind][color] == 0
                                                    for color in ('red', 'yellow', 'purple')))
                            self.assertEqual(snapshot['signal_counts']['chg']['red'], int(chg == 6))
                            self.assertEqual(snapshot['signal_counts']['chg']['yellow'], int(chg == 3))
                            self.assertEqual(snapshot['signal_counts']['add_price']['red'], int(trigger is not None))
                            self.assertEqual((snapshot['summary']['red'], snapshot['summary']['yellow']),
                                             (int(overall == 'red'), int(overall == 'yellow')))
                            technical = [g for g in monitor._signal_description_groups(row)
                                         if g['kind'] in ('rsi', 'boll', 'boll_rsi')]
                            self.assertEqual([(g['kind'], g['level']) for g in technical],
                                             [('boll_rsi' if side else 'rsi', 'gray' if gray_expected else None)])
                            output = monitor._stock_row(row, {})
                            self.assertIn('data-quiet-keep="' + str(not facts['foldable']).lower() + '"', output)
                            self.assertNotIn('signal-dot', output)
                            self.assertEqual(row, before)

    def test_nonposition_two_overbought_legacy_texts_do_not_become_gray_from_other_lamps(self):
        for signals in (['RSI 2条 ↑超买（≥70）'],
                        ['RSI 2条 ↑超买（≥70）', 'RSI 1条 ↓超卖（≤35）'],
                        ['RSI 2条 ↑超买 / 1条 ↓超卖']):
            for level in ('green', 'yellow', 'red'):
                with self.subTest(signals=signals, level=level):
                    row = dict(symbol='FOCUSLEGACY', group='focus', price=100, level=level,
                               signals=signals + (['异动 +6%'] if level == 'red' else []),
                               signal_marks=[('chg', level)] if level != 'green' else [])
                    self.assertEqual(monitor.text_only_members([row]), [])
                    self.assertEqual([g['level'] for g in monitor._signal_description_groups(row)
                                      if g['kind'] == 'rsi'], [None])
                    for group in (g['item_group'] for g in monitor.GROUPS):
                        control = dict(row, group=group)
                        self.assertEqual([m['kind'] for m in monitor.text_only_members([control])],
                                         ['rsi'] if group == 'position' else [])
                        self.assertEqual([g['level'] for g in monitor._signal_description_groups(control)
                                          if g['kind'] == 'rsi'], ['gray' if group == 'position' else None])
                    control = dict(row, signals=['RSI 2条 ↓超卖（≤35）', 'RSI 1条 ↑超买（≥70）'])
                    self.assertEqual(monitor.text_only_members([control])[0]['kind'], 'rsi')
                    for price in (None, True, 0, -1, float('nan'), float('inf')):
                        self.assertEqual(monitor.text_only_members([dict(control, price=price)]), [])
                    self.assertEqual(monitor.text_only_members([dict(control, reference_only=True)]), [])

    def test_four_authorized_changes_keep_statistics_and_other_lamps_independent(self):
        import copy
        cases = [('position', [50, 50, 50], (100, 110, 99), 'boll', 'gray', False),
                 ('focus', [70, 70, 70], (100, 110, 90), 'rsi', 'gray', False)]
        cases += [('focus', values, (100, 101, 90), 'boll_rsi', 'gray', False)
                  for values in ([70, 70, 50], [70, 70, 35], [35, 35, 50], [70, 35, 35])]
        for group in [g['item_group'] for g in monitor.GROUPS
                      if g['item_group'] not in ('position', 'focus')] + ['custom_group']:
            cases += [(group, values, (100, 101, 90), kind, level, True)
                      for values, kind, level in (([70, 50, 50], None, 'green'),
                                                  ([35, 50, 50], None, 'green'),
                                                  ([70, 35, 50], None, 'green'),
                                                  ([70, 70, 50], None, 'green'),
                                                  ([70, 70, 35], None, 'green'),
                                                  ([35, 35, 50], None, 'green'),
                                                  ([70, 35, 35], None, 'green'),
                                                  ([70, 70, 70], 'boll_rsi', 'gray'),
                                                  ([35, 35, 35], 'boll_rsi', 'gray'))]
        for group, values, bands, kind, level, foldable in cases:
            high, low = values.count(70), values.count(35)
            side = '下轨' if bands[2] == 99 else '上轨' if bands[1] == 101 else None
            for chg in (0, 3, 6):
                for trigger in (None, 100):
                    with self.subTest(group=group, values=values, chg=chg, trigger=trigger):
                        facts = dict(kind=kind, level=level, foldable=foldable)
                        self.assertEqual(monitor.classify_technical_facts(group, high, low, side), facts)
                        row = self.analyzer.analyze(values, group, bands=bands, chg=chg,
                                                    cfg={'trigger': trigger}, symbol='FOURCHANGE')
                        original = copy.deepcopy(row)
                        overall = 'red' if trigger is not None or chg == 6 else 'yellow' if chg else 'green'
                        self.assertEqual((row['level'], row['foldable']), (overall, foldable))
                        self.assertEqual([mark for mark in row['signal_marks'] if mark[0] in ('rsi', 'boll', 'boll_rsi')], [])
                        gray = monitor.text_only_members([row])
                        self.assertEqual([(member['kind'], member['level']) for member in gray],
                                         [(kind, overall)] if level == 'gray' else [])
                        # 旧结构只有信号文字时，上轨一二条正反向均无灰，三条仍保留合法灰。
                        legacy = {k: v for k, v in row.items() if k not in ('foldable', 'boll_alert_text')}
                        self.assertEqual(bool(monitor.text_only_members([legacy])), level == 'gray')
                        with patch.object(monitor, 'global_dca', return_value=None):
                            snap = monitor.build_snapshot({}, [row], {}, group_counts={})
                        for indicator in ('rsi', 'boll', 'boll_rsi'):
                            self.assertTrue(all(snap['signal_counts'][indicator][color] == 0
                                                for color in ('red', 'yellow', 'purple')))
                        self.assertEqual(snap['text_only'], gray)
                        self.assertEqual((snap['summary']['red'], snap['summary']['yellow']),
                                         (int(overall == 'red'), int(overall == 'yellow')))
                        self.assertEqual(snap['signal_counts']['chg']['red'], int(chg == 6))
                        self.assertEqual(snap['signal_counts']['chg']['yellow'], int(chg == 3))
                        self.assertEqual(snap['signal_counts']['add_price']['red'], int(trigger is not None))
                        groups = monitor._signal_description_groups(row)
                        technical = [g for g in groups if g['kind'] in ('rsi', 'boll', 'boll_rsi')]
                        self.assertEqual([(g['kind'], g['level']) for g in technical],
                                         [(kind or 'boll_rsi', 'gray' if level == 'gray' else None)])
                        output = monitor._stock_row(row, {})
                        self.assertIn('data-quiet-keep="' + str(not foldable).lower() + '"', output)
                        self.assertNotIn('signal-dot', output)
                        self.assertEqual(row, original)

    def test_changed_rules_respect_signed_dual_selection_and_upper_group_membership(self):
        groups = [g['item_group'] for g in monitor.GROUPS]
        for group in groups:
            for high_n, low_n in ((0, 0), (0, 1), (1, 1), (0, 2), (1, 2), (0, 3)):
                values = [70] * high_n + [35] * low_n + [50] * (3 - high_n - low_n)
                for day_high, day_low, side in ((120, 89, '上轨'), (111, 80, '下轨'),
                                                (120, 80, '下轨'), (110, 90, '上轨')):
                    with self.subTest(group=group, high_n=high_n, low_n=low_n, side=side), \
                            patch.dict(monitor.S, amp_yellow=1000, amp_red=2000):
                        row = self.analyzer.analyze(values, group, high=day_high, low=day_low)
                    facts = self.expected(group, high_n, low_n, side)
                    self.assertIn('布林' + side, row['boll_alert_text'])
                    self.assertTrue(any('布林上轨' in text for text in row['signals']))
                    self.assertTrue(any('布林下轨' in text for text in row['signals']))
                    self.assertEqual(row['foldable'], facts['foldable'])
                    gray = monitor.text_only_members([row])
                    self.assertEqual([m['side'] for m in gray], [side] if facts['level'] == 'gray' else [])
                    self.assertEqual(row['signal_marks'], [(facts['kind'], facts['level'])]
                                     if facts['level'] in ('red', 'yellow') else [])
        # 未登记的新组也按“不在 BOLL_UP_SIGNAL_GROUPS”判断；不能只特判 technology。
        self.assertEqual(monitor.classify_technical_facts('custom_group', 0, 2, '上轨'),
                         dict(kind=None, level='green', foldable=True))
        self.assertEqual(monitor.classify_technical_facts('custom_group', 0, 3, '上轨'),
                         dict(kind='boll_rsi', level='gray', foldable=True))
        with patch.object(monitor, 'BOLL_UP_SIGNAL_GROUPS', ('position', 'focus', 'custom_group')):
            self.assertEqual(monitor.classify_technical_facts('custom_group', 0, 2, '上轨'),
                             dict(kind='boll_rsi', level='yellow', foldable=True))
            self.assertEqual(monitor.classify_technical_facts('custom_group', 0, 3, '上轨'),
                             dict(kind='boll_rsi', level='yellow', foldable=True))

    def test_focus_upper_single_rsi_both_directions_are_text_without_gray(self):
        for group in ('position', 'focus', 'technology'):
            for values in ([70, 50, 50], [35, 50, 50]):
                for side, bands in (('上轨', (100, 101, 90)), ('下轨', (100, 110, 99))):
                    for chg in (0, 3, 6):
                        with self.subTest(group=group, values=values, side=side, chg=chg):
                            row = self.analyzer.analyze(values, group, bands=bands, chg=chg)
                            excluded = group != 'position' and side == '上轨'
                            gray = monitor.text_only_members([row])
                            self.assertEqual(bool(gray), not excluded)
                            self.assertTrue(any('布林' + side in s for s in row['signals']))
                            self.assertTrue(any(s.startswith('RSI 1条') for s in row['signals']))
                            keep = side == '下轨' or group == 'position' and values[0] == 35
                            self.assertEqual(row['foldable'], not keep)
                            self.assertFalse(any(k in ('boll', 'rsi', 'boll_rsi') for k, _ in row['signal_marks']))
                            self.assertEqual(row['level'], 'red' if chg == 6 else 'yellow' if chg == 3 else 'green')
                            # 旧快照仅有文字也按同一例外排除；其它独立红黄保持。
                            old = {k: v for k, v in row.items() if k not in ('foldable', 'boll_alert_text')}
                            self.assertEqual(bool(monitor.text_only_members([old])), not excluded)
                            with patch.object(monitor, 'global_dca', return_value=None):
                                snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
                            self.assertEqual(bool(snapshot['text_only']), not excluded)
                            if chg:
                                self.assertEqual(snapshot['signal_counts']['chg'][row['level']], 1)
        # 双向各一条：持仓/重点仍灰，其他上轨无灰；不改变各组折叠契约。
        for group in (g['item_group'] for g in monitor.GROUPS):
            row = self.analyzer.analyze([70, 35, 50], group, bands=(100, 101, 90))
            self.assertEqual([m['kind'] for m in monitor.text_only_members([row])],
                             ['boll_rsi'] if group in ('position', 'focus') else [])
            self.assertTrue(any('超买' in s for s in row['signals']))
            self.assertTrue(any('超卖' in s for s in row['signals']))
            self.assertEqual(row['foldable'], group != 'position')
        # 重点两条同侧技术灰不折，三条原红/黄不变。
        for values, expected in (([70, 70, 50], 'gray'), ([35, 35, 50], 'gray'),
                                  ([70, 70, 70], 'red'), ([35, 35, 35], 'yellow')):
            row = self.analyzer.analyze(values, 'focus', bands=(100, 101, 90))
            self.assertEqual(row['signal_marks'], [] if expected == 'gray' else [('boll_rsi', expected)])
            self.assertEqual(bool(monitor.text_only_members([row])), expected == 'gray')
            self.assertEqual(row['foldable'], expected != 'gray')

    def test_focus_single_rsi_dual_rail_respects_selected_side(self):
        for values in ([70, 50, 50], [35, 50, 50]):
            for high, low, side in ((120, 90, '上轨'), (110, 80, '下轨'), (110, 90, '上轨')):
                with self.subTest(values=values, side=side), patch.dict(monitor.S, amp_yellow=1000, amp_red=2000):
                    row = self.analyzer.analyze(values, 'focus', high=high, low=low)
                self.assertIn('布林' + side, row['boll_alert_text'])
                self.assertTrue(any('布林上轨' in s for s in row['signals']))
                self.assertTrue(any('布林下轨' in s for s in row['signals']))
                self.assertEqual([m['side'] for m in monitor.text_only_members([row])],
                                 ['下轨'] if side == '下轨' else [])

    def test_pure_api_complete_group_count_and_selected_side_matrix(self):
        with patch.object(monitor, 'calc_rsi', side_effect=AssertionError('纯API不得计算RSI')), \
                patch.object(monitor, 'boll', side_effect=AssertionError('纯API不得计算布林')), \
                patch.object(monitor, '_boll_hits', side_effect=AssertionError('纯API不得猜测轨距')), \
                patch.object(monitor, 'analyze_symbol', side_effect=AssertionError('纯API不得伪造行情')):
            for group in [g['item_group'] for g in monitor.GROUPS] + ['custom_group']:
                for high in range(4):
                    for low in range(4 - high):
                        for side in (None, '上轨', '下轨'):
                            with self.subTest(group=group, high=high, low=low, side=side):
                                self.assertEqual(monitor.classify_technical_facts(group, high, low, side),
                                                 self.expected(group, high, low, side))

    def test_invalid_count_types_ranges_sums_and_ambiguous_side_are_rejected(self):
        bad_counts = ((True, 0), (0, False), (1.0, 0), (0, 2.0), ('1', 0), (0, None),
                      (float('nan'), 0), ([], 0), (0, {}), (-1, 0), (0, -1),
                      (4, 0), (0, 4), (2, 2), (3, 1), (1, 3), (3, 3))
        for high, low in bad_counts:
            with self.subTest(high=high, low=low), self.assertRaises(ValueError):
                monitor.classify_technical_facts('position', high, low, None)
        for side in ('', '上下轨', 'both', 'up', 'down', [], {}, True, 0):
            with self.subTest(side=side), self.assertRaises(ValueError):
                monitor.classify_technical_facts('position', 0, 3, side)
        for group in (None, '', {}, True):
            with self.subTest(group=group), self.assertRaises(ValueError):
                monitor.classify_technical_facts(group, 0, 3, None)
        with self.assertRaises(TypeError):
            monitor.classify_technical_facts('position', 0, 3)

    def test_none_means_no_selected_rail_and_results_are_fresh(self):
        expected = dict(kind='rsi', level='red', foldable=True)
        result = monitor.classify_technical_facts('technology', 0, 3, None)
        self.assertEqual(result, expected)
        result['kind'] = 'boll_rsi'
        result['level'] = 'gray'
        with patch.object(monitor, 'S', dict(rsi_high=90, rsi_low=10, boll_near_pct=100)):
            self.assertEqual(monitor.classify_technical_facts('technology', 0, 3, None), expected)

    def test_production_single_rail_and_no_rail_dispatch_match_pure_api(self):
        bands = {None: (100, 110, 90), '上轨': (100, 101, 90), '下轨': (100, 110, 99)}
        classify = monitor.classify_technical_facts
        for group in (g['item_group'] for g in monitor.GROUPS):
            for hold in ('short', 'long'):
                for high in range(4):
                    for low in range(4 - high):
                        values = [70] * high + [35] * low + [50] * (3 - high - low)
                        for side in (None, '上轨', '下轨'):
                            with self.subTest(group=group, hold=hold, high=high, low=low, side=side), \
                                    patch.object(monitor, 'classify_technical_facts', wraps=classify) as dispatch:
                                row = self.analyzer.analyze(values, group, hold, bands=bands[side])
                            dispatch.assert_called_once_with(group, high, low, side)
                            facts = self.expected(group, high, low, side)
                            gray = monitor.text_only_members([row])
                            kind = row['signal_marks'][0][0] if row['signal_marks'] else gray[0]['kind'] if gray else None
                            level = row['level'] if row['signal_marks'] else 'gray' if gray else 'green'
                            self.assertEqual(dict(kind=kind, level=level, foldable=row['foldable']), facts)
                            expected_marks = [(facts['kind'], facts['level'])] if facts['level'] in ('red', 'yellow') else []
                            self.assertEqual(row['signal_marks'], expected_marks)
                            counts = monitor.signal_counts([row])
                            for indicator in ('rsi', 'boll', 'boll_rsi'):
                                for color in ('red', 'yellow'):
                                    self.assertEqual(counts[indicator][color], int((indicator, color) in expected_marks))
                            self.assertIn(f'data-quiet-keep="{str(not facts["foldable"]).lower()}"',
                                          monitor._stock_row(row, {}))

    def test_real_dual_rail_selection_is_passed_to_api_without_switching(self):
        classify = monitor.classify_technical_facts
        for group in (g['item_group'] for g in monitor.GROUPS):
            for high, low in ((0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (0, 3), (3, 0), (1, 2), (2, 1)):
                values = [70] * high + [35] * low + [50] * (3 - high - low)
                for day_high, day_low, side in ((120, 90, '上轨'), (110, 80, '下轨'), (110, 90, '上轨')):
                    with self.subTest(group=group, high=high, low=low, side=side), \
                            patch.dict(monitor.S, amp_yellow=1000, amp_red=2000), \
                            patch.object(monitor, 'classify_technical_facts', wraps=classify) as dispatch:
                        row = self.analyzer.analyze(values, group, high=day_high, low=day_low)
                    dispatch.assert_called_once_with(group, high, low, side)
                    facts = self.expected(group, high, low, side)
                    self.assertIn('布林' + side, row['boll_alert_text'])
                    self.assertTrue(any('布林上轨' in text for text in row['signals']))
                    self.assertTrue(any('布林下轨' in text for text in row['signals']))
                    self.assertEqual(row['level'], facts['level'] if facts['level'] in ('red', 'yellow') else 'green')
                    self.assertEqual(row['foldable'], facts['foldable'])
                    self.assertTrue(all(mark[0] == 'boll_rsi' for mark in row['signal_marks']))

    def test_real_rsi_boll_and_history_use_api_for_all_three_oversold_paths(self):
        import copy
        closes = [100.0 - i * 0.1 for i in range(80)]
        dates = monitor.market_sessions.preceding_sessions('2026-10-20', 79) + ['2026-10-20']
        base = dict(dates=dates, closes=closes, highs=[p + 0.05 for p in closes],
                    lows=[p - 0.05 for p in closes], volumes=[1000] * 80,
                    price=closes[-1], prev_close=closes[-2], source='offline_fixture')
        classify = monitor.classify_technical_facts
        for group in (g['item_group'] for g in monitor.GROUPS):
            for hold in ('short', 'long'):
                for side in (None, '上轨', '下轨'):
                    data = copy.deepcopy(base)
                    if side == '上轨':
                        data['highs'][-1] = 200.0
                    elif side == '下轨':
                        data['lows'][-1] = 20.0
                    before = copy.deepcopy(data)
                    with self.subTest(group=group, hold=hold, side=side), \
                            patch.dict(monitor.S, boll_near_pct=0, chg_yellow=1000, chg_red=2000,
                                       amp_yellow=1000, amp_red=2000), \
                            patch.object(monitor, '_rule_amplitude', side_effect=lambda al, x: setattr(x, 'amp', None)), \
                            patch.object(monitor, 'classify_technical_facts', wraps=classify) as dispatch:
                        row = monitor.analyze_symbol('TEST', {'hold': hold}, data, group)[2]
                    dispatch.assert_called_once_with(group, 0, 3, side)
                    self.assertTrue(all(value <= monitor.S['rsi_low'] for value in row['rsi'].values()))
                    gray = side == '上轨' and group not in monitor.BOLL_UP_SIGNAL_GROUPS
                    color = 'yellow' if side == '上轨' else 'red'
                    self.assertEqual(row['signal_marks'], [] if gray else [('rsi' if side is None else 'boll_rsi', color)])
                    self.assertEqual(row['level'], 'green' if gray else color)
                    self.assertTrue(row['foldable'])
                    self.assertEqual(bool(monitor.text_only_members([row])), gray)
                    self.assertEqual(data, before)


class TestAmplitudeChain(unittest.TestCase):
    """独立振幅整链：真实规则、身份优先级、技术互斥与灰灯统计。"""

    def setUp(self):
        self.enterContext(patch.object(monitor, 'S', dict(monitor.DEFAULT_SETTINGS,
            amp_yellow=6, amp_red=10, near_52w_low_pct=0, ma_short=0, ma_long=0, boll_near_band_pct=0)))
        self.enterContext(patch.object(monitor, 'global_dca', return_value=None))

    def analyze(self, symbol='AMPCHAIN', group='technology', high=101, low=99, prev=100,
                values=(50, 50, 50), bands=(100, 110, 90), cfg=None, ratio=1):
        closes = [100 + (.5 if i % 2 else -.5) for i in range(60)]
        closes[-2:] = [prev, 100]
        data = dict(closes=closes, highs=[101] * 59 + [high], lows=[99] * 59 + [low],
                    volumes=[1000] * 60, price=100, prev_close=prev,
                    dates=['2026-10-08'] * 60, source='offline_fixture')
        with patch.object(monitor, 'calc_rsi', side_effect=values), \
                patch.object(monitor, 'vol_profile', return_value=('mid', None)), \
                patch.object(monitor, 'boll', return_value=bands), \
                patch.object(monitor, 'boll_streak', return_value={'up': 0, 'dn': 0}), \
                patch.object(monitor, 'completed_day_volume_ratio', return_value=(ratio, '2026-10-08')), \
                patch.object(monitor, 'completed_day_volume_distribution', return_value=None), \
                patch.object(monitor, 'intraday_volume_ratio', return_value=(None, '')):
            return monitor.analyze_symbol(symbol, cfg or {}, data, group)[2]

    def test_all_groups_include_zero_and_use_amplitude_thresholds_without_gate(self):
        self.assertEqual(monitor.AMP_GROUPS, ('position', 'focus'))  # 仅验证旧导出兼容，不用它生成测试分组。
        for group in [g['item_group'] for g in monitor.GROUPS] + ['custom_group']:
            for amp in (0, .01, 2, 6, 10, 12):
                for yellow, red in ((6, 10), (1000, 2000)):
                    with self.subTest(group=group, amp=amp, yellow=yellow, red=red), \
                            patch.dict(monitor.S, amp_yellow=yellow, amp_red=red), \
                            patch.object(monitor, 'AMP_GROUPS', ()):
                        row = self.analyze(group=group, high=100 + amp / 2, low=100 - amp / 2)
                        level = 'red' if amp >= red else 'yellow' if amp >= yellow else 'green'
                        self.assertAlmostEqual(row['amp'], amp)
                        self.assertEqual((row['level'], row['foldable'], row['signal_marks']),
                                         (level, True, [] if level == 'green' else [('amp', level)]))
                        self.assertEqual(monitor.text_only_members([row]), [])
                        self.assertEqual(monitor._signal_description_groups(row), [] if level == 'green' else
                                         [{'kind': 'amp', 'level': level, 'texts': row['signals']}])
                        self.assertEqual(monitor.signal_counts([row])['amp'],
                                         {'label': '振幅', 'red': int(level == 'red'),
                                          'yellow': int(level == 'yellow'), 'purple': 0})

    def test_all_group_threshold_boundaries_are_red_yellow_or_silent(self):
        for group in [g['item_group'] for g in monitor.GROUPS] + ['custom_group']:
            for amp, level in ((0, 'green'), (5.999, 'green'), (6, 'yellow'),
                               (9.999, 'yellow'), (10, 'red'), (12, 'red')):
                with self.subTest(group=group, amp=amp):
                    row = self.analyze(group=group, high=100 + amp / 2, low=100 - amp / 2)
                    self.assertEqual(row['level'], level)
                    self.assertTrue(row['foldable'])
                    self.assertEqual(row['signal_marks'], [] if level == 'green' else [('amp', level)])
                    self.assertEqual(monitor.text_only_members([row]), [])
                    self.assertEqual(monitor.signal_counts([row])['amp'][level if level != 'green' else 'yellow'],
                                     int(level != 'green'))
            with patch.dict(monitor.S, amp_yellow=3, amp_red=9):
                self.assertEqual(self.analyze(group=group, high=101.5, low=98.5)['signal_marks'], [('amp', 'yellow')])
                self.assertEqual(self.analyze(group=group, high=104.5, low=95.5)['signal_marks'], [('amp', 'red')])

    def test_amplitude_red_and_technical_gray_share_identity_independently(self):
        import copy
        row = self.analyze(symbol='BRK.B-US', high=106, low=94, values=(35, 35, 50))
        row['note'] = '优先备注'
        later = self.analyze(symbol='BRK-B', group='healthcare', values=(35, 35, 50))
        rows = [later, row, dict(row, note='同组后备注')]
        before = copy.deepcopy(rows)
        members = monitor.text_only_members(rows)
        self.assertEqual([(m['symbol'], m['kind'], m['note']) for m in members],
                         [('BRK-B', 'rsi', '优先备注')])
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(snap['text_only'], members)
        self.assertEqual((snap['summary']['total'], snap['summary']['red']), (1, 1))
        self.assertEqual(snap['signal_counts']['amp']['red'], 1)
        card = monitor._signal_counts_card(snap)
        self.assertIn('aria-label="白 1 项"', card)
        self.assertNotRegex(card, r'aria-label="(?:灰灯|白灯) · 振幅')
        self.assertEqual(card.count('aria-label="白灯 · RSI · 1 只'), 1)
        self.assertEqual(rows, before)

    def test_gray_survives_independent_lamps_and_never_duplicates_combo_technical_counts(self):
        for values, bands, technical in (((50, 50, 50), (100, 110, 90), []),
                                        ((35, 35, 50), (100, 110, 90), ['rsi']),
                                        ((70, 70, 50), (100, 110, 90), []),
                                        ((70, 50, 50), (100, 110, 99), ['boll_rsi']),
                                        ((35, 35, 35), (100, 110, 99), [])):
            with self.subTest(values=values, bands=bands), patch.dict(monitor.S, ma_long=20), \
                    patch.object(monitor, 'TARGET_DATE', '2026-10-08'):
                row = self.analyze(values=values, bands=bands, high=106, low=94, prev=100 / 1.06,
                                   cfg={'trigger': 100, 'target': 190}, ratio=3)
                marks = row['signal_marks']
                for mark in (('chg', 'red'), ('add_price', 'red'), ('vol_ratio', 'red'),
                             ('amp', 'red'), ('ma_band', 'yellow')):
                    self.assertIn(mark, marks)
                self.assertEqual((row['level'], row['foldable'], row['purple']),
                                 ('red', values != (70, 50, 50), True))
                self.assertEqual([m['kind'] for m in monitor.text_only_members([row])], technical)
                snap = monitor.build_snapshot({}, [row], {}, group_counts={})
                for kind, level in (('chg', 'red'), ('add_price', 'red'), ('vol_ratio', 'red'),
                                    ('amp', 'red'), ('ma', 'yellow'), ('target', 'purple')):
                    self.assertEqual(snap['signal_counts'][kind][level], 1)
                combo_red = values == (35, 35, 35)
                self.assertEqual(snap['signal_counts']['boll_rsi']['red'], int(combo_red))
                self.assertTrue(all(snap['signal_counts'][k][c] == 0 for k in ('boll', 'rsi')
                                    for c in ('red', 'yellow')))
                self.assertEqual((snap['summary']['red'], snap['summary']['purple']), (1, 1))
                self.assertTrue(all(m['level'] == 'red' for m in snap['text_only']))

    def test_filter_red_amplitude_and_upper_three_reverse_are_exact_compact_facts(self):
        import copy
        previous = 100 / 1.06
        row = self.analyze(symbol='FACTGRAY', high=100 + previous * .12 / 2,
                           low=100 - previous * .12 / 2, values=(35, 35, 35),
                           bands=(100, 101, 90), prev=previous, ratio=1.5)
        self.assertAlmostEqual(row['amp'], 12)
        self.assertEqual(row['level'], 'red')
        self.assertIn(('amp', 'red'), row['signal_marks'])
        self.assertIn(('vol_ratio', 'yellow'), row['signal_marks'])
        before = copy.deepcopy(row)
        snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
        snapshot_before = copy.deepcopy(snapshot)
        page = monitor._signal_counts_card(snapshot)
        data = TestMaMergedStatistics.filters(page)
        self.assertEqual(data['amp']['members'][0]['d'], [{'l': 'red', 't': '振幅 12.0%'}])
        self.assertEqual(data['boll_rsi']['members'][0]['d'], [
            {'l': 'gray', 't': '布林上轨▲ · RSI 3条 ↓超卖（反）'}])
        self.assertEqual(data['chg']['members'][0]['d'][0]['l'], 'red')
        self.assertEqual(data['vol_ratio']['members'][0]['d'][0]['l'], 'yellow')
        self.assertEqual(snapshot['signal_counts']['vol_ratio']['yellow'], 1)
        self.assertEqual(snapshot['signal_counts']['boll_rsi']['yellow'], 0)
        self.assertEqual(snapshot['signal_counts']['boll_rsi']['red'], 0)
        for kind, condition in data.items():
            for member in condition['members']:
                self.assertEqual(member['l'], 'red')
                for fact in member['d']:
                    self.assertEqual(set(fact), {'l', 't'})
                    self.assertNotRegex(fact['t'], r'灰灯|白灯|红灯|黄灯|布林\+RSI')
        self.assertEqual(row, before)
        self.assertEqual(snapshot, snapshot_before)

    def test_invalid_amplitude_missing_price_reference_and_priority_never_leak_gray(self):
        for fields in ({'prev': 0}, {'prev': -100}, {'high': float('nan')},
                       {'high': float('inf')}, {'low': -float('inf')}):
            with self.subTest(fields=fields):
                row = self.analyze(**fields)
                self.assertNotIn(('amp', 'gray'), row['signal_marks'])
                self.assertFalse(any(s.startswith('振幅') for s in row['signals']))
                self.assertEqual([m for m in monitor.text_only_members([row]) if m['kind'] == 'amp'], [])
        row = self.analyze(symbol='BRK-B', values=(35, 35, 50))
        self.assertEqual([m['kind'] for m in monitor.text_only_members([row])], ['rsi'])
        for changes in ({'level': 'gray'}, {'reference_only': True}, {'price': None}, {'price': True},
                        {'price': 0}, {'price': -1}, {'price': float('nan')}, {'price': float('inf')}):
            with self.subTest(changes=changes):
                self.assertEqual(monitor.text_only_members([dict(row, **changes)]), [])
        for symbol in monitor.RISK_IDENTITIES:
            self.assertEqual(monitor.text_only_members([dict(row, symbol=symbol)]), [])
        for amp in (None, True, -1, float('nan'), float('inf')):
            self.assertEqual([m for m in monitor.text_only_members([dict(row, amp=amp)]) if m['kind'] == 'amp'], [])
        for group in [g['item_group'] for g in monitor.GROUPS] + ['custom_group']:
            self.assertEqual([m for m in monitor.text_only_members([dict(row, group=group)]) if m['kind'] == 'amp'], [])
        # 优先条目即使静默、失败或参考，也不能借低优先分组补灰。
        for first in (self.analyze(symbol='BRK.B-US', group='position'),
                      dict(row, symbol='BRK.B-US', group='focus', level='gray', price=None),
                      dict(row, symbol='BRK.B-US', group='focus', reference_only=True)):
            self.assertEqual(monitor.text_only_members([row, first]), [])
            snap = monitor.build_snapshot({}, [row, first], {}, group_counts={})
            self.assertEqual(snap['text_only'], [])
            self.assertEqual(snap['summary']['total'], 1)

    def test_legacy_amplitude_gray_is_filtered_without_mutating_snapshot(self):
        import copy
        row = self.analyze(symbol='LEGACYTECH', high=106, low=94, values=(35, 35, 50))
        current = monitor.build_snapshot({}, [row], {}, group_counts={})
        retired = [{'symbol': 'LEGACYAMP', 'group': 'technology', 'kind': 'amp',
                    'level': 'green', 'metric': '振幅 0.0%'},
                   {'symbol': row['symbol'], 'group': row['group'], 'kind': 'amp',
                    'level': 'red', 'metric': '振幅 12.0%'}]
        for field in (None, 'items', 'stocks'):
            legacy = copy.deepcopy(current)
            legacy['text_only'] += retired
            if field:
                legacy[field] = [copy.deepcopy(row)]
            before = copy.deepcopy(legacy)
            clean = monitor._sanitize_snapshot(legacy)
            self.assertEqual(clean['text_only'], current['text_only'])
            self.assertEqual(legacy, before)
            for source in (legacy, clean):
                card = monitor._signal_counts_card(source)
                self.assertIn('aria-label="白 1 项"', card)
                self.assertNotRegex(card, r'aria-label="(?:灰灯|白灯) · 振幅')
                self.assertNotIn('LEGACYAMP', card)
                data = TestMaMergedStatistics.filters(card)
                self.assertEqual(data['amp']['members'][0]['d'], [{'l': 'red', 't': '振幅 12.0%'}])
                self.assertEqual(data['rsi']['members'][0]['d'][0]['l'], 'gray')
            self.assertEqual(legacy, before)
        amp_only = {'text_only': retired[:1], 'summary': {'gray': 1},
                    'signal_counts': {'amp': {'label': '振幅', 'red': 0, 'yellow': 0}}}
        before = copy.deepcopy(amp_only)
        self.assertEqual(monitor._sanitize_snapshot(amp_only)['text_only'], [])
        self.assertEqual(monitor._signal_counts_card(amp_only), '')
        self.assertEqual(amp_only, before)


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

    def test_change_and_amplitude_red_apply_to_all_groups(self):
        big = self.data(self.series(1.8), 9.0, hi_pct=6.0, lo_pct=6.0)
        for grp in [g['item_group'] for g in monitor.GROUPS] + ['custom_group']:
            lv, sig, details = monitor.analyze_symbol('X', {}, big, group=grp)
            self.assertEqual(lv, 'red', (grp, sig))
            self.assertTrue(any('异动' in text for text in sig), (grp, sig))
            self.assertTrue(any('振幅' in text for text in sig), (grp, sig))
            self.assertIn(('amp', 'red'), details['signal_marks'])
            self.assertIn(('chg', 'red'), [tuple(mark) for mark in details['signal_marks']])
            self.assertEqual([m for m in monitor.text_only_members([details]) if m['kind'] == 'amp'], [])

    def test_change_threshold_boundaries_and_both_directions_in_all_groups(self):
        for grp in [g['item_group'] for g in monitor.GROUPS] + ['custom_group']:
            for tier, sigma, mult in (('low', 1.0, .6), ('mid', 2.0, 1), ('high', 3.0, 1.4)):
                for sign in (-1, 1):
                    for move, expected in ((2 * mult - .001, None), (2 * mult, 'yellow'), (4 * mult, 'red')):
                        with self.subTest(group=grp, tier=tier, sign=sign, move=move):
                            x = monitor.SimpleNamespace(group=grp, closes=[], chg=sign * move)
                            with patch.object(monitor, 'vol_profile', return_value=(tier, sigma)):
                                monitor._apply_vol_tier(x)
                            alerts = monitor._Alerts()
                            monitor._rule_chg_red(alerts, x)
                            monitor._rule_chg_yellow(alerts, x)
                            self.assertEqual(alerts.marks, [('chg', expected)] if expected else [])

    def test_other_group_change_is_in_snapshot_counts_and_members(self):
        _, _, details = monitor.analyze_symbol('OTHER', {}, self.data(self.series(1.8), 9.0), group='technology')
        snap = monitor.build_snapshot({}, [details], {}, group_counts={})
        self.assertEqual(snap['signal_counts']['chg']['red'], 1)
        self.assertEqual(snap['signal_members']['chg']['red'][0]['symbol'], 'OTHER')


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
                patch.object(monitor, 'boll', return_value=(None, None, None)), \
                patch.object(monitor, '_rule_amplitude', side_effect=lambda al, x: setattr(x, 'amp', None)):
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
        # 本用例只验证均线整合；全组振幅已改红黄，单独提高其门槛避免合法振幅红灯干扰。
        with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250, 'chg_yellow': 20, 'chg_red': 30,
                                    'amp_yellow': 20, 'amp_red': 30}), \
                patch.object(monitor, 'boll', return_value=(None, None, None)), \
                patch.object(monitor, 'calc_rsi', return_value=50):
            level, signals, detail = monitor.analyze_symbol('T', {}, data, group='technology')
        self.assertEqual(level, 'yellow')
        self.assertIn('上穿250日均线', signals)
        self.assertTrue(any('日内触及250日' in s for s in signals), signals)
        counts = monitor.signal_counts([detail])
        self.assertEqual(counts['ma']['yellow'], 1)
        self.assertEqual(counts['ma']['label'], '均线')
        self.assertNotIn('ma_cross', counts)
        self.assertNotIn('ma_band', counts)

    def test_long_ma_dual_facts_share_one_statistic_without_double_counting_stock(self):
        level, signals, detail = self.run_sym(self.flat(last=(99.9, 100.05)), shorts=0)
        self.assertEqual(level, 'yellow')
        self.assertIn('上穿250日均线', signals)
        self.assertTrue(any('日内触及250日' in s for s in signals))
        counts = monitor.signal_counts([detail])
        self.assertEqual(counts['ma']['yellow'], 1)
        self.assertEqual(counts['ma']['label'], '均线')
        self.assertEqual({kind for kind, _ in detail['signal_marks']}, {'ma_cross', 'ma_band'})

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


class TestMaMergedStatistics(unittest.TestCase):
    """调用真实规则、快照、渲染和前端函数，证明仅合并统计、不改事实。"""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(monitor.requests, 'get', side_effect=AssertionError('专项禁止联网')))
        self.stack.enter_context(patch.object(monitor.requests, 'post', side_effect=AssertionError('专项禁止联网')))
        self.stack.enter_context(patch.object(monitor, 'TARGET_DATE', ''))

    @staticmethod
    def rule_row(previous=99, price=100, low=99, high=101, symbol='BRK.B-US', group='position'):
        from types import SimpleNamespace
        x = SimpleNamespace(closes=[100] * 249 + [previous, price], price=price,
                            moving_averages=[(250, 100)], vol_sigma=None,
                            data={'highs': [100] * 250 + [high], 'lows': [100] * 250 + [low]})
        alerts = monitor._Alerts()
        with patch.dict(monitor.S, {'ma_short': 0, 'ma_long': 250}), patch.object(monitor, 'sma', return_value=100):
            monitor._rule_moving_averages(alerts, x)
        return alerts, {'symbol': symbol, 'group': group, 'hold': 'long', 'note': '均线专项',
                        'price': price, 'level': alerts.level, 'signals': alerts.signals[:],
                        'signal_marks': alerts.marks[:], 'ma_band_direction': getattr(x, 'ma_band_direction', ''),
                        'ma_settings': {'ma_short': 0, 'ma_long': 250}}

    @staticmethod
    def filters(page):
        import html
        return json.loads(html.unescape(re.search(r'data-filter="([^"]*)"', page).group(1)))

    def test_real_touch_and_cross_merge_one_and_leave_original_marks(self):
        import copy
        alerts, row = self.rule_row()
        before = copy.deepcopy(row)
        self.assertEqual(alerts.marks, [('ma_band', 'yellow'), ('ma_cross', 'yellow')])
        self.assertEqual(alerts.level, 'yellow')
        members = monitor.signal_count_members([row])
        counts = monitor.signal_counts([row])
        self.assertEqual(counts['ma'], {'label': '均线', 'red': 0, 'yellow': 1, 'purple': 0})
        for field in (members, counts):
            self.assertNotIn('ma_cross', field)
            self.assertNotIn('ma_band', field)
        member, = members['ma']['yellow']
        self.assertEqual({f['kind'] for f in member['ma_facts']}, {'ma_cross', 'ma_band'})
        self.assertEqual(len(member['indicators']), 2)
        snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
        self.assertEqual(snapshot['signal_members'], members)
        self.assertEqual(snapshot['signal_counts'], counts)
        page = monitor._signal_counts_card(snapshot)
        self.assertEqual(page.count('data-kind="ma"'), 1)
        self.assertEqual(page.count('href="#s-brk-b"'), 1)
        self.assertIn('长均线上穿', page)
        self.assertIn('触及长均线', page)
        self.assertEqual(member['ma_settings'], {'ma_short': 0, 'ma_long': 250})
        self.assertNotIn('明细待下次更新', page)
        self.assertEqual(row, before)
        self.assertEqual(alerts.marks, before['signal_marks'])

    def test_real_one_percent_touch_boundaries_and_cross_rules_unchanged(self):
        for previous, price, low, high, marks in (
            (100, 102, 101, 104, [('ma_band', 'yellow')]),
            (100, 102, 101.0001, 104, []),
            (100, 98, 96, 99, [('ma_band', 'yellow')]),
            (100, 98, 96, 98.9999, []),
            (99, 105, 104, 106, [('ma_cross', 'yellow')]),
            (101, 95, 94, 96, [('ma_cross', 'yellow')]),
            (100, 100, 100, 100, [('ma_band', 'yellow')]),
        ):
            with self.subTest(previous=previous, price=price, low=low):
                alerts, row = self.rule_row(previous, price, low, high)
                self.assertEqual(alerts.marks, marks)
                self.assertEqual(monitor.signal_counts([row])['ma']['yellow'], int(bool(marks)))
                self.assertEqual(row['signal_marks'], marks)
                self.assertEqual(alerts.level, 'yellow' if marks else 'green')

    def test_cross_group_aliases_deduplicate_at_highest_signal_level_keep_all_facts(self):
        import copy
        _, touch = self.rule_row(previous=100, group='position')
        _, cross = self.rule_row(previous=101, symbol='BRK-B', group='technology')
        cross.update(level='red', signal_marks=[('ma_cross', 'red')])
        rows = [cross, touch, copy.deepcopy(cross)]
        before = copy.deepcopy(rows)
        members = monitor.signal_count_members(rows)['ma']
        member, = members['red']
        self.assertEqual(members['yellow'], [])
        self.assertEqual({f['kind'] for f in member['ma_facts']}, {'ma_cross', 'ma_band'})
        self.assertEqual(len(member['indicators']), 2)
        snapshot = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(snapshot['signal_members']['ma'], members)
        self.assertEqual(snapshot['signal_counts']['ma']['red'], 1)
        page = monitor._signal_counts_card(snapshot)
        self.assertEqual(page.count('href="#s-brk-b"'), 1)
        self.assertIn('长均线跌破', page)
        self.assertIn('触及长均线', page)
        data = self.filters(page)
        self.assertEqual([m['id'] for m in data['ma']['members']], ['BRK-B'])
        self.assertEqual(data['ma']['members'][0]['l'], 'red')
        self.assertIn('长均线跌破', data['ma']['members'][0]['d'][0]['t'])
        self.assertNotIn(' / ', data['ma']['members'][0]['d'][0]['t'])
        self.assertEqual(rows, before)

    def test_multiple_periods_and_opposite_directions_remain_in_one_member(self):
        _, row = self.rule_row()
        row['signals'] += ['上穿50日均线', '跌破120日均线']
        row['signal_marks'] += [('ma_cross', 'yellow'), ('ma_cross', 'yellow')]
        row['ma_settings']['ma_short'] = 50
        snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
        member, = snapshot['signal_members']['ma']['yellow']
        self.assertEqual(len(member['ma_facts']), 4)
        self.assertEqual(snapshot['signal_counts']['ma']['yellow'], 1)
        page = monitor._signal_counts_card(snapshot)
        self.assertEqual(page.count('href="#s-brk-b"'), 1)
        self.assertIn('<span class="arrow-band-up">▲</span> 短均线上穿', page)
        self.assertIn('<span class="arrow-band-down">▼</span> 均线（归属待确认）跌破', page)
        detail = self.filters(page)['ma']['members'][0]['d'][0]['t']
        for fact in member['indicators']:
            # 与分组标题同一套措辞：穿越倒装，且同一指标内部不再出现白色「 / 」。
            self.assertIn(monitor._ma_stat_label(fact['label'], member['ma_settings']), detail)
        self.assertNotIn(' / ', detail)
        self.assertNotRegex(detail, r'\d+日(?:弱)?均线')

    def test_same_mark_at_two_levels_uses_highest_and_exclusions_hold(self):
        _, row = self.rule_row()
        row['signal_marks'] += [('ma_cross', 'red'), ('ma_band', 'purple')]
        counts = monitor.signal_counts([row])['ma']
        self.assertEqual((counts['red'], counts['yellow'], counts['purple']), (1, 0, 0))
        for changes in ({'price': None}, {'reference_only': True}, {'level': 'gray'}, {'symbol': '.SPX'}):
            with self.subTest(changes=changes):
                out = monitor.signal_counts([dict(row, **changes)])['ma']
                self.assertEqual((out['red'], out['yellow'], out['purple']), (0, 0, 0))

    def test_legacy_two_lists_merge_without_mutating_snapshot(self):
        import copy
        _, row = self.rule_row()
        base = {'symbol': row['symbol'], 'level': 'yellow', 'group': 'position'}
        legacy = {'signal_counts': {kind: {'label': kind, 'red': 0, 'yellow': 1, 'purple': 0}
                                   for kind in ('ma_cross', 'ma_band')},
                  'signal_members': {kind: {'yellow': [dict(base, indicators=monitor._member_direction_markers(row, kind))]}
                                     for kind in ('ma_cross', 'ma_band')}}
        before = copy.deepcopy(legacy)
        normalized = monitor._sanitize_snapshot(legacy)
        self.assertEqual(normalized['signal_counts']['ma']['yellow'], 1)
        self.assertEqual(len(normalized['signal_members']['ma']['yellow'][0]['indicators']), 2)
        page = monitor._signal_counts_card(legacy)
        self.assertEqual(page.count('href="#s-brk-b"'), 1)
        self.assertEqual([m['id'] for m in self.filters(page)['ma']['members']], ['BRK-B'])
        self.assertEqual(legacy, before)

    def test_legacy_without_lists_is_pending_and_rows_can_restore_facts(self):
        legacy = {'signal_counts': {'ma_cross': {'yellow': 1}, 'ma_band': {'yellow': 1}}}
        page = monitor._signal_counts_card(legacy)
        self.assertIn('明细待下次更新', page)
        self.assertFalse(self.filters(page)['ma']['ok'])
        for malformed in ('无效名单', {'yellow': None}, {'yellow': [{'symbol': ''}]}):
            with self.subTest(malformed=malformed):
                page = monitor._signal_counts_card(dict(legacy, signal_members={'ma_cross': malformed}))
                self.assertFalse(self.filters(page)['ma']['ok'])
        _, row = self.rule_row()
        restored = monitor._sanitize_snapshot(dict(legacy, items=[row]))
        self.assertEqual(restored['signal_counts']['ma']['yellow'], 1)
        self.assertEqual(restored['items'][0]['signal_marks'], row['signal_marks'])
        self.assertNotIn('明细待下次更新', monitor._signal_counts_card(restored))

    def test_all_visible_ma_exits_project_fact_settings_without_mutating_internal_numbers(self):
        import copy
        _, row = self.rule_row(price=100.5)
        row['signals'].append('上穿50日弱均线')
        row.update(chg=0, prev_close=100)
        row['ma_settings'] = {'ma_short': 50, 'ma_long': 250}
        before = copy.deepcopy(row)
        with patch.dict(monitor.S, {'ma_short': 250, 'ma_long': 50}):
            snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
            member, = snapshot['signal_members']['ma']['yellow']
            self.assertEqual(member['ma_settings'], row['ma_settings'])
            self.assertEqual(member['indicators'], monitor._member_direction_markers(row, 'ma'))
            self.assertIn('上穿50日均线', [m['label'] for m in member['indicators']])
            self.assertIn('日内触及250日均线，现价在均线上方',
                          [m['label'] for m in member['indicators']])
            snapshot_before = copy.deepcopy(snapshot)
            outputs = {'描述': _row_signals(row),
                       '主表': monitor._stock_row(row, snapshot),
                       '框标题': monitor._member_block(member, 'ma')[1],
                       '图标': monitor._member_icons(member, 'ma', 'yellow'),
                       '筛选详情': monitor._filter_detail(member, 'ma', '均线'),
                       '统计': monitor._signal_counts_card(snapshot)}
            for name, output in outputs.items():
                with self.subTest(exit=name):
                    self.assertIn('短均线', output)
                    self.assertIn('长均线', output)
                    self.assertNotRegex(output, r'\d+日(?:弱)?均线')
            self.assertIn('现价在长均线上方', outputs['筛选详情'])
            self.assertIn('现价在长均线上方', outputs['图标'])
            self.assertEqual(snapshot, snapshot_before)
        self.assertEqual(row, before)
        self.assertTrue(all(re.search(r'\d+日(?:弱)?均线', f['text']) for f in member['ma_facts']))
        self.assertEqual(row['signal_marks'], [('ma_band', 'yellow'), ('ma_cross', 'yellow')])

    def test_ma_touch_display_shortens_long_only_and_preserves_facts(self):
        config = {'ma_short': 60, 'ma_long': 250}
        before = dict(config)
        for side in ('现价在均线上方', '现价在均线下方', '现价与均线相等'):
            text = '日内触及250日均线，' + side
            output = monitor._ma_display_text(text, config)
            self.assertTrue(output.startswith('触及长均线，'), output)
            self.assertNotIn('日内触及长均线', output)
            self.assertIn(side.replace('均线', '长均线'), output)
        self.assertEqual(monitor._ma_display_text('日内触及60日均线', config),
                         '日内触及短均线')
        self.assertEqual(monitor._ma_display_text('现价贴近250日均线', config),
                         '现价贴近长均线')
        self.assertEqual(config, before)

    def test_ma_display_same_disabled_unknown_period_and_weak_line_names(self):
        cases = (
            ({'ma_short': 60, 'ma_long': 120}, '上穿60日弱均线 / 日内触及120日均线',
             '上穿短均线 / 触及长均线'),
            ({'ma_short': 60, 'ma_long': 60}, '上穿60日均线', '上穿短均线/长均线'),
            ({'ma_short': 0, 'ma_long': 120}, '上穿60日均线 / 跌破120日均线',
             '上穿均线（归属待确认） / 跌破长均线'),
            ({'ma_short': 60, 'ma_long': 0}, '上穿60日均线 / 跌破120日均线',
             '上穿短均线 / 跌破均线（归属待确认）'),
            ({'ma_short': 0, 'ma_long': 0}, '上穿60日均线', '上穿均线（归属待确认）'),
            ({}, '上穿60日均线', '上穿均线（归属待确认）'),
            ({'ma_short': 50, 'ma_long': 200}, '上穿120日均线', '上穿均线（归属待确认）'),
        )
        for config, text, expected in cases:
            with self.subTest(config=config, text=text):
                self.assertEqual(monitor._ma_display_text(text, config), expected)
        with patch.dict(monitor.S, {'ma_short': 60, 'ma_long': 120}):
            self.assertEqual(monitor._ma_display_text('上穿60日均线'), '上穿短均线')
            self.assertEqual(monitor._ma_display_text('跌破120日均线'), '跌破长均线')
        _, _, same = TestMaConfirm.run_sym(TestMaConfirm.flat(last=(99.9, 100.05)), shorts=50, longs=50)
        same['ma_settings'] = {'ma_short': 50, 'ma_long': 50}
        snapshot = monitor.build_snapshot({}, [same], {}, group_counts={})
        self.assertEqual(same['signal_marks'], [('ma_cross', 'yellow')])
        self.assertEqual(snapshot['signal_counts']['ma']['yellow'], 1)
        self.assertIn('短均线与长均线', _row_signals(same))
        self.assertNotIn('signal-separator', _row_signals(same))

    def test_legacy_effective_ma_settings_override_current_settings_and_generic_touch(self):
        import copy
        base = {'symbol': 'OLDMA', 'level': 'yellow', 'group': 'technology'}
        legacy = {'effective_settings': {'ma_short': 60, 'ma_long': 120},
                  'signal_counts': {'ma_cross': {'yellow': 1}, 'ma_band': {'yellow': 1}},
                  'signal_members': {
                      'ma_cross': {'yellow': [dict(base, indicators=[
                          {'shape': '▲', 'tone': 'cyan', 'label': '上穿60日均线'}])]},
                      'ma_band': {'yellow': [dict(base, indicators=[
                          {'shape': '▲', 'tone': 'cyan', 'label': '现价在均线上方，日内触及±1%'}])]}}}
        before = copy.deepcopy(legacy)
        with patch.dict(monitor.S, {'ma_short': 120, 'ma_long': 60}):
            normalized = monitor._sanitize_snapshot(legacy)
            member, = normalized['signal_members']['ma']['yellow']
            self.assertEqual(member['ma_settings'], {'ma_short': 60, 'ma_long': 120})
            self.assertIn('上穿60日均线', [m['label'] for m in member['indicators']])
            self.assertIn('日内触及120日均线，现价在均线上方±1%',
                          [m['label'] for m in member['indicators']])
            output = monitor._signal_counts_card(legacy)
            self.assertIn('短均线上穿', output)
            self.assertIn('触及长均线', output)
            detail = self.filters(output)['ma']['members'][0]['d'][0]['t']
            # 多选组合清单与统计分组标题同一套措辞（穿越倒装），同指标内部用「，」。
            self.assertIn('短均线上穿', detail)
            self.assertIn('触及长均线，现价在长均线上方', detail)
            self.assertNotIn(' / ', detail)
            self.assertNotRegex(output, r'\d+日(?:弱)?均线')
            self.assertEqual(normalized['signal_counts']['ma']['yellow'], 1)
        self.assertEqual(legacy, before)
        no_config = copy.deepcopy(legacy)
        no_config.pop('effective_settings')
        output = monitor._signal_counts_card(no_config)
        self.assertIn('归属待确认', output)
        self.assertNotIn('短均线', output)
        self.assertNotIn('长均线', self.filters(output)['ma']['members'][0]['d'][0]['t'])
        override = copy.deepcopy(legacy)
        for levels in override['signal_members'].values():
            levels['yellow'][0]['ma_settings'] = {'ma_short': 120, 'ma_long': 60}
        member, = monitor._sanitize_snapshot(override)['signal_members']['ma']['yellow']
        self.assertEqual(member['ma_settings'], {'ma_short': 120, 'ma_long': 60})
        self.assertIn('长均线上穿', monitor._member_block(member, 'ma')[1])
        # 分组标题与筛选详情不再一个倒装一个不倒装：去掉方向前缀后逐字相同。
        title = monitor._member_block(member, 'ma')[1]
        self.assertEqual(re.sub(r'^[▲▼■]\s*', '', title),
                         monitor._filter_detail(member, 'ma', '均线'))

    def test_legacy_rows_without_ma_settings_use_snapshot_not_current_config(self):
        import copy
        _, row = self.rule_row()
        row.pop('ma_settings')
        legacy = {'effective_settings': {'ma_short': 50, 'ma_long': 250},
                  'items': [row], 'signal_counts': monitor.signal_counts([row])}
        before = copy.deepcopy(legacy)
        with patch.dict(monitor.S, {'ma_short': 250, 'ma_long': 50}):
            normalized = monitor._sanitize_snapshot(legacy)
            member, = normalized['signal_members']['ma']['yellow']
            self.assertEqual(member['ma_settings'], {'ma_short': 50, 'ma_long': 250})
            # sanitize 已由完整事实恢复成员；原始缺名单快照仍应保持待更新，不临时猜名单。
            self.assertIn('触及长均线', monitor._signal_counts_card(normalized))
            self.assertIn('明细待下次更新', monitor._signal_counts_card(legacy))
        self.assertEqual(legacy, before)

    def test_band_direction_markers_keep_period_and_price_side_for_same_fact(self):
        for direction, shape, position in (('up', '▲', '现价在均线上方'),
                                           ('down', '▼', '现价在均线下方'),
                                           ('level', '■', '现价与均线相等')):
            for prefix in ('日内触及', '现价贴近'):
                with self.subTest(direction=direction, prefix=prefix):
                    row = {'signals': [prefix + '120日均线 ±1%区间'], 'ma_band_direction': direction,
                           'signal_marks': [('ma_band', 'yellow')],
                           'ma_settings': {'ma_short': 60, 'ma_long': 120}}
                    expected = [{'shape': shape, 'tone': 'cyan', 'label': prefix + '120日均线，' + position}]
                    self.assertEqual(monitor._member_direction_markers(row, 'ma_band'), expected)
                    self.assertEqual(monitor._member_direction_markers(row, 'ma'), expected)
                    member = {'indicators': expected, 'ma_settings': row['ma_settings']}
                    for kind in ('ma', 'ma_band'):
                        self.assertIn(('触及' if prefix == '日内触及' else prefix) + '长均线',
                                  monitor._member_block(member, kind)[1])
                        self.assertIn('长均线', monitor._filter_detail(member, kind, '均线'))
                        self.assertIn('长均线', monitor._member_icons(member, kind, 'yellow'))

    def test_real_frontend_functions_accept_new_ma_and_old_aliases(self):
        import shutil
        import subprocess
        node = shutil.which('node')
        self.assertIsNotNone(node, '专项必须实际运行 Node，不跳过前端证明')
        with open(os.path.join(ROOT, 'cost_panel.js'), encoding='utf-8') as stream:
            source = stream.read()
        functions = source[source.index('  function signalIdentity('):source.index('  var maFilterBox=')]
        interaction = monitor.PAGE_INTERACTION_JS
        intersect = interaction[interaction.index('function intersectSignals('):interaction.index('function renderSignalFilter(')]
        _, row = self.rule_row()
        row['signal_marks'].append(('chg', 'yellow'))
        row['chg'] = 1
        snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
        data = self.filters(monitor._signal_counts_card(snapshot))
        code = ('var window=globalThis;var LV_ORDER=["red","yellow","green","gray"];'
                'function own(o,k){return Object.prototype.hasOwnProperty.call(o,k);}' + intersect + functions
                + 'var d=' + json.dumps(data, ensure_ascii=False) + ';'
                'var legacy={ma_cross:{ok:true,members:[{id:"BRK.B-US",s:"BRK.B-US",l:"yellow",d:["上穿50日均线"]}]},'
                'ma_band:{ok:true,members:[{id:"BRK-B",s:"BRK-B",l:"red",d:["日内触及250日均线"]}]},chg:d.chg};'
                'var current=intersectSignals(d,["ma","chg"]);'
                'var old=intersectSignals(legacy,["ma_cross","ma_band","chg"]);'
                'legacy.ma_band.ok=false;var pending=intersectSignals(legacy,["ma","chg"]);'
                'var ids=["BRK.B-US","BRK-B","00700-HK","0700.HK","31#CLMAIN","CL=F"]'
                '.map(signalIdentity);process.stdout.write(JSON.stringify({current,old,pending,ids}));')
        result = json.loads(subprocess.run([node, '-e', code], capture_output=True, text=True,
                                          encoding='utf-8', check=True).stdout)
        self.assertEqual([m['id'] for m in result['current']['members']], ['BRK-B'])
        self.assertIn('长均线上穿', result['current']['members'][0]['details'][0]['t'])
        self.assertIn('触及长均线', result['current']['members'][0]['details'][0]['t'])
        self.assertNotIn(' / ', result['current']['members'][0]['details'][0]['t'])
        member, = result['old']['members']
        self.assertEqual(member['id'], 'BRK-B')
        self.assertEqual(member['l'], 'red')
        # 旧字符串没有逐事实灯级，兼容读取使用合并后的成员级别；新对象的独立灯另有专项。
        self.assertEqual([d for d in member['details'] if d['k'] == 'ma'], [
            {'l': 'red', 't': '上穿50日均线', 'k': 'ma'},
            {'l': 'red', 't': '日内触及250日均线', 'k': 'ma'}])
        self.assertEqual(result['pending'], {'pending': ['均线']})
        self.assertEqual(result['ids'], [monitor.instrument_identity(s) for s in
                         ('BRK.B-US', 'BRK-B', '00700-HK', '0700.HK', '31#CLMAIN', 'CL=F')])


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
        self.assertIn('range52-lo" aria-hidden="true">100.00<span class="range52-gap">-33.3%</span>', row)
        self.assertIn('range52-hi" aria-hidden="true">200.00<span class="range52-gap">+33.3%</span>', row)
        self.assertNotIn('range52-percent', row)   # 三角上方不再挂百分比
        self.assertIn('style="left:50.0%"', row)
        self.assertIn('.range52-track{position:relative;flex:1 1 auto;height:2.1px;', monitor.PAGE_CSS)
        # 2026-10-20：现价标记从「压在轨道中间的紫方块」改成「尖角朝下、顶在灰轨上沿的倒三角」
        self.assertIn('.range52-marker{position:absolute;bottom:100%;width:0;height:0;', monitor.PAGE_CSS)
        # 2026-10-27：三角只收底宽（10.4 → 8.4px），高度仍 9px 不变 → 顶角 60° 变约 50°（更尖锐）
        self.assertIn('border-top:9px solid #efd9ff;', monitor.PAGE_CSS)
        self.assertIn('border-top:7px solid #713caf', monitor.PAGE_CSS)   # 内层高度不跟着缩
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

    def test_endpoint_distances_are_labeled_under_both_prices_for_every_group(self):
        """两端价格下方各写一行「到低点 / 到高点」，以本轮价为基数；全组生效，三角上方不再有百分比。"""
        result = monitor._range52_from_history(self.history())
        base = {'range52': result, 'price': 150.0, 'data_date': result['to']}
        for group in ('position', 'focus', 'technology', None):
            row = monitor._range52_row({**base, 'group': group})
            self.assertIn('range52-lo" aria-hidden="true">100.00<span class="range52-gap">-33.3%</span>', row)
            self.assertIn('range52-hi" aria-hidden="true">200.00<span class="range52-gap">+33.3%</span>', row)
            self.assertIn('从本轮价到低点 -33.3%、到高点 +33.3%', row)
            self.assertNotIn('range52-percent', row)   # 三角上方不再挂百分比
            self.assertNotIn('has-endpoint-gaps', row)  # 不再有持仓专属第二行，全组同一套结构
        # 区间线性位置只保留在三角位置和读屏说明里，不再单列一个百分比
        visible = re.sub(r' aria-label="[^"]*"', '', monitor._range52_row(base))
        self.assertNotIn('>50.0%<', visible)
        self.assertIn('位于区间 50.0%', monitor._range52_row(base))
        # 现价正好等于端点时，该侧写 0.0%，另一侧仍是真实的到另一点幅度
        at_low = monitor._range52_row({**base, 'price': 100.0})
        self.assertIn('range52-lo" aria-hidden="true">100.00<span class="range52-gap">0.0%</span>', at_low)
        self.assertIn('range52-hi" aria-hidden="true">200.00<span class="range52-gap">+100.0%</span>', at_low)
        at_high = monitor._range52_row({**base, 'price': 200.0})
        self.assertIn('range52-lo" aria-hidden="true">100.00<span class="range52-gap">-50.0%</span>', at_high)
        self.assertIn('range52-hi" aria-hidden="true">200.00<span class="range52-gap">0.0%</span>', at_high)
        for boundary in (at_low, at_high):
            self.assertNotIn('-0.0%', boundary)
        self.assertIn('数据待更新', monitor._range52_row({**base, 'price': 0}))
        self.assertIn('数据待更新', monitor._range52_row({**base, 'price': float('nan')}))
        self.assertEqual(monitor._range52_row({**base, 'reference_only': True}), '')
        # 第二行占下面 13px，不与三角／灰轨重叠；三角左右再收窄一档
        self.assertIn('height:26px;padding-bottom:13px', monitor.PAGE_CSS)
        self.assertIn('border-left:4.2px solid transparent;border-right:4.2px solid transparent', monitor.PAGE_CSS)
        self.assertNotIn('.range52-percent{', monitor.PAGE_CSS)
        self.assertNotIn('clamp(40px,var(--range-pos)', monitor.PAGE_CSS)

    def test_cost_price_draws_a_white_triangle_without_touching_the_range_bar(self):
        """成本价只在位置条上多一个白三角：紫方块是本轮价（条上），白三角是成本价（条下）。
        成本价不参与灯级，写错或没写都不能画出来。"""
        result = monitor._range52_from_history(self.history())   # 低 100 / 高 200 / 本轮价 150
        base = {'range52': result, 'price': 150.0, 'data_date': result['to']}
        row = monitor._range52_row({**base, 'cost': 125.0})
        self.assertIn('<span class="range52-cost" style="left:25.0%"></span>', row)
        self.assertIn('成本价 125.00 位于区间 25.0%', row)
        self.assertNotIn('低于区间', row)
        self.assertIn('style="left:50.0%"', row)          # 本轮价的三角位置不变
        self.assertIn('class="range52-gap">-33.3%</span>', row)   # 端点下方仍是本轮价到两端的幅度
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
        self.assertIn('class="range52-gap">-33.3%</span>', short_row)
        self.assertIn('class="range52-gap">+33.3%</span>', short_row)
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
        self.assertIn('.stk tr.range52-row td{padding:0 12px 4px 18px;', monitor.PAGE_CSS)  # 位置条下方只留 4px
        self.assertIn('.stk tr.red+tr.range52-row>td{--lc:var(--red);--lw:6px}', monitor.PAGE_CSS)  # 灯条延伸到本股位置条
        self.assertIn('background-size:var(--lw) calc(85% - 2.1px);background-position:left bottom', monitor.PAGE_CSS)  # 灯条顶部下移
        self.assertIn('</tr><tr class="range52-row"><td colspan="4">', output)
        self.assertEqual(monitor.signal_counts([row])['low52']['red'], 0)


class TestDisplayInteractions(unittest.TestCase):
    """展示改动不改变业务信号；颜色、局部收起与计数位置均覆盖。"""

    def test_signal_color_priority_and_html_escape(self):
        cases = [
            ('RSI 2条 ↓超卖（≤30）：RSI6=20', 'signal-magenta'),
            ('RSI 3条 ↓超卖（≤30）：RSI6=20', 'signal-magenta'),
            ('RSI 2条 ↑超买（≥70）：RSI6=80', ''),
            ('贴近布林上轨（距 0.2%）', ''),
            ('逼近布林上轨▲ 还差0.29%', ''),
            ('触及布林上轨▲ 103.00', ''),
            ('盘中突破布林上轨▲ 101.00（盘中触及 103.00）', ''),
            ('突破布林上轨▲ 101.00', ''),
            ('逼近布林下轨▼ 还差0.30%', 'signal-yellow'),
            ('逼近布林下轨▼ 还差0.08%（盘中触及 50.04）', 'signal-yellow'),
            ('触及布林下轨▼ 99.00', 'signal-yellow'),
            ('跌破布林下轨▼', 'signal-yellow'),
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
                        # 无灯色文案不套灯色 span；方向箭头（▲▼↑↓）的配色 span 是唯一例外
                        stripped = re.sub(r'<span class="arrow-[a-z-]+">([▲▼↑↓])</span>', r'\1', rendered)
                        self.assertNotIn('<span', stripped, rendered)
                    self.assertEqual(monitor.html_lib.unescape(plain_text(rendered)), text)
                    self.assertEqual(re.findall(r'<span class="(arrow-[a-z-]+)">([▲▼↑↓])</span>', rendered),
                                     [(cls, char) for char in text
                                      for symbol, cls in (('▲', 'arrow-band-up'), ('▼', 'arrow-band-down'),
                                                          ('↑', 'arrow-rsi-up'), ('↓', 'arrow-rsi-down'))
                                      if char == symbol])
        with patch.dict(monitor.S, {'ma_long': 0}):
            self.assertNotIn('signal-cyan', monitor._signal_text_html('上穿250日均线'))

    def test_lower_boll_approach_renders_yellow_text_without_changing_level(self):
        row = {'signals': ['逼近布林下轨▼ 还差0.08%（盘中触及 50.04）'],
               'target': {}, 'level': 'yellow', 'signal_marks': [('boll', 'yellow')]}
        output = _row_signals(row)
        self.assertEqual(re.findall(r'data-signal-level="([^"]+)"', output), ['yellow'])
        self.assertIn('data-signal-kind="boll" data-signal-level="yellow"', output)
        self.assertNotIn('signal-dot', output)
        self.assertIn('逼近布林下轨<span class="arrow-band-down">▼</span>', output)
        self.assertEqual(plain_text(output), '逼近布林下轨▼ 还差0.08%')
        self.assertEqual(row['signals'], ['逼近布林下轨▼ 还差0.08%（盘中触及 50.04）'])
        self.assertEqual(row['level'], 'yellow')
        self.assertEqual(row['signal_marks'], [('boll', 'yellow')])

    def test_signal_text_order_and_original_data_are_unchanged(self):
        import copy
        row = {'signals': ['距目标价 100 还差 1%', '跌破布林下轨▼', 'RSI 2条 ↓超卖'],
               'target': {}, 'level': 'yellow', 'signal_marks': [('boll', 'yellow')]}
        old = copy.deepcopy(row)
        output = _row_signals(row)
        self.assertIn('跌破布林', output)
        self.assertNotIn('距目标价', output)
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

    def test_collapse_button_css_has_full_width_short_tinted_strip(self):
        button_rule = re.search(r'\.collapse-group\{([^}]+)\}', monitor.PAGE_CSS)
        self.assertIsNotNone(button_rule)
        for declaration in ('width:100%', 'height:26px', 'padding:0', 'background:var(--collapse-bg)', 'border:0',
                            'display:flex', 'align-items:center', 'justify-content:center', 'box-sizing:border-box'):
            self.assertIn(declaration, button_rule.group(1))
        self.assertNotIn('width:44px', button_rule.group(1))
        self.assertIn('.collapse-group .collapse-icon{font-size:16px;line-height:1}', monitor.PAGE_CSS)
        strip_rule = re.search(r'\.group-bottom\{([^}]+)\}', monitor.PAGE_CSS)
        self.assertRegex(strip_rule.group(1), r'(?:^|;)padding:0(?:;|$)')
        self.assertIn('margin:0 -4px -6px', strip_rule.group(1))
        self.assertIn('line-height:0', strip_rule.group(1))
        self.assertIn('.collapse-group:hover{color:var(--text);background:var(--collapse-bg)}', monitor.PAGE_CSS)
        counts_rule = re.search(r'#signalCounts\{([^}]+)\}', monitor.PAGE_CSS)[1]
        self.assertIn('--collapse-bg:rgba(102,134,198,.6)', counts_rule)
        self.assertNotIn('opacity:', counts_rule)
        self.assertIn('#signalCounts .collapse-group{color:#fff}', monitor.PAGE_CSS)
        self.assertNotIn('opacity:', button_rule.group(1))
        self.assertIn('--collapse-bg:var(--signal-bg)', monitor.PAGE_CSS)
        self.assertIn('#group_positions{background:linear-gradient(rgba(46,160,67,.02),rgba(46,160,67,.02)),var(--card);--collapse-bg:rgba(46,160,67,.15)}', monitor.PAGE_CSS)

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
        self.assertIn('#signalCounts{--collapse-bg:rgba(102,134,198,.6);border:var(--frame-w) solid var(--signal-border);box-shadow:none;background:none}', page)
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


class TestQuietKeep(unittest.TestCase):
    @staticmethod
    def item(symbol='TEST', **changes):
        row = dict(symbol=symbol, note='', level='green', group='position', chg=0.0,
                   price=100.0, prev_close=100.0, signals=[], signal_marks=[], foldable=True)
        row.update(changes)
        return row

    def body(self, rows, group='position', asof='2030-10-20', snapshot=None):
        config = next(g for g in monitor.GROUPS if g['item_group'] == group)
        return monitor._group_body(rows, config, snapshot or {}, asof, True,
                                   'group_' + config['key'], config['label'])

    def test_keep_flags_and_actual_group_placement_share_all_four_reasons(self):
        for group in (g['item_group'] for g in monitor.GROUPS):
            for level in ('green', 'red', 'yellow', 'gray'):
                rows = [self.item('NORMAL', group=group, level=level),
                        self.item('REFERENCE', group=group, level=level, reference_only=True),
                        self.item('TECHKEEP', group=group, level=level, foldable=False),
                        self.item('TARGETKEEP', group=group, level=level,
                                  target=monitor.evaluate_target(100, 100, {'kind': 'manual', 'price': 150}, group, 3)),
                        self.item('EARNKEEP', group=group, level=level,
                                  earnings={'status': 'ok', 'kind': 'official', 'date': '2030-10-21'})]
                with self.subTest(group=group, level=level):
                    output = self.body(rows, group)
                    shown, _, quiet = output.partition('<details class="quiet-list">')
                    for row in rows:
                        keep = row['symbol'] != 'NORMAL'
                        marker = f'id="{monitor._stock_anchor_id(row["symbol"])}" data-quiet-keep="{str(keep).lower()}"'
                        self.assertIn(marker, output)
                        self.assertEqual(marker in shown, keep or level != 'green')
                        self.assertEqual(marker in quiet, not keep and level == 'green')

    def test_target_reached_uses_container_group_not_stale_row_group(self):
        for container, stale, keep in (('position', 'technology', True),
                                        ('focus', 'technology', True),
                                        ('technology', 'position', False)):
            row = self.item(group=stale, target={'kind': 'manual', 'price': 99})
            before = json.dumps(row, sort_keys=True)
            with self.subTest(container=container, stale=stale):
                output = self.body([row], container)
                marker = f'id="s-test" data-quiet-keep="{str(keep).lower()}"'
                self.assertIn(marker, output)
                self.assertEqual('<details class="quiet-list">' in output, not keep)
                self.assertEqual(json.dumps(row, sort_keys=True), before)

    def test_explicit_earnings_window_is_used_in_both_classification_and_row(self):
        row = self.item(earnings={'status': 'ok', 'kind': 'official', 'date': '2030-10-21'})
        snapshot = {'earnings_week': {'asof': '2030-12-01'}}
        with patch.object(monitor, '_bj_today', return_value='2031-01-01'):
            output = self.body([row], asof='2030-10-20', snapshot=snapshot)
            self.assertIn('data-quiet-keep="true"', output)
            self.assertNotIn('<details class="quiet-list">', output)
            output = self.body([row], asof='2030-12-01', snapshot=snapshot)
            self.assertIn('data-quiet-keep="false"', output)
            self.assertIn('<details class="quiet-list">', output)
            direct = monitor._stock_row(row, {'earnings_week': {'asof': '2030-10-20'}})
            self.assertIn('data-quiet-keep="true"', direct)

    def test_missing_foldable_and_original_red_yellow_do_not_imply_keep(self):
        for level in ('green', 'red', 'yellow'):
            row = self.item(level=level)
            row.pop('foldable')
            with self.subTest(level=level):
                self.assertIn('data-quiet-keep="false"', monitor._stock_row(row, {}))
                output = self.body([row])
                self.assertEqual('<details class="quiet-list">' in output, level == 'green')

    def test_target_below_medium_and_etf_target_do_not_force_keep(self):
        for row in (self.item(target=monitor.evaluate_target(100, 100,
                             {'kind': 'manual', 'price': 110}, 'position', 3)),
                    self.item(etf=True, target=monitor.evaluate_target(100, 100,
                             {'kind': 'manual', 'price': 150}, 'position', 3))):
            output = self.body([row])
            self.assertIn('data-quiet-keep="false"', output)
            self.assertIn('<details class="quiet-list">', output)

    def test_long_and_short_zones_share_the_same_keep_and_earnings_window(self):
        rows = [self.item('LONGRED', level='red', hold='long'),
                self.item('SHORTRED', level='red', hold='short'),
                self.item('LONGEARN', hold='long', earnings={'status': 'ok', 'date': '2030-10-21'}),
                self.item('SHORTEARN', hold='short', earnings={'status': 'ok', 'date': '2030-10-21'})]
        group = next(g for g in monitor.GROUPS if g['item_group'] == 'position')
        output = monitor._group_card(group, rows, {}, {'earnings_week': {'asof': '2030-10-20'}})
        for symbol in ('longred', 'shortred'):
            self.assertIn(f'id="s-{symbol}" data-quiet-keep="false"', output)
        for symbol in ('longearn', 'shortearn'):
            self.assertIn(f'id="s-{symbol}" data-quiet-keep="true"', output)


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
        self.assertIn('<details class="quiet-list"><summary>无异动 1 只</summary>', out)
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


class TestSnapshotDataBoundaries(unittest.TestCase):
    def row(self, symbol='SAFE', **changes):
        row = dict(symbol=symbol, group='position', price=100.0, level='yellow',
                   signals=['RSI 2条↓超卖'], signal_marks=[('chg', 'yellow'), ('ma_cross', 'yellow')],
                   foldable=False, data_date='2026-10-20')
        row.update(changes)
        return row

    def test_invalid_explicit_prices_are_missing_in_every_statistics_projection(self):
        invalid = (None, 0, -1, True, False, float('nan'), float('inf'), float('-inf'),
                   '100', 'nan', '', [], {})
        for price in invalid:
            for level in ('red', 'yellow', 'green'):
                row = self.row('BAD', price=price, level=level)
                with self.subTest(price=price, level=level), patch.object(monitor, 'TARGET_DATE', '2026-10-20'):
                    self.assertEqual(monitor.text_only_members([row]), [])
                    self.assertTrue(all(not entries for levels in monitor.signal_count_members([row]).values()
                                        for entries in levels.values()))
                    snap = monitor.build_snapshot({}, [row], {}, group_counts={})
                    self.assertEqual([snap['summary'][key] for key in ('red', 'yellow', 'green', 'gray')], [0, 0, 0, 1])
                    self.assertEqual([snap['summary'][key] for key in ('today_prices', 'prior_prices', 'missing_prices')], [0, 0, 1])
                    self.assertEqual(snap['summary']['missing_symbols'], ['BAD'])
                    self.assertEqual(snap['text_only'], [])
                    self.assertEqual(snap['signal_members'], monitor.signal_count_members([row]))
                    self.assertIs(row['price'], price)
                    self.assertEqual(row['level'], level)

    def test_missing_price_is_compatible_only_in_lightweight_members_not_full_snapshot(self):
        row = self.row()
        row.pop('price')
        self.assertEqual([m['symbol'] for m in monitor.signal_count_members([row])['chg']['yellow']], ['SAFE'])
        self.assertEqual([m['symbol'] for m in monitor.signal_count_members([row])['ma']['yellow']], ['SAFE'])
        snap = monitor.build_snapshot({}, [row], {}, group_counts={})
        self.assertEqual(snap['summary']['yellow'], 0)
        self.assertEqual(snap['summary']['gray'], 1)
        self.assertEqual(snap['summary']['missing_symbols'], ['SAFE'])
        self.assertEqual(snap['signal_members']['chg']['yellow'], [])
        self.assertEqual(snap['signal_members']['ma']['yellow'], [])
        self.assertNotIn('price', row)

    def test_corrupt_technical_facts_keep_independent_change_amplitude_volume_members(self):
        row = self.row(signals=['RSI 2条↑超买', 'RSI 2条↓超卖', '异动 +3%', '振幅 4%', '量比 2.0×'],
                       signal_marks=[('chg', 'yellow'), ('amp', 'yellow'), ('vol_ratio', 'yellow')])
        for candidate in (row, monitor._sanitize_snapshot_fields(row)):
            members = monitor.signal_count_members([candidate])
            for kind in ('chg', 'amp', 'vol_ratio'):
                self.assertEqual([m['symbol'] for m in members[kind]['yellow']], ['SAFE'])
            self.assertEqual(monitor.text_only_members([candidate]), [])
        snap = monitor.build_snapshot({}, [row], {}, group_counts={})
        for kind in ('chg', 'amp', 'vol_ratio'):
            self.assertEqual([m['symbol'] for m in snap['signal_members'][kind]['yellow']], ['SAFE'])
        self.assertEqual(snap['summary']['yellow'], 1)

    def test_retired_target_light_cannot_restore_invalid_price_or_downgrade_unknown_facts(self):
        missing = self.row(price=0, level='red', signal_marks=[('chg', 'red'), ('target', 'yellow')])
        clean = monitor._sanitize_snapshot_fields(missing)
        self.assertEqual((clean['price'], clean['level']), (None, 'gray'))
        bad = self.row(level='yellow', signals=['RSI 2条↑超买', 'RSI 2条↓超卖'],
                       signal_marks=[('target', 'yellow')])
        clean = monitor._sanitize_snapshot_fields(bad)
        self.assertEqual(clean['level'], 'yellow')
        self.assertTrue(clean['technical_pending'])
        self.assertEqual(monitor.text_only_members([clean]), [])

    def test_finite_positive_prices_keep_valid_members_and_counts(self):
        for price in (1, 100.5, 1e-9):
            row = self.row(price=price)
            with self.subTest(price=price):
                members = monitor.signal_count_members([row])
                self.assertEqual([item['symbol'] for item in members['chg']['yellow']], ['SAFE'])
                self.assertEqual([item['symbol'] for item in members['ma']['yellow']], ['SAFE'])
                self.assertEqual([item['symbol'] for item in monitor.text_only_members([row])], ['SAFE'])

    def test_old_snapshots_rebuild_counts_when_price_is_invalid(self):
        import copy
        row = self.row('BAD', price=0, level='red')
        good = self.row()
        original = {'items': [row, good], 'summary': {'red': 1, 'yellow': 1, 'green': 0, 'gray': 0},
                    'signal_counts': {'chg': {'label': '涨跌幅', 'red': 1, 'yellow': 1, 'purple': 0}},
                    'signal_members': {'chg': {'red': [row], 'yellow': [good], 'purple': []}}}
        before = copy.deepcopy(original)
        clean = monitor._sanitize_snapshot(original)
        self.assertEqual(clean['summary']['red'], 0)
        self.assertEqual(clean['summary']['gray'], 1)
        self.assertEqual(clean['signal_counts']['chg']['red'], 0)
        self.assertEqual([item['symbol'] for item in clean['signal_members']['chg']['yellow']], ['SAFE'])
        self.assertEqual([item['symbol'] for item in clean['text_only']], ['SAFE'])
        self.assertEqual(original, before)

    def test_malformed_legacy_technical_facts_are_isolated_without_changing_main_level(self):
        import copy
        mutations = [
            {'signals': ['RSI 2条↑超买', 'RSI 2条↓超卖']},
            {'signals': ['RSI 2条↑超买 / 2条↓超卖']},
            {'signals': ['RSI 4条↓超卖']},
            {'signals': ['RSI 未知', '触及布林下轨 90']},
            {'signals': 123}, {'signals': 'RSI 2条↓超卖'},
            {'signals': ['RSI 2条↓超卖', None]},
            {'signals': ['触及布林轨道未知', 'RSI 1条↓超卖']},
            {'signal_marks': [None, ('rsi',), ('chg', 'yellow', 'extra')]},
            {'signal_marks': 'bad'},
            {'group': None}, {'group': ''}, {'group': []}, {'group': {}},
            {'group': ['position']}, {'group': {'name': 'position'}}, {'group': ' '},
        ]
        for mutation in mutations:
            bad = self.row('BAD', **mutation)
            if mutation == {'group': None}:
                bad.pop('group')
            safe = self.row()
            before = copy.deepcopy(bad)
            with self.subTest(mutation=mutation):
                self.assertEqual([item['symbol'] for item in monitor.text_only_members([bad, safe])], ['SAFE'])
                snap = monitor.build_snapshot({}, [bad, safe], {}, group_counts={})
                self.assertEqual([item['symbol'] for item in snap['text_only']], ['SAFE'])
                clean = monitor._sanitize_snapshot_fields(bad)
                self.assertEqual(clean['level'], before['level'])
                self.assertEqual(clean['foldable'], before['foldable'])
                self.assertTrue(clean['technical_pending'])
                self.assertEqual(bad, before)

    def test_retired_amplitude_gray_does_not_reclassify_corrupt_technical_facts(self):
        for group in ('position', None, []):
            row = self.row(group=group, signals=['振幅 2.0%', 'RSI 2条↑超买', 'RSI 2条↓超卖'],
                           signal_marks=[('amp', 'gray')], foldable=False)
            clean = monitor._sanitize_snapshot_fields(row)
            self.assertFalse(clean['foldable'])
            self.assertEqual(clean['level'], 'yellow')
            self.assertEqual(monitor.text_only_members([clean]), [])


class TestSignalCounts(unittest.TestCase):
    """status.json 的 signal_counts：各类信号各触发多少、哪些黄灯只由一类信号造成。"""

    @staticmethod
    def row(level, *marks):
        return {'symbol': 'T', 'price': 100.0, 'level': level, 'signal_marks': list(marks)}

    def test_counts_by_kind_and_level(self):
        rows = [self.member_row('CHANGE', 'position', 'red', ('chg', 'red'), ('rsi', 'yellow')),
                self.member_row('BOLL1', 'focus', 'yellow', ('boll', 'yellow')),
                self.member_row('BOLL2', 'technology', 'yellow', ('boll', 'yellow'), ('vol_ratio', 'yellow')),
                self.member_row('TARGET', 'focus', 'green', ('target', 'purple'))]
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
        self.assertEqual(out['ma']['yellow'], 1)

    def test_every_kind_is_listed_even_when_zero(self):
        out = monitor.signal_counts([])
        self.assertEqual(list(out), [k for k, _ in monitor.SIGNAL_KINDS])
        self.assertTrue(all(v['red'] == v['yellow'] == v['purple'] == 0 for v in out.values()))

    def test_legacy_fund_fields_marks_and_duplicates_are_ignored(self):
        import copy
        rows = [self.member_row('AAA', 'position', 'red', ('chg', 'red')),
                self.member_row('BBB', 'focus', 'green', ('target', 'purple')),
                self.member_row('CCC', 'technology', 'green')]
        for row, chg in zip(rows, (-3, 2, 1)):
            row.update(price=100, chg=chg, signals=[])
        legacy = copy.deepcopy(rows)
        for row in legacy:
            row['fund'] = {'level': 'red', 'hits': ['旧基本面风险'], 'period': '2026-06-30'}
            row['fund_cache_note'] = '旧基本面待更新'
            row['signal_marks'].append(('fund', 'red'))
        before = copy.deepcopy(legacy)
        self.assertNotIn('fund', dict(monitor.SIGNAL_KINDS))
        self.assertEqual(monitor.signal_counts(legacy), monitor.signal_counts(rows))
        self.assertEqual(monitor.signal_count_members(legacy), monitor.signal_count_members(rows))
        self.assertEqual([monitor._sort_key(row) for row in legacy],
                         [monitor._sort_key(row) for row in rows])
        duplicated = legacy + [dict(legacy[0], group='technology')]
        baseline_duplicates = rows + [dict(rows[0], group='technology')]
        self.assertEqual(monitor.signal_counts(duplicated), monitor.signal_counts(baseline_duplicates))
        self.assertEqual(monitor.signal_count_members(duplicated), monitor.signal_count_members(rows))
        self.assertEqual(legacy, before)

    def test_legacy_fund_snapshot_is_sanitized_without_changing_other_signals(self):
        import copy
        rows = [self.member_row('AAA', 'position', 'red', ('chg', 'red')),
                self.member_row('CCC', 'technology', 'green')]
        for row in rows:
            row.update(price=100, chg=0, signals=[])
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        legacy = copy.deepcopy(snap)
        legacy['fundamental'] = {'data': {'AAA': {'data': {'period': '2026-06-30', 'marker': '旧基本面秘密'}}}}
        legacy['effective_settings'].update(fund_enable=1, fund_report_grace_days=90)
        legacy['signal_counts']['fund'] = {'label': '基本面', 'red': 1, 'yellow': 0, 'purple': 0}
        legacy['signal_members']['fund'] = {'red': [{'symbol': 'AAA', 'group': 'position',
                                                   'note': '旧基本面秘密', 'fund_hits': ['旧风险']}],
                                         'yellow': [], 'purple': []}
        before = copy.deepcopy(legacy)
        baseline = monitor.render({}, rows, 2, snapshot=snap)
        page = monitor.render({}, rows, 2, snapshot=legacy)
        self.assertEqual(page, baseline)
        self.assertEqual(monitor._signal_counts_card(legacy), monitor._signal_counts_card(snap))
        self.assertNotIn('基本面', re.sub(r'<template\b[^>]*>.*?</template>', '', page, flags=re.S))
        self.assertNotIn('旧基本面秘密', page)
        self.assertNotIn('fund_enable', page)
        self.assertNotIn('fund_report_grace_days', page)
        self.assertEqual(legacy, before)
        only_fund = {'signal_counts': {'fund': legacy['signal_counts']['fund']},
                     'signal_members': {'fund': legacy['signal_members']['fund']}}
        self.assertEqual(monitor._signal_counts_card(only_fund), '')

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
        rows = [{'symbol': 'AAA', 'price': 100.0, 'level': 'yellow', 'signal_marks': [('boll', 'yellow')]},
                {'symbol': 'BBB', 'price': 100.0, 'level': 'red', 'signal_marks': [('chg', 'red'), ('rsi', 'yellow')]}]
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        page = monitor.render({}, [], 0, snapshot=snap)
        self.assertIn('id="signalCounts"', page)
        self.assertIn('<span class="grp-name">统计</span>', page)
        self.assertNotIn('仅此一条黄灯', page)
        self.assertLess(page.index('id="signalCounts"'), page.index('id="pageNotes"'))
        self.assertNotIn('<div class="foot">', page)
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
        self.assertIn('<div class="signal-filter" id="signalFilterResult" role="status" data-filter="', output)
        self.assertTrue(output.endswith('" hidden></div>' + monitor._collapse_button('signalCounts', '统计')
                                        + '</details>\n'))
        buttons = re.findall(r'<button\b([^>]*\bclass="collapse-group"[^>]*)>(.*?)</button>', output, re.S)
        self.assertEqual(len(buttons), 1)
        attrs, content = buttons[0]
        for attribute in ('type="button"', 'aria-controls="signalCounts"',
                          'aria-label="收起统计"'):
            self.assertIn(attribute, attrs)
        self.assertEqual(content, '<span class="collapse-icon" aria-hidden="true">▲</span>')

    def test_statistics_name_bars_keep_size_and_multi_descriptions_use_text_color(self):
        """单选及多选名称竖条尺寸不变；多选描述移除圆点并独立着色。"""
        css = monitor.PAGE_CSS
        self.assertIn('--lamp-size:14px', css)
        self.assertNotIn('signal-dot', css)
        self.assertNotIn('display:inline list-item', css)
        self.assertIn('#signalCounts .signal-members-panel li:not(.signal-member-split)::before,', css)
        self.assertIn('width:3px;height:12px;border-radius:1px;', css)
        self.assertIn('margin-right:6px;vertical-align:-1px', css)
        self.assertNotIn('#signalCounts .signal-members-panel li::marker', css)
        self.assertNotIn('.signal-filter .stat-dot', css)
        for level, color in (('red', '#ff8580'), ('yellow', '#f0c674'), ('gray', 'var(--text)')):
            self.assertIn(f'#signalCounts .signal-filter-condition[data-signal-level="{level}"]'
                          '{color:' + color + '}', css)
        self.assertNotIn('condition.append(dot', monitor.PAGE_INTERACTION_JS)
        # index.html 是构建产物；直接检查唯一真源，避免依赖旧产物或手工同步。
        script = monitor.PAGE_INTERACTION_JS
        # 名称前仍追加原色竖条，指标描述无圆点。
        seg = script.split('function renderSignalFilter', 1)[1].split('box.append(list);', 1)[0]
        self.assertIn('dot.className = "signal-name-lamp " + m.l;', seg)
        self.assertIn('li.append(dot);', seg)
        self.assertLess(seg.index('li.append(dot);'),
                        seg.index('const sym = document.createElement("strong");'))
        self.assertNotIn('"stat-dot " + m.l', script)

    def test_filter_fact_color_uses_statistics_cell_not_entire_stock_level(self):
        import copy
        row = self.member_row('MIXED', 'position', 'red', ('chg', 'red'), ('vol_ratio', 'yellow'))
        counts = monitor.signal_counts([row])
        members = monitor.signal_count_members([row])
        # 明确把黄格成员的整股灯设为红，防止与正常fixture偶然同色而漏测。
        members['vol_ratio']['yellow'][0]['level'] = 'red'
        snapshot = {'signal_counts': counts, 'signal_members': members}
        before = copy.deepcopy(snapshot)
        data = TestMaMergedStatistics.filters(monitor._signal_counts_card(snapshot))
        self.assertEqual(data['chg']['members'][0]['d'][0]['l'], 'red')
        self.assertEqual(data['vol_ratio']['members'][0]['d'][0]['l'], 'yellow')
        self.assertEqual(data['vol_ratio']['members'][0]['l'], 'red')
        self.assertEqual(counts['vol_ratio']['yellow'], 1)
        self.assertEqual(snapshot, before)

    def test_technical_filter_selection_is_exclusive_and_keeps_other_kinds(self):
        import subprocess
        source = monitor.PAGE_INTERACTION_JS
        branch = source[source.index('  const filterButton = event.target.closest'):source.index('  if (event.target.closest(".signal-filter-clear"))')]
        code = '''
const assert = require('assert/strict');
const buttons = ['rsi', 'boll', 'boll_rsi', 'chg', 'vol_ratio'].map(kind => ({
  dataset: {kind}, pressed: 'false',
  getAttribute() {return this.pressed;},
  setAttribute(name, value) {this.pressed = value;}
}));
const document = {querySelectorAll() {return buttons;}};
let renders = 0;
function renderSignalFilter() {renders++;}
const click = new Function('event', 'document', 'renderSignalFilter', BRANCH);
function tap(kind) {
  click({target: {closest() {return buttons.find(b => b.dataset.kind === kind);}}}, document, renderSignalFilter);
}
function selected() {return buttons.filter(b => b.pressed === 'true').map(b => b.dataset.kind);}
tap('chg'); tap('vol_ratio');
for (const first of ['rsi', 'boll', 'boll_rsi']) {
  for (const next of ['rsi', 'boll', 'boll_rsi'].filter(k => k !== first)) {
    buttons.filter(b => ['rsi', 'boll', 'boll_rsi'].includes(b.dataset.kind)).forEach(b => b.pressed = 'false');
    tap(first); tap(next);
    assert.deepEqual(selected(), [next, 'chg', 'vol_ratio']);
    tap(next);
    assert.deepEqual(selected(), ['chg', 'vol_ratio']);
  }
}
assert.equal(renders, 20);
'''.replace('BRANCH', json.dumps(branch))
        subprocess.run([os.environ.get('NODE') or 'node', '-e', code], check=True,
                       capture_output=True, text=True, encoding='utf-8')

    def test_filter_detail_arrows_use_same_classes_as_panel(self):
        """多选四箭头共用方向类与限域涨跌色；RSI 原继承色/字号基础规则保留。"""
        script = monitor.PAGE_INTERACTION_JS
        seg = script.split('function renderSignalFilter', 1)[1].split('box.append(list);', 1)[0]
        self.assertRegex(seg, r'appendRsiArrows\([^;]*\.t\)')
        self.assertNotIn('signal-dot', seg)
        self.assertIn('signal-filter-condition', seg)
        self.assertIn('condition.dataset.signalLevel = level;', seg)
        self.assertIn('signal-name-lamp ', seg)
        self.assertIn(' / ', seg)
        self.assertNotIn('m.details.join(" · ")', seg)
        self.assertNotIn('detail.textContent = m.details.join', script)
        css = monitor.PAGE_CSS
        rule = [line for line in css.splitlines() if line.startswith('.arrow-rsi-up')]
        self.assertEqual(len(rule), 1, rule)
        self.assertIn('color:inherit', rule[0])
        self.assertIn('font-size:1.15em', rule[0])
        self.assertNotIn('#3ec9d6', rule[0])
        self.assertNotIn('#e061c9', rule[0])
        self.assertNotIn('font-weight:700', rule[0])
        for direction in ('up', 'down'):
            selectors = ','.join(f'{scope} .arrow-{kind}-{direction}'
                                 for scope in ('.stk .sig', '#signalCounts')
                                 for kind in ('band', 'rsi'))
            self.assertIn(selectors + '{color:var(--' + direction + ')}',
                          re.sub(r'\s+', ' ', css).replace(', ', ','))
        arrow_function = 'function appendRsiArrows' + script.split('function appendRsiArrows', 1)[1].split(
            'function renderSignalFilter', 1)[0]
        import subprocess
        code = r'''
const assert = require('assert/strict');
const document = {
  createTextNode(textContent) {return {textContent};},
  createElement(tagName) {assert.equal(tagName, 'span'); return {tagName};}
};
FUNCTION
for (const level of ['red', 'yellow', 'gray', undefined]) {
  const parent = {dataset: {signalLevel: level}, children: [],
    appendChild(node) {this.children.push(node);}};
  const text = '布林▲ / RSI↑ / 布林▼ / RSI↓ <img src=x onerror=bad()> & 保留';
  appendRsiArrows(parent, text);
  assert.deepEqual(parent.children.filter(node => node.tagName).map(node => [node.className, node.textContent]),
    [['arrow-band-up', '▲'], ['arrow-rsi-up', '↑'], ['arrow-band-down', '▼'], ['arrow-rsi-down', '↓']]);
  assert.equal(parent.children.map(node => node.textContent).join(''), text);
  assert.equal(parent.dataset.signalLevel, level);
  assert.ok(parent.children.every(node => !('innerHTML' in node)));
}
'''.replace('FUNCTION', arrow_function)
        subprocess.run([os.environ.get('NODE') or 'node', '-e', code], check=True,
                       capture_output=True, text=True, encoding='utf-8')

    def test_earnings_is_not_added_to_signal_kinds_counts_or_members(self):
        expected = ['chg', 'amp', 'vol_ratio', 'rsi', 'boll', 'boll_rsi',
                    'ma', 'low52', 'add_price', 'reduce_price', 'target']
        self.assertEqual([kind for kind, _ in monitor.SIGNAL_KINDS], expected)
        rows = [self.member_row('AAA', 'position', 'red', *((kind, 'red') for kind in expected)),
                self.member_row('BBB', 'focus', 'yellow', ('earnings', 'yellow'))]
        # 名单使用北京今天起至 +15（含），不随美东交易日回退。
        soon = (monitor.NOW.astimezone(monitor.BJ_TZ).date() + timedelta(days=2)).isoformat()
        rows[1].update(earnings={'status': 'ok', 'date': soon}, signals=['财报临近'])
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(list(snap['signal_counts']), expected)
        self.assertEqual(list(snap['signal_members']), expected)
        for kind in expected:
            with self.subTest(kind=kind):
                excluded = kind in ('target', 'rsi', 'boll')  # 组合互斥，紫不列红黄。
                self.assertEqual(snap['signal_counts'][kind]['red'], 0 if excluded else 1)
                self.assertEqual(snap['signal_counts'][kind]['yellow'], 0)
                self.assertEqual([d['symbol'] for d in snap['signal_members'][kind]['red']],
                                 [] if excluded else ['AAA'])
        output = monitor._signal_counts_card(snap)
        self.assertEqual(output.count('class="signal-members-row"'), len(expected) - 3)
        self.assertNotIn('<td>财报</td>', output)  # 不混入红黄紫交易信号
        self.assertIn('未来15天财报', output)
        self.assertIn('<span class="signal-member-symbol">BBB</span>', output.split('earnings-week', 1)[1])
        self.assertNotIn('>BBB</span>', output.split('earnings-week', 1)[0])

    def test_earnings_members_use_beijing_calendar_day_and_keep_green_visible(self):
        with patch.object(monitor, '_earn_today', return_value='2026-10-13'), \
                patch.object(monitor, '_bj_today', return_value='2026-10-14'):
            def item(symbol, group, date, level='green', status='ok', kind='expected'):
                row = self.member_row(symbol, group, level)
                row.update(price=100.0, chg=0.0, signals=[], earnings={
                    'status': status, 'date': date, 'timing': 'post', 'kind': kind},
                    data_date='2026-10-13')
                return row
            rows = [item('AAPL', 'technology', '2026-10-14'),
                    item('BRK.B-US', 'position', '2026-10-20', kind='official'),   # D 距北京今天 6 天，盘后不加一天
                    item('BRK-B', 'technology', '2026-10-20'),  # 持仓优先，不能重复
                    item('D15', 'focus', '2026-10-29'),   # 第15天（含）进名单
                    item('LATE', 'focus', '2026-10-30'),  # 第16天不进
                    item('PAST', 'position', '2026-10-12'),   # 已是 D+2，退出红窗及名单
                    item('UNKNOWN', 'technology', '2026-10-15', status='unknown')]
            snap = monitor.build_snapshot({}, rows, {'group_monitoring': {'technology': True}}, group_counts={})
            self.assertEqual(snap['earnings_week']['asof'], '2026-10-14')
            self.assertEqual([m['symbol'] for m in snap['earnings_week']['members']], ['AAPL', 'BRK-B', 'D15'])
            self.assertEqual(snap['signal_counts']['chg']['red'], 0)
            self.assertEqual(snap['summary']['green'], 6)  # 同身份按优先分组去重，财报仍不改绿灯
            panel = monitor._signal_counts_card(snap)
            self.assertIn('<details class="earnings-week"><summary>未来15天财报 · 3 只</summary>', panel)
            self.assertNotIn('<details class="earnings-week" open>', panel)
            self.assertIn('<summary class="grp"><span class="grp-name">统计</span><span class="group-stats">'
                          '<span class="stat-badge red" aria-label="红 0 项">0</span>'
                          '<span class="stat-badge yellow" aria-label="黄 0 项">0</span>', panel)
            self.assertIn('白 0', panel)
            self.assertIn('财报 2', panel)
            old = monitor._signal_counts_card({'summary': {'red': 4, 'yellow': 32},
                                               'signal_counts': {'target': {'label': '目标价', 'purple': 3}}})
            for text in ('红 4', '黄 32', '白 0', '财报 —'):
                self.assertIn(text, old)
            self.assertNotIn('紫灯 3 只', old)
            self.assertNotIn('class="signal-member-symbol"', old)
            self.assertNotIn('美东 2026-10-14 至 2026-10-21', panel)
            self.assertNotIn('预计或估算日期可能调整', panel)
            self.assertIn('earnings.open = false;', monitor.PAGE_INTERACTION_JS)
            # 折叠态绝对不占高度：移动端内核不重排时用 CSS 兜底
            self.assertIn('#signalCounts .earnings-week:not([open])>*:not(summary){display:none}', monitor.PAGE_CSS)
            self.assertIn('#signalCounts .earnings-week summary{padding:8.1px 0;color:var(--text)', monitor.PAGE_CSS)
            self.assertIn('#signalCounts td{padding:6.3px 5px;', monitor.PAGE_CSS)  # 统计面板压缩上下留白
            self.assertIn('.signal-count-toggle{box-sizing:border-box;height:38px;width:44px;', monitor.PAGE_CSS)  # 数字胶囊扁长方形
            self.assertIn('.signal-filter-toggle{box-sizing:border-box;height:38px;', monitor.PAGE_CSS)  # 与信号名胶囊同高对齐
            # 财报的灯跟统计表其它栏目一致：行首圆点（marker），不再在行尾挂一个 stat-dot
            # 跟其它名单同一套：名字可点跳转 + 分组写在括号里 + 灰色小字说明，不再有加粗蓝色分组
            self.assertIn('<li class="lamp-green"><a class="signal-member-jump" href="#s-aapl">'
                          '<span class="signal-member-symbol">AAPL</span></a>'
                          '<span class="signal-member-belong">（IT软硬Ai）</span> ', panel)
            self.assertIn('10/14 盘后 · 预计</span><span class="lamp-text">（绿灯）</span>', panel)
            self.assertIn('<span class="signal-member-belong">（短持仓）</span> '
                          '<span class="earnings-week-meta is-yellow">美东 10/20 盘后 · 官方活动日期</span>'
                          '<span class="lamp-text">（绿灯）</span>', panel)
            self.assertNotIn('earnings-week-group', panel)
            self.assertNotIn('<strong>', panel.split('earnings-week', 1)[1])
            self.assertNotIn('stat-dot', panel)
            self.assertNotIn('· 绿灯', panel)
            self.assertNotIn('earnings-week-group', monitor.PAGE_CSS)
            red = monitor._earnings_week_panel({'earnings_week': {'asof': '2026-10-14', 'members': [
                {'symbol': 'NOC', 'group': 'position', 'level': 'red', 'date': '2026-10-20'},
                {'symbol': 'BAD', 'group': 'position', 'level': '<x>', 'date': '2026-10-20'}]}})
            self.assertIn('<li class="lamp-red"><a class="signal-member-jump" href="#s-noc">', red)
            self.assertIn('<li><a class="signal-member-jump" href="#s-bad">', red)          # 未知灯级不画灯、不注入
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

    def test_earnings_row_and_statistics_use_the_same_source_timing(self):
        import html as H
        date = '2026-10-30'
        for kind in ('official', 'expected', 'estimated'):
            for timing, label in (('pre', '盘前'), ('post', '盘后'),
                                  ('during', '盘中'), ('', '时段待定')):
                with self.subTest(kind=kind, timing=timing):
                    e = {'status': 'ok', 'date': date, 'timing': timing, 'kind': kind}
                    if kind == 'official':
                        e['official_error'] = 'http_429'
                    row = self.member_row('AAPL', 'position', 'green')
                    row.update(price=100.0, chg=0.0, signals=[], rsi={}, dist_high=None,
                               dist_low=None, vol_ratio=None, trigger=None, boll_up=None,
                               boll_dn=None, earnings=e, data_date='2026-10-20')
                    with patch.object(monitor, '_bj_today', return_value='2026-10-20'), \
                            patch.object(monitor, 'TARGET_DATE', '2026-10-20'), \
                            patch.object(monitor, 'global_dca', return_value=None):
                        snap = monitor.build_snapshot({}, [row], {'positions': {'AAPL': {}}})
                        page = monitor.render({}, [row], 1, snapshot=snap)
                    member, = snap['earnings_week']['members']
                    self.assertEqual((member['date'], member['timing'], member['kind']),
                                     (date, timing, kind))
                    duplicate = dict(row, group='technology', earnings=dict(
                        e, timing='pre' if timing == 'post' else 'post', kind='expected'))
                    self.assertEqual(monitor.earnings_week_members([duplicate, row], '2026-10-20'),
                                     [member])
                    panel = monitor._signal_counts_card(snap)
                    self.assertIn(f'美东 10/30 {label} · ', panel)
                    self.assertEqual(monitor._earnings_week_panel(snap, count_only=True), 1)
                    data = json.loads(H.unescape(re.search(r'data-filter="([^"]*)"', panel).group(1)))
                    self.assertTrue(data['earnings_week']['ok'])
                    filtered, = data['earnings_week']['members']
                    self.assertEqual(filtered['d'][0]['l'], 'gray')   # 10/30 距基准 10 天：不红不黄，跟财报名单同为灰字
                    self.assertIn(f'财报 {date} {label} · ', filtered['d'][0]['t'])
                    position = page.split('id="group_positions"', 1)[1].split('id="group_focus"', 1)[0]
                    self.assertIn('无异动 1 只', position)
                    folded = position.split('<details class="quiet-list">', 1)[1]
                    main, note = monitor.earnings_label(e)
                    self.assertIn(label, main)
                    self.assertIn(f'{main}<span class="unit">{note}</span>', folded)
                    self.assertEqual(snap['summary']['green'], 1)
                    self.assertTrue(all(c['red'] == c['yellow'] == 0 for c in snap['signal_counts'].values()))
                    for today, urgency in (('2026-10-28', 'yellow'), ('2026-10-29', 'red'), ('2026-10-30', 'red'),
                                           ('2026-10-31', 'red'), ('2026-11-01', '')):
                        self.assertEqual(monitor.earnings_urgency(e, today), urgency)
                        self.assertEqual(monitor.earnings_within_week(e, today), bool(urgency))

    def test_earnings_list_15_days_and_folding_share_beijing_boundaries(self):
        import copy
        import html as H
        from datetime import date, datetime
        for bj_day in ('2026-01-15', '2026-07-15', '2026-10-11', '2026-12-31'):
            for timing in ('pre', 'post', 'during', '', 'unknown'):
                with self.subTest(bj_day=bj_day, timing=timing), ExitStack() as stack:
                    stack.enter_context(patch.object(monitor, 'NOW', datetime.fromisoformat(bj_day + 'T00:00:00+08:00')))
                    stack.enter_context(patch.object(monitor, 'TARGET_DATE', '2026-01-02'))
                    stack.enter_context(patch.object(monitor, 'global_dca', return_value=None))
                    rows = []
                    for symbol, gap in (('OLD', -2), ('YESTERDAY', -1), ('TODAY', 0),
                                        ('TOMORROW', 1), ('D2', 2), ('D7', 7), ('D8', 8),
                                        ('D15', 15), ('D16', 16)):
                        e = {'status': 'ok', 'date': (date.fromisoformat(bj_day) + timedelta(days=gap)).isoformat(),
                             'timing': timing, 'kind': 'expected'}
                        row = self.member_row(symbol, 'technology', 'green')
                        row.update(price=100, chg=0, signals=[], earnings=e, data_date='2026-01-02')
                        rows.append(row)
                    for symbol, status in (('PENDING', 'unknown'), ('ERROR', 'error')):
                        row = self.member_row(symbol, 'technology', 'green')
                        row.update(price=100, chg=0, signals=[], earnings={'status': status, 'date': bj_day})
                        rows.append(row)
                    before = copy.deepcopy(rows)
                    snap = monitor.build_snapshot({}, rows, {}, group_counts={})
                    expected = ['YESTERDAY', 'TODAY', 'TOMORROW', 'D2', 'D7', 'D8', 'D15']
                    self.assertEqual(snap['earnings_week']['asof'], bj_day)
                    self.assertEqual([m['symbol'] for m in snap['earnings_week']['members']], expected)
                    self.assertEqual(snap['summary']['green'], len(rows))
                    self.assertTrue(all(c['red'] == c['yellow'] == 0 for c in snap['signal_counts'].values()))
                    panel = monitor._signal_counts_card(snap)
                    self.assertIn('未来15天财报 · 7 只', panel)
                    self.assertEqual(panel.count('earnings-week-meta is-red'), 3)
                    self.assertEqual(panel.count('earnings-week-meta is-yellow'), 2)
                    self.assertEqual(monitor._earnings_week_panel(snap, count_only=True), 7)
                    filtered = json.loads(H.unescape(re.search(r'data-filter="([^"]*)"', panel).group(1)))
                    self.assertTrue(filtered['earnings_week']['ok'])
                    self.assertEqual([m['id'] for m in filtered['earnings_week']['members']], sorted(expected))
                    body = monitor._group_body(rows, {'item_group': 'technology'}, snap, bj_day, True, 'group_technology', '科技')
                    main, folded = body.split('<details class="quiet-list">', 1)
                    self.assertIn('无异动 6 只', folded)
                    for symbol in expected[:5]:
                        self.assertIn(f'id="s-{symbol.lower()}"', main)
                        self.assertNotIn(f'id="s-{symbol.lower()}"', folded)
                    for symbol in ('OLD', 'D8', 'D15', 'D16', 'PENDING', 'ERROR'):
                        self.assertIn(f'id="s-{symbol.lower()}"', folded)
                        self.assertNotIn(f'id="s-{symbol.lower()}"', main)
                    self.assertEqual(rows, before)

    def test_earnings_week_rejects_bad_date_missing_and_expired(self):
        with patch.object(monitor, '_earn_today', return_value='2026-10-14'), \
                patch.object(monitor, '_bj_today', return_value='2026-10-14'):
            rows = [self.member_row(name, 'position', 'green') for name in ('UNKNOWN', 'BROKEN', 'OLD', 'FUTURE')]
            for row, e in zip(rows, ({'status': 'unknown', 'date': '2026-10-15'},
                                     {'status': 'ok', 'date': '2026-10-50'},
                                     {'status': 'ok', 'date': '2026-10-12'},
                                     {'status': 'ok', 'date': '2026-10-30'})):
                row.update(price=100.0, chg=0.0, signals=[], earnings=e)
            self.assertEqual(monitor.earnings_week_members(rows, '2026-10-14'), [])
            self.assertNotIn('未来15天财报', monitor._signal_counts_card(
                monitor.build_snapshot({}, rows, {}, group_counts={})))

    def test_text_only_boll_rsi_detail_names_rsi_count(self):
        row = {'symbol': 'XLP', 'group': 'focus', 'level': 'green', 'note': '',
               'signals': ['盘中突破布林上轨▲（盘中触及）', 'RSI 2条 ↑超买（RSI6 80）']}
        member = monitor.text_only_members([row])[0]
        self.assertEqual(member['kind'], 'boll_rsi')
        self.assertEqual(member['detail'], '盘中突破布林上轨▲ · RSI 2条 ↑超买')

    def test_snapshot_carries_signal_counts(self):
        snap = monitor.build_snapshot({}, [self.row('red', ('chg', 'red'))], {}, group_counts={})
        self.assertEqual(snap['signal_counts']['chg']['red'], 1)
        self.assertEqual(snap['signal_members']['chg']['red'],
                         [{'symbol': 'T', 'note': '', 'group': '', 'hold': '', 'level': 'red'}])

    @staticmethod
    def member_row(symbol, group, level, *marks, note=''):
        row = {'symbol': symbol, 'group': group, 'note': note, 'price': 100.0,
               'level': level, 'signal_marks': list(marks)}
        if ('target', 'purple') in marks:
            row.update(price=100, purple=True, target=monitor.evaluate_target(
                100, 100, {'kind': 'consensus', 'price': 101, 'failed': False}, group, 3))
        return row

    def test_filter_members_share_single_panel_group_and_anchor(self):
        import copy
        import html as H
        rows = [dict(self.member_row('LONG', 'position', 'red', ('chg', 'red')), hold='long'),
                self.member_row('SHORT', 'position', 'yellow', ('chg', 'yellow')),
                self.member_row('BRK.B', 'focus', 'red', ('chg', 'red')),
                dict(self.member_row('GRAY', 'position', 'green'), hold='long',
                     signals=['RSI 2条 ↑超买（≥70）'])]
        snap = {'signal_counts': monitor.signal_counts(rows),
                'signal_members': monitor.signal_count_members(rows),
                'text_only': monitor.text_only_members(rows)}
        before = copy.deepcopy(snap)
        output = monitor._signal_counts_card(snap)
        data = json.loads(H.unescape(re.search(r'data-filter="([^"]*)"', output).group(1)))
        members = [m for f in data.values() for m in f['members']]
        self.assertEqual({m['s'] for m in members}, {monitor.normalize_symbol(r['symbol']) for r in rows})
        for row in rows:
            member = next(m for m in members if m['s'] == monitor.normalize_symbol(row['symbol']))
            self.assertEqual(member['g'], monitor._member_belong(row))
            self.assertEqual(member['a'], monitor._stock_anchor_id(row['symbol']))
            self.assertIn(f'href="#{member["a"]}"', output)
            self.assertIn(f'（{member["g"]}）', output)
        self.assertEqual(snap, before)
        script = monitor.PAGE_INTERACTION_JS.split('function renderSignalFilter', 1)[1].split('box.append(list);', 1)[0]
        self.assertIn('link.className = "signal-member-jump";', script)
        self.assertIn('link.setAttribute("href", "#" + m.a);', script)
        self.assertIn('link.append(note);', script)
        self.assertIn('group.className = "signal-member-belong";', script)
        self.assertIn('group.textContent = "（" + m.g + "）";', script)
        self.assertNotIn('signal-filter-group', monitor.PAGE_CSS)

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
        self.assertEqual(sorted(ids('boll')), ['AAA', 'DDD'])  # 布林只含单独布林
        self.assertTrue(set(ids('boll')).isdisjoint(ids('boll_rsi')))
        self.assertEqual(ids('boll_rsi'), ['CCC'])
        aaa = next(m for m in data['vol_ratio']['members'] if m['id'] == 'AAA')
        self.assertEqual((aaa['g'], aaa['l'], aaa['a']), ('短持仓', 'red', 's-aaa'))
        self.assertEqual(snap['signal_counts']['boll']['yellow'], 2)  # 筛选和计数都是单独布林
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
                    'const c=intersectSignals(d,["vol_ratio","boll_rsi"]);'
                    'const exclusive=intersectSignals(d,["boll","boll_rsi"]);'
                    'process.stdout.write(JSON.stringify([r.members.map(m=>m.id),c.members[0].details,p.pending,exclusive.members]));')
            out = json.loads(subprocess.run([node, '-e', code], capture_output=True, text=True, encoding='utf-8', check=True).stdout)
            self.assertEqual(out[0], ['AAA'])
            self.assertEqual(out[1][1], {'l': 'red', 't': '布林+RSI', 'k': 'boll_rsi'})  # 缺方向事实保留信号名兜底
            self.assertEqual(out[2], ['布林'])
            self.assertEqual(out[3], [])
        self.assertIn('button.setAttribute("aria-pressed", "false");', monitor.PAGE_INTERACTION_JS)  # 收起统计时清空选择

    def test_purple_and_earnings_are_strict_overlay_conditions(self):
        import html as H
        with patch.object(monitor, '_earn_today', return_value='2026-10-14'), \
                patch.object(monitor, '_bj_today', return_value='2026-10-14'):
            def item(symbol, group, marks=(), earnings_date=None):
                row = self.member_row(symbol, group, 'green', *marks)
                row['earnings'] = ({'status': 'ok', 'date': earnings_date, 'timing': 'post', 'kind': 'expected'}
                                   if earnings_date else {'status': 'unknown'})
                return row
            rows = [item('BOTH', 'position', (('target', 'purple'),), '2026-10-18'),
                    item('PURPLE', 'focus', (('target', 'purple'),), '2026-11-30'),
                    item('EARN', 'technology', (), '2026-10-20'),
                    item('ANOM', 'focus', (('target', 'red'),), '2026-10-19'),
                    item('SOON', 'focus', (), '2026-10-15'),      # D+1：红
                    item('FAR', 'focus', (), '2026-10-25')]       # D+11：灰（不红不黄）
            snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        output = monitor._signal_counts_card(snap)
        data = json.loads(H.unescape(re.search(r'data-filter="([^"]*)"', output).group(1)))
        ids = lambda kind: [m['s'] for m in data[kind]['members']]
        self.assertEqual(set(ids('target_purple')), {'BOTH', 'PURPLE'})
        self.assertEqual(set(ids('earnings_week')), {'BOTH', 'EARN', 'ANOM', 'SOON', 'FAR'})
        self.assertNotIn('ANOM', ids('target_purple'))  # 目标价异常红不混入紫灯条件
        # 财报条件带自身红/黄/灰（与财报名单同一套），不再整条白字；紫灯条件仍是紫字。
        self.assertEqual({m['s']: m['d'][0]['l'] for m in data['earnings_week']['members']},
                         {'SOON': 'red', 'BOTH': 'yellow', 'EARN': 'yellow', 'ANOM': 'yellow', 'FAR': 'gray'})
        for member in data['target_purple']['members']:
            self.assertEqual(member['d'][0]['l'], 'purple')
        for kind in ('target_purple', 'earnings_week'):
            for member in data[kind]['members']:
                row = next(r for r in rows if r['symbol'] == member['s'])
                self.assertEqual(member['g'], monitor._member_belong(row))
                self.assertEqual(member['a'], monitor._stock_anchor_id(row['symbol']))
        self.assertIn('data-kind="target_purple"', output)
        self.assertIn('data-kind="earnings_week"', output)
        # 两块叠加条件：紫灯在上、财报在下；按钮文案就是「紫灯」「财报」，不再带「条件」二字。
        self.assertLess(output.index('purple-extra'), output.index('earnings-extra'))
        self.assertIn('aria-label="选择紫灯叠加条件">紫灯</button>', output)
        self.assertIn('aria-label="选择财报叠加条件">财报</button>', output)
        self.assertNotIn('紫灯条件', output)
        self.assertNotIn('财报条件', output)
        node = os.environ.get('NODE')
        if node:
            import subprocess
            script = monitor.PAGE_INTERACTION_JS.split('function intersectSignals', 1)[1].split('function renderSignalFilter', 1)[0]
            code = ('function intersectSignals' + script + ';const d=' + json.dumps(data) + ';'
                    'process.stdout.write(JSON.stringify(intersectSignals(d,["target_purple","earnings_week"]).members.map(m=>m.s)));')
            result = json.loads(subprocess.run([node, '-e', code], capture_output=True, text=True,
                                               encoding='utf-8', check=True).stdout)
            self.assertEqual(result, ['BOTH'])

    def panel(self, output, kind, level):
        import hashlib
        panel_id = f'signal-members-{hashlib.sha256(kind.encode("utf-8")).hexdigest()}-{level}'
        match = re.search(r'<div class="signal-members-panel tone-' + level + r'" id="' + panel_id
                          + r'" hidden>(.*?)</div>(?=<div class="signal-members-panel|</td>)',
                          output, re.S)
        self.assertIsNotNone(match, output)
        return match.group(1)



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


    def test_member_panels_are_toned_so_bullets_follow_the_light(self):
        """分类面板保持自身等级，名称前竖条独立跟随整股主表灯级。"""
        rows = [self.member_row('AAA', 'position', 'red', ('chg', 'red'), ('rsi', 'yellow')),
                self.member_row('BBB', 'focus', 'green', ('target', 'purple'))]
        output = monitor._signal_counts_card(monitor.build_snapshot({}, rows, {}, group_counts={}))
        self.assertIn('<div class="signal-members-panel tone-red" id="'
                      + self.panel_id('chg') + '-red" hidden>', output)
        self.assertIn('<div class="signal-members-panel tone-yellow" id="'
                      + self.panel_id('rsi') + '-yellow" hidden>', output)
        self.assertIn('class="text-only target-purple"', output)
        self.assertIn('data-kind="target_purple"', output)
        self.assertIn('<li class="lamp-red"><a class="signal-member-jump" href="#s-aaa">',
                      self.panel(output, 'rsi', 'yellow'))
        white_rows = [{'symbol': 'AAA', 'group': 'position', 'level': 'red', 'kind': 'rsi',
                       'detail': 'RSI 2条 ↓超卖', 'rsi_hits': 2, 'rsi_side': '超卖'}]
        white_output = monitor._signal_counts_card({'text_only': white_rows})
        self.assertIn('<li class="lamp-red"><a class="signal-member-jump" href="#s-aaa">', white_output)
        self.assertIn('class="signal-members-panel tone-gray"', white_output)
        for level, color in (('red', 'var(--red)'), ('yellow', 'var(--yellow)'),
                             ('green', 'var(--green)'), ('gray', 'var(--text)'), ('unknown', 'var(--dim)')):
            self.assertIn(f'#signalCounts .signal-members-panel li.lamp-{level}{{--name-lamp:{color}}}', monitor.PAGE_CSS)
        self.assertIn('#signalCounts .signal-members-panel.tone-red,#signalCounts .signal-name-lamp.red,', monitor.PAGE_CSS)
        self.assertIn('#signalCounts .text-only .signal-member-list,#signalCounts .signal-name-lamp.gray{--name-lamp:#9fa3aa}', monitor.PAGE_CSS)

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
            self.member_row('UP', 'position', 'yellow', ('rsi', 'yellow'),
                            ('ma_cross', 'yellow'), ('ma_band', 'yellow'), ('target', 'purple')),
            self.member_row('DOWN', 'focus', 'red', ('rsi', 'red'),
                            ('ma_cross', 'yellow'), ('ma_band', 'yellow'), ('target', 'purple')),
            self.member_row('LEVEL', 'technology', 'yellow', ('ma_band', 'yellow')),
            self.member_row('SPACE', 'focus', 'green', ('target', 'purple')),
            self.member_row('CROSS', 'focus', 'green', ('target', 'purple')),
        ]
        rows[0].update(signals=['RSI 2条 ↑超买（≥70）：RSI6=80',
                                '上穿60日均线', '日内触及120日均线 ±1%区间',
                                '在共识价 200 ±3% 内（偏离 +1%）'],
                       ma_band_direction='up')
        rows[1].update(signals=['RSI 3条 ↓超卖（≤30）：RSI6=20',
                                '跌破120日均线', '现价贴近120日均线 -0.1%（仅按现价）',
                                '今日跌破目标价 90'], ma_band_direction='down')
        rows[2].update(signals=['日内触及120日均线 ±1%区间'], ma_band_direction='level')
        rows[0]['target'] = monitor.evaluate_target(202, 202, {'kind': 'consensus', 'price': 200}, 'position', 3)
        rows[1]['target'] = monitor.evaluate_target(89, 91, {'kind': 'manual', 'price': 90}, 'focus', 3)
        rows[3].update(signals=['共识价 155，空间 +55%'], price=100,
                       target=monitor.evaluate_target(100, 100, {'kind': 'consensus', 'price': 155}, 'focus', 3))
        rows[4].update(signals=['今日上穿共识价 200'], price=202,
                       target=monitor.evaluate_target(202, 198, {'kind': 'consensus', 'price': 200}, 'focus', 3))
        # 单独布林与RSI使用不同标的，避免把旧组合fixture误当成两个独立栏目。
        for symbol, text in (('BOLLUP', '突破布林上轨▲ 100'), ('BOLLDOWN', '触及布林下轨▼ 90')):
            row = self.member_row(symbol, 'position', 'yellow', ('boll', 'yellow'))
            row['signals'] = [text]
            rows.append(row)
        for row in rows:
            row['ma_settings'] = {'ma_short': 60, 'ma_long': 120}
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        cases = {
            ('rsi', 'yellow'): ['<h5>RSI 2条 <span class="arrow-rsi-up">↑</span>超买</h5>'],
            ('rsi', 'red'): ['<h5>RSI 3条 <span class="arrow-rsi-down">↓</span>超卖</h5>'],
            ('boll', 'yellow'): [f'<h5><span class="grp-arrow arrow-band-up">▲</span> 突破上轨</h5>', f'<h5><span class="grp-arrow arrow-band-down">▼</span> 触及下轨</h5>'],
        }
        output = monitor._signal_counts_card(snap)
        for (kind, level), expected in cases.items():
            panel = (output.split('class="text-only target-purple"', 1)[1].split('</details>', 1)[0]
                     if kind == 'target' and level == 'purple' else self.panel(output, kind, level))
            self.assertNotRegex(panel, r'<p><strong>.*?信号.*?</strong></p>')
            for title in expected:
                with self.subTest(kind=kind, title=title):
                    self.assertIn(title, panel)
            self.assertNotIn('signal-member-icon', panel)
        # 均线合并一栏：同一成员的穿越与触及事实都写在标题里，不重复名单。
        for kind, titles in (('ma', [f'<span class="grp-arrow arrow-band-up">▲</span> 短均线上穿，触及长均线，现价在长均线上方',
                                   f'<span class="grp-arrow arrow-band-down">▼</span> 长均线跌破，现价贴近长均线，现价在长均线下方',
                                   '■ 触及长均线，现价与长均线相等']),):
            panel = self.panel(output, kind, 'yellow')
            with self.subTest(kind=kind):
                self.assertEqual(panel.count('class="signal-member-block"'), len(titles))
                for title in titles:
                    self.assertIn(f'<h5>{title}</h5>', panel)
                # 同一指标内部只用「，」；白色「 / 」只留给指标之间切换。
                self.assertNotIn('signal-member-icon', panel)
        ma_panel = self.panel(output, 'ma', 'yellow')
        self.assertNotIn(' / ', ' '.join(re.findall(r'<h5>(.*?)</h5>', ma_panel)))
        self.assertEqual(snap['signal_counts']['target']['purple'], 4)
        target = output.split('class="text-only target-purple"', 1)[1].split('</details>', 1)[0]
        # 竖条跟随个股自身灯级（UP 黄 / DOWN 红 / SPACE、CROSS 绿），不因紫灯条件染紫。
        self.assertEqual(target.count('class="signal-name-lamp '), 4)
        self.assertEqual(target.count('class="signal-name-lamp yellow"'), 1)
        self.assertEqual(target.count('class="signal-name-lamp red"'), 1)
        self.assertEqual(target.count('class="signal-name-lamp green"'), 2)
        self.assertNotIn('class="signal-name-lamp tone-purple"', target)
        self.assertNotIn('signal-member-icon', target)
        self.assertIn('今日上穿共识价 200', target)
        self.assertIn('今日跌破目标价 90', target)
        self.assertNotIn('>▲</span>', target)
        self.assertNotIn('>▼</span>', target)
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
                         '量比 2.4×（2026-10-13）')
        output = monitor._signal_counts_card(snap)
        amp_yellow = self.panel(output, 'amp', 'yellow')
        amp_red = self.panel(output, 'amp', 'red')
        volume = self.panel(output, 'vol_ratio', 'yellow')
        # 量比/振幅/涨跌幅都按方向分框：三角只出现在框标题（上涨绿／下跌红），名单里不再逐条画三角
        self.assertIn(f'<h5><span class="grp-arrow arrow-band-up">▲</span> 上涨</h5>', amp_yellow)
        self.assertIn(f'<h5><span class="grp-arrow arrow-band-down">▼</span> 下跌</h5>', amp_red)
        self.assertIn(f'<h5><span class="grp-arrow arrow-band-up">▲</span> 上涨</h5>', volume)
        self.assertIn(f'<h5><span class="grp-arrow arrow-band-down">▼</span> 下跌</h5>', volume)
        self.assertIn('<h5>平盘</h5>', volume)
        self.assertNotIn('signal-member-icon', volume)
        self.assertIn('振幅 5.4%', amp_yellow)
        self.assertIn('振幅 7.1%', amp_red)
        self.assertIn('量比 2.4×（2026-10-13）', volume)
        self.assertIn('量比 1.6×（2026-10-13）', volume)
        self.assertNotIn('量比数字取标注的完整交易日，不一定与股价三角同日', volume)
        self.assertNotIn('振幅为本轮行情日高低价之差占昨收的比例', amp_yellow)
        self.assertIn(f'<h5><span class="grp-arrow arrow-band-down">▼</span> 下跌</h5>', amp_red)
        self.assertNotIn('title=', output)

    def test_change_members_show_signed_percent_and_independent_arrow_direction(self):
        rows = [self.member_row('UP', 'position', 'red', ('chg', 'red')),
                self.member_row('DOWN', 'focus', 'yellow', ('chg', 'yellow'))]
        rows[0].update(chg=6.237, data_date='2026-10-14')
        rows[1].update(chg=-1.45, data_date='2026-10-14')
        page = monitor._signal_counts_card(monitor.build_snapshot({}, rows, {}, group_counts={}))
        red, yellow = self.panel(page, 'chg', 'red'), self.panel(page, 'chg', 'yellow')
        self.assertIn(f'<h5><span class="grp-arrow arrow-band-up">▲</span> 上涨</h5>', red)
        self.assertIn('涨跌幅 +6.24%', red)
        self.assertIn(f'<h5><span class="grp-arrow arrow-band-down">▼</span> 下跌</h5>', yellow)
        self.assertIn('涨跌幅 -1.45%', yellow)
        self.assertNotIn('tone-red', red)
        self.assertNotIn('白▲ / 白▼', yellow)

    def test_boll_range_entry_is_white_and_intraday_break_is_yellow(self):
        cases = [('逼近布林上轨▲ 还差0.4%', 'tone-white', '▲', '逼近上轨'),
                 ('触及布林上轨▲ 100', 'tone-white', '▲', '触及上轨'),
                 ('盘中突破布林上轨▲ 100（盘中触及 102）', 'tone-yellow', '▲', '盘中突破上轨'),
                 ('突破布林上轨▲ 100', 'tone-yellow', '▲', '突破上轨'),
                 ('逼近布林下轨▼ 还差0.4%', 'tone-white', '▼', '逼近下轨'),
                 ('触及布林下轨▼ 90', 'tone-white', '▼', '触及下轨'),
                 ('跌破布林下轨▼ 90（盘中触及 89）', 'tone-yellow', '▼', '盘中跌破下轨')]
        for text, tone, shape, name in cases:
            with self.subTest(text=text):
                row = self.member_row('BOLL', 'position', 'yellow', ('boll', 'yellow'))
                row.update(boll_alert_text=text, signals=[text])
                page = self.panel(monitor._signal_counts_card(monitor.build_snapshot({}, [row], {}, group_counts={})),
                                  'boll', 'yellow')
                arrow_class = 'arrow-band-up' if shape == '▲' else 'arrow-band-down'
                self.assertIn(f'<h5><span class="grp-arrow {arrow_class}">{shape}</span> {name}</h5>', page)
                self.assertIn(f'<span class="signal-member-metric">{name}</span>', page)
                self.assertNotIn('tone-red', page)
        row = self.member_row('INFO', 'technology', 'green', ('boll', 'yellow'))
        row.update(boll_alert_text='', signals=['盘中突破布林上轨▲ 100'])
        member = monitor.signal_count_members([row])['boll']['yellow'][0]
        self.assertNotIn('indicators', member)  # 其他组只有上轨说明，不能伪造黄灯图标

    def test_rsi_three_overbought_is_yellow_and_oversold_is_red_regardless_of_overall_lamp(self):
        rows = [self.member_row('BX', 'position', 'red', ('rsi', 'yellow'), ('chg', 'red')),
                self.member_row('TWO_HIGH', 'focus', 'yellow', ('rsi', 'yellow')),
                self.member_row('THREE_LOW', 'focus', 'red', ('rsi', 'red')),
                self.member_row('THREE_HIGH', 'position', 'red', ('rsi', 'yellow'), ('chg', 'red'))]
        rows[0]['signals'] = ['RSI 2条 ↓超卖（≤30）：RSI6=22 / RSI12=27 / RSI24=33']
        rows[1]['signals'] = ['RSI 2条 ↑超买（≥70）：RSI6=81 / RSI12=72 / RSI24=67']
        rows[2]['signals'] = ['RSI 3条 ↓超卖（≤30）：RSI6=19 / RSI12=24 / RSI24=29']
        rows[3]['signals'] = ['RSI 3条 ↑超买（≥70）：RSI6=85 / RSI12=79 / RSI24=71']
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual((snap['signal_counts']['rsi']['yellow'], snap['signal_counts']['rsi']['red']), (3, 1))
        self.assertEqual(snap['signal_members']['rsi']['yellow'][0]['rsi_hits'], 'RSI 2条 ↓超卖')
        output = monitor._signal_counts_card(snap)
        yellow = self.panel(output, 'rsi', 'yellow')
        red = self.panel(output, 'rsi', 'red')
        # 方向写到框标题上（↑上涨绿 / ↓下跌红），名单里不再逐条画三角
        self.assertIn('<h5>RSI 2条 <span class="arrow-rsi-down">↓</span>超卖</h5>', yellow)
        self.assertIn('<h5>RSI 2条 <span class="arrow-rsi-up">↑</span>超买</h5>', yellow)
        self.assertIn('<h5>RSI 3条 <span class="arrow-rsi-down">↓</span>超卖</h5>', red)
        self.assertIn('<h5>RSI 3条 <span class="arrow-rsi-up">↑</span>超买</h5>', yellow)
        self.assertIn('RSI 2条 ↓超卖', plain_text(yellow))
        self.assertIn('RSI 3条 ↑超买', plain_text(yellow))
        self.assertNotIn('RSI 3条 ↑超买', plain_text(red))
        self.assertNotIn('2条为黄，3条为红', yellow)
        self.assertNotIn('tone-red" role="img" aria-label="RSI 2条 ↓超卖"', yellow)
        for panel, count in ((yellow, 3), (red, 1)):
            lines = re.findall(r'<li\b[^>]*>(.*?)</li>', panel, re.S)
            self.assertEqual(len(lines), count)
            self.assertNotIn('signal-member-metric', panel)
            self.assertTrue(all(not re.search(r'RSI|条|超买|超卖', plain_text(line)) for line in lines))
        # 去掉单选名单行尾重复文字，不应删掉多选交集的真实条数、方向和独立灯级。
        filtered = TestMaMergedStatistics.filters(output)['rsi']['members']
        self.assertEqual({m['s']: m['d'] for m in filtered}, {
            'BX': [{'l': 'yellow', 't': 'RSI 2条 ↓超卖'}],
            'TWO_HIGH': [{'l': 'yellow', 't': 'RSI 2条 ↑超买'}],
            'THREE_LOW': [{'l': 'red', 't': 'RSI 3条 ↓超卖'}],
            'THREE_HIGH': [{'l': 'yellow', 't': 'RSI 3条 ↑超买'}]})

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
        self.assertIn('<h5>平盘</h5>', self.panel(page, 'amp', 'yellow'))       # 方向不明归「平盘」框
        self.assertIn('<h5>平盘</h5>', self.panel(page, 'vol_ratio', 'yellow'))  # 量比方向不明归「平盘」框
        self.assertIn('条数待下次更新', self.panel(page, 'rsi', 'yellow'))

    def test_target_same_symbol_can_show_solid_and_hollow_squares_without_double_count(self):
        row = self.member_row('BOTH', 'focus', 'green', ('target', 'purple'), ('target', 'purple'))
        row.update(price=100, target=monitor.evaluate_target(
            100, 200, {'kind': 'manual', 'price': 190}, 'focus', 3),
            signals=['今日跌破目标价 190', '目标价 190，高空间 +90%'])
        snap = monitor.build_snapshot({}, [row], {}, group_counts={})
        self.assertEqual(snap['signal_counts']['target']['purple'], 1)
        self.assertEqual(len(snap['signal_members']['target']['purple']), 1)
        self.assertEqual(len(snap['target_overlays']['space']), 1)
        self.assertEqual(len(snap['target_overlays']['reached']), 1)
        output = monitor._signal_counts_card(snap)
        output = output.split('class="text-only target-purple"', 1)[1].split('</details>', 1)[0]
        self.assertEqual(output.count('class="signal-name-lamp '), 2)
        # 竖条跟随个股自身灯级（BOTH 绿灯），高空间段仍靠 li.target-space-high 区分。
        self.assertEqual(output.count('class="signal-name-lamp green"'), 2)
        self.assertNotIn('class="signal-name-lamp tone-purple"', output)
        self.assertNotIn('class="signal-name-lamp tone-target-high"', output)
        self.assertIn('class="target-space-high"', output)
        self.assertNotIn('signal-member-icon', output)
        self.assertIn('今日跌破目标价 190', output)
        self.assertNotIn('title=', output)
        self.assertEqual(output.count('href="#s-both"'), 2)

    def test_old_snapshot_missing_icons_does_not_infer_direction(self):
        row = self.member_row('OLDER', 'position', 'yellow', ('rsi', 'yellow'), ('boll', 'yellow'))
        snap = monitor.build_snapshot({}, [row], {}, group_counts={})
        output = monitor._signal_counts_card(snap)
        self.assertIn('条数待下次更新', self.panel(output, 'rsi', 'yellow'))
        self.assertIn('状态待下次更新', self.panel(output, 'boll', 'yellow'))
        for kind in ('rsi', 'boll'):
            self.assertNotIn('signal-member-icon', self.panel(output, kind, 'yellow'))

    def test_icon_data_is_whitelisted_and_escaped(self):
        counts = {'rsi': {'label': 'RSI', 'yellow': 1}}
        member = {'symbol': 'X', 'note': '', 'group': 'focus', 'indicators': [
            {'shape': '▲', 'tone': 'yellow', 'label': '<img src=x onerror=alert(1)>'},
            {'shape': '<svg>', 'tone': 'red', 'label': 'bad'},
            {'shape': '□', 'tone': 'purple', 'label': '只允许目标价空间使用'},
            {'shape': '■', 'tone': '\" onclick=alert(1)', 'label': 'bad'},
        ]}
        icons = monitor._member_icons(member, 'rsi', 'yellow')
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', icons)
        self.assertNotIn('<img ', icons)
        self.assertNotIn('<svg>', icons)
        self.assertNotIn('onclick=', icons)
        panel = self.panel(monitor._signal_counts_card({'signal_counts': counts,
            'signal_members': {'rsi': {'yellow': [member]}}}), 'rsi', 'yellow')
        self.assertNotIn('<img ', panel)
        self.assertNotIn('<svg>', panel)
        self.assertNotIn('onclick=', panel)
        self.assertNotIn('signal-member-icon', panel)   # 方向已移到框标题，名单里不再逐条画三角

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
        for label in ('<h5>上轨<span class="arrow-band-up">▲</span> · RSI 2条 <span class="arrow-rsi-up">↑</span>超买</h5>', '<h5>下轨<span class="arrow-band-down">▼</span> · RSI 3条 <span class="arrow-rsi-down">↓</span>超卖</h5>',
                      '<h5>上轨<span class="arrow-band-up">▲</span> · RSI 3条 <span class="arrow-rsi-down">↓</span>超卖 · 反向</h5>', '<h5>方向待下次更新</h5>'):
            self.assertIn(label, output)
        # 黄档同样分框
        yellow = self.panel(monitor._signal_counts_card({'signal_counts': counts,
                                                         'signal_members': members}), 'boll_rsi', 'yellow')
        self.assertIn('<h5>下轨<span class="arrow-band-down">▼</span> · RSI 2条 <span class="arrow-rsi-down">↓</span>超卖</h5>', yellow)
        self.assertIn('RSI 2条', output)
        self.assertIn('RSI 3条', output)
        self.assertIn('>UP</span><span class="note">上方</span></a>', output)  # 备注名回到代码后面
        self.assertIn('class="signal-member-jump"', output)  # 名字可点跳转，不再写分组名
        # 方向全在框标题上：名单行里既没有三角图标，也没有方向文字
        self.assertEqual(output.count('class="signal-member-direction"'), 0)
        self.assertEqual(output.count('class="signal-member-icon'), 0)

    def test_reverse_display_helper_preserves_original_direction_and_signal_data(self):
        import copy
        for direction, expected in (
            ('上轨（RSI超卖，方向不一致）', '上轨▲ · RSI 3条 ↓超卖 · 反向'),
            ('下轨（RSI超买，方向不一致）', '下轨▼ · RSI 3条 ↑超买 · 反向')):
            with self.subTest(direction=direction):
                row = self.member_row('REVERSE', 'position', 'yellow', ('boll_rsi', 'yellow'))
                row.update(boll_rsi_direction=direction, boll_rsi_hits=3)
                snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
                before = copy.deepcopy(snapshot)
                self.assertEqual(monitor.boll_rsi_text(direction, 3), expected)
                self.assertEqual(monitor._parse_boll_rsi_direction(direction)[2], '方向不一致')
                self.assertEqual(row['boll_rsi_direction'], direction)
                self.assertEqual(snapshot['signal_members']['boll_rsi']['yellow'][0]['direction'], direction)
                output = monitor._signal_counts_card(snapshot)
                self.assertIn(expected, plain_text(self.panel(output, 'boll_rsi', 'yellow')))
                self.assertEqual(TestMaMergedStatistics.filters(output)['boll_rsi']['members'][0]['d'],
                                 [{'l': 'yellow', 't': '布林' + expected.replace(' · 反向', '（反）')}])
                self.assertEqual(snapshot['signal_counts']['boll_rsi']['yellow'], 1)
                self.assertEqual(row['signal_marks'], [('boll_rsi', 'yellow')])
                self.assertEqual(snapshot, before)

    def test_combination_counts_follow_matching_rsi_side_but_red_lamp_stays_unchanged(self):
        # 保留红黄 tone 与计数；方向类独立使用涨跌色，不改变条件灯级。
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
                head = monitor.boll_rsi_text(direction, hits)
                self.assertIn(head, plain_text(page))   # 方向 + 条数写在框标题上
                arrow_class = 'arrow-band-up' if shape == '▲' else 'arrow-band-down'
                member = snap['signal_members']['boll_rsi'][level][0]
                self.assertEqual(monitor._member_icons(member, 'boll_rsi', level),
                                 f'<span class="signal-member-icon {tone} {arrow_class}" role="img" '
                                 f'aria-label="{head}">{shape}</span>')
                self.assertIn(f'<span class="{arrow_class}">{shape}</span>', page)
                self.assertIn(f'RSI {hits}条', page)
                self.assertIn('上轨' if direction.startswith('上轨') else '下轨', page)  # 轨道方向要可见
                self.assertNotIn('signal-member-icon', page)      # 名单里不再逐条画三角
                self.assertEqual(snap['signal_counts']['boll_rsi'][level], 1)
        conflict = self.member_row('CONFLICT', 'position', 'red', ('boll_rsi', 'red'))
        conflict.update(boll_rsi_direction='下轨（RSI超买，方向不一致）', boll_rsi_hits=3)
        page = self.panel(monitor._signal_counts_card(monitor.build_snapshot({}, [conflict], {}, group_counts={})),
                          'boll_rsi', 'red')
        self.assertIn('<h5>下轨<span class="arrow-band-down">▼</span> · RSI 3条 <span class="arrow-rsi-up">↑</span>超买 · 反向</h5>', page)  # 轨道方向明确，照样分框
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
        rows[0].update(signals=['上穿250日均线'], hold='long', ma_settings={'ma_short': 50, 'ma_long': 250})
        rows[1].update(signals=['跌破120日均线'], ma_settings={'ma_short': 120, 'ma_long': 250})
        rows[2].update(signals=['日内触及250日均线 ±1%区间'], ma_band_direction='up', hold='short',
                       ma_settings={'ma_short': 50, 'ma_long': 250})
        rows[3].update(signals=['日内触及250日均线 ±1%区间'], ma_band_direction='down',
                       ma_settings={'ma_short': 50, 'ma_long': 250})
        output = monitor._signal_counts_card({'signal_counts': monitor.signal_counts(rows),
                                              'signal_members': monitor.signal_count_members(rows)})
        panel = self.panel(output, 'ma', 'yellow')
        for title in ('<span class="grp-arrow arrow-band-up">▲</span> 长均线上穿', '<span class="grp-arrow arrow-band-down">▼</span> 短均线跌破',
                      '<span class="grp-arrow arrow-band-up">▲</span> 触及长均线，现价在长均线上方',
                      '<span class="grp-arrow arrow-band-down">▼</span> 触及长均线，现价在长均线下方'):
            self.assertIn(f'<h5>{title}</h5>', panel)
        for group in ('长持仓', '重点', '短持仓', '金融'):
            self.assertIn(f'<span class="signal-member-belong">（{group}）</span>', panel)
        self.assertNotIn('signal-member-icon', panel)

    def test_boll_rsi_states_the_direction_only_in_the_block_title(self):
        """布林+RSI 的框标题已经写了「下轨▼ · RSI 2条」，行尾不再重复同一句。"""
        row = self.member_row('BOTH', 'position', 'yellow', ('boll_rsi', 'yellow'))
        row.update(boll_rsi_direction='下轨（超卖）', boll_rsi_hits=2, hold='long')
        output = monitor._signal_counts_card({'signal_counts': monitor.signal_counts([row]),
                                              'signal_members': monitor.signal_count_members([row])})
        page = self.panel(output, 'boll_rsi', 'yellow')
        self.assertIn('<h5>下轨<span class="arrow-band-down">▼</span> · RSI 2条 <span class="arrow-rsi-down">↓</span>超卖</h5>', page)
        self.assertEqual(page.count('RSI 2条'), 1)   # 只有框标题一处
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
        """仅规则表允许的技术灰按归因分类，不把仅文字或无提示计灰。"""
        rows = [{'symbol': 'RSIONLY', 'group': 'position', 'level': 'green',
                 'signal_marks': [], 'signals': ['RSI 2条 ↑超买（≥70）']},
                {'symbol': 'BOLLONLY', 'group': 'focus', 'level': 'green',
                 'signal_marks': [], 'signals': ['逼近布林下轨▼ 还差0.30%']},
                {'symbol': 'BOTH', 'group': 'technology', 'level': 'green',
                 'signal_marks': [], 'signals': ['RSI 1条 ↓超卖（≤35）', '触及布林下轨▼']},
                {'symbol': 'OLDUP', 'group': 'technology', 'level': 'green',
                 'signal_marks': [], 'signals': ['RSI 2条 ↑超买（≥70）', '触及布林上轨▲']},
                {'symbol': 'OLDRSI', 'group': 'technology', 'level': 'green',
                 'signal_marks': [], 'signals': ['RSI 2条 ↑超买（≥70）']},
                {'symbol': 'LIT', 'group': 'position', 'level': 'yellow',
                 'signal_marks': [('boll', 'yellow')], 'signals': ['逼近布林下轨▼ 还差0.10%']},
                {'symbol': 'QUIET', 'group': 'technology', 'level': 'green',
                 'signal_marks': [], 'signals': []}]
        members = monitor.text_only_members(rows)
        self.assertEqual([(m['symbol'], m['kind'], m['shape']) for m in members],
                         [('RSIONLY', 'rsi', '▲'), ('BOLLONLY', 'boll', '▼'), ('BOTH', 'boll_rsi', '▼')])
        # 灰灯三行不进红黄计数；LIT 行是真黄灯，本来就该计进 boll.yellow
        counts = monitor.signal_counts(rows)
        self.assertEqual([(k, v['yellow']) for k, v in counts.items() if v['yellow']], [('boll', 1)])
        output = monitor._signal_counts_card({'signal_counts': counts,
                                              'signal_members': monitor.signal_count_members(rows),
                                              'text_only': members})
        self.assertNotIn('仅此一条黄灯', output)
        self.assertIn('<span class="stat-badge gray" aria-label="白 3 项">3</span>', output)
        # 灰灯现在在统计表按归因分行；每行的灰色计数可展开详细名单
        self.assertNotRegex(output, r'<td>(?:灰灯|白灯)')   # 最左列只写 RSI / 布林 / 布林+RSI，不加灯级前缀
        self.assertLess(output.index('aria-pressed="false">RSI</button>'), output.index('aria-pressed="false">布林</button>'))
        # 技术三栏与规则表格同序：组合条件（布林+RSI）排在单独布林、单独 RSI 之前
        self.assertLess(output.index('aria-pressed="false">布林+RSI</button>'),
                        output.index('aria-pressed="false">布林</button>'))
        self.assertIn('<h5>RSI 2条 <span class="arrow-rsi-up">↑</span>超买</h5>', output)
        self.assertIn('<h5>下轨<span class="arrow-band-down">▼</span></h5>', output)
        self.assertIn('<h5>下轨<span class="arrow-band-down">▼</span> · RSI 1条 <span class="arrow-rsi-down">↓</span>超卖</h5>', output)
        self.assertNotIn('OLDUP', output)
        self.assertNotIn('OLDRSI', output)
        self.assertNotIn('逼近布林', output.split('signal-members-gray-')[1])
        self.assertEqual(output.count('class="signal-count-toggle"'), 4)  # 3 条灰灯 + LIT 黄灯
        self.assertIn('signal-member-jump', output)
        self.assertNotIn('(持仓)', output)
        self.assertNotIn('(IT软硬Ai)', output)

    def test_statistics_header_orders_red_yellow_gray_purple_then_earnings(self):
        rows = [self.member_row('RED', 'position', 'red', ('chg', 'red')),
                self.member_row('PURPLE', 'focus', 'green', ('target', 'purple'))]
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        from copy import deepcopy
        before = deepcopy(snap)
        output = monitor._signal_counts_card(snap)
        header = output.split('</summary>', 1)[0]
        tokens = ['stat-badge red', 'stat-badge yellow', 'stat-badge gray',
                  'stat-badge purple', 'count-earn']
        self.assertEqual([header.index(t) for t in tokens], sorted(header.index(t) for t in tokens))
        self.assertIn('<span class="stat-badge purple" aria-label="紫 1 项">1</span>', header)
        self.assertIn('<span class="stat-badge gray" aria-label="白 0 项">0</span>', header)
        # 统计表不再有「信号 / 红 / 黄 / 白」表头行：整排删除，第一行就是信号行
        self.assertNotIn('class="gray mh"', output)
        self.assertNotRegex(output, r'>白</td>')
        self.assertNotRegex(output, r'>灰<|aria-label="(?:灰灯|灰 \d+ 项)')
        self.assertEqual(snap, before)

    def test_statistics_header_purple_zero_and_unknown_do_not_guess(self):
        snap = monitor.build_snapshot({}, [self.member_row('RED', 'position', 'red', ('chg', 'red'))], {}, group_counts={})
        header = monitor._signal_counts_card(snap).split('</summary>', 1)[0]
        self.assertIn('<span class="stat-badge purple" aria-label="紫 0 项">0</span>', header)
        for value in (None, True, -1, '3'):
            with self.subTest(value=value):
                snap['summary']['purple'] = value
                header = monitor._signal_counts_card(snap).split('</summary>', 1)[0]
                self.assertIn('<span class="count-total count-purple">紫 —</span>', header)
                self.assertNotIn('stat-badge purple', header)

    def test_purple_only_and_missing_quote_keep_original_levels(self):
        rows = [self.member_row('PURPLE', 'focus', 'green', ('target', 'purple'), ('target', 'purple')),
                self.member_row('GRAY', 'position', 'gray', ('target', 'purple'))]
        rows[1].update(price=None, purple=False, target=monitor.evaluate_target(
            None, 100, {'kind': 'consensus', 'price': 101}, 'position', 3))
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        self.assertEqual(snap['signal_counts']['target']['purple'], 1)
        self.assertEqual((snap['summary']['green'], snap['summary']['gray'], snap['summary']['yellow']), (1, 1, 0))
        output = monitor._signal_counts_card(snap)
        self.assertIn('class="text-only target-purple"', output)
        self.assertRegex(output, r'空间\s*0\s*只[\s　]*达到\s*1\s*只')
        self.assertNotIn('>GRAY</span>', output)
        self.assertIn('>PURPLE</span>', output)
        self.assertIn('data-kind="target_purple"', output)
        self.assertEqual(output.count('class="signal-count-toggle"'), 0)
        self.assertNotIn('>紫</td>', output)
        self.assertNotIn('紫灯 2 只', output)
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
                     'level': 'yellow', 'ma_settings': {key: monitor.S.get(key) for key in ('ma_short', 'ma_long')}}]
        self.assertEqual(members['ma']['yellow'], expected)
        for kind in ('boll', 'rsi', 'chg'):
            self.assertEqual(members[kind]['yellow'], [])
        # 统计与成员使用同一优先归属；不把同标的在低优先分组里的指标再计一次。
        counts = monitor.signal_counts(rows)
        self.assertEqual([counts[k]['yellow'] for k in ('boll', 'rsi', 'ma', 'chg')], [0, 0, 1, 0])
        output = monitor._signal_counts_card({'signal_counts': counts, 'signal_members': members})
        self.assertIn('class="signal-member-jump"', self.panel(output, 'ma', 'yellow'))
        self.assertNotIn('行业备注', output)
        self.assertNotIn('data-kind="boll"', output)

    def test_every_nonzero_cell_has_accessible_hidden_adjacent_panel(self):
        rows = [self.member_row('AAA', 'position', 'red', ('rsi', 'red'), ('rsi', 'yellow'), ('target', 'purple')),
                self.member_row('BBB', 'focus', 'yellow', ('rsi', 'yellow'))]
        snap = monitor.build_snapshot({}, rows, {}, group_counts={})
        output = monitor._signal_counts_card(snap)
        buttons = re.findall(r'<button type="button" class="signal-count-toggle" data-target="([^"]+)" '
                             r'aria-controls="([^"]+)" aria-expanded="false" aria-label="([^"]+)">(\d+)</button>', output)
        expected_buttons = sum(bool(v[level]) for v in snap['signal_counts'].values()
                               for level in ('red', 'yellow'))
        self.assertEqual(len(buttons), expected_buttons)
        self.assertEqual(len({b[0] for b in buttons}), expected_buttons)
        panels = re.findall(r'<div class="signal-members-panel tone-\w+" id="([^"]+)" hidden>', output)
        self.assertEqual([b[0] for b in buttons], panels)
        for target, controls, label, n in buttons:
            self.assertEqual(target, controls)
            self.assertIn(f'{n} 只', label)
        # 只有有计数的行才出现（RSI 行 + 目标价行）
        pairs = re.findall(r'<tr><td>.*?</tr><tr class="signal-members-row"><td colspan="4">.*?</td></tr>', output, re.S)
        self.assertEqual(len(pairs), 1)  # 紫灯移出统计表的计数行，单独折叠展示
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
        good_row = self.member_row('GOOD', 'focus', 'green', ('target', 'purple'), note='有效备注')
        good_snap = monitor.build_snapshot({}, [good_row], {}, group_counts={})
        good = good_snap['signal_members']['target']['purple'][0]
        invalid_lists = (None, [], [{'symbol': 'ONE', 'note': '不可信', 'group': 'position'}],
                         [{'symbol': 'A'}, {'symbol': 'B'}, {'symbol': 'C'}],
                         [{'symbol': 'BRK.B'}, {'symbol': 'BRK-B'}],
                         [{'symbol': ''}, {'symbol': 'B'}], ['A', 'B'])
        for entries in invalid_lists:
            with self.subTest(entries=entries):
                output = monitor._signal_counts_card({'signal_counts': counts,
                           'signal_members': {'chg': {'red': entries}, 'target': {'purple': [good]}},
                           'target_overlays': good_snap['target_overlays']})
                panel = self.panel(output, 'chg', 'red')
                self.assertNotIn('红色信号 · 2 只', panel)
                self.assertIn('明细待下次更新', panel)
                self.assertNotIn('class="signal-member-symbol"', panel)
                self.assertNotIn('不可信', output)
                target = output.split('class="text-only target-purple"', 1)[1]
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


class TestTargetOverlays(unittest.TestCase):
    """30/70 两名单契约；所有行情和共识价均为离线合成样本。"""

    @staticmethod
    def row(symbol, group='technology', target_price=190, price=100, prev=100, **extra):
        target = monitor.evaluate_target(price, prev,
            {'kind': 'consensus', 'price': target_price, 'failed': False}, group, 3)
        return dict(symbol=symbol, group=group, note='测试备注', hold='', price=price,
                    chg=0, level='green', data_date='2026-10-20', target=target,
                    purple=bool(target['hit'] or target['space_hit']), signal_marks=[], signals=[], **extra)

    @staticmethod
    def snapshot(rows):
        with patch.object(monitor, 'global_dca', return_value=None):
            return monitor.build_snapshot({}, rows, {}, group_counts={})

    def test_snapshot_lists_records_and_union_counts(self):
        rows = [self.row('SPACE', target_price=140), self.row('HIGH'),
                self.row('HIT', 'position', 101), self.row('BOTH', 'focus', 190, prev=200),
                self.row('LOW', target_price=139.999)]
        snap = self.snapshot(rows)
        overlays = snap['target_overlays']
        self.assertEqual(set(overlays), {'space', 'reached'})
        self.assertEqual({m['symbol'] for m in overlays['space']}, {'SPACE', 'HIGH', 'BOTH'})
        self.assertEqual({m['symbol'] for m in overlays['reached']}, {'HIT', 'BOTH'})
        fields = {'symbol', 'note', 'group', 'hold', 'level', 'space', 'band', 'price', 'kind', 'detail'}
        for entries in overlays.values():
            for member in entries:
                self.assertTrue(fields.issubset(member))
                row = next(r for r in rows if r['symbol'] == member['symbol'])
                self.assertEqual(member['price'], row['target']['price'])
                self.assertEqual(member['space'], row['target']['space'])
                self.assertEqual(member['band'], row['target']['band'])
                self.assertEqual(member['kind'], 'consensus')
                self.assertTrue(member['detail'])
        self.assertEqual(snap['summary']['purple'], 4)
        self.assertEqual(snap['signal_counts']['target']['purple'], 4)
        header = monitor._signal_counts_card(snap).split('</summary>', 1)[0]
        self.assertIn('<span class="stat-badge purple" aria-label="紫 4 项">4</span>', header)
        self.assertEqual({m['symbol'] for m in snap['signal_members']['target']['purple']}, {'SPACE', 'HIGH', 'HIT', 'BOTH'})
        self.assertEqual(snap['signal_counts']['target']['red'], 0)
        self.assertEqual(snap['signal_counts']['target']['yellow'], 0)

    def test_identity_dedup_uses_groups_priority_and_first_note(self):
        industry = self.row('BRK-B', 'technology', 250)
        position = self.row('BRK.B-US', 'position', 101)
        position.update(note='优先备注', hold='long')
        later = self.row('BRK.B', 'position', 250)
        later['note'] = '后来备注'
        snap = self.snapshot([industry, position, later])
        self.assertEqual(snap['target_overlays']['space'], [])
        member, = snap['target_overlays']['reached']
        self.assertEqual((member['symbol'], member['group'], member['hold'], member['note']),
                         ('BRK-B', 'position', 'long', '优先备注'))
        self.assertEqual(snap['summary']['purple'], 1)
        self.assertEqual(snap['signal_counts']['target']['purple'], 1)
        self.assertEqual(len(snap['signal_members']['target']['purple']), 1)

    def test_priority_low_or_failed_row_does_not_revive_lower_group_space(self):
        for target in ({'kind': 'consensus', 'price': 250, 'failed': True},
                       {'kind': 'consensus', 'price': 120, 'failed': False}):
            with self.subTest(target=target):
                first = self.row('AAA', 'position', 120)
                first['target'] = monitor.evaluate_target(100, 100, target, 'position', 3)
                first['purple'] = False
                snap = self.snapshot([self.row('AAA', 'technology', 250), first])
                self.assertEqual(snap['target_overlays'], {'space': [], 'reached': []})
                self.assertEqual(snap['signal_counts']['target']['purple'], 0)

    def test_all_group_stock_space_but_reached_only_position_focus(self):
        rows = []
        expected = set()
        for index, group in enumerate(monitor.GROUPS):
            name = 'STOCK' + str(index)
            rows.append(self.row(name, group['item_group'], 190, prev=200))
            expected.add(name)
        snap = self.snapshot(rows)
        self.assertEqual({m['symbol'] for m in snap['target_overlays']['space']}, expected)
        self.assertEqual({m['group'] for m in snap['target_overlays']['reached']}, {'position', 'focus'})
        self.assertEqual(snap['signal_counts']['target']['purple'], len(expected))

    def test_etf_index_futures_and_failed_missing_quotes_excluded(self):
        rows = []
        for group in (g['item_group'] for g in monitor.GROUPS):
            for symbol, extra in (('XLK', {}), ('^GSPC', {}), ('CL=F', {}),
                                   ('ETFTEST', {'etf': True}), ('INDEXTEST', {'asset': 'index'}),
                                   ('FUTTEST', {'asset': 'futures'})):
                rows.append(self.row(symbol, group, **extra))
        bad = self.row('FAIL', 'focus', 250)
        bad['target'] = monitor.evaluate_target(100, 100, {'kind': 'consensus', 'price': 250, 'failed': True}, 'focus', 3)
        missing = self.row('MISS', 'position', 190, price=None)
        snap = self.snapshot(rows + [bad, missing])
        self.assertEqual(snap['target_overlays'], {'space': [], 'reached': []})
        self.assertEqual(snap['summary']['purple'], 0)
        self.assertEqual(snap['signal_counts']['target']['purple'], 0)
        self.assertEqual(snap['signal_members']['target']['purple'], [])

    def test_legacy_target_marks_never_resurrect_anomaly_or_purple(self):
        rows = [dict(symbol='OLD', group='focus', note='', price=100, chg=0, level='green',
                     purple=True, signal_marks=[('target', 'red'), ('target', 'yellow'), ('target', 'purple')],
                     signals=['目标价疑似数据异常'])]
        snap = self.snapshot(rows)
        self.assertEqual(snap['target_overlays'], {'space': [], 'reached': []})
        self.assertEqual([snap['signal_counts']['target'][lv] for lv in ('red', 'yellow', 'purple')], [0, 0, 0])
        self.assertEqual(snap['summary']['purple'], 0)

    def test_two_sections_high_first_squares_direction_and_union_filter(self):
        import html as H
        rows = [self.row('MED', 'position', 140), self.row('HIGH', 'technology', 250),
                self.row('BOTH', 'focus', 190, prev=200), self.row('UP', 'position', 100, prev=99)]
        rows[2]['signal_marks'] = [('chg', 'red')]
        rows[2]['level'] = 'red'
        rows[2]['chg'] = -20
        snap = self.snapshot(rows)
        output = monitor._signal_counts_card(snap)
        target = output.split('class="text-only target-purple"', 1)[1].split('</details>', 1)[0]
        self.assertRegex(target, r'空间\s*3\s*只[\s　]*达到\s*2\s*只')
        self.assertNotRegex(target, r'紫灯\s*\d+\s*只')
        self.assertEqual(target.count('<ul'), 2)
        self.assertLess(target.index('>HIGH</span>'), target.index('>MED</span>'))
        self.assertEqual(target.count('class="signal-name-lamp '), 5)
        self.assertNotIn('signal-member-icon', target)
        self.assertRegex(target, r'(?:tone-target-high|target-space-high)')
        self.assertIn('今日跌破共识价 190', target)
        self.assertIn('今日上穿共识价 100', target)
        self.assertIn('共识价 250，空间 +150.0%', target)
        self.assertNotRegex(target, r'高空间|中空间|普通空间|模拟')
        self.assertNotIn('>▲</span>', target)
        self.assertNotIn('>▼</span>', target)
        self.assertEqual(target.count('href="#s-both"'), 2)
        self.assertIn('href="#s-high"', target)
        self.assertIn('（IT软硬Ai）', target)
        self.assertIn('测试备注', target)
        data = json.loads(H.unescape(re.search(r'data-filter="([^"]*)"', output)[1]))
        union = data['target_purple']
        self.assertTrue(union['ok'])
        self.assertEqual({m['id'] for m in union['members']}, {'MED', 'HIGH', 'BOTH', 'UP'})
        self.assertEqual(len(union['members']), 4)
        self.assertEqual({m['id'] for m in union['members']} & {m['id'] for m in data['chg']['members']}, {'BOTH'})
        self.assertNotIn('>目标价</button>', output)
        self.assertNotIn('>共识价</button>', output)

    def test_space_order_preserves_bands_then_raw_space_descending_across_groups(self):
        import copy
        rows = [self.row('HOLDLOW', 'position', 190),
                self.row('HIGHEST', 'technology', 260),
                self.row('FOCUSMID', 'focus', 220),
                self.row('MEDLOW', 'position', 145),
                self.row('MEDHIGH', 'technology', 180),
                self.row('TIEZ', 'position', 160),
                self.row('TIEA', 'technology', 160),
                self.row('PRECZ', 'technology', 160.044),
                self.row('PRECA', 'position', 160.041),
                self.row('ZREACHED', 'position', 101),
                self.row('AREACHED', 'focus', 100)]
        snap = self.snapshot(rows)
        before = copy.deepcopy(snap)
        panel = monitor._target_purple_panel(snap)
        spaces, reached = panel.split('<h5>空间</h5>', 1)[1].split('<h5>达到</h5>', 1)
        symbols = lambda text: re.findall(r'class="signal-member-symbol">([^<]+)</span>', text)
        expected = ['HIGHEST', 'FOCUSMID', 'HOLDLOW', 'MEDHIGH',
                    'PRECZ', 'PRECA', 'TIEA', 'TIEZ', 'MEDLOW']
        self.assertEqual(symbols(spaces), expected)
        # PRECA/PRECZ显示同为60.0%，排序仍按未舍入原值，不按代码或分组。
        precision = {m['symbol']: m for m in snap['target_overlays']['space']
                     if m['symbol'] in ('PRECA', 'PRECZ')}
        self.assertEqual([round(m['space'], 1) for m in precision.values()], [60.0, 60.0])
        self.assertGreater(precision['PRECZ']['space'], precision['PRECA']['space'])
        self.assertEqual(symbols(reached), ['ZREACHED', 'AREACHED'])
        self.assertEqual(snap, before)
        card = monitor._signal_counts_card(snap)
        self.assertEqual(symbols(card.split('<h5>空间</h5>', 1)[1].split('<h5>达到</h5>', 1)[0]), expected)
        # 反转输入不能改变清单排序、成员或计数。
        reversed_snap = self.snapshot(list(reversed(rows)))
        self.assertEqual(monitor._target_purple_panel(reversed_snap), panel)
        self.assertEqual(reversed_snap['summary']['purple'], snap['summary']['purple'])

    def test_space_order_legacy_missing_values_last_with_band_priority_unchanged(self):
        import copy
        def member(symbol, band, value):
            return {'symbol': symbol, 'group': 'technology', 'band': band,
                    'space': value, 'detail': '旧快照说明，不从文字猜空间'}
        snap = {'target_overlays': {
            'space': [member('LOWBAND', 'medium', 500), member('HIGHBAND', 'high', 90),
                      member('GOOD', 'medium', 60), member('MISSING', 'medium', None),
                      member('NAN', 'medium', float('nan')), member('INF', 'medium', float('inf')),
                      member('STRING', 'medium', '70'), member('BOOLEAN', 'medium', True)],
            'reached': []}}
        missing = snap['target_overlays']['space'][3]
        del missing['space']
        before = copy.deepcopy(snap)
        panel = monitor._target_purple_panel(snap)
        symbols = re.findall(r'class="signal-member-symbol">([^<]+)</span>', panel)
        self.assertEqual(symbols, ['HIGHBAND', 'LOWBAND', 'GOOD',
                                   'BOOLEAN', 'INF', 'MISSING', 'NAN', 'STRING'])
        self.assertNotIn('明细待下次更新', panel)
        self.assertEqual(snap, before)

    def test_old_snapshot_target_red_yellow_tables_are_not_rendered(self):
        old = {'summary': {'red': 1, 'yellow': 1, 'purple': 0},
               'signal_counts': {'chg': {'label': '涨跌幅', 'red': 1},
                                 'target': {'label': '目标价', 'red': 9, 'yellow': 8, 'purple': 0}},
               'signal_members': {'chg': {'red': [{'symbol': 'LIVE', 'group': 'focus'}]},
                                  'target': {'red': [{'symbol': 'ANOM', 'detail': '疑似数据异常'}],
                                             'yellow': [{'symbol': 'OLDTARGET'}]}}}
        output = monitor._signal_counts_card(old)
        self.assertIn('>LIVE</span>', output)
        self.assertNotIn('ANOM', output)
        self.assertNotIn('OLDTARGET', output)
        self.assertNotIn('疑似数据异常', output)
        self.assertNotIn('>目标价</button>', output)
        self.assertNotIn('data-kind="target"', output)

    def test_snapshot_and_overlay_render_do_not_mutate_inputs(self):
        import copy
        rows = [self.row('BOTH', 'focus', prev=200)]
        before = copy.deepcopy(rows)
        snap = self.snapshot(rows)
        self.assertEqual(rows, before)
        saved = copy.deepcopy(snap)
        monitor._signal_counts_card(snap)
        self.assertEqual(snap, saved)


class TestSignalDescriptionLamps(unittest.TestCase):
    def test_arrow_html_wraps_all_four_symbols_and_preserves_escaped_text(self):
        text = '▲↑▼↓ / ▲▼ / &lt;img src=x onerror=bad()&gt; &amp; 保留'
        expected = ('<span class="arrow-band-up">▲</span><span class="arrow-rsi-up">↑</span>'
                    '<span class="arrow-band-down">▼</span><span class="arrow-rsi-down">↓</span>'
                    ' / <span class="arrow-band-up">▲</span><span class="arrow-band-down">▼</span>'
                    ' / &lt;img src=x onerror=bad()&gt; &amp; 保留')
        self.assertEqual(monitor.arrow_html(text), expected)
        self.assertEqual(plain_text(expected), text)

    def test_main_description_arrow_wrappers_preserve_body_lamp_and_row_data(self):
        import copy
        for level in ('red', 'yellow', 'gray'):
            for side, triangle, band_class in (('上轨', '▲', 'arrow-band-up'),
                                                ('下轨', '▼', 'arrow-band-down')):
                for direction, symbol, rsi_class in (('超买', '↑', 'arrow-rsi-up'),
                                                       ('超卖', '↓', 'arrow-rsi-down')):
                    with self.subTest(level=level, side=side, direction=direction):
                        boll = f'触及布林{side}{triangle}'
                        rsi = f'RSI 3条 {symbol}{direction}'
                        row = {'symbol': 'ARROWBODY', 'group': 'technology', 'price': 100, 'chg': 1,
                               'note': '', 'level': 'red', 'signals': [boll, rsi],
                               'signal_marks': [('boll_rsi', level)]}
                        before = copy.deepcopy(row)
                        reverse = (side, direction) in (('上轨', '超卖'), ('下轨', '超买'))
                        body = (f'触及布林{side}<span class="{band_class}">{triangle}</span> · '
                                f'RSI 3条 <span class="{rsi_class}">{symbol}</span>{direction}'
                                + ('（反）' if reverse else ''))
                        attr = f' data-signal-level="{level}"'
                        expected = f'<span class="signal-item" data-signal-kind="boll_rsi"{attr}>{body}</span>'
                        output = monitor._signals_html(row)
                        self.assertEqual(output, expected)
                        self.assertIn('<td class="sig">' + expected + '</td>', monitor._stock_row(row, {}))
                        self.assertEqual(row, before)
                        row['level'] = 'yellow'
                        self.assertEqual(monitor._signals_html(row), output)
        unlit = {'signals': ['无灯说明 ▲↑▼↓'], 'signal_marks': [], 'level': 'red'}
        self.assertEqual(monitor._signals_html(unlit),
                         '<span class="signal-item" data-signal-kind="">无灯说明 '
                         '<span class="arrow-band-up">▲</span><span class="arrow-rsi-up">↑</span>'
                         '<span class="arrow-band-down">▼</span><span class="arrow-rsi-down">↓</span></span>')
        self.assertEqual(unlit, {'signals': ['无灯说明 ▲↑▼↓'], 'signal_marks': [], 'level': 'red'})
        for level, color in (('red', '#ff8580'), ('yellow', '#f0c674'), ('gray', 'var(--text)')):
            self.assertIn(f'.signal-item[data-signal-level="{level}"]{{color:{color}}}', monitor.PAGE_CSS)
        self.assertIn('.signal-item{color:var(--dim)}', monitor.PAGE_CSS)

    def test_legacy_amp_gray_retired_without_losing_technical_foldability(self):
        import copy
        analyzer = TestBollRsiRules()
        analyzer.setUp()
        self.addCleanup(analyzer.doCleanups)
        for group in ('position', 'focus', 'technology'):
            for bands in ((100, 101, 90), (100, 110, 99), (100, 110, 90)):
                for values in ([50, 50, 50], [70, 50, 50], [35, 50, 50],
                               [70, 70, 50], [35, 35, 50], [35, 35, 35]):
                    with self.subTest(group=group, bands=bands, values=values):
                        row = analyzer.analyze(values, group, bands=bands)
                        legacy = copy.deepcopy(row)
                        legacy['amp'] = 2
                        legacy['signals'].append('振幅 2.0%（99~101）')
                        legacy['signal_marks'].append(['amp', 'gray'])
                        legacy['foldable'] = False
                        before = copy.deepcopy(legacy)
                        clean = monitor._sanitize_snapshot_fields(legacy)
                        self.assertEqual(clean['foldable'], row['foldable'])
                        self.assertEqual(clean['signals'], row['signals'])
                        self.assertEqual(clean['signal_marks'], row['signal_marks'])
                        self.assertEqual(clean['amp'], 2)
                        self.assertEqual(clean['level'], row['level'])
                        self.assertNotIn('data-signal-level="gray">振幅', monitor._signals_html(clean))
                        self.assertEqual(legacy, before)
                        self.assertEqual(monitor._sanitize_snapshot_fields(clean), clean)
        row = {'symbol': 'LEGACYAMP', 'group': 'technology', 'price': 100,
               'level': 'green', 'foldable': False, 'amp': 2,
               'signals': ['振幅 2.0%'], 'signal_marks': [('amp', 'gray')]}
        clean = monitor._sanitize_snapshot_fields(row)
        self.assertEqual(clean['signals'], [])
        self.assertEqual(clean['signal_marks'], [])
        self.assertTrue(clean['foldable'])
        self.assertFalse(monitor._quiet_keep(clean, None))
        for extra in ({'signals': ['RSI 未知事实']}, {'signals': ['触及布林未知轨']},
                      {'signal_marks': [('amp', 'gray'), ('boll_rsi', 'gray')]},
                      {'boll_alert_text': 17}, {'boll_alert_text': {'未知': True}}):
            unknown = dict(row, **extra)
            self.assertFalse(monitor._sanitize_snapshot_fields(unknown)['foldable'])
        both = dict(row, level='red', signal_marks=[('amp', 'gray'), ('amp', 'red')])
        self.assertEqual(monitor._sanitize_snapshot_fields(both)['signal_marks'], [('amp', 'red')])
        self.assertEqual(monitor._sanitize_snapshot_fields(both)['signals'], ['振幅 2.0%'])

    def test_reverse_rsi_suffix_in_main_and_multi_filters_without_mutation(self):
        import copy
        from monitor_render import _reverse_rsi_description
        analyzer = TestBollRsiRules()
        analyzer.setUp()
        self.addCleanup(analyzer.doCleanups)
        for side, bands in (('上轨', (100, 101, 90)), ('下轨', (100, 110, 99))):
            for tag, value in (('超买', 70), ('超卖', 35)):
                reverse = (side, tag) in (('上轨', '超卖'), ('下轨', '超买'))
                for count in (1, 2, 3):
                    for group in ('position', 'focus', 'technology'):
                        with self.subTest(side=side, tag=tag, count=count, group=group):
                            row = analyzer.analyze([value] * count + [50] * (3-count), group, bands=bands)
                            before = copy.deepcopy(row)
                            main = plain_text(monitor._signals_html(row))
                            self.assertEqual(main.count('（反）'), int(reverse))
                            self.assertEqual(row, before)
                            snap = monitor.build_snapshot({}, [row], {}, group_counts={})
                            card = monitor._signal_counts_card(snap)
                            data = TestMaMergedStatistics.filters(card) if 'data-filter=' in card else {}
                            if 'boll_rsi' in data:
                                records = data['boll_rsi']['members']
                                self.assertTrue(records)
                                details = [part['t'] for rec in records for part in rec['d']]
                                self.assertEqual(sum(text.count('（反）') for text in details), int(reverse))
                            else:
                                self.assertFalse(monitor.text_only_members([row]))
                                self.assertFalse(any(k == 'boll_rsi' for k, _ in row['signal_marks']))
                            text = f'布林{side} · RSI {count}条 {tag}（≤35）'
                            shown = _reverse_rsi_description(text, side)
                            self.assertEqual(shown.count('（反）'), int(reverse))
                            self.assertEqual(_reverse_rsi_description(shown, side), shown)
        self.assertEqual(_reverse_rsi_description('RSI 3条 ↓超卖', ''), 'RSI 3条 ↓超卖')
        self.assertEqual(_reverse_rsi_description('RSI 1条 ↑超买 · RSI 2条 ↓超卖', '上轨'),
                         'RSI 1条 ↑超买 · RSI 2条 ↓超卖（反）')
        for direction in ('上轨（RSI超卖，方向不一致）', '下轨（RSI超买，方向不一致）'):
            text = monitor._filter_detail({'direction': direction, 'boll_rsi_hits': 3}, 'boll_rsi', '布林+RSI')
            self.assertTrue(text.endswith('（反）'))
            self.assertNotIn('反向', text)

    def test_upper_single_rsi_render_is_unlit_except_positions_stay_white(self):
        import copy
        analyzer = TestBollRsiRules()
        analyzer.setUp()
        self.addCleanup(analyzer.doCleanups)
        for values, direction in (([70, 50, 50], '↑超买'), ([35, 50, 50], '↓超卖')):
            for group in ('position', 'focus', 'technology'):
                with self.subTest(group=group, direction=direction):
                    row = analyzer.analyze(values, group, bands=(100, 101, 90))
                    before = copy.deepcopy(row)
                    output = monitor._signals_html(row)
                    self.assertEqual(re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                                               r'(?: data-signal-level="([^"]*)")?>', output),
                                     [('boll_rsi', 'gray' if group == 'position' else '')])
                    self.assertIn('布林上轨▲', plain_text(output))
                    self.assertIn('RSI 1条 ' + direction, plain_text(output))
                    self.assertNotIn('signal-dot', monitor._stock_row(row, {}))
                    self.assertEqual(monitor._signal_lamps(row), '')
                    self.assertEqual(row['foldable'], not (group == 'position' and direction == '↓超卖'))
                    self.assertEqual(bool(monitor.text_only_members([row])), group == 'position')
                    self.assertEqual(row, before)
        css = monitor.PAGE_CSS
        self.assertIn('.signal-item{color:var(--dim)}', css)
        self.assertIn('.signal-item[data-signal-level="gray"]{color:var(--text)}', css)
        self.assertIn('--text:#e6e8ec; --dim:#8b93a1', css)
        self.assertNotIn('.signal-item[data-signal-level="gray"]{color:var(--dim)}', css)

    def test_same_stock_red_yellow_gray_and_unlit_descriptions_are_independent_and_safe(self):
        import copy
        row = dict(symbol='MIXSAFE', note='备注 <b>不可执行</b>', group='technology',
                   price=100.123, prev_close=101.456, chg=-6, level='red', trigger=None,
                   signals=['仅文字 <img src=x onerror="bad()"> & \'保留\'',
                            'RSI 2条 ↓超卖（≤35）', '量比 2.8x', '异动 -6%'],
                   signal_marks=[('chg', 'red'), ('vol_ratio', 'yellow')])
        before = copy.deepcopy(row)
        output = monitor._signals_html(row)
        self.assertEqual(re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                                   r'(?: data-signal-level="([^"]*)")?>', output),
                         [('chg', 'red'), ('vol_ratio', 'yellow'), ('rsi', 'gray'), ('', '')])
        self.assertEqual(output.count('signal-separator'), 3)
        self.assertIn(monitor.html_lib.escape(row['signals'][0]), output)
        self.assertEqual(monitor.html_lib.unescape(plain_text(output)),
                         '异动 -6% / 量比 2.8x / RSI 2条 ↓超卖 / ' + row['signals'][0])
        stock = monitor._stock_row(row, {})
        self.assertNotIn('signal-dot', stock)
        self.assertNotRegex(stock, r'<img\b|<b\b|<[^>]+\sonerror\s*=')
        self.assertIn('&lt;b&gt;不可执行&lt;/b&gt;', stock)
        self.assertIn('<td class="sig">' + output + '</td>', stock)
        for level, color in (('red', '#ff8580'), ('yellow', '#f0c674'), ('gray', 'var(--text)')):
            self.assertIn('.signal-item[data-signal-level="' + level + '"]{color:' + color + '}',
                          monitor.PAGE_CSS)
        row['level'] = 'yellow'
        self.assertEqual(monitor._signals_html(row), output)
        row['level'] = before['level']
        self.assertEqual(row, before)

    def test_stock_row_matches_head_except_removed_name_dots_and_keeps_stripes(self):
        import ast
        import copy
        import subprocess
        from monitor_render import PageRenderer

        source = subprocess.run(['git', '-C', ROOT, 'show', 'HEAD:monitor_render.py'],
                                check=True, capture_output=True, text=True, encoding='utf-8').stdout
        cls = next(node for node in ast.parse(source).body
                   if isinstance(node, ast.ClassDef) and node.name == 'PageRenderer')
        methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('_stock_row', '_signal_lamps')]
        self.assertEqual({node.name for node in methods}, {'_stock_row', '_signal_lamps'})
        head_row = next(node for node in methods if node.name == '_stock_row')
        head_lamp_calls = sum(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                              and node.func.attr == '_signal_lamps' for node in ast.walk(head_row))
        self.assertIn(head_lamp_calls, (0, 1))
        namespace = {'html_lib': monitor.html_lib}
        exec(compile(ast.Module(body=methods, type_ignores=[]), 'HEAD:monitor_render.py', 'exec'), namespace)
        renderer = PageRenderer(monitor)
        for level in ('red', 'yellow', 'green', 'gray'):
            for foldable in (True, False):
                for target_price in (None, 120, 150, 200, 100):
                    with self.subTest(level=level, foldable=foldable, target=target_price):
                        row = dict(symbol='STRIPE', group='position', hold='long', note='备注 <b>安全</b>',
                                   level=level, price=100.123, prev_close=101.456, chg=-1.31,
                                   unit='单位&', trigger=90, reduce=130, foldable=foldable,
                                   data_date='2026-10-20', signals=['异动 -6%', '量比 2.8x'],
                                   signal_marks=[('chg', 'red'), ('vol_ratio', 'yellow')],
                                   earnings={'status': 'ok', 'date': '2026-10-22', 'timing': 'post'},
                                   range52={'low': 50, 'high': 150, 'position': 50.123, 'scope': '52周',
                                            'from': '2025-10-20', 'to': '2026-10-20'})
                        if target_price is not None:
                            row['target'] = monitor.evaluate_target(row['price'], row['prev_close'],
                                {'kind': 'manual', 'price': target_price, 'failed': False}, 'position', 3)
                        before = copy.deepcopy(row)
                        with patch.object(monitor, '_signal_lamps',
                                          side_effect=lambda d: namespace['_signal_lamps'](renderer, d)):
                            old = namespace['_stock_row'](renderer, row, {}, '2026-10-20')
                        # HEAD 尚未迁移时只剥掉名称前圆点；迁移提交后同一对照仍可运行。
                        self.assertEqual('signal-dot red' in old, bool(head_lamp_calls))
                        expected, removed = re.subn(r'(<td class="sym">)'
                            r'(?:<span class="signal-dot [^"]+"[^>]*></span>)+', r'\1', old)
                        self.assertEqual(removed, head_lamp_calls)
                        with patch.object(monitor, '_signal_lamps', side_effect=AssertionError('主表不能调用旧灯接口')) as lamps:
                            current = monitor._stock_row(row, {}, '2026-10-20')
                        lamps.assert_not_called()
                        self.assertNotIn('signal-dot', current)
                        # 新发布页新增基础灯元数据，布局对照单独剥离并精确验值。
                        self.assertIn('data-base-level="' + level + '"', current)
                        self.assertEqual(re.sub(r' data-base-level="[^"]*"', '', current),
                                         re.sub(r' data-base-level="[^"]*"', '', expected))
                        self.assertIn('<tr class="' + level, current)
                        self.assertIn('class="range52-row"', current)
                        self.assertEqual(row, before)
        for rule in ('tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red);padding-left:14px}',
                     'tr.yellow td:first-child{box-shadow:inset 4px 0 0 var(--yellow);padding-left:12px}',
                     'tr.green td:first-child{box-shadow:inset 2px 0 0 var(--green)}',
                     '.stk tr.red+tr.range52-row>td{--lc:var(--red);--lw:6px}',
                     '.stk tr.yellow+tr.range52-row>td{--lc:var(--yellow);--lw:4px}',
                     '.stk tr.green+tr.range52-row>td{--lc:var(--green);--lw:2px}'):
            self.assertIn(rule, monitor.PAGE_CSS)

    def test_combo_description_has_facts_without_statistics_prefix(self):
        import copy
        for side, state in (('上轨', '触及'), ('下轨', '跌破')):
            for level in ('red', 'yellow', 'gray'):
                with self.subTest(side=side, level=level):
                    arrow = '▲' if side == '上轨' else '▼'
                    boll = f'{state}布林{side}{arrow}'
                    row = {'symbol': 'COMBO', 'group': 'position', 'price': 100,
                           'level': level if level != 'gray' else 'green',
                           'signals': [boll, 'RSI 3条 ↑超买（≥70）', '量比 2.8x'],
                           'signal_marks': [('boll_rsi', level), ('vol_ratio', 'yellow')]
                                           if level != 'gray' else [('vol_ratio', 'yellow')]}
                    original = copy.deepcopy(row)
                    result = monitor._signals_html(row)
                    arrow_class = 'arrow-band-up' if side == '上轨' else 'arrow-band-down'
                    boll_html = f'{state}布林{side}<span class="{arrow_class}">{arrow}</span>'
                    self.assertIn(boll_html + ' · RSI 3条 ', result)
                    self.assertNotIn(boll + '，RSI', plain_text(result))
                    self.assertNotIn('布林+RSI', result)
                    self.assertEqual(result.count('data-signal-kind="boll_rsi"'), 1)
                    self.assertEqual(result.count('signal-separator'), 1)
                    self.assertEqual(row, original)
                    self.assertIn('布林+RSI', dict(monitor.SIGNAL_KINDS).values())

    def test_combo_dot_keeps_boll_tail_comma_and_other_indicator_slash(self):
        import copy
        row = {'symbol': 'DOT', 'group': 'position', 'price': 100, 'level': 'yellow',
               'signals': ['触及布林下轨▼', '连续第2日贴近下轨',
                           'RSI 2条 ↓超卖（≤35）', '量比 2.8x'],
               'signal_marks': [('boll_rsi', 'yellow'), ('vol_ratio', 'yellow')]}
        before = copy.deepcopy(row)
        result = monitor._signals_html(row)
        self.assertIn('触及布林下轨<span class="arrow-band-down">▼</span>，连续第2日贴近下轨 · RSI 2条 ', result)
        self.assertIn('<span class="arrow-rsi-down">↓</span>超卖', result)
        self.assertNotIn('（≤35）', result)
        self.assertEqual(result.count(' · '), 1)
        self.assertEqual(result.count('signal-separator'), 1)
        self.assertEqual(row, before)

    def test_combo_groups_all_facts_under_one_true_lamp(self):
        import copy
        row = {'symbol': 'COMBO', 'group': 'position', 'level': 'red', 'price': 700,
               'signals': ['RSI 3条 ↓超卖（≤35）', '逼近布林下轨▼ 还差0.29%（盘中触及 695.33）',
                           '布林 + RSI → 红', '量比 2.8x', '日内触及250日均线±1%'],
               'signal_marks': [('boll_rsi', 'red'), ('vol_ratio', 'yellow'), ('ma_band', 'yellow')]}
        original = copy.deepcopy(row)
        result = _row_signals(row)
        self.assertNotIn('signal-dot', result)
        self.assertEqual(re.findall(r'data-signal-kind="([^"]*)" data-signal-level="([^"]*)"', result),
                         [('boll_rsi', 'red'), ('vol_ratio', 'yellow'), ('ma', 'yellow')])
        self.assertEqual(result.count('data-signal-kind="boll_rsi"'), 1)
        self.assertNotIn('data-signal-kind="rsi"', result)
        self.assertNotIn('data-signal-kind="boll"', result)
        self.assertIn('RSI 3条 <span class="arrow-rsi-down">↓</span>超卖 · '
                      '逼近布林下轨<span class="arrow-band-down">▼</span> 还差0.29%', result)
        self.assertNotIn('（≤35）', result)
        self.assertNotIn('盘中触及', result)
        self.assertNotIn('布林+RSI', monitor._signals_html(row))
        self.assertEqual(monitor._signal_lamps(row), '')
        self.assertIn('data-signal-kind="boll_rsi" data-signal-level="red"', result)
        self.assertNotIn('→', result)
        self.assertEqual(result.count(' · '), 1)
        self.assertEqual(result.count('signal-separator'), 2)
        self.assertEqual(row, original)

    def test_white_volume_text_gets_its_own_yellow_not_other_red(self):
        row = {'symbol': 'SPCX', 'group': 'focus', 'level': 'red', 'price': 10,
               'signals': ['异动 -12.1%', '量比 2.8x'],
               'signal_marks': [('chg', 'red'), ('vol_ratio', 'yellow')]}
        groups = monitor._signal_description_groups(row)
        self.assertEqual([(g['kind'], g['level']) for g in groups], [('chg', 'red'), ('vol_ratio', 'yellow')])
        self.assertIn('data-signal-kind="vol_ratio" data-signal-level="yellow"', _row_signals(row))
        self.assertNotIn('signal-dot', _row_signals(row))
        row['signal_marks'] = [('chg', 'red')]
        self.assertIn('<span class="signal-item" data-signal-kind="vol_ratio">量比 2.8x</span>', _row_signals(row))
        self.assertNotIn('data-signal-level="yellow"', _row_signals(row))

    def test_gray_is_only_true_technical_gray_and_context_has_no_lamp(self):
        row = {'symbol': 'GRAYTEST', 'group': 'position', 'level': 'green', 'price': 10,
               'signals': ['RSI 2条 ↓超卖（≤35）'], 'signal_marks': []}
        self.assertIn('data-signal-kind="rsi" data-signal-level="gray"', _row_signals(row))
        row['level'] = 'red'
        row['signal_marks'] = [('chg', 'red')]
        self.assertIn('data-signal-kind="rsi" data-signal-level="gray"', _row_signals(row))
        self.assertNotIn('data-signal-level="red"', _row_signals(row))
        self.assertNotIn('signal-dot', _row_signals(row))

    def test_description_sort_is_stable_red_yellow_gray_unlit_then_manual_target(self):
        import copy
        row = {'symbol': 'ORDER', 'level': 'red', 'group': 'technology', 'price': 100,
               'target': {'kind': 'manual'}, 'ma_settings': {'ma_short': 50, 'ma_long': 200},
               'signals': ['手填目标价说明 <b>尾部</b>', '无灯说明 <img onerror=x>',
                           'RSI 2条 ↓超卖（≤35）', '日内触及200日均线±1%', '振幅 6%',
                           '量比 2.8x', '异动 -12.1%', '距加仓价 90 还差 1%',
                           '另条无灯说明 & 未触发', '日内触及200日均线±1%'],
               'signal_marks': [('target', 'purple'), ('ma_band', 'yellow'), ('amp', 'yellow'),
                                ('vol_ratio', 'yellow'), ('chg', 'red'), ('add_price', 'red')]}
        before = copy.deepcopy(row)
        groups = monitor._signal_description_groups(row)
        self.assertEqual([(g['kind'], g['level']) for g in groups],
                         [('chg', 'red'), ('add_price', 'red'), ('ma', 'yellow'),
                          ('amp', 'yellow'), ('vol_ratio', 'yellow'), ('rsi', 'gray'),
                          ('', None), ('', None), ('target', 'purple')])
        output = _row_signals(row)
        self.assertEqual(re.findall(r'data-signal-kind="([^"]*)"', output),
                         ['chg', 'amp', 'add_price', 'ma', 'vol_ratio', 'rsi', '', ''])
        self.assertNotIn('signal-dot', output)
        self.assertEqual(re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                                   r'(?: data-signal-level="([^"]*)")?(?: data-signal-role="[^"]*")?>', output),
                         [('chg', 'red'), ('amp', 'yellow'), ('add_price', 'red'), ('ma', 'yellow'),
                          ('vol_ratio', 'yellow'), ('rsi', 'gray'),
                          ('', ''), ('', '')])
        self.assertIn('data-signal-kind="amp" data-signal-level="yellow" data-signal-role="supplement"', output)
        self.assertEqual(monitor._signal_description_groups(row), groups)
        self.assertEqual(output.count('触及长均线±1%'), 1)
        self.assertNotIn('手填目标价', output)
        self.assertIn('&lt;img onerror=x&gt;', output)
        self.assertIn('&amp; 未触发', output)
        self.assertNotIn('<img', output)
        self.assertNotIn('<b>', output)
        self.assertEqual(_row_signals(row), output)
        self.assertEqual(row, before)
        row['signal_marks'] = list(reversed(row['signal_marks']))
        self.assertEqual(_row_signals(row), output)
        for marked in (False, True):
            manual = {'signals': ['手填目标价说明', '普通说明'], 'target': {'kind': 'manual'},
                      'signal_marks': [('target', 'purple')] if marked else []}
            self.assertEqual([g['kind'] for g in monitor._signal_description_groups(manual)], ['', 'target'])
        low = {'signals': ['手填目标价说明', '普通说明'], 'signal_marks': [],
               'target': monitor.evaluate_target(100, 100, {'kind': 'manual', 'price': 120}, 'technology', 3)}
        output = _row_signals(low)
        self.assertIn('普通说明', output)
        self.assertNotIn('手填目标价说明', output)
        self.assertNotIn('tnote', output)

    def test_html_escape_and_multiple_ma_facts_preserved(self):
        row = {'symbol': 'SAFE', 'level': 'yellow', 'group': 'position', 'price': 10,
               'signals': ['上穿250日均线', '日内触及250日均线±1%', '<img onerror=x> · 说明'],
               'ma_settings': {'ma_short': 50, 'ma_long': 250},
               'signal_marks': [('ma_cross', 'yellow'), ('ma_band', 'yellow')]}
        result = _row_signals(row)
        self.assertNotIn('signal-dot', result)
        self.assertEqual(re.findall(r'<span class="signal-item" data-signal-kind="([^"]*)"'
                                   r'(?: data-signal-level="([^"]*)")?>', result),
                         [('ma', 'yellow'), ('', '')])
        self.assertIn('上穿长均线，触及长均线±1%', result)
        self.assertIn('&lt;img onerror=x&gt;，说明', result)
        self.assertNotIn('<img', result)


class TestMainOnlyDescriptionProjection(unittest.TestCase):
    """主表精简独立于原始事实、统计及技术判灯。"""

    def assert_main_only(self, row, expected):
        import copy
        before = copy.deepcopy(row)
        groups = monitor._signal_description_groups(row)
        counts = monitor.signal_counts([row])
        members = monitor.signal_count_members([row])
        gray = monitor.text_only_members([row])
        snapshot = monitor.build_snapshot({}, [row], {}, group_counts={})
        snapshot_before = copy.deepcopy(snapshot)
        card = monitor._signal_counts_card(snapshot)
        filters = TestMaMergedStatistics.filters(card) if 'data-filter=' in card else {}
        output = monitor._signals_html(row)
        self.assertEqual(monitor.html_lib.unescape(plain_text(output)), expected)
        self.assertEqual(monitor._signals_html(row), output)
        self.assertEqual(monitor._signal_description_groups(row), groups)
        self.assertEqual(monitor.signal_counts([row]), counts)
        self.assertEqual(monitor.signal_count_members([row]), members)
        self.assertEqual(monitor.text_only_members([row]), gray)
        from monitor_render import PageRenderer
        with patch.object(PageRenderer, '_main_description_text',
                          side_effect=AssertionError('统计不能调用主表投影')):
            repeated_card = monitor._signal_counts_card(snapshot)
        self.assertEqual(repeated_card, card)
        self.assertEqual(TestMaMergedStatistics.filters(repeated_card) if filters else {}, filters)
        self.assertEqual(snapshot, snapshot_before)
        self.assertEqual(row, before)
        return output

    def test_price_reminders_have_one_primary_without_mutating_facts(self):
        for chg_level, amp_level, primary in (
                ('red', 'red', 'chg'), ('red', 'yellow', 'chg'),
                ('yellow', 'red', 'amp'), ('yellow', 'yellow', 'chg')):
            for reverse in (False, True):
                with self.subTest(chg=chg_level, amp=amp_level, reverse=reverse):
                    texts = {'chg': '异动 -9.0%' if chg_level == 'red' else '波动 +6.0%',
                             'amp': '振幅 12.0%（96.00~108.00）' if amp_level == 'red' else '振幅 7.0%（99.00~106.00）'}
                    order = ['amp', 'chg'] if reverse else ['chg', 'amp']
                    row = {'symbol': 'PRICEPAIR', 'group': 'focus', 'price': 100, 'chg': -9, 'amp': 12,
                           'level': 'red' if 'red' in (chg_level, amp_level) else 'yellow',
                           'signals': [texts[k] for k in order],
                           'signal_marks': [('chg', chg_level), ('amp', amp_level)]}
                    secondary = 'amp' if primary == 'chg' else 'chg'
                    output = self.assert_main_only(row, texts[primary] + ' ， ' + texts[secondary])
                    for kind, role in ((primary, 'primary'), (secondary, 'supplement')):
                        level = chg_level if kind == 'chg' else amp_level
                        self.assertIn(f'data-signal-kind="{kind}" data-signal-level="{level}" data-signal-role="{role}"', output)
                    self.assertEqual(output.count('data-signal-role="supplement"'), 1)
                    self.assertNotIn('data-signal-level="gray"', output)
        self.assertNotRegex(monitor.PAGE_CSS, r'\.signal-item\[data-signal-role=[^\]]+\]\{[^}]*color\s*:')
        for level, color in (('red', '#ff8580'), ('yellow', '#f0c674'), ('gray', 'var(--text)')):
            self.assertIn(f'.signal-item[data-signal-level="{level}"]{{color:{color}}}', monitor.PAGE_CSS)
        self.assertIn('.signal-item{color:var(--dim)}', monitor.PAGE_CSS)

    def test_price_single_legacy_unlit_and_unknown_levels_do_not_guess_primary(self):
        for chg_level, amp_level in ((None, 'red'), ('yellow', None), ('red', 'gray'),
                                      ('green', 'yellow'), ('red', 'invalid'), (None, None)):
            with self.subTest(chg=chg_level, amp=amp_level):
                row = {'symbol': 'LEGACYPRICE', 'group': 'focus', 'price': 100, 'level': 'red',
                       'signals': ['异动 +9.0%', '振幅 7.0%（99.00~106.00）'],
                       'signal_marks': [(kind, lv) for kind, lv in (('chg', chg_level), ('amp', amp_level)) if lv]}
                expected = ' / '.join('，'.join(g['texts']) for g in monitor._signal_description_groups(row))
                output = self.assert_main_only(row, expected)
                self.assertNotIn('data-signal-role', output)
        for text, kind in (('异动 +9.0%', 'chg'), ('振幅 12.0%', 'amp')):
            row = {'symbol': 'SINGLEPRICE', 'group': 'focus', 'signals': [text],
                   'level': 'red', 'signal_marks': [(kind, 'red')]}
            self.assertNotIn('data-signal-role', self.assert_main_only(row, text))

    def test_helper_is_not_a_monitor_export(self):
        from monitor_render import PageRenderer
        self.assertIn('_main_description_text', PageRenderer.__dict__)
        self.assertFalse(hasattr(monitor, '_main_description_text'))
        self.assertEqual(monitor._signals_html({'signals': [], 'signal_marks': []}), '—')

    def test_default_and_custom_rsi_thresholds_only_removed_from_main(self):
        for threshold, arrow, tag, wrapper in (
                ('≤35', '↓', '超卖', 'arrow-rsi-down'),
                ('≥70', '↑', '超买', 'arrow-rsi-up'),
                ('≤28.5', '↓', '超卖', 'arrow-rsi-down'),
                ('≥81.25', '↑', '超买', 'arrow-rsi-up')):
            for kind in ('rsi', 'boll_rsi'):
                for level in ('red', 'yellow', 'gray'):
                    with self.subTest(threshold=threshold, kind=kind, level=level):
                        rsi = f'RSI 3条 {arrow}{tag}（{threshold}）：RSI6=22.50'
                        boll = ('触及布林下轨▼（盘中触及 998.25）' if tag == '超卖'
                                else '触及布林上轨▲（盘中触及 1,002.75）')
                        row = {'symbol': 'PROJRSI', 'group': 'position', 'price': 1000,
                               'level': 'red', 'signals': [boll, rsi] if kind == 'boll_rsi' else [rsi],
                               'signal_marks': [(kind, level)]}
                        body = f'RSI 3条 {arrow}{tag}：RSI6=22.50'
                        if kind == 'boll_rsi':
                            body = ('触及布林下轨▼' if tag == '超卖' else '触及布林上轨▲') + ' · ' + body
                        output = self.assert_main_only(row, body)
                        self.assertIn(f'data-signal-kind="{kind}" data-signal-level="{level}"', output)
                        self.assertIn(f'<span class="{wrapper}">{arrow}</span>{tag}：RSI6=22.50', output)
                        self.assertNotIn('（' + threshold + '）', output)
                        self.assertIn(rsi, monitor._signal_description_groups(row)[0]['texts'])
                        self.assertIn(rsi, row['signals'])
                        if kind == 'boll_rsi':
                            self.assertIn(boll, monitor._signal_description_groups(row)[0]['texts'])

    def test_custom_thresholds_from_analysis_remain_detailed_in_raw_groups(self):
        analyzer = TestBollRsiRules()
        analyzer.setUp()
        self.addCleanup(analyzer.doCleanups)
        for value, raw, expected in ((28.5, 'RSI 3条 ↓超卖（≤28.5）', 'RSI 3条 ↓超卖'),
                                     (81.25, 'RSI 3条 ↑超买（≥81.25）', 'RSI 3条 ↑超买')):
            with self.subTest(value=value), patch.dict(monitor.S, {'rsi_low': 28.5, 'rsi_high': 81.25}):
                row = analyzer.analyze([value] * 3, bands=(None, None, None))
            self.assertEqual(row['signals'], [raw])
            self.assert_main_only(row, expected)
            self.assertEqual(monitor._signal_description_groups(row)[0]['texts'], [raw])
            self.assertEqual(monitor.html_lib.unescape(plain_text(monitor._signal_text_html(raw))), raw)

    def test_lower_break_below_above_and_equal_are_separate_main_states(self):
        tail = '连续第≥120日贴近下轨（其中3日穿越）'
        for price, state in ((999.999999999, '跌破布林下轨▼，下轨外'),
                             (1000.000000001, '盘中跌破布林下轨▼后收回'),
                             (1000, '盘中跌破布林下轨▼后收回')):
            for kind in ('boll', 'boll_rsi'):
                with self.subTest(price=price, kind=kind):
                    raw = '跌破布林下轨▼（盘中触及 987.65）'
                    row = {'symbol': 'PROJBOLL', 'group': 'position', 'price': price, 'boll_dn': 1000,
                           'level': 'red', 'signals': [raw, tail], 'signal_marks': [(kind, 'red')]}
                    expected = state + '，' + tail
                    if kind == 'boll_rsi':
                        row['signals'].append('RSI 3条 ↓超卖（≤28.5）')
                        expected += ' · RSI 3条 ↓超卖'
                    output = self.assert_main_only(row, expected)
                    self.assertIn('data-signal-level="red"', output)
                    self.assertEqual(output.count('<span class="arrow-band-down">▼</span>'), 1)
                    self.assertIn(raw + '，' + tail, monitor._signal_description_groups(row)[0]['texts'])

    def test_lower_break_does_not_guess_from_missing_or_invalid_prices(self):
        for key in ('price', 'boll_dn'):
            for value in (None, 0, -1, float('nan'), float('inf'), True, '1000'):
                for kind in ('boll', 'boll_rsi'):
                    with self.subTest(key=key, value=value, kind=kind):
                        row = {'symbol': 'PROJBAD', 'group': 'position', 'price': 1001, 'boll_dn': 1000,
                               'level': 'yellow', 'signals': ['跌破布林下轨▼（盘中触及 987.65）'],
                               'signal_marks': [(kind, 'yellow')]}
                        row[key] = value
                        expected = '跌破布林下轨▼'
                        if kind == 'boll_rsi':
                            row['signals'].append('RSI 3条 ↓超卖（≤35）')
                            expected += ' · RSI 3条 ↓超卖'
                        self.assert_main_only(row, expected)
            row = {'symbol': 'PROJABSENT', 'group': 'position', 'level': 'yellow',
                   'signals': ['跌破布林下轨▼（盘中触及 987.65）'], 'signal_marks': [('boll', 'yellow')]}
            row['boll_dn' if key == 'price' else 'price'] = 1000
            self.assert_main_only(row, '跌破布林下轨▼')

    def test_upper_break_outside_recovered_equal_and_invalid_prices(self):
        tail = '连续第4日贴近上轨（其中2日穿越）'
        for price, state in ((1000.000000001, '突破布林上轨▲，上轨外'),
                             (999.999999999, '盘中突破布林上轨▲后收回'),
                             (1000, '盘中突破布林上轨▲后收回')):
            for kind in ('boll', 'boll_rsi'):
                for prefix in ('', '盘中'):
                    with self.subTest(price=price, kind=kind, prefix=prefix):
                        raw = prefix + '突破布林上轨▲（盘中触及 1,023.45）'
                        row = {'symbol': 'PROJUP', 'group': 'position', 'price': price, 'boll_up': 1000,
                               'level': 'yellow', 'signals': [raw, tail], 'signal_marks': [(kind, 'yellow')]}
                        expected = state + '，' + tail
                        if kind == 'boll_rsi':
                            row['signals'].append('RSI 2条 ↑超买（≥70）')
                            expected += ' · RSI 2条 ↑超买'
                        output = self.assert_main_only(row, expected)
                        self.assertEqual(output.count('<span class="arrow-band-up">▲</span>'), 1)
        for key in ('price', 'boll_up'):
            for value in (None, 0, -1, float('nan'), float('inf'), True, '1000'):
                for prefix in ('', '盘中'):
                    with self.subTest(key=key, value=value, prefix=prefix):
                        raw = prefix + '突破布林上轨▲（盘中触及 1,023.45）'
                        row = {'symbol': 'PROJUPBAD', 'group': 'position', 'price': 1001, 'boll_up': 1000,
                               'level': 'yellow', 'signals': [raw], 'signal_marks': [('boll', 'yellow')]}
                        row[key] = value
                        self.assert_main_only(row, prefix + '突破布林上轨▲')

    def test_upper_and_nonbreak_lower_keep_state_gap_and_continuous_days(self):
        for raw, expected in (
                ('盘中突破布林上轨▲（盘中触及 1,023.45）', '盘中突破布林上轨▲'),
                ('突破布林上轨▲（盘中触及 1,023.45）', '突破布林上轨▲'),
                ('触及布林上轨▲（盘中触及 1,023.45）', '触及布林上轨▲'),
                ('逼近布林上轨▲ 还差0.15%（盘中触及 1,023.45）', '逼近布林上轨▲ 还差0.15%'),
                ('触及布林下轨▼（盘中触及 987.65）', '触及布林下轨▼'),
                ('逼近布林下轨▼ 还差0.15%（盘中触及 987.65）', '逼近布林下轨▼ 还差0.15%')):
            with self.subTest(raw=raw):
                side = '上轨' if '上轨' in raw else '下轨'
                tail = f'连续第4日贴近{side}（其中2日穿越）'
                row = {'symbol': 'PROJUP', 'group': 'position', 'level': 'yellow', 'price': 1,
                       'boll_dn': 1000, 'signals': [raw, tail], 'signal_marks': [('boll', 'yellow')]}
                self.assert_main_only(row, expected + '，' + tail)
        context = {'symbol': 'PROJCTX', 'group': 'position', 'price': 100, 'level': 'green',
                   'signals': ['说明（≤28.5）（盘中触及 99.75）'], 'signal_marks': []}
        self.assert_main_only(context, context['signals'][0])

    def test_ma_uses_real_values_and_row_settings_not_current_price_or_rounded_gap(self):
        row = {'symbol': 'PROJMA', 'group': 'position', 'price': 2222.22, 'level': 'yellow',
               'signals': ['日内触及60日均线 ±1%区间（日内 1,230.00~2,223.00，现价偏离 +0.1%）',
                           '日内触及120日均线 ±1%区间（日内 1,230.00~2,223.00，现价偏离 +0.1%）'],
               'signal_marks': [('ma_band', 'yellow')], 'ma_settings': {'ma_short': 60, 'ma_long': 120},
               'ma_values': {'60': 1234.567, '120': 1567.894}}
        with patch.dict(monitor.S, {'ma_short': 120, 'ma_long': 60}):
            self.assert_main_only(row, '日内触及短均线 ±1%区间（均线价：1,234.57），'
                                       '触及长均线 ±1%区间（均线价：1,567.89）')
        self.assertIn('日内 1,230.00~2,223.00，现价偏离 +0.1%',
                      monitor._signal_description_groups(row)[0]['texts'][0])
        row['price'] = 9999.99
        self.assert_main_only(row, '日内触及短均线 ±1%区间（均线价：1,234.57），'
                                   '触及长均线 ±1%区间（均线价：1,567.89）')
        cross = dict(row, signals=['上穿60日均线', '现价贴近120日均线 +0.1%（日内高低缺失，仅按现价）'])
        self.assert_main_only(cross, '上穿短均线，现价贴近长均线 +0.1%（日内高低缺失，仅按现价）')

    def test_ma_missing_zero_nan_and_wrong_key_never_reverse_calculated(self):
        for values in (None, {}, {'120': None}, {'120': 0}, {'120': -1}, {'120': float('nan')},
                       {'120': float('inf')}, {'120': True}, {'120': '1234.57'}, {120: 1234.57}):
            with self.subTest(values=values):
                row = {'symbol': 'PROJMANONE', 'group': 'position', 'price': 1235.8, 'level': 'yellow',
                       'signals': ['日内触及120日均线 ±1%区间（日内 1,234.00~1,240.00，现价偏离 +0.1%）'],
                       'signal_marks': [('ma_band', 'yellow')], 'ma_settings': {'ma_short': 60, 'ma_long': 120},
                       'ma_values': values}
                self.assert_main_only(row, '触及长均线 ±1%区间（均线价：—）')
        del row['ma_values']
        self.assert_main_only(row, '触及长均线 ±1%区间（均线价：—）')

    def test_analysis_ma_values_are_this_round_averages_and_zero_settings_disable_them(self):
        closes = [1234.567] * 129 + [1290.123]
        data = {'closes': closes, 'highs': [1300.] * 130, 'lows': [1230.] * 130,
                'volumes': [1000] * 130, 'price': closes[-1], 'prev_close': closes[-2],
                'dates': ['2026-10-26'] * 130, 'source': 'test', 'realtime': False}
        for short, long in ((60, 120), (120, 60), (0, 120), (60, 0), (0, 0)):
            with self.subTest(short=short, long=long), \
                    patch.object(monitor, 'TARGET_DATE', ''), \
                    patch.dict(monitor.S, {'ma_short': short, 'ma_long': long, 'near_52w_low_pct': 0}), \
                    patch.object(monitor, 'calc_rsi', return_value=50), \
                    patch.object(monitor, 'boll', return_value=(None, None, None)):
                _, signals, row = monitor.analyze_symbol('PROJMACALC', {}, data, 'position')
            expected = {str(period): sum(closes[-period:]) / period for period in (short, long) if period > 0}
            self.assertEqual(row['ma_settings'], {'ma_short': short, 'ma_long': long})
            self.assertEqual(set(row['ma_values']), set(expected))
            for key, value in expected.items():
                self.assertAlmostEqual(row['ma_values'][key], value)
            output = monitor._signals_html(row)
            self.assertEqual(row['signals'], signals)
            if long:
                self.assertTrue(any(text.startswith(f'日内触及{long}日均线') for text in signals), signals)
                self.assertIn(f'触及长均线 ±1%区间（均线价：{expected[str(long)]:,.2f}）', plain_text(output))
                self.assertNotIn('现价偏离', output)
            else:
                self.assertNotIn('均线价：', output)

    def test_low52_main_and_statistics_share_real_value_without_changing_raw_fact(self):
        for low in (12.3456, 1234.567):
            with self.subTest(low=low):
                raw = '触及52周新低（距低点 +2.6%）'
                row = {'symbol': 'PROJLOW', 'group': 'position', 'level': 'red', 'price': 9999.99,
                       'dist_low': 2.6, 'low52': low, 'signals': [raw], 'signal_marks': [('low52', 'red')]}
                self.assert_main_only(row, raw + f'（低点价：{low:,.2f}）')
                member, = monitor.signal_count_members([row])['low52']['red']
                self.assertEqual(member['low52_price'], f'{low:,.2f}')
                card = monitor._signal_counts_card(monitor.build_snapshot({}, [row], {}, group_counts={}))
                self.assertIn(f'52周低 {low:,.2f}', card)
                self.assertEqual(monitor._signal_description_groups(row)[0]['texts'], [raw])
        _, _, actual = Test52wLow.run_sym([100.] * 150 + [80.] * 20 + [90.] * 29 + [82.])
        self.assertAlmostEqual(actual['low52'], 79.92)
        self.assertIn('（低点价：79.92）', monitor._signals_html(actual))
        self.assertEqual(monitor.signal_count_members([actual])['low52']['red'][0]['low52_price'], '79.92')

    def test_low52_missing_or_invalid_does_not_use_current_price_or_distance(self):
        for value in (None, 0, -1, float('nan'), float('inf'), True, '12.35'):
            with self.subTest(value=value):
                row = {'symbol': 'PROJLOWNONE', 'group': 'position', 'level': 'red', 'price': 1234.57,
                       'dist_low': 2.6, 'low52': value, 'signals': ['触及52周新低（距低点 +2.6%）'],
                       'signal_marks': [('low52', 'red')]}
                self.assert_main_only(row, row['signals'][0])
                self.assertNotIn('low52_price', monitor.signal_count_members([row])['low52']['red'][0])
        del row['low52']
        self.assert_main_only(row, row['signals'][0])
        row['low52'] = 12.3456
        row['signals'] = ['仍在52周低位（距低点 +2.6%）']
        self.assert_main_only(row, row['signals'][0])
        row['signals'] = ['触及52周新低（低点价：12.35）']
        self.assert_main_only(row, row['signals'][0])


if __name__ == '__main__':
    unittest.main()
