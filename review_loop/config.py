"""Loop configuration: one JSON file per loop under ``~/.hermes/review-loops.d/``.

Everything a run must know about a repository is data here — the repo, its seats, its
budget, its credential files, the roots it is allowed to clean. Nothing is a constant in
the code, which is what lets one install serve several repositories with different
settings, and what keeps a stranger's repo names and handles out of the source.

File shape (all keys except ``repo`` have defaults)::

    {
      "id": "attest",
      "repo": "patchhive/attest",
      "base": "main",
      "cap": 3,
      "fixers": ["coe0718"],
      "reviewers": ["tuck-coe"],
      "reviewer_seat": "vex-coe",
      "seats": {
        "reviewer": {"profile": "vex", "route": "attest-pr-ready",
                     "login": "vex-coe", "agent": "Vex"},
        "fixer":    {"profile": "drey", "route": "attest-review-posted",
                     "login": "drey-coe", "agent": "Drey"}
      },
      "adjudicator": {"route": "attest-loop-breach", "profile": "default"},
      "observer": {"route": "attest-observe", "profile": "tuck", "deliver": "telegram",
                   "events": ["opened", "handoff", "verdict", "approved", "escalation",
                              "ruling", "stall", "closed"],
                   "digest_min": 0, "mute": false},
      "skill": "attest-pr-review",
      "read_token": "tuck-coe",
      "tokens": {"tuck-coe": "~/.hermes/keys/tuck-coe-pat"},
      "state_dir": "~/.hermes/state/review-loops/attest",
      "clone": "~/projects/attest",
      "roots": ["~/reviews", "~/.hermes/cache/scratch"],
      "grace_min": 25, "marker_grace_min": 60, "cooldown_h": 6,
      "ttl_min": 45, "inflight_ttl_min": 10
    }

``config.py`` is deliberately strict: a loop that cannot be resolved to a repository,
a base branch and two seats is a configuration error, not a run that guesses.

The one key that is *not* strict is ``observer`` — the optional read-only feed (see
``review_loop.observer``). A seat that cannot be resolved is a loop that cannot turn; an
observer that cannot be resolved is a loop that turns without telling anyone, so a broken feed
is dropped with its reason kept under ``misconfigured`` for ``status`` to report.
"""

from __future__ import annotations

import contextlib

import ipaddress
import json
import os
import pathlib
import re
import stat
from urllib.parse import urlsplit
from urllib.request import Request

# Plugin-level settings. The desktop's Capabilities → Plugins form renders `config_schema` from
# plugin.yaml; this table mirrors it so the CLI can use the same defaults without a YAML parser
# (the package is stdlib-only on purpose). `tests/run_tests.py` asserts the two agree, because a
# form that writes keys nothing reads is worse than no form at all.
SETTINGS_SCHEMA: dict = {
    "cap": {"label": "Review cap (verdicts)", "type": "int", "default": 3,
            "description": "Verdicts before the loop stops and hands the PR to an adjudicator "
                           "(cap - 1 fixes)"},
    "reviewer_concurrency": {"label": "Reviews at once", "type": "int", "default": 1,
                             "description": "Reviews that may run at once; everything above this "
                                            "queues. Above 1 needs a clone path, so each run gets "
                                            "its own sandbox."},
    "fixer_concurrency": {"label": "Fixes at once", "type": "int", "default": 1,
                          "description": "Fixes that may run at once; everything above this queues"},
    "clone": {"label": "Clone path (required above 1)", "type": "str", "default": "",
              "description": "Local clone the runs isolate from — required above 1, because a "
                             "parallel run in a shared checkout produces wrong verdicts"},
    "base": {"label": "Base branch", "type": "str", "default": "main",
             "description": "Base branch the loop watches"},
    "grace_min": {"label": "Watchdog grace (minutes)", "type": "int", "default": 25,
                  "description": "Minutes a quiet PR may sit before the watchdog speaks"},
    "ttl_min": {"label": "Seat slot TTL (minutes)", "type": "int", "default": 45,
                "description": "Minutes a seat slot survives — the backstop for a run that died "
                               "without a verdict"},
    "inflight_ttl_min": {"label": "In-flight mark TTL (minutes)", "type": "int", "default": 10,
                         "description": "Minutes an in-flight mark blocks a second run at the "
                                        "same head"},
    "host": {"label": "Webhook host", "type": "str", "default": "",
             "description": "Your gateway's public webhook origin (required to create GitHub hooks)"},
    "reviewer_profile": {"label": "Reviewer's Hermes profile", "type": "str", "default": "",
                         "description": "Profile the reviewer seat runs as. Blank = not set here: "
                                        "a new loop still needs one, and it must differ from the "
                                        "fixer's"},
    "fixer_profile": {"label": "Fixer's Hermes profile", "type": "str", "default": "",
                      "description": "Profile the fixer seat runs as. Blank = not set here: a new "
                                     "loop still needs one, and it must differ from the reviewer's"},
    "reviewer_login": {"label": "Reviewer's GitHub login", "type": "str", "default": "",
                       "description": "GitHub login the reviewer seat acts as — the login the "
                                      "review route serves. Must be in the loop's reviewers "
                                      "allowlist. Blank = not set here"},
    "fixer_login": {"label": "Fixer's GitHub login", "type": "str", "default": "",
                    "description": "GitHub login the fixer seat acts as. Must be in the loop's "
                                   "fixers allowlist. Blank = not set here"},
    "adjudicator_profile": {"label": "Adjudicator's Hermes profile (optional)", "type": "str",
                            "default": "",
                            "description": "Profile that rules when the verdict budget is spent. "
                                           "Blank = not set here: a loop with no adjudicator route "
                                           "is left exactly as it is"},
    # Token *paths* only. A token value typed into a settings form would be stored in plain text in
    # a config file this plugin never owns, so these keys hold where the PAT lives — never the PAT.
    "reviewer_token_file": {"label": "Reviewer's token file (path only)", "type": "str",
                            "default": "",
                            "description": "Absolute path (~ allowed) to the file holding the "
                                           "reviewer login's GitHub PAT — the path only, never the "
                                           "token itself. Must be your own mode-600 regular file. "
                                           "Blank = not set here"},
    "fixer_token_file": {"label": "Fixer's token file (path only)", "type": "str", "default": "",
                         "description": "Absolute path (~ allowed) to the file holding the fixer "
                                        "login's GitHub PAT — the path only, never the token "
                                        "itself. Must be your own mode-600 regular file. Blank = "
                                        "not set here"},
    "adjudicator_login": {"label": "Adjudicator's GitHub login (optional)", "type": "str",
                          "default": "",
                          "description": "Optional fourth account the ruling is also posted as, "
                                         "as a PR comment. Must differ from the reader and both "
                                         "seats, with its own token file. Lands only on a loop "
                                         "with an adjudicator route. Blank = not set here"},
    "adjudicator_token_file": {"label": "Adjudicator's token file (path only, optional)",
                               "type": "str", "default": "",
                               "description": "Absolute path (~ allowed) to the file holding the "
                                              "adjudicator login's GitHub PAT — the path only, "
                                              "never the token itself. Must be your own mode-600 "
                                              "regular file, not shared with any other login. "
                                              "Blank = not set here"},
}

