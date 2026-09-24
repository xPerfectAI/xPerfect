# Contributing to xPerfect

Start with a reproducible bug, a useful documentation correction or a small feature. Explain the
user's problem and expected outcome in an issue or pull request. For a vulnerability, use
[private security reporting](SECURITY.md).

## Set up

Use Python 3.11 or later and `uv`. Read the repository's `AGENTS.md`, then follow the
[local deployment guide](docs/deployment.md#local-development). Docker and a working provider
connection are needed when exercising Docker-backed workers. Keep test data synthetic.

From the checkout root, install development dependencies:

```bash
uv sync --project runtime_phase1 --group dev
uv sync --project frontends/glass-drive-ui --group dev
```

## Make a focused change

Preserve existing behavior outside the change. Use typed configuration and capabilities for runtime
decisions; let the model interpret goals. Keep `GLASSHIVE_*`, `WPR_*`, Python package names,
on-disk state and existing MCP tool/skill IDs compatible unless an explicit migration is included.
User-facing product text should say xPerfect; retained names identify compatibility interfaces.

Add or update tests that prove the affected behavior. Run the relevant test file first. The
component suite entrypoints are:

```bash
uv run --project runtime_phase1 --group dev pytest runtime_phase1/tests -q
uv run --project frontends/glass-drive-ui --group dev pytest frontends/glass-drive-ui/tests -q
```

For UI changes, exercise the affected interface, failure/recovery path and reload behavior. For
runtime or packaging changes, check the built/running artifact. State which checks could not run
and why. Documentation-only changes need accurate links and examples, not unrelated provider runs.

## Open a pull request

Describe the problem, resulting behavior and validation. Link the owning requirement or issue when
one exists. Update affected documentation and include only public-safe evidence. Do not commit
credentials, real customer data, private prompts, machine paths or generated runtime state.

xPerfect is distributed under Apache License 2.0. Submit only work you have the right to contribute
and preserve required third-party notices. A fork is an optional contribution path; using the
product, reporting a clear issue and improving the docs also help.
