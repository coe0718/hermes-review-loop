"""Shared fixture and framework for ``tests/run_tests.py``.

Importing this module creates the temp tree (removed at exit); ``run_tests.main`` starts the
webhook sink and rebinds ``HOST`` in every area module. Named without a ``test_`` prefix so
``unittest discover`` never imports it.
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

ROOT = pathlib.Path(__file__).resolve().parents[2]
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
            "base": {"ref": base, "sha": "c" * 40,
                     "repo": {"full_name": REPO}}, "user": {"login": author},
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

if re.search(r"/hooks\\?per_page=100(?:&page=\\d+)?$", path):
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
elif path.endswith("/git/ref/heads/main"):
    print(json.dumps({"ref": "refs/heads/main", "object": {"type": "commit", "sha": "c" * 40}}))
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
    (REVIEWS / "widgets-pr7-build.log").write_text("log\n")
    (REVIEWS / "pr7-phase3-evidence").mkdir()
    (REVIEWS / "pr7-phase3-evidence" / "keep.json").write_text("{}\n")
    # a detached review checkout for PR 9 (still open) and a branch worktree for PR 8
    git("worktree", "add", "--detach", str(REVIEWS / "pr9-wt"), "HEAD")
    (REVIEWS / "pr9-wt" / "notes.txt").write_text("keep me\n")
    (SCRATCH / "widgets-pr8-target").mkdir(parents=True)
    # A build-output file the cleanup should reclaim. Text, and named like the real artifacts
    # (.log), because a stray .bin here trips the plugin security scan's binary-file caution every
    # time anyone runs `hermes plugins validate` on this repo.
    (SCRATCH / "widgets-pr8-target" / "build.log").write_text("y" * 2048 + "\n")
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
        "tokens": {REVIEWER: str(TMP / "rev.pat"), FIXER: str(TMP / "fix.pat"),
                   READ_LOGIN: str(READ_PAT)},
        "read_token": READ_LOGIN,
        "host": HOST,
        "grace_min": 25, "marker_grace_min": 60, "cooldown_h": 6,
        "ttl_min": 45, "inflight_ttl_min": 10,
        # The gate scenarios exercise the fix leg; the push-off hold has its own suite
        # (tests/test_fixer_gating.py).
        "unattended_fixer_push": True,
    }
    (LOOPS_DIR / "widgets.json").write_text(json.dumps(cfg, indent=2))
    (TMP / "rev.pat").write_text("token-reviewer\n")
    (TMP / "fix.pat").write_text("token-fixer\n")
    READ_PAT.write_text("token-reader\n")
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


def set_concurrency(value: int) -> None:
    path = LOOPS_DIR / "widgets.json"
    cfg = json.loads(path.read_text())
    cfg["concurrency"] = value
    path.write_text(json.dumps(cfg))


def real_head() -> str:
    return subprocess.run(["git", "-C", str(CLONE), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


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
# The reader is its own account with its own file (the four-identity rule): every `init` the
# harness drives names it, since init no longer borrows the reviewer seat for reads.
READ_LOGIN, READ_PAT = "read-acct", TMP / "read.pat"
READER_ARGS = ["--read-token", READ_LOGIN, "--token", f"{READ_LOGIN}={READ_PAT}"]


def verify_sig(request: dict, route: str) -> bool:
    subs = json.loads(SUBS.read_text())
    secret = subs[route]["secret"].encode()
    want = "sha256=" + hmac.new(secret, request["body"].encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, request["sig"])


def set_host(host: str) -> None:
    """Point ``HOST`` at the live sink in every harness module.

    Area modules take ``HOST`` by ``import *``, i.e. a copy of the placeholder; rebinding it only
    here would leave their copies stale.
    """
    for name, module in list(sys.modules.items()):
        if (name == __name__ or name.startswith(f"{__package__}.")) and hasattr(module, "HOST"):
            module.HOST = host
