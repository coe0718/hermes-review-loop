"""Regressions for observer installation, first-sweep notices, and effective verdicts."""
from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import cli, gate, gh
from scripts import watchdog

HEAD = "a" * 40
LOOP = {"id": "widgets", "repo": "acme/widgets", "base": "main", "cap": 5,
        "fixers": ["fixer"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
        "grace_min": 1, "marker_grace_min": 1, "cooldown_h": 1,
        "ttl_min": 30, "inflight_ttl_min": 30, "seats": {"reviewer": {"route": "widgets-review"},
        "fixer": {"route": "widgets-fix"}}}
PR = {"number": 7, "state": "open", "head": {"sha": HEAD},
      "base": {"ref": "main"}, "user": {"login": "fixer"}, "draft": False,
      "created_at": "2020-01-01T00:00:00Z", "title": "Fix widget"}


def review(rid, state, when):
    return {"id": rid, "state": state, "submitted_at": when,
            "commit_id": HEAD, "user": {"login": "reviewer"}}


APPROVED = review(100, "APPROVED", "2026-01-01T00:00:00Z")
REJECTED = review(101, "CHANGES_REQUESTED", "2026-01-01T00:01:00Z")


class ObserverInitCollisionTest(unittest.TestCase):
    def test_collision_refused_before_config_routes_or_hooks_are_written(self):
        args = SimpleNamespace(token=None, id="widgets", repo="acme/widgets", base="main", cap=3,
                               concurrency=1, fixer=["fixer"], reviewer=["reviewer"],
                               reviewer_seat="reviewer", reviewer_profile="default", fixer_profile="default",
                               reviewer_agent="", fixer_agent="", adjudicator_route="widgets-breach",
                               adjudicator_profile="default", skill="", read_token="reviewer",
                               clone="", root=None, state_dir="", host="https://example.test",
                               grace_min=1, ttl_min=30, inflight_ttl_min=30,
                               reviewer_concurrency=None, fixer_concurrency=None,
                               observer_profile="default", observer_deliver="telegram",
                               observer_events=None, observer_digest_min=None,
                               hooks=True, admin_token=None, schedule=None)
        for route, preexisting in (("widgets-review", False), ("widgets-fix", False),
                                   ("widgets-breach", False), ("taken-observe", True)):
            with self.subTest(route=route):
                args.observer_route = route
                normalized = LOOP | {"host": "https://example.test",
                                     "adjudicator": {"route": "widgets-breach"},
                                     "observer": {"route": route, "deliver": "telegram"}}
                with (mock.patch.object(cli.config, "normalize", return_value=normalized),
                      mock.patch.object(cli.config, "webhook_host", return_value="https://example.test"),
                      mock.patch.object(cli.config, "config_dir", return_value=pathlib.Path("/no-write")),
                      mock.patch.object(cli.routes, "route", return_value={"script": "foreign.py"} if preexisting else None),
                      mock.patch.object(cli, "_write_config") as write,
                      mock.patch.object(cli, "_install_routes") as install,
                      mock.patch.object(cli, "_install_hooks") as hooks,
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertEqual(cli.cmd_init(args), 2)
                write.assert_not_called()
                install.assert_not_called()
                hooks.assert_not_called()


class EffectiveVerdictTest(unittest.TestCase):
    def test_explain_reads_all_pages_and_fails_closed_on_later_page_error(self):
        with (mock.patch.object(gh, "fetch", side_effect=[(PR, ""), ([APPROVED] * 100, ""),
                                                      (None, "HTTP 503")]) as fetch,
              mock.patch.object(gate, "hooks_read", return_value=(True, ""))):
            facts = gate.explain_facts(LOOP, 7)
        self.assertIsNone(facts["reviews"])
        self.assertIn("page 2", facts["reviews_error"])
        self.assertEqual(fetch.call_args_list[-1], mock.call(LOOP, gh.reviews_path(LOOP, 7) + "&page=2"))

    def test_page_101_rejection_overrides_approval_in_explain(self):
        first_page = [review(i, "COMMENTED", "2026-01-01T00:00:00Z")
                      for i in range(1, 100)] + [APPROVED]
        with (mock.patch.object(gh, "fetch", side_effect=[(PR, ""), (first_page, ""),
                                                      ([REJECTED], "")]),
              mock.patch.object(gate, "hooks_read", return_value=(True, ""))):
            facts = gate.explain_facts(LOOP, 7)
        self.assertEqual(len(facts["reviews"]), 101)
        self.assertEqual(facts["reviews_error"], "")
        self.assert_no_merge(facts)

    def test_explain_never_says_merge_after_newer_same_head_rejection(self):
        self.assert_no_merge({"pr": PR, "reviews": [REJECTED, APPROVED],
                              "armed": True, "read_at": time.time()})

    def assert_no_merge(self, facts):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": {}, "parked": False, "delivery_status": "",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local):
            result = gate.explain(LOOP, mock.Mock(), 7, facts)
        self.assertNotIn("human merges", str(result))
        self.assertIn("changes requested", str(result).lower())

    def test_watchdog_newer_rejection_is_not_hidden_by_prior_approval(self):
        st = mock.Mock()
        st.watch.return_value = {"armed_since": time.time() - 3600,
                                 "heads": {"7": {"sha": HEAD, "observed_at": time.time() - 3600}},
                                 "alerts": {}}
        st.breach_all.return_value = {}
        st._load.return_value = {}
        st.queue_all.return_value = {}
        with (mock.patch.object(watchdog, "TEST", True),
              mock.patch.object(gh, "open_prs", return_value=[PR]),
              mock.patch.object(gh, "reviews", return_value=[APPROVED, REJECTED]),
              mock.patch.object(watchdog, "retry_pending_breaches"),
              mock.patch.object(watchdog, "drain_queued"),
              mock.patch.object(watchdog.observer, "notify") as notify,
              mock.patch.object(watchdog.observer, "retry", return_value=0),
              mock.patch.object(watchdog.observer, "flush")):
            lines = watchdog.sweep_loop(LOOP, st)
        self.assertIn("fixer never pushed", "\n".join(lines))
        notify.assert_called_once()

    def test_queued_fixer_replays_only_live_latest_rejection(self):
        st = mock.Mock()
        st.queue_items.return_value = {"acme/widgets#7": {"head": HEAD, "at": 1}}
        st.active.return_value = {}
        st.held_by_other.return_value = None
        st.watch.return_value = {}
        newer_approval = APPROVED | {"submitted_at": "2026-01-01T00:02:00Z"}
        scenarios = (([REJECTED, newer_approval], False, True),
                     ([newer_approval, REJECTED], False, True),
                     ([APPROVED, REJECTED], True, True),
                     ([REJECTED, APPROVED], True, True),
                     ([REJECTED, newer_approval | {"submitted_at": None}], False, False),
                     ([REJECTED | {"state": "DISMISSED"}], False, False))
        for reviews, should_fire, should_pop in scenarios:
            with self.subTest(reviews=reviews):
                st.reset_mock()
                entry = {"head": HEAD, "at": 1}
                # The gate acknowledges a fired entry by removing it; drain reads that back.
                st.queue_items.side_effect = [{"acme/widgets#7": entry}, {}]
                st.active.return_value = {}
                st.held_by_other.return_value = None
                st.watch.return_value = {}
                with (mock.patch.object(watchdog.config, "seat_concurrency", return_value=1),
                      mock.patch.object(gh, "pr", return_value=PR),
                      mock.patch.object(gh, "reviews", return_value=reviews),
                      mock.patch.object(watchdog.routes, "fire", return_value=True) as fire):
                    started = watchdog.drain(LOOP, st, "fixer", quiet=True)
                self.assertEqual(started, int(should_fire))
                if should_fire:
                    self.assertEqual(fire.call_args.args[2]["review"]["id"], REJECTED["id"])
                else:
                    fire.assert_not_called()
                # A superseded entry is dropped by drain; a fired one only by the gate that took it.
                if should_pop and not should_fire:
                    st.queue_pop_if.assert_called_once_with("fixer", "acme/widgets#7", entry)
                else:
                    st.queue_pop_if.assert_not_called()

    def test_first_armed_sweep_retries_and_flushes_without_stall(self):
        st = mock.Mock()
        st.watch.return_value = {}
        st.breach_all.return_value = {}
        with (mock.patch.object(watchdog, "TEST", True),
              mock.patch.object(gh, "open_prs", return_value=[PR]),
              mock.patch.object(watchdog, "retry_pending_breaches"),
              mock.patch.object(watchdog, "drain_queued"),
              mock.patch.object(watchdog.observer, "retry", return_value=1) as retry,
              mock.patch.object(watchdog.observer, "flush") as flush,
              mock.patch.object(watchdog.observer, "notify") as notify):
            lines = watchdog.sweep_loop(LOOP, st)
        self.assertEqual(lines, [])
        retry.assert_called_once_with(LOOP, st)
        flush.assert_called_once_with(LOOP, st, wait_s=0)
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
