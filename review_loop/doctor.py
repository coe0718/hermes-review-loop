"""Read-only preflight — *can this installation run the loop at all?*

``init`` writes a loop config, three routes and (optionally) two GitHub hooks and a cron job.
Every one of those can be syntactically perfect while the installation still cannot run: the
reviewer's profile does not exist, the token file named for the fixer went away in a key
rotation, the route in the gateway registry wakes a *different* profile than the loop config
says, the repo hook points at the operator's previous gateway, or the cron shim is still pinned
to the plugin directory a previous upgrade left behind. A loop that looks armed and cannot wake
a seat — or cannot post a verdict — is the failure this plugin exists to make loud, so the
preflight answers it before anyone arms anything:

    hermes review-loop doctor --loop attest

One line per check, in one of four states:

* ``verified`` — checked, and correct;
* ``absent`` — the thing is not there (a missing profile, token file, route, hook, job, script);
* ``mismatch`` — present, but not what this loop needs (a route waking the wrong profile, a hook
  pointing at another gateway, a shim pinned to a stale plugin path, a world-readable PAT);
* ``unknown`` — could not be decided *from here* (a hooks read the token was not allowed to make,
  a probe skipped with ``--offline``).

``unknown`` is never folded into ``absent``. "The API refused to tell me" and "there are no
hooks" are different claims, and printing the second one when the first is true sends the
operator hunting for a hook that exists. Failures (``absent``/``mismatch``) exit 1 so a
preflight can gate an install; ``--strict`` makes ``unknown`` a failure too.

Two things this deliberately never does:

* **It writes nothing** — no config, no route registry, no state, no GitHub hook. A preflight
  that repairs what it checks cannot be run against a live install to find out what is wrong.
* **It never fires a route.** A synthetic POST at a seat's route is a real agent run with a real
  budget, so the network side here is a TCP connect (is something listening at the gateway?) and,
  when the token is allowed to, a read of the repo's hooks. There is no test-fire mode on
  purpose: the way to test a route is to hand GitHub a real event.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
from datetime import datetime
from urllib.parse import urlsplit

from . import config, gh, route_intent, routes

VERIFIED = "verified"
ABSENT = "absent"
MISMATCH = "mismatch"
UNKNOWN = "unknown"
FAILURES = (ABSENT, MISMATCH)
MARKS = {VERIFIED: "✅", ABSENT: "❌", MISMATCH: "❌", UNKNOWN: "⚠️"}

# What `init` writes, mirrored here because ``cli`` imports this module (so this module cannot
# import ``cli``) and `tests/run_tests.py` asserts the two spellings agree — a preflight that
# looks for a filename nothing writes would report a healthy install as broken.
SHIM_NAME = "review-loop-watchdog.py"
PLUGIN_SCRIPTS = ("watchdog.py", "gate_reviewer.py", "gate_fixer.py",
                  "gate_adjudicator.py", "cleanup.py")
GATE_SCRIPT = {"reviewer": "gate_reviewer.py", "fixer": "gate_fixer.py"}
GATE_EVENT = {"reviewer": "pull_request", "fixer": "pull_request_review"}


class Check:
    """One line of the preflight: what was asked, what was found, and how to fix it."""

    def __init__(self, name: str, status: str, detail: str, fix: str = "") -> None:
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix

    @property
    def failed(self) -> bool:
        return self.status in FAILURES


def plugin_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def scripts_dir() -> pathlib.Path:
    """Where the plugin's own gate/watchdog/cleanup scripts live — read at call time, so a test
    can point the check at an empty directory without touching the installed plugin."""
    return plugin_root() / "scripts"


def profile_dir(name: str) -> pathlib.Path:
    """A seat profile's home: the root itself for ``default``, else ``profiles/<name>``.

    That is the layout the gateway uses, and it is also where the gates look for a seat's
    ``.env`` (the start-ping reads ``<home>/profiles/<profile>/.env``), so "does this profile
    exist" is answerable offline and without asking the gateway anything.
    """
    if not name or name == "default":
        return config.home()
    return config.home() / "profiles" / name


def shim_path() -> pathlib.Path:
    return config.home() / "scripts" / SHIM_NAME


def cron_store() -> pathlib.Path:
    """The scheduler's job store. Its own file, so the preflight needs no scheduler running."""
    return config.home() / "cron" / "jobs.json"


def watchdog_job_name(loop: dict) -> str:
    return f"review loop watchdog ({loop['id']})"


def init_fix(loop: dict, extra: str = "") -> str:
    """The one remediation that fixes most route/shim problems: rewrite them from the config.

    Worded as a command the operator can actually run, not as advice: the plugin never writes
    anything itself during a preflight.
    """
    tail = f" {extra}" if extra else ""
    return (f"re-run `hermes review-loop init --repo {loop['repo']} ...` for this loop to "
            f"regenerate it from the config{tail}")


def cron_fix(loop: dict) -> str:
    return (f"`hermes cron create 15m --name \"{watchdog_job_name(loop)}\" --no-agent "
            f"--script {SHIM_NAME} --deliver local` (or re-run init with --schedule 15m)")


def _env_keys(path: pathlib.Path) -> set[str]:
    """Keys with nonempty values in a profile env; never return or report the values."""
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return set()
    keys = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip() and not value.strip().startswith("#"):
            keys.add(key.strip())
    return keys


