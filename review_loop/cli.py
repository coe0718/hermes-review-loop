"""``hermes review-loop`` — install, inspect and drive the loop from the CLI.

The plugin does not try to own the gateway. Everything the loop needs beyond its own scripts —
webhook routes, GitHub hooks, a cron entry — is written through the same config surfaces the
operator would touch by hand, so `hermes review-loop uninstall` is really just the inverse of
`init` and nothing lives in a place you cannot see.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import tempfile
from urllib.parse import urlsplit

from . import config, doctor, gate, gh, observer, prompts, route_intent, routes, state as state_mod

SHIM_NAME = "review-loop-watchdog.py"


def watchdog_job_name(loop: dict) -> str:
    """The scheduler job name ``init`` registers for a loop's watchdog.

    ``doctor`` looks for exactly this name when it checks the cron job, so it lives here as one
    spelling rather than a format string in two files.
    """
    return f"review loop watchdog ({loop['id']})"

SHIM = '''#!/usr/bin/env python3
"""Cron shim written by `hermes review-loop init`.

The scheduler runs scripts from ~/.hermes/scripts/, so this forwards to the plugin's own
watchdog and delivers its stdout. Keep the plugin as the single copy of the code.
"""
import pathlib
import subprocess
import sys

WATCHDOG = pathlib.Path("{watchdog}")
if not WATCHDOG.exists():
    print(f"review-loop watchdog is missing: {{WATCHDOG}}")
    sys.exit(0)

proc = subprocess.run([sys.executable, str(WATCHDOG), *sys.argv[1:]], capture_output=True, text=True)
if proc.stdout.strip():
    print(proc.stdout.strip())
if proc.returncode != 0 and proc.stderr.strip():
    print(f"review-loop watchdog failed: {{proc.stderr.strip()[:400]}}")
'''


def routes_for(loop: dict) -> dict:
    """The conventional route names for a loop, and the names ``init`` writes."""
    return {"reviewer": f"{loop['id']}-review", "fixer": f"{loop['id']}-fix",
            "observer": f"{loop['id']}-observe",
            "adjudicator": f"{loop['id']}-breach"}


def _routes_of(loop: dict) -> dict:
    """role → route name, from the loop's own config: the seats own the names, not this module.

    A hand-edited loop may route its reviewer anywhere; every path that fires, verifies or
    rebinds a route must read that answer rather than re-derive the convention.
    """
    return route_intent.routes_of(loop)


# Which gate script each role's route must run. Ownership is checked against this before a route
# is written: the registry is shared with every other plugin on the host, and rebinding someone
# else's route to our profile would be a silent takeover of their webhook.
GATE_SCRIPT = {"reviewer": "gate_reviewer.py", "fixer": "gate_fixer.py",
               "adjudicator": "gate_adjudicator.py", "observer": "observe.py"}


# Each role's route prompt: together with the gate script, the proof that a route is ours.
_ROUTE_PROMPT = {"reviewer": prompts.REVIEWER, "fixer": prompts.FIXER,
                 "adjudicator": prompts.ADJUDICATOR, "observer": prompts.OBSERVER}


def _verify_routes(loop: dict, roles) -> None:
    """Refuse to write a route that is not this loop's to write.

    Three rails: no other loop may already claim the name, no two roles of this loop may share one
    route (one route wakes one seat), and an installed route must have this role's exact gate and
    prompt. All three fail *before* anything is written, because a route is the one
    artifact here that another plugin could own.
    """
    mine = _routes_of(loop)
    route_roles = (*config.ROUTE_ROLES, "observer")
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
    wanted = (*config.ROUTE_ROLES, "observer") if roles is None else tuple(roles)
    written: dict = {}
    for role in config.ROUTE_ROLES:
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
        else:
            adjudicator = loop.get("adjudicator") or {}
            routes.new_route(name, profile=adjudicator.get("profile", "default"),
                             prompt=prompts.ADJUDICATOR, events=["pull_request"],
                             deliver=adjudicator.get("deliver", "telegram"),
                             description=f"{loop['repo']} — adjudicate a loop that spent its "
                                          "budget", **common)
        written[role] = name
    if "observer" in wanted and "observer" in names:
        observer_cfg = loop["observer"]
        name = names["observer"]
        routes.new_route(name, profile=observer_cfg.get("profile", "default"),
                         prompt=prompts.OBSERVER, events=["pull_request"],
                         script="observe.py", deliver=observer_cfg.get("deliver", "telegram"),
                         deliver_only=True, host=host,
                         description=f"{loop['repo']} — read-only observer feed: one short "
                                     "notice per loop transition")
        written["observer"] = name
    return written


def _seat_lines(loop: dict, title: str = "seat mapping") -> list[str]:
    """The effective mapping: who serves each role, as what login, woken by which route.

    This is the line an operator reads to answer "is Drey actually the fixer here?" without
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
        login_cell = f"login {login:<16} " if role != "adjudicator" else " " * 23
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
    return lines


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
        return f"{role} {name}: not installed — run init"
    got = str(entry.get("profile") or "default")
    if got == want:
        return f"{role} {name} → {got} (ok)"
    return (f"{role} {name} → {got}, not {want}: MISMATCH — "
            f"hermes review-loop apply --loop {loop['id']}")


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
        current = str(entry.get("profile") or "default")
        target = config.seat_profile(loop, role)
        if current != target:
            binds[role] = (name, current, target)
    return binds


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
    return observer_cfg




