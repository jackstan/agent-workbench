"""GitHub-hosted boundary around the existing bounded repair coordinator.

Trusted jobs reserve a run and publish; the model job only has read permissions.
An immutable Git tag reservation survives runner loss. Reruns are refused, not
treated as resumable. This module never merges a PR.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .codex_local import LocalCodexAdapter
from .config import load_config, parse_config
from .coordinator import Journal, run_cycle
from .dispatch import _scope_digest
from .linear import _proposal
from .policy import apply_patch, validate_patch
from .review import CheckResult, ReviewEvidence, review, SHA


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    data = Path(path).read_bytes()
    if len(data) > 2_000_000:
        raise ValueError("Evidence exceeds size budget")
    return json.loads(data)


def save(path, value):
    with Path(path).open("x") as file:
        file.write(json.dumps(value, indent=2) + "\n")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("GitHub redirects are not accepted")


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise ValueError("Repository and scoped GitHub token required")
        self.repository, self.token = repository, token

    def request(self, path, data=None):
        if not path.startswith("/") or ".." in path:
            raise ValueError("Invalid API path")
        request = Request("https://api.github.com/repos/" + self.repository + path,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"},
                          data=canonical(data).encode() if data is not None else None,
                          method="POST" if data is not None else "GET")
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise ValueError("GitHub response exceeded budget")
            return json.loads(body) if body else {}
        except HTTPError as error:
            raise RuntimeError(f"GitHub {error.code} for {path}; no automatic retry") from None

    def status(self, head, state, description, run_id):
        return self.request("/statuses/" + head, {
            "state": state, "context": "agent-workbench/verifier",
            "description": description[:140],
            "target_url": f"https://github.com/{self.repository}/actions/runs/{run_id}"})


def validate_run(run, pr, *, repository, actor_id, expected_head, expected_base,
                 workflow=".github/workflows/workbench.yml"):
    """Only an explicit owner/approver dispatch on the current trusted base."""
    if (run["event"] != "workflow_dispatch" or run["run_attempt"] != 1
            or run["path"] != workflow or run["head_sha"] != expected_base
            or run["head_branch"] != pr["base"]["ref"]
            or str(run["actor"]["id"]) != actor_id
            or str(run["triggering_actor"]["id"]) != actor_id
            or run["actor"]["type"] != "User" or run["triggering_actor"]["type"] != "User"
            or run["status"] != "in_progress"):
        raise ValueError("Untrusted, stale, cancelled, or repeated workflow invocation")
    if (pr["state"] != "open" or pr["merged"] or pr.get("auto_merge") is not None
            or pr["head"]["repo"]["full_name"] != repository
            or pr["base"]["repo"]["full_name"] != repository
            or pr["head"]["sha"] != expected_head or pr["base"]["sha"] != expected_base
            or not re.fullmatch(r"agent/[A-Za-z0-9_./-]+", pr["head"]["ref"])
            or ".." in pr["head"]["ref"] or pr["head"]["ref"] == pr["base"]["ref"]):
        raise ValueError("PR is stale, closed, foreign, auto-merging, or outside agent branches")


def git(path, *args):
    # Called only by trusted coordination, never to execute candidate code.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                          cwd=path, env=env, capture_output=True, text=True, check=True,
                          timeout=60).stdout.rstrip("\n")


def claim(client, config, proposal, pr, run):
    _proposal(proposal)
    if config.approver_actor_id != "github:" + str(run["actor"]["id"]):
        raise ValueError("GitHub actor is not the configured approver")
    material = json.loads(canonical({"repository": config.repository, "pr": pr["number"],
                "head": pr["head"]["sha"], "base": pr["base"]["sha"],
                "config": asdict(config), "proposal": proposal}))
    key = digest(material)
    receipt = {"approval_key": key, "run_id": run["id"], "run_attempt": 1,
               "workflow_sha": run["head_sha"], "material": material}
    tag = client.request("/git/tags", {
        "tag": "agent-workbench-claim-" + key, "message": canonical(receipt),
        "object": pr["base"]["sha"], "type": "commit",
        "tagger": {"name": "Agent Workbench", "email": "agent-workbench@users.noreply.github.com",
                   "date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}})
    # Atomic ref creation: conflicts/lost responses halt. Never overwrite/delete.
    client.request("/git/refs", {"ref": "refs/tags/agent-workbench/claims/" + key, "sha": tag["sha"]})
    authorization = {
        "mode": "automatic", "approval_key": key, "repository": config.repository,
        "base_sha": pr["base"]["sha"], "base_branch": pr["base"]["ref"],
        "branch_name": pr["head"]["ref"], "approved_paths": list(config.approved_paths),
        "scope_digest": _scope_digest(proposal["proposal_id"], key, config.approved_paths),
        "audit_event": {"kind": "github.workflow.approved", "approval_key": key,
                        "details": {"proposal_id": proposal["proposal_id"], "receipt": receipt}},
        "proposal_binding": {"envelope": {"proposal": proposal}}}
    return {"authorization": authorization, "config": material["config"], "receipt": receipt,
            "claim_sha": tag["sha"], "initial_head": pr["head"]["sha"], "pr_number": pr["number"]}


def revalidate(client, packet):
    validate_packet(packet)
    auth, receipt = packet["authorization"], packet["receipt"]
    if (str(receipt["run_id"]) != os.environ["GITHUB_RUN_ID"]
            or os.environ["GITHUB_RUN_ATTEMPT"] != "1"
            or client.repository != auth["repository"]):
        raise ValueError("Artifact belongs to a different workflow invocation")
    run = client.request(f"/actions/runs/{receipt['run_id']}")
    pr = client.request(f"/pulls/{packet['pr_number']}")
    validate_run(run, pr, repository=auth["repository"],
                 actor_id=packet["config"]["approver_actor_id"].removeprefix("github:"),
                 expected_head=packet["initial_head"], expected_base=auth["base_sha"])
    if (packet["config"]["base_branch"] != pr["base"]["ref"]
            or auth["branch_name"] != pr["head"]["ref"]):
        raise ValueError("Unexpected base branch")
    ref = client.request("/git/ref/tags/agent-workbench/claims/" + auth["approval_key"])
    if ref["object"]["sha"] != packet["claim_sha"]:
        raise ValueError("Claim reference changed")
    tag = client.request("/git/tags/" + packet["claim_sha"])
    if (tag["object"]["type"] != "commit" or tag["object"]["sha"] != auth["base_sha"]
            or json.loads(tag["message"]) != receipt):
        raise ValueError("Durable run reservation does not match")
    return {"run_id": run["id"], "claim_sha": packet["claim_sha"], "remote_head": pr["head"]["sha"]}


def validate_packet(packet):
    auth, receipt = packet["authorization"], packet["receipt"]
    material = receipt["material"]
    expected = {"mode": "automatic", "approval_key": digest(material),
                "repository": material["repository"], "base_sha": material["base"],
                "base_branch": material["config"]["base_branch"],
                "approved_paths": material["config"]["approved_paths"]}
    if (any(auth.get(k) != v for k, v in expected.items())
            or packet["config"] != material["config"]
            or packet["initial_head"] != material["head"] or packet["pr_number"] != material["pr"]
            or receipt["run_attempt"] != 1 or receipt["workflow_sha"] != material["base"]
            or receipt["approval_key"] != auth["approval_key"]
            or auth["audit_event"]["details"]["receipt"] != receipt
            or auth["proposal_binding"]["envelope"]["proposal"] != material["proposal"]
            or auth["scope_digest"] != _scope_digest(
                material["proposal"]["proposal_id"], auth["approval_key"],
                tuple(material["config"]["approved_paths"]))):
        raise ValueError("Run packet differs from its durable approval binding")
    parse_config(packet["config"])


def prepare(args, client):
    config = load_config(args.project)
    proposal = read_json(args.proposal)
    if config.repository != client.repository or not SHA.fullmatch(args.head):
        raise ValueError("Repository or candidate binding is invalid")
    run = client.request("/actions/runs/" + os.environ["GITHUB_RUN_ID"])
    pr = client.request(f"/pulls/{args.pr}")
    base = git(args.checkout, "rev-parse", "HEAD")
    validate_run(run, pr, repository=config.repository,
                 actor_id=config.approver_actor_id.removeprefix("github:"),
                 expected_head=args.head, expected_base=base)
    if pr["base"]["ref"] != config.base_branch:
        raise ValueError("Wrong base branch")
    git(args.checkout, "fetch", "origin", args.head)
    git(args.checkout, "merge-base", "--is-ancestor", base, args.head)
    patch = git(args.checkout, "diff", "--no-ext-diff", "--no-textconv", base, args.head).encode() + b"\n"
    validate_patch(patch, {"approved_paths": config.approved_paths})
    packet = claim(client, config, proposal, pr, run)
    save(args.output, packet)
    client.status(args.head, "pending", "GitHub-hosted verifier and bounded repair cycle started", run["id"])
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("base=" + base + "\n")


class LiveJournal(Journal):
    def event(self, key, kind, payload):
        super().event(key, kind, payload)
        # One JSON line prevents model text from becoming a workflow command.
        print("WORKBENCH " + canonical({"stage": kind, "evidence": payload}), flush=True)


def run_hosted(args, client):
    packet = read_json(args.packet)
    if int(os.environ["GITHUB_RUN_ATTEMPT"]) != 1:
        raise ValueError("Reruns require reconciliation; the old reservation is retained")
    revalidate(client, packet)
    auth = packet["authorization"]
    config = parse_config(packet["config"])
    git(args.checkout, "checkout", "-B", auth["branch_name"], packet["initial_head"])
    # The official action creates a root-owned server info file and drops sudo.
    info_path = Path.home() / ".codex" / (os.environ["GITHUB_RUN_ID"] + ".json")
    if info_path.stat().st_uid != 0:
        raise ValueError("Expected root-owned official action proxy information")
    info = read_json(info_path)
    port = info["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid protected proxy port")
    store_path = args.workspace.parent / "cycle.sqlite"
    Journal.initialize(store_path)
    journal = LiveJournal(store_path)
    adapter = LocalCodexAdapter(
        checkout=args.checkout, workspace=args.workspace, evidence=args.evidence,
        config=config, authorization=auth, authority=lambda: revalidate(client, packet),
        codex=args.codex, python=Path(sys.executable),
        denied_paths=(args.evidence, store_path, args.packet),
        proxy_url=f"http://127.0.0.1:{port}/v1")
    try:
        result = run_cycle(config, auth, packet["initial_head"], journal, adapter)
        adapter.save("result.json", result)
        if result["state"] != "ready_for_human":
            raise ValueError("Cycle stopped without readiness")
        git(args.checkout, "bundle", "create", str(args.evidence / "candidate.bundle"),
            "HEAD", "^" + auth["base_sha"])
        summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
        with summary.open("a") as file:
            file.write(f"## Agent Workbench\n\nVerified `{result['head_sha']}` after "
                       f"{result['repairs']} repair(s). Publication and human merge remain separate.\n")
    finally:
        adapter.save("events.json", journal.export(auth["approval_key"]))


def validate_final(packet, result, events):
    if (result["state"] != "ready_for_human" or result.get("replayed") is not False
            or type(result["repairs"]) is not int
            or not 0 <= result["repairs"] <= packet["config"]["max_repair_attempts"]
            or not events or events[-1]["kind"] != "finished" or events[-1]["payload"] != result):
        raise ValueError("Incomplete or replayed cycle cannot publish")
    reservations = [e for e in events if e["kind"] == "repair_reserved"]
    if (len(reservations) != result["repairs"]
            or len({e["payload"]["head_sha"] for e in reservations}) != len(reservations)):
        raise ValueError("Repair reservation history is inconsistent")
    reviews = [e["payload"] for e in events if e["kind"] == "verification_completed"]
    if not reviews:
        raise ValueError("No verification evidence")
    last = reviews[-1]
    raw = last["evidence"]
    evidence = ReviewEvidence(raw["head_sha"], raw["trusted_base_sha"],
                              tuple(raw["required_checks"]),
                              tuple(CheckResult(**c) for c in raw["checks"]), raw["context_complete"])
    required = tuple(c["id"] for c in packet["config"]["checks"])
    if (evidence.head_sha != result["head_sha"]
            or evidence.trusted_base_sha != packet["authorization"]["base_sha"]
            or evidence.required_checks != required
            or review(evidence, lambda _: last["review"])["verdict"] != "pass"):
        raise ValueError("Final review/check evidence is stale, missing, or failed")
    return reviews


def publish(args, client):
    packet = read_json(args.packet)
    auth, receipt = packet["authorization"], packet["receipt"]
    try:
        revalidate(client, packet)
        result, events = read_json(args.evidence / "result.json"), read_json(args.evidence / "events.json")
        reviews = validate_final(packet, result, events)
        head = result["head_sha"]
        git(args.checkout, "fetch", str(args.evidence / "candidate.bundle"), "HEAD")
        if git(args.checkout, "rev-parse", "FETCH_HEAD") != head:
            raise ValueError("Candidate bundle differs from verified head")
        git(args.checkout, "merge-base", "--is-ancestor", packet["initial_head"], head)
        commits = git(args.checkout, "rev-list", "--count", packet["initial_head"] + ".." + head)
        if int(commits) != result["repairs"]:
            raise ValueError("Unexpected candidate history")
        cumulative = git(args.checkout, "diff", "--no-ext-diff", "--no-textconv",
                         "--full-index", auth["base_sha"], head).encode() + b"\n"
        validate_patch(cumulative, auth)
        patch_path = args.evidence / "published.patch"
        patch_path.write_bytes(cumulative)
        git(args.checkout, "checkout", "--detach", auth["base_sha"])
        apply_patch(auth, auth["repository"], args.checkout, patch_path)
        if git(args.checkout, "write-tree") != git(args.checkout, "rev-parse", head + "^{tree}"):
            raise ValueError("Published tree does not match the validated cumulative patch")
        revalidate(client, packet)
        # Ancestry was checked above. The explicit lease makes this fast-forward
        # conditional on the approved remote head, including concurrent rewinds.
        branch = "refs/heads/" + auth["branch_name"]
        git(args.checkout, "push", "--force-with-lease=" + branch + ":" + packet["initial_head"],
            "origin", head + ":" + branch)
        for row in reviews:
            verdict = row["review"]["verdict"]
            client.status(row["review"]["head_sha"], "success" if verdict == "pass" else "failure",
                          row["review"]["summary"], receipt["run_id"])
        body = (f"GitHub-hosted repair cycle completed: **{result['repairs']} repair(s)**.\n\n"
                f"Verified commit: `{head}`.\n\n"
                f"[Live stage log and full evidence](https://github.com/{auth['repository']}/actions/runs/{receipt['run_id']}). "
                "Human merge remains required.")
        client.request(f"/issues/{packet['pr_number']}/comments", {"body": body})
        # GITHUB_TOKEN pushes do not normally start push/PR workflows.
        client.request("/actions/workflows/ci.yml/dispatches", {"ref": auth["branch_name"]})
        print(body)
    except Exception:
        client.status(packet["initial_head"], "error", "Cycle stopped; inspect Actions evidence before recovery",
                      receipt["run_id"])
        raise


def report_failure(args, client):
    packet = read_json(args.packet)
    # Do not accept a different run's artifact even when reporting failure.
    validate_packet(packet)
    if str(packet["receipt"]["run_id"]) != os.environ["GITHUB_RUN_ID"]:
        raise ValueError("Failure packet belongs to another run")
    client.status(packet["initial_head"], "error",
                  "Cycle stopped; inspect Actions evidence before recovery",
                  packet["receipt"]["run_id"])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "run", "publish", "report-failure"))
    for name in ("project", "proposal", "checkout", "output", "packet", "workspace", "evidence", "codex"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--head")
    args = parser.parse_args(argv)
    client = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    try:
        {"prepare": prepare, "run": run_hosted, "publish": publish,
         "report-failure": report_failure}[args.stage](args, client)
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"agent-workbench: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
