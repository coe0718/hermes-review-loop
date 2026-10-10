"""``hermes dk show``: every setting a loop has, its effective value and where it came from (#555).

Read-only. One row per setting, built from the *same* accessors the loop runs with (``config``'s
``seat_concurrency``, ``review_only_cap``, ``max_steps``, ...) so this never states a value the
loop would not use. Where the value came from is read off the raw loop file:

* ``loop file`` - the file sets it;
* ``derived``   - the file does not, and the value follows another setting (see ``note``);
* ``default``   - the file does not, and the built-in default applies.

The plugin settings form is *not* a source for a running loop: it supplies defaults for a new loop
and `apply --loop` pushes it. A form value the loop does not hold is shown as a ``note``, never as
the effective value, so `show` cannot say a setting is on when the loop would not act on it.

Token values are never read: ``tokens`` rows show the file paths from the loop file.
"""
from __future__ import annotations

import json

from . import config, observer

# Every key `show` covers. The tests compare these against SETTINGS_SCHEMA, DEFAULTS, TRIAGE_KEYS
# and the observer keys, so a key added to the loop without a row here turns a test red.
OBSERVER_KEYS = ("route", "profile", "deliver", "mute", "events", "digest_min", "urgent_route",
                 "urgent_profile", "urgent_deliver")
SEAT_FILE_KEYS = ("route", "profile", "login", "agent", "concurrency", "turn_budget_s",
                  "daily_turns", "max_steps")

_UNSET = "(unset)"


