"""``doctor``: a correct installation passes, and says nothing it cannot prove."""

from __future__ import annotations

from .fixture import *  # noqa: F403 - the shared harness namespace


def doctor_parser():
    """The real CLI tree, so `doctor` is exercised through the argparse wiring users get."""
    import argparse

    from review_loop import cli

    fake = FakeCtx()
    cli.register_cli(fake)
    parser = argparse.ArgumentParser(prog="hermes review-loop")
    fake.setup(parser)
    return parser


def run_doctor(*argv) -> tuple[int, str]:
    parsed = doctor_parser().parse_args(["doctor", *argv])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = parsed.func(parsed)
    return rc, buf.getvalue()


def loop_file() -> pathlib.Path:
    return LOOPS_DIR / "widgets.json"


def load_loop() -> dict:
    return json.loads(loop_file().read_text())


def save_loop(cfg: dict) -> None:
    loop_file().write_text(json.dumps(cfg, indent=2))


def edit_loop(**keys) -> dict:
    cfg = load_loop()
    cfg.update(keys)
    save_loop(cfg)
    return cfg


def profile_env(profile: str) -> pathlib.Path:
    return TMP / "hermes-home" / "profiles" / profile / ".env"


def install_doctor_fixture() -> dict:
    """A complete, correct installation — down to the pieces `reset()` does not build.

    `reset()` gives the loop, the clone and the three routes. The parts doctor exists to check
    beyond those are built here explicitly: the two profile homes (with the GH_TOKEN a seat
    pushes with), owner-only PAT files, the cron shim pinned to *this* plugin install, the
    scheduler's job store, and two repo hooks pointing at this loop's own gateway.
    """
    from review_loop import cli, config

    reset(prs={})
    cfg = load_loop()
    cfg["seats"]["reviewer"]["login"] = REVIEWER
    save_loop(cfg)
    edit_subs(lambda subs: subs["widgets-breach"].update(script="gate_adjudicator.py"))
    for profile in ("reviewer-profile", "fixer-profile"):
        home = TMP / "hermes-home" / "profiles" / profile
        home.mkdir(parents=True, exist_ok=True)
        (home / ".env").write_text("DISCORD_BOT_TOKEN=unused\nDISCORD_HOME_CHANNEL=0\n"
                                   "GH_TOKEN=unused\n")
    for pat in (TMP / "rev.pat", TMP / "fix.pat"):
        pat.chmod(0o600)
    scripts = TMP / "hermes-home" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / cli.SHIM_NAME).write_text(cli.SHIM.format(watchdog=ROOT / "scripts" / "watchdog.py"))
    cron = TMP / "hermes-home" / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{
        "id": "watchdog-job", "name": cli.watchdog_job_name({"id": "widgets"}),
        "script": cli.SHIM_NAME, "no_agent": True, "enabled": True, "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 15},
        "schedule_display": "every 15m", "next_run_at": PAST, "deliver": "local"}]}))
    DATA["world"]["hooks"] = [
        {"id": 41, "active": True, "events": ["pull_request"],
         "config": {"url": f"{HOST}/p/reviewer-profile/webhooks/widgets-review",
                    "content_type": "json"}},
        {"id": 42, "active": True, "events": ["pull_request_review"],
         "config": {"url": f"{HOST}/p/fixer-profile/webhooks/widgets-fix",
                    "content_type": "json"}},
    ]
    save_world()
    return config.load_id("widgets")


def doctor_runtime_fixture() -> list[pathlib.Path]:
    """A runtime file whose "Hermes" is this interpreter, and a provider in each seat profile.

    Returns the paths it created, for the caller to remove: later groups rely on there being no
    runtime file (eligible gates then hold, fail closed).
    """
    import sys
    home = TMP / "hermes-home"
    venv = TMP / "doctor-venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    if not (venv / "bin" / "python").exists():
        (venv / "bin" / "python").symlink_to(sys.executable)
    runtime = home / "review-loop-runtime.json"
    runtime.write_text(json.dumps({"source": str(TMP), "venv": str(venv),
                                   "runtime": str(TMP), "rust": str(TMP)}))
    runtime.chmod(0o600)
    added = [runtime]
    model = "model:\n  default: test-model\n  provider: openrouter\n"
    for profile in (home / "profiles" / "reviewer-profile", home / "profiles" / "fixer-profile"):
        (profile / "config.yaml").write_text(model)
    if not (home / "config.yaml").exists():
        (home / "config.yaml").write_text(model)
        added.append(home / "config.yaml")
    return added


def edit_subs(mutate) -> dict:
    subs = json.loads(SUBS.read_text())
    mutate(subs)
    SUBS.write_text(json.dumps(subs, indent=2))
    return subs


