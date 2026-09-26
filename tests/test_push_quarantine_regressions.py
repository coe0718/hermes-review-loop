"""Offline regressions for published push errors and late merge-notice holds."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from review_loop import broker, broker_ipc, config, gh, observer, safe_push
from review_loop.run_supervisor import Supervisor
from review_loop.state import LoopState
from tests.test_safe_push import FakeGitHub, HEAD, NEW_HEAD, REPO, manifest


class PostWriteRegressions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "state" / "review-loop-runs.sqlite"
        self.sup = Supervisor(self.db)
        self.sup.enqueue("fix", REPO, 7, HEAD, "fixer")
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running',owner='worker',launch_intent=1,push_admitted=1 WHERE delivery='fix'")
        self.run_id = self.sup.get("fix")["id"]
        tokens = {}
        for login in ("read", "review", "fix"):
            token = self.root / (login + ".pat")
            token.write_text("DUMMY_SECRET_" + login)
            tokens[login] = str(token)
        self.loop = {"id": "fixture", "repo": REPO, "base": "main", "state_dir": str(self.root),
                     "unattended_fixer_push": True, "fixers": ["fix"], "reviewers": ["review"],
                     "tokens": tokens, "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.fake = FakeGitHub()
        patch = mock.patch.object(gh, "api", side_effect=self.fake.api)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(gh, "reviews", return_value=[{
            "id": 41, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
            "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "review"}}])
        patch.start()
        self.addCleanup(patch.stop)

    def test_published_ref_with_git_error_is_quarantined_and_cannot_request_review(self):
        def published_then_error(*args, before_push):
            before_push(NEW_HEAD)
            self.fake.branch_head = NEW_HEAD
            self.fake.pr_head = NEW_HEAD
            raise OSError("lost receive-pack response")

        scope = broker_ipc.RunScope(REPO, 7, HEAD, "fixer", "fix-7", self.run_id, str(self.db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(safe_push, "_git_cas", side_effect=published_then_error):
            with self.assertRaisesRegex(broker.BrokerDenied, r"not confirmed \(published\)"):
                server._dispatch(json.dumps({"operation": "push", "manifest": manifest()}).encode())
        self.assertEqual(self.fake.branch_head, NEW_HEAD)
        self.assertEqual(self.sup.get("fix")["state"], "uncertain")
        self.assertTrue(self.sup.post_write_hold(REPO, 7))
        self.assertEqual(json.loads((self.root / "broker-audit.jsonl").read_text().splitlines()[-1])["outcome"],
                         "published")
        self.assertFalse(server._pushed_head)
        with self.assertRaises(broker_ipc.ProtocolError):
            server._dispatch(b'{"operation":"request_review","verdict":"","body":""}')

    def test_reconciled_audit_failure_after_attempt_quarantines(self):
        actual_audit = safe_push._audit
        def fail_reconciled(loop, record):
            if record['phase'] == 'reconciled':
                raise broker.BrokerDenied('durable audit directory unavailable')
            return actual_audit(loop, record)

        scope = broker_ipc.RunScope(REPO, 7, HEAD, 'fixer', 'fix-7', self.run_id, str(self.db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        def publish(*args, before_push):
            before_push(NEW_HEAD)
            self.fake.branch_head = NEW_HEAD
            self.fake.pr_head = NEW_HEAD
            return NEW_HEAD
        with mock.patch.object(config, 'by_repo', return_value=self.loop), \
             mock.patch.object(safe_push, '_git_cas', side_effect=publish), \
             mock.patch.object(safe_push, '_audit', side_effect=fail_reconciled):
            with self.assertRaises(broker.BrokerDenied):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': manifest()}).encode())
        self.assertEqual(self.fake.branch_head, NEW_HEAD)
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')
        self.assertTrue(self.sup.post_write_hold(REPO, 7))
        self.assertFalse(server._pushed_head)
        with self.assertRaises(broker_ipc.ProtocolError):
            server._dispatch(b'{"operation":"request_review","verdict":"","body":""}')

    def test_failed_pre_attempt_check_does_not_quarantine(self):
        scope = broker_ipc.RunScope(REPO, 7, HEAD, 'fixer', 'fix-7', self.run_id, str(self.db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        def before_attempt(*args, before_push):
            self.fake.pr_state = 'closed'
            before_push(NEW_HEAD)
        with mock.patch.object(config, 'by_repo', return_value=self.loop), \
             mock.patch.object(safe_push, '_git_cas', side_effect=before_attempt):
            with self.assertRaises(broker.BrokerDenied):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': manifest()}).encode())
        self.assertEqual(self.sup.get('fix')['state'], 'running')
        self.assertTrue(self.sup.post_write_hold(REPO, 7))

    def test_attempt_audit_failure_quarantines_even_with_unchanged_ref(self):
        actual_audit = safe_push._audit
        def fail_attempt(loop, record):
            if record['phase'] == 'attempt':
                raise OSError('audit fsync failed')
            return actual_audit(loop, record)
        scope = broker_ipc.RunScope(REPO, 7, HEAD, 'fixer', 'fix-7', self.run_id, str(self.db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        def attempt(*args, before_push):
            before_push(NEW_HEAD)
            self.fail('attempt audit failure must block Git transport')
        with mock.patch.object(config, 'by_repo', return_value=self.loop), \
             mock.patch.object(safe_push, '_git_cas', side_effect=attempt), \
             mock.patch.object(safe_push, '_audit', side_effect=fail_attempt):
            with self.assertRaises(safe_push.PushFailure) as failure:
                server._dispatch(json.dumps({'operation': 'push', 'manifest': manifest()}).encode())
        self.assertEqual(failure.exception.outcome, 'unchanged')
        self.assertEqual(self.fake.branch_head, HEAD)
        self.assertTrue(self.sup.post_write_hold(REPO, 7))

    def test_pre_attempt_denial_text_is_not_attempt_state(self):
        scope = broker_ipc.RunScope(REPO, 7, HEAD, 'fixer', 'fix-7', self.run_id, str(self.db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        with mock.patch.object(config, 'by_repo', return_value=self.loop), \
             mock.patch.object(safe_push, 'push', side_effect=broker.BrokerDenied(
                 'Git ref update not confirmed (unknown)')):
            with self.assertRaises(broker.BrokerDenied):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': manifest()}).encode())
        # Once intent is committed, pre-attempt denials also hold conservatively.
        self.assertTrue(self.sup.post_write_hold(REPO, 7))

    def test_quarantine_persistence_failure_is_surfaced_and_cannot_succeed(self):
        scope = broker_ipc.RunScope(REPO, 7, HEAD, 'fixer', 'fix-7', self.run_id, str(self.db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        def failed_after_attempt(*args, before_push):
            before_push(NEW_HEAD)
            raise OSError('transport lost')
        with mock.patch.object(config, 'by_repo', return_value=self.loop), \
             mock.patch.object(safe_push, '_git_cas', side_effect=failed_after_attempt), \
             mock.patch.object(self.sup.__class__, 'quarantine_push', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(Exception, 'quarantine|disk full'):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': manifest()}).encode())
        self.assertFalse(server._pushed_head)
        self.assertTrue(server._used)

    def test_late_post_write_hold_removes_merge_instruction_at_delivery(self):
        self.loop.update({"host": "https://owner.example", "observer": {
            "route": "observe", "profile": "default", "deliver": "telegram"}})
        state = LoopState(self.loop)
        approval = {"id": 42, "state": "APPROVED", "commit_id": HEAD,
                    "submitted_at": "2026-01-01T00:01:00Z", "user": {"login": "review"}}
        current = {"number": 7, "state": "open", "head": {"sha": HEAD}}
        def late_reviews(*args):
            self.assertFalse(self.sup.post_write_hold(REPO, 7))
            self.sup.quarantine_push(self.run_id, REPO, 7, HEAD, "unknown")
            return [approval]

        with mock.patch.object(config, "home", return_value=self.root), \
             mock.patch.object(observer, "configured", return_value=""), \
             mock.patch.object(observer, "_post", return_value=(True, "", False)) as post, \
             mock.patch.object(observer.gh, "pr", return_value=current), \
             mock.patch.object(observer.gh, "reviews", side_effect=late_reviews):
            # Simulate quarantine during the observer's own live review read.
            self.assertTrue(observer.notify(self.loop, state, "approved", 7, HEAD,
                                            identity=42, next_turn="you merge"))
        self.assertNotIn("next: you merge", post.call_args.args[1]["message"])
        self.assertIn("approved", post.call_args.args[1]["message"])
        self.assertTrue(self.sup.post_write_hold(REPO, 7))


if __name__ == "__main__":
    unittest.main()
