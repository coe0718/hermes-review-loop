"""Executable integration proof: the real Hermes process and tool dispatcher in bwrap."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from review_loop import contained

SOURCE = _home_guard.HERMES_AGENT_SOURCE


class WholeAgentFixture(unittest.TestCase):
    def test_host_capture_bounds_both_streams_and_kills_noisy_child(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            home = Path(directory)
            for stream in ('stdout', 'stderr'):
                with self.subTest(stream=stream):
                    code = ("import os,sys; stream=getattr(sys, '" + stream + "'); "
                            "stream.write('x' * 1000000); stream.flush()")
                    with mock.patch.object(contained, 'command', return_value=[sys.executable, '-c', code]):
                        with self.assertRaises(contained.OutputLimitExceeded):
                            contained.run(home=home, timeout=5)
            with mock.patch.object(contained, 'command', return_value=[sys.executable, '-c',
                            "import sys;print('ok');print('warning',file=sys.stderr)"]):
                result = contained.run(home=home, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (0, 'ok\n', 'warning\n'))

    @_home_guard.needs_real_hermes(bool(shutil.which('bwrap')),
                                   reason='bubblewrap or Hermes checkout unavailable')
    def test_real_agent_cannot_read_host_dummy_credentials(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            root = Path(directory)
            code = root / 'code'
            code.mkdir()
            paths = subprocess.check_output(['git', '-C', str(SOURCE), 'ls-files', '-z']).split(b'\0')
            for raw in paths:
                if not raw:
                    continue
                name = raw.decode()
                if (name.startswith(('.', 'tests/', 'docs/', 'website/', 'scripts/')) or
                        Path(name).suffix not in ('.py', '.yaml', '.json', '.txt', '.md', '.toml')):
                    continue
                target = code / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / name, target, follow_symlinks=False)
            home = root / 'home'
            home.mkdir()
            (home / 'config.yaml').write_text('''model:
  provider: custom
  default: fixture-model
  base_url: http://127.0.0.1:18761/v1
  api_key: sandbox-fixture-only
plugins:
  enabled: []
memory:
  memory_enabled: false
''')
            (home / 'query.txt').write_text('Execute the requested tool and then report completion.')
            checkout = root / 'work'
            (checkout / 'src').mkdir(parents=True)
            (checkout / 'Cargo.toml').write_text('[package]\nname="contained_proof"\nversion="0.1.0"\nedition="2021"\n')
            (checkout / 'src/lib.rs').write_text('#[test] fn works() { assert_eq!(2 + 2, 4); }\n')
            pat = root / 'host-dummy.pat'
            pat.write_text('HOST_DUMMY_PAT_SENTINEL')
            key = root / 'host-dummy.model-key'
            key.write_text('HOST_DUMMY_MODEL_KEY_SENTINEL')
            (home / 'host-paths.json').write_text(json.dumps([str(pat), str(key), str(_home_guard.USER_HOME / '.hermes/.env')]))
            venv = SOURCE / 'venv'
            # Derive the generation directory from the absolute venv Python symlink.
            runtime = Path(os.readlink(venv / 'bin/python')).parents[2]
            rust = _home_guard.RUST
            if not (rust / 'bin/cargo').exists():
                self.skipTest('offline stable Rust toolchain unavailable')
            result = contained.run(code=code, venv=venv, runtime=runtime,
                                   home=home, checkout=checkout, rust=rust,
                                   query=Path(__file__).with_name('contained_fixture.py'),
                                   entry=['/opt/venv/bin/python', '/opt/query'], timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
            lines = (home / 'requests.jsonl').read_text().splitlines()
            requests = [json.loads(line) for line in lines]
            self.assertGreaterEqual(len(requests), 2)
            self.assertTrue(all(row['authorization'] == 'Bearer sandbox-fixture-only' for row in requests))
            response = json.dumps(requests)
            self.assertNotIn('HOST_DUMMY_PAT_SENTINEL', response)
            self.assertNotIn('HOST_DUMMY_MODEL_KEY_SENTINEL', response)
            self.assertIn('FIXTURE_DONE', result.stdout)
            # The model-requested tool call must actually execute and fail on the
            # host path, not merely be omitted from the context.
            self.assertTrue(any(m.get('role') == 'tool' for row in requests
                                for m in row['request'].get('messages', [])))
            tool_output = '\n'.join(str(m.get('content', '')) for row in requests
                                    for m in row['request'].get('messages', [])
                                    if m.get('role') == 'tool')
            for secret_path in (pat, key):
                self.assertIn(str(secret_path), tool_output)
            self.assertIn('No such file or directory', tool_output)
            self.assertIn('test result: ok', tool_output)


if __name__ == '__main__':
    unittest.main()
