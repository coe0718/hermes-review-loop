"""`hermes review-loop selftest`: checklist output, exit codes, redaction and the no-write broker.

GitHub, the model provider and bubblewrap are all mocked; nothing here needs a network.
"""
import argparse
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import (broker, broker_ipc, cli, contained, doctor, gh,  # noqa: E402
                         inference_proxy, review_receipt, selftest, trusted_fetch, trusted_turn)

HEAD = "c" * 40
SECRETS = {"reader": "READER-SECRET-0a1b2c3d", "reviewer": "ghp_" + "R" * 36,
           "fixer": "FIXER-SECRET-9z8y7x6w"}
MODEL_KEY = "MODEL-KEY-SECRET-5566"


class Fixture:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.home = root / "hermes"
        self.home.mkdir()
        tokens = {}
        for login, secret in SECRETS.items():
            path = root / f"{login}.pat"
            path.write_text(secret + "\n")
            path.chmod(0o600)
            tokens[login] = str(path)
        self.loop = {"id": "demo", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "state_dir": str(root / "state"), "tokens": tokens, "read_token": "reader",
                     "reviewer_seat": "reviewer", "fixers": ["fixer"], "reviewers": ["reviewer"],
                     "seats": {"reviewer": {"login": "reviewer"}, "fixer": {"login": "fixer"}}}
        source = root / "hermes-agent"
        (source / ".git").mkdir(parents=True)
        (source / "run_agent.py").write_text("")
        runtime = root / "python"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/python3").write_text("")
        venv = root / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to(runtime / "bin/python3")
        (venv / "bin/hermes").write_text("")
        rust = root / "rust"
        (rust / "bin").mkdir(parents=True)
        (rust / "bin/cargo").write_text("")
        key = root / "model.key"
        key.write_text(MODEL_KEY)
        key.chmod(0o600)
        self.settings = {"source": str(source), "venv": str(venv), "runtime": str(runtime),
                         "rust": str(rust), "key_file": str(key), "model": "tiny-model",
                         "upstream": "https://models.example/v1/chat/completions"}
        self.runtime_file = self.home / "review-loop-runtime.json"
        self.write_runtime()
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "fixer"},
                   "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": "acme/widgets"}},
                   "base": {"ref": "main", "sha": "d" * 40, "repo": {"full_name": "acme/widgets"}}}
        self.ids = {"reader": 1, "reviewer": 2, "fixer": 3}
        self.calls = []
        self.probe = {"readable": [], "network": False, "dns": False, "cargo": True,
                      "hermes": True, "env": []}
        self.sandbox_paths = []

    def write_runtime(self, **changes):
        self.runtime_file.write_text(json.dumps({**self.settings, **changes}))
        self.runtime_file.chmod(0o600)

    def fetch(self, loop, path, method="GET", body=None, login=None):
        self.calls.append((method, path, login))
        if method != "GET":
            raise AssertionError("selftest attempted a GitHub write")
        if path == "/user":
            return {"login": login, "id": self.ids[login]}, ""
        if path == "/repos/acme/widgets":
            return {"full_name": "acme/widgets"}, ""
        if path == "/repos/acme/widgets/pulls/7":
            return self.pr, ""
        if path.startswith("/repos/acme/widgets/pulls/7/reviews"):
            return [], ""
        return None, "HTTP 404"

    def contained_run(self, **kwargs):
        if "cargo metadata" in kwargs["entry"][-1]:     # step 5's build check: resolves
            return subprocess.CompletedProcess([], 0, "", "")
        self.sandbox_paths = json.loads(kwargs["entry"][-1])
        return subprocess.CompletedProcess([], 0, json.dumps(self.probe) + "\n", "")


def _ok(name):
    return doctor.Check(name, doctor.VERIFIED, "fine")


class SelftestBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.fx = Fixture(pathlib.Path(temp.name))
        env = {k: v for k, v in os.environ.items() if k != "REVIEW_LOOP_GH_STUB"}
        env["HERMES_HOME"] = str(self.fx.home)
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(gh, "fetch", side_effect=self.fx.fetch),
            mock.patch.object(selftest.shutil, "which", return_value="/usr/bin/bwrap"),
            mock.patch.object(selftest.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "", "")),
            mock.patch.object(trusted_turn, "_safe_code_snapshot",
                              side_effect=lambda source, dest: dest.mkdir()),
            mock.patch.object(contained, "run", side_effect=self.fx.contained_run),
            mock.patch.object(trusted_fetch, "stage", side_effect=self.stage),
            mock.patch.object(inference_proxy._NoRedirectConnection, "post", autospec=True,
                              side_effect=self.model_post),
            mock.patch.object(doctor, "check_shim", return_value=_ok("cron:shim")),
            mock.patch.object(doctor, "check_cron_job", return_value=_ok("cron:job")),
            mock.patch.object(doctor, "check_gateway", return_value=_ok("gateway")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.model_status = 200
        self.model_keys = []
        self.head_files = {}

    def stage(self, loop, **kw):
        """The exact-head export, faked: ``head_files`` is the PR head's tree."""
        repo = kw["sandbox_root"] / "repo"
        repo.mkdir(parents=True)
        for name, text in self.head_files.items():
            (repo / name).write_text(text)
        return repo

    def model_post(self, endpoint, body, headers):
        key = headers.get("Authorization", "").removeprefix("Bearer ")
        self.model_keys.append(key)
        payload = json.loads(body)
        self.assertEqual(payload["model"], "tiny-model")
        return (self.model_status, "application/json",
                json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode())

    def run_selftest(self, **kwargs):
        out = io.StringIO()
        rc = selftest.run(self.fx.loop, out=out, **kwargs)
        return rc, out.getvalue()

    def assert_no_secrets(self, text):
        for secret in (*SECRETS.values(), MODEL_KEY):
            self.assertNotIn(secret, text)

    def assert_reads_only(self):
        self.assertTrue(self.fx.calls)
        self.assertEqual({method for method, _, _ in self.fx.calls}, {"GET"})


class ChecklistTests(SelftestBase):
    def test_all_green_exits_zero_with_checkmarks(self):
        rc, text = self.run_selftest(pr=7)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("❌", text)
        for name in ("runtime:file", "bwrap:userns", "sandbox:secrets", "model:completion",
                     "github:distinct", "broker:reviewer", "ledger", "cron:job"):
            self.assertRegex(text, rf"✅ {name}\b")
        self.assertIn("would post as reviewer (nothing posted)", text)
        self.assertEqual(self.model_keys, [MODEL_KEY])  # the key reached only the upstream call
        self.assert_no_secrets(text)
        self.assert_reads_only()
        # The sandbox probe was asked about the dummy secret and every configured secret path.
        for path in (self.fx.settings["key_file"], str(self.fx.runtime_file),
                     *self.fx.loop["tokens"].values()):
            self.assertIn(path, self.fx.sandbox_paths)
        self.assertTrue(any("dummy-host-secret" in p for p in self.fx.sandbox_paths))

    def test_no_model_skips_the_completion(self):
        rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 0, text)
        self.assertIn("model:completion", text)
        self.assertIn("--no-model", text)
        self.assertEqual(self.model_keys, [])

    def test_runtime_not_private_fails_with_chmod_fix_and_skips_dependents(self):
        self.fx.runtime_file.chmod(0o644)
        rc, text = self.run_selftest()
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ runtime:file .*mode 644")
        self.assertIn(f"fix: chmod 600 {self.fx.runtime_file}", text)
        self.assertRegex(text, r"sandbox:containment\s+needs a usable runtime")
        self.assertIn("fix these before enabling turns: runtime:file", text)

    def test_missing_runtime_and_bad_upstream(self):
        self.fx.runtime_file.unlink()
        rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertIn("❌ runtime:file", text)
        self.assertIn("chmod 600", text)
        self.fx.write_runtime(upstream="http://models.example/v1/chat/completions")
        rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ runtime:upstream .*'http'")
        self.fx.write_runtime(extra="x")
        rc, text = self.run_selftest(model=False)
        self.assertIn("unexpected extra", text)

    def test_key_file_readable_by_others_fails(self):
        pathlib.Path(self.fx.settings["key_file"]).chmod(0o640)
        rc, text = self.run_selftest()
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ runtime:key_file .*mode 640")
        self.assert_no_secrets(text)

    def test_userns_refused_is_actionable(self):
        refused = subprocess.CompletedProcess([], 1, "", "bwrap: No permissions to creating new namespace")
        with mock.patch.object(selftest.subprocess, "run", return_value=refused):
            rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertIn("❌ bwrap:userns", text)
        self.assertIn("unprivileged_userns", text)

    def test_sandbox_reading_a_host_secret_fails(self):
        self.fx.probe["readable"] = [self.fx.settings["key_file"]]
        self.fx.probe["network"] = True
        rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ sandbox:secrets .*model.key")
        self.assertIn("❌ sandbox:network", text)
        self.assert_no_secrets(text)

    def test_model_rejection_names_the_key_file(self):
        self.model_status = 401
        rc, text = self.run_selftest()
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ model:completion .*HTTP 401")
        self.assertIn("was rejected", text)
        self.assert_no_secrets(text)

    def test_shared_principal_and_wrong_login_fail(self):
        self.fx.ids["fixer"] = 2
        rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ github:distinct .*reviewer \+ fixer")

    def test_github_error_echoing_a_token_is_redacted(self):
        original = self.fx.fetch

        def leaky(loop, path, method="GET", body=None, login=None):
            if path == "/user" and login == "reviewer":
                return None, f"HTTP 401 bad credentials {SECRETS['reviewer']} {SECRETS['reader']}"
            return original(loop, path, method, body, login)
        with mock.patch.object(gh, "fetch", side_effect=leaky):
            rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertIn("❌ github:reviewer", text)
        self.assertIn("[REDACTED]", text)
        self.assert_no_secrets(text)

    def test_denied_authorization_names_the_reason(self):
        self.fx.pr["draft"] = True
        rc, text = self.run_selftest(pr=7, model=False)
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ broker:reviewer .*draft")
        self.assertIn("open and not a draft", text)
        self.assert_reads_only()

    def test_github_writes_are_refused_during_the_selftest(self):
        with selftest.github_read_only():
            with self.assertRaises(selftest.WriteBlocked):
                gh.api(self.fx.loop, "/repos/acme/widgets/pulls/7/reviews", method="POST",
                       body={"event": "APPROVE"}, login="reviewer")
            with self.assertRaises(selftest.WriteBlocked):
                broker.perform(self.fx.loop, repo="acme/widgets", number=7, head=HEAD,
                               role="reviewer", branch="fix-7", operation="review",
                               verdict="APPROVE", body="ok")
        self.assertNotIn("POST", {method for method, _, _ in self.fx.calls})
        self.assertIs(gh.fetch.side_effect.__self__, self.fx)  # original restored

    def test_stub_environment_is_a_failure(self):
        with mock.patch.dict(os.environ, {"REVIEW_LOOP_GH_STUB": "/bin/true"}):
            rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertIn("❌ github:stub", text)


