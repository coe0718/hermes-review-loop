"""Credentialed, checkout-free, exact-head Git push for one scoped PR head.

The sandbox supplies only bounded regular-file bytes; only the trusted broker uses
Git in a fresh bare repository, with isolated configuration and an exact-head lease.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote

from . import broker, config, gh

MAX_FILES = 24
MAX_CONTENT = 128 * 1024
MAX_FILE = 64 * 1024
MAX_MESSAGE = 240
SHA = re.compile(r"[0-9a-f]{40}\Z")
SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


class PushFailure(broker.BrokerDenied):
    """A failed push with a durable attempt boundary; never infer this from text.

    Once attempt journaling begins, even a failed journal or an unchanged ref
    needs a host hold: the write/verification path was entered but did not finish.
    """

    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__(f"Git ref update not confirmed ({outcome})")


def _sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise broker.BrokerDenied("invalid Git object SHA")
    return value


# Files that act on the repository rather than live in it. A workflow runs with the
# repository's Actions secrets, so writing one would hand a credentialless fixer those
# secrets through CI; the others change checkout, review ownership or submodule sources.
CONTROL_FILES = {".gitmodules", ".gitattributes"}
CONTROL_PATHS = {"codeowners", "docs/codeowners"}


def _path(value: object) -> str:
    if (not isinstance(value, str) or len(value) > 512 or not value
            or any(not SEGMENT.fullmatch(part) or part in (".", "..")
                   or part.lower() == ".git"
                   for part in value.split("/"))):
        raise broker.BrokerDenied("unsafe file path")
    parts = value.lower().split("/")
    if parts[0] == ".github" or CONTROL_FILES.intersection(parts) or value.lower() in CONTROL_PATHS:
        raise broker.BrokerDenied("repository control file")
    return value


def _manifest(manifest: object) -> tuple[str, list[tuple[str, bytes]]]:
    if not isinstance(manifest, dict) or set(manifest) != {"base_head", "message", "files"}:
        raise broker.BrokerDenied("invalid push manifest")
    base = _sha(manifest["base_head"])
    message, files = manifest["message"], manifest["files"]
    if (not isinstance(message, str) or not message.strip() or "\x00" in message
            or len(message.encode("utf-8")) > MAX_MESSAGE
            or not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES):
        raise broker.BrokerDenied("invalid push message or file count")
    parsed = []
    total = 0
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "content_b64", "sha256"}:
            raise broker.BrokerDenied("invalid file entry")
        path = _path(entry["path"])
        if path in seen:
            raise broker.BrokerDenied("duplicate file path")
        seen.add(path)
        encoded, digest = entry["content_b64"], entry["sha256"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE + 2) // 3):
            raise broker.BrokerDenied("file too large")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise broker.BrokerDenied("invalid base64 content") from None
        if len(data) > MAX_FILE or not isinstance(digest, str) or digest != hashlib.sha256(data).hexdigest():
            raise broker.BrokerDenied("file content mismatch")
        total += len(data)
        if total > MAX_CONTENT:
            raise broker.BrokerDenied("manifest too large")
        parsed.append((path, data))
    for path in seen:
        parts = path.split("/")
        if any("/".join(parts[:n]) in seen for n in range(1, len(parts))):
            raise broker.BrokerDenied("file and directory conflict")
    return base, parsed


def _api(loop: dict, path: str, *, login: str, method: str = "GET", body=None) -> dict:
    result = gh.api(loop, path, method=method, body=body, login=login)
    if not isinstance(result, dict) or "message" in result and "documentation_url" in result:
        raise broker.BrokerDenied("GitHub read request failed")
    return result


def _audit(loop: dict, record: dict) -> None:
    """Durable metadata journal; failure aborts the write."""
    audit = Path(loop["state_dir"]) / "broker-audit.jsonl"
    if not audit.parent.is_dir() or audit.parent.is_symlink():
        raise broker.BrokerDenied("durable audit directory unavailable")
    dirfd = os.open(audit.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            line = (json.dumps({**record, "at": time.time()}, sort_keys=True) + "\n").encode()
            if os.write(fd, line) != len(line):
                raise OSError("short audit write")
            os.fsync(fd)
            # A new audit file's directory entry must survive a crash too.
            os.fsync(dirfd)
        finally:
            os.close(fd)
    finally:
        os.close(dirfd)


def _git_cas(loop: dict, repo: str, branch: str, head: str,
             files: list[tuple[str, bytes]], message: str, login: str,
             identity: dict, *, before_push=None, remote: str | None = None) -> str:
    """Fetch the advertised branch, construct local objects, and exact-lease push.

    `remote` is a private local-fixture seam, never sourced from IPC or config.
    Only validated manifest paths/bytes reach Git's temporary private bare repo.
    """
    url = config.guard_network(remote if remote is not None else f"https://github.com/{repo}.git")
    protocol = "file" if remote is not None else "https"
    with tempfile.TemporaryDirectory(prefix="review-loop-git-") as temp:
        root = Path(temp)
        os.chmod(root, 0o700)
        bare = root / "objects.git"
        askpass = root / "askpass.py"
        askpass.write_text("#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\n"
                           "print('x-access-token' if 'Username' in sys.argv[1] "
                           "else Path(os.environ['REVIEW_LOOP_TOKEN_FILE']).read_text().strip())\n")
        askpass.chmod(0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": temp, "XDG_CONFIG_HOME": temp,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass),
               "GIT_OPTIONAL_LOCKS": "0", "GIT_INDEX_FILE": str(root / "manifest.index"),
               "REVIEW_LOOP_TOKEN_FILE": str(gh.token_path(loop, login)), "LC_ALL": "C",
               "GIT_AUTHOR_NAME": identity["name"], "GIT_AUTHOR_EMAIL": identity["email"],
               "GIT_COMMITTER_NAME": identity["name"], "GIT_COMMITTER_EMAIL": identity["email"]}

        def run(*args: str, input: bytes | None = None) -> bytes:
            cmd = ["/usr/bin/git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
                   "-c", "commit.gpgsign=false", "-c", "protocol.allow=never",
                   "-c", f"protocol.{protocol}.allow=always", *args]
            try:
                result = subprocess.run(cmd, cwd=temp, env=env, input=input,
                                        capture_output=True, timeout=90, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise broker.BrokerDenied("isolated Git transport failed") from exc
            if result.returncode:
                # Git stderr can contain URLs and server-controlled text; never expose it.
                raise broker.BrokerDenied("isolated Git transport rejected operation")
            return result.stdout.strip()

        run("init", "--bare", "--template", str(root / "empty-template"), str(bare))
        # Fetch an ADVERTISED ref, never a dangling object ID. Verify the exact
        # snapshot before constructing anything or attempting a ref mutation.
        ref = f"refs/heads/{branch}"
        run("--git-dir", str(bare), "fetch", "--no-tags", "--no-recurse-submodules",
            url, f"{ref}:refs/heads/snapshot")
        snapshot = _sha(run("--git-dir", str(bare), "rev-parse", "refs/heads/snapshot").decode())
        if snapshot != head:
            raise broker.BrokerDenied("fetched PR branch moved")
        parents = run("--git-dir", str(bare), "rev-list", "--parents", "-n", "1", head).decode().split()
        if not parents or parents[0] != head:
            raise broker.BrokerDenied("invalid fetched head")
        run("--git-dir", str(bare), "read-tree", head)
        existing = {}
        for entry in run("--git-dir", str(bare), "ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                mode, _, stage = metadata.decode().split()
                if stage != "0":
                    raise broker.BrokerDenied("unmerged base tree")
                existing[path.decode("utf-8", errors="surrogateescape")] = mode
        for path, data in files:
            parts = path.split("/")
            if any("/".join(parts[:n]) in existing for n in range(1, len(parts))):
                raise broker.BrokerDenied("manifest traverses tracked file or symlink")
            if any(item.startswith(path + "/") for item in existing):
                raise broker.BrokerDenied("manifest replaces tracked directory")
            if path in existing and existing[path] not in ("100644", "100755"):
                raise broker.BrokerDenied("manifest replaces nonregular file")
            blob = _sha(run("--git-dir", str(bare), "hash-object", "-w", "--stdin",
                            input=data).decode())
            # Keep an edited script executable; a new file is a plain file.
            mode = existing.get(path, "100644")
            run("--git-dir", str(bare), "update-index", "--add", "--cacheinfo",
                f"{mode},{blob},{path}")
        tree = _sha(run("--git-dir", str(bare), "write-tree").decode())
        base_tree = _sha(run("--git-dir", str(bare), "rev-parse", f"{head}^{{tree}}").decode())
        if tree == base_tree:
            raise broker.BrokerDenied("push has no changes")
        new_head = _sha(run("--git-dir", str(bare), "commit-tree", tree, "-p", head,
                            input=message.encode("utf-8") + b"\n").decode())
        created = run("--git-dir", str(bare), "rev-list", "--parents", "-n", "1", new_head).decode().split()
        if created != [new_head, head]:
            raise broker.BrokerDenied("local commit does not have exact expected parent")
        if before_push is not None:
            before_push(new_head)
        run("--git-dir", str(bare), "push", "--porcelain",
            f"--force-with-lease={ref}:{head}", url, f"{new_head}:{ref}")
        return new_head

def push(loop: dict, *, repo: str, number: int, head: str, role: str,
         branch: str, manifest: object) -> dict:
    """Create Git objects and lease-advance only the gate-scoped PR branch.

    The trusted caller supplies scope from the gate, never from the manifest. All
    repository/ref API paths are derived from this scope after live authorization.
    GitHub API calls here are read-only; the only remote write is a leased Git push.
    The lease protects the branch SHA, not PR metadata: a close/retarget/draft
    transition after the final PR read and before receive-pack remains possible.
    """
    base, files = _manifest(manifest)
    assert isinstance(manifest, dict)  # _manifest rejects any other shape
    if not config.unattended_fixer_push_enabled(loop):
        raise broker.BrokerDenied("unattended fixer push disabled")
    if base != head:
        raise broker.BrokerDenied("manifest base differs from scoped PR head")
    login = broker.authorize(loop, repo=repo, number=number, head=head,
                             role=role, branch=branch, operation="push")
    if not isinstance(branch, str) or len(branch) > 200 or not all(
            SEGMENT.fullmatch(part) and not part.startswith(".") and not part.endswith(".")
            and ".." not in part and not part.endswith(".lock")
            for part in branch.split("/")):
        raise broker.BrokerDenied("unsafe branch ref")
    root = f"/repos/{repo}/git"
    ref_path = f"{root}/ref/heads/{quote(branch, safe='/')}"

    def check_ref() -> None:
        ref = _api(loop, ref_path, login=login)
        if ref.get("ref") != f"refs/heads/{branch}" or _sha((ref.get("object") or {}).get("sha")) != head:
            raise broker.BrokerDenied("PR branch moved")

    check_ref()
    # Resolve the authenticated fixer before attributing the local commit.
    seat = _api(loop, "/user", login=login)
    if (not isinstance(seat.get("login"), str) or seat["login"].casefold() != login.casefold()
            or type(seat.get("id")) is not int or seat["id"] <= 0):
        raise broker.BrokerDenied("fixer identity changed")
    identity = {"name": seat["login"],
                "email": f"{seat['id']}+{seat['login']}@users.noreply.github.com"}
    if not re.fullmatch(r"[A-Za-z0-9-]+", identity["name"]):
        raise broker.BrokerDenied("invalid fixer identity")
    # Refresh BOTH PR identity and branch immediately before ref mutation.
    broker.authorize(loop, repo=repo, number=number, head=head,
                     role=role, branch=branch, operation="push")
    check_ref()
    # The SHA is known after local construction, before the only remote mutation.
    receipt = {"repo": repo, "pr": number, "old_head": head,
               "branch": branch, "role": role, "login": login,
               "paths": [path for path, _ in files], "operation": "push"}
    error = None
    new_head = None
    attempt_started = False
    def before_push(created: str) -> None:
        nonlocal new_head, attempt_started
        # Object construction/fetch may take time; the initial PR check cannot
        # authorize a later write. Recheck as close to the Git push as possible.
        broker.authorize(loop, repo=repo, number=number, head=head,
                         role=role, branch=branch, operation="push")
        check_ref()
        attempt_started = True
        _audit(loop, {**receipt, "new_head": created, "phase": "attempt"})
        new_head = created
    try:
        _git_cas(loop, repo, branch, head, files, manifest["message"], login, identity,
                 before_push=before_push)
    except Exception as exc:
        error = exc
    outcome = "unknown"
    try:
        # Read back independently even on timeout, rejection, or lost response.
        try:
            ref = _api(loop, ref_path, login=login)
            observed = _sha((ref.get("object") or {}).get("sha")) if ref.get("ref") == f"refs/heads/{branch}" else None
        except Exception:
            observed = None
        outcome = "published" if new_head is not None and observed == new_head else ("unchanged" if observed == head else "unknown")
        if outcome == "published" and new_head is not None:
            try:
                # Git's lease verifies only the ref. The PR may have closed during
                # receive-pack without changing that ref; never acknowledge it as
                # a successful authorized push in that case.
                broker.authorize(loop, repo=repo, number=number, head=new_head,
                                 role=role, branch=branch, operation="push",
                                 require_verdict=False)
            except Exception:
                outcome = "published_pr_unverified"
        _audit(loop, {**receipt, "new_head": new_head, "phase": "reconciled",
                      "outcome": outcome, "observed_head": observed})
    except Exception as exc:
        if attempt_started:
            raise PushFailure(outcome) from exc
        raise
    if error is not None or outcome != "published":
        failure = PushFailure(outcome) if attempt_started else broker.BrokerDenied(
            f"Git ref update not confirmed ({outcome})")
        raise failure from error
    return {**receipt, "new_head": new_head, "outcome": outcome}
