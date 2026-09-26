"""One offline route -> real worker -> exact export -> bwrap Hermes -> broker -> ledger turn.

The ONLY fake transport is sitecustomize in a disposable copy of the package.
Production _spawn strips the ambient GH stub; production modules are copied unchanged.
No real GitHub, provider, user HOME, or token is used.
"""
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.test_turn_vertical import SOURCE, RUST
from review_loop.inference_proxy import PATH

ROOT = Path(__file__).resolve().parents[1]
HEAD = 'a' * 40


class Fixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status, data, content_type='application/json'):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        world = self.server.world
        if self.path == '/user':
            auth = self.headers.get('Authorization', '')
            login = auth.split()[-1].removeprefix('dummy-')
            self._send(200, {'login': login, 'id': {'reader': 1, 'reviewer': 2, 'fixer': 3}.get(login)})
        elif self.path == '/repos/acme/widgets/pulls/7':
            self._send(200, world['pr'])
        elif self.path == '/repos/acme/widgets/git/commits/' + HEAD:
            if world.get('move_during_export'):
                world['pr']['head']['sha'] = 'c' * 40
            self._send(200, {'sha': HEAD, 'tree': {'sha': world['tree_sha']}})
        elif self.path == '/repos/acme/widgets/git/trees/' + world['tree_sha'] + '?recursive=1':
            self._send(200, {'sha': world['tree_sha'], 'truncated': False,
                             'tree': [{'path': name, 'type': 'blob', 'mode': '100644',
                                       'sha': world['blobs'][name][0], 'size': len(world['blobs'][name][1])}
                                      for name in world['blobs']]})
        elif self.path.startswith('/repos/acme/widgets/git/blobs/'):
            oid = self.path.rsplit('/', 1)[-1]
            blob = next((raw for blob_sha, raw in world['blobs'].values() if blob_sha == oid), None)
            self._send(200 if blob is not None else 404, blob if blob is not None else b'')
        elif self.path == '/repos/acme/widgets/pulls/7/reviews?per_page=100':
            self._send(200, world['writes'])
        elif self.path.startswith('/repos/acme/widgets/pulls/7/reviews/'):
            rid = int(self.path.rsplit('/', 1)[-1])
            self._send(200, world['reviews_by_id'].get(rid, {}))
        else:
            self._send(404, {})

    def do_POST(self):
        world = self.server.world
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == PATH:
            world['model'].append((self.headers.get('Authorization'), data))
            if any(m.get('role') == 'tool' for m in data.get('messages', [])):
                delta, finish = {'role': 'assistant', 'content': 'Finished the scoped review.'}, 'stop'
            else:
                cmd = world.get('model_command') or (
                    'cat ' + str(Path.home() / '.hermes/.env') + '; cat ' + str(world['key_path']) +
                    ' ' + str(world['pat_path']) +
                    '; cargo test --offline; python -m review_loop.broker_client review '
                    '--verdict APPROVE --body-file /work/review.txt')
                delta, finish = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'probe', 'type': 'function', 'function': {
                        'name': 'terminal', 'arguments': json.dumps({'command': cmd})}}]}, 'tool_calls'
            chunk = {'id': 'offline', 'object': 'chat.completion.chunk', 'created': 1,
                     'model': 'fixture-model', 'choices': [{'index': 0, 'delta': delta,
                                                           'finish_reason': None}]}
            end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
            raw = (''.join('data: ' + json.dumps(x) + '\n\n' for x in (chunk, end)) +
                   'data: [DONE]\n\n').encode()
            self._send(200, raw, 'text/event-stream')
        elif self.path == '/repos/acme/widgets/pulls/7/reviews':
            world['writes'].append(data)
            world['write_auth'].append(self.headers.get('Authorization'))
            rid = len(world['writes'])
            world['reviews_by_id'][rid] = {'id': rid,
                'state': {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED',
                          'COMMENT': 'COMMENTED'}[data['event']],
                'commit_id': data['commit_id'], 'user': {'id': 2, 'login': 'reviewer'}}
            self._send(200, {'id': rid, 'state': data.get('event')})
        else:
            self._send(404, {})


@unittest.skipUnless(shutil.which('bwrap') and (SOURCE / 'venv/bin/hermes').exists()
                     and (RUST / 'bin/cargo').exists(), 'offline sandbox prerequisites absent')
