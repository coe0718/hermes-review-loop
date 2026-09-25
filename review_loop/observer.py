"""The observer feed — short, read-only notices about the loop's transitions.

An observer is not a third seat and not a relay. The fixer and the reviewer keep driving each
other exactly as before; this is a second, one-way audience (a person's phone, a profile's
channel) that is told what the loop just decided. Nothing here can start, stop, delay or fail a
run.

**It rides the mechanism the loop already has.** The loop reaches a human the same way it
reaches an agent: it POSTs a signed payload at a webhook route and the gateway delivers it (see
:mod:`review_loop.routes`). An observer destination *is* such a route — configured separately
from the seats' routes, registered with ``deliver_only`` so the gateway renders the notice and
sends it without waking a model. No second HTTP client, no token in the loop, no chat SDK.

**A notice is emitted from the transition, never from a narrative.** Every call site is the same
deterministic code that just changed the loop's durable state — the moment a seat is claimed,
the moment a breach marker is written, the moment the watchdog read a stall out of GitHub — so a
notice can never describe something the loop did not do. Agent text is never an input here.

**One transition, one notice.** Every ledger entry is keyed by loop + PR + head + event and an
identity for the fact itself (a review id, an action, a stall kind), so a redelivered webhook, a
re-armed hook, or a sweep that runs twice cannot produce a second ping for the same fact.

The ledger (``observations.json`` in the loop's state dir) doubles as the outbox: definite
pre-POST failures can be retried, but an ambiguous POST result is quarantined for manual
reconciliation, not replayed. The claim is written *before* the POST so concurrent gates
cannot send the same fact twice.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import re
import tempfile
import time
from urllib.parse import quote

from . import gh, prompts, routes
from .util import log, now_iso

# The transitions an observer may subscribe to. These names are the loop's vocabulary for what
# happened; the same strings are what `--observer-events` accepts and what the ledger stores.
EVENTS = ("opened", "handoff", "verdict", "approved", "escalation", "stall", "closed")

# The batched form: one message for many transitions (see ``observer.digest_min``).
DIGEST_EVENT = "digest"

# How a transition reads in a chat line. Derived from the event, never from the payload: an
# observer that phrased things differently per call site would be a second, wrong source of truth.
EMOJI = {"opened": "📬", "handoff": "🔧", "verdict": "🔍", "approved": "✅",
         "escalation": "⚠️", "stall": "⏳", "closed": "🧹", "digest": "🗂"}
LABEL = {"opened": "opened — first look", "handoff": "fix pushed · review requested",
         "verdict": "review posted", "approved": "approved",
         "escalation": "loop stopped — cap spent", "stall": "stalled",
         "closed": "PR closed"}

# A stale claim may have reached the gateway before its sender died. Never replay it:
# without a receiver-side idempotency guarantee, a replay can ping twice.
STALE_CLAIM_S = 15 * 60
MAX_ATTEMPTS = 3

# How long a delivery receipt is kept. Long enough that "was Tuck told?" is answerable for a
# month, short enough that the ledger stays a few kilobytes.
RETENTION_S = 30 * 86400

DIGEST_LIMIT = 25


# -- configuration -------------------------------------------------------------


def configured(loop: dict) -> str:
    """Why there is nothing to deliver to at all, or ``""`` when a feed is switched on.

    Config and mute only — deliberately *not* whether the route resolves, because a route that
    cannot be reached is a delivery failure the operator should see recorded and retried (see
    :func:`notify`), not a reason to pretend the feed does not exist.
    """
    observer = loop.get("observer") or {}
    if not observer:
        return "no observer configured"
    if observer.get("misconfigured"):
        return str(observer["misconfigured"])
    if observer.get("mute"):
        return "the observer feed is muted"
    return ""


def unusable(loop: dict) -> str:
    """Why this loop's feed cannot deliver, or ``""`` when it can.

    Everything :func:`configured` knows, plus the registry: whether the route the gateway needs
    is actually there and carries a secret and a URL. Local reads only (no network, no lock), so
    ``status`` and every call site can afford to ask.
    """
    reason = configured(loop)
    if reason:
        return reason
    observer = loop.get("observer") or {}
    if _target(loop) is None:
        return (f"route {observer.get('route')!r} is missing from the gateway's subscriptions, "
                f"has no secret/url, or does not match the observer delivery-only contract")
    return ""


def subscribes(loop: dict, event: str) -> bool:
    """Is this event in the loop's feed? No ``events`` key means every event."""
    wanted = (loop.get("observer") or {}).get("events")
    return not wanted or event in set(wanted)


