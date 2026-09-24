"""The gate runtime: the parts both gates share.

A gate is a script between a GitHub event and an agent run. It answers one question —
*should this event start a run, and under what terms?* — and it must answer it the same
way twice, out loud or not at all. Every branch here ends in either ``silence()`` (no run,
no tokens, nothing announced) or a payload annotated with a ``_loop`` block the route
prompt can render.

The order of the guards matters and is the same in both gates:

1. is this event even ours (repo, action, who did it);
2. can we read the facts we need (never guess a round count from a failed API call);
3. has this exact head already been handled (in-flight marks);
4. is the budget spent (cap → hand the PR to adjudication, do not buy another round);
5. does the other seat already own this PR (one PR runs one seat at a time — queue, don't start);
6. is there a free slot for this seat (otherwise queue it and stay quiet);
7. can this run be isolated (above ``concurrency 1`` a shared clone is not an option);
8. only then: record, prepare the workspace, announce, fire.

A gate must free the *other* seat's turn **before** it claims its own (the fixer's request is what
ends the fixer's turn, the reviewer's verdict is what ends the reviewer's) — claim first and the
gates deadlock against each other, each waiting for the other's hold to clear.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.request

from . import config, gh, isolation, routes, state as state_mod
from .util import iso_at, log, now_iso, silence


def payload_loop(payload: dict) -> dict:
    full = ((payload.get("repository") or {}).get("full_name") or "")
    loop = config.by_repo(full)
    if not loop:
        silence(f"no loop configured for {full or 'an unknown repository'}")
    return loop


def context(payload: dict):
    loop = payload_loop(payload)
    return loop, state_mod.state_for(loop)


def pr_of(payload: dict) -> dict:
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        silence("no pull_request in payload")
    return pr


def number_of(payload: dict, pr: dict) -> int:
    number = pr.get("number") or payload.get("number")
    if not number:
        silence("payload has no PR number")
    return int(number)


def head_of(pr: dict) -> str:
    head = (pr.get("head") or {}).get("sha") or ""
    if not head:
        silence("payload has no head sha")
    return head


def seat_key(loop: dict, number: int) -> str:
    return f"{loop['repo']}#{number}"


def pr_url(loop: dict, number: int) -> str:
    return f"https://github.com/{loop['repo']}/pull/{number}"


def artifacts_for(loop: dict, number: int) -> str:
    return str(config.artifacts_dir(loop, number))


def isolation_block(loop: dict, number: int, workspace: dict | None, seat: str) -> dict:
    """Where this run is allowed to work — always present, so no prompt key renders as text.

    The gate decided the workspace; the prompt only repeats the decision. ``isolated: false``
    means no sandbox could be built, which is only allowed to happen at ``concurrency = 1``.
    """
    p = isolation.paths(loop, number, seat)
    shared = str(config.clone_path(loop)) if loop.get("clone") else ""
    if workspace:
        env = " ".join(f"{k}={v}" for k, v in workspace["env"].items()
                       if k in ("CARGO_TARGET_DIR", "TMPDIR"))
        return {"isolated": True, "root": workspace["root"], "clone": workspace["clone"],
                "target": workspace["target"], "tmp": workspace["tmp"], "env": env,
                "shared": shared, "seat": seat,
                "brief": isolation.describe(workspace, loop, number, seat)}
    return {"isolated": False, "root": str(p["root"]), "clone": shared or str(p["root"]),
            "target": "", "tmp": "", "env": "", "shared": shared, "seat": seat,
            "brief": isolation.describe(None, loop, number, seat)}


def loop_block(loop: dict, number: int, head: str, workspace: dict | None = None,
               seat: str = "reviewer", **extra) -> dict:
    block = {"pr": number, "repo": loop["repo"], "head": head, "cap": loop["cap"],
             "url": pr_url(loop, number), "artifacts": artifacts_for(loop, number),
             "concurrency": config.seat_concurrency(loop, seat),
             "isolation": isolation_block(loop, number, workspace, seat)}
    block.update(extra)
    return block


# -- reading GitHub -----------------------------------------------------------


def fetch_reviews(loop: dict, number: int):
    """The verdict list, or silence. A loop that cannot count its rounds must not guess one."""
    reviews = gh.reviews(loop, number)
    if not isinstance(reviews, list):
        silence("could not read a valid review list — not guessing the round count")
    return reviews


def is_reviewer(review: dict, loop: dict) -> bool:
    login = ((review.get("user") or {}).get("login") or "").lower()
    return login in set(loop["reviewers"])


def reviewer_login(review: dict) -> str:
    return ((review.get("user") or {}).get("login") or "").lower()


def verdicts(reviews: list, loop: dict, exclude_id=None) -> list:
    return [r for r in reviews
            if isinstance(r, dict)
            and is_reviewer(r, loop)
            and gh.review_state(r) == "CHANGES_REQUESTED"
            and (exclude_id is None or r.get("id") != exclude_id)]


def reviewed_at_head(reviews: list, loop: dict, head: str) -> bool:
    """A submitted verdict at this head, not a comment, draft, or dismissed review."""
    return any(gh.review_state(r) in {"APPROVED", "CHANGES_REQUESTED"}
               for r in reviews_at_head(reviews, loop, head))


def reviews_at_head(reviews: list, loop: dict, head: str) -> list:
    """Reviewer reviews at this head, including non-verdicts for diagnostics only."""
    return [r for r in reviews
            if isinstance(r, dict) and is_reviewer(r, loop) and r.get("commit_id") == head]


def approved_at_head(reviews: list, loop: dict, head: str) -> bool:
    return any(is_reviewer(r, loop) and gh.review_state(r) == "APPROVED"
               and r.get("commit_id") == head
               for r in reviews if isinstance(r, dict))


def changes_at_head(reviews: list, loop: dict, head: str) -> list:
    return [r for r in reviews
            if is_reviewer(r, loop) and gh.review_state(r) == "CHANGES_REQUESTED"
            and r.get("commit_id") == head]


def hooks_read(loop: dict) -> tuple[bool | None, str]:
    """``(armed, error)``. ``None`` means the hook list could not be read at all.

    Both seat routes must exist as active repo hooks, or the loop is parked. An unreadable list is
    **not** "paused": a token without ``admin:repo_hook`` cannot see hooks that may well be active.
    The watchdog reads that as silence, and ``explain`` labels it unknown rather than guessing in
    the other direction — "the loop is paused" is a claim, and it needs the hooks to prove it.
    """
    hooks, error = gh.fetch(loop, gh.hooks_path(loop))
    if error or not isinstance(hooks, list):
        return None, error or "GitHub returned no hook list"
    missing = [seat for seat in ("reviewer", "fixer")
               if not any(isinstance(h, dict)
                          and loop["seats"][seat]["route"] in (h.get("config") or {}).get("url", "")
                          and h.get("active") is True for h in hooks)]
    return not missing, ", ".join(missing)


def hooks_armed(loop: dict) -> bool:
    """Both seat routes present and active. A failed read answers False on purpose: the watchdog
    must stay quiet rather than alert on a loop it cannot confirm is armed."""
    armed, _ = hooks_read(loop)
    return bool(armed)


# -- explain --------------------------------------------------------------------
#
# "Why is this PR not moving?" is the question an operator actually has at 2am, and no single file
# answers it: half the answer is in GitHub (the head, the verdicts *at that head*, whether an
# approval exists) and half is on disk (who holds the PR, what is queued, what is marked in
# flight, what the watchdog last saw).
#
# So ``explain`` gathers both halves read-only and runs them through the *same* predicates the
# gates run — ``verdicts``, verdict-only ``reviewed_at_head``, ``changes_at_head``, ``approved_at_head``,
# ``seat_key``, the seat ledgers, the queue, the breach marker, ``hooks_read`` — in the gates' own
# guard order. It re-derives no rule, so it cannot drift from what the loop would do, and it writes
# nothing: no claim, no queue entry, no drain, no webhook POST, no token. Two runs leave GitHub and
# the state directory byte-for-byte as they were.
#
# The difference from a gate is what happens at a guard. A gate stops there — *silence* — and the
# operator only sees that nothing happened. This walks every guard, reports all of them, and then
# names the one event that would move the PR.

# The vocabulary of ``next.kind``. The suite asserts every conclusion is one of these, so a new
# branch cannot quietly invent a kind nobody is checking for.
EXPLAIN_KINDS = ("review-verdict", "review-request", "fixer-retry", "fixer-push", "release", "adjudication",
                 "rearm", "ready", "retry", "none")


def _mark_time(entry: dict | None, fallback: float) -> float:
    """A state mark's ``at`` epoch, or the fallback when the mark is malformed or has none."""
    try:
        return float((entry or {}).get("at") or fallback)
    except (TypeError, ValueError):
        return fallback


