"""Cross-process and failure-path regression tests for the shared webhook registry."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import multiprocessing
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import routes


def add(path: str, name: str, start) -> None:
    os.environ["REVIEW_LOOP_SUBS"] = path
    start.wait()
    routes.new_route(name, profile="default", prompt="test", events=["push"],
                     script="test.py", deliver="none")


def remove(path: str, name: str, start) -> None:
    os.environ["REVIEW_LOOP_SUBS"] = path
    start.wait()
    if not routes.remove_route(name):
        raise RuntimeError(f"route {name} disappeared")


class RouteRegistryTest(unittest.TestCase):
    def setUp(self):
        # The main suite resets its HERMES_HOME fixture between groups; use stable scratch.
        scratch = pathlib.Path(os.environ.get("TMPDIR", pathlib.Path.home() / ".hermes/cache/scratch"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "subscriptions.json"
        self.old = os.environ.get("REVIEW_LOOP_SUBS")
        os.environ["REVIEW_LOOP_SUBS"] = str(self.path)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        if self.old is None:
            os.environ.pop("REVIEW_LOOP_SUBS", None)
        else:
            os.environ["REVIEW_LOOP_SUBS"] = self.old

    def create(self, name="one"):
        return routes.new_route(name, profile="default", prompt="test", events=["push"],
                                script="test.py", deliver="none")

    def workers(self, operations):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        jobs = [ctx.Process(target=fn, args=(str(self.path), name, start))
                for fn, name in operations]
        for job in jobs:
            job.start()
        try:
            start.set()
            for job in jobs:
                job.join(30)
            self.assertTrue(all(job.exitcode == 0 for job in jobs),
                            [job.exitcode for job in jobs])
        finally:
            for job in jobs:
                if job.is_alive():
                    job.terminate()
                    job.join()

    def test_parallel_independent_creates_and_removes_preserve_every_edit(self):
        old = {f"old-{n}": {"secret": f"original-{n}"} for n in range(12)}
        self.path.write_text(json.dumps(old))
        self.workers([(add, f"new-{n}") for n in range(12)] +
                     [(remove, f"old-{n}") for n in range(12)])
        data = json.loads(self.path.read_text())
        self.assertEqual(set(data), {f"new-{n}" for n in range(12)})
        self.assertEqual(len({entry["secret"] for entry in data.values()}), 12)

    def test_failed_replace_leaves_original_bytes_and_no_temp(self):
        self.create()
        original = self.path.read_bytes()
        with mock.patch.object(routes.os, "replace", side_effect=OSError("injected replace")):
            with self.assertRaises(OSError) as create_error:
                self.create("two")
            with self.assertRaises(OSError) as remove_error:
                routes.remove_route("one")
        self.assertNotIsInstance(create_error.exception, routes.RegistryDurabilityError)
        self.assertNotIsInstance(remove_error.exception, routes.RegistryDurabilityError)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(set(self.path.parent.iterdir()) - {self.path, self.path.with_name(self.path.name + ".lock")}, set())

    def test_failed_mid_write_leaves_original_bytes_and_no_temp(self):
        self.create()
        original = self.path.read_bytes()
        real_write = os.write
        def broken_write(fd, data):
            real_write(fd, data[:min(5, len(data))])
            raise OSError("injected write failure")
        with mock.patch.object(routes.os, "write", side_effect=broken_write):
            with self.assertRaises(OSError) as create_error:
                self.create("two")
            with self.assertRaises(OSError) as remove_error:
                routes.remove_route("one")
        self.assertNotIsInstance(create_error.exception, routes.RegistryDurabilityError)
        self.assertNotIsInstance(remove_error.exception, routes.RegistryDurabilityError)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(set(self.path.parent.iterdir()) - {self.path, self.path.with_name(self.path.name + ".lock")}, set())

    def test_failed_directory_sync_reports_published_but_unconfirmed(self):
        self.create()
        original = self.path.read_bytes()
        original_inode = self.path.stat().st_ino
        real_fsync = os.fsync
        calls = []

        def fail_directory_sync(fd):
            calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            if calls[-1]:
                raise OSError("injected directory fsync failure")
            return real_fsync(fd)

        with mock.patch.object(routes.os, "fsync", side_effect=fail_directory_sync):
            with self.assertRaises(routes.RegistryDurabilityError) as raised:
                self.create("two")
        self.assertEqual(calls, [False, True])
        self.assertTrue(raised.exception.published)
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn("injected directory fsync failure", str(raised.exception.__cause__))
        self.assertNotEqual(self.path.read_bytes(), original)
        self.assertNotEqual(self.path.stat().st_ino, original_inode)
        self.assertEqual(set(json.loads(self.path.read_text())), {"one", "two"})
        self.assertEqual(set(self.path.parent.iterdir()) - {self.path, self.path.with_name(self.path.name + ".lock")}, set())

    def test_malformed_existing_json_fails_closed_for_both_writers(self):
        original = b'{"one": {"secret": "keep"}, broken'
        self.path.write_bytes(original)
        with self.assertRaises((ValueError, TypeError)):
            self.create("two")
        with self.assertRaises((ValueError, TypeError)):
            routes.remove_route("one")
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text("[]")
        with self.assertRaises((ValueError, TypeError)):
            self.create("two")
        with self.assertRaises((ValueError, TypeError)):
            routes.remove_route("one")

    def test_owner_only_creation_and_update_preserve_secret(self):
        old_umask = os.umask(0)
        try:
            entry = self.create()
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            secret = entry["secret"]
            os.chmod(self.path, 0o666)
            self.assertEqual(self.create()["secret"], secret)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            os.chmod(self.path, 0o666)
            self.assertTrue(routes.remove_route("one"))
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        finally:
            os.umask(old_umask)


if __name__ == "__main__":
    unittest.main()
