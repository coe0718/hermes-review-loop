"""Focused proof for fail-closed gates, broker guards, and a Rust-only namespace probe.

The bwrap probe does NOT prove an entire Hermes agent is contained: no agent is
launched by this plugin. It validates a feasible isolated compiler mount layout.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker, gh, gate, isolation


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.tokens = {}
        for role in ("read", "review", "fix"):
            path = self.root / (role + ".pat")
            path.write_text("dummy-" + role)
            self.tokens[role] = str(path)
        self.head = "a" * 40
        self.pr = {"number": 7, "state": "open", "draft": False,
                   "user": {"login": "fix"}, "head": {"sha": self.head,
                   "ref": "fix-7", "repo": {"full_name": "acme/widgets"}},
                   "base": {"ref": "main", "repo": {"full_name": "acme/widgets"}}}
        self.loop = {"repo": "acme/widgets", "base": "main", "state_dir": str(self.root),
                     "fixers": ["fix"], "reviewers": ["review"],
                     "tokens": self.tokens, "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.kw = dict(repo="acme/widgets", number=7, head=self.head, role="reviewer",
                       branch="fix-7", operation="review", verdict="APPROVE", body="verified")

    def test_broker_exact_binding_and_audit(self):
        calls = []
        def api(loop, path, method="GET", body=None, login=None):
            calls.append((path, method, body, login))
            if path == "/user":
                return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}
            return self.pr if method == "GET" else {"id": 8}
        reviews = [{'id': 41, 'state': 'CHANGES_REQUESTED', 'commit_id': self.head,
                    'submitted_at': '2026-01-01T00:00:00Z', 'user': {'login': 'review'}}]
        with mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(gh, 'reviews', return_value=reviews):
            self.assertEqual(broker.perform(self.loop, **self.kw), {"id": 8})
            self.assertEqual(calls[-1][2]["commit_id"], self.head)
            self.assertEqual(calls[-1][3], "review")
            self.assertEqual(broker.perform(self.loop, **{**self.kw, "role": "fixer",
                              "operation": "request_review", "verdict": "", "body": ""}), {"id": 8})
            self.assertEqual(calls[-1][2], {"reviewers": ["review"]})
            self.assertEqual(calls[-1][3], "fix")
            before = len(calls)
            for changed in ({"repo": "other/widgets"}, {"number": 8},
                            {"branch": "elsewhere"}, {"head": "b" * 40},
                            {"role": "fixer"}, {"operation": "push"}):
                with self.subTest(changed=changed), self.assertRaises(broker.BrokerDenied):
                    broker.perform(self.loop, **{**self.kw, **changed})
            self.assertEqual(len([c for c in calls[before:] if c[1] == "POST"]), 0)
            self.pr["head"]["sha"] = "b" * 40
            with self.assertRaisesRegex(broker.BrokerDenied, "stale"):
                broker.perform(self.loop, **self.kw)
        records = (self.root / "broker-audit.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 2)
        self.assertEqual(json.loads(records[0])["branch"], "fix-7")
        self.assertNotIn("dummy-", "".join(records))

    def test_missing_explicit_mapping_fails_before_api(self):
        with mock.patch.object(gh, "api") as api:
            for loop in ({**self.loop, "read_token": "absent"},
                         {**self.loop, "seats": {"reviewer": {"login": "absent"}}},
                         {**self.loop, "read_token": "review"},
                         {**self.loop, "tokens": {**self.tokens, "review": self.tokens["read"]}}):
                with self.assertRaises(broker.BrokerDenied):
                    broker.perform(loop, **self.kw)
            api.assert_not_called()

    def test_gate_blocks_before_workspace_or_gateway_payload(self):
        st = mock.Mock()
        st.queue_items.return_value = {}
        with mock.patch.object(gate.isolation, "ensure") as ensure:
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as stdout:
                with self.assertRaises(SystemExit) as caught:
                    gate.block_pr_agent(self.loop, st, "reviewer", 7, self.head)
                self.assertEqual(caught.exception.code, 0)
                self.assertEqual(stdout.getvalue().strip(), "[SILENT]")
            ensure.assert_not_called()
        self.assertIn("isolated worker unavailable", st.queue_replace_if.call_args.args[-1])

    def test_clone_removes_inherited_credential_helper(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "credential.helper",
                        "!f() { echo password=dummy-seat-token; }; f"], check=True)
        isolation._configure(repo, self.loop, "review")
        helper = subprocess.run(["git", "-C", str(repo), "config", "--get", "credential.helper"],
                                text=True, capture_output=True, check=True)
        self.assertEqual(helper.stdout.strip(), "")
        result = subprocess.run(["git", "-C", str(repo), "-c", "credential.interactive=false",
                                 "credential", "fill"],
                                input="protocol=https\nhost=github.com\nusername=unused\n\n", text=True,
                                capture_output=True, timeout=5,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                     "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                                     "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false"})
        self.assertNotIn("dummy-seat-token", result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap unavailable")
    def test_credentialless_rust_namespace_probe(self):
        toolchains = pathlib.Path.home() / ".rustup/toolchains"
        stable = toolchains / "stable-x86_64-unknown-linux-gnu"
        if not (stable / "bin/cargo").exists():
            self.skipTest("stable Rust toolchain unavailable")
        workspace = self.root / "work"
        (workspace / "src").mkdir(parents=True)
        (workspace / "Cargo.toml").write_text('[package]\nname="boundary_probe"\nversion="0.1.0"\nedition="2021"\n')
        (workspace / "src/lib.rs").write_text('#[test] fn works() { assert_eq!(2 + 2, 4); }\n')
        secret = self.root / "seat.pat"
        secret.write_text("dummy-seat-token")
        probe = self.root / "probe.py"
        probe.write_text('''import os, subprocess
assert not os.path.exists(os.environ["HOST_PAT"])
for candidate in (os.environ["HOST_PAT"], "/work/../../" + os.environ["HOST_PAT"].lstrip("/"),
                  "/proc/self/root" + os.environ["HOST_PAT"], "/home/jeremy/.hermes/.env"):
    try:
        open(candidate, "rb").read()
    except (FileNotFoundError, PermissionError):
        pass
    else:
        raise AssertionError("host path readable: " + candidate)
for path in ("/bin/sh", "/usr/bin/python3"):
    subprocess.run([path, "-c", "test ! -r $HOST_PAT" if path.endswith("sh") else "import os; assert not os.path.exists(os.environ['HOST_PAT'])"], check=True)
p = subprocess.run(["git", "credential", "fill"], input="protocol=https\\nhost=github.com\\n\\n", text=True, capture_output=True, timeout=5)
assert "dummy-seat-token" not in (p.stdout + p.stderr)
subprocess.run(["cargo", "--version"], check=True)
subprocess.run(["cargo", "test", "--offline"], cwd="/work", check=True)
''')
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
               "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
               "--ro-bind", "/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
               "--tmpfs", "/tmp", "--dir", "/home", "--dir", "/opt",
               "--ro-bind", str(stable), "/opt/rust", "--bind", str(workspace), "/work",
               "--ro-bind", str(probe), "/probe.py", "--setenv", "HOME", "/tmp",
               "--setenv", "CARGO_HOME", "/tmp/cargo", "--setenv", "RUSTUP_HOME", "/tmp/rustup",
               "--setenv", "HOST_PAT", str(secret),
               "--setenv", "PATH", "/opt/rust/bin:/usr/bin:/bin", "--chdir", "/work",
               "--", "/usr/bin/python3", "/probe.py"]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=120,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)})
        if "Creating new namespace failed" in result.stderr:
            self.skipTest("user namespaces disabled by host")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cargo ", result.stdout)
        self.assertIn("test result: ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