def _minutes_since(when: float, now: float) -> int:
    return max(0, int((now - when) / 60)) if when else 0


def _adjudication_next(loop: dict, head: str) -> str:
    """The one thing left when the budget is spent: a ruling, or a human if nobody can rule."""
    short = head[:7] if head else "?"
    if not (loop.get("adjudicator") or {}).get("route"):
        return ("human adjudication: this loop has no adjudicator route, so the breach marker is "
                "the only record — rule by hand, then merge or close")
    return (f"human adjudication: the adjudicator rules at head {short} and reports; nothing else "
            f"fires for this PR (both gates stop at the cap)")

def seat_capacity(loop: dict, st: state_mod.LoopState, seat: str) -> tuple[int, int]:
    """The gate's capacity predicate, with a read-only ledger view for explain."""
    return len(st.live_locks(seat)), config.seat_concurrency(loop, seat)

def breach_delivery_status(marker: dict, head: str) -> str:
    """Only a marker for the live head can park or retry this PR."""
    return str(marker.get("status") or "") if marker.get("head") == head else ""


def _explain_state(loop: dict, st: state_mod.LoopState, key: str, number: int, head: str,
                   now: float) -> dict:
    """What the loop's own files say about this PR, read without persisting anything.

    Everything here is either a claim on the PR (``held``), a place in line (``queued_seat``), a
    head-level mark, or a loop-level fact; the report shows all of them, because "the seat is free
    and nothing is queued" is as much an answer as a queue entry is.
    """
    held: dict[str, dict] = {}
    capacity: dict[str, tuple[int, int]] = {}
    for seat in ("reviewer", "fixer"):
        live = st.live_locks(seat)
        capacity[seat] = seat_capacity(loop, st, seat)
        entry = live.get(key)
        if isinstance(entry, dict):
            held[seat] = entry
    seat_line = " · ".join(
        f"{seat} holds it ({_minutes_since(_mark_time(entry, 0.0), now)}m of ttl "
        f"{loop['ttl_min']}m, since {iso_at(_mark_time(entry, 0.0)) or 'an unrecorded time'})"
        for seat, entry in sorted(held.items())) or "nobody holds it"

    queue_bits: list[str] = []
    queued_seat = ""
    queued_reason = ""
    stale_queues: list[str] = []
    for seat in ("reviewer", "fixer"):
        items = st.queue_items(seat)
        entry = items.get(key)
        if not isinstance(entry, dict):
            continue
        if head and entry.get("head") != head:
            stale_queues.append(f"{seat} queue targets head {str(entry.get('head') or '?')[:7]}, "
                                f"not current head {head[:7]} — the watchdog drops it, not retargets it")
            queue_bits.append(stale_queues[-1])
            continue
        order = sorted(items, key=lambda name: _mark_time(items.get(name), 0.0))
        position = order.index(key) + 1 if key in order else 0
        queued_reason = str(entry.get("reason") or "no reason recorded")
        queue_bits.append(f"{seat} {position} of {len(order)} "
                          f"(waiting {_minutes_since(_mark_time(entry, 0.0), now)}m) — "
                          f"{queued_reason}")
        queued_seat = seat
    queue_line = " · ".join(queue_bits) or "not queued"

    marks: list[str] = []
    inflight_review = inflight_fix = False
    if head:
        for label in ("review", "fix"):
            mark = f"{label}:{number}:{head}"
            if not st.inflight(mark):
                continue
            marks.append(f"{label} for head {head[:7]} armed "
                         f"{_minutes_since(st.inflight_at(mark), now)}m ago "
                         f"(ttl {loop['inflight_ttl_min']}m)")
            if label == "review":
                inflight_review = True
            else:
                inflight_fix = True
    inflight_line = " · ".join(marks) or "none"

    raw_marker = st.breach_get(number)
    marker = raw_marker if isinstance(raw_marker, dict) else {}
    delivery_status = breach_delivery_status(marker, head)
    parked = delivery_status in {"awaiting-adjudication", "adjudicating"}
    escalation_line = (
        f"{marker.get('status') or 'recorded'} at head {str(marker.get('head') or '?')[:7]} since "
        f"{marker.get('at') or 'an unrecorded time'} — {marker.get('reason') or 'no reason recorded'}"
        if marker else "none")

    watch = st.watch()
    if watch.get("last_run"):
        armed_since = iso_at(float(watch.get("armed_since") or 0)) or "unrecorded"
        sweep = f"watchdog last swept {watch['last_run']} (armed since {armed_since})"
    else:
        sweep = "no watchdog sweep recorded — nothing has read this loop's PRs yet"

    return {"seat": seat_line, "queue": queue_line, "inflight": inflight_line,
            "escalation": escalation_line, "held": held, "queued_seat": queued_seat,
            "queued_reason": queued_reason, "stale_queues": stale_queues,
            "inflight_review": inflight_review,
            "inflight_fix": inflight_fix, "parked": parked, "delivery_status": delivery_status,
            "capacity": capacity, "marker": marker, "sweep": sweep}