def digest_wait(loop: dict) -> int:
    """Seconds a digest waits before the sweep flushes it (0 = one notice per transition)."""
    return int((loop.get("observer") or {}).get("digest_min") or 0) * 60


def describe(observer: dict) -> str:
    """One line about a destination, for ``status`` and for ``set``'s diff."""
    if not observer:
        return "not configured"
    if observer.get("misconfigured"):
        return f"misconfigured — {observer['misconfigured']}"
    line = f"{observer.get('route')} → {observer.get('deliver')} (profile {observer.get('profile')})"
    if observer.get("mute"):
        line += " [muted]"
    if observer.get("events"):
        line += " · " + ",".join(observer["events"])
    if observer.get("digest_min"):
        line += f" · digest every {observer['digest_min']}m"
    return line

def route_contract(loop: dict) -> dict:
    """Exact destination and no-model adapter the observer has authorized."""
    cfg = loop.get("observer") or {}
    return {"profile": cfg.get("profile", "default"), "deliver": cfg.get("deliver", "telegram"),
            "deliver_only": True, "prompt": prompts.OBSERVER, "script": "observe.py",
            "events": ["pull_request"], "deliver_extra": cfg.get("deliver_extra") or {}}

def _target(loop: dict):
    """Refuse gateway-side destination overrides not authorized by this loop."""
    cfg = loop.get("observer") or {}
    name = cfg.get("route") or ""
    # The registry is mutable gateway state, not authority for where a private PR
    # link may go. A hostless loop must not inherit its host from that registry.
    if not loop.get("host"):
        log(f"observer route {name!r} has no loop-authorized webhook host")
        return None
    entry = routes.route(name)
    if not isinstance(entry, dict) or (entry.get("deliver_extra") or {}) != (cfg.get("deliver_extra") or {}):
        log(f"observer route {name!r} has an unauthorized deliver_extra destination")
        return None
    return routes.target(name, loop.get("host"), expected=route_contract(loop))


# -- the ledger ----------------------------------------------------------------


@contextlib.contextmanager
def _lock(path: pathlib.Path):
    """Serialize ledger read-modify-write against a sibling lock inode.

    The same shape the route registry uses, and held only across a read and a write — never
    across the POST. A delivery that hangs must not hold a lock that the next transition needs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: pathlib.Path) -> dict:
    """An unreadable existing ledger must never erase claims and allow duplicates."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise ValueError("observer ledger has invalid entries")
    return data

def unsettled(st) -> int:
    """Count claims that could still send to an old destination."""
    with _lock(st.observations):
        return sum(isinstance(entry, dict) and entry.get("status") in
                   {"queued", "digesting", "pending", "failed", "uncertain"}
                   for entry in _read(st.observations)["entries"].values())


def _save(path: pathlib.Path, data: dict) -> None:
    """Durably publish a claim; callers catch failures without blocking their seat."""
    temp = None
    try:
        body = json.dumps(data, indent=2).encode()
        fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write to the observer ledger")
                view = view[written:]
        finally:
            os.fsync(fd)
            os.close(fd)
        os.replace(temp, path)
        temp = None
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception as exc:
        log(f"observer ledger write failed ({path.name}): {exc}")
        raise
    finally:
        if temp and os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass


def _prune(data: dict, now: float) -> None:
    for key, entry in list(data["entries"].items()):
        if not isinstance(entry, dict):
            data["entries"].pop(key, None)
            continue
        if entry.get("status") in ("delivered", "failed") and now - float(entry.get("at") or 0) > RETENTION_S:
            data["entries"].pop(key, None)


