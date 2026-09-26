"""Issue #53/#73: a turn that failed before any external write is retried, never lost.

Real SQLite ledger and real fixture child processes (the worker runs in-process so the
test controls time); the claim's GitHub reads go through a patched ``gh.api``. Nothing
here launches Hermes, a sandbox or a network call.
"""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from review_loop import run_supervisor
from review_loop.run_supervisor import MAX_REARMS, MAX_RETRIES, Supervisor

HEAD = 'a' * 40
CHILD = r'''
import os, sqlite3, sys
db, codes, mode = sys.argv[1], sys.argv[2].split(','), sys.argv[3]
counter = db + '.launches'
n = len(open(counter).read().splitlines()) if os.path.exists(counter) else 0
open(counter, 'a').write('x\n')
code = int(codes[min(n, len(codes) - 1)])
con = sqlite3.connect(db, timeout=10)
run = con.execute("SELECT id FROM runs WHERE state='running'").fetchone()[0]
if mode in ('claimed', 'confirmed'):
    # What the broker commits before (claimed) or after (confirmed) a review POST.
    con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,created) "
                "VALUES(?,?,?,?,?)", (run, mode, 'g', 1, 0))
    con.commit()
if code:
    print('hermes: provider error', flush=True)
    print('upstream HTTP 429: rate limited (attempt %d)' % (n + 1), file=sys.stderr)
sys.exit(code)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'ledger.sqlite'
        (self.root / 'child.py').write_text(CHILD)
        env = patch.dict(os.environ, {'HERMES_HOME': str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def sup(self, codes='0', mode='none', production_claim=False):
        sup = Supervisor(self.db, fixture_mode=True, fixture_command=[
            sys.executable, str(self.root / 'child.py'), str(self.db), codes, mode])
        sup._spawn = lambda: None          # the test drives the worker in-process
        if production_claim:
            # The production claim's GitHub reads, with the fixture child as the turn.
            sup.production_config = self.root / 'unused'
            sup._run_production = sup._run_fixture
        return sup

    def launches(self):
        path = Path(str(self.db) + '.launches')
        return len(path.read_text().splitlines()) if path.exists() else 0

    def elapse(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET retry_at=0 WHERE state='waiting'")

    def notices(self, sup):
        out = []
        sup.notify(out.append)
        return out


class PreWriteTurnFailures(Base):
    def test_nonzero_exit_waits_with_backoff_then_succeeds(self):
        sup = self.sup(codes='3,0')
        self.assertEqual(sup.submit('d', 'o/r', 1, HEAD, 'reviewer'), 'enqueued')
        sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['retries'], row['outcome']), ('waiting', 1, 3))
        self.assertEqual(row['error'], 'turn exited with status 3')
        self.assertIn('upstream HTTP 429', row['detail'])
        self.assertAlmostEqual(row['retry_at'] - time.time(), run_supervisor.backoff(1), delta=5)
        sup.recover()                      # backoff not elapsed: nothing scheduled
        self.assertEqual(sup.get('d')['state'], 'waiting')
        self.assertEqual(self.notices(sup), [], 'a waiting run is not an operator alarm')
        self.elapse()
        sup.recover()
        self.assertEqual(sup.get('d')['state'], 'pending')
        sup._run_one()
        self.assertEqual(sup.get('d')['state'], 'succeeded')
        self.assertEqual(self.launches(), 2)

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual([run_supervisor.backoff(n) for n in (1, 2, 3)], [120, 240, 480])
        self.assertEqual(run_supervisor.backoff(20), run_supervisor.RETRY_CAP)

    def test_retry_limit_fails_with_real_reason_then_redelivery_and_retry_rearm(self):
        sup = self.sup(codes='3')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        for _ in range(MAX_RETRIES):
            self.elapse()
            sup.recover()
            sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['retries']), ('failed', MAX_RETRIES))
        self.assertEqual(row['error'], f'retry limit ({MAX_RETRIES} attempts): turn exited with status 3')
        [notice] = self.notices(sup)
        self.assertIn('turn exited with status 3', notice)
        self.assertIn(f'upstream HTTP 429: rate limited (attempt {MAX_RETRIES})', notice)
        self.assertIn('No external write was made', notice)
        self.assertIn('hermes review-loop retry --loop LOOP --pr 1 --seat reviewer', notice)
        self.assertNotIn('Do not replay', notice)
        # A new event for this head re-arms it, one attempt per event (#53/#73).
        self.assertEqual(sup.submit('redelivery', 'o/r', 1, HEAD, 'reviewer'), 'rearmed')
        self.assertEqual(sup.get('d')['state'], 'pending')
        sup._run_one()
        self.assertEqual(sup.get('d')['state'], 'failed')
        self.assertEqual(len(self.notices(sup)), 1, 'the second failure is reported afresh')
        # The operator's retry resets the budget: the backoff chain starts over.
        self.assertEqual(sup.retry(row['id']), 'pending')
        sup._run_one()
        self.assertEqual((sup.get('d')['state'], sup.get('d')['retries']), ('waiting', 1))

    def test_redelivery_rearm_is_bounded(self):
        sup = self.sup(codes='3')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retries=?", (MAX_REARMS,))
        outcome = sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        self.assertTrue(outcome.startswith('duplicate failed:'), outcome)
        self.assertIn('hermes review-loop retry', outcome)
        self.assertEqual(sup.get('d')['state'], 'failed')

    def test_timeout_is_retryable(self):
        (self.root / 'slow.py').write_text('import time\ntime.sleep(5)\n')
        sup = Supervisor(self.db, fixture_mode=True, child_timeout=0.2,
                         fixture_command=[sys.executable, str(self.root / 'slow.py')])
        sup._spawn = lambda: None
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        sup._run_one()
        self.assertEqual((sup.get('d')['state'], sup.get('d')['error']), ('waiting', 'child timeout'))

    def test_duplicate_of_active_or_finished_run_is_reported_not_enqueued(self):
        sup = self.sup()
        self.assertEqual(sup.submit('d', 'o/r', 1, HEAD, 'reviewer'), 'enqueued')
        self.assertEqual(sup.submit('d', 'o/r', 1, HEAD, 'reviewer'), 'pending')
        sup._run_one()
        self.assertEqual(sup.submit('again', 'o/r', 1, HEAD, 'reviewer'), 'duplicate succeeded')
        self.assertEqual(self.launches(), 1)


class PostWriteStaysQuarantined(Base):
    def test_claimed_receipt_then_crash_is_uncertain_and_never_rearmed(self):
        sup = self.sup(codes='1', mode='claimed')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        sup._run_one()
        row = sup.get('d')
        self.assertEqual(row['state'], 'uncertain')
        outcome = sup.submit('redelivery', 'o/r', 1, HEAD, 'reviewer')
        self.assertEqual(outcome, 'duplicate uncertain')
        with self.assertRaisesRegex(ValueError, 'never replayed.*reconcile'):
            sup.retry(row['id'])
        [notice] = self.notices(sup)
        self.assertIn('Do not replay', notice)
        self.assertIn('Possible external write (run is uncertain', notice)
        # Even after the operator reconciles it, the run is not re-armed.
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET pid=NULL WHERE id=?", (row['id'],))
        sup.reconcile_uncertain(row['id'], reason='inspected', acknowledge_no_live_worker=True)
        self.assertTrue(sup.submit('again', 'o/r', 1, HEAD, 'reviewer').startswith(
            'duplicate failed: run was quarantined'))
        with self.assertRaisesRegex(ValueError, 'refused'):
            sup.retry(row['id'])
        self.assertEqual(self.launches(), 1)

    def test_confirmed_write_then_nonzero_exit_fails_without_retry(self):
        sup = self.sup(codes='1', mode='confirmed')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['retries']), ('failed', 0))
        self.assertEqual(sup.submit('redelivery', 'o/r', 1, HEAD, 'reviewer'),
                         'duplicate failed: review receipt confirmed — reconcile, never replayed')
        with self.assertRaisesRegex(ValueError, 'review receipt confirmed'):
            sup.retry(row['id'])
        self.assertEqual(self.launches(), 1)

    def test_push_intent_and_ruling_count_as_writes(self):
        sup = self.sup()
        sup.submit('d', 'o/r', 1, HEAD, 'fixer')
        row = sup.get('d')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', push_confirmed=1 WHERE id=?", (row['id'],))
        with self.assertRaisesRegex(ValueError, 'fixer push recorded'):
            sup.retry(row['id'])
        sup.submit('a', 'o/r', 2, HEAD, 'adjudicator', turn_key='breach:3')
        adj = sup.get('a')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed' WHERE id=?", (adj['id'],))
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (adj['id'], 'o/r', 2, HEAD, 'breach:3',
                                                     'ACCEPT', 'ok', 0, 0))
        with self.assertRaisesRegex(ValueError, 'ruling recorded'):
            sup.retry(adj['id'])


class ClaimTimeReads(Base):
    LOOP = {'repo': 'o/r', 'base': 'main', 'read_token': 'read'}

    def pull(self, **changes):
        pr = {'number': 1, 'state': 'open', 'draft': False,
              'base': {'ref': 'main', 'sha': 'b' * 40, 'repo': {'full_name': 'o/r'}},
              'head': {'sha': HEAD, 'repo': {'full_name': 'o/r'}}}
        pr.update(changes)
        return pr

    def setUp(self):
        super().setUp()
        self.LOOP = {**self.LOOP, 'state_dir': str(self.root / 'state')}
        self.s = self.sup(production_claim=True)
        self.s.submit('review', 'o/r', 1, HEAD, 'reviewer')

    def claim(self, pr):
        with patch('review_loop.config.by_repo', return_value=self.LOOP), \
             patch('review_loop.gh.api', return_value=pr):
            return self.s._claim()

    def test_502_at_claim_is_a_read_retry_then_the_run_succeeds(self):
        self.assertIsNone(self.claim(None))           # gh.api answers None for an HTTP 502
        row = self.s.get('review')
        self.assertEqual((row['state'], row['attempts'], row['error']), ('pending', 0, None))
        with patch('review_loop.config.by_repo', return_value=self.LOOP), \
             patch('review_loop.gh.api', return_value=self.pull()), \
             patch('review_loop.gh.reviews', return_value=[]):
            self.s._run_one()
        self.assertEqual(self.s.get('review')['state'], 'succeeded')

    def test_draft_waits_like_the_fixer(self):
        self.assertIsNone(self.claim(self.pull(draft=True)))
        self.assertEqual(self.s.get('review')['state'], 'pending')
        self.assertIsNotNone(self.claim(self.pull()))

    def test_closed_then_reopen_rearms(self):
        self.assertIsNone(self.claim(self.pull(state='closed')))
        row = self.s.get('review')
        self.assertEqual((row['state'], row['error']), ('cancelled', 'PR closed before the review started'))
        self.assertEqual(self.s.submit('reopened', 'o/r', 1, HEAD, 'reviewer'), 'rearmed')
        self.assertIsNotNone(self.claim(self.pull()))

    def test_unresolvable_generation_fails_with_reason_and_is_rearmable(self):
        self.assertIsNone(self.claim(self.pull(base={'ref': 'other', 'sha': 'b' * 40,
                                                     'repo': {'full_name': 'o/r'}})))
        row = self.s.get('review')
        self.assertEqual((row['state'], row['error']),
                         ('failed', 'review generation unavailable: stale or unresolvable generation'))
        self.assertIsNone(run_supervisor.read_only_view(self.db, 'o/r', 1)[0]['write'])
        self.assertEqual(self.s.submit('retargeted-back', 'o/r', 1, HEAD, 'reviewer'), 'rearmed')


class ProductionClassification(unittest.TestCase):
    def test_transient_versus_terminal(self):
        from review_loop import seat_model, trusted_fetch
        yes = [run_supervisor.RetryableError('x'), TimeoutError(), ConnectionResetError(),
               trusted_fetch.FetchDenied('GitHub response unavailable'),
               seat_model.SeatModelError('profile p: Hermes did not resolve within 30s')]
        no = [ValueError('PR head moved'), trusted_fetch.FetchDenied('stale PR head'),
              seat_model.SeatModelError('profile p (bedrock): unsupported'),
              FileNotFoundError('live broker capability required')]
        self.assertTrue(all(run_supervisor.retryable(e) for e in yes))
        self.assertFalse(any(run_supervisor.retryable(e) for e in no))

    def test_tail_is_bounded_and_printable(self):
        text = run_supervisor.tail('a\x1b[31m' + 'z' * 5000)
        self.assertLessEqual(len(text.encode()), run_supervisor.DETAIL_BYTES + 10)
        self.assertNotIn('\x1b', text)


if __name__ == '__main__':
    unittest.main()


class OperatorCommands(unittest.TestCase):
    """``hermes review-loop retry`` and the ledger lines ``status``/``explain`` print."""

    def setUp(self):
        import json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        loops = home / 'review-loops.d'
        loops.mkdir()
        loop = {"id": "widgets", "repo": "acme/widgets", "base": "main", "cap": 3,
                "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
                "seats": {"reviewer": {"profile": "r", "route": "review"},
                          "fixer": {"profile": "f", "route": "fix"}},
                "state_dir": str(home / "state"), "tokens": {}, "read_token": "",
                "host": "http://127.0.0.1:9"}
        (loops / 'widgets.json').write_text(json.dumps(loop))
        env = patch.dict(os.environ, {'HERMES_HOME': str(home), 'REVIEW_LOOP_CONFIG_DIR': str(loops)})
        env.start()
        self.addCleanup(env.stop)
        self.db = home / 'state' / 'review-loop-runs.sqlite'
        self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=['true'])
        self.sup._spawn = lambda: None
        self.sup.submit('r', 'acme/widgets', 7, HEAD, 'reviewer')
        self.sup.submit('f', 'acme/widgets', 7, HEAD, 'fixer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retries=4, "
                        "error='retry limit (4 attempts): turn exited with status 3', "
                        "detail='stderr: upstream HTTP 429' WHERE delivery='r'")
            con.execute("UPDATE runs SET state='uncertain', launch_intent=1, "
                        "error='worker lost after launch intent' WHERE delivery='f'")

    def run_cli(self, func, **kw):
        import argparse
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func(argparse.Namespace(**kw))
        return code, out.getvalue()

    def test_retry_rearms_prewrite_and_refuses_uncertain(self):
        from review_loop import cli
        code, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat=None)
        self.assertEqual(code, 2, out)            # one refusal makes the command exit 2
        self.assertIn('reviewer #7 @ aaaaaaa re-armed (was failed: retry limit', out)
        self.assertIn('fixer #7 @ aaaaaaa uncertain: refused: run is uncertain', out)
        self.assertIn('run_supervisor reconcile DB', out)
        self.assertIn('no private runtime file', out)
        self.assertEqual(self.sup.get('r')['state'], 'pending')
        self.assertEqual(self.sup.get('r')['retries'], 0)
        self.assertEqual(self.sup.get('f')['state'], 'uncertain')
        code, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat='reviewer')
        self.assertEqual(code, 2)
        self.assertIn('nothing to retry', out)

    def test_ledger_lines_show_reason_tail_and_next_step(self):
        from review_loop import cli, config
        loop = config.load_id('widgets')
        import contextlib
        import io
        out = io.StringIO()
        before = sorted(p.name for p in self.db.parent.iterdir())
        with contextlib.redirect_stdout(out):
            cli._print_ledger_runs(loop, 7, '  run: ', limit=6)
        text = out.getvalue()
        self.assertIn('reviewer #7 @ aaaaaaa failed — retry limit (4 attempts): turn exited with '
                      'status 3; no external write — re-arm: hermes review-loop retry --loop '
                      'widgets --pr 7 --seat reviewer', text)
        self.assertIn('| stderr: upstream HTTP 429', text)
        self.assertIn('fixer #7 @ aaaaaaa uncertain — worker lost after launch intent; may have '
                      'written (run is uncertain', text)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before,
                         'reading the ledger for explain/status writes nothing')

    def test_armed_sweep_schedules_due_retries_only_with_a_runtime(self):
        from review_loop import config, gate
        loop = config.load_id('widgets')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='waiting', retry_at=0 WHERE delivery='r'")
        with patch.object(Supervisor, '_spawn') as spawn:
            self.assertFalse(gate.resume_isolated(loop))       # no private runtime: nothing
            spawn.assert_not_called()
            runtime = Path(os.environ['HERMES_HOME']) / 'review-loop-runtime.json'
            runtime.write_text('{}')
            runtime.chmod(0o600)
            self.assertTrue(gate.resume_isolated(loop))
            spawn.assert_called_once_with()
        self.assertEqual(self.sup.get('r')['state'], 'pending')

    def test_breach_resume_hands_back_only_this_heads_adjudicating_marker(self):
        from review_loop import config, state
        st = state.LoopState(config.load_id('widgets'))
        st.breach_set(7, {'pr': 7, 'head': HEAD, 'rounds': 3, 'status': 'awaiting-adjudication'})
        self.assertIsNone(st.breach_resume(7, HEAD, 3), 'not adjudicating: left alone')
        self.assertIsNotNone(st.breach_start(7, HEAD, 3))
        self.assertIsNone(st.breach_resume(7, 'b' * 40, 3))
        self.assertIsNone(st.breach_resume(7, HEAD, 2))
        self.assertEqual(st.breach_resume(7, HEAD, 3)['status'], 'awaiting-adjudication')
        self.assertIsNotNone(st.breach_start(7, HEAD, 3), 'the retry can start it again')
