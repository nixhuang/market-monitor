"""正式决议解析、缓存和原油回归；样本仅用于离线测试，不运行主监测器。"""
import ast
import copy
import inspect
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import policy_alerts as p

NOW = datetime(2025, 7, 1, 12, tzinfo=p.BJ)
FED = {"date": "2025-06-18T14:00:00-04:00", "published_date": "2025-06-18",
       "released_at": "2025-06-18T14:00:00-04:00", "action": "maintain", "low": 4.25, "high": 4.5,
       "next_release": "2025-07-30T14:00:00-04:00", "source": "美联储官方正式利率决议声明",
       "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20250618a.htm",
       "calendar_url": p.FED_URL, "fetch_error": "", "pending_update": False}


def row(month, day, date=None, link=True, extra="", quote='"'):
    href = f'<a href={quote}/newsevents/pressreleases/monetary{date}a.htm{quote}>HTML</a>' if date and link else ""
    return (f'<div class={quote}row fomc-meeting{quote}>'
            f'<div data-test={quote}x{quote} class={quote}fomc-meeting__month col-xs-5{quote}><strong>{month}</strong></div>'
            f'<div class={quote}col-xs-4 fomc-meeting__date{quote}><span>{day}</span>{extra}</div>{href}</div>')


def calendar(july_link=False):
    return ('<h4><a>2025 FOMC Meetings</a></h4>' + row('June', '17–18*', '20250618')
            + row('July', '29-30', '20250730', july_link) + row('September', '16-17*'))


def statement(action="maintain", adjustment="", low="4-1/4", high="4-1/2",
              date="June 18, 2025", release="2:00 p.m. EDT", suffix=""):
    prep = "at" if action in ("maintain", "keep") else "to"
    return (f'<div><p class="article__time">{date}</p><p class="releaseTime">For release at {release}</p>'
            f'<p>The Committee decided to {action} the target range for the federal funds rate '
            f'{adjustment} {prep} {low} to {high} percent. {suffix}</p></div>')


