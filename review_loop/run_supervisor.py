"""Durable, fail-closed route-run ledger with isolated production worker.

A launch intent is committed before creating a child; ambiguous launches are
quarantined rather than retried. Fixture mode accepts a trusted fake command.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

SILENT = "[SILENT]"
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, delivery TEXT NOT NULL UNIQUE, repo TEXT NOT NULL,
 pr INTEGER NOT NULL, head TEXT NOT NULL, seat TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 owner TEXT, lease REAL, launch_intent REAL, pid INTEGER,
 outcome INTEGER, error TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_seat ON runs(seat,state);
CREATE INDEX IF NOT EXISTS runs_pr ON runs(repo,pr,state);

CREATE TABLE IF NOT EXISTS operator_notices (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), state TEXT NOT NULL,
 created REAL NOT NULL, delivered REAL
);
CREATE TABLE IF NOT EXISTS review_receipts (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), state TEXT NOT NULL,
 generation TEXT NOT NULL, principal_id INTEGER NOT NULL,
 review_id INTEGER, verdict TEXT, created REAL NOT NULL, confirmed REAL
);
"""
ACTIVE = ("claimed", "launching", "running", "uncertain")
MAX_ATTEMPTS = 3
_WORKERS: list[subprocess.Popen] = []


class Supervisor:
    def __init__(self, db: str | Path, *, fixture_command: list[str] | None = None,
                 fixture_mode: bool = False, capacity: dict[str, int] | None = None,
                 lease_seconds: float = 60, child_timeout: float = 120,
                 production_config: str | Path | None = None,
                 hermes_home: str | Path | None = None):
        if fixture_mode and production_config is not None:
            raise ValueError("fixture and production modes are exclusive")
        if production_config is not None and hermes_home is None:
            raise ValueError("production worker requires explicit host HERMES_HOME")
        if fixture_command is not None and (not fixture_mode or not fixture_command):
            raise ValueError("child command requires explicit fixture mode")
        if lease_seconds <= 0 or child_timeout <= 0:
            raise ValueError("positive timeouts required")
        self.db = Path(db)
        self.fixture_command = fixture_command
        self.fixture_mode = fixture_mode
        self.production_config = Path(production_config).resolve(strict=True) if production_config else None
        self.hermes_home = Path(hermes_home).resolve(strict=True) if hermes_home else None
        if self.production_config and (not self.production_config.is_file() or
                self.production_config.stat().st_mode & 0o077):
            raise ValueError("production config must be a private regular file")
        self.capacity = capacity or {"reviewer": 1, "fixer": 1}
        if not self.capacity or any(v < 1 for v in self.capacity.values()):
            raise ValueError("positive seat capacities required")
        self.lease_seconds = lease_seconds
        self.child_timeout = child_timeout
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            con.execute('BEGIN IMMEDIATE')
            if 'generation' not in {r[1] for r in con.execute('PRAGMA table_info(runs)')}:
                con.execute('ALTER TABLE runs ADD COLUMN generation TEXT')
            if 'turn_key' not in {r[1] for r in con.execute('PRAGMA table_info(runs)')}:
                con.execute("ALTER TABLE runs ADD COLUMN turn_key TEXT NOT NULL DEFAULT ''")
                con.execute('DROP INDEX IF EXISTS runs_turn')
                con.execute('CREATE UNIQUE INDEX runs_turn ON runs(repo,pr,head,seat,turn_key)')
            con.execute('COMMIT')

    def _connect(self):
        con = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def get(self, delivery: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE delivery=?", (delivery,)).fetchone()
            return dict(row) if row else None

    def status(self) -> list[dict]:
        """Read-only, bounded operator view; no lease or worker is altered."""
        with self._connect() as con:
            return [dict(row) for row in con.execute(
                "SELECT r.id,r.repo,r.pr,r.head,r.seat,r.state,r.pid,r.error,"
                "r.outcome,n.state AS notice FROM runs r LEFT JOIN operator_notices n "
                "ON n.run_id=r.id WHERE r.state IN ('failed','uncertain') "
                "ORDER BY r.created,r.id LIMIT 100")]

    def notify(self, deliver) -> int:
        """One bounded alert per failed/uncertain run, retried if delivery fails.

        The callback must return only after its transport acknowledges delivery.
        A crash between acknowledgement and the SQLite commit can duplicate a
        notice; the stable run ID lets the receiving operator deduplicate it.
        """
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("INSERT OR IGNORE INTO operator_notices(run_id,state,created) "
                        "SELECT id,'pending',? FROM runs WHERE state IN ('failed','uncertain')",
                        (time.time(),))
            con.execute('COMMIT')
            rows = con.execute("SELECT r.id,r.repo,r.pr,r.head,r.seat,r.state "
                               "FROM operator_notices n JOIN runs r ON r.id=n.run_id "
                               "WHERE n.state='pending' ORDER BY n.created,n.run_id LIMIT 20").fetchall()
        count = 0
        for row in rows:
            message = None
            with self._connect() as con:
                con.execute('BEGIN IMMEDIATE')
                pending = con.execute("SELECT 1 FROM operator_notices WHERE run_id=? "
                                      "AND state='pending'", (row['id'],)).fetchone()
                if pending:
                    current = con.execute("SELECT state FROM runs WHERE id=?",
                                          (row['id'],)).fetchone()
                    if current is None or current['state'] not in ('failed', 'uncertain'):
                        con.execute("UPDATE operator_notices SET state='resolved' WHERE run_id=?",
                                    (row['id'],))
                        con.execute('COMMIT')
                        continue
                    message = (f"⚠️ Review-loop worker {current['state']}: "
                               f"https://github.com/{row['repo']}/pull/{row['pr']} "
                               f"seat={row['seat']} head={row['head']} run={row['id']}. "
                               "Do not replay this turn or release its seat based on a lease alone. "
                               "Inspect the worker PID and external GitHub writes; use "
                               "`python -m review_loop.run_supervisor status DB` and "
                               "`python -m review_loop.run_supervisor reconcile DB RUN_ID "
                               "--reason REASON --acknowledge-no-live-worker` only after "
                               "establishing no worker remains. Failed writes require "
                               "manual inspection before any new turn.")
                    # Claim durably before calling a potentially slow transport.
                    # A crash while sending is ambiguous: leave it for an operator,
                    # rather than replaying a possibly acknowledged notification.
                    con.execute("UPDATE operator_notices SET state='sending' "
                                "WHERE run_id=? AND state='pending'", (row['id'],))
                con.execute('COMMIT')
            if not pending:
                continue
            try:
                deliver(message)
            except Exception:
                with self._connect() as con:
                    con.execute("UPDATE operator_notices SET state='pending' "
                                "WHERE run_id=? AND state='sending'", (row['id'],))
                raise
            with self._connect() as con:
                con.execute("UPDATE operator_notices SET state='delivered', delivered=? "
                            "WHERE run_id=? AND state='sending'", (time.time(), row['id']))
            count += 1
        return count

    def enqueue(self, delivery: str, repo: str, pr: int, head: str, seat: str,
                *, turn_key: str = '') -> str:
        """Commit identity before any spawn. A repeated delivery cannot change terms."""
        if not all(isinstance(v, str) and v and len(v) <= 256 for v in
                   (delivery, repo, head, seat)) or not isinstance(turn_key, str) or len(turn_key) > 256 or type(pr) is not int or pr <= 0:
            raise ValueError("invalid run identity")
        if seat not in self.capacity:
            raise ValueError("unconfigured seat")
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT * FROM runs WHERE delivery=?", (delivery,)).fetchone()
            if prior:
                if (prior["repo"], prior["pr"], prior["head"], prior["seat"], prior['turn_key']) != (repo, pr, head, seat, turn_key):
                    raise ValueError("delivery identity collision")
            else:
                prior = con.execute("SELECT * FROM runs WHERE repo=? AND pr=? AND head=? AND seat=? AND turn_key=?",
                                    (repo, pr, head, seat, turn_key)).fetchone()
                if not prior:
                    con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,created,updated) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (uuid.uuid4().hex, delivery, repo, pr, head, seat, turn_key,
                                 "pending" if self.fixture_mode or self.production_config else "blocked", now, now))
            con.execute("COMMIT")
        # A redelivery of an unclaimed run must rearm the worker after a
        # transient generation/read outage; active or completed runs stay deduped.
        if (self.fixture_mode or self.production_config) and (not prior or prior['state'] == 'pending'):
            self._spawn()
        return SILENT

    def _spawn(self):
        # Trusted supervisor process only, never the credential-owning gateway agent.
        if not self.fixture_mode and not self.production_config:
            return
        operation = "_fixture-worker" if self.fixture_mode else "_production-worker"
        command = json.dumps(self.fixture_command) if self.fixture_mode else str(self.production_config)
        args = [sys.executable, "-m", "review_loop.run_supervisor", operation,
                str(self.db), command, json.dumps(self.capacity),
                str(self.lease_seconds), str(self.child_timeout)]
        host_home = self.hermes_home or Path(os.environ.get("HERMES_HOME", os.environ["HOME"])).resolve(strict=True)
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
               "HOME": str(host_home), "HERMES_HOME": str(host_home),
               "REVIEW_LOOP_TEST_FIXTURE": "1" if self.fixture_mode else "0"}
        if os.environ.get("REVIEW_LOOP_GH_STUB") and self.fixture_mode:
            env["REVIEW_LOOP_GH_STUB"] = os.environ["REVIEW_LOOP_GH_STUB"]
        _WORKERS[:] = [worker for worker in _WORKERS if worker.poll() is None]
        _WORKERS.append(subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         close_fds=True, start_new_session=True))

    def recover(self) -> str:
        """Sweep lost claims and ambiguous launches; schedule waiting work."""
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # Never retry an attempt whose child may have been launched.
            con.execute("UPDATE runs SET state='uncertain', error='worker lost after launch intent', "
                        "updated=? WHERE state IN ('launching','running') AND lease<?",
                        (now, now))
            con.execute("UPDATE runs SET state='pending', owner=NULL, lease=NULL, updated=? "
                        "WHERE state='claimed' AND lease<? AND attempts<?",
                        (now, now, MAX_ATTEMPTS))
            con.execute("UPDATE runs SET state='failed', error='claim retry limit', "
                        "owner=NULL, lease=NULL, updated=? WHERE state='claimed' "
                        "AND lease<? AND attempts>=?",
                        (now, now, MAX_ATTEMPTS))
            pending = con.execute("SELECT COUNT(*) FROM runs WHERE state='pending'").fetchone()[0]
            con.execute("COMMIT")
        if pending and (self.fixture_mode or self.production_config):
            self._spawn()
        return SILENT

    def _claim(self):
        # Snapshot candidates without taking a writer lock. A slow GitHub read
        # must not block unrelated enqueues, heartbeats, or receipt commits.
        with self._connect() as con:
            candidates = con.execute("SELECT * FROM runs WHERE state='pending' "
                                     "ORDER BY created,id").fetchall()
        for row in candidates:
            generation = None
            unavailable = False
            retry_read = False
            superseded = False
            if self.production_config and row['seat'] == 'reviewer':
                from . import config, gh
                from .review_receipt import ReceiptDenied, generation_for
                try:
                    loop = config.by_repo(row['repo'])
                    if loop is None:
                        unavailable = True
                    else:
                        pr = gh.api(loop, f"/repos/{row['repo']}/pulls/{row['pr']}",
                                    login=loop['read_token'])
                        generation = generation_for(pr, loop, row['pr'], row['head'])
                except ReceiptDenied:
                    unavailable = True
                except Exception:
                    retry_read = True
            if self.production_config and row['seat'] == 'fixer':
                from . import config, gh, gate
                try:
                    loop = config.by_repo(row['repo'])
                    if loop is None:
                        retry_read = True
                    else:
                        pr = gh.api(loop, f"/repos/{row['repo']}/pulls/{row['pr']}",
                                    login=loop['read_token'])
                        if not isinstance(pr, dict) or not isinstance(pr.get('head'), dict):
                            retry_read = True
                        elif pr['head'].get('sha') != row['head'] or pr.get('state') == 'closed':
                            superseded = True
                        elif pr.get('state') != 'open' or pr.get('draft') is not False:
                            retry_read = True
                        else:
                            reviews = gh.reviews(loop, row['pr'])
                            if not isinstance(reviews, list):
                                retry_read = True
                            else:
                                latest = gate.latest_effective_review_at_head(reviews, loop, row['head'])
                                if latest is None:
                                    retry_read = True
                                elif gh.review_state(latest) != 'CHANGES_REQUESTED':
                                    superseded = True
                except Exception:
                    retry_read = True
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                current = con.execute("SELECT * FROM runs WHERE id=?", (row['id'],)).fetchone()
                # Another claimant, recovery, or a changed generation invalidates
                # the read; never bind a resolved receipt to different row terms.
                if current is None or dict(current) != dict(row):
                    con.execute("COMMIT")
                    continue
                # Recheck both capacity and PR occupancy under the writer lock.
                occupied = con.execute("SELECT 1 FROM runs WHERE repo=? AND pr=? "
                                       "AND state IN ('claimed','launching','running','uncertain')",
                                       (row['repo'], row['pr'])).fetchone()
                used = con.execute("SELECT COUNT(*) FROM runs WHERE seat=? AND "
                                   "state IN ('claimed','launching','running','uncertain')",
                                   (row['seat'],)).fetchone()[0]
                if occupied or used >= self.capacity[row['seat']]:
                    con.execute("COMMIT")
                    continue
                now = time.time()
                if superseded:
                    con.execute("UPDATE runs SET state='cancelled', error='fixer verdict superseded',updated=? WHERE id=?",
                                (now, row['id']))
                    con.execute('COMMIT')
                    continue
                if retry_read:
                    con.execute('COMMIT')
                    continue
                if unavailable:
                    con.execute("UPDATE runs SET state='failed', attempts=attempts+1, "
                                "error='review generation unavailable',updated=? WHERE id=?",
                                (now, row['id']))
                    con.execute("COMMIT")
                    continue
                owner = uuid.uuid4().hex
                con.execute("UPDATE runs SET state='claimed', attempts=attempts+1, "
                            "owner=?, lease=?, updated=?, generation=? WHERE id=?",
                            (owner, now + self.lease_seconds, now, generation, row['id']))
                con.execute("COMMIT")
                return row['id'], owner
        return None

    def _heartbeat(self, run_id: str, owner: str, stop: threading.Event) -> None:
        # Only the owning worker can extend a live lease. An expired lease is
        # never silently revived after recovery has quarantined the turn.
        while not stop.wait(max(0.01, min(self.lease_seconds / 3, 5))):
            try:
                with self._connect() as con:
                    now = time.time()
                    con.execute("UPDATE runs SET lease=?, updated=? WHERE id=? AND owner=? "
                                "AND state IN ('launching','running') AND lease>=?",
                                (now + self.lease_seconds, now, run_id, owner, now))
            except sqlite3.Error:
                # Recovery will quarantine this run if persistence stays down.
                pass

    def complete_uncertain(self, run_id: str, owner: str, rc: int | None,
                           error: str | None = None, *, stopped: bool = True) -> None:
        """Record direct worker completion, including after lease expiry.

        Never use this for operator guesswork: only the owner after its child
        has stopped may call it. A lost worker remains uncertain until manual
        reconciliation, and is never automatically retried.
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            ambiguous = con.execute("SELECT 1 FROM review_receipts WHERE run_id=? AND state='claimed'",
                                    (run_id,)).fetchone()
            con.execute("UPDATE runs SET state=?, outcome=?, error=?, lease=NULL, "
                        "updated=? WHERE id=? AND owner=? AND state IN "
                        "('launching','running','uncertain')",
                        ("uncertain" if not stopped or ambiguous else
                         "succeeded" if rc == 0 and error is None else "failed",
                         rc, error, time.time(), run_id, owner))
            con.execute("COMMIT")

    def reconcile_uncertain(self, run_id: str, *, reason: str,
                            acknowledge_no_live_worker: bool = False) -> bool:
        """Operator-only release after inspecting the turn's external writes.

        A present or inaccessible PID conservatively blocks release. A missing
        PID still requires acknowledgement: absence alone cannot prove that
        a GitHub write did not happen before the worker died.
        """
        if not acknowledge_no_live_worker or not reason or len(reason) > 512:
            raise ValueError('explicit reconciliation acknowledgement and reason required')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("SELECT pid,state,launch_intent FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row['state'] != 'uncertain':
                con.execute('COMMIT')
                return False
            if row['pid'] is not None:
                try:
                    os.kill(row['pid'], 0)
                except OSError as exc:
                    if exc.errno != errno.ESRCH:
                        raise ValueError('cannot establish worker is absent') from exc
                else:
                    raise ValueError('worker PID exists; cannot release uncertain run')
            if row['launch_intent'] is None:
                raise ValueError('missing launch intent; cannot establish worker identity')
            con.execute("UPDATE runs SET state='failed', error=?, lease=NULL, updated=? "
                        "WHERE id=? AND state='uncertain'",
                        ('operator reconciliation: ' + reason, time.time(), run_id))
            con.execute('COMMIT')
            return True

    def _run_one(self):
        claim = self._claim()
        if not claim:
            return
        run_id, owner = claim
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE runs SET state='launching', launch_intent=?, lease=?, "
                        "updated=? WHERE id=? AND owner=? AND state='claimed'",
                        (time.time(), time.time() + self.child_timeout + self.lease_seconds,
                         time.time(), run_id, owner))
            con.execute("COMMIT")
        # From here onward recovery must NEVER launch this job again.
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat,
                                     args=(run_id, owner, heartbeat_stop), daemon=True)
        heartbeat.start()
        try:
            if self.production_config is not None:
                self._run_production(run_id, owner)
                return
            self._run_fixture(run_id, owner)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)

    def _run_fixture(self, run_id: str, owner: str) -> None:
        assert self.fixture_command is not None
        child = None
        rc = None
        error = None
        stopped = True
        try:
            child = subprocess.Popen(self.fixture_command,
                                     env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"],
                                          "HERMES_HOME": os.environ["HERMES_HOME"]},
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, close_fds=True,
                                     start_new_session=True)
            with self._connect() as con:
                con.execute("UPDATE runs SET state='running', pid=?, lease=?, updated=? "
                            "WHERE id=? AND owner=? AND state='launching'",
                            (child.pid, time.time() + self.lease_seconds, time.time(), run_id, owner))
            try:
                rc = child.wait(timeout=self.child_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                error = "child timeout"
        except Exception as exc:
            error = f"launch/wait failed: {type(exc).__name__}: {exc}"
            if child and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                except OSError:
                    stopped = False
        finally:
            # A failed launch remains failed, not retryable: spawn may have occurred.
            self.complete_uncertain(run_id, owner, rc, error, stopped=stopped)
            # A completed child releases the seat and allows waiting work to advance.
            self.recover()


    def _run_production(self, run_id: str, owner: str) -> None:
        """Worker-only host control plane; never pass credentials to bwrap."""
        from . import broker_ipc, config, gh, trusted_turn
        rc, error = None, None
        try:
            assert self.production_config is not None
            settings = json.loads(self.production_config.read_text())
            required = {"source", "venv", "runtime", "rust", "upstream", "key_file", "model"}
            if not isinstance(settings, dict) or set(settings) != required:
                raise ValueError("invalid production configuration")
            if not settings["upstream"].startswith("https://"):
                raise ValueError("production inference requires HTTPS")
            key_path = Path(settings["key_file"])
            if not key_path.is_file() or key_path.stat().st_mode & 0o077:
                raise ValueError("model key file must be private")
            with self._connect() as con:
                row = con.execute("SELECT * FROM runs WHERE id=? AND owner=?", (run_id, owner)).fetchone()
                if row is None:
                    raise ValueError("run ownership lost")
                con.execute("UPDATE runs SET state='running', pid=?, lease=?, updated=? "
                            "WHERE id=? AND owner=? AND state='launching'",
                            (os.getpid(), time.time() + self.lease_seconds, time.time(), run_id, owner))
            loop = config.by_repo(row["repo"])
            if loop is None:
                raise ValueError("loop not configured")
            reader = loop["read_token"]
            pr = gh.api(loop, f'/repos/{row["repo"]}/pulls/{row["pr"]}', login=reader)
            head = pr.get("head") if isinstance(pr, dict) else None
            if not isinstance(head, dict) or head.get("sha") != row["head"]:
                raise ValueError("PR head moved")
            if row['seat'] == 'reviewer' and not row['generation']:
                raise ValueError('review generation not durably pinned')
            if row['seat'] == 'fixer':
                from . import gate
                reviews = gh.reviews(loop, row['pr'])
                latest = (gate.latest_effective_review_at_head(reviews, loop, row['head'])
                          if isinstance(reviews, list) else None)
                if latest is None or gh.review_state(latest) != 'CHANGES_REQUESTED':
                    raise ValueError('fixer verdict no longer current')
            scope = broker_ipc.RunScope(row["repo"], row["pr"], row["head"],
                                        row["seat"], head["ref"], row['id'],
                                        str(self.db), row['generation'])
            prompt = (f'You are the {scope.role} for {scope.repo} PR #{scope.number} '
                      f'at head {scope.head}. Inspect the checkout under /work and '
                      'perform exactly one scoped review or correction. Do not access '
                      'other repos or host paths. Provide a truthful outcome.')
            rc = trusted_turn.run_turn(loop, scope, source=Path(settings["source"]),
                  venv=Path(settings["venv"]), runtime=Path(settings["runtime"]),
                  rust=Path(settings["rust"]), upstream=settings["upstream"],
                  key=key_path.read_text().strip(), model=settings["model"],
                  prompt=prompt, timeout=int(self.child_timeout),
                  work_root=Path(loop["state_dir"]) / "isolated-runs")
        except Exception as exc:
            error = f"isolated turn failed: {type(exc).__name__}"
        finally:
            self.complete_uncertain(run_id, owner, rc, error)
            self.recover()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("operation", choices=["_fixture-worker", "_production-worker",
                                         "status", "sweep", "reconcile"])
    p.add_argument("db")
    p.add_argument("command", nargs='?')
    p.add_argument("capacity", nargs='?')
    p.add_argument("lease", type=float, nargs='?')
    p.add_argument("timeout", type=float, nargs='?')
    p.add_argument('--reason')
    p.add_argument('--acknowledge-no-live-worker', action='store_true')
    a = p.parse_args()
    if a.operation in ('status', 'sweep', 'reconcile'):
        sup = Supervisor(a.db)
        if a.operation == 'status':
            if a.command or a.reason or a.acknowledge_no_live_worker:
                p.error('unexpected status arguments')
            print(json.dumps(sup.status(), sort_keys=True))
        elif a.operation == 'sweep':
            if a.command or a.reason or a.acknowledge_no_live_worker:
                p.error('unexpected sweep arguments')
            sup.recover()  # no production configuration: cannot launch waiting work
            sup.notify(lambda message: print(message, flush=True))
        else:
            if not a.command or not a.reason or not a.acknowledge_no_live_worker:
                p.error('reconcile requires run ID, reason and explicit acknowledgement')
            changed = sup.reconcile_uncertain(a.command, reason=a.reason,
                        acknowledge_no_live_worker=True)
            print('reconciled' if changed else 'unchanged')
        return
    if a.command is None or a.capacity is None or a.lease is None or a.timeout is None:
        p.error('worker requires command, capacity, lease and timeout')
    if a.operation == "_fixture-worker":
        if os.environ.get("REVIEW_LOOP_TEST_FIXTURE") != "1":
            raise SystemExit("fixture worker disabled")
        sup = Supervisor(a.db, fixture_mode=True, fixture_command=json.loads(a.command),
                         capacity=json.loads(a.capacity), lease_seconds=a.lease,
                         child_timeout=a.timeout)
    else:
        home = os.environ.get("HERMES_HOME")
        if not home or os.environ.get("REVIEW_LOOP_TEST_FIXTURE") != "0":
            raise SystemExit("production worker requires explicit host home")
        sup = Supervisor(a.db, production_config=a.command, hermes_home=home,
                         capacity=json.loads(a.capacity), lease_seconds=a.lease,
                         child_timeout=a.timeout)
    sup._run_one()


if __name__ == "__main__":
    main()