GIT_LOCK = ('[[package]]\nname = "x"\nversion = "1.0.0"\n'
            'source = "git+https://evil.example/x#abc"\n')
PATH_ONLY_LOCK = '[[package]]\nname = "tiny"\nversion = "0.1.0"\n'


class BuildCheckTests(SelftestBase):
    """Step 5 (cont.): whether the seat can build the PR head (issue #51)."""

    def sandbox(self, rc):
        self.sandbox_calls = []

        def run(**kwargs):
            if "cargo metadata" not in " ".join(kwargs["entry"]):
                return self.fx.contained_run(**kwargs)
            self.sandbox_calls.append(kwargs)
            return subprocess.CompletedProcess([], rc, "", "" if rc == 0 else
                                               "error: no matching package named `x` found")
        return mock.patch.object(contained, "run", side_effect=run)

    def test_not_a_rust_head_skips(self):
        rc, text = self.run_selftest(pr=7)
        self.assertEqual(rc, 0, text)
        self.assertIn("build:deps", text)
        self.assertIn("nothing to prefetch", text)

    def test_prefetched_head_that_resolves_offline_passes(self):
        self.head_files = {"Cargo.toml": "[package]\n", "Cargo.lock": PATH_ONLY_LOCK}
        with self.sandbox(0):
            rc, text = self.run_selftest(pr=7)
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r"✅ build:rust:fetch\b")
        self.assertRegex(text, r"✅ build:rust\b.*the seat can build")
        [call] = self.sandbox_calls
        self.assertEqual(list(call["dependency_caches"]), ["rust"])
        self.assertIn("--offline", call["entry"][-1])
        self.assert_reads_only()

    def test_prefetched_but_unresolvable_offline_fails(self):
        self.head_files = {"Cargo.toml": "[package]\n", "Cargo.lock": PATH_ONLY_LOCK}
        with self.sandbox(101):
            rc, text = self.run_selftest(pr=7)
        self.assertEqual(rc, 1)
        self.assertRegex(text, r"❌ build:rust\b")

    def test_refused_prefetch_warns_and_the_seat_cannot_build(self):
        self.head_files = {"Cargo.toml": "[package]\n", "Cargo.lock": GIT_LOCK}
        with self.sandbox(101):
            rc, text = self.run_selftest(pr=7)
        self.assertEqual(rc, 0, text)                 # turns still run; the seat is told
        self.assertIn("build:rust:fetch", text)
        self.assertIn("non-crates.io", text)
        self.assertRegex(text, r"⚠️ +build:rust\b.*cannot build")
        self.assertEqual(self.sandbox_calls[0]["dependency_caches"], {})


