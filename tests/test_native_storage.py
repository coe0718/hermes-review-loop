"""Actual disk-full boundary and failure-safe cleanup for native workspaces."""
import _home_guard  # noqa: F401
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diaktoros import native_storage, seatbelt


class StorageValidation(unittest.TestCase):
    def test_invalid_sizes_fail_before_creating_storage(self):
        for value in (True, 0, 63, 16385, '512', 1.5):
            with self.assertRaises(ValueError):
                native_storage.Workspace(value)

    def test_non_mac_has_no_unbounded_fallback(self):
        if sys.platform == 'darwin':
            self.skipTest('non-mac fail-closed probe')
        workspace = native_storage.Workspace()
        with self.assertRaisesRegex(native_storage.StorageError, 'requires macOS'):
            workspace.__enter__()
        self.assertIsNone(workspace.root)

    def test_failed_detach_retains_backing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.write_bytes(b'fixture')
            with mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                    mock.patch.object(native_storage, '_run', side_effect=native_storage.StorageError('busy')):
                with self.assertRaisesRegex(native_storage.StorageError, 'retained at'):
                    workspace._cleanup()
            self.assertTrue(workspace.image.is_file())
            self.assertFalse(workspace.active)

    def test_unverified_execution_cleanup_preserves_mount_and_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.touch()
            workspace.retain('watchdog completion unknown')
            with mock.patch.object(workspace, '_devices') as devices:
                with self.assertRaisesRegex(native_storage.StorageError, 'watchdog completion unknown'):
                    workspace._cleanup()
                devices.assert_not_called()
            self.assertTrue(workspace.image.exists())

    def test_busy_eject_rediscovers_owned_disk_before_retry(self):
        workspace = native_storage.Workspace()
        busy = native_storage.StorageError('hdiutil: Resource busy')
        with mock.patch.object(workspace, '_devices', side_effect=[
                ['/dev/disk99'], ['/dev/disk99'], ['/dev/disk99'],
                ['/dev/disk100'], ['/dev/disk100'], []]), \
                mock.patch.object(native_storage, '_run', side_effect=[busy, busy, b'']) as run, \
                mock.patch.object(native_storage.time, 'sleep') as sleep:
            workspace._detach_owned()
        self.assertEqual(run.call_args_list, [
            mock.call([native_storage.HDIUTIL, 'detach', '/dev/disk99']),
            mock.call([native_storage.HDIUTIL, 'detach', '-force', '/dev/disk99']),
            mock.call([native_storage.HDIUTIL, 'detach', '/dev/disk100'])])
        sleep.assert_called_once_with(0.25)

    def test_failed_eject_that_detached_does_not_force_stale_disk(self):
        workspace = native_storage.Workspace()
        with mock.patch.object(workspace, '_devices', side_effect=[
                ['/dev/disk99'], ['/dev/disk99'], [], []]), \
                mock.patch.object(native_storage, '_run', side_effect=
                                  native_storage.StorageError('hdiutil: Resource busy')) as run:
            workspace._detach_owned()
        run.assert_called_once_with([native_storage.HDIUTIL, 'detach', '/dev/disk99'])

    def test_persistent_busy_eject_is_bounded_and_retains_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.write_bytes(b'fixture')
            with mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                    mock.patch.object(native_storage, '_run', side_effect=
                                      native_storage.StorageError('hdiutil: Resource busy')) as run, \
                    mock.patch.object(native_storage.time, 'sleep') as sleep:
                with self.assertRaisesRegex(native_storage.StorageError, 'retained at'):
                    workspace._cleanup()
            self.assertEqual(run.call_count, 6)
            self.assertEqual(sleep.call_args_list, [mock.call(0.25), mock.call(0.5)])
            self.assertEqual(workspace.image.read_bytes(), b'fixture')

    def test_false_successful_detach_still_retains_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.touch()
            with mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                    mock.patch.object(native_storage, '_run', return_value=b''):
                with self.assertRaisesRegex(native_storage.StorageError, 'retained at'):
                    workspace._cleanup()
            self.assertTrue(workspace.image.exists())


class NativeStorageBoundary(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_STORAGE') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def test_all_writable_roots_share_a_hard_allocation_bound(self):
        with tempfile.TemporaryDirectory(prefix='dk-storage-probe-', dir='/tmp') as directory:
            outer = Path(directory).resolve()
            outside = outer / 'outside'
            outside.write_text('HOST_FILE')
            policy_file = outer / 'policy.sb'
            with native_storage.Workspace(128) as workspace:
                image, image_size, root = workspace.image, workspace.image.stat().st_size, workspace.root
                workspace.validate(home=workspace.home, work=workspace.work, scratch=workspace.scratch)
                with self.assertRaises(native_storage.StorageError):
                    workspace.validate(home=workspace.home, work=outer, scratch=workspace.scratch)
                profile = seatbelt.profile(read_roots=(Path(sys.base_prefix),),
                                           write_roots=(workspace.home, workspace.work, workspace.scratch))
                script = f'''
import errno, os
from pathlib import Path
for forbidden in ({str(image)!r}, {str(outside)!r}):
    try:
        with open(forbidden, 'r+b'):
            pass
    except PermissionError:
        pass
    else:
        raise AssertionError('host/image write allowed')
escape = Path({str(workspace.work / 'escape')!r})
escape.symlink_to({str(outside)!r})
try:
    escape.write_text('changed')
except PermissionError:
    pass
else:
    raise AssertionError('symlink escaped volume')
for root in ({str(workspace.work)!r}, {str(workspace.home)!r}, {str(workspace.scratch)!r}):
    written = 0
    try:
        with open(Path(root) / 'fill', 'wb', buffering=0) as out:
            while written <= {workspace.size_bytes}:
                written += out.write(os.urandom(1024 * 1024))
    except OSError as error:
        assert error.errno == errno.ENOSPC, repr(error)
    else:
        raise AssertionError('allocated beyond fixed capacity')
print('ENOSPC_ALL_ROOTS')
'''
                policy_file.write_text(profile.text)
                argv = profile.command(policy_file, [str(Path(sys.executable).resolve()), '-I', '-B', '-c', script])
                result = subprocess.run(argv, env={'PATH': '/usr/bin:/bin', 'HOME': str(workspace.home),
                                                   'TMPDIR': str(workspace.scratch)},
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('ENOSPC_ALL_ROOTS', result.stdout)
                self.assertEqual(image.stat().st_size, image_size)
                self.assertEqual(outside.read_text(), 'HOST_FILE')
            self.assertFalse(root.exists())

    def test_exception_unmounts_and_removes_private_image(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            with native_storage.Workspace(128) as workspace:
                root = workspace.root
                raise RuntimeError('fixture failure')
        self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