def _explain_hooks(armed, armed_error: str) -> str:
    """The armed/paused line. Unreadable is its own answer: "paused" is a claim, not a default."""
    if armed is True:
        return "armed — both seat routes are active repo hooks"
    if armed is False:
        return ("PAUSED — seat route(s) without an active repo hook: "
                f"{armed_error or 'unknown'}; those seats cannot receive events")
    return (f"unknown — the repo's hooks could not be read "
            f"({armed_error or 'no reason given'}); 'paused' is not claimed")


def explain_facts(loop: dict, number: int) -> dict:
    """Everything ``explain`` reasons about, read once, with the reason any read failed.

    Read-only by construction: three GETs, and a state directory that is only ever opened for
    reading (``live_locks``, ``inflight_at`` and ``queue_items`` never persist their pruning).
    """
    pr, pr_error = gh.fetch(loop, gh.pr_path(loop, number))
    if pr is None and (pr_error == "HTTP 404" or pr_error.startswith("HTTP 404 ")):
        pr_error = ""  # GitHub hides inaccessible resources behind 404 as well.
    reviews, reviews_error = gh.fetch(loop, gh.reviews_path(loop, number))
    armed, armed_error = hooks_read(loop)
    return {"pr": pr, "pr_error": pr_error, "reviews": reviews, "reviews_error": reviews_error,
            "armed": armed, "armed_error": armed_error, "read_at": time.time()}


