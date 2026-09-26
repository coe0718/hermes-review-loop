"""Size bounds for the real sandbox: sized tmpfs, a bounded checkout, a read-only root.

``MAX_CAPTURE`` bounds what the parent retains and the caller bounds wall-clock time, but
bubblewrap's tmpfs default is half of RAM and a bind mount has no size at all: a seat that spent
its whole turn writing could fill the same host filesystem that holds the loop's ledger and state
(issue #89). These tests run the launcher's real argv through real bubblewrap — the style of
``tests/test_boundary.py`` — and assert the bound the *kernel* applies, not that a flag appears in
a list. Missing bubblewrap or user namespaces is a reported skip, never a silent pass.

The broker half pins the claim, not a hypothetical: ``broker_ipc``'s socket is a bearer capability
whose path any same-UID process can enumerate, and the module docstring has to say that instead of
implying an authentication it does not implement.
"""
from __future__ import annotations

import contextlib
import glob
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker_ipc, contained

MiB = 1024 * 1024
# Small enough to prove enforcement inside a test, larger than anything the probe stages.
TINY_SCRATCH = 8 * MiB
TINY_CHECKOUT = 16 * MiB
# Comfortably past either tiny budget, and cheap enough to write once per run.
WRITE_LIMIT = 64 * MiB

# Runs as the seat. It reports what the kernel enforced — mount options as mounted, the errno of a
# runaway write — never what the launcher intended.
PROBE = r'''
import errno, json, os, sys

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 0


def mounts():
    table = {}
    with open("/proc/mounts", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) >= 4:
                table[parts[1]] = {"type": parts[2], "options": parts[3]}
    return table


def fill(path):
    written, error = 0, ""
    try:
        with open(path, "wb") as handle:
            while written < LIMIT:
                handle.write(b"0" * (1024 * 1024))
                written += 1024 * 1024
    except OSError as exc:
        error = errno.errorcode.get(exc.errno, str(exc.errno))
    return {"written": written, "error": error}


def create(path):
    try:
        open(path, "w").close()
        return ""
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))


with open("/work/f.txt", encoding="utf-8") as handle:
    staged = handle.read()
try:
    with open("/work/f.txt", "w", encoding="utf-8") as handle:
        handle.write("seat-was-here\n")
    edited = ""
except OSError as exc:
    edited = errno.errorcode.get(exc.errno, str(exc.errno))
print(json.dumps({
    "mounts": mounts(),
    "scratch": fill("/tmp/limit.bin"),
    "checkout": fill("/work/limit.bin"),
    "root_write": create("/root-probe"),
    "dev_write": create("/dev/dev-probe"),
    "staged": staged,
    "edited": edited,
    "seat_file": create("/work/seat-new.txt"),
    "home_file": create("/home/agent/seat-probe"),
}))
'''

# A same-UID process that was told only where the *parent* of the run directory is: it finds the
# capability socket by enumeration, exactly as the audit did.
SEAT_CLIENT = r'''
import glob, json, sys
from review_loop import broker_ipc

found = sorted(glob.glob(sys.argv[1] + "/**/broker.sock", recursive=True))
assert found, "no broker socket reachable by enumeration"
print(json.dumps({"found": found}))
print(json.dumps(broker_ipc.request("push", manifest={}, socket_path=found[0])))
'''


def tmpfs_size(options: str) -> int | None:
    """Bytes from a ``size=`` mount option; ``None`` when the mount was never sized."""
    for part in options.split(","):
        if part.startswith("size="):
            value = part.removeprefix("size=")
            factor = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}.get(value[-1].lower(), 1)
            return int(value.rstrip("kKmMgG")) * factor
    return None


def bwrap_works() -> bool:
    """Whether this host can start an unprivileged namespace at all."""
    if not shutil.which("bwrap") or not os.path.exists("/usr/bin/python3"):
        return False
    try:
        return subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr",
                               "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
                               "--ro-bind-try", "/lib64", "/lib64", "--", "/usr/bin/true"],
                              capture_output=True, timeout=20).returncode == 0
    except OSError:
        return False


