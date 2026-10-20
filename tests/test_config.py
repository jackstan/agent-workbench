from copy import deepcopy
from pathlib import Path
import tempfile
import tomllib
import unittest

from agent_workbench.config import load_config, parse_config

EXAMPLE = Path(__file__).parents[1] / "examples/demo/project.toml"


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.value = tomllib.loads(EXAMPLE.read_text())

    def test_example_is_complete_and_neutral(self):
        config = load_config(EXAMPLE)
        self.assertEqual(config.repository, "example/slug-demo")
        self.assertEqual(config.checks[0].command[0], "python")

    def test_another_project_needs_no_engine_change(self):
        value = deepcopy(self.value)
        value.update(project_id="notes", repository="example/notes", approver_actor_id="different-operator",
                     approved_paths=["app/*.js", "test/*.js"])
        value["checks"] = [{"id": "javascript", "command": ["node", "--test"], "timeout_seconds": 60}]
        self.assertEqual(parse_config(value).checks[0].command, ("node", "--test"))

    def test_unknown_fields_and_schema_versions_rejected(self):
        for value in ({**self.value, "extra": True}, {**self.value, "schema_version": 2},
                      {**self.value, "schema_version": True}):
            with self.assertRaises(ValueError):
                parse_config(value)

    def test_unsafe_scope_is_rejected(self):
        for path in ("../outside", "/tmp/app.py", ".github/*", "src/agent_workbench/*", "app\\file.py", ""):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_config({**self.value, "approved_paths": [path]})

    def test_invalid_checks_and_repair_limits_are_rejected(self):
        for attempts in (-1, 3, True):
            with self.assertRaises(ValueError):
                parse_config({**self.value, "max_repair_attempts": attempts})
        for checks in ([], [self.value["checks"][0]] * 2,
                       [{"id": "unit", "command": "python -m unittest", "timeout_seconds": 30}],
                       [{"id": "unit", "command": ["python"], "timeout_seconds": 0}]):
            with self.assertRaises(ValueError):
                parse_config({**self.value, "checks": checks})

    def test_branch_and_repository_validation(self):
        for key, value in (("repository", "https://example.com/repo"), ("base_branch", "--upload-pack=x"),
                           ("base_branch", "a/../b"), ("base_branch", "main.lock")):
            with self.assertRaises(ValueError):
                parse_config({**self.value, key: value})

    def test_configuration_size_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.toml"
            path.write_text("#" * 64_001)
            with self.assertRaisesRegex(ValueError, "64 KB"):
                load_config(path)
