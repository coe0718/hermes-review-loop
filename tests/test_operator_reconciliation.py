"""Real SQLite operator outbox and no-replay recovery tests."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from review_loop.run_supervisor import Supervisor


class OperatorReconciliation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'runs.sqlite'
        self.sup = Supervisor(self.db, fixture_mode=True,
                              fixture_command=[sys.executable, '-c', 'pass'])
        # Suppress automatic fixture launches: inject durable crash snapshots.
        self.sup._spawn = lambda: None
        self.sup.enqueue('d', 'owner/repo', 21, 'abc123', 'reviewer')
        self.row = self.sup.get('d')
        assert self.row is not None

    def crash(self, state='running', pid=None):
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE runs SET state=?,owner=?,attempts=1,lease=0, '
                        'launch_intent=1,pid=? WHERE id=?',
                        (state, 'lost-owner', pid, self.row['id']))
        self.sup.recover()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'review_loop.run_supervisor',
                               *args], text=True, capture_output=True, check=True)

    def test_crash_stale_lease_failed_notification_repeated_sweep_and_reconcile(self):
        self.crash()
        self.assertEqual(self.sup.get('d')['state'], 'uncertain')
        self.sup.enqueue('waiting', 'owner/repo', 22, 'new', 'reviewer')
        self.assertEqual(self.sup.get('waiting')['state'], 'pending')
        def fail(_):
            raise OSError('delivery failed')
        with self.assertRaises(OSError):
            self.sup.notify(fail)
        self.assertEqual(self.sup.status()[0]['notice'], 'pending')
        output = self.cli('sweep', str(self.db)).stdout
        for value in ('owner/repo/pull/21', 'seat=reviewer', 'head=abc123',
                      'run=' + self.row['id'], 'Do not replay'):
            self.assertIn(value, output)
        self.assertEqual(self.cli('sweep', str(self.db)).stdout, '')
        status = json.loads(self.cli('status', str(self.db)).stdout)
        self.assertEqual(status[0]['notice'], 'delivered')
        self.assertEqual(self.sup.get('waiting')['state'], 'pending')
        self.assertEqual(self.cli('reconcile', str(self.db), self.row['id'],
                                  '--reason', 'inspected external writes',
                                  '--acknowledge-no-live-worker').stdout.strip(), 'reconciled')
        self.assertEqual(self.cli('reconcile', str(self.db), self.row['id'],
                                  '--reason', 'inspected external writes',
                                  '--acknowledge-no-live-worker').stdout.strip(), 'unchanged')
        self.assertEqual(self.sup.get('d')['state'], 'failed')
        self.assertEqual(self.sup.get('waiting')['state'], 'pending')

    def test_live_pid_blocks_reconciliation_even_after_stale_lease(self):
        self.crash(pid=os.getpid())
        with self.assertRaisesRegex(ValueError, 'PID exists'):
            self.sup.reconcile_uncertain(self.row['id'], reason='inspected',
                                         acknowledge_no_live_worker=True)
        self.assertEqual(self.sup.get('d')['state'], 'uncertain')

    def test_failed_worker_alert_is_single_per_run(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', error='child exit 7' WHERE id=?",
                        (self.row['id'],))
        notices = []
        self.assertEqual(self.sup.notify(notices.append), 1)
        self.assertEqual(self.sup.notify(notices.append), 0)
        self.assertEqual(len(notices), 1)
        self.assertIn('worker failed', notices[0])

    def test_late_owner_completion_cancels_pending_uncertain_notice(self):
        self.crash()
        with self.assertRaises(OSError):
            self.sup.notify(lambda _: (_ for _ in ()).throw(OSError('offline')))
        self.sup.complete_uncertain(self.row['id'], 'lost-owner', 0)
        notices = []
        self.assertEqual(self.sup.notify(notices.append), 0)
        self.assertEqual(notices, [])
        with sqlite3.connect(self.db) as con:
            state = con.execute('SELECT state FROM operator_notices WHERE run_id=?',
                                (self.row['id'],)).fetchone()[0]
        self.assertEqual(state, 'resolved')


if __name__ == '__main__':
    unittest.main()
