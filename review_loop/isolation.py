"""Per-PR isolated workspaces — the "parallel with isolation" half of the design.

**The hazard this removes.** A review is not read-only: it checks out, adds worktrees, mutates
files, runs guard batteries and aborts merges. Two runs sharing one clone therefore do not merely
slow each other down — they corrupt each other's checkouts and produce *wrong verdicts*, which is
worse than a failed run. Serializing per seat hid that hazard by paying for it with throughput:

    parallel-with-isolation = own clone + own build dir + own tmp dir per PR
                              under the loop's artifacts root, one root per PR,
                              reused across that PR's rounds and torn down when it merges.

**Why it is cheap.** The clone is `git clone --local`, which hardlinks the object store, and a
code repository's git data is tiny next to its build output — the loop this was built for holds
25 MB of git and 177 GB of build artifacts. Isolation roots go under `artifacts/<PR>/`, which is
exactly what the cleanup already deletes at merge, so there is no new garbage and nothing extra to
remember. A PR's second round reuses its own warm build directory, so the cold build is paid once
per PR rather than once per round.

**Why no secret lands in the tree.** The isolated clone's `origin` is the plain GitHub URL and
`credential.helper` echoes the PAT out of its file, so what is written to disk is the *path* to a
0600 key, never the key. The fixer can push from its own clone; a leaked artifacts directory
leaks no credential.

**When it cannot be built**, ``ensure`` says so and returns ``None`` — it never half-builds a
sandbox and never falls back to a shared clone behind the caller's back. Callers decide: above
`concurrency = 1` an unisolatable run is queued, never started.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

from . import config
from .util import log

CLONE_DIR = "repo"
TARGET_DIR = "target"
TMP_DIR = "tmp"


def paths(loop: dict, number: int) -> dict:
    """The isolation root for a PR, and the directories inside it. Pure — creates nothing."""
    root = config.artifacts_dir(loop, number)
    return {"root": root, "clone": root / CLONE_DIR, "target": root / TARGET_DIR,
            "tmp": root / TMP_DIR}


def exists(loop: dict, number: int) -> bool:
    return (paths(loop, number)["clone"] / ".git").exists()


def _git(*args: str, cwd=None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, timeout=timeout)


def credential_helper(pat_path: pathlib.Path) -> str:
    """A helper that reads the token from its file at use time, so the repo holds a path only."""
    quoted = str(pat_path).replace("'", "'\\''")
    return ("!f() { echo username=x-access-token; "
            f"echo password=\"$(cat '{quoted}')\"; }}; f")


def _configure(clone: pathlib.Path, loop: dict, login: str) -> None:
    """Point origin at GitHub, install the credential helper, and give commits an identity."""
    remote = f"https://github.com/{loop['repo']}.git"
    if _git("remote", "set-url", "origin", remote, cwd=clone).returncode != 0:
        _git("remote", "add", "origin", remote, cwd=clone)
    pat = (loop.get("tokens") or {}).get(login) or (loop.get("tokens") or {}).get(
        loop.get("read_token", ""))
    if pat:
        _git("config", "credential.helper", credential_helper(config._path(pat)), cwd=clone)
    # There is no global git identity guarantee on a headless host, and a fixer that cannot commit
    # is a loop that dies one step after it starts.
    _git("config", "user.name", login or "review-loop", cwd=clone)
    _git("config", "user.email", f"{login or 'review-loop'}@users.noreply.github.com", cwd=clone)


def ensure(loop: dict, number: int, head: str = "", login: str = "") -> dict | None:
    """Create or reuse this PR's isolated workspace and check out ``head`` in it.

    Returns the workspace (root, clone, target, tmp, env, created) or ``None`` when isolation is
    not possible. Callers treat ``None`` as "no isolated run", never as "use the shared clone".
    """
    source = config.clone_path(loop)
    if not source or not (source / ".git").exists():
        return None  # nothing to clone from — the loop has no clone configured

    p = paths(loop, number)
    root, clone = p["root"], p["clone"]

    # A rail with teeth: the configured clone must never live inside the isolation root, or a
    # cleanup would delete the developer's working copy.
    try:
        if str(source.resolve()).startswith(str(root.resolve()) + os.sep) or source == clone:
            log(f"refusing to isolate #{number}: the configured clone is inside {root}")
            return None
    except Exception:
        pass

    try:
        for d in (root, p["target"], p["tmp"]):
            d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log(f"isolation mkdir failed for #{number}: {exc}")
        return None

    created = False
    if not (clone / ".git").exists():
        # --local hardlinks the object store (cheap, same filesystem); a plain clone is the
        # fallback when it is not (git refuses cross-device hardlinks outright).
        for args in (["clone", "--local", "--quiet", "--no-checkout", str(source), str(clone)],
                     ["clone", "--quiet", "--no-checkout", str(source), str(clone)]):
            if clone.exists():
                shutil.rmtree(clone, ignore_errors=True)
            try:
                proc = _git(*args, timeout=900)
            except Exception as exc:
                log(f"isolation clone failed for #{number}: {exc}")
                return None
            if proc.returncode == 0:
                created = True
                break
            log(f"isolation clone attempt failed for #{number}: {proc.stderr.strip()[:200]}")
        if not created:
            return None

    _configure(clone, loop, login)

    # Best effort: bring in refs from GitHub so the head can be checked out. A failure here is
    # not fatal on its own — the source may already carry the ref — but an impossible checkout is.
    try:
        _git("fetch", "--quiet", "--prune", "origin", timeout=900, cwd=clone)
    except Exception as exc:
        log(f"isolation fetch failed for #{number}: {exc}")

    if head:
        if _git("checkout", "--quiet", "--detach", head, cwd=clone).returncode != 0:
            _git("fetch", "--quiet", "origin", head, timeout=900, cwd=clone)
            if _git("checkout", "--quiet", "--detach", head, cwd=clone).returncode != 0:
                log(f"isolation cannot check out {head[:7]} for #{number} — no isolated run")
                # Never leave a half-built sandbox behind: if this call created it, it goes too.
                # A *reused* root stays, because its warm build directory belongs to the PR.
                if created:
                    shutil.rmtree(root, ignore_errors=True)
                return None

    return {"root": str(root), "clone": str(clone), "target": str(p["target"]),
            "tmp": str(p["tmp"]), "env": env(loop, number), "created": created}


def env(loop: dict, number: int) -> dict:
    """The environment a run needs so its build output lands in its own sandbox.

    ``CARGO_TARGET_DIR`` and ``TMPDIR`` are the two that matter for the Rust harnesses this was
    built against: without them two runs share one target directory and one /tmp namespace and
    corrupt each other while looking isolated. Set them per run — never globally.
    """
    p = paths(loop, number)
    merged = {"CARGO_TARGET_DIR": str(p["target"]), "TMPDIR": str(p["tmp"]),
              "REVIEW_LOOP_WORKSPACE": str(p["root"]), "REVIEW_LOOP_PR": str(number)}
    if loop.get("clone"):
        merged["REVIEW_LOOP_SHARED_CLONE"] = str(config.clone_path(loop))
    return merged


def describe(workspace: dict | None, loop: dict, number: int) -> str:
    """One paragraph for a prompt: where this run works and what it must not touch."""
    p = paths(loop, number)
    if not workspace:
        shared = loop.get("clone") or "(no clone configured)"
        return (f"No isolated workspace was prepared for this PR, so run against the shared clone "
                f"at {shared} and keep every artifact (worktrees, build dirs, logs) under "
                f"{p['root']} — nothing inside the repository.")
    export = " ".join(f"{k}={v}" for k, v in workspace["env"].items()
                      if k in ("CARGO_TARGET_DIR", "TMPDIR"))
    created = "prepared for this PR" if workspace.get("created") else "reused (warm build dir)"
    return (f"Work in your own isolated clone — {created}:\n"
            f"  clone:  {workspace['clone']}\n"
            f"  export: {export}\n"
            f"  logs:   {workspace['root']}\n"
            f"That clone is yours alone (its own origin, its own credential helper, its own "
            f"detached checkout) — commit and push from there if you are the fixer. Never touch "
            f"the shared clone at {loop.get('clone') or '(none)'} and never write inside the "
            f"repository without committing to the PR branch.")