def _install_hooks(loop: dict, token_login: str | None) -> list[str]:
    """Create the two repo hooks via the API. Needs a token with admin:repo_hook on the repo."""
    names = _routes_of(loop)
    host = config.webhook_host(loop.get("host"), required=True)
    # Validate both destinations and secrets before creating either external hook.
    hooks = []
    for seat, event in (("reviewer", "pull_request"), ("fixer", "pull_request_review")):
        route_name = names.get(seat, "")
        url = routes.url_for(route_name, host)
        secret = (routes.route(route_name) or {}).get("secret", "")
        if not url or not secret:
            raise config.ConfigError(f"route {route_name!r} needs a valid webhook URL and secret before installing hooks")
        hooks.append((event, url, secret))
    baseline = _hook_listing(loop, token_login)
    created = []
    try:
        for event, url, secret in hooks:
            body = {"name": "web", "active": True, "events": [event],
                    "config": {"url": url, "content_type": "json", "secret": secret,
                               "insecure_ssl": "0"}}
            result = gh.api(loop, f"/repos/{loop['repo']}/hooks", method="POST", body=body,
                            login=token_login or loop.get("read_token"))
            if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                raise config.ConfigError(f"hook creation not confirmed for {url}")
            created.append(result["id"])
        return [f"hook {id} → {url}" for id, (_, url, _) in zip(created, hooks)]
    except Exception as exc:
        failures = []
        # A lost POST response may still have created a hook: compare with the baseline.
        try:
            current = _hook_listing(loop, token_login)
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
                if any(h["id"] == hook_id for h in _hook_listing(loop, token_login)):
                    raise config.ConfigError("still present after DELETE")
            except Exception as rollback_exc:
                failures.append(f"hook {hook_id}: {rollback_exc}")
        raise config.ConfigError(f"hook install failed: {exc}; " +
                                 ("ROLLBACK FAILED: " + "; ".join(failures) if failures
                                  else "created hooks removed")) from exc

def _hook_listing(loop: dict, token_login: str | None = None) -> list[dict]:
    hooks = gh.api(loop, f"/repos/{loop['repo']}/hooks?per_page=100",
                   login=token_login or loop.get("read_token"))
    if not isinstance(hooks, list) or len(hooks) >= 100 or any(
        not isinstance(h, dict) or not isinstance(h.get("id"), int) or
        not isinstance(h.get("config"), dict) or
        not isinstance(h["config"].get("url"), str) for h in hooks
    ):
        raise config.ConfigError("cannot read a complete, valid repo hook listing; no changes made")
    return hooks


def _set_hooks(loop: dict, active: bool, token_login: str | None) -> list[str]:
    names = _routes_of(loop)
    wanted = tuple(name for role, name in names.items() if role in ("reviewer", "fixer"))
    try:
        hooks = _hook_listing(loop, token_login)
    except config.ConfigError as exc:
        return [f"could not read the repo's hooks: {exc}"]
    out = []
    for hook in hooks:
        url = (hook.get("config") or {}).get("url", "")
        if not any(name in url for name in wanted):
            continue
        if bool(hook.get("active")) == active:
            out.append(f"hook {hook['id']} already {'active' if active else 'paused'}")
            continue
        gh.api(loop, f"/repos/{loop['repo']}/hooks/{hook['id']}", method="PATCH",
               body={"active": active}, login=token_login or loop.get("read_token"))
        out.append(f"hook {hook['id']} → {'active' if active else 'paused'}")
    return out or ["no loop hooks found — run init --hooks first"]


def _hook_moves(before: dict, after: dict, binds: dict) -> list[tuple[int, str, str]]:
    """Preflight exact hook URLs against configured and installed owned route profiles."""
    names = _routes_of(after)
    expected: dict[str, tuple[str, str]] = {}
    targets: dict[str, str] = {}
    unchanged: dict[str, str] = {}
    for role in ("reviewer", "fixer"):
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
    if not targets:
        return []
    hooks = _hook_listing(before)  # Never repair a route if this listing cannot be trusted.
    moves = []
    route_names = {name: role for role, name in names.items() if role in ("reviewer", "fixer")}
    for hook in hooks:
        old = hook["config"]["url"]
        parts = urlsplit(old)
        # Match a complete webhook route segment, not a substring of another route.
        installed_name = parts.path.rsplit("/webhooks/", 1)[-1] if "/webhooks/" in parts.path else ""
        role = route_names.get(installed_name)
        if role is None:
            continue
        if role in unchanged and old == unchanged[role]:
            continue
        if role in targets and old == targets[role]:
            continue  # Already corrected independently; do not rewrite it.
        if role not in targets or old not in expected or expected[old][0] != role:
            raise config.ConfigError(f"installed {role} hook {hook['id']} "
                                     f"points at unexpected URL {old!r}; no changes made")
        moves.append((hook["id"], old, targets[role]))
    return moves


def _patch_hook_url(loop: dict, hook_id: int, url: str) -> None:
    path = f"/repos/{loop['repo']}/hooks/{hook_id}"
    result = gh.api(loop, path, method="PATCH", body={"config": {"url": url}},
                    login=loop.get("read_token"))
    # A lost response is ambiguous. Always read back and roll back if it does not agree.
    actual = gh.api(loop, path, login=loop.get("read_token"))
    if not isinstance(result, dict) or not isinstance(actual, dict) or \
            (actual.get("config") or {}).get("url") != url:
        raise config.ConfigError(f"hook {hook_id} URL update not confirmed as {url!r}")


