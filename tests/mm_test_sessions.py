"""固定时点、合成行情及内存官网样本；必须通过 run_all --offline-python 运行。"""

import copy
import itertools
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import earnings_sources as es
import market_sessions as ms
import monitor


def at(value):
    return datetime.fromisoformat(value)


def history(end, count=35):
    dates = ms.preceding_sessions(end, count - 1) + [end]
    return {"dates": dates, "closes": [100.0] * count, "highs": [105.0] * count,
            "lows": [95.0] * count, "volumes": [1000.0] * count,
            "price": 100.0, "prev_close": 100.0, "source": "offline_fixture"}


class SessionTests(unittest.TestCase):
    def test_summer_winter_and_half_day_bounds(self):
        for day, opened, closed in (
            ("2026-10-20", "2026-10-20T13:30:00+00:00", "2026-10-20T20:00:00+00:00"),
            ("2026-12-09", "2026-12-09T14:30:00+00:00", "2026-12-09T21:00:00+00:00"),
            ("2026-11-27", "2026-11-27T14:30:00+00:00", "2026-11-27T18:00:00+00:00"),
            ("2025-07-03", "2025-07-03T13:30:00+00:00", "2025-07-03T17:00:00+00:00"),
        ):
            with self.subTest(day=day):
                self.assertEqual(ms.session_bounds(day), (at(opened), at(closed)))
                self.assertIsNone(ms.active_session(at(opened) - timedelta(seconds=1)))
                self.assertEqual(ms.active_session(at(opened))[0], day)
                self.assertIsNotNone(ms.active_session(at(closed) - timedelta(seconds=1)))
                self.assertIsNone(ms.active_session(at(closed)))

    def test_dst_transition_uses_calendar_not_fixed_offset(self):
        for day, hour in (("2026-03-06", 14), ("2026-03-09", 13),
                          ("2026-10-30", 13), ("2026-11-02", 14)):
            self.assertEqual(ms.session_bounds(day)[0].hour, hour)

    def test_holiday_and_weekend_are_not_regular_sessions(self):
        for day, offset in (("2026-11-26", "-05:00"), ("2026-12-25", "-05:00"),
                            ("2026-10-18", "-04:00"), ("2026-07-03", "-04:00")):
            with self.subTest(day=day):
                self.assertIsNone(ms.session_bounds(day))
                self.assertIsNone(ms.active_session(at(day + "T11:00:00" + offset)))

    def test_manual_plan_before_open_and_at_half_day_close(self):
        cases = (
            ("2026-10-20T09:29:59-04:00", True, "2026-10-19"),
            ("2026-10-20T09:30:00-04:00", False, "2026-10-20"),
            ("2026-12-09T09:30:00-05:00", False, "2026-12-09"),
            ("2026-11-27T12:59:59-05:00", False, "2026-11-27"),
            ("2026-11-27T13:00:00-05:00", True, "2026-11-27"),
            ("2026-11-26T12:00:00-05:00", True, "2026-11-25"),
            ("2026-11-28T12:00:00-05:00", True, "2026-11-27"),
        )
        for moment, closed, target in cases:
            with self.subTest(moment=moment):
                self.assertEqual(monitor.plan_run("local", at(moment)),
                                 {"closed_only": closed, "target": target})

    def test_schedule_keeps_twenty_oclock_anchor_in_every_season(self):
        for day, offset, previous in (("2026-10-20", "-04:00", "2026-10-19"),
                                      ("2026-12-09", "-05:00", "2026-12-08"),
                                      ("2026-11-27", "-05:00", "2026-11-25")):
            for clock, target in (("19:59:59", previous), ("20:00:00", day), ("20:45:00", day)):
                with self.subTest(day=day, clock=clock):
                    self.assertEqual(monitor.plan_run("schedule", at(day + "T" + clock + offset)),
                                     {"closed_only": True, "target": target})

    def test_completed_session_keeps_fifteen_minute_grace(self):
        for day, closed in (("2026-10-20", "2026-10-20T16:00:00-04:00"),
                            ("2026-11-27", "2026-11-27T13:00:00-05:00")):
            dates = history(day)["dates"]
            with patch.object(monitor, "TARGET_DATE", ""):
                before = at(closed) + timedelta(minutes=15, seconds=-1)
                ready = at(closed) + timedelta(minutes=15)
                self.assertEqual(monitor._latest_completed_session_index(dates, before), len(dates) - 2)
                self.assertEqual(monitor._latest_completed_session_index(dates, ready), len(dates) - 1)
                self.assertIsNone(monitor._latest_completed_session_index(dates[:-1], ready))
                self.assertIsNone(monitor._latest_completed_session_index(dates + [day], ready))

    def test_completed_session_respects_historical_and_holiday_targets(self):
        dates = history("2026-11-27")["dates"]
        when = at("2026-11-27T20:15:00-05:00")
        with patch.object(monitor, "TARGET_DATE", "2026-11-26"):
            self.assertEqual(dates[monitor._latest_completed_session_index(dates, when)], "2026-11-25")
        with patch.object(monitor, "TARGET_DATE", ""):
            self.assertEqual(ms.latest_completed_session(at("2026-11-26T20:15:00-05:00")), "2026-11-25")

    def test_naive_clock_is_not_interpreted_as_machine_timezone(self):
        with self.assertRaises(ValueError):
            monitor.plan_run("local", at("2026-10-20T11:00:00"))
        self.assertIsNone(ms.validated_quote_time("2026-10-20T11:00:00-04:00", at("2026-10-20T11:00:00")))


