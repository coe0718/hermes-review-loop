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
"""

from __future__ import annotations

import json
import os
import pathlib
from urllib.parse import urlsplit

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
}


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
    return {**loop_raw, "cap": d["cap"], "clone": clone, "base": d["base"],
            "host": host, "grace_min": d["grace_min"], "ttl_min": d["ttl_min"],
            "inflight_ttl_min": d["inflight_ttl_min"], "seats": seats}


DEFAULTS: dict = {
    "base": "main",
    "cap": 3,
    "fixers": [],
    "reviewers": [],
    "reviewer_seat": "",
    "seats": {},
    "adjudicator": {},
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
}

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
    if value is None or value == "":
        value = loop.get("concurrency", 1)
    if value is None or value == "":
        value = 1
    return int(value)


class ConfigError(Exception):
    """A loop file that cannot be trusted to drive a run."""

def webhook_host(value: str | None, *, required: bool = False) -> str:
    """Accept only a gateway origin, never a relative URL or another route prefix."""
    host = str(value or "").strip().rstrip("/")
    if not host:
        if required:
            raise ConfigError("webhook host required to install routes/hooks: pass --host https://your-gateway.example "
                              "or set your own webhook host in the plugin settings")
        return ""
    try:
        parsed = urlsplit(host)
        valid = (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and not parsed.username and not parsed.password
                 and not parsed.path and "?" not in host and "#" not in host
                 and not any(char.isspace() for char in host))
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"invalid webhook host {value!r}: --host must be an http(s) gateway "
                          "origin without a path, query, or credentials")
    return host


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def config_dir() -> pathlib.Path:
    override = os.environ.get("REVIEW_LOOP_CONFIG_DIR")
    return pathlib.Path(override).expanduser() if override else home() / "review-loops.d"


def _path(value: str) -> pathlib.Path:
    return pathlib.Path(str(value)).expanduser()


def normalize(raw: dict, source: pathlib.Path | None = None) -> dict:
    """Fill defaults, expand paths, and refuse anything that would make a run ambiguous."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{source or 'config'}: expected a JSON object")
    loop = {**DEFAULTS, **raw}
    where = f"{source or loop.get('id', '<inline>')}"

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

    loop["cap"] = int(loop["cap"])
    if loop["cap"] < 2:
        raise ConfigError(f"{where}: 'cap' is the number of verdicts allowed; must be >= 2")

    loop["tokens"] = {k: str(v) for k, v in (loop.get("tokens") or {}).items()}
    loop["read_token"] = str(loop.get("read_token") or (next(iter(loop["tokens"]), "")))
    loop["roots"] = [str(p) for p in (loop.get("roots") or [])]

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
    path = config_dir() / f"{loop_id}.json"
    if not path.exists():
        raise ConfigError(f"no loop config named {loop_id!r} in {config_dir()}")
    return load_file(path)


def all_loops() -> list[dict]:
    """Every readable loop, sorted by id. A broken file raises — silently skipping a repo
    would mean silently not driving it, which is the failure mode this tool exists to kill."""
    directory = config_dir()
    if not directory.exists():
        return []
    return [load_file(p) for p in sorted(directory.glob("*.json"))]


def by_repo(full_name: str) -> dict | None:
    want = str(full_name or "").lower()
    for loop in all_loops():
        if loop["repo"] == want:
            return loop
    return None


def artifacts_dir(loop: dict, number: int) -> pathlib.Path:
    return _path(loop["state_dir"]) / "artifacts" / str(number)


def clone_path(loop: dict) -> pathlib.Path | None:
    return _path(loop["clone"]) if loop.get("clone") else None