class TestStatements(unittest.TestCase):
    def test_actions_and_verified_basis_points(self):
        for action, adjustment, bps in (("raise", "by 1/4 percentage point", 25),
                                        ("lower", "by 1/2 percentage point", 50),
                                        ("raise", "by 0.25 percentage point", 25),
                                        ("lower", "by 25 basis points", 25),
                                        ("lower", "by one-quarter percentage point", 25),
                                        ("raise", "by ¼ percentage point", 25)):
            with self.subTest(action=action, adjustment=adjustment):
                data = p.parse_statement(statement(action, adjustment), '2025-06-18')
                self.assertEqual((data['action'], data['low'], data['high'], data['change_bps']),
                                 (action, 4.25, 4.5, bps))
        for action in ('maintain', 'keep'):
            data = p.parse_statement(statement(action), '2025-06-18')
            self.assertEqual(data['action'], action)
            self.assertNotIn('change_bps', data)

    def test_unknown_adjustment_not_guessed(self):
        for adjustment in ('', 'by a quarter percentage point', 'by a small amount'):
            data = p.parse_statement(statement('lower', adjustment), '2025-06-18')
            self.assertEqual(data['action'], 'lower')
            self.assertNotIn('change_bps', data)
            rendered = p.fed_decision({**FED, **data}, NOW)
            self.assertIn('降息', rendered['text'])
            self.assertNotIn('基点', rendered['text'])

    def test_fraction_decimal_zero_and_inline_tags(self):
        for low, high, expected in (('3-3/4', '4', (3.75, 4)), ('3.75', '4.00', (3.75, 4)),
                                    ('0', '1/4', (0, .25)), ('4 1/4', '4 1/2', (4.25, 4.5))):
            text = statement('raise', 'by 1/4 percentage point', low, high)
            text = text.replace(' to ', ' <strong> </strong>to ')
            data = p.parse_statement(text, '2025-06-18')
            self.assertEqual((data['low'], data['high']), expected)

    def test_dates_required_and_match_official_calendar(self):
        for text in (statement(date='June 17, 2025'), statement().replace('article__time', 'other'),
                     statement(date='N/A'), statement() + '<p class="article__time">June 19, 2025</p>'):
            with self.assertRaises(ValueError):
                p.parse_statement(text, '2025-06-18')
        with self.assertRaises(ValueError):
            p.parse_statement(statement(), 'bad')

    def test_sentence_boundaries_and_dissent(self):
        dissent = '<p>Voting against this action was a member who preferred to lower the target range for the federal funds rate to 3 to 3-1/4 percent.</p>'
        text = statement('maintain') + dissent
        data = p.parse_statement(text, '2025-06-18')
        self.assertEqual((data['action'], data['low'], data['high']), ('maintain', 4.25, 4.5))
        for gap in ('. ', '. the dissenters ', '; ', '! '):
            text = statement().replace('maintain the target range', 'maintain' + gap + 'the target range')
            with self.assertRaises(ValueError):
                p.parse_statement(text, '2025-06-18')
        with self.assertRaises(ValueError):
            p.parse_statement(statement().replace('The Committee decided', 'A dissenter preferred and The Committee decided'), '2025-06-18')

    def test_conflicting_or_missing_decisions(self):
        for text in (statement() + statement('lower', 'by 1/4 percentage point'),
                     statement().replace('The Committee decided to maintain', 'A member wanted to lower'),
                     statement().replace('at 4-1/4', 'to 4-1/4'),
                     statement('raise', 'by 0 percentage point'),
                     statement('raise', 'by 1/0 percentage point')):
            with self.assertRaises(ValueError):
                p.parse_statement(text, '2025-06-18')

    def test_invalid_target_ranges(self):
        for low, high in (('5', '4'), ('4', '4'), ('0', '30'), ('2', '4'), ('NaN', '4'), ('3-1/0', '4')):
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                p.parse_statement(statement(low=low, high=high), '2025-06-18')

    def test_publication_time_and_dst(self):
        self.assertEqual(p.parse_statement(statement(), '2025-06-18')['released_at'], '2025-06-18T14:00:00-04:00')
        data = p.parse_statement(statement(date='January 29, 2025', release='2:00 p.m. EST'), '2025-01-29')
        self.assertEqual(data['released_at'], '2025-01-29T14:00:00-05:00')
        data = p.parse_statement(statement(date='March 15, 2020', release='5:00 p.m. EDT'), '2020-03-15')
        self.assertEqual(data['released_at'], '2020-03-15T17:00:00-04:00')
        with self.assertRaises(ValueError):
            p.parse_statement(statement(release='2:00 p.m. EST'), '2025-06-18')


