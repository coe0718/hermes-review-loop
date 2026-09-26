"""OAuth/subscription seats: per-api_mode proxy contracts and host-side credential freshness.

Nothing here touches a real credential, profile, network or ~/.hermes: profiles live in a
throwaway HERMES_HOME, a fake ``hermes_cli``/``agent`` tree (the entry points the resolver imports
from the real Hermes source) resolves them — including a fake Codex-style ``auth.json`` with a
refresh token that rotates under a fake ``auth.lock`` — and every upstream is a local HTTP fake.
"""
import base64
import http.client
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from review_loop import broker_ipc, contained, doctor, inference_proxy, seat_model, trusted_turn  # noqa: E402
from review_loop.inference_proxy import (CONTRACTS, Credential, InferenceCapability,  # noqa: E402
                                         RefreshingCredential, StaticCredential, _UnixHTTP)
import test_seat_models as tsm  # noqa: E402

HEAD = "a" * 40
REFRESH_TOKEN = "RT-FAKE-REFRESH-TOKEN-never-leaves-the-host-0001"


def jwt(exp: float, account: str = "acct-fake-123") -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return ".".join([part({"alg": "none"}), part({"exp": int(exp), "https://api.openai.com/auth":
                                                  {"chatgpt_account_id": account}}), "sig"])


# -- a fake upstream ----------------------------------------------------------------------------

