"""Small shared helpers. No third-party imports anywhere in this package."""

from __future__ import annotations

import sys
import time
from typing import NoReturn


def log(message: str, quiet: bool = False) -> None:
    """Diagnostics go to stderr always — a gate's stdout is a protocol, not a console."""
    if not quiet:
        print(f"[review-loop] {message}", file=sys.stderr)


def silence(reason: str = "") -> NoReturn:
    """The gate's "nothing to do" answer. The route adapter renders nothing for this.

    Typed ``NoReturn`` on purpose: every guard reads as "silence, *then* we know the event
    is ours", and the type checker needs to agree that the code after it is reachable only
    for events that passed.
    """
    if reason:
        log(reason)
    print("[SILENT]")
    raise SystemExit(0)


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} GB"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def epoch(iso: str | None) -> float:
    """GitHub timestamps are UTC ISO-8601. Parse as UTC, never as local time."""
    if not iso:
        return 0.0
    import calendar

    try:
        return float(calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        try:
            from datetime import datetime

            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0


def age_min(iso: str | None) -> float:
    t = epoch(iso)
    return (time.time() - t) / 60 if t else 0.0
