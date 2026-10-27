"""共享取数预算、无请求不扣额度、同进程重跑隔离；全部离线。"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m
import earnings_sources


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Session:
    def __init__(self, clock, durations=None):
        self.headers = {}
        self.calls = []
        self.clock = clock
        self.durations = iter(durations or [])

    def get(self, url, timeout):
        self.calls.append((url, timeout, self.clock()))
        self.clock.advance(next(self.durations, 0))
        text = 'User-agent: *\nAllow: /' if url == m.CONSENSUS_ROBOTS else (
            'Targets:{source:"spg",currency:"USD",avg:123}')
        return SimpleNamespace(status_code=200, text=text)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("RUN_DEADLINE", None), ("QUOTE_DEADLINE", None),
                            ("_REQUEST_TLS", threading.local()), ("_QUOTE_TLS", threading.local())):
            self.stack.enter_context(patch.object(m, name, value))
        self.stack.enter_context(patch.object(m, "log"))
        for name in ("_EARN_CACHE", "_SA_EARN_CACHE", "_YAHOO_EARN_CACHE"):
            self.stack.enter_context(patch.dict(getattr(m, name), {}, clear=True))
        self.network = self.stack.enter_context(patch.object(
            m.requests.Session, "request", side_effect=AssertionError("测试禁止联网")))

    def test_subsecond_budget_never_rounds_up_and_nested_scope_restores(self):
        clock = Clock(10)
        with m.request_budget(10.06, clock):
            self.assertAlmostEqual(m.request_timeout(8), 0.03)
            with m.request_budget(100, clock):
                self.assertAlmostEqual(m.quote_timeout(8), 0.03)
            clock.advance(0.06)
            with self.assertRaises(m.BudgetExhausted):
                m.request_timeout(8)
        self.assertEqual(m.request_timeout(8), 8)
        self.network.assert_not_called()

    def test_http_wrapper_bounds_all_gets_and_restores_after_exception(self):
        clock = Clock(10)
        original = m.requests.Session.request
        original.side_effect = None
        with self.assertRaisesRegex(ValueError, "模拟失败"):
            with m.request_budget(12, clock), m.bounded_http_requests():
                session = m.requests.Session()
                session.get("https://example.invalid/fixture", timeout=25)
                self.assertEqual(original.call_args.kwargs["timeout"], 1)
                clock.advance(2)
                with self.assertRaises(m.BudgetExhausted):
                    session.get("https://example.invalid/fixture", timeout=25)
                self.assertEqual(original.call_count, 1)
                session.post("https://example.invalid/fixture", timeout=25)
                self.assertEqual(original.call_args.kwargs["timeout"], 25)
                raise ValueError("模拟失败")
        self.assertIs(m.requests.Session.request, original)

    def test_consensus_deadline_prevents_even_robots_and_preserves_old_value(self):
        clock = Clock(10)
        old = {"s": "ok", "avg": 123, "r": "2026-09-01", "r_tz": "America/New_York"}
        session = Session(clock)
        data, stat = m.update_consensus(["AAPL"], {"data": {"AAPL": old}}, "2026-10-27",
                                        sess=session, clock=clock, deadline=10)
        self.assertEqual(data["AAPL"], old)
        self.assertEqual((stat["tried"], stat["stopped"], stat["deferred"]), (0, "budget", 1))
        self.assertEqual(stat["quota"]["symbols"], [])
        self.assertEqual(session.calls, [])

    def test_consensus_robots_consumes_shared_stage_budget(self):
        clock = Clock(10)
        session = Session(clock, [5])
        data, stat = m.update_consensus(["AAPL"], None, "2026-10-27", sess=session,
                                        clock=clock, deadline=15)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1], 2.5)
        self.assertEqual((data, stat["tried"], stat["stopped"]), ({}, 0, "budget"))
        self.assertEqual(stat["quota"]["symbols"], [])

    def test_consensus_does_not_sleep_or_charge_for_unstarted_next_request(self):
        clock = Clock()
        session = Session(clock, [0, 3])
        sleep = Mock(side_effect=clock.advance)
        data, stat = m.update_consensus(["AAPL", "MSFT"], None, "2026-10-27", sess=session,
                                        sleep=sleep, clock=clock, deadline=4)
        self.assertEqual(set(data), {"AAPL"})
        self.assertEqual(stat["quota"]["symbols"], ["AAPL"])
        self.assertEqual((stat["tried"], stat["deferred"], stat["stopped"]), (1, 1, "budget"))
        sleep.assert_not_called()
        self.assertEqual(clock(), 3)

    def test_request_boundary_expiry_does_not_charge_retry_quota(self):
        clock = Clock()
        old = {"s": "fail", "avg": 90, "r": "2026-09-01", "try": "2026-10-26"}
        with patch.object(m, "fetch_consensus_one", side_effect=m.BudgetExhausted("尚未请求")):
            data, stat = m.update_consensus(["AAPL"], {"data": {"AAPL": old}}, "2026-10-27",
                                            sess=Session(clock), clock=clock, deadline=10)
        self.assertEqual(data["AAPL"], old)
        self.assertEqual((stat["tried"], stat["fail"]), (0, 0))
        self.assertEqual(stat["quota"]["symbols"], [])
        self.assertEqual(stat["quota"]["retry_symbols"], [])

    def test_earnings_expired_budget_keeps_original_fetched_date(self):
        old = {"status": "ok", "date": "2026-11-10", "kind": "estimated", "fetched": "2026-09-01"}
        with patch.object(m, "verified_earnings_events", return_value={}), \
                patch.object(m, "resolve_earnings") as resolve:
            stat = m.attach_earnings([{"symbol": "AAPL", "group": "position"}], {"AAPL": old},
                                     "2026-10-27", deadline=10, clock=Clock(10))
        resolve.assert_not_called()
        self.assertEqual(stat["data"]["AAPL"], old)
        self.assertEqual((stat["fetched"], stat["deferred"], stat["stopped"]), (0, 1, "budget"))

    def test_earnings_queued_tasks_skip_after_deadline(self):
        clock = Clock()
        def first(*args):
            clock.advance(6)
            return {"status": "ok", "date": "2026-11-10", "kind": "expected"}
        with patch.object(m, "verified_earnings_events", return_value={}), \
                patch.object(m, "resolve_earnings", side_effect=first) as resolve, \
                patch.object(m, "ThreadPoolExecutor") as pool:
            pool.return_value.__enter__.return_value.map.side_effect = lambda fn, values: map(fn, values)
            stat = m.attach_earnings([{"symbol": s, "group": "position"} for s in ("AAPL", "MSFT", "NVDA")],
                                     today="2026-10-27", deadline=5, clock=clock)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual((stat["fetched"], stat["deferred"]), (1, 2))
        self.assertEqual(set(stat["data"]), {"AAPL"})

    def test_macro_and_reference_do_not_request_or_sleep_when_budget_expired(self):
        with m.request_budget(0, Clock()), patch.object(m, "fred_series") as fred, \
                patch.object(m, "financial_conditions_series") as nfci, \
                patch.object(m, "market_breadth") as breadth, patch.object(m.time, "sleep") as sleep:
            macro = m.build_macro()
            previous = {"assessed_at": "2026-09-01T00:00:00+08:00"}
            ref = m.build_reference_alerts(previous)
        for mock in (fred, nfci, breadth, sleep):
            mock.assert_not_called()
        self.assertTrue(all(not item["ok"] for item in macro.values()))
        self.assertEqual(ref["assessed_at"], previous["assessed_at"])
        self.assertEqual(ref["deferred"], "budget")

    def test_official_deadline_uses_injected_clock_before_session_creation(self):
        with patch.object(earnings_sources.requests, "Session") as session:
            result = earnings_sources.fetch_official_earnings("COST", "2026-10-27",
                                                              deadline=5, clock=Clock(5))
        session.assert_not_called()
        self.assertEqual(result["reason"], "budget_exhausted")

    def test_reset_clears_all_source_caches_and_sessions_not_now(self):
        sessions = [Mock(), Mock(), Mock()]
        now = m.NOW
        with patch.dict(m._EARN_CACHE, {"AAPL": {"status": "unknown"}}, clear=True), \
                patch.dict(m._SA_EARN_CACHE, {"AAPL": {"status": "unknown"}}, clear=True), \
                patch.dict(m._YAHOO_EARN_CACHE, {"AAPL": {"status": "unknown"}}, clear=True), \
                patch.object(m, "_EARN_TLS", SimpleNamespace(s=sessions[0])), \
                patch.dict(m._SA_EARN_SESSION, {"sess": sessions[1], "t": 5}, clear=True), \
                patch.dict(m._YAHOO_CRUMB, {"session": sessions[2], "crumb": "fixture", "t": 5}, clear=True):
            m.reset_run_caches()
            self.assertEqual((m._EARN_CACHE, m._SA_EARN_CACHE, m._YAHOO_EARN_CACHE), ({}, {}, {}))
            self.assertIsNone(m._SA_EARN_SESSION["sess"])
            self.assertIsNone(m._YAHOO_CRUMB["session"])
            self.assertIsNone(m._YAHOO_CRUMB["crumb"])
        for session in sessions:
            session.close.assert_called_once()
        self.assertIs(m.NOW, now)

    def test_main_shared_deadline_preserves_render_reserve_and_allows_clean_rerun(self):
        clock = Clock(100)
        target = "2026-10-27"
        old = {"status": "ok", "date": "2026-11-10", "kind": "estimated", "fetched": "2026-09-01"}
        previous = {"earnings": {"data": {"AAPL": old}}, "consensus": {"data": {"AAPL": {
            "s": "ok", "avg": 123, "r": "2026-09-01", "r_tz": "America/New_York"}}}}
        runtime = Mock()
        runtime.load_previous.return_value = previous
        runtime.checkpoint.side_effect = lambda section, data: data
        seen = []
        def macro(*args):
            self.assertEqual((m._EARN_CACHE, m._SA_EARN_CACHE, m._YAHOO_EARN_CACHE), ({}, {}, {}))
            seen.append(m.RUN_DEADLINE - clock())
            m._SA_EARN_CACHE["AAPL"] = {"status": "unknown"}
            clock.advance(120)
            return {}
        def quotes(symbols):
            self.assertLessEqual(m.QUOTE_DEADLINE, m.RUN_DEADLINE)
            self.assertLessEqual(m.QUOTE_DEADLINE - clock(), m.QUOTE_BUDGET_SECONDS)
            clock.now = m.RUN_DEADLINE
            return {"AAPL": m.QUOTE_EXHAUSTED}
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            Path(folder, "holdings.json").write_text(json.dumps({"positions": {"AAPL": {}}}), encoding="utf-8")
            replacements = {
                "BASE": folder, "NOW": datetime(2026, 10, 27, 20, tzinfo=m.TZ),
                "TARGET_DATE": target, "CLOSED_ONLY": True, "EVENT": "workflow_dispatch",
            }
            for name, value in replacements.items():
                stack.enter_context(patch.object(m, name, value))
            for name, kwargs in {
                "RuntimeState": {"return_value": runtime},
                "build_macro": {"side_effect": macro},
                "build_reference_alerts": {"return_value": {}},
                "collect_quotes": {"side_effect": quotes},
                "verified_earnings_events": {"return_value": {}},
                "resolve_earnings": {},
                "_position_symbols": {"return_value": []},
                "build_snapshot": {"return_value": {"actual_dates": {}, "summary": {}}},
                "snapshot_freshness": {"return_value": (False, 0, 1)},
                "render": {"return_value": "离线页面"},
                "write_artifacts": {},
                "push_serverchan": {},
            }.items():
                stack.enter_context(patch.object(m, name, **kwargs))
            stack.enter_context(patch.object(m.time, "monotonic", side_effect=clock))
            original = m.requests.Session.request
            for _ in range(2):
                self.assertEqual(m.main([]), 0)
                self.assertIsNone(m.RUN_DEADLINE)
                self.assertIsNone(m.QUOTE_DEADLINE)
                self.assertIs(m.requests.Session.request, original)
                snapshot = m.write_artifacts.call_args.args[2]
                self.assertEqual(snapshot["earnings"]["data"]["AAPL"]["fetched"], old["fetched"])
                self.assertEqual(snapshot["consensus"]["stat"]["stopped"], "budget")
                self.assertEqual(snapshot["consensus"]["quota"]["symbols"], [])
                m.resolve_earnings.assert_not_called()
            self.assertEqual(m.render.call_count, 2)
        self.assertEqual(seen, [m.RUN_BUDGET_SECONDS - m.FINALIZE_RESERVE_SECONDS] * 2)
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
