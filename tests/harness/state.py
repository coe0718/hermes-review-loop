"""Seat reconciliation, concurrent state writes, open-PR paging, and one malformed lock mark."""

from __future__ import annotations

import fcntl

from .fixture import *  # noqa: F403 - the shared harness namespace


STATE_RACE_CHILD = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["ROOT"])
from review_loop import config, gate, isolation, state
loop = config.load_id("widgets")
st = state.state_for(loop)
mode, who = sys.argv[1], int(sys.argv[2])
if mode == "acquire":
    for i in range(50):
        st.acquire("reviewer", f"acme/widgets#{who * 1000 + i}", "h", "race")
        st.queue_add("fixer", f"acme/widgets#{who * 1000 + i}", "h", "u", "race")
        st.inflight(f"review:{who * 1000 + i}:h", record=True)
else:
    # Widen the window between the capacity read and the claim, as a slow disk would.
    read = gate.seat_capacity
    def slow(*a, **k):
        out = read(*a, **k)
        time.sleep(0.2)
        return out
    gate.seat_capacity = slow
    isolation.ensure = lambda *a, **k: None
    try:
        gate.take_seat(loop, st, "reviewer", 100 + who, "h", "race")
        print("CLAIMED")
    except SystemExit:
        print("QUEUED")
"""


def group_state_race() -> None:
    section("state — concurrent gates never lose each other's entries")
    reset(prs={})
    script = TMP / "state_race_child.py"
    script.write_text(STATE_RACE_CHILD)
    child_env = {**env(), "ROOT": str(ROOT)}

    def spawn(mode: str, n: int) -> list[str]:
        procs = [subprocess.Popen([sys.executable, str(script), mode, str(i)], env=child_env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(n)]
        return [proc.communicate(timeout=180)[0].strip().rsplit("\n", 1)[-1] for proc in procs]

    spawn("acquire", 4)
    check("4 procs x 50 acquires keep every seat lock",
          len(load_state("locks.json").get("reviewer") or {}), 200)
    check("  and every queue entry", len(load_state("pending.json").get("fixer") or {}), 200)
    check("  and every in-flight mark", len(load_state("inflight.json")), 200)
    check("  and no temp file is left behind",
          sorted(p.name for p in STATE_DIR.iterdir() if p.name.startswith(".")), [])

    for name in ("locks.json", "pending.json", "inflight.json"):
        state_file(name).unlink()
    outcomes = spawn("claim", 4)
    check("concurrent take_seat at capacity 1 claims once", outcomes.count("CLAIMED"), 1)
    check("  the rest are queued", outcomes.count("QUEUED"), 3)
    check("  and the ledger holds one run", len(load_state("locks.json").get("reviewer") or {}), 1)


def group_open_prs() -> None:
    section("open PR listing — every page, or unknown")
    from review_loop import gh

    loop = {"repo": REPO}
    path = f"/repos/{REPO}/pulls?state=open&per_page=100"
    first = [pr(n) for n in range(1, 101)]
    second = [pr(n) for n in range(101, 151)]
    with mock.patch.object(gh, "fetch", side_effect=[(first, ""), (second, "")]) as fetch:
        listed = gh.open_prs(loop)
    check("a second page is read", [c.args[1] for c in fetch.call_args_list],
          [path, path + "&page=2"])
    check("  and every open PR is listed", len(listed or []), 150)
    for label, page in (("failed", (None, "HTTP 502")), ("malformed", ({"message": "x"}, "")),
                        ("item-less", ([None], ""))):
        with mock.patch.object(gh, "fetch", side_effect=[(first, ""), page]):
            check(f"a {label} later page makes the listing unknown",
                  gh.open_prs(loop) is None, True)
    with (mock.patch.object(gh, "MAX_PR_PAGES", 3),
          mock.patch.object(gh, "fetch", return_value=(first, "")) as fetch):
        check("an endless full listing is unknown, not truncated",
              gh.open_prs(loop) is None, True)
        check("  and the read is bounded", fetch.call_count, 3)


def group_reconciliation() -> None:
    section("seat reconciliation — route, hook and config rollback")
    test = subprocess.run([sys.executable, str(ROOT / "tests" / "test_reconciliation.py")],
                          capture_output=True, text=True)
    if test.returncode:
        print(test.stdout + test.stderr)
    check("route and hook reconciliation regression suite", test.returncode, 0)


def group_situation_authorization() -> None:
    section("stacked situation authorization — read-only fail closed")
    test = subprocess.run([sys.executable, "-m", "unittest",
                           "tests.test_situation_authorization"],
                          cwd=ROOT, capture_output=True, text=True)
    if test.returncode:
        print(test.stdout + test.stderr)
    check("generation-bound parent approval fail-closed suite", test.returncode, 0)


def group_malformed_marks() -> None:
    """One unreadable mark in ``locks.json`` must not take every reader of the ledger down.

    ``live_locks``/``active`` are what the gate's capacity check, ``explain`` and the watchdog's
    drain all read, so a single mark whose ``at`` is not a number — a hand edit, an older writer,
    a truncated file — used to raise ``TypeError`` out of ``watchdog --drain`` (stopping the queue
    for *every* loop) and out of ``explain`` instead of an answer. The reading rule here is the one
    the rest of the state files already follow: a mark that cannot be read degrades to expired and
    the next write drops it, while a well-formed ledger keeps its exact TTL semantics.
    """
    section("state — a malformed lock mark degrades, it never raises")

    from review_loop import config, gate, state as state_mod

    reset(prs={})
    loop = config.load_id("widgets")
    st = state_mod.state_for(loop)
    key = f"{REPO}#7"

    def outcome(fn):
        """The call's value, or the exception that escaped it — the ``got`` of a no-raise check."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - "must not raise" is the property under test
            return f"{type(exc).__name__}: {exc}"

    def ledger(marks, seat: str = "reviewer") -> None:
        """Write exactly ``marks`` for one seat, the way a hand edit or an older writer would."""
        state_file("locks.json").write_text(json.dumps({seat: marks}))

    def malformed() -> None:
        ledger({key: {"at": "2026-01-01T00:00:00Z", "head": HEAD_A, "why": "hand edit"}})

    def locked_read():
        with st.locked():
            return st.is_active("reviewer", key)

    def state_lock_free() -> bool:
        """Nobody holds the loop's state lock (a second description of the same file conflicts)."""
        fd = os.open(st.dir / "state.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    # -- the audited failure: an ``at`` nothing can subtract -----------------------------------
    malformed()
    check("a string 'at' is answered, not subtracted",
          outcome(lambda: st.live_locks("reviewer")), {})
    malformed()
    check("  active() drops it instead of raising", outcome(lambda: st.active("reviewer")), {})
    check("  the drop is written, so the file heals", load_state("locks.json"), {})
    malformed()
    check("  is_active() answers from it", outcome(lambda: st.is_active("reviewer", key)), False)
    malformed()
    check("  held_by_other() answers from it",
          outcome(lambda: st.held_by_other("fixer", key)), None)
    malformed()
    check("  a locked read of it answers too", outcome(locked_read), False)
    check("  and the state lock is released on the way out", state_lock_free(), True)
    malformed()
    facts = {"pr": pr(7), "reviews": [], "armed": True}
    report = outcome(lambda: gate.explain(loop, st, 7, facts))
    check("  explain answers instead of raising",
          isinstance(report, dict) and bool((report.get("next") or {}).get("kind")), True)
    check("  and reports nobody holding the PR",
          report.get("seat") if isinstance(report, dict) else report, "nobody holds it")

    # -- every other shape a bad mark can take ------------------------------------------------
    for label, mark in (("no 'at'", {"head": HEAD_A}), ("a null 'at'", {"at": None}),
                        ("a bool 'at'", {"at": True}), ("a list 'at'", {"at": [1]}),
                        ("a mark that is a string", "junk"), ("a mark that is a list", [1]),
                        ("a mark that is null", None), ("a mark that is a number", 1)):
        ledger({key: mark})
        check(f"{label} reads as an expired mark", outcome(lambda: st.live_locks("reviewer")), {})
        ledger({key: mark})
        check(f"  {label}: active() answers", outcome(lambda: st.active("reviewer")), {})

    # -- and the shapes the mapping around the marks can take -----------------------------------
    for label, doc in (("a seat value that is a string", {"reviewer": "junk"}),
                       ("a seat value that is a list", {"reviewer": [key]}),
                       ("a seat value that is a number", {"reviewer": 7}),
                       ("a locks file that is a list", [key]),
                       ("a locks file that is a string", "junk")):
        state_file("locks.json").write_text(json.dumps(doc))
        check(f"{label} reads as an empty ledger", outcome(lambda: st.live_locks("reviewer")), {})
    state_file("locks.json").write_text(json.dumps({"reviewer": "junk"}))
    check("  and the next write drops the unreadable seat",
          (outcome(lambda: st.active("reviewer")), load_state("locks.json")), ({}, {}))
    state_file("locks.json").write_text(json.dumps([key]))
    check("  and the next write heals a ledger that is not a mapping",
          (outcome(lambda: st.active("reviewer")), load_state("locks.json")), ({}, {}))

    # ``explain``'s read must not be what heals the file: only the writers do that.
    ledger({key: {"at": time.time(), "head": HEAD_A}})
    before = state_file("locks.json").read_text()
    st.live_locks("reviewer")
    check("live_locks stays read-only (the ledger on disk is untouched)",
          state_file("locks.json").read_text(), before)

    # -- a well-formed ledger keeps exactly the semantics it had --------------------------------
    live_at = time.time() - (loop["ttl_min"] * 60 - 60)
    dead_at = time.time() - (loop["ttl_min"] * 60 + 60)
    fresh = {"at": live_at, "head": HEAD_A, "why": "live"}
    state_file("locks.json").write_text(json.dumps({"reviewer": {
        key: fresh, f"{REPO}#8": {"at": dead_at, "head": HEAD_B, "why": "expired"},
        f"{REPO}#9": {"at": 0, "head": HEAD_A, "why": "no clock"}}}))
    check("a mark inside the ttl still holds", sorted(st.live_locks("reviewer")), [key])
    check("  returned unchanged, exactly as written", st.live_locks("reviewer").get(key), fresh)
    check("  a mark past it does not", st.is_active("reviewer", f"{REPO}#8"), False)
    check("  a zero clock is expired, as it always was",
          st.is_active("reviewer", f"{REPO}#9"), False)
    check("  is_active sees the live mark", st.is_active("reviewer", key), True)
    check("  held_by_other names the holding seat", st.held_by_other("fixer", key), "reviewer")
    check("  a seat is never its own other holder", st.held_by_other("reviewer", key), None)
    check("  active() prunes to the live set", sorted(st.active("reviewer")), [key])
    check("  and persists exactly that", load_state("locks.json"), {"reviewer": {key: fresh}})
    # A timestamp stored as a string is expired, never guessed at: readers that take our live set
    # age it by unguarded subtraction (the watchdog's capacity line, cli's status), so only a mark
    # we can subtract ourselves is a mark we may call live.
    state_file("locks.json").write_text(json.dumps({"reviewer": {key: {"at": repr(live_at)}}}))
    check("a timestamp stored as a string is expired, not guessed at",
          outcome(lambda: st.live_locks("reviewer")), {})

    # -- the ttl is the ledger's own knob, and a bad one is not the reader's problem -------------
    missing = object()
    for label, ttl in (("missing", missing), ("string", "45m"), ("null", None), ("list", [45])):
        loop_ttl = {k: v for k, v in loop.items() if k != "ttl_min"}
        if ttl is not missing:
            loop_ttl["ttl_min"] = ttl
        broken = state_mod.state_for(loop_ttl)
        state_file("locks.json").write_text(json.dumps({"reviewer": {key: {"at": time.time() - 60,
                                                                          "head": HEAD_A}}}))
        check(f"a {label} ttl_min falls back to the default ttl",
              sorted(outcome(lambda: broken.live_locks("reviewer"))), [key])


GROUPS = {
    "reconciliation": group_reconciliation,
    "state_race": group_state_race,
    "open_prs": group_open_prs,
    "malformed_marks": group_malformed_marks,
}
