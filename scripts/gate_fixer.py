#!/usr/bin/env python3
"""Fixer gate — decides whether this event starts a fix run.

Route this at the fixer's profile, on the ``pull_request_review`` event. It wakes the fixer
when a verdict it must answer lands:

* ``submitted`` with state ``changes_requested``, authored by one of the loop's reviewers,
  **at the current head** (a verdict on an older commit is already superseded);
* ``commented`` and ``approved`` end here — approval is where the loop stops being useful;
* the verdict that reaches the cap is **not** a work order. Handing the fixer a fourth fix
  no reviewer will read is how a loop burns a night; the PR goes to adjudication instead.

stdin : a GitHub webhook payload
stdout: that payload with a ``_loop`` block, or ``[SILENT]``
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import gate, gh, observer  # noqa: E402
from review_loop.util import log, silence  # noqa: E402


def main() -> None:
    payload = json.load(sys.stdin)
    loop, st = gate.context(payload)
    pr = gate.pr_of(payload)
    review = payload.get("review")
    if not isinstance(review, dict):
        silence("no review in payload")

    if payload.get("action", "") != "submitted":
        silence(f"review action {payload.get('action')!r} is not a verdict landing")

    # GitHub's webhook payloads spell review states lowercase ("changes_requested"); the REST
    # API shouts them ("CHANGES_REQUESTED"). Compare case-insensitively on both sides of that
    # boundary — assuming one spelling is how the fixer leg of this loop died on arrival once.
    if gate.reviewer_login(review) not in set(loop["reviewers"]):
        silence(f"verdict author {gate.reviewer_login(review) or 'unknown'} is not a reviewer")

    seat = "fixer"
    number = gate.number_of(payload, pr)
    key = gate.seat_key(loop, number)

    # A webhook is a delivered snapshot, not permission to release a seat, announce
    # approval, escalate, or start a fixer after the PR was retargeted. Stacked reviews
    # have no trustworthy review-ID/base association in this direct-gh workflow yet.
    if (pr.get("base") or {}).get("ref") != loop["base"]:
        silence("stacked/retargeted verdict has no verified review situation")
    live = gh.pr(loop, number)
    if (not isinstance(live, dict) or live.get("number") != number
            or live.get("state") != "open" or live.get("draft")
            or (live.get("base") or {}).get("ref") != loop["base"]
            or (live.get("head") or {}).get("sha") != (pr.get("head") or {}).get("sha")):
        silence("verdict snapshot is stale or live PR unavailable")
    snapshot_base_sha = (pr.get("base") or {}).get("sha")
    live_base_sha = (live.get("base") or {}).get("sha")
    if snapshot_base_sha and live_base_sha != snapshot_base_sha:
        silence("verdict base generation changed")

    state = str(review.get("state", "")).upper()
    if state == "APPROVED":
        # An approval ends the reviewer's turn exactly as a rejection does, and nothing else would
        # free that slot before it expired. A slot that leaks for `ttl_min` is a queue that stops
        # moving — on a busy repo, that is the difference between ten review slots and nine.
        if st.release_if("reviewer", key):
            log(f"released reviewer seat for {key}")
        gate.drain_seat(loop, "reviewer")
        # The webhook PR is a snapshot: an approval can arrive after a push, close or failed
        # lookup. Only a matching, currently open live head authorizes a merge handoff.
        approved_head = review.get("commit_id") or ""
        current = gh.pr(loop, number)
        live_open = (isinstance(current, dict) and current.get("number") == number
                     and current.get("state") == "open")
        current_head = ""
        if live_open and isinstance(current, dict):
            current_head = ((current.get("head") or {}).get("sha") or "")
        snapshot_matches = approved_head == ((pr.get("head") or {}).get("sha") or "")
        # The webhook is not evidence of the review's *current* verdict: GitHub can dismiss
        # the same review id after submitting it. A failed/partial live read is not approval.
        reviews = gh.reviews(loop, number) if live_open and current_head == approved_head else None
        latest = (gate.latest_effective_review_at_head(reviews, loop, approved_head)
                  if isinstance(reviews, list) else None)
        live_approval = (latest is not None and latest.get("id") == review.get("id")
                         and gh.review_state(latest) == "APPROVED"
                         and gate.reviewer_login(latest) == gate.reviewer_login(review))
        if live_open and current_head and approved_head and approved_head != current_head:
            outcome, next_turn = "on an older head — the PR moved since", "the reviewer, on this head"
        elif live_open and current_head and approved_head == current_head and snapshot_matches and live_approval:
            outcome, next_turn = "", "you merge"
        else:
            outcome, next_turn = "current approval/head unverified — no merge handoff", "check current PR state"
        observer.notify(loop, st, "approved", number, approved_head,
                        identity=review.get("id"), actor=gate.reviewer_login(review),
                        outcome=outcome, next_turn=next_turn)
        silence(f"#{number} approved — reviewer's slot freed, nothing for the fixer to do")
    if state != "CHANGES_REQUESTED":
        silence(f"verdict state {review.get('state')!r} needs no fix")

    pr_head = gate.head_of(pr)
    if review.get("commit_id") != pr_head:
        silence("verdict is on an older head — superseded")

    reviews = gate.fetch_reviews(loop, number)
    latest = gate.latest_effective_review_at_head(reviews, loop, pr_head)
    if (latest is None or latest.get("id") != review.get("id")
            or gh.review_state(latest) != "CHANGES_REQUESTED"
            or gate.reviewer_login(latest) != gate.reviewer_login(review)):
        silence("changes-requested webhook is not the live latest effective verdict")
    prior = len(gate.verdicts(reviews, loop, exclude_id=review.get("id")))
    if st.inflight(f"fix:{number}:{pr_head}"):
        silence(f"a fix run for head {pr_head[:7]} is already out")

    # A verdict landed: the reviewer's turn is over, and anything queued behind it can start.
    if st.release_if("reviewer", key):
        log(f"released reviewer seat for {key}")
    gate.drain_seat(loop, "reviewer")

    if prior + 1 >= loop["cap"]:
        gate.breach(loop, st, number, pr_head, prior + 1,
                    f"verdict {prior + 1} returned changes requested — loop exhausted "
                    f"({loop['cap']} reviews / {loop['cap'] - 1} fixes)")
        silence(f"cap reached on #{number} — handed to adjudication instead of a fix")

    workspace = gate.take_seat(loop, st, seat, number, pr_head, f"fix #{number} @ {pr_head[:7]}",
                              login=(loop["seats"][seat].get("login") or ""))

    payload["_loop"] = gate.loop_block(loop, number, pr_head, workspace, seat=seat, round=prior + 1,
                                       role="fixer", verdict="changes_requested",
                                       reviewer=gate.reviewer_login(review))
    st.inflight(f"fix:{number}:{pr_head}", record=True)
    gate.ping_start(loop, seat, gate.start_text(
        loop, seat, number, pr_head, prior + 1,
        note=f"on a changes-requested verdict from {gate.reviewer_login(review)}"))
    # The feed is told last, after the seat is claimed and the run recorded: a destination that
    # hangs costs seconds at the end of this gate and never the fixer's slot or the queue behind
    # it. Keyed by the review's own id, so a redelivered `pull_request_review` — which happens,
    # and is why the in-flight marks exist — cannot ping twice for one verdict.
    observer.notify(loop, st, "verdict", number, pr_head, identity=review.get("id"),
                    outcome="changes requested", actor=gate.reviewer_login(review),
                    next_turn="fixer", round_no=prior + 1)
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
