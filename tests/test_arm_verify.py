"""`arm` reports what GitHub shows after the PATCH, and `init --schedule` fails when no job exists.

Issue #55 / #84: a refused PATCH used to print "hook 1 → paused" and exit 0; a failed
`cron create` printed an unquoted fallback and exited 0. Disposable config/state only.
"""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from review_loop import cli, config, gh

HOST = "https://gw.example"


def raw_loop(loop_id):
    return {"id": loop_id, "repo": f"owner/{loop_id}", "fixers": ["fixer"],
            "reviewers": ["reviewer"], "read_token": "reader", "host": HOST,
            "seats": {"reviewer": {"route": f"{loop_id}-review", "profile": "reviewer"},
                      "fixer": {"route": f"{loop_id}-fix", "profile": "fixer"}}}


class FakeGitHub:
    """Hooks by id; PATCH may be refused (403) or silently ignored."""

    def __init__(self, hooks, patch_error="", ignore_patch=False, list_error="", get_error=""):
        self.hooks = hooks
        self.patch_error = patch_error
        self.ignore_patch = ignore_patch
        self.list_error = list_error
        self.get_error = get_error
        self.calls = []

    def fetch(self, loop, path, method="GET", body=None, login=None):
        self.calls.append((method, path, login))
        if path.endswith("/hooks?per_page=100"):
            if self.list_error:
                return None, self.list_error
            return [dict(h) for h in self.hooks.values()], ""
        hook_id = int(path.rsplit("/", 1)[-1])
        if method == "PATCH":
            if self.patch_error:
                return None, self.patch_error
            if not self.ignore_patch:
                self.hooks[hook_id]["active"] = body["active"]
            return dict(self.hooks[hook_id]), ""
        if self.get_error:
            return None, self.get_error
        return dict(self.hooks[hook_id]), ""


def hooks(active):
    return {1: {"id": 1, "active": active,
                "config": {"url": f"{HOST}/p/reviewer/webhooks/widgets-review"}},
            2: {"id": 2, "active": active,
                "config": {"url": f"{HOST}/p/fixer/webhooks/widgets-fix"}},
            3: {"id": 3, "active": active,
                "config": {"url": "https://elsewhere.example/ci"}}}


class ArmVerifyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"REVIEW_LOOP_CONFIG_DIR": str(self.root / "configs"),
                                      "HERMES_HOME": str(self.root / "hermes")})
        env.start()
        self.addCleanup(env.stop)
        config.config_dir().mkdir()
        (config.config_dir() / "widgets.json").write_text(json.dumps(raw_loop("widgets")))

    def arm(self, fake, pause=False, admin_token="", loop="widgets"):
        args = argparse.Namespace(loop=loop, pause=pause, admin_token=admin_token)
        out = io.StringIO()
        with patch.object(gh, "fetch", fake.fetch), redirect_stdout(out):
            rc = cli.cmd_arm(args)
        return rc, out.getvalue()

    def test_refused_patch_reports_truth_and_fails(self):
        fake = FakeGitHub(hooks(True), patch_error="HTTP 403 Resource not accessible")
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("→ paused", out)
        self.assertIn("hook 1 is still active, not paused", out)
        self.assertIn("HTTP 403", out)
        self.assertEqual(out.count("--admin-token <owner login>"), 1, out)
        self.assertEqual([h["active"] for h in fake.hooks.values()], [True, True, True])
        self.assertNotIn(("PATCH", "/repos/owner/widgets/hooks/3", "reader"), fake.calls)

    def test_admin_token_names_that_login(self):
        fake = FakeGitHub(hooks(True), patch_error="HTTP 403")
        rc, out = self.arm(fake, pause=True, admin_token="owner")
        self.assertEqual(rc, 1)
        self.assertIn("'owner'", out)
        self.assertIn(("PATCH", "/repos/owner/widgets/hooks/1", "owner"), fake.calls)

    def test_accepted_but_unchanged_patch_fails(self):
        fake = FakeGitHub(hooks(True), ignore_patch=True)
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("read-back disagrees", out)

    def test_unreadable_read_back_fails(self):
        fake = FakeGitHub(hooks(False), get_error="HTTP 502")
        rc, out = self.arm(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT CONFIRMED active", out)
        self.assertIn("retry `arm`", out)

    def test_success_reports_read_back_state(self):
        fake = FakeGitHub(hooks(True))
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 1 → paused (read back)", out)
        self.assertIn("hook 2 → paused (read back)", out)
        self.assertTrue(fake.hooks[3]["active"])
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 1 already paused", out)

    def test_unreadable_listing_fails_with_fix(self):
        rc, out = self.arm(FakeGitHub(hooks(True), list_error="HTTP 404"))
        self.assertEqual(rc, 1, out)
        self.assertIn("could not read the repo's hooks", out)
        self.assertIn("--admin-token", out)

    def test_no_loop_hooks_fails(self):
        fake = FakeGitHub({3: hooks(True)[3]})
        rc, out = self.arm(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn("no loop hooks found", out)

    def test_no_loops_or_unknown_loop_fail(self):
        (config.config_dir() / "widgets.json").unlink()
        rc, out = self.arm(FakeGitHub({}), loop=None)
        self.assertEqual(rc, 2, out)
        rc, out = self.arm(FakeGitHub({}), loop="nope")
        self.assertEqual(rc, 2, out)


class ScheduleFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes")})
        env.start()
        self.addCleanup(env.stop)
        self.hermes = self.root / "bin dir" / "hermes"
        self.hermes.parent.mkdir()

    def fake_hermes(self, rc):
        self.hermes.write_text(f"#!/bin/sh\necho 'cron: store is locked' >&2\nexit {rc}\n")
        self.hermes.chmod(0o755)

    def test_failed_cron_create_is_not_ok_and_fallback_is_quoted(self):
        self.fake_hermes(1)
        with patch.object(cli.shutil, "which", return_value=str(self.hermes)):
            lines, ok = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertFalse(ok)
        self.assertIn("store is locked", lines[0])
        command = lines[1].split("run it yourself: ", 1)[1]
        self.assertEqual(shlex.split(command)[:6],
                         [str(self.hermes), "cron", "create", "15m", "--name",
                          "review loop watchdog (widgets)"])
        subprocess.run(["bash", "-n", "-c", command], check=True)

    def test_init_exits_nonzero_when_the_job_was_not_created(self):
        self.fake_hermes(1)
        hermes_home = self.root / "hermes"
        for profile in ("rp", "fp"):
            (hermes_home / "profiles" / profile).mkdir(parents=True)
            (hermes_home / "profiles" / profile / "config.yaml").write_text("model: x\n")
        tokens = self.root / "tokens"
        tokens.mkdir()
        for login in ("rv", "fx"):
            (tokens / login).write_text("dummy")
            (tokens / login).chmod(0o600)

        class Ctx:
            def register_cli_command(self, name, help_text, setup, **kw):
                self.setup = setup

        ctx = Ctx()
        cli.register_cli(ctx, {})
        parser = argparse.ArgumentParser()
        ctx.setup(parser)
        args = parser.parse_args([
            "init", "--repo", "acme/gadgets", "--fixer", "fx", "--reviewer", "rv",
            "--host", HOST, "--reviewer-profile", "rp", "--fixer-profile", "fp",
            "--token", f"rv={tokens / 'rv'}", "--token", f"fx={tokens / 'fx'}",
            "--schedule", "15m"])
        out = io.StringIO()
        with patch.dict(os.environ, {"REVIEW_LOOP_CONFIG_DIR": str(self.root / "configs")}), \
                patch.object(cli.shutil, "which", return_value=str(self.hermes)), \
                redirect_stdout(out):
            rc = args.func(args)
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("init INCOMPLETE", out.getvalue())
        self.assertNotIn("Next:", out.getvalue())

    def test_successful_cron_create_is_ok(self):
        self.fake_hermes(0)
        with patch.object(cli.shutil, "which", return_value=str(self.hermes)):
            lines, ok = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertTrue(ok, lines)


if __name__ == "__main__":
    unittest.main()
