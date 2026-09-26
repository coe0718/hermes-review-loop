"""Offline GitHub transport fixtures for the trusted fetch boundary."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from review_loop import trusted_fetch, trusted_turn


class CommittedSourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.destination = self.root / 'snapshot'
        self.git('init', '-q')
        (self.source / 'run_agent.py').write_text('committed\n')
        (self.source / 'module.py').write_text('original\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args])

    def test_uses_head_not_dirty_or_staged_files(self):
        (self.source / 'run_agent.py').write_text('dirty secret\n')
        (self.source / 'module.py').write_text('staged secret\n')
        self.git('add', 'module.py')
        trusted_turn._safe_code_snapshot(self.source, self.destination)
        self.assertEqual((self.destination / 'run_agent.py').read_text(), 'committed\n')
        self.assertEqual((self.destination / 'module.py').read_text(), 'original\n')

    def test_symlink_swap_cannot_export_external_file(self):
        external = self.root / 'external.py'
        external.write_text('EXTERNAL SECRET\n')
        (self.source / 'module.py').unlink()
        (self.source / 'module.py').symlink_to(external)
        trusted_turn._safe_code_snapshot(self.source, self.destination)
        self.assertEqual((self.destination / 'module.py').read_text(), 'original\n')

    def test_nested_hidden_and_credentials_are_not_exported(self):
        for name in ('.private/token.py', 'pkg/.hidden.py', 'pkg/CONFIG.YAML',
                     'pkg/credentials/key.py', 'pkg/normal.py'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('secret\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'more')
        trusted_turn._safe_code_snapshot(self.source, self.destination)
        for name in ('.private/token.py', 'pkg/.hidden.py', 'pkg/CONFIG.YAML',
                     'pkg/credentials/key.py'):
            self.assertFalse((self.destination / name).exists(), name)
        self.assertTrue((self.destination / 'pkg/normal.py').is_file())

    def test_committed_symlink_and_source_symlink_denied(self):
        (self.source / 'linked.py').symlink_to(self.root / 'external.py')
        self.git('add', 'linked.py')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'link')
        with self.assertRaisesRegex(trusted_turn.TurnDenied, 'nonregular'):
            trusted_turn._safe_code_snapshot(self.source, self.destination)
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(trusted_turn.TurnDenied, 'invalid source'):
            trusted_turn._safe_code_snapshot(alias, self.root / 'other')

    def test_destination_directory_swap_cannot_write_through_symlink(self):
        (self.source / 'pkg').mkdir()
        (self.source / 'pkg/normal.py').write_text('safe\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'nested')
        outside = self.root / 'outside'
        outside.mkdir()
        real_mkdir = trusted_turn.os.mkdir
        def swap(path, *args, **kwargs):
            if path == 'pkg' and kwargs.get('dir_fd') is not None:
                (self.destination / 'pkg').symlink_to(outside, target_is_directory=True)
                raise FileExistsError(path)
            return real_mkdir(path, *args, **kwargs)
        with mock.patch.object(trusted_turn.os, 'mkdir', side_effect=swap):
            with self.assertRaises(OSError):
                trusted_turn._safe_code_snapshot(self.source, self.destination)
        self.assertFalse((outside / 'normal.py').exists())


class Response:
    def __init__(self, data, length=None):
        self.stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}
        self.status = 200
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        self.read_sizes.append(size)
        return self.stream.read(size)


class TrustedFetchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.paths = {}
        for name in ("reader", "reviewer", "fixer"):
            path = self.root / (name + ".key")
            path.write_text("dummy-" + name)
            self.paths[name] = str(path)
        self.loop = {"repo": "acme/widgets", "base": "main", "read_token": "reader",
                     "tokens": self.paths, "seats": {"reviewer": {"login": "reviewer"},
                                                   "fixer": {"login": "fixer"}}}
        self.blob = b"safe\n"
        self.oid = hashlib.sha1(b"blob 5\0" + self.blob).hexdigest()
        self.head = "a" * 40
        self.tree_sha = "b" * 40
        self.tree = {"sha": self.tree_sha, "truncated": False,
                     "tree": [{"path": "hello.txt", "mode": "100644", "type": "blob",
                               "sha": self.oid, "size": len(self.blob)}]}
        self.calls = []
        self.pr_count = 0
        self.pr_head = self.head
        self.kw = {"repo": "acme/widgets", "number": 7, "head": self.head,
                   "ref": "work", "role": "reviewer", "sandbox_root": self.root / "sandbox"}

    def response(self, loop, path, login, limit, accept):
        self.calls.append((path, login, limit))
        if path == "/user":
            return json.dumps({"login": login}).encode()
        if path.endswith("/pulls/7"):
            self.pr_count += 1
            return json.dumps({"number": 7, "state": "open", "head": {"sha": self.pr_head,
                "ref": "work", "repo": {"full_name": "acme/widgets"}},
                "base": {"ref": "main", "repo": {"full_name": "acme/widgets"}}}).encode()
        if path.endswith("/git/commits/" + self.head):
            return json.dumps({"sha": self.head, "tree": {"sha": self.tree_sha}}).encode()
        if "/git/trees/" in path:
            return json.dumps(self.tree).encode()
        if "/git/blobs/" in path:
            return self.blob
        raise AssertionError(path)

    def stage(self, callback=None, **changes):
        with mock.patch.object(trusted_fetch, "_request", side_effect=callback or self.response):
            return trusted_fetch._stage(self.loop, **{**self.kw, **changes})

    def test_exact_export_no_credentials_and_three_head_checks(self):
        result = self.stage()
        self.assertEqual((result / "hello.txt").read_bytes(), self.blob)
        self.assertFalse((result / ".git").exists())
        self.assertEqual(self.pr_count, 3)
        self.assertEqual([login for path, login, _ in self.calls if path == "/user"],
                         ["reader", "reviewer", "fixer"])
        self.assertFalse(any(self.root.glob(".review-trusted-*")))

    def test_oversized_response_stopped_before_download(self):
        response = Response(b"x" * 100, length=0)
        with mock.patch.object(trusted_fetch.gh, "token", return_value="dummy"), \
                mock.patch.object(trusted_fetch.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(trusted_fetch.FetchDenied, "bounds"):
                trusted_fetch._request(self.loop, "/repos/acme/widgets/git/trees/x", "reader", 8,
                                       "application/vnd.github+json")
        self.assertEqual(response.stream.tell(), 9)
        self.assertEqual(response.read_sizes, [9])
        response = Response(b"x" * 100, length=100)
        with mock.patch.object(trusted_fetch.gh, "token", return_value="dummy"), \
                mock.patch.object(trusted_fetch.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(trusted_fetch.FetchDenied, "bounds"):
                trusted_fetch._request(self.loop, "/user", "reader", 8, "application/vnd.github+json")
        self.assertEqual(response.stream.tell(), 0)

    def test_deleted_large_blob_never_fetched(self):
        # A historical oversized blob, modeled by an excluded OID, is never requested.
        historical_oid = "d" * 40
        def only_head(loop, path, login, limit, accept):
            self.assertNotIn(historical_oid, path)
            return self.response(loop, path, login, limit, accept)
        self.stage(callback=only_head)
        self.assertEqual([path for path, _, _ in self.calls if "/git/blobs/" in path],
                         [f"/repos/acme/widgets/git/blobs/{self.oid}"])

    def test_truncated_malformed_and_unsafe_tree_rejected(self):
        valid = self.tree["tree"][0]
        for change in ({"truncated": True}, {"tree": [{**valid, "mode": "120000"}]},
                       {"tree": [{**valid, "type": "commit", "mode": "160000"}]},
                       {"tree": [{**valid, "path": "../escape"}]},
                       {"tree": [{**valid, "size": trusted_fetch._MAX_BYTES + 1}]},
                       {"tree": [valid, valid]},
                       {"tree": [{**valid, "path": "dir"}, {**valid, "path": "dir/file"}]}):
            with self.subTest(change=change), self.assertRaises(trusted_fetch.FetchDenied):
                trusted_fetch._entries({**self.tree, **change})
        self.assertFalse((self.root / "sandbox").exists())
        self.assertEqual(len(trusted_fetch._entries({**self.tree, "tree": [
            {"path": "dir", "mode": "040000", "type": "tree", "sha": self.tree_sha},
            {**valid, "path": "dir/file"}]})), 1)

    def test_principal_mismatch_including_seat_substitution(self):
        def mismatch(loop, path, login, limit, accept):
            if path == "/user" and login == "reader":
                return b'{"login":"reviewer"}'
            return self.response(loop, path, login, limit, accept)
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "principal mismatch"):
            self.stage(callback=mismatch)
        self.assertFalse((self.root / "sandbox").exists())

    def test_case_variant_logins_are_not_distinct_principals(self):
        self.loop["read_token"] = "Reviewer"
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "distinct read and seat"):
            self.stage()
        self.assertFalse((self.root / "sandbox").exists())

    def test_malformed_nested_api_objects_fail_closed(self):
        for path_fragment, replacement in (
            ("/pulls/7", {"head": "bad"}),
            ("/pulls/7", {"base": {"repo": []}}),
            ("/pulls/7", {"head": {"repo": "bad"}}),
            ("/git/commits/", {"tree": "bad"}),
        ):
            with self.subTest(path_fragment=path_fragment, replacement=replacement):
                def malformed(loop, path, login, limit, accept):
                    original = self.response(loop, path, login, limit, accept)
                    if path_fragment in path:
                        return json.dumps({**json.loads(original), **replacement}).encode()
                    return original
                with self.assertRaises(trusted_fetch.FetchDenied):
                    self.stage(callback=malformed)
                self.assertFalse((self.root / "sandbox").exists())

    def test_exclusive_publish_never_replaces_empty_sibling(self):
        source = self.root / "unpublished"
        source.mkdir()
        (source / "sentinel").write_text("new")
        destination = self.root / "sandbox"
        destination.mkdir()
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "publish failed"):
            trusted_fetch._publish_exclusive(source, destination)
        self.assertTrue((source / "sentinel").exists())
        self.assertTrue(destination.is_dir())

    def test_partial_export_invisible_and_stale_head(self):
        def inspect(loop, path, login, limit, accept):
            if "/git/blobs/" in path:
                self.assertFalse((self.root / "sandbox").exists())
                self.assertEqual(len(list(self.root.glob(".review-trusted-*"))), 1)
            data = self.response(loop, path, login, limit, accept)
            if path.endswith("/pulls/7") and self.pr_count == 3:
                self.pr_head = "c" * 40
                return self.response(loop, path, login, limit, accept)
            return data
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "stale"):
            self.stage(callback=inspect)
        self.assertFalse((self.root / "sandbox").exists())
        self.assertFalse(any(self.root.glob(".review-trusted-*")))

    def test_corrupt_blob_and_wrong_commit_rejected(self):
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "hash mismatch"):
            self.stage(callback=lambda loop, path, login, limit, accept:
                b"wrong" if "/git/blobs/" in path else self.response(loop, path, login, limit, accept))
        self.assertFalse((self.root / "sandbox").exists())
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "commit SHA mismatch"):
            self.stage(callback=lambda loop, path, login, limit, accept:
                b'{"sha":"bad"}' if "/git/commits/" in path else self.response(loop, path, login, limit, accept))


if __name__ == "__main__":
    unittest.main()
