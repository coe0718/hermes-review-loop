"""Stacked visibility is not a reviewer-seat authorization."""
import importlib.util
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from review_loop import gate, situation
from review_loop.state import LoopState
from tests.test_stacked_situation import A, B, C, D, pr

WATCHDOG_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
spec = importlib.util.spec_from_file_location("stack_watchdog", WATCHDOG_PATH)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stack-fixture-", dir=str(pathlib.Path.home()))
        self.addCleanup(self.temp.cleanup)
        self.loop = {"id": "stack-fixture", "repo": "acme/widgets", "base": "main",
                     "fixers": ["fixer"], "state_dir": self.temp.name, "cap": 3,
                     "ttl_min": 60, "grace_min": 5, "marker_grace_min": 5,
                     "cooldown_h": 1, "reviewer_seat": "reviewer", "inflight_ttl_min": 60}
        self.st = LoopState(self.loop)
        self.parent = pr(182, "parent", B, "main", A)
        self.child = pr(184, "child", C, "parent", B)
        self.parent["user"] = self.child["user"] = {"login": "fixer"}
        self.parent["created_at"] = self.child["created_at"] = "2020-01-01T00:00:00Z"
        self.notices = []
        self.posts = []

    def sweep(self, listing):
        by_number = {p["number"]: p for p in listing} if isinstance(listing, list) else {}
        with mock.patch.object(watchdog, "TEST", True), mock.patch.object(watchdog.gh, "open_prs", return_value=listing), \
             mock.patch.object(watchdog.gh, "pr", side_effect=lambda _loop, n: by_number.get(n)), \
             mock.patch.object(watchdog.gh, "reviews", return_value=[]), \
             mock.patch.object(watchdog.gh, "fetch", return_value=(
                 {"ref": "refs/heads/main", "object": {"type": "commit",
                  "sha": next((p["base"]["sha"] for p in listing
                               if p["head"]["ref"] == "grand"), A) if isinstance(listing, list) else A}}, "")), \
             mock.patch.object(watchdog.observer, "notify", side_effect=lambda *a, **kw: self.notices.append((a, kw))), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"), \
             mock.patch.object(watchdog.routes, "fire", side_effect=lambda *a, **kw: self.posts.append((a, kw))):
            return watchdog.sweep_loop(self.loop, self.st)

    def test_wait_parent_advance_retarget_and_listing_failure(self):
        listing = [self.parent, self.child]
        first = self.sweep(listing)
        self.assertTrue(any("#184 stacked waiting" in line for line in first))
        old = self.st.watch()["stacked_wait"]["184"]
        self.assertEqual(old["parents"], [182])
        self.assertEqual(sum(str(kw.get("identity", "")).startswith("stacked:")
                             for _, kw in self.notices), 1)
        self.assertFalse(any("#184 stacked" in line for line in self.sweep(listing)))
        self.assertEqual(sum(str(kw.get("identity", "")).startswith("stacked:")
                             for _, kw in self.notices), 1)
        self.assertEqual(self.st.queue_all(), {})
        self.assertEqual(self.posts, [])
        self.parent["head"]["sha"] = D
        self.assertTrue(any("#184 stacked blocked" in line for line in self.sweep(listing)))
        self.assertNotEqual(old["generation"], self.st.watch()["stacked_wait"]["184"]["generation"])
        self.child["base"]["sha"] = D
        self.assertTrue(any("#184 stacked waiting" in line for line in self.sweep(listing)))
        newer = self.st.watch()["stacked_wait"]["184"]
        self.assertNotEqual(old["generation"], newer["generation"])
        self.assertEqual(sum(str(kw.get("identity", "")).startswith("stacked:")
                             for _, kw in self.notices), 3)
        self.parent["head"]["sha"] = B  # child still anchored to D: parent moved independently
        self.assertTrue(any("blocked" in line for line in self.sweep(listing)))
        self.assertEqual(self.st.watch()["stacked_wait"]["184"]["status"], "blocked")
        saved = self.st.watch()
        self.assertIn("could not list", " ".join(self.sweep(None)))
        self.assertEqual(self.st.watch(), saved)
        self.child["base"]["ref"] = "main"  # edited base, same head SHA
        self.child["base"]["sha"] = A
        self.assertFalse(any("#184 stacked" in line for line in self.sweep(listing)))
        after = self.st.watch()
        self.assertNotIn("184", after["stacked_wait"])
        self.assertIn("184", after["heads"])
        self.assertTrue(any(k.startswith("184:") for k in after["head_history"]), after)
        self.assertEqual(self.posts, [])

    def test_missing_ambiguous_closed_and_stale_queue(self):
        self.sweep([self.parent, self.child])
        for listing, reason in (([self.child], "missing"),
                                ([self.child, self.parent, dict(self.parent, number=183)], "ambiguous"),
                                ([self.child, dict(self.parent, state="closed")], "closed")):
            self.assertIn(reason, " ".join(self.sweep(listing)))
            self.assertEqual(self.st.watch()["stacked_wait"]["184"]["status"], "blocked")
        self.st.queue_add("reviewer", "acme/widgets#184", C, "url", "old request")
        self.child["base"]["ref"] = "main"
        self.child["base"]["sha"] = A
        with mock.patch.object(watchdog.gh, "pr", return_value=self.child), \
             mock.patch.object(watchdog.routes, "fire", side_effect=AssertionError("unauthorized wake")):
            self.assertEqual(watchdog.drain(self.loop, self.st, "reviewer", quiet=True), 0)
        self.assertNotIn("acme/widgets#184", self.st.queue_items("reviewer"))
        self.st.queue_add("reviewer", "acme/widgets#184", C, "url", "old request")
        self.sweep([self.parent, self.child])
        self.assertNotIn("acme/widgets#184", self.st.queue_items("reviewer"))
        self.assertEqual(self.posts, [])

    def test_grandparent_advance_changes_generation_without_child_push(self):
        grandparent = pr(180, "grand", A, "main", D)
        grandparent["user"] = {"login": "fixer"}
        grandparent["created_at"] = "2020-01-01T00:00:00Z"
        self.parent["base"]["ref"] = "grand"
        self.parent["base"]["sha"] = A
        listing = [grandparent, self.parent, self.child]
        self.sweep(listing)
        before = self.st.watch()["stacked_wait"]["184"]["generation"]
        grandparent["head"]["sha"] = D
        lines = self.sweep(listing)
        self.assertTrue(any("#184 stacked blocked" in line for line in lines))
        self.assertNotEqual(before, self.st.watch()["stacked_wait"]["184"]["generation"])
        self.assertEqual(self.posts, [])

    def test_explain_wait_is_read_only(self):
        self.sweep([self.parent, self.child])
        self.loop["seats"] = {"reviewer": {"concurrency": 1}, "fixer": {"concurrency": 1}}
        resolution = situation.Resolution("waiting", "waiting on #182",
                                          situation.Identity(C, "parent", B, ((182, "parent", B, A),)), (182,))
        facts = {"pr": self.child, "reviews": [], "armed": True,
                 "read_at": time.time(), "chain": resolution}
        before = {p.name: p.read_bytes() for p in pathlib.Path(self.temp.name).iterdir()}
        with mock.patch.object(gate, "seat_capacity", return_value=(0, 1)):
            report = gate.explain(self.loop, self.st, 184, facts)
        self.assertEqual(report["next"]["kind"], "wait")
        self.assertIn("#182", report["chain"]["reason"])
        self.assertIn("visibility queue", report["queue"])
        self.assertIn("no automatic reviewer", report["next"]["action"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in pathlib.Path(self.temp.name).iterdir()})


if __name__ == "__main__":
    unittest.main()
