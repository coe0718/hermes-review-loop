"""Fail-closed whole-process bubblewrap launcher with per-run IPC capabilities and size bounds.

``MAX_CAPTURE`` bounds what the parent retains and the caller bounds wall-clock time; neither
bounds memory or disk, because bubblewrap's tmpfs default is half of RAM and a bind mount has no
size at all. A seat that spent its whole turn writing could therefore fill the same host
filesystem that holds the loop's ledger and state (issue #89). Every write surface the seat can
grow is now a named size instead of an inherited default — and the two that have to stay the
host's are named too, below:

* ``/tmp`` is a tmpfs of ``SCRATCH_SIZE``: TMPDIR, ``CARGO_HOME``/``RUSTUP_HOME`` and an
  unwritable checkout's build target.
* A writable ``/work`` is a tmpfs of ``CHECKOUT_SIZE``, which this launcher fills in-namespace
  from a read-only bind of the staged export before the seat starts. The seat needs to write
  there — the reviewer and the fixer build, test and edit in ``/work`` — and no host process
  reads it afterwards: a fixer's push carries file contents through the broker, and
  ``safe_push`` never opens the checkout. The host's export is mounted read-only, so a seat's
  edits cannot reach even the staged copy. An unwritable ``/work`` stays a plain read-only bind
  of that export: an adjudicator's ruling is judgement, not a change.
* The namespace root bubblewrap creates implicitly, and ``--dev``, are remounted read-only: both
  are tmpfs mounts of nobody's chosen size.

Two things stay the host's, and they are the honest limit of an in-namespace fix:

* ``/home/agent`` is a read-write bind of the per-turn host directory, because the host reads what
  the seat wrote there (Hermes state, fixture request logs). A bind mount has no size option.
* ``size=`` bounds tmpfs *data*, not inode metadata (an 8 MiB tmpfs accepts hundreds of thousands
  of empty files), and neither bound covers the host filesystem behind the ``/home/agent`` bind.
  The outer half of this bound belongs on the worker's unit — ``MemoryMax=`` for the RAM-backed
  tmpfs, ``IOWeight=`` or a disk quota for the host bind — not in a user namespace.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

MAX_CAPTURE = 256 * 1024

# Named size bounds for the writable mounts above. tmpfs is charged page by page, so these are
# caps and not reservations: a seat that writes nothing costs nothing. ``SCRATCH_SIZE`` holds
# TMPDIR, the cargo and rustup homes and a scratch build target; ``CHECKOUT_SIZE`` holds the
# exported head (at most 100 MiB — ``trusted_fetch._MAX_BYTES``) plus the build output of a turn
# whose wall-clock bound is minutes. Both are orders of magnitude below the host filesystem free
# space they used to be able to consume.
#
# The defaults are sized against what this loop actually builds, not against what a test writes:
# a Rust debug target for the repos it watches is 2.2 GiB (patchhive/attest) and two other real
# workspaces' are 3.3 GiB and 3.9 GiB, all of which land in ``/work`` because ``CARGO_TARGET_DIR``
# points there. A cap below a real target does not fail loudly — the seat reports "could not
# verify" and every review requests changes, which is the failure the crate cache exists to fix.
# Overrides this resolver refused, so `doctor` can report them: a bound nobody can parse must not
# vanish into an unattended turn's stderr.
IGNORED_SIZE_OVERRIDES: list[tuple[str, str, str]] = []


def _size_from_env(name: str, gib: int) -> int:
    """A mount bound, overridable with ``REVIEW_LOOP_<NAME>_GIB``.

    These are environment settings rather than config keys because the number that matters is a
    property of the *host* — its RAM, and how large a build in the repos it watches grows — not of
    one loop. A value nobody can parse is reported and ignored rather than fatal: a bound that
    cannot be read must not take an unattended loop down.
    """
    raw = os.environ.get(f"REVIEW_LOOP_{name}_GIB", "").strip()
    try:
        value = int(raw) if raw else gib
        if not 1 <= value <= 1024:
            raise ValueError("outside 1..1024")
    except ValueError as exc:
        if raw:
            print(f"contained: ignoring REVIEW_LOOP_{name}_GIB={raw!r} ({exc}); using {gib} GiB",
                  file=sys.stderr)
            IGNORED_SIZE_OVERRIDES.append((name, raw, str(exc)))
        value = gib
    return value * 1024 ** 3


SCRATCH_SIZE = _size_from_env("SCRATCH_SIZE", 2)
CHECKOUT_SIZE = _size_from_env("CHECKOUT_SIZE", 8)

# Where a writable checkout's read-only source export is mounted, and the loader that stages it
# into the sized tmpfs at /work before the seat runs. bubblewrap has no "copy this tree" mount, so
# the copy happens inside the namespace, where it is also charged to the budget it fills. Its
# failure is the shell's: the entry never runs against a half-staged tree.
EXPORT_DIR = "/opt/export"
LOAD_CHECKOUT = f'cp -a {EXPORT_DIR}/. /work/ && exec "$@"'


def _sized_tmpfs(destination: str, size: int) -> list[str]:
    """``--size`` applies to the *next* ``--tmpfs``, so the two options must stay adjacent.

    The ``--tmpfs DEST,size=N`` spelling is not accepted by every bubblewrap build, and one that
    does not know it takes the whole string as the mount point — silently creating a directory
    named ``tmp,size=N`` and no ``/tmp`` at all. The adjacent form fails loudly instead.
    """
    return ["--size", str(size), "--tmpfs", destination]

class OutputLimitExceeded(RuntimeError):
    """The sandbox produced more output than the control plane will retain."""


def command(*, code: Path, venv: Path, runtime: Path, home: Path,
            checkout: Path, rust: Path, query: Path, entry: list[str],
            network: bool = False, inference_socket_dir: Path | None = None,
            broker_socket_dir: Path | None = None,
            client_code: Path | None = None,
            checkout_writable: bool = True,
            review_dir: Path | None = None) -> list[str]:
    """Build an allowlisted mount namespace for the *entire* process tree.

    code must be a separately staged, audited, credentialless source snapshot;
    none of these directories may be the user's actual home or source checkout.
    There is deliberately no caller-selectable arbitrary host bind.

    A writable ``checkout`` is never bound into the namespace directly: it is staged into a sized
    tmpfs that the seat cannot grow past, and the old, unbounded ``--bind`` of the host filesystem
    is gone. See the module docstring for what that costs and what it does not cover.
    """
    if network:
        raise ValueError("host networking is forbidden")
    for p in (code, venv, runtime, home, checkout, rust, query):
        if not Path(p).exists():
            raise FileNotFoundError(p)
    if inference_socket_dir is not None:
        socket_dir = Path(inference_socket_dir)
        if not (socket_dir.is_dir() and (socket_dir / 'model.sock').is_socket()):
            raise FileNotFoundError('live inference capability required')
        if list(socket_dir.iterdir()) != [socket_dir / 'model.sock']:
            raise ValueError('inference mount must contain only the capability socket')
    if broker_socket_dir is not None:
        broker_dir = Path(broker_socket_dir)
        if not broker_dir.is_dir() or not (broker_dir / 'broker.sock').is_socket():
            raise FileNotFoundError('live broker capability required')
        if set(broker_dir.iterdir()) != {broker_dir / 'broker.sock'}:
            raise ValueError('broker mount must contain only the capability socket')
    if review_dir is not None:
        review = Path(review_dir)
        diff = review / 'pr.diff'
        if (review.is_symlink() or not review.is_dir() or diff.is_symlink()
                or not diff.is_file() or list(review.iterdir()) != [diff]):
            raise ValueError('review mount must contain only the staged pr.diff')
    if client_code is not None and not (Path(client_code) / 'review_loop/broker_client.py').is_file():
        raise FileNotFoundError('staged broker client required')
    # The venv's interpreter symlinks point at the runtime's absolute host path, so the
    # runtime is mounted at that same path; its ancestors are empty directories.
    runtime_parents = [arg for parent in reversed(Path(runtime).parents[:-1])
                       for arg in ("--dir", str(parent))]
    if checkout_writable:
        # The seat's tree is a tmpfs it cannot outgrow, filled by LOAD_CHECKOUT from a read-only
        # bind of the staged export: edits and build output are charged to CHECKOUT_SIZE and reach
        # neither the host filesystem nor the export the host staged.
        checkout_mount = [*_sized_tmpfs("/work", CHECKOUT_SIZE),
                          "--ro-bind", str(checkout), EXPORT_DIR]
    else:
        checkout_mount = ["--ro-bind", str(checkout), "/work"]
    mounts = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
            # Debian/Ubuntu resolve cc, c++ and friends through /etc/alternatives;
            # without it Rust cannot link. It holds only symlinks.
            "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
            "--proc", "/proc", "--dev", "/dev", *_sized_tmpfs("/tmp", SCRATCH_SIZE),
            "--dir", "/opt", *runtime_parents,
            "--ro-bind", str(runtime), str(runtime),
            "--ro-bind", str(venv), "/opt/venv",
            "--ro-bind", str(code), "/opt/code",
            "--ro-bind", str(rust), "/opt/rust",
            "--bind", str(home), "/home/agent",
            *checkout_mount,
            "--ro-bind", str(query), "/opt/query"]
    if inference_socket_dir is not None:
        mounts += ["--dir", "/opt/inference", "--ro-bind", str(Path(inference_socket_dir)), "/opt/inference"]
    if broker_socket_dir is not None:
        mounts += ["--dir", "/run", "--dir", "/run/review-loop",
                   "--ro-bind", str(Path(broker_socket_dir)), "/run/review-loop/broker"]
    if client_code is not None:
        mounts += ["--ro-bind", str(Path(client_code)), "/opt/client"]
    if review_dir is not None:
        # Read-only and outside /work: the diff is something to read, never something to push.
        # Mounted before the remount-ro below, which seals the root and /dev.
        mounts += ["--ro-bind", str(Path(review_dir)), "/opt/review"]
    # Last, after every mount point above exists: the root and /dev are tmpfs mounts of no chosen
    # size, and the seat has no business writing to either (its scratch lives in /tmp, /work and
    # /home/agent). Nothing after this creates a directory.
    mounts += ["--remount-ro", "/", "--remount-ro", "/dev"]
    launch = ["/bin/sh", "-c", LOAD_CHECKOUT, "sh", *entry] if checkout_writable else list(entry)
    return mounts + [
            "--setenv", "HOME", "/home/agent", "--setenv", "HERMES_HOME", "/home/agent",
            "--setenv", "PYTHONPATH", "/opt/code:/opt/client" if client_code else "/opt/code", "--setenv", "CARGO_HOME", "/tmp/cargo",
            "--setenv", "RUSTUP_HOME", "/tmp/rustup", "--setenv", "CARGO_TARGET_DIR", "/work/target" if checkout_writable else "/tmp/target",
            "--setenv", "TMPDIR", "/tmp", "--setenv", "PATH", "/opt/venv/bin:/opt/rust/bin:/usr/bin:/bin",
            "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null", "--setenv", "GIT_CONFIG_SYSTEM", "/dev/null",
            "--setenv", "GIT_TERMINAL_PROMPT", "0", "--chdir", "/work", "--", *launch]


def run(*, timeout: int = 180, **kwargs) -> subprocess.CompletedProcess:
    # Parent environment is discarded, not merely filtered by a fragile denylist.
    home = str(Path(kwargs["home"]).resolve(strict=True))
    argv = command(**kwargs)
    process = subprocess.Popen(argv,
                               env={"PATH": "/usr/sbin:/usr/bin:/bin", "HOME": home,
                                    "HERMES_HOME": home},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    data = output[key.data]
                    if len(data) + len(chunk) > MAX_CAPTURE:
                        raise OutputLimitExceeded(f"sandbox {key.data} exceeded {MAX_CAPTURE} bytes")
                    data.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        process.wait(timeout=remaining)
        return subprocess.CompletedProcess(argv, process.returncode,
                                           output["stdout"].decode(errors="replace"),
                                           output["stderr"].decode(errors="replace"))
    except BaseException:
        # The parent may have exited while descendants still hold the pipes.
        # Kill the isolated process group even when the direct child is gone.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
