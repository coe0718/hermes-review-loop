"""Issue #32 boundary: a seat's provider credential stays on the host, and only with its own seat.

* ``run_turn`` gets the seat's key only for the host inference proxy; the sandbox HOME names the
  seat's model but points at the local bridge with a dummy key, and no file staged for the
  sandbox contains the key;
* a real bubblewrap probe cannot read a seat profile's ``.env``, ``auth.json`` or ``config.yaml``;
* ``selftest`` resolves every seat, runs one completion per distinct resolution with that
  resolution's own key, and asks the sandbox probe about every seat profile's credential files.

No real credentials, Hermes, network or ~/.hermes.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from review_loop import broker_ipc, contained, seat_model, trusted_turn  # noqa: E402
from review_loop.seat_model import SeatInference  # noqa: E402
from test_selftest import SelftestBase  # noqa: E402

HEAD = "a" * 40
REVIEWER_KEY = "SEAT-REVIEWER-KEY-7a7a7a7a"
FIXER_KEY = "SEAT-FIXER-KEY-8b8b8b8b"


class TurnStaging(unittest.TestCase):
    def test_sandbox_home_names_the_seat_model_but_holds_no_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ("venv", "runtime", "rust"):
                (root / name).mkdir()
            seen, staged = {}, {}

            class Inference:
                def __init__(self, directory, upstream, key, *, model, quota, **kw):
                    seen.update(upstream=upstream, key=key, model=model, **kw)
                    self.directory = directory

                def __enter__(self):
                    self.directory.mkdir()
                    (self.directory / "model.sock").touch()
                    return self

                def __exit__(self, *a):
                    return False

            def stage(_loop, **kw):
                kw["sandbox_root"].mkdir()
                return kw["sandbox_root"]

            def run(**kw):
                seen["argv"] = contained.command(**{k: v for k, v in kw.items() if k != "timeout"})
                for base in (kw["home"], kw["code"], kw["client_code"], kw["checkout"],
                             kw["query"].parent):
                    for path in pathlib.Path(base).rglob("*"):
                        if path.is_file():
                            staged[str(path)] = path.read_bytes()
                return subprocess.CompletedProcess([], 0, "", "")

            loop = {"repo": "acme/widgets", "state_dir": str(root)}
            scope = broker_ipc.RunScope("acme/widgets", 7, HEAD, "reviewer", "fix-7", "rid",
                                        str(root / "db"))
            with mock.patch.object(trusted_turn, "_safe_code_snapshot",
                                   side_effect=lambda src, dst: dst.mkdir()), \
                 mock.patch.object(trusted_turn.trusted_fetch, "stage", side_effect=stage), \
                 mock.patch.object(trusted_turn.inference_proxy, "InferenceCapability", Inference), \
                 mock.patch.object(contained.Path, "is_socket", return_value=True), \
                 mock.patch.object(contained, "run", side_effect=run), \
                 self.assertRaises(trusted_turn.TurnDenied):   # no broker write: expected
                trusted_turn.run_turn(loop, scope, source=root, venv=root / "venv",
                                      runtime=root / "runtime", rust=root / "rust",
                                      upstream="https://openrouter.test/api/v1/chat/completions",
                                      key=REVIEWER_KEY, model="vendor/reviewer-model",
                                      prompt="REVIEW", timeout=5, work_root=root / "work")
            self.assertEqual((seen["key"], seen["model"]), (REVIEWER_KEY, "vendor/reviewer-model"))
            homes = [text for path, text in staged.items() if path.endswith("/home/config.yaml")]
            self.assertEqual(len(homes), 1)
            home = homes[0].decode()
            self.assertIn('default: "vendor/reviewer-model"', home)
            self.assertIn("base_url: http://127.0.0.1:18761/v1", home)
            self.assertIn("api_key: sandbox-dummy-not-a-credential", home)
            self.assertNotIn("openrouter.test", home)
            for path, data in staged.items():
                self.assertNotIn(REVIEWER_KEY.encode(), data, path)
            self.assertNotIn(REVIEWER_KEY, json.dumps(seen["argv"]))
            self.assertIn("vendor/reviewer-model", seen["argv"])


class ProviderPathUpstream(unittest.TestCase):
    """OpenRouter-style upstreams live at /api/v1/chat/completions; the sandbox path stays fixed."""

    def test_host_chosen_upstream_path_and_its_limits(self):
        import http.server
        import threading
        from review_loop import inference_proxy
        seen = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers["Content-Length"]))
                data = b'{"choices": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                url = f"http://127.0.0.1:{server.server_port}/api/v1/chat/completions"
                with inference_proxy.InferenceCapability(pathlib.Path(tmp) / "c", url, REVIEWER_KEY,
                                                         model="m", quota=2) as cap:
                    for path, status in (("/api/v1/chat/completions", 400),
                                         (inference_proxy.PATH, 200)):
                        conn = inference_proxy._UnixHTTP(str(cap.socket_path))
                        conn.request("POST", path, body=b"{}")
                        self.assertEqual(conn.getresponse().status, status)
                        conn.close()
                for bad in ("https://h/v1/completions", "https://h/v1/../chat/completions",
                            "https://h//chat/completions", "https://h/v1/chat/completions?x=1"):
                    with self.subTest(bad=bad), self.assertRaises(ValueError):
                        inference_proxy._NoRedirectConnection(bad)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(seen, [("/api/v1/chat/completions", "Bearer " + REVIEWER_KEY)])


def _bwrap_works() -> bool:
    if not shutil.which("bwrap") or not os.path.exists("/usr/bin/python3"):
        return False
    try:
        return subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr",
                               "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
                               "--ro-bind-try", "/lib64", "/lib64", "--", "/usr/bin/true"],
                              capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


@unittest.skipUnless(_bwrap_works(), "unprivileged bubblewrap unavailable")
class SandboxProbe(unittest.TestCase):
    def test_seat_profile_credentials_are_not_readable_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            hermes = root / "hermes"
            paths = []
            for name, key in (("rev", REVIEWER_KEY), ("fix", FIXER_KEY)):
                profile = hermes / "profiles" / name
                profile.mkdir(parents=True)
                for file, text in ((".env", f"OPENROUTER_API_KEY={key}\n"),
                                   ("auth.json", json.dumps({"credential_pool": {"x": [{"access_token": key}]}})),
                                   ("config.yaml", json.dumps({"custom_providers": [{"api_key": key}]}))):
                    (profile / file).write_text(text)
                    (profile / file).chmod(0o600)
                    paths.append(str(profile / file))
            loop = {"seats": {"reviewer": {"profile": "rev"}, "fixer": {"profile": "fix"}}}
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes)}):
                expected = seat_model.secret_paths(loop, {})
            self.assertTrue(set(paths) <= set(expected), set(paths) - set(expected))
            paths = expected
            venv, code, home, work, rust = (root / n for n in ("venv", "code", "home", "work", "rust"))
            for d in (code, home, work, rust, venv / "bin"):
                d.mkdir(parents=True)
            (venv / "bin" / "python").symlink_to("/usr/bin/python3")
            query = root / "query.txt"
            query.write_text("probe\n")
            probe = ("import json, os, subprocess, sys\n"
                     "readable = []\n"
                     "for p in json.loads(sys.argv[1]):\n"
                     "    try:\n"
                     "        open(p, 'rb').read(1); readable.append(p)\n"
                     "    except OSError: pass\n"
                     "found = subprocess.run(['grep', '-rl', '-e', sys.argv[2], '-e', sys.argv[3],\n"
                     "                        '/home/agent', '/opt', '/work'], capture_output=True, text=True).stdout\n"
                     "print(json.dumps({'readable': readable, 'env': [k for k, v in os.environ.items()\n"
                     "                  if sys.argv[2] in v or sys.argv[3] in v], 'found': found}))\n")
            result = contained.run(code=code, venv=venv, runtime=pathlib.Path("/usr"), home=home,
                                   checkout=work, rust=rust, query=query, timeout=60,
                                   entry=["/opt/venv/bin/python", "-c", probe, json.dumps(paths),
                                          REVIEWER_KEY, FIXER_KEY])
            self.assertEqual(result.returncode, 0, result.stderr)
            facts = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(facts, {"readable": [], "env": [], "found": ""})


class SelftestPerSeat(SelftestBase):
    def setUp(self):
        super().setUp()
        self.fx.loop["seats"]["reviewer"]["profile"] = "rev"
        self.fx.loop["seats"]["fixer"]["profile"] = "fix"
        self.fx.loop["adjudicator"] = {"route": "breach", "profile": "adj"}
        settings = {k: v for k, v in self.fx.settings.items()
                    if k in seat_model.HOST_KEYS}
        self.fx.runtime_file.write_text(json.dumps(settings))
        self.posts = []

    def model_post(self, endpoint, body, headers):
        key = headers.get("Authorization", "").removeprefix("Bearer ")
        self.posts.append((endpoint.url.hostname, json.loads(body)["model"], key))
        return (200, "application/json",
                json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode())

    @staticmethod
    def resolver(profile, seat, settings):
        table = {"rev": ("openrouter", "vendor/rev", "openrouter.test", REVIEWER_KEY),
                 "fix": ("custom:acme", "fix-model", "acme.test", FIXER_KEY),
                 "adj": ("openrouter", "vendor/rev", "openrouter.test", REVIEWER_KEY)}
        if profile not in table:
            raise seat_model.SeatModelError(f"profile {profile} does not exist")
        provider, model, host, key = table[profile]
        return SeatInference(seat, profile, "profile", provider, model,
                             f"https://{host}/v1/chat/completions", key)

    def test_one_completion_per_distinct_resolution_with_its_own_key(self):
        out_rc, text = self.run_selftest(resolver=self.resolver)
        self.assertEqual(out_rc, 0, text)
        self.assertRegex(text, r"✅ seat:reviewer\s+profile rev: openrouter / vendor/rev via openrouter.test")
        self.assertRegex(text, r"✅ seat:fixer\s+profile fix: custom:acme / fix-model via acme.test")
        self.assertRegex(text, r"✅ seat:adjudicator\s+profile adj:")
        self.assertIn("HTTP 200 — reviewer+adjudicator: profile rev", text)
        self.assertIn("HTTP 200 — fixer: profile fix", text)
        self.assertEqual(sorted(self.posts),
                         [("acme.test", "fix-model", FIXER_KEY),
                          ("openrouter.test", "vendor/rev", REVIEWER_KEY)])
        for key in (REVIEWER_KEY, FIXER_KEY):
            self.assertNotIn(key, text)
        profiles = self.fx.home / "profiles"
        for name in ("rev", "fix", "adj"):
            for file in (".env", "auth.json", "config.yaml"):
                self.assertIn(str(profiles / name / file), self.fx.sandbox_paths)

    def test_unresolvable_seat_fails_the_selftest_with_its_reason(self):
        self.fx.loop["seats"]["fixer"]["profile"] = "ghost"
        rc, text = self.run_selftest(resolver=self.resolver)
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ seat:fixer\s+profile ghost does not exist; the fixer turn would be held")
        self.assertEqual({post[2] for post in self.posts}, {REVIEWER_KEY})


if __name__ == "__main__":
    unittest.main()
