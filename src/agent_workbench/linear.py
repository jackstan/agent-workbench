"""Read-only Linear approval adapter with explicit, content-bound decisions.

Only the HTTPS transport authenticates provider data. Injected transports are a
trusted boundary, useful for tests; JSON fixtures are not live authentication.
"""

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import tomllib
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from .config import ProjectConfig
from .contracts import (
    AuthorityConfig, DecisionAction, DecisionArtifact, Evidence, Proposal, ScoutActor,
    VerifiedDecision, VerifiedProposalCreation, apply_decision, create_builder_handoff,
    create_proposal_event,
)
from .dispatch import ApprovalSnapshot, BuildAuthorization, authorize_build
from .ledger import SQLiteLedger


ENDPOINT = "https://api.linear.app/graphql"
MAX_RESPONSE_BYTES = 1_000_000
MAX_PAGES = 20
PROPOSAL_FENCE = "```agent-workbench-proposal\n"
QUERY = """
query WorkbenchApproval($issue: String!, $after: String) {
  organization { id }
  issue(id: $issue) {
    id identifier url archivedAt trashed
    team { id }
    project { id }
    comments(first: 100, after: $after, includeArchived: true, orderBy: createdAt) {
      nodes {
        id body createdAt updatedAt editedAt archivedAt
        issue { id }
        parent { id }
        user { id active app }
        botActor { id }
        onBehalfOf { id }
        externalUser { id }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class LinearError(ValueError):
    """Incomplete, ambiguous or unauthenticated input cannot authorize work."""


def _canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise LinearError("Invalid JSON value") from error


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LinearError("Duplicate JSON field")
        result[key] = value
    return result


def _json(text):
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(LinearError("Non-finite JSON")))
        _canonical(value)
        return value
    except (ValueError, TypeError, RecursionError) as error:
        raise LinearError("Invalid or ambiguous JSON") from error


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except (ValueError, AttributeError) as error:
        raise LinearError("Linear identities must be canonical UUIDs, not names") from error
    return value


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
        return stamp
    except (ValueError, TypeError, AttributeError) as error:
        raise LinearError("Invalid provider timestamp") from error


def _base(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{40}", value):
        raise LinearError("A trusted expected base commit is required")


@dataclass(frozen=True)
class LinearApprovalConfig:
    organization_id: str
    team_id: str
    project_id: str
    creator_actor_id: str

    def __post_init__(self):
        for field in fields(self):
            _uuid(getattr(self, field.name))


def load_linear_config(path: Path) -> LinearApprovalConfig:
    with path.open("rb") as stream:
        raw = stream.read(16_001)
    if len(raw) > 16_000:
        raise LinearError("Linear configuration exceeds 16 KB")
    value = tomllib.loads(raw.decode("utf-8"))
    if set(value) != {field.name for field in fields(LinearApprovalConfig)}:
        raise LinearError("Linear configuration has missing or unknown fields")
    return LinearApprovalConfig(**value)


def _proposal(value) -> Proposal:
    try:
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(Proposal)}:
            raise ValueError("proposal fields")
        data = dict(value)
        data["scout"] = ScoutActor(**data["scout"])
        data["evidence"] = tuple(Evidence(**{**item, "reproductions": tuple(item["reproductions"])})
                                 for item in data["evidence"])
        for name in ("alternatives", "acceptance_criteria", "required_tests", "risks_non_goals", "dependencies"):
            if not isinstance(data[name], list):
                raise ValueError("expected a JSON list")
            data[name] = tuple(data[name])
        result = Proposal(**data)
        result.validate()
        # Reject type coercion such as a string used as a list of reproductions.
        if _canonical(result.to_dict()) != _canonical(value):
            raise ValueError("proposal was not represented exactly")
        return result
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise LinearError("Invalid complete proposal contract") from error


def prepare_proposal(project: ProjectConfig, settings: LinearApprovalConfig,
                     proposal: dict, base_sha: str) -> dict:
    _base(base_sha)
    _uuid(project.approver_actor_id)
    if settings.creator_actor_id == project.approver_actor_id:
        raise LinearError("Creator and approver must be distinct identities")
    parsed = _proposal(proposal)
    envelope = {
        "schema_version": 1, "repository": project.repository, "base_branch": project.base_branch,
        "base_sha": base_sha, "approved_paths": list(project.approved_paths),
        "project_config_sha256": _sha(asdict(project)),
        "linear_config_sha256": _sha(asdict(settings)), "proposal": parsed.to_dict(),
    }
    body = PROPOSAL_FENCE + json.dumps(envelope, indent=2) + "\n```"
    if len(body.encode("utf-8")) > 64_000:
        raise LinearError("Rendered proposal exceeds the 64 KB comment budget")
    return {"body": body, "proposal_sha256": _sha(envelope)}


class QueryClient(Protocol):
    def query(self, query: str, variables: dict, *, timeout: float) -> dict:
        """Return data from an authenticated, complete GraphQL response."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LinearError("Linear redirects are not accepted")