def _install_schedule(loop: dict, schedule: str, deliver: str) -> list[str]:
    """A cron shim plus the job itself, through the scheduler's own CLI."""
    scripts = config.home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    shim = scripts / SHIM_NAME
    shim.write_text(SHIM.format(watchdog=watchdog))
    shim.chmod(0o755)
    hermes = shutil.which("hermes") or "hermes"
    cmd = [hermes, "cron", "create", schedule, "--name", watchdog_job_name(loop),
           "--no-agent", "--script", SHIM_NAME, "--deliver", deliver]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return [f"could not create the cron job: {exc}", f"run it yourself: {' '.join(cmd)}"]
    if proc.returncode != 0:
        return [f"cron create failed: {(proc.stderr or proc.stdout).strip()[:200]}",
                f"run it yourself: {' '.join(cmd)}"]
    return [f"scheduled the watchdog ({schedule}, deliver={deliver})", f"shim: {shim}"]


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
        current = config.load_id(loop['id'])
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


def cmd_init(args) -> int:
    """Install a loop: write its config, its routes, and (on request) its hooks and cron job.

    The settings form supplies *who serves each seat* the same way it supplies the numeric knobs —
    as a default a new loop starts from — while explicit flags still win, because an operator
    installing a loop from the CLI on this machine should not have to depend on what a per-profile
    form happens to hold. Everything is validated (profiles, allowlists, credentials, route
    ownership) before the first file is written.
    """
    d = config.settings_defaults(_SETTINGS)
    tokens = {}
    for pair in args.token or []:
        if "=" not in pair:
            print(f"--token expects login=/path/to/pat, got {pair!r}")
            return 2
        login, path = pair.split("=", 1)
        tokens[login] = path

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
              "them in the plugin settings (Capabilities → Plugins → review loop)")
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

    raw = {
        "id": args.id or args.repo.split("/")[-1],
        "repo": args.repo, "base": args.base, "cap": args.cap,
        "concurrency": args.concurrency,
        "fixers": fixers, "reviewers": reviewers,
        "reviewer_seat": reviewer_seat,
        "seats": {
            "reviewer": {"profile": reviewer_profile, "route": "",
                         "login": reviewer_seat,
                         "agent": args.reviewer_agent},
            "fixer": {"profile": fixer_profile, "route": "",
                      "login": fixer_seat, "agent": args.fixer_agent},
        },
        "adjudicator": ({"route": args.adjudicator_route, "profile": adjudicator_profile}
                        if args.adjudicator_route else {}),
        "skill": args.skill,
        "tokens": tokens, "read_token": args.read_token or reviewer_seat,
        "clone": args.clone, "roots": args.root or [],
        "state_dir": args.state_dir or str(config.home() / "state" / "review-loops"
                                           / (args.id or args.repo.split("/")[-1])),
        "host": args.host, "grace_min": args.grace_min,
        "ttl_min": args.ttl_min, "inflight_ttl_min": args.inflight_ttl_min,
        "observer": _observer_args(args, args.id or args.repo.split("/")[-1]),
    }
    # A seat-level capacity wins over the loop default, so only write it when it was asked for.
    for seat, value in (("reviewer", args.reviewer_concurrency), ("fixer", args.fixer_concurrency)):
        if value is not None:
            raw["seats"][seat]["concurrency"] = value
    names = routes_for(raw)
    raw["seats"]["reviewer"]["route"] = names["reviewer"]
    raw["seats"]["fixer"]["route"] = names["fixer"]
    roles = {"reviewer", "fixer"} | ({"adjudicator"} if raw["adjudicator"] else set())
    if raw["observer"].get("route"):
        roles.add("observer")
    try:
        loop = config.normalize(raw)
        # Routes are installed even without --hooks; never write a partial loop with
        # route URLs that cannot resolve to this operator's own gateway.
        config.webhook_host(loop["host"], required=True)
        # Who does what, and may they: profiles, allowlists, distinct credentials and route
        # ownership are all checked before a single file is written.
        config.verify_seats(loop, roles)
        _verify_routes(loop, roles)
        _observer_check(loop)
    except config.ConfigError as exc:
        print(f"config refused: {exc}")
        return 2

    if args.dry_run:
        print("dry run — nothing written: no loop config, no routes, no hooks, no cron job")
        print(f"  would write: {config.config_dir() / (loop['id'] + '.json')}")
        for line in _seat_lines(loop, "effective seat mapping"):
            print(f"  {line}")
        for name in _routes_of(loop).values():
            print(f"  would write route: {name}")
        if args.hooks:
            print("  would create the two repo hooks (pull_request, pull_request_review)")
        if args.schedule:
            print(f"  would install the watchdog cron job ({args.schedule})")
        if loop.get("observer", {}).get("route"):
            print(f"  would write route: {loop['observer']['route']}")
        return 0

    path = config.config_dir() / f"{loop['id']}.json"
    if path.exists():
        print(f"refused: loop {loop['id']!r} already exists; use `hermes review-loop set` "
              "to change it without losing observer destination/receipt bindings")
        return 2
    observer_name = (loop.get("observer") or {}).get("route")
    if observer_name and routes.route(observer_name):
        print(f"refused: route {observer_name!r} already exists and is not this observer's route")
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
    try:
        hook_lines = _install_hooks(loop, args.admin_token) if args.hooks else []
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
    if not args.hooks:
        print("  (repo hooks not created — pass --hooks, or add them by hand with the route URLs)")
    for line in _install_schedule(loop, args.schedule, args.watchdog_deliver) if args.schedule else []:
        print(f"  {line}")
    print("\nNext: give each seat's profile the token it needs ("
          "GH_TOKEN in the profile .env for git push, plus the token file named here), then "
          "`hermes review-loop status`.")
    return 0


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
        print(f"no such loop: {exc}")
        return 2

    wanted = {"concurrency": args.concurrency, "cap": args.cap, "base": args.base,
              "clone": args.clone, "grace_min": args.grace_min,
              "marker_grace_min": args.marker_grace_min, "ttl_min": args.ttl_min,
              "inflight_ttl_min": args.inflight_ttl_min, "host": args.host}
    changes = {k: v for k, v in wanted.items()
               if v is not None and v != "" and v != loop.get(k)}

    seats = {seat: dict(cfg) for seat, cfg in loop["seats"].items()}
    seat_changes = {}
    for seat, value in (("reviewer", args.reviewer_concurrency), ("fixer", args.fixer_concurrency)):
        if value is not None and value != config.seat_concurrency(loop, seat):
            seats[seat]["concurrency"] = value
            seat_changes[seat] = value

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

    if not changes and not seat_changes and observer_cfg == (loop.get("observer") or {}):
        print("nothing to change — pass at least one setting "
              "(--concurrency, --reviewer-concurrency, --fixer-concurrency, --cap, --clone, "
              "--observer-profile, ...)")
        return 0

    try:
        updated = config.normalize({**loop, **changes, "seats": seats, "observer": observer_cfg})
        _observer_check(updated)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2

    before = loop.get("observer") or {}
    after = updated.get("observer") or {}
    # Disabling stops new notices and removes the route, but leaves the outbox intact.
    # Keep its former destination as a tombstone: otherwise re-enabling at a new
    # destination (or host) could forward a queued private PR link there.
    previous = loop.get("observer_disabled") or {}
    bound = before or previous
    old_target = {k: bound.get(k) for k in ("route", "profile", "deliver")}
    old_target["host"] = previous.get("host") if previous else loop.get("host")
    new_target = {k: after.get(k) for k in ("route", "profile", "deliver")}
    new_target["host"] = updated.get("host")
    destination_changed = any(before.get(k) != after.get(k)
                              for k in ("route", "profile", "deliver"))
    if bound and after and old_target != new_target:
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
            written = routes.new_route(name, profile=after["profile"], prompt=prompts.OBSERVER,
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
    path = _write_config(updated)
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
    if updated.get("observer") != (loop.get("observer") or {}):
        print(f"  observer: {observer.describe(loop.get('observer') or {})} → "
              f"{observer.describe(updated.get('observer') or {})}")
    print(f"loop config updated: {path}")

    if "concurrency" in changes:
        # Say it out loud, because a loop-wide default that a seat overrides is exactly the kind
        # of setting someone changes twice and wonders why nothing moved.
        for seat in ("reviewer", "fixer"):
            if (loop["seats"].get(seat) or {}).get("concurrency") is not None:
                print(f"  note: {seat} has its own concurrency "
                      f"({loop['seats'][seat]['concurrency']}) — the loop default does not apply to it")

    print("  parallel now: " + " · ".join(
        f"{seat} {config.seat_concurrency(updated, seat)}" for seat in ("reviewer", "fixer"))
        + "   (1 = serialized; everything above the limit queues)")
    return 0


def cmd_apply(args) -> int:
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
        updated = config.normalize(config.apply_settings(loop, _SETTINGS))
    except config.ConfigError as exc:
        print(f"settings refused: {exc}")
        return 2

    identity, touched = _seat_diffs(loop, updated)
    # The installed registry can drift independently of the loop and the form. Repair those
    # routes through the same ownership, seat and in-flight preflight as an identity push.
    binds = _route_binds(updated, set(_routes_of(updated)))
    # A route installed by an older release (the pre-#21 breach route on gate_reviewer.py) is
    # repaired here too: `init` refuses an existing loop, so apply is the only reconcile path.
    repairs = _stale_scripts(updated)
    rebinding = touched | set(binds) | set(repairs)
    try:
        # Validate what this apply would *write*: a loop that predates the seat checks keeps
        # loading, but a seat this push moves must be one that can actually run.
        config.verify_seats(updated, rebinding)
        if rebinding:
            _verify_routes(updated, rebinding)
    except config.ConfigError as exc:
        print(f"settings refused: {exc}")
        return 2

    changes = []
    for key in ("cap", "base", "host", "grace_min", "ttl_min", "inflight_ttl_min"):
        if updated.get(key) != loop.get(key):
            changes.append((key, loop.get(key), updated.get(key)))
    if (updated.get("clone") or "") != (loop.get("clone") or ""):
        changes.append(("clone", loop.get("clone") or "(none)", updated.get("clone") or "(none)"))
    for seat in ("reviewer", "fixer"):
        was, now = config.seat_concurrency(loop, seat), config.seat_concurrency(updated, seat)
        if was != now:
            changes.append((f"{seat} concurrency", was, now))

    missing_routes = sorted(name for role, name in _routes_of(updated).items()
                            if role in touched and not routes.route(name))

    if not changes and not identity and not binds and not repairs:
        print(f"[{loop['id']}] already matches the plugin settings")
        return 0
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
        print(f"  route {name}: profile {current} → {target}   (the URL carries the profile)")
    for role, (name, script) in sorted(repairs.items()):
        print(f"  route {name}: script {script} → {GATE_SCRIPT[role]}   (installed by an older "
              "release)")
    for name in missing_routes:
        print(f"  route {name}: not installed — `hermes review-loop init` creates routes; "
              "apply will not invent one behind your back")
    if missed := [role for role, name in _routes_of(updated).items()
                  if role in touched and name in missing_routes]:
        print(f"refused: {', '.join(missed)} would move to a route that does not exist yet")
        return 2

    if args.dry_run:
        print("(dry run — nothing written: no loop config, no routes touched)")
        return 0

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
        hook_moves = _hook_moves(loop, updated, binds)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    previous = {name: routes.route(name)
                for name in {bind[0] for bind in binds.values()} | {n for n, _ in repairs.values()}}
    config_path = config.config_dir() / f"{loop['id']}.json"
    previous_config = config_path.read_bytes()
    attempted_hooks = []
    try:
        rewrite = tuple(set(binds) | set(repairs))
        rebound = list(_install_routes(updated, roles=rewrite).items()) if rewrite else []
        for role, name in rebound:
            entry = routes.route(name)
            if not entry or str(entry.get("profile") or "") != config.seat_profile(updated, role):
                raise config.ConfigError(f"route {name} readback does not match requested profile")
            if entry.get("script") != GATE_SCRIPT[role]:
                raise config.ConfigError(f"route {name} readback does not run {GATE_SCRIPT[role]}")
        for hook_id, old, new in hook_moves:
            attempted_hooks.append((hook_id, old))
            _patch_hook_url(loop, hook_id, new)
        path = _write_config(updated) if changes or identity else config_path
        if rebound:
            route_intent.record_live(updated, [name for _, name in rebound])
    except Exception as exc:
        failed = []
        for hook_id, old in reversed(attempted_hooks):
            try:
                _patch_hook_url(loop, hook_id, old)
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
              + (f", script {GATE_SCRIPT[role]}" if role in repairs else ""))
    for hook_id, _, new in hook_moves:
        print(f"  hook {hook_id} → {new}")
    return 0


