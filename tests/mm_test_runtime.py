"""运行状态纯离线回归：所有文件写入临时 BASE，所有抓取及推送均 mock。"""
from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m
import runtime_state as rt


IDENTITY_ENV = {"MM_SOURCE_SHA": "source-frozen", "MM_INPUT_HASH": "input-frozen",
                "MM_BUILD_ID": "build-42.3", "GITHUB_SHA": "must-not-use",
                "GITHUB_RUN_ID": "42", "MM_REQUEST_ID": "request-42"}
IDENTITY = {"source_sha": "source-frozen", "input_hash": "input-frozen", "build_id": "build-42.3"}
TARGET = "2026-10-19"
OLD = "2026-10-18T00:00:00+00:00"
NEW = "2026-10-19T00:00:00+00:00"


class TemporaryBase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(m, "BASE", str(self.base)))
        self.stack.enter_context(patch.dict(os.environ, IDENTITY_ENV, clear=True))
        self.stack.enter_context(patch.object(m, "log"))

    def read(self, name):
        return json.loads((self.base / name).read_text(encoding="utf-8"))

    def cache(self, name, value, asof=OLD, version=1):
        return {"schema_version": 1, "asof": asof, "sections": {
            name: {"schema_version": version, "asof": asof, "data": value}}}

    def save_cache(self, value):
        rt.atomic_write_json(self.base / ".cache/market-monitor.json", value)