def owed(st) -> dict:
    """What the feed still owes: ``{status: count}`` over the ledger, for ``status`` to print."""
    counts: dict = {}
    with _lock(st.observations):
        data = _read(st.observations)
        for entry in (data.get("entries") or {}).values():
            if isinstance(entry, dict):
                status = str(entry.get("status") or "?")
                counts[status] = counts.get(status, 0) + 1
    return counts


# -- the notice ----------------------------------------------------------------


def key_for(loop: dict, number, head: str, event: str, identity: object = "") -> str:
    """The idempotency key: what fact is this?

    Loop + PR + head + event + an identity for the fact itself. The identity is what makes the
    key honest rather than merely unique: a review id for a verdict, the action for a handoff,
    the stall kind for a stall — so the *same* fact redelivered is one entry, and a genuinely new
    fact at the same head is a new one.
    """
    return ":".join([str(loop.get("id") or loop.get("repo") or "loop"), str(number),
                     head or "nohead", event, str(identity or "-")])


def summarize(loop: dict, event: str, number, head: str, *, outcome: str = "",
              next_turn: str = "", round_no=None, actor: str = "") -> str:
    """The one-line summary of a transition: seat/event, head, outcome, next turn.

    No review body, no diff, no token, no secret — a ping carries the facts a person needs to
    decide whether to look, and the link to look at. Anything longer belongs on the PR.
    """
    emoji = EMOJI.get(event, "•")
    parts = [f"{emoji} [{loop.get('id')}] #{number} `{(head or '')[:7]}` {LABEL.get(event, event)}"]
    if outcome:
        parts.append(f" — {outcome}")
    if actor:
        parts.append(f" ({actor})")
    if round_no:
        parts.append(f" · round {round_no}/{loop.get('cap')}")
    if next_turn:
        parts.append(f" · next: {next_turn}")
    return "".join(parts)


def render(loop: dict, event: str, number, head: str, **fields) -> str:
    """The full notice: the summary, then the direct PR link on its own line."""
    return f"{summarize(loop, event, number, head, **fields)}\n{gh.pr_url(loop, number)}"


def render_digest(loop: dict, entries: list) -> str:
    """One compact message for historical transitions, without stale next-turn advice."""
    lines = [f"🗂 [{loop.get('id')}] review-loop digest — {len(entries)} transition(s)"]
    for entry in entries[:DIGEST_LIMIT]:
        lines.append(f"• #{entry.get('number')} `{(entry.get('head') or '')[:7]}` "
                     f"{_without_next_turn(entry.get('summary') or entry.get('event'))} · {entry.get('url')}")
    if len(entries) > DIGEST_LIMIT:
        lines.append(f"…and {len(entries) - DIGEST_LIMIT} more in this window")
    return "\n".join(lines)


def _without_next_turn(text: str) -> str:
    """Queued/retried facts remain historical; a cached instruction is not live state."""
    # A legacy cached digest has its URL later on the same line. Keep that link.
    return re.sub(r" · next: [^\n]*?(?= · https://|\n|$)", "", text)

def base_identity(loop: dict, pr: object) -> str:
    """The configured, same-repo base in PR metadata, or unknown."""
    base = pr.get("base") if isinstance(pr, dict) else None
    if not isinstance(base, dict) or base.get("ref") != loop.get("base"):
        return ""
    sha = base.get("sha")
    repo = base.get("repo")
    if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not isinstance(repo, dict) or repo.get("full_name") != loop.get("repo")):
        return ""
    return sha

def verified_base_sha(loop: dict, pr: object) -> str:
    """Read the exact configured base ref; PR metadata alone is not ref authority."""
    sha = base_identity(loop, pr)
    if not sha:
        return ""
    branch = loop["base"]
    path = f"/repos/{loop['repo']}/git/ref/heads/{quote(branch, safe='/')}"
    try:
        ref = gh.api(loop, path)
        obj = ref.get("object") if isinstance(ref, dict) else None
        if (ref.get("ref") == f"refs/heads/{branch}" and isinstance(obj, dict)
                and obj.get("type") == "commit" and obj.get("sha") == sha):
            return sha
    except Exception:
        pass
    return ""


