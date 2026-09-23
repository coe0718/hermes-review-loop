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

import hashlib
import hmac
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    (SCRATCH / "pr8-target" / "junk.bin").write_bytes(b"y" * 2048)
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

    # a stale lock frees itself
    locks = load_state("locks.json")
    locks["reviewer"]["at"] = time.time() - 46 * 60
    state_file("locks.json").write_text(json.dumps(locks))
    check("lock older than the TTL → seat free again",
          run("gate_reviewer.py", pr_payload(9, head=HEAD_B))[0], "FIRE")

    # releasing a seat only ever releases ITS OWN turn
    reset(prs={"7": pr(7)})
    state_file("locks.json").write_text(json.dumps(
        {"fixer": {"at": time.time(), "key": f"{REPO}#999", "why": "other PR"}}))
    run("gate_reviewer.py", pr_payload(7))
    check("another PR's fixer lock survives", "999" in state_file("locks.json").read_text(), True)

    # the reviewer seat and the in-flight mark are cleared so the next run reaches the release
    locks = load_state("locks.json")
    locks.pop("reviewer", None)
    locks["fixer"] = {"at": time.time(), "key": f"{REPO}#7", "why": "this PR"}
    state_file("locks.json").write_text(json.dumps(locks))
    state_file("inflight.json").write_text("{}")
    run("gate_reviewer.py", pr_payload(7))
    check("this PR's fixer lock is released", "fixer" in load_state("locks.json"), False)

    section("seats — the fixer side releases the reviewer")
    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {"at": time.time(), "key": f"{REPO}#7", "why": "review"}}))
    run("gate_fixer.py", review_payload(rid=5))
    check("verdict → reviewer seat released", "reviewer" in load_state("locks.json"), False)


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

    # a still-busy seat fires nothing
    reset(prs={"7": pr(7)})
    state_file("locks.json").write_text(json.dumps(
        {"reviewer": {"at": time.time(), "key": f"{REPO}#9", "why": "working"}}))
    state_file("pending.json").write_text(json.dumps(
        {"reviewer": {f"{REPO}#7": {"at": time.time(), "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets", "--drain", "--seat", "reviewer")
    check("busy seat drains nothing", "still busy" in out, True)

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
        {"reviewer": {"at": time.time() - 120 * 60, "key": f"{REPO}#7", "why": "died"}}))
    state_file("pending.json").write_text(json.dumps(
        {"fixer": {f"{REPO}#7": {"at": time.time() - 90 * 60, "head": HEAD_A, "url": "u", "reason": "busy"}}}))
    out, _, _ = run("watchdog.py", None, "--loop", "widgets")
    check("stuck: dead seat lock reported", "seat held 120m" in out, True)
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
          "fixer": group_fixer_gate, "seats": group_seats, "watchdog": group_watchdog,
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