def _nearest_dir(path: pathlib.Path) -> pathlib.Path | None:
    """The deepest existing directory at or above ``path`` — what a write would land in."""
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return None


# -- the checks ------------------------------------------------------------------


def check_config(loop: dict) -> Check:
    path = config.config_dir() / f"{loop['id']}.json"
    if not path.exists():
        return Check("config", ABSENT, f"no loop config at {path}",
                     "write one with `hermes review-loop init`: the gates select a loop by the "
                     "payload's repository, so a loop nobody can load drives nothing")
    try:
        json.loads(path.read_text())
    except Exception as exc:
        return Check("config", MISMATCH, f"{path} is not readable JSON ({exc})",
                     "repair the file (or re-run init): every gate reads it on every event")
    return Check("config", VERIFIED,
                 f"{path} (repo {loop['repo']}, cap {loop['cap']}, base {loop['base']})")


def _check_profile(name: str, seat: str) -> Check:
    if not name:
        return Check(f"profile:{seat}", ABSENT, "no profile named for this seat",
                     f"re-run init with --{seat}-profile <an existing profile>")
    path = profile_dir(name)
    if path.is_dir():
        return Check(f"profile:{seat}", VERIFIED, f"{name} → {path}")
    return Check(f"profile:{seat}", ABSENT, f"no profile home at {path}",
                 f"`hermes profile create {name}`, or re-run init with --{seat}-profile pointing "
                 f"at a profile that exists: the run happens as this profile")

def check_profile(loop: dict, seat: str) -> Check:
    return _check_profile(str(loop["seats"][seat].get("profile") or ""), seat)

def check_adjudicator_profile(loop: dict) -> Check:
    return _check_profile(str((loop.get("adjudicator") or {}).get("profile") or "default"),
                          "adjudicator")



def runtime_settings() -> tuple[dict | None, Check | None]:
    """The runtime file's settings for the model checks, or a check explaining why not."""
    from . import seat_model
    path = config.home() / "review-loop-runtime.json"
    if not path.exists():
        return None, None
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("must be a private (0600) regular file")
        return seat_model.load_runtime(path), None
    except (OSError, ValueError) as exc:
        return None, Check("runtime", MISMATCH, f"{path}: {exc}",
                           f"repair {path}; `hermes review-loop selftest` shows each problem")


def check_seat_models(loop: dict) -> list[Check]:
    """Each seat's model as the worker will pick it — profile, provider, model; never the key.

    Read-only: the profile's ``config.yaml`` ``model`` block is read by the Hermes interpreter,
    without resolving (or refreshing) any credential. ``selftest`` resolves the credential and
    makes one tiny completion per seat.
    """
    from . import seat_model
    settings, problem = runtime_settings()
    checks = [problem] if problem else []
    if settings is not None and seat_model.legacy_override(settings) is not None:
        checks.append(Check("runtime:legacy-model", UNKNOWN,
                            "review-loop-runtime.json still sets a top-level model/upstream/"
                            "key_file: a LEGACY fallback used only for a seat whose profile "
                            "cannot be resolved",
                            "drop it once every seat's profile resolves, or move it under "
                            "seats.<seat> as an explicit per-seat override"))
    status_of = {"ok": VERIFIED, "warn": UNKNOWN, "fail": ABSENT}
    for seat in seat_model.seats_for(loop):
        status, detail, fix = seat_model.describe_seat(loop, seat, settings)
        checks.append(Check(f"model:{seat}", status_of[status], detail, fix))
    return checks

def _token_file_facts(path: pathlib.Path) -> str:
    """``path (exists: yes, private: no)`` — metadata only; the file is never opened here."""
    exists = path.exists()
    private = exists and not config.token_file_problem(str(path))
    return (f"{path} (exists: {'yes' if exists else 'no'}, "
            f"private: {'yes' if private else 'no'})")


def check_credential(loop: dict, seat: str) -> Check:
    """The gates use gh.token_path, not the profile's GH_TOKEN environment variable."""
    login = str(loop["seats"][seat].get("login") or "")
    tokens = loop.get("tokens") or {}
    if login:
        path = gh.token_path(loop, login)
        if path and path.is_file() and path.stat().st_size > 0:
            facts = _token_file_facts(path)
            if config.token_file_problem(str(path)):
                return Check(f"credential:{seat}", MISMATCH,
                             f"{login} → {facts}: {config.token_file_problem(str(path))}",
                             f"chmod 600 {path} (and own it): a token file other users can read "
                             "is a shared credential")
            return Check(f"credential:{seat}", VERIFIED,
                         f"{login} → {facts}, nonempty (identity and API access not checked)")
    env = profile_dir(str(loop["seats"][seat].get("profile") or "")) / ".env"
    if "GH_TOKEN" in _env_keys(env):
        return Check(f"credential:{seat}", ABSENT,
                     f"nonempty GH_TOKEN in {env}, but no mapped token file for "
                     f"{login or seat}; a profile environment alone does not provide the "
                     "gate's configured GitHub identity",
                     f"add --token {login or '<login>'}=/path/to/pat for this seat")
    who = login or f"the {seat} seat"
    mapped = gh.token_path(loop, login) if login else None
    if mapped is not None:
        return Check(f"credential:{seat}", ABSENT,
                     f"{login} → {_token_file_facts(mapped)}: missing or empty",
                     f"write the PAT for {login} to {mapped} (chmod 600)")
    return Check(f"credential:{seat}", ABSENT,
                 f"no tokens entry for {who!r} and no GH_TOKEN in {env}",
                 f"re-run init with --token {login or '<login>'}=/path/to/pat, or put "
                 f"GH_TOKEN=<pat> in {env} — a seat with neither cannot push or post a verdict")


