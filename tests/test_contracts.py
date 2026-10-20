import json
from dataclasses import replace
from pathlib import Path
import unittest

from agent_workbench.contracts import (
    AuthorityConfig, DecisionAction, Evidence,
    DecisionArtifact, Proposal, ProposalEventLog, ProposalState, ScoutActor, ValidationError,
    VerifiedDecision, VerifiedProposalCreation,
    apply_decision, create_builder_handoff, create_proposal_event,
)


FIXTURES = Path(__file__).parent / "fixtures"
APPROVER = "fixture-user-operator-configured"


class FakeTrustedDecisionVerifier:
    """Deterministic stand-in for the authenticated integration adapter; not production auth code."""

    def __init__(self):
        self._trusted = set()
        self._creations = {}

    def creation_key(self, proposal, metadata):
        return json.dumps([proposal.to_dict(), metadata], sort_keys=True)

    def trust_creation(self, proposal, author="fixture-scout-quality", actor_class="scout"):
        metadata = dict(source_event_id="fixture-comment-create-1",
                        occurred_at="2026-09-17T00:00:00Z", event_id="event-create-1")
        self._creations[self.creation_key(proposal, metadata)] = VerifiedProposalCreation(
            author, actor_class, "verified:creation-1", "test.trusted-fixture-adapter")

    def verify_creation(self, proposal, **metadata):
        return self._creations.get(self.creation_key(proposal, metadata))


    def trust(self, artifact):
        self._trusted.add(artifact)

    def verify(self, artifact):
        if artifact not in self._trusted:
            return None
        return VerifiedDecision(
            artifact=artifact,
            verification_id=f"verified:{artifact.source_event_id}",
            verifier="test.trusted-fixture-adapter",
        )


def proposal_from_fixture(name="valid_proposal.json"):
    value = json.loads((FIXTURES / name).read_text())
    value["scout"] = ScoutActor(**value["scout"])
    value["evidence"] = tuple(
        Evidence(item["source"], item["observation"], tuple(item["reproductions"]))
        for item in value["evidence"]
    )
    for field in ("alternatives", "acceptance_criteria", "required_tests", "risks_non_goals", "dependencies"):
        value[field] = tuple(value[field])
    return Proposal(**value)


def unsigned_artifact(proposal, created, action=DecisionAction.APPROVE, actor=APPROVER, **changes):
    values = dict(
        source_event_id="fixture-webhook-decision-1", proposal_id=proposal.proposal_id,
        creation_event_id=created.event_id, actor_id=actor, action=action,
        occurred_at="2026-09-17T00:10:00Z", rationale="Reviewed against acceptance criteria",
        duplicate_of=None,
    )
    values.update(changes)
    return DecisionArtifact(**values)


def trusted_artifact(verifier, proposal, created, **changes):
    artifact = unsigned_artifact(proposal, created, **changes)
    verifier.trust(artifact)
    return artifact


