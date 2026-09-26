"""#23 acceptance 4: a parent merge + same-head retarget starts a FRESH review situation.

Exactly one isolated reviewer turn is enqueued per transition; afterwards only a review with a
host receipt from an isolated reviewer run pinned to this head on the root base decides
anything. Old reviews, approvals and rounds never carry over; an unreceipted post-boundary
review stays diagnostic; a missing baseline holds forever.

No GitHub, model, Hermes or ~/.hermes: GitHub is mocked, and HERMES_HOME (the run ledger) and
the loop state live in a private temporary directory.
"""
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest import mock

from review_loop import gate, state as state_mod, transition
from review_loop.run_supervisor import Supervisor
from tests.test_stacked_reconciliation import fixer, reviewer, watchdog
from tests.test_stacked_situation import A, B, C, D, pr

REPO = "acme/widgets"
PRINCIPAL = 11


def review(review_id, state, *, head=C, minute=0, login="vex", user_id=PRINCIPAL):
    return {"id": review_id, "state": state, "commit_id": head, "body": f"review {review_id}",
            "submitted_at": f"2099-01-01T00:{minute:02d}:00Z",
            "user": {"login": login, "id": user_id, "type": "User"}}


class FreshReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fresh-review-")
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(root / "hermes")})
        env.start()
        self.addCleanup(env.stop)
        self.loop = {"id": "fresh", "repo": REPO, "base": "main", "fixers": ["fixer"],
                     "unattended_fixer_push": True,
                     "reviewers": ["vex"], "reviewer_seat": "vex", "state_dir": str(root / "state"),
                     "cap": 3, "ttl_min": 60, "grace_min": 5, "marker_grace_min": 5,
                     "cooldown_h": 1, "inflight_ttl_min": 60, "read_token": "read",
                     "seats": {"reviewer": {"route": "review", "concurrency": 1},
                               "fixer": {"route": "fix", "concurrency": 1}}}
        self.st = state_mod.LoopState(self.loop)
        self.db = transition.ledger_path()
        self.ledger = Supervisor(self.db)  # schema only: never spawns a worker
        self.parent = pr(182, "parent", B, "main", A)
        self.child = pr(184, "child", C, "parent", B)
        for p in (self.parent, self.child):
            p.update(user={"login": "fixer"}, draft=False, created_at="2020-01-01T00:00:00Z")
        # Two pre-boundary rejections and a pre-boundary approval, all at the same head.
        self.old = [review(17, "CHANGES_REQUESTED", minute=1), review(18, "CHANGES_REQUESTED", minute=2),
                    review(19, "APPROVED", minute=3)]
        self.reviews = list(self.old)
        self.baseline_error = ""
        self.enqueued = []
        self.enqueue_error = None

    # -- fixtures ------------------------------------------------------------------------------

    def fake_enqueue(self, loop, seat, number, head, *, turn_key=""):
        """The real ledger insert and its unique index, without arming a production worker."""
        self.enqueued.append((seat, number, head, turn_key))
        if self.enqueue_error:
            raise self.enqueue_error
        delivery = f"{loop['repo']}:{number}:{head}:{seat}" + (f":{turn_key}" if turn_key else "")
        self.ledger.enqueue(delivery, loop["repo"], number, head, seat, turn_key=turn_key)

    def ledger_rows(self, seat="reviewer"):
        with sqlite3.connect(self.db) as con:
            return con.execute("SELECT head, turn_key FROM runs WHERE seat=? AND pr=184",
                               (seat,)).fetchall()

    def receipt(self, review_id, verdict, *, head=C, base="main", seat="reviewer",
                principal=PRINCIPAL):
        """A confirmed host receipt exactly as the broker leaves it after an isolated review."""
        generation = json.dumps({"head": head, "base_ref": base, "base_sha": A, "parents": [],
                                 "parent_chain_verified": False}, sort_keys=True, separators=(",", ":"))
        run_id = uuid.uuid4().hex
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,created,updated,"
                        "generation) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, run_id, REPO, 184, head, seat, f"t:{run_id}", "completed",
                         time.time(), time.time(), generation))
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,review_id,"
                        "verdict,created,confirmed) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, "confirmed", generation, principal, review_id, verdict,
                         time.time(), time.time()))

    def live(self, number):
        return {182: self.parent, 184: self.child}.get(number)

    def sweep(self, listing):
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(watchdog.gh, "open_prs", return_value=listing), \
             mock.patch.object(watchdog.gh, "pr", side_effect=lambda _loop, n: self.live(n)), \
             mock.patch.object(watchdog.gh, "reviews", side_effect=lambda *_: list(self.reviews)), \
             mock.patch.object(watchdog.gh, "reviews_read", side_effect=lambda *_: (
                 (None, self.baseline_error) if self.baseline_error else (list(self.reviews), ""))), \
             mock.patch.object(watchdog.gh, "fetch", return_value=(
                 {"ref": "refs/heads/main", "object": {"type": "commit", "sha": A}}, "")), \
             mock.patch.object(gate, "enqueue_isolated", side_effect=self.fake_enqueue), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"), \
             mock.patch.object(watchdog.routes, "fire", side_effect=AssertionError("route wake")):
            return watchdog.sweep_loop(self.loop, self.st)

    def merge_parent_and_retarget(self):
        """Observe the stacked child, then GitHub merges #182 and retargets #184 at head C."""
        self.sweep([self.parent, self.child])
        self.assertEqual(self.enqueued, [], "no unattended review while stacked")
        self.parent.update(state="closed", merged=True)
        self.child["base"].update(ref="main", sha=A)
        return self.sweep([self.child])

    def run_gate(self, module, payload, **patches):
        output = io.StringIO()
        calls = {"block": [], "breach": [], "notify": []}

        def block(*args, **kwargs):
            calls["block"].append((args, kwargs))
            if kwargs.get("on_queued"):
                kwargs["on_queued"]()

        # silence() logs its reason to stderr and prints only [SILENT]; keep both.
        with mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
             mock.patch.object(module.gate, "context", return_value=(self.loop, self.st)), \
             mock.patch.object(module.gh, "pr", side_effect=lambda _loop, n: self.live(n)), \
             mock.patch.object(module.gh, "reviews", side_effect=lambda *_: list(self.reviews)), \
             mock.patch.object(module.gh, "reviews_read", side_effect=lambda *_: (list(self.reviews), "")), \
             mock.patch.object(gate, "enqueue_isolated", side_effect=self.fake_enqueue), \
             mock.patch.object(module.gate, "block_pr_agent", side_effect=block), \
             mock.patch.object(module.gate, "breach",
                               side_effect=lambda *a, **kw: calls["breach"].append(a)), \
             mock.patch.object(module.gate, "drain_seat"), \
             mock.patch.object(module.observer, "verified_base_sha", return_value=A), \
             mock.patch.object(module.observer, "notify",
                               side_effect=lambda *a, **kw: calls["notify"].append(kw)):
            try:
                module.main()
            except SystemExit:
                pass
        return output.getvalue(), calls

    def verdict_event(self, review_body):
        return {"action": "submitted", "number": 184, "repository": {"full_name": REPO},
                "pull_request": self.child, "review": review_body, "sender": {"login": "vex"}}

    def request_event(self):
        return {"action": "review_requested", "number": 184, "repository": {"full_name": REPO},
                "sender": {"login": "fixer"}, "requested_reviewer": {"login": "vex"},
                "pull_request": self.child}

    def merge_cues(self, calls):
        return [n for n in calls["notify"] if n.get("next_turn") == "you merge"]

    # -- one fresh reviewer turn per transition --------------------------------------------------

    def test_sweep_enqueues_exactly_one_fresh_reviewer_turn(self):
        lines = self.merge_parent_and_retarget()
        entry = transition.hold(self.st, 184, C)
        key = transition.turn_key(entry)
        self.assertTrue(key.startswith("retarget:parent:"), key)
        self.assertEqual(self.enqueued, [("reviewer", 184, C, key)])
        self.assertEqual(entry["fresh_review"]["state"], "enqueued")
        self.assertTrue(any("fresh review: enqueued" in line for line in lines), lines)
        self.sweep([self.child])
        self.sweep([self.child])
        self.assertEqual(len(self.enqueued), 1, "a repeated sweep must not enqueue again")
        # A redelivered edited webhook re-reads the live PR and names the same turn.
        payload = {"action": "edited", "number": 184, "repository": {"full_name": REPO},
                   "pull_request": self.child}
        self.run_gate(reviewer, payload)
        self.assertEqual(len(self.enqueued), 1)
        # Even with the bookkeeping lost, the ledger's unique turn index dedups the redelivery.
        self.st.transition_update(184, C, {"fresh_review": None})
        self.run_gate(reviewer, payload)
        self.assertEqual(len(self.enqueued), 2)
        self.assertEqual(self.ledger_rows(), [(C, key)])

    def test_edited_webhook_enqueues_before_the_sweep_and_the_sweep_dedups(self):
        self.sweep([self.parent, self.child])
        self.parent.update(state="closed", merged=True)
        self.child["base"].update(ref="main", sha=A)
        payload = {"action": "edited", "number": 184, "repository": {"full_name": REPO},
                   "pull_request": self.child}
        output, _ = self.run_gate(reviewer, payload)
        self.assertIn("fresh review enqueued", output)
        self.run_gate(reviewer, payload)
        self.sweep([self.child])
        key = transition.turn_key(transition.hold(self.st, 184, C))
        self.assertEqual(self.enqueued, [("reviewer", 184, C, key)])
        self.assertEqual(self.ledger_rows(), [(C, key)])

    def test_enqueue_failure_is_visible_and_retried_next_sweep(self):
        self.enqueue_error = FileNotFoundError("review-loop-runtime.json")
        lines = self.merge_parent_and_retarget()
        self.assertEqual(len(self.enqueued), 1, "a failure is not retried twice in one sweep")
        self.assertTrue(any("fresh review: retry" in line for line in lines), lines)
        fresh = transition.hold(self.st, 184, C)["fresh_review"]
        self.assertEqual(fresh["state"], "retry")
        report = gate.explain(self.loop, self.st, 184, {"pr": self.child, "reviews": self.reviews,
                                                        "receipts": {}, "armed": True})
        self.assertEqual(report["next"]["kind"], "retry")
        self.assertIn("reviewer enqueue failed", report["next"]["action"])
        self.enqueue_error = None
        lines = self.sweep([self.child])
        self.assertEqual(len(self.enqueued), 2)
        self.assertTrue(any("fresh review after retarget enqueued" in line for line in lines), lines)
        self.assertEqual(len(self.ledger_rows()), 1)
        self.sweep([self.child])
        self.assertEqual(len(self.enqueued), 2)

    def test_draft_retarget_waits_until_ready_then_enqueues_once(self):
        self.sweep([self.parent, self.child])
        self.child["draft"] = True
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.child])
        self.assertIsNotNone(transition.hold(self.st, 184, C))
        self.assertEqual(self.enqueued, [], "a draft is not reviewed")
        self.child["draft"] = False
        self.sweep([self.child])
        self.sweep([self.child])
        self.assertEqual(len(self.enqueued), 1)

    # -- only receipted post-boundary reviews count ------------------------------------------

    def test_old_pre_boundary_approval_never_produces_merge_cue(self):
        self.merge_parent_and_retarget()
        self.receipt(19, "APPROVED")  # even a receipt cannot launder a baseline review
        _, calls = self.run_gate(fixer, self.verdict_event(self.old[2]))
        self.assertEqual(self.merge_cues(calls), [])

    def test_receipted_post_boundary_approval_produces_merge_cue(self):
        self.merge_parent_and_retarget()
        fresh = review(30, "APPROVED", minute=9)
        self.reviews.append(fresh)
        _, calls = self.run_gate(fixer, self.verdict_event(fresh))
        self.assertEqual(self.merge_cues(calls), [], "unreceipted: diagnostic only")
        self.receipt(30, "APPROVED")
        _, calls = self.run_gate(fixer, self.verdict_event(fresh))
        self.assertEqual(len(self.merge_cues(calls)), 1, calls)
        report = gate.explain(self.loop, self.st, 184, {
            "pr": self.child, "reviews": self.reviews, "armed": True,
            "receipts": transition.read_receipts(self.loop, 184, C)})
        self.assertTrue(report["approved"])
        self.assertIn("approved", report["next"]["action"])

    def test_unreceipted_or_misbound_post_boundary_reviews_are_ignored(self):
        self.merge_parent_and_retarget()
        human = review(40, "APPROVED", minute=5, login="vex")
        wrong_principal = review(41, "APPROVED", minute=6)
        stacked_gen = review(42, "APPROVED", minute=7)
        other_head = review(43, "APPROVED", minute=8)
        fixer_seat = review(44, "APPROVED", minute=9)
        other_verdict = review(45, "APPROVED", minute=10)
        self.receipt(41, "APPROVED", principal=99)
        self.receipt(42, "APPROVED", base="parent")
        self.receipt(43, "APPROVED", head=D)
        self.receipt(44, "APPROVED", seat="fixer")
        self.receipt(45, "CHANGES_REQUESTED")
        reviews = self.reviews + [human, wrong_principal, stacked_gen, other_head, fixer_seat,
                                  other_verdict]
        self.assertEqual(transition.effective_reviews(self.loop, self.st, 184, C, reviews), [])
        for event in (human, wrong_principal, stacked_gen, other_verdict):
            self.reviews = reviews
            _, calls = self.run_gate(fixer, self.verdict_event(event))
            self.assertEqual(self.merge_cues(calls), [], event["id"])
        # No hold (an ordinary trunk head): the list is unchanged.
        self.assertEqual(transition.effective_reviews(self.loop, self.st, 184, D, reviews), reviews)
        # Unknown stays unknown.
        self.assertIsNone(transition.effective_reviews(self.loop, self.st, 184, C, None))
        with mock.patch.object(transition, "read_receipts", side_effect=sqlite3.DatabaseError("x")):
            self.assertIsNone(transition.effective_reviews(self.loop, self.st, 184, C, reviews))

    def test_missing_baseline_stays_held_with_its_reason(self):
        self.sweep([self.parent, self.child])
        self.child["base"].update(ref="main", sha=A)
        self.baseline_error = "HTTP 502"
        self.sweep([self.child])
        entry = transition.hold(self.st, 184, C)
        self.assertIsNone(entry["old_review_ids"])
        self.baseline_error = ""
        self.sweep([self.child])
        self.assertEqual(self.enqueued, [], "an unseparable history gets no fresh turn")
        fresh = review(30, "APPROVED", minute=9)
        self.reviews.append(fresh)
        self.receipt(30, "APPROVED")
        self.assertEqual(transition.effective_reviews(self.loop, self.st, 184, C, self.reviews), [])
        _, calls = self.run_gate(fixer, self.verdict_event(fresh))
        self.assertEqual(self.merge_cues(calls), [])
        output, calls = self.run_gate(reviewer, self.request_event())
        self.assertIn("unreadable when the retarget was recorded", output)
        self.assertEqual(calls["block"], [])
        report = gate.explain(self.loop, self.st, 184, {
            "pr": self.child, "reviews": self.reviews, "armed": True,
            "receipts": transition.read_receipts(self.loop, 184, C)})
        self.assertEqual(report["next"]["kind"], "wait")
        self.assertIn(transition.MISSING_BASELINE, report["next"]["action"])
        self.assertFalse(report["approved"])

    def test_receipted_post_boundary_changes_requested_enqueues_the_fixer(self):
        self.merge_parent_and_retarget()
        fresh = review(32, "CHANGES_REQUESTED", minute=9)
        self.reviews.append(fresh)
        _, calls = self.run_gate(fixer, self.verdict_event(fresh))
        self.assertEqual(calls["block"], [], "unreceipted: not a work order")
        self.receipt(32, "CHANGES_REQUESTED")
        _, calls = self.run_gate(fixer, self.verdict_event(fresh))
        self.assertEqual(calls["breach"], [], "old rounds must not spend the cap")
        self.assertEqual(len(calls["block"]), 1, calls)
        args, kwargs = calls["block"][0]
        self.assertEqual(args[2:5], ("fixer", 184, C))
        self.assertEqual(calls["notify"][-1]["round_no"], 1)
        # An old pre-boundary rejection is never that work order.
        _, calls = self.run_gate(fixer, self.verdict_event(self.old[1]))
        self.assertEqual(calls["block"], [])

    def test_round_count_after_boundary_counts_only_receipted_verdicts(self):
        self.merge_parent_and_retarget()
        key = transition.turn_key(transition.hold(self.st, 184, C))
        # Three old rejections would spend a cap of 3; after the boundary they count for nothing.
        self.reviews = self.old[:2] + [review(20, "CHANGES_REQUESTED", minute=4)]
        self.assertEqual(len(gate.verdicts(
            transition.effective_reviews(self.loop, self.st, 184, C, self.reviews), self.loop)), 0)
        self.reviews.append(review(33, "COMMENTED", minute=9))  # unreceipted, and not a verdict
        output, calls = self.run_gate(reviewer, self.request_event())
        self.assertEqual(calls["breach"], [])
        self.assertEqual(len(calls["block"]), 1, output)
        # A request re-drives only the transition's own fresh turn (deduped with the sweep).
        self.assertEqual(calls["block"][0][1]["turn_key"], key)
        self.assertEqual(calls["notify"][-1]["round_no"], 1)
        # Once a receipted post-boundary verdict exists at this head, the request is a no-op.
        self.reviews.append(review(34, "CHANGES_REQUESTED", minute=10))
        self.receipt(34, "CHANGES_REQUESTED")
        output, calls = self.run_gate(reviewer, self.request_event())
        self.assertIn("already has a reviewer's verdict", output)
        self.assertEqual(calls["block"], [])
        self.assertEqual(len(gate.verdicts(
            transition.effective_reviews(self.loop, self.st, 184, C, self.reviews), self.loop)), 1)

    def test_watchdog_stall_scan_ignores_old_verdicts_at_held_head(self):
        self.merge_parent_and_retarget()
        self.reviews = self.old[:2] + [review(20, "CHANGES_REQUESTED", minute=4)]
        lines = self.sweep([self.child])
        self.assertFalse(any("NO escalation marker" in line for line in lines), lines)
        self.assertFalse(any("fixer never pushed" in line for line in lines), lines)

    def test_explain_reports_queued_fresh_review(self):
        self.merge_parent_and_retarget()
        report = gate.explain(self.loop, self.st, 184, {
            "pr": self.child, "reviews": self.reviews, "armed": True,
            "receipts": transition.read_receipts(self.loop, 184, C)})
        self.assertEqual(report["next"]["kind"], "review-verdict")
        self.assertIn("fresh review after retarget: reviewer queued", report["next"]["action"])
        self.assertNotIn("push a new", report["next"]["action"])
        self.assertFalse(report["approved"])
        self.assertEqual(report["spent"], 0)
        self.assertTrue(any("fresh review situation" in b for b in report["blockers"]))
        # A failed receipt read is unknown, never "no trusted review".
        report = gate.explain(self.loop, self.st, 184, {
            "pr": self.child, "reviews": self.reviews, "armed": True, "receipts": None,
            "receipts_error": "database is locked"})
        self.assertEqual(report["next"]["kind"], "retry")
        self.assertIn("database is locked", report["next"]["action"])

    def test_new_head_after_retarget_is_an_ordinary_trunk_pr(self):
        self.merge_parent_and_retarget()
        self.child["head"]["sha"] = D
        self.reviews = [review(50, "APPROVED", head=D, minute=9)]
        self.sweep([self.child])
        self.assertEqual(len(self.enqueued), 1, "a new head is not another transition")
        _, calls = self.run_gate(fixer, self.verdict_event(self.reviews[0]))
        self.assertEqual(len(self.merge_cues(calls)), 1, "ordinary trunk approval, no receipt needed")

    # -- the supervisor's live claim ---------------------------------------------------------

    def test_fixer_claim_needs_a_receipted_post_boundary_verdict(self):
        self.merge_parent_and_retarget()
        run_id = uuid.uuid4().hex
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,created,updated,"
                        "push_admitted) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                        (run_id, "fix-184", REPO, 184, C, "fixer", "", "pending", time.time(),
                         time.time()))
        sup = Supervisor(self.db)
        sup.production_config = pathlib.Path(self.temp.name) / "unused-config"

        def claim():
            with mock.patch("review_loop.config.by_repo", return_value=self.loop), \
                 mock.patch("review_loop.gh.api", return_value=self.child), \
                 mock.patch("review_loop.gh.reviews", side_effect=lambda *_: list(self.reviews)):
                return sup._claim()

        # The latest listed verdict is an old rejection: not a work order after the boundary.
        self.reviews = [self.old[0]]
        self.assertIsNone(claim())
        self.assertEqual(sup.get("fix-184")["state"], "pending")
        fresh = review(35, "CHANGES_REQUESTED", minute=9)
        self.reviews.append(fresh)
        self.receipt(35, "CHANGES_REQUESTED")
        claimed = claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], run_id)


if __name__ == "__main__":
    unittest.main()
