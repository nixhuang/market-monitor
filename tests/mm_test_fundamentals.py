# -*- coding: utf-8 -*-
"""真实基本面函数的离线回归；所有财务数字为合成样本，不代表公司行情。"""
import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor

TODAY = "2026-10-20"
PERIOD = "6/30/2026"
BASE_PERIOD = "9/30/2025"


def sample():
    rows = {
        "inc": {"Total Revenue": (100000, 100000), "Income Tax": (1000, 1000)},
        "bs": {"Total Liabilities": (50000, 50000), "Total Equity": (50000, 50000)},
        "cf": {"Net Cash Flow-Operating": (20000, 20000)},
        "rt": {"Gross Margin": (50, 50), "Operating Margin": (10, 10),
               "After Tax ROE": (10, 10), "Current Ratio": (2.46, 2.46), "Profit Margin": (9, 9)},
    }
    return {"period": PERIOD, "base_period": BASE_PERIOD, "source": "stockanalysis",
            "frequency": "quarterly", "schema_version": 2, "fetched_at": TODAY,
            "units": {"amount": "thousands", "currency": None, "margin": "percentage_points", "current_ratio": "times"},
            "table_periods": {t: {"value2": PERIOD, "value5": BASE_PERIOD} for t in rows},
            **{t: {label: dict(zip(("value2", "value5"), values)) for label, values in table.items()}
               for t, table in rows.items()}}


def nasdaq_payload(facts):
    data = {}
    for table, key in (("inc", "incomeStatementTable"), ("bs", "balanceSheetTable"),
                       ("cf", "cashFlowTable"), ("rt", "financialRatiosTable")):
        rows = []
        for label, values in facts[table].items():
            row = {"value1": label, **values}
            if table == "rt":
                row.update({col: f"{value * 100 if label == 'Current Ratio' else value}%"
                            for col, value in values.items() if value is not None})
            rows.append(row)
        data[key] = {"headers": dict(facts["table_periods"][table]), "rows": rows}
    return {"data": data}


def sa_pages(facts, extra_ratio=True):
    dates = ["Jun 30, 2026", "Mar 31, 2026", "Dec 31, 2025", "Sep 30, 2025"]
    pages = {}
    for page, mapping in monitor.SA_FIN_ROWS.items():
        cols = (["Oct 19, 2026"] + dates) if page == "ratios" and extra_ratio else dates
        html = ["<table><tr><td>Period Ending</td>", *[f"<td>{d}</td>" for d in cols], "</tr>"]
        for label, (table, target, scale) in mapping.items():
            row = facts[table].get(target, {})
            vals = [row.get("value2"), 0, 0, row.get("value5")]
            if len(cols) == 5:
                vals.insert(0, 999)
            def format_value(v):
                if v is None:
                    return "--"
                if scale:
                    return str(v / scale)
                return str(v) if target == "Current Ratio" else f"{v}%"
            html.extend(["<tr><td>" + label + "</td>", *[f"<td>{format_value(v)}</td>" for v in vals], "</tr>"])
        pages[page] = "".join(html) + "</table>"
    return pages


