"""Issue #105: every loop route's script must resolve where the gateway actually looks.

Hermes's webhook gateway runs a route's bare ``script`` from ``get_hermes_home()/scripts`` of the
profile the route is served under (``_profile_scope`` → ``get_profile_dir(profile)``), and refuses
anything that resolves outside that directory. These tests install loops into a disposable
HERMES_HOME laid out like a real one (root + ``profiles/<name>``) and then resolve every route the
way the gateway does: with a local copy of ``_resolve_script_path`` always, and with Hermes's own
function too when its source is available (``HERMES_AGENT_SOURCE``, default
``~/.hermes/hermes-agent``) — run in a subprocess whose HOME and HERMES_HOME are the disposable
ones, never the live install.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import cli, config, doctor, routes  # noqa: E402

SOURCE = pathlib.Path(os.environ.get("HERMES_AGENT_SOURCE")
                      or pathlib.Path.home() / ".hermes/hermes-agent")
FIX, REV, READER = "fix-acct", "rev-acct", "reader-acct"


def gateway_home(root: pathlib.Path, profile) -> pathlib.Path:
    """``hermes_cli.profiles.get_profile_dir``: the root for ``default``, else profiles/<name>."""
    profile = profile if isinstance(profile, str) and profile.strip() else "default"
    return root if profile == "default" else root / "profiles" / profile


def gateway_resolve(home: pathlib.Path, script_value):
    """``webhook_filters._resolve_script_path`` (pinned f84db42a) with get_hermes_home() = home."""
    if not isinstance(script_value, str) or not script_value.strip():
        return None, "script path is empty"
    scripts_root = (home / "scripts").resolve()
    raw_text = os.path.expandvars(script_value.strip())
    if raw_text == "~/.hermes" or raw_text.startswith("~/.hermes/"):
        candidate = (home / raw_text[len("~/.hermes/"):]).resolve()
    else:
        raw = pathlib.Path(raw_text).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (scripts_root / raw).resolve()
    if not candidate.is_relative_to(scripts_root):
        return None, f"script path resolves outside {scripts_root}"
    if not candidate.exists():
        return None, f"script not found: {candidate}"
    return (candidate, None) if candidate.is_file() else (None, f"script path is not a file: {candidate}")


_REAL = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from gateway.platforms.webhook_filters import _resolve_script_path
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    out = []
    for profile, script in json.loads(sys.argv[2]):
        # What WebhookAdapter._profile_scope -> gateway.run._profile_runtime_scope does to the home.
        token = set_hermes_home_override(str(get_profile_dir(profile)))
        try:
            path, error = _resolve_script_path(script)
        finally:
            reset_hermes_home_override(token)
        out.append([str(path) if path else None, error])
    print(json.dumps(out))
""")


def real_python():
    venv = SOURCE / "venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def real_resolver_available() -> bool:
    return (SOURCE / "gateway" / "platforms" / "webhook_filters.py").exists()


def real_resolve(home_env: dict, pairs) -> list:
    """Hermes's own resolver, per (profile, script), under the disposable HOME/HERMES_HOME."""
    proc = subprocess.run([real_python(), "-c", _REAL, str(SOURCE), json.dumps(pairs)],
                          capture_output=True, text=True, timeout=120, env=home_env,
                          cwd=home_env["HOME"])
    if proc.returncode != 0:
        raise unittest.SkipTest(f"Hermes resolver not importable here: {proc.stderr[-300:]}")
    return json.loads(proc.stdout)


