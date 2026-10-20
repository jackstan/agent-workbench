"""Project settings are trusted operator input, never model or PR input."""
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from .policy import validate_path_rule


@dataclass(frozen=True)
class Check:
    id: str
    command: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int
    project_id: str
    repository: str
    base_branch: str
    approver_actor_id: str
    approved_paths: tuple[str, ...]
    max_repair_attempts: int
    checks: tuple[Check, ...]


def parse_config(value: dict) -> ProjectConfig:
    required = {"schema_version", "project_id", "repository", "base_branch",
                "approver_actor_id", "approved_paths", "max_repair_attempts", "checks"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Project configuration has missing or unknown fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported configuration schema_version")
    for key in ("project_id", "repository", "base_branch", "approver_actor_id"):
        if (not isinstance(value[key], str) or not value[key].strip()
                or len(value[key]) > 200 or any(ord(c) < 32 for c in value[key])):
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise ValueError("repository must have owner/name form")
    branch = value["base_branch"]
    if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))):
        raise ValueError("Invalid base_branch")
    paths = value["approved_paths"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
        raise ValueError("approved_paths requires 1–100 rules")
    for rule in paths:
        validate_path_rule(rule)
    attempts = value["max_repair_attempts"]
    if type(attempts) is not int or not 0 <= attempts <= 2:
        raise ValueError("max_repair_attempts must be between 0 and 2")
    rows = value["checks"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("At least one required check must be configured")
    checks = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "command", "timeout_seconds"}:
            raise ValueError("A check requires id, command, and timeout_seconds")
        if not isinstance(row["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", row["id"]):
            raise ValueError("Invalid check id")
        command = row["command"]
        if (not isinstance(command, list) or not 1 <= len(command) <= 30
                or any(not isinstance(s, str) or not s or "\x00" in s for s in command)):
            raise ValueError("Check command must be a nonempty argument array")
        timeout = row["timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= 900:
            raise ValueError("Check timeout_seconds must be between 1 and 900")
        checks.append(Check(row["id"], tuple(command), timeout))
    if len({check.id for check in checks}) != len(checks):
        raise ValueError("Check ids must be unique")
    return ProjectConfig(**{**value, "approved_paths": tuple(paths), "checks": tuple(checks)})


def load_config(path: Path) -> ProjectConfig:
    with path.open("rb") as stream:
        raw = stream.read(64_001)
    if len(raw) > 64_000:
        raise ValueError("Configuration exceeds 64 KB")
    return parse_config(tomllib.loads(raw.decode("utf-8")))