def tree_digest(root: pathlib.Path) -> str:
    """A digest of every path under `root` with its size and mtime: a preflight that wrote
    anything changes it. A digest rather than the tree itself, so a passing check stays a
    one-line line of output."""
    lines = []
    for path in sorted(root.rglob("*")):
        try:
            stat = path.stat()
        except OSError:
            continue
        lines.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
    body = "\n".join(lines).encode()
    return f"{len(lines)} entries, sha256 {hashlib.sha256(body).hexdigest()[:16]}"


def group_doctor() -> None:
    """`hermes review-loop doctor` — the read-only preflight of an installation."""
    from review_loop import cli, config, doctor

    section("doctor — a correct installation passes, and says nothing it cannot prove")
    check("doctor and init agree on the shim name", doctor.SHIM_NAME, cli.SHIM_NAME)
    check("doctor and init agree on the watchdog job name",
          doctor.watchdog_job_name({"id": "x"}), cli.watchdog_job_name({"id": "x"}))

    install_doctor_fixture()
    added = doctor_runtime_fixture()
    before_files = tree_digest(TMP)
    before_posts = len(RECEIVED)
    rc, out = run_doctor("--loop", "widgets")
    check("a correct install passes", rc, 0)
    check("  every check verified", "widgets: 24 verified, 0 failed, 0 unknown (of 24 checks)" in out,
          True)
    check("  nothing is marked failed", "❌" in out, False)
    check("  the header says it is read-only",
          "read-only: it writes nothing and fires nothing" in out, True)
    for name in ("config", "profile:reviewer", "profile:fixer", "profile:adjudicator", "credential:reviewer",
                 "credential:fixer", "token:rev-coach", "token:dev-fixer", "read_token",
                 "route:widgets-review", "route:widgets-fix", "route:widgets-breach", "scripts",
                 "cron:shim", "cron:job", "clone", "state_dir", "roots", "gateway",
                 "hook:widgets-review", "hook:widgets-fix",
                 "model:reviewer", "model:fixer", "model:adjudicator"):
        check(f"  ✅ {name}", f"✅ {name}" in out, True)
    check("  it writes nothing", tree_digest(TMP), before_files)
    for path in added:   # the model check reads profiles through the runtime's Hermes (#32)
        path.unlink()
    check("  it fires no webhook", len(RECEIVED), before_posts)
    check("  no token value appears in the report", "token-reviewer" in out, False)
    check("  nor a route secret",
          hashlib.sha256(b"widgets-review").hexdigest() in out, False)

    section("doctor — route/profile/secret correspondence")
    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-fix"].update(profile="someone-else"))
    rc, out = run_doctor("--loop", "widgets")
    check("a route waking another profile fails", rc, 1)
    check("  and it names the route", "❌ route:widgets-fix" in out, True)
    check("  and both profiles", "someone-else" in out and "fixer-profile" in out, True)
    check("  with a remediation", "re-run init" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs.pop("widgets-fix"))
    rc, out = run_doctor("--loop", "widgets")
    check("a missing route fails", rc, 1)
    check("  named, not guessed at", "❌ route:widgets-fix" in out and "not in" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-review"].update(secret=""))
    rc, out = run_doctor("--loop", "widgets")
    check("a route with no secret fails", rc, 1)
    check("  and says why", "❌ route:widgets-review" in out and "without a secret" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-fix"].update(prompt=""))
    rc, out = run_doctor("--loop", "widgets")
    check("a route with no prompt fails", rc, 1)
    check("  and says why", "❌ route:widgets-fix" in out and "without a prompt" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-review"].update(script="gate_fixer.py"))
    rc, out = run_doctor("--loop", "widgets")
    check("a route running the wrong gate fails", rc, 1)
    check("  and names both scripts",
          "❌ route:widgets-review" in out and "'gate_fixer.py'" in out
          and "'gate_reviewer.py'" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-fix"].update(events=["pull_request"]))
    rc, out = run_doctor("--loop", "widgets")
    check("a route subscribed to the wrong event fails", rc, 1)
    check("  and names the event", "❌ route:widgets-fix" in out
          and "pull_request_review" in out, True)

    for malformed in (42, "pull_request", [42]):
        install_doctor_fixture()
        edit_subs(lambda subs: subs["widgets-review"].update(events=malformed))
        rc, out = run_doctor("--loop", "widgets")
        check(f"malformed reviewer route events {malformed!r} fail without crashing", rc, 1)
        check("  route is not verified", "❌ route:widgets-review" in out, True)

    for field, value in (("prompt", ""), ("events", ["push"]), ("events", 42)):
        install_doctor_fixture()
        edit_subs(lambda subs: subs["widgets-breach"].update({field: value}))
        rc, out = run_doctor("--loop", "widgets")
        check(f"adjudicator {field}={value!r} fails without crashing", rc, 1)
        check("  adjudicator route is not verified", "❌ route:widgets-breach" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-fix"].update(host="https://old-gateway.example"))
    rc, out = run_doctor("--loop", "widgets")
    check("a route still registered at the old gateway fails", rc, 1)
    check("  and reports mismatched origins without echoing URLs",
          "❌ route:widgets-fix" in out and "registered gateway origin differs" in out
          and "https://old-gateway.example" not in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs.pop("widgets-breach"))
    rc, out = run_doctor("--loop", "widgets")
    check("a missing adjudicator route fails", rc, 1)
    check("  and says what is lost", "❌ route:widgets-breach" in out
          and "breach marker" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-breach"].update(profile="vex"))
    rc, out = run_doctor("--loop", "widgets")
    check("an adjudicator route waking a seat fails", rc, 1)
    check("  and names adjudicator.profile", "❌ route:widgets-breach" in out
          and "adjudicator.profile" in out, True)

    install_doctor_fixture()
    cfg = load_loop()
    cfg["adjudicator"]["profile"] = "missing-judge"
    save_loop(cfg)
    edit_subs(lambda subs: subs["widgets-breach"].update(profile="missing-judge"))
    rc, out = run_doctor("--loop", "widgets")
    check("a correctly routed adjudicator without a profile home fails", rc, 1)
    check("  adjudicator profile is absent, never verified", "❌ profile:adjudicator" in out
          and "✅ route:widgets-breach" in out, True)
    check("  remediation names the missing profile", "hermes profile create missing-judge" in out, True)

    install_doctor_fixture()
    SUBS.unlink()
    rc, out = run_doctor("--loop", "widgets")
    check("no route registry at all fails", rc, 1)
    check("  and says so once", "❌ routes" in out and "no route registry" in out, True)

    install_doctor_fixture()
    SUBS.write_text("{ not json")
    rc, out = run_doctor("--loop", "widgets")
    check("an unreadable registry fails", rc, 1)
    check("  without pretending to know the routes", "not readable JSON" in out, True)

    section("doctor — credentials, without ever printing one")
    install_doctor_fixture()
    (TMP / "fix.pat").unlink()
    rc, out = run_doctor("--loop", "widgets")
    check("a missing token file fails", rc, 1)
    check("  named with its path", "❌ token:dev-fixer" in out and "no file at" in out, True)

    install_doctor_fixture()
    (TMP / "rev.pat").chmod(0o644)
    rc, out = run_doctor("--loop", "widgets")
    check("a world-readable PAT fails", rc, 1)
    check("  with the mode it has", "mode 644" in out, True)
    check("  and the chmod to run", "chmod 600" in out, True)

    install_doctor_fixture()
    (TMP / "rev.pat").write_text("\n")
    rc, out = run_doctor("--loop", "widgets")
    check("an empty PAT file fails", rc, 1)
    check("  and says it is empty", "❌ token:rev-coach" in out and "is empty" in out, True)

    install_doctor_fixture()
    edit_loop(tokens={}, read_token="")
    rc, out = run_doctor("--loop", "widgets")
    check("no credential mapping at all fails", rc, 1)
    check("  reported once, not per seat", "❌ tokens" in out and "❌ read_token" in out, True)

    install_doctor_fixture()
    edit_loop(read_token="who-is-that")
    rc, out = run_doctor("--loop", "widgets")
    check("a read_token with no file fails", rc, 1)
    check("  and names the login", "❌ read_token" in out and "who-is-that" in out, True)

    install_doctor_fixture()
    cfg = load_loop()
    cfg["tokens"].pop(FIXER)
    save_loop(cfg)
    profile_env("fixer-profile").write_text("DISCORD_BOT_TOKEN=unused\n")
    rc, out = run_doctor("--loop", "widgets")
    check("a seat with no credential at all fails", rc, 1)
    check("  and names the seat", "❌ credential:fixer" in out, True)
    check("  and both places it looked", "GH_TOKEN" in out and FIXER in out, True)
    check("  the reviewer keeps its GH_TOKEN alternative", "✅ credential:reviewer" in out, True)

    section("doctor — profiles, scripts and the cron shim")
    install_doctor_fixture()
    shutil.rmtree(TMP / "hermes-home" / "profiles")
    (TMP / "fix.pat").unlink()
    before_files = tree_digest(TMP)
    rc, out = run_doctor("--loop", "widgets", "--offline")
    check("an offline run finds a missing profile", "❌ profile:reviewer" in out, True)
    check("  and the profile home it looked for", "no profile home at" in out, True)
    check("  and offers the profile command", "hermes profile create reviewer-profile" in out, True)
    check("  and finds the missing token", "❌ token:dev-fixer" in out, True)
    check("  it fails", rc, 1)
    check("  and writes nothing", tree_digest(TMP), before_files)

    install_doctor_fixture()
    (TMP / "hermes-home" / "scripts" / cli.SHIM_NAME).unlink()
    rc, out = run_doctor("--loop", "widgets")
    check("a missing cron shim fails", rc, 1)
    check("  and says how to write it", "❌ cron:shim" in out and "--schedule" in out, True)

    install_doctor_fixture()
    shim = TMP / "hermes-home" / "scripts" / cli.SHIM_NAME
    shim.write_text(shim.read_text().replace(
        str(ROOT / "scripts" / "watchdog.py"), "/opt/old/plugins/hermes-review-loop/scripts/watchdog.py"))
    rc, out = run_doctor("--loop", "widgets")
    check("a shim pinned to a stale plugin path fails", rc, 1)
    check("  and names the expected path", "❌ cron:shim" in out
          and str(ROOT / "scripts" / "watchdog.py") in out, True)
    check("  and explains the mismatch", "differs from init's executable shim" in out, True)

    install_doctor_fixture()
    (TMP / "hermes-home" / "scripts" / cli.SHIM_NAME).write_text("#!/usr/bin/env python3\n")
    rc, out = run_doctor("--loop", "widgets")
    check("a shim that runs nothing fails", rc, 1)
    check("  and says so", "❌ cron:shim" in out and "differs from init" in out, True)

    install_doctor_fixture()
    shim = TMP / "hermes-home" / "scripts" / cli.SHIM_NAME
    shim.write_text("# WATCHDOG = pathlib.Path(" + repr(str(ROOT / "scripts" / "watchdog.py"))
                    + ")\nprint('inert shim')\n")
    rc, out = run_doctor("--loop", "widgets")
    check("a shim with only matching text fails", rc, 1)
    check("  executable content is checked", "❌ cron:shim" in out, True)

    # Exercise the actual init template as a subprocess, but point it at a harmless
    # scratch watchdog. This proves that the exact bytes the doctor accepts do forward.
    fake_watchdog = TMP / "mock-watchdog.py"
    fake_watchdog.write_text("import sys\nprint('MOCK_WATCHDOG ' + ' '.join(sys.argv[1:]))\n")
    shim.write_text(cli.SHIM.format(watchdog=fake_watchdog))
    proc = subprocess.run([sys.executable, str(shim), "--mock-probe"],
                          capture_output=True, text=True, check=False)
    check("init's accepted shim executes mock watchdog", (proc.returncode, proc.stdout.strip()),
          (0, "MOCK_WATCHDOG --mock-probe"))

    real_scripts_dir = doctor.scripts_dir
    doctor.scripts_dir = lambda: TMP / "no-scripts"
    try:
        install_doctor_fixture()
        rc, out = run_doctor("--loop", "widgets")
    finally:
        doctor.scripts_dir = real_scripts_dir
    check("missing plugin scripts fail", rc, 1)
    check("  all four named", "❌ scripts" in out
          and "cleanup.py" in out and "gate_fixer.py" in out, True)
    check("  and the shim is stale against them", "❌ cron:shim" in out, True)
    check("  the plugin's scripts are untouched", (ROOT / "scripts" / "watchdog.py").exists(), True)

    section("doctor — the scheduled job")
    install_doctor_fixture()
    cron_file = TMP / "hermes-home" / "cron" / "jobs.json"
    cron_file.write_text(json.dumps([{"id": "watchdog-job",
                                      "name": cli.watchdog_job_name({"id": "widgets"}),
                                      "script": cli.SHIM_NAME, "enabled": True,
                                      "state": "scheduled", "no_agent": True,
                                      "schedule": {"kind": "interval", "minutes": 15},
                                      "schedule_display": "every 15m", "next_run_at": PAST}]))
    rc, out = run_doctor("--loop", "widgets")
    check("a bare-list job store is still read", rc, 0)
    check("  and the job counts as verified", "✅ cron:job" in out, True)

    # The scheduler's runnable predicate rejects pause markers even if enabled stays true.
    for marker in ({"paused_at": "2026-09-24T10:00:00+00:00"}, {"state": "paused"}):
        install_doctor_fixture()
        cron_file = TMP / "hermes-home" / "cron" / "jobs.json"
        jobs = json.loads(cron_file.read_text())
        jobs["jobs"][0].update(marker)
        cron_file.write_text(json.dumps(jobs))
        rc, out = run_doctor("--loop", "widgets")
        check(f"enabled watchdog with {marker!r} cannot fire", rc, 1)
        check("  reports pause marker rather than a verified wake",
              "❌ cron:job" in out and "✅ cron:job" not in out
              and "hermes cron resume watchdog-job" in out, True)

    install_doctor_fixture()
    cron_file.write_text(json.dumps({"jobs": [{"id": "watchdog-job",
                                               "name": cli.watchdog_job_name({"id": "widgets"}),
                                               "script": cli.SHIM_NAME, "enabled": False,
                                               "state": "paused"}]}))
    rc, out = run_doctor("--loop", "widgets")
    check("a paused watchdog job fails", rc, 1)
    check("  with the command to resume it", "❌ cron:job" in out
          and "hermes cron resume watchdog-job" in out, True)

    for enabled in (None, 0, ""):
        install_doctor_fixture()
        cron_file = TMP / "hermes-home" / "cron" / "jobs.json"
        jobs = json.loads(cron_file.read_text())
        jobs["jobs"][0]["enabled"] = enabled
        cron_file.write_text(json.dumps(jobs))
        rc, out = run_doctor("--loop", "widgets")
        check(f"falsey enabled={enabled!r} cannot verify a wake", rc, 1)
        check("  job is a mismatch, not verified", "❌ cron:job" in out
              and "✅ cron:job" not in out, True)

    install_doctor_fixture()
    cron_file = TMP / "hermes-home" / "cron" / "jobs.json"
    jobs = json.loads(cron_file.read_text())
    jobs["jobs"][0]["state"] = "completed"
    cron_file.write_text(json.dumps(jobs))
    rc, out = run_doctor("--loop", "widgets")
    check("completed watchdog cannot verify a wake", rc, 1)
    check("  terminal state is identified", "❌ cron:job" in out and "completed" in out, True)

    install_doctor_fixture()
    jobs = json.loads(cron_file.read_text())
    jobs["jobs"][0]["schedule"] = {"kind": "cron", "expr": "*/15 * * * *"}
    cron_file.write_text(json.dumps(jobs))
    with mock.patch.dict(sys.modules, {"croniter": None}):
        rc, out = run_doctor("--loop", "widgets")
        strict_rc, strict_out = run_doctor("--loop", "widgets", "--strict")
    check("missing croniter leaves cron expression undecided", rc, 0)
    check("  reports unknown validation, not invalid stored job",
          "⚠️ cron:job" in out and "❌ cron:job" not in out and "croniter" in out, True)
    check("  strict mode fails undecided validation", strict_rc, 1)
    check("  strict mode keeps the unknown label", "⚠️ cron:job" in strict_out, True)

    for next_run in (None, "", "not-a-date"):
        install_doctor_fixture()
        cron_file = TMP / "hermes-home" / "cron" / "jobs.json"
        jobs = json.loads(cron_file.read_text())
        jobs["jobs"][0]["next_run_at"] = next_run
        cron_file.write_text(json.dumps(jobs))
        rc, out = run_doctor("--loop", "widgets")
        check(f"enabled watchdog with next_run_at={next_run!r} fails", rc, 1)
        check("  cannot claim the watchdog will fire", "❌ cron:job" in out
              and "next_run_at" in out, True)
        check("  does not claim the scheduler will never select it",
              "will never select it" in out, False)

    install_doctor_fixture()
    cron_file.write_text(json.dumps({"jobs": []}))
    rc, out = run_doctor("--loop", "widgets")
    check("an unscheduled watchdog fails", rc, 1)
    check("  with the command init runs", "❌ cron:job" in out
          and "hermes cron create 15m" in out, True)

    install_doctor_fixture()
    cron_file.unlink()
    rc, out = run_doctor("--loop", "widgets")
    check("no job store at all fails", rc, 1)
    check("  named by path", "no cron store at" in out, True)

    section("doctor — clone, worktree safety and the gateway")
    install_doctor_fixture()
    edit_loop(clone=str(TMP / "nowhere"))
    rc, out = run_doctor("--loop", "widgets")
    check("a clone that is not there fails", rc, 1)
    check("  and says how to repoint it", "❌ clone" in out
          and "set --loop widgets --clone" in out, True)

    install_doctor_fixture()
    plain = TMP / "not-a-repo"
    shutil.rmtree(plain, ignore_errors=True)
    plain.mkdir()
    edit_loop(clone=str(plain))
    rc, out = run_doctor("--loop", "widgets")
    check("a clone that is not a git checkout fails", rc, 1)
    check("  and says why it matters", "❌ clone" in out and "isolation clones from it" in out, True)

    install_doctor_fixture()
    inside = STATE_DIR / "artifacts" / "9" / "repo"
    (inside / ".git").mkdir(parents=True)
    edit_loop(clone=str(inside))
    rc, out = run_doctor("--loop", "widgets")
    check("a clone inside the artifacts root fails", rc, 1)
    check("  and explains the deletion it would suffer", "❌ clone" in out
          and "would be your working copy" in out, True)

    install_doctor_fixture()
    edit_loop(state_dir=str(WORLD_FILE))
    rc, out = run_doctor("--loop", "widgets")
    check("a state_dir that is a file fails", rc, 1)
    check("  and says so", "❌ state_dir" in out and "is not a directory" in out, True)

    install_doctor_fixture()
    edit_loop(roots=[str(WORLD_FILE)])
    rc, out = run_doctor("--loop", "widgets")
    check("a cleanup root that is a file fails", rc, 1)
    check("  and says so", "❌ roots" in out and "not directories" in out, True)

    install_doctor_fixture()
    edit_loop(roots=[])
    rc, out = run_doctor("--loop", "widgets")
    check("no cleanup roots is not a failure", rc, 0)
    check("  but it says what is lost", "reclaims nothing" in out, True)

    install_doctor_fixture()
    edit_loop(host="")
    rc, out = run_doctor("--loop", "widgets")
    check("no host fails", rc, 1)
    check("  with what to pass", "❌ gateway" in out and "--host" in out, True)

    install_doctor_fixture()
    edit_loop(host="http://127.0.0.1:1")
    rc, out = run_doctor("--loop", "widgets")
    check("a gateway nothing listens at fails", rc, 1)
    check("  and says to start it", "❌ gateway" in out
          and "hermes gateway status" in out, True)

    reachable, detail = doctor.gateway_reachable("http://127.0.0.1:1")
    check("the probe itself refuses a dead port", reachable, False)
    check("  with the address it tried", "127.0.0.1:1" in detail, True)
    reachable, detail = doctor.gateway_reachable(HOST)
    check("and accepts the live gateway", reachable, True)

    section("doctor — unknown is not absent")
    install_doctor_fixture()
    DATA["world"]["hooks"] = None            # an API denial: no list, and no claim about hooks
    save_world()
    rc, out = run_doctor("--loop", "widgets")
    check("a denied hooks read is not a failure", rc, 0)
    check("  reported as unknown", "⚠️ hooks" in out and "could not read" in out, True)
    check("  never as a missing hook", "❌ hook:" in out, False)
    check("  with the permission to fix", "admin:repo_hook" in out, True)

    install_doctor_fixture()
    denial = os.environ["REVIEW_LOOP_GH_STUB"]
    os.environ["REVIEW_LOOP_GH_STUB"] = "/bin/false"
    try:
        rc, out = run_doctor("--loop", "widgets")
    finally:
        os.environ["REVIEW_LOOP_GH_STUB"] = denial
    check("an unreachable GitHub is not a failure either", rc, 0)
    check("  and is also unknown, not absent", "⚠️ hooks" in out and "❌ hook:" in out, False)

    install_doctor_fixture()
    DATA["world"]["hooks"] = []
    save_world()
    rc, out = run_doctor("--loop", "widgets")
    check("a repo with no hooks does fail", rc, 1)
    check("  per hook, with its URL", "❌ hook:widgets-review" in out
          and "no repo hook posts to" in out, True)
    check("  and the admin fix", "admin:repo_hook" in out, True)

    install_doctor_fixture()
    DATA["world"]["hooks"][1]["active"] = False
    save_world()
    rc, out = run_doctor("--loop", "widgets")
    check("a paused hook fails", rc, 1)
    check("  and says how to arm it", "❌ hook:widgets-fix" in out
          and "arm --loop widgets" in out, True)

    install_doctor_fixture()
    DATA["world"]["hooks"][0]["config"]["url"] = (
        f"{HOST}/p/reviewer-profile/webhooks/widgets-review".replace(HOST, "https://old.example"))
    save_world()
    rc, out = run_doctor("--loop", "widgets")
    check("a hook pointing at another gateway fails", rc, 1)
    check("  and redacts both URLs", "not [webhook URL redacted]" in out
          and "https://old.example" not in out, True)

    install_doctor_fixture()
    DATA["world"]["hooks"][0]["events"] = ["push"]
    save_world()
    rc, out = run_doctor("--loop", "widgets")
    check("a hook that never delivers this event fails", rc, 1)
    check("  and names the event", "❌ hook:widgets-review" in out and "pull_request" in out, True)

    for content_type in ("form", None):
        install_doctor_fixture()
        config = DATA["world"]["hooks"][0]["config"]
        if content_type is None:
            config.pop("content_type")
        else:
            config["content_type"] = content_type
        save_world()
        rc, out = run_doctor("--loop", "widgets")
        check(f"{content_type!r} hook content type fails", rc, 1)
        check("  reviewer hook is not verified", "❌ hook:widgets-review" in out
              and "✅ hook:widgets-fix" in out, True)
        check("  JSON requirement is explicit", "content_type" in out and "json" in out, True)

    section("doctor — adversarial preflight")
    install_doctor_fixture()
    sentinel = "DOCTOR_SECRET_SENTINEL"
    cfg = load_loop()
    edit_subs(lambda subs: subs["widgets-review"].update(
        host=f"https://user:{sentinel}@old.example/{sentinel}?key={sentinel}"))
    DATA["world"]["hooks"][0]["config"]["url"] = (
        f"https://user:{sentinel}@old.example/{sentinel}/webhooks/widgets-review?key={sentinel}")
    save_world()
    rc, out = run_doctor("--loop", "widgets", "--offline")
    check("secret-bearing URL fails safely", rc, 1)
    check("secret URL never appears in detail or remediation", sentinel in out, False)

    install_doctor_fixture()
    sentinel = "SECOND_SENTINEL"
    edit_subs(lambda subs: subs["widgets-review"].update(
        host=f"https://old.example/?token='{sentinel}"))
    DATA["world"]["hooks"][0]["config"]["url"] = (
        f"https://old.example/p/reviewer-profile/webhooks/widgets-review?token='{sentinel}")
    save_world()
    rc, out = run_doctor("--loop", "widgets", "--offline")
    check("apostrophe-bearing URL does not leak", sentinel in out, False)
    rc, out = run_doctor("--loop", "widgets")
    check("apostrophe-bearing URL stays hidden during hooks read", sentinel in out, False)

    install_doctor_fixture()
    cfg = load_loop()
    cfg["tokens"].pop(FIXER)
    save_loop(cfg)  # GH_TOKEN remains present in the fixer's .env
    rc, out = run_doctor("--loop", "widgets", "--offline")
    check("unmapped fixer fails despite GH_TOKEN", rc, 1)
    check("  mapped file requirement explicit", "❌ credential:fixer" in out
          and "profile environment alone" in out, True)

    install_doctor_fixture()
    edit_subs(lambda subs: subs["widgets-breach"].update(script="gate_reviewer.py"))
    rc, out = run_doctor("--loop", "widgets")
    check("wrong adjudicator gate fails", rc, 1)
    check("  requires gate_adjudicator.py", "gate_adjudicator.py" in out, True)
    # A pre-#21 loop: `init` refuses an existing loop, so the hint must name apply, and apply
    # must actually rebind the route it names.
    check("  and names the command that repairs it",
          "hermes review-loop apply --loop widgets" in out and "re-run init" not in out, True)
    from review_loop import prompts as prompts_mod
    edit_subs(lambda subs: subs["widgets-breach"].update(prompt=prompts_mod.ADJUDICATOR))
    secret = json.loads(SUBS.read_text())["widgets-breach"]["secret"]
    rc, out = run_cli(parser_for({}).parse_args(["apply", "--loop", "widgets", "--dry-run"]))
    check("apply --dry-run shows the legacy gate repair",
          (rc, "script gate_reviewer.py → gate_adjudicator.py" in out), (0, True))
    rc, out = run_cli(parser_for({}).parse_args(["apply", "--loop", "widgets"]))
    breach = json.loads(SUBS.read_text())["widgets-breach"]
    check("apply rebinds a legacy breach route", (rc, breach["script"]), (0, "gate_adjudicator.py"))
    check("  keeping its secret", breach["secret"], secret)
    check("  and doctor is satisfied", "❌ route:widgets-breach" in run_doctor("--loop", "widgets")[1],
          False)
    check("  a second apply is a no-op", "already matches" in
          run_cli(parser_for({}).parse_args(["apply", "--loop", "widgets"]))[1], True)
    edit_subs(lambda subs: subs["widgets-breach"].update(script="someone_elses.py"))
    run_cli(parser_for({}).parse_args(["apply", "--loop", "widgets"]))
    check("apply never rebinds a foreign script",
          json.loads(SUBS.read_text())["widgets-breach"]["script"], "someone_elses.py")

    install_doctor_fixture()
    profile_env("reviewer-profile").write_text("GH_TOKEN=  # unset\n")
    cfg = load_loop()
    cfg["tokens"].pop(REVIEWER)
    save_loop(cfg)
    rc, out = run_doctor("--loop", "widgets")
    check("empty GH_TOKEN is not a credential", "❌ credential:reviewer" in out, True)

    install_doctor_fixture()
    cfg = load_loop()
    cfg["seats"]["reviewer"]["login"] = "someone-else"
    save_loop(cfg)
    rc, out = run_doctor("--loop", "widgets")
    check("unmapped identity not claimed verified", "✅ credential:reviewer" in out, False)

    install_doctor_fixture()
    job_file = TMP / "hermes-home" / "cron" / "jobs.json"
    jobs = json.loads(job_file.read_text())
    jobs["jobs"][0]["script"] = "another-task.py"
    job_file.write_text(json.dumps(jobs))
    rc, out = run_doctor("--loop", "widgets")
    check("named cron job with wrong script fails", "❌ cron:job" in out, True)

    install_doctor_fixture()
    jobs = json.loads(job_file.read_text())
    jobs["jobs"][0]["schedule"] = {"kind": "interval", "minutes": 0}
    job_file.write_text(json.dumps(jobs))
    rc, out = run_doctor("--loop", "widgets")
    check("invalid stored schedule fails", "❌ cron:job" in out, True)

    # Match Hermes' persisted schedule shape without requiring Hermes as a test dependency.
    install_doctor_fixture()
    jobs = json.loads(job_file.read_text())
    jobs["jobs"][0]["schedule"] = {"kind": "interval", "minutes": 15, "display": "every 15m"}
    job_file.write_text(json.dumps(jobs))
    rc, out = run_doctor("--loop", "widgets")
    check("real Hermes 15m interval passes", rc, 0)

    install_doctor_fixture()
    jobs = json.loads(job_file.read_text())
    jobs["jobs"][0]["schedule"] = {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"}
    job_file.write_text(json.dumps(jobs))
    rc, out = run_doctor("--loop", "widgets")
    # A standalone checkout deliberately has no croniter; installed Hermes does.
    import importlib.util
    if importlib.util.find_spec("croniter") is None:
        check("cron schedule is undecided without croniter", rc == 0
              and "⚠️ cron:job" in out and "❌ cron:job" not in out, True)
    else:
        check("real Hermes cron schedule passes", rc, 0)

    install_doctor_fixture()
    jobs = json.loads(job_file.read_text())
    jobs["jobs"][0]["schedule"] = {"kind": "cron", "expr": "61 25 * * *"}
    job_file.write_text(json.dumps(jobs))
    rc, out = run_doctor("--loop", "widgets")
    if importlib.util.find_spec("croniter") is None:
        check("invalid cron expression cannot be diagnosed without validator",
              "⚠️ cron:job" in out and "❌ cron:job" not in out, True)
    else:
        check("invalid cron expression fails", "❌ cron:job" in out, True)

    install_doctor_fixture()
    jobs = json.loads(job_file.read_text())
    jobs["jobs"][0]["schedule"] = {"kind": "at", "at_ms": 1800000000000}
    job_file.write_text(json.dumps(jobs))
    rc, out = run_doctor("--loop", "widgets")
    check("one-shot watchdog is not recurring", "❌ cron:job" in out, True)

    install_doctor_fixture()
    DATA["world"]["hooks"] = ([{"id": n, "active": True, "events": ["push"],
        "config": {"url": f"https://other.example/hooks/{n}"}} for n in range(100)]
        + DATA["world"]["hooks"])
    save_world()
    rc, out = run_doctor("--loop", "widgets")
    check("hooks on second page verified", "✅ hook:widgets-review" in out and
          "✅ hook:widgets-fix" in out, True)

    install_doctor_fixture()
    DATA["world"]["hooks"] = ([{"id": n, "active": True, "events": ["push"],
        "config": {"url": f"https://other.example/hooks/{n}"}} for n in range(100)]
        + DATA["world"]["hooks"])
    save_world()
    from review_loop import gh
    original_api = gh.api
    gh.api = lambda loop, path, **kw: None if "page=2" in path else original_api(loop, path, **kw)
    try:
        rc, out = run_doctor("--loop", "widgets")
    finally:
        gh.api = original_api
    check("failed second page means unknown, never absent", "⚠️ hooks" in out and
          "❌ hook:" not in out, True)

    for malformed in ({"events": 42}, {"events": [42]}, {"active": 42},
                      {"config": {"url": 42}}, {"config": 42}):
        install_doctor_fixture()
        DATA["world"]["hooks"][0].update(malformed)
        save_world()
        rc, out = run_doctor("--loop", "widgets")
        check(f"malformed hook {malformed} fails closed/unknown", "⚠️ hooks" in out
              and "✅ hook:" not in out and "❌ hook:" not in out, True)

    install_doctor_fixture()
    DATA["world"]["hooks"][0]["config"]["url"] += "-impostor"
    save_world()
    rc, out = run_doctor("--loop", "widgets")
    check("route-name suffix impostor not verified", "✅ hook:widgets-review" in out, False)

    section("doctor — reading it")
    install_doctor_fixture()
    rc, out = run_doctor()
    check("without --loop it preflights every configured loop", rc, 0)
    check("  naming the loop", "[widgets] acme/widgets" in out, True)

    install_doctor_fixture()
    added = doctor_runtime_fixture()
    rc, out = run_doctor("--loop", "widgets", "--offline")
    for path in added:
        path.unlink()
    check("--offline leaves two checks undecided", rc, 0)
    check("  and counts them", "0 failed, 2 unknown" in out, True)
    check("  the gateway is not probed", "⚠️ gateway" in out and "not probed" in out, True)
    check("  nor the hooks", "⚠️ hooks" in out, True)
    check("  and it says what to do about it", "verify the ⚠️ lines by hand" in out, True)

    rc, out = run_doctor("--loop", "widgets", "--offline", "--strict")
    check("--strict fails on an undecided check", rc, 1)
    check("  and says why", "--strict" in out, True)

    install_doctor_fixture()
    for path in LOOPS_DIR.glob("*.json"):
        path.unlink()
    rc, out = run_doctor()
    check("no loops configured is not an error", (rc, out.strip()),
          (0, f"no loops configured in {LOOPS_DIR}"))
    rc, out = run_doctor("--loop", "nope")
    check("an unknown loop is refused", rc, 2)
    check("  with the reason", "cannot preflight loop" in out, True)


GROUPS = {
    "doctor": group_doctor,
}