def explain(loop: dict, st: state_mod.LoopState, number: int, facts: dict) -> dict:
    """Why one PR is not moving, and the single event that would move it. Reads nothing itself.

    ``facts`` comes from ``explain_facts`` (or a test). Every guard the live gates run is evaluated
    here in the same order; the first one that names an action decides ``next``, and all of them
    are reported in ``blockers``. Unknowns stay unknown: an unreadable review list is never
    rendered as "0 verdicts".
    """
    now = float(facts.get("read_at") or time.time())
    pr = facts.get("pr") if isinstance(facts.get("pr"), dict) else None
    reviews = facts.get("reviews") if isinstance(facts.get("reviews"), list) else None
    armed = facts.get("armed")
    pr_error = str(facts.get("pr_error") or "")
    reviews_error = str(facts.get("reviews_error") or "")
    armed_error = str(facts.get("armed_error") or "")

    key = seat_key(loop, number)
    cap = loop["cap"]
    repo = loop["repo"]
    head_data = (pr or {}).get("head")
    raw_head = head_data.get("sha") if isinstance(head_data, dict) else None
    head = raw_head if isinstance(raw_head, str) else ""
    short = head[:7] if head else "?"
    base = str(((pr or {}).get("base") or {}).get("ref") or "")
    author = str(((pr or {}).get("user") or {}).get("login") or "").lower()
    state = ("unknown" if pr is None
             else "merged" if (pr.get("merged") or pr.get("merged_at"))
             else str(pr.get("state") or "unknown"))
    requested = {str((entry or {}).get("login") or "").lower()
                 for entry in (pr or {}).get("requested_reviewers") or []
                 if isinstance(entry, dict)}
    request_pending = bool(loop["reviewer_seat"]) and loop["reviewer_seat"] in requested
    blockers: list[str] = []

    # -- what the gates conclude about this head (their predicates, not new ones) -------------
    spent: int | None = None
    at_head: int | None = None
    head_states: list[str] = []
    approved = False
    reviewed = False
    changes: list[dict] = []
    if reviews is not None:
        spent = len(verdicts(reviews, loop))
        if head:
            changes = changes_at_head(reviews, loop, head)
            at_head = len(changes)
            approved = approved_at_head(reviews, loop, head)
            reviewed = reviewed_at_head(reviews, loop, head)
            head_states = sorted({gh.review_state(r) for r in reviews_at_head(reviews, loop, head)})

    # -- the budget line ---------------------------------------------------------------------
    if reviews is None:
        budget = (f"unknown — the review list could not be read "
                  f"({reviews_error or 'no reason given'}); never guessed from a failed read")
    elif at_head:
        last = [r for r in changes if isinstance(r, dict)][-1]
        budget = (f"{spent}/{cap} verdicts spent · {at_head} at head {short} — changes requested "
                  f"{last.get('submitted_at') or 'at an unrecorded time'} by "
                  f"{reviewer_login(last) or 'an unrecorded reviewer'}")
    elif approved:
        budget = f"{spent}/{cap} verdicts spent · approved at head {short}"
    elif head_states:
        budget = (f"{spent}/{cap} verdicts spent · a non-verdict review at head {short} "
                  f"({'/'.join(head_states)}) — reviewer gate still accepts a fresh request")
    elif head:
        budget = f"{spent}/{cap} verdicts spent · nothing at head {short}"
    else:
        budget = (f"{spent if spent is not None else 'unknown'}/{cap} verdicts spent · "
                  f"head unknown")

    # -- local state: who holds it, what waits, what is marked -------------------------------
    local = _explain_state(loop, st, key, number, head, now)
    held = local["held"]
    queued_seat = local["queued_seat"]
    queued_reason = local["queued_reason"]
    inflight_review = local["inflight_review"]
    inflight_fix = local["inflight_fix"]
    marker = local["marker"]
    # A marker records a past escalation, not a permanent veto. A dismissed
    # verdict can lower the live count, and the reviewer gate then starts a new
    # review even when the marker still says awaiting-adjudication/adjudicating.
    parked = local["parked"] and spent is not None and spent >= cap
    # A dismissed verdict can lower the live count after a failed POST. The
    # watchdog only retries a pending marker while the cap remains spent.
    pending_delivery = (local["delivery_status"] == "delivery-pending"
                        and spent is not None and spent >= cap)
    stale_held = {seat for seat, entry in held.items() if entry.get("head") != head}
    needed_seat = ("fixer" if at_head else "reviewer" if request_pending else "")
    used, limit = local["capacity"].get(needed_seat, (0, 0))
    full_seat = bool(needed_seat and used >= limit and needed_seat not in held
                     and not (inflight_fix if needed_seat == "fixer" else inflight_review))
    hooks_line = _explain_hooks(armed, armed_error)

    # -- every guard, in the gates' order, reported instead of silencing ----------------------
    if pr is None:
        if pr_error:
            blockers.append(f"stale/failing GitHub read: {pr_error} — the PR's state is unknown, "
                            f"not closed, and its head cannot be read")
        else:
            blockers.append(f"GitHub returned no PR #{number} in {repo} — the number is wrong, or "
                            f"the read token cannot see the repository")
    else:
        if state in ("closed", "merged"):
            blockers.append(f"the PR is {state} — the loop is over here; the closed path reclaims "
                            f"its disk and drops any queue entry for it")
        else:
            if armed is False:
                blockers.append(f"paused loop: seat route(s) without an active repo hook: "
                                f"{armed_error or 'unknown'}")
            elif armed is None:
                blockers.append(f"hook state unreadable ({armed_error or 'no reason given'}) — "
                                f"'paused' is not claimed: not seeing the hooks is not evidence they "
                                f"are off")
            if reviews is None:
                blockers.append(f"the review list could not be read "
                                f"({reviews_error or 'no reason given'}) — the verdict count is "
                                f"unknown, never guessed")
            if not head:
                blockers.append("PR head missing or malformed — no gate can authorize a run")
            if pr.get("draft"):
                blockers.append("draft PR: the reviewer gate stays silent until ready_for_review")
            if base and base != loop["base"]:
                blockers.append(f"wrong base: this loop watches {loop['base']}, the PR targets {base}")
            if author and author not in set(loop["fixers"]):
                blockers.append(f"the author {author} is not one of this loop's fixers "
                                f"({', '.join(loop['fixers'])})")
            if parked:
                blockers.append(f"escalated: the PR is parked awaiting adjudication at head "
                                f"{str(marker.get('head') or '?')[:7]} since "
                                f"{marker.get('at') or 'an unrecorded time'}")
            elif pending_delivery:
                blockers.append(f"adjudicator delivery pending at head {short} — no ruling is "
                                "promised until the route acknowledges delivery")
            elif spent is not None and spent >= cap:
                blockers.append(f"{spent}/{cap} verdicts spent with no approval and no escalation "
                                f"marker — the cap may not have fired (the watchdog reports this "
                                f"shape too)")
            if queued_seat:
                blockers.append(f"no capacity: queued with the {queued_seat} seat — {queued_reason}")
            elif full_seat:
                blockers.append(f"no capacity: {needed_seat} seat at capacity {used}/{limit} "
                                "on other PRs — this PR has no queue entry yet")
            blockers.extend(local["stale_queues"])
            for seat in sorted(stale_held):
                blockers.append(f"{seat} lock targets an older head, not current head {short} "
                                "— it cannot authorize a verdict or push at this head")
            if head_states and not reviewed:
                blockers.append(f"non-verdict review at head {short} ({'/'.join(head_states)}) "
                                "does not suppress a fresh reviewer request")
            if at_head and "fixer" not in held and not inflight_fix and not queued_seat:
                blockers.append(f"the changes-requested verdict at head {short} has no fix run out "
                                f"— the fixer gate did not start one for that delivery")
            if request_pending and not held and not inflight_review and not at_head and not approved:
                blockers.append(f"a review request for {loop['reviewer_seat']} is pending at head "
                                f"{short}, but no review run is out — the reviewer gate did not "
                                f"start one")
            if (spent and head and not reviewed and not approved and not request_pending and not held
                    and not inflight_review and not queued_seat):
                blockers.append(f"no review request exists for head {short} — GitHub clears a request "
                                f"when a verdict lands, so the reviewer gate stays silent until the "
                                f"fixer asks again")

    # -- the single next event ---------------------------------------------------------------
    if pr is None:
        if pr_error:
            kind = "retry"
            action = (f"retry the GitHub read for {repo}#{number} ({pr_error}) — nothing local can "
                      f"stand in for the head and the verdicts")
        else:
            kind = "none"
            action = (f"nothing to drive — check that #{number} exists, and that the loop's read "
                      f"token can see {repo}")
    elif state in ("closed", "merged"):
        kind = "none"
        action = (f"nothing — the PR is {state}; the loop is over for it, and its next event is "
                  f"gate_reviewer's closed path reclaiming the disk")
    elif armed is False:
        kind = "rearm"
        action = (f"re-arm the loop — hermes review-loop arm --loop {loop['id']} — no event can "
                  f"reach both seats while a repo hook is missing or inactive")
    elif not head:
        kind = "retry"
        action = (f"retry the PR read for {repo}#{number} — its head is missing or malformed; "
                  "no review can be requested without a known head")
    elif pr.get("draft"):
        kind = "ready"
        action = ("mark the PR ready for review (the ready_for_review event) — the reviewer gate "
                  "ignores drafts")
    elif base and base != loop["base"]:
        kind = "none"
        action = (f"nothing — this loop only serves PRs based on {loop['base']}; retarget the PR, "
                  f"or add a loop for {base}")
    elif author and author not in set(loop["fixers"]):
        kind = "none"
        action = (f"nothing — the reviewer gate only serves PRs opened by this loop's fixers "
                  f"({', '.join(loop['fixers'])})")
    elif reviews is None:
        kind = "retry"
        action = (f"retry the review list for {repo}#{number} "
                  f"({reviews_error or 'unreadable'}) — whether head {short} already has a verdict "
                  f"is unknown, and the loop never guesses that")
    elif approved:
        kind = "none"
        action = f"nothing — head {short} is approved; a human merges it"
    elif pending_delivery and not (loop.get("adjudicator") or {}).get("route"):
        kind = "adjudication"
        action = _adjudication_next(loop, head)
    elif pending_delivery:
        kind = "retry"
        action = (f"retry adjudicator delivery for head {short} on the next armed watchdog sweep "
                  "after checking the current head and review cap — no ruling is underway yet")
    elif parked:
        kind = "adjudication"
        action = _adjudication_next(loop, head)
    elif spent is not None and spent >= cap:
        if not (loop.get("adjudicator") or {}).get("route"):
            kind = "adjudication"
            action = _adjudication_next(loop, head)
        else:
            kind = "retry"
            action = (f"re-deliver the {'changes-requested review' if at_head else 'review request'} "
                      f"event for head {short} to the {'fixer' if at_head else 'reviewer'} gate "
                      "to create and deliver the missing breach marker — no ruling is underway yet")
    elif stale_held == {"reviewer"} and at_head:
        kind = "fixer-retry"
        action = (f"re-deliver the changes-requested review event for head {short} to the fixer gate "
                  "— that verdict hands off the PR and releases the stale reviewer lock")
    elif stale_held == {"fixer"} and request_pending:
        kind = "retry"
        action = (f"re-deliver the review_requested event for head {short} to the reviewer gate "
                  "— that request hands off the PR and releases the stale fixer lock")
    elif stale_held:
        kind = "release"
        action = (f"release or wait for the stale {', '.join(sorted(stale_held))} lock to expire "
                  f"before re-driving the current head {short}; an old-head run cannot finish it")
    elif "reviewer" in held and not at_head:
        kind = "review-verdict"
        action = (f"the reviewer's verdict at head {short} "
                  f"(round {(spent or 0) + 1} of {cap}) — the reviewer holds the PR right now")
    elif "fixer" in held:
        kind = "fixer-push"
        action = (f"the fixer pushes a fix and re-requests review of head {short} — asking is what "
                  f"hands the PR back, and it is what frees the fixer's slot")
    elif queued_seat:
        kind = "release"
        action = (f"a {queued_seat} slot frees — the queued run starts then (a verdict or a handoff "
                  f"ends the run holding it; the lock expiry at {loop['ttl_min']}m is the backstop)")
    elif full_seat:
        kind = "retry"
        action = (f"re-deliver the {'changes-requested review' if at_head else 'review_requested'} "
                  f"event for head {short} now — the {needed_seat} gate will queue it at "
                  f"capacity {used}/{limit}; a {needed_seat} slot on another PR freeing then "
                  "starts the queued run")
    elif inflight_review:
        kind = "review-verdict"
        action = (f"the reviewer's verdict at head {short} (round {(spent or 0) + 1} of {cap}) — a "
                  f"review for this head is already marked in flight")
    elif inflight_fix:
        kind = "fixer-push"
        action = (f"the fixer pushes a fix and re-requests review of head {short} — a fix run for "
                  f"this head is already marked in flight")
    elif at_head:
        kind = "fixer-retry"
        action = (f"re-deliver the changes-requested review event for head {short} to the fixer gate "
                  "after checking why its run did not start — no fixer is running to push a fix")
    elif request_pending:
        kind = "retry"
        action = (f"re-deliver the review_requested event for head {short} to the reviewer gate "
                  f"after checking why the pending request for {loop['reviewer_seat']} did not "
                  "start a run — no verdict can arrive without one")
    else:
        kind = "review-request"
        detail = ("a fresh PR also wakes the reviewer on opened / ready_for_review, so re-driving "
                  "that event is the other way in" if not spent else "a push alone wakes nobody")
        action = (f"the fixer asks for review of head {short}: gh api -X POST "
                  f"repos/{repo}/pulls/{number}/requested_reviewers -f "
                  f"'reviewers[]={loop['reviewer_seat']}' — {detail}")

    if pr is None:
        state_line = "unknown — the PR itself could not be read"
    else:
        bits = [state, f"base {base or 'unknown'}", f"author {author or 'unknown'}"]
        bits.append(f"head {short}" if head else "head unread")
        if pr.get("draft"):
            bits.append("draft")
        if request_pending:
            bits.append(f"review requested from {loop['reviewer_seat']}")
        state_line = " · ".join(bits)

    return {
        "pr": number, "repo": repo, "url": pr_url(loop, number), "state": state,
        "head": head, "cap": cap, "spent": spent, "at_head": at_head,
        "approved": approved, "reviewed": reviewed, "request_pending": request_pending,
        "read_at": iso_at(now),
        "state_line": state_line, "budget": budget, "seat": local["seat"], "queue": local["queue"],
        "inflight": local["inflight"], "escalation": local["escalation"], "hooks": hooks_line,
        "sweep": local["sweep"], "blockers": blockers, "next": {"kind": kind, "action": action},
    }