class ScoutProposalContractTests(unittest.TestCase):
    def setUp(self):
        self.proposal = proposal_from_fixture()
        self.verifier = FakeTrustedDecisionVerifier()
        self.verifier.trust_creation(self.proposal)
        self.created = create_proposal_event(
            self.proposal, source_event_id="fixture-comment-create-1",
            occurred_at="2026-09-17T00:00:00Z", event_id="event-create-1",
            verifier=self.verifier,
        )
        self.authority = AuthorityConfig(APPROVER)

    def decide(self, artifact, event_id="event-decision-1"):
        return apply_decision(
            self.proposal, self.created, artifact, authority=self.authority,
            verifier=self.verifier, event_id=event_id,
        )

    def test_valid_fixture_and_creation_event_are_machine_readable(self):
        self.proposal.validate()
        encoded = json.dumps(self.created.to_dict())
        self.assertIn('"event_type": "proposal.created"', encoded)
        self.assertEqual(self.proposal.to_dict(), self.created.record["comment"]["contract"])

    def test_invalid_fixture_missing_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "evidence"):
            proposal_from_fixture("invalid_missing_evidence.json").validate()

    def test_incomplete_required_text_criteria_and_tests_are_rejected(self):
        for field, value in (("problem", " "), ("acceptance_criteria", ()), ("required_tests", ("",))):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                replace(self.proposal, **{field: value}).validate()

    def test_forged_operator_identity_and_tampered_artifact_are_rejected(self):
        forged = trusted_artifact(self.verifier, self.proposal, self.created, actor="attacker")
        with self.assertRaisesRegex(ValidationError, "configured approver"):
            self.decide(forged)
        authentic = trusted_artifact(self.verifier, self.proposal, self.created)
        tampered = replace(authentic, actor_id=APPROVER + "-forged")
        with self.assertRaisesRegex(ValidationError, "not authentic"):
            self.decide(tampered)

    def test_authority_is_configurable_and_fails_closed(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        for authority in (AuthorityConfig(""), AuthorityConfig("somebody-else")):
            with self.subTest(authority=authority), self.assertRaises(ValidationError):
                apply_decision(
                    self.proposal, self.created, artifact, authority=authority,
                    verifier=self.verifier, event_id="decision",
                )

    def test_decision_must_bind_proposal_creation_and_distinct_source(self):
        for changes in (
            {"proposal_id": "another"}, {"creation_event_id": "another"},
            {"source_event_id": self.created.source_event_id},
        ):
            artifact = trusted_artifact(self.verifier, self.proposal, self.created, **changes)
            with self.assertRaises(ValidationError):
                self.decide(artifact)

    def test_discuss_reject_and_duplicate_preserve_rationale(self):
        cases = (
            (DecisionAction.DISCUSS, ProposalState.DISCUSS, None),
            (DecisionAction.REJECT, ProposalState.REJECTED, None),
            (DecisionAction.DUPLICATE, ProposalState.DUPLICATE, "proposal-existing"),
        )
        for index, (action, state, duplicate_of) in enumerate(cases):
            artifact = trusted_artifact(
                self.verifier, self.proposal, self.created, action=action,
                source_event_id=f"decision-{index}", rationale=f"reason-{index}",
                duplicate_of=duplicate_of,
            )
            event = self.decide(artifact, f"event-{index}")
            self.assertEqual(state, event.state)
            self.assertEqual(f"reason-{index}", event.rationale)
            self.assertEqual(state.value, event.record["comment"]["state"])

    def test_discussion_rejection_and_duplicate_require_rationale(self):
        for action in (DecisionAction.DISCUSS, DecisionAction.REJECT, DecisionAction.DUPLICATE):
            artifact = trusted_artifact(
                self.verifier, self.proposal, self.created, action=action,
                rationale="", duplicate_of="other" if action is DecisionAction.DUPLICATE else None,
            )
            with self.subTest(action=action), self.assertRaisesRegex(ValidationError, "rationale"):
                self.decide(artifact)

    def test_missing_or_fabricated_creation_event_is_rejected(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        fabricated = replace(self.created, actor={"scout_id": "attacker"})
        with self.assertRaisesRegex(ValidationError, "proposal.created"):
            apply_decision(
                self.proposal, fabricated, artifact, authority=self.authority,
                verifier=self.verifier, event_id="decision",
            )
        with self.assertRaises((AttributeError, ValidationError)):
            apply_decision(
                self.proposal, None, artifact, authority=self.authority,  # type: ignore[arg-type]
                verifier=self.verifier, event_id="decision",
            )

    def test_valid_approval_and_builder_handoff(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        approval = self.decide(artifact)
        handoff = create_builder_handoff(
            self.proposal, self.created, approval, artifact,
            authority=self.authority, verifier=self.verifier,
        )
        self.assertEqual(APPROVER, handoff.authorized_by_actor_id)
        self.assertEqual("verified:fixture-webhook-decision-1", handoff.decision_verification_id)
        self.assertEqual(self.proposal.to_dict(), handoff.proposal)

    def test_events_remain_inspectable_in_append_only_log(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        approval = self.decide(artifact)
        empty = ProposalEventLog()
        created_log = empty.append(self.created)
        final_log = created_log.append(approval)
        self.assertEqual((), empty.events)
        self.assertEqual(1, len(created_log.events))
        self.assertEqual(["proposal.created", "proposal.approved"],
                         [item["event_type"] for item in final_log.to_list()])
        with self.assertRaisesRegex(ValidationError, "unique"):
            final_log.append(approval)
        with self.assertRaisesRegex(ValidationError, "terminal"):
            final_log.append(replace(approval, event_id="later-decision"))

    def test_nested_event_payloads_cannot_change_history(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        approval = self.decide(artifact)
        actor = dict(approval.actor)
        authentication = dict(approval.authentication)
        projection = approval.to_dict()["record"]
        detached = replace(approval, actor=actor, authentication=authentication,
                           record=projection)
        log = ProposalEventLog().append(self.created).append(detached)
        before = json.dumps(log.to_list(), sort_keys=True)
        actor["actor_id"] = "attacker"
        authentication["verification_id"] = "forged"
        projection["comment"]["rationale"] = "rewritten"
        for mapping, key in (
            (self.created.actor, "actor_id"),
            (self.created.authentication, "verification_id"),
            (self.created.record["comment"]["contract"], "problem"),
            (self.created.record["comment"]["contract"]["evidence"][0], "source"),
            (detached.actor, "actor_id"),
            (detached.authentication, "verification_id"),
            (detached.record["comment"], "rationale"),
            (log.events[1].record["comment"], "rationale"),
        ):
            with self.subTest(key=key), self.assertRaises(TypeError):
                mapping[key] = "rewritten"
        with self.assertRaises(AttributeError):
            self.created.record["comment"]["contract"]["required_tests"].append("fake")
        inspected = log.to_list()
        inspected[0]["record"]["comment"]["contract"]["evidence"][0]["source"] = "fake"
        inspected[1]["authentication"]["verification_id"] = "fake"
        self.assertEqual(before, json.dumps(log.to_list(), sort_keys=True))

    def test_creation_requires_authenticated_content_metadata_and_actor_class(self):
        for changes in (
            {"proposal": replace(self.proposal, problem="tampered")},
            {"source_event_id": "forged"},
            {"event_id": "forged"},
            {"occurred_at": "2026-09-17T01:00:00Z"},
        ):
            kwargs = dict(proposal=self.proposal, source_event_id=self.created.source_event_id,
                          event_id=self.created.event_id, occurred_at=self.created.occurred_at,
                          verifier=self.verifier)
            kwargs.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValidationError, "authentic"):
                create_proposal_event(**kwargs)
        self.verifier.trust_creation(self.proposal, actor_class="human")
        with self.assertRaisesRegex(ValidationError, "Scout or Orchestrator"):
            create_proposal_event(
                self.proposal, source_event_id=self.created.source_event_id,
                event_id=self.created.event_id, occurred_at=self.created.occurred_at,
                verifier=self.verifier)

    def test_authentic_operator_cannot_approve_as_scout_or_orchestrator_creator(self):
        for actor_class in ("scout", "orchestrator"):
            # The submitted Scout ID can conceal the real creator identity.
            self.verifier.trust_creation(self.proposal, author=APPROVER, actor_class=actor_class)
            created = create_proposal_event(
                self.proposal, source_event_id=self.created.source_event_id,
                event_id=self.created.event_id, occurred_at=self.created.occurred_at,
                verifier=self.verifier)
            artifact = trusted_artifact(self.verifier, self.proposal, created)
            with self.subTest(actor_class=actor_class):
                with self.assertRaisesRegex(ValidationError, "self-approve"):
                    apply_decision(
                        self.proposal, created, artifact, authority=self.authority,
                        verifier=self.verifier, event_id="approval")
                fabricated = replace(created, event_type="proposal.approved",
                                     event_id="approval", state=ProposalState.APPROVED)
                with self.assertRaisesRegex(ValidationError, "self-approve"):
                    create_builder_handoff(
                        self.proposal, created, fabricated, artifact,
                        authority=self.authority, verifier=self.verifier)

    def test_creator_identity_is_reverified_at_decision_and_builder_gates(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        approval = self.decide(artifact)
        fabricated = replace(self.created, actor={
            "actor_id": "different-creator", "actor_class": "scout"})
        with self.assertRaisesRegex(ValidationError, "proposal.created"):
            apply_decision(
                self.proposal, fabricated, artifact, authority=self.authority,
                verifier=self.verifier, event_id="decision")
        self.verifier._creations.clear()
        with self.assertRaisesRegex(ValidationError, "not authentic"):
            self.decide(artifact)
        with self.assertRaisesRegex(ValidationError, "not authentic"):
            create_builder_handoff(
                self.proposal, self.created, approval, artifact,
                authority=self.authority, verifier=self.verifier)

    def test_untrusted_caller_artifact_is_not_an_authenticated_decision(self):
        artifact = unsigned_artifact(self.proposal, self.created)
        with self.assertRaisesRegex(ValidationError, "not authentic"):
            self.decide(artifact)

    def test_builder_rejects_incomplete_forged_and_unapproved_work(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        approval = self.decide(artifact)
        incomplete = replace(self.proposal, required_tests=())
        with self.assertRaises(ValidationError):
            create_builder_handoff(incomplete, self.created, approval, artifact,
                                   authority=self.authority, verifier=self.verifier)
        forged = replace(artifact, rationale="caller-tampered-rationale")
        with self.assertRaisesRegex(ValidationError, "authentic"):
            create_builder_handoff(self.proposal, self.created, approval, forged,
                                   authority=self.authority, verifier=self.verifier)
        discussion_artifact = trusted_artifact(
            self.verifier, self.proposal, self.created, action=DecisionAction.DISCUSS)
        discussion = self.decide(discussion_artifact)
        with self.assertRaisesRegex(ValidationError, "approval"):
            create_builder_handoff(self.proposal, self.created, discussion, discussion_artifact,
                                   authority=self.authority, verifier=self.verifier)

    def test_builder_rejects_fabricated_approval_event(self):
        artifact = trusted_artifact(self.verifier, self.proposal, self.created)
        approval = self.decide(artifact)
        for fabricated in (
            replace(approval, source_event_id="fabricated"),
            replace(approval, authentication=None),
            replace(approval, actor={"actor_id": "attacker"}),
            replace(approval, record={"status": "Todo", "comment": {}}),
        ):
            with self.subTest(fabricated=fabricated), self.assertRaises(ValidationError):
                create_builder_handoff(
                    self.proposal, self.created, fabricated, artifact,
                    authority=self.authority, verifier=self.verifier,
                )


if __name__ == "__main__":
    unittest.main()