def cmd_settings(args) -> int:
    """Show the plugin-level defaults — what a new loop starts from, and what ``apply`` pushes."""
    d = config.settings_defaults(_SETTINGS)
    print("plugin settings (desktop: Capabilities → Plugins → review loop)")
    for key, spec in config.SETTINGS_SCHEMA.items():
        value = d[key]
        source = "set" if str((_SETTINGS or {}).get(key, "")) not in ("", "None") else "default"
        print(f"  {key:<21} {str(value):<26} [{source}]  {spec['description']}")

    print("\nseat mapping (blank = not set here: a loop keeps its own answer)")
    mapping = config.seat_mapping(_SETTINGS)
    for role in config.ROUTE_ROLES:
        entry = mapping.get(role) or {}
        profile = entry.get("profile") or "(blank)"
        if role in config.LOGIN_SETTINGS:
            cell = f"login {(entry.get('login') or '(blank)'):<30} "
        else:
            # The adjudicator reads and rules and never pushes, so there is no login to name — the
            # cell says so in the same width, which keeps the [set]/[blank] markers in one column.
            cell = f"{'no login (rules, never pushes)':<36} "
        print(f"  {role:<12} profile {profile:<20} {cell}[{'set' if entry else 'blank'}]")
    adjudicator_note = ("the adjudicator lands only on a loop that already has an adjudicator "
                        "route, and it has no login to misattribute a ruling to; route names stay "
                        "per repository")
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
    if not _SETTINGS:
        print("\nnothing set — every value above is the schema default")
    print("\napply them to a loop with: hermes review-loop apply --loop <id>"
          "\n(settings are defaults, not a subscription: an existing loop keeps its own seats, "
          "profiles and numbers until you apply — and blank fields above never erase them)")
    return 0


