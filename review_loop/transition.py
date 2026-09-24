"""Conservative boundary between a stacked child and a direct-trunk review.

GitHub review commit_id does not bind a base. A same-head retarget therefore
cannot inherit any old review or seat authorization. Observation is host-owned;
an edited webhook is only a hint to re-read the live PR.
"""
from __future__ import annotations

import time

from . import gh


def record(loop, st, number, head, base, *, watch=None):
    """Quarantine a previously observed stacked child; return its hold if any.

    Caller must have verified the live PR. Never create a transition from an
    untrusted webhook alone. A missing review-list baseline is a permanent
    same-head hold: ordering old reviews against future reviews is unknowable.
    Even a complete baseline does not authorize same-head unattended work.
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


def current_reviews(reviews, entry, loop):
    """Diagnostic candidates only, never proof of dispatch or base generation.

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