def block_for(loop: dict, event: str, number, head: str, text: str) -> dict:
    """The ``_observer`` block the route prompt renders (``{_observer.message}``).

    Shaped like the ``_loop`` block the seats get, and for the same reason: the gateway renders
    a template against this payload, so everything the message needs must be in here and nothing
    the message must not carry (credentials, diffs) may be.
    """
    return {"event": event, "loop": loop.get("id"), "pr": number, "head": head,
            "url": gh.pr_url(loop, number), "at": now_iso(), "message": text}


# -- delivery ------------------------------------------------------------------


def _post(loop: dict, block: dict, tag: str) -> tuple:
    """Return (delivered, error, uncertain); a POST without a 2xx may have landed."""
    route = (loop.get("observer") or {}).get("route") or ""
    contract = route_contract(loop)
    if _target(loop) is None:
        return False, (f"route {route!r} is missing from the gateway's subscriptions, or has no "
                       f"secret/url, or violates the observer delivery-only contract"), False
    payload = {"repository": {"full_name": loop["repo"]}, "_observer": block}
    if routes.fire(route, "pull_request", payload, tag, loop.get("host"), expected=contract):
        return True, "", False
    return False, "POST result unverified; manual reconciliation required (not automatically retried)", True


def _receipt(st, key: str, delivered: bool, error: str = "", uncertain: bool = False) -> None:
    """Record how a delivery ended, and free the members of a digest batch when it lands."""
    with _lock(st.observations):
        data = _read(st.observations)
        entry = data["entries"].get(key)
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            return
        entry["status"] = "delivered" if delivered else "uncertain" if uncertain else "failed"
        # Only a known pre-POST failure is safe to replay. Old writer versions
        # recorded ambiguous POST failures as `failed` without this evidence.
        entry["retryable"] = not delivered and not uncertain
        entry["at"] = time.time()
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["error"] = "" if delivered else (error or "delivery failed")
        if delivered:
            entry["delivered_at"] = entry["at"]
            for member in entry.get("batch") or []:
                record = data["entries"].get(member)
                if isinstance(record, dict):
                    record["status"] = "delivered"
                    record["at"] = entry["at"]
                    record["error"] = ""
        elif entry.get("batch"):
            # A possibly accepted batch must keep members attached, not queue duplicates.
            for member in entry.get("batch") or []:
                record = data["entries"].get(member)
                if isinstance(record, dict) and record.get("status") == "digesting":
                    record["status"] = "uncertain" if uncertain else "queued"
        _save(st.observations, data)


def notify(loop: dict, st, event: str, number, head: str = "", *, identity: object = "",
           outcome: str = "", next_turn: str = "", round_no=None, actor: str = "",
           base_sha: str = "") -> bool:
    """Send one notice about one transition. Best effort, never fatal, never a gate.

    Returns ``True`` only when *this* call got a receipt. Every other outcome — no destination,
    muted, filtered out, already recorded, batched for a digest, or a POST that failed — logs
    and returns ``False``, because a caller in the middle of a handoff must not have to care.
    """
    try:
        reason = configured(loop)
        if reason:
            log(f"observer: no notice for {event} #{number} — {reason}")
            return False
        if not subscribes(loop, event):
            log(f"observer: {event} is not in the feed for #{number} — not sent")
            return False

        summary = summarize(loop, event, number, head, outcome=outcome, next_turn=next_turn,
                            round_no=round_no, actor=actor)
        text = f"{summary}\n{gh.pr_url(loop, number)}"
        key = key_for(loop, number, head, event, identity)
        path = st.observations
        now = time.time()
        with _lock(path):
            data = _read(path)
            _prune(data, now)
            prior = data["entries"].get(key)
            if isinstance(prior, dict):
                log(f"observer: {event} #{number} at {(head or '')[:7]} is already recorded "
                    f"({prior.get('status')}) — not sending it twice")
                _save(path, data)
                return False
            queued = bool((loop.get("observer") or {}).get("digest_min"))
            data["entries"][key] = {"status": "queued" if queued else "pending",
                                    "event": event, "number": number, "head": head,
                                    "identity": str(identity or ""), "summary": summary,
                                    "base_sha": base_sha,
                                    "message": text, "url": gh.pr_url(loop, number),
                                    "attempts": 0, "error": "", "at": now}
            if queued:
                data["entries"][key]["queued_at"] = now
            _save(path, data)

        if queued:
            log(f"observer: queued {event} #{number} for the next digest")
            return False
        return _deliver(loop, st, key, text, f"{event}-{number}", event, number, head)
    except Exception as exc:
        log(f"observer: {event} notice for #{number} failed: {type(exc).__name__}: {exc}")
        return False


