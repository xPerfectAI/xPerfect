# xPerfect: Quick Power QA Playbook

Use this playbook to rapidly test whether xPerfect is behaving like a real powerful workstation-sandbox runtime instead of a toy demo.

## 1. Core Project Flow

1. Create a project with a real goal and success criteria.
2. Create a worker with a chosen profile.
3. Run a task.
4. Confirm live worker state appears immediately.
5. Confirm the worker can be paused, resumed, interrupted, and terminated.
6. Confirm the project-first watch flow lands on the desktop by default.
7. Confirm the active live terminal is visible inside that desktop by default.
8. Confirm a fresh worker desktop does not default to the Selenium splash screen.
9. Confirm the worker does not report final completion until it has checked the actual result against
   the goal and success criteria, or returned a specific blocker.

Pass if:

- the operator path feels simple
- the worker is resumable
- the run/event history is coherent
- the desktop-first view still shows the real live run terminal
- fresh workers open on an xPerfect-owned surface instead of Selenium branding
- final reports reflect verified completion or a real blocker, not progress chatter

## 2. Sandbox Persistence

1. Create a file inside the worker workspace.
2. Pause the worker.
3. Resume the worker.
4. Confirm the file still exists.
5. Confirm the worker can continue from the same sandbox.

Pass if:

- workspace survives
- home/config survives
- session continuity is believable

## 2.1 Duplicate Workspace Safety

1. Create a file inside a source workspace.
2. Duplicate that workspace through the glossy UI.
3. Confirm the duplicate opens as a new workspace.
4. Confirm the file is present in the duplicate.
5. Confirm browser-session state was not cloned implicitly.

Pass if:

- file/context continuity carries into the duplicate
- the duplicate has its own workspace identity
- browser-session cloning is not silently happening

## 3. Terminal + Desktop Takeover

1. Open the live worker desktop.
2. Open the live terminal takeover.
3. Launch shell, files, browser, Codex, Claude, and OpenClaw surfaces.
4. Pause the worker and take over.
5. Resume it.

Pass if:

- takeover is obvious
- the operator can intervene without guessing
- control returns cleanly to the worker

## 4. Codex Worker

1. Start a `codex-cli` worker.
2. Ask it to create files, inspect code, and continue a second turn.
3. Confirm it reuses the same sandbox and session continuity.

Pass if:

- first turn works
- second turn resumes instead of restarting from scratch
- artifacts are saved in the same workspace

## 5. Claude Worker

1. Start a `claude-code` worker.
2. Verify the sandbox is preconfigured with the expected Claude state.
3. Run a task requiring the local login or configured auth mode.
4. Continue it with a second turn.

Pass if:

- configuration is present
- the worker can use its projected auth/state
- project-scoped settings and `.mcp.json` are honored

## 6. OpenClaw Worker

1. Start an `openclaw-general` worker.
2. Verify it uses the same workstation substrate.
3. Launch the OpenClaw desktop surface.
4. Run a real task and confirm live operator visibility.

Pass if:

- it behaves like a workstation-backed worker, not a separate hidden runtime
- takeover and lifecycle semantics match the other profiles

## 7. Bootstrap Bundle

1. Create a worker with `bootstrap_profile=clean-room`.
2. Pass a `bootstrap_bundle` containing:
   - env
   - project files
   - `claude_project_mcp`
   - `claude_settings_local`
   - `system_instructions`
3. Inspect the sandbox.

Pass if:

- the expected files exist
- env is present in shell/task execution
- instructions and MCP config land in the right scope
- the bootstrap includes a universal completion self-check instead of a task-specific QA script
- delegation stays sparse and faithful: no invented provider lists, urgency rubrics, file outputs,
  MCP/tool success criteria, or fake tool results are introduced by the host
- no raw secrets are echoed in API responses

## 7.1 Runtime Prerequisite Recovery

1. Configure a host profile with a missing or too-old local runtime dependency.
2. Start a public-safe task through the normal API/MCP path.
3. Verify xPerfect classifies the prerequisite problem before creating dead work or telling the user
   the task is running.
4. Verify configured safe recovery is attempted first: managed/bundled dependency, worker-local
   toolchain, alternate available profile, or sandbox/workstation mode when compatible with the user's
   request. These branches must be declared in runtime/profile configuration, and unavailable
   branches are skipped instead of faked with one-off shell commands.
5. If recovery creates a replacement worker/run, verify the returned follow-up context points at the
   active recovered run.

Pass if:

- the user's task is preserved through recovery
- the user is not told to change global machine state while a managed or sandboxed recovery path exists
- if recovery is impossible, the blocker is specific, structured, and does not masquerade as task failure

## 7.2 Native control and restart regression boundaries

The focused deterministic owners below live under `runtime_phase1/tests/`:

| Boundary | Regression owner | Required result |
|---|---|---|
| Exact host generation | `test_host_generation_preservation.py` | Stale controls and old process cleanup preserve a replacement session/process/slot, including the same run ID. |
| Credential and result recovery | `test_credential_redaction.py`, `test_profile_runtime.py` | Both credential halves stay hidden across four output paths; an unconfirmed process stop cannot manufacture completion; missing diagnostics give a concrete nonblocking warning. |
| Managed shutdown | `test_host_run_leases.py` | Unknown or failed stop retains the lease; proven absence permits one retry of the accepted run. |
| Lost conversation authority | `test_stateless_restart_preservation.py` | No bearer persists; exact retry keeps instructions; stateless siblings progress while persistent/control/lease fences hold. |
| Interrupted retry projection | `test_restart_retry_preservation.py` | Durable Pause and a concurrent active generation survive retry reconciliation. |
| Rebound session cleanup | `test_conversation_provider_recovery.py` | Deadline and historical-result recovery use the original run's worker; later work remains unchanged. |

These tests include simulated failures, database races and local child processes. They prove their
listed contracts, not installed native-provider parity, response-time targets or channel delivery.
For a user-visible completion claim, repeat the affected normal client journey: start independent
work, send another request, target a correction or Stop, restart at the relevant boundary, and inspect
visible results and retained files. Record the actual configured route and remaining gaps privately;
publish only sanitized evidence. Reuse valid unchanged evidence and run only the affected checks.

## 8. Connected-Account Broker Readiness

1. Confirm xPerfect does not require direct coupling to LibreChat internals.
2. Confirm a future client could provide provider auth through a brokered bundle.
3. Confirm the runtime can still run without any client-specific broker.

Pass if:

- the runtime remains standalone
- client-specific integration is optional
- the bootstrap contract is sufficient to carry auth/context/memory/callbacks later