def _dig(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _in_file(raw: dict, path) -> bool:
    value = _dig(raw, path)
    return value is not None and value != "" and value != [] and value != {}


def _plain(value):
    """A JSON-safe copy: sets and tuples become sorted lists."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class _Row:
    def __init__(self, name, path, value, meaning, *, schema=False, derived="", off="",
                 on=None, note=""):
        self.name, self.path, self.value, self.meaning = name, tuple(path), value, meaning
        self.schema, self.derived, self.off, self.on, self.note = schema, derived, off, on, note


def _rows(loop: dict) -> list[_Row]:
    seats = loop.get("seats") or {}
    triage = loop.get("triage") or {}
    adj = loop.get("adjudicator") or {}
    obs = loop.get("observer") or {}
    S = config.SETTINGS_SCHEMA
    rows: list[_Row] = []

    def schema(key, path, value, *, derived="", off="", on=None, note=""):
        rows.append(_Row(key, path, value, S[key]["description"], schema=True, derived=derived,
                         off=off, on=on, note=note))

    def plain(name, path, value, meaning, **kw):
        rows.append(_Row(name, path, value, meaning, **kw))

    cap_loop_c = loop.get("concurrency")
    for seat in config.SEAT_KEYS:
        own = (seats.get(seat) or {}).get("concurrency") not in (None, "")
        schema(f"{seat}_concurrency", ("seats", seat, "concurrency"),
               config.seat_concurrency(loop, seat),
               derived="" if own else f"the loop's concurrency ({cap_loop_c})")
    schema("cap", ("cap",), loop["cap"])
    schema("clone", ("clone",), loop.get("clone") or _UNSET,
           note="" if loop.get("clone") else "needed above 1 run at once")
    schema("base", ("base",), loop["base"])
    for key in ("grace_min", "ttl_min", "inflight_ttl_min", "turn_budget_s"):
        schema(key, (key,), loop[key])
    schema("host", ("host",), loop.get("host") or _UNSET)
    for role in config.ROUTE_ROLES:
        profile_key = config.PROFILE_SETTINGS[role]
        if role == "adjudicator":
            schema(profile_key, ("adjudicator", "profile"),
                   config.seat_profile(loop, role) if adj.get("route") else _UNSET,
                   on=bool(adj.get("route")),
                   off="" if adj.get("route") else
                   "no adjudicator route: the review cap only writes a marker")
        else:
            schema(profile_key, ("seats", role, "profile"), config.seat_profile(loop, role))
    for role in config.SEAT_KEYS:
        derived = ""
        if not _in_file({"seats": seats}, ("seats", role, "login")) and role == "reviewer":
            derived = "the first of reviewers"
        schema(f"{role}_login", ("seats", role, "login"), config.seat_login(loop, role) or _UNSET,
               derived=derived)
    schema("adjudicator_login", ("seats", "adjudicator", "login"),
           config.adjudicator_login(loop) or _UNSET)
    tokens = loop.get("tokens") or {}
    for role, key in config.TOKEN_FILE_SETTINGS.items():
        login = config.seat_login(loop, role) if role != "adjudicator" \
            else config.adjudicator_login(loop)
        match = {str(k).lower(): v for k, v in tokens.items()}.get(login.lower()) if login else None
        schema(key, ("tokens",), match or _UNSET,
               note="a path, never the token" if match else "")
    for role in config.SEAT_KEYS:
        schema(f"{role}_max_steps", ("seats", role, "max_steps"), config.max_steps(loop, role))
    schema("auto_fix_labels", ("triage", "auto_fix_labels"),
           sorted(config.auto_fix_labels(loop) or [str(x) for x in triage.get("auto_fix_labels") or []]),
           off="no auto-offer: issues reach the fixer only when a maintainer applies the fix label",
           on=bool(triage.get("auto_fix_labels")) and config.issue_fixes_enabled(loop),
           note=("set but inert: needs triage.fix_label and unattended fixer pushes"
                 if triage.get("auto_fix_labels") and not config.issue_fixes_enabled(loop) else ""))
    schema("auto_fix_daily", ("triage", "auto_fix_daily"), config.auto_fix_daily(loop))
    schema("fix_daily_turns", ("triage", "fix_daily_turns"),
           config.seat_daily_turns(loop, "issue_fixer"))
    schema("review_only", ("review_only",), list(loop.get("review_only") or []),
           on=bool(loop.get("review_only")),
           off="every author's PR may be fixed by the fixer")
    schema("review_only_cap", ("review_only_cap",), config.review_only_cap(loop),
           derived=f"cap ({loop['cap']})")
    schema("ci_fix_cap", ("ci_fix_cap",), config.ci_fix_cap(loop))
    schema("review_only_daily", ("review_only_daily",), loop.get("review_only_daily") or "no cap")
    for key, off in (
            ("review_only_update", "a conflicting review-only PR gets a notice, no merge push"),
            ("review_after_ci", "a review starts at once, with whatever CI has finished"),
            ("fix_ci", "red CI on fixer PRs waits for a review")):
        schema(key, (key,), bool(loop[key]), on=bool(loop[key]), off=off)
    if loop["fix_ci"] and not config.unattended_fixer_push_enabled(loop):
        rows[-1].note = "on, but inert: needs unattended fixer pushes (hermes dk fixer-push)"
    schema("attribution", ("attribution",), bool(loop["attribution"]), on=bool(loop["attribution"]),
           off="nothing the loop posts is signed")
    schema("required_checks", ("required_checks",), list(loop.get("required_checks") or []),
           on=bool(loop.get("required_checks")), off="every check gates an approval")
    schema("human_paths", ("human_paths",), list(loop.get("human_paths") or []),
           on=bool(loop.get("human_paths")), off="no path is reserved for a person")
    schema("fixer_check", ("fixer_check",), loop.get("fixer_check") or _UNSET,
           on=bool(loop.get("fixer_check")), off="the fixer runs no always-run check")

    # Loop-file-only keys (DEFAULTS and identity), then nested blocks.
    plain("id", ("id",), loop["id"], "the loop's id (its file name)")
    plain("repo", ("repo",), loop["repo"], "the repository the loop watches")
    plain("state_dir", ("state_dir",), loop.get("state_dir"), "where the loop keeps its state",
          derived="the default state directory for the id")
    plain("fixers", ("fixers",), list(loop["fixers"]), "logins allowed to author fixer pushes")
    plain("reviewers", ("reviewers",), list(loop["reviewers"]), "logins allowed to review")
    plain("reviewer_seat", ("reviewer_seat",), loop.get("reviewer_seat"),
          "the login the reviewer seat acts as", derived="seats.reviewer.login")
    plain("read_token", ("read_token",), loop.get("read_token"),
          "login the gates read GitHub as (the reader; never a seat)")
    plain("tokens", ("tokens",), {k: str(v) for k, v in tokens.items()},
          "login -> token file path (paths only; token values are never read)")
    plain("skill", ("skill",), loop.get("skill") or _UNSET, "plugin skill the seats load")
    plain("roots", ("roots",), list(loop.get("roots") or []),
          "directories cleanup prunes PR-named children of")
    plain("concurrency", ("concurrency",), loop["concurrency"],
          "runs at once per working seat (a seat's own value wins)")
    plain("marker_grace_min", ("marker_grace_min",), loop["marker_grace_min"],
          "minutes a marker may sit before the watchdog speaks")
    plain("cooldown_h", ("cooldown_h",), loop["cooldown_h"], "hours between repeated notices")
    plain("unattended_fixer_push", ("unattended_fixer_push",),
          bool(loop["unattended_fixer_push"]),
          "per-repository consent to unattended fixer pushes; never inherited from the form",
          on=bool(loop["unattended_fixer_push"]),
          off="a changes-requested verdict waits for you (hermes dk fixer-push --enable)")
    for seat in ("reviewer", "fixer", "adjudicator", "triage"):
        for key in SEAT_FILE_KEYS:
            if seat == "adjudicator" and key not in ("login", "concurrency", "turn_budget_s",
                                                     "daily_turns", "max_steps"):
                continue
            if seat == "triage" and key in ("route", "profile", "login", "agent"):
                continue
            if key in ("login", "profile") and seat != "triage":
                continue                       # schema rows above cover these
            if key == "max_steps" and seat in ("reviewer", "fixer"):
                continue
            if key == "concurrency" and seat in ("reviewer", "fixer"):
                continue
            value = (seats.get(seat) or {}).get(key)
            if key == "turn_budget_s" and seat in ("reviewer", "fixer"):
                plain(f"seats.{seat}.turn_budget_s", ("seats", seat, key),
                      config.turn_budget(loop, seat), "wall-clock seconds one turn may run",
                      derived="the loop's turn_budget_s")
                continue
            if key == "daily_turns":
                value = config.seat_daily_turns(loop, seat) or "no cap"
            plain(f"seats.{seat}.{key}", ("seats", seat, key),
                  value if value not in (None, "") else _UNSET, f"seat {seat}: {key}")
    plain("adjudicator.route", ("adjudicator", "route"), adj.get("route") or _UNSET,
          "route that wakes the adjudicator when the verdict budget is spent",
          on=bool(adj.get("route")), off="the review cap only writes a marker")
    plain("triage.enabled", ("triage", "route"), config.triage_enabled(loop),
          "issue triage (hermes dk triage)", on=config.triage_enabled(loop),
          off="new issues are not labelled")
    for key in sorted(config.TRIAGE_KEYS - {"auto_fix_labels", "auto_fix_daily", "fix_daily_turns"}):
        value = triage.get(key)
        plain(f"triage.{key}", ("triage", key), value if value not in (None, "", []) else _UNSET,
              f"triage: {key}")
    events = obs.get("events")
    hidden = sorted(set(observer.EVENTS) - set(events)) if events else []
    for key in OBSERVER_KEYS:
        value = obs.get(key)
        row = _Row(f"observer.{key}", ("observer", key),
                   value if value not in (None, "") else _UNSET, f"observer feed: {key}")
        if key == "events":
            row.value = list(events) if events else "all events"
            row.meaning = "transitions the feed delivers (blank = all)"
            row.note = ("hides: " + ", ".join(hidden)) if hidden else ""
        if key == "route":
            row.on, row.off = bool(obs.get("route")), "no observer feed: transitions are silent"
        rows.append(row)
    return rows


def collect(loop: dict, raw: dict, form: dict | None = None) -> list[dict]:
    """Every row as a JSON-safe dict: name, value, source, meaning, state, note."""
    out = []
    for row in _rows(loop):
        source = ("loop file" if _in_file(raw, row.path)
                  else "derived" if row.derived else "default")
        if row.on is None:
            on = row.value is True
        else:
            on = row.on
        state = "on" if on else ("off" if row.off else "")
        note = row.note
        if source == "derived":
            note = "; ".join(x for x in (f"from {row.derived}", note) if x)
        if row.schema and form and config._form_value(form, row.name) is not None:
            held = config.settings_defaults(form)[row.name]
            if row.name not in config.TOKEN_FILE_SETTINGS.values() and held != row.value \
                    and str(held) != str(row.value):
                note = "; ".join(x for x in (
                    note, f"settings form holds {held!r}, not applied (hermes dk apply --loop "
                          f"{loop['id']})") if x)
        if state == "off":
            note = "; ".join(x for x in (f"off — {row.off}", note) if x)
        out.append({"name": row.name, "value": _plain(row.value), "source": source,
                    "meaning": row.meaning, "state": state, "note": note})
    return out


def render(loop_id: str, rows: list[dict]) -> str:
    """The text `show` prints: on first, then off-but-matters, then everything else."""
    groups = (("on", [r for r in rows if r["state"] == "on"]),
              ("off — would matter", [r for r in rows if r["state"] == "off"]),
              ("other settings", [r for r in rows if r["state"] == ""]))
    width = max(len(r["name"]) for r in rows)
    lines = [f"[{loop_id}] every setting, its effective value and where it came from"]
    for title, items in groups:
        if not items:
            continue
        lines.append(f"\n{title}")
        for r in items:
            value = json.dumps(r["value"]) if not isinstance(r["value"], str) else r["value"]
            lines.append(f"  {r['name']:<{width}}  {value}  [{r['source']}]  {r['meaning']}")
            if r["note"]:
                lines.append(f"  {'':<{width}}  -> {r['note']}")
    return "\n".join(lines)
