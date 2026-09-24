"""An approval is not merge authority after a same-head base retarget or base push."""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from review_loop import observer
from review_loop.state import LoopState
from scripts import gate_fixer

HEAD = 'a' * 40
BASE = 'b' * 40
OTHER = 'c' * 40
LOOP = {'id': 'widgets', 'repo': 'owner/widgets', 'base': 'main',
        'reviewers': ['reviewer'], 'fixers': ['fixer']}
REVIEW = {'id': 42, 'state': 'APPROVED', 'commit_id': HEAD,
          'user': {'login': 'reviewer'}, 'submitted_at': '2026-01-01T00:00:00Z'}


def pr(ref='main', sha=BASE):
    return {'number': 7, 'state': 'open', 'head': {'sha': HEAD},
            'base': {'ref': ref, 'sha': sha, 'repo': {'full_name': LOOP['repo']}},
            'user': {'login': 'fixer'}}


def git_ref(sha=BASE):
    return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': sha}}


class ExactBaseHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.loop = dict(LOOP, state_dir=self.temp.name, host='https://owner.example',
                         observer={'route': 'observe'})
        self.st = LoopState(self.loop)

    def gate(self, snapshot, current, ref):
        payload = {'action': 'submitted', 'number': 7, 'pull_request': snapshot,
                   'review': REVIEW}
        with (mock.patch.object(gate_fixer.sys, 'stdin', io.StringIO(json.dumps(payload))),
              contextlib.redirect_stdout(io.StringIO()),
              mock.patch.object(gate_fixer.gate, 'context', return_value=(self.loop, self.st)),
              mock.patch.object(gate_fixer.gh, 'pr', return_value=current),
              mock.patch.object(gate_fixer.gh, 'reviews', return_value=[REVIEW]),
              mock.patch.object(gate_fixer.gh, 'api', return_value=ref),
              mock.patch.object(gate_fixer.gate, 'drain_seat'),
              mock.patch.object(gate_fixer.observer, 'notify') as notify):
            with self.assertRaises(SystemExit) as result:
                gate_fixer.main()
            self.assertEqual(result.exception.code, 0)
        return notify.call_args.kwargs

    def test_gate_pins_exact_base_and_rejects_retarget_or_unreadable_ref(self):
        accepted = self.gate(pr(), pr(), git_ref())
        self.assertEqual(accepted['next_turn'], 'you merge')
        self.assertEqual(accepted['base_sha'], BASE)
        for snapshot, current, ref in ((pr(), pr('other', OTHER), git_ref()),
                                       (pr(), pr('main', OTHER), git_ref(OTHER)),
                                       (pr(), pr(), git_ref(OTHER)),
                                       (pr(), pr(), None),
                                       (pr('other', OTHER), pr(), git_ref()),
                                       (pr(), pr(), {'ref': 'refs/heads/main', 'object': {'sha': BASE}})):
            with self.subTest(snapshot=snapshot, current=current, ref=ref):
                self.assertNotEqual(self.gate(snapshot, current, ref)['next_turn'], 'you merge')

    def test_delivery_drops_merge_if_base_retargets_or_ref_moves(self):
        for current, ref in ((pr(), git_ref()), (pr('other', OTHER), git_ref()),
                             (pr('main', OTHER), git_ref(OTHER)), (pr(), None)):
            with self.subTest(current=current, ref=ref):
                self.st.observations.unlink(missing_ok=True)
                with (mock.patch.object(observer.gh, 'pr', return_value=current),
                      mock.patch.object(observer.gh, 'reviews', return_value=[REVIEW]),
                      mock.patch.object(observer.gh, 'api', return_value=ref) as api,
                      mock.patch.object(observer, '_post', return_value=(True, '', False)) as post):
                    self.assertTrue(observer.notify(self.loop, self.st, 'approved', 7, HEAD,
                                                    identity=42, next_turn='you merge', base_sha=BASE))
                if current['base']['ref'] == 'main':
                    api.assert_called_with(self.loop, '/repos/owner/widgets/git/ref/heads/main')
                else:
                    api.assert_not_called()
                message = post.call_args.args[1]['message']
                self.assertEqual('next: you merge' in message, current == pr() and ref == git_ref())


if __name__ == '__main__':
    unittest.main()
