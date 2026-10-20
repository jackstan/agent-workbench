"""Fail-closed authorization for automatic Builder creation.

Network adapters must resolve an ``ApprovalSnapshot`` from authenticated tracker
API data.  No field in the webhook hint is an authority input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from .contracts import BuilderHandoff
from .policy import validate_path_rule


class DispatchError(ValueError):
    """The requested automatic build is not authorized."""


class BuildMode(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True)
class ApprovalSnapshot:
    """Authoritative state returned by the trusted approval adapter.

    Task and source-decision IDs must be stable and unambiguous within the
    configured repository's approval source; observation IDs may change on retry.
    """

    task_id: str
    task_key: str
    task_url: str
    repository: str
    base_branch: str
    base_sha: str
    proposal_id: str
    creation_event_id: str
    approval_event_id: str
    approval_source_event_id: str
    approver_actor_id: str
    decision_verification_id: str
    approved_at: str
    acceptance_criteria: tuple[str, ...]
    required_tests: tuple[str, ...]
    proposed_change: str
    approved_paths: tuple[str, ...]
    complete: bool
    revoked: bool
    ambiguous: bool
    proposal_binding: dict | None = None


class ConsumptionLedger(Protocol):
    """Durable, trusted claim store (for example, atomic GitHub tag refs)."""

    def claim(self, key: str, event: Mapping[str, Any]) -> bool:
        """Atomically return true only for the first claim of ``key``."""


@dataclass(frozen=True)
class AuditEvent:
    kind: str
    approval_key: str
    task_key: str
    occurred_at: str
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "approval_key": self.approval_key,
            "task_key": self.task_key,
            "occurred_at": self.occurred_at, "details": dict(self.details),
        }


@dataclass(frozen=True)
class BuildAuthorization:
    mode: BuildMode
    approval_key: str
    repository: str
    base_branch: str
    base_sha: str
    branch_name: str | None
    pr_title: str | None
    pr_body: str | None
    prompt: str | None
    approved_paths: tuple[str, ...]
    scope_digest: str
    audit_event: AuditEvent
    proposal_binding: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        value = dict(self.__dict__)
        value["mode"] = self.mode.value
        value["audit_event"] = self.audit_event.to_dict()
        if self.proposal_binding is None:
            value.pop("proposal_binding", None)
        return value


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise DispatchError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise DispatchError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _required(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} is required")


def _approval_key(snapshot: ApprovalSnapshot) -> str:
    # Consume the source decision, not a local projection or verification run.
    # Versioned JSON avoids ambiguous delimiter joins in canonical source IDs.
    material = json.dumps(("source-approval-v1", snapshot.repository,
                           snapshot.task_id, snapshot.approval_source_event_id),
                          separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def _scope_digest(proposal_id: str, approval_key: str,
                  approved_paths: tuple[str, ...]) -> str:
    material = "\0".join((proposal_id, approval_key, *approved_paths))
    return hashlib.sha256(material.encode()).hexdigest()


def _validate_handoff(snapshot: ApprovalSnapshot, handoff: BuilderHandoff,
                      approver_actor_id: str, expected_repository: str) -> None:
    for value, field in (
        (snapshot.task_id, "task_id"),
        (snapshot.task_key, "task_key"),
        (snapshot.task_url, "task_url"), (snapshot.repository, "repository"),
        (snapshot.base_branch, "base_branch"), (snapshot.proposal_id, "proposal_id"),
        (snapshot.base_sha, "base_sha"), (expected_repository, "expected_repository"),
        (snapshot.approval_source_event_id, "approval_source_event_id"),
        (approver_actor_id, "approver_actor_id"),
    ):
        _required(value, field)
    if snapshot.repository != expected_repository:
        raise DispatchError("approved repository does not match workflow repository")
    if len(snapshot.base_sha) != 40 or any(c not in "0123456789abcdef" for c in snapshot.base_sha):
        raise DispatchError("resolved base SHA is invalid")
    if not snapshot.approved_paths or any(not isinstance(path, str) or not path.strip()
                                          for path in snapshot.approved_paths):
        raise DispatchError("approved_paths must contain reviewed path rules")
    for path in snapshot.approved_paths:
        try:
            validate_path_rule(path)
        except ValueError as error:
            raise DispatchError(str(error)) from error
    if not snapshot.complete:
        raise DispatchError("proposal is incomplete")
    if snapshot.revoked:
        raise DispatchError("approval has been revoked")
    if snapshot.approver_actor_id != approver_actor_id:
        raise DispatchError("approval is not from configured approver")
    if snapshot.creation_event_id != handoff.creation_event_id:
        raise DispatchError("creation event does not match BuilderHandoff")
    if snapshot.approval_event_id != handoff.approval_event_id:
        raise DispatchError("approval event does not match BuilderHandoff")
    if snapshot.approver_actor_id != handoff.authorized_by_actor_id:
        raise DispatchError("approver does not match BuilderHandoff")
    if snapshot.decision_verification_id != handoff.decision_verification_id:
        raise DispatchError("decision attestation does not match BuilderHandoff")
    proposal = dict(handoff.proposal)
    if proposal.get("proposal_id") != snapshot.proposal_id:
        raise DispatchError("proposal does not match BuilderHandoff")
    if tuple(proposal.get("acceptance_criteria", ())) != snapshot.acceptance_criteria:
        raise DispatchError("acceptance criteria changed after approval")
    if tuple(proposal.get("required_tests", ())) != snapshot.required_tests:
        raise DispatchError("required tests changed after approval")
    if proposal.get("proposed_change") != snapshot.proposed_change:
        raise DispatchError("proposed change changed after approval")


def authorize_build(
    snapshot: ApprovalSnapshot,
    handoff: BuilderHandoff,
    *,
    approver_actor_id: str,
    ledger: ConsumptionLedger,
    expected_repository: str,
    now: datetime | None = None,
    max_age: timedelta = timedelta(days=7),
) -> BuildAuthorization:
    """Validate, consume, and bind one approval to one bounded build."""
    _validate_handoff(snapshot, handoff, approver_actor_id,
                      expected_repository)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    approved_at = _parse_time(snapshot.approved_at, "approved_at")
    if approved_at > current + timedelta(minutes=5):
        raise DispatchError("approval timestamp is in the future")
    if current - approved_at > max_age:
        raise DispatchError("approval is stale")

    key = _approval_key(snapshot)
    mode = BuildMode.MANUAL if snapshot.ambiguous else BuildMode.AUTOMATIC
    occurred_at = current.isoformat().replace("+00:00", "Z")
    event = AuditEvent(
        kind="builder.manual_required" if snapshot.ambiguous else "builder.authorized",
        approval_key=key, task_key=snapshot.task_key,
        occurred_at=occurred_at,
        details={"task_id": snapshot.task_id, "mode": mode.value,
                 "repository": snapshot.repository,
                 "approval_source_event_id": snapshot.approval_source_event_id,
                 "approval_key_version": "source-approval-v1",
                 "approval_event_id": snapshot.approval_event_id,
                 "proposal_id": snapshot.proposal_id},
    )
    if not ledger.claim(key, event.to_dict()):
        raise DispatchError("approval has already been consumed")

    suffix = key[:10]
    branch = f"agent/{snapshot.task_key.lower()}-{suffix}"
    criteria = "\n".join(f"- {item}" for item in snapshot.acceptance_criteria)
    tests = "\n".join(f"- {item}" for item in snapshot.required_tests)
    title = f"{snapshot.task_key}: {snapshot.proposed_change}"
    body = (
        f"Task: {snapshot.task_url}\n\n"
        "## Approved acceptance criteria\n" + criteria +
        "\n\n## Required tests\n" + tests +
        f"\n\nApproval key: `{key}`\n\n"
        "This PR requires human review and must never be auto-merged."
    )
    prompt = (
        f"Implement the workspace change for {snapshot.task_key} in repository {snapshot.repository}.\n"
        "You are the unprivileged workspace worker in a multi-stage Builder workflow. "
        "Your task is to edit approved files, run the required local tests, and leave "
        "the changes uncommitted for patch capture.\n"
        f"Approved change: {snapshot.proposed_change}\n"
        f"Full approved end-to-end requirements:\nAcceptance criteria:\n{criteria}\nRequired tests:\n{tests}\n"
        f"Approved file scope: {', '.join(snapshot.approved_paths)}\n"
        "Responsibility boundary: the trusted downstream workflow validates and applies "
        "your patch, commits and pushes it, creates the non-draft PR linked to the approved "
        "issue with these exact approved criteria, and dispatches the existing Verifier. "
        "Those lifecycle criteria remain required for the overall workflow; they are "
        "not actions for this worker. Do not treat their presence as a contradiction "
        "or as a reason to skip the workspace change. Never claim they have completed. "
        "Report your edits, observed test results, and that PR finalization is pending.\n"
        "Stay within the approved workspace scope. If the workspace requirements "
        "are ambiguous or cannot be met, "
        "make no changes and report that human intervention is required. "
        "Do not commit, push, create a PR, merge, or modify workflow credentials."
    )
    return BuildAuthorization(mode, key, snapshot.repository, snapshot.base_branch,
                              snapshot.base_sha,
                              branch, title, body, prompt, snapshot.approved_paths,
                              _scope_digest(snapshot.proposal_id, key,
                                            snapshot.approved_paths), event, snapshot.proposal_binding)


def dump_authorization(value: BuildAuthorization) -> str:
    return json.dumps(value.to_dict(), sort_keys=True, indent=2) + "\n"
