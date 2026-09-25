"""Per-run model capability: host-held key, one Unix socket, fixed upstream.

The caller supplies only OpenAI chat-completion bodies. The upstream URL and key
are constructor arguments on the trusted side, never fields in the RPC request.
This is a transport primitive, not a production supervisor or billing policy.

Upstream contract: one fixed HTTP(S) URL whose path ends in ``/chat/completions`` (for example
``https://api.deepseek.com/v1/chat/completions`` or ``https://openrouter.ai/api/v1/chat/completions``),
with no userinfo, query or fragment, authenticated with ``Authorization: Bearer <key>``. That is
the OpenAI chat-completions wire shape and nothing else: Anthropic Messages, Codex/Responses and
OAuth-subscription endpoints are not reachable through this capability. The sandbox side always
speaks to the fixed local path ``PATH``; the upstream path is chosen by the host only.
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
from urllib.parse import urlsplit

MAX_REQUEST = 1_000_000
MAX_RESPONSE = 4_000_000
MAX_OUTPUT_TOKENS = 4096
MAX_CALLS = 32
MAX_CONNECTIONS = 8
CLIENT_TIMEOUT = 3
PATH = '/v1/chat/completions'
UPSTREAM_SUFFIX = '/chat/completions'

class _BoundedThreads(socketserver.ThreadingMixIn):
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs):
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(CLIENT_TIMEOUT)
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        socketserver.BaseServer.handle_error(self, request, client_address)


class ProxyError(Exception):
    pass


def _bounded_request(body: bytes, model: str) -> bytes:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProxyError('invalid JSON request') from exc
    if not isinstance(payload, dict):
        raise ProxyError('request must be a JSON object')
    if 'max_tokens' in payload and 'max_completion_tokens' in payload:
        raise ProxyError('ambiguous output token limits')
    for field in ('max_tokens', 'max_completion_tokens'):
        if field in payload and (type(payload[field]) is not int or
                                 not 1 <= payload[field] <= MAX_OUTPUT_TOKENS):
            raise ProxyError('invalid output token limit')
    for field in ('n', 'best_of'):
        if field in payload and (type(payload[field]) is not int or payload[field] != 1):
            raise ProxyError('multiple completions are not permitted')
    payload['model'] = model
    if 'max_tokens' not in payload and 'max_completion_tokens' not in payload:
        payload['max_tokens'] = MAX_OUTPUT_TOKENS
    return json.dumps(payload).encode('utf-8')


class _NoRedirectConnection:
    """Open a fresh connection to the one trusted endpoint per request."""

    def __init__(self, upstream: str):
        url = urlsplit(upstream)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or
                url.password or url.query or url.fragment or
                not url.path.endswith(UPSTREAM_SUFFIX) or '//' in url.path or
                any(part in ('.', '..') for part in url.path.split('/')) or
                any(ord(char) < 33 or ord(char) == 127 for char in url.path)):
            raise ValueError('upstream must be a fixed chat-completions URL')
        self.url = url

    def post(self, body: bytes, key: str) -> tuple[int, str, bytes]:
        cls = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        conn = cls(self.url.hostname or '', self.url.port, timeout=30)
        try:
            conn.request('POST', self.url.path, body=body, headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key,
                'Accept': 'text/event-stream, application/json'})
            response = conn.getresponse()
            # Do not follow redirects or pass their Location headers to the caller.
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise ProxyError('upstream response too large')
            content_type = response.getheader('Content-Type', 'application/json')
            if not (content_type.startswith('application/json') or
                    content_type.startswith('text/event-stream')):
                raise ProxyError('invalid upstream content type')
            return response.status, content_type, data
        finally:
            conn.close()


class InferenceCapability:
    def __init__(self, socket_dir: Path, upstream: str, key: str, *, model: str, quota: int = 8):
        if (not key or '\r' in key or '\n' in key or not isinstance(model, str) or
                not model or type(quota) is not int or not 1 <= quota <= MAX_CALLS):
            raise ValueError('invalid key, model or quota')
        self.endpoint = _NoRedirectConnection(upstream)
        self.key = key
        self.model = model
        self.quota = quota
        self.used = 0
        self.lock = threading.Lock()
        self.directory = Path(socket_dir)
        self.socket_path = self.directory / 'model.sock'
        self.server = None
        self.thread = None

    def __enter__(self):
        self.directory.mkdir(mode=0o700)
        os.chmod(self.directory, 0o700)
        if self.socket_path.exists():
            raise FileExistsError(self.socket_path)
        capability = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = self.headers.get('Content-Length', '')
                if (self.path != PATH or self.headers.get('Transfer-Encoding') or
                        not length.isdecimal() or not 0 < int(length) <= MAX_REQUEST):
                    self.send_error(400)
                    return
                body = self.rfile.read(int(length))
                if len(body) != int(length):
                    self.send_error(400)
                    return
                try:
                    body = _bounded_request(body, capability.model)
                except ProxyError:
                    self.send_error(400)
                    return
                # Reserve quota before contacting provider, including failed requests.
                with capability.lock:
                    if capability.used >= capability.quota:
                        self.send_error(429)
                        return
                    capability.used += 1
                try:
                    status, content_type, data = capability.endpoint.post(body, capability.key)
                except (OSError, ProxyError, http.client.HTTPException):
                    self.send_error(502)
                    return
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send_error(405)

        class Server(_BoundedThreads, socketserver.UnixStreamServer):
            pass

        self.server = Server(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.socket_path.unlink(missing_ok=True)
        # Do not retain the key beyond the capability lifetime.
        self.key = ''


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__('localhost', timeout=35)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def bridge(entry: list[str], socket_path: str = '/opt/inference/model.sock') -> int:
    """Run localhost HTTP and the real Hermes CLI in the same sandbox process tree."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            length = self.headers.get('Content-Length', '')
            if (self.path != PATH or self.headers.get('Transfer-Encoding') or
                    not length.isdecimal() or not 0 < int(length) <= MAX_REQUEST):
                self.send_error(400)
                return
            body = self.rfile.read(int(length))
            conn = _UnixHTTP(socket_path)
            try:
                # Never forward Hermes's dummy Authorization or caller-selected headers.
                conn.request('POST', PATH, body=body, headers={'Content-Type': 'application/json'})
                response = conn.getresponse()
                data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise ProxyError('response too large')
                self.send_response(response.status)
                self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (OSError, ProxyError, http.client.HTTPException):
                self.send_error(502)
            finally:
                conn.close()

    class Server(_BoundedThreads, http.server.HTTPServer):
        pass

    server = Server(('127.0.0.1', 18761), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return subprocess.call(entry)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3 or sys.argv[1] != 'bridge' or sys.argv[2] != '--':
        raise SystemExit('usage: python -m review_loop.inference_proxy bridge -- COMMAND...')
    raise SystemExit(bridge(sys.argv[3:]))
