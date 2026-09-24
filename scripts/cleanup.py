#!/usr/bin/env python3
"""Local cleanup — give a finished PR's disk back.

A review is expensive locally: a checkout per head, a cargo target per battery, probe logs,
sometimes several GB per round. A merged PR leaves all of it behind and nothing else in the
world removes it (one merged PR in the repo this was built on was holding 8 GB).

Two modes:

* ``--pr N`` — the merge/close path, run from the gate when a PR closes. Removes that PR's
  loop state (locks, queue, in-flight marks, breach marker) and its local paths.
* ``--sweep`` — every PR that is already closed, for the backlog a loop accumulated before
  it had a cleanup path. Add ``--dry-run`` to see the bill without paying it.

Safety rails, because this deletes real directories:

* only paths inside non-symlink configured roots or the artifacts directory are considered;
* only detached worktrees registered to this clone are removable, and only inside those roots;
  other Git repositories and nested checkouts are protected;
* a worktree with a **branch** checked out is never touched — that is somebody's working
  tree, not a review artifact (only detached review checkouts are cleaned);
* evidence patterns (``phase3``, ``evidence``, ``soak``, ``release-verification``) are
  skipped outright: regenerable build output is not the same thing as a receipt;
* only a fresh lookup confirming this PR is closed permits cleanup; ``--force`` is an
  explicit operator override for open or unavailable/malformed lookup results;
* the clone itself, and anything outside the roots, is out of scope by construction.

Cleanup is not a transactional lock on the filesystem: another writer can still
replace files inside an owned directory during traversal. Do not run it against
roots concurrently modified by an untrusted process.

The report prints **file bytes removed** (``du``), which is not the same number as disk
recovered: on a compressed or reflink-sharing volume the filesystem gains less. Quote the
``df`` delta when the distinction matters.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import config, gh, state as state_mod  # noqa: E402
from review_loop.util import human

# A CLI's report belongs on stdout: the cron that runs a sweep delivers it, and a gate that
# runs the merge path captures it. (The gates' own logging stays on stderr, where a protocol
# lives on stdout — this script is not a gate.)
def log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message)

# A path is attributable to a PR when it carries a "pr<number>" token with a boundary on both
# sides, so pr1 can never claim pr151. The separator is optional on purpose: reviews in the
# wild produce attest-pr152, pr152-wt and target-pr162-465aa9 alike.
PR_IN_PATH = re.compile(r"(?:^|[^0-9a-z])pr[_-]?([0-9]{1,6})(?![0-9])", re.I)
NEVER_TOUCH = re.compile(r"(phase3|phase4-evidence|evidence|soak|release-verification)", re.I)


def pr_from_path(path: str) -> int | None:
    hits = PR_IN_PATH.findall(str(path))
    return int(hits[-1]) if hits else None


def overlaps_branch_worktree(path: pathlib.Path, protected: set[pathlib.Path]) -> bool:
    """Never delete a branch checkout, anything inside it, or its enclosing directory."""
    candidate = path.resolve()
    return any(candidate == branch or candidate in branch.parents or branch in candidate.parents
               for branch in protected)


def worktrees(clone: pathlib.Path) -> list[dict]:
    try:
        proc = subprocess.run(["git", "-C", str(clone), "worktree", "list", "--porcelain"],
                              capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    trees: list[dict] = []
    cur: dict | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line.split(" ", 1)[1], "branch": "", "sha": ""}
            trees.append(cur)
        elif cur is not None and line.startswith("HEAD "):
            cur["sha"] = line.split(" ", 1)[1]
        elif cur is not None and line.startswith("branch "):
            cur["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
    return [t for t in trees if pathlib.Path(t["path"]) != clone]


def has_symlink_component(path: pathlib.Path) -> bool:
    absolute = path.absolute()
    return any(part.is_symlink() for part in (absolute, *absolute.parents))


def safe_roots(loop: dict, number: int) -> list[pathlib.Path]:
    roots = [pathlib.Path(p).expanduser() for p in loop["roots"]]
    roots.append(config.artifacts_dir(loop, number).parent)
    return [root.resolve() for root in roots if root.is_dir() and not has_symlink_component(root)]


def inside(path: pathlib.Path, parent: pathlib.Path) -> bool:
    return path == parent or parent in path.parents


def git_owner(path: pathlib.Path) -> pathlib.Path | None:
    directory = path if path.is_dir() else path.parent
    try:
        proc = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return pathlib.Path(proc.stdout.strip()).resolve() if proc.returncode == 0 and proc.stdout.strip() else None


def contains_nested_git(path: pathlib.Path, registered_worktree: bool = False) -> bool:
    if not path.is_dir():
        return False
    for directory, dirs, files in os.walk(path, followlinks=False):
        if registered_worktree and pathlib.Path(directory) == path:
            files = [name for name in files if name != ".git"]
            dirs = [name for name in dirs if name != ".git"]
        if ".git" in dirs or ".git" in files:
            return True
    return False


def owned_candidate(path: pathlib.Path, roots: list[pathlib.Path],
                    clone: pathlib.Path | None, registered: set[pathlib.Path]) -> bool:
    """Only root-contained artifacts or registered detached checkouts may be removed."""
    if has_symlink_component(path):
        return False
    candidate = path.resolve()
    if not any(inside(candidate, root) for root in roots):
        return False
    if clone and (inside(candidate, clone) or inside(clone, candidate)):
        return False
    owner = git_owner(path)
    # A configured root is a discovery boundary, not proof that another Git
    # repository's contents belong to this loop (including inherited owners).
    if owner and (owner != candidate or candidate not in registered):
        return False
    # A PR-named build directory may enclose an unrelated checkout. Do not recurse
    # through a Git registration we cannot prove belongs to this clone.
    if contains_nested_git(path, candidate in registered):
        return False
    return True


def du(path: pathlib.Path) -> int:
    try:
        proc = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, timeout=600)
        return int(proc.stdout.split()[0]) if proc.returncode == 0 else 0
    except Exception:
        return 0


@contextlib.contextmanager
def pinned_parent(path: pathlib.Path):
    """Resolve each ancestor without symlinks and hold its directory fd for unlink.

    An attacker renaming a configured root cannot redirect a subsequent unlink
    outside it. The candidate's inode is checked separately after measuring.
    """
    absolute = path.absolute()
    with contextlib.ExitStack() as stack:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, fd)
        for part in absolute.parent.parts[1:]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
        yield fd


def candidate_identity(path: pathlib.Path) -> tuple[tuple[int, int], tuple[int, int]]:
    with pinned_parent(path) as fd:
        parent = os.fstat(fd)
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError("candidate is a symlink")
        return (parent.st_dev, parent.st_ino), (info.st_dev, info.st_ino)


def remove_tree_fd(fd: int) -> None:
    """Walk only opened directories; never resolve a child through a symlink."""
    with os.scandir(fd) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd)
                try:
                    if not os.path.samestat(info, os.fstat(child)):
                        raise OSError("child changed during traversal")
                    remove_tree_fd(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=fd)
            else:
                os.unlink(entry.name, dir_fd=fd)


def remove_path(path: pathlib.Path, quiet: bool,
                expected: tuple[tuple[int, int], tuple[int, int]] | None = None,
                size: int | None = None) -> int:
    """Remove by pinned parent fd, rejecting a changed candidate or root."""
    if NEVER_TOUCH.search(str(path)):
        log(f"    SKIP (evidence pattern): {path}", quiet)
        return 0
    if size is None:
        size = du(path)
    try:
        with pinned_parent(path) as fd:
            parent = os.fstat(fd)
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or (expected is not None and
                    expected != ((parent.st_dev, parent.st_ino), (info.st_dev, info.st_ino))):
                raise OSError("candidate changed after validation")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd)
                try:
                    if not os.path.samestat(info, os.fstat(child)):
                        raise OSError("candidate changed before traversal")
                    remove_tree_fd(child)
                finally:
                    os.close(child)
                os.rmdir(path.name, dir_fd=fd)
            else:
                os.unlink(path.name, dir_fd=fd)
    except Exception as exc:
        log(f"    FAILED {path}: {exc}", quiet)
        return 0
    if path.exists() and not path.is_symlink():
        log(f"    SKIP (not removable): {path}", quiet)
        return 0
    return size


def pr_state(loop: dict, number: int) -> dict:
    """Return a fresh, matching GitHub PR state; invalid responses remain unknown."""
    data = gh.pr(loop, number)
    if not isinstance(data, dict) or type(data.get("number")) is not int or data["number"] != number:
        return {}
    if data.get("state") not in ("open", "closed"):
        return {}
    return {"state": data["state"], "merged": data.get("merged_at")}


def clear_state(loop: dict, number: int, quiet: bool) -> list[str]:
    """Drop this PR's loop state: seat locks, queue entries, in-flight marks, breach marker.

    Two shapes live in these files: entries keyed *by* the PR (queue, in-flight, breach) and
    entries that merely *mention* it (a seat lock, which is keyed by seat and names the PR it
    is holding). Both have to go, or a merged PR keeps a seat locked until the TTL expires.
    """
    st = state_mod.state_for(loop)
    key = f"{loop['repo']}#{number}"
    cleared: list[str] = []
    # The same locks the gates take: a gate claiming a seat between our read and our write
    # would otherwise be erased by this rewrite.
    with st.locked(), st._breach_lock():
        for path in (st.locks, st.pending, st.inflight_file, st.breach):
            data = st._load(path, {}) or {}
            if not isinstance(data, dict):
                continue
            before = json.dumps(data, sort_keys=True)
            data.pop(key, None)
            if path == st.inflight_file:
                # Marks are role:PR:SHA; require exact fields, not a string prefix.
                for mark in list(data):
                    if not isinstance(mark, str):
                        continue
                    parts = mark.split(":")
                    if (len(parts) == 3 and parts[0] in ("review", "fix")
                            and parts[1] == str(number) and parts[2]):
                        data.pop(mark)
            for seat, entry in list(data.items()):
                if not isinstance(entry, dict):
                    continue
                if entry.get("key") == key:              # a seat lock holding this PR
                    data.pop(seat, None)
                    continue
                entry.pop(key, None)                     # a queue keyed by PR
                if not entry:
                    data.pop(seat, None)
            if json.dumps(data, sort_keys=True) != before:
                st._save(path, data)
                cleared.append(path.name)
    if cleared:
        log(f"    state cleared: {', '.join(cleared)}", quiet)
    return cleared


def clean_pr(loop: dict, number: int, dry: bool, quiet: bool, force: bool = False) -> int:
    """Remove the local footprint of one finished PR. Returns the bytes removed."""
    state = pr_state(loop, number)
    if state.get("state") != "closed" and not force:
        log(f"    SKIP: PR #{number} is still open or its state is unknown", quiet)
        return 0

    freed = 0
    if not dry:
        clear_state(loop, number, quiet)

    clone = config.clone_path(loop)
    roots = safe_roots(loop, number)
    trees = worktrees(clone) if clone else []
    registered = {pathlib.Path(tree["path"]).resolve() for tree in trees if not tree["branch"]}
    protected = {pathlib.Path(tree["path"]).resolve() for tree in trees if tree["branch"]}

    cands: list[pathlib.Path] = []
    for tree in trees:
        if pr_from_path(pathlib.Path(tree["path"]).name) != number:
            continue
        if tree["branch"]:
            log(f"    SKIP (branch checked out: {tree['branch']}): {tree['path']}", quiet)
            continue
        cands.append(pathlib.Path(tree["path"]))
    for root in roots:
        if not root.exists():
            continue
        for child in root.iterdir():
            if child in cands:
                continue
            if pr_from_path(child.name) == number:
                cands.append(child)
    base = config.artifacts_dir(loop, number)
    if base.exists() and base not in cands:
        cands.append(base)

    for cand in cands:
        # Pin the directory identity BEFORE ownership checks; a real-directory
        # swap between Git validation and this snapshot must not be accepted.
        try:
            identity = candidate_identity(cand)
        except OSError as exc:
            log(f"    SKIP (changed candidate): {cand}: {exc}", quiet)
            continue
        if not owned_candidate(cand, roots, clone.resolve() if clone else None, registered):
            log(f"    SKIP (outside owned cleanup scope): {cand}", quiet)
            continue
        # Every source (worktree list, configured roots, artifacts base) shares this
        # final guard. Resolve aliases and protect enclosing paths as well: rmtree
        # on a parent would remove a nested branch checkout and uncommitted work.
        if overlaps_branch_worktree(cand, protected):
            log(f"    SKIP (branch worktree): {cand}", quiet)
            continue
        size = du(cand)
        if dry:
            log(f"    would remove {human(size):>10}  {cand}", quiet)
            freed += size
            continue
        # Never give git worktree remove an unpinned string path: it could be
        # swapped after validation. Remove via directory fd and prune metadata.
        size = remove_path(cand, quiet, expected=identity, size=size)
        log(f"    removed {human(size):>10}  {cand}", quiet)
        freed += size

    if not dry and clone:
        subprocess.run(["git", "-C", str(clone), "worktree", "prune", "--expire", "now"],
                       capture_output=True, text=True, timeout=300)
    return freed


def sweep(loop: dict, dry: bool, quiet: bool) -> int:
    clone = config.clone_path(loop)
    if not clone:
        log(f"{loop['id']}: no 'clone' configured — nothing to sweep", quiet)
        return 0
    by_pr: dict[int, list[dict]] = {}
    for tree in worktrees(clone):
        number = pr_from_path(pathlib.Path(tree["path"]).name)
        if number:
            by_pr.setdefault(number, []).append(tree)
    # Roots can hold a PR's logs with no worktree left; those still count.
    for root in [pathlib.Path(p).expanduser() for p in loop["roots"]]:
        if not root.is_dir() or has_symlink_component(root):
            continue
        for child in root.iterdir():
            number = pr_from_path(child.name)
            if number:
                by_pr.setdefault(number, [])

    total = 0
    seen: set[int] = set()
    for number in sorted(by_pr):
        if number in seen:
            continue
        seen.add(number)
        state = pr_state(loop, number)
        if not state or state.get("state") == "open":
            log(f"  PR #{number:<5} open/unknown — kept ({len(by_pr[number])} path(s))", quiet)
            continue
        freed = clean_pr(loop, number, dry, quiet)
        total += freed
        log(f"  PR #{number:<5} {state.get('state')} — "
            f"{'reclaimable' if dry else 'reclaimed'} {human(freed)}", quiet)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Reclaim local disk for finished PRs")
    ap.add_argument("--loop", help="loop id")
    ap.add_argument("--all", action="store_true", help="every configured loop (sweep only)")
    ap.add_argument("--pr", type=int, help="clean one PR")
    ap.add_argument("--sweep", action="store_true", help="every closed PR this clone knows about")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="explicitly clean one PR despite an open or unknown GitHub state")
    args = ap.parse_args()

    if not args.pr and not args.sweep:
        ap.error("pass --pr N or --sweep")
    if not args.loop and not args.all:
        ap.error("pass --loop <id> (or --all with --sweep)")

    loops = config.all_loops() if args.all else [config.load_id(args.loop)]
    total = 0
    for loop in loops:
        tag = f"[{loop['id']}]"
        if args.pr:
            state = pr_state(loop, args.pr)
            log(f"{tag} cleanup PR #{args.pr} ({state.get('state', 'unknown')}"
                f"{' · merged ' + state['merged'][:10] if state.get('merged') else ''})", args.quiet)
            freed = clean_pr(loop, args.pr, args.dry_run, args.quiet, args.force)
            log(f"{tag}   {'reclaimable' if args.dry_run else 'reclaimed'}: {human(freed)}",
                args.quiet)
            total += freed
        else:
            log(f"{tag} sweep: worktrees and review dirs for merged/closed PRs", args.quiet)
            total += sweep(loop, args.dry_run, args.quiet)
        log(f"{tag} nothing outside {', '.join(loop['roots']) or '(no roots)'} was touched; "
            f"evidence patterns and branch worktrees are always skipped", args.quiet)
    if len(loops) > 1:
        log(f"total {'reclaimable' if args.dry_run else 'reclaimed'}: {human(total)}", args.quiet)
    log(f"finished {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", args.quiet)


if __name__ == "__main__":
    main()
