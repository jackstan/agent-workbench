# Security boundaries

Agent Workbench is currently a development foundation with an offline demo. It is not
a hardened execution service for arbitrary repositories or untrusted code.

- No live agent, publisher, or deployment workflow is enabled in this repository.
- CI has read-only repository permissions and no model, issue-tracker, or deployment secrets.
- The demo runs only bundled fixture code in a temporary directory. That directory is
  not a security sandbox for arbitrary user-supplied code.
- Trusted adapter objects must be constructed from authenticated provider state.
  Names, schema validation, and hashes alone do not establish authority.
- The SQLite ledger is for one host and one persistent, operator-owned store shared
  by its runners. Never reset it, copy it per run, place it in a worker checkout,
  or restore an older backup and assume approvals are unused. The demo remains in-memory.
- Linear control comments are mutable. Retained observations detect seen changes,
  but cannot prove history deleted before observation or revocation after a read.
  A future publisher must revalidate current authority immediately before its action.
- Only direct app-created proposals and distinct human-authored approval records are
  accepted. Keep the human's posting credential out of automation; API identity alone
  cannot prove human intent if an agent possesses that credential.
- The patch validator handles data only; privileged publication must never execute
  the changed repository or import its code.
- Runtime automation cannot modify its own controls or grant itself new permissions.

The opt-in consuming-project workflow has separate approval, read-only model, and
privileged publisher jobs. GitHub dispatch is an explicit approval route, with a
durable tag reservation and no automatic retry/reset. Trusted repository administrators
can alter those tags; this is not a tamper-proof ledger. The publisher revalidates
same-run artifacts and exact revisions and never executes the candidate.

Local approval and repair have been observed. Hosted operation still requires its
own live trial; tests with fixture API responses do not establish provider behavior.
Review the execution, provenance and recovery limits in PLAN.md and
docs/architecture.md before enabling a consuming project's workflow.
The approval CLI does not execute or publish changes.

Do not include credentials, private evidence, or exploitable payloads in a public issue.
Use GitHub's private vulnerability reporting feature when it is available on the repository.
