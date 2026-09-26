"""Token-file settings and the adjudicator comment login: paths only, never token values.

Stdlib only, disposable HOME/HERMES_HOME. The "tokens" are short obviously-fake sentinels; every
test ends by grepping everything the CLI printed and every file it wrote for them.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from review_loop import cli, config, doctor

ROOT = Path(__file__).resolve().parent.parent
READER, REV, FIX, ADJ = "reader-acct", "rev-acct", "fix-acct", "adj-acct"
SENTINEL = "pat-fixture"          # never a real token; anything containing it is a leak


class _Ctx:
    def register_cli_command(self, name, summary, setup, **kwargs):
        self.setup = setup


class TokenFileSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.hermes = self.root / "hermes"
        env = patch.dict(os.environ, {
            "HOME": str(self.home), "HERMES_HOME": str(self.hermes),
            "REVIEW_LOOP_CONFIG_DIR": str(self.hermes / "review-loops.d"),
            "REVIEW_LOOP_SUBS": str(self.hermes / "webhook_subscriptions.json")})
        env.start()
        self.addCleanup(env.stop)
        for profile in ("vex", "drey", "tuck"):
            (self.hermes / "profiles" / profile).mkdir(parents=True)
            (self.hermes / "profiles" / profile / "config.yaml").write_text("model: {}\n")
        self.keys = self.home / "keys"
        self.keys.mkdir(parents=True)
        self.token_files = []
        self.pats = {name: self.pat(name) for name in ("read", "rev", "fix", "adj", "rev2")}
        self.outputs = []
        self.addCleanup(setattr, cli, "_SETTINGS", getattr(cli, "_SETTINGS", {}))

    # -- helpers ---------------------------------------------------------------------------

    def pat(self, name, mode=0o600):
        path = self.keys / f"{name}.pat"
        path.write_text(f"{SENTINEL}-{name}\n")
        path.chmod(mode)
        self.token_files.append(path)
        return path

    def run_cli(self, argv, settings=None):
        ctx = _Ctx()
        cli.register_cli(ctx, settings=settings or {})
        parser = argparse.ArgumentParser(prog="hermes review-loop")
        ctx.setup(parser)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            args = parser.parse_args(argv)
            rc = args.func(args)
        text = out.getvalue() + err.getvalue()
        self.outputs.append(text)
        return rc, text

    def init_argv(self, *extra, adjudicator=True, tokens=True):
        argv = ["init", "--repo", "acme/widgets", "--fixer", FIX, "--reviewer", REV,
                "--reviewer-profile", "vex", "--fixer-profile", "drey",
                "--read-token", READER, "--host", "https://gateway.example"]
        if tokens:
            argv += ["--token", f"{READER}={self.pats['read']}",
                     "--token", f"{REV}={self.pats['rev']}",
                     "--token", f"{FIX}={self.pats['fix']}"]
        if adjudicator:
            argv += ["--adjudicator-route", "widgets-breach", "--adjudicator-profile", "tuck"]
        return argv + list(extra)

    def loop_file(self):
        return config.config_dir() / "widgets.json"

    def loop_json(self):
        return json.loads(self.loop_file().read_text())

    def install(self, *extra):
        rc, out = self.run_cli(self.init_argv(*extra))
        self.assertEqual(rc, 0, out)
        return out

    def assert_no_leak(self):
        """No sentinel in any output, and none in any file except the token files themselves."""
        for text in self.outputs:
            self.assertNotIn(SENTINEL, text)
        owned = {p.resolve() for p in self.token_files}
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.resolve() not in owned:
                self.assertNotIn(SENTINEL.encode(), path.read_bytes(), str(path))

    def assert_refused(self, argv, reason, settings=None):
        before = self.loop_file().read_bytes() if self.loop_file().exists() else None
        rc, out = self.run_cli(argv, settings)
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, reason)
        after = self.loop_file().read_bytes() if self.loop_file().exists() else None
        self.assertEqual(after, before, "a refusal must not write the loop config")
        return out

    # -- init --adjudicator-login -------------------------------------------------------------

    def test_init_adjudicator_login_happy_path(self):
        out = self.install("--adjudicator-login", ADJ, "--token", f"{ADJ}={self.pats['adj']}")
        data = self.loop_json()
        self.assertEqual(data["seats"]["adjudicator"]["login"], ADJ)
        self.assertEqual(data["tokens"][ADJ], str(self.pats["adj"]))
        self.assertIn(f"adjudicator {ADJ} → {self.pats['adj']}", out)
        rc, status = self.run_cli(["status", "--loop", "widgets"])
        self.assertEqual(rc, 0, status)
        self.assertIn(f"comments as {ADJ}", status)
        self.assertIn(f"adjudicator {ADJ} → {self.pats['adj']}", status)
        self.assert_no_leak()

    def test_init_adjudicator_login_refusals(self):
        adj_token = ["--token", f"{ADJ}={self.pats['adj']}"]
        self.assert_refused(self.init_argv("--adjudicator-login", ADJ), r"has no entry in 'tokens'")
        self.assert_refused(self.init_argv("--adjudicator-login", READER), r"also the reader")
        self.assert_refused(self.init_argv("--adjudicator-login", FIX), r"also the reader, a seat")
        self.assert_refused(self.init_argv("--adjudicator-login", ADJ,
                                           "--token", f"{ADJ}={self.pats['rev']}"),
                            r"same token file")
        loose = self.pat("loose", 0o644)
        self.assert_refused(self.init_argv("--adjudicator-login", ADJ,
                                           "--token", f"{ADJ}={loose}"),
                            r"group/other can read it")
        self.assert_refused(self.init_argv("--adjudicator-login", ADJ,
                                           "--token", f"{ADJ}={self.keys / 'gone.pat'}"),
                            r"does not exist")
        # A relative path is refused even when it happens to resolve from the current directory.
        cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)
        self.assert_refused(self.init_argv("--adjudicator-login", ADJ,
                                           "--token", f"{ADJ}=keys/adj.pat"),
                            r"not an absolute path")
        os.chdir(cwd)
        self.assert_refused([a for a in self.init_argv("--adjudicator-login", ADJ, *adj_token,
                                                       adjudicator=False)],
                            r"needs --adjudicator-route")
        self.assertFalse(self.loop_file().exists())
        self.assert_no_leak()

    # -- set --adjudicator-login --------------------------------------------------------------

    def test_set_adjudicator_login_happy_path_and_clear(self):
        self.install()
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-login", ADJ,
                                "--token", f"{ADJ}=~/keys/adj.pat"])
        self.assertEqual(rc, 0, out)
        data = self.loop_json()
        self.assertEqual(data["seats"]["adjudicator"]["login"], ADJ)
        self.assertEqual(data["tokens"][ADJ], str(self.pats["adj"]))   # ~ expanded
        self.assertIn(f"adjudicator login: (none) → {ADJ}", out)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-login", ""])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("adjudicator", self.loop_json().get("seats", {}))
        self.assertIn("operator only", out)
        self.assert_no_leak()

    def test_set_adjudicator_login_refusals(self):
        self.install()
        base = ["set", "--loop", "widgets", "--adjudicator-login"]
        self.assert_refused([*base, ADJ], r"has no entry in 'tokens'")
        self.assert_refused([*base, READER], r"also the reader")
        self.assert_refused([*base, REV, "--token", f"{REV}={self.pats['adj']}"],
                            r"also the reader, a seat")
        self.assert_refused([*base, ADJ, "--token", f"{ADJ}={self.pats['fix']}"],
                            r"same token file")
        loose = self.pat("loose", 0o640)
        self.assert_refused([*base, ADJ, "--token", f"{ADJ}={loose}"], r"group/other can read it")
        self.assert_refused([*base, ADJ, "--token", f"{ADJ}={self.keys / 'gone.pat'}"],
                            r"does not exist")
        self.assert_refused([*base, ADJ, "--token", f"{ADJ}=keys/adj.pat"],
                            r"not an absolute path")
        self.assert_refused(["set", "--loop", "widgets", "--token", f"{REV}={self.pats['rev2']}"],
                            r"only maps the adjudicator")
        self.assert_no_leak()

    # -- plugin settings → apply / init -----------------------------------------------------

    def test_apply_maps_settings_to_tokens(self):
        self.install()
        settings = {"reviewer_token_file": "~/keys/rev2.pat",
                    "adjudicator_login": ADJ, "adjudicator_token_file": str(self.pats["adj"])}
        before = self.loop_file().read_bytes()
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"], settings)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"reviewer token file ({REV}): {self.pats['rev']} → {self.pats['rev2']}", out)
        self.assertIn(f"adjudicator login: (none) → {ADJ}", out)
        self.assertEqual(self.loop_file().read_bytes(), before)

        # A seat whose token path moves is an identity change: in flight → --while-busy.
        with patch.object(cli, "_busy_seats", return_value=["reviewer is in flight on #7 (1m)"]):
            rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
            self.assertEqual(rc, 2, out)
            self.assertIn("--while-busy", out)
            self.assertEqual(self.loop_file().read_bytes(), before)
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--while-busy"], settings)
        self.assertEqual(rc, 0, out)
        data = self.loop_json()
        self.assertEqual(data["tokens"][REV], str(self.pats["rev2"]))
        self.assertEqual(data["tokens"][FIX], str(self.pats["fix"]))
        self.assertEqual(data["tokens"][ADJ], str(self.pats["adj"]))
        self.assertEqual(data["seats"]["adjudicator"]["login"], ADJ)
        rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
        self.assertIn("already matches", out)

        rc, shown = self.run_cli(["settings"], settings)
        self.assertEqual(rc, 0, shown)
        self.assertIn("token file ~/keys/rev2.pat", shown)
        self.assertIn(f"login {ADJ}", shown)
        self.assertIn(f"reviewer {REV} → {self.pats['rev2']}", shown)
        self.assert_no_leak()

    def test_apply_refuses_bad_token_paths_before_writing(self):
        self.install()
        loose = self.pat("loose", 0o604)
        cases = {"keys/rev2.pat": r"reviewer_token_file: .*not an absolute path",
                 str(self.keys / "gone.pat"): r"reviewer_token_file: .*does not exist",
                 str(loose): r"reviewer_token_file: .*group/other can read it",
                 str(self.keys): r"reviewer_token_file: .*not a regular file"}
        for value, reason in cases.items():
            with self.subTest(value=value):
                self.assert_refused(["apply", "--loop", "widgets"], reason,
                                    {"reviewer_token_file": value})
        self.assert_refused(["apply", "--loop", "widgets"], r"same token file",
                            {"adjudicator_login": ADJ,
                             "adjudicator_token_file": str(self.pats["fix"])})
        self.assert_refused(["apply", "--loop", "widgets"], r"same token file",
                            {"fixer_token_file": str(self.pats["rev"])})
        self.assert_refused(["apply", "--loop", "widgets"], r"also the reader",
                            {"adjudicator_login": READER,
                             "adjudicator_token_file": str(self.pats["adj"])})
        self.assert_refused(["apply", "--loop", "widgets"], r"no adjudicator login",
                            {"adjudicator_token_file": str(self.pats["adj"])})
        self.assert_no_leak()

    def test_new_loop_takes_token_files_from_settings(self):
        settings = {"reviewer_token_file": str(self.pats["rev"]),
                    "fixer_token_file": str(self.pats["fix"]),
                    "adjudicator_login": ADJ, "adjudicator_token_file": str(self.pats["adj"])}
        argv = self.init_argv(tokens=False) + ["--token", f"{READER}={self.pats['read']}"]
        rc, out = self.run_cli(argv, settings)
        self.assertEqual(rc, 0, out)
        data = self.loop_json()
        self.assertEqual(data["tokens"], {READER: str(self.pats["read"]), REV: str(self.pats["rev"]),
                                          FIX: str(self.pats["fix"]), ADJ: str(self.pats["adj"])})
        self.assertEqual(data["seats"]["adjudicator"]["login"], ADJ)
        self.assertFalse(self.loop_file().with_name("other.json").exists())
        # A group-readable path in the form refuses a new loop too, by the setting's name.
        self.assert_refused([*self.init_argv(), "--id", "other"], r"fixer_token_file: .*group",
                            {"fixer_token_file": str(self.pat("loose", 0o644))})
        self.assert_no_leak()

    # -- doctor ------------------------------------------------------------------------------

    def test_doctor_reports_token_file_path_exists_private(self):
        self.install("--adjudicator-login", ADJ, "--token", f"{ADJ}={self.pats['adj']}")
        loop = config.load_id("widgets")
        rev = doctor.check_credential(loop, "reviewer")
        self.assertEqual(rev.status, doctor.VERIFIED, rev.detail)
        self.assertIn(f"{self.pats['rev']} (exists: yes, private: yes)", rev.detail)
        adj = doctor.check_adjudicator_identity(loop)
        self.assertEqual(adj.status, doctor.VERIFIED, adj.detail)
        self.assertIn(f"{self.pats['adj']} (exists: yes, private: yes)", adj.detail)
        self.pats["fix"].chmod(0o644)
        self.pats["adj"].chmod(0o644)
        fix = doctor.check_credential(loop, "fixer")
        self.assertEqual(fix.status, doctor.MISMATCH)
        self.assertIn("private: no", fix.detail)
        self.assertEqual(doctor.check_adjudicator_identity(loop).status, doctor.MISMATCH)
        self.pats["fix"].unlink()
        missing = doctor.check_credential(loop, "fixer")
        self.assertEqual(missing.status, doctor.ABSENT)
        self.assertIn("exists: no", missing.detail)
        buf = io.StringIO()
        with redirect_stdout(buf):
            doctor.report(loop, [rev, adj, fix, missing])
        self.outputs.append(buf.getvalue())
        self.assert_no_leak()

    # -- the form and the code agree --------------------------------------------------------

    def test_settings_schema_and_plugin_yaml_agree(self):
        text = (ROOT / "plugin.yaml").read_text().split("\nconfig_schema:", 1)[1]
        manifest, current = {}, None
        for line in text.splitlines():
            if re.match(r"^  \S", line):
                current = line.strip().rstrip(":")
                manifest[current] = {}
            elif re.match(r"^    \S", line) and current:
                key, _, value = line.strip().partition(":")
                manifest[current][key.strip()] = value.strip().strip('"')
        self.assertEqual(sorted(manifest), sorted(config.SETTINGS_SCHEMA))
        for key in ("reviewer_token_file", "fixer_token_file", "adjudicator_login",
                    "adjudicator_token_file"):
            spec = config.SETTINGS_SCHEMA[key]
            self.assertEqual((spec["type"], spec["default"]), ("str", ""))
            self.assertEqual(manifest[key], {"label": spec["label"], "type": "str", "default": "",
                                             "description": spec["description"]})
            if key.endswith("_token_file"):
                self.assertIn("path only", spec["label"])
                self.assertIn("never the token itself", spec["description"])

    def test_token_file_problem_never_opens_the_file(self):
        with patch("builtins.open", side_effect=AssertionError("token file opened")), \
                patch.object(Path, "read_text", side_effect=AssertionError("token file read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("token file read")):
            self.assertEqual(config.token_file_problem(str(self.pats["rev"])), "")
            self.assertEqual(config.token_file_problem("~/keys/rev.pat"), "")
            self.assertIn("absolute", config.token_file_problem("keys/rev.pat"))


if __name__ == "__main__":
    unittest.main()
