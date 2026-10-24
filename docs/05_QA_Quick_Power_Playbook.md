# xPerfect: Quick Power QA Playbook

Use this playbook to rapidly test whether xPerfect is behaving like a real powerful workstation-sandbox runtime instead of a toy demo.

Follow the [batch delivery and QA rule](00_Key_Principles.md#7-think-and-plan-then-complete-the-work).
Select affected cases under the agreed QA scope; this catalog is not an automatic full-suite gate.

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

### Delivery lessons

- Keep one native Queen/worker management owner. Hosts carry context, controls and results; they
  must not create another planner, goal ledger or scheduler for the same work.
- A queued input receipt proves admission, not model consumption. Prove the exact work, attempt,
  native generation and public result before claiming that a correction was used.
- Resume, Retry and Message must retain the accepted goal and retire only a proved ended
  generation. A busy lifecycle lock keeps accepted queued work for the existing next wake.
- Use the initialized native MCP registry to prove required tools are ready. A delayed cosmetic
  command list must not add a separate startup wait. Keep session and tool identity checks.
- Preserve typed native failures through the provider protocol so existing clients can recover.
  Do not replace actionable errors with a generic status or speak internal maintenance notices.
- Separate implementation, deterministic proof and channel acceptance. Short local examples do
  not prove always-on endurance, natural audio quality, every provider path or latency targets.

## 8. Connected-Account Broker Readiness

1. Confirm xPerfect does not require direct coupling to LibreChat internals.
2. Confirm a future client could provide provider auth through a brokered bundle.
3. Confirm the runtime can still run without any client-specific broker.

Pass if:

- the runtime remains standalone
- client-specific integration is optional
- the bootstrap contract is sufficient to carry auth/context/memory/callbacks later

## 9. Shared UI and Library acceptance

For appearance or Library changes, select the affected rows under the agreed QA scope. Use the
normal running UI and an isolated browser origin for temporary appearance preferences. Keep
screenshots, raw browser receipts and machine paths private.

| Path | User check | Expected result |
|---|---|---|
| Delivered assets | Reload an existing browser tab normally; inspect entry and shared-module URLs. | Current source loads without a hard refresh. Conditional requests retain validators and revalidation; shared modules have one URL per generation. |
| Common shell | Visit each affected navigation destination in both themes and at wide and narrow widths. | Header anchors, title roles, controls and account menu agree. No horizontal page overflow; visible semantic states retain contrast. |
| Chat | Keep an unsent draft, choose one saved Library version, then Use selected. | Draft and selected account stay intact, the exact version appears, and focus returns to the picker trigger. Cancel and Escape apply nothing. |
| Chat draft sizing | Type a long draft, reduce the available viewport, then clear or edit a saved message. | Only the message field grows to a bounded height, then scrolls. Focused typing keeps actions in view; reading elsewhere does not move the page. Draft text, retry keys and input composition stay intact. A desktop viewport simulation is not a real phone keyboard test. |
| Run actions | Inspect actual settled lifecycle controls in Workspaces and Watch in both themes. | Continue, Resume and Pause use neutral secondary styles; status colours remain on status. Disabled and keyboard-focus states stay visible. Do not start work merely to inspect the button. |
| Custom worker entry | Choose an expert, then Create my own; repeat with Existing selected. | The automatic expert name clears, the visible worker-setup heading receives focus, and the current New/Existing choice stays intact. No worker is created by selection alone. |
| Library picker | Compare a loaded list, an absent search and a cleared search, including a short phone viewport. | Search, filters and footer keep their natural heights. Item text remains readable; the list scrolls. Cancel and Use selected share one footer. |
| Library editors | Open Add → Write a note and an existing item's Edit action, then cancel each. | Shared field/action styles remain readable; keyboard focus returns to the visible Add or item-options trigger. No save occurs on Cancel. |
| Watch Library | Open Files and My Library on an existing workspace; resize wide and narrow. | Existing files and authority remain intact. Selectable cards retain room for their text; the phone drawer uses the available viewport. Close returns focus to Library. |
| Appearance | Change Light/Dark, reload and inspect another open app tab; restore Auto. | The preference persists and synchronizes across tabs. Auto follows the device; native worker content retains its own appearance. |
| Native selects and disclosures | Open each field family; use keyboard selection, disabled state, long values and narrow widths. | One inset arrow, selected text fits, complete visible focus, and a clear open/closed content cue. Browser semantics remain native. |
| Action menus | Open last-row Library/Files and workspace menus; use arrows, Home/End, Escape, outside click and a dialog nested menu. | Every action stays in the viewport without moving the card. Escape restores its trigger and leaves the parent dialog open. |
| Short Library picker | Reduce available height, search one/zero/many items, scroll, and select/cancel. | Content remains reachable through one body scroll region; the action footer remains visible. No stretched card or clipped ring. |
| Schedule fields | Open weekly and advanced fields; save a paused synthetic schedule and reload; inspect connection modal at short height. | Checkbox remains inline, weekday/time values fit, unsupported DST choices stay hidden, and header/actions remain visible around scrolling fields. |
| Watch result and activity | Reload a completed workspace; open Technical log and close/reopen the result by keyboard. | Exact saved native output loads when its visible container opens; filename is shown, and result dismissal restores its trigger. |
| Initial and degraded state | Observe pre-owner loading, a healthy empty catalog and a rejected catalog request. | Loading and empty results do not claim failure; a real failed request retains its error and retry path. |

Focused deterministic owners are in `frontends/glass-drive-ui/tests/`: `test_asset_delivery.py`,
`test_theme_appearance.py`, `test_final_ui_polish.py`, `test_personal_library_ui.py` and
`test_expert_picker.py`. Reuse their valid proof and relevant consumer checks. They do not replace
the actual browser checks above or certify unrelated provider execution.
Record the actual rendered viewport and settled state from the same tab as each responsive
capture. A requested size or a filename is not proof of the captured layout. Keep rejected or
loading captures separate from acceptance evidence.


## 10. Professional Worker starters and real deliveries

Use the bundled examples under `runtime_phase1/src/workers_projects_runtime/starter_packs/`.
Each package contains a sample job, company onboarding material, valid and deliberately invalid
inputs, and expected checks. Keep actual account details, native traces, screenshots and raw
research private. A working synthetic example is not professional certification.

| Starting expert | Useful work to inspect | Required check |
|---|---|---|
| Maya, Clinical Biostatistician | Study review, analysis tables and a reproducible workbook | Recompute the planned estimates and uncertainty; identify missing responses. No unsupported efficacy claim. |
| Theo, Bioanalytical Scientist | Assay review, QC exceptions and an editable calculation workbook | Reconcile the fitted results and approved QC limits; keep unsupported causes unknown. |
| Priya, Regulatory Affairs Specialist | Evidence map, gaps and draft change-impact review | Follow each claim to the supplied source and revision; distinguish missing, draft and approved evidence. |
| Alex, Growth Marketer | Campaign plan, copy, claims review and measurement workbook | Reconcile the budget and assumptions. Separate observed results from scale, audience and capacity hypotheses. |
| Morgan, Finance Director | Variance bridges, cash scenarios and a board brief | Reconcile source totals and formulas; keep unexplained cash differences visible instead of plugging them. |

1. Choose a starter through Run Project. Supply the job, desired result and company files. Review
   the actual target's selected native Skills before execution; cancelling must keep the job and
   saved worker. Optional source links do not mean a plugin is installed or an account connected.
2. Run the sample through the supported configured harness. Inspect the real report and download
   its files through the delivered UI. Independently compare key numbers and formulas against the
   original inputs. Verify source and Skill bytes are unchanged. Reject unsupported claims even
   when the arithmetic passes.
3. Supply the deliberate bad input. The worker must identify the source problem without guessing
   a repair or presenting that case as completed analysis. Keep unrelated accepted work intact.
4. Ask the same worker to refine the result or change an explicit assumption. Retain its account,
   workspace, selected versions and native history. Preserve the original deliverable where the
   request calls for it. Verify the actual revised files and any required numeric invariants.
5. Use Ask in Chat for that worker's project. Confirm the exact existing worker is selected, queued
   work is described truthfully, and a useful completed result with working file links returns to
   the conversation. Reload and replay must not create another worker or repeat completed work.
6. Repeat the affected entry path through the public API and management MCP using the same starter,
   template and worker mechanisms. Check the real MCP tools and file downloads; tool discovery or
   import alone does not prove execution. Foreign identities, unapproved native material and
   invalid file paths must retain their existing access boundaries.

Deterministic owners include `test_starter_templates.py`, `test_starter_pack_examples.py`,
`test_coordinator_existing_worker.py`, `test_coordinator_result_files.py`, and the existing native
file, provider, template and Library banks. Use only the checks selected by the agreed QA scope.
Native jobs, real browser journeys and provider limitations remain explicit evidence gates.