def check_adjudicator_identity(loop: dict) -> Check | None:
    """The optional identity a ruling is also posted as; ``None`` when the loop has none.

    Without one, rulings go to the operator feed and the host ledger only — that is a valid
    configuration, not a failure, so nothing is reported. With one, it must be a fourth account:
    its own login and its own token file, never the reader's or a seat's. (Distinct *principals*
    need a network read; the broker verifies those via ``/user`` before every comment.)
    """
    login = config.adjudicator_login(loop)
    if not login:
        return None
    seats = loop.get("seats") or {}
    others = {"read_token": loop.get("read_token"),
              "reviewer": (seats.get("reviewer") or {}).get("login"),
              "fixer": (seats.get("fixer") or {}).get("login")}
    for role, other in others.items():
        if isinstance(other, str) and other and other.casefold() == login.casefold():
            return Check("credential:adjudicator", MISMATCH,
                         f"{login} is also the {role} — a ruling would post as that identity",
                         "set seats.adjudicator.login to its own account, or remove it for "
                         "operator-only rulings")
    path = gh.token_path(loop, login)
    if path is None:
        return Check("credential:adjudicator", ABSENT, f"no tokens entry for {login!r}",
                     f"add --token {login}=/path/to/pat, or remove seats.adjudicator.login for "
                     "operator-only rulings")
    if not path.is_file() or path.stat().st_size == 0:
        return Check("credential:adjudicator", ABSENT,
                     f"{login} → {_token_file_facts(path)}: no nonempty token file",
                     f"write the PAT for {login} (chmod 600)")
    if config.token_file_problem(str(path)):
        return Check("credential:adjudicator", MISMATCH,
                     f"{login} → {_token_file_facts(path)}: "
                     f"{config.token_file_problem(str(path))}",
                     f"chmod 600 {path} (and own it): the ruling identity needs a private "
                     "credential")
    for role, other in others.items():
        theirs = gh.token_path(loop, other) if isinstance(other, str) and other else None
        try:
            shared = theirs is not None and theirs.exists() and path.samefile(theirs)
        except OSError:
            shared = True
        if shared:
            return Check("credential:adjudicator", MISMATCH,
                         f"{login} reads the same token file as the {role}",
                         f"give {login} its own PAT file: a shared file is one account")
    return Check("credential:adjudicator", VERIFIED,
                 f"{login} → its own token file {_token_file_facts(path)}; rulings are also posted as a PR comment "
                 "(principal checked by the broker before each comment)")


def check_token(login: str, raw: str) -> Check:
    """One named credential file. The PAT's *bytes* are never printed, hashed or compared."""
    path = pathlib.Path(str(raw)).expanduser()
    if not path.exists():
        return Check(f"token:{login}", ABSENT, f"no file at {path}",
                     f"write the PAT for {login} to {path} (chmod 600), or re-run init with "
                     f"--token {login}=<a path that exists>")
    if not path.is_file():
        return Check(f"token:{login}", MISMATCH, f"{path} is not a file",
                     f"point --token {login} at a regular file holding the PAT")
    try:
        body = path.read_text()
    except Exception as exc:
        return Check(f"token:{login}", MISMATCH, f"{path} cannot be read ({exc})",
                     f"chmod 600 {path} so the user the gateway runs as can read it")
    if not body.strip():
        return Check(f"token:{login}", MISMATCH, f"{path} is empty",
                     f"write the PAT into {path}: an empty credential reads as an anonymous "
                     f"request, which GitHub answers with 404")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return Check(f"token:{login}", MISMATCH,
                     f"{path} is mode {mode:03o} — readable by group/other users",
                     f"chmod 600 {path}")
    return Check(f"token:{login}", VERIFIED, f"{path} (mode {mode:03o}, non-empty)")


def check_tokens(loop: dict) -> list[Check]:
    tokens = loop.get("tokens") or {}
    if not tokens:
        return [Check("tokens", ABSENT, "no token file is named for any login",
                      "re-run init with --token <login>=/path/to/pat (the gates read GitHub "
                      "through it; without one they go silent)")]
    return [check_token(login, raw) for login, raw in sorted(tokens.items())]


def check_read_token(loop: dict) -> Check:
    name = str(loop.get("read_token") or "")
    if not name:
        return Check("read_token", ABSENT, "no login is named as the reader",
                     "re-run init with --read-token <login> and --token <login>=/path/to/pat")
    if name not in (loop.get("tokens") or {}):
        return Check("read_token", MISMATCH, f"read_token names {name!r}, which has no token file",
                     f"re-run init with --token {name}=/path/to/pat: the gates read every PR "
                     f"state as this login")
    return Check("read_token", VERIFIED, f"{name} (mapped in tokens)")


def _route_entry(data: dict, name: str) -> dict | None:
    entry = data.get(name)
    return entry if isinstance(entry, dict) else None


