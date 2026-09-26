"""Which model, provider and credential a seat's isolated turn uses (issue #32).

Each seat — reviewer, fixer, adjudicator — runs as its own Hermes profile, and the profile decides
the model, the provider and the account. The isolated turn cannot read the profile itself (its
sandbox has no host home and no credentials), so the **host** resolves it, right before the turn:

* the seat's profile home (``$HERMES_HOME`` for ``default``, else ``profiles/<name>``) is handed to
  Hermes's own resolution — ``hermes_cli.env_loader.load_hermes_dotenv`` for the profile's
  ``.env`` and secret sources, then ``hermes_cli.runtime_provider.resolve_runtime_provider`` — the
  same path ``hermes -p <name> chat`` takes, including ``auth.json`` credential pools and named
  ``custom_providers``. It runs in a **separate process per seat**, with an environment built from
  scratch (no inherited provider variables), so one seat's credential can never satisfy another
  seat's resolution. The key comes back over a pipe and lives only in the host inference proxy.
* the result must be one of the inference proxy's wire contracts — ``chat_completions``,
  ``codex_responses`` or ``anthropic_messages`` (see ``inference_proxy.CONTRACTS``) — over HTTPS
  with a non-empty credential. API-key providers and the OAuth/subscription providers Hermes
  resolves to one of those (``openai-codex`` and ``xai-oauth`` → Responses, ``qwen-oauth`` and
  ``nous`` → chat-completions, ``minimax-oauth`` → Messages with a bearer token, ``anthropic``
  with a Claude subscription token → Messages with the Claude Code identity) are accepted.
  Copilot, Bedrock, Vertex, Azure Foundry, MoA, the ``codex_app_server`` runtime and any other
  ``api_mode`` are refused *before* a credential is looked up.
* an OAuth credential is only its short-lived **access token**: Hermes keeps the refresh token
  in the profile's ``auth.json`` (under its own ``auth.lock``). The host re-runs the same
  isolated resolution when the token nears the expiry Hermes (or the token itself) states, or
  once after an upstream 401; every resolution of one profile is serialized (a per-profile
  thread lock plus a ``flock``), so two seats sharing a profile never refresh in parallel, and
  the second one simply reads the token the first refreshed. The sandbox only ever sees a dummy.

``review-loop-runtime.json`` names host paths (``source``, ``venv``, ``runtime``, ``rust``). The
model may additionally be overridden there. Precedence, per seat:

1. ``seats.<seat>`` in the runtime file — ``{"model", "upstream", "key_file"}`` — an explicit,
   per-seat override (testing, or a profile whose provider the proxy cannot speak);
2. the seat's Hermes profile — the default, and the point of the design;
3. the legacy top-level ``model``/``upstream``/``key_file`` — used **only** when the profile cannot
   be resolved, so pre-#32 runtime files keep working; ``doctor`` and ``selftest`` warn whenever
   it is in effect, because it gives every such seat the same model.

Anything else fails closed: the turn is not launched and the ledger records the reason.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from . import config

SEATS = ("reviewer", "fixer", "adjudicator")
HOST_KEYS = ("source", "venv", "runtime", "rust")
OVERRIDE_KEYS = ("model", "upstream", "key_file")
RESOLVE_TIMEOUT = 90

# Providers refused by name before Hermes resolves (and possibly refreshes) anything: a token
# exchange with provider-specific client headers (Copilot), cloud IAM signing (Bedrock, Vertex,
# Azure Foundry), or a fan-out of several models (MoA).
UNSUPPORTED_PROVIDERS = frozenset({
    "copilot", "copilot-acp", "github-copilot", "bedrock", "aws-bedrock", "vertex",
    "google-vertex", "vertex-ai", "gcp-vertex", "vertexai", "azure-foundry", "moa",
})
# OAuth/subscription providers whose Hermes runtime speaks a proxied wire format, and that format.
# ``anthropic`` is not listed: it may hold an API key or a Claude subscription token, and Hermes's
# resolution decides (``client_identity == "claude_code"`` when it is the subscription).
OAUTH_PROVIDERS = {"openai-codex": "codex_responses", "xai-oauth": "codex_responses",
                   "qwen-oauth": "chat_completions", "nous": "chat_completions",
                   "minimax-oauth": "anthropic_messages"}
ANTHROPIC_ALIASES = frozenset({"anthropic", "claude", "claude-code"})
REFUSED_API_MODES = frozenset({"bedrock_converse", "codex_app_server"})

_SECRETISH = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
                        r"|sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|[A-Za-z0-9_-]{40,})")


class SeatModelError(Exception):
    """The seat's model cannot be resolved; the message says why and what to change."""