# Seat identity: *who* a seat is. A profile decides the model, the budget and the credentials the
# run happens with; a login decides the GitHub attribution. Both are per loop — two repositories can
# legitimately be served by different profiles — so the plugin settings only ever supply defaults a
# new loop starts from, and `apply --loop` pushes them onto one loop at a time.
PROFILE_SETTINGS: dict = {"reviewer": "reviewer_profile", "fixer": "fixer_profile",
                          "adjudicator": "adjudicator_profile"}
LOGIN_SETTINGS: dict = {"reviewer": "reviewer_login", "fixer": "fixer_login"}
# Where each role's PAT lives. Paths, never values: see ``token_file_problem``.
TOKEN_FILE_SETTINGS: dict = {"reviewer": "reviewer_token_file", "fixer": "fixer_token_file",
                             "adjudicator": "adjudicator_token_file"}
# Every role that can own a webhook route. The adjudicator is here but not in ``SEAT_KEYS``: it has
# a route and a profile, and no login or allowlist of its own.
ROUTE_ROLES = ("reviewer", "fixer", "adjudicator")
# Gate scripts an older release of *this* plugin installed for a role. Before the dedicated
# adjudicator gate (PR #21) the breach route ran gate_reviewer.py. Such a route is still ours —
# its prompt proves it — so ``apply`` rebinds it in place instead of refusing it as foreign, and
# ``doctor`` points there rather than at ``init``, which refuses an existing loop.
LEGACY_GATE_SCRIPTS: dict = {"adjudicator": frozenset({"gate_reviewer.py"})}

# A Hermes profile name is a directory name under ``profiles/``. Refusing separators and dots-only
# names here is what keeps a typo from resolving to somewhere outside the profiles root.
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def settings_defaults(settings: dict | None) -> dict:
    """Effective plugin-level defaults: what the settings form set, else the schema default.

    A value of the wrong type falls back to the default rather than propagating a string into a
    field the loop does arithmetic on. Nothing here raises: a bad setting must not break the CLI.
    """
    out: dict = {}
    for key, spec in SETTINGS_SCHEMA.items():
        value = (settings or {}).get(key)
        if value is None or value == "":
            value = spec["default"]
        try:
            value = int(value) if spec["type"] == "int" else str(value)
        except (TypeError, ValueError):
            value = spec["default"]
        out[key] = value
    return out


def apply_settings(loop_raw: dict, settings: dict | None) -> dict:
    """The loop knobs the plugin settings own, overlaid on a raw (pre-``normalize``) loop dict.

    `apply` is deliberately a push, not a subscription: the form sets defaults, and a loop takes
    them when the operator says so. A running loop whose numbers changed under it would be a very
    confusing thing to debug at 2am.
    """
    d = settings_defaults(settings)
    seats = {k: dict(v or {}) for k, v in (loop_raw.get("seats") or {}).items()}
    seats.setdefault("reviewer", {})["concurrency"] = d["reviewer_concurrency"]
    seats.setdefault("fixer", {})["concurrency"] = d["fixer_concurrency"]
    # A blank clone in the form means "not set here", never "forget the clone this loop uses":
    # silently dropping it would quietly downgrade the cleanup, which prunes worktrees through it.
    clone = d["clone"] or str(loop_raw.get("clone") or "")
    # The per-seat numbers are written explicitly, so the loop-level default never has to be
    # guessed at: whatever `concurrency` says, the seats carry their own answered value.
    # An unset form value cannot erase an existing loop's explicitly configured gateway.
    host = d["host"] or loop_raw.get("host") or ""
    overlaid = {**loop_raw, "cap": d["cap"], "clone": clone, "base": d["base"],
                "host": host, "grace_min": d["grace_min"], "ttl_min": d["ttl_min"],
                "inflight_ttl_min": d["inflight_ttl_min"], "seats": seats}
    # Seat identity rides the same push: the form names who serves each seat, and a blank field
    # stays blank rather than unsetting what the loop already answered for itself.
    return apply_seats(overlaid, settings)


