"""Ambiguous fixer pushes must hold the PR and alert the operator."""
import json
import contextlib
import io
from pathlib import Path
import sqlite3

import tempfile
import unittest
from unittest import mock

from review_loop import broker, broker_ipc, safe_push
from review_loop.run_supervisor import Supervisor
from scripts import gate_fixer

HEAD = 'a' * 40


class PostWriteQuarantine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / 'state').mkdir()
        self.db = self.home / 'state' / 'review-loop-runs.sqlite'
        self.sup = Supervisor(self.db)
        self.sup.enqueue('fix', 'acme/widgets', 7, HEAD, 'fixer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running',owner='worker',launch_intent=1,push_admitted=1 "
                        "WHERE delivery='fix'")
        self.row = self.sup.get('fix')
        self.scope = broker_ipc.RunScope('acme/widgets', 7, HEAD, 'fixer', 'fix-7',
                                         self.row['id'], str(self.db))
        self.manifest = {'base_head': HEAD, 'message': 'fix', 'files': []}

    def dispatch_denied(self, outcome):
        loop = {'repo': self.scope.repo, 'state_dir': str(self.db.parent),
                'unattended_fixer_push': True}
        server = broker_ipc.RunBroker(loop, self.scope, self.db.parent)
        with mock.patch('review_loop.config.by_repo', return_value=loop), \
             mock.patch('review_loop.safe_push._manifest'), \
             mock.patch('review_loop.safe_push.push',
                        side_effect=safe_push.PushFailure(outcome)):
            with self.assertRaises(broker.BrokerDenied):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': self.manifest}).encode())
        return server

    def test_post_write_metadata_invalid_stays_uncertain_after_worker_exit(self):
        server = self.dispatch_denied('published_pr_unverified')
        self.assertFalse(server.completed)
        self.assertFalse(server._pushed_head)
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')
        self.sup.complete_uncertain(self.row['id'], 'worker', 0)
        self.sup.recover()
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')
        with self.assertRaises(broker_ipc.ProtocolError):
            server._dispatch(b'{"operation":"request_review","verdict":"","body":""}')
        notices = []
        self.assertEqual(self.sup.notify(notices.append), 1)
        self.assertIn('post-write', notices[0])
        self.assertEqual(self.sup.notify(notices.append), 0)
        self.assertEqual(self.sup.status()[0]['state'], 'uncertain')

    def test_approval_event_cannot_announce_merge_on_quarantined_pr(self):
        self.dispatch_denied('published_pr_unverified')
        # The real gate resolves its ledger from the host home; use a disposable
        # home with the same schema, not a live Hermes configuration.
        home = self.home
        payload = {'action': 'submitted', 'number': 7,
                   'pull_request': {'number': 7, 'head': {'sha': HEAD},
                                    'user': {'login': 'fixer'}},
                   'review': {'id': 42, 'state': 'approved', 'commit_id': HEAD,
                              'user': {'login': 'reviewer'}}}
        loop = {'repo': 'acme/widgets', 'fixers': ['fixer'], 'reviewers': ['reviewer']}
        st = mock.Mock()
        with mock.patch.object(gate_fixer.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             mock.patch.object(gate_fixer.gate, 'context', return_value=(loop, st)), \
             mock.patch('review_loop.config.home', return_value=home), \
             mock.patch.object(gate_fixer.gh, 'pr') as live, \
             mock.patch.object(gate_fixer.observer, 'notify') as notice:
            with self.assertRaises(SystemExit) as stopped:
                gate_fixer.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), '[SILENT]')
        live.assert_not_called()
        notice.assert_not_called()
        st.release_if.assert_not_called()
        self.assertTrue(self.sup.post_write_hold('acme/widgets', 7))

    def test_unknown_ref_readback_holds_pr_even_if_worker_fails(self):
        self.dispatch_denied('unknown')
        self.sup.complete_uncertain(self.row['id'], 'worker', 1, 'isolated turn failed')
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')
        with mock.patch.object(self.sup, '_spawn') as spawn:
            self.sup.enqueue('review', 'acme/widgets', 7, 'b' * 40, 'reviewer')
            self.assertIsNone(self.sup._claim())
        spawn.assert_not_called()

    def test_failed_attempt_with_unchanged_ref_is_quarantined(self):
        self.dispatch_denied('unchanged')
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')

    def test_unexpected_failure_after_consuming_capability_is_quarantined(self):
        loop = {'repo': self.scope.repo, 'state_dir': str(self.db.parent),
                'unattended_fixer_push': True}
        server = broker_ipc.RunBroker(loop, self.scope, self.db.parent)
        with mock.patch('review_loop.config.by_repo', return_value=loop), \
             mock.patch('review_loop.safe_push._manifest'), \
             mock.patch('review_loop.safe_push.push', side_effect=OSError('audit unavailable')):
            with self.assertRaises(OSError):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': self.manifest}).encode())
        self.assertTrue(self.sup.post_write_hold(self.scope.repo, self.scope.number))
        self.assertTrue(server._used)

    def test_quarantine_write_fails_after_ref_advances_and_worker_exits(self):
        loop = {'repo': self.scope.repo, 'state_dir': str(self.db.parent),
                'unattended_fixer_push': True}
        server = broker_ipc.RunBroker(loop, self.scope, self.db.parent)
        ref = {'sha': HEAD}
        def published_then_error(*args, **kwargs):
            ref['sha'] = 'b' * 40
            raise safe_push.PushFailure('unknown')
        with mock.patch('review_loop.config.by_repo', return_value=loop), \
             mock.patch('review_loop.safe_push._manifest'), \
             mock.patch('review_loop.safe_push.push', side_effect=published_then_error), \
             mock.patch.object(Supervisor, 'quarantine_push', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(broker_ipc.ProtocolError, 'quarantine persistence failed'):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': self.manifest}).encode())
        self.assertEqual(ref['sha'], 'b' * 40)
        self.assertTrue(self.sup.post_write_hold(self.scope.repo, 7))
        self.sup.complete_uncertain(self.row['id'], 'worker', 1, 'worker stopped')
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')
        self.assertTrue(self.sup.post_write_hold(self.scope.repo, 7))
        with mock.patch.object(self.sup, '_spawn'):
            self.sup.enqueue('next', self.scope.repo, 7, 'c' * 40, 'reviewer')
            self.assertIsNone(self.sup._claim())
        notices = []
        self.assertEqual(self.sup.notify(notices.append), 1)
        self.assertIn('post-write', notices[0])

    def test_intent_refusal_prevents_external_push(self):
        loop = {'repo': self.scope.repo, 'state_dir': str(self.db.parent),
                'unattended_fixer_push': True}
        server = broker_ipc.RunBroker(loop, self.scope, self.db.parent)
        with mock.patch('review_loop.config.by_repo', return_value=loop), \
             mock.patch('review_loop.safe_push._manifest'), \
             mock.patch.object(Supervisor, 'begin_push', side_effect=OSError('disk full')), \
             mock.patch('review_loop.safe_push.push') as push:
            with self.assertRaises(OSError):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': self.manifest}).encode())
        push.assert_not_called()
        self.assertIsNone(self.sup.get('fix')['push_intent'])

    def test_lost_worker_recovery_preserves_push_intent(self):
        self.sup.begin_push(self.row['id'], self.scope.repo, 7, HEAD)
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE runs SET lease=1 WHERE id=?', (self.row['id'],))
        self.sup.recover()
        row = self.sup.get('fix')
        self.assertEqual(row['state'], 'uncertain')
        self.assertIn('post-write push quarantine:', row['error'])
        self.sup.complete_uncertain(self.row['id'], 'worker', 0, stopped=True)
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')
        self.assertTrue(self.sup.post_write_hold(self.scope.repo, 7))

    def test_verified_success_clears_intent_and_completion_failure_does_not(self):
        loop = {'repo': self.scope.repo, 'state_dir': str(self.db.parent),
                'unattended_fixer_push': True}
        request = json.dumps({'operation': 'push', 'manifest': self.manifest}).encode()
        with mock.patch('review_loop.config.by_repo', return_value=loop), \
             mock.patch('review_loop.safe_push._manifest'), \
             mock.patch('review_loop.safe_push.push', return_value={'new_head': 'b' * 40}):
            server = broker_ipc.RunBroker(loop, self.scope, self.db.parent)
            self.assertEqual(server._dispatch(request)['new_head'], 'b' * 40)
        self.assertFalse(self.sup.post_write_hold(self.scope.repo, 7))
        self.assertIsNotNone(self.sup.get('fix')['push_confirmed'])
        with self.assertRaises(ValueError):
            self.sup.begin_push(self.row['id'], self.scope.repo, 7, HEAD)
        # A separate run whose completion commit fails must remain held.
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE runs SET push_intent=NULL,push_confirmed=NULL WHERE id=?',
                        (self.row['id'],))
        with mock.patch('review_loop.config.by_repo', return_value=loop), \
             mock.patch('review_loop.safe_push._manifest'), \
             mock.patch('review_loop.safe_push.push', return_value={'new_head': 'b' * 40}), \
             mock.patch.object(Supervisor, 'confirm_push', side_effect=OSError('disk full')):
            server = broker_ipc.RunBroker(loop, self.scope, self.db.parent)
            with self.assertRaises(OSError):
                server._dispatch(request)
        self.assertTrue(self.sup.post_write_hold(self.scope.repo, 7))
        self.sup.complete_uncertain(self.row['id'], 'worker', 0)
        self.assertEqual(self.sup.get('fix')['state'], 'uncertain')