@dataclass(frozen=True)
class SeatInference:
    """Everything the host needs to start one seat's inference proxy. ``key`` never prints."""
    seat: str
    profile: str
    origin: str          # "override" | "profile" | "legacy"
    provider: str
    model: str
    upstream: str
    key: str = field(repr=False, compare=False)
    warning: str = ""
    api_mode: str = "chat_completions"
    auth: str = "api_key"                 # "api_key" | "oauth"
    scheme: str = "bearer"                # how the upstream takes it: "bearer" | "x-api-key"
    headers: tuple = field(default=(), repr=False)   # host-chosen, non-credential
    expires_at: float | None = field(default=None, repr=False, compare=False)
    wire_model: str = ""                  # the model id on the wire, when Hermes normalizes it
    client_identity: str = ""             # "claude_code" for a Claude subscription token
    refreshable: bool = False
    settings: dict | None = field(default=None, repr=False, compare=False)

    @property
    def host(self) -> str:
        return urlsplit(self.upstream).hostname or ""

    @property
    def proxy_model(self) -> str:
        """The model the proxy forces into every body (Hermes's wire spelling of it)."""
        return self.wire_model or self.model

    @property
    def auth_label(self) -> str:
        return "OAuth (host-refreshed)" if self.auth == "oauth" else "API key"

    def identity(self) -> tuple:
        """Distinct (provider, model, endpoint, credential) — for once-per-resolution checks."""
        return (self.provider, self.model, self.upstream,
                hashlib.sha256(self.key.encode()).hexdigest())

    def describe(self) -> str:
        where = {"override": "runtime override seats." + self.seat,
                 "legacy": "LEGACY runtime model",
                 "profile": "profile " + self.profile}[self.origin]
        what = self.model if self.origin != "profile" else f"{self.provider} / {self.model}"
        return f"{where}: {what} via {self.host} [{self.api_mode}, {self.auth_label}]"

    def credential(self):
        """The first ``inference_proxy.Credential`` (host-side only)."""
        from .inference_proxy import Credential
        return Credential(self.key, self.scheme, self.headers, self.expires_at)

    def credential_provider(self):
        """What the turn's proxy authenticates with: a static key, or a host-refreshed token."""
        from .inference_proxy import RefreshingCredential, StaticCredential
        if not self.refreshable or self.origin != "profile":
            return StaticCredential(self.credential())
        return RefreshingCredential(self.credential(), self._refresh)

    def _refresh(self, stale: str):
        """Re-resolve this seat's profile through Hermes; a new credential or an exception."""
        fresh = resolve_profile(self.profile, self.seat, self.settings,
                                stale=hashlib.sha256(stale.encode()).hexdigest())
        if (fresh.api_mode, fresh.upstream, fresh.wire_model or fresh.model) != \
                (self.api_mode, self.upstream, self.wire_model or self.model):
            raise SeatModelError(f"profile {self.profile} changed provider or model mid-turn")
        return fresh.credential()


# -- the runtime file --------------------------------------------------------------------------

def parse_runtime(settings: object) -> dict:
    """Validate the runtime file's shape; raise ``ValueError`` naming what is wrong.

    Required: the four host paths. Optional: the legacy global trio (all three or none) and a
    ``seats`` object of per-seat override trios.
    """
    if not isinstance(settings, dict):
        raise ValueError("runtime file must be a JSON object")
    allowed = set(HOST_KEYS) | set(OVERRIDE_KEYS) | {"seats"}
    extra = sorted(set(settings) - allowed)
    missing = [key for key in HOST_KEYS if key not in settings]
    if extra or missing:
        raise ValueError("runtime keys: " + "; ".join(
            ([f"missing {', '.join(missing)}"] if missing else []) +
            ([f"unexpected {', '.join(extra)}"] if extra else [])))
    bad = [key for key in HOST_KEYS if not isinstance(settings[key], str) or not settings[key]]
    if bad:
        raise ValueError(f"empty or non-string: {', '.join(bad)}")
    present = [key for key in OVERRIDE_KEYS if key in settings]
    if present and len(present) != len(OVERRIDE_KEYS):
        raise ValueError("the legacy model override needs all of model, upstream, key_file "
                         f"(found only {', '.join(present)})")
    if present:
        _override_shape(settings, "runtime")
    seats = settings.get("seats", {})
    if not isinstance(seats, dict):
        raise ValueError("seats must be an object of per-seat overrides")
    for seat, block in seats.items():
        if seat not in SEATS:
            raise ValueError(f"seats.{seat}: unknown seat (use {', '.join(SEATS)})")
        if not isinstance(block, dict) or set(block) != set(OVERRIDE_KEYS):
            raise ValueError(f"seats.{seat} must have exactly model, upstream, key_file")
        _override_shape(block, f"seats.{seat}")
    return settings


