"""Conservative boundary between a stacked child and a direct-trunk review.

GitHub review commit_id does not bind a base. A same-head retarget therefore
cannot inherit any old review or seat authorization. Observation is host-owned;
an edited webhook is only a hint to re-read the live PR.

After the boundary the PR starts a *fresh* review situation (owner policy, #23):
exactly one isolated reviewer turn is enqueued for the transition, and only a
review the host itself attested -- an exact-ID receipt from an isolated reviewer
run pinned to this head on the root base -- can decide anything for the held
head. Old reviews, rounds and approvals never carry over, and a review GitHub
merely lists after the boundary (a human's, a manual one) stays diagnostic.
"""
from __future__ import annotations

import math
import time

from . import gh, review_receipt

# Why a hold without a baseline can never be released at its head. Shared by every
# surface that reports it, so the operator reads one reason, not four paraphrases.
MISSING_BASELINE = ("the pre-retarget review list was unreadable when the retarget was "
                    "recorded: old and new reviews cannot be separated, so no review at this "
                    "head can count — push a new head")
_UNREAD = object()


def record(loop, st, number, head, base, *, watch=None):
    """Quarantine a previously observed stacked child; return its hold if any.

    Caller must have verified the live PR. Never create a transition from an
    untrusted webhook alone. A missing review-list baseline is a permanent
    same-head hold: ordering old reviews against future reviews is unknowable.
    A complete baseline authorizes only the transition's one fresh isolated
    reviewer turn (``start_fresh_review``), never the old reviews themselves.
    """
    data = watch if watch is not None else st.watch()
    stacked = (data.get("stacked_wait") or {}).get(str(number))
    if not isinstance(stacked, dict):
        previous = (data.get("heads") or {}).get(str(number))
        if (isinstance(previous, dict)
                and previous.get("base") not in (None, loop["base"])):
            stacked = {"head": previous.get("sha"), "base": previous["base"]}
    prior = st.transition_get(number)
    if isinstance(prior, dict) and prior.get("head") == head:
        return prior
    if not isinstance(stacked, dict) or base != loop["base"]:
        return None
    reviews, error = gh.reviews_read(loop, number)
    # The entire old review set must be known before a future human review can
    # be distinguished. Never use a partial or failed list as an empty baseline.
    ids = ([r.get("id") for r in reviews if isinstance(r, dict)]
           if not error and isinstance(reviews, list) else None)
    valid = ids is not None and all(type(i) is int and i > 0 for i in ids)
    entry = {"head": head, "from_base": stacked.get("base"),
             "observed_stacked_head": stacked.get("head"),
             "at": time.time(), "old_review_ids": ids if valid else None}
    entry = st.transition_set(number, entry)
    for seat in ("reviewer", "fixer"):
        st.queue_pop_head(seat, f"{loop['repo']}#{number}", head)
    # Do not release a PR-keyed seat here: another generation could acquire it
    # concurrently. Its normal handoff or TTL will release it.
    # Old in-flight keys and breach markers must not be interpreted as new
    # generation work. The head-scoped marks are removed by the state helper.
    st.quarantine(number, head)
    return entry


def hold(st, number, head):
    entry = st.transition_get(number)
    return entry if isinstance(entry, dict) and entry.get("head") == head else None


def baseline_missing(entry) -> bool:
    return isinstance(entry, dict) and not isinstance(entry.get("old_review_ids"), list)


def turn_key(entry):
    """The one reviewer turn this transition may start, or None when it is unnamable.

    Derived only from facts frozen when the hold was recorded, so every sweep and every
    redelivered webhook names the same turn and dedups on the ledger's unique
    repo/PR/head/seat/turn index. A later transition at a new head gets a new key.
    """
    at = entry.get("at") if isinstance(entry, dict) else None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at <= 0:
        return None
    return f"retarget:{str(entry.get('from_base') or '?')[:64]}:{int(at * 1000)}"


def ledger_path():
    from . import config
    return config.home() / "state" / "review-loop-runs.sqlite"


def read_receipts(loop, number, head, *, ledger=None):
    """Host receipts for this head on the root base; raises when the ledger is unreadable."""
    return review_receipt.confirmed_receipts(str(ledger or ledger_path()), loop["repo"],
                                             number, head, loop["base"])


