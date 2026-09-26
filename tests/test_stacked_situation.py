"""Fail-closed identity and chain resolution for stacked PRs."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from review_loop import gh, situation

REPO = "acme/widgets"
A, B, C, D = (letter * 40 for letter in "abcd")
LOOP = {"repo": REPO, "base": "main"}


def pr(number, branch, head, base, base_sha, *, repo=REPO, state="open"):
    return {"number": number, "state": state,
            "head": {"ref": branch, "sha": head, "repo": {"full_name": repo}},
            "base": {"ref": base, "sha": base_sha, "repo": {"full_name": REPO}}}


class SituationTest(unittest.TestCase):
    def resolve(self, child, others=(), *, open_prs=None):
        by_number = {p["number"]: p for p in others}
        by_number[child["number"]] = child
        with mock.patch.object(situation.gh, "pr", side_effect=lambda _loop, n: by_number.get(n)), mock.patch.object(
                situation.gh, "open_prs_read", return_value=(list(others), "") if open_prs is None else open_prs), mock.patch.object(
                situation.gh, "fetch", return_value=({"ref": "refs/heads/main", "object": {"type": "commit", "sha": A}}, "")):
            return situation.resolve(LOOP, child["number"])

    def test_direct_base_requires_readable_live_trunk_ref(self):
        direct = pr(184, "child", C, "main", A)
        path = "/repos/acme/widgets/git/ref/heads/main"
        cases = (({"ref": "refs/heads/other", "object": {"type": "commit", "sha": A}}, ""),
                 ({"ref": "refs/heads/main", "object": {"type": "tag", "sha": A}}, ""),
                 ({"ref": "refs/heads/main", "object": {"type": "commit", "sha": "bad"}}, ""),
                 ({"ref": "refs/heads/main", "object": {"type": "commit"}}, ""),
                 ([], ""), (None, "HTTP 404"))
        for response in cases:
            with self.subTest(response=response), mock.patch.object(gh, "pr", return_value=direct), \
                 mock.patch.object(gh, "fetch", return_value=response) as fetch:
                resolution = situation.resolve(LOOP, 184)
                self.assertEqual(resolution.status, "blocked")
                self.assertIsNone(resolution.identity)
                fetch.assert_called_once_with(LOOP, path)
        with mock.patch.object(gh, "pr", return_value=direct), mock.patch.object(
                gh, "fetch", return_value=({"ref": "refs/heads/main", "object": {"type": "commit", "sha": A.upper()}}, "")):
            self.assertEqual(situation.resolve(LOOP, 184).status, "eligible")
        # Trunk advancing past the PR's base snapshot is ordinary; it does not block the PR.
        with mock.patch.object(gh, "pr", return_value=direct), mock.patch.object(
                gh, "fetch", return_value=({"ref": "refs/heads/main", "object": {"type": "commit", "sha": B}}, "")):
            self.assertEqual(situation.resolve(LOOP, 184).status, "eligible")

    def test_stacked_root_requires_readable_live_trunk_ref(self):
        child = pr(184, "child", D, "middle", C)
        middle = pr(183, "middle", C, "parent", B)
        root = pr(182, "parent", B, "main", A)
        path = "/repos/acme/widgets/git/ref/heads/main"
        valid = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": A.upper()}}
        cases = ((valid, "", "waiting"),
                 # Trunk advanced after the root was opened: still waiting on the parent.
                 ({"ref": "refs/heads/main", "object": {"type": "commit", "sha": D}}, "", "waiting"),
                 ({"ref": "refs/heads/other", "object": {"type": "commit", "sha": A}}, "", "blocked"),
                 ({"ref": "refs/heads/main", "object": {"type": "tag", "sha": A}}, "", "blocked"),
                 ({"ref": "refs/heads/main", "object": {"type": "commit", "sha": "invalid"}}, "", "blocked"),
                 ([], "", "blocked"), (None, "HTTP 503", "blocked"))
        for ref, error, status in cases:
            with self.subTest(ref=ref, error=error), mock.patch.object(
                    gh, "pr", side_effect=lambda _loop, n: {184: child, 183: middle, 182: root}.get(n)), \
                    mock.patch.object(gh, "open_prs_read", return_value=([root, middle, child], "")), \
                    mock.patch.object(gh, "fetch", return_value=(ref, error)) as fetch:
                result = situation.resolve(LOOP, 184)
                self.assertEqual(result.status, status)
                self.assertEqual(result.identity is not None, status == "waiting")
                fetch.assert_called_once_with(LOOP, path)

    def test_direct_and_stacked_identity(self):
        direct = pr(182, "parent", B, "main", A)
        child = pr(184, "child", C, "parent", B)
        result = self.resolve(child, (direct, child))
        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.parents, (182,))
        self.assertEqual(result.identity.base_ref, "parent")
        self.assertEqual(result.identity.base_sha, B)
        self.assertEqual(result.identity.head_sha, C)
        self.assertEqual(self.resolve(direct).status, "eligible")
        advanced = pr(184, "child", C, "parent", D)
        parent_advanced = pr(182, "parent", D, "main", A)
        self.assertNotEqual(result.identity.key,
                            self.resolve(advanced, (parent_advanced, advanced)).identity.key)
        self.assertNotEqual(result.identity.key,
                            self.resolve(pr(184, "child", C, "main", A)).identity.key)

    def test_missing_ambiguous_cyclic_foreign_and_unreadable_fail_closed(self):
        child = pr(184, "child", C, "parent", B)
        parent = pr(182, "parent", B, "main", A)
        cases = [((), "missing"), ((parent, pr(183, "parent", B, "main", A)), "ambiguous"),
                 ((pr(182, "parent", B, "child", C), child), "cycle"),
                 ((pr(182, "parent", B, "main", A, repo="other/repo"),), "foreign")]
        for others, reason in cases:
            with self.subTest(reason=reason):
                result = self.resolve(child, others)
                self.assertEqual(result.status, "blocked")
                self.assertIn(reason, result.reason)
        result = self.resolve(child, open_prs=(None, "page 2 failed"))
        self.assertEqual(result.status, "blocked")
        self.assertIn("page 2 failed", result.reason)

    def test_parent_advance_unreadable_sha_and_closed_fail_closed(self):
        child = pr(184, "child", C, "parent", B)
        for parents in ((pr(182, "parent", D, "main", A),),
                        (pr(182, "parent", "", "main", A),),
                        (pr(182, "parent", B, "main", A, state="closed"),)):
            with self.subTest(parents=parents):
                self.assertEqual(self.resolve(child, parents).status, "blocked")

    def test_parent_readback_detects_change_after_listing(self):
        child = pr(184, "child", C, "parent", B)
        listed = pr(182, "parent", B, "main", A)
        changed = pr(182, "parent", D, "main", A)
        with mock.patch.object(situation.gh, "open_prs_read", return_value=([listed], "")), \
             mock.patch.object(situation.gh, "pr", side_effect=[child, changed]):
            self.assertEqual(situation.resolve(LOOP, 184).status, "blocked")

    def test_paginated_open_prs_requires_complete_valid_response(self):
        page = [{} for _ in range(100)]
        with mock.patch.object(gh, "fetch", side_effect=[(page, ""), ([{"number": 182}], "")]) as fetch:
            result, error = gh.open_prs_read(LOOP)
            self.assertIsNotNone(result)
            self.assertEqual((len(result or []), error), (101, ""))
            self.assertIn("page=2", fetch.call_args_list[-1].args[1])
        with mock.patch.object(gh, "fetch", side_effect=[(page, ""), (None, "timeout")]):
            result, error = gh.open_prs_read(LOOP)
            self.assertIsNone(result)
            self.assertIn("page 2", error)


if __name__ == "__main__":
    unittest.main()
