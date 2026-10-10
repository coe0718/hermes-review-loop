"""``hermes dk`` — install, inspect and drive the loop from the CLI.

The plugin does not try to own the gateway. Everything the loop needs beyond its own scripts —
webhook routes, GitHub hooks, a cron entry — is written through the same config surfaces the
operator would touch by hand, so `hermes dk uninstall` is really just the inverse of
`init` and nothing lives in a place you cannot see.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile

from .ledger import LOCK_WAIT_S
from . import (attribution, config, doctor, envnames, gate, gate_shims, gh, observer, prompts,
               route_intent, routes, state as state_mod)
from .util import logged

# The shim and job names a fresh install gets; an install not yet migrated keeps the old pair
# (``config.watchdog_shim`` / ``config.watchdog_job_name`` say which is live).
SHIM_NAME = "diaktoros-watchdog.py"

# One shared job runs the shim (issue #60): `hermes cron create --script` takes only a filename
# under ~/.hermes/scripts/ and no arguments, so a job cannot carry `--loop <id>`. A single job
# whose shim runs the watchdog with no `--loop` sweeps every loop exactly once per tick — N loops
# cost N sweeps, not N². The shim is shared by that one job and is removed only with the last loop.
SHARED_JOB_NAME = "diaktoros watchdog"
SHARED_JOB_NAMES = frozenset(config.WATCHDOG_JOBS.values())


def watchdog_job_name(loop: dict) -> str:
    """The scheduler job name for a loop — now the shared name; the loop id is not part of it.

    Kept as a function of ``loop`` for the callers that already hold one (and for doctor to
    recognise a legacy per-loop job from before #60). A job created by ``init`` uses
    ``SHARED_JOB_NAME``: one job sweeps every loop.
    """
    return config.watchdog_job_name()


def _legacy_job_name(loop: dict) -> str:
    """The pre-#60 per-loop job name, recognised only to migrate it away."""
    return f"review loop watchdog ({loop['id']})"

SHIM = '''#!/usr/bin/env python3
"""Cron shim written by `hermes dk init`.

The scheduler runs scripts from ~/.hermes/scripts/, so this forwards to the plugin's own
watchdog and delivers its stdout. Keep the plugin as the single copy of the code.
"""
import pathlib
import subprocess
import sys

WATCHDOG = pathlib.Path("{watchdog}")
if not WATCHDOG.exists():
    print(f"diaktoros watchdog is missing: {{WATCHDOG}}")
    sys.exit(0)

proc = subprocess.run([sys.executable, str(WATCHDOG), *sys.argv[1:]], capture_output=True, text=True)
if proc.stdout.strip():
    print(proc.stdout.strip())
if proc.returncode != 0 and proc.stderr.strip():
    print(f"diaktoros watchdog failed: {{proc.stderr.strip()[:400]}}")
'''


def routes_for(loop: dict) -> dict:
    """The conventional route names for a loop, and the names ``init`` writes."""
    return {"reviewer": f"{loop['id']}-review", "fixer": f"{loop['id']}-fix",
            "observer": f"{loop['id']}-observe",
            "adjudicator": f"{loop['id']}-breach", "triage": f"{loop['id']}-triage"}


def _routes_of(loop: dict) -> dict:
    """role → route name, from the loop's own config: the seats own the names, not this module.

    A hand-edited loop may route its reviewer anywhere; every path that fires, verifies or
    rebinds a route must read that answer rather than re-derive the convention.
    """
    return route_intent.routes_of(loop)


# Which gate script each role's route must run. Ownership is checked against this before a route
# is written: the registry is shared with every other plugin on the host, and rebinding someone
# else's route to our profile would be a silent takeover of their webhook.
GATE_SCRIPT = route_intent.GATE_SCRIPT


# Each role's route prompt: together with the gate script, the proof that a route is ours.
_ROUTE_PROMPT = route_intent.ROUTE_PROMPT


def _verify_routes(loop: dict, roles) -> None:
    """Refuse to write a route that is not this loop's to write.

    Three rails: no other loop may already claim the name, no two roles of this loop may share one
    route (one route wakes one seat), and an installed route must have this role's exact gate and
    prompt. All three fail *before* anything is written, because a route is the one
    artifact here that another plugin could own.
    """
    mine = _routes_of(loop)
    route_roles = (*config.ROUTED_ROLES, "observer", "observer_urgent")
    wanted = [role for role in route_roles if role in set(roles) and role in mine]
    names = [mine[role] for role in route_roles if role in mine]
    shared = sorted({name for name in names if names.count(name) > 1})
    if shared:
        raise config.ConfigError(
            f"{loop['id']}: {', '.join(shared)} is routed to more than one seat — one route wakes "
            "one seat, or two profiles fight over the same webhook")
    try:
        other_loops = config.all_loops()
    except config.ConfigError as exc:
        raise config.ConfigError(f"cannot check route ownership: {exc}") from exc
    registry = routes.all_routes()
    for role in wanted:
        name = mine[role]
        # "Itself" is the loop file being rewritten — same id *and* same repo. A loop that merely
        # shares the id is a different loop, and `init` writing its own file would take the routes
        # (and the repo) out from under it.
        owners = [other["id"] for other in other_loops
                  if not (other["id"] == loop["id"] and other.get("repo") == loop.get("repo"))
                  and name in set(_routes_of(other).values())]
        if owners:
            raise config.ConfigError(
                f"route {name!r} already belongs to loop {', '.join(sorted(owners))} — give this "
                "loop its own route name (the registry is shared by every plugin)")
        if name not in registry:
            continue                  # not installed yet: nothing of someone else's to collide with
        entry = registry[name]
        if not isinstance(entry, dict):
            raise config.ConfigError(f"route {name!r} exists but its ownership cannot be verified "
                                     "— pick another route name")
        script = entry.get("script")
        if script != GATE_SCRIPT[role] and script not in config.LEGACY_GATE_SCRIPTS.get(role, ()):
            raise config.ConfigError(
                f"route {name!r} runs {script!r}, not {GATE_SCRIPT[role]!r} — it belongs to "
                "something else; pick another route name")
        if entry.get("prompt") != _ROUTE_PROMPT[role]:
            raise config.ConfigError(
                f"route {name!r} does not have this {role} gate's prompt — ownership cannot be "
                "verified; pick another route name")


def _install_routes(loop: dict, roles=None) -> dict:
    """Write (or rewrite) the loop's routes; return only the roles actually written.

    Rewriting is idempotent and keeps the existing secret, and it is how a stale route gets fixed:
    the URL itself carries the seat's profile, so a seat that changed profile has a route that no
    longer points at the right one until this runs. Nothing else in the registry is touched —
    another loop's routes and their secrets survive this untouched.
    """
    names = _routes_of(loop)
    host = config.webhook_host(loop.get("host"), required=True)
    skill = loop.get("skill") or ""
    wanted = ((*config.ROUTED_ROLES, "observer", "observer_urgent") if roles is None
              else tuple(roles))
    written: dict = {}
    for role in config.ROUTED_ROLES:
        if role not in wanted or role not in names:
            continue
        name = names[role]
        common = {"script": GATE_SCRIPT[role], "host": host,
                  "skills": [skill] if skill else []}
        if role == "reviewer":
            routes.new_route(name, profile=config.seat_profile(loop, "reviewer"),
                             prompt=prompts.REVIEWER, events=["pull_request"],
                             deliver="discord",
                             description=f"{loop['repo']} — wake the reviewer for a new or "
                                          "requested review", **common)
        elif role == "fixer":
            routes.new_route(name, profile=config.seat_profile(loop, "fixer"),
                             prompt=prompts.FIXER, events=["pull_request_review"],
                             deliver="discord",
                             description=f"{loop['repo']} — wake the fixer on a changes-requested "
                                          "verdict", **common)
        elif role == "triage":
            routes.new_route(name, profile=config.seat_profile(loop, "triage"),
                             prompt=prompts.TRIAGE, events=["issues"], deliver="discord",
                             description=f"{loop['repo']} — triage a new issue from an "
                                          "allowlisted author", **common)
        else:
            adjudicator = loop.get("adjudicator") or {}
            routes.new_route(name, profile=config.seat_profile(loop, "adjudicator"),
                             prompt=prompts.ADJUDICATOR, events=["pull_request"],
                             deliver=adjudicator.get("deliver", "telegram"),
                             description=f"{loop['repo']} — adjudicate a loop that spent its "
                                          "budget", **common)
        written[role] = name
    if "observer" in wanted and "observer" in names:
        observer_cfg = loop["observer"]
        name = names["observer"]
        routes.new_route(name, profile=config.seat_profile(loop, "observer"),
                         prompt=prompts.OBSERVER, events=["pull_request"],
                         script="observe.py", deliver=observer_cfg.get("deliver", "telegram"),
                         deliver_only=True, host=host,
                         description=f"{loop['repo']} — read-only observer feed: one short "
                                     "notice per loop transition")
        written["observer"] = name
    if "observer_urgent" in wanted and "observer_urgent" in names:
        observer_cfg = loop["observer"]
        name = names["observer_urgent"]
        routes.new_route(name, profile=config.seat_profile(loop, "observer_urgent"),
                         prompt=prompts.OBSERVER, events=["pull_request"],
                         script="observe.py",
                         deliver=observer_cfg.get("urgent_deliver") or observer_cfg.get(
                             "deliver", "telegram"),
                         deliver_only=True, host=host,
                         description=f"{loop['repo']} — read-only observer feed: urgent "
                                     "notices only")
        written["observer_urgent"] = name
    return written


def _seat_lines(loop: dict, title: str = "seat mapping") -> list[str]:
    """The effective mapping: who serves each role, as what login, woken by which route.

    This is the line an operator reads to answer "is the right agent actually the fixer here?" without
    opening two JSON files — so it shows the resolved values, including the route URL the profile
    is part of.
    """
    lines = [f"{title}:"]
    names = _routes_of(loop)
    host = loop.get("host") or ""
    for role in config.ROUTE_ROLES:
        profile = config.seat_profile(loop, role) or "(none)"
        name = names.get(role) or ""
        if not name:
            lines.append(f"  {role:<12} profile {profile:<16} route (none — nothing wakes it)")
            continue
        url = ""
        if host:
            try:
                url = routes.url_for_profile(name, profile, host) or ""
            except config.ConfigError:
                url = ""
        login = config.seat_login(loop, role) or "(unset)"
        # The adjudicator has no login, so it keeps the column empty rather than shifting the route
        # and URL columns out of line in the one place an operator compares seats side by side.
        if role == "adjudicator":
            adj = config.adjudicator_login(loop)
            login_cell = f"login {adj:<16} " if adj else " " * 23
        else:
            login_cell = f"login {login:<16} "
        lines.append(f"  {role:<12} profile {profile:<16} {login_cell}"
                     f"route {name:<22} {url}".rstrip())
    return lines


def _credential_lines(loop: dict) -> list[str]:
    """Where each seat's PAT is *referenced*. Never its value: the value lives in a 0600 file the
    seats read at use time, and this CLI has no business copying it into a terminal."""
    tokens = loop.get("tokens") or {}
    lines: list[str] = []
    seen: set[str] = set()
    for role in ("reviewer", "fixer"):
        login = config.seat_login(loop, role)
        if not login or login.lower() in seen:
            continue
        seen.add(login.lower())
        ref = tokens.get(login) or tokens.get(login.lower()) or ""
        lines.append(f"{role} {login} → "
                     f"{ref or '(no file mapped — falls back to read_token)'}")
    read_token = loop.get("read_token") or ""
    if read_token and read_token.lower() not in seen:
        ref = tokens.get(read_token) or tokens.get(read_token.lower()) or ""
        lines.append(f"read {read_token} → {ref or '(no file mapped)'}")
    adj = config.adjudicator_login(loop)
    if adj:
        lines.append(f"adjudicator {adj} → {_token_ref(loop, adj) or '(no file mapped)'}")
    return lines


def _token_ref(loop: dict, login: str) -> str:
    """The token *path* mapped for ``login`` (exact key first, then any case), or ``""``."""
    tokens = loop.get("tokens") or {}
    if not login:
        return ""
    if tokens.get(login):
        return str(tokens[login])
    return next((str(v) for k, v in tokens.items() if str(k).lower() == login.lower() and v), "")


def _role_summary(loop: dict, role: str) -> str:
    profile = config.seat_profile(loop, role) or "(no profile)"
    if role == "adjudicator":
        if not (loop.get("adjudicator") or {}).get("route"):
            return "adjudicator (none)"
        return f"adjudicator {profile}"
    return f"{role} {config.seat_login(loop, role) or '(no login)'} ({profile})"


def _route_state(loop: dict, role: str) -> str:
    """What the *installed* route serves, next to what the loop says it should serve.

    The mismatch is the interesting case: a seat whose profile moved but whose route did not is a
    loop that runs as the old agent while every config file claims otherwise.
    """
    name = _routes_of(loop).get(role) or ""
    if not name:
        return f"{role}: (no route)"
    want = config.seat_profile(loop, role)
    entry = routes.route(name)
    if not entry:
        if role in ("observer", "observer_urgent"):
            # init refuses an existing loop, so "run init" would be a dead end for the feed.
            return f"{role} {name}: not installed — {observer.route_remedy(loop).strip('`')}"
        return f"{role} {name}: not installed — run init"
    got = routes.route_profile(entry)
    if got is None:
        return (f"{role} {name} → {entry.get('profile')!r} (blank — the gateway refuses it), "
                f"not {want}: MISMATCH — hermes dk apply --loop {loop['id']}")
    if got == want:
        muted = role in ("observer", "observer_urgent") and (loop.get("observer") or {}).get("mute")
        return f"{role} {name} → {got} (ok{', muted' if muted else ''})"
    return (f"{role} {name} → {got}, not {want}: MISMATCH — "
            f"hermes dk apply --loop {loop['id']}")


def _seat_diffs(was: dict, now: dict) -> tuple[list[tuple[str, object, object]], set[str]]:
    """The seat identities the settings move, and which roles that touches.

    A role is *touched* when its profile, its login, or the login its review route serves moves:
    all three are "who does what", and each one owes the same validation, the same in-flight check
    and the same route rebind.
    """
    diffs: list[tuple[str, object, object]] = []
    touched: set[str] = set()
    for role in ("reviewer", "fixer"):
        for key in ("profile", "login"):
            before = str((was["seats"].get(role) or {}).get(key) or "")
            after = str((now["seats"].get(role) or {}).get(key) or "")
            if before != after:
                diffs.append((f"{role} {key}", before or "(none)", after or "(none)"))
                touched.add(role)
    review_seat_before = str(was.get("reviewer_seat") or "")
    review_seat_after = str(now.get("reviewer_seat") or "")
    if review_seat_before != review_seat_after and "reviewer" not in touched:
        # Only worth its own line when nothing else already explains it: the two move together
        # through the settings, and a duplicate line reads like two changes.
        diffs.append(("reviewer_seat", review_seat_before or "(none)", review_seat_after or "(none)"))
        touched.add("reviewer")
    adj_before = str((was.get("adjudicator") or {}).get("profile") or "")
    adj_after = str((now.get("adjudicator") or {}).get("profile") or "")
    if adj_before != adj_after:
        diffs.append(("adjudicator profile", adj_before or "(none)", adj_after or "(none)"))
        touched.add("adjudicator")
    # A seat's token file is part of who it is: a new path for a login a seat acts as is an
    # identity change, owed the same in-flight check. Paths only — the file is never opened here.
    for role in ("reviewer", "fixer"):
        login = config.seat_login(now, role)
        before, after = _token_ref(was, login), _token_ref(now, login)
        if before != after:
            diffs.append((f"{role} token file ({login})", before or "(none)", after or "(none)"))
            touched.add(role)
    # The adjudicator's comment identity has no run of its own to strand: it is reported, and
    # validated, but it does not rebind a route.
    login_before, login_after = config.adjudicator_login(was), config.adjudicator_login(now)
    if login_before != login_after:
        diffs.append(("adjudicator login", login_before or "(none)", login_after or "(none)"))
    if login_after:
        before, after = _token_ref(was, login_after), _token_ref(now, login_after)
        if before != after:
            diffs.append((f"adjudicator token file ({login_after})", before or "(none)",
                          after or "(none)"))
    return diffs, touched


def _route_binds(loop: dict, touched: set[str]) -> dict:
    """role → (route name, installed profile, target profile) for routes a seat change rebinds."""
    binds: dict = {}
    for role, name in _routes_of(loop).items():
        if role not in touched:
            continue
        entry = routes.route(name)
        if not entry:
            continue
        current = routes.route_profile(entry) or ""   # "" = blank: the gateway refuses it
        target = config.seat_profile(loop, role)
        if current != target:
            binds[role] = (name, current, target)
    return binds


def _drifted_routes(loop: dict) -> dict:
    """role → (route name, fields) for this loop's own routes whose registry entry differs from
    what the plugin writes (``gate_shims.contract_drift``) in a way apply can repair.

    Only a route provably ours (its role's gate *and* prompt) counts; rewriting it from the config
    keeps its secret. That covers the gateway's 403 on ``enabled: false``, an observer that lost
    ``deliver_only`` (which would wake an agent) or its destination, and a seat off its event.
    """
    out: dict = {}
    for role, name in _routes_of(loop).items():
        entry = routes.route(name)
        if (isinstance(entry, dict) and entry.get("script") == GATE_SCRIPT[role]
                and entry.get("prompt") == _ROUTE_PROMPT[role]):
            fields = gate_shims.contract_drift(loop, role, entry)
            if fields:
                out[role] = (name, fields)
    return out


def _stale_scripts(loop: dict) -> dict:
    """role → (route name, installed script) for this loop's routes still on an older gate.

    Only a script this plugin itself once installed for that role counts, and only with the
    role's own prompt: anything else is not provably ours and ``_verify_routes`` refuses it.
    """
    stale: dict = {}
    for role, name in _routes_of(loop).items():
        entry = routes.route(name)
        if not isinstance(entry, dict):
            continue
        script = entry.get("script")
        if (script in config.LEGACY_GATE_SCRIPTS.get(role, ())
                and entry.get("prompt") == _ROUTE_PROMPT[role]):
            stale[role] = (name, script)
    return stale


def _busy_seats(loop: dict, roles: set[str]) -> list[str]:
    """Seats with a run in flight right now — the ones an identity change must not surprise."""
    from . import state as state_mod

    st = state_mod.state_for(loop)
    lines = []
    for seat in ("reviewer", "fixer"):
        if seat not in roles:
            continue
        live = st.active(seat)
        if live:
            held = ", ".join(f"{key} ({int((time.time() - entry.get('at', time.time())) / 60)}m)"
                             for key, entry in sorted(live.items()))
            lines.append(f"{seat} is in flight on {held}")
    return lines


def _observer_check(loop: dict) -> None:
    """Refuse an observer destination the gateway could not deliver without waking an agent.

    ``deliver_only`` is the gateway's no-agent mode and it rejects a ``log`` target outright: a
    route configured that way does not merely fail to deliver a notice, it stops the gateway from
    starting. So the mistake is caught here, before anything is written, rather than at the
    gateway's next restart.
    """
    observer_cfg = loop.get("observer") or {}
    if observer_cfg.get("route") and (observer_cfg.get("deliver") or "log") == "log":
        raise config.ConfigError(
            "observer.deliver must be a real destination (telegram, discord, ...) — an observer "
            "route never wakes an agent, and the gateway refuses a deliver_only file target")


def _on_off(args, name: str, default):
    """An ``on|off`` flag as a bool; ``default`` when the flag was not given."""
    value = getattr(args, name, None)
    return default if value is None else value == "on"


BRANCH_PUSH_REFUSAL = (
    "refused: review_only_update lets the host push a merge commit to a branch the loop does not "
    "own (a review-only author's, same repository only, only a clean merge, with a lease on the "
    "head it read), as the account that makes host merge pushes, and needs unattended fixer "
    "pushes. If the author pushes meanwhile the lease fails and nothing is overwritten, but a "
    "published merge cannot be undone by the loop. Pass --acknowledge-branch-push to opt in.")


def _attribution_arg(args, default):
    """``--attribution on|off`` as a bool; ``default`` when the flag was not given (#197)."""
    value = getattr(args, "attribution", None)
    return default if value is None else value == "on"


def _observer_args(args, loop_id: str) -> dict:
    """The observer block a new loop starts with — empty when the feed was not asked for.

    Opt-in by construction: naming a profile (or a route) is the whole switch, and a loop without
    one behaves exactly as it did before this existed.
    """
    if not (args.observer_profile or args.observer_route):
        return {}
    observer_cfg = {"route": args.observer_route or f"{loop_id}-observe",
                    "profile": args.observer_profile or "default",
                    "deliver": args.observer_deliver}
    if args.observer_events:
        observer_cfg["events"] = args.observer_events
    if args.observer_digest_min:
        observer_cfg["digest_min"] = args.observer_digest_min
    for key in ("urgent_route", "urgent_profile", "urgent_deliver"):
        if getattr(args, f"observer_{key}", ""):
            observer_cfg[key] = getattr(args, f"observer_{key}")
    return observer_cfg




def _install_hooks(loop: dict, token_login: str | None, active: bool = False,
                   seats=None) -> list[str]:
    """Create the loop's repo hooks via the API (every hooked role, or only ``seats``). Needs hook
    write access on the repo: classic ``repo``, or the narrower ``admin:repo_hook``."""
    names = _routes_of(loop)
    host = config.webhook_host(loop.get("host"), required=True)
    # Validate every destination and secret before creating any external hook.
    hooks = []
    for seat in config.hook_roles(loop):
        event = config.HOOK_EVENT[seat]
        if seats is not None and seat not in seats:
            continue
        route_name = names.get(seat, "")
        url = routes.url_for(route_name, host)
        secret = (routes.route(route_name) or {}).get("secret", "")
        if not url or not secret:
            raise config.ConfigError(f"route {route_name!r} needs a valid webhook URL and secret before installing hooks")
        hooks.append((event, url, secret))
    baseline = _hook_listing(loop, token_login, require_active=False)
    created = []
    try:
        for event, url, secret in hooks:
            # Created paused unless the operator asked for --arm: a loop goes live with `arm`,
            # after `doctor` and `selftest` pass, never as a side effect of writing its config.
            body = {"name": "web", "active": active, "events": [event],
                    "config": {"url": url, "content_type": "json", "secret": secret,
                               "insecure_ssl": "0"}}
            result = gh.api(loop, f"/repos/{loop['repo']}/hooks", method="POST", body=body,
                            login=token_login or loop.get("read_token"))
            if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                raise config.ConfigError(f"hook creation not confirmed for {url}: "
                                         f"{hook_write_need(loop, token_login)}")
            created.append(result["id"])
        state = "armed" if active else "paused"
        return [f"hook {id} → {url} ({state})" for id, (_, url, _) in zip(created, hooks)]
    except Exception as exc:
        failures = []
        # A lost POST response may still have created a hook: compare with the baseline.
        try:
            current = _hook_listing(loop, token_login, require_active=False)
            target_urls = {url for _, url, _ in hooks}
            created = list(set(created) | {h["id"] for h in current
                           if h["id"] not in {b["id"] for b in baseline}
                           and h["config"]["url"] in target_urls})
        except Exception as read_exc:
            failures.append(f"cannot identify newly created hooks: {read_exc}")
        for hook_id in created:
            try:
                gh.api(loop, f"/repos/{loop['repo']}/hooks/{hook_id}", method="DELETE",
                       login=token_login or loop.get("read_token"))
                if any(h["id"] == hook_id for h in _hook_listing(loop, token_login, require_active=False)):
                    raise config.ConfigError("still present after DELETE")
            except Exception as rollback_exc:
                failures.append(f"hook {hook_id}: {rollback_exc}")
        raise config.ConfigError(f"hook install failed: {exc}; " +
                                 ("ROLLBACK FAILED: " + "; ".join(failures) if failures
                                  else "created hooks removed")) from exc

class HookAccessError(config.ConfigError):
    """GitHub did not let this token read or write the repo's hooks. Only this cause is fixed by
    a different token; every other hook error names its own remedy."""


def _hook_fix(loop: dict, exc: Exception, token_login: str | None = None) -> str:
    """The remedy for a failed hook step, matched to its cause."""
    if isinstance(exc, HookAccessError):
        return f"re-run with --admin-token <login> ({hook_write_need(loop, token_login)})"
    return f"`hermes dk doctor --loop {loop['id']}` names what to repair first"


def _hook_listing(loop: dict, token_login: str | None = None, *,
                  require_active: bool = True) -> list[dict]:
    hooks = gh.api(loop, f"/repos/{loop['repo']}/hooks?per_page=100",
                   login=token_login or loop.get("read_token"))
    # ``active`` is part of a valid entry where a choice depends on it (which of two hooks for one
    # route is kept). `arm` asks for ``require_active=False``: it flips and reads back a hook with
    # no real bool rather than trusting — or refusing — the listing's word for its state (#55).
    if not isinstance(hooks, list) or len(hooks) >= 100 or any(
        not isinstance(h, dict) or not isinstance(h.get("id"), int) or
        (require_active and not isinstance(h.get("active"), bool)) or
        not isinstance(h.get("config"), dict) or
        not isinstance(h["config"].get("url"), str) for h in hooks
    ):
        raise HookAccessError("cannot read a complete, valid repo hook listing; no changes made")
    return hooks


def _keep_one(candidates: list[dict], dest: str) -> tuple[dict, list[dict]]:
    """Of several repo hooks for one route, the one that stays: an ACTIVE hook first (a route must
    never end up with fewer armed hooks than it had), then one already at ``dest``, then the
    oldest (lowest id). The rest are redundant — named for deletion, never moved or deleted."""
    ordered = sorted(candidates, key=lambda hook: (hook.get("active") is not True,
                                                  not routes.serves_route_url(hook["config"]["url"], dest),
                                                  hook["id"]))
    return ordered[0], ordered[1:]


def hook_write_need(loop: dict, token_login: str | None) -> str:
    """Which login's file creates, arms and pauses this loop's hooks, and what it must carry.

    ``init --hooks`` and ``arm`` act as ``--admin-token``'s login, else as the reader. The reader
    is the one role that can hold a read-only token — and on a user-owned repo it is usually the
    owner, the only account that can manage hooks at all — so say which case this is.
    """
    reader = str(loop.get("read_token") or "")
    login = token_login or reader
    need = (f"the token for {login!r} needs hook write access on {loop.get('repo')} (classic `repo` or "
            "`admin:repo_hook`, or fine-grained `repository_hooks: write`)")
    if login and login.lower() == reader.lower():
        need += (" — that is the reader's file. On a user-owned repo only the owner can manage "
                 "hooks, so if the reader is the owner give that file hook write; otherwise pass "
                 "--admin-token <owner login> (a login mapped with its own --token at init)")
    else:
        need += " — check that login's token file"
    return need


def _hook_editor_line(loop: dict, token_login: str | None, armed: bool, dry_run: bool) -> str:
    """Who created (or would create) the hooks, in which state, and what `arm` will need."""
    who = token_login or loop.get("read_token")
    verb = "would be created" if dry_run else "were created"
    state = "armed (--arm)" if armed else "paused"
    return (f"hooks {verb} {state} as {who}, and `arm` / `arm --pause` edit them as {who} too: "
            f"{hook_write_need(loop, token_login)}")


def _hook_write_fix(token_login: str | None, transient: bool = False,
                    loop: dict | None = None) -> str:
    """What to do when a hook read or write failed with the token ``arm`` used.

    ``transient`` is for failures that were neither a refusal nor a write that did not stick (a
    timeout, a 5xx): those are worth a retry before blaming the token.
    """
    if transient:
        return ("retry `arm`; if it fails again, check the repo's webhooks on GitHub — "
                + _hook_write_fix(token_login, loop=loop))
    return hook_write_need(loop or {}, token_login)


def _set_hooks(loop: dict, active: bool, token_login: str | None) -> tuple[list[str], bool]:
    """Flip this loop's repo hooks, then read each one back and report what GitHub now shows.

    Returns ``(lines, ok)``. ``ok`` is true only when every loop hook was observed in the asked-for
    state: a refused PATCH, a read-back that disagrees or cannot be made, an unreadable listing,
    a repo with no loop hooks, and a repo missing either seat's hook all make it false — ``arm``
    must never say "paused" about a hook GitHub still delivers to, nor "armed" about a loop that
    only one seat can hear.
    """
    names = _routes_of(loop)
    seats = [(role, names[role]) for role in config.hook_roles(loop) if names.get(role)]
    wanted = tuple(name for _, name in seats)
    word = "active" if active else "paused"
    login = token_login or loop.get("read_token")
    try:
        hooks = _hook_listing(loop, token_login, require_active=False)
    except config.ConfigError as exc:
        return [f"could not read the repo's hooks: {exc}",
                f"fix: {_hook_write_fix(token_login, loop=loop)}"], False
    try:
        # One matcher (doctor.split_route_hooks). Arming credits a hook only at the registry's
        # route URL bound to the seat's profile: another profile's URL (the gateway answers it
        # 404), another path, a retired gateway — or a route named inside another route's name —
        # is not that seat's hook. Pausing asks the ownership question instead: every hook this
        # install made is stopped, even once `uninstall` has removed the route it posted to.
        own, foreign = doctor.split_route_hooks(loop, hooks, wanted, ownership=not active)
    except config.ConfigError as exc:
        return [f"cannot tell this loop's hooks from anyone else's: {exc}",
                f"fix: hermes dk set --loop {loop.get('id')} --host "
                "https://your-gateway.example"], False
    out, ok = [], True
    failed: list[tuple[int, list[str]]] = []      # (hook id, the errors that decide its fix)
    targets = {name: doctor.seat_route_target(loop, name) for name in wanted}
    role_of = {name: role for role, name in seats}
    for hook in foreign:
        name = doctor.hook_route_name(hook)
        want, reason = targets[name]
        if want is None and active:
            out.append(f"hook {hook['id']} posts to route {name!r}; {reason} — not armed, "
                       "left as it is")
            continue
        why = doctor.hook_url_difference(str(hook["config"].get("url") or ""),
                                         want or (doctor.install_hook_urls(loop, name) or [""])[0])
        out.append(f"hook {hook['id']} posts to route {name!r} at {why} — not this seat's hook "
                   "(nothing this loop serves receives it), left as it is")
    matched: set[str] = set()
    miswired: list[int] = []
    for hook in own:
        name = doctor.hook_route_name(hook)
        matched.add(name)
        if active:
            # At the seat's URL but subscribed to the wrong event (or not JSON), or with a
            # trailing slash the gateway 404s: doctor calls each a MISMATCH, so arming it would
            # report a loop live that the seat never hears. (Found by same_hook_url, judged by
            # exact_hook_url — pausing still stops it.)
            want = targets[name][0] or ""
            problem = ("" if doctor.exact_hook_url(str(hook["config"].get("url") or ""), want)
                       else doctor.SLASH_404)
            problem = problem or doctor.hook_wake_problem(hook, role_of.get(name, ""))
            if problem:
                ok = False
                miswired.append(hook["id"])
                why = (problem if problem.endswith("never woken") else
                       f"{problem}, so it would not wake the {role_of.get(name, '?')} seat")
                out.append(f"hook {hook['id']} NOT armed: it {why} — left as it is")
                continue
        # Only a real bool is a state: a hook with no `active` (or a non-bool one) is flipped and
        # read back like any other, never taken as already there.
        if isinstance(hook.get("active"), bool) and hook["active"] == active:
            out.append(f"hook {hook['id']} already {word}")
            continue
        path = f"/repos/{loop['repo']}/hooks/{hook['id']}"
        _, error = gh.fetch(loop, path, method="PATCH", body={"active": active}, login=login)
        # A lost response is ambiguous and a stub or proxy can answer 200 without writing, so the
        # hook's state is whatever a fresh read says — never what was asked for.
        actual, read_error = gh.fetch(loop, path, login=login)
        if not isinstance(actual, dict) or not isinstance(actual.get("active"), bool):
            ok = False
            out.append(f"hook {hook['id']} NOT CONFIRMED {word}: "
                       + (f"PATCH failed ({error}); " if error else "")
                       + f"read-back failed ({read_error or 'no hook in the answer'})")
            failed.append((hook["id"], [e for e in (error, read_error) if e]))
            continue
        seen = "active" if actual["active"] else "paused"
        if actual["active"] == active:
            out.append(f"hook {hook['id']} → {seen} (read back)")
            continue
        ok = False
        out.append(f"hook {hook['id']} is still {seen}, not {word}"
                   + (f": PATCH failed ({error})" if error else ": GitHub accepted the PATCH "
                      "but the read-back disagrees"))
        # A PATCH GitHub accepted that did not stick is a refusal; a failed PATCH is judged by its
        # own code, so a timeout or a 5xx gets the retry advice, not the token-scope one.
        failed.append((hook["id"], [error or "refused"]))
    # Arming needs each seat's route bound in the registry; pausing only needs the hooks.
    unbound = [(role, name) for role, name in seats
               if active and targets[name][0] is None]
    if not matched and not unbound:
        return out + ["no loop hooks found at the routes' own URLs — run init --hooks first "
                      f"(looked for hooks posting to {', '.join(wanted) or 'a loop route'})"], False
    missing = [(role, name) for role, name in seats if name not in matched]
    for role, name in missing:
        reason = ((targets[name][1] if active else "") or
                  ("no repo hook posts to this route's URL, so the loop cannot be "
                   f"{'armed' if active else 'paused'} as a whole"))
        out.append(f"hook:{name} ABSENT ({role} seat) — {reason}")
    if miswired:
        out.append(f"fix: hook{'s' if len(miswired) > 1 else ''} {', '.join(map(str, miswired))}:"
                   f" `hermes dk doctor --loop {loop.get('id')}` names what each needs "
                   "(re-run init --hooks, or on GitHub set the event / content_type, or drop the "
                   f"URL's trailing slash — `hermes dk apply --hooks --loop {loop.get('id')}` "
                   "repoints a slashed hook), then run `arm` again")
    # The fix is per hook: one refused hook must not hide the retry advice another hook's 5xx
    # earned. Hooks that need the same fix share its line.
    advice: dict[str, list[int]] = {}
    for hook_id, errors in failed:
        # GitHub answers 404 to a token that may not see hooks, so 404 counts as a refusal too.
        refused = any(e == "refused" or any(f"HTTP {code}" in e for code in (401, 403, 404))
                      for e in errors)
        advice.setdefault(_hook_write_fix(token_login, transient=not refused, loop=loop),
                          []).append(hook_id)
    for text, ids in advice.items():
        out.append(f"fix: hook{'s' if len(ids) > 1 else ''} {', '.join(map(str, ids))}: {text}")
    if unbound:
        ok = False
        out.append(f"fix: `hermes dk doctor --loop {loop.get('id')}` names what each "
                   "route needs (its `route:` line and fix) — repair the route first, then run "
                   f"`arm{' --pause' if not active else ''}` again")
    if [pair for pair in missing if pair not in unbound]:
        ok = False
        out.append(f"fix: `hermes dk doctor --loop {loop.get('id')}` shows the hook each "
                   "seat needs; add the missing one (by hand with its route's URL and secret, or "
                   "via `init --hooks` for a loop being set up — hook write on "
                   f"{loop.get('repo')}, --admin-token <login>), then run "
                   f"`arm{' --pause' if not active else ''}` again")
    return out, ok


def _hook_moves(before: dict, after: dict, binds: dict, token_login: str | None = None, *,
                check_only: bool = False) -> tuple[list, list]:
    """Preflight exact hook URLs against configured and installed owned route profiles."""
    names = _routes_of(after)
    expected: dict[str, tuple[str, str]] = {}
    targets: dict[str, str] = {}
    unchanged: dict[str, str] = {}
    for role in config.hook_roles(after):
        name = names.get(role)
        if not name or name != _routes_of(before).get(role):
            continue
        new = routes.url_for_profile(name, config.seat_profile(after, role), after.get("host"))
        old = routes.url_for_profile(name, config.seat_profile(before, role), before.get("host"))
        if not old or not new:
            raise config.ConfigError(f"cannot resolve {role} hook URL; no changes made")
        if old != new:
            expected[old] = (role, new)
        if role in binds:
            # The registry can drift while loop config and form still agree. Its owned route's
            # installed profile is another known old URL, not an arbitrary hook destination.
            installed = routes.url_for_profile(name, binds[role][1], before.get("host"))
            if not installed:
                raise config.ConfigError(f"cannot resolve installed {role} hook URL; no changes made")
            if installed != new:
                expected[installed] = (role, new)
            targets[role] = new
        elif old != new:
            targets[role] = new
        else:
            unchanged[role] = new
    route_names = {name: role for role, name in names.items() if role in config.hook_roles(after)}
    if not targets and not (check_only and unchanged):
        # Nothing moves: a settings push reads no hooks. Only plain `apply` (check_only) reads
        # them anyway, to name duplicates and repoint a slashed hook nothing else would report.
        return [], []
    if targets:
        hooks = _hook_listing(before, token_login)  # Never repair a route if this listing cannot be trusted.
    else:
        # Nothing moves, so an unreadable listing blocks nothing — but a readable one is still
        # checked for two hooks on one route URL, which nothing else would ever report.
        try:
            hooks = _hook_listing(before, token_login)
        except config.ConfigError as exc:
            print(f"  (repo hooks not checked for duplicates: {exc})")
            return [], []
    moves, redundant = [], []
    for name, role in route_names.items():
        mine = [hook for hook in hooks if routes.route_name_of(hook["config"]["url"]) == name]
        if role in targets:
            dest = targets[role]
            for hook in mine:
                old = hook["config"]["url"]
                # A trailing slash on the target or a known old URL is the same route, spelled so
                # the gateway 404s it: repairable by a move, never "unexpected".
                known = routes.same_webhook_url(old, dest) or any(
                    routes.same_webhook_url(old, url) and owner == role
                    for url, (owner, _new) in expected.items())
                if not known:
                    raise config.ConfigError(f"installed {role} hook {hook['id']} "
                                             f"points at unexpected URL {old!r}; no changes made")
        elif role in unchanged:
            dest = unchanged[role]
            # Every hook on this route is left (already right), repaired (a trailing slash on this
            # URL), or refused — never skipped in silence (#55): one posting anywhere else is not
            # something this push can explain.
            for hook in mine:
                if not routes.same_webhook_url(hook["config"]["url"], dest):
                    raise config.ConfigError(
                        f"installed {role} hook {hook['id']} points at unexpected URL "
                        f"{hook['config']['url']!r}; no changes made — `hermes dk doctor "
                        f"--loop {after.get('id')}` names what it is")
        else:
            continue
        if not mine:
            continue
        keep, rest = _keep_one(mine, dest)
        if not routes.serves_route_url(keep["config"]["url"], dest):
            moves.append((keep["id"], keep["config"]["url"], dest, keep["config"].get("insecure_ssl")))
        redundant += [(hook, keep) for hook in rest]
    return moves, redundant


def _hook_origin(loop: dict, drifted: dict) -> dict:
    """The loop as its repo hooks still know it, for ``_hook_moves``' "before" side.

    After ``set --host`` the loop config already names the new origin while the seat routes (and
    the hooks that post to them) still carry the old one; apply rewrites the routes' origin
    (``drifted`` ⊇ "host"), so the hooks' old URLs are the routes' *recorded* origin. Handing
    that to ``_hook_moves`` moves the hooks with the routes through #106's own path — its
    listing, active-first choice, duplicate naming and full-config PATCH — unchanged.
    """
    hosts = {str((routes.route(name) or {}).get("host") or "").removesuffix("/")
             for role, (name, fields) in drifted.items()
             if role in config.HOOK_EVENT and "host" in fields}
    if not hosts:
        return loop
    if len(hosts) > 1:
        raise config.ConfigError("the reviewer and fixer routes record different gateway origins; "
                                 "cannot tell which one their repo hooks post to — no changes made")
    return {**loop, "host": hosts.pop()}


def _ensure_hooks(loop: dict, token_login: str | None, dry_run: bool,
                  outcome: dict | None = None) -> int:
    """``apply --hooks``: make this loop's two repo hooks what its routes need, the way ``init
    --hooks`` would have for a new loop (``init`` refuses an existing one).

    Per seat, hooks are matched by route name and one is kept by #106's ``_keep_one`` (active
    first). A seat with none gets one created, paused until ``arm``. The kept hook is repointed at
    the route's exact URL with ``_patch_hook_url`` (full config: the route's secret, the hook's own
    TLS setting, json), and given the gate's event if it lacks it. Extra hooks are only named. The
    listing is read first and nothing is written if it cannot be trusted; returns an exit code.
    """
    names = _routes_of(loop)
    try:
        host = config.webhook_host(loop.get("host"), required=True)
        listing = _hook_listing(loop, token_login)
        plans, missing = [], []
        for seat in config.hook_roles(loop):
            event = config.HOOK_EVENT[seat]
            name = names.get(seat, "")
            url = routes.url_for(name, host) if name else None
            if not url or not (routes.route(name) or {}).get("secret"):
                raise config.ConfigError(f"route {name!r} needs a webhook URL and a secret before "
                                         "its hook can be written")
            mine = [hook for hook in listing if routes.route_name_of(hook["config"]["url"]) == name]
            if not mine:
                missing.append((seat, url))
                continue
            keep, rest = _keep_one(mine, url)
            todo = []
            if (not routes.serves_route_url(keep["config"]["url"], url)
                    or keep["config"].get("content_type") != "json"):
                todo.append("config")
            if event not in (keep.get("events") or []):
                todo.append("event")
            plans.append((seat, event, url, keep, rest, todo))
    except config.ConfigError as exc:
        print(f"refused: repo hooks not reconciled: {exc}")
        print(f"  fix: re-run with --admin-token <login> ({hook_write_need(loop, token_login)})")
        return 2
    if outcome is not None:
        outcome["created"] = [seat for seat, _ in missing]
        outcome["kept"] = {seat: keep for seat, _e, _u, keep, _r, _t in plans}
    verb = "would " if dry_run else ""
    for seat, url in missing:
        print(f"  hook for {names[seat]}: {verb}create → {url} (paused until `arm`)")
    for seat, event, url, keep, rest, todo in plans:
        if "config" in todo:
            print(f"  hook {keep['id']}: {verb}repoint → {url} (json, secret and TLS kept)")
        if "event" in todo:
            print(f"  hook {keep['id']}: {verb}add event {event!r}")
        for hook in rest:
            print(f"  {_redundant_hook_line(loop, hook, keep)}")
    redundant = any(rest for *_ignored, rest, _todo in plans)
    if dry_run:
        return 1 if redundant else 0
    login = token_login or loop.get("read_token")
    try:
        if missing:
            for line in _install_hooks(loop, token_login, seats=[seat for seat, _ in missing]):
                print(f"  {line}")
        for seat, event, url, keep, rest, todo in plans:
            if "config" in todo:
                _patch_hook_url(loop, keep["id"], url, token_login,
                                insecure_ssl=keep["config"].get("insecure_ssl"))
                print(f"  hook {keep['id']} → {url}")
            if "event" in todo:
                path = f"/repos/{loop['repo']}/hooks/{keep['id']}"
                gh.api(loop, path, method="PATCH", body={"add_events": [event]}, login=login)
                actual = gh.api(loop, path, login=login)
                if not isinstance(actual, dict) or event not in (actual.get("events") or []):
                    raise config.ConfigError(f"hook {keep['id']} event {event!r} not confirmed — "
                                             f"{hook_write_need(loop, token_login)}")
                print(f"  hook {keep['id']} now subscribes to {event!r}")
    except config.ConfigError as exc:
        print(f"repo hooks NOT fully reconciled: {exc}")
        print(f"  fix: re-run with --admin-token <login> ({hook_write_need(loop, token_login)})")
        return 2
    return 1 if redundant else 0


def _redundant_hook_line(loop: dict, hook: dict, keep: dict) -> str:
    def state(h: dict) -> str:
        return "active" if h["active"] else "paused"
    return (f"⚠️ hook {hook['id']} ({state(hook)}) at {hook['config']['url']} is redundant — hook "
            f"{keep['id']} ({state(keep)}) is the one kept for this route, so this one was left "
            f"where it is rather than duplicated. Delete it: "
            f"`gh api -X DELETE repos/{loop['repo']}/hooks/{hook['id']}`")


def _hook_config(url: str, insecure_ssl=None) -> dict:
    """The complete config a loop hook for ``url`` must carry. Always sent whole: GitHub may treat
    a PATCHed ``config`` as a replacement, and a url-only body would then drop the secret, leaving
    a hook whose deliveries the route rejects. The secret is the route's own, from the registry;
    it is never printed or logged. ``insecure_ssl`` is the hook's own (the operator's TLS choice,
    read from the hook listing); ``"0"`` — verify TLS — only when the hook has none."""
    name = routes.route_name_of(url)
    secret = (routes.route(name) or {}).get("secret") or ""
    if not name or not secret:
        raise config.ConfigError(f"route {name or url!r} has no secret in the registry; "
                                 "the hook was not changed")
    ssl = "0" if insecure_ssl in (None, "") else str(insecure_ssl)
    return {"url": url, "content_type": "json", "insecure_ssl": ssl, "secret": secret}


def _patch_hook_url(loop: dict, hook_id: int, url: str, login: str | None = None,
                    require_secret: bool = False, insecure_ssl=None) -> None:
    path = f"/repos/{loop['repo']}/hooks/{hook_id}"
    login = login or loop.get("read_token")
    result = gh.api(loop, path, method="PATCH", body={"config": _hook_config(url, insecure_ssl)}, login=login)
    # A lost response is ambiguous. Always read back and roll back if it does not agree.
    actual = gh.api(loop, path, login=login)
    got = (actual.get("config") or {}) if isinstance(actual, dict) else {}
    if not isinstance(result, dict) or not isinstance(actual, dict) or got.get("url") != url \
            or (require_secret and not got.get("secret")):
        raise HookAccessError(f"hook {hook_id} update not confirmed as {url!r} — "
                              f"{hook_write_need(loop, login)}")


def _hook_route_names(loop: dict) -> set[str]:
    """The route names a repo hook of this loop posts to (both seats, and triage when on)."""
    return {name for role, name in _routes_of(loop).items()
            if role in config.hook_roles(loop) and name}


# One matcher for every caller (doctor.split_route_hooks): arm and selftest --ping credit a hook
# only at the registry's route URL; uninstall and init's stale-hook guard (ownership) also count
# the URL the loop's config gives a route the registry no longer holds.
_hook_route_name = doctor.hook_route_name


def _classify_hooks(loop: dict, login: str | None) -> tuple[list[dict] | None, list[dict], str]:
    """``(own, foreign, error)`` for the hooks posting to this loop's route names.

    *Own* hooks post to exactly one of the routes' URLs (``doctor.seat_hook_url``: the gateway
    binds a route to its profile by URL and answers any other profile's URL 404). *Foreign* ones
    post to the same route name at another origin, profile or path — another install, an old
    gateway — and are only ever reported, never deleted or counted as a collision. Every page is read; a partial or malformed listing is
    ``(None, [], reason)``, never "no hooks".
    """
    listing, error = gh.hooks_read(loop, login or loop.get("read_token"))
    if error:
        return None, [], error
    if any(not isinstance(hook.get("id"), int) or not isinstance(hook.get("config"), dict)
           or not isinstance(hook["config"].get("url"), str) for hook in listing):
        return None, [], "invalid hook listing"
    try:
        own, foreign = doctor.split_route_hooks(loop, listing, _hook_route_names(loop),
                                                ownership=True)
    except config.ConfigError as exc:
        return None, [], f"cannot resolve the loop's webhook host: {exc}"
    return own, foreign, ""


def _loop_hooks(loop: dict, login: str | None) -> tuple[list[dict] | None, str]:
    """This loop's own repo hooks (see ``_classify_hooks``)."""
    own, _, error = _classify_hooks(loop, login)
    return own, error


def _foreign_lines(loop: dict, foreign: list[dict]) -> list[str]:
    lines = []
    for hook in foreign:
        name = _hook_route_name(hook)
        why = doctor.hook_url_difference(str(hook["config"].get("url") or ""),
                                         (doctor.install_hook_urls(loop, name) or [""])[0])
        lines.append(f"hook {hook['id']} posts to route {name!r} at {why} — not this install's "
                     "hook (not the route's URL), left alone")
    return lines


def _hook_delete_commands(loop: dict, hook_ids) -> list[str]:
    """Pasteable ``gh`` commands that delete these hooks (a token with admin:repo_hook or repo)."""
    repo = loop["repo"]
    return [f"gh api -X DELETE {shlex.quote(f'repos/{repo}/hooks/{hook_id}')}"
            for hook_id in hook_ids]


def _hook_find_command(loop: dict) -> str:
    """A pasteable ``gh`` command listing the ids of the hooks posting to this loop's routes."""
    names = "|".join(sorted(_hook_route_names(loop)))
    jq = f'.[] | select(.config.url | test("/webhooks/({names})/?$")) | .id'
    repo = loop["repo"]
    return (f"gh api {shlex.quote(f'repos/{repo}/hooks?per_page=100')} "
            f"--jq {shlex.quote(jq)}")


def _delete_loop_hooks(loop: dict, login: str | None
                       ) -> tuple[list[str], list[str], list[int], bool]:
    """Delete this loop's repo hooks and read the listing back: ``(done, failures, left, unread)``.

    Deleted rather than paused: a paused hook still signs with a secret the next install's route
    will not hold, and it is exactly what a later ``init --hooks`` would trip over.

    ``unread`` is the structured fact that some DELETEs were accepted but the read-back could not
    confirm them (the honest case): it lets ``_uninstall_refused`` choose that remedy by fact
    rather than by matching the reason's wording (#131).
    """
    hooks, foreign, error = _classify_hooks(loop, login)
    if hooks is None:
        return [], [f"could not read the repo's hooks: {error}"], [], False
    done, failures = _foreign_lines(loop, foreign), []
    login = login or loop.get("read_token")
    if not hooks:
        # Nothing of ours to delete — but "gone" is only ever concluded from a read: a second
        # sample still catches a hook created meanwhile (a concurrent arm or init --hooks), and
        # an unreadable one is "not confirmed", never a claim that anything is live.
        after, error = _loop_hooks(loop, login)
        if after is None:
            return done, [f"could not confirm that no hook of this loop's appeared while uninstall "
                          f"ran (the listing read-back failed: {error}) — nothing was deleted"], [], False
        if after:
            ids = sorted(hook["id"] for hook in after)
            return done, [f"hook{'s' if len(ids) > 1 else ''} {', '.join(map(str, ids))} "
                          "appeared on this loop's route URLs while uninstall ran (a concurrent "
                          "arm or init --hooks?) — not deleted"], ids, False
        return done, failures, [], False
    refused: list[int] = []                       # ids whose DELETE failed, recorded as they fail
    for hook in hooks:
        _, error = gh.fetch(loop, f"/repos/{loop['repo']}/hooks/{hook['id']}", method="DELETE",
                            login=login)
        if error:
            refused.append(hook["id"])
            failures.append(f"hook {hook['id']}: DELETE failed ({error})")
    after, error = _loop_hooks(loop, login)
    if after is None:
        # Only the hooks whose DELETE failed are known to be live; the rest were accepted and
        # merely not read back — say exactly that, never "still live" about ids that are gone.
        accepted = [hook["id"] for hook in hooks if hook["id"] not in refused]
        if accepted:
            failures.append(f"could not confirm the deletion of hook"
                            f"{'s' if len(accepted) > 1 else ''} "
                            f"{', '.join(map(str, accepted))} (GitHub accepted each DELETE; the "
                            f"listing read-back failed: {error})")
        else:
            failures.append(f"could not confirm the deletion: {error}")
        return done, failures, refused, bool(accepted)
    left = {hook["id"] for hook in after}
    for hook in hooks:
        if hook["id"] not in left:
            done.append(f"hook {hook['id']} deleted")
    return done, failures, sorted(left), False


def _hermes_bin() -> str | None:
    """The ``hermes`` executable the scheduler commands run. ``DIAKTOROS_HERMES`` names a
    stand-in (the test suite's fake), so no test ever drives the operator's real install."""
    found = envnames.get("HERMES") or shutil.which("hermes")
    # Under the test guard (#101), never the operator's real hermes — for cron create and remove.
    return config.guard_real_hermes(found) if found else None


def _cron_jobs(loop: dict) -> tuple[list[dict] | None, str]:
    """The scheduler watchdog job(s) for this install, read from the job store.

    The shared job (``SHARED_JOB_NAME``) is the one ``init`` creates; a legacy per-loop job
    (pre-#60) is returned too so it can be recognised and migrated.
    """
    path = doctor.cron_store()
    if not path.exists():
        return [], ""
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return None, f"{path} is not readable JSON ({exc})"
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None, f"{path} has no job list"
    wanted = {*SHARED_JOB_NAMES, _legacy_job_name(loop)}
    return [job for job in jobs if isinstance(job, dict)
            and str(job.get("name") or "").strip() in wanted], ""


def _shared_job_present() -> bool:
    """Is the one shared watchdog job already scheduled? (idempotent init.)"""
    jobs, _ = _cron_jobs({"id": ""})
    if jobs is None:
        return False
    return any(str(job.get("name") or "").strip() in SHARED_JOB_NAMES for job in jobs)


def _other_loops(loop: dict) -> list[str]:
    """Loop ids configured besides ``loop`` — the shared job must outlive any one loop."""
    directory = config.config_dir()
    if not directory.exists():
        return []
    return [path.stem for path in sorted(directory.glob("*.json"))
            if path.stem != loop.get("id")]


def _remove_cron(loop: dict) -> tuple[list[str], list[str]]:
    """Remove the watchdog job through the scheduler's own CLI, then read the store back.

    The shared job is removed only with the **last** loop (#60): any other loop still needs it.
    A legacy per-loop job belonging to this loop is always removed — it is this loop's own.
    """
    jobs, error = _cron_jobs(loop)
    if jobs is None:
        return [], [f"cron: {error}"]
    others = _other_loops(loop)
    shared = [job for job in jobs
              if str(job.get("name") or "").strip() in SHARED_JOB_NAMES]
    legacy = [job for job in jobs
              if str(job.get("name") or "").strip() == _legacy_job_name(loop)]
    removing = legacy + (shared if not others else [])
    keeping = [job for job in jobs if job not in removing]
    hermes = _hermes_bin()
    done, failures = [], []
    for job in removing:
        job_id = str(job.get("id") or "")
        name = str(job.get("name") or "")
        if not job_id:
            failures.append(f"cron: job {name!r} has no id")
            continue
        if not hermes:
            failures.append(f"cron: no `hermes` on PATH to remove job {job_id}")
            continue
        try:
            proc = subprocess.run([hermes, "cron", "remove", job_id], capture_output=True,
                                  text=True, timeout=120)
        except Exception as exc:
            failures.append(f"cron: removing job {job_id} failed ({exc})")
            continue
        if proc.returncode != 0:
            failures.append(f"cron: removing job {job_id} failed "
                            f"({(proc.stderr or proc.stdout).strip()[:200]})")
    after, error = _cron_jobs(loop)
    if after is None:
        return done, failures + [f"cron: could not confirm the removal: {error}"]
    left = {str(job.get("id") or "") for job in after}
    for job in removing:
        if str(job.get("id") or "") in left:
            if not failures:
                failures.append(f"cron: job {job.get('id')} is still scheduled")
        else:
            done.append(f"cron job removed: {job.get('id')} ({job.get('name')})")
    for job in keeping:
        done.append(f"cron job kept: {job.get('id')} ({job.get('name')}) — "
                    f"{len(others)} other loop(s) still sweep through it")
    return done, failures


def _remove_unused_shim() -> str:
    """The cron shim is shared by every loop's job: remove it only when no job runs it any more."""
    shim = config.watchdog_shim()
    if not shim.is_symlink() and not shim.is_file():
        return ""
    try:
        data = json.loads(doctor.cron_store().read_text()) if doctor.cron_store().exists() else []
    except Exception:
        return ""
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list) or any(
            isinstance(job, dict) and pathlib.Path(str(job.get("script") or "")).name == shim.name
            for job in jobs):
        return ""
    if shim.is_symlink():
        # Never followed or removed on its own authority — but named, like every other leftover.
        return (f"cron shim NOT removed: {shim} is a symlink (never followed) — no job runs it; "
                f"remove the link itself: rm -- {shlex.quote(str(shim))}")
    try:
        shim.unlink()
    except OSError as exc:
        return (f"cron shim NOT removed: {shim} ({exc.strerror or exc}) — no job runs it; remove "
                f"it by hand: rm -- {shlex.quote(str(shim))}")
    return f"cron shim removed: {shim} (no job runs it any more)"