def _override_shape(block: dict, where: str) -> None:
    bad = [key for key in OVERRIDE_KEYS if not isinstance(block.get(key), str) or not block[key]]
    if bad:
        raise ValueError(f"{where}: empty or non-string {', '.join(bad)}")


def load_runtime(path: Path) -> dict:
    return parse_runtime(json.loads(Path(path).read_text()))


def legacy_override(settings: dict) -> dict | None:
    return ({key: settings[key] for key in OVERRIDE_KEYS}
            if all(key in settings for key in OVERRIDE_KEYS) else None)


def seat_override(settings: dict, seat: str) -> dict | None:
    return (settings.get("seats") or {}).get(seat)


def check_upstream(upstream: str, where: str, api_mode: str = "chat_completions") -> str:
    from .inference_proxy import _NoRedirectConnection, contract_for
    suffix = contract_for(api_mode).upstream_suffix
    if urlsplit(upstream).scheme != "https":
        raise SeatModelError(f"{where}: inference must use HTTPS, got {upstream.split(':', 1)[0]!r}")
    try:
        _NoRedirectConnection(upstream, suffix)
    except ValueError:
        raise SeatModelError(f"{where}: upstream must be https://host[:port]/…{suffix} "
                             "with no credentials, query or fragment") from None
    return upstream


def upstream_for(base_url: str, where: str, api_mode: str = "chat_completions") -> str:
    """A provider base URL → the proxy's fixed upstream URL for ``api_mode``.

    ``…/v1`` + ``/chat/completions``; a Responses base (``…/backend-api/codex``, ``…/v1``) +
    ``/responses``; an Anthropic base with any trailing ``/v1`` removed + ``/v1/messages`` (the
    Anthropic SDK's own rule).
    """
    from .inference_proxy import contract_for
    suffix = contract_for(api_mode).upstream_suffix
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise SeatModelError(f"{where}: the provider resolved without a base URL")
    if api_mode == "anthropic_messages":
        base = re.sub(r"/v1$", "", base)
    return check_upstream(base if base.endswith(suffix) else base + suffix, where, api_mode)


def _read_key_file(raw: str, where: str) -> str:
    path = Path(raw).expanduser()
    try:
        info = path.lstat()
    except OSError:
        raise SeatModelError(f"{where}: key file {path} does not exist") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SeatModelError(f"{where}: key file {path} must be a regular 0600 file you own "
                             f"(`chmod 600 {path}`)")
    key = path.read_text().strip()
    if not key or "\n" in key or "\r" in key:
        raise SeatModelError(f"{where}: key file {path} is empty or has more than one line")
    return key


def _from_override(block: dict, seat: str, profile: str, origin: str, where: str,
                   warning: str = "") -> SeatInference:
    upstream = check_upstream(block["upstream"], where)
    return SeatInference(seat, profile, origin, "runtime", block["model"], upstream,
                         _read_key_file(block["key_file"], where), warning)


# -- Hermes, run as the seat's profile ------------------------------------------------------------

