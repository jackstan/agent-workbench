# Architecture

## Three layers

1. **Core policies:** deterministic contracts and decisions, independent of network providers.
2. **Adapters:** authenticated issue/identity reads, atomic claims, model execution,
   repository publication, artifact persistence, and release inspection.
3. **Project settings and extensions:** commands, scope, documentation context, release
   targets, and domain-specific Scout collectors.

The current package implements the first layer and configuration validation. The demo
contains explicitly named fixture adapters. Production adapters remain on the roadmap.

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

`ProposalEventLog` creates immutable in-memory values and detached inspection copies.
It is not persistent storage or a tamper-evident hosted event service.

## Execution boundaries

The future worker will edit approved project files with no publication credentials.
A separate trusted publisher will recheck authority and base/head identity, validate
the whole patch and resulting index, and create the PR without executing patched code.
Checks must execute in an isolated environment without privileged tokens or the
ability to alter trusted orchestration, output attestations, or sibling workspaces.

The patch policy always protects workflow files, agent controls, dependency manifests,
configuration, and secret-like paths. Project allowlists cannot override those guards.
Broader maintenance changes require a separate reviewed human workflow.

The current `apply_patch` function stages a validated diff in a clean inert checkout.
Its authorization input must already be authenticated by its caller. Scope hashes bind
fields against accidental or substituted state; they are not signatures or credentials.

The review gate is independent of the review provider. Required checks must all exist,
have unique expected identities, and match the exact head and trusted base. A model
cannot override a failed check. Missing evidence or malformed review output is incomplete.

The repair module is a pure decision function. `dispatch=True` is eligibility only:
the future coordinator must additionally authenticate provenance, validate finding scope,
atomically reserve an attempt, and recheck PR state/head before applying or publishing.
Human merge and production promotion remain separate from a PASS.

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
