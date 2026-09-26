#!/usr/bin/env python3
"""Issue #57 (and #83's tracebacks): uninstall leaves nothing live, init never doubles hooks.

GitHub is the harness's stateful stub (hook POST/PATCH/DELETE change its world file) and the
scheduler is the harness's fake ``hermes`` — no test here can reach a real repo or install.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from review_loop import cli, config, doctor, routes  # noqa: E402

LOOP_FILE = t.LOOPS_DIR / "widgets.json"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        self.world(hooks=[])
        self.hermes_log = t.TMP / "hermes-calls.jsonl"
        self.hermes_log.unlink(missing_ok=True)
        os.environ["FAKE_HERMES_LOG"] = str(self.hermes_log)
        os.environ["REVIEW_LOOP_HERMES"] = str(t.FAKE_HERMES)
        self.addCleanup(os.environ.pop, "FAKE_HERMES_LOG", None)

    # -- helpers -------------------------------------------------------------------

    def world(self, **changes) -> dict:
        data = json.loads(t.WORLD_FILE.read_text())
        data.update(changes)
        t.WORLD_FILE.write_text(json.dumps(data))
        return data

    def hooks(self) -> list[dict]:
        return json.loads(t.WORLD_FILE.read_text()).get("hooks") or []

    def cli(self, *argv) -> tuple[int, str]:
        return t.run_cli(t.parser_for().parse_args(list(argv)))

    def init(self, *extra) -> tuple[int, str]:
        return self.cli("init", "--repo", t.REPO, "--id", "widgets", "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *extra)

    def fresh_install(self) -> None:
        """A clean `init --hooks` of the widgets loop (the fixture's hand-written one removed)."""
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.hooks()), 2)

    def cron_store(self, jobs: list[dict]) -> pathlib.Path:
        store = doctor.cron_store()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"jobs": jobs}))
        return store

    def job(self, loop_id: str, job_id: str) -> dict:
        return {"id": job_id, "name": f"review loop watchdog ({loop_id})",
                "script": cli.SHIM_NAME, "no_agent": True}


