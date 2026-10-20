# Architecture

## Three layers

1. **Core policies:** deterministic contracts and decisions, independent of network providers.
2. **Adapters:** authenticated issue/identity reads, atomic claims, model execution,
   repository publication, artifact persistence, and release inspection.
3. **Project settings and extensions:** commands, scope, documentation context, release
   targets, and domain-specific Scout collectors.

The current package implements core policies, configuration, Linear approval and
SQLite ledgers, a local Codex repair coordinator, and a GitHub workflow boundary.
The demo still uses explicitly named fixture adapters. A local approval/repair cycle
has been observed; hosted model execution remains a separate validation milestone.

## Modules

| Module | Responsibility |
| --- | --- |
| `contracts` | Evidence-backed proposals, authenticated-adapter protocol, event records, Builder handoff |
| `dispatch` | Bind and consume one approval for one repository/base/scope; prepare worker instructions |
| `policy` | Validate patch data and Git index changes without importing the changed project |
| `review` | Require complete check evidence for the exact head and trusted orchestration revision |
| `repair` | Decide whether to request a repair, wait, or hand work to a human |
| `read_recovery` | Retry transient idempotent reads under a shared attempt/time bound |
| `config` | Parse reviewed project settings and validate argument arrays and scope rules |
| `linear` | Authenticate current proposal/decision reads, bind the full content and reviewed configs |
| `ledger` | Atomically consume approvals and retain observed controls and recovery receipts on one host |
| `coordinator` | Reserve bounded repairs, pass verifier findings to a builder, reverify each new commit |
| `codex_local` | Isolate macOS worker processes and retain check/model evidence |
| `github_cycle` | Authenticate dispatch, reserve work, expose live events and publish the verified tree in a separate job |
| `demo` | Replay fixed fixtures with temporary Git operations and real application tests |

## Authority and evidence

An object named `VerifiedDecision` is an adapter result, not a cryptographic proof.
The core assumes the adapter authenticated it. Constructing such an object from JSON,
reading an actor ID from a model response, or accepting a webhook body as authority is
not a valid production integration.

The adapter must bind the full proposal and creation/decision metadata to current
provider records. It must verify actor identity independently of claimed fields,
retrieve all relevant history, and detect revocation or changed scope. The dispatch
caller supplies its trusted expected repository explicitly.

`ConsumptionLedger.claim` must atomically reserve a key across processes and reruns.
The demo's `MemoryLedger` only illustrates the interface. Production must preserve
uncertain claims and support recovery without resetting approval or repair history.

The implemented `SQLiteLedger` opens an explicitly initialized persistent database.
It serializes claims across local processes, retains complete receipts, and rejects
missing/corrupt/unsupported stores. No automatic reset or resumption is provided.
The Linear adapter uses a comparison and claim in one transaction so another
runner's newly observed control record prevents consumption of a stale snapshot.
See [the operational guide](linear-approval.md) for setup, recovery and limits.

The consumption key identifies the canonical source decision using repository,
source task ID, and source decision ID (`source-approval-v1`). Local creation,
approval-projection, and verification-observation IDs do not create new authority.
Adapters must keep source identities stable and unambiguous; multiple providers
must use provider-qualified identities. A genuinely new approval requires a new
authenticated source decision. Receipts retain source identity and key version.

This is the first supported durable ledger format. No existing live ledgers need
migration; preserve all new receipts rather than resetting history on failure.

Linear proposals are unedited top-level comments by the configured app user.
The human decision names that comment and the digest of the entire envelope,
including the proposal, repository/base/scope and both reviewed configurations.
Provider-qualified issue and comment UUIDs define source identity. Two complete
current reads and durable source observations detect seen edits/deletions and
revocation. Linear does not supply an immutable transaction across these reads;
unseen deleted history and later revocation remain outside the guarantee.

`ProposalEventLog` creates immutable in-memory values and detached inspection copies.
It is not persistent storage or a tamper-evident hosted event service.

## Execution boundaries

The worker edits approved project files with no publication credentials.
A separate trusted publisher rechecks authority and base/head identity and validates
the whole patch and resulting index without executing patched code.
Checks must execute in an isolated environment without privileged tokens or the
ability to alter trusted orchestration, output attestations, or sibling workspaces.

The patch policy always protects workflow files, agent controls, dependency manifests,
configuration, and secret-like paths. Project allowlists cannot override those guards.
Broader maintenance changes require a separate reviewed human workflow.
Reserved basenames, including `AGENTS.md`, `uv.lock`, `poetry.lock`, `Pipfile`, and
`Pipfile.lock`, are protected at every directory depth, case-insensitively. Approved
path patterns retain Python `fnmatch` semantics: `*` may match `/`. Mandatory
per-file protections apply even when an approved pattern is broad.

The current `apply_patch` function stages a validated diff in a clean inert checkout.
It captures at most the patch size limit plus one byte, validates that captured
content, and supplies the same bytes to both Git application commands. Subsequent
changes to the original patch file cannot replace the validated content.

Application and final scope/mode checks operate on a private index held under the
standard `index.lock`. Only a successful final check permits atomic replacement of
the real index. Errors before that replacement preserve the original index; normal
exception cleanup releases only the held lock. An existing lock is never stolen.
The caller must exclusively own the checkout, its Git metadata, and its environment;
the lock excludes cooperating Git writers, not hostile processes with access to
those locations. Hard interruption can leave stale lock files, and interruption
around final replacement requires inspecting state before retrying. This is not a
power-loss durability or automatic recovery guarantee. Worktree files are not edited.

