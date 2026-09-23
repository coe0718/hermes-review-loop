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
stdout: that payload with a ``_loop`` block, or ``[SILENT]``
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import gate  # noqa: E402
from review_loop.util import log, silence  # noqa: E402

ACTIONS = {"opened", "ready_for_review", "reopened", "review_requested"}


def main() -> None:
    payload = json.load(sys.stdin)
    loop, st = gate.context(payload)
    pr = gate.pr_of(payload)
    action = payload.get("action", "")

    if action == "closed":
        number = gate.number_of(payload, pr)
        gate.reclaim(loop, number, "merged" if pr.get("merged") else "closed")
        silence()

    if action not in ACTIONS:
        silence(f"action {action!r} is not a review trigger")

    if action == "review_requested":
        # Only an explicit request for THIS seat counts, and only from the fixer side: a
        # request aimed at another reviewer, or from a stranger, is somebody else's business.
        requested = ((payload.get("requested_reviewer") or {}).get("login") or "").lower()
        if requested != loop["reviewer_seat"]:
            silence(f"review requested from {requested or 'nobody'} — not this seat")
        sender = ((payload.get("sender") or {}).get("login") or "").lower()
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

    workspace = gate.take_seat(loop, st, seat, number, head, f"review #{number} @ {head[:7]}",
                               login=(loop["seats"][seat].get("login") or ""))
    # The fixer pushed and asked: their turn is over, and anything queued behind them can start.
    if st.release_if("fixer", gate.seat_key(loop, number)):
        log(f"released fixer seat for {gate.seat_key(loop, number)}")
    gate.drain_seat(loop, "fixer")

    payload["_loop"] = gate.loop_block(loop, number, head, workspace, round=rounds + 1,
                                       role="reviewer")
    st.inflight(f"review:{number}:{head}", record=True)
    gate.ping_start(loop, seat, gate.start_text(loop, seat, number, head, rounds + 1))
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
