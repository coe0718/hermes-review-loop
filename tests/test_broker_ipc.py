"""Scoped broker IPC: real UDS, strict frames, mocked GitHub REST and optional bwrap."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker_ipc, gh
from scripts import broker_client

HEAD = "a" * 40
REPO = "acme/widgets"


class BrokerIPCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / (login + ".pat")
            path.write_text("DUMMY_SECRET_" + login)
            tokens[login] = str(path)
        self.loop = {"repo": REPO, "base": "main", "state_dir": str(self.root),
                     "fixers": ["fix"], "reviewers": ["review"],
                     "tokens": tokens, "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "fix"}, "head": {"sha": HEAD, "ref": "fix-7",
                    "repo": {"full_name": REPO}}, "base": {"ref": "main", "repo": {"full_name": REPO}}}
        self.calls = []
        def api(loop, path, method="GET", body=None, login=None):
            self.calls.append((path, method, body, login))
            if path == "/user":
                return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}
            return self.pr if method == "GET" else {"id": 9}
        patch = mock.patch.object(gh, "api", side_effect=api)
        patch.start()
        self.addCleanup(patch.stop)
        self.reviews = [{'id': 41, 'state': 'CHANGES_REQUESTED', 'commit_id': HEAD,
                         'submitted_at': '2026-01-01T00:00:00Z',
                         'user': {'login': 'review'}}]
        review_patch = mock.patch.object(gh, 'reviews', side_effect=lambda *args: self.reviews)
        review_patch.start()
        self.addCleanup(review_patch.stop)

    def start(self, role="reviewer", head=HEAD, branch="fix-7", repo=REPO, number=7):
        scope = broker_ipc.RunScope(repo, number, head, role, branch)
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        server.__enter__()
        thread = broker_ipc.serve_in_thread(server)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.close)
        return server

    def send(self, server, request):
        raw = request if isinstance(request, bytes) else json.dumps(request).encode() + b"\n"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(str(server.socket_path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(16384))

    def review(self):
        return {"operation": "review", "verdict": "APPROVE", "body": "verified"}

    def test_success_rechecks_live_head_and_audits_without_secrets(self):
        server = self.start()
        with mock.patch.object(broker_client, "SOCKET", str(server.socket_path)):
            self.assertEqual(broker_client.request("review", "APPROVE", "verified"),
                             {"ok": True, "result": {"accepted": True}})
        self.assertEqual([c[1] for c in self.calls if c[0] != "/user"], ["GET", "POST"])
        self.assertEqual(self.calls[3][-1], "read")
        self.assertEqual(self.calls[4], (f"/repos/{REPO}/pulls/7/reviews", "POST",
                         {"commit_id": HEAD, "event": "APPROVE", "body": "verified"}, "review"))
        self.assertEqual(self.send(server, self.review())["error"], "run capability already used")
        self.assertEqual(len(self.calls), 5)
        audit = (self.root / "broker-audit.jsonl").read_text()
        self.assertEqual(json.loads(audit)["head"], HEAD)
        self.assertNotIn("DUMMY_SECRET", audit)
        self.assertNotIn("DUMMY_SECRET", str(self.send(server, self.review())))

    def test_arbitrary_github_response_is_not_relayed(self):
        server = self.start()
        with mock.patch.object(gh, "api", side_effect=lambda loop, path, method="GET", **kw:
                               ({"login": kw.get("login"), "id": {"read": 1, "review": 2, "fix": 3}[kw.get("login")]}
                                if path == "/user" else self.pr if method == "GET" else {"id": 9, "token": "DUMMY_SECRET_LEAK"})):
            response = self.send(server, self.review())
        self.assertEqual(response, {"ok": True, "result": {"accepted": True}})
        self.assertNotIn("DUMMY_SECRET_LEAK", json.dumps(response))

    def test_each_new_run_revalidates_before_its_write(self):
        first = self.start()
        self.assertTrue(self.send(first, self.review())["ok"])
        self.pr["head"]["sha"] = "b" * 40
        second = self.start(role="fixer")
        self.assertFalse(self.send(second, {"operation": "request_review", "verdict": "", "body": ""})["ok"])
        self.assertEqual([c[1] for c in self.calls if c[0] != "/user"], ["GET", "POST", "GET"])

    def test_request_review_uses_fixed_reviewer_and_fixer_identity(self):
        server = self.start(role="fixer")
        self.assertTrue(self.send(server, {"operation": "request_review", "verdict": "", "body": ""})["ok"])
        self.assertEqual(self.calls[-1], (f"/repos/{REPO}/pulls/7/requested_reviewers", "POST",
                                          {"reviewers": ["review"]}, "fix"))

    def push_then_request(self, live_head_after_push):
        from review_loop import config, run_supervisor, safe_push
        pushed = "c" * 40
        server = self.start(role="fixer")
        object.__setattr__(server, "scope", broker_ipc.RunScope(
            REPO, 7, HEAD, "fixer", "fix-7", "rid", "/nonexistent"))
        supervisor = run_supervisor.Supervisor
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(config, "unattended_fixer_push_enabled", return_value=True), \
             mock.patch.object(config, "push_policy_lock", return_value=mock.MagicMock()), \
             mock.patch.object(supervisor, "__init__", return_value=None), \
             mock.patch.object(supervisor, "push_admitted", return_value=True), \
             mock.patch.object(supervisor, "begin_push"), \
             mock.patch.object(supervisor, "confirm_push"), \
             mock.patch.object(safe_push, "_manifest"), \
             mock.patch.object(safe_push, "push", return_value={"new_head": pushed}):
            self.assertTrue(self.send(server, {"operation": "push", "manifest": {}})["ok"])
            self.pr["head"]["sha"] = live_head_after_push or pushed
            result = self.send(server, {"operation": "request_review", "verdict": "", "body": ""})
        return server, result

    def test_confirmed_push_can_request_review_at_new_head(self):
        # The verdict lives on the old head; the pushed head cannot carry one yet.
        server, result = self.push_then_request(None)
        self.assertTrue(result["ok"], result)
        self.assertTrue(server.completed)
        self.assertEqual(self.calls[-1], (f"/repos/{REPO}/pulls/7/requested_reviewers", "POST",
                                          {"reviewers": ["review"]}, "fix"))

    def test_request_after_push_denied_when_head_moved_again(self):
        server, result = self.push_then_request("d" * 40)
        self.assertFalse(result["ok"])
        self.assertFalse(server.completed)
        self.assertFalse(any(call[0].endswith("/requested_reviewers") for call in self.calls))

    def test_client_waits_for_slow_writes_and_reports_unknown_outcome(self):
        from review_loop import broker_client as sandbox_client, safe_push
        import contextlib, io
        # A real push spends up to 90s in each of fetch and push before GitHub calls.
        for timeout in (broker_client.WRITE_TIMEOUT, sandbox_client.WRITE_TIMEOUT,
                        broker_ipc.WRITE_TIMEOUT):
            self.assertGreater(timeout, 4 * 90)
        path = self.root / "silent.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen(1)
            out = io.StringIO()
            with mock.patch.object(sandbox_client, "WRITE_TIMEOUT", 0.2), \
                 mock.patch.object(sandbox_client, "SOCKET", str(path)), \
                 mock.patch.object(sys, "argv", ["broker_client", "request_review"]), \
                 contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                sandbox_client.main()
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("outcome unknown", result["error"])

    def test_new_approval_cancels_fixer_write_at_unchanged_head(self):
        self.reviews.append({'id': 42, 'state': 'APPROVED', 'commit_id': HEAD,
                             'submitted_at': '2026-01-01T00:01:00Z',
                             'user': {'login': 'review'}})
        server = self.start(role='fixer')
        result = self.send(server, {'operation': 'request_review', 'verdict': '', 'body': ''})
        self.assertFalse(result['ok'])
        self.assertFalse(any(call[1] == 'POST' for call in self.calls))

    def test_fixer_write_rechecks_live_author_and_fails_closed(self):
        for author in (None, {}, {"login": "outsider"}, {"login": 7}):
            with self.subTest(author=author):
                self.pr["user"] = author
                server = self.start(role="fixer")
                result = self.send(server, {"operation": "request_review", "verdict": "", "body": ""})
                self.assertFalse(result["ok"])
                self.assertFalse(any(call[1] == "POST" for call in self.calls))
        self.pr["user"] = {"login": "FIX"}
        server = self.start(role="fixer")
        self.assertTrue(self.send(server, {"operation": "request_review", "verdict": "", "body": ""})["ok"])

    def test_untrusted_fields_and_cross_role_operations_denied(self):
        server = self.start()
        for field, value in (("role", "fixer"), ("head", "b" * 40), ("branch", "other"),
                             ("repo", "other/widgets"), ("number", 8), ("path", "/repos/x"),
                             ("url", "https://evil"), ("token", "abc"), ("destination", "evil")):
            with self.subTest(field=field):
                self.assertFalse(self.send(server, {**self.review(), field: value})["ok"])
        for operation in ("push", "request_review", "delete", "GET"):
            with self.subTest(operation=operation):
                self.assertFalse(self.send(server, {**self.review(), "operation": operation})["ok"])
        self.assertEqual(self.calls, [])
        self.assertTrue(self.send(server, self.review())["ok"])
        fixer = self.start(role="fixer")
        self.assertFalse(self.send(fixer, self.review())["ok"])
        self.assertEqual(len(self.calls), 5)

    def test_stale_and_wrong_scope_rejected_before_post(self):
        for overrides in ({"head": "b" * 40}, {"branch": "wrong"},
                          {"repo": "other/widgets"}, {"number": 8}, {"role": "other"}):
            with self.subTest(overrides=overrides):
                server = self.start(**overrides)
                self.assertFalse(self.send(server, self.review())["ok"])
        self.assertFalse(any(call[1] == "POST" for call in self.calls))
        server = self.start()
        self.pr["head"]["sha"] = "b" * 40
        self.assertFalse(self.send(server, self.review())["ok"])
        self.assertFalse(any(call[1] == "POST" for call in self.calls))

    def test_draft_and_ambiguous_pr_status_denied_before_each_write(self):
        for role, request in (("reviewer", self.review()),
                              ("fixer", {"operation": "request_review", "verdict": "", "body": ""})):
            for status in (True, None, "false"):
                with self.subTest(role=role, draft=status):
                    self.pr["draft"] = status
                    server = self.start(role=role)
                    self.assertFalse(self.send(server, request)["ok"])
                    self.assertFalse(any(call[1] == "POST" for call in self.calls))
        self.pr["draft"] = False
        self.pr["state"] = "closed"
        server = self.start()
        self.assertFalse(self.send(server, self.review())["ok"])
        self.assertFalse(any(call[1] == "POST" for call in self.calls))

    def test_oversize_malformed_and_replay_fail_closed(self):
        server = self.start()
        for request in (b"{not-json}\n", b"x" * (broker_ipc.MAX_REQUEST + 1),
                        json.dumps({**self.review(), "body": "x" * (broker_ipc.MAX_BODY + 1)}).encode() + b"\n"):
            self.assertFalse(self.send(server, request)["ok"])
        self.assertEqual(self.calls, [])
        self.assertTrue(self.send(server, self.review())["ok"])
        self.assertFalse(self.send(server, self.review())["ok"])
        self.assertEqual(len([c for c in self.calls if c[1] == "POST"]), 1)

    def test_socket_path_symlink_and_private_root(self):
        target = self.root / "elsewhere"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(broker_ipc.ProtocolError):
            broker_ipc.RunBroker(self.loop, broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7"), link).__enter__()
        world = self.root / "world"
        world.mkdir(mode=0o755)
        with self.assertRaises(broker_ipc.ProtocolError):
            broker_ipc.RunBroker(self.loop, broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7"), world).__enter__()
        self.assertEqual(list(target.iterdir()), [])
        server = self.start()
        self.assertEqual(server.socket_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(server.socket_path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap unavailable")
    def test_constrained_namespace_socket_round_trip(self):
        server = self.start()
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
               "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
               "--ro-bind", "/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
               "--tmpfs", "/tmp", "--dir", "/run", "--dir", "/run/review-loop",
               "--ro-bind", str(server.socket_path), broker_client.SOCKET,
               "--ro-bind", str(ROOT / "scripts/broker_client.py"), "/client.py",
               "--setenv", "HOME", "/tmp", "--setenv", "HOST_PAT", self.loop["tokens"]["review"],
               "--chdir", "/tmp", "--", "/usr/bin/python3", "-c",
               "import os,runpy,sys; assert not os.path.exists(os.environ['HOST_PAT']); "
               "sys.argv=['/client.py','review','APPROVE','verified']; runpy.run_path('/client.py',run_name='__main__')"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
        if "Creating new namespace failed" in result.stderr:
            self.skipTest("user namespaces disabled by host")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], {"accepted": True})
        self.assertEqual([c[1] for c in self.calls if c[0] != "/user"], ["GET", "POST"])


if __name__ == "__main__":
    unittest.main()
