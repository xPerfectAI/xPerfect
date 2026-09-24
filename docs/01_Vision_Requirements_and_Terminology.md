# xPerfect: Vision, Requirements, and Terminology

## Vision

xPerfect is a standalone runtime for powerful AI workers that can be resumed, watched, interrupted, and steered in real time.

It is meant to feel like this:

- define a `Project`
- choose or create a `Worker`
- run it inside a persistent `Sandbox` or, when explicitly selected, on the `Host Computer`
- watch what it is doing live
- interrupt or take over when needed
- resume later without losing the work surface

## Product Requirements

xPerfect should provide:

1. a project-first operator flow
2. persistent sandbox or host workspace state
3. fast warm resume
4. controllable workers
5. explicit human takeover
6. compatibility with multiple worker runtimes
7. client independence
8. strong local-first posture
9. safe publication through MCP and direct HTTP APIs
10. a clean path for auth, MCP, memory, context, and callback injection
11. a universal worker completion self-check before any user-facing final report
12. harness-level prerequisite preflight and safe recovery before asking the user to fix local runtime substrate
13. sparse, faithful delegation: pass goals, constraints, files, MCP/tool capability context, and
    explicit success conditions without inventing plans, rubrics, artifacts, provider lists, or tool
    results for the worker

## First-use simplicity

The default path must remove unnecessary effort and learning. A new user should be able to give a
goal, add files when useful, start, and receive a result without learning runtime terminology or
configuring technical details. Use the user's configured defaults; ask only for missing information,
credentials, or consent that is necessary for the actual task.

Show the fewest necessary actions and words. Reveal advanced configuration on request and contextual
actions when they become useful. Keep full capabilities available without turning the normal path
into a configuration form. Do not repeat completed progress, empty sections, or internal mechanics.
Do not simplify by hiding a failure, inventing context, changing a selected model, or weakening access.

Acceptance requires real browser/computer use from a new user's perspective: start without prior
product knowledge, follow the default path, inspect the result, and recover from an ordinary error.
Record visible choices, required inputs, clicks, confusing wording, and before/after screenshots.
Check supported small screens, themes, keyboard/focus, and accessibility. Source, tests, and reviewer
opinions support this evidence; none replaces using and visually evaluating the delivered product.
The [operator UI contract](07_Minimal_Unified_Operator_UI.md) owns the concrete interaction cases.

## Completion evidence boundary

The runtime records process, run, transcript and artifact identity. A structured provider or
process failure, an empty result, an unmet explicitly typed output format, invalid document bytes,
or corrupt/foreign evidence remains a failed result. These checks do not replace authority,
workspace, capability or owner controls.

Prose-derived constraint, coverage and completion diagnostics are advisory. Their missing report
marker, inferred date/format/count or unavailable internal ledger must not discard useful native
output. The runtime authors these diagnostics; the model does not have to create an internal ledger
or a planning artifact. The result retains a visible warning and private diagnostic detail. Recovery
uses the same boundary and never invents missing historical evidence. Declared or present evidence
still has to be readable and belong to the exact run. An unavailable internal diagnostic names
that missing diagnostic in the warning; it must not emit an unexplained empty warning.

## Core Worker Operating Instructions

This document owns the standalone worker contract. Integrations may add their own narrower
authorization constraints without becoming a dependency of xPerfect:

```
CRITICAL OPERATING INSTRUCTIONS (FOLLOW STRICTLY):

1. PATH OF LEAST RESISTANCE: Use the simplest, most direct solution. Don't reinvent wheels.

2. JUST DO IT: Execute immediately without asking questions. Users want RESULTS. Rely on your intelligence, tools, MCPs, skills to find ways around blockers to get it done full and complete.

3. SELF-TEST AND VERIFY:
   - After creating code, RUN IT
   - After starting a server, CURL IT to confirm it responds
   - After researching or creating files, open them and deliver them
   - NEVER report success without verification
   - Debate with yourself on gaps, issues, mistakes, misalignments in your delivery and work on them. Do not stop early. Do not just tell the user what you missed. Actually take action and address them so that the delivery to the user is complete and reliable.

4. LOOP UNTIL SUCCESS:
   - If something fails, FIX IT and try again
   - Keep iterating until ACTUALLY COMPLETE

5. NO USER INTERVENTION: Deliver a COMPLETE, WORKING solution.
```

