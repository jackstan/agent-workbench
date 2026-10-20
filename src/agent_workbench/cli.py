"""Offline preparation/demo commands and an explicit read-only Linear boundary."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

from . import __version__
from .config import load_config
from .ledger import SQLiteLedger


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-workbench")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("check-config", help="Validate configuration without executing its commands")
    config.add_argument("path", type=Path)
    demo = sub.add_parser("demo", help="Replay a scripted example with real temporary Git operations and tests")
    demo.add_argument("--json", action="store_true", help="Print the complete evidence record")
    demo.add_argument("--output", type=Path, help="Save a new JSON evidence file; existing files are never replaced")
    initialize = sub.add_parser("ledger-init", help="Create a new local approval ledger; never overwrite one")
    initialize.add_argument("path", type=Path)
    receipt = sub.add_parser("ledger-receipt", help="Inspect a consumed approval without resetting it")
    receipt.add_argument("path", type=Path)
    receipt.add_argument("approval_key")
    prepare = sub.add_parser("prepare-linear-proposal", help="Render a proposal and digest without network calls")
    prepare.add_argument("project", type=Path)
    prepare.add_argument("proposal", type=Path)
    prepare.add_argument("--linear-config", required=True, type=Path)
    prepare.add_argument("--base-sha", required=True)
    authorize = sub.add_parser("authorize-linear", help="Read current Linear approval and consume it locally")
    authorize.add_argument("project", type=Path)
    authorize.add_argument("--linear-config", required=True, type=Path)
    authorize.add_argument("--issue", required=True, help="Canonical Linear issue UUID")
    authorize.add_argument("--proposal-comment", required=True, help="Canonical proposal comment UUID")
    authorize.add_argument("--base-sha", required=True, help="Base commit independently resolved by the operator")
    authorize.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-config":
            value = load_config(args.path)
            print(json.dumps({"valid": True, "executes_commands": False, "config": asdict(value)}, indent=2))
            return 0
        if args.command == "ledger-init":
            SQLiteLedger.initialize(args.path)
            print(json.dumps({"initialized": str(args.path.absolute()), "schema_version": 1}))
            return 0
        if args.command == "ledger-receipt":
            print(json.dumps(SQLiteLedger(args.path).receipt(args.approval_key), indent=2))
            return 0
        if args.command in {"prepare-linear-proposal", "authorize-linear"}:
            from .linear import LinearApprovalAdapter, LinearClient, load_linear_config, prepare_proposal, _json
            project = load_config(args.project)
            settings = load_linear_config(args.linear_config)
            if args.command == "prepare-linear-proposal":
                with args.proposal.open("rb") as stream:
                    raw = stream.read(64_001)
                if len(raw) > 64_000:
                    raise ValueError("Proposal exceeds 64 KB")
                result = prepare_proposal(project, settings, _json(raw), args.base_sha)
            else:
                api_key = os.environ.get("AGENT_WORKBENCH_LINEAR_API_KEY")
                access_token = os.environ.get("AGENT_WORKBENCH_LINEAR_ACCESS_TOKEN")
                if bool(api_key) == bool(access_token):
                    raise ValueError("Set exactly one workbench Linear API key or OAuth access token")
                client = LinearClient(api_key or access_token, oauth=bool(access_token))
                result = LinearApprovalAdapter(client, project, settings).authorize(
                    args.issue, args.proposal_comment, expected_base_sha=args.base_sha,
                    ledger=SQLiteLedger(args.ledger),
                ).to_dict()
            print(json.dumps(result, indent=2))
            return 0
        if args.output and args.output.exists():
            raise ValueError("Output already exists; select a new file")
        from .demo import run_demo
        result = run_demo()
        encoded = json.dumps(result, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        if args.json:
            print(encoded, end="")
        else:
            print("Agent Workbench — offline fixture replay")
            print("  Application tests: reproduced failure → passed after scripted patch")
            print("  Approval replay: rejected")
            print("  Change scope: " + ", ".join(result["changed_paths"]))
            print("  Review: " + result["review"]["verdict"])
            print("  Final state: " + result["decision"]["state"])
            print("  Human merge remains required.")
            print("  No model calls, remote writes, deployment, or merge occurred.")
            if args.output:
                print("  Evidence saved: " + str(args.output))
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"agent-workbench: {error}", file=sys.stderr)
        return 2
