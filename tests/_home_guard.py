"""Keep every test, and every process a test starts, out of the operator's real ``~/.hermes``.

Import this before anything from ``review_loop`` — every ``tests/test_*.py`` does so as its first
import (``test_home_guard.py`` enforces that), and ``run_tests.py`` does for the harness. On first
import in a process it:

* captures, for read-only use, the Rust toolchain (``RUSTUP_HOME``/``CARGO_HOME`` and
  ``USER_HOME`` below). The real-Hermes tests take their Hermes source only from an explicit
  ``HERMES_AGENT_SOURCE`` — a disposable checkout, never the live ``~/.hermes/hermes-agent``
  (``needs_real_hermes`` fails them loudly if it points there);
* points ``HOME`` and ``HERMES_HOME`` at a fresh temp directory and drops inherited overrides that
  could name real state, so ``config.home()``, ``Path.home()``, ``~`` and every subprocess that
  inherits the environment (gate scripts, run_supervisor workers, the watchdog) land there;
* puts a ``hermes`` shim first on PATH that refuses to run (see ``FAKE_HERMES_ENV``);
* arms the plugin's tripwire (``REVIEW_LOOP_TEST_HOME_GUARD``): while it is set, resolving the
  Hermes home, a ledger, a state dir or a cleanup root inside the real home's ``.hermes`` raises
  ``config.RealHomeError`` — so a test that escapes this guard fails instead of writing.

unittest's ``discover -s tests`` never imports ``tests/__init__.py`` (the start directory is the
top level, not a package), which is why this is an explicit first import rather than a package hook.
"""

from __future__ import annotations

import atexit
import functools
import os
import pathlib
import shutil
import tempfile
import unittest

GUARD_ENV = "REVIEW_LOOP_TEST_HOME_GUARD"
# Inherited settings that could point a test at real state; the fixtures set their own.
_DROP = ("REVIEW_LOOP_CONFIG_DIR", "REVIEW_LOOP_SUBS", "REVIEW_LOOP_TOKEN_FILE")

if os.environ.get(GUARD_ENV) == "1" and os.environ.get("REVIEW_LOOP_TEST_USER_HOME"):
    # Already guarded (a child of a guarded test): keep the parent's temp home.
    USER_HOME = pathlib.Path(os.environ["REVIEW_LOOP_TEST_USER_HOME"])
    TEST_HOME = pathlib.Path(os.environ["HOME"])
else:
    USER_HOME = pathlib.Path.home()
    for _var, _default in (("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo")):
        if not os.environ.get(_var) and (USER_HOME / _default).is_dir():
            os.environ[_var] = str(USER_HOME / _default)
    TEST_HOME = pathlib.Path(tempfile.mkdtemp(prefix="review-loop-test-home-")).resolve()
    atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)
    for _var in _DROP:
        os.environ.pop(_var, None)
    os.environ.update({"HOME": str(TEST_HOME), "HERMES_HOME": str(TEST_HOME / ".hermes"),
                       "REVIEW_LOOP_TEST_USER_HOME": str(USER_HOME), GUARD_ENV: "1"})

# No guarded test, nor any process it starts, may run the operator's real `hermes` CLI: it acts on
# the real install (a bare `hermes` once resumed an interrupted source update and rebuilt the real
# hermes-agent's UI builds). A shim goes FIRST on PATH and fails loudly, unless a test names its
# own fake in FAKE_HERMES_ENV — and even then never the real binary.
FAKE_HERMES_ENV = "REVIEW_LOOP_TEST_FAKE_HERMES"
BLOCKED = "real hermes blocked under test guard"
SHIM_EXIT = 97
_SHIM = """#!/bin/sh
real={real}
if [ -n "$REVIEW_LOOP_TEST_FAKE_HERMES" ]; then
  fake=$(readlink -f -- "$REVIEW_LOOP_TEST_FAKE_HERMES")
  if [ -n "$real" ] && [ "$fake" = "$(readlink -f -- "$real")" ]; then
    echo "{blocked}: REVIEW_LOOP_TEST_FAKE_HERMES names the real binary" >&2
    exit {code}
  fi
  exec "$REVIEW_LOOP_TEST_FAKE_HERMES" "$@"
fi
echo "{blocked}: set REVIEW_LOOP_TEST_FAKE_HERMES to a fake (tests/_home_guard.py)" >&2
exit {code}
"""
SHIM_DIR = pathlib.Path(os.environ.get("REVIEW_LOOP_TEST_SHIM_DIR") or TEST_HOME / ".review-loop-test-bin")
if not (SHIM_DIR / "hermes").exists():
    import shlex
    _real = shutil.which("hermes") or ""
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    (SHIM_DIR / "hermes").write_text(_SHIM.format(real=shlex.quote(_real), blocked=BLOCKED,
                                                  code=SHIM_EXIT))
    (SHIM_DIR / "hermes").chmod(0o755)
