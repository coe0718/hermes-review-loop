"""Issue #54 (and #78): a watchdog that cannot read GitHub must say so, not go silent.

An unreadable hook list used to read as "paused": no lines, exit 0 — exactly what a deliberate
pause looks like. These pin the three states apart, the alert's cadence (it re-fires every
cooldown, unlike the stall map's #77), the token-expiry warning, and the gate's failed read
that is now left on disk for the next sweep and ``explain`` instead of only the gateway's stderr.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import gate, gh, state as state_mod  # noqa: E402
from scripts import watchdog  # noqa: E402

OK = gh.Response({"login": "rev-coach"}, "", 200, {})
DEAD = gh.Response(None, 'HTTP 401 {"message":"Bad credentials"}', 401, {})
DOWN = gh.Response(None, "HTTP 502 bad gateway", 502, {})


def expiring(days: float) -> gh.Response:
    when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + days * 86400))
    return gh.Response({"login": "rev-coach"}, "", 200, {gh.TOKEN_EXPIRY_HEADER: when})


class Health(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = {"id": "widgets", "repo": "acme/widgets", "read_token": "rev-coach",
                     "state_dir": str(pathlib.Path(self.tmp.name) / "state"), "cooldown_h": 6,
                     "ttl_min": 45, "inflight_ttl_min": 10,
                     "seats": {"reviewer": {"route": "widgets-review"},
                               "fixer": {"route": "widgets-fix"}}}
        self.st = state_mod.state_for(self.loop)
        self.now = time.time()

    def sweep(self, hooks=(None, "HTTP 401 Bad credentials"), probe=DEAD, at=None):
        with mock.patch.object(watchdog, "TEST", False), \
             mock.patch.object(watchdog.gate, "hooks_read", return_value=hooks), \
             mock.patch.object(watchdog.gh, "auth_probe", return_value=probe), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]) as heal, \
             mock.patch.object(watchdog.gh, "open_prs", return_value=None) as listing, \
             mock.patch.object(watchdog.observer, "retry", return_value=0) as retry, \
             mock.patch.object(watchdog.observer, "flush"), \
             mock.patch.object(watchdog.time, "time", return_value=at or self.now):
            lines = watchdog.sweep_loop(self.loop, self.st)
        return lines, heal, listing, retry

    def test_unreadable_hooks_alert_with_login_and_status_and_keep_non_github_work(self):
        lines, heal, listing, retry = self.sweep()
        alert = [line for line in lines if "cannot read GitHub" in line]
        self.assertEqual(len(alert), 1, lines)
        self.assertIn("as rev-coach: HTTP 401", alert[0])
        self.assertIn("token expired or revoked?", alert[0])
        self.assertIn("stall scan and queue drain are skipped", alert[0])
        heal.assert_called_once()           # route self-heal needs no GitHub read
        retry.assert_called_once()          # nor do the observer's retries
        listing.assert_not_called()         # unknown hooks: fail closed on scan and drain
        self.assertEqual(self.st.watch()["github_read"]["status"], 401)

    def test_paused_is_still_silent_and_reads_nothing_more(self):
        lines, heal, _, _ = self.sweep(hooks=(False, "reviewer, fixer"), probe=DEAD)
        self.assertEqual(lines, [])
        heal.assert_not_called()
        self.assertNotIn("github_read", self.st.watch())

    def test_alert_refires_after_each_cooldown_not_once_ever(self):
        first, *_ = self.sweep()
        stamp = self.st.watch()["github_alerts"]["read:401"]
        again, *_ = self.sweep(at=self.now + 15 * 60)
        self.assertFalse(any("cannot read GitHub" in line for line in again), again)
        # A suppressed sweep must not refresh the clock (the stall map's #77 bug).
        self.assertEqual(self.st.watch()["github_alerts"]["read:401"], stamp)
        later, *_ = self.sweep(at=self.now + 6 * 3600 + 60)
        self.assertTrue(any("cannot read GitHub" in line for line in later), later)
        self.assertIn("3 sweep(s)", [line for line in later if "cannot read" in line][0])

    def test_5xx_alerts_only_after_consecutive_failed_sweeps(self):
        hooks = (None, "HTTP 502 bad gateway")
        seen = [any("cannot read GitHub" in line
                    for line in self.sweep(hooks=hooks, probe=DOWN, at=self.now + i * 900)[0])
                for i in range(watchdog.READ_FAILURE_SWEEPS)]
        self.assertEqual(seen, [False] * (watchdog.READ_FAILURE_SWEEPS - 1) + [True])

    def test_recovery_is_said_once(self):
        self.sweep()
        back, *_ = self.sweep(hooks=(True, ""), probe=OK, at=self.now + 900)
        self.assertTrue(any("GitHub reads work again as rev-coach" in line for line in back), back)
        self.assertNotIn("github_read", self.st.watch())
        quiet, *_ = self.sweep(hooks=(True, ""), probe=OK, at=self.now + 1800)
        self.assertFalse(any("GitHub" in line for line in quiet), quiet)

    def test_readable_hooks_but_dead_user_probe_still_alert(self):
        lines, _, listing, _ = self.sweep(hooks=(True, ""), probe=DEAD)
        self.assertTrue(any("cannot read GitHub as rev-coach: HTTP 401" in line for line in lines))
        listing.assert_called_once()

    def test_expiry_warns_within_the_window_and_daily_after(self):
        lines, *_ = self.sweep(hooks=(True, ""), probe=expiring(3))
        warn = [line for line in lines if "read token for rev-coach" in line]
        self.assertEqual(len(warn), 1, lines)
        self.assertIn("in 3.0 day(s)", warn[0])
        lines, *_ = self.sweep(hooks=(True, ""), probe=expiring(3), at=self.now + 3600)
        self.assertFalse(any("read token" in line for line in lines))
        lines, *_ = self.sweep(hooks=(True, ""), probe=expiring(30))
        self.assertFalse(any("read token" in line for line in lines))

    def test_gate_read_failure_is_surfaced_once_by_the_sweep_and_by_explain(self):
        self.st.github_failure_record({"at": self.now - 60, "where": "gate_reviewer.py",
                                       "method": "GET", "path": "/repos/acme/widgets/pulls/7",
                                       "error": "HTTP 401 Bad credentials", "status": 401,
                                       "login": "rev-coach"})
        lines, *_ = self.sweep(hooks=(True, ""), probe=OK)
        gate_lines = [line for line in lines if "gate_reviewer.py could not GET" in line]
        self.assertEqual(len(gate_lines), 1, lines)
        self.assertIn("explain --loop widgets --pr 7", gate_lines[0])
        lines, *_ = self.sweep(hooks=(True, ""), probe=OK, at=self.now + 900)
        self.assertFalse(any("gate_reviewer.py" in line for line in lines), lines)
        local = gate._explain_state(self.loop, self.st, "acme/widgets#7", 7, "", self.now)
        self.assertIn("gate_reviewer.py GET /repos/acme/widgets/pulls/7", local["github"])


class Reads(unittest.TestCase):
    """The gh layer: statuses, headers, the tri-state hook answer, and the failure record."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.loop = {"id": "w", "repo": "acme/widgets", "read_token": "rev-coach",
                     "state_dir": str(self.tmp / "state"),
                     "seats": {"reviewer": {"route": "r"}, "fixer": {"route": "f"}}}

    def stub(self, body: str) -> None:
        path = self.tmp / "stub.py"
        path.write_text(f"#!{sys.executable}\nprint({body!r})\n")
        path.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"REVIEW_LOOP_GH_STUB": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stub_envelope_carries_status_and_headers(self):
        self.stub('{"__gh_stub_response__": {"status": 200, "body": {"login": "x"}, '
                  '"headers": {"GitHub-Authentication-Token-Expiration": "2026-10-01 00:00:00 UTC"}}}')
        response = gh.auth_probe(self.loop)
        self.assertEqual((response.data, response.error, response.status),
                         ({"login": "x"}, "", 200))
        self.assertEqual(response.headers[gh.TOKEN_EXPIRY_HEADER], "2026-10-01 00:00:00 UTC")

    def test_unreadable_hooks_are_unknown_not_paused_and_failures_are_recorded(self):
        self.stub('{"__gh_stub_response__": {"status": 401, "body": {"message": "Bad credentials"}}}')
        self.assertIsNone(gate.hooks_armed(self.loop))
        self.assertEqual(gh.status_of(gate.hooks_read(self.loop)[1]), 401)
        with mock.patch.object(gh, "_RECORD_FAILURES", False):
            self.assertIsNone(gh.pr(self.loop, 7))
        # Only an opted-in process (a gate) writes it: explain and doctor stay read-only.
        self.assertEqual(state_mod.state_for(self.loop).github_failure(), {})
        with mock.patch.object(gh, "_RECORD_FAILURES", True):
            self.assertIsNone(gh.pr(self.loop, 7))
        failure = state_mod.state_for(self.loop).github_failure()
        self.assertEqual((failure["path"], failure["status"], failure["login"]),
                         ("/repos/acme/widgets/pulls/7", 401, "rev-coach"))

    def test_expiry_header_parses_both_spellings(self):
        self.assertEqual(watchdog.parse_expiry("2026-10-01 00:00:00 UTC"),
                         watchdog.parse_expiry("2026-09-30 17:00:00 -0700"))
        self.assertIsNone(watchdog.parse_expiry("soon"))


if __name__ == "__main__":
    unittest.main()
