"""Bounded, credential-owning GitHub PR export into an unpublished checkout.

The caller must mount only the returned directory into a separately contained run.
A same-UID process is not isolated by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
import ctypes
import urllib.error
import urllib.request

from . import gh

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MAX_FILES = 10_000
_MAX_BYTES = 100 * 1024 * 1024
_MAX_TREE_RESPONSE = 16 * 1024 * 1024
_MAX_METADATA_RESPONSE = 256 * 1024
_MAX_PATH_BYTES = 4096
_CHUNK = 64 * 1024


class FetchDenied(Exception):
    """The requested head cannot safely be staged."""

def _publish_exclusive(private: pathlib.Path, root: pathlib.Path) -> None:
    """Atomically publish a staged directory without replacing an existing one."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise FetchDenied("exclusive sandbox publish unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(private), -100, os.fsencode(root), 1) != 0:
        raise FetchDenied(f"sandbox publish failed (errno={ctypes.get_errno()})")


def _request(loop: dict, path: str, login: str, limit: int, accept: str) -> bytes:
    """Read at most limit+1 bytes, including for chunked and dishonest responses."""
    try:
        credential = gh.token(loop, login)
        req = urllib.request.Request(
            f"{gh.API}{path}", headers={"Accept": accept,
                "Authorization": f"Bearer {credential}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hermes-review-loop"})
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                raise FetchDenied("GitHub response unavailable")
            if int(response.headers.get("Content-Length", "0")) > limit:
                raise FetchDenied("GitHub response exceeds bounds")
            chunks, remaining = [], limit + 1
            while remaining:
                chunk = response.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining == 0:
                raise FetchDenied("GitHub response exceeds bounds")
            return b"".join(chunks)
    except (OSError, ValueError, urllib.error.URLError, gh.GitHubError) as exc:
        raise FetchDenied("GitHub response unavailable") from exc


def _json(loop: dict, path: str, login: str, limit: int = _MAX_METADATA_RESPONSE):
    try:
        return json.loads(_request(loop, path, login, limit, "application/vnd.github+json"))
    except (ValueError, UnicodeError) as exc:
        raise FetchDenied("invalid GitHub response") from exc


def _identity(loop: dict, repo: str, number: int, head: str, ref: str, role: str) -> str:
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo != loop.get("repo"):
        raise FetchDenied("repository mismatch")
    if type(number) is not int or number <= 0 or not isinstance(head, str) or not _SHA.fullmatch(head):
        raise FetchDenied("invalid PR identity")
    if role not in ("reviewer", "fixer") or not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise FetchDenied("invalid role or ref")
    seats = loop.get("seats") or {}
    reader = loop.get("read_token")
    reviewer = (seats.get("reviewer") or {}).get("login")
    fixer = (seats.get("fixer") or {}).get("login")
    if not all(isinstance(x, str) and x for x in (reader, reviewer, fixer)):
        raise FetchDenied("explicit, distinct read and seat identities required")
    assert isinstance(reader, str) and isinstance(reviewer, str) and isinstance(fixer, str)
    if len({reader.casefold(), reviewer.casefold(), fixer.casefold()}) != 3:
        raise FetchDenied("explicit, distinct read and seat identities required")
    paths = [gh.token_path(loop, x) for x in (reader, reviewer, fixer)]
    if any(p is None for p in paths) or len({p.resolve() for p in paths if p is not None}) != 3:
        raise FetchDenied("distinct token files required")
    # Distinct filenames are not distinct principals; verify every actual credential.
    for login in (reader, reviewer, fixer):
        assert isinstance(login, str)
        user = _json(loop, "/user", login)
        if not isinstance(user, dict) or not isinstance(user.get("login"), str) or user["login"].casefold() != login.casefold():
            raise FetchDenied("credential principal mismatch")
    assert isinstance(reader, str)
    return reader


def _live_head(loop: dict, repo: str, number: int, head: str, ref: str, reader: str) -> None:
    pr = _json(loop, f"/repos/{repo}/pulls/{number}", reader)
    if not isinstance(pr, dict) or pr.get("number") != number or pr.get("state") != "open":
        raise FetchDenied("PR unavailable or closed")
    base, current = pr.get("base"), pr.get("head")
    if not isinstance(base, dict) or not isinstance(current, dict):
        raise FetchDenied("invalid PR repository or ref")
    base_repo, current_repo = base.get("repo"), current.get("repo")
    if not isinstance(base_repo, dict) or not isinstance(current_repo, dict):
        raise FetchDenied("invalid PR repository or ref")
    if (base_repo.get("full_name") != repo or base.get("ref") != loop.get("base")
            or current_repo.get("full_name") != repo or current.get("ref") != ref):
        raise FetchDenied("PR repository, base or ref changed")
    if current.get("sha") != head:
        raise FetchDenied("stale PR head")