def profiles_root() -> pathlib.Path:
    return home() / "profiles"


def profile_dir(name: str) -> pathlib.Path:
    """Where a Hermes profile lives, whether or not it exists.

    The launch profile *is* the Hermes home; every other profile is a directory under
    ``profiles/``. Nothing is created here on purpose — a validation that mkdir'd would turn a
    typo into what looks like a working install.
    """
    name = str(name or "").strip()
    return home() if name in ("", "default") else profiles_root() / name


def profile_exists(name: str) -> bool:
    name = str(name or "").strip()
    if not name or not _PROFILE_NAME.fullmatch(name):
        return False
    if name == "default":
        return True
    profile = profile_dir(name)
    marker = profile / "config.yaml"
    # A symlink can give two differently named seats the same credentials and budget.
    # Do not accept an alias as a named profile, even if its target has a config.
    return (not profile.is_symlink() and profile.is_dir()
            and marker.is_file() and marker.stat().st_size > 0)


def seat_mapping(settings: dict | None) -> dict:
    """The seat identities the settings form names — only the keys the operator actually filled in.

    A blank profile/login is *not set here*, never "forget what this loop uses": that is what lets
    one per-profile form hold defaults for a new loop without quietly rewriting seats a loop
    already answered for itself.
    """
    d = settings_defaults(settings)
    mapping: dict = {}
    for seat, key in PROFILE_SETTINGS.items():
        entry: dict = {}
        if str(d.get(key) or "").strip():
            entry["profile"] = str(d[key]).strip()
        login_key = LOGIN_SETTINGS.get(seat) or ("adjudicator_login" if seat == "adjudicator"
                                                 else "")
        if login_key and str(d.get(login_key) or "").strip():
            entry["login"] = str(d[login_key]).strip()
        token_key = TOKEN_FILE_SETTINGS.get(seat)
        if token_key and str(d.get(token_key) or "").strip():
            entry["token_file"] = str(d[token_key]).strip()
        if entry:
            mapping[seat] = entry
    return mapping


def seat_profile(loop: dict, role: str) -> str:
    """What a seat (or the adjudicator) runs as, from the config it is actually driving with."""
    if role == "adjudicator":
        return str((loop.get("adjudicator") or {}).get("profile") or "default")
    return str(((loop.get("seats") or {}).get(role) or {}).get("profile") or "")


def seat_login(loop: dict, role: str) -> str:
    if role == "adjudicator":
        return ""
    return str(((loop.get("seats") or {}).get(role) or {}).get("login") or "")


def apply_seats(loop_raw: dict, settings: dict | None) -> dict:
    """Overlay the form's seat identities onto a raw loop, touching nothing it does not name.

    The reviewer's login and ``reviewer_seat`` move together: the reviewer route serves the login
    named there, and a form that changed one without the other would leave the route waking a
    login whose verdicts this loop does not count. An adjudicator profile only lands on a loop that
    already has an adjudicator *route* — a profile alone cannot wake anyone, and route names belong
    to the repository, not to the form.
    """
    mapping = seat_mapping(settings)
    if not mapping:
        return dict(loop_raw)
    raw = {**loop_raw}
    seats = {k: dict(v or {}) for k, v in (loop_raw.get("seats") or {}).items()}
    for seat in SEAT_KEYS:
        entry = mapping.get(seat) or {}
        profile = str(entry.get("profile") or "")
        if profile:
            seats.setdefault(seat, {})["profile"] = profile
            # The display name follows the profile unless the loop set its own: an agent name that
            # disagrees with the profile is a prompt telling the wrong agent it is speaking.
            if not str(seats[seat].get("agent") or "").strip():
                seats[seat]["agent"] = profile.capitalize()
        if str(entry.get("login") or ""):
            seats.setdefault(seat, {})["login"] = entry["login"]
    raw["seats"] = seats
    reviewer_login = str((mapping.get("reviewer") or {}).get("login") or "")
    if reviewer_login:
        raw["reviewer_seat"] = reviewer_login
    adj_entry = mapping.get("adjudicator") or {}
    adj_profile = str(adj_entry.get("profile") or "")
    adjudicator = dict(loop_raw.get("adjudicator") or {})
    if adj_profile and adjudicator.get("route"):
        adjudicator["profile"] = adj_profile
        raw["adjudicator"] = adjudicator
    # Token files: each seat's login → the path the form names. The value is never read here; the
    # path is checked (``verify_token_settings``) before anything is written.
    tokens = dict(loop_raw.get("tokens") or {})
    for seat in SEAT_KEYS:
        path = str((mapping.get(seat) or {}).get("token_file") or "")
        login = str(seats.get(seat, {}).get("login") or "")
        if path:
            if not login:
                raise ConfigError(f"{TOKEN_FILE_SETTINGS[seat]} is set but the {seat} seat has no "
                                  "login to map it to — set its login first")
            _map_token(tokens, login, path)
    # The adjudicator's comment identity, like its profile, only lands on a loop that already has an
    # adjudicator route: a login alone cannot rule on anything.
    adj_login = str(adj_entry.get("login") or "")
    adj_path = str(adj_entry.get("token_file") or "")
    if adjudicator.get("route") and (adj_login or adj_path):
        adj_seat = dict(seats.get("adjudicator") or {})
        if adj_login:
            adj_seat["login"] = adj_login
        login = str(adj_seat.get("login") or "")
        if adj_path:
            if not login:
                raise ConfigError("adjudicator_token_file is set but no adjudicator login is — set "
                                  "adjudicator_login (a token file names nobody on its own)")
            _map_token(tokens, login, adj_path)
        seats["adjudicator"] = adj_seat
    if tokens != dict(loop_raw.get("tokens") or {}):
        raw["tokens"] = tokens
    return raw


