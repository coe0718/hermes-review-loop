"""The plugin's own record of the routes it installed, and self-heal against it (issue #1).

The gateway's ``webhook_subscriptions.json`` is shared with Hermes's CLI and dashboard, which
rewrite it without the plugin's lock. A native write racing a plugin write can erase or rewrite
this loop's routes — and a route whose secret changed is one GitHub's hook can no longer
authenticate against. Until upstream makes every writer share one locked transaction, the
plugin keeps a private copy of what it wrote and puts it back.

* **Intent record** — ``<state_dir>/route-intent.json``, mode 0600, written atomically. Every
  plugin command that installs, rebinds or removes a route updates it as the last step of that
  command's transaction, so the record is always what the operator last asked the plugin for.
  It holds each route's full entry, secret included: it is the source of truth for what the
  loop expects, and the only place the original secret survives an erasure.
* **Self-heal** — every armed watchdog sweep (and ``doctor --repair``) compares the live
  registry with the record for this loop's own route names. A missing route, or one whose
  ``WATCHED`` fields differ, is restored under the registry lock with the *same* secret, and the
  operator is told what was restored. A name another writer now uses for a non-review-loop
  script is reported, never overwritten; names not in this loop's config are never read.
* **Adoption** — a loop installed before this record existed has none. A live route that
  provably is this loop's (its role's exact gate script and prompt, and a secret) is adopted
  into the record by the watchdog, so existing installs are protected without a reinstall.

A malformed registry is never "restored" over: the registry write fails closed, the heal alerts.
A malformed record is not trusted either: nothing is healed and the operator is told.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import tempfile

from . import config, prompts, routes

INTENT_FILE = "route-intent.json"
VERSION = 1

# The fields that decide whether a route still wakes the right seat and still authenticates.
WATCHED = ("secret", "script", "prompt", "events", "profile", "deliver_only", "host")

GATE_SCRIPT = {"reviewer": "gate_reviewer.py", "fixer": "gate_fixer.py",
               "adjudicator": "gate_adjudicator.py", "observer": "observe.py"}
ROUTE_PROMPT = {"reviewer": prompts.REVIEWER, "fixer": prompts.FIXER,
                "adjudicator": prompts.ADJUDICATOR, "observer": prompts.OBSERVER}
PLUGIN_SCRIPTS = frozenset(GATE_SCRIPT.values()).union(*config.LEGACY_GATE_SCRIPTS.values())


class IntentError(ValueError):
    """The intent record exists but cannot be trusted."""


def routes_of(loop: dict) -> dict:
    """role → route name, from the loop's own config (the one spelling ``cli`` uses too)."""
    names: dict = {}
    for role in ("reviewer", "fixer"):
        name = str(((loop.get("seats") or {}).get(role) or {}).get("route") or "")
        if name:
            names[role] = name
    adjudicator = str((loop.get("adjudicator") or {}).get("route") or "")
    if adjudicator:
        names["adjudicator"] = adjudicator
    observer_route = str((loop.get("observer") or {}).get("route") or "")
    if observer_route:
        names["observer"] = observer_route
    return names


def path(loop: dict) -> pathlib.Path:
    return pathlib.Path(str(loop["state_dir"])).expanduser() / INTENT_FILE


def owned(entry: dict) -> bool:
    """Is a live entry still one of this plugin's gates (so restoring it takes nothing over)?"""
    return entry.get("script") in PLUGIN_SCRIPTS


@contextlib.contextmanager
def _lock(loop: dict):
    target = path(loop)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target.with_name(INTENT_FILE + ".lock"),
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield target
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load(loop: dict) -> dict | None:
    """name → entry, or ``None`` when no record exists. Raises ``IntentError`` if malformed."""
    if not loop.get("state_dir"):
        return None
    target = path(loop)
    try:
        raw = target.read_text()
    except (FileNotFoundError, NotADirectoryError):
        return None                     # no record (a state_dir that is a file is doctor's to flag)
    except OSError as exc:
        raise IntentError(f"{target} is unreadable ({exc})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise IntentError(f"{target} is not readable JSON ({exc})") from exc
    entries = data.get("routes") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not all(
            isinstance(entry, dict) and entry.get("secret") for entry in entries.values()):
        raise IntentError(f"{target} must hold {{\"routes\": {{name: entry with a secret}}}}")
    return entries


def _write(target: pathlib.Path, loop: dict, entries: dict) -> None:
    body = json.dumps({"version": VERSION, "loop": loop.get("id"), "repo": loop.get("repo"),
                       "routes": entries}, indent=2, sort_keys=True).encode()
    fd, temp = tempfile.mkstemp(prefix=f".{INTENT_FILE}.", dir=target.parent)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, target)
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def record(loop: dict, entries: dict, *, replace: bool = False) -> None:
    """Remember routes the plugin just wrote (name → entry). ``replace`` drops every other name.

    A malformed existing record is replaced only with ``replace``: a merge into bytes nobody can
    read would silently forget the routes it held.
    """
    for name, entry in entries.items():
        if not isinstance(entry, dict) or not entry.get("secret"):
            raise IntentError(f"route {name!r} has no readable entry to record")
    with _lock(loop) as target:
        current = {} if replace else (load(loop) or {})
        current.update({name: dict(entry) for name, entry in entries.items()})
        _write(target, loop, current)


