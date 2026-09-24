"""Fail-closed stacked approval tests; no GitHub network or live Hermes home."""
import pathlib
import json
import tempfile
import unittest
from unittest import mock

from review_loop import gate, situation, state

A, B, C, D = (letter * 40 for letter in "abcd")
REPO = "acme/widgets"


def pr(number, head, branch, base_ref, base_sha):
    return {"number": number, "state": "open", "draft": False,
            "head": {"ref": branch, "sha": head, "repo": {"full_name": REPO}},
            "base": {"ref": base_ref, "sha": base_sha, "repo": {"full_name": REPO}}}


class SituationAuthorization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = {"repo": REPO, "base": "main", "state_dir": str(pathlib.Path(self.tmp.name) / "state"),
                     "reviewers": ["reviewer"]}
        self.state = state.LoopState(self.loop)
        self.child = pr(2, B, "child", "parent", A)
        self.parent = pr(1, A, "parent", "main", C)
        self.listing = [self.child, self.parent]
        self.reviews = [{"id": 101, "commit_id": A, "state": "APPROVED",
                         "user": {"login": "reviewer"}, "submitted_at": "2026-01-02T00:00:00Z"}]

    def resolution(self):
        with mock.patch.object(situation.gh, "pr", side_effect=lambda loop, n: {1: self.parent, 2: self.child}[n]):
            return situation.resolve(self.loop, 2, listing=self.listing)

    def readiness(self, resolved):
        def read(loop, n):
            return {1: self.parent, 2: self.child}[n]
        with (mock.patch.object(situation.gh, "pr", side_effect=read),
              mock.patch.object(situation.gh, "open_prs_read", return_value=(self.listing, "")),
              mock.patch.object(situation.gh, "fetch", return_value=(
                  {"ref": "refs/heads/main", "object": {"type": "commit", "sha": C}}, "")),
              mock.patch.object(situation.gh, "reviews_read", return_value=(self.reviews, ""))):
            return situation.parent_readiness(self.loop, self.state, 2, resolved)

    def test_unassociated_approval_is_not_readiness(self):
        resolved = self.resolution()
        self.assertEqual(resolved.status, "waiting")
        ready, reason = self.readiness(resolved)
        self.assertFalse(ready)
        self.assertIn("unassociated", reason)

    def test_parent_advance_and_retarget_invalidate_generation(self):
        resolved = self.resolution()
        self.parent = pr(1, D, "parent", "main", C)
        self.listing = [self.child, self.parent]
        self.assertFalse(self.readiness(resolved)[0])
        self.parent = pr(1, A, "parent", "main", C)
        self.child = pr(2, B, "child", "main", C)
        self.listing = [self.child, self.parent]
        self.assertFalse(self.readiness(resolved)[0])

    def test_no_standalone_webhook_can_bind_approval(self):
        resolved = self.resolution()
        self.assertFalse(self.state.associated_review(1, resolved.identity.key, 101))
        self.assertFalse(self.readiness(resolved)[0])

    def test_explain_names_unassociated_approval_without_writing(self):
        def fetch(loop, path):
            if path == gate.gh.pr_path(loop, 2):
                return self.child, ""
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": C}}, ""

        with (mock.patch.object(situation.gh, "pr", side_effect=lambda loop, n: {1: self.parent, 2: self.child}[n]),
              mock.patch.object(situation.gh, "open_prs_read", return_value=(self.listing, "")),
              mock.patch.object(situation.gh, "fetch", side_effect=fetch),
              mock.patch.object(situation.gh, "reviews_read", return_value=(self.reviews, "")),
              mock.patch.object(gate, "hooks_read", return_value=(True, ""))):
            facts = gate.explain_facts(self.loop, 2)
        self.assertEqual(facts["parent_readiness"][0], False)
        self.assertIn("unassociated", facts["parent_readiness"][1])
        self.assertFalse(self.state.review_situations.exists())

    def test_forged_source_label_never_authorizes_stacked_approval(self):
        resolved = self.resolution()
        parent_identity = situation.Identity(A, "main", C, ())
        self.state.review_situations.parent.mkdir(parents=True)
        self.state.review_situations.write_text(json.dumps({f"{REPO}#1": {"101": {
            "identity": parent_identity.key, "review_id": 101,
            "source": "trusted-submission-receipt"}}}))
        self.assertFalse(self.state.associated_review(1, parent_identity.key, 101))
        self.assertFalse(self.readiness(resolved)[0])
        self.reviews[0]["state"] = "DISMISSED"
        self.assertFalse(self.readiness(resolved)[0])
        self.reviews[0]["state"] = "APPROVED"
        self.reviews.append({**self.reviews[0], "id": 102,
                             "state": "CHANGES_REQUESTED", "submitted_at": "2026-01-03T00:00:00Z"})
        self.assertFalse(self.readiness(resolved)[0])
        self.reviews.pop()
        self.parent = pr(1, A, "parent", "main", D)
        self.listing = [self.child, self.parent]
        self.assertFalse(self.readiness(resolved)[0])


if __name__ == "__main__":
    unittest.main()
