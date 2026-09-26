"""A cap breach must not wake adjudication after approval of the parked head."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import state

HEAD = "a" * 40
OLD_HEAD = "b" * 40


class PostCapApprovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        config = root / "loops"
        config.mkdir()
        loop = {"id": "approval", "repo": "acme/widgets", "base": "main", "cap": 3,
                "fixers": ["fixer"], "reviewers": ["reviewer"],
                "seats": {seat: {"route": seat, "profile": "default"}
                          for seat in ("reviewer", "fixer")},
                "state_dir": str(root / "state")}
        (config / "approval.json").write_text(json.dumps(loop))
        self.st = state.LoopState(loop)
        self.reviews = root / "reviews.json"
        stub = root / "gh_stub.py"
        stub.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        "if '/reviews' in sys.argv[1]:\n"
                        " print(open(os.environ['TEST_REVIEWS']).read())\n"
                        "else:\n print(json.dumps({'number': 7, 'state': 'open', "
                        "'draft': False, 'base': {'ref': 'main'}, "
                        "'user': {'login': 'fixer'}, 'head': {'sha': '" + HEAD + "'}}))\n")
        stub.chmod(0o700)
        self.env = {**os.environ, "HOME": str(root), "HERMES_HOME": str(root / "hermes"),
                    "TMPDIR": str(root), "REVIEW_LOOP_CONFIG_DIR": str(config),
                    "REVIEW_LOOP_GH_STUB": str(stub), "TEST_REVIEWS": str(self.reviews)}
        self.payload = {"action": "review_loop_breach", "number": 7,
                        "repository": {"full_name": "acme/widgets"},
                        "_loop": {"role": "adjudicator", "pr": 7, "head": HEAD}}

    def run_gate(self, approval_head, status="awaiting-adjudication", later_verdict=None,
                 earlier_verdict=None):
        marker = {"pr": 7, "head": HEAD, "rounds": 3, "reason": "cap", "status": status}
        if status == "delivery-pending":
            marker.update(delivery_token="in-flight", delivery_at=0)
        self.st.breach_set(7, marker)
        reviews = [{"id": i, "state": "CHANGES_REQUESTED", "user": {"login": "reviewer"},
                    "commit_id": OLD_HEAD} for i in range(3)]
        if approval_head:
            reviews.append({"id": 4, "state": "APPROVED", "user": {"login": "reviewer"},
                            "commit_id": approval_head, "submitted_at": "2026-01-01T00:00:00Z"})
        if earlier_verdict:
            reviews.append({"id": 4, "state": earlier_verdict,
                            "user": {"login": "reviewer"}, "commit_id": HEAD,
                            "submitted_at": "2026-01-01T00:00:00Z"})
        if later_verdict:
            reviews.append({"id": 5, "state": later_verdict,
                            "user": {"login": "reviewer"}, "commit_id": HEAD,
                            "submitted_at": "2026-01-02T00:00:00Z"})
        self.reviews.write_text(json.dumps(reviews))
        result = subprocess.run([sys.executable, str(ROOT / "scripts/gate_adjudicator.py")],
                                input=json.dumps(self.payload), text=True, capture_output=True,
                                env=self.env, timeout=5)
        return result

    def test_approval_of_parked_head_blocks_adjudication(self):
        result = self.run_gate(HEAD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stdout.strip().startswith("{"), result.stdout)
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_approval_during_delivery_pending_blocks_adjudication(self):
        result = self.run_gate(HEAD, status="delivery-pending")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stdout.strip().startswith("{"), result.stdout)
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")

    def test_old_head_approval_does_not_block_current_breach(self):
        result = self.run_gate(OLD_HEAD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[SILENT]")
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_no_approval_allows_current_breach(self):
        result = self.run_gate(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[SILENT]")
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_older_approval_then_newer_changes_at_cap_claims_breach(self):
        result = self.run_gate(HEAD, later_verdict="CHANGES_REQUESTED")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[SILENT]")
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_latest_approval_suppresses_earlier_changes_at_cap(self):
        result = self.run_gate(None, earlier_verdict="CHANGES_REQUESTED",
                               later_verdict="APPROVED")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stdout.strip().startswith("{"), result.stdout)
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")


if __name__ == "__main__":
    unittest.main()
