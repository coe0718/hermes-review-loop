#!/usr/bin/env python3
"""Reviewer gate — decides whether this event starts a review run.

Route this at the reviewer's profile, on the ``pull_request`` event. It wakes the reviewer
when its turn *starts*:

* ``opened`` / ``ready_for_review`` / ``reopened`` — a new PR needs a first look;
* ``review_requested`` — the fixer asked for one, and only when the request names this
  seat and comes from the fixer side;
* **never** on ``synchronize``. A push is not a turn: the fixer pushes, *then* asks, and
  GitHub clears a review request the moment a verdict lands — so the explicit ask is the
  only signal that means "review me now". Intermediate pushes cost nothing.
* ``closed`` — the review is over (merged or abandoned): reclaim the PR's local disk and
  let it go. No agent runs, no tokens.

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

ACTIONS = {"opened", "ready_for_review", "reopened", "review_requested"}


def main() -> None:
    payload = json.load(sys.stdin)
    loop, st = gate.context(payload)
    pr = gate.pr_of(payload)
    action = payload.get("action", "")

    if action == "closed":
        number = gate.number_of(payload, pr)
        # A delayed close can arrive after a reopen, or while GitHub is unavailable. Neither
        # the event snapshot nor cleanup's quiet zero exit code proves anything was removed.
        current = gh.pr(loop, number)
        if (not isinstance(current, dict) or current.get("number") != number
                or current.get("state") != "closed"):
            silence("close event is stale or current PR state is unavailable")
        closing = "merged" if (current.get("merged") or current.get("merged_at")) else "closed"
        gate.reclaim(loop, number, closing)
        # Reclaim is best-effort and has no success receipt. Report the close, not freed disk.
        observer.notify(loop, st, "closed", number, (current.get("head") or {}).get("sha") or "",
                        identity=closing, outcome=closing, next_turn="nothing — cleanup attempted")
        silence()

    if action not in ACTIONS:
        silence(f"action {action!r} is not a review trigger")

    sender = ((payload.get("sender") or {}).get("login") or "").lower()
    if action == "review_requested":
        # Only an explicit request for THIS seat counts, and only from the fixer side: a
        # request aimed at another reviewer, or from a stranger, is somebody else's business.
        requested = ((payload.get("requested_reviewer") or {}).get("login") or "").lower()
        if requested != loop["reviewer_seat"]:
            silence(f"review requested from {requested or 'nobody'} — not this seat")
        if sender not in set(loop["fixers"]) | {"patchhive"} and sender not in loop["reviewers"]:
            silence(f"sender {sender or 'unknown'} is not a fixer")

    if pr.get("draft"):
        silence("draft PR")
    if (pr.get("base") or {}).get("ref") != loop["base"]:
        silence(f"base is not {loop['base']}")
    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author not in set(loop["fixers"]):
        silence(f"author {author or 'unknown'} is not a fixer for this loop")

    seat = "reviewer"
    number = gate.number_of(payload, pr)
    head = gate.head_of(pr)
    # Every trigger can arrive after a push or close. Never claim a seat or escalate
    # using the event snapshot if GitHub now points at another head.
    current = gh.pr(loop, number)
    if (not isinstance(current, dict) or current.get("number") != number
            or current.get("state") != "open"
            or (current.get("head") or {}).get("sha") != head):
        silence("review trigger is stale or current PR is unavailable")
    # Eligibility must be judged against current facts, not only the old snapshot.
    if (current.get("draft") or (current.get("base") or {}).get("ref") != loop["base"]
            or ((current.get("user") or {}).get("login") or "").lower() not in loop["fixers"]):
        silence("current PR is no longer eligible for this review")

    reviews = gate.fetch_reviews(loop, number)
    if gate.reviewed_at_head(reviews, loop, head):
        silence(f"head {head[:7]} already has a reviewer's verdict")
    if st.inflight(f"review:{number}:{head}"):
        silence(f"a review for head {head[:7]} is already out")

    rounds = len(gate.verdicts(reviews, loop))
    if rounds >= loop["cap"]:
        gate.breach(loop, st, number, head, rounds,
                    f"review cap reached — {loop['cap']} verdicts, no approval; another review "
                    f"would loop forever")
        silence(f"cap reached on #{number} — handed to adjudication")

    # A request *is* the handoff: the fixer pushes, then asks. That is the only signal that means
    # the fixer is finished with this PR, so it is what frees the fixer's slot. A review must never
    # start against a PR the fixer is still working — and everything else (opened, ready_for_review,
    # reopened) is *not* a handoff, so if the fixer still holds this PR the claim below queues us.
    if action == "review_requested" and st.release_if("fixer", gate.seat_key(loop, number)):
        log(f"released fixer seat for {gate.seat_key(loop, number)}")
    gate.drain_seat(loop, "fixer")

    gate.block_pr_agent(
        loop, st, seat, number, head,
        on_queued=lambda: observer.notify(
            loop, st, "handoff" if action == "review_requested" else "opened", number, head,
            identity=action, actor=sender if action == "review_requested" else author,
            next_turn="reviewer queued", round_no=rounds + 1))


if __name__ == "__main__":
    main()