This is bounded by the runtime safety model: it does not override tenant/user scope, auth boundaries,
destructive host-action checkpoints, or cost/time controls. Workers should fix what they can and
report a concrete blocker instead of looping indefinitely.

## Non-Goals

xPerfect is not:

- a LibreChat-only feature
- a Skyvern replacement
- a provider-specific wrapper tied only to Anthropic or OpenAI
- a promise of hostile multi-tenant security from phase-1 Docker alone

## Terminology

### Sandbox
A persistent workstation-like execution environment that holds the worker's home directory, workspace, processes, and live control surfaces.

### Host Computer
A no-sandbox execution mode where the worker uses local CLIs and OS/browser/filesystem access on
the user's main machine. This mode is intentionally powerful and must be selected explicitly.

### Worker
The active AI runtime inside a sandbox or host workspace.
Examples:

- `codex-cli`
- `claude-code`
- `openclaw-general`

The worker profile plus execution mode is the runtime selector. The legacy API field
`backend=openclaw` may still appear for compatibility, but it must not be presented as the product
backend or used to override `codex-cli`, `claude-code`, or `openclaw-general` profile selection.

There are three separate OpenClaw concepts that must not be collapsed:

- `integrations.openclaw` is a lab/integration toggle.
- `@openclaw` is a mention/activation alias.
- `openclaw-general` is a worker profile, selected the same way as `codex-cli` or `claude-code`.

### Project
The durable task definition around one or more workers, including goal, success criteria, continuity, and audit trail.

### Bootstrap Bundle
A portable preset describing what should be projected into a worker when it starts or resumes.
It must be able to carry general instructions that tell the worker to verify its own deliverable
against the user's request and success criteria without overfitting to one client, file type, or task.
It should preserve real data in and data out: uploaded bytes/references, broker grants,
capabilities, and retrieved context are projected when present; missing data is represented as a
blocker or unavailable capability, not filled in by the host assistant.

## Client Compatibility Goal

xPerfect should work with:

- Viventium / LibreChat
- Claude / Claude Code
- Codex
- ChatGPT-compatible MCP clients
- direct operator API usage

The runtime must remain usable even when none of those clients are present.

## Standalone conversation coordination

A coordinator uses the existing native conversation role. It can answer directly or choose bounded
worker delegation. Native model selection, account leases, runtime capacity and exact controls keep
the same owners as ordinary work. LibreChat, Viventium and external callback services are optional.

The runtime stores the raw user message before inference. A complete structured goal batch is
accepted atomically before worker admission; capacity or a missing prerequisite cannot silently
remove later siblings. Goals link to authoritative conversation or worker runs. The model owns
interpretation, decomposition and completion judgment. A list containing ten steps is not converted
into ten workers by a text parser. Explicit ten-objective interpretation needs exact-model evidence
as well as deterministic batch-retention tests.
When the shared workspace probe, host capacity, or a selected account is briefly unavailable at
first use, the saved foreground turn retries automatically with its original identity and a
bounded delay. A missing or invalid configuration stays blocked with a clear recovery action.

New users start from a single conversation composer using their configured assistant. Route,
context, tool and admission settings remain advanced controls. UI status must distinguish saved
intent, waiting work, execution, failure and completion without exposing internal protocol details.
Grok Build needs one exact native model: a deployment choice wins, otherwise the owner chooses an
offered installed model in Connections. Missing or unavailable choices fail with a typed setup
message. The owner choice persists and cannot silently change an existing run's model. A helper
result notification carries exact saved output when it fits; larger output names its remaining
offset and hash so the coordinator can read the rest from the authorized result tool.

### Allowed AI

Each owned project permits all authorized AI options by default. A project can select allowed
harnesses, exact models and connections; an execution workspace inherits or narrows that ceiling.
These choices govern new native starts, including queued work, retries and delegated helpers.
An existing running invocation keeps its exact binding. A standalone conversation creates one owned
project when none is chosen. A coordinator conversation carries its project or workspace origin into
every child project, worker and run. A child project's
default cannot widen its origin. If a selected origin disappears, new starts stop; the owner can
still read saved status and results and stop work. Connection readiness and execution placement
remain separate from permission, and the native binder confirms the actual account at start.