class QuoteTimeTests(unittest.TestCase):
    def test_existing_nasdaq_and_aware_iso_formats(self):
        now = at("2026-10-20T11:00:00-04:00")
        for raw in ("Oct 20, 2026 11:00 AM ET", "October 20, 2026 11:00:00 AM EDT",
                    "10/20/2026 11:00 AM ET", "2026-10-20 11:00:00 ET",
                    "2026-10-20T15:00:00Z", "2026-10-20T23:00:00+08:00", now):
            with self.subTest(raw=raw):
                self.assertEqual(ms.validated_quote_time(raw, now), now)
        winter = at("2026-12-09T11:00:00-05:00")
        self.assertEqual(ms.validated_quote_time("Dec 9, 2026 11:00 AM EST", winter), winter)

    def test_wrong_day_future_stale_missing_and_ambiguous_times_are_rejected(self):
        now = at("2026-10-20T11:00:00-04:00")
        for raw in (None, "", "today", "11:00 AM ET", "2026-10-20", "2026-10-20T11:00:00",
                    "Oct 20, 2026 11:00 AM EST", "2026-10-19T11:00:00-04:00",
                    "2026-10-20T11:00:01-04:00", "2026-10-20T10:54:59-04:00",
                    "2026-10-20T09:29:59-04:00", "2026-02-30T11:00:00-05:00", 123, True):
            with self.subTest(raw=raw):
                self.assertIsNone(ms.validated_quote_time(raw, now))
        self.assertEqual(ms.validated_quote_time("2026-10-20T10:55:00-04:00", now), now - timedelta(minutes=5))

    def test_half_day_close_and_holiday_cannot_accept_quote(self):
        for raw in ("2026-11-27T13:00:00-05:00", "2026-11-26T11:00:00-05:00",
                    "2026-10-18T11:00:00-04:00"):
            self.assertIsNone(ms.validated_quote_time(raw, at(raw)))


class RealtimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(monitor, "CLOSED_ONLY", False))
        self.enterContext(patch.object(monitor, "TARGET_DATE", ""))
        self.enterContext(patch.object(monitor, "S", dict(monitor.DEFAULT_SETTINGS, use_realtime=1)))
        self.now = at("2026-10-20T11:00:00-04:00")
        self.quote = {"price": 102.0, "ts": "Oct 20, 2026 11:00 AM ET", "volume": 1200}

    def apply(self, data, quote=None, when=None):
        with patch.object(monitor, "nasdaq_realtime", return_value=quote or self.quote):
            return monitor.apply_realtime("AAPL", data, asof=when or self.now)

    def test_append_keeps_raw_close_and_marks_missing_intraday_range(self):
        data = history("2026-10-19")
        before = copy.deepcopy(data)
        result = self.apply(data)
        self.assertEqual(data, before)
        self.assertIsNot(result, data)
        self.assertEqual(result["dates"][-2:], ["2026-10-19", "2026-10-20"])
        self.assertEqual((result["price"], result["prev_close"]), (102, 100))
        self.assertEqual(result["closes"][:-1], before["closes"])
        self.assertEqual(result["rt_volume"], 1200)
        self.assertEqual(result["volumes"][-1], 1200)
        self.assertFalse(result["ma_range_valid"][-1])
        self.assertIsNone(monitor._range52_from_history(result))
        self.assertTrue(monitor.valid_history(result))

    def test_replace_preserves_real_high_low_and_original_input(self):
        data = history("2026-10-20")
        data["highs"][-1], data["lows"][-1] = 110.0, 90.0
        before = copy.deepcopy(data)
        result = self.apply(data)
        self.assertEqual(data, before)
        self.assertEqual(len(result["dates"]), len(data["dates"]))
        self.assertEqual((result["price"], result["highs"][-1], result["lows"][-1]), (102, 110, 90))
        self.assertTrue(result["realtime"])
        self.assertTrue(monitor.valid_history(result))

    def test_replace_extends_range_only_with_observed_quote(self):
        result = self.apply(history("2026-10-20"), dict(self.quote, price=108.0))
        self.assertEqual(result["highs"][-1], 108.0)
        self.assertEqual(result["lows"][-1], 95.0)

    def test_missing_previous_session_never_becomes_yesterdays_close(self):
        missing = history("2026-10-20")
        for key in ("dates", "closes", "highs", "lows", "volumes"):
            del missing[key][-2]
        for data in (history("2026-10-16"), missing, history("2026-10-21")):
            before = copy.deepcopy(data)
            with patch.object(monitor, "nasdaq_realtime") as quote:
                self.assertIs(monitor.apply_realtime("AAPL", data, self.now), data)
                quote.assert_not_called()
            self.assertEqual(data, before)

    def test_previous_session_spans_holiday_and_weekend(self):
        for when, previous in (("2026-11-27T11:00:00-05:00", "2026-11-25"),
                               ("2026-10-19T11:00:00-04:00", "2026-10-16")):
            result = self.apply(history(previous), dict(self.quote, ts=when), at(when))
            self.assertEqual(result["dates"][-1], when[:10])
            self.assertTrue(result["realtime"])

    def test_invalid_quote_never_mutates_data(self):
        for raw in ("today", None, "2026-10-19T11:00:00-04:00", "2026-10-20T10:54:59-04:00",
                    "2026-10-20T11:00:01-04:00", "2026-10-20T11:00:00"):
            for end in ("2026-10-19", "2026-10-20"):
                data = history(end)
                before = copy.deepcopy(data)
                self.assertIs(self.apply(data, dict(self.quote, ts=raw)), data)
                self.assertEqual(data, before)
        for price in (float("nan"), float("inf"), 0, -1, True, "102"):
            data = history("2026-10-19")
            self.assertIs(self.apply(data, dict(self.quote, price=price)), data)

    def test_no_volume_never_reuses_stale_rt_volume_or_previous_daily_volume(self):
        for end in ("2026-10-19", "2026-10-20"):
            for volume in (None, 0, -1, float("nan"), "1200", True):
                data = dict(history(end), rt_volume=999999)
                result = self.apply(data, dict(self.quote, volume=volume))
                self.assertNotIn("rt_volume", result)
                self.assertEqual(result["volumes"][-1], 0)
                self.assertEqual(data["rt_volume"], 999999)
                self.assertTrue(monitor.valid_history(result))

    def test_absent_optional_source_arrays_are_not_invented(self):
        data = history("2026-10-19")
        data["highs"], data["lows"], data["volumes"] = [], [], []
        result = self.apply(data)
        self.assertEqual((result["highs"], result["lows"], result["volumes"]), ([], [], []))
        self.assertTrue(monitor.valid_history(result))

    def test_post_merge_validation_failure_keeps_original(self):
        data = history("2026-10-19")
        before = copy.deepcopy(data)
        with patch.object(monitor, "valid_history", side_effect=[True, False]) as validate:
            result = self.apply(data)
        self.assertIs(result, data)
        self.assertEqual(data, before)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(validate.call_args.args[0]["price"], 102)

    def test_corrupt_input_or_range_flags_are_not_repaired_by_live_quote(self):
        invalid = history("2026-10-19")
        invalid["prev_close"] = 99
        for data in (invalid, dict(history("2026-10-19"), ma_range_valid=[True])):
            with patch.object(monitor, "nasdaq_realtime") as quote:
                self.assertIs(monitor.apply_realtime("AAPL", data, self.now), data)
                quote.assert_not_called()

    def test_closed_mode_historical_target_and_disabled_realtime_do_not_fetch(self):
        data = history("2026-10-19")
        for target, closed, enabled in (("2026-10-19", False, 1), ("", True, 1), ("", False, 0)):
            with patch.object(monitor, "TARGET_DATE", target), patch.object(monitor, "CLOSED_ONLY", closed), \
                    patch.dict(monitor.S, use_realtime=enabled), patch.object(monitor, "nasdaq_realtime") as quote:
                self.assertIs(monitor.apply_realtime("AAPL", data, self.now), data)
                quote.assert_not_called()

    def test_closed_hours_holidays_and_half_day_do_not_fetch(self):
        data = history("2026-11-25")
        for raw in ("2026-11-27T09:29:59-05:00", "2026-11-27T13:00:00-05:00",
                    "2026-11-26T11:00:00-05:00", "2026-11-28T11:00:00-05:00"):
            with patch.object(monitor, "nasdaq_realtime") as quote:
                self.assertIs(monitor.apply_realtime("AAPL", data, at(raw)), data)
                quote.assert_not_called()

    def test_request_crossing_half_day_close_cannot_merge(self):
        moments = iter((at("2026-11-27T12:59:59-05:00"), at("2026-11-27T13:00:00-05:00")))

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return next(moments).astimezone(tz)

        data = history("2026-11-25")
        with patch.object(monitor, "datetime", Clock), \
                patch.object(monitor, "nasdaq_realtime", return_value=dict(self.quote, ts="2026-11-27T12:59:59-05:00")):
            self.assertIs(monitor.apply_realtime("AAPL", data), data)

    def response(self, **changes):
        primary = {"lastSalePrice": "$102.50", "lastTradeTimestamp": self.quote["ts"],
                   "volume": "1,200.25", "isRealTime": True}
        primary.update(changes)
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {"primaryData": primary}}
        return response

    def test_nasdaq_keeps_existing_primary_fields(self):
        with patch.object(monitor.requests, "get", return_value=self.response()) as get:
            result = monitor.nasdaq_realtime("AAPL", self.now)
        self.assertEqual(result, {"price": 102.5, "ts": self.quote["ts"], "volume": 1200})
        get.assert_called_once()

    def test_nasdaq_rejects_nonrealtime_stale_and_nonfinite_prices(self):
        for fields in ({"isRealTime": False}, {"isRealTime": "false"}, {"isRealTime": None},
                       {"lastTradeTimestamp": "today"}, {"lastTradeTimestamp": "2026-10-19T11:00:00-04:00"},
                       {"lastTradeTimestamp": "2026-10-20T10:54:59-04:00"},
                       {"lastSalePrice": "nan"}, {"lastSalePrice": "inf"}, {"lastSalePrice": "-1"}):
            with self.subTest(fields=fields), patch.object(monitor.requests, "get", return_value=self.response(**fields)):
                self.assertIsNone(monitor.nasdaq_realtime("AAPL", self.now))

    def test_nasdaq_keeps_stock_etf_and_symbol_variant_fallback(self):
        missing = MagicMock(status_code=404)
        with patch.object(monitor.requests, "get", side_effect=[missing, missing, self.response()]) as get:
            self.assertEqual(monitor.nasdaq_realtime("BRK-B", self.now)["price"], 102.5)
        self.assertIn("/BRK-B/info?assetclass=stocks", get.call_args_list[0].args[0])
        self.assertIn("/BRK-B/info?assetclass=etf", get.call_args_list[1].args[0])
        self.assertIn("/BRK.B/info?assetclass=stocks", get.call_args_list[2].args[0])

    def test_nasdaq_does_not_request_after_half_day_close(self):
        with patch.object(monitor.requests, "get") as get:
            self.assertIsNone(monitor.nasdaq_realtime("AAPL", at("2026-11-27T13:00:00-05:00")))
            get.assert_not_called()


class IntradayVolumeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(monitor, "TARGET_DATE", ""))

    def data(self, moment, volume=1500):
        data = history(moment[:10])
        data.update(rt_volume=volume, rt_ts=moment)
        data["volumes"][-1] = volume
        return data

    def test_summer_winter_and_half_day_use_true_duration(self):
        for moment, total in (("2026-10-20T12:00:00-04:00", 390),
                              ("2026-12-09T12:00:00-05:00", 390),
                              ("2026-11-27T12:00:00-05:00", 210),
                              ("2025-07-03T12:00:00-04:00", 210)):
            with self.subTest(moment=moment):
                ratio, label = monitor.intraday_volume_ratio("AAPL", self.data(moment), at(moment))
                self.assertAlmostEqual(ratio, (1500 / 150) / (1000 / total))
                self.assertEqual(label, "12:00")

    def test_delayed_snapshot_uses_quote_time_not_retrieval_time(self):
        quoted = "2026-10-20T11:55:00-04:00"
        ratio, label = monitor.intraday_volume_ratio("AAPL", self.data(quoted), at("2026-10-20T12:00:00-04:00"))
        self.assertAlmostEqual(ratio, (1500 / 145) / (1000 / 390))
        self.assertEqual(label, "11:55")

    def test_missing_wrong_day_delayed_future_timestamp_do_not_report(self):
        moment = "2026-10-20T12:00:00-04:00"
        for raw in (None, "today", "2026-10-19T12:00:00-04:00", "2026-10-20T11:54:59-04:00",
                    "2026-10-20T12:00:01-04:00"):
            self.assertEqual(monitor.intraday_volume_ratio("AAPL", dict(self.data(moment), rt_ts=raw), at(moment)), (None, ""))

    def test_missing_today_previous_session_or_middle_session_do_not_report(self):
        moment = "2026-10-20T12:00:00-04:00"
        for index in (-1, -2, -10):
            data = self.data(moment)
            for key in ("dates", "closes", "highs", "lows", "volumes"):
                del data[key][index]
            self.assertEqual(monitor.intraday_volume_ratio("AAPL", data, at(moment)), (None, ""))
        duplicate = self.data(moment)
        duplicate["dates"][-2] = duplicate["dates"][-3]
        self.assertEqual(monitor.intraday_volume_ratio("AAPL", duplicate, at(moment)), (None, ""))

    def test_quiet_period_and_historical_target(self):
        for minute, valid in (("09:44:59", False), ("09:45:00", True)):
            moment = "2026-10-20T" + minute + "-04:00"
            ratio, _ = monitor.intraday_volume_ratio("AAPL", self.data(moment), at(moment))
            self.assertEqual(ratio is not None, valid)
        moment = "2026-10-20T12:00:00-04:00"
        with patch.object(monitor, "TARGET_DATE", "2026-10-19"):
            self.assertEqual(monitor.intraday_volume_ratio("AAPL", self.data(moment), at(moment)), (None, ""))

    def test_closed_session_and_missing_volume_return_no_intraday_ratio(self):
        moment = "2026-11-27T12:59:00-05:00"
        self.assertEqual(monitor.intraday_volume_ratio("AAPL", self.data(moment), at("2026-11-27T13:00:00-05:00")), (None, ""))
        for value in (None, 0, -1, True, float("inf"), float("nan")):
            self.assertEqual(monitor.intraday_volume_ratio("AAPL", self.data(moment, value), at(moment)), (None, ""))
        for symbol in ("ESMAIN", "^VIX"):
            self.assertEqual(monitor.intraday_volume_ratio(symbol, self.data(moment), at(moment)), (None, ""))

    def test_verified_volume_keeps_existing_alert_thresholds(self):
        moment = "2026-10-20T12:00:00-04:00"
        data = self.data(moment)
        data["volumes"][-2] = 2600
        average = (2600 + 19 * 1000) / 20

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return at(moment).astimezone(tz)

        with patch.object(monitor, "NOW", at(moment)), patch.object(monitor, "datetime", Clock), \
                patch.object(monitor, "S", dict(monitor.DEFAULT_SETTINGS)):
            _, signals, detail = monitor.analyze_symbol("AAPL", {}, data)
            self.assertTrue(detail["vol_ratio_intraday"])
            self.assertAlmostEqual(detail["rt_vol_ratio"], (1500 / 150) / (average / 390))
            self.assertIn(("vol_ratio", "red"), detail["signal_marks"])
            self.assertTrue(any("盘中量比（截至 12:00 ET）" in text for text in signals))
            with patch.dict(monitor.S, vol_ratio_red=4.0):
                _, _, detail = monitor.analyze_symbol("AAPL", {}, data)
                self.assertIn(("vol_ratio", "yellow"), detail["signal_marks"])

    def test_default_clock_is_not_task_start_time(self):
        moment = "2026-10-20T12:00:00-04:00"

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return at(moment).astimezone(tz)

        with patch.object(monitor, "NOW", at("2026-10-20T11:50:00-04:00")), patch.object(monitor, "datetime", Clock):
            self.assertIsNotNone(monitor.intraday_volume_ratio("AAPL", self.data(moment))[0])


