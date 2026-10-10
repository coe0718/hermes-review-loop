"""#219: a seat out of its usage window waits for the reset instead of failing.

Subscription seats (Codex, a Claude subscription, other OAuth providers) share the operator's plan.
Before this, a 429 was an ordinary non-zero exit: four backed-off retries over about half an hour,
then ``failed``, against a window that reopens hours later. Now the proxy reads when the window
reopens, the worker holds the account until then without spending a retry, a new turn for that
account is not launched into the closed window, and ``seats.<seat>.daily_turns`` can cap a day.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import http.server
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import (config, gate, gh, inference_proxy, pacing, run_supervisor,  # noqa: E402
                         seat_model, trusted_turn)
from diaktoros.inference_proxy import PATH, InferenceCapability, _UnixHTTP  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

HEAD = "a" * 40
NOW = 1_000_000.0


class ParseReset(unittest.TestCase):
    def test_every_way_a_provider_says_when(self):
        cases = {
            "Retry-After seconds": ({"Retry-After": "120"}, b"", 120),
            "OpenAI duration": ({"x-ratelimit-reset-requests": "6m0s"}, b"", 360),
            "Anthropic RFC 3339": ({"anthropic-ratelimit-requests-reset": "1970-01-12T13:50:00Z"},
                                   b"", 200),
            "Anthropic epoch": ({"anthropic-ratelimit-unified-reset": str(int(NOW + 3600))}, b"", 3600),
            "Codex body": ({}, b'{"error": {"type": "usage_limit_reached", "resets_in_seconds": 5400}}',
                           5400),
            "Codex body, absolute": ({}, json.dumps({"error": {"resets_at": NOW + 900}}).encode(), 900),
            "Codex header": ({"x-codex-primary-reset-after-seconds": "90"}, b"", 90),
            "Retry-After is the server's own answer":
                ({"Retry-After": "60", "x-ratelimit-reset-tokens": "10m"}, b"", 60),
        }
        for name, (headers, body, want) in cases.items():
            with self.subTest(name):
                self.assertAlmostEqual(pacing.parse_reset(headers, body, NOW) - NOW, want, delta=1)

    def test_the_hold_is_the_full_windows_not_the_latest_one(self):
        # Live, 2026-10-10: Codex's 5-hour window was full (resets in 47 m) and its weekly one had
        # 84% left (resets in 6 days); the reviewer was held until the weekly reset.
        five_h, week = 47 * 60, 6 * 24 * 3600
        codex = {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-after-seconds": str(five_h),
                 "x-codex-secondary-used-percent": "16", "x-codex-secondary-reset-after-seconds": str(week)}
        cases = {
            "the full 5-hour window": (codex, b"", five_h),
            "the full weekly window": ({**codex, "x-codex-primary-used-percent": "40",
                                        "x-codex-secondary-used-percent": "100"}, b"", week),
            "the body names the limit it hit": (codex, json.dumps({"error": {
                "type": "usage_limit_reached", "resets_in_seconds": 1234}}).encode(), 1234),
            "no usage given: the earliest reset": ({"x-codex-primary-reset-after-seconds": str(five_h),
                                                    "x-codex-secondary-reset-after-seconds": str(week)},
                                                   b"", five_h),
            "Anthropic: the window with nothing remaining": (
                {"anthropic-ratelimit-tokens-remaining": "0",
                 "anthropic-ratelimit-tokens-reset": str(int(NOW + 7200)),
                 "anthropic-ratelimit-requests-remaining": "40",
                 "anthropic-ratelimit-requests-reset": str(int(NOW + 60))}, b"", 7200),
            "OpenAI: the window with nothing remaining": (
                {"x-ratelimit-remaining-requests": "12", "x-ratelimit-reset-requests": "20s",
                 "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "9m"}, b"", 540),
        }
        for name, (headers, body, want) in cases.items():
            with self.subTest(name):
                self.assertAlmostEqual(pacing.parse_reset(headers, body, NOW) - NOW, want, delta=1)

    def test_nothing_usable_is_none_and_a_far_future_is_capped(self):
        for headers, body in (({}, b""), ({"content-type": "application/json"}, b'{"error": "x"}'),
                              ({"Retry-After": "0"}, b""), ({"Retry-After": "soon"}, b""),
                              ({}, b"not json")):
            with self.subTest(headers=headers, body=body):
                self.assertIsNone(pacing.parse_reset(headers, body, NOW))
        far = pacing.parse_reset({"Retry-After": str(30 * 24 * 3600)}, b"", NOW)
        self.assertEqual(far, NOW + pacing.MAX_WAIT_S)


class Store(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_hold_lasts_until_its_time_and_the_later_hold_wins(self):
        key = pacing.account_key("openai-codex", "https://chatgpt.com/x", "critic")
        self.assertIsNone(pacing.held(key))
        pacing.hold(key, time.time() + 600, "first")
        pacing.hold(key, time.time() + 60, "earlier, ignored")
        until, reason = pacing.held(key)
        self.assertAlmostEqual(until - time.time(), 600, delta=5)
        self.assertEqual(reason, "first")
        self.assertIsNone(pacing.held(key, now=time.time() + 601))
        self.assertIsNone(pacing.held(pacing.account_key("openai-codex", "https://chatgpt.com/x",
                                                         "coder")), "another profile's window")

    def test_turns_are_counted_per_loop_seat_and_day(self):
        self.assertEqual(pacing.turns_today("w", "reviewer"), 0)
        self.assertEqual(pacing.count_turn("w", "reviewer"), 1)
        self.assertEqual(pacing.count_turn("w", "reviewer"), 2)
        self.assertEqual(pacing.turns_today("w", "fixer"), 0)
        self.assertEqual(pacing.turns_today("other", "reviewer"), 0)
        self.assertEqual(pacing.turns_today("w", "reviewer", now=time.time() + 2 * 86400), 0)
        midnight = pacing.next_midnight()
        self.assertTrue(time.time() < midnight <= time.time() + 86400 + 3600)

    def test_an_unreadable_file_is_no_hold_never_a_stop(self):
        pacing.path().parent.mkdir(parents=True, exist_ok=True)
        pacing.path().write_text("{not json")
        self.assertIsNone(pacing.held("k"))
        self.assertEqual(pacing.turns_today("w", "reviewer"), 0)
        pacing.hold("k", time.time() + 60, "r")            # and it recovers on the next write
        self.assertIsNotNone(pacing.held("k"))


class ProxyReadsTheReset(unittest.TestCase):
    def serve(self, status, headers, body):
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}{PATH}"

    def call(self, upstream, calls=1):
        with tempfile.TemporaryDirectory() as d:
            with InferenceCapability(pathlib.Path(d) / "cap", upstream, "KEY", model="m",
                                     quota=1) as cap:
                answers = []
                for _ in range(calls):
                    conn = _UnixHTTP(str(cap.socket_path))
                    conn.request("POST", PATH, body=b'{"messages": []}')
                    response = conn.getresponse()
                    answers.append((response.status, response.read()))
                    conn.close()
                return cap.rate_limited_until, answers

    def test_a_provider_429_records_its_reset_and_is_relayed_unchanged(self):
        body = b'{"error": {"type": "usage_limit_reached", "resets_in_seconds": 5400}}'
        until, answers = self.call(self.serve(429, {"Retry-After": "60"}, body))
        self.assertAlmostEqual(until - time.time(), 5400, delta=10)
        self.assertEqual(answers, [(429, body)])

    def test_a_bare_429_is_not_guessed_into_a_window_and_success_records_nothing(self):
        # No reset named: an ordinary failure, left to the ordinary backoff (often a per-minute
        # limit, which 2m covers better than any guessed window).
        until, answers = self.call(self.serve(429, {}, b'{"error": "slow down"}'))
        self.assertIsNone(until)
        self.assertEqual(answers[0][0], 429)
        until, _ = self.call(self.serve(200, {}, b'{"ok": true}'))
        self.assertIsNone(until)

    def test_the_proxys_own_quota_refusal_is_not_the_providers_window(self):
        until, answers = self.call(self.serve(200, {}, b'{"ok": true}'), calls=2)
        self.assertEqual([status for status, _ in answers], [200, 429])
        self.assertIsNone(until)


class ProductionWorker(unittest.TestCase):
    """The real ``_run_production``, its turn faked, as test_turn_budget drives it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name)
        runtime = self.home / "diaktoros-runtime.json"
        runtime.write_text(json.dumps({"source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}))
        runtime.chmod(0o600)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "DIAKTOROS_CONFIG_DIR": str(self.home / "none")})
        patch.start()
        self.addCleanup(patch.stop)
        self.raw = {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"],
                    "reviewers": ["reviewer"], "read_token": "reader",
                    "seats": {"reviewer": {"profile": "rev", "route": "r"},
                              "fixer": {"profile": "fix", "route": "f"}},
                    "state_dir": str(self.home / "state"), "unattended_fixer_push": True}
        self.loop = config.normalize(self.raw)
        self.inference = mock.Mock(upstream="https://chatgpt.com/backend-api/codex/responses",
                                   provider="openai-codex", profile="fix", key="k", model="m",
                                   api_mode="codex_responses", proxy_model="", client_identity="")
        self.account = pacing.account_key("openai-codex", self.inference.upstream, "fix")
        self.turns = 0

    def run_production(self, run_turn, loop=None, seat="fixer", author="fixer"):
        loop = loop or self.loop
        with mock.patch.object(Supervisor, "_spawn"), \
                mock.patch.object(config, "by_repo", return_value=loop):
            gate.enqueue_isolated(loop, seat, 8, HEAD)
        sup = Supervisor(self.home / "state" / "diaktoros-runs.sqlite",
                         production_config=self.home / "diaktoros-runtime.json",
                         hermes_home=self.home)
        row = sup.get(f"acme/widgets:8:{HEAD}:{seat}")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='launching', owner='w', launch_intent=1, "
                        "generation=COALESCE(NULLIF(generation, ''), 'g1') WHERE id=?",
                        (row["id"],))

        def counted(*args, **kwargs):
            self.turns += 1
            return run_turn(*args, **kwargs)
        pr = {"number": 8, "head": {"sha": HEAD, "ref": "fix-8"}, "user": {"login": author}}
        with mock.patch.object(config, "by_repo", return_value=loop), \
                mock.patch.object(seat_model, "load_runtime", return_value={
                    "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
                mock.patch.object(seat_model, "resolve_seat", return_value=self.inference), \
                mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=[]), \
                mock.patch.object(gh, "review_state", return_value="CHANGES_REQUESTED"), \
                mock.patch("diaktoros.gate.latest_effective_review_at_head", return_value={}), \
                mock.patch.object(run_supervisor, "isolated_prompt", return_value="PROMPT"), \
                mock.patch.object(run_supervisor, "pr_change", return_value=None), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=counted), \
                mock.patch.object(sup, "recover"):
            sup._run_production(row["id"], "w")
        return sup.get(f"acme/widgets:8:{HEAD}:{seat}")

    def test_a_429d_turn_waits_for_the_reset_without_spending_a_retry(self):
        reset = time.time() + 3600

        def rate_limited(_loop, scope, observed=None, **kw):
            observed.update(rate_limited_until=reset)
            return 1
        row = self.run_production(rate_limited)
        self.assertEqual((row["state"], row["retries"]), ("waiting", 0))
        self.assertAlmostEqual(row["retry_at"], reset, delta=2)
        self.assertTrue(row["error"].startswith("held: fixer usage window (openai-codex) — resumes "))
        self.assertIn("no retry spent", run_supervisor.next_step(row))
        self.assertIsNotNone(pacing.held(self.account), "the account is held for the next turn")

    def test_a_429d_turn_that_wrote_is_not_paced(self):
        """Pacing is only for a run that wrote nothing: with a write-ahead record present the
        run takes the existing path, and is never parked for a relaunch into a second write."""
        reset = time.time() + 3600

        def wrote_then_limited(_loop, scope, observed=None, **kw):
            with closing(sqlite3.connect(self.home / "state" / "diaktoros-runs.sqlite")) as con, con:
                rid = con.execute("SELECT id FROM runs").fetchone()[0]
                con.execute("INSERT INTO issue_fixes(run_id,repo,number,base,kind,branch,state,"
                            "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                            (rid, "acme/widgets", 8, HEAD, "fix",
                             "fix-8", "pushing", time.time(), time.time()))
            observed.update(rate_limited_until=reset)
            return 1
        row = self.run_production(wrote_then_limited)
        self.assertEqual(self.turns, 1)
        self.assertNotEqual(row["state"], "waiting")
        self.assertIsNone(row["retry_at"])
        self.assertEqual(row["state"], "failed")

    def test_a_closed_window_is_not_launched_into(self):
        pacing.hold(self.account, time.time() + 1800, "earlier 429")
        row = self.run_production(lambda *a, **k: self.fail("launched into a closed window"))
        self.assertEqual((row["state"], row["retries"], self.turns), ("waiting", 0, 0))
        self.assertAlmostEqual(row["retry_at"] - time.time(), 1800, delta=5)

    def test_the_daily_cap_holds_until_midnight(self):
        raw = {**self.raw, "seats": {**self.raw["seats"],
                                     "fixer": {**self.raw["seats"]["fixer"], "daily_turns": 1}}}
        loop = config.normalize(raw)
        pacing.count_turn("widgets", "fixer")                  # today's one turn is spent
        row = self.run_production(lambda *a, **k: self.fail("launched past the daily cap"), loop)
        self.assertEqual((row["state"], row["retries"], self.turns), ("waiting", 0, 0))
        self.assertAlmostEqual(row["retry_at"], pacing.next_midnight(), delta=2)
        self.assertIn("daily turn cap (1) reached", row["error"])

    def review_only_loop(self, daily=1):
        return config.normalize({**self.raw, "review_only": ["owner-human"],
                                 "review_only_daily": daily})

    def test_review_only_daily_holds_the_next_review_only_turn(self):
        loop = self.review_only_loop()
        pacing.count_turn("widgets", pacing.REVIEW_ONLY_SEAT)      # today's one turn is spent
        row = self.run_production(lambda *a, **k: self.fail("launched past the cap"), loop,
                                  seat="reviewer", author="owner-human")
        self.assertEqual((row["state"], row["retries"], self.turns), ("waiting", 0, 0))
        self.assertAlmostEqual(row["retry_at"], pacing.next_midnight(), delta=2)
        self.assertIn("review-only daily turn cap (1) reached", row["error"])

    def test_review_only_daily_counts_review_only_turns_and_leaves_a_fixer_pr_alone(self):
        loop = self.review_only_loop()
        pacing.count_turn("widgets", pacing.REVIEW_ONLY_SEAT)
        row = self.run_production(lambda *a, **k: 1, loop, seat="reviewer", author="fixer")
        self.assertEqual(self.turns, 1, "a fixer PR's review is not held by the review-only cap")
        self.assertEqual(pacing.turns_today("widgets", pacing.REVIEW_ONLY_SEAT), 1)
        loop = self.review_only_loop(daily=5)
        self.run_production(lambda *a, **k: 1, loop, seat="reviewer", author="owner-human")
        self.assertEqual(pacing.turns_today("widgets", pacing.REVIEW_ONLY_SEAT), 2)

    def test_an_ordinary_failure_still_backs_off_and_counts(self):
        row = self.run_production(lambda *a, **k: 1)
        self.assertEqual((row["state"], row["retries"]), ("waiting", 1))
        self.assertAlmostEqual(row["retry_at"] - time.time(), run_supervisor.backoff(1), delta=5)
        self.assertIsNone(pacing.held(self.account))
        self.assertEqual(pacing.turns_today("widgets", "fixer"), 1, "a launched turn is counted")


class Config(unittest.TestCase):
    def raw(self, **fixer):
        return {"id": "w", "repo": "a/b", "fixers": ["f"], "reviewers": ["r"], "read_token": "x",
                "seats": {"reviewer": {"profile": "p", "route": "r"},
                          "fixer": {"profile": "q", "route": "f", **fixer}}}

    def test_daily_turns_is_a_bounded_whole_number(self):
        self.assertEqual(config.seat_daily_turns(config.normalize(self.raw(daily_turns=5)), "fixer"), 5)
        self.assertIsNone(config.seat_daily_turns(config.normalize(self.raw()), "fixer"))
        for bad in (0, -1, True, "5", 1.5, config.DAILY_TURNS_MAX + 1):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.normalize(self.raw(daily_turns=bad))


if __name__ == "__main__":
    unittest.main()
