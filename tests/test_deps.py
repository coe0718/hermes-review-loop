"""Issue #51: host-side dependency prefetch, read-only offline cache, and what the seat is told."""
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from review_loop import contained, deps  # noqa: E402

CRATES = "registry+https://github.com/rust-lang/crates.io-index"
LOCK = f'''version = 4

[[package]]
name = "itoa"
version = "1.0.18"
source = "{CRATES}"
checksum = "8f42a60cbdf9a97f5d2305f08a87dc4e09308d1276d28c869c684d7777685682"

[[package]]
name = "tiny"
version = "0.1.0"
dependencies = ["itoa"]
'''
MANIFEST = '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\nitoa = "1"\n'


def _package(name, version, source):
    return f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = "{source}"\n'


def _toolchain() -> Path:
    if os.environ.get("REVIEW_LOOP_TEST_RUST"):
        return Path(os.environ["REVIEW_LOOP_TEST_RUST"])
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".rustup/toolchains/stable-x86_64-unknown-linux-gnu"


def _crates_io_reachable() -> bool:
    try:
        socket.create_connection(("index.crates.io", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        return subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr", "--ro-bind",
                               "/bin", "/bin", "--ro-bind", "/lib", "/lib", "--ro-bind-try",
                               "/lib64", "/lib64", "--", "/usr/bin/true"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.checkout = self.root / "export"
        (self.checkout / "src").mkdir(parents=True)
        (self.checkout / "src/lib.rs").write_text("")
        self.cache_parent = self.root / "deps"
        self.cache_parent.mkdir(mode=0o700)

    def write(self, lock=LOCK, manifest=MANIFEST):
        if manifest is not None:
            (self.checkout / "Cargo.toml").write_text(manifest)
        if lock is not None:
            (self.checkout / "Cargo.lock").write_text(lock)


class LockfileTests(Base):
    def test_only_crates_io_pairs_are_taken_and_path_packages_skipped(self):
        self.write(LOCK + _package("ryu", "1.0.0-rc.1+b", "sparse+https://index.crates.io/"))
        self.assertEqual(deps.locked_crates(self.checkout / "Cargo.lock"),
                         [("itoa", "1.0.18"), ("ryu", "1.0.0-rc.1+b")])

    def test_git_and_other_registries_are_refused(self):
        for source in ("git+https://evil.example/x.git#abc", "git+ssh://git@host/x#abc",
                       "registry+https://evil.example/index", "sparse+https://evil.example/"):
            with self.subTest(source=source):
                self.write(LOCK + _package("x", "1.0.0", source))
                with self.assertRaisesRegex(ValueError, "non-crates.io"):
                    deps.locked_crates(self.checkout / "Cargo.lock")

    def test_names_and_versions_cannot_inject_into_the_synthetic_manifest(self):
        for name, version in (('a"b', "1.0.0"), ("a", '1.0.0", path = "/etc'), ("a b", "1.0.0"),
                              ("a", "1.0"), ("a", "^1.0.0"), ("-a", "1.0.0")):
            with self.subTest(name=name, version=version):
                (self.checkout / "Cargo.lock").write_text(
                    "[[package]]\nname = " + json.dumps(name) + "\nversion = " + json.dumps(version)
                    + f'\nsource = "{CRATES}"\n')
                with self.assertRaises(ValueError):
                    deps.locked_crates(self.checkout / "Cargo.lock")

    def test_bounds_and_malformed(self):
        (self.checkout / "Cargo.lock").write_text("not = [toml")
        with self.assertRaises(ValueError):
            deps.locked_crates(self.checkout / "Cargo.lock")
        many = "".join(_package(f"c{i}", "1.0.0", CRATES) for i in range(deps.MAX_PACKAGES + 1))
        (self.checkout / "Cargo.lock").write_text(many)
        with self.assertRaisesRegex(ValueError, "more than"):
            deps.locked_crates(self.checkout / "Cargo.lock")
        (self.checkout / "Cargo.lock").unlink()
        (self.checkout / "Cargo.lock").symlink_to("/etc/hostname")
        with self.assertRaises(ValueError):
            deps.locked_crates(self.checkout / "Cargo.lock")

    def test_synthetic_manifest_pins_exact_versions(self):
        text = deps.synthetic_manifest([("itoa", "1.0.18"), ("ryu", "1.0.0")])
        self.assertIn('d0 = { package = "itoa", version = "=1.0.18", default-features = false }', text)
        self.assertIn('d1 = { package = "ryu", version = "=1.0.0"', text)
        self.assertNotIn("path", text)
        self.assertNotIn("git", text)


FAKE_CARGO = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
home = pathlib.Path(os.environ["CARGO_HOME"])
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ),
          "manifest": pathlib.Path("Cargo.toml").read_text(),
          "config_in_ancestry": [str(p) for p in pathlib.Path.cwd().parents
                                 if (p / ".cargo").exists()]}
