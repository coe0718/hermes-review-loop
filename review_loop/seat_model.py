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
* the result must be an OpenAI chat-completions endpoint over HTTPS with a non-empty key, because
  that is all the inference proxy forwards (see ``inference_proxy``). OAuth/subscription providers
  (``openai-codex``, ``nous``, ``xai-oauth``, ``qwen-oauth``, ``minimax-oauth``, Copilot), Anthropic
  Messages, Bedrock and Vertex are refused *before* their credentials are touched.

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

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

from . import config

SEATS = ("reviewer", "fixer", "adjudicator")
HOST_KEYS = ("source", "venv", "runtime", "rust")
OVERRIDE_KEYS = ("model", "upstream", "key_file")
RESOLVE_TIMEOUT = 90

# Providers whose credential is an OAuth login, a token exchange, or a non-chat-completions wire
# protocol. They are refused by name before Hermes resolves (and possibly refreshes) anything.
UNSUPPORTED_PROVIDERS = frozenset({
    "openai-codex", "nous", "xai-oauth", "qwen-oauth", "minimax-oauth", "anthropic",
    "claude-code", "copilot", "copilot-acp", "github-copilot", "bedrock", "aws-bedrock",
    "vertex", "google-vertex", "vertex-ai", "gcp-vertex", "vertexai", "azure-foundry", "moa",
})

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

    @property
    def host(self) -> str:
        return urlsplit(self.upstream).hostname or ""

    def identity(self) -> tuple:
        """Distinct (provider, model, endpoint, credential) — for once-per-resolution checks."""
        return (self.provider, self.model, self.upstream,
                hashlib.sha256(self.key.encode()).hexdigest())

    def describe(self) -> str:
        where = {"override": "runtime override seats." + self.seat,
                 "legacy": "LEGACY runtime model",
                 "profile": "profile " + self.profile}[self.origin]
        what = self.model if self.origin != "profile" else f"{self.provider} / {self.model}"
        return f"{where}: {what} via {self.host}"


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


def check_upstream(upstream: str, where: str) -> str:
    from .inference_proxy import UPSTREAM_SUFFIX, _NoRedirectConnection
    if urlsplit(upstream).scheme != "https":
        raise SeatModelError(f"{where}: inference must use HTTPS, got {upstream.split(':', 1)[0]!r}")
    try:
        _NoRedirectConnection(upstream)
    except ValueError:
        raise SeatModelError(f"{where}: upstream must be https://host[:port]/…{UPSTREAM_SUFFIX} "
                             "with no credentials, query or fragment") from None
    return upstream


def upstream_for(base_url: str, where: str) -> str:
    """A provider base URL (``…/v1``) → the proxy's fixed chat-completions URL."""
    from .inference_proxy import UPSTREAM_SUFFIX
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise SeatModelError(f"{where}: the provider resolved without a base URL")
    return check_upstream(base if base.endswith(UPSTREAM_SUFFIX) else base + UPSTREAM_SUFFIX, where)


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
import json, os, sys
out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)                      # anything Hermes prints goes to stderr, never into our answer
sys.stdout = sys.stderr
source, mode, unsupported = sys.argv[1], sys.argv[2], set(json.loads(sys.argv[3]))
sys.path.insert(0, source)

def done(**answer):
    out.write(json.dumps(answer))
    out.flush()
    os._exit(0)

def text(exc):
    return (type(exc).__name__ + ": " + str(exc))[:400]

home = os.environ["HERMES_HOME"]
if mode == "describe":             # read-only: the profile's config, no credential lookup
    try:
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as handle:
            raw = handle.read()
        try:
            import yaml
            cfg = yaml.safe_load(raw) or {}
        except ImportError:          # a JSON config is valid YAML; Hermes itself always has yaml
            cfg = json.loads(raw)
    except Exception as exc:
        done(kind="config", error="profile config.yaml unreadable (" + text(exc) + ")")
    block = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(block, str):
        block = {"default": block}
    block = block if isinstance(block, dict) else {}
    done(model=str(block.get("default") or block.get("model") or ""),
         requested=str(block.get("provider") or "auto").strip().lower(),
         base_url=str(block.get("base_url") or ""))
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
if requested in ("", "auto"):
    done(kind="unsupported", model=model, requested=requested,
         error="the profile names no model.provider, so Hermes would auto-detect one")
if requested in unsupported:
    done(kind="unsupported", model=model, requested=requested, error="provider " + requested +
         " authenticates by OAuth/subscription or speaks a non-chat-completions API")
try:
    from hermes_cli.auth import PROVIDER_REGISTRY
    entry = PROVIDER_REGISTRY.get(requested)
    if entry is not None and getattr(entry, "auth_type", "api_key") != "api_key":
        done(kind="unsupported", model=model, requested=requested, error="provider " + requested +
             " authenticates by " + str(entry.auth_type) + ", not an API key")
except ImportError:
    pass
try:
    runtime = rp.resolve_runtime_provider()
except Exception as exc:
    done(kind="credential", model=model, requested=requested, error=text(exc))
key = runtime.get("api_key")
try:
    key = key() if callable(key) else key
except Exception as exc:
    done(kind="credential", model=model, requested=requested, error="key command failed (" + text(exc) + ")")
