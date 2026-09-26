"""A secret merely *committed* in the source must not reach the sandbox (issue #88).

An isolated turn mounts an exported copy of the source at ``/opt/code`` (and on ``PYTHONPATH``) of
a seat that runs untrusted PR content, so anything committed there is readable by that seat — and a
seat has one trusted write channel (the broker: review body, ruling, push manifest) through which
those bytes can be published. The snapshot filter therefore has to reject a *shape*, not an exact
component: ``credentials.json``, ``keys.json``, ``prod-secrets.toml`` and ``secrets.yaml`` were all
exported by the component-exact filter, and so was an ordinary ``notes.md`` holding a token.

Two rules are checked here:

* a credential-shaped *name* is dropped on whole words, so ``monkey.py``/``keyboard.py`` stay
  source while ``keys.json``/``prod-secrets.toml`` do not (a naive substring rule fails this);
* documentation and data whose *content* is credential-shaped is dropped too, which is the only
  rule that catches ``deploy/notes.md``.

Code and templates are deliberately exempt from the shape rule: the sandbox imports this tree, and
dropping an importable module breaks the turn at start (``hermes_cli.subcommands.secrets`` is
imported by ``hermes_cli.main``; ``agent/secret_sources`` is a package *about* credentials). The
exact names of the original filter (``credentials``, ``id_rsa``, ...) still apply everywhere, and a
shape-named directory holding data or docs is still a credential container.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from review_loop import trusted_turn  # noqa: E402
from test_selftest import SelftestBase  # noqa: E402  (its fixture: mocked GitHub/model/bwrap)

SENTINEL = 'SENTINEL-4f1c9a7b2e3d5f60a1b2c3d4e5f60718'

# Exactly the files the audit found exported while carrying the sentinel.
SECRET_FILES = {
    'credentials.json': '{"aws": "%s"}\n' % SENTINEL,
    'keys.json': '{"%s": "value"}\n' % SENTINEL,
    'prod-secrets.toml': 'secret = "%s"\n' % SENTINEL,
    'secrets.yaml': 'token: %s\n' % SENTINEL,
    'deploy/notes.md': 'runbook\n\nDEPLOY_TOKEN=%s\n' % SENTINEL,
}

# Ordinary source that must survive the filter, including the false-positive guards.
ORDINARY = ('run_agent.py', 'review_loop/run_supervisor.py', 'review_loop/prompts.py',
            'monkey.py', 'keyboard.py', 'keyring.py', 'tokenizer.py', 'secretive.py',
            'monkey_business.py', 'docs_map.txt')


class GitTree(unittest.TestCase):
    """A throwaway committed checkout the filter can snapshot."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.destination = self.root / 'snapshot'
        self.write({**SECRET_FILES, **{name: 'ordinary source\n' for name in ORDINARY}})
        self.git('init', '-q')
        self.git('add', '.')
        self.commit('secrets')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args])

    def commit(self, message):
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm',
                 message)

    def write(self, files):
        for name, text in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def export(self) -> pathlib.Path:
        trusted_turn._safe_code_snapshot(self.source, self.destination)
        return self.destination


class SnapshotSecretTests(GitTree):
    def test_committed_sentinel_files_are_not_exported(self):
        destination = self.export()
        for name in SECRET_FILES:
            self.assertFalse((destination / name).exists(), f'{name} reached the sandbox')

    def test_no_exported_byte_carries_the_sentinel(self):
        destination = self.export()
        carriers = [str(path.relative_to(destination))
                    for path in destination.rglob('*')
                    if path.is_file() and SENTINEL.encode() in path.read_bytes()]
        self.assertEqual(carriers, [])

    def test_ordinary_source_is_exported(self):
        destination = self.export()
        for name in ORDINARY:
            self.assertTrue((destination / name).is_file(), name)

    def test_naive_substring_rule_would_have_dropped_these(self):
        # A rule matching 'key' inside 'monkey'/'keyboard' is the false positive to avoid.
        destination = self.export()
        for name in ('monkey.py', 'keyboard.py', 'keyring.py', 'tokenizer.py', 'secretive.py',
                     'monkey_business.py'):
            self.assertTrue((destination / name).is_file(), name)

    def test_whitespace_and_case_variants_of_a_credential_name_are_dropped(self):
        self.write({'prod/TOKEN Store.json': '{"k": "%s"}\n' % SENTINEL,
                    'pkg/api-key.yaml': 'k: %s\n' % SENTINEL,
                    'pkg/password-store.toml': 'k = "%s"\n' % SENTINEL,
                    'pkg/private_key.json': '{"k": "%s"}\n' % SENTINEL})
        self.git('add', '.')
        self.commit('variants')
        destination = self.export()
        for name in ('prod/TOKEN Store.json', 'pkg/api-key.yaml', 'pkg/password-store.toml',
                     'pkg/private_key.json'):
            self.assertFalse((destination / name).exists(), name)

    def test_credential_shaped_directory_of_data_is_not_exported(self):
        # A directory of data or docs named like a credential is a credential container.
        self.write({'pkg/keys/prod.json': '{"k": "%s"}\n' % SENTINEL,
                    'pkg/credentials/notes.md': 'operator runbook\n'})
        self.git('add', '.')
        self.commit('containers')
        destination = self.export()
        self.assertFalse((destination / 'pkg/keys/prod.json').exists())
        self.assertFalse((destination / 'pkg/credentials/notes.md').exists())

    def test_importable_modules_are_exported_whatever_their_name(self):
        # The snapshot is on PYTHONPATH: `hermes_cli.main` imports
        # `hermes_cli.subcommands.secrets` at module level, and `agent/secret_sources` is a package
        # about credentials. The shape rule reads a module's name only when the sandbox does not
        # import it; the same word on a data file (or a data directory) is a credential.
        self.write({'hermes_cli/subcommands/secrets.py': 'ordinary source\n',
                    'review_loop/secrets.py': 'ordinary source\n',
                    'agent/secret_sources/registry.py': 'ordinary source\n',
                    'review_loop/secrets.json': '{"k": "%s"}\n' % SENTINEL})
        self.git('add', '.')
        self.commit('modules')
        destination = self.export()
        for name in ('hermes_cli/subcommands/secrets.py', 'review_loop/secrets.py',
                     'agent/secret_sources/registry.py'):
            self.assertTrue((destination / name).is_file(), name)
        self.assertFalse((destination / 'review_loop/secrets.json').exists())

    def test_credential_shaped_content_is_dropped_from_data_and_docs(self):
        # A name rule cannot see these: the leak is the value.
        self.write({'deploy/copy.md': 'API_KEY = sk-' + 'L' * 30 + '\n',
                    'config/settings.json': '{"refresh_token": "%s"}\n' % SENTINEL})
        self.git('add', '.')
        self.commit('values')
        destination = self.export()
        self.assertFalse((destination / 'deploy/copy.md').exists())
        self.assertFalse((destination / 'config/settings.json').exists())


