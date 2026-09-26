"""#49: one isolated turn's wall clock is a per-loop, per-seat setting — not a hard 120 s.

The budget travels gate → ledger row → detached worker → Hermes's ``--run-budget`` and the
sandbox kill. These tests follow it along that path with the real pieces wherever they can run
without a network or a model: a real ``Supervisor`` spawning its real detached worker around a
fixture child, the real ``gate.enqueue_isolated`` writing a real ledger, and the real
``trusted_turn.run_turn`` building the real Hermes argv (only bwrap itself is faked).
"""
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from review_loop import (broker_ipc, cli, config, doctor, gate, gh,  # noqa: E402
                         run_supervisor, seat_model, selftest, trusted_turn)
from review_loop.run_supervisor import Supervisor  # noqa: E402
import test_selftest  # noqa: E402

HEAD = "a" * 40


def _loop(**extra) -> dict:
    raw = {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
           "seats": {"reviewer": {"profile": "rev", "route": "r"},
                     "fixer": {"profile": "fix", "route": "f"}}}
    raw.update(extra)
    return raw


class Settings(unittest.TestCase):
    def test_default_is_a_real_review_not_two_minutes(self):
        loop = config.normalize(_loop())
        self.assertEqual(loop["turn_budget_s"], 900)
        for seat in ("reviewer", "fixer", "adjudicator"):
            self.assertEqual(config.turn_budget(loop, seat), 900)
        self.assertEqual(Supervisor.__init__.__kwdefaults__["child_timeout"], 900)
        self.assertEqual(config.SETTINGS_SCHEMA["turn_budget_s"]["default"], 900)
        self.assertEqual(config.settings_defaults(None)["turn_budget_s"], 900)

    def test_per_seat_wins_over_the_loop(self):
        raw = _loop(turn_budget_s=1200)
        raw["seats"]["fixer"]["turn_budget_s"] = "2400"
        loop = config.normalize(raw)
        self.assertEqual((config.turn_budget(loop, "reviewer"), config.turn_budget(loop, "fixer"),
                          config.turn_budget(loop, "adjudicator")), (1200, 2400, 1200))
        self.assertEqual(loop["seats"]["fixer"]["turn_budget_s"], 2400)   # stored as an int

    def test_an_unnormalized_loop_still_gets_the_default(self):
        self.assertEqual(config.turn_budget({"seats": {}}, "reviewer"), 900)

    def test_nonsense_is_refused(self):
        for bad in (0, 59, 14401, "soon", True, -5):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.normalize(_loop(turn_budget_s=bad))
        raw = _loop()
        raw["seats"]["reviewer"]["turn_budget_s"] = 10
        with self.assertRaisesRegex(config.ConfigError, "seats.reviewer.turn_budget_s"):
            config.normalize(raw)

    def test_plugin_settings_feed_new_loops_and_apply(self):
        d = config.settings_defaults({"turn_budget_s": "1800"})
        self.assertEqual(d["turn_budget_s"], 1800)
        overlaid = config.apply_settings(_loop(turn_budget_s=900), {"turn_budget_s": 1800})
        self.assertEqual(config.normalize(overlaid)["turn_budget_s"], 1800)

    def test_plugin_yaml_declares_it(self):
        text = (ROOT / "plugin.yaml").read_text().split("\nconfig_schema:", 1)[1]
        block = text.split("\n  turn_budget_s:\n", 1)[1].split("\n  description:", 1)[0]
        self.assertIn("type: int", block)
        self.assertIn("default: 900", block)

    def test_cli_parses_init_set_and_selftest_flags(self):
        captured = {}

        class Ctx:
            def register_cli_command(self, name, help_text, setup, description=""):
                captured["setup"] = setup
        cli.register_cli(Ctx(), {"turn_budget_s": 1500})
        import argparse
        parser = argparse.ArgumentParser()
        captured["setup"](parser)
        init = parser.parse_args(["init", "--repo", "a/b", "--fixer-turn-budget", "3000"])
        self.assertEqual((init.turn_budget, init.reviewer_turn_budget, init.fixer_turn_budget),
                         (1500, None, 3000))   # the plugin setting is init's default
        change = parser.parse_args(["set", "--loop", "b", "--turn-budget", "1200",
                                    "--reviewer-turn-budget", "600"])
        self.assertEqual((change.turn_budget, change.reviewer_turn_budget), (1200, 600))
        self.assertIsNone(parser.parse_args(["selftest", "--loop", "b"]).timeout)