def forget(loop: dict, names) -> None:
    """The operator removed or renamed these routes through the plugin: stop healing them."""
    names = set(names)
    with _lock(loop) as target:
        try:
            current = load(loop)
        except IntentError:
            return          # an unreadable record heals nothing anyway; leave it for the operator
        if not current or not names & set(current):
            return
        _write(target, loop, {k: v for k, v in current.items() if k not in names})


def record_live(loop: dict, names, *, replace: bool = False) -> None:
    """Record the registry's current entries for ``names`` (read back after a plugin write)."""
    registry = routes.all_routes()
    record(loop, {name: registry[name] for name in names
                  if isinstance(registry.get(name), dict)}, replace=replace)


def _adoptable(loop: dict, registry: dict, known: dict) -> dict:
    """This loop's live routes with no record yet whose gate and prompt prove they are ours."""
    found = {}
    for role, name in routes_of(loop).items():
        entry = registry.get(name)
        if name in known or not isinstance(entry, dict):
            continue
        if (entry.get("script") == GATE_SCRIPT[role] and entry.get("prompt") == ROUTE_PROMPT[role]
                and entry.get("secret")):
            found[name] = entry
    return found


def drift(loop: dict, registry: dict, intent: dict) -> dict:
    """name → differing watched fields (``["missing"]`` when erased), for this loop's names only."""
    mine = set(routes_of(loop).values())
    out = {}
    for name, want in intent.items():
        if name not in mine:
            continue
        live = registry.get(name)
        if live is None:
            out[name] = ["missing"]
        elif not isinstance(live, dict):
            out[name] = ["entry"]
        else:
            diff = [key for key in WATCHED if live.get(key) != want.get(key)]
            if diff:
                out[name] = diff
    return out


def heal(loop: dict, *, adopt: bool = True) -> list[str]:
    """Restore this loop's routes from the record. Returns alert lines ([] when all is well).

    Never raises for a registry or record problem: the watchdog must keep sweeping. Every
    failure is an alert line instead, and nothing is written over bytes that cannot be parsed.
    """
    if not loop.get("state_dir"):
        return []                       # nowhere to keep a record: nothing to heal from
    label = f"[{loop.get('id', '?')}] {loop.get('repo', '')}".rstrip()
    try:
        intent = load(loop)
    except IntentError as exc:
        return [f"⚠️ Review loop {label} — route intent record unreadable, routes NOT self-healed: "
                f"{exc}. Re-run `hermes review-loop apply` or restore the file."]
    subs = routes.subs_path()
    try:
        raw = subs.read_bytes()
    except FileNotFoundError:
        registry = {}
    except OSError as exc:
        return [f"⚠️ Review loop {label} — route registry unreadable, routes NOT self-healed: {exc}"]
    else:
        try:
            registry = json.loads(raw)
        except ValueError as exc:
            registry = exc
        if not isinstance(registry, dict):
            return [f"⚠️ Review loop {label} — route registry {subs} is malformed; it was NOT "
                    "overwritten and no route was restored. Repair it by hand (the plugin's "
                    f"copy of this loop's routes is in {path(loop)})."]
    lines = []
    intent = dict(intent or {})
    if adopt:
        found = _adoptable(loop, registry, intent)
        if found:
            try:
                record(loop, found)
                intent.update(found)
            except (OSError, IntentError) as exc:
                lines.append(f"⚠️ Review loop {label} — could not record route intent: {exc}")
    expected = {name: entry for name, entry in intent.items()
                if name in set(routes_of(loop).values())}
    if not expected or not drift(loop, registry, expected):
        return lines
    try:
        restored, conflicts = routes.heal_entries(expected, WATCHED, owned)
    except (OSError, ValueError) as exc:
        return lines + [f"⚠️ Review loop {label} — route registry changed by another writer and "
                        f"could NOT be restored (fail closed): {exc}"]
    if restored:
        lines.append(f"🔧 Review loop {label} — restored {len(restored)} route(s) another "
                     "registry writer erased or changed (same secret, so GitHub's hook still "
                     "authenticates):")
        for name, fields in sorted(restored.items()):
            what = "was missing" if fields == ["missing"] else "had changed " + ", ".join(fields)
            lines.append(f"  {name}: {what}")
    for name, reason in sorted(conflicts.items()):
        lines.append(f"⚠️ Review loop {label} — route {name} NOT restored: {reason}. "
                     "Pick another route name or remove the other entry.")
    return lines
