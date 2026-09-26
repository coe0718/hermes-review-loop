"""Host-side orchestration for a credentialless, whole-process Hermes PR turn.

Only a trusted supervisor may call this. Config and provider secrets stay on the
host; the agent sees an exported PR tree, disposable HOME, and two scoped sockets.

The exported tree is not a secret filter you can rely on. It drops documentation
and data whose *name* or *content* is shaped like a credential, and it exports the
code and templates the sandboxed Hermes has to import byte for byte. Nothing here
can tell a live credential from a fixture, so a source tree mounted this way must
carry no secrets at all: anything committed in it is readable inside the sandbox,
and a seat that runs untrusted PR content can publish what it read through its one
authorized write.
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

from . import broker_client, broker_ipc, contained, gh, inference_proxy, safe_push, trusted_fetch


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


# -- what a snapshot may export ------------------------------------------------------------------
# Every exported file is readable inside the sandbox (mounted at /opt/code, and on PYTHONPATH), so
# a *committed* secret is a leak: a seat can put those bytes in the review body it publishes. Two
# rules keep a snapshot to a code tree:
#
#  * a name that *names* a credential is dropped on whole words -- `monkey.py` and `keyboard.py`
#    are source, `keys.json` and `prod-secrets.toml` are not;
#  * documentation and data whose *content* is credential-shaped is dropped too, because one
#    ordinary `notes.md` holding a token defeats any name rule.
#
# Code and templates are never dropped by shape: the sandbox imports this tree (dropping
# `hermes_cli/subcommands/secrets.py` is a ModuleNotFoundError at start, and `agent/secret_sources`
# is a package about credentials rather than a credential), and no shape rule can tell a module
# *about* credentials from a file *holding* one. That limit is exactly why the source tree itself
# must carry no secrets -- this filter is a containment aid, not a scanner.
_ALLOWED_SUFFIXES = frozenset({'.py', '.json', '.yaml', '.yml', '.toml', '.md', '.txt',
                               '.jinja2', '.j2', '.html'})
_CODE_SUFFIXES = frozenset({'.py', '.jinja2', '.j2', '.html'})
# A plugin's manifest is configuration, not a credential, whatever directory holds it:
# `plugins/model-providers/nebius-token-factory/plugin.yaml` configures a token provider. The
# content filter below still applies to it, so a key written inside one is still dropped.
_PLUGIN_MANIFESTS = frozenset({'plugin.yaml', 'plugin.json'})
_CONTENT_SUFFIXES = frozenset({'.md', '.txt', '.json', '.yaml', '.yml', '.toml'})
_EXCLUDED_COMPONENTS = frozenset({'.git', '.venv', 'venv', '__pycache__', 'tests', 'docs',
                                  'website', 'node_modules', '.hermes', '.pytest_cache'})
# Names that are a credential whatever else they are: the exact components the first, shape-blind
# filter carried, kept because they catch a credential container a shape rule cannot.
_CREDENTIAL_NAMES = frozenset({'.env', 'auth.json', 'config.yaml', 'credentials', 'id_rsa',
                               'id_ed25519'})
# A whole word that names a credential. `key`/`keys` match only as one, so `monkey.py`,
# `keyboard.py` and `keyring.py` stay source while `keys.json` and `api-key.yaml` do not.
_CREDENTIAL_WORDS = frozenset({'credential', 'secret', 'token', 'apikey', 'accesskey', 'secretkey',
                               'password', 'passwd', 'passphrase'})
_KEY_WORDS = frozenset({'key', 'keys'})
_CREDENTIAL_SUFFIXES = frozenset({'.pem', '.pat', '.p12', '.pfx', '.key', '.jks', '.keystore'})
_PRIVATE_KEY_NAME = re.compile(r'(?:^|[^a-z0-9])id_(?:rsa|ed25519|ecdsa|dsa)(?:$|[^a-z0-9])')
_WORD = re.compile(r'[A-Za-z0-9]+')
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')
_MAX_BLOB = 2 * 1024 * 1024


def _words(part: str) -> list[str]:
    """The casefolded words of one path component: separators and camelCase both split."""
    return [word.casefold() for word in _WORD.findall(_CAMEL.sub(' ', part))]


def _credential_shaped_name(part: str) -> bool:
    """True when a component *names* a credential rather than ordinary source."""
    folded = part.casefold()
    if folded in _CREDENTIAL_NAMES or _PRIVATE_KEY_NAME.search(folded):
        return True
    if os.path.splitext(folded)[1] in _CREDENTIAL_SUFFIXES:
        return True
    for word in _words(part):
        if word in _KEY_WORDS or word in _CREDENTIAL_WORDS or word.rstrip('s') in _CREDENTIAL_WORDS:
            return True
    return False


def _credential_shaped_path(parts: list[str]) -> bool:
    """True when a tree entry's names say credential rather than source.

    The sandbox *imports* this tree, so the shape rules read a path only where dropping it cannot
    break a turn:

    * the exact names of the first filter (``credentials``, ``.env``, ``id_rsa``, ...) always say
      credential, container or not -- that is what makes ``pkg/credentials/key.py`` unsafe;
    * a module or template is exported whatever else it is called: ``hermes_cli/subcommands/
      secrets.py`` is imported by ``hermes_cli.main``, and ``agent/secret_sources/`` is a package
      *about* credentials, not a credential;
    * for the documentation and data the sandbox does not import -- and the directories holding
      them -- a credential-shaped word anywhere in the path is enough.
    """
    if any(part.casefold() in _CREDENTIAL_NAMES or _PRIVATE_KEY_NAME.search(part.casefold())
           for part in parts):
        return True
    if Path(parts[-1]).suffix.casefold() in _CODE_SUFFIXES:
        return False
    if parts[-1].casefold() in _PLUGIN_MANIFESTS:
        return False
    return any(_credential_shaped_name(part) for part in parts)


# Values that are unambiguous wherever they appear: a provider's own token prefix.
_CREDENTIAL_TEXT = (
    re.compile(r'(?<![A-Za-z0-9])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}'),
    re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}'),
    re.compile(r'(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9-]{10,}'),
    re.compile(r'(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}'),
    re.compile(r'(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'),
    re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'),
)
# `DEPLOY_TOKEN=<value>`, `"api-key": "<value>"` or a bulleted `- token: <value>`: the name is
# checked with the same word rule as a filename, and the value has to be long, alphanumeric, and
# neither a URL path nor a flag.
_ASSIGNMENT = re.compile(r'(?m)^[\s{,<>*\[\]\-]*["\']?(?P<name>[A-Za-z0-9_.\-]{1,64})["\']?'
                         r'\s*[:=]\s*["\']?(?P<value>[A-Za-z0-9+/=_.\-]{24,})')


def _credential_shaped_text(data: bytes) -> bool:
    """True when a blob's *content* carries a credential-shaped value."""
    if not data or len(data) > _MAX_BLOB:
        return False
    text = data.decode('utf-8', 'replace')
    if any(pattern.search(text) for pattern in _CREDENTIAL_TEXT):
        return True
    for match in _ASSIGNMENT.finditer(text):
        value = match.group('value')
        if (not value.startswith(('/', '-'))
                and any(char.isdigit() for char in value)
                and any(char.isalpha() for char in value)
                and _credential_shaped_name(match.group('name'))):
            return True
    return False


