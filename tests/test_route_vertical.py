"""Offline route-script subprocess acceptance probes; never use a live gateway or CLI.

The fixture creates both HOME and HERMES_HOME before starting *any* child.
Only the stub executable is permitted to answer the gate's GitHub reads.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import sqlite3
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40


class RouteSubprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        home = self.root / "home"
        home.mkdir()
        loops = home / "review-loops.d"
        loops.mkdir()
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(home),
                    "HERMES_HOME": str(home), "REVIEW_LOOP_CONFIG_DIR": str(loops),
                    "REVIEW_LOOP_GH_STUB": str(self.root / "gh-stub.py"),
                    "GH_WORLD": str(self.root / "world.json"),
                    "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0"}
        self.loop = {"id": "widgets", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
                     "seats": {"reviewer": {"profile": "fixture-reviewer", "route": "review"},
                               "fixer": {"profile": "fixture-fixer", "route": "fix"}},
                     "state_dir": str(home / "state"), "tokens": {}, "read_token": "",
                     "host": "http://127.0.0.1:9"}
        (loops / "widgets.json").write_text(json.dumps(self.loop))
        (self.root / "gh-stub.py").write_text(
            "#!/usr/bin/python3\nimport json, os, sys\n"
            "assert os.environ['HOME'] == os.environ['HERMES_HOME']\n"
            "assert os.environ['HOME'].startswith(os.path.dirname(os.environ['GH_WORLD']))\n"
            "world=json.load(open(os.environ['GH_WORLD']))\n"
            "path=sys.argv[1]\n"
            "if path.endswith('/reviews?per_page=100'):\n"
            " print(json.dumps(world['reviews']))\n"
            "else:\n print(json.dumps(world['pr']))\n")
        (self.root / "gh-stub.py").chmod(0o700)
        self.world = self.root / "world.json"
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "dev"},
                   "base": {"ref": "main"}, "head": {"sha": HEAD, "ref": "fix-7"}}
        self.world.write_text(json.dumps({"pr": self.pr, "reviews": []}))

    def route(self, script, payload):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script)],
                              input=json.dumps(payload), text=True, capture_output=True,
                              env=self.env, cwd=self.root, timeout=15)

    def test_without_runtime_eligible_reviewer_stays_silent_and_records_hold(self):
        payload = {"repository": {"full_name": "acme/widgets"}, "action": "opened",
                   "number": 7, "pull_request": self.pr, "sender": {"login": "dev"}}
        result = self.route("gate_reviewer.py", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[SILENT]")
        pending = json.loads((Path(self.loop["state_dir"]) / "pending.json").read_text())
        self.assertIn("isolated worker unavailable", json.dumps(pending).lower())
        self.assertNotIn("token", result.stdout.lower())
        # This is an explicit blocker, not evidence that a whole-agent turn ran:
        # the current route script queues in the old gate state, not Supervisor.
        self.assertFalse(list(self.root.rglob("*.sqlite")))

    def test_missing_runtime_hold_survives_explicit_drain_and_retries_when_available(self):
        from review_loop import config, gh, routes, state
        from scripts import watchdog
        payload = {"repository": {"full_name": "acme/widgets"}, "action": "opened",
                   "number": 7, "pull_request": self.pr, "sender": {"login": "dev"}}
        self.assertEqual(self.route("gate_reviewer.py", payload).stdout.strip(), "[SILENT]")
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(gh, "pr", return_value=self.pr), \
             mock.patch.object(gh, "reviews", return_value=[]):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            def dispatch(*args, **kwargs):
                # The gateway says HTTP 2xx even when its gate emits [SILENT].
                result = self.route("gate_reviewer.py", payload)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "[SILENT]"))
                return True
            with mock.patch.object(routes, "fire", side_effect=dispatch):
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
                self.assertIn("unavailable", st.queue_items("reviewer")[key]["reason"])
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
                self.assertIn(key, st.queue_items("reviewer"))
                runtime = Path(self.env["HERMES_HOME"]) / "review-loop-runtime.json"
                runtime.write_text("{}")
                runtime.chmod(0o600)
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 1)
                self.assertNotIn(key, st.queue_items("reviewer"))

    def test_drain_preserves_replaced_queue_entry_on_successful_http(self):
        from review_loop import config, gh, routes, state
        from scripts import watchdog
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(gh, "pr", return_value=self.pr), \
             mock.patch.object(gh, "reviews", return_value=[]):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            st.queue_add("reviewer", key, HEAD, "url", "original")
            def replace(*args, **kwargs):
                st.queue_add("reviewer", key, HEAD, "url", "replacement")
                return True
            with mock.patch.object(routes, "fire", side_effect=replace):
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
            self.assertEqual(st.queue_items("reviewer")[key]["reason"], "replacement")

    def test_gate_compare_pop_does_not_remove_replacement_during_enqueue(self):
        from review_loop import config, gate, state, run_supervisor
        with mock.patch.dict(os.environ, self.env):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            st.queue_add("reviewer", key, HEAD, "url", "original")
            runtime = Path(self.env["HERMES_HOME"]) / "review-loop-runtime.json"
            runtime.write_text("{}")
            runtime.chmod(0o600)
            def enqueue(*args, **kwargs):
                st.queue_add("reviewer", key, HEAD, "url", "new turn")
            with mock.patch.object(run_supervisor.Supervisor, "recover"), \
                 mock.patch.object(run_supervisor.Supervisor, "enqueue", side_effect=enqueue), \
                 mock.patch.object(config, "seat_concurrency", return_value=1), \
                 mock.patch.object(gate, "silence", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    gate.block_pr_agent(loop, st, "reviewer", 7, HEAD)
            self.assertEqual(st.queue_items("reviewer")[key]["reason"], "new turn")

    def test_gate_failure_does_not_overwrite_replacement_during_enqueue(self):
        from review_loop import config, gate, state, run_supervisor
        with mock.patch.dict(os.environ, self.env):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            st.queue_add("reviewer", key, HEAD, "url", "original")
            runtime = Path(self.env["HERMES_HOME"]) / "review-loop-runtime.json"
            runtime.write_text("{}")
            runtime.chmod(0o600)
            def fail(*args, **kwargs):
                st.queue_add("reviewer", key, HEAD, "url", "new turn")
                raise OSError("worker unavailable")
            with mock.patch.object(run_supervisor.Supervisor, "recover"), \
                 mock.patch.object(run_supervisor.Supervisor, "enqueue", side_effect=fail), \
                 mock.patch.object(config, "seat_concurrency", return_value=1), \
                 mock.patch.object(gate, "silence", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    gate.block_pr_agent(loop, st, "reviewer", 7, HEAD)
            self.assertEqual(st.queue_items("reviewer")[key]["reason"], "new turn")

    def test_ambiguous_route_post_keeps_hold_without_replaying(self):
        from review_loop import config, gh, routes, state
        from scripts import watchdog
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(gh, "pr", return_value=self.pr), \
             mock.patch.object(gh, "reviews", return_value=[]):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            st.queue_add("reviewer", key, HEAD, "url", "original")
            def ambiguous(*args, **kwargs):
                kwargs["on_attempt"]()
                return False
            with mock.patch.object(routes, "fire", side_effect=ambiguous) as fire:
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
                fire.assert_called_once()
            self.assertIn("manual reconciliation", st.queue_items("reviewer")[key]["reason"])

    def test_pre_post_route_failure_remains_retryable(self):
        from review_loop import config, gh, routes, state
        from scripts import watchdog
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(gh, "pr", return_value=self.pr), \
             mock.patch.object(gh, "reviews", return_value=[]):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            st.queue_add("reviewer", key, HEAD, "url", "original")
            with mock.patch.object(routes, "target", return_value=None), \
                 mock.patch.object(routes.urllib.request, "urlopen") as urlopen:
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
                urlopen.assert_not_called()
            self.assertNotIn("manual reconciliation", st.queue_items("reviewer")[key]["reason"])

    def test_stale_head_read_does_not_erase_replacement(self):
        from review_loop import config, gh, state
        from scripts import watchdog
        with mock.patch.dict(os.environ, self.env):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            st.queue_add("reviewer", key, HEAD, "url", "old head")
            def live(*args):
                st.queue_add("reviewer", key, "b" * 40, "url", "new head")
                return {**self.pr, "head": {**self.pr["head"], "sha": "b" * 40}}
            with mock.patch.object(gh, "pr", side_effect=live):
                self.assertEqual(watchdog.drain(loop, st, "reviewer", quiet=True), 0)
            self.assertEqual(st.queue_items("reviewer")[key]["reason"], "new head")

    def test_reverse_ordered_fixer_verdict_uses_latest(self):
        from review_loop import config, gh, routes, state
        from scripts import watchdog
        older = {"id": 10, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
                 "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        newer = {**older, "id": 11, "submitted_at": "2026-01-02T00:00:00Z"}
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(gh, "pr", return_value=self.pr), \
             mock.patch.object(gh, "reviews", return_value=[newer, older]):
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            st.queue_add("fixer", key, HEAD, "url", "original")
            def dispatch(route, event, payload, tag, host, **kwargs):
                self.assertEqual(payload["review"]["id"], 11)
                return False
            with mock.patch.object(routes, "fire", side_effect=dispatch) as fire:
                watchdog.drain(loop, st, "fixer", quiet=True)
                fire.assert_called_once()

    def test_newer_approval_or_unknown_order_cannot_wake_fixer(self):
        from review_loop import config, gh, routes, state
        from scripts import watchdog
        old = {"id": 10, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
               "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        approved = {**old, "id": 11, "state": "APPROVED",
                    "submitted_at": "2026-01-02T00:00:00Z"}
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(gh, "pr", return_value=self.pr), \
             mock.patch.object(routes, "fire") as fire:
            loop = config.load_id("widgets")
            st = state.LoopState(loop)
            key = "acme/widgets#7"
            for reviews, remains in (([approved, old], False), ([{**old, "id": None}], True)):
                st.queue_add("fixer", key, HEAD, "url", "original")
                with mock.patch.object(gh, "reviews", return_value=reviews):
                    self.assertEqual(watchdog.drain(loop, st, "fixer", quiet=True), 0)
                self.assertEqual(key in st.queue_items("fixer"), remains)
            fire.assert_not_called()

    def test_eligible_fixer_and_ineligible_events_stay_silent(self):
        verdict = {"id": 10, "state": "changes_requested", "commit_id": HEAD,
                   "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        self.world.write_text(json.dumps({"pr": self.pr, "reviews": [verdict]}))
        eligible = {"repository": {"full_name": "acme/widgets"}, "action": "submitted",
                    "number": 7, "pull_request": self.pr, "review": verdict}
        for script, payload in (("gate_fixer.py", eligible),
                                ("gate_reviewer.py", {**eligible, "action": "synchronize"}),
                                ("gate_fixer.py", {**eligible, "review": {**verdict, "commit_id": "b" * 40}})):
            with self.subTest(script=script, action=payload["action"],
                              head=payload["review"]["commit_id"]):
                result = self.route(script, payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "[SILENT]")
        pending = json.loads((Path(self.loop["state_dir"]) / "pending.json").read_text())
        self.assertIn("fixer", json.dumps(pending))

    def test_untrusted_sender_and_wrong_repository_do_not_queue(self):
        payload = {"repository": {"full_name": "acme/widgets"}, "action": "opened",
                   "number": 7, "pull_request": self.pr}
        for changed in ({"repository": {"full_name": "other/widgets"}},
                        {"pull_request": {**self.pr, "user": {"login": "stranger"}}},
                        {"pull_request": {**self.pr, "draft": True}}):
            with self.subTest(changed=changed):
                result = self.route("gate_reviewer.py", {**payload, **changed})
                self.assertEqual((result.returncode, result.stdout.strip()),
                                 (0, "[SILENT]"), result.stderr)
        self.assertFalse((Path(self.loop["state_dir"]) / "pending.json").exists())

    def test_outsider_review_verdict_never_queues_fixer(self):
        payload = {"repository": {"full_name": "acme/widgets"}, "action": "submitted",
                   "number": 7, "pull_request": {**self.pr, "user": {"login": "outsider"}},
                   "review": {"id": 10, "state": "changes_requested", "commit_id": HEAD,
                              "user": {"login": "reviewer"}}}
        result = self.route("gate_fixer.py", payload)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "[SILENT]"), result.stderr)
        self.assertFalse((Path(self.loop["state_dir"]) / "pending.json").exists())
        self.assertIn("not an authorized fixer", result.stderr)

    def test_route_enqueues_deduplicates_and_detached_worker_fails_closed(self):
        runtime = Path(self.env["HERMES_HOME"]) / "review-loop-runtime.json"
        runtime.write_text("{}")  # invalid production settings; no network or key access
        runtime.chmod(0o600)
        payload = {"repository": {"full_name": "acme/widgets"}, "action": "opened",
                   "number": 7, "pull_request": self.pr, "sender": {"login": "dev"}}
        first = self.route("gate_reviewer.py", payload)
        second = self.route("gate_reviewer.py", payload)
        for result in (first, second):
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "[SILENT]"),
                             result.stderr)
        db = Path(self.env["HERMES_HOME"]) / "state" / "review-loop-runs.sqlite"
        deadline = time.monotonic() + 10
        rows = []
        while time.monotonic() < deadline:
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT state, attempts, error FROM runs").fetchall()
            if rows and rows[0][0] == "failed":
                break
            time.sleep(0.05)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0:2], ("failed", 1))
        self.assertIn("review generation unavailable", rows[0][2])
        self.assertFalse((Path(self.loop["state_dir"]) / "pending.json").exists())

    def test_dismissed_same_head_reopens_one_distinct_reviewer_turn(self):
        runtime = Path(self.env['HERMES_HOME']) / 'review-loop-runtime.json'
        runtime.write_text('{}')
        runtime.chmod(0o600)
        payload = {'repository': {'full_name': 'acme/widgets'}, 'action': 'opened',
                   'number': 7, 'pull_request': self.pr, 'sender': {'login': 'dev'}}
        self.assertEqual(self.route('gate_reviewer.py', payload).returncode, 0)
        dismissed = {'id': 42, 'state': 'DISMISSED', 'commit_id': HEAD,
                     'user': {'login': 'reviewer'}}
        self.world.write_text(json.dumps({'pr': self.pr, 'reviews': [dismissed]}))
        db = Path(self.env['HERMES_HOME']) / 'state' / 'review-loop-runs.sqlite'
        for _ in range(2):
            result = self.route('gate_reviewer.py', payload)
            self.assertEqual((result.returncode, result.stdout.strip()),
                             (0, '[SILENT]'), result.stderr)
        with sqlite3.connect(db) as conn:
            keys = [row[0] for row in conn.execute('SELECT turn_key FROM runs ORDER BY turn_key')]
        self.assertEqual(keys, ['', 'dismissed:42'])


if __name__ == "__main__":
    unittest.main()
