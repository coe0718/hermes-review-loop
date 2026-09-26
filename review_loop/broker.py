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
from .wire import ANSWERS_MARKER  # one home, shared with the sandbox client

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
    if role not in ("reviewer", "fixer") or operation not in ("review", "request_review", "push",
                                                              "answers"):
        raise BrokerDenied("unsupported role or operation")
    if (role, operation) not in (("reviewer", "review"), ("fixer", "request_review"), ("fixer", "push"),
                                 ("fixer", "answers")):
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


# A reviewer write must move the loop: an approval cues the merge, changes-requested wakes the
# fixer. A COMMENT is neither, so it would spend the reviewer's one write and stall the PR.
REVIEW_VERDICTS = ("APPROVE", "REQUEST_CHANGES")


def perform(loop: dict, *, repo: str, number: int, head: str, role: str,
            branch: str, operation: str, verdict: str = "", body: str = "",
            require_verdict: bool = True) -> object:
    """One allowlisted REST write, with server-side destination and reviewer selection.

    For GitHub review submissions, commit_id pins the review to the exact checked head.
    This does not implement push (which needs a separate safe, transactional design).
    ``require_verdict=False`` is only for a fixer's request after its own confirmed push:
    the verdict was checked at the old head before the push, and the new head cannot have one.
    """
    login = authorize(loop, repo=repo, number=number, head=head, role=role,
                      branch=branch, operation=operation, require_verdict=require_verdict)
    if operation == "review":
        if verdict not in REVIEW_VERDICTS or not body.strip():
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


# The fixer's answers to the findings: one bounded PR comment, posted by the host as the fixer
# identity after a confirmed push (#52). The hidden marker is how a later record tells these from
# any other comment: it is only trusted on a comment authored by the fixer seat's own login, and
# even then the text is the fixer model's words — data for the next seat, never instructions.
ANSWERS_MAX = 8 * 1024
_ANSWERS_MARKER = re.compile(re.escape(ANSWERS_MARKER) + r" run=([A-Za-z0-9_.:-]{1,80}) "
                             r"head=([0-9a-f]{40}) base=([0-9a-f]{40}) -->\n")


def answers_valid(text: object) -> bool:
    """Non-empty text within the bound, with no NUL and no marker of its own."""
    return (isinstance(text, str) and bool(text.strip()) and "\x00" not in text
            and len(text.encode()) <= ANSWERS_MAX and ANSWERS_MARKER not in text)


def answers_comment_body(text: str, *, head: str, base: str, run_id: str) -> str:
    return (f"{ANSWERS_MARKER} run={run_id} head={head} base={base} -->\n"
            f"**Fixer's answers to the review of `{base[:12]}`** — pushed as `{head[:12]}`\n\n"
            f"{text.strip()}\n\n"
            "_Posted by the review loop's host for the fixer seat; the fixer's own words, "
            "not verified by the loop._")


def parse_answers_comment(comment: object, loop: dict) -> dict | None:
    """``{run, head, base, body, created_at}`` for a fixer-answers comment, else ``None``.

    Both must hold: the author is the configured fixer seat login, and the body starts with the
    host's marker. A human (or anyone else) writing the marker is not the fixer seat; the fixer
    seat writing prose without it is not an answers record.
    """
    if not isinstance(comment, dict):
        return None
    login = ((loop.get("seats") or {}).get("fixer") or {}).get("login")
    user = comment.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    body = comment.get("body")
    if (not isinstance(login, str) or not login or not isinstance(author, str)
            or author.casefold() != login.casefold() or not isinstance(body, str)):
        return None
    match = _ANSWERS_MARKER.match(body)
    if not match:
        return None
    return {"run": match.group(1), "head": match.group(2), "base": match.group(3),
            "body": body[match.end():], "created_at": str(comment.get("created_at") or "")}


def post_fixer_answers(loop: dict, *, repo: str, number: int, head: str, branch: str,
                       login: str, text: str) -> int:
    """POST one issue comment as the fixer identity; return its id or raise. Never retried."""
    result = gh.api(loop, f"/repos/{repo}/issues/{number}/comments", method="POST",
                    body={"body": text}, login=login)
    if not isinstance(result, dict) or type(result.get("id")) is not int:
        raise BrokerDenied("GitHub comment write did not return a successful response")
    _audit(loop, repo, number, head, branch, "fixer", "answers", login)
    return result["id"]