class LinearClient:
    """Fixed endpoint, bounded reads, no redirect or mutation capability.

    Personal API keys are sent without Bearer; OAuth access tokens use Bearer.
    Credentials are supplied by the trusted caller, never stored in receipts.
    """

    def __init__(self, token: str, *, oauth: bool = False):
        if (not isinstance(token, str) or not token.strip()
                or any(character.isspace() for character in token)):
            raise LinearError("A Linear credential is required")
        self._authorization = ("Bearer " if oauth else "") + token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def query(self, query: str, variables: dict, *, timeout: float) -> dict:
        if query != QUERY:
            raise LinearError("Only the reviewed approval read query is supported")
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise LinearError("Invalid API timeout")
        request = Request(ENDPOINT, data=_canonical({"query": query, "variables": variables}).encode(),
                          headers={"Authorization": self._authorization, "Content-Type": "application/json"},
                          method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise LinearError("Linear returned an unsuccessful HTTP status")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise LinearError("Linear response exceeds the size budget")
            result = _json(raw)
            if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
                raise LinearError("Linear returned errors or incomplete data")
            return result["data"]
        except (HTTPError, URLError, OSError):
            # Provider messages and request objects may contain sensitive data.
            raise LinearError("Linear read failed; no approval can be inferred") from None


class _RecordVerifier:
    """Private projection of the authenticated read, not an independently signed proof."""

    def __init__(self, proposal, creation, creator, artifact, digest):
        self.proposal = _canonical(proposal.to_dict())
        self.creation = creation
        self.creator = creator
        self.artifact = artifact
        self.digest = digest

    def verify_creation(self, proposal, **metadata):
        if _canonical(proposal.to_dict()) == self.proposal and metadata == self.creation:
            return VerifiedProposalCreation(self.creator, "orchestrator", self.digest, "linear-api-v1")
        return None

    def verify(self, artifact):
        if artifact == self.artifact:
            return VerifiedDecision(artifact, self.digest, "linear-api-v1")
        return None


@dataclass(frozen=True)
class _ObservedLedger:
    ledger: SQLiteLedger
    source: str
    controls: dict

    def claim(self, key, event):
        return self.ledger.claim_observed(key, event, self.source, self.controls)


class LinearApprovalAdapter:
    def __init__(self, client: QueryClient, project: ProjectConfig, settings: LinearApprovalConfig):
        _uuid(project.approver_actor_id)
        if settings.creator_actor_id == project.approver_actor_id:
            raise LinearError("Creator and approver must be distinct identities")
        self.client, self.project, self.settings = client, project, settings

    def _read(self, issue_id, deadline):
        after, seen_cursors, comments, issue = None, set(), {}, None
        for _ in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinearError("Linear read deadline exceeded")
            data = self.client.query(QUERY, {"issue": issue_id, "after": after}, timeout=min(remaining, 10))
            if time.monotonic() >= deadline:
                raise LinearError("Linear read deadline exceeded")
            try:
                if data["organization"]["id"] != self.settings.organization_id:
                    raise LinearError("Unexpected Linear organization")
                page_issue = dict(data["issue"])
                connection = page_issue.pop("comments")
                if (page_issue["id"] != issue_id or page_issue["team"]["id"] != self.settings.team_id
                        or page_issue["project"]["id"] != self.settings.project_id
                        or page_issue["archivedAt"] is not None
                        or (page_issue["trashed"] is not None and page_issue["trashed"] is not False)):
                    raise LinearError("Issue is outside the configured active project")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-[1-9][0-9]*", page_issue["identifier"]):
                    raise LinearError("Invalid issue identifier")
                if not isinstance(page_issue["url"], str) or not page_issue["url"].startswith("https://linear.app/"):
                    raise LinearError("Invalid issue URL")
                if issue is not None and issue != page_issue:
                    raise LinearError("Issue changed during pagination")
                issue = page_issue
                nodes, page_info = connection["nodes"], connection["pageInfo"]
                if not isinstance(nodes, list) or len(nodes) > 100:
                    raise LinearError("Invalid comments page")
                for row in nodes:
                    required = {"id", "body", "createdAt", "updatedAt", "editedAt", "archivedAt",
                                "issue", "parent", "user", "botActor", "onBehalfOf", "externalUser"}
                    if not isinstance(row, dict) or not required.issubset(row):
                        raise LinearError("Incomplete comment record")
                    identifier = _uuid(row["id"])
                    if identifier in comments or row["issue"]["id"] != issue_id:
                        raise LinearError("Duplicate or foreign comment record")
                    if not isinstance(row["body"], str) or len(row["body"].encode()) > 64_000:
                        raise LinearError("Invalid comment body")
                    user = row["user"]
                    if user is not None:
                        if (not isinstance(user, dict) or not {"id", "active", "app"}.issubset(user)
                                or type(user["active"]) is not bool or type(user["app"]) is not bool):
                            raise LinearError("Incomplete canonical author identity")
                        _uuid(user["id"])
                    for nullable in ("parent", "botActor", "onBehalfOf", "externalUser"):
                        if row[nullable] is not None and not isinstance(row[nullable], dict):
                            raise LinearError("Incomplete comment provenance")
                    comments[identifier] = row
                if type(page_info["hasNextPage"]) is not bool:
                    raise LinearError("Incomplete pagination metadata")
                if not page_info["hasNextPage"]:
                    return issue, comments
                after = page_info["endCursor"]
                if not isinstance(after, str) or not after or after in seen_cursors or not nodes:
                    raise LinearError("Pagination did not advance")
                seen_cursors.add(after)
            except (KeyError, TypeError, AttributeError, RecursionError) as error:
                raise LinearError("Incomplete Linear response") from error
        raise LinearError("Linear comments exceeded the page budget")

    def _controls(self, comments, proposal_id):
        controls = {}
        for identifier, row in comments.items():
            user = row["user"] or {}
            # Ignore ordinary discussion. Preserve configured actors' protocol
            # records even when malformed/edited, so later deletion is visible.
            if (identifier == proposal_id
                    or (user.get("id") in {self.settings.creator_actor_id, self.project.approver_actor_id}
                        and row["body"].strip().startswith("agent-workbench"))
                    or (user.get("id") == self.settings.creator_actor_id
                        and row["body"].startswith(PROPOSAL_FENCE))):
                controls[identifier] = {key: value for key, value in row.items() if key != "updatedAt"}
        return controls

    @staticmethod
    def _actor(row, expected, *, app):
        user = row["user"]
        if (not isinstance(user, dict) or user.get("id") != expected
                or user.get("active") is not True or user.get("app") is not app
                or any(row[field] is not None for field in
                       ("botActor", "onBehalfOf", "externalUser", "parent", "editedAt", "archivedAt"))):
            raise LinearError("Control record requires its direct, active, unedited author")

    def authorize(self, issue_id: str, proposal_comment_id: str, *, expected_base_sha: str,
                  ledger: SQLiteLedger, now: datetime | None = None) -> BuildAuthorization:
        _uuid(issue_id)
        _uuid(proposal_comment_id)
        _base(expected_base_sha)
        deadline = time.monotonic() + 30
        source = f"linear:{self.settings.organization_id}:issue:{issue_id}"
        reads = []
        for _ in range(2):
            issue, comments = self._read(issue_id, deadline)
            controls = self._controls(comments, proposal_comment_id)
            # Observe denied decisions too. A later deletion cannot revive them.
            ledger.observe(source, controls)
            reads.append((issue, controls))
        if reads[0] != reads[1]:
            raise LinearError("Control records changed between complete reads")
        issue, controls = reads[-1]
        try:
            creation_row = controls[proposal_comment_id]
        except KeyError as error:
            raise LinearError("Proposal comment is missing") from error
        self._actor(creation_row, self.settings.creator_actor_id, app=True)
        body = creation_row["body"]
        if not body.startswith(PROPOSAL_FENCE) or not body.endswith("\n```"):
            raise LinearError("Proposal must be a complete workbench proposal block")
        envelope = _json(body[len(PROPOSAL_FENCE):-4])
        if not isinstance(envelope, dict) or "proposal" not in envelope:
            raise LinearError("Missing proposal envelope")
        expected = prepare_proposal(self.project, self.settings, envelope["proposal"], expected_base_sha)
        digest = _sha(envelope)
        if digest != expected["proposal_sha256"]:
            raise LinearError("Proposal, base, scope or reviewed configuration does not match")
        proposal = _proposal(envelope["proposal"])
        approvals, revoked = [], False
        for identifier, row in controls.items():
            if identifier == proposal_comment_id:
                continue
            if row["body"].startswith(PROPOSAL_FENCE):
                continue
            parts = row["body"].strip().split()
            # Comments about other proposals are observed, but do not decide this one.
            if len(parts) >= 3 and parts[2] != proposal_comment_id:
                continue
            self._actor(row, self.project.approver_actor_id, app=False)
            if (len(parts) != 4 or parts[0] != "agent-workbench"
                    or parts[1] not in {"approve", "revoke"} or parts[3] != digest):
                raise LinearError("Malformed or mismatched approval decision")
            if _timestamp(row["createdAt"]) <= _timestamp(creation_row["createdAt"]):
                raise LinearError("Decision must follow proposal creation")
            if parts[1] == "revoke":
                revoked = True
            else:
                approvals.append(row)
        if revoked:
            raise LinearError("Proposal was revoked; create a new proposal for reconsideration")
        if len(approvals) != 1:
            raise LinearError("Exactly one direct human approval is required")
        approval = approvals[0]
        creation_id = f"linear:{self.settings.organization_id}:comment:{proposal_comment_id}"
        decision_id = f"linear:{self.settings.organization_id}:comment:{approval['id']}"
        creation = {"source_event_id": creation_id, "event_id": creation_id,
                    "occurred_at": creation_row["createdAt"]}
        artifact = DecisionArtifact(decision_id, proposal.proposal_id, creation_id,
                                    self.project.approver_actor_id, DecisionAction.APPROVE,
                                    approval["createdAt"], "Explicit content-bound Linear approval", None)
        read_digest = _sha(reads[-1])
        verifier = _RecordVerifier(proposal, creation, self.settings.creator_actor_id, artifact, read_digest)
        authority = AuthorityConfig(self.project.approver_actor_id)
        created = create_proposal_event(proposal, verifier=verifier, **creation)
        approved = apply_decision(proposal, created, artifact, authority=authority,
                                  verifier=verifier, event_id=decision_id)
        handoff = create_builder_handoff(proposal, created, approved, artifact,
                                         authority=authority, verifier=verifier)
        snapshot = ApprovalSnapshot(
            task_id=source, task_key=issue["identifier"], task_url=issue["url"],
            repository=self.project.repository, base_branch=self.project.base_branch,
            base_sha=expected_base_sha, proposal_id=proposal.proposal_id,
            creation_event_id=creation_id, approval_event_id=decision_id,
            approval_source_event_id=decision_id, approver_actor_id=self.project.approver_actor_id,
            decision_verification_id=read_digest, approved_at=approval["createdAt"],
            acceptance_criteria=proposal.acceptance_criteria, required_tests=proposal.required_tests,
            proposed_change=proposal.proposed_change, approved_paths=self.project.approved_paths,
            complete=True, revoked=False, ambiguous=False,
            proposal_binding={"proposal_sha256": digest, "read_sha256": read_digest,
                              "envelope": envelope, "source": source, "controls": controls},
        )
        return authorize_build(snapshot, handoff, approver_actor_id=self.project.approver_actor_id,
                               expected_repository=self.project.repository,
                               ledger=_ObservedLedger(ledger, source, controls),
                               now=now or datetime.now(timezone.utc))
