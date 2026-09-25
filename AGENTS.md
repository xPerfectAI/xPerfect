# xPerfect Component

xPerfect is a standalone git and instruction root. Keep its runtime, docs, tests, and public
examples usable without another application. When this checkout is nested in a host project, as
`viventium_v0_4/xPerfect` is in Viventium, and `../../AGENTS.md` exists, read it before work that
affects the host; it owns the host's scope, safety, delivery, and verification rules.

## Worker Runtime Boundary

- Workers are general intelligent
  workers: give them the real goal, constraints, files, capabilities, and tool results, then let
  them choose the path.
- Do not predict or hardcode the provider, account, tool, artifact, or workflow unless the user
  explicitly selected it or verified structured evidence requires it.
- Harness/runtime owns reliable data in/out, prerequisite recovery, authorization boundaries,
  cancellation, persistence, and observable completion. Model judgment owns planning and tool choice.
- Solve reliability with typed contracts, capability metadata, receipts, logs, and tests. Never route
  from prompt text, human-facing names, tool substrings, provider labels, or one user's wording.

## Component Safety

- Keep prompts and evidence public-safe. Never place credentials, private user data, raw exports,
  private paths, or owner-machine state in tracked fixtures, logs, docs, or reviewer handoffs.
- Preserve unrelated changes. Do not commit or push unless the user requested those actions.
- A source change is not shipped until the built and running artifacts agree with the source.
- Run the smallest relevant component tests and exercise affected user-visible paths. Worker output
  or unit tests alone do not replace the user surface.
