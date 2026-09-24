#!/usr/bin/env python3
"""The loop's proof: every gate branch, the watchdog's four stall shapes, and the cleanup rails.

Runs with a plain interpreter and no network — no ``gh``, no pytest, no GitHub:

* GitHub is a stub executable (``REVIEW_LOOP_GH_STUB``) answering from a JSON "world" file, so
  the tests can put a fixer, a verdict and a review request exactly where they want them;
* the webhook endpoint is a real local HTTP server, so watchdog wake paths still exercise
  signature validation; eligible gate runs return [SILENT] and fail closed without runtime;
* the isolated supervisor's SQLite ledger is tested with a trusted inert fixture command,
  never an ambient GitHub bypass or a credential-owning gateway agent;
* git is real: the cleanup tests build a throwaway clone with detached review worktrees and a
  branch worktree, because the difference between those two is the whole safety story.

    python3 tests/run_tests.py            # all of it
    python3 tests/run_tests.py cleanup    # one group
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import importlib.util
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Fixtures must not inherit the source checkout's Git owner: cleanup correctly
# refuses to delete artifacts under an unrelated repository, even a test repo.
TMP = pathlib.Path(tempfile.mkdtemp(prefix="review-loop-tests-", dir=os.environ.get("TMPDIR")))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
HOME = TMP / "hermes-home"
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
       merged: str | None = None, requested: str | None = None) -> dict:
    """A pull request as the REST API renders it.

    ``requested`` is the reviewer whose request is still pending — GitHub's own
    ``requested_reviewers``, which it clears the moment a verdict lands. ``explain`` reads it, so
    the fixtures carry it rather than inventing a queue entry for something GitHub owns.
    """
    return {"number": number, "state": state, "draft": draft, "merged_at": merged,
            "title": title, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "base": {"ref": base}, "user": {"login": author},
            "head": {"sha": head, "ref": "fix-thing"},
            "requested_reviewers": [{"login": requested}] if requested else []}


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

if re.search(r"/hooks\?per_page=100(?:&page=\\d+)?$", path):
    page = int(re.search(r"[?&]page=(\\d+)", path).group(1)) if "&page=" in path else 1
    hooks = world.get("hooks", [])
    print(json.dumps(hooks[(page-1)*100:page*100] if isinstance(hooks, list) else hooks))
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
        # A route whose name says "fail" gets a 5xx. That is the one delivery failure a test can
        # produce without also breaking the seat routes, which share this host and must keep
        # answering 202 — the point of the test being that the loop turns either way.
        self.send_response(500 if "fail" in self.path else 202)
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
                      "prompt": "Review-loop event", "skills": [], "deliver": "discord", "profile": profile,
                      "created_at": PAST, "script": script, "host": HOST}
    SUBS.write_text(json.dumps(subs, indent=2))
    return subs


def write_profiles(*names: str) -> None:
    """Place the Hermes profiles the seat tests name.

    A named profile needs its own config.yaml, not merely an empty directory.
    """
    for name in names:
        profile = HOME / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text("model:\n  default: test-model\n")
def observer_route(route: str = "widgets-observe", profile: str = "tuck-profile",
                   deliver: str = "telegram", events=None, mute: bool = False,
                   digest_min: int = 0, secret: bool = True, register: bool = True) -> dict:
    """Configure a loop's observer feed the way ``init --observer-*`` would, plus its route.

    A route is registered exactly as ``_install_routes`` registers one (deliver_only, the
    notice prompt, the adapter script), so the feed under test is the feed that ships.
    ``register=False`` points the loop at a route that was never installed — the operator's
    typo, which the loop has to survive.
    """
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    block: dict = {"route": route, "profile": profile, "deliver": deliver}
    if events is not None:
        block["events"] = events
    if mute:
        block["mute"] = True
    if digest_min:
        block["digest_min"] = digest_min
    cfg["observer"] = block
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    subs = json.loads(SUBS.read_text())
    if register:
        subs[route] = {"description": route, "events": ["pull_request"],
                       "secret": hashlib.sha256(route.encode()).hexdigest() if secret else "",
                       "prompt": "{_observer.message}", "skills": [], "deliver": deliver,
                       "deliver_only": True, "profile": profile, "created_at": PAST,
                       "script": "observe.py", "host": HOST}
        SUBS.write_text(json.dumps(subs))
    return block


def observer_posts(route: str = "widgets-observe") -> list[dict]:
    """Every delivery the loop sent to an observer route, in order."""
    return [r for r in RECEIVED if r["path"].endswith(f"/webhooks/{route}")]


def notice(request: dict) -> dict:
    """The ``_observer`` block of a delivered notice — what the route prompt renders."""
    return json.loads(request["body"])["_observer"]


def reset(hooks_active: bool = True, prs: dict | None = None) -> dict:
    for path in (STATE_DIR, REVIEWS, SCRATCH, CLONE, LOOPS_DIR, HOME / "state"):
        shutil.rmtree(path, ignore_errors=True)
    RECEIVED.clear()
    for path in (SUBS, WORLD_FILE):
        if path.exists():
            path.unlink()
    write_profiles("reviewer-profile", "fixer-profile", "drey", "vex", "tuck")
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
    # Isolate every child, even when the CI runner has no HERMES_HOME.
    return {**os.environ, "HERMES_HOME": str(TMP / "hermes-home"),
            "REVIEW_LOOP_CONFIG_DIR": str(LOOPS_DIR),
            "REVIEW_LOOP_SUBS": str(SUBS), "REVIEW_LOOP_GH_STUB": str(STUB),
            "GH_WORLD": str(WORLD_FILE), "REVIEW_LOOP_TEST": "1"}


def run(script: str, payload: dict | None = None, *args: str,
        extra_env: dict | None = None) -> tuple[str, str, str]:
    cmd = [sys.executable, str(ROOT / "scripts" / script), *args]
    proc = subprocess.run(cmd, input=json.dumps(payload) if payload else None,
                          capture_output=True, text=True,
                          env={**env(), **(extra_env or {})}, timeout=180)
    out, err = proc.stdout.strip(), proc.stderr.strip()
    if out.startswith("[SILENT]"):
        kind = "SILENT"
    else:
        kind = out
    return kind, out, err


def held(seat: str, number: int, head: str, reason: str = "isolated worker unavailable") -> bool:
    """An eligible gate fails closed without a private runtime, never dispatching to gateway."""
    entry = load_state("pending.json").get(seat, {}).get(f"{REPO}#{number}", {})
    return entry.get("head") == head and reason in entry.get("reason", "")


def no_ledger_run() -> bool:
    db = TMP / "hermes-home" / "state" / "review-loop-runs.sqlite"
    if not db.exists():
        return True
    with sqlite3.connect(db) as con:
        return con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def check_eligible(name: str, script: str, payload: dict, seat: str,
                   number: int = 7, head: str = HEAD_A) -> None:
    kind, out, _ = run(script, payload)
    check(name + " returns [SILENT]", (kind, out), ("SILENT", "[SILENT]"))
    check(name + " held for isolated worker", held(seat, number, head), True)
    check(name + " never enters gateway ledger", no_ledger_run(), True)


def check_rejected(name: str, script: str, payload: dict) -> None:
    kind, out, _ = run(script, payload)
    check(name, (kind, out), ("SILENT", "[SILENT]"))
    check(name + " leaves no pending run", load_state("pending.json"), {})
    check(name + " leaves no ledger run", no_ledger_run(), True)


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
                fixer_concurrency=None, dry_run=False, seat=None, pr=None,
                observer_route=None, observer_profile=None, observer_deliver=None,
                observer_events=None, observer_digest_min=None, observer_mute=False,
                observer_unmute=False, observer_disable=False, keep_config=False)
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
    check_eligible("explicit request for this seat", "gate_reviewer.py", pr_payload(), "reviewer")

    reset(prs={"7": pr(7)})
    check_rejected("request naming another reviewer is silent", "gate_reviewer.py",
                   pr_payload(requested="someone-else"))

    reset(prs={"7": pr(7)})
    check_rejected("request from a stranger is silent", "gate_reviewer.py",
                   pr_payload(sender="passer-by"))

    reset(prs={"7": pr(7)})
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
    check("  no review run queued at cap", load_state("pending.json"), {})
    check("  no ledger run at cap", no_ledger_run(), True)
    check("  breach marker remains pending while adjudicator is blocked",
          load_state("breach.json").get(f"{REPO}#7", {}).get("status"), "delivery-pending")
    check("  adjudicator is blocked", RECEIVED, [])

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
    check("  breach marker remains pending while adjudicator is blocked",
          load_state("breach.json").get(f"{REPO}#7", {}).get("status"), "delivery-pending")
    check("  adjudicator is blocked", RECEIVED, [])

    reset(prs={"7": {**pr(7), "reviews": [review(REVIEWER, rid=5)]}})
    kind, out, _ = run("gate_fixer.py", review_payload(rid=5))
    check("first changes-requested → eligible but silent", (kind, out), ("SILENT", "[SILENT]"))
    check("  fix held for isolated worker", held("fixer", 7, HEAD_A), True)
    check("  no gateway dispatch", no_ledger_run(), True)

def group_adjudicator() -> None:
    """Keep #21 breach guards without dispatching a credential-owning agent."""
    from review_loop import cli, config, prompts

    section("adjudicator — guarded marker, no legacy gateway dispatch")
    reviews = [review(REVIEWER, head=ch * 40, rid=i) for i, ch in enumerate("cde", 1)]
    reset(prs={"7": {**pr(7, head=HEAD_B), "reviews": reviews}})
    cli._install_routes(config.load_id("widgets"))
    route = json.loads(SUBS.read_text())["widgets-breach"]
    check("adjudicator route retains its own gate", route["script"], "gate_adjudicator.py")
    check("adjudicator retains ruling prompt", route["prompt"] == prompts.ADJUDICATOR, True)
    check("cap blocks fourth review", run("gate_reviewer.py", pr_payload(head=HEAD_B))[0], "SILENT")
    marker = load_state("breach.json")[f"{REPO}#7"]
    check("blocked wake stays delivery-pending", marker["status"], "delivery-pending")
    check("blocked wake does not POST to gateway", len(RECEIVED), 0)
    run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("repeat head does not POST", len(RECEIVED), 0)
    set_prs({"7": {**pr(7, head=HEAD_A), "reviews": reviews}})
    run("gate_reviewer.py", pr_payload(head=HEAD_B))
    check("stale event cannot regress marker", load_state("breach.json")[f"{REPO}#7"]["head"], HEAD_B)
    set_prs({"7": {**pr(7, head=HEAD_B), "reviews": reviews}})
    run("watchdog.py", None, "--loop", "widgets")
    check("watchdog cannot deliver blocked route", len(RECEIVED), 0)
    check("watchdog leaves marker retryable", load_state("breach.json")[f"{REPO}#7"]["status"],
          "delivery-pending")
    # A stale pre-hold acknowledged marker and signed wake must not bypass the hold.
    marker = load_state("breach.json")[f"{REPO}#7"]
    marker["status"] = "awaiting-adjudication"
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


