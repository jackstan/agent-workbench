"""Single-host repair coordinator. Adapters are trusted, workers are not.

One durable run per approval. Interrupted/incomplete runs require inspection;
there is deliberately no retry/reset switch that could repeat an uncertain dispatch.
"""
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Protocol

from .config import ProjectConfig
from .policy import _safe_path, _sensitive
from .repair import decide
from .review import ReviewEvidence, SHA, review


class CoordinationError(RuntimeError):
    pass


class Adapter(Protocol):
    def current_head(self) -> str: ...
    def revalidate(self) -> dict: ...
    def verify(self, head: str) -> tuple[ReviewEvidence, dict]: ...
    def repair(self, head: str, findings: dict, attempt: int) -> str: ...


class Journal:
    """Explicitly initialized local SQLite journal, outside worker access."""
    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    @classmethod
    def initialize(cls, path):
        path = Path(path).absolute()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=1")
            db.execute("PRAGMA application_id=1096237874")
            db.execute("CREATE TABLE runs (key TEXT PRIMARY KEY, context TEXT NOT NULL, result TEXT)")
            db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, key TEXT NOT NULL, "
                       "kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE attempts (key TEXT NOT NULL, head TEXT NOT NULL, "
                       "number INTEGER NOT NULL, PRIMARY KEY(key, head), UNIQUE(key, number))")
        return cls(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.execute("PRAGMA synchronous=EXTRA")
        if (db.execute("PRAGMA user_version").fetchone()[0] != 1
                or db.execute("PRAGMA application_id").fetchone()[0] != 1096237874):
            db.close()
            raise CoordinationError("Unsupported journal; preserve it for inspection")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, key, context):
        encoded = json.dumps(context, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT context,result FROM runs WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != encoded:
                    raise CoordinationError("Approval already belongs to a different run context")
                if row[1] is None:
                    raise CoordinationError("Run already started; inspect journal before recovery")
                return json.loads(row[1])
            db.execute("INSERT INTO runs VALUES (?,?,NULL)", (key, encoded))
        return None

    def event(self, key, kind, payload):
        with self.connect() as db:
            db.execute("INSERT INTO events(key,kind,payload,created_at) VALUES (?,?,?,?)",
                       (key, kind, json.dumps(payload, sort_keys=True),
                        datetime.now(timezone.utc).isoformat()))

    def reserve(self, key, head, limit):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM attempts WHERE key=?", (key,)).fetchone()[0]
            if count >= limit:
                raise CoordinationError("Repair limit reached")
            try:
                db.execute("INSERT INTO attempts VALUES (?,?,?)", (key, head, count + 1))
            except sqlite3.IntegrityError as error:
                raise CoordinationError("Repair already reserved") from error
        return count + 1

    def finish(self, key, result):
        self.event(key, "finished", result)
        with self.connect() as db:
            db.execute("UPDATE runs SET result=? WHERE key=?", (json.dumps(result), key))

    def export(self, key):
        with self.connect() as db:
            rows = db.execute("SELECT seq,kind,payload,created_at FROM events WHERE key=? ORDER BY seq",
                              (key,)).fetchall()
        return [{"seq": seq, "kind": kind, "payload": json.loads(payload), "created_at": created}
                for seq, kind, payload, created in rows]


def run_cycle(config: ProjectConfig, authorization: dict, initial_head: str,
              journal: Journal, adapter: Adapter) -> dict:
    """Verify → reserve → repair → verify, bounded by trusted project policy.

    The adapter supplies authenticated authority/current-revision checks and runs
    isolated processes. This function never grants model output publication rights.
    """
    key = authorization["approval_key"]
    if (len(key) != 64 or any(c not in "0123456789abcdef" for c in key)
            or not SHA.fullmatch(initial_head)
            or authorization["repository"] != config.repository
            or tuple(authorization["approved_paths"]) != config.approved_paths):
        raise CoordinationError("Invalid run binding")
    context = {"config": asdict(config), "authorization_sha256": hashlib.sha256(
        json.dumps(authorization, sort_keys=True).encode()).hexdigest(), "initial_head": initial_head}
    existing = journal.begin(key, context)
    if existing is not None:
        # Historical result only: consumers must freshly verify head and authority
        # before publishing. A repeated invocation does not start another worker.
        return {**existing, "replayed": True}
    head, history = initial_head, set()
    try:
        journal.event(key, "started", {"head_sha": head, "base_sha": authorization["base_sha"]})
        while True:
            authority = adapter.revalidate()
            if adapter.current_head() != head:
                raise CoordinationError("Candidate head changed before verification")
            journal.event(key, "verification_started", {"head_sha": head, "authority": authority})
            evidence, model_result = adapter.verify(head)
            if (evidence.head_sha != head
                    or evidence.trusted_base_sha != authorization["base_sha"]
                    or evidence.required_checks != tuple(check.id for check in config.checks)):
                raise CoordinationError("Verification evidence has wrong head, base, or required checks")
            if adapter.current_head() != head:
                raise CoordinationError("Candidate head changed during verification")
            result = review(evidence, lambda _: model_result)
            journal.event(key, "verification_completed",
                          {"evidence": asdict(evidence), "review": result})
            decision = decide(result, head, history, config.max_repair_attempts)
            # A model cannot expand file permissions or turn external work into code work.
            for finding in result["findings"]:
                if finding["severity"] != "blocker":
                    continue
                if finding["remediation_kind"] != "code_change":
                    decision = type(decision)("human_intervention_required",
                                              reason="External blocker requires human action")
                for path in finding["files"]:
                    if (_safe_path(path) != path or _sensitive(path)
                            or not any(fnmatch.fnmatchcase(path, rule) for rule in config.approved_paths)):
                        decision = type(decision)("human_intervention_required",
                                                  reason="Finding is outside approved scope")
            journal.event(key, "decision", asdict(decision) | {"head_sha": head})
            if not decision.dispatch:
                if decision.state == "ready_for_human":
                    journal.event(key, "final_authority", adapter.revalidate())
                    if adapter.current_head() != head:
                        raise CoordinationError("Candidate head changed before readiness")
                final = {"state": decision.state, "head_sha": head, "repairs": len(history),
                         "reason": decision.reason, "replayed": False}
                journal.finish(key, final)
                return final
            adapter.revalidate()
            if adapter.current_head() != head:
                raise CoordinationError("Candidate head changed before repair")
            attempt = journal.reserve(key, head, config.max_repair_attempts)
            journal.event(key, "repair_reserved",
                          {"head_sha": head, "attempt": attempt, "feedback": result})
            history.add(head)
            new_head = adapter.repair(head, result, attempt)
            if not SHA.fullmatch(new_head) or new_head == head or adapter.current_head() != new_head:
                raise CoordinationError("Repair did not produce the expected new commit")
            journal.event(key, "repair_completed",
                          {"old_head_sha": head, "head_sha": new_head, "attempt": attempt})
            head = new_head
    except Exception as error:
        # Leave result NULL: even an exception after a dispatch must not allow a retry.
        journal.event(key, "halted", {"head_sha": head, "error_type": type(error).__name__})
        raise