# -- side effects -------------------------------------------------------------


def drain_seat(loop: dict, seat: str) -> None:
    """Start whatever queued for a seat now that its turn is over. Best effort, never fatal."""
    try:
        subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parents[1]
                                            / "scripts" / "watchdog.py"),
                        "--loop", loop["id"], "--drain", "--seat", seat],
                       capture_output=True, text=True, timeout=180)
    except Exception as exc:
        log(f"drain {seat} failed: {exc}")


def reclaim(loop: dict, number: int, state: str) -> None:
    """A finished PR gives back its disk: worktrees, build dirs, locks, counters."""
    log(f"PR #{number} {state} — reclaiming local review artifacts")
    try:
        proc = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parents[1]
                                                   / "scripts" / "cleanup.py"),
                               "--loop", loop["id"], "--pr", str(number), "--quiet"],
                              capture_output=True, text=True, timeout=1800)
        for line in (proc.stdout or "").strip().splitlines()[-6:]:
            log(f"cleanup: {line}")
        if proc.returncode != 0:
            log(f"cleanup rc={proc.returncode}: {(proc.stderr or '')[:200]}")
    except Exception as exc:
        log(f"cleanup failed for #{number}: {exc}")


def wake_adjudicator(loop: dict, number: int, head: str, rounds: int, reason: str) -> bool:
    """Hand a stalled PR to the adjudicator seat — a route bound to its own profile.

    The rule the adjudicator is given: read the two positions, rule, and do not merge. The
    pending marker written by the caller stays retryable if this POST fails.
    """
    target = routes.target(loop["adjudicator"]["route"], loop.get("host"))
    if not target:
        log("no adjudicator route — the breach marker is the only record")
        return False
    payload = {"repository": {"full_name": loop["repo"]},
               "action": "review_loop_breach", "number": number,
               "_loop": {**loop_block(loop, number, head, round=rounds, reason=reason),
                         "role": "adjudicator"}}
    delivered = routes.fire(loop["adjudicator"]["route"], "pull_request", payload,
                            f"breach-{number}", loop.get("host"))
    if delivered:
        log(f"adjudicator woken for #{number}")
    return delivered


