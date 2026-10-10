"""Credentialless CLI for the one-run broker socket (copy into staged code).

Standard library only: this file is copied into the sandbox on its own.

A fixer publishes with ``push --files <path>... --message-file <file>`` (or ``--message``): the
client reads those files from ``/work``, builds the broker's manifest
``{base_head, message, files: [{path, content_b64, sha256}]}`` itself, and refuses anything the
broker would refuse *before* the one write is spent. When whole files do not fit (a large file, a
deleted one, many of them) it sends a unified diff of ``/work`` against the read-only export of
the same head instead, ``{base_head, message, patch_b64, sha256}`` (#64); the host applies it to
that exact tree and checks every path it changed. ``base_head`` comes from the turn file the
host writes, read-only, next to this client; the broker still compares it with the run's own
scoped head, so a wrong value is refused, never trusted. ``push --manifest-file`` still sends a
manifest built by hand.

After the push, ``request_review --answers-file <file>`` asks for the next review and, with it,
publishes the fixer's answers to the findings: the host posts them as one PR comment by the fixer
identity, which the next reviewer and the adjudicator read. The client checks the answers' size
before the request is spent.
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

DEFAULT_SOCKET = '/run/review-loop/broker/broker.sock'
SOCKET = os.environ.get('DIAKTOROS_BROKER_SOCKET', DEFAULT_SOCKET)
TRIAGE_COMMENT_MAX = 1000          # run_supervisor.TRIAGE_COMMENT_MAX; this file runs standalone
MAX_FRAME = 768 * 1024           # broker_ipc.MAX_PUSH_REQUEST
# A push or review is several GitHub calls plus git fetch/push (each up to 90s), all
# host-side. Wait for the answer instead of timing out mid-write; the turn deadline is
# the real bound.
WRITE_TIMEOUT = 900

# The exported PR tree, and the host's read-only facts about this turn (``{"head": sha}``).
DEFAULT_WORK = '/work'
WORK = os.environ.get('DIAKTOROS_WORK', DEFAULT_WORK)
# The read-only export the seat's /work was copied from (contained.EXPORT_DIR): the base a diff is
# taken against.
DEFAULT_EXPORT = '/opt/export'
EXPORT = os.environ.get('DIAKTOROS_EXPORT', DEFAULT_EXPORT)
DEFAULT_TURN_FILE = '/opt/client/review-loop-turn.json'
TURN_FILE = os.environ.get('DIAKTOROS_TURN_FILE', DEFAULT_TURN_FILE)

# The broker's own limits (diaktoros.safe_push), repeated here only so an agent learns about a
# refusal before spending its write. The broker re-checks every one of them.
MAX_FILES = 24
MAX_FILE = 64 * 1024
MAX_CONTENT = 128 * 1024
MAX_MESSAGE = 240
MAX_PATCH = 512 * 1024             # safe_push.MAX_PATCH
MAX_PATCH_PATHS = 64               # safe_push.MAX_PATCH_PATHS
MAX_DIFF_SOURCE = 16 * 1024 * 1024  # the largest file the client will read to diff
# diaktoros.broker.ANSWERS_MAX, and the broker's request line limit the answers travel in.
MAX_ANSWERS = 8 * 1024
FILED_ISSUE_BODY_MAX = 6 * 1024    # broker.FILED_ISSUE_BODY_MAX
ISSUE_TITLE_MAX = 120              # broker.ISSUE_PR_TITLE_MAX
MAX_REQUEST = 16 * 1024
from diaktoros.wire import ANSWERS_MARKER, ANSWERS_MARKERS  # noqa: F401 - copied into the sandbox with this client
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


def _content(root: str, path: str, limit: int) -> tuple[bytes, str] | None:
    """A regular file's bytes and Git mode under ``root``, or None when it does not exist.

    Never through a symlink anywhere along its path; a file over ``limit`` is refused."""
    current = root
    for part in path.split('/'):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise ManifestError(f'{path}: a symlink is on the path — a push writes regular files only')
    if not stat.S_ISREG(info.st_mode):
        raise ManifestError(f'{path}: not a regular file')
    if info.st_size > limit:
        raise ManifestError(f'{path}: {info.st_size} bytes — too large to publish (at most {limit})')
    with open(current, 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ManifestError(f'{path}: larger than {limit} bytes')
    return data, ('100755' if info.st_mode & stat.S_IXUSR else '100644')


def _lines(path: str, data: bytes) -> list[str]:
    if b'\0' in data:
        raise ManifestError(f'{path}: a binary file can only be published whole, at most '
                            f'{MAX_FILE} bytes')
    try:
        return data.decode('utf-8').splitlines(keepends=True)
    except UnicodeDecodeError:
        raise ManifestError(f'{path}: not UTF-8 text — it can only be published whole, at most '
                            f'{MAX_FILE} bytes') from None


def build_patch(paths: list[str], work: str, export: str) -> bytes:
    """A unified diff (Git's format) of each path in ``work`` against ``export``: an edit, a new
    file, or — a path gone from ``work`` — a deletion. Text files only."""
    import difflib
    out = []
    for path in paths:
        old = _content(export, path, MAX_DIFF_SOURCE)
        new = _content(work, path, MAX_DIFF_SOURCE)
        if old is None and new is None:
            raise ManifestError(f'{path}: no such file in {work} or in the head it came from')
        if old is not None and new is not None and old[0] == new[0]:
            continue                       # unchanged: nothing to say about it
        before = _lines(path, old[0]) if old is not None else []
        after = _lines(path, new[0]) if new is not None else []
        header = f'diff --git a/{path} b/{path}\n'
        if old is None:
            header += f'new file mode {new[1]}\n'
        elif new is None:
            header += f'deleted file mode {old[1]}\n'
        out.append(header)
        for line in difflib.unified_diff(before, after,
                                         '/dev/null' if old is None else f'a/{path}',
                                         '/dev/null' if new is None else f'b/{path}'):
            out.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    if not out:
        raise ManifestError('nothing changed: every named file matches the head')
    return ''.join(out).encode('utf-8')


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
                   turn_file: str | None = None, export: str | None = None) -> dict:
    """The push manifest for ``files`` under ``work``, or ``ManifestError`` naming the limit.

    Whole files when every one exists and fits the whole-file limits (the original shape);
    otherwise a unified diff against the export of the same head (#64), which carries large
    files, deletions and up to MAX_PATCH_PATHS paths."""
    work, export = work or WORK, export or EXPORT
    if not isinstance(message, str) or not message.strip() or '\x00' in message:
        raise ManifestError('the commit message must be non-empty text')
    if len(message.encode('utf-8')) > MAX_MESSAGE:
        raise ManifestError(f'the commit message is {len(message.encode("utf-8"))} bytes — '
                            f'at most {MAX_MESSAGE}')
    if not 1 <= len(files) <= MAX_PATCH_PATHS:
        raise ManifestError(f'{len(files)} files — a push carries 1 to {MAX_PATCH_PATHS}')
    paths, seen = [], set()
    for argument in files:
        path = _repo_path(argument, work)
        if path in seen:
            raise ManifestError(f'{path}: named twice')
        seen.add(path)
        paths.append(path)
    for path in seen:
        parts = path.split('/')
        if any('/'.join(parts[:n]) in seen for n in range(1, len(parts))):
            raise ManifestError(f'{path}: both a file and a directory in this push')
    whole, total = [], 0
    for path in paths:
        found = _content(work, path, MAX_DIFF_SOURCE)
        if found is None or len(found[0]) > MAX_FILE:
            whole = None
            break
        total += len(found[0])
        whole.append((path, found[0]))
    if whole is not None and len(whole) <= MAX_FILES and total <= MAX_CONTENT:
        return {'base_head': turn_head(turn_file), 'message': message,
                'files': [{'path': path, 'content_b64': base64.b64encode(data).decode('ascii'),
                           'sha256': hashlib.sha256(data).hexdigest()} for path, data in whole]}
    patch = build_patch(paths, work, export)
    if len(patch) > MAX_PATCH:
        raise ManifestError(f'the change is a {len(patch)}-byte diff — at most {MAX_PATCH}; '
                            'publish a smaller change')
    return {'base_head': turn_head(turn_file), 'message': message,
            'patch_b64': base64.b64encode(patch).decode('ascii'),
            'sha256': hashlib.sha256(patch).hexdigest()}


def manifest_paths(manifest: dict) -> list[str]:
    """The paths a built manifest publishes, for a dry run's summary."""
    if 'files' in manifest:
        return [entry['path'] for entry in manifest['files']]
    import re as _re
    text = base64.b64decode(manifest['patch_b64']).decode('utf-8')
    return _re.findall(r'^diff --git a/(\S+) b/', text, _re.M)


def read_answers(path: str) -> str:
    """The answers text from ``path``, or ``ManifestError`` naming the limit (nothing sent)."""
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_ANSWERS + 1)
    if len(raw) > MAX_ANSWERS:
        raise ManifestError(f'the answers are over {MAX_ANSWERS} bytes — shorten them '
                            '(one line per finding: fixed at file:line, or why not)')
    text = raw.decode('utf-8')
    if not text.strip() or '\x00' in text:
        raise ManifestError('the answers must be non-empty text')
    if any(marker in text for marker in ANSWERS_MARKERS):
        raise ManifestError('the answers may not contain the host\'s answers marker')
    frame = json.dumps({'operation': 'request_review', 'verdict': '', 'body': text},
                       separators=(',', ':')).encode()
    if len(frame) > MAX_REQUEST:
        raise ManifestError(f'the answers encode to {len(frame)} bytes on the wire (non-ASCII '
                            f'text is escaped) — at most {MAX_REQUEST}; shorten them')
    return text


def call(operation: str, *, verdict: str = '', body: str = '', manifest=None,
         labels: list | None = None, socket_path: str | None = None) -> dict:
    if operation == 'push':
        payload = {'operation': 'push', 'manifest': manifest}
    elif operation == 'triage':
        payload = {'operation': 'triage', 'labels': list(labels or []), 'body': body}
    elif operation == 'open_pr':
        payload = {'operation': 'open_pr', 'manifest': manifest, 'title': verdict, 'body': body}
    elif operation == 'issue_comment':
        payload = {'operation': 'issue_comment', 'body': body}
    elif operation == 'file_issue':
        payload = {'operation': 'file_issue', 'title': verdict, 'body': body,
                   'labels': list(labels or [])}
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
                   'as': 'diff' if 'patch_b64' in manifest else 'whole files',
                   'files': manifest_paths(manifest)}
        return lambda: summary
    return lambda: call('push', manifest=manifest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('review', 'request_review', 'push', 'ruling',
                                              'triage', 'open_pr', 'issue_comment',
                                              'file_issue'))
    parser.add_argument('--verdict', default='')
    parser.add_argument('--body-file')
    parser.add_argument('--manifest-file')
    parser.add_argument('--files', nargs='+', default=[],
                        help='push/open_pr: every file you changed, added or deleted under /work '
                             '(sent whole when small, else as a diff against the head)')
    parser.add_argument('--message', help='push: the commit message (at most 240 bytes)')
    parser.add_argument('--message-file', help='push: a file holding the commit message')
    parser.add_argument('--dry-run', action='store_true',
                        help='push: build and check the manifest, send nothing')
    parser.add_argument('--answers-file',
                        help=f'request_review: your answers to the findings (at most {MAX_ANSWERS} '
                             'bytes), posted once on the PR by the host as the fixer')
    parser.add_argument('--dispute', action='store_true',
                        help='request_review, with --answers-file and no push: every finding is '
                             'not a defect (cite commands run and output); no re-review is asked')
    parser.add_argument('--title', help='open_pr: the PR title; file_issue: the issue title '
                                        '(one line)')
    parser.add_argument('--label', action='append', default=[],
                        help='triage/file_issue: one label from the list in your instructions '
                             '(repeatable)')
    parser.add_argument('--comment-file',
                        help=f'triage: one short comment ({TRIAGE_COMMENT_MAX} characters at '
                             'most), only when the loop allows one')
    args = parser.parse_args()
    if args.answers_file and args.operation != 'request_review':
        parser.error('--answers-file is a request_review option')
    if args.dispute and (args.operation != 'request_review' or not args.answers_file):
        parser.error('--dispute is a request_review option and needs --answers-file')
    if args.comment_file and args.operation != 'triage':
        parser.error('--comment-file is a triage option')
    if args.label and args.operation not in ('triage', 'file_issue'):
        parser.error('--label is a triage or file_issue option')
    if args.title is not None and args.operation not in ('open_pr', 'file_issue'):
        parser.error('--title is an open_pr or file_issue option')
    if args.operation == 'open_pr':
        if args.manifest_file or args.verdict or args.label or args.comment_file:
            parser.error('open_pr takes --files, --message/--message-file, --title and --body-file')
        if not args.title or not args.body_file:
            parser.error('open_pr requires --title and --body-file (the PR description)')
        if not args.files or (args.message is None) == (not args.message_file):
            parser.error('open_pr requires --files <path>... and exactly one of --message or '
                         '--message-file')
        try:
            message = (Path(args.message_file).read_text().rstrip('\n') if args.message_file
                       else args.message)
            manifest = build_manifest(args.files, message)
            description = Path(args.body_file).read_text()
        except (ManifestError, OSError, UnicodeError) as exc:
            parser.error(f'open_pr refused before sending (your write is unspent): {exc}')
        if args.dry_run:
            summary = {'ok': True, 'dry_run': True, 'base_head': manifest['base_head'],
                       'title': args.title,
                       'as': 'diff' if 'patch_b64' in manifest else 'whole files',
                       'files': manifest_paths(manifest)}
            operation = lambda: summary  # noqa: E731
        else:
            operation = lambda: call('open_pr', manifest=manifest, verdict=args.title,  # noqa: E731
                                     body=description)
    elif args.operation == 'file_issue':
        if (args.files or args.message is not None or args.message_file or args.dry_run
                or args.verdict or args.manifest_file or args.comment_file
                or not args.title or not args.body_file):
            parser.error('file_issue takes --title, --body-file and optional --label (repeatable)')
        title = args.title.strip()
        body = Path(args.body_file).read_text()
        if not title or len(title) > ISSUE_TITLE_MAX or any(ord(ch) < 32 for ch in title):
            parser.error(f'file_issue refused before sending: the title must be one line of 1-'
                         f'{ISSUE_TITLE_MAX} characters')
        if not body.strip() or len(body.encode()) > FILED_ISSUE_BODY_MAX:
            parser.error(f'file_issue refused before sending: the body must be non-empty and at '
                         f'most {FILED_ISSUE_BODY_MAX} bytes')
        operation = lambda: call('file_issue', verdict=title, body=body,  # noqa: E731
                                 labels=args.label)
    elif args.operation == 'issue_comment':
        if (args.files or args.message is not None or args.message_file or args.dry_run
                or args.verdict or args.manifest_file or not args.body_file):
            parser.error('issue_comment takes only --body-file')
        body = Path(args.body_file).read_text()
        operation = lambda: call('issue_comment', body=body)  # noqa: E731
    elif args.operation == 'triage':
        if (args.files or args.message is not None or args.message_file or args.dry_run
                or args.verdict or args.body_file or args.manifest_file):
            parser.error('triage takes only --label (repeatable) and --comment-file')
        comment = ''
        if args.comment_file:
            comment = Path(args.comment_file).read_text().strip()
            if len(comment) > TRIAGE_COMMENT_MAX:
                parser.error(f'triage refused before sending (your write is unspent): the '
                             f'comment is over {TRIAGE_COMMENT_MAX} characters')
        operation = lambda: call('triage', labels=args.label, body=comment)
    elif args.operation == 'push':
        operation = _push(parser, args)
    elif args.operation == 'request_review':
        if (args.files or args.message is not None or args.message_file or args.dry_run
                or args.verdict or args.body_file or args.manifest_file):
            parser.error('request_review takes only --answers-file')
        body = ''
        if args.answers_file:
            try:
                body = read_answers(args.answers_file)
            except (ManifestError, OSError, UnicodeError) as exc:
                parser.error(f'request_review refused before sending (your request is unspent): {exc}')
        extra = {'verdict': 'DISPUTE'} if args.dispute else {}
        operation = lambda: call('request_review', body=body, **extra)
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