class RouteWorkerVertical(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        (self.home / 'review-loops.d').mkdir()
        package = self.root / 'fixture-source'
        package.mkdir()
        shutil.copytree(ROOT / 'review_loop', package / 'review_loop',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'scripts', package / 'scripts')
        # This file exists ONLY inside this disposable test package; production has no
        # environment-controlled fake API, no test transport, and no worker GH stub.
        (package / 'sitecustomize.py').write_text(
            'import http.client, os\n'
            'from review_loop import gh\n'
            "gh.API = 'http://127.0.0.1:' + open(os.path.join(os.environ['HOME'], 'offline-port')).read()\n"
            'http.client.HTTPSConnection = http.client.HTTPConnection\n')
        self.package = package
        self.key = self.root / 'model.key'
        self.key.write_text('HOST_ONLY_MODEL_SENTINEL')
        self.key.chmod(0o600)
        tokens = {}
        for login in ('reader', 'reviewer', 'fixer'):
            path = self.root / (login + '.pat')
            path.write_text('dummy-' + login)
            path.chmod(0o600)
            tokens[login] = str(path)
        loop = {'id': 'widgets', 'repo': 'acme/widgets', 'base': 'main', 'cap': 3,
                'fixers': ['dev'], 'reviewers': ['reviewer'], 'reviewer_seat': 'reviewer',
                'seats': {'reviewer': {'profile': 'fixture', 'route': 'review', 'login': 'reviewer'},
                          'fixer': {'profile': 'fixture', 'route': 'fix', 'login': 'fixer'}},
                'read_token': 'reader', 'tokens': tokens, 'state_dir': str(self.home / 'state')}
        (self.home / 'review-loops.d/widgets.json').write_text(json.dumps(loop))
        venv = SOURCE / 'venv'
        runtime = Path(os.readlink(venv / 'bin/python')).parents[2]
        self.settings = {'source': str(SOURCE), 'venv': str(venv), 'runtime': str(runtime),
                         'rust': str(RUST), 'upstream': '', 'key_file': str(self.key),
                         'model': 'fixture-model'}
        blobs = {
            'Cargo.toml': b'[package]\nname="vertical_probe"\nversion="0.1.0"\nedition="2021"\n',
            'src/lib.rs': b'#[test] fn works() { assert_eq!(2 + 2, 4); }\n',
            'review.txt': b'offline verified',
        }
        self.world = {'pr': {'number': 7, 'state': 'open', 'draft': False,
                             'user': {'login': 'dev'},
                             'base': {'ref': 'main', 'sha': 'b' * 40,
                                      'repo': {'full_name': 'acme/widgets'}},
                             'head': {'sha': HEAD, 'ref': 'fix-7',
                                      'repo': {'full_name': 'acme/widgets'}}},
                      'tree_sha': 'b' * 40, 'blobs': {
                          name: (hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(), raw)
                          for name, raw in blobs.items()},
                      'model': [], 'writes': [], 'reviews_by_id': {}, 'write_auth': [], 'key_path': self.key,
                      'pat_path': tokens['reviewer']}
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        self.server.world = self.world
        (self.home / 'offline-port').write_text(str(self.server.server_port))
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.settings['upstream'] = f'https://127.0.0.1:{self.server.server_port}{PATH}'
        settings = self.home / 'review-loop-runtime.json'
        settings.write_text(json.dumps(self.settings))
        settings.chmod(0o600)
        stub = self.root / 'gate-gh-stub.py'
        stub.write_text('#!/usr/bin/python3\nimport json, os, sys\n'
                        "world=json.load(open(os.environ['GATE_WORLD']))\n"
                        "print(json.dumps([] if '/reviews' in sys.argv[1] else world['pr']))\n")
        stub.chmod(0o700)
        self.env = {'HOME': str(self.home), 'HERMES_HOME': str(self.home),
                    'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(package),
                    'OFFLINE_GITHUB_PORT': str(self.server.server_port),
                    'REVIEW_LOOP_GH_STUB': str(stub), 'GATE_WORLD': str(self.root / 'gate-world.json'),
                    'TMPDIR': tempfile.gettempdir(), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.payload = {'repository': {'full_name': 'acme/widgets'}, 'action': 'opened',
                        'number': 7, 'pull_request': self.world['pr'], 'sender': {'login': 'dev'}}

    def route(self):
        Path(self.env['GATE_WORLD']).write_text(json.dumps({'pr': self.world['pr']}))
        return subprocess.run([sys.executable, str(self.package / 'scripts/gate_reviewer.py')],
                              input=json.dumps(self.payload), capture_output=True, text=True,
                              env=self.env, cwd=self.root, timeout=15)

    def result(self, expected, seconds=180):
        db = self.home / 'state/review-loop-runs.sqlite'
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if db.exists():
                with sqlite3.connect(db) as con:
                    rows = con.execute('SELECT state, attempts, outcome, error FROM runs').fetchall()
                if rows and rows[0][0] in ('succeeded', 'failed', 'uncertain'):
                    break
            time.sleep(0.1)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][0], expected, rows)
        return rows[0]

    def test_route_to_completed_scoped_review(self):
        first = self.route()
        self.assertEqual((first.returncode, first.stdout.strip()), (0, '[SILENT]'), first.stderr)
        row = self.result('succeeded')
        self.assertEqual(row[:3], ('succeeded', 1, 0))
        self.assertIsNone(row[3])
        self.assertEqual(len(self.world['writes']), 1)
        self.assertEqual(self.world['writes'][0]['commit_id'], HEAD)
        self.assertEqual(self.world['write_auth'], ['token dummy-reviewer'])
        with sqlite3.connect(self.home / 'state/review-loop-runs.sqlite') as con:
            receipt = con.execute('SELECT state,review_id,verdict,principal_id,generation '
                                  'FROM review_receipts').fetchone()
        self.assertEqual(receipt[:4], ('confirmed', 1, 'APPROVED', 2))
        self.assertEqual(json.loads(receipt[4]), {'head': HEAD, 'base_ref': 'main',
                                                   'base_sha': 'b' * 40, 'parents': [],
                                                   'parent_chain_verified': False})
        self.assertGreaterEqual(len(self.world['model']), 2)
        self.assertTrue(all(auth == 'Bearer HOST_ONLY_MODEL_SENTINEL'
                            for auth, _ in self.world['model']))
        output = '\n'.join(str(m.get('content')) for _, request in self.world['model']
                           for m in request.get('messages', []) if m.get('role') == 'tool')
        self.assertIn('test result: ok', output)
        self.assertIn('"ok": true', output.replace('\\"', '"'))
        self.assertNotIn('HOST_ONLY_MODEL_SENTINEL', output)
        self.assertNotIn('dummy-reviewer', output)
        self.assertIn('No such file or directory', output)
        duplicate = self.route()
        self.assertEqual((duplicate.returncode, duplicate.stdout.strip()),
                         (0, '[SILENT]'), duplicate.stderr)
        self.assertEqual(len(self.world['writes']), 1)

    def test_stale_head_fails_before_export_or_write(self):
        self.world['pr']['head']['sha'] = 'c' * 40
        result = self.route()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, '[SILENT]'), result.stderr)
        row = self.result('failed')
        self.assertEqual(row[1], 1)
        self.assertEqual(self.world['model'], [])
        self.assertEqual(self.world['writes'], [])

    def test_head_moves_during_export_fails_without_agent_or_write(self):
        self.world['move_during_export'] = True
        route = self.route()
        self.assertEqual((route.returncode, route.stdout.strip()), (0, '[SILENT]'), route.stderr)
        self.result('failed')
        self.assertEqual(self.world['model'], [])
        self.assertEqual(self.world['writes'], [])

    def test_out_of_scope_agent_write_fails_and_is_never_written(self):
        self.world['model_command'] = 'python -m review_loop.broker_client request_review'
        route = self.route()
        self.assertEqual((route.returncode, route.stdout.strip()), (0, '[SILENT]'), route.stderr)
        row = self.result('failed')
        self.assertEqual(row[1], 1)
        self.assertEqual(self.world['writes'], [])
        # A redelivery re-arms the failed pre-write turn (#53): it runs once more, is denied
        # the same way, and still writes nothing.
        again = self.route()
        self.assertEqual((again.returncode, again.stdout.strip()), (0, '[SILENT]'), again.stderr)
        self.assertEqual(self.result('failed')[1], 1)
        self.assertEqual(self.world['writes'], [])


if __name__ == '__main__':
    unittest.main()
