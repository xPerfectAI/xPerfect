# xPerfect Component

xPerfect is a standalone git and instruction root. Keep its runtime, docs, tests, and public
examples usable without another application. When this checkout is nested in a host project, as
`viventium_v0_4/xPerfect` is in Viventium, and `../../AGENTS.md` exists, read it before work that
affects the host; it owns the host's scope, safety, delivery, and verification rules.

## Core Principles

Read and follow [xPerfect AI Key Principles](docs/00_Key_Principles.md) before product work.
That document is the canonical owner of model judgment, parity, single sources of truth,
separation of concerns, reuse and research, simple UX, batch delivery, QA and learning discipline.
Feature contracts and narrower user instructions still govern the specific task.

## Component Safety

- Attribute project work to `AdrienBeyk` using the project’s public GitHub noreply identity.
  Do not add AI-tool authors, co-author trailers or generated-by credits to commits or PRs.
  Keep third-party license and copyright notices intact.

- Keep prompts and evidence public-safe. Never place credentials, private user data, raw exports,
  private paths, or owner-machine state in tracked fixtures, logs, docs, or reviewer handoffs.
- Preserve unrelated changes. Do not commit or push unless the user requested those actions.
- A source change is not shipped until the built and running artifacts agree with the source.
- Run the smallest relevant component tests and exercise affected user-visible paths. Worker output
  or unit tests alone do not replace the user surface.