class _Ctx:
    def register_cli_command(self, name, summary, setup, **kwargs):
        self.setup = setup


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = pathlib.Path(temp.name).resolve()
        self.home = self.tmp / "home"
        self.hermes = self.home / ".hermes"           # the classic layout: root under $HOME
        self.env = {"HOME": str(self.home), "HERMES_HOME": str(self.hermes),
                    "REVIEW_LOOP_CONFIG_DIR": str(self.hermes / "review-loops.d"),
                    "REVIEW_LOOP_SUBS": str(self.hermes / "webhook_subscriptions.json")}
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hermes.mkdir(parents=True)
        (self.hermes / "config.yaml").write_text("model: {}\n")
        for profile in ("vex", "drey", "tuck"):
            (self.hermes / "profiles" / profile).mkdir(parents=True)
            (self.hermes / "profiles" / profile / "config.yaml").write_text("model: {}\n")
        keys = self.home / "keys"
        keys.mkdir()
        self.pats = {}
        for name in ("read", "rev", "fix"):
            path = keys / f"{name}.pat"
            path.write_text(f"pat-fixture-{name}\n")
            path.chmod(0o600)
            self.pats[name] = path
        self.addCleanup(setattr, cli, "_SETTINGS", getattr(cli, "_SETTINGS", {}))

    def run_cli(self, argv, settings=None):
        ctx = _Ctx()
        cli.register_cli(ctx, settings=settings or {})
        parser = argparse.ArgumentParser(prog="hermes review-loop")
        ctx.setup(parser)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            args = parser.parse_args(argv)
            rc = args.func(args)
        return rc, out.getvalue()

    def init_argv(self, repo="acme/widgets", *extra):
        return ["init", "--repo", repo, "--fixer", FIX, "--reviewer", REV,
                "--reviewer-profile", "vex", "--fixer-profile", "drey",
                "--read-token", READER, "--host", "https://gateway.example",
                "--token", f"{READER}={self.pats['read']}",
                "--token", f"{REV}={self.pats['rev']}",
                "--token", f"{FIX}={self.pats['fix']}", *extra]

    def install(self, repo="acme/widgets", *extra):
        rc, out = self.run_cli(self.init_argv(repo, *extra))
        self.assertEqual(rc, 0, out)
        return out

    def full_install(self):
        return self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                            "--adjudicator-profile", "tuck", "--observer-profile", "default")

    def loop_routes(self, loop_id="widgets") -> dict:
        path = pathlib.Path(self.env["REVIEW_LOOP_SUBS"])
        registry = json.loads(path.read_text()) if path.exists() else {}
        return {name: entry for name, entry in registry.items() if name.startswith(loop_id + "-")}


class GatewayResolvesEveryRoute(Base):
    def test_init_puts_every_gate_where_the_gateway_looks(self):
        self.full_install()
        entries = self.loop_routes()
        self.assertEqual(set(entries), {"widgets-review", "widgets-fix", "widgets-breach",
                                        "widgets-observe"})
        for name, entry in entries.items():
            home = gateway_home(self.hermes, entry.get("profile", "default"))
            path, error = gateway_resolve(home, entry["script"])
            self.assertIsNone(error, f"{name}: the gateway would drop every event: {error}")
            self.assertFalse((home / "scripts" / entry["script"]).is_symlink())
        # Each seat's gate lives in that seat's own profile home, not the root's.
        self.assertTrue((self.hermes / "profiles/vex/scripts/gate_reviewer.py").is_file())
        self.assertTrue((self.hermes / "profiles/drey/scripts/gate_fixer.py").is_file())
        self.assertTrue((self.hermes / "profiles/tuck/scripts/gate_adjudicator.py").is_file())
        self.assertTrue((self.hermes / "scripts/observe.py").is_file())

    def test_hermes_own_resolver_agrees(self):
        if not real_resolver_available():
            self.skipTest(f"no Hermes source at {SOURCE}")
        self.full_install()
        entries = self.loop_routes()
        pairs = [[entry.get("profile", "default"), entry["script"]] for entry in entries.values()]
        real = real_resolve({**os.environ, **self.env}, pairs)
        for (profile, script), (path, error) in zip(pairs, real):
            self.assertIsNone(error, f"{profile}/{script}: {error}")
            self.assertEqual(pathlib.Path(path),
                             (gateway_home(self.hermes, profile) / "scripts" / script).resolve())

    def test_local_resolver_matches_hermes_on_the_edge_cases(self):
        """The copy doctor uses must fail exactly where the gateway fails."""
        if not real_resolver_available():
            self.skipTest(f"no Hermes source at {SOURCE}")
        from review_loop import gate_shims
        scripts = self.hermes / "profiles" / "vex" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "real.py").write_text("print(1)\n")
        (scripts / "adir").mkdir()
        outside = self.tmp / "outside.py"
        outside.write_text("print(1)\n")
        (scripts / "link.py").symlink_to(outside)
        (scripts / "inner-link.py").symlink_to(scripts / "real.py")
        cases = ["real.py", "missing.py", "adir", "link.py", "inner-link.py", "../config.yaml",
                 str(outside), str(scripts / "real.py"), "~/.hermes/profiles/vex/scripts/real.py",
                 "", "  "]
        pairs = [[profile, case] for profile in ("vex", "default") for case in cases]
        real = real_resolve({**os.environ, **self.env}, pairs)
        for (profile, case), want in zip(pairs, real):
            home = gateway_home(self.hermes, profile)
            for ours in (gate_shims.resolve(home, case), gateway_resolve(home, case)):
                got = [str(ours[0]) if ours[0] else None, ours[1]]
                self.assertEqual(got, want, f"{profile}: {case!r}")

    def test_dry_run_names_the_shims_and_writes_none(self):
        rc, out = self.run_cli(self.init_argv("acme/widgets"))  # a real loop for the id
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(self.init_argv("acme/gizmos", "--id", "gizmos",
                                              "--reviewer-profile", "tuck", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"would write: {self.hermes / 'profiles/tuck/scripts/gate_reviewer.py'}", out)
        self.assertFalse((self.hermes / "profiles/tuck/scripts").exists())

    def test_foreign_file_is_refused_before_anything_is_written(self):
        scripts = self.hermes / "profiles" / "vex" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "gate_reviewer.py").write_text("print('mine')\n")
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 2, out)
        self.assertIn("not written by hermes-review-loop", out)
        self.assertEqual((scripts / "gate_reviewer.py").read_text(), "print('mine')\n")
        self.assertFalse((config.config_dir() / "widgets.json").exists())
        self.assertEqual(self.loop_routes(), {})

    def test_init_is_idempotent_and_rewrites_a_stale_shim(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        before = shim.stat()
        self.assertEqual(gate_shims.install(config.load_id("widgets")), [])
        self.assertEqual(shim.stat().st_mtime_ns, before.st_mtime_ns)
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER, target="/old/plugin/x.py"))
        lines = gate_shims.install(config.load_id("widgets"))
        self.assertEqual(lines, [f"gate shim rewrote: {shim}"])
        self.assertEqual(shim.read_text(), gate_shims.render("gate_reviewer.py"))