def breach(loop: dict, st: state_mod.LoopState, number: int, head: str, rounds: int,
           reason: str) -> None:
    entry = {
        "pr": number, "head": head, "rounds": rounds, "cap": loop["cap"],
        "reason": reason, "at": now_iso(), "status": "awaiting-adjudication",
    }

    def current_head() -> bool:
        # Both gate payloads can arrive late. Recheck GitHub inside the breach
        # lock so a delayed A cannot overwrite an already parked B.
        current = gh.pr(loop, number)
        return (isinstance(current, dict) and current.get("number") == number
                and current.get("state") == "open" and not current.get("draft")
                and (current.get("base") or {}).get("ref") == loop["base"]
                and ((current.get("user") or {}).get("login") or "").lower() in loop["fixers"]
                and (current.get("head") or {}).get("sha") == head)

    route = loop.get("adjudicator", {}).get("route")
    outcome = st.breach_deliver(number, entry, current_head,
                                lambda marker: wake_adjudicator(loop, number, head,
                                                                marker["rounds"], marker["reason"])
                                if route else True)
    if outcome == "new":
        st.note(f"breach {loop['repo']}#{number} at {head[:7]}: {reason}")
        if not route:
            log("no adjudicator configured — marker written, nothing woken")
    elif outcome == "stale":
        log(f"#{number} @ {head[:7]} no longer current — not escalating")


