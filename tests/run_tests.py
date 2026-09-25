#!/usr/bin/env python3
"""The loop's proof: every gate branch, the watchdog's four stall shapes, and the cleanup rails.

Runs with a plain interpreter and no network — no ``gh``, no pytest, no GitHub:

* GitHub is a stub executable (``REVIEW_LOOP_GH_STUB``) answering from a JSON "world" file, so
  the tests can put a fixer, a verdict and a review request exactly where they want them;
* the webhook endpoint is a real local HTTP server, so watchdog wake paths still exercise
  signature validation; eligible gate runs return [SILENT] and fail closed without runtime;
* the isolated supervisor's SQLite ledger is tested with a trusted inert fixture command,
  never an ambient GitHub bypass or a credential-owning gateway agent;
* git is real: the cleanup tests build a throwaway clone with detached review worktrees and a
  branch worktree, because the difference between those two is the whole safety story.

    python3 tests/run_tests.py             # all of it
    python3 tests/run_tests.py watchdog    # one area (a module under tests/harness/)
    python3 tests/run_tests.py explain     # or one group inside an area
    python3 tests/run_tests.py --list      # the areas and their groups, in run order

The shared fixture (fake world, gh stub, webhook sink, temp HOME/state, loop helpers and the
``check`` framework) lives in ``tests/harness/fixture.py``; each area module registers its
groups in ``GROUPS``. Areas run in the order below, and groups within an area in their order.
Every group resets the fixture it needs, so each area (and each group) also runs alone.
"""

from __future__ import annotations

import sys
import types

from harness import cleanup, doctor, fixture, gates, observer, routes, seats, state, watchdog

AREAS = {"routes": routes, "gates": gates, "seats": seats, "watchdog": watchdog,
         "cleanup": cleanup, "doctor": doctor, "state": state, "observer": observer}
GROUPS = {name: func for module in AREAS.values() for name, func in module.GROUPS.items()}

# Suites such as test_reconciliation.py do ``import run_tests as t`` and use the fixture through
# it (``t.reset``, ``t.make_loop``, ``t.HOST = t.start_sink()``). Keep that namespace whole: every
# public fixture and area helper is reachable here, and assigning ``t.HOST`` reaches them all.
for _module in (fixture, *AREAS.values()):
    for _name, _value in vars(_module).items():
        if not _name.startswith("_") and _name != "GROUPS":
            globals().setdefault(_name, _value)


class _Harness(types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        if name == "HOST":
            fixture.set_host(value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Harness


def selected(argv: list[str]) -> list[str] | None:
    """Group names to run, in run order; ``None`` for an unknown name."""
    wanted: list[str] = []
    for arg in argv:
        if arg in AREAS:
            names = list(AREAS[arg].GROUPS)
        elif arg in GROUPS:
            names = [arg]
        else:
            return None
        wanted += [n for n in names if n not in wanted]
    return wanted


def main() -> int:
    argv = sys.argv[1:]
    if "--list" in argv:
        for area, module in AREAS.items():
            print(f"{area:10s} {' '.join(module.GROUPS)}")
        return 0
    wanted = selected(argv) if argv else list(GROUPS)
    if wanted is None:
        print(f"unknown area or group; areas: {', '.join(AREAS)} (see --list)", file=sys.stderr)
        return 2
    sink = fixture.start_sink()
    fixture.set_host(sink)
    fixture.DATA["host"] = sink
    fixture.os.environ.update(fixture.env())   # the config group reads fixtures in-process
    fixture.reset(prs={})                      # fixtures exist before any group runs
    for name in wanted:
        GROUPS[name]()
    results = fixture.results
    passed = sum(1 for ok, _ in results if ok)
    failed = [n for ok, n in results if not ok]
    print(f"\n{passed}/{len(results)} checks pass")
    if failed:
        print("failed:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
