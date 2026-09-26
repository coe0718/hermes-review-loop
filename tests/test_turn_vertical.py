"""Offline host-capability -> bwrap -> real Hermes turn, independent of route wiring.

The route subprocess is tested separately; this is not proof of a route-to-worker
link. No real token, GitHub endpoint, model endpoint, or credential HOME is used.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import http.server
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from review_loop import contained, gh, review_receipt, trusted_fetch, trusted_turn
from review_loop.broker_ipc import RunScope
from review_loop.run_supervisor import Supervisor
from review_loop.inference_proxy import PATH

SOURCE = _home_guard.HERMES_AGENT_SOURCE
RUST = _home_guard.RUST
HEAD = 'a' * 40


@_home_guard.needs_real_hermes(bool(shutil.which('bwrap')), (RUST / 'bin/cargo').exists(),
                               reason='offline sandbox prerequisites absent')
class WholeTurn(unittest.TestCase):
    def test_real_agent_host_only_broker_and_model_key_with_rust(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            home = root / 'outer-home'
            home.mkdir()
            host_pat = root / 'seat.pat'
            host_pat.write_text('HOST_ONLY_PAT_SENTINEL')
            key_path = root / 'model.key'
            key_path.write_text('HOST_ONLY_MODEL_SENTINEL')
            for name in ('reader', 'reviewer', 'fixer'):
                (root / (name + '.pat')).write_text('dummy-' + name)
            loop = {'repo': 'acme/widgets', 'base': 'main', 'state_dir': str(root),
                    'read_token': 'reader', 'tokens': {name: str(root / (name + '.pat'))
                                                        for name in ('reader', 'reviewer', 'fixer')},
                    'seats': {'reviewer': {'login': 'reviewer'}, 'fixer': {'login': 'fixer'}}}
            pr = {'number': 7, 'state': 'open', 'draft': False,
                  'base': {'ref': 'main', 'sha': 'b' * 40, 'repo': {'full_name': loop['repo']}},
                  'head': {'sha': HEAD, 'ref': 'fix-7', 'repo': {'full_name': loop['repo']}}}
            sup = Supervisor(root / 'runs.sqlite')
            sup.enqueue('turn', loop['repo'], 7, HEAD, 'reviewer')
            generation = review_receipt.generation_for(pr, loop, 7, HEAD)
            with sqlite3.connect(sup.db) as con:
                con.execute("UPDATE runs SET state='running',generation=?", (generation,))
                run_id = con.execute('SELECT id FROM runs').fetchone()[0]
            scope = RunScope(loop['repo'], 7, HEAD, 'reviewer', 'fix-7',
                             run_id, str(sup.db), generation)
            writes, requests = [], []
            def api(_loop, path, method='GET', body=None, login=None):
                if path == '/user':
                    assert login is not None
                    return {'login': login, 'id': {'reader': 1, 'reviewer': 2, 'fixer': 3}[login]}
                if method == 'POST':
                    writes.append((path, body, login))
                    return {'id': 42}
                if path.endswith('/reviews/42'):
                    return {'id': 42, 'state': 'APPROVED', 'commit_id': HEAD,
                            'user': {'id': 2, 'login': 'reviewer'}}
                return pr

            class Model(http.server.BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass

                def do_POST(self):
                    self_outer = self
                    request = json.loads(self_outer.rfile.read(int(self_outer.headers['Content-Length'])))
                    requests.append((self_outer.path, self_outer.headers.get('Authorization'), request))
                    has_tool = any(message.get('role') == 'tool' for message in request.get('messages', []))
                    if has_tool:
                        message = {'role': 'assistant', 'content': 'TURN_DONE'}
                        finish = 'stop'
                    else:
                        command = ('cat ' + str(host_pat) + ' ' + str(key_path) +
                                   ' ' + str(_home_guard.USER_HOME / '.hermes/.env') + '; '
                                   'git credential fill </dev/null; cargo test --offline; '
                                   'python -m review_loop.broker_client review --verdict APPROVE '
                                   '--body-file /work/review.txt')
                        message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                            'id': 'probe', 'type': 'function', 'function': {
                                'name': 'terminal', 'arguments': json.dumps({'command': command})}}]}
                        finish = 'tool_calls'
                    delta = {'role': 'assistant', 'content': message.get('content')}
                    if 'tool_calls' in message:
                        delta['tool_calls'] = [dict(index=i, id=c['id'], type='function', function=c['function'])
                                               for i, c in enumerate(message.get('tool_calls', []))]
                    chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': 'fixture-model', 'choices': [{'index': 0, 'delta': delta,
                                                                    'finish_reason': None}]}
                    end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
                    data = (''.join('data: ' + json.dumps(c) + '\n\n' for c in (chunk, end))
                            + 'data: [DONE]\n\n').encode()
                    self_outer.send_response(200)
                    self_outer.send_header('Content-Type', 'text/event-stream')
                    self_outer.send_header('Content-Length', str(len(data)))
                    self_outer.end_headers()
                    self_outer.wfile.write(data)

            upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Model)
            thread = threading.Thread(target=upstream.serve_forever)
            thread.start()
            venv = SOURCE / 'venv'
            runtime = Path(os.readlink(venv / 'bin/python')).parents[2]
            def stage(_loop, **kw):
                checkout = kw['sandbox_root']
                (checkout / 'src').mkdir(parents=True)
                (checkout / 'Cargo.toml').write_text('[package]\nname="vertical_probe"\nversion="0.1.0"\nedition="2021"\n')
                (checkout / 'src/lib.rs').write_text('#[test] fn works() { assert_eq!(2 + 2, 4); }\n')
                (checkout / 'review.txt').write_text('offline verified')
                return checkout
            # Existing contained.run discards HOME/HERMES_HOME. Supply both at
            # the outer bwrap subprocess until the launcher is fixed upstream.
            def isolated_run(**kw):
                command = contained.command(**{k: v for k, v in kw.items() if k != 'timeout'})
                return subprocess.run(command, env={'PATH': '/usr/sbin:/usr/bin:/bin',
                                      'HOME': str(home), 'HERMES_HOME': str(home)},
                                      text=True, capture_output=True, timeout=kw['timeout'])
            try:
                with mock.patch.object(trusted_fetch, 'stage', side_effect=stage), \
                     mock.patch.object(gh, 'api', side_effect=api), \
                     mock.patch.object(contained, 'run', side_effect=isolated_run):
                    rc = trusted_turn.run_turn(loop, scope, source=SOURCE, venv=venv,
                         runtime=runtime, rust=RUST,
                         upstream=f'http://127.0.0.1:{upstream.server_port}{PATH}',
                         key=key_path.read_text(), model='fixture-model',
                         prompt='Run offline Rust tests and submit the scoped review.',
                         timeout=180, work_root=root)
                self.assertEqual(rc, 0)
                self.assertGreaterEqual(len(requests), 2)
                self.assertTrue(all(path == PATH and auth == 'Bearer HOST_ONLY_MODEL_SENTINEL'
                                    for path, auth, _ in requests))
                outputs = '\n'.join(str(m.get('content')) for _, _, req in requests
                                    for m in req.get('messages', []) if m.get('role') == 'tool')
                self.assertIn('test result: ok', outputs)
                self.assertIn('\\"ok\\": true', outputs)
                self.assertIn('No such file or directory', outputs)
                self.assertNotIn('HOST_ONLY_PAT_SENTINEL', outputs)
                self.assertNotIn('HOST_ONLY_MODEL_SENTINEL', outputs)
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0][1]['commit_id'], HEAD)
                self.assertEqual(writes[0][2], 'reviewer')
                self.assertEqual(len((root / 'broker-audit.jsonl').read_text().splitlines()), 1)
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