class LauncherLimitTests(unittest.TestCase):
    """The argv contract, checked without bubblewrap: every tmpfs this module mounts is sized."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.code = self.root / "code"
        self.code.mkdir(mode=0o700)
        self.checkout = self.root / "export"
        self.checkout.mkdir()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.query = self.root / "query.txt"
        self.query.write_text("probe\n")

    def kwargs(self, entry: list[str]) -> dict:
        return dict(code=self.code, venv=pathlib.Path("/usr"), runtime=pathlib.Path("/usr"),
                    home=self.home, checkout=self.checkout, rust=pathlib.Path("/usr"),
                    query=self.query, entry=entry)

    def test_launcher_sizes_every_tmpfs_it_mounts(self):
        for writable in (True, False):
            with self.subTest(checkout_writable=writable):
                argv = contained.command(checkout_writable=writable,
                                         **self.kwargs(["/usr/bin/true"]))
                sized = [index for index, arg in enumerate(argv) if arg == "--tmpfs"]
                self.assertTrue(sized)
                for index in sized:
                    self.assertEqual(argv[index - 2], "--size", argv[index - 2:index + 1])
                    self.assertTrue(argv[index - 1].isdigit(), argv[index - 1])


@unittest.skipUnless(bwrap_works(), "unprivileged bubblewrap unavailable")
class SandboxLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.code = self.root / "code"
        self.code.mkdir(mode=0o700)
        (self.code / "probe.py").write_text(PROBE)
        self.checkout = self.root / "export"
        (self.checkout / "src").mkdir(parents=True)
        (self.checkout / "f.txt").write_text("reviewed head\n")
        (self.checkout / "src" / "lib.rs").write_text("fn main() {}\n")
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.query = self.root / "query.txt"
        self.query.write_text("probe\n")

    def kwargs(self, entry: list[str]) -> dict:
        return dict(code=self.code, venv=pathlib.Path("/usr"), runtime=pathlib.Path("/usr"),
                    home=self.home, checkout=self.checkout, rust=pathlib.Path("/usr"),
                    query=self.query, entry=entry)

    def probe(self, *, limit: int = 0, tiny_sizes: bool = False) -> dict:
        """Run the real launcher through real bubblewrap and return the seat's own facts."""
        entry = ["/usr/bin/python3", "/opt/code/probe.py", str(limit)]
        with contextlib.ExitStack() as stack:
            if tiny_sizes:
                for name, value in (("SCRATCH_SIZE", TINY_SCRATCH), ("CHECKOUT_SIZE", TINY_CHECKOUT)):
                    stack.enter_context(mock.patch.object(contained, name, value, create=True))
            result = contained.run(timeout=120, **self.kwargs(entry))
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_real_writable_mounts_are_sized_and_the_root_is_not_writable(self):
        facts = self.probe()
        for where in ("/tmp", "/work"):
            mount = facts["mounts"][where]
            self.assertEqual(mount["type"], "tmpfs", where)
            size = tmpfs_size(mount["options"])
            # Not a bound if it is half of RAM: this host has 64 GiB, so the bubblewrap default is
            # 32 GiB. Whatever the exact number is, it has to be a small one.
            if size is None:
                self.fail(f"{where} must be a sized tmpfs, not {mount['options']}")
            # Still a bound and not the bwrap default (half of RAM, 32 GiB here), but /work is not
            # a small one: it holds the checkout plus a debug target that is 2.2-3.9 GiB in the
            # repos this loop watches. See test_the_caps_hold_a_real_rust_build below.
            self.assertLess(size, 16 * 1024 * MiB, where)
            if where == "/work":
                self.assertGreaterEqual(size, 4 * 1024 * MiB, where)
        self.assertIn("ro", facts["mounts"]["/"]["options"].split(","))
        # Enforced by the kernel, not merely requested: the seat's own writes are refused.
        self.assertEqual(facts["root_write"], "EROFS")
        self.assertEqual(facts["dev_write"], "EROFS")

    def test_the_caps_hold_a_real_rust_build(self):
        """A cap under a real debug target turns every Rust review into "could not verify"."""
        # Measured on this host: patchhive/attest's debug target is 2.2 GiB, two other real
        # workspaces' are 3.3 GiB and 3.9 GiB. The floor is here so a later "optimization" cannot
        # quietly shrink the bound back under a build the loop is expected to run.
        self.assertGreaterEqual(contained.CHECKOUT_SIZE, 8 * 1024 ** 3)
        self.assertGreaterEqual(contained.SCRATCH_SIZE, 2 * 1024 ** 3)

    def test_a_cap_can_be_overridden_and_a_bad_one_is_ignored(self):
        """Sizes are a host property, so the operator sets them without editing code."""
        with mock.patch.dict("os.environ", {"REVIEW_LOOP_CHECKOUT_SIZE_GIB": "3"}):
            self.assertEqual(contained._size_from_env("CHECKOUT_SIZE", 8), 3 * 1024 ** 3)
        with mock.patch.dict("os.environ", {"REVIEW_LOOP_CHECKOUT_SIZE_GIB": "lots of them"}):
            self.assertEqual(contained._size_from_env("CHECKOUT_SIZE", 8), 8 * 1024 ** 3)
        with mock.patch.dict("os.environ", {"REVIEW_LOOP_CHECKOUT_SIZE_GIB": "0"}):
            self.assertEqual(contained._size_from_env("CHECKOUT_SIZE", 8), 8 * 1024 ** 3)

    def test_a_refusal_stops_being_reported_once_the_value_is_gone(self):
        """The record is process-wide: a refusal must not outlive the bad value that caused it.

        A malformed override provoked by one test used to leave every later selftest in the same
        process refusing to call itself green, and the record grew one entry per resolution.
        """
        from review_loop import contained
        with mock.patch.dict("os.environ", {"REVIEW_LOOP_CHECKOUT_SIZE_GIB": "0"}):
            contained._size_from_env("CHECKOUT_SIZE", 8)
            self.assertEqual(len(contained.live_ignored_overrides()), 1,
                             "a bad value is reported while it is set")
            contained._size_from_env("CHECKOUT_SIZE", 8)
            self.assertEqual(len(contained.live_ignored_overrides()), 1,
                             "and a second resolution does not duplicate it")
        self.assertEqual(contained.live_ignored_overrides(), [],
                         "a corrected value stops being reported")
        self.assertEqual(contained._size_from_env("CHECKOUT_SIZE", 8), 8 * 1024 ** 3)

    def test_real_sized_budgets_stop_a_runaway_writer(self):
        facts = self.probe(limit=WRITE_LIMIT, tiny_sizes=True)
        for name, budget, where in (("scratch", TINY_SCRATCH, "/tmp"),
                                    ("checkout", TINY_CHECKOUT, "/work")):
            with self.subTest(mount=where):
                self.assertEqual(facts[name]["error"], "ENOSPC", facts[name])
                self.assertLessEqual(facts[name]["written"], budget)
        # The size asked for is the size mounted: the constants are what the kernel honours, and
        # no later edit may quietly drop them (``--tmpfs DEST,size=N`` is not accepted everywhere
        # and would mount nothing at /tmp at all).
        self.assertEqual(tmpfs_size(facts["mounts"]["/tmp"]["options"]), TINY_SCRATCH)
        self.assertEqual(tmpfs_size(facts["mounts"]["/work"]["options"]), TINY_CHECKOUT)
        self.assertEqual(facts["staged"], "reviewed head\n")

    def test_real_seat_writes_never_reach_the_staged_export(self):
        facts = self.probe()
        self.assertEqual(facts["staged"], "reviewed head\n")
        self.assertEqual(facts["edited"], "")
        self.assertEqual(facts["seat_file"], "")
        # The seat edited /work and created a file there; the host's export is the staged head.
        self.assertEqual((self.checkout / "f.txt").read_text(), "reviewed head\n")
        self.assertEqual(sorted(path.name for path in self.checkout.iterdir()),
                         ["f.txt", "src"])

    def test_real_home_is_still_a_writable_host_bind(self):
        """The write surface left open on purpose: the host reads what the seat wrote."""
        facts = self.probe()
        self.assertEqual(facts["home_file"], "")
        self.assertTrue((self.home / "seat-probe").exists())