def set_concurrency(value: int) -> None:
    path = LOOPS_DIR / "widgets.json"
    cfg = json.loads(path.read_text())
    cfg["concurrency"] = value
    path.write_text(json.dumps(cfg))


def real_head() -> str:
    return subprocess.run(["git", "-C", str(CLONE), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


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

def parser_for(settings: dict | None = None):
    """A ``hermes review-loop`` parser wired to a settings form, for in-process commands."""
    from review_loop import cli

    fake = FakeCtx()
    cli.register_cli(fake, settings=settings)
    parser = argparse.ArgumentParser(prog="hermes review-loop")
    fake.setup(parser)
    return parser


def run_cli(parsed) -> tuple[int, str]:
    """Run a parsed command in-process and hand back its exit code plus what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = parsed.func(parsed)
    return rc, buf.getvalue()


SEAT_PATS = (TMP / "rev.pat", TMP / "fix.pat")     # written by ``write_loop`` for both seats


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
    check("the drain starts the queued review", "started the queued run" in out, True)
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
        check(f"  {state} queue entry cleared", load_state("pending.json"), {})
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
    check("  queue is cleared", load_state("pending.json"), {})
    check("  expired lock is cleared", load_state("locks.json"), {})
    before = len(RECEIVED)
    run("watchdog.py", None, "--loop", "widgets", extra_env={"REVIEW_LOOP_TEST": ""})
    check("  repeated sweep does not wake twice", len(RECEIVED) - before, 0)

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
    check("  fixer queue cleared", load_state("pending.json"), {})

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
        check(f"{label} rejects stale queued head", load_state("pending.json"), {})
        check(f"{label} never wakes stale head", len(RECEIVED) - before, 1)

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
    check("a wrong base → nothing", decide(pr=pr(7, base="release"))["next"]["kind"], "none")
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
    for doc in ("README.md", "docs/configuration.md"):
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

    # A branch checkout matching the *same* closed PR must survive the configured-root
    # scan, even when a configured root points inside that checkout.
    reset(prs={"7": pr(7, state="closed")})
    branch = REVIEWS / "pr7-dev"
    subprocess.run(["git", "-C", str(CLONE), "worktree", "add", "-b", "fix/pr7",
                    str(branch), "HEAD"], check=True, capture_output=True)
    sentinel = branch / "uncommitted.txt"
    sentinel.write_text("do not lose local work\n")
    nested = branch / "pr7-logs"
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
    parent = REVIEWS / "pr7-container"
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

    # Configured roots are discovery boundaries, not permission to remove another repo.
    reset(prs={"7": pr(7, state="closed")})
    other = REVIEWS / "pr7-other-repo"
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
    foreign_file = foreign_root / "pr7-source"
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
    (external / "pr7-sentinel").write_text("external root\n")
    linked = TMP / "linked-root"
    linked.unlink(missing_ok=True)
    linked.symlink_to(external, target_is_directory=True)
    cfg = json.loads((LOOPS_DIR / "widgets.json").read_text())
    cfg["roots"].append(str(linked))
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg))
    run("cleanup.py", None, "--loop", "widgets", "--pr", "7")
    check("symlinked root cannot delete external child", (external / "pr7-sentinel").exists(), True)

    # Swap a validated root at the du seam; string-path unlink would hit the sentinel.
    reset(prs={"7": pr(7, state="closed")})
    external = TMP / "outside-swap"
    shutil.rmtree(external, ignore_errors=True)
    external.mkdir()
    outside_file = external / "pr7-build.log"
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
    nested_repo = REVIEWS / "pr7-bundle" / "developer"
    subprocess.run(["git", "init", "-q", "-b", "main", str(nested_repo)], check=True)
    (nested_repo / "uncommitted.txt").write_text("nested repo\n")
    link = REVIEWS / "pr7-external-link"
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
    check("sweep keeps the unknown one", (SCRATCH / "pr8-target").exists(), True)
    check("sweep reports a total", "reclaimed" in out, True)


def group_routes() -> None:
    section("routes — atomic owner-only cross-process registry edits")
    test = subprocess.run([sys.executable, str(ROOT / "tests" / "test_routes_atomic.py")],
                          capture_output=True, text=True)
    if test.returncode:
        print(test.stdout + test.stderr)
    check("route registry regression suite", test.returncode, 0)


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
    before_files = tree_digest(TMP)
    before_posts = len(RECEIVED)
    rc, out = run_doctor("--loop", "widgets")
    check("a correct install passes", rc, 0)
    check("  every check verified", "widgets: 21 verified, 0 failed, 0 unknown (of 21 checks)" in out,
          True)
    check("  nothing is marked failed", "❌" in out, False)
    check("  the header says it is read-only",
          "read-only: it writes nothing and fires nothing" in out, True)
    for name in ("config", "profile:reviewer", "profile:fixer", "profile:adjudicator", "credential:reviewer",
                 "credential:fixer", "token:rev-coach", "token:dev-fixer", "read_token",
                 "route:widgets-review", "route:widgets-fix", "route:widgets-breach", "scripts",
                 "cron:shim", "cron:job", "clone", "state_dir", "roots", "gateway",
                 "hook:widgets-review", "hook:widgets-fix"):
        check(f"  ✅ {name}", f"✅ {name}" in out, True)
    check("  it writes nothing", tree_digest(TMP), before_files)
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
    rc, out = run_doctor("--loop", "widgets", "--offline")
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

def group_reconciliation() -> None:
    section("seat reconciliation — route, hook and config rollback")
    test = subprocess.run([sys.executable, str(ROOT / "tests" / "test_reconciliation.py")],
                          capture_output=True, text=True)
    if test.returncode:
        print(test.stdout + test.stderr)
    check("route and hook reconciliation regression suite", test.returncode, 0)


GROUPS = {"routes": group_routes, "config": group_config, "reviewer": group_reviewer_gate, "budget": group_budget,
          "adjudicator": group_adjudicator,
          "fixer": group_fixer_gate, "seats": group_seats, "parallel": group_parallel,
          "exclusive": group_exclusive, "settings": group_settings,
          "webhook_host": group_webhook_host,
          "plugin_settings": group_plugin_settings, "seat_identity": group_seat_identity,
          "watchdog": group_watchdog, "explain": group_explain,
          "cleanup": group_cleanup, "doctor": group_doctor,
          "reconciliation": group_reconciliation,
           "observer": group_observer, "observer_safety": group_observer_safety,
           "observer_cli": group_observer_cli}


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
