#!/usr/bin/env python3
"""Observer route adapter — the script the observer route runs before the gateway delivers.

The loop fires a signed payload at this route exactly as it fires one at a seat's route, and the
gateway runs this script with that payload on stdin (the same contract ``gate_reviewer.py`` and
``gate_fixer.py`` satisfy). Its entire job is to answer one question: *is this a notice at all?*

* a payload carrying an ``_observer`` block passes through, narrowed to that block — the route is
  registered with ``deliver_only``, so the rendered prompt (``{_observer.message}``) becomes the
  literal message that reaches the configured destination. No agent, no model, nothing to review;
* anything else prints ``[SILENT]``, which is how the gateway is told to ignore the request — a
  stray POST that happens to hit this URL must not push a blank line into somebody's chat.

Narrowing matters: the observer route publishes to a chat, and its prompt template is operator-
editable, so what the template can see is a whitelist of fields the loop itself wrote rather than
whatever a caller happened to send.

stdin : the payload the loop fired
stdout: that payload narrowed to its notice block, or ``[SILENT]``
"""

from __future__ import annotations

import json
import sys

# The fields a notice may carry. ``message`` is the rendered line; the rest are the facts behind
# it, kept so an operator can retarget or reword a feed without editing the loop.
FIELDS = ("message", "event", "loop", "pr", "head", "url", "at", "count")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("[SILENT]")
        return
    block = payload.get("_observer") if isinstance(payload, dict) else None
    if not isinstance(block, dict) or not isinstance(block.get("message"), str) \
            or not block["message"].strip():
        print("[SILENT]")
        return
    print(json.dumps({"_observer": {k: v for k, v in block.items() if k in FIELDS}}))


if __name__ == "__main__":
    main()