def check_routes(loop: dict) -> list[Check]:
    """Route/profile/secret correspondence, read from the gateway's own subscription file."""
    path = routes.subs_path()
    if not path.exists():
        return [Check("routes", ABSENT, f"no route registry at {path}",
                      "re-run init for this loop: it writes the routes into the subscription "
                      "file the gateway already reads")]
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return [Check("routes", MISMATCH, f"{path} is not readable JSON ({exc})",
                      "repair the subscription file: the gateway can route nothing out of it")]
    if not isinstance(data, dict):
        return [Check("routes", MISMATCH, f"{path} must hold a JSON object of routes",
                      "repair the subscription file: the gateway can route nothing out of it")]
    checks = [check_route(loop, data, seat) for seat in ("reviewer", "fixer")]
    adjudicator = check_adjudicator_route(loop, data)
    if adjudicator:
        checks.append(adjudicator)
    return _intent_overlay(loop, data, checks)


REPAIR_FIX = ("the next armed watchdog sweep restores it from the plugin's intent record with the "
              "same secret — or run `hermes review-loop doctor --repair` now; if the change was "
              "intended, make it through `hermes review-loop set/apply/uninstall` instead")


def _intent_overlay(loop: dict, data: dict, checks: list[Check]) -> list[Check]:
    """Compare the live registry with the plugin's own record of its routes (issue #1).

    Read-only, like every check here. A route another registry writer changed can still look
    well-formed (a fresh secret, say) — only the record knows it no longer matches GitHub's hook.
    With no record yet (an install that predates it) the route checks stand as they are.
    """
    try:
        intent = route_intent.load(loop)
    except route_intent.IntentError as exc:
        return checks + [Check("route-intent", MISMATCH, str(exc),
                               "restore the file, or re-run `hermes review-loop apply` so the "
                               "plugin records its routes again")]
    if intent is None:
        return checks
    drifted = route_intent.drift(loop, data, intent)
    by_name = {check.name: check for check in checks}
    for name in sorted(set(intent) & set(route_intent.routes_of(loop).values())):
        check = by_name.get(f"route:{name}")
        fields = drifted.get(name)
        if check is None:
            if fields:
                checks.append(Check(f"route:{name}", MISMATCH,
                                    "differs from the plugin's intent record: " + ", ".join(fields),
                                    REPAIR_FIX))
            continue
        if not fields:
            if check.status == VERIFIED:
                check.detail += " · matches intent record"
            continue
        what = ("erased by another registry writer" if fields == ["missing"]
                else "differs from the plugin's intent record: " + ", ".join(fields))
        if check.status == VERIFIED:
            check.status, check.detail = MISMATCH, what
        else:
            check.detail += f" ({what})"
        check.fix = REPAIR_FIX
    return checks


def check_route(loop: dict, data: dict, seat: str) -> Check:
    name = str(loop["seats"][seat].get("route") or "")
    profile = str(loop["seats"][seat].get("profile") or "")
    if not name:
        return Check(f"route:{seat}", ABSENT, "no route named for this seat",
                     f"re-run init for this loop: it names {seat} routes <id>-review/-fix")
    entry = _route_entry(data, name)
    if entry is None:
        return Check(f"route:{name}", ABSENT, f"not in {routes.subs_path().name}",
                     f"re-run init for this loop (it writes {name!r} with a generated secret), "
                     f"or `hermes webhook subscribe {name}`")
    if str(entry.get("profile") or "") != profile:
        return Check(f"route:{name}", MISMATCH,
                     f"wakes profile {entry.get('profile')!r}, but seats.{seat}.profile is "
                     f"{profile!r} — the wake would run the wrong agent",
                     f"re-run init with --{seat}-profile {profile or '<name>'} so the route and "
                     f"the loop config agree")
    if not str(entry.get("secret") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a secret",
                     f"re-run init for this loop: without a secret the hook signature can never "
                     f"verify, so every event would be rejected")
    if not str(entry.get("prompt") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a prompt",
                     "re-run init for this loop: the wake would start an agent with no protocol")
    script = str(entry.get("script") or "")
    if script != GATE_SCRIPT[seat]:
        return Check(f"route:{name}", MISMATCH,
                     f"runs gate script {script or '(none)'!r}, expected "
                     f"{GATE_SCRIPT[seat]!r}",
                     f"re-run init for this loop: {GATE_SCRIPT[seat]} is what decides whether an "
                     f"event starts a {seat} run")
    events = entry.get("events")
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        return Check(f"route:{name}", MISMATCH, "events must be a list of event names",
                     f"re-run init for this loop: the gateway needs an event list for {seat}")
    if GATE_EVENT[seat] not in events:
        return Check(f"route:{name}", MISMATCH,
                     f"events {events or '(none)'} do not include {GATE_EVENT[seat]!r}",
                     f"re-run init for this loop: the gateway only routes the events a route "
                     f"subscribes to, so {GATE_EVENT[seat]} never reaches this seat")
    host = str(loop.get("host") or "")
    try:
        url = routes.url_for(name, host or None)
    except config.ConfigError:
        return Check(f"route:{name}", MISMATCH, "invalid webhook host (URL withheld)",
                     f"fix the stored origin (`hermes review-loop set --loop {loop['id']} "
                     f"--host https://your-gateway.example`) and re-run init")
    if not url:
        return Check(f"route:{name}", ABSENT, "no webhook URL (neither the loop nor the route "
                                              "names a gateway origin)",
                     "pass --host https://your-gateway.example at init (or set the plugin's "
                     "webhook host), then re-run init so the route carries it")
    stored = str(entry.get("host") or "").removesuffix("/")
    if host and stored and stored != host:
        return Check(f"route:{name}", MISMATCH,
                     "registered gateway origin differs from the loop's configured origin (URLs withheld)",
                     "re-run init to rewrite the route: a hook or a manual POST still "
                     f"goes to the recorded origin")
    return Check(f"route:{name}", VERIFIED,
                 f"{profile} · {GATE_EVENT[seat]} · [webhook URL redacted]")