_RESOLVER = r'''
import base64, hashlib, json, os, sys, time
out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)                      # anything Hermes prints goes to stderr, never into our answer
sys.stdout = sys.stderr
source, mode, policy = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
unsupported, oauth_providers = set(policy["unsupported"]), dict(policy["oauth"])
modes, refused_modes = set(policy["modes"]), set(policy["refused_modes"])
anthropic_aliases, stale = set(policy["anthropic"]), policy.get("stale") or ""
sys.path.insert(0, source)

def done(**answer):
    out.write(json.dumps(answer))
    out.flush()
    os._exit(0)

def text(exc):
    return (type(exc).__name__ + ": " + str(exc))[:400]

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else ""

def jwt_exp(token):
    """A JWT access token's own ``exp`` — only a refresh schedule hint, never trusted for auth."""
    try:
        part = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
        return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
    except Exception:
        return None

def expiry(runtime, key):
    for name, scale in (("expires_at", 1.0), ("agent_key_expires_at", 1.0), ("expires_at_ms", 0.001)):
        value = runtime.get(name)
        if isinstance(value, bool) or value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            return float(value) * scale
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
    return jwt_exp(key)


class NoYamlReader(Exception):
    """No YAML reader in this interpreter: the config cannot be read at all, and calling it
    "unreadable" would blame a file that is perfectly fine."""


def read_config(raw):
    """Parse a config.yaml with whichever reader this interpreter has.

    Hermes reads its configs with ruamel.yaml, and the interpreter running this check was chosen
    by the host — on a packaged install that is Hermes's own bundled python, which ships neither
    PyYAML nor ruamel. Try both readers, then JSON (a JSON config is valid YAML), and when there
    is no reader at all report *that*: the profile's config being readable is exactly what made
    this failure look like a corrupt file.
    """
    for name in ("yaml", "ruamel.yaml"):
        try:
            if name == "yaml":
                import yaml as module
                loaded = module.safe_load(raw)
            else:
                from ruamel.yaml import YAML
                loaded = YAML(typ="safe").load(raw)
        except ImportError:
            continue
        return loaded or {}
    try:
        return json.loads(raw)
    except ValueError:
        raise NoYamlReader("this interpreter (" + sys.executable + ") has no YAML library "
                           "(looked for yaml and ruamel.yaml), so it cannot read a profile's "
                           "config at all; name a venv with Hermes's own dependencies in "
                           "$HERMES_HOME/review-loop-runtime.json (docs/configuration.md)") from None


home = os.environ["HERMES_HOME"]
if mode == "describe":             # read-only: the profile's config, no credential lookup
    try:
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as handle:
            raw = handle.read()
        cfg = read_config(raw)
    except NoYamlReader as exc:
        done(kind="interpreter", error=str(exc))
    except Exception as exc:
        done(kind="config", error="profile config.yaml unreadable (" + text(exc) + ")")
    block = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(block, str):
        block = {"default": block}
    block = block if isinstance(block, dict) else {}
    done(model=str(block.get("default") or block.get("model") or ""),
         requested=str(block.get("provider") or "auto").strip().lower(),
         base_url=str(block.get("base_url") or ""),
         api_mode=str(block.get("api_mode") or "").strip().lower(),
         openai_runtime=str(block.get("openai_runtime") or "").strip().lower())
try:
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_cli import runtime_provider as rp
except Exception as exc:
    done(kind="unavailable", error="Hermes is not importable from " + source + " (" + text(exc) + ")")
try:
    load_hermes_dotenv(hermes_home=home)
    cfg = rp._get_model_config()
    requested = rp.resolve_requested_provider()
except Exception as exc:
    done(kind="config", error="profile config unreadable (" + text(exc) + ")")
model = str(cfg.get("default") or "").strip()
if mode == "models":
    try:
        from hermes_cli import model_catalog
        block = model_catalog._get_provider_block(requested.split(":", 1)[0])
        ids = [mid for mid, _ in model_catalog._block_ids(block)]
    except Exception as exc:
        done(kind="catalog", error="Hermes catalog unavailable (" + text(exc) + ")",
             model=model, requested=requested)
    declared = []
    try:
        from hermes_cli.config import load_config
        entries = load_config().get("custom_providers") or []
        name = requested.split(":", 1)[1] if requested.startswith("custom:") else ""
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and name and str(entry.get("name") or "").strip().lower() == name:
                raw = entry.get("models") or []
                declared = [str(m.get("id") if isinstance(m, dict) else m) for m in
                            (raw if isinstance(raw, list) else list(raw))]
    except Exception:
        pass
    done(model=model, requested=requested, catalog=ids, declared=declared,
         catalog_known=block is not None)

# -- refusals that must happen before any credential is read ------------------------------------
def refuse(error):
    done(kind="unsupported", model=model, requested=requested, error=error)

if requested in ("", "auto"):
    refuse("the profile names no model.provider, so Hermes would auto-detect one")
if requested in unsupported:
    refuse("provider " + requested + " needs a token exchange, cloud signing or several models, "
           "which the inference proxy does not do")
if str(cfg.get("openai_runtime") or "").strip().lower() == "codex_app_server":
    refuse("model.openai_runtime is codex_app_server: the turn would run a codex subprocess "
           "with its own login, not a proxied API")
configured = str(cfg.get("api_mode") or "").strip().lower()
try:
    from hermes_cli.config_providers import _canonical_api_mode
    configured = _canonical_api_mode(configured).lower() if configured else ""
except Exception:
    pass
if configured in refused_modes:
    refuse("model.api_mode is " + configured + ", which the inference proxy cannot speak")
try:
    from hermes_cli.auth import PROVIDER_REGISTRY
    entry = PROVIDER_REGISTRY.get(requested)
    auth_type = str(getattr(entry, "auth_type", "api_key")) if entry is not None else "api_key"
    if auth_type != "api_key" and requested not in oauth_providers and requested not in anthropic_aliases:
        refuse("provider " + requested + " authenticates by " + auth_type +
               ", which the inference proxy cannot refresh")
except ImportError:
    pass

def resolve():
    runtime = rp.resolve_runtime_provider()
    key = runtime.get("api_key")
    return runtime, key() if callable(key) else key, callable(key)

try:
    runtime, key, minted = resolve()
except Exception as exc:
    done(kind="credential", model=model, requested=requested, error=text(exc))
api_mode = str(runtime.get("api_mode") or "")
provider = str(runtime.get("provider") or "")
if api_mode not in modes:
    refuse("provider " + (provider or requested) + " resolves to api_mode " + (api_mode or "?") +
           ", which the inference proxy cannot speak")

if mode == "refresh" and stale and digest(key) == stale:
    # The host saw this very token rejected (or expiring): make Hermes rotate it, under Hermes's
    # own auth.lock, then resolve again. A peer that refreshed first shows up as a new token.
    try:
        pool = runtime.get("credential_pool")
        if pool is not None and hasattr(pool, "try_refresh_matching"):
            pool.try_refresh_matching(api_key_hint=key)
        else:
            forced = {"openai-codex": "resolve_codex_runtime_credentials",
                      "xai-oauth": "resolve_xai_oauth_runtime_credentials",
                      "qwen-oauth": "resolve_qwen_runtime_credentials"}.get(provider)
            if forced and hasattr(rp, forced):
                getattr(rp, forced)(force_refresh=True)
            elif provider == "nous":
                from hermes_cli.auth import resolve_nous_runtime_credentials
                resolve_nous_runtime_credentials(force_refresh=True, stale_access_token=key)
        runtime, key, minted = resolve()
    except Exception as exc:
        done(kind="credential", model=model, requested=requested, error="refresh failed (" + text(exc) + ")")

base_url = str(runtime.get("base_url") or "")
key = key if isinstance(key, str) else ""
oauth = provider in oauth_providers or requested in oauth_providers
scheme, headers, identity, wire_model = "bearer", {}, "", model
if api_mode == "anthropic_messages":
    try:
        from agent import anthropic_adapter as aa
        from agent.anthropic_credentials import anthropic_route_is_oauth
        if anthropic_route_is_oauth(base_url, key, provider=provider):
            # A Claude subscription token: Bearer plus the Claude Code identity Hermes sends.
            oauth, identity = True, "claude_code"
            headers = {"anthropic-beta": ",".join(aa._common_betas_for_base_url(base_url) + list(aa._OAUTH_ONLY_BETAS)),
                       "user-agent": "claude-code/" + aa._get_claude_code_version() + " (external, cli)",
                       "x-app": "cli"}
        else:
            import re
            style = aa._auth_style(key, base_url, re.sub(r"/v1/?$", "", base_url.rstrip("/")))
            scheme = "x-api-key" if style == "api_key" else "bearer"
            headers = {"anthropic-beta": ",".join(aa._common_betas_for_base_url(base_url))}
            if style == "kimi":
                headers.update(aa._attribution_headers())
        if not aa._is_nous_portal_endpoint(base_url):
            wire_model = aa.normalize_model_name(model)
    except Exception as exc:
        done(kind="unsupported", model=model, requested=requested,
             error="cannot derive the Anthropic wire headers from this Hermes (" + text(exc) + ")")
else:
    try:
        from agent.agent_init import _host_default_headers_factory
        factory = _host_default_headers_factory(base_url)
        headers = dict(factory(key, base_url)) if factory else {}
    except Exception:
        headers = {}
    if api_mode == "codex_responses" and not headers and "chatgpt.com" in base_url:
        try:
            from agent.codex_headers import codex_cloudflare_headers
            headers = codex_cloudflare_headers(key, base_url=base_url)
        except Exception:
            headers = {}
    if api_mode == "codex_responses":
        try:
            from agent.model_metadata import strip_codex_context_variant_suffix
            wire_model = strip_codex_context_variant_suffix(model) or model
        except Exception:
            pass
done(model=model, requested=requested, provider=provider, api_mode=api_mode, base_url=base_url,
     key=key, auth="oauth" if oauth else "api_key", scheme=scheme,
     headers={str(k): str(v) for k, v in (headers or {}).items() if v is not None},
     expires_at=expiry(runtime, key) if oauth else None, wire_model=wire_model,
     identity=identity, refreshable=bool(oauth or minted))
'''


