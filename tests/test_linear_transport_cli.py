import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from agent_workbench.cli import main
from agent_workbench.linear import ENDPOINT, QUERY, LinearClient, LinearError, _NoRedirect

from test_linear_approval import (
    APPROVAL, BASE, CREATOR, HUMAN, ISSUE, NOW, ORG, PROJECT, PROPOSAL, TEAM, Source,
)


class Response(io.BytesIO):
    status = 200


class TransportTests(unittest.TestCase):
    def test_fixed_endpoint_and_explicit_authentication_modes(self):
        for oauth, expected in ((False, "test-key"), (True, "Bearer test-key")):
            with self.subTest(oauth=oauth):
                client = LinearClient("test-key", oauth=oauth)
                with patch.object(client._opener, "open", return_value=Response(b'{"data":{"ok":true}}')) as opened:
                    self.assertEqual(client.query(QUERY, {"issue": ISSUE}, timeout=2), {"ok": True})
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, ENDPOINT)
                self.assertEqual(request.get_header("Authorization"), expected)
                self.assertEqual(json.loads(request.data)["variables"], {"issue": ISSUE})
                self.assertEqual(opened.call_args.kwargs["timeout"], 2)

    def test_partial_graphql_errors_invalid_json_and_oversize_responses_fail(self):
        for body in (b'{"data":{},"errors":[{"message":"partial"}]}', b'{"data":null}',
                     b'{"data":{}, "data":{"injected":true}}', b"not JSON",
                     b'{"data":{"bad":NaN}}', b"x" * 1_000_001):
            with self.subTest(body=body[:80]):
                client = LinearClient("test-key")
                with patch.object(client._opener, "open", return_value=Response(body)):
                    with self.assertRaises(LinearError):
                        client.query(QUERY, {}, timeout=2)

    def test_transport_failures_do_not_echo_credentials_or_provider_body(self):
        client = LinearClient("test-key")
        error = HTTPError(ENDPOINT, 401, "test-key sensitive body", {}, None)
        with patch.object(client._opener, "open", side_effect=error):
            with self.assertRaises(LinearError) as raised:
                client.query(QUERY, {}, timeout=2)
        self.assertNotIn("test-key", str(raised.exception))
        self.assertNotIn("sensitive body", str(raised.exception))

    def test_redirects_and_arbitrary_queries_are_rejected(self):
        with self.assertRaises(LinearError):
            _NoRedirect().redirect_request(Request(ENDPOINT), None, 302, "redirect", {}, "https://example.org")
        with self.assertRaises(LinearError):
            LinearClient("test-key").query("mutation { issueDelete }", {}, timeout=2)


class ApprovalCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        fixture = Path(__file__).parents[1] / "examples/demo/project.toml"
        self.project = self.root / "project.toml"
        self.project.write_text(fixture.read_text().replace('"demo-human"', f'"{HUMAN}"'))
        self.settings = self.root / "linear.toml"
        self.settings.write_text(
            f'organization_id = "{ORG}"\nteam_id = "{TEAM}"\nproject_id = "{PROJECT}"\n'
            f'creator_actor_id = "{CREATOR}"\n'
        )
        self.ledger = self.root / "approvals.sqlite"

    def call(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_initialize_then_authorize_through_cli_and_inspect_receipt(self):
        self.assertEqual(self.call(["ledger-init", str(self.ledger)])[0], 0)
        source = Source()
        # Source timestamps remain deterministic in policy tests; the CLI uses today's clock.
        from datetime import datetime, timedelta, timezone
        source.rows[0]["createdAt"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        source.rows[1]["createdAt"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        args = ["authorize-linear", str(self.project), "--linear-config", str(self.settings),
                "--issue", ISSUE, "--proposal-comment", PROPOSAL, "--base-sha", BASE,
                "--ledger", str(self.ledger)]
        with patch.dict(os.environ, {"AGENT_WORKBENCH_LINEAR_API_KEY": "test-key"}, clear=True):
            with patch("agent_workbench.linear.LinearClient", return_value=source):
                status, output, error = self.call(args)
                self.assertEqual(status, 0, error)
                result = json.loads(output)
                self.assertEqual(self.call(args)[0], 2)
        status, output, error = self.call(["ledger-receipt", str(self.ledger), result["approval_key"]])
        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)["details"]["base_sha"], BASE)
        self.assertNotIn("test-key", output)

    def test_preparation_is_offline_and_self_approval_configuration_is_rejected(self):
        proposal = Path(__file__).parents[1] / "tests/fixtures/valid_proposal.json"
        args = ["prepare-linear-proposal", str(self.project), str(proposal),
                "--linear-config", str(self.settings), "--base-sha", BASE]
        with patch("agent_workbench.linear.LinearClient", side_effect=AssertionError("must be offline")):
            status, output, error = self.call(args)
        self.assertEqual(status, 0, error)
        self.assertEqual(len(json.loads(output)["proposal_sha256"]), 64)
        self.settings.write_text(self.settings.read_text().replace(CREATOR, HUMAN))
        self.assertEqual(self.call(args)[0], 2)

    def test_missing_credentials_fail_before_network_or_ledger_creation(self):
        args = ["authorize-linear", str(self.project), "--linear-config", str(self.settings),
                "--issue", ISSUE, "--proposal-comment", PROPOSAL, "--base-sha", BASE,
                "--ledger", str(self.ledger)]
        with patch.dict(os.environ, {}, clear=True):
            with patch("agent_workbench.linear.LinearClient", side_effect=AssertionError("must not connect")):
                self.assertEqual(self.call(args)[0], 2)
        self.assertFalse(self.ledger.exists())