def authorize_ruling_comment(loop: dict, *, repo: str, number: int, head: str,
                             branch: str) -> str:
    """Return the adjudicator's comment login, or deny; never touches the seats' write paths.

    A sibling of :func:`authorize`, not a relaxation of it: the reviewer/fixer role and
    operation table there is unchanged. Only an *optional* ``seats.adjudicator.login`` can
    comment, and only if it is a fourth identity — distinct login, distinct token file and a
    distinct ``/user`` principal from the reader and both seats — at a live PR that is still the
    open, non-draft, same-repository, unapproved head the ruling was made on.
    """
    from . import config, gate
    if not _REPO.fullmatch(repo) or repo != loop.get("repo"):
        raise BrokerDenied("wrong repository")
    if type(number) is not int or number <= 0 or not _SHA.fullmatch(head):
        raise BrokerDenied("invalid PR number or head SHA")
    login = config.adjudicator_login(loop)
    if not login:
        raise BrokerDenied("no adjudicator identity configured")
    seats = loop.get("seats") or {}
    reader = loop.get("read_token")
    reviewer = (seats.get("reviewer") or {}).get("login")
    fixer = (seats.get("fixer") or {}).get("login")
    identities = (reader, reviewer, fixer, login)
    if (not all(isinstance(i, str) and i for i in identities)
            or len({i.casefold() for i in identities}) != 4):
        raise BrokerDenied("read, reviewer, fixer and adjudicator must use distinct identities")
    mapped = [gh.token_path(loop, identity) for identity in identities]
    if any(path is None for path in mapped):
        raise BrokerDenied("read, reviewer, fixer and adjudicator token mappings required")
    if len({path.resolve() for path in mapped if path is not None}) != 4:
        raise BrokerDenied("read, reviewer, fixer and adjudicator must use distinct token files")
    principals = []
    for identity in identities:
        try:
            if not gh.token(loop, identity):
                raise BrokerDenied(f"empty token for {identity}")
        except gh.GitHubError as exc:
            raise BrokerDenied(f"missing token for {identity}") from exc
        account = gh.api(loop, "/user", login=identity)
        if (not isinstance(account, dict) or type(account.get("id")) is not int
                or not isinstance(account.get("login"), str)
                or account["login"].casefold() != identity.casefold()):
            raise BrokerDenied("token principal cannot be verified")
        principals.append(account["id"])
    if len(set(principals)) != 4:
        raise BrokerDenied("adjudicator token resolves to the same principal as another identity")
    current = gh.api(loop, f"/repos/{repo}/pulls/{number}", login=reader)
    if not isinstance(current, dict):
        raise BrokerDenied("cannot verify live PR")
    pr_head = current.get("head") or {}
    pr_base = current.get("base") or {}
    if (type(current.get("number")) is not int or current["number"] != number
            or current.get("state") != "open" or current.get("draft") is not False):
        raise BrokerDenied("PR identity, state or draft status changed")
    if (pr_base.get("repo") or {}).get("full_name") != repo or pr_base.get("ref") != loop.get("base"):
        raise BrokerDenied("PR base changed")
    if pr_head.get("sha") != head:
        raise BrokerDenied("stale PR head")
    if pr_head.get("ref") != branch or (pr_head.get("repo") or {}).get("full_name") != repo:
        raise BrokerDenied("PR head branch or repository changed")
    user = current.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    if not isinstance(author, str) or author.casefold() not in {
            f.casefold() for f in loop.get("fixers") or () if isinstance(f, str)}:
        raise BrokerDenied("PR author is not an authorized fixer")
    reviews = gh.reviews(loop, number)
    if not isinstance(reviews, list):
        raise BrokerDenied("cannot verify the head is still unapproved")
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if latest is not None and gh.review_state(latest) == "APPROVED":
        raise BrokerDenied("head was approved after the breach")
    return login


def ruling_comment_body(verdict: str, body: str, *, head: str, turn_key: str, run_id: str,
                        cap: object) -> str:
    rounds = turn_key.split(":", 1)[1] if turn_key.startswith("breach:") else "?"
    return (f"**Adjudicator ruling: {verdict}**\n\n"
            f"Head `{head}` · verdicts counted: {rounds} of {cap} · run `{run_id}`\n\n"
            f"{body.strip()}\n\n"
            "_The adjudicator does not merge, push or review. The operator decides._")


def post_ruling_comment(loop: dict, *, repo: str, number: int, head: str, branch: str,
                        login: str, text: str) -> int:
    """POST one issue comment as the adjudicator identity; return its id or raise."""
    result = gh.api(loop, f"/repos/{repo}/issues/{number}/comments", method="POST",
                    body={"body": text}, login=login)
    if not isinstance(result, dict) or type(result.get("id")) is not int:
        raise BrokerDenied("GitHub comment write did not return a successful response")
    _audit(loop, repo, number, head, branch, "adjudicator", "ruling_comment", login)
    return result["id"]
