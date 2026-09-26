"""Credentialless CLI for the one-run broker socket (copy into staged code).

Standard library only: this file is copied into the sandbox on its own.

A fixer publishes with ``push --files <path>... --message-file <file>`` (or ``--message``): the
client reads those files from ``/work``, builds the broker's manifest
``{base_head, message, files: [{path, content_b64, sha256}]}`` itself, and refuses anything the
broker would refuse *before* the one write is spent. ``base_head`` comes from the turn file the
host writes, read-only, next to this client; the broker still compares it with the run's own
scoped head, so a wrong value is refused, never trusted. ``push --manifest-file`` still sends a
manifest built by hand.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat

SOCKET = '/run/review-loop/broker/broker.sock'
MAX_FRAME = 196 * 1024
# A push or review is several GitHub calls plus git fetch/push (each up to 90s), all
# host-side. Wait for the answer instead of timing out mid-write; the turn deadline is
# the real bound.
WRITE_TIMEOUT = 900

# The exported PR tree, and the host's read-only facts about this turn (``{"head": sha}``).
WORK = '/work'
TURN_FILE = '/opt/client/review-loop-turn.json'

# The broker's own limits (review_loop.safe_push), repeated here only so an agent learns about a
# refusal before spending its write. The broker re-checks every one of them.
MAX_FILES = 24
MAX_FILE = 64 * 1024
MAX_CONTENT = 128 * 1024
MAX_MESSAGE = 240
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_SEGMENT = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')
_CONTROL_FILES = {'.gitmodules', '.gitattributes'}
_CONTROL_PATHS = {'codeowners', 'docs/codeowners'}


class ManifestError(ValueError):
    """A push the broker would refuse; nothing was sent."""


def _repo_path(argument: str, work: str) -> str:
    """The repository-relative path for a file named on the command line."""
    raw = os.path.normpath(argument if os.path.isabs(argument) else os.path.join(work, argument))
    relative = os.path.relpath(raw, work)
    if relative == '.' or relative.startswith('..'):
        raise ManifestError(f'{argument}: not a file under {work}')
    path = relative.replace(os.sep, '/')
    if (len(path) > 512 or any(not _SEGMENT.fullmatch(part) or part in ('.', '..')
                               or part.lower() == '.git' for part in path.split('/'))):
        raise ManifestError(f'{path}: unsafe path (each segment must be 1-128 of A-Z a-z 0-9 _ . -)')
    parts = path.lower().split('/')
    if parts[0] == '.github' or _CONTROL_FILES.intersection(parts) or path.lower() in _CONTROL_PATHS:
        raise ManifestError(f'{path}: repository control file (.github/, .gitmodules, '
                            '.gitattributes, CODEOWNERS) — the broker refuses it')
    return path


def _read(work: str, path: str) -> bytes:
    """The bytes of one regular file, never through a symlink anywhere along its path."""
    current = work
    for part in path.split('/'):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise ManifestError(f'{path}: no such file in {work} (a push cannot delete a file)') from None
        if stat.S_ISLNK(info.st_mode):
            raise ManifestError(f'{path}: a symlink is on the path — a push writes regular files only')
    if not stat.S_ISREG(info.st_mode):
        raise ManifestError(f'{path}: not a regular file')
    if info.st_size > MAX_FILE:
        raise ManifestError(f'{path}: {info.st_size} bytes — a file may be at most {MAX_FILE}')
    with open(current, 'rb') as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ManifestError(f'{path}: larger than {MAX_FILE} bytes')
    return data


def turn_head(turn_file: str | None = None) -> str:
    turn_file = turn_file or TURN_FILE
    try:
        head = json.loads(Path(turn_file).read_text()).get('head')
    except (OSError, ValueError, AttributeError):
        head = None
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise ManifestError(f'this turn\'s head is unavailable ({turn_file}); '
                            'use --manifest-file with base_head set to the head you were given')
    return head


def build_manifest(files: list[str], message: str, *, work: str | None = None,
                   turn_file: str | None = None) -> dict:
    """The push manifest for whole files under ``work``, or ``ManifestError`` naming the limit."""
    work = work or WORK
    if not isinstance(message, str) or not message.strip() or '\x00' in message:
        raise ManifestError('the commit message must be non-empty text')
    if len(message.encode('utf-8')) > MAX_MESSAGE:
        raise ManifestError(f'the commit message is {len(message.encode("utf-8"))} bytes — '
                            f'at most {MAX_MESSAGE}')
    if not 1 <= len(files) <= MAX_FILES:
        raise ManifestError(f'{len(files)} files — a push carries 1 to {MAX_FILES}')
    entries, seen, total = [], set(), 0
    for argument in files:
        path = _repo_path(argument, work)
        if path in seen:
            raise ManifestError(f'{path}: named twice')
        seen.add(path)
        data = _read(work, path)
        total += len(data)
        if total > MAX_CONTENT:
            raise ManifestError(f'the files add up to more than {MAX_CONTENT} bytes')
        entries.append({'path': path, 'content_b64': base64.b64encode(data).decode('ascii'),
                        'sha256': hashlib.sha256(data).hexdigest()})
    for path in seen:
        parts = path.split('/')
        if any('/'.join(parts[:n]) in seen for n in range(1, len(parts))):
            raise ManifestError(f'{path}: both a file and a directory in this push')
    return {'base_head': turn_head(turn_file), 'message': message, 'files': entries}


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


def _push(parser: argparse.ArgumentParser, args: argparse.Namespace):
    if args.body_file or args.verdict:
        parser.error('push takes --files with --message/--message-file, or --manifest-file')
    if args.manifest_file:
        if args.files or args.message is not None or args.message_file or args.dry_run:
            parser.error('--manifest-file cannot be combined with --files, --message or --dry-run')
        path = Path(args.manifest_file)
        if path.stat().st_size > MAX_FRAME:
            parser.error('manifest too large')
        manifest = json.loads(path.read_text())
        return lambda: call('push', manifest=manifest)
    if not args.files or (args.message is None) == (not args.message_file):
        parser.error('push requires --files <path>... and exactly one of --message or '
                     '--message-file (or a hand-built --manifest-file)')
    try:
        if args.message_file:
            raw = Path(args.message_file).read_bytes()
            if len(raw) > 4 * MAX_MESSAGE:
                raise ManifestError(f'the commit message is over {MAX_MESSAGE} bytes')
            message = raw.decode('utf-8').rstrip('\n')
        else:
            message = args.message
        manifest = build_manifest(args.files, message)
    except (ManifestError, OSError, UnicodeError) as exc:
        parser.error(f'push refused before sending (your one write is unspent): {exc}')
    if args.dry_run:
        summary = {'ok': True, 'dry_run': True, 'base_head': manifest['base_head'],
                   'message': manifest['message'],
                   'files': [{'path': entry['path'], 'sha256': entry['sha256']}
                             for entry in manifest['files']]}
        return lambda: summary
    return lambda: call('push', manifest=manifest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('review', 'request_review', 'push', 'ruling'))
    parser.add_argument('--verdict', default='')
    parser.add_argument('--body-file')
    parser.add_argument('--manifest-file')
    parser.add_argument('--files', nargs='+', default=[],
                        help='push: files under /work to publish, each as its whole new content')
    parser.add_argument('--message', help='push: the commit message (at most 240 bytes)')
    parser.add_argument('--message-file', help='push: a file holding the commit message')
    parser.add_argument('--dry-run', action='store_true',
                        help='push: build and check the manifest, send nothing')
    args = parser.parse_args()
    if args.operation == 'push':
        operation = _push(parser, args)
    else:
        if args.files or args.message is not None or args.message_file or args.dry_run:
            parser.error('--files, --message, --message-file and --dry-run are push options')
        if args.operation == 'ruling' and (args.verdict not in ('ACCEPT', 'REJECT', 'RESPEC')
                                           or not args.body_file or args.manifest_file):
            parser.error('ruling requires --verdict ACCEPT|REJECT|RESPEC and --body-file')
        if args.manifest_file or (args.operation == 'review' and not args.body_file):
            parser.error('invalid review arguments')
        if args.operation == 'review' and args.verdict not in ('APPROVE', 'REQUEST_CHANGES'):
            parser.error('review requires --verdict APPROVE|REQUEST_CHANGES (COMMENT is not a verdict)')
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
