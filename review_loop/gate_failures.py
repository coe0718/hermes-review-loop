"""A gate that cannot finish must never look like a gate that chose silence (issue #75).

What the Hermes gateway does with a route script (``gateway/platforms/webhook_filters.py``,
``run_route_script``; ``gateway/platforms/webhook.py``, ``_handle_webhook``):

* it runs ``<python> <script>`` synchronously inside the request handler, with the webhook
  payload as JSON on stdin (no headers: the ``X-GitHub-Delivery`` id never reaches the script),
  ``cwd`` = the script's directory and the gateway's scrubbed subprocess environment;
* the timeout is platform-wide ``script_timeout_seconds`` (default **30s**); on expiry the
  child is killed, so a gate cannot record its own overrun after the fact;
* non-zero exit, a timeout, empty stdout and ``[SILENT]`` are all the same answer: HTTP
  **200** ``{"status": "ignored", "reason": "script"}``. No script outcome can produce a
  non-2xx, and GitHub does not automatically redeliver failed deliveries anyway — so
  "exit non-zero and let GitHub retry" is not available to the plugin. The loop has to
  remember and retry on its own.

So every gate runs under :func:`run`, which

1. budgets the gate well under the gateway's timeout (``REVIEW_LOOP_GATE_BUDGET_S``, default
   20s): every GitHub call is clipped to what is left, a spent budget raises
   :class:`gh.GateBudgetExceeded`, and a ``SIGALRM`` backstop a few seconds later interrupts
   anything else that hangs (a lock, a subprocess);
2. records a crash, a timeout, or a silence that followed a failed GitHub read ("incomplete")
   durably in the loop's ``gate-failures.json`` — event fingerprint, repo, PR, head, action,
   gate, exception type and message, a bounded traceback — with the payload kept beside it so
   the watchdog can re-drive the event;
3. marks the fingerprint's entry resolved the next time the same event completes cleanly
   (a watchdog re-drive, or a manual redelivery from GitHub's UI).

A crash or timeout exits non-zero (the gateway logs ``code=`` with our stderr line); an
incomplete silence still prints ``[SILENT]`` and exits 0 — the answer to GitHub is the same
200 either way, and the ledger is what makes them different.

Re-driving is safe for the reviewer and fixer gates only: they print nothing but ``[SILENT]``
(runs are enqueued in the host run ledger, whose repo/PR/head/seat/turn index dedups a second
delivery) and re-read the live PR before acting, so a stale event silences. The adjudicator
gate's stdout *is* its dispatch, which a watchdog re-run could not hand to the gateway; its
failures are alerted, never re-driven.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import pathlib
import re
import signal
import sys
import time
import traceback
from collections.abc import Callable

from . import config, gh
from .util import iso_at, log

LEDGER = "gate-failures.json"
PAYLOADS = "gate-failures"
DEFAULT_BUDGET_S = 20.0
BACKSTOP_S = 3.0             # SIGALRM this long after the soft budget; 20 + 3 < the gateway's 30
MAX_REDRIVES = 3
MAX_ENTRIES = 200
MAX_PAYLOAD_BYTES = 1 << 20
RESOLVED_RETENTION_S = 7 * 86400
REDRIVABLE = frozenset({"gate_reviewer", "gate_fixer"})
REDRIVE_ENV = "REVIEW_LOOP_GATE_REDRIVE"
# A GitHub answer about the resource, not a failure to read it.
_FACT_ERRORS = re.compile(r"^HTTP (404|410|422)\b")
_SECRETS = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def _bounded(text: str, limit: int) -> str:
    text = _SECRETS.sub("[redacted]", str(text))
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def budget_s() -> float:
    try:
        value = float(os.environ.get("REVIEW_LOOP_GATE_BUDGET_S", DEFAULT_BUDGET_S))
    except ValueError:
        value = DEFAULT_BUDGET_S
    return value if 0 < value < 600 else DEFAULT_BUDGET_S


def fingerprint(gate: str, raw: str) -> str:
    """The event's identity. The gateway never hands a script the delivery id, but a GitHub
    redelivery is byte-for-byte the same payload, so its canonical form names the event."""
    try:
        canon = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except Exception:
        canon = raw
    return hashlib.sha256(f"{gate}\0{canon}".encode()).hexdigest()[:16]


def describe(payload) -> dict:
    """repo / PR / head / action out of whatever arrived — never raising on a bad shape."""
    def get(obj, *keys):
        for key in keys:
            obj = obj.get(key) if isinstance(obj, dict) else None
        return obj
    if not isinstance(payload, dict):
        return {"repo": "", "pr": None, "head": "", "action": ""}
    number = get(payload, "pull_request", "number") or payload.get("number")
    head = get(payload, "pull_request", "head", "sha") or get(payload, "review", "commit_id")
    repo = get(payload, "repository", "full_name")
    return {"repo": str(repo).lower() if isinstance(repo, str) else "",
            "pr": number if type(number) is int else None,
            "head": head if isinstance(head, str) else "",
            "action": str(payload.get("action") or "")[:40]}


# -- the ledger -----------------------------------------------------------------------------


class Ledger:
    """``gate-failures.json`` plus one payload file per entry, under one flock."""

    def __init__(self, directory: pathlib.Path):
        self.dir = pathlib.Path(directory)
        self.path = self.dir / LEDGER
        self.payload_dir = self.dir / PAYLOADS

    @contextlib.contextmanager
    def _locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "gate-failures.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def entries(self) -> dict:
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
        except Exception:
            # An unreadable ledger is itself a failure worth an alert, not "no failures".
            return {"_unreadable": {"gate": "?", "kind": "ledger", "resolved": False,
                                    "error": f"{self.path} is unreadable", "attempts": 1}}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        from .state import _atomic_write
        _atomic_write(self.path, data)

    def payload(self, key: str) -> str | None:
        try:
            return (self.payload_dir / f"{key}.json").read_text()
        except OSError:
            return None

    def record(self, key: str, entry: dict, raw: str) -> dict:
        now = time.time()
        with self._locked():
            data = self.entries()
            data.pop("_unreadable", None)
            prior = data.get(key) if isinstance(data.get(key), dict) else {}
            merged = {**prior, **entry, "id": key, "last_at": now,
                      "first_at": prior.get("first_at") or now,
                      "attempts": int(prior.get("attempts") or 0) + 1,
                      "redrives": int(prior.get("redrives") or 0), "resolved": False}
            merged.pop("resolution", None)
            data[key] = merged
            # Bounded: drop the oldest resolved entries first, then the oldest of all.
            if len(data) > MAX_ENTRIES:
                order = sorted(data, key=lambda k: (not data[k].get("resolved"),
                                                    data[k].get("last_at") or 0))
                for stale in order[:len(data) - MAX_ENTRIES]:
                    data.pop(stale, None)
                    with contextlib.suppress(OSError):
                        (self.payload_dir / f"{stale}.json").unlink()
            if raw and len(raw.encode()) <= MAX_PAYLOAD_BYTES:
                self.payload_dir.mkdir(parents=True, exist_ok=True)
                target = self.payload_dir / f"{key}.json"
                fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as out:
                    out.write(raw)
                merged["payload_kept"] = True
            else:
                merged["payload_kept"] = False
            self._save(data)
            return merged

    def update(self, key: str, fields: dict) -> None:
        with self._locked():
            data = self.entries()
            if isinstance(data.get(key), dict):
                data[key] = {**data[key], **fields}
                self._save(data)

    def resolve(self, key: str, how: str) -> bool:
        with self._locked():
            data = self.entries()
            entry = data.get(key)
            if not isinstance(entry, dict) or entry.get("resolved"):
                return False
            data[key] = {**entry, "resolved": True, "resolution": how, "resolved_at": time.time()}
            now = time.time()
            for stale in [k for k, v in data.items() if isinstance(v, dict) and v.get("resolved")
                          and now - (v.get("resolved_at") or 0) > RESOLVED_RETENTION_S]:
                data.pop(stale, None)
            self._save(data)
        with contextlib.suppress(OSError):
            (self.payload_dir / f"{key}.json").unlink()
        return True

    def open_for(self, number: int) -> list[dict]:
        return [e for e in self.entries().values()
                if isinstance(e, dict) and not e.get("resolved") and e.get("pr") == number]


def loop_ledger(loop: dict) -> Ledger:
    return Ledger(pathlib.Path(str(loop["state_dir"])).expanduser())


def open_for(loop: dict, number: int) -> list[dict]:
    """Unresolved failures recorded for one PR; nothing when the loop has no state directory."""
    return loop_ledger(loop).open_for(number) if loop.get("state_dir") else []


def fallback_ledger() -> Ledger:
    """For a failure before any loop could be named (a malformed payload, a broken config)."""
    return Ledger(config.home() / "state" / "review-loop-gate-failures")


def _loop_for(payload) -> dict | None:
    repo = describe(payload)["repo"]
    if not repo:
        return None
    try:
        return config.by_repo(repo)
    except Exception:
        return None


def _ledgers_for(payload) -> list[Ledger]:
    loop = _loop_for(payload)
    return ([loop_ledger(loop)] if loop else []) + [fallback_ledger()]


# -- the guard ------------------------------------------------------------------------------


def _backstop(_signum, _frame):
    raise gh.GateBudgetExceeded("gate exceeded its hard time budget (not in a GitHub read)")


def run(gate: str, main: Callable[[], None]) -> None:
    """Run one gate's ``main`` with a budget, and never let a failure pass as ``[SILENT]``."""
    raw = sys.stdin.read()
    sys.stdin = io.StringIO(raw)
    budget = budget_s()
    started = time.monotonic()
    gh.begin_gate(started + budget)
    alarm = hasattr(signal, "setitimer")
    if alarm:
        signal.signal(signal.SIGALRM, _backstop)
        signal.setitimer(signal.ITIMER_REAL, budget + BACKSTOP_S)
    kind, exc, code = "", None, 0
    try:
        try:
            main()
        finally:
            if alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        if code != 0:
            kind, exc = "crash", stop
    except gh.GateBudgetExceeded as over:
        kind, exc = "timeout", over
    except Exception as crash:  # noqa: BLE001 - the whole point: nothing escapes unrecorded
        kind, exc = "crash", crash
    elapsed = time.monotonic() - started
    read_errors = gh.end_gate()
    failed_reads = [e for e in read_errors if not _FACT_ERRORS.match(e[2])]
    if not kind and failed_reads:
        kind = "incomplete"
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    key = fingerprint(gate, raw)
    redrive = os.environ.get(REDRIVE_ENV, "")
    if not kind:
        how = (f"re-driven by the watchdog; completed at {iso_at(time.time())}" if redrive
               else f"event completed on a later delivery at {iso_at(time.time())}")
        for ledger in _ledgers_for(payload):
            try:
                ledger.resolve(key, how)
            except Exception as err:  # noqa: BLE001
                log(f"gate-failure ledger resolve failed: {type(err).__name__}: {err}")
        raise SystemExit(code)

    facts = describe(payload)
    if kind == "incomplete":
        error_type = "GitHubReadFailed"
        message = "; ".join(f"{m} {p}: {e}" for m, p, e in failed_reads[:4])
        trace = ""
    else:
        error_type = type(exc).__name__
        message = str(exc) if not isinstance(exc, SystemExit) else f"exit code {code}"
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    entry = {"gate": gate, "kind": kind, **facts, "error_type": error_type,
             "error": _bounded(message, 500), "traceback": _bounded(trace, 4000),
             "elapsed_s": round(elapsed, 2), "budget_s": budget,
             "redrivable": gate in REDRIVABLE}
    recorded = ""
    for ledger in _ledgers_for(payload):
        try:
            ledger.record(key, entry, raw)
            recorded = str(ledger.path)
            break
        except Exception as err:  # noqa: BLE001 - fall through to the next ledger
            log(f"gate-failure ledger write failed ({ledger.path}): {type(err).__name__}: {err}")
    where = f"#{facts['pr']}" if facts["pr"] else "an unnamed PR"
    log(f"GATE FAILURE ({kind}) {gate} {facts['repo'] or '?'} {where}: {error_type}: "
        f"{_bounded(message, 200)} — recorded {key} in {recorded or 'NOWHERE (ledger unwritable)'}"
        f"; the watchdog alerts and re-drives it")
    if kind == "incomplete":
        raise SystemExit(0)   # the gate already printed its [SILENT]; the ledger tells them apart
    raise SystemExit(3 if kind == "timeout" else 2)