def _map_token(tokens: dict, login: str, path: str) -> None:
    """Point ``login`` at ``path``, dropping any case-variant key that would shadow it."""
    for key in [k for k in tokens if str(k).lower() == login.lower() and k != login]:
        del tokens[key]
    tokens[login] = str(_path(path))


def token_file_problem(value) -> str:
    """Why ``value`` cannot be a token file path, or ``""`` when it can.

    Only metadata is inspected — ``stat``, never ``open``: the PAT itself is not read, printed,
    hashed or copied. The rules: ``~`` expands, the result is absolute, it exists, it is a regular
    file owned by you, and group/other cannot read it (0600-style). A symlink is followed, as every
    other token reader in this plugin (``gh.token``, ``doctor``) follows it, and its *target* has
    to pass the same checks.
    """
    raw = str(value or "").strip()
    if not raw:
        return "no path given"
    path = _path(raw)
    if not path.is_absolute():
        return f"{raw!r} is not an absolute path (use /… or ~/…)"
    try:
        info = path.stat()
    except FileNotFoundError:
        return f"{path} does not exist"
    except OSError as exc:
        return f"{path} cannot be inspected ({exc.strerror or exc})"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} is not a regular file"
    if info.st_uid != os.getuid():
        return f"{path} is owned by uid {info.st_uid}, not you ({os.getuid()})"
    mode = info.st_mode & 0o777
    if mode & 0o077:
        return f"{path} is mode {mode:03o} — group/other can read it (chmod 600 {path})"
    return ""


def check_token_file(value, what: str) -> None:
    """Raise a named ``ConfigError`` when ``value`` is not a usable private token-file path."""
    problem = token_file_problem(value)
    if problem:
        raise ConfigError(f"{what}: {problem}")


def verify_adjudicator_token(loop: dict) -> None:
    """The adjudicator comment identity's token file must be a private absolute path.

    Checked when a CLI or settings push *writes* the identity; ``normalize`` already refused a
    missing mapping, a shared file and a login that is also the reader, a seat or an allowlisted
    account. The live ``/user`` principal check stays in the broker, before each comment.
    """
    login = adjudicator_login(loop)
    if login:
        check_token_file((loop.get("tokens") or {}).get(login),
                         f"token file for the adjudicator login {login!r}")


def verify_token_settings(settings: dict | None) -> None:
    """Every token-file path the settings form holds, checked before any write. Blank is skipped."""
    d = settings_defaults(settings)
    for key in TOKEN_FILE_SETTINGS.values():
        if str(d.get(key) or "").strip():
            check_token_file(d[key], key)


DEFAULTS: dict = {
    "base": "main",
    "cap": 3,
    "fixers": [],
    "reviewers": [],
    "reviewer_seat": "",
    "seats": {},
    "adjudicator": {},
    "observer": {},
    "skill": "",
    "read_token": "",
    "tokens": {},
    "clone": "",
    "roots": [],
    "concurrency": 1,         # runs allowed at once per seat; >1 requires isolation
    "grace_min": 25,          # how long a quiet head is allowed to sit before the watchdog speaks
    "marker_grace_min": 60,
    "cooldown_h": 6,
    "ttl_min": 45,            # seat lock lifetime: past this a crashed run has lost its seat
    "inflight_ttl_min": 10,
    "host": "",
    "unattended_fixer_push": False,  # per-repository; never inherited from plugin settings
}

def unattended_fixer_push_enabled(loop: dict) -> bool:
    """Only a literal opt-in in a trusted loop config authorizes unattended fixer pushes.

    Callers must load the loop from the host-owned config file, not an event payload or
    sandbox-supplied mapping. Hook arming, seat assignment and a legacy config are not consent.
    """
    return isinstance(loop, dict) and loop.get("unattended_fixer_push") is True

SEAT_KEYS = ("reviewer", "fixer")


def seat_concurrency(loop: dict, seat: str) -> int:
    """How many PRs this seat may work at once.

    A seat's own ``concurrency`` wins; the loop-level value is the default for every seat; and 1
    (serialized) is the fallback. Per seat is the shape that matters: a fixer and a reviewer are
    different models on different budgets, and wanting two reviews in flight rarely means wanting
    two fixes in flight.
    """
    seat_cfg = (loop.get("seats") or {}).get(seat) or {}
    value = seat_cfg.get("concurrency")
    if (value is None or value == "") and seat != "adjudicator":
        # The loop default is documented as the default for the two *working* seats. A ruling
        # is rare and read-only; it never inherits a parallelism the operator chose for reviews.
        value = loop.get("concurrency", 1)
    if value is None or value == "":
        value = 1
    return int(value)


def adjudicator_login(loop: dict) -> str:
    """The optional GitHub identity the adjudicator comments as, or ``""`` for operator-only.

    Unlike the two working seats the adjudicator needs no GitHub identity at all: a ruling is
    always delivered to the operator and recorded in the host ledger. Only when this is set is
    the ruling *also* posted on the PR, as this login and never as a seat or the reader.
    """
    return str((((loop.get("seats") or {}).get("adjudicator")) or {}).get("login") or "")


