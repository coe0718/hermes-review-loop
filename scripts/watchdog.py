#!/usr/bin/env python3
"""Stall watchdog — the loop's janitor, and the only thing that assumes nothing.

An unattended loop fails by going quiet, and "quiet" is indistinguishable from "nothing to
do". So this reads GitHub state directly instead of trusting pings, markers or logs, and it
names four shapes of stall:

1. the reviewer never posted a verdict for a head that has been sitting there;
2. the fixer never pushed after a verdict (or is held because the loop has not opted in to
   unattended fixer pushes — reported once per head, with the command that enables them);
3. a PR is parked awaiting adjudication;
4. the cap is spent at this head with no approval and no escalation marker.

It also reports stuck *seats* — a lock older than a run could plausibly live, or a request
that has been waiting — and drains whatever queued once its seat is free. A drained request
is re-checked against GitHub before it fires, so a stale queue entry dies instead of
starting a run against a head that has moved on.

Runs from cron (no agent, no tokens). Silent when the loop is paused: a parked loop must
never spend a run, and a watchdog that cries wolf on a deliberate pause gets ignored. *Not*
silent when it cannot tell: a hook list or ``/user`` it cannot read (a dead token, a 5xx, no
network) is an alert naming the login and status, re-raised every cooldown, while the parts
that need no GitHub read (route self-heal, ledger notices, observer retries) keep running. Each
sweep also warns before the read token's expiry date.

    watchdog.py                       # every configured loop
    watchdog.py --loop attest         # one loop
    watchdog.py --loop attest --drain --seat reviewer     # start a queued run, nothing else
    REVIEW_LOOP_TEST=1 watchdog.py     # ignore the paused check, zero grace (real data)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import config, gate, gate_failures, gh, observer, route_intent, routes, situation, transition, state as state_mod  # noqa: E402
from review_loop.util import age_min, epoch, log, now_iso  # noqa: E402

TEST = bool(os.environ.get("REVIEW_LOOP_TEST"))
PUSH_OFF_KIND = "fixer held — unattended fixer pushes are off"
HEAD_RETENTION_SEC = 30 * 86400  # retain absent PRs long enough for transient listing/state changes
# A 401/403 is a verdict about the token and alerts at once; a 5xx or no answer at all can be a
# blip, so it alerts once reads have failed this many sweeps in a row.
READ_FAILURE_SWEEPS = 3
EXPIRY_WARN_DAYS = 7
EXPIRY_WARN_EVERY_SEC = 86400
SCRIPTS = pathlib.Path(__file__).resolve().parent
# A hanging GitHub must not stall the sweep (#75): every read is capped, and the whole run has
# a budget; when it is spent the sweep stops, says so, and the next cron run starts fresh.
WATCHDOG_BUDGET_S = 600.0
WATCHDOG_PER_CALL_S = 20.0


def watchdog_budget() -> float:
    try:
        value = float(os.environ.get("REVIEW_LOOP_WATCHDOG_BUDGET_S", WATCHDOG_BUDGET_S))
    except ValueError:
        value = WATCHDOG_BUDGET_S
    return value if 0 < value <= 86400 else WATCHDOG_BUDGET_S


def budget_spent_line(where: str, budget: float, exc: BaseException) -> str:
    return (f"⚠️ Review loop {where} watchdog stopped: GitHub did not answer within the "
            f"sweep's {budget:g}s budget ({exc}) — the rest of this run was skipped; the next "
            f"sweep starts fresh")


def sweep_gate_failures(ledger: gate_failures.Ledger, header: str, cooldown_s: float,
                        may_redrive: bool) -> list[str]:
    """A gate that crashed, overran, or silenced after a failed read (#75): alert, re-drive."""
    try:
        return gate_failures.sweep(ledger, header, SCRIPTS, cooldown_s=cooldown_s,
                                   may_redrive=may_redrive)
    except Exception as exc:                      # never let this hide the stall scan
        return [f"⚠️ Review loop {header} gate-failure sweep failed: {type(exc).__name__}: {exc}"]


def valid_clock(value: object, now: float) -> float | None:
    """Treat corrupt or future persisted clocks as unknown, never as a grace deadline."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    clock = float(value)
    return clock if math.isfinite(clock) and 0 < clock <= now else None


def alert_due(watch: dict, key: str, now: float, cooldown: float) -> bool:
    """True at most once per ``cooldown`` for ``key``, and again after each cooldown passes.

    Stamped only when it fires: a condition that persists re-alerts every cooldown instead of
    refreshing its own clock on every sweep and never speaking again (the stall map's #77).
    """
    marks = watch.get("github_alerts")
    if not isinstance(marks, dict):
        marks = watch["github_alerts"] = {}
    last = valid_clock(marks.get(key), now)
    if last is not None and now - last < cooldown:
        return False
    marks[key] = now
    return True


def parse_expiry(value: str) -> float | None:
    """GitHub's expiry header (``2026-10-01 12:00:00 UTC``, or a numeric offset) as epoch."""
    from datetime import datetime, timezone
    text = str(value or "").strip().replace(" UTC", " +0000")
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def github_health(loop: dict, st: state_mod.LoopState, watch: dict, now: float,
                  armed: bool | None, armed_error: str) -> list[str]:
    """Can this sweep read GitHub at all, as whom, and for how much longer?

    Runs every armed-or-unknown sweep, before anything that needs GitHub. Blindness is said out
    loud (with the login and HTTP status), re-raised every cooldown until reads work, and its
    end is said once. Also surfaces a gate's last failed read, which otherwise reached only the
    gateway's stderr while the event it could not verify was dropped as "unavailable".
    """
    lines: list[str] = []
    header = f"[{loop['id']}] {loop['repo']}"
    cooldown = 0.0 if TEST else float(loop.get("cooldown_h", 6)) * 3600
    configured = str(loop.get("read_token") or "the read token")
    probe = gh.auth_probe(loop)
    who = (probe.data.get("login") if isinstance(probe.data, dict) else None) or configured

    error, status = "", None
    if probe.error:
        error, status = probe.error, probe.status if probe.status is not None else gh.status_of(probe.error)
    elif armed is None:
        error, status = f"hook list: {armed_error or 'unreadable'}", gh.status_of(armed_error)

    record = watch.get("github_read") if isinstance(watch.get("github_read"), dict) else {}
    if error:
        sweeps = record["sweeps"] + 1 if type(record.get("sweeps")) is int else 1
        since = valid_clock(record.get("since"), now) or now
        record = {**record, "sweeps": sweeps, "since": since, "error": error[:200],
                  "status": status, "login": who}
        if status in (401, 403) or sweeps >= READ_FAILURE_SWEEPS:
            if alert_due(watch, f"read:{status or 'none'}", now, cooldown):
                record["alerted"] = True
                hint = gh.failure_hint(status)
                lines.append(
                    f"⚠️ Review loop {header}: cannot read GitHub as {who}: {error}"
                    f"{f' — {hint}' if hint else ''} ({sweeps} sweep(s) since "
                    f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(since))}). "
                    + ("Hook state unknown: stall scan and queue drain are skipped; "
                       if armed is None else "")
                    + "route self-heal and notices continue.")
        watch["github_read"] = record
    else:
        if record.get("alerted"):
            lines.append(f"✅ Review loop {header}: GitHub reads work again as {who} "
                         f"(after {record.get('sweeps', '?')} failed sweep(s))")
        watch.pop("github_read", None)
        marks = watch.get("github_alerts")
        if isinstance(marks, dict):
            for key in [k for k in marks if k.startswith("read:")]:
                marks.pop(key)

    expiry = (probe.headers or {}).get(gh.TOKEN_EXPIRY_HEADER) if not probe.error else None
    expires = parse_expiry(expiry) if expiry else None
    if expires is not None and expires - now < EXPIRY_WARN_DAYS * 86400:
        if alert_due(watch, f"expiry:{int(expires)}", now,
                     0.0 if TEST else EXPIRY_WARN_EVERY_SEC):
            days = (expires - now) / 86400
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires))
            named = who if who == configured else f"{who} ({configured})"
            lines.append(f"⚠️ Review loop {header}: the read token for {named} "
                         + (f"expires {when} — in {days:.1f} day(s); rotate it before then or "
                            "this loop goes blind" if days > 0 else
                            f"expired {when}; rotate it now"))

    failure = st.github_failure()
    at = valid_clock(failure.get("at"), now)
    seen = valid_clock(watch.get("gate_failure_seen"), now) or 0.0
    # A gate's failed read that its gate-failure entry owns is alerted (and re-driven) by that
    # ledger's sweep; saying it here too would report one read twice.
    if at is not None and at > seen and not failure.get("owned_by"):
        watch["gate_failure_seen"] = at
        status = failure.get("status") if type(failure.get("status")) is int else None
        if alert_due(watch, f"gate:{failure.get('where')}:{status or 'none'}", now, cooldown):
            hint = gh.failure_hint(status)
            number = re.search(r"/pulls/(\d+)", str(failure.get("path") or ""))
            lines.append(
                f"⚠️ Review loop {header}: {failure.get('where') or 'a gate'} could not "
                f"{failure.get('method') or 'GET'} {failure.get('path') or '?'} as "
                f"{failure.get('login') or configured} at "
                f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(at))}: "
                f"{failure.get('error') or 'unknown error'}{f' — {hint}' if hint else ''}; "
                + ("it treated the PR as unavailable and started nothing"
                   if (failure.get("method") or "GET") == "GET" else "that call did not take effect")
                + (f" — `hermes review-loop explain --loop {loop['id']} --pr {number.group(1)}`"
                   " shows it" if number else ""))
    return lines


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
        if str(entry.get("reason") or "").startswith("route delivery uncertain —"):
            log(f"drain: {key} has an unverified POST — manual reconciliation required")
            continue
        if config.is_fixer_push_hold(entry) and not config.unattended_fixer_push_enabled(loop):
            # Held for the operator, not for capacity: nothing to wake until the loop opts in.
            # Once it has, this entry drains like any other — the fixer gate re-checks the live
            # verdict and admits a *new* run under the new policy (no older run is upgraded).
            continue
        try:
            number = int(str(key).split("#")[-1])
        except Exception:
            st.queue_pop_if(seat, key, entry)
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
            st.queue_pop_if(seat, key, entry)
            log(f"drain: PR #{number} is {pr.get('state')} — dropped from queue")
            continue
        head = (pr.get("head") or {}).get("sha") or ""
        if not head:
            log(f"drain: PR #{number} head unreadable — left queued")
            continue
        if entry.get("head") != head:
            # A queued event is authorization for exactly its observed head. Drop it;
            # a new webhook for the new SHA must be evaluated through the normal gate.
            st.queue_pop_if(seat, key, entry)
            log(f"drain: PR #{number} moved since queued — stale head dropped")
            continue
        base = (pr.get("base") or {}).get("ref") or ""
        stacked = (st.watch().get("stacked_wait") or {}).get(str(number))
        if isinstance(stacked, dict) and stacked.get("base") != base:
            if base == loop["base"] and transition.record(loop, st, number, head, base):
                status, detail = transition.start_fresh_review(loop, st, number, live=pr)
                log(f"drain: PR #{number} retarget: fresh review {status} ({detail})")
            st.queue_pop_head(seat, key, stacked.get("head"))
            log(f"drain: PR #{number} retargeted since stacked observation — stale request dropped")
            continue
        boundary = transition.hold(st, number, head)
        if boundary and transition.baseline_missing(boundary):
            st.queue_pop_head(seat, key, head)
            log(f"drain: PR #{number} same-head retarget held — {transition.MISSING_BASELINE}")
            continue
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if pr.get("draft") or base != loop["base"] or author not in set(loop["fixers"]):
            st.queue_pop_if(seat, key, entry)
            log(f"drain: PR #{number} is not a fixer PR on {loop['base']} — dropped")
            continue

        # A held head's queue entry is post-boundary (record() dropped the old ones); its
        # checks still see only host-receipted post-boundary reviews.
        reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
        if not isinstance(reviews, list):
            log(f"drain: cannot read a valid review list or receipts for #{number} — left queued")
            continue
        short = {"number": number, "draft": False, "base": {"ref": base},
                 "user": {"login": author}, "head": {"sha": head, "ref": (pr.get("head") or {}).get("ref")},
                 "title": pr.get("title", ""), "html_url": pr.get("html_url", "")}

        if seat == "fixer":
            # Unknown chronology is not proof that the queued verdict was superseded.
            # Any changes-requested at this head is not enough: a later approval (or an
            # undatable verdict) at the same head means there is no fix to order.
            latest = gate.latest_effective_review_at_head(reviews, loop, head)
            if latest is None:
                log(f"drain: latest verdict at {head[:7]} of #{number} unknown — left queued")
                continue
            if gh.review_state(latest) != "CHANGES_REQUESTED":
                st.queue_pop_if(seat, key, entry)
                log(f"drain: latest verdict at {head[:7]} of #{number} is not changes-requested — dropped")
                continue
            payload = {"repository": {"full_name": loop["repo"]}, "action": "submitted",
                       "review": latest, "pull_request": short,
                       "sender": latest.get("user") or {}}
            event, tag = "pull_request_review", f"drain-fix-{number}"
        else:
            if gate.reviewed_at_head(reviews, loop, head):
                st.queue_pop_if(seat, key, entry)
                log(f"drain: #{number} head {head[:7]} already reviewed — dropped")
                continue
            payload = {"repository": {"full_name": loop["repo"]}, "action": "review_requested",
                       "requested_reviewer": {"login": loop["reviewer_seat"]},
                       "sender": {"login": loop["fixers"][0]}, "number": number,
                       "pull_request": short}
            event, tag = "pull_request", f"drain-review-{number}"

        attempted = False
        def mark_attempt() -> None:
            nonlocal attempted
            attempted = True

        posted = routes.fire(loop["seats"][seat]["route"], event, payload, tag,
                             loop.get("host"), on_attempt=mark_attempt)
        # HTTP 2xx only acknowledges webhook receipt: the gate may have emitted [SILENT]
        # because no private runtime exists. Only the gate can remove its observed queue
        # entry, after Supervisor.enqueue succeeds. Never pop a replacement here.
        current = st.queue_items(seat).get(key)
        if posted and current is None:
            st.note(f"drained {seat} for {key}")
            if not quiet:
                print(f"{seat}: started the queued run for PR #{number} (head {head[:7]})")
            started += 1
            if started >= free:
                break
            continue
        if current == entry and (attempted or posted):
            st.queue_replace_if(seat, key, entry, entry["head"], entry.get("url", ""),
                                "route delivery uncertain — manual reconciliation required")
        # Known pre-POST failure remains retryable; a potentially sent POST
        # without a gate acknowledgement requires operator inspection.
        break
    return started


