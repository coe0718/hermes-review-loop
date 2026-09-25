"""Adversarial offline host receipt checks; never contacts GitHub."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from review_loop import broker_ipc, gh, review_receipt
from review_loop.run_supervisor import Supervisor

HEAD = 'a' * 40
BASE = 'b' * 40
REPO = 'acme/widgets'


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        tokens = {}
        for login in ('read', 'review', 'fix'):
            p = root / (login + '.pat')
            p.write_text('dummy-' + login)
            tokens[login] = str(p)
        self.loop = {'repo': REPO, 'base': 'main', 'state_dir': str(root),
                     'tokens': tokens, 'read_token': 'read', 'reviewer_seat': 'review',
                     'seats': {'reviewer': {'login': 'review'}, 'fixer': {'login': 'fix'}}}
        self.pr = {'number': 7, 'state': 'open', 'draft': False,
                   'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': REPO}},
                   'head': {'ref': 'fix-7', 'sha': HEAD, 'repo': {'full_name': REPO}}}
        self.sup = Supervisor(root / 'runs.sqlite')
        self.sup.enqueue('d', REPO, 7, HEAD, 'reviewer')
        self.generation = review_receipt.generation_for(self.pr, self.loop, 7, HEAD)
        with sqlite3.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET state='running', generation=?", (self.generation,))
            self.run_id = con.execute('SELECT id FROM runs').fetchone()[0]
        self.scope = broker_ipc.RunScope(REPO, 7, HEAD, 'reviewer', 'fix-7',
                                         self.run_id, str(self.sup.db), self.generation)
        self.ledger = review_receipt.ReceiptLedger(str(self.sup.db), self.run_id,
                                                    self.generation)
        self.posts = 0
        self.mode = None

    def api(self, loop, path, method='GET', body=None, login=None):
        if path == '/user':
            return {'login': login, 'id': {'read': 1, 'review': 2, 'fix': 3}[login]}
        if path == f'/repos/{REPO}/pulls/7':
            if self.mode == 'stale-before-post' and self.posts == 0:
                pr = copy.deepcopy(self.pr)
                pr['base']['sha'] = 'c' * 40
                return pr
            if self.mode == 'stale-after-post' and self.posts:
                pr = copy.deepcopy(self.pr)
                pr['base']['sha'] = 'c' * 40
                return pr
            return self.pr
        if path == f'/repos/{REPO}/pulls/7/reviews' and method == 'POST':
            self.posts += 1
            self.assertEqual(body['commit_id'], HEAD)
            if self.mode == 'lost-post':
                raise TimeoutError('response lost')
            return {'id': 19}
        if path == f'/repos/{REPO}/pulls/7/reviews/19':
            if self.mode == 'readback-failed':
                raise TimeoutError('exact review unreadable')
            return {'id': 20 if self.mode == 'wrong-id' else 19,
                    'state': 'APPROVED', 'commit_id': HEAD,
                    'user': {'id': 2, 'login': 'review'}}
        raise AssertionError(path)

    def receipt(self):
        with sqlite3.connect(self.sup.db) as con:
            return con.execute('SELECT state,review_id FROM review_receipts').fetchone()

    def test_exact_id_confirmed_and_replay_rejected(self):
        with mock.patch.object(gh, 'api', side_effect=self.api):
            self.assertEqual(review_receipt.submit(self.loop, self.scope, self.ledger,
                                                   'APPROVE', 'reviewed'), {'id': 19})
            with self.assertRaises(sqlite3.IntegrityError):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'again')
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.receipt(), ('confirmed', 19))

    def test_stale_generation_before_post_denies_without_claim(self):
        self.mode = 'stale-before-post'
        with mock.patch.object(gh, 'api', side_effect=self.api):
            with self.assertRaises(review_receipt.ReceiptDenied):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'reviewed')
        self.assertEqual(self.posts, 0)
        self.assertIsNone(self.receipt())

    def test_exact_id_mismatch_stays_uncertain(self):
        self.mode = 'wrong-id'
        self.assert_uncertain_after_post()

    def test_failed_exact_id_readback_stays_uncertain(self):
        self.mode = 'readback-failed'
        self.assert_uncertain_after_post()

    def test_generation_change_after_post_stays_uncertain(self):
        self.mode = 'stale-after-post'
        self.assert_uncertain_after_post()

    def test_lost_post_stays_uncertain_without_retry(self):
        self.mode = 'lost-post'
        self.assert_uncertain_after_post()

    def assert_uncertain_after_post(self):
        with mock.patch.object(gh, 'api', side_effect=self.api):
            with self.assertRaises((review_receipt.ReceiptDenied, TimeoutError)):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'reviewed')
            with self.assertRaises((sqlite3.IntegrityError, review_receipt.ReceiptDenied)):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'again')
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.receipt(), ('claimed', None))
        self.sup.complete_uncertain(self.run_id, '', 1, 'failed')
        # The real owner is required to complete; a replay cannot forge ownership.
        with sqlite3.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET owner='owner' WHERE id=?", (self.run_id,))
        self.sup.complete_uncertain(self.run_id, 'owner', 1, 'failed')
        with sqlite3.connect(self.sup.db) as con:
            self.assertEqual(con.execute('SELECT state FROM runs').fetchone()[0], 'uncertain')

    def test_missing_base_sha_cannot_pin_generation(self):
        self.pr['base'].pop('sha')
        with self.assertRaises(review_receipt.ReceiptDenied):
            review_receipt.generation_for(self.pr, self.loop, 7, HEAD)
