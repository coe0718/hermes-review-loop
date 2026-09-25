"""Route registry: the cross-process atomic-edit suite, run as a subprocess."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


def group_routes() -> None:
    section("routes — atomic owner-only cross-process registry edits")
    test = subprocess.run([sys.executable, str(ROOT / "tests" / "test_routes_atomic.py")],
                          capture_output=True, text=True)
    if test.returncode:
        print(test.stdout + test.stderr)
    check("route registry regression suite", test.returncode, 0)


GROUPS = {
    "routes": group_routes,
}
