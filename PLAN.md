# Plan

Build an opinionated, reusable agent delivery toolkit for the owner's side projects.
Keep Sendable's existing agent code, workflows, configuration, and deployments independent.

## 1. Standalone foundation — implemented

- Installable Python package, CLI, and versioned project configuration.
- Extracted and generalized proposal, approval, patch, and read-recovery controls.
- Review and bounded-repair policy separated from provider transport.
- Credential-free fixture replay using a real temporary repository and application tests.
- Adversarial tests, package-install checks, and read-only CI.
- Reproduced-boundary fixes: stable source-approval consumption, nested protected
  basenames, and immutable patch input with isolated index validation. Regression
  tests cover interrupted application, failed final checks, and lock ownership.

This milestone is not a live autonomous delivery demonstration.

## 2. Authenticated integrations — implemented in part; live approval observed

- Implemented a read-only Linear API adapter for content-bound proposals, direct
  app/human identities, complete current pagination, and observed revocation.
- Implemented a local SQLite ledger with atomic claims, retained source records,
  comparison at claim time, and receipts surviving a lost response after commit.
- Tested fixture-based approval failure cases, eight-process contention, and
  process termination before/after commit. Query validated against the official schema.
- Created the independent duration-parser sandbox with fixed acceptance tests.
- Observed a genuine approval on the independent sandbox, with dedicated Linear
  app/read identities, a persistent receipt, and rejected duplicate consumption.
- Added explicit saved-authorization revalidation without consuming it again.
- Add GitHub read adapters and explicit workflow provenance validation.
- Revalidate the complete approved proposal, repository, scope, and base before publication.
- Use separate test-project identities and credentials; do not import Sendable's settings.

Done when stale, forged, revoked, duplicated, and changed approvals cannot create work,
and an intentionally approved test proposal can produce a bound build authorization.
The current reader cannot prove deleted history it never observed, and cannot make
a future publication safe without a fresh authority check.

## 3. Isolated builder and PR workflow

- Port the existing Codex worker and trusted publisher boundaries into reviewed,
  versioned workflows using the reusable core.
- Keep model execution separate from repository/issue write credentials.
- Add bounded test commands and independent model review with strict result schemas.
- Preserve exact head/base bindings, repair reservations, limits, and recovery history.
- Keep human merge mandatory and post-merge obligations visible.
- Observed separate local builder/reviewer processes and a first-pass sandbox PR.
- Implemented a local coordinator that dispatches scoped repairs from verifier
  findings, records reservations before dispatch, and verifies the resulting commit.
- Added a macOS Codex adapter with isolated worker shells and evidence capture.
- Regression coverage includes stale evidence, changed heads, revoked authority,
  concurrent admission, uncertain builder outcomes, scope and repair limits.
- Observed a real local verifier → builder repair → fresh verifier cycle on a
  deliberately seeded sandbox defect that passed the original acceptance suite.
  The journal records one repair, both verdicts and exact commits. See the
  [public evidence](docs/evidence/sandbox-repair-cycle.json) and README reproducer.
- Hosted orchestration, automatic publication and unattended recovery remain open.

Done when a real approved improvement creates a PR, passes independent verification,
and handles a failed or interrupted run without duplicate publication.

## 4. Release validation and optional Scout

- Add a configurable release observer and project-owned semantic checks.
- Connect one Quality or UX Scout to the same proposal path.
- Preserve source evidence, duplicate dispositions, and no-finding outcomes.
- Run one genuine finding through approval, delivery, human merge, and release validation.

Done when the full chain is observed and its extra human interventions are recorded.
Synthetic fixture results cannot satisfy this milestone.

## 5. Reuse and portfolio release

- Install in a second unrelated side project without changing the core engine.
- Measure completed tasks, review usefulness, failures, human intervention, time, and usage.
- Record a short demo and case study with limitations and concrete evidence.
- Review public-facing fixtures, dependencies, security boundaries, and license selection.

Sendable adoption, if desired, is a later explicit decision. Initially, fixes are ported
deliberately between the independent repositories rather than synchronized automatically.
