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
from .util import log, now_iso, silence


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
    if reviews is None:
        silence("could not read the review list — not guessing the round count")
    return reviews if isinstance(reviews, list) else []


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
    return any(is_reviewer(r, loop) and r.get("commit_id") == head
               for r in reviews if isinstance(r, dict))


def approved_at_head(reviews: list, loop: dict, head: str) -> bool:
    return any(is_reviewer(r, loop) and gh.review_state(r) == "APPROVED"
               and r.get("commit_id") == head
               for r in reviews if isinstance(r, dict))


def changes_at_head(reviews: list, loop: dict, head: str) -> list:
    return [r for r in reviews
            if is_reviewer(r, loop) and gh.review_state(r) == "CHANGES_REQUESTED"
            and r.get("commit_id") == head]


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


def wake_adjudicator(loop: dict, number: int, head: str, rounds: int, reason: str) -> None:
    """Hand a stalled PR to the adjudicator seat — a route bound to its own profile.

    The rule the adjudicator is given: read the two positions, rule, and do not merge. The
    marker written by the caller stays the audit trail if this POST never lands.
    """
    target = routes.target(loop["adjudicator"]["route"], loop.get("host"))
    if not target:
        log("no adjudicator route — the breach marker is the only record")
        return
    payload = {"repository": {"full_name": loop["repo"]},
               "_loop": {**loop_block(loop, number, head, round=rounds, reason=reason),
                         "role": "adjudicator"}}
    if routes.fire(loop["adjudicator"]["route"], "pull_request", payload,
                   f"breach-{number}", loop.get("host")):
        log(f"adjudicator woken for #{number}")


def breach(loop: dict, st: state_mod.LoopState, number: int, head: str, rounds: int,
           reason: str) -> None:
    prior = st.breach_set(number, {
        "pr": number, "head": head, "rounds": rounds, "cap": loop["cap"],
        "reason": reason, "at": now_iso(), "status": "awaiting-adjudication",
    })
    st.note(f"breach {loop['repo']}#{number} at {head[:7]}: {reason}")
    # One wake per head: a PR already parked at this sha stays parked, so repeated events
    # cannot spawn an adjudication run each time. A new head is a new escalation.
    if prior.get("status") == "awaiting-adjudication" and prior.get("head") == head:
        log(f"#{number} already awaiting adjudication at {head[:7]} — not re-waking")
        return
    if not loop.get("adjudicator", {}).get("route"):
        log("no adjudicator configured — marker written, nothing woken")
        return
    wake_adjudicator(loop, number, head, rounds, reason)


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
    if len(live) >= capacity:
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
