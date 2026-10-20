"""Trusted validation and application boundary for untrusted Builder patches."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shlex
import subprocess
from pathlib import Path, PurePosixPath


SENSITIVE_PREFIXES = (".github/", ".agent/", ".agent-workbench/", "agents/", "src/agent_workbench/")
SENSITIVE_FILES = {
    ".env", ".env.example", "AGENTS.md", "requirements.txt", "agent-workbench.toml",
    "frontend/package.json", "frontend/package-lock.json", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "yarn.lock", "pnpm-lock.yaml",
}
SENSITIVE_BASENAMES = {PurePosixPath(path).name.lower() for path in SENSITIVE_FILES} | {
    "package.json", "package-lock.json", "pyproject.toml", "setup.py", "setup.cfg",
}
MAX_PATCH_BYTES = 2_000_000


def _sensitive(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (name in SENSITIVE_BASENAMES or path.startswith(SENSITIVE_PREFIXES)
            or any(part.startswith(".") for part in PurePosixPath(path).parts)
            or "config" in name or name.startswith("requirements") or name.endswith((".yaml", ".yml", ".toml"))
            or name.startswith(".env") or name.endswith((".pem", ".key"))
            or "credential" in name or "secret" in name)



def validate_path_rule(rule: str) -> None:
    """Validate a trusted allowlist entry. Protected paths still win per file."""
    if (not isinstance(rule, str) or not rule.strip() or rule.startswith('/')
            or '\\' in rule or '..' in rule.split('/') or '\x00' in rule
            or any(ord(c) < 32 for c in rule) or rule.startswith('.')):
        raise ValueError('approved_paths contains an unsafe rule')
    if _sensitive(rule):
        raise ValueError('approved_paths contains a protected rule')


def validate_authorization(authorization: dict, repository: str, checkout: Path) -> None:
    if authorization.get("mode") != "automatic":
        raise ValueError("Automatic patch application requires automatic authorization")
    if authorization.get("repository") != repository:
        raise ValueError("authorization repository mismatch")
    expected = authorization.get("base_sha")
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                            check=True, text=True, capture_output=True).stdout.strip()
    if expected != actual:
        raise ValueError("checkout does not match resolved base SHA")
    if not authorization.get("base_branch") or not authorization.get("branch_name"):
        raise ValueError("authorization branch binding is incomplete")
    paths = authorization.get("approved_paths")
    proposal_id = authorization.get("audit_event", {}).get("details", {}).get("proposal_id")
    if not isinstance(paths, (list, tuple)) or not paths or not proposal_id:
        raise ValueError("authorization scope is incomplete")
    material = "\0".join((proposal_id, authorization.get("approval_key", ""), *paths))
    digest = hashlib.sha256(material.encode()).hexdigest()
    if digest != authorization.get("scope_digest"):
        raise ValueError("authorization scope digest mismatch")


def _safe_path(value: str) -> str:
    path = value[2:] if value.startswith(("a/", "b/")) else value
    parsed = PurePosixPath(path)
    if (not path or path == "/dev/null" or parsed.is_absolute() or ".." in parsed.parts
            or "\\" in path or path.startswith(".git/")):
        raise ValueError(f"unsafe patch path: {value}")
    return path


def patch_paths(data: bytes) -> tuple[str, ...]:
    if not data.strip():
        raise ValueError("patch must be nonempty")
    if b"GIT binary patch" in data or b"Binary files " in data or b"\x00" in data:
        raise ValueError("binary patches are not permitted")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("patch must be UTF-8 text") from exc
    paths: list[str] = []
    for line in lines:
        if line.startswith(("new file mode ", "new mode ", "old mode ", "deleted file mode ")):
            if line.rsplit(" ", 1)[-1] not in {"100644", "100755"}:
                raise ValueError("symlink/submodule modes are not permitted")
        if line.startswith("index ") and line.rsplit(" ", 1)[-1] in {"120000", "160000"}:
            raise ValueError("symlink/submodule modes are not permitted")
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4:
                raise ValueError("malformed diff header")
            paths.extend((_safe_path(parts[2]), _safe_path(parts[3])))
        elif line.startswith(("--- ", "+++ ")):
            parts = shlex.split(line[4:])
            if not parts:
                raise ValueError("malformed patch path header")
            if parts[0] != "/dev/null":
                paths.append(_safe_path(parts[0]))
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            raise ValueError("renamed or copied files are not permitted")
    if not paths:
        raise ValueError("patch contains no changed paths")
    return tuple(dict.fromkeys(paths))


def validate_patch(data: bytes, authorization: dict) -> tuple[str, ...]:
    if len(data) > MAX_PATCH_BYTES:
        raise ValueError("Patch exceeds size budget")
    paths = patch_paths(data)
    allowed = authorization.get("approved_paths", [])
    if not isinstance(allowed, (list, tuple)) or not allowed:
        raise ValueError("approved_paths must be nonempty")
    for rule in allowed:
        validate_path_rule(rule)
    for path in paths:
        if _sensitive(path):
            raise ValueError(f"sensitive path is not permitted: {path}")
        if not any(fnmatch.fnmatchcase(path, rule) for rule in allowed):
            raise ValueError(f"path is outside approved scope: {path}")
    return paths


def apply_patch(authorization: dict, repository: str, checkout: Path,
                patch: Path) -> tuple[str, ...]:
    """Validate captured bytes in a private index, then atomically publish that index.

    The caller owns an inert checkout exclusively. The standard index lock also
    excludes cooperating Git writers. A hard interruption can leave a stale lock;
    it must be inspected, never automatically stolen by a later invocation.
    """
    index_name = subprocess.run(["git", "rev-parse", "--git-path", "index"], cwd=checkout,
                                check=True, text=True, capture_output=True).stdout.rstrip("\n")
    index = (checkout / index_name).resolve()
    lock_path = index.with_name(index.name + ".lock")
    # Exclusive creation happens outside cleanup so an existing owner's lock is untouched.
    lock = lock_path.open("xb")
    promoted = False
    try:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        with lock:
            validate_authorization(authorization, repository, checkout)
            if subprocess.run(["git", "status", "--porcelain"], cwd=checkout, env=env,
                              check=True, text=True, capture_output=True).stdout.strip():
                raise ValueError("Target checkout must be clean")
            with patch.open("rb") as stream:
                data = stream.read(MAX_PATCH_BYTES + 1)
            paths = validate_patch(data, authorization)
            lock.write(index.read_bytes())

        # Git writes this private index via index.lock.lock; the real index is untouched.
        env["GIT_INDEX_FILE"] = str(lock_path)
        subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "apply", "--cached", "--check", "-"],
                       cwd=checkout, env=env, input=data, check=True)
        subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "apply", "--cached", "-"],
                       cwd=checkout, env=env, input=data, check=True)
        actual = subprocess.run(["git", "diff", "--cached", "--no-ext-diff", "--no-textconv",
                                 "--name-only", "-z"], cwd=checkout, env=env, check=True,
                                capture_output=True).stdout.decode().split("\0")
        if not set(filter(None, actual)) or not set(filter(None, actual)).issubset(paths):
            raise ValueError("Git interpreted a different or empty patch scope")
        entries = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=checkout, env=env,
                                 check=True, capture_output=True).stdout.decode().split("\0")
        for entry in filter(None, entries):
            metadata, path = entry.split("\t", 1)
            if path in actual and metadata.split()[0] not in {"100644", "100755"}:
                raise ValueError("Git index contains a symlink/submodule change")
        os.replace(lock_path, index)
        promoted = True
        return paths
    finally:
        lock.close()
        if not promoted:
            lock_path.unlink(missing_ok=True)
