"""独立参考规则、到期、失败及换月防误报；所有样本为离线测试输入。"""
import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import policy_alerts as p
import monitor

NOW = datetime(2026, 10, 10, 23, 47, tzinfo=p.BJ)
CPI = {"date": "2026-08", "mom": .3, "previous": .2, "history": [.0,.2,.3],
       "next_release": "2026-10-14T08:30:00-04:00"}
FED = {"date": "2026-09-16T14:00:00-04:00", "action": "raise", "low": 3.75, "high": 4,
       "median": 4.1, "year": 2026, "next_release": "2026-10-28T14:00:00-04:00"}


class TestReferences(unittest.TestCase):
    def test_cpi_levels(self):
        self.assertEqual(p.cpi_warning(CPI,NOW)["level"],"yellow")
        for value, history, expected in [(.2,[.3,.2],"green"),(.4,[.2,.4],"yellow"),(.4,[.4,.4],"red")]:
            d={**CPI,"mom":value,"history":history}
            self.assertEqual(p.cpi_warning(d,NOW)["level"],expected)

    def test_cpi_due_missing_future_invalid(self):
        self.assertEqual(p.cpi_warning(CPI,datetime(2026,10,14,21,tzinfo=p.BJ))["level"],"gray")
        for change in [{"date":"2026-05"},{"date":"2026-12"},{"mom":float("nan")},{"fetch_error":"HTTP 403"}]:
            self.assertEqual(p.cpi_warning({**CPI,**change},NOW)["level"],"gray")

    def test_fred_no_month_gaps_and_rounding(self):
        self.assertEqual(p.rounding(.35),.4)
        rows=[("2026-05-01",100),("2026-06-01",100),("2026-07-01",100.2),("2026-08-01",100.5)]
        self.assertEqual(p.cpi_from_index(rows,NOW)["mom"],.3)
        boundary=[('2026-06-01',320),('2026-07-01',320),('2026-08-01',320),('2026-09-01',320.8)]
        self.assertEqual(p.cpi_from_index(boundary,NOW)['mom'],.3)
        with self.assertRaises(ValueError):p.cpi_from_index([rows[0],("2026-04-01",99),*rows[2:]],NOW)

    def test_bls_parser_not_headline_or_yoy(self):
        table='<tr><th>All items</th>'+''.join('<td>9.9</td>' for _ in range(8))+'</tr>'
        table+='<tr><th>All items less food and energy</th>'+''.join(f'<td>{v}</td>' for v in [.2,.2,.4,.2,0,.2,.3,2.4])+'</tr>'
        text=('8:30 a.m. (ET) Friday, September 11, 2026 CONSUMER PRICE INDEX - AUGUST 2026 '
              'Seasonally adjusted changes from preceding month '+table+
              'The Consumer Price Index news release for September 2026 is scheduled to be published on Wednesday, October 14, 2026, at 8:30 a.m. (ET).')
        data=p.parse_cpi(text)
        self.assertEqual((data['mom'],data['previous'],data['history']),(.3,.2,[0,.2,.3]))
        self.assertEqual(data['next_release'],'2026-10-14T08:30:00-04:00')
        with self.assertRaises(ValueError):p.parse_cpi(text.replace('Seasonally adjusted changes from preceding month',''))

    def test_statement_fractions_and_failure(self):
        d=p.parse_statement('The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent.')
        self.assertEqual(d,{'action':'raise','low':3.75,'high':4.0})
        self.assertEqual(p.parse_statement('decided to maintain the target range for the federal funds rate at 3.75 to 4 percent')['action'],'maintain')
        with self.assertRaises(ValueError):p.parse_statement('Inflation remains elevated, no target rate here')

    def test_projection_exact_headers(self):
        text='<th id="xt1b1">2026</th><tr><th>Core PCE</th><td>3.4</td></tr><tr><th>Federal funds rate</th><td headers="xt1a2 xt1b1 xt1r10">4.1</td></tr><tr><th>June projection</th><td headers="xt1a2 xt1b1 xt1r11">3.8</td></tr>'
        self.assertEqual(p.parse_projection(text),{'year':2026,'median':4.1,'previous_median':3.8})
        with self.assertRaises(ValueError):p.parse_projection(text.replace('xt1a2','xt1a3'))

    def test_calendar_no_october_dotplot(self):
        def row(month,day,star=False):
            return f'<div class="fomc-meeting__month"><strong>{month}</strong></div><div class="fomc-meeting__date">{day}{"*" if star else ""}</div>'
        text='2026 FOMC Meetings'+row('September','15-16',True)+row('October','27-28')+row('December','8-9',True)+'2025 FOMC Meetings'+row('October','28-29')
        result=p.parse_calendar(text,NOW)
        self.assertEqual(result['next']['date'],'2026-10-28')
        self.assertFalse(result['next']['sep'])
        self.assertEqual(result['next_sep']['date'],'2026-12-09')
        self.assertEqual(result['latest_sep']['date'],'2026-09-16')
        self.assertEqual(p.parse_calendar(text,datetime(2026,11,3,tzinfo=p.BJ))['next']['at'],'2026-12-09T14:00:00-05:00')

    def test_fed_recent_and_due(self):
        self.assertEqual(p.fed_warning(FED,NOW)['level'],'yellow')
        self.assertEqual(p.fed_warning(FED,datetime(2026,9,17,3,tzinfo=p.BJ))['level'],'red')
        self.assertEqual(p.fed_warning(FED,datetime(2026,10,29,3,tzinfo=p.BJ))['level'],'gray')
        self.assertEqual(p.fed_warning({**FED,'fetch_error':'timeout'},NOW)['level'],'gray')

    def test_summary_missing_never_safe(self):
        result=p.policy_summary({},FED,NOW)
        self.assertEqual(result['level'],'yellow');self.assertTrue(result['incomplete'])
        self.assertIn('不完整',result['text'])
        self.assertEqual(p.policy_summary({}, {},NOW)['level'],'gray')

    def test_cache_no_network_and_failure_not_green(self):
        prev={'cpi':{**CPI,'checked_at':NOW.isoformat()},'fed':{**FED,'checked_at':NOW.isoformat()}}
        before=copy.deepcopy(prev)
        with patch.object(p,'fetch_text',side_effect=AssertionError('should not fetch')):
            result=p.update_policy(prev,NOW)
        self.assertEqual(result['cpi'],prev['cpi']);self.assertEqual(before,prev)
        with patch.object(p,'fetch_text',side_effect=RuntimeError('offline')):
            result=p.update_policy(prev,NOW+timedelta(days=2))
        self.assertEqual(result['summary']['level'],'gray')
        self.assertEqual(result['cpi']['mom'],.3)

    def test_invalid_cache_is_gray_and_render_safe(self):
        for change in ({'low':None}, {'high':'N/A'}, {'median':float('nan')}, {'year':2025}, {'action':'unknown'}):
            self.assertEqual(p.fed_warning({**FED,**change},NOW)['level'],'gray')
        for change in ({'history':[.4,'N/A']},{'history':[.4,.4]},{'mom':True}):
            self.assertEqual(p.cpi_warning({**CPI,**change},NOW)['level'],'gray')
        refs={'policy':{'cpi':{**CPI,'mom':'N/A'},'fed':{'date':FED['date'],'low':'bad'}},
              'oil':{'price':'N/A','week':1,'month':None},'assessed_at':NOW.isoformat()}
        self.assertIn('待更新',monitor._reference_card({'reference_alerts':refs}))
        with patch.object(p,'update_policy',side_effect=RuntimeError('broken cache')), patch.object(monitor,'fetch_history',return_value=None):
            self.assertEqual(monitor.build_reference_alerts()['policy']['cpi']['fetch_error'],'broken cache')

    def test_fred_stale_without_release_is_gray(self):
        recent={**CPI,'date':'2026-09','next_release':None}
        self.assertEqual(p.cpi_warning(recent,NOW)['level'],'yellow')
        self.assertEqual(p.cpi_warning(recent,datetime(2026,11,20,tzinfo=p.BJ))['level'],'gray')
        self.assertEqual(p.cpi_warning({**CPI,'released_at':'2026-10-12T08:30:00-04:00'},NOW)['level'],'gray')

    def test_projection_year_rollover(self):
        text='<th id="xt1b1">2026</th><th id="xt1b2">2027</th><tr><th>Federal funds rate</th><td headers="xt1a2 xt1b1">3.9</td><td headers="xt1a2 xt1b2">4.6</td></tr>'
        self.assertEqual(p.parse_projection(text,2027),{'year':2027,'median':4.6})
        with self.assertRaises(ValueError):p.parse_projection(text,2028)
        with self.assertRaises(ValueError):p.parse_projection(text.replace('4.6','NaN'),2027)

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
        page=monitor._oil_reference_row({'reference_alerts':{'oil':result}})
        self.assertIn('+5.00%',page);self.assertIn('oil-rise',page)
        self.assertIn('63.00',page);self.assertIn('待更新',page)

    def test_oil_row_missing_and_policy_inside_macro(self):
        missing={'reference_alerts':{'oil':{},'policy':{},'assessed_at':NOW.isoformat()}}
        page=monitor.render({},[],0,snapshot=missing)
        self.assertLess(page.index('id="macroCard"'),page.index('id="oilAlert"'))
        self.assertLess(page.index('id="oilAlert"'),page.index('id="referenceAlerts"'))
        self.assertLess(page.index('id="referenceAlerts"'),page.index('id="group_positions"'))
        self.assertLess(page.index('<div class="foot">'),page.index('id="pageNotes"'))
        self.assertLess(page.index('id="pageNotes"'),page.index('id="marketRiskRules"'))
        self.assertLess(page.index('id="marketRiskRules"'),page.index('id="policyAlertDetails"'))
        self.assertLess(page.index('id="policyAlertDetails"'),page.index('id="marketRiskNotes"'))
        macro=page.split('id="macroCard"',1)[1].split('id="group_positions"',1)[0]
        self.assertNotIn('id="marketRiskRules"',macro)
        self.assertNotIn('id="marketRiskNotes"',macro)
        self.assertNotIn('id="policyAlertDetails"',macro)
        self.assertNotIn('查看依据与来源',macro)
        self.assertEqual(page.count('id="policyAlertDetails"'),1)
        self.assertTrue(page.split('id="marketRiskNotes"',1)[1].endswith('</section>\n</div></body></html>'))
        self.assertNotIn('class="card reference-card"',page)
        self.assertIn('日涨跌',page);self.assertIn('美元／桶',page)
        self.assertIn('不计入综合灯',page)

    def test_scope_hidden_in_rules_and_policy_compact(self):
        snap={'reference_alerts':{'policy':{},'oil':{},'assessed_at':NOW.isoformat()}}
        page=monitor.render({},[],0,snapshot=snap)
        text='补充参考：通胀／利率、WTI原油不计入综合灯；综合灯仍依据原5项指标。'
        rules=page.split('<details class="macro-help" id="marketRiskRules">',1)[1].split('</details>',1)[0]
        macro=page.split('id="macroCard"',1)[1].split('id="group_positions"',1)[0]
        self.assertEqual(page.count(text),1)
        self.assertIn(text,rules)
        self.assertNotIn(text,macro)
        self.assertNotIn('reference-scope',page)
        self.assertIn('.policy-reference .reference-alert{padding:7px 12px}',page)
        self.assertIn('.policy-reference .reference-conclusion{font-size:12px;',page)
        self.assertNotIn(text,monitor.render({},[],0))

    def test_policy_details_moved_without_losing_data_or_escaping(self):
        fed={**FED,'projection_url':'https://example.test/sep','projection_date':'2026-09-16'}
        cpi={**CPI,'fetch_error':'<unsafe>','url':'https://example.test/cpi','source':'CPI <official>'}
        snap={'reference_alerts':{'policy':{'cpi':cpi,'fed':fed},'assessed_at':NOW.isoformat()}}
        conclusion=monitor._reference_card(snap)
        details=monitor._reference_card(snap,details_only=True)
        self.assertNotIn('<details',conclusion)
        self.assertNotIn('https://example.test',conclusion)
        self.assertIn('核心CPI环比 +0.3%',details)
        self.assertIn('前值 +0.2%',details)
        self.assertIn('2026年末中位数 4.1%',details)
        self.assertIn('https://example.test/sep',details)
        self.assertIn('https://example.test/cpi',details)
        self.assertIn('&lt;unsafe&gt;',details)
        self.assertIn('CPI &lt;official&gt;',details)
        self.assertEqual(monitor._reference_card({},details_only=True),'')

    def test_reference_does_not_change_score_and_escapes(self):
        snap={'reference_alerts':{'policy':{'cpi':CPI,'fed':FED},'oil':{'level':'gray','text':'<unsafe>'},'assessed_at':NOW.isoformat()}}
        before=copy.deepcopy(snap)
        page=monitor.render({},[],0,snapshot=snap)
        self.assertIn('id="referenceAlerts"',page);self.assertIn('&lt;unsafe&gt;',page)
        self.assertEqual(before,snap)
        self.assertEqual(monitor.market_risk_summary({})['valid_count'],0)
        self.assertNotIn('id="referenceAlerts"',monitor.render({},[],0))


if __name__ == '__main__':unittest.main()