def hermes_interpreter(settings: dict | None) -> tuple[str, str]:
    """(python, source) for running Hermes: the runtime's venv and checkout, else this process."""
    if settings and settings.get("venv") and settings.get("source"):
        return str(Path(settings["venv"]) / "bin" / "python"), str(settings["source"])
    try:
        import hermes_cli  # noqa: F401 — the plugin normally runs inside Hermes
    except ImportError:
        raise SeatModelError("Hermes is not importable here and no runtime file names its venv "
                             "and source; write $HERMES_HOME/review-loop-runtime.json") from None
    return sys.executable, str(Path(hermes_cli.__file__).resolve().parents[1])


def _redact(text: str, key: str = "") -> str:
    text = str(text)
    if key and len(key) >= 4:
        text = text.replace(key, "[REDACTED]")
    return _SECRETISH.sub("[REDACTED]", text)[:400]


_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def profile_lock(profile: str):
    """Serialize every credential resolution of one profile, across threads and processes.

    Two seats (or two turns) sharing a profile must not refresh its OAuth token in parallel: the
    second waits, then resolves the token the first one refreshed. Hermes's own ``auth.lock``
    still guards ``auth.json`` inside the resolution; this lock keeps review-loop from even
    starting a second resolution meanwhile. The lock file lives in review-loop's state
    directory, never in the profile.
    """
    home = str(config.profile_dir(profile).expanduser().resolve())
    with _PROFILE_LOCKS_GUARD:
        local = _PROFILE_LOCKS.setdefault(home, threading.Lock())
    with local:
        directory = config.home() / "state" / "review-loop-seat-locks"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = open(directory / (hashlib.sha256(home.encode()).hexdigest()[:24] + ".lock"), "a")
        except OSError:
            handle = None                       # read-only state: the thread lock still holds
        try:
            if handle is not None:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    pass
            yield
        finally:
            if handle is not None:
                handle.close()                  # closing releases the flock


