"""Deterministic Scout proposal and human approval contracts.

Proposal authors are untrusted. Decisions enter through an injected trusted
adapter boundary and are reverified when a Builder handoff is constructed.

Authenticated integrations must implement ``TrustedDecisionVerifier``.
Constructing these Python objects alone does not authenticate a human.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence


class ValidationError(ValueError):
    """Raised when a proposal or control-plane event fails closed."""


class ProposalState(str, Enum):
    PROPOSED = "Proposed"
    DISCUSS = "Discuss"
    APPROVED = "Approved for Build"
    REJECTED = "Rejected"
    DUPLICATE = "Duplicate"


class DecisionAction(str, Enum):
    APPROVE = "approve"
    DISCUSS = "discuss"
    REJECT = "reject"
    DUPLICATE = "duplicate"



def _required(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")


def _required_strings(values: Sequence[str], field: str) -> None:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValidationError(f"{field} must contain at least one item")
    for value in values:
        _required(value, field)


def _iso_timestamp(value: str, field: str) -> None:
    _required(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")


@dataclass(frozen=True)
class ScoutActor:
    scout_id: str
    scout_type: str
    display_name: str

    def validate(self) -> None:
        _required(self.scout_id, "actor.scout_id")
        _required(self.scout_type, "actor.scout_type")
        _required(self.display_name, "actor.display_name")


@dataclass(frozen=True)
class Evidence:
    source: str
    observation: str
    reproductions: tuple[str, ...]

    def validate(self) -> None:
        _required(self.source, "evidence.source")
        _required(self.observation, "evidence.observation")
        _required_strings(self.reproductions, "evidence.reproductions")


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    scout: ScoutActor
    role: str
    trigger: str
    problem: str
    evidence: tuple[Evidence, ...]
    user_impact: str
    confidence: str
    proposed_change: str
    alternatives: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_tests: tuple[str, ...]
    risks_non_goals: tuple[str, ...]
    dependencies: tuple[str, ...]

    def validate(self) -> None:
        _required(self.proposal_id, "proposal_id")
        self.scout.validate()
        for field in ("role", "trigger", "problem", "user_impact", "confidence", "proposed_change"):
            _required(getattr(self, field), field)
        if not self.evidence:
            raise ValidationError("evidence must contain at least one item")
        for item in self.evidence:
            if not isinstance(item, Evidence):
                raise ValidationError("evidence items must use the Evidence contract")
            item.validate()
        for field in (
            "alternatives", "acceptance_criteria", "required_tests",
            "risks_non_goals", "dependencies",
        ):
            _required_strings(getattr(self, field), field)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _freeze(value: Any) -> Any:
    """Detach JSON payloads from callers and recursively make them read-only."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValidationError("event payloads must contain only JSON values")


def _inspection_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _inspection_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_inspection_copy(item) for item in value]
    return value


@dataclass(frozen=True)
class ProposalEvent:
    event_type: str
    event_id: str
    proposal_id: str
    occurred_at: str
    state: ProposalState
    actor: Mapping[str, str]
    source_event_id: str
    record: Mapping[str, Any]
    rationale: str | None = None
    duplicate_of: str | None = None
    authentication: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        for name in ("actor", "record", "authentication"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        value = {field.name: _inspection_copy(getattr(self, field.name))
                 for field in fields(self)}
        value["state"] = self.state.value
        return value


@dataclass(frozen=True)
class ProposalEventLog:
    """Persistent-style event sequence: appending returns a new log."""

    events: tuple[ProposalEvent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(replace(event) for event in self.events))

    def append(self, event: ProposalEvent) -> "ProposalEventLog":
        _required(event.event_id, "event.event_id")
        if any(existing.event_id == event.event_id for existing in self.events):
            raise ValidationError("event_id must be unique in the append-only log")
        if self.events and event.proposal_id != self.events[0].proposal_id:
            raise ValidationError("an event log contains exactly one proposal")
        if not self.events and event.event_type != "proposal.created":
            raise ValidationError("the first event must be proposal.created")
        if self.events and event.event_type == "proposal.created":
            raise ValidationError("proposal.created may only be the first event")
        if self.events and self.events[-1].state in {
            ProposalState.APPROVED, ProposalState.REJECTED, ProposalState.DUPLICATE,
        }:
            raise ValidationError("terminal proposal state cannot transition")
        return ProposalEventLog(self.events + (event,))

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]


