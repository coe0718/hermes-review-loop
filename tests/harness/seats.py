"""Seat holds, supervisor capacity, one-seat-per-PR, settings, the webhook host and seat identity."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


def group_settings() -> None:
    """`hermes review-loop set` — changing the knobs without hand-editing JSON."""
    import contextlib
    import io
    from types import SimpleNamespace

    from review_loop import cli, config

    section("settings — how many PRs a seat may work at once")

    def call(**kw) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_set(ns(**kw))
        return rc, buf.getvalue()

    def file_loop() -> dict:
        return json.loads((LOOPS_DIR / "widgets.json").read_text())

    reset(prs={})
    set_concurrency(1)
    check("starts serialized", file_loop().get("concurrency"), 1)

    rc, out = call(concurrency=2)
    check("set 2 → accepted", rc, 0)
    check("  written to the config", file_loop().get("concurrency"), 2)
    check("  and it says what changed", "concurrency: 1 → 2" in out, True)
    check("  and the effective capacities", "parallel now: reviewer 2 · fixer 2" in out, True)

    # per seat: Drey and Vex get their own numbers
    rc, out = call(fixer_concurrency=1)
    check("fixer-only setting → accepted", rc, 0)
    check("  fixer written", file_loop()["seats"]["fixer"]["concurrency"], 1)
    check("  reviewer keeps the loop default", file_loop()["seats"].get("reviewer", {}).get(
        "concurrency"), None)
    check("  and it says who it applies to", "(this seat only)" in out, True)
    check("  effective split reported", "parallel now: reviewer 2 · fixer 1" in out, True)

    rc, out = call(concurrency=3)
    check("changing the default again → accepted", rc, 0)
    check("  and it flags the seat that overrides it",
          "fixer has its own concurrency" in out, True)

    rc, out = call(cap=4)
    check("set cap → accepted", rc, 0)
    check("  cap written", file_loop().get("cap"), 4)
    check("  capacities untouched", config.seat_concurrency(config.load_id("widgets"), "fixer"), 1)

    rc, out = call(concurrency=0)
    check("set 0 → refused", rc, 2)
    check("  with the reason", "must be >= 1" in out, True)
    check("  nothing written", file_loop().get("concurrency"), 3)

    rc, out = call()
    check("set with nothing → says so", "nothing to change" in out, True)

    # the rail that matters: no clone, so no parallel
    solo = config.normalize({"id": "solo", "repo": "acme/solo", "fixers": [FIXER],
                             "reviewers": [REVIEWER], "reviewer_seat": SEAT,
                             "seats": {"reviewer": {"profile": "r", "route": "solo-review"},
                                       "fixer": {"profile": "f", "route": "solo-fix"}},
                             "state_dir": str(STATE_DIR / "solo")})
    (LOOPS_DIR / "solo.json").write_text(json.dumps(solo))
    rc, out = call(loop="solo", concurrency=3)
    check("parallel without a clone → refused", rc, 2)
    check("  and it says why", "requires 'clone'" in out, True)
    check("  the loop still says serialized",
          json.loads((LOOPS_DIR / "solo.json").read_text())["concurrency"], 1)

    # a seat-level number is caught even when the loop default stays serialized
    rc, out = call(loop="solo", reviewer_concurrency=2)
    check("seat-level parallel without a clone → refused", rc, 2)
    check("  and it names the seat", "seats.reviewer.concurrency > 1 requires 'clone'" in out, True)

    # the round trip that a stranger's install depends on: what we write must read back
    rt = config.normalize({"id": "rt", "repo": "acme/rt", "fixers": [FIXER],
                           "reviewers": [REVIEWER], "reviewer_seat": SEAT,
                           "seats": {"reviewer": {"profile": "r", "route": "rt-review"},
                                     "fixer": {"profile": "f", "route": "rt-fix"}}})
    (LOOPS_DIR / "rt.json").write_text(json.dumps(rt))
    check("a normalized loop reads back", config.load_id("rt")["concurrency"], 1)
    check("  its empty adjudicator survives the round trip",
          config.load_id("rt")["adjudicator"], {})

    rc, out = call(loop="nope", concurrency=2)
    check("unknown loop → refused", rc, 2)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_status(ns(loop="widgets"))
    check("status reports the setting", "parallel:   reviewer 3 · fixer 1" in buf.getvalue(), True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_list(ns())
    check("list reports it too", "reviewer=3 fixer=1" in buf.getvalue(), True)


def group_seats() -> None:
    section("seats — fail-closed route holds never become gateway runs")
    reset(prs={"7": pr(7), "9": pr(9, head=HEAD_B)})
    check_eligible("first reviewer turn", "gate_reviewer.py", pr_payload(7), "reviewer")
    check_eligible("second reviewer turn", "gate_reviewer.py",
                   pr_payload(9, head=HEAD_B), "reviewer", 9, HEAD_B)
    check("both held without leaking a gateway slot",
          sorted(load_state("pending.json").get("reviewer", {})),
          [f"{REPO}#7", f"{REPO}#9"])
    check("no legacy seat lock acquired", load_state("locks.json"), {})
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    check_eligible("fixer verdict", "gate_fixer.py", review_payload(rid=5), "fixer")
    check("no reviewer lock acquired by eligible fix", load_state("locks.json"), {})


def group_parallel() -> None:
    section("parallel — durable supervisor capacity and deduplication")
    from review_loop.run_supervisor import Supervisor

    reset(prs={"7": pr(7), "9": pr(9), "11": pr(11)})
    set_concurrency(2)
    # The route cannot obtain the private runtime and must hold the exact head,
    # regardless of the configured concurrency. Never provision a live worker here.
    for number in (7, 9, 11):
        check_eligible(f"parallel gate PR #{number}", "gate_reviewer.py",
                       pr_payload(number), "reviewer", number)
    check("no legacy clone or slot created", load_state("locks.json"), {})

    db = STATE_DIR / "fixture-runs.sqlite"
    supervisor = Supervisor(db, fixture_mode=True, fixture_command=[sys.executable, "-c", "pass"],
                            capacity={"reviewer": 2, "fixer": 1})
    # Claim synchronously to make the capacity race deterministic; no subprocess or
    # GitHub credentials are involved. The real route uses the same SQLite ledger.
    supervisor._spawn = lambda: None
    for number in (7, 9, 11):
        check(f"enqueue PR #{number} always silent",
              supervisor.enqueue(f"delivery-{number}", REPO, number, HEAD_A, "reviewer"), "[SILENT]")
    first, second = supervisor._claim(), supervisor._claim()
    check("two reviewer slots claimed", bool(first and second), True)
    check("third PR waits at capacity", supervisor._claim(), None)
    check("third remains pending", supervisor.get("delivery-11")["state"], "pending")
    check("same delivery is deduplicated", supervisor.enqueue("delivery-7", REPO, 7, HEAD_A, "reviewer"), "[SILENT]")
    check("same head under another delivery is deduplicated",
          supervisor.enqueue("redelivery-7", REPO, 7, HEAD_A, "reviewer"), "[SILENT]")
    with sqlite3.connect(db) as con:
        check("one ledger row for duplicate head",
              con.execute("SELECT COUNT(*) FROM runs WHERE pr=7 AND seat='reviewer'").fetchone()[0], 1)
        # A new head and the opposite seat cannot occupy this same PR while it is claimed.
        supervisor.enqueue("new-head-7", REPO, 7, HEAD_B, "reviewer")
        supervisor.enqueue("fix-7", REPO, 7, HEAD_A, "fixer")
    check("same PR new head waits", supervisor.get("new-head-7")["state"], "pending")
    check("other seat same PR waits", supervisor.get("fix-7")["state"], "pending")
    check("no third claim while occupied", supervisor._claim(), None)
    # A finished turn releases precisely one slot. Another pending PR can then claim it.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE runs SET state='succeeded' WHERE id=?", (first[0],))
    third = supervisor._claim()
    check("completed reviewer frees one slot", third is not None, True)
    check("waiting PR #11 claimed", supervisor.get("delivery-11")["state"], "claimed")
    check("still only two active reviewer slots", sum(supervisor.get(f"delivery-{n}")["state"] == "claimed"
                                                      for n in (7, 9, 11)), 2)

    section("parallel — per-seat capacity independent")
    reset(prs={"7": pr(7), "9": pr(9)})
    set_concurrency(1)
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["seats"]["reviewer"]["concurrency"] = 2
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    from review_loop import config
    loop = config.load_id("widgets")
    check("reviewer capacity configured separately", config.seat_concurrency(loop, "reviewer"), 2)
    check("fixer capacity remains one", config.seat_concurrency(loop, "fixer"), 1)
    db = STATE_DIR / "split-runs.sqlite"
    supervisor = Supervisor(db, fixture_mode=True, fixture_command=[sys.executable, "-c", "pass"],
                            capacity={s: config.seat_concurrency(loop, s) for s in ("reviewer", "fixer")})
    supervisor._spawn = lambda: None
    for n in (7, 9, 11):
        supervisor.enqueue(f"fix-{n}", REPO, n, HEAD_A, "fixer")
    check("first fixer claims", supervisor._claim() is not None, True)
    check("second fixer waits at its own capacity", supervisor._claim(), None)
    check("other fix stays pending", supervisor.get("fix-9")["state"], "pending")


def group_exclusive() -> None:
    section("one seat per PR — handoff remains fail-closed")
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    check_eligible("fixer receives verdict", "gate_fixer.py", review_payload(rid=5), "fixer")
    check("fixer has no gateway lock", load_state("locks.json"), {})
    set_prs({"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, rid=5)]}})
    check_eligible("new review request", "gate_reviewer.py",
                   pr_payload(7, head=HEAD_B), "reviewer", head=HEAD_B)
    check("new head held without dispatch",
          held("reviewer", 7, HEAD_B), True)
    check("old fixer head no longer queued after handoff",
          load_state("pending.json").get("fixer", {}), {})
    check("no legacy slot acquired", load_state("locks.json"), {})


def manifest_schema() -> dict:
    """The ``config_schema`` block out of plugin.yaml, without a YAML dependency.

    The package is stdlib-only on purpose (it ships to other people's machines), so the suite reads
    the manifest by hand instead of adding PyYAML for one assertion. A shape this cannot follow
    raises rather than returning an empty dict — a drift test that silently passes is worse than no
    drift test.
    """
    text = (ROOT / "plugin.yaml").read_text()
    parts = text.split("\nconfig_schema:", 1)
    if len(parts) != 2:
        raise AssertionError("plugin.yaml has no config_schema: block")
    entries: dict = {}
    current = None
    for line in parts[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^  \S", line):
            current = line.strip().rstrip(":")
            entries[current] = {}
        elif re.match(r"^    \S", line) and current:
            key, _, value = line.strip().partition(":")
            entries[current][key.strip()] = value.strip().strip('"')
    return entries


def group_plugin_settings() -> None:
    section("plugin settings — the desktop form and the loop must agree")
    from review_loop import cli, config

    reset(prs={"7": pr(7)})      # a known starting loop, whatever the earlier groups left behind
    manifest = manifest_schema()
    check("plugin.yaml declares a config_schema", bool(manifest), True)
    check("  the same keys the code reads", sorted(manifest), sorted(config.SETTINGS_SCHEMA))
    for key, spec in config.SETTINGS_SCHEMA.items():
        declared = manifest.get(key) or {}
        check(f"  {key}: type agrees", declared.get("type"), spec["type"])
        check(f"  {key}: default agrees", declared.get("default"), str(spec["default"]))
        check(f"  {key}: label agrees (the form shows the label, not the key)",
              declared.get("label"), spec["label"])
        check(f"  {key}: has a description", bool(declared.get("description")), True)

    check("unset → schema defaults", config.settings_defaults(None)["cap"],
          config.SETTINGS_SCHEMA["cap"]["default"])
    tuned = config.settings_defaults({"cap": 6, "reviewer_concurrency": "4"})
    check("a set value wins", (tuned["cap"], tuned["reviewer_concurrency"]), (6, 4))
    check("  and is coerced to the declared type", isinstance(tuned["reviewer_concurrency"], int), True)
    check("a junk value falls back", config.settings_defaults({"cap": "many"})["cap"], 3)

    settings = {"cap": 5, "reviewer_concurrency": 2, "fixer_concurrency": 1,
                "clone": str(CLONE), "grace_min": 30}
    raw = config.apply_settings(config.load_id("widgets"), settings)
    check("apply writes both seats",
          (raw["seats"]["reviewer"]["concurrency"], raw["seats"]["fixer"]["concurrency"]), (2, 1))
    check("  and the plain knobs", (raw["cap"], raw["grace_min"]), (5, 30))

    kept = config.apply_settings({"clone": "/some/where", "seats": {}}, {"clone": ""})
    check("a blank clone in the form never wipes the loop's own",
          kept["clone"], "/some/where")

    fake = FakeCtx()
    cli.register_cli(fake, settings=settings)
    check("the CLI registers itself under one name", fake.registered, "review-loop")

    parser = argparse.ArgumentParser(prog="hermes review-loop")
    fake.setup(parser)          # the framework hands setup the COMMAND's parser, not a subparsers action
    args = parser.parse_args(["init", "--repo", "acme/solo", "--fixer", "f", "--reviewer", "r",
                              "--reviewer-profile", "p", "--fixer-profile", "q"])
    check("a new loop starts from the settings",
          (args.cap, args.reviewer_concurrency, args.fixer_concurrency), (5, 2, 1))
    check("  clone and grace too", (args.clone, args.grace_min), (str(CLONE), 30))
    check("  seats differing → no misleading loop-level default", args.concurrency, 1)

    # The bug this test used to *encode*: setup was handed a subparsers action here and the
    # command's own parser in the framework, so the real CLI silently offered zero subcommands.
    for argv in (["list"], ["settings"], ["status", "--loop", "widgets"],
                 ["apply", "--loop", "widgets", "--dry-run"],
                 ["doctor", "--loop", "widgets"],
                 ["set", "--loop", "widgets", "--cap", "4"],
                 ["explain", "--loop", "widgets", "--pr", "7"],
                 ["arm", "--loop", "widgets"], ["cleanup", "--loop", "widgets"],
                 ["uninstall", "--loop", "widgets"]):
        parsed = parser.parse_args(argv)
        check(f"  `{' '.join(argv)}` parses", callable(parsed.func), True)
        check(f"    …as the {argv[0]} command", parsed.command, argv[0])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = parser.parse_args([]).func(ns())
    check("bare invocation prints usage instead of erroring", rc, 0)
    check("  and it lists the commands", "apply" in buf.getvalue(), True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_apply(ns(loop="widgets", dry_run=True))
    check("apply --dry-run exits 0", rc, 0)
    check("  and shows the diff", "reviewer concurrency: 1 → 2" in buf.getvalue(), True)
    check("  nothing written on a dry run",
          config.seat_concurrency(config.load_id("widgets"), "reviewer"), 1)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_apply(ns(loop="widgets", dry_run=False))
    check("apply writes it", config.seat_concurrency(config.load_id("widgets"), "reviewer"), 2)
    check("  and reports the file", "loop config updated" in buf.getvalue(), True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_apply(ns(loop="widgets", dry_run=False))
    check("a second apply is a no-op", "already matches" in buf.getvalue(), True)

    # the rails still hold: two reviews at once with nowhere to isolate them is refused
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["clone"] = ""
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_apply(ns(loop="widgets", dry_run=False))
    check("parallel settings without a clone → refused", rc, 2)
    check("  and it says why", "requires 'clone'" in buf.getvalue(), True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_settings(ns())
    check("settings lists every knob", "reviewer_concurrency" in buf.getvalue(), True)
    check("  and where it came from", "[set]" in buf.getvalue(), True)


def make_loop(loop_id: str, repo: str, reviewer_profile: str, fixer_profile: str,
              adjudicator: str = "") -> dict:
    """Write one loop the way ``init`` would — config and routes — without a CLI or a network.

    The seat-identity tests need several loops side by side (that is the whole point: one form,
    many repositories), and driving each one through ``init`` would only test ``init`` again.
    """
    from review_loop import cli as cli_mod, config

    raw = {
        "id": loop_id, "repo": repo, "base": "main", "cap": 3,
        "fixers": [FIXER], "reviewers": [REVIEWER], "reviewer_seat": REVIEWER,
        "seats": {"reviewer": {"profile": reviewer_profile, "route": f"{loop_id}-review",
                               "login": REVIEWER},
                  "fixer": {"profile": fixer_profile, "route": f"{loop_id}-fix", "login": FIXER}},
        "state_dir": str(STATE_DIR / loop_id),
        "tokens": {REVIEWER: str(SEAT_PATS[0]), FIXER: str(SEAT_PATS[1])},
        "read_token": REVIEWER, "host": HOST,
    }
    if adjudicator:
        raw["adjudicator"] = {"route": f"{loop_id}-breach", "profile": adjudicator}
    LOOPS_DIR.mkdir(parents=True, exist_ok=True)
    loop = config.normalize(raw)
    (LOOPS_DIR / f"{loop_id}.json").write_text(json.dumps(loop, indent=2))
    cli_mod._install_routes(loop)
    return loop


def group_seat_identity() -> None:
    """Who serves each seat: the form names a profile and a login per role, and the loop, its
    routes and the guard rails all have to agree — per loop, never globally."""
    from review_loop import cli, config, gh

    section("seat identity — the settings form says who serves each seat")

    def subs() -> dict:
        return json.loads(SUBS.read_text())

    def loop_bytes(loop_id: str) -> str:
        return (LOOPS_DIR / f"{loop_id}.json").read_text()

    reset(prs={})
    original_api = gh.api
    gh.api = lambda loop, path, **kw: [] if path.endswith("/hooks?per_page=100") else original_api(loop, path, **kw)
    form = {"reviewer_profile": "vex", "fixer_profile": "drey", "adjudicator_profile": "tuck",
            "reviewer_login": REVIEWER, "fixer_login": FIXER}
    parser = parser_for(form)
    init_args = ["init", "--repo", "acme/seats", "--fixer", FIXER, "--reviewer", REVIEWER,
                 "--host", HOST, "--read-token", REVIEWER,
                 "--token", f"{REVIEWER}={SEAT_PATS[0]}", "--token", f"{FIXER}={SEAT_PATS[1]}",
                 "--adjudicator-route", "seats-breach"]

    rc, out = run_cli(parser.parse_args([*init_args, "--dry-run"]))
    check("init --dry-run previews the loop", rc, 0)
    check("  reviewer: the profile the form names", "profile vex" in out, True)
    check("  fixer: the profile the form names", "profile drey" in out, True)
    check("  adjudicator: the profile the form names", "profile tuck" in out, True)
    check("  and it says nothing was written", "nothing written" in out, True)
    check("  no loop config was written", (LOOPS_DIR / "seats.json").exists(), False)
    check("  no route was written", "seats-review" in SUBS.read_text(), False)

    untouched = {name: subs()[name] for name in ("widgets-review", "widgets-fix", "widgets-breach")}
    rc, out = run_cli(parser.parse_args(init_args))
    check("install succeeds", rc, 0)
    installed = subs()
    check("  the reviewer route runs under vex", installed["seats-review"]["profile"], "vex")
    check("  the fixer route runs under drey", installed["seats-fix"]["profile"], "drey")
    check("  the adjudicator route runs under tuck", installed["seats-breach"]["profile"], "tuck")
    check("  and the other loops' routes are untouched",
          [{"description": subs()[n]["description"], "profile": subs()[n]["profile"],
            "secret": subs()[n]["secret"]} for n in untouched],
          [{"description": e["description"], "profile": e["profile"], "secret": e["secret"]}
           for e in untouched.values()])

    seats_loop = config.load_id("seats")
    check("the loop records the same mapping",
          tuple(config.seat_profile(seats_loop, role) for role in config.ROUTE_ROLES),
          ("vex", "drey", "tuck"))
    check("  with the logins the form named",
          (config.seat_login(seats_loop, "reviewer"), config.seat_login(seats_loop, "fixer")),
          (REVIEWER, FIXER))
    check("  and the reviewer login the route serves",
          seats_loop["reviewer_seat"], REVIEWER)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_status(ns(loop="seats"))
    status = buf.getvalue()
    check("status shows who serves each seat", f"reviewer={REVIEWER} (vex)" in status, True)
    check("  and the adjudicator", "adjudicator: tuck" in status, True)
    check("  and that the review route agrees", "seats-review → vex (ok)" in status, True)
    check("  and that the fix route agrees", "seats-fix → drey (ok)" in status, True)
    check("  and that the adjudicator route agrees", "seats-breach → tuck (ok)" in status, True)
    check("  and where each seat's token is referenced",
          f"reviewer {REVIEWER} → {SEAT_PATS[0]}" in status, True)

    # The #16 route holds eligible review work for an isolated worker instead of
    # handing a FIRE payload to the gateway. The #17 profile mapping must remain intact.
    set_prs({"7": pr(7)})
    payload = {**pr_payload(7, requested=REVIEWER), "repository": {"full_name": "acme/seats"}}
    kind, out, err = run("gate_reviewer.py", payload)
    check("the new loop gate stays silent", (kind, out), ("SILENT", "[SILENT]"))
    seats_pending = HOME / "state" / "review-loops" / "seats" / "pending.json"
    queued = json.loads(seats_pending.read_text()) if seats_pending.exists() else {}
    check("  and holds the authorized reviewer head",
          queued.get("reviewer", {}).get("acme/seats#7", {}).get("head"), HEAD_A)
    check("  and never starts a gateway run", no_ledger_run(), True)

    loop_state = HOME / "state" / "review-loops" / "seats"
    loop_state.mkdir(parents=True, exist_ok=True)
    (loop_state / "locks.json").write_text("{}")
    (loop_state / "pending.json").write_text(json.dumps(
        {"reviewer": {f"acme/seats#7": {"at": time.time(), "head": HEAD_A, "url": "u",
                                        "reason": "capacity"}}}))
    before = len(RECEIVED)
    out, _, _ = run("watchdog.py", None, "--loop", "seats", "--drain", "--seat", "reviewer")
    check("HTTP-only drain does not claim queued review started", "started the queued run" in out, False)
    check("  and it wakes the reviewer at the profile the form chose",
          RECEIVED[-1]["path"] if len(RECEIVED) > before else None,
          "/p/vex/webhooks/seats-review")
    check("  with that route's secret", verify_sig(RECEIVED[-1], "seats-review"), True)

    section("seat identity — every surface shows the effective mapping")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_settings(ns())
    shown = buf.getvalue()
    check("settings shows the form's seat mapping", "seat mapping" in shown, True)
    check("  reviewer profile", "profile vex" in shown, True)
    check("  fixer profile", "profile drey" in shown, True)
    check("  adjudicator profile", "profile tuck" in shown, True)
    check("  and resolves each loop against it", "reviewer rev-coach (vex)" in shown, True)
    check("  including the adjudicator it would push", "adjudicator tuck" in shown, True)

    section("seat identity — a form that holds nothing rewrites nothing")
    reset(prs={})
    north = make_loop("north", "acme/north", "vex", "drey")
    south = make_loop("south", "acme/south", "reviewer-profile", "fixer-profile")
    east = make_loop("east", "acme/east", "vex", "drey", adjudicator="tuck")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_settings(ns())
    shown = buf.getvalue()
    check("settings says which loops have no adjudicator",
          "adjudicator (none)" in shown, True)
    check("  and shows the ones that do", "adjudicator tuck" in shown, True)

    rc, out = run_cli(parser_for({}).parse_args(["apply", "--loop", "east", "--dry-run"]))
    check("an empty form changes nothing", "already matches the plugin settings" in out, True)
    check("  and the seat keeps its own profile",
          config.seat_profile(config.load_id("east"), "reviewer"), "vex")
    check("  and its own adjudicator",
          config.seat_profile(config.load_id("east"), "adjudicator"), "tuck")

    # A form with numbers only must not touch seats either: that is what "defaults, not a
    # subscription" means for identity, and it is the difference between a form and a takeover.
    south_before, north_before = loop_bytes("south"), loop_bytes("north")
    rc, out = run_cli(parser_for({"cap": 4}).parse_args(["apply", "--loop", "south"]))
    check("a numbers-only form leaves the seats alone", rc, 0)
    check("  no seat appeared in the diff", "profile" in out, False)
    check("  the loop's own profiles survive",
          (config.seat_profile(config.load_id("south"), "reviewer"),
           config.seat_profile(config.load_id("south"), "fixer")),
          ("reviewer-profile", "fixer-profile"))
    check("  and its routes still run them",
          (subs()["south-review"]["profile"], subs()["south-fix"]["profile"]),
          ("reviewer-profile", "fixer-profile"))
    check("  while the other loops' configs are byte-identical",
          (loop_bytes("north") == north_before, loop_bytes("south") != south_before),
          (True, True))

    section("seat identity — one loop moves, the other two do not (A / B / A)")
    # push the form onto 'south' only, in two steps: a refusal-free preview first
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "south", "--dry-run"]))
    check("apply --dry-run explains the change", rc, 0)
    check("  reviewer profile in the diff", "reviewer profile: reviewer-profile → vex" in out, True)
    check("  fixer profile in the diff", "fixer profile: fixer-profile → drey" in out, True)
    check("  the route rebind it needs",
          "route south-review: profile reviewer-profile → vex" in out, True)
    check("  and it says nothing was written", "nothing written" in out, True)
    check("  nothing was written", config.seat_profile(config.load_id("south"), "reviewer"),
          "reviewer-profile")
    check("  the route still runs the old profile",
          subs()["south-review"]["profile"], "reviewer-profile")

    secret_before = subs()["south-review"]["secret"]
    north_snapshot, east_snapshot = loop_bytes("north"), loop_bytes("east")
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "south"]))
    check("apply stages the change", rc, 0)
    check("  seat moved", config.seat_profile(config.load_id("south"), "reviewer"), "vex")
    check("  route rebound in the same operation", subs()["south-review"]["profile"], "vex")
    check("  and it reports the rebind", "route south-review rebound → profile vex" in out, True)
    check("  the route keeps its secret", subs()["south-review"]["secret"], secret_before)
    check("  a second apply is a no-op",
          "already matches the plugin settings"
          in run_cli(parser_for(form).parse_args(["apply", "--loop", "south"]))[1], True)
    check("  loop A is untouched (byte-identical)", loop_bytes("north"), north_snapshot)
    check("  loop C is untouched (byte-identical)", loop_bytes("east"), east_snapshot)
    check("  and their routes still run their own profiles",
          (subs()["north-review"]["profile"], subs()["east-review"]["profile"]), ("vex", "vex"))
    check("  (A and C agree because they were configured that way, not because B leaked)",
          (config.seat_profile(config.load_id("north"), "reviewer"),
           config.seat_profile(config.load_id("east"), "reviewer")), ("vex", "vex"))
    check("  and B is the one that moved",
          config.seat_profile(config.load_id("south"), "fixer"), "drey")

    section("seat identity — a seat mid-run is not rewritten underneath itself")
    busy = make_loop("busy", "acme/busy", "reviewer-profile", "fixer-profile")
    busy_state = STATE_DIR / "busy"
    busy_state.mkdir(parents=True, exist_ok=True)
    (busy_state / "locks.json").write_text(json.dumps(
        {"reviewer": {f"acme/busy#7": {"at": time.time(), "head": HEAD_A, "why": "review"}}}))
    before_bytes = loop_bytes("busy")
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "busy"]))
    check("a seat in flight → apply refused", rc, 2)
    check("  it names the running seat", "reviewer is in flight" in out, True)
    check("  and offers the explicit override", "--while-busy" in out, True)
    check("  nothing was written", loop_bytes("busy"), before_bytes)
    check("  the route still runs the old profile",
          subs()["busy-review"]["profile"], "reviewer-profile")
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "busy", "--dry-run"]))
    check("  (a dry run is still allowed while a seat is busy)", rc, 0)
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "busy", "--while-busy"]))
    check("--while-busy applies it anyway", rc, 0)
    check("  seat moved", config.seat_profile(config.load_id("busy"), "reviewer"), "vex")
    check("  route rebound", subs()["busy-review"]["profile"], "vex")
    check("  and it says the live run keeps its identity",
          "keeps the identity it started with" in out, True)

    section("seat identity — an invalid mapping fails before any side effect")
    # the loop the refusals below push onto, written the same way `init` writes one
    make_loop("seats", "acme/seats", "vex", "drey", adjudicator="tuck")
    for label, bad, expect in (
            ("a profile that does not exist", {"reviewer_profile": "ghost"},
             "no Hermes profile named 'ghost'"),
            ("a login outside the allowlist", {"reviewer_login": "stranger"},
             "is not in this loop's reviewers allowlist"),
            ("one profile for both seats", {"reviewer_profile": "drey", "fixer_profile": "drey"},
             "both run as profile 'drey'"),
            ("an adjudicator that is one of the seats", {"adjudicator_profile": "vex"},
             "the same as a seat it is meant to rule on"),
            ("a seat moving onto the adjudicator's profile", {"reviewer_profile": "tuck"},
             "the same as a seat it is meant to rule on"),
            ("an adjudicator profile that does not exist", {"adjudicator_profile": "ghost"},
             "no Hermes profile named 'ghost'")):
        fingerprint = (loop_bytes("seats"), SUBS.read_text())
        rc, out = run_cli(parser_for({**form, **bad}).parse_args(["apply", "--loop", "seats"]))
        check(f"{label} → refused", rc, 2)
        check(f"  {label}: the reason is named", expect in out, True)
        check(f"  {label}: nothing was written", (loop_bytes("seats"), SUBS.read_text()),
              fingerprint)

    # A loop that trusts both logins on both sides leaves only the two-seats-one-account rule.
    overlap = make_loop("overlap", "acme/overlap", "reviewer-profile", "fixer-profile")
    raw = json.loads(loop_bytes("overlap"))
    raw["reviewers"] = [REVIEWER, FIXER]
    (LOOPS_DIR / "overlap.json").write_text(json.dumps(raw))
    fingerprint = (loop_bytes("overlap"), SUBS.read_text())
    rc, out = run_cli(parser_for({**form, "reviewer_login": FIXER, "fixer_login": FIXER})
                      .parse_args(["apply", "--loop", "overlap"]))
    check("two seats on one login → refused", rc, 2)
    check("  and it says why", "both act as" in out, True)
    check("  nothing was written", (loop_bytes("overlap"), SUBS.read_text()), fingerprint)

    shared = make_loop("shared", "acme/shared", "reviewer-profile", "fixer-profile")
    raw = json.loads(loop_bytes("shared"))
    raw["tokens"] = {REVIEWER: str(SEAT_PATS[0]), FIXER: str(SEAT_PATS[0])}
    (LOOPS_DIR / "shared.json").write_text(json.dumps(raw))
    fingerprint = (loop_bytes("shared"), SUBS.read_text())
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "shared"]))
    check("two seats on one token file → refused", rc, 2)
    check("  and it says why", "read the same token file" in out, True)
    check("  nothing was written", (loop_bytes("shared"), SUBS.read_text()), fingerprint)

    for kind in ("symlink", "hardlink"):
        alias = TMP / f"{kind}-fixer.pat"
        alias.unlink(missing_ok=True)
        if kind == "symlink":
            alias.symlink_to(SEAT_PATS[0])
        else:
            os.link(SEAT_PATS[0], alias)
        raw["tokens"] = {REVIEWER: str(SEAT_PATS[0]), FIXER: str(alias)}
        (LOOPS_DIR / "shared.json").write_text(json.dumps(raw))
        fingerprint = (loop_bytes("shared"), SUBS.read_text())
        rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "shared"]))
        check(f"{kind} alias to one credential → refused", rc, 2)
        check(f"  {kind}: same file identified", "read the same token file" in out, True)
        check(f"  {kind}: no writes", (loop_bytes("shared"), SUBS.read_text()), fingerprint)

    section("seat identity — profile homes must be distinct real directories")
    profile_root = HOME / "profiles"
    alias = profile_root / "alias-fixer"
    alias.symlink_to(profile_root / "vex", target_is_directory=True)
    check("a symlink to a profile is not a profile", config.profile_exists("alias-fixer"), False)
    for action in ("init", "apply"):
        alias_form = {"reviewer_profile": "vex", "fixer_profile": "alias-fixer",
                      "reviewer_login": REVIEWER, "fixer_login": FIXER}
        alias_parser = parser_for(alias_form)
        if action == "init":
            args = ["init", "--repo", "acme/profile-alias", "--id", "profile-alias",
                    "--fixer", FIXER, "--reviewer", REVIEWER, "--host", HOST,
                    "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                    "--token", f"{FIXER}={SEAT_PATS[1]}"]
            fingerprint = SUBS.read_text()
        else:
            make_loop("profile-alias", "acme/profile-alias", "vex", "drey")
            args = ["apply", "--loop", "profile-alias"]
            fingerprint = (loop_bytes("profile-alias"), SUBS.read_text())
        rc, out = run_cli(alias_parser.parse_args(args))
        check(f"{action}: symlinked profile home refused", rc, 2)
        check(f"  {action}: refusal names the profile", "alias-fixer" in out, True)
        check(f"  {action}: no writes", (loop_bytes("profile-alias"), SUBS.read_text())
              if action == "apply" else SUBS.read_text(), fingerprint)
        if action == "init":
            check("  init: no loop created", (LOOPS_DIR / "profile-alias.json").exists(), False)
    alias.unlink()
    alias.symlink_to(HOME, target_is_directory=True)
    check("a symlink to the default home is not a profile",
          config.profile_exists("alias-fixer"), False)
    alias.unlink()

    # A bind mount can expose one inode under two non-symlink names. Simulate that
    # same-directory identity without requiring mount privileges, at the path seam.
    real_profile_dir = config.profile_dir
    config.profile_dir = lambda name: (profile_root / "vex" if name == "drey"
                                      else real_profile_dir(name))
    try:
        for action in ("init", "apply"):
            alias_form = {"reviewer_profile": "vex", "fixer_profile": "drey",
                          "reviewer_login": REVIEWER, "fixer_login": FIXER}
            if action == "init":
                args = ["init", "--repo", "acme/inode-alias", "--id", "inode-alias",
                        "--fixer", FIXER, "--reviewer", REVIEWER, "--host", HOST,
                        "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                        "--token", f"{FIXER}={SEAT_PATS[1]}"]
                fingerprint = SUBS.read_text()
            else:
                make_loop("inode-alias", "acme/inode-alias", "reviewer-profile", "fixer-profile")
                args = ["apply", "--loop", "inode-alias"]
                fingerprint = (loop_bytes("inode-alias"), SUBS.read_text())
            rc, out = run_cli(parser_for(alias_form).parse_args(args))
            check(f"{action}: same-directory profiles refused", rc, 2)
            check(f"  {action}: identity reason", "same profile home" in out, True)
            check(f"  {action}: no writes", (loop_bytes("inode-alias"), SUBS.read_text())
                  if action == "apply" else SUBS.read_text(), fingerprint)
            if action == "init":
                check("  init: no alias loop created", (LOOPS_DIR / "inode-alias.json").exists(), False)
    finally:
        config.profile_dir = real_profile_dir

    # The adjudicator has no login, but must not share the reviewed seat's home.
    config.profile_dir = lambda name: (profile_root / "vex" if name == "tuck"
                                      else real_profile_dir(name))
    try:
        make_loop("adj-alias", "acme/adj-alias", "vex", "drey",
                  adjudicator="fixer-profile")
        before = (loop_bytes("adj-alias"), SUBS.read_text())
        rc, out = run_cli(parser_for({"adjudicator_profile": "tuck"})
                          .parse_args(["apply", "--loop", "adj-alias"]))
        check("adjudicator sharing reviewer home refused", rc, 2)
        check("  adjudicator identity reason", "same profile home" in out, True)
        check("  adjudicator refusal has no writes", (loop_bytes("adj-alias"), SUBS.read_text()),
              before)
    finally:
        config.profile_dir = real_profile_dir

    section("seat identity — a route belongs to the loop that already owns it")
    make_loop("overlap2", "acme/overlap2", "reviewer-profile", "fixer-profile")
    raw = json.loads(loop_bytes("overlap2"))
    raw["seats"]["reviewer"]["route"] = "overlap-review"      # loop `overlap` owns that name
    (LOOPS_DIR / "overlap2.json").write_text(json.dumps(raw))
    fingerprint = (loop_bytes("overlap2"), SUBS.read_text())
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "overlap2"]))
    check("a route another loop owns → refused", rc, 2)
    check("  and it names the owner", "already belongs to loop overlap" in out, True)
    check("  nothing was written", (loop_bytes("overlap2"), SUBS.read_text()), fingerprint)

    make_loop("foreign", "acme/foreign", "reviewer-profile", "fixer-profile")
    data = subs()
    data["stranger-inbox"] = {"description": "someone else's route", "events": ["push"],
                              "secret": "not-ours", "prompt": "", "skills": [], "deliver": "local",
                              "profile": "other-plugin", "script": "not_our_gate.py", "host": HOST}
    SUBS.write_text(json.dumps(data))
    raw = json.loads(loop_bytes("foreign"))
    raw["seats"]["reviewer"]["route"] = "stranger-inbox"
    (LOOPS_DIR / "foreign.json").write_text(json.dumps(raw))
    fingerprint = (loop_bytes("foreign"), SUBS.read_text())
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "foreign"]))
    check("a route running someone else's gate → refused", rc, 2)
    check("  and it says which script", "not_our_gate.py" in out, True)
    check("  nothing was written", (loop_bytes("foreign"), SUBS.read_text()), fingerprint)

    # A script-less route and a route with our script but another prompt are not ours.
    # Neither may have its secret retained while its handler is rewritten by apply.
    for label, script, prompt in (("missing gate", None, "someone else's prompt"),
                                  ("foreign prompt", "gate_reviewer.py", "someone else's prompt")):
        data = subs()
        data["stranger-inbox"].update(script=script, prompt=prompt)
        SUBS.write_text(json.dumps(data))
        fingerprint = (loop_bytes("foreign"), SUBS.read_text())
        rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "foreign"]))
        check(f"{label} route → refused", rc, 2)
        check(f"  {label}: nothing was written", (loop_bytes("foreign"), SUBS.read_text()), fingerprint)

    two_seats = make_loop("two-seats", "acme/two-seats", "reviewer-profile", "fixer-profile")
    raw = json.loads(loop_bytes("two-seats"))
    raw["seats"]["fixer"]["route"] = raw["seats"]["reviewer"]["route"]   # one route, two seats
    (LOOPS_DIR / "two-seats.json").write_text(json.dumps(raw))
    fingerprint = (loop_bytes("two-seats"), SUBS.read_text())
    rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "two-seats"]))
    check("two seats on one route → refused", rc, 2)
    check("  and it says why", "routed to more than one seat" in out, True)
    check("  nothing was written", (loop_bytes("two-seats"), SUBS.read_text()), fingerprint)

    # `init` writes routes from scratch, so it must refuse a name another loop already owns too.
    fingerprint = (SUBS.read_text(), loop_bytes("north"))
    rc, out = run_cli(parser.parse_args(["init", "--repo", "acme/elsewhere", "--id", "north",
                                         "--fixer", FIXER, "--reviewer", REVIEWER, "--host", HOST,
                                         "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                                         "--token", f"{FIXER}={SEAT_PATS[1]}"]))
    check("init refuses a route another loop owns", rc, 2)
    check("  and it names the owner", "already belongs to loop north" in out, True)
    check("  nothing was written", (SUBS.read_text(), loop_bytes("north")), fingerprint)

    section("seat identity — a rebind that cannot be written leaves the loop where it was")
    # A registry write is the one thing here that can fail on someone else's account (a full disk, a
    # lock, a read-only mount), so the staged move has to be all-or-nothing: the loop config and the
    # routes are read back after the refusal and must be byte-identical.
    make_loop("frozen", "acme/frozen", "reviewer-profile", "fixer-profile")
    fingerprint = (loop_bytes("frozen"), SUBS.read_text())
    real_new_route = cli.routes.new_route

    def refuse_route(*_args, **_kwargs):
        raise OSError("registry is on a read-only mount")

    cli.routes.new_route = refuse_route
    try:
        rc, out = run_cli(parser_for(form).parse_args(["apply", "--loop", "frozen"]))
    finally:
        cli.routes.new_route = real_new_route
    check("a rebind the registry refuses → refused", rc, 2)
    check("  and it says config was unchanged", "config unchanged" in out, True)
    check("  loop and routes unchanged", (loop_bytes("frozen"), SUBS.read_text()), fingerprint)

    # `init` is the other half of the same promise: a loop whose routes cannot be written is not
    # left behind as a config with no route to wake it.
    cli.routes.new_route = refuse_route
    try:
        rc, out = run_cli(parser.parse_args(["init", "--repo", "acme/halfway", "--id", "halfway",
                                             "--fixer", FIXER, "--reviewer", REVIEWER,
                                             "--host", HOST,
                                             "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                                             "--token", f"{FIXER}={SEAT_PATS[1]}"]))
    finally:
        cli.routes.new_route = real_new_route
    check("a route install that fails → refused", rc, 2)
    check("  and it says previous state was restored", "previous config and routes restored" in out, True)
    check("  no loop config left behind", (LOOPS_DIR / "halfway.json").exists(), False)
    check("  no route left behind", [n for n in subs() if n.startswith("halfway")], [])

    section("seat identity — credentials are checked before anything is written")
    parser = parser_for(form)          # the credentials below are named by this form
    empty_pat = TMP / "empty.pat"
    empty_pat.write_text("")
    for label, extra, expect in (
            ("no token mappings", [], "has no entry in 'tokens'"),
            ("a seat login with no token mapped", ["--read-token", FIXER,
                                                   "--token", f"{FIXER}={SEAT_PATS[1]}"],
             "no token mapped for the reviewer login"),
            ("a token file that is not there", ["--read-token", REVIEWER,
                                                "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                                                "--token", f"{FIXER}={TMP / 'missing.pat'}"],
             "token file for 'dev-fixer' is missing"),
            ("a token file that is empty", ["--read-token", REVIEWER,
                                            "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                                            "--token", f"{FIXER}={empty_pat}"],
             "is empty")):
        (LOOPS_DIR / "probe.json").unlink(missing_ok=True)
        fingerprint = SUBS.read_text()
        args = ["init", "--repo", "acme/probe", "--fixer", FIXER, "--reviewer", REVIEWER,
                "--host", HOST, *extra]
        rc, out = run_cli(parser.parse_args(args))
        check(f"{label} → refused", rc, 2)
        check(f"  {label}: the reason is named", expect in out, True)
        check(f"  {label}: no loop config", (LOOPS_DIR / "probe.json").exists(), False)
        check(f"  {label}: no routes touched", SUBS.read_text(), fingerprint)
    gh.api = original_api


def group_webhook_host() -> None:
    section("webhook host — never borrow another operator's gateway")
    from review_loop import cli, config, gh

    reset(prs={})
    init_args = ["init", "--repo", "acme/host-probe", "--fixer", FIXER,
                 "--reviewer", REVIEWER, "--reviewer-profile", "reviewer-profile",
                 "--fixer-profile", "fixer-profile", "--hooks",
                 "--token", f"{REVIEWER}={SEAT_PATS[0]}",
                 "--token", f"{FIXER}={SEAT_PATS[1]}"]
    calls = []
    installed_hooks = {}
    original_api = gh.api
    def fake_api(loop, path, **kwargs):
        if path.endswith('/hooks?per_page=100'):
            return [{'id': key, 'config': {'url': url}} for key, url in installed_hooks.items()]
        if kwargs.get('method') == 'POST':
            calls.append((path, kwargs))
            hook_id = len(calls)
            installed_hooks[hook_id] = kwargs['body']['config']['url']
            return {'id': hook_id}
        if kwargs.get('method') == 'DELETE':
            installed_hooks.pop(int(path.rsplit('/', 1)[-1]), None)
            return None
        return None
    gh.api = fake_api
    try:
        def parser_for(settings=None):
            fake = FakeCtx()
            cli.register_cli(fake, settings=settings)
            parser = argparse.ArgumentParser(prog="hermes review-loop")
            fake.setup(parser)
            return parser

        parser = parser_for()
        check("unset schema host has no operator URL", config.settings_defaults(None)["host"], "")
        kept = config.apply_settings(config.load_id("widgets"), {"host": ""})
        check("empty plugin setting preserves existing loop host", kept["host"], HOST)
        for label, args in (("missing", []), ("blank", ["--host", "   "]),
                            ("relative", ["--host", "gateway.local"]),
                            ("invalid URL", ["--host", "https://"]),
                            ("path", ["--host", "https://own.example/someone-else"]),
                            ("credentials", ["--host", "https://user:pass@own.example"]),
                            ("empty userinfo", ["--host", "https://@own.example"]),
                            ("empty password", ["--host", "https://user:@own.example"]),
                            ("encoded slash in hostname", ["--host", "https://own.example%2fattacker.example"]),
                            ("encoded at in hostname", ["--host", "https://own.example%40attacker.example"]),
                            ("backslash", ["--host", "https://own.example\\attacker.example"]),
                            ("bad port", ["--host", "https://own.example:wrong"]),
                            ("empty port", ["--host", "https://own.example:"]),
                            ("signed port", ["--host", "https://own.example:+80"]),
                            ("unicode port", ["--host", "https://own.example:８０"]),
                            ("out-of-range port", ["--host", "https://own.example:65536"]),
                            ("empty query", ["--host", "https://own.example?"]),
                            ("query", ["--host", "https://own.example/?foo=bar"]),
                            ("empty fragment", ["--host", "https://own.example#"]),
                            ("fragment", ["--host", "https://own.example#x"]),
                            ("double trailing slash", ["--host", "https://own.example//"]),
                            ("unbracketed IPv6", ["--host", "http://::1"]),
                            ("bad bracketed IPv6", ["--host", "http://[2001:db8:::1]"]),
                            ("invalid DNS label", ["--host", "https://own..example"]),
                            ("invalid IPv4", ["--host", "http://999.999.999.999"])):
            (LOOPS_DIR / "host-probe.json").unlink(missing_ok=True)
            before = (LOOPS_DIR / "widgets.json").read_bytes(), SUBS.read_bytes()
            calls.clear()
            buf = io.StringIO()
            parsed = parser.parse_args([*init_args, *args])
            with contextlib.redirect_stdout(buf):
                rc = parsed.func(parsed)
            check(f"{label} host refused before init writes", rc, 2)
            check(f"  {label}: actionable error", "--host" in buf.getvalue(), True)
            check(f"  {label}: no config/route writes",
                  ((LOOPS_DIR / "widgets.json").read_bytes(), SUBS.read_bytes()) == before, True)
            check(f"  {label}: no new loop config", (LOOPS_DIR / "host-probe.json").exists(), False)
            check(f"  {label}: no API calls", calls, [])

        parsed = parser.parse_args([arg for arg in init_args if arg != "--hooks"])
        with contextlib.redirect_stdout(io.StringIO()):
            rc = parsed.func(parsed)
        check("without --hooks still refuses missing host before writes", rc, 2)
        check("  no config/route files added", (LOOPS_DIR / "host-probe.json").exists(), False)

        from urllib.request import Request
        for origin, expected in (
                ("https://own.example:8443/", "https://own.example:8443"),
                ("https://own.example", "https://own.example"),
                ("HTTPS://OWN.example", "HTTPS://OWN.example"),
                ("https://own.example.", "https://own.example."),
                ("http://localhost:8080", "http://localhost:8080"),
                ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
                ("https://[2001:db8::1]:8443/", "https://[2001:db8::1]:8443"),
                ("http://[::1]", "http://[::1]")):
            actual = config.webhook_host(origin, required=True)
            check(f"valid origin {origin}", actual, expected)
            request = Request(f"{actual}/webhooks/test")
            check(f"  urllib destination {origin}",
                  (request.type, request.host, request.selector),
                  (expected.split("://", 1)[0].lower(), expected.split("://", 1)[1], "/webhooks/test"))

        for label, settings, extra, host in (
                ("explicit --host", {}, ["--host", "https://own.example:8443/"], "https://own.example:8443"),
                ("own plugin setting", {"host": "https://settings.example"}, [], "https://settings.example")):
            reset(prs={})
            calls.clear()
            installed_hooks.clear()
            parser = parser_for(settings)
            args = parser.parse_args([*init_args, *extra])
            with contextlib.redirect_stdout(io.StringIO()):
                rc = args.func(args)
            check(f"{label}: init succeeds", rc, 0)
            check(f"  {label}: saved host", config.load_id("host-probe")["host"], host)
            check(f"  {label}: hook URLs", [body["config"]["url"] for _, kw in calls
                  for body in [kw["body"]]],
                  [f"{host}/p/reviewer-profile/webhooks/host-probe-review",
                   f"{host}/p/fixer-profile/webhooks/host-probe-fix"])
            check(f"  {label}: route hosts",
                  [json.loads(SUBS.read_text())[name]["host"]
                   for name in ("host-probe-review", "host-probe-fix")], [host, host])

        legacy = json.loads((LOOPS_DIR / "widgets.json").read_text())
        legacy["host"] = "https://existing.example/"
        (LOOPS_DIR / "widgets.json").write_text(json.dumps(legacy))
        check("existing explicit host loads unchanged except trailing slash",
              config.load_id("widgets")["host"], "https://existing.example")
        # A legacy route without a host must not produce a relative URL, even when
        # the gateway subscription still carries a valid secret.
        from review_loop import routes
        import urllib.request
        subs = json.loads(SUBS.read_text())
        subs["widgets-review"].pop("host", None)
        SUBS.write_text(json.dumps(subs))
        check("hostless route has no URL", routes.url_for("widgets-review"), None)
        check("hostless route has no target", routes.target("widgets-review"), None)
        original_urlopen = urllib.request.urlopen
        def forbidden_urlopen(*args, **kwargs):
            raise AssertionError("hostless route attempted a webhook POST")
        urllib.request.urlopen = forbidden_urlopen
        try:
            check("hostless route cannot fire", routes.fire("widgets-review", "pull_request", {}, "probe"), False)
        finally:
            urllib.request.urlopen = original_urlopen
        check("explicit host resolves legacy route", routes.url_for("widgets-review", HOST),
              f"{HOST}/p/reviewer-profile/webhooks/widgets-review")
        check("existing route host still resolves", routes.url_for("widgets-fix"),
              f"{HOST}/p/fixer-profile/webhooks/widgets-fix")
        check("existing host yields a target", routes.target("widgets-fix"),
              (f"{HOST}/p/fixer-profile/webhooks/widgets-fix",
               subs["widgets-fix"]["secret"].encode()))
        check("existing host can fire", routes.fire("widgets-fix", "pull_request_review", {}, "probe"), True)
        check("existing host delivered to the sink", RECEIVED[-1]["path"],
              "/p/fixer-profile/webhooks/widgets-fix")
        calls.clear()
        try:
            cli._install_hooks({**config.load_id("widgets"), "host": ""}, None)
        except config.ConfigError:
            check("install refuses absent loop host", True, True)
        else:
            check("install refuses absent loop host", False, True)
        check("absent loop host made no API calls", calls, [])
        try:
            routes.url_for("widgets-review", "https://own.example/foreign")
        except config.ConfigError:
            check("invalid route host rejected", True, True)
        else:
            check("invalid route host rejected", False, True)
        subs["widgets-review"]["host"] = "https://own.example/foreign"
        SUBS.write_text(json.dumps(subs))
        check("invalid stored origin has no target", routes.target("widgets-review"), None)
        check("invalid stored origin cannot fire", routes.fire("widgets-review", "pull_request", {}, "probe"), False)
        # The second missing route must be caught before the first hook is posted.
        subs.pop("widgets-fix")
        SUBS.write_text(json.dumps(subs))
        calls.clear()
        try:
            cli._install_hooks(config.load_id("widgets"), None)
        except config.ConfigError as exc:
            check("install refuses missing route before API", "route" in str(exc), True)
        else:
            check("install refuses missing route before API", False, True)
        check("missing route made no API calls", calls, [])
    finally:
        gh.api = original_api


GROUPS = {
    "seats": group_seats,
    "parallel": group_parallel,
    "exclusive": group_exclusive,
    "settings": group_settings,
    "webhook_host": group_webhook_host,
    "plugin_settings": group_plugin_settings,
    "seat_identity": group_seat_identity,
}
