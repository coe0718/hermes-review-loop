"""Seat reconciliation, concurrent state writes and open-PR paging."""

from __future__ import annotations

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


GROUPS = {
    "reconciliation": group_reconciliation,
    "state_race": group_state_race,
    "open_prs": group_open_prs,
}
