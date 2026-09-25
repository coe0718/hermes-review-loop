"""Fail-closed whole-process bubblewrap launcher with per-run IPC capabilities."""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

MAX_CAPTURE = 256 * 1024

class OutputLimitExceeded(RuntimeError):
    """The sandbox produced more output than the control plane will retain."""


def command(*, code: Path, venv: Path, runtime: Path, home: Path,
            checkout: Path, rust: Path, query: Path, entry: list[str],
            network: bool = False, inference_socket_dir: Path | None = None,
            broker_socket_dir: Path | None = None,
            client_code: Path | None = None,
            checkout_writable: bool = True) -> list[str]:
    """Build an allowlisted mount namespace for the *entire* process tree.

    code must be a separately staged, audited, credentialless source snapshot;
    none of these directories may be the user's actual home or source checkout.
    There is deliberately no caller-selectable arbitrary host bind.
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
    if client_code is not None and not (Path(client_code) / 'review_loop/broker_client.py').is_file():
        raise FileNotFoundError('staged broker client required')
    # The venv's interpreter symlinks point at the runtime's absolute host path, so the
    # runtime is mounted at that same path; its ancestors are empty directories.
    runtime_parents = [arg for parent in reversed(Path(runtime).parents[:-1])
                       for arg in ("--dir", str(parent))]
    mounts = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
            # Debian/Ubuntu resolve cc, c++ and friends through /etc/alternatives;
            # without it Rust cannot link. It holds only symlinks.
            "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--dir", "/opt", *runtime_parents,
            "--ro-bind", str(runtime), str(runtime),
            "--ro-bind", str(venv), "/opt/venv",
            "--ro-bind", str(code), "/opt/code",
            "--ro-bind", str(rust), "/opt/rust",
            "--bind", str(home), "/home/agent",
            "--bind" if checkout_writable else "--ro-bind", str(checkout), "/work",
            "--ro-bind", str(query), "/opt/query"]
    if inference_socket_dir is not None:
        mounts += ["--dir", "/opt/inference", "--ro-bind", str(Path(inference_socket_dir)), "/opt/inference"]
    if broker_socket_dir is not None:
        mounts += ["--dir", "/run", "--dir", "/run/review-loop",
                   "--ro-bind", str(Path(broker_socket_dir)), "/run/review-loop/broker"]
    if client_code is not None:
        mounts += ["--ro-bind", str(Path(client_code)), "/opt/client"]
    return mounts + [
            "--setenv", "HOME", "/home/agent", "--setenv", "HERMES_HOME", "/home/agent",
            "--setenv", "PYTHONPATH", "/opt/code:/opt/client" if client_code else "/opt/code", "--setenv", "CARGO_HOME", "/tmp/cargo",
            "--setenv", "RUSTUP_HOME", "/tmp/rustup", "--setenv", "CARGO_TARGET_DIR", "/work/target" if checkout_writable else "/tmp/target",
            "--setenv", "TMPDIR", "/tmp", "--setenv", "PATH", "/opt/venv/bin:/opt/rust/bin:/usr/bin:/bin",
            "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null", "--setenv", "GIT_CONFIG_SYSTEM", "/dev/null",
            "--setenv", "GIT_TERMINAL_PROMPT", "0", "--chdir", "/work", "--", *entry]


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