def check_adjudicator_route(loop: dict, data: dict) -> Check | None:
    """The escalation route must use the post-#21 adjudicator gate."""
    adjudicator = loop.get("adjudicator") or {}
    name = str(adjudicator.get("route") or "")
    if not name:
        return None
    profile = str(adjudicator.get("profile") or "default")
    entry = _route_entry(data, name)
    if entry is None:
        return Check(f"route:{name}", ABSENT, f"not in {routes.subs_path().name}",
                     f"re-run init with --adjudicator-route {name}: the breach marker is the only "
                     f"record of an escalation nobody is woken for")
    if str(entry.get("profile") or "") != profile:
        return Check(f"route:{name}", MISMATCH,
                     f"wakes profile {entry.get('profile')!r}, but adjudicator.profile is "
                     f"{profile!r}",
                     f"re-run init with --adjudicator-profile {profile}: the ruling must not "
                     f"happen as one of the two seats that just stalled")
    if not str(entry.get("secret") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a secret",
                     "re-run init for this loop: a wake without a secret cannot be signed")
    if not str(entry.get("prompt") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a prompt",
                     "re-run init for this loop: the adjudicator wake has no ruling protocol")
    events = entry.get("events")
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        return Check(f"route:{name}", MISMATCH, "events must be a list of event names",
                     "re-run init for this loop: the gateway needs an adjudicator event list")
    if "pull_request" not in events:
        return Check(f"route:{name}", MISMATCH,
                     f"events {events or '(none)'} do not include 'pull_request'",
                     "re-run init for this loop: breach wakes use pull_request events")
    script = str(entry.get("script") or "")
    if script in config.LEGACY_GATE_SCRIPTS["adjudicator"]:
        # Installed before the dedicated adjudicator gate. `init` refuses an existing loop, so
        # the hint names the command that rewrites this route in place, secret kept.
        return Check(f"route:{name}", MISMATCH,
                     f"runs {script!r} (installed by an older release), expected "
                     "'gate_adjudicator.py'",
                     f"run `hermes review-loop apply --loop {loop['id']}` to rebind it to "
                     "gate_adjudicator.py (its secret is kept)")
    if script != "gate_adjudicator.py":
        return Check(f"route:{name}", MISMATCH,
                     f"runs {script or '(none)'!r}, expected 'gate_adjudicator.py'",
                     "this route is not the loop's adjudicator gate — point adjudicator.route at "
                     "a route of its own")
    if not (scripts_dir() / script).is_file():
        return Check(f"route:{name}", ABSENT, f"gate_adjudicator.py missing from {scripts_dir()}",
                     "reinstall the plugin")
    return Check(f"route:{name}", VERIFIED, f"{profile} · adjudication wake")


def check_scripts() -> Check:
    missing = [name for name in PLUGIN_SCRIPTS if not (scripts_dir() / name).exists()]
    if missing:
        return Check("scripts", ABSENT, "missing from the plugin: " + ", ".join(missing),
                     "reinstall the plugin: the routes and the cron shim run these files by name, "
                     "so a missing one is a seat that can never be woken")
    return Check("scripts", VERIFIED, f"{scripts_dir()} (watchdog, three gates, cleanup)")


_WATCHDOG_LINE = re.compile(r"WATCHDOG\s*=\s*pathlib\.Path\((['\"])(?P<path>.+?)\1\)")


def check_shim(loop: dict) -> Check:
    """The cron shim, and the plugin path it is pinned to.

    The shim is written once, at init, with the *absolute* path of the watchdog that existed
    then. An upgrade that moves the plugin directory leaves a shim pointing at a path that no
    longer runs this code — the scheduler keeps firing it, and the loop keeps looking armed.
    """
    path = shim_path()
    if not path.exists():
        return Check("cron:shim", ABSENT, f"no {path}",
                     f"re-run init with --schedule 15m: it writes the shim and the cron job that "
                     f"runs the watchdog")
    try:
        text = path.read_text()
    except Exception as exc:
        return Check("cron:shim", MISMATCH, f"{path} cannot be read ({exc})",
                     f"chmod +r {path}, or re-run init --schedule 15m to rewrite it")
    # Do not run arbitrary installed shim code during a read-only preflight. Instead require
    # byte-for-byte identity with the shim init actually generates; a dead assignment, commented
    # line or altered subprocess invocation cannot pass merely by mentioning WATCHDOG.
    live = str(scripts_dir() / "watchdog.py")
    from . import cli
    expected = cli.SHIM.format(watchdog=pathlib.Path(live))
    if text != expected:
        return Check("cron:shim", MISMATCH, f"{path} differs from init's executable shim for {live}",
                     "re-run init --schedule 15m to rewrite the shim")
    pinned = live
    return Check("cron:shim", VERIFIED, f"{path} → {pinned}")