def _purge_target(loop: dict) -> tuple[pathlib.Path | None, str]:
    """The state directory ``uninstall --purge`` may delete, or ``(None, why not)``.

    Only the default ``<hermes home>/state/diaktoros/<id>`` (``review-loops/<id>`` before the
    rename) is ever removed: a custom
    ``state_dir`` could be anything the operator typed, and a recursive delete is not the place
    to find out. No symlink anywhere below the Hermes home is followed.
    """
    base = config.home()
    default = config.default_state_dir(loop["id"])
    raw = pathlib.Path(str(loop.get("state_dir") or "")).expanduser()
    lid = shlex.quote(loop["id"])
    if os.path.normpath(str(raw)) != os.path.normpath(str(default)):
        return None, (f"state_dir {raw} is not the default {default}, so --purge will not delete "
                      "it: a custom directory could hold anything the operator pointed it at. "
                      "Check it holds only this loop's state, then run:\n"
                      f"  hermes dk uninstall --loop {lid} && "
                      f"rm -rf -- {shlex.quote(str(raw))}")
    for path in (base / "state", default.parent, default):
        if path.is_symlink():
            return None, (f"{path} is a symlink; --purge never follows one — it may point at "
                          "this loop's own (moved) state or somewhere else entirely, and this "
                          "command cannot tell which. Check where it points, uninstall without "
                          "--purge, remove the link, and remove the directory it points at by "
                          "hand if it is this loop's:\n"
                          f"  ls -ld -- {shlex.quote(str(path))}   # where it points\n"
                          f"  hermes dk uninstall --loop {lid} && "
                          f"rm -- {shlex.quote(str(path))}   # the link itself")
    if default.exists() and not default.is_dir():
        return None, f"{default} is not a directory"
    return default, ""


def _write_watchdog_shim() -> pathlib.Path:
    """The cron shim the watchdog job runs by name, pinned to this plugin's watchdog."""
    shim = config.watchdog_shim()
    shim.parent.mkdir(parents=True, exist_ok=True)
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    shim.write_text(SHIM.format(watchdog=watchdog))
    shim.chmod(0o755)
    return shim


def _remove_legacy_jobs(loop: dict) -> list[str]:
    """Remove this loop's pre-#60 per-loop watchdog job, if one exists.

    Called when ``init`` creates (or finds) the shared job: without it the install sits at
    two sweepers — the shared job and the stale per-loop one — until an operator removes the
    stale job by hand. The shared job is what sweeps every loop; the legacy job is this
    loop's own and is safe to remove here.
    """
    jobs, error = _cron_jobs(loop)
    if jobs is None:
        return [f"cron: could not check for a legacy job ({error})"]
    legacy = [job for job in jobs
              if str(job.get("name") or "").strip() == _legacy_job_name(loop)]
    if not legacy:
        return []
    hermes = _hermes_bin()
    if not hermes:
        return ["cron: no `hermes` on PATH to remove the legacy job"]
    lines = []
    for job in legacy:
        job_id = str(job.get("id") or "")
        name = str(job.get("name") or "")
        if not job_id:
            lines.append(f"cron: legacy job {name!r} has no id; remove it by hand")
            continue
        try:
            proc = subprocess.run([hermes, "cron", "remove", job_id],
                                  capture_output=True, text=True, timeout=120)
        except Exception as exc:
            lines.append(f"cron: removing legacy job {job_id} failed ({exc})")
            continue
        if proc.returncode != 0:
            lines.append(f"cron: removing legacy job {job_id} failed "
                         f"({(proc.stderr or proc.stdout).strip()[:200]})")
        else:
            lines.append(f"removed the pre-#60 per-loop job: {job_id} ({name})")
    return lines


def _install_schedule(loop: dict, schedule: str, deliver: str) -> tuple[list[str], bool]:
    """A cron shim plus the one shared job, through the scheduler's own CLI.

    The job is created **only if missing** (#60): one shared job sweeps every loop, so a second
    ``init`` for another loop must not add a duplicate that would sweep them all again. Returns
    ``(lines, ok)``; ``ok`` is false only when no job exists and one could not be created. The
    fallback command is shell-quoted — the job name has spaces — so it can be pasted as printed.
    """
    shim = _write_watchdog_shim()
    if _shared_job_present():
        lines = [f"watchdog already scheduled (shared job, deliver={deliver})", f"shim: {shim}"]
        lines.extend(_remove_legacy_jobs(loop))
        return lines, True
    hermes = _hermes_bin() or "hermes"
    cmd = [hermes, "cron", "create", schedule, "--name", config.watchdog_job_name(),
           "--no-agent", "--script", shim.name, "--deliver", deliver]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return [f"could not create the cron job: {exc}",
                f"run it yourself: {shlex.join(cmd)}"], False
    if proc.returncode != 0:
        return [f"cron create failed: {(proc.stderr or proc.stdout).strip()[:200]}",
                f"run it yourself: {shlex.join(cmd)}"], False
    lines = [f"scheduled the watchdog ({schedule}, deliver={deliver})", f"shim: {shim}"]
    # Only now — after the shared job is confirmed present or just created — remove this
    # loop's legacy job. Removing it earlier would leave no sweeper if creation failed.
    lines.extend(_remove_legacy_jobs(loop))
    return lines, True


def _install_shims(loop: dict, report: bool = True, pairs=None) -> bool:
    """Write the loop's gate shims where the gateway resolves route scripts (issue #105)."""
    try:
        for line in gate_shims.install(loop, report=report, pairs=pairs):
            print(f"  {line}")
        return True
    except (OSError, config.ConfigError) as exc:
        print(f"gate shim install FAILED: {exc}")
        print(f"  fix it, then: hermes dk apply --loop {loop['id']}")
        return False


def _recreate_routes(loop: dict, missing: dict, *, dry_run: bool,
                     token_login: str | None = None) -> tuple[int, int]:
    """``apply --recreate-routes``: write routes the registry lost when no intent record can
    restore them. The old secret is gone with the route, so each gets a new one and the repo hook
    that points at the route's URL is re-keyed to it — a route whose hook still signs with the old
    secret would reject every delivery. Refuses, writing nothing, if the hooks cannot be read.

    Hooks are matched by route name, as ``_hook_moves`` does, and one is kept by ``_keep_one``
    (active first, then already at the route's URL, then lowest id): it is re-keyed, and moved to
    the route's URL if it was left at the loop's previous one. Any further hook for the route is
    never duplicated onto its URL — it is named with the command that deletes it. Returns
    ``(rc, redundant hooks named)``."""
    roles = set(missing)
    names = ", ".join(sorted(missing.values()))
    try:
        config.verify_seats(loop, roles & set(config.SEAT_KEYS))
        _verify_routes(loop, roles)
        hooks = _hook_listing(loop, token_login)
    except config.ConfigError as exc:
        print(f"refused: cannot recreate {names}: {exc}")
        if isinstance(exc, HookAccessError):
            print(f"  fix: {_hook_fix(loop, exc, token_login)}")
        return 2, 0
    urls = {name: routes.url_for_profile(name, config.seat_profile(loop, role), loop.get("host"))
            for role, name in missing.items()}
    rekey, redundant = [], []
    for name, url in urls.items():
        mine = [hook for hook in hooks if routes.route_name_of(hook["config"]["url"]) == name]
        if url and mine:
            keep, rest = _keep_one(mine, url)
            rekey.append((keep, name))
            redundant += [(hook, keep) for hook in rest]
    for name in sorted(missing.values()):
        plan = [(f"re-keys hook {hook['id']}" if routes.serves_route_url(hook["config"]["url"], urls[name])
                 else f"moves hook {hook['id']} from {hook['config']['url']} and re-keys it")
                for hook, hooked in rekey if hooked == name]
        print(f"  route {name}: {'would recreate' if dry_run else 'recreating'} from the loop "
              f"config (new secret)" + (f"; {plan[0]}" if plan else "; no repo hook points at it"))
    for hook, keep in redundant:
        print(f"  {_redundant_hook_line(loop, hook, keep)}")
    if dry_run:
        return 0, len(redundant)
    written: list[str] = []
    try:
        written = list(_install_routes(loop, roles=tuple(roles)).values())
        for hook, name in rekey:
            # A hook that already reaches the route keeps its own URL (a query string, say);
            # one left elsewhere is moved to the route's URL.
            url = (hook["config"]["url"] if routes.serves_route_url(hook["config"]["url"], urls[name])
                   else urls[name])
            _patch_hook_url(loop, hook["id"], url, token_login, require_secret=True,
                            insecure_ssl=hook["config"].get("insecure_ssl"))
            print(f"  hook {hook['id']} re-keyed for {name}")
        route_intent.record_live(loop, written)
    except Exception as exc:
        try:
            routes.restore_entries({name: None for name in written})
            route_intent.forget(loop, written)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect routes {names} manually")
        print(f"route recreation FAILED: {exc}; the recreated routes were taken back out — a hook "
              "already re-keyed now signs with a secret no route holds: re-run this command")
        print(f"  fix: {_hook_fix(loop, exc, token_login)}")
        return 2, 0
    for name in written:
        print(f"  route {name} recreated")
    return (0 if _install_shims(loop, report=False) else 2), len(redundant)


def _diverged(loop: dict, *, rewritten=(), header: str = "") -> bool:
    """Say so when a route still is not what the config installs, even after a push: never
    report success over a route the gateway runs under another profile or gate.

    ``rewritten`` names the routes this apply is about to write from the config: a dry run passes
    them, so it prints exactly what the real apply will still find afterwards."""
    left = {name: value for name, value in gate_shims.divergence(loop, contract=True).items()
            if name not in set(rewritten)}
    if left and header:
        print(header)
    for name, (detail, fix, _status) in left.items():
        print(f"  ⚠️ route {name}: {detail} — fix: {fix}")
    return bool(left)


# -- verbs ----------------------------------------------------------------------


def _write_config(loop: dict, *, policy_change: bool = False) -> pathlib.Path:
    with config.push_policy_lock():
        return _write_config_locked(loop, policy_change=policy_change)


