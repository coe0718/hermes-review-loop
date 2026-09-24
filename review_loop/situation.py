"""Resolve the *diff* a review is about, not merely its child commit.

An unresolved base is never interpreted as trunk. The returned identity is useful for
persisting verdict associations, but a caller must still enforce parent readiness and
re-read the situation immediately before any side effect.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from . import gh

SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
MAX_PARENT_DEPTH = 20


@dataclass(frozen=True)
class Identity:
    head_sha: str
    base_ref: str
    base_sha: str
    parents: tuple[tuple[int, str, str, str], ...]

    @property
    def key(self) -> str:
        fields = [self.head_sha, self.base_ref, self.base_sha, self.parents]
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Resolution:
    status: str  # eligible, waiting, blocked
    reason: str
    identity: Identity | None = None
    parents: tuple[int, ...] = ()


def _repo(part: dict) -> str:
    return ((part.get("repo") or {}).get("full_name") or "") if isinstance(part, dict) else ""


def _fields(pr: dict, repo: str) -> tuple[str, str, str, str] | None:
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if (not isinstance(head, dict) or not isinstance(base, dict)
            or _repo(head) != repo or _repo(base) != repo):
        return None
    branch, head_sha = head.get("ref"), head.get("sha")
    base_ref, base_sha = base.get("ref"), base.get("sha")
    if (not isinstance(branch, str) or not branch or not isinstance(base_ref, str)
            or not base_ref or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha)
            or not isinstance(base_sha, str) or not SHA.fullmatch(base_sha)):
        return None
    return branch, head_sha.lower(), base_ref, base_sha.lower()


def resolve(loop: dict, number: int) -> Resolution:
    """Read child and bounded open-parent listing; ambiguity or inconsistent SHA blocks."""
    repo = loop["repo"]
    child = gh.pr(loop, number)
    if (not isinstance(child, dict) or child.get("number") != number
            or child.get("state") != "open"):
        return Resolution("blocked", "child PR unavailable or not open")
    child_fields = _fields(child, repo)
    if child_fields is None:
        return Resolution("blocked", "child base/head SHA or repository unverified")
    _, child_sha, base_ref, base_sha = child_fields
    if base_ref == loop["base"]:
        return Resolution("eligible", "direct trunk base",
                          Identity(child_sha, base_ref, base_sha, ()))

    listing, error = gh.open_prs_read(loop)
    if error or listing is None:
        return Resolution("blocked", f"parent list unreadable: {error or 'unknown response'}")
    seen = {number}
    chain: list[tuple[int, str, str, str]] = []
    current_ref, current_sha = base_ref, base_sha
    for _ in range(MAX_PARENT_DEPTH):
        matches = [p for p in listing if isinstance(p.get("head"), dict)
                   and p["head"].get("ref") == current_ref]
        if not matches:
            return Resolution("blocked", f"missing open parent for branch {current_ref}")
        if len(matches) != 1:
            return Resolution("blocked", f"ambiguous parent branch {current_ref}")
        parent = matches[0]
        parent_number = parent.get("number")
        if not isinstance(parent_number, int) or parent_number <= 0:
            return Resolution("blocked", "parent number unverified")
        if parent_number in seen:
            return Resolution("blocked", f"cycle at parent #{parent_number}")
        live_parent = gh.pr(loop, parent_number)
        if (not isinstance(live_parent, dict) or live_parent.get("number") != parent_number
                or live_parent.get("state") != "open"):
            return Resolution("blocked", f"parent #{parent_number} unreadable or closed")
        fields = _fields(live_parent, repo)
        if fields is None:
            return Resolution("blocked", f"foreign or unverified parent #{parent_number}")
        branch, head_sha, next_ref, next_sha = fields
        if branch != current_ref or _fields(parent, repo) != fields:
            return Resolution("blocked", f"parent #{parent_number} changed during resolution")
        if head_sha != current_sha:
            return Resolution("blocked", f"parent #{parent_number} advanced beyond child base SHA")
        seen.add(parent_number)
        chain.append((parent_number, branch, head_sha, next_sha))
        if next_ref == loop["base"]:
            return Resolution("waiting", f"waiting on #{chain[0][0]}",
                              Identity(child_sha, base_ref, base_sha, tuple(chain)),
                              tuple(item[0] for item in chain))
        current_ref, current_sha = next_ref, next_sha
    return Resolution("blocked", f"parent chain exceeds {MAX_PARENT_DEPTH} levels")
