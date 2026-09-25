"""Stacked visibility is not a reviewer-seat authorization."""
import importlib.util
import contextlib
import io
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from review_loop import gate, situation, transition
from review_loop.state import LoopState
from tests.test_stacked_situation import A, B, C, D, pr

WATCHDOG_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
spec = importlib.util.spec_from_file_location("stack_watchdog", WATCHDOG_PATH)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)
REVIEWER_PATH = WATCHDOG_PATH.with_name("gate_reviewer.py")
reviewer_spec = importlib.util.spec_from_file_location("stack_reviewer", REVIEWER_PATH)
reviewer = importlib.util.module_from_spec(reviewer_spec)
reviewer_spec.loader.exec_module(reviewer)
FIXER_PATH = WATCHDOG_PATH.with_name("gate_fixer.py")
fixer_spec = importlib.util.spec_from_file_location("stack_fixer", FIXER_PATH)
fixer = importlib.util.module_from_spec(fixer_spec)
fixer_spec.loader.exec_module(fixer)


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stack-fixture-", dir=str(pathlib.Path.home()))
        self.addCleanup(self.temp.cleanup)
        self.loop = {"id": "stack-fixture", "repo": "acme/widgets", "base": "main",
                     "fixers": ["fixer"], "state_dir": self.temp.name, "cap": 3,
                     "ttl_min": 60, "grace_min": 5, "marker_grace_min": 5,
                     "cooldown_h": 1, "reviewer_seat": "reviewer", "inflight_ttl_min": 60}
        self.st = LoopState(self.loop)
        # A retarget now enqueues a fresh isolated reviewer turn: keep its host ledger and
        # runtime lookup inside this fixture, never under the operator's ~/.hermes.
        env = mock.patch.dict("os.environ", {"HERMES_HOME": str(pathlib.Path(self.temp.name) / "hermes")})
        env.start()
        self.addCleanup(env.stop)
        self.parent = pr(182, "parent", B, "main", A)
        self.child = pr(184, "child", C, "parent", B)
        self.parent["user"] = self.child["user"] = {"login": "fixer"}
        self.parent["created_at"] = self.child["created_at"] = "2020-01-01T00:00:00Z"
        self.notices = []
        self.posts = []

    def sweep(self, listing, baseline=([], "")):
        by_number = {p["number"]: p for p in listing} if isinstance(listing, list) else {}
        with mock.patch.object(watchdog, "TEST", True), mock.patch.object(watchdog.gh, "open_prs", return_value=listing), \
             mock.patch.object(watchdog.gh, "pr", side_effect=lambda _loop, n: by_number.get(n)), \
             mock.patch.object(watchdog.gh, "reviews", return_value=[]), \
             mock.patch.object(watchdog.gh, "reviews_read", return_value=baseline), \
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

    def test_stale_watchdog_snapshot_cannot_erase_transition_hold(self):
        self.sweep([self.parent, self.child])
        stale = self.st.watch()
        self.child["base"] = {"ref": "main", "sha": A, "repo": {"full_name": "acme/widgets"}}
        with mock.patch.object(gate.gh, "reviews_read", return_value=([], "")):
            transition.record(self.loop, self.st, 184, C, "main")
        self.st.watch_save(stale)
        self.assertIsNotNone(transition.hold(self.st, 184, C))

    def test_transition_queue_cleanup_keeps_new_head_request(self):
        self.sweep([self.parent, self.child])
        self.st.queue_add("reviewer", "acme/widgets#184", D, "url", "new head")
        with mock.patch.object(gate.gh, "reviews_read", return_value=([], "")):
            transition.record(self.loop, self.st, 184, C, "main")
        self.assertEqual(self.st.queue_items("reviewer")["acme/widgets#184"]["head"], D)

    def test_only_post_boundary_human_review_survives_quarantine(self):
        entry = {"at": time.time() - 5, "old_review_ids": [7]}
        old = {"id": 7, "submitted_at": "2099-01-01T00:00:00Z", "user": {"login": "human", "type": "User"}}
        bot = {"id": 8, "submitted_at": "2099-01-01T00:00:00Z", "user": {"login": "reviewer", "type": "User"}}
        human = {"id": 9, "submitted_at": "2099-01-01T00:00:00Z", "user": {"login": "human", "type": "User"}}
        self.assertEqual(transition.current_reviews([old, bot, human], entry, self.loop), [human])
        entry["old_review_ids"] = None
        self.assertEqual(transition.current_reviews([human], entry, self.loop), [])

    def test_draft_same_head_retarget_quarantines_before_skipping_work(self):
        # GitHub lists an observed stacked child as a draft after its base is
        # edited. The individual PR read agrees; no edited webhook is needed.
        self.sweep([self.parent, self.child])
        key = "acme/widgets#184"
        self.assertEqual(self.st.watch()["stacked_wait"]["184"]["head"], C)
        for seat in ("reviewer", "fixer"):
            self.st.queue_add(seat, key, C, "url", "old stacked generation")
        self.st.inflight(f"review:184:{C}", record=True)
        self.st.inflight(f"fix:184:{C}", record=True)
        self.st.breach_set(184, {"head": C, "status": "delivery-pending"})
        self.child["draft"] = True
        self.child["base"].update(ref="main", sha=A)
        old_review = self.old_review(review_id=71)
        lines = self.sweep([self.parent, self.child], baseline=([old_review], ""))
        hold = transition.hold(self.st, 184, C)
        self.assertIsNotNone(hold, lines)
        self.assertEqual(hold["old_review_ids"], [71])
        self.assertNotIn("184", self.st.watch().get("stacked_wait", {}))
        self.assertNotIn(key, self.st.queue_items("reviewer"))
        self.assertNotIn(key, self.st.queue_items("fixer"))
        self.assertFalse(self.st.inflight(f"review:184:{C}"))
        self.assertFalse(self.st.inflight(f"fix:184:{C}"))
        self.assertEqual(self.st.breach_get(184), {})
        self.assertEqual(self.posts, [], "draft retarget must not start an agent")
        self.child["draft"] = False
        self.sweep([self.parent, self.child])
        self.assertIsNotNone(transition.hold(self.st, 184, C))
        self.assertEqual(self.posts, [])

    def test_draft_first_then_retarget_uses_retained_stacked_head(self):
        self.sweep([self.parent, self.child])
        self.child["draft"] = True
        self.sweep([self.parent, self.child])
        self.assertNotIn("184", self.st.watch().get("stacked_wait", {}))
        self.assertEqual(self.st.watch()["heads"]["184"]["base"], "parent")
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])
        self.assertIsNotNone(transition.hold(self.st, 184, C))
        self.assertEqual(self.posts, [])

    def test_unobserved_stacked_push_then_retarget_quarantines_new_head(self):
        self.sweep([self.parent, self.child])
        self.child["head"]["sha"] = D  # no observer saw this stacked head
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])
        self.assertIsNotNone(transition.hold(self.st, 184, D))
        self.assertEqual(self.posts, [])

    def test_other_bot_is_not_a_human_resolution(self):
        entry = {"at": time.time() - 5, "old_review_ids": []}
        review = {"id": 9, "submitted_at": "2099-01-01T00:00:00Z",
                  "user": {"login": "external[bot]", "type": "Bot"}}
        self.assertEqual(transition.current_reviews([review], entry, self.loop), [])

    def test_post_boundary_approval_on_wrong_head_never_announces_merge(self):
        self.sweep([self.parent, self.child])
        self.child["base"].update(ref="main", sha=A)
        with mock.patch.object(gate.gh, "reviews_read", return_value=([], "")):
            self.sweep([self.parent, self.child])
        self.loop.update(reviewers=["vex", "human"], reviewer_seat="vex")
        approval = self.old_review(review_id=29)
        approval["user"].update(login="human", type="User")
        approval["submitted_at"] = "2099-01-01T00:00:00Z"
        approval["commit_id"] = B
        self.assertEqual(self.st.transition_get(184).get("old_review_ids"), [])
        self.assertEqual(transition.current_reviews([approval], self.st.transition_get(184), self.loop), [approval])
        payload = {"action": "submitted", "number": 184, "pull_request": self.child,
                   "review": approval, "sender": {"login": "vex"}}
        notices = []
        with mock.patch.object(fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(fixer.gate, "context", return_value=(self.loop, self.st)), \
             mock.patch.object(fixer.gh, "pr", return_value=self.child), \
             mock.patch.object(fixer.gh, "reviews_read", return_value=([approval], "")), \
             mock.patch.object(fixer.observer, "notify", side_effect=lambda *a, **kw: notices.append(kw)):
            with self.assertRaises(SystemExit):
                fixer.main()
        self.assertFalse(any(n.get("next_turn") == "you merge" for n in notices), notices)
        # Even the current head is not enough without a generation-bound receipt.
        approval["commit_id"] = C
        payload["review"] = approval
        notices.clear()
        with mock.patch.object(fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(fixer.gate, "context", return_value=(self.loop, self.st)), \
             mock.patch.object(fixer.gh, "pr", return_value=self.child), \
             mock.patch.object(fixer.observer, "notify", side_effect=lambda *a, **kw: notices.append(kw)):
            with self.assertRaises(SystemExit):
                fixer.main()
        self.assertFalse(any(n.get("next_turn") == "you merge" for n in notices), notices)

    def test_approval_live_retarget_same_head_cannot_announce_merge(self):
        self.loop.update(reviewers=["vex"], reviewer_seat="vex")
        direct = pr(184, "child", C, "main", A)
        direct.update(state="open", draft=False, user={"login": "fixer"})
        stacked = pr(184, "child", C, "parent", B)
        stacked.update(state="open", draft=False, user={"login": "fixer"})
        approval = {"id": 29, "state": "APPROVED", "commit_id": C,
                    "submitted_at": "2024-01-01T00:00:00Z", "user": {"login": "vex"}}
        payload = {"action": "submitted", "number": 184, "pull_request": direct,
                   "review": approval}
        notices = []
        with mock.patch.object(fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(fixer.gate, "context", return_value=(self.loop, self.st)), \
             mock.patch.object(fixer.gh, "pr", return_value=stacked) as reads, \
             mock.patch.object(fixer.gh, "reviews", return_value=[approval]), \
             mock.patch.object(fixer.gate, "drain_seat"), \
             mock.patch.object(fixer.observer, "notify", side_effect=lambda *a, **kw: notices.append(kw)):
            with self.assertRaises(SystemExit):
                fixer.main()
        self.assertEqual(reads.call_count, 1)
        self.assertFalse(any(n.get("next_turn") == "you merge" for n in notices), notices)

    def test_approval_live_base_advance_same_head_cannot_announce_merge(self):
        self.loop.update(reviewers=["vex"], reviewer_seat="vex")
        direct = pr(184, "child", C, "main", A)
        direct.update(state="open", draft=False, user={"login": "fixer"})
        advanced = pr(184, "child", C, "main", B)
        advanced.update(state="open", draft=False, user={"login": "fixer"})
        approval = {"id": 29, "state": "APPROVED", "commit_id": C,
                    "submitted_at": "2024-01-01T00:00:00Z", "user": {"login": "vex"}}
        payload = {"action": "submitted", "number": 184, "pull_request": direct,
                   "review": approval}
        notices = []
        with mock.patch.object(fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(fixer.gate, "context", return_value=(self.loop, self.st)), \
             mock.patch.object(fixer.gh, "pr", return_value=advanced) as reads, \
             mock.patch.object(fixer.gh, "reviews", return_value=[approval]), \
             mock.patch.object(fixer.gate, "drain_seat"), \
             mock.patch.object(fixer.observer, "notify", side_effect=lambda *a, **kw: notices.append(kw)):
            with self.assertRaises(SystemExit):
                fixer.main()
        self.assertEqual(reads.call_count, 1)
        self.assertFalse(any(n.get("next_turn") == "you merge" for n in notices), notices)

    def test_new_request_on_held_head_is_not_its_own_receipt(self):
        self.sweep([self.parent, self.child])
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])
        self.child["requested_reviewers"] = [{"login": "vex"}]
        output, seat = self.human_request(self.child, [])
        # The request is not a receipt and never a second fresh turn: it can only re-drive
        # the transition's own reviewer turn, which the ledger dedups on its turn key.
        self.assert_only_fresh_turn(seat, output)
        self.assertIsNotNone(transition.hold(self.st, 184, C))

    def assert_only_fresh_turn(self, seat, output=""):
        self.assertEqual(seat.call_count, 1, output)
        args, kwargs = seat.call_args
        self.assertEqual(args[2:5], ("reviewer", 184, C))
        self.assertEqual(kwargs["turn_key"], transition.turn_key(transition.hold(self.st, 184, C)))

    def human_request(self, snapshot, reviews):
        """Exercise the real reviewer route with a fake, strict GitHub read."""
        self.loop.update(reviewers=["vex"], reviewer_seat="vex",
                         seats={"reviewer": {"route": "review"}, "fixer": {"route": "fix"}})
        payload = {"action": "review_requested", "number": 184,
                   "repository": {"full_name": self.loop["repo"]},
                   "sender": {"login": "fixer"},
                   "requested_reviewer": {"login": "vex"},
                   "pull_request": snapshot}
        output = io.StringIO()
        with mock.patch.object(reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(output), \
             mock.patch.object(reviewer.gate, "context", return_value=(self.loop, self.st)), \
             mock.patch.object(reviewer.gh, "pr", return_value=self.child), \
             mock.patch.object(reviewer.gh, "reviews", return_value=reviews), \
             mock.patch.object(reviewer.gate, "block_pr_agent", return_value=None) as seat, \
             mock.patch.object(reviewer.gate, "loop_block", return_value={"pr": 184}), \
             mock.patch.object(reviewer.gate, "drain_seat"), \
             mock.patch.object(reviewer.gate, "start_text", return_value="fixture start"), \
             mock.patch.object(reviewer.gate, "ping_start"), \
             mock.patch.object(reviewer.observer, "notify"):
            try:
                reviewer.main()
            except SystemExit:
                pass  # silence() terminates the route after printing [SILENT]
        return output.getvalue(), seat

    def old_review(self, state="APPROVED", review_id=17):
        return {"id": review_id, "user": {"login": "vex"}, "state": state,
                "commit_id": C, "submitted_at": "2020-01-02T00:00:00Z"}

    def test_same_sha_retarget_does_not_replay_review_request_or_old_verdict(self):
        self.sweep([self.parent, self.child])
        self.st.queue_add("reviewer", "acme/widgets#184", C, "url", "old stacked request")
        old_snapshot = json.loads(json.dumps(self.child))
        self.child["base"].update(ref="main", sha=A)
        self.assertFalse(any("#184 stacked" in line for line in self.sweep([self.parent, self.child])))
        self.assertNotIn("acme/widgets#184", self.st.queue_items("reviewer"))
        self.assertEqual(self.posts, [], "a retarget/edited observation cannot wake a reviewer")
        # An unreceipted verdict at the held head counts for nothing: the request re-drives
        # only the transition's own fresh turn.
        output, seat = self.human_request(self.child, [self.old_review()])
        self.assert_only_fresh_turn(seat, output)
        self.assertEqual(self.posts, [])
        # A delayed request from the parent-base generation is not that fresh request.
        output, seat = self.human_request(old_snapshot, [self.old_review()])
        seat.assert_not_called()
        self.assertIn("[SILENT]", output)

    def test_parent_push_then_retarget_same_head_does_not_reuse_parent_generation(self):
        self.sweep([self.parent, self.child])
        before = self.st.watch()["stacked_wait"]["184"]["generation"]
        self.parent["head"]["sha"] = D
        lines = self.sweep([self.parent, self.child])
        self.assertTrue(any("#184 stacked blocked" in line for line in lines), lines)
        self.assertNotEqual(before, self.st.watch()["stacked_wait"]["184"]["generation"])
        self.assertEqual(self.posts, [])
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])
        output, seat = self.human_request(self.child, [self.old_review("CHANGES_REQUESTED")])
        self.assert_only_fresh_turn(seat, output)  # never a fixer order from the old verdict
        self.assertEqual(self.st.queue_all(), {})

    def test_new_head_after_retarget_is_a_new_human_turn_not_an_auto_wake(self):
        self.sweep([self.parent, self.child])
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])  # quarantine the observed C transition
        self.child["head"]["sha"] = D  # only a subsequent trunk push clears it
        self.sweep([self.parent, self.child])
        self.assertEqual(self.posts, [])
        output, seat = self.human_request(self.child, [self.old_review()])
        self.assertEqual(seat.call_count, 1, output)

    def test_same_sha_retarget_request_only_drives_the_fresh_turn(self):
        self.sweep([self.parent, self.child])
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])
        output, seat = self.human_request(self.child, [])
        self.assert_only_fresh_turn(seat, output)  # a request is not a verdict or a receipt
        self.assertEqual(self.posts, [])

    def test_same_sha_retarget_with_unreadable_baseline_never_wakes_reviewer(self):
        self.sweep([self.parent, self.child])
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child], baseline=(None, "HTTP 502"))
        self.assertTrue(transition.baseline_missing(transition.hold(self.st, 184, C)))
        output, seat = self.human_request(self.child, [])
        seat.assert_not_called()
        self.assertIn("[SILENT]", output)
        self.assertEqual(self.posts, [])

    def test_missed_edited_event_sweep_recovery_is_idempotent_and_read_failure_retryable(self):
        self.sweep([self.parent, self.child])
        self.st.queue_add("reviewer", "acme/widgets#184", C, "url", "stale")
        self.child["base"].update(ref="main", sha=A)  # webhook never delivered
        before = self.st.watch()
        self.assertIn("could not list", " ".join(self.sweep(None)))
        self.assertEqual(self.st.watch(), before)
        self.assertIn("acme/widgets#184", self.st.queue_items("reviewer"))
        self.sweep([self.parent, self.child])
        self.assertNotIn("184", self.st.watch().get("stacked_wait", {}))
        self.assertNotIn("acme/widgets#184", self.st.queue_items("reviewer"))
        first_notices = [kw.get("identity") for _, kw in self.notices
                         if str(kw.get("identity", "")).startswith("stacked:")]
        first_history = self.st.watch()["head_history"]
        self.sweep([self.parent, self.child])
        self.assertEqual([kw.get("identity") for _, kw in self.notices
                          if str(kw.get("identity", "")).startswith("stacked:")], first_notices)
        self.assertEqual(self.st.watch()["head_history"], first_history)
        self.assertEqual(self.posts, [])

    def test_old_verdict_and_stale_queue_cannot_start_fixer_after_retarget(self):
        self.sweep([self.parent, self.child])
        self.st.queue_add("fixer", "acme/widgets#184", C, "url", "old changes")
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])
        self.assertNotIn("acme/widgets#184", self.st.queue_items("fixer"))
        self.assertEqual(self.posts, [])

    def test_old_approval_after_retarget_never_announces_merge(self):
        self.sweep([self.parent, self.child])
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.parent, self.child])
        self.loop.update(reviewers=["vex"], reviewer_seat="vex",
                         seats={"reviewer": {"route": "review"}, "fixer": {"route": "fix"}})
        approval = self.old_review()
        payload = {"action": "submitted", "number": 184, "pull_request": self.child,
                   "review": approval, "sender": {"login": "vex"}}
        notices = []
        with mock.patch.object(fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(fixer.gate, "context", return_value=(self.loop, self.st)), \
             mock.patch.object(fixer.gh, "pr", return_value=self.child), \
             mock.patch.object(fixer.gh, "reviews", return_value=[approval]), \
             mock.patch.object(fixer.gate, "drain_seat"), \
             mock.patch.object(fixer.observer, "notify", side_effect=lambda *a, **kw: notices.append(kw)):
            try:
                fixer.main()
            except SystemExit:
                pass
        self.assertFalse(any(n.get("next_turn") == "you merge" for n in notices), notices)
        self.assertFalse(any(n.get("next_turn") == "fixer" for n in notices), notices)


if __name__ == "__main__":
    unittest.main()
