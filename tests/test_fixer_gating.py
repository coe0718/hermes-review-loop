"""The fix leg's two operator-facing gaps, offline and with disposable config/state only.

1. A loop that has not opted in to unattended fixer pushes starts no fixer turn: the verdict is
   held (no ledger row, no worker) with the exact enable command, and every surface says so.
2. The fixer's in-sandbox client builds the push manifest itself and refuses what the broker
   would refuse before spending the one write.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_loop import cli, config, doctor, gate, gh, run_supervisor, state as state_mod  # noqa: E402
from review_loop.run_supervisor import FixerPushDisabled, Supervisor  # noqa: E402
from scripts import gate_fixer, watchdog  # noqa: E402

HEAD = "a" * 40
REPO = "owner/one"
ENABLE = "hermes review-loop fixer-push --loop one --enable --acknowledge-pr-race"


def raw_loop(push: bool) -> dict:
    return {"id": "one", "repo": REPO, "base": "main", "cap": 3, "fixers": ["fixer"],
            "reviewers": ["reviewer"], "reviewer_seat": "reviewer", "read_token": "reviewer",
            "unattended_fixer_push": push,
            "seats": {"reviewer": {"route": "one-review", "profile": "reviewer"},
                      "fixer": {"route": "one-fix", "profile": "fixer"}}}


def verdict(rid: int = 5, state: str = "CHANGES_REQUESTED") -> dict:
    return {"id": rid, "state": state, "commit_id": HEAD, "submitted_at": "2026-01-01T00:00:00Z",
            "user": {"login": "reviewer"}}


LIVE = {"number": 7, "state": "open", "draft": False, "head": {"sha": HEAD, "ref": "fix-7"},
        "base": {"ref": "main", "sha": "b" * 40}, "user": {"login": "fixer"},
        "title": "Fix widget", "html_url": "https://github.com/owner/one/pull/7",
        "created_at": "2020-01-01T00:00:00Z"}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = mock.patch.dict(os.environ, {"REVIEW_LOOP_CONFIG_DIR": str(self.root / "cfg"),
                                           "HERMES_HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        (self.root / "home").mkdir()
        config.config_dir().mkdir(parents=True)
        self.set_push(False)

    def set_push(self, enabled: bool) -> None:
        raw = {**raw_loop(enabled), "state_dir": str(self.root / "state")}
        (config.config_dir() / "one.json").write_text(json.dumps(raw))
        self.loop = config.load_id("one")
        self.st = state_mod.state_for(self.loop)

    def ledger_rows(self) -> list:
        db = self.root / "home" / "state" / "review-loop-runs.sqlite"
        if not db.exists():
            return []
        with sqlite3.connect(db) as con:
            return con.execute("SELECT seat, push_admitted, state FROM runs").fetchall()


class GateHold(Base):
    """gate_fixer end to end, with GitHub, the worker spawn and the observer mocked."""

    def run_gate(self, rid: int = 5):
        payload = {"action": "submitted", "repository": {"full_name": REPO},
                   "pull_request": LIVE, "review": verdict(rid) | {"state": "changes_requested"}}
        with mock.patch.object(gate_fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             mock.patch.object(gate_fixer.gh, "pr", return_value=LIVE), \
             mock.patch.object(gate_fixer.gate, "fetch_reviews", return_value=[verdict(rid)]), \
             mock.patch.object(gate_fixer.gate, "drain_seat"), \
             mock.patch.object(gate, "enqueue_isolated") as enqueue, \
             mock.patch.object(gate_fixer.observer, "notify") as notify, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gate_fixer.main()
        self.assertEqual(out.getvalue().strip(), "[SILENT]")
        return enqueue, notify

    def test_push_off_holds_the_verdict_with_the_enable_command(self):
        enqueue, notify = self.run_gate()
        enqueue.assert_not_called()                      # no ledger row, no worker, no turn
        self.assertEqual(self.ledger_rows(), [])
        entry = self.st.queue_items("fixer")[f"{REPO}#7"]
        self.assertEqual(entry["head"], HEAD)
        self.assertTrue(config.is_fixer_push_hold(entry))
        self.assertIn(ENABLE, entry["reason"])
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["identity"], 5)
        self.assertIn(ENABLE, kwargs["next_turn"])
        self.assertIn("fixer held", kwargs["next_turn"])

    def test_redelivered_verdict_is_one_hold_and_one_notice_key(self):
        _, first = self.run_gate()
        at = self.st.queue_items("fixer")[f"{REPO}#7"]["at"]
        _, second = self.run_gate()
        self.assertEqual(self.st.queue_items("fixer")[f"{REPO}#7"]["at"], at)
        # Same review id → same observer ledger key → the observer records it once.
        self.assertEqual(first.call_args.kwargs["identity"], second.call_args.kwargs["identity"])
        self.assertEqual(first.call_args.args[2:5], second.call_args.args[2:5])

    def test_push_on_is_unchanged(self):
        self.set_push(True)
        enqueue, notify = self.run_gate()
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1:4], ("fixer", 7, HEAD))
        self.assertEqual(notify.call_args.kwargs["next_turn"], "fixer queued")
        self.assertEqual(self.st.queue_items("fixer"), {})

    def test_policy_flip_under_the_admission_lock_is_the_same_hold(self):
        self.set_push(True)
        with mock.patch.object(gate, "enqueue_isolated", side_effect=FixerPushDisabled(REPO)), \
             mock.patch.object(gate, "silence", side_effect=SystemExit), \
             contextlib.redirect_stderr(io.StringIO()):
            queued, held = mock.Mock(), mock.Mock()
            with self.assertRaises(SystemExit):
                gate.block_pr_agent(self.loop, self.st, "fixer", 7, HEAD,
                                    on_queued=queued, on_push_off=held)
        queued.assert_not_called()
        held.assert_called_once()
        self.assertTrue(config.is_fixer_push_hold(self.st.queue_items("fixer")[f"{REPO}#7"]))


class SupervisorAdmission(Base):
    def production(self) -> Supervisor:
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        return Supervisor(config.home() / "state" / "review-loop-runs.sqlite",
                          production_config=settings, hermes_home=self.root / "home")

    def test_gate_path_enqueue_refuses_before_any_row(self):
        sup = self.production()
        with mock.patch.object(sup, "_spawn") as spawn:
            with self.assertRaises(FixerPushDisabled):
                sup.enqueue("d", REPO, 7, HEAD, "fixer", require_push_admission=True)
        spawn.assert_not_called()
        self.assertEqual(self.ledger_rows(), [])
        # After an opt-in the same verdict is a fresh admission, not an upgraded row.
        self.set_push(True)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", REPO, 7, HEAD, "fixer", require_push_admission=True)
        self.assertEqual(self.ledger_rows(), [("fixer", 1, "pending")])

    def test_unadmitted_row_is_cancelled_at_claim_without_a_github_read(self):
        sup = self.production()
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("old", REPO, 7, HEAD, "fixer")       # legacy/raced: admitted while off
        self.assertEqual(sup.get("old")["push_admitted"], 0)
        self.set_push(True)                                    # a later opt-in does not help it
        with mock.patch.object(gh, "api") as api:
            self.assertIsNone(sup._claim())
        api.assert_not_called()
        row = sup.get("old")
        self.assertEqual((row["state"], row["error"]),
                         ("cancelled", run_supervisor.FIXER_NOT_ADMITTED))

    def test_admitted_row_is_cancelled_when_the_loop_opted_out_since(self):
        self.set_push(True)
        sup = self.production()
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("run", REPO, 7, HEAD, "fixer", require_push_admission=True)
        self.set_push(False)
        with mock.patch.object(gh, "api") as api:
            self.assertIsNone(sup._claim())
        api.assert_not_called()
        self.assertEqual(sup.get("run")["error"], run_supervisor.FIXER_PUSH_REVOKED)


class Surfaces(Base):
    def explain(self):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": {}, "parked": False, "delivery_status": "",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local):
            return gate.explain(self.loop, self.st, 7, {"pr": LIVE, "reviews": [verdict()],
                                                        "armed": True, "read_at": time.time()})

    def test_explain_names_the_operator_decision(self):
        report = self.explain()
        self.assertEqual(report["next"]["kind"], "operator")
        self.assertIn(ENABLE, report["next"]["action"])
        self.assertTrue(any(ENABLE in line for line in report["blockers"]))
        self.assertFalse(any("did not start one" in line for line in report["blockers"]))
        self.set_push(True)
        self.assertEqual(self.explain()["next"]["kind"], "fixer-retry")

    def test_doctor_says_whether_the_fix_leg_can_run(self):
        off = doctor.check_fixer_push(self.loop)
        self.assertEqual(off.status, doctor.UNKNOWN)
        self.assertIn("cannot run", off.detail)
        self.assertIn(ENABLE, off.detail)
        with mock.patch.object(doctor, "check_fixer_push", return_value=off) as probe:
            doctor.check_loop(self.loop, offline=True)
        probe.assert_called_once_with(self.loop)
        self.set_push(True)
        self.assertEqual(doctor.check_fixer_push(self.loop).status, doctor.VERIFIED)

    def sweep(self):
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[LIVE]), \
             mock.patch.object(gh, "reviews", return_value=[verdict()]), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire") as fire, \
             mock.patch.object(watchdog.observer, "notify") as notify, \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            lines = watchdog.sweep_loop(self.loop, self.st)
        return lines, fire, notify

    def test_watchdog_reports_once_per_head_and_never_wakes_the_fixer(self):
        gate.hold_fixer_push_off(self.loop, self.st, 7, HEAD)
        self.sweep()                                           # first sweep: arms, baselines
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 3600
        self.st.watch_save(watch)
        lines, fire, notify = self.sweep()
        text = "\n".join(lines)
        self.assertIn(watchdog.PUSH_OFF_KIND, text)
        self.assertIn(ENABLE, text)
        self.assertNotIn("fixer never pushed", text)
        self.assertNotIn("fixer queue:", text)                 # not also a per-sweep stuck line
        fire.assert_not_called()
        self.assertEqual(notify.call_args.kwargs["identity"], "fixer-push-off")
        # TEST mode has no cooldown, so the next sweep re-raises the line — but its observer
        # notice is keyed without the sweep clock: the same fact, recorded once.
        _, fire, again = self.sweep()
        fire.assert_not_called()
        for call in again.call_args_list:
            self.assertEqual(call.kwargs["identity"], "fixer-push-off")

    def test_drain_starts_the_held_verdict_only_after_opt_in(self):
        gate.hold_fixer_push_off(self.loop, self.st, 7, HEAD)
        with mock.patch.object(watchdog.routes, "fire") as fire, \
             mock.patch.object(gh, "pr") as pr:
            self.assertEqual(watchdog.drain(self.loop, self.st, "fixer", quiet=True), 0)
        fire.assert_not_called()
        pr.assert_not_called()                                 # not even a GitHub read
        self.set_push(True)

        def delivered(*_args, **_kw):                          # the gate enqueues and pops it
            self.st.queue_pop("fixer", f"{REPO}#7")
            return True
        with mock.patch.object(watchdog.routes, "fire", side_effect=delivered) as fire, \
             mock.patch.object(gh, "pr", return_value=LIVE), \
             mock.patch.object(gh, "reviews", return_value=[verdict()]):
            self.assertEqual(watchdog.drain(self.loop, self.st, "fixer", quiet=True), 1)
        self.assertEqual(fire.call_args.args[1], "pull_request_review")

    def test_enable_names_held_verdicts_and_init_names_the_step(self):
        gate.hold_fixer_push_off(self.loop, self.st, 7, HEAD)
        args = argparse.Namespace(loop="one", enable=True, disable=False,
                                  acknowledge_pr_race=True, dry_run=False)
        with mock.patch.object(cli, "_busy_seats", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_fixer_push(args), 0)
        self.assertIn("1 held verdict(s)", out.getvalue())
        self.assertIn("drain --loop one --seat fixer", out.getvalue())
        source = (ROOT / "review_loop" / "cli.py").read_text()
        self.assertIn("decide the fix leg", source)
        self.assertIn("fixer-push --loop", (ROOT / "docs" / "operations.md").read_text())



# -- Gap 2: the fixer is told, and helped to build, the push manifest ----------------------------


class PushHelper(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / "work"
        (self.work / "src").mkdir(parents=True)
        (self.work / "src" / "a.py").write_text("print('fixed')\n")
        (self.work / "README.md").write_text("docs\n")
        self.turn = self.root / "turn.json"
        self.turn.write_text(json.dumps({"head": HEAD}))
        (self.root / "msg.txt").write_text("Fix the widget off-by-one\n")
        from review_loop import broker_client
        self.client = broker_client
        for name, value in (("WORK", str(self.work)), ("TURN_FILE", str(self.turn))):
            patcher = mock.patch.object(broker_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, *argv: str):
        """Run the in-sandbox CLI; any socket use at all fails the test."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["broker_client", *argv]), \
             mock.patch.object(self.client.socket, "socket",
                               side_effect=AssertionError("socket opened")) as sock, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                self.client.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), sock

    def test_helper_builds_a_manifest_the_broker_accepts(self):
        from review_loop import safe_push
        manifest = self.client.build_manifest(["src/a.py", str(self.work / "README.md")],
                                              "Fix the widget")
        self.assertEqual(set(manifest), {"base_head", "message", "files"})
        base, files = safe_push._manifest(manifest)
        self.assertEqual(base, HEAD)
        self.assertEqual(files, [("src/a.py", b"print('fixed')\n"), ("README.md", b"docs\n")])

    def test_limits_mirror_the_broker(self):
        from review_loop import safe_push
        self.assertEqual((self.client.MAX_FILES, self.client.MAX_FILE, self.client.MAX_CONTENT,
                          self.client.MAX_MESSAGE),
                         (safe_push.MAX_FILES, safe_push.MAX_FILE, safe_push.MAX_CONTENT,
                          safe_push.MAX_MESSAGE))
        self.assertEqual(self.client._CONTROL_FILES, safe_push.CONTROL_FILES)
        self.assertEqual(self.client._CONTROL_PATHS, safe_push.CONTROL_PATHS)

    def test_dry_run_sends_nothing(self):
        code, out, _, sock = self.main("push", "--files", "src/a.py", "--message-file",
                                       str(self.root / "msg.txt"), "--dry-run")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["dry_run"], result["base_head"], result["message"]),
                         (True, HEAD, "Fix the widget off-by-one"))
        sock.assert_not_called()

    def test_refusals_happen_before_any_socket_call(self):
        (self.work / "big.py").write_bytes(b"x" * (64 * 1024 + 1))
        (self.work / ".github" / "workflows").mkdir(parents=True)
        (self.work / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        (self.work / "CODEOWNERS").write_text("* @me\n")
        (self.work / "link.py").symlink_to(self.work / "src" / "a.py")
        many = []
        for i in range(25):
            (self.work / f"f{i}.py").write_text(str(i))
            many.append(f"f{i}.py")
        cases = {
            "file too large": (["big.py"], "fix", "at most 65536"),
            "too many files": (many, "fix", "1 to 24"),
            "message too long": (["src/a.py"], "x" * 241, "at most 240"),
            "empty message": (["src/a.py"], "  ", "non-empty"),
            "workflow": ([".github/workflows/ci.yml"], "fix", "control file"),
            "codeowners": (["CODEOWNERS"], "fix", "control file"),
            "missing file": (["gone.py"], "fix", "cannot delete"),
            "symlink": (["link.py"], "fix", "symlink"),
            "outside work": (["/etc/hostname"], "fix", "not a file under"),
            "duplicate": (["src/a.py", "src/a.py"], "fix", "named twice"),
        }
        for name, (files, message, expect) in cases.items():
            with self.subTest(name):
                code, _, err, sock = self.main("push", "--files", *files, "--message", message)
                self.assertEqual(code, 2)
                self.assertIn("unspent", err)
                self.assertIn(expect, err)
                sock.assert_not_called()

    def test_base_head_comes_from_the_host_turn_file(self):
        self.turn.unlink()
        code, _, err, sock = self.main("push", "--files", "src/a.py", "--message", "fix")
        self.assertEqual(code, 2)
        self.assertIn("head is unavailable", err)
        sock.assert_not_called()

    def test_tampered_base_head_is_still_refused_by_the_broker(self):
        from review_loop import broker, safe_push
        self.turn.write_text(json.dumps({"head": "c" * 40}))
        manifest = self.client.build_manifest(["src/a.py"], "fix")
        loop = {"repo": REPO, "unattended_fixer_push": True}
        with mock.patch.object(broker, "authorize", side_effect=AssertionError("authorized")):
            with self.assertRaisesRegex(broker.BrokerDenied, "base differs"):
                safe_push.push(loop, repo=REPO, number=7, head=HEAD, role="fixer",
                               branch="fix-7", manifest=manifest)

    def test_manifest_file_still_works(self):
        manifest = self.client.build_manifest(["src/a.py"], "fix")
        (self.root / "m.json").write_text(json.dumps(manifest))
        with mock.patch.object(self.client, "call", return_value={"ok": True}) as call, \
             mock.patch.object(sys, "argv", ["broker_client", "push", "--manifest-file",
                                             str(self.root / "m.json")]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.client.main()
        call.assert_called_once_with("push", manifest=manifest)


class FixerInstructions(unittest.TestCase):
    def test_fixer_is_told_the_helper_limits_and_what_a_push_cannot_do(self):
        from review_loop import prompts, trusted_turn
        text = trusted_turn.tool_instructions("fixer")
        for needle in ("broker_client push --files", "--message-file", "--dry-run",
                       "24 files", "64 KiB", "128 KiB", "240 bytes", "cannot delete or rename",
                       ".github/", "CODEOWNERS", "request_review", "--manifest-file",
                       "content_b64", "no `.git`"):
            self.assertIn(needle, text)
        self.assertIn("cannot delete or rename", prompts.ISOLATED_FIXER)
        self.assertIn("--dry-run", prompts.ISOLATED_FIXER)
        # Other seats are not offered the helper.
        for role in ("reviewer", "adjudicator"):
            self.assertNotIn("--files", trusted_turn.tool_instructions(role))

    def test_run_turn_writes_the_head_read_only_for_the_fixer_only(self):
        import subprocess
        from review_loop import broker_client, broker_ipc, contained, trusted_turn
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        root.chmod(0o700)
        for name in ("venv", "runtime", "rust"):
            (root / name).mkdir()
        loop = {"id": "one", "repo": REPO, "state_dir": str(root / "state")}

        class Inference:
            def __init__(self, directory, *a, **k):
                self.directory = directory

            def __enter__(self):
                self.directory.mkdir()
                (self.directory / "model.sock").touch()
                return self

            def __exit__(self, *a):
                return False

        def stage(_loop, **kw):
            kw["sandbox_root"].mkdir()
            return kw["sandbox_root"]

        for role in ("fixer", "reviewer"):
            seen = {}

            def run(**kw):
                turn = Path(kw["client_code"]) / Path(broker_client.TURN_FILE).name
                seen["turn"] = json.loads(turn.read_text()) if turn.exists() else None
                seen["mode"] = turn.stat().st_mode & 0o777 if turn.exists() else None
                seen["query"] = Path(kw["query"]).read_text()
                return subprocess.CompletedProcess([], 1, "", "")

            scope = broker_ipc.RunScope(REPO, 7, HEAD, role, "fix-7", "rid", str(root / "db"))
            with self.subTest(role), \
                 mock.patch.object(trusted_turn, "_safe_code_snapshot",
                                   side_effect=lambda src, dst: dst.mkdir()), \
                 mock.patch.object(trusted_turn.trusted_fetch, "stage", side_effect=stage), \
                 mock.patch.object(trusted_turn.inference_proxy, "InferenceCapability", Inference), \
                 mock.patch.object(contained.Path, "is_socket", return_value=True), \
                 mock.patch.object(contained, "run", side_effect=run):
                trusted_turn.run_turn(loop, scope, source=root, venv=root / "venv",
                                      runtime=root / "runtime", rust=root / "rust",
                                      upstream="https://model.invalid", key="k", model="m",
                                      prompt="FIX", timeout=5, work_root=root / "work")
                if role == "fixer":
                    self.assertEqual(seen["turn"], {"head": HEAD})
                    self.assertEqual(seen["mode"], 0o444)
                    self.assertIn("push --files", seen["query"])
                else:
                    self.assertIsNone(seen["turn"])


if __name__ == "__main__":
    unittest.main()