def check_cron_job(loop: dict) -> Check:
    """The scheduled job itself, read from the scheduler's own store (no scheduler needed)."""
    path = cron_store()
    if not path.exists():
        return Check("cron:job", ABSENT, f"no cron store at {path}",
                     cron_fix(loop))
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return Check("cron:job", MISMATCH, f"{path} is not readable JSON ({exc})",
                     "repair the job store (or re-run init --schedule 15m): a store the scheduler "
                     "cannot read is a watchdog that never sweeps")
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return Check("cron:job", MISMATCH, f"{path} has no job list",
                     "repair the job store (or re-run init --schedule 15m)")
    wanted = watchdog_job_name(loop)
    job = next((entry for entry in jobs if isinstance(entry, dict)
                and str(entry.get("name") or "").strip() == wanted), None)
    if job is None:
        # A job the operator wrote by hand for this loop: same shim, a name that names the loop.
        job = next((entry for entry in jobs if isinstance(entry, dict)
                    and pathlib.Path(str(entry.get("script") or "")).name == SHIM_NAME
                    and loop["id"] in str(entry.get("name") or "")), None)
    if job is None:
        return Check("cron:job", ABSENT, f"no job named {wanted!r} in {path}",
                     cron_fix(loop))
    job_id = str(job.get("id") or "?")
    # Match the scheduler's runnable predicate: a stored pause timestamp blocks firing even
    # when enabled=True and the display state has already been normalized to "scheduled".
    if (not job.get("enabled", True) or job.get("state") in ("paused", "completed")
            or bool(job.get("paused_at"))):
        state = job.get("state")
        reason = ("completed" if state == "completed" else "paused or disabled")
        fix = (cron_fix(loop) if state == "completed" else
               f"`hermes cron resume {job_id}`: the scheduler skips a disabled watchdog")
        return Check("cron:job", MISMATCH, f"{job_id} ({wanted}) is {reason}",
                     fix)
    if job.get("script") != SHIM_NAME or job.get("no_agent") is not True:
        return Check("cron:job", MISMATCH,
                     f"{job_id} runs {job.get('script')!r} (no_agent={job.get('no_agent')!r}), "
                     f"expected {SHIM_NAME!r} with --no-agent", cron_fix(loop))
    schedule_data = job.get("schedule")
    valid = False
    missing_croniter = False
    if isinstance(schedule_data, dict):
        kind = schedule_data.get("kind")
        if kind == "interval":
            interval = schedule_data.get("minutes")
            valid = type(interval) is int and interval > 0
        elif kind == "cron":
            expression = schedule_data.get("expr")
            if isinstance(expression, str):
                try:
                    from croniter import croniter
                except ImportError:
                    missing_croniter = True
                else:
                    try:
                        croniter(expression)
                        valid = True
                    except (ValueError, TypeError, KeyError):
                        pass
    if not valid and not missing_croniter:
        return Check("cron:job", MISMATCH,
                     f"{job_id} has no valid stored schedule (display text does not schedule work)",
                     cron_fix(loop))
    next_run = job.get("next_run_at")
    try:
        if not isinstance(next_run, str) or not next_run.strip():
            raise ValueError("missing next run")
        datetime.fromisoformat(next_run.replace("Z", "+00:00"))
    except ValueError:
        return Check("cron:job", MISMATCH,
                     f"{job_id} has no valid next_run_at — cannot verify the next wake "
                     "(the scheduler may recompute a missing value for a recurring job)",
                     cron_fix(loop))
    if missing_croniter:
        return Check("cron:job", UNKNOWN,
                     f"{job_id} cron schedule could not be validated here (croniter unavailable); "
                     "the stored job may be valid — check on the scheduler host")
    schedule = (job.get("schedule_display")
                or ((job.get("schedule") or {}).get("display") if isinstance(job.get("schedule"), dict)
                    else "")
                or "?")
    return Check("cron:job", VERIFIED, f"{job_id} {schedule}, next {next_run}")


def check_clone(loop: dict) -> Check:
    """The clone the runs isolate from — and the one rail that would delete it if it were wrong."""
    raw = str(loop.get("clone") or "")
    if not raw:
        return Check("clone", VERIFIED, "none configured — runs work in the checkout they find "
                                        "(fine at concurrency 1)")
    path = pathlib.Path(raw).expanduser()
    if not path.exists():
        return Check("clone", ABSENT, f"no clone at {path}",
                     f"clone it, or `hermes review-loop set --loop {loop['id']} --clone <path>`: "
                     f"the cleanup prunes worktrees through this path")
    if not path.is_dir() or not (path / ".git").exists():
        return Check("clone", MISMATCH, f"{path} is not a git checkout",
                     f"point `hermes review-loop set --loop {loop['id']} --clone` at the "
                     f"repository's working clone: isolation clones from it and cleanup prunes "
                     f"worktrees in it")
    # The tree the cleanup deletes, derived from config rather than re-spelled here — a clone that
    # lives inside it is a working copy the cleanup would take with the artifacts.
    root = config.artifacts_dir(loop, 1).parent
    try:
        inside = str(path.resolve()).startswith(str(root.resolve()) + os.sep)
    except Exception:
        inside = False
    if inside:
        return Check("clone", MISMATCH, f"{path} lives inside the loop's artifacts root {root}",
                     "move the clone out of the state directory: the cleanup deletes everything "
                     "under artifacts/, which would be your working copy")
    parallel = [seat for seat in ("reviewer", "fixer") if config.seat_concurrency(loop, seat) > 1]
    note = f" — isolation source for {', '.join(parallel)}" if parallel else ""
    return Check("clone", VERIFIED, f"{path} (git checkout){note}")


