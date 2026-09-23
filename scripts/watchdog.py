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
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import config, gate, gh, routes, state as state_mod  # noqa: E402
from review_loop.util import age_min, log, now_iso  # noqa: E402

TEST = bool(os.environ.get("REVIEW_LOOP_TEST"))


# -- is the loop actually live? -------------------------------------------------


def hooks_armed(loop: dict) -> bool:
    """Both seat routes must exist as active repo hooks, or the loop is parked."""
    hooks = gh.api(loop, f"/repos/{loop['repo']}/hooks?per_page=100")
    if not isinstance(hooks, list):
        return False
    wanted = {loop["seats"]["reviewer"]["route"], loop["seats"]["fixer"]["route"]}
    found = [h for h in hooks
             if isinstance(h, dict) and any(name in (h.get("config") or {}).get("url", "")
                                            for name in wanted)]
    return bool(found) and all(h.get("active") for h in found)


# -- draining ------------------------------------------------------------------


def drain(loop: dict, st: state_mod.LoopState, seat: str, quiet: bool = False) -> int:
    """Start whatever queued while a seat was busy. One at a time; the lock then holds it."""
    items = st.queue_items(seat)
    if not items:
        return 0
    if not st.seat_free(seat):
        if not quiet:
            held = st.seat_holder(seat)
            print(f"{seat} is still busy ({held.get('key')}) — {len(items)} request(s) queued")
        return 0

    for key in sorted(items, key=lambda k: items[k].get("at", 0)):
        entry = items[key]
        try:
            number = int(str(key).split("#")[-1])
        except Exception:
            st.queue_pop(seat, key)
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
        base = (pr.get("base") or {}).get("ref") or ""
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if pr.get("draft") or base != loop["base"] or author not in set(loop["fixers"]):
            st.queue_pop(seat, key)
            log(f"drain: PR #{number} is not a fixer PR on {loop['base']} — dropped")
            continue

        reviews = gh.reviews(loop, number)
        if reviews is None:
            log(f"drain: cannot read reviews for #{number} — left queued")
            continue
        reviews = reviews if isinstance(reviews, list) else []
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
            return 1
        return 0
    return 0


# -- the sweep ------------------------------------------------------------------


def sweep_loop(loop: dict, st: state_mod.LoopState) -> list[str]:
    lines: list[str] = []
    watch = st.watch()
    now = time.time()

    if not TEST and not hooks_armed(loop):
        return lines                              # parked on purpose: say nothing, ever

    if not watch.get("armed_since"):
        # First sighting of a live loop: stamp the moment. Heads older than this predate the
        # loop being armed, so they are history, not stalls.
        watch["armed_since"] = now
        st.watch_save(watch)
        st.note("loop observed armed — baseline set; older heads excluded")
        return lines

    armed_since = 0.0 if TEST else watch["armed_since"]
    grace = 0.0 if TEST else loop["grace_min"]
    marker_grace = 0.0 if TEST else loop["marker_grace_min"]
    cooldown = 0.0 if TEST else loop["cooldown_h"] * 3600
    breach = st.breach_all()

    prs = gh.open_prs(loop)
    if not isinstance(prs, list):
        return [f"⚠️ {loop['id']}: could not list open PRs — nothing checked this run"]

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
        if reviews is None:
            continue                              # unknown beats wrong
        reviews = reviews if isinstance(reviews, list) else []
        if gate.approved_at_head(reviews, loop, head):
            continue                              # approved at this head: the loop is done here

        at_head = gate.changes_at_head(reviews, loop, head)
        changes = gate.verdicts(reviews, loop)
        marker = breach.get(f"{loop['repo']}#{number}") or {}
        pushed_epoch = gh.commit_epoch(loop, head)
        head_postdates_arming = TEST or pushed_epoch > armed_since
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
            mins = (time.time() - pushed_epoch) / 60 if pushed_epoch else 0.0
            if mins > grace and head_postdates_arming:
                kind = (f"reviewer never posted a verdict — head {head[:7]} pushed "
                        f"{mins / 60:.1f}h ago, 0 verdicts at this head")

        if kind:
            seen[f"{number}:{head[:7]}:{kind[:24]}"] = now
            if now - watch.get("alerts", {}).get(f"{number}:{head[:7]}:{kind[:24]}", 0) > cooldown:
                alerts.append((number, kind, (pr.get("title") or "")[:60]))

    stuck: list[str] = []
    for seat, entry in (st._load(st.locks, {}) or {}).items():
        age = (now - entry.get("at", now)) / 60
        if age > loop["ttl_min"] * 2:
            stuck.append(f"  {seat} seat held {age:.0f}m on {entry.get('key')} — that run died; "
                         f"the seat frees itself at {loop['ttl_min']}m")
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
        lines.append("Nothing here is retrying itself. Check the gateway log for the run, or "
                     "re-drive the route by hand.")
        for seat in ("reviewer", "fixer"):
            if drain(loop, st, seat, quiet=True):
                lines.append(f"started the queued {seat} run whose wait was over")

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
            if not TEST and not hooks_armed(loop):
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
