"""Host-side orchestration for a credentialless, whole-process Hermes PR turn.

Only a trusted supervisor may call this. Config and provider secrets stay on the
host; the agent sees an exported PR tree, disposable HOME, and two scoped sockets.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import broker_ipc, contained, gh, inference_proxy, trusted_fetch


class TurnDenied(Exception):
    pass


def _safe_code_snapshot(source: Path, destination: Path) -> None:
    """Export blobs from a pinned commit, never the index or mutable worktree."""
    source = Path(source).absolute()
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise TurnDenied('invalid source') from exc
    try:
        _export_committed_source(source_fd, destination)
    finally:
        os.close(source_fd)


def _export_committed_source(source_fd: int, destination: Path) -> None:
    """Keep the repository directory pinned while Git reads the committed tree."""
    allowed = {'.py', '.json', '.yaml', '.yml', '.toml', '.md', '.txt', '.jinja2', '.j2', '.html'}
    forbidden = {'.env', 'auth.json', 'config.yaml', 'credentials', 'id_rsa', 'id_ed25519'}
    excluded = {'.git', '.venv', 'venv', '__pycache__', 'tests', 'docs',
                'website', 'node_modules', '.hermes', '.pytest_cache'}
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1',
           'GIT_OPTIONAL_LOCKS': '0'}
    command = ['/usr/bin/git', '-C', f'/proc/self/fd/{source_fd}']

    def git(*args: str, limit: int) -> bytes:
        process = None
        try:
            process = subprocess.Popen([*command, *args], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=env,
                                       pass_fds=(source_fd,))
            assert process.stdout is not None
            data = process.stdout.read(limit + 1)
            if len(data) > limit:
                raise TurnDenied('source snapshot exceeds bounds')
            if process.wait(timeout=15) != 0:
                raise TurnDenied('source must be a committed Git snapshot')
            return data
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TurnDenied('source Git snapshot unavailable') from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()

    try:
        format_name = git('rev-parse', '--show-object-format', limit=32).strip()
        if format_name not in (b'sha1', b'sha256'):
            raise TurnDenied('unsupported source object format')
        digest = hashlib.sha256 if format_name == b'sha256' else hashlib.sha1
        width = 64 if format_name == b'sha256' else 40
        commit = git('rev-parse', '--verify', 'HEAD^{commit}', limit=128).strip().decode('ascii')
        if not re.fullmatch('[0-9a-f]{' + str(width) + '}', commit):
            raise TurnDenied('invalid source commit')
        tree = git('ls-tree', '-rz', '-l', '--full-tree', commit, limit=8 * 1024 * 1024)
    except (ValueError, UnicodeError) as exc:
        raise TurnDenied('invalid source Git metadata') from exc
    destination.mkdir(mode=0o700)
    count = total = 0
    for raw in tree.split(b'\0'):
        if not raw:
            continue
        try:
            metadata, name_bytes = raw.split(b'\t', 1)
            mode, kind, blob, size = metadata.split()
            name = name_bytes.decode('utf-8')
            parts = name.split('/')
            length = int(size)
        except (ValueError, UnicodeError) as exc:
            raise TurnDenied('invalid source tree entry') from exc
        if (any(not part or part in ('.', '..') or '\\' in part or
                any(ord(char) < 32 or ord(char) == 127 for char in part)
                for part in parts) or len(name_bytes) > 4096):
            raise TurnDenied('unsafe source path')
        if any(part.startswith('.') or part.casefold() in excluded or
               part.casefold() in forbidden for part in parts):
            continue
        if Path(parts[-1]).suffix.casefold() not in allowed:
            continue
        if mode not in (b'100644', b'100755') or kind != b'blob':
            raise TurnDenied('source contains nonregular file')
        if not re.fullmatch(b'[0-9a-f]{' + str(width).encode() + b'}', blob) or length < 0:
            raise TurnDenied('invalid source blob')
        count += 1
        total += length
        if count > 20000 or length > 2 * 1024 * 1024 or total > 100 * 1024 * 1024:
            raise TurnDenied('source snapshot exceeds bounds')
        data = git('cat-file', 'blob', blob.decode('ascii'), limit=length)
        if (len(data) != length or
                digest(b'blob ' + str(length).encode() + b'\0' + data).hexdigest().encode() != blob):
            raise TurnDenied('source blob hash mismatch')
        # Resolve each destination component relative to pinned directory fds.
        # A swapped symlink can never redirect a write outside this snapshot.
        with ExitStack() as stack:
            directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            for part in parts[:-1]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                directory = os.open(part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory)
                stack.callback(os.close, directory)
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o700 if mode == b'100755' else 0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
    if not (destination / 'run_agent.py').is_file():
        raise TurnDenied('source snapshot lacks Hermes')
    client = destination / 'review_loop'
    client.mkdir(exist_ok=True)
    (client / '__init__.py').touch()
    shutil.copyfile(Path(__file__).with_name('broker_client.py'), client / 'broker_client.py')
    shutil.copyfile(Path(__file__).with_name('inference_proxy.py'), client / 'inference_proxy.py')


# Each role is told only about its own broker command. Advertising another role's command would
# invite a denied write at best, and it is the kind of prompt drift that later reads as permission.
_COMMON = ('You have no GitHub credentials or network. Never claim a write succeeded without '
           'an ok response. A write can take minutes; if it times out, its outcome is unknown: '
           'do not retry it, say so.')
TOOLS = {
    'reviewer': ('For your one authorized write use `python -m review_loop.broker_client review '
                 '--verdict APPROVE --body-file /work/review.txt` (or REQUEST_CHANGES/COMMENT). '
                 'A reviewer gets exactly one write. '),
    'fixer': ('To publish use `python -m review_loop.broker_client push '
              '--manifest-file /work/manifest.json`, then `python -m review_loop.broker_client '
              'request_review`. A fixer gets one push followed by one review request. '),
    'adjudicator': ('`/work` is read-only; write files under `/tmp`. To deliver your ruling use '
                    '`python -m review_loop.broker_client ruling --verdict ACCEPT '
                    '--body-file /tmp/ruling.txt` (or REJECT/RESPEC). An adjudicator gets '
                    'exactly one ruling and cannot review, push or merge. '),
}


def tool_instructions(role: str) -> str:
    if role not in TOOLS:
        raise TurnDenied('unsupported role')
    return TOOLS[role] + _COMMON


def run_turn(loop: dict, scope: broker_ipc.RunScope, *, source: Path, venv: Path,
             runtime: Path, rust: Path, upstream: str, key: str, model: str,
             prompt: str, timeout: int = 600, work_root: Path | None = None) -> int:
    """Stage a live PR head, start host capabilities, execute Hermes within bwrap."""
    if scope.repo != loop.get('repo') or scope.role not in TOOLS:
        raise TurnDenied('scope mismatch')
    if not model or not prompt or timeout < 1 or not key:
        raise TurnDenied('missing model, prompt or credential')
    parent = Path(work_root or loop['state_dir']).resolve()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_mode & 0o077:
        raise TurnDenied('work root must be private')
    with tempfile.TemporaryDirectory(prefix='turn-', dir=parent) as tmp:
        root = Path(tmp)
        code, home = root / 'code', root / 'home'
        _safe_code_snapshot(Path(source), code)
        client = root / 'client' / 'review_loop'
        client.mkdir(mode=0o700, parents=True)
        (client / '__init__.py').touch()
        shutil.copyfile(Path(__file__).with_name('broker_client.py'), client / 'broker_client.py')
        home.mkdir(mode=0o700)
        (home / 'config.yaml').write_text('model:\n  provider: custom\n  default: ' +
            json.dumps(model) + '\n  base_url: http://127.0.0.1:18761/v1\n'
            '  api_key: sandbox-dummy-not-a-credential\nplugins:\n  enabled: []\n'
            'memory:\n  memory_enabled: false\n')
        (home / 'config.yaml').chmod(0o600)
        query = root / 'query.txt'
        query.write_text(prompt + '\n\n' + tool_instructions(scope.role) + '\n')
        checkout = trusted_fetch.stage(loop, repo=scope.repo, number=scope.number,
                                       head=scope.head, ref=scope.branch, role=scope.role,
                                       sandbox_root=root / 'export')
        with ExitStack() as stack:
            sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rl-', dir=tempfile.gettempdir())))
            # AF_UNIX has a ~108-byte pathname limit, independent of work_root.
            if len(os.fsencode(sockets)) > 50:
                raise TurnDenied('scratch socket directory path too long')
            inference = stack.enter_context(inference_proxy.InferenceCapability(
                sockets / 'i', upstream, key, model=model, quota=32))
            broker_root = sockets / 'b'
            broker_root.mkdir(mode=0o700)
            broker = stack.enter_context(broker_ipc.RunBroker(
                loop, scope, broker_root, require_push=scope.role == 'fixer',
                require_receipt=scope.role == 'reviewer'))
            server = broker_ipc.serve_in_thread(broker)
            try:
                command = ['/opt/venv/bin/python', '-m', 'review_loop.inference_proxy',
                           'bridge', '--', '/opt/venv/bin/python', '/opt/venv/bin/hermes', 'chat',
                           '--query-file', '/opt/query', '--oneshot', '-Q',
                           '--provider', 'custom', '-m', model, '-t', 'terminal,file',
                           '--ignore-rules', '--max-turns', '24', '--run-budget', str(timeout)]
                result = contained.run(code=code, venv=venv, runtime=runtime,
                    home=home, checkout=checkout, rust=rust, query=query, entry=command,
                    inference_socket_dir=inference.directory,
                    broker_socket_dir=broker.socket_path.parent,
                    client_code=client.parent, timeout=timeout,
                    # A ruling is judgement, not a change: the adjudicator's tree is mounted
                    # read-only so nothing it runs can dress up the head it rules on.
                    checkout_writable=scope.role != 'adjudicator')
                if result.returncode == 0 and not broker.completed:
                    raise TurnDenied('agent exited without a confirmed scoped write')
                return result.returncode
            finally:
                broker.close()
                server.join(timeout=5)
                if server.is_alive():
                    raise TurnDenied('broker did not shut down')
