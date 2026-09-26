"""The watchdog's stall shapes and ``explain``."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


def group_watchdog() -> None:
    section("watchdog — quiet is not the same as nothing to do")

    reset(prs={"7": pr(7)})
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("empty queue says so", out, "widgets: reviewer queue empty")

    # a queued request starts once the seat is free
    reset(prs={"7": pr(7)})
    state_file("pending.json").parent.mkdir(parents=True, exist_ok=True)
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A,
                                    "url": f"https://github.com/{REPO}/pull/7", "reason": "busy"}}}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("HTTP-only queued review remains unacknowledged", "started the queued run" in out, False)
    check("  it asked for the reviewer seat",
          json.loads(RECEIVED[-1]["body"])["requested_reviewer"]["login"], SEAT)
    check("  signature is valid for that route", verify_sig(RECEIVED[-1], "widgets-review"), True)
    check("  queue remains until the gate acknowledges enqueue",
          f"{REPO}#7" in load_state("pending.json").get("reviewer", {}), True)

    # A queued A must not turn into a synthetic request for B. A fresh event can
    # subsequently enqueue B, but the stale request has no authority to wake it.
    for seat, reviews in (("reviewer", []), ("fixer", [review(REVIEWER, head=HEAD_B)])):
        reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": reviews}})
        state_file("pending.json").write_text(json.dumps({seat: {
            f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
        before = len(RECEIVED)
        out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", seat)
        check(f"stale {seat} A queue never wakes B", len(RECEIVED) - before, 0)
        check(f"stale {seat} A queue is discarded", load_state("pending.json"), {})
        check(f"stale {seat} queue is not reported started", "started the queued run" in out, False)
    # An unreadable PR leaves the queue alone for a later safe retry.
    reset(prs={"7": pr(7, head=HEAD_B)})
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer",
        extra_env={"REVIEW_LOOP_GH_STUB": "/bin/false"})
    check("unreadable fresh PR keeps queue for retry", f"{REPO}#7" in
          load_state("pending.json").get("reviewer", {}), True)
    # A genuinely new B request is independently eligible, rather than inheriting A.
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_B, "url": "u", "reason": "fresh"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("fresh B request is woken", len(RECEIVED) - before, 1)
    check("fresh B wake carries B", json.loads(RECEIVED[-1]["body"])["pull_request"]["head"]["sha"], HEAD_B)

    # A readable but malformed reviews response is not evidence of zero verdicts.
    reset(prs={"7": {**pr(7), "reviews": {"message": "not a review list"}}})
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("malformed review list never wakes queued reviewer", len(RECEIVED) - before, 0)
    check("malformed review list retains queue for retry",
          f"{REPO}#7" in load_state("pending.json").get("reviewer", {}), True)

    # a queued request for a head that was already reviewed dies quietly
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER)]}})
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("already-reviewed queue entry is dropped", len(RECEIVED) - before, 0)
    check("  and removed from the queue", load_state("pending.json"), {})

    for state in ("COMMENTED", "PENDING", "DISMISSED", "APPROVED", "CHANGES_REQUESTED"):
        reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, state=state)]}})
        state_file("pending.json").write_text(json.dumps(
            {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A,
                                         "url": "u", "reason": "busy"}}}))
        before = len(RECEIVED)
        run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
        expected = state in ("COMMENTED", "PENDING", "DISMISSED")
        check(f"queued review with {state} {'fires' if expected else 'drops'}",
              len(RECEIVED) - before, 1 if expected else 0)
        check(f"  {state} queue {'held' if expected else 'cleared'}",
              f"{REPO}#7" in load_state("pending.json").get("reviewer", {}), expected)
        if expected and len(RECEIVED) > before:
            check(f"  {state} wake targets requested head",
                  json.loads(RECEIVED[-1]["body"])["pull_request"]["head"]["sha"], HEAD_A)

    # a full seat fires nothing
    reset(prs={"7": pr(7)})
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#9": {"at": time.time(), "head": HEAD_B, "why": "working"}}}))
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "capacity"}}}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("full seat drains nothing", "at capacity (1/1" in out, True)

    # An ordinary armed sweep must drain after a lock expires even without an alert.
    reset(prs={"7": pr(7), "9": pr(9)})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 60}))
    state_file("locks.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#9": {"at": time.time() - 46 * 60, "head": HEAD_B, "why": "expired"}}}))
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "capacity"}}}))
    before = len(RECEIVED)
    out, _, _ = run("watchdog.py", None, "--loop", "widgets",
                    extra_env={"REVIEW_LOOP_TEST": ""})
    check("zero-alert sweep wakes queued PR after lock expiry", len(RECEIVED) - before, 1)
    check("  the eligible PR was woken", json.loads(RECEIVED[-1]["body"])["number"] if RECEIVED else None, 7)
    check("  no stall warning is required", "silent stall" in out or "stuck state" in out, False)
    check("  HTTP-only queue remains unacknowledged",
          f"{REPO}#7" in load_state("pending.json").get("reviewer", {}), True)
    check("  expired lock is cleared", load_state("locks.json"), {})
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    check("  uncertain route is not automatically replayed", len(RECEIVED) - before, 0)

    # A spare slot must not wake a PR already held by that seat.
    reset(prs={"7": pr(7)})
    set_concurrency(2)
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 60}))
    state_file("locks.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "why": "working"}}}))
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    check("spare slot does not re-wake an active PR", len(RECEIVED) - before, 0)
    check("  active PR stays queued for its handoff", f"{REPO}#7" in
          load_state("pending.json").get("reviewer", {}), True)
    check("  existing slot remains held", len(load_state("locks.json").get("reviewer", {})), 1)

    # The fixer seat also drains without a fresh stall, but only with an eligible verdict.
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER)]}})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 60}))
    state_file("pending.json").write_text(json.dumps({"fixer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    check("zero-alert sweep drains eligible fixer", len(RECEIVED) - before, 1)
    check("  fixer route received the verdict", RECEIVED[-1]["event"], "pull_request_review")
    check("  fixer hold remains until gate acknowledgement",
          f"{REPO}#7" in load_state("pending.json").get("fixer", {}), True)

    # A complete fake GitHub listing and individual read agree: a known stacked
    # child became draft while retargeting main at the SAME head. This is not a
    # new reviewer/fixer authorization, even when stale tokens survived on disk.
    parent = {**pr(7, head=HEAD_B), "head": {"ref": "parent", "sha": HEAD_B},
              "base": {"ref": "main", "sha": HEAD_A}}
    child = {**pr(9, head=HEAD_A, base="parent"),
             "base": {"ref": "parent", "sha": HEAD_B},
             "reviews": [review(REVIEWER, head=HEAD_A, rid=71)]}
    reset(prs={"7": parent, "9": child})
    run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    stacked = load_state("watchdog.json").get("stacked_wait", {}).get("9")
    check("stacked child observed before draft retarget", stacked.get("head") if stacked else None, HEAD_A)
    key = f"{REPO}#9"
    state_file("pending.json").write_text(json.dumps({seat: {key: {
        "at": time.time(), "head": HEAD_A, "url": "u", "reason": "stacked"}}
        for seat in ("reviewer", "fixer")}))
    state_file("inflight.json").write_text(json.dumps({
        f"review:9:{HEAD_A}": time.time(), f"fix:9:{HEAD_A}": time.time()}))
    state_file("breach.json").write_text(json.dumps({key: {
        "head": HEAD_A, "status": "delivery-pending"}}))
    child["draft"] = True
    child["base"] = {"ref": "main", "sha": HEAD_A}
    set_prs({"7": parent, "9": child})
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    check("draft retarget records same-head hold", load_state("stack-transitions.json").get("9", {}).get("head"), HEAD_A)
    check("draft retarget clears stacked wait", "9" in load_state("watchdog.json").get("stacked_wait", {}), False)
    check("draft retarget drops both stale seat requests", load_state("pending.json"), {})
    check("draft retarget clears in-flight tokens", load_state("inflight.json"), {})
    check("draft retarget clears escalation marker", load_state("breach.json"), {})
    check("draft retarget never starts an agent", len(RECEIVED) - before, 0)
    child["draft"] = False
    set_prs({"7": parent, "9": child})
    run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    check("ready transition keeps same-head hold", load_state("stack-transitions.json").get("9", {}).get("head"), HEAD_A)
    check("ready transition does not wake stale work", len(RECEIVED) - before, 0)
    # Owner policy (#23): the ready, retargeted child gets ONE fresh isolated reviewer turn.
    # This harness has no private runtime, so the enqueue fails visibly and stays retryable.
    fresh = load_state("stack-transitions.json").get("9", {}).get("fresh_review") or {}
    check("ready retarget attempts the fresh reviewer turn", fresh.get("state"), "retry")
    check("  under the transition's own turn key",
          str(fresh.get("turn_key", "")).startswith("retarget:parent:"), True)
    _, out, _ = run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    check("  a failed fresh enqueue is reported and retried next sweep",
          "fresh review after retarget not enqueued" in out, True)

    # shape 1: the reviewer never posted a verdict
    reset(prs={"7": pr(7, head=HEAD_A)})
    state_file("watchdog.json").parent.mkdir(parents=True, exist_ok=True)
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    DATA["world"]["commit_dates"] = {HEAD_A: "2020-01-01T00:00:00Z"}
    save_world()
    out, _, _ = run("watchdog.py", None, "--loop", "widgets")
    check("stall: no verdict at a quiet head", "reviewer never posted a verdict" in out, True)

    # shape 2: the fixer never pushed after a verdict
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER)]}})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets")
    check("stall: verdict with no fix", "fixer never pushed" in out, True)

    # shape 3: parked awaiting adjudication
    reset(prs={"7": pr(7)})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    state_file("breach.json").write_text(json.dumps(
        {f"{REPO}#7": {"pr": 7, "head": HEAD_A, "rounds": 3, "cap": 3, "at": "2026-01-01T00:00:00Z",
                       "status": "awaiting-adjudication", "reason": "cap"}}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets")
    check("stall: parked awaiting adjudication", "parked awaiting adjudication" in out, True)

    # shape 4: the cap is spent but nothing escalated (the gate never fired)
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head="c" * 40, rid=1),
                                                       review(REVIEWER, head="d" * 40, rid=2),
                                                       review(REVIEWER, head="e" * 40, rid=3)]}})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets")
    check("stall: cap spent, no escalation marker", "NO escalation marker" in out, True)

    # Real grace/baseline mode (not REVIEW_LOOP_TEST's zero-grace bypass).
    normal = {"REVIEW_LOOP_TEST": ""}
    reset(prs={"7": pr(7)})
    DATA["world"]["commit_dates"] = {HEAD_A: "2020-01-01T00:00:00Z",
                                       HEAD_B: "2020-01-01T00:00:00Z"}
    save_world()
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)  # arm with old head
    watch = load_state("watchdog.json")
    check("arming snapshots the existing head", watch.get("heads", {}).get("7", {}).get("sha"), HEAD_A)
    watch["armed_since"] = time.time() - 7200
    state_file("watchdog.json").write_text(json.dumps(watch))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("old PR at original head is not a stall", "reviewer never posted" in out, False)
    set_prs({"7": pr(7, head=HEAD_B)})
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    watch = load_state("watchdog.json")
    check("changed old-dated head gets observation clock", watch.get("heads", {}).get("7", {}).get("sha"), HEAD_B)
    check("new head gets grace before alarm", "reviewer never posted" in out, False)
    watch["heads"]["7"]["observed_at"] = time.time() - 3600
    state_file("watchdog.json").write_text(json.dumps(watch))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("old-dated new head alarms after observed grace", "reviewer never posted" in out, True)
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("restart retains head and cooldown", "reviewer never posted" in out, False)

    # Cap marker is likewise gated by observation, not by the commit's date.
    reset(prs={"7": pr(7)})
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    cap_reviews = [review(REVIEWER, head="c" * 40, rid=1),
                   review(REVIEWER, head="d" * 40, rid=2),
                   review(REVIEWER, head="e" * 40, rid=3)]
    set_prs({"7": {**pr(7), "reviews": cap_reviews}})
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("old unchanged PR does not signal missing cap marker", "NO escalation marker" in out, False)
    set_prs({"7": {**pr(7, head=HEAD_B), "reviews": cap_reviews}})
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("new old-dated head signals missing cap marker", "NO escalation marker" in out, True)

    # Review API failure leaves the observation persisted but does not guess verdicts.
    reset(prs={"7": pr(7)})
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    set_prs({"7": {**pr(7, head=HEAD_B), "reviews": None}})
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("unknown reviews yield no false stall", "reviewer never posted" in out, False)
    watch = load_state("watchdog.json")
    check("review failure does not erase new head clock", watch["heads"]["7"]["sha"], HEAD_B)
    watch["heads"]["7"]["observed_at"] = time.time() - 3600
    state_file("watchdog.json").write_text(json.dumps(watch))
    set_prs({"7": pr(7, head=HEAD_B)})
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("review recovery evaluates retained clock", "reviewer never posted" in out, True)

    # First-seen old PRs (including migrated state) are conservative; PRs created
    # after arming receive a clock even if they were absent from the initial list.
    reset(prs={})
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    old = pr(7)
    old["created_at"] = "2020-01-01T00:00:00Z"
    new = pr(9)
    new["created_at"] = datetime.now(timezone.utc).isoformat()
    set_prs({"7": old, "9": new})
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    heads = load_state("watchdog.json")["heads"]
    check("first-seen old PR is baseline", heads["7"]["observed_at"], None)
    check("post-arm new PR gets observed clock", heads["9"]["observed_at"] is not None, True)

    reset(prs={"7": old})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 7200}))
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("legacy watchdog state baselines unknown old head",
          load_state("watchdog.json")["heads"]["7"]["observed_at"], None)

    # A failed listing must not arm the loop or replace its head snapshot.
    reset(prs={"7": pr(7)})
    DATA["world"]["prs"] = None
    save_world()
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("failed initial listing reports uncertainty", "could not list open PRs" in out, True)
    check("failed initial listing does not arm", load_state("watchdog.json"), {})
    set_prs({"7": pr(7)})
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    old = load_state("watchdog.json")
    DATA["world"]["prs"] = None
    save_world()
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("failed listing preserves prior snapshot", load_state("watchdog.json")["heads"], old["heads"])
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("failed listing never drains queue", len(RECEIVED) - before, 0)
    check("failed listing leaves queue intact", f"{REPO}#7" in
          load_state("pending.json").get("reviewer", {}), True)

    # A malformed arming clock cannot grant a historical grace deadline. Recovery
    # snapshots only after a successful listing, while safe queued work still drains.
    for bad_clock in ("not-a-timestamp", [1], True, False, 0, time.time() + 86400):
        reset(prs={"7": pr(7), "9": pr(9, head=HEAD_B)})
        old_clock = time.time() - 7200
        state_file("watchdog.json").write_text(json.dumps({
            "armed_since": bad_clock,
            "heads": {"7": {"sha": HEAD_A, "observed_at": old_clock,
                             "last_seen_at": old_clock}}}))
        state_file("pending.json").write_text(json.dumps({"reviewer": {
            f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"},
            f"{REPO}#9": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "stale"}}}))
        before = len(RECEIVED)
        started = time.time()
        out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
        watch = load_state("watchdog.json")
        label = repr(bad_clock)
        check(f"{label} recovery does not crash", "watchdog failed" in out, False)
        check(f"{label} recovery does not alert", "silent stall" in out, False)
        check(f"{label} re-arms at recovery, not historical time",
              isinstance(watch.get("armed_since"), (int, float)) and
              started <= watch["armed_since"] <= time.time(), True)
        check(f"{label} baselines existing head", watch["heads"]["7"]["observed_at"], None)
        check(f"{label} drains authorized same head", len(RECEIVED) - before, 1)
        check(f"{label} leaves second queued item for capacity", f"{REPO}#9" in
              load_state("pending.json").get("reviewer", {}), True)
        out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
        check(f"{label} next sweep does not prematurely alert", "silent stall" in out, False)
        check(f"{label} retains uncertain first head but drops stale second head",
              set(load_state("pending.json").get("reviewer", {})),
              {f"{REPO}#7"})
        check(f"{label} never replays uncertain first head or wakes stale second head",
              len(RECEIVED) - before, 1)

    # Failed listing cannot establish a safe recovery baseline or drain.
    reset(prs={"7": pr(7)})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": [1]}))
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    DATA["world"]["prs"] = None
    save_world()
    before = len(RECEIVED)
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("invalid arming plus failed listing reports uncertainty", "could not list open PRs" in out, True)
    check("invalid arming plus failed listing retains state", load_state("watchdog.json")["armed_since"], [1])
    check("invalid arming plus failed listing does not drain", len(RECEIVED) - before, 0)
    check("invalid arming plus failed listing keeps queue", f"{REPO}#7" in
          load_state("pending.json").get("reviewer", {}), True)

    # Clock survives transient omission, draft, and close/reopen at the same SHA.
    for absent in (None, pr(7, head=HEAD_B, draft=True), pr(7, head=HEAD_B, state="closed")):
        reset(prs={"7": pr(7)})
        run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
        set_prs({"7": pr(7, head=HEAD_B)})
        run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
        watch = load_state("watchdog.json")
        watch["heads"]["7"]["observed_at"] = time.time() - 3600
        state_file("watchdog.json").write_text(json.dumps(watch))
        set_prs({"7": absent} if absent is not None else {})
        run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
        check(f"{absent and ('draft' if absent['draft'] else 'closed') or 'omitted'} retains clock",
              load_state("watchdog.json")["heads"]["7"]["observed_at"], watch["heads"]["7"]["observed_at"])
        set_prs({"7": pr(7, head=HEAD_B)})
        out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
        check("return at same SHA alarms on original clock", "reviewer never posted" in out, True)

    # Old noncurrent observations are pruned; malformed timestamps cannot crash a scan
    # or masquerade as a trusted grace clock.
    reset(prs={"7": pr(7)})
    run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    watch = load_state("watchdog.json")
    watch["heads"]["999"] = {"sha": HEAD_B, "observed_at": time.time() - 40 * 86400,
                                "last_seen_at": time.time() - 40 * 86400}
    watch["heads"]["7"]["observed_at"] = "not-a-timestamp"
    state_file("watchdog.json").write_text(json.dumps(watch))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("malformed clock never crashes scan", "watchdog failed" in out, False)
    check("malformed clock is not a false stall", "reviewer never posted" in out, False)
    check("aged absent observation is pruned", "999" in load_state("watchdog.json")["heads"], False)
    # After the bounded absence, an old PR at the same SHA is not a fresh push.
    watch = load_state("watchdog.json")
    watch["heads"]["7"] = {"sha": HEAD_A, "observed_at": time.time() - 41 * 86400,
                             "last_seen_at": time.time() - 40 * 86400}
    state_file("watchdog.json").write_text(json.dumps(watch))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=normal)
    check("expired clock cannot cause a false stall", "reviewer never posted" in out, False)
    check("expired old head is conservative baseline",
          load_state("watchdog.json")["heads"]["7"]["observed_at"], None)

    # stuck state, and paused means silent
    reset(prs={})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time() - 120 * 60, "head": HEAD_A, "why": "died"}}}))
    state_file("pending.json").write_text(json.dumps(
        {"fixer": {f"{REPO}#7": {"at": time.time() - 90 * 60, "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets")
    check("stuck: dead slot reported", "slot held 120m" in out, True)
    check("stuck: waiting request reported", "waiting 90m" in out, True)

    reset(prs={"7": pr(7)}, hooks_active=False)
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer",
                    extra_env={"REVIEW_LOOP_TEST": ""})
    check("paused loop drains nothing", "hooks are paused" in out, True)

    # Unknown is not paused (#54/#78): a dead read token is said out loud, never slept through.
    dead = TMP / "gh_dead.py"
    dead.write_text("#!/usr/bin/env python3\nprint('{\"__gh_stub_response__\": {\"status\": 401, "
                    "\"body\": {\"message\": \"Bad credentials\"}}}')\n")
    os.chmod(dead, 0o755)
    blind = {"REVIEW_LOOP_TEST": "", "REVIEW_LOOP_GH_STUB": str(dead)}
    reset(prs={"7": pr(7)})
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", extra_env=blind)
    check("unreadable hooks alert with login and status",
          "cannot read GitHub as rev-coach: HTTP 401" in out, True)
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer",
                    extra_env=blind)
    check("  drain says unreadable, not paused", "hook list unreadable" in out, True)


def group_explain() -> None:
    """``explain`` — the answer to "why is this PR not moving?".

    The golden cases are the states an operator actually meets at 2am: a review that went out, a
    review with no verdict, a verdict with no fix, a head nobody asked about, a PR queued behind a
    full seat, a spent budget, a paused loop, a closed PR, a PR that does not exist, and a GitHub
    call that failed. Two properties get their own checks: the conclusions come from the gates' own
    predicates (so the report cannot drift from what the loop does), and none of it writes.
    """
    section("explain — why is this PR not moving?")

    import argparse

    from review_loop import cli, config, gate, gh, state as state_mod

    def explain(loop: str | None = "widgets", pr_number: int = 7,
                extra_env: dict | None = None) -> tuple[int, str]:
        """The verb as the CLI runs it, with stdout captured and any stub override restored."""
        saved = {key: os.environ.get(key) for key in (extra_env or {})}
        os.environ.update(extra_env or {})
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_explain(ns(loop=loop, pr=pr_number))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return rc, buf.getvalue()

    def held(seat: str, head: str = HEAD_A, age_min: float = 4, number: int = 7) -> None:
        state_file("locks.json").write_text(json.dumps(
            {seat: {f"{REPO}#{number}": {"at": time.time() - age_min * 60, "head": head,
                                         "why": f"{seat} run"}}}))

    def without_adjudicator(loop_id: str) -> None:
        """A second loop over the same repo, with no adjudicator route configured."""
        loop_cfg = config.normalize(
            {"id": loop_id, "repo": REPO, "base": "main", "cap": 2,
             "fixers": [FIXER], "reviewers": [REVIEWER], "reviewer_seat": SEAT,
             "seats": {"reviewer": {"profile": "reviewer-profile", "route": "widgets-review"},
                       "fixer": {"profile": "fixer-profile", "route": "widgets-fix"}},
             "state_dir": str(STATE_DIR), "clone": str(CLONE), "host": HOST})
        (LOOPS_DIR / f"{loop_id}.json").write_text(json.dumps(loop_cfg))

    # -- a first review that was requested and is running --------------------------------
    reset(prs={"7": {**pr(7, requested=SEAT)}})
    held("reviewer", age_min=4)
    state_file("inflight.json").write_text(json.dumps({f"review:7:{HEAD_A}": time.time() - 4 * 60}))
    rc, out = explain()
    check("first review in flight: exits 0", rc, 0)
    check("  the next event is the reviewer's verdict", "reviewer's verdict at head aaaaaaa" in out, True)
    check("  with the round it is", "round 1 of 3" in out, True)
    check("  the PR is linked", f"https://github.com/{REPO}/pull/7" in out, True)
    check("  the seat holder and its age are shown", "reviewer holds it (4m of ttl 45m" in out, True)
    check("  the in-flight mark and its age are shown", "review for head aaaaaaa armed 4m ago" in out, True)
    check("  the pending request is named", "review requested from rev-seat" in out, True)
    check("  nothing is called a blocker", "blocked:    nothing — no guard" in out, True)
    check("  no token reaches the report", "token-reviewer" in out or "token-fixer" in out, False)
    check("  it labels when it read GitHub", bool(re.search(r"read:\s+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", out)), True)
    check("  and says the read wrote nothing", "read once, nothing written" in out, True)

    # -- a review at the head with no verdict -------------------------------------------
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, state="commented", rid=4)]}})
    rc, out = explain()
    check("commented review: counted as no verdict", "non-verdict review at head aaaaaaa (COMMENTED)" in out, True)
    check("  reviewer gate still accepts a fresh request", "does not suppress a fresh reviewer request" in out, True)
    check("  next is a fresh request", "the fixer asks for review of head aaaaaaa" in out, True)
    check("  no verdict was counted", "0/3 verdicts spent" in out, True)
    check("  live gate predicate ignores COMMENTED",
          gate.reviewed_at_head([review(REVIEWER, state="commented")], config.load_id("widgets"), HEAD_A), False)
    check("  live gate predicate accepts CHANGES_REQUESTED",
          gate.reviewed_at_head([review(REVIEWER)], config.load_id("widgets"), HEAD_A), True)

    # -- a verdict at the head with no fix run out --------------------------------------
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    rc, out = explain()
    check("verdict awaiting a fix: next is fixer gate retry",
          "re-deliver the changes-requested review event for head aaaaaaa" in out, True)
    check("  no imaginary fixer is told to push", "no fixer is running to push a fix" in out, True)
    check("  the missing run is named", "has no fix run out" in out, True)
    check("  budget counts verdicts at the head", "1/3 verdicts spent · 1 at head aaaaaaa" in out, True)
    check("  and labels the verdict's timestamp",
          "changes requested 2026-01-01T00:00:00Z by rev-coach" in out, True)

    held("fixer", age_min=2)
    rc, out = explain()
    check("fixer mid-turn: next is its push and ask", "frees the fixer's slot" in out, True)
    check("  and it is not called a stall", "blocked:    nothing" in out, True)

    # -- a new head nobody has asked about ---------------------------------------------
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head=HEAD_A, rid=5)]}})
    rc, out = explain()
    check("new head: nothing at this head", "nothing at head bbbbbbb" in out, True)
    check("  the fixer must ask for the review", "the fixer asks for review of head bbbbbbb" in out, True)
    check("  the missing request is called out", "no review request exists for head bbbbbbb" in out, True)
    check("  the spent verdict still counts", "1/3 verdicts spent" in out, True)

    # A pending request is not a running reviewer. The same request event must be replayed.
    reset(prs={"7": pr(7, requested=SEAT)})
    rc, out = explain()
    check("pending request without run: retry gate", "re-deliver the review_requested event" in out, True)
    check("pending request without run: no imaginary verdict", "next:       the reviewer's verdict" in out, False)
    check("pending request without run: pure kind", gate.explain(config.load_id("widgets"),
          state_mod.state_for(config.load_id("widgets")), 7,
          {"pr": pr(7, requested=SEAT), "reviews": [], "armed": True})["next"]["kind"], "retry")

    # Other PRs occupy the same slots the live gate checks, even before this PR is queued.
    reset(prs={"7": pr(7, requested=SEAT), "9": pr(9)})
    held("reviewer", number=9)
    rc, out = explain()
    check("other reviewer fills seat: capacity blocker", "reviewer seat at capacity 1/1 on other PRs" in out, True)
    check("other reviewer fills seat: replay queues until release",
          "reviewer gate will queue it at capacity 1/1" in out, True)
    check("same capacity predicate as gate", gate.seat_capacity(config.load_id("widgets"),
          state_mod.state_for(config.load_id("widgets")), "reviewer"), (1, 1))
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER)]}, "9": pr(9)})
    held("fixer", number=9)
    rc, out = explain()
    check("other fixer fills seat: capacity blocker", "fixer seat at capacity 1/1 on other PRs" in out, True)
    check("other fixer fills seat: replay queues until release",
          "fixer gate will queue it at capacity 1/1" in out, True)

    # A lock's PR key survives a push, but its recorded SHA is not authorization at the new SHA.
    reset(prs={"7": pr(7, head=HEAD_B, requested=SEAT)})
    held("reviewer", head=HEAD_A)
    rc, out = explain()
    check("stale reviewer lock: named", "reviewer lock targets an older head" in out, True)
    check("stale reviewer lock: no new-head verdict", "the reviewer's verdict at head bbbbbbb" in out, False)
    check("stale reviewer lock: release before replay", "release or wait for the stale reviewer lock" in out, True)
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head=HEAD_B)]}})
    held("fixer", head=HEAD_A)
    rc, out = explain()
    check("stale fixer lock: named", "fixer lock targets an older head" in out, True)
    check("stale fixer lock: no new-head push", "the fixer pushes a fix" in out, False)
    check("stale fixer lock: release before replay", "release or wait for the stale fixer lock" in out, True)

    # The opposite seat's completed turn is released by the handoff event itself.
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head=HEAD_B)]}})
    held("reviewer", head=HEAD_A)
    rc, out = explain()
    check("stale opposite reviewer lock: replay verdict releases it",
          "re-deliver the changes-requested review event" in out, True)
    check("stale opposite reviewer lock: no wait for expiry",
          "release or wait for the stale reviewer lock" in out, False)
    reset(prs={"7": pr(7, head=HEAD_B, requested=SEAT)})
    held("fixer", head=HEAD_A)
    rc, out = explain()
    check("stale opposite fixer lock: replay request releases it",
          "re-deliver the review_requested event" in out, True)
    check("stale opposite fixer lock: no wait for expiry",
          "release or wait for the stale fixer lock" in out, False)

    # -- queued behind a full seat ------------------------------------------------------
    reset(prs={"7": pr(7), "9": pr(9, head=HEAD_B)})
    held("reviewer", head=HEAD_A, age_min=12, number=7)
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#9": {"at": time.time() - 9 * 60, "head": HEAD_B, "url": "u",
                                    "reason": "reviewer at capacity 1/1: acme/widgets#7 (720s)"}}}))
    rc, out = explain(pr_number=9)
    check("queued: position and age are shown", "reviewer 1 of 1 (waiting 9m)" in out, True)
    check("  the gate's own reason is repeated",
          "reviewer at capacity 1/1: acme/widgets#7 (720s)" in out, True)
    check("  blocked by no capacity", "no capacity: queued with the reviewer seat" in out, True)
    check("  next is a freed slot", "a reviewer slot frees" in out, True)

    # A queue entry authorizes only its stored SHA. A slot freeing cannot run an old head.
    reset(prs={"9": pr(9, head=HEAD_A)})
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#9": {"at": time.time() - 9 * 60, "head": HEAD_B,
                                    "reason": "reviewer at capacity 1/1"}}}))
    rc, out = explain(pr_number=9)
    check("stale queue identifies old and live head", "queue targets head bbbbbbb, not current head aaaaaaa" in out, True)
    check("stale queue never promises release will launch it", "a reviewer slot frees" in out, False)
    check("stale queue asks for a fresh request", "the fixer asks for review of head aaaaaaa" in out, True)

    # -- the cap is spent ---------------------------------------------------------------
    spent_reviews = [review(REVIEWER, head="c" * 40, rid=1), review(REVIEWER, head="d" * 40, rid=2),
                     review(REVIEWER, head="e" * 40, rid=3)]
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": spent_reviews}})
    state_file("breach.json").write_text(json.dumps(
        {f"{REPO}#7": {"pr": 7, "head": HEAD_B, "rounds": 3, "cap": 3, "at": PAST,
                       "status": "awaiting-adjudication", "reason": "review cap reached"}}))
    rc, out = explain()
    check("cap spent: the escalation is reported",
          "escalation: awaiting-adjudication at head bbbbbbb" in out, True)
    check("  with the marker's own timestamp", PAST in out, True)
    check("  and a blocker that says why", "parked awaiting adjudication" in out, True)
    check("  next is the ruling", "the adjudicator rules at head bbbbbbb" in out, True)

    state_file("breach.json").write_text(json.dumps(
        {f"{REPO}#7": {"pr": 7, "head": HEAD_B, "rounds": 3, "cap": 3, "at": PAST,
                       "status": "adjudicating", "reason": "review cap reached"}}))
    rc, out = explain()
    check("consumed marker: adjudicating is parked", "parked awaiting adjudication" in out, True)
    check("consumed marker: next is ruling, not redelivery",
          "the adjudicator rules at head bbbbbbb" in out and "re-deliver" not in out, True)

    # POST failure leaves a pending marker; only an acknowledged delivery promises a ruling.
    state_file("breach.json").write_text(json.dumps(
        {f"{REPO}#7": {"pr": 7, "head": HEAD_B, "rounds": 3, "cap": 3, "at": PAST,
                       "status": "delivery-pending", "reason": "review cap reached"}}))
    rc, out = explain()
    check("pending breach: retry delivery", "retry adjudicator delivery for head bbbbbbb" in out, True)
    check("pending breach: no promised ruling", "the adjudicator rules at head bbbbbbb" in out, False)
    check("pending breach: blocker is delivery", "adjudicator delivery pending" in out, True)
    check("pending marker uses live head predicate", gate.breach_delivery_status(
          {"head": HEAD_B, "status": "delivery-pending"}, HEAD_B), "delivery-pending")

    # A dismissed verdict can bring the live count back below cap. The watchdog
    # refuses this delivery, so explain must not promise a retry on its next sweep.
    set_prs({"7": {**pr(7, head=HEAD_B, requested=SEAT), "reviews": spent_reviews[:-1]}})
    rc, out = explain()
    check("pending marker below cap: no watchdog retry promised",
          "retry adjudicator delivery" in out, False)
    check("pending marker below cap: review request can be replayed",
          "re-deliver the review_requested event" in out, True)
    check("pending marker below cap: stale delivery not blocking",
          "adjudicator delivery pending" in out, False)
    check("old marker does not park new head", gate.breach_delivery_status(
          {"head": HEAD_A, "status": "awaiting-adjudication"}, HEAD_B), "")

    # The third verdict was dismissed after escalation. Its historical marker
    # remains on this head, but the live gate can start round three again.
    for status in ("awaiting-adjudication", "adjudicating"):
        reset(prs={"7": {**pr(7, head=HEAD_B, requested=SEAT),
                         "reviews": [*spent_reviews[:-1],
                                     review(REVIEWER, state="dismissed", head="e" * 40,
                                            rid=3)]}})
        state_file("breach.json").write_text(json.dumps(
            {f"{REPO}#7": {"pr": 7, "head": HEAD_B, "rounds": 3, "cap": 3,
                            "at": PAST, "status": status, "reason": "review cap reached"}}))
        report = gate.explain(config.load_id("widgets"),
                              state_mod.state_for(config.load_id("widgets")), 7,
                              {"pr": pr(7, head=HEAD_B, requested=SEAT),
                               "reviews": DATA["world"]["prs"]["7"]["reviews"],
                               "armed": True})
        check(f"dismissed third verdict / {status}: live count", report["spent"], 2)
        check(f"dismissed third verdict / {status}: no parked blocker",
              any("parked awaiting adjudication" in b for b in report["blockers"]), False)
        check(f"dismissed third verdict / {status}: explain asks for gate replay",
              (report["next"]["kind"],
               "re-deliver the review_requested event" in report["next"]["action"]),
              ("retry", True))
        check(f"dismissed third verdict / {status}: marker stays diagnostic",
              report["escalation"].startswith(f"{status} at head bbbbbbb"), True)
        check_eligible(f"dismissed third verdict / {status}: reviewer gate holds round three",
                       "gate_reviewer.py", pr_payload(head=HEAD_B),
                       "reviewer", head=HEAD_B)

    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": spent_reviews}})
    rc, out = explain()
    check("cap spent with no marker: the gate never fired", "no escalation marker" in out, True)
    check("  missing marker retries cap event", "create and deliver the missing breach marker" in out, True)

    without_adjudicator(loop_id="widgets-solo")
    rc, out = explain(loop="widgets-solo")
    check("no adjudicator route: said plainly", "has no adjudicator route" in out, True)
    check("  and it points at a human", "rule by hand" in out, True)

    # -- paused, closed, missing, unreadable --------------------------------------------
    reset(prs={"7": pr(7)}, hooks_active=False)
    rc, out = explain()
    check("paused: both hooks reported off", "PAUSED — seat route(s) without an active repo hook: reviewer, fixer" in out, True)
    check("  blocked by the pause", "paused loop: seat route(s) without an active repo hook: reviewer, fixer" in out, True)
    check("  next is re-arming", "hermes review-loop arm --loop widgets" in out, True)

    reset(prs={"7": pr(7)})
    partial = world({"7": pr(7)})
    partial["hooks"] = partial["hooks"][:1]
    WORLD_FILE.write_text(json.dumps(partial))
    rc, out = explain()
    check("one active hook diagnoses missing fixer only", "PAUSED — seat route(s) without an active repo hook: fixer" in out, True)
    check("  next is re-arm", "hermes review-loop arm --loop widgets" in out, True)
    check("  watchdog shares the both-hook predicate", gate.hooks_armed(config.load_id("widgets")), False)
    partial["hooks"] = world({"7": pr(7)})["hooks"]
    WORLD_FILE.write_text(json.dumps(partial))
    check("both active hooks arm the loop", gate.hooks_armed(config.load_id("widgets")), True)
    partial["hooks"][0]["active"] = False
    WORLD_FILE.write_text(json.dumps(partial))
    check("inactive reviewer hook diagnoses reviewer only", "active repo hook: reviewer" in explain()[1], True)

    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z")})
    rc, out = explain()
    check("closed: the state is named", "state:      merged" in out, True)
    check("  the loop is over for it", "the loop is over for it" in out, True)
    check("  next is nothing", "nothing — the PR is merged" in out, True)

    reset(prs={"7": pr(7)})
    rc, out = explain(pr_number=404)
    check("missing PR: GitHub having none is said plainly", "no PR #404 in acme/widgets" in out, True)
    check("  the state is unknown, not closed", "the PR is closed" in out, False)
    check("  next says there is nothing to drive", "nothing to drive" in out, True)

    # GitHub deliberately reports inaccessible repositories/PRs as HTTP 404.
    with mock.patch.object(gh, "fetch", side_effect=[(None, 'HTTP 404 {"message":"Not Found"}'),
                                                       (None, "HTTP 404"), ([], "")]):
        facts_404 = gate.explain_facts(config.load_id("widgets"), 404)
    check("HTTP 404 is missing or inaccessible, not transient", facts_404["pr_error"], "")
    check("  404 diagnosis does not recommend retry", gate.explain(config.load_id("widgets"),
          state_mod.state_for(config.load_id("widgets")), 404, facts_404)["next"]["kind"], "none")

    reset(prs={"7": {**pr(7), "head": {}}})
    rc, out = explain()
    check("PR without a head diagnoses unknown head", "PR head missing or malformed" in out, True)
    check("  cannot suggest a review request", "the fixer asks for review" in out, False)
    check("  suggests retrying the malformed PR read", "retry the PR read" in out, True)

    reset(prs={"7": pr(7)})
    rc, out = explain(extra_env={"REVIEW_LOOP_GH_STUB": "/bin/false"})
    check("API failure: labelled a stale/failing read", "stale/failing GitHub read" in out, True)
    check("  the PR is unknown rather than closed",
          "unknown — the PR itself could not be read" in out, True)
    check("  the verdict count is not guessed",
          "unknown — the review list could not be read" in out, True)
    check("  next is a retry", "retry the GitHub read" in out, True)
    check("  and it does not claim the loop is paused", "PAUSED" in out, False)

    # -- the two properties the issue is really about -----------------------------------
    def snapshot() -> dict:
        files: dict = {}
        for base in (STATE_DIR, LOOPS_DIR):
            for path in sorted(pathlib.Path(base).rglob("*")):
                if path.is_file():
                    files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (WORLD_FILE, SUBS):
            files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    reset(prs={"7": {**pr(7, requested=SEAT)}})
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time() - 120, "head": HEAD_A, "why": "review"},
                      f"{REPO}#8": {"at": time.time() - 120 * 60, "head": HEAD_B, "why": "died"}}}))
    state_file("pending.json").write_text(json.dumps(
        {"fixer": {f"{REPO}#9": {"at": time.time(), "head": HEAD_B, "url": "u",
                                 "reason": "fixer at capacity 1/1"}}}))
    state_file("inflight.json").write_text(json.dumps({f"review:7:{HEAD_A}": time.time() - 60}))
    state_file("breach.json").write_text(json.dumps(
        {f"{REPO}#7": {"head": "c" * 40, "status": "awaiting-adjudication", "at": PAST,
                       "reason": "cap"}}))
    before, fired = snapshot(), len(RECEIVED)
    explain()
    explain(pr_number=9)
    after = snapshot()
    check("explain twice: fixture paths and SHA-256 hashes unchanged", before == after, True)
    check("  and it compared both loops' and the state's files", len(before) >= 7, True)
    check("  and no webhook was fired", len(RECEIVED) - fired, 0)
    check("  the expired lock was not pruned away",
          f"{REPO}#8" in load_state("locks.json").get("reviewer", {}), True)
    check("  the queue was not touched",
          load_state("pending.json").get("fixer", {}).get(f"{REPO}#9", {}).get("reason"),
          "fixer at capacity 1/1")

    # -- the pure decision, called as the gates call their predicates -------------------
    reset(prs={})                      # no leftover locks: these facts say who holds what
    loop = config.load_id("widgets")
    st = state_mod.state_for(loop)

    def decide(**over) -> dict:
        facts = {"pr": pr(7), "pr_error": "", "reviews": [], "reviews_error": "",
                 "armed": True, "armed_error": "", "read_at": 1_700_000_000.0}
        facts.update(over)
        return gate.explain(loop, st, 7, facts)

    check("a failed read → retry", decide(pr=None, pr_error="HTTP 500")["next"]["kind"], "retry")
    check("no such PR → nothing", decide(pr=None)["next"]["kind"], "none")
    check("an unreadable review list → retry",
          decide(reviews=None, reviews_error="HTTP 403")["next"]["kind"], "retry")
    check("unreadable hooks are not called paused", decide(armed=None)["hooks"].startswith("unknown"),
          True)
    check("  and that is named as a blocker",
          any("hook state unreadable" in text for text in decide(armed=None)["blockers"]), True)
    check("paused → re-arm", decide(armed=False)["next"]["kind"], "rearm")
    check("a draft → ready_for_review", decide(pr=pr(7, draft=True))["next"]["kind"], "ready")
    check("unverified stacked base → retry, not a run", decide(pr=pr(7, base="release"))["next"]["kind"], "retry")
    from review_loop import situation
    verified_stack = situation.Resolution("waiting", "waiting on #6",
        situation.Identity(HEAD_A, "release", HEAD_B, ((6, "release", HEAD_B, HEAD_A),)), (6,))
    stacked_kind = decide(pr=pr(7, base="release"), chain=verified_stack,
                          parent_readiness=(False, "approval unassociated"))["next"]["kind"]
    check("verified stacked branch waits", stacked_kind, "wait")
    check("stacked wait is a declared explain kind", stacked_kind in gate.EXPLAIN_KINDS, True)
    check("someone else's PR → nothing", decide(pr=pr(7, author="outsider"))["next"]["kind"], "none")
    check("approved at the head → nothing",
          decide(reviews=[review(REVIEWER, state="approved")])["next"]["kind"], "none")
    check("a request pending with no run out → retry",
          decide(pr=pr(7, requested=SEAT))["next"]["kind"], "retry")
    check("nothing pending at all → ask for review", decide()["next"]["kind"], "review-request")
    check("every conclusion is a declared kind", decide()["next"]["kind"] in gate.EXPLAIN_KINDS, True)
    check("the same facts decide the same way", decide() == decide(), True)

    # -- asking without --loop ----------------------------------------------------------
    reset(prs={"7": pr(7)})
    rc, out = explain(loop=None)
    check("omitted --loop: the only loop answers", rc, 0)
    check("  and it is the widgets loop", "[widgets] acme/widgets#7" in out, True)

    (LOOPS_DIR / "second.json").write_text(json.dumps(config.normalize(
        {"id": "second", "repo": "acme/second", "fixers": [FIXER], "reviewers": [REVIEWER],
         "reviewer_seat": SEAT,
         "seats": {"reviewer": {"profile": "reviewer-profile", "route": "second-review"},
                   "fixer": {"profile": "fixer-profile", "route": "second-fix"}}})))
    rc, out = explain(loop=None)
    check("two loops: refuses instead of guessing", rc, 2)
    check("  and names them", "second, widgets" in out, True)
    (LOOPS_DIR / "second.json").unlink()
    rc, out = explain(loop=None)
    check("back to one loop: answers again", rc, 0)

    rc, out = explain(loop="nope")
    check("an unknown loop is refused", rc, 2)
    check("  with the reason", "no such loop" in out, True)

    fake = FakeCtx()
    cli.register_cli(fake)
    parser = argparse.ArgumentParser(prog="hermes review-loop")
    fake.setup(parser)
    parsed = parser.parse_args(["explain", "--loop", "widgets", "--pr", "7"])
    check("`explain --loop widgets --pr 7` parses", (parsed.command, parsed.pr), ("explain", 7))
    check("  and the CLI explains without a loop too",
          parser.parse_args(["explain", "--pr", "7"]).loop, None)
    try:
        with contextlib.redirect_stderr(io.StringIO()):    # argparse legitimately shouts here
            parser.parse_args(["explain", "--loop", "widgets"])
    except SystemExit:
        check("--pr is required", True, True)
    else:
        check("--pr is required", False, True)


GROUPS = {
    "watchdog": group_watchdog,
    "explain": group_explain,
}
