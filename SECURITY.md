# Security boundaries

Agent Workbench is currently a development foundation with an offline demo. It is not
a hardened execution service for arbitrary repositories or untrusted code.

- No live agent, publisher, or deployment workflow is enabled in this repository.
- CI has read-only repository permissions and no model, issue-tracker, or deployment secrets.
- The demo runs only bundled fixture code in a temporary directory. That directory is
  not a security sandbox for arbitrary user-supplied code.
- Trusted adapter objects must be constructed from authenticated provider state.
  Names, schema validation, and hashes alone do not establish authority.
- A production ledger must be durable and atomic. The in-memory demo ledger is not.
- The patch validator handles data only; future privileged publication must never execute
  the changed repository or import its code.
- Runtime automation cannot modify its own controls or grant itself new permissions.

Before live use, complete the authenticated adapters, worker isolation, artifact provenance,
publication recovery, and public-fork tests described in PLAN.md and docs/architecture.md.

Do not include credentials, private evidence, or exploitable payloads in a public issue.
Use GitHub's private vulnerability reporting feature when it is available on the repository.