class OfficialCandidateTests(unittest.TestCase):
    def setUp(self):
        self.list_url = es.SOURCES["COST"]["list_url"]
        self.urls = ["https://" + es.SOURCES["COST"]["host"] + es.SOURCES["COST"]["event_paths"][0]
                     + f"2030/Q{i}-Earnings/default.aspx" for i in range(1, 5)]
        self.enterContext(patch.object(es.time, "monotonic", return_value=100.0))

    def page(self, title, day):
        return f"<main><h1>{title}</h1><time>{day}</time></main>"

    def listing(self, urls):
        return "<main>" + "".join(f'<a href="{url}">Earnings details</a>' for url in urls) + "</main>"

    def response(self, text="", status=200):
        response = MagicMock(status_code=status, encoding="utf-8")
        response.headers = {"Content-Type": "text/html"}
        response.iter_content.return_value = [text.encode("utf-8")]
        response.__enter__.return_value = response
        return response

    def fetch(self, pages, today="2030-10-20"):
        def get(url, **kwargs):
            self.assertIn(url, pages, "请求不得离开固定样本")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["stream"])
            value = pages[url]
            return value if not isinstance(value, str) else self.response(value)

        with patch.object(es.requests.Session, "get", side_effect=get) as request:
            result = es.fetch_official_earnings("COST", today)
        return result, request

    def test_details_in_any_order_choose_nearest_future_event(self):
        pages = {self.urls[0]: self.page("Q1 2031 Earnings Results", "2030-12-10"),
                 self.urls[1]: self.page("Q4 2030 Earnings Results", "2030-10-21"),
                 self.urls[2]: self.page("Q3 2030 Earnings Results", "2030-10-01")}
        for order in itertools.permutations(self.urls[:3]):
            with self.subTest(order=order):
                result, request = self.fetch(dict(pages, **{self.list_url: self.listing(order)}))
                self.assertEqual((result["status"], result["date"], result["source_url"]),
                                 ("ok", "2030-10-21", self.urls[1]))
                self.assertEqual(result["timing"], "")
                self.assertEqual(request.call_count, 4)

    def test_valid_list_candidate_does_not_hide_nearer_detail(self):
        listing = self.page("Q1 2031 Earnings Results", "2030-12-10") + self.listing(self.urls[:1])
        result, request = self.fetch({self.list_url: listing,
                                     self.urls[0]: self.page("Q4 2030 Earnings Results", "2030-10-21")})
        self.assertEqual(result["date"], "2030-10-21")
        self.assertEqual(request.call_count, 2)

    def test_same_day_and_timezone_target_keep_existing_date_semantics(self):
        pages = {self.list_url: self.listing(self.urls[:2]),
                 self.urls[0]: self.page("Q1 2031 Earnings Results", "2030-12-10"),
                 self.urls[1]: self.page("Q4 2030 Earnings Results", "2030-10-20")}
        result, _ = self.fetch(pages, at("2030-10-21T01:00:00+08:00"))
        self.assertEqual((result["date"], result["official_checked"]), ("2030-10-20", "2030-10-20"))

    def test_conflicting_dates_across_details_stay_unknown_even_with_past_date(self):
        for first in ("2030-10-01", "2030-10-21"):
            for order in (self.urls[:2], self.urls[:2][::-1]):
                result, _ = self.fetch({self.list_url: self.listing(order),
                                        self.urls[0]: self.page("Q4 2030 Earnings Results", first),
                                        self.urls[1]: self.page("Q4 2030 Earnings Results", "2030-10-22")})
                self.assertEqual((result["status"], result["reason"]), ("unknown", "conflicting_or_ambiguous_event"))

    def test_duplicate_same_event_and_date_are_not_a_conflict(self):
        page = self.page("Q4 2030 Earnings Results", "2030-10-21")
        result, request = self.fetch({self.list_url: self.listing(self.urls[:2]),
                                     self.urls[0]: page, self.urls[1]: page})
        self.assertEqual((result["status"], result["date"]), ("ok", "2030-10-21"))
        self.assertEqual(request.call_count, 3)

    def test_malformed_structured_detail_remains_unknown(self):
        result, _ = self.fetch({self.list_url: self.listing(self.urls[:1]),
                               self.urls[0]: self.page("Q4 2030 Earnings Results", "2030-10-21")
                               + '<script type="application/ld+json">{broken}</script>'})
        self.assertEqual((result["status"], result["reason"]), ("unknown", "conflicting_or_ambiguous_event"))

    def test_conflict_with_list_or_within_detail_is_not_overridden(self):
        detail = self.page("Q4 2030 Earnings Results", "2030-10-21")
        for listing, page in ((self.page("Q4 2030 Earnings Results", "2030-10-22") + self.listing(self.urls[:1]), detail),
                              (self.listing(self.urls[:1]), detail.replace("</main>", "<time>2030-10-22</time></main>"))):
            result, _ = self.fetch({self.list_url: listing, self.urls[0]: page})
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["reason"], "conflicting_or_ambiguous_event")

    def test_only_three_unique_details_within_original_budget(self):
        pages = {self.list_url: self.listing([self.urls[0], self.urls[0], *self.urls[1:]])}
        for i, url in enumerate(self.urls[:3], 1):
            pages[url] = self.page(f"Q{i} 2031 Earnings Results", f"2030-12-{10+i:02d}")
        result, request = self.fetch(pages)
        self.assertEqual(result["date"], "2030-12-11")
        self.assertEqual(request.call_count, 4)
        self.assertNotIn(self.urls[3], [call.args[0] for call in request.call_args_list])

    def test_forbidden_domains_queries_and_paths_are_never_requested(self):
        bad = [self.urls[0].replace("https://", "http://"), self.urls[0].replace(".com/", ".com.evil.test/"),
               self.urls[0] + "?redirect=x", self.urls[0].replace("/2030/", "/%2e%2e/"),
               self.urls[0].replace("/2030/", "/2030\\other/")]
        result, request = self.fetch({self.list_url: self.listing(bad + self.urls[:1]),
                                     self.urls[0]: self.page("Q4 2030 Earnings Results", "2030-10-21")})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.call_count, 2)

    def test_redirects_are_not_followed(self):
        response = self.response(status=302)
        response.headers["Location"] = self.urls[1]
        result, request = self.fetch({self.list_url: self.listing(self.urls[:1]), self.urls[0]: response})
        self.assertEqual((result["status"], result["reason"]), ("error", "http_302"))
        self.assertEqual(request.call_count, 2)

    def test_403_429_stop_even_after_valid_candidate(self):
        for code in (403, 429):
            result, request = self.fetch({self.list_url: self.listing(self.urls[:3]),
                                         self.urls[0]: self.page("Q1 2031 Earnings Results", "2030-12-10"),
                                         self.urls[1]: self.response(status=code)})
            self.assertEqual((result["status"], result["reason"]), ("error", f"http_{code}"))
            self.assertEqual(request.call_count, 3)

    def test_page_size_limit_remains_enforced(self):
        with patch.object(es, "_MAX_BYTES", 512):
            result, request = self.fetch({self.list_url: self.listing(self.urls[:1]), self.urls[0]: "x" * 513})
        self.assertEqual((result["status"], result["reason"]), ("error", "page_too_large"))
        self.assertEqual(request.call_count, 2)

    def test_expired_budget_does_not_claim_first_candidate_is_nearest(self):
        clock = [100.0]
        pages = {self.list_url: self.listing(self.urls[:2]),
                 self.urls[0]: self.page("Q1 2031 Earnings Results", "2030-12-10")}

        def download(session, url, timeout, deadline):
            self.assertEqual(deadline, 120.0)
            if url == self.urls[0]:
                clock[0] = 121.0
            return pages[url]

        with patch.object(es.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(es, "_download", side_effect=download) as fetch:
            result = es.fetch_official_earnings("COST", "2030-10-20")
        self.assertEqual((result["status"], result["reason"]), ("error", "budget_exhausted"))
        self.assertEqual(fetch.call_count, 2)

    def test_ordinary_detail_failure_does_not_hide_collected_valid_event(self):
        result, request = self.fetch({self.list_url: self.listing(self.urls[:2]),
                                     self.urls[0]: self.page("Q4 2030 Earnings Results", "2030-10-21"),
                                     self.urls[1]: self.response(status=500)})
        self.assertEqual((result["status"], result["date"]), ("ok", "2030-10-21"))
        self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
