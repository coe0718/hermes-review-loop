"""Adversarial webhook snapshots must not claim stale heads or reclaimed disk."""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import gate_fixer, gate_reviewer

HEAD_A = "a" * 40
HEAD_B = "b" * 40


def pr(head=HEAD_A, state="open"):
    return {"number": 7, "state": state, "head": {"sha": head},
            "base": {"ref": "main", "sha": HEAD_B,
                     "repo": {"full_name": "acme/widgets"}}, "user": {"login": "fixer"}}


class NotificationFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.loop = {"repo": "acme/widgets", "id": "widgets", "base": "main",
                     "fixers": ["fixer"], "reviewers": ["reviewer"],
                     "reviewer_seat": "reviewer"}
        self.state = mock.Mock()
        self.state.release_if.return_value = True

    def invoke(self, module, payload, current, reviews: object = ...):
        if reviews is ...:
            reviews = [payload.get("review")]
        output = io.StringIO()
        with (mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
              contextlib.redirect_stdout(output),
              mock.patch.object(module.gate, "context", return_value=(self.loop, self.state)),
              mock.patch.object(module.gh, "pr", return_value=current) as live,
              mock.patch.object(module.gh, "reviews", return_value=reviews),
              mock.patch.object(module.gh, "api", return_value={
                  "ref": "refs/heads/main", "object": {"type": "commit", "sha": HEAD_B}}),
              mock.patch.object(module.gate, "reclaim") as reclaim,
              mock.patch.object(module.gate, "drain_seat") as drain,
              mock.patch.object(module.observer, "notify") as notify):
            with self.assertRaises(SystemExit) as stop:
                module.main()
            self.assertEqual(stop.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "[SILENT]")
        return live, reclaim, drain, notify

    def test_delayed_close_against_reopened_or_unknown_pr_does_not_claim_reclaim(self):
        payload = {"action": "closed", "number": 7, "pull_request": pr(state="closed")}
        for current in (pr(), pr(HEAD_B), None, {}, pr(state="closed") | {"number": 8}):
            with self.subTest(current=current):
                live, reclaim, _, notify = self.invoke(gate_reviewer, payload, current)
                live.assert_called_once_with(self.loop, 7)
                reclaim.assert_not_called()
                notify.assert_not_called()

    def test_confirmed_close_never_claims_disk_reclaimed_without_receipt(self):
        payload = {"action": "closed", "number": 7,
                   "pull_request": pr(state="closed") | {"merged": True}}
        _, reclaim, _, notify = self.invoke(gate_reviewer, payload, pr(state="closed") | {"merged": True})
        reclaim.assert_called_once_with(self.loop, 7, "merged")
        notify.assert_called_once()
        self.assertNotIn("reclaimed", str(notify.call_args).lower())

    def test_delayed_approval_uses_live_head_not_webhook_snapshot(self):
        payload = {"action": "submitted", "number": 7, "pull_request": pr(),
                   "review": {"id": 42, "state": "approved", "commit_id": HEAD_A,
                              "user": {"login": "reviewer"}}}
        for current in (pr(HEAD_B), None, {}, pr(state="closed"),
                        pr() | {"number": 8}, pr() | {"head": {}}):
            with self.subTest(current=current):
                self.state.reset_mock()
                self.state.release_if.return_value = False    # no claim at the approved head
                live, _, drain, notify = self.invoke(gate_fixer, payload, current)
                live.assert_called_once_with(self.loop, 7)
                # Only the claim made for the approved head may end; never another run's.
                self.state.release_if.assert_called_once_with("reviewer", mock.ANY, HEAD_A)
                drain.assert_not_called()
            with self.subTest(current=current, claim_at_approved_head=True):
                # That claim is freed even though the live read failed, and its queue drains.
                self.state.reset_mock()
                self.state.release_if.return_value = True
                _, _, drain, _ = self.invoke(gate_fixer, payload, current)
                drain.assert_called_once()
                notify.assert_called_once()
                self.assertNotEqual(notify.call_args.kwargs["next_turn"], "you merge")

    def test_matching_live_approval_keeps_merge_handoff(self):
        payload = {"action": "submitted", "number": 7, "pull_request": pr(),
                   "review": {"id": 43, "state": "approved", "commit_id": HEAD_A,
                              "user": {"login": "reviewer"},
                              "submitted_at": "2026-01-01T00:00:00Z"}}
        _, _, _, notify = self.invoke(gate_fixer, payload, pr())
        self.assertEqual(notify.call_args.kwargs["next_turn"], "you merge")

    def test_newer_same_head_changes_requested_blocks_delayed_approval(self):
        approval = {"id": 43, "state": "approved", "commit_id": HEAD_A,
                    "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        rejection = approval | {"id": 44, "state": "CHANGES_REQUESTED",
                                "submitted_at": "2026-01-01T00:01:00Z"}
        payload = {"action": "submitted", "number": 7, "pull_request": pr(),
                   "review": approval}
        for reviews in ([rejection, approval], [approval, rejection],
                        [approval, rejection | {"submitted_at": None}],
                        [approval, rejection | {"state": "UNRECOGNIZED"}]):
            with self.subTest(reviews=reviews):
                _, _, _, notify = self.invoke(gate_fixer, payload, pr(), reviews)
                self.assertNotEqual(notify.call_args.kwargs["next_turn"], "you merge")

    def test_delayed_rejection_after_approval_neither_releases_nor_notifies(self):
        rejection = {"id": 43, "state": "changes_requested", "commit_id": HEAD_A,
                     "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        approval = rejection | {"id": 44, "state": "APPROVED",
                                "submitted_at": "2026-01-01T00:01:00Z"}
        payload = {"action": "submitted", "number": 7, "pull_request": pr(),
                   "review": rejection}
        _, _, drain, notify = self.invoke(gate_fixer, payload, pr(), [approval, rejection])
        self.state.release_if.assert_not_called()
        drain.assert_not_called()
        notify.assert_not_called()
        self.state.breach_get.assert_not_called()

    def test_dismissed_and_stale_head_reviews_do_not_override_live_approval(self):
        approval = {"id": 43, "state": "approved", "commit_id": HEAD_A,
                    "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        later = approval | {"id": 44, "state": "CHANGES_REQUESTED",
                            "submitted_at": "2026-01-01T00:01:00Z"}
        payload = {"action": "submitted", "number": 7, "pull_request": pr(),
                   "review": approval}
        for review in (later | {"state": "DISMISSED"}, later | {"commit_id": HEAD_B}):
            with self.subTest(review=review):
                _, _, _, notify = self.invoke(gate_fixer, payload, pr(), [review, approval])
                self.assertEqual(notify.call_args.kwargs["next_turn"], "you merge")

    def test_dismissed_or_unverified_live_review_never_claims_merge(self):
        payload = {"action": "submitted", "number": 7, "pull_request": pr(),
                   "review": {"id": 43, "state": "approved", "commit_id": HEAD_A,
                              "user": {"login": "reviewer"}}}
        for reviews in ([payload["review"] | {"state": "DISMISSED"}], [], None,
                        [payload["review"] | {"id": 44}],
                        [payload["review"] | {"commit_id": HEAD_B}]):
            with self.subTest(reviews=reviews):
                _, _, _, notify = self.invoke(gate_fixer, payload, pr(), reviews)
                self.assertNotEqual(notify.call_args.kwargs["next_turn"], "you merge")


    def test_delayed_rejection_after_approval_never_releases_or_starts_fix(self):
        rejection = {"id": 42, "state": "changes_requested", "commit_id": HEAD_A,
                     "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        approval = rejection | {"id": 43, "state": "APPROVED",
                                "submitted_at": "2026-01-01T00:01:00Z"}
        payload = {"action": "submitted", "number": 7, "pull_request": pr(), "review": rejection}
        for reviews in ([rejection, approval], [approval, rejection],
                        [rejection | {"state": "DISMISSED"}],
                        [rejection, approval | {"submitted_at": None}], None):
            with self.subTest(reviews=reviews):
                self.state.reset_mock()
                with (mock.patch.object(gate_fixer.gate, "take_seat") as take,
                      mock.patch.object(gate_fixer.gate, "breach") as breach):
                    _, _, drain, notify = self.invoke(gate_fixer, payload, pr(), reviews)
                self.state.release_if.assert_not_called()
                drain.assert_not_called()
                notify.assert_not_called()
                take.assert_not_called()
                breach.assert_not_called()

    def test_live_rejection_keeps_direct_trunk_cap_path(self):
        rejection = {"id": 42, "state": "changes_requested", "commit_id": HEAD_A,
                     "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:01:00Z"}
        older = rejection | {"id": 41, "submitted_at": "2026-01-01T00:00:00Z"}
        payload = {"action": "submitted", "number": 7, "pull_request": pr(), "review": rejection}
        self.loop["cap"] = 2
        self.state.inflight.return_value = False
        with (mock.patch.object(gate_fixer.sys, "stdin", io.StringIO(json.dumps(payload))),
              mock.patch.object(gate_fixer.gate, "context", return_value=(self.loop, self.state)),
              mock.patch.object(gate_fixer.gh, "pr", return_value=pr()),
              mock.patch.object(gate_fixer.gh, "reviews", return_value=[rejection, older]),
              mock.patch.object(gate_fixer.gate, "drain_seat"),
              mock.patch.object(gate_fixer.gate, "breach") as breach,
              mock.patch.object(gate_fixer.gate, "take_seat") as take,
              mock.patch.object(gate_fixer.observer, "notify"),
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaises(SystemExit):
                gate_fixer.main()
        breach.assert_called_once()
        take.assert_not_called()


if __name__ == "__main__":
    unittest.main()