class RoundTripTest(Base):
    def test_uninstall_then_init_leaves_exactly_one_set_of_hooks(self):
        self.fresh_install()
        first = {hook["id"] for hook in self.hooks()}
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hooks(), [])
        for hook_id in first:
            self.assertIn(f"hook {hook_id} deleted", out)
        self.assertFalse(LOOP_FILE.exists())
        self.assertIsNone(routes.route("widgets-review"))
        self.assertNotIn(" arm ", out)  # no remediation that needs the config it just removed
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        hooks = self.hooks()
        self.assertEqual(len(hooks), 2)
        self.assertFalse(first & {hook["id"] for hook in hooks})
        self.assertTrue(all(hook["active"] is False for hook in hooks))

    def test_init_refuses_hooks_left_by_a_previous_install(self):
        self.fresh_install()
        stale = sorted(hook["id"] for hook in self.hooks())
        self.world(hooks=[{**hook, "active": True} for hook in self.hooks()])
        # The old uninstall: routes and config gone, hooks left live.
        rc, out = self.cli("uninstall", "--loop", "widgets", "--keep-hooks")
        self.assertEqual(rc, 0, out)
        subs_before = t.SUBS.read_text()
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 2, out)
        self.assertIn(", ".join(str(i) for i in stale), out)
        self.assertIn("2 active", out)
        for hook_id in stale:
            command = f"gh api -X DELETE repos/{t.REPO}/hooks/{hook_id}"
            self.assertIn(command, out)
            self.assertEqual(shlex.split(command)[-1], f"repos/{t.REPO}/hooks/{hook_id}")
        self.assertFalse(LOOP_FILE.exists())
        self.assertEqual(t.SUBS.read_text(), subs_before)
        self.assertEqual(sorted(hook["id"] for hook in self.hooks()), stale)

    def test_init_refuses_when_the_hook_listing_cannot_be_read(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.world(hooks=None)
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot read", out)
        self.assertFalse(LOOP_FILE.exists())

    def test_init_ignores_other_routes_hooks(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        other = {"id": 7, "active": True, "events": ["push"],
                 "config": {"url": f"{t.HOST}/webhooks/widgets-review-impostor"}}
        self.world(hooks=[other])
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.hooks()), 3)

    def test_hooks_on_another_gateway_are_info_not_a_refusal(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        foreign = {"id": 8, "active": True, "events": ["pull_request"],
                   "config": {"url": "https://old-gateway.example/p/reviewer-profile/webhooks/widgets-review"}}
        self.world(hooks=[foreign])
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 8", out)
        self.assertIn("another gateway", out)
        self.assertEqual(len(self.hooks()), 3)

    def test_same_gateway_hook_on_the_route_still_refuses(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        same = {"id": 8, "active": True, "events": ["pull_request"],
                "config": {"url": f"{t.HOST}/p/old-profile/webhooks/widgets-review"}}
        self.world(hooks=[same])
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 2, out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/8", out)


class UninstallRefusalTest(Base):
    def test_a_token_github_refuses_leaves_everything_and_prints_pasteable_commands(self):
        self.fresh_install()
        ids = sorted(hook["id"] for hook in self.hooks())
        self.world(hook_write_denied=[t.REVIEWER])
        subs_before = t.SUBS.read_text()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn("HTTP 403", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertEqual(t.SUBS.read_text(), subs_before)
        self.assertEqual(sorted(hook["id"] for hook in self.hooks()), ids)
        for hook_id in ids:
            self.assertIn(f"  gh api -X DELETE repos/{t.REPO}/hooks/{hook_id}\n", out)
        self.assertIn("hermes review-loop uninstall --loop widgets --admin-token <login>", out)
        # The printed re-run still parses: the config it needs is still there.
        t.parser_for().parse_args(["uninstall", "--loop", "widgets"])
        rc, out = self.cli("uninstall", "--loop", "widgets", "--admin-token", t.FIXER)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hooks(), [])
        self.assertFalse(LOOP_FILE.exists())

    def test_unmapped_admin_token_is_refused_before_anything(self):
        self.fresh_install()
        rc, out = self.cli("uninstall", "--loop", "widgets", "--admin-token", "stranger")
        self.assertEqual(rc, 2, out)
        self.assertIn("no token file mapped", out)
        self.assertEqual(len(self.hooks()), 2)
        self.assertTrue(LOOP_FILE.exists())

    def test_unreadable_listing_refuses_with_a_find_command(self):
        self.world(hooks=None)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIn(f"gh api 'repos/{t.REPO}/hooks?per_page=100' --jq", out)

    def test_keep_hooks_is_an_explicit_opt_out(self):
        self.fresh_install()
        rc, out = self.cli("uninstall", "--loop", "widgets", "--keep-hooks")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.hooks()), 2)
        self.assertIn("kept (--keep-hooks)", out)
        self.assertFalse(LOOP_FILE.exists())

    def test_hooks_on_another_gateway_are_not_deleted(self):
        foreign = {"id": 9, "active": True, "events": ["pull_request"],
                   "config": {"url": "https://other.example/webhooks/widgets-review"}}
        self.world(hooks=[foreign])
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hooks(), [foreign])
        self.assertIn("hook 9", out)
        self.assertIn("another gateway", out)


