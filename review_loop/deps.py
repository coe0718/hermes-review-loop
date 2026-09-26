"""Host-side dependency prefetch for the offline sandbox (issue #51).

The sandbox has no network, so a seat cannot download a single crate: without help every
``cargo build`` on a repo with dependencies fails, and a reviewer told to verify would request
changes on every round. Before launch, the trusted host fetches the dependencies a staged PR
head pins into a per-repository host cache; ``contained`` then mounts that cache **read-only**
and the seat builds offline. When the prefetch is impossible or fails, the turn still runs and the
seat is told plainly that dependencies are unavailable, so it judges by reading instead of treating
"could not build" as a finding.

Trust decision (see docs/issue-16-boundary.md, "Dependency prefetch"): the host never runs cargo
against the PR's own manifests. Everything the PR controls — ``Cargo.toml``, ``build.rs``,
``.cargo/config.toml``, ``rust-toolchain.toml``, ``[patch]`` and git dependencies — stays out of
the host process. The host only *reads* ``Cargo.lock`` as data (``tomllib``), accepts nothing but
crates.io packages named and versioned by strict patterns, and writes its own synthetic manifest
pinning exactly those ``name = version`` pairs. ``cargo fetch`` on that manifest downloads and
unpacks crates from crates.io and compiles or executes nothing from them. It runs from a private
empty directory outside any user config, with an environment built from scratch (no token, no
proxy or credential variables, a throwaway ``HOME``), the configured toolchain's own ``cargo`` and
``rustc`` (never a rustup proxy that would honour a toolchain override), a timeout and a bounded
output capture.

Pluggable: ``ECOSYSTEMS`` maps a name to a prefetcher; ``contained.DEPENDENCY_MOUNTS`` fixes where
(and only where) that ecosystem's cache is mounted and which environment makes it offline. Only
Rust is implemented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time

READY, UNAVAILABLE = "ready", "unavailable"

CRATES_IO = frozenset({"registry+https://github.com/rust-lang/crates.io-index",
                       "sparse+https://index.crates.io/"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
                      r"(-[0-9A-Za-z.-]{1,64})?(\+[0-9A-Za-z.-]{1,64})?\Z")
MAX_LOCKFILE = 4 * 1024 * 1024
MAX_PACKAGES = 4000
FETCH_TIMEOUT = 300
MAX_OUTPUT = 64 * 1024


@dataclass(frozen=True)
class Prefetch:
    """One ecosystem's outcome: what the sandbox mounts and what the seat is told."""
    ecosystem: str
    status: str                       # READY or UNAVAILABLE
    reason: str                       # short, host-written; never raw tool output
    cache: Path | None = None         # host directory mounted read-only when READY
    detail: str = field(default="", compare=False)   # bounded tool output tail, for operators

    @property
    def ready(self) -> bool:
        return self.status == READY


def cache_root(loop: dict) -> Path:
    """The per-repository host cache: private, under the loop's own state directory."""
    root = Path(loop["state_dir"]).expanduser() / "deps"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise PermissionError(f"{root} must be a private (0700) directory")
    return root


def _read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > limit:
        raise ValueError("too large")
    return data


