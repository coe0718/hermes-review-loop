"""Issue #1: the plugin's intent record, self-heal, and optimistic registry writes.

Hermes's CLI/dashboard rewrite the shared webhook registry without the plugin's lock. These
tests play that native writer: plain ``write_text`` calls that ignore the lock entirely.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import cli, doctor, prompts, route_intent, routes  # noqa: E402
from scripts import watchdog  # noqa: E402

HOST = "https://gateway.example"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.subs = root / "webhook_subscriptions.json"
        env = {"REVIEW_LOOP_SUBS": str(self.subs), "HERMES_HOME": str(root / "hermes"),
               "REVIEW_LOOP_CONFIG_DIR": str(root / "loops")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.loop = {"id": "widgets", "repo": "acme/widgets", "state_dir": str(root / "state"),
                     "seats": {"reviewer": {"route": "widgets-review", "profile": "rev"},
                               "fixer": {"route": "widgets-fix", "profile": "fix"}}}
        # Someone else's route, present from the start: nothing here may ever change it.
        self.subs.write_text(json.dumps({"other-plugin": {"secret": "theirs", "script": "x.py",
                                                          "prompt": "p", "events": ["push"]}}))

    def install(self, *, record=True):
        routes.new_route("widgets-review", profile="rev", prompt=prompts.REVIEWER,
                         events=["pull_request"], script="gate_reviewer.py", deliver="discord",
                         host=HOST)
        routes.new_route("widgets-fix", profile="fix", prompt=prompts.FIXER,
                         events=["pull_request_review"], script="gate_fixer.py",
                         deliver="discord", host=HOST)
        if record:
            route_intent.record_live(self.loop, ["widgets-review", "widgets-fix"], replace=True)
        return json.loads(self.subs.read_text())

    def native(self, mutate):
        """A Hermes CLI/dashboard write: read, edit, rewrite — no plugin lock taken."""
        data = json.loads(self.subs.read_text())
        mutate(data)
        self.subs.write_text(json.dumps(data))

    def live(self):
        return json.loads(self.subs.read_text())


class IntentRecordTest(Fixture):
    def test_record_is_private_atomic_copy_with_secrets(self):
        before = self.install()
        target = route_intent.path(self.loop)
        self.assertEqual(target.parent, pathlib.Path(self.loop["state_dir"]))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        intent = route_intent.load(self.loop)
        self.assertEqual(intent["widgets-review"]["secret"], before["widgets-review"]["secret"])
        self.assertNotIn("other-plugin", intent)
        self.assertEqual([p.name for p in target.parent.iterdir() if p.name.startswith(".")], [])

    def test_malformed_record_heals_nothing_and_alerts(self):
        self.install()
        route_intent.path(self.loop).write_text("{broken")
        self.native(lambda d: d.pop("widgets-review"))
        before = self.subs.read_bytes()
        lines = route_intent.heal(self.loop)
        self.assertEqual(self.subs.read_bytes(), before)
        self.assertTrue(any("intent record unreadable" in line for line in lines), lines)


class SelfHealTest(Fixture):
    def test_erased_route_restored_with_same_secret_and_alert(self):
        before = self.install()
        self.native(lambda d: d.pop("widgets-review"))
        lines = route_intent.heal(self.loop)
        self.assertEqual(self.live()["widgets-review"], before["widgets-review"])
        self.assertIn("restored 1 route", lines[0])
        self.assertTrue(any("widgets-review: was missing" in line for line in lines), lines)
        self.assertFalse(any(before["widgets-review"]["secret"] in line for line in lines))
        self.assertEqual(route_intent.heal(self.loop), [])       # healed stays healed, silently

    def test_altered_secret_and_profile_restored(self):
        before = self.install()

        def alter(d):
            d["widgets-fix"].update(secret="rotated-by-dashboard", profile="elsewhere",
                                    deliver_only=True)
        self.native(alter)
        lines = route_intent.heal(self.loop)
        self.assertEqual(self.live()["widgets-fix"], before["widgets-fix"])
        joined = "\n".join(lines)
        for field in ("secret", "profile", "deliver_only"):
            self.assertIn(field, joined)
        self.assertNotIn("rotated-by-dashboard", joined)

    def test_unrelated_native_routes_and_edits_untouched(self):
        before = self.install()

        def native(d):
            d.pop("widgets-review")
            d["other-plugin"]["secret"] = "their-rotation"
            d["brand-new"] = {"secret": "n", "script": "y.py"}
            d["widgets-fix"]["description"] = "renamed in the dashboard"   # not watched
        self.native(native)
        route_intent.heal(self.loop)
        live = self.live()
        self.assertEqual(live["widgets-review"], before["widgets-review"])
        self.assertEqual(live["other-plugin"]["secret"], "their-rotation")
        self.assertEqual(live["brand-new"], {"secret": "n", "script": "y.py"})
        self.assertEqual(live["widgets-fix"]["description"], "renamed in the dashboard")

    def test_name_taken_by_foreign_script_is_reported_not_overwritten(self):
        self.install()
        self.native(lambda d: d.__setitem__("widgets-fix", {"secret": "s", "script": "theirs.py"}))
        lines = route_intent.heal(self.loop)
        self.assertEqual(self.live()["widgets-fix"], {"secret": "s", "script": "theirs.py"})
        self.assertTrue(any("widgets-fix NOT restored" in line for line in lines), lines)

    def test_malformed_registry_is_not_overwritten(self):
        self.install()
        for body in (b'{"widgets-review": {broken', b"[]"):
            self.subs.write_bytes(body)
            lines = route_intent.heal(self.loop)
            self.assertEqual(self.subs.read_bytes(), body)
            self.assertTrue(any("NOT" in line and "overwritten" in line for line in lines), lines)

    def test_routes_outside_this_loops_config_are_not_healed(self):
        self.install()
        moved = dict(self.loop, seats={"reviewer": {"route": "widgets-review"},
                                       "fixer": {"route": "widgets-fix-2"}})
        self.native(lambda d: d.pop("widgets-fix"))
        route_intent.heal(moved)
        self.assertNotIn("widgets-fix", self.live())

    def test_existing_install_is_adopted_then_protected(self):
        before = self.install(record=False)
        self.assertIsNone(route_intent.load(self.loop))
        self.assertEqual(route_intent.heal(self.loop), [])
        self.assertEqual(set(route_intent.load(self.loop)), {"widgets-review", "widgets-fix"})
        self.native(lambda d: d.pop("widgets-fix"))
        route_intent.heal(self.loop)
        self.assertEqual(self.live()["widgets-fix"], before["widgets-fix"])

    def test_adoption_refuses_a_route_that_does_not_prove_ownership(self):
        self.install(record=False)
        self.native(lambda d: d["widgets-fix"].update(prompt="something else"))
        route_intent.heal(self.loop)
        self.assertEqual(set(route_intent.load(self.loop)), {"widgets-review"})


class OperatorIntentTest(Fixture):
    def test_uninstall_through_the_plugin_is_not_undone(self):
        self.install()
        args = SimpleNamespace(loop="widgets", keep_config=True)
        with mock.patch.object(cli.config, "load_id", return_value=self.loop), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_uninstall(args), 0)
        self.assertEqual(set(self.live()), {"other-plugin"})
        self.assertEqual(route_intent.load(self.loop), {})
        self.assertEqual(route_intent.heal(self.loop), [])
        self.assertEqual(set(self.live()), {"other-plugin"})

    def test_plugin_rebind_updates_the_record(self):
        self.install()
        routes.new_route("widgets-fix", profile="fix-2", prompt=prompts.FIXER,
                         events=["pull_request_review"], script="gate_fixer.py",
                         deliver="discord", host=HOST)
        route_intent.record_live(self.loop, ["widgets-fix"])
        self.assertEqual(route_intent.heal(self.loop), [])
        self.assertEqual(self.live()["widgets-fix"]["profile"], "fix-2")


class WatchdogAndDoctorTest(Fixture):
    def test_armed_sweep_heals_even_when_pr_listing_fails(self):
        before = self.install()
        self.native(lambda d: d.pop("widgets-review"))
        st = mock.Mock()
        st.watch.return_value = {}
        with mock.patch.object(watchdog.gate, "hooks_read", return_value=(True, "")), \
                mock.patch.object(watchdog.gh, "auth_probe",
                                  return_value=watchdog.gh.Response({"login": "rev"}, "", 200, {})), \
                mock.patch.object(watchdog, "TEST", False), \
                mock.patch.object(watchdog.gh, "open_prs", return_value=None):
            lines = watchdog.sweep_loop(self.loop, st)
        self.assertEqual(self.live()["widgets-review"], before["widgets-review"])
        self.assertTrue(any("restored 1 route" in line for line in lines), lines)
        self.assertTrue(any("could not list open PRs" in line for line in lines), lines)
        st.note.assert_called()

    def test_paused_sweep_does_not_heal(self):
        self.install()
        self.native(lambda d: d.pop("widgets-review"))
        with mock.patch.object(watchdog.gate, "hooks_read", return_value=(False, "reviewer, fixer")), \
                mock.patch.object(watchdog.gh, "auth_probe") as probe, \
                mock.patch.object(watchdog, "TEST", False):
            self.assertEqual(watchdog.sweep_loop(self.loop, mock.Mock()), [])
        probe.assert_not_called()               # a confirmed pause reads nothing more
        self.assertNotIn("widgets-review", self.live())

    def test_doctor_reports_drift_without_writing(self):
        self.install()
        self.native(lambda d: d["widgets-review"].update(secret="rotated"))
        data = self.live()
        before = self.subs.read_bytes()
        checks = [doctor.Check("route:widgets-review", doctor.VERIFIED, "rev"),
                  doctor.Check("route:widgets-fix", doctor.VERIFIED, "fix")]
        out = doctor._intent_overlay(self.loop, data, checks)
        self.assertEqual(out[0].status, doctor.MISMATCH)
        self.assertIn("secret", out[0].detail)
        self.assertIn("--repair", out[0].fix)
        self.assertEqual(out[1].status, doctor.VERIFIED)
        self.assertIn("matches intent record", out[1].detail)
        self.assertEqual(self.subs.read_bytes(), before)


class OptimisticWriteTest(Fixture):
    def test_native_write_between_read_and_replace_is_preserved(self):
        self.install()
        real = routes._identity
        injected = []

        def racing_identity(path):
            if not injected:                     # first pre-replace check: a native write lands
                injected.append(True)
                self.native(lambda d: d.__setitem__("native-new", {"secret": "n"}))
            return real(path)

        with mock.patch.object(routes, "_identity", side_effect=racing_identity):
            routes.new_route("widgets-observe", profile="default", prompt=prompts.OBSERVER,
                             events=["pull_request"], script="observe.py", deliver="telegram",
                             deliver_only=True, host=HOST)
        live = self.live()
        self.assertEqual(live["native-new"], {"secret": "n"})
        self.assertIn("widgets-observe", live)
        self.assertIn("other-plugin", live)
        self.assertEqual(stat.S_IMODE(self.subs.stat().st_mode), 0o600)

    def test_secret_is_stable_across_a_retry(self):
        real = routes._identity
        calls = []

        def racing_identity(path):
            calls.append(1)
            if len(calls) == 1:
                self.native(lambda d: d.__setitem__("native-new", {"secret": "n"}))
            return real(path)

        with mock.patch.object(routes, "_identity", side_effect=racing_identity):
            entry = routes.new_route("widgets-review", profile="rev", prompt=prompts.REVIEWER,
                                     events=["pull_request"], script="gate_reviewer.py",
                                     deliver="discord")
        self.assertEqual(self.live()["widgets-review"]["secret"], entry["secret"])

    def test_writer_that_never_settles_gets_bounded_retries_and_no_publish(self):
        self.install()
        n = [0]

        def always_changing(path):
            n[0] += 1
            self.native(lambda d: d.__setitem__("churn", n[0]))
            return routes._snapshot(path)[0]

        with mock.patch.object(routes, "_identity", side_effect=always_changing):
            with self.assertRaises(routes.RegistryConflictError):
                routes.remove_route("widgets-review")
        self.assertEqual(n[0], routes.CONFLICT_RETRIES)
        self.assertIn("widgets-review", self.live())
        self.assertEqual([p.name for p in self.subs.parent.iterdir() if p.name.startswith(".")],
                         [])

    def test_heal_racing_a_native_write_keeps_both(self):
        before = self.install()
        self.native(lambda d: d.pop("widgets-fix"))
        real = routes._identity
        injected = []

        def racing_identity(path):
            if not injected:
                injected.append(True)
                self.native(lambda d: d.__setitem__("native-new", {"secret": "n"}))
            return real(path)

        with mock.patch.object(routes, "_identity", side_effect=racing_identity):
            route_intent.heal(self.loop)
        live = self.live()
        self.assertEqual(live["widgets-fix"], before["widgets-fix"])
        self.assertEqual(live["native-new"], {"secret": "n"})


if __name__ == "__main__":
    unittest.main()
