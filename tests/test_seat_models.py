"""Issue #32: each seat's isolated turn runs its own Hermes profile's model, provider and key.

No real Hermes, credentials, ~/.hermes or network: a throwaway HERMES_HOME holds fake profiles,
and a fake ``hermes_cli`` package (the same entry points the resolver imports from the real
Hermes source tree) resolves them. The model upstream is never contacted.
"""
import argparse
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import cli, config, doctor, gh, seat_model, trusted_turn  # noqa: E402
from review_loop.run_supervisor import Supervisor  # noqa: E402

HEAD = "a" * 40
KEYS = {"rev": "REVIEWER-PROFILE-KEY-1111", "fix": "FIXER-PROFILE-KEY-2222",
        "adj": "ADJUDICATOR-PROFILE-KEY-3333"}

# A miniature of the Hermes entry points the resolver uses. Resolution reads only HERMES_HOME
# and the environment load_hermes_dotenv fills from the profile's .env, like the real one.
FAKE_HERMES = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/env_loader.py": """
        import os
        def load_hermes_dotenv(*, hermes_home=None, **_):
            path = os.path.join(str(hermes_home), ".env")
            if os.path.exists(path):
                for line in open(path):
                    if "=" in line:
                        name, value = line.strip().split("=", 1)
                        os.environ[name] = value
    """,
    "hermes_cli/config.py": """
        import json, os
        def load_config():
            with open(os.path.join(os.environ["HERMES_HOME"], "config.yaml")) as handle:
                return json.load(handle)
    """,
    "hermes_cli/auth.py": """
        class _P:
            def __init__(self, auth_type):
                self.auth_type = auth_type
        PROVIDER_REGISTRY = {"deepseek": _P("api_key"), "someoauth": _P("oauth_device_code")}
    """,
    "hermes_cli/runtime_provider.py": """
        import os
        from hermes_cli.config import load_config
        def _get_model_config():
            return dict(load_config().get("model") or {})
        def resolve_requested_provider(requested=None):
            return str(_get_model_config().get("provider") or "auto").lower()
        def resolve_runtime_provider(**_):
            open(os.path.join(os.environ["HERMES_HOME"], "resolved.marker"), "w").close()
            cfg = _get_model_config()
            provider = resolve_requested_provider()
            if provider == "openrouter":
                key = os.environ.get("OPENROUTER_API_KEY", "")
                if not key:
                    raise RuntimeError("No usable credentials found for provider 'openrouter'.")
                return {"provider": "openrouter", "api_mode": "chat_completions",
                        "base_url": "https://openrouter.test/api/v1", "api_key": key}
            if provider == "deepseek":
                return {"provider": "deepseek", "api_mode": "chat_completions",
                        "base_url": cfg.get("base_url", ""), "api_key": os.environ.get("DEEPSEEK_API_KEY", "")}
            if provider == "geminiish":
                return {"provider": "geminiish", "api_mode": "gemini_native",
                        "base_url": "https://gemini.test", "api_key": "G-KEY-0000"}
            if provider.startswith("custom:"):
                name = provider.split(":", 1)[1]
                for entry in load_config().get("custom_providers") or []:
                    if entry["name"].lower() == name:
                        return {"provider": "custom", "api_mode": "chat_completions",
                                "base_url": entry["base_url"], "api_key": entry["api_key"]}
            raise RuntimeError("Unknown provider " + provider)
    """,
    "hermes_cli/model_catalog.py": """
        def _get_provider_block(provider):
            if provider == "openrouter":
                return {"models": [{"id": "vendor/model-a"}, {"id": "vendor/model-b"}]}
            return None
        def _block_ids(block):
            return [(m["id"], m) for m in (block or {}).get("models", [])]
    """,
}


def write_profile(home: pathlib.Path, name: str, model: dict, env: dict | None = None,
                  extra: dict | None = None) -> pathlib.Path:
    path = home if name == "default" else home / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(json.dumps({"model": model, **(extra or {})}))
    if env:
        (path / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        (path / ".env").chmod(0o600)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.home = self.root / "hermes"
        self.home.mkdir()
        source = self.root / "hermes-agent"
        for name, body in FAKE_HERMES.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(textwrap.dedent(body))
        venv = self.root / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(sys.executable)
        self.settings = {"source": str(source), "venv": str(venv),
                         "runtime": str(self.root), "rust": str(self.root)}
        write_profile(self.home, "rev", {"default": "vendor/rev-model", "provider": "openrouter"},
                      {"OPENROUTER_API_KEY": KEYS["rev"]})
        write_profile(self.home, "fix", {"default": "fix-model", "provider": "custom:acme",
                                         "base_url": "https://acme.test/v1"},
                      extra={"custom_providers": [{"name": "Acme", "base_url": "https://acme.test/v1",
                                                   "api_key": KEYS["fix"]}]})
        write_profile(self.home, "adj", {"default": "deepseek-chat", "provider": "deepseek",
                                         "base_url": "https://api.deepseek.test/v1"},
                      {"DEEPSEEK_API_KEY": KEYS["adj"]})
        write_profile(self.home, "default", {"default": "claude-x", "provider": "bedrock"})
        self.loop = {"id": "demo", "repo": "acme/widgets", "base": "main", "state_dir": str(self.root / "state"),
                     "read_token": "reader", "tokens": {},
                     "seats": {"reviewer": {"profile": "rev", "login": "reviewer"},
                               "fixer": {"profile": "fix", "login": "fixer"}},
                     "adjudicator": {"route": "breach", "profile": "adj"}}
        # The parent process holds a provider key of its own (Hermes loads the launch profile's
        # .env into os.environ): it must never satisfy a seat's resolution.
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")}
        env.update(HERMES_HOME=str(self.home), OPENROUTER_API_KEY="PARENT-PROCESS-LEAK-9999")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def key_file(self, name: str, value: str) -> str:
        path = self.root / name
        path.write_text(value + "\n")
        path.chmod(0o600)
        return str(path)


class ProfileResolution(Base):
    def test_each_seat_gets_its_own_profile_model_provider_and_key(self):
        got = {seat: seat_model.resolve_seat(self.loop, seat, self.settings)
               for seat in ("reviewer", "fixer", "adjudicator")}
        self.assertEqual(
            {seat: (i.origin, i.profile, i.provider, i.model, i.upstream, i.key) for seat, i in got.items()},
            {"reviewer": ("profile", "rev", "openrouter", "vendor/rev-model",
                          "https://openrouter.test/api/v1/chat/completions", KEYS["rev"]),
             "fixer": ("profile", "fix", "custom:acme", "fix-model",
                       "https://acme.test/v1/chat/completions", KEYS["fix"]),
             "adjudicator": ("profile", "adj", "deepseek", "deepseek-chat",
                             "https://api.deepseek.test/v1/chat/completions", KEYS["adj"])})
        self.assertEqual(len({i.identity() for i in got.values()}), 3)
        for inference in got.values():
            self.assertNotIn(inference.key, repr(inference))
            self.assertNotIn(inference.key, inference.describe())

    def test_parent_environment_key_never_satisfies_a_seat(self):
        (self.home / "profiles" / "rev" / ".env").unlink()
        with self.assertRaises(seat_model.SeatModelError) as caught:
            seat_model.resolve_seat(self.loop, "reviewer", self.settings)
        self.assertIn("No usable credentials", str(caught.exception))
        self.assertNotIn("PARENT-PROCESS-LEAK", str(caught.exception))

    def test_unsupported_providers_are_refused_before_credentials_are_touched(self):
        for provider in ("bedrock", "copilot", "someoauth", "auto"):
            with self.subTest(provider=provider):
                path = write_profile(self.home, "rev", {"default": "m", "provider": provider})
                with self.assertRaisesRegex(seat_model.SeatModelError, "rev"):
                    seat_model.resolve_seat(self.loop, "reviewer", self.settings)
                self.assertFalse((path / "resolved.marker").exists())

    def test_unknown_api_mode_is_refused(self):
        write_profile(self.home, "rev", {"default": "m", "provider": "geminiish"})
        with self.assertRaisesRegex(seat_model.SeatModelError, "gemini_native.*cannot speak"):
            seat_model.resolve_seat(self.loop, "reviewer", self.settings)

    def test_plain_http_provider_is_refused(self):
        write_profile(self.home, "adj", {"default": "d", "provider": "deepseek",
                                         "base_url": "http://api.deepseek.test/v1"},
                      {"DEEPSEEK_API_KEY": KEYS["adj"]})
        with self.assertRaisesRegex(seat_model.SeatModelError, "HTTPS"):
            seat_model.resolve_seat(self.loop, "adjudicator", self.settings)

    def test_missing_profile_holds_with_a_reason(self):
        loop = {**self.loop, "seats": {**self.loop["seats"], "fixer": {"profile": "ghost"}}}
        with self.assertRaisesRegex(seat_model.SeatModelError, "profile ghost does not exist"):
            seat_model.resolve_seat(loop, "fixer", self.settings)
        with self.assertRaisesRegex(seat_model.SeatModelError, "no Hermes profile"):
            seat_model.resolve_seat({**loop, "seats": {"fixer": {}}}, "fixer", self.settings)

    def test_hermes_unavailable_fails_closed(self):
        settings = {**self.settings, "source": str(self.root / "nowhere")}
        with self.assertRaisesRegex(seat_model.SeatModelError, "not importable"):
            seat_model.resolve_seat(self.loop, "reviewer", settings)


class Precedence(Base):
    def test_per_seat_override_beats_the_profile(self):
        settings = {**self.settings, "seats": {"fixer": {
            "model": "override-model", "upstream": "https://override.test/v1/chat/completions",
            "key_file": self.key_file("fixer.key", "OVERRIDE-KEY-4444")}}}
        fixer = seat_model.resolve_seat(self.loop, "fixer", settings)
        self.assertEqual((fixer.origin, fixer.model, fixer.key), ("override", "override-model",
                                                                  "OVERRIDE-KEY-4444"))
        self.assertEqual(seat_model.resolve_seat(self.loop, "reviewer", settings).origin, "profile")

    def test_legacy_trio_only_when_the_profile_cannot_resolve(self):
        settings = {**self.settings, "model": "legacy-model",
                    "upstream": "https://legacy.test/v1/chat/completions",
                    "key_file": self.key_file("legacy.key", "LEGACY-KEY-5555")}
        self.assertEqual(seat_model.resolve_seat(self.loop, "reviewer", settings).origin, "profile")
        loop = {**self.loop, "adjudicator": {"route": "breach", "profile": "default"}}
        adj = seat_model.resolve_seat(loop, "adjudicator", settings)
        self.assertEqual((adj.origin, adj.model), ("legacy", "legacy-model"))
        self.assertIn("bedrock", adj.warning)

    def test_runtime_file_shapes(self):
        base = {k: "/x" for k in seat_model.HOST_KEYS}
        seat_model.parse_runtime(base)
        seat_model.parse_runtime({**base, "model": "m", "upstream": "u", "key_file": "k"})
        seat_model.parse_runtime({**base, "seats": {"reviewer": {"model": "m", "upstream": "u",
                                                                 "key_file": "k"}}})
        for bad in ({**base, "model": "m"}, {k: "/x" for k in ("source", "venv", "runtime")},
                    {**base, "extra": 1}, {**base, "seats": {"observer": {}}},
                    {**base, "seats": {"fixer": {"model": "m"}}}, [], {**base, "source": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                seat_model.parse_runtime(bad)


class Worker(Base):
    """The production worker hands run_turn the row's own seat resolution, or holds the run."""

    def run_seat(self, seat: str, settings: dict | None = None, loop: dict | None = None):
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps(settings or self.settings))
        runtime.chmod(0o600)
        sup = Supervisor(self.root / "ledger.sqlite", production_config=runtime, hermes_home=self.home)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue(f"d-{seat}", "acme/widgets", 7, HEAD, seat)
        with sqlite3.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='launching', owner='w', generation='g' "
                        "WHERE delivery=?", (f"d-{seat}",))
            run_id = con.execute("SELECT id FROM runs WHERE delivery=?", (f"d-{seat}",)).fetchone()[0]
        seen = {}

        def run_turn(_loop, scope, **kw):
            seen.update(kw, role=scope.role)
            return 0
        pr = {"number": 7, "head": {"sha": HEAD, "ref": "fix-7"}}
        from review_loop import run_supervisor
        with mock.patch.object(config, "by_repo", return_value=loop or self.loop), \
             mock.patch.object(gh, "api", return_value=pr) as api, \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(gh, "review_state", return_value="CHANGES_REQUESTED"), \
             mock.patch("review_loop.gate.latest_effective_review_at_head", return_value={}), \
             mock.patch.object(run_supervisor, "adjudication_state",
                               return_value=("ok", {"reviews": [], "marker": {"rounds": 3}})), \
             mock.patch("review_loop.state.state_for") as state_for, \
             mock.patch.object(run_supervisor, "isolated_prompt", return_value="PROMPT"), \
             mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
             mock.patch.object(sup, "recover"):
            state_for.return_value.breach_start.return_value = {"ok": True}
            sup._run_production(run_id, "w")
            called_github = api.called
        with sqlite3.connect(sup.db) as con:
            row = con.execute("SELECT state, error FROM runs WHERE id=?", (run_id,)).fetchone()
        return seen, row, called_github

    def test_each_seat_turn_runs_with_its_own_model_and_key(self):
        expected = {"reviewer": ("vendor/rev-model", KEYS["rev"], "openrouter.test"),
                    "fixer": ("fix-model", KEYS["fix"], "acme.test"),
                    "adjudicator": ("deepseek-chat", KEYS["adj"], "api.deepseek.test")}
        for seat, (model, key, host) in expected.items():
            with self.subTest(seat=seat):
                seen, row, _ = self.run_seat(seat)
                self.assertEqual(row, ("succeeded", None))
                self.assertEqual((seen["role"], seen["model"], seen["key"]), (seat, model, key))
                self.assertIn(host, seen["upstream"])
                others = {k for s, (_, k, _) in expected.items() if s != seat}
                self.assertFalse(others & {seen["key"]})

    def test_unresolvable_seat_is_held_before_any_github_read(self):
        loop = {**self.loop, "adjudicator": {"route": "breach", "profile": "default"}}
        seen, row, called_github = self.run_seat("adjudicator", loop=loop)
        self.assertEqual(seen, {})
        self.assertFalse(called_github)
        self.assertEqual(row[0], "failed")
        self.assertRegex(row[1], r"^seat model unresolved: profile default \(bedrock\).*cloud signing")

    def test_legacy_seven_key_runtime_still_runs(self):
        settings = {**self.settings, "model": "legacy-model",
                    "upstream": "https://legacy.test/v1/chat/completions",
                    "key_file": self.key_file("legacy.key", "LEGACY-KEY-5555")}
        loop = {**self.loop, "seats": {**self.loop["seats"], "reviewer": {"profile": "ghost"}}}
        seen, row, _ = self.run_seat("reviewer", settings, loop)
        self.assertEqual(row, ("succeeded", None))
        self.assertEqual((seen["model"], seen["key"]), ("legacy-model", "LEGACY-KEY-5555"))