def _deliver(loop: dict, st, key: str, text: str, tag: str, event: str, number, head: str) -> bool:
    """POST one rendered notice and record the receipt. The claim already exists in the ledger."""
    if event == "approved" and " · next: you merge" in text:
        # An approval can be dismissed between the gate's check and this POST. A merge
        # instruction is justified only by the same review id at the live open head.
        with _lock(st.observations):
            entry = _read(st.observations)["entries"].get(key) or {}
        current = gh.pr(loop, number)
        reviews = gh.reviews(loop, number) if (isinstance(current, dict)
            and current.get("number") == number and current.get("state") == "open"
            and (current.get("head") or {}).get("sha") == head) else None
        # Re-evaluate the latest verdict at delivery time, not merely whether the original
        # approval is still present. A later rejection on this same head supersedes it.
        from . import gate
        latest = (gate.latest_effective_review_at_head(reviews, loop, head)
                  if isinstance(reviews, list) else None)
        # A fixer push may be quarantined after the gate checked the ledger,
        # including during these live reads. Check immediately before delivery;
        # on an unreadable ledger, omit the instruction rather than guessing.
        try:
            from . import config
            from .run_supervisor import Supervisor
            ledger = config.home() / "state" / "review-loop-runs.sqlite"
            held = ledger.exists() and Supervisor(ledger).post_write_hold(loop["repo"], number)
        except Exception:
            held = True
        if not (not held and entry.get("base_sha")
                and entry["base_sha"] == verified_base_sha(loop, current)
                and latest and gh.review_state(latest) == "APPROVED"
                and str(latest.get("id")) == entry.get("identity")):
            text = _without_next_turn(text)
    delivered, error, uncertain = _post(loop, block_for(loop, event, number, head, text), tag)
    _receipt(st, key, delivered, error, uncertain)
    if delivered:
        log(f"observer: notified {event} #{number} at {(head or '')[:7]}")
    else:
        log(f"observer: {event} #{number} not verified ({error})")
    return delivered


