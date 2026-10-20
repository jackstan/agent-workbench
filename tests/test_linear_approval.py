from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench.config import load_config
from agent_workbench.dispatch import DispatchError
from agent_workbench.ledger import LedgerError, SQLiteLedger
from agent_workbench.linear import (
    LinearApprovalAdapter, LinearApprovalConfig, LinearError, prepare_proposal,
)


ROOT = Path(__file__).parents[1]
ORG = "00000000-0000-4000-8000-000000000001"
TEAM = "00000000-0000-4000-8000-000000000002"
PROJECT = "00000000-0000-4000-8000-000000000003"
CREATOR = "00000000-0000-4000-8000-000000000004"
HUMAN = "00000000-0000-4000-8000-000000000005"
ISSUE = "00000000-0000-4000-8000-000000000006"
PROPOSAL = "00000000-0000-4000-8000-000000000007"
APPROVAL = "00000000-0000-4000-8000-000000000008"
REVOCATION = "00000000-0000-4000-8000-000000000009"
BASE = "a" * 40
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
STAMP = (NOW - timedelta(minutes=5)).isoformat()
DECISION_STAMP = (NOW - timedelta(minutes=4)).isoformat()


def comment(identifier, author, body, *, app=False, stamp=DECISION_STAMP):
    return {"id": identifier, "body": body, "createdAt": stamp, "updatedAt": stamp,
            "editedAt": None, "archivedAt": None, "parent": None, "issue": {"id": ISSUE},
            "user": {"id": author, "active": True, "app": app},
            "botActor": None, "onBehalfOf": None, "externalUser": None}


class Source:
    """Synthetic authenticated-API boundary, never a live authentication claim."""

    def __init__(self):
        self.project = replace(load_config(ROOT / "examples/demo/project.toml"), approver_actor_id=HUMAN)
        self.settings = LinearApprovalConfig(ORG, TEAM, PROJECT, CREATOR)
        raw = json.loads((ROOT / "tests/fixtures/valid_proposal.json").read_text())
        self.prepared = prepare_proposal(self.project, self.settings, raw, BASE)
        self.issue = {"id": ISSUE, "identifier": "LAB-1", "url": "https://linear.app/lab/issue/LAB-1",
                      "team": {"id": TEAM}, "project": {"id": PROJECT},
                      "archivedAt": None, "trashed": False}
        self.rows = [comment(PROPOSAL, CREATOR, self.prepared["body"], app=True, stamp=STAMP),
                     comment(APPROVAL, HUMAN, self.command("approve"))]
        self.calls = []
        self.page_size = 1
        self.on_call = None

    def command(self, action):
        return f"agent-workbench {action} {PROPOSAL} {self.prepared['proposal_sha256']}"

    def query(self, query, variables, *, timeout):
        self.calls.append(deepcopy(variables))
        if self.on_call:
            self.on_call(self)
        self.assert_query(query)
        start = int(variables.get("after") or 0)
        end = min(start + self.page_size, len(self.rows))
        return {"organization": {"id": ORG},
                "issue": {**deepcopy(self.issue), "comments": {
                    "nodes": deepcopy(self.rows[start:end]),
                    "pageInfo": {"hasNextPage": end < len(self.rows), "endCursor": str(end)}}}}

    @staticmethod
    def assert_query(query):
        assert "includeArchived: true" in query
        assert "onBehalfOf" in query and "editedAt" in query
        assert "mutation" not in query


