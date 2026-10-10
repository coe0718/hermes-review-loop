"""Real native Hermes -> AF_UNIX inference -> scoped broker; local fakes only."""
import _home_guard  # noqa: F401
import _ci_green  # noqa: F401
from contextlib import ExitStack
import http.server
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

from diaktoros import (broker_ipc, gh, inference_proxy, native_macos, native_storage,
                      seatbelt, trusted_turn, turn_layout)
import native_rust_fixture

SOURCE = _home_guard.HERMES_AGENT_SOURCE
HEAD = 'a' * 40


class NativeHermesTurn(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if not reason and (SOURCE is None or not (SOURCE / 'venv/bin/hermes').is_file()):
            reason = 'a disposable Hermes source checkout and venv are required'
        if not reason and any(not os.environ.get(name) for name in
                              ('DIAKTOROS_NATIVE_RUST_ROOT', 'DIAKTOROS_NATIVE_BUILD_FIXTURE',
                               'DIAKTOROS_NATIVE_SDK', 'DIAKTOROS_NATIVE_DEVELOPER_TOOLS')):
            reason = 'dedicated native Rust, vendor and SDK fixtures are required'
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_HERMES') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def test_real_hermes_runs_tool_and_posts_only_scoped_fake_review(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='dk-n-', dir='/tmp'))).resolve()
            workspace = stack.enter_context(native_storage.Workspace(512))
            home, work, scratch = workspace.home, workspace.work, workspace.scratch
            code, export, client = (root / n for n in ('code', 'export', 'client'))
            for directory in (export, client):
                directory.mkdir(mode=0o700)
            # Exercise the same committed, filtered and hash-checked exporter as
            # trusted turns, including its native directory-descriptor pin.
            trusted_turn._safe_code_snapshot(SOURCE, code)
            (client / 'diaktoros').mkdir()
            (client / 'diaktoros/__init__.py').touch()
            for filename in ('broker_client.py', 'wire.py'):
                shutil.copyfile(Path(native_macos.__file__).with_name(filename), client / 'diaktoros' / filename)
            secret = root / 'host-secret'
            secret.write_text('HOST_SECRET_MUST_NOT_REACH_MODEL')
            model_key = 'HOST_MODEL_KEY_FIXTURE'
            (root / 'model-key').write_text(model_key)
            for login in ('reader', 'reviewer', 'fixer'):
                (root / f'{login}.pat').write_text('fixture-' + login)
            loop = {'repo': 'acme/widgets', 'base': 'main', 'state_dir': str(root),
                    'read_token': 'reader', 'tokens': {n: str(root / f'{n}.pat') for n in
                                                       ('reader', 'reviewer', 'fixer')},
                    'seats': {'reviewer': {'login': 'reviewer'}, 'fixer': {'login': 'fixer'}}}
            pr = {'number': 7, 'state': 'open', 'draft': False,
                  'base': {'ref': 'main', 'repo': {'full_name': loop['repo']}},
                  'head': {'sha': HEAD, 'ref': 'fix-7', 'repo': {'full_name': loop['repo']}}}
            writes, requests = [], []
            build_fixture = Path(os.environ['DIAKTOROS_NATIVE_BUILD_FIXTURE'])
            dependencies = build_fixture / 'vendor'
            native_rust_fixture.stage(work, secret=secret, vendor=dependencies)
            shutil.copyfile(build_fixture / 'Cargo.lock', work / 'Cargo.lock')

            def api(_loop, path, method='GET', body=None, login=None):
                if path == '/user':
                    return {'login': login, 'id': {'reader': 1, 'reviewer': 2, 'fixer': 3}[login]}
                if method == 'POST':
                    writes.append((path, body, login))
                    return {'id': 42}
                return pr

            probe = f"""
import os,socket
from pathlib import Path
try:
    Path({str(secret)!r}).read_text()
except PermissionError:
    print('HOST_SECRET_BLOCKED')
else:
    raise AssertionError('secret readable')
with socket.socket() as connection:
    try:
        connection.connect(('127.0.0.1', 9))
    except PermissionError:
        print('HOST_NETWORK_BLOCKED')
    else:
        raise AssertionError('host network reachable')
assert 'GITHUB_TOKEN' not in os.environ
Path('review.txt').write_text('native Python and offline Rust fixture verified\\nNot verified: live services')
"""
            import shlex
            # A host-staged immutable fixture script is an ordinary tool command;
            # do not disable Hermes's unattended command-approval policy for -c.
            probe_file = code / 'native-probe.py'
            probe_file.write_text(probe)
            command = ('python ' + shlex.quote(str(probe_file)) +
                       ' && cargo test --offline --locked --lib && '
                       'python -m diaktoros.broker_client review --verdict APPROVE --body-file review.txt')

            class Model(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append((self.path, self.headers.get('Authorization'), request))
                    done = any(m.get('role') == 'tool' for m in request.get('messages', []))
                    delta = ({'role': 'assistant', 'content': 'NATIVE_TURN_DONE'} if done else
                             {'role': 'assistant', 'content': None, 'tool_calls': [{
                                 'index': 0, 'id': 'native_probe', 'type': 'function',
                                 'function': {'name': 'terminal',
                                              'arguments': json.dumps({'command': command})}}]})
                    chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': 'fixture-model', 'choices': [{'index': 0, 'delta': delta,
                                                                    'finish_reason': None}]}
                    end = {**chunk, 'choices': [{'index': 0, 'delta': {},
                                                'finish_reason': 'stop' if done else 'tool_calls'}]}
                    data = (''.join('data: ' + json.dumps(c) + '\n\n' for c in (chunk, end))
                            + 'data: [DONE]\n\n').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Model)
            thread = threading.Thread(target=upstream.serve_forever)
            thread.start()
            def close_upstream():
                upstream.shutdown()
                upstream.server_close()
                thread.join()
            stack.callback(close_upstream)
            stack.enter_context(mock.patch.object(gh, 'api', side_effect=_ci_green.green(api)))
            scope = broker_ipc.RunScope(loop['repo'], 7, HEAD, 'reviewer', 'fix-7')
            # Short socket paths are separate from all writable/read-root generations.
            capability = stack.enter_context(inference_proxy.InferenceCapability(
                root / 'i', f'http://127.0.0.1:{upstream.server_port}{inference_proxy.PATH}',
                model_key, model='fixture-model', quota=5))
            broker_dir = root / 'b'
            broker_dir.mkdir(mode=0o700)
            broker = stack.enter_context(broker_ipc.RunBroker(loop, scope, broker_dir))
            server = broker_ipc.serve_in_thread(broker)
            def close_broker():
                broker.close()
                server.join(timeout=5)
                self.assertFalse(server.is_alive())
            stack.callback(close_broker)
            query = code / 'native-query.txt'
            venv = SOURCE / 'venv'
            layout = turn_layout.TurnLayout(code=code, venv=venv, home=home, work=work,
                                            export=export, client=client, scratch=scratch, query=query)
            instructions = trusted_turn.tool_instructions('reviewer', layout=layout)
            query.write_text('Execute the requested terminal probe, submit the scoped review, then stop.\n'
                             + instructions)
            runtime = Path((venv / 'bin/python').resolve()).parents[1]
            rust = Path(os.environ['DIAKTOROS_NATIVE_RUST_ROOT'])
            result = native_macos.run(code=code, venv=venv, runtime=runtime,
                                      rust=rust, home=home, work=work, export=export,
                                      client=client, scratch=scratch, query=query,
                                      inference_socket=capability.socket_path,
                                      broker_socket=broker.socket_path, model='fixture-model',
                                      role=scope.role,
                                      workspace=workspace,
                                      sdk=Path(os.environ['DIAKTOROS_NATIVE_SDK']),
                                      developer_tools=Path(os.environ['DIAKTOROS_NATIVE_DEVELOPER_TOOLS']),
                                      dependencies=dependencies)
            outputs = '\n'.join(str(m.get('content')) for _, _, request in requests
                                for m in request.get('messages', []) if m.get('role') == 'tool')
            detail = result.stdout[-6000:] + result.stderr[-6000:] + '\nTOOL OUTPUT:\n' + outputs[-8000:]
            self.assertEqual(result.returncode, 0, detail)
            self.assertTrue(broker.completed, detail)
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][1]['commit_id'], HEAD)
            self.assertEqual(writes[0][2], 'reviewer')
            self.assertGreaterEqual(len(requests), 2)
            user_messages = '\n'.join(str(m.get('content')) for _, _, request in requests
                                      for m in request.get('messages', []) if m.get('role') == 'user')
            self.assertIn(instructions, user_messages)
            self.assertIn('--body-file ' + shlex.quote(str(work / 'review.txt')), instructions)
            self.assertNotIn('--body-file /work/review.txt', instructions)
            self.assertTrue(all(auth == 'Bearer ' + model_key for _, auth, _ in requests))
            self.assertIn('HOST_SECRET_BLOCKED', outputs)
            self.assertIn('HOST_NETWORK_BLOCKED', outputs)
            self.assertIn('RUST_HOST_SECRET_BLOCKED', outputs)
            self.assertIn('RUST_NETWORK_BLOCKED', outputs)
            self.assertIn('RUST_VENDOR_WRITE_BLOCKED', outputs)
            self.assertIn('1 passed; 0 failed', outputs)
            self.assertTrue(any((work / 'target/debug/deps').glob('libmemchr-*.rlib')))
            self.assertNotIn(secret.read_text(), outputs)
            self.assertNotIn(model_key, outputs)


if __name__ == '__main__':
    unittest.main()