class TestCalendar(unittest.TestCase):
    def test_regular_calendar_and_no_prediction_fields(self):
        result = p.parse_calendar(calendar(), NOW)
        self.assertEqual(result['latest']['date'], '2025-06-18')
        self.assertEqual(result['latest_statement']['statement'], FED['url'])
        self.assertEqual(result['next']['at'], '2025-07-30T14:00:00-04:00')
        self.assertEqual(set(result), {'latest', 'latest_statement', 'next'})
        self.assertNotIn('sep', result['next'])

    def test_quotes_tags_whitespace_duplicates_and_invalid_dates(self):
        text = ('<h4>2025 <span>FOMC</span> Meetings</h4>'
                + row('June', '17—18*', '20250618', quote="'")
                + row('July', '32-33', '20250733')
                + row('July', '29 - 30', '20250730', link=False)
                + '<h4>2025 FOMC Meetings</h4>' + row('June', '17-18'))
        data = p.parse_calendar(text, NOW)
        self.assertEqual(data['latest_statement']['statement'], FED['url'])
        self.assertEqual(data['next']['date'], '2025-07-30')

    def test_cross_month_cross_year_and_dst(self):
        text = ('2024 FOMC Meetings' + row('December', '17-18', '20241218')
                + '2025 FOMC Meetings' + row('January', '28-29') + row('April/May', '30-1'))
        data = p.parse_calendar(text, datetime(2025, 1, 1, tzinfo=p.BJ))
        self.assertEqual(data['latest_statement']['date'], '2024-12-18')
        self.assertEqual(data['next']['at'], '2025-01-29T14:00:00-05:00')
        data = p.parse_calendar(text, datetime(2025, 3, 1, tzinfo=p.BJ))
        self.assertEqual(data['next']['at'], '2025-05-01T14:00:00-04:00')

    def test_unscheduled_requires_real_link(self):
        text = ('2020 FOMC Meetings' + row('January', '28-29', '20200129')
                + row('March', '3', '20200303', extra=' (unscheduled)')
                + row('March', '15', '20200315', extra=' (unscheduled)')
                + row('April', '28-29'))
        now = datetime(2020, 3, 20, tzinfo=p.BJ)
        data = p.parse_calendar(text, now)
        self.assertEqual(data['latest_statement']['date'], '2020-03-15')
        self.assertTrue(data['latest_statement']['unscheduled'])
        self.assertEqual(data['next']['date'], '2020-04-29')
        without = text.replace('<a href="/newsevents/pressreleases/monetary20200315a.htm">HTML</a>', '')
        self.assertEqual(p.parse_calendar(without, now)['latest']['date'], '2020-03-03')

    def test_missing_latest_statement_and_no_future(self):
        result = p.parse_calendar(calendar(), datetime(2025, 8, 1, tzinfo=p.BJ))
        self.assertEqual(result['latest']['date'], '2025-07-30')
        self.assertEqual(result['latest_statement']['date'], '2025-06-18')
        result = p.parse_calendar('2020 FOMC Meetings' + row('March', '15', '20200315'), NOW)
        self.assertIsNone(result['next'])
        self.assertEqual(result['latest_statement']['date'], '2020-03-15')
        with self.assertRaises(ValueError):
            p.parse_calendar('not a calendar', NOW)

    def test_wrong_date_external_and_implementation_links_ignored(self):
        base = '2025 FOMC Meetings' + row('June', '17-18', '20250617') + row('July', '29-30')
        for href in ('/newsevents/pressreleases/monetary20250618a1.htm',
                     'https://example.test/newsevents/pressreleases/monetary20250618a.htm',
                     '//www.federalreserve.gov.evil.test/newsevents/pressreleases/monetary20250618a.htm'):
            text = base.replace('/newsevents/pressreleases/monetary20250617a.htm', href)
            self.assertIsNone(p.parse_calendar(text, NOW)['latest_statement'])
        absolute = base.replace('/newsevents/pressreleases/monetary20250617a.htm', FED['url'])
        self.assertEqual(p.parse_calendar(absolute, NOW)['latest_statement']['statement'], FED['url'])


