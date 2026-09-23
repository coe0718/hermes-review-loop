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
    "grace_min": 25,          # how long a quiet head is allowed to sit before the watchdog speaks
    "marker_grace_min": 60,
    "cooldown_h": 6,
    "ttl_min": 45,            # seat lock lifetime: past this a crashed run has lost its seat
    "inflight_ttl_min": 10,
    "host": "https://hooks.coemedia.us",
}

SEAT_KEYS = ("reviewer", "fixer")


class ConfigError(Exception):
    """A loop file that cannot be trusted to drive a run."""


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

    adj = dict(loop.get("adjudicator") or {})
    if adj and not adj.get("route"):
        raise ConfigError(f"{where}: adjudicator.route is required when adjudicator is set")
    adj.setdefault("profile", "default")
    loop["adjudicator"] = adj

    loop["cap"] = int(loop["cap"])
    if loop["cap"] < 2:
        raise ConfigError(f"{where}: 'cap' is the number of verdicts allowed; must be >= 2")

    loop["tokens"] = {k: str(v) for k, v in (loop.get("tokens") or {}).items()}
    loop["read_token"] = str(loop.get("read_token") or (next(iter(loop["tokens"]), "")))
    loop["roots"] = [str(p) for p in (loop.get("roots") or [])]
    loop["host"] = str(loop.get("host") or DEFAULTS["host"]).rstrip("/")
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
