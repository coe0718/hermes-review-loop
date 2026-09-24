"""Per-loop state on disk: seat locks, the queue, in-flight marks, breach markers, and the
observer's delivery ledger.

All of it lives under the loop's own ``state_dir`` (default
``~/.hermes/state/review-loops/<id>/``), so two loops never share a file and a loop can
be deleted by removing one directory.

Two rules the shapes below encode:

* **A seat is a capacity, not a mutex.** ``concurrency`` says how many PRs that seat may work at
  once (1 = serialized). The ledger is keyed by PR so one PR can never run twice, and it expires,
  because a crashed run must not wedge a loop forever.
* **One wake per head.** Every marker is keyed by PR *and* head sha: a new commit is a new
  situation, the same commit is not.

Every file is written atomically (temp file, fsync, ``os.replace``) and every read-modify-write
of the ledger, the queue and the in-flight marks happens under one per-loop ``flock``. Gates run
as separate processes per webhook; without both, a reader could see a half-written file, fall
back to ``{}``, and the next save would silently drop every other PR's claim.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import tempfile
import threading
import time
import uuid
from collections.abc import Callable

from . import config
from .util import log

# Per-thread depth of the state lock we already hold, keyed by lock path. ``flock`` is tied to
# the open file description, so a second ``open`` + ``flock`` in the same thread would deadlock
# against itself; nested sections (``take_seat`` queueing under its own claim) reuse the outer one.
_HELD = threading.local()


def _atomic_write(path: pathlib.Path, data) -> None:
    """Publish ``data`` to ``path`` whole or not at all, and durably before returning."""
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.stem}-",
                                         delete=False) as file:
            name = file.name
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class LoopState:
    def __init__(self, loop: dict):
        self.loop = loop
        self.dir = pathlib.Path(str(loop["state_dir"])).expanduser()
        self.locks = self.dir / "locks.json"
        self.pending = self.dir / "pending.json"
        self.inflight_file = self.dir / "inflight.json"
        self.breach = self.dir / "breach.json"
        self.observations = self.dir / "observations.json"
        self.watch_file = self.dir / "watchdog.json"
        self.log = self.dir / "watchdog.log"

    # -- raw ----------------------------------------------------------------

    def _load(self, path: pathlib.Path, default):
        try:
            return json.loads(path.read_text()) if path.exists() else default
        except Exception:
            return default

    def _save(self, path: pathlib.Path, data) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, data)
        except Exception as exc:
            log(f"state write failed ({path.name}): {exc}")

    @contextlib.contextmanager
    def locked(self):
        """Hold the loop's state lock across a read-modify-write of locks/pending/inflight.

        One lock for all three files, because the gate's claim spans them (check the ledger,
        write the ledger, drop the queue entry) and must be one step to every other process.
        Reentrant within a thread, so a locked caller can use the ordinary methods.
        """
        path = str(self.dir / "state.lock")
        held = getattr(_HELD, "paths", None)
        if held is None:
            held = _HELD.paths = {}
        if held.get(path):
            held[path] += 1
            try:
                yield
            finally:
                held[path] -= 1
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[path] = 1
            try:
                yield
            finally:
                held.pop(path, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def note(self, message: str) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.log.open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
        except Exception:
            pass

    # -- active runs per seat (the concurrency ledger) ----------------------

    def live_locks(self, seat: str) -> dict:
        """A seat's unexpired entries, read *without* the pruning ``active`` persists.

        ``explain`` is read-only down to the state files: adopting ``active`` there would rewrite
        ``locks.json`` on every question the operator asks, which is a mutation nobody asked for
        and exactly what the acceptance test for a read-only command looks at.
        """
        entries = (self._load(self.locks, {}) or {}).get(seat) or {}
        ttl = self.loop["ttl_min"] * 60
        now = time.time()
        return {k: v for k, v in entries.items()
                if isinstance(v, dict) and now - v.get("at", 0) <= ttl}

    def active(self, seat: str) -> dict:
        """This seat's live runs, ``{key: entry}``, expired ones dropped and persisted away.

        The ledger is per *seat* and keyed by PR, because isolation is per *PR*: two PRs may run
        at once when ``concurrency`` allows it, but the same PR never runs twice.
        """
        with self.locked():
            data = self._load(self.locks, {}) or {}
            entries = data.get(seat) or {}
            live = self.live_locks(seat)
            if live != entries:
                if live:
                    data[seat] = live
                else:
                    data.pop(seat, None)
                self._save(self.locks, data)
            return live

    def active_count(self, seat: str) -> int:
        return len(self.active(seat))

    def is_active(self, seat: str, key: str) -> bool:
        return key in self.active(seat)

    def held_by_other(self, seat: str, key: str) -> str | None:
        """The other seat's name if it is working this PR right now, else ``None``.

        One PR belongs to **one seat at a time**: a review must never run against a PR the fixer
        is mid-fix on, and a fix must not start on a PR under review. This is a PR-level claim
        that sits under the per-seat capacities, not a replacement for them — capacity says how
        many PRs a seat may hold, this says a single PR may not be held by both.

        It is deliberately keyed on PR rather than head: two different heads of the same PR are
        still the same checkout's worth of trouble.
        """
        for other in ("reviewer", "fixer"):
            if other != seat and key in self.active(other):
                return other
        return None

    def acquire(self, seat: str, key: str, head: str = "", why: str = "") -> None:
        with self.locked():
            data = self._load(self.locks, {}) or {}
            data.setdefault(seat, {})[key] = {"at": time.time(), "head": head, "why": why}
            self._save(self.locks, data)

    def release_if(self, seat: str, key: str) -> bool:
        """Free a seat only for *this* PR's turn — never another PR's in-flight work."""
        with self.locked():
            data = self._load(self.locks, {}) or {}
            if (data.get(seat) or {}).pop(key, None) is None:
                return False
            if not data[seat]:
                data.pop(seat, None)
            self._save(self.locks, data)
            return True

    def release_all(self, seat: str) -> int:
        with self.locked():
            data = self._load(self.locks, {}) or {}
            count = len(data.pop(seat, {}) or {})
            if count:
                self._save(self.locks, data)
            return count

    # -- queue --------------------------------------------------------------

    def queue_add(self, seat: str, key: str, head: str, url: str, reason: str) -> None:
        with self.locked():
            data = self._load(self.pending, {}) or {}
            data.setdefault(seat, {})[key] = {"at": time.time(), "head": head, "url": url,
                                              "reason": reason}
            self._save(self.pending, data)

    def queue_items(self, seat: str) -> dict:
        return (self._load(self.pending, {}) or {}).get(seat) or {}

    def queue_all(self) -> dict:
        return self._load(self.pending, {}) or {}

    def queue_pop(self, seat: str, key: str) -> None:
        with self.locked():
            data = self._load(self.pending, {}) or {}
            items = data.get(seat) or {}
            if items.pop(key, None) is not None:
                if not items:
                    data.pop(seat, None)
                self._save(self.pending, data)

    # -- in-flight marks ----------------------------------------------------

    def inflight(self, key: str, record: bool = False) -> bool:
        """Has a run for this exact head already been armed, and is it still plausibly out?

        Guards the burst the platform cannot see: several events for the same head arriving
        before the first verdict lands. TTL-bounded so a crashed run cannot wedge the head.
        """
        now = time.time()
        if record:
            with self.locked():
                data = self._load(self.inflight_file, {}) or {}
                data[key] = now
                data = {k: v for k, v in data.items() if now - v < 24 * 3600}
                self._save(self.inflight_file, data)
            return False
        data = self._load(self.inflight_file, {}) or {}
        return now - data.get(key, 0) < self.loop["inflight_ttl_min"] * 60

    def inflight_at(self, key: str) -> float:
        """When this head's in-flight mark was armed, or 0.0 — the mark's own clock, read-only.

        ``inflight()`` answers yes/no; an operator asking "how long has this been out" needs the
        timestamp, and recomputing the TTL comparison anywhere else would be a second rule.
        """
        return float((self._load(self.inflight_file, {}) or {}).get(key, 0) or 0)

    # -- breach markers -----------------------------------------------------

    @contextlib.contextmanager
    def _breach_lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.dir / "breach.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _breach_save(self, data: dict) -> None:
        """Persist a claim before releasing the lock or allowing the route to fire.

        Unlike ``_save`` this raises: a breach claim that did not reach disk must not fire.
        """
        _atomic_write(self.breach, data)

    def breach_get(self, number: int) -> dict:
        return (self._load(self.breach, {}) or {}).get(f"{self.loop['repo']}#{number}") or {}

    def breach_set(self, number: int, entry: dict) -> dict:
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            prior = data.get(key) or {}
            # Duplicate cap events cannot re-arm an already claimed head.
            if prior.get("head") == entry.get("head"):
                return prior
            data[key] = entry
            self._breach_save(data)
            return prior

    def breach_deliver(self, number: int, entry: dict, current: Callable[[], bool],
                       send: Callable[[dict], bool], reserved: Callable[[dict], None] | None = None) -> str:
        """Reserve a pending delivery under lock, then POST without holding it.

        A synchronous gateway must be able to claim the marker before answering
        the POST. The attempt token prevents concurrent deliveries; its lease lets
        a watchdog retry if the sender dies. Finalization is compare-and-swap so
        a late response cannot overwrite a newer head or a claimed wake.
        """
        key = f"{self.loop['repo']}#{number}"
        head = entry["head"]
        with self._breach_lock():
            if not current():
                return "stale"
            data = self._load(self.breach, {}) or {}
            prior = data.get(key) or {}
            new = prior.get("head") != head
            if not new and (prior.get("status") != "delivery-pending"
                            or (prior.get("delivery_token")
                                and time.time() - prior.get("delivery_at", 0) < 60)):
                return "already"
            # Keep the original reason/rounds when retrying a pending marker.
            marker = {**(entry if new else prior), "status": "delivery-pending",
                      "delivery_token": uuid.uuid4().hex, "delivery_at": time.time()}
            data[key] = marker
            self._breach_save(data)
        if reserved:
            try:
                reserved(marker)
            except Exception as exc:
                log(f"breach observer notification failed: {exc}")
        try:
            delivered = send(marker)
        except Exception as exc:
            log(f"adjudicator delivery failed: {exc}")
            delivered = False
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            latest = data.get(key) or {}
            if (latest.get("head") == head
                    and latest.get("delivery_token") == marker["delivery_token"]):
                latest = {k: v for k, v in latest.items()
                          if k not in {"delivery_token", "delivery_at"}}
                if latest.get("status") == "delivery-pending" and delivered:
                    latest["status"] = "awaiting-adjudication"
                # A gateway may already have moved this marker to adjudicating.
                data[key] = latest
                self._breach_save(data)
        return "new" if new else "retry"


    def breach_claim(self, number: int, head: str) -> dict | None:
        """Claim exactly one wake for this PR/head across gateway processes."""
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head
                    or not (marker.get("status") == "awaiting-adjudication"
                            or (marker.get("status") == "delivery-pending"
                                and marker.get("delivery_token")))):
                return None
            data[key] = {**marker, "status": "adjudicating"}
            self._breach_save(data)
            return marker

    def breach_all(self) -> dict:
        return self._load(self.breach, {}) or {}

    # -- watchdog memory ----------------------------------------------------

    def watch(self) -> dict:
        return self._load(self.watch_file, {}) or {}

    def watch_save(self, data: dict) -> None:
        self._save(self.watch_file, data)


def state_for(loop: dict) -> LoopState:
    return LoopState(loop)


def artifacts_path(loop: dict, number: int) -> pathlib.Path:
    return config.artifacts_dir(loop, number)