class ExportedSnapshotProbeTests(GitTree):
    """``exported_secrets`` is what the selftest runs over the snapshot it just staged."""

    def setUp(self):
        super().setUp()
        self.probe_root = self.root / 'probe'
        self.probe_root.mkdir()

    def probe(self, files):
        self.write_under_probe(files)
        return trusted_turn.exported_secrets(self.probe_root)

    def write_under_probe(self, files):
        for name, text in files.items():
            path = self.probe_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_probe_reports_a_secret_that_reached_the_snapshot(self):
        violations, advisories = self.probe({
            'credentials.json': '{"aws": "%s"}\n' % SENTINEL,
            'deploy/notes.md': 'DEPLOY_TOKEN=%s\n' % SENTINEL,
            'run_agent.py': 'ordinary source\n'})
        self.assertTrue(any('credentials.json' in entry for entry in violations), violations)
        self.assertTrue(any('deploy/notes.md' in entry for entry in violations), violations)
        self.assertEqual(advisories, [])

    def test_probe_reports_a_credential_directory(self):
        violations, _ = self.probe({'keys/prod.json': '{"k": "value"}\n'})
        self.assertTrue(any('keys/prod.json' in entry for entry in violations), violations)

    def test_probe_flags_imported_code_as_advisory_only(self):
        # Code is never a violation: the sandbox imports it, so dropping it is not an option.
        violations, advisories = self.probe({'review_loop/config_source.py':
                                             'API_KEY = "sk-' + 'a' * 30 + '"\n'})
        self.assertEqual(violations, [])
        self.assertTrue(any('config_source.py' in entry for entry in advisories), advisories)

    def test_probe_is_quiet_on_the_export_of_a_clean_tree(self):
        self.write({'clean/notes.md': 'no credentials here\n'})
        self.git('add', '.')
        self.commit('clean notes')
        destination = self.export()
        self.assertEqual(trusted_turn.exported_secrets(destination), ([], []))


class SelftestSnapshotCheckTests(SelftestBase):
    """The selftest step that runs the probe over the snapshot it just staged (issue #88)."""

    def stage(self, files):
        def snapshot(source, destination):
            destination.mkdir()
            (destination / 'run_agent.py').write_text('committed\n')
            for name, text in files.items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        return snapshot

    def test_a_secret_that_reached_the_snapshot_fails_containment(self):
        # `_safe_code_snapshot` is mocked, so this is exactly a filter regression: the bytes are
        # in the tree the sandbox mounts.
        stage = self.stage({'credentials.json': '{"aws": "%s"}\n' % SENTINEL,
                            'deploy/notes.md': 'DEPLOY_TOKEN=%s\n' % SENTINEL})
        with mock.patch.object(trusted_turn, '_safe_code_snapshot', side_effect=stage):
            rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1)
        self.assertRegex(text, r'❌ sandbox:secret-files .*credentials\.json')
        self.assertIn('deploy/notes.md', text)
        self.assertIn('fix these before enabling turns', text)

    def test_credential_shaped_text_in_imported_code_is_a_warning(self):
        stage = self.stage({'review_loop/config_source.py': 'API_KEY = "sk-' + 'b' * 30 + '"\n'})
        with mock.patch.object(trusted_turn, '_safe_code_snapshot', side_effect=stage):
            rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r'⚠️\s+sandbox:secret-text .*config_source\.py')
        self.assertNotIn('❌ sandbox:secret-files', text)


if __name__ == '__main__':
    unittest.main()
