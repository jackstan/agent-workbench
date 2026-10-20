"""Real Git/application integration; model output is explicitly a fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench.codex_local import LocalCodexAdapter
from agent_workbench.demo import run_demo


class LocalAdapterTests(unittest.TestCase):
    def test_repair_validates_cumulative_patch_against_original_base_and_preserves_parent(self):
        from agent_workbench.config import load_config
        from agent_workbench.dispatch import _scope_digest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                                      cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-b", "agent/test")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.test")
            (repo / "src").mkdir()
            (repo / "src/slugify.py").write_text("value = 0\n")
            git("add", ".")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            (repo / "src/slugify.py").write_text("value = 1\n")
            git("commit", "-am", "candidate")
            initial = git("rev-parse", "HEAD")
            config = load_config(Path(__file__).parents[1] / "examples/demo/project.toml")
            paths = list(config.approved_paths)
            auth = {"mode": "automatic", "repository": config.repository, "base_sha": base,
                    "base_branch": "main", "branch_name": "agent/test", "approval_key": "e" * 64,
                    "approved_paths": paths,
                    "audit_event": {"details": {"proposal_id": "fixture"}},
                    "scope_digest": _scope_digest("fixture", "e" * 64, tuple(paths)),
                    "proposal_binding": {"envelope": {"proposal": {"problem": "fixture"}}}}
            # Example config uses a glob; local provider requires literal files.
            from dataclasses import replace
            config = replace(config, approved_paths=("src/slugify.py",))
            auth["approved_paths"] = list(config.approved_paths)
            auth["scope_digest"] = _scope_digest("fixture", "e" * 64, config.approved_paths)
            with patch("agent_workbench.codex_local.sys.platform", "darwin"), \
                    patch.object(LocalCodexAdapter, "execute", return_value=0), \
                    patch.dict(os.environ, GH_TOKEN="fixture-github-token",
                               OPENAI_API_KEY="fixture-api-key"):
                adapter = LocalCodexAdapter(
                    checkout=repo, workspace=root / "workers", evidence=root / "evidence",
                    config=config, authorization=auth, authority=lambda: {},
                    codex=Path(sys.executable), python=Path(sys.executable), denied_paths=(),
                    proxy_url="http://127.0.0.1:12345/v1")
            self.assertNotIn("GH_TOKEN", adapter.env)
            self.assertNotIn("OPENAI_API_KEY", adapter.env)
            profile = " ".join(adapter.profile(repo, True))
            self.assertIn("http://127.0.0.1:12345/v1", profile)
            self.assertNotIn("fixture-api-key", profile)
            self.assertNotIn("fixture-github-token", profile)
            def builder(name, checkout, prompt, schema, writable=False):
                self.assertIn("expected 2", prompt)
                (checkout / "src/slugify.py").write_text("value = 2\n")
                return {"status": "complete", "summary": "scripted fixture"}
            adapter.model = builder
            head = adapter.repair(initial, {"finding": "expected 2"}, 1)
            self.assertEqual(git("rev-parse", "HEAD"), head)
            self.assertEqual(git("rev-parse", "HEAD^"), initial)
            self.assertEqual((repo / "src/slugify.py").read_text(), "value = 2\n")
            self.assertEqual(auth["base_sha"], base)
            self.assertEqual(git("status", "--porcelain"), "")