# -- the sweep ------------------------------------------------------------------


def drain_queued(loop: dict, st: state_mod.LoopState, lines: list[str]) -> None:
    """Free capacity is a scheduling signal, not a stall notification."""
    for seat in ("reviewer", "fixer"):
        if drain(loop, st, seat, quiet=True):
            lines.append(f"started the queued {seat} run whose wait was over")


def reconcile_stacked(loop: dict, st: state_mod.LoopState, watch: dict,
                      prs: list[dict], lines: list[str]) -> None:
    """Separate visibility queue; never a seat authorization or drain input."""
    previous = watch.get("stacked_wait")
    pending = dict(previous) if isinstance(previous, dict) else {}
    listed = set()
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") != "open":
            continue
        number = pr.get("number")
        if type(number) is not int or number <= 0:
            continue
        key = str(number)
        base = (pr.get("base") or {}).get("ref")
        prior_head = (watch.get("heads") or {}).get(key)
        # A draft-only sweep can have removed the visibility entry while retaining
        # its stacked head observation. Restore that boundary before reconciling.
        if (base == loop["base"] and key not in pending and isinstance(prior_head, dict)
                and prior_head.get("base") not in (None, loop["base"])):
            pending[key] = {"head": prior_head.get("sha"), "base": prior_head["base"]}
        # Drafts are not scheduling candidates, but an observed stacked child
        # can be retargeted while draft. Quarantine before skipping draft work.
        if pr.get("draft") and not (key in pending and base == loop["base"]):
            continue
        listed.add(key)
        if (not base or base == loop["base"] or
                ((pr.get("user") or {}).get("login") or "").lower() not in loop["fixers"]):
            if key in pending:
                if base == loop["base"]:
                    live = gh.pr(loop, number)
                    if (not isinstance(live, dict) or live.get("number") != number
                            or live.get("state") != "open" or
                            (live.get("head") or {}).get("sha") != (pr.get("head") or {}).get("sha") or
                            (live.get("base") or {}).get("ref") != base or
                            (live.get("base") or {}).get("sha") != (pr.get("base") or {}).get("sha")):
                        continue
                    entry = transition.record(loop, st, number, (pr.get("head") or {}).get("sha"),
                                              base, watch=watch)
                    if entry:
                        # Owner policy (#23): the parent merged and the child is on trunk
                        # now, so start a fresh review situation — one isolated reviewer
                        # turn. Nothing from before the boundary carries over.
                        status, detail = transition.start_fresh_review(loop, st, number, live=live)
                        next_turn = {"enqueued": "reviewer queued (fresh review)",
                                     "retry": "fresh reviewer enqueue retries next sweep"}.get(
                                         status, "you")
                        lines.append(f"#{number} retargeted to {base} at the same head — old "
                                     "reviews, rounds and queued work quarantined; fresh review: "
                                     f"{status} ({detail})")
                        observer.notify(loop, st, "stall", number, entry["head"],
                                        identity=f"retarget:{entry['head']}",
                                        outcome="old same-head reviews quarantined; fresh review "
                                        f"{status}", next_turn=next_turn)
                # A retarget is not permission to reinterpret a previous seat request
                # for the same child SHA as a trunk request.
                for seat in ("reviewer", "fixer"):
                    st.queue_pop_head(seat, f"{loop['repo']}#{number}", pending[key].get("head"))
                pending.pop(key)
            continue
        resolution = situation.resolve(loop, number, listing=prs)
        head = (pr.get("head") or {}).get("sha")
        base_sha = (pr.get("base") or {}).get("sha")
        if (resolution.status == "eligible" or
                resolution.identity and (resolution.identity.head_sha != head or
                                         resolution.identity.base_ref != base or
                                         resolution.identity.base_sha != base_sha)):
            # Individual read disagrees with the listing; retry next sweep.
            continue
        branch_heads = []
        ref, visited = base, set()
        for _ in range(situation.MAX_PARENT_DEPTH):
            if ref in visited or ref == loop["base"]:
                break
            visited.add(ref)
            matches = [p for p in prs if isinstance(p, dict) and
                       isinstance(p.get("head"), dict) and p["head"].get("ref") == ref]
            branch_heads.append(sorted(
                [(p.get("number"), p["head"].get("sha"),
                  (p.get("base") or {}).get("ref"), (p.get("base") or {}).get("sha"),
                  p.get("state")) for p in matches], key=lambda item: str(item)))
            if len(matches) != 1:
                break
            ref = (matches[0].get("base") or {}).get("ref")
        generation = hashlib.sha256(json.dumps(
            [head, base, base_sha, branch_heads, resolution.status, resolution.reason,
             resolution.identity.key if resolution.identity else ""],
            separators=(",", ":")).encode()).hexdigest()
        if (pending.get(key) or {}).get("generation") == generation:
            continue
        pending[key] = {"head": head, "base": base, "base_sha": base_sha,
                        "generation": generation, "status": resolution.status,
                        "reason": resolution.reason, "parents": list(resolution.parents),
                        "identity": resolution.identity.key if resolution.identity else "",
                        "at": time.time()}
        lines.append(f"#{number} stacked {resolution.status}: {resolution.reason} "
                     "— visibility queue only; no reviewer run authorized")
        observer.notify(loop, st, "stall", number, head or "", identity=f"stacked:{generation}",
                        outcome=f"stacked {resolution.status}: {resolution.reason}", next_turn="you")
    watch["stacked_wait"] = {k: v for k, v in pending.items() if k in listed}

