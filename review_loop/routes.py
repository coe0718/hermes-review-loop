"""Webhook routes: read the gateway's subscription file, and fire a signed POST at it.

The loop does not own the gateway, so it does not invent a second way to reach agents. It
writes routes through the same config file the gateway already reads (``new_route``) and
wakes a seat by POSTing a GitHub-shaped payload with a valid signature (``fire``).

A route's URL is derived from its ``profile``: the gateway serves the launch profile at
``/webhooks/<name>`` and every other profile at ``/p/<profile>/webhooks/<name>``.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import tempfile
import time
import urllib.request
from contextlib import contextmanager

from . import config
from .util import log


def subs_path() -> pathlib.Path:
    override = os.environ.get("REVIEW_LOOP_SUBS")
    return pathlib.Path(override).expanduser() if override else config.home() / "webhook_subscriptions.json"


def all_routes() -> dict:
    try:
        return json.loads(subs_path().read_text())
    except Exception:
        return {}


def _parse_for_write(path: pathlib.Path, raw: bytes | None) -> dict:
    """Unlike best-effort reads, writes must not replace an unreadable registry."""
    if raw is None:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"route registry {path} must be a JSON object")
    return data


# -- optimistic concurrency against writers that do not take our lock -----------------------
#
# Hermes's own CLI/dashboard subscription writers rewrite this file without the plugin's
# ``flock``. So every plugin read-modify-write records the file's identity when it reads, and
# re-checks it immediately before ``os.replace``: if a native writer published in between, the
# plugin re-reads and re-applies its edit instead of publishing a registry built from stale
# bytes. What remains is the gap between that last check and the ``rename`` itself (a few
# syscalls), plus a native writer that read *before* our publish and writes *after* it — that
# one overwrites us, and only the intent record + self-heal (``route_intent``) repairs it.

CONFLICT_RETRIES = 5


class RegistryConflictError(OSError):
    """A non-cooperating writer kept changing the registry; nothing was published."""

    published = False


def _identity_of(st: os.stat_result | None, raw: bytes | None):
    if st is None:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size,
            hashlib.sha256(raw or b"").hexdigest())


def _snapshot(path: pathlib.Path):
    """(identity, bytes) read from one open file; (None, None) when the file does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, None
    try:
        st = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    return _identity_of(st, raw), raw


def _identity(path: pathlib.Path):
    """The registry's identity right now: inode, mtime, size and a content hash."""
    return _snapshot(path)[0]


class _RegistryChanged(Exception):
    pass


def _transact(path: pathlib.Path, edit):
    """Locked, optimistic read-modify-write of the registry.

    ``edit(data)`` mutates the parsed registry in place and returns ``(write, result)``. It must
    be a pure function of ``data``: on a detected concurrent write it is re-run against the
    fresh bytes, so a native writer's edit survives alongside ours.
    """
    with _registry_lock(path):
        for _ in range(CONFLICT_RETRIES):
            identity, raw = _snapshot(path)
            data = _parse_for_write(path, raw)
            write, result = edit(data)
            if not write:
                return result
            try:
                _write_registry(path, data, expected=identity)
            except _RegistryChanged:
                log(f"route registry {path.name} changed under a plugin edit; re-applying")
                continue
            return result
    raise RegistryConflictError(
        f"route registry {path} kept changing under the plugin's edit "
        f"({CONFLICT_RETRIES} attempts); nothing was published")