def check_state_dir(loop: dict) -> Check:
    path = pathlib.Path(str(loop["state_dir"])).expanduser()
    if path.exists() and not path.is_dir():
        return Check("state_dir", MISMATCH, f"{path} is not a directory",
                     "point state_dir at a directory: the locks, the queue and the artifacts all "
                     "live under it")
    parent = _nearest_dir(path) or path.parent
    if not os.access(parent, os.W_OK):
        return Check("state_dir", MISMATCH, f"{parent} is not writable",
                     f"make {parent} writable by the user the gateway runs as: the seat locks and "
                     f"the queue live under {path}")
    when = "exists" if path.exists() else f"created under {parent} on the first run"
    return Check("state_dir", VERIFIED, f"{path} ({when})")


def check_roots(loop: dict) -> Check:
    roots = [str(root) for root in (loop.get("roots") or [])]
    if not roots:
        return Check("roots", VERIFIED, "none configured — the cleanup reclaims nothing "
                                        "(runs are unaffected)")
    wrong = [root for root in roots
             if pathlib.Path(root).expanduser().exists()
             and not pathlib.Path(root).expanduser().is_dir()]
    if wrong:
        return Check("roots", MISMATCH, "not directories: " + ", ".join(wrong),
                     "fix them (re-run init with --root <dir>): the cleanup only ever deletes "
                     "inside a configured root")
    return Check("roots", VERIFIED, f"{len(roots)} configured: " + ", ".join(roots))


def gateway_reachable(host: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Is anything listening at the gateway's origin? A TCP connect — never a webhook POST,
    because a POST at a seat's route starts a real agent run."""
    parsed = urlsplit(host)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    name = parsed.hostname or host
    try:
        with socket.create_connection((name, port), timeout=timeout):
            return True, f"{name}:{port} accepts a connection"
    except Exception as exc:
        return False, f"{name}:{port} — {type(exc).__name__}: {exc}"


def check_gateway(loop: dict, offline: bool) -> Check:
    host = str(loop.get("host") or "")
    if not host:
        return Check("gateway", ABSENT, "no webhook host configured",
                     "pass --host https://your-gateway.example at init (or set the plugin's "
                     "webhook host): without one no route URL resolves and no hook can point at "
                     "this operator's gateway")
    if offline:
        return Check("gateway", UNKNOWN, "configured gateway not probed (--offline; URL withheld)")
    reachable, _ = gateway_reachable(host)
    if not reachable:
        return Check("gateway", ABSENT, "configured gateway unreachable (URL withheld)",
                     "start it (`hermes gateway status`, `hermes gateway start`), and check that "
                     "this origin is the one GitHub posts to; re-run with --offline to skip this "
                     "probe")
    return Check("gateway", VERIFIED, "configured gateway accepts a TCP connection (URL withheld)")


def check_hooks(loop: dict, offline: bool) -> list[Check]:
    """The two repo hooks GitHub posts to, matched by the URL each route resolves to.

    A read the token is not allowed to make is ``unknown``: the repo may have both hooks and
    none at all, and this call cannot tell those apart. Saying "absent" here would be the exact
    wrong-hook hunt the preflight exists to prevent.
    """
    expected = []
    for seat in ("reviewer", "fixer"):
        name = str(loop["seats"][seat].get("route") or "")
        try:
            url = routes.url_for(name, str(loop.get("host") or "") or None) if name else None
        except config.ConfigError:
            url = None
        if url:
            expected.append((seat, name, url))
    if not expected:
        return [Check("hooks", UNKNOWN, "not checked — no route URL resolves yet "
                                        "(see the route checks above)")]
    if offline:
        return [Check("hooks", UNKNOWN,
                      f"not probed (--offline) — {len(expected)} hook(s) were not read")]
    hooks = []
    complete = False
    for page in range(1, 1001):
        path = f"/repos/{loop['repo']}/hooks?per_page=100"
        batch = gh.api(loop, path if page == 1 else f"{path}&page={page}")
        if not isinstance(batch, list) or any(
            not isinstance(hook, dict) or not isinstance(hook.get("config"), dict)
            or not isinstance(hook["config"].get("url"), str)
            or not isinstance(hook.get("events"), list)
            or any(not isinstance(event, str) for event in hook["events"])
            or type(hook.get("active")) is not bool
            for hook in batch):
            break
        hooks.extend(batch)
        if len(batch) < 100:
            complete = True
            break
    if not complete:
        return [Check("hooks", UNKNOWN,
                      f"could not read the complete /repos/{loop['repo']}/hooks listing — nothing was proved about "
                      f"{len(expected)} hook(s) (a token without admin:repo_hook reads as denied)",
                      f"give the read token admin:repo_hook (or repo) scope and re-run; check by "
                      f"hand with `gh api repos/{loop['repo']}/hooks`")]
    checks = []
    for seat, name, url in expected:
        checks.append(check_hook(loop, hooks, seat, name, url))
    return checks


def check_hook(loop: dict, hooks: list, seat: str, name: str, url: str) -> Check:
    event = GATE_EVENT[seat]
    def posted_url(hook: dict) -> str:
        cfg = hook.get("config")
        return str(cfg.get("url") or "") if isinstance(cfg, dict) else ""

    exact = [hook for hook in hooks if posted_url(hook).rstrip("/") == url.rstrip("/")]
    # A wrong origin/profile for the same webhook route is a mismatch, not an absent hook.
    candidates = exact or [hook for hook in hooks if
                           urlsplit(posted_url(hook)).path.rstrip("/").endswith(
                               "/webhooks/" + name)]
    match = next((hook for hook in candidates if hook.get("active") and
                  event in (hook.get("events") or [])), None) or (candidates[0] if candidates else None)
    if match is None:
        return Check(f"hook:{name}", ABSENT, "no repo hook posts to [webhook URL redacted]",
                     f"re-run init --hooks --admin-token <login> (needs admin:repo_hook on "
                     f"{loop['repo']}), or add the hook by hand with that URL and the route's "
                     f"secret")
    hook_id = match.get("id")
    posted = posted_url(match)
    if posted.removesuffix("/") != url.removesuffix("/"):
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id} posts to another origin, not [webhook URL redacted]",
                     f"re-run init --hooks, or repoint hook {hook_id} at the route's URL: this "
                     f"loop cannot be woken through the old origin")
    events = [str(item) for item in (match.get("events") or [])]
    if event not in events:
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id} subscribes to {events or '(no events)'}, not {event!r}",
                     f"re-run init --hooks, or add {event!r} to hook {hook_id} on {loop['repo']}")
    if not match.get("active"):
        return Check(f"hook:{name}", MISMATCH, f"hook {hook_id} is paused",
                     f"`hermes review-loop arm --loop {loop['id']}` (or activate hook {hook_id} "
                     f"in the repo's settings)")
    content_type = match["config"].get("content_type")
    if content_type != "json":
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id} has content_type {content_type!r}, expected 'json'",
                     f"re-run init --hooks, or set hook {hook_id}'s content_type to json: "
                     "the gate reads a JSON payload, not form-encoded data")
    return Check(f"hook:{name}", VERIFIED,
                 f"hook {hook_id} → [webhook URL redacted] ({event}, active)")


