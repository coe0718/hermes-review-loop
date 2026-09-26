"""``hermes review-loop selftest`` — verify the live isolated path, step by step.

``doctor`` answers "is this loop installed?". This answers the question the issue #16 live
verification asks: *can the host actually run one isolated turn?* Each step is independent where
it can be, prints one ✅/❌ line per check with the command that fixes a failure, and the verb
exits 1 if anything failed:

1. the private runtime file (``$HERMES_HOME/review-loop-runtime.json``) and the paths it names,
   then each seat's model as the worker would resolve it (its Hermes profile, or a runtime
   override — see ``seat_model``), shown as profile → provider / model with its
   ``[api_mode, API key | OAuth (host-refreshed)]``, never the key or token;
2. bubblewrap with unprivileged user namespaces, and a probe *inside* the real sandbox layout
   (configured venv, runtime and Rust, a staged source snapshot) that must not be able to read a
   dummy host secret, any model key file, the seat profiles' ``.env``/``auth.json``/``config.yaml``,
   the PATs, the runtime file or ``$HERMES_HOME/.env``;
3. one tiny real request in the seat's wire format (chat completion, Responses or Messages)
   through the host inference capability, once per distinct seat resolution (``--no-model``
   skips it);
4. the read/reviewer/fixer (and optional adjudicator) tokens resolve via ``/user`` to distinct
   principals with the expected logins, and the repository is readable;
5. with ``--pr N``: the broker's reviewer-write authorization, run with reads only;
6. the supervisor ledger migrates and ``status`` works, plus ``doctor``'s cron/state/gateway checks
   and the observer route;
7. with ``--live-turn --pr N``: one real isolated reviewer turn whose broker runs in its host-only
   no-write mode — the verdict and body the agent *would* have submitted are printed, never posted.

Guarantees: **no step writes to GitHub** (every GitHub call made while the selftest runs goes
through a GET-only guard, on top of the broker's no-write mode), and no token or key is printed
(every line passes a redactor that knows the configured secrets).
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from . import config, doctor, gh

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"
MARKS = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "⏭️ "}
RUNTIME_KEYS = ("source", "venv", "runtime", "rust")   # required; the model comes from each seat
DEFAULT_TIMEOUT = 600  # a real review routinely outlasts a two-minute budget


class WriteBlocked(RuntimeError):
    """A selftest step tried a non-GET GitHub call; it is refused before any network I/O."""


def runtime_path() -> Path:
    return config.home() / "review-loop-runtime.json"


def ledger_path() -> Path:
    return config.home() / "state" / "review-loop-runs.sqlite"


# -- redaction ---------------------------------------------------------------------------------

_TOKEN_SHAPES = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
                           r"|sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+|token\s+[A-Za-z0-9_]{20,})")


class Redactor:
    """Replace every configured secret (and anything shaped like a token) before printing."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, value: object) -> None:
        if isinstance(value, str) and len(value.strip()) >= 4:
            self._secrets.add(value.strip())

    def add_file(self, path: object) -> None:
        try:
            self.add(Path(str(path)).expanduser().read_text())
        except Exception:
            pass

    def __call__(self, text: object) -> str:
        text = str(text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return _TOKEN_SHAPES.sub("[REDACTED]", text)


# -- report ------------------------------------------------------------------------------------

class Report:
    def __init__(self, out=None, redact: Redactor | None = None) -> None:
        self.out = out or sys.stdout
        self.redact = redact or Redactor()
        self.results: list[tuple[str, str, str, str, str]] = []

    def step(self, title: str) -> None:
        self._print(f"\n{title}")

    def add(self, step: str, name: str, status: str, detail: str, fix: str = "") -> str:
        self.results.append((step, name, status, detail, fix))
        self._print(f"  {MARKS[status]} {name:<22} {detail}")
        if fix and status in (FAIL, WARN):
            self._print(f"      fix: {fix}")
        return status

    def text(self, text: str) -> None:
        self._print(text)

    def _print(self, text: str) -> None:
        print(self.redact(text), file=self.out, flush=True)

    def failed(self, step: str | None = None) -> bool:
        return any(r[2] == FAIL and (step is None or r[0] == step) for r in self.results)

    def counts(self) -> dict:
        out = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
        for result in self.results:
            out[result[2]] += 1
        return out


_DOCTOR = {doctor.VERIFIED: PASS, doctor.ABSENT: FAIL, doctor.MISMATCH: FAIL,
           doctor.UNKNOWN: WARN}


def _from_doctor(report: Report, step: str, check) -> None:
    report.add(step, check.name, _DOCTOR[check.status],
               doctor._safe_report_text(check.detail), doctor._safe_report_text(check.fix or ""))


# -- GitHub: GET only --------------------------------------------------------------------------

@contextmanager
def github_read_only():
    """Refuse any non-GET GitHub call for the duration, whatever code path makes it."""
    original = gh.fetch

    def guarded(loop, path, method="GET", body=None, login=None):
        if str(method).upper() != "GET" or body is not None:
            raise WriteBlocked(f"selftest refused a GitHub {method} to {path}")
        return original(loop, path, method, body, login)

    gh.fetch = guarded
    try:
        yield
    finally:
        gh.fetch = original


@contextmanager
def worker_tempdir():
    """Use the temp directory the production worker gets.

    ``run_supervisor._spawn`` starts the worker without ``TMPDIR``, so its per-turn socket
    directories land in ``/tmp``. A long ``TMPDIR`` in the operator's shell would otherwise make
    the selftest fail (AF_UNIX path limit) where the worker would not, or pass where it would not.
    """
    saved = tempfile.tempdir
    for candidate in ("/tmp", "/var/tmp", "/usr/tmp"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            tempfile.tempdir = candidate
            break
    try:
        yield
    finally:
        tempfile.tempdir = saved


# -- step 1: runtime ---------------------------------------------------------------------------

def _private_file(path: Path) -> tuple[bool, str, str]:
    """(ok, detail, fix) for a file that must be a regular, owned, 0600-style private file."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False, f"no file at {path}", ""
    if stat.S_ISLNK(info.st_mode):
        return False, f"{path} is a symlink", f"replace {path} with the regular file it points to"
    if not stat.S_ISREG(info.st_mode):
        return False, f"{path} is not a regular file", f"make {path} a regular file"
    if info.st_uid != os.getuid():
        return False, f"{path} is owned by uid {info.st_uid}, not you ({os.getuid()})", \
            f"chown {os.getuid()} {path} && chmod 600 {path}"
    mode = info.st_mode & 0o777
    if mode & 0o077:
        return False, f"{path} is mode {mode:03o} (group/other can read it)", f"chmod 600 {path}"
    return True, f"{path} (mode {mode:03o}, owned by you)", ""


def _interpreter_outside(link: Path, runtime: Path) -> str:
    """The venv interpreter's link target when the sandbox could not follow it, else ``""``.

    The sandbox binds ``runtime`` at the same absolute path, so the venv's literal link target
    must lie under ``runtime`` as written, and must still resolve inside it.
    """
    if not link.is_symlink():
        return ""
    target = Path(os.path.normpath(link.parent / os.readlink(link)))
    root = Path(os.path.normpath(runtime.absolute()))
    if root != target and root not in target.parents:
        return str(target)
    resolved, real_root = Path(os.path.realpath(target)), Path(os.path.realpath(root))
    if real_root != resolved and real_root not in resolved.parents:
        return str(resolved)
    return ""


def _check_override(report: Report, step: str, prefix: str, block: dict) -> bool:
    """An explicit model override's upstream and key file; True when both are usable."""
    from .inference_proxy import UPSTREAM_SUFFIX, _NoRedirectConnection
    ok = True
    upstream = block["upstream"]
    parts = urlsplit(upstream)
    try:
        _NoRedirectConnection(upstream)
        shape_ok = True
    except ValueError:
        shape_ok = False
    if parts.scheme != "https":
        ok = False
        report.add(step, prefix + "upstream", FAIL, f"upstream scheme is {parts.scheme or '(none)'!r}",
                   "use an https:// URL: the production worker refuses plain HTTP inference")
    elif not shape_ok:
        ok = False
        report.add(step, prefix + "upstream", FAIL,
                   f"upstream must be https://host[:port]/…{UPSTREAM_SUFFIX} with no credentials, "
                   "query or fragment", f"set upstream to the provider's full …{UPSTREAM_SUFFIX} URL")
    else:
        report.add(step, prefix + "upstream", PASS,
                   f"https://{parts.hostname}{parts.path} (model {block['model']})")
    key_path = Path(block["key_file"]).expanduser()
    good, detail, fix = _private_file(key_path)
    if good and not key_path.read_text().strip():
        good, detail, fix = False, f"{key_path} is empty", f"write the provider API key into {key_path}"
    if not good:
        ok = False
        report.add(step, prefix + "key_file", FAIL, detail,
                   fix or f"write the provider API key to {key_path} and `chmod 600 {key_path}`")
    else:
        report.add(step, prefix + "key_file", PASS, detail + ", non-empty")
    return ok


def check_runtime(report: Report, path: Path) -> dict | None:
    """Step 1. Returns the settings when the turn could start from them, else ``None``."""
    from . import seat_model
    step = "runtime"
    template = ('{"source": "/path/to/hermes-agent", "venv": "/path/to/hermes-agent/venv", '
                '"runtime": "/path/to/python-runtime", "rust": "~/.rustup/toolchains/stable-..."}')
    ok, detail, fix = _private_file(path)
    if not ok:
        if not path.exists() and not path.is_symlink():
            fix = f"write {path} with {template} and `chmod 600 {path}`"
        report.add(step, "runtime:file", FAIL, detail, fix)
        return None
    try:
        settings = json.loads(path.read_text())
    except Exception as exc:
        report.add(step, "runtime:file", FAIL, f"{path} is not valid JSON ({type(exc).__name__})",
                   f"repair {path}: the worker refuses to start without it")
        return None
    try:
        seat_model.parse_runtime(settings)
    except ValueError as exc:
        report.add(step, "runtime:file", FAIL, str(exc),
                   f"edit {path}: required {', '.join(RUNTIME_KEYS)}; optional seats.<seat> "
                   "{model, upstream, key_file} overrides (and the legacy top-level trio)")
        return None
    report.add(step, "runtime:file", PASS, detail + ", host paths present")
    usable = True

    legacy = seat_model.legacy_override(settings)
    blocks = [("runtime:", legacy)] if legacy is not None else []
    blocks += [(f"runtime:seats.{seat}.", block)
               for seat, block in sorted((settings.get("seats") or {}).items())]
    for prefix, block in blocks:
        usable = _check_override(report, step, prefix, block) and usable
    if legacy is not None:
        report.add(step, "runtime:legacy-model", WARN,
                   f"top-level model {legacy['model']} is a LEGACY fallback: used only for a seat "
                   "whose profile cannot be resolved, and every such seat then shares it",
                   "once each seat's profile resolves, drop model/upstream/key_file from the "
                   "runtime file (or move them under seats.<seat> as an explicit override)")

    source = Path(settings["source"])
    if not source.is_dir():
        usable = False
        report.add(step, "runtime:source", FAIL, f"no directory at {source}",
                   "point source at the hermes-agent Git checkout (it holds run_agent.py)")
    elif not (source / "run_agent.py").is_file() or not (source / ".git").exists():
        usable = False
        report.add(step, "runtime:source", FAIL,
                   f"{source} is not a hermes-agent Git checkout (run_agent.py and .git required)",
                   "point source at the hermes-agent Git checkout; the turn snapshots its HEAD commit")
    else:
        report.add(step, "runtime:source", PASS, f"{source} (Git checkout with run_agent.py)")

    venv = Path(settings["venv"])
    missing = [name for name in ("bin/python", "bin/hermes") if not (venv / name).exists()]
    if missing:
        usable = False
        report.add(step, "runtime:venv", FAIL, f"{venv} lacks {', '.join(missing)}",
                   f"point venv at the virtualenv Hermes is installed in (it must hold bin/hermes)")
    else:
        report.add(step, "runtime:venv", PASS, f"{venv} (bin/python, bin/hermes)")

    runtime = Path(settings["runtime"])
    outside = "" if missing else _interpreter_outside(venv / "bin/python", runtime)
    if not runtime.is_dir():
        usable = False
        report.add(step, "runtime:runtime", FAIL, f"no directory at {runtime}",
                   "set runtime to the Python installation the venv was created from")
    elif outside:
        usable = False
        report.add(step, "runtime:runtime", FAIL,
                   f"the venv interpreter points to {outside}, outside {runtime}",
                   f"set runtime to {Path(outside).parents[2]} (the directory holding the "
                   "interpreter's install): the sandbox mounts runtime at its own path and "
                   "nothing else, so a link leaving it cannot start")
    else:
        report.add(step, "runtime:runtime", PASS, f"{runtime}")

    rust = Path(settings["rust"])
    if not (rust / "bin/cargo").is_file():
        usable = False
        report.add(step, "runtime:rust", FAIL, f"{rust}/bin/cargo does not exist",
                   "point rust at a toolchain directory, e.g. "
                   "~/.rustup/toolchains/stable-x86_64-unknown-linux-gnu")
    else:
        report.add(step, "runtime:rust", PASS, f"{rust} (bin/cargo)")
    return settings if usable else None


# -- step 2: bubblewrap ------------------------------------------------------------------------

_PROBE = r'''
import json, os, socket, subprocess, sys
paths = json.loads(sys.argv[1])
readable = []
for path in paths:
    for candidate in (path, "/proc/self/root" + path, "/proc/1/root" + path):
        try:
            with open(candidate, "rb") as handle:
                handle.read(1)
        except OSError:
            continue
        readable.append(path)
        break
network = True
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
except OSError:
    network = False
try:
    socket.getaddrinfo("api.github.com", 443)
    dns = True
except OSError:
    dns = False
try:
    cargo = subprocess.run(["/opt/rust/bin/cargo", "--version"], capture_output=True,
                           timeout=60).returncode == 0
except Exception:
    cargo = False
import re
leaked = sorted(k for k in os.environ if re.search(
    r"TOKEN|SECRET|PASSWORD|API_?KEY|_KEY$|^KEY$|(^|_)PAT$|^GH_|^GITHUB_|^OPENAI|^ANTHROPIC",
    k.upper()))
print(json.dumps({"readable": readable, "network": network, "dns": dns, "cargo": cargo,
                  "hermes": os.path.exists("/opt/venv/bin/hermes"), "env": leaked}))
'''

_USERNS_FIX = ("enable unprivileged user namespaces: `sudo sysctl -w kernel.unprivileged_userns_clone=1` "
               "(Debian), `sudo sysctl -w user.max_user_namespaces=15000`, or on Ubuntu 24.04+ "
               "`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (or an AppArmor "
               "profile allowing bwrap); persist it in /etc/sysctl.d/")


def _work_root(loop: dict) -> Path:
    """The same private parent the production worker uses for its turn directories."""
    root = Path(loop["state_dir"]).expanduser() / "isolated-runs"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise PermissionError(f"{root} must be a private (0700) directory")
    return root


def host_secret_paths(loop: dict, settings: dict, runtime_file: Path) -> list[str]:
    from . import seat_model
    paths = seat_model.secret_paths(loop, settings)
    paths += [str(runtime_file), str(config.home() / ".env"),
              str(config.config_dir() / f"{loop['id']}.json")]
    paths += [str(Path(str(raw)).expanduser()) for raw in (loop.get("tokens") or {}).values()]
    return list(dict.fromkeys(paths))


def check_bwrap(report: Report, loop: dict, settings: dict | None, runtime_file: Path) -> None:
    """Step 2."""
    from . import contained, trusted_turn
    step = "bubblewrap"
    if not shutil.which("bwrap"):
        report.add(step, "bwrap:installed", FAIL, "bwrap is not on PATH",
                   "install bubblewrap (`sudo dnf install bubblewrap` / `sudo apt install bubblewrap`)")
        return
    report.add(step, "bwrap:installed", PASS, shutil.which("bwrap"))
    probe = ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
             "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
             "--ro-bind-try", "/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
             "--", "/usr/bin/true"]
    try:
        result = subprocess.run(probe, capture_output=True, text=True, timeout=30,
                                env={"PATH": "/usr/bin:/bin"})
    except Exception as exc:
        report.add(step, "bwrap:userns", FAIL, f"bwrap did not run ({type(exc).__name__})",
                   _USERNS_FIX)
        return
    if result.returncode != 0:
        report.add(step, "bwrap:userns", FAIL,
                   "unprivileged namespace refused: " + (result.stderr.strip()[:160] or
                                                         f"rc={result.returncode}"), _USERNS_FIX)
        return
    report.add(step, "bwrap:userns", PASS, "unprivileged user/net/pid namespaces work")
    if settings is None:
        report.add(step, "sandbox:containment", SKIP, "needs a usable runtime config (step 1)")
        return
    try:
        parent = _work_root(loop)
    except Exception as exc:
        report.add(step, "sandbox:containment", FAIL, f"work root unusable: {exc}",
                   f"`chmod 700 {Path(loop['state_dir']).expanduser() / 'isolated-runs'}`")
        return
    with tempfile.TemporaryDirectory(prefix="selftest-", dir=parent) as tmp:
        root = Path(tmp)
        code, home, work = root / "code", root / "home", root / "work"
        try:
            trusted_turn._safe_code_snapshot(Path(settings["source"]), code)
        except Exception as exc:
            report.add(step, "sandbox:snapshot", FAIL,
                       f"source snapshot refused: {exc}",
                       "commit the hermes-agent checkout (the turn exports its HEAD commit, never "
                       "the worktree) and make sure run_agent.py is committed")
            return
        report.add(step, "sandbox:snapshot", PASS, "committed source snapshot staged")
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        query = root / "query.txt"
        query.write_text("selftest\n")
        dummy = root / "dummy-host-secret.txt"
        marker = "SELFTEST-HOST-SECRET-" + os.urandom(8).hex()
        dummy.write_text(marker)
        dummy.chmod(0o600)
        report.redact.add(marker)
        paths = [str(dummy)] + host_secret_paths(loop, settings, runtime_file)
        entry = ["/opt/venv/bin/python", "-c", _PROBE, json.dumps(paths)]
        try:
            result = contained.run(code=code, venv=Path(settings["venv"]),
                                   runtime=Path(settings["runtime"]), home=home, checkout=work,
                                   rust=Path(settings["rust"]), query=query, entry=entry,
                                   timeout=90)
        except Exception as exc:
            report.add(step, "sandbox:start", FAIL, f"sandbox did not run: {type(exc).__name__}: {exc}",
                       "check the venv/runtime/rust paths in step 1; the venv interpreter must "
                       "live under runtime")
            return
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        try:
            facts = json.loads(lines[-1])
            if not isinstance(facts, dict):
                raise ValueError
        except (IndexError, ValueError):
            report.add(step, "sandbox:start", FAIL,
                       f"probe gave no answer (rc={result.returncode}): "
                       + (result.stderr.strip().splitlines() or ["no stderr"])[-1][:200],
                       "the venv's python must run inside the sandbox: set runtime to the "
                       "Python installation that venv/bin/python resolves into")
            return
        report.add(step, "sandbox:start", PASS, "venv python runs inside bwrap with the configured mounts")
        leaked = marker in (result.stdout + result.stderr)
        readable = facts.get("readable") or []
        if readable or leaked:
            report.add(step, "sandbox:secrets", FAIL,
                       f"the sandbox could read {len(readable) or 1} host path(s): "
                       + ", ".join(readable or ["dummy secret echoed"]),
                       "do not run turns: a host secret is visible inside the namespace; make "
                       "sure none of these paths lives under source, venv, runtime or rust")
        else:
            report.add(step, "sandbox:secrets", PASS,
                       f"{len(paths)} host secret paths unreadable (dummy secret, model key "
                       "files, seat profiles' .env/auth.json/config.yaml, PATs, runtime file, "
                       ".env, loop config)")
        if facts.get("network") or facts.get("dns"):
            report.add(step, "sandbox:network", FAIL, "the sandbox reached the network",
                       "bwrap must run with --unshare-all; check for a wrapper replacing bwrap")
        else:
            report.add(step, "sandbox:network", PASS, "no network, no DNS inside the sandbox")
        if facts.get("env"):
            report.add(step, "sandbox:env", FAIL,
                       "credential-like variables inside: " + ", ".join(facts["env"]),
                       "the sandbox environment must be built from scratch (contained.run)")
        else:
            report.add(step, "sandbox:env", PASS, "no credential-like environment variables")
        if not facts.get("hermes") or not facts.get("cargo"):
            what = [name for name in ("hermes", "cargo") if not facts.get(name)]
            report.add(step, "sandbox:toolchain", FAIL, f"not usable inside: {', '.join(what)}",
                       "check venv (bin/hermes) and rust (bin/cargo) in the runtime file")
        else:
            report.add(step, "sandbox:toolchain", PASS, "hermes entry point and cargo available")


# -- step 3: inference ---------------------------------------------------------------------------

def check_seats(report: Report, loop: dict, settings: dict | None, resolver=None) -> dict:
    """Step 1 (cont.): each seat's model exactly as the worker resolves it. Returns seat → it."""
    from . import seat_model
    step = "runtime"
    resolved: dict = {}
    for seat in seat_model.seats_for(loop):
        name = f"seat:{seat}"
        if settings is None:
            report.add(step, name, SKIP, "needs a usable runtime config")
            continue
        try:
            inference = seat_model.resolve_seat(loop, seat, settings, resolver=resolver)
        except seat_model.SeatModelError as exc:
            report.add(step, name, FAIL, f"{exc}; the {seat} turn would be held",
                       f"`hermes review-loop models --seat {seat} --loop {loop['id']}` lists what "
                       "its profile's provider offers")
            continue
        report.redact.add(inference.key)
        resolved[seat] = inference
        if inference.origin == "legacy":
            report.add(step, name, WARN, f"{inference.describe()} — {inference.warning}",
                       f"make profile {inference.profile or '<name>'} resolve, or add an explicit "
                       f"seats.{seat} override")
        else:
            report.add(step, name, PASS, inference.describe())
    return resolved


def check_model(report: Report, seats: dict | None) -> None:
    """Step 3: one tiny completion per distinct seat resolution (keys never leave the host)."""
    if not seats:
        report.add("inference", "model:completion", SKIP,
                   "needs at least one resolved seat model (step 1)")
        return
    groups: dict = {}
    for seat, inference in seats.items():
        groups.setdefault(inference.identity(), []).append((seat, inference))
    for members in groups.values():
        names = "+".join(seat for seat, _ in members)
        _one_completion(report, names, members[0][1])


_PROBE = "Reply with the single word OK."


def probe_body(api_mode: str, client_identity: str = "") -> bytes:
    """The smallest request of each wire format (the proxy forces model and output cap)."""
    if api_mode == "codex_responses":
        body = {"instructions": "You are a connectivity probe.", "store": False, "stream": True,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": _PROBE}]}]}
    elif api_mode == "anthropic_messages":
        body = {"max_tokens": 16, "messages": [{"role": "user", "content": _PROBE}]}
        if client_identity == "claude_code":
            # What Hermes sends first on a subscription token (see its anthropic adapter).
            body["system"] = [{"type": "text",
                               "text": "You are Claude Code, Anthropic's official CLI for Claude."}]
    else:
        body = {"messages": [{"role": "user", "content": _PROBE}], "max_tokens": 16}
    return json.dumps(body).encode()


def probe_answer(api_mode: str, content_type: str, data: bytes) -> str | None:
    """The reply text of a probe answer in ``api_mode``'s shape, or ``None`` if it is not one."""
    try:
        if content_type.startswith("text/event-stream"):
            text, done = [], False
            for line in data.decode("utf-8", "replace").splitlines():
                if not line.startswith("data:") or line[5:].strip() in ("", "[DONE]"):
                    continue
                event = json.loads(line[5:])
                kind = event.get("type", "")
                if kind in ("response.output_text.delta",):
                    text.append(str(event.get("delta") or ""))
                elif kind == "content_block_delta":
                    text.append(str((event.get("delta") or {}).get("text") or ""))
                elif "choices" in event:
                    text.append(str(((event["choices"] or [{}])[0].get("delta") or {}).get("content") or ""))
                done = done or kind in ("response.completed", "message_stop") or "choices" in event
            return "".join(text) if done else None
        answer = json.loads(data)
        if api_mode == "anthropic_messages":
            return "".join(b.get("text", "") for b in answer["content"] if b.get("type") == "text")
        if api_mode == "codex_responses":
            return "".join(c.get("text", "") for item in answer["output"] if item.get("type") == "message"
                           for c in item.get("content", []))
        return answer["choices"][0]["message"].get("content") or ""
    except Exception:
        return None


def _one_completion(report: Report, seats: str, inference) -> None:
    from . import inference_proxy
    step, name = "inference", "model:completion"
    report.redact.add(inference.key)
    host = inference.host
    mode = getattr(inference, "api_mode", "chat_completions")
    identity = getattr(inference, "client_identity", "")
    body = probe_body(mode, identity)
    try:
        with tempfile.TemporaryDirectory(prefix="rl-st-") as sockets:
            with inference_proxy.InferenceCapability(
                    Path(sockets) / "i", inference.upstream, model=inference.proxy_model,
                    quota=1, api_mode=mode, credential=inference.credential_provider()) as capability:
                conn = inference_proxy._UnixHTTP(str(capability.socket_path))
                try:
                    conn.request("POST", capability.contract.local_path, body=body,
                                 headers={"Content-Type": "application/json"})
                    response = conn.getresponse()
                    status, data = response.status, response.read(inference_proxy.MAX_RESPONSE)
                    content_type = response.getheader("Content-Type", "")
                finally:
                    conn.close()
    except (OSError, http.client.HTTPException, ValueError) as exc:
        report.add(step, name, FAIL,
                   f"{seats}: the host capability did not answer ({type(exc).__name__})",
                   "check that the upstream host is reachable from this machine")
        return
    where = (f"profile {inference.profile}'s credential" if inference.origin == "profile"
             else "the override's key file")
    oauth = getattr(inference, "auth", "") == "oauth"
    fixes = {401: (f"{where} was rejected even after a host refresh — log in again with "
                   f"`hermes -p {inference.profile} auth`" if oauth
                   else f"{where} was rejected — replace it"),
             403: f"{where} may not use model {inference.model!r}",
             404: f"the provider does not know model {inference.model!r} at this URL — "
                  "`hermes review-loop models` lists what it offers",
             429: ("the subscription's rate limit is spent — it is shared with your own use of "
                   "this account" if oauth else
                   "the provider rate-limited or the account is out of credit"),
             502: f"the proxy could not complete an HTTPS call to {host} (DNS, TLS, network, or a "
                  "non-JSON answer) — try `curl -sS https://" + str(host) + "` from this host"}
    what = f"{seats}: {inference.describe()}"
    if status != 200:
        report.add(step, name, FAIL, f"HTTP {status} for {what}",
                   fixes.get(status, "check the upstream URL, the model name and the key"))
        return
    answer = probe_answer(mode, content_type, data)
    if answer is None:
        report.add(step, name, FAIL, f"{seats}: HTTP 200 via {host}, but not a {mode} answer",
                   f"upstream must speak {mode} at {inference.upstream}")
        return
    report.add(step, name, PASS, f"HTTP 200 — {what}, reply {answer.strip()[:40]!r}")


# -- step 4: identities --------------------------------------------------------------------------

def _roles(loop: dict) -> list[tuple[str, str]]:
    seats = loop.get("seats") or {}
    roles = [("read", str(loop.get("read_token") or "")),
             ("reviewer", str((seats.get("reviewer") or {}).get("login") or "")),
             ("fixer", str((seats.get("fixer") or {}).get("login") or ""))]
    adjudicator = config.adjudicator_login(loop)
    if adjudicator:
        roles.append(("adjudicator", adjudicator))
    return roles


def check_identities(report: Report, loop: dict) -> bool:
    """Step 4. Returns True when every identity resolved to its own principal."""
    step = "identities"
    principals: dict[str, int] = {}
    files: dict[str, Path] = {}
    ok = True
    for role, login in _roles(loop):
        name = f"github:{role}"
        if not login:
            ok = False
            report.add(step, name, FAIL, f"no login configured for the {role} identity",
                       f"hermes review-loop set --loop {loop.get('id') or '<id>'} --read-token "
                       "LOGIN --token LOGIN=/path/to/pat" if role == "read" else
                       f"name the {role} login: reviewer_login/fixer_login in the plugin settings, "
                       f"then `hermes review-loop apply --loop {loop.get('id') or '<id>'}` (a new "
                       "loop takes them from `init --reviewer/--reviewer-seat/--fixer`)")
            continue
        raw = (loop.get("tokens") or {}).get(login)
        if not raw:
            ok = False
            report.add(step, name, FAIL, f"{login}: no token file mapped",
                       f"re-run init with --token {login}=/path/to/pat (chmod 600)")
            continue
        check = doctor.check_token(login, raw)
        if check.status != doctor.VERIFIED:
            ok = False
            report.add(step, name, FAIL, f"{login}: {check.detail}", check.fix)
            continue
        files[role] = Path(str(raw)).expanduser().resolve()
        account, error = gh.fetch(loop, "/user", login=login)
        if error or not isinstance(account, dict):
            ok = False
            report.add(step, name, FAIL, f"{login}: GET /user failed ({error or 'no answer'})",
                       f"the PAT in {raw} is invalid, expired or revoked — regenerate it for {login}")
            continue
        actual, ident = account.get("login"), account.get("id")
        if not isinstance(actual, str) or type(ident) is not int:
            ok = False
            report.add(step, name, FAIL, f"{login}: /user answered without a login and id",
                       "retry; if it persists, the token is not a user PAT")
            continue
        if actual.casefold() != login.casefold():
            ok = False
            report.add(step, name, FAIL, f"the token mapped to {login} belongs to {actual}",
                       f"put {login}'s own PAT in {raw}")
            continue
        principals[role] = ident
        report.add(step, name, PASS, f"{actual} (id {ident})")
    if len(set(files.values())) != len(files):
        ok = False
        report.add(step, "github:distinct-files", FAIL, "two identities share one token file",
                   "give every identity its own PAT file")
    by_id: dict[int, list[str]] = {}
    for role, ident in principals.items():
        by_id.setdefault(ident, []).append(role)
    shared = [roles for roles in by_id.values() if len(roles) > 1]
    if shared:
        ok = False
        report.add(step, "github:distinct", FAIL,
                   "same GitHub principal for: " + "; ".join(" + ".join(r) for r in shared),
                   "each of read/reviewer/fixer (and adjudicator) must be a separate GitHub account")
    elif len(principals) == len(_roles(loop)):
        report.add(step, "github:distinct", PASS, f"{len(principals)} distinct principals")
    reader = str(loop.get("read_token") or "")
    if "read" in principals:
        repo, error = gh.fetch(loop, f"/repos/{loop['repo']}", login=reader)
        if error or not isinstance(repo, dict) or str(repo.get("full_name", "")).casefold() != loop["repo"].casefold():
            ok = False
            report.add(step, "github:repo", FAIL, f"{loop['repo']} not readable as {reader} ({error or 'unexpected answer'})",
                       f"give {reader}'s PAT read access to {loop['repo']} (Contents + Pull requests: read)")
        else:
            report.add(step, "github:repo", PASS, f"{loop['repo']} readable as {reader}")
    return ok


# -- step 5: broker authorization dry run ------------------------------------------------------

_DENIAL_FIX = {
    "PR identity, state or draft status changed": "the PR must be open and not a draft",
    "PR base branch mismatch": "the PR must target the loop's base branch",
    "PR base repository mismatch": "the PR must be in the loop's repository",
    "fork head not permitted for credentialed writes": "use a PR whose head branch is in the repository itself, not a fork",
    "seat token principal cannot be verified": "see the identities step",
    "read, reviewer and fixer tokens resolve to same principal": "see the identities step",
    "cannot verify live PR": "check the PR number and that the read token can see it",
}


def check_authorization(report: Report, loop: dict, number: int | None) -> dict | None:
    """Step 5: ``broker.authorize`` for a reviewer write — it only reads. Returns the live PR."""
    from . import broker, review_receipt
    step = "authorization"
    if number is None:
        report.add(step, "broker:reviewer", SKIP, "pass --pr N to dry-run the reviewer write checks")
        return None
    pr, error = gh.fetch(loop, f"/repos/{loop['repo']}/pulls/{number}", login=loop.get("read_token"))
    if error or not isinstance(pr, dict):
        report.add(step, "broker:pr", FAIL, f"PR #{number} unreadable ({error or 'no answer'})",
                   "check the PR number and the read token's access")
        return None
    head = (pr.get("head") or {}).get("sha") or ""
    ref = (pr.get("head") or {}).get("ref") or ""
    report.add(step, "broker:pr", PASS, f"#{number} {pr.get('state')}"
               f"{' draft' if pr.get('draft') else ''}, head {head[:12]} on {ref}")
    try:
        login = broker.authorize(loop, repo=loop["repo"], number=number, head=head,
                                 role="reviewer", branch=ref, operation="review")
    except broker.BrokerDenied as exc:
        report.add(step, "broker:reviewer", FAIL, f"a reviewer write would be denied: {exc}",
                   _DENIAL_FIX.get(str(exc), "fix the reason above; the broker checks it before every write"))
        return None
    report.add(step, "broker:reviewer", PASS, f"a review at {head[:12]} would post as {login} (nothing posted)")
    try:
        review_receipt.generation_for(pr, loop, number, head)
    except review_receipt.ReceiptDenied as exc:
        report.add(step, "broker:receipt", FAIL, f"host receipt generation unresolvable: {exc}",
                   f"the PR must target {loop.get('base')} directly (stacked bases need #23)")
        return None
    report.add(step, "broker:receipt", PASS, "host review receipt generation resolves")
    return pr


# -- step 6: ledger, cron, observer --------------------------------------------------------------

def check_ledger(report: Report, loop: dict, runtime_file: Path, settings: dict | None) -> None:
    from . import observer
    from .run_supervisor import Supervisor
    step = "supervisor"
    db = ledger_path()
    try:
        supervisor = Supervisor(db)
        rows = supervisor.status()
    except Exception as exc:
        report.add(step, "ledger", FAIL, f"{db}: {type(exc).__name__}: {exc}",
                   f"make {db.parent} writable by you; a corrupt ledger must be moved aside by hand")
        return
    report.add(step, "ledger", PASS, f"{db} (schema migrated, status readable)")
    if rows:
        report.add(step, "ledger:attention", WARN, f"{len(rows)} failed/uncertain run(s) in the ledger",
                   f"inspect `python -m review_loop.run_supervisor status {db}`; an uncertain seat "
                   "blocks new turns until reconciled")
    if settings is not None:
        try:
            Supervisor(db, production_config=runtime_file, hermes_home=config.home())
        except Exception as exc:
            report.add(step, "ledger:production", FAIL, f"route enqueue would refuse the runtime: {exc}",
                       f"`chmod 600 {runtime_file}`")
        else:
            report.add(step, "ledger:production", PASS, "the route's enqueue accepts this runtime file")
    for check in (doctor.check_state_dir(loop), doctor.check_shim(loop), doctor.check_cron_job(loop),
                  doctor.check_gateway(loop, offline=False)):
        _from_doctor(report, step, check)
    reason = observer.unusable(loop)
    if reason == "no observer configured":
        report.add(step, "observer", SKIP, "no observer configured (alerts go to the cron outbox only)")
    elif reason:
        report.add(step, "observer", FAIL, reason,
                   "re-run `hermes review-loop init` with the --observer-* flags, then "
                   "`hermes review-loop doctor`")
    else:
        report.add(step, "observer", PASS, "observer route registered with a secret and URL")


# -- step 7: live no-write turn ------------------------------------------------------------------

def run_live_turn(report: Report, loop: dict, settings: dict | None, pr: dict | None,
                  timeout: int, reviewer=None) -> None:
    from . import broker_ipc, gh as gh_mod, trusted_turn
    from .run_supervisor import effective_reviews, isolated_prompt
    step = "live-turn"
    if settings is None or pr is None or reviewer is None:
        report.add(step, "turn:reviewer", SKIP,
                   "needs a usable runtime and reviewer model (step 1) and an authorized PR (step 5)")
        return
    number, head, ref = pr["number"], pr["head"]["sha"], pr["head"]["ref"]
    row = {"seat": "reviewer", "repo": loop["repo"], "pr": number, "head": head}
    try:
        reviews = effective_reviews(loop, row, gh_mod.reviews(loop, number), str(ledger_path()))
        prompt = isolated_prompt(loop, row, reviews)
        report.redact.add(reviewer.key)
    except Exception as exc:
        report.add(step, "turn:prompt", FAIL, f"could not build the reviewer prompt: {exc}",
                   "check the reviews read in step 5")
        return
    scope = broker_ipc.RunScope(loop["repo"], number, head, "reviewer", ref)
    observed: dict = {}
    report.text(f"  … running one isolated reviewer turn on #{number} as "
                f"{reviewer.describe()} (up to {timeout}s; the broker is in no-write mode)")
    try:
        rc = trusted_turn.run_turn(loop, scope, source=Path(settings["source"]),
                                   venv=Path(settings["venv"]), runtime=Path(settings["runtime"]),
                                   rust=Path(settings["rust"]), upstream=reviewer.upstream,
                                   key=reviewer.key, model=reviewer.model, prompt=prompt,
                                   api_mode=reviewer.api_mode,
                                   credential=reviewer.credential_provider(),
                                   proxy_model=reviewer.proxy_model,
                                   client_identity=reviewer.client_identity,
                                   timeout=timeout, work_root=_work_root(loop),
                                   no_write=True, observed=observed)
        error = ""
    except Exception as exc:
        rc, error = observed.get("returncode"), f"{type(exc).__name__}: {exc}"
    for entry in observed.get("submissions") or []:
        state = ("authorized" if entry.get("authorized")
                 else f"would be DENIED: {entry.get('denial')}")
        report.text(f"  ── the agent would submit {entry['verdict']} ({state}); NOT posted ──")
        for line in (entry.get("body") or "").splitlines()[:200]:
            report.text("  │ " + line)
        report.text("  ──")
    submissions = observed.get("submissions") or []
    if error or rc != 0 or not submissions:
        tail = (observed.get("stderr") or observed.get("stdout") or "").strip().splitlines()[-5:]
        report.add(step, "turn:reviewer", FAIL,
                   f"turn did not finish with a verdict ({error or f'rc={rc}'})"
                   + ("; last output: " + " | ".join(t[:120] for t in tail) if tail else ""),
                   f"raise --timeout (now {timeout}s) if it timed out; otherwise read the output "
                   "above — the production worker runs the same turn")
        return
    if not submissions[-1].get("authorized"):
        report.add(step, "turn:reviewer", FAIL, "the agent's verdict would have been denied",
                   "see the denial above and step 5")
        return
    report.add(step, "turn:reviewer", PASS,
               f"isolated turn submitted {submissions[-1]['verdict']}; nothing was posted to GitHub")


# -- driver ------------------------------------------------------------------------------------

def run(loop: dict, *, pr: int | None = None, model: bool = True, live_turn: bool = False,
        timeout: int = DEFAULT_TIMEOUT, out=None, runtime_file: Path | None = None,
        resolver=None) -> int:
    """Run every step; return 1 when any check failed, else 0."""
    runtime_file = Path(runtime_file or runtime_path())
    redact = Redactor()
    for raw in (loop.get("tokens") or {}).values():
        redact.add_file(raw)
    report = Report(out, redact)
    report.text(f"[{loop['id']}] {loop['repo']} — isolated-path selftest "
                "(GitHub: reads only, nothing is posted; no token or key is printed)")
    if os.environ.get("REVIEW_LOOP_GH_STUB"):
        report.add("preconditions", "github:stub", FAIL, "REVIEW_LOOP_GH_STUB is set: GitHub is faked",
                   "unset REVIEW_LOOP_GH_STUB — the selftest must talk to the real API")
    with github_read_only(), worker_tempdir():
        report.step("1. Runtime config and seat models")
        settings = check_runtime(report, runtime_file)
        if settings is not None:
            from . import seat_model
            for block in [seat_model.legacy_override(settings),
                          *(settings.get("seats") or {}).values()]:
                if block:
                    redact.add_file(Path(block["key_file"]).expanduser())
        seats = check_seats(report, loop, settings, resolver)
        report.step("2. Bubblewrap containment")
        check_bwrap(report, loop, settings, runtime_file)
        report.step("3. Inference proxy (once per distinct seat model)")
        if model:
            check_model(report, seats if settings is not None else None)
        else:
            report.add("inference", "model:completion", SKIP, "--no-model")
        report.step("4. GitHub identities")
        check_identities(report, loop)
        report.step("5. Broker authorization (dry run)")
        live_pr = check_authorization(report, loop, pr)
        report.step("6. Supervisor ledger, watchdog, observer")
        check_ledger(report, loop, runtime_file, settings)
        report.step("7. Live isolated reviewer turn (no-write)")
        if live_turn:
            run_live_turn(report, loop, settings, live_pr, timeout, seats.get("reviewer"))
        else:
            report.add("live-turn", "turn:reviewer", SKIP, "pass --live-turn --pr N to run one")
    counts = report.counts()
    report.text(f"\n{loop['id']}: {counts[PASS]} passed, {counts[FAIL]} failed, "
                f"{counts[WARN]} warnings, {counts[SKIP]} skipped")
    if counts[FAIL]:
        failed = [r[1] for r in report.results if r[2] == FAIL]
        report.text(f"  ❌ fix these before enabling turns: {', '.join(failed)}")
    else:
        report.text("  ✅ the isolated path is ready as far as these checks can see.")
    return 1 if counts[FAIL] else 0
