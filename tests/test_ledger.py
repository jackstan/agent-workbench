import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench.ledger import LedgerError, SQLiteLedger


KEY = "a" * 64
EVENT = {"approval_key": KEY, "kind": "builder.authorized", "details": {"base_sha": "b" * 40}}


def race_claim(path, gate, results):
    gate.wait(10)
    try:
        results.put(SQLiteLedger(Path(path)).claim(KEY, EVENT))
    except Exception as error:
        results.put(type(error).__name__)


def interrupted_claim(path, after_commit, reached, release):
    real_connect = sqlite3.connect

    class InterruptedConnection(sqlite3.Connection):
        def commit(self):
            if after_commit:
                super().commit()
            reached.set()
            release.wait(30)
            if not after_commit:
                super().commit()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=InterruptedConnection)

    with patch("agent_workbench.ledger.sqlite3.connect", connect):
        SQLiteLedger(Path(path)).claim(KEY, EVENT)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "claims.sqlite"
        SQLiteLedger.initialize(self.path)

    def test_explicit_initialization_never_resets_existing_or_missing_store(self):
        with self.assertRaises(LedgerError):
            SQLiteLedger.initialize(self.path)
        missing = self.path.with_name("missing.sqlite")
        with self.assertRaises(LedgerError):
            SQLiteLedger(missing).claim(KEY, EVENT)
        self.assertFalse(missing.exists())

    def test_claim_survives_new_instance_and_retains_exact_receipt(self):
        self.assertTrue(SQLiteLedger(self.path).claim(KEY, EVENT))
        self.assertFalse(SQLiteLedger(self.path).claim(KEY, {**EVENT, "kind": "replacement"}))
        self.assertEqual(SQLiteLedger(self.path).receipt(KEY), EVENT)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM claims").fetchone()[0], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_simultaneous_processes_have_exactly_one_winner(self):
        context = multiprocessing.get_context("spawn")
        gate, results = context.Event(), context.Queue()
        processes = [context.Process(target=race_claim, args=(str(self.path), gate, results))
                     for _ in range(8)]
        try:
            for process in processes:
                process.start()
            gate.set()
            outcomes = [results.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(outcomes.count(True), 1, outcomes)
            self.assertEqual(outcomes.count(False), 7, outcomes)
            self.assertEqual(SQLiteLedger(self.path).receipt(KEY), EVENT)
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join(5)
            results.close()
            results.join_thread()

    def test_killed_process_before_commit_allows_retry_after_commit_preserves_claim(self):
        context = multiprocessing.get_context("spawn")
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                path = self.path.with_name(f"interrupted-{after_commit}.sqlite")
                SQLiteLedger.initialize(path)
                reached, release = context.Event(), context.Event()
                process = context.Process(target=interrupted_claim,
                                          args=(str(path), after_commit, reached, release))
                process.start()
                try:
                    self.assertTrue(reached.wait(15), "child did not reach the commit boundary")
                    process.kill()
                    process.join(10)
                    self.assertNotEqual(process.exitcode, 0)
                    ledger = SQLiteLedger(path)
                    self.assertEqual(ledger.receipt(KEY), EVENT if after_commit else None)
                    self.assertEqual(ledger.claim(KEY, EVENT), not after_commit)
                finally:
                    if process.is_alive():
                        process.kill()
                        process.join(5)

    def test_observed_controls_cannot_disappear_or_change(self):
        ledger = SQLiteLedger(self.path)
        records = {"proposal": {"body": "exact"}, "revocation": {"body": "revoke"}}
        ledger.observe("linear:workspace:issue", records)
        for changed in ({"proposal": records["proposal"]},
                        {**records, "revocation": {"body": "approve"}}):
            with self.assertRaisesRegex(LedgerError, "removed or changed"):
                SQLiteLedger(self.path).observe("linear:workspace:issue", changed)
        ledger.observe("linear:workspace:issue", {**records, "new": {"body": "another"}})
        # Another source cannot erase or collide with this source's history.
        ledger.observe("linear:other:issue", {})
        with self.assertRaises(LedgerError):
            ledger.observe("linear:workspace:issue", {})

    def test_corrupt_foreign_and_unsupported_database_fail_closed(self):
        for mode in ("corrupt", "foreign", "newer"):
            with self.subTest(mode=mode):
                path = self.path.with_name(mode + ".sqlite")
                if mode == "corrupt":
                    path.write_bytes(b"not a database")
                elif mode == "foreign":
                    sqlite3.connect(path).close()
                else:
                    SQLiteLedger.initialize(path)
                    with sqlite3.connect(path) as connection:
                        connection.execute("PRAGMA user_version=99")
                with self.assertRaises(LedgerError):
                    SQLiteLedger(path).claim(KEY, EVENT)

    def test_receipt_tampering_is_detected(self):
        SQLiteLedger(self.path).claim(KEY, EVENT)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE claims SET event_json=?", (json.dumps({"changed": True}),))
        with self.assertRaises(LedgerError):
            SQLiteLedger(self.path).receipt(KEY)

    def test_invalid_event_cannot_reserve_a_key(self):
        for event in ({}, {"approval_key": "b" * 64}, {**EVENT, "non_json": object()}):
            with self.subTest(event=event), self.assertRaises(LedgerError):
                SQLiteLedger(self.path).claim(KEY, event)
        self.assertIsNone(SQLiteLedger(self.path).receipt(KEY))