done(model=model, requested=requested, provider=str(runtime.get("provider") or ""),
     api_mode=str(runtime.get("api_mode") or ""), base_url=str(runtime.get("base_url") or ""),
     key=key if isinstance(key, str) else "")
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


def run_resolver(profile: str, mode: str, settings: dict | None,
                 timeout: int = RESOLVE_TIMEOUT) -> dict:
    """Run Hermes as ``profile`` in its own process and return its JSON answer.

    The child's environment is built from scratch: HOME, PATH, and HERMES_HOME set to the profile
    home. Nothing from this process's environment — which, inside Hermes, holds the launch
    profile's provider keys — reaches the seat's resolution.
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
    try:
        process = subprocess.run([python, "-E", "-s", "-c", _RESOLVER, source, mode,
                                  json.dumps(sorted(UNSUPPORTED_PROVIDERS))],
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


def secret_paths(loop: dict, settings: dict | None) -> list[str]:
    """Host files holding a seat's provider credential — none may be readable in the sandbox."""
    paths: list[str] = []
    for block in [legacy_override(settings or {}), *((settings or {}).get("seats") or {}).values()]:
        if block:
            paths.append(str(Path(block["key_file"]).expanduser()))
    for seat in seats_for(loop):
        home = config.profile_dir(config.seat_profile(loop, seat))
        paths += [str(home / name) for name in (".env", "auth.json", "config.yaml")]
    return list(dict.fromkeys(paths))


def profile_problem(profile: str, seat: str) -> str:
    """Why this profile name cannot be used at all, or ``""``."""
    if not profile:
        return f"no Hermes profile is configured for the {seat} seat"
    if not config.profile_exists(profile):
        return (f"profile {profile} does not exist at {config.profile_dir(profile)} "
                "(or has no config.yaml)")
    return ""


def resolve_profile(profile: str, seat: str, settings: dict | None) -> SeatInference:
    """The seat's model from its profile, via Hermes; ``SeatModelError`` with a fix otherwise."""
    problem = profile_problem(profile, seat)
    if problem:
        raise SeatModelError(problem)
    answer = run_resolver(profile, "resolve", settings)
    key = answer.get("key") if isinstance(answer.get("key"), str) else ""
    where = f"profile {profile}"
    requested = str(answer.get("requested") or "")
    fixes = {"unsupported": "set this profile's model.provider to an API-key, OpenAI-compatible "
                            "provider (custom:<name>, openrouter, deepseek, …), or add a "
                            f"seats.{seat} override to the runtime file",
             "credential": f"give profile {profile} its provider key (`hermes -p {profile} auth` "
                           "or its .env), or add a seats override",
             "config": f"repair {config.profile_dir(profile) / 'config.yaml'}",
             "unavailable": "point source/venv in the runtime file at the Hermes install"}
    if answer.get("error"):
        kind = str(answer.get("kind") or "")
        raise SeatModelError(f"{where}{' (' + requested + ')' if requested else ''}: "
                             f"{_redact(answer['error'], key)} — {fixes.get(kind, 'see above')}")
    model = str(answer.get("model") or "").strip()
    provider = str(answer.get("provider") or requested)
    if str(answer.get("api_mode") or "") != "chat_completions":
        raise SeatModelError(f"{where} ({requested}): provider speaks "
                             f"{answer.get('api_mode') or 'an unknown API'}, but the inference "
                             f"proxy forwards only OpenAI chat-completions — {fixes['unsupported']}")
    if not model:
        raise SeatModelError(f"{where}: no model.default set — `hermes -p {profile} model`")
    if not key or "\n" in key or "\r" in key:
        raise SeatModelError(f"{where} ({requested}): resolved without a usable API key — "
                             f"{fixes['credential']}")
    upstream = upstream_for(str(answer.get("base_url") or ""), where)
    return SeatInference(seat, profile, "profile", requested or provider, model, upstream, key)


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


def describe_seat(loop: dict, seat: str, settings: dict | None) -> tuple[str, str, str]:
    """Read-only (no credential lookup) — ``(status, detail, fix)`` with status ok|warn|fail."""
    profile = config.seat_profile(loop, seat)
    override = seat_override(settings or {}, seat)
    if override is not None:
        return ("ok", f"runtime override seats.{seat}: {override['model']} via "
                      f"{urlsplit(override['upstream']).hostname} (profile {profile or '-'} unused)", "")
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
        if answer.get("error"):
            reason = f"profile {profile}: {_redact(answer['error'])}"
        elif requested in ("", "auto"):
            reason = f"profile {profile} names no model.provider"
        elif requested in UNSUPPORTED_PROVIDERS:
            reason = (f"profile {profile} uses {requested}, which the inference proxy cannot "
                      "speak (OAuth/subscription or non-chat-completions)")
        elif not model:
            reason = f"profile {profile} has no model.default"
        else:
            base = answer.get("base_url") or ""
            return ("ok", f"profile {profile}: {requested} / {model}"
                          + (f" via {urlsplit(base).hostname}" if base else "")
                          + " (credential checked by selftest)", "")
    fix = (f"set a chat-completions provider in profile {profile or '<name>'}, or add "
           f"seats.{seat} to the runtime file")
    if legacy is not None:
        return ("warn", f"{reason}; falls back to the LEGACY runtime model {legacy['model']} "
                        "(every such seat shares it)", fix)
    return ("fail", f"{reason}; the {seat} turn will be held", fix)
