"""The isolated reviewer's one write must be a verdict: APPROVE or REQUEST_CHANGES.

A COMMENT neither wakes the fixer nor cues a merge, so accepting it would spend the reviewer's
one-shot capability (including the single fresh review after a stacked retarget) and stall the
PR. The broker refuses it before consuming the capability, so a retry in the same turn works.
Offline: GitHub REST is mocked; the socket is a real Unix domain socket.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import string
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker, broker_client, broker_ipc, gh, prompts, review_receipt, trusted_turn
from review_loop.run_supervisor import Supervisor
from scripts import broker_client as script_client

HEAD = "a" * 40
BASE = "b" * 40
REPO = "acme/widgets"
REFUSAL = "review verdict must be APPROVE or REQUEST_CHANGES"


class VerdictOnlyBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / (login + ".pat")
            path.write_text("DUMMY_" + login)
            tokens[login] = str(path)
        self.loop = {"repo": REPO, "base": "main", "state_dir": str(self.root),
                     "fixers": ["fix"], "reviewers": ["review"], "tokens": tokens,
                     "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "fix"},
                   "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": REPO}},
                   "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}}}
        self.posts = []
        patch = mock.patch.object(gh, "api", side_effect=self.api)
        patch.start()
        self.addCleanup(patch.stop)

    def api(self, loop, path, method="GET", body=None, login=None):
        if path == "/user":
            return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}
        if method == "POST":
            self.posts.append(body)
            return {"id": 19}
        if path == f"/repos/{REPO}/pulls/7/reviews/19":
            state = {"APPROVE": "APPROVED",
                     "REQUEST_CHANGES": "CHANGES_REQUESTED"}[self.posts[-1]["event"]]
            return {"id": 19, "state": state, "commit_id": HEAD,
                    "user": {"id": 2, "login": "review"}}
        return self.pr

    def start(self, scope):
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        server.__enter__()
        thread = broker_ipc.serve_in_thread(server)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.close)
        return server

    def send(self, server, verdict, body="reviewed"):
        raw = json.dumps({"operation": "review", "verdict": verdict, "body": body}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(str(server.socket_path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(16384))

    def receipt_scope(self):
        sup = Supervisor(self.root / "runs.sqlite")
        sup.enqueue("d", REPO, 7, HEAD, "reviewer")
        generation = review_receipt.generation_for(self.pr, self.loop, 7, HEAD)
        with sqlite3.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running', generation=?", (generation,))
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        return sup, broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7", run_id,
                                        str(sup.db), generation)

    def test_comment_refused_without_consuming_then_approve_succeeds(self):
        server = self.start(broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7"))
        refused = self.send(server, "COMMENT")
        self.assertFalse(refused["ok"])
        self.assertIn(REFUSAL, refused["error"])
        self.assertIn("COMMENT is not a verdict", refused["error"])
        self.assertEqual(self.posts, [])
        self.assertFalse(server.completed)
        self.assertEqual(self.send(server, "APPROVE"), {"ok": True, "result": {"accepted": True}})
        self.assertEqual([p["event"] for p in self.posts], ["APPROVE"])
        self.assertEqual(self.send(server, "REQUEST_CHANGES")["error"], "run capability already used")
        self.assertEqual(len(self.posts), 1)

    def test_receipted_run_comment_refused_before_claim_then_verdict_confirms(self):
        sup, scope = self.receipt_scope()
        server = self.start(scope)
        self.assertIn(REFUSAL, self.send(server, "COMMENT")["error"])
        with sqlite3.connect(sup.db) as con:
            self.assertIsNone(con.execute("SELECT * FROM review_receipts").fetchone())
        self.assertEqual(self.posts, [])
        self.assertTrue(self.send(server, "REQUEST_CHANGES")["ok"])
        with sqlite3.connect(sup.db) as con:
            self.assertEqual(con.execute("SELECT state,review_id,verdict FROM review_receipts")
                             .fetchone(), ("confirmed", 19, "CHANGES_REQUESTED"))

    def test_invalid_verdicts_and_empty_body_refused_without_consuming(self):
        server = self.start(broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7"))
        for verdict in ("COMMENT", "comment", "approve", "", "PENDING", "DISMISS", "APPROVE "):
            with self.subTest(verdict=verdict):
                response = self.send(server, verdict)
                self.assertFalse(response["ok"])
                self.assertIn(REFUSAL, response["error"])
        self.assertIn(REFUSAL, self.send(server, "APPROVE", body="  \n")["error"])
        self.assertEqual(self.posts, [])
        self.assertTrue(self.send(server, "APPROVE")["ok"])

    def test_host_write_paths_refuse_comment_too(self):
        with self.assertRaises(broker.BrokerDenied):
            broker.perform(self.loop, repo=REPO, number=7, head=HEAD, role="reviewer",
                           branch="fix-7", operation="review", verdict="COMMENT", body="x")
        sup, scope = self.receipt_scope()
        ledger = review_receipt.ReceiptLedger(str(sup.db), scope.run_id, scope.generation)
        with self.assertRaises(review_receipt.ReceiptDenied):
            review_receipt.submit(self.loop, scope, ledger, "COMMENT", "x")
        self.assertEqual(self.posts, [])
        self.assertEqual(broker.REVIEW_VERDICTS, ("APPROVE", "REQUEST_CHANGES"))


class VerdictOnlyClientTests(unittest.TestCase):
    def run_main(self, module, attr, argv):
        with mock.patch.object(module, attr) as call, mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_:
                module.main()
        return call, exit_.exception.code, err.getvalue()

    def test_in_sandbox_clients_reject_comment_before_sending(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", dir=os.environ.get("TMPDIR")) as body:
            body.write("reviewed")
            body.flush()
            for module, attr in ((broker_client, "call"), (broker_ipc, "request")):
                for verdict in ("COMMENT", "", "approve"):
                    with self.subTest(module=module.__name__, verdict=verdict):
                        call, code, err = self.run_main(module, attr, [
                            "broker_client", "review", "--verdict", verdict,
                            "--body-file", body.name])
                        self.assertEqual(code, 2)
                        self.assertIn("APPROVE|REQUEST_CHANGES", err)
                        call.assert_not_called()
        for verdict in ("COMMENT", ""):
            with self.subTest(module="scripts", verdict=verdict):
                with mock.patch.object(script_client, "request") as call, \
                        mock.patch.object(sys, "argv", ["broker_client", "review", verdict, "x"]), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as exit_:
                        script_client.main()
                self.assertEqual(exit_.exception.code, 2)
                self.assertIn("APPROVE|REQUEST_CHANGES", err.getvalue())
                call.assert_not_called()


class VerdictOnlyInstructionTests(unittest.TestCase):
    def test_reviewer_tool_instructions_offer_only_real_verdicts(self):
        text = trusted_turn.tool_instructions("reviewer")
        self.assertNotIn("COMMENT", text)
        self.assertIn("--verdict APPROVE", text)
        self.assertIn("REQUEST_CHANGES", text)

    def test_isolated_reviewer_prompt_demands_one_verdict(self):
        names = {n for _, n, _, _ in string.Formatter().parse(prompts.ISOLATED_REVIEWER) if n}
        text = prompts.render_isolated("reviewer", **{n: "x" for n in names})
        self.assertNotIn("COMMENT", text)
        self.assertIn("exactly one verdict, APPROVE or REQUEST_CHANGES", text)
        self.assertIn("the verdict is REQUEST_CHANGES, naming exactly what could not be verified",
                      text)


if __name__ == "__main__":
    unittest.main()
