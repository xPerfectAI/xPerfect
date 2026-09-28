# xPerfect Component

xPerfect is a standalone git and instruction root. Keep its runtime, docs, tests, and public
examples usable without another application. When this checkout is nested in a host project, as
`viventium_v0_4/xPerfect` is in Viventium, and `../../AGENTS.md` exists, read it before work that
affects the host; it owns the host's scope, safety, delivery, and verification rules.

## Product Standard

- Own the whole user result, not one file. Trace the real trigger, runtime, and output first, and
  judge a change by what a new user gets: output quality, wait, and actions needed, not code or
  test volume.
- A new user should reach the main value on first use, with no learning curve, in the fewest
  actions, with their exact choices and safety kept. Say what cannot work before asking for input.
- Reuse existing project mechanisms and current established practice before adding one. Never
  hardcode or overfit to one example.
- QA scope: follow the latest explicit or approved mode (`skip`, `critical-path`, `blast-radius`,
  or `full`) and pass it unchanged to every delegated agent. Skipped is not passed.

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

- Attribute project work to `AdrienBeyk` using the project’s public GitHub noreply identity.
  Do not add AI-tool authors, co-author trailers or generated-by credits to commits or PRs.
  Keep third-party license and copyright notices intact.

- Keep prompts and evidence public-safe. Never place credentials, private user data, raw exports,
  private paths, or owner-machine state in tracked fixtures, logs, docs, or reviewer handoffs.
- Preserve unrelated changes. Do not commit or push unless the user requested those actions.
- A source change is not shipped until the built and running artifacts agree with the source.
- Run the smallest relevant component tests and exercise affected user-visible paths. Worker output
  or unit tests alone do not replace the user surface.