(home / "record.json").write_text(json.dumps(record))
mode = os.environ.get("FAKE_MODE") or pathlib.Path(__file__).with_name("mode").read_text()
if mode == "ok":
    d = home / "registry/cache/index.crates.io-1949cf8c6b5b557f"
    d.mkdir(parents=True, exist_ok=True)
    (d / "itoa-1.0.18.crate").write_bytes(b"x")
elif mode == "fail":
    print("error: failed to download from index.crates.io")
    sys.exit(101)
elif mode == "sleep":
    import time; time.sleep(30)
'''


class PrefetchTests(Base):
    def setUp(self):
        super().setUp()
        self.rust = self.root / "rust"
        (self.rust / "bin").mkdir(parents=True)
        cargo = self.rust / "bin/cargo"
        cargo.write_text(FAKE_CARGO)
        cargo.chmod(0o755)
        (self.rust / "bin/rustc").write_text("")
        self.mode("ok")

    def mode(self, value):
        (self.rust / "bin/mode").write_text(value)

    def record(self):
        return json.loads((self.cache_parent / "cargo/record.json").read_text())

    def test_not_a_rust_head(self):
        self.assertIsNone(deps.prefetch_rust(self.checkout, self.cache_parent, self.rust))
        self.assertEqual(deps.prepare(self.checkout, self.cache_parent, self.rust), [])

    def test_manifest_without_lockfile_is_unavailable(self):
        self.write(lock=None)
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual((result.status, result.cache), (deps.UNAVAILABLE, None))
        self.assertIn("no Cargo.lock", result.reason)

    def test_git_source_never_runs_cargo(self):
        self.write(LOCK + _package("x", "1.0.0", "git+https://evil.example/x#a"))
        with mock.patch.object(deps, "bounded_run", side_effect=AssertionError("ran cargo")):
            result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("non-crates.io", result.reason)

    def test_no_registry_crates_is_ready_without_cargo(self):
        self.write('version = 4\n[[package]]\nname = "tiny"\nversion = "0.1.0"\n')
        with mock.patch.object(deps, "bounded_run", side_effect=AssertionError("ran cargo")):
            result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertTrue(result.ready)
        self.assertTrue((result.cache / "registry").is_dir())

    def test_fetch_runs_on_a_synthetic_manifest_with_a_scratch_environment(self):
        self.write()
        # Everything the PR controls that cargo would otherwise honour.
        (self.checkout / ".cargo").mkdir()
        (self.checkout / ".cargo/config.toml").write_text('[build]\nrustc-wrapper = "/bin/false"\n')
        (self.checkout / "build.rs").write_text("fn main() {}")
        (self.checkout / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "nightly"\n')
        parent_env = {"GH_TOKEN": "ghp_dummyparenttoken000000000", "GITHUB_TOKEN": "x",
                      "CARGO_REGISTRY_TOKEN": "x", "SSH_AUTH_SOCK": "/tmp/agent",
                      "HTTPS_PROXY": "http://proxy.example", "RUSTC_WRAPPER": "/bin/false",
                      "CARGO_HOME": "/somewhere/else", "RUSTUP_TOOLCHAIN": "nightly"}
        with mock.patch.dict(os.environ, parent_env):
            result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertTrue(result.ready, result)
        self.assertEqual(result.cache, self.cache_parent / "cargo")
        record = self.record()
        self.assertEqual(record["argv"], ["fetch"])
        self.assertEqual(record["manifest"], deps.synthetic_manifest([("itoa", "1.0.18")]))
        cwd = Path(record["cwd"])
        self.assertNotEqual(cwd, self.checkout)
        self.assertNotIn(self.checkout, cwd.parents)
        self.assertEqual(record["config_in_ancestry"], [])
        env = record["env"]
        for name in parent_env:
            if name != "CARGO_HOME":
                self.assertNotIn(name, env)
        self.assertEqual(env["CARGO_HOME"], str(self.cache_parent / "cargo"))
        self.assertEqual(env["RUSTC"], str(self.rust / "bin/rustc"))
        self.assertNotEqual(env["HOME"], os.environ.get("HOME"))
        self.assertFalse(Path(env["HOME"]).exists())       # a throwaway, removed afterwards
        self.assertFalse(cwd.exists())

    def test_failed_or_incomplete_fetch_is_unavailable_with_detail(self):
        self.write()
        self.mode("fail")
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("failed", result.reason)
        self.assertIn("failed to download", result.detail)
        self.mode("noop")                               # rc 0 but nothing downloaded
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("1 of 1 crates missing", result.reason)

    def test_timeout_kills_the_fetch(self):
        self.write()
        self.mode("sleep")
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, timeout=1)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("timed out", result.reason)

    def test_output_is_bounded(self):
        rc, tail = deps.bounded_run([sys.executable, "-c", "print('x' * 1000000)"],
                                    env={"PATH": "/usr/bin:/bin"}, cwd=self.root, timeout=20,
                                    limit=1000)
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(tail), 1000)

    def test_a_crashing_prefetcher_is_unavailable_not_raised(self):
        self.write()
        with mock.patch.dict(deps.ECOSYSTEMS, {"rust": mock.Mock(side_effect=RuntimeError("x"))}):
            [result] = deps.prepare(self.checkout, self.cache_parent, self.rust)
        self.assertEqual((result.ecosystem, result.status), ("rust", deps.UNAVAILABLE))

    def test_unusable_cache_is_unavailable_without_cargo(self):
        self.write()
        with mock.patch.object(deps, "bounded_run", side_effect=AssertionError("ran cargo")):
            [result] = deps.prepare(self.checkout, None, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("cache is unusable", result.reason)

    def test_cache_root_is_private(self):
        loop = {"state_dir": str(self.root / "state")}
        self.assertEqual(deps.cache_root(loop), self.root / "state/deps")
        self.assertEqual((self.root / "state/deps").stat().st_mode & 0o777, 0o700)
        (self.root / "state/deps").chmod(0o755)
        with self.assertRaises(PermissionError):
            deps.cache_root(loop)


class SeatNoteTests(unittest.TestCase):
    def test_unavailable_tells_the_reviewer_to_judge_by_reading(self):
        note = deps.seat_note([deps.Prefetch("rust", deps.UNAVAILABLE, "no network")], "reviewer")
        self.assertIn("NOT available", note)
        self.assertIn("no network", note)
        self.assertIn("Judge by reading", note)
        self.assertIn("not by itself a reason to request changes", note)
        fixer = deps.seat_note([deps.Prefetch("rust", deps.UNAVAILABLE, "x")], "fixer")
        self.assertIn("unbuilt", fixer)
        self.assertIn("answers", fixer)  # the summary is not published; the answers are (#52)
        adjudicator = deps.seat_note([deps.Prefetch("rust", deps.UNAVAILABLE, "x")], "adjudicator")
        self.assertIn("not evidence either way", adjudicator)

    def test_ready_and_absent(self):
        note = deps.seat_note([deps.Prefetch("rust", deps.READY, "3 crates", Path("/c"))], "reviewer")
        self.assertIn("available offline", note)
        self.assertNotIn("NOT", note)
        self.assertEqual(deps.seat_note([], "reviewer"), "")


class ContainedMountTests(Base):
    def layout(self):
        base = Path(tempfile.mkdtemp(dir=self.root))
        paths = {name: base / name for name in
                 ("code", "venv", "runtime", "home", "checkout", "rust", "query")}
        for directory in paths.values():
            directory.mkdir()
        return paths

    def test_cache_is_mounted_read_only_inside_the_tmpfs_and_offline_is_always_set(self):
        cache = self.cache_parent / "cargo"
        (cache / "registry").mkdir(parents=True)
        argv = contained.command(**self.layout(), entry=["true"],
                                 dependency_caches={"rust": cache})
        bind = argv.index(str(cache / "registry"))
        self.assertEqual(argv[bind - 1:bind + 2],
                         ["--ro-bind", str(cache / "registry"), "/tmp/cargo/registry"])
        self.assertLess(argv.index("/tmp"), bind)          # after --tmpfs /tmp, not shadowed by it
        self.assertEqual(argv[argv.index("CARGO_HOME") + 1], "/tmp/cargo")
        self.assertEqual(argv[argv.index("CARGO_NET_OFFLINE") + 1], "true")
        bare = contained.command(**self.layout(), entry=["true"])
        self.assertEqual(bare[bare.index("CARGO_NET_OFFLINE") + 1], "true")
        self.assertNotIn("/tmp/cargo/registry", bare)      # no cache, no mount

    def test_cache_and_review_diff_mount_together(self):
        # #50 and #51 in one turn: the reviewer's diff and the crate cache are both read-only.
        cache = self.cache_parent / "cargo"
        (cache / "registry").mkdir(parents=True)
        review = Path(tempfile.mkdtemp(dir=self.root))
        (review / "pr.diff").write_text("diff --git a/x b/x\n")
        argv = contained.command(**self.layout(), entry=["true"],
                                 dependency_caches={"rust": cache}, review_dir=review)
        bind = argv.index(str(cache / "registry"))
        self.assertEqual(argv[bind - 1:bind + 2],
                         ["--ro-bind", str(cache / "registry"), "/tmp/cargo/registry"])
        diff = argv.index(str(review))
        self.assertEqual(argv[diff - 1:diff + 2], ["--ro-bind", str(review), "/opt/review"])

    def test_unknown_ecosystem_or_missing_cache_is_refused(self):
        with self.assertRaises(ValueError):
            contained.command(**self.layout(), entry=["true"],
                              dependency_caches={"npm": self.cache_parent})
        with self.assertRaises(FileNotFoundError):
            contained.command(**self.layout(), entry=["true"],
                              dependency_caches={"rust": self.cache_parent / "absent"})


@unittest.skipUnless((_toolchain() / "bin/cargo").exists(), "stable Rust toolchain unavailable")
@unittest.skipUnless(_bwrap_works(), "unprivileged bubblewrap unavailable")
@unittest.skipUnless(_crates_io_reachable(), "crates.io unreachable (no network)")
class RealPrefetchAndOfflineBuild(Base):
    """The whole point: real cargo fetch on the host, real offline build in the real sandbox."""

    def test_seat_builds_offline_with_the_cache_and_cannot_without_it(self):
        self.write()
        rust = _toolchain()
        [result] = deps.prepare(self.checkout, self.cache_parent, rust, timeout=240)
        self.assertTrue(result.ready, (result.reason, result.detail))
        layout = {name: self.root / name for name in ("code", "venv", "runtime", "home")}
        for directory in layout.values():
            directory.mkdir()
        query = self.root / "query"
        query.write_text("x")
        script = ("touch /tmp/cargo/registry/poison 2>/dev/null && echo CACHE-WRITABLE; "
                  "cargo metadata --format-version 1 --locked >/dev/null && echo METADATA-OK; "
                  "cargo build --offline --locked -j 2 && echo BUILD-OK")

        def build(caches):
            shutil.rmtree(self.checkout / "target", ignore_errors=True)
            return contained.run(**layout, checkout=self.checkout, rust=rust, query=query,
                                 entry=["/bin/sh", "-c", script], timeout=240,
                                 dependency_caches=caches)

        good = build({"rust": result.cache})
        self.assertEqual(good.returncode, 0, good.stderr[-2000:])
        self.assertIn("METADATA-OK", good.stdout)
        self.assertIn("BUILD-OK", good.stdout)
        self.assertNotIn("CACHE-WRITABLE", good.stdout)
        self.assertIn("Compiling itoa", good.stderr)
        bare = build({})
        self.assertNotEqual(bare.returncode, 0)
        self.assertNotIn("BUILD-OK", bare.stdout)
        self.assertIn("itoa", bare.stderr)


if __name__ == "__main__":
    unittest.main()