def effective_reviews(loop, st, number, head, reviews, *, receipts=_UNREAD, ledger=None):
    """The reviews that may decide anything for this PR head.

    * no hold at ``head``: ``reviews`` unchanged (an ordinary trunk PR);
    * a hold without a baseline: ``[]`` forever -- see ``MISSING_BASELINE``;
    * a hold with a baseline: only reviews whose ID is not in the baseline AND that carry a
      confirmed host receipt binding that exact ID, principal, verdict and head to an isolated
      reviewer run pinned to the root base.

    A non-list ``reviews`` (unknown) stays unknown, and so does an unreadable receipt ledger:
    ``None`` is never "no trusted review" nor "all reviews". ``receipts`` may be supplied by a
    caller that already read them (``explain`` reads nothing itself).
    """
    if not isinstance(reviews, list):
        return reviews
    entry = hold(st, number, head)
    if entry is None:
        return reviews
    old = entry.get("old_review_ids")
    if not isinstance(old, list):
        return []
    if receipts is _UNREAD:
        try:
            receipts = read_receipts(loop, number, head, ledger=ledger)
        except Exception:
            return None
    if not isinstance(receipts, dict):
        return None
    kept = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_id = review.get("id")
        receipt = receipts.get(review_id) if type(review_id) is int else None
        if review_id in old or not isinstance(receipt, dict):
            continue
        user = review.get("user")
        state = gh.review_state(review)
        # The receipt names what the host read back right after the POST. A later dismissal
        # is GitHub's own live state and must still be able to retract the verdict; any
        # other drift (different head, author or verdict) means this is not that review.
        if (review.get("commit_id") != head or not isinstance(user, dict)
                or type(user.get("id")) is not int or user["id"] != receipt.get("principal_id")
                or (state != "DISMISSED" and state != receipt.get("verdict"))):
            continue
        kept.append(review)
    return kept


def start_fresh_review(loop, st, number, *, live=None):
    """Enqueue the transition's one fresh isolated reviewer turn; ``(status, detail)``.

    ``status`` is ``enqueued`` (durably in the ledger, worker armed), ``retry`` (a read or
    the enqueue failed: the next sweep tries again, and the failure is kept on the hold for
    ``explain``), or ``skip`` (nothing to start: no hold at the live head, a missing
    baseline, or not an eligible trunk PR right now -- e.g. still a draft).

    Always re-reads the live PR unless the caller just did: a webhook snapshot or an old
    listing never names the head that gets reviewed.
    """
    from . import gate
    if live is None:
        live = gh.pr(loop, number)
    if not isinstance(live, dict) or live.get("number") != number or live.get("state") != "open":
        return "retry", "live PR unavailable"
    head = (live.get("head") or {}).get("sha")
    entry = hold(st, number, head) if isinstance(head, str) and head else None
    if entry is None:
        return "skip", "no same-head retarget hold at the live head"
    if baseline_missing(entry):
        return "skip", MISSING_BASELINE
    author = ((live.get("user") or {}).get("login") or "").lower()
    if (live.get("draft") is not False or (live.get("base") or {}).get("ref") != loop["base"]
            or author not in set(loop["fixers"])):
        return "skip", "not an eligible trunk PR right now (draft, base or author)"
    key = turn_key(entry)
    if key is None:
        return "skip", "transition time unreadable — cannot name its one fresh turn"
    fresh = entry.get("fresh_review")
    if isinstance(fresh, dict) and fresh.get("turn_key") == key and fresh.get("state") == "enqueued":
        return "enqueued", key
    try:
        gate.enqueue_isolated(loop, "reviewer", number, head, turn_key=key)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        st.transition_update(number, head, {"fresh_review": {
            "turn_key": key, "state": "retry", "error": error, "at": time.time()}})
        return "retry", error
    st.transition_update(number, head, {"fresh_review": {
        "turn_key": key, "state": "enqueued", "at": time.time()}})
    return "enqueued", key


def current_reviews(reviews, entry, loop):
    """Diagnostic candidates only, never proof of dispatch or base generation.

    Authorization after a retarget comes from ``effective_reviews`` (host receipts) alone.

    GitHub's User type excludes service accounts marked Bot but cannot attest
    that a named User is human, nor which diff the review actually examined.
    Do not use this result as a seat, fixer, or merge authorization.
    """
    if entry is None:
        return reviews
    old = entry.get("old_review_ids")
    if not isinstance(old, list):
        return []
    return [r for r in reviews if isinstance(r, dict)
            and type(r.get("id")) is int and r["id"] not in old
            and isinstance(r.get("submitted_at"), str)
            and _after(r["submitted_at"], entry.get("at"))
            and (r.get("user") or {}).get("type") == "User"
            and ((r.get("user") or {}).get("login") or "").lower()
            != loop["reviewer_seat"].lower()]


def _after(timestamp, boundary):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(
            timezone.utc).timestamp() > boundary
    except (ValueError, TypeError, OverflowError):
        return False