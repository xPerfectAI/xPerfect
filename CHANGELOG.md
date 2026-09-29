# Changelog

From GlassHive's first retained source snapshot to xPerfect today. These are dated development
milestones, not versioned release announcements. Related fixes are grouped by their benefit to
users and builders; internal merges, test-only changes and dependency housekeeping are omitted.

GlassHive entries describe the predecessor at that time, not a promise of current support.
The [capability guide](docs/capability-matrix.md) owns today's availability and limits.
xPerfect is licensed under [Apache 2.0](LICENSE); this does not change GlassHive's historical license.

[Visual user guide](https://www.xperfect.ai/docs/) · [Website changelog](https://www.xperfect.ai/changelog/)

## 2026-09-29 — Start, reuse and schedule an expert

- **Added:** One `./xperfect start --docker` command obtains the published package, starts the local app and reports its address and private setup files.
- **Improved:** Find and copy saved experts by name or alias. A copy keeps ordinary native skill files; account reuse stays within the same owner, ready account and matching worker type.
- **Fixed:** Scheduling keeps the saved expert's definition, name and selected account instead of creating a different workspace. Follow-up messages return to that expert.
- **Improved:** Local AI clients receive the instance's MCP address and the running service's CLI information. Docker startup names actionable network and registry failures and cleans up only its own failed attempt.
- **Added:** A visual user guide covers startup, accounts, UI and MCP control, files, native skills, schedules and reuse. Current public images are `linux/arm64`; Windows startup and other architectures remain unverified. Hosted account onboarding and automatic container-client setup still have documented limits.

## 2026-09-27 — Complete replies and safer retries

- **Fixed:** Claude replies retain every authored turn. Result handling keeps the full final text, including literal report labels and the first character.
- **Fixed:** Coordinator retries retain the correct delegated goal. Refused work says why it is blocked rather than appearing to run.
- **Fixed:** A file attached in Watch is recorded once; repeated event handling no longer creates duplicate attachments.

## 2026-09-26 — Clearer Watch and first use

- **Improved:** Watch shows the exact run attempt, its terminal and a formatted saved answer. Reopening a result reads saved work without starting it again.
- **Fixed:** Stopping native work tracks the process tree that belongs to that run. Interrupted work releases the appropriate account state.
- **Improved:** Conversation names an unavailable route before Send, keeps explicit model choices and distinguishes local execution limits. First-run instructions now match the actual screens.

## 2026-09-25 — Queue recovery and guarded upgrades

- **Fixed:** Interrupted and capacity-waiting work keeps its workspace and exact run ownership. Old completions cannot overwrite a newer attempt.
- **Improved:** Idle workspace compute can be released without losing its files. Packaged upgrades stop idle compute; active, queued or waiting work still blocks an unsafe upgrade.
- **Fixed:** Service shutdown stops accepting new work before releasing its leases, reducing stranded reservations after restart.

## 2026-09-24 — Grok Build and shared workspaces

- **Improved:** The Grok Build adapter uses native execution and provider controls while preserving the selected model and account.
- **Added:** Shared workspace controls support mixed worker types, common project files or private member folders, and explicit collaboration permissions on a configured Linux host.
- **Improved:** Workers have an authorized native context route. Unavailable context or isolation blocks execution instead of silently weakening the boundary.

## 2026-09-23 — xPerfect becomes a standalone project

- **Added:** A separate public xPerfect repository with an Apache 2.0 license, standalone launcher, browser UI, MCP/API interfaces and deployment documentation.
- **Added:** Hosted storage defaults to 5 GB per owner with no default per-file or file-count cap; local storage has no default quota. Host permissions and available disk space still apply.
- **Preserved:** GlassHive's projects, workers, native harnesses and durable workspace foundation. The new repository starts with independent public-safe history; the original history is preserved separately.

## 2026-09-21 — Joined orchestration and recovery

- **Improved:** Worker brokerage and controls share durable goal, run and provider-session records, helping retries and callbacks target the right work.
- **Fixed:** Native input, cancellation and terminal-request reconciliation retain exact ownership through recovery. Provider failures keep their reason rather than becoming empty results.

## 2026-09-07 — Native continuity across restarts

- **Improved:** Input, credentials, cancellation and recovered results stay bound to their native run. Late results cannot finish a different generation of work.
- **Improved:** Workers keep native tool and media capabilities; the runtime carries context without inventing a plan or replacing model judgment.
- **Fixed:** Worker sessions and terminal histories remain separate. Credential redaction covers complete values across output boundaries.

## 2026-08-30 — Durable parallel work

- **Added:** Persistent orchestration records for parallel goals, admission, queues and lifecycle effects, with isolation and resource checks before execution.
- **Improved:** Restart and callback recovery use recorded ownership. Queue and provider failures report structured causes.
- **Improved:** Public compatibility and credential-safe links remain intact while the orchestration foundation expands.

## 2026-08-21 — Workspace readiness and status

- **Improved:** Workspace and run status explains readiness, failure and available recovery actions more clearly.
- **Fixed:** Runtime health checks verify workspace data, and the browser's first paint works in Edge.
- **Improved:** Provider diagnostics distinguish expired authentication, temporary failures and unavailable prerequisites.

## 2026-08-18 — Reusable workspaces and connected accounts

- **Added:** Reopen saved workspaces by human-readable name or alias and set up native tools in an idle workspace.
- **Improved:** Supported workspace setup reuses the selected connected account; Claude sign-in and workspace tools work together without copying unrelated sessions.
- **Fixed:** Stale workspace sessions, account removal and credential-bearing sandbox cleanup have explicit recovery paths.

## 2026-08-14 — Simpler MCP setup and native plugins

- **Improved:** External AI setup discovers the appropriate connection path; Codex OAuth reconnect retains the required scope.
- **Added:** Native client plugin packages and persistent personal-workspace plugins, alongside direct MCP launch and follow-up.
- **Improved:** Workspace discovery tolerates large listing requests, and database/service shutdown waits for its own background work.

## 2026-08-11 — A workspace control room

- **Added:** A simpler Workspaces experience with open, duplicate, new work and visible saved deliveries. Opening or downloading a completed result stays read-only.
- **Improved:** Safe HTML result previews and reliable downloads; copies separate workspace files from private browser and account state.
- **Added:** User-scoped control-plane access, guided personal connections and configured local or provider-hosted login. Authorized Watch actions and voice task-control integration use the existing runtime boundaries.

## 2026-08-03 — Persistent conversations

- **Improved:** Conversation state survives service restarts, with bounded recovery and one wake-up per waiting execution lane.
- **Added:** The conversation-provider adapter advertises its cascaded voice capability for compatible clients; it is not a standalone calling service.

## 2026-07-30 — A conversation API for builders

- **Added:** A conversation-provider adapter and OpenAI-compatible Responses endpoint, including supported streaming options.
- **Improved:** Activity stays separate from the final answer; provider authentication, rate limits and cancellation keep their own meaning.
- **Improved:** Queued work can recover without repeated API polling. HTTP MCP requires isolated authentication.

## 2026-07-24 — Live activity without exposing task content

- **Added:** Bounded Claude activity and retry-location telemetry tied to the active run.
- **Fixed:** Incremental activity stays cumulative, completed transcripts remain available, and telemetry responses avoid task content.

## 2026-07-22 — Bedrock and recorded usage

- **Added:** A Claude Code route for Amazon Bedrock and per-run Claude token-usage records, including recovered failures.
- **Fixed:** Paginated artifact retrieval retains files across pages. Public link and browser privacy safeguards keep sensitive connection details out of shared surfaces.

## 2026-07-16 — Exact effort and safer lifecycle controls

- **Improved:** Requested Claude effort reaches native execution, and the API reports the accepted setting.
- **Fixed:** Worker termination verifies compute has actually stopped. Provider setup and final-report handling preserve the selected profile and complete output.
- **Improved:** Runtime and UI dependencies receive maintenance and security updates without changing the worker's goal.

## 2026-06-27 — More reliable files, reports and links

- **Improved:** Native worker capabilities and complete instructions survive delegation. Preflight checks match the selected provider and requested effort.
- **Fixed:** Deliverable selection, worker evidence and status distinguish a real result from incomplete work. Workspace links are scoped and checked before use.
- **Improved:** Watch and desktop assets refresh together; sensitive preflight and MCP configuration values are redacted.

## 2026-06-06 — Portable context and tools

- **Improved:** The bootstrap bundle carries instructions, files and native MCP configuration into host or sandbox workers.
- **Added:** A host-owned capability-broker path for approved connected tools, keeping provider credentials with the host application.
- **Improved:** Runtime preflight, deliverable handling and Watch visibility support the complete work-to-result path.

## 2026-05-26 — Tenant boundaries and delivery recovery

- **Added:** Enterprise tenant isolation and scoped MCP access, with explicit provider bootstrap and worker permissions.
- **Fixed:** Workspace delivery recovery and signed-link routing preserve the intended result and surface.
- **Improved:** Host callbacks and worker reports remain tied to their authorized request; relevant dependencies receive security updates.

## 2026-04-30 — Background delegation and callbacks

- **Added:** Host-native callbacks and nonblocking worker delegation, so a host application can receive the final report while other work continues.
- **Fixed:** Completed reports reliably return to the host instead of remaining only in the worker session.

## 2026-04-20 — Steer and queue a follow-up

- **Added:** Change an active worker's direction or queue its next instruction through explicit controls.

## 2026-04-16 — Launch and watch a workspace

- **Added:** Workspace launch and a desktop-first Watch experience with live terminal and workstation access.
- **Improved:** Saved workspace and browser-session continuity make it easier to return to ongoing work.

## 2026-04-01 — GlassHive's initial source snapshot

- **Added:** Persistent projects, workers, runs and events; resumable workstation sandboxes; lifecycle controls; browser desktop and terminal takeover.
- **Added:** Native Codex, Claude Code and OpenClaw profiles, a portable bootstrap bundle, an HTTP API and MCP transports for external clients.
- **Established:** A standalone runtime boundary: host applications can be clients or identity brokers without owning the worker runtime.

<!-- Maintainers: newest dated milestone first. Record user-visible changes and limits, not private
     evidence or raw predecessor metadata. No release version unless an actual release owns it.
     This file is the canonical source for the website timeline. -->