class Upstream:
    """Records every request; ``reply(handler, request)`` decides the answer."""

    def __init__(self, reply):
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                request = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                           "body": body}
                owner.requests.append(request)
                reply(self, request)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def send_json(handler, status=200, obj=None):
    data = json.dumps(obj if obj is not None else {"ok": True}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def post(cap, body, headers=None, path=None):
    conn = _UnixHTTP(str(cap.socket_path))
    try:
        conn.request("POST", path or cap.contract.local_path, body=json.dumps(body).encode(),
                     headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


class Tmp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.tmp = pathlib.Path(temp.name)
        self.n = 0

    def socket_dir(self):
        self.n += 1
        return self.tmp / f"cap{self.n}"


# -- 1. per-mode proxy contracts ----------------------------------------------------------------

class ModeContracts(Tmp):
    ATTACK = {"Authorization": "Bearer sandbox-attacker", "x-api-key": "sandbox-attacker",
              "ChatGPT-Account-ID": "attacker-acct", "anthropic-beta": "attacker-beta",
              "anthropic-version": "1999-01-01", "User-Agent": "sandbox-agent",
              "Cookie": "a=b", "session_id": "sess-1", "OpenAI-Organization": "org-x"}

    def test_codex_responses_contract(self):
        cred = Credential("CODEX-ACCESS-TOKEN", "bearer",
                          (("ChatGPT-Account-ID", "acct-host"), ("originator", "hermes-agent"),
                           ("User-Agent", "HermesAgent/test")))
        with Upstream(lambda h, r: send_json(h)) as up, \
                InferenceCapability(self.socket_dir(), up.url("/backend-api/codex/responses"),
                                    model="gpt-seat", quota=3, api_mode="codex_responses",
                                    credential=StaticCredential(cred)) as cap:
            self.assertEqual(cap.contract.local_path, "/v1/responses")
            self.assertEqual(post(cap, {"model": "x"}, path="/v1/chat/completions")[0], 400)
            self.assertEqual(post(cap, {"max_output_tokens": CONTRACTS["codex_responses"].cap + 1})[0], 400)
            self.assertEqual(post(cap, {"background": True})[0], 400)
            self.assertEqual(cap.used, 0)
            self.assertEqual(post(cap, {"model": "attacker", "input": []}, self.ATTACK)[0], 200)
            self.assertEqual(post(cap, {"max_output_tokens": 50})[0], 200)
        first, second = up.requests
        self.assertEqual(first["path"], "/backend-api/codex/responses")
        self.assertEqual(first["body"], {"model": "gpt-seat", "input": [],
                                         "max_output_tokens": CONTRACTS["codex_responses"].cap})
        self.assertEqual(second["body"]["max_output_tokens"], 50)
        h = first["headers"]
        self.assertEqual(h["authorization"], "Bearer CODEX-ACCESS-TOKEN")
        self.assertEqual((h["chatgpt-account-id"], h["originator"], h["user-agent"]),
                         ("acct-host", "hermes-agent", "HermesAgent/test"))
        self.assertEqual(h["session_id"], "sess-1")            # allowlisted, non-credential
        for dropped in ("x-api-key", "cookie", "anthropic-beta", "anthropic-version",
                        "openai-organization"):
            self.assertNotIn(dropped, h)

    def test_codex_backend_never_gets_an_output_cap_field(self):
        contract = CONTRACTS["codex_responses"]
        body = json.loads(inference_proxy.bounded_request(
            json.dumps({"model": "x", "max_output_tokens": 100, "temperature": 0.2,
                        "store": True, "stream": True}).encode(), "gpt-seat", contract, True))
        self.assertEqual(body, {"model": "gpt-seat", "store": False, "stream": True})
        with self.assertRaises(inference_proxy.ProxyError):   # still validated before dropping
            inference_proxy.bounded_request(json.dumps({"max_output_tokens": 10 ** 6}).encode(),
                                            "gpt-seat", contract, True)
        self.assertTrue(inference_proxy.is_codex_backend("https://chatgpt.com/backend-api/codex/responses"))
        self.assertFalse(inference_proxy.is_codex_backend("https://api.x.ai/v1/responses"))

    def test_anthropic_messages_contract_api_key_and_oauth(self):
        cap_limit = CONTRACTS["anthropic_messages"].cap
        for scheme, token in (("x-api-key", "sk-ant-api-HOST-KEY"), ("bearer", "sk-ant-oat-HOST-TOKEN")):
            with self.subTest(scheme=scheme):
                cred = Credential(token, scheme, (("anthropic-beta", "host-beta-1,oauth-2025-04-20"),
                                                  ("x-app", "cli")))
                with Upstream(lambda h, r: send_json(h)) as up, \
                        InferenceCapability(self.socket_dir(), up.url("/v1/messages"),
                                            model="claude-seat-4-5", quota=3,
                                            api_mode="anthropic_messages",
                                            credential=StaticCredential(cred)) as cap:
                    self.assertEqual(post(cap, {}, path="/v1/messages")[0], 400)
                    self.assertEqual(post(cap, {"model": "claude-opus", "max_tokens": 64000,
                                                "messages": []}, self.ATTACK)[0], 200)
                    self.assertEqual(post(cap, {"max_tokens": 64000, "thinking": {
                        "type": "enabled", "budget_tokens": 20000}})[0], 200)
                    self.assertEqual(post(cap, {"max_tokens": 0})[0], 400)
                (first, second) = up.requests
                self.assertEqual(first["path"], "/v1/messages")
                self.assertEqual(first["body"], {"model": "claude-seat-4-5", "max_tokens": cap_limit,
                                                 "messages": []})
                self.assertEqual(second["body"]["thinking"]["budget_tokens"], cap_limit - 1)
                h = first["headers"]
                self.assertEqual(h["anthropic-version"], "2023-06-01")
                self.assertEqual(h["anthropic-beta"], "host-beta-1,oauth-2025-04-20")
                self.assertEqual(h["x-app"], "cli")
                if scheme == "x-api-key":
                    self.assertEqual(h["x-api-key"], token)
                    self.assertNotIn("authorization", h)
                else:
                    self.assertEqual(h["authorization"], "Bearer " + token)
                    self.assertNotIn("x-api-key", h)
                for dropped in ("cookie", "chatgpt-account-id", "session_id"):
                    self.assertNotIn(dropped, h)
                self.assertNotEqual(h.get("user-agent"), "sandbox-agent")

    def test_event_streams_are_relayed_as_they_arrive(self):
        release = threading.Event()

        def reply(handler, _request):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(b'data: {"type": "response.created"}\n\n')
            handler.wfile.flush()
            release.wait(10)                   # the rest only after the client saw the first event
            handler.wfile.write(b'data: {"type": "response.completed"}\n\n')
            handler.wfile.flush()
            handler.close_connection = True
        with Upstream(reply) as up, \
                InferenceCapability(self.socket_dir(), up.url("/v1/responses"), "KEY",
                                    model="m", quota=1, api_mode="codex_responses") as cap:
            conn = _UnixHTTP(str(cap.socket_path))
            conn.request("POST", "/v1/responses", body=b'{"stream": true}')
            response = conn.getresponse()
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            first = response.fp.readline() + response.fp.readline()
            self.assertIn(b"response.created", first)
            self.assertFalse(release.is_set())
            release.set()
            rest = response.read()
            conn.close()
        self.assertIn(b"response.completed", rest)

    def test_unsupported_modes_are_refused_before_the_credential_is_read(self):
        class Tripwire:
            refreshable = True

            def current(self):
                raise AssertionError("credential read for an unsupported mode")
            refresh = close = current
        for mode in ("bedrock_converse", "codex_app_server", "gemini_native", "", None):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                InferenceCapability(self.socket_dir(), "https://h/v1/chat/completions",
                                    model="m", api_mode=mode, credential=Tripwire())
        with self.assertRaises(ValueError):          # each mode's upstream suffix is fixed
            InferenceCapability(self.socket_dir(), "https://h/v1/chat/completions", "K",
                                 model="m", api_mode="codex_responses")

    def test_host_headers_cannot_carry_credentials(self):
        for name in ("Authorization", "x-api-key", "Cookie", "Host", "bad header"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Credential("T", "bearer", ((name, "v"),))
        with self.assertRaises(ValueError):
            Credential("T", "basic")


# -- 2. host-side refresh -----------------------------------------------------------------------

class Refresh(Tmp):
    def upstream_accepting(self, good):
        def reply(handler, request):
            ok = request["headers"].get("authorization") == "Bearer " + good
            send_json(handler, 200 if ok else 401, {"ok": ok})
        return Upstream(reply)

    def test_401_refreshes_once_and_retries_once(self):
        calls = []

        def refresh(stale):
            calls.append(stale)
            return Credential("TOKEN-B")
        with self.upstream_accepting("TOKEN-B") as up, \
                InferenceCapability(self.socket_dir(), up.url("/v1/chat/completions"), model="m",
                                    quota=2, credential=RefreshingCredential(Credential("TOKEN-A"),
                                                                             refresh)) as cap:
            self.assertEqual(post(cap, {})[0], 200)
            self.assertEqual(post(cap, {})[0], 200)            # the new token is kept
        self.assertEqual(calls, ["TOKEN-A"])
        self.assertEqual([r["headers"]["authorization"] for r in up.requests],
                         ["Bearer TOKEN-A", "Bearer TOKEN-B", "Bearer TOKEN-B"])
        self.assertEqual(cap.used, 2)                            # a retry is not a second call

    def test_a_second_401_is_final_and_a_static_key_never_retries(self):
        calls = []
        with self.upstream_accepting("NOTHING") as up:
            refreshing = RefreshingCredential(Credential("TOKEN-A"),
                                              lambda stale: calls.append(stale) or Credential("TOKEN-B"))
            with InferenceCapability(self.socket_dir(), up.url("/v1/chat/completions"), model="m",
                                     quota=1, credential=refreshing) as cap:
                self.assertEqual(post(cap, {})[0], 401)
            with InferenceCapability(self.socket_dir(), up.url("/v1/chat/completions"), "KEY",
                                     model="m", quota=1) as cap:
                self.assertEqual(post(cap, {})[0], 401)
        self.assertEqual(calls, ["TOKEN-A"])
        self.assertEqual(len(up.requests), 3)                    # A, B (retry once), static KEY

    def test_near_expiry_refreshes_before_the_request(self):
        now = [1000.0]
        refreshing = RefreshingCredential(Credential("TOKEN-A", expires_at=1030.0),
                                          lambda stale: Credential("TOKEN-B", expires_at=5000.0),
                                          clock=lambda: now[0])
        with self.upstream_accepting("TOKEN-B") as up, \
                InferenceCapability(self.socket_dir(), up.url("/v1/chat/completions"), model="m",
                                    quota=1, credential=refreshing) as cap:
            self.assertEqual(post(cap, {})[0], 200)
        self.assertEqual([r["headers"]["authorization"] for r in up.requests], ["Bearer TOKEN-B"])
        self.assertEqual(refreshing.refreshes, 1)

    def test_a_failed_refresh_keeps_the_token_and_backs_off(self):
        now = [1000.0]
        attempts = []

        def refresh(stale):
            attempts.append(stale)
            raise RuntimeError("network down")
        refreshing = RefreshingCredential(Credential("TOKEN-A", expires_at=1030.0), refresh,
                                          clock=lambda: now[0])
        self.assertEqual(refreshing.current().token, "TOKEN-A")
        self.assertEqual(refreshing.current().token, "TOKEN-A")
        self.assertEqual(len(attempts), 1)                       # quiet for 30s
        now[0] += 31
        refreshing.current()
        self.assertEqual(len(attempts), 2)


# -- 3. seat_model against a fake Hermes (OAuth profiles) -----------------------------------------

OAUTH_HERMES = {
    "hermes_cli/auth.py": """
        class _P:
            def __init__(self, auth_type):
                self.auth_type = auth_type
        PROVIDER_REGISTRY = {"deepseek": _P("api_key"), "someoauth": _P("oauth_device_code"),
                             "openai-codex": _P("oauth_external"), "anthropic": _P("oauth_external")}
    """,
    "hermes_cli/runtime_provider.py": """
        import base64, fcntl, json, os, time
        from hermes_cli.config import load_config
        HOME = lambda: os.environ["HERMES_HOME"]

        def _get_model_config():
            return dict(load_config().get("model") or {})

        def resolve_requested_provider(requested=None):
            return str(_get_model_config().get("provider") or "auto").lower()

        def _log(line):
            with open(os.path.join(HOME(), "resolve.log"), "a") as handle:
                handle.write(line + "\\n")

        def _jwt(exp):
            part = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
            return ".".join([part({"alg": "none"}), part({"exp": int(exp),
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct-fake-123"}}), "sig"])

        def _exp(token):
            p = token.split(".")[1]
            return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))["exp"]

        def resolve_codex_runtime_credentials(force_refresh=False, **_):
            # Like Hermes: refresh under auth.lock, re-reading the store first; the refresh
            # token is spent and rotated, and never returned.
            path = os.path.join(HOME(), "auth.json")
            with open(os.path.join(HOME(), "auth.lock"), "a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                store = json.load(open(path))
                tokens = store["tokens"]
                if force_refresh or _exp(tokens["access_token"]) - time.time() < 120:
                    with open(os.path.join(HOME(), "refresh.count"), "a") as c:
                        c.write("x")
                    time.sleep(0.3)
                    n = store.get("n", 0) + 1
                    tokens = {"access_token": _jwt(time.time() + 3600) + str(n),
                              "refresh_token": "RT-rotated-%d" % n}
                    json.dump({"tokens": tokens, "n": n}, open(path, "w"))
            return {"api_key": tokens["access_token"], "base_url": "https://chatgpt.com/backend-api/codex",
                    "last_refresh": "2026-01-01T00:00:00Z"}

        def resolve_runtime_provider(**_):
            _log("start %f" % time.time())
            open(os.path.join(HOME(), "resolved.marker"), "w").close()
            try:
                time.sleep(0.1)
                cfg = _get_model_config()
                provider = resolve_requested_provider()
                if provider == "openai-codex":
                    creds = resolve_codex_runtime_credentials()
                    return {"provider": "openai-codex", "api_mode": "codex_responses",
                            "base_url": creds["base_url"], "api_key": creds["api_key"],
                            "last_refresh": creds["last_refresh"]}
                if provider == "anthropic":
                    token = os.environ.get("ANTHROPIC_TOKEN", "")
                    return {"provider": "anthropic", "api_mode": "anthropic_messages",
                            "base_url": "https://api.anthropic.com", "api_key": token}
                if provider == "nous":
                    return {"provider": "nous", "api_mode": "chat_completions",
                            "base_url": "https://inference.nous.test/v1", "api_key": "NOUS-JWT",
                            "expires_at": "2030-01-01T00:00:00+00:00"}
                raise RuntimeError("Unknown provider " + provider)
            finally:
                _log("end %f" % time.time())
    """,
    "agent/__init__.py": "",
    "agent/anthropic_credentials.py": """
        def anthropic_route_is_oauth(base_url, credential, provider=None):
            return "api.anthropic.com" in str(base_url) and str(credential).startswith("sk-ant-oat")
    """,
    "agent/anthropic_adapter.py": """
        _OAUTH_ONLY_BETAS = ["claude-code-20250219", "oauth-2025-04-20"]
        def _common_betas_for_base_url(base_url, drop_context_1m_beta=False):
            return ["interleaved-thinking-2025-05-14"]
        def _get_claude_code_version():
            return "9.9.9"
        def _auth_style(key, base_url, normalized):
            return "api_key"
        def _attribution_headers():
            return {}
        def _is_nous_portal_endpoint(base_url):
            return False
        def normalize_model_name(model, preserve_dots=False):
            return model.replace(".", "-")
    """,
    "agent/agent_init.py": """
        def _host_default_headers_factory(base_url):
            if "chatgpt.com" in base_url:
                return lambda key, base: {"ChatGPT-Account-ID": "acct-fake-123",
                                          "originator": "hermes-agent", "User-Agent": "HermesAgent/test"}
            return None
    """,
    "agent/model_metadata.py": """
        def strip_codex_context_variant_suffix(model):
            return model.removesuffix("-900k")
    """,
}


class OAuthBase(tsm.Base):
    def setUp(self):
        super().setUp()
        source = pathlib.Path(self.settings["source"])
        for name, body in OAUTH_HERMES.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(textwrap.dedent(body))
        self.codex = self.write_codex_profile("codex", time.time() + 3600)

    def write_codex_profile(self, name, exp, extra=None):
        path = tsm.write_profile(self.home, name, {"default": "gpt-5.3-codex-900k",
                                                   "provider": "openai-codex", **(extra or {})})
        (path / "auth.json").write_text(json.dumps({"tokens": {"access_token": jwt(exp),
                                                              "refresh_token": REFRESH_TOKEN}}))
        (path / "auth.json").chmod(0o600)
        return path

    def seat(self, profile, seat="reviewer"):
        return seat_model.resolve_seat({**self.loop, "seats": {**self.loop["seats"],
                                                               seat: {"profile": profile}}},
                                       seat, self.settings)


class OAuthResolution(OAuthBase):
    def test_codex_subscription_resolves_to_a_host_refreshed_responses_seat(self):
        exp = time.time() + 3600
        (self.codex / "auth.json").write_text(json.dumps(
            {"tokens": {"access_token": jwt(exp), "refresh_token": REFRESH_TOKEN}}))
        seat = self.seat("codex")
        self.assertEqual((seat.api_mode, seat.auth, seat.scheme, seat.provider),
                         ("codex_responses", "oauth", "bearer", "openai-codex"))
        self.assertEqual(seat.upstream, "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(seat.proxy_model, "gpt-5.3-codex")
        self.assertEqual(dict(seat.headers)["ChatGPT-Account-ID"], "acct-fake-123")
        self.assertAlmostEqual(seat.expires_at, int(exp), delta=1)
        self.assertTrue(seat.refreshable)
        self.assertIn("[codex_responses, OAuth (host-refreshed)]", seat.describe())
        everything = repr(seat) + seat.describe() + json.dumps(seat.headers) + seat.key
        self.assertNotIn(REFRESH_TOKEN, everything)
        self.assertNotIn(seat.key, repr(seat) + seat.describe())

    def test_claude_subscription_and_api_key(self):
        path = tsm.write_profile(self.home, "claude", {"default": "claude-sonnet-4.5",
                                                       "provider": "anthropic"},
                                 {"ANTHROPIC_TOKEN": "sk-ant-oat01-FAKE-SUBSCRIPTION"})
        seat = self.seat("claude")
        self.assertEqual((seat.api_mode, seat.auth, seat.scheme, seat.client_identity),
                         ("anthropic_messages", "oauth", "bearer", "claude_code"))
        self.assertEqual(seat.upstream, "https://api.anthropic.com/v1/messages")
        self.assertEqual(seat.proxy_model, "claude-sonnet-4-5")
        headers = dict(seat.headers)
        self.assertIn("oauth-2025-04-20", headers["anthropic-beta"])
        self.assertEqual((headers["user-agent"], headers["x-app"]), ("claude-code/9.9.9 (external, cli)", "cli"))
        (path / ".env").write_text("ANTHROPIC_TOKEN=sk-ant-api03-FAKE-CONSOLE-KEY\n")
        seat = self.seat("claude")
        self.assertEqual((seat.auth, seat.scheme, seat.client_identity), ("api_key", "x-api-key", ""))
        self.assertIn("[anthropic_messages, API key]", seat.describe())

    def test_the_expiry_hermes_states_is_the_refresh_schedule(self):
        from datetime import datetime, timezone
        tsm.write_profile(self.home, "nous", {"default": "hermes-4", "provider": "nous"})
        seat = self.seat("nous")
        self.assertEqual((seat.api_mode, seat.auth), ("chat_completions", "oauth"))
        self.assertEqual(seat.expires_at, datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertIsInstance(seat.credential_provider(), RefreshingCredential)
        api_key_seat = seat_model.SeatInference("fixer", "fix", "profile", "custom:acme", "m",
                                                "https://acme.test/v1/chat/completions", "K")
        self.assertIsInstance(api_key_seat.credential_provider(), StaticCredential)

    def test_unsupported_modes_are_refused_before_any_credential_read(self):
        cases = {"bedrock": {"provider": "bedrock"},
                 "copilot": {"provider": "copilot"},
                 "app-server": {"provider": "openai-codex", "openai_runtime": "codex_app_server"},
                 "converse": {"provider": "openai-codex", "api_mode": "bedrock_converse"}}
        for name, model in cases.items():
            with self.subTest(case=name):
                path = self.write_codex_profile(name, time.time() + 3600,
                                                {k: v for k, v in model.items() if k != "provider"})
                config = json.loads((path / "config.yaml").read_text())
                config["model"]["provider"] = model["provider"]
                (path / "config.yaml").write_text(json.dumps(config))
                before = (path / "auth.json").read_bytes()
                with self.assertRaises(seat_model.SeatModelError) as caught:
                    self.seat(name)
                self.assertFalse((path / "resolved.marker").exists())
                self.assertFalse((path / "auth.lock").exists())
                self.assertEqual((path / "auth.json").read_bytes(), before)
                self.assertNotIn(REFRESH_TOKEN, str(caught.exception))

    def test_doctor_reports_api_mode_and_auth_without_resolving(self):
        loop = {**self.loop, "seats": {**self.loop["seats"], "reviewer": {"profile": "codex"}}}
        runtime = self.home / "review-loop-runtime.json"
        runtime.write_text(json.dumps(self.settings))
        runtime.chmod(0o600)
        checks = {c.name: c for c in doctor.check_seat_models(loop)}
        self.assertEqual(checks["model:reviewer"].status, doctor.VERIFIED)
        self.assertIn("openai-codex / gpt-5.3-codex-900k [codex_responses, OAuth (host-refreshed)]",
                      checks["model:reviewer"].detail)
        self.assertIn("[chat_completions, API key]", checks["model:fixer"].detail)
        self.assertFalse((self.codex / "resolved.marker").exists())


class OAuthRefresh(OAuthBase):
    def events(self, profile):
        """The resolver's start/end marks in the order they happened."""
        lines = (self.home / "profiles" / profile / "resolve.log").read_text().splitlines()
        return [line.split()[0] for line in sorted(lines, key=lambda line: float(line.split()[1]))]

    def refresh_count(self, profile):
        path = self.home / "profiles" / profile / "refresh.count"
        return len(path.read_text()) if path.exists() else 0

    def test_near_expiry_refresh_rotates_through_hermes_and_keeps_the_refresh_token_home(self):
        seat = self.seat("codex")
        (self.codex / "auth.json").write_text(json.dumps(
            {"tokens": {"access_token": jwt(time.time() + 30), "refresh_token": REFRESH_TOKEN}}))
        provider = seat_model.SeatInference(**{**vars(seat), "expires_at": time.time() + 30,
                                               "key": seat.key}).credential_provider()
        fresh = provider.current()
        self.assertNotEqual(fresh.token, seat.key)
        self.assertEqual(self.refresh_count("codex"), 1)
        store = json.loads((self.codex / "auth.json").read_text())
        self.assertEqual(store["tokens"]["refresh_token"], "RT-rotated-1")   # rotated host-side
        self.assertNotIn("RT-rotated", repr(fresh) + fresh.token)

    def test_a_401_forces_one_hermes_refresh_of_that_token(self):
        seat = self.seat("codex")
        fresh = seat._refresh(seat.key)
        self.assertNotEqual(fresh.token, seat.key)
        self.assertEqual(self.refresh_count("codex"), 1)

    def test_two_seats_on_one_profile_never_refresh_in_parallel(self):
        reviewer, fixer = self.seat("codex", "reviewer"), self.seat("codex", "fixer")
        (self.codex / "resolve.log").unlink()
        (self.codex / "auth.json").write_text(json.dumps(
            {"tokens": {"access_token": jwt(time.time() + 30), "refresh_token": REFRESH_TOKEN}}))
        providers = [seat_model.SeatInference(**{**vars(s), "expires_at": time.time() + 30}
                                              ).credential_provider() for s in (reviewer, fixer)]
        results, start = [], threading.Barrier(2)

        def run(provider):
            start.wait()
            results.append(provider.current().token)
        threads = [threading.Thread(target=run, args=(p,)) for p in providers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1, "both seats use the one refreshed token")
        self.assertEqual(self.refresh_count("codex"), 1, "the second seat did not refresh again")
        self.assertEqual(self.events("codex"), ["start", "end", "start", "end"],
                         "the two resolutions overlapped")

    def test_the_profile_lock_also_serializes_across_processes(self):
        lockdir = self.home / "state" / "review-loop-seat-locks"
        with seat_model.profile_lock("codex"):
            (lock,) = lockdir.iterdir()
            probe = subprocess.run([sys.executable, "-c",
                                    "import fcntl,sys; f=open(sys.argv[1]);"
                                    "fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)", str(lock)],
                                   capture_output=True)
            self.assertNotEqual(probe.returncode, 0, "another process could take the lock")
        probe = subprocess.run([sys.executable, "-c",
                                "import fcntl,sys; f=open(sys.argv[1]);"
                                "fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)", str(lock)])
        self.assertEqual(probe.returncode, 0)


# -- 4. the sandbox side of an OAuth seat -----------------------------------------------------------

class SandboxConfig(unittest.TestCase):
    def test_each_mode_gets_a_matching_provider_and_only_a_dummy(self):
        chat = trusted_turn.sandbox_config("m", "chat_completions")
        self.assertEqual(chat[1:], ("", "custom"))
        self.assertIn("base_url: http://127.0.0.1:18761/v1", chat[0])
        codex = trusted_turn.sandbox_config("gpt-5.3-codex", "codex_responses")
        self.assertEqual(codex[2], "review-loop-seat")
        self.assertIn("api_mode: codex_responses", codex[0])
        self.assertIn("base_url: http://127.0.0.1:18761/v1", codex[0])
        anth = trusted_turn.sandbox_config("claude-x", "anthropic_messages")
        self.assertIn("api_mode: anthropic_messages", anth[0])
        self.assertIn("base_url: http://127.0.0.1:18761/anthropic", anth[0])
        sub = trusted_turn.sandbox_config("claude-x", "anthropic_messages", "claude_code")
        self.assertEqual(sub[2], "anthropic")
        self.assertEqual(sub[1], "ANTHROPIC_TOKEN=sk-ant-oat01-sandbox-dummy-not-a-credential\n")
        for text in (chat[0], codex[0], anth[0], sub[0]):
            self.assertIn("sandbox-dummy-not-a-credential", text) if "api_key" in text else None
        with self.assertRaises(ValueError):
            trusted_turn.sandbox_config("m", "bedrock_converse")


class OAuthTurnStaging(OAuthBase):
    def test_an_oauth_turn_stages_no_token_and_proxies_the_refreshing_provider(self):
        seat = self.seat("codex")
        root = self.root / "turn"
        for name in ("venv", "runtime", "rust", "src"):
            (root / name).mkdir(parents=True)
        seen, staged = {}, {}

        class Inference:
            def __init__(self, directory, upstream, key, **kw):
                seen.update(upstream=upstream, key=key, **kw)
                self.directory = directory

            def __enter__(self):
                self.directory.mkdir()
                (self.directory / "model.sock").touch()
                return self

            def __exit__(self, *a):
                return False

        def stage(_loop, **kw):
            kw["sandbox_root"].mkdir()
            return kw["sandbox_root"]

        def run(**kw):
            seen["argv"] = contained.command(**{k: v for k, v in kw.items() if k != "timeout"})
            for base in (kw["home"], kw["code"], kw["client_code"], kw["checkout"], kw["query"].parent):
                for path in pathlib.Path(base).rglob("*"):
                    if path.is_file():
                        staged[str(path)] = path.read_bytes()
            return subprocess.CompletedProcess([], 0, "", "")

        scope = broker_ipc.RunScope("acme/widgets", 7, HEAD, "reviewer", "fix-7", "rid", str(root / "db"))
        loop = {"repo": "acme/widgets", "state_dir": str(root)}
        with mock.patch.object(trusted_turn, "_safe_code_snapshot", side_effect=lambda s, d: d.mkdir()), \
             mock.patch.object(trusted_turn.trusted_fetch, "stage", side_effect=stage), \
             mock.patch.object(trusted_turn.inference_proxy, "InferenceCapability", Inference), \
             mock.patch.object(contained.Path, "is_socket", return_value=True), \
             mock.patch.object(contained, "run", side_effect=run), \
             self.assertRaises(trusted_turn.TurnDenied):
            trusted_turn.run_turn(loop, scope, source=root / "src", venv=root / "venv",
                                  runtime=root / "runtime", rust=root / "rust",
                                  upstream=seat.upstream, key=seat.key, model=seat.model,
                                  api_mode=seat.api_mode, credential=seat.credential_provider(),
                                  proxy_model=seat.proxy_model, prompt="REVIEW", timeout=5,
                                  work_root=root / "work")
        self.assertIsNone(seen["key"])
        self.assertIsInstance(seen["credential"], RefreshingCredential)
        self.assertEqual((seen["api_mode"], seen["model"]), ("codex_responses", "gpt-5.3-codex"))
        home = next(v for k, v in staged.items() if k.endswith("/home/config.yaml")).decode()
        self.assertIn("api_mode: codex_responses", home)
        for path, data in staged.items():
            for secret in (seat.key, REFRESH_TOKEN, "acct-fake-123"):
                self.assertNotIn(secret.encode(), data, path)
        argv = json.dumps(seen["argv"])
        self.assertNotIn(seat.key, argv)
        self.assertIn('"--provider", "review-loop-seat"', argv)


def _bwrap_works() -> bool:
    if not shutil.which("bwrap") or not os.path.exists("/usr/bin/python3"):
        return False
    try:
        return subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr",
                               "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
                               "--ro-bind-try", "/lib64", "/lib64", "--", "/usr/bin/true"],
                              capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


@unittest.skipUnless(_bwrap_works(), "unprivileged bubblewrap unavailable")
class OAuthSandboxProbe(OAuthBase):
    def test_the_profile_auth_store_and_its_tokens_are_invisible_in_the_sandbox(self):
        seat = self.seat("codex")
        loop = {**self.loop, "seats": {**self.loop["seats"], "reviewer": {"profile": "codex"}}}
        paths = seat_model.secret_paths(loop, self.settings)
        self.assertIn(str(self.codex / "auth.json"), paths)
        self.assertIn(str(self.codex / "auth.lock"), paths)
        root = self.root / "box"
        venv, code, home, work, rust = (root / n for n in ("venv", "code", "home", "work", "rust"))
        for d in (code, home, work, rust, venv / "bin"):
            d.mkdir(parents=True)
        (venv / "bin" / "python").symlink_to("/usr/bin/python3")
        text, env_text, _ = trusted_turn.sandbox_config(seat.model, seat.api_mode)
        (home / "config.yaml").write_text(text)
        query = root / "query.txt"
        query.write_text("probe\n")
        probe = ("import json, os, subprocess, sys\n"
                 "readable = []\n"
                 "for p in json.loads(sys.argv[1]):\n"
                 "    try:\n"
                 "        open(p, 'rb').read(1); readable.append(p)\n"
                 "    except OSError: pass\n"
                 "found = subprocess.run(['grep', '-rl', '-e', sys.argv[2], '-e', sys.argv[3],\n"
                 "                        '/home/agent', '/opt', '/work', '/tmp'], capture_output=True, text=True).stdout\n"
                 "print(json.dumps({'readable': readable, 'env': [k for k, v in os.environ.items()\n"
                 "                  if sys.argv[2] in v or sys.argv[3] in v], 'found': found}))\n")
        result = contained.run(code=code, venv=venv, runtime=pathlib.Path("/usr"), home=home,
                               checkout=work, rust=rust, query=query, timeout=60,
                               entry=["/opt/venv/bin/python", "-c", probe, json.dumps(paths),
                                      REFRESH_TOKEN, seat.key])
        self.assertEqual(result.returncode, 0, result.stderr)
        facts = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(facts, {"readable": [], "env": [], "found": ""})


# -- 5. the real sandboxed Hermes speaking each wire format through the real proxy ----------------

SOURCE = pathlib.Path(os.environ.get("HERMES_AGENT_SOURCE") or pathlib.Path.home() / ".hermes/hermes-agent")


def _sse(events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def responses_reply(request):
    """Tool call first, then a final message once the tool's output comes back."""
    body = request["body"]
    done = any(isinstance(item, dict) and item.get("type") == "function_call_output"
               for item in body.get("input", []))
    if done:
        item = {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "FIXTURE_DONE", "annotations": []}]}
    else:
        item = {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "terminal",
                "arguments": json.dumps({"command": "echo TOOL-RAN"}), "status": "completed"}
    base = {"id": "resp_1", "object": "response", "model": body["model"]}
    return _sse([("response.created", {"type": "response.created", "response": {**base, "status": "in_progress", "output": []}}),
                 ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
                 ("response.completed", {"type": "response.completed", "response": {
                     **base, "status": "completed", "output": [item],
                     "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}})])


def messages_reply(request):
    body = request["body"]
    done = any(isinstance(block, dict) and block.get("type") == "tool_result"
               for message in body.get("messages", []) if isinstance(message.get("content"), list)
               for block in message["content"])
    start = {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
             "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": {"input_tokens": 1, "output_tokens": 1}}}
    if done:
        blocks = [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                  ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "FIXTURE_DONE"}})]
        stop = "end_turn"
    else:
        blocks = [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                      "type": "tool_use", "id": "toolu_1", "name": "mcp__terminal", "input": {}}}),
                  ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                      "type": "input_json_delta", "partial_json": json.dumps({"command": "echo TOOL-RAN"})}})]
        stop = "tool_use"
    return _sse([("message_start", start), *blocks,
                 ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                 ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                    "usage": {"output_tokens": 1}}),
                 ("message_stop", {"type": "message_stop"})])