def locked_crates(lockfile: Path) -> list[tuple[str, str]]:
    """The crates.io ``(name, version)`` pairs a ``Cargo.lock`` pins; refuse anything else.

    Path/workspace packages (no ``source``) are skipped: they are the repository itself. A git
    source or any other registry is refused, because fetching it means the host contacting a
    URL the PR chose.
    """
    import tomllib
    try:
        document = tomllib.loads(_read_regular(lockfile, MAX_LOCKFILE).decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("Cargo.lock is unreadable or not valid TOML") from exc
    packages = document.get("package", [])
    if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
        raise ValueError(f"Cargo.lock lists more than {MAX_PACKAGES} packages or is malformed")
    crates: set[tuple[str, str]] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("Cargo.lock has a malformed package entry")
        source = package.get("source")
        if source is None:
            continue
        if source not in CRATES_IO:
            raise ValueError("Cargo.lock pins a git or non-crates.io source; the host fetches "
                             "only crates.io packages")
        name, version = package.get("name"), package.get("version")
        if not (isinstance(name, str) and _NAME.match(name) and isinstance(version, str)
                and _VERSION.match(version)):
            raise ValueError("Cargo.lock has a package name or version outside the accepted form")
        crates.add((name, version))
    return sorted(crates)


def synthetic_manifest(crates: list[tuple[str, str]]) -> str:
    """A throwaway package depending on exactly each locked crate at its exact version."""
    lines = ['[package]', 'name = "review-loop-prefetch"', 'version = "0.0.0"',
             'edition = "2021"', 'publish = false', '', '[dependencies]']
    for index, (name, version) in enumerate(crates):
        lines.append(f'd{index} = {{ package = "{name}", version = "={version}", '
                     'default-features = false }')
    return "\n".join(lines) + "\n"


def bounded_run(argv: list[str], *, env: dict, cwd: Path, timeout: float,
                limit: int = MAX_OUTPUT) -> tuple[int | None, str]:
    """Run with a scratch environment; keep the last ``limit`` bytes; kill the group on timeout.

    Returns ``(returncode, tail)``; ``returncode`` is ``None`` when the deadline passed.
    """
    process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, output[-limit:].decode(errors="replace")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    del output[:-limit]
        try:
            return process.wait(timeout=max(deadline - time.monotonic(), 0.1)), \
                output[-limit:].decode(errors="replace")
        except subprocess.TimeoutExpired:
            return None, output[-limit:].decode(errors="replace")
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def _cached(cache: Path, crates: list[tuple[str, str]]) -> list[str]:
    """Locked crates with no downloaded ``.crate`` in the crates.io cache."""
    have: set[str] = set()
    registry = cache / "registry" / "cache"
    if registry.is_dir():
        for index in registry.iterdir():
            if index.name.startswith("index.crates.io-") and index.is_dir():
                have.update(entry.name for entry in index.iterdir())
    return [f"{name} {version}" for name, version in crates
            if f"{name}-{version}.crate" not in have]


def prefetch_rust(checkout: Path, cache_parent: Path | None, rust: Path,
                  timeout: float = FETCH_TIMEOUT) -> Prefetch | None:
    """Fetch a staged Rust head's crates.io dependencies into ``cache_parent/cargo``.

    ``None`` when the head is not a Rust project. Never raises for a PR-controlled reason: an
    unusable lockfile or a failed fetch is an ``UNAVAILABLE`` outcome with a plain reason.
    """
    checkout = Path(checkout)
    lockfile, manifest = checkout / "Cargo.lock", checkout / "Cargo.toml"
    if not lockfile.exists() and not manifest.exists():
        return None
    if not lockfile.is_file() or lockfile.is_symlink():
        return Prefetch("rust", UNAVAILABLE, "the head has no Cargo.lock at its root, so there is "
                        "nothing pinned the host could fetch")
    try:
        crates = locked_crates(lockfile)
    except ValueError as exc:
        return Prefetch("rust", UNAVAILABLE, str(exc))
    if cache_parent is None:
        return Prefetch("rust", UNAVAILABLE, "the host dependency cache is unusable (it must be "
                        "a private 0700 directory under the loop's state_dir)")
    cache = Path(cache_parent) / "cargo"
    cache.mkdir(mode=0o700, exist_ok=True)
    if cache.is_symlink() or not cache.is_dir():
        return Prefetch("rust", UNAVAILABLE, "the host crate cache is not a private directory")
    (cache / "registry").mkdir(mode=0o700, exist_ok=True)
    if not crates:
        return Prefetch("rust", READY, "Cargo.lock pins no crates.io dependencies", cache)
    cargo, rustc = Path(rust) / "bin" / "cargo", Path(rust) / "bin" / "rustc"
    if not (cargo.is_file() and rustc.is_file()):
        return Prefetch("rust", UNAVAILABLE, "the configured Rust toolchain has no cargo/rustc")
    with tempfile.TemporaryDirectory(prefix="rl-prefetch-") as tmp:
        work, home = Path(tmp) / "pkg", Path(tmp) / "home"
        (work / "src").mkdir(parents=True)
        home.mkdir()
        (work / "src" / "lib.rs").write_text("")
        (work / "Cargo.toml").write_text(synthetic_manifest(crates))
        # Seed the resolver with the PR's lockfile *as data*: it keeps each locked version even if
        # it was yanked since. Cargo re-derives the root; this file never reaches the sandbox.
        (work / "Cargo.lock").write_bytes(_read_regular(lockfile, MAX_LOCKFILE))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CARGO_HOME": str(cache),
               "RUSTC": str(rustc), "CARGO": str(cargo), "CARGO_TERM_COLOR": "never",
               "CARGO_TERM_PROGRESS_WHEN": "never", "CARGO_NET_RETRY": "2",
               "CARGO_HTTP_TIMEOUT": "60", "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": "sparse",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8"}
        try:
            rc, tail = bounded_run([str(cargo), "fetch"], env=env, cwd=work, timeout=timeout)
        except OSError as exc:
            return Prefetch("rust", UNAVAILABLE, f"cargo could not start ({type(exc).__name__})")
    if rc is None:
        return Prefetch("rust", UNAVAILABLE, f"the host fetch timed out after {int(timeout)}s",
                        detail=tail)
    missing = _cached(cache, crates)
    if rc != 0 or missing:
        what = f"{len(missing)} of {len(crates)} crates missing" if missing else f"rc={rc}"
        return Prefetch("rust", UNAVAILABLE, f"the host fetch from crates.io failed ({what})",
                        detail=tail)
    return Prefetch("rust", READY, f"{len(crates)} crates.io crates from Cargo.lock", cache)


ECOSYSTEMS = {"rust": prefetch_rust}


def prepare(checkout: Path, cache_parent: Path | None, rust: Path,
            timeout: float = FETCH_TIMEOUT) -> list[Prefetch]:
    """Run every applicable prefetcher; a crash in one is an UNAVAILABLE outcome, not a raise.

    ``cache_parent`` is ``None`` when the host cache is unusable: nothing is fetched, and a
    project that needed it is reported UNAVAILABLE rather than failing the turn.
    """
    results = []
    for name, prefetcher in ECOSYSTEMS.items():
        try:
            outcome = prefetcher(checkout, cache_parent, rust, timeout)
        except Exception as exc:  # the turn still runs; the seat is told
            outcome = Prefetch(name, UNAVAILABLE, f"host prefetch error ({type(exc).__name__})")
        if outcome is not None:
            results.append(outcome)
    return results


_LABEL = {"rust": ("Rust", "crates", "`cargo build`, `cargo test` and `cargo metadata`")}


def seat_note(results: list[Prefetch], role: str) -> str:
    """What the seat is told about building, appended to its query by the host."""
    if not results:
        return ""
    lines = ["Build environment (host-checked before this turn):"]
    for result in results:
        label, unit, commands = _LABEL.get(result.ecosystem, (result.ecosystem, "dependencies",
                                                             "builds"))
        if result.ready:
            lines.append(f"- {label}: dependencies are available offline ({result.reason}); the "
                         f"{unit} cache is read-only and the network is off, so {commands} work "
                         "as long as they need nothing beyond Cargo.lock.")
            continue
        lines.append(f"- {label}: dependencies are NOT available in this sandbox — {result.reason}. "
                     f"{commands} will fail on missing {unit}; that is the sandbox, not the PR.")
        if role == "fixer":
            lines.append("  You cannot build or test your fix here: check it by reading, and say "
                         "in your published answers that it is unbuilt.")
        elif role == "adjudicator":
            lines.append("  Neither seat could build either. Rule on what can be shown from the "
                         "code; a finding or answer that rests only on the missing build is not "
                         "evidence either way.")
        else:
            lines.append("  Judge by reading the code and the diff. That the build could not run "
                         "is not by itself a finding and not by itself a reason to request "
                         "changes: say plainly in your review what you could not run, and base "
                         "the verdict on what you can show from the code.")
    return "\n".join(lines)