def cmd_list(args) -> int:
    loops = config.all_loops()
    if not loops:
        print(f"no loops configured in {config.config_dir()}")
        return 0
    for loop in loops:
        seats = " ".join(f"{seat}={config.seat_concurrency(loop, seat)}"
                         for seat in ("reviewer", "fixer"))
        print(f"{loop['id']:<20} {loop['repo']:<30} cap={loop['cap']} {seats} "
              f"fixers={','.join(loop['fixers'])} reviewers={','.join(loop['reviewers'])}")
    return 0


def cmd_status(args) -> int:
    loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    for loop in loops:
        from . import state as state_mod

        st = state_mod.state_for(loop)
        print()
        print(f"[{loop['id']}] {loop['repo']}  (cap {loop['cap']}, base {loop['base']})")
        print("  parallel:   " + " · ".join(
            f"{seat} {config.seat_concurrency(loop, seat)}"
            + ("" if config.seat_concurrency(loop, seat) > 1 else " (serialized)")
            for seat in ("reviewer", "fixer")))
        print(f"  clone:      {loop['clone'] or '(none)'}")
        print("  fixer push: " + ("ENABLED — operator accepted PR-metadata/ref race"
                                  if config.unattended_fixer_push_enabled(loop)
                                  else "off (unattended pushes disabled)"))
        print(f"  state:      {st.dir}")
        print(f"  seats:      reviewer={loop['seats']['reviewer']['login']} "
              f"({loop['seats']['reviewer']['profile']}) · "
              f"fixer={loop['seats']['fixer']['login']} ({loop['seats']['fixer']['profile']})")
        adjudicator = loop.get("adjudicator") or {}
        if adjudicator.get("route"):
            print(f"  {'adjudicator:':<12} {adjudicator.get('profile', 'default')} "
                  f"(route {adjudicator['route']})")
        else:
            print(f"  {'adjudicator:':<12} (no route — the cap only writes a marker)")
        # What the registry actually serves, next to what the config claims: those two facts can
        # disagree after a profile change, and this is the one place the operator would see it.
        print("  routes:     " + " · ".join(_route_state(loop, role)
                                            for role in config.ROUTE_ROLES
                                            if role in _routes_of(loop)))
        refs = _credential_lines(loop)
        if refs:
            print("  token refs: " + " · ".join(refs))
        locks = st._load(st.locks, {}) or {}
        for seat, entries in locks.items():
            for key, entry in (entries or {}).items():
                held = (time.time() - entry.get("at", time.time())) / 60
                print(f"  running:    {seat} on {key} for {held:.0f}m")
        for seat in ("reviewer", "fixer"):
            queued = len(st.queue_items(seat))
            if queued:
                print(f"  queued:     {seat} {queued} "
                      f"({config.seat_concurrency(loop, seat)} at a time)")
        queue = st.queue_all()
        for seat, items in queue.items():
            for key, entry in items.items():
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
    return 0