@unittest.skipUnless(_bwrap_works() and (SOURCE / "venv/bin/hermes").exists(),
                     "bubblewrap or Hermes checkout unavailable")
class RealHermesWireFormats(unittest.TestCase):
    """The sandboxed Hermes, configured by ``sandbox_config``, completes a tool turn in each mode."""

    def turn(self, api_mode, identity, model, proxy_model, upstream_path, reply, credential):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            code = root / "code"
            for raw in subprocess.check_output(["git", "-C", str(SOURCE), "ls-files", "-z"]).split(b"\0"):
                name = raw.decode()
                if (not name or name.startswith((".", "tests/", "docs/", "website/", "scripts/")) or
                        pathlib.Path(name).suffix not in (".py", ".yaml", ".json", ".txt", ".md", ".toml")):
                    continue
                (code / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / name, code / name, follow_symlinks=False)
            (code / "review_loop").mkdir(exist_ok=True)
            shutil.copyfile(HERE.parent / "review_loop/inference_proxy.py", code / "review_loop/inference_proxy.py")
            home, work, rust = root / "home", root / "work", root / "rust"
            for d in (home, work, rust):
                d.mkdir()
            text, env_text, provider = trusted_turn.sandbox_config(model, api_mode, identity)
            (home / "config.yaml").write_text(text)
            if env_text:
                (home / ".env").write_text(env_text)
            (home / "query.txt").write_text("Run `echo TOOL-RAN` with the terminal tool, then say FIXTURE_DONE.")

            def answer(handler, request):
                if request["headers"].get("authorization", "").endswith("TOKEN-A"):
                    send_json(handler, 401, {"error": "expired"})
                    return
                data = reply(request)
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
            venv = SOURCE / "venv"
            runtime = pathlib.Path(os.readlink(venv / "bin/python")).parents[2]
            with Upstream(answer) as up, \
                    InferenceCapability(root / "cap", up.url(upstream_path), model=proxy_model, quota=6,
                                        api_mode=api_mode, credential=credential) as cap:
                result = contained.run(
                    code=code, venv=venv, runtime=runtime, home=home, checkout=work, rust=rust,
                    query=home / "query.txt", inference_socket_dir=cap.directory, timeout=240,
                    entry=["/opt/venv/bin/python", "-m", "review_loop.inference_proxy", "bridge", "--",
                           "/opt/venv/bin/python", "/opt/venv/bin/hermes", "chat", "--query-file",
                           "/home/agent/query.txt", "--oneshot", "-Q", "--provider", provider, "-m", model,
                           "-t", "terminal", "--ignore-rules", "--max-turns", "4", "--run-budget", "150"])
            self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-2000:])
            self.assertIn("FIXTURE_DONE", result.stdout)
            return up.requests

    def refreshing(self, extra=()):
        return RefreshingCredential(Credential("TOKEN-A", "bearer", extra),
                                    lambda stale: Credential("TOKEN-B", "bearer", extra))

    def test_codex_responses_seat(self):
        requests = self.turn("codex_responses", "", "gpt-5.3-codex", "gpt-5.3-codex",
                             "/backend-api/codex/responses", responses_reply,
                             self.refreshing((("ChatGPT-Account-ID", "acct-host"),)))
        self.assertGreaterEqual(len(requests), 3)            # 401 → refreshed retry, tool round trip
        self.assertEqual(requests[0]["headers"]["authorization"], "Bearer TOKEN-A")
        for request in requests[1:]:
            h = request["headers"]
            self.assertEqual((request["path"], h["authorization"], h["chatgpt-account-id"]),
                             ("/backend-api/codex/responses", "Bearer TOKEN-B", "acct-host"))
            self.assertNotIn("sandbox-dummy", json.dumps(h))
            self.assertEqual(request["body"]["model"], "gpt-5.3-codex")
            self.assertLessEqual(request["body"]["max_output_tokens"], CONTRACTS["codex_responses"].cap)
        outputs = [item for item in requests[-1]["body"]["input"]
                   if isinstance(item, dict) and item.get("type") == "function_call_output"]
        self.assertIn("TOOL-RAN", json.dumps(outputs))

    def test_claude_subscription_seat(self):
        requests = self.turn("anthropic_messages", "claude_code", "claude-sonnet-4.5",
                             "claude-sonnet-4-5", "/v1/messages", messages_reply,
                             self.refreshing((("anthropic-beta", "host-beta,oauth-2025-04-20"),
                                              ("x-app", "cli"))))
        for request in requests[1:]:
            h, body = request["headers"], request["body"]
            self.assertEqual((request["path"], h["authorization"], h["anthropic-beta"]),
                             ("/v1/messages", "Bearer TOKEN-B", "host-beta,oauth-2025-04-20"))
            self.assertNotIn("x-api-key", h)
            self.assertEqual(body["model"], "claude-sonnet-4-5")
            self.assertLessEqual(body["max_tokens"], CONTRACTS["anthropic_messages"].cap)
            self.assertTrue(body["system"][0]["text"].startswith("You are Claude Code"))
            self.assertTrue(all(tool["name"].startswith("mcp__") for tool in body.get("tools", [])))
        results = [block for message in requests[-1]["body"]["messages"]
                   if isinstance(message.get("content"), list) for block in message["content"]
                   if block.get("type") == "tool_result"]
        self.assertIn("TOOL-RAN", json.dumps(results))


