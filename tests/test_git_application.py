"""Exercise the patch boundary with real Git; fault hooks never fabricate Git output."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch as mock_patch

from agent_workbench import policy
from agent_workbench.demo import MemoryLedger
from agent_workbench.dispatch import authorize_build
from test_policy import NOW, fixtures


class GitApplicationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory(prefix="workbench-git-test-"))
        self.root = Path(directory) / "repo"
        self.root.mkdir()
        self.patch_path = Path(directory) / "builder.patch"
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "TMPDIR", "TEMP") if key in os.environ}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
        self.enterContext(mock_patch.dict(os.environ, env, clear=True))
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Test Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", os.devnull)
        for name in ("src/app.py", "tests/outside.py"):
            target = self.root / name
            target.parent.mkdir()
            target.write_bytes(b"value = 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic baseline")
        snapshot, handoff = fixtures()
        snapshot = replace(snapshot, base_sha=self.git("rev-parse", "HEAD").decode().strip(),
                           approved_paths=("src/app.py",))
        self.authorization = authorize_build(snapshot, handoff, approver_actor_id="operator", ledger=MemoryLedger(),
                                              expected_repository="example/demo", now=NOW).to_dict()
        self.index = self.root / ".git/index"
        self.lock = self.root / ".git/index.lock"
        self.allowed = self.diff("src/app.py", b"value = 2\n")

    def git(self, *args, **kwargs):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, timeout=15, **kwargs).stdout

    def diff(self, name, content):
        target = self.root / name
        original = target.read_bytes()
        target.write_bytes(content)
        result = self.git("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv")
        target.write_bytes(original)
        return result

    def apply(self):
        return policy.apply_patch(self.authorization, "example/demo", self.root, self.patch_path)

    def assert_original_index(self, original, tree):
        self.assertEqual(self.git("write-tree"), tree)
        self.assertEqual(self.index.read_bytes(), original)
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.root / ".git/index.lock.lock").exists())

    def test_valid_patch_stages_without_changing_worktree(self):
        self.patch_path.write_bytes(self.allowed)
        self.assertEqual(self.apply(), ("src/app.py",))
        self.assertEqual(self.git("show", ":src/app.py"), b"value = 2\n")
        self.assertEqual((self.root / "src/app.py").read_bytes(), b"value = 1\n")
        self.assertFalse(self.lock.exists())

    def test_replacing_patch_after_validation_cannot_change_applied_bytes(self):
        binary = self.diff("src/app.py", b"\x00BINARY-FIXTURE\x00")
        outside = self.diff("tests/outside.py", b"value = 3\n")
        with self.assertRaisesRegex(ValueError, "binary"):
            policy.validate_patch(binary, self.authorization)
        for replacement in (binary, outside):
            with self.subTest(replacement=replacement[:80]):
                self.git("reset", "--hard", "HEAD")
                self.patch_path.write_bytes(self.allowed)
                real_run = subprocess.run
                swapped = []

                def run(args, *pos, **kwargs):
                    if "apply" in args and "--check" in args and not swapped:
                        self.patch_path.write_bytes(replacement)
                        swapped.append(True)
                    return real_run(args, *pos, **kwargs)

                with mock_patch.object(policy.subprocess, "run", side_effect=run):
                    self.assertEqual(self.apply(), ("src/app.py",))
                self.assertTrue(swapped)
                self.assertEqual(self.git("show", ":src/app.py"), b"value = 2\n")
                self.assertEqual(self.git("diff", "--cached", "--name-only"), b"src/app.py\n")

    def test_interruption_after_application_preserves_original_index(self):
        self.patch_path.write_bytes(self.allowed)
        tree, original = self.git("write-tree"), self.index.read_bytes()
        real_run = subprocess.run
        interrupted = []

        def run(args, *pos, **kwargs):
            result = real_run(args, *pos, **kwargs)
            if args[1] == "diff" and "--cached" in args:
                interrupted.append(True)
                raise subprocess.TimeoutExpired(args, 1)
            return result

        with mock_patch.object(policy.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.apply()
        self.assertTrue(interrupted)
        self.assert_original_index(original, tree)

    def test_final_scope_or_mode_rejection_preserves_original_index(self):
        for mode, name, error in (("100644", "tests/outside.py", "scope"),
                                   ("120000", "src/app.py", "symlink")):
            with self.subTest(mode=mode):
                self.git("reset", "--hard", "HEAD")
                self.patch_path.write_bytes(self.allowed)
                tree, original = self.git("write-tree"), self.index.read_bytes()
                blob = self.git("hash-object", "-w", "--stdin", input=b"fixture target\n").decode().strip()
                real_run = subprocess.run
                injected = []

                def run(args, *pos, **kwargs):
                    boundary = "diff" if mode == "100644" else "ls-files"
                    if args[1] == boundary and not injected:
                        real_run(["git", "update-index", "--add", "--cacheinfo", mode, blob, name],
                                 cwd=self.root, env=kwargs.get("env"), check=True, capture_output=True)
                        injected.append(True)
                    return real_run(args, *pos, **kwargs)

                with mock_patch.object(policy.subprocess, "run", side_effect=run):
                    with self.assertRaisesRegex(ValueError, error):
                        self.apply()
                self.assertTrue(injected)
                self.assert_original_index(original, tree)

    def test_existing_index_lock_is_preserved(self):
        self.patch_path.write_bytes(self.allowed)
        original = self.index.read_bytes()
        self.lock.write_bytes(b"another writer owns this lock")
        with self.assertRaises((ValueError, FileExistsError, subprocess.CalledProcessError)):
            self.apply()
        self.assertEqual(self.index.read_bytes(), original)
        self.assertEqual(self.lock.read_bytes(), b"another writer owns this lock")

    def test_failed_promotion_preserves_original_index(self):
        self.patch_path.write_bytes(self.allowed)
        tree, original = self.git("write-tree"), self.index.read_bytes()
        with mock_patch.object(policy.os, "replace", side_effect=OSError("injected rename failure")):
            with self.assertRaisesRegex(OSError, "rename failure"):
                self.apply()
        self.assert_original_index(original, tree)

    def test_successful_promotion_preserves_next_writers_lock(self):
        self.patch_path.write_bytes(self.allowed)
        real_replace = os.replace

        def next_writer(source, target):
            real_replace(source, target)
            Path(source).write_bytes(b"next writer owns this lock")

        with mock_patch.object(policy.os, "replace", side_effect=next_writer):
            self.apply()
        self.assertEqual(self.git("show", ":src/app.py"), b"value = 2\n")
        self.assertEqual(self.lock.read_bytes(), b"next writer owns this lock")

    def test_valid_patch_in_linked_worktree(self):
        linked = self.root.parent / "linked"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        self.root = linked
        self.patch_path.write_bytes(self.allowed)
        self.assertEqual(self.apply(), ("src/app.py",))
        self.assertEqual(self.git("show", ":src/app.py"), b"value = 2\n")
