#!/usr/bin/env python3
"""The loop's proof: every gate branch, the watchdog's four stall shapes, and the cleanup rails.

Runs with a plain interpreter and no network — no ``gh``, no pytest, no GitHub:

* GitHub is a stub executable (``REVIEW_LOOP_GH_STUB``) answering from a JSON "world" file, so
  the tests can put a fixer, a verdict and a review request exactly where they want them;
* the webhook endpoint is a real local HTTP server, so the wake path is exercised end to end,
  signature included — a drain that "fires" into a stub would prove nothing about the POST;
* git is real: the cleanup tests build a throwaway clone with detached review worktrees and a
  branch worktree, because the difference between those two is the whole safety story.

    python3 tests/run_tests.py            # all of it
    python3 tests/run_tests.py cleanup    # one group
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / "tests" / ".tmp"
LOOPS_DIR = TMP / "loops"
STATE_DIR = TMP / "state"
REVIEWS = TMP / "reviews"
SCRATCH = TMP / "scratch"
CLONE = TMP / "clone"
SUBS = TMP / "webhook_subscriptions.json"
WORLD_FILE = TMP / "world.json"
STUB = TMP / "gh_stub.py"

REPO = "acme/widgets"
FIXER, REVIEWER, SEAT = "dev-fixer", "rev-coach", "rev-seat"
HEAD_A, HEAD_B = "a" * 40, "b" * 40
HOST = "http://127.0.0.1:0"          # rewritten with the real port at start-up

RECEIVED: list[dict] = []
PAST = "2026-01-01T00:00:00Z"

results: list[tuple[bool, str]] = []


def check(name: str, got, want) -> None:
    ok = got == want
    results.append((ok, name))
    print(f" {'✅' if ok else '❌'} {name:52s} want={str(want):22s} got={got}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# -- the fake world --------------------------------------------------------------


def world(prs: dict | None = None, hooks_active: bool = True) -> dict:
    routes = ("widgets-review", "widgets-fix")
    return {
        "prs": prs or {},
        "hooks": [{"id": n, "active": hooks_active,
                   "config": {"url": f"http://127.0.0.1:9/p/seat/webhooks/{name}"}}
                  for n, name in enumerate(routes, 1)],
        "requested_reviewers": [],
    }


def pr(number: int, head: str = HEAD_A, state: str = "open", draft: bool = False,
       author: str = FIXER, base: str = "main", title: str = "a change",
       merged: str | None = None) -> dict:
    return {"number": number, "state": state, "draft": draft, "merged_at": merged,
            "title": title, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "base": {"ref": base}, "user": {"login": author},
            "head": {"sha": head, "ref": "fix-thing"}}


def review(login: str, state: str = "changes_requested", head: str = HEAD_A, rid: int = 1) -> dict:
    return {"id": rid, "state": state, "commit_id": head, "user": {"login": login},
            "submitted_at": PAST, "body": "looks off"}


STUB_SRC = '''#!/usr/bin/env python3
"""Answers GitHub REST paths from a JSON world. Unknown paths print null (= unknown)."""
import json, os, re, sys

path = sys.argv[1]
world = json.loads(open(os.environ["GH_WORLD"]).read())
method = os.environ.get("GH_METHOD", "GET")
repo = world.get("repo")
body = sys.argv[2] if len(sys.argv) > 2 else ""

def n_of(p):
    m = re.search(r"/pulls/(\\d+)", p)
    return int(m.group(1)) if m else None

if path.endswith("/hooks?per_page=100"):
    print(json.dumps(world.get("hooks", [])))
elif "/requested_reviewers" in path:
    world.setdefault("requested_reviewers", []).append([n_of(path), json.loads(body or "{}")])
    open(os.environ["GH_WORLD"], "w").write(json.dumps(world))
    print("{}")
elif path.endswith("/reviews?per_page=100"):
    print(json.dumps((world["prs"].get(str(n_of(path))) or {}).get("reviews", [])))
elif "/commits/" in path:
    sha = path.rsplit("/", 1)[1]
    print(json.dumps({"commit": {"committer": {"date": world.get("commit_dates", {}).get(sha, "2026-01-01T00:00:00Z")}}}))
elif "/pulls?" in path:
    print(json.dumps([p for p in world["prs"].values() if p.get("state") == "open"]))
elif n_of(path) is not None:
    entry = world["prs"].get(str(n_of(path)))
    print(json.dumps(entry) if entry else "null")
else:
    print("null")
'''


class Sink(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        RECEIVED.append({"path": self.path, "event": self.headers.get("X-GitHub-Event"),
                         "sig": self.headers.get("X-Hub-Signature-256", ""),
                         "delivery": self.headers.get("X-GitHub-Delivery", ""),
                         "body": body.decode()})
        self.send_response(202)
        self.end_headers()

    def log_message(self, fmt, *args) -> None:  # noqa: A003 - stdlib signature
        pass


def start_sink() -> str:
    server = HTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


# -- fixtures --------------------------------------------------------------------


def make_clone() -> None:
    """A real repository with real worktrees: two detached (reviews) and one on a branch."""
    CLONE.mkdir(parents=True, exist_ok=True)

    def git(*args, cwd=CLONE):
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (CLONE / "README.md").write_text("hi\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    # a detached review checkout for PR 7, with a build dir, a log, and an evidence dir
    wt = REVIEWS / "pr7-wt"
    git("worktree", "add", "--detach", str(wt), "HEAD")
    (wt / "target").mkdir()
    (wt / "target" / "big.bin").write_bytes(b"x" * 4096)
    (REVIEWS / "pr7-build.log").write_text("log\n")
    (REVIEWS / "pr7-phase3-evidence").mkdir()
    (REVIEWS / "pr7-phase3-evidence" / "keep.json").write_text("{}\n")
    # a detached review checkout for PR 9 (still open) and a branch worktree for PR 8
    git("worktree", "add", "--detach", str(REVIEWS / "pr9-wt"), "HEAD")
    (REVIEWS / "pr9-wt" / "notes.txt").write_text("keep me\n")
    (SCRATCH / "pr8-target").mkdir(parents=True)
    # A build-output file the cleanup should reclaim. Text, and named like the real artifacts
    # (.log), because a stray .bin here trips the plugin security scan's binary-file caution every
    # time anyone runs `hermes plugins validate` on this repo.
    (SCRATCH / "pr8-target" / "build.log").write_text("y" * 2048 + "\n")
    branch_wt = SCRATCH / "pr8-work-branch"
    git("worktree", "add", "-b", "fix/thing-8", str(branch_wt), "HEAD")
    (branch_wt / "work.txt").write_text("someone's work\n")
    # a file that belongs to no PR at all
    (SCRATCH / "unrelated.log").write_text("hands off\n")


def write_loop() -> dict:
    LOOPS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {
        "id": "widgets", "repo": REPO, "base": "main", "cap": 3,
        "fixers": [FIXER], "reviewers": [REVIEWER], "reviewer_seat": SEAT,
        "seats": {
            "reviewer": {"profile": "reviewer-profile", "route": "widgets-review",
                         "login": SEAT, "agent": "Rex"},
            "fixer": {"profile": "fixer-profile", "route": "widgets-fix",
                      "login": FIXER, "agent": "Dee"},
        },
        "adjudicator": {"route": "widgets-breach", "profile": "default"},
        "state_dir": str(STATE_DIR), "clone": str(CLONE),
        "roots": [str(REVIEWS), str(SCRATCH)],
        "tokens": {REVIEWER: str(TMP / "rev.pat"), FIXER: str(TMP / "fix.pat")},
        "read_token": REVIEWER,
        "host": HOST,
        "grace_min": 25, "marker_grace_min": 60, "cooldown_h": 6,
        "ttl_min": 45, "inflight_ttl_min": 10,
    }
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg, indent=2))
    (TMP / "rev.pat").write_text("token-reviewer\n")
    (TMP / "fix.pat").write_text("token-fixer\n")
    return cfg


def write_subs() -> dict:
    subs = {}
    for name, profile, script, events in (
        ("widgets-review", "reviewer-profile", "gate_reviewer.py", ["pull_request"]),
        ("widgets-fix", "fixer-profile", "gate_fixer.py", ["pull_request_review"]),
        ("widgets-breach", "default", None, ["pull_request"]),
    ):
        subs[name] = {"description": name, "events": events, "secret": hashlib.sha256(name.encode()).hexdigest(),
                      "prompt": "{_loop}", "skills": [], "deliver": "discord", "profile": profile,
                      "created_at": PAST, "script": script, "host": HOST}
    SUBS.write_text(json.dumps(subs, indent=2))
    return subs


def reset(hooks_active: bool = True, prs: dict | None = None) -> dict:
    for path in (STATE_DIR, REVIEWS, SCRATCH, CLONE, LOOPS_DIR):
        shutil.rmtree(path, ignore_errors=True)
    RECEIVED.clear()
    for path in (SUBS, WORLD_FILE):
        if path.exists():
            path.unlink()
    make_clone()
    cfg = write_loop()
    subs = write_subs()
    DATA["world"] = world(prs or {}, hooks_active)
    WORLD_FILE.write_text(json.dumps(DATA["world"]))
    STUB.write_text(STUB_SRC)
    os.chmod(STUB, 0o755)
    return cfg


def save_world() -> None:
    WORLD_FILE.write_text(json.dumps(DATA["world"]))


def set_prs(prs: dict) -> None:
    DATA["world"]["prs"] = prs
    save_world()


DATA: dict = {}


def env() -> dict:
    return {**os.environ, "REVIEW_LOOP_CONFIG_DIR": str(LOOPS_DIR),
            "REVIEW_LOOP_SUBS": str(SUBS), "REVIEW_LOOP_GH_STUB": str(STUB),
            "GH_WORLD": str(WORLD_FILE), "REVIEW_LOOP_TEST": "1",
            "HERMES_HOME": str(TMP / "hermes-home")}


def run(script: str, payload: dict | None = None, *args: str,
        extra_env: dict | None = None) -> tuple[str, str, str]:
    cmd = [sys.executable, str(ROOT / "scripts" / script), *args]
    proc = subprocess.run(cmd, input=json.dumps(payload) if payload else None,
                          capture_output=True, text=True,
                          env={**env(), **(extra_env or {})}, timeout=180)
    out, err = proc.stdout.strip(), proc.stderr.strip()
    if out.startswith("[SILENT]"):
        kind = "SILENT"
    elif out.startswith("{"):
        kind = "FIRE"
    else:
        kind = out
    return kind, out, err


# -- payloads --------------------------------------------------------------------


def pr_payload(number: int = 7, head: str = HEAD_A, action: str = "review_requested",
               requested: str = SEAT, sender: str = FIXER, author: str = FIXER,
               draft: bool = False, base: str = "main", merged=None) -> dict:
    return {"repository": {"full_name": REPO}, "action": action, "number": number,
            "requested_reviewer": {"login": requested}, "sender": {"login": sender},
            "pull_request": pr(number, head=head, draft=draft, author=author, base=base,
                               merged=merged)}


def review_payload(number: int = 7, head: str = HEAD_A, state: str = "changes_requested",
                   login: str = REVIEWER, rid: int = 5, commit: str | None = None) -> dict:
    return {"repository": {"full_name": REPO}, "action": "submitted", "number": number,
            "sender": {"login": login},
            "review": review(login, state=state, head=commit or head, rid=rid),
            "pull_request": pr(number, head=head)}


def state_file(name: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / name


def load_state(name: str):
    path = state_file(name)
    return json.loads(path.read_text()) if path.exists() else {}


def ns(**kw):
    """A stand-in for an argparse Namespace, with every flag the commands read."""
    base = dict(loop="widgets", concurrency=None, cap=None, clone=None, base=None,
                grace_min=None, marker_grace_min=None, ttl_min=None,
                inflight_ttl_min=None, host=None, reviewer_concurrency=None,
                fixer_concurrency=None, dry_run=False, seat=None, pr=None)
    base.update(kw)
    return SimpleNamespace(**base)


# == groups ======================================================================

def group_config() -> None:
    section("config")
    from review_loop import config

    loop = config.load_id("widgets")
    check("loop resolves by repo", config.by_repo(REPO)["id"], "widgets")
    check("unknown repo → nothing", config.by_repo("nope/nope"), None)
    check("cap kept", loop["cap"], 3)
    check("artifacts path is per PR", str(config.artifacts_dir(loop, 7)).endswith("artifacts/7"), True)

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
    check("explicit request for this seat fires", run("gate_reviewer.py", pr_payload())[0], "FIRE")

    reset(prs={"7": pr(7)})
    check("request naming another reviewer is silent",
          run("gate_reviewer.py", pr_payload(requested="someone-else"))[0], "SILENT")

    reset(prs={"7": pr(7)})
    check("request from a stranger is silent",
          run("gate_reviewer.py", pr_payload(sender="passer-by"))[0], "SILENT")

    reset(prs={"7": pr(7)})
    check("a plain push (synchronize) is silent",
          run("gate_reviewer.py", pr_payload(action="synchronize"))[0], "SILENT")

    reset(prs={"7": pr(7)})
    check("opened fires immediately", run("gate_reviewer.py", pr_payload(action="opened"))[0], "FIRE")

    reset(prs={"7": pr(7)})
    check("draft is silent", run("gate_reviewer.py", pr_payload(draft=True))[0], "SILENT")

    reset(prs={"7": pr(7)})
    check("wrong base branch is silent",
          run("gate_reviewer.py", pr_payload(base="release"))[0], "SILENT")

    reset(prs={"7": pr(7, author="outsider")})
    check("a stranger's PR is silent",
          run("gate_reviewer.py", pr_payload(author="outsider"))[0], "SILENT")

    reset(prs={"7": pr(7)})
    check("another repository is silent",
          run("gate_reviewer.py", {**pr_payload(), "repository": {"full_name": "other/repo"}})[0],
          "SILENT")

    reset(prs={"7": pr(7)})
    check("an unknown action is silent", run("gate_reviewer.py", pr_payload(action="labeled"))[0],
          "SILENT")

    # a head that already has a verdict from a reviewer
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER)]}})
    check("head already reviewed → silent", run("gate_reviewer.py", pr_payload())[0], "SILENT")

    # an approved head
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, state="approved")]}})
    check("new commit after a verdict fires",
          run("gate_reviewer.py", pr_payload(head=HEAD_B))[0], "FIRE")

    # the review list is unreadable: never guess a round count
    reset(prs={"7": pr(7)})
    kind, _, err = run("gate_reviewer.py", pr_payload(), extra_env={"REVIEW_LOOP_GH_STUB": "/bin/false"})
    check("unreadable review list → silent (never guess)", kind, "SILENT")
    check("  and it says why", "not guessing" in err, True)


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


def group_budget() -> None:
    section("budget — the cap is a wall, not a suggestion")

    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head="c" * 40, rid=1),
                                                       review(REVIEWER, head="d" * 40, rid=2),
                                                       review(REVIEWER, head="e" * 40, rid=3)]}})
    kind, out, err = run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("third verdict spent → no fourth review", kind, "SILENT")
    check("  breach marker written",
          load_state("breach.json").get(f"{REPO}#7", {}).get("status"), "awaiting-adjudication")
    check("  adjudicator was woken", RECEIVED[-1]["path"] if RECEIVED else None,
          "/webhooks/widgets-breach")

    # one wake per head
    before = len(RECEIVED)
    run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("  same head does not re-wake", len(RECEIVED) - before, 0)

    # under the cap: still a normal review
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": [review(REVIEWER, head="c" * 40, rid=1)]}})
    kind, out, _ = run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("one verdict in → second review fires", kind, "FIRE")
    check("  round number is 2", json.loads(out)["_loop"]["round"], 2)

    section("fixer gate — the cap stops the fix, not just the review")
    # the fixer gate counts the OTHER verdicts; 2 prior + this one = the cap → adjudication
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=8),
                                          review(REVIEWER, rid=9),
                                          review(REVIEWER, rid=5)]}})
    kind, out, err = run("gate_fixer.py", review_payload(rid=5))
    check("verdict that hits the cap → no fix run", kind, "SILENT")
    check("  breach marker written",
          load_state("breach.json").get(f"{REPO}#7", {}).get("status"), "awaiting-adjudication")
    check("  adjudicator woken", RECEIVED[-1]["path"] if RECEIVED else None,
          "/webhooks/widgets-breach")

    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    kind, out, _ = run("gate_fixer.py", review_payload(rid=5))
    check("first changes-requested → fix run", kind, "FIRE")
    check("  round number is 1", json.loads(out)["_loop"]["round"], 1)
    check("  verdict carried in _loop", json.loads(out)["_loop"]["verdict"], "changes_requested")


def group_fixer_gate() -> None:
    section("fixer gate — only a verdict it must answer")
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    check("approved → silent",
          run("gate_fixer.py", review_payload(state="approved", rid=5))[0], "SILENT")
    check("commented → silent",
          run("gate_fixer.py", review_payload(state="commented", rid=5))[0], "SILENT")
    check("verdict from a non-reviewer → silent",
          run("gate_fixer.py", review_payload(login="passer-by", rid=5))[0], "SILENT")
    check("verdict on an older head → silent",
          run("gate_fixer.py", review_payload(commit="c" * 40, rid=5))[0], "SILENT")
    check("dismissed review event → silent",
          run("gate_fixer.py", {**review_payload(rid=5), "action": "dismissed"})[0], "SILENT")
    check("uppercase state still accepted (REST spelling)",
          run("gate_fixer.py", review_payload(state="CHANGES_REQUESTED", rid=5))[0], "FIRE")

    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    run("gate_fixer.py", review_payload(rid=5))
    check("same head twice → second is silent",
          run("gate_fixer.py", review_payload(rid=5))[0], "SILENT")


def group_seats() -> None:
    section("seats — one run at a time, and only its own turn")
    reset(prs={"7": pr(7)})
    run("gate_reviewer.py", pr_payload(7))
    check("review starts → reviewer seat locked", "widgets#7" in json.dumps(load_state("locks.json")), True)

    # a second PR arriving while the reviewer works is queued, not run
    set_prs({"7": pr(7), "9": pr(9, head=HEAD_B)})
    kind, _, err = run("gate_reviewer.py", pr_payload(9, head=HEAD_B))
    check("busy seat → queued, silent", kind, "SILENT")
    check("  queue holds PR 9", "9" in load_state("pending.json").get("reviewer", {}).get(f"{REPO}#9", {}).get("url", ""), True)

    # a stale slot frees itself (the ledger is seat → PR key → entry)
    locks = load_state("locks.json")
    locks["reviewer"][f"{REPO}#7"]["at"] = time.time() - 46 * 60
    state_file("locks.json").write_text(json.dumps(locks))
    check("slot older than the TTL → capacity again",
          run("gate_reviewer.py", pr_payload(9, head=HEAD_B))[0], "FIRE")

    # releasing a seat only ever releases ITS OWN turn
    reset(prs={"7": pr(7)})
    state_file("locks.json").write_text(json.dumps(
        {"fixer": {f"{REPO}#999": {"at": time.time(), "head": HEAD_A, "why": "other PR"}}}))
    run("gate_reviewer.py", pr_payload(7))
    check("another PR's fixer slot survives", "999" in state_file("locks.json").read_text(), True)

    # the reviewer slot and the in-flight mark are cleared so the next run reaches the release
    locks = load_state("locks.json")
    locks.pop("reviewer", None)
    locks["fixer"] = {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "why": "this PR"}}
    state_file("locks.json").write_text(json.dumps(locks))
    state_file("inflight.json").write_text("{}")
    run("gate_reviewer.py", pr_payload(7))
    check("this PR's fixer slot is released", "fixer" in load_state("locks.json"), False)

    section("seats — the fixer side releases the reviewer")
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "why": "review"}}}))
    run("gate_fixer.py", review_payload(rid=5))
    check("verdict → reviewer slot released", "reviewer" in load_state("locks.json"), False)


def set_concurrency(value: int) -> None:
    path = LOOPS_DIR / "widgets.json"
    cfg = json.loads(path.read_text())
    cfg["concurrency"] = value
    path.write_text(json.dumps(cfg))


def real_head() -> str:
    return subprocess.run(["git", "-C", str(CLONE), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def group_parallel() -> None:
    section("parallel — own clone per PR, N at a time")
    reset(prs={"7": pr(7), "9": pr(9)})
    set_concurrency(2)
    head = real_head()          # isolation checks out a real commit, so the sha must exist
    set_prs({"7": pr(7, head=head), "9": pr(9, head=head), "11": pr(11, head=head)})

    kind, out, err = run("gate_reviewer.py", pr_payload(7, head=head))
    check("concurrency 2 → first PR starts", kind, "FIRE")

    locks = load_state("locks.json").get("reviewer", {})
    check("  its slot is held", f"{REPO}#7" in locks, True)

    payload = json.loads(out)
    iso = payload["_loop"]["isolation"]
    check("  the run is isolated", iso.get("isolated"), True)
    iso_clone = pathlib.Path(iso["clone"])
    check("  own clone, not the shared one", iso_clone != CLONE and str(STATE_DIR) in str(iso_clone), True)
    check("  the clone is a real checkout", (iso_clone / ".git").exists(), True)
    check("  checked out at the head under review",
          subprocess.run(["git", "-C", str(iso_clone), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip(), head)
    check("  build dir is inside the sandbox", iso["env"].startswith("CARGO_TARGET_DIR="), True)
    check("  its own target dir, not a shared one",
          str(STATE_DIR) in iso["target"] and iso["target"].startswith(str(iso_clone.parents[0])), True)
    check("  the prompt says where to work", iso_clone.name in iso["brief"], True)
    check("  no token in the clone's config",
          "token-reviewer" in (iso_clone / ".git" / "config").read_text(), False)

    # a second PR now runs too, in its own sandbox
    kind, out2, _ = run("gate_reviewer.py", pr_payload(9, head=head))
    check("second PR starts in parallel", kind, "FIRE")
    iso2 = json.loads(out2)["_loop"]["isolation"]["clone"]
    check("  different sandbox from the first", iso2 != iso["clone"], True)
    check("  two runs now counted", len(load_state("locks.json").get("reviewer", {})), 2)

    # capacity is the wall: a third waits
    kind, _, _ = run("gate_reviewer.py", pr_payload(11, head=head))
    check("third PR waits for a slot", kind, "SILENT")
    check("  queued, not lost", f"{REPO}#11" in load_state("pending.json").get("reviewer", {}), True)

    # the same PR never gets two runs, even with a free slot
    state_file("inflight.json").write_text("{}")
    kind, _, err = run("gate_reviewer.py", pr_payload(7, head=HEAD_B))
    check("same PR twice → refused", kind, "SILENT")
    check("  and it says why", "already running" in err, True)
    check("  no second slot taken", len(load_state("locks.json").get("reviewer", {})), 2)

    # a finished turn frees a slot, and the queue drains into it (the drain POSTs at the seat's
    # route — the gate then runs there, so the sandbox is that run's business, not the drain's)
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#9": {"at": time.time(), "head": head, "why": "review"}}}))
    before = len(RECEIVED)
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("freed slot → queued PR starts", "PR #11" in out, True)
    check("  the wake really went out", len(RECEIVED) - before, 1)
    check("  and the queue is cleared", load_state("pending.json").get("reviewer", {}), {})

    # at capacity the drain declines rather than overcommitting
    state_file("locks.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#7": {"at": time.time(), "head": head, "why": "review"},
        f"{REPO}#9": {"at": time.time(), "head": head, "why": "review"}}}))
    state_file("pending.json").write_text(json.dumps({"reviewer": {
        f"{REPO}#11": {"at": time.time(), "head": head, "url": "u", "reason": "capacity"}}}))
    before = len(RECEIVED)
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("full seat → drain declines", "at capacity (2/2" in out, True)
    check("  nothing fired", len(RECEIVED) - before, 0)

    # an unisolatable run is queued, never started beside another
    reset(prs={"7": pr(7), "9": pr(9)})
    set_concurrency(2)
    state_file("locks.json").write_text(json.dumps({"reviewer": {}}))
    kind, _, err = run("gate_reviewer.py", pr_payload(9, head=HEAD_B))
    check("no sandbox + concurrency 2 → queued", kind, "SILENT")
    check("  and it says isolation is why", "no isolated workspace" in err, True)
    check("  no slot left held", load_state("locks.json").get("reviewer", {}), {})

    section("parallel — each seat its own number, each seat its own sandbox")
    reset(prs={"7": pr(7), "9": pr(9)})
    set_concurrency(1)                     # loop default stays serialized...
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["seats"]["reviewer"]["concurrency"] = 2      # ...Vex gets 2, Drey keeps 1
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    head = real_head()
    # A verdict at the older head so the fixer has something to answer, plus a newer head so the
    # reviewer still has something to review: the same PR, two different turns.
    subprocess.run(["git", "-C", str(CLONE), "commit", "--allow-empty", "-qm", "second"],
                   capture_output=True, text=True)
    newer = real_head()
    set_prs({"7": {**pr(7, head=newer), "reviews": [review(REVIEWER, head=head, rid=5)]},
             "9": {**pr(9, head=newer), "reviews": [review(REVIEWER, head=head, rid=6)]}})

    kind_r7, out_r7, _ = run("gate_reviewer.py", pr_payload(7, head=newer))
    check("reviewer 2 → first review runs", kind_r7, "FIRE")
    check("reviewer 2 → second review runs too",
          run("gate_reviewer.py", pr_payload(9, head=newer))[0], "FIRE")
    kind_f7, out_f7, _ = run("gate_fixer.py", review_payload(7, head=newer, rid=5))
    check("fixer 1 → its fix runs", kind_f7, "FIRE")
    check("  the fix released that PR's review slot",
          len(load_state("locks.json").get("reviewer", {})), 1)
    kind_f9, _, err_f9 = run("gate_fixer.py", review_payload(9, head=newer, rid=6))
    check("fixer 1 → a second fix queues", kind_f9, "SILENT")
    check("  and it says capacity", "at capacity 1/1" in err_f9, True)

    iso_r = json.loads(out_r7)["_loop"]["isolation"]
    iso_f = json.loads(out_f7)["_loop"]["isolation"]
    check("the two seats never share a sandbox", iso_r["clone"] != iso_f["clone"], True)
    check("  the reviewer's is a reviewer workspace", f"/{7}/reviewer/" in iso_r["clone"], True)
    check("  the fixer's is a fixer workspace", f"/{7}/fixer/" in iso_f["clone"], True)
    check("  both are real clones",
          (pathlib.Path(iso_r["clone"]) / ".git").exists()
          and (pathlib.Path(iso_f["clone"]) / ".git").exists(), True)
    check("  the payload reports its own seat", iso_f["seat"], "fixer")
    check("  at the head under review",
          subprocess.run(["git", "-C", iso_f["clone"], "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip(), newer)


def group_exclusive() -> None:
    section("one seat per PR — Vex never reviews what Drey is fixing")
    reset(prs={"7": pr(7)})
    set_concurrency(2)          # capacity 2, so the only reason to queue is the other seat
    head = real_head()
    set_prs({"7": {**pr(7, head=head), "reviews": [review(REVIEWER, head=head, rid=5)]}})

    check("the fixer takes PR #7 on the verdict",
          run("gate_fixer.py", review_payload(7, head=head, rid=5))[0], "FIRE")
    check("  and holds it", "acme/widgets#7" in load_state("locks.json").get("fixer", {}), True)

    # A push and a review trigger that is *not* a handoff, while the fixer still works the PR.
    subprocess.run(["git", "-C", str(CLONE), "commit", "--allow-empty", "-qm", "pushed"],
                   capture_output=True, text=True)
    newer = real_head()
    set_prs({"7": {**pr(7, head=newer), "reviews": [review(REVIEWER, head=head, rid=5)]}})

    kind, _, err = run("gate_reviewer.py", pr_payload(7, head=newer, action="ready_for_review"))
    check("a non-handoff trigger while the fixer works it → queued", kind, "SILENT")
    check("  and it says why", "the fixer seat is working this PR" in err, True)
    check("  no reviewer slot was taken", load_state("locks.json").get("reviewer", {}), {})
    check("  queued, not dropped",
          "fixer seat is working" in load_state("pending.json")["reviewer"]["acme/widgets#7"]["reason"], True)
    check("  the fixer still holds it", "acme/widgets#7" in load_state("locks.json").get("fixer", {}), True)

    kind, out, _ = run("gate_reviewer.py", pr_payload(7, head=newer, action="review_requested"))
    check("the fixer's request *is* the handoff → review starts", kind, "FIRE")
    check("  and the fixer's slot is freed", "acme/widgets#7" in load_state("locks.json").get("fixer", {}), False)
    check("  the stale queue entry is gone", load_state("pending.json").get("reviewer", {}), {})
    check("  the payload names the newer head", json.loads(out)["_loop"]["head"], newer)

    section("an approval frees the reviewer's slot — the queue keeps moving")
    reset(prs={"7": pr(7), "9": pr(9), "11": pr(11)})
    set_concurrency(2)                     # reviewer capacity 2, fixer capacity 2 (independently)
    head = real_head()
    set_prs({n: pr(n, head=head) for n in (7, 9, 11)})

    check("review #7 starts", run("gate_reviewer.py", pr_payload(7, head=head, action="opened"))[0], "FIRE")
    check("review #9 starts", run("gate_reviewer.py", pr_payload(9, head=head, action="opened"))[0], "FIRE")
    kind, _, err = run("gate_reviewer.py", pr_payload(11, head=head, action="opened"))
    check("review #11 queues (at capacity)", kind, "SILENT")
    check("  it says capacity", "at capacity 2/2" in err, True)
    check("  both slots are held", len(load_state("locks.json").get("reviewer", {})), 2)

    before = len(RECEIVED)
    kind, _, err = run("gate_fixer.py", review_payload(7, head=head, state="approved", rid=9))
    check("an approval wakes no fix run", kind, "SILENT")
    check("  it says the slot was freed", "reviewer's slot freed" in err, True)
    check("  the reviewer is down to one hold", len(load_state("locks.json").get("reviewer", {})), 1)
    check("  the queued PR was picked up", len(RECEIVED) - before, 1)
    check("  and it was #11 that started", json.loads(RECEIVED[-1]["body"])["number"], 11)
    check("  with a valid signature", verify_sig(RECEIVED[-1], "widgets-review"), True)
    check("  the queue is empty again", load_state("pending.json").get("reviewer", {}), {})


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


class FakeCtx:
    """Just enough PluginContext to capture the CLI parser a plugin registers."""

    def __init__(self) -> None:
        self.registered: str | None = None
        self.setup: Any = None
        self.skill = None

    def register_cli_command(self, name, help_text, setup, description="", **kw):  # noqa: ANN001
        self.registered = name
        self.setup = setup

    def register_skill(self, name, path, description="", **kw):  # noqa: ANN001
        self.skill = name


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
                 ["set", "--loop", "widgets", "--cap", "4"],
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

def group_webhook_host() -> None:
    section("webhook host — never borrow another operator's gateway")
    from review_loop import cli, config, gh

    reset(prs={})
    init_args = ["init", "--repo", "acme/host-probe", "--fixer", FIXER,
                 "--reviewer", REVIEWER, "--reviewer-profile", "reviewer-profile",
                 "--fixer-profile", "fixer-profile", "--hooks"]
    calls = []
    original_api = gh.api
    def fake_api(loop, path, **kwargs):
        calls.append((path, kwargs))
        return {"id": len(calls)}
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

        parsed = parser.parse_args(init_args[:-1])
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
    check("queued review starts", "started the queued run" in out, True)
    check("  it asked for the reviewer seat",
          json.loads(RECEIVED[-1]["body"])["requested_reviewer"]["login"], SEAT)
    check("  signature is valid for that route", verify_sig(RECEIVED[-1], "widgets-review"), True)
    check("  queue is now empty", load_state("pending.json"), {})

    # a queued request for a head that was already reviewed dies quietly
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER)]}})
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("already-reviewed queue entry is dropped", len(RECEIVED) - before, 0)
    check("  and removed from the queue", load_state("pending.json"), {})

    # a full seat fires nothing
    reset(prs={"7": pr(7)})
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#9": {"at": time.time(), "head": HEAD_B, "why": "working"}}}))
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "capacity"}}}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("full seat drains nothing", "at capacity (1/1" in out, True)

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


def verify_sig(request: dict, route: str) -> bool:
    subs = json.loads(SUBS.read_text())
    secret = subs[route]["secret"].encode()
    want = "sha256=" + hmac.new(secret, request["body"].encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, request["sig"])


def group_cleanup() -> None:
    section("cleanup — a finished PR gives its disk back, and nothing else")

    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z"), "9": pr(9)})
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--pr", "7", "--dry-run")
    check("dry run reports the worktree", "pr7-wt" in out, True)
    check("  and does NOT delete it", (REVIEWS / "pr7-wt").exists(), True)

    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("real run removes the worktree", (REVIEWS / "pr7-wt").exists(), False)
    check("  removes its build log", (REVIEWS / "pr7-build.log").exists(), False)
    check("  SKIPS the evidence dir", (REVIEWS / "pr7-phase3-evidence").exists(), True)
    check("  another PR's worktree untouched", (REVIEWS / "pr9-wt").exists(), True)
    check("  the branch worktree untouched", (SCRATCH / "pr8-work-branch").exists(), True)
    check("  unrelated files untouched", (SCRATCH / "unrelated.log").exists(), True)
    check("  worktree registration pruned",
          "pr7-wt" in subprocess.run(["git", "-C", str(CLONE), "worktree", "list"],
                                     capture_output=True, text=True).stdout, False)

    # state is cleared for that PR
    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z")})
    state_file("locks.json").write_text(json.dumps({"reviewer": {"at": time.time(), "key": f"{REPO}#7"}}))
    state_file("breach.json").write_text(json.dumps({f"{REPO}#7": {"status": "awaiting-adjudication"}}))
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("locks cleared for the PR", load_state("locks.json"), {})
    check("breach marker cleared", load_state("breach.json"), {})

    # an open PR is refused without --force
    reset(prs={"7": pr(7, state="open")})
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("open PR is refused", "still open" in out, True)
    check("  and its files stay", (REVIEWS / "pr7-wt").exists(), True)

    # the sweep walks what is closed and leaves what is open
    reset(prs={"7": pr(7, state="closed", merged="2026-02-02T00:00:00Z"),
               "8": pr(8), "9": pr(9)})
    out, _, _ = run("cleanup.py", None, "--loop", "widgets", "--sweep")
    check("sweep reclaims the closed PR", (REVIEWS / "pr7-wt").exists(), False)
    check("sweep keeps the open one", (REVIEWS / "pr9-wt").exists(), True)
    check("sweep keeps the unknown one", (SCRATCH / "pr8-target").exists(), True)
    check("sweep reports a total", "reclaimed" in out, True)


GROUPS = {"config": group_config, "reviewer": group_reviewer_gate, "budget": group_budget,
          "fixer": group_fixer_gate, "seats": group_seats, "parallel": group_parallel,
          "exclusive": group_exclusive, "settings": group_settings,
          "webhook_host": group_webhook_host,
          "plugin_settings": group_plugin_settings, "watchdog": group_watchdog,
          "cleanup": group_cleanup}


def main() -> int:
    wanted = sys.argv[1:] or list(GROUPS)
    sink = start_sink()
    global HOST
    HOST = sink
    DATA["host"] = sink
    os.environ.update(env())          # the config group reads fixtures in-process
    reset(prs={})                     # fixtures exist before any group runs
    for name in wanted:
        GROUPS[name]()
    passed = sum(1 for ok, _ in results if ok)
    failed = [n for ok, n in results if not ok]
    print(f"\n{passed}/{len(results)} checks pass")
    if failed:
        print("failed:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
