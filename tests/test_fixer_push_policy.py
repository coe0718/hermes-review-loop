"""Explicit per-repository unattended push policy, using disposable config/state only."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from review_loop import cli, config


def raw_loop(loop_id):
    return {"id": loop_id, "repo": f"owner/{loop_id}", "fixers": ["fixer"],
            "reviewers": ["reviewer"], "seats": {
                "reviewer": {"route": f"{loop_id}-review", "profile": "reviewer"},
                "fixer": {"route": f"{loop_id}-fix", "profile": "fixer"}}}


class FixerPushPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"REVIEW_LOOP_CONFIG_DIR": str(self.root / "configs"),
                                   "HERMES_HOME": str(self.root / "hermes")})
        env.start()
        self.addCleanup(env.stop)
        config.config_dir().mkdir()
        self.first = config.config_dir() / "one.json"
        self.second = config.config_dir() / "two.json"
        self.first.write_text(json.dumps(raw_loop("one")))
        self.second.write_text(json.dumps(raw_loop("two")))

    def invoke(self, *, loop="one", enable=False, ack=False, dry=False):
        args = argparse.Namespace(loop=loop, enable=enable, disable=not enable,
                                  acknowledge_pr_race=ack, dry_run=dry)
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cli.cmd_fixer_push(args)
        return rc, output.getvalue()

    def test_legacy_and_false_values_are_off_and_other_types_refused(self):
        self.assertFalse(config.unattended_fixer_push_enabled(config.load_id("one")))
        self.assertFalse(config.unattended_fixer_push_enabled({"unattended_fixer_push": 1}))
        for value in (1, "true", None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(config.ConfigError, "JSON boolean"):
                    config.normalize({**raw_loop("one"), "unattended_fixer_push": value})
        self.assertFalse(config.unattended_fixer_push_enabled(
            config.normalize({**raw_loop("one"), "unattended_fixer_push": False})))

    def test_requires_ack_and_single_loop_and_round_trip_disable(self):
        original = self.first.read_bytes()
        other = self.second.read_bytes()
        rc, out = self.invoke(enable=True)
        self.assertEqual(rc, 2)
        self.assertIn("residual PR-metadata/ref race", out)
        self.assertEqual(self.first.read_bytes(), original)
        rc, _ = self.invoke(enable=True, ack=True, dry=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.first.read_bytes(), original)
        with patch.object(cli, "_busy_seats", return_value=[]):
            rc, out = self.invoke(enable=True, ack=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(config.unattended_fixer_push_enabled(config.load_id("one")))
        self.assertEqual(self.second.read_bytes(), other)
        self.assertTrue(json.loads(self.first.read_text())["unattended_fixer_push"])
        rc, out = self.invoke(enable=False)
        self.assertEqual(rc, 0, out)
        self.assertFalse(config.unattended_fixer_push_enabled(config.load_id("one")))
        self.assertIs(json.loads(self.first.read_text())["unattended_fixer_push"], False)

    def test_busy_fixer_refuses_without_writing(self):
        original = self.first.read_bytes()
        with patch.object(cli, "_busy_seats", return_value=["fixer on PR 4"]):
            rc, out = self.invoke(enable=True, ack=True)
        self.assertEqual(rc, 2)
        self.assertIn("in flight", out)
        self.assertEqual(self.first.read_bytes(), original)

    def test_plugin_settings_cannot_arm_existing_or_new_loop(self):
        raw = raw_loop("one")
        settings = {"unattended_fixer_push": True}
        self.assertFalse(config.unattended_fixer_push_enabled(
            config.normalize(config.apply_settings(raw, settings))))
        armed = {**raw, "unattended_fixer_push": True}
        self.assertTrue(config.unattended_fixer_push_enabled(
            config.normalize(config.apply_settings(armed, settings))))
        self.assertNotIn("unattended_fixer_push", config.SETTINGS_SCHEMA)

    def test_cli_parser_requires_exact_loop_and_one_direction(self):
        class Ctx:
            def register_cli_command(self, name, summary, setup, **kwargs):
                self.setup = setup
        ctx = Ctx()
        cli.register_cli(ctx)
        parser = argparse.ArgumentParser()
        ctx.setup(parser)
        args = parser.parse_args(["fixer-push", "--loop", "one", "--enable",
                                  "--acknowledge-pr-race"])
        self.assertIs(args.func, cli.cmd_fixer_push)
        for argv in (["fixer-push", "--enable"], ["fixer-push", "--loop", "one"],
                     ["fixer-push", "--loop", "one", "--enable", "--disable"]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()), \
                    patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
