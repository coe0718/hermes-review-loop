"""Credentialless CLI for the one-run broker socket (copy into staged code)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket

SOCKET = '/run/review-loop/broker/broker.sock'
MAX_FRAME = 196 * 1024
# A push or review is several GitHub calls plus git fetch/push (each up to 90s), all
# host-side. Wait for the answer instead of timing out mid-write; the turn deadline is
# the real bound.
WRITE_TIMEOUT = 900


def call(operation: str, *, verdict: str = '', body: str = '', manifest=None,
         socket_path: str | None = None) -> dict:
    if operation == 'push':
        payload = {'operation': 'push', 'manifest': manifest}
    elif operation in ('review', 'request_review', 'ruling'):
        payload = {'operation': operation, 'verdict': verdict, 'body': body}
    else:
        raise ValueError('unsupported operation')
    frame = json.dumps(payload, separators=(',', ':')).encode() + b'\n'
    if len(frame) > MAX_FRAME:
        raise ValueError('request too large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(WRITE_TIMEOUT)
        conn.connect(socket_path or SOCKET)
        conn.sendall(frame)
        response = bytearray()
        while b'\n' not in response and len(response) < 16384:
            chunk = conn.recv(4096)
            if not chunk:
                raise ValueError('broker disconnected')
            response.extend(chunk)
    line, _, extra = response.partition(b'\n')
    if extra:
        raise ValueError('invalid broker response')
    result = json.loads(line)
    if not isinstance(result, dict) or type(result.get('ok')) is not bool:
        raise ValueError('invalid broker response')
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('review', 'request_review', 'push', 'ruling'))
    parser.add_argument('--verdict', default='')
    parser.add_argument('--body-file')
    parser.add_argument('--manifest-file')
    args = parser.parse_args()
    if args.operation == 'push':
        if not args.manifest_file or args.body_file or args.verdict:
            parser.error('push requires only --manifest-file')
        path = Path(args.manifest_file)
        if path.stat().st_size > MAX_FRAME:
            parser.error('manifest too large')
        operation = lambda: call('push', manifest=json.loads(path.read_text()))
    else:
        if args.operation == 'ruling' and (args.verdict not in ('ACCEPT', 'REJECT', 'RESPEC')
                                           or not args.body_file or args.manifest_file):
            parser.error('ruling requires --verdict ACCEPT|REJECT|RESPEC and --body-file')
        if args.manifest_file or (args.operation == 'review' and not args.body_file):
            parser.error('invalid review arguments')
        body = Path(args.body_file).read_text() if args.body_file else ''
        if len(body.encode()) > 12 * 1024:
            parser.error('body too large')
        operation = lambda: call(args.operation, verdict=args.verdict, body=body)
    try:
        result = operation()
    except TimeoutError:
        result = {'ok': False, 'error': 'broker response timed out: write outcome unknown; do not retry or report success'}
    print(json.dumps(result))
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
