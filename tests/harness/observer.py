"""The observer feed, its durable claim, and its CLI."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


def group_observer() -> None:
    """The read-only feed: one notice per transition, none for a repeat, never a gate."""
    section("observer — a feed of transitions, not a third seat")

    # -- opt-in: with no observer configured, nothing about the loop changes ------
    reset(prs={"7": pr(7)})
    kind, out, err = run("gate_reviewer.py", pr_payload())
    check("no observer → the review still runs", kind, "SILENT")
    check("  nothing is sent to a feed", observer_posts(), [])
    check("  and no ledger is written", state_file("observations.json").exists(), False)

    # -- a fixer handoff is one short, linked notice -------------------------------
    reset(prs={"7": pr(7)})
    observer_route()
    kind, out, err = run("gate_reviewer.py", pr_payload())
    check("handoff: the review runs", kind, "SILENT")
    check("  exactly one notice", len(observer_posts()), 1)
    post = observer_posts()[0]
    check("  sent to the observer's own route", post["path"],
          "/p/tuck-profile/webhooks/widgets-observe")
    check("  signed with that route's secret", verify_sig(post, "widgets-observe"), True)
    block = notice(post)
    check("  event", block["event"], "handoff")
    check("  head", block["head"], HEAD_A)
    check("  the direct PR link", block["url"], f"https://github.com/{REPO}/pull/7")
    check("  seat, event, head, actor and next turn in one line",
          block["message"].splitlines()[0],
          f"🔧 [widgets] #7 `{HEAD_A[:7]}` fix pushed · review requested (dev-fixer) "
          f"· round 1/3 · next: reviewer queued")
    check("  the link is on its own line", block["message"].splitlines()[1], block["url"])
    secret = json.loads(SUBS.read_text())["widgets-observe"]["secret"]
    check("  no token, no secret, no review body in the ping",
          [leak for leak in ("token-reviewer", "token-fixer", "looks off", secret)
           if leak in post["body"]], [])

    # A delivery that lands twice for the same fact is the thing the key exists for: the gate
    # itself is silenced by the seat lock here, so the lock and the mark are cleared to let the
    # *same* transition arrive again — which is exactly what a redelivered webhook is.
    state_file("locks.json").write_text("{}")
    state_file("inflight.json").write_text("{}")
    kind, out, err = run("gate_reviewer.py", pr_payload())
    check("redelivered handoff: the gate fires again", kind, "SILENT")
    check("  and the feed says nothing new", len(observer_posts()), 1)
    check("  it says why", "already recorded" in err, True)

    set_prs({"7": pr(7), "9": pr(9)})
    state_file("locks.json").write_text("{}")
    state_file("inflight.json").write_text("{}")
    run("gate_reviewer.py", pr_payload(9))
    check("a different PR at the same head is its own notice", len(observer_posts()), 2)

    # -- a review verdict is one notice, and a redelivered verdict is none ---------
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    observer_route()
    kind, out, err = run("gate_fixer.py", review_payload(rid=5))
    check("verdict: the fix run starts", kind, "SILENT")
    check("  exactly one notice", len(observer_posts()), 1)
    block = notice(observer_posts()[0])
    check("  event", block["event"], "verdict")
    check("  outcome, actor, round and next turn",
          block["message"].splitlines()[0],
          f"🔍 [widgets] #7 `{HEAD_A[:7]}` review posted — changes requested (rev-coach) "
          f"· round 1/3 · next: fixer queued")
    state_file("locks.json").write_text("{}")
    state_file("inflight.json").write_text("{}")
    run("gate_fixer.py", review_payload(rid=5))
    check("  a redelivered verdict is not a second notice", len(observer_posts()), 1)

    # -- an approval says so, and says honestly whether it still applies -----------
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, state="approved", rid=9)]}})
    observer_route()
    kind, _, _ = run("gate_fixer.py", review_payload(state="approved", rid=9))
    check("approval: no fix run", kind, "SILENT")
    block = notice(observer_posts()[0])
    check("  event", block["event"], "approved")
    check("  who approved and what is next",
          block["message"].splitlines()[0],
          f"✅ [widgets] #7 `{HEAD_A[:7]}` approved (rev-coach) · next: you merge")
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, state="approved", rid=9)]}})
    observer_route()
    run("gate_fixer.py", review_payload(state="approved", rid=9, head=HEAD_B, commit=HEAD_A))
    check("  an approval of a head the PR moved past says so",
          "on an older head" in notice(observer_posts()[0])["message"], True)

    # -- escalation reports a durable pending marker before adjudicator delivery -----
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=8), review(REVIEWER, rid=9),
                                          review(REVIEWER, rid=10)]}})
    observer_route()
    kind, out, err = run("gate_fixer.py", review_payload(rid=10))
    check("cap spent: no fix run", kind, "SILENT")
    check("  observer sees the pending marker but no unsafe adjudicator dispatch",
           [r["path"] for r in RECEIVED],
           ["/p/tuck-profile/webhooks/widgets-observe"])
    block = notice(observer_posts()[0])
    check("  event", block["event"], "escalation")
    check("  the spent budget, and who owns it now",
          block["message"].splitlines()[0],
          f"⚠️ [widgets] #7 `{HEAD_A[:7]}` loop stopped — cap spent — 3/3 verdicts, "
          f"no approval · next: adjudicator delivery pending")
    run("gate_fixer.py", review_payload(rid=10))
    check("  a redelivered cap event pings nobody twice",
          (len(observer_posts()), len([r for r in RECEIVED if r["path"].endswith("widgets-breach")])),
          (1, 0))

    # -- a stall the watchdog decided to report is a notice ------------------------
    reset(prs={"7": pr(7, head=HEAD_A)})
    observer_route()
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    DATA["world"]["commit_dates"] = {HEAD_A: "2020-01-01T00:00:00Z"}
    save_world()
    out, _, _ = run("watchdog.py", None, "--loop", "widgets")
    check("stall: the watchdog reports it", "reviewer never posted a verdict" in out, True)
    block = notice(observer_posts()[-1])
    check("  the feed carries it", block["event"], "stall")
    check("  with the kind and the link",
          ("reviewer never posted a verdict" in block["message"]
           and block["url"].endswith("/pull/7")), True)

    # -- terminal close / merge ----------------------------------------------------
    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z")})
    observer_route()
    kind, _, _ = run("gate_reviewer.py",
                     pr_payload(action="closed", merged="2026-02-02T00:00:00Z"))
    check("closed: no review run", kind, "SILENT")
    block = notice(observer_posts()[0])
    check("  event", block["event"], "closed")
    check("  merged, with cleanup attempted but disk not asserted reclaimed",
          block["message"].splitlines()[0],
          f"🧹 [widgets] #7 `{HEAD_A[:7]}` PR closed — merged · next: nothing — cleanup attempted")

    # -- the feed is opt-in, and being off never touches the loop ------------------
    reset(prs={"7": pr(7)})
    observer_route(mute=True)
    kind, _, err = run("gate_reviewer.py", pr_payload())
    check("muted: the review still runs", kind, "SILENT")
    check("  nothing is sent", observer_posts(), [])
    check("  and nothing is queued up for later", state_file("observations.json").exists(), False)

    reset(prs={"7": pr(7)})
    observer_route(events=["escalation"])
    kind, _, err = run("gate_reviewer.py", pr_payload())
    check("filtered out: the review still runs", kind, "SILENT")
    check("  nothing is sent for it", observer_posts(), [])
    check("  it says why", "not in the feed" in err, True)
    check("  and it is not recorded as owed",
          load_state("observations.json").get("entries", {}), {})

    reset(prs={"7": pr(7)})
    observer_route(route="widgets-observe-missing", register=False)   # configured, never installed
    kind, _, err = run("gate_reviewer.py", pr_payload())
    check("misconfigured: the review still runs", kind, "SILENT")
    check("  the turn is held for isolation",
          held("reviewer", 7, HEAD_A), True)
    entries = load_state("observations.json").get("entries", {})
    check("  the refused delivery is recorded", [e["status"] for e in entries.values()], ["failed"])
    check("  with the reason",
          "missing from the gateway's subscriptions" in list(entries.values())[0]["error"], True)

    reset(prs={"7": pr(7)})
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["observer"] = {"profile": "tuck"}                     # a feed with nowhere to go
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    from review_loop import config as config_mod
    check("a route-less observer never refuses the loop",
          config_mod.load_id("widgets")["observer"]["misconfigured"],
          "observer.route is required to deliver anything")
    check("  and the review still runs", run("gate_reviewer.py", pr_payload(7))[0], "SILENT")

    # -- failed observer transport cannot cause an unsafe agent wake ---------------
    reset(prs={"7": pr(7), "9": pr(9), "11": pr(11)})
    set_concurrency(2)
    head = real_head()
    set_prs({n: pr(n, head=head) for n in (7, 9, 11)})
    observer_route(route="widgets-observe-fail")             # the sink answers 5xx
    for number in (7, 9, 11):
        check(f"review #{number} is held from gateway dispatch",
              run("gate_reviewer.py", pr_payload(number, head=head, action="opened"))[0],
              "SILENT")
        check(f"  reviewer #{number} is queued for isolation", held("reviewer", number, head), True)
    before = len(RECEIVED)
    kind, _, err = run("gate_fixer.py", review_payload(7, head=head, state="approved", rid=9))
    check("an unverified approval wakes no fix run", kind, "SILENT")
    woken = [r for r in RECEIVED[before:] if r["path"].endswith("/webhooks/widgets-review")]
    refused = [r for r in RECEIVED[before:] if "observe-fail" in r["path"]]
    check("  observer failure does not dispatch a gateway review", woken, [])
    check("  while the notice was refused (5xx)", len(refused), 1)
    check("  the failure is recorded, not hidden",
          sorted({e["status"] for e in load_state("observations.json")["entries"].values()}),
          ["uncertain"])
    check("  no legacy lock acquired", load_state("locks.json").get("reviewer", {}), {})
    check("  all turns remain held for isolation",
          sorted(load_state("pending.json").get("reviewer", {})),
          sorted(f"{REPO}#{n}" for n in (7, 9, 11)))

    # -- what is owed is retried by the sweep, and lands once the route is fixed ---
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    observer_route(secret=False)                              # no secret: cannot be signed
    kind, _, err = run("gate_fixer.py", review_payload(rid=5))
    check("an unsigned destination does not stop the fix", kind, "SILENT")
    entries = load_state("observations.json")["entries"]
    check("  the notice is owed, with its reason",
          [(e["status"], e["attempts"], "no secret" in e["error"]) for e in entries.values()],
          [("failed", 1, True)])
    subs = json.loads(SUBS.read_text())
    subs["widgets-observe"]["secret"] = hashlib.sha256(b"widgets-observe").hexdigest()
    SUBS.write_text(json.dumps(subs))
    set_prs({})                     # nothing open: this sweep is only about what is owed
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    before = len(observer_posts())
    run("watchdog.py", None, "--loop", "widgets")
    check("the sweep re-sends what the ledger owed", len(observer_posts()) - before, 1)
    check("  the same notice, now signed", verify_sig(observer_posts()[-1], "widgets-observe"), True)
    check("  and it is settled as delivered",
          [e["status"] for e in load_state("observations.json")["entries"].values()], ["delivered"])

    # -- the digest: many transitions, one compact message -------------------------
    reset(prs={"7": pr(7)})
    observer_route(digest_min=15)
    check("the review runs", run("gate_reviewer.py", pr_payload(7, action="opened"))[0], "SILENT")
    set_prs({"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    check("the fix runs on the verdict", run("gate_fixer.py", review_payload(rid=5))[0], "SILENT")
    check("nothing is sent while the batch is open", observer_posts(), [])
    set_prs({})
    state_file("watchdog.json").write_text(json.dumps({"armed_since": time.time() - 86400}))
    run("watchdog.py", None, "--loop", "widgets")
    posts = observer_posts()
    check("the sweep flushes one digest", len(posts), 1)
    digest = notice(posts[0])
    check("  carrying both transitions", digest["count"], 2)
    check("  with a direct link", digest["message"].count(f"https://github.com/{REPO}/pull/7"), 2)
    check("  and each transition on its own line",
          [line.split()[1] for line in digest["message"].splitlines()[1:]], ["#7", "#7"])
    ledger = list(load_state("observations.json")["entries"].values())
    check("  the two transitions are settled by it",
          sorted(len(e.get("batch") or []) for e in ledger), [0, 0, 2])
    check("  and the digest claim itself is accounted for, every entry delivered",
          sorted(e["status"] for e in ledger), ["delivered", "delivered", "delivered"])

    # -- the adapter is what the route runs, and it publishes only the notice ------
    text = digest["message"]
    kind, out, _ = run("observe.py", {"repository": {"full_name": REPO},
                                      "_observer": {"message": text, "event": "digest",
                                                    "leak": "should not survive"}})
    check("the route adapter passes the notice through", json.loads(out)["_observer"]["message"],
          text)
    check("  and drops what the notice never carried",
          "leak" in json.loads(out)["_observer"], False)
    check("  a payload that is not a notice is ignored",
          run("observe.py", {"pull_request": {"number": 7}})[0], "SILENT")
    check("  a notice with no message is ignored",
          run("observe.py", {"_observer": {"event": "verdict"}})[0], "SILENT")
    check("  the route the loop registers is deliver-only",
          json.loads(SUBS.read_text())["widgets-observe"]["deliver_only"], True)


def group_observer_safety() -> None:
    """Outbox failure, concurrent retry ownership, and route contract regressions."""
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch as mock_patch
    from review_loop import config, gate, observer, state as state_mod

    section("observer — durable claim and delivery-only destination")
    reset(prs={"7": pr(7)})
    observer_route()
    loop = config.load_id("widgets")
    st = state_mod.state_for(loop)
    with mock_patch.object(observer, "_save", side_effect=OSError("disk full")):
        delivered = observer.notify(loop, st, "opened", 7, HEAD_A)
    check("failed claim never reports delivery", delivered, False)
    check("failed claim never POSTs private link", observer_posts(), [])
    check("failed claim does not block subsequent seat", run("gate_reviewer.py", pr_payload())[0], "SILENT")

    check("full head differentiates same-prefix commits",
          observer.key_for(loop, 7, HEAD_A[:12] + "0" * 28, "opened") !=
          observer.key_for(loop, 7, HEAD_A[:12] + "1" * 28, "opened"), True)
    for key, changed in (("profile", "foreign"), ("deliver_only", False),
                         ("prompt", "{_observer.message} private {_loop.url}"),
                         ("script", "gate_reviewer.py"), ("deliver", "discord"),
                         ("deliver_extra", {"chat_id": "foreign-private-chat"})):
        reset(prs={"7": pr(7)})
        observer_route()
        subs = json.loads(SUBS.read_text())
        subs["widgets-observe"][key] = changed
        SUBS.write_text(json.dumps(subs))
        kind, _, _ = run("gate_reviewer.py", pr_payload())
        check(f"{key} mismatch does not block seat", kind, "SILENT")
        check(f"{key} mismatch never leaks link / wakes model", observer_posts(), [])
        check(f"{key} mismatch is owed, not silently dropped",
              list(load_state("observations.json")["entries"].values())[0]["status"], "failed")

    reset(prs={"7": pr(7)})
    observer_route(secret=False)
    loop = config.load_id("widgets")
    st = state_mod.state_for(loop)
    observer.notify(loop, st, "opened", 7, HEAD_A)
    subs = json.loads(SUBS.read_text())
    subs["widgets-observe"]["secret"] = hashlib.sha256(b"widgets-observe").hexdigest()
    SUBS.write_text(json.dumps(subs))
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = list(pool.map(lambda _: observer.retry(loop, st), range(2)))
    check("concurrent retry sweeps claim exactly once", sorted(sent), [0, 1])
    check("concurrent retry sweeps POST only once", len(observer_posts()), 1)
    check("concurrent retry settles entry", list(load_state("observations.json")["entries"].values())[0]["status"], "delivered")

    reset(prs={"7": pr(7)})
    observer_route()
    loop = config.load_id("widgets")
    st = state_mod.state_for(loop)
    with mock_patch.object(gate.gh, "pr", return_value=pr(7)), \
         mock_patch.object(gate, "wake_adjudicator", return_value=False):
        gate.breach(loop, st, 7, HEAD_A, 3, "cap")
    check("unverified adjudicator delivery leaves pending marker",
          st.breach_get(7)["status"], "delivery-pending")
    check("pending marker informs observer first", len(observer_posts()), 1)
    check("pending notice never claims adjudicator received it",
          "delivery pending" in notice(observer_posts()[0])["message"], True)
    with mock_patch.object(gate.gh, "pr", return_value=pr(7)), \
         mock_patch.object(gate, "wake_adjudicator", return_value=True):
        gate.breach(loop, st, 7, HEAD_A, 3, "cap")
    check("verified retry promotes marker", st.breach_get(7)["status"], "awaiting-adjudication")
    check("verified retry emits one escalation", len(observer_posts()), 1)

    reset(prs={"7": pr(7)})
    observer_route()
    loop = config.load_id("widgets")
    st = state_mod.state_for(loop)
    accepted = []
    def accepted_without_response(*args, **kwargs):
        accepted.append(args)
        return False                       # gateway accepted, but sender lost its response
    with mock_patch.object(observer.routes, "fire", side_effect=accepted_without_response):
        check("unverified accepted POST has no receipt", observer.notify(loop, st, "opened", 7, HEAD_A), False)
        check("ambiguous delivery is durable", list(load_state("observations.json")["entries"].values())[0]["status"], "uncertain")
        check("sweep cannot send a second accepted POST", observer.retry(loop, st), 0)
    check("one accepted POST even after retry sweep", len(accepted), 1)
    check("unknown delivery blocks changing destination", observer.unsettled(st), 1)
    reset(prs={"7": pr(7)})
    observer_route()
    loop = config.load_id("widgets")
    st = state_mod.state_for(loop)
    with mock_patch.object(observer.routes, "fire", side_effect=accepted_without_response), \
         mock_patch.object(observer, "_receipt", side_effect=OSError("lost response")):
        observer.notify(loop, st, "opened", 7, HEAD_A)
    entries = load_state("observations.json")["entries"]
    entry = next(iter(entries.values()))
    check("crashed receipt leaves pending claim", entry["status"], "pending")
    entry["at"] = time.time() - observer.STALE_CLAIM_S - 1
    st.observations.write_text(json.dumps({"entries": entries}))
    with mock_patch.object(observer.routes, "fire", side_effect=accepted_without_response):
        observer.retry(loop, st)
    check("stale claim quarantined instead of second accepted POST",
          next(iter(load_state("observations.json")["entries"].values()))["status"], "uncertain")
    check("two separate accepted POSTs, no replay of either", len(accepted), 2)


def group_observer_cli() -> None:
    """Configuring, muting and inspecting the feed — the operator's side of it."""
    import contextlib
    import io

    from review_loop import cli, config, observer

    section("observer — configuring the feed, and being told when it is broken")

    def parser_for(settings=None):
        fake = FakeCtx()
        cli.register_cli(fake, settings=settings)
        parser = argparse.ArgumentParser(prog="hermes review-loop")
        fake.setup(parser)
        return parser

    def call_set(**kw) -> tuple:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_set(ns(loop="feed", **kw))
        return rc, buf.getvalue()

    reset(prs={})
    write_profiles("rv", "fx")
    parser = parser_for()
    args = parser.parse_args(["init", "--repo", "acme/feed", "--fixer", FIXER,
                              "--reviewer", REVIEWER, "--reviewer-profile", "rv",
                              "--fixer-profile", "fx", "--host", HOST,
                              "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                              "--token", f"{FIXER}={SEAT_PATS[1]}",
                              "--observer-profile", "tuck"])
    init_output = io.StringIO()
    with contextlib.redirect_stdout(init_output):
        rc = args.func(args)
    if rc:
        print(init_output.getvalue())
    check("init with an observer profile succeeds", rc, 0)
    loop = config.load_id("feed")
    check("  the feed is on, named after the loop", loop["observer"]["route"], "feed-observe")
    check("  for the profile that was named", loop["observer"]["profile"], "tuck")
    check("  delivered to telegram by default", loop["observer"]["deliver"], "telegram")
    subs = json.loads(SUBS.read_text())
    check("  its route exists, deliver-only (no agent)", subs["feed-observe"]["deliver_only"], True)
    check("  with the notice prompt", subs["feed-observe"]["prompt"], "{_observer.message}")
    check("  and the adapter script", subs["feed-observe"]["script"], "observe.py")
    check("  bound to the observer's own profile", subs["feed-observe"]["profile"], "tuck")
    check("  the seats' routes are untouched",
          [subs[name].get("deliver_only") for name in ("feed-review", "feed-fix")], [None, None])

    check("  registered at the operator's own gateway", subs["feed-observe"]["host"], HOST)
    args.repo = "acme/conflict"
    args.observer_route = "widgets-review"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = args.func(args)
    check("observer cannot take another loop's reviewer route", rc, 2)
    check("  collision writes no loop config", (LOOPS_DIR / "conflict.json").exists(), False)
    check("  existing reviewer route survives", json.loads(SUBS.read_text())["widgets-review"],
          subs["widgets-review"])
    args.repo = "acme/feed"
    args.observer_route = ""
    args.host = "https://attacker.example"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = args.func(args)
    check("init cannot overwrite an existing loop's private destination", rc, 2)
    check("  original host remains", config.load_id("feed")["host"], HOST)
    args.host = HOST

    rc, out = call_set(observer_profile="another")
    check("profile change reconciles existing route", rc, 0)
    check("  updated profile is actually routed",
          json.loads(SUBS.read_text())["feed-observe"]["profile"], "another")
    rc, out = call_set(observer_route="feed-new-observe")
    check("route rename reconciles destination", rc, 0)
    check("  new delivery-only route installed",
          json.loads(SUBS.read_text())["feed-new-observe"]["deliver_only"], True)
    check("  old observer route removed", "feed-observe" in json.loads(SUBS.read_text()), False)
    rc, out = call_set(observer_route="feed-observe")
    check("route can be reconciled back", rc, 0)
    from review_loop import state as state_mod
    st = state_mod.state_for(config.load_id("feed"))
    st.observations.parent.mkdir(parents=True, exist_ok=True)
    st.observations.write_text(json.dumps({"entries": {"old": {"status": "failed"}}}))
    for status in ("queued", "digesting", "pending", "failed", "uncertain"):
        st.observations.write_text(json.dumps({"entries": {"old": {"status": status}}}))
        for change in ({"host": "https://attacker.example"},
                       {"observer_profile": "new-profile"},
                       {"observer_route": "foreign-observe"},
                       {"observer_deliver": "discord"}):
            rc, out = call_set(**change)
            check(f"{status} blocks {next(iter(change))} change", rc, 2)
            check(f"  {status} keeps authorized host and profile",
                  (config.load_id("feed")["host"], config.load_id("feed")["observer"]["profile"],
                   json.loads(SUBS.read_text())["feed-observe"]["profile"]),
                  (HOST, "another", "another"))
    # Plugin-level apply is another host write path; it must not bypass set's boundary.
    cli._SETTINGS = {"host": "https://attacker.example"}
    rc = cli.cmd_apply(ns(loop="feed", dry_run=False))
    check("apply cannot reroute an unsettled observer host", rc, 2)
    check("apply leaves original host intact", config.load_id("feed")["host"], HOST)
    cli._SETTINGS = {}
    # Disabling is a stop switch, not a new destination: do not force users to
    # send (or adjudicate) an uncertain private notice just to turn off the feed.
    st.observations.write_text(json.dumps({"entries": {"old": {"status": "queued",
        "message": "private PR", "event": "opened", "number": 7, "head": HEAD_A,
        "queued_at": time.time() - 3600, "at": time.time() - 3600}}}))
    rc, out = call_set(observer_disable=True)
    check("queued notice allows disable", rc, 0)
    check("disable removes route but retains queued receipt",
          ("feed-observe" in json.loads(SUBS.read_text()), observer.unsettled(st)), (False, 1))
    disabled = config.load_id("feed")
    check("disable retains original destination binding", disabled["observer_disabled"],
          {"route": "feed-observe", "profile": "another", "deliver": "telegram", "host": HOST})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_status(ns(loop="feed"))
    check("disabled status shows queued notice still owed",
          "disabled — existing notices remain owed" in buf.getvalue() and "1 owed" in buf.getvalue(), True)
    check("disabled feed cannot flush queued private notice", observer.flush(disabled, st), False)
    rc, out = call_set(observer_profile="foreign")
    check("new profile cannot inherit disabled queue", rc, 2)
    rc, out = call_set(host="https://attacker.example", observer_profile="another")
    check("new host cannot inherit disabled queue", rc, 2)
    rc, out = call_set(observer_profile="another")
    check("original destination may resume queued notices", rc, 0)
    check("resumed feed retains queue", observer.unsettled(st), 1)
    st.observations.write_text(json.dumps({"entries": {"old": {"status": "delivered"}}}))

    rc, out = call_set(observer_mute=True)
    check("mute → accepted", rc, 0)
    check("  written", config.load_id("feed")["observer"]["mute"], True)
    check("  and shown as muted", "[muted]" in out, True)
    rc, out = call_set(observer_events="verdict, closed")
    check("narrowing the feed → accepted", rc, 0)
    check("  written as a list", config.load_id("feed")["observer"]["events"],
          ["closed", "verdict"])
    rc, out = call_set(observer_digest_min=30)
    check("digest → accepted", rc, 0)
    check("  written", config.load_id("feed")["observer"]["digest_min"], 30)
    rc, out = call_set(observer_unmute=True)
    check("unmute → accepted", rc, 0)
    check("  written", config.load_id("feed")["observer"]["mute"], False)
    rc, out = call_set(observer_deliver="log")
    check("a log destination is refused", rc, 2)
    check("  with the reason", "never wakes an agent" in out, True)
    check("  and nothing was written", config.load_id("feed")["observer"]["deliver"], "telegram")
    rc, out = call_set(observer_disable=True)
    check("disable → accepted", rc, 0)
    check("  the feed is gone", config.load_id("feed")["observer"], {})
    rc, out = call_set(observer_profile="fresh-profile")
    check("profile alone creates a new observer route", rc, 0)
    check("  route and config agree",
          (config.load_id("feed")["observer"]["route"],
           json.loads(SUBS.read_text())["feed-observe"]["profile"]),
          ("feed-observe", "fresh-profile"))
    rc, out = call_set(observer_disable=True)
    check("new observer can be disabled", rc, 0)
    rc, out = call_set(observer_mute=True)
    check("muting a loop with no feed → refused", rc, 2)
    check("  with what to do instead", "no observer feed" in out, True)

    # a normalized loop reads back: the write must not need a hand edit to load
    check("a configured feed survives a write/read round trip",
          config.load_id("feed")["observer"], {})

    # status says where the feed goes, what it owes, and when it is broken
    reset(prs={"7": pr(7)})
    observer_route()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_status(ns(loop="widgets"))
    check("status names the destination and the profile",
          "observer:   widgets-observe → telegram (profile tuck-profile)" in buf.getvalue(), True)
    check("  and what has been delivered", "0 delivered · 0 owed" in buf.getvalue(), True)
    state_file("inflight.json").write_text("{}")
    state_file("locks.json").write_text("{}")
    run("gate_reviewer.py", pr_payload())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_status(ns(loop="widgets"))
    check("  and it counts a delivery", "1 delivered · 0 owed" in buf.getvalue(), True)
    subs = json.loads(SUBS.read_text())
    subs["widgets-observe"].pop("secret")
    SUBS.write_text(json.dumps(subs))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_status(ns(loop="widgets"))
    check("  and shouts when the route cannot deliver",
          "⚠ route 'widgets-observe' is missing from the gateway's subscriptions" in buf.getvalue(),
          True)
    check("  and the loader still returns the loop", config.load_id("widgets")["observer"]["route"],
          "widgets-observe")

    check("describe() reads a misconfigured feed as such",
          observer.describe({"route": "", "misconfigured": "observer.route is required"}),
          "misconfigured — observer.route is required")
    check("describe() reads a missing feed as not configured",
          observer.describe({}), "not configured")

    # The event vocabulary is one list written down in three places — the code, the CLI flags and
    # the docs. Drift is the quiet failure this whole plugin exists to kill, so it fails here.
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            parser_for().parse_args(["set", "--help"])
    except SystemExit:
        pass
    # argparse wraps help at 80 columns, so the vocabulary is compared with the whitespace folded
    # out — otherwise the check fails on a line break rather than on real drift.
    help_text = re.sub(r"\s+", "", buf.getvalue())
    check("the CLI names every event the feed can send",
          [name for name in observer.EVENTS if name not in help_text], [])
    for doc in ("docs/observer.md", "docs/configuration.md"):
        text = (ROOT / doc).read_text()
        check(f"{doc.split('/')[-1]} names every event",
              [name for name in observer.EVENTS if name not in text], [])

    # uninstall is the inverse of init, the observer route included
    reset(prs={})
    observer_route()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_uninstall(ns(loop="widgets", keep_config=True))
    check("uninstall removes the observer route",
          "route removed: widgets-observe" in buf.getvalue(), True)
    check("  and it is gone from the registry", "widgets-observe" in SUBS.read_text(), False)
    check("  while its config is kept with --keep-config",
          config.load_id("widgets")["observer"]["route"], "widgets-observe")


GROUPS = {
    "observer": group_observer,
    "observer_safety": group_observer_safety,
    "observer_cli": group_observer_cli,
}
