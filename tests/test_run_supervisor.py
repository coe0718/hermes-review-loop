"""Fixture-only lifecycle tests: never invoke a real Hermes agent."""
import concurrent.futures
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from review_loop.run_supervisor import MAX_ATTEMPTS, SILENT, Supervisor



class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "ledger.sqlite"
        self.events = self.root / "launches"
        self.child = self.root / "child.py"
        self.child.write_text("import sys,time\nfrom pathlib import Path\n"
                              "with Path(sys.argv[1]).open('a') as f: f.write('launched\\n')\n"
                              "time.sleep(float(sys.argv[2]))\nsys.exit(int(sys.argv[3]))\n")

    def supervisor(self, delay=0.05, rc=0, **kw):
        return Supervisor(self.db, fixture_mode=True,
                          fixture_command=[sys.executable, str(self.child),
                                           str(self.events), str(delay), str(rc)], **kw)

    def wait(self, sup, delivery, state, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            row = sup.get(delivery)
            if row and row["state"] == state:
                return row
            time.sleep(0.02)
        self.fail(f"{delivery} did not reach {state}: {sup.get(delivery)}")

    def launches(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_production_blocks_without_agent_fallback(self):
        sup = Supervisor(self.db)
        self.assertEqual(sup.enqueue("d", "o/r", 1, "sha", "reviewer"), SILENT)
        self.assertEqual(sup.get("d")["state"], "blocked")
        self.assertEqual(sup.recover(), SILENT)
        self.assertFalse(self.events.exists())
        with self.assertRaises(ValueError):
            Supervisor(self.db, fixture_command=["hermes", "chat"])

    def test_parallel_retries_only_one_launch_and_conflict_fails(self):
        sup = self.supervisor()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: sup.enqueue("d", "o/r", 1, "sha", "reviewer"), range(32)))
        self.assertEqual(results, [SILENT] * len(results))
        self.wait(sup, "d", "succeeded")
        sup.enqueue("other-delivery", "o/r", 1, "sha", "reviewer")
        sup.recover()
        time.sleep(0.1)
        self.assertEqual(len(self.launches()), 1)
        with self.assertRaises(ValueError):
            sup.enqueue("d", "o/r", 2, "sha", "reviewer")

    def test_detached_seat_queue_and_release(self):
        sup = self.supervisor(delay=0.3)
        started = time.monotonic()
        self.assertEqual(sup.enqueue("a", "o/r", 1, "a", "reviewer"), SILENT)
        self.assertLess(time.monotonic() - started, 1)
        self.wait(sup, "a", "running")
        sup.enqueue("b", "o/r", 2, "b", "reviewer")
        self.assertEqual(sup.get("b")["state"], "pending")
        self.wait(sup, "a", "succeeded")
        self.wait(sup, "b", "succeeded")
        self.assertEqual(len(self.launches()), 2)

    def test_child_failure_and_timeout_release_seat(self):
        sup = self.supervisor(rc=7)
        sup.enqueue("bad", "o/r", 1, "a", "reviewer")
        self.assertEqual(self.wait(sup, "bad", "failed")["outcome"], 7)
        slow = self.supervisor(delay=1, child_timeout=0.08)
        slow.enqueue("slow", "o/r", 2, "b", "reviewer")
        self.assertEqual(self.wait(slow, "slow", "failed")["error"], "child timeout")
        ok = self.supervisor()
        ok.enqueue("ok", "o/r", 3, "c", "reviewer")
        self.wait(ok, "ok", "succeeded")

    def test_spawn_failure_remains_recoverable_before_claim(self):
        sup = self.supervisor()
        original = sup._spawn
        sup._spawn = lambda: (_ for _ in ()).throw(OSError("injected"))
        with self.assertRaises(OSError):
            sup.enqueue("d", "o/r", 1, "a", "reviewer")
        self.assertEqual(sup.get("d")["state"], "pending")
        sup._spawn = original
        sup.recover()
        self.wait(sup, "d", "succeeded")

    def test_different_host_home_and_hermes_home_launches(self):
        host = self.root / "host-home"
        hermes = self.root / "hermes-home"
        host.mkdir()
        hermes.mkdir()
        sup = self.supervisor()
        with patch.dict(os.environ, {"HOME": str(host), "HERMES_HOME": str(hermes)}):
            sup.enqueue("split-home", "o/r", 8, "head", "reviewer")
            self.wait(sup, "split-home", "succeeded")
        self.assertEqual(len(self.launches()), 1)

    def test_running_lease_heartbeats_and_completion_after_expiry(self):
        # Sleep well past one lease: only heartbeats keep the run alive. The lease is wide
        # enough that a slow CI runner's SQLite stall does not miss a whole beat.
        sup = self.supervisor(delay=2.0, lease_seconds=0.6, child_timeout=5)
        sup.enqueue("heartbeat", "o/r", 9, "head", "reviewer")
        self.wait(sup, "heartbeat", "running")
        time.sleep(1.0)
        sup.recover()
        self.assertEqual(sup.get("heartbeat")["state"], "running")
        self.wait(sup, "heartbeat", "succeeded")
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='running', lease=0 WHERE delivery='heartbeat'")
        sup.recover()
        self.assertEqual(sup.get("heartbeat")["state"], "uncertain")
        row = sup.get("heartbeat")
        sup.complete_uncertain(row["id"], row["owner"], 0)
        self.assertEqual(sup.get("heartbeat")["state"], "succeeded")

    def test_recovery_retries_prelaunch_but_quarantines_post_intent(self):
        sup = self.supervisor()
        sup.enqueue("before", "o/r", 1, "a", "reviewer")
        self.wait(sup, "before", "succeeded")
        # Inject expired state transitions while preserving a real durable row.
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='claimed', attempts=1, lease=0 WHERE delivery='before'")
        sup.recover()
        self.wait(sup, "before", "succeeded")
        self.assertEqual(len(self.launches()), 2)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='launching', lease=0 WHERE delivery='before'")
        sup.recover()
        self.assertEqual(sup.get("before")["state"], "uncertain")
        sup.enqueue("held", "o/r", 1, "new-head", "fixer")
        time.sleep(0.15)
        self.assertEqual(sup.get("held")["state"], "pending")
        self.assertEqual(len(self.launches()), 2)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='claimed', attempts=?, lease=0 WHERE delivery='held'",
                       (MAX_ATTEMPTS,))
        sup.recover()
        self.assertEqual(sup.get("held")["state"], "failed")

    def test_uncertain_seat_requires_explicit_dead_worker_reconciliation(self):
        sup = self.supervisor()
        sup.enqueue("old", "o/r", 11, "head", "reviewer")
        self.wait(sup, "old", "succeeded")
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='running', pid=?, lease=0 "
                       "WHERE delivery='old'", (os.getpid(),))
        sup.recover()
        old = sup.get("old")
        sup.enqueue("next", "o/r", 12, "head", "reviewer")
        self.assertEqual(sup.get("next")["state"], "pending")
        with self.assertRaises(ValueError):
            sup.reconcile_uncertain(old["id"], reason='inspected')
        with self.assertRaisesRegex(ValueError, 'PID exists'):
            sup.reconcile_uncertain(old["id"], reason='inspected',
                                    acknowledge_no_live_worker=True)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET pid=NULL WHERE id=?", (old["id"],))
        self.assertTrue(sup.reconcile_uncertain(old["id"], reason='confirmed external state',
                        acknowledge_no_live_worker=True))
        self.assertEqual(sup.get("old")["state"], "failed")
        sup.recover()
        self.wait(sup, "next", "succeeded")
        self.assertEqual(len(self.launches()), 2)


class ReviewerClaimConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'ledger.sqlite'
        self.sup = Supervisor(self.db)
        self.sup.enqueue('review', 'o/r', 1, 'a' * 40, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='pending' WHERE delivery='review'")
        # Exercise the production generation gate without launching a worker.
        self.sup.production_config = Path(self.tmp.name) / 'unused-config'
        self.loop = {'repo': 'o/r', 'base': 'main', 'read_token': 'read'}

    def pull(self, base_sha='b' * 40):
        return {'number': 1, 'state': 'open', 'draft': False,
                'base': {'ref': 'main', 'sha': base_sha,
                         'repo': {'full_name': 'o/r'}},
                'head': {'sha': 'a' * 40, 'repo': {'full_name': 'o/r'}}}

    def test_slow_read_does_not_hold_writer_lock_and_competing_claim_is_atomic(self):
        entered, both_entered, release = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        calls = 0
        errors = []
        def slow_api(*args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                entered.set()
                if calls == 2:
                    both_entered.set()
            if not release.wait(5):
                raise TimeoutError('test timed out waiting for release')
            return self.pull()
        def claim():
            try:
                return self.sup._claim()
            except Exception as exc:
                errors.append(exc)
                return None
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=slow_api):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(claim)
                try:
                    self.assertTrue(entered.wait(2))
                    start = time.monotonic()
                    with patch.object(self.sup, '_spawn'):
                        self.sup.enqueue('unrelated', 'o/r', 2, 'c' * 40, 'fixer')
                    self.assertLess(time.monotonic() - start, 2)
                    with sqlite3.connect(self.db, timeout=1) as con:
                        con.execute("UPDATE runs SET state='blocked' WHERE delivery='unrelated'")
                    second = pool.submit(claim)
                    self.assertTrue(both_entered.wait(2))
                finally:
                    release.set()
                claims = [first.result(timeout=5), second.result(timeout=5)]
        self.assertFalse(errors)
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.assertEqual(self.sup.get('review')['state'], 'claimed')
        self.assertEqual(self.sup.get('unrelated')['state'], 'blocked')

    def test_stale_row_changed_during_read_is_not_claimed(self):
        entered, release = threading.Event(), threading.Event()
        def slow_api(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test timed out waiting for release')
            return self.pull()
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=slow_api):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.sup._claim)
                try:
                    self.assertTrue(entered.wait(2))
                    with sqlite3.connect(self.db, timeout=1) as con:
                        con.execute("UPDATE runs SET generation='changed' WHERE delivery='review'")
                finally:
                    release.set()
                self.assertIsNone(future.result(timeout=5))
        self.assertEqual(self.sup.get('review')['state'], 'pending')
        self.assertEqual(self.sup.get('review')['generation'], 'changed')

    def test_unavailable_generation_fails_without_claim(self):
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', return_value=self.pull(base_sha='invalid')):
            self.assertIsNone(self.sup._claim())
        self.assertEqual(self.sup.get('review')['state'], 'failed')
        self.assertEqual(self.sup.get('review')['generation'], None)

    def test_transient_generation_read_remains_pending_then_claims(self):
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=[TimeoutError('temporary'), self.pull()]):
            self.assertIsNone(self.sup._claim())
            self.assertEqual(self.sup.get('review')['state'], 'pending')
            self.assertEqual(self.sup.get('review')['attempts'], 0)
            self.assertIsNotNone(self.sup._claim())
        self.assertEqual(self.sup.get('review')['state'], 'claimed')

    def test_same_delivery_rearms_pending_after_transient_read(self):
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=TimeoutError('temporary')):
            self.assertIsNone(self.sup._claim())
        with patch.object(self.sup, '_spawn') as spawn:
            self.sup.enqueue('review', 'o/r', 1, 'a' * 40, 'reviewer')
            spawn.assert_called_once_with()

    def test_dismissed_same_head_uses_new_turn_but_redelivery_deduplicates(self):
        head = 'a' * 40
        self.sup.enqueue('first-delivery', 'o/r', 2, head, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='succeeded' WHERE delivery='first-delivery'")
        self.sup.enqueue('dismissed-42', 'o/r', 2, head, 'reviewer', turn_key='dismissed:42')
        self.sup.enqueue('dismissed-42-replay', 'o/r', 2, head, 'reviewer', turn_key='dismissed:42')
        with sqlite3.connect(self.db) as con:
            rows = con.execute('SELECT delivery,turn_key FROM runs WHERE pr=2 ORDER BY created,id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({key for _, key in rows}, {'', 'dismissed:42'})

    def test_new_turn_never_bypasses_active_same_pr_run(self):
        self.sup.enqueue('first-delivery', 'o/r', 2, 'a' * 40, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running' WHERE delivery='first-delivery'")
        self.sup.enqueue('dismissed-42', 'o/r', 2, 'a' * 40, 'reviewer', turn_key='dismissed:42')
        self.assertIsNone(self.sup._claim())
        self.assertEqual(self.sup.get('dismissed-42')['state'], 'pending')

    def test_legacy_unique_index_migrates_without_losing_existing_run(self):
        self.sup.enqueue('initial', 'o/r', 4, 'a' * 40, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute('DROP INDEX runs_turn')
            con.execute('CREATE UNIQUE INDEX runs_turn ON runs(repo,pr,head,seat)')
            con.execute('ALTER TABLE runs DROP COLUMN turn_key')
        migrated = Supervisor(self.db)
        migrated.enqueue('dismissed', 'o/r', 4, 'a' * 40, 'reviewer', turn_key='dismissed:42')
        with sqlite3.connect(self.db) as con:
            rows = con.execute('SELECT delivery,turn_key FROM runs WHERE pr=4 ORDER BY delivery').fetchall()
        self.assertEqual(rows, [('dismissed', 'dismissed:42'), ('initial', '')])

    def test_queued_fixer_checks_latest_live_verdict_before_claim(self):
        self.sup.enqueue('fix', 'o/r', 3, 'a' * 40, 'fixer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='pending' WHERE delivery='fix'")
            con.execute("UPDATE runs SET state='blocked' WHERE delivery='review'")
        reviews = [{'id': 41, 'state': 'CHANGES_REQUESTED', 'commit_id': 'a' * 40,
                    'submitted_at': '2026-01-01T00:00:00Z', 'user': {'login': 'review'}},
                   {'id': 42, 'state': 'APPROVED', 'commit_id': 'a' * 40,
                    'submitted_at': '2026-01-01T00:01:00Z', 'user': {'login': 'review'}}]
        loop = {**self.loop, 'reviewers': ['review']}
        with patch('review_loop.config.by_repo', return_value=loop), \
             patch('review_loop.gh.api', return_value=self.pull()), \
             patch('review_loop.gh.reviews', return_value=reviews):
            self.assertIsNone(self.sup._claim())
        self.assertEqual(self.sup.get('fix')['state'], 'cancelled')


if __name__ == "__main__":
    unittest.main()