class FundamentalRegression(unittest.TestCase):
    def setUp(self):
        for name, value in (("_FUND_CACHE", {}), ("_SA_FUND_CACHE", {})):
            p = patch.object(monitor, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(monitor, "_earn_today", return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(monitor.S, monitor.FUND_DEFAULT)
        p.start()
        self.addCleanup(p.stop)

    def check(self, facts, today=TODAY):
        with patch.object(monitor, "nasdaq_financials", side_effect=AssertionError("显式facts不得查询")), \
                patch.object(monitor, "stockanalysis_financials", side_effect=AssertionError("显式facts不得查询")):
            return monitor.fundamental_check("OFFLINE", facts, today=today)

    def test_complete_sample_is_green(self):
        got = self.check(sample())
        self.assertEqual(got["level"], "green")
        self.assertTrue(got["quality"]["complete"])
        self.assertEqual(len(got["checks"]), 7)
        self.assertTrue(all(x["status"] == "checked" for x in got["checks"].values()))

    def test_pdd_2018_fetched_today_is_gray_not_current_risk(self):
        f = sample()
        f.update(period="3/31/2018", base_period="6/30/2017", fetched_at=TODAY)
        f["rt"]["Operating Margin"]["value2"] = -18
        got = monitor.fundamental_check("PDD", f, today=TODAY)
        self.assertEqual((got["level"], got["hits"]), ("gray", []))
        self.assertEqual(got["quality"]["status"], "stale")
        self.assertIn("2018-03-31", " ".join(got["context"]))
        self.assertIn("过期", monitor._fund_cache_note({"fetched": TODAY, "data": f}))

    def test_empty_and_partial_tables_cannot_turn_green(self):
        for f in ({}, {"period": PERIOD}, {**sample(), **{t: {} for t in monitor.FUND_CELLS}},
                  {**sample(), "cf": {}}, {**sample(), "rt": {}}, {**sample(), "bs": {}}):
            with self.subTest(f=list(f)):
                got = self.check(f)
                self.assertEqual(got["level"], "gray")
                self.assertFalse(got["quality"]["complete"])

    def test_only_revenue_is_partial_even_when_healthy(self):
        f = sample()
        for table in ("bs", "cf", "rt"):
            f[table] = {}
        got = self.check(f)
        self.assertEqual(got["level"], "gray")
        self.assertEqual(got["checks"]["revenue"]["status"], "checked")
        f["inc"]["Total Revenue"]["value2"] = 70000
        got = self.check(f)
        self.assertEqual(got["level"], "red")
        self.assertFalse(got["quality"]["complete"])

    def test_partial_preserves_verified_loss_without_comparison(self):
        f = sample()
        f["base_period"] = None
        f["rt"]["Operating Margin"]["value2"] = -18
        got = self.check(f)
        self.assertEqual(got["level"], "red")
        self.assertIn("营业亏损 -18%", got["hits"])
        self.assertFalse(got["quality"]["complete"])

    def test_invalid_and_future_period_reject_all_lights(self):
        for period in (None, "bad", "2/30/2026", "2026-13-01", "2027-03-31", [], {}):
            f = sample()
            f["period"] = period
            f["rt"]["Operating Margin"]["value2"] = -18
            with self.subTest(period=period):
                got = self.check(f)
                self.assertEqual((got["level"], got["hits"]), ("gray", []))

    def test_invalid_base_or_wrong_spacing_never_claims_three_quarters(self):
        for base in ("bad", None, "6/30/2026", "9/30/2026", "6/30/2025"):
            f = sample()
            f["base_period"] = base
            for periods in f["table_periods"].values():
                periods["value5"] = base
            f["inc"]["Total Revenue"]["value2"] = 100
            got = self.check(f)
            self.assertEqual(got["level"], "gray")
            self.assertEqual(got["checks"]["revenue"]["status"], "incomparable")

    def test_cross_table_current_date_mismatch_blocks_only_untrusted_rules(self):
        f = sample()
        f["rt"]["Operating Margin"]["value2"] = -18
        f["table_periods"]["rt"]["value2"] = "3/31/2026"
        got = self.check(f)
        self.assertEqual(got["level"], "gray")
        self.assertEqual(got["checks"]["operating_margin"]["status"], "incomparable")
        f["inc"]["Total Revenue"]["value2"] = 70000
        self.assertEqual(self.check(f)["level"], "red")

    def test_cross_table_base_date_mismatch_not_compared(self):
        f = sample()
        f["rt"]["Current Ratio"]["value2"] = 1.8
        f["table_periods"]["rt"]["value5"] = "6/30/2025"
        got = self.check(f)
        self.assertFalse(any("流动比率" in h for h in got["hits"]))
        self.assertEqual(got["level"], "gray")

    def test_iso_and_us_periods_are_compared_as_dates(self):
        f = sample()
        f["period"], f["base_period"] = "2026-06-30", "2025-09-30"
        self.assertEqual(self.check(f)["level"], "green")

    def test_frequency_and_grace_metadata_not_fetch_timestamp_define_age(self):
        f = sample()
        f["period"] = "2025-12-31"
        for frequency, expected in (("quarterly", False), ("semiannual", False), ("annual", True)):
            f["frequency"] = frequency
            self.assertEqual(self.check(f)["quality"]["current"], expected)
        f = sample()
        with patch.dict(monitor.S, {"fund_report_grace_days": 0}):
            self.assertEqual(self.check(f)["quality"]["status"], "stale")
        with patch.dict(monitor.S, {"fund_report_grace_days": 20}):
            self.assertTrue(self.check(f)["quality"]["current"])

    def test_unknown_frequency_is_not_certified_green(self):
        for frequency in (None, "weekly", [], {}):
            f = sample()
            f["frequency"] = frequency
            got = self.check(f)
            self.assertEqual(got["level"], "gray")

    def test_nonfinite_or_malformed_cells_are_missing(self):
        for value in (float("nan"), float("inf"), "NaN", "Infinity", [], {}, True, "--"):
            f = sample()
            f["cf"]["Net Cash Flow-Operating"]["value2"] = value
            self.assertEqual(self.check(f)["level"], "gray")
            self.assertIsNone(monitor._fund_num(value))

    def test_legacy_unknown_units_not_guessed(self):
        f = sample()
        for key in ("source", "frequency", "units", "table_periods", "schema_version"):
            f.pop(key)
        f["rt"]["Current Ratio"] = {"value2": "1.80", "value5": "2.46"}
        got = self.check(f)
        self.assertEqual(got["level"], "gray")
        self.assertEqual(got["checks"]["current_ratio"]["status"], "incomparable")
        f["rt"]["Current Ratio"] = {"value2": "180%", "value5": "246%"}
        got = self.check(f)
        self.assertTrue(any("2.46→1.80倍" in h for h in got["hits"]))
        self.assertFalse(got["quality"]["complete"])

    def test_unknown_amount_scale_does_not_claim_millions(self):
        f = sample()
        f.pop("units")
        f["cf"]["Net Cash Flow-Operating"] = {"value2": -20, "value5": -10}
        got = self.check(f)
        self.assertIn("经营现金流为负", got["hits"])
        self.assertFalse(any("M" in h for h in got["hits"]))

    def test_compact_keeps_quality_dates_units_without_mutation(self):
        f = sample()
        f["cell_periods"] = {"rt": {"Current Ratio": {"value2": PERIOD, "value5": BASE_PERIOD}}}
        f["quality"] = self.check(f)["quality"]
        original = copy.deepcopy(f)
        compact = monitor.compact_financials(f)
        self.assertEqual(self.check(compact), self.check(f))
        for key in ("units", "table_periods", "cell_periods", "quality", "source", "frequency", "fetched_at"):
            self.assertEqual(compact[key], f[key])
        compact["units"]["amount"] = "changed"
        self.assertEqual(f, original)

    def test_real_nasdaq_adapter_normalizes_ratio_and_records_dates(self):
        f = sample()
        f["rt"]["Current Ratio"]["value2"] = 1.8
        payload = nasdaq_payload(f)
        response = SimpleNamespace(status_code=200, json=lambda: payload)
        session = Mock()
        session.get.return_value = response
        with patch.object(monitor, "_fund_session", return_value=session):
            got = monitor.nasdaq_financials("OFFLINE")
        self.assertEqual(got["rt"]["Current Ratio"]["value2"], 1.8)
        self.assertEqual(got["source"], "nasdaq")
        self.assertEqual(got["frequency"], "quarterly")
        self.assertEqual(got["fetched_at"], TODAY)
        self.assertEqual(got["table_periods"]["rt"], f["table_periods"]["rt"])
        self.assertTrue(any("2.46→1.80倍" in h for h in self.check(got)["hits"]))

    def test_real_adapters_equivalent_units_and_ratio_snapshot_alignment(self):
        f = sample()
        f["rt"]["Current Ratio"]["value2"] = 1.8
        pages = sa_pages(f)
        payload = nasdaq_payload(f)
        def get(url, timeout=12):
            if "api.nasdaq.com" in url:
                return SimpleNamespace(status_code=200, json=lambda: payload)
            page = next(p for p in pages if "/" + p + "/" in url)
            return SimpleNamespace(status_code=200, text=pages[page])
        session = Mock()
        session.get.side_effect = get
        with patch.object(monitor, "_fund_session", return_value=session):
            nasdaq = monitor.nasdaq_financials("OFFLINE")
            sa = monitor.stockanalysis_financials("OFFLINE")
        self.assertEqual(monitor._fund_num(sa["rt"]["Current Ratio"]["value2"]), 1.8)
        self.assertEqual(sa["source"], "stockanalysis")
        for key in ("hits", "level", "context", "checks"):
            self.assertEqual(self.check(nasdaq)[key], self.check(sa)[key])
        self.assertEqual(self.check(sa)["level"], "yellow")
        self.assertEqual(sa["units"]["currency"], None)

    def test_real_nasdaq_misaligned_headers_and_empty_rows(self):
        payload = nasdaq_payload(sample())
        payload["data"]["financialRatiosTable"]["headers"]["value2"] = "3/31/2026"
        response = SimpleNamespace(status_code=200, json=lambda: payload)
        with patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=Mock(return_value=response))):
            f = monitor.nasdaq_financials("OFFLINE")
        self.assertEqual(self.check(f)["level"], "gray")
        payload["data"]["incomeStatementTable"]["rows"] = []
        with patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=Mock(return_value=response))):
            self.assertIsNone(monitor.nasdaq_financials("EMPTY"))

    def test_real_sa_missing_income_does_not_promote_balance_sheet_date(self):
        pages = sa_pages(sample())
        def get(url, timeout=12):
            page = next(p for p in pages if "/" + p + "/" in url)
            return SimpleNamespace(status_code=404 if page == "income-statement" else 200, text=pages[page])
        with patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=get)):
            self.assertIsNone(monitor.stockanalysis_financials("OFFLINE"))

    def test_stale_primary_nonempty_uses_fresh_backup(self):
        stale, fresh = sample(), sample()
        stale["period"] = "3/31/2018"
        with patch.object(monitor, "nasdaq_financials", return_value=stale) as primary, \
                patch.object(monitor, "stockanalysis_financials", return_value=fresh) as secondary:
            facts, stat = monitor.update_fundamentals(["PDD"], today=TODAY)
        primary.assert_called_once_with("PDD")
        secondary.assert_called_once_with("PDD")
        self.assertEqual(facts["PDD"]["period"], PERIOD)
        self.assertEqual(stat["failed"], 0)

    def test_valid_primary_keeps_backup_idle(self):
        with patch.object(monitor, "nasdaq_financials", return_value=sample()), \
                patch.object(monitor, "stockanalysis_financials") as secondary:
            monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        secondary.assert_not_called()

    def test_primary_missing_invalid_future_or_incomparable_uses_backup(self):
        invalid = []
        for period in ("bad", "3/31/2027"):
            f = sample()
            f["period"] = period
            invalid.append(f)
        f = sample()
        f["table_periods"]["rt"]["value2"] = "3/31/2026"
        invalid.extend([None, {}, f, {"period": PERIOD, "inc": {}}])
        for primary in invalid:
            with self.subTest(primary=primary), patch.object(monitor, "nasdaq_financials", return_value=primary), \
                    patch.object(monitor, "stockanalysis_financials", return_value=sample()) as secondary:
                facts, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
                secondary.assert_called_once()
                self.assertEqual(stat["failed"], 0)
                self.assertEqual(self.check(facts["OFFLINE"])["level"], "green")

    def test_source_exception_still_tries_backup(self):
        with patch.object(monitor, "nasdaq_financials", side_effect=ValueError("合成错误")), \
                patch.object(monitor, "stockanalysis_financials", return_value=sample()):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        self.assertEqual(stat["failed"], 0)
        self.assertIn("OFFLINE", facts)

    def test_both_stale_keep_history_without_success_timestamp_or_lights(self):
        f = sample()
        f["period"] = "3/31/2018"
        with patch.object(monitor, "nasdaq_financials", return_value=f), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            facts, stat = monitor.update_fundamentals(["PDD"], today=TODAY)
        self.assertNotIn("fetched", stat["data"]["PDD"])
        self.assertEqual(stat["failed"], 1)
        self.assertEqual(self.check(facts["PDD"])["level"], "gray")
        self.assertEqual(stat["data"]["PDD"]["error_kind"], "quality_invalid")
        self.assertEqual(stat["data"]["PDD"]["retry_after"], "2026-10-21")

    def test_failed_no_data_entry_persists_without_event(self):
        with patch.object(monitor, "nasdaq_financials", return_value=None), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        self.assertEqual(facts, {})
        entry = stat["data"]["OFFLINE"]
        self.assertTrue(entry["fetch_error"])
        self.assertEqual(entry["attempted"], TODAY)
        self.assertEqual(entry["retry_after"], "2026-10-27")
        self.assertIn("error", entry)

    def test_backoff_no_data_or_empty_data_never_calls_either_source(self):
        for data in (None, {}):
            entry = {"fetch_error": True, "attempted": "2026-10-19", "error_kind": "no_coverage"}
            if data is not None:
                entry["data"] = data
            with patch.object(monitor, "nasdaq_financials") as primary, \
                    patch.object(monitor, "stockanalysis_financials") as secondary:
                self.assertFalse(monitor.fundamentals_refetch_due(entry, TODAY))
                _, stat = monitor.update_fundamentals(["OFFLINE"], {"OFFLINE": entry}, TODAY,
                                                     {"OFFLINE": ["2026-10-18"]})
            primary.assert_not_called()
            secondary.assert_not_called()
            self.assertEqual(stat["fetched"], 0)
            self.assertEqual(stat["data"]["OFFLINE"]["attempted"], "2026-10-19")

    def test_retry_day_fetches_each_source_once_and_renews_failure(self):
        prev = {"OFFLINE": {"fetch_error": True, "attempted": "2026-10-13", "retry_after": TODAY}}
        with patch.object(monitor, "nasdaq_financials", return_value=None) as primary, \
                patch.object(monitor, "stockanalysis_financials", return_value=None) as secondary:
            _, stat = monitor.update_fundamentals(["OFFLINE", "OFFLINE"], prev, TODAY)
            _, same = monitor.update_fundamentals(["OFFLINE"], stat["data"], TODAY)
        primary.assert_called_once()
        secondary.assert_called_once()
        self.assertEqual(same["fetched"], 0)
        self.assertEqual(same["data"]["OFFLINE"]["retry_after"], "2026-10-27")

    def test_success_after_failure_clears_backoff(self):
        prev = {"OFFLINE": {"fetch_error": True, "attempted": "2026-10-13", "retry_after": TODAY, "error": "旧错误"}}
        with patch.object(monitor, "nasdaq_financials", return_value=sample()), \
                patch.object(monitor, "stockanalysis_financials") as secondary:
            _, stat = monitor.update_fundamentals(["OFFLINE"], prev, TODAY)
        secondary.assert_not_called()
        entry = stat["data"]["OFFLINE"]
        self.assertEqual(entry["fetched"], TODAY)
        for key in ("fetch_error", "retry_after", "error"):
            self.assertNotIn(key, entry)

    def test_failed_refresh_keeps_old_success_and_retries_next_day(self):
        prev = {"OFFLINE": {"fetched": "2026-09-01", "data": sample()}}
        with patch.object(monitor, "nasdaq_financials", return_value=None), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], prev, TODAY)
        entry = stat["data"]["OFFLINE"]
        self.assertEqual(entry["fetched"], "2026-09-01")
        self.assertEqual(facts["OFFLINE"], prev["OFFLINE"]["data"])
        self.assertFalse(monitor.fundamentals_refetch_due(entry, TODAY))
        self.assertTrue(monitor.fundamentals_refetch_due(entry, "2026-10-21"))

    def test_report_regression_does_not_overwrite_newer_cache(self):
        old = sample()
        old["period"] = "9/30/2026"
        with patch.object(monitor, "nasdaq_financials", return_value=sample()), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], {"OFFLINE": {"data": old, "fetched": "2026-09-01"}}, TODAY)
        self.assertEqual(facts["OFFLINE"]["period"], "9/30/2026")
        self.assertTrue(stat["data"]["OFFLINE"]["fetch_error"])

    def test_disabled_stops_queries_and_lights_preserves_cache(self):
        prev = {"OFFLINE": {"data": sample(), "fetched": "2026-09-01"}}
        with patch.dict(monitor.S, {"fund_enable": 0}), \
                patch.object(monitor, "nasdaq_financials") as primary, \
                patch.object(monitor, "stockanalysis_financials") as secondary:
            facts, stat = monitor.update_fundamentals(["OFFLINE"], prev, TODAY)
            self.assertIsNone(monitor.fundamental_check("OFFLINE"))
            self.assertIsNone(monitor.financials_any_source("OFFLINE"))
        primary.assert_not_called()
        secondary.assert_not_called()
        self.assertEqual(facts, {})
        self.assertEqual(stat["data"], prev)

    def test_all_fund_config_keys_are_loaded(self):
        values = {"fund_enable": 0, "fund_rev_red": -40, "fund_rev_yellow": -30,
                  "fund_om_drop": 99, "fund_gm_drop": 4, "fund_de_rise": 40,
                  "fund_cr_drop": 25, "fund_roe_drop": 40, "fund_pm_abnormal": 70,
                  "fund_report_grace_days": 30, "trigger_gap_pct": 5}
        with patch("builtins.open", mock_open(read_data=json.dumps(values))):
            got = monitor.load_settings()
        for key, value in values.items():
            self.assertEqual(got[key], value, key)

    def test_invalid_fund_configs_warn_without_resetting_other_settings(self):
        for key in monitor.FUND_DEFAULT:
            for value in (True, "99", None, float("nan"), float("inf"), -99999, 100001):
                with self.subTest(key=key, value=value), \
                        patch("builtins.open", mock_open(read_data=json.dumps({key: value, "trigger_gap_pct": 5}))), \
                        patch.object(monitor, "log") as warn:
                    got = monitor.load_settings()
                self.assertEqual(got[key], monitor.FUND_DEFAULT[key])
                self.assertEqual(got["trigger_gap_pct"], 5)
                self.assertTrue(any(key in str(call) for call in warn.call_args_list))

    def test_unknown_fund_key_and_bad_order_warn(self):
        data = {"fund_missing": 10, "fund_rev_red": -10, "fund_rev_yellow": -20, "fund_om_drop": 99}
        with patch("builtins.open", mock_open(read_data=json.dumps(data))), patch.object(monitor, "log") as warn:
            got = monitor.load_settings()
        self.assertNotIn("fund_missing", got)
        self.assertEqual(got["fund_rev_red"], -20)
        self.assertEqual(got["fund_rev_yellow"], -10)
        self.assertEqual(got["fund_om_drop"], 99)
        self.assertTrue(any("fund_missing" in str(call) for call in warn.call_args_list))
        self.assertTrue(any("顺序冲突" in str(call) for call in warn.call_args_list))

    def test_real_sa_partial_date_mismatch_is_missing_not_green(self):
        pages = sa_pages(sample())
        pages["cash-flow-statement"] = pages["cash-flow-statement"].replace("Jun 30, 2026", "Mar 30, 2026")
        def get(url, timeout=12):
            page = next(p for p in pages if "/" + p + "/" in url)
            return SimpleNamespace(status_code=200, text=pages[page])
        with patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=get)):
            f = monitor.stockanalysis_financials("OFFLINE")
        got = self.check(f)
        self.assertEqual(got["level"], "gray")
        self.assertEqual(got["checks"]["operating_cash_flow"]["status"], "missing")
        self.assertIsNone(f["cell_periods"]["cf"]["Net Cash Flow-Operating"]["value2"])

    def test_future_report_cannot_overwrite_old_cache(self):
        fresh, future = sample(), sample()
        future["period"] = "3/31/2027"
        prev = {"OFFLINE": {"fetched": "2026-09-01", "data": fresh}}
        with patch.object(monitor, "nasdaq_financials", return_value=future), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], prev, TODAY)
        self.assertEqual(facts["OFFLINE"], fresh)
        self.assertEqual(stat["failed"], 1)
        self.assertEqual(stat["data"]["OFFLINE"]["fetched"], "2026-09-01")

    def test_iso_event_baseline_and_period_progression(self):
        f = sample()
        f["period"] = "2026-06-30"
        prev = {"OFFLINE": {"fetched": "2026-10-19", "data": f}}
        with patch.object(monitor, "nasdaq_financials", return_value=f), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            _, stat = monitor.update_fundamentals(["OFFLINE"], prev, TODAY, {"OFFLINE": [TODAY]})
        self.assertEqual(stat["data"]["OFFLINE"]["event_base"], "2026-06-30")
        self.assertEqual(stat["data"]["OFFLINE"]["event_date"], TODAY)

    def test_integer_settings_and_huge_number_warn(self):
        for key, value in (("fund_enable", 0.5), ("fund_report_grace_days", 2.5), ("fund_om_drop", 10 ** 400)):
            with patch("builtins.open", mock_open(read_data=json.dumps({key: value, "trigger_gap_pct": 5}))), \
                    patch.object(monitor, "log") as warn:
                got = monitor.load_settings()
            self.assertEqual(got[key], monitor.FUND_DEFAULT[key])
            self.assertEqual(got["trigger_gap_pct"], 5)
            self.assertTrue(any(key in str(call) for call in warn.call_args_list))

    def test_oneoff_baseline_and_current_keep_existing_business_semantics(self):
        f = sample()
        f["rt"]["After Tax ROE"] = {"value2": 8.8, "value5": 23.6}
        f["rt"]["Profit Margin"]["value5"] = 90
        f["inc"]["Income Tax"]["value5"] = -4000
        got = self.check(f)
        self.assertTrue(any("基准季含一次性收益，仅供参考" in h for h in got["hits"]))
        f["inc"]["Income Tax"]["value2"] = -100
        got = self.check(f)
        self.assertEqual(got["level"], "green")
        self.assertTrue(got["oneoff"])

    def test_both_mock_timeouts_without_success_are_short_retry(self):
        with patch.object(monitor, "nasdaq_financials", side_effect=monitor.requests.exceptions.Timeout("模拟超时")), \
                patch.object(monitor, "stockanalysis_financials", side_effect=monitor.requests.exceptions.Timeout("模拟超时")):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        entry = stat["data"]["OFFLINE"]
        self.assertEqual(facts, {})
        self.assertNotIn("fetched", entry)
        self.assertEqual(entry["error_kind"], "timeout")
        self.assertEqual(entry["retry_after"], "2026-10-21")
        self.assertEqual([s["kind"] for s in entry["error_sources"]], ["timeout", "timeout"])
        self.assertFalse(monitor.fundamentals_refetch_due(entry, TODAY))
        self.assertTrue(monitor.fundamentals_refetch_due(entry, "2026-10-21"))
        self.assertIn("超时", monitor._fund_cache_note(entry))
        self.assertNotIn("暂无财报数据", monitor._fund_cache_note(entry))

    def test_real_adapters_timeout_metadata_survives_threads_and_next_day_retry(self):
        session = SimpleNamespace(get=Mock(side_effect=monitor.requests.exceptions.Timeout("模拟超时")))
        with patch.object(monitor, "_fund_session", return_value=session):
            _, first = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
            count = session.get.call_count
            _, same = monitor.update_fundamentals(["OFFLINE"], first["data"], TODAY)
            self.assertEqual(session.get.call_count, count)
            _, next_day = monitor.update_fundamentals(["OFFLINE"], first["data"], "2026-10-21")
        self.assertEqual(count, 5)
        self.assertEqual(session.get.call_count, 10)
        self.assertEqual(first["data"]["OFFLINE"]["error_kind"], "timeout")
        self.assertEqual(same["fetched"], 0)
        self.assertEqual(next_day["fetched"], 1)
        self.assertEqual(next_day["data"]["OFFLINE"]["retry_after"], "2026-10-22")
        self.assertNotIn("OFFLINE", monitor._FUND_CACHE)
        self.assertNotIn("OFFLINE", monitor._SA_FUND_CACHE)

    def test_real_http_429_5xx_403_are_not_no_coverage(self):
        for status in (429, 500, 502, 503, 403, 404):
            session = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=status)))
            with self.subTest(status=status), patch.object(monitor, "_fund_session", return_value=session):
                _, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
            entry = stat["data"]["OFFLINE"]
            self.assertEqual(entry["error_kind"], "http_error")
            self.assertEqual(entry["retry_after"], "2026-10-21")
            self.assertNotIn("fetched", entry)
            self.assertTrue(all(s["http_status"] == status for s in entry["error_sources"]))
            self.assertIn("HTTP", monitor._fund_cache_note(entry))

    def test_http_exception_keeps_status_without_truthiness_of_error_response(self):
        response = monitor.requests.Response()
        response.status_code = 403
        exc = monitor.requests.exceptions.HTTPError("模拟禁止访问", response=response)
        with patch.object(monitor, "nasdaq_financials", side_effect=exc), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            _, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        entry = stat["data"]["OFFLINE"]
        self.assertEqual(entry["error_kind"], "http_error")
        self.assertEqual(entry["error_sources"][0]["http_status"], 403)
        self.assertEqual(entry["retry_after"], "2026-10-21")

    def test_real_confirmed_empty_coverage_has_seven_day_retry(self):
        payload = {"data": {"incomeStatementTable": {"rows": []}}, "status": {"rCode": 200}}
        def get(url, timeout=12):
            if "api.nasdaq.com" in url:
                return SimpleNamespace(status_code=200, json=lambda: payload)
            return SimpleNamespace(status_code=200, text="<main>No financial data available</main>")
        with patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=Mock(side_effect=get))):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        entry = stat["data"]["OFFLINE"]
        self.assertEqual(facts, {})
        self.assertEqual(entry["error_kind"], "no_coverage")
        self.assertEqual(entry["retry_after"], "2026-10-27")
        self.assertEqual(len(entry["error_sources"]), 5)
        self.assertTrue(all(s["kind"] == "no_coverage" for s in entry["error_sources"]))
        self.assertFalse(monitor.fundamentals_refetch_due(entry, "2026-10-26"))
        self.assertTrue(monitor.fundamentals_refetch_due(entry, "2026-10-27"))

    def test_real_empty_or_block_html_and_invalid_json_are_quality_not_coverage(self):
        for body in ({}, {"data": None}, {"data": {}}, {"data": {"incomeStatementTable": {"rows": []}}, "status": {"rCode": 500}}):
            def get(url, timeout=12):
                if "api.nasdaq.com" in url:
                    return SimpleNamespace(status_code=200, json=lambda: body)
                return SimpleNamespace(status_code=200, text="<main>Access denied</main>")
            with self.subTest(body=body), patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=get)):
                _, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
            entry = stat["data"]["OFFLINE"]
            self.assertEqual(entry["error_kind"], "quality_invalid")
            self.assertEqual(entry["retry_after"], "2026-10-21")

    def test_real_stale_source_and_timeout_keep_timeout_and_history(self):
        f = sample()
        f["period"] = "3/31/2018"
        for dates in f["table_periods"].values():
            dates["value2"] = f["period"]
        payload = nasdaq_payload(f)
        def get(url, timeout=12):
            if "api.nasdaq.com" in url:
                return SimpleNamespace(status_code=200, json=lambda: payload)
            raise monitor.requests.exceptions.Timeout("模拟超时")
        with patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=get)):
            facts, stat = monitor.update_fundamentals(["PDD"], today=TODAY)
        entry = stat["data"]["PDD"]
        self.assertEqual(entry["error_kind"], "timeout")
        self.assertEqual(entry["retry_after"], "2026-10-21")
        self.assertEqual(self.check(facts["PDD"])["level"], "gray")
        self.assertEqual({s["kind"] for s in entry["error_sources"]}, {"timeout", "quality_invalid"})

    def test_transient_plus_confirmed_none_never_becomes_long_backoff(self):
        for exc, kind in ((monitor.requests.exceptions.ConnectionError("连接失败"), "transient"),
                          (monitor.requests.exceptions.Timeout("超时"), "timeout"),
                          (ValueError("数据损坏"), "quality_invalid")):
            with self.subTest(kind=kind), patch.object(monitor, "nasdaq_financials", return_value=None), \
                    patch.object(monitor, "stockanalysis_financials", side_effect=exc):
                _, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
            self.assertEqual(stat["data"]["OFFLINE"]["error_kind"], kind)
            self.assertEqual(stat["data"]["OFFLINE"]["retry_after"], "2026-10-21")

    def test_later_event_breaks_confirmed_no_coverage_backoff(self):
        with patch.object(monitor, "nasdaq_financials", return_value=None), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            _, first = monitor.update_fundamentals(["OFFLINE"], today="2026-10-19")
        with patch.object(monitor, "nasdaq_financials", return_value=sample()) as primary, \
                patch.object(monitor, "stockanalysis_financials") as secondary:
            facts, next_day = monitor.update_fundamentals(["OFFLINE"], first["data"], TODAY,
                                                         {"OFFLINE": [TODAY]})
        primary.assert_called_once()
        secondary.assert_not_called()
        self.assertEqual(next_day["fetched"], 1)
        self.assertEqual(self.check(facts["OFFLINE"])["level"], "green")
        for key in ("fetch_error", "error_kind", "error_sources", "retry_after"):
            self.assertNotIn(key, next_day["data"]["OFFLINE"])

    def test_same_or_earlier_event_does_not_retrigger_same_attempt(self):
        for kind in ("timeout", "http_error", "transient", "quality_invalid", "no_coverage"):
            for event in ("2026-10-19", TODAY):
                entry = {"attempted": TODAY, "fetch_error": True, "error_kind": kind, "event_date": event}
                self.assertFalse(monitor.fundamentals_refetch_due(entry, TODAY))
                with patch.object(monitor, "nasdaq_financials") as primary, \
                        patch.object(monitor, "stockanalysis_financials") as secondary:
                    _, stat = monitor.update_fundamentals(["OFFLINE"], {"OFFLINE": entry}, TODAY,
                                                         {"OFFLINE": [event]})
                primary.assert_not_called()
                secondary.assert_not_called()
                self.assertEqual(stat["fetched"], 0)

    def test_later_event_does_not_replace_old_unfinished_event_baseline(self):
        entry = {"attempted": "2026-10-19", "fetch_error": True, "error_kind": "no_coverage",
                 "event_date": "2026-10-15", "event_base": "2026-06-30", "retry_after": "2026-10-26"}
        with patch.object(monitor, "nasdaq_financials", return_value=None) as primary, \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            _, stat = monitor.update_fundamentals(["OFFLINE"], {"OFFLINE": entry}, TODAY,
                                                  {"OFFLINE": ["2026-10-15", TODAY]})
        primary.assert_called_once()
        updated = stat["data"]["OFFLINE"]
        self.assertEqual(updated["event_date"], "2026-10-15")
        self.assertEqual(updated["event_base"], "2026-06-30")
        self.assertEqual(updated["event_retry_date"], TODAY)
        self.assertFalse(monitor.fundamentals_refetch_due(updated, TODAY))

    def test_unclassified_legacy_cache_is_not_proof_of_no_coverage(self):
        for with_data in (False, True):
            entry = {"attempted": "2026-10-19", "fetch_error": True, "retry_after": "2026-10-26"}
            if with_data:
                entry.update(data=sample(), fetched="2026-10-01")
            self.assertFalse(monitor.fundamentals_refetch_due(entry, "2026-10-19"))
            self.assertTrue(monitor.fundamentals_refetch_due(entry, TODAY))
            original = copy.deepcopy(entry)
            with patch.object(monitor, "nasdaq_financials", return_value=sample()), \
                    patch.object(monitor, "stockanalysis_financials", return_value=None):
                _, stat = monitor.update_fundamentals(["OFFLINE"], {"OFFLINE": entry}, TODAY,
                                                     {"OFFLINE": [TODAY]})
            self.assertEqual(stat["fetched"], 1)
            self.assertEqual(entry, original)

    def test_source_metadata_does_not_leak_between_symbols_or_legacy_mock(self):
        def primary(sym):
            if sym == "TIMEOUT":
                raise monitor.requests.exceptions.Timeout("模拟超时")
            return None
        with patch.object(monitor, "nasdaq_financials", side_effect=primary), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            _, stat = monitor.update_fundamentals(["TIMEOUT", "EMPTY", "TIMEOUT", "EMPTY"], today=TODAY)
        self.assertEqual(stat["data"]["TIMEOUT"]["error_kind"], "timeout")
        self.assertEqual(stat["data"]["EMPTY"]["error_kind"], "no_coverage")
        self.assertEqual(stat["fetched"], 2)

    def test_partial_sa_http_failure_keeps_data_but_not_success_timestamp(self):
        pages = sa_pages(sample())
        def get(url, timeout=12):
            page = next(p for p in pages if "/" + p + "/" in url)
            return SimpleNamespace(status_code=503 if page == "ratios" else 200, text=pages[page])
        with patch.object(monitor, "nasdaq_financials", return_value=None), \
                patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=get)):
            facts, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        entry = stat["data"]["OFFLINE"]
        self.assertEqual(entry["error_kind"], "http_error")
        self.assertNotIn("fetched", entry)
        self.assertEqual(entry["retry_after"], "2026-10-21")
        self.assertEqual(self.check(facts["OFFLINE"])["level"], "gray")
        self.assertTrue(any(s.get("page") == "ratios" and s["kind"] == "http_error" for s in entry["error_sources"]))

    def test_partial_coverage_not_mislabeled_as_no_coverage(self):
        payload = nasdaq_payload(sample())
        payload["data"]["incomeStatementTable"]["rows"] = []
        with patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=Mock(
                return_value=SimpleNamespace(status_code=200, json=lambda: payload)))), \
                patch.object(monitor, "stockanalysis_financials", return_value=None):
            _, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        self.assertEqual(stat["data"]["OFFLINE"]["error_kind"], "quality_invalid")
        self.assertEqual(stat["data"]["OFFLINE"]["retry_after"], "2026-10-21")

    def test_html_script_placeholder_cannot_confirm_no_coverage(self):
        html = '<script>const empty = "No financial data available";</script><main>Access denied</main>'
        with patch.object(monitor, "nasdaq_financials", return_value=None), \
                patch.object(monitor, "_fund_session", return_value=SimpleNamespace(get=Mock(
                    return_value=SimpleNamespace(status_code=200, text=html)))):
            _, stat = monitor.update_fundamentals(["OFFLINE"], today=TODAY)
        self.assertEqual(stat["data"]["OFFLINE"]["error_kind"], "quality_invalid")

    def test_same_worker_metadata_reset_after_error_then_plain_none(self):
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=1)
        def primary(sym):
            if sym == "TIMEOUT":
                raise monitor.requests.exceptions.Timeout("模拟超时")
            return None
        with patch.object(monitor, "nasdaq_financials", side_effect=primary), \
                patch.object(monitor, "stockanalysis_financials", return_value=None), \
                patch.object(monitor, "ThreadPoolExecutor", return_value=executor):
            _, stat = monitor.update_fundamentals(["TIMEOUT", "EMPTY"], today=TODAY)
        self.assertEqual(stat["data"]["TIMEOUT"]["error_kind"], "timeout")
        self.assertEqual(stat["data"]["EMPTY"]["error_kind"], "no_coverage")

    def test_temporary_failure_before_new_event_retries_once_after_event(self):
        with patch.object(monitor, "nasdaq_financials", side_effect=monitor.requests.exceptions.Timeout()), \
                patch.object(monitor, "stockanalysis_financials", side_effect=monitor.requests.exceptions.Timeout()):
            _, first = monitor.update_fundamentals(["OFFLINE"], today="2026-10-19")
        with patch.object(monitor, "nasdaq_financials", return_value=sample()) as primary, \
                patch.object(monitor, "stockanalysis_financials") as secondary:
            _, second = monitor.update_fundamentals(["OFFLINE"], first["data"], TODAY, {"OFFLINE": [TODAY]})
            _, same = monitor.update_fundamentals(["OFFLINE"], second["data"], TODAY, {"OFFLINE": [TODAY]})
        primary.assert_called_once()
        secondary.assert_not_called()
        self.assertEqual(second["fetched"], 1)
        self.assertEqual(same["fetched"], 0)
        self.assertEqual(second["data"]["OFFLINE"]["event_date"], TODAY)

    def test_config_changes_real_rule_without_changing_three_quarter_baseline(self):
        f = sample()
        f["rt"]["Operating Margin"] = {"value2": 5, "value5": 10}
        self.assertEqual(self.check(f)["level"], "yellow")
        with patch.dict(monitor.S, {"fund_om_drop": 99}):
            self.assertEqual(self.check(f)["level"], "green")
        f["inc"]["Total Revenue"].update(value2=70000, value5=100000, value6=10000)
        got = self.check(f)
        self.assertTrue(any("营收较2025/9 -30%" in h for h in got["hits"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