def run_resolver(profile: str, mode: str, settings: dict | None,
                 timeout: int = RESOLVE_TIMEOUT, stale: str = "") -> dict:
    """Run Hermes as ``profile`` in its own process and return its JSON answer.

    The child's environment is built from scratch: HOME, PATH, and HERMES_HOME set to the profile
    home. Nothing from this process's environment — which, inside Hermes, holds the launch
    profile's provider keys — reaches the seat's resolution. ``resolve`` and ``refresh`` (which
    may rotate an OAuth token) run under ``profile_lock``; ``stale`` is the SHA-256 of a token
    the upstream rejected, so Hermes rotates that token rather than hand it back.
    """
    home = config.profile_dir(profile)
    python, source = hermes_interpreter(settings)
    try:
        import pwd
        user_home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        user_home = os.environ.get("HOME", "/")
    env = {"PATH": "/usr/bin:/bin", "HOME": user_home,
           "HERMES_HOME": str(home), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    from .inference_proxy import SUPPORTED_MODES
    policy = json.dumps({"unsupported": sorted(UNSUPPORTED_PROVIDERS), "oauth": OAUTH_PROVIDERS,
                         "modes": sorted(SUPPORTED_MODES), "refused_modes": sorted(REFUSED_API_MODES),
                         "anthropic": sorted(ANTHROPIC_ALIASES), "stale": stale})
    locked = profile_lock(profile) if mode in ("resolve", "refresh") else contextlib.nullcontext()
    try:
        with locked:
            process = subprocess.run([python, "-E", "-s", "-c", _RESOLVER, source, mode, policy],
                                     env=env, cwd=str(home), stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise SeatModelError(f"profile {profile}: Hermes did not resolve within {timeout}s") from None
    except OSError as exc:
        raise SeatModelError(f"profile {profile}: cannot run Hermes at {python} "
                             f"({type(exc).__name__}); check venv/source in the runtime file") from None
    try:
        answer = json.loads(process.stdout[:65536].decode("utf-8"))
        if not isinstance(answer, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise SeatModelError(f"profile {profile}: Hermes gave no answer "
                             f"(rc={process.returncode}); check venv/source in the runtime file") from None
    return answer


def seats_for(loop: dict) -> list[str]:
    """The seats this loop can run isolated turns for (the adjudicator only with its route)."""
    return ["reviewer", "fixer"] + (
        ["adjudicator"] if str((loop.get("adjudicator") or {}).get("route") or "") else [])


# Credential files Hermes may read for an OAuth/subscription seat outside the profile directory
# (Claude Code's login, the Codex CLI's, Qwen's) — in the user's real home, never in the sandbox.
USER_CREDENTIAL_FILES = (".claude/.credentials.json", ".codex/auth.json", ".qwen/oauth_creds.json")
PROFILE_SECRET_FILES = (".env", "auth.json", "auth.lock", "config.yaml", ".anthropic_oauth.json")


def secret_paths(loop: dict, settings: dict | None) -> list[str]:
    """Host files holding a seat's provider credential — none may be readable in the sandbox."""
    paths: list[str] = []
    for block in [legacy_override(settings or {}), *((settings or {}).get("seats") or {}).values()]:
        if block:
            paths.append(str(Path(block["key_file"]).expanduser()))
    for seat in seats_for(loop):
        home = config.profile_dir(config.seat_profile(loop, seat))
        paths += [str(home / name) for name in PROFILE_SECRET_FILES]
    try:
        import pwd
        user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        user_home = Path.home()
    paths += [str(user_home / name) for name in USER_CREDENTIAL_FILES]
    return list(dict.fromkeys(paths))


def profile_problem(profile: str, seat: str) -> str:
    """Why this profile name cannot be used at all, or ``""``."""
    if not profile:
        return f"no Hermes profile is configured for the {seat} seat"
    if not config.profile_exists(profile):
        return (f"profile {profile} does not exist at {config.profile_dir(profile)} "
                "(or has no config.yaml)")
    return ""


def _headers(raw: object) -> tuple:
    if not isinstance(raw, dict):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in raw.items()))


def _expiry(raw: object) -> float | None:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def resolve_profile(profile: str, seat: str, settings: dict | None, *,
                    stale: str = "") -> SeatInference:
    """The seat's model from its profile, via Hermes; ``SeatModelError`` with a fix otherwise.

    ``stale`` (a SHA-256 of a rejected access token) asks Hermes to rotate that token first —
    the host's refresh path; the caller never sees a refresh token either way.
    """
    problem = profile_problem(profile, seat)
    if problem:
        raise SeatModelError(problem)
    answer = run_resolver(profile, "refresh" if stale else "resolve", settings, stale=stale)
    key = answer.get("key") if isinstance(answer.get("key"), str) else ""
    where = f"profile {profile}"
    requested = str(answer.get("requested") or "")
    fixes = {"unsupported": "set this profile's model.provider to one whose Hermes runtime is "
                            "chat_completions, codex_responses or anthropic_messages (an API-key "
                            "provider, openai-codex, xai-oauth, qwen-oauth, nous, minimax-oauth, "
                            "anthropic), or add a seats." + seat + " override to the runtime file",
             "credential": f"give profile {profile} its provider login or key (`hermes -p {profile} "
                           "auth`, or its .env), or add a seats override",
             "config": f"repair {config.profile_dir(profile) / 'config.yaml'}",
             "interpreter": "name a venv with Hermes's own dependencies in the runtime file "
                            "($HERMES_HOME/review-loop-runtime.json): the interpreter the host "
                            "picked cannot read YAML, so no profile's model can be resolved",
             "unavailable": "point source/venv in the runtime file at the Hermes install"}
    if answer.get("error"):
        kind = str(answer.get("kind") or "")
        raise SeatModelError(f"{where}{' (' + requested + ')' if requested else ''}: "
                             f"{_redact(answer['error'], key)} — {fixes.get(kind, 'see above')}")
    from .inference_proxy import SUPPORTED_MODES
    model = str(answer.get("model") or "").strip()
    provider = str(answer.get("provider") or requested)
    api_mode = str(answer.get("api_mode") or "")
    if api_mode not in SUPPORTED_MODES:
        raise SeatModelError(f"{where} ({requested}): provider speaks {api_mode or 'an unknown API'}, "
                             "which the inference proxy cannot forward — " + fixes["unsupported"])
    if not model:
        raise SeatModelError(f"{where}: no model.default set — `hermes -p {profile} model`")
    if not key or "\n" in key or "\r" in key:
        raise SeatModelError(f"{where} ({requested}): resolved without a usable credential — "
                             f"{fixes['credential']}")
    upstream = upstream_for(str(answer.get("base_url") or ""), where, api_mode)
    auth = "oauth" if answer.get("auth") == "oauth" else "api_key"
    scheme = "x-api-key" if answer.get("scheme") == "x-api-key" else "bearer"
    try:
        from .inference_proxy import Credential
        Credential(key, scheme, _headers(answer.get("headers")))
    except ValueError:
        raise SeatModelError(f"{where} ({requested}): Hermes gave headers the proxy will not "
                             "send (credential-like or malformed)") from None
    wire = str(answer.get("wire_model") or "").strip()
    return SeatInference(seat, profile, "profile", requested or provider, model, upstream, key,
                         api_mode=api_mode, auth=auth, scheme=scheme,
                         headers=_headers(answer.get("headers")),
                         expires_at=_expiry(answer.get("expires_at")),
                         wire_model=wire if wire and wire != model else "",
                         client_identity="claude_code" if answer.get("identity") == "claude_code" else "",
                         refreshable=bool(answer.get("refreshable")), settings=settings)


def resolve_seat(loop: dict, seat: str, settings: dict, *, resolver=None) -> SeatInference:
    """Apply the precedence (override > profile > legacy) for one seat, or raise with the reason."""
    if seat not in SEATS:
        raise SeatModelError(f"unknown seat {seat!r}")
    resolver = resolver or resolve_profile
    profile = config.seat_profile(loop, seat)
    override = seat_override(settings, seat)
    if override is not None:
        return _from_override(override, seat, profile, "override", f"runtime seats.{seat}")
    try:
        return resolver(profile, seat, settings)
    except SeatModelError as exc:
        legacy = legacy_override(settings)
        if legacy is None:
            raise
        return _from_override(legacy, seat, profile, "legacy", "runtime model/upstream/key_file",
                              warning=f"{exc}; using the legacy runtime model instead")


def expected_wire(requested: str, api_mode: str = "") -> tuple[str, str]:
    """``(api_mode, auth label)`` a provider is expected to resolve to — no credential lookup."""
    if requested in OAUTH_PROVIDERS:
        return OAUTH_PROVIDERS[requested], "OAuth (host-refreshed)"
    if requested in ANTHROPIC_ALIASES:
        return "anthropic_messages", "API key, or Claude subscription OAuth (host-refreshed)"
    return api_mode or "chat_completions", "API key"


def describe_seat(loop: dict, seat: str, settings: dict | None) -> tuple[str, str, str]:
    """Read-only (no credential lookup) — ``(status, detail, fix)`` with status ok|warn|fail."""
    profile = config.seat_profile(loop, seat)
    override = seat_override(settings or {}, seat)
    if override is not None:
        return ("ok", f"runtime override seats.{seat}: {override['model']} via "
                      f"{urlsplit(override['upstream']).hostname} [chat_completions, API key] "
                      f"(profile {profile or '-'} unused)", "")
    legacy = legacy_override(settings or {})
    problem = profile_problem(profile, seat)
    reason = problem
    if not problem:
        try:
            answer = run_resolver(profile, "describe", settings)
        except SeatModelError as exc:
            return ("warn", f"profile {profile}: {exc}", "run `hermes review-loop selftest`")
        requested = str(answer.get("requested") or "auto")
        model = str(answer.get("model") or "")
        configured = str(answer.get("api_mode") or "")
        if answer.get("error"):
            reason = f"profile {profile}: {_redact(answer['error'])}"
            if str(answer.get("kind") or "") == "interpreter":
                return ("fail", f"{reason}; the {seat} turn will be held",
                        "name a venv with Hermes's own dependencies in "
                        f"{config.home() / 'review-loop-runtime.json'} — the interpreter the host "
                        "picked cannot read YAML at all (docs/configuration.md)")
        elif requested in ("", "auto"):
            reason = f"profile {profile} names no model.provider"
        elif requested in UNSUPPORTED_PROVIDERS:
            reason = (f"profile {profile} uses {requested}, which the inference proxy cannot "
                      "speak (token exchange, cloud signing or several models)")
        elif answer.get("openai_runtime") == "codex_app_server":
            reason = (f"profile {profile} runs the codex_app_server runtime, which the inference "
                      "proxy cannot carry")
        elif configured in REFUSED_API_MODES:
            reason = f"profile {profile} sets api_mode {configured}, which the proxy cannot speak"
        elif not model:
            reason = f"profile {profile} has no model.default"
        else:
            base = answer.get("base_url") or ""
            mode, label = expected_wire(requested, configured)
            return ("ok", f"profile {profile}: {requested} / {model}"
                          + (f" via {urlsplit(base).hostname}" if base else "")
                          + f" [{mode}, {label}] (credential checked by selftest)", "")
    fix = (f"set a supported provider in profile {profile or '<name>'} (see `docs/configuration.md`), "
           f"or add seats.{seat} to the runtime file")
    if legacy is not None:
        return ("warn", f"{reason}; falls back to the LEGACY runtime model {legacy['model']} "
                        "(every such seat shares it)", fix)
    return ("fail", f"{reason}; the {seat} turn will be held", fix)