class BrokerClaimTests(unittest.TestCase):
    def test_docstring_states_the_bound_the_socket_actually_has(self):
        """Pin issue #89's claim-vs-code gap.

        "A per-run, unguessable path ... provide[s] authentication" is the overclaim: the path is
        enumerable by any same-UID process (proved below), so the docstring must name the bound it
        has — socket mode and directory mode against other UIDs, nothing against the same UID.
        """
        doc = (broker_ipc.__doc__ or "").lower()
        for phrase in ("bearer capability", "same uid", "no accept-time credential check",
                       "enumerable", "not an extra privilege"):
            self.assertIn(phrase, doc, phrase)
        self.assertNotIn("unguessable", doc)

    def test_same_uid_process_reaches_the_socket_by_enumeration(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            root = pathlib.Path(temp)
            directory = root / "rl-01234567" / "b"
            directory.mkdir(mode=0o700, parents=True)
            scope = broker_ipc.RunScope("acme/widgets", 7, "a" * 40, "reviewer", "fix-7")
            server = broker_ipc.RunBroker({"repo": "acme/widgets"}, scope, directory)
            with server:
                thread = broker_ipc.serve_in_thread(server)
                try:
                    client = subprocess.run(
                        [sys.executable, "-c", SEAT_CLIENT, str(root)],
                        capture_output=True, text=True, timeout=30,
                        env={**os.environ, "PYTHONPATH": str(ROOT)})
                    self.assertEqual(client.returncode, 0, client.stderr[-2000:])
                    lines = client.stdout.strip().splitlines()
                    reachable = json.loads(lines[-2])["found"]
                    answer = json.loads(lines[-1])
                    # Same UID, no credential, no accept-time check: only mode bits keep out
                    # other UIDs, and they are all this socket is protected by.
                    modes = {path: stat.S_IMODE(os.stat(path).st_mode) for path in reachable}
                finally:
                    server.close()
                    thread.join(timeout=5)
        self.assertTrue(server.socket_path is None)  # closed with the run
        self.assertTrue(reachable)
        for path, mode in modes.items():
            self.assertTrue(path.startswith(str(root)), path)
            self.assertEqual(mode, 0o600, path)
        # It reached the real broker: the refusal is the RunScope's operation bound, not a
        # refusal to connect.
        self.assertEqual(answer, {"ok": False, "error": "operation out of scope"})

    def test_a_refused_override_is_recorded_for_doctor(self):
        """A bound nobody can parse must not vanish into an unattended turn's stderr."""
        self.addCleanup(contained.IGNORED_SIZE_OVERRIDES.clear)
        with mock.patch.dict("os.environ", {"REVIEW_LOOP_CHECKOUT_SIZE_GIB": "sixteen"}):
            self.assertEqual(contained._size_from_env("CHECKOUT_SIZE", 8), 8 * 1024 ** 3)
        self.assertTrue(any(name == "CHECKOUT_SIZE" and raw == "sixteen" and why
                            for name, raw, why in contained.IGNORED_SIZE_OVERRIDES))

    def test_doctor_reports_the_caps_and_the_worst_case(self):
        """The caps are tmpfs, so they are a memory budget, not a disk one."""
        from review_loop import doctor
        # This loop names no seats: one turn each, plus the adjudicator.
        # A refused override is its own check, so it must not colour the ordinary lines here:
        # the list is filled once at import, from this process's environment.
        with mock.patch.object(contained, "IGNORED_SIZE_OVERRIDES", []), \
                mock.patch.object(doctor, "_host_memory",
                                  return_value=(64 * 1024 ** 3, 64 * 1024 ** 3)):
            check = doctor.check_sandbox_caps({"id": "t"})
        self.assertEqual(check.name, "sandbox:caps")
        self.assertEqual(check.status, "verified")
        self.assertIn("8.0 GiB", check.detail)
        self.assertIn("3 concurrent turns", check.detail)
        with mock.patch.object(contained, "IGNORED_SIZE_OVERRIDES", []), \
                mock.patch.object(doctor, "_host_memory",
                                  return_value=(4 * 1024 ** 3, 8 * 1024 ** 3)):
            small = doctor.check_sandbox_caps({"id": "t"})
        self.assertEqual(small.status, "mismatch")
        self.assertIn("more than the 4.0 GiB available", small.detail)
        self.assertIn("one at a time", small.fix)
        with mock.patch.object(contained, "IGNORED_SIZE_OVERRIDES",
                               [("CHECKOUT_SIZE", "sixteen", "not an integer")]), \
                mock.patch.dict("os.environ", {"REVIEW_LOOP_CHECKOUT_SIZE_GIB": "sixteen"}):
            refused = doctor.check_sandbox_caps({"id": "t"})
        self.assertEqual(refused.status, "mismatch")
        self.assertIn("REVIEW_LOOP_CHECKOUT_SIZE_GIB", refused.detail)
        self.assertIn("fix the value", refused.fix)
        # The record is process-wide and the resolution is read once per process, so a refusal
        # must stop being reported once the value is gone — otherwise one bad value seen once
        # leaves a healthy install red for the life of the process (and every later test in it).
        with mock.patch.object(contained, "IGNORED_SIZE_OVERRIDES",
                               [("CHECKOUT_SIZE", "sixteen", "not an integer")]):
            corrected = doctor.check_sandbox_caps({"id": "t"})
        self.assertEqual(corrected.status, "verified",
                         "a corrected override must not leave the install red forever")


if __name__ == "__main__":
    unittest.main()
