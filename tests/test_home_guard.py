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
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
from review_loop import config, state  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
