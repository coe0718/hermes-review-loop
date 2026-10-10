"""Native behavioral probes; Mac CI requires execution rather than accepting skips."""
import _home_guard  # noqa: F401
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diaktoros import seatbelt


class ProfileValidation(unittest.TestCase):
    def test_refuses_root_relative_and_overlapping_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = root / 'child'
            child.mkdir()
            for reads, writes in (((Path('/'),), ()), ((Path('relative'),), ()),
                                  ((child,), (root,))):
                with self.subTest(reads=reads, writes=writes), self.assertRaises(ValueError):
                    seatbelt.profile(read_roots=reads, write_roots=writes)

    def test_paths_are_parameters_not_policy_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'quoted"(allow default)'
            path.mkdir()
            profile = seatbelt.profile(read_roots=(path,), write_roots=())
            self.assertNotIn(str(path), profile.text)
            self.assertIn(('READ_0', str(path)), profile.parameters)
            self.assertNotIn('(allow default)', profile.text)

    def test_refuses_missing_non_socket_and_writable_socket(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory).resolve()
            ordinary = root / 'file'
            ordinary.touch()
            with self.assertRaises(ValueError):
                seatbelt.profile(read_roots=(), write_roots=(), sockets=(ordinary,))
            with self.assertRaises(FileNotFoundError):
                seatbelt.profile(read_roots=(), write_roots=(), sockets=(root / 'absent',))
            # Construction check only; real socket enforcement runs in NativeBoundary.
            with mock.patch.object(Path, 'is_socket', return_value=True):
                with self.assertRaises(ValueError):
                    seatbelt.profile(read_roots=(), write_roots=(root,), sockets=(ordinary,))

    def test_non_mac_launch_fails_closed(self):
        if sys.platform == 'darwin':
            return
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / 'policy.sb'
            policy.write_text('(version 1)\n(deny default)\n')
            with self.assertRaisesRegex(RuntimeError, 'requires macOS'):
                seatbelt.Profile(policy.read_text(), ()).command(policy, [sys.executable])


class NativeBoundary(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_SEATBELT') == '1':
                self.fail(reason)
            self.skipTest(reason)
        self.temp = tempfile.TemporaryDirectory(prefix='dk-sb-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work, self.code, self.ipc = (self.root / p for p in ('work', 'code', 'ipc'))
        for path in (self.work, self.code, self.ipc):
            path.mkdir()
        self.secret = self.root / 'host-secret'
        self.secret.write_text('FAKE_HOST_SECRET')

    def run_probe(self, script, *, sockets=()):
        profile = seatbelt.profile(read_roots=(Path(sys.base_prefix), self.code),
                                   write_roots=(self.work,), sockets=tuple(sockets))
        policy = self.root / 'profile.sb'
        policy.write_text(profile.text)
        policy.chmod(0o400)
        argv = profile.command(policy, [str(Path(sys.executable).resolve()), '-I', '-B',
                                        '-c', script])
        result = subprocess.run(argv, cwd=self.work,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(self.work),
                                     'TMPDIR': str(self.work), 'HERMES_HOME': str(self.work)},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_python_starts_with_scrubbed_environment_and_writable_scratch(self):
        self.run_probe("import os,pathlib; assert 'GITHUB_TOKEN' not in os.environ; "
                       "p=pathlib.Path('scratch'); p.write_text('ok'); assert p.read_text()=='ok'")

    def test_host_secret_and_read_only_source_are_enforced(self):
        source = self.code / 'source'
        source.write_text('READ_ONLY')
        self.run_probe(f"""
from pathlib import Path
assert Path({str(source)!r}).read_text() == 'READ_ONLY'
for path, action in [({str(self.secret)!r}, 'read'), ({str(source)!r}, 'write')]:
    try:
        p = Path(path)
        p.read_text() if action == 'read' else p.write_text('changed')
    except PermissionError:
        pass
    else:
        raise AssertionError('forbidden file access succeeded')
""")
        self.assertEqual(source.read_text(), 'READ_ONLY')

    def test_symlink_from_scratch_cannot_read_or_write_host_secret(self):
        self.run_probe(f"""
from pathlib import Path
p = Path('escape')
p.symlink_to({str(self.secret)!r})
for operation in (lambda: p.read_text(), lambda: p.write_text('changed')):
    try:
        operation()
    except PermissionError:
        pass
    else:
        raise AssertionError('symlink escaped the sandbox')
""")
        self.assertEqual(self.secret.read_text(), 'FAKE_HOST_SECRET')

    def test_descendant_inherits_secret_denial(self):
        script = f"""
from pathlib import Path
try:
    Path({str(self.secret)!r}).read_text()
except PermissionError:
    pass
else:
    raise AssertionError('descendant read a host secret')
"""
        self.run_probe(f"import subprocess,sys; subprocess.run([sys.executable, '-I', '-B', "
                       f"'-c', {script!r}], check=True, timeout=5)")

    def test_tcp_loopback_ipv6_and_udp_are_denied(self):
        # Host endpoints prove denial, rather than treating an absent server as isolation.
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            port = server.getsockname()[1]
            self.run_probe(f"""
import socket
for family, kind, address in [
    (socket.AF_INET, socket.SOCK_STREAM, ('127.0.0.1', {port})),
    (socket.AF_INET6, socket.SOCK_STREAM, ('::1', {port})),
    (socket.AF_INET, socket.SOCK_DGRAM, ('127.0.0.1', {port})),
    (socket.AF_INET, socket.SOCK_STREAM, ('192.0.2.1', 443))]:
    try:
        with socket.socket(family, kind) as client:
            client.settimeout(1)
            if kind == socket.SOCK_DGRAM:
                client.sendto(b'probe', address)
            else:
                client.connect(address)
    except PermissionError:
        pass
    else:
        raise AssertionError('network attempt did not fail with a policy denial')
""")

    def test_only_assigned_unix_sockets_are_reachable(self):
        with socket.socket(socket.AF_UNIX) as allowed, socket.socket(socket.AF_UNIX) as other:
            endpoint, forbidden = self.ipc / 'model.sock', self.ipc / 'other.sock'
            for server, path in ((allowed, endpoint), (other, forbidden)):
                server.bind(str(path))
                server.listen()
                server.settimeout(2)
            self.run_probe(f"""
import socket
with socket.socket(socket.AF_UNIX) as client:
    client.connect({str(endpoint)!r})
    client.sendall(b'capability')
try:
    with socket.socket(socket.AF_UNIX) as client:
        client.connect({str(forbidden)!r})
except PermissionError:
    pass
else:
    raise AssertionError('reached another turn socket')
""", sockets=(endpoint,))
            conn, _ = allowed.accept()
            with conn:
                self.assertEqual(conn.recv(100), b'capability')

    def test_capability_socket_cannot_be_deleted_or_replaced(self):
        with socket.socket(socket.AF_UNIX) as server:
            endpoint = self.ipc / 'broker.sock'
            server.bind(str(endpoint))
            server.listen()
            self.run_probe(f"""
from pathlib import Path
try:
    Path({str(endpoint)!r}).unlink()
except PermissionError:
    pass
else:
    raise AssertionError('deleted host capability socket')
""", sockets=(endpoint,))
            self.assertTrue(endpoint.is_socket())


if __name__ == '__main__':
    unittest.main()
