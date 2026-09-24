#!/usr/bin/env python3
"""Stall watchdog — the loop's janitor, and the only thing that assumes nothing.

An unattended loop fails by going quiet, and "quiet" is indistinguishable from "nothing to
do". So this reads GitHub state directly instead of trusting pings, markers or logs, and it
names four shapes of stall:

1. the reviewer never posted a verdict for a head that has been sitting there;
2. the fixer never pushed after a verdict;
3. a PR is parked awaiting adjudication;
4. the cap is spent at this head with no approval and no escalation marker.

It also reports stuck *seats* — a lock older than a run could plausibly live, or a request
that has been waiting — and drains whatever queued once its seat is free. A drained request
is re-checked against GitHub before it fires, so a stale queue entry dies instead of
starting a run against a head that has moved on.

Runs from cron (no agent, no tokens). Silent when the loop is paused: a parked loop must
never spend a run, and a watchdog that cries wolf on a deliberate pause gets ignored.

    watchdog.py                       # every configured loop
    watchdog.py --loop attest         # one loop
    watchdog.py --loop attest --drain --seat reviewer     # start a queued run, nothing else
    REVIEW_LOOP_TEST=1 watchdog.py     # ignore the paused check, zero grace (real data)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import config, gate, gh, observer, routes, state as state_mod  # noqa: E402
from review_loop.util import age_min, epoch, log, now_iso  # noqa: E402

TEST = bool(os.environ.get("REVIEW_LOOP_TEST"))
HEAD_RETENTION_SEC = 30 * 86400  # retain absent PRs long enough for transient listing/state changes


def valid_clock(value: object, now: float) -> float | None:
    """Treat corrupt or future persisted clocks as unknown, never as a grace deadline."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    clock = float(value)
    return clock if math.isfinite(clock) and 0 < clock <= now else None


# -- draining ------------------------------------------------------------------


def drain(loop: dict, st: state_mod.LoopState, seat: str, quiet: bool = False) -> int:
    """Start whatever queued while a seat was at capacity. Up to the free slots; the claim holds it."""
    items = st.queue_items(seat)
    if not items:
        return 0
    capacity = config.seat_concurrency(loop, seat)
    live = st.active(seat)
    free = capacity - len(live)
    if free <= 0:
        if not quiet:
            held = ", ".join(f"{k} ({int((time.time() - v.get('at', 0)) / 60)}m)"
                             for k, v in sorted(live.items()))
            print(f"{seat} is at capacity ({len(live)}/{capacity}: {held}) — "
                  f"{len(items)} request(s) queued")
        return 0

    started = 0

    for key in sorted(items, key=lambda k: items[k].get("at", 0)):
        if key in live:
            # A free *other* slot is not permission to wake this PR twice.
            continue
        entry = items[key]
        try:
            number = int(str(key).split("#")[-1])
        except Exception:
            st.queue_pop(seat, key)
            continue

        held = st.held_by_other(seat, key)
        if held:
            # The other seat owns this PR. Its handoff (or its slot expiring) is what frees it —
            # the poller must not start a run on top of it, or it would just re-queue.
            log(f"drain: the {held} seat holds {key} — left queued")
            continue

        pr = gh.pr(loop, number)
        if not isinstance(pr, dict) or not pr:
            log(f"drain: PR #{number} unreadable — left queued")
            continue
        if pr.get("state") != "open":
            st.queue_pop(seat, key)
            log(f"drain: PR #{number} is {pr.get('state')} — dropped from queue")
            continue
        head = (pr.get("head") or {}).get("sha") or ""
        if not head:
            log(f"drain: PR #{number} head unreadable — left queued")
            continue
        if entry.get("head") != head:
            # A queued event is authorization for exactly its observed head. Drop it;
            # a new webhook for the new SHA must be evaluated through the normal gate.
            st.queue_pop(seat, key)
            log(f"drain: PR #{number} moved since queued — stale head dropped")
            continue
        base = (pr.get("base") or {}).get("ref") or ""
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if pr.get("draft") or base != loop["base"] or author not in set(loop["fixers"]):
            st.queue_pop(seat, key)
            log(f"drain: PR #{number} is not a fixer PR on {loop['base']} — dropped")
            continue

        reviews = gh.reviews(loop, number)
        if not isinstance(reviews, list):
            log(f"drain: cannot read a valid review list for #{number} — left queued")
            continue
        short = {"number": number, "draft": False, "base": {"ref": base},
                 "user": {"login": author}, "head": {"sha": head, "ref": (pr.get("head") or {}).get("ref")},
                 "title": pr.get("title", ""), "html_url": pr.get("html_url", "")}

        if seat == "fixer":
            changes = gate.changes_at_head(reviews, loop, head)
            if not changes:
                st.queue_pop(seat, key)
                log(f"drain: no changes-requested verdict at {head[:7]} of #{number} — dropped")
                continue
            payload = {"repository": {"full_name": loop["repo"]}, "action": "submitted",
                       "review": changes[-1], "pull_request": short,
                       "sender": changes[-1].get("user") or {}}
            event, tag = "pull_request_review", f"drain-fix-{number}"
        else:
            if gate.reviewed_at_head(reviews, loop, head):
                st.queue_pop(seat, key)
                log(f"drain: #{number} head {head[:7]} already reviewed — dropped")
                continue
            payload = {"repository": {"full_name": loop["repo"]}, "action": "review_requested",
                       "requested_reviewer": {"login": loop["reviewer_seat"]},
                       "sender": {"login": loop["fixers"][0]}, "number": number,
                       "pull_request": short}
            event, tag = "pull_request", f"drain-review-{number}"

        if routes.fire(loop["seats"][seat]["route"], event, payload, tag, loop.get("host")):
            st.queue_pop(seat, key)
            st.note(f"drained {seat} for {key}")
            if not quiet:
                print(f"{seat}: started the queued run for PR #{number} (head {head[:7]})")
            started += 1
            if started >= free:
                break
            continue
        break
    return started