class LiveTurnTests(SelftestBase):
    def agent(self, verdict="REQUEST_CHANGES", body=None, extra=None):
        body = body if body is not None else f"Needs a test.\nleaked? {SECRETS['fixer']}"

        def run(**kwargs):
            if "broker_socket_dir" not in kwargs:        # step 2's probe
                return self.fx.contained_run(**kwargs)
            sock = str(pathlib.Path(kwargs["broker_socket_dir"]) / "broker.sock")
            self.query_text = pathlib.Path(kwargs["query"]).read_text()
            self.caches = kwargs.get("dependency_caches")
            if extra:
                self.extra_answer = self.raw_request(sock, extra)
            self.answer = broker_ipc.request("review", verdict=verdict, body=body, socket_path=sock)
            return subprocess.CompletedProcess([], 0, "agent done", "")
        return run

    @staticmethod
    def raw_request(sock, payload):
        import socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(sock)
            conn.sendall(json.dumps(payload).encode() + b"\n")
            return json.loads(broker_ipc._read_line(conn, broker_ipc.MAX_REQUEST))

    def live(self, agent):
        with mock.patch.object(contained, "run", side_effect=agent), \
             mock.patch.object(broker, "perform", side_effect=AssertionError("perform called")), \
             mock.patch.object(review_receipt, "submit", side_effect=AssertionError("submit called")):
            return self.run_selftest(pr=7, live_turn=True, timeout=30)

    def test_live_turn_prints_the_would_be_review_and_posts_nothing(self):
        rc, text = self.live(self.agent())
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.answer, {"ok": True, "result": {"accepted": True}})
        self.assertIn("the agent would submit REQUEST_CHANGES (authorized); NOT posted", text)
        self.assertIn("│ Needs a test.", text)
        self.assertIn("✅ turn:reviewer", text)
        self.assert_no_secrets(text)                     # even inside the model's body
        self.assert_reads_only()
        self.assertFalse((pathlib.Path(self.fx.loop["state_dir"]) / "broker-audit.jsonl").exists())

    def test_live_turn_denied_verdict_fails(self):
        self.fx.pr["draft"] = True
        rc, text = self.live(self.agent())
        self.assertEqual(rc, 1)
        self.assertIn("turn:reviewer", text)
        self.assert_reads_only()

    def test_sandbox_cannot_switch_the_broker_mode(self):
        rc, text = self.live(self.agent(extra={"operation": "review", "verdict": "APPROVE",
                                               "body": "x", "no_write": False}))
        self.assertEqual(self.extra_answer, {"ok": False, "error": "unsupported request fields"})
        self.assertEqual(rc, 0, text)
        self.assert_reads_only()

    def test_unbuildable_head_tells_the_reviewer_to_judge_by_reading(self):
        self.head_files = {"Cargo.toml": "[package]\n", "Cargo.lock": GIT_LOCK}
        rc, text = self.live(self.agent())
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.caches, {})
        self.assertIn("Rust: dependencies are NOT available in this sandbox", self.query_text)
        self.assertIn("not by itself a reason to request changes", self.query_text)
        # The host note leads the message, ahead of the PR records a PR author can shape.
        self.assertTrue(self.query_text.startswith("Build environment (host-checked"))

    def test_buildable_head_mounts_the_cache(self):
        self.head_files = {"Cargo.toml": "[package]\n", "Cargo.lock": PATH_ONLY_LOCK}
        rc, text = self.live(self.agent())
        self.assertEqual(rc, 0, text)
        self.assertEqual(list(self.caches), ["rust"])
        self.assertIn("dependencies are available offline", self.query_text)

    def test_cli_requires_pr_for_live_turn(self):
        args = argparse.Namespace(loop="demo", pr=None, no_model=False, live_turn=True, timeout=120)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.cmd_selftest(args), 2)
        self.assertIn("--pr", out.getvalue())
        args = argparse.Namespace(loop="demo", pr=7, no_model=True, live_turn=True, timeout=120)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.cmd_selftest(args), 2)


class NoWriteBrokerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir="/tmp" if os.access("/tmp", os.W_OK) else None)
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.root.chmod(0o700)
        self.fx = Fixture(self.root)
        self.scope = broker_ipc.RunScope("acme/widgets", 7, HEAD, "reviewer", "fix-7")
        self.api_calls = []

        def api(loop, path, method="GET", body=None, login=None):
            self.api_calls.append((method, path))
            if method != "GET":
                raise AssertionError("POST attempted")
            return self.fx.fetch(loop, path, method, body, login)[0]
        patch = mock.patch.object(gh, "api", side_effect=api)
        patch.start()
        self.addCleanup(patch.stop)

    def serve(self, **kwargs):
        server = broker_ipc.RunBroker(self.fx.loop, self.scope, self.root, **kwargs)
        server.__enter__()
        self.addCleanup(server.close)
        broker_ipc.serve_in_thread(server)
        return server

    def test_no_write_records_and_never_posts(self):
        server = self.serve(require_receipt=True, no_write=True)
        with mock.patch.object(broker, "perform", side_effect=AssertionError), \
             mock.patch.object(review_receipt, "submit", side_effect=AssertionError):
            answer = broker_ipc.request("review", verdict="APPROVE", body="LGTM",
                                        socket_path=str(server.socket_path))
            again = broker_ipc.request("review", verdict="APPROVE", body="LGTM",
                                       socket_path=str(server.socket_path))
        self.assertTrue(answer["ok"])
        self.assertEqual(again, {"ok": False, "error": "run capability already used"})
        self.assertEqual(server.recorded[0]["verdict"], "APPROVE")
        self.assertTrue(server.recorded[0]["authorized"])
        self.assertTrue(server.completed)
        self.assertEqual({m for m, _ in self.api_calls}, {"GET"})
        self.assertFalse((self.root / "state" / "broker-audit.jsonl").exists())

    def test_no_write_refuses_other_operations(self):
        server = self.serve(no_write=True)
        for operation in ("request_review", "ruling"):
            answer = broker_ipc.request(operation, verdict="", body="",
                                        socket_path=str(server.socket_path))
            self.assertEqual(answer, {"ok": False, "error": "operation out of scope"})
        answer = broker_ipc.request("push", manifest={}, socket_path=str(server.socket_path))
        self.assertEqual(answer, {"ok": False, "error": "operation out of scope"})
        self.assertEqual(server.recorded, [])

    def test_flag_is_host_only(self):
        server = self.serve()
        self.assertFalse(server.no_write)
        with self.assertRaises(AttributeError):
            server.no_write = True
        # The socket protocol has no field for it: the client cannot send one ...
        import inspect
        self.assertNotIn("no_write", inspect.signature(broker_ipc.request).parameters)
        # ... and a hand-built request carrying it is refused before any GitHub call.
        with mock.patch.object(broker, "perform", side_effect=AssertionError):
            answer = LiveTurnTests.raw_request(str(server.socket_path),
                                               {"operation": "review", "verdict": "APPROVE",
                                                "body": "x", "no_write": True})
        self.assertEqual(answer, {"ok": False, "error": "unsupported request fields"})
        self.assertEqual(self.api_calls, [])
        for role in ("fixer", "adjudicator"):
            with self.assertRaises(ValueError):
                broker_ipc.RunBroker(self.fx.loop, self.scope.__class__(
                    "acme/widgets", 7, HEAD, role, "fix-7"), self.root, no_write=True)
        with self.assertRaises(ValueError):
            broker_ipc.RunBroker(self.fx.loop, self.scope, self.root, no_write="yes")

    def test_run_turn_rejects_no_write_for_other_roles(self):
        scope = broker_ipc.RunScope("acme/widgets", 7, HEAD, "fixer", "fix-7")
        with self.assertRaises(trusted_turn.TurnDenied):
            trusted_turn.run_turn(self.fx.loop, scope, source=self.root, venv=self.root,
                                  runtime=self.root, rust=self.root, upstream="https://x/v1/chat/completions",
                                  key="k", model="m", prompt="p", no_write=True)


class RegistrationTests(unittest.TestCase):
    def test_selftest_subcommand_is_registered(self):
        captured = {}

        class Ctx:
            def register_cli_command(self, name, help_text, setup, description=""):
                captured["setup"] = setup
        cli.register_cli(Ctx())
        parser = argparse.ArgumentParser()
        captured["setup"](parser)
        args = parser.parse_args(["selftest", "--loop", "demo", "--pr", "7", "--live-turn"])
        self.assertIs(args.func, cli.cmd_selftest)
        self.assertEqual((args.pr, args.live_turn, args.no_model, args.timeout), (7, True, False, 600))
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            parser.parse_args(["selftest"])  # --loop is required


if __name__ == "__main__":
    unittest.main()