class ShimRunsThePluginScript(Base):
    def stub(self) -> pathlib.Path:
        stub = self.tmp / "gh-stub"
        stub.write_text("#!/bin/sh\necho '{}'\n")
        stub.chmod(0o755)
        return stub

    def sh(self, argv, cwd, payload: str):
        env = {**os.environ, **self.env, "REVIEW_LOOP_GH_STUB": str(self.stub())}
        return subprocess.run(argv, input=payload, capture_output=True, text=True, cwd=cwd,
                              env=env, timeout=60)

    def test_shim_is_indistinguishable_from_the_plugin_script(self):
        self.full_install()
        observed = {"_observer": {"message": "PR #7 opened", "event": "opened", "loop": "widgets",
                                  "pr": 7}, "noise": "dropped"}
        cases = [
            ("observe.py", self.hermes, json.dumps(observed)),
            ("observe.py", self.hermes, json.dumps({"action": "opened"})),
            ("gate_reviewer.py", self.hermes / "profiles/vex",
             json.dumps({"action": "synchronize", "repository": {"full_name": "acme/widgets"},
                         "pull_request": {"number": 7, "head": {"sha": "a" * 40}}})),
            ("gate_fixer.py", self.hermes / "profiles/drey", "not json"),
            ("gate_adjudicator.py", self.hermes / "profiles/tuck",
             json.dumps({"action": "opened", "repository": {"full_name": "acme/unknown"}})),
        ]
        for script, home, payload in cases:
            with self.subTest(script=script, payload=payload[:30]):
                shim = home / "scripts" / script
                real = ROOT / "scripts" / script
                via_shim = self.sh([sys.executable, str(shim)], shim.parent, payload)
                direct = self.sh([sys.executable, str(real)], real.parent, payload)
                self.assertEqual((via_shim.returncode, via_shim.stdout),
                                 (direct.returncode, direct.stdout))
                if direct.returncode == 0:
                    self.assertEqual(via_shim.stderr, direct.stderr)
        narrowed = self.sh([sys.executable, str(self.hermes / "scripts/observe.py")],
                            self.hermes / "scripts", json.dumps(observed))
        self.assertEqual(json.loads(narrowed.stdout)["_observer"]["message"], "PR #7 opened")

    def test_file_path_argv_cwd_stdin_and_exit_code(self):
        from review_loop import gate_shims
        plugin = self.tmp / "plugin" / "scripts"
        plugin.mkdir(parents=True)
        probe = plugin / "probe.py"
        probe.write_text(textwrap.dedent("""
            import json, os, sys
            print(json.dumps({"file": __file__, "name": __name__, "path0": sys.path[0],
                              "argv0": sys.argv[0], "cwd": os.getcwd(),
                              "stdin": sys.stdin.read(),
                              "main": sys.modules["__main__"].__dict__.get("__file__")}))
            sys.exit(3)
        """))
        shim_dir = self.tmp / "profile" / "scripts"
        shim_dir.mkdir(parents=True)
        shim = shim_dir / "probe.py"
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER, target=str(probe)))
        via_shim = self.sh([sys.executable, str(shim)], shim_dir, "payload-bytes")
        direct = self.sh([sys.executable, str(probe)], plugin, "payload-bytes")
        self.assertEqual(via_shim.returncode, 3, via_shim.stderr)
        self.assertEqual(json.loads(via_shim.stdout), json.loads(direct.stdout))
        self.assertEqual(json.loads(direct.stdout)["file"], str(probe))

    def test_missing_plugin_script_fails_loudly(self):
        from review_loop import gate_shims
        shim = self.tmp / "shim.py"
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER,
                                               target=str(self.tmp / "gone.py")))
        proc = self.sh([sys.executable, str(shim)], self.tmp, "{}")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("gate script missing", proc.stderr)


