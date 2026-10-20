# Source provenance

Agent Workbench began as a curated extraction from the owner's Sendable development
system at commit `10923b1c7b835e2e4285613a14416ea0552ef937`.

Sendable is a separate repository and continues to operate independently. Its Git
history, live workflows, credentials, account IDs, deployment configuration, private
review documents, and captured research/data artifacts are not included here.

| Workbench component | Original component | Treatment |
| --- | --- | --- |
| `contracts.py` | `agents/scout_proposals/contract.py` | Generalized identity terminology and removed tracker-specific status projection |
| `dispatch.py` | `agents/builder_dispatch/control.py` | Generalized task/approver identity, explicit expected-repository binding, shared path validation |
| `policy.py` | `agents/builder_dispatch/apply.py` | Generalized protected paths and removed environment-driven execution entrypoint |
| `read_recovery.py` | `agents/read_recovery.py` | Preserved bounded-read behavior with neutral internal names |
| `repair.py` | `agents/builder_loop/run.py` decision logic | Extracted pure bounded decision policy; remote coordination is not migrated |
| `review.py` | `agents/verifier/run.py` gating principles | Smaller provider-independent gate; not the full original verifier |
| `test_contracts.py` | `test_scout_proposals.py` | Retained adversarial cases with neutral synthetic identities and new fixture content |

Configuration, packaging, demo, CLI, and the remaining tests are new standalone work.
The proposal fixture is a fictional slug-normalization example, not a retained product
observation. Source-system deployments and historical test counts are not claims about
this new repository. Its own CI and evidence define its status.