# -- the sweep ------------------------------------------------------------------


def drain_queued(loop: dict, st: state_mod.LoopState, lines: list[str]) -> None:
    """Free capacity is a scheduling signal, not a stall notification."""
    for seat in ("reviewer", "fixer"):
        if drain(loop, st, seat, quiet=True):
            lines.append(f"started the queued {seat} run whose wait was over")


def retry_pending_breaches(loop: dict, st: state_mod.LoopState, prs: list) -> None:
    """Retry listed eligible heads only after reviews verify the cap.

    The first armed sweep baselines stall clocks, not pending delivery. The
    breach gate rechecks the live head under its delivery lock before POST.
    """
    markers = st.breach_all()
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("draft"):
            continue
        if (pr.get("base") or {}).get("ref") != loop["base"]:
            continue
        if ((pr.get("user") or {}).get("login") or "").lower() not in loop["fixers"]:
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha")
        if type(number) is not int or not head:
            continue
        marker = markers.get(f"{loop['repo']}#{number}")
        if (not isinstance(marker, dict) or gate.breach_delivery_status(marker, head) != "delivery-pending"
                or marker.get("head") != head):
            continue
        reviews = gh.reviews(loop, number)
        if not isinstance(reviews, list):
            continue
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is not None and gh.review_state(latest) == "APPROVED":
            continue
        changes = gate.verdicts(reviews, loop)
        if len(changes) >= loop["cap"]:
            gate.breach(loop, st, number, head, marker.get("rounds", len(changes)),
                        marker.get("reason", "review cap reached"))


