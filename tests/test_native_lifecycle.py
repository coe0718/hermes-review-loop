"""Characterize unresolved native lifecycle gaps; these are not production acceptance.

Every survivor is a cooperative, time-bounded fixture. The host registers a
kernel exit notification before triggering failure and waits for actual exit
before removing fixture paths. No polling process-tree killer is proposed here.
"""
import _home_guard  # noqa: F401
from pathlib import Path
from contextlib import closing
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from diaktoros import contained, native_lifecycle, seatbelt


CHILD = '''
import json, os, sys, time
from pathlib import Path
work, control, secret = map(Path, sys.argv[1:4])
owner = os.getppid()
if sys.argv[4] in ('detach', 'group'):
    pid = os.fork()
    if pid:
        # The host's timeout will kill this original process group.
        deadline = time.monotonic() + 15
        trigger = 'finish' if sys.argv[4] == 'group' else 'release'
        while time.monotonic() < deadline and not (control / trigger).exists():
            time.sleep(0.02)
        if sys.argv[4] != 'group':
            os.waitpid(pid, 0)
        raise SystemExit(0)
    if sys.argv[4] == 'detach':
        os.setsid()
# Close capture pipes: surviving because a pipe is open is a different case.
if sys.argv[4] != 'output':
    fd = os.open('/dev/null', os.O_RDWR)
    for target in (0, 1, 2):
        os.dup2(fd, target)
    if fd > 2:
        os.close(fd)
(work / 'ready-tmp').write_text(json.dumps({'pid': os.getpid(), 'pgrp': os.getpgrp(),
                                             'parent': os.getppid(), 'owner': owner}))
(work / 'ready-tmp').replace(work / 'ready')
deadline = time.monotonic() + 15
try:
    while time.monotonic() < deadline and not (control / 'release').exists():
        if (control / 'probe').exists():
            if sys.argv[4] == 'output':
                sys.stdout.write('x' * 1000000)
                sys.stdout.flush()
            try:
                secret.read_text()
            except PermissionError:
                (work / 'response-tmp').write_text('alive; host secret denied')
                (work / 'response-tmp').replace(work / 'after-failure')
            else:
                (work / 'response-tmp').write_text('HOST SECRET WAS READ')
                (work / 'response-tmp').replace(work / 'after-failure')
        time.sleep(0.02)
finally:
    (work / 'done').write_text('fixture exited')
'''

SUPERVISOR = '''
import json, os, subprocess, sys, time
from pathlib import Path
from diaktoros import contained, native_lifecycle
launcher = native_lifecycle if sys.argv[5] == "guarded" else contained
argv, env = json.loads(sys.argv[1]), json.loads(sys.argv[2])
if sys.argv[7] == 'extra-writer':
    original_popen = subprocess.Popen
    def spawn(*args, **kwargs):
        result = original_popen(*args, **kwargs)
        if os.fork() == 0:
            # Deliberately retain the host's lifeline writer after fork.
            fd = os.open('/dev/null', os.O_RDWR)
            for target in (0, 1, 2):
                os.dup2(fd, target)
            if fd > 2:
                os.close(fd)
            control = Path(sys.argv[8])
            (control / 'extra-tmp').write_text(str(os.getpid()))
            (control / 'extra-tmp').replace(control / 'extra-ready')
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not (control / 'release').exists():
                time.sleep(0.02)
            os._exit(0)
        return result
    subprocess.Popen = spawn
try:
    result = launcher.capture(argv, env=env, timeout=int(sys.argv[3]))
except subprocess.TimeoutExpired:
    Path(sys.argv[4]).write_text('timeout observed')
except contained.OutputLimitExceeded:
    if sys.argv[6] != 'output':
        raise
    Path(sys.argv[4]).write_text('output limit observed')
else:
    if sys.argv[6] == 'normal' and result.returncode == 0:
        Path(sys.argv[4]).write_text('normal exit observed')
    else:
        raise AssertionError('fixture unexpectedly completed')
'''


