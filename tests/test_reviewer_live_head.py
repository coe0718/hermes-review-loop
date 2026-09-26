"""Reviewer starts only for a currently eligible, live PR head."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import gate_reviewer

HEAD_A = "a" * 40
HEAD_B = "b" * 40
ACTIONS = ("opened", "ready_for_review", "reopened", "review_requested")


def pr(head=HEAD_A, **changes):
    return {"number": 7, "state": "open", "head": {"sha": head},
            "base": {"ref": "main"}, "user": {"login": "fixer"},
            "draft": False} | changes


class ReviewerLiveHeadTest(unittest.TestCase):
    def setUp(self):
        self.loop = {"repo": "acme/widgets", "base": "main", "fixers": ["fixer"],
                     "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
                     "seats": {"reviewer": {"login": "reviewer"}}, "cap": 3}
        self.state = mock.Mock()
        self.state.inflight.return_value = False
        self.state.release_if.return_value = False

    def run_gate(self, action, current):
        self.state.reset_mock()
        payload = {"action": action, "number": 7, "pull_request": pr(),
                   "sender": {"login": "fixer"},
                   "requested_reviewer": {"login": "reviewer"}}
        output = io.StringIO()
        with (mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))),
              contextlib.redirect_stdout(output),
              mock.patch.object(gate_reviewer.gate, "context", return_value=(self.loop, self.state)),
              mock.patch.object(gate_reviewer.gh, "pr", return_value=current) as live,
              mock.patch.object(gate_reviewer.gate, "fetch_reviews", return_value=[]) as reviews,
              mock.patch.object(gate_reviewer.gate, "drain_seat") as drain,
              mock.patch.object(gate_reviewer.gate, "block_pr_agent",
                                side_effect=lambda *a, **kw: gate_reviewer.silence()) as block,
              mock.patch.object(gate_reviewer.observer, "notify")):
            try:
                gate_reviewer.main()
            except SystemExit as exc:
                self.assertEqual(exc.code, 0)
        return output.getvalue(), live, reviews, drain, block

    def test_stale_open_head_never_starts_or_mutates_seats(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                output, live, reviews, drain, take = self.run_gate(action, pr(HEAD_B))
                self.assertEqual(output.strip(), "[SILENT]")
                live.assert_called_once_with(self.loop, 7)
                reviews.assert_not_called()
                drain.assert_not_called()
                take.assert_not_called()
                self.state.release_if.assert_not_called()

    def test_live_lookup_failure_or_ineligible_pr_fails_closed(self):
        for current in (None, {}, pr(state="closed"), pr(draft=True),
                        pr(base={"ref": "release"}), pr(user={"login": "outsider"}),
                        pr(number=8)):
            for action in ACTIONS:
                with self.subTest(current=current, action=action):
                    output, _, reviews, drain, take = self.run_gate(action, current)
                    self.assertEqual(output.strip(), "[SILENT]")
                    reviews.assert_not_called()
                    drain.assert_not_called()
                    take.assert_not_called()

    def test_matching_live_head_starts_each_trigger(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                output, live, reviews, _, block = self.run_gate(action, pr())
                self.assertEqual(output.strip(), "[SILENT]")
                live.assert_called_once_with(self.loop, 7)
                reviews.assert_called_once_with(self.loop, 7)
                block.assert_called_once()
                self.assertEqual(block.call_args.args[4], HEAD_A)


if __name__ == "__main__":
    unittest.main()
