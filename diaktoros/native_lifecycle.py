"""Experimental independent watchdog for a native turn's original process group.

The host-only lifeline never reaches the sandboxed executable. EOF, timeout,
and normal leader exit all kill the original group before reaping its leader.
Detached descendants remain an explicit production blocker. This is not a
general descendant tracker, nor recovery after watchdog/host-machine death.
"""
from __future__ import annotations

from contextlib import closing
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

MAX_CONFIG = 1024 * 1024
MAX_STATUS = 1024
MAX_GROUP_MEMBERS = 65536


class CleanupIncomplete(RuntimeError):
    """Watchdog completion cannot be verified; storage must be retained."""


def _only_unreaped_leader(pid: int) -> bool:
    """Verify the EPERM zombie-only case, never suppress a live-group denial.

    This is one group-membership check while the known leader's PID is reserved,
    not a descendant tracker or a source of PIDs to signal individually.
    proc_listpgrppids returns a PID count (not bytes); full buffers are retried.
    """
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    query = library.proc_listpgrppids
    query.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
    query.restype = ctypes.c_int
    capacity = 256
    while capacity <= MAX_GROUP_MEMBERS:
        members = (ctypes.c_int * capacity)()
        count = query(pid, members, ctypes.sizeof(members))
        if count < 0:
            raise OSError(ctypes.get_errno(), 'cannot verify native process group')
        if count < capacity:
            return set(members[:count]) == {pid}
        capacity *= 2
    return False


def _watch(config_fd: int, lifeline_fd: int, status_fd: int):
    # Configuration comes from a trusted anonymous file, not child arguments or state.
    with os.fdopen(config_fd, 'rb') as source:
        config = json.loads(source.read(MAX_CONFIG + 1))
    argv, env, timeout, cwd, owner = (config[k] for k in ('argv', 'env', 'timeout', 'cwd', 'owner'))
    process = None
    timed_out = False
    parent_lost = False
    leader_exited = False
    try:
        with closing(select.kqueue()) as events:
            events.control([select.kevent(lifeline_fd, filter=select.KQ_FILTER_READ,
                                          flags=select.KQ_EV_ADD)], 0, 0)
            # A fork-only host child can retain a copy of the lifeline writer.
            # Also watch our actual parent, checking PPID around registration so
            # startup after reparenting/PID reuse cannot target an unrelated PID.
            if owner <= 1 or os.getppid() != owner:
                parent_lost = True
            else:
                try:
                    events.control([select.kevent(owner, filter=select.KQ_FILTER_PROC,
                                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                  fflags=select.KQ_NOTE_EXIT)], 0, 0)
                except OSError as error:
                    if error.errno != errno.ESRCH:
                        raise
                    parent_lost = True
                if os.getppid() != owner:
                    parent_lost = True
            # Do not launch new work if the owner died during watchdog startup.
            if parent_lost or events.control(None, 2, 0):
                parent_lost = True
            else:
                process = subprocess.Popen(argv, env=env, cwd=cwd, close_fds=True,
                                           start_new_session=True)
                # No wait/poll before group cleanup: the unreaped child reserves
                # its PID, preventing a stale group ID from targeting a reused PID.
                try:
                    events.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                  fflags=select.KQ_NOTE_EXIT)], 0, 0)
                except OSError as error:
                    if error.errno != errno.ESRCH:
                        raise
                    leader_exited = True
                    # A very short-lived child can exit before registration.
                    # It has not been reaped; cleanup still precedes wait().
                else:
                    deadline = time.monotonic() + timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        ready = events.control(None, 3, remaining)
                        if not ready:
                            timed_out = True
                            break
                        leader_exited = any(event.filter == select.KQ_FILTER_PROC and
                                            event.ident == process.pid for event in ready)
                        if any(event.filter == select.KQ_FILTER_READ or
                               (event.filter == select.KQ_FILTER_PROC and event.ident == owner)
                               for event in ready):
                            parent_lost = True
                            break
                        if leader_exited:
                            break
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin excludes zombies from group signalling and returns
                # EPERM if no signalable member remains. Require both a known
                # leader exit and a group containing only that unreaped PID.
                if not leader_exited or not _only_unreaped_leader(process.pid):
                    raise
            process.wait()
    result = {'returncode': process.returncode if process is not None else None,
              'timeout': timed_out, 'parent_lost': parent_lost}
    try:
        os.write(status_fd, json.dumps(result).encode())
    except BrokenPipeError:
        pass  # Owner death closed the status reader; group cleanup already ran.
    finally:
        os.close(status_fd)
        os.close(lifeline_fd)