def exported_secrets(root: Path) -> tuple[list[str], list[str]]:
    """(violations, advisories) in an already exported snapshot tree.

    The selftest runs this over the snapshot it just staged, so containment notices a secret that
    reached the sandbox instead of trusting the filter that built it:

    * violations -- what the export rules say must not be there (a credential-shaped name on a
      file the filter can drop, or documentation/data carrying a credential-shaped value);
    * advisories -- code carrying credential-shaped *text*. The sandbox imports that code, so the
      filter never drops it, and only a human can say whether the text is a live secret.
    """
    violations: list[str] = []
    advisories: list[str] = []
    base = Path(root)
    for directory, subdirectories, names in os.walk(base, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories
                                   if not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
            suffix = path.suffix.casefold()
            if _credential_shaped_path(relative.split('/')):
                violations.append(f'{relative}: credential-shaped name')
                continue
            try:
                if path.stat().st_size > _MAX_BLOB:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if suffix in _CONTENT_SUFFIXES and _credential_shaped_text(data):
                violations.append(f'{relative}: credential-shaped content')
            elif suffix in _CODE_SUFFIXES and any(
                    pattern.search(data.decode('utf-8', 'replace'))
                    for pattern in _CREDENTIAL_TEXT):
                advisories.append(f'{relative}: credential-shaped text in imported code')
    return violations, advisories


def _export_committed_source(source_fd: int, destination: Path) -> None:
    """Keep the repository directory pinned while Git reads the committed tree."""
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
        if any(part.startswith('.') or part.casefold() in _EXCLUDED_COMPONENTS for part in parts):
            continue
        suffix = Path(parts[-1]).suffix.casefold()
        if suffix not in _ALLOWED_SUFFIXES:
            continue
        if _credential_shaped_path(parts):
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
        # A committed `notes.md` holding a token passes every name rule there is, so the content of
        # documentation and data is checked too (never of code or templates -- see above).
        if suffix in _CONTENT_SUFFIXES and _credential_shaped_text(data):
            continue
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
    shutil.copyfile(Path(__file__).with_name('wire.py'), client / 'wire.py')
    shutil.copyfile(Path(__file__).with_name('inference_proxy.py'), client / 'inference_proxy.py')


# Each role is told only about its own broker command. Advertising another role's command would
# invite a denied write at best, and it is the kind of prompt drift that later reads as permission.
_COMMON = ('You have no GitHub credentials or network. Never claim a write succeeded without '
           'an ok response. A write can take minutes; if it times out, its outcome is unknown: '
           'do not retry it, say so.')
TOOLS = {
    'reviewer': ('For your one authorized write use `python -m review_loop.broker_client review '
                 '--verdict APPROVE --body-file /work/review.txt` (or --verdict REQUEST_CHANGES). '
                 'The verdict must be exactly APPROVE or REQUEST_CHANGES; anything else is refused '
                 'without spending the write. A reviewer gets exactly one write. '),
    'fixer': ('To publish, name the files you changed and write a commit message: '
              '`python -m review_loop.broker_client push --files src/a.py src/b.py '
              '--message-file /tmp/commit.txt` (or `--message "..."`; paths are under `/work`). '
              'Add `--dry-run` first to check it without spending the write. The client builds '
              'the manifest itself (each file\'s whole new content, base64 and sha256, and this '
              'turn\'s head as base_head, which the host provides) and refuses before sending '
              f'anything past the broker\'s limits: at most {safe_push.MAX_FILES} files, '
              f'{safe_push.MAX_FILE // 1024} KiB per file and {safe_push.MAX_CONTENT // 1024} KiB '
              f'in total, a non-empty commit message of at most {safe_push.MAX_MESSAGE} bytes, '
              'path segments of A-Z a-z 0-9 _ . - only, and nothing under `.github/`, no `.git`, '
              '`.gitmodules`, `.gitattributes` or `CODEOWNERS`. A push only adds or replaces whole '
              'regular files: it cannot delete or rename a file (a rename would leave the old '
              'path in place), change a file mode, or write a symlink; if the fix needs one of '
              'those, say so in your answers. `/work` is a plain export with no `.git`, so keep '
              'track of which files you changed. Then write your answers to the findings to a file '
              '(for each: fixed at file:line, or why it is not a defect, with evidence; at most '
              f'{broker_client.MAX_ANSWERS // 1024} KiB) and run `python -m review_loop.broker_client '
              'request_review --answers-file /tmp/answers.md`: the host posts the answers once as a '
              'PR comment by the fixer account, where the next reviewer and the adjudicator read '
              'them, then requests the review. It is the only way your answers leave the sandbox, '
              'and the comment is public to everyone who can see the PR. '
              'A fixer gets one push followed by one review request. '
              '(`--manifest-file` still takes a hand-built manifest: '
              '{"base_head", "message", "files": [{"path", "content_b64", "sha256"}]}.) '),
    'adjudicator': ('`/work` is read-only; write files under `/tmp`. To deliver your ruling use '
                    '`python -m review_loop.broker_client ruling --verdict ACCEPT '
                    '--body-file /tmp/ruling.txt` (or REJECT/RESPEC). An adjudicator gets '
                    'exactly one ruling and cannot review, push or merge. '),
}


def tool_instructions(role: str) -> str:
    if role not in TOOLS:
        raise TurnDenied('unsupported role')
    return TOOLS[role] + _COMMON


SANDBOX_KEY = 'sandbox-dummy-not-a-credential'
# Hermes applies the Claude Code request identity to any token it classifies as a Claude
# subscription login (``agent/anthropic_credentials._is_oauth_token``: ``sk-ant-`` but not
# ``sk-ant-api``, a JWT, or ``cc-``). The ``cc-`` form is used on purpose: it satisfies that test
# without imitating a real ``sk-ant-oat01`` secret, which secret scanners (GitHub's, Hermes's
# plugin guard) rightly flag, and short enough not to resemble any secret. It authenticates
# nothing; the proxy drops it.
SANDBOX_OAUTH_TOKEN = 'cc-dummy'  # deliberately short: a placeholder, not secret-shaped
SEAT_PROVIDER = 'review-loop-seat'


def sandbox_config(model: str, api_mode: str = 'chat_completions',
                   client_identity: str = '') -> tuple[str, str, str]:
    """``(config.yaml, .env, --provider)`` for the sandboxed Hermes of one seat.

    The sandbox speaks the seat's wire format to the local bridge and holds only a dummy key:

    * ``chat_completions`` — ``provider: custom`` at ``http://127.0.0.1:18761/v1``;
    * ``codex_responses`` / ``anthropic_messages`` — a named provider ``review-loop-seat`` with
      that ``api_mode`` (Hermes ignores ``api_mode: codex_responses`` on a *bare* custom endpoint
      that is not OpenAI's), at ``/v1`` (→ ``/v1/responses``) or ``/anthropic`` (→
      ``/anthropic/v1/messages``);
    * a Claude subscription (``client_identity == 'claude_code'``) — ``provider: anthropic`` at
      the bridge's ``/anthropic`` path with a dummy OAuth-shaped ``ANTHROPIC_TOKEN``, so Hermes
      applies the Claude Code system prefix and tool naming that subscription requests need.
    """
    from . import inference_proxy
    inference_proxy.contract_for(api_mode)
    base = f'http://127.0.0.1:{inference_proxy.BRIDGE_PORT}'
    tail = 'plugins:\n  enabled: []\nmemory:\n  memory_enabled: false\n'
    name = json.dumps(model)
    if api_mode == 'chat_completions':
        return ('model:\n  provider: custom\n  default: ' + name + f'\n  base_url: {base}/v1\n'
                f'  api_key: {SANDBOX_KEY}\n' + tail, '', 'custom')
    if api_mode == 'anthropic_messages' and client_identity == 'claude_code':
        return ('model:\n  provider: anthropic\n  default: ' + name +
                f'\n  base_url: {base}/anthropic\n' + tail,
                f'ANTHROPIC_TOKEN={SANDBOX_OAUTH_TOKEN}\n', 'anthropic')
    path = '/v1' if api_mode == 'codex_responses' else '/anthropic'
    return (f'model:\n  provider: {SEAT_PROVIDER}\n  default: ' + name + '\nproviders:\n'
            f'  {SEAT_PROVIDER}:\n    base_url: {base}{path}\n    api_key: {SANDBOX_KEY}\n'
            f'    api_mode: {api_mode}\n' + tail, '', SEAT_PROVIDER)


def run_turn(loop: dict, scope: broker_ipc.RunScope, *, source: Path, venv: Path,
             runtime: Path, rust: Path, upstream: str, key: str, model: str,
             prompt: str, timeout: int = 600, work_root: Path | None = None,
             no_write: bool = False, observed: dict | None = None,
             api_mode: str = 'chat_completions', credential=None, proxy_model: str = '',
             client_identity: str = '', review_diff: str | None = None) -> int:
    """Stage a live PR head, start host capabilities, execute Hermes within bwrap.

    ``api_mode`` picks the proxy contract and the sandbox's provider config; ``credential`` (a
    provider from ``seat_model.SeatInference.credential_provider``) replaces the static ``key``
    when given — an OAuth seat's token is then refreshed host-side during the turn.
    ``proxy_model`` is the model id forced on the wire (Hermes's spelling), default ``model``.

    ``no_write`` (host-only; the selftest's live turn) starts the broker in its record-only
    mode: a reviewer's verdict is authorized with live reads and recorded, never POSTed.
    ``observed``, when given, receives the sandbox exit code, bounded output tails and the
    recorded submissions.

    ``review_diff``, when given, is the host-built diff of the PR (``run_supervisor.pr_change``);
    it is mounted read-only at ``/opt/review/pr.diff``, outside the ``/work`` a fixer publishes
    from, so it can never become part of a push.
    """
    if scope.repo != loop.get('repo') or scope.role not in TOOLS:
        raise TurnDenied('scope mismatch')
    if no_write is not False and (no_write is not True or scope.role != 'reviewer'):
        raise TurnDenied('no-write mode supports only a reviewer turn')
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
        shutil.copyfile(Path(__file__).with_name('wire.py'), client / 'wire.py')
        if scope.role == 'fixer':
            # Host-written and mounted read-only at /opt/client: the push helper's base_head.
            # A convenience, not an authority — the broker compares it with scope.head itself.
            turn = client.parent / Path(broker_client.TURN_FILE).name
            turn.write_text(json.dumps({'head': scope.head}) + '\n')
            turn.chmod(0o444)
        home.mkdir(mode=0o700)
        config_text, env_text, provider = sandbox_config(model, api_mode, client_identity)
        (home / 'config.yaml').write_text(config_text)
        (home / 'config.yaml').chmod(0o600)
        if env_text:
            (home / '.env').write_text(env_text)
            (home / '.env').chmod(0o600)
        query = root / 'query.txt'
        query.write_text(prompt + '\n\n' + tool_instructions(scope.role) + '\n')
        review = None
        if review_diff is not None:
            review = root / 'review'
            review.mkdir(mode=0o700)
            (review / 'pr.diff').write_text(review_diff)
            (review / 'pr.diff').chmod(0o444)
        checkout = trusted_fetch.stage(loop, repo=scope.repo, number=scope.number,
                                       head=scope.head, ref=scope.branch, role=scope.role,
                                       sandbox_root=root / 'export')
        with ExitStack() as stack:
            sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rl-', dir=tempfile.gettempdir())))
            # AF_UNIX has a ~108-byte pathname limit, independent of work_root.
            if len(os.fsencode(sockets)) > 50:
                raise TurnDenied('scratch socket directory path too long')
            inference = stack.enter_context(inference_proxy.InferenceCapability(
                sockets / 'i', upstream, None if credential is not None else key,
                model=proxy_model or model, quota=32, api_mode=api_mode, credential=credential))
            broker_root = sockets / 'b'
            broker_root.mkdir(mode=0o700)
            broker = stack.enter_context(broker_ipc.RunBroker(
                loop, scope, broker_root, require_push=scope.role == 'fixer',
                require_receipt=scope.role == 'reviewer', no_write=no_write))
            server = broker_ipc.serve_in_thread(broker)
            try:
                command = ['/opt/venv/bin/python', '-m', 'review_loop.inference_proxy',
                           'bridge', '--', '/opt/venv/bin/python', '/opt/venv/bin/hermes', 'chat',
                           '--query-file', '/opt/query', '--oneshot', '-Q',
                           '--provider', provider, '-m', model, '-t', 'terminal,file',
                           '--ignore-rules', '--max-turns', '24', '--run-budget', str(timeout)]
                result = contained.run(code=code, venv=venv, runtime=runtime,
                    home=home, checkout=checkout, rust=rust, query=query, entry=command,
                    inference_socket_dir=inference.directory,
                    broker_socket_dir=broker.socket_path.parent,
                    client_code=client.parent, review_dir=review, timeout=timeout,
                    # A ruling is judgement, not a change: the adjudicator's tree is mounted
                    # read-only so nothing it runs can dress up the head it rules on.
                    checkout_writable=scope.role != 'adjudicator')
                if observed is not None:
                    observed.update(returncode=result.returncode,
                                    stdout=result.stdout[-4000:], stderr=result.stderr[-4000:],
                                    submissions=[dict(entry) for entry in broker.recorded])
                if result.returncode == 0 and not broker.completed:
                    raise TurnDenied('agent exited without a confirmed scoped write')
                return result.returncode
            finally:
                broker.close()
                server.join(timeout=5)
                if server.is_alive():
                    raise TurnDenied('broker did not shut down')
