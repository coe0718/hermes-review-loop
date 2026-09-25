"""Cleanup: a finished PR gives its disk back, and nothing else."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


def group_cleanup() -> None:
    section("cleanup — a finished PR gives its disk back, and nothing else")

    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z"), "9": pr(9)})
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--pr", "7", "--dry-run")
    check("dry run reports the worktree", "pr7-wt" in out, True)
    check("  and does NOT delete it", (REVIEWS / "pr7-wt").exists(), True)

    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("real run removes the worktree", (REVIEWS / "pr7-wt").exists(), False)
    check("  removes its build log", (REVIEWS / "widgets-pr7-build.log").exists(), False)
    check("  SKIPS the evidence dir", (REVIEWS / "pr7-phase3-evidence").exists(), True)
    check("  another PR's worktree untouched", (REVIEWS / "pr9-wt").exists(), True)
    check("  the branch worktree untouched", (SCRATCH / "pr8-work-branch").exists(), True)
    check("  unrelated files untouched", (SCRATCH / "unrelated.log").exists(), True)
    check("  worktree registration pruned",
          "pr7-wt" in subprocess.run(["git", "-C", str(CLONE), "worktree", "list"],
                                     capture_output=True, text=True).stdout, False)

    # A branch checkout matching the *same* closed PR must survive the configured-root
    # scan, even when a configured root points inside that checkout.
    reset(prs={"7": pr(7, state="closed")})
    branch = REVIEWS / "widgets-pr7-dev"
    subprocess.run(["git", "-C", str(CLONE), "worktree", "add", "-b", "fix/pr7",
                    str(branch), "HEAD"], check=True, capture_output=True)
    sentinel = branch / "uncommitted.txt"
    sentinel.write_text("do not lose local work\n")
    nested = branch / "widgets-pr7-logs"
    nested.mkdir()
    (nested / "build.log").write_text("preserve\n")
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["roots"].append(str(branch))
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("same-PR branch sentinel survives root cleanup", sentinel.read_text() if sentinel.exists() else None,
          "do not lose local work\n")
    check("nested candidate inside branch survives", (nested / "build.log").exists(), True)
    check("branch remains registered", str(branch) in subprocess.run(
        ["git", "-C", str(CLONE), "worktree", "list", "--porcelain"],
        capture_output=True, text=True).stdout, True)

    # A root child enclosing a branch checkout is just as destructive to remove.
    reset(prs={"7": pr(7, state="closed")})
    parent = REVIEWS / "widgets-pr7-container"
    parent.mkdir()
    inside = parent / "developer"
    subprocess.run(["git", "-C", str(CLONE), "worktree", "add", "-b", "fix/inside7",
                    str(inside), "HEAD"], check=True, capture_output=True)
    (inside / "uncommitted.txt").write_text("inside branch\n")
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("parent candidate containing branch survives", (inside / "uncommitted.txt").exists(), True)

    # The explicit artifacts base is a separate candidate source, not just a root scan.
    reset(prs={"7": pr(7, state="closed")})
    base = STATE_DIR / "artifacts" / "7"
    base.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(CLONE), "worktree", "add", "-b", "fix/base7",
                    str(base), "HEAD"], check=True, capture_output=True)
    base_sentinel = base / "uncommitted.txt"
    base_sentinel.write_text("preserve base\n")
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("branch worktree at artifacts base survives", base_sentinel.read_text() if base_sentinel.exists() else None,
          "preserve base\n")

    # The loop's own isolation workspace is a full clone (artifacts/<N>/<seat>/repo). Its .git
    # is ours: refusing it as a "nested checkout" would leave every isolated PR on disk forever.
    reset(prs={"7": pr(7, state="closed")})
    base = STATE_DIR / "artifacts" / "7"
    iso = base / "reviewer" / "repo"
    iso.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--local", str(CLONE), str(iso)], check=True,
                   capture_output=True)
    (base / "reviewer" / "target").mkdir()
    (base / "reviewer" / "target" / "build.log").write_text("z" * 1024 + "\n")
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("isolated per-PR workspace is removed", base.exists(), False)
    check("  the loop clone is untouched", (CLONE / "README.md").exists(), True)

    # ...but only the real directory: an artifacts path reached through a symlink is not ours.
    reset(prs={"7": pr(7, state="closed")})
    elsewhere = TMP / "elsewhere-artifacts"
    shutil.rmtree(elsewhere, ignore_errors=True)
    subprocess.run(["git", "clone", "-q", "--local", str(CLONE), str(elsewhere / "reviewer" / "repo")],
                   check=True, capture_output=True)
    (STATE_DIR / "artifacts").mkdir(parents=True)
    (STATE_DIR / "artifacts" / "7").symlink_to(elsewhere, target_is_directory=True)
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("symlinked artifacts dir is not followed",
          (elsewhere / "reviewer" / "repo" / "README.md").exists(), True)

    # Roots are shared between loops: pr7 alone is PR 7 of *some* repository.
    reset(prs={"7": pr(7, state="closed")})
    for name in ("gadgets-pr7-target", "pr7-build", "widgetsplus-pr7.log", "widgets-pr7-target"):
        (SCRATCH / name).mkdir()
        (SCRATCH / name / "sentinel.txt").write_text(name + "\n")
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("another repo's pr7 dir in a shared root survives",
          (SCRATCH / "gadgets-pr7-target" / "sentinel.txt").exists(), True)
    check("  an unscoped pr7 dir survives", (SCRATCH / "pr7-build" / "sentinel.txt").exists(), True)
    check("  a prefix-sharing repo name survives",
          (SCRATCH / "widgetsplus-pr7.log" / "sentinel.txt").exists(), True)
    check("  this repo's pr7 dir is reclaimed", (SCRATCH / "widgets-pr7-target").exists(), False)
    reset(prs={"7": pr(7, state="closed")})
    (SCRATCH / "gadgets-pr7-target").mkdir()
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--sweep", "--dry-run")
    check("  sweep does not list another repo's PR", "gadgets-pr7-target" in out, False)

    # Configured roots are discovery boundaries, not permission to remove another repo.
    reset(prs={"7": pr(7, state="closed")})
    other = REVIEWS / "widgets-pr7-other-repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    other_sentinel = other / "uncommitted.txt"
    other_sentinel.write_text("other repo's branch\n")
    clone_child = CLONE / "pr7-cache"
    clone_child.mkdir()
    (clone_child / "sentinel.txt").write_text("inside clone\n")
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["roots"].append(str(CLONE))
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("unrelated repository branch survives", other_sentinel.read_text() if other_sentinel.exists() else None,
          "other repo's branch\n")
    check("PR-named directory inside clone survives", (clone_child / "sentinel.txt").exists(), True)

    # A configured root inside another repository is not owned by this clone.
    reset(prs={"7": pr(7, state="closed")})
    foreign = TMP / "foreign-checkout"
    shutil.rmtree(foreign, ignore_errors=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(foreign)], check=True)
    foreign_root = foreign / "build"
    foreign_root.mkdir()
    foreign_file = foreign_root / "widgets-pr7-source"
    foreign_file.write_text("foreign source\n")
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["roots"].append(str(foreign_root))
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("foreign repo root cannot authorize source removal", foreign_file.read_text() if foreign_file.exists() else None,
          "foreign source\n")

    reset(prs={"7": pr(7, state="closed")})
    outside = TMP / "pr7-outside"
    subprocess.run(["git", "-C", str(CLONE), "worktree", "add", "--detach", str(outside), "HEAD"],
                   check=True, capture_output=True)
    (outside / "sentinel.txt").write_text("outside roots\n")
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("detached worktree outside roots survives", (outside / "sentinel.txt").exists(), True)
    check("outside worktree stays registered", str(outside) in subprocess.run(
        ["git", "-C", str(CLONE), "worktree", "list", "--porcelain"],
        capture_output=True, text=True).stdout, True)
    if outside.exists():
        subprocess.run(["git", "-C", str(CLONE), "worktree", "remove", "--force", str(outside)], check=True)

    reset(prs={"7": pr(7, state="closed")})
    external = TMP / "external-cleanup"
    shutil.rmtree(external, ignore_errors=True)
    external.mkdir()
    (external / "widgets-pr7-sentinel").write_text("external root\n")
    linked = TMP / "linked-root"
    linked.unlink(missing_ok=True)
    linked.symlink_to(external, target_is_directory=True)
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["roots"].append(str(linked))
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("symlinked root cannot delete external child", (external / "widgets-pr7-sentinel").exists(), True)

    # Swap a validated root at the du seam; string-path unlink would hit the sentinel.
    reset(prs={"7": pr(7, state="closed")})
    external = TMP / "outside-swap"
    shutil.rmtree(external, ignore_errors=True)
    external.mkdir()
    outside_file = external / "widgets-pr7-build.log"
    outside_file.write_text("outside must survive\n")
    outside_tree = external / "pr7-wt"
    outside_tree.mkdir()
    (outside_tree / "sentinel.txt").write_text("outside worktree survives\n")
    saved_root = TMP / "reviews-before-swap"
    spec = importlib.util.spec_from_file_location("cleanup_under_test", ROOT / "scripts" / "cleanup.py")
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)
    with mock.patch.object(cleanup.gh, "pr", return_value={"number": True, "state": "closed"}):
        check("boolean true is not PR #1", cleanup.pr_state({}, 1), {})
    real_du = cleanup.du
    swapped = False

    def swap_during_du(path):
        nonlocal swapped
        if not swapped:
            swapped = True
            REVIEWS.rename(saved_root)
            REVIEWS.symlink_to(external, target_is_directory=True)
        return real_du(path)

    try:
        with mock.patch.object(cleanup, "du", side_effect=swap_during_du):
            cleanup.clean_pr(json.loads((LOOPS_DIR / "widgets.json").read_text()), 7,
                             dry=False, quiet=True, force=True)
        check("swap hook exercised after validation", swapped, True)
        check("swapped root cannot unlink outside sentinel", outside_file.read_text() if outside_file.exists() else None,
              "outside must survive\n")
        check("swapped root cannot remove outside tree", (outside_tree / "sentinel.txt").exists(), True)
    finally:
        REVIEWS.unlink(missing_ok=True)
        saved_root.rename(REVIEWS)

    # A replacement *real directory* (not a symlink) must not bypass the
    # validation-time parent identity check, even with a matching basename.
    original = TMP / "original-root"
    replacement = TMP / "replacement-root"
    original.mkdir()
    replacement.mkdir()
    candidate = original / "pr7-log"
    candidate.write_text("original\n")
    (replacement / "pr7-log").write_text("replacement\n")
    identity = cleanup.candidate_identity(candidate)
    moved = TMP / "moved-original-root"
    original.rename(moved)
    replacement.rename(original)
    check("real-directory root swap refuses replacement inode",
          cleanup.remove_path(candidate, quiet=True, expected=identity), 0)
    check("real-directory root swap preserves replacement", candidate.read_text(), "replacement\n")

    tree = TMP / "pr7-tree-swap"
    tree.mkdir()
    child = tree / "build"
    child.mkdir()
    (child / "artifact.log").write_text("build\n")
    outside_tree = TMP / "outside-tree-swap"
    outside_tree.mkdir()
    outside_marker = outside_tree / "sentinel.txt"
    outside_marker.write_text("do not follow\n")
    real_remove_tree = cleanup.remove_tree_fd

    def swap_nested_before_walk(fd):
        shutil.rmtree(child)
        child.symlink_to(outside_tree, target_is_directory=True)
        return real_remove_tree(fd)

    with mock.patch.object(cleanup, "remove_tree_fd", side_effect=swap_nested_before_walk):
        cleanup.remove_path(tree, quiet=True, expected=cleanup.candidate_identity(tree))
    check("nested symlink swap cannot traverse outside", outside_marker.read_text(), "do not follow\n")

    reset(prs={"7": pr(7, state="closed")})
    unrelated = TMP / "pr7-unrelated-root"
    shutil.rmtree(unrelated, ignore_errors=True)
    unrelated.mkdir()
    (unrelated / "neutral.log").write_text("unrelated child\n")
    subprocess.run(["git", "-C", str(CLONE), "worktree", "add", "--detach",
                    str(unrelated / "neutral-wt"), "HEAD"], check=True, capture_output=True)
    (unrelated / "neutral-wt" / "sentinel.txt").write_text("not PR 7\n")
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["roots"].append(str(unrelated))
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("root name does not attribute unrelated child", (unrelated / "neutral.log").exists(), True)
    check("root name does not attribute neutral detached checkout",
          (unrelated / "neutral-wt" / "sentinel.txt").exists(), True)
    run("cleanup.py", None, "--loop", "widgets", "--sweep")
    check("sweep does not attribute unrelated child", (unrelated / "neutral.log").exists(), True)
    check("sweep does not attribute neutral detached checkout",
          (unrelated / "neutral-wt" / "sentinel.txt").exists(), True)

    reset(prs={"7": pr(7, state="closed")})
    nested_repo = REVIEWS / "widgets-pr7-bundle" / "developer"
    subprocess.run(["git", "init", "-q", "-b", "main", str(nested_repo)], check=True)
    (nested_repo / "uncommitted.txt").write_text("nested repo\n")
    link = REVIEWS / "widgets-pr7-external-link"
    external = TMP / "external-cleanup"
    external.mkdir(exist_ok=True)
    (external / "sentinel.txt").write_text("external link\n")
    link.symlink_to(external, target_is_directory=True)
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("nested unrelated checkout survives parent removal", (nested_repo / "uncommitted.txt").exists(), True)
    check("symlink child is not removed or followed", link.is_symlink() and
          (external / "sentinel.txt").exists(), True)

    # state is cleared for that PR
    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z")})
    state_file("locks.json").write_text(json.dumps({"reviewer": {"at": time.time(), "key": f"{REPO}#7"}}))
    state_file("breach.json").write_text(json.dumps({f"{REPO}#7": {"status": "awaiting-adjudication"}}))
    state_file("inflight.json").write_text(json.dumps({
        f"review:7:{HEAD_A}": time.time(), f"fix:7:{HEAD_A}": time.time(),
        f"review:70:{HEAD_A}": 123, f"fix:70:{HEAD_A}": 456,
        f"review:7x:{HEAD_A}": 789, f"other:7:{HEAD_A}": 321,
        f"review:7:{HEAD_A}:extra": 654}))
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"head": HEAD_A}, f"{REPO}#70": {"head": HEAD_A}}}))
    state_file("breach.json").write_text(json.dumps({f"{REPO}#7": {"status": "awaiting-adjudication"},
                                                      f"{REPO}#70": {"status": "delivery-pending"}}))
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("locks cleared for the PR", load_state("locks.json"), {})
    check("breach marker cleared without adjacent PR", load_state("breach.json"),
          {f"{REPO}#70": {"status": "delivery-pending"}})
    check("queue keeps adjacent PR", load_state("pending.json"),
          {"reviewer": {f"{REPO}#70": {"head": HEAD_A}}})
    check("only exact PR in-flight marks cleared", load_state("inflight.json"), {
        f"review:70:{HEAD_A}": 123, f"fix:70:{HEAD_A}": 456,
        f"review:7x:{HEAD_A}": 789, f"other:7:{HEAD_A}": 321,
        f"review:7:{HEAD_A}:extra": 654})
    set_prs({"7": pr(7, head=HEAD_A)})
    check_eligible("reopened same head can enter reviewer gate", "gate_reviewer.py",
                   pr_payload(), "reviewer")

    # an open PR is refused without --force
    reset(prs={"7": pr(7, state="open")})
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("open PR is refused", "still open" in out, True)
    check("  and its files stay", (REVIEWS / "pr7-wt").exists(), True)

    # A failed or malformed fresh lookup must not turn an unknown PR into a deletion.
    # Use real scratch worktrees and persisted state so the test catches both effects.
    for label, response in (("missing", None),
                            ("malformed state", {"number": 7, "state": "mystery"}),
                            ("missing identity", {"state": "closed"}),
                            ("error body", {"message": "API unavailable"}),
                            ("wrong PR", pr(9, state="closed")),
                            ("boolean PR", {**pr(7, state="closed"), "number": True}),
                            ("float PR", {**pr(7, state="closed"), "number": 7.0})):
        reset(prs={"7": response} if response is not None else {})
        marker = state_file("breach.json")
        marker.write_text(json.dumps({f"{REPO}#7": {"status": "awaiting-adjudication"}}))
        run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
        check(f"direct {label}: worktree kept", (REVIEWS / "pr7-wt").exists(), True)
        check(f"direct {label}: state kept", f"{REPO}#7" in load_state("breach.json"), True)
        payload = pr_payload(action="closed", merged="2026-02-02T00:00:00Z")
        kind, _, _ = run("gate_reviewer.py", payload)
        check(f"webhook {label}: silent", kind, "SILENT")
        check(f"webhook {label}: worktree kept", (REVIEWS / "pr7-wt").exists(), True)
        check(f"webhook {label}: state kept", f"{REPO}#7" in load_state("breach.json"), True)

    reset(prs={"7": pr(7, state="closed")})
    marker = state_file("breach.json")
    marker.write_text(json.dumps({f"{REPO}#7": {"status": "awaiting-adjudication"}}))
    failed_lookup = {"REVIEW_LOOP_GH_STUB": "/bin/false"}
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7", extra_env=failed_lookup)
    check("failed GitHub command: direct keeps worktree", (REVIEWS / "pr7-wt").exists(), True)
    check("failed GitHub command: direct keeps state", f"{REPO}#7" in load_state("breach.json"), True)
    kind, _, _ = run("gate_reviewer.py", pr_payload(action="closed"), extra_env=failed_lookup)
    check("failed GitHub command: webhook silent", kind, "SILENT")
    check("failed GitHub command: webhook keeps worktree", (REVIEWS / "pr7-wt").exists(), True)
    check("failed GitHub command: webhook keeps state", f"{REPO}#7" in load_state("breach.json"), True)

    reset(prs={"7": pr(7, state="open")})
    payload = pr_payload(action="closed", merged="2026-02-02T00:00:00Z")
    run("gate_reviewer.py", payload)
    check("stale close webhook keeps reopened PR", (REVIEWS / "pr7-wt").exists(), True)
    reset(prs={"7": pr(7, state="closed")})
    run("gate_reviewer.py", pr_payload(action="closed"))
    check("confirmed closed webhook reclaims worktree", (REVIEWS / "pr7-wt").exists(), False)

    reset(prs={})
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7", "--force")
    check("explicit operator force permits unknown cleanup", (REVIEWS / "pr7-wt").exists(), False)

    # the sweep walks what is closed and leaves what is open
    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z"),
               "8": pr(8), "9": pr(9)})
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--sweep")
    check("sweep reclaims the closed PR", (REVIEWS / "pr7-wt").exists(), False)
    check("sweep keeps the open one", (REVIEWS / "pr9-wt").exists(), True)
    check("sweep keeps the unknown one", (SCRATCH / "widgets-pr8-target").exists(), True)
    check("sweep reports a total", "reclaimed" in out, True)


GROUPS = {
    "cleanup": group_cleanup,
}