class NativeLifecycleValidation(unittest.TestCase):
    def test_failed_watchdog_during_output_abort_reports_unverified_cleanup(self):
        # No sandbox/turn is launched: this fake helper fails after excessive
        # output, exercising the parent's abort-completion validation on Linux too.
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'failed-watchdog.py'
            helper.write_text("import sys; print('fixture', flush=True); sys.exit(7)")
            with mock.patch.object(native_lifecycle, '__file__', str(helper)), \
                    mock.patch.object(sys, 'platform', 'darwin'), \
                    mock.patch.object(contained, 'MAX_CAPTURE', 0):
                with self.assertRaises(native_lifecycle.CleanupIncomplete):
                    native_lifecycle.capture(['unused'], env={}, timeout=5)

    def test_missing_status_does_not_wait_for_an_inherited_writer(self):
        reader, writer = os.pipe()
        try:
            with self.assertRaises(native_lifecycle.CleanupIncomplete):
                native_lifecycle._completion(subprocess.CompletedProcess([], 0), reader)
        finally:
            os.close(reader)
            os.close(writer)


class NativeLifecycleGaps(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_LIFECYCLE') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def wait_file(self, path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file() and path.stat().st_size:
                return path.read_text()
            time.sleep(0.02)
        self.fail(f'fixture did not write {path.name}')

    def probe_gap(self, *, detached, guarded=False, normal=False, extra_writer=False,
                  output_limit=False):
        with tempfile.TemporaryDirectory(prefix='dk-life-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            work, control = root / 'work', root / 'control'
            work.mkdir()
            control.mkdir()
            secret = root / 'host-secret'
            secret.write_text('FAKE_SECRET')
            script = control / 'child.py'
            script.write_text(CHILD)
            profile = seatbelt.profile(read_roots=(Path(sys.base_prefix), control),
                                       write_roots=(work,))
            policy = root / 'policy.sb'
            policy.write_text(profile.text)
            argv = profile.command(policy, [str(Path(sys.executable).resolve()), '-I', '-B',
                                            str(script), str(work), str(control), str(secret),
                                            'output' if output_limit else
                                            ('group' if normal else ('detach' if detached else 'stay'))])
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(work), 'TMPDIR': str(work)}
            with closing(select.kqueue()) as exits, closing(select.kqueue()) as extra_exits:
                supervisor = subprocess.Popen(
                    [sys.executable, '-c', SUPERVISOR, json.dumps(argv), json.dumps(env),
                     '3' if detached else '30', str(root / 'timeout'),
                     'guarded' if guarded else 'legacy',
                     'output' if output_limit else ('normal' if normal else 'timeout'),
                     'extra-writer' if extra_writer else 'ordinary', str(control)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    start_new_session=True)
                registered = False
                extra_registered = False
                try:
                    ready = json.loads(self.wait_file(work / 'ready'))
                    watched = list({ready['pid'], ready['parent'], ready['owner']} if guarded else
                                   ({ready['pid'], ready['parent']} if detached else {ready['pid']}))
                    remaining = set(watched)
                    exits.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                                 flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                 fflags=select.KQ_NOTE_EXIT) for pid in watched], 0, 0)
                    registered = True
                    if extra_writer:
                        extra_pid = int(self.wait_file(control / 'extra-ready'))
                        extra_exits.control([select.kevent(extra_pid, filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                            fflags=select.KQ_NOTE_EXIT)], 0, 0)
                        extra_registered = True
                    if normal:
                        (control / 'finish').touch()
                        _, errors = supervisor.communicate(timeout=8)
                        self.assertEqual(supervisor.returncode, 0, errors.decode(errors='replace'))
                        self.assertEqual(self.wait_file(root / 'timeout'), 'normal exit observed')
                    elif output_limit:
                        (control / 'probe').touch()
                        _, errors = supervisor.communicate(timeout=8)
                        self.assertEqual(supervisor.returncode, 0, errors.decode(errors='replace'))
                        self.assertEqual(self.wait_file(root / 'timeout'), 'output limit observed')
                    elif detached:
                        self.assertEqual(ready['pid'], ready['pgrp'])
                        _, errors = supervisor.communicate(timeout=8)
                        self.assertEqual(supervisor.returncode, 0, errors.decode(errors='replace'))
                        self.assertEqual(self.wait_file(root / 'timeout'), 'timeout observed')
                    else:
                        supervisor.kill()  # SIGKILL: no Python finally/atexit can run.
                        supervisor.communicate(timeout=5)
                        self.assertEqual(supervisor.returncode, -signal.SIGKILL)
                    if guarded and not detached:
                        deadline = time.monotonic() + 5
                        while remaining and time.monotonic() < deadline:
                            for event in exits.control(None, len(remaining),
                                                       max(0, deadline - time.monotonic())):
                                self.assertTrue(event.fflags & select.KQ_NOTE_EXIT)
                                remaining.discard(event.ident)
                        self.assertFalse(remaining, 'watchdog did not terminate its group and exit')
                        self.assertFalse((work / 'done').exists(), 'fixture exited cooperatively')
                        if extra_writer:
                            self.assertFalse(extra_exits.control(None, 1, 0),
                                             'inherited writer exited before watchdog cleanup')
                    else:
                        # Require fresh activity after failure, not an earlier heartbeat.
                        (control / 'probe').touch()
                        self.assertEqual(self.wait_file(work / 'after-failure'),
                                         'alive; host secret denied')
                finally:
                    (control / 'release').touch()
                    if supervisor.poll() is None:
                        supervisor.kill()
                    supervisor.communicate(timeout=5)
                    if registered:
                        deadline = time.monotonic() + 17
                        while remaining and time.monotonic() < deadline:
                            events = exits.control(None, len(remaining),
                                                   max(0, deadline - time.monotonic()))
                            for event in events:
                                self.assertTrue(event.fflags & select.KQ_NOTE_EXIT)
                                remaining.discard(event.ident)
                        self.assertFalse(remaining, 'fixture processes did not actually exit')
                        if extra_registered:
                            extra_events = extra_exits.control(None, 1, 17)
                            self.assertTrue(extra_events, 'inherited writer did not exit')
                            self.assertTrue(extra_events[0].fflags & select.KQ_NOTE_EXIT)
                    else:
                        # Startup failure may occur after fork but before ready. A bounded
                        # fixture observes release or its own deadline; preserve its paths.
                        time.sleep(16)

    def test_detached_child_survives_group_timeout_but_keeps_seatbelt(self):
        self.probe_gap(detached=True)

    def test_child_survives_supervisor_sigkill_but_keeps_seatbelt(self):
        self.probe_gap(detached=False)

    def test_watchdog_kills_original_group_after_supervisor_sigkill(self):
        self.probe_gap(detached=False, guarded=True)

    def test_owner_exit_watch_works_with_an_inherited_lifeline_writer(self):
        self.probe_gap(detached=False, guarded=True, extra_writer=True)

    def test_output_abort_works_with_an_inherited_lifeline_writer(self):
        self.probe_gap(detached=False, guarded=True, extra_writer=True, output_limit=True)

    def test_watchdog_kills_group_descendants_after_normal_leader_exit(self):
        self.probe_gap(detached=False, guarded=True, normal=True)

    def test_watchdog_still_does_not_own_detached_descendants(self):
        self.probe_gap(detached=True, guarded=True)

    def test_seat_cannot_kill_watchdog_or_inherit_its_private_descriptors(self):
        with tempfile.TemporaryDirectory(prefix='dk-wd-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            profile = seatbelt.profile(read_roots=(Path(sys.base_prefix),), write_roots=())
            policy = root / 'policy.sb'
            policy.write_text(profile.text)
            script = """
import errno, os, signal
try:
    os.kill(os.getppid(), signal.SIGKILL)
except PermissionError:
    pass
else:
    raise AssertionError('seat could signal trusted watchdog')
for fd in range(3, 128):
    try:
        os.fstat(fd)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        raise AssertionError('unexpected inherited descriptor: ' + str(fd))
print('WATCHDOG_PROTECTED')
"""
            argv = profile.command(policy, [str(Path(sys.executable).resolve()), '-I', '-B',
                                            '-c', script])
            result = native_lifecycle.capture(argv, env={'PATH': '/usr/bin:/bin'}, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('WATCHDOG_PROTECTED', result.stdout)

    def test_watchdog_preserves_output_and_returncode(self):
        result = native_lifecycle.capture([sys.executable, '-c',
            "import sys; print('output'); print('warning',file=sys.stderr); sys.exit(7)"],
            env={'PATH': '/usr/bin:/bin'}, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (7, 'output\n', 'warning\n'))

    def test_watchdog_output_limit_requests_cleanup(self):
        with self.assertRaises(contained.OutputLimitExceeded):
            native_lifecycle.capture([sys.executable, '-c',
                "import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(10)"],
                env={'PATH': '/usr/bin:/bin'}, timeout=5)


if __name__ == '__main__':
    unittest.main()
