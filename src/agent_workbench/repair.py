"""Bounded repair decisions adapted from Sendable's Builder loop.

This module computes decisions only. It cannot launch a worker or publish a PR.
"""
from dataclasses import dataclass

from .review import SHA, valid_result


@dataclass(frozen=True)
class RepairDecision:
    state: str
    dispatch: bool = False
    reason: str = ""


def decide(result: dict, current_sha: str, prior_dispatches: set[str], max_attempts: int = 2) -> RepairDecision:
    if type(max_attempts) is not int or not 0 <= max_attempts <= 2:
        raise ValueError("max_attempts must be between 0 and 2")
    if (not isinstance(current_sha, str) or not SHA.fullmatch(current_sha)
            or not isinstance(prior_dispatches, set)
            or any(not isinstance(sha, str) or not SHA.fullmatch(sha) for sha in prior_dispatches)):
        raise ValueError("Repair history and current head require exact commit identities")
    if not isinstance(result, dict) or result.get("head_sha") != current_sha:
        return RepairDecision("waiting_for_verifier", reason="Reviewed commit is stale or missing")
    if not valid_result(result, current_sha):
        return RepairDecision("waiting_for_verifier", reason="Review output is invalid")
    if result["verdict"] == "pass":
        return RepairDecision("ready_for_human", reason="Checks and review passed; human merge remains required")
    if result["verdict"] != "blockers":
        return RepairDecision("waiting_for_verifier", reason="Review is incomplete")
    if current_sha in prior_dispatches:
        return RepairDecision("repair_dispatched", reason="This commit already has a repair reservation")
    blockers = [item for item in result["findings"] if item["severity"] == "blocker"]
    if all(item["remediation_kind"] in {"deployment_verification", "human_action"} for item in blockers):
        deployment_only = all(item["remediation_kind"] == "deployment_verification" for item in blockers)
        return RepairDecision("awaiting_deployment_verification" if deployment_only else "human_intervention_required",
                              reason="External prerequisites require a human handoff")
    if len(prior_dispatches) >= max_attempts:
        return RepairDecision("human_intervention_required", reason="Automatic repair limit reached")
    return RepairDecision("repair_requested", dispatch=True, reason="Blockers are eligible for a bounded repair")
