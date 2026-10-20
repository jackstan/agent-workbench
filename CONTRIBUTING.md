# Contributing

This project is being developed first for the owner's side projects. Start with the
README and PLAN.md to understand which capabilities are implemented.

Use Python 3.11 or later, install with `python -m pip install -e .`, and run
`python -m unittest discover -s tests -v` plus `agent-workbench demo`.

Keep changes focused. Explain the concrete behavior, tests, and remaining limitations.
Use synthetic identities and observations in fixtures. Never submit account credentials,
private tickets, customer data, or live production configuration.

Separate deterministic policy from external integrations. Tests should exercise failed
and interrupted work as well as successful work. A mocked provider result is not proof
of hosted operation. New integrations need documented permission and recovery boundaries.

The project has not selected an open-source license yet.
