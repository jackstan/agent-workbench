from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agent_workbench.contracts import BuilderHandoff
from agent_workbench.demo import MemoryLedger
from agent_workbench.dispatch import ApprovalSnapshot, DispatchError, authorize_build
from agent_workbench.policy import validate_patch, validate_path_rule

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def fixtures():
    proposal = {"proposal_id": "demo", "proposed_change": "Fix the example",
                "acceptance_criteria": ["Expected output"], "required_tests": ["Run tests"]}
    snapshot = ApprovalSnapshot("task", "DEMO-1", "https://example.invalid/task", "example/demo", "main",
                                "a" * 40, "demo", "created", "approved", "source", "operator", "verified",
                                NOW.isoformat(), ("Expected output",), ("Run tests",), "Fix the example",
                                ("src/*.py",), True, False, False)
    return snapshot, BuilderHandoff(proposal, "created", "approved", "operator", "verified")


def patch(path="src/app.py"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n".encode()


class SQLiteLedger:
    """Test-only durable store; each call opens a new connection."""

    def __init__(self, path):
        self.path = path

    def claim(self, key, event):
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY)")
                return connection.execute("INSERT OR IGNORE INTO claims VALUES (?)", (key,)).rowcount == 1
        finally:
            connection.close()


class AuthorizationTests(unittest.TestCase):
    def test_reprojected_source_approval_cannot_be_consumed_again(self):
        snapshot, handoff = fixtures()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "claims.sqlite"
            args = dict(approver_actor_id="operator", expected_repository="example/demo", now=NOW)
            authorize_build(snapshot, handoff, ledger=SQLiteLedger(path), **args)
            for snapshot_fields, handoff_fields in (
                ({"approval_event_id": "new-projection"}, {"approval_event_id": "new-projection"}),
                ({"decision_verification_id": "new-observation"}, {"decision_verification_id": "new-observation"}),
                ({"creation_event_id": "new-creation-projection"}, {"creation_event_id": "new-creation-projection"}),
            ):
                with self.subTest(fields=snapshot_fields), self.assertRaisesRegex(DispatchError, "consumed"):
                    authorize_build(replace(snapshot, **snapshot_fields), replace(handoff, **handoff_fields),
                                    ledger=SQLiteLedger(path), **args)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
            finally:
                connection.close()

    def test_new_source_approval_can_authorize_new_work(self):
        snapshot, handoff = fixtures()
        ledger = MemoryLedger()
        args = dict(approver_actor_id="operator", expected_repository="example/demo", ledger=ledger, now=NOW)
        first = authorize_build(snapshot, handoff, **args)
        second = authorize_build(replace(snapshot, approval_source_event_id="source-2", approval_event_id="approved-2"),
                                 replace(handoff, approval_event_id="approved-2"), **args)
        self.assertNotEqual(first.approval_key, second.approval_key)

    def test_source_identity_is_scoped_to_repository(self):
        snapshot, handoff = fixtures()
        ledger = MemoryLedger()
        first = authorize_build(snapshot, handoff, approver_actor_id="operator", ledger=ledger,
                                expected_repository=snapshot.repository, now=NOW)
        second = authorize_build(replace(snapshot, repository="example/another"), handoff,
                                 approver_actor_id="operator", ledger=ledger,
                                 expected_repository="example/another", now=NOW)
        self.assertNotEqual(first.approval_key, second.approval_key)

    def test_authorization_binds_scope_and_rejects_consumed_approval(self):
        snapshot, handoff = fixtures()
        ledger = MemoryLedger()
        args = dict(approver_actor_id="operator", expected_repository="example/demo", ledger=ledger, now=NOW)
        authorization = authorize_build(snapshot, handoff, **args)
        self.assertEqual(authorization.approved_paths, ("src/*.py",))
        self.assertIn("human review", authorization.pr_body)
        with self.assertRaisesRegex(DispatchError, "consumed"):
            authorize_build(snapshot, handoff, **args)

    def test_wrong_repository_cannot_authorize_itself(self):
        snapshot, handoff = fixtures()
        with self.assertRaisesRegex(DispatchError, "repository"):
            authorize_build(snapshot, handoff, approver_actor_id="operator", ledger=MemoryLedger(),
                            expected_repository="another/project", now=NOW)

    def test_expired_revoked_changed_and_forged_approvals_rejected(self):
        snapshot, handoff = fixtures()
        for changes in ({"revoked": True}, {"complete": False}, {"approver_actor_id": "attacker"},
                        {"approved_at": (NOW - timedelta(days=8)).isoformat()},
                        {"approved_at": (NOW + timedelta(hours=1)).isoformat()},
                        {"acceptance_criteria": ("Altered after approval",)}, {"approved_paths": ("../outside",)}):
            with self.subTest(changes=changes), self.assertRaises(DispatchError):
                authorize_build(replace(snapshot, **changes), handoff, approver_actor_id="operator",
                                expected_repository="example/demo", ledger=MemoryLedger(), now=NOW)


class PatchPolicyTests(unittest.TestCase):
    def test_reserved_basenames_are_protected_at_every_depth_and_case(self):
        for directory in ("", "src/", "packages/tool/src/"):
            for name in ("AGENTS.md", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock"):
                for spelling in (name, name.upper(), name.lower()):
                    path = directory + spelling
                    with self.subTest(path=path):
                        with self.assertRaisesRegex(ValueError, "sensitive"):
                            validate_patch(patch(path), {"approved_paths": ["*"]})
                        with self.assertRaisesRegex(ValueError, "protected"):
                            validate_path_rule(path)

    def test_ordinary_nested_files_remain_eligible(self):
        for path in ("src/feature.py", "packages/tool/README.md"):
            with self.subTest(path=path):
                validate_path_rule(path)
                self.assertEqual(validate_patch(patch(path), {"approved_paths": ["*"]}), (path,))

    def test_in_scope_patch_is_allowed(self):
        self.assertEqual(validate_patch(patch(), {"approved_paths": ["src/*.py"]}), ("src/app.py",))

    def test_out_of_scope_and_protected_paths_rejected(self):
        for path in ("../escape.py", ".github/workflows/build.yml", "src/agent_workbench/policy.py",
                     "src/secret.py", "package.json", "uv.lock", "app.toml"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_patch(patch(path), {"approved_paths": ["*"]})
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_patch(patch("tests/app.py"), {"approved_paths": ["src/*.py"]})

    def test_binary_symlink_rename_and_empty_patch_rejected(self):
        for data in (b"", b"GIT binary patch", b"\x00", b"new file mode 120000\n" + patch(),
                     patch() + b"rename from src/a.py\nrename to src/b.py\n"):
            with self.assertRaises(ValueError):
                validate_patch(data, {"approved_paths": ["src/*.py"]})

    def test_oversize_patch_rejected(self):
        with self.assertRaisesRegex(ValueError, "size"):
            validate_patch(patch() + b"x" * 2_000_000, {"approved_paths": ["src/*.py"]})