def _adjudicator_seat(raw, loop: dict, where: str) -> dict:
    """Validate ``seats.adjudicator`` — only a login (with its own token) and a concurrency.

    The broker re-checks every one of these properties against live credentials before any
    write; this is the early, file-level refusal, so an unsafe shape never loads at all.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict) or not set(raw) <= {"login", "concurrency"}:
        raise ConfigError(f"{where}: seats.adjudicator may only hold 'login' and 'concurrency'")
    seat: dict = {}
    if raw.get("concurrency") not in (None, ""):
        seat["concurrency"] = int(raw["concurrency"])
        if seat["concurrency"] < 1:
            raise ConfigError(f"{where}: seats.adjudicator.concurrency must be >= 1 (1 = serialized)")
    login = raw.get("login")
    if login in (None, ""):
        return seat
    if not isinstance(login, str) or not login.strip() or login != login.strip():
        raise ConfigError(f"{where}: seats.adjudicator.login must be a GitHub login")
    others = {str(x).casefold() for x in (
        loop.get("read_token"), loop.get("reviewer_seat"),
        (loop["seats"].get("reviewer") or {}).get("login"),
        (loop["seats"].get("fixer") or {}).get("login"),
        *loop["reviewers"], *loop["fixers"]) if x}
    if login.casefold() in others:
        raise ConfigError(f"{where}: seats.adjudicator.login {login!r} is also the reader, a seat "
                          "or an allowlisted reviewer/fixer — the ruling identity must be its own "
                          "account")
    tokens = loop.get("tokens") or {}
    if not tokens.get(login):
        raise ConfigError(f"{where}: seats.adjudicator.login {login!r} has no entry in 'tokens' — "
                          "an adjudicator identity without its own credential cannot comment")
    mine = _path(tokens[login])
    for other, raw_path in tokens.items():
        if other == login or not raw_path:
            continue
        theirs = _path(raw_path)
        same = os.path.realpath(mine) == os.path.realpath(theirs)
        if not same and mine.exists() and theirs.exists():
            try:
                same = mine.samefile(theirs)
            except OSError as exc:
                raise ConfigError(f"{where}: cannot verify adjudicator token file identity: "
                                  f"{exc}") from exc
        if same:
            raise ConfigError(f"{where}: the adjudicator and {other!r} read the same token file "
                              "— the ruling identity needs its own credential")
    seat["login"] = login
    return seat


def normalize_observer(raw) -> dict:
    """The observer feed's destination, or ``{}`` when this loop has no feed.

    Deliberately lenient where the seats are strict, and for one reason: an observer is
    read-only by construction, so a broken feed must never refuse a loop that can still turn.
    Anything unusable is dropped here and the *reason* is kept under ``misconfigured`` so
    ``hermes review-loop status`` says it out loud — silence is the failure mode this whole
    plugin exists to kill, and a feed that quietly delivers nothing would be a new one.

    ``events`` narrows the feed; absent or empty means every transition (``observer.EVENTS``).
    ``digest_min`` above zero batches transitions into one compact message flushed by the
    watchdog sweep instead of one notice per transition.
    """
    if raw is None or raw == "" or raw == {}:
        return {}
    if not isinstance(raw, dict):
        return {"route": "", "misconfigured": "observer must be a JSON object"}
    route = str(raw.get("route") or "").strip()
    if not route:
        return {"route": "", "misconfigured": "observer.route is required to deliver anything"}
    observer = {"route": route,
                "profile": str(raw.get("profile") or "default").strip() or "default",
                "deliver": str(raw.get("deliver") or "telegram").strip() or "telegram",
                "mute": bool(raw.get("mute"))}
    events = raw.get("events")
    if isinstance(events, str):
        events = re.split(r"[,\s]+", events)
    if isinstance(events, (list, tuple)):
        wanted = sorted({str(e).strip().lower() for e in events if str(e).strip()})
        if wanted:
            observer["events"] = wanted
    try:
        digest = int(raw.get("digest_min") or 0)
    except (TypeError, ValueError):
        digest = 0
    if digest > 0:
        observer["digest_min"] = digest
    return observer


class ConfigError(Exception):
    """A loop file that cannot be trusted to drive a run."""


def verify_credentials(loop: dict, roles: set[str] | None = None) -> None:
    """Check the loop's token *references* — never their values.

    Two rules, both learned the hard way. A mapping that names a file which is not there fails at
    the first API call, hours later, in a log nobody is reading; and a loop that maps tokens but
    leaves a seat's login unmapped quietly pushes as whatever ``read_token`` is, which is the wrong
    identity holding write access. Values are only ever read by ``gh.token()`` at use time, from a
    file the operator owns and this CLI does not print.
    """
    where = loop.get("id") or loop.get("repo") or "<inline>"
    tokens = {str(k).lower(): str(v or "") for k, v in (loop.get("tokens") or {}).items()}
    for login, raw_path in sorted(tokens.items()):
        if not raw_path:
            raise ConfigError(f"{where}: tokens.{login} names no file")
        path = _path(raw_path)
        if path.is_dir():
            raise ConfigError(f"{where}: token for {login!r} points at a directory ({path}), "
                              "not the file holding the PAT")
        if not path.is_file():
            raise ConfigError(f"{where}: token file for {login!r} is missing ({path}) — the seats "
                              "read that file at use time")
        try:
            empty = not path.read_text().strip()
        except OSError as exc:
            raise ConfigError(f"{where}: token file for {login!r} is unreadable ({path}): {exc}") from exc
        if empty:
            raise ConfigError(f"{where}: token file for {login!r} is empty ({path}) — an empty PAT "
                              "reads as an unauthenticated call, not as a loud failure")

    roles = set(roles or ())
    read_token = str(loop.get("read_token") or "").lower()
    if roles and read_token and read_token not in tokens:
        raise ConfigError(f"{where}: read_token {read_token!r} has no entry in 'tokens' — the gates "
                          "read GitHub as that login")

    for seat in SEAT_KEYS:
        if seat not in roles:
            continue                       # a loop that predates this check keeps loading
        login = seat_login(loop, seat).lower()
        if login and login not in tokens:
            raise ConfigError(f"{where}: no token mapped for the {seat} login {login!r} — add "
                              f"--token {login}=/path/to/pat, or the {seat} acts as {read_token!r}")
    if roles & set(SEAT_KEYS):
        # Distinct role credentials: two seats sharing one PAT is one account wearing two hats, and
        # the loop's whole point is that a different account reviews the fixer's work.
        reviewer, fixer = seat_login(loop, "reviewer").lower(), seat_login(loop, "fixer").lower()
        if reviewer in tokens and fixer in tokens and reviewer != fixer:
            try:
                same_file = _path(tokens[reviewer]).samefile(_path(tokens[fixer]))
            except OSError as exc:
                raise ConfigError(f"{where}: cannot verify reviewer/fixer token file identity: "
                                  f"{exc}") from exc
            if same_file:
                raise ConfigError(f"{where}: the reviewer and the fixer read the same token file "
                                  "— each seat needs its own credential")


def same_profile_home(first: str, second: str) -> bool:
    """Compare actual directory identity, not just names (also catches bind mounts).

    Legacy loops may mention a missing, untouched profile; their existence is checked only when
    a role is rebound. A missing home has no identity to compare, but other stat failures must
    not silently turn a potentially shared home into two independent seats.
    """
    if not first or not second:
        return False
    try:
        return profile_dir(first).samefile(profile_dir(second))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConfigError(f"cannot verify profile home identity for {first!r} and {second!r}: "
                          f"{exc}") from exc


def verify_seats(loop: dict, roles: set[str] | None = None) -> None:
    """Refuse a seat mapping that could not drive a run — before any file is written.

    Existence and allowlist membership are checked only for the roles this operation *writes*: a
    loop created before this validation existed may name a profile this machine never had, and
    rewriting its ``cap`` must not be the moment that surfaces. The combination checks (two seats
    on one profile, two seats on one login) always run against the *effective* loop, because the
    unsafe shape is the combination, not either half.

    Order matters for the error message, not for the outcome: profiles first, because a wrong
    profile name is the mistake an operator makes in a form.
    """
    roles = set(roles or ())
    where = loop.get("id") or loop.get("repo") or "<inline>"
    for seat in SEAT_KEYS:
        if seat not in roles:
            continue
        profile = seat_profile(loop, seat).strip()
        login = seat_login(loop, seat).strip().lower()
        if not profile:
            raise ConfigError(f"{where}: seats.{seat}.profile is required — the seat cannot run as "
                              "nobody")
        if not profile_exists(profile):
            raise ConfigError(
                f"{where}: no Hermes profile named {profile!r} for the {seat} seat (looked in "
                f"{profiles_root()}) — create the profile, or name one that exists")
        allowlist = [str(x).lower() for x in (loop.get("reviewers") if seat == "reviewer"
                                              else loop.get("fixers")) or []]
        if login and login not in set(allowlist):
            raise ConfigError(
                f"{where}: the {seat} login {login!r} is not in this loop's "
                f"{'reviewers' if seat == 'reviewer' else 'fixers'} allowlist "
                f"({', '.join(allowlist) or 'empty'}) — a seat may only act as a login the "
                "repository already trusts")

    reviewer_profile, fixer_profile = seat_profile(loop, "reviewer"), seat_profile(loop, "fixer")
    reviewer_login, fixer_login = seat_login(loop, "reviewer"), seat_login(loop, "fixer")
    if reviewer_profile and reviewer_profile == fixer_profile:
        raise ConfigError(f"{where}: reviewer and fixer both run as profile {reviewer_profile!r} — "
                          "one seat cannot review its own work")
    if same_profile_home(reviewer_profile, fixer_profile):
        raise ConfigError(f"{where}: reviewer and fixer use the same profile home "
                          f"({reviewer_profile!r}, {fixer_profile!r}) — one seat cannot review "
                          "its own work")
    if reviewer_login and reviewer_login.lower() == fixer_login.lower():
        raise ConfigError(f"{where}: reviewer and fixer both act as {reviewer_login!r} — the two "
                          "seats must be different accounts")

    adjudicator = loop.get("adjudicator") or {}
    # The adjudicator is judged against the seats whenever a seat *moves* too: pushing the reviewer
    # onto the profile that rules on it is the same mistake from the other direction, and the
    # effective loop is what runs.
    if adjudicator and roles & {"reviewer", "fixer", "adjudicator"}:
        adj_profile = str(adjudicator.get("profile") or "").strip() or "default"
        if "adjudicator" in roles and not adjudicator.get("route"):
            raise ConfigError(f"{where}: adjudicator.profile is set but adjudicator.route is not — "
                              "a profile alone cannot wake anyone")
        if "adjudicator" in roles and not profile_exists(adj_profile):
            raise ConfigError(f"{where}: no Hermes profile named {adj_profile!r} for the "
                              f"adjudicator seat (looked in {profiles_root()})")
        if adj_profile in (reviewer_profile, fixer_profile):
            raise ConfigError(f"{where}: the adjudicator runs as profile {adj_profile!r}, the same "
                              "as a seat it is meant to rule on")
        for other_profile in (reviewer_profile, fixer_profile):
            if same_profile_home(adj_profile, other_profile):
                raise ConfigError(f"{where}: the adjudicator and {other_profile!r} use the same "
                                  "profile home — a seat cannot rule on its own work")

    verify_credentials(loop, roles=roles)


_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def webhook_host(value: str | None, *, required: bool = False) -> str:
    """Accept only a gateway origin that urllib.request will send to that authority."""
    host = str(value or "").strip()
    if not host:
        if required:
            raise ConfigError("webhook host required to install routes/hooks: pass --host https://your-gateway.example "
                              "or set your own webhook host in the plugin settings")
        return ""
    try:
        # urlsplit alone accepts empty userinfo/ports and percent-escaped host delimiters.
        # urllib.request then unquotes parts of the authority, so inspect the raw syntax too.
        parsed = urlsplit(host)
        authority = parsed.netloc
        valid = (parsed.scheme in ("http", "https")
                 and host[:len(parsed.scheme) + 3].lower() == parsed.scheme + "://"
                 and bool(authority) and parsed.path in ("", "/")
                 and "?" not in host and "#" not in host
                 and not any(ord(char) < 33 or ord(char) == 127 for char in host)
                 and not any(char in authority for char in "@%\\"))
        if valid:
            if authority.startswith("["):
                bracket = authority.find("]")
                ipaddress.IPv6Address(authority[1:bracket])
                suffix = authority[bracket + 1:]
            else:
                address, sep, port_text = authority.partition(":")
                suffix = sep + port_text
                if not address or len(address.rstrip(".")) > 253:
                    valid = False
                elif re.fullmatch(r"[0-9.]+", address):
                    ipaddress.IPv4Address(address)
                else:
                    labels = address.removesuffix(".").split(".")
                    valid = all(_DNS_LABEL.fullmatch(label) for label in labels)
            if suffix:
                valid = (valid and suffix.startswith(":") and bool(suffix[1:])
                         and suffix[1:].isascii() and suffix[1:].isdecimal()
                         and 1 <= int(suffix[1:]) <= 65535)
            valid = valid and parsed.hostname is not None
            if suffix and valid:
                valid = parsed.port is not None
        if valid:
            origin = host.removesuffix("/")
            request = Request(f"{origin}/webhooks/test")
            valid = (request.type == parsed.scheme and request.host == authority
                     and request.selector == "/webhooks/test")
    except (ValueError, IndexError):
        valid = False
    if not valid:
        raise ConfigError(f"invalid webhook host {value!r}: --host must be an http(s) gateway "
                          "origin without a path, query, or credentials")
    return host.removesuffix("/")


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def config_dir() -> pathlib.Path:
    override = os.environ.get("REVIEW_LOOP_CONFIG_DIR")
    return pathlib.Path(override).expanduser() if override else home() / "review-loops.d"


@contextlib.contextmanager
def push_policy_lock():
    """Serialize host CLI policy writes and a broker's final push attempt.

    Manual config edits outside this lock are not an authorization mechanism.
    """
    import fcntl
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.fixer-push-policy.lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _path(value: str) -> pathlib.Path:
    return pathlib.Path(str(value)).expanduser()


def dangerous_root(value: str) -> str:
    """Why a cleanup root is too broad to accept, or ``""`` when it is fine.

    Cleanup removes PR-named children of every root. ``/``, the home directory and anything
    above it hold the operator's own projects and dotfiles, which are never a loop's to delete,
    however they happen to be named.
    """
    path = _path(value).resolve()
    home_dir = pathlib.Path.home().resolve()
    if path == pathlib.Path(path.anchor):
        return "the filesystem root"
    if path == home_dir:
        return "the home directory"
    if path in home_dir.parents:
        return "an ancestor of the home directory"
    return ""


def normalize(raw: dict, source: pathlib.Path | None = None) -> dict:
    """Fill defaults, expand paths, and refuse anything that would make a run ambiguous."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{source or 'config'}: expected a JSON object")
    loop = {**DEFAULTS, **raw}
    where = f"{source or loop.get('id', '<inline>')}"
    if type(loop["unattended_fixer_push"]) is not bool:
        raise ConfigError(f"{where}: 'unattended_fixer_push' must be a JSON boolean; "
                          "only explicit true authorizes unattended fixer pushes")

    repo = str(loop.get("repo") or "").strip()
    if repo.count("/") != 1:
        raise ConfigError(f"{where}: 'repo' must be 'owner/name', got {repo!r}")

    loop["id"] = str(loop.get("id") or repo.split("/")[-1]).strip()
    loop["repo"] = repo.lower()
    loop["fixers"] = [str(x).lower() for x in loop.get("fixers") or []]
    loop["reviewers"] = [str(x).lower() for x in loop.get("reviewers") or []]
    if not loop["fixers"]:
        raise ConfigError(f"{where}: 'fixers' must list at least one GitHub login")
    if not loop["reviewers"]:
        raise ConfigError(f"{where}: 'reviewers' must list at least one GitHub login")

    raw_seats = loop.get("seats") if isinstance(loop.get("seats"), dict) else {}
    seats = {}
    for seat in SEAT_KEYS:
        seat_cfg = dict((loop.get("seats") or {}).get(seat) or {})
        if not seat_cfg.get("route"):
            raise ConfigError(f"{where}: seats.{seat}.route is required")
        if not seat_cfg.get("profile"):
            raise ConfigError(f"{where}: seats.{seat}.profile is required")
        seat_cfg.setdefault("login", loop["reviewers"][0] if seat == "reviewer"
                            else (loop["fixers"][0] if len(loop["fixers"]) == 1 else ""))
        seat_cfg.setdefault("agent", seat_cfg["profile"].capitalize())
        seats[seat] = seat_cfg
    loop["seats"] = seats

    loop["reviewer_seat"] = str(loop.get("reviewer_seat")
                               or seats["reviewer"].get("login") or "").lower()
    if not loop["reviewer_seat"]:
        raise ConfigError(f"{where}: 'reviewer_seat' (or seats.reviewer.login) is required")

    adj = {k: v for k, v in (loop.get("adjudicator") or {}).items()}
    if adj and not adj.get("route") and set(adj) <= {"profile"}:
        # A route-less adjudicator carries no configuration at all — it is exactly what the
        # normalized form writes. Dropping it is what lets our own output be read back; anything
        # with more than a profile and still no route is a real mistake and raises below.
        adj = {}
    if adj and not adj.get("route"):
        raise ConfigError(f"{where}: adjudicator.route is required when adjudicator is set")
    if adj:
        adj.setdefault("profile", "default")
    loop["adjudicator"] = adj

    loop["observer"] = normalize_observer(loop.get("observer"))

    loop["cap"] = int(loop["cap"])
    if loop["cap"] < 2:
        raise ConfigError(f"{where}: 'cap' is the number of verdicts allowed; must be >= 2")

    loop["tokens"] = {k: str(v) for k, v in (loop.get("tokens") or {}).items()}
    loop["read_token"] = str(loop.get("read_token") or (next(iter(loop["tokens"]), "")))
    adjudicator_seat = _adjudicator_seat(raw_seats.get("adjudicator"), loop, where)
    if adjudicator_seat:
        seats["adjudicator"] = adjudicator_seat
    loop["roots"] = [str(p) for p in (loop.get("roots") or [])]
    for root in loop["roots"]:
        reason = dangerous_root(root)
        if reason:
            raise ConfigError(f"{where}: root {root!r} is {reason}; cleanup deletes PR-named "
                              f"children of every root, so a root must be a dedicated directory")

    # `or 1` here would swallow a literal 0 into "serialized", which is the worst kind of silent
    # correction: the operator asked for something invalid and got a loop that looks configured.
    raw_capacity = loop.get("concurrency")
    if raw_capacity is None or raw_capacity == "":
        raw_capacity = 1
    loop["concurrency"] = int(raw_capacity)
    if loop["concurrency"] < 1:
        raise ConfigError(f"{where}: 'concurrency' must be >= 1 (1 = serialized)")

    for seat in SEAT_KEYS:
        raw = seats[seat].get("concurrency")
        if raw is None or raw == "":
            continue
        seats[seat]["concurrency"] = int(raw)
        if seats[seat]["concurrency"] < 1:
            raise ConfigError(f"{where}: seats.{seat}.concurrency must be >= 1 (1 = serialized)")

    if not loop.get("clone"):
        # Above one run at once, isolation is not a preference: without a clone to isolate from,
        # two runs would share whatever checkout they find, which is the wrong-verdict bug. Check
        # the effective value per seat, so a seat-level 2 is caught even when the loop default is 1.
        for seat in SEAT_KEYS:
            if seat_concurrency(loop, seat) > 1:
                raise ConfigError(
                    f"{where}: seats.{seat}.concurrency > 1 requires 'clone' "
                    f"(runs must be isolated)")

    loop["host"] = webhook_host(loop.get("host"))
    if not loop.get("state_dir"):
        loop["state_dir"] = str(home() / "state" / "review-loops" / loop["id"])
    return loop


