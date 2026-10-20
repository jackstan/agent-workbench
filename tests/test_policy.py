from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from agent_workbench.contracts import BuilderHandoff
from agent_workbench.demo import MemoryLedger
from agent_workbench.dispatch import ApprovalSnapshot, DispatchError, authorize_build
from agent_workbench.policy import validate_patch

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


class AuthorizationTests(unittest.TestCase):
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
