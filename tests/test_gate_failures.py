#!/usr/bin/env python3
"""Issue #75: a gate that crashes, overruns, or silences after a failed read is recorded,
alerted and re-driven — never indistinguishable from a deliberate ``[SILENT]``.

Every gate here runs the way the Hermes gateway runs a route script
(``webhook_filters.run_route_script``): ``[sys.executable, script]``, the payload as JSON on
stdin, ``cwd`` = the script's directory, a 30-second timeout.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from review_loop import config, gate, gate_failures, state as state_mod  # noqa: E402

SCRIPTS = t.ROOT / "scripts"


def gateway_run(script: str, payload, extra_env: dict | None = None):
    """The gateway's contract, minus the HTTP: argv, stdin, cwd and its 30s timeout."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(SCRIPTS / script)], input=raw,
                          capture_output=True, text=True, cwd=str(SCRIPTS), timeout=30,
                          env={**t.env(), **(extra_env or {})})
    return proc, time.monotonic() - started


def watchdog() -> str:
    proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")], capture_output=True,
                          text=True, cwd=str(SCRIPTS), timeout=120, env=t.env())
    return proc.stdout


def loop_entries() -> dict:
    return gate_failures.Ledger(t.STATE_DIR).entries()


class GateFailureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})

    def test_malformed_payload_crash_is_recorded_without_a_loop(self):
        proc, _ = gateway_run("gate_reviewer.py", {"repository": "acme/widgets",
                                                   "action": "opened"})
        self.assertEqual(proc.returncode, 2)
        self.assertNotEqual(proc.stdout.strip(), "[SILENT]")
        self.assertIn("GATE FAILURE (crash)", proc.stderr)
        entries = gate_failures.fallback_ledger().entries()
        (entry,) = entries.values()
        self.assertEqual((entry["gate"], entry["kind"], entry["error_type"]),
                         ("gate_reviewer", "crash", "AttributeError"))
        self.assertIn("Traceback", entry["traceback"])
        self.assertLessEqual(len(entry["traceback"]), 4000)
        self.assertIn("gate failure", watchdog())

    def test_crash_is_recorded_alerted_redriven_and_resolved(self):
        broken = t.pr(7, requested=t.SEAT)
        broken["head"] = "not-an-object"          # GitHub answered a shape the gate chokes on
        t.set_prs({"7": broken})
        payload = t.pr_payload(7)
        proc, _ = gateway_run("gate_reviewer.py", payload)
        self.assertEqual(proc.returncode, 2)
        (key, entry), = loop_entries().items()
        self.assertEqual((entry["kind"], entry["repo"], entry["pr"], entry["head"],
                          entry["action"], entry["error_type"]),
                         ("crash", t.REPO, 7, t.HEAD_A, "review_requested", "AttributeError"))
        self.assertTrue(entry["payload_kept"] and entry["redrivable"])
        # explain names it as a blocker.
        loop = config.load_id("widgets")
        failures = gate_failures.open_for(loop, 7)
        self.assertEqual([f["id"] for f in failures], [key])
        self.assertIn(f"gate failure {key}", gate.gate_failure_line(failures[0]))

        # GitHub recovers; the watchdog alerts and re-drives the stored event.
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        out = watchdog()
        self.assertIn(f"gate failure {key}: gate_reviewer crash on #7", out)
        self.assertIn("re-driven — completed", out)
        self.assertTrue(loop_entries()[key]["resolved"])
        self.assertIn("re-driven by the watchdog", loop_entries()[key]["resolution"])
        self.assertEqual(gate_failures.open_for(loop, 7), [])
        self.assertNotIn(f"gate failure {key}", watchdog())   # resolved: said once

    def test_repeat_crash_is_deduped_and_redrives_are_capped(self):
        broken = t.pr(7, requested=t.SEAT)
        broken["head"] = "not-an-object"
        t.set_prs({"7": broken})
        payload = t.pr_payload(7)
        gateway_run("gate_reviewer.py", payload)
        gateway_run("gate_reviewer.py", payload)          # a manual redelivery: same event
        (key, entry), = loop_entries().items()
        self.assertEqual(entry["attempts"], 2)
        for _ in range(gate_failures.MAX_REDRIVES):
            self.assertIn("failed again", watchdog())
        out = watchdog()
        self.assertIn(f"gave up after {gate_failures.MAX_REDRIVES} re-drives", out)
        self.assertEqual(loop_entries()[key]["redrives"], gate_failures.MAX_REDRIVES)

    def test_hanging_github_is_a_recorded_timeout_well_inside_the_gateway_limit(self):
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                    {"REVIEW_LOOP_GH_STUB": str(hang),
                                     "REVIEW_LOOP_GATE_BUDGET_S": "1"})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 10)
        (entry,) = loop_entries().values()
        self.assertEqual((entry["kind"], entry["error_type"]), ("timeout", "GateBudgetExceeded"))

    def test_hang_outside_github_hits_the_backstop(self):
        # A lock another process never releases: not a GitHub read, so only SIGALRM ends it.
        import fcntl
        st = state_mod.LoopState(config.load_id("widgets"))
        st.dir.mkdir(parents=True, exist_ok=True)
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        with open(st.dir / "state.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                        {"REVIEW_LOOP_GATE_BUDGET_S": "1"})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 1 + gate_failures.BACKSTOP_S + 5)
        (entry,) = loop_entries().values()
        self.assertEqual(entry["kind"], "timeout")

    def test_silence_after_failed_read_is_incomplete_not_a_decision(self):
        failing = t.TMP / "fail_stub.py"
        failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('HTTP 502')\nsys.exit(1)\n")
        failing.chmod(0o755)
        proc, _ = gateway_run("gate_reviewer.py", t.pr_payload(7),
                              {"REVIEW_LOOP_GH_STUB": str(failing)})
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "[SILENT]"))
        (key, entry), = loop_entries().items()
        self.assertEqual((entry["kind"], entry["error_type"]), ("incomplete", "GitHubReadFailed"))
        # One owner per failed read (#75 with #54): github-reads.json keeps it as the last failed
        # call, marked as owned by this entry, so the health sweep does not report it again.
        st = state_mod.LoopState(config.load_id("widgets"))
        failure = st.github_failure()
        self.assertEqual(failure.get("owned_by"), f"gate-failures:{key}")
        from unittest import mock
        from scripts import watchdog as wd
        ok = wd.gh.Response({"login": t.REVIEWER}, "", 200, {})
        with mock.patch.object(wd, "TEST", False), \
             mock.patch.object(wd.gh, "auth_probe", return_value=ok):
            quiet = wd.github_health(config.load_id("widgets"), st, {}, time.time(), True, "")
            st.github_failure_record({k: v for k, v in failure.items() if k != "owned_by"})
            loud = wd.github_health(config.load_id("widgets"), st, {}, time.time(), True, "")
        self.assertEqual([line for line in quiet if "could not" in line], [])
        self.assertEqual(len([line for line in loud if "could not" in line]), 1)

    def test_deliberate_silence_records_nothing(self):
        proc, _ = gateway_run("gate_reviewer.py", t.pr_payload(7, action="labeled"))
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "[SILENT]"))
        self.assertEqual(loop_entries(), {})

    def test_adjudicator_failures_are_alerted_never_redriven(self):
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.record("k1", {"gate": "gate_adjudicator", "kind": "crash", "pr": 7,
                             "redrivable": False, "error_type": "X", "error": "y"}, "{}")
        out = watchdog()
        self.assertIn("not re-driven (its output is a dispatch)", out)
        self.assertEqual(ledger.entries()["k1"]["redrives"], 0)

    # -- the gate's budget fits the gateway's script timeout ---------------------------------

    def gateway_config(self, data: dict | None, legacy: dict | None = None) -> None:
        cfg, gw = t.HOME / "config.yaml", t.HOME / "gateway.json"
        for path, value in ((cfg, data), (gw, legacy)):
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(value))       # JSON is valid YAML
        self.addCleanup(cfg.unlink, missing_ok=True)
        self.addCleanup(gw.unlink, missing_ok=True)

    def test_gateway_timeout_is_read_the_way_the_gateway_merges_it(self):
        self.gateway_config(None)
        self.assertEqual(gate_failures.gateway_script_timeout(),
                         (30, "gateway default (not set)"))
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10}}},
                            {"platforms": {"webhook": {"script_timeout_seconds": 90}}})
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 10)   # yaml beats gateway.json
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10,
                                                       "extra": {"script_timeout_seconds": 45}}},
                             "gateway": {"webhook": {"script_timeout_seconds": 12}}})
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 45)   # extra wins
        self.gateway_config({"gateway": {"webhook": {"script_timeout_seconds": 12}}})
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 12)
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": "soon"}}})
        self.assertIsNone(gate_failures.gateway_script_timeout()[0])
        from unittest import mock
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10}}})
        with mock.patch.object(gate_failures, "_read_yaml", side_effect=ValueError):
            timeout, why = gate_failures.gateway_script_timeout()
        self.assertIsNone(timeout)
        self.assertIn("no YAML reader", why)

    def profile_config(self, name: str, data: dict) -> pathlib.Path:
        """A profile home's config.yaml (JSON is valid YAML), restored after the test."""
        home = t.HOME / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        path = home / "config.yaml"
        before = path.read_text() if path.exists() else None
        path.write_text(json.dumps({"model": {"default": "test-model"}, **data}))
        self.addCleanup(lambda: path.write_text(before) if before is not None
                        else path.unlink(missing_ok=True))
        return home

    def doctor_lines(self):
        from review_loop import doctor
        return {c.name: c for c in doctor.check_gate_timeouts(config.load_id("widgets"))}

    def test_doctor_checks_every_profile_gateway_hosting_a_loop_route(self):
        from review_loop import doctor
        # Root (host) gateway: 30s by default. The reviewer's profile runs its own standalone
        # gateway at 10s; the fixer's profile is multiplexed by the host but sets 60s itself.
        self.gateway_config(None)
        rev = self.profile_config("reviewer-profile", {
            "gateway": {"standalone": True},
            "platforms": {"webhook": {"script_timeout_seconds": 10}}})
        self.profile_config("fixer-profile", {
            "platforms": {"webhook": {"script_timeout_seconds": 60}}})
        checks = self.doctor_lines()
        self.assertEqual(sorted(checks), ["gate:timeout:default", "gate:timeout:fixer-profile",
                                          "gate:timeout:reviewer-profile"])
        low = checks["gate:timeout:reviewer-profile"]
        self.assertEqual(low.status, doctor.MISMATCH)
        self.assertIn("serves reviewer", low.detail)
        self.assertIn("profile reviewer-profile's standalone gateway: 10s", low.detail)
        self.assertIn(f"in {rev / 'config.yaml'} for the profile reviewer-profile's standalone "
                      f"gateway", low.fix)
        fixer = checks["gate:timeout:fixer-profile"]
        self.assertEqual(fixer.status, doctor.VERIFIED, fixer.detail)
        self.assertIn("own gateway (if it runs one): 60s", fixer.detail)
        self.assertIn("host gateway multiplexing profile fixer-profile: 30s", fixer.detail)
        self.assertEqual(checks["gate:timeout:default"].status, doctor.VERIFIED)
        self.assertIn("serves adjudicator", checks["gate:timeout:default"].detail)

        # Now the host gateway drops to 20s: the multiplexed fixer profile and the default
        # profile are flagged against the ROOT file; the standalone reviewer is unaffected by it.
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 20}}})
        checks = self.doctor_lines()
        fixer = checks["gate:timeout:fixer-profile"]
        self.assertEqual(fixer.status, doctor.MISMATCH)
        self.assertIn(f"in {t.HOME / 'config.yaml'} for the host gateway", fixer.fix)
        self.assertNotIn("fixer-profile/config.yaml", fixer.fix)
        self.assertEqual(checks["gate:timeout:default"].status, doctor.MISMATCH)
        self.assertNotIn(str(t.HOME / "config.yaml") + " ",
                         checks["gate:timeout:reviewer-profile"].fix)
        self.assertIn("reviewer-profile/config.yaml", checks["gate:timeout:reviewer-profile"].fix)

        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": "x"}}})
        self.assertEqual(self.doctor_lines()["gate:timeout:default"].status, doctor.UNKNOWN)

    def test_gate_reads_the_gateway_it_runs_under(self):
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 45}}})
        solo = self.profile_config("solo", {"gateway": {"standalone": True},
                                            "platforms": {"webhook": {"script_timeout_seconds": 12}}})
        muxed = self.profile_config("muxed", {"platforms": {"webhook": {"script_timeout_seconds": 90}}})
        self.assertEqual(gate_failures.effective_timeout(t.HOME)[0], 45)       # root gateway
        self.assertEqual(gate_failures.effective_timeout(solo)[0], 12)         # its own only
        self.assertEqual(gate_failures.effective_timeout(muxed)[0], 45)        # host is lower
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 200}}})
        self.assertEqual(gate_failures.effective_timeout(muxed)[0], 90)        # own is lower
        # The real gate, run as a standalone profile gateway runs it: HERMES_HOME = the profile.
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                    {"REVIEW_LOOP_GH_STUB": str(hang), "HERMES_HOME": str(solo)})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 12)
        (entry,) = loop_entries().values()
        self.assertAlmostEqual(entry["budget_s"], 4.8)                         # 12 * 0.4

    def test_gate_shrinks_its_budget_to_a_low_gateway_timeout(self):
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10}}})
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                    {"REVIEW_LOOP_GH_STUB": str(hang)})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 10)                      # inside the gateway's 10s kill
        (entry,) = loop_entries().values()
        self.assertEqual((entry["kind"], entry["budget_s"]), ("timeout", 4.0))

    # -- a failed gate never keeps a seat ------------------------------------------------------

    def claim_then(self, action: str):
        script = t.TMP / "claiming_gate.py"
        script.write_text(
            f"import sys\nsys.path.insert(0, {str(t.ROOT)!r})\n"
            "from review_loop import config, gate_failures, state\n"
            "def main():\n"
            "    st = state.state_for(config.load_id('widgets'))\n"
            f"    st.acquire('reviewer', {t.REPO + '#7'!r}, {t.HEAD_A!r}, 'test claim')\n"
            f"    {action}\n"
            "gate_failures.run('gate_reviewer', main)\n")
        proc = subprocess.run([sys.executable, str(script)], input=json.dumps(t.pr_payload(7)),
                              capture_output=True, text=True, timeout=30,
                              env={**t.env(), "REVIEW_LOOP_GATE_BUDGET_S": "1"})
        return proc, state_mod.LoopState(config.load_id("widgets"))

    def test_a_timeout_after_claiming_a_seat_releases_it(self):
        proc, st = self.claim_then("import time; time.sleep(60)")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(st.live_locks("reviewer"), {})
        (entry,) = loop_entries().values()
        self.assertEqual(entry["released_claims"], [f"reviewer:{t.REPO}#7"])

    def test_a_crash_after_claiming_a_seat_releases_it(self):
        proc, st = self.claim_then("raise RuntimeError('boom')")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(st.live_locks("reviewer"), {})

    def test_a_claim_that_succeeded_is_kept(self):
        proc, st = self.claim_then("print('[SILENT]')")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f"{t.REPO}#7", st.live_locks("reviewer"))

    def test_real_gate_timeout_leaves_no_seat_or_inflight_mark(self):
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, _ = gateway_run("gate_fixer.py", t.review_payload(7),
                              {"REVIEW_LOOP_GH_STUB": str(hang), "REVIEW_LOOP_GATE_BUDGET_S": "1"})
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(t.load_state("locks.json"), {})
        self.assertEqual(t.load_state("inflight.json"), {})

    # -- the watchdog's own reads are budgeted -------------------------------------------------

    def test_hanging_github_cannot_stall_the_watchdog(self):
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")], capture_output=True,
                              text=True, cwd=str(SCRIPTS), timeout=60,
                              env={**t.env(), "REVIEW_LOOP_GH_STUB": str(hang),
                                   "REVIEW_LOOP_WATCHDOG_BUDGET_S": "2"})
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("watchdog stopped: GitHub did not answer within the sweep's 2s budget",
                      proc.stdout)

    def test_a_gate_drain_runs_on_the_gates_clock(self):
        from unittest import mock
        from review_loop import gh
        loop = config.load_id("widgets")
        with mock.patch.object(gate.subprocess, "run") as run:
            gh.begin_gate(time.monotonic() + 1.5)
            try:
                gate.drain_seat(loop, "reviewer")
            finally:
                gh.end_gate()
            run.assert_not_called()                       # deferred to the watchdog
            gh.begin_gate(time.monotonic() + 10)
            try:
                gate.drain_seat(loop, "reviewer")
            finally:
                gh.end_gate()
        timeout = run.call_args.kwargs["timeout"]
        self.assertTrue(4 < timeout <= 5, timeout)
        self.assertLess(float(run.call_args.kwargs["env"]["REVIEW_LOOP_WATCHDOG_BUDGET_S"]), 5)

    def test_secrets_are_scrubbed_and_text_bounded(self):
        text = gate_failures._bounded("x" * 50 + " ghp_" + "A" * 36, 40)
        self.assertNotIn("ghp_", text)
        self.assertLessEqual(len(text), 40)


if __name__ == "__main__":
    unittest.main()