def retry(loop: dict, st) -> int:
    """Retry only definite pre-POST failures; quarantine stale claims as uncertain.

    Called by the watchdog sweep. Bounded by ``MAX_ATTEMPTS``; a destination that stays broken
    leaves its entries visible in ``status`` rather than retrying for ever, and nothing here
    touches a seat, a lock or the queue.
    """
    try:
        if configured(loop):
            return 0                     # muted or unconfigured: retrying would defeat the point
        now = time.time()
        with _lock(st.observations):
            data = _read(st.observations)
            for entry in data["entries"].values():
                if (isinstance(entry, dict) and entry.get("status") == "failed"
                        and entry.get("retryable") is not True):
                    entry["status"] = "uncertain"
                    entry["error"] = "legacy failure has unknown POST outcome; reconcile manually"
            _prune(data, now)
            pending = []
            for key, entry in data["entries"].items():
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if (status == "failed" and entry.get("retryable") is True
                        and int(entry.get("attempts") or 0) < MAX_ATTEMPTS):
                    pending.append((key, dict(entry)))
                elif status == "pending" and now - float(entry.get("at") or 0) > STALE_CLAIM_S:
                    entry["status"] = "uncertain"
                    entry["error"] = "sender died before receipt; POST outcome unknown; reconcile manually"
                    for member in entry.get("batch") or []:
                        record = data["entries"].get(member)
                        if isinstance(record, dict) and record.get("status") == "digesting":
                            record["status"] = "uncertain"
            for key, entry in pending:
                data["entries"][key] = {**entry, "status": "pending", "at": now}
            _save(st.observations, data)
    except Exception as exc:
        log(f"observer retry scan failed: {type(exc).__name__}: {exc}")
        return 0

    sent = 0
    for key, entry in pending:
        text = _without_next_turn(str(entry.get("message") or ""))
        if not text:
            continue
        tag = f"retry-{entry.get('event')}-{entry.get('number')}"
        # Built by hand rather than through block_for: a digest batch has no single PR, and a
        # synthesized link to "pull/" would be worse than no link at all.
        block = {"event": entry.get("event") or "notice", "loop": loop.get("id"),
                 "pr": entry.get("number") or "", "head": entry.get("head") or "",
                 "url": entry.get("url") or "", "at": now_iso(), "message": text}
        if entry.get("batch"):
            block["count"] = len(entry["batch"])
        delivered, error, uncertain = _post(loop, block, tag)
        _receipt(st, key, delivered, error, uncertain)
        if delivered:
            sent += 1
            log(f"observer: retried {entry.get('event')} #{entry.get('number')} — delivered")
        else:
            log(f"observer: retry failed for {entry.get('event')} #{entry.get('number')}: {error}")
    return sent


def flush(loop: dict, st, wait_s: int = 0) -> bool:
    """Send one compact digest for the transitions that are queued for a batch.

    ``wait_s`` is the batching window (``digest_min``), passed by the caller so the sweep owns
    the cadence: with no scheduled watchdog there is no flush, which is why the digest is
    documented as "batched until the next sweep" rather than "batched for N minutes".
    """
    try:
        reason = configured(loop)
        if reason:
            return False
        path = st.observations
        now = time.time()
        members: list = []
        text = ""
        key = ""
        with _lock(path):
            data = _read(path)
            _prune(data, now)
            queued = sorted((k for k, e in data["entries"].items()
                             if isinstance(e, dict) and e.get("status") == "queued"),
                            key=lambda k: data["entries"][k].get("queued_at") or 0)
            if not queued:
                return False
            oldest = float(data["entries"][queued[0]].get("queued_at") or now)
            if now - oldest < wait_s:
                return False
            key = key_for(loop, "-", "", DIGEST_EVENT, queued[0])
            prior = data["entries"].get(key)
            if isinstance(prior, dict) and prior.get("status") in {"pending", "uncertain", "delivered", "failed"}:
                return False
            members = [data["entries"][k] for k in queued]
            text = render_digest(loop, members)
            data["entries"][key] = {"status": "pending", "event": DIGEST_EVENT, "number": "",
                                    "head": "", "identity": queued[0], "summary": "",
                                    "message": text, "url": "", "batch": queued,
                                    "attempts": int(prior.get("attempts") or 0) if isinstance(prior, dict) else 0,
                                    "error": "", "at": now, "queued_at": oldest}
            for member in queued:
                data["entries"][member]["status"] = "digesting"
            _save(path, data)

        delivered, error, uncertain = _post(loop, {"event": DIGEST_EVENT, "loop": loop.get("id"),
                                        "count": len(members), "at": now_iso(), "message": text},
                                 f"digest-{len(members)}")
        _receipt(st, key, delivered, error, uncertain)
        if delivered:
            log(f"observer: digest of {len(members)} transition(s) delivered")
        else:
            log(f"observer: digest could not be delivered ({error}) — members left queued")
        return delivered
    except Exception as exc:
        log(f"observer digest failed: {type(exc).__name__}: {exc}")
        return False
