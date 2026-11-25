# Contributing to xPerfect

Start with a reproducible bug, a useful documentation correction or a small feature. Explain the
user's problem and expected outcome in an issue or pull request. For a vulnerability, use
[private security reporting](SECURITY.md).

## Set up

Read [the core principles](docs/00_Key_Principles.md) and `AGENTS.md`. Use Python 3.11 or later
and `uv`, then follow the
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

Follow the [batch delivery and QA rule](docs/00_Key_Principles.md#7-think-and-plan-then-complete-the-work).
Add or update tests for changed behavior; select cases by the agreed scope. These are the suite
entrypoints, one per test folder, run from the checkout root; they are not mandatory per-edit gates:

```bash
uv run --project runtime_phase1 --group dev pytest runtime_phase1/tests -q    # runtime
uv run --project frontends/glass-drive-ui --group dev pytest frontends/glass-drive-ui/tests -q    # page
uv run --project runtime_phase1 --group dev pytest tests -q    # package, upgrade and restore
(cd integrations/teams && uv run --extra test pytest -q)    # Teams channel package
PYTHONPATH=integrations/livekit python -m pytest integrations/livekit/tests -q    # LiveKit reference
(cd integrations/evidence-kit && python -m pytest tests -q)    # evidence kit
```

The last three follow each integration's own README, which lists what it needs installed.

For UI changes, exercise the affected interface, failure/recovery path and reload behavior. For
runtime or packaging changes, check the built/running artifact. State which checks could not run
and why. Documentation-only changes need accurate links and examples, not unrelated provider runs.

## Open a pull request

Describe the problem, resulting behavior and validation. Link the owning requirement or issue when
one exists. Update affected documentation, and add a dated entry to `CHANGELOG.md` for a change a
user can see. Include only public-safe evidence. Do not commit credentials, real customer data,
private prompts, machine paths or generated runtime state.

Commit under your own identity. Do not credit an AI tool as author or co-author, and add no
generated-by line, in commits or in the pull request. The
[pull request template](.github/pull_request_template.md) lists these checks.

xPerfect is distributed under Apache License 2.0. Submit only work you have the right to contribute
and preserve required third-party notices. A fork is an optional contribution path; using the
product, reporting a clear issue and improving the docs also help.
