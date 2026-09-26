"""Real UDS mount, host-only dummy key and whole-process Hermes turn."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from review_loop import contained
from review_loop import inference_proxy
from review_loop.inference_proxy import InferenceCapability, _UnixHTTP, PATH, MAX_OUTPUT_TOKENS

SOURCE = _home_guard.HERMES_AGENT_SOURCE


def live_threads(server):
    """The server's request threads; before the first request socketserver holds a
    non-iterable placeholder instead of a list, which simply means none yet."""
    threads = server._threads
    return list(threads) if isinstance(threads, list) else []


class TransportTests(unittest.TestCase):
    def test_slow_incomplete_clients_cannot_spawn_threads_before_quota(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as d:
            with mock.patch.object(inference_proxy, 'CLIENT_TIMEOUT', 0.3):
                with InferenceCapability(Path(d) / 'cap', f'http://localhost{PATH}',
                                         'DUMMY_KEY', model='fixed', quota=1) as cap:
                    clients = []
                    try:
                        for _ in range(80):
                            client = socket.socket(socket.AF_UNIX)
                            client.settimeout(0.1)
                            try:
                                client.connect(str(cap.socket_path))
                                client.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 50\r\n\r\n{')
                                clients.append(client)
                            except (OSError, TimeoutError):
                                client.close()
                        self.assertEqual(cap.used, 0)
                        self.assertGreaterEqual(cap.server._connections._value, 0)
                        self.assertLessEqual(sum(t.is_alive() for t in live_threads(cap.server)),
                                             inference_proxy.MAX_CONNECTIONS)
                    finally:
                        for client in clients:
                            client.close()

    def test_adversarial_payloads_cannot_change_model_or_expand_budget(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as d:
            seen = []

            class Upstream(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                    data = b'{}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            thread = threading.Thread(target=upstream.serve_forever)
            thread.start()
            try:
                with InferenceCapability(Path(d) / 'cap',
                        f'http://127.0.0.1:{upstream.server_port}{PATH}',
                        'DUMMY_KEY', model='fixed-model', quota=2) as cap:
                    def send(payload):
                        conn = _UnixHTTP(str(cap.socket_path))
                        conn.request('POST', PATH, body=json.dumps(payload).encode())
                        response = conn.getresponse()
                        status = response.status
                        response.read()
                        conn.close()
                        return status

                    for payload in ([], None, {'max_tokens': MAX_OUTPUT_TOKENS + 1},
                                    {'max_completion_tokens': MAX_OUTPUT_TOKENS + 1},
                                    {'max_tokens': True}, {'max_completion_tokens': '999999'},
                                    {'max_tokens': 0}, {'n': 2}, {'n': True},
                                    {'best_of': 2}, {'max_tokens': 5,
                                                    'max_completion_tokens': 5}):
                        with self.subTest(payload=payload):
                            self.assertEqual(send(payload), 400)
                    self.assertEqual(cap.used, 0)
                    self.assertEqual(send({'model': 'attacker-model', 'messages': [], 'n': 1}), 200)
                    self.assertEqual(send({'model': 'other', 'max_completion_tokens': 12}), 200)
                    self.assertEqual(send({'model': 'other'}), 429)
                    self.assertEqual(seen, [
                        {'model': 'fixed-model', 'messages': [], 'n': 1,
                         'max_tokens': MAX_OUTPUT_TOKENS},
                        {'model': 'fixed-model', 'max_completion_tokens': 12}])
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join()

    def test_rejects_unbounded_capability_settings(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as d:
            for options in ({'model': ''}, {'model': 'fixed', 'quota': 1000000},
                            {'model': 'fixed', 'quota': True}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    InferenceCapability(Path(d) / 'cap', f'http://localhost{PATH}',
                                        'DUMMY_KEY', **options)

    def test_policy_and_quota(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as d:
            root = Path(d)
            seen = []

            class Upstream(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    seen.append((self.path, self.headers.get('Authorization'), self.rfile.read(int(self.headers['Content-Length']))))
                    data = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            thread = threading.Thread(target=upstream.serve_forever)
            thread.start()
            try:
                endpoint = f'http://127.0.0.1:{upstream.server_port}{PATH}'
                with InferenceCapability(root / 'cap', endpoint, 'HOST_ONLY_KEY', model='fixture-model', quota=1) as cap:
                    def send(path, body=b'{}', headers=None):
                        conn = _UnixHTTP(str(cap.socket_path))
                        try:
                            conn.request('POST', path, body=body, headers=headers or {})
                            response = conn.getresponse()
                        except (BrokenPipeError, ConnectionResetError):
                            # A rejected request may be answered and closed before its body
                            # is sent; the answer is still waiting on the socket.
                            response = http.client.HTTPResponse(conn.sock)
                            response.begin()
                        result = (response.status, response.read())
                        conn.close()
                        return result

                    self.assertEqual(send('/other')[0], 400)
                    self.assertEqual(send(PATH, b'{}', {'Authorization': 'Bearer attacker'})[0], 200)
                    self.assertEqual(send(PATH)[0], 429)
                    self.assertEqual(seen, [(PATH, 'Bearer HOST_ONLY_KEY',
                        json.dumps({'model': 'fixture-model', 'max_tokens': MAX_OUTPUT_TOKENS}).encode())])
                    self.assertNotIn(b'HOST_ONLY_KEY', (root / 'cap').read_bytes() if (root / 'cap').is_file() else b'')
                self.assertFalse(cap.socket_path.exists())
                for bad in ('http://127.0.0.1:123/else', 'http://user@127.0.0.1/v1/chat/completions'):
                    with self.assertRaises(ValueError):
                        InferenceCapability(root / 'bad', bad, 'key', model='fixture-model')
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join()

    @unittest.skipUnless(shutil.which('bwrap') and (SOURCE / 'venv/bin/hermes').exists(),
                         'bubblewrap or Hermes checkout unavailable')
    def test_real_hermes_via_host_capability(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as d:
            root = Path(d)
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
            module = code / 'review_loop/inference_proxy.py'
            module.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(__file__).parents[1] / 'review_loop/inference_proxy.py', module)
            home = root / 'home'
            home.mkdir()
            (home / 'config.yaml').write_text('''model:
  provider: custom
  default: fixture-model
  base_url: http://127.0.0.1:18761/v1
  api_key: unused-public-placeholder
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
            keyfile = root / 'host-dummy.model-key'
            keyfile.write_text('HOST_DUMMY_MODEL_KEY_SENTINEL')
            requests = []

            class Upstream(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append((self.path, self.headers.get('Authorization'), body))
                    has_result = any(m.get('role') == 'tool' for m in body.get('messages', []))
                    if has_result:
                        message = {'role': 'assistant', 'content': 'FIXTURE_DONE'}
                        finish = 'stop'
                    else:
                        command = ('cat ' + str(pat) + ' ' + str(keyfile) +
                                   ' ' + str(_home_guard.USER_HOME / '.hermes/.env') + '; git credential fill </dev/null; cargo test --offline')
                        message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                            'id': 'call_host_read', 'type': 'function', 'function': {
                                'name': 'terminal', 'arguments': json.dumps({'command': command})}},
                            {'id': 'call_file_read', 'type': 'function', 'function': {
                                'name': 'read_file', 'arguments': json.dumps({'path': str(keyfile)})}}]}
                        finish = 'tool_calls'
                    delta = {'role': 'assistant', 'content': message.get('content')}
                    if 'tool_calls' in message:
                        delta['tool_calls'] = [dict(index=i, id=call['id'], type='function', function=call['function'])
                                               for i, call in enumerate(message['tool_calls'])]
                    chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': 'fixture-model', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
                    end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
                    data = (''.join('data: ' + json.dumps(part) + '\n\n' for part in (chunk, end)) +
                            'data: [DONE]\n\n').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            thread = threading.Thread(target=upstream.serve_forever)
            thread.start()
            try:
                venv = SOURCE / 'venv'
                runtime = Path(os.readlink(venv / 'bin/python')).parents[2]
                rust = _home_guard.RUST
                if not (rust / 'bin/cargo').exists():
                    self.skipTest('offline stable Rust toolchain unavailable')
                with InferenceCapability(root / 'cap',
                                         f'http://127.0.0.1:{upstream.server_port}{PATH}',
                                         'HOST_DUMMY_MODEL_KEY_SENTINEL', model='fixture-model', quota=3) as cap:
                    result = contained.run(code=code, venv=venv, runtime=runtime,
                                           home=home, checkout=checkout, rust=rust,
                                           query=home / 'query.txt', inference_socket_dir=cap.directory,
                                           entry=['/opt/venv/bin/python', '-m', 'review_loop.inference_proxy', 'bridge', '--',
                                                  '/opt/venv/bin/python', '/opt/venv/bin/hermes', 'chat', '--query-file', '/home/agent/query.txt',
                                                  '--oneshot', '-Q', '--provider', 'custom', '-m', 'fixture-model',
                                                  '-t', 'terminal,file', '--ignore-rules', '--max-turns', '3',
                                                  '--run-budget', '90'], timeout=180)
                    used = cap.used
                self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
                self.assertIn('FIXTURE_DONE', result.stdout)
                self.assertGreaterEqual(used, 2)
                self.assertTrue(all(row[0] == PATH and row[1] == 'Bearer HOST_DUMMY_MODEL_KEY_SENTINEL'
                                    for row in requests))
                self.assertTrue(all(body['model'] == 'fixture-model' and
                                    1 <= body.get('max_tokens', body.get('max_completion_tokens', 0)) <= MAX_OUTPUT_TOKENS
                                    for _, _, body in requests))
                tool_messages = [m for _, _, body in requests for m in body.get('messages', [])
                                 if m.get('role') == 'tool']
                tool_output = '\n'.join(str(m.get('content', '')) for m in tool_messages)
                self.assertTrue({'call_host_read', 'call_file_read'}.issubset(
                    {m.get('tool_call_id') for m in tool_messages}), tool_output)
                for secret in (pat, keyfile):
                    self.assertIn(str(secret), tool_output)
                self.assertIn('No such file or directory', tool_output)
                self.assertIn('test result: ok', tool_output)
                self.assertNotIn('HOST_DUMMY_PAT_SENTINEL', tool_output)
                self.assertNotIn('HOST_DUMMY_MODEL_KEY_SENTINEL', tool_output)
                print('TOOL_OUTPUT_EXCERPT', json.dumps({
                    'terminal': str(next(m['content'] for m in tool_messages
                                         if m.get('tool_call_id') == 'call_host_read'))[-1100:],
                    'file': str(next(m['content'] for m in tool_messages
                                     if m.get('tool_call_id') == 'call_file_read'))[-350:]}))
                print('INFERENCE_PROOF', json.dumps({'requests': used, 'completion': 'FIXTURE_DONE',
                    'host_paths_denied': True, 'offline_rust': True, 'host_key_only_upstream': True}))
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
