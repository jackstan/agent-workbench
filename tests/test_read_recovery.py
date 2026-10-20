import ssl
import unittest
from urllib.error import HTTPError

from agent_workbench.read_recovery import is_graphql_read, read_with_recovery, single_attempt


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ReadRecoveryTests(unittest.TestCase):
    def test_transient_reads_recover_within_three_attempts(self):
        clock = Clock()
        calls = []

        def operation(timeout):
            calls.append(timeout)
            if len(calls) < 3:
                raise TimeoutError()
            return "observed"

        self.assertEqual(read_with_recovery(operation, clock=clock, sleep=clock.sleep), "observed")
        self.assertEqual(len(calls), 3)

    def test_elapsed_budget_and_retry_after_do_not_expand(self):
        clock = Clock()

        def operation(timeout):
            self.assertLessEqual(timeout, 1)
            raise HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "10"}, None)

        with self.assertRaises(HTTPError):
            read_with_recovery(operation, budget=1, clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.value, 0)

    def test_nested_reads_can_leave_retry_control_to_outer_snapshot(self):
        calls = []

        def operation(timeout):
            calls.append(timeout)
            raise TimeoutError()

        with single_attempt(remaining=lambda: 2), self.assertRaises(TimeoutError):
            read_with_recovery(operation, sleep=lambda _: self.fail("Nested retry"))
        self.assertEqual(calls, [2])

    def test_validation_and_tls_failures_are_not_retried(self):
        for error in (ValueError("invalid"), ssl.SSLError("invalid certificate")):
            calls = []

            def operation(timeout):
                calls.append(timeout)
                raise error

            with self.assertRaises(type(error)):
                read_with_recovery(operation, sleep=lambda _: self.fail("Unexpected retry"))
            self.assertEqual(len(calls), 1)

    def test_graphql_mutations_never_opt_into_retry(self):
        self.assertTrue(is_graphql_read("query Example { viewer { id } }"))
        for query in ("mutation { createThing { id } }", "query { viewer { id } } mutation { x }", "{ viewer { id } }"):
            self.assertFalse(is_graphql_read(query))
