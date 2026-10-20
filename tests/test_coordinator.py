from dataclasses import replace
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from agent_workbench.config import Check, ProjectConfig
from agent_workbench.coordinator import CoordinationError, Journal, run_cycle
from agent_workbench.review import CheckResult, ReviewEvidence

BASE, FIRST, SECOND, THIRD = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
KEY = "e" * 64
CONFIG = ProjectConfig(1, "fixture", "owner/repo", "main", "human",
                       ("src/app.py",), 2, (Check("unit", ("python", "-m", "unittest"), 30),))
AUTH = {"approval_key": KEY, "base_sha": BASE, "repository": CONFIG.repository,
        "approved_paths": list(CONFIG.approved_paths)}


def verdict(head, fail=False, path="src/app.py", kind="code_change"):
    return {"verdict": "blockers" if fail else "pass", "head_sha": head,
            "summary": "Fixture verification", "findings": [{
                "severity": "blocker", "evidence": "expected 240, got 180",
                "files": [path], "remediation_kind": kind}] if fail else []}


class FixtureAdapter:
    """Scripted policy fixture, not live agent proof."""
    def __init__(self):
        self.head, self.calls, self.builds = FIRST, [], 0
        self.transform = lambda value: value

    def current_head(self):
        return self.head

    def revalidate(self):
        self.calls.append(("authority", self.head))
        return {"authenticated_fixture": True}

    def verify(self, head):
        self.calls.append(("verify", head))
        evidence = ReviewEvidence(head, BASE, ("unit",), (CheckResult("unit", head, BASE, 0),), True)
        return self.transform((evidence, verdict(head, head == FIRST)))

    def repair(self, head, feedback, attempt):
        assert feedback["head_sha"] == head and feedback["verdict"] == "blockers"
        self.calls.append(("repair", head))
        self.builds += 1
        self.head = SECOND if self.builds == 1 else THIRD
        return self.head


def race_begin(path, ready, start, output):
    ready.put(True)
    start.wait(5)
    try:
        Journal(path).begin(KEY, {"test": "race"})
        output.put("won")
    except CoordinationError:
        output.put("blocked")


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cycle.sqlite"
        self.store = Journal.initialize(self.path)
        self.adapter = FixtureAdapter()

    def run_cycle(self):
        return run_cycle(CONFIG, AUTH, FIRST, self.store, self.adapter)

    def test_real_dispatch_handoff_and_fresh_review_order(self):
        result = self.run_cycle()
        self.assertEqual(result["state"], "ready_for_human")
        self.assertEqual(result["head_sha"], SECOND)
        self.assertEqual([c for c in self.adapter.calls if c[0] != "authority"],
                         [("verify", FIRST), ("repair", FIRST), ("verify", SECOND)])
        events = self.store.export(KEY)
        reservation = next(e for e in events if e["kind"] == "repair_reserved")
        self.assertEqual(reservation["payload"]["feedback"], verdict(FIRST, True))
        self.assertEqual(result["repairs"], 1)

    def test_completed_replay_is_historical_and_never_dispatches_again(self):
        self.run_cycle()
        count = len(self.adapter.calls)
        result = self.run_cycle()
        self.assertTrue(result["replayed"])
        self.assertEqual(len(self.adapter.calls), count)

    def test_stale_review_of_first_head_cannot_pass_second(self):
        self.adapter.transform = lambda pair: (pair[0], verdict(FIRST, pair[0].head_sha == FIRST))
        result = self.run_cycle()
        self.assertEqual(result["state"], "waiting_for_verifier")
        self.assertEqual(result["repairs"], 1)

    def test_failed_checks_override_model_pass(self):
        self.adapter.transform = lambda pair: (
            replace(pair[0], checks=(CheckResult("unit", pair[0].head_sha, BASE,
                                                int(pair[0].head_sha == FIRST)),)),
            verdict(pair[0].head_sha))
        self.assertEqual(self.run_cycle()["repairs"], 1)

    def test_wrong_required_check_set_halts_without_dispatch(self):
        self.adapter.transform = lambda pair: (replace(pair[0], required_checks=("other",)), pair[1])
        with self.assertRaises(CoordinationError):
            self.run_cycle()
        self.assertEqual(self.adapter.builds, 0)

    def test_out_of_scope_finding_and_external_blocker_require_human(self):
        for field in ("scope", "external"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                store = Journal.initialize(Path(directory) / "cycle.sqlite")
                adapter = FixtureAdapter()
                adapter.transform = lambda pair: (pair[0], verdict(
                    pair[0].head_sha, True,
                    path="tests/test_app.py" if field == "scope" else "src/app.py",
                    kind="human_action" if field == "external" else "code_change"))
                result = run_cycle(CONFIG, AUTH, FIRST, store, adapter)
                self.assertEqual(result["state"], "human_intervention_required")
                self.assertEqual(adapter.builds, 0)

    def test_two_attempt_limit_is_enforced(self):
        self.adapter.transform = lambda pair: (pair[0], verdict(pair[0].head_sha, True))
        self.assertEqual(self.run_cycle()["state"], "human_intervention_required")
        self.assertEqual(self.adapter.builds, 2)

    def test_lost_builder_response_keeps_reservation_and_blocks_replay(self):
        def lost(*args):
            self.adapter.builds += 1
            raise TimeoutError("outcome unknown")
        self.adapter.repair = lost
        with self.assertRaises(TimeoutError):
            self.run_cycle()
        with self.assertRaises(CoordinationError):
            self.run_cycle()
        self.assertEqual(self.adapter.builds, 1)
        self.assertIn("repair_reserved", [e["kind"] for e in self.store.export(KEY)])

    def test_revocation_before_repair_prevents_builder(self):
        count = 0
        def authority():
            nonlocal count
            count += 1
            if count > 1:
                raise ValueError("revoked")
            return {}
        self.adapter.revalidate = authority
        with self.assertRaises(ValueError):
            self.run_cycle()
        self.assertEqual(self.adapter.builds, 0)

    def test_head_change_during_verification_prevents_dispatch(self):
        def changed(pair):
            self.adapter.head = SECOND
            return pair
        self.adapter.transform = changed
        with self.assertRaises(CoordinationError):
            self.run_cycle()
        self.assertEqual(self.adapter.builds, 0)

    def test_two_processes_cannot_own_same_approval(self):
        ctx = multiprocessing.get_context("spawn")
        ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=race_begin, args=(self.path, ready, start, output)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for _ in workers:
            ready.get(timeout=10)
        start.set()
        self.assertCountEqual([output.get(timeout=10) for _ in workers], ["won", "blocked"])
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)

    def test_initialize_never_resets_existing_journal(self):
        self.store.begin(KEY, {})
        with self.assertRaises(FileExistsError):
            Journal.initialize(self.path)