class CliSurfaces(unittest.TestCase):
    """init writes it, set changes it, status and doctor show it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes"),
                                             "REVIEW_LOOP_CONFIG_DIR": str(self.root / "loops")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.root / "loops").mkdir()
        raw = _loop(turn_budget_s=900, state_dir=str(self.root / "state"))
        (self.root / "loops" / "widgets.json").write_text(json.dumps(raw))

    def run_cli(self, func, **kw):
        import argparse
        out = io.StringIO()
        with redirect_stdout(out):
            rc = func(argparse.Namespace(**kw))
        return rc, out.getvalue()

    def test_set_then_status_and_doctor_show_it(self):
        empty = dict(concurrency=None, cap=None, base=None, clone=None, grace_min=None,
                     marker_grace_min=None, ttl_min=None, inflight_ttl_min=None, host=None,
                     reviewer_concurrency=None, fixer_concurrency=None, adjudicator_login=None,
                     token=[], observer_route=None, observer_profile=None, observer_deliver=None,
                     observer_events=None, observer_digest_min=None, observer_mute=False,
                     observer_unmute=False, observer_disable=False)
        rc, out = self.run_cli(cli.cmd_set, loop="widgets", turn_budget=1200,
                               fixer_turn_budget=2400, reviewer_turn_budget=None, **empty)
        self.assertEqual(rc, 0, out)
        self.assertIn("turn_budget_s: 900 → 1200", out)
        self.assertIn("fixer turn budget: 900s → 2400s", out)
        loop = config.load_id("widgets")
        self.assertEqual((config.turn_budget(loop, "reviewer"), config.turn_budget(loop, "fixer")),
                         (1200, 2400))
        rc, out = self.run_cli(cli.cmd_set, loop="widgets", turn_budget=30,
                               fixer_turn_budget=None, reviewer_turn_budget=None, **empty)
        self.assertEqual(rc, 2)
        self.assertIn("60-14400", out)

        with mock.patch("review_loop.state.state_for") as state_for:
            state_for.return_value.dir = self.root / "state"
            state_for.return_value._load.return_value = {}
            state_for.return_value.queue_items.return_value = {}
            state_for.return_value.queue_all.return_value = {}
            state_for.return_value.breach_all.return_value = {}
            state_for.return_value.watch.return_value = {}
            rc, out = self.run_cli(cli.cmd_status, loop="widgets")
        self.assertIn("turn:       reviewer 1200s · fixer 2400s per turn", out)

        check = doctor.check_turn_budget(loop)
        self.assertEqual(check.status, doctor.UNKNOWN)          # 2400s > 25m watchdog grace
        self.assertIn("reviewer 1200s · fixer 2400s", check.detail)
        self.assertIn("--grace-min above 40", check.fix)
        ok = doctor.check_turn_budget(config.normalize(_loop()))
        self.assertEqual((ok.status, ok.detail[:28]), (doctor.VERIFIED, "reviewer 900s · fixer 900s p"))


class LedgerAndWorker(unittest.TestCase):
    """A real Supervisor, its real detached worker process, and a fixture child."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.db = self.root / "ledger.sqlite"
        self.seen = self.root / "seen"
        self.child = self.root / "child.py"
        # Records the budget the worker handed it, then sleeps for argv[2] seconds.
        self.child.write_text("import os,sys,time\nfrom pathlib import Path\n"
                              "Path(sys.argv[1]).write_text(os.environ.get('REVIEW_LOOP_TURN_BUDGET',''))\n"
                              "time.sleep(float(sys.argv[2]))\n")
        home = self.root / "home"
        home.mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(home), "HERMES_HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)

    def sup(self, sleep: float, **kw) -> Supervisor:
        return Supervisor(self.db, fixture_mode=True,
                          fixture_command=[sys.executable, str(self.child), str(self.seen), str(sleep)],
                          **kw)

    def wait(self, sup, delivery, states, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            row = sup.get(delivery)
            if row and row["state"] in states:
                return row
            time.sleep(0.05)
        self.fail(f"{delivery} never reached {states}: {sup.get(delivery)}")

    def test_the_rows_budget_reaches_the_child_not_the_worker_default(self):
        sup = self.sup(0)
        self.assertEqual(sup.child_timeout, 900)   # no longer 120
        sup.enqueue("d1", "o/r", 1, HEAD, "reviewer", budget=1234)
        row = self.wait(sup, "d1", ("succeeded", "failed"))
        self.assertEqual((row["state"], row["budget"]), ("succeeded", 1234))
        self.assertEqual(self.seen.read_text(), "1234")

    def test_the_rows_budget_is_the_kill_deadline(self):
        # The worker is spawned with its 900 s default; only the row says 1 s.
        sup = self.sup(30)
        started = time.monotonic()
        sup.enqueue("d2", "o/r", 2, HEAD, "reviewer", budget=1)
        row = self.wait(sup, "d2", ("succeeded", "failed"))
        self.assertEqual((row["state"], row["error"]), ("failed", "child timeout"))
        self.assertLess(time.monotonic() - started, 12)

    def test_launch_lease_covers_the_whole_budget(self):
        sup = self.sup(0, lease_seconds=5)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d3", "o/r", 3, HEAD, "reviewer", budget=2000)
        seen = {}
        with mock.patch.object(sup, "_run_fixture",
                               side_effect=lambda run_id, owner, budget: seen.update(
                                   budget=budget, lease=sup.get("d3")["lease"] - time.time())):
            sup._run_one()
        self.assertEqual(seen["budget"], 2000)
        self.assertGreater(seen["lease"], 2000)

    def test_a_legacy_row_without_a_budget_uses_the_worker_value(self):
        sup = self.sup(0, child_timeout=77)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d4", "o/r", 4, HEAD, "reviewer")
        self.assertEqual(sup.get("d4")["budget"], 77)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET budget=NULL")
        self.assertEqual(sup.budget_of(sup.get("d4")["id"]), 77)

    def test_bad_budgets_are_refused_at_enqueue(self):
        sup = self.sup(0)
        for bad in (0, -1, True, "900"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sup.enqueue(f"bad-{bad!r}", "o/r", 5, HEAD, "reviewer", budget=bad)


class GateToProductionWorker(unittest.TestCase):
    """The gate records the loop's seat budget; the production worker hands it to the turn."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name)
        runtime = self.home / "review-loop-runtime.json"
        runtime.write_text(json.dumps({"source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}))
        runtime.chmod(0o600)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "REVIEW_LOOP_CONFIG_DIR": str(self.home / "none")})
        patch.start()
        self.addCleanup(patch.stop)
        raw = _loop(turn_budget_s=1100, state_dir=str(self.home / "state"), read_token="reader")
        raw["seats"]["fixer"]["turn_budget_s"] = 2700
        self.loop = config.normalize(raw)

    def ledger(self):
        return Supervisor(self.home / "state" / "review-loop-runs.sqlite")

    def test_enqueue_isolated_records_each_seats_budget(self):
        with mock.patch.object(Supervisor, "_spawn") as spawn:
            gate.enqueue_isolated(self.loop, "reviewer", 7, HEAD)
            gate.enqueue_isolated(self.loop, "fixer", 8, HEAD)
        self.assertTrue(spawn.called)          # a production worker would have been armed
        ledger = self.ledger()
        self.assertEqual(ledger.get(f"acme/widgets:7:{HEAD}:reviewer")["budget"], 1100)
        self.assertEqual(ledger.get(f"acme/widgets:8:{HEAD}:fixer")["budget"], 2700)

    def run_production(self, run_turn):
        with mock.patch.object(Supervisor, "_spawn"):
            gate.enqueue_isolated(self.loop, "fixer", 8, HEAD)
        sup = Supervisor(self.home / "state" / "review-loop-runs.sqlite",
                         production_config=self.home / "review-loop-runtime.json",
                         hermes_home=self.home)
        row = sup.get(f"acme/widgets:8:{HEAD}:fixer")
        with sqlite3.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='launching', owner='w', launch_intent=1 WHERE id=?",
                        (row["id"],))
        inference = mock.Mock(upstream="u", key="k", model="m", api_mode="chat_completions",
                              proxy_model="", client_identity="")
        pr = {"number": 8, "head": {"sha": HEAD, "ref": "fix-8"}}
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(seat_model, "load_runtime", return_value={
                 "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
             mock.patch.object(seat_model, "resolve_seat", return_value=inference), \
             mock.patch.object(gh, "api", return_value=pr), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(gh, "review_state", return_value="CHANGES_REQUESTED"), \
             mock.patch("review_loop.gate.latest_effective_review_at_head", return_value={}), \
             mock.patch.object(run_supervisor, "isolated_prompt", return_value="PROMPT"), \
             mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
             mock.patch.object(sup, "recover"):
            sup._run_production(row["id"], "w")
        return sup.get(f"acme/widgets:8:{HEAD}:fixer")

    def test_the_worker_hands_the_rows_budget_to_the_turn(self):
        seen = {}
        row = self.run_production(lambda _loop, scope, **kw: seen.update(kw) or 0)
        self.assertEqual(seen["timeout"], 2700)
        self.assertEqual(row["state"], "succeeded")

    def test_a_timeout_names_the_budget(self):
        def run_turn(_loop, scope, **kw):
            raise subprocess.TimeoutExpired(["bwrap"], kw["timeout"])
        row = self.run_production(run_turn)
        self.assertEqual(row["state"], "failed")
        self.assertIn("killed at the 2700s turn budget", row["error"])


class RealTurnArgv(test_selftest.SelftestBase):
    """The real run_turn: the budget becomes --run-budget, and the kill comes a grace later."""

    def capture(self, delay_dispatch: float = 0.0, time_out: bool = False):
        seen = {}

        def run(**kwargs):
            if "broker_socket_dir" not in kwargs:           # step 2's sandbox probe
                return self.fx.contained_run(**kwargs)
            seen.update(entry=kwargs["entry"], timeout=kwargs["timeout"])
            sock = str(pathlib.Path(kwargs["broker_socket_dir"]) / "broker.sock")
            request = threading.Thread(target=lambda: seen.update(answer=broker_ipc.request(
                "review", verdict="REQUEST_CHANGES", body="late", socket_path=sock)), daemon=True)
            request.start()
            if not time_out:
                request.join()
                return subprocess.CompletedProcess([], 0, "done", "")
            dispatching.wait(5)
            # The sandbox is killed at its deadline while the broker is still mid-request.
            raise subprocess.TimeoutExpired(["bwrap"], kwargs["timeout"])

        dispatching = threading.Event()
        original = broker_ipc.RunBroker._dispatch

        def slow_dispatch(broker, raw):
            dispatching.set()
            time.sleep(delay_dispatch)
            seen["dispatched"] = True
            return original(broker, raw)
        stage = lambda loop, **kw: (kw["sandbox_root"].mkdir() or kw["sandbox_root"])  # noqa: E731
        from review_loop import broker, contained, review_receipt, trusted_fetch
        with mock.patch.object(trusted_fetch, "stage", side_effect=stage), \
             mock.patch.object(contained, "run", side_effect=run), \
             mock.patch.object(broker_ipc.RunBroker, "_dispatch", slow_dispatch), \
             mock.patch.object(broker, "perform", side_effect=AssertionError("perform called")), \
             mock.patch.object(review_receipt, "submit", side_effect=AssertionError("submit")):
            rc, text = self.run_selftest(pr=7, live_turn=True)   # no timeout: the loop's
        return seen, rc, text

    def test_selftest_uses_the_loops_reviewer_budget_and_argv_carries_it(self):
        self.fx.loop["turn_budget_s"] = 1500
        self.fx.loop["seats"]["reviewer"]["turn_budget_s"] = 777
        seen, rc, text = self.capture()
        self.assertEqual(rc, 0, text)
        entry = seen["entry"]
        self.assertEqual(entry[entry.index("--run-budget") + 1], "777")
        self.assertEqual(seen["timeout"], 777 + trusted_turn.KILL_GRACE_S)
        self.assertIn("up to 777s", text)

    def test_default_loop_selftest_is_the_production_default(self):
        seen, rc, text = self.capture()
        entry = seen["entry"]
        self.assertEqual(entry[entry.index("--run-budget") + 1], "900")

    def test_a_kill_mid_request_lets_the_broker_finish_instead_of_abandoning_it(self):
        # 6 s > the old 5 s join: before #49 this surfaced as "broker did not shut down" with
        # the request abandoned mid-write (for a fixer: an unresolved push intent → quarantine).
        seen, rc, text = self.capture(delay_dispatch=6, time_out=True)
        self.assertTrue(seen.get("dispatched"))
        self.assertIn("TimeoutExpired", text)
        self.assertNotIn("broker did not shut down", text)


if __name__ == "__main__":
    unittest.main()
