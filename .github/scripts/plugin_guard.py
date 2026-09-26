#!/usr/bin/env python3
"""Run Hermes's install-time plugin scanner on this checkout, the way `hermes plugins install` does.

Usage: plugin_guard.py HERMES_TOOLS_ROOT PLUGIN_DIR

HERMES_TOOLS_ROOT is a hermes-agent checkout (only its ``tools/`` package is needed). A
``dangerous`` verdict fails: Hermes blocks that install and ``--force`` cannot override it.
``caution`` passes with every finding printed, because ``install --force`` accepts it, and the
findings stay visible for review rather than disappearing behind a green check.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    hermes, plugin = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(hermes))
    from tools.plugin_guard import (PLUGIN_SCANNER_VERSION, format_scan_report, scan_plugin,
                                    should_allow_plugin_install)

    result = scan_plugin(plugin, source="https://github.com/coe0718/hermes-review-loop.git")
    counts = Counter(f.severity for f in result.findings)
    allowed, reason = should_allow_plugin_install(result, force=True)
    print(format_scan_report(result))
    summary = (f"Hermes plugin guard ({PLUGIN_SCANNER_VERSION}): **{result.verdict}** — "
               + ", ".join(f"{counts[s]} {s}" for s in ("critical", "high", "medium", "low") if counts[s])
               + f"\n\n`install --force`: {'allowed' if allowed else 'BLOCKED'} ({reason})\n")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(summary)
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
