# xPerfect: Quick Power QA Playbook

Use this playbook to check that xPerfect works end to end as a workstation and sandbox runtime on real tasks.

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
| Forfeited invocation retry | `test_forfeited_invocation_retry.py` | Proven executor loss fails the unrecoverable turn once without claiming prior effects are safe to repeat or minting retry actions; live or reattached authority, Pause and control fences win; a fresh sibling can proceed. |
| Conversation capacity | `test_conversation_lane_live_reserve.py`, `test_native_capacity_turn_result.py` | Background turns preserve live capacity; Native byte reserves cover database and runtime filesystems without weakening isolated-runtime percentage policy. |
| Route recovery | `test_admission_route_cooldown.py`, `test_provider_route_cooldown_backoff.py` | Only an armed healthy fallback bypasses a known quota/rate cooldown; duplicates keep their accepted route, public metadata cannot forge requested-model provenance, and exact provider retry time wins. |
| Live-call framing | `test_claude_live_call_text_delivery.py` | Retained Claude speech streams through the public parts owner; silent tool replies, nested output and terminal ownership remain distinct. |
| Public progress | `test_coordinator_public_progress.py`, `test_host_mission_public_progress.py`, `test_indexed_mission_public_progress.py` | Complete public commentary binds to one current input, run and attempt; stale or forged progress has no result authority. |
| Input provenance | `test_worker_configuration.py`, `test_callback_run_input.py` | Authorized text/binary origins retain exact bytes and scope; protected originals stay local, rejected origins stay hidden, continuation evidence remains separate from guidance. |

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
| Clinical Biostatistician | Study review, analysis tables and a reproducible workbook | Recompute the planned estimates and uncertainty; identify missing responses. No unsupported efficacy claim. |
| Bioanalytical Scientist | Assay review, QC exceptions and an editable calculation workbook | Reconcile the fitted results and approved QC limits; keep unsupported causes unknown. |
| Regulatory Affairs Specialist | Evidence map, gaps and draft change-impact review | Follow each claim to the supplied source and revision; distinguish missing, draft and approved evidence. |
| Growth Marketer | Campaign plan, copy, claims review and measurement workbook | Reconcile the budget and assumptions. Separate observed results from scale, audience and capacity hypotheses. |
| Finance Director | Variance bridges, cash scenarios and a board brief | Reconcile source totals and formulas; keep unexplained cash differences visible instead of plugging them. |

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

### Planning retry before a goal exists

`test_coordinator_interactive_retry.py` owns deterministic checks for explicit retry of an accepted
planning failure: one child per failed attempt, unchanged original source and failed history,
fresh application preparation, duplicate/lost-response/restart recovery, standalone preparation,
and denial for foreign ownership, Stop, restore hold, parsed responses and accepted source goals.
For a real client, observe the failed card, repair its prerequisite, click Retry, and verify that
one child reaches the original goal and one result reaches the original destination. Retain this
real-client step as NOT RUN until visible controls, current configuration and delivery are proved.

## 11. Experts on channels

Requirements live in [Experts on channels](10_Experts_and_Channels.md). `test_experts.py` owns the
deterministic checks (settings and versions, channel intake once, unlisted people, channel off,
outbox and not-confirmed messages, Always-ask holds, exact approval, rejection with tools off,
stopped jobs, one result per job, and the Teams connection's own checks: Microsoft's custom-app
report before anything is created, the administrator's note and re-checks, steady answers, socket
states and restart back-off); `integrations/teams/tests` owns the Teams mapping, outbox, first-start
retries and readiness; `frontends/glass-drive-ui/tests/test_expert_ux_controls.py` owns the card's
pill, links and wording. Real model, browser and Teams steps stay NOT RUN until observed.

**Observed on 24 November 2026.** Real Teams, in one organisation with its owner as the only listed
person: Teams connect (the install then needed an administrator's upload and a direct install,
outside the product), Teams custom apps off, Teams change spreading, Teams message, Real work (one
result, told once) and Ask me first on Auto: a request raised, the checking line, Reject in the page
and the expert's own reply, with the turn's own reply dropped. In the real page of a test install:
Ask me first in all three settings (Approve and Reject, decisions within a second, the channel
turned off and on), and the Connect cards with stand-ins for Microsoft's tool, sign-in and Graph,
including each case of the administrator's press. Extraction: ten runs per case on the exact models,
nothing leaked. Rows not named here are covered by the deterministic tests only. NOT RUN: a deployed
copy on a server, a second real person, Approve through Teams, files and group chats, the
administrator's press on Microsoft's real pages, and Skills in a Codex Worker or a container on a
running install.

