# xPerfect: Repository Structure and Publication Boundaries

## Purpose

Keep xPerfect publish-ready while preserving a strict line between:

- the standalone product repo
- private user-specific state
- external client integrations

For documentation ownership, names and maintenance, follow the
[core documentation rules](00_Key_Principles.md#8-preserve-learning-without-drift).

## Repository Structure

The repository is organized as follows:

- `README.md`
  - project entry point; `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and
    `SECURITY.md` beside it
- `xperfect`
  - the launcher (`./xperfect start`, `doctor`, `stop`, `restart`); `scripts/host.py` runs a
    local install, `scripts/render-capability-comparison.py` renders the comparison
- `docs/`
  - current source-of-truth product docs; `docs/user-guide/` is the visual guide's source and
    `docs/assets/` its images
- `contracts/`
  - API and example payloads
- `runtime_phase1/`
  - current standalone runtime implementation
- `frontends/`
  - separate frontend/operator surfaces
- `integrations/teams/`
  - the optional Microsoft Teams channel package for Experts; it talks to xPerfect only through
    the channel routes
- `integrations/evidence-kit/`
  - optional, standard-library examples for immutable intake, evaluation, evidence
    export and verified delivery; not imported by the runtime
- `integrations/livekit/`
  - a reference voice integration
- `deployment/linux/`
  - the packaged Linux service, its images, upgrade and restore
- `skills/` and `plugins/`
  - the `connect-glasshive` Skill for native clients, and its Codex and Claude Code plugin form
    (GlassHive is xPerfect's former name)
- `tests/`
  - package, upgrade and restore tests; component tests live in `runtime_phase1/tests`,
    `frontends/glass-drive-ui/tests` and each `integrations/*/tests` (commands in `CONTRIBUTING.md`)
- `.github/`
  - workflows, code owners, issue forms and the pull request template

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
