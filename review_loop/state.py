"""Per-loop state on disk: seat locks, the queue, in-flight marks, breach markers.

All of it lives under the loop's own ``state_dir`` (default
``~/.hermes/state/review-loops/<id>/``), so two loops never share a file and a loop can
be deleted by removing one directory.

Two rules the shapes below encode:

* **A seat is a person-sized resource.** A run holds a lock while it works; anything else
  waits in the queue. Locks expire, because a crashed run must not wedge a loop forever.
* **One wake per head.** Every marker is keyed by PR *and* head sha: a new commit is a new
  situation, the same commit is not.
"""

from __future__ import annotations

import json
import pathlib
import time

from . import config
from .util import log


class LoopState:
    def __init__(self, loop: dict):
        self.loop = loop
        self.dir = pathlib.Path(str(loop["state_dir"])).expanduser()
        self.locks = self.dir / "locks.json"
        self.pending = self.dir / "pending.json"
        self.inflight_file = self.dir / "inflight.json"
        self.breach = self.dir / "breach.json"
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
            path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            log(f"state write failed ({path.name}): {exc}")

    def note(self, message: str) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.log.open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
        except Exception:
            pass

    # -- seat locks ---------------------------------------------------------

    def seat_free(self, seat: str) -> bool:
        entry = (self._load(self.locks, {}) or {}).get(seat)
        if not entry:
            return True
        return (time.time() - entry.get("at", 0)) > self.loop["ttl_min"] * 60

    def seat_holder(self, seat: str) -> dict:
        return (self._load(self.locks, {}) or {}).get(seat) or {}

    def seat_acquire(self, seat: str, key: str, why: str) -> None:
        data = self._load(self.locks, {}) or {}
        data[seat] = {"at": time.time(), "key": key, "why": why}
        self._save(self.locks, data)

    def seat_release(self, seat: str) -> bool:
        data = self._load(self.locks, {}) or {}
        if data.pop(seat, None) is None:
            return False
        self._save(self.locks, data)
        return True

    def seat_release_if(self, seat: str, key: str) -> bool:
        """Release a seat only when the lock is *this* PR's turn — never another PR's work."""
        data = self._load(self.locks, {}) or {}
        if (data.get(seat) or {}).get("key") != key:
            return False
        data.pop(seat, None)
        self._save(self.locks, data)
        return True

    # -- queue --------------------------------------------------------------

    def queue_add(self, seat: str, key: str, head: str, url: str, reason: str) -> None:
        data = self._load(self.pending, {}) or {}
        data.setdefault(seat, {})[key] = {"at": time.time(), "head": head, "url": url,
                                          "reason": reason}
        self._save(self.pending, data)

    def queue_items(self, seat: str) -> dict:
        return (self._load(self.pending, {}) or {}).get(seat) or {}

    def queue_all(self) -> dict:
        return self._load(self.pending, {}) or {}

    def queue_pop(self, seat: str, key: str) -> None:
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
        data = self._load(self.inflight_file, {}) or {}
        now = time.time()
        if record:
            data[key] = now
            data = {k: v for k, v in data.items() if now - v < 24 * 3600}
            self._save(self.inflight_file, data)
            return False
        return now - data.get(key, 0) < self.loop["inflight_ttl_min"] * 60

    # -- breach markers -----------------------------------------------------

    def breach_get(self, number: int) -> dict:
        return (self._load(self.breach, {}) or {}).get(f"{self.loop['repo']}#{number}") or {}

    def breach_set(self, number: int, entry: dict) -> dict:
        data = self._load(self.breach, {}) or {}
        key = f"{self.loop['repo']}#{number}"
        prior = data.get(key) or {}
        data[key] = entry
        self._save(self.breach, data)
        return prior

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