def ping_start(loop: dict, seat: str, text: str) -> None:
    """Announce the run in the seat's own Discord channel before the agent spawns.

    Reads that profile's ``.env`` (the gateway already has it) so no credentials are
    duplicated in the loop config, and a failed ping never blocks a review.
    """
    seat_cfg = loop["seats"][seat]
    profile = seat_cfg["profile"]
    try:
        env: dict[str, str] = {}
        env_path = config.home() / "profiles" / profile / ".env"
        for line in env_path.read_text().splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
        token = env.get("DISCORD_BOT_TOKEN", "")
        channel = seat_cfg.get("channel") or env.get("DISCORD_HOME_CHANNEL", "")
        if not token or not channel:
            raise RuntimeError(f"no Discord token/channel for profile {profile!r}")
        body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel}/messages", data=body,
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                     "User-Agent": "hermes-review-loop"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"Discord HTTP {resp.status}")
    except Exception as exc:
        log(f"start-ping failed: {exc}")


def start_text(loop: dict, seat: str, number: int, head: str, round_no: int,
               note: str = "") -> str:
    seat_cfg = loop["seats"][seat]
    emoji = seat_cfg.get("emoji") or ("🔍" if seat == "reviewer" else "🔧")
    verb = "starting review of" if seat == "reviewer" else "starting fixes on"
    extra = f" {note}" if note else ""
    return (f"{emoji} **{seat_cfg['agent']}** — {verb} PR #{number} "
            f"(round {round_no}/{loop['cap']}, head `{head[:7]}`){extra}\n"
            f"{pr_url(loop, number)}")


