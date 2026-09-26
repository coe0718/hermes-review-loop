"""Prove a repo hook's secret end to end: ask GitHub to ping it, then read how the gateway answered.

GitHub never returns a hook's secret, and ``doctor`` (read-only) can only judge a hook by the
deliveries it already has — a hook that has never fired stays unproven. A ping closes that gap:
``POST /repos/{repo}/hooks/{id}/pings`` makes GitHub sign and deliver a ``ping`` event exactly as
it would a real one, and the delivery log records the gateway's status code.

The ping is harmless by construction. The gateway checks the signature first (401 when it does
not verify, 403 for a disabled route or one without a secret) and only then filters events: the
loop's routes subscribe to ``pull_request`` / ``pull_request_review`` only, so a verified ping is
answered 2xx "ignored" and never reaches a gate. Were it ever to reach one, both gates stop at
"no pull_request in payload" before any state is touched.

Only ``arm`` (after it activated the hooks) and ``selftest --ping`` send pings; ``doctor`` never
does.
"""

from __future__ import annotations

import time

from . import gh

# How long to wait for GitHub to record the ping's delivery, and how often to look.
PING_WAIT = 10.0
POLL = 1.0

OK, REJECTED, SILENT, ERROR = "accepted", "rejected", "silent", "error"

# The gateway's answers to a delivery it would not authenticate (see doctor.REJECTED).
_REJECTED = {401: "signature rejected — the hook's secret does not match the route's",
             403: "refused — the route is disabled or holds no secret"}


def deliveries_path(loop: dict, hook_id: int) -> str:
    return f"/repos/{loop['repo']}/hooks/{hook_id}/deliveries?per_page=30"


def _deliveries(loop: dict, hook_id: int, login: str | None) -> tuple[list[dict] | None, str]:
    data, error = gh.fetch(loop, deliveries_path(loop, hook_id), login=login)
    if error:
        return None, error
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        return None, "no delivery list returned"
    return data, ""


def fix_line(loop: dict) -> str:
    return (f"`hermes review-loop uninstall --loop {loop['id']}` then re-run init --hooks, so the "
            "hook and the route share one fresh secret")


def ping(loop: dict, hook_id: int, login: str | None, *, wait: float | None = None,
         sleep=time.sleep, clock=time.monotonic) -> tuple[str, str]:
    """Ping one hook and wait (bounded) for its delivery: ``(status, line)``.

    ``status`` is ``accepted`` (2xx: the signature verified), ``rejected`` (any other status the
    gateway answered — 401 is a wrong secret), ``silent`` (no ping delivery recorded within the
    wait; nothing proven either way) or ``error`` (the ping could not be sent or observed).
    """
    wait = PING_WAIT if wait is None else wait
    login = login or loop.get("read_token")
    before, error = _deliveries(loop, hook_id, login)
    if before is None:
        return ERROR, (f"⚠️ hook {hook_id}: its deliveries cannot be read ({error}), so a ping could "
                       "not be observed — not sent")
    known = {item.get("id") for item in before}
    _, error = gh.fetch(loop, f"/repos/{loop['repo']}/hooks/{hook_id}/pings", method="POST",
                        login=login)
    if error:
        return ERROR, f"❌ hook {hook_id}: ping not sent ({error})"
    deadline = clock() + wait
    while True:
        items, _ = _deliveries(loop, hook_id, login)
        fresh = [item for item in items or [] if item.get("id") not in known
                 and item.get("event") == "ping" and isinstance(item.get("status_code"), int)]
        if fresh:
            code = max(fresh, key=lambda item: str(item.get("delivered_at") or ""))["status_code"]
            if 200 <= code < 300:
                return OK, f"✅ hook {hook_id}: ping answered HTTP {code} — signature accepted"
            reason = _REJECTED.get(code, "the gateway did not accept the delivery")
            return REJECTED, (f"❌ hook {hook_id}: ping answered HTTP {code} — {reason}\n"
                              f"  fix: {fix_line(loop)}")
        if clock() >= deadline:
            return SILENT, (f"⚠️ hook {hook_id}: no ping delivery seen within {wait:g}s — nothing "
                            "proven yet; look again with `hermes review-loop doctor --loop "
                            f"{loop['id']}` or `gh api repos/{loop['repo']}/hooks/{hook_id}/deliveries`")
        sleep(POLL)
