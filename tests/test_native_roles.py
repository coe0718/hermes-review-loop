"""Actual checkout mutation permissions for each host-selected native role."""
import _home_guard  # noqa: F401
from pathlib import Path
import os
import sys
import tempfile
import unittest

from diaktoros import native_lifecycle, native_macos, seatbelt, turn_layout


class RoleValidation(unittest.TestCase):
    def test_unknown_roles_have_no_writable_fallback(self):
        for role in ('observer', '', None, True, {}):
            with self.subTest(role=role), self.assertRaises(ValueError):
                native_macos._work_roots(turn_layout.TurnLayout.linux(), role=role)


class NativeRoleBoundary(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_ROLES') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def probe(self, role):
        writable = role not in ('adjudicator', 'triage')
        with tempfile.TemporaryDirectory(prefix='dk-role-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            paths = {name: root / name for name in
                     ('code', 'venv', 'home', 'work', 'export', 'client', 'scratch', 'query')}
            for name, path in paths.items():
                if name == 'query':
                    path.touch()
                else:
                    path.mkdir()
            layout = turn_layout.TurnLayout(**paths)
            existing = layout.work / 'existing'
            existing.write_text('original')
            secret = root / 'host-secret'
            secret.write_text('HOST_SECRET')
            (layout.work / 'escape').symlink_to(secret)
            reads, writes = native_macos._work_roots(layout, role=role)
            profile = seatbelt.profile(read_roots=(Path(sys.base_prefix), *reads), write_roots=writes)
            policy = root / 'policy.sb'
            policy.write_text(profile.text)
            script = f'''
from pathlib import Path
import subprocess, sys
work, home, scratch = map(Path, {tuple(str(p) for p in (layout.work, layout.home, layout.scratch))!r})
assert (work / 'existing').read_text() == 'original'
for action in (
    lambda: (work / 'existing').write_text('updated'),
    lambda: Path('created').write_text('created'),
    lambda: (work / 'existing').chmod(0o777),
    lambda: (work / 'existing').rename(work / 'renamed'),
    lambda: (work / {'renamed' if writable else 'existing'!r}).unlink(),
):
    try:
        action()
    except PermissionError:
        assert not {writable!r}, 'writing role cannot mutate checkout'
    else:
        assert {writable!r}, 'judging role mutated checkout'
for action in (lambda: (work / 'escape').read_text(),
               lambda: (work / 'escape').write_text('escaped')):
    try:
        action()
    except PermissionError:
        pass
    else:
        raise AssertionError('host secret accessible through checkout symlink')
(home / 'home-output').write_text('allowed')
(scratch / 'scratch-output').write_text('allowed')
if not {writable!r}:
    child = "from pathlib import Path\\ntry: Path('child-output').write_text('bad')\\nexcept PermissionError: pass\\nelse: raise AssertionError('descendant mutated checkout')"
    subprocess.run([sys.executable, '-I', '-B', '-c', child], check=True)
print('ROLE_PERMISSIONS_VERIFIED')
'''
            result = native_lifecycle.capture(profile.command(policy,
                [str(Path(sys.executable).resolve()), '-I', '-B', '-c', script]),
                env={'PATH': '/usr/bin:/bin', 'HOME': str(layout.home), 'TMPDIR': str(layout.scratch)},
                cwd=layout.work, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('ROLE_PERMISSIONS_VERIFIED', result.stdout)
            self.assertEqual(secret.read_text(), 'HOST_SECRET')
            self.assertTrue((layout.home / 'home-output').exists())
            self.assertTrue((layout.scratch / 'scratch-output').exists())
            if not writable:
                self.assertEqual(existing.read_text(), 'original')
                self.assertFalse((layout.work / 'created').exists())
                self.assertFalse((layout.work / 'renamed').exists())
                self.assertFalse((layout.work / 'child-output').exists())

    def test_adjudicator_cannot_mutate_checkout(self):
        self.probe('adjudicator')

    def test_triage_cannot_mutate_checkout(self):
        self.probe('triage')

    def test_reviewer_can_write_checkout(self):
        self.probe('reviewer')

    def test_fixer_can_write_checkout(self):
        self.probe('fixer')

    def test_issue_fixer_can_write_checkout(self):
        self.probe('issue_fixer')


if __name__ == '__main__':
    unittest.main()