class CronTest(Base):
    def test_uninstall_removes_its_job_through_the_fake_scheduler_only(self):
        self.cron_store([self.job("widgets", "job1"), self.job("gadgets", "job2")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        calls = [json.loads(line) for line in self.hermes_log.read_text().splitlines()]
        self.assertEqual(calls, [["cron", "remove", "job1"]])
        jobs = json.loads(doctor.cron_store().read_text())["jobs"]
        self.assertEqual([job["id"] for job in jobs], ["job2"])
        self.assertTrue(shim.exists(), "another loop's job still runs the shared shim")

    def test_the_shared_shim_goes_with_the_last_job(self):
        self.cron_store([self.job("widgets", "job1")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertFalse(shim.exists())

    def test_a_job_that_will_not_go_refuses_before_routes_and_config(self):
        self.cron_store([self.job("widgets", "job1")])
        os.environ["REVIEW_LOOP_HERMES"] = "/bin/false"
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn("hermes cron remove job1", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIsNotNone(routes.route("widgets-review"))

    def test_an_unreadable_store_refuses_before_any_hook_is_deleted(self):
        self.fresh_install()
        doctor.cron_store().write_text("{broken")
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.hooks()), 2)


class PurgeTest(Base):
    def default_state(self) -> pathlib.Path:
        target = config.home() / "state" / "review-loops" / "widgets"
        cfg = json.loads(LOOP_FILE.read_text())
        cfg["state_dir"] = str(target)
        LOOP_FILE.write_text(json.dumps(cfg))
        (target / "sub").mkdir(parents=True, exist_ok=True)
        (target / "sub" / "x.json").write_text("{}")
        return target

    def test_purge_removes_the_default_state_dir_only(self):
        target = self.default_state()
        neighbour = target.parent / "gadgets"
        neighbour.mkdir()
        outside = t.TMP / "outside.txt"
        outside.write_text("keep")
        (target / "link").symlink_to(outside)
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(target.exists())
        self.assertTrue(neighbour.exists())
        self.assertEqual(outside.read_text(), "keep")

    def test_purge_refuses_a_custom_state_dir(self):
        t.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (t.STATE_DIR / "keep.json").write_text("{}")
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertIn("not the default", out)
        self.assertIn(f"rm -rf -- {shlex.quote(str(t.STATE_DIR))}", out)
        self.assertIn("hermes review-loop uninstall --loop widgets &&", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertTrue((t.STATE_DIR / "keep.json").exists())

    def test_a_custom_state_dir_with_spaces_is_quoted(self):
        odd = t.TMP / "my state's dir"
        odd.mkdir()
        cfg = json.loads(LOOP_FILE.read_text())
        cfg["state_dir"] = str(odd)
        LOOP_FILE.write_text(json.dumps(cfg))
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        line = next(l for l in out.splitlines() if "rm -rf --" in l)
        self.assertEqual(shlex.split(line.split("&&")[-1])[-1], str(odd))
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"rm -rf -- {shlex.quote(str(odd))}", out)
        self.assertTrue(odd.exists())

    def test_purge_refuses_a_symlinked_state_dir(self):
        target = self.default_state()
        real = t.TMP / "real-state"
        target.rename(real)
        target.symlink_to(real)
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertIn("symlink", out)
        self.assertIn(f"rm -- {shlex.quote(str(target))}", out)
        self.assertTrue((real / "sub" / "x.json").exists())
        self.assertTrue(LOOP_FILE.exists())

    def test_without_purge_default_state_is_kept_and_named(self):
        self.default_state()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertIn("pass --purge", out)


class DoctorDuplicateTest(Base):
    def test_two_hooks_on_one_route_fail_even_when_one_is_active(self):
        loop = config.load_id("widgets")
        url = routes.url_for("widgets-review", t.HOST)
        hooks = [{"id": 3, "active": True, "events": ["pull_request"],
                  "config": {"url": url, "content_type": "json"}},
                 {"id": 101, "active": False, "events": ["pull_request"],
                  "config": {"url": url, "content_type": "json"}}]
        check = doctor.check_hook(loop, hooks, "reviewer", "widgets-review", url)
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("2 repo hooks", check.detail)
        self.assertIn("1 active", check.detail)
        self.assertIn("`gh api -X DELETE repos/acme/widgets/hooks/3`", check.fix)
        self.assertIn("uninstall --loop widgets", check.fix)
        self.assertNotIn("hooks/101", check.fix)  # the newest is the one kept
        single = doctor.check_hook(loop, hooks[:1], "reviewer", "widgets-review", url)
        self.assertEqual(single.status, doctor.VERIFIED)

    def test_two_hooks_on_different_gateways_are_not_duplicates(self):
        loop = config.load_id("widgets")
        url = routes.url_for("widgets-review", t.HOST)
        hooks = [{"id": 3, "active": True, "events": ["pull_request"],
                  "config": {"url": "https://old.example/webhooks/widgets-review",
                             "content_type": "json"}},
                 {"id": 101, "active": True, "events": ["pull_request"],
                  "config": {"url": url, "content_type": "json"}}]
        check = doctor.check_hook(loop, hooks, "reviewer", "widgets-review", url)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)


class DoctorDeliveriesTest(Base):
    """The one place GitHub reveals a secret mismatch: how the gateway answered its deliveries."""

    def check(self, deliveries):
        loop = config.load_id("widgets")
        url = routes.url_for("widgets-review", t.HOST)
        hook = {"id": 5, "active": True, "events": ["pull_request"],
                "config": {"url": url, "content_type": "json"}}
        self.world(hooks=[hook], deliveries={"5": deliveries})
        return doctor.check_hook(loop, [hook], "reviewer", "widgets-review", url)

    @staticmethod
    def delivery(n, code, at):
        return {"id": n, "status_code": code, "delivered_at": at, "event": "pull_request",
                "status": "OK" if code < 300 else f"Invalid HTTP Response: {code}"}

    def test_latest_delivery_rejected_401_is_a_secret_mismatch(self):
        check = self.check([self.delivery(2, 401, "2026-09-02T00:00:00Z"),
                            self.delivery(1, 202, "2026-09-01T00:00:00Z")])
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("401", check.detail)
        self.assertIn("secret", check.detail)
        self.assertIn("uninstall --loop widgets", check.fix)
        self.assertIn("init", check.fix)

    def test_order_is_read_from_delivered_at_not_list_position(self):
        check = self.check([self.delivery(1, 202, "2026-09-01T00:00:00Z"),
                            self.delivery(2, 401, "2026-09-02T00:00:00Z")])
        self.assertEqual(check.status, doctor.MISMATCH)

    def test_a_403_is_a_refused_route(self):
        check = self.check([self.delivery(2, 403, "2026-09-02T00:00:00Z")])
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("403", check.detail)

    def test_a_later_success_clears_an_old_rejection(self):
        check = self.check([self.delivery(2, 202, "2026-09-02T00:00:00Z"),
                            self.delivery(1, 401, "2026-09-01T00:00:00Z")])
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("latest delivery 202", check.detail)

    def test_no_deliveries_yet_says_the_secret_is_unproven(self):
        check = self.check([])
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("no deliveries yet", check.detail)

    def test_an_unreadable_delivery_list_is_unknown_not_green(self):
        check = self.check("not a list")
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("deliveries", check.detail)


class ConfigErrorTest(Base):
    def test_unknown_loop_is_a_clean_refusal_for_every_verb(self):
        for verb in ("status", "arm", "uninstall"):
            rc, out = self.cli(verb, "--loop", "wdigets")
            self.assertEqual(rc, 2, (verb, out))
            self.assertIn("no loop config named 'wdigets'", out)

    def test_list_and_status_skip_a_broken_file_and_show_the_rest(self):
        (t.LOOPS_DIR / "broken.json").write_text("{not json")
        for verb in ("list", "status"):
            rc, out = self.cli(verb)
            self.assertEqual(rc, 2, (verb, out))
            self.assertIn("skipping broken.json", out)
            self.assertIn("widgets", out)

    def test_init_refuses_ids_that_cannot_name_a_config_file(self):
        for bad in (".", "..", "a/b", ".hidden"):
            rc, out = self.cli("init", "--repo", t.REPO, "--id", bad, "--host", t.HOST,
                               "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                               "--reviewer-profile", "reviewer-profile",
                               "--fixer-profile", "fixer-profile")
            self.assertEqual(rc, 2, (bad, out))
        self.assertEqual(sorted(p.name for p in t.LOOPS_DIR.iterdir()), ["widgets.json"])
        rc, out = self.cli("list")
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
