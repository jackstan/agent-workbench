"""Hosted boundary tests. GitHub responses are fixtures; Git operations are real."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_workbench.config import load_config
from agent_workbench.github_cycle import (
    claim, git, publish, revalidate, save, validate_final, validate_packet, validate_run,
)

ROOT = Path(__file__).parents[1]
BASE, HEAD, FINAL = ("a" * 40, "b" * 40, "c" * 40)
REPOSITORY = "owner/sandbox"


def fixture():
    config = replace(load_config(ROOT / "examples/demo/project.toml"),
                     repository=REPOSITORY, approver_actor_id="github:123",
                     approved_paths=("src/slugify.py",))
    proposal = json.loads((ROOT / "tests/fixtures/valid_proposal.json").read_text())
    actor = {"id": 123, "type": "User"}
    run = {"id": 42, "event": "workflow_dispatch", "run_attempt": 1,
           "path": ".github/workflows/workbench.yml", "head_sha": BASE, "head_branch": "main",
           "actor": actor, "triggering_actor": dict(actor), "status": "in_progress"}
    pr = {"number": 7, "state": "open", "merged": False, "auto_merge": None,
          "head": {"sha": HEAD, "ref": "agent/pilot", "repo": {"full_name": REPOSITORY}},
          "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}}}
    return config, proposal, run, pr


class FakeGitHub:
    repository = REPOSITORY

    def __init__(self, run, pr):
        self.run, self.pr = run, pr
        self.tags, self.refs, self.writes, self.statuses = {}, {}, [], []

    def request(self, path, data=None):
        if path.startswith("/actions/runs/"):
            return deepcopy(self.run)
        if path.startswith("/pulls/"):
            return deepcopy(self.pr)
        if path == "/git/tags":
            sha = "d" * 40
            self.tags[sha] = {"object": {"type": data["type"], "sha": data["object"]},
                              "message": data["message"]}
            return {"sha": sha}
        if path == "/git/refs":
            if data["ref"] in self.refs:
                raise RuntimeError("GitHub 422: claim already exists")
            self.refs[data["ref"]] = data["sha"]
            return {}
        if path.startswith("/git/ref/"):
            return {"object": {"sha": self.refs["refs/" + path.removeprefix("/git/ref/")]}}
        if path.startswith("/git/tags/"):
            return deepcopy(self.tags[path.split("/")[-1]])
        self.writes.append((path, data))
        return {}

    def status(self, *args):
        self.statuses.append(args)


def evidence(packet, head=FINAL):
    base = packet["authorization"]["base_sha"]
    check_id = packet["config"]["checks"][0]["id"]
    result = {"state": "ready_for_human", "head_sha": head, "repairs": 1,
              "reason": "", "replayed": False}
    review = {"verdict": "pass", "head_sha": head, "summary": "Fixture review", "findings": []}
    events = [
        {"kind": "repair_reserved", "payload": {"head_sha": packet["initial_head"]}},
        {"kind": "verification_completed", "payload": {
            "evidence": {"head_sha": head, "trusted_base_sha": base, "required_checks": [check_id],
                         "context_complete": True, "checks": [
                             {"id": check_id, "head_sha": head, "trusted_base_sha": base, "exit_code": 0}]},
            "review": review}},
        {"kind": "finished", "payload": deepcopy(result)}]
    return result, events


class GitHubBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.config, self.proposal, self.run, self.pr = fixture()
        self.client = FakeGitHub(self.run, self.pr)
        self.packet = claim(self.client, self.config, self.proposal, self.pr, self.run)
        self.environment = patch.dict(os.environ, GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_exact_owner_dispatch_and_durable_claim_validate(self):
        validate_packet(self.packet)
        self.assertEqual(revalidate(self.client, self.packet)["remote_head"], HEAD)

    def test_duplicate_dispatch_cannot_replace_claim(self):
        with self.assertRaisesRegex(RuntimeError, "claim already exists"):
            claim(self.client, self.config, self.proposal, self.pr, {**self.run, "id": 43})
        self.assertEqual(len(self.client.refs), 1)

    def test_untrusted_stale_cancelled_and_repeated_runs_are_refused(self):
        changes = [
            {"event": "pull_request"}, {"run_attempt": 2}, {"head_sha": FINAL},
            {"head_branch": "agent/pilot"}, {"path": ".github/workflows/other.yml"},
            {"actor": {"id": 999, "type": "User"}},
            {"triggering_actor": {"id": 999, "type": "User"}},
            {"actor": {"id": 123, "type": "Bot"}}, {"status": "completed"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_run({**self.run, **change}, self.pr, repository=REPOSITORY,
                             actor_id="123", expected_head=HEAD, expected_base=BASE)

    def test_foreign_closed_automerge_and_moved_prs_are_refused(self):
        for change in ("fork", "closed", "automerge", "head", "base", "branch"):
            pr = deepcopy(self.pr)
            if change == "fork":
                pr["head"]["repo"]["full_name"] = "outsider/fork"
            elif change == "closed":
                pr["state"] = "closed"
            elif change == "automerge":
                pr["auto_merge"] = {"enabled_by": {"id": 123}}
            elif change in ("head", "base"):
                pr[change]["sha"] = FINAL
            else:
                pr["head"]["ref"] = "main"
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_run(self.run, pr, repository=REPOSITORY, actor_id="123",
                             expected_head=HEAD, expected_base=BASE)

    def test_changed_scope_and_proposal_cannot_reuse_packet(self):
        for change in ("scope", "proposal", "head", "run"):
            packet = deepcopy(self.packet)
            if change == "scope":
                packet["authorization"]["approved_paths"].append("tests/test.py")
            elif change == "proposal":
                packet["authorization"]["proposal_binding"]["envelope"]["proposal"]["problem"] = "replaced"
            elif change == "head":
                packet["initial_head"] = FINAL
            else:
                packet["receipt"]["run_id"] = 99
            with self.subTest(change=change), self.assertRaises(ValueError):
                revalidate(self.client, packet)

    def test_recomputed_packet_hash_does_not_replace_provider_reservation(self):
        packet = deepcopy(self.packet)
        packet["claim_sha"] = "e" * 40
        with self.assertRaisesRegex(ValueError, "reference changed"):
            revalidate(self.client, packet)
        self.client.tags["d" * 40]["message"] = "{}"
        with self.assertRaisesRegex(ValueError, "reservation does not match"):
            revalidate(self.client, self.packet)

    def test_other_run_and_attempt_cannot_consume_same_run_artifact(self):
        for env in ({"GITHUB_RUN_ID": "43"}, {"GITHUB_RUN_ATTEMPT": "2"}):
            with patch.dict(os.environ, env), self.assertRaises(ValueError):
                revalidate(self.client, self.packet)

    def test_failed_or_stale_check_cannot_publish_despite_model_pass(self):
        result, events = evidence(self.packet)
        validate_final(self.packet, result, events)
        for key, value in (("exit_code", 1), ("head_sha", HEAD),
                           ("trusted_base_sha", FINAL), ("id", "unexpected")):
            changed = deepcopy(events)
            changed[1]["payload"]["evidence"]["checks"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_final(self.packet, result, changed)
        for change in ("missing", "replayed", "budget", "reservation"):
            changed, outcome = deepcopy(events), deepcopy(result)
            if change == "missing":
                changed.pop()
            elif change == "replayed":
                outcome["replayed"] = True
            elif change == "budget":
                outcome["repairs"] = 3
            else:
                changed.pop(0)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_final(self.packet, outcome, changed)

    def test_real_bundle_publisher_pushes_only_verified_tree_and_never_merges(self):
        self.publish_fixture()

    def test_concurrent_remote_rewind_prevents_publication(self):
        self.publish_fixture(rewind=True)

    def test_passing_review_cannot_publish_protected_file(self):
        self.publish_fixture(unsafe=True)

    def publish_fixture(self, rewind=False, unsafe=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            candidate.mkdir()
            git(candidate, "init", "-b", "main")
            git(candidate, "config", "user.name", "Fixture")
            git(candidate, "config", "user.email", "fixture@example.test")
            (candidate / "src").mkdir()
            (candidate / "src/slugify.py").write_text("value = 0\n")
            git(candidate, "add", ".")
            git(candidate, "commit", "-m", "base")
            self.run["head_sha"] = self.pr["base"]["sha"] = git(candidate, "rev-parse", "HEAD")
            git(candidate, "checkout", "-b", "agent/pilot")
            (candidate / "src/slugify.py").write_text("value = 1\n")
            git(candidate, "commit", "-am", "candidate")
            self.pr["head"]["sha"] = git(candidate, "rev-parse", "HEAD")
            remote = root / "remote.git"
            git(root, "clone", "--bare", str(candidate), str(remote))
            checkout = root / "publisher"
            git(root, "clone", str(remote), str(checkout))
            (candidate / "src/slugify.py").write_text("value = 2\n")
            if unsafe:
                (candidate / ".github").mkdir()
                (candidate / ".github/forbidden.yml").write_text("value: forbidden\n")
            git(candidate, "add", ".")
            git(candidate, "commit", "-m", "repair")
            final = git(candidate, "rev-parse", "HEAD")
            packet = claim(self.client, self.config, self.proposal, self.pr, self.run)
            packet_path = root / "packet.json"
            save(packet_path, packet)
            output = root / "evidence"
            output.mkdir()
            result, events = evidence(packet, final)
            save(output / "result.json", result)
            save(output / "events.json", events)
            git(candidate, "bundle", "create", str(output / "candidate.bundle"),
                "HEAD", "^" + self.pr["base"]["sha"])
            args = SimpleNamespace(packet=packet_path, evidence=output, checkout=checkout)
            if unsafe:
                with self.assertRaisesRegex(ValueError, "sensitive path"):
                    publish(args, self.client)
                self.assertEqual(git(remote, "rev-parse", "refs/heads/agent/pilot"),
                                 packet["initial_head"])
                self.assertEqual(self.client.writes, [])
                return
            if rewind:
                def concurrent_git(path, *arguments):
                    if arguments[0] == "push":
                        git(remote, "update-ref", "refs/heads/agent/pilot", self.pr["base"]["sha"])
                    return git(path, *arguments)
                with patch("agent_workbench.github_cycle.git", side_effect=concurrent_git), \
                        self.assertRaises(subprocess.CalledProcessError):
                    publish(args, self.client)
                self.assertEqual(git(remote, "rev-parse", "refs/heads/agent/pilot"),
                                 self.pr["base"]["sha"])
                self.assertEqual(self.client.writes, [])
                self.assertEqual(self.client.statuses[-1][1], "error")
                return
            publish(args, self.client)
            self.assertEqual(git(remote, "rev-parse", "refs/heads/agent/pilot"), final)
            self.assertEqual(git(remote, "show", final + ":src/slugify.py"), "value = 2")
            self.assertEqual([p for p, _ in self.client.writes],
                             ["/issues/7/comments", "/actions/workflows/ci.yml/dispatches"])
            self.assertEqual(self.client.statuses[-1][1], "success")


if __name__ == "__main__":
    unittest.main()
