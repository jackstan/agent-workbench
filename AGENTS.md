# Working on Agent Workbench

This is an independent toolkit derived from Sendable. Never modify Sendable, import
its live configuration, enable its workflows, or copy its credentials as part of work here.

Keep the initial stack Python, GitHub, Linear, and Codex. The core must remain importable
without network access or provider credentials. Provider integrations belong behind
explicit interfaces; project-specific values belong in reviewed configuration.

Current delivery scope and status live in PLAN.md. README.md is the runnable entrypoint.
Architecture and provenance are durable references in docs/. Update existing documents
instead of creating a status note for each task.

Validate changes with:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
agent-workbench check-config examples/demo/project.toml
agent-workbench demo
```

Changes to packaging also require building a wheel and running the installed CLI outside
the source checkout. Preserve protected-path, exact-commit, approval-replay, fail-closed,
and bounded-repair tests. Add tests for meaningful failure modes, not implementation details.

Do not treat fixtures as live proof. Human merge and release promotion remain explicit
gates for live integrations. Never give a model publication credentials or let generated
content change its own permissions. Ordinary approved maintenance of this repository is
distinct from the runtime patch policy used by automatic builders.
