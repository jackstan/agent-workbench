from dataclasses import replace
import unittest
from unittest.mock import Mock

from agent_workbench.review import CheckResult, ReviewEvidence, review
from agent_workbench.repair import decide

HEAD, BASE = "a" * 40, "b" * 40


def result(verdict="pass", kind="code_change"):
    findings = [] if verdict != "blockers" else [{"severity": "blocker", "evidence": "Observed failure",
                                                 "files": ["src/app.py"], "remediation_kind": kind}]
    return {"verdict": verdict, "head_sha": HEAD, "summary": "Fixture review", "findings": findings}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.evidence = ReviewEvidence(HEAD, BASE, ("unit",), (CheckResult("unit", HEAD, BASE, 0),), True)

    def test_valid_review_can_pass(self):
        self.assertEqual(review(self.evidence, lambda _: result())["verdict"], "pass")

    def test_missing_duplicate_and_stale_evidence_prevent_model_call(self):
        cases = [replace(self.evidence, checks=()), replace(self.evidence, checks=self.evidence.checks * 2),
                 replace(self.evidence, context_complete=False), replace(self.evidence, required_checks=()),
                 replace(self.evidence, checks=(CheckResult("unit", "c" * 40, BASE, 0),)),
                 replace(self.evidence, checks=(CheckResult("unit", HEAD, "c" * 40, 0),)),
                 replace(self.evidence, checks=(CheckResult("unit", HEAD, BASE, False),))]
        for evidence in cases:
            reviewer = Mock()
            self.assertEqual(review(evidence, reviewer)["verdict"], "incomplete")
            reviewer.assert_not_called()

    def test_failed_checks_cannot_be_overridden_by_review(self):
        reviewer = Mock(return_value=result())
        output = review(replace(self.evidence, checks=(CheckResult("unit", HEAD, BASE, 1),)), reviewer)
        self.assertEqual(output["verdict"], "blockers")
        reviewer.assert_not_called()

    def test_failure_and_malformed_model_output_are_incomplete(self):
        for output in (None, [], {**result(), "verdict": []}, {**result(), "head_sha": "c" * 40},
                       result("blockers") | {"verdict": "pass"}, result() | {"verdict": "blockers"}):
            self.assertEqual(review(self.evidence, lambda _, x=output: x)["verdict"], "incomplete")
        self.assertEqual(review(self.evidence, Mock(side_effect=TimeoutError()))["verdict"], "incomplete")

    def test_reviewer_cannot_mutate_returned_result_later(self):
        original = result()
        output = review(self.evidence, lambda _: original)
        original["verdict"] = "blockers"
        self.assertEqual(output["verdict"], "pass")


class RepairTests(unittest.TestCase):
    def test_pass_requires_human_and_never_dispatches(self):
        decision = decide(result(), HEAD, set())
        self.assertEqual(decision.state, "ready_for_human")
        self.assertFalse(decision.dispatch)

    def test_stale_or_malformed_review_cannot_dispatch(self):
        for output in (None, result() | {"head_sha": "c" * 40}, result() | {"verdict": []}):
            self.assertFalse(decide(output, HEAD, set()).dispatch)

    def test_repairs_are_bounded_and_duplicate_heads_do_not_dispatch(self):
        self.assertTrue(decide(result("blockers"), HEAD, set()).dispatch)
        self.assertFalse(decide(result("blockers"), HEAD, {HEAD}).dispatch)
        exhausted = decide(result("blockers"), HEAD, {"c" * 40, "d" * 40})
        self.assertEqual(exhausted.state, "human_intervention_required")
        self.assertFalse(exhausted.dispatch)
        self.assertFalse(decide(result("blockers"), HEAD, set(), 0).dispatch)

    def test_external_work_is_not_a_code_repair(self):
        for kind, state in (("human_action", "human_intervention_required"),
                            ("deployment_verification", "awaiting_deployment_verification")):
            decision = decide(result("blockers", kind), HEAD, set())
            self.assertEqual(decision.state, state)
            self.assertFalse(decision.dispatch)

    def test_repair_limit_cannot_be_increased(self):
        with self.assertRaises(ValueError):
            decide(result("blockers"), HEAD, set(), 3)