_path = os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join([str(SHIM_DIR), *(p for p in _path if p != str(SHIM_DIR))])
os.environ["REVIEW_LOOP_TEST_SHIM_DIR"] = str(SHIM_DIR)

# The Hermes source the opt-in real-Hermes tests run (in bwrap, by its venv's own `hermes`). Only
# ever an explicit HERMES_AGENT_SOURCE — there is no default, because the obvious default is the
# operator's live install, whose `hermes` acts on the real ~/.hermes.
HERMES_AGENT_SOURCE = (pathlib.Path(os.environ["HERMES_AGENT_SOURCE"])
                       if os.environ.get("HERMES_AGENT_SOURCE") else None)


def _protected_homes() -> list[pathlib.Path]:
    homes = [USER_HOME]
    try:
        import pwd
        homes.append(pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError):
        pass
    if os.environ.get("REVIEW_LOOP_TEST_REAL_HOME"):     # the plugin's test-only fake real home
        homes.append(pathlib.Path(os.environ["REVIEW_LOOP_TEST_REAL_HOME"]))
    return homes


def source_refusal(source: pathlib.Path | None = None) -> str:
    """Why the real-Hermes tests must not run against ``source``, or ``""``.

    Refused: a source at or inside a protected home's ``.hermes`` (the live install), lexically
    or once symlinks are resolved.
    """
    source = HERMES_AGENT_SOURCE if source is None else source
    if source is None:
        return ""
    forms = {pathlib.Path(os.path.normpath(source.absolute())), source.resolve()}
    for home in _protected_homes():
        for live in {pathlib.Path(os.path.normpath(home.absolute())) / ".hermes",
                     home.resolve() / ".hermes"}:
            if any(form == live or live in form.parents for form in forms):
                return (f"HERMES_AGENT_SOURCE={source} is inside the live Hermes install "
                        f"({live}); the real-Hermes tests refuse to run it. Point "
                        "HERMES_AGENT_SOURCE at a disposable hermes-agent checkout with its own "
                        "venv, outside ~/.hermes.")
    return ""


def needs_real_hermes(*prerequisites: bool, reason: str = "real-Hermes test prerequisites absent"):
    """Decorate an opt-in real-Hermes test (function or class).

    A HERMES_AGENT_SOURCE inside the live install fails the test loudly, whatever else is
    missing; no source or a missing prerequisite skips it, as before.
    """
    def decorate(target):
        refusal = source_refusal()
        if refusal:
            if isinstance(target, type):
                def set_up_class(cls):
                    raise AssertionError(refusal)
                target.setUpClass = classmethod(set_up_class)
                return target

            @functools.wraps(target)
            def refuse(*args, **kwargs):
                raise AssertionError(refusal)
            return refuse
        ready = (HERMES_AGENT_SOURCE is not None
                 and (HERMES_AGENT_SOURCE / "venv/bin/hermes").exists() and all(prerequisites))
        return unittest.skipUnless(ready, reason)(target)
    return decorate

RUST = (pathlib.Path(os.environ.get("RUSTUP_HOME") or USER_HOME / ".rustup")
        / "toolchains/stable-x86_64-unknown-linux-gnu")