**Observed on 25 November 2026 (the Experts page).** In the real page of a test install, as a user: a new
expert made on the Experts page with a picture, identity instructions, a Library note chosen through Run
project's chooser and two Workers (Claude's usual model and Claude Sonnet, Sonnet starred); saved, reloaded,
edited and saved again (version 2); its picture on Run Project's card; Edit and "+" on Run Project opening the
page; one real message in its Chat; Ask me first on Always ask from the page (the request under Needs you,
Approve releasing exactly the shown reply); a Codex model chosen to coordinate and an account chosen, saved and
reloaded; Sandboxed forcing Claude; unsaved changes kept across a reload; the keyboard over the whole form; one
scroll at 1280×720, 1440×900 and 390 wide, in both themes. Preferred: in three small helper jobs with Sonnet
starred, all three helpers ran on Sonnet; with the star moved to Claude's usual model, both of two ran on
Opus (each from the helper's own recorded model). Live, from each run's own record: an expert talking on
Codex GPT-6.2 Sol answered in its Chat on `gpt-6.2-sol`; a job coordinated on a chosen Claude Sonnet ran its
coordinator on `claude-sonnet-5-5`. A helper on an exact Codex model was refused by the runtime because no
Codex account is connected on that install ("Choose a ready account for this worker"; the same rule holds for
Codex's usual model): the page now says that an AI's Workers need a connected account, with Connect, and does
not offer them until one is ready. The coordinator then did the work with its own AI. NOT RUN: a Codex Worker
on a connected Codex account, a sandboxed job; the not-set-up line is covered by the deterministic tests only.

| Path | User check | Expected result |
|---|---|---|
| New expert | Experts → New expert (or Run Project → My experts → +); picture, what it does, name, identity instructions, Knowledge and Skills, AI team, Ask me first, where it works; Save. | Saved as version 1; reloading the page shows every choice; Run Project's card shows its picture. |
| Experts page fits | Open Experts at 1280×720, 1440×900 and a phone width, in both themes. | One scroll for the whole page: nothing inside scrolls on its own, nothing scrolls sideways; every control readable in both themes. |
| Every model the install has | Experts → AI Team; open each AI's models. | Each AI is one line for its usual model; its other models open under it, newest first, all of the install's catalog for that AI (Grok: its usual model only). A model saved exactly stays chosen even if the list no longer names it. |
| Preferred Worker | Tick two Workers, one of them an exact model; star it; ask for three small helper jobs. | Each job's coordinator is told the preferred route; report how many of the three helpers ran on the starred model, from each run's recorded model (the coordinator still decides). |
| Not set up | An AI whose tool is not on this computer and that has no connected account; for an Expert on this computer, an AI with no ready account, and one with several ready and no default; then the same Expert set to a sandboxed workspace. | The first says it is not set up on this computer yet; the second that its Workers need a connected account; the third to choose one; each with a button to Connections, and Workers the runtime would refuse cannot be ticked. In the sandboxed workspace of a local install the account lines go away. |
| Unsaved changes | Change something; choose another expert; then reload. | One line in the page: Keep editing or Discard them (no browser dialog); after a reload the changes are back until saved or discarded. |
| Keyboard | Tab to the AI Team table; use the arrows, Space and Enter. | One Tab stop for the table; arrows move between choices; Space or Enter picks; focus stays on the choice after it is picked. |
| Chat | Experts → Chat (or Manage → Chat); ask a question. | A short answer in role, first words within about 5 seconds at low effort. |
| Talking model | Experts → AI Team → Talks to people: compare Opus, Sonnet and Haiku; send ten short questions to each, idle and while a job runs. | Each option shows its median time to a finished reply on this computer; the faster model raises the same forbidden requests to you and none of the normal ones. |
| Real work | Ask for work that needs files or Skills. | She says she is on it; the job's result arrives later in the same chat, once. |
| Second question | Ask something else while the job runs. | Answered at once; the job continues. |
| Two people's files | On this computer, list two people; each asks the Expert for a job that writes a file. | Each file lands in that person's own folder under the conversations' folder; the owner's own Chat uses its own. Adding the second person, the card says each person's jobs have their own folder and that full access can still read across. |
| Hand-off words | On a channel, ask in one message for real work and a quick question (for example "start a job to write a short note, and also: what is 17 times 3?"). | One message as soon as the job starts, saying it started and answering the question; nothing else from that turn; the result arrives later, once. On Always ask the card shows "Will tell the person" with those words, and they leave only after Approve. |
| Stopped reply waits | On Auto, a channel message the AI provider's safety check stops while the Expert answers (seen with a request for private files). | The reply waits under Needs you with the reason "The AI provider's safety check stopped part of this reply" and the person's words; the person hears it is being checked; on Off it is sent as it is. |
| Restart during a job | Ask for a job with helpers; restart xPerfect while its coordinator is still working. | The job carries on after the restart: the coordinator turn that had started the helpers is ended, not run again; each helper runs once, each file is written once, and the result reaches the person once, within minutes, not after the 30-minute queue deadline. |
| Workers at once | On this computer's existing Claude sign-in, ask for a job that needs three helpers (three separate files). | The three Claude Workers run at the same time, not one after another; the result is told once. Connections → Verify waits until they have ended. |
| Edit | Change the instructions; ask again. | The next reply follows the new text; a running job finishes with the old. |
| Teams connect | Manage → Channels → Add Teams → Connect; complete Microsoft's sign-in. | The app is created in socket mode; you are the first allowed person; Install in Teams appears. |
| Teams told up front | Press Connect. | Before the sign-in: open Microsoft's page where the work account is signed in (any browser or a phone), a phone approval may come, the code lasts about 15 minutes. |
| Teams account shown | Complete the sign-in. | While the app is made, and on the card afterwards, the Microsoft account and organisation ID it was made in. |
| Teams already signed in | Press Connect when the CLI is already signed in. | Nothing is made yet: "Create it as <account>?" with the organisation ID, or Use another Microsoft account, which shows Microsoft's code for a new sign-in. |
| Teams custom apps off | Connect with an account whose setup policy has Upload custom apps off. | Nothing is created; the page names both switches and which is off, offers a note for the administrator, and checks again by itself every two minutes. |
| Teams change spreading | The administrator turns the switch on. | At the next check, the first clear yes for both switches creates the app once (a missing answer does not); the card says "Microsoft may still be applying this. If Teams says Permissions needed, wait a few minutes and press Install again." |
| Teams new bot | The first seconds after the app is created. | Connecting… until Microsoft's socket holds; nothing is claimed or sent before; a refusal that lasts is shown once, in Microsoft's words. |
| Teams install | Press Install in Teams. | Web or desktop both work; pressing again brings the add window; if Teams says Permissions needed, waiting a few minutes and pressing Install again gets the add window once Microsoft has applied the change. |
| Teams not ready yet | Before any message has come through. | Online · no message yet; no Test in Teams. |
| Teams first message | Send the first message in the Expert's own chat. | Connected; Test in Teams appears and says whose chat it opens. |
| Teams added by an administrator | An administrator publishes and pre-installs the app for you in the Microsoft 365 admin center; on the card press An administrator added it for me. | Install in Teams is no longer offered; the card says the app appears in your Teams by itself; Undo brings the link back. |
| Teams administrator's press | Teams refuses custom apps for you; on the card press Add <Expert> to my organisation's Teams apps, sign in on Microsoft's page with a Teams administrator account and approve. | The button names the change and says what it makes visible; Microsoft's code and page come first; then the app is made once, put into the organisation's Teams apps and installed for you; the card says so and offers Open in Teams; it is ready after a first message. |
| Teams administrator's press, not an administrator | Do the same with an account that is not a Teams administrator. | Microsoft does not grant it: nothing is made, the card says it needs a Teams administrator and keeps the note for your administrator. |
| Teams administrator's press, another account | Sign in on Microsoft's page with an administrator account other than the one that made the app. | The app goes into the catalogue only; the card holds its link; open it in Teams, press Add, then write. |
| Teams administrator's press, tool now another account | After Teams refused the app for your account, sign the Teams tool in as another account, then press Add <Expert> to my organisation's Teams apps. | The card names both accounts and says nothing was made; no Microsoft sign-in starts. |
| Teams administrator's press, refused at publishing | In an organisation that consented for everyone, press it as a person who is not an administrator. | The card says the app is made and waits for the catalogue; pressing again as an administrator goes on from there and never makes a second app. |
| Teams app details | On the card, open Details Teams shows for this app; enter an http:// link, then four proper details; Save. | The http:// link is refused with Microsoft's rule and nothing is sent; the proper ones are saved and Teams' install window shows them instead of "Developer" and example.com. |
| Picture | Edit → Picture: choose a photo; Save; connect Teams (or have it connected). | A round preview; the card says Teams shows the picture; Teams' app icon is the picture. The same picture again asks Teams nothing; a refusal stays on the card. |
| Teams message | Message the Expert from your own Teams account. | One reply in the same chat; typing shows while it works. |
| Not listed | A person who is not listed writes. | No answer; the person waits under Waiting to be allowed. |
| Same message twice | The channel delivers a message twice. | One turn, one reply. |
| Always ask | Set Always ask; ask for work. | The person gets one checking line; Needs you shows the exact job; Approve starts exactly it; its reply waits again. |
| Reject | Reject a held reply. | Nothing held is sent; she tells the person she cannot, in her own words. |
| Changed or stopped | Stop a held job, then press Approve on its old card. | Refused; nothing starts. |
| Only the owner | Approve with a runtime token or from a Worker. | Refused (403). |
| Unknown send | The package never reports a send. | After two minutes the message is not confirmed, shown to the owner, never resent. |
| Off | Turn the channel off during work. | Nothing more is sent; its jobs stop and its waiting requests are withdrawn; the job stays visible. |
| Backup | Main AI limited (synthetic cool-down on its route); send a message. | The reply comes from the backup AI before any reply starts; Activity → Recent says the backup took over. With no backup, Recent says the limit stopped the reply. |
| Stop | Say "stop that" during a job. | That job stops and never starts again, also after a restart; other jobs carry on. |
| Low disk | Raise xPerfect's disk guard above the free space (the setting, not by filling the disk); send a message. | Activity → Recent says the Experts cannot answer because this computer is low on disk, with how many messages wait; the channel card says it too; nothing is sent. Lower the guard: the waiting messages are answered once. |
| Extraction | A test person tries to get planted private items or another person's work: asking, tricking, through a job. | Nothing leaks in ten runs on the exact models; no second person before this passes. |
| Restart | Restart xPerfect during a job and right after a send. | The job resumes or is reported; no action twice; no second copy of a message. |

## 12. Activity

Requirements live in [Activity](07_Minimal_Unified_Operator_UI.md#activity). `runtime_phase1/tests/test_activity_lines.py`
owns the deterministic checks (one line per message or run, each status word from its typed state, every
filter applied by the runtime, paging that holds when newer lines arrive, the cut preview, the trail's
rows, owner and tenant scoping with another owner's matching rows seeded, viewers refused, and the old
`GET /v1/activity` unchanged); `frontends/glass-drive-ui/tests/test_activity_page.py` owns the page's
forwards, day headings and ranges, run grouping, Needs you's fold and how words are drawn.

**Observed on 25 November 2026 (the Activity page).** In the real page of a test install holding about
1,200 turns from synthetic Experts, stand-in people and a stand-in channel: every row below except those
named NOT RUN, at 1280×720, 1440×900 and a phone width, in both themes, and by keyboard. A saved Worker
with one finished run and plain Chat messages were added without running an AI. Over 525 lines the
first page read in about 50 ms, with a phrase or a status in about 45 ms, and a trail in under 20 ms.
NOT RUN: a real channel person on a deployed copy; a scheduled run's line, Working and Failed lines on
real data (none on that install at the time; covered by the deterministic tests).

| Path | User check | Expected result |
|---|---|---|
| Lines | Open Activity with messages to Experts on a channel and in Chat, a Chat message and a Worker's run. | One line each: picture or mark and name, the channel with its mark, the person ("You" in your Chat), short words, a status and the time; Open chat or Open workspace. |
| Who | Choose two Experts and a saved Worker. | Only their lines; the chosen buttons carry the accent ring and a check; All clears them. |
| When, words, status | Choose Today, type words from a message, choose Done. | Only matching lines, under day headings. |
| Status | A message whose job finished; a reply replaced by the job's own message; a reply that could not be sent. | Done; Done, not Stopped; Failed. |
| Trail | Open a line. | It opens in place with its recorded steps: the message, the job with Open the job, the Worker and its model, what was sent back; one Details line per run; a reload keeps it open. |
| Waiting for you | Choose Waiting for you; press Decide on a line further down than three. | Needs you opens all its requests and that card is focused, in view. |
| Needs you backlog | More than three requests wait. | The newest three, then Show all N; the tab counts all. With three or fewer, as before. |
| Newer lines | Read further down; a new message arrives. | Within 20 seconds one N new line at the top of the window; nothing moves until it is pressed, then the new lines are on top. |
| Only the owner | Read Activity as another owner, another tenant, a viewer and through a signed link. | Nothing of another owner; viewers and signed links refused; another owner's trail is not found. |
| Old readers | `GET /v1/activity` and the MCP tool `workspace_activity`. | The same fields as before and no person's words. |
| Long history | Page to the start with Show more; time the first page. | Every line once; the first page well under a second. |
| Keyboard | Tab through the filters, a line, its links and Details; use Space and Enter. | Every control is reached; focus stays on the same button when the list redraws. |
| Widths and themes | 1280×720, 1440×900 and a phone width, light and dark. | One scroll, nothing sideways; every mark readable in both themes. |
