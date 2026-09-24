# xPerfect: Repository Structure and Publication Boundaries

## Purpose

Keep xPerfect publish-ready while preserving a strict line between:

- the standalone product repo
- private user-specific state
- external client integrations

## Repository Structure

The repository is organized as follows:

- `README.md`
  - project entry point
- `docs/`
  - current source-of-truth product docs
- `contracts/`
  - API and example payloads
- `runtime_phase1/`
  - current standalone runtime implementation
- `frontends/`
  - separate frontend/operator surfaces

## Boundary Rules

What belongs in the xPerfect repo:

- product code
- publish-oriented docs
- tests
- API contracts
- example payloads
- neutral defaults that are safe to share

What does not belong in the xPerfect repo:

- live secrets
- connected-account tokens
- personal prompts
- owner-private notes
- machine-specific runtime captures
- private bootstrap bundles containing real credentials

## Private Companion Location

Owner-specific or confidential xPerfect state belongs outside the public repository.

Recommended categories there:

- private bootstrap bundles
- provider presets
- personal prompts
- private QA captures
- machine-specific notes

## Integration Boundary

xPerfect can serve external clients while remaining independently runnable and publishable.

That means:

- Clients may use xPerfect's MCP and HTTP APIs.
- Client-specific configuration stays with each client, outside xPerfect core.
