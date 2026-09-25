"""Config loading, the reviewer and fixer gates, the budget cap and the adjudicator marker."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


# == groups ======================================================================

def group_config() -> None:
    section("config")
    from review_loop import config

    loop = config.load_id("widgets")
    check("loop resolves by repo", config.by_repo(REPO)["id"], "widgets")
    check("unknown repo → nothing", config.by_repo("nope/nope"), None)
    check("cap kept", loop["cap"], 3)
    check("artifacts path is per PR", str(config.artifacts_dir(loop, 7)).endswith("artifacts/7"), True)

    # Cleanup deletes PR-named children of every root: a root above the operator's own files
    # would put their projects in scope.
    home_dir = pathlib.Path.home()
    for label, value in (("/", "/"), ("home", "~"), ("home (absolute)", str(home_dir)),
                         ("ancestor of home", str(home_dir.parent))):
        try:
            config.normalize({**json.loads((LOOPS_DIR / "widgets.json").read_text()),
                              "roots": [value]})
            check(f"root {label} is refused", "accepted", "ConfigError")
        except config.ConfigError:
            check(f"root {label} is refused", "ConfigError", "ConfigError")
    check("a directory under home is still a valid root", config.normalize(
        {**json.loads((LOOPS_DIR / "widgets.json").read_text()),
         "roots": ["~/.hermes/cache/scratch"]})["roots"], ["~/.hermes/cache/scratch"])

    bad = {"repo": "no-slash", "fixers": ["x"], "reviewers": ["y"],
           "seats": {"reviewer": {"route": "r", "profile": "p"}, "fixer": {"route": "r", "profile": "p"}}}
    try:
        config.normalize(bad)
        check("repo without a slash is refused", "accepted", "ConfigError")
    except config.ConfigError:
        check("repo without a slash is refused", "ConfigError", "ConfigError")

    missing_seat = {"repo": "a/b", "fixers": ["x"], "reviewers": ["y"], "seats": {"reviewer": {"route": "r", "profile": "p"}}}
    try:
        config.normalize(missing_seat)
        check("missing seat is refused", "accepted", "ConfigError")
    except config.ConfigError:
        check("missing seat is refused", "ConfigError", "ConfigError")


def group_reviewer_gate() -> None:
    section("reviewer gate — who gets to start a review")

    reset(prs={"7": pr(7)})
    check_eligible("explicit request for this seat", "gate_reviewer.py", pr_payload(), "reviewer")

    reset(prs={"7": pr(7)})
    check_rejected("request naming another reviewer is silent", "gate_reviewer.py",
                   pr_payload(requested="someone-else"))

    reset(prs={"7": pr(7)})
    check_rejected("request from a stranger is silent", "gate_reviewer.py",
                   pr_payload(sender="passer-by"))

    reset(prs={"7": pr(7)})
    # No hardcoded account outside the loop's own config may hand a PR to review.
    check_rejected("request from an unconfigured org account is silent", "gate_reviewer.py",
                   pr_payload(sender="patchhive"))
    check_rejected("a plain push (synchronize) is silent", "gate_reviewer.py",
                   pr_payload(action="synchronize"))

    reset(prs={"7": pr(7)})
    check_eligible("opened is eligible", "gate_reviewer.py", pr_payload(action="opened"), "reviewer")

    reset(prs={"7": pr(7)})
    check_rejected("draft is silent", "gate_reviewer.py", pr_payload(draft=True))

    reset(prs={"7": pr(7)})
    check_rejected("wrong base branch is silent", "gate_reviewer.py", pr_payload(base="release"))

    reset(prs={"7": pr(7, author="outsider")})
    check_rejected("a stranger's PR is silent", "gate_reviewer.py", pr_payload(author="outsider"))

    reset(prs={"7": pr(7)})
    check_rejected("another repository is silent", "gate_reviewer.py",
                   {**pr_payload(), "repository": {"full_name": "other/repo"}})

    reset(prs={"7": pr(7)})
    check_rejected("an unknown action is silent", "gate_reviewer.py",
                   pr_payload(action="labeled"))

    # A delayed request must not resurrect a closed, deleted, or advanced PR,
    # even if its webhook snapshot still describes a valid open head.
    for label, current in (("closed", pr(7, state="closed")),
                           ("missing", None), ("superseded", pr(7, head=HEAD_B)),
                           ("draft", pr(7, draft=True)),
                           ("retargeted", pr(7, base="release")),
                           ("transferred", pr(7, author="outsider"))):
        reset(prs={"7": current} if current else {})
        state_file("locks.json").write_text(json.dumps({"fixer": {
            f"{REPO}#7": {"at": time.time(), "head": HEAD_A}}}))
        check(f"delayed request for {label} PR is silent",
              run("gate_reviewer.py", pr_payload())[0], "SILENT")
        check(f"  {label} PR did not release fixer",
              f"{REPO}#7" in load_state("locks.json").get("fixer", {}), True)
        check(f"  {label} PR did not claim reviewer",
              load_state("locks.json").get("reviewer", {}), {})
    reset(prs={"7": pr(7)})
    check("failed fresh PR lookup is silent",
          run("gate_reviewer.py", pr_payload(),
              extra_env={"REVIEW_LOOP_GH_STUB": "/bin/false"})[0], "SILENT")

    # a head that already has a verdict from a reviewer
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER)]}})
    check_rejected("head already reviewed → silent", "gate_reviewer.py", pr_payload())

    # Only a submitted verdict closes this head. COMMENTED is an ordinary review
    # comment, PENDING is not submitted, and DISMISSED has lost its verdict.
    for state in ("COMMENTED", "PENDING", "DISMISSED"):
        reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, state=state)]}})
        check_eligible(f"{state} at head does not suppress review_requested",
                       "gate_reviewer.py", pr_payload(), "reviewer")

    for state in ("APPROVED", "CHANGES_REQUESTED"):
        reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, state=state)]}})
        check(f"{state} at head suppresses duplicate request",
              run("gate_reviewer.py", pr_payload())[0], "SILENT")

    reset(prs={"7": {**pr(7), "reviews": [review("unconfigured", state="APPROVED")]}})
    check_eligible("unconfigured reviewer's verdict does not suppress request",
                   "gate_reviewer.py", pr_payload(), "reviewer")

    # an approved head
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, state="approved")]}})
    check_eligible("new commit after a verdict", "gate_reviewer.py",
                   pr_payload(head=HEAD_B), "reviewer", head=HEAD_B)

    # the review list is unreadable: never guess a round count
    reset(prs={"7": pr(7)})
    kind, _, err = run("gate_reviewer.py", pr_payload(), extra_env={"REVIEW_LOOP_GH_STUB": "/bin/false"})
    check("unreadable review list → silent (never guess)", kind, "SILENT")
    check("  and it says why", "unavailable" in err or "not guessing" in err, True)

    # A syntactically valid but wrong-shaped API response is still an unknown
    # round count. It must not release an already occupied reviewer seat.
    reset(prs={"7": {**pr(7), "reviews": {"message": "bad response"}}})
    state_file("locks.json").write_text(json.dumps({"fixer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A}}}))
    check("object review list fails closed", run("gate_reviewer.py", pr_payload())[0], "SILENT")
    check("  malformed list keeps fixer seat", f"{REPO}#7" in
          load_state("locks.json").get("fixer", {}), True)
    check("  malformed list never claims reviewer seat",
          load_state("locks.json").get("reviewer", {}), {})
    reset(prs={"7": {**pr(7), "reviews": {"message": "bad response"}}})
    state_file("locks.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A}}}))
    check("fixer refuses malformed review list",
          run("gate_fixer.py", review_payload())[0], "SILENT")
    check("  malformed list keeps reviewer seat", f"{REPO}#7" in
          load_state("locks.json").get("reviewer", {}), True)


def group_budget() -> None:
    section("budget — the cap is a wall, not a suggestion")

    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head="c" * 40, rid=1),
                                                       review(REVIEWER, head="d" * 40, rid=2),
                                                       review(REVIEWER, head="e" * 40, rid=3)]}})
    kind, out, err = run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("third verdict spent → no fourth review", kind, "SILENT")
    check("  no review run queued at cap", load_state("pending.json"), {})
    check("  no ledger run at cap", no_ledger_run(), True)
    check("  breach marker stays pending without a private worker runtime",
          load_state("breach.json").get(f"{REPO}#7", {}).get("status"), "delivery-pending")
    check("  no gateway adjudicator POST", RECEIVED, [])

    # one wake per head
    before = len(RECEIVED)
    run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("  same head does not re-wake", len(RECEIVED) - before, 0)

    # under the cap: still a normal review
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head="c" * 40, rid=1)]}})
    kind, out, _ = run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("one verdict in → eligible but silent", (kind, out), ("SILENT", "[SILENT]"))
    check("  held for isolated worker", held("reviewer", 7, HEAD_B), True)
    check("  no gateway dispatch", no_ledger_run(), True)

    section("fixer gate — the cap stops the fix, not just the review")
    # the fixer gate counts the OTHER verdicts; 2 prior + this one = the cap → adjudication
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=8),
                                          review(REVIEWER, rid=9),
                                          review(REVIEWER, rid=10)]}})
    kind, out, err = run("gate_fixer.py", review_payload(rid=10))
    check("verdict that hits the cap → no fix run", kind, "SILENT")
    check("  no fix run queued at cap", load_state("pending.json"), {})
    check("  no ledger run at cap", no_ledger_run(), True)
    check("  breach marker stays pending without a private worker runtime",
          load_state("breach.json").get(f"{REPO}#7", {}).get("status"), "delivery-pending")
    check("  no gateway adjudicator POST", RECEIVED, [])

    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    kind, out, _ = run("gate_fixer.py", review_payload(rid=5))
    check("first changes-requested → eligible but silent", (kind, out), ("SILENT", "[SILENT]"))
    check("  fix held for isolated worker", held("fixer", 7, HEAD_A), True)
    check("  no gateway dispatch", no_ledger_run(), True)

def group_adjudicator() -> None:
    """Keep #21 breach guards; wake only an isolated turn, never the gateway route."""
    from review_loop import cli, config, gate, prompts, state as state_mod
    from review_loop.run_supervisor import Supervisor

    section("adjudicator — guarded marker, isolated turn, no legacy gateway dispatch")
    reviews = [review(REVIEWER, head=ch * 40, rid=i) for i, ch in enumerate("cde", 1)]
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": reviews}})
    cli._install_routes(config.load_id("widgets"))
    route = json.loads(SUBS.read_text())["widgets-breach"]
    check("adjudicator route retains its own gate", route["script"], "gate_adjudicator.py")
    check("adjudicator retains ruling prompt", route["prompt"] == prompts.ADJUDICATOR, True)
    check("cap blocks fourth review", run("gate_reviewer.py", pr_payload(head=HEAD_B))[0], "SILENT")
    marker = load_state("breach.json")[f"{REPO}#7"]
    check("no private runtime: wake fails closed, marker stays delivery-pending",
          marker["status"], "delivery-pending")
    check("no private runtime: no adjudicator ledger run", no_ledger_run(), True)
    check("wake never POSTs to the gateway", len(RECEIVED), 0)
    run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("repeat head does not POST", len(RECEIVED), 0)
    set_prs({"7": {**pr(7, head=HEAD_A), "reviews": reviews}})
    run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("stale event cannot regress marker", load_state("breach.json")[f"{REPO}#7"]["head"], HEAD_B)
    set_prs({"7": {**pr(7, head=HEAD_B), "reviews": reviews}})
    run("watchdog.py", None, "--loop", "widgets")
    check("watchdog cannot deliver without a runtime", len(RECEIVED), 0)
    check("watchdog leaves marker retryable", load_state("breach.json")[f"{REPO}#7"]["status"],
          "delivery-pending")

    # With a private runtime file the wake is a durable isolated enqueue. The detached worker
    # is not started here (it would read real GitHub); the ledger row is the contract.
    runtime = HOME / "review-loop-runtime.json"
    runtime.write_text("{}")
    runtime.chmod(0o600)
    db = HOME / "state" / "review-loop-runs.sqlite"

    def adjudicator_rows() -> list:
        if not db.exists():
            return []
        with sqlite3.connect(db) as con:
            return con.execute("SELECT head,turn_key,state FROM runs WHERE seat='adjudicator' "
                               "ORDER BY created").fetchall()

    try:
        loop = config.load_id("widgets")
        st = state_mod.state_for(loop)
        with mock.patch.object(Supervisor, "_spawn", side_effect=OSError("spawn refused")):
            gate.breach(loop, st, 7, HEAD_B, 3, "review cap reached")
        check("spawn failure after a durable row keeps the marker retryable",
              st.breach_get(7)["status"], "delivery-pending")
        check("  ...and the row waits pending for a re-arm",
              adjudicator_rows(), [(HEAD_B, "breach:3", "pending")])
        with mock.patch.object(Supervisor, "_spawn") as spawn:
            gate.breach(loop, st, 7, HEAD_B, 3, "review cap reached")
            check("watchdog-style retry re-arms the same turn", spawn.called, True)
        check("durable enqueue promotes marker to awaiting-adjudication",
              st.breach_get(7)["status"], "awaiting-adjudication")
        check("one isolated adjudicator turn per breach", adjudicator_rows(),
              [(HEAD_B, "breach:3", "pending")])
        with mock.patch.object(Supervisor, "_spawn"):
            gate.breach(loop, st, 7, HEAD_B, 3, "review cap reached")
            marker = st.breach_get(7)
            marker["status"] = "delivery-pending"          # an acknowledgement lost in transit
            state_file("breach.json").write_text(json.dumps({f"{REPO}#7": marker}))
            gate.breach(loop, st, 7, HEAD_B, 3, "review cap reached")
        check("redelivery dedups on the ledger's unique turn", len(adjudicator_rows()), 1)
        check("  ...and still acknowledges the marker", st.breach_get(7)["status"],
              "awaiting-adjudication")
        check("no gateway POST on an isolated wake", len(RECEIVED), 0)
        head_c = "f" * 40
        set_prs({"7": {**pr(7, head=head_c), "reviews": reviews}})
        with mock.patch.object(Supervisor, "_spawn"):
            gate.breach(loop, st, 7, head_c, 3, "review cap reached")
        check("a re-breach at a new head is a new turn", [r[0] for r in adjudicator_rows()],
              [HEAD_B, head_c])
        with mock.patch.object(Supervisor, "_spawn"):
            gate.breach(loop, st, 7, HEAD_B, 3, "review cap reached")   # live head is still C
        check("a late event for the old head cannot re-arm it", st.breach_get(7)["head"], head_c)
        check("  ...nor enqueue another turn", len(adjudicator_rows()), 2)
    finally:
        runtime.unlink(missing_ok=True)

    # A stale pre-hold acknowledged marker and signed wake must not bypass the hold.
    set_prs({"7": {**pr(7, head=HEAD_B), "reviews": reviews}})
    marker = {"pr": 7, "head": HEAD_B, "rounds": 3, "cap": 3, "reason": "legacy",
              "status": "awaiting-adjudication"}
    state_file("breach.json").write_text(json.dumps({f"{REPO}#7": marker}))
    wake = {**pr_payload(head=HEAD_B), "action": "review_loop_breach", "number": 7,
            "_loop": {"role": "adjudicator", "pr": 7, "head": HEAD_B}}
    check("legacy acknowledged wake cannot dispatch gateway adjudicator",
          run(route["script"], wake)[0], "SILENT")
    check("legacy marker is not consumed", load_state("breach.json")[f"{REPO}#7"]["status"],
          "awaiting-adjudication")


