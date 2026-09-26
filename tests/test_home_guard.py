"""The suites never read or write the operator's real ~/.hermes, and an escape fails loudly.

``_home_guard`` gives every test process a temp HOME/HERMES_HOME and arms the plugin's tripwire;
these tests prove each half. The escape probes use a *fake* real home (``REVIEW_LOOP_TEST_REAL_HOME``
names a temp directory) so proving the tripwire never touches the operator's actual home.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import os
import pathlib
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
from review_loop import cli, config, state  # noqa: E402
from review_loop.run_supervisor import Supervisor  # noqa: E402

LEAKING = "test_boundary.BoundaryTests.test_gate_blocks_before_workspace_or_gateway_payload"


def first_import(path: pathlib.Path) -> str:
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.Import):
            return node.names[0].name
        if isinstance(node, ast.ImportFrom):
            return node.module or ""
    return ""


class HomeGuard(unittest.TestCase):
    def test_every_suite_imports_the_guard_first(self):
        files = [*sorted(TESTS.glob("test_*.py")), TESTS / "run_tests.py", TESTS / "harness/fixture.py"]
        late = [f.name for f in files if first_import(f) != "_home_guard"]
        self.assertEqual(late, [], "import _home_guard before anything else in these suites")

    def test_guard_is_armed_with_a_temp_home(self):
        real = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.assertEqual(os.environ[config.TEST_HOME_GUARD_ENV], "1")
        self.assertNotEqual(pathlib.Path(os.environ["HOME"]).resolve(), real.resolve())
        self.assertEqual(pathlib.Path.home(), _home_guard.TEST_HOME)
        self.assertIn(_home_guard.TEST_HOME, config.home().resolve().parents)

    def test_tripwire_refuses_the_passwd_home(self):
        # Refused lexically, before anything under the real home is even stat'ed.
        real = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
        for hermes_home in (real, real / ".hermes", real / ".hermes/profiles/x"):
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                with self.assertRaises(config.RealHomeError):
                    config.home()
        with self.assertRaises(config.RealHomeError):
            Supervisor(real / ".hermes/state/review-loop-runs.sqlite")
        with self.assertRaises(config.RealHomeError):
            state.LoopState({"state_dir": str(real / ".hermes/state/review-loops/x")})
        with mock.patch.dict(os.environ, {config.TEST_HOME_GUARD_ENV: ""}):
            self.assertEqual(config.home(), pathlib.Path(os.environ["HERMES_HOME"]))

    def _run_leaking_test(self, fake_home: pathlib.Path, *, escaped: bool):
        env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "HOME")}
        env.update(HOME=str(fake_home), REVIEW_LOOP_TEST_REAL_HOME=str(fake_home))
        if escaped:
            # What an escape looks like: the guard believes it already ran, but HOME is the
            # "real" home and HERMES_HOME is unset, so config.home() defaults into it.
            env.update({config.TEST_HOME_GUARD_ENV: "1", "REVIEW_LOOP_TEST_USER_HOME": str(fake_home)})
        else:
            env.pop(config.TEST_HOME_GUARD_ENV, None)
            env.pop("REVIEW_LOOP_TEST_USER_HOME", None)
        return subprocess.run([sys.executable, "-m", "unittest", "-v", LEAKING], cwd=TESTS, env=env,
                              text=True, capture_output=True, timeout=120)

    def test_escaping_test_hits_the_tripwire_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as fake:
            fake_home = pathlib.Path(fake)
            result = self._run_leaking_test(fake_home, escaped=True)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn("RealHomeError", result.stderr)
            self.assertEqual(list(fake_home.rglob("*")), [])

    def test_previously_leaking_gate_test_uses_a_temp_home(self):
        with tempfile.TemporaryDirectory() as fake:
            fake_home = pathlib.Path(fake)
            result = self._run_leaking_test(fake_home, escaped=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(fake_home.rglob("*")), [])



class HermesShim(unittest.TestCase):
    """No guarded test may run the operator's real ``hermes``; plugin code gets the shim."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def test_hermes_on_path_is_the_shim_and_it_refuses(self):
        shim = _home_guard.SHIM_DIR / "hermes"
        self.assertEqual(shutil.which("hermes"), str(shim))
        self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(_home_guard.SHIM_DIR))
        result = subprocess.run([str(shim), "update"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, _home_guard.SHIM_EXIT)
        self.assertIn(_home_guard.BLOCKED, result.stderr)

    def test_plugin_cron_create_hits_the_shim_not_the_real_binary(self):
        # cli._install_schedule is the plugin's one PATH lookup of `hermes` (`init --schedule`).
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes")}):
            os.environ.pop(_home_guard.FAKE_HERMES_ENV, None)
            lines = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertIn("cron create failed", lines[0])
        self.assertIn(_home_guard.BLOCKED, lines[0])

    def test_plugin_cron_create_runs_an_explicit_fake(self):
        record = self.root / "argv"
        fake = self.root / "fake-hermes"
        fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n')
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes"),
                                          _home_guard.FAKE_HERMES_ENV: str(fake)}):
            lines = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertIn("scheduled the watchdog", lines[0])
        self.assertEqual(record.read_text().split("\n")[:5],
                         ["cron", "create", "15m", "--name", cli.watchdog_job_name({"id": "widgets"})])

    def test_a_fake_naming_the_real_binary_is_refused(self):
        # A stand-in "real hermes" that would leave a marker if the shim ever ran it.
        marker = self.root / "ran"
        real = self.root / "real-hermes"
        real.write_text(f"#!/bin/sh\ntouch {marker}\n")
        real.chmod(0o755)
        shim = self.root / "hermes"
        shim.write_text(_home_guard._SHIM.format(real=str(real), blocked=_home_guard.BLOCKED,
                                                 code=_home_guard.SHIM_EXIT))
        shim.chmod(0o755)
        result = subprocess.run([str(shim)], capture_output=True, text=True, timeout=10,
                                env={**os.environ, _home_guard.FAKE_HERMES_ENV: str(real)})
        self.assertEqual(result.returncode, _home_guard.SHIM_EXIT)
        self.assertIn("names the real binary", result.stderr)
        self.assertFalse(marker.exists())

    def test_plugin_refuses_a_hermes_inside_the_real_home(self):
        fake_home = self.root / "home"
        with mock.patch.dict(os.environ, {config.TEST_REAL_HOME_ENV: str(fake_home)}):
            with self.assertRaises(config.RealHomeError):
                config.guard_real_hermes(str(fake_home / ".local/bin/hermes"))
            self.assertEqual(config.guard_real_hermes(str(_home_guard.SHIM_DIR / "hermes")),
                             str(_home_guard.SHIM_DIR / "hermes"))


if __name__ == "__main__":
    unittest.main()