def take_seat(loop: dict, st: state_mod.LoopState, seat: str, number: int, head: str,
              why: str, login: str = "") -> dict | None:
    """Claim a slot for this PR and build its isolated workspace.

    Either the run starts — slot available, own clone ready — or this PR is queued and the gate
    stays silent. The claim happens *before* the workspace is built (a clone can take minutes;
    two events arriving in that window must not both decide the seat is free).

    Returns the workspace, or ``None`` when no sandbox could be built. Above ``concurrency = 1``
    an unisolated run is never started: two runs sharing a checkout produce wrong verdicts, and
    the caller would rather have a queued PR than a wrong one.
    """
    key = seat_key(loop, number)
    capacity = config.seat_concurrency(loop, seat)

    if st.is_active(seat, key):
        log(f"{seat} is already running {key} — refusing a second run at the same PR")
        silence()

    other = st.held_by_other(seat, key)
    if other:
        st.queue_add(seat, key, head, pr_url(loop, number),
                     f"the {other} seat is working this PR")
        log(f"{other} holds #{number} — queued {seat} rather than running both on one PR")
        silence(f"the {other} seat is working this PR — queued until it hands off")

    live = st.active(seat)
    used, limit = seat_capacity(loop, st, seat)
    if used >= limit:
        held = ", ".join(f"{k} ({int(time.time() - v.get('at', time.time()))}s)"
                         for k, v in sorted(live.items()))
        st.queue_add(seat, key, head, pr_url(loop, number),
                     f"{seat} at capacity {len(live)}/{capacity}: {held}")
        log(f"{seat} at capacity {len(live)}/{capacity} ({held}) — queued #{number} @ {head[:7]}")
        silence()

    st.acquire(seat, key, head, why)
    st.queue_pop(seat, key)        # a direct event can outrun the drain: the entry is stale now

    workspace = isolation.ensure(loop, number, seat, head, login=login or "")
    if workspace is None and capacity > 1:
        st.release_if(seat, key)
        st.queue_add(seat, key, head, pr_url(loop, number),
                     "no isolated workspace — a parallel run would share a checkout")
        log(f"no isolated workspace for #{number} and concurrency={capacity} — queued")
        silence()
    if workspace is None:
        log(f"running #{number} without isolation (concurrency=1) under {artifacts_for(loop, number)}")
    return workspace