class LinearApprovalTests(unittest.TestCase):
    def setUp(self):
        self.source = Source()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "claims.sqlite"
        SQLiteLedger.initialize(self.path)

    def authorize(self, **kwargs):
        adapter = LinearApprovalAdapter(self.source, self.source.project, self.source.settings)
        return adapter.authorize(ISSUE, PROPOSAL, expected_base_sha=BASE,
                                 ledger=SQLiteLedger(self.path), now=NOW, **kwargs)

    def fresh_source(self):
        self.source = Source()
        self.path = self.path.with_name(self.path.stem + "-next.sqlite")
        SQLiteLedger.initialize(self.path)

    def assert_no_claim(self):
        import sqlite3
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM claims").fetchone()[0], 0)

    def test_paginated_approval_uses_canonical_identity_and_persists_replay_protection(self):
        result = self.authorize()
        self.assertEqual(result.base_sha, BASE)
        self.assertEqual(result.repository, self.source.project.repository)
        self.assertEqual(result.audit_event.details["approval_source_event_id"],
                         f"linear:{ORG}:comment:{APPROVAL}")
        self.assertEqual(result.proposal_binding["proposal_sha256"], self.source.prepared["proposal_sha256"])
        self.assertGreaterEqual(len(self.source.calls), 4)
        self.assertEqual(SQLiteLedger(self.path).receipt(result.approval_key),
                         result.audit_event.to_dict())
        with self.assertRaisesRegex(DispatchError, "consumed"):
            self.authorize()

    def test_saved_authorization_revalidated_without_second_claim(self):
        saved = json.loads(json.dumps(self.authorize().to_dict()))
        ledger = SQLiteLedger(self.path)
        original = ledger.receipt(saved["approval_key"])
        adapter = LinearApprovalAdapter(self.source, self.source.project, self.source.settings)
        result = adapter.revalidate(saved, issue_id=ISSUE, proposal_comment_id=PROPOSAL,
                                    ledger=ledger, now=NOW)
        self.assertEqual(result["approval_key"], saved["approval_key"])
        self.assertEqual(ledger.receipt(saved["approval_key"]), original)
        with self.assertRaisesRegex(DispatchError, "consumed"):
            self.authorize()

    def test_saved_authorization_tampering_and_revocation_block_continuation(self):
        saved = json.loads(json.dumps(self.authorize().to_dict()))
        ledger = SQLiteLedger(self.path)
        adapter = LinearApprovalAdapter(self.source, self.source.project, self.source.settings)
        for field, value in (("repository", "other/repo"), ("approved_paths", ["tests/*"]),
                             ("branch_name", "other"), ("scope_digest", "0" * 64)):
            with self.subTest(field=field), self.assertRaises(DispatchError):
                adapter.revalidate({**saved, field: value}, issue_id=ISSUE,
                                   proposal_comment_id=PROPOSAL, ledger=ledger, now=NOW)
        self.source.rows.append(comment(REVOCATION, HUMAN, self.source.command("revoke")))
        with self.assertRaisesRegex(LinearError, "revoked"):
            adapter.revalidate(saved, issue_id=ISSUE, proposal_comment_id=PROPOSAL,
                               ledger=ledger, now=NOW)

    def test_saved_authorization_needs_receipt_and_fresh_decision(self):
        saved = json.loads(json.dumps(self.authorize().to_dict()))
        adapter = LinearApprovalAdapter(self.source, self.source.project, self.source.settings)
        empty = SQLiteLedger.initialize(Path(self.directory.name) / "empty.sqlite")
        with self.assertRaises(DispatchError):
            adapter.revalidate(saved, issue_id=ISSUE, proposal_comment_id=PROPOSAL, ledger=empty, now=NOW)
        with self.assertRaisesRegex(DispatchError, "stale"):
            adapter.revalidate(saved, issue_id=ISSUE, proposal_comment_id=PROPOSAL,
                               ledger=SQLiteLedger(self.path), now=NOW + timedelta(days=8))

    def test_revocation_on_later_page_blocks_then_deletion_cannot_revive(self):
        self.source.rows.append(comment(REVOCATION, HUMAN, self.source.command("revoke")))
        with self.assertRaisesRegex(LinearError, "revoked"):
            self.authorize()
        self.assert_no_claim()
        self.source.rows.pop()
        with self.assertRaisesRegex(LedgerError, "removed or changed"):
            self.authorize()
        self.assert_no_claim()

    def test_forged_delegated_bot_inactive_and_edited_approvals_fail_closed(self):
        for field, value in (
            ("user", {"id": CREATOR, "active": True, "app": True}),
            ("user", {"id": HUMAN, "active": True, "app": True}),
            ("user", {"id": HUMAN, "active": False, "app": False}),
            ("user", None), ("onBehalfOf", {"id": HUMAN}), ("botActor", {"id": "bot"}),
            ("externalUser", {"id": "external"}), ("editedAt", DECISION_STAMP),
            ("archivedAt", DECISION_STAMP), ("parent", {"id": PROPOSAL}),
        ):
            with self.subTest(field=field, value=value):
                self.fresh_source()
                self.source.rows[1][field] = value
                with self.assertRaises((LinearError, LedgerError)):
                    self.authorize()
                self.assert_no_claim()

    def test_creator_requires_distinct_configured_app_identity(self):
        for creator in ({"id": HUMAN, "app": False, "active": True},
                        {"id": CREATOR, "app": False, "active": True},
                        {"id": CREATOR, "app": True, "active": False}):
            with self.subTest(creator=creator):
                self.fresh_source()
                self.source.rows[0]["user"] = creator
                with self.assertRaises((LinearError, LedgerError)):
                    self.authorize()
                self.assert_no_claim()

    def test_complete_proposal_and_configuration_are_bound(self):
        original = deepcopy(self.source)
        for field, value in (
            ("problem", "Different problem"), ("risks_non_goals", ["Changed constraints"]),
            ("dependencies", ["New dependency"]), ("required_tests", ["Skip the tests"]),
        ):
            with self.subTest(field=field):
                self.fresh_source()
                self.source = deepcopy(original)
                envelope = json.loads(self.source.rows[0]["body"].split("\n", 1)[1].rsplit("\n", 1)[0])
                envelope["proposal"][field] = value
                self.source.rows[0]["body"] = "```agent-workbench-proposal\n" + json.dumps(envelope) + "\n```"
                with self.assertRaises((LinearError, LedgerError)):
                    self.authorize()
                self.assert_no_claim()
        self.fresh_source()
        self.source = deepcopy(original)
        self.source.project = replace(self.source.project, max_repair_attempts=0)
        with self.assertRaises((LinearError, LedgerError)):
            self.authorize()

    def test_stale_future_and_pre_creation_decisions_fail_without_claim(self):
        for stamp in ((NOW - timedelta(days=8)).isoformat(),
                      (NOW + timedelta(hours=1)).isoformat(), STAMP):
            with self.subTest(stamp=stamp):
                self.fresh_source()
                self.source.rows[1]["createdAt"] = stamp
                with self.assertRaises((LinearError, DispatchError, LedgerError)):
                    self.authorize()
                self.assert_no_claim()

    def test_wrong_issue_team_project_archived_or_base_are_rejected(self):
        for field, value in (("id", PROPOSAL), ("team", {"id": PROJECT}),
                             ("project", {"id": TEAM}), ("project", None),
                             ("archivedAt", STAMP), ("trashed", True)):
            with self.subTest(field=field):
                self.fresh_source()
                self.source.issue[field] = value
                with self.assertRaises((LinearError, LedgerError)):
                    self.authorize()
                self.assert_no_claim()
        self.fresh_source()
        adapter = LinearApprovalAdapter(self.source, self.source.project, self.source.settings)
        with self.assertRaises(LinearError):
            adapter.authorize(ISSUE, PROPOSAL, expected_base_sha="b" * 40,
                              ledger=SQLiteLedger(self.path), now=NOW)
        self.assert_no_claim()

    def test_multiple_approvals_are_ambiguous_not_new_authority(self):
        self.source.rows.append(comment(REVOCATION, HUMAN, self.source.command("approve")))
        with self.assertRaisesRegex(LinearError, "[Ee]xactly one"):
            self.authorize()
        self.assert_no_claim()

    def test_mid_read_changes_and_missing_later_page_fail_closed(self):
        def mutate(source):
            if len(source.calls) == 3:
                source.rows.append(comment(REVOCATION, HUMAN, source.command("revoke")))

        self.source.on_call = mutate
        with self.assertRaisesRegex(LinearError, "changed|revoked"):
            self.authorize()
        self.assert_no_claim()
        self.fresh_source()

        def fail(source):
            if len(source.calls) == 2:
                raise LinearError("partial API failure")

        self.source.on_call = fail
        with self.assertRaisesRegex(LinearError, "partial"):
            self.authorize()
        self.assert_no_claim()

    def test_repeated_cursor_and_duplicate_rows_are_rejected(self):
        original = self.source.query

        class Repeating:
            def query(self, query, variables, **kwargs):
                return original(query, {"after": None}, **kwargs)

        adapter = LinearApprovalAdapter(Repeating(), self.source.project, self.source.settings)
        with self.assertRaises(LinearError):
            adapter.authorize(ISSUE, PROPOSAL, expected_base_sha=BASE,
                              ledger=SQLiteLedger(self.path), now=NOW)
        self.assert_no_claim()

    def test_page_budget_exhaustion_does_not_authorize_from_a_partial_history(self):
        with patch("agent_workbench.linear.MAX_PAGES", 1):
            with self.assertRaisesRegex(LinearError, "page budget"):
                self.authorize()
        self.assert_no_claim()

    def test_read_deadline_expiry_does_not_claim(self):
        with patch("agent_workbench.linear.time.monotonic", side_effect=[0, 1, 31]):
            with self.assertRaisesRegex(LinearError, "deadline"):
                self.authorize()
        self.assert_no_claim()

    def test_ordinary_discussion_and_other_peoples_commands_do_not_grant_or_revoke(self):
        self.source.rows.append(comment(REVOCATION, HUMAN, "Looks interesting."))
        stranger = "00000000-0000-4000-8000-000000000099"
        self.source.rows.append(comment(stranger, stranger, self.source.command("revoke")))
        self.assertEqual(self.authorize().base_sha, BASE)

    def test_wrong_organization_and_missing_identity_fields_fail_closed(self):
        original = self.source.query

        class WrongOrganization:
            def query(self, query, variables, **kwargs):
                data = original(query, variables, **kwargs)
                data["organization"]["id"] = TEAM
                return data

        self.source.query = WrongOrganization().query
        with self.assertRaisesRegex(LinearError, "organization"):
            self.authorize()
        self.assert_no_claim()
        self.fresh_source()
        del self.source.rows[1]["onBehalfOf"]
        with self.assertRaisesRegex(LinearError, "Incomplete"):
            self.authorize()
        self.assert_no_claim()

    def test_editing_or_deleting_a_seen_approval_preserves_consumed_receipt(self):
        authorized = self.authorize()
        self.source.rows[1]["body"] = "ordinary discussion now"
        with self.assertRaises(LedgerError):
            self.authorize()
        self.assertIsNotNone(SQLiteLedger(self.path).receipt(authorized.approval_key))

    def test_malformed_or_wrong_digest_decisions_cannot_authorize(self):
        for body in ("agent-workbench approve",
                     f"agent-workbench approve {PROPOSAL} {'0' * 64}",
                     self.source.command("approve") + " extra",
                     self.source.command("approve") + "\nagent-workbench revoke"):
            with self.subTest(body=body):
                self.fresh_source()
                self.source.rows[1]["body"] = body
                with self.assertRaises(LinearError):
                    self.authorize()
                self.assert_no_claim()

    def test_changed_approved_scope_cannot_be_substituted(self):
        self.source.project = replace(self.source.project, approved_paths=("src/other.py",))
        with self.assertRaises(LinearError):
            self.authorize()
        self.assert_no_claim()

    def test_concurrent_revocation_observed_before_claim_blocks_stale_reader(self):
        class RevokedBeforeClaim(SQLiteLedger):
            def claim_observed(self, key, event, source, controls):
                self.observe(source, {**controls, REVOCATION: {"body": "new revocation observed by another runner"}})
                return super().claim_observed(key, event, source, controls)

        adapter = LinearApprovalAdapter(self.source, self.source.project, self.source.settings)
        with self.assertRaisesRegex(LedgerError, "changed before approval claim"):
            adapter.authorize(ISSUE, PROPOSAL, expected_base_sha=BASE,
                              ledger=RevokedBeforeClaim(self.path), now=NOW)
        self.assert_no_claim()

    def test_new_proposal_on_same_issue_preserves_old_controls_and_can_be_approved(self):
        first = self.authorize()
        new_proposal = "00000000-0000-4000-8000-000000000010"
        new_approval = "00000000-0000-4000-8000-000000000011"
        self.source.rows.extend([
            comment(new_proposal, CREATOR, self.source.prepared["body"], app=True, stamp=STAMP),
            comment(new_approval, HUMAN,
                    f"agent-workbench approve {new_proposal} {self.source.prepared['proposal_sha256']}"),
        ])
        adapter = LinearApprovalAdapter(self.source, self.source.project, self.source.settings)
        second = adapter.authorize(ISSUE, new_proposal, expected_base_sha=BASE,
                                   ledger=SQLiteLedger(self.path), now=NOW)
        self.assertNotEqual(first.approval_key, second.approval_key)
        self.assertIsNotNone(SQLiteLedger(self.path).receipt(first.approval_key))
