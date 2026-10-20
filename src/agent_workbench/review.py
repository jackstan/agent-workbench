"""Pure review gates. Integrations must authenticate all supplied evidence."""
from dataclasses import dataclass
import copy
import re
from typing import Callable

SHA = re.compile(r"[0-9a-f]{40}")
REMEDIATIONS = {"code_change", "deployment_verification", "human_action"}


@dataclass(frozen=True)
class CheckResult:
    id: str
    head_sha: str
    trusted_base_sha: str
    exit_code: int


@dataclass(frozen=True)
class ReviewEvidence:
    head_sha: str
    trusted_base_sha: str
    required_checks: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    context_complete: bool


def valid_result(result: object, expected_sha: str) -> bool:
    if not isinstance(result, dict) or set(result) != {"verdict", "head_sha", "summary", "findings"}:
        return False
    if (not isinstance(expected_sha, str) or result["head_sha"] != expected_sha or not SHA.fullmatch(expected_sha)
            or not isinstance(result["verdict"], str) or result["verdict"] not in {"pass", "blockers", "incomplete"}
            or not isinstance(result["summary"], str) or not result["summary"].strip()
            or not isinstance(result["findings"], list) or len(result["findings"]) > 50):
        return False
    blockers = []
    for finding in result["findings"]:
        if not isinstance(finding, dict) or set(finding) != {"severity", "evidence", "files", "remediation_kind"}:
            return False
        if (not isinstance(finding["severity"], str) or finding["severity"] not in {"blocker", "warning", "note"}
                or not isinstance(finding["remediation_kind"], str) or finding["remediation_kind"] not in REMEDIATIONS
                or not isinstance(finding["evidence"], str) or not finding["evidence"].strip()
                or not isinstance(finding["files"], list)
                or any(not isinstance(path, str) or not path for path in finding["files"])):
            return False
        if finding["severity"] == "blocker":
            blockers.append(finding)
    return not (result["verdict"] == "pass" and blockers) and not (result["verdict"] == "blockers" and not blockers)


def review(evidence: ReviewEvidence, reviewer: Callable[[ReviewEvidence], dict]) -> dict:
    """Never let reviewer output override failed, absent, or stale checks."""
    def outcome(verdict, summary, findings=None):
        return {"verdict": verdict, "head_sha": evidence.head_sha,
                "summary": summary, "findings": findings or []}

    if (not isinstance(evidence.head_sha, str) or not SHA.fullmatch(evidence.head_sha)
            or not isinstance(evidence.trusted_base_sha, str) or not SHA.fullmatch(evidence.trusted_base_sha)
            or evidence.context_complete is not True):
        return outcome("incomplete", "Exact revisions and complete authenticated context are required")
    expected = set(evidence.required_checks)
    observed = {check.id for check in evidence.checks}
    if (not expected or len(expected) != len(evidence.required_checks)
            or observed != expected or len(observed) != len(evidence.checks)):
        return outcome("incomplete", "Required check evidence is missing, duplicated, or unexpected")
    if any(check.head_sha != evidence.head_sha or check.trusted_base_sha != evidence.trusted_base_sha
           or type(check.exit_code) is not int for check in evidence.checks):
        return outcome("incomplete", "Check evidence does not match the reviewed revisions")
    failed = [check.id for check in evidence.checks if check.exit_code != 0]
    if failed:
        return outcome("blockers", "Required checks failed", [{
            "severity": "blocker", "evidence": "Failed checks: " + ", ".join(failed),
            "files": [], "remediation_kind": "code_change"}])
    try:
        result = reviewer(evidence)
        if not valid_result(result, evidence.head_sha):
            return outcome("incomplete", "Reviewer output failed validation")
    except Exception:
        return outcome("incomplete", "Reviewer failed; no completion claim is available")
    return copy.deepcopy(result)