@contextmanager
def _registry_lock(path: pathlib.Path):
    """Serialize cooperating plugin writers on a persistent sibling inode.

    Hermes CLI/dashboard subscription writers do not take this lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class RegistryDurabilityError(OSError):
    """Replacement is visible, but directory sync failed; durability is unconfirmed."""

    published = True


_UNCHECKED = object()


def _write_registry(path: pathlib.Path, data: dict, expected=_UNCHECKED) -> None:
    """Publish owner-only bytes atomically, then sync the containing directory.

    With ``expected`` (an identity from ``_snapshot``), the live file is re-checked immediately
    before ``os.replace``; a mismatch raises ``_RegistryChanged`` and publishes nothing.

    Before replacement, failures leave the old inode intact. After replacement,
    a directory sync failure raises RegistryDurabilityError: the new bytes are
    visible but their survival across a crash has not been confirmed.
    """
    body = json.dumps(data, indent=2).encode()
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write to route registry")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if expected is not _UNCHECKED and _identity(path) != expected:
            raise _RegistryChanged()
        os.replace(temp, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise RegistryDurabilityError(
                f"route registry {path} was published but directory sync failed; durability unconfirmed"
            ) from exc
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def route(name: str) -> dict | None:
    entry = all_routes().get(name)
    return entry if isinstance(entry, dict) else None


def url_for_profile(name: str, profile: str | None, host: str | None = None) -> str | None:
    """The URL a route *has* under a profile — the same shape the gateway serves.

    Split out from ``url_for`` so a preview can show the URL a route is about to get before the
    registry holds it: the profile is part of the URL, which is exactly why changing a seat's
    profile is a route change and not only a config edit.
    """
    base = config.webhook_host(host)
    if not base:
        return None
    if not profile or profile == "default":
        return f"{base}/webhooks/{name}"
    return f"{base}/p/{profile}/webhooks/{name}"


def url_for(name: str, host: str | None = None) -> str | None:
    entry = route(name)
    if not entry:
        return None
    # Never invent a relative webhook URL when neither the caller nor the route
    # names an operator-owned gateway. Reject malformed origins at this boundary.
    # A malformed *stored* origin raises here, which is the caller's to refuse loudly.
    base = config.webhook_host(host or entry.get("host")) or ""
    if not base:
        return None
    return url_for_profile(name, entry.get("profile", "default"), base)


def target(name: str, host: str | None = None, *, expected: dict | None = None):
    """(url, secret_bytes) for a route, or None when it is missing or has no secret."""
    entry = route(name)
    if not entry:
        log(f"route {name!r} not found in {subs_path().name}")
        return None
    if expected is not None and any((entry.get(k) or {}) != value if k == "deliver_extra"
                                    else entry.get(k) != value for k, value in expected.items()):
        log(f"route {name!r} no longer matches its delivery contract")
        return None
    secret = entry.get("secret") or ""
    try:
        base = config.webhook_host(host or entry.get("host"))
        profile = entry.get("profile", "default")
        url = (f"{base}/webhooks/{name}" if profile == "default"
               else f"{base}/p/{profile}/webhooks/{name}") if base else None
    except config.ConfigError as exc:
        log(f"route {name!r} has invalid webhook host: {exc}")
        return None
    if not secret or not url:
        log(f"route {name!r} has no secret/url")
        return None
    return url, secret.encode()


def fire(name: str, event: str, payload: dict, tag: str, host: str | None = None,
         *, expected: dict | None = None, on_attempt=None) -> bool:
    """POST a signed payload; on_attempt marks the boundary before transport I/O.

    A false result before that callback is known not delivered; a false result
    after it may have reached the gateway and must not be blindly replayed.
    """
    target_ = target(name, host, expected=expected)
    if not target_:
        return False
    url, secret = target_
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
        "X-GitHub-Delivery": f"{tag}-{int(time.time())}",
        "User-Agent": "hermes-review-loop",
    })
    try:
        if on_attempt is not None:
            on_attempt()
        with urllib.request.urlopen(req, timeout=20) as resp:
            log(f"fired {name} for {tag} (HTTP {resp.status})")
            return 200 <= resp.status < 300
    except Exception as exc:
        log(f"could not fire {name} for {tag}: {exc}")
        return False


def new_route(name: str, *, profile: str, prompt: str, events: list[str], script: str,
               deliver: str, description: str = "", skills: list[str] | None = None,
               host: str | None = None, deliver_only: bool = False,
               deliver_extra: dict | None = None) -> dict:
    """Create (or update) a route entry and write it back to the gateway's file.

    The secret is generated here, not asked for. 0600, same file the gateway reads.

    ``deliver_only`` is the gateway's own "no agent here" mode: the rendered prompt *is* the
    message that reaches ``deliver``, with no model run and nothing to review afterwards. That
    is what makes an observer route a feed rather than a third seat.
    """
    import secrets as _secrets

    path = subs_path()

    def edit(data: dict):
        prior = data.get(name) or {}
        if not isinstance(prior, dict):
            raise ValueError(f"route {name!r} must be a JSON object")
        entry = {
            "description": description or prior.get("description", ""),
            "events": list(events),
            "secret": prior.get("secret") or secret,
            "prompt": prompt,
            "skills": list(skills or prior.get("skills") or []),
            "deliver": deliver,
            "profile": profile,
            "created_at": prior.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "script": script,
        }
        # Written only when asked for, so the seats' routes keep exactly the shape they had:
        # an entry that gains a key the gateway has not seen yet is a change nobody reviewed.
        if deliver_only:
            entry["deliver_only"] = True
        if deliver_extra:
            entry["deliver_extra"] = dict(deliver_extra)
        if host:
            entry["host"] = host
        data[name] = entry
        return True, entry

    # Generated once, outside the retry loop: a re-applied edit must not mint a second secret.
    secret = _secrets.token_hex(32)
    return _transact(path, edit)


def remove_route(name: str) -> bool:
    def edit(data: dict):
        if name not in data:
            return False, False
        data.pop(name)
        return True, True

    return _transact(subs_path(), edit)


def restore_entries(entries: dict[str, dict | None]) -> None:
    """Restore only owned route entries after an incomplete multi-route update."""
    def edit(data: dict):
        for name, entry in entries.items():
            if entry is None:
                data.pop(name, None)
            else:
                data[name] = entry
        return True, None

    _transact(subs_path(), edit)


def heal_entries(expected: dict[str, dict], fields: tuple[str, ...], owned) -> tuple[dict, dict]:
    """Put back the plugin's own routes a non-cooperating writer erased or rewrote.

    ``expected`` is the plugin's intent record (name → full entry). Under the lock, against the
    live bytes, each name is: left alone when every watched ``field`` already matches; restored
    when missing, or present and still ``owned(entry)`` (one of this plugin's gate scripts);
    reported as a conflict — never overwritten — when something else now holds the name.
    Other names in the registry are not read for anything but preservation. A malformed
    registry raises (fail closed) exactly like every other plugin write.

    Returns ``(restored, conflicts)``: name → list of fields restored ("missing" for an erased
    route), and name → reason.
    """
    def edit(data: dict):
        restored: dict = {}
        conflicts: dict = {}
        for name, want in expected.items():
            live = data.get(name)
            if live is None:
                data[name] = dict(want)
                restored[name] = ["missing"]
                continue
            if not isinstance(live, dict):
                conflicts[name] = "registry entry is not a JSON object"
                continue
            diff = [key for key in fields if live.get(key) != want.get(key)]
            if not diff:
                continue
            if not owned(live):
                conflicts[name] = (f"now runs {live.get('script')!r}, not a review-loop gate — "
                                   "something else holds this name")
                continue
            merged = {**live, **want}
            for key in fields:          # a watched key the plugin never wrote is not kept either
                if key not in want:
                    merged.pop(key, None)
            data[name] = merged
            restored[name] = diff
        return bool(restored), (restored, conflicts)

    return _transact(subs_path(), edit)