Its authorization input must already be authenticated by its caller. Scope hashes
bind fields against accidental or substituted state; they are not signatures or credentials.

The review gate is independent of the review provider. Required checks must all exist,
have unique expected identities, and match the exact head and trusted base. A model
cannot override a failed check. Missing evidence or malformed review output is incomplete.

The repair module is a pure decision function. `dispatch=True` is eligibility only.
The local coordinator authenticates fresh authority through the adapter, validates
finding scope, durably reserves each attempt before execution, rechecks the local
head and verifies every new commit. A unique approval key admits one run; an
interrupted or uncertain run cannot silently start again. Reservations survive
process exit. Terminal replays do not re-run workers or confer fresh authority.
The publisher remains responsible for remote PR state/head/base checks.
Human merge and production promotion remain separate from a PASS.

The macOS Codex adapter clones inert worker checkouts without a remote, gives the
builder write access to exact approved existing files, and gives the verifier read
access. Shell network access is disabled, sensitive operator directories are denied,
and an explicit minimal environment excludes credentials. The parent Codex process
uses its existing authentication for inference. Isolation probes are evidence of
selected boundaries, not a comprehensive host sandbox audit.

Repairs are captured as cumulative patches against the original approved base.
The existing patch policy applies those bytes in a fresh base checkout; a trusted
commit then uses the rejected candidate as its parent. Authorization is never
rewritten to bless a new base. Tests and protected paths remain outside scope.
Each verifier has a new process and checkout, receives no builder conversation,
and supplies structured findings bound to the exact candidate.

The core journal and coordinator are provider-independent. The current CLI wires
Linear reads and a local Codex process adapter. It does not publish, merge, deploy,
automatically reconcile crashes, or establish distributed exactly-once execution.

### GitHub-hosted boundary

The opt-in workflow starts from an existing candidate PR. Reviewed configuration
and proposal come from the current base; the engine and third-party actions are
pinned. An explicit first-attempt `workflow_dispatch` must have the configured
canonical human as both actor and triggering actor. The workflow revision, PR base,
head, repository, branch, open state and disabled auto-merge are checked through the
GitHub API. Forks and changed approvals fail before workers start.

The approval job atomically creates `refs/tags/agent-workbench/claims/<digest>`
pointing to an annotated tag receipt. The digest binds repository, PR, candidate,
base, proposal and complete configuration. The receipt also records the run and
workflow revision. Conflicts and uncertain API responses stop; the claim is never
overwritten or removed by the adapter. This gives durable reservation across runner
loss, while SQLite journals repair attempts inside a single job. A consumed claim
is not reusable; reruns do not resume interrupted work.

The model job has read-only repository access. The pinned official Codex action
installs the CLI and inference proxy and removes sudo access. Worker tools receive
no GitHub or OpenAI credentials and cannot edit orchestration, evidence or Git
metadata. The parent coordinator uses the read-only API token for authority checks
and the loopback proxy for inference. Each persisted coordinator event is printed
as a JSON line to the live Actions log. The entire repair loop runs in one model
job; stage boundaries are events, not separate Actions jobs.

The publisher downloads only named artifacts from the current run, revalidates the
tag receipt and all approval bindings, and independently evaluates the final check
evidence. It treats the Git bundle as data, validates history and the cumulative
patch, stages that patch in an inert base checkout and compares the resulting tree.
After another authority check, an explicit expected-head lease protects the
fast-forward push against concurrent changes, including remote rewinds. It posts
verdict statuses and requests `ci.yml` explicitly because a GITHUB_TOKEN push does
not normally start another workflow. No candidate code runs in this privileged job.

Limits: repository administrators, the reviewed engine/actions and runner host are
trusted; claim tags are not tamper-proof. There is no atomic transaction between
the final API read and a base update or cancellation, though the branch push has
an exact-head condition. Artifact publication and CI dispatch can fail after a
successful push; preserve the claim and inspect the remote branch instead of
retrying automatically. Artifact retention is finite. A GitHub dispatch is its own
approval route and does not assert that an earlier Linear approval remains valid.
The API adapter currently has fixture coverage; a live hosted cycle is still required.

## Extension contracts

- **Approval adapter:** implement `TrustedDecisionVerifier` and construct a current,
  authenticated `ApprovalSnapshot`.
- **Ledger:** implement atomic `claim(key, event)` with durable recovery receipts.
- **Reviewer:** return the strict result shape used by `review`; provider failures stay incomplete.
- **Scout:** construct a `Proposal` retaining observed evidence, hypotheses, criteria,
  tests, and non-goals. A finding cannot authenticate its creator or approve itself.
- **Release observer:** planned; retain exact deployed identity and semantic outcomes separately.

Prefer a small set of reviewed built-in adapters. Dynamic untrusted plugin loading is
not part of this initial architecture.

## Configuration example

See `examples/demo/project.toml`. Commands are argument arrays, not interpolated shell
strings. Validation does not execute them. The future runner must enforce time/output
budgets and terminate subprocess groups on cancellation; a configured timeout alone
does not implement isolation or an aggregate spending limit.

The public schema is version 1 and the package is pre-release. Schema changes should
be explicit and tested. Projects should consume reviewed versions, rather than
automatically following the newest development branch.
