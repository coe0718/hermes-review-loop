#!/usr/bin/env python3
"""Accept only a signed-route breach for a still-current, verified PR.

The gateway checks the subscription's HMAC before executing this script. The script
independently checks the local breach marker and GitHub instead of trusting the
incoming ``_loop`` fields (which are only a pointer to the marked PR/head).
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import gate, gh  # noqa: E402
from review_loop.util import silence  # noqa: E402


def main() -> None:
    payload = json.load(sys.stdin)
    loop, st = gate.context(payload)
    if payload.get("action") != "review_loop_breach":
        silence("not a breach wake")
    fields = payload.get("_loop")
    if not isinstance(fields, dict) or fields.get("role") != "adjudicator":
        silence("not an adjudicator wake")
    number = payload.get("number")
    if type(number) is not int or number < 1 or fields.get("pr") != number:
        silence("invalid breach PR number")
    marker = st.breach_get(number)
    if (marker.get("status") != "awaiting-adjudication"
            and not (marker.get("status") == "delivery-pending"
                     and marker.get("delivery_token"))) or marker.get("pr") != number:
        silence("no pending breach for this PR")
    head = marker.get("head")
    rounds = marker.get("rounds")
    if (not isinstance(head, str) or not head or fields.get("head") != head
            or type(rounds) is not int or rounds < loop["cap"]):
        silence("breach marker and wake disagree")

    # Do not adjudicate an unrelated, closed, moved, or unreviewed PR. GitHub
    # failures are unknown facts, never permission to start an agent run.
    pr = gh.pr(loop, number)
    if not isinstance(pr, dict) or pr.get("number") != number or pr.get("state") != "open":
        silence("PR is unavailable or closed")
    if pr.get("draft") or (pr.get("base") or {}).get("ref") != loop["base"]:
        silence("PR is draft or targets another base")
    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author not in loop["fixers"] or (pr.get("head") or {}).get("sha") != head:
        silence("PR author or head no longer matches breach")
    reviews = gate.fetch_reviews(loop, number)
    # An approval can arrive after the cap breach was parked but before its wake.
    # Only the latest effective same-head verdict settles it; an older approval
    # cannot veto a later changes-requested verdict at the cap.
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if latest is not None and gh.review_state(latest) == "APPROVED":
        silence("PR head was approved after the breach")
    if len(gate.verdicts(reviews, loop)) < loop["cap"]:
        silence("review cap is no longer spent")
    # The signed route may redeliver the same POST. Claim only after all fresh
    # facts pass, atomically with other gateway processes, before emitting a run.
    if st.breach_claim(number, head) is None:
        silence("breach wake was already claimed or replaced")

    payload["_loop"] = {**gate.loop_block(loop, number, head, seat="reviewer",
                                          round=rounds, reason=marker.get("reason") or ""),
                        "role": "adjudicator"}
    print(json.dumps(payload))


if __name__ == "__main__":
    main()