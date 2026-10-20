# Linear approval and local receipts

This integration reads Linear and consumes an approval in a local SQLite ledger.
It does not create Linear issues/comments, run a model, execute project code,
publish a PR, merge, or deploy. The sandbox target is
[`jackstan/agent-workbench-sandbox`](https://github.com/jackstan/agent-workbench-sandbox).

## Trust and setup

Use a dedicated sandbox Linear project and an app user that creates proposals.
The creator must be an active Linear app user, distinct from the active human
approver. Author identity comes from API records, never a name in comment text.
Legacy bot-only authors without a canonical user, delegated/on-behalf-of authors,
external users, replies, archived records, and edited controls are rejected.

The reader needs a separate, read-only Linear credential with access to this
project. It should not be available to a builder. A human's credential capable of
posting comments can impersonate that human; the adapter cannot infer physical
human intent from an API identity. Keep approval credentials outside automation.

Pin the installed workbench revision and review both configuration files outside
the worker's write scope. The project config has the existing schema, with
`approver_actor_id` set to the human's canonical Linear UUID. A separate Linear
config requires exactly these fields (all canonical UUIDs):

```toml
organization_id = "00000000-0000-4000-8000-000000000001"
team_id = "00000000-0000-4000-8000-000000000002"
project_id = "00000000-0000-4000-8000-000000000003"
creator_actor_id = "00000000-0000-4000-8000-000000000004"
```

These IDs are synthetic examples, not working identities. No Sendable identity,
setting, workflow, or credential is required.

## Proposal and decision protocol

1. Independently resolve the test repository's current trusted base commit.
2. Render the complete proposal, bound to that base and both reviewed configs:

   ```sh
   agent-workbench prepare-linear-proposal project.local.toml proposal.json \
     --linear-config linear.local.toml --base-sha BASE_COMMIT
   ```

   Output includes `body` and `proposal_sha256`. The digest is SHA-256 of the
   complete envelope encoded with Python `json.dumps(..., sort_keys=True,
   separators=(",", ":"), allow_nan=False)` using its default ASCII escaping.
   It includes the full proposal, repository, branch, base, scope, and hashes of
   both configuration objects. A digest is a content binding, not authentication.
3. The configured app posts that exact `body` as a new top-level comment on an
   issue in the sandbox Linear project. Retain the issue and comment UUIDs.
4. The human reads the complete proposal and posts a new, direct top-level comment:

   ```text
   agent-workbench approve PROPOSAL_COMMENT_UUID PROPOSAL_SHA256
   ```

   A revocation uses the same fields with `revoke`. A revocation blocks that
   proposal; reconsideration requires a new proposal comment. Multiple approval
   comments are ambiguous and stop the run. Ordinary discussion is not approval.
   Do not edit, delete, or repurpose control comments.

Only configured actors' workbench control comments and the selected proposal
enter the durable observations. Other people's comments cannot approve or revoke.
Malformed controls from configured actors fail closed. Both the proposal creator
and human approver must match their independent API identities.

## Initialize once, then authorize

Keep the ledger on a persistent local disk, outside every worker checkout, with
an operator-owned directory. All local runners for this installation must use
the same file. Temporary files, per-run databases, network filesystems, or copies
per worker do not satisfy the consumption guarantee.

```sh
umask 077
mkdir -p ../workbench-state
agent-workbench ledger-init ../workbench-state/approvals.sqlite
```

Initialization refuses existing files. Authorization opens an existing initialized
database and will not recreate a missing, corrupt, foreign, or unsupported store.
The file is created with mode `0600`; protect its directory, backups, and journal.

Supply exactly one credential through your trusted credential manager/environment:
`AGENT_WORKBENCH_LINEAR_API_KEY` for a personal API key, or
`AGENT_WORKBENCH_LINEAR_ACCESS_TOKEN` for an OAuth access token. Never put it in
the project, a proposal, a command argument, or a receipt. OAuth token refresh is
the caller's responsibility.

```sh
agent-workbench authorize-linear project.local.toml \
  --linear-config linear.local.toml \
  --issue ISSUE_UUID --proposal-comment PROPOSAL_COMMENT_UUID \
  --base-sha BASE_COMMIT --ledger ../workbench-state/approvals.sqlite
```

This command **consumes the approval** and prints authorization JSON. It is not a
dry run. The caller must independently obtain `BASE_COMMIT`; a GitHub reader and
publication-time revalidation remain future work. Repeating the same decision is
rejected, even after a process restart.

## Failure and recovery

The ledger stores the canonical approval key, complete receipt, approved base and
scope, and observed provider controls. Inspect a known key without consuming it:

```sh
agent-workbench ledger-receipt ../workbench-state/approvals.sqlite APPROVAL_KEY
```

If stdout is lost, the operator can inspect the `claims` table with a SQLite tool;
the key and `event_json` remain there. A missing command response does not imply
an unconsumed approval. Preserve the database and any SQLite journal together.

SQLite transactions use `BEGIN IMMEDIATE`, rollback journaling and
`synchronous=EXTRA`. Eight-process contention is tested with one winner. A process
killed before commit leaves no claim; one killed after commit leaves a claim,
even if the caller received no response. The adapter compares the observed
controls and claims the key in the same transaction, preventing another local
runner's already-observed revocation from being overlooked.

There is no reset, rollback-to-old-backup, automatic resumption, or distributed
claim service. A consumed approval with unfinished work needs explicit operator
recovery. Restoring an older database or losing it can destroy replay protection;
never resume automation from an empty replacement. Stored checksums detect
accidental receipt damage; they are not signatures against a malicious disk owner.

## Evidence and limits

Run:

```sh
python -m unittest discover -s tests -p 'test_l*.py' -v
```

These tests exercise real SQLite transactions, independent processes and forced
termination. Linear responses are controlled fixtures, including complete
pagination, later-page revocation, record mutation, actor forgery, delegation,
changed scope/configuration, stale decisions, partial errors and transport failures.
They do not prove a real issue has been approved.

The adapter reads every currently exposed comment page, twice, with archived
records requested. Repeated cursors, duplicate records, incomplete responses and
changed controls stop authorization. Limits are 20 pages per pass, 100 comments
per page, a 1 MB response, 64 KB per comment and 2 MB of retained control evidence.
The read has a 30-second elapsed acceptance budget and per-request socket
timeouts. A caller needing a hard wall-clock kill must supervise the process:
a socket inactivity timeout is not an OS execution deadline.

Linear comments are mutable; pagination is not a transactional historical archive.
Deletion or editing of **previously observed** controls is detected durably.
History removed before the first observation, changes that appear and disappear
between reads, and revocation after the final read cannot be proven absent. This
adapter provides an observed approval snapshot, not a lease for future execution.
Live publication must revalidate current authority before performing its write.
Complete deletion-proof history would require a separate trusted archival source.

The query was validated against the official
[Linear schema at `67791e5`](https://github.com/linear/linear/blob/67791e535db585eedc5c7b7805f43d4e8af5d2b9/packages/sdk/src/schema.graphql).
Authentication and pagination follow Linear's
[GraphQL documentation](https://linear.app/developers/graphql) and
[pagination documentation](https://linear.app/developers/pagination).
A live smoke test with a dedicated project, creator, human approval and read
credential is still required before claiming live integration proof.