class TestDecisionAndCache(unittest.TestCase):
    def test_neutral_render_for_all_actions(self):
        before = copy.deepcopy(FED)
        for action, label in (('raise', '加息'), ('lower', '降息'), ('maintain', '维持'), ('keep', '维持')):
            rendered = p.fed_decision({**FED, 'action': action, 'change_bps': 25}, NOW)
            self.assertEqual((rendered['level'], rendered['status']), ('neutral', 'available'))
            self.assertIn(label, rendered['text'])
            self.assertIn('2025-06-18', rendered['text'])
            self.assertIn('4.25%–4.5%', rendered['text'])
            self.assertEqual('基点' in rendered['text'], action in ('raise', 'lower'))
            for phrase in ('买入', '卖出', '利好', '利空', '安全', '压力'):
                self.assertNotIn(phrase, rendered['text'])
        self.assertEqual(FED, before)

    def test_old_decision_never_expires_by_age_or_year(self):
        data = {**FED, 'next_release': None}
        for now in (NOW + timedelta(days=100), datetime(2027, 1, 1, tzinfo=p.BJ)):
            rendered = p.fed_decision(data, now)
            self.assertEqual(rendered['status'], 'available')
            self.assertEqual(rendered['date'], FED['date'])
            with patch.object(p, 'fetch_text') as fetch:
                result = p.update_policy({'fed': {**data, 'checked_at': now.isoformat()}}, now)
            fetch.assert_not_called()
            self.assertEqual(result['fed']['date'], FED['date'])

    def test_due_and_failure_keep_old_date(self):
        due = p.moment(FED['next_release'])
        for data, now in ((FED, due), ({**FED, 'fetch_error': 'offline'}, NOW),
                          ({**FED, 'pending_update': True}, NOW)):
            rendered = p.fed_decision(data, now)
            self.assertEqual((rendered['status'], rendered['level']), ('pending', 'neutral'))
            self.assertIn('待更新', rendered['text'])
            self.assertIn('2025-06-18', rendered['text'])
            self.assertEqual(rendered['date'], FED['date'])

    def test_invalid_data_is_neutral_unavailable(self):
        changes = ({'low': None}, {'high': 'N/A'}, {'low': True}, {'high': float('nan')},
                   {'low': 5}, {'high': float('inf')}, {'action': 'unknown'}, {'date': 'bad'},
                   {'date': '2026-06-18'}, {'url': 'https://example.test/data'},
                   {'published_date': '2025-06-17'}, {'released_at': 'bad'},
                   {'released_at': '2025-07-18T14:00:00-04:00'})
        for change in changes:
            with self.subTest(change=change):
                rendered = p.fed_decision({**FED, **change}, NOW)
                self.assertEqual((rendered['status'], rendered['level']), ('unavailable', 'neutral'))
                self.assertIn('待更新', rendered['text'])
        for bad in (None, [], 'bad', {}):
            self.assertEqual(p.fed_decision(bad, NOW)['status'], 'unavailable')
        for amount in (True, '25', float('nan'), -25, 0):
            rendered = p.fed_decision({**FED, 'action': 'raise', 'change_bps': amount}, NOW)
            self.assertNotIn('基点', rendered['text'])

    def test_interface_and_no_removed_dependencies(self):
        self.assertEqual(str(inspect.signature(p.update_policy)), '(prev=None, now=None)')
        for name in ('parse_cpi', 'cpi_warning', 'cpi_from_index', 'parse_projection', 'fed_warning', 'policy_summary'):
            self.assertFalse(hasattr(p, name))
        prev = {'cpi': {'fetch_error': 'must ignore'}, 'fed': {**FED, 'checked_at': NOW.isoformat(),
                'median': float('nan'), 'year': 1900, 'projection_url': 'https://example.test/data'}}
        with patch.object(p, 'fetch_text', side_effect=[calendar(), statement()]) as fetch:
            result = p.update_policy(prev, NOW)
        self.assertEqual(fetch.call_count, 2, '旧CPI/SEP组合迁移应立即核实正式决议')
        self.assertEqual(set(result), {'fed', 'summary', 'rules'})
        self.assertNotIn('median', result['fed'])
        self.assertNotIn('projection_url', result['fed'])
        self.assertEqual(result['summary']['level'], 'neutral')
        source = inspect.getsource(p)
        self.assertNotIn('bls.gov', source)
        self.assertNotIn('stlouisfed.org', source)
        self.assertNotIn('ThreadPoolExecutor', source)
        tree = ast.parse(Path(__file__).read_text(encoding='utf-8'))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertNotIn('monitor', imports)

    def test_invalid_future_cache_recovers_from_official_statement(self):
        prev = {'fed': {**FED, 'date': '2026-06-18', 'checked_at': (NOW - timedelta(hours=2)).isoformat()}}
        with patch.object(p, 'fetch_text', side_effect=[calendar(), statement()]):
            result = p.update_policy(prev, NOW)
        self.assertEqual(result['summary']['status'], 'available')
        self.assertEqual(result['fed']['published_date'], '2025-06-18')
        self.assertEqual(result['fed']['fetch_error'], '')

    def test_normal_cache_full_24_hours_and_not_mutated(self):
        prev = {'fed': {**FED, 'checked_at': NOW.isoformat()}}
        before = copy.deepcopy(prev)
        with patch.object(p, 'fetch_text', side_effect=RuntimeError('offline')) as fetch:
            p.update_policy(prev, NOW + timedelta(hours=23, minutes=59))
            fetch.assert_not_called()
            result = p.update_policy(prev, NOW + timedelta(hours=24))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(prev, before)
        self.assertEqual(result['fed']['date'], FED['date'])
        self.assertEqual(result['summary']['status'], 'pending')

    def test_due_bypasses_24h_cache_and_new_decision_replaces_old(self):
        due = p.moment(FED['next_release'])
        prev = {'fed': {**FED, 'checked_at': (due - timedelta(minutes=10)).isoformat()}}
        text = statement('lower', 'by 1/4 percentage point', date='July 30, 2025')
        with patch.object(p, 'fetch_text', side_effect=[calendar(True), text]) as fetch:
            result = p.update_policy(prev, due)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result['fed']['published_date'], '2025-07-30')
        self.assertEqual(result['fed']['change_bps'], 25)
        self.assertEqual(result['summary']['status'], 'available')

    def test_due_failure_does_not_bypass_one_hour_retry(self):
        due = p.moment(FED['next_release'])
        prev = {'fed': {**FED, 'checked_at': (due - timedelta(minutes=10)).isoformat()}}
        with patch.object(p, 'fetch_text', side_effect=RuntimeError('timeout')) as fetch:
            first = p.update_policy(prev, due)
            for minutes in (0, 1, 30, 59):
                result = p.update_policy(first, due + timedelta(minutes=minutes))
                self.assertEqual(result['fed']['checked_at'], first['fed']['checked_at'])
            self.assertEqual(fetch.call_count, 1)
            second = p.update_policy(first, due + timedelta(hours=1))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(second['fed']['date'], FED['date'])
        self.assertEqual(second['summary']['status'], 'pending')

    def test_error_checked_before_future_due_still_limited_after_due(self):
        due = p.moment(FED['next_release'])
        prev = {'fed': {**FED, 'fetch_error': 'HTTP 403', 'checked_at': (due - timedelta(minutes=30)).isoformat()}}
        with patch.object(p, 'fetch_text') as fetch:
            result = p.update_policy(prev, due + timedelta(minutes=29))
        fetch.assert_not_called()
        self.assertEqual(result['summary']['status'], 'pending')

    def test_no_latest_link_keeps_formal_old_and_throttles_pending(self):
        now = datetime(2025, 8, 1, tzinfo=p.BJ)
        with patch.object(p, 'fetch_text', side_effect=[calendar(), statement()]) as fetch:
            result = p.update_policy({'fed': FED}, now)
            again = p.update_policy(result, now + timedelta(minutes=59))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(again['fed']['date'], FED['date'])
        self.assertEqual(again['summary']['status'], 'pending')
        self.assertEqual(result['fed']['pending_release'], FED['next_release'])
        self.assertEqual(result['fed']['next_release'], '2025-09-17T14:00:00-04:00')
        self.assertEqual(result['fed']['fetch_error'], '')

    def test_statement_failure_preserves_old_evidence_and_retry(self):
        now = datetime(2025, 8, 1, tzinfo=p.BJ)
        for failure in (RuntimeError('HTTP 403'), statement(date='July 29, 2025')):
            with patch.object(p, 'fetch_text', side_effect=[calendar(True), failure]) as fetch:
                result = p.update_policy({'fed': FED}, now)
                p.update_policy(result, now + timedelta(minutes=59))
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(result['fed']['date'], FED['date'])
            self.assertEqual(result['fed']['url'], FED['url'])
            self.assertTrue(result['fed']['fetch_error'])
            self.assertEqual(result['summary']['status'], 'pending')
            self.assertEqual(result['fed']['pending_release'], FED['next_release'])

    def test_initial_failure_and_invalid_cache_throttle(self):
        for prev in (None, {'fed': []}, {'fed': {'low': True}}):
            with patch.object(p, 'fetch_text', side_effect=RuntimeError('offline')) as fetch:
                result = p.update_policy(prev, NOW)
                p.update_policy(result, NOW + timedelta(minutes=59))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result['summary']['status'], 'unavailable')
        with patch.object(p, 'fetch_text') as fetch:
            result = p.update_policy({'fed': {'checked_at': NOW.isoformat(), 'low': True}}, NOW)
        fetch.assert_not_called()
        self.assertEqual(result['summary']['status'], 'unavailable')

    def test_stale_calendar_never_replaces_newer_cache(self):
        prev = {'fed': {**FED, 'date': '2025-07-30T14:00:00-04:00', 'published_date': '2025-07-30',
                       'released_at': '2025-07-30T14:00:00-04:00',
                       'url': 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20250730a.htm'}}
        with patch.object(p, 'fetch_text', side_effect=[calendar(), statement()]):
            result = p.update_policy(prev, datetime(2025, 8, 1, tzinfo=p.BJ))
        self.assertEqual(result['fed']['date'], prev['fed']['date'])
        self.assertTrue(result['fed']['fetch_error'])

    def test_unscheduled_real_release_time_not_assumed_14h(self):
        cal = ('2020 FOMC Meetings' + row('March', '15', '20200315', extra=' (unscheduled)') + row('April', '28-29'))
        text = statement('lower', 'by 1 percentage point', '0', '1/4', date='March 15, 2020', release='5:00 p.m. EDT')
        for now, status in ((datetime(2020, 3, 15, 16, tzinfo=p.NY), 'unavailable'),
                            (datetime(2020, 3, 15, 17, tzinfo=p.NY), 'available')):
            with patch.object(p, 'fetch_text', side_effect=[cal, text]):
                result = p.update_policy(None, now)
            self.assertEqual(result['summary']['status'], status)
        self.assertEqual(result['fed']['date'], '2020-03-15T17:00:00-04:00')
        self.assertEqual(result['fed']['change_bps'], 100)

    def test_naive_now_and_invalid_checked_at_fetch(self):
        prev = {'fed': {**FED, 'checked_at': 'bad'}}
        with patch.object(p, 'fetch_text', side_effect=[calendar(), statement()]):
            result = p.update_policy(prev, NOW.replace(tzinfo=None))
        self.assertEqual(result['summary']['status'], 'available')
        self.assertEqual(result['fed']['checked_at'], NOW.isoformat())

    def test_fetch_text_only_official_and_no_redirects_or_immediate_retries(self):
        for url in ('https://example.test/', 'http://www.federalreserve.gov/monetarypolicy/fomccalendars.htm',
                    'https://www.federalreserve.gov.evil.test/newsevents/pressreleases/monetary20250618a.htm'):
            with patch.object(p.requests, 'get') as get, self.assertRaises(ValueError):
                p.fetch_text(url)
            get.assert_not_called()
        response = Mock(status_code=200, text='<p>official</p>')
        with patch.object(p.requests, 'get', return_value=response) as get:
            self.assertEqual(p.fetch_text(FED['url']), '<p>official</p>')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        for status in (302, 403, 429):
            response = Mock(status_code=status)
            if status >= 400:
                response.raise_for_status.side_effect = p.requests.exceptions.HTTPError(str(status))
            with patch.object(p.requests, 'get', return_value=response) as get, self.assertRaises(Exception):
                p.fetch_text(p.FED_URL)
            self.assertEqual(get.call_count, 1)

    def test_saved_real_official_responses_offline(self):
        root = Path(__file__).resolve().parents[1] / 'outputs'
        cal_path = root / 'fed-official-calendar-2026-10-11.html'
        statement_path = root / 'fed-official-statement-2026-10-11.html'
        meta_path = root / 'fed-official-fetch-2026-10-11.json'
        if not all(path.exists() for path in (cal_path, statement_path, meta_path)):
            self.skipTest('本机官方抓取产物未随代码分发')
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        now = p.moment(meta['statement']['fetched_at'])
        cal = cal_path.read_text(encoding='utf-8')
        text = statement_path.read_text(encoding='utf-8')
        selected = p.parse_calendar(cal, now)['latest_statement']
        self.assertEqual(selected['statement'], meta['statement']['url'])
        data = p.parse_statement(text, selected['date'])
        self.assertEqual(data['published_date'], selected['date'])
        with patch.object(p, 'fetch_text', side_effect=[cal, text]) as fetch:
            result = p.update_policy(None, now)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result['fed']['low'], data['low'])
        self.assertEqual(result['fed']['high'], data['high'])
        self.assertEqual(result['fed']['action'], data['action'])
        self.assertEqual(result['summary']['status'], 'available')


class TestOilRegression(unittest.TestCase):
    def test_oil_same_contract_only(self):
        dates=[f'2026-09-{d:02d}' for d in range(1,22)]+['2026-10-09']
        cont={'dates':dates,'closes':[100]*22,'quote_symbol':'CL=F','quote_type':'FUTURE','currency':'USD','quote_timezone':'America/New_York','quote_name':'Crude Oil Nov 26'}
        contract={**cont,'quote_symbol':'CLX26.NYM'}
        self.assertEqual(p.oil_warning(cont,contract,'2026-10-09')['level'],'green')
        self.assertEqual(p.oil_warning(cont,None,'2026-10-09')['level'],'gray')
        self.assertEqual(p.oil_warning(cont,{**contract,'quote_name':'Crude Oil Dec 26'},'2026-10-09')['level'],'gray')
        self.assertEqual(p.oil_warning(cont,{**contract,'quote_symbol':'CLZ26.NYM'},'2026-10-09')['level'],'gray')
        self.assertEqual(p.oil_warning({**cont,'quote_type':'ETF'},contract,'2026-10-09')['level'],'gray')
        self.assertEqual(p.oil_warning(cont,contract,'2026-10-08')['level'],'gray')
        self.assertEqual(p.oil_contract_symbol('Crude Oil Nov 26'),'CLX26.NYM')
        self.assertIsNone(p.oil_contract_symbol('WTI CFD'))

    def test_oil_surge_and_missing_month(self):
        dates=[f'2026-09-{d:02d}' for d in range(1,22)]
        base={'dates':dates,'closes':[100]*20+[130],'quote_symbol':'CL=F','quote_type':'FUTURE','currency':'USD','quote_timezone':'America/New_York','quote_name':'Crude Oil Nov 26'}
        self.assertEqual(p.oil_warning(base,{**base,'quote_symbol':'CLX26.NYM'},dates[-1])['level'],'red')
        boundary={**base,'closes':[60]*15+[72]*6}
        self.assertEqual(p.oil_warning(boundary,{**boundary,'quote_symbol':'CLX26.NYM'},dates[-1])['level'],'yellow')
        self.assertEqual(p.oil_warning(base,{**base,'quote_symbol':'CLX26.NYM','closes':[130]*5,'dates':dates[-5:]},dates[-1])['level'],'gray')

    def test_oil_day_with_short_history(self):
        data={'dates':['2026-10-08','2026-10-09'],'closes':[60,63],'quote_symbol':'CL=F','quote_type':'FUTURE',
              'currency':'USD','quote_timezone':'America/New_York','quote_name':'Crude Oil Nov 26'}
        result=p.oil_warning(data,{**data,'quote_symbol':'CLX26.NYM'},'2026-10-09')
        self.assertEqual(result['day'],5)
        self.assertEqual(result['level'],'gray')
        self.assertIsNone(result['week']);self.assertIsNone(result['month'])
        self.assertNotIn('day',p.oil_warning(data,None,'2026-10-09'))

    def test_oil_invalid_history_and_month_symbols(self):
        data = {'dates': ['2026-10-08', '2026-10-09'], 'closes': [60, 63], 'quote_symbol': 'CL=F',
                'quote_type': 'FUTURE', 'currency': 'USD', 'quote_timezone': 'America/New_York', 'quote_name': 'Crude Oil Nov 26'}
        contract = {**data, 'quote_symbol': 'CLX26.NYM'}
        before = copy.deepcopy(data)
        for change in ({'closes': [True, 63]}, {'closes': [float('nan'), 63]}, {'closes': [60, 90]},
                       {'dates': ['2026-10-09', '2026-10-09']}):
            self.assertEqual(p.oil_warning(data, {**contract, **change}, '2026-10-09')['level'], 'gray')
        for month, code in zip(p.MONTHS, 'FGHJKMNQUVXZ'):
            self.assertEqual(p.oil_contract_symbol(f'Crude Oil {month[:3]} 26'), f'CL{code}26.NYM')
        self.assertEqual(data, before)


if __name__ == '__main__':
    unittest.main()