def group_fixer_gate() -> None:
    section("fixer gate — only a verdict it must answer")
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    check_rejected("approved → silent", "gate_fixer.py", review_payload(state="approved", rid=5))
    check_rejected("commented → silent", "gate_fixer.py", review_payload(state="commented", rid=5))
    check_rejected("verdict from a non-reviewer → silent", "gate_fixer.py",
                   review_payload(login="passer-by", rid=5))
    check_rejected("verdict on an older head → silent", "gate_fixer.py",
                   review_payload(commit="c" * 40, rid=5))
    check_rejected("dismissed review event → silent", "gate_fixer.py",
                   {**review_payload(rid=5), "action": "dismissed"})
    check_eligible("uppercase state still accepted (REST spelling)", "gate_fixer.py",
                   review_payload(state="CHANGES_REQUESTED", rid=5), "fixer")

    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    run("gate_fixer.py", review_payload(rid=5))
    check("same head twice → second is silent",
          run("gate_fixer.py", review_payload(rid=5))[0], "SILENT")

    # Trunk moving on does not supersede a verdict on a direct-trunk PR: the webhook's base
    # snapshot differs from the live base only because main advanced.
    reset(prs={"7": {**pr(7), "base": {"ref": "main", "sha": HEAD_B},
                        "reviews": [review(REVIEWER, rid=5)]}})
    state_file("locks.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "why": "review"}}}))
    moved = review_payload(rid=5)
    moved["pull_request"]["base"] = {"ref": "main", "sha": HEAD_A}
    check_eligible("verdict after trunk advanced still starts the fixer", "gate_fixer.py",
                   moved, "fixer")
    reset(prs={"7": {**pr(7), "base": {"ref": "main", "sha": HEAD_B},
                        "reviews": [review(REVIEWER, state="approved", rid=5)]}})
    state_file("locks.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "why": "review"}}}))
    moved["review"]["state"] = "approved"
    before = len(RECEIVED)
    check("approval after trunk advanced is silent", run("gate_fixer.py", moved)[0], "SILENT")
    check("  and still frees the claim for its head", "reviewer" in load_state("locks.json"), False)
    check("  and produces no gateway wake", [r for r in RECEIVED[before:]
          if r["path"].endswith("/webhooks/widgets-review")], [])

    reset(prs={"7": {**pr(7, base="parent"), "reviews": [review(REVIEWER, rid=5)]}})
    old = review_payload(rid=5)
    before = len(RECEIVED)
    check("retargeted stacked PR refuses old fixer wake", run("gate_fixer.py", old)[0], "SILENT")
    check("retargeted stacked PR produces no POST", len(RECEIVED), before)

    # The payload's head is a snapshot: the live PR is what authorizes a fix run.
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, rid=5)]}})
    check("live PR moved past the verdict's head → silent",
          run("gate_fixer.py", review_payload(rid=5))[0], "SILENT")
    reset(prs={"7": {**pr(7, state="closed"), "reviews": [review(REVIEWER, rid=5)]}})
    check("live PR closed → silent", run("gate_fixer.py", review_payload(rid=5))[0], "SILENT")
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    check("unreadable live PR → silent", run("gate_fixer.py", review_payload(rid=5),
          extra_env={"REVIEW_LOOP_GH_STUB": "/bin/false"})[0], "SILENT")
    later_approval = {**review(REVIEWER, state="APPROVED", rid=6),
                      "submitted_at": "2026-01-02T00:00:00Z"}
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5), later_approval]}})
    check("changes-requested superseded by a same-head approval → silent",
          run("gate_fixer.py", review_payload(rid=5))[0], "SILENT")
    check("  and no fixer seat is claimed", "fixer" in load_state("locks.json"), False)
    dismissed = {**review(REVIEWER, state="DISMISSED", rid=5)}
    reset(prs={"7": {**pr(7), "reviews": [dismissed]}})
    check("changes-requested dismissed since → silent",
          run("gate_fixer.py", review_payload(rid=5))[0], "SILENT")

    # An approval always ends the reviewer's turn, even when GitHub cannot be read back:
    # only the merge handoff waits on the live read, never the seat release.
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, state="approved", rid=9)]}})
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "why": "review"}}}))
    run("gate_fixer.py", review_payload(state="approved", rid=9),
        extra_env={"REVIEW_LOOP_GH_STUB": "/bin/false"})
    check("approval with an unreadable PR still frees the reviewer seat",
          "reviewer" in load_state("locks.json"), False)

    # The drain re-reads GitHub: an older changes-requested at the head is not a work order
    # once a later approval landed at that same head.
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5), later_approval]}})
    state_file("pending.json").write_text(json.dumps({"fixer": {
        f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "fixer")
    check("drain never wakes the fixer past a later approval", len(RECEIVED) - before, 0)
    check("  and the stale queue entry is dropped", load_state("pending.json"), {})


GROUPS = {
    "config": group_config,
    "reviewer": group_reviewer_gate,
    "budget": group_budget,
    "adjudicator": group_adjudicator,
    "fixer": group_fixer_gate,
}