def cmd_explain(args) -> int:
    """Why one PR is not moving, and the one event that would move it.

    Read-only all the way down: it reads GitHub and the loop's own state files and writes neither —
    no claim, no queue entry, no drain, no webhook POST, no token printed. The conclusions come
    from ``gate.explain``, so they are the predicates the live gates run rather than a second
    opinion about them.

    Exit 2 only when the question cannot be asked at all (an unknown loop, or several loops and no
    ``--loop``). A PR GitHub does not have, or cannot be read, is an *answer*: it is reported as
    unknown, with the read to retry.
    """
    from . import state as state_mod

    if args.loop:
        try:
            loops = [config.load_id(args.loop)]
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return 2
    else:
        loops = config.all_loops()
        if len(loops) > 1:
            print(f"{len(loops)} loops are configured "
                  f"({', '.join(loop['id'] for loop in loops)}) — name one with --loop")
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
        if report['chain']['status'] != 'direct':
            print(f"  {'chain:':<12}{report['chain']['status']} · "
                  f"parents {report['chain']['parents']} · {report['chain']['reason']}")
        print(f"  {'budget:':<12}{report['budget']}")
        print(f"  {'seat:':<12}{report['seat']}")
        print(f"  {'queue:':<12}{report['queue']}")
        print(f"  {'in-flight:':<12}{report['inflight']}")
        print(f"  {'escalation:':<12}{report['escalation']}")
        print(f"  {'hooks:':<12}{report['hooks']}")
        print(f"  {'sweep:':<12}{report['sweep']}")
        for text in report["blockers"]:
            print(f"  {'blocked:':<12}{text}")
        if not report["blockers"]:
            print(f"  {'blocked:':<12}nothing — no guard is holding this PR back")
        print(f"  {'next:':<12}{report['next']['action']}")
    return 0


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
        print(f"no loops configured in {config.config_dir()}")
        return 0
    failed = 0
    for loop in loops:
        if getattr(args, "repair", False):
            # The one write doctor can make, and only when asked: put this loop's own routes back
            # from the plugin's intent record (same secret). Everything after it stays read-only.
            lines = route_intent.heal(loop)
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
    if args.timeout < 1:
        print("--timeout must be positive")
        return 2
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"cannot selftest loop: {doctor._safe_report_text(str(exc))}")
        return 2
    return selftest.run(loop, pr=args.pr, model=not args.no_model, live_turn=args.live_turn,
                        timeout=args.timeout)


def cmd_models(args) -> int:
    """Read-only: what a profile's provider offers, from Hermes's model catalog (issue #32).

    Changing a seat's model is changing that profile's model in Hermes (`hermes -p NAME model`);
    this only lists. Exit 1 when the profile, its provider or the catalog cannot answer.
    """
    from . import doctor, seat_model
    if bool(args.profile) == bool(args.seat):
        print("pass exactly one of --profile NAME or --seat reviewer|fixer|adjudicator")
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


def cmd_arm(args) -> int:
    for loop in ([config.load_id(args.loop)] if args.loop else config.all_loops()):
        for line in _set_hooks(loop, not args.pause, args.admin_token):
            print(f"[{loop['id']}] {line}")
    return 0

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
            ledger = config.home() / 'state' / 'review-loop-runs.sqlite'
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
    return 0


def cmd_drain(args) -> int:
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    cmd = [sys.executable, str(watchdog), "--loop", args.loop, "--drain", "--seat", args.seat]
    return subprocess.run(cmd).returncode


def cmd_cleanup(args) -> int:
    cleanup = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cleanup.py"
    cmd = [sys.executable, str(cleanup), "--loop", args.loop]
    cmd += ["--pr", str(args.pr)] if args.pr else ["--sweep"]
    if args.dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd).returncode


