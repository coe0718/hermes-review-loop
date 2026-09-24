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

from review_loop import gate  # noqa: E402
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

    state = str(review.get("state", "")).upper()
    if state == "APPROVED":
        # An approval ends the reviewer's turn exactly as a rejection does, and nothing else would
        # free that slot before it expired. A slot that leaks for `ttl_min` is a queue that stops
        # moving — on a busy repo, that is the difference between ten review slots and nine.
        if st.release_if("reviewer", key):
            log(f"released reviewer seat for {key}")
        gate.drain_seat(loop, "reviewer")
        silence(f"#{number} approved — reviewer's slot freed, nothing for the fixer to do")
    if state != "CHANGES_REQUESTED":
        silence(f"verdict state {review.get('state')!r} needs no fix")

    pr_head = gate.head_of(pr)
    if review.get("commit_id") != pr_head:
        silence("verdict is on an older head — superseded")

    reviews = gate.fetch_reviews(loop, number)
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
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