def create_proposal_event(
    proposal: Proposal, *, source_event_id: str, occurred_at: str, event_id: str,
    verifier: TrustedDecisionVerifier,
) -> ProposalEvent:
    """Validate a Scout proposal and return its immutable creation event."""
    proposal.validate()
    for value, field in ((source_event_id, "source_event_id"), (event_id, "event_id")):
        _required(value, field)
    _iso_timestamp(occurred_at, "occurred_at")
    author = verifier.verify_creation(
        proposal, source_event_id=source_event_id, occurred_at=occurred_at, event_id=event_id,
    )
    if author is None:
        raise ValidationError("proposal creation is not authentic")
    for name in ("actor_id", "verification_id", "verifier"):
        _required(getattr(author, name), f"creation.{name}")
    if author.actor_class not in {"scout", "orchestrator"}:
        raise ValidationError("proposal creator must be an authenticated Scout or Orchestrator")
    state = ProposalState.PROPOSED
    projection = {
                "comment": {
            "kind": "agent_workbench.proposal.created",
            "proposal_id": proposal.proposal_id,
            "state": state.value,
            "contract": proposal.to_dict(),
        },
    }
    return ProposalEvent(
        event_type="proposal.created", event_id=event_id,
        proposal_id=proposal.proposal_id, occurred_at=occurred_at, state=state,
        actor={"actor_id": author.actor_id, "actor_class": author.actor_class},
        source_event_id=source_event_id,
        authentication={"verification_id": author.verification_id, "verifier": author.verifier},
        record=projection,
    )


@dataclass(frozen=True)
class AuthorityConfig:
    approver_actor_id: str

    def validate(self) -> None:
        _required(self.approver_actor_id, "approver_actor_id")


@dataclass(frozen=True)
class DecisionArtifact:
    """Untrusted decision payload received by an authenticated integration."""

    source_event_id: str
    proposal_id: str
    creation_event_id: str
    actor_id: str
    action: DecisionAction
    occurred_at: str
    rationale: str
    duplicate_of: str | None


@dataclass(frozen=True)
class VerifiedDecision:
    """Attestation returned only by the trusted adapter boundary."""

    artifact: DecisionArtifact
    verification_id: str
    verifier: str


@dataclass(frozen=True)
class VerifiedProposalCreation:
    """Creator identity resolved by the trusted adapter, never from Scout fields."""

    actor_id: str
    actor_class: str
    verification_id: str
    verifier: str


class TrustedDecisionVerifier(Protocol):
    """Boundary integrations must implement using authenticated actor data."""

    def verify_creation(
        self, proposal: Proposal, *, source_event_id: str, occurred_at: str, event_id: str,
    ) -> VerifiedProposalCreation | None:
        """Authenticate the full proposal and creation metadata against trusted records.

        Resolve the creator's canonical actor identity and actor class from
        authenticated source data, independently of the Scout-supplied contract.
        Return None for any mismatch. Recheck on decisions and Builder handoffs.
        """

    def verify(self, artifact: DecisionArtifact) -> VerifiedDecision | None:
        """Return an attestation only for an authentic, trusted source event."""


def _verify_decision(
    artifact: DecisionArtifact, verifier: TrustedDecisionVerifier
) -> VerifiedDecision:
    attestation = verifier.verify(artifact)
    if attestation is None or attestation.artifact != artifact:
        raise ValidationError("Decision artifact is not authentic")
    _required(attestation.verification_id, "verification_id")
    _required(attestation.verifier, "verifier")
    return attestation


def _validate_created(
    proposal: Proposal, created: ProposalEvent, verifier: TrustedDecisionVerifier,
) -> None:
    proposal.validate()
    expected = create_proposal_event(
        proposal, source_event_id=created.source_event_id,
        occurred_at=created.occurred_at, event_id=created.event_id, verifier=verifier,
    )
    if created != expected:
        raise ValidationError("a valid proposal.created event bound to this proposal is required")