def retry_fresh_reviews(loop: dict, st: state_mod.LoopState, prs: list, lines: list[str],
                        since: float = 0.0) -> None:
    """Re-drive a transition's fresh reviewer turn that is not durably enqueued yet.

    Covers a failed enqueue (missing runtime, ledger or spawn failure) and a child that was
    still a draft when retargeted. Each attempt re-reads the live PR; the turn key keeps a
    success idempotent, and a failure stays visible here until one succeeds.
    """
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("draft"):
            continue
        if (pr.get("base") or {}).get("ref") != loop["base"]:
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha")
        if type(number) is not int or not head:
            continue
        entry = transition.hold(st, number, head)
        if entry is None or transition.baseline_missing(entry):
            continue
        fresh = entry.get("fresh_review")
        if isinstance(fresh, dict) and (fresh.get("state") == "enqueued" or (
                isinstance(fresh.get("at"), (int, float)) and fresh["at"] >= since)):
            continue  # done, or already attempted (and reported) earlier in this sweep
        status, detail = transition.start_fresh_review(loop, st, number)
        if status == "retry":
            lines.append(f"⚠️ #{number} fresh review after retarget not enqueued: {detail} "
                         "— retrying next sweep")
        elif status == "enqueued":
            lines.append(f"#{number} fresh review after retarget enqueued ({detail})")


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
        # record() erased the pre-retarget marker; a marker at a held head can only come from
        # receipted post-boundary verdicts, and only those may re-verify its cap.
        reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
        if not isinstance(reviews, list):
            continue
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is not None and gh.review_state(latest) == "APPROVED":
            continue
        changes = gate.verdicts(reviews, loop)
        if len(changes) >= loop["cap"]:
            gate.breach(loop, st, number, head, marker.get("rounds", len(changes)),
                        marker.get("reason", "review cap reached"))