class CacheTests(TemporaryBase):
    def test_newer_status_wins_and_missing_symbols_survive(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": {"fetched": "old"}, "BBB": {"next_retry": "later"}}}))
        status = {"finished_at": NEW, "earnings": {"data": {"AAA": {"fetched": "new"}}},
                  "risk_quotes": {"value": 7}, "summary": {"total": 5}}
        original = copy.deepcopy(status)
        previous = rt.RuntimeState(self.base).load_previous(status)
        self.assertEqual(previous["earnings"]["data"]["AAA"], {"fetched": "new"})
        self.assertEqual(previous["earnings"]["data"]["BBB"], {"next_retry": "later"})
        self.assertEqual(previous["risk_quotes"], status["risk_quotes"])
        self.assertEqual(previous["summary"], status["summary"])
        self.assertEqual(status, original)

    def test_newer_cache_wins_with_timezone_normalization(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": {
            "status": "error", "retry": 3, "attempted": NEW}}}, NEW))
        previous = rt.RuntimeState(self.base).load_previous({"finished_at": "2026-10-19T07:00:00+08:00",
            "earnings": {"data": {"AAA": {"status": "ok", "fetched": "2026-10-19T07:00:00+08:00"}}}})
        self.assertEqual(previous["earnings"]["data"]["AAA"]["status"], "error")

    def test_section_asof_takes_precedence_over_status_finished_at(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": {"source": "cache"}}}, OLD))
        previous = rt.RuntimeState(self.base).load_previous({"finished_at": "2026-10-17T00:00:00Z",
            "earnings": {"schema_version": 1, "asof": NEW, "data": {"AAA": {"source": "status"}}}})
        self.assertEqual(previous["earnings"]["data"]["AAA"]["source"], "status")

    def test_equal_asof_prefers_status(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": {"status": "error"}}}, NEW))
        previous = rt.RuntimeState(self.base).load_previous({"finished_at": NEW,
            "earnings": {"data": {"AAA": {"status": "ok"}}}})
        self.assertEqual(previous["earnings"]["data"]["AAA"]["status"], "ok")

    def test_schema_and_asof_reject_incompatible_cache(self):
        for version, asof in ((99, NEW), (True, NEW), (1, "invalid"), (1, "2026-10-20")):
            with self.subTest(version=version, asof=asof):
                self.save_cache(self.cache("earnings", {"data": {"AAA": {"status": "error"}}}, asof, version))
                status = {"earnings": {"data": {"AAA": {"status": "ok"}}}}
                self.assertEqual(rt.RuntimeState(self.base).load_previous(status)["earnings"]["data"], status["earnings"]["data"])

    def test_container_schema_and_asof_are_required(self):
        for key, value in (("schema_version", 99), ("schema_version", True), ("asof", None)):
            with self.subTest(key=key, value=value):
                cache = self.cache("earnings", {"data": {"AAA": {"status": "error"}}}, NEW)
                cache[key] = value
                self.save_cache(cache)
                self.assertEqual(rt.RuntimeState(self.base).load_previous({}), {})

    def test_unknown_status_schema_never_overridden(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": {"status": "error"}}}, NEW))
        status = {"earnings": {"schema_version": 99, "data": {"AAA": {"status": "ok"}}}}
        self.assertEqual(rt.RuntimeState(self.base).load_previous(status), status)

    def test_corrupt_and_non_object_cache_are_ignored(self):
        for value in (b"{broken", b"[]", b"null"):
            with self.subTest(value=value):
                rt.atomic_write_bytes(self.base / ".cache/market-monitor.json", value)
                self.assertEqual(rt.RuntimeState(self.base).load_previous({"summary": {"total": 3}}), {"summary": {"total": 3}})

    def test_auxiliary_whitelist_excludes_quote_snapshots(self):
        runtime = rt.RuntimeState(self.base)
        runtime.load_previous({"risk_quotes": {"price": 50}, "items": [{"price": 1}], "macro": {"price": 2}})
        runtime.checkpoint("reference_alerts", {"policy": {"checked_at": NEW}, "oil": {"price": 70},
                                                  "risk_quotes": {"price": 3}})
        cache = self.read(".cache/market-monitor.json")
        self.assertEqual(set(cache["sections"]), {"reference_alerts"})
        self.assertEqual(cache["sections"]["reference_alerts"]["data"], {"policy": {"checked_at": NEW}})
        cache["sections"]["risk_quotes"] = {"schema_version": 1, "asof": NEW, "data": {"price": 100}}
        self.save_cache(cache)
        previous = rt.RuntimeState(self.base).load_previous({"risk_quotes": {"price": 4},
            "reference_alerts": {"oil": {"price": 60}}})
        self.assertEqual(previous["risk_quotes"], {"price": 4})
        self.assertEqual(previous["reference_alerts"]["oil"], {"price": 60})
        with self.assertRaises(KeyError):
            runtime.checkpoint("risk_quotes", {"price": 5})

    def test_checkpoints_preserve_inactive_symbols_and_failures(self):
        runtime = rt.RuntimeState(self.base)
        runtime.load_previous({"finished_at": OLD, "earnings": {
            "data": {"AAA": {"fetched": OLD}, "BBB": {"next_retry": NEW, "status": "error"}}}})
        runtime.checkpoint("earnings", {"data": {"AAA": {"fetched": NEW}}})
        runtime.checkpoint("earnings", {"data": {}})
        restarted = rt.RuntimeState(self.base).load_previous({})
        self.assertEqual(restarted["earnings"]["data"]["AAA"], {"fetched": NEW})
        self.assertEqual(restarted["earnings"]["data"]["BBB"], {"next_retry": NEW, "status": "error"})

    def test_legacy_status_fundamental_is_dropped_without_mutating_input(self):
        earnings = {"data": {"AAA": {"status": "ok", "fetched": OLD, "date": "2026-10-23",
                                      "official_attempted": TARGET, "official_error": "读取失败"}},
                    "events": {"AAA": [TARGET]}}
        consensus = {"data": {"AAA": {"s": "fail", "r": OLD, "avg": 20, "try": TARGET,
                                       "next_retry": "2026-10-22", "ne": "2026-10-23", "earn_pending": [TARGET]}},
                     "quota": {"date": TARGET, "symbols": ["AAA"], "retry_symbols": ["AAA"]}}
        for fundamental in ({"data": {"LEGACY": {"fetched": NEW}}}, None, ["legacy"]):
            with self.subTest(fundamental=fundamental):
                status = {"finished_at": NEW, "fundamental": fundamental,
                          "earnings": earnings, "consensus": consensus, "summary": {"total": 5}}
                original = copy.deepcopy(status)
                runtime = rt.RuntimeState(self.base)
                previous = runtime.load_previous(status)
                self.assertEqual(previous, {key: value for key, value in status.items() if key != "fundamental"})
                self.assertEqual(status, original)
                self.assertEqual(set(runtime.sections), {"earnings", "consensus"})

    def test_legacy_cache_fundamental_is_not_migrated_or_written_back(self):
        earnings = {"data": {"AAA": {"status": "ok", "fetched": OLD, "date": "2026-10-23",
                                      "official_attempted": TARGET, "official_error": "读取失败"}},
                    "events": {"AAA": [TARGET]}}
        consensus = {"data": {"AAA": {"s": "fail", "r": OLD, "avg": 20, "try": TARGET,
                                       "next_retry": "2026-10-22", "ne": "2026-10-23", "earn_pending": [TARGET]}},
                     "quota": {"date": TARGET, "symbols": ["AAA"], "retry_symbols": ["AAA"]}}
        for version in (1, rt.SCHEMA_VERSION):
            with self.subTest(version=version):
                cache = self.cache("fundamental", {"data": {"LEGACY": {"fetched": NEW}}}, NEW, version)
                cache["schema_version"] = version
                for name, value in (("earnings", earnings), ("consensus", consensus)):
                    cache["sections"][name] = {"schema_version": version, "asof": OLD, "data": value}
                self.save_cache(cache)
                original = (self.base / ".cache/market-monitor.json").read_bytes()
                runtime = rt.RuntimeState(self.base)
                self.assertEqual(runtime.load_previous({}), {"earnings": earnings, "consensus": consensus})
                self.assertEqual((self.base / ".cache/market-monitor.json").read_bytes(), original)
                self.assertEqual(set(runtime.sections), {"earnings", "consensus"})
                runtime.checkpoint("earnings", earnings)
                rewritten = self.read(".cache/market-monitor.json")
                self.assertEqual(set(rewritten["sections"]), {"earnings", "consensus"})
                self.assertNotIn("fundamental", rewritten)
                self.assertEqual(rt.RuntimeState(self.base).load_previous({}), {"earnings": earnings, "consensus": consensus})
                self.assertTrue(rt.verify_checkpoint(self.base, rewritten["checkpoint"]["id"]))

    def test_fundamental_is_not_an_allowed_checkpoint_section(self):
        runtime = rt.RuntimeState(self.base)
        runtime.checkpoint("earnings", {"events": {"AAA": [TARGET]}})
        original = (self.base / ".cache/market-monitor.json").read_bytes()
        sections = copy.deepcopy(runtime.sections)
        for whitelist in (rt.ENTRY_SECTIONS, rt.ATTEMPT_FIELDS, rt.EVENT_FIELDS, rt.AUX_FIELDS):
            self.assertNotIn("fundamental", whitelist)
        with self.assertRaises(KeyError):
            runtime.checkpoint("fundamental", {"data": {"LEGACY": {"fetched": NEW}}})
        self.assertEqual(runtime.sections, sections)
        self.assertEqual((self.base / ".cache/market-monitor.json").read_bytes(), original)

    def test_malformed_quota_lists_do_not_break_cache_recovery(self):
        self.save_cache(self.cache("consensus", {"quota": {"date": TARGET,
            "symbols": None, "retry_symbols": "AAA"}}, OLD))
        previous = rt.RuntimeState(self.base).load_previous({"finished_at": NEW,
            "consensus": {"quota": {"date": TARGET, "symbols": ["BBB"], "retry_symbols": []}}})
        self.assertEqual(previous["consensus"]["quota"], {"date": TARGET, "symbols": ["BBB"], "retry_symbols": []})

    def test_consensus_quota_and_failure_backoff_survive_restart(self):
        runtime = rt.RuntimeState(self.base)
        runtime.load_previous({"finished_at": OLD, "consensus": {"quota": {
            "date": TARGET, "symbols": ["AAA"], "retry_symbols": ["AAA"]}}})
        runtime.checkpoint("consensus", {"data": {"BBB": {"s": "fail", "next_retry": "2026-10-22", "failures": 3}},
            "quota": {"date": TARGET, "symbols": ["BBB"], "retry_symbols": []}})
        previous = rt.RuntimeState(self.base).load_previous({})["consensus"]
        self.assertEqual(previous["quota"]["symbols"], ["AAA", "BBB"])
        self.assertEqual(previous["quota"]["retry_symbols"], ["AAA"])
        self.assertEqual(previous["data"]["BBB"]["failures"], 3)


class EntryVersionTests(TemporaryBase):
    def setUp(self):
        super().setUp()
        self.instant = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        test = self
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return test.instant.astimezone(tz) if tz else test.instant.replace(tzinfo=None)
        self.stack.enter_context(patch.object(rt, "datetime", Clock))

    def earn(self, fetched="2026-10-18", date="2026-10-23", **fields):
        return {"status": "ok", "fetched": fetched, "date": date, **fields}

    def restored(self, name, cached, saved, *, asof="2026-10-20T10:00:00Z"):
        self.save_cache(self.cache(name, {"data": {"AAA": cached}}, asof))
        return rt.RuntimeState(self.base).load_previous({"finished_at": OLD,
            name: {"data": {"AAA": saved}}})[name]["data"]["AAA"]

    def test_checkpoint_other_symbol_never_promotes_old_facts(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": self.earn()}}))
        runtime = rt.RuntimeState(self.base)
        runtime.load_previous({})
        original_meta = copy.deepcopy(runtime.sections["earnings"]["entry_meta"]["AAA"])
        runtime.checkpoint("earnings", {"data": {"BBB": self.earn("2026-10-20", "2026-10-25")}})
        cache = self.read(".cache/market-monitor.json")
        self.assertEqual(cache["sections"]["earnings"]["entry_meta"]["AAA"], original_meta)
        status = {"finished_at": NEW, "earnings": {"data": {"AAA": self.earn("2026-10-19", "2026-10-24")}}}
        restarted = rt.RuntimeState(self.base)
        previous = restarted.load_previous(status)
        self.assertEqual(previous["earnings"]["data"]["AAA"], status["earnings"]["data"]["AAA"])
        self.assertIn("BBB", previous["earnings"]["data"])
        restarted.checkpoint("consensus", {"data": {}})
        self.assertEqual(rt.RuntimeState(self.base).load_previous(status)["earnings"]["data"]["AAA"]["date"], "2026-10-24")

    def test_unchanged_included_entry_does_not_advance_any_component(self):
        runtime = rt.RuntimeState(self.base)
        first = runtime.checkpoint("earnings", {"data": {"AAA": self.earn()}})
        metadata = copy.deepcopy(first["entry_meta"]["AAA"])
        self.instant = self.instant.replace(hour=13)
        second = runtime.checkpoint("earnings", {"data": {"AAA": self.earn(), "BBB": self.earn()}})
        self.assertEqual(second["entry_meta"]["AAA"], metadata)
        self.assertNotEqual(first["asof"], second["asof"])

    def test_failed_refresh_updates_only_attempt_version(self):
        runtime = rt.RuntimeState(self.base)
        first = runtime.checkpoint("earnings", {"data": {"AAA": self.earn(attempted="2026-10-18")}})
        self.instant = self.instant.replace(hour=13)
        failure = self.earn(attempted="2026-10-20", status="error", error="离线失败", next_retry="2026-10-21",
                            official_attempted="2026-10-20", official_error="读取失败", retry=2)
        second = runtime.checkpoint("earnings", {"data": {"AAA": failure}})
        a, b = first["entry_meta"]["AAA"], second["entry_meta"]["AAA"]
        self.assertEqual(a["facts"], b["facts"])
        self.assertNotEqual(a["attempt"], b["attempt"])
        self.assertEqual(second["data"]["AAA"]["fetched"], "2026-10-18")

    def test_new_failure_without_attempt_date_uses_its_own_observation(self):
        runtime = rt.RuntimeState(self.base)
        first = runtime.checkpoint("earnings", {"data": {"AAA": self.earn()}})
        self.instant = self.instant.replace(hour=13)
        runtime.checkpoint("earnings", {"data": {"AAA": self.earn(status="error", next_retry="2026-10-22")}})
        status = {"earnings": {"data": {"AAA": self.earn("2026-10-19", "2026-10-24")}}}
        actual = rt.RuntimeState(self.base).load_previous(status)["earnings"]["data"]["AAA"]
        self.assertEqual(actual["fetched"], "2026-10-19")
        self.assertEqual(actual["date"], "2026-10-24")
        self.assertEqual(actual["status"], "error")
        self.assertEqual(actual["next_retry"], "2026-10-22")
        self.assertEqual(first["data"]["AAA"]["status"], "ok")

    def test_legacy_cache_ignores_section_time_and_prefers_entry_time(self):
        older = self.earn("2026-10-18", "2026-10-24")
        newer = self.earn("2026-10-19", "2026-10-24")
        self.assertEqual(self.restored("earnings", older, newer), newer)

    def test_new_status_facts_and_new_cache_attempt_are_merged_independently(self):
        cached = self.earn(attempted="2026-10-20", status="error", error="限流", next_retry="2026-10-23",
                           official_attempted="2026-10-20", official_error="HTTP 429", retry=3)
        status = self.earn("2026-10-19", "2026-10-24", attempted="2026-10-19")
        actual = self.restored("earnings", cached, status)
        self.assertEqual(actual["date"], status["date"])
        self.assertEqual(actual["fetched"], "2026-10-19")
        self.assertEqual(actual["attempted"], "2026-10-20")
        self.assertEqual(actual["next_retry"], "2026-10-23")
        self.assertEqual(actual["official_attempted"], "2026-10-20")
        self.assertEqual(actual["official_error"], "HTTP 429")
        self.assertEqual(actual["retry"], 3)
        self.assertEqual(actual["status"], "error")

    def test_new_cache_facts_keep_newer_status_failure(self):
        cached = self.earn("2026-10-19", "2026-10-24", attempted="2026-10-19")
        status = self.earn(attempted="2026-10-20", status="error", error="失败", next_retry="2026-10-21")
        actual = self.restored("earnings", cached, status)
        self.assertEqual(actual["date"], cached["date"])
        self.assertEqual(actual["fetched"], cached["fetched"])
        self.assertEqual(actual["attempted"], status["attempted"])
        self.assertEqual(actual["status"], "error")

    def test_success_newer_than_failure_clears_error_and_retry(self):
        cached = self.earn(attempted="2026-10-19", status="error", error="失败", next_retry="2026-10-29",
                           official_attempted="2026-10-19", official_error="读取失败", retry=3)
        status = self.earn("2026-10-20", "2026-10-24", attempted="2026-10-20")
        actual = self.restored("earnings", cached, status)
        self.assertEqual(actual, status)
        self.assertNotIn("next_retry", actual)
        self.assertNotIn("official_error", actual)

    def test_no_success_failure_is_kept_with_existing_facts(self):
        failure = {"attempted": "2026-10-20", "status": "error", "next_retry": "2026-10-27"}
        status = self.earn("2026-10-19", "2026-10-24")
        actual = self.restored("earnings", failure, status)
        self.assertEqual(actual["date"], status["date"])
        self.assertEqual(actual["fetched"], status["fetched"])
        self.assertEqual(actual["next_retry"], failure["next_retry"])

    def test_merged_attempt_clock_does_not_change_with_chosen_facts(self):
        cached = self.earn("2026-10-18", "2026-10-24", official_checked="2026-10-20")
        self.save_cache(self.cache("earnings", {"data": {"AAA": cached}}))
        runtime = rt.RuntimeState(self.base)
        runtime.load_previous({"earnings": {"data": {"AAA": self.earn("2026-10-19")}}})
        metadata = runtime.sections["earnings"]["entry_meta"]["AAA"]
        self.assertEqual(metadata["facts"]["at"], "2026-10-20T00:00:00+00:00")
        self.assertEqual(metadata["attempt"]["at"], "2026-10-19T00:00:00+00:00")
        runtime.checkpoint("earnings", {"data": {"BBB": self.earn()}})
        old_failure = self.earn(attempted="2026-10-18T12:00:00+00:00", status="error", next_retry="2026-10-28")
        actual = rt.RuntimeState(self.base).load_previous({"earnings": {"data": {"AAA": old_failure}}})["earnings"]["data"]["AAA"]
        self.assertEqual(actual, cached)
        self.assertEqual(actual["status"], "ok")
        self.assertNotIn("next_retry", actual)

    def test_retry_deadline_is_not_an_attempt_timestamp(self):
        cached = self.earn(attempted="2026-10-18", status="error", next_retry="2026-11-30")
        status = self.earn(attempted="2026-10-19", status="error", next_retry="2026-10-21")
        self.assertEqual(self.restored("earnings", cached, status)["next_retry"], "2026-10-21")

    def test_same_day_fail_then_success_uses_component_metadata(self):
        runtime = rt.RuntimeState(self.base)
        failure = self.earn("2026-10-20", "2026-10-24", attempted="2026-10-20", status="error", next_retry="2026-10-21")
        first = runtime.checkpoint("earnings", {"data": {"AAA": failure}})
        old_cache = copy.deepcopy(self.read(".cache/market-monitor.json"))
        self.instant = self.instant.replace(hour=13)
        success = self.earn("2026-10-20", "2026-10-24", attempted="2026-10-20")
        second = runtime.checkpoint("earnings", {"data": {"AAA": success}})
        self.save_cache(old_cache)
        actual = rt.RuntimeState(self.base).load_previous({"earnings": second})["earnings"]["data"]["AAA"]
        self.assertEqual(actual, success)
        self.assertNotEqual(first["entry_meta"]["AAA"]["attempt"], second["entry_meta"]["AAA"]["attempt"])

    def test_same_day_success_then_failure_preserves_facts_and_backoff(self):
        runtime = rt.RuntimeState(self.base)
        success = self.earn("2026-10-20", "2026-10-24", attempted="2026-10-20")
        first = runtime.checkpoint("earnings", {"data": {"AAA": success}})
        self.instant = self.instant.replace(hour=13)
        failure = {**success, "status": "error", "next_retry": "2026-10-21"}
        runtime.checkpoint("earnings", {"data": {"AAA": failure}})
        actual = rt.RuntimeState(self.base).load_previous({"earnings": first})["earnings"]["data"]["AAA"]
        self.assertEqual(actual, failure)

    def test_invalid_copied_metadata_does_not_override_entry_evidence(self):
        runtime = rt.RuntimeState(self.base)
        fresh = runtime.checkpoint("earnings", {"data": {"AAA": self.earn("2026-10-19", "2026-10-24")}})
        cache = self.read(".cache/market-monitor.json")
        cache["sections"]["earnings"]["data"]["data"]["AAA"] = self.earn()
        self.save_cache(cache)
        actual = rt.RuntimeState(self.base).load_previous({"earnings": fresh})["earnings"]["data"]["AAA"]
        self.assertEqual(actual["fetched"], "2026-10-19")
        self.assertEqual(actual["date"], "2026-10-24")

    def test_consensus_facts_and_failure_attempt_have_separate_versions(self):
        failure = {"s": "fail", "r": "2026-10-18", "avg": 10, "try": "2026-10-20",
                   "why": "限流", "failures": 3, "next_retry": "2026-10-25"}
        success = {"s": "ok", "r": "2026-10-19", "avg": 20, "try": "2026-10-19"}
        actual = self.restored("consensus", failure, success)
        self.assertEqual((actual["r"], actual["avg"]), ("2026-10-19", 20))
        self.assertEqual((actual["s"], actual["try"], actual["failures"]), ("fail", "2026-10-20", 3))
        self.assertEqual(actual["next_retry"], "2026-10-25")

    def test_consensus_newer_success_and_negative_coverage_clear_retry(self):
        failure = {"s": "fail", "r": "2026-10-18", "avg": 10, "try": "2026-10-19", "next_retry": "2026-10-28"}
        for success in ({"s": "ok", "r": "2026-10-20", "avg": 20, "try": "2026-10-20"},
                        {"s": "none", "r": "2026-10-20", "reason": "http404", "try": "2026-10-20"}):
            with self.subTest(success=success):
                self.assertEqual(self.restored("consensus", failure, success), success)

    def test_earnings_official_attempt_does_not_replace_newer_event_facts(self):
        failed = {"status": "ok", "date": "2026-10-25", "fetched": "2026-10-18",
                  "official_attempted": "2026-10-20", "official_error": "读取失败"}
        success = {"status": "ok", "date": "2026-10-23", "fetched": "2026-10-19",
                   "official_attempted": "2026-10-19"}
        actual = self.restored("earnings", failed, success)
        self.assertEqual(actual["date"], success["date"])
        self.assertEqual(actual["fetched"], success["fetched"])
        self.assertEqual(actual["official_attempted"], "2026-10-20")
        self.assertEqual(actual["official_error"], "读取失败")

    def test_legacy_unknown_entry_time_does_not_borrow_section_time(self):
        self.assertEqual(self.restored("earnings", {"source": "old"}, {"source": "status"}), {"source": "status"})

    def test_earnings_events_union_across_load_checkpoint_and_restart(self):
        self.save_cache(self.cache("earnings", {"events": {"AAA": ["2026-10-16"], "BBB": ["2026-10-17"]}}))
        runtime = rt.RuntimeState(self.base)
        previous = runtime.load_previous({"earnings": {"events": {"AAA": ["2026-10-18"]}}})
        self.assertEqual(previous["earnings"]["events"]["AAA"], ["2026-10-16", "2026-10-18"])
        runtime.checkpoint("earnings", {"events": {"AAA": ["2026-10-19", "invalid"]}, "data": {}})
        actual = rt.RuntimeState(self.base).load_previous({})["earnings"]["events"]
        self.assertEqual(actual, {"AAA": ["2026-10-16", "2026-10-18", "2026-10-19"], "BBB": ["2026-10-17"]})

    def test_consensus_pending_events_are_unioned_without_moving_ne_back(self):
        runtime = rt.RuntimeState(self.base)
        first = runtime.checkpoint("consensus", {"data": {"AAA": {
            "r": "2026-10-18", "s": "ok", "ne": "2026-11-01", "earn_pending": ["2026-10-17"]}}})
        self.instant = self.instant.replace(hour=13)
        runtime.checkpoint("consensus", {"data": {"AAA": {
            "r": "2026-10-18", "s": "ok", "ne": "2026-10-30", "earn_pending": ["2026-10-19"]}}})
        actual = rt.RuntimeState(self.base).load_previous({"consensus": first})["consensus"]["data"]["AAA"]
        self.assertEqual(actual["ne"], "2026-10-30")
        self.assertEqual(actual["earn_pending"], ["2026-10-17", "2026-10-19"])

    def test_same_day_consensus_event_uses_its_component_version(self):
        runtime = rt.RuntimeState(self.base)
        old = {"r": "2026-10-20", "s": "ok", "ne": "2026-10-23"}
        first = runtime.checkpoint("consensus", {"data": {"AAA": old}})
        self.instant = self.instant.replace(hour=13)
        runtime.checkpoint("consensus", {"data": {"AAA": {**old, "ne": "2026-10-24"}}})
        actual = rt.RuntimeState(self.base).load_previous({"consensus": first})["consensus"]["data"]["AAA"]
        self.assertEqual(actual["ne"], "2026-10-24")
        self.assertEqual(actual["r"], "2026-10-20")

    def test_beijing_quota_day_survives_midnight_load_checkpoint_and_restart(self):
        for instant in (datetime(2026, 10, 19, 16, tzinfo=timezone.utc),
                        datetime(2026, 10, 19, 23, 59, tzinfo=timezone.utc),
                        datetime(2026, 10, 20, 0, tzinfo=timezone.utc)):
            with self.subTest(instant=instant):
                self.instant = instant
                today = {"date": "2026-10-20", "symbols": ["AAA"], "retry_symbols": ["AAA"]}
                yesterday = {"date": "2026-10-19", "symbols": ["OLD"], "retry_symbols": []}
                self.save_cache(self.cache("consensus", {"quota": today}, OLD))
                runtime = rt.RuntimeState(self.base)
                restored = runtime.load_previous({"finished_at": instant.isoformat(),
                    "consensus": {"quota": yesterday}})
                self.assertEqual(restored["consensus"]["quota"], today)
                runtime.checkpoint("consensus", {"quota": {
                    "date": "2026-10-20", "symbols": ["BBB"], "retry_symbols": ["BBB"]}})
                runtime.checkpoint("consensus", {"quota": yesterday})
                restarted = rt.RuntimeState(self.base).load_previous({})["consensus"]["quota"]
                self.assertEqual(restarted, {"date": "2026-10-20", "symbols": ["AAA", "BBB"],
                                             "retry_symbols": ["AAA", "BBB"]})

    def test_beijing_consensus_attempt_day_sorts_before_utc_midnight(self):
        failure = {"s": "fail", "r": "2026-10-18", "avg": 10, "try": "2026-10-20",
                   "failures": 3, "next_retry": "2026-10-22"}
        success = {"s": "ok", "r": "2026-10-19", "avg": 20, "try": "2026-10-19"}
        for instant in (datetime(2026, 10, 19, 16, tzinfo=timezone.utc),
                        datetime(2026, 10, 19, 23, 59, tzinfo=timezone.utc),
                        datetime(2026, 10, 20, 0, tzinfo=timezone.utc)):
            with self.subTest(instant=instant):
                self.instant = instant
                for cached, saved in ((failure, success), (success, failure)):
                    actual = self.restored("consensus", cached, saved, asof=instant.isoformat())
                    self.assertEqual((actual["r"], actual["avg"]), ("2026-10-19", 20))
                    self.assertEqual((actual["s"], actual["try"], actual["failures"]),
                                     ("fail", "2026-10-20", 3))
                    self.assertEqual(actual["next_retry"], "2026-10-22")
                runtime = rt.RuntimeState(self.base)
                runtime.load_previous({"consensus": {"data": {"AAA": failure}}})
                runtime.checkpoint("consensus", {"data": {"AAA": success}})
                metadata = runtime.sections["consensus"]["entry_meta"]["AAA"]["attempt"]
                self.assertEqual(metadata["at"], "2026-10-19T16:00:00+00:00")
                restarted = rt.RuntimeState(self.base).load_previous({})["consensus"]["data"]["AAA"]
                self.assertEqual((restarted["s"], restarted["try"], restarted["failures"]),
                                 ("fail", "2026-10-20", 3))
                self.assertTrue(rt.verify_checkpoint(self.base, self.read(".cache/market-monitor.json")["checkpoint"]["id"]))

    def test_consensus_beijing_attempt_boundary_keeps_facts_and_timestamps_strict(self):
        self.instant = datetime(2026, 10, 19, 15, 59, tzinfo=timezone.utc)
        entry = {"s": "fail", "try": "2026-10-20", "r": "2026-10-20"}
        self.assertIsNone(rt._own_stamp("consensus", "attempt", entry))
        self.instant = self.instant.replace(hour=16, minute=0)
        expected = self.instant
        for day in ("2026-10-20", "10/20/2026"):
            self.assertEqual(rt._own_stamp("consensus", "attempt", {**entry, "try": day}), expected)
        self.assertIsNone(rt._own_stamp("consensus", "facts", entry))
        self.assertIsNone(rt._own_stamp("earnings", "attempt", {"attempted": "2026-10-20"}))
        for value in ("2026-10-21", "2026-10-20T00:00:00Z", "2026-10-20T00:00:01+08:00",
                      "2026-10-20T00:00:00", "invalid", True, None):
            self.assertIsNone(rt._own_stamp("consensus", "attempt", {"s": "fail", "try": value}))
        self.assertEqual(rt._own_stamp("consensus", "attempt", {"try": "2026-10-19T16:00:00Z"}), expected)

    def test_quota_date_boundary_does_not_relax_utc_fact_or_event_dates(self):
        self.instant = datetime(2026, 10, 19, 15, 59, tzinfo=timezone.utc)
        self.assertIsNone(rt._quota_date("2026-10-20"))
        self.instant = self.instant.replace(hour=16, minute=0)
        self.assertEqual(rt._quota_date("2026-10-20").isoformat(), "2026-10-20")
        self.assertIsNone(rt._date("2026-10-20"))
        self.assertIsNone(rt._entry_stamp("2026-10-20"))
        self.assertEqual(rt._dates(["2026-10-19", "2026-10-20"]), {"2026-10-19"})

    def test_future_or_invalid_quota_cannot_displace_valid_beijing_ledger(self):
        self.instant = datetime(2026, 10, 19, 16, tzinfo=timezone.utc)
        valid = {"quota": {"date": "2026-10-20", "symbols": ["AAA"], "retry_symbols": []}}
        for day in ("2026-10-21", "invalid", True, None):
            with self.subTest(day=day):
                invalid = {"quota": {"date": day, "symbols": ["BAD"]}}
                self.assertEqual(rt._merge_auxiliary(valid, invalid), valid)
                self.assertEqual(rt._merge_auxiliary(invalid, valid), valid)
                self.assertNotIn("quota", rt._merge_auxiliary({}, invalid))

    def test_quota_union_and_newest_ledger_day_win_over_section_time(self):
        runtime = rt.RuntimeState(self.base)
        runtime.load_previous({"consensus": {"quota": {"date": "2026-10-20", "symbols": ["AAA"], "retry_symbols": ["AAA"]}}})
        runtime.checkpoint("consensus", {"quota": {"date": "2026-10-20", "symbols": ["BBB"], "retry_symbols": ["BBB"]}})
        runtime.checkpoint("consensus", {"quota": {"date": "2026-10-19", "symbols": ["OLD"], "retry_symbols": []}})
        actual = rt.RuntimeState(self.base).load_previous({})["consensus"]["quota"]
        self.assertEqual(actual, {"date": "2026-10-20", "symbols": ["AAA", "BBB"], "retry_symbols": ["AAA", "BBB"]})


class CheckpointProofTests(TemporaryBase):
    def setUp(self):
        super().setUp()
        self.output = self.base / "github-output"
        os.environ["GITHUB_OUTPUT"] = str(self.output)

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines()) if self.output.exists() else {}

    def test_restored_cache_without_this_steps_nonce_is_not_saveable(self):
        runtime = rt.RuntimeState(self.base)
        runtime.checkpoint("earnings", {"data": {"AAA": {"status": "ok", "fetched": TARGET}}})
        nonce = self.outputs()["cache_checkpoint_id"]
        self.assertFalse(rt.verify_checkpoint(self.base, ""))
        self.assertFalse(rt.verify_checkpoint(self.base, "another-step"))
        self.assertTrue(rt.verify_checkpoint(self.base, nonce))
        with patch.dict(os.environ, {"MM_BUILD_ID": "42-other-attempt"}):
            self.assertFalse(rt.verify_checkpoint(self.base, nonce))

    def test_outputs_appear_only_after_atomic_cache_commit(self):
        runtime = rt.RuntimeState(self.base)
        real_write = rt.atomic_write_json
        def checked_write(path, value):
            self.assertFalse(self.output.exists())
            real_write(path, value)
        with patch.object(rt, "atomic_write_json", side_effect=checked_write):
            runtime.checkpoint("earnings", {"events": {"AAA": [TARGET]}})
        output = self.outputs()
        self.assertEqual(output["cache_ready"], "true")
        self.assertTrue(rt.verify_checkpoint(self.base, output["cache_checkpoint_id"]))
        self.assertEqual(rt.main(["verify-cache", "--base", str(self.base), "--checkpoint-id", output["cache_checkpoint_id"]]), 0)
        self.assertEqual(rt.main(["verify-cache", "--base", str(self.base), "--checkpoint-id", "wrong"]), 1)

    def test_first_checkpoint_failure_emits_no_ready_and_preserves_cache(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": {"fetched": TARGET}}}))
        original = (self.base / ".cache/market-monitor.json").read_bytes()
        runtime = rt.RuntimeState(self.base)
        runtime.load_previous({})
        sections = copy.deepcopy(runtime.sections)
        with patch.object(rt.os, "replace", side_effect=OSError("模拟写入失败")):
            with self.assertRaises(OSError):
                runtime.checkpoint("earnings", {"data": {"BBB": {"fetched": TARGET}}})
        self.assertEqual(self.outputs(), {})
        self.assertEqual(runtime.sections, sections)
        self.assertEqual((self.base / ".cache/market-monitor.json").read_bytes(), original)

    def test_later_checkpoint_failure_preserves_first_success_proof(self):
        runtime = rt.RuntimeState(self.base)
        runtime.checkpoint("earnings", {"events": {"AAA": [TARGET]}})
        output = self.outputs()
        old = (self.base / ".cache/market-monitor.json").read_bytes()
        with patch.object(rt.os, "replace", side_effect=OSError("模拟第二次写入失败")):
            with self.assertRaises(OSError):
                runtime.checkpoint("consensus", {"data": {"BBB": {"try": TARGET, "s": "fail"}}})
        self.assertEqual((self.base / ".cache/market-monitor.json").read_bytes(), old)
        self.assertEqual(self.outputs(), output)
        self.assertTrue(rt.verify_checkpoint(self.base, output["cache_checkpoint_id"]))

    def test_missing_identity_never_emits_ci_ready(self):
        with patch.dict(os.environ, {"MM_SOURCE_SHA": ""}):
            runtime = rt.RuntimeState(self.base)
            runtime.checkpoint("earnings", {"events": {"AAA": [TARGET]}})
            proof = self.read(".cache/market-monitor.json")["checkpoint"]
            self.assertFalse(rt.verify_checkpoint(self.base, proof["id"]))
        self.assertEqual(self.outputs(), {})

    def test_changed_cache_content_or_identity_invalidates_proof(self):
        runtime = rt.RuntimeState(self.base)
        runtime.checkpoint("earnings", {"events": {"AAA": [TARGET]}})
        original = self.read(".cache/market-monitor.json")
        nonce = self.outputs()["cache_checkpoint_id"]
        for mutation in ("content", "identity", "schema", "asof", "unknown_section"):
            with self.subTest(mutation=mutation):
                cache = copy.deepcopy(original)
                if mutation == "content":
                    cache["sections"]["earnings"]["data"]["events"]["AAA"] = []
                elif mutation == "identity":
                    cache["checkpoint"]["input_hash"] = "other-input"
                elif mutation == "schema":
                    cache["schema_version"] = 1
                elif mutation == "asof":
                    cache["asof"] = OLD
                else:
                    cache["sections"]["risk_quotes"] = {"price": 1}
                self.save_cache(cache)
                self.assertFalse(rt.verify_checkpoint(self.base, nonce))

    def test_verifier_rejects_invalid_section_even_with_matching_hash(self):
        runtime = rt.RuntimeState(self.base)
        runtime.checkpoint("earnings", {"data": {"AAA": {"status": "ok", "fetched": TARGET}}})
        original = self.read(".cache/market-monitor.json")
        nonce = self.outputs()["cache_checkpoint_id"]
        for mutation in ("schema", "metadata", "whitelist", "entries", "fundamental"):
            with self.subTest(mutation=mutation):
                cache = copy.deepcopy(original)
                section = cache["sections"]["earnings"]
                if mutation == "schema":
                    section["schema_version"] = 99
                elif mutation == "metadata":
                    section["entry_meta"]["AAA"]["facts"]["digest"] = "wrong"
                elif mutation == "whitelist":
                    section["data"]["risk_quotes"] = {"price": 123}
                elif mutation == "fundamental":
                    cache["sections"]["fundamental"] = copy.deepcopy(section)
                else:
                    section["data"]["data"] = []
                cache["checkpoint"]["sections_sha256"] = rt._digest(cache["sections"])
                self.save_cache(cache)
                self.assertFalse(rt.verify_checkpoint(self.base, nonce))

    def test_corrupt_and_missing_cache_fail_closed(self):
        self.assertFalse(rt.verify_checkpoint(self.base, "nonce"))
        rt.atomic_write_bytes(self.base / ".cache/market-monitor.json", b"{broken")
        self.assertFalse(rt.verify_checkpoint(self.base, "nonce"))


class AtomicTests(TemporaryBase):
    def test_json_replace_failure_leaves_original_and_no_temp(self):
        path = self.base / "status.json"
        rt.atomic_write_json(path, {"old": True})
        old = path.read_bytes()
        with patch.object(rt.os, "replace", side_effect=OSError("模拟替换失败")):
            with self.assertRaises(OSError):
                rt.atomic_write_json(path, {"new": True})
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_serialization_failure_does_not_touch_artifacts(self):
        rt.atomic_write_bytes(self.base / "index.html", b"old page")
        rt.atomic_write_json(self.base / "status.json", {"old": True})
        with self.assertRaises(ValueError):
            rt.write_artifacts(self.base, "new page", {"value": float("nan")})
        self.assertEqual((self.base / "index.html").read_bytes(), b"old page")
        self.assertEqual(self.read("status.json"), {"old": True})

    def test_status_failure_rolls_page_back(self):
        rt.atomic_write_bytes(self.base / "index.html", b"old page")
        rt.atomic_write_json(self.base / "status.json", {"old": True})
        original = rt.os.replace
        def fail_status(src, dst):
            if Path(dst).name == "status.json":
                raise OSError("模拟状态写入失败")
            return original(src, dst)
        with patch.object(rt.os, "replace", side_effect=fail_status):
            with self.assertRaises(OSError):
                rt.write_artifacts(self.base, "new page", {"new": True})
        self.assertEqual((self.base / "index.html").read_bytes(), b"old page")
        self.assertEqual(self.read("status.json"), {"old": True})

    def test_receipt_write_failure_restores_both_artifacts(self):
        rt.atomic_write_bytes(self.base / "index.html", b"old page")
        rt.atomic_write_json(self.base / "status.json", {"old": True})
        original = rt.os.replace
        def fail_receipt(src, dst):
            if Path(dst).name == "run-receipt.json":
                raise OSError("模拟回执写入失败")
            return original(src, dst)
        with patch.object(rt.os, "replace", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                rt.write_artifacts(self.base, "new page", {"new": True}, receipt={"result": "generated"})
        self.assertEqual((self.base / "index.html").read_bytes(), b"old page")
        self.assertEqual(self.read("status.json"), {"old": True})
        self.assertFalse((self.base / "run-receipt.json").exists())

    def test_first_run_write_failure_leaves_no_partial_page(self):
        original = rt.os.replace
        def fail_status(src, dst):
            if Path(dst).name == "status.json":
                raise OSError("模拟状态写入失败")
            return original(src, dst)
        with patch.object(rt.os, "replace", side_effect=fail_status):
            with self.assertRaises(OSError):
                rt.write_artifacts(self.base, "new page", {"new": True})
        self.assertEqual(list(self.base.iterdir()), [])

    def test_dump_http_uses_atomic_json(self):
        path = self.base / "http.json"
        with patch.object(m, "atomic_write_json", wraps=rt.atomic_write_json) as writer:
            m.dump_http_snapshot({"mode": "dump", "path": str(path), "data": {"GET offline": {"status": 200}}})
        writer.assert_called_once()
        self.assertEqual(self.read("http.json")["GET offline"]["status"], 200)


class MainTests(TemporaryBase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(m, "TARGET_DATE", TARGET))
        self.stack.enter_context(patch.object(m, "CLOSED_ONLY", True))
        self.stack.enter_context(patch.object(m, "EVENT", "schedule"))
        self.stack.enter_context(patch.object(m, "_earn_today", return_value=TARGET))
        self.stack.enter_context(patch.object(m, "_position_symbols", return_value=[]))
        self.stack.enter_context(patch.object(m, "quote_supported", return_value=True))
        self.stack.enter_context(patch.object(m, "is_etf", return_value=False))
        rt.atomic_write_json(self.base / "holdings.json", {"positions": {"AAA": {}}})
        self.old_status = {"finished_at": OLD, "target_trade_date": "2026-10-16", "risk_quotes": {"value": 5},
            "earnings": {"data": {"BBB": {"next_retry": NEW, "status": "error"}}}}
        rt.atomic_write_json(self.base / "status.json", self.old_status)
        rt.atomic_write_bytes(self.base / "index.html", b"old page")
        self.output = self.base / "github-output"
        os.environ["GITHUB_OUTPUT"] = str(self.output)
        self.mocks = {}
        def macro(previous, state):
            state.update(rows=[["2026-10-09", -0.5]], source="verified", checked_at=NEW, result="pending")
            return {}
        mocks = {
            "grouped_universe": {"return_value": ([("AAA", {}, "position")], {g["key"]: int(g["key"] == "positions") for g in m.GROUPS}, 0)},
            "build_macro": {"side_effect": macro},
            "build_reference_alerts": {"return_value": {"policy": {"checked_at": NEW}, "oil": {"price": 2}}},
            "collect_quotes": {"return_value": {}},
            "source_day_summary": {"return_value": {}},
            "attach_earnings": {"return_value": {"data": {"AAA": {"status": "error", "fetched": TARGET}}, "events": {"AAA": [TARGET]}}},
            "update_consensus": {"return_value": ({"AAA": {"s": "fail", "next_retry": "2026-10-22", "failures": 2}}, {"quota": {"date": TARGET, "symbols": ["AAA"], "retry_symbols": []}})},
            "build_snapshot": {"return_value": {**IDENTITY, "target_trade_date": TARGET, "actual_dates": {"max": TARGET},
                "summary": {"today_prices": 79, "prior_prices": 21, "missing_prices": 0}}},
            "render": {"return_value": "new page"},
            "push_serverchan": {},
        }
        for name, kwargs in mocks.items():
            self.mocks[name] = self.stack.enter_context(patch.object(m, name, **kwargs))

    def assert_old_artifacts(self):
        self.assertEqual(self.read("status.json"), self.old_status)
        self.assertEqual((self.base / "index.html").read_bytes(), b"old page")

    def test_stale_skip_saves_all_auxiliary_sections_and_receipt(self):
        self.assertEqual(m.main([]), 0)
        self.assert_old_artifacts()
        self.mocks["render"].assert_not_called()
        self.mocks["push_serverchan"].assert_not_called()
        self.assertIn("publish=no", self.output.read_text())
        receipt = self.read("run-receipt.json")
        self.assertEqual(receipt["result"], "skipped")
        self.assertEqual(receipt["reason"], "freshness_below_threshold")
        self.assertEqual((receipt["target_trade_date"], receipt["fresh"], receipt["today"], receipt["expected"]), (TARGET, False, 79, 100))
        self.assertEqual({k: receipt[k] for k in IDENTITY}, IDENTITY)
        sections = self.read(".cache/market-monitor.json")["sections"]
        self.assertEqual(set(sections), set(rt.AUX_FIELDS))
        self.assertEqual(sections["earnings"]["data"]["data"]["BBB"]["next_retry"], NEW)
        self.assertEqual(sections["consensus"]["data"]["data"]["AAA"]["failures"], 2)
        self.assertNotIn("oil", sections["reference_alerts"]["data"])
        self.assertEqual(sections["nfci_cache"]["data"]["result"], "pending")

    def test_exactly_eighty_percent_generates_not_publishes(self):
        self.mocks["build_snapshot"].return_value["summary"].update(today_prices=80, prior_prices=20)
        self.assertEqual(m.main([]), 0)
        receipt = self.read("run-receipt.json")
        self.assertEqual(receipt["result"], "generated")
        self.assertEqual(receipt["reason"], "awaiting_publish")
        self.assertTrue(receipt["fresh"])
        self.assertNotIn("published", receipt)
        self.assertNotIn("publish=no", self.output.read_text())
        self.assertIn("cache_ready=true", self.output.read_text())
        self.assertEqual((self.base / "index.html").read_text(), "new page")
        self.assertEqual({k: self.read("status.json")[k] for k in IDENTITY}, IDENTITY)
        self.mocks["push_serverchan"].assert_called_once()
        self.assertEqual(self.mocks["push_serverchan"].call_args.kwargs["snapshot"]["build_id"], IDENTITY["build_id"])

    def test_manual_stale_run_still_generates(self):
        with patch.object(m, "EVENT", "workflow_dispatch"):
            self.assertEqual(m.main([]), 0)
        self.assertEqual(self.read("run-receipt.json")["result"], "generated")
        self.assertFalse(self.read("run-receipt.json")["fresh"])

    def test_render_exception_saves_failed_receipt_and_old_artifacts(self):
        self.mocks["build_snapshot"].return_value["summary"].update(today_prices=80, prior_prices=20)
        self.mocks["render"].side_effect = RuntimeError("模拟渲染失败")
        with self.assertRaisesRegex(RuntimeError, "模拟渲染失败"):
            m.main([])
        self.assert_old_artifacts()
        receipt = self.read("run-receipt.json")
        self.assertEqual(receipt["result"], "failed")
        self.assertIn("RuntimeError", receipt["reason"])
        self.assertEqual((receipt["today"], receipt["expected"]), (80, 100))
        self.assertEqual(set(self.read(".cache/market-monitor.json")["sections"]), set(rt.AUX_FIELDS))
        output = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        self.assertEqual(output["cache_ready"], "true")
        self.assertTrue(rt.verify_checkpoint(self.base, output["cache_checkpoint_id"]))
        self.mocks["push_serverchan"].assert_not_called()

    def test_generated_receipt_failure_rolls_back_then_records_failed(self):
        self.mocks["build_snapshot"].return_value["summary"].update(today_prices=80, prior_prices=20)
        original = rt.os.replace
        failed_once = False
        def fail_first_receipt(src, dst):
            nonlocal failed_once
            if Path(dst).name == "run-receipt.json" and not failed_once:
                failed_once = True
                raise OSError("模拟首次回执写入失败")
            return original(src, dst)
        with patch.object(rt.os, "replace", side_effect=fail_first_receipt):
            with self.assertRaises(OSError):
                m.main([])
        self.assert_old_artifacts()
        self.assertEqual(self.read("run-receipt.json")["result"], "failed")
        self.mocks["push_serverchan"].assert_not_called()

    def test_macro_exception_keeps_partial_success_cache(self):
        def failed(previous, state):
            state.update(rows=[["2026-10-09", -0.5]], checked_at=NEW, result="ok", source="verified")
            raise RuntimeError("模拟宏观中断")
        self.mocks["build_macro"].side_effect = failed
        with self.assertRaises(RuntimeError):
            m.main([])
        self.assert_old_artifacts()
        receipt = self.read("run-receipt.json")
        self.assertEqual(receipt["result"], "failed")
        self.assertIsNone(receipt["fresh"])
        self.assertIsNone(receipt["expected"])
        self.assertEqual(receipt["target_trade_date"], TARGET)
        sections = self.read(".cache/market-monitor.json")["sections"]
        self.assertEqual(sections["nfci_cache"]["data"]["result"], "ok")
        self.assertIn("BBB", sections["earnings"]["data"]["data"])
        self.assertIsNone(m.QUOTE_DEADLINE)
        self.assertFalse(m.YAHOO_GATE.enabled)

    def test_exception_before_fetch_leaves_existing_cache_bytes(self):
        self.save_cache(self.cache("earnings", {"data": {"AAA": {"status": "ok"}}}))
        original = (self.base / ".cache/market-monitor.json").read_bytes()
        self.mocks["grouped_universe"].side_effect = RuntimeError("模拟配置错误")
        with self.assertRaises(RuntimeError):
            m.main([])
        self.assert_old_artifacts()
        self.assertEqual((self.base / ".cache/market-monitor.json").read_bytes(), original)
        self.assertEqual(self.read("run-receipt.json")["result"], "failed")

    def test_second_run_restores_unpublished_cache(self):
        m.main([])
        m.main([])
        restored = self.mocks["build_macro"].call_args.args[0]
        self.assertEqual(restored["risk_quotes"], self.old_status["risk_quotes"])
        self.assertEqual(restored["consensus"]["data"]["AAA"]["next_retry"], "2026-10-22")
        self.assertEqual(restored["earnings"]["data"]["BBB"]["next_retry"], NEW)
        self.assertNotIn("fundamental", restored)
        self.assert_old_artifacts()

    def test_generated_output_never_restores_legacy_fundamental(self):
        self.old_status["fundamental"] = {"data": {"AAA": {"fetched": OLD, "next_retry": NEW}}}
        rt.atomic_write_json(self.base / "status.json", self.old_status)
        self.save_cache(self.cache("fundamental", {"data": {"LEGACY": {"fetched": NEW}}}, NEW))
        self.mocks["build_snapshot"].return_value["summary"].update(today_prices=80, prior_prices=20)
        self.assertEqual(m.main([]), 0)
        previous = self.mocks["build_macro"].call_args.args[0]
        self.assertNotIn("fundamental", previous)
        snapshot = self.read("status.json")
        self.assertNotIn("fundamental", snapshot)
        self.assertTrue({"earnings", "consensus"} <= snapshot.keys())
        self.assertEqual(snapshot["earnings"]["data"]["AAA"]["status"], "error")
        self.assertEqual(snapshot["consensus"]["data"]["AAA"]["failures"], 2)
        sections = self.read(".cache/market-monitor.json")["sections"]
        self.assertNotIn("fundamental", sections)
        self.assertEqual(set(sections), set(rt.AUX_FIELDS))
        rendered = self.mocks["render"].call_args.kwargs["snapshot"]
        self.assertNotIn("fundamental", rendered)
        self.assertEqual(self.read("run-receipt.json")["result"], "generated")


class IdentityAndEnsureTests(TemporaryBase):
    def test_build_snapshot_uses_exact_frozen_identity(self):
        with patch.object(m, "TARGET_DATE", TARGET), patch.object(m, "global_dca", return_value=None), \
             patch.object(m, "config_hash", return_value="config"), patch.object(m, "market_risk_summary", return_value={}):
            snapshot = m.build_snapshot({}, [], {})
        self.assertEqual({k: snapshot[k] for k in IDENTITY}, IDENTITY)
        with patch.dict(os.environ, {"MM_SOURCE_SHA": "", "MM_INPUT_HASH": "", "MM_BUILD_ID": ""}):
            self.assertEqual(rt.build_identity(), {k: "" for k in IDENTITY})

    def test_notification_explicitly_awaits_publish_and_binds_identity(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0}
        with patch.dict(os.environ, {"SERVERCHAN_KEY": "test-key"}), patch.object(m.requests, "post", return_value=response) as post:
            m.push_serverchan({}, [{"symbol": "AAA", "level": "red", "price": 12}],
                             snapshot={**IDENTITY, "target_trade_date": TARGET})
        data = post.call_args.kwargs["data"]
        self.assertIn("已生成待发布", data["title"])
        for value in ("本轮已生成待发布", IDENTITY["build_id"], TARGET, "可能仍为上一版"):
            self.assertIn(value, data["desp"])
        self.assertNotIn("已发布", data["desp"])

    def ensure_mocks(self, minute=30, hour=0, runs=None):
        instant = datetime(2026, 10, 20, hour, minute, tzinfo=timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        self.stack.enter_context(patch.object(m, "datetime", Clock))
        self.stack.enter_context(patch.object(m, "TARGET_DATE", ""))
        plan = self.stack.enter_context(patch.object(m, "plan_run", return_value={"target": TARGET, "closed_only": True}))
        response = Mock()
        response.json.return_value = {"workflow_runs": runs or []}
        get = self.stack.enter_context(patch.object(m.requests, "get", return_value=response))
        post = self.stack.enter_context(patch.object(m.requests, "post", return_value=Mock()))
        return plan, get, post

    def test_busy_skip_records_candidate_not_target_proof(self):
        for state in ("queued", "in_progress", "waiting", "pending"):
            with self.subTest(state=state), ExitStack() as local:
                original = self.stack
                self.stack = local
                _, get, post = self.ensure_mocks(runs=[{"id": 789, "created_at": "2026-10-20T00:00:00Z", "status": state}])
                self.stack = original
                self.assertEqual(m.ensure_schedule_run(), 0)
                receipt = self.read("run-receipt.json")
                self.assertEqual(receipt["result"], "skipped")
                self.assertEqual(receipt["reason"], "candidate_run_busy")
                self.assertEqual(receipt["candidate_run_id"], 789)
                self.assertEqual(receipt["candidate_status"], state)
                self.assertFalse(receipt["candidate_target_verified"])
                self.assertEqual(receipt["target_trade_date"], TARGET)
                post.assert_not_called()
                get.assert_called_once()

    def test_outside_window_still_skips_without_network_or_plan(self):
        plan, get, post = self.ensure_mocks(hour=19, minute=0)   # 美东 15:00，盘中，不在 20:00–24:00 窗口
        self.assertEqual(m.ensure_schedule_run(), 0)
        plan.assert_not_called()
        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(self.read("run-receipt.json")["reason"], "outside_schedule_window")

    def test_existing_eighty_percent_snapshot_skips_without_network(self):
        _, get, post = self.ensure_mocks()
        rt.atomic_write_json(self.base / "status.json", {"target_trade_date": TARGET, "mode": "closed",
            "summary": {"total": 100, "today_prices": 80, "prior_prices": 20, "missing_prices": 0}})
        self.assertEqual(m.ensure_schedule_run(), 0)
        get.assert_not_called()
        post.assert_not_called()
        receipt = self.read("run-receipt.json")
        self.assertEqual(receipt["reason"], "target_already_fresh")
        self.assertEqual((receipt["fresh"], receipt["today"], receipt["expected"]), (True, 80, 100))

    def test_dispatch_request_logic_unchanged_and_not_generated(self):
        _, _, post = self.ensure_mocks(runs=[{"id": 1, "created_at": "2026-10-19T20:00:00Z", "status": "queued"}])
        self.assertEqual(m.ensure_schedule_run(), 0)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"ref": "main", "inputs": {"source": "schedule-fallback"}})
        receipt = self.read("run-receipt.json")
        self.assertEqual(receipt["result"], "skipped")
        self.assertEqual(receipt["reason"], "dispatch_requested")

    def test_ensure_api_failure_records_target_in_failed_receipt(self):
        _, get, post = self.ensure_mocks()
        get.side_effect = RuntimeError("模拟 API 失败")
        with self.assertRaises(RuntimeError):
            m.main(["--ensure-schedule-run"])
        receipt = self.read("run-receipt.json")
        self.assertEqual(receipt["result"], "failed")
        self.assertEqual(receipt["target_trade_date"], TARGET)
        self.assertEqual({k: receipt[k] for k in IDENTITY}, IDENTITY)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