def apply_decision(
    proposal: Proposal,
    created: ProposalEvent,
    artifact: DecisionArtifact,
    *,
    authority: AuthorityConfig,
    verifier: TrustedDecisionVerifier,
    event_id: str,
) -> ProposalEvent:
    """Authenticate and project an authenticated decision into an inspectable event."""
    _validate_created(proposal, created, verifier)
    authority.validate()
    _required(event_id, "event_id")
    _iso_timestamp(artifact.occurred_at, "occurred_at")
    attestation = _verify_decision(artifact, verifier)
    if artifact.actor_id != authority.approver_actor_id:
        raise ValidationError("only configured approver may decide proposals")
    if (artifact.action is DecisionAction.APPROVE
            and artifact.actor_id == created.actor["actor_id"]):
        raise ValidationError("proposal creators cannot self-approve")
    if artifact.proposal_id != proposal.proposal_id or artifact.creation_event_id != created.event_id:
        raise ValidationError("decision is not bound to the proposal creation event")
    if artifact.source_event_id == created.source_event_id:
        raise ValidationError("decision must have a distinct source event")
    if artifact.action in {DecisionAction.DISCUSS, DecisionAction.REJECT, DecisionAction.DUPLICATE}:
        _required(artifact.rationale, "rationale")
    if artifact.action is DecisionAction.DUPLICATE:
        _required(artifact.duplicate_of or "", "duplicate_of")
        if artifact.duplicate_of == proposal.proposal_id:
            raise ValidationError("proposal cannot duplicate itself")
    state = {
        DecisionAction.APPROVE: ProposalState.APPROVED,
        DecisionAction.DISCUSS: ProposalState.DISCUSS,
        DecisionAction.REJECT: ProposalState.REJECTED,
        DecisionAction.DUPLICATE: ProposalState.DUPLICATE,
    }[artifact.action]
    event_type = {
        DecisionAction.APPROVE: "proposal.approved",
        DecisionAction.DISCUSS: "proposal.discussed",
        DecisionAction.REJECT: "proposal.rejected",
        DecisionAction.DUPLICATE: "proposal.duplicated",
    }[artifact.action]
    return ProposalEvent(
        event_type=event_type,
        event_id=event_id, proposal_id=proposal.proposal_id,
        occurred_at=artifact.occurred_at, state=state,
        actor={"actor_id": artifact.actor_id},
        source_event_id=artifact.source_event_id,
        rationale=artifact.rationale, duplicate_of=artifact.duplicate_of,
        authentication={
            "verification_id": attestation.verification_id,
            "verifier": attestation.verifier,
        },
        record={
            "comment": {"kind": "agent_workbench.proposal.decision", "proposal_id": proposal.proposal_id,
                        "state": state.value, "rationale": artifact.rationale,
                        "duplicate_of": artifact.duplicate_of},
        },
    )


@dataclass(frozen=True)
class BuilderHandoff:
    proposal: Mapping[str, Any]
    creation_event_id: str
    approval_event_id: str
    authorized_by_actor_id: str
    decision_verification_id: str


def create_builder_handoff(
    proposal: Proposal,
    created: ProposalEvent,
    approval: ProposalEvent,
    artifact: DecisionArtifact,
    *, authority: AuthorityConfig,
    verifier: TrustedDecisionVerifier,
) -> BuilderHandoff:
    """Fail-closed Builder gate that revalidates content and authentication."""
    _validate_created(proposal, created, verifier)
    authority.validate()
    attestation = _verify_decision(artifact, verifier)
    if artifact.action is not DecisionAction.APPROVE:
        raise ValidationError("Builder requires an approval decision")
    if artifact.actor_id != authority.approver_actor_id:
        raise ValidationError("Builder approval is not from configured approver")
    if artifact.proposal_id != proposal.proposal_id or artifact.creation_event_id != created.event_id:
        raise ValidationError("Builder approval is not bound to this proposal")
    expected_approval = apply_decision(
        proposal, created, artifact, authority=authority, verifier=verifier,
        event_id=approval.event_id,
    )
    if approval != expected_approval:
        raise ValidationError("Builder requires the validated approval event")
    return BuilderHandoff(
        proposal.to_dict(), created.event_id, approval.event_id,
        artifact.actor_id, attestation.verification_id,
    )