def _completion(process, descriptor, *, allow_owner_loss=False):
    # The helper has exited. A host fork may still hold a status writer, so
    # missing completion must fail closed rather than block waiting for EOF.
    os.set_blocking(descriptor, False)
    try:
        status = os.read(descriptor, MAX_STATUS + 1)
    except BlockingIOError:
        status = b''
    try:
        record = json.loads(status)
        valid = (process.returncode == 0 and len(status) <= MAX_STATUS and
                 type(record['returncode']) is int and
                 type(record['timeout']) is bool and type(record['parent_lost']) is bool and
                 (allow_owner_loss or record['parent_lost'] is False))
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise CleanupIncomplete('native watchdog completion could not be verified; '
                                f'helper={process.returncode}, status={status!r}')
    return record


def capture(argv: list[str], *, env: dict[str, str], timeout: int,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Capture through a trusted watchdog; no sandbox is provided by this function.

    Caller must pass an already-contained command. No unsupervised fallback.
    The helper is outside the child's Seatbelt profile, in a separate session,
    with a scrubbed environment and exclusively host-owned pipe descriptors.
    """
    from . import contained
    if sys.platform != 'darwin':
        raise contained.ContainmentUnavailable('native watchdog requires macOS kqueue')
    if type(timeout) is not int or timeout < 1:
        raise ValueError('watchdog timeout must be positive')
    config = json.dumps({'argv': argv, 'env': env, 'timeout': timeout,
                         'cwd': str(cwd) if cwd is not None else None,
                         'owner': os.getpid()}).encode()
    if len(config) > MAX_CONFIG:
        raise ValueError('native watchdog configuration exceeds limit')
    with tempfile.TemporaryFile() as source:
        source.write(config)
        source.seek(0)
        life_read, life_write = os.pipe()
        status_read, status_write = os.pipe()
        process = None
        try:
            process = subprocess.Popen(
                [str(Path(sys.executable).resolve()), '-I', '-B', str(Path(__file__).resolve()),
                 str(source.fileno()), str(life_read), str(status_write)],
                pass_fds=(source.fileno(), life_read, status_write),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
                env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
            os.close(life_read)
            life_read = None
            os.close(status_write)
            status_write = None

            def abort():
                nonlocal life_write
                if life_write is not None:
                    # An explicit request also works if a forked host process
                    # retains another writer while this supervisor stays alive.
                    try:
                        os.write(life_write, b'x')
                    except BrokenPipeError:
                        pass
                    os.close(life_write)
                    life_write = None
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired as error:
                    # Do not kill the independent cleanup owner or pretend it
                    # finished. Caller must preserve storage for recovery.
                    raise CleanupIncomplete('native watchdog cleanup did not complete') from error
                _completion(process, status_read, allow_owner_loss=True)

            result = contained.capture_process(process, argv=argv, timeout=timeout + 10,
                                               abort=abort)
            try:
                record = _completion(process, status_read)
            except CleanupIncomplete as error:
                raise CleanupIncomplete(str(error) + '; ' + result.stderr[-3000:]) from error
            if record['timeout']:
                raise subprocess.TimeoutExpired(argv, timeout, result.stdout.encode(),
                                                result.stderr.encode())
            return subprocess.CompletedProcess(argv, record['returncode'],
                                               result.stdout, result.stderr)
        finally:
            for descriptor in (life_read, life_write, status_read, status_write):
                if descriptor is not None:
                    os.close(descriptor)


if __name__ == '__main__':
    _watch(*(int(value) for value in sys.argv[1:]))