def _entries(tree: dict) -> list[tuple[str, str, int, bool]]:
    if not isinstance(tree, dict) or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise FetchDenied("truncated or invalid tree")
    entries, total, seen, files, parents = [], 0, set(), set(), set()
    for item in tree["tree"]:
        if not isinstance(item, dict):
            raise FetchDenied("unsafe tree entry")
        name, mode, kind, oid, length = (item.get(key) for key in ("path", "mode", "type", "sha", "size"))
        if not isinstance(name, str):
            raise FetchDenied("unsafe tree entry")
        parts = name.split("/")
        try:
            path_length = len(name.encode("utf-8"))
        except UnicodeError as exc:
            raise FetchDenied("unsafe tree entry") from exc
        if (path_length > _MAX_PATH_BYTES
                or any(not p or p in (".", "..") or p.casefold() in (".git", ".gitmodules")
                       or "\\" in p or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts)
                or name in seen or (name in parents and kind != "tree")
                or any("/".join(parts[:index]) in files
                                          for index in range(1, len(parts)))):
            raise FetchDenied("unsafe tree entry")
        seen.add(name)
        if len(seen) > _MAX_FILES:
            raise FetchDenied("tree exceeds export bounds")
        parents.update("/".join(parts[:index]) for index in range(1, len(parts)))
        if kind == "tree" and mode == "040000":
            continue
        if (kind != "blob" or mode not in ("100644", "100755")
                or not isinstance(oid, str) or not _SHA.fullmatch(oid)
                or type(length) is not int or length < 0):
            raise FetchDenied("unsafe tree entry")
        total += length
        files.add(name)
        entries.append((name, oid, length, mode == "100755"))
        if total > _MAX_BYTES:
            raise FetchDenied("tree exceeds export bounds")
    return entries


def _export_blob(loop: dict, repo: str, reader: str, oid: str, length: int, target: pathlib.Path) -> None:
    raw = _request(loop, f"/repos/{repo}/git/blobs/{oid}", reader, length,
                   "application/vnd.github.raw+json")
    if len(raw) != length or hashlib.sha1(b"blob " + str(length).encode() + b"\0" + raw).hexdigest() != oid:
        raise FetchDenied("blob size or hash mismatch")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)


def _stage(loop: dict, *, repo: str, number: int, head: str, ref: str,
           role: str, sandbox_root: pathlib.Path) -> pathlib.Path:
    reader = _identity(loop, repo, number, head, ref, role)
    root = pathlib.Path(sandbox_root).absolute()
    token_paths = [path.resolve() for login in
                   (reader, loop["seats"]["reviewer"]["login"], loop["seats"]["fixer"]["login"])
                   if (path := gh.token_path(loop, login)) is not None]
    if (root.exists() or root.is_symlink() or root.parent.resolve() != root.parent
            or any(root == token or root in token.parents for token in token_paths)):
        raise FetchDenied("sandbox root exists, follows a symlink or overlaps credential")
    _live_head(loop, repo, number, head, ref, reader)
    commit = _json(loop, f"/repos/{repo}/git/commits/{head}", reader)
    if not isinstance(commit, dict) or commit.get("sha") != head:
        raise FetchDenied("commit SHA mismatch")
    commit_tree = commit.get("tree")
    if not isinstance(commit_tree, dict):
        raise FetchDenied("invalid commit tree")
    tree_sha = commit_tree.get("sha")
    if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
        raise FetchDenied("invalid commit tree")
    tree = _json(loop, f"/repos/{repo}/git/trees/{tree_sha}?recursive=1", reader, _MAX_TREE_RESPONSE)
    if not isinstance(tree, dict) or tree.get("sha") != tree_sha:
        raise FetchDenied("tree SHA mismatch")
    entries = _entries(tree)
    _live_head(loop, repo, number, head, ref, reader)
    # Sibling on the same filesystem: none of the partial export is visible at root.
    with tempfile.TemporaryDirectory(prefix=".review-trusted-", dir=root.parent) as temp:
        private = pathlib.Path(temp)
        private.chmod(0o700)
        directory = private / "repo"
        directory.mkdir(mode=0o700)
        for name, oid, length, executable in entries:
            target = directory.joinpath(*name.split("/"))
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _export_blob(loop, repo, reader, oid, length, target)
            target.chmod(0o755 if executable else 0o644)
        _live_head(loop, repo, number, head, ref, reader)
        if root.exists() or root.is_symlink():
            raise FetchDenied("sandbox root appeared during staging")
        _publish_exclusive(private, root)
    return root / "repo"


def stage(loop: dict, *, repo: str, number: int, head: str, ref: str,
          role: str, sandbox_root: pathlib.Path) -> pathlib.Path:
    """Stage an exact same-repository PR head; return a credentialless checkout path."""
    return _stage(loop, repo=repo, number=number, head=head, ref=ref, role=role,
                  sandbox_root=sandbox_root)
