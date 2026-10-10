"""Native Git directory pinning, exercised on Linux too through the isolated helper."""
import _home_guard  # noqa: F401
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diaktoros import trusted_turn


class NativeSnapshotPinning(unittest.TestCase):
    def repository(self, path, text):
        path.mkdir()
        (path / 'run_agent.py').write_text(text)
        (path / 'credentials.json').write_text('{"secret": "HOST_FIXTURE"}')
        subprocess.run(['/usr/bin/git', 'init', '-q', str(path)], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(path), 'add', '.'], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(path), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.org', 'commit', '-qm', 'fixture'], check=True)

    def test_replaced_source_path_still_exports_the_opened_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, replacement = root / 'source', root / 'replacement'
            self.repository(source, 'original committed source\n')
            self.repository(replacement, 'REPLACEMENT MUST NOT BE EXPORTED\n')
            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            before = os.getcwd()
            try:
                source.rename(root / 'pinned')
                source.symlink_to(replacement, target_is_directory=True)
                with mock.patch.object(sys, 'platform', 'darwin'):
                    trusted_turn._export_committed_source(descriptor, root / 'snapshot')
            finally:
                os.close(descriptor)
            self.assertEqual((root / 'snapshot/run_agent.py').read_text(),
                             'original committed source\n')
            self.assertFalse((root / 'snapshot/credentials.json').exists())
            self.assertEqual(os.getcwd(), before)

    def test_native_export_ignores_dirty_and_staged_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            self.repository(source, 'committed\n')
            (source / 'run_agent.py').write_text('dirty or staged secret\n')
            subprocess.run(['/usr/bin/git', '-C', str(source), 'add', 'run_agent.py'], check=True)
            with mock.patch.object(sys, 'platform', 'darwin'):
                trusted_turn._safe_code_snapshot(source, root / 'snapshot')
            self.assertEqual((root / 'snapshot/run_agent.py').read_text(), 'committed\n')

    def test_invalid_directory_descriptor_has_no_path_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'file'
            file.touch()
            descriptor = os.open(file, os.O_RDONLY)
            try:
                with mock.patch.object(sys, 'platform', 'darwin'):
                    with self.assertRaises(trusted_turn.TurnDenied):
                        trusted_turn._export_committed_source(descriptor, root / 'snapshot')
            finally:
                os.close(descriptor)
            self.assertFalse((root / 'snapshot').exists())


if __name__ == '__main__':
    unittest.main()
