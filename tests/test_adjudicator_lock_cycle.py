"""The gateway must finish adjudicator filtering before its POST response returns."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import state

HEAD_A, HEAD_B = "a" * 40, "b" * 40


class AdjudicatorCycleTest(unittest.TestCase):
    def setUp(self):
        scratch = pathlib.Path(os.environ.get("TMPDIR", pathlib.Path.home() / ".hermes/cache/scratch"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.config = self.root / "loops"
        self.config.mkdir()
        self.head = HEAD_A
        self.loop = {"id": "cycle", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "fixers": ["fixer"], "reviewers": ["reviewer"],
                     "seats": {seat: {"route": seat, "profile": "default"}
                               for seat in ("reviewer", "fixer")},
                     "state_dir": str(self.root / "state")}
        (self.config / "cycle.json").write_text(json.dumps(self.loop))
        stub = self.root / "gh_stub.py"
        stub.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        "head = open(os.environ['TEST_HEAD']).read().strip()\n"
                        "if '/reviews' in sys.argv[1]:\n"
                        " print(json.dumps([{'id': i, 'state': 'CHANGES_REQUESTED', "
                        "'user': {'login': 'reviewer'}} for i in range(3)]))\n"
                        "else:\n print(json.dumps({'number': 7, 'state': 'open', "
                        "'draft': False, 'base': {'ref': 'main'}, "
                        "'user': {'login': 'fixer'}, 'head': {'sha': head}}))\n")
        stub.chmod(0o700)
        self.head_file = self.root / "head"
        self.head_file.write_text(HEAD_A)
        self.env = {**os.environ, "REVIEW_LOOP_CONFIG_DIR": str(self.config),
                    "REVIEW_LOOP_GH_STUB": str(stub), "TEST_HEAD": str(self.head_file)}
        self.st = state.LoopState(self.loop)
        self.requests = []
        parent = self

        class Gateway(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    result = subprocess.run([sys.executable, str(ROOT / "scripts/gate_adjudicator.py")],
                                            input=payload, capture_output=True, env=parent.env,
                                            timeout=2)
                except subprocess.TimeoutExpired:
                    parent.requests.append("timeout")
                    self.send_response(504)
                else:
                    output = result.stdout.decode().strip()
                    parent.requests.append(output or result.stderr.decode().strip())
                    self.send_response(200 if result.returncode == 0 and output.startswith("{") else 422)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def deliver(self, head, current=None):
        entry = {"pr": 7, "head": head, "rounds": 3, "reason": "cap"}
        def send(marker):
            payload = {"action": "review_loop_breach", "number": 7,
                       "repository": {"full_name": "acme/widgets"},
                       "_loop": {"role": "adjudicator", "pr": 7, "head": marker["head"]}}
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/",
                                           data=json.dumps(payload).encode()), timeout=4) as response:
                    return response.status == 200
            except Exception:
                return False
        return self.st.breach_deliver(7, entry, current or (lambda: self.head_file.read_text() == head), send)

    def test_synchronous_gateway_is_held_without_deadlock(self):
        self.assertEqual(self.deliver(HEAD_A), "new")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0], "[SILENT]")
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")
        self.head_file.write_text(HEAD_B)
        self.assertEqual(self.deliver(HEAD_A), "stale")
        self.assertEqual(self.deliver(HEAD_B), "new")
        self.assertEqual(self.requests[-1], "[SILENT]")
        self.assertEqual(self.st.breach_get(7)["head"], HEAD_B)

    def test_concurrent_delivery_and_failure_recovery(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.deliver(HEAD_A), range(4)))
        self.assertEqual(results.count("new"), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")

        self.head_file.write_text(HEAD_B)
        # An unsuccessful transport leaves the new head retryable, not consumed.
        original = self.server.server_port
        self.server.server_port = 9
        try:
            self.assertEqual(self.deliver(HEAD_B), "new")
        finally:
            self.server.server_port = original
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")
        self.assertEqual(self.deliver(HEAD_B), "retry")
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")
        self.assertEqual(self.requests[-1], "[SILENT]")

    def test_late_delivery_does_not_overwrite_newer_head(self):
        entry = lambda head: {"pr": 7, "head": head, "rounds": 3, "reason": "cap"}
        def send_a(marker):
            self.head_file.write_text(HEAD_B)
            self.assertEqual(self.st.breach_deliver(7, entry(HEAD_B),
                                                   lambda: True, lambda _: True), "new")
            return True
        self.assertEqual(self.st.breach_deliver(7, entry(HEAD_A),
                                               lambda: True, send_a), "new")
        self.assertEqual(self.st.breach_get(7)["head"], HEAD_B)
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_abandoned_attempt_waits_for_lease_then_retries(self):
        entry = {"pr": 7, "head": HEAD_A, "rounds": 3, "reason": "cap"}
        self.st.breach_set(7, {**entry, "status": "delivery-pending",
                               "delivery_token": "abandoned", "delivery_at": 0})
        sent = []
        self.assertEqual(self.st.breach_deliver(7, entry, lambda: True,
                                               lambda marker: sent.append(marker) or True), "retry")
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")


if __name__ == "__main__":
    unittest.main()
