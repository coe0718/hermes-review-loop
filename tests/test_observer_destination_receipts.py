"""Observer destination and legacy receipt safety regressions."""
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from review_loop import observer, routes
from review_loop.state import LoopState


class ObserverSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.subs = pathlib.Path(self.temp.name) / "subscriptions.json"
        self.env = patch.dict("os.environ", {"REVIEW_LOOP_SUBS": str(self.subs)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.loop = {"id": "private", "repo": "owner/private", "state_dir": self.temp.name,
                     "host": "", "observer": {"route": "private-observe", "profile": "default",
                                               "deliver": "telegram"}}
        self.subs.write_text(json.dumps({"private-observe": {
            "host": "https://attacker.example", "secret": "secret", **observer.route_contract(self.loop)}}))
        self.state = LoopState(self.loop)

    def test_hostless_loop_never_trusts_registry_host_for_private_link(self):
        with patch.object(routes, "fire") as fire:
            self.assertIsNone(observer._target(self.loop))
            self.assertFalse(observer.notify(self.loop, self.state, "opened", 7, "a" * 40))
            fire.assert_not_called()
        self.assertEqual(observer.owed(self.state).get("failed"), 1)

    def test_explicit_loop_host_overrides_registry_host(self):
        self.loop["host"] = "https://owner.example"
        target = observer._target(self.loop)
        self.assertIsNotNone(target)
        self.assertEqual(target[0] if target else None,
                         "https://owner.example/webhooks/private-observe")

    def test_legacy_failed_receipt_is_quarantined_not_replayed(self):
        self.loop["host"] = "https://owner.example"
        key = "old-writer"
        self.state.observations.write_text(json.dumps({"entries": {key: {
            "status": "failed", "attempts": 1, "at": time.time(),
            "event": "opened", "number": 7, "head": "a" * 40,
            "message": "private PR link", "url": "https://github.com/owner/private/pull/7"
        }}}))
        with patch.object(routes, "fire") as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 0)
            fire.assert_not_called()
        entry = observer._read(self.state.observations)["entries"][key]
        self.assertEqual(entry["status"], "uncertain")
        self.assertEqual(observer.unsettled(self.state), 1)

    def test_new_definite_prepost_failure_retries_once(self):
        self.loop["host"] = "https://owner.example"
        subs = json.loads(self.subs.read_text())
        subs["private-observe"]["secret"] = ""
        self.subs.write_text(json.dumps(subs))
        self.assertFalse(observer.notify(self.loop, self.state, "opened", 7, "a" * 40))
        entry = next(iter(observer._read(self.state.observations)["entries"].values()))
        self.assertEqual(entry["status"], "failed")
        subs["private-observe"]["secret"] = "secret"
        self.subs.write_text(json.dumps(subs))
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
            fire.assert_called_once()

    def test_retry_after_head_move_and_dismissal_drops_cached_merge_instruction(self):
        self.loop["host"] = "https://owner.example"
        subs = json.loads(self.subs.read_text())
        subs["private-observe"]["secret"] = ""
        self.subs.write_text(json.dumps(subs))
        head = "a" * 40
        self.assertFalse(observer.notify(self.loop, self.state, "approved", 7, head,
                                         identity=42, next_turn="you merge"))
        self.assertIn("next: you merge", next(iter(observer._read(self.state.observations)["entries"].values()))["message"])
        subs["private-observe"]["secret"] = "secret"
        self.subs.write_text(json.dumps(subs))
        with patch.object(observer.gh, "pr", return_value={"number": 7, "state": "open", "head": {"sha": "b" * 40}}), \
             patch.object(observer.gh, "reviews", return_value=[{"id": 42, "state": "DISMISSED", "commit_id": head}]), \
             patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
        self.assertNotIn("next: you merge", fire.call_args.args[2]["_observer"]["message"])

    def test_digest_drops_queued_next_turn_instructions(self):
        self.loop["host"] = "https://owner.example"
        self.loop["observer"]["digest_min"] = 15
        head = "a" * 40
        self.assertFalse(observer.notify(self.loop, self.state, "approved", 7, head,
                                         identity=42, next_turn="you merge"))
        self.assertFalse(observer.notify(self.loop, self.state, "handoff", 7, head,
                                         identity="fix", next_turn="reviewer"))
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertTrue(observer.flush(self.loop, self.state))
        message = fire.call_args.args[2]["_observer"]["message"]
        self.assertIn("approved", message)
        self.assertIn("fix pushed", message)
        self.assertNotIn("next:", message)

    def test_approval_dismissed_before_immediate_post_loses_merge_instruction(self):
        self.loop["host"] = "https://owner.example"
        self.loop["reviewers"] = ["reviewer"]
        head = "a" * 40
        with patch.object(observer.gh, "pr", return_value={"number": 7, "state": "open", "head": {"sha": head}}), \
             patch.object(observer.gh, "reviews", return_value=[{
                 "id": 42, "state": "DISMISSED", "commit_id": head,
                 "user": {"login": "reviewer"}}]), \
             patch.object(routes, "fire", return_value=True) as fire:
            self.assertTrue(observer.notify(self.loop, self.state, "approved", 7, head,
                                            identity=42, next_turn="you merge"))
        self.assertNotIn("next: you merge", fire.call_args.args[2]["_observer"]["message"])

    def test_newer_same_head_rejection_drops_merge_instruction_at_delivery_and_retry(self):
        self.loop["host"] = "https://owner.example"
        self.loop["reviewers"] = ["reviewer"]
        head = "a" * 40
        approved = {"id": 42, "state": "APPROVED", "commit_id": head,
                    "user": {"login": "reviewer"}, "submitted_at": "2026-01-01T00:00:00Z"}
        rejected = approved | {"id": 43, "state": "CHANGES_REQUESTED",
                               "submitted_at": "2026-01-01T00:01:00Z"}
        current = {"number": 7, "state": "open", "head": {"sha": head}}
        for retry in (False, True):
            with self.subTest(retry=retry):
                self.state.observations.unlink(missing_ok=True)
                subs = json.loads(self.subs.read_text())
                subs["private-observe"]["secret"] = "" if retry else "secret"
                self.subs.write_text(json.dumps(subs))
                with patch.object(observer.gh, "pr", return_value=current), \
                     patch.object(observer.gh, "reviews", return_value=[rejected, approved]), \
                     patch.object(routes, "fire", return_value=True) as fire:
                    observer.notify(self.loop, self.state, "approved", 7, head,
                                    identity=42, next_turn="you merge")
                    if retry:
                        subs["private-observe"]["secret"] = "secret"
                        self.subs.write_text(json.dumps(subs))
                        self.assertEqual(observer.retry(self.loop, self.state), 1)
                self.assertNotIn("next: you merge", fire.call_args.args[2]["_observer"]["message"])

    def test_legacy_cached_digest_retry_keeps_link_but_not_instruction(self):
        self.loop["host"] = "https://owner.example"
        cached = "🗂 digest\n• #7 `aaaaaaa` approved · next: you merge · https://github.com/owner/private/pull/7"
        self.state.observations.write_text(json.dumps({"entries": {"digest-old": {
            "status": "failed", "retryable": True, "attempts": 1, "at": time.time(),
            "event": "digest", "number": "", "head": "", "message": cached,
            "batch": ["member"], "url": ""
        }, "member": {"status": "digesting", "at": time.time()}}}))
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
        message = fire.call_args.args[2]["_observer"]["message"]
        self.assertNotIn("next: you merge", message)
        self.assertIn("https://github.com/owner/private/pull/7", message)


if __name__ == "__main__":
    unittest.main()