# -- 6. selftest names the wire format and the auth kind ------------------------------------------

from test_selftest import SelftestBase  # noqa: E402


class SelftestOAuth(SelftestBase):
    def setUp(self):
        super().setUp()
        self.fx.loop["seats"]["reviewer"]["profile"] = "codex"
        self.fx.loop["seats"]["fixer"]["profile"] = "claude"
        settings = {k: v for k, v in self.fx.settings.items() if k in seat_model.HOST_KEYS}
        self.fx.runtime_file.write_text(json.dumps(settings))
        self.bodies = []

    def model_post(self, endpoint, body, headers):
        self.bodies.append((endpoint.url.path, json.loads(body), dict(headers)))
        if endpoint.url.path.endswith("/responses"):
            return (200, "text/event-stream", iter([
                b'data: {"type": "response.output_text.delta", "delta": "OK"}\n\n',
                b'data: {"type": "response.completed"}\n\n']))
        return (200, "application/json", json.dumps(
            {"content": [{"type": "text", "text": "OK"}]}).encode())

    @staticmethod
    def resolver(profile, seat, settings):
        if profile == "codex":
            return seat_model.SeatInference(
                seat, profile, "profile", "openai-codex", "gpt-5.3-codex",
                "https://chatgpt.com/backend-api/codex/responses", "CODEX-ACCESS-TOKEN-77",
                api_mode="codex_responses", auth="oauth", refreshable=True,
                headers=(("ChatGPT-Account-ID", "acct-9"),))
        return seat_model.SeatInference(
            seat, profile, "profile", "anthropic", "claude-sonnet-4.5",
            "https://api.anthropic.com/v1/messages", "CLAUDE-SUB-TOKEN-88",
            api_mode="anthropic_messages", auth="oauth", client_identity="claude_code",
            wire_model="claude-sonnet-4-5", refreshable=True)

    def test_selftest_probes_each_wire_format_and_says_oauth(self):
        rc, text = self.run_selftest(resolver=self.resolver)
        self.assertRegex(text, r"seat:reviewer\s+profile codex: openai-codex / gpt-5.3-codex via "
                               r"chatgpt.com \[codex_responses, OAuth \(host-refreshed\)\]")
        self.assertRegex(text, r"seat:fixer\s+profile claude: anthropic / claude-sonnet-4.5 via "
                               r"api.anthropic.com \[anthropic_messages, OAuth \(host-refreshed\)\]")
        self.assertIn("reply 'OK'", text)
        paths = {path: (body, headers) for path, body, headers in self.bodies}
        codex_body, codex_headers = paths["/backend-api/codex/responses"]
        self.assertEqual(codex_body["model"], "gpt-5.3-codex")
        self.assertNotIn("max_output_tokens", codex_body)          # the Codex backend rejects it
        self.assertEqual(codex_headers["Authorization"], "Bearer CODEX-ACCESS-TOKEN-77")
        claude_body, claude_headers = paths["/v1/messages"]
        self.assertEqual(claude_body["model"], "claude-sonnet-4-5")
        self.assertTrue(claude_body["system"][0]["text"].startswith("You are Claude Code"))
        self.assertEqual(claude_headers["Authorization"], "Bearer CLAUDE-SUB-TOKEN-88")
        for secret in ("CODEX-ACCESS-TOKEN-77", "CLAUDE-SUB-TOKEN-88"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
