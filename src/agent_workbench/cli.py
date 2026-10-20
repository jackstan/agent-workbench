"""A small, offline command surface. No live credentials are read."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

from . import __version__
from .config import load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-workbench")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("check-config", help="Validate configuration without executing its commands")
    config.add_argument("path", type=Path)
    demo = sub.add_parser("demo", help="Replay a scripted example with real temporary Git operations and tests")
    demo.add_argument("--json", action="store_true", help="Print the complete evidence record")
    demo.add_argument("--output", type=Path, help="Save a new JSON evidence file; existing files are never replaced")
    args = parser.parse_args(argv)
    try:
        if args.command == "check-config":
            value = load_config(args.path)
            print(json.dumps({"valid": True, "executes_commands": False, "config": asdict(value)}, indent=2))
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
