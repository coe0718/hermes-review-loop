"""Route registry: the cross-process atomic-edit suite, run as a subprocess."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


def group_routes() -> None:
    section("routes — atomic owner-only cross-process registry edits")
    test = subprocess.run([sys.executable, str(ROOT / "tests" / "test_routes_atomic.py")],
                          capture_output=True, text=True)
    if test.returncode:
        print(test.stdout + test.stderr)
    check("route registry regression suite", test.returncode, 0)


def group_delivery_ids() -> None:
    """One notice, one ``X-GitHub-Delivery`` — the gateway's idempotency key.

    The Hermes gateway keys a 3600s idempotency window on that header and drops a POST it has
    seen before. The loop's tags repeat by construction (``opened-7``, ``digest-3``), so an id
    built from the tag plus the current second collapses two distinct notices onto one delivery,
    and the second is recorded as delivered without ever reaching the operator.
    """
    from unittest.mock import patch as mock_patch

    from review_loop import routes

    section("routes — one notice, one delivery id")

    reset(prs={})
    observer_route()
    payload = {"repository": {"full_name": REPO},
               "_observer": {"event": "digest", "message": "two transitions"}}
    # The clock is frozen so "inside one second" is a fact here rather than a race: without the
    # fix these two notices are issued one id, with it they are two deliveries.
    before = len(RECEIVED)
    with mock_patch.object(routes.time, "time", return_value=1_700_000_000):
        routes.fire("widgets-observe", "pull_request", payload, "digest-3", HOST)
        routes.fire("widgets-observe", "pull_request", payload, "digest-3", HOST)
    sent = RECEIVED[before:]
    check("two distinct notices sharing a tag both deliver", len(sent), 2)
    check("  as two distinct delivery ids", sent[0]["delivery"] != sent[1]["delivery"], True)
    check("  each still naming the notice it was sent for",
          [r["delivery"].startswith("digest-3-") for r in sent], [True, True])

    # A re-send of *one* logical delivery is the same delivery: the caller pins the id, so a first
    # attempt that did reach the gateway deduplicates the retry instead of that retry becoming a
    # second copy of one notice. (``observer.retry`` does this for the feed — observer_honesty.)
    before = len(RECEIVED)
    routes.fire("widgets-observe", "pull_request", payload, "retry-digest-3", HOST,
                delivery="digest-3-issued-once")
    check("a pinned id is the header the gateway sees", RECEIVED[before]["delivery"],
          "digest-3-issued-once")


GROUPS = {
    "routes": group_routes,
    "delivery_ids": group_delivery_ids,
}