def sweep_loop(loop: dict, st: state_mod.LoopState) -> list[str]:
    lines: list[str] = []
    watch = st.watch()
    now = time.time()

    if not TEST and not gate.hooks_armed(loop):
        return lines                              # parked on purpose: say nothing, ever

    prs = gh.open_prs(loop)
    if not isinstance(prs, list):
        # Without a complete listing, even individually readable PRs cannot establish
        # that the sweep's scheduling view is current. Explicit --drain still rechecks.
        lines.append(f"⚠️ {loop['id']}: could not list open PRs — stall scan and queue drain skipped this run")
        return lines

    # A commit's authored/committed date says nothing about when its SHA reached a PR.
    # Snapshot the heads on the first *successful* armed sweep, before any stall evaluation.
    # Those heads are history; later SHA changes get their own durable observation clock.
    # A malformed persisted arming clock has no trustworthy ordering against PR
    # creation or prior observations. Re-arm only after this successful listing and
    # baseline its heads; never coerce strings/bools or reuse old grace clocks.
    first_sweep = valid_clock(watch.get("armed_since"), now) is None
    if first_sweep:
        watch["armed_since"] = now
    heads = {} if first_sweep else watch.get("heads")
    if not isinstance(heads, dict):
        heads = {}
    current_heads: dict[str, dict] = {}
    for key, previous in heads.items():
        if not isinstance(previous, dict) or not previous.get("sha"):
            continue
        # Older state has no last_seen_at. Give it one bounded retention window
        # instead of erasing a live observation during the schema transition.
        last_seen = valid_clock(previous.get("last_seen_at"), now) or now
        if now - last_seen < HEAD_RETENTION_SEC:
            current_heads[key] = {"sha": previous["sha"],
                                  "observed_at": valid_clock(previous.get("observed_at"), now),
                                  "last_seen_at": last_seen}
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if author not in set(loop["fixers"]) or pr.get("draft"):
            continue
        if (pr.get("base") or {}).get("ref") != loop["base"]:
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha") or ""
        if not number or not head:
            continue
        key = str(number)
        previous = current_heads.get(key, {})
        if previous.get("sha") == head:
            current_heads[key] = {**previous, "last_seen_at": now}
        else:
            # A first-seen old PR could be preexisting; created_at only establishes
            # eligibility for genuinely new PRs, never the time of a later push.
            new_pr = epoch(pr.get("created_at")) >= int(watch["armed_since"])
            current_heads[key] = {"sha": head, "observed_at":
                                  now if previous or (not first_sweep and new_pr) else None,
                                  "last_seen_at": now}
    watch["heads"] = current_heads
    st.watch_save(watch)                 # persist observations even if review reads fail
    retry_pending_breaches(loop, st, prs)
    if first_sweep:
        st.note("loop observed armed — head snapshot set; existing heads excluded")
        drain_queued(loop, st, lines)
        if observer.retry(loop, st):
            log("observer: retried an undelivered notice")
        observer.flush(loop, st, wait_s=0 if TEST else observer.digest_wait(loop))
        return lines

    grace = 0.0 if TEST else loop["grace_min"]
    marker_grace = 0.0 if TEST else loop["marker_grace_min"]
    cooldown = 0.0 if TEST else loop["cooldown_h"] * 3600
    breach = st.breach_all()
    alerts: list[tuple[int, str, str]] = []
    seen: dict[str, float] = {}

    for pr in prs:
        if not isinstance(pr, dict):
            continue
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if author not in set(loop["fixers"]) or pr.get("draft"):
            continue
        if (pr.get("base") or {}).get("ref") != loop["base"]:
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha") or ""
        if not number or not head:
            continue

        reviews = gh.reviews(loop, number)
        if not isinstance(reviews, list):
            continue                              # unknown beats wrong
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is not None and gh.review_state(latest) == "APPROVED":
            continue                              # approved at this head: the loop is done here

        at_head = (gate.changes_at_head(reviews, loop, head)
                   if latest is not None and gh.review_state(latest) == "CHANGES_REQUESTED"
                   else [])
        changes = gate.verdicts(reviews, loop)
        marker = breach.get(f"{loop['repo']}#{number}") or {}
        if gate.breach_delivery_status(marker, head) == "delivery-pending":
            continue  # failed delivery is not a silent stall
        observed_at = current_heads[str(number)]["observed_at"]
        head_postdates_arming = TEST or observed_at is not None
        kind = ""

        if marker.get("head") == head and age_min(marker.get("at")) > marker_grace:
            kind = (f"parked awaiting adjudication for {age_min(marker.get('at')) / 60:.1f}h "
                    f"(marker {marker.get('at') or 'unknown'})")
        elif len(changes) >= loop["cap"] and head_postdates_arming:
            kind = (f"{len(changes)} verdicts, no approval and NO escalation marker — "
                    f"the cap may not have fired")
        elif at_head:
            mins = age_min(at_head[-1].get("submitted_at"))
            if mins > grace:
                kind = (f"fixer never pushed — changes requested {mins / 60:.1f}h ago at head "
                        f"{head[:7]} by {gate.reviewer_login(at_head[-1])}")
        else:
            mins = (now - observed_at) / 60 if observed_at is not None else 0.0
            if (TEST or mins > grace) and head_postdates_arming:
                kind = (f"reviewer never posted a verdict — head {head[:7]} observed "
                        f"{mins / 60:.1f}h ago, 0 verdicts at this head")

        if kind:
            seen[f"{number}:{head[:7]}:{kind[:24]}"] = now
            if now - watch.get("alerts", {}).get(f"{number}:{head[:7]}:{kind[:24]}", 0) > cooldown:
                alerts.append((number, kind, (pr.get("title") or "")[:60]))

    stuck: list[str] = []
    for seat, entries in (st._load(st.locks, {}) or {}).items():
        for key, entry in (entries or {}).items():
            age = (now - entry.get("at", now)) / 60
            if age > loop["ttl_min"] * 2:
                stuck.append(f"  {seat} slot held {age:.0f}m on {key} — that run died; the slot "
                             f"frees itself at {loop['ttl_min']}m")
    for seat, items in st.queue_all().items():
        for key, entry in (items or {}).items():
            age = (now - entry.get("at", now)) / 60
            if age > loop["grace_min"]:
                stuck.append(f"  {seat} queue: {key} waiting {age:.0f}m — {entry.get('reason')}")

    if alerts or stuck:
        header = f"[{loop['id']}] {loop['repo']}"
        if alerts:
            lines.append(f"⚠️ Review loop {header} — {len(alerts)} silent stall(s):")
            for number, kind, title in alerts:
                lines.append(f"  #{number}  {kind}")
                lines.append(f"        {title}")
        if stuck:
            lines.append(f"⚠️ Review loop {header} — {len(stuck)} stuck state(s):")
            lines.extend(stuck)
        lines.append("Pending adjudicator delivery retries on the next sweep; other stalls "
                     "need investigation. Check the gateway log before re-driving a route.")

    drain_queued(loop, st, lines)

    # The observer feed, last and best effort. Only the alerts this sweep actually decided to
    # raise become notices (each stamped with the sweep's own clock, so re-raising a stall after
    # the cooldown is a new notice while a second sweep in the same breath is not), and a
    # destination that cannot be reached costs a retry, never this sweep's job.
    for number, kind, _title in alerts:
        head = next(((pr.get("head") or {}).get("sha", "") for pr in prs
                     if isinstance(pr, dict) and pr.get("number") == number), "")
        observer.notify(loop, st, "stall", number, head, identity=f"{kind[:40]}#{int(now)}",
                        outcome=kind, next_turn="you")
    if observer.retry(loop, st):
        log("observer: retried an undelivered notice")
    observer.flush(loop, st, wait_s=0 if TEST else observer.digest_wait(loop))

    history = {**watch.get("alerts", {}), **seen}
    watch["alerts"] = {k: v for k, v in history.items() if now - v < 30 * 86400}
    watch["last_run"] = now_iso()
    st.watch_save(watch)
    st.note(f"run: {len(alerts)} alert(s), {len(stuck)} stuck, {len(prs)} open PRs")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Stall watchdog for configured review loops")
    ap.add_argument("--loop", help="loop id (default: every configured loop)")
    ap.add_argument("--drain", action="store_true", help="start a queued run and print nothing else")
    ap.add_argument("--seat", default="reviewer", choices=["reviewer", "fixer"])
    args = ap.parse_args()

    loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    if not loops and args.loop:
        print(f"no loop config named {args.loop}")
        return

    if args.drain:
        for loop in loops:
            st = state_mod.state_for(loop)
            if not TEST and not gate.hooks_armed(loop):
                if args.loop:
                    print(f"{loop['id']}: hooks are paused — nothing drained")
                continue
            fired = drain(loop, st, args.seat)
            if not fired and not st.queue_items(args.seat) and args.loop:
                print(f"{loop['id']}: {args.seat} queue empty")
        return

    out: list[str] = []
    for loop in loops:
        try:
            out.extend(sweep_loop(loop, state_mod.state_for(loop)))
        except Exception as exc:                  # one bad loop must not hide the others
            out.append(f"⚠️ Review loop [{loop.get('id', '?')}] watchdog failed: "
                       f"{type(exc).__name__}: {exc}")
    if out:
        print("\n".join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                      # never crash the scheduler silently
        print(f"⚠️ Review loop watchdog failed: {type(exc).__name__}: {exc}")
        sys.exit(0)
