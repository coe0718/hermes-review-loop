"""The reader is its own account: init, set and doctor hold the four-identity rule (#56).

Also runs README's own install command through ``init --dry-run`` — parsing it is not enough
(it parsed while every copy of it was refused), so the example is extracted from the file and
validated against a fixture home holding exactly the profiles and token files it names.

Stdlib only, disposable HOME/HERMES_HOME; the "tokens" are obviously-fake sentinels.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import unittest
from unittest.mock import patch

from review_loop import cli, config, doctor

ROOT = Path(__file__).resolve().parent.parent
READER, REV, FIX, ADJ = "reader-acct", "rev-acct", "fix-acct", "adj-acct"
SENTINEL = "pat-fixture"


class _Ctx:
    def register_cli_command(self, name, summary, setup, **kwargs):
        self.setup = setup


def readme_install_argv() -> list[str]:
    """The argv of README's ``hermes review-loop init`` example, continuations joined."""
    lines = (ROOT / "README.md").read_text().splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and line.strip().startswith("hermes review-loop init"):
            command = line.strip()
            while command.endswith("\\"):
                index += 1
                command = command[:-1] + " " + lines[index].strip()
            return shlex.split(re.sub(r"^hermes\s+review-loop\s+", "", command), comments=True)
    raise AssertionError("README.md has no fenced `hermes review-loop init` example")


