"""Trusted, narrow GitHub REST broker primitives for broker_ipc's scoped UDS.

These operations run only in the credential-owning control plane. A launcher can
mount only a per-run socket capability into the agent, not credentials or config.
The gate still blocks agent dispatch until that launcher is wired in.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import re
import time

from . import gh

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class BrokerDenied(Exception):
    """Fail closed: the requested operation is not authorized at this PR head."""


def authorize(loop: dict, *, repo: str, number: int, head: str, role: str,
              branch: str, operation: str, require_verdict: bool = True) -> str:
    """Return the seat login only after checking exact live PR identity and credentials.

    No caller-supplied path, URL, token, reviewer or destination enters an API request.
    A missing explicit seat/read mapping is denied even in stubbed tests. Fetch a fresh
    PR immediately before EACH write; the push path separately enforces exact-head CAS.
    """
    if not _REPO.fullmatch(repo) or repo != loop.get("repo"):
        raise BrokerDenied("wrong repository")
    if type(number) is not int or number <= 0 or not _SHA.fullmatch(head):
        raise BrokerDenied("invalid PR number or head SHA")
    if role not in ("reviewer", "fixer") or operation not in ("review", "request_review", "push"):
        raise BrokerDenied("unsupported role or operation")
    if (role, operation) not in (("reviewer", "review"), ("fixer", "request_review"), ("fixer", "push")):
        raise BrokerDenied("operation not permitted for role")
    login = ((loop.get("seats") or {}).get(role) or {}).get("login")
    reader = loop.get("read_token")
    if not login or not reader or not gh.token_path(loop, login) or not gh.token_path(loop, reader):
        raise BrokerDenied("explicit seat and read token mappings required")
    seats = loop.get("seats") or {}
    reviewer = (seats.get("reviewer") or {}).get("login")
    fixer = (seats.get("fixer") or {}).get("login")
    if not reviewer or not fixer or len({reviewer, fixer, reader}) != 3:
        raise BrokerDenied("read, reviewer and fixer must use distinct identities")
    mapped = [gh.token_path(loop, identity) for identity in (reader, reviewer, fixer)]
    if any(path is None for path in mapped):
        raise BrokerDenied("read, reviewer and fixer token mappings required")
    if len({path.resolve() for path in mapped if path is not None}) != 3:
        raise BrokerDenied("read, reviewer and fixer must use distinct token files")
    principals = []
    for identity in (reader, reviewer, fixer):
        try:
            if not gh.token(loop, identity):
                raise BrokerDenied(f"empty token for {identity}")
        except gh.GitHubError as exc:
            raise BrokerDenied(f"missing token for {identity}") from exc
        account = gh.api(loop, "/user", login=identity)
        if not isinstance(account, dict) or not isinstance(account.get("id"), int) or type(account.get("id")) is bool or not isinstance(account.get("login"), str) or account["login"].casefold() != identity.casefold():
            raise BrokerDenied("seat token principal cannot be verified")
        principals.append(account["id"])
    if len(set(principals)) != 3:
        raise BrokerDenied("read, reviewer and fixer tokens resolve to same principal")
    current = gh.api(loop, f"/repos/{repo}/pulls/{number}", login=reader)
    if not isinstance(current, dict):
        raise BrokerDenied("cannot verify live PR")
    pr_head = current.get("head") or {}
    pr_base = current.get("base") or {}
    # An omitted or wrong-shaped draft flag is unknown, never an eligible PR.
    # The same check runs before every REST write and at each push checkpoint.
    if (type(current.get("number")) is not int or current["number"] != number
            or current.get("state") != "open" or current.get("draft") is not False):
        raise BrokerDenied("PR identity, state or draft status changed")
    if (pr_base.get("repo") or {}).get("full_name") != repo:
        raise BrokerDenied("PR base repository mismatch")
    if pr_base.get("ref") != loop.get("base"):
        raise BrokerDenied("PR base branch mismatch")
    if pr_head.get("sha") != head:
        raise BrokerDenied("stale PR head")
    if pr_head.get("ref") != branch or not branch or branch.startswith("-"):
        raise BrokerDenied("wrong destination branch")
    if (pr_head.get("repo") or {}).get("full_name") != repo:
        raise BrokerDenied("fork head not permitted for credentialed writes")
    if role == "fixer":
        user = current.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        allowed = loop.get("fixers")
        if (not isinstance(author, str) or not isinstance(allowed, list)
                or author.casefold() not in {login.casefold() for login in allowed
                                             if isinstance(login, str) and login}):
            raise BrokerDenied("PR author is not an authorized fixer")
        if require_verdict:
            from . import gate
            reviews = gh.reviews(loop, number)
            if not isinstance(reviews, list):
                raise BrokerDenied("cannot verify latest fixer verdict")
            latest = gate.latest_effective_review_at_head(reviews, loop, head)
            if latest is None or gh.review_state(latest) != "CHANGES_REQUESTED":
                raise BrokerDenied("fixer verdict no longer current")
    return login


def perform(loop: dict, *, repo: str, number: int, head: str, role: str,
            branch: str, operation: str, verdict: str = "", body: str = "") -> object:
    """One allowlisted REST write, with server-side destination and reviewer selection.

    For GitHub review submissions, commit_id pins the review to the exact checked head.
    This does not implement push (which needs a separate safe, transactional design).
    """
    login = authorize(loop, repo=repo, number=number, head=head, role=role,
                      branch=branch, operation=operation)
    if operation == "review":
        if verdict not in ("APPROVE", "REQUEST_CHANGES", "COMMENT") or not body.strip():
            raise BrokerDenied("invalid review verdict or empty body")
        path = f"/repos/{repo}/pulls/{number}/reviews"
        payload = {"commit_id": head, "event": verdict, "body": body}
    else:
        if verdict or body:
            raise BrokerDenied("unexpected request-review fields")
        seat = loop.get("reviewer_seat")
        if not seat or seat != ((loop.get("seats") or {}).get("reviewer") or {}).get("login"):
            raise BrokerDenied("reviewer seat mapping missing or inconsistent")
        path = f"/repos/{repo}/pulls/{number}/requested_reviewers"
        payload = {"reviewers": [seat]}
    result = gh.api(loop, path, method="POST", body=payload, login=login)
    if not isinstance(result, dict) or result.get("message") and result.get("documentation_url"):
        raise BrokerDenied("GitHub write did not return a successful response")
    _audit(loop, repo, number, head, branch, role, operation, login)
    return result


def _audit(loop: dict, repo: str, number: int, head: str, branch: str,
           role: str, operation: str, login: str) -> None:
    # Metadata only; never token or model-produced body. Lock an append-only file.
    path = pathlib.Path(loop["state_dir"]) / "broker-audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = {"at": time.time(), "repo": repo, "pr": number, "head": head,
                  "branch": branch, "role": role, "operation": operation, "login": login}
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
