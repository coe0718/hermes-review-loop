"""``hermes review-loop`` — install, inspect and drive the loop from the CLI.

The plugin does not try to own the gateway. Everything the loop needs beyond its own scripts —
webhook routes, GitHub hooks, a cron entry — is written through the same config surfaces the
operator would touch by hand, so `hermes review-loop uninstall` is really just the inverse of
`init` and nothing lives in a place you cannot see.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time

from . import config, gh, prompts, routes

SHIM_NAME = "review-loop-watchdog.py"

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
    return {"reviewer": f"{loop['id']}-review", "fixer": f"{loop['id']}-fix",
            "adjudicator": f"{loop['id']}-breach"}


def _install_routes(loop: dict) -> dict:
    names = routes_for(loop)
    host = loop.get("host") or config.DEFAULTS["host"]
    skill = loop.get("skill") or ""
    routes.new_route(
        names["reviewer"], profile=loop["seats"]["reviewer"]["profile"],
        prompt=prompts.REVIEWER, events=["pull_request"],
        script="gate_reviewer.py", deliver="discord", host=host,
        skills=[skill] if skill else [],
        description=f"{loop['repo']} — wake the reviewer for a new or requested review")
    routes.new_route(
        names["fixer"], profile=loop["seats"]["fixer"]["profile"],
        prompt=prompts.FIXER, events=["pull_request_review"],
        script="gate_fixer.py", deliver="discord", host=host,
        skills=[skill] if skill else [],
        description=f"{loop['repo']} — wake the fixer on a changes-requested verdict")
    if loop.get("adjudicator", {}).get("route"):
        routes.new_route(
            loop["adjudicator"]["route"], profile=loop["adjudicator"].get("profile", "default"),
            prompt=prompts.ADJUDICATOR, events=["pull_request"],
            script="gate_reviewer.py", deliver=loop["adjudicator"].get("deliver", "telegram"),
            host=host,
            description=f"{loop['repo']} — adjudicate a loop that spent its budget")
    return names


def _install_hooks(loop: dict, token_login: str | None) -> list[str]:
    """Create the two repo hooks via the API. Needs a token with admin:repo_hook on the repo."""
    names = routes_for(loop)
    host = loop.get("host") or config.DEFAULTS["host"]
    made = []
    for seat, event in (("reviewer", "pull_request"), ("fixer", "pull_request_review")):
        route_name = names[seat]
        url = routes.url_for(route_name, host)
        secret = (routes.route(route_name) or {}).get("secret", "")
        body = {"name": "web", "active": True, "events": [event],
                "config": {"url": url, "content_type": "json", "secret": secret,
                           "insecure_ssl": "0"}}
        result = gh.api(loop, f"/repos/{loop['repo']}/hooks", method="POST", body=body,
                        login=token_login or loop.get("read_token"))
        if isinstance(result, dict) and result.get("id"):
            made.append(f"hook {result['id']} → {url}")
        else:
            made.append(f"FAILED to create a hook for {url} "
                        f"(needs a token with admin:repo_hook on {loop['repo']})")
    return made


def _set_hooks(loop: dict, active: bool, token_login: str | None) -> list[str]:
    names = routes_for(loop)
    wanted = (names["reviewer"], names["fixer"])
    hooks = gh.api(loop, f"/repos/{loop['repo']}/hooks?per_page=100",
                   login=token_login or loop.get("read_token"))
    if not isinstance(hooks, list):
        return ["could not read the repo's hooks (token needs admin:repo_hook to change them)"]
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


def _install_schedule(loop: dict, schedule: str, deliver: str) -> list[str]:
    """A cron shim plus the job itself, through the scheduler's own CLI."""
    scripts = config.home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    watchdog = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
    shim = scripts / SHIM_NAME
    shim.write_text(SHIM.format(watchdog=watchdog))
    shim.chmod(0o755)
    hermes = shutil.which("hermes") or "hermes"
    cmd = [hermes, "cron", "create", schedule, "--name", f"review loop watchdog ({loop['id']})",
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


def _write_config(loop: dict) -> pathlib.Path:
    directory = config.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{loop['id']}.json"
    path.write_text(json.dumps({k: v for k, v in loop.items() if v not in ({}, [], "")},
                               indent=2, sort_keys=True))
    return path


def cmd_init(args) -> int:
    tokens = {}
    for pair in args.token or []:
        if "=" not in pair:
            print(f"--token expects login=/path/to/pat, got {pair!r}")
            return 2
        login, path = pair.split("=", 1)
        tokens[login] = path
    raw = {
        "id": args.id or args.repo.split("/")[-1],
        "repo": args.repo, "base": args.base, "cap": args.cap,
        "concurrency": args.concurrency,
        "fixers": args.fixer, "reviewers": args.reviewer,
        "reviewer_seat": args.reviewer_seat or (args.reviewer[0] if len(args.reviewer) == 1 else ""),
        "seats": {
            "reviewer": {"profile": args.reviewer_profile, "route": "",
                         "login": args.reviewer_seat or args.reviewer[0],
                         "agent": args.reviewer_agent},
            "fixer": {"profile": args.fixer_profile, "route": "",
                      "login": args.fixer[0], "agent": args.fixer_agent},
        },
        "adjudicator": ({"route": args.adjudicator_route, "profile": args.adjudicator_profile}
                        if args.adjudicator_route else {}),
        "skill": args.skill,
        "tokens": tokens, "read_token": args.read_token or (args.reviewer_seat or args.reviewer[0]),
        "clone": args.clone, "roots": args.root or [],
        "state_dir": args.state_dir or str(config.home() / "state" / "review-loops" / (args.id or args.repo.split("/")[-1])),
        "host": args.host, "grace_min": args.grace_min,
    }
    if not args.reviewer_seat and len(args.reviewer) > 1:
        print("with several reviewer logins, --reviewer-seat names which one this loop's route serves")
        return 2
    # A seat-level capacity wins over the loop default, so only write it when it was asked for.
    for seat, value in (("reviewer", args.reviewer_concurrency), ("fixer", args.fixer_concurrency)):
        if value is not None:
            raw["seats"][seat]["concurrency"] = value
    names = {"reviewer": f"{raw['id']}-review", "fixer": f"{raw['id']}-fix"}
    raw["seats"]["reviewer"]["route"] = names["reviewer"]
    raw["seats"]["fixer"]["route"] = names["fixer"]
    try:
        loop = config.normalize(raw)
    except config.ConfigError as exc:
        print(f"config refused: {exc}")
        return 2

    path = _write_config(loop)
    print(f"loop config written: {path}")
    for name in _install_routes(loop).values():
        print(f"route written: {name}")
    for line in _install_hooks(loop, args.admin_token) if args.hooks else []:
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

    Nothing route-side needs re-writing: prompts are rendered from the payload at fire time, so a
    new cap or concurrency takes effect on the next event. The rails still apply — ``concurrency``
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

    if not changes and not seat_changes:
        print("nothing to change — pass at least one setting "
              "(--concurrency, --reviewer-concurrency, --fixer-concurrency, --cap, --clone, ...)")
        return 0

    try:
        updated = config.normalize({**loop, **changes, "seats": seats})
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2

    path = _write_config(updated)
    for key, value in changes.items():
        print(f"  {key}: {loop.get(key)!r} → {value!r}")
    for seat, value in seat_changes.items():
        was = config.seat_concurrency(loop, seat)
        print(f"  {seat} concurrency: {was} → {value}  (this seat only)")
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
        print(f"  state:      {st.dir}")
        print(f"  seats:      reviewer={loop['seats']['reviewer']['login']} "
              f"({loop['seats']['reviewer']['profile']}) · "
              f"fixer={loop['seats']['fixer']['login']} ({loop['seats']['fixer']['profile']})")
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
        watch = st.watch()
        if watch.get("last_run"):
            print(f"  watchdog:   last run {watch['last_run']}")
    return 0


def cmd_arm(args) -> int:
    for loop in ([config.load_id(args.loop)] if args.loop else config.all_loops()):
        for line in _set_hooks(loop, not args.pause, args.admin_token):
            print(f"[{loop['id']}] {line}")
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
    names = routes_for(loop)
    for name in (names["reviewer"], names["fixer"], loop.get("adjudicator", {}).get("route")):
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


def register_cli(ctx) -> None:
    def setup(sub) -> None:  # noqa: ANN001
        sub.add_parser("list", help="List configured loops").set_defaults(func=cmd_list)

        init = sub.add_parser("init", help="Configure a loop and install its routes")
        init.add_argument("--repo", required=True, help="owner/name")
        init.add_argument("--id", help="loop id (default: the repository name)")
        init.add_argument("--fixer", action="append", required=True, help="GitHub login that pushes (repeatable)")
        init.add_argument("--reviewer", action="append", required=True, help="GitHub login that may review (repeatable)")
        init.add_argument("--reviewer-seat", help="the login the reviewer route serves")
        init.add_argument("--reviewer-profile", required=True, help="Hermes profile for the reviewer seat")
        init.add_argument("--fixer-profile", required=True, help="Hermes profile for the fixer seat")
        init.add_argument("--reviewer-agent", default="", help="display name for the reviewer (default: profile)")
        init.add_argument("--fixer-agent", default="", help="display name for the fixer")
        init.add_argument("--cap", type=int, default=3, help="verdicts allowed before adjudication")
        init.add_argument("--concurrency", type=int, default=1,
                          help="default PRs per seat at once: 1 = serialized (default). "
                               "Above 1 needs --clone, because each run then gets its own clone. "
                               "Override per seat with --reviewer-concurrency / --fixer-concurrency.")
        init.add_argument("--reviewer-concurrency", type=int, default=None,
                          help="PRs the reviewer may work at once (overrides --concurrency)")
        init.add_argument("--fixer-concurrency", type=int, default=None,
                          help="PRs the fixer may work at once (overrides --concurrency)")
        init.add_argument("--base", default="main")
        init.add_argument("--clone", default="", help="local clone the runs may use")
        init.add_argument("--root", action="append", default=[], help="a directory reviews may clean (repeatable)")
        init.add_argument("--state-dir", default="")
        init.add_argument("--token", action="append", default=[], help="login=/path/to/pat (repeatable)")
        init.add_argument("--read-token", default="", help="login whose token reads GitHub")
        init.add_argument("--skill", default="", help="skill the seats should load")
        init.add_argument("--adjudicator-route", default="")
        init.add_argument("--adjudicator-profile", default="default")
        init.add_argument("--host", default=config.DEFAULTS["host"], help="gateway webhook host")
        init.add_argument("--grace-min", type=int, default=25)
        init.add_argument("--hooks", action="store_true", help="create the GitHub hooks too")
        init.add_argument("--admin-token", default="", help="login whose token can create hooks")
        init.add_argument("--schedule", default="", help="e.g. 15m — install the watchdog cron job")
        init.add_argument("--watchdog-deliver", default="local", help="cron delivery target for watchdog alerts")
        init.set_defaults(func=cmd_init)

        status = sub.add_parser("status", help="Show a loop's config and live state")
        status.add_argument("--loop")
        status.set_defaults(func=cmd_status)

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
        change.set_defaults(func=cmd_set)

        arm = sub.add_parser("arm", help="Activate the loop's GitHub hooks")
        arm.add_argument("--loop")
        arm.add_argument("--pause", action="store_true", help="pause instead of arming")
        arm.add_argument("--admin-token", default="")
        arm.set_defaults(func=cmd_arm)

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