class DoctorAndModels(Base):
    def write_runtime(self, settings: dict) -> None:
        path = self.home / "review-loop-runtime.json"
        path.write_text(json.dumps(settings))
        path.chmod(0o600)

    def test_doctor_shows_each_seat_without_touching_credentials(self):
        self.write_runtime(self.settings)
        checks = {c.name: c for c in doctor.check_seat_models(self.loop)}
        self.assertEqual({n: c.status for n, c in checks.items()},
                         {"model:reviewer": doctor.VERIFIED, "model:fixer": doctor.VERIFIED,
                          "model:adjudicator": doctor.VERIFIED})
        self.assertIn("rev: openrouter / vendor/rev-model", checks["model:reviewer"].detail)
        self.assertIn("custom:acme / fix-model via acme.test", checks["model:fixer"].detail)
        for name in ("rev", "fix", "adj"):
            self.assertFalse((self.home / "profiles" / name / "resolved.marker").exists())
        text = json.dumps([vars(c) for c in checks.values()])
        for key in KEYS.values():
            self.assertNotIn(key, text)

    def test_doctor_fails_an_unresolvable_seat_and_warns_on_legacy(self):
        loop = {**self.loop, "adjudicator": {"route": "breach", "profile": "default"}}
        self.write_runtime(self.settings)
        checks = {c.name: c for c in doctor.check_seat_models(loop)}
        self.assertEqual(checks["model:adjudicator"].status, doctor.ABSENT)
        self.assertIn("will be held", checks["model:adjudicator"].detail)
        self.write_runtime({**self.settings, "model": "legacy-model",
                            "upstream": "https://legacy.test/v1/chat/completions",
                            "key_file": self.key_file("legacy.key", "LEGACY-KEY-5555")})
        checks = {c.name: c for c in doctor.check_seat_models(loop)}
        self.assertEqual(checks["model:adjudicator"].status, doctor.UNKNOWN)
        self.assertIn("LEGACY", checks["model:adjudicator"].detail)
        self.assertEqual(checks["runtime:legacy-model"].status, doctor.UNKNOWN)

    def models(self, **kw):
        self.write_runtime(self.settings)
        out = io.StringIO()
        args = argparse.Namespace(profile=kw.get("profile"), seat=kw.get("seat"), loop=None)
        with redirect_stdout(out), mock.patch.object(config, "all_loops", return_value=[self.loop]):
            rc = cli.cmd_models(args)
        return rc, out.getvalue()

    def test_models_lists_the_catalog_for_a_profile_or_seat(self):
        rc, text = self.models(profile="rev")
        self.assertEqual(rc, 0, text)
        self.assertIn("provider openrouter", text)
        self.assertIn("vendor/model-a", text)
        self.assertIn("not in this list", text)            # current model vendor/rev-model
        rc, text = self.models(seat="reviewer")
        self.assertEqual(rc, 0, text)
        self.assertIn("profile rev (seat reviewer)", text)
        self.assertNotIn(KEYS["rev"], text)

    def test_models_is_loud_about_unknown_providers_and_profiles(self):
        rc, text = self.models(profile="adj")
        self.assertEqual(rc, 1)
        self.assertIn("unknown to the Hermes model catalog", text)
        rc, text = self.models(profile="ghost")
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", text)


if __name__ == "__main__":
    unittest.main()