class DoctorApplyUninstall(Base):
    def gateway_checks(self, loop_id="widgets"):
        loop = config.load_id(loop_id)
        return {c.name: c for c in doctor.check_loop(loop, offline=True)
                if c.name.startswith("gateway-script:")}

    def test_doctor_verifies_every_route_of_a_full_install(self):
        self.full_install()
        checks = self.gateway_checks()
        self.assertEqual(set(checks), {f"gateway-script:widgets-{r}"
                                       for r in ("review", "fix", "breach", "observe")})
        self.assertTrue(all(c.status == doctor.VERIFIED for c in checks.values()), checks)
        (self.hermes / "scripts" / "observe.py").unlink()
        self.assertEqual(self.gateway_checks()["gateway-script:widgets-observe"].status,
                         doctor.ABSENT)

    def test_doctor_resolves_like_the_gateway_and_apply_repairs(self):
        # (No observer here: `apply` on an observer loop trips an unrelated route-bind check.)
        self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                     "--adjudicator-profile", "tuck")
        checks = self.gateway_checks()
        self.assertEqual(set(checks), {f"gateway-script:widgets-{r}"
                                       for r in ("review", "fix", "breach")})
        self.assertTrue(all(c.status == doctor.VERIFIED for c in checks.values()), checks)

        shim = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        shim.unlink()
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertIn(f"script not found: {shim}", check.detail)
        self.assertIn("hermes review-loop apply --loop widgets", check.fix)

        rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"would write: {shim}", out)
        self.assertFalse(shim.exists())
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"gate shim wrote: {shim}", out)
        self.assertEqual(self.gateway_checks()["gateway-script:widgets-review"].status,
                         doctor.VERIFIED)

        shim.write_text("print('someone else')\n")
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.MISMATCH)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertEqual(shim.read_text(), "print('someone else')\n")

        link = self.hermes / "profiles/drey/scripts/gate_fixer.py"
        link.unlink()
        link.symlink_to(ROOT / "scripts" / "gate_fixer.py")
        check = self.gateway_checks()["gateway-script:widgets-fix"]
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertIn("resolves outside", check.detail)

    def test_doctor_follows_the_route_profile_in_the_registry(self):
        self.install()
        routes.new_route("widgets-review", profile="tuck", prompt=routes.route("widgets-review")["prompt"],
                         events=["pull_request"], script="gate_reviewer.py", deliver="discord",
                         host="https://gateway.example")
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertIn(str(self.hermes / "profiles/tuck/scripts/gate_reviewer.py"), check.detail)

    def test_uninstall_keeps_shims_another_loop_needs(self):
        self.install("acme/widgets")
        self.install("acme/gizmos", "--id", "gizmos", "--fixer-profile", "tuck")
        vex = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        drey = self.hermes / "profiles/drey/scripts/gate_fixer.py"
        tuck = self.hermes / "profiles/tuck/scripts/gate_fixer.py"
        rc, out = self.run_cli(["uninstall", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(vex.is_file(), "gizmos still routes its reviewer through vex")
        self.assertFalse(drey.exists())
        self.assertIn(f"gate shim removed: {drey}", out)
        vex.write_text("print('hand edited')\n")      # not ours any more: never removed
        rc, out = self.run_cli(["uninstall", "--loop", "gizmos"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(tuck.exists())
        self.assertEqual(vex.read_text(), "print('hand edited')\n")

    def test_watchdog_heal_restores_a_deleted_shim(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/drey/scripts/gate_fixer.py"
        shim.unlink()
        lines = gate_shims.heal(config.load_id("widgets"))
        self.assertTrue(shim.is_file())
        self.assertIn("restored 1 gate shim", lines[0])
        self.assertEqual(gate_shims.heal(config.load_id("widgets")), [])
        # The heal follows the registry, not the config: no route, nothing for the gateway to run.
        routes.remove_route("widgets-fix")
        shim.unlink()
        self.assertEqual(gate_shims.heal(config.load_id("widgets")), [])
        self.assertFalse(shim.exists())

    def test_gate_names_agree(self):
        from review_loop import gate_shims
        self.assertEqual(gate_shims.GATE_SCRIPT, cli.GATE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
