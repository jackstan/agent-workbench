"""Bounded recovery for idempotent reads only; callers own mutation receipts."""
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import re
import socket
import ssl
import time
import urllib.error

_SINGLE_ATTEMPT = ContextVar("agent_workbench_single_read_attempt", default=False)
_OUTER_REMAINING = ContextVar("agent_workbench_outer_read_remaining", default=None)


@contextmanager
def read_budget(remaining):
    """Carry the earliest caller deadline through nested authority hydration."""
    outer = _OUTER_REMAINING.get()
    bounded = (lambda: min(outer(), remaining())) if outer is not None else remaining
    token = _OUTER_REMAINING.set(bounded)
    try:
        yield
    finally:
        _OUTER_REMAINING.reset(token)


@contextmanager
def single_attempt(*, remaining=None):
    """Let an enclosing snapshot retry own the budget, avoiding nested retries."""
    token = _SINGLE_ATTEMPT.set(True)
    try:
        if remaining is None:
            yield
        else:
            with read_budget(remaining):
                yield
    finally:
        _SINGLE_ATTEMPT.reset(token)


def retry_delay(error):
    """Retry only temporary read failures; never treat all URLError as transient."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code not in {429, 502, 503, 504}:
            return None
        retry_after = (error.headers or {}).get('Retry-After')
        if retry_after is not None:
            # Honor explicit second counts when the remaining logical budget allows.
            # Date-form or malformed delays fail closed instead of retrying early.
            if not retry_after.isdigit():
                return None
            return max(0.25, int(retry_after))
        return 0.25
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, ssl.SSLError):
        return None
    if isinstance(cause, socket.gaierror):
        return 0.25 if cause.errno == socket.EAI_AGAIN else None
    if isinstance(cause, (TimeoutError, ConnectionError)) or (
            isinstance(cause, OSError) and cause.errno in {
                errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED,
                errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}):
        return 0.25
    return None


def remaining_read_budget(limit):
    outer = _OUTER_REMAINING.get()
    return min(limit, outer()) if outer is not None else limit


def read_with_recovery(operation, *, timeout=30, budget=90, clock=None, sleep=None):
    """At most three attempts and one elapsed budget. Never retry validation."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + budget
    def remaining_budget():
        return remaining_read_budget(deadline - clock())
    for attempt in range(1 if _SINGLE_ATTEMPT.get() else 3):
        remaining = remaining_budget()
        if remaining <= 0:
            raise TimeoutError("Read recovery elapsed budget exceeded")
        try:
            result = operation(min(timeout, remaining))
        except (OSError, urllib.error.URLError) as error:
            delay = retry_delay(error)
            if _SINGLE_ATTEMPT.get() or attempt == 2 or delay is None or delay >= remaining_budget():
                raise
            sleep(delay)
            continue
        if remaining_budget() <= 0:
            raise TimeoutError("Read recovery elapsed budget exceeded")
        return result


def is_graphql_read(query):
    # All owned queries use an explicit query keyword. Unknown/anonymous or
    # mixed GraphQL documents never opt into transport retry.
    return isinstance(query, str) and bool(re.match(r"^\s*query(?:\s|\(|\{)", query)) and not re.search(r"\bmutation\b|\bsubscription\b", query)
