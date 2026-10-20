import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from agent_workbench.cli import main
from agent_workbench.demo import run_demo


class DemonstrationTests(unittest.TestCase):
    def test_real_git_and_test_replay_stops_at_human_gate(self):
        result = run_demo()
        self.assertNotEqual(result["before_test_exit_code"], 0)
        self.assertEqual(result["after_test_exit_code"], 0)
        self.assertEqual(result["changed_paths"], ["src/slugify.py"])
        self.assertTrue(result["duplicate_approval_rejected"])
        self.assertEqual(result["decision"]["state"], "ready_for_human")
        self.assertEqual(result["external_actions"], [])
        self.assertEqual(result["mode"], "offline_fixture_replay")

    def test_cli_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text("keep this evidence")
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(["demo", "--output", str(report)])
            self.assertEqual(code, 2)
            self.assertEqual(report.read_text(), "keep this evidence")

    def test_examples_match_packaged_configuration(self):
        root = Path(__file__).parents[1]
        self.assertEqual((root / "examples/demo/project.toml").read_bytes(),
                         (root / "src/agent_workbench/data/project.toml").read_bytes())
