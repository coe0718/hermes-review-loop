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
stdout: ``[SILENT]`` (eligible runs queue; whole-agent isolation not available)
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

    state = str(review.get("state", "")).upper()
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
        if live_open and current_head and approved_head and approved_head != current_head:
            outcome, next_turn = "on an older head — the PR moved since", "the reviewer, on this head"
        elif (live_open and current_head and approved_head == current_head
              and snapshot_matches and base_matches and live_approval):
            outcome, next_turn = "", "you merge"
        else:
            outcome, next_turn = "current approval/head unverified — no merge handoff", "check current PR state"
        if next_turn == "you merge":
            if st.release_if("reviewer", key):
                log(f"released reviewer seat for {key}")
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
        silence("verdict is not the latest effective same-head rejection")
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
