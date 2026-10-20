"""Offline fixture replay with real temporary Git operations and application tests.

Approval and builder/reviewer outputs are scripted fixtures. Nothing here proves
remote authentication, autonomous coding, hosted publication, or release readiness.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib

from .config import parse_config
from .contracts import (
    AuthorityConfig, DecisionAction, DecisionArtifact, Evidence, Proposal,
    ProposalEventLog, ScoutActor, VerifiedDecision, VerifiedProposalCreation,
    apply_decision, create_builder_handoff, create_proposal_event,
)
from .dispatch import ApprovalSnapshot, DispatchError, authorize_build
from .policy import apply_patch
from .repair import decide
from .review import CheckResult, ReviewEvidence, review

BEFORE = 'def slugify(text):\n    return text.lower().replace(" ", "-")\n'
AFTER = 'import re\n\ndef slugify(text):\n    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n'
APP_TEST = '''import unittest
from src.slugify import slugify

class SlugTests(unittest.TestCase):
    def test_punctuation_and_repeated_spaces(self):
        self.assertEqual(slugify("  Hello,   World! "), "hello-world")

    def test_empty_input(self):
        self.assertEqual(slugify(""), "")

    def test_existing_slug(self):
        self.assertEqual(slugify("already-a-slug"), "already-a-slug")
'''


class FixtureVerifier:
    """Demo-only exact-record lookup, deliberately not a live identity adapter."""

    def __init__(self, proposal, creation, decision):
        self.proposal_json = json.dumps(proposal.to_dict(), sort_keys=True)
        self.creation = dict(creation)
        self.decision = decision

    def verify_creation(self, proposal, **metadata):
        if (json.dumps(proposal.to_dict(), sort_keys=True) == self.proposal_json
                and metadata == self.creation):
            return VerifiedProposalCreation("demo-scout", "scout", "demo-creation", "fixture-only")
        return None

    def verify(self, artifact):
        if artifact == self.decision:
            return VerifiedDecision(artifact, "demo-decision", "fixture-only")
        return None


class MemoryLedger:
    """Demo-only replay detection. Production needs durable atomic claims."""

    def __init__(self):
        self.claims = set()

    def claim(self, key, event):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


def load_demo_proposal() -> Proposal:
    value = json.loads(files("agent_workbench").joinpath("data/proposal.json").read_text())
    value["scout"] = ScoutActor(**value["scout"])
    value["evidence"] = tuple(Evidence(item["source"], item["observation"], tuple(item["reproductions"]))
                              for item in value["evidence"])
    for key in ("alternatives", "acceptance_criteria", "required_tests", "risks_non_goals", "dependencies"):
        value[key] = tuple(value[key])
    return Proposal(**value)


def run_demo() -> dict:
    config = parse_config(tomllib.loads(files("agent_workbench").joinpath("data/project.toml").read_text()))
    proposal = load_demo_proposal()
    # The fixed demo executes only packaged fixture code. It is not a sandbox
    # for a user-supplied project, shell command, patch, or model response.
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "TMPDIR", "TEMP") if key in os.environ}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    with TemporaryDirectory(prefix="agent-workbench-demo-") as directory:
        root = Path(directory)

        def git(*args):
            return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                                   "-c", "user.name=Workbench Demo", "-c", "user.email=demo@example.invalid",
                                   "-c", "commit.gpgsign=false", *args],
                                  cwd=root, env=env, check=True, capture_output=True, text=True, timeout=15).stdout.strip()

        def check():
            return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=30).returncode

        git("init", "--initial-branch=main")
        (root / "src").mkdir()
        (root / "tests").mkdir()
        target = root / "src/slugify.py"
        target.write_text(BEFORE)
        (root / "src/__init__.py").write_text("")
        (root / "tests/test_slugify.py").write_text(APP_TEST)
        (root / ".gitignore").write_text("__pycache__/\n")
        git("add", ".")
        git("commit", "-m", "Initialize demonstration fixture")
        base_sha = git("rev-parse", "HEAD")
        before_exit = check()
        if before_exit == 0:
            raise RuntimeError("The demonstration did not reproduce its expected failing check")
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        creation = {"source_event_id": "demo-source", "occurred_at": stamp, "event_id": "demo-created"}
        artifact = DecisionArtifact("demo-approval", proposal.proposal_id, "demo-created",
                                    config.approver_actor_id, DecisionAction.APPROVE, stamp,
                                    "Scripted approval for this offline demonstration", None)
        verifier = FixtureVerifier(proposal, creation, artifact)
        authority = AuthorityConfig(config.approver_actor_id)
        created = create_proposal_event(proposal, verifier=verifier, **creation)
        approved = apply_decision(proposal, created, artifact, authority=authority,
                                  verifier=verifier, event_id="demo-approved")
        handoff = create_builder_handoff(proposal, created, approved, artifact,
                                         authority=authority, verifier=verifier)
        snapshot = ApprovalSnapshot(
            task_id="demo-task", task_key="DEMO-1", task_url="https://example.invalid/tasks/1",
            repository=config.repository, base_branch=config.base_branch, base_sha=base_sha,
            proposal_id=proposal.proposal_id, creation_event_id=created.event_id,
            approval_event_id=approved.event_id, approval_source_event_id=artifact.source_event_id,
            approver_actor_id=config.approver_actor_id, decision_verification_id=handoff.decision_verification_id,
            approved_at=stamp, acceptance_criteria=proposal.acceptance_criteria,
            required_tests=proposal.required_tests, proposed_change=proposal.proposed_change,
            approved_paths=config.approved_paths, complete=True, revoked=False, ambiguous=False)
        ledger = MemoryLedger()
        authorization = authorize_build(snapshot, handoff, approver_actor_id=config.approver_actor_id,
                                         expected_repository=config.repository, ledger=ledger, now=now)
        replay_rejected = False
        try:
            authorize_build(snapshot, handoff, approver_actor_id=config.approver_actor_id,
                            expected_repository=config.repository, ledger=ledger, now=now)
        except DispatchError:
            replay_rejected = True
        # Scripted builder fixture: capture a real diff, then restore the base.
        target.write_text(AFTER)
        patch_text = git("diff", "--no-ext-diff", "--no-textconv", "--full-index") + "\n"
        git("checkout", "--", "src/slugify.py")
        # Keep untrusted patch data outside the target checkout.
        with TemporaryDirectory(prefix="workbench-patch-") as patch_dir:
            patch = Path(patch_dir) / "change.patch"
            patch.write_text(patch_text)
            paths = apply_patch(authorization.to_dict(), config.repository, root, patch)
        git("checkout-index", "-f", "--all")
        after_exit = check()
        git("commit", "-m", "Apply scripted slug normalization improvement")
        head_sha = git("rev-parse", "HEAD")
        evidence = ReviewEvidence(head_sha, base_sha, ("unit",),
                                  (CheckResult("unit", head_sha, base_sha, after_exit),), True)
        result = review(evidence, lambda e: {"verdict": "pass", "head_sha": e.head_sha,
                                            "summary": "Scripted reviewer fixture; no model call", "findings": []})
        decision = decide(result, head_sha, set(), config.max_repair_attempts)
        if after_exit != 0 or decision.state != "ready_for_human" or not replay_rejected:
            raise RuntimeError("Demonstration did not satisfy its expected policy outcomes")
        return {
            "schema_version": 1, "mode": "offline_fixture_replay", "project": config.project_id,
            "before_test_exit_code": before_exit, "after_test_exit_code": after_exit,
            "changed_paths": list(paths), "duplicate_approval_rejected": replay_rejected,
            "events": ProposalEventLog().append(created).append(approved).to_list(),
            "review": result, "decision": asdict(decision),
            "external_actions": [], "human_merge_required": True,
            "limitations": ["Approval, builder, and reviewer are scripted fixtures",
                            "No model calls, GitHub PR, Linear writes, deployment, or merge",
                            "Replay ledger is in memory and is not durable across processes"],
        }