# -- the watchdog's side --------------------------------------------------------------------


def redrive(ledger: Ledger, key: str, gate: str, scripts_dir: pathlib.Path) -> str:
    """Re-run the gate on the stored payload, the way the gateway runs it; return an outcome."""
    import subprocess
    raw = ledger.payload(key)
    if raw is None:
        return "payload not kept — cannot re-drive"
    script = scripts_dir / f"{gate}.py"
    if not script.is_file():
        return f"{script.name} not found — cannot re-drive"
    ledger.update(key, {"redrives": int((ledger.entries().get(key) or {}).get("redrives") or 0) + 1,
                        "last_redrive_at": time.time()})
    try:
        proc = subprocess.run([sys.executable, str(script)], input=raw, capture_output=True,
                              text=True, cwd=str(scripts_dir), timeout=60,
                              env={**os.environ, REDRIVE_ENV: key})
    except subprocess.TimeoutExpired:
        return "re-drive timed out"
    entry = ledger.entries().get(key) or {}
    if entry.get("resolved"):
        return "re-driven — completed" + (f" ({proc.stdout.strip()[:40]})" if proc.stdout.strip() else "")
    return f"re-driven — failed again (exit {proc.returncode})"


def sweep(ledger: Ledger, header: str, scripts_dir: pathlib.Path, *, cooldown_s: float,
          may_redrive: bool = True) -> list[str]:
    """Alert on unresolved gate failures (once per new failure, then per cooldown) and re-drive
    the re-drivable ones up to ``MAX_REDRIVES`` times."""
    lines: list[str] = []
    now = time.time()
    for key, entry in sorted(ledger.entries().items(), key=lambda kv: (kv[1] or {}).get("last_at") or 0):
        if not isinstance(entry, dict) or entry.get("resolved"):
            continue
        fresh = (entry.get("alerted_attempts") != entry.get("attempts")
                 or now - float(entry.get("alerted_at") or 0) > cooldown_s)
        outcome = ""
        if (may_redrive and entry.get("redrivable") and entry.get("payload_kept")
                and int(entry.get("redrives") or 0) < MAX_REDRIVES):
            outcome = redrive(ledger, key, str(entry.get("gate")), scripts_dir)
            entry = ledger.entries().get(key) or entry
            fresh = True
        elif not entry.get("redrivable"):
            outcome = "not re-driven (its output is a dispatch) — re-deliver it from GitHub by hand"
        elif int(entry.get("redrives") or 0) >= MAX_REDRIVES:
            outcome = f"gave up after {MAX_REDRIVES} re-drives — needs you"
        else:
            outcome = "not re-driven this sweep"
        if not fresh:
            continue
        pr = f"#{entry['pr']}" if entry.get("pr") else "no PR"
        head = str(entry.get("head") or "")[:7] or "?"
        lines.append(f"⚠️ Review loop {header} — gate failure {key}: {entry.get('gate')} "
                     f"{entry.get('kind')} on {pr} @ {head} ({entry.get('action') or '?'}), "
                     f"{entry.get('attempts')} attempt(s): {entry.get('error_type')}: "
                     f"{_bounded(entry.get('error') or '', 160)} — {outcome}")
        if not entry.get("resolved"):
            ledger.update(key, {"alerted_at": now, "alerted_attempts": entry.get("attempts")})
    return lines