def cmd_uninstall(args) -> int:
    loop = config.load_id(args.loop)
    # Forget first: a route the operator removed must not be put back by the next watchdog
    # sweep's self-heal (which only ever restores routes still in the intent record).
    try:
        route_intent.forget(loop, _routes_of(loop).values())
    except OSError as exc:
        print(f"refused: route intent record could not be updated, routes left in place: {exc}")
        return 2
    for name in _routes_of(loop).values():
        if name and routes.remove_route(name):
            print(f"route removed: {name}")
    if not args.keep_config:
        path = config.config_dir() / f"{loop['id']}.json"
        if path.exists():
            path.unlink()
            print(f"config removed: {path}")
    print("GitHub hooks and the cron job are NOT removed automatically:")
    print(f"  hooks: hermes review-loop arm --loop {loop['id']}  # to pause them first")
    print("  cron:  hermes cron list | grep review-loop-watchdog && hermes cron remove <id>")
    return 0


# What the plugin's settings form held when this process started (Capabilities → Plugins in the
# desktop). Set by `register_cli`; read by `apply` and `settings`. A form that silently renumbered
# a running loop would be a nasty thing to debug, so these are *defaults* and a push — never a
# subscription.
_SETTINGS: dict = {}


def register_cli(ctx, settings: dict | None = None) -> None:
    """Wire ``hermes review-loop`` into the CLI.

    ``settings`` is the plugin-level settings form. It supplies the defaults a new loop starts
    from, and what ``apply`` pushes onto an existing loop; it never rewrites a loop behind the
    operator's back.
    """
    global _SETTINGS
    _SETTINGS = dict(settings or {})
    d = config.settings_defaults(settings)
    # Both seats agreeing is the only case where a loop-level default says anything useful: when
    # they disagree the two seat values below carry the answer explicitly.
    both = d["reviewer_concurrency"] if d["reviewer_concurrency"] == d["fixer_concurrency"] else 1

    def setup(parser) -> None:  # noqa: ANN001
        """Build the command's argparse tree.

        The framework hands this the parser for ``hermes review-loop`` itself — the subcommands are
        ours to create. (Claiming a subparsers action here compiles, loads, validates, and then
        quietly offers zero subcommands: ``hermes review-loop list`` is "unrecognized arguments".)
        """
        sub = parser.add_subparsers(dest="command", metavar="<command>")

        def _usage(_args) -> int:      # bare `hermes review-loop` prints the commands, not an error
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
        init.add_argument("--concurrency", type=int, default=both,
                          help="default PRs per seat at once: 1 = serialized (default). "
                               "Above 1 needs --clone, because each run then gets its own clone. "
                               "Override per seat with --reviewer-concurrency / --fixer-concurrency.")
        init.add_argument("--reviewer-concurrency", type=int, default=d["reviewer_concurrency"],
                          help="PRs the reviewer may work at once (overrides --concurrency)")
        init.add_argument("--fixer-concurrency", type=int, default=d["fixer_concurrency"],
                          help="PRs the fixer may work at once (overrides --concurrency)")
        init.add_argument("--base", default=d["base"])
        init.add_argument("--clone", default=d["clone"], help="local clone the runs may use")
        init.add_argument("--root", action="append", default=[], help="a directory reviews may clean (repeatable)")
        init.add_argument("--state-dir", default="")
        init.add_argument("--token", action="append", default=[], help="login=/path/to/pat (repeatable)")
        init.add_argument("--read-token", default="", help="login whose token reads GitHub")
        init.add_argument("--skill", default="",
                          help="skill the seats are told to load. A plugin-provided skill is "
                               "qualified, e.g. hermes-review-loop:review-loop")
        init.add_argument("--adjudicator-route", default="")
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
                               "opened,handoff,verdict,approved,escalation,ruling,stall,closed "
                               "(default: all)")
        init.add_argument("--observer-digest-min", type=int, default=0,
                          help="batch the feed into one message per this many minutes "
                               "(0 = one notice per transition)")
        init.add_argument("--host", default=d["host"],
                          help="your gateway webhook origin (required unless set in plugin settings)")
        init.add_argument("--grace-min", type=int, default=d["grace_min"])
        init.add_argument("--ttl-min", type=int, default=d["ttl_min"],
                          help="how long a run may hold its seat slot")
        init.add_argument("--inflight-ttl-min", type=int, default=d["inflight_ttl_min"],
                          help="how long an in-flight mark blocks a second run at the same head")
        init.add_argument("--hooks", action="store_true", help="create the GitHub hooks too")
        init.add_argument("--admin-token", default="", help="login whose token can create hooks")
        init.add_argument("--schedule", default="", help="e.g. 15m — install the watchdog cron job")
        init.add_argument("--watchdog-deliver", default="local", help="cron delivery target for watchdog alerts")
        init.add_argument("--dry-run", action="store_true",
                          help="print the seat mapping and what would be written, write nothing")
        init.set_defaults(func=cmd_init)

        status = sub.add_parser("status", help="Show a loop's config and live state")
        status.add_argument("--loop")
        status.set_defaults(func=cmd_status)

        explain = sub.add_parser("explain",
                                 help="Why one PR is not moving, and what has to happen next")
        explain.add_argument("--loop", help="loop id (default: the only configured loop)")
        explain.add_argument("--pr", type=int, required=True, help="pull request number to explain")
        explain.set_defaults(func=cmd_explain)

        preflight = sub.add_parser("doctor", help="Preflight a loop read-only: profiles, tokens, "
                                                  "routes, hooks, scripts, cron, clone")
        preflight.add_argument("--loop")
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
        check.add_argument("--loop", required=True)
        check.add_argument("--pr", type=int, help="dry-run the reviewer write authorization on this PR")
        check.add_argument("--no-model", action="store_true",
                           help="skip the one tiny real completion (costs a few tokens)")
        check.add_argument("--live-turn", action="store_true",
                           help="with --pr: run one real isolated reviewer turn whose verdict is "
                                "printed and never posted")
        check.add_argument("--timeout", type=int, default=600,
                           help="live turn budget in seconds (default 600; the production worker uses its own child_timeout)")
        check.set_defaults(func=cmd_selftest)

        models = sub.add_parser("models", help="Read-only: list the models a seat's Hermes "
                                               "profile's provider offers (Hermes catalog)")
        models.add_argument("--profile", help="Hermes profile name")
        models.add_argument("--seat", choices=("reviewer", "fixer", "adjudicator"),
                            help="use this seat's profile from the loop config")
        models.add_argument("--loop", help="loop id for --seat (default: the only loop)")
        models.set_defaults(func=cmd_models)

        change = sub.add_parser("set", help="Change a loop's settings in place")
        change.add_argument("--loop", required=True)
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
        change.add_argument("--marker-grace-min", type=int)
        change.add_argument("--ttl-min", type=int, help="how long a run may hold its slot")
        change.add_argument("--inflight-ttl-min", type=int)
        change.add_argument("--host", help="gateway webhook host")
        change.add_argument("--observer-route", help="route the observer feed delivers through")
        change.add_argument("--observer-profile", help="profile that owns the observer destination")
        change.add_argument("--observer-deliver",
                            help="where the gateway delivers the feed (telegram, discord, ...)")
        change.add_argument("--observer-events", default=None,
                            help="comma-separated transitions to send, from "
                                 "opened,handoff,verdict,approved,escalation,ruling,stall,closed "
                                 "(blank = all)")
        change.add_argument("--observer-digest-min", type=int, default=None,
                            help="batch the feed into one message per N minutes (0 = per "
                                 "transition)")
        change.add_argument("--observer-mute", action="store_true",
                            help="stop the feed without forgetting it")
        change.add_argument("--observer-unmute", action="store_true", help="resume a muted feed")
        change.add_argument("--observer-disable", action="store_true",
                            help="drop this loop's observer config entirely")
        change.set_defaults(func=cmd_set)

        apply_cmd = sub.add_parser("apply", help="Push the plugin settings onto a loop")
        apply_cmd.add_argument("--loop", required=True)
        apply_cmd.add_argument("--dry-run", action="store_true",
                               help="show the diff without writing it")
        apply_cmd.add_argument("--while-busy", action="store_true",
                               help="rebind a seat's profile/login even while a run is in flight "
                                    "(that run keeps the identity it started with)")
        apply_cmd.set_defaults(func=cmd_apply)

        settings_cmd = sub.add_parser("settings",
                                      help="Show the plugin-level defaults a loop starts from")
        settings_cmd.set_defaults(func=cmd_settings)

        arm = sub.add_parser("arm", help="Activate the loop's GitHub hooks")
        arm.add_argument("--loop")
        arm.add_argument("--pause", action="store_true", help="pause instead of arming")
        arm.add_argument("--admin-token", default="")
        arm.set_defaults(func=cmd_arm)

        fixer_push = sub.add_parser("fixer-push", help="Explicit per-repository unattended fixer push policy")
        fixer_push.add_argument("--loop", required=True, help="exact loop id (never all loops)")
        direction = fixer_push.add_mutually_exclusive_group(required=True)
        direction.add_argument("--enable", action="store_true", help="opt this repository in")
        direction.add_argument("--disable", action="store_true", help="turn unattended pushes off")
        fixer_push.add_argument("--acknowledge-pr-race", action="store_true",
                                help="accept the residual non-atomic PR-metadata/ref race; required for --enable")
        fixer_push.add_argument("--dry-run", action="store_true", help="show action without writing")
        fixer_push.set_defaults(func=cmd_fixer_push)

        drain = sub.add_parser("drain", help="Start a queued run once its seat is free")
        drain.add_argument("--loop", required=True)
        drain.add_argument("--seat", default="reviewer", choices=["reviewer", "fixer"])
        drain.set_defaults(func=cmd_drain)

        cleanup = sub.add_parser("cleanup", help="Reclaim local disk for finished PRs")
        cleanup.add_argument("--loop", required=True)
        cleanup.add_argument("--pr", type=int)
        cleanup.add_argument("--dry-run", action="store_true")
        cleanup.set_defaults(func=cmd_cleanup)

        uninstall = sub.add_parser("uninstall", help="Remove a loop's routes and config")
        uninstall.add_argument("--loop", required=True)
        uninstall.add_argument("--keep-config", action="store_true")
        uninstall.set_defaults(func=cmd_uninstall)

    ctx.register_cli_command(
        "review-loop",
        "Unattended PR review loop between two agents (fixer + reviewer, budget counted in verdicts)",
        setup,
        description="Configure, inspect and drive review loops. Each loop is one JSON file under "
                    "~/.hermes/review-loops.d/, and it drives two webhook routes, two GitHub hooks "
                    "and (optionally) one cron watchdog job.",
    )
