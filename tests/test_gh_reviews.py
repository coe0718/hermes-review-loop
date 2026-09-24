"""Review enumeration cannot silently turn a partial history into a verdict."""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import gate, gh

HEAD = "a" * 40
LOOP = {"repo": "acme/widgets", "reviewers": ["reviewer"]}
PATH = "/repos/acme/widgets/pulls/7/reviews?per_page=100"


def verdict(rid: int, state: str, submitted: str = "2026-01-01T00:00:00Z") -> dict:
    return {"id": rid, "user": {"login": "reviewer"}, "state": state,
            "commit_id": HEAD, "submitted_at": submitted}


class ReviewPaginationTest(unittest.TestCase):
    def test_later_page_overturns_first_page_approval(self):
        first = [verdict(i, "COMMENTED") for i in range(1, 100)] + [
            verdict(100, "APPROVED")]
        later = [verdict(101, "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")]
        with mock.patch.object(gh, "fetch", side_effect=[(first, ""), (later, "")]) as fetch:
            reviews = gh.reviews(LOOP, 7)
        self.assertEqual(fetch.call_args_list, [mock.call(LOOP, PATH),
                                                mock.call(LOOP, PATH + "&page=2")])
        self.assertIsInstance(reviews, list)
        assert reviews is not None
        self.assertEqual(len(reviews), 101)
        latest = gate.latest_effective_review_at_head(reviews, LOOP, HEAD)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["state"], "CHANGES_REQUESTED")

    def test_exactly_full_page_probes_next_page_even_when_empty(self):
        first = [verdict(i, "COMMENTED") for i in range(1, 101)]
        with mock.patch.object(gh, "fetch", side_effect=[(first, ""), ([], "")]) as fetch:
            self.assertEqual(gh.reviews_read(LOOP, 7), (first, ""))
        self.assertEqual(fetch.call_count, 2)

    def test_second_page_failure_discards_earlier_approval(self):
        first = [verdict(i, "COMMENTED") for i in range(1, 100)] + [
            verdict(100, "APPROVED")]
        for payload, error in ((None, "HTTP 500"), (None, ""),
                               ({"message": "rate limited"}, ""), ([None], "")):
            with self.subTest(payload=payload, error=error):
                with mock.patch.object(gh, "fetch", side_effect=[(first, ""),
                                                                 (payload, error)]):
                    result, reason = gh.reviews_read(LOOP, 7)
                self.assertIsNone(result)
                self.assertIn("page 2", reason)

    def test_oversized_page_and_unbounded_full_pages_fail_closed(self):
        first = [verdict(i, "COMMENTED") for i in range(101)]
        with mock.patch.object(gh, "fetch", return_value=(first, "")):
            self.assertIsNone(gh.reviews(LOOP, 7))
        with (mock.patch.object(gh, "MAX_REVIEW_PAGES", 2),
              mock.patch.object(gh, "fetch", return_value=(first[:100], "")) as fetch):
            result, reason = gh.reviews_read(LOOP, 7)
        self.assertIsNone(result)
        self.assertIn("exceeds", reason)
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