def _write_config_locked(loop: dict, *, policy_change: bool = False) -> pathlib.Path:
    directory = config.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{loop['id']}.json"
    if path.is_symlink():
        raise config.ConfigError('symlinked loop config refused')
    if path.exists() and not policy_change:
        # Set/apply snapshots never own this switch. Re-read under the same lock
        # used by explicit enable/disable and by the broker's ref operation.
        try:
            current = config.load_id(loop['id'])
        except config.ConfigError:
            # `set --read-token` repairing a file whose only defect is its missing reader.
            current = config.load_id_for_reader_repair(loop['id'],
                                                       str(loop.get('read_token') or ''))
            if current is None:
                raise
        if current['repo'] != loop['repo']:
            raise config.ConfigError('repository changed during config update')
        loop = {**loop, 'unattended_fixer_push': current['unattended_fixer_push']}
    payload = json.dumps({k: v for k, v in loop.items() if v not in ({}, [], "")},
                         indent=2, sort_keys=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{loop['id']}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    return path

def _write_moved_repo(loop: dict, new: str) -> pathlib.Path:
    """Rename a loop's repository for ``migrate`` (#425), the one writer allowed to.

    ``_write_config`` refuses any repository change so a set/apply snapshot can never carry a loop
    (and its push policy) to another repository. ``migrate`` has verified that ``new`` is the
    *same* repository by id, so here the loop is re-read under the policy lock, must still name
    the repository it was read with, and only ``repo`` changes; the push policy is the current one.
    """
    with config.push_policy_lock():
        current = config.load_id(loop["id"])
        if current["repo"] != loop["repo"]:
            raise config.ConfigError("repository changed during migrate")
        return _write_config_locked({**current, "repo": new}, policy_change=True)


def _restore_config(path: pathlib.Path, data: bytes) -> None:
    """Publish a previous snapshot without exposing partially restored JSON."""
    with config.push_policy_lock():
        if path.is_symlink():
            raise config.ConfigError('symlinked loop config refused')
        if path.exists():
            current = config.load_id(path.stem)
            snapshot = json.loads(data)
            if snapshot.get('repo', '').lower() != current['repo']:
                raise config.ConfigError('repository changed during config rollback')
            if snapshot.get('unattended_fixer_push', False) != current['unattended_fixer_push']:
                snapshot['unattended_fixer_push'] = current['unattended_fixer_push']
                data = json.dumps(snapshot, indent=2, sort_keys=True).encode()
        _restore_config_locked(path, data)


def _restore_config_locked(path: pathlib.Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def _stale_hooks_refusal(loop: dict, admin: str | None) -> str:
    """Why ``init --hooks`` must not create hooks yet, or ``""`` when the routes have none.

    Only hooks at this loop's route URLs collide; the same route name at another origin, profile
    or path is printed as information (the gateway never delivers it to these routes).

    Refuse, never adopt. Adopting would mean PATCHing a new secret onto hooks this install did not
    create: GitHub never returns a hook's secret, so nothing can prove whose they are or which of
    several is current, and an adopted hook keeps its old ``active`` state — an armed leftover
    would arm a loop that has not passed doctor/selftest. Refusing writes nothing, and the fix is
    one pasteable command per hook (or ``uninstall`` for a loop that is still configured).
    """
    hooks, foreign, error = _classify_hooks(loop, admin)
    if hooks is None:
        return (f"refused: cannot read {loop['repo']}'s hooks to check for a previous install's "
                f"({error}); nothing written. Give the --admin-token login hook access "
                "(`admin:repo_hook`, or classic `repo`) and re-run")
    for line in _foreign_lines(loop, foreign):
        print(f"  info: {line}")
    if not hooks:
        return ""
    ids = sorted(hook["id"] for hook in hooks)
    lines = [f"refused: {loop['repo']} already has repo hook(s) posting to this loop's routes "
             f"({', '.join(sorted({_hook_route_name(h) for h in hooks}))}): "
             f"{', '.join(str(i) for i in ids)} — "
             f"{sum(1 for h in hooks if h.get('active'))} active. They were created by a previous "
             "install and sign with its secret, which the routes this init writes will not hold.",
             "nothing written. Delete them (a token with `admin:repo_hook` or classic `repo`), "
             "then re-run this init:"]
    lines += [f"  {command}" for command in _hook_delete_commands(loop, ids)]
    return "\n".join(lines)


def _init_loop_concurrency(args, d: dict) -> int:
    return args.concurrency if args.concurrency is not None else config.settings_loop_concurrency(d)


def _init_seat_concurrency(args, d: dict) -> dict:
    """The ``seats.<seat>.concurrency`` values ``init`` writes — only the ones that were asked for.

    A seat value wins over the loop default for good, so writing one nobody asked for pins that
    seat and makes ``--concurrency`` (and every later ``set --concurrency``) dead — issue #76.
    A per-seat flag is always written. Without one, a seat takes the settings form's own value
    only when the operator did not name a loop default *and* the form's seat differs from the one
    the form implies (reviewer 2, fixer 1): an explicit flag beats a form default, as everywhere.
    """
    loop_value = _init_loop_concurrency(args, d)
    out = {}
    for seat in ("reviewer", "fixer"):
        flag = getattr(args, f"{seat}_concurrency", None)
        if flag is not None:
            out[seat] = flag
        elif args.concurrency is None and d[f"{seat}_concurrency"] != loop_value:
            out[seat] = d[f"{seat}_concurrency"]
    return out


def _parallel_lines(loop: dict) -> list[str]:
    """What ``init`` and ``set`` say about capacity: a note per pinned seat, then the effective
    limits. A loop default that a seat overrides is exactly the setting someone changes twice and
    wonders why nothing moved, so the override is said out loud."""
    lines = [f"note: {seat} has its own concurrency ({loop['seats'][seat]['concurrency']}) — "
             "the loop default does not apply to it"
             for seat in ("reviewer", "fixer")
             if (loop["seats"].get(seat) or {}).get("concurrency") is not None]
    lines.append("parallel now: " + " · ".join(
        f"{seat} {config.seat_concurrency(loop, seat)}" for seat in ("reviewer", "fixer"))
        + "   (1 = serialized; everything above the limit queues)")
    return lines


def _pinned_seat_notes(loop: dict) -> list[str]:
    """Seats held at 1 by their own value while the loop default asks for more.

    ``init`` before #76 wrote ``seats.<seat>.concurrency: 1`` into every loop whether or not it
    was asked for, so this is the shape such a loop has after someone raised ``concurrency``. It
    is also exactly what an explicit ``--fixer-concurrency 1`` looks like, and the two cannot be
    told apart from the file — so the value is kept, and the `set` that raises it is named.
    """
    notes = []
    for seat in ("reviewer", "fixer"):
        own = (loop["seats"].get(seat) or {}).get("concurrency")
        if own == 1 and loop.get("concurrency", 1) > 1:
            notes.append(f"{seat} is pinned at 1 by seats.{seat}.concurrency (loop default "
                         f"{loop['concurrency']}) — to raise it: `hermes dk set "
                         f"--loop {loop['id']} --{seat}-concurrency {loop['concurrency']}`")
    return notes


def cmd_init(args) -> int:
    """Install a loop: write its config, its routes, and (on request) its hooks and cron job.

    The settings form supplies *who serves each seat* the same way it supplies the numeric knobs —
    as a default a new loop starts from — while explicit flags still win, because an operator
    installing a loop from the CLI on this machine should not have to depend on what a per-profile
    form happens to hold. Everything is validated (profiles, allowlists, credentials, route
    ownership) before the first file is written.
    """
    if getattr(args, "arm", False) and not args.hooks:
        print("--arm arms the repo hooks init creates; it needs --hooks")
        return 2
    loop_id = args.id or args.repo.split("/")[-1]
    # The rule the loader enforces: an id that does not name exactly one config file would be
    # written here and then break every loop-wide command that reads the directory.
    if (not loop_id or loop_id in (".", "..") or pathlib.Path(loop_id).name != loop_id
            or "\\" in loop_id or loop_id.startswith(".")):
        print(f"--id {loop_id!r} cannot name a loop config file: use letters, digits, '-', '_' "
              "or '.' (not leading, not a path)")
        return 2
    d = config.settings_defaults(_SETTINGS)
    tokens = {}
    for pair in args.token or []:
        if "=" not in pair:
            print(f"--token expects login=/path/to/pat, got {pair!r}")
            return 2
        login, path = pair.split("=", 1)
        try:
            config.check_token_file(path, f"--token {login}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        tokens[login] = path

    # Token-file paths from the settings form are checked before anything else is: a path that is
    # relative, missing, not yours or group/world readable is refused by name, and never opened.
    try:
        config.verify_token_settings(_SETTINGS)
    except config.ConfigError as exc:
        print(f"config refused: {exc}")
        return 2

    reviewer_profile = args.reviewer_profile or d["reviewer_profile"]
    fixer_profile = args.fixer_profile or d["fixer_profile"]
    fixers = [login for login in (args.fixer or []) if login] or (
        [d["fixer_login"]] if d["fixer_login"] else [])
    reviewers = [login for login in (args.reviewer or []) if login] or (
        [d["reviewer_login"]] if d["reviewer_login"] else [])
    if not fixers or not reviewers:
        print("--fixer and --reviewer name the GitHub logins this loop trusts (repeatable); with "
              "neither the flag nor the plugin settings naming them, there is no loop to install")
        return 2
    if not reviewer_profile or not fixer_profile:
        print("both seats need a Hermes profile: pass --reviewer-profile/--fixer-profile, or set "
              "them in the plugin settings (Capabilities → Plugins → diaktoros)")
        return 2
    configured_reviewer = d["reviewer_login"]
    reviewer_seat = args.reviewer_seat or (
        configured_reviewer if configured_reviewer in reviewers else "") or (
        reviewers[0] if len(reviewers) == 1 else "")
    if not reviewer_seat:
        print("with several reviewer logins, --reviewer-seat names which one this loop's route serves")
        return 2
    configured_fixer = d["fixer_login"]
    if configured_fixer and configured_fixer.lower() not in {login.lower() for login in fixers}:
        print(f"configured fixer login {configured_fixer!r} is not in the --fixer allowlist — "
              "name an eligible fixer in the plugin settings before installing this loop")
        return 2
    fixer_seat = next((login for login in fixers
                       if login.lower() == configured_fixer.lower()), fixers[0]) if configured_fixer else fixers[0]
    adjudicator_profile = args.adjudicator_profile or d["adjudicator_profile"] or "default"
    # An explicit --token for a login wins over the form's path for it, as every explicit flag does.
    mapped = {login.lower() for login in tokens}
    for seat_login_, key in ((reviewer_seat, "reviewer_token_file"), (fixer_seat, "fixer_token_file")):
        if d[key] and seat_login_ and seat_login_.lower() not in mapped:
            tokens[seat_login_] = str(pathlib.Path(d[key]).expanduser())
    explicit_adj = getattr(args, "adjudicator_login", None)
    adjudicator_login = (explicit_adj if explicit_adj is not None
                         else (d["adjudicator_login"] if args.adjudicator_route else "")).strip()
    if adjudicator_login and not args.adjudicator_route:
        print("--adjudicator-login needs --adjudicator-route: the comment identity only ever posts "
              "a ruling, and without a breach route nothing rules")
        return 2
    if (adjudicator_login and d["adjudicator_token_file"]
            and adjudicator_login.lower() not in mapped
            and (not d["adjudicator_login"]
                 or d["adjudicator_login"].lower() == adjudicator_login.lower())):
        tokens[adjudicator_login] = str(pathlib.Path(d["adjudicator_token_file"]).expanduser())

    raw = {
        "id": args.id or args.repo.split("/")[-1],
        "repo": args.repo, "base": args.base, "cap": args.cap,
        "concurrency": _init_loop_concurrency(args, d),
        "fixers": fixers, "reviewers": reviewers,
        "reviewer_seat": reviewer_seat,
        "seats": {
            "reviewer": {"profile": reviewer_profile, "route": "",
                         "login": reviewer_seat,
                         "agent": args.reviewer_agent},
            "fixer": {"profile": fixer_profile, "route": "",
                      "login": fixer_seat, "agent": args.fixer_agent},
            **({"adjudicator": {"login": adjudicator_login}} if adjudicator_login else {}),
        },
        "adjudicator": ({"route": args.adjudicator_route, "profile": adjudicator_profile}
                        if args.adjudicator_route else {}),
        "skill": args.skill,
        "tokens": tokens, "read_token": args.read_token,
        "clone": args.clone, "roots": args.root or [],
        "state_dir": args.state_dir or str(config.default_state_dir(
            args.id or args.repo.split("/")[-1])),
        "host": args.host, "grace_min": args.grace_min,
        "ttl_min": args.ttl_min, "inflight_ttl_min": args.inflight_ttl_min,
        "turn_budget_s": getattr(args, "turn_budget", None),
        "observer": _observer_args(args, args.id or args.repo.split("/")[-1]),
        # Sign what the loop posts (#197): on unless --attribution off or the form says so.
        "attribution": _attribution_arg(args, d["attribution"]),
        "review_after_ci": _on_off(args, "review_after_ci", d["review_after_ci"]),
        "fix_ci": _on_off(args, "fix_ci", d["fix_ci"]),
        # Host merge pushes to a review-only author's branch: off unless asked for and acknowledged.
        "review_only_update": _on_off(args, "review_only_update", d["review_only_update"]),
        # The checks CI always runs, which a fixer running only its touched tests would miss.
        "fixer_check": (d["fixer_check"] if getattr(args, "fixer_check", None) is None
                        else args.fixer_check),
        # The checks that gate an approval (#368); none named = every check gates.
        "required_checks": (config.split_check_names(d["required_checks"])
                            if getattr(args, "required_check", None) is None
                            else [name for name in args.required_check if name.strip()]),
        # Paths only a human may approve (#478).
        "human_paths": (config.split_human_paths(d["human_paths"])
                        if getattr(args, "human_path", None) is None
                        else [name for name in args.human_path if name.strip()]),
        # Authors reviewed but never fixed (#191).
        "review_only": ([name.strip() for name in str(d["review_only"]).split(",") if name.strip()]
                        if getattr(args, "review_only", None) is None
                        else [name for name in args.review_only if name.strip()]),
    }
    if raw["review_only_update"] and not getattr(args, "acknowledge_branch_push", False):
        print(BRANCH_PUSH_REFUSAL)
        return 2
    # #539: CI-fix turns per PR; a flag wins over the form; empty = the default.
    try:
        raw["ci_fix_cap"] = config.check_ci_fix_cap(
            getattr(args, "ci_fix_cap", None) if getattr(args, "ci_fix_cap", None) is not None
            else d["ci_fix_cap"], "init")
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    # The review-only verdict cap and daily cap: a flag wins over the form; empty = unset.
    for key in ("review_only_cap", "review_only_daily"):
        flag = getattr(args, key, None)
        try:
            raw[key] = config._check_review_only_limit(
                flag if flag is not None else d[key], key, "init")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
    # A seat-level capacity wins over the loop default, so only write it when it was asked for.
    for seat, value in _init_seat_concurrency(args, d).items():
        raw["seats"][seat]["concurrency"] = value
    for seat, value in (("reviewer", getattr(args, "reviewer_turn_budget", None)),
                         ("fixer", getattr(args, "fixer_turn_budget", None))):
        if value is not None:
            raw["seats"][seat]["turn_budget_s"] = value
    # Turn knobs (#323): a flag wins over the form; 0 or blank is the role default (nothing written).
    try:
        for seat in ("reviewer", "fixer"):
            flag = getattr(args, f"{seat}_max_steps", None)
            steps = flag if flag is not None else config._form_int(d[f"{seat}_max_steps"].strip())
            if steps not in (0, ""):
                raw["seats"][seat]["max_steps"] = config._check_max_steps(
                    steps, f"seats.{seat}.max_steps", "init")
        flag = getattr(args, "fix_daily_turns", None)
        cap = flag if flag is not None else config._form_int(d["fix_daily_turns"].strip())
        if cap not in (0, ""):
            config._check_daily_turns(cap, "triage.fix_daily_turns", "init")
            # A new loop has no triage block (so no fix_label) for the cap to live in, and
            # `apply` refuses it for the same reason: refuse rather than accept and drop it.
            print(f"refused: the issue-fix daily cap {cap} cannot be set by init: a new loop "
                  "has no triage.fix_label for it to live in. Leave --fix-daily-turns (and the "
                  "fix_daily_turns setting) blank, then set it with `hermes dk triage "
                  "--fix-label LABEL --maintainer LOGIN --fix-daily-turns N`; nothing written")
            return 2
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    names = routes_for(raw)
    raw["seats"]["reviewer"]["route"] = names["reviewer"]
    raw["seats"]["fixer"]["route"] = names["fixer"]
    roles = {"reviewer", "fixer"} | ({"adjudicator"} if raw["adjudicator"] else set())
    if raw["observer"].get("route"):
        roles.add("observer")
        if raw["observer"].get("urgent_route"):
            roles.add("observer_urgent")
    try:
        # The reader is named, never inferred: a default seat login (or the first token) is the
        # one-account-two-hats shape the broker refuses at the first write.
        if not args.read_token:
            raise config.ConfigError(
                "--read-token LOGIN names the account the gates read GitHub as (map its file with "
                f"--token LOGIN=/path/to/pat) — {config.FOUR_IDENTITY_RULE}")
        loop = config.normalize(raw)
        # One repo, one loop: a second id for it makes by_repo raise in every gate.
        others = config.loop_ids_for_repo(loop["repo"], exclude=loop["id"])
        if others:
            raise config.ConfigError(
                f"{loop['repo']} is already configured as loop {', '.join(repr(i) for i in others)}"
                " — change it with `hermes dk set`, or remove it first")
        # Routes are installed even without --hooks; never write a partial loop with
        # route URLs that cannot resolve to this operator's own gateway.
        config.webhook_host(loop["host"], required=True)
        # Who does what, and may they: profiles, allowlists, distinct credentials and route
        # ownership are all checked before a single file is written.
        # The adjudicator's token file is checked first, so a relative, missing or shared-readable
        # path is refused by its own name rather than as a generic missing file.
        config.verify_adjudicator_token(loop)
        config.verify_seats(loop, roles)
        # Hooks are created as --admin-token's login, after the config and routes are written;
        # an unmapped login would fail there and roll everything back, so refuse it up front.
        if args.hooks and args.admin_token and args.admin_token.lower() not in {
                str(k).lower() for k in loop.get("tokens") or {}}:
            raise config.ConfigError(
                f"--admin-token {args.admin_token!r} has no token file — add "
                f"--token {args.admin_token}=/path/to/pat (hook write access)")
        _verify_routes(loop, roles)
        _observer_check(loop)
        # Refuses a foreign file (#105). Routes init is about to write are not "missing".
        shim_lines = gate_shims.install(loop, dry_run=True, report=False) + [
            f"⚠️ route {name}: {detail}" for name, (detail, _fix, _status)
            in gate_shims.divergence(loop, include_missing=False).items()]
    except config.ConfigError as exc:
        print(f"config refused: {exc}")
        return 2

    if args.dry_run:
        print("dry run — nothing written: no loop config, no routes, no hooks, no cron job")
        print(f"  would write: {config.config_dir() / (loop['id'] + '.json')}")
        for line in _seat_lines(loop, "effective seat mapping"):
            print(f"  {line}")
        for line in _parallel_lines(loop):
            print(f"  {line}")
        print("  credentials: " + " · ".join(_credential_lines(loop)))
        for name in _routes_of(loop).values():
            print(f"  would write route: {name}")
        if args.hooks:
            print("  would create the two repo hooks (pull_request, pull_request_review), "
                  + ("armed (--arm)" if getattr(args, "arm", False) else "paused until `arm`"))
            print(f"  {_hook_editor_line(loop, args.admin_token, getattr(args, 'arm', False), True)}")
        if args.schedule:
            print(f"  would install the watchdog cron job ({args.schedule})")
        if loop.get("observer", {}).get("route"):
            print(f"  would write route: {loop['observer']['route']}")
        for line in shim_lines:
            print(f"  {line}")
        return 0

    path = config.config_dir() / f"{loop['id']}.json"
    if path.exists():
        print(f"refused: loop {loop['id']!r} already exists; use `hermes dk set` "
              "to change it without losing observer destination/receipt bindings")
        return 2
    observer_name = (loop.get("observer") or {}).get("route")
    if observer_name and routes.route(observer_name):
        print(f"refused: route {observer_name!r} already exists and is not this observer's route")
        return 2
    if args.hooks:
        # A previous install's hooks on these routes still sign with that install's secret, and
        # this init writes the routes with a fresh one: a second set would leave the old ones live
        # but unable to authenticate, next to new paused ones. Refused (not adopted) while nothing
        # is written yet; see _stale_hooks_refusal for why.
        refusal = _stale_hooks_refusal(loop, args.admin_token)
        if refusal:
            print(refusal)
            return 2
    previous_config = None
    previous_routes = {name: routes.route(name) for name in _routes_of(loop).values()}
    try:
        path = _write_config(loop)
    except Exception as exc:
        print(f"config install FAILED: {exc}; previous config unchanged")
        return 2
    print(f"loop config written: {path}")
    for line in _seat_lines(loop):
        print(line)
    for line in _parallel_lines(loop):
        print(f"  {line}")
    print("  credentials: " + " · ".join(_credential_lines(loop)))
    # A route that fails to land must not leave a loop installed with half its seats woken: the
    # config `init` just wrote and any route it just added are taken back out again, so a second
    # `init` starts from clean rather than from a loop nobody can see.
    try:
        written_routes = list(_install_routes(loop).values())
        # The plugin's private copy of what it just wrote (secret included) is part of the same
        # transaction: self-heal restores from it, so it must never describe routes that failed.
        route_intent.record_live(loop, written_routes, replace=True)
    except Exception as exc:
        try:
            routes.restore_entries(previous_routes)
            if previous_config is None:
                path.unlink(missing_ok=True)
            else:
                _restore_config(path, previous_config)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect config and routes manually")
            return 2
        print(f"route install FAILED: {exc}")
        print("  previous config and routes restored; fix the registry and re-run init")
        return 2
    for name in written_routes:
        print(f"route written: {name}")
    shims_ok = _install_shims(loop)
    try:
        hook_lines = (_install_hooks(loop, args.admin_token, active=bool(getattr(args, "arm", False)))
                      if args.hooks else [])
    except Exception as exc:
        failed = []
        try:
            routes.restore_entries(previous_routes)
        except Exception as rollback_exc:
            failed.append(f"routes: {rollback_exc}")
        try:
            route_intent.forget(loop, written_routes)
        except Exception as rollback_exc:
            failed.append(f"route intent: {rollback_exc}")
        try:
            if previous_config is None:
                path.unlink(missing_ok=True)
            else:
                _restore_config(path, previous_config)
        except Exception as rollback_exc:
            failed.append(f"config: {rollback_exc}")
        print(f"hook install FAILED: {exc}")
        if failed or "ROLLBACK FAILED" in str(exc):
            print("ROLLBACK FAILED — inspect before retrying: " + "; ".join(failed))
        else:
            print("  prior config and routes restored")
        return 2
    for line in hook_lines:
        print(f"  {line}")
    if args.hooks:
        print(f"  {_hook_editor_line(loop, args.admin_token, getattr(args, 'arm', False), False)}")
    if not args.hooks:
        print("  (repo hooks not created — pass --hooks, or add them by hand with the route URLs)")
    # Armed at birth: prove the secret now, as `arm` does, rather than at the first real event.
    pinged = (_ping_loop_hooks(loop, args.admin_token)
              if args.hooks and getattr(args, "arm", False) else True)
    schedule_lines, scheduled = (_install_schedule(loop, args.schedule, args.watchdog_deliver)
                                 if args.schedule else ([], True))
    for line in schedule_lines:
        print(f"  {line}")
    if not scheduled:
        # Config, routes and hooks are in place; only the job is missing. Say so and fail, so an
        # install script's `init && ...` does not read a missing watchdog as success.
        print("\ninit INCOMPLETE: the watchdog job was not scheduled — run the command above, "
              f"then `hermes dk doctor --loop {loop['id']}`")
        return 1
    if not pinged:
        print(f"\ninit INCOMPLETE: a hook's ping was rejected — see the ❌ line above, then "
              f"`hermes dk doctor --loop {loop['id']}`")
        return 1
    # The seats never hold a GitHub token: every write goes through the host broker with the token
    # files mapped above, so a GH_TOKEN in a seat profile's .env is only an extra copy to leak.
    lid = loop["id"]
    runtime = config.host_path("runtime")
    steps = ([f"create the runtime file {runtime} (`hermes dk setup --repo "
              f"{shlex.quote(loop['repo'])}` detects and writes it)"]
             if not runtime.exists() else []) + [
             f"hermes dk doctor --loop {lid}",
             f"hermes dk selftest --loop {lid} --no-model, then --pr N, then --pr N --live-turn"]
    if not config.unattended_fixer_push_enabled(loop):
        steps.append("decide the fix leg: unattended fixer pushes are off, so a changes-requested "
                     "verdict is held for you and no fixer turn starts. To let the fixer answer "
                     f"verdicts: {config.fixer_push_enable_command(loop)} "
                     "(read docs/operations.md on the PR-metadata race first)")
    if args.hooks and not getattr(args, "arm", False):
        admin = f" --admin-token {args.admin_token}" if args.admin_token else ""
        steps.append(f"hermes dk arm --loop {lid}{admin}   (the hooks were created "
                     "paused)")
    elif args.hooks:
        steps.append("the hooks are ARMED: until the runtime file exists every turn is held")
    print("\nNext:")
    for n, step in enumerate(steps, 1):
        print(f"  {n}. {step}")
    print("  Seat tokens live only in the token files mapped above; a seat profile needs no "
          "GH_TOKEN.")
    return 0 if shims_ok else 1


# The root ``hermes dk`` parser, kept by register_cli so ``setup`` runs the other verbs
# through the very parser (and settings defaults) the operator would.
_PARSER: argparse.ArgumentParser | None = None


def _verb(*argv: str) -> int:
    """Run another ``hermes dk`` verb in-process; argparse refusing it is exit 2."""
    try:
        parsed = _PARSER.parse_args(list(argv))
    except SystemExit:
        return 2
    return parsed.func(parsed)


def _ask(question: str, default: str, interactive: bool) -> str:
    """One answer: typed, or the default (always the default without a terminal)."""
    if not interactive:
        return default
    try:
        answer = input(f"   {question}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _agree(question: str, default: bool, interactive: bool, unattended: bool) -> bool:
    """A yes/no; without a terminal (``--yes``) the answer is ``unattended``."""
    if not interactive:
        return unattended
    try:
        answer = input(f"   {question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        answer = ""
    return default if not answer else answer in ("y", "yes")


def _setup_runtime(args, interactive: bool) -> bool:
    """Step 1: keep the runtime file's working paths, detect the rest, write it 0600."""
    from . import runtime_detect, seat_model
    file = runtime_detect.path()
    print(f"\n1. Runtime paths — {file}")
    current, why = runtime_detect.read()
    if why and why != "absent":
        print(f"   the current file cannot be used ({why}); it will be replaced")
    have = {key: current[key] for key in runtime_detect.HOST_KEYS
            if current and isinstance(current.get(key), str) and current[key]}
    broken = runtime_detect.problems(have) if have else {}
    detected = runtime_detect.detect()
    chosen, origin = {}, {}
    # The runtime is derived from the venv actually chosen, so it is resolved last — explicitly,
    # not by trusting HOST_KEYS to list "venv" before "runtime" (#242).
    order = [key for key in runtime_detect.HOST_KEYS if key != "runtime"] + ["runtime"]
    for key in order:
        if key == "runtime" and "venv" in chosen:
            # The runtime belongs to the venv actually chosen (given, kept or detected), never to
            # one detection found elsewhere.
            derived = runtime_detect.runtime_for(pathlib.Path(chosen["venv"]))
            detected = {**detected, "runtime": str(derived)} if derived else detected
        given = getattr(args, key, None)
        for source, value in (("given", given), ("kept", None if key in broken else have.get(key)),
                              ("detected", detected.get(key)), ("kept", have.get(key))):
            if value:
                chosen[key], origin[key] = str(pathlib.Path(value).expanduser()), source
                break
    left = runtime_detect.problems(chosen)
    for key in runtime_detect.HOST_KEYS:
        if key in left:
            print(f"   ❌ {key:<8} {left[key]} — pass --{key} PATH")
        else:
            print(f"   ✅ {key:<8} {chosen[key]} ({origin[key]})")
    if left:
        print("   not written: every path must check out first (the file is all or nothing)")
        return False
    settings = runtime_detect.merged(current, chosen)
    try:
        seat_model.parse_runtime(settings)
    except ValueError as exc:
        print(f"   ❌ the model overrides kept from the current file do not parse ({exc}); "
              f"fix {file} by hand")
        return False
    private = (current is not None and not file.is_symlink()
               and not file.stat().st_mode & 0o077)
    if current == settings and private:
        print("   in place — nothing to change")
        return True
    if args.dry_run:
        print(f"   would write {file} (0600)")
        return True
    if not _agree(f"Write {file.name}?", True, interactive, True):
        print("   not written")
        return False
    print(f"   written: {runtime_detect.write(settings)} (0600)")
    return True


def _setup_init_argv(args, repo: str, loop_id: str, interactive: bool) -> tuple[list[str], str]:
    """Step 2's answers as the ``init`` command line, and the hook admin login ("" for none)."""
    d = config.settings_defaults(_SETTINGS)
    ask = lambda flag, question, default="": flag or _ask(question, default, interactive)  # noqa: E731
    reviewer = ask(args.reviewer, "reviewer GitHub login (the account that reviews)",
                   d["reviewer_login"])
    fixer = ask(args.fixer, "fixer GitHub login (the account that pushes fixes)", d["fixer_login"])
    reviewer_profile = ask(args.reviewer_profile, "reviewer's Hermes profile", d["reviewer_profile"])
    fixer_profile = ask(args.fixer_profile, "fixer's Hermes profile", d["fixer_profile"])
    reviewer_file = ask(args.reviewer_token, f"token file for {reviewer or 'the reviewer'}",
                        d["reviewer_token_file"])
    fixer_file = ask(args.fixer_token, f"token file for {fixer or 'the fixer'}",
                     d["fixer_token_file"])
    reader = ask(args.read_token, "reader login (its own account; the gates read GitHub as it)")
    reader_file = ask(args.read_token_file, f"token file for {reader or 'the reader'}")
    host = ask(args.host, "your gateway's webhook origin (https://…)", d["host"])
    observer = (args.observer_profile if args.observer_profile is not None else
                _ask("Hermes profile whose chat gets the loop's notices (blank: none)", "",
                     interactive))
    after_ci = args.review_after_ci or (
        "on" if _agree("Start each review after the head's CI finishes (up to an hour)?",
                       d["review_after_ci"], interactive, d["review_after_ci"]) else "off")
    fix_ci = args.fix_ci or (
        "on" if _agree("Give a failed CI check on a fixer's PR to the fixer (needs fixer pushes)?",
                       d["fix_ci"], interactive, d["fix_ci"]) else "off")
    attribution = args.attribution or (
        "on" if _agree("Sign what the loop posts ('Automated by Diaktoros')?",
                       d["attribution"], interactive, d["attribution"]) else "off")
    adjudicator = (args.adjudicator_profile if args.adjudicator_profile is not None else
                   _ask("Adjudicate a PR whose rounds are spent? Profile [blank = no]",
                        d["adjudicator_profile"], interactive)).strip()
    check = (args.fixer_check if args.fixer_check is not None else
             _ask("a command the fixer always runs before publishing, the checks CI always runs "
                  "(blank: none)", d["fixer_check"], interactive))
    admin = (args.admin_token if args.admin_token is not None else
             _ask("hook admin login, to create the repo hooks (blank: add them yourself)", "",
                  interactive))
    admin_file = ""
    if admin and admin.lower() not in {x.lower() for x in (reviewer, fixer, reader) if x}:
        admin_file = ask(args.admin_token_file, f"token file for {admin}")
    argv = ["init", f"--repo={repo}", f"--id={loop_id}", f"--reviewer={reviewer}",
            f"--fixer={fixer}", f"--reviewer-profile={reviewer_profile}",
            f"--fixer-profile={fixer_profile}", f"--read-token={reader}", f"--host={host}",
            f"--attribution={attribution}", f"--review-after-ci={after_ci}", f"--fix-ci={fix_ci}"]
    for login, file in ((reviewer, reviewer_file), (fixer, fixer_file), (reader, reader_file),
                        (admin, admin_file)):
        if login and file:
            argv.append(f"--token={login}={pathlib.Path(file).expanduser()}")
    if observer:
        argv.append(f"--observer-profile={observer}")
    if adjudicator:
        argv += [f"--adjudicator-route={loop_id}-breach", f"--adjudicator-profile={adjudicator}"]
    argv.append(f"--fixer-check={check}")
    required = (args.required_check if args.required_check is not None else
                config.split_check_names(_ask(
                    "the CI checks that must pass before an approval, comma-separated, exactly as "
                    "GitHub names them (blank: every check)", d["required_checks"], interactive)))
    argv += [f"--required-check={name}" for name in required]
    humans = (args.human_path if getattr(args, "human_path", None) is not None else
              config.split_human_paths(_ask(
                  "glob patterns for paths only a human may approve, comma-separated "
                  "(blank: none)", d["human_paths"], interactive)))
    argv += [f"--human-path={name}" for name in humans]
    reviewed = (args.review_only if getattr(args, "review_only", None) is not None else
                [name for name in _ask("GitHub logins whose PRs the reviewer reviews but the fixer "
                                       "never touches, comma-separated (blank: none)",
                                       d["review_only"], interactive).split(",") if name.strip()])
    argv += [f"--review-only={name.strip()}" for name in reviewed]
    if getattr(args, "review_only_update", None) is not None:
        argv.append(f"--review-only-update={args.review_only_update}")
    if getattr(args, "acknowledge_branch_push", False):
        argv.append("--acknowledge-branch-push")
    for flag in ("reviewer_max_steps", "fixer_max_steps", "fix_daily_turns",
                 "review_only_cap", "review_only_daily", "ci_fix_cap"):
        if getattr(args, flag, None) is not None:
            argv.append(f"--{flag.replace('_', '-')}={getattr(args, flag)}")
    if admin:
        argv += ["--hooks", f"--admin-token={admin}"]   # created paused; step 5 arms them
    return argv, admin


def cmd_setup(args) -> int:
    """A first install in one command, safe to re-run (#215).

    1. the runtime file: working paths kept, the rest detected (``runtime_detect``), written 0600;
    2. the loop: ``init``'s answers asked (the settings form's values as defaults), its dry run
       shown and confirmed, then ``init`` itself — skipped when the loop already exists;
    3. the shared watchdog job (created only if missing);
    4. ``doctor`` and ``selftest --no-model``: any ❌ stops here, with its fix line above;
    5. ``arm``, only on a clean pass and only when asked.

    Every step runs the same code as its own verb, so ``setup`` adds no second path to keep right.
    """
    interactive = not args.yes
    if interactive and not sys.stdin.isatty():
        print("setup asks questions; with no terminal, pass --yes to take the flags and the plugin "
              "settings as the answers")
        return 2
    repo = args.repo or _ask("GitHub repository (owner/name)", "", interactive)
    if not repo or repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
        print("setup needs the repository as owner/name (--repo)")
        return 2
    loop_id = args.id or repo.split("/")[-1]
    print(f"hermes dk setup — {repo} (loop {loop_id!r}). Safe to re-run: what is already "
          "in place is kept." + (" Dry run: nothing is written." if args.dry_run else ""))

    runtime_ok = _setup_runtime(args, interactive)

    print("\n2. The loop")
    existing = (config.config_dir() / f"{loop_id}.json").exists()
    admin = args.admin_token or ""
    if existing:
        try:
            loop = config.load_id(loop_id)
        except config.ConfigError as exc:
            print(f"   ❌ loop {loop_id!r} exists but does not load: {exc}")
            return 2
        if loop["repo"].lower() != repo.lower():
            print(f"   ❌ loop {loop_id!r} serves {loop['repo']}, not {repo} — pass --id to name "
                  "a new loop")
            return 2
        print(f"   ✅ loop {loop_id!r} is configured — kept as it is (change it with `hermes "
              f"dk set --loop {loop_id}`)")
    else:
        argv, admin = _setup_init_argv(args, repo, loop_id, interactive)
        print("   init dry run:")
        if _verb(*argv, "--dry-run") != 0:
            print("\nsetup stopped: init refused the answers — fix what it names and re-run setup")
            return 2
        if args.dry_run:
            print("\n3. would schedule the shared watchdog job (if missing)\n"
                  "4. would run doctor and selftest --no-model\n5. would arm only when asked")
            return 0
        if not _agree("Install this loop?", True, interactive, True):
            print("setup stopped: nothing installed")
            return 1
        if _verb(*argv) == 2:
            print("\nsetup stopped: init failed — see above; re-running setup is safe")
            return 2

    print("\n3. The watchdog")
    schedule = args.schedule or _ask("how often the watchdog sweeps", "15m", interactive)
    deliver = args.watchdog_deliver or _ask("where its alerts go (local, telegram, …)", "local",
                                            interactive)
    if args.dry_run:
        print(f"   would schedule the shared watchdog job ({schedule}, deliver={deliver}) "
              "if it is missing")
        return 0
    lines, scheduled = _install_schedule(config.load_id(loop_id), schedule, deliver)
    for line in lines:
        print(f"   {'✅' if scheduled else '❌'} {line}")

    print("\n4. Checks")
    doctor_rc = _verb("doctor", f"--loop={loop_id}")
    selftest_rc = _verb("selftest", f"--loop={loop_id}", "--no-model")
    if doctor_rc or selftest_rc or not scheduled or not runtime_ok:
        print("\nsetup stopped before arming: fix each ❌ above (its fix line says how), then "
              f"re-run `hermes dk setup --repo {shlex.quote(repo)}` — it keeps what is done")
        return 1

    print("\n5. Arm")
    if not (args.arm or _agree("Arm the repo hooks now? The loop goes live.", False,
                               interactive, False)):
        print(f"   not armed. When ready: hermes dk arm --loop {loop_id}"
              + (f" --admin-token {admin}" if admin else ""))
        return 0
    if not admin:
        admin = _ask("hook admin login", "", interactive)
    rc = _verb("arm", f"--loop={loop_id}", *([f"--admin-token={admin}"] if admin else []))
    print("\nsetup complete: the loop is live" if rc == 0 else
          "\narm was not confirmed — see above; re-running setup is safe")
    return rc


def _set_adjudication(args, loop: dict) -> int:
    """``set --adjudicator-profile P`` / ``--adjudicator-route NAME`` / ``--adjudicator off``.

    On: the route is written the way ``init --adjudicator-route`` writes it (ownership check,
    intent record, shim) and the ``adjudicator`` block with it. Off: the route, its intent record
    and its shim go, and so does the block; the ledger's breach markers and rulings stay.
    A failed route write puts the config and the registry back.
    """
    path = config.config_dir() / f"{loop['id']}.json"
    current = dict(loop.get("adjudicator") or {})
    old_route = str(current.get("route") or "")
    if getattr(args, "adjudicator", None) == "off":
        if getattr(args, "adjudicator_profile", None) or getattr(args, "adjudicator_route", None):
            print("refused: --adjudicator off cannot be combined with --adjudicator-profile "
                  "or --adjudicator-route")
            return 2
        if not old_route:
            print(f"[{loop['id']}] adjudication already off")
            return 0
        seats = {seat: dict(cfg) for seat, cfg in loop["seats"].items()}
        adj_seat = seats.pop("adjudicator", None) or {}
        adj_seat.pop("login", None)
        if adj_seat:
            seats["adjudicator"] = adj_seat
        try:
            updated = config.normalize({**loop, "adjudicator": {}, "seats": seats})
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        previous_config = path.read_bytes()
        previous_route = routes.route(old_route)
        try:
            _write_config(updated)
            routes.remove_route(old_route)
            route_intent.forget(loop, [old_route])
        except Exception as exc:
            try:
                routes.restore_entries({old_route: previous_route})
                _restore_config(path, previous_config)
            except Exception as rollback_exc:
                print(f"ROLLBACK FAILED: {rollback_exc} — inspect {path} and route {old_route}")
                return 2
            print(f"adjudication off FAILED: {exc}; config and route restored")
            return 2
        shims = gate_shims.remove({**loop, "seats": {}, "triage": {}, "observer": {}},
                                  [updated, *[other for other in config.all_loops()
                                              if other["id"] != loop["id"]]])
        print(f"[{loop['id']}] adjudication off: route {old_route} removed — a spent cap now "
              "writes only the breach marker (existing markers and rulings stay in the ledger)")
        for line in shims:
            print(f"  {line}")
        return 0

    d = config.settings_defaults(_SETTINGS)
    profile = (str(getattr(args, "adjudicator_profile", None) or "").strip()
               or str(current.get("profile") or "") or d["adjudicator_profile"] or "default")
    route = (str(getattr(args, "adjudicator_route", None) or "").strip() or old_route
             or routes_for(loop)["adjudicator"])
    try:
        updated = config.normalize({**loop, "adjudicator": {**current, "route": route,
                                                            "profile": profile}})
        config.webhook_host(updated.get("host"), required=True)
        config.verify_seats(updated, {"adjudicator"})
        _verify_routes(updated, {"adjudicator"})
        gate_shims.install(updated, dry_run=True, report=False)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    if (updated.get("adjudicator") or {}) == current and routes.route(route):
        print(f"[{loop['id']}] adjudication already on (route {route}, profile {profile})")
        return 0
    previous_config = path.read_bytes()
    names = {route, *([old_route] if old_route else [])}
    previous_routes = {name: routes.route(name) for name in names}
    try:
        _write_config(updated)
        _install_routes(updated, roles=("adjudicator",))
        route_intent.record_live(updated, [route])
    except Exception as exc:
        try:
            routes.restore_entries(previous_routes)
            _restore_config(path, previous_config)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect {path} and route {route}")
            return 2
        print(f"adjudication install FAILED: {exc}; config and route restored")
        return 2
    print(f"[{loop['id']}] adjudication on: profile {profile}, route {route} written")
    if old_route and old_route != route:
        try:
            route_intent.forget(updated, [old_route])
            routes.remove_route(old_route)
            print(f"  old route {old_route} removed")
        except (OSError, ValueError) as exc:
            print(f"warning: old adjudicator route {old_route!r} remains; remove it manually: {exc}")
    shims_ok = _install_shims(updated)
    print("  no repo hook is needed: the loop wakes the adjudicator itself when the cap is spent")
    print(f"  next: hermes dk doctor --loop {loop['id']}")
    return 0 if shims_ok else 1


def cmd_set(args) -> int:
    """Change a loop's settings in place, through the same validation ``init`` uses.

    Seat prompts are rendered from the payload at fire time; observer destination changes also
    reconcile the delivery-only route before updating config. The rails still apply — ``concurrency``
    above 1 without a ``clone`` is refused here exactly as it is at init, because a parallel run
    that cannot be isolated would share a checkout.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        # The one repair a verb can make to a file the loader refuses: a missing reader, named
        # here with --read-token (and its --token). Any other defect still refuses.
        try:
            loop = config.load_id_for_reader_repair(args.loop,
                                                    str(getattr(args, "read_token", "") or "").strip())
        except config.ConfigError as other:
            exc = other
            loop = None
        if loop is None:
            print(f"no such loop: {exc}")
            return 2
        print(f"repairing {args.loop}: it has no read_token; setting the reader named by --read-token")

    adj_done = False
    if (getattr(args, "adjudicator_profile", None) or getattr(args, "adjudicator_route", None)
            or getattr(args, "adjudicator", None)):
        rc = _set_adjudication(args, loop)
        if rc:
            return rc
        adj_done = True
        loop = config.load_id(args.loop)

    host = args.host
    if host is not None:
        # A blank or whitespace host is not "unchanged": it would strip the loop of the origin
        # its hooks and routes are matched by. Say so, and validate a real one here, by name.
        host = host.strip()
        try:
            if not host:
                raise config.ConfigError("--host needs your gateway origin (https://…); a blank "
                                         "host would orphan the loop's hooks — to take the loop "
                                         "down use `uninstall`")
            config.webhook_host(host, required=True)
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
    wanted = {"concurrency": args.concurrency, "cap": args.cap, "base": args.base,
              "clone": args.clone, "grace_min": args.grace_min,
              "marker_grace_min": args.marker_grace_min, "ttl_min": args.ttl_min,
              "inflight_ttl_min": args.inflight_ttl_min, "host": host,
              "turn_budget_s": getattr(args, "turn_budget", None),
              "attribution": _attribution_arg(args, None),
              "review_after_ci": _on_off(args, "review_after_ci", None),
              "fix_ci": _on_off(args, "fix_ci", None),
              "review_only_update": _on_off(args, "review_only_update", None)}
    if (wanted["review_only_update"] and not loop.get("review_only_update")
            and not getattr(args, "acknowledge_branch_push", False)):
        print(BRANCH_PUSH_REFUSAL)
        return 2
    changes = {k: v for k, v in wanted.items()
               if v is not None and v != "" and v != loop.get(k)}
    if getattr(args, "required_check", None) is not None or getattr(args, "no_required_checks",
                                                                     False):
        try:
            names = config.check_required_checks(
                [] if args.no_required_checks else args.required_check, "--required-check")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if names != (loop.get("required_checks") or []):
            changes["required_checks"] = names
    if getattr(args, "human_path", None) is not None or getattr(args, "no_human_paths", False):
        try:
            names = config.check_human_paths(
                [] if args.no_human_paths else args.human_path, "--human-path")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if names != (loop.get("human_paths") or []):
            changes["human_paths"] = names
    if getattr(args, "review_only", None) is not None or getattr(args, "no_review_only", False):
        try:
            names = config.check_review_only([] if args.no_review_only else args.review_only,
                                             loop, "--review-only")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if names != (loop.get("review_only") or []):
            changes["review_only"] = names
    if getattr(args, "ci_fix_cap", None) is not None:
        try:
            value = (None if str(args.ci_fix_cap).strip() in ("0", "")
                     else config.check_ci_fix_cap(args.ci_fix_cap, "--ci-fix-cap"))
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if value != loop.get("ci_fix_cap"):
            changes["ci_fix_cap"] = value
    for key in ("review_only_cap", "review_only_daily"):
        wanted = getattr(args, key, None)
        if wanted is None:
            continue
        try:
            value = None if str(wanted).strip() in ("0", "") else config._check_review_only_limit(
                wanted, key, f"--{key.replace('_', '-')}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if value != loop.get(key):
            changes[key] = value
    # '' is a real value here: it clears the check.
    if getattr(args, "fixer_check", None) is not None:
        try:
            check = config.check_fixer_check(args.fixer_check, "--fixer-check")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if check != (loop.get("fixer_check") or ""):
            changes["fixer_check"] = check

    seats = {seat: dict(cfg) for seat, cfg in loop["seats"].items()}
    seat_changes = {}

    # The adjudicator's optional comment identity and the reader. `set` maps only *their*
    # credentials: the seats' token files move through the settings form and `apply`, which owns
    # the in-flight rules.
    tokens = dict(loop.get("tokens") or {})
    adj_before = config.adjudicator_login(loop)
    adj_wanted = getattr(args, "adjudicator_login", None)
    adj_after = adj_before if adj_wanted is None else adj_wanted.strip()
    read_before = str(loop.get("read_token") or "")
    read_wanted = getattr(args, "read_token", None)
    read_after = read_before if read_wanted is None else read_wanted.strip()
    if read_wanted is not None and not read_after:
        print("refused: --read-token needs a login — the gates cannot read GitHub as nobody")
        return 2
    for pair in getattr(args, "token", None) or []:
        if "=" not in pair:
            print(f"--token expects login=/path/to/pat, got {pair!r}")
            return 2
        login, path = pair.split("=", 1)
        login = login.strip()
        owner = next((name for name in (adj_after, read_after if read_wanted is not None else "")
                      if name and login.lower() == name.lower()), "")
        seat_logins = {config.seat_login(loop, seat).lower() for seat in config.SEAT_KEYS} - {""}
        if not owner and login.lower() in seat_logins | {str(loop.get("read_token") or "").lower()}:
            print(f"refused: `set --token` maps the token file of the login named by --read-token "
                  f"or --adjudicator-login, or of an extra login such as a hook admin "
                  f"(--admin-token); {login!r} is a seat or the current reader — seat token files "
                  "move through the plugin settings and `apply`, and a reader moves with "
                  "--read-token")
            return 2
        try:
            config.check_token_file(path, f"--token {login}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        for key in [k for k in tokens if str(k).lower() == login.lower()]:
            del tokens[key]
        tokens[owner or login] = str(pathlib.Path(path.strip()).expanduser())
    # gh looks a login's file up by its exact key: keep the reader spelled as its mapping is.
    read_after = next((str(k) for k in tokens if str(k).lower() == read_after.lower()), read_after)
    tokens_changed = tokens != (loop.get("tokens") or {})
    adj_changed = adj_after != adj_before or tokens_changed
    read_changed = (read_after != read_before
                    or _token_ref({"tokens": tokens}, read_after) != _token_ref(loop, read_after))
    if adj_after != adj_before:
        adj_seat = dict(seats.get("adjudicator") or {})
        if adj_after:
            if not (loop.get("adjudicator") or {}).get("route"):
                print("refused: this loop has no adjudicator route, so nothing rules — an "
                      "adjudicator login would never post; turn adjudication on first with "
                      f"`hermes dk set --loop {loop['id']} --adjudicator-profile PROFILE`")
                return 2
            adj_seat["login"] = adj_after
        else:
            adj_seat.pop("login", None)
        if adj_seat:
            seats["adjudicator"] = adj_seat
        else:
            seats.pop("adjudicator", None)
    for seat, value in (("reviewer", args.reviewer_concurrency), ("fixer", args.fixer_concurrency)):
        if value is not None and value != config.seat_concurrency(loop, seat):
            seats[seat]["concurrency"] = value
            seat_changes[seat] = value
    budget_changes = {}
    for seat, value in (("reviewer", getattr(args, "reviewer_turn_budget", None)),
                         ("fixer", getattr(args, "fixer_turn_budget", None))):
        if value is not None and value != (seats[seat].get("turn_budget_s")):
            budget_changes[seat] = (config.turn_budget(loop, seat), value)
            seats[seat]["turn_budget_s"] = value
    # A daily turn cap per seat (#219); 0 removes it. normalize refuses anything else.
    for seat, value in (("reviewer", getattr(args, "reviewer_daily_turns", None)),
                         ("fixer", getattr(args, "fixer_daily_turns", None))):
        if value is None or (value or None) == config.seat_daily_turns(loop, seat):
            continue
        budget_changes[f"{seat} daily turns"] = (config.seat_daily_turns(loop, seat), value or None)
        if value:
            seats[seat]["daily_turns"] = value
        else:
            seats[seat].pop("daily_turns", None)
    # Agent steps per turn (#271); 0 goes back to the seat's default. An issue fix takes the
    # fixer's value. normalize refuses anything outside the range.
    for seat, value in (("reviewer", getattr(args, "reviewer_max_steps", None)),
                         ("fixer", getattr(args, "fixer_max_steps", None))):
        if value is None or (value or config.DEFAULT_MAX_STEPS[seat]) == config.max_steps(loop, seat):
            continue
        budget_changes[f"{seat} max steps"] = (config.max_steps(loop, seat),
                                               value or config.DEFAULT_MAX_STEPS[seat])
        if value:
            seats[seat]["max_steps"] = value
        else:
            seats[seat].pop("max_steps", None)

    # The observer is a nested block, so it is collected the same way the seats are: flags the
    # operator did not pass leave the existing answer alone, and a flag that means "drop it"
    # (--observer-disable) is explicit rather than implied by an empty string.
    observer_cfg = dict(loop.get("observer") or {})
    if args.observer_disable:
        observer_cfg = {}
    if args.observer_route:
        observer_cfg["route"] = args.observer_route
    if args.observer_profile:
        observer_cfg["profile"] = args.observer_profile
    if args.observer_profile and not observer_cfg.get("route"):
        observer_cfg["route"] = f"{loop['id']}-observe"
    if args.observer_deliver:
        observer_cfg["deliver"] = args.observer_deliver
    if args.observer_events is not None:
        observer_cfg["events"] = args.observer_events
    if args.observer_digest_min is not None:
        observer_cfg["digest_min"] = args.observer_digest_min
    for key in ("urgent_route", "urgent_profile", "urgent_deliver"):
        value = getattr(args, f"observer_{key}", None)
        if value is not None:
            if value:
                observer_cfg[key] = value
            else:
                observer_cfg.pop(key, None)        # blank = back to one feed
    if args.observer_mute or args.observer_unmute:
        if not observer_cfg.get("route"):
            # Muting something that does not exist would write a feed with nowhere to go — a
            # configuration error the operator would only discover by finding no notices.
            print("no observer feed on this loop — name one with --observer-route / "
                  "--observer-profile first")
            return 2
    if args.observer_mute:
        observer_cfg["mute"] = True
    if args.observer_unmute:
        observer_cfg["mute"] = False

    if (not changes and not seat_changes and not budget_changes and not adj_changed
            and not read_changed
            and observer_cfg == (loop.get("observer") or {})):
        if adj_done:
            return 0
        print("nothing to change — pass at least one setting "
              "(--concurrency, --reviewer-concurrency, --fixer-concurrency, --cap, --clone, "
              "--turn-budget, "
              "--observer-profile, ...)")
        return 0

    try:
        updated = config.normalize({**loop, **changes, "seats": seats, "observer": observer_cfg,
                                    "tokens": tokens, "read_token": read_after})
        if read_changed:
            # The same rules init applies to the reader: mapped, its file present and private,
            # and its own account (the four-identity rule).
            if not _token_ref(updated, read_after):
                raise config.ConfigError(
                    f"no token file mapped for the reader {read_after!r} — add "
                    f"--token {read_after}=/path/to/pat")
            config.check_token_file(_token_ref(updated, read_after),
                                    f"token file for the reader {read_after!r}")
            config.verify_credentials(updated, {"read"})
        if adj_changed and config.adjudicator_login(updated):
            # The same file-level rules init applies, plus: the file must exist and be private.
            config.verify_adjudicator_token(updated)
            config.verify_credentials(updated)
        _observer_check(updated)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2

    shims_ok = True
    before = loop.get("observer") or {}
    after = updated.get("observer") or {}
    # Disabling stops new notices and removes the route, but leaves the outbox intact.
    # Keep its former destination as a tombstone: otherwise re-enabling at a new
    # destination (or host) could forward a queued private PR link there.
    previous = loop.get("observer_disabled") or {}
    bound = before or previous
    keys = ("route", "profile", "deliver")
    old_target = {k: bound.get(k) for k in keys}
    old_target["host"] = previous.get("host") if previous else loop.get("host")
    new_target = {k: after.get(k) for k in keys}
    new_target["host"] = updated.get("host")
    destination_changed = any(before.get(k) != after.get(k)
                              for k in ("route", "profile", "deliver"))
    urgent_keys = ("urgent_route", "urgent_profile", "urgent_deliver")
    urgent_changed = any(before.get(k) != after.get(k) for k in urgent_keys) or (
        bool(after.get("urgent_route")) and
        any(before.get(k) != after.get(k) for k in ("profile", "deliver")))
    if bound and after and (old_target != new_target or (urgent_changed and before)):
        try:
            outstanding = observer.unsettled(state_mod.state_for(loop))
        except (OSError, ValueError) as exc:
            print(f"observer ledger cannot be checked — destination unchanged: {exc}")
            return 2
        if outstanding:
            print(f"refused: {outstanding} observer notice(s) are unsettled; retry/resolve "
                  "them before changing the route, profile, delivery or host")
            return 2
    if after:
        updated.pop("observer_disabled", None)
    elif before:
        updated["observer_disabled"] = old_target
    if destination_changed and after:
        name = after["route"]
        reserved = {cfg.get("route") for cfg in loop["seats"].values()}
        reserved.add((loop.get("adjudicator") or {}).get("route"))
        if name in reserved:
            print("refused: observer route must not replace a seat or adjudicator route")
            return 2
        existing = routes.route(name)
        if existing and name != before.get("route"):
            print(f"refused: route {name!r} already exists and is not this observer's route")
            return 2
        try:
            host = config.webhook_host(updated.get("host"), required=True)
            written = routes.new_route(name, profile=config.seat_profile(updated, "observer"),
                                       prompt=prompts.OBSERVER,
                                       events=["pull_request"], script="observe.py",
                                       deliver=after["deliver"], deliver_only=True, host=host,
                                       description=f"{updated['repo']} — read-only observer feed")
        except (OSError, ValueError, config.ConfigError) as exc:
            print(f"observer route could not be reconciled; loop config unchanged: {exc}")
            return 2
        try:
            route_intent.record(updated, {name: written})
        except (OSError, ValueError) as exc:
            # Without the record, self-heal would put the *old* route back: undo the route too.
            try:
                routes.restore_entries({name: existing})
            except (OSError, ValueError) as rollback_exc:
                print(f"ROLLBACK FAILED: {rollback_exc} — inspect route {name!r} manually")
            print(f"observer route intent could not be recorded; loop config unchanged: {exc}")
            return 2
        shims_ok = _install_shims(updated)
    if urgent_changed and after.get("urgent_route"):
        urgent = after["urgent_route"]
        reserved = {cfg.get("route") for cfg in loop["seats"].values()}
        reserved |= {(loop.get("adjudicator") or {}).get("route"), after.get("route")}
        existing = routes.route(urgent)
        if urgent in reserved or (existing and urgent != before.get("urgent_route")):
            print(f"refused: route {urgent!r} is already in use and is not this observer's "
                  "urgent route")
            return 2
        try:
            host = config.webhook_host(updated.get("host"), required=True)
            written = routes.new_route(
                urgent, profile=config.seat_profile(updated, "observer_urgent"),
                prompt=prompts.OBSERVER, events=["pull_request"], script="observe.py",
                deliver=after.get("urgent_deliver") or after["deliver"], deliver_only=True,
                host=host, description=f"{updated['repo']} — read-only observer feed: urgent "
                                       "notices only")
            route_intent.record(updated, {urgent: written})
        except (OSError, ValueError, config.ConfigError) as exc:
            print(f"observer urgent route could not be reconciled; loop config unchanged: {exc}")
            return 2
        shims_ok = _install_shims(updated) and shims_ok
    path = _write_config(updated)
    if before.get("urgent_route") and before["urgent_route"] != after.get("urgent_route"):
        try:
            route_intent.forget(updated, [before["urgent_route"]])
            routes.remove_route(before["urgent_route"])
        except (OSError, ValueError) as exc:
            print(f"warning: old urgent route {before['urgent_route']!r} remains; "
                  f"remove it manually: {exc}")
    if destination_changed and before.get("route") and before["route"] != after.get("route"):
        try:
            route_intent.forget(updated, [before["route"]])
            routes.remove_route(before["route"])
        except (OSError, ValueError) as exc:
            print(f"warning: old observer route {before['route']!r} remains; remove it manually: {exc}")
    for key, value in changes.items():
        print(f"  {key}: {loop.get(key)!r} → {value!r}")
    for seat, value in seat_changes.items():
        was = config.seat_concurrency(loop, seat)
        print(f"  {seat} concurrency: {was} → {value}  (this seat only)")
    for seat, (was, value) in budget_changes.items():
        if seat.endswith(" daily turns"):
            print(f"  {seat}: {was or 'no cap'} → {value or 'no cap'}  (this seat only)")
        elif seat.endswith(" max steps"):
            print(f"  {seat}: {was} → {value}  (this seat only; an issue fix takes the fixer's)")
        else:
            print(f"  {seat} turn budget: {was}s → {value}s  (this seat only)")
    if read_after != read_before:
        print(f"  read_token: {read_before or '(none)'} → {read_after}")
    if read_after and _token_ref(loop, read_after) != _token_ref(updated, read_after):
        print(f"  reader token file ({read_after}): {_token_ref(loop, read_after) or '(none)'}"
              f" → {_token_ref(updated, read_after)}")
    if adj_after != adj_before:
        shown = adj_after or "(none — rulings go to the operator only)"
        print(f"  adjudicator login: {adj_before or '(none)'} → {shown}")
    if adj_after and _token_ref(loop, adj_after) != _token_ref(updated, adj_after):
        print(f"  adjudicator token file ({adj_after}): {_token_ref(loop, adj_after) or '(none)'}"
              f" → {_token_ref(updated, adj_after)}")
    if updated.get("observer") != (loop.get("observer") or {}):
        print(f"  observer: {observer.describe(loop.get('observer') or {})} → "
              f"{observer.describe(updated.get('observer') or {})}")
    print(f"loop config updated: {path}")
    if updated.get("host") != loop.get("host"):
        print(f"  next: `hermes dk apply --loop {updated['id']}` "
              "(rewrites the routes' origin; add --hooks to repoint the hooks)")

    if "turn_budget_s" in changes:
        for seat in ("reviewer", "fixer"):
            if (updated["seats"].get(seat) or {}).get("turn_budget_s") is not None:
                print(f"  note: {seat} has its own turn budget "
                      f"({updated['seats'][seat]['turn_budget_s']}s) — the loop default does not "
                      "apply to it")
    if "turn_budget_s" in changes or any(not key.endswith((" daily turns", " max steps"))
                                         for key in budget_changes):
        print("  turn budget now: " + _budget_line(updated)
              + "   (queued turns keep the budget they were enqueued with)")

    for line in _parallel_lines(updated):
        # The seat notes only when the loop default moved: that is when they explain something.
        if "concurrency" in changes or not line.startswith("note:"):
            print(f"  {line}")
    return 0 if shims_ok else 1


def cmd_apply(args) -> int:
    """``apply``, then the explicit extras it was asked for (``--hooks``, ``--watchdog-shim``),
    after the routes are what the config says — their URLs are what the hooks must post to."""
    rc = _apply(args)
    if rc == 2 or not (getattr(args, "hooks", False) or getattr(args, "watchdog_shim", False)):
        return rc
    loop = config.load_id(args.loop)
    if getattr(args, "watchdog_shim", False):
        if args.dry_run:
            print(f"  would write the watchdog shim: {doctor.shim_path()}")
        else:
            try:
                print(f"  watchdog shim written: {_write_watchdog_shim()}")
            except OSError as exc:
                print(_shim_refusal(exc))
                return 1
    if getattr(args, "hooks", False):
        rc = max(rc, _ensure_hooks(loop, getattr(args, "admin_token", "") or None, args.dry_run))
    return rc


def _apply(args) -> int:
    """Make a loop match the plugin settings — one push, with the diff printed.

    Push, not subscription: a running loop whose numbers changed under it is exactly the kind of
    thing nobody can debug at 2am. The same validation as ``init``/``set`` still applies, so a
    settings form asking for two reviews at once without a clone path is refused here too.

    An identity change is *staged* rather than half-applied: the loop config and the routes whose
    URL carries the old profile move together, and a change that would land while a seat has a run
    in flight is refused unless the operator says otherwise out loud.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2

    try:
        # Token-file paths first: a bad path is refused by name before anything is computed.
        config.verify_token_settings(_SETTINGS)
        updated = config.normalize(config.apply_settings(loop, _SETTINGS))
    except config.ConfigError as exc:
        print(f"settings refused: {exc}")
        return 2

    # The four-identity rule holds for the config apply would leave behind, whether or not this
    # push moves an identity: "nothing changed" must not endorse a reader that is also a seat.
    problem = config.reader_problem(updated)
    if problem:
        print(f"settings refused: {updated['id']}: {problem} — {config.FOUR_IDENTITY_RULE}")
        print(f"fix: {config.reader_fix(updated)}")
        return 2
    reader = str(updated.get("read_token") or "")
    if reader.casefold() not in {str(k).casefold() for k in updated.get("tokens") or {}}:
        # doctor fails this loop; apply must not report success over it either.
        print(f"settings refused: {updated['id']}: read_token {reader!r} has no entry in "
              "'tokens' — the gates read GitHub as that login and have no file to read it from")
        print(f"fix: hermes dk set --loop {updated['id']} --read-token {reader} "
              f"--token {reader}=/path/to/pat")
        return 2

    identity, touched = _seat_diffs(loop, updated)
    # The installed registry can drift independently of the loop and the form. Repair those
    # routes through the same ownership, seat and in-flight preflight as an identity push.
    binds = _route_binds(updated, set(_routes_of(updated)))
    # A route installed by an older release (the pre-#21 breach route on gate_reviewer.py) is
    # repaired here too: `init` refuses an existing loop, so apply is the only reconcile path.
    repairs = _stale_scripts(updated)
    drifted = _drifted_routes(updated)
    rebinding = touched | set(binds) | set(repairs) | set(drifted)
    try:
        # Validate what this apply would *write*: a loop that predates the seat checks keeps
        # loading, but a seat this push moves must be one that can actually run.
        config.verify_seats(updated, rebinding)
        adj = config.adjudicator_login(updated)
        if adj and (adj != config.adjudicator_login(loop)
                    or _token_ref(loop, adj) != _token_ref(updated, adj)):
            config.verify_adjudicator_token(updated)
            config.verify_credentials(updated)
        if rebinding:
            _verify_routes(updated, rebinding)
        # Refuses a foreign file (#105) — only over what this apply writes: the config's pairs. A
        # route the registry holds elsewhere is what apply is about to rebind away, so it never
        # blocks; disagreements left afterwards are reported by ``_diverged``.
        shim_pairs = gate_shims.wanted(updated)
        shim_lines = gate_shims.install(updated, dry_run=True, report=False, pairs=shim_pairs)
    except config.ConfigError as exc:
        print(f"settings refused: {exc}")
        return 2
    # The gate shims are not a setting and move nothing, so apply writes them up front, even when
    # everything else already matches: a loop whose gates the gateway cannot find drops every event.
    if shim_lines and args.dry_run:
        for line in shim_lines:
            print(f"  {line}")
    elif shim_lines and not _install_shims(updated, report=False, pairs=shim_pairs):
        return 2
    missing = {role: name for role, name in _routes_of(updated).items() if not routes.route(name)}
    leftover_hooks = 0
    if missing and getattr(args, "recreate_routes", False):
        rc, leftover_hooks = _recreate_routes(updated, missing, dry_run=args.dry_run,
                                              token_login=getattr(args, "admin_token", "") or None)
        if rc:
            return rc

    changes = []
    for key in ("cap", "base", "host", "grace_min", "ttl_min", "inflight_ttl_min",
                "turn_budget_s", "attribution", "fixer_check", "review_after_ci",
                "fix_ci", "ci_fix_cap", "review_only_update", "required_checks", "human_paths",
                "review_only", "review_only_cap", "review_only_daily"):
        if updated.get(key) != loop.get(key):
            changes.append((key, loop.get(key), updated.get(key)))
    if (updated.get("clone") or "") != (loop.get("clone") or ""):
        changes.append(("clone", loop.get("clone") or "(none)", updated.get("clone") or "(none)"))
    for seat in ("reviewer", "fixer"):
        was, now = config.seat_concurrency(loop, seat), config.seat_concurrency(updated, seat)
        if was != now:
            changes.append((f"{seat} concurrency", was, now))

    for seat in ("reviewer", "fixer"):
        was, now = config.max_steps(loop, seat), config.max_steps(updated, seat)
        if was != now:
            changes.append((f"{seat} max steps", was, now))
    was = config.seat_daily_turns(loop, "issue_fixer")
    now = config.seat_daily_turns(updated, "issue_fixer")
    if was != now:
        changes.append(("fix daily turns", was, now))

    missing_routes = sorted(name for role, name in _routes_of(updated).items()
                            if role in touched and not routes.route(name))

    if not changes and not identity and not binds and not repairs and not drifted:
        # Never "matches" over a route the registry holds differently (#112 review): the loop
        # config can agree with the settings while the gateway serves something else.
        left = _diverged(updated, header=f"[{loop['id']}] the loop config matches the plugin "
                                         "settings, but the route registry does not:")
        if not left:
            print(f"[{loop['id']}] already matches the plugin settings")
        duplicates, slashed = [], []
        # With --hooks, the hooks are reconciled after this by _ensure_hooks — the same listing,
        # the same keep-one rule, and a repoint for a hook this read-only check can only refuse.
        if not args.dry_run and not getattr(args, "hooks", False):
            token = getattr(args, "admin_token", "") or None
            try:        # nothing else moves: this reads, to name two hooks on one route URL and
                        # find one whose URL differs only by a trailing slash (a gateway 404)
                slashed, duplicates = _hook_moves(loop, updated, {}, token, check_only=True)
            except HookAccessError as exc:
                print(f"  (repo hooks not checked: {exc})")
            except config.ConfigError as exc:
                print(f"  {exc}")       # a hook on a loop route this push cannot explain (#55)
                return 1
        for hook_id, old, new, ssl in slashed:
            try:
                _patch_hook_url(loop, hook_id, new, getattr(args, "admin_token", "") or None,
                                insecure_ssl=ssl)
            except config.ConfigError as exc:
                print(f"  hook {hook_id} NOT repointed from {old} to {new}: {exc}")
                print(f"  fix: {_hook_fix(loop, exc, getattr(args, 'admin_token', '') or None)}")
                return 2
            print(f"  hook {hook_id} → {new}   (was {old}: the gateway does not route a trailing "
                  "slash)")
        for hook, keep in duplicates:
            print(f"  {_redundant_hook_line(loop, hook, keep)}")
        return 1 if left or leftover_hooks or duplicates else 0
    if not args.dry_run and updated.get("host") != loop.get("host") and loop.get("observer"):
        try:
            outstanding = observer.unsettled(state_mod.state_for(loop))
        except (OSError, ValueError) as exc:
            print(f"observer ledger cannot be checked — host unchanged: {exc}")
            return 2
        if outstanding:
            print(f"refused: {outstanding} observer notice(s) are unsettled; retry/resolve "
                  "them before changing the host")
            return 2
    for name, was, now in list(changes) + list(identity):
        print(f"  {name}: {was} → {now}")
    for role, (name, current, target) in sorted(binds.items()):
        print(f"  route {name}: profile {current or '(blank)'} → {target}   (the URL carries "
              "the profile)")
    for role, (name, fields) in sorted(drifted.items()):
        if fields == ["enabled"]:
            print(f"  route {name}: disabled (enabled: false) → enabled   (the gateway answers 403 "
                  "to every event while it is off)")
        else:
            print(f"  route {name}: {', '.join(fields)} "
                  f"{'differs' if len(fields) == 1 else 'differ'} from what the plugin writes → "
                  "rewritten from the loop config   (secret kept)")
    for role, (name, script) in sorted(repairs.items()):
        print(f"  route {name}: script {script} → {GATE_SCRIPT[role]}   (installed by an older "
              "release)")
    for name in missing_routes:
        print(f"  route {name}: not installed — `hermes dk init` creates routes; "
              "apply will not invent one behind your back")
    if missed := [role for role, name in _routes_of(updated).items()
                  if role in touched and name in missing_routes]:
        print(f"refused: {', '.join(missed)} would move to a route that does not exist yet")
        return 2

    if args.dry_run:
        rewritten = ({bind[0] for bind in binds.values()} | {n for n, _ in repairs.values()}
                     | {n for n, _ in drifted.values()})
        if getattr(args, "recreate_routes", False):
            rewritten |= set(missing.values())
        left = _diverged(updated, rewritten=rewritten)
        try:
            hook_before = _hook_origin(loop, drifted)
            if hook_before is not loop:
                # The routes' origin moves, so their hooks move too: preview it (reads only).
                moves, redundant = _hook_moves(hook_before, updated, binds,
                                               getattr(args, "admin_token", "") or None)
                for hook_id, _old, new, _ssl in moves:
                    print(f"  hook {hook_id} would move → {new}")
                for hook, keep in redundant:
                    print(f"  {_redundant_hook_line(loop, hook, keep)}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        print("(dry run — nothing written: no loop config, no routes touched)")
        if left:
            print("  apply would exit 1: the route(s) above still disagree with the config after it")
        return 1 if left or leftover_hooks else 0

    busy = _busy_seats(loop, rebinding) if rebinding else []
    if busy and not getattr(args, "while_busy", False):
        for line in busy:
            print(f"  {line}")
        print("refused: a seat is in flight, and its live run holds the old profile and login "
              "until it ends. Let it finish, then apply again — or pass --while-busy to rebind "
              "now, knowing that run still finishes under the identity it started with.")
        return 2
    for line in busy:
        print(f"  {line}  (--while-busy: that run keeps the identity it started with)")

    # Preflight remote hooks before any local mutation. Snapshot each owned route and roll back
    # both surfaces on any failure; config is published only after route/hook readback agrees.
    try:
        hook_moves, redundant_hooks = _hook_moves(_hook_origin(loop, drifted), updated, binds,
                                                  getattr(args, "admin_token", "") or None)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    previous = {name: routes.route(name)
                for name in ({bind[0] for bind in binds.values()} | {n for n, _ in repairs.values()}
                             | {n for n, _ in drifted.values()})}
    config_path = config.config_dir() / f"{loop['id']}.json"
    previous_config = config_path.read_bytes()
    attempted_hooks = []
    try:
        rewrite = tuple(set(binds) | set(repairs) | set(drifted))
        rebound = list(_install_routes(updated, roles=rewrite).items()) if rewrite else []
        for role, name in rebound:
            entry = routes.route(name)
            if not entry or routes.route_profile(entry) != config.seat_profile(updated, role):
                raise config.ConfigError(f"route {name} readback does not match requested profile")
            if entry.get("script") != GATE_SCRIPT[role]:
                raise config.ConfigError(f"route {name} readback does not run {GATE_SCRIPT[role]}")
            if gate_shims.contract_drift(updated, role, entry):
                raise config.ConfigError(f"route {name} readback still differs from what the "
                                         "plugin writes: "
                                         + ", ".join(gate_shims.contract_drift(updated, role, entry)))
        for hook_id, old, new, ssl in hook_moves:
            attempted_hooks.append((hook_id, old, ssl))
            _patch_hook_url(loop, hook_id, new, getattr(args, "admin_token", "") or None,
                            insecure_ssl=ssl)
        path = _write_config(updated) if changes or identity else config_path
        if rebound:
            route_intent.record_live(updated, [name for _, name in rebound])
    except Exception as exc:
        failed = []
        for hook_id, old, ssl in reversed(attempted_hooks):
            try:
                _patch_hook_url(loop, hook_id, old, getattr(args, "admin_token", "") or None,
                                insecure_ssl=ssl)
            except Exception as rollback_exc:
                failed.append(f"hook {hook_id}: {rollback_exc}")
        if previous:
            try:
                routes.restore_entries(previous)
            except Exception as rollback_exc:
                failed.append(f"routes: {rollback_exc}")
        try:
            if config_path.read_bytes() != previous_config:
                _restore_config(config_path, previous_config)
        except Exception as rollback_exc:
            failed.append(f"config: {rollback_exc}")
        try:
            config_unchanged = config_path.read_bytes() == previous_config
        except OSError:
            config_unchanged = False
        print(f"reconciliation FAILED: {exc}; " +
              ("config unchanged" if config_unchanged else "config may have changed"))
        if failed:
            print("ROLLBACK FAILED — inspect before retrying: " + "; ".join(failed))
        else:
            print("  prior routes and hook URLs restored")
        return 2
    print(f"loop config updated: {path}")
    for role, name in rebound:
        print(f"  route {name} rebound → profile {config.seat_profile(updated, role)}"
              + (f", script {GATE_SCRIPT[role]}" if role in repairs else "")
              + (f", {', '.join(drifted[role][1])} restored" if role in drifted else ""))
    for hook_id, _, new, _ssl in hook_moves:
        print(f"  hook {hook_id} → {new}")
    for hook, keep in redundant_hooks:
        print(f"  {_redundant_hook_line(loop, hook, keep)}")
    return 1 if _diverged(updated) or redundant_hooks or leftover_hooks else 0


def cmd_show(args) -> int:
    """Every setting one loop has, its effective value and where it came from (#555). Read-only."""
    from . import show

    try:
        loop = config.load_id(args.loop)
        raw = json.loads((config.config_dir() / f"{args.loop}.json").read_text())
    except (config.ConfigError, OSError, ValueError) as exc:
        print(f"cannot show loop: {exc}")
        return 2
    rows = show.collect(loop, raw if isinstance(raw, dict) else {}, _SETTINGS)
    if args.json:
        print(json.dumps({"loop": loop["id"], "settings": rows}, indent=2, sort_keys=True))
    else:
        print(show.render(loop["id"], rows))
    return 0


def cmd_settings(args) -> int:
    """Show the plugin-level defaults — what a new loop starts from, and what ``apply`` pushes."""
    d = config.settings_defaults(_SETTINGS)
    print("plugin settings (desktop: Capabilities → Plugins → diaktoros)")
    width = max(len(key) for key in config.SETTINGS_SCHEMA)
    for key, spec in config.SETTINGS_SCHEMA.items():
        value = d[key]
        source = "set" if str((_SETTINGS or {}).get(key, "")) not in ("", "None") else "default"
        print(f"  {key:<{width}} {str(value):<26} [{source}]  {spec['description']}")

    print("\nseat mapping (blank = not set here: a loop keeps its own answer)")
    mapping = config.seat_mapping(_SETTINGS)
    for role in config.ROUTE_ROLES:
        entry = mapping.get(role) or {}
        profile = entry.get("profile") or "(blank)"
        login = entry.get("login") or ("(blank)" if role in config.LOGIN_SETTINGS
                                       else "(blank — operator-only)")
        cell = f"login {login:<30} "
        print(f"  {role:<12} profile {profile:<20} {cell}[{'set' if entry else 'blank'}]")
        # The path only, never the file: this is where the login's PAT is read from at use time.
        print(f"  {'':<12} token file {entry.get('token_file') or '(blank)'}")
    adjudicator_note = ("the adjudicator lands only on a loop that already has an adjudicator "
                        "route; its login is optional — a fourth account the ruling is also posted "
                        "as — and it never pushes or reviews; route names stay per repository")
    print(f"  ({adjudicator_note})")

    try:
        loops = config.all_loops()
    except config.ConfigError as exc:
        loops = []
        print(f"\ncould not read every loop config: {exc}")
    if loops:
        print("\neffective mapping per loop (what each one runs as today; "
              "`apply --loop <id>` pushes the mapping above onto exactly one of them):")
        for loop in loops:
            print(f"  {loop['id']:<18} "
                  + " · ".join(_role_summary(loop, role) for role in config.ROUTE_ROLES))
            refs = _credential_lines(loop)
            if refs:
                print(f"  {'':<18} token files: " + " · ".join(refs))
    if not _SETTINGS:
        print("\nnothing set — every value above is the schema default")
    print("\napply them to a loop with: hermes dk apply --loop <id>"
          "\n(settings are defaults, not a subscription: an existing loop keeps its own seats, "
          "profiles and numbers until you apply — and blank fields above never erase them)")
    return 0


def _budget_line(loop: dict) -> str:
    """Each seat's turn budget, the way status/doctor/set print it."""
    seats = ["reviewer", "fixer"] + (["adjudicator"] if (loop.get("adjudicator") or {}).get("route")
                                     else [])
    return " · ".join(f"{seat} {config.turn_budget(loop, seat)}s" for seat in seats)


def _steps_line(loop: dict) -> str:
    """Each seat's agent-step cap per turn (#271); an issue fix takes the fixer's."""
    seats = ["reviewer", "fixer"]
    if (loop.get("adjudicator") or {}).get("route"):
        seats.append("adjudicator")
    if (loop.get("triage") or {}).get("route"):
        seats.append("triage")
    return " · ".join(f"{seat} {config.max_steps(loop, seat)}" for seat in seats)


def _readable_loops() -> tuple[list[dict], list[str]]:
    """Every loop that loads, plus one ``skipping <file>: <reason>`` line per one that does not.

    The formatted form of ``config.readable_loops`` for ``list`` and ``status``: one broken file
    must not hide every healthy loop's state. (``explain`` calls ``config.readable_loops``
    itself — it needs the ids, not these lines.) Verbs that act on loops keep ``all_loops``'s
    all-or-nothing refusal.
    """
    loops, skipped = config.readable_loops()
    return loops, [f"skipping {loop_id}.json: {reason}" for loop_id, reason in skipped]


def cmd_list(args) -> int:
    loops, skipped = _readable_loops()
    for line in skipped:
        print(line)
    if not loops:
        if not skipped:
            print(f"no loops configured in {config.config_dir()}")
        return 2 if skipped else 0
    for loop in loops:
        seats = " ".join(f"{seat}={config.seat_concurrency(loop, seat)}"
                         for seat in ("reviewer", "fixer"))
        print(f"{loop['id']:<20} {loop['repo']:<30} cap={loop['cap']} {seats} "
              f"fixers={','.join(loop['fixers'])} reviewers={','.join(loop['reviewers'])}")
    return 2 if skipped else 0


def _dependency_lines(loop: dict, pr: int | None = None, limit: int = 5) -> list[str]:
    """What the host dependency prefetch did for this loop's newest turns (#51), from the ledger.

    Read-only; an absent or unreadable ledger is simply no lines (status/explain say the rest).
    """
    from .run_supervisor import dependency_view, describe_dependencies
    rows = dependency_view(config.host_path("ledger"), loop["repo"], pr,
                           limit)
    return [describe_dependencies(row) for row in rows or []]


def _view_lines(loop: dict, pr: int, head: str | None, limit: int = 3) -> list[str]:
    """This PR's turns whose seat could not see the whole change (#93, #110), from the ledger.

    Only the current head's: a new head gets a new change record. Read-only; an absent or
    unreadable ledger is no lines.
    """
    from .run_supervisor import describe_view, view_view
    rows = view_view(_ledger_path(), loop["repo"], pr, limit) or []
    return [describe_view(row) for row in rows if head and row["head"] == head]


def cmd_status(args) -> int:
    skipped: list[str] = []
    if args.loop:
        try:
            loops = [config.load_id(args.loop)]
        except config.ConfigError as exc:
            print(f"cannot show loop: {exc}")
            return 2
    else:
        loops, skipped = _readable_loops()
        for line in skipped:
            print(line)
        if not loops and not skipped:
            print(f"no loops configured in {config.config_dir()} — run "
                  "`hermes dk setup` to create one")
    for loop in loops:
        from . import state as state_mod

        st = state_mod.state_for(loop)
        print()
        print(f"[{loop['id']}] {loop['repo']}  (cap {loop['cap']}, base {loop['base']})")
        print("  parallel:   " + " · ".join(
            f"{seat} {config.seat_concurrency(loop, seat)}"
            + ("" if config.seat_concurrency(loop, seat) > 1 else " (serialized)")
            for seat in ("reviewer", "fixer")))
        for line in _pinned_seat_notes(loop):
            print(f"  note:       {line}")
        print(f"  clone:      {loop['clone'] or '(none)'}")
        print(f"  turn:       {_budget_line(loop)} per turn (killed past it)")
        print(f"  steps:      {_steps_line(loop)} per turn (agent steps; `set --reviewer-max-steps "
              "N`/`--fixer-max-steps N`)")
        for line in _pacing_lines(loop):
            print(f"  pacing:     {line}")
        print("  fixer push: " + ("ENABLED — operator accepted PR-metadata/ref race"
                                  if config.unattended_fixer_push_enabled(loop)
                                  else "off (unattended pushes disabled)"))
        print("  signed:     " + ("on — what the loop posts says 'Automated by Diaktoros'"
                                  if attribution.enabled(loop)
                                  else "off (no footer or commit trailer)"))
        print(f"  state:      {st.dir}")
        print(f"  seats:      reviewer={loop['seats']['reviewer']['login']} "
              f"({loop['seats']['reviewer']['profile']}) · "
              f"fixer={loop['seats']['fixer']['login']} ({loop['seats']['fixer']['profile']})")
        adjudicator = loop.get("adjudicator") or {}
        if adjudicator.get("route"):
            adj_login = config.adjudicator_login(loop)
            print(f"  {'adjudicator:':<12} {adjudicator.get('profile', 'default')} "
                  f"(route {adjudicator['route']})"
                  + (f" · comments as {adj_login}" if adj_login else " · operator-only rulings"))
        else:
            print(f"  {'adjudicator:':<12} (no route — the cap only writes a marker)")
        # What the registry actually serves, next to what the config claims: those two facts can
        # disagree after a profile change, and this is the one place the operator would see it.
        print("  routes:     " + " · ".join(_route_state(loop, role)
                                            for role in (*config.ROUTED_ROLES, "observer",
                                                         "observer_urgent")
                                            if role in _routes_of(loop)))
        refs = _credential_lines(loop)
        if refs:
            print("  token refs: " + " · ".join(refs))
        problem = config.reader_problem(loop)
        if problem:
            print(f"  ⚠️  reader:  {problem} — {config.FOUR_IDENTITY_RULE}")
            print(f"  fix:        {config.reader_fix(loop)}")
        for seat, entries in st._lock_ledger().items():
            for key, entry in (entries if isinstance(entries, dict) else {}).items():
                at = state_mod.mark_at(entry)
                if at is None:
                    # Shown, not hidden: the file holds it, but no reader counts it as running
                    # and the next write of the ledger drops it (#80).
                    print(f"  ⚠️  lock:    {seat} mark for {key} is unreadable (no numeric 'at') — "
                          "not counted as running; the next sweep prunes it")
                    continue
                held = (time.time() - at) / 60
                print(f"  running:    {seat} on {key} for {held:.0f}m")
        for line in _dependency_lines(loop):
            print(f"  deps:       {line}")
        for issue_no, origin in sorted(st.fix_holds().items()):
            print(f"  held:       issue #{issue_no} fix waits for PR #{origin} to merge")
        for seat in ("reviewer", "fixer"):
            queued = len(st.queue_items(seat))
            if queued:
                print(f"  queued:     {seat} {queued} "
                      f"({config.seat_concurrency(loop, seat)} at a time)")
        queue = st.queue_all()
        for seat, items in (queue if isinstance(queue, dict) else {}).items():
            for key, entry in (items if isinstance(items, dict) else {}).items():
                if state_mod.mark_at(entry) is None:
                    print(f"  ⚠️  queued:  {seat} · {key} is unreadable (no numeric 'at') — "
                          "the next drain drops it")
                    continue
                print(f"  queued:     {seat} · {key} — {entry.get('reason')}")
        breaches = st.breach_all()
        if breaches:
            for key, entry in breaches.items():
                print(f"  breach:     {key} at {(entry.get('head') or '')[:7]} "
                      f"— {entry.get('status')}")
        observer_cfg = loop.get("observer") or {}
        print(f"  observer:   {observer.describe(observer_cfg)}")
        if not observer_cfg and loop.get("observer_disabled"):
            print("  observer:   disabled — existing notices remain owed; restoring the original "
                  "destination can resume them")
        if observer_cfg or loop.get("observer_disabled"):
            counts = observer.owed(st)
            owed = sum(n for status, n in counts.items() if status != "delivered")
            print(f"  observer:   {counts.get('delivered', 0)} delivered · {owed} owed "
                  f"(failed or waiting)")
            # Only a live feed can be broken: a muted or absent one is reported above, and calling
            # that a problem would be crying wolf at a setting the operator chose.
            if observer_cfg and not observer.configured(loop):
                problem = observer.unusable(loop)
                if problem:
                    print(f"  observer:   ⚠ {problem}")
        watch = st.watch()
        if watch.get("last_run"):
            print(f"  watchdog:   last run {watch['last_run']}")
        _print_ledger_runs(loop, None, "  runs:       ", limit=10)
    return 2 if skipped else 0


def _ledger_path() -> pathlib.Path:
    return config.host_path("ledger")


def _print_ledger_runs(loop: dict, pr: int | None, prefix: str, limit: int) -> list[dict]:
    """The isolated run ledger's failed/waiting/uncertain rows for this loop, read-only (#53).
    Prints them and returns them ([] when there is no readable ledger)."""
    from .run_supervisor import describe_run, read_only_view

    ledger = _ledger_path()
    if not ledger.exists():
        return []
    rows = read_only_view(ledger, loop["repo"], pr)
    if rows is None:
        print(f"{prefix}run ledger unreadable ({ledger})")
        return []
    for row in rows[-limit:]:
        print(prefix + describe_run(row, loop["id"]))
        if row.get("detail") and pr is not None:
            for line in str(row["detail"]).splitlines()[-6:]:
                print(f"{' ' * len(prefix)}| {line[:200]}")
    if len(rows) > limit:
        print(f"{prefix}… {len(rows) - limit} more: python -m diaktoros.run_supervisor "
              f"status {ledger}")
    return rows


def cmd_retry(args) -> int:
    """Re-arm a PR's isolated run that failed before any external write (issue #53).

    Only runs at the PR's newest ledgered head are considered: the head of the most recently
    active run (``updated``), which is where the PR is after a backwards force-push re-armed an
    older head's run. A run that may have written —
    uncertain, quarantined, reconciled, or with a receipt claim, push intent or ruling on
    record — is refused with the reconcile instructions; it is never replayed.
    """
    from .run_supervisor import Supervisor

    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    ledger = _ledger_path()
    if not ledger.exists():
        print(f"no run ledger at {ledger} — nothing to retry")
        return 2
    sup = Supervisor(ledger)
    with sup._connect() as con:
        rows = [dict(row) for row in con.execute(
            "SELECT id,seat,head,turn_key,state,error,updated FROM runs WHERE repo=? AND pr=? "
            "ORDER BY created,id", (loop["repo"], args.pr))]
    if args.seat:
        rows = [row for row in rows if row["seat"] == args.seat]
    if not rows:
        print(f"[{loop['id']}] #{args.pr}: no isolated run on record"
              + (f" for the {args.seat} seat" if args.seat else ""))
        return 2
    # Not a timestamp: the claim path bumps `updated` when it supersedes a row for a head the PR
    # has since left, so `max(updated)` can name an abandoned head and offer nothing (#126).
    # The newest head that still has an offerable row is the ledger-only answer; the PR's own head
    # is the tiebreaker when a supersession bump makes those two disagree (a gh read, or — when
    # that read fails — the newest offerable head: never the rewritten timestamp).
    from .run_supervisor import policy_cancelled
    from . import gh

    def offerable(row: dict) -> bool:
        return row["state"] in ("failed", "waiting", "uncertain") or (
            row["state"] == "cancelled" and policy_cancelled(row["error"]))

    offerable_rows = [row for row in rows if offerable(row)]

    def newest(rws):
        return max(rws, key=lambda row: (row["updated"] or 0, row["id"]))["head"]

    ledger_head = newest(offerable_rows) if offerable_rows else newest(rows)
    bumped_head = newest(rows)
    # The PR read is only a tiebreaker: reach for it when a supersession bump made the two ledger
    # answers disagree. Otherwise stay on the ledger — no network round-trip for the common case
    # (and none when nothing is offerable: the "nothing to retry" path answers from the ledger).
    if offerable_rows and ledger_head != bumped_head:
        pr = gh.pr(loop, args.pr)
        pr_head = ((pr or {}).get("head") or {}).get("sha") if isinstance(pr, dict) else None
        head = pr_head or ledger_head
    else:
        head = ledger_head
    # A fixer run the push policy cancelled at claim is recovered here, under the policy in
    # force now; any other cancellation is superseded and is not offered (runs_view draws the
    # same line: a new head gets its own turn).
    candidates = [row for row in rows if row["head"] == head and offerable(row)]
    if not candidates:
        print(f"[{loop['id']}] #{args.pr} @ {head[:7]}: nothing to retry — "
              + ", ".join(f"{row['seat']} {row['state']}" for row in rows if row["head"] == head))
        return 2
    rearmed, refused = 0, 0
    for row in candidates:
        label = f"{row['seat']} #{args.pr} @ {head[:7]}" + (f" ({row['turn_key']})" if row["turn_key"] else "")
        try:
            # The loop's budget now (#49): a turn killed at its budget reruns on the raised one.
            sup.retry(row["id"], budget=config.turn_budget(loop, row["seat"]))
        except ValueError as exc:
            refused += 1
            print(f"[{loop['id']}] {label} {row['state']}: {exc}")
            continue
        rearmed += 1
        print(f"[{loop['id']}] {label} re-armed (was {row['state']}: {row['error'] or 'no reason'})")
    if rearmed:
        try:
            started = gate.resume_isolated(loop)
        except Exception as exc:
            print(f"worker not started: {type(exc).__name__}: {exc} — the next event or armed "
                  "watchdog sweep starts it")
        else:
            print("worker started" if started else
                  "no private runtime file — the run stays pending until one exists")
    return 0 if rearmed and not refused else 2


def cmd_trace(args) -> int:
    """Dry-run one webhook through its gate: why it would, or would not, start a run (#216).

    The gate script runs for real on a temporary copy of the loop's home, so the answer is the
    live gate's own; nothing is posted, no run is started and the loop's state is untouched (see
    ``diaktoros.trace``). Exit 2 when it cannot be asked: an unknown loop, no such delivery, an
    unreadable payload file, or an event or route this loop does not serve.
    """
    from . import trace
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    admin = getattr(args, "admin_token", "") or None
    if admin and gh.token_path(loop, admin) is None:
        print(f"refused: --admin-token {admin!r} has no token file mapped on this loop — map it "
              f"with `hermes dk set --loop {shlex.quote(loop['id'])} --token "
              f"{shlex.quote(admin)}=/abs/path`")
        return 2
    try:
        if args.delivery:
            payload, event, route = trace.fetch_delivery(loop, args.delivery,
                                                         admin or loop.get("read_token"))
        else:
            try:
                payload = json.loads(pathlib.Path(args.payload).expanduser().read_text())
            except (OSError, ValueError) as exc:
                print(f"cannot read the payload file: {exc}")
                return 2
            if not isinstance(payload, dict):
                print("cannot read the payload file: not a JSON object")
                return 2
            event, route = args.event or trace.infer_event(payload), None
        role = trace.role_for(loop, event, args.route or route)
    except trace.TraceError as exc:
        print(f"cannot trace: {exc}")
        return 2
    return trace.run(loop, payload, event, role)


def _pacing_lines(loop: dict) -> list[str]:
    """Daily caps and today's counts per seat, and any seat account held for its reset (#219)."""
    from . import pacing
    lines = []
    caps = [(seat, config.seat_daily_turns(loop, seat)) for seat in ("reviewer", "fixer")]
    if any(cap for _, cap in caps):
        lines.append(" · ".join(f"{seat} {pacing.turns_today(loop['id'], seat)}/"
                                f"{cap if cap else 'no cap'} today" for seat, cap in caps))
    try:
        holds = (json.loads(pacing.path().read_text()).get("holds") or {})
    except (OSError, ValueError, AttributeError):
        holds = {}
    now = time.time()
    # Only this loop's seat profiles: an account key is provider|endpoint|profile (pacing).
    profiles = {str(((loop.get("seats") or {}).get(seat) or {}).get("profile") or "")
                for seat in ("reviewer", "fixer")} | {
                str((loop.get("adjudicator") or {}).get("profile") or "")}
    for key, entry in sorted(holds.items()):
        until = entry.get("until") if isinstance(entry, dict) else None
        provider, profile = key.split("|", 1)[0], key.rsplit("|", 1)[-1]
        if isinstance(until, (int, float)) and until > now and profile in profiles - {""}:
            lines.append(f"held: {provider} as profile {profile} until {pacing.when(until)} "
                         f"({entry.get('reason') or 'usage window'})")
    return lines


def cmd_stats(args) -> int:
    """What one loop did since ``--since``: seat turns from the run ledger (how they ended, how
    long they ran and waited) and, with ``--github``, its PRs and reviews read as the reader.

    Read-only: the ledger is opened read-only and GitHub is only read. ``--html FILE`` writes one
    self-contained page; ``--json`` prints the same data. Both hold totals and timings only, so
    either can be published (docs/operations.md, "Publishing stats").
    """
    from . import run_supervisor, stats
    try:
        since = stats.parse_since(args.since)
    except ValueError as exc:
        print(str(exc))
        return 2
    if args.loop:
        try:
            loop = config.load_id(args.loop)
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return 2
    else:
        loops, refused = config.readable_loops()
        for loop_id, reason in refused:
            print(f"skipping {loop_id}.json: {reason}")
        names = [item["id"] for item in loops] + [loop_id for loop_id, _ in refused]
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if refused:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
        loop = loops[0]
    report = stats.collect(loop, run_supervisor.production_ledger(), since, args.github)
    print(stats.as_json(report) if args.json else stats.text(report))
    if args.html:
        target = pathlib.Path(args.html).expanduser()
        target.write_text(stats.as_html(report))
        if not args.json:
            print(f"\nwrote {target}")
    return 0


def _shim_refusal(exc: OSError) -> str:
    """Why the watchdog shim could not be written, and a chmod that can repair it: on a path that
    exists — a shim not created yet means its directory (or the nearest existing ancestor)
    refused the write (#354)."""
    failed = pathlib.Path(exc.filename or doctor.shim_path())
    target = failed
    while not target.exists() and target.parent != target:
        target = target.parent
    return (f"  refused: cannot write the watchdog shim at {failed} ({exc.strerror or exc}); "
            f"make {target} writable (e.g. `chmod u+w -- {shlex.quote(str(target))}`) and "
            "re-run `apply --watchdog-shim`")


def _reviewer_runs(repo: str, number: int, head: str) -> int:
    """How many reviewer runs the ledger holds for this head (read-only; 0 without a ledger)."""
    import sqlite3
    from . import run_supervisor
    db = run_supervisor.production_ledger()
    if not db.exists():
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        return con.execute("SELECT COUNT(*) FROM runs WHERE repo=? AND pr=? AND head=? AND "
                           "seat='reviewer'", (repo, number, head)).fetchone()[0]
    finally:
        con.close()


def cmd_escalate(args) -> int:
    """Send one PR to the adjudicator now, whatever its verdict count (#459), as the operator.

    The same breach marker and isolated ruling turn a spent cap starts, marked ``escalated``: the
    PR is parked at this head as if its cap were spent (no further review or fix there), and the
    worker re-reads every fact before the ruling runs. Exit 1 when refused, with the reason;
    2 when the question cannot be asked.
    """
    from . import run_supervisor, state as state_mod, transition
    from .util import now_iso
    if args.loop:
        try:
            loop = config.load_id(args.loop)
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return 2
    else:
        loops, refused = config.readable_loops()
        for loop_id, reason in refused:
            print(f"skipping {loop_id}.json: {reason}")
        names = [lp["id"] for lp in loops] + [loop_id for loop_id, _ in refused]
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if refused:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
        loop = loops[0]
    number = args.pr
    say = f"#{number}"
    if not str((loop.get("adjudicator") or {}).get("route") or ""):
        print(f"{say}: refused — this loop has no adjudicator; turn it on with "
              f"`hermes dk set --loop {loop['id']} --adjudicator-profile PROFILE`")
        return 1
    pr = gh.pr(loop, number)
    if not isinstance(pr, dict) or pr.get("number") != number:
        print(f"{say}: the PR could not be read from GitHub — nothing escalated")
        return 2
    head = str((pr.get("head") or {}).get("sha") or "")
    author = str((pr.get("user") or {}).get("login") or "").lower()
    say = f"#{number} @ {head[:7]}"
    if (pr.get("state") != "open" or pr.get("draft") or not head
            or (pr.get("base") or {}).get("ref") != loop["base"] or author not in loop["fixers"]):
        print(f"{say}: refused — only an open, ready PR by a fixer ({', '.join(loop['fixers'])}) "
              f"on {loop['base']} can be escalated")
        return 1
    st = state_mod.state_for(loop)
    reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
    if not isinstance(reviews, list):
        print(f"{say}: the reviews could not be read — nothing escalated")
        return 2
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if latest is not None and gh.review_state(latest) == "APPROVED":
        print(f"{say}: refused — approved at this head; there is nothing to rule on")
        return 1
    verdicts = len(gate.verdicts(reviews, loop))
    if verdicts < 1:
        print(f"{say}: refused — no verdict yet; there is nothing to rule on")
        return 1
    marker = st.breach_get(number)
    if isinstance(marker, dict) and marker.get("head") == head and marker.get("status") in gate.STANDING:
        print(f"{say}: already awaiting a ruling ({marker.get('status')})")
        return 1
    db = run_supervisor.production_ledger()
    if db.exists():
        import sqlite3
        # A queued review or fix is retired by the escalation itself (the worker's claim); only
        # a turn already running must finish first, since it may still write at this head.
        busy = run_supervisor.ACTIVE
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
        try:
            out = con.execute(f"SELECT COUNT(*) FROM runs WHERE repo=? AND pr=? AND state IN "
                              f"({','.join('?' * len(busy))})", (loop["repo"], number, *busy)).fetchone()[0]
        finally:
            con.close()
        if out:
            print(f"{say}: refused — {out} run(s) for this PR are in flight; escalate once "
                  "they finish (a queued review or fix is retired by the escalation)")
            return 1
    why = " ".join(str(args.reason or "").split())[:200] or "the operator asked for a ruling"
    outcome = gate.breach(loop, st, number, head, verdicts, f"escalated: {why}",
                          escalated={"by": "operator", "at": now_iso(), "reason": why})
    if outcome == "stale":
        print(f"{say}: the PR moved while escalating — nothing escalated")
        return 1
    print(f"{say}: escalated at {verdicts}/{loop['cap']} verdicts — adjudicator turn "
          f"{'enqueued' if outcome in ('new', 'retry') else outcome}; the PR is parked at this "
          f"head (`hermes dk explain --loop {loop['id']} --pr {number}` follows it)")
    return 0


def cmd_review(args) -> int:
    """Ask for a fresh review of a PR's current head (#375), as the operator.

    The real reviewer gate decides, fed a ``ready_for_review`` event built from the live PR: the
    same rules as a webhook (open, not a draft, a fixer's PR on the loop's base, no verdict or
    run already at this head, the verdict cap), so this command cannot start a review the gate
    would not. No GitHub write: the review itself is the only one, as usual. Exit 1 when the
    gate declines, with its own reason; 2 when the question cannot be asked.
    """
    if args.loop:
        try:
            loop = config.load_id(args.loop)
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return 2
    else:
        loops, refused = config.readable_loops()
        for loop_id, reason in refused:
            print(f"skipping {loop_id}.json: {reason}")
        names = [lp["id"] for lp in loops] + [loop_id for loop_id, _ in refused]
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if refused:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
        loop = loops[0]
    pr = gh.pr(loop, args.pr)
    if not isinstance(pr, dict) or pr.get("number") != args.pr:
        print(f"#{args.pr}: the PR could not be read from GitHub — nothing started")
        return 2
    head = (pr.get("head") or {}).get("sha") or ""
    from . import state as state_mod
    st = state_mod.state_for(loop)
    key = gate.seat_key(loop, args.pr)
    if getattr(args, "another_round", False):
        # One more verdict on a review-only PR that hit its cap. The operator running this on the
        # host is the maintainer; the grant is recorded against this head and verdict count, so it
        # buys exactly one verdict and a repeat of the command changes nothing.
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if author not in config.review_only(loop):
            print(f"#{args.pr}: {author or 'its author'} is not review-only — only a review-only "
                  "PR has a review cap to lift; nothing granted")
            return 1
        reviews = gh.reviews(loop, args.pr)
        if not isinstance(reviews, list):
            print(f"#{args.pr}: the reviews could not be read — nothing granted")
            return 2
        rounds = len(gate.verdicts(reviews, loop))
        if rounds < config.review_only_cap(loop):
            print(f"#{args.pr}: {rounds} of {config.review_only_cap(loop)} verdicts spent — the "
                  "cap is not reached; nothing to grant")
            return 1
        if gate.reviewed_at_head(reviews, loop, head):
            # The gate declines a head that already has a verdict before it looks at the cap, so a
            # grant here would be spent on nothing and not match the author's next push.
            print(f"#{args.pr} @ {head[:7]}: head already has a verdict — push the change, then "
                  "grant another round; nothing granted")
            return 1
        if not st.review_cap_grant(args.pr, head, rounds):
            print(f"#{args.pr} @ {head[:7]}: another round is already granted for this head")
        else:
            print(f"#{args.pr} @ {head[:7]}: another round granted — one more verdict")

    def queued():
        entry = (st.queue_all().get("reviewer") or {}).get(key) or {}
        return entry if entry.get("head") == head else None
    before = (_reviewer_runs(loop["repo"], args.pr, head), queued())
    payload = {"action": "ready_for_review", "number": args.pr, "pull_request": pr,
               "repository": {"full_name": loop["repo"]},
               "sender": {"login": loop.get("read_token") or "operator"}}
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "gate_reviewer.py"
    try:
        done = subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                              text=True, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"#{args.pr} @ {head[:7]}: the gate did not finish within 120s — "
              "no answer; check `explain` before asking again")
        return 2
    if _reviewer_runs(loop["repo"], args.pr, head) > before[0]:
        print(f"#{args.pr} @ {head[:7]}: review queued for the isolated worker "
              f"(`hermes dk explain --loop {loop['id']} --pr {args.pr}` follows it)")
        return 0
    waiting = queued()
    if waiting and waiting != before[1]:
        reason = waiting.get("reason") or "the reviewer seat is busy"
        print(f"#{args.pr} @ {head[:7]}: review queued, waiting — {reason}")
        return 0
    reasons = logged(done.stderr.splitlines())
    print(f"#{args.pr} @ {head[:7]}: no review started — "
          + (reasons[-1] if reasons else "the reviewer gate declined without a reason"
             + (f" (exit {done.returncode})" if done.returncode else "")))
    return 1


def cmd_explain(args) -> int:
    """Why one PR is not moving, and the one event that would move it.

    Read-only all the way down: it reads GitHub and the loop's own state files and writes neither —
    no claim, no queue entry, no drain, no webhook POST, no token printed. The conclusions come
    from ``gate.explain``, so they are the predicates the live gates run rather than a second
    opinion about them.

    Exit 2 only when the question cannot be asked at all: an unknown loop, a loop file the loader
    refuses (without ``--loop`` each is named on a ``skipping <file>: <reason>`` line), several
    loops — refused ones included — and no ``--loop``, or no loop files at all. Before this function
    runs, argparse can also exit 2 for the same subcommand on its own account: a missing ``--pr``
    (``the following arguments are required: --pr``), an ``--pr`` value that is not an integer
    (``invalid int value``), an ``--pr`` flag with no value after it (``expected one argument``),
    an ``--loop`` flag with no value after it (``argument --loop: expected one argument``), or an
    unrecognized flag (``unrecognized arguments``).
    A PR GitHub does
    not have, or cannot be read, is an *answer*: it is reported as unknown, with the read to
    retry.
    """
    from . import state as state_mod
    from .run_supervisor import next_step

    if args.loop:
        try:
            loops = [config.load_id(args.loop)]
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return 2
    else:
        loops, refused = config.readable_loops()
        for loop_id, reason in refused:
            print(f"skipping {loop_id}.json: {reason}")
        skipped = [loop_id for loop_id, _ in refused]
        # A file that will not load is still a configured loop: the question may be about it.
        names = [loop["id"] for loop in loops] + skipped
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if skipped:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2

    for loop in loops:
        st = state_mod.state_for(loop)
        report = gate.explain(loop, st, args.pr, gate.explain_facts(loop, args.pr))
        print()
        print(f"[{loop['id']}] {loop['repo']}#{args.pr} — why this PR is not moving")
        print(f"  {'pr:':<12}{report['url']}")
        print(f"  {'read:':<12}{report['read_at']} (GitHub pulls/reviews/hooks + local state; "
              f"read once, nothing written)")
        print(f"  {'state:':<12}{report['state_line']}")
        for issue_no, origin in sorted(st.fix_holds().items()):
            if origin == args.pr:
                print(f"  {'held:':<12}issue #{issue_no} fix waits for this PR to merge")
        if report['chain']['status'] != 'direct':
            print(f"  {'chain:':<12}{report['chain']['status']} · "
                  f"parents {report['chain']['parents']} · {report['chain']['reason']}")
        print(f"  {'budget:':<12}{report['budget']}")
        print(f"  {'seat:':<12}{report['seat']}")
        print(f"  {'queue:':<12}{report['queue']}")
        print(f"  {'in-flight:':<12}{report['inflight']}")
        for line in _dependency_lines(loop, args.pr, limit=3):
            print(f"  {'deps:':<12}{line}")
        # A head the host could not show whole cannot be approved by the loop (the broker refuses
        # it); only an operator can end it, so it is said here rather than left to the cap.
        for line in _view_lines(loop, args.pr, report.get("head")):
            print(f"  {'view:':<12}{line}")
        from . import findings as findings_mod
        for text in findings_mod.lines(st.findings_get(args.pr)):
            print(f"  {'finding:':<12}{text}")
        print(f"  {'escalation:':<12}{report['escalation']}")
        print(f"  {'hooks:':<12}{report['hooks']}")
        print(f"  {'sweep:':<12}{report['sweep']}")
        print(f"  {'github:':<12}{report['github']}")
        decisions = report.get("gate_decisions") or []
        for text in decisions:
            print(f"  {'decided:':<12}{text}")
        if not decisions:
            print(f"  {'decided:':<12}no gate decision recorded for this PR")
        if not report.get("gate_failures"):
            print(f"  {'gates:':<12}no unresolved gate failure recorded for this PR")
        runs = _print_ledger_runs(loop, args.pr, f"  {'run:':<12}", limit=6)
        # A ledgered turn at the current head that waits to retry, failed before any write, or
        # is quarantined holds the PR as surely as any gate guard (#53): it is the blocker, and
        # its step is next. A failed turn that did write is final; GitHub shows where it left off.
        held = [row for row in runs if report.get("head") and row["head"] == report["head"]
                and (row["state"] != "failed" or row["write"] is None)]
        blockers = list(report["blockers"]) + [
            f"isolated {row['seat']} turn {row['state']} at this head — "
            f"{row['error'] or 'no reason recorded'}" for row in held]
        for text in blockers:
            print(f"  {'blocked:':<12}{text}")
        if not blockers:
            print(f"  {'blocked:':<12}nothing — no guard is holding this PR back")
        print(f"  {'next:':<12}"
              + (next_step(held[-1], loop["id"]) if held else report['next']['action']))
    return 0


def _move_watchdog_job(*, dry_run: bool) -> list[str]:
    """``migrate`` (#425 stage 4): the shared watchdog job and its shim take the new name.

    The new shim is written and a job created with the old one's schedule and deliver target,
    read back, and only then the old job removed and its shim deleted. A failed create takes the
    new shim back out, so the old pair stays the live one.
    """
    new_name, old_name = config.HOST_FILES["watchdog_shim"]
    old_shim, new_shim = config.home() / old_name, config.home() / new_name
    if not old_shim.exists():
        return []
    jobs, error = _cron_jobs({"id": ""})
    if jobs is None:
        return [f"watchdog: NOT FINISHED — {error}"]
    old_job = config.WATCHDOG_JOBS[old_shim.name]
    old_jobs = [job for job in jobs if str(job.get("name") or "").strip() == old_job]
    if dry_run:
        return [f"watchdog: would move job {old_job!r} → {SHARED_JOB_NAME!r} and its shim "
                f"{old_shim.name} → {new_shim.name}"]
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    new_shim.write_text(SHIM.format(watchdog=watchdog))
    new_shim.chmod(0o755)
    lines = [f"watchdog: shim written as {new_shim.name}"]
    if old_jobs and not any(str(job.get("name") or "").strip() == SHARED_JOB_NAME for job in jobs):
        schedule, deliver = doctor._job_schedule(old_jobs[0]), doctor._job_deliver(old_jobs[0])
        cmd = [_hermes_bin() or "hermes", "cron", "create", schedule, "--name", SHARED_JOB_NAME,
               "--no-agent", "--script", new_shim.name, "--deliver", deliver]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            failed = proc.returncode != 0 and (proc.stderr or proc.stdout).strip()[:200]
        except Exception as exc:
            failed = str(exc)
        after, _ = _cron_jobs({"id": ""})
        if failed or not any(str(job.get("name") or "").strip() == SHARED_JOB_NAME
                             for job in after or []):
            new_shim.unlink(missing_ok=True)
            return [f"watchdog: NOT FINISHED — the new job was not created"
                    + (f" ({failed})" if failed else "") + f"; the old job keeps running. "
                    f"Run it yourself, then migrate again: {shlex.join(cmd)}"]
        lines.append(f"watchdog: job {SHARED_JOB_NAME!r} created ({schedule}, deliver {deliver})")
    for job in old_jobs:
        job_id = str(job.get("id") or "")
        try:
            proc = subprocess.run([_hermes_bin() or "hermes", "cron", "remove", job_id],
                                  capture_output=True, text=True, timeout=120)
            ok = proc.returncode == 0
        except Exception:
            ok = False
        lines.append(f"watchdog: old job {job_id} ({old_job!r}) removed" if ok else
                     f"watchdog: NOT FINISHED — remove the old job yourself: "
                     f"hermes cron remove {shlex.quote(job_id)}")
    remaining, _ = _cron_jobs({"id": ""})
    if remaining is not None and not any(pathlib.Path(str(job.get("script") or "")).name
                                         == old_shim.name for job in remaining):
        old_shim.unlink(missing_ok=True)
        lines.append(f"watchdog: old shim {old_shim.name} removed")
    return lines


def _rename_loop(spec: str, *, dry_run: bool, admin: str | None) -> tuple[list[str], bool]:
    """``migrate --rename-loop OLD=NEW`` (#425 stage 3): ``(lines, refused)``.

    In an order a crash or a re-run cannot hurt: the routes are copied under the new names (the
    same secret, so a hook moved to one still verifies); each hook is moved to its new route's
    URL and pinged; then the state directory, today's pacing counts, the new loop file and the
    route intent record move; the old loop file goes; and an old route is removed only once every
    hook that pointed at it was answered on the new one. A refused ping puts its hook back.
    """
    from . import hook_ping, migrate, pacing, run_supervisor
    old_id, sep, new_id = spec.partition("=")
    tag = f"rename-loop: {old_id or '?'} → {new_id or '?'}"
    if not sep or old_id == new_id or not all(migrate.LOOP_ID.match(x or "") for x in (old_id, new_id)):
        return [f"{tag}: REFUSED — give OLD=NEW, two different loop ids of letters, digits, "
                "'.', '_' or '-' (at most 64)"], True
    directory = config.config_dir()
    try:
        old_loop = config.load_id(old_id) if (directory / f"{old_id}.json").exists() else None
        new_loop = config.load_id(new_id) if (directory / f"{new_id}.json").exists() else None
    except config.ConfigError as exc:
        return [f"{tag}: REFUSED — {exc}"], True
    if old_loop is None and new_loop is None:
        return [f"{tag}: REFUSED — no loop named {old_id!r}"], True
    if old_loop is not None and new_loop is not None and old_loop["repo"] != new_loop["repo"]:
        return [f"{tag}: REFUSED — a loop named {new_id!r} already exists for {new_loop['repo']}"], True
    base = old_loop or new_loop
    renames = migrate.route_renames(base, old_id, new_id)
    target = new_loop or migrate.renamed_loop(old_loop, new_id, renames)
    busy = migrate.busy_runs(run_supervisor.production_ledger(), base["repo"])
    if busy:
        return [f"{tag}: REFUSED — {busy} run(s) for {base['repo']} are in flight or uncertain; "
                "wait for them (or reconcile them), then run migrate again"], True
    registry = routes.all_routes()
    for old, new in renames.items():
        here = [registry[name] for name in (old, new) if isinstance(registry.get(name), dict)]
        if not here:
            return [f"{tag}: REFUSED — route {old} is in the registry under neither name; restore it "
                    f"first: `hermes dk doctor --loop {base['id']} --repair` (or `apply "
                    "--recreate-routes`)"], True
        if not all(route_intent.owned(entry) for entry in here):
            return [f"{tag}: REFUSED — route {old} or {new} is not one of this plugin's gates"], True
        if len(here) == 2 and here[0].get("secret") != here[1].get("secret"):
            return [f"{tag}: REFUSED — route {new} already exists with another secret"], True
    try:
        hooks = _hook_listing(base, admin)
    except config.ConfigError as exc:
        return [f"{tag}: REFUSED — cannot list the repo hooks: {exc}",
                f"  fix: {_hook_fix(base, exc, admin)}"], True
    role_of = {name: role for role, name in route_intent.routes_of(target).items()}
    moves = []
    for hook in hooks:
        name = routes.route_name_of(hook["config"]["url"])
        if name in renames or name in renames.values():
            new_name = renames.get(name, name)
            url = routes.url_for_profile(new_name, config.seat_profile(target, role_of.get(new_name, "")),
                                         target.get("host"))
            if not url:
                return [f"{tag}: REFUSED — cannot build the URL for route {new_name}"], True
            moves.append((hook, new_name, url))
    lines = [f"{tag}: routes " + (", ".join(f"{a} → {b}" for a, b in renames.items())
                                   or "(none follow the loop id)")
             + f"; {len(moves)} hook(s) to move and ping"]
    if dry_run:
        if old_loop is not None:
            lines.append(f"{tag}: " + migrate.move_state_dir(old_loop, target, dry_run=True))
        return lines, False
    copies = {new: dict(registry[old]) for old, new in renames.items()
              if new not in registry and isinstance(registry.get(old), dict)}
    if copies:
        routes.restore_entries(copies)
        lines.append(f"{tag}: routes copied under the new names (same secrets): {', '.join(copies)}")
    proven, unproven = set(), set()
    for hook, new_name, url in moves:
        before = hook["config"]["url"]
        if not routes.serves_route_url(before, url):
            _patch_hook_url(base, hook["id"], url, admin, require_secret=True,
                            insecure_ssl=hook["config"].get("insecure_ssl"))
        status, said = hook_ping.ping(base, hook["id"], admin)
        if status == hook_ping.OK:
            proven.add(new_name)
            lines.append(f"{tag}: hook {hook['id']} → {new_name}: {said}")
        elif status == hook_ping.SILENT:
            unproven.add(new_name)
            lines.append(f"{tag}: hook {hook['id']} → {new_name}: {said} — the old route stays "
                         "until a ping is answered; run migrate again later")
        else:
            back = ""
            if before != url and routes.route(routes.route_name_of(before)):
                _patch_hook_url(base, hook["id"], before, admin, require_secret=True,
                                insecure_ssl=hook["config"].get("insecure_ssl"))
                back = f"; hook {hook['id']} put back on {before}"
            return lines + [f"{tag}: REFUSED — {said}{back}; nothing else was moved"], True
    if old_loop is not None:
        lines.append(f"{tag}: " + migrate.move_state_dir(old_loop, target, dry_run=False))
    if pacing.rename_loop(old_id, new_id):
        lines.append(f"{tag}: today's turn counts moved to {new_id}")
    if new_loop is None:
        _write_config(target)
        lines.append(f"{tag}: loop file written as {new_id}.json")
    route_intent.record_live(target, list(renames.values()))
    route_intent.forget(target, list(renames))
    (directory / f"{old_id}.json").unlink(missing_ok=True)
    live = routes.all_routes()
    gone = [old for old, new in renames.items() if old in live and new not in unproven]
    if gone:
        routes.restore_entries({old: None for old in gone})
        lines.append(f"{tag}: old routes removed: {', '.join(gone)}")
    if unproven:
        lines.append(f"{tag}: NOT FINISHED — not proven yet: {', '.join(sorted(unproven))}")
    return lines, bool(unproven)


def cmd_migrate(args) -> int:
    """Move an install from the ``hermes-review-loop`` plugin to this one (#425); see ``migrate``.

    Run between turns. The install is paused for the whole run (#431): gates defer, the worker
    claims nothing, the watchdog sweeps nothing. Exit 1 when a step was refused or is not
    finished (a run in flight, a shim this plugin did not write, a hook not yet proven);
    re-running finishes what an interrupted run began.
    """
    from . import migrate
    if args.dry_run:
        return _migrate(args)
    try:
        with migrate.hold() as left:
            if left is not None:
                print(f"migrate: picking up — {migrate.describe(left)}")
            return _migrate(args)
    except migrate.MigrationBusy as exc:
        print(f"cannot migrate: {exc}")
        return 2


def _migrate(args) -> int:
    from . import backup, migrate, run_supervisor
    try:
        loops = config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot migrate: {exc}")
        return 2
    if args.dry_run:
        print(f"backup: would write {backup.default_out()} before anything moves")
    else:
        # Before the first move (#496): migrate has no undo, the archive is the undo.
        try:
            archive = backup.create()
        except (backup.BackupError, OSError, sqlite3.Error, config.ConfigError) as exc:
            print(f"cannot migrate: the backup before it failed ({exc}); nothing was moved")
            return 2
        print(f"backup: {archive} (restore with `hermes dk restore {shlex.quote(str(archive))}`)")
        print(f"backup: {backup.WARNING}")
    lines = migrate.settings_step(_CTX, dry_run=args.dry_run)
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    if not args.dry_run:
        # Before anything moves, every route and the watchdog run this plugin's code, which
        # honours the pause. Until now they may run the old plugin's, which does not: a delivery
        # mid-migration then wrote under the old file names (seen on the first live upgrade).
        lines += migrate.shim_step(loops, _write_watchdog_shim, config.watchdog_shim(),
                                   SHIM.format(watchdog=watchdog), dry_run=False)
    lines += _move_watchdog_job(dry_run=args.dry_run)
    # Then the host files: everything after reads and writes them under their new names.
    lines += migrate.files_step(_write_config, dry_run=args.dry_run)
    if not args.dry_run:
        loops = config.all_loops()                 # the loop files may live elsewhere now
    lines += migrate.repo_step(loops, _write_moved_repo, dry_run=args.dry_run,
                               ledger=run_supervisor.production_ledger())
    if getattr(args, "rename_loop", None):
        # After the repository step: a hook is written through the repository's current name.
        renamed, _ = _rename_loop(args.rename_loop, dry_run=args.dry_run,
                                  admin=getattr(args, "admin_token", None))
        lines += renamed
    if not args.dry_run:
        loops = config.all_loops()                 # a moved repository or id is the loop's name now
    lines += migrate.shim_step(loops, _write_watchdog_shim, config.watchdog_shim(),
                               SHIM.format(watchdog=watchdog), dry_run=args.dry_run)
    if not args.dry_run:
        lines += migrate.doctor_step(loops)
    lines += migrate.old_plugin_note(config.home() / "plugins")
    print("\n".join(lines))
    if args.dry_run:
        print("dry run: nothing was written")
    return 1 if any(word in line for line in lines
                    for word in ("REFUSED", "NOT rewritten", "NOT FINISHED")) else 0


def cmd_backup(args) -> int:
    """``backup`` (#496): one 0600 archive of everything the plugin owns; see ``backup``."""
    from . import backup
    try:
        archive = backup.create(pathlib.Path(args.out) if args.out else None)
    except (backup.BackupError, OSError, sqlite3.Error, config.ConfigError) as exc:
        print(f"cannot back up: {exc}")
        return 2
    print(f"backup written: {archive}")
    print(f"warning: {backup.WARNING}")
    return 0


def _restore_cron(job: dict | None) -> list[str]:
    """Recreate the watchdog job through ``hermes cron`` and read it back, as migrate does."""
    if not job:
        return ["cron: the backup holds no watchdog job"]
    jobs, error = _cron_jobs({"id": ""})
    if jobs is None:
        return [f"cron: NOT FINISHED — {error}"]
    if any(str(j.get("name") or "").strip() in SHARED_JOB_NAMES for j in jobs):
        return ["cron: the watchdog job exists — kept"]
    cmd = [_hermes_bin() or "hermes", "cron", "create", job["schedule"], "--name", job["name"],
           "--no-agent", "--script", config.watchdog_shim().name, "--deliver", job["deliver"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        failed = proc.returncode != 0 and (proc.stderr or proc.stdout).strip()[:200]
    except Exception as exc:
        failed = str(exc)
    after, _ = _cron_jobs({"id": ""})
    if failed or not any(str(j.get("name") or "").strip() == job["name"] for j in after or []):
        return ["cron: NOT FINISHED — the job was not created"
                + (f" ({failed})" if failed else "") + f"; run it yourself: {shlex.join(cmd)}"]
    return [f"cron: job {job['name']!r} created ({job['schedule']}, deliver {job['deliver']})"]


def _restore(args, manifest: dict, archive: pathlib.Path) -> int:
    from . import backup, migrate
    lines = [f"restore: {len(manifest['files'])} file(s), {len(manifest.get('routes') or {})} "
             f"route(s), loops: {', '.join(manifest.get('loops') or []) or 'none'}"]
    lines += [f"restore: NOT FINISHED — token file missing: {line}"
              for line in backup.missing_tokens(manifest)]
    lines += [f"restore: will overwrite {name}" for name in backup.existing(manifest)]
    lines += [f"restore: writes a loop state directory outside the Hermes home: {path}"
              for path in backup.outside_state_dirs(manifest)]
    problems = backup.unsafe(manifest,
                             allowed_state_dirs=getattr(args, "allow_state_dir", None) or ())
    if problems:
        print("\n".join(lines + [f"restore: REFUSED — {p}" for p in problems[:20]]
                         + ["nothing was written"]))
        return 2
    if args.dry_run:
        print("\n".join(lines + ["dry run: nothing was written"]))
        return 0
    backup.put_files(archive, manifest)
    lines.append(f"restore: {len(manifest['files'])} file(s) put back")
    if manifest.get("routes"):
        routes.restore_entries(manifest["routes"])
        lines.append(f"restore: {len(manifest['routes'])} route entr(ies) put back")
    loops = config.all_loops()
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    if not config.watchdog_shim().is_file():
        _write_watchdog_shim()
    lines += migrate.shim_step(loops, _write_watchdog_shim, config.watchdog_shim(),
                               SHIM.format(watchdog=watchdog), dry_run=False)
    lines += _restore_cron(manifest.get("cron"))
    lines += migrate.doctor_step(loops)
    print("\n".join(lines))
    return 1 if any(word in line for line in lines
                    for word in ("REFUSED", "NOT rewritten", "NOT FINISHED")) \
        or any(line.startswith("doctor:") and line != "doctor: every check verified"
               for line in lines) else 0


def cmd_restore(args) -> int:
    """``restore`` (#496): put a backup back, paused with migrate's marker; see ``backup``."""
    from . import backup, migrate
    archive = pathlib.Path(args.file).expanduser()
    try:
        manifest = backup.read_manifest(archive)
        if args.dry_run:
            return _restore(args, manifest, archive)
        with migrate.hold():
            busy = migrate.runs_in_flight()
            if busy:
                print(f"cannot restore: {busy} run(s) are in flight or uncertain; wait for them "
                      "(or reconcile them), then run restore again")
                return 2
            clash = backup.existing(manifest)
            if clash and not args.force:
                print("cannot restore: existing state would be overwritten (use --force):\n  "
                      + "\n  ".join(clash[:20])
                      + (f"\n  … {len(clash) - 20} more" if len(clash) > 20 else ""))
                return 2
            return _restore(args, manifest, archive)
    except migrate.MigrationBusy as exc:
        print(f"cannot restore: {exc}")
        return 2
    except (backup.BackupError, OSError, sqlite3.Error, config.ConfigError) as exc:
        print(f"cannot restore: {exc}")
        return 2


def cmd_doctor(args) -> int:
    """Read-only preflight: is this installation able to run the loop at all?

    Every check is in ``doctor.py`` — what each state means and, deliberately, everything this
    verb does *not* do (it writes nothing, and it never fires a route: a synthetic POST at a
    seat's route is a real agent run). This is the installation-level counterpart of the
    per-PR diagnosis in the watchdog, for the question `init` cannot answer about itself.
    """
    try:
        loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot preflight loop: {doctor._safe_report_text(str(exc))}")
        return 2
    if not loops:
        print(f"no loops configured in {config.config_dir()} — run "
              "`hermes dk setup` to create one")
        return 0
    failed = 0
    for loop in loops:
        if getattr(args, "repair", False):
            # The one write doctor can make, and only when asked: put this loop's own routes back
            # from the plugin's intent record (same secret). Everything after it stays read-only.
            lines = route_intent.heal(loop) + gate_shims.heal(loop)
            print("\n".join(lines) if lines else
                  f"[{loop['id']}] repair: routes match the plugin's intent record — nothing restored")
        failed += doctor.report(loop, doctor.check_loop(loop, offline=args.offline),
                                strict=args.strict)
    return 1 if failed else 0


def cmd_selftest(args) -> int:
    """Verify the live isolated path for one loop; every step and its fix live in selftest.py.

    Unlike ``doctor`` this touches the real capabilities (bubblewrap, the model, GitHub reads, and
    with ``--live-turn`` one real reviewer turn) — but it never writes to GitHub: GitHub calls go
    through a GET-only guard and the live turn's broker runs in its host-only no-write mode.
    """
    from . import selftest
    if args.live_turn and args.pr is None:
        print("--live-turn needs --pr N: the turn reviews that pull request")
        return 2
    if args.live_turn and args.no_model:
        print("--live-turn needs the model; drop --no-model")
        return 2
    if getattr(args, "timeout", None) is not None and args.timeout < 1:
        print("--timeout must be positive")
        return 2
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"cannot selftest loop: {doctor._safe_report_text(str(exc))}")
        return 2
    # Default to what production gives the reviewer seat, so a passing selftest proves the turn
    # fits the budget the unattended worker will actually enforce (#49).
    return selftest.run(loop, pr=args.pr, model=not args.no_model, live_turn=args.live_turn,
                        timeout=(args.timeout if getattr(args, "timeout", None) is not None
                                 else config.turn_budget(loop, "reviewer")),
                        ping=getattr(args, "ping", False),
                        ping_login=getattr(args, "admin_token", "") or None)


def cmd_corpus(args) -> int:
    """Replay the golden corpus through the reviewer (no-write) and record the scores (#491)."""
    from . import corpus, prompts, seat_model, selftest
    try:
        loop = config.load_id(args.loop)
        directory = corpus.corpus_dir(loop, args.dir)
        cases = corpus.load(directory)
        scores = config.state_dir(loop) / corpus.SCORES_FILE
    except (config.ConfigError, corpus.CorpusError) as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    if args.history:
        bad: list = []
        for entry in corpus.history(scores, bad):
            print(f"{entry['prompt_rev']}  {entry['model']}  caught {entry['caught']}  "
                  f"missed {entry['missed']}")
        if bad:
            print(f"skipped {len(bad)} unreadable line(s) in {scores} "
                  f"(line {', '.join(map(str, bad))})")
        return 0
    if not cases:
        print(f"no cases in {directory}")
        return 2
    try:
        settings = selftest.check_runtime(selftest.Report(None, selftest.Redactor()),
                                          selftest.runtime_path())
        if settings is None:
            raise corpus.CorpusError("no usable runtime config; run `hermes dk selftest` first")
        reviewer = seat_model.resolve_seat(loop, "reviewer", settings)
        review = corpus.live_review(loop, settings, reviewer,
                                    args.timeout or config.turn_budget(loop, "reviewer"))
    except Exception as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    try:
        with selftest.github_read_only(), selftest.worker_tempdir():
            corpus.check_heads(loop, cases)
            results = corpus.replay(cases, review)
    except corpus.CorpusError as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    for r in results:
        print(f"{r['case']}: caught {len(r['caught'])} missed {len(r['missed'])}"
              + (f" ({', '.join(r['missed'])})" if r["missed"] else "")
              + (f" [{r['error']}]" if r.get("error") else ""))
    entry = corpus.record(scores, prompts.revision("reviewer"), reviewer.model, results)
    print(f"caught {entry['caught']}, missed {entry['missed']} "
          f"(prompt {entry['prompt_rev']}, model {entry['model']}); recorded in {scores}")
    return 1 if entry["missed"] else 0


def cmd_models(args) -> int:
    """Read-only: what a profile's provider offers, from Hermes's model catalog (issue #32).

    Changing a seat's model is changing that profile's model in Hermes (`hermes -p NAME model`);
    this only lists. Exit 1 when the profile, its provider or the catalog cannot answer.
    """
    from . import doctor, seat_model
    if bool(args.profile) == bool(args.seat):
        print("pass exactly one of --profile-name NAME or --seat reviewer|fixer|adjudicator")
        return 2
    profile, seat = args.profile, args.seat
    if seat:
        try:
            loop = config.load_id(args.loop) if args.loop else None
            if loop is None:
                loops = config.all_loops()
                if len(loops) != 1:
                    print("--seat needs --loop ID when more than one loop is configured")
                    return 2
                loop = loops[0]
        except config.ConfigError as exc:
            print(f"cannot read loop: {doctor._safe_report_text(str(exc))}")
            return 2
        profile = config.seat_profile(loop, seat)
    problem = seat_model.profile_problem(profile, seat or "requested")
    if problem:
        print(f"❌ {problem}")
        return 1
    settings, bad = doctor.runtime_settings()
    try:
        answer = seat_model.run_resolver(profile, "models", settings if bad is None else None)
    except seat_model.SeatModelError as exc:
        print(f"❌ {exc}")
        return 1
    provider = str(answer.get("requested") or "(auto)")
    current = str(answer.get("model") or "")
    print(f"profile {profile}{f' (seat {seat})' if seat else ''}: provider {provider}, "
          f"current model {current or '(unset)'}")
    if answer.get("error"):
        print(f"❌ {seat_model._redact(answer['error'])}")
        return 1
    rows = [(str(mid), "catalog") for mid in answer.get("catalog") or []]
    rows += [(str(mid), "profile config") for mid in answer.get("declared") or []
             if str(mid) not in {row[0] for row in rows}]
    if not answer.get("catalog_known"):
        print(f"⚠️  provider {provider} is unknown to the Hermes model catalog "
              "(no curated list; the catalog may also be unreachable or disabled)")
    if not rows:
        print("❌ no models listed for this provider — check the provider name, or ask the "
              "provider directly; nothing here changes the profile")
        return 1
    for mid, where in rows:
        print(f"  {'*' if mid == current else ' '} {mid}  ({where})")
    if current and current not in {mid for mid, _ in rows}:
        print(f"⚠️  the current model {current} is not in this list")
    print(f"change it with `hermes -p {profile} model` — this command never writes")
    return 0


def _ping_loop_hooks(loop: dict, login: str | None) -> bool:
    """After arming: ping each of the loop's hooks and report how the gateway answered.

    An active hook whose secret the route does not hold looks armed and wakes nothing; this is
    the moment to find out. False only on a ping the gateway rejected or one that could not be
    sent — no delivery seen within the bounded wait is a warning, not a verdict.
    """
    from . import hook_ping
    hooks, error = _loop_hooks(loop, login)
    if hooks is None:
        print(f"[{loop['id']}] ⚠️ hooks not pinged: cannot read the repo's hooks ({error})")
        return True
    ok = True
    for hook in sorted(hooks, key=lambda item: item["id"]):
        status, line = hook_ping.ping(loop, hook["id"], login)
        for part in line.split("\n"):
            print(f"[{loop['id']}] {part}")
        if status in (hook_ping.REJECTED, hook_ping.ERROR) and not line.startswith("⚠️"):
            ok = False
    return ok


def cmd_arm(args) -> int:
    """Arm or pause loops by flipping their repo hooks; exit 1 unless GitHub confirms every one."""
    try:
        loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot {'pause' if args.pause else 'arm'}: {exc}")
        return 2
    if not loops:
        print(f"no loops configured in {config.config_dir()} — nothing to "
              f"{'pause' if args.pause else 'arm'}")
        return 2
    failed = []
    for loop in loops:
        lines, ok = _set_hooks(loop, not args.pause, args.admin_token)
        for line in lines:
            print(f"[{loop['id']}] {line}")
        if not ok:
            failed.append(loop["id"])
        elif not args.pause and not _ping_loop_hooks(loop, args.admin_token):
            failed.append(loop["id"])
    if failed:
        print(f"{'pause' if args.pause else 'arm'} NOT confirmed for: {', '.join(failed)} "
              "— the lines above show what GitHub reports now")
        return 1
    return 0


def _triage_lines(loop: dict) -> list[str]:
    triage = loop.get("triage") or {}
    if not triage.get("route"):
        return [f"[{loop['id']}] issue triage: off"]
    seat = (loop.get("seats") or {}).get("triage") or {}
    return [f"[{loop['id']}] issue triage: on — route {triage['route']} · profile "
            f"{triage['profile']} · labels as {config.triage_login(loop)}",
            f"  authors: {', '.join(triage['authors'])}",
            f"  labels (at most {triage['max_labels']}): {', '.join(triage['labels'])}",
            f"  comment: {'allowed' if triage['comment'] else 'off (labels only)'}"
            + (f" · daily cap {seat['daily_turns']}" if seat.get("daily_turns") else ""),
            (f"  issue fixes: label {triage['fix_label']!r} by "
             f"{', '.join(triage['maintainers'])} hands an issue to the fixer"
             f" · at most {config.seat_daily_turns(loop, 'issue_fixer')} a day"
             + ("" if config.unattended_fixer_push_enabled(loop) else
                f" — OFF until unattended fixer pushes are on: "
                f"{config.fixer_push_enable_command(loop)}")) if triage.get("fix_label")
            else "  issue fixes: off (--fix-label LABEL --maintainer LOGIN turns them on)",
            _auto_offer_line(triage)]


def _auto_offer_line(triage: dict) -> str:
    """The #232 auto-offer setting, so an operator can see what a save changed (#527)."""
    labels = triage.get("auto_fix_labels") or []
    if not labels:
        return "  auto-offer: off"
    return (f"  auto-offer: {', '.join(labels)} · at most "
            f"{triage.get('auto_fix_daily') or 25} a day")


_TRIAGE_CHANGE_FLAGS = ("profile", "author", "labels", "max_labels", "comment", "login", "token",
                        "fix_label", "maintainer", "daily_turns", "fix_daily_turns",
                        "auto_fix_label", "auto_fix_daily", "admin_token")


def _triage_change_flags(args) -> list[str]:
    """Option names given on the command line that change triage (not --loop/--dry-run)."""
    return ["--" + name.replace("_", "-") for name in _TRIAGE_CHANGE_FLAGS
            if getattr(args, name, None) not in (None, "", [])]


def cmd_triage(args) -> int:
    """Turn issue triage (#213) on or off for one loop, or show it.

    ``--enable`` writes the ``triage`` block, the triage route (a new secret) and its gate shim,
    and with ``--admin-token`` the repo hook on ``issues`` (paused until ``arm``). ``--disable``
    removes the route and shim, and with ``--admin-token`` the hook. Everything is validated
    before the first write, and a failed route write puts the config back.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    if not (args.enable or args.disable):
        given = _triage_change_flags(args)
        if not given:
            for line in _triage_lines(loop):
                print(line)
            return 0
        if not (loop.get("triage") or {}).get("route"):
            print(f"refused: issue triage is off on {loop['id']}, so {', '.join(given)} would "
                  "change nothing; add --enable (with --triage-profile, --author, --labels) "
                  "to turn it on")
            return 2
        args.enable = True      # triage is on: apply the flags as --enable would (#527)
    admin = args.admin_token or None
    if admin and gh.token_path(loop, admin) is None and admin.lower() not in {
            str(pair.split("=", 1)[0]).lower() for pair in args.token or []}:
        print(f"refused: --admin-token {admin!r} has no token file mapped on this loop")
        return 2
    path = config.config_dir() / f"{loop['id']}.json"
    previous_config = path.read_bytes()
    old_name = (loop.get("triage") or {}).get("route") or ""
    if args.disable:
        if not old_name:
            print(f"[{loop['id']}] issue triage already off")
            return 0
        updated = config.normalize({**loop, "triage": {}})
        if args.dry_run:
            print(f"[{loop['id']}] dry run — would turn issue triage off: remove route {old_name}"
                  + (" and its repo hook" if admin else "") + "; nothing written")
            return 0
        hook_lines = []
        if admin:
            try:
                listing = _hook_listing(loop, admin, require_active=False)
                own, _other = doctor.split_route_hooks(loop, listing, [old_name], ownership=True)
                for hook in own:
                    gh.api(loop, f"/repos/{loop['repo']}/hooks/{hook['id']}", method="DELETE",
                           login=admin)
                left = [h for h in _hook_listing(loop, admin, require_active=False)
                        if h.get("id") in {hook["id"] for hook in own}]
                if left:
                    raise config.ConfigError(f"hook(s) {', '.join(str(h['id']) for h in left)} "
                                             "still present after DELETE")
                hook_lines = [f"  hook {hook['id']} deleted" for hook in own]
            except config.ConfigError as exc:
                print(f"refused: the triage hook was not removed ({exc}); nothing else changed")
                return 2
        shims = gate_shims.remove({**loop, "seats": {}, "adjudicator": {}, "observer": {}},
                                  [updated, *[other for other in config.all_loops()
                                              if other["id"] != loop["id"]]])
        _write_config(updated)
        routes.restore_entries({old_name: None})
        route_intent.forget(loop, [old_name])
        print(f"[{loop['id']}] issue triage off: route {old_name} removed")
        for line in hook_lines + [f"  {line}" for line in shims]:
            print(line)
        if not admin:
            print(f"  the repo hook posting to {old_name} (if any) is left: it now gets 404s — "
                  f"delete it on GitHub, or re-run with --admin-token LOGIN")
        return 0

    current = loop.get("triage") or {}
    labels = ([x.strip() for x in args.labels.split(",") if x.strip()] if args.labels
              else current.get("labels"))
    block = {"route": old_name or routes_for(loop)["triage"],
             "profile": args.profile or current.get("profile") or "",
             "authors": args.author or current.get("authors") or [],
             "labels": labels or [],
             "max_labels": args.max_labels or current.get("max_labels", 3),
             "comment": (args.comment == "on") if args.comment else current.get("comment", False)}
    login = args.login or current.get("login")
    if login:
        block["login"] = login
    fix_label = current.get("fix_label") if args.fix_label is None else args.fix_label
    if fix_label:
        block["fix_label"] = fix_label
        block["maintainers"] = args.maintainer or current.get("maintainers") or []
        # The issue-fix daily cap (#247); omitted keeps the current one, 0 the default.
        fix_cap = (current.get("fix_daily_turns") if args.fix_daily_turns is None
                   else args.fix_daily_turns or None)
        if fix_cap:
            block["fix_daily_turns"] = fix_cap
        # Automatic hand-off (#232): omitted keeps the current list, an empty value clears it.
        auto = (current.get("auto_fix_labels") if args.auto_fix_label is None
                else [x for x in args.auto_fix_label if x])
        if auto:
            block["auto_fix_labels"] = auto
            auto_cap = (current.get("auto_fix_daily") if args.auto_fix_daily is None
                        else args.auto_fix_daily or None)
            if auto_cap:
                block["auto_fix_daily"] = auto_cap
    elif [x for x in args.auto_fix_label or [] if x] or args.auto_fix_daily:
        print("refused: --auto-fix-label/--auto-fix-daily need a fix label "
              "(--fix-label LABEL --maintainer LOGIN); nothing changed")
        return 2
    tokens = dict(loop.get("tokens") or {})
    for pair in args.token or []:
        if "=" not in pair:
            print(f"--token expects login=/path/to/pat, got {pair!r}")
            return 2
        who, file = pair.split("=", 1)
        tokens[who] = str(pathlib.Path(file).expanduser())
    seats = dict(loop.get("seats") or {})
    if args.daily_turns is not None:
        seat = {k: v for k, v in (seats.get("triage") or {}).items() if k != "daily_turns"}
        if args.daily_turns:
            seat["daily_turns"] = args.daily_turns
        seats["triage"] = seat
    try:
        updated = config.normalize({**loop, "triage": block, "tokens": tokens, "seats": seats})
        if not config.profile_exists(updated["triage"]["profile"]):
            raise config.ConfigError(f"no Hermes profile named {updated['triage']['profile']!r} "
                                     f"(looked in {config.profiles_root()})")
        config.verify_credentials(updated, roles={"read"})
        config.webhook_host(updated["host"], required=True)
        _verify_routes(updated, {"triage"})
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    for line in _triage_lines(updated):
        print(line)
    if args.dry_run:
        hook_note = ""
        if admin:
            try:
                existing = [h for h in _hook_listing(updated, admin, require_active=False)
                            if routes.route_name_of(h["config"]["url"]) == updated["triage"]["route"]]
            except config.ConfigError:
                existing = None
            if existing is None:
                hook_note = ", and reconcile the repo hook"
            elif existing:
                dest = routes.url_for(updated["triage"]["route"],
                                      config.webhook_host(updated.get("host"), required=True))
                kept, _rest = _keep_one(existing, dest)   # the same choice reconciliation makes
                state = "active" if kept.get("active") else "paused"
                hook_note = f", and keep hook {kept['id']} ({state}), repointing it if needed"
            else:
                hook_note = ", and create the repo hook (paused; next, arm)"
        print(f"  dry run — would write route {updated['triage']['route']} (issues) and its gate "
              "shim" + hook_note + "; nothing written")
        return 0
    name = updated["triage"]["route"]
    previous_route = routes.route(name)
    try:
        _write_config(updated)
        _install_routes(updated, roles=("triage",))
        route_intent.record_live(updated, [name])
    except Exception as exc:
        try:
            routes.restore_entries({name: previous_route})
            _restore_config(path, previous_config)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect {path} and route {name}")
            return 2
        print(f"triage install FAILED: {exc}; config and route restored")
        return 2
    print(f"  route written: {name}")
    shims_ok = _install_shims(updated)
    if admin:
        outcome: dict = {}
        rc = _ensure_hooks(updated, admin, dry_run=False, outcome=outcome)
        if rc:
            return rc
        kept = (outcome.get("kept") or {}).get("triage")
        if kept is not None:
            state = "active" if kept.get("active") else "paused"
            print(f"  hook {kept['id']} kept ({state})")
            if not kept.get("active"):
                print(f"  next: hermes dk arm --loop {loop['id']} --admin-token {admin} "
                      "(the hook is paused; arm turns every loop hook on)")
        else:
            print("  hook created (paused)")
            print(f"  next: hermes dk arm --loop {loop['id']} --admin-token {admin} "
                  "(arm turns every loop hook on)")
    else:
        print(f"  next: hermes dk apply --loop {loop['id']} --hooks --admin-token LOGIN "
              "creates the issues hook (paused), then `arm`")
    return 0 if shims_ok else 1

def cmd_fixer_push(args) -> int:
    """Change only this repository's unattended push permission by explicit operator action."""
    try:
        with config.push_policy_lock():
            return _cmd_fixer_push_locked(args)
    except (OSError, ValueError, config.ConfigError) as exc:
        print(f"fixer push policy update not confirmed: {exc}")
        return 2


def _cmd_fixer_push_locked(args) -> int:
    try:
        loop = config.load_id(args.loop)
        config.by_repo(loop['repo'])  # duplicate owners fail closed
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    if args.enable and not args.acknowledge_pr_race:
        print("refused: host-operator policy only (not verified GitHub owner/admin consent). "
              "Unattended fixer pushes have a residual PR-metadata/ref race: "
              "closing, drafting or retargeting a PR between the last check and Git's "
              "exact-SHA-lease push can still publish. Inspect the production boundary and "
              "pass --acknowledge-pr-race to opt in for this repository.")
        return 2
    if args.enable:
        try:
            busy = _busy_seats(loop, {"fixer"})
            from .run_supervisor import Supervisor, ACTIVE
            ledger = config.host_path("ledger")
            if ledger.exists():
                with Supervisor(ledger)._connect() as con:
                    rows = con.execute(
                        'SELECT pr,state FROM runs WHERE repo=? AND seat=? '
                        'AND state IN (?,?,?,?) LIMIT 1',
                        (loop['repo'], 'fixer', *ACTIVE)).fetchall()
                busy.extend(f"fixer supervisor on PR {row['pr']} ({row['state']})"
                            for row in rows)
        except (OSError, ValueError) as exc:
            print(f"refused: cannot check active fixer runs: {exc}")
            return 2
        if busy:
            print("refused: fixer run in flight; wait for it to finish before arming pushes: "
                  + "; ".join(busy))
            return 2
    enabled = bool(args.enable)
    if config.unattended_fixer_push_enabled(loop) == enabled:
        print(f"[{loop['id']}] unattended fixer push already {'enabled' if enabled else 'disabled'}")
        return 0
    updated = config.normalize({**loop, "unattended_fixer_push": enabled})
    if args.dry_run:
        print(f"[{loop['id']}] dry run — would {'enable' if enabled else 'disable'} "
              "unattended fixer pushes; nothing written")
        return 0
    try:
        path = _write_config_locked(updated, policy_change=True)
        actual = config.load_id(loop["id"])
        if actual["repo"] != loop["repo"] or config.unattended_fixer_push_enabled(actual) != enabled:
            raise config.ConfigError("readback does not match the requested repository/policy")
    except (OSError, ValueError, config.ConfigError) as exc:
        print(f"fixer push policy update not confirmed: {exc}")
        return 2
    print(f"[{loop['id']}] {loop['repo']}: host-operator unattended fixer push "
          f"{'enabled (not GitHub owner consent; residual PR-metadata/ref race acknowledged)' if enabled else 'disabled'} "
          f"in {path}")
    if enabled:
        try:
            from . import state as state_mod
            held = [key for key, entry in state_mod.state_for(actual).queue_items("fixer").items()
                    if config.is_fixer_push_hold(entry)]
        except Exception:
            held = []
        if held:
            print(f"  {len(held)} held verdict(s) ({', '.join(sorted(held))}) start on the next "
                  f"watchdog sweep, or now: `hermes dk drain --loop {loop['id']} "
                  "--seat fixer`")
    return 0


def cmd_drain(args) -> int:
    try:
        config.load_id(args.loop)          # the watchdog reports a bad file but exits 0 (cron)
    except config.ConfigError as exc:
        print(f"cannot drain: {exc}")
        return 2
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    cmd = [sys.executable, str(watchdog), "--loop", args.loop, "--drain", "--seat", args.seat]
    return subprocess.run(cmd).returncode


def cmd_cleanup(args) -> int:
    try:
        config.load_id(args.loop)          # refuse a loop that will not load here, by name
    except config.ConfigError as exc:
        print(f"cannot clean up: {exc}")
        return 2
    cleanup = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cleanup.py"
    cmd = [sys.executable, str(cleanup), "--loop", args.loop]
    cmd += ["--pr", str(args.pr)] if args.pr else ["--sweep"]
    if args.dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd).returncode


def _uninstall_preflight(loop: dict) -> list[str]:
    """Reasons a later uninstall step would fail on a shared file, read before anything is removed.

    The route registry is rewritten by step 3, after the hooks and the watchdog job are already
    gone; it must parse now. (The cron store was read just before this; the intent record's own
    reader tolerates a bad file — it heals nothing then.)
    """
    problems = []
    path = routes.subs_path()
    if path.exists() or path.is_symlink():
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            problems.append(f"route registry {path} cannot be read ({type(exc).__name__}: "
                            f"{exc}) — fix or restore it; nothing was removed")
        else:
            if not isinstance(data, dict):
                problems.append(f"route registry {path} is not a JSON object — fix or restore "
                                "it; nothing was removed")
    return problems


def _unloadable_teardown_advice(loop_id: str) -> None:
    """For a loop file the loader refuses: what may still be live, and how to find it.

    ``uninstall`` will not act on a config it cannot validate — deleting hooks or jobs on a
    guess is the wrong way to fail — but it can still read the raw file for the repo, the route
    names and the job name, and hand over the commands to look them up.
    """
    try:
        raw = json.loads((config.config_dir() / f"{loop_id}.json").read_text())
    except Exception:
        return
    if not isinstance(raw, dict) or str(raw.get("repo") or "").count("/") != 1:
        return
    raw = {**raw, "id": loop_id, "repo": str(raw["repo"]).strip().lower()}
    print("it may still have live repo hooks and a watchdog job; nothing was touched. "
          "Look them up with:")
    if _hook_route_names(raw):
        print(f"  {_hook_find_command(raw)}   # hook ids on its route names — check each URL "
              "before deleting: `gh api -X DELETE repos/<owner>/<repo>/hooks/<id>`")
    jobs, _ = _cron_jobs(raw)
    for job in jobs or []:
        print(f"  hermes cron remove {shlex.quote(str(job.get('id') or '<id>'))}   "
              f"# {watchdog_job_name(raw)}")
    print("then fix the file (the reason above) and re-run `hermes dk uninstall "
          f"--loop {shlex.quote(loop_id)}`, or remove what is listed by hand and delete "
          f"{config.config_dir() / (loop_id + '.json')}")


def _uninstall_incomplete(removed: list[str], left: list[str], keep_hooks: bool,
                          commands: list[str]) -> int:
    """A late step failed after earlier ones were done: say which, and how to finish. Exit 2.

    Past this point the config may be gone, so a re-run cannot finish the job — the printed
    commands do.
    """
    print("uninstall INCOMPLETE — removed: " + (", ".join(removed) or "nothing else")
          + "; left behind: " + ("the repo hooks (--keep-hooks), " if keep_hooks else "")
          + ", ".join(left))
    print("fix what refused (the permissions of the path above), then finish with:")
    for command in commands:
        print(f"  {command}")
    return 2


def _uninstall_refused(loop: dict, reasons: list[str], left_hooks: list[int],
                       args, unattributed: bool = False, accepted_unread: bool = False) -> int:
    """Refuse an uninstall with the exact commands that finish it. The config is still there.

    ``accepted_unread`` is the structured fact that every DELETE GitHub accepted but the listing
    read-back could not confirm: the caller knows it, so the remedy is chosen by that flag rather
    than by pattern-matching a reason's wording (#131). Rewording the message that produced a
    reason must not silently change which remedy the operator is given.
    """
    lid = shlex.quote(loop["id"])
    print("refused: uninstall stopped before removing routes or config — nothing below the "
          "failure was touched:")
    for reason in reasons:
        print(f"  {reason}")
    if unattributed:
        print("they may be this install's or another install's (same repo, same route names): "
              "look at each one's URL before deleting anything:")
        print(f"  {_hook_find_command(loop)}   # the ids")
        print(f"  gh api {shlex.quote(f'repos/' + loop['repo'] + '/hooks/<id>')} --jq .config.url"
              "   # where each posts")
        print("then either set this loop's host (`hermes dk set --loop "
              f"{lid} --host https://your-gateway.example`) so uninstall can tell its own hooks "
              "apart, or delete the ones that are this install's by hand")
    elif accepted_unread and not left_hooks:
        # Chosen by the caller's structured fact, never by the reason's wording: a reworded
        # message must not flip this honest case into a different remedy (#131).
        print("every DELETE was accepted, but the hook listing could not be read back to confirm "
              "it — look before re-running (it lists any id that is somehow still there):")
        print(f"  {_hook_find_command(loop)}")
    elif left_hooks:
        # Only ids a listing actually showed at this loop's route URLs are called live, and only
        # they get DELETE commands.
        print("the loop's repo hooks are still live. Delete them with a token that has "
              "`admin:repo_hook` (or classic `repo`):")
        for command in _hook_delete_commands(loop, left_hooks):
            print(f"  {command}")
        print("or map such a token on this loop and let uninstall do it:")
        print(f"  hermes dk uninstall --loop {lid} --admin-token <login>")
    elif any(reason.startswith(("hook", "could not")) for reason in reasons):
        # Nothing was read (or read back), so nothing is known about which hooks exist — or
        # whose they are. No liveness claim, and no DELETE on a guess.
        print("the hook listing could not be read or confirmed, so this command does not know "
              "which hooks exist; nothing was deleted on a guess. Look first — check each one's "
              "URL before deleting anything:")
        print(f"  {_hook_find_command(loop)}   # the ids")
        print(f"  gh api {shlex.quote(f'repos/' + loop['repo'] + '/hooks/<id>')} --jq .config.url"
              "   # where each posts")
    if any(reason.startswith("cron") for reason in reasons):
        jobs, _ = _cron_jobs(loop)
        for job in jobs or []:
            print(f"  hermes cron remove {shlex.quote(str(job.get('id') or '<id>'))}")
    print("then re-run:")
    admin = getattr(args, "admin_token", "") or ""
    print(f"  hermes dk uninstall --loop {lid}"
          + (f" --admin-token {shlex.quote(admin)}" if admin else "")
          + (" --keep-config" if args.keep_config else "")
          + (" --purge" if getattr(args, "purge", False) else ""))
    print("(`--keep-hooks` uninstalls anyway and leaves the hooks live — they keep posting to "
          "routes that no longer exist)")
    return 2


def cmd_uninstall(args) -> int:
    """The inverse of ``init``: hooks, cron job, routes, config and (``--purge``) state.

    Order matters. Everything that needs the loop config — its hook routes, its token mapping, its
    job name — is undone *first*, while the config still exists; if any of it cannot be done the
    command refuses with the commands that finish it, and the config stays so they still run.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"cannot uninstall: {exc}")
        _unloadable_teardown_advice(args.loop)
        return 2
    keep_hooks = getattr(args, "keep_hooks", False)
    purge = getattr(args, "purge", False)
    admin = getattr(args, "admin_token", "") or ""
    target = None
    if purge:
        if args.keep_config:
            print("refused: --purge removes the state the kept config points at; drop one of "
                  "--purge / --keep-config")
            return 2
        target, why = _purge_target(loop)
        if target is None:
            print(f"refused: {why}")
            return 2
        try:
            busy = _busy_seats(loop, {"reviewer", "fixer"})
        except OSError as exc:
            # Reading the in-flight marks takes the state lock inside the directory about to be
            # deleted; if that cannot be opened, nothing can be proved idle — refuse, untouched.
            where = f" ({exc.filename})" if getattr(exc, "filename", None) else ""
            print(f"refused: --purge cannot check for a run in flight — the state directory "
                  f"{target} cannot be read: {exc.strerror or exc}{where}. Nothing was removed. "
                  "Fix its permissions and re-run, or uninstall without --purge and remove it "
                  f"by hand afterwards: rm -rf -- {shlex.quote(str(target))}")
            return 2
        if busy:
            print("refused: --purge would delete the state of a run in flight: " + "; ".join(busy))
            return 2
    if admin and gh.token_path(loop, admin) is None:
        print(f"refused: --admin-token {admin!r} has no token file mapped on this loop — map it "
              f"with `hermes dk set --loop {shlex.quote(loop['id'])} --token "
              f"{shlex.quote(admin)}=/abs/path/to/pat`")
        return 2
    jobs, error = _cron_jobs(loop)
    if jobs is None:
        return _uninstall_refused(loop, [f"cron: {error}"], [], args)
    # Every shared file a later step rewrites is read *now*, before a hook or a job is touched:
    # a registry that will not parse must refuse here, not raise after the destructive steps.
    problems = _uninstall_preflight(loop)
    if problems:
        return _uninstall_refused(loop, problems, [], args)
    # What is already gone, for the summary if a later step (the state purge) fails.
    removed: list[str] = []
    # 1. Stop deliveries: delete the repo hooks while the config still names their routes.
    if keep_hooks:
        print("hooks: kept (--keep-hooks) — they stay live and post to routes about to be removed;"
              " find them with:")
        print(f"  {_hook_find_command(loop)}")
    elif not str(loop.get("host") or "").strip():
        # A host-less loop has no gateway origin to tell its own hooks from another install's —
        # and it may once have had one (a host blanked by hand). So look before skipping: any hook
        # posting to its route names may be this install's, live, and is refused with the
        # commands that delete it; only an empty answer lets the uninstall go on.
        listing, error = gh.hooks_read(loop, admin or loop.get("read_token"))
        if error:
            return _uninstall_refused(loop, [f"could not read the repo's hooks: {error}"], [], args)
        matching = [hook for hook in listing
                    if not isinstance(hook, dict) or doctor.hook_route_name(hook)
                    in _hook_route_names(loop)]
        if any(not isinstance(hook, dict) or not isinstance(hook.get("id"), int)
               for hook in matching):
            # _classify_hooks' rule: an entry that cannot be identified makes the listing
            # untrustworthy — never "no hooks".
            return _uninstall_refused(loop, ["could not read the repo's hooks: invalid hook "
                                             "listing (an entry on the loop's route names has no "
                                             "integer id)"], [], args)
        named = sorted(hook["id"] for hook in matching)
        if named:
            # Without a host there is no route URL to compare with, so none of these can be
            # attributed to this install: never hand out DELETE commands for them.
            return _uninstall_refused(loop, [
                f"hooks {', '.join(map(str, named))} post to this loop's route names, and the loop "
                "has no host to tell whether they are this install's (a blanked host leaves its "
                "hooks behind) or another install's on the same repo — nothing was deleted"],
                [], args, unattributed=True)
        print("hooks: none — the loop has no host, and no repo hook posts to its route names")
    else:
        done, failures, left, accepted_unread = _delete_loop_hooks(loop, admin or None)
        for line in done:
            print(line)
        if failures or left:
            if left and not failures:
                failures = [f"hook {hook_id} still present after DELETE" for hook_id in left]
            return _uninstall_refused(loop, failures, left, args,
                                      accepted_unread=accepted_unread)
        if not done:
            print("hooks: none of this loop's routes has a repo hook")
        elif any(line.endswith(" deleted") for line in done):
            removed.append("repo hooks")
    # 2. Stop the watchdog.
    done, failures = _remove_cron(loop)
    for line in done:
        print(line)
    if failures:
        return _uninstall_refused(loop, failures, [], args)
    if any(line.startswith("cron job removed") for line in done):
        removed.append("watchdog job")
    shim_line = _remove_unused_shim()
    leftovers: list[tuple[str, str]] = []          # (what, the command that removes it)
    if shim_line:
        print(shim_line)
        if shim_line.startswith("cron shim removed"):
            removed.append("cron shim")
        else:
            shim = config.watchdog_shim()
            leftovers.append((f"the cron shim {shim}", f"rm -- {shlex.quote(str(shim))}"))
    # 3. Forget first: a route the operator removed must not be put back by the next watchdog
    # sweep's self-heal (which only ever restores routes still in the intent record).
    lid = shlex.quote(loop["id"])
    finish = f"hermes dk uninstall --loop {lid}"
    try:
        route_intent.forget(loop, _routes_of(loop).values())
        for name in _routes_of(loop).values():
            if name and routes.remove_route(name):
                print(f"route removed: {name}")
                if "routes" not in removed:
                    removed.append("routes")
    except (OSError, ValueError) as exc:
        # Hooks and the job are already gone; the config is still here, so a re-run finishes
        # once the registry (or intent record) can be written — say so, never a traceback.
        print(f"routes NOT removed: {exc}")
        left = [what for what, _ in leftovers] + [
            f"the routes {', '.join(n for n in _routes_of(loop).values() if n)} in "
            f"{routes.subs_path()}", f"the loop config (so `{finish}` can finish)"]
        commands = [cmd for _, cmd in leftovers] + [f"{finish}   # once the file above is "
                                                    "readable and writable again"]
        return _uninstall_incomplete(removed, left, keep_hooks, commands)
    try:
        others = [other for other in config.all_loops()
                  if not (other["id"] == loop["id"] and other.get("repo") == loop.get("repo"))]
        for line in gate_shims.remove(loop, others):
            print(line)
    except (OSError, config.ConfigError) as exc:
        print(f"gate shims left in place (another loop may still need them): {exc}")
    if not args.keep_config:
        path = config.config_dir() / f"{loop['id']}.json"
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                # Hooks, cron and routes are already gone: report the half-done state and the
                # one command that finishes it, never a traceback.
                print(f"config NOT removed: {path} — {exc.strerror or exc}")
                left = [what for what, _ in leftovers] + [f"the loop config {path}"]
                commands = [cmd for _, cmd in leftovers] + [f"rm -f -- {shlex.quote(str(path))}"]
                if target is not None:
                    left.append(f"the state directory {target} (not attempted)")
                    commands.append(f"rm -rf -- {shlex.quote(str(target))}")
                return _uninstall_incomplete(removed, left, keep_hooks, commands)
            print(f"config removed: {path}")
            removed.append("config")
    if target is not None:
        if target.is_symlink():
            # Swapped for a link after the preflight vetted it: never followed. Everything above
            # is already done, so this is a partial decommission, reported as one.
            print(f"state NOT removed: {target} became a symlink after it was checked — never "
                  "followed")
            return _uninstall_incomplete(
                removed, [what for what, _ in leftovers]
                + [f"the state directory {target} (now a symlink: check where it points, "
                   "remove the link, and that directory by hand if it is this loop's)"],
                keep_hooks, [cmd for _, cmd in leftovers]
                + [f"ls -ld -- {shlex.quote(str(target))}   # where it points",
                   f"rm -- {shlex.quote(str(target))}   # the link itself"])
        if target.exists():
            refused: list[tuple[str, str]] = []

            def note(_func, path, exc_info) -> None:
                # rmtree's own exception names only the entry, relative to an open directory:
                # keep the full path of every refusal, and delete everything else it can.
                exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
                refused.append((str(path), getattr(exc, "strerror", None) or str(exc)))

            try:
                if sys.version_info >= (3, 12):
                    shutil.rmtree(target, onexc=note)
                else:
                    shutil.rmtree(target, onerror=note)
            except OSError as exc:
                refused.append((str(getattr(exc, "filename", None) or target),
                                exc.strerror or str(exc)))
            if refused or target.exists():
                # Everything above is already undone and the config is gone, so a re-run cannot
                # finish this: say what happened and hand over the one command that does.
                path, why = refused[0] if refused else (str(target), "still present")
                more = f" (and {len(refused) - 1} more)" if len(refused) > 1 else ""
                print(f"state NOT removed: {target} — {why}: {path}{more}")
                return _uninstall_incomplete(
                    removed, [what for what, _ in leftovers]
                    + [f"the state directory {target} (whatever the delete could remove "
                       "is gone; the rest is still there)"],
                    keep_hooks, [cmd for _, cmd in leftovers]
                    + [f"rm -rf -- {shlex.quote(str(target))}"])
            print(f"state removed: {target}")
    elif not args.keep_config:
        _, why = _purge_target(loop)
        raw = pathlib.Path(str(loop.get("state_dir") or "")).expanduser()
        if why:
            print(f"state kept: {raw} — not the default location or not a plain directory, so "
                  f"--purge would not remove it; check it, then: rm -rf -- {shlex.quote(str(raw))}")
        else:
            print(f"state kept: {raw} (pass --purge to remove it)")
    if not args.keep_config:
        # The last loop gone (#113): forget that a run ledger existed, so a later fresh install
        # is not reported as a vanished ledger (run_supervisor.presence_marker). After the state
        # step, so a --purge has removed the state dir first.
        from .run_supervisor import forget_ledger_presence, presence_marker
        if not any(config.config_dir().glob("*.json")) and forget_ledger_presence():
            print(f"ledger presence marker removed: {presence_marker()}")
    if leftovers:
        # Everything else went; the shim did not. That is not a clean uninstall.
        return _uninstall_incomplete(removed, [what for what, _ in leftovers], keep_hooks,
                                     [cmd for _, cmd in leftovers])
    return 0


# What the plugin's settings form held when this process started (Capabilities → Plugins in the
# desktop). Set by `register_cli`; read by `apply` and `settings`. A form that silently renumbered
# a running loop would be a nasty thing to debug, so these are *defaults* and a push — never a
# subscription.
_SETTINGS: dict = {}
# The Hermes plugin context register_cli was given: `migrate` writes settings through it (#425).
_CTX = None


RENAMED_NOTE = ("note: `hermes review-loop` is now `hermes dk` (short for `hermes diaktoros`); "
                "the old name will be removed in a later release")


def _renamed(func):
    """``func`` run through the old command name: the rename note on stderr, then the verb."""
    def run(args):  # noqa: ANN001
        print(RENAMED_NOTE, file=sys.stderr)
        return func(args)
    return run


def register_cli(ctx, settings: dict | None = None) -> None:
    """Wire ``hermes diaktoros``, its short name ``hermes dk``, and the old ``hermes review-loop``.

    ``settings`` is the plugin-level settings form. It supplies the defaults a new loop starts
    from, and what ``apply`` pushes onto an existing loop; it never rewrites a loop behind the
    operator's back.
    """
    global _SETTINGS, _CTX
    _SETTINGS = dict(settings or {})
    _CTX = ctx
    d = config.settings_defaults(settings)
    # Both seats agreeing is the only case where a loop-level default says anything useful: when
    # they disagree the differing seat carries its value explicitly (see _init_seat_concurrency).
    both = config.settings_loop_concurrency(d)

    def setup(parser) -> None:  # noqa: ANN001
        """Build the command's argparse tree.

        The framework hands this the parser for ``hermes dk`` itself — the subcommands are
        ours to create. (Claiming a subparsers action here compiles, loads, validates, and then
        quietly offers zero subcommands: ``hermes dk list`` is "unrecognized arguments".)
        """
        sub = parser.add_subparsers(dest="command", metavar="<command>")
        global _PARSER
        _PARSER = parser

        def _usage(_args) -> int:      # bare `hermes dk` prints the commands, not an error
            parser.print_help()
            return 0

        parser.set_defaults(func=_usage)
        sub.add_parser("list", help="List configured loops").set_defaults(func=cmd_list)

        init = sub.add_parser("init", help="Configure a loop and install its routes")
        init.add_argument("--repo", required=True, help="owner/name")
        init.add_argument("--id", help="loop id (default: the repository name)")
        init.add_argument("--fixer", action="append", default=[],
                          help="GitHub login that pushes (repeatable; default: the plugin setting)")
        init.add_argument("--reviewer", action="append", default=[],
                          help="GitHub login that may review (repeatable; default: the plugin setting)")
        init.add_argument("--reviewer-seat", help="the login the reviewer route serves")
        init.add_argument("--reviewer-profile", default=d["reviewer_profile"],
                          help="Hermes profile for the reviewer seat (default: the plugin setting)")
        init.add_argument("--fixer-profile", default=d["fixer_profile"],
                          help="Hermes profile for the fixer seat (default: the plugin setting)")
        init.add_argument("--reviewer-agent", default="", help="display name for the reviewer (default: profile)")
        init.add_argument("--fixer-agent", default="", help="display name for the fixer")
        init.add_argument("--cap", type=int, default=d["cap"],
                          help="verdicts allowed before adjudication")
        # None, not the settings value: a flag nobody passed must not be written as a seat
        # override, or the loop default is dead for that seat forever (#76). cmd_init fills in the
        # settings form's values at read time.
        init.add_argument("--concurrency", type=int, default=None,
                          help=f"default PRs per seat at once: 1 = serialized (default: {both}, "
                               "from the plugin settings). Above 1 needs --clone, because each "
                               "run then gets its own clone. Override per seat with "
                               "--reviewer-concurrency / --fixer-concurrency.")
        init.add_argument("--reviewer-concurrency", type=int, default=None,
                          help="PRs the reviewer may work at once (overrides --concurrency; "
                               f"default: the loop's, settings {d['reviewer_concurrency']})")
        init.add_argument("--fixer-concurrency", type=int, default=None,
                          help="PRs the fixer may work at once (overrides --concurrency; "
                               f"default: the loop's, settings {d['fixer_concurrency']})")
        init.add_argument("--base", default=d["base"],
                          help="the branch PRs target; only PRs against it are reviewed")
        init.add_argument("--clone", default=d["clone"], help="local clone the runs may use")
        init.add_argument("--root", action="append", default=[], help="a directory reviews may clean (repeatable)")
        init.add_argument("--state-dir", default="",
                          help="where this loop keeps its state files (default: "
                               "~/.hermes/state/review-loops/<id>)")
        init.add_argument("--token", action="append", default=[], help="login=/path/to/pat (repeatable)")
        init.add_argument("--read-token", default="",
                          help="required: login whose token reads GitHub — its own account, never "
                               "a seat or the adjudicator login (the four-identity rule)")
        init.add_argument("--skill", default="",
                          help="skill the seats are told to load. A plugin-provided skill is "
                               "qualified, e.g. diaktoros:review-loop")
        init.add_argument("--adjudicator-route", default="",
                          help="route name for the adjudicator (e.g. <id>-breach): setting it "
                               "turns adjudication on when the verdict cap is spent")
        init.add_argument("--adjudicator-login", default=None,
                          help="optional fourth GitHub account the ruling is also posted as (needs "
                               "--adjudicator-route and its own --token LOGIN=/path)")
        init.add_argument("--adjudicator-profile", default="",
                          help="Hermes profile for the adjudicator (default: the plugin setting, "
                               "else the launch profile)")
        init.add_argument("--observer-route", default="",
                          help="route name for the read-only observer feed "
                               "(default: <id>-observe)")
        init.add_argument("--observer-profile", default="",
                          help="Hermes profile the observer feed belongs to (its chat) — naming "
                               "one switches the feed on")
        init.add_argument("--observer-deliver", default="telegram",
                          help="where the gateway delivers the feed (telegram, discord, ...); "
                               "the feed never wakes an agent")
        init.add_argument("--observer-events", default="",
                          help="comma-separated transitions to send, from "
                               "opened,handoff,verdict,approved,escalation,ruling,stall,closed,"
                               "triaged,fixing,fixed,failed,held,conflict,ci_failed,updated,main_red,stale_approval,human_paths "
                               "(default: all)")
        init.add_argument("--observer-digest-min", type=int, default=0,
                          help="batch the feed into one message per this many minutes "
                               "(0 = one notice per transition)")
        init.add_argument("--observer-urgent-route", default="",
                          help="second route for urgent notices (failed, held, escalation, ruling, "
                               "stall, conflict, uncertain); routine ones keep the main feed")
        init.add_argument("--observer-urgent-profile", default="",
                          help="profile for the urgent route (default: the observer profile)")
        init.add_argument("--observer-urgent-deliver", default="",
                          help="where the gateway delivers urgent notices (default: the feed's)")
        init.add_argument("--host", default=d["host"],
                          help="your gateway webhook origin (required unless set in plugin settings)")
        init.add_argument("--grace-min", type=int, default=d["grace_min"],
                          help="minutes a PR may sit quiet before the watchdog reports a stall")
        init.add_argument("--ttl-min", type=int, default=d["ttl_min"],
                          help="how long a run may hold its seat slot")
        init.add_argument("--inflight-ttl-min", type=int, default=d["inflight_ttl_min"],
                          help="how long an in-flight mark blocks a second run at the same head")
        init.add_argument("--review-only-update", choices=("on", "off"), default=None,
                          help="push a clean merge of the base into a review-only author's PR "
                               "branch (same repository only; off by default; turning it on "
                               "needs --acknowledge-branch-push)")
        init.add_argument("--acknowledge-branch-push", action="store_true",
                          help="accept that the host pushes to a branch the loop does not own; "
                               "required to turn --review-only-update on")
        init.add_argument("--review-after-ci", choices=("on", "off"), default=None,
                          help="start each review after the head's checks finish (up to an hour) "
                               f"(default {'on' if d['review_after_ci'] else 'off'})")
        init.add_argument("--fix-ci", choices=("on", "off"), default=None,
                          help="hand a failed required check on a fixer's PR to the fixer, one "
                               "turn per head; needs unattended fixer pushes "
                               f"(default {'on' if d['fix_ci'] else 'off'})")
        init.add_argument("--attribution", choices=("on", "off"), default=None,
                          help="sign what the loop posts with 'Automated by Diaktoros' "
                               f"(default {'on' if d['attribution'] else 'off'})")
        init.add_argument("--review-only-cap", default=None, metavar="N",
                          help="verdicts the reviewer gives one review-only PR before it waits "
                               "for `review --another-round`, 1-1000 (default: the plugin "
                               "setting, else the review cap)")
        init.add_argument("--ci-fix-cap", default=None, metavar="N",
                          help="CI-fix turns per PR before a red head goes to the reviewer, 1-10 "
                               "(default: the plugin setting, else 3)")
        init.add_argument("--review-only-daily", default=None, metavar="N",
                          help="reviewer turns a day on review-only PRs, 1-1000 (default: the "
                               "plugin setting, else no cap)")
        init.add_argument("--review-only", action="append", default=None,
                          help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it) (default: the plugin setting)")
        init.add_argument("--required-check", action="append", default=None,
                          help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting)")
        init.add_argument("--human-path", action="append", default=None,
                          help="a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting)")
        init.add_argument("--fixer-check", default=None,
                          help="one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none) (default: the plugin setting)")
        init.add_argument("--turn-budget", type=int, default=d["turn_budget_s"],
                          help="seconds one isolated seat turn may run, build and tests included "
                               f"(default {d['turn_budget_s']}; the sandbox is killed past it)")
        init.add_argument("--reviewer-turn-budget", type=int, default=None,
                          help="the reviewer seat's own turn budget in seconds (overrides --turn-budget)")
        init.add_argument("--fixer-turn-budget", type=int, default=None,
                          help="the fixer seat's own turn budget in seconds (overrides --turn-budget)")
        init.add_argument("--reviewer-max-steps", type=int, default=None,
                          help="agent steps one reviewer turn may take, 8-200 (0 = default 60) "
                               "(default: the plugin setting)")
        init.add_argument("--fixer-max-steps", type=int, default=None,
                          help="agent steps one fixer or issue-fix turn may take, 8-200 "
                               "(0 = default 80) (default: the plugin setting)")
        init.add_argument("--fix-daily-turns", type=int, default=None,
                          help="issue-fix turns per day: refused here, a new loop has no fix "
                               "label; set it with `triage --fix-daily-turns N` (0 = ignore)")
        init.add_argument("--hooks", action="store_true",
                          help="create the GitHub hooks too, paused until `arm`")
        init.add_argument("--arm", action="store_true",
                          help="with --hooks: create them armed (live at once) instead of paused")
        init.add_argument("--admin-token", default="", help="login whose token can create hooks")
        init.add_argument("--schedule", default="", help="e.g. 15m — install the watchdog cron job")
        init.add_argument("--watchdog-deliver", default="local", help="cron delivery target for watchdog alerts")
        init.add_argument("--dry-run", action="store_true",
                          help="print the seat mapping and what would be written, write nothing")
        init.set_defaults(func=cmd_init)

        first = sub.add_parser("setup", help="First install in one command: runtime paths, init, "
                                             "watchdog, doctor, selftest, then arm when asked "
                                             "(safe to re-run)")
        first.add_argument("--repo", help="owner/name (asked when not given)")
        first.add_argument("--id", help="loop id (default: the repository name)")
        first.add_argument("--yes", action="store_true",
                           help="no questions: the flags and the plugin settings are the answers, "
                                "and every confirmation is yes (arming still needs --arm)")
        first.add_argument("--dry-run", action="store_true", help="show every step, write nothing")
        first.add_argument("--arm", action="store_true",
                           help="arm the hooks after a clean doctor and selftest")
        for flag, what in (("reviewer", "reviewer GitHub login"), ("fixer", "fixer GitHub login"),
                           ("reviewer-profile", "reviewer's Hermes profile"),
                           ("fixer-profile", "fixer's Hermes profile"),
                           ("reviewer-token", "reviewer's token file"),
                           ("fixer-token", "fixer's token file"),
                           ("read-token", "reader login (its own account)"),
                           ("read-token-file", "reader's token file"),
                           ("host", "your gateway's webhook origin"),
                           ("admin-token-file", "hook admin's token file"),
                           ("schedule", "watchdog interval (default 15m)"),
                           ("watchdog-deliver", "where watchdog alerts go (default local)")):
            first.add_argument(f"--{flag}", default="", help=what)
        first.add_argument("--admin-token", default=None,
                           help="hook admin login: the hooks are created (paused) as it")
        first.add_argument("--observer-profile", default=None,
                           help="Hermes profile whose chat gets the loop's notices")
        first.add_argument("--adjudicator-profile", default=None,
                           help="Hermes profile that rules when a PR's verdict cap is spent; "
                                "turns adjudication on (blank: off) (default: the plugin setting)")
        first.add_argument("--review-only-cap", default=None, metavar="N",
                           help="verdicts the reviewer gives one review-only PR before it waits "
                                "for `review --another-round`, 1-1000 (default: the plugin "
                                "setting, else the review cap)")
        first.add_argument("--ci-fix-cap", default=None, metavar="N",
                           help="CI-fix turns per PR before a red head goes to the reviewer, 1-10 "
                                "(default: the plugin setting, else 3)")
        first.add_argument("--review-only-daily", default=None, metavar="N",
                           help="reviewer turns a day on review-only PRs, 1-1000 (default: the "
                                "plugin setting, else no cap)")
        first.add_argument("--review-only", action="append", default=None,
                           help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it) (default: the plugin setting)")
        first.add_argument("--required-check", action="append", default=None,
                           help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting)")
        first.add_argument("--human-path", action="append", default=None,
                           help="a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting)")
        first.add_argument("--fixer-check", default=None,
                           help="one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none) (default: the plugin setting)")
        first.add_argument("--review-only-update", choices=("on", "off"), default=None,
                           help="push a clean merge of the base into a review-only author's PR "
                                "branch (same repository only; off by default; turning it on "
                                "needs --acknowledge-branch-push)")
        first.add_argument("--acknowledge-branch-push", action="store_true",
                           help="accept that the host pushes to a branch the loop does not own; "
                                "required to turn --review-only-update on")
        first.add_argument("--review-after-ci", choices=("on", "off"), default=None,
                           help="start each review after the head's checks finish (up to an hour) (default: the plugin setting, off)")
        first.add_argument("--fix-ci", choices=("on", "off"), default=None,
                           help="hand a failed required check on a fixer's PR to the fixer "
                                "(default: the plugin setting, off)")
        first.add_argument("--attribution", choices=("on", "off"), default=None,
                           help="sign what the loop posts with 'Automated by Diaktoros' "
                                "(default: the plugin setting, on)")
        first.add_argument("--reviewer-max-steps", type=int, default=None,
                           help="agent steps one reviewer turn may take, 8-200 (0 = default 60) "
                                "(default: the plugin setting)")
        first.add_argument("--fixer-max-steps", type=int, default=None,
                           help="agent steps one fixer or issue-fix turn may take, 8-200 "
                                "(0 = default 80) (default: the plugin setting)")
        first.add_argument("--fix-daily-turns", type=int, default=None,
                           help="issue-fix turns per day: refused here, a new loop has no fix "
                                "label; set it with `triage --fix-daily-turns N` (0 = ignore)")
        for key in ("source", "venv", "runtime", "rust"):
            first.add_argument(f"--{key}", default="",
                               help=f"runtime file's {key} path (default: detected)")
        first.set_defaults(func=cmd_setup)

        status = sub.add_parser("status", help="Show a loop's config and live state")
        status.add_argument("--loop", help="loop id (default: every configured loop)")
        status.set_defaults(func=cmd_status)

        stats_cmd = sub.add_parser("stats", help="What the loop did over a window: seat turns, "
                                                 "how long they ran, and (with --github) its PRs "
                                                 "and reviews")
        stats_cmd.add_argument("--loop", help="loop id (default: the only configured loop)")
        stats_cmd.add_argument("--since", default="7d",
                               help="window start: 7d, 24h or a date like 2026-09-28 (default 7d)")
        stats_cmd.add_argument("--github", action="store_true",
                               help="also read the window's PRs and reviews from GitHub, as the "
                                    "reader (one request per PR)")
        stats_cmd.add_argument("--json", action="store_true", help="print the data as JSON")
        stats_cmd.add_argument("--html", metavar="FILE",
                               help="also write one self-contained HTML page to FILE")
        stats_cmd.set_defaults(func=cmd_stats)

        review = sub.add_parser("review", help="Ask for a fresh review of a PR's current head "
                                               "(the reviewer gate decides, as for a webhook)")
        review.add_argument("--loop", help="loop id (default: the only configured loop)")
        review.add_argument("--pr", type=int, required=True, help="the pull request to review")
        review.add_argument("--another-round", action="store_true",
                            help="allow exactly one more verdict on a review-only PR that has "
                                 "reached its review cap (maintainer or operator only)")
        review.set_defaults(func=cmd_review)

        escalate = sub.add_parser("escalate", help="Send one PR to the adjudicator now, whatever "
                                                   "its verdict count (#459)")
        escalate.add_argument("--loop", help="loop id (default: the only configured loop)")
        escalate.add_argument("--pr", type=int, required=True, help="the pull request to escalate")
        escalate.add_argument("--reason", default="", help="why, for the ruling's record (one line)")
        escalate.set_defaults(func=cmd_escalate)

        explain = sub.add_parser("explain",
                                 help="Why one PR is not moving, and what has to happen next")
        explain.add_argument("--loop", help="loop id (default: the only configured loop)")
        explain.add_argument("--pr", type=int, required=True, help="pull request number to explain")
        explain.set_defaults(func=cmd_explain)

        tracer = sub.add_parser("trace", help="Dry-run one webhook through its gate: why it would "
                                              "(or would not) start a run")
        tracer.add_argument("--loop", required=True,
                            help="loop id (its config file name; `list` shows them)")
        source = tracer.add_mutually_exclusive_group(required=True)
        source.add_argument("--delivery", help="a recorded delivery to this loop's hooks: GitHub's "
                                               "numeric id or the X-GitHub-Delivery GUID")
        source.add_argument("--payload", help="a webhook payload JSON file instead")
        tracer.add_argument("--event", choices=("pull_request", "pull_request_review", "issues"),
                            help="with --payload: the event it was (default: read from the payload)")
        tracer.add_argument("--route", help="the route it was sent to (default: from the delivery's "
                                            "hook, or the event)")
        tracer.add_argument("--admin-token", default="",
                            help="login whose token can read hook deliveries (admin:repo_hook or repo)")
        tracer.set_defaults(func=cmd_trace)

        move = sub.add_parser("migrate", help="Move an install from the hermes-review-loop plugin to "
                                              "this one: settings, a renamed repository, shims (#425)")
        move.add_argument("--dry-run", action="store_true",
                          help="report every step and write nothing")
        move.add_argument("--rename-loop", metavar="OLD=NEW", default=None,
                          help="also give a loop a new id: its file, default state directory, "
                               "routes and the URLs its repo hooks post to (each hook is pinged "
                               "before the old route goes)")
        move.add_argument("--admin-token", default=None, metavar="LOGIN",
                          help="mapped login whose token may edit the repo hooks (--rename-loop)")
        move.set_defaults(func=cmd_migrate)

        bak = sub.add_parser("backup", help="Write one 0600 archive of everything the plugin owns "
                                            "(loops, ledger, state, routes, watchdog job) (#496)")
        bak.add_argument("--out", metavar="FILE", default=None,
                         help="where to write it (default: $HERMES_HOME/backups/…); never "
                              "overwrites a file")
        bak.set_defaults(func=cmd_backup)

        rest = sub.add_parser("restore", help="Put a backup back: files, routes, shims, the "
                                              "watchdog job; ends with doctor (#496)")
        rest.add_argument("file", metavar="FILE", help="an archive `backup` wrote")
        rest.add_argument("--dry-run", action="store_true",
                          help="report what would be restored and overwritten; write nothing")
        rest.add_argument("--force", action="store_true",
                          help="overwrite existing state (refused without it)")
        rest.add_argument("--allow-state-dir", action="append", default=None, metavar="DIR",
                          help="write this loop state directory, which the archive declares "
                               "outside the Hermes home (the dry run lists them; repeat per "
                               "directory)")
        rest.set_defaults(func=cmd_restore)

        preflight = sub.add_parser("doctor", help="Preflight a loop read-only: profiles, tokens, "
                                                  "routes, hooks, scripts, cron, clone")
        preflight.add_argument("--loop", help="loop id (default: every configured loop)")
        preflight.add_argument("--offline", action="store_true",
                               help="skip the two network probes (gateway reachability, repo hooks)")
        preflight.add_argument("--strict", action="store_true",
                               help="treat a check that could not be decided as a failure")
        preflight.add_argument("--repair", action="store_true",
                               help="restore this loop's own routes from the plugin's intent record "
                                    "(same secret) before checking; the only write doctor makes")
        preflight.set_defaults(func=cmd_doctor)

        check = sub.add_parser("selftest", help="Verify the live isolated path step by step "
                                                "(runtime, bwrap, model, identities, broker, ledger); "
                                                "never writes to GitHub")
        check.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
        check.add_argument("--pr", type=int, help="dry-run the reviewer write authorization on this PR")
        check.add_argument("--no-model", action="store_true",
                           help="skip the one tiny real completion (costs a few tokens)")
        check.add_argument("--live-turn", action="store_true",
                           help="with --pr: run one real isolated reviewer turn whose verdict is "
                                "printed and never posted")
        check.add_argument("--ping", action="store_true",
                           help="ask GitHub to ping each loop hook and report whether the gateway "
                                "accepted its signature (the selftest's only GitHub write)")
        check.add_argument("--admin-token", default="",
                           help="login whose token may ping hooks (admin:repo_hook or repo)")
        check.add_argument("--timeout", type=int, default=None,
                           help="live turn budget in seconds (default: the loop's reviewer "
                                "turn_budget_s — the budget the production worker enforces)")
        check.set_defaults(func=cmd_selftest)

        corpus_cmd = sub.add_parser("corpus", help="Replay historical PRs with known problems "
                                                   "through the reviewer (no-write) and score "
                                                   "what it caught, per prompt revision and model")
        corpus_cmd.add_argument("--loop", required=True, help="loop id")
        corpus_cmd.add_argument("--dir", default="",
                                help="directory of case files (default: corpus/ in the loop's "
                                     "state directory)")
        corpus_cmd.add_argument("--timeout", type=int, default=None,
                                help="per-case turn budget in seconds (default: the reviewer's)")
        corpus_cmd.add_argument("--history", action="store_true",
                                help="print the recorded scores and run nothing")
        corpus_cmd.set_defaults(func=cmd_corpus)

        models = sub.add_parser("models", help="Read-only: list the models a seat's Hermes "
                                               "profile's provider offers (Hermes catalog)")
        # Never `--profile`/`-p`: `hermes` takes those from anywhere on its command line to switch
        # its own profile, so the plugin would never see them.
        models.add_argument("--profile-name", dest="profile", help="Hermes profile name")
        models.add_argument("--seat", choices=("reviewer", "fixer", "adjudicator"),
                            help="use this seat's profile from the loop config")
        models.add_argument("--loop", help="loop id for --seat (default: the only loop)")
        models.set_defaults(func=cmd_models)

        change = sub.add_parser("set", help="Change a loop's settings in place")
        change.add_argument("--loop", required=True,
                            help="loop id (its config file name; `list` shows them)")
        change.add_argument("--concurrency", type=int,
                            help="default PRs per seat at once (1 = serialized; above 1 needs a "
                                 "clone, since each run gets its own)")
        change.add_argument("--reviewer-concurrency", type=int, default=None,
                            help="how many PRs the reviewer may work at once")
        change.add_argument("--fixer-concurrency", type=int, default=None,
                            help="how many PRs the fixer may work at once")
        change.add_argument("--cap", type=int, help="verdicts allowed before adjudication")
        change.add_argument("--clone", help="local clone the runs isolate from")
        change.add_argument("--base", help="base branch the loop watches")
        change.add_argument("--grace-min", type=int, help="quiet minutes before the watchdog speaks")
        change.add_argument("--marker-grace-min", type=int,
                            help="minutes an adjudication may sit claimed with no live run before "
                                 "the watchdog reports it")
        change.add_argument("--ttl-min", type=int, help="how long a run may hold its slot")
        change.add_argument("--inflight-ttl-min", type=int,
                            help="minutes an in-flight mark blocks a second run at the same head")
        change.add_argument("--turn-budget", type=int,
                            help="seconds one isolated seat turn may run (loop default)")
        change.add_argument("--attribution", choices=("on", "off"), default=None,
                            help="sign what the loop posts ('Automated by Diaktoros'), "
                                 "or stop")
        change.add_argument("--review-only-update", choices=("on", "off"), default=None,
                            help="push a clean merge of the base into a review-only author's PR "
                                 "branch (same repository only), or stop; turning it on needs "
                                 "--acknowledge-branch-push")
        change.add_argument("--acknowledge-branch-push", action="store_true",
                            help="accept that the host pushes to a branch the loop does not own; "
                                 "required to turn --review-only-update on")
        change.add_argument("--review-after-ci", choices=("on", "off"), default=None,
                            help="start each review after the head's checks finish (up to an hour), "
                                 "or start at once")
        change.add_argument("--fix-ci", choices=("on", "off"), default=None,
                            help="hand a failed required check on a fixer's PR to the fixer "
                                 "(needs unattended fixer pushes), or stop")
        change.add_argument("--review-only-cap", default=None, metavar="N",
                            help="verdicts the reviewer gives one review-only PR before it waits "
                                 "for `review --another-round`, 1-1000; 0 = the review cap")
        change.add_argument("--ci-fix-cap", default=None, metavar="N",
                            help="CI-fix turns per PR before a red head goes to the reviewer, "
                                 "1-10; 0 = the default (3)")
        change.add_argument("--review-only-daily", default=None, metavar="N",
                            help="reviewer turns a day on review-only PRs, 1-1000; 0 = no cap")
        reviewed = change.add_mutually_exclusive_group()
        reviewed.add_argument("--review-only", action="append", default=None,
                              help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it); replaces the list")
        reviewed.add_argument("--no-review-only", action="store_true",
                              help="clear the review-only list")
        required = change.add_mutually_exclusive_group()
        required.add_argument("--required-check", action="append", default=None,
                              help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates); replaces the list")
        required.add_argument("--no-required-checks", action="store_true",
                              help="clear the list: every check gates again")
        humans = change.add_mutually_exclusive_group()
        humans.add_argument("--human-path", action="append", default=None,
                            help="a glob pattern for paths only a human may approve (repeat it); replaces the list")
        humans.add_argument("--no-human-paths", action="store_true",
                            help="clear the list: no path is reserved for a human")
        change.add_argument("--fixer-check", default=None, help="one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none)")
        change.add_argument("--reviewer-turn-budget", type=int, default=None,
                            help="the reviewer seat's own turn budget in seconds")
        change.add_argument("--fixer-turn-budget", type=int, default=None,
                            help="the fixer seat's own turn budget in seconds")
        change.add_argument("--reviewer-daily-turns", type=int, default=None,
                            help="most reviewer turns per day on this loop; later ones wait for "
                                 "midnight (0 removes the cap)")
        change.add_argument("--fixer-daily-turns", type=int, default=None,
                            help="most fixer turns per day on this loop (0 removes the cap)")
        change.add_argument("--reviewer-max-steps", type=int, default=None,
                            help="agent steps one reviewer turn may take, 8-200 (0 = default 60)")
        change.add_argument("--fixer-max-steps", type=int, default=None,
                            help="agent steps one fixer or issue-fix turn may take, 8-200 "
                                 "(0 = default 80)")
        change.add_argument("--host", help="gateway webhook host")
        change.add_argument("--adjudicator-login", default=None,
                            help="optional fourth GitHub account the ruling is also posted as; "
                                 "\"\" clears it (rulings go to the operator only)")
        change.add_argument("--adjudicator-profile", default=None,
                            help="turn adjudication on: the Hermes profile that rules when the "
                                 "verdict cap is spent; creates the <id>-breach route and its shim")
        change.add_argument("--adjudicator-route", default=None,
                            help="with --adjudicator-profile: the route's name (default: <id>-breach)")
        change.add_argument("--adjudicator", choices=("off",), default=None,
                            help="turn adjudication off: removes the route and the block (breach "
                                 "markers and rulings stay in the ledger)")
        change.add_argument("--read-token", default=None,
                            help="the login the gates read GitHub as — its own account, never a "
                                 "seat or the adjudicator login (the four-identity rule); map a "
                                 "new login with --token LOGIN=/path")
        change.add_argument("--token", action="append", default=[],
                            help="LOGIN=/path/to/pat for the --read-token or --adjudicator-login "
                                 "login only (a path, never the token)")
        change.add_argument("--observer-route", help="route the observer feed delivers through")
        change.add_argument("--observer-profile", help="profile that owns the observer destination")
        change.add_argument("--observer-deliver",
                            help="where the gateway delivers the feed (telegram, discord, ...)")
        change.add_argument("--observer-events", default=None,
                            help="comma-separated transitions to send, from "
                                 "opened,handoff,verdict,approved,escalation,ruling,stall,closed,"
                                 "triaged,fixing,fixed,failed,held,conflict,ci_failed,updated,main_red,stale_approval,human_paths "
                                 "(blank = all)")
        change.add_argument("--observer-digest-min", type=int, default=None,
                            help="batch the feed into one message per N minutes (0 = per "
                                 "transition)")
        change.add_argument("--observer-urgent-route", default=None,
                            help="route for urgent notices only (blank = one feed for everything)")
        change.add_argument("--observer-urgent-profile", default=None,
                            help="profile that owns the urgent destination (blank = the feed's)")
        change.add_argument("--observer-urgent-deliver", default=None,
                            help="where the gateway delivers urgent notices (blank = the feed's)")
        change.add_argument("--observer-mute", action="store_true",
                            help="stop the feed without forgetting it")
        change.add_argument("--observer-unmute", action="store_true", help="resume a muted feed")
        change.add_argument("--observer-disable", action="store_true",
                            help="drop this loop's observer config entirely")
        change.set_defaults(func=cmd_set)

        apply_cmd = sub.add_parser("apply", help="Push the plugin settings onto a loop")
        apply_cmd.add_argument("--loop", required=True,
                               help="loop id (its config file name; `list` shows them)")
        apply_cmd.add_argument("--dry-run", action="store_true",
                               help="show the diff without writing it")
        apply_cmd.add_argument("--while-busy", action="store_true",
                               help="rebind a seat's profile/login even while a run is in flight "
                                    "(that run keeps the identity it started with)")
        apply_cmd.add_argument("--admin-token", default="",
                               help="login whose token can write the repo's hooks, for the hook "
                                    "moves and re-keys apply makes (default: the reader)")
        apply_cmd.add_argument("--recreate-routes", action="store_true",
                               help="write this loop's routes the registry lost, from the loop "
                                    "config, with a new secret, and re-key the repo hooks that "
                                    "point at them (when no intent record can restore them)")
        apply_cmd.add_argument("--hooks", action="store_true",
                               help="make this loop's two repo hooks what its routes need: "
                                    "create a missing one (paused until arm), repoint one at the "
                                    "route's exact URL, add its gate's event (hook write access, "
                                    "see --admin-token)")
        apply_cmd.add_argument("--watchdog-shim", action="store_true",
                               help="rewrite the cron shim the watchdog job runs, pinned to this "
                                    "plugin's watchdog")
        apply_cmd.set_defaults(func=cmd_apply)

        settings_cmd = sub.add_parser("settings",
                                      help="Show the plugin-level defaults a loop starts from")
        settings_cmd.set_defaults(func=cmd_settings)

        show_cmd = sub.add_parser("show",
                                  help="Every setting of one loop: value, source, meaning")
        show_cmd.add_argument("--loop", required=True,
                              help="loop id (its config file name; `list` shows them)")
        show_cmd.add_argument("--json", action="store_true", help="machine-readable output")
        show_cmd.set_defaults(func=cmd_show)

        arm = sub.add_parser("arm", help="Activate the loop's GitHub hooks")
        arm.add_argument("--loop", help="loop id (default: every configured loop)")
        arm.add_argument("--pause", action="store_true", help="pause instead of arming")
        arm.add_argument("--admin-token", default="",
                         help="login whose token can edit the repo's hooks (default: the reader)")
        arm.set_defaults(func=cmd_arm)

        triage = sub.add_parser("triage", help="Issue triage for one loop: --enable, --disable, "
                                               "or show it (#213)")
        triage.add_argument("--loop", required=True,
                            help="loop id (its config file name; `list` shows them)")
        switch = triage.add_mutually_exclusive_group()
        switch.add_argument("--enable", action="store_true",
                            help="turn triage on (or change it): writes its route, shim and, with "
                                 "--admin-token, its issues hook (paused until arm)")
        switch.add_argument("--disable", action="store_true",
                            help="turn triage off: removes its route, shim and (with --admin-token) hook")
        triage.add_argument("--triage-profile", dest="profile", default="",
                            help="Hermes profile whose model triages")
        triage.add_argument("--author", action="append", default=[],
                            help="GitHub login whose new issues are triaged (repeatable); "
                                 "anyone else's are ignored")
        triage.add_argument("--labels", default="",
                            help="comma-separated labels triage may apply, e.g. "
                                 "bug,feature,docs,question,P0,P1,P2,P3")
        triage.add_argument("--max-labels", type=int, default=None,
                            help="at most this many labels per issue (default 3)")
        triage.add_argument("--comment", choices=("on", "off"), default=None,
                            help="allow one short comment with the labels (default off)")
        triage.add_argument("--login", default="",
                            help="account that labels (default: the reviewer seat); needs "
                                 "issues: write, never the reader")
        triage.add_argument("--token", action="append", default=[],
                            help="login=/path/to/pat for --login (or --admin-token), if not mapped")
        triage.add_argument("--fix-label", default=None,
                            help="a label a maintainer applies to hand an issue to the fixer "
                                 "(#214; needs unattended fixer pushes on); '' turns it off")
        triage.add_argument("--maintainer", action="append", default=[],
                            help="login whose applying --fix-label counts (repeatable)")
        triage.add_argument("--daily-turns", type=int, default=None,
                            help="at most this many triage turns per day (0 removes the cap)")
        triage.add_argument("--fix-daily-turns", type=int, default=None,
                            help="at most this many issue-fix turns per day (0 = the default, "
                                 f"{config.DEFAULT_FIX_DAILY_TURNS}); issue fixes are always "
                                 "capped")
        triage.add_argument("--auto-fix-label", action="append", default=None,
                            help="a triage label that hands an issue to the fixer without a "
                                 "maintainer (#232; repeatable; never P0-P2; '' clears the list)")
        triage.add_argument("--auto-fix-daily", type=int, default=None,
                            help="at most this many automatic issue fixes per day (0 = the "
                                 f"default, {config.DEFAULT_AUTO_FIX_DAILY})")
        triage.add_argument("--admin-token", default="",
                            help="login whose token can create or delete repo hooks")
        triage.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
        triage.set_defaults(func=cmd_triage)

        fixer_push = sub.add_parser("fixer-push", help="Explicit per-repository unattended fixer push policy")
        fixer_push.add_argument("--loop", required=True, help="exact loop id (never all loops)")
        direction = fixer_push.add_mutually_exclusive_group(required=True)
        direction.add_argument("--enable", action="store_true", help="opt this repository in")
        direction.add_argument("--disable", action="store_true", help="turn unattended pushes off")
        fixer_push.add_argument("--acknowledge-pr-race", action="store_true",
                                help="accept the residual non-atomic PR-metadata/ref race; required for --enable")
        fixer_push.add_argument("--dry-run", action="store_true", help="show action without writing")
        fixer_push.set_defaults(func=cmd_fixer_push)

        retry = sub.add_parser("retry", help="Re-arm a PR's isolated run that failed before "
                                             "any GitHub write (refuses one that may have written)")
        retry.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
        retry.add_argument("--pr", type=int, required=True,
                           help="the pull request whose failed run to re-arm")
        retry.add_argument("--seat", choices=["reviewer", "fixer", "adjudicator", "triage",
                                              "issue_fixer"],
                           help="only that seat's run (default: whichever failed at the PR's "
                                "newest head)")
        retry.set_defaults(func=cmd_retry)

        drain = sub.add_parser("drain", help="Start a queued run once its seat is free")
        drain.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
        drain.add_argument("--seat", default="reviewer", choices=["reviewer", "fixer"],
                           help="which seat's queue to drain")
        drain.set_defaults(func=cmd_drain)

        cleanup = sub.add_parser("cleanup", help="Reclaim local disk for finished PRs")
        cleanup.add_argument("--loop", required=True,
                             help="loop id (its config file name; `list` shows them)")
        cleanup.add_argument("--pr", type=int,
                             help="clean one closed PR (default: sweep every closed PR the clone "
                                  "knows about)")
        cleanup.add_argument("--dry-run", action="store_true",
                             help="list what would be removed, remove nothing")
        cleanup.set_defaults(func=cmd_cleanup)

        uninstall = sub.add_parser("uninstall", help="Remove a loop: its repo hooks, cron job, "
                                   "routes and config (refuses rather than leave live hooks)")
        uninstall.add_argument("--loop", required=True,
                               help="loop id (its config file name; `list` shows them)")
        uninstall.add_argument("--keep-config", action="store_true",
                               help="remove hooks, cron job and routes but keep the loop config file")
        uninstall.add_argument("--admin-token", default="",
                               help="login whose token can delete hooks (admin:repo_hook or repo)")
        uninstall.add_argument("--keep-hooks", action="store_true",
                               help="leave the repo hooks live (explicit opt-out)")
        uninstall.add_argument("--purge", action="store_true",
                               help="also delete the loop's default state directory")
        uninstall.set_defaults(func=cmd_uninstall)

    summary = "Diaktoros — bounded, autonomous software maintenance: review, fix, triage"
    description = ("Configure, inspect and drive Diaktoros loops. Each loop is one JSON file under "
                   "~/.hermes/diaktoros.d/, and it drives two webhook routes, two GitHub hooks "
                   "and (optionally) one cron watchdog job.")

    def deprecated(parser) -> None:  # noqa: ANN001
        """``hermes review-loop`` (#425): the same commands, each saying once that it was renamed.

        It never becomes ``_PARSER``: verbs one command runs for another go through the real name.
        """
        global _PARSER
        kept = _PARSER
        setup(parser)
        _PARSER = kept if kept is not None else parser
        if parser.get_default("func") is not None:          # the bare command prints its usage
            parser.set_defaults(func=_renamed(parser.get_default("func")))
        for action in parser._subparsers._group_actions if parser._subparsers else ():
            for verb in action.choices.values():
                func = verb.get_default("func")
                if func is not None:
                    verb.set_defaults(func=_renamed(func))

    # The old name first and the full name last: a host that keeps one registration keeps that one.
    ctx.register_cli_command("review-loop", "Renamed: use `hermes dk` (Diaktoros)", deprecated,
                             description=description)
    ctx.register_cli_command("dk", summary + " (short for `hermes diaktoros`)", setup,
                             description=description)
    ctx.register_cli_command("diaktoros", summary, setup, description=description)
