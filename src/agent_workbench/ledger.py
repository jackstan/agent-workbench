"""Local, single-host SQLite approval receipts. No reset or automatic recovery."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Mapping


APPLICATION_ID = 0x41574231  # AWB1
SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 2_000_000


class LedgerError(RuntimeError):
    """The caller must stop; an uncertain claim must never be assumed absent."""


def _encode(value) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt is too large")
        return encoded
    except (ValueError, TypeError, RecursionError) as error:
        raise LedgerError("Ledger evidence must be bounded JSON") from error


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise LedgerError("Invalid approval key")


class SQLiteLedger:
    """One shared file for all local runners; keep it outside agent workspaces.

    Opening a missing store is an error, never an implicit reset. The operator
    owns the directory and must preserve this file and any SQLite journal.
    SQLite transactions serialize claims across processes on a local filesystem.
    This is not a distributed ledger or tamper-proof audit service.
    """

    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    @classmethod
    def initialize(cls, path: Path) -> "SQLiteLedger":
        path = Path(path).absolute()
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            connection = sqlite3.connect(path, isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=EXTRA")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute(
                    "CREATE TABLE claims (key TEXT PRIMARY KEY NOT NULL, "
                    "event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE observations (source TEXT NOT NULL, id TEXT NOT NULL, "
                    "record_json TEXT NOT NULL, PRIMARY KEY (source, id))"
                )
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as error:
            # Never delete a possibly committed file after an ambiguous failure.
            raise LedgerError("Could not initialize a new ledger; existing files are never replaced") from error
        return cls(path)

    @contextmanager
    def _connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                         timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous=EXTRA")
            if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                    or connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete"):
                raise LedgerError("Unsupported or uninitialized ledger")
            yield connection
        except sqlite3.Error as error:
            raise LedgerError("Ledger operation failed; inspect receipts before retrying") from error
        finally:
            if connection is not None:
                connection.close()  # Rolls back an uncommitted transaction, never a committed receipt.

    @staticmethod
    def _receipt(connection, key):
        row = connection.execute(
            "SELECT event_json, event_sha256 FROM claims WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            if _digest(row[0]) != row[1] or value.get("approval_key") != key:
                raise ValueError("receipt checksum or identity mismatch")
            return value
        except (ValueError, TypeError, AttributeError) as error:
            raise LedgerError("Invalid stored receipt; preserve the ledger for inspection") from error

    def receipt(self, key: str) -> dict | None:
        _key(key)
        with self._connection() as connection:
            return self._receipt(connection, key)

    def claim(self, key: str, event: Mapping) -> bool:
        return self._claim(key, event)

    def claim_observed(self, key: str, event: Mapping, source: str, records: Mapping) -> bool:
        """Atomically compare the observed controls and consume the approval."""
        return self._claim(key, event, (source, {identifier: _encode(record)
                                               for identifier, record in records.items()}))

    def _claim(self, key: str, event: Mapping, observation=None) -> bool:
        _key(key)
        if not isinstance(event, Mapping) or event.get("approval_key") != key:
            raise LedgerError("Receipt must be bound to its approval key")
        encoded = _encode(dict(event))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if observation is not None:
                source, expected = observation
                current = dict(connection.execute(
                    "SELECT id, record_json FROM observations WHERE source=?", (source,)
                ))
                if current != expected:
                    raise LedgerError("Source observations changed before approval claim")
            if self._receipt(connection, key) is not None:
                connection.rollback()
                return False
            connection.execute("INSERT INTO claims VALUES (?, ?, ?)", (key, encoded, _digest(encoded)))
            connection.commit()
            # A lost response here does not undo the receipt. Retry returns False.
            return True

    def observe(self, source: str, records: Mapping[str, dict]) -> None:
        """Retain complete observed control records, including denied decisions.

        A later complete read cannot silently drop or rewrite previously seen
        records. No claim about history deleted before the first observation.
        """
        if not isinstance(source, str) or not source or len(source) > 500:
            raise LedgerError("Invalid observation source")
        if not isinstance(records, Mapping) or len(records) > 2000:
            raise LedgerError("Invalid observation records")
        _encode(dict(records))  # Bound the whole observation, not just each row.
        encoded = {}
        for identifier, record in records.items():
            if not isinstance(identifier, str) or not identifier or len(identifier) > 500:
                raise LedgerError("Invalid observation identity")
            encoded[identifier] = _encode(record)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = dict(connection.execute(
                "SELECT id, record_json FROM observations WHERE source=?", (source,)
            ))
            if any(encoded.get(identifier) != record for identifier, record in previous.items()):
                raise LedgerError("Previously observed control records were removed or changed")
            connection.executemany("INSERT INTO observations VALUES (?, ?, ?)",
                                   ((source, identifier, record) for identifier, record in encoded.items()
                                    if identifier not in previous))
            connection.commit()
