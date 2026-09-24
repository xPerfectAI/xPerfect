# xPerfect: Minimal Unified Operator UI

## Purpose

Define a separate, minimal, modern operator UI for xPerfect that does not alter the existing runtime UI and does not mix frontend concerns into xPerfect core runtime code.

## Visual Thesis

Calm dark glass over a bright signal core, like a modern appliance or Tesla control surface rather than a developer dashboard.

## Content Plan

1. Entry composer
2. Automatic transition into live watch mode
3. Minimal ribbon controls
4. Hidden advanced controls only when needed

## Interaction Thesis

- the centered project composer should feel like the only thing the user needs to understand
- after submit, the interface should hand off immediately into live watch mode
- advanced controls stay tucked away until the user explicitly asks for them
- the default watch handoff should land on the live desktop, not the raw terminal page
- user-facing language should call long-lived personal environments `Workspaces`, not `workers` or `sandboxes`
- apply the [first-use simplicity principle](01_Vision_Requirements_and_Terminology.md#first-use-simplicity):
  use configured defaults, remove unnecessary required inputs and choices, and reveal advanced controls on demand
- Schedules open to saved definitions or a clear empty state. Creating or editing opens a compact
  form with Once, Every day, Every week, and Custom choices. The workspace list includes one-off
  runs; creating their first schedule keeps the chosen run as a saved workspace in the same action.
  When no workspace exists, show Start project and Cancel instead of an unusable schedule form;
  Start project returns to the visible goal field, not Advanced.
  Saved rows keep Run now, Edit, and
  Pause or Resume visible, with History and Remove under More. They show the authoritative next
  occurrence with its saved timezone. A failed refresh keeps the last known list visible with an
  error and Retry; an uncertain Run now result tells the owner to check history before retrying.
  That uncertain request retains its idempotency key through a list or same-owner tab refresh;
  the owner sees Confirm prior run until a confirmed response settles the request. History reloads
  after the attempt so a cached empty view cannot hide a new occurrence. Weekly day/time edits
  submit the new local anchor, while untouched stored recurrence fields retain their exact values.
  A failed occurrence puts View history on the row and keeps technical error text behind Error
  details. A failed history request has its own Retry action.

## Hard Requirements

1. Do not edit or replace the existing xPerfect runtime UI.
2. Do not mix this frontend into xPerfect core runtime code.
3. The new UI must live in a separate service/surface.
4. The entry screen must present one centered project definition box.
5. `Describe your project` is the only required project input. Adding files is a primary optional action.
6. `Success criteria` is optional and visible with `Background` in the default composer. An omitted
   value stays omitted; the host must not invent, copy, or silently inject a replacement rubric.
7. Background/context are optional and visible in the composer; technical settings remain
   progressively disclosed. This replaces the earlier fixed three-field form and required
   success-criteria step.
8. There must be an optional smooth workspace selector for:
   - `New workspace`
   - existing named workspaces
9. On submit:
   - create or reuse the selected workspace's underlying worker
   - faithfully send the goal, supplied criteria/context/files, and authorized capabilities
   - redirect automatically to the live watch screen
10. The watch screen must prioritize the live sandbox view.
11. For webpage, app, and browser-visible deliverables, the watch screen must end on the delivered result itself rather than a raw terminal transcript.
11a. Chat/tool takeover links must target the configured xPerfect operator `/watch/{worker}` URL
    as the primary user-facing surface. The compatibility `worker_url` field is an alias for that
    operator URL; raw noVNC and runtime takeover URLs are diagnostic-only.
11b. The secondary `Open` action for a self-contained `.html` or `.htm` artifact must render the
    page inside the existing xPerfect file landing page, not show raw markup. That preview runs in
    a credentialless, no-referrer, unique-origin iframe sandbox with no scripts, authenticated
    same-origin access, forms, popups, top-level navigation, connection APIs, or external
    subresources. Ordinary link or frame navigation remains confined to that sandbox. Other text
    formats remain escaped source previews; interactive or multi-file web apps stay on the isolated
    workspace desktop. `Download` remains a distinct attachment action and must not navigate the
    user into an error page.
12. The raw live terminal session must still exist and stay available as a secondary surface for takeover and debugging.
13. When desktop-first watch is enabled, the active worker terminal must also be visible inside the desktop itself by default so the operator can watch the real live session without leaving desktop view.
14. The watch screen must expose a direct, obvious path back to the modern xPerfect Workspaces
    control room. Ordinary product navigation must not send users to the legacy runtime `/ui`
    pages. Those routes remain available only as compatible operator diagnostics.
    A previously shared worker-detail URL opens the primary Watch view for the same authorized
    worker. A signed view token moves into a short-lived, scoped cookie before the clean Watch
    URL is shown. A closed workspace with a saved result shows that result in Watch; the raw
    worker-detail form is not part of the normal journey. For the signed-in owner, a closed workspace
    lists and downloads files from a retained file root in Watch as read-only; file changes and new
    work stay closed. Signed view links remain revoked on close. Teardown-in-progress and failed
    teardown never expose the retained file view.
15. The initial watch surface, provider/model overrides, and other technical choices remain advanced controls. The default path uses the configured route without making the user choose it again. If host Codex has no explicit model setting, the conversation route invokes its native default without a model override; xPerfect applies a conservative 32,768-token admission ceiling because the account's actual model is not known to the catalog. A selected exact-model Allowed AI policy requires an explicit catalog model.

Allowed AI is at the top of Workspace settings. Project and Workspace each have a short summary; the Workspace choice is open first and opening either scope closes the other. The project default permits all currently owner-authorized AI as accounts change; the workspace inherits it unless the owner chooses a narrower setting. Radio choices expose these modes directly. Choosing exact options starts with all offered harnesses checked. Opening exact models or accounts checks currently authorized and busy choices first; denied, unavailable and unknown choices stay unchecked. An owner can uncheck a harness or narrow models or accounts with one button per group. Project changes refresh the Workspace choices in the same dialog without dropping an unsaved selection; selections outside the new project limit remain visible and marked denied. The connection list labels subscriptions and API keys even when only one kind is present. Saved choices absent from the current catalog keep their last safe label and kind when known, otherwise show a distinguishable stable ID. Connect account remains direct. These settings affect new starts only and never replace the account chosen for a particular worker.

The exact catalog also includes `gpt-6-sol`, `gpt-6-luna`, `claude-opus-5-5`, and the Docker Codex default `gpt-5.4`. Their context limits and supported effort levels follow the [OpenAI Sol](https://developers.openai.com/api/docs/models/gpt-6-sol), [OpenAI Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [Anthropic Opus 5.5](https://platform.claude.com/docs/en/models/opus-5-5/overview), and [OpenAI GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4) model pages. A connected account's entitlement is checked when native work starts.

Standalone native replay uses neutral conversation wording. Existing versioned `viventium_*` delimiters remain as compatibility markers for the accepted-turn protocol.

A selected Docker account starts in a private execution-workspace member. Its prepared workspace path stays under the provisioned owner root across a continued conversation and fresh recovery members. A server-side `custom` or `life` workspace path is rejected for that route; files enter through the authorized Files attachment flow.
16. If project launch fails after a worker was created, the UI/runtime must record that launch as an explicit failure instead of leaving a healthy-looking but runless worker behind.
17. The main controls must be simple and obvious:
   - pause
   - resume/play
   - interrupt
   - more menu
   - delete/shut down available from more menu
18. The latest result must be visible in the top ribbon and expandable without leaving the live view.
18a. The latest workspace output affordance must look and read like an explicit action wherever it
   appears. The watch ribbon and workspace overview tiles should show `Latest workspace output`,
   the current status/summary, and a clear output/status action so users do not have to guess which
   text is clickable. Inspecting output must never resume or restart compute.
18b. Workspaces is the executive control room. It must show direct safe delivery actions and a
   bounded, view-only overview of active workers without mounting an unbounded wall of interactive
   desktops. Only active workspaces reserve a live-preview panel; retained and closed cards present
   the result/status and available actions directly. Overview polling is compact, non-overlapping, and limited to visible tiles; at most a
   small fixed number of live previews may be connected at once. A full interactive desktop remains
   one click away in Watch / Steer.
19. The steering box must remain visible across normal desktop widths and mobile-safe layouts.
20. A non-technical user should be able to understand the flow with near-zero explanation.
21. `Steer + send` must behave like a real redirect, not a passive note:
   - interrupt the active run
   - stop the exact live run session and its process tree
   - queue the replacement steer run automatically
   - keep the replacement steer run in execution mode until the requested action is actually performed or a blocker is raised
22. The watch footer must expose a safe secondary queue gesture without diluting the default steer meaning:
   - normal `Send` or plain Enter redirects now
   - long-pressing `Send` queues a follow-up without interrupting current work
   - `Cmd/Ctrl+Enter` and modifier-click send must also queue the follow-up
   - the primary action stays obvious; queue behavior is explained in its contextual menu or accessible help without a persistent instruction block
   - once queued, the UI/runtime contract must keep the workspace marked `running` until that queued follow-up actually settles
22a. Session-authenticated watch actions must forward the current double-submit CSRF cookie in the
    `X-GlassHive-CSRF` header on every state-changing request. Worker-bound signed links retain only
    their deliberately narrow communication exception; the watch UI must not broaden that bypass.
23. Exact run interruption depends on standard process utilities being present inside the sandbox image:
   - workstation images must include `ps`, `awk`, and `pkill`/procps
   - xPerfect uses those tools to stop the exact `.glasshive-runs/<run_id>` process tree during steer redirects

## User-Facing Workspace Model

- In the user-facing xPerfect UI, a persistent personal execution environment is called a
  `Workspace`.
- Internally, xPerfect runtime terms remain:
  - `worker` for the AI runtime identity
  - `sandbox` for the isolated workstation container
- A workspace is therefore a friendly label over:
  - one stable worker alias
  - one persistent home directory
  - one persistent project workspace
  - one browser profile / website-login state surface

## Least-Resistance V1 UX

The easiest non-technical user flow should be:

1. show recent named workspaces first, not raw worker IDs
2. make `Open workspace` the primary reuse action
3. make `Duplicate workspace` a first-class default option for branching from an existing workspace
4. make `New workspace` the clean-start option
5. auto-reuse the matching workspace when the parent system already knows the stable alias for the
   task or service
6. land directly in the desktop-first watch view after open, duplicate, or create
7. keep advanced lifecycle or debugging controls behind the existing ribbon / more menu
8. expose the latest delivery directly on each workspace card, with `Open output` and `Download`
   distinct from `Open workspace`

`Open workspace` should automatically resume a paused workspace. Non-technical users should not
need to choose between `open` and `resume`.

Opening or downloading an existing delivery is always read-only. A completed workspace restarts
only through an explicit `Continue` or new-message action; output inspection never calls resume.
Worker-local targets such as `file:///workspace/...` are never browser delivery links. If an older
deliverable record has only that internal target, Workspaces promotes the matching owner-scoped
artifact Open/Download refs instead.

### V1 Action Set

Primary:

1. `Open workspace`
2. `Duplicate workspace`
3. `New workspace`

Secondary:

1. `Pause`
2. `Resume`
3. `Interrupt`
4. `Delete`

Failed, cancelled, and interrupted workspaces must keep an explicit `Pause` recovery action when
their workspace substrate can still be running. Error guidance must never recommend pausing while
the normal Workspaces and Watch surfaces hide or disable that action. `Resume` remains inappropriate
for these terminal run states because it can restart compute without creating a corrected follow-up.

### Duplicate Semantics

`Duplicate workspace` is approved for the default v1 flow, but only with safe semantics.

V1 duplicate should:

1. create a new workspace identity
2. copy project files and project-scoped context from the selected source workspace
3. keep browser-session state clean instead of cloning cookies or active website logins
4. require an 8-128 character idempotency key on the workspace-catalog duplicate contract
5. scope that key to the authenticated tenant and owner, replay the original project/workspace for
   the same source/name request, and reject reuse for a different request
6. keep one key on the UI action until success so retries cannot create a hidden second project

The durable reservation is fail-closed. A fresh pending request cannot start a second project. A
stale reservation is recovered only when one exact owner-scoped worker has both its persisted copy
report and completed duplication event; ambiguous state is preserved for diagnosis or cleaned when
it has no workspace, never silently duplicated. The legacy project-worker duplicate route keeps its
request/response compatibility but uses the same fail-closed destination review gate.

Capabilities that cannot be copied safely are returned as exact, owner-scoped review items. Duplicate
and template destinations are created with that deterministic action-id report in the same durable
write as the new worker, before file copy or HTTP success. The destination remains unable to run until
every Library item is restored through its real approval, a concrete personal provider selection is
reviewed again, or the user explicitly confirms `Continue without this capability`. Legacy brokered
connection and provider-grant references are deliberately non-transferable: the UI says they were
not copied and never claims a setup message restored them. Provider selection cannot be waived.
Reapproval preserves the source grant's exact scope subset rather than requesting every current
manifest scope; API and MCP omission cannot widen it. Account-less `personal_preferred` fallback has
no private capability and therefore creates no impossible review item. A preferred account that is
disconnected, otherwise not ready, or later forgotten degrades to that same explicit fallback; an
unready `personal_required` account blocks duplication with a concise reconnect-or-choose recovery
instead of creating a permanent review item. Review state survives
refresh/restart in the destination record, including when the copied workspace falls outside the
first catalog page; an exact owner-scoped lookup clears it on account change without exposing another
user's labels. A terminal duplicate failure offers `Start fresh copy` only after the failed
attempt has no retained project or worker. If a destination remains, the response names it and
keeps the same idempotency key bound to that attempt. Host-capacity and storage failures retain
their typed cause so the UI can show the applicable recovery.

This gives users a real branch/fork action without silently carrying over sensitive browser state.
Duplicate has no default per-file, file-count, or batch-byte limit. Explicit
`GLASSHIVE_DUPLICATE_MAX_FILES` and `GLASSHIVE_DUPLICATE_MAX_BYTES` settings
remain available; the owner storage quota, safe-file checks, and copy deadline
still apply.

## Post-V1 Rename Semantics

- A workspace should have:
  - a stable internal alias for parent-side routing
  - a user-facing display label for the glossy UI
- If `Rename workspace` is added later, it should update only the display label and not silently
  rewrite the stable routing alias.
- If the product later supports alias editing, that must be a separate explicit flow because it can
  affect parent-side auto-reuse behavior.

## Derived Operator Brief Template

A canonical master prompt template was not found in the current repo search.

For this UI, the phase-1 operator brief should be:

- project description
- success criteria
- optional context
- execution rules:
  - treat success criteria as hard acceptance gates
  - keep working and researching until criteria are satisfied or a real blocker appears
  - keep asking: have I achieved this successfully?
  - inspect the actual output, artifact, browser-visible result, or tool evidence before final report
  - pause before risky or irreversible external actions
  - if the deliverable is a webpage or app, open the final result in the sandbox browser and leave it visible
  - if the result is a simple static page, prefer opening the final HTML file directly instead of relying on a temporary localhost server

## Architecture Decision

Use a separate frontend service that:

- serves the new UI
- proxies requests to the existing xPerfect runtime API
- keeps CORS and browser-origin concerns out of the runtime
- leaves the current runtime UI untouched

## Success Criteria

1. Existing xPerfect runtime UI still works unchanged.
2. The new UI works as a separate service.
3. A user can launch a project from the new UI with one primary interaction flow.
4. A user is redirected into a live watch surface automatically.
5. A worker can be paused, resumed, interrupted, and deleted from the new UI.
6. The live desktop remains the dominant surface.
7. A hello-world landing page task ends on the rendered page in the sandbox browser.
8. The active worker terminal is visible inside the desktop-first watch flow by default.
9. The exact live terminal session remains available from the same watch screen.
10. The watch screen provides a first-class link to the modern Workspaces control room, while direct
    legacy runtime project URLs remain compatible diagnostics and never become primary navigation.
11. Launches that fail after worker creation leave an explicit failure trail instead of a silent orphan worker.
12. The UI feels simple enough for a non-technical user.
13. A non-technical user can understand that reopening a named workspace returns them to the same files, browser setup, and login state.
14. The primary action set is open-duplicate-new and each action is understandable to a non-technical user.
15. `Open workspace` is the single reuse verb and automatically resumes paused workspaces.
16. `Duplicate workspace` creates a new workspace with copied files/context but a clean browser-session state.
17. Workspaces exposes a delivery without opening Watch / Steer or changing lifecycle state.
18. The overview bounds live previews and compact polling as the catalog grows; no offscreen tile
    captures keyboard/mouse input or creates an unbounded desktop connection.

## Current Phase-1 Clipboard Note

The sandbox desktop currently relies on noVNC. Clipboard support is available through the noVNC clipboard controls and browser clipboard permissions, but truly seamless automatic browser-clipboard synchronization is still constrained by browser security rules and is not yet elevated into a custom first-class xPerfect clipboard bridge.

## First-use acceptance

These are required interaction outcomes, not a claim that the current build passes them.

| User action | Expected experience and evidence |
| --- | --- |
| Start with only a goal | Configured defaults work without opening Advanced or requiring an invented criterion. Missing connection setup asks only for the necessary choice or credential. Record inputs, clicks, visible choices, and actual result. |
| Add files at kickoff or in Watch | One obvious add/drop area and one file list. Completed transfers become normal file rows; duplicate progress rows and repeated internal explanations disappear. Folder hierarchy, exact bytes, retry, and authorization remain correct. |
| Browse and organize | File/folder names are the direct targets. Row actions are contextual; bulk controls appear with a selection. Moving files does not require knowledge of internal paths. Empty selection/trash sections do not consume the default view. |
| Recover from a failure or deletion | One clear status and useful recovery action preserve work. Recoverable deletion offers Undo; retry does not duplicate files, runs, or effects. A user upload is never presented as a worker-authored result. |
| Use advanced options | Context, tools, members, placement, access, limits, and diagnostics remain discoverable on demand, with exact saved-state readback. Hiding complexity never broadens authority or removes a required capability. |
| Use supported displays and input methods | Exercise the actual desktop and small-screen paths, light/dark states, keyboard/focus, touch, and accessibility. Visually inspect the rendered result; compare it to state/logs and exact artifact bytes. |

## Retained members and collaboration settings

The default workspace card keeps its normal task and delivery actions. Its **Work together**
action opens collaboration controls directly; Workspace settings also remains available.
The card does not expose provider or permission forms itself. A retained execution workspace can contain several named worker identities. This
extends the original one-worker workspace model without changing stored worker or protocol keys.

Settings show the current member roster and each selected member's actual lifecycle state.
Supported pause/resume and interrupt actions use the existing lifecycle routes. Interrupt
keeps the exact run fence: if a concurrent status write changes the worker snapshot,
Interrupt retries once for that same active run. If the run changes or another
control wins, the direct and signed workspace view routes return a typed `active_work_generation_changed`
conflict so the user can review current status and retry; it never signals a
replacement run by inference. Permanent close stays under More actions.
Provider/model readback stays under Advanced settings. The modal keeps
keyboard focus, closes with Escape, returns focus to its opener, and supports narrow screens.

Collaboration is collapsed in general settings and open when entered through Work together.
The entry focuses and scrolls to the controls, with Allowed AI folded. A policy save refreshes
the eligible roster without a separate reload click. Discovery and access are independent, initially off. Enabling discovery
alone shows eligible worker names without allowing messages. The owner-scoped catalog preselects
all current eligible workers when within the service limit; above the limit the owner must choose
an exact subset. Clear selection and Select all remain direct. Reloading a changed roster keeps the
previous exact selection, so newly added workers are not silently included. One explicit save
enables access in the selected workspaces and grants only those selected exact workers; two-way
messages are the default. Message direction and idle wake remain configurable. Wake is off by
default; a stopped recipient needs explicit wake permission before it can receive a new message.
Duration defaults
to Until revoked after that deliberate owner action; temporary duration is under an optional
disclosure. Future workers need their own permission. Discovery stays unchanged. Unchanged policy
saves keep permission; actual policy changes invalidate previous grants. Each directed grant has
an explicit revoke action. Revocation blocks new use and redacts replayed message content, but
cannot remove content that a worker already received. Queue and invocation receipts do not claim
the recipient model read or acted on the message.
The owner MCP can read the policy; changing discovery or access requires the signed-in owner's
human-confirmed UI/API route. A native worker cannot widen its own permission.

Add worker is a direct action on a shared workspace card and its member's Watch header when the
owner can manage that workspace. One click opens the add form. The normal add action requires no
name or provider choice and uses the configured default. Optional native profile and account
choices are visible in that form; profiles include only those advertised as
registered, compatible with the workspace execution mode, and shared-workspace capable. Placement
belongs to the workspace. This panel does not silently convert an isolated workspace or create
shared metadata and call it a ready runtime. Uncertain creation responses require refreshing the
roster before retrying because the current member-create API has no idempotency contract.

Watch identifies each member by its name, native harness, and shared or separate workspace mode.
In a shared workspace, a directory scan cannot prove which member authored a file. The run result
therefore shows its own text or URL; Files lists the shared files without assigning them to that run.

Advanced settings exposes optional Context, Connected tools, and Background work controls through the
owner-only versioned worker configuration API. Defaults use all currently authorized context and
configured worker tool connections. The assistant’s own tools, plugins, and app-provided capabilities
stay available under their existing policy. Selected mode uses exact source/server IDs from the
runtime catalog; an empty tool selection excludes configured worker connections only. Limits set the
inline context budget and an additional concurrency ceiling beneath existing capacity. Turning
background work off blocks new admissions, not an invocation already running.

The connection catalog follows the member's native harness: Claude sees its project MCP entries,
Codex sees its native MCP entries, and Grok sees its explicit ACP entries plus compatible Claude
entries. A connection declared only for another harness is not offered as available to this member.
The catalog describes configured projection, not a successful live connection or model tool use;
the run receipt owns those observations. Other worker profiles cannot select these native
connections until their projection is supported.

Save includes the fetched revision. Concurrent edits preserve the visible draft and require an
explicit reload; unavailable selections have their own recovery message. Current configuration
shows requested/effective delivery and omissions, without claiming a native model read. Optional
source previews retrieve actual authorized text in bounded pages, show how much remains, and mark
completion only at the final page. A source change between pages or a revoked source clears the
preview and asks for refreshed access; text is rendered as text, never HTML.

The normal workspace card still requires no configuration. Browser fixtures prove rendering,
interaction, owner-facade behavior, configured API readback and persisted synthetic state only.
Integrated runtime and actual native worker turns remain separate acceptance gates.

## Conversation entry

`conversation.html` provides a single message composer using the configured assistant. Accepted
goals and results appear beside the conversation; no task schema, route, capacity or context form is
required to send the first message. The model may handle work directly or use available helpers.
If a send response fails after the server saved the turn, the composer reads back the exact saved
turn before reporting failure. A saved turn clears the draft and shows its current state; an
unconfirmed turn keeps its draft and idempotency key for a safe resend.
Guidance and Pause/Resume/Stop address exact work. A blocked admission leaves the original message
saved; Retry reuses that turn's idempotency key. A provider attempt that already ran instead offers
Send again as a new message. Run-scoped native permission requests appear beside the conversation,
with the exact requested command and one-time/decline options; lasting choices are under More
choices. The same controls remain in Watch. A runtime capacity block gives setup/retry guidance.
If shared workspace capacity, its resource probe, host capacity, or the selected account is briefly
unavailable before any provider request, the saved turn waits for bounded automatic retry and says
it will start automatically.
After that bound, or for missing configuration, the owner can use Retry.
If a native request expires, the saved conversation says so and offers Send again.
For a failed helper whose native response request expired, was declined, or was cancelled,
the owner may explicitly Retry that exact child. Its replacement stays bound to the same goal;
sibling results are preserved.
Foreground native requests remain visible beside the conversation even before the coordinator
accepts any goals. A result wake includes exact saved helper output within a bounded inline page;
longer output shows its total length, hash, and next offset for the result tool.
If an automatic helper-result handoff fails after its native attempt, show a plain failure with
one owner-scoped action to retry the exact saved result evidence in a new turn. Never present the
internal result JSON as a user draft or replay the failed provider request. Hide the action after
a later answer has already used those results.
Assistant Markdown is rendered locally with raw HTML disabled. Conversation-created internal policy
projects are marked by origin metadata and omitted from the project picker. Reload retains the
conversation and results through the owner-scoped API. Native file/work detail continues to use
the existing Watch surface.

Acceptance requires real browser create/message/result/control/reload checks, a plain first-use
review, mobile and keyboard checks, plus native same-account contention and independent-account
responsiveness. Static page rendering and source tests do not prove these user journeys.

The UI preserves draft IDs on reload when browser storage is available, retries incomplete uploads with the same ID, checks reselected bytes, and keeps failed cleanup visible. If storage is blocked or full, owner-scoped hints and retry keys stay in memory for the current page; uploads, attachment retries, and accepted-launch cleanup continue. This fallback does not survive reload. Failed storage removal cannot restore an old hint within the same page. A view-only Watch link can list and download current files but cannot mutate Files or inspect Trash. New Files proxy routes accept the normal session or a scoped worker cookie; query-string and Referer bearer tokens do not authenticate those routes. The live desktop drop overlay requires real browser/iframe drop QA; source handlers alone do not prove that path.

In the new-project Files section, each file has direct Replace and Remove actions, and Clear all files stays next to Add files while draft files exist. Replacing keeps the original until the new upload is ready and old-file removal is confirmed. Removal and clearing report the confirmed outcome; a failed cleanup stays listed with Retry remove. These actions change the draft only and do not alter an already accepted project's files.
Watch Files labels the standard `inputs` folder as Attached files and shows an accepted upload's
relative filename in place of its opaque input directory ID. File links, revisions, and immutable
workspace paths keep their exact stored identity. Duplicate copies an accepted-input name only
when the copied file still matches the accepted immutable bytes; edited copies keep their actual
file path without a false accepted-input label.

Files at project creation and in Watch accept dropped files as well as the Add files picker. Ready files have a direct Download action that works by keyboard and on mobile. Workspace file names can also offer a copy drag to the operating system only when the server advertises that browser/platform target; the transferred value is a same-origin, revision-bound content URL and never carries a bearer token. Download remains visible when that native drag path is unavailable.

Watch ZIP downloads submit selected file IDs in a native POST form, so selection size does not grow the URL. The UI forwards that selection to the existing runtime POST export as JSON. This operation only reads files: normal sessions retain CSRF checks, and worker-view cookies require their matching workspace and an allowed Origin. No mutation scope is granted for an export.

The old operator `/api/launch` inline-base64 `files` transport is retired. Non-empty legacy payloads receive 422 with `legacy_upload_transport` before any workspace change. Clients must create streamed draft receipts and send `file_upload_ids`; the browser already uses this contract. Runtime bootstrap compatibility is separate and remains subject to the native storage policy.

Files downloads bind the selected revision and abort if the open file changes during transfer. Same-name uploads retain both versions, including names at the filesystem UTF-8 byte limit. Native quota or disk-full errors leave a visible retry state, release the failed transfer reservation after cleanup, and preserve the same upload ID for retry.


## Run and Watch status readback

Run shows the worker and its account beside the workspace choice before Advanced settings. A
missing personal account has a direct Connections action; a saved workspace shows its saved account
status. Only the selected worker's effort appears in More settings. The summary uses the advertised
profile label and typed account/readiness data, refreshes on account, policy, workspace and
connection changes, and identifies copy reapproval. An unavailable account catalog is not an empty
or ready account. A chosen account stays selected through readiness or catalog refreshes; strict
launch stops until that account is ready or the user chooses another. When several accounts are
ready but none is the default, the composer asks the user to choose one. Shared selection chooses
managed placement and states its Linux host requirement;
typed launch failures give a plain recovery action. Saved default-worker preferences remain editable
and persistent.
The workspace catalog initially includes saved, one-off and legacy workspaces; its filter remains
available.

Watch derives status from run and worker lifecycle fields, never instruction prefixes or console
phrasing. A ready worker is not a completed run. Human status and completed result text are separate
from technical output; technical details start collapsed and preserve diagnostics on demand.
If the Watch session expires, the status distinguishes sign-in from a denied workspace and gives a
direct Sign in again link that returns to the current Watch URL. A denied workspace does not offer
that link as an access workaround.
Accepted guidance does not claim the worker has already redirected. Failed or uncertain actions
keep the draft and show an action notice until dismissed or another action succeeds; polling does
not silently erase it. A temporary polling error clears after the next successful poll.
Existing Files, artifacts and lifecycle controls remain available.

The main composer does not depend on backdrop blur for first paint. Floating Files, result and
context-menu surfaces use an opaque background so underlying text cannot bleed through. Browser
acceptance includes first paint, narrow-screen legibility, account changes, failure/recovery,
result/detail disclosure and reload; source tests do not substitute for those checks.