class _Home(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.hermes = self.home / ".hermes"
        env = patch.dict(os.environ, {
            "HOME": str(self.home), "HERMES_HOME": str(self.hermes),
            "REVIEW_LOOP_CONFIG_DIR": str(self.hermes / "review-loops.d"),
            "REVIEW_LOOP_SUBS": str(self.hermes / "webhook_subscriptions.json")})
        env.start()
        self.addCleanup(env.stop)
        for profile in ("vex", "drey", "tuck"):
            (self.hermes / "profiles" / profile).mkdir(parents=True)
            (self.hermes / "profiles" / profile / "config.yaml").write_text("model: {}\n")
        self.keys = self.hermes / "keys"
        self.keys.mkdir(parents=True)
        self.addCleanup(setattr, cli, "_SETTINGS", getattr(cli, "_SETTINGS", {}))

    def pat(self, name, mode=0o600):
        path = self.keys / f"{name}-pat"
        path.write_text(f"{SENTINEL}-{name}\n")
        path.chmod(mode)
        return path

    def run_cli(self, argv):
        ctx = _Ctx()
        cli.register_cli(ctx, settings={})
        parser = argparse.ArgumentParser(prog="hermes review-loop")
        ctx.setup(parser)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            args = parser.parse_args(argv)
            rc = args.func(args)
        text = out.getvalue()
        self.assertNotIn(SENTINEL, text)
        return rc, text


class ReadmeInstallTests(_Home):
    def test_readme_install_command_passes_init_dry_run(self):
        argv = readme_install_argv()
        self.assertEqual(argv[0], "init")
        # Every token file the example maps, created the way the docs say (mode 600).
        for value in [argv[i + 1] for i, arg in enumerate(argv) if arg == "--token"]:
            login, path = value.split("=", 1)
            target = Path(path).expanduser()
            self.assertEqual(target.parent, self.keys, f"{value}: expected under ~/.hermes/keys")
            self.pat(target.name[:-len("-pat")])
        rc, out = self.run_cli([*argv, "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing written", out)
        self.assertFalse(config.config_dir().exists() and any(config.config_dir().iterdir()))

    def test_the_old_readme_shape_is_refused(self):
        """The shape README used to print (reader on the reviewer seat) stays refused."""
        self.pat("rev-bot")
        self.pat("dev-account")
        rc, out = self.run_cli(["init", "--repo", "owner/name", "--fixer", "dev-account",
                                "--reviewer", "rev-bot", "--fixer-profile", "drey",
                                "--reviewer-profile", "vex",
                                "--token", f"rev-bot={self.keys / 'rev-bot-pat'}",
                                "--token", f"dev-account={self.keys / 'dev-account-pat'}",
                                "--read-token", "rev-bot", "--host", "https://gw.example",
                                "--dry-run"])
        self.assertEqual(rc, 2, out)
        self.assertIn("also the reviewer seat", out)
        self.assertIn("four-identity rule", out)


class ReaderIdentityTests(_Home):
    def init_argv(self, *extra, reader=READER, reader_file="read"):
        argv = ["init", "--repo", "acme/widgets", "--fixer", FIX, "--reviewer", REV,
                "--reviewer-profile", "vex", "--fixer-profile", "drey",
                "--host", "https://gateway.example",
                "--token", f"{REV}={self.pat('rev')}", "--token", f"{FIX}={self.pat('fix')}"]
        if reader:
            argv += ["--read-token", reader]
        if reader and reader_file:
            argv += ["--token", f"{reader}={self.pat(reader_file)}"]
        return argv + list(extra)

    def loop_file(self):
        return config.config_dir() / "widgets.json"

    def refused(self, argv, reason):
        rc, out = self.run_cli(argv)
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, reason)
        return out

    def test_init_refuses_a_reader_that_is_a_seat(self):
        for seat, login in (("reviewer", REV), ("fixer", FIX)):
            out = self.refused(self.init_argv(reader=login, reader_file=""),
                               rf"the reader {login!r} is also the {seat} seat")
            self.assertIn("four-identity rule", out)
            self.assertFalse(self.loop_file().exists())

    def test_init_no_longer_defaults_the_reader_to_the_reviewer_seat(self):
        out = self.refused(self.init_argv(reader=""), r"--read-token LOGIN names the account")
        self.assertIn("four-identity rule", out)
        self.assertFalse(self.loop_file().exists())

    def test_init_refuses_a_reader_sharing_a_seat_token_file(self):
        self.refused(self.init_argv(reader_file="rev"), r"read the same token file")

    def test_init_refuses_a_reader_that_is_the_adjudicator_login(self):
        out = self.refused(self.init_argv("--adjudicator-route", "widgets-breach",
                                          "--adjudicator-profile", "tuck",
                                          "--adjudicator-login", READER),
                           r"also the reader")
        self.assertIn("four-identity rule", out)

    def test_init_refuses_an_admin_token_with_no_file(self):
        self.refused(self.init_argv("--hooks", "--admin-token", "owner-acct", "--dry-run"),
                     r"--admin-token 'owner-acct' has no token file")

    def test_distinct_reader_installs(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.loop_file().read_text())["read_token"], READER)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline"])
        self.assertIn(f"{READER} (mapped in tokens; its own account and file)", out)

    def install_legacy_shared_reader(self):
        """A loop written before this rule: the reader is the reviewer seat."""
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        data = json.loads(self.loop_file().read_text())
        data["read_token"] = REV
        self.loop_file().write_text(json.dumps(data))

    def test_doctor_flags_a_reader_on_a_seat(self):
        self.install_legacy_shared_reader()
        check = doctor.check_read_token(config.load_id("widgets"))
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("also the reviewer seat", check.detail)
        self.assertIn("four-identity rule", check.detail)
        self.assertIn("set --loop widgets --read-token", check.fix)

    def test_set_read_token_moves_the_reader_off_the_seat(self):
        self.install_legacy_shared_reader()
        new = self.pat("new-reader")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", "new-reader",
                                "--token", f"new-reader={new}"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"read_token: {REV} → new-reader", out)
        data = json.loads(self.loop_file().read_text())
        self.assertEqual(data["read_token"], "new-reader")
        self.assertEqual(data["tokens"]["new-reader"], str(new))
        self.assertEqual(doctor.check_read_token(config.load_id("widgets")).status, doctor.VERIFIED)
        # An already-mapped login needs no --token.
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", READER])
        self.assertEqual(rc, 0, out)

    def test_set_read_token_refusals(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        before = self.loop_file().read_bytes()
        out = self.refused(["set", "--loop", "widgets", "--read-token", FIX],
                           r"also the fixer seat")
        self.assertIn("four-identity rule", out)
        self.refused(["set", "--loop", "widgets", "--read-token", "ghost"],
                     r"no token file mapped for the reader 'ghost'")
        self.refused(["set", "--loop", "widgets", "--read-token", "twin",
                      "--token", f"twin={self.keys / 'fix-pat'}"], r"read the same token file")
        loose = self.pat("loose", 0o644)
        self.refused(["set", "--loop", "widgets", "--read-token", "loose",
                      "--token", f"loose={loose}"], r"group/other can read it")
        self.refused(["set", "--loop", "widgets", "--token", f"{REV}={self.keys / 'rev-pat'}"],
                     r"only maps the token file of the login named by --read-token")
        self.assertEqual(self.loop_file().read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
