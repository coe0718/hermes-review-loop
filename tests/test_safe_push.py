"""Safe push security tests: synthetic API plus real local bare Git receive-pack."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker, broker_ipc, config, gh, safe_push
from review_loop.run_supervisor import Supervisor

HEAD = "a" * 40
REPO = "acme/widgets"
TREE = "b" * 40
NEW_TREE = "c" * 40
NEW_HEAD = "d" * 40

def manifest(path="src/fix.py", data=b"fixed\n"):
    return {"base_head": HEAD, "message": "Fix review feedback", "files": [{
        "path": path, "content_b64": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest()}]}

class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.branch_head = HEAD
        self.pr_head = HEAD
        self.pr_state = "open"
        self.pr_draft: object = False
        self.pr_repo = REPO
        self.pr_branch = "fix-7"
        self.pr_author = "fix"
        self.on_call = None
        self.principal = {"read": 1, "review": 2, "fix": 3}
        self.unreadable_ref = False

    def api(self, loop, path, method="GET", body=None, login=None):
        self.calls.append((path, method, body, login))
        if self.on_call:
            self.on_call(path, method)
        prefix = f"/repos/{REPO}"
        if path == "/user":
            return {"login": login, "id": self.principal[login]}
        if path == f"{prefix}/pulls/7":
            return {"number": 7, "state": self.pr_state, "draft": self.pr_draft,
                    "user": {"login": self.pr_author}, "head": {"sha": self.pr_head,
                    "ref": self.pr_branch, "repo": {"full_name": self.pr_repo}},
                    "base": {"ref": "main", "repo": {"full_name": REPO}}}
        if path.startswith(f"{prefix}/pulls/"):
            return None
        if path == f"{prefix}/git/ref/heads/fix-7":
            return None if self.unreadable_ref else {"ref": "refs/heads/fix-7", "object": {"sha": self.branch_head}}
        raise AssertionError(f"unexpected API call {method} {path}")

class SafePushTests(unittest.TestCase):
    def admitted_scope(self):
        db = self.root / 'runs.sqlite'
        sup = Supervisor(db)
        sup.enqueue('fix', REPO, 7, HEAD, 'fixer')
        with sqlite3.connect(db) as con:
            con.execute("UPDATE runs SET state='running',launch_intent=1,push_admitted=1")
        return broker_ipc.RunScope(REPO, 7, HEAD, 'fixer', 'fix-7',
                                   sup.get('fix')['id'], str(db))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / (login + ".pat")
            path.write_text("DUMMY_SECRET_" + login)
            tokens[login] = str(path)
        self.loop = {"repo": REPO, "base": "main", "state_dir": str(self.root),
                     "unattended_fixer_push": True,
                     "fixers": ["fix"], "reviewers": ["review"],
                     "tokens": tokens, "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.fake = FakeGitHub()
        p = mock.patch.object(gh, "api", side_effect=self.fake.api)
        p.start()
        self.addCleanup(p.stop)
        reviews = [{'id': 41, 'state': 'CHANGES_REQUESTED', 'commit_id': HEAD,
                    'submitted_at': '2026-01-01T00:00:00Z', 'user': {'login': 'review'}}]
        review_patch = mock.patch.object(gh, 'reviews', return_value=reviews)
        review_patch.start()
        self.addCleanup(review_patch.stop)
        cas = mock.patch.object(safe_push, "_git_cas", side_effect=self.cas)
        cas.start()
        self.addCleanup(cas.stop)

    def cas(self, loop, repo, branch, head, files, message, login, identity, *, before_push):
        self.assertEqual((repo, branch, head, login), (REPO, "fix-7", HEAD, "fix"))
        self.assertEqual(identity, {"name": "fix", "email": "3+fix@users.noreply.github.com"})
        self.assertEqual(message, "Fix review feedback")
        self.assertTrue(files)
        before_push(NEW_HEAD)
        self.fake.branch_head = NEW_HEAD
        self.fake.pr_head = NEW_HEAD
        return NEW_HEAD

    def push(self, data=None, **scope):
        return safe_push.push(self.loop, repo=scope.get("repo", REPO), number=scope.get("number", 7),
                              head=scope.get("head", HEAD), role=scope.get("role", "fixer"),
                              branch=scope.get("branch", "fix-7"),
                              manifest=manifest() if data is None else data)

    def records(self):
        return [json.loads(line) for line in (self.root / "broker-audit.jsonl").read_text().splitlines()]

    def test_success_exact_head_identity_and_durable_receipts(self):
        receipt = self.push()
        self.assertEqual(receipt["outcome"], "published")
        self.assertEqual([x["phase"] for x in self.records()], ["attempt", "reconciled"])
        self.assertEqual(self.records()[-1]["outcome"], "published")
        self.assertFalse(any(method == "PATCH" for _, method, *_ in self.fake.calls))
        self.assertEqual(receipt["new_head"], NEW_HEAD)
        self.assertFalse(any(method != "GET" for _, method, *_ in self.fake.calls))
        self.assertNotIn("DUMMY_SECRET", (self.root / "broker-audit.jsonl").read_text())

    def test_absent_or_invalid_opt_in_denied_before_any_git_or_api(self):
        for value in (None, False, "true", 1):
            with self.subTest(value=value):
                candidate = {**self.loop, "unattended_fixer_push": value}
                with mock.patch.object(safe_push, "_git_cas") as git:
                    with self.assertRaises(broker.BrokerDenied):
                        safe_push.push(candidate, repo=REPO, number=7, head=HEAD,
                                       role="fixer", branch="fix-7", manifest=manifest())
                    git.assert_not_called()
                self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.root / "broker-audit.jsonl").exists())

    def test_pr_closes_during_object_construction_before_remote_push(self):
        def close_before_push(*args, before_push):
            self.fake.pr_state = "closed"
            before_push(NEW_HEAD)
            self.fail("closed PR must never reach remote push")
        with mock.patch.object(safe_push, "_git_cas", side_effect=close_before_push):
            with self.assertRaises(broker.BrokerDenied):
                self.push()
        self.assertEqual(self.fake.branch_head, HEAD)
        self.assertFalse(any(record["phase"] == "attempt" for record in self.records()))

    def test_pr_becomes_draft_during_object_construction_before_remote_push(self):
        def draft_before_push(*args, before_push):
            self.fake.pr_draft = True
            before_push(NEW_HEAD)
            self.fail("draft PR must never reach remote push")
        with mock.patch.object(safe_push, "_git_cas", side_effect=draft_before_push):
            with self.assertRaises(broker.BrokerDenied):
                self.push()
        self.assertEqual(self.fake.branch_head, HEAD)
        self.assertFalse(any(record["phase"] == "attempt" for record in self.records()))

    def test_draft_or_unknown_status_denied_before_push(self):
        for status in (True, None, "false"):
            with self.subTest(draft=status), self.assertRaises(broker.BrokerDenied):
                self.fake.pr_draft = status
                self.push()
            self.assertEqual(self.fake.branch_head, HEAD)
        self.assertFalse((self.root / "broker-audit.jsonl").exists())

    def test_outsider_author_denied_before_ref_or_push(self):
        for author in ("outsider", "", None):
            with self.subTest(author=author), self.assertRaises(broker.BrokerDenied):
                self.fake.pr_author = author
                self.push()
        self.assertEqual(self.fake.branch_head, HEAD)
        self.assertFalse((self.root / "broker-audit.jsonl").exists())
        self.assertFalse(any("/git/ref/" in path for path, *_ in self.fake.calls))

    def test_pr_becomes_draft_during_receive_pack_not_acknowledged(self):
        def draft_after_push(*args, before_push):
            before_push(NEW_HEAD)
            self.fake.branch_head = NEW_HEAD
            self.fake.pr_head = NEW_HEAD
            self.fake.pr_draft = True
            return NEW_HEAD
        with mock.patch.object(safe_push, "_git_cas", side_effect=draft_after_push):
            with self.assertRaises(broker.BrokerDenied):
                self.push()
        self.assertEqual(self.fake.branch_head, NEW_HEAD)
        self.assertEqual(self.records()[-1]["outcome"], "published_pr_unverified")

    def test_close_reopen_between_final_check_and_push_exposes_residual_race(self):
        # The ref lease does not compare PR metadata. A close/reopen wholly
        # between the last API check and final API readback is unobservable.
        def interleaved(*args, before_push):
            before_push(NEW_HEAD)
            self.fake.pr_state = "closed"
            self.fake.branch_head = NEW_HEAD
            self.fake.pr_head = NEW_HEAD
            self.fake.pr_state = "open"
            return NEW_HEAD
        with mock.patch.object(safe_push, "_git_cas", side_effect=interleaved):
            self.assertEqual(self.push()["outcome"], "published")
        self.assertEqual(self.fake.branch_head, NEW_HEAD)

    def test_pr_closes_during_receive_pack_not_acknowledged(self):
        def closed_after_push(*args, before_push):
            before_push(NEW_HEAD)
            self.fake.branch_head = NEW_HEAD
            self.fake.pr_head = NEW_HEAD
            self.fake.pr_state = "closed"
            return NEW_HEAD
        with mock.patch.object(safe_push, "_git_cas", side_effect=closed_after_push):
            with self.assertRaises(broker.BrokerDenied):
                self.push()
        self.assertEqual(self.fake.branch_head, NEW_HEAD)
        self.assertEqual(self.records()[-1]["outcome"], "published_pr_unverified")

    def test_attempt_directory_fsync_failure_blocks_remote_push(self):
        actual_fsync = os.fsync
        dirs_synced = []
        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                dirs_synced.append(True)
                if len(dirs_synced) == 1:
                    raise OSError("simulated directory fsync failure")
            return actual_fsync(fd)
        with mock.patch.object(safe_push.os, "fsync", side_effect=fail_directory_sync):
            with self.assertRaises(broker.BrokerDenied):
                self.push()
        self.assertTrue(dirs_synced)
        self.assertEqual(self.fake.branch_head, HEAD)

    def test_same_principal_in_distinct_files_denied(self):
        self.fake.principal = {"read": 1, "review": 1, "fix": 1}
        with self.assertRaises(broker.BrokerDenied):
            self.push()
        self.assertFalse(any(method != "GET" for _, method, *_ in self.fake.calls))

    def test_wrong_scope_and_moved_head_denied(self):
        for scope in ({"repo": "other/repo"}, {"number": 8}, {"head": "e" * 40},
                      {"role": "reviewer"}, {"branch": "other"}):
            with self.subTest(scope=scope), self.assertRaises(broker.BrokerDenied):
                self.push(**scope)
        self.fake.pr_head = "e" * 40
        with self.assertRaises(broker.BrokerDenied):
            self.push()
        self.assertFalse((self.root / "broker-audit.jsonl").exists())

    def test_manifest_policy(self):
        for name in (".git/config", "src/.GIT/hooks/a", "../x", "/abs", "x//y", "x/./y", "x/../y", "x\\y", "x?y", "x%2fy", "x\ny"):
            with self.subTest(name=name), self.assertRaises(broker.BrokerDenied):
                self.push(manifest(name))
        for case in (manifest(data=b"x" * (safe_push.MAX_FILE + 1)),
                     {**manifest(), "base_head": "e" * 40},
                     {**manifest(), "files": [manifest()["files"][0]] * 2},
                     {**manifest(), "files": [manifest("a")["files"][0], manifest("a/b")["files"][0]]}):
            with self.assertRaises(broker.BrokerDenied):
                self.push(case)
        self.assertEqual(self.fake.calls, [])
        for name in (".github/workflows/test.yml", ".GitHub/CODEOWNERS", ".gitmodules",
                     "vendor/.gitattributes", "CODEOWNERS", "docs/CODEOWNERS"):
            with self.subTest(name=name), self.assertRaises(broker.BrokerDenied):
                safe_push._manifest(manifest(name))
        self.assertEqual(safe_push._manifest(manifest("src/codeowners.py"))[1][0][0], "src/codeowners.py")
        self.assertEqual(safe_push._manifest(manifest("docs/release..notes.lock"))[1][0][0], "docs/release..notes.lock")

    def test_prewrite_failure_and_lost_response_reconcile(self):
        def fail(*args, before_push):
            before_push(NEW_HEAD)
            self.assertEqual(self.records()[-1]["phase"], "attempt")
            raise OSError("network down")
        with mock.patch.object(safe_push, "_git_cas", side_effect=fail):
            with self.assertRaises(broker.BrokerDenied):
                self.push()
        self.assertEqual(self.records()[-1]["outcome"], "unchanged")
        (self.root / "broker-audit.jsonl").unlink()
        def lost(*args, before_push):
            before_push(NEW_HEAD)
            self.assertEqual(self.records()[-1]["phase"], "attempt")
            self.fake.branch_head = NEW_HEAD
            self.fake.pr_head = NEW_HEAD
            raise OSError("lost response after published push")
        with mock.patch.object(safe_push, "_git_cas", side_effect=lost):
            with self.assertRaises(broker.BrokerDenied):
                self.push()
        self.assertEqual(self.records()[-1]["outcome"], "published")

    def test_ipc_token_not_exposed_and_one_use(self):
        scope = self.admitted_scope()
        with mock.patch.object(config, "by_repo", return_value=self.loop), broker_ipc.RunBroker(self.loop, scope, self.root) as server:
            thread = broker_ipc.serve_in_thread(server)
            try:
                def send(value):
                    with socket.socket(socket.AF_UNIX) as conn:
                        conn.settimeout(6)
                        conn.connect(str(server.socket_path))
                        conn.sendall(json.dumps(value).encode() + b"\n")
                        conn.shutdown(socket.SHUT_WR)
                        return json.loads(conn.recv(4096))
                self.assertFalse(send({"operation": "push", "manifest": manifest(), "token": "evil"})["ok"])
                result = send({"operation": "push", "manifest": manifest()})
                self.assertEqual(result, {"ok": True, "result": {"accepted": True}})
                self.assertNotIn("DUMMY_SECRET", json.dumps(result))
                self.assertFalse(send({"operation": "push", "manifest": manifest()})["ok"])
            finally:
                server.close()
                thread.join(2)

    def test_broker_reloads_host_policy_and_consumes_one_attempt(self):
        scope = self.admitted_scope()
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        payload = json.dumps({"operation": "push", "manifest": manifest()}).encode()
        with mock.patch.object(config, "by_repo", return_value=None), mock.patch.object(safe_push, "_git_cas") as git:
            with self.assertRaises(broker_ipc.ProtocolError):
                server._dispatch(payload)
            git.assert_not_called()
        with mock.patch.object(config, "by_repo", return_value={**self.loop, "unattended_fixer_push": False}):
            with self.assertRaises(broker_ipc.ProtocolError):
                server._dispatch(payload)
        with mock.patch.object(config, "by_repo", return_value=self.loop):
            self.assertEqual(server._dispatch(payload)["outcome"], "published")
            with self.assertRaises(broker_ipc.ProtocolError):
                server._dispatch(payload)

class RealBareCAS(unittest.TestCase):
    def test_local_object_construction_and_lease_races(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            remote = str(Path(temp) / "remote.git")
            local = str(Path(temp) / "creator.git")
            def git(*args, **kwargs):
                return subprocess.check_output(["git", *args], text=True, **kwargs).strip()
            git("init", "--bare", remote)
            git("init", "--bare", local)
            base_blob = git("--git-dir", local, "hash-object", "-w", "--stdin", input="base")
            link_blob = git("--git-dir", local, "hash-object", "-w", "--stdin", input="file")
            tree = git("--git-dir", local, "mktree", input=f"100644 blob {base_blob}\tfile\n120000 blob {link_blob}\tlink\n"
                                                            f"100755 blob {base_blob}\trun.sh\n")
            env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.org",
                   "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.org"}
            base = git("--git-dir", local, "commit-tree", tree, input="base\n", env=env)
            other = git("--git-dir", local, "commit-tree", tree, "-p", base, input="other\n", env=env)
            git("--git-dir", local, "push", remote, f"{base}:refs/heads/fix-7")
            git("--git-dir", local, "push", remote, f"{other}:refs/heads/staging")
            loop = {"tokens": {"fix": str(Path(temp) / "dummy")}}
            Path(loop["tokens"]["fix"]).write_text("not-a-real-token")
            identity = {"name": "fix", "email": "3+fix@users.noreply.github.com"}
            def cas(expected, entries, *, before_push=None):
                return safe_push._git_cas(loop, REPO, "fix-7", expected, entries,
                                          "Fix review feedback", "fix", identity,
                                          before_push=before_push, remote=remote)
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow",
                                              "GIT_CONFIG_VALUE_0": "never", "GIT_CONFIG_GLOBAL": "/does/not/exist"}):
                for path in ("link/nested", "link", "file/nested", ".git/config"):
                    with self.subTest(path=path), self.assertRaises(broker.BrokerDenied):
                        cas(base, [(path, b"evil")])
                journal = []
                new = cas(base, [("file", b"updated"), ("src/added.py", b"new"), ("run.sh", b"#!/bin/sh\n")],
                          before_push=lambda sha: journal.append(sha))
                self.assertEqual(journal, [new])
                self.assertEqual(git("--git-dir", remote, "rev-parse", "refs/heads/fix-7"), new)
                self.assertEqual(git("--git-dir", remote, "rev-list", "--parents", "-n", "1", new), f"{new} {base}")
                self.assertEqual(git("--git-dir", remote, "show", f"{new}:file"), "updated")
                self.assertEqual(git("--git-dir", remote, "show", f"{new}:src/added.py"), "new")
                modes = {line.split("\t")[1]: line.split(" ")[0] for line in
                         git("--git-dir", remote, "ls-tree", new).splitlines()}
                self.assertEqual((modes["run.sh"], modes["file"]), ("100755", "100644"))
                self.assertEqual(git("--git-dir", remote, "show", "-s", "--format=%an <%ae>|%cn <%ce>", new),
                                 "fix <3+fix@users.noreply.github.com>|fix <3+fix@users.noreply.github.com>")
                # Concurrent advance after advertised fetch is rejected by the exact lease.
                def move(sha):
                    git("--git-dir", remote, "update-ref", "refs/heads/fix-7", other)
                with self.assertRaises(broker.BrokerDenied):
                    cas(new, [("file", b"second")], before_push=move)
                self.assertEqual(git("--git-dir", remote, "rev-parse", "refs/heads/fix-7"), other)
                # A rollback is also rejected, even when a plain force push would succeed.
                git("--git-dir", remote, "update-ref", "refs/heads/fix-7", new)
                def rollback(sha):
                    git("--git-dir", remote, "update-ref", "refs/heads/fix-7", base)
                with self.assertRaises(broker.BrokerDenied):
                    cas(new, [("file", b"third")], before_push=rollback)
                self.assertEqual(git("--git-dir", remote, "rev-parse", "refs/heads/fix-7"), base)
                with self.assertRaises(broker.BrokerDenied):
                    cas(new, [("file", b"stale")])

if __name__ == "__main__":
    unittest.main()