# -- the report ------------------------------------------------------------------

_URL_IN_REPORT = re.compile(r"https?://[^\s`<>]+", re.IGNORECASE)


def _safe_report_text(text: str) -> str:
    """Never echo a configured URL: userinfo, path, query and fragment may all be secrets."""
    return _URL_IN_REPORT.sub("[webhook URL redacted]", text)


def check_loop(loop: dict, offline: bool = False) -> list[Check]:
    """Every check, in the order an operator reads an install: what it is, who runs it, what
    wakes it, what schedules it, and where it works."""
    checks = [check_config(loop)]
    for seat in ("reviewer", "fixer"):
        checks.append(check_profile(loop, seat))
        checks.append(check_credential(loop, seat))
    if str((loop.get("adjudicator") or {}).get("route") or ""):
        checks.append(check_adjudicator_profile(loop))
    checks.extend(check_seat_models(loop))
    identity = check_adjudicator_identity(loop)
    if identity:
        checks.append(identity)
    checks.extend(check_tokens(loop))
    checks.append(check_read_token(loop))
    checks.extend(check_routes(loop))
    checks.append(check_scripts())
    checks.append(check_shim(loop))
    checks.append(check_cron_job(loop))
    checks.append(check_clone(loop))
    checks.append(check_state_dir(loop))
    checks.append(check_roots(loop))
    checks.append(check_gateway(loop, offline))
    checks.extend(check_hooks(loop, offline))
    return checks


def report(loop: dict, checks: list[Check], strict: bool = False) -> int:
    """Print one loop's preflight. Returns 1 when something has to be fixed, else 0."""
    failed = [check for check in checks if check.failed]
    unknown = [check for check in checks if check.status == UNKNOWN]
    verified = [check for check in checks if check.status == VERIFIED]

    print()
    print(f"[{loop['id']}] {loop['repo']} — preflight "
          f"(read-only: it writes nothing and fires nothing)")
    for check in checks:
        print(f"  {MARKS[check.status]} {check.name:<20} {_safe_report_text(check.detail)}")
        if check.failed and check.fix:
            print(f"      fix: {_safe_report_text(check.fix)}")
    print()
    print(f"{loop['id']}: {len(verified)} verified, {len(failed)} failed, {len(unknown)} unknown "
          f"(of {len(checks)} checks)")
    if failed:
        print(f"  {len(failed)} failed: {', '.join(check.name for check in failed)} — "
              f"fix the ❌ lines above before this loop is armed.")
    elif unknown:
        print(f"  no failures — but {len(unknown)} check(s) could not be decided from here; "
              f"verify the ⚠️ lines by hand.")
    else:
        print("  every check passed — this loop can wake a seat and post a verdict.")
    if strict and unknown and not failed:
        print(f"  --strict: {len(unknown)} undecided check(s) count as a failure.")
    return 1 if failed or (strict and unknown) else 0
