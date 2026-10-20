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

## 2. Authenticated integrations — next

- Implement a Linear adapter resolving proposal creation, current decision, revocation,
  and canonical actor identity from complete authenticated records.
- Implement a durable atomic consumption ledger; uncertain writes preserve receipts.
- Add GitHub read adapters and explicit workflow provenance validation.
- Revalidate the complete approved proposal, repository, scope, and base before publication.
- Use separate test-project identities and credentials; do not import Sendable's settings.

Done when stale, forged, revoked, duplicated, and changed approvals cannot create work,
and an intentionally approved test proposal can produce a bound build authorization.

## 3. Isolated builder and PR workflow

- Port the existing Codex worker and trusted publisher boundaries into reviewed,
  versioned workflows using the reusable core.
- Keep model execution separate from repository/issue write credentials.
- Add bounded test commands and independent model review with strict result schemas.
- Preserve exact head/base bindings, repair reservations, limits, and recovery history.
- Keep human merge mandatory and post-merge obligations visible.

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
