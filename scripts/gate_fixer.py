#!/usr/bin/env python3
"""Fixer gate — decides whether this event starts a fix run.

Route this at the fixer's profile, on the ``pull_request_review`` event. It wakes the fixer
when a verdict it must answer lands:

* ``submitted`` with state ``changes_requested``, authored by one of the loop's reviewers,
  **at the current head** (a verdict on an older commit is already superseded) — current as
  GitHub says *now*, with this review still the latest effective verdict there;
* ``commented`` and ``approved`` end here — approval is where the loop stops being useful;
* the verdict that reaches the cap is **not** a work order. Handing the fixer a fourth fix
  no reviewer will read is how a loop burns a night; the PR goes to adjudication instead.

stdin : a GitHub webhook payload
stdout: ``[SILENT]`` (eligible runs queue; whole-agent isolation not available)
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import gate, gh, observer, transition  # noqa: E402
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

    user = pr.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    author = author.lower() if isinstance(author, str) else ""
    if author not in set(loop["fixers"]):
        silence(f"PR author {author or 'unknown'} is not an authorized fixer")

    seat = "fixer"
    number = gate.number_of(payload, pr)
    key = gate.seat_key(loop, number)

    # A ref may have advanced while PR metadata became unverifiable. Its
    # supervisor hold is authoritative even if a later webhook says approved:
    # neither a merge handoff nor another fixer turn may bypass inspection.
    from review_loop import config
    from review_loop.run_supervisor import Supervisor
    ledger = config.home() / 'state' / 'review-loop-runs.sqlite'
    if ledger.exists() and Supervisor(ledger).post_write_hold(loop['repo'], number):
        silence('post-write push quarantined — operator inspection required; no merge handoff')

    # An approval ends the turn that was claimed for the approved head. Free that claim before
    # the checks below: they gate the merge handoff and fixer work, and a failed read or a
    # retarget must not keep the reviewer's seat until its TTL. Only that head's claim ends,
    # so a late webhook cannot end a newer run on the same PR.
    approved_early = False
    if (str(review.get("state", "")).upper() == "APPROVED" and review.get("commit_id")
            and st.release_if("reviewer", key, review["commit_id"])):
        approved_early = True
        log(f"released reviewer seat for {key}")
        gate.drain_seat(loop, "reviewer")

    state = str(review.get("state", "")).upper()
    # An approval reaches its own verification below, which rechecks the live head, base and
    # retarget hold before any merge cue and otherwise reports it unverified. A rejection is a
    # work order, so it must pass these checks before anything else happens.
    if state != "APPROVED":
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
        # Trunk moving on does not supersede a verdict on a direct-trunk PR; a stacked base's
        # generation does. The merge handoff still rechecks the base below.
        if (snapshot_base_sha and (pr.get("base") or {}).get("ref") != loop["base"]
                and live_base_sha != snapshot_base_sha):
            silence("verdict base generation changed")
        boundary = transition.record(loop, st, number, (live.get("head") or {}).get("sha"), loop["base"])
        if boundary:
            # Review commit_id binds only the head. Even a post-boundary REST review
            # cannot prove which base it examined or which request dispatched it.
            # In particular a review on another head must never produce a merge cue.
            silence("base retarget: no generation-bound review receipt; no automated handoff")

    if state == "APPROVED":
        # An approval ends the reviewer's turn exactly as a rejection does, and nothing else would
        # free that slot before it expired. A slot that leaks for `ttl_min` is a queue that stops
        # moving — on a busy repo, that is the difference between ten review slots and nine.
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
        # The review commit pins only the child. A retarget or base advance can
        # change the reviewed diff without changing that child SHA.
        base_sha = observer.verified_base_sha(loop, current) if live_open else ""
        base_matches = bool(base_sha and observer.base_identity(loop, pr) == base_sha)
        # The webhook is not evidence of the review's *current* verdict: GitHub can dismiss
        # the same review id after submitting it. A failed/partial live read is not approval.
        reviews = gh.reviews(loop, number) if live_open and current_head == approved_head else None
        latest = (gate.latest_effective_review_at_head(reviews, loop, approved_head)
                  if isinstance(reviews, list) else None)
        live_approval = (latest is not None and latest.get("id") == review.get("id")
                         and gh.review_state(latest) == "APPROVED"
                         and gate.reviewer_login(latest) == gate.reviewer_login(review))
        # A review's commit_id binds the head only. The live PR must still describe the
        # same direct-trunk generation as the webhook snapshot; a same-head base retarget or
        # advancement between reads makes the approval an unknown-diff verdict.
        first_base = pr.get("base") or {}
        final_base = (current.get("base") or {}) if isinstance(current, dict) and live_open else {}
        same_base = (final_base.get("ref") == loop["base"]
                     and first_base.get("ref") == final_base.get("ref")
                     and first_base.get("sha") == final_base.get("sha")
                     and not transition.hold(st, number, current_head))
        if live_open and current_head and approved_head and approved_head != current_head:
            outcome, next_turn = "on an older head — the PR moved since", "the reviewer, on this head"
        elif (live_open and current_head and approved_head == current_head
              and snapshot_matches and base_matches and live_approval and same_base):
            outcome, next_turn = "", "you merge"
        else:
            outcome, next_turn = "current approval/head/base unverified — no merge handoff", "check current PR state"
        # The claim for the approved head was freed above; a verified handoff also ends any other.
        released = st.release_if("reviewer", key) if next_turn == "you merge" else False
        if released:
            log(f"released reviewer seat for {key}")
        if (released or next_turn == "you merge") and not approved_early:
            gate.drain_seat(loop, "reviewer")
        observer.notify(loop, st, "approved", number, approved_head,
                        identity=review.get("id"), actor=gate.reviewer_login(review),
                        outcome=outcome, next_turn=next_turn,
                        base_sha=base_sha if base_matches else "")
        silence(f"#{number} approval event — no fixer dispatch")
    if state != "CHANGES_REQUESTED":
        silence(f"verdict state {review.get('state')!r} needs no fix")

    pr_head = gate.head_of(pr)
    if review.get("commit_id") != pr_head:
        silence("verdict is on an older head — superseded")

    # The payload's head is a snapshot from when the review was submitted; the PR may have moved,
    # closed, or had this verdict superseded (a later approval, a dismissal) since. A fix run is a
    # work order, so it needs the live PR at this head *and* this review as the latest effective
    # verdict there — the same chronology rule the approval path uses for a merge handoff.
    current = gh.pr(loop, number)
    if (not isinstance(current, dict) or current.get("number") != number
            or current.get("state") != "open" or current.get("draft")
            or (current.get("base") or {}).get("ref") != loop["base"]
            or ((current.get("user") or {}).get("login") or "").lower() not in loop["fixers"]
            or (current.get("head") or {}).get("sha") != pr_head):
        silence("verdict PR is stale or current state is unverified")
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

    gate.block_pr_agent(
        loop, st, seat, number, pr_head,
        on_queued=lambda: observer.notify(
            loop, st, "verdict", number, pr_head, identity=review.get("id"),
            outcome="changes requested", actor=gate.reviewer_login(review),
            next_turn="fixer queued", round_no=prior + 1))


if __name__ == "__main__":
    main()