def sweep_loop(loop: dict, st: state_mod.LoopState, lines: list[str] | None = None) -> list[str]:
    # Filled in place, so the lines a failing sweep already produced (a gate-failure alert
    # among them) still reach the operator next to the error.
    lines = [] if lines is None else lines
    watch = st.watch()
    now = time.time()

    armed, armed_error = (True, "") if TEST else gate.hooks_read(loop)
    if armed is False:
        return lines                              # parked on purpose: say nothing, ever
    # Armed, or unknown because the hook list could not be read. Unknown is not paused: say so.
    if not TEST:
        lines.extend(github_health(loop, st, watch, now, armed, armed_error))

    # Self-heal first, and independent of GitHub listing: a route another registry writer erased
    # or rewrote (issue #1) is a loop that cannot wake a seat, whatever the PRs look like.
    try:
        healed = route_intent.heal(loop)
    except Exception as exc:                      # never let the heal hide the stall scan
        healed = [f"⚠️ Review loop [{loop['id']}] route self-heal failed: "
                  f"{type(exc).__name__}: {exc}"]
    if healed:
        lines.extend(healed)
        st.note("route self-heal: " + " | ".join(line.strip() for line in healed))

    if armed is None:
        # Blind: hooks unconfirmed, so nothing is drained or scanned (fail closed), but the
        # observer's retries need no GitHub read and the alert above already said why.
        if observer.retry(loop, st):
            log("observer: retried an undelivered notice")
        observer.flush(loop, st, wait_s=0 if TEST else observer.digest_wait(loop))
        watch["last_run"] = now_iso()
        st.watch_save(watch)
        st.note(f"run: blind — hook list unreadable ({armed_error or 'no reason given'})")
        if loop.get("state_dir"):                 # gate failures still alert; re-drives wait
            lines.extend(sweep_gate_failures(
                gate_failures.loop_ledger(loop), f"[{loop['id']}] {loop['repo']}",
                0.0 if TEST else float(loop.get("cooldown_h") or 6) * 3600, may_redrive=False))
        return lines

    prs = gh.open_prs(loop)
    # Re-drive only while GitHub answers: a re-run during an outage would only fail again.
    if loop.get("state_dir"):
        lines.extend(sweep_gate_failures(
            gate_failures.loop_ledger(loop), f"[{loop['id']}] {loop['repo']}",
            0.0 if TEST else float(loop.get("cooldown_h") or 6) * 3600,
            may_redrive=isinstance(prs, list)))
    if not isinstance(prs, list):
        # Without a complete listing, even individually readable PRs cannot establish
        # that the sweep's scheduling view is current. Explicit --drain still rechecks.
        lines.append(f"⚠️ {loop['id']}: could not list open PRs — stall scan and queue drain skipped this run")
        st.watch_save(watch)
        return lines

    reconcile_stacked(loop, st, watch, prs, lines)
    retry_fresh_reviews(loop, st, prs, lines, since=now)

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
    history = watch.get("head_history")
    watch["head_history"] = ({k: v for k, v in history.items()
                              if isinstance(v, dict) and
                              (clock := valid_clock(v.get("last_seen_at"), now)) is not None and
                              now - clock < HEAD_RETENTION_SEC}
                             if isinstance(history, dict) else {})
    current_heads: dict[str, dict] = {}
    for key, previous in heads.items():
        if not isinstance(previous, dict) or not previous.get("sha"):
            continue
        # Older state has no last_seen_at. Give it one bounded retention window
        # instead of erasing a live observation during the schema transition.
        last_seen = valid_clock(previous.get("last_seen_at"), now) or now
        if now - last_seen < HEAD_RETENTION_SEC:
            current_heads[key] = {"sha": previous["sha"],
                                  "base": previous.get("base"),
                                  "base_sha": previous.get("base_sha"),
                                  "observed_at": valid_clock(previous.get("observed_at"), now),
                                  "last_seen_at": last_seen}
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if author not in set(loop["fixers"]) or pr.get("draft"):
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha") or ""
        if not number or not head:
            continue
        key = str(number)
        previous = current_heads.get(key, {})
        base = (pr.get("base") or {}).get("ref")
        base_sha = (pr.get("base") or {}).get("sha")
        # For a direct-trunk PR, trunk moving on is not a new situation: only its own head or
        # base ref restarts the stall clock. A stacked base's generation does.
        if (previous.get("sha") == head and previous.get("base") in (None, base)
                and (base == loop["base"] or previous.get("base_sha") in (None, base_sha))):
            current_heads[key] = {**previous, "base": base, "base_sha": base_sha,
                                  "last_seen_at": now}
        else:
            if previous:
                history = watch.setdefault("head_history", {})
                history[f"{key}:{previous.get('sha')}:{previous.get('base', '')}:{previous.get('base_sha', '')}"] = previous
            # A first-seen old PR could be preexisting; created_at only establishes
            # eligibility for genuinely new PRs, never the time of a later push.
            new_pr = epoch(pr.get("created_at")) >= int(watch["armed_since"])
            current_heads[key] = {"sha": head, "base": base, "base_sha": base_sha, "observed_at":
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

        # A held head is judged only by host-receipted post-boundary reviews: an old verdict
        # is not a current stall, and a missing fresh verdict is the reviewer's to post.
        boundary = transition.hold(st, number, head)
        if boundary and transition.baseline_missing(boundary):
            continue  # permanently held; explain reports why, no seat can move it
        reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
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
        elif at_head and not config.unattended_fixer_push_enabled(loop):
            mins = age_min(at_head[-1].get("submitted_at"))
            if mins > grace:
                # Not a stall the fixer can end: no fixer turn starts until the loop opts in.
                kind = (f"{PUSH_OFF_KIND} — changes requested {mins / 60:.1f}h ago at head "
                        f"{head[:7]} waits for you: run "
                        f"`{config.fixer_push_enable_command(loop)}` (or fix it by hand)")
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
            if config.is_fixer_push_hold(entry):
                continue  # reported once per head as a stall above, not on every sweep
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
        # A push-off hold is one fact per head: its notice is keyed without the sweep clock,
        # so a cooldown re-raise in the sweep output never pings the observer twice.
        identity = ("fixer-push-off" if kind.startswith(PUSH_OFF_KIND)
                    else f"{kind[:40]}#{int(now)}")
        observer.notify(loop, st, "stall", number, head, identity=identity,
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
    budget = watchdog_budget()
    gh.begin_gate(time.monotonic() + budget, per_call=min(WATCHDOG_PER_CALL_S, budget))
    try:
        run(args, budget)
    except gh.GateBudgetExceeded as exc:          # e.g. the drain path's hook read
        print(budget_spent_line(f"[{args.loop or 'all loops'}]", budget, exc))
    finally:
        gh.end_gate()


def run(args: argparse.Namespace, budget: float) -> None:
    loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    if not loops and args.loop:
        print(f"no loop config named {args.loop}")
        return

    if args.drain:
        for loop in loops:
            st = state_mod.state_for(loop)
            armed, armed_error = (True, "") if TEST else gate.hooks_read(loop)
            if not armed:
                if args.loop:
                    print(f"{loop['id']}: hooks are paused — nothing drained" if armed is False
                          else f"{loop['id']}: hook list unreadable ({armed_error}) — nothing drained")
                continue
            try:
                fired = drain(loop, st, args.seat)
            except gh.GateBudgetExceeded as exc:
                print(budget_spent_line(f"[{loop['id']}]", budget, exc))
                return
            if not fired and not st.queue_items(args.seat) and args.loop:
                print(f"{loop['id']}: {args.seat} queue empty")
        return

    out: list[str] = []
    # The supervisor outbox is independent of GitHub listing availability or
    # paused hooks. It uses the existing cron stdout delivery path.
    from review_loop.run_supervisor import Supervisor
    ledger = config.home() / 'state' / 'review-loop-runs.sqlite'
    if ledger.exists():
        try:
            sup = Supervisor(ledger)
            sup.recover()  # no runtime configured here: never launch a worker
            sup.notify(lambda message: print(message, flush=True))
        except Exception as exc:
            out.append(f"⚠️ Review-loop operator notification sweep failed: {type(exc).__name__}: {exc}")
    if not args.loop:
        # Failures no loop could be named for (a malformed payload, a broken config).
        out.extend(sweep_gate_failures(gate_failures.fallback_ledger(), "(no loop)",
                                       0.0 if TEST else 6 * 3600, may_redrive=True))
    for loop in loops:
        lines: list[str] = []
        try:
            out.extend(sweep_loop(loop, state_mod.state_for(loop), lines))
        except gh.GateBudgetExceeded as exc:      # GitHub is hanging: every loop would too
            out.extend(lines)
            out.append(budget_spent_line(f"[{loop.get('id', '?')}]", budget, exc))
            break
        except Exception as exc:                  # one bad loop must not hide the others
            out.extend(lines)
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