def load_file(path: pathlib.Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return normalize(raw, path)


def load_id(loop_id: str) -> dict:
    if not loop_id or pathlib.Path(loop_id).name != loop_id or loop_id in ('.', '..'):
        raise ConfigError('loop ID must name one config file')
    path = config_dir() / f"{loop_id}.json"
    if path.is_symlink():
        raise ConfigError(f"{path}: symlinked loop configs are not allowed")
    if not path.is_file():
        raise ConfigError(f"no loop config named {loop_id!r} in {config_dir()}")
    loop = load_file(path)
    if loop['id'] != loop_id:
        raise ConfigError(f"{path}: loop ID does not match filename")
    return loop


def all_loops() -> list[dict]:
    """Every readable loop, sorted by id. A broken file raises — silently skipping a repo
    would mean silently not driving it, which is the failure mode this tool exists to kill."""
    directory = config_dir()
    if not directory.exists():
        return []
    return [load_id(p.stem) for p in sorted(directory.glob("*.json"))]


def by_repo(full_name: str) -> dict | None:
    want = str(full_name or "").lower()
    matches = [loop for loop in all_loops() if loop['repo'] == want]
    if len(matches) > 1:
        raise ConfigError(f"duplicate loop configs for {want}: unattended writes denied")
    return matches[0] if matches else None


def artifacts_dir(loop: dict, number: int) -> pathlib.Path:
    return _path(loop["state_dir"]) / "artifacts" / str(number)


def clone_path(loop: dict) -> pathlib.Path | None:
    return _path(loop["clone"]) if loop.get("clone") else None
