# xPerfect: Minimal Unified Operator UI

## Purpose

Define a separate, minimal, modern operator UI for xPerfect that does not alter the existing runtime UI and does not mix frontend concerns into xPerfect core runtime code.

## Visual Thesis

Use the established [xPerfect website](https://www.xperfect.ai/) identity: graphite, pearl and
restrained taupe, with a quiet gradient behind readable working surfaces. Light and dark use
separate surface, text, border and semantic state colours. Keep the same visual language across
Run Project, Chat, Workspaces, Connections, Library, Schedules, Activity, sign-in and Watch chrome.
The native desktop and terminal retain their own appearance.

### Shared appearance and navigation

`frontends/glass-drive-ui/src/glass_drive_ui/static/theme.css` owns shared colours, atmosphere,
application header and appearance controls. Existing page styles own their layouts and consume
these tokens; they must not introduce another page-specific palette. Keep error, warning, pending,
unknown and success states readable in both themes, including text on translucent surfaces.

**Run Project** is the first navigation item and the default entry. **Chat** is second, followed by
Workspaces, Connections, Library, Schedules and Activity. Navigation uses native links so reload,
new-tab and browser history retain the selected destination. Chat is the visible label; existing
conversation URLs, APIs, state identities and behaviour remain unchanged.

The compact **Light / Auto / Dark** control uses labelled native radio buttons with sun, device and
moon symbols. Auto follows the operating system; Light and Dark override it. The selected preference
persists in browser-local storage, applies before styles paint, and synchronizes across tabs.
Clearing storage or an unknown saved value restores Auto. If storage is blocked, the choice still
works for the current page. Native Tab, arrow-key and Space behaviour and visible segment focus
remain available; no custom keyboard router or account/runtime setting is required.

Navigation uses a segmented menu with one raised selected surface. The signed-in account appears
as an avatar button at the top right of the same header. Its menu contains the user's name,
Light / Auto / Dark control and existing switch-account and sign-out actions. Account appearance
is a preference, not the primary task action. Keep its native radio behaviour inside the menu.
The menu must support keyboard access, visible focus, Escape, outside-click close and returning
focus to its trigger. Signed-out and loading states must be clear without moving another action.

The main shell has a common 1360px maximum width with responsive gutters. A wider workspace body
may retain its existing cap, while the shared header, brand mark, navigation and account anchor
stay aligned with the other pages. Keep the same header dimensions and stable interaction targets
across Run Project, Chat and the hash views. On narrow screens, wrap deliberately without hiding
navigation or creating horizontal page overflow. Account loading must not move a pending navigation
tap onto another action. Preserve accessible names and tooltips when a control uses icons.

The monochrome **Forward pair** symbol and favicon reuse the first-party mark selected for the
[current website](https://www.xperfect.ai/brand/xperfect-forward-pair.svg). The local
`brand-symbol.svg` and `favicon.svg` retain that vector geometry. The website's earlier P mark and
the former orange GlassHive icon are superseded. Keep first-party asset provenance public-safe;
do not copy private brand research or user prompts into this repository.

## Content Plan

1. Entry composer
2. Automatic transition into live watch mode
3. Minimal ribbon controls
4. Hidden advanced controls only when needed

## Interaction Thesis

- packaged onboarding presents one xPerfect start command, then the app and account
  connection; internal service and worker images are installation details, not
  separate products, required choices, or setup lessons

- the centered project composer should feel like the only thing the user needs to understand
- after submit, the interface should hand off immediately into live watch mode
- advanced controls stay tucked away until the user explicitly asks for them
- the default watch handoff should land on the live desktop, not the raw terminal page
- distinguish the named AI `Worker`, its `Worker type` and its `Workspace`; use `sandbox` only where an execution-mode detail is needed
- apply the [first-use simplicity principle](01_Vision_Requirements_and_Terminology.md#first-use-simplicity):
  use configured defaults, remove unnecessary required inputs and choices, and reveal advanced controls on demand
- Schedules open to saved definitions or a clear empty state. Creating or editing opens a compact
  form with Once, Every day, Every week, and Custom choices. The workspace list includes one-off
  runs; creating their first schedule keeps the chosen run as a saved workspace in the same action.
  When the AI-workspace target has no workspace, show Start project and Cancel;
  Start project returns to the visible goal field, not Advanced. The webhook target remains usable
  without any project, workspace, model or provider account.
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
  Run now does not consume a future one-time clock. Pausing after a manual run still offers Resume;
  only the clock's actual one-time occurrence can finish it. Use the authoritative `clock_finished`
  field, not the latest manual result, for that distinction.

## Schedule an AI workspace or an application webhook

The **Schedules → New schedule** editor asks three plain questions:

- **What?** Choose **Run a task** or **Send to an app**, then enter the task or an optional message.
- **Where?** Choose a named workspace or connected app. Existing apps need no connection setup again.
- **When?** Choose one time, every day, every week or an optional custom rule. A short preview repeats
  the friendly date/time, city and selected place before saving. **Tomorrow** updates that preview.
  The detected city is shown by default. **Change** opens city search and selection; direct time-zone
  identifiers remain optional.

The ordinary flow requires no JSON, auth headers, signing configuration or reliability-policy
choices. Both targets retain the same saved list, edit, pause/resume, Run now, history and removal.
An app schedule does not create a project, workspace or AI account. With no workspace, the task
choice shows **Start project** and **Cancel**; unrelated time/reliability fields stay hidden. Opening
the form focuses its visible selected choice. Reloading a named surface restores it through the
existing navigation registry before waiting for network startup.

**Connect an app** opens a compact native dialog. Its basic fields are **App name** and **App address**.
Existing signing/auth/payload options stay under **Advanced connection settings**. A generated key is
shown on the successful creation receipt for the receiver's one-time setup. Closing the dialog,
pressing Escape, resetting the form or changing owners clears private key/header/address fields.
The dialog uses native keyboard focus and background isolation; visible labels identify its fields.
**Manage connection** renames, disables/re-enables or rotates credentials for the selected app. Its
saved address and credentials are never read back. A new address requires a new connection.

A saved app schedule with an instruction shows and edits that instruction in its plain task field.
Otherwise, its plain message maps to request data’s `message` string. A new app schedule never
inherits hidden text from the task-workspace choice. **Extra app details** holds
optional typed fields and a further explicit JSON editor for complex data. Editing a plain message
changes only that field; existing request references, arrays, nested data and other fields remain
intact. A custom non-string message disables the plain message field and explains where to edit it.
Existing app data keys are read-only in the field list; deliberate changes use **Edit as JSON**.
Very large JSON integers remain JSON-only and keep their exact decimal digits through the current
browser’s native source-aware JSON parser and raw-JSON serializer. Typed number fields reject
unsafe integers rather than rounding them. No app context is summarized or reconstructed. Invalid numbers, duplicate
field names or invalid JSON produce a visible error rather than silently sending an older value.

Repeating an interrupted creation keeps the same key for the exact unchanged request. Changing the
request uses a new key. Run-now recovery retains its separate existing same-owner receipt. Failed
refreshes preserve the saved list; unavailable app choices show Retry. Save, Run now and history
states use plain text and distinguish uncertainty from an acknowledged request. A save leaves a
visible page/card receipt and scrolls to the exact saved definition. Run now checks only the saved request until its delivery settles or its saved delivery window ends.
Checks use capped backoff and stop when the view is hidden or its owner changes. While an app
delivery is pending, Run now is disabled; a lost status read does not permit a duplicate. The
card and history use the receipt state, with error codes kept as details. An unknown terminal
receipt warns that sending again may repeat the action. The original recovery receipt is retained. A completed
one-time schedule says Done/Sent and offers no misleading Resume action. Known ACKs win; an
unacknowledged send remains visibly uncertain even when later cancelled or rejected. A generated
signing key has a **Copy key** action. Known field errors use a plain explanation, error colour and
focus on the field; successful receipts use a distinct success tone.

Schedules use the same shared appearance and compact visual hierarchy, with readable spacing,
visible focus and responsive controls down to a 320px viewport. Its simple What / Where / When
flow and progressively disclosed technical fields remain unchanged by appearance settings. This is an existing
vanilla frontend and native dialog, informed by [shadcn field grouping](https://ui.shadcn.com/docs/components/base/field),
[dialog guidance](https://ui.shadcn.com/docs/components/base/dialog) and [progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/);
it does not install or claim a React/shadcn component migration.

History shows the receiver's numeric HTTP status and optional **Delivery times**. These times keep
clock detection, sender queue, signing, connection, sending and response waits distinct; the HTTP
attempt total is inclusive and must not be added to its component times. Raw response bodies stay
out of the UI. **Received** means the app acknowledged delivery; its application task can continue in the background. A cancelled or superseded schedule cannot retract an already sent request; known ACKs and
unacknowledged attempts stay truthful. See the [runtime state definitions](../runtime_phase1/README.md#delivery-states-and-recovery).

UI source/BFF tests cover structured requests and private error handling. Real browser create,
edit, pause/resume, Run now, history and reload acceptance belongs to the completed running candidate;
those tests are not replaced by source checks.

## Watch clarity and spacing

Watch is a working view for ordinary users, not a diagnostics dashboard. Its compact header keeps
one direct Workspaces action, the named worker and its type, a truthful status, Files and a labelled
More button. A separate Latest workspace output row gives a clear result/status action. Use the
shared palette and small consistent icons with visible labels. A live native desktop or terminal
keeps its own appearance; the surrounding application and settled answer use the shared surfaces.

More uses compact left-aligned menu rows grouped into View, Open in workspace and Workspace,
with separated destructive close styling. Native desktop launch actions appear only where the runtime reports a desktop and management
access; host-only workers do not offer desktop no-ops. Show the worker's own declared native app,
not another provider app bound to no account. Read-only and closed views keep observation and saved
results without offering management actions. Retain the original server authorization; presentation
is not a new permission grant. Close opens a native confirmation with Keep workspace open focused;
only explicit confirmation sends the existing close action. Cancel/Escape leave the workspace
unchanged, duplicate confirmation is blocked, and an already-dispatched close cannot be cancelled
through the dialog. Menu focus supports arrows, Home/End, Escape with
return to its trigger, Tab exit and outside-click dismissal. Its position follows the real header
height and its scrollable bounds fit a narrow or short viewport.

The latest job's recorded start/end times can show Run time; active work shows Running for and a
queued job shows Waiting from its queue timestamp. These are elapsed wall-clock times, not model
inference, an estimate, a completion percentage or a promise of speed. Missing or invalid timestamps
hide the measurement. Use the existing live refresh; do not add another clock, API or poller.

A settled result has natural content height and presents its readable answer before historical
workspace files. Current declared file actions remain prominent. The optional Workspace files
section is collapsed by default and labels its bounded available inventory honestly; it is not a
count of all files or a claim that every file belongs to the latest reply. The Files drawer remains
a direct browse/upload path. Native requests stay above the file inventory; Technical details and
the unchanged native Technical log remain secondary. Ended turns hide empty native-control chrome
after its existing state controller clears the old target; pending and unknown active turns keep
their existing native request and draft behavior. Preserve current-result identity, signed and
read-only access, failed/unknown state distinctions, draft recovery and native turn controls.

The shared theme owns the Run Project title/form width, centring, card padding and spacing. Page
styles must not override it with older layout values. Chat keeps its readable column and compact
fixed empty-state spacing. Empty status regions stay mounted but reserve no blank paragraph space;
a real status or error remains visible. Browser QA checks the actual affected pages, results,
Files and menu keyboard/focus flows in light/dark, wide/narrow and short layouts.

Native `artifact_sha256:` references are file-transport identities, not browser URLs. The shared
result renderer used by Watch and workspace summaries renders unresolved references as plain
labels, including image labels. It must not guess a digest-to-file binding from a filename, alter
the native answer, or send the identifier to a browser route. Existing declared Open/Download
artifact actions remain the authoritative one-click delivery paths. Normal safe Markdown links,
formatting and literal code examples retain their behavior; raw HTML and unsafe schemes stay blocked.

These patterns follow the current [shadcn menu guidance](https://ui.shadcn.com/docs/components/aria/dropdown-menu)
and [WAI menu-button pattern](https://www.w3.org/WAI/ARIA/apg/patterns/menu-button/), adapted to the
existing native HTML/vanilla frontend. No frontend framework or harness replacement is required.

## Hard Requirements

1. Do not edit or replace the existing xPerfect runtime UI.
2. Do not mix this frontend into xPerfect core runtime code.
3. The new UI must live in a separate service/surface.
4. The entry screen must present one centered project definition box.
5. **What's the Job?** is the required project input, with the explanation
   **Explain the work/project for this specialized AI Worker**. Keep the original goal unchanged.
6. **What's the required outcome?** maps to the existing optional success criteria. An omitted value
   stays omitted; the host must not invent, copy or silently inject a replacement rubric.
7. **Required Knowledge** groups the existing **Background Context** text and Files controls in one
   onboarding card. Explain: **What background context, files, material does this AI Worker need to
   get itself fully onboarded?** Retain the existing upload, drop, replace, remove, folder, revision
   and draft-preservation behaviour. Existing native setup and approved Library references remain
   the knowledge mechanisms; this grouping does not create a new catalog. Background and files are
   optional, and technical settings remain progressively disclosed.
8. **Who's Doing The Job?** offers **Existing** or **New**:
   - A saved worker or shared-workspace choice outside the first page is checked by its exact
     owner-scoped ID before restore; unavailable choices stay visible and block launch until
     explicitly changed. Search and Load more use the existing paged catalog. Existing shows them
     beside its chooser; New keeps them inside the optional Copy disclosure, available before
     selecting a worker to copy. The ordinary new-worker form has no previous-worker search box.
   - Existing choices show the safe name, worker type, account label and workspace label. Reuse keeps
     those exact stored choices rather than applying New-worker settings to the saved worker.
   - New offers an optional name, worker type, account and workspace, with **Copy existing** using
     the existing safe-copy or template mechanism. Blank name preserves the current automatic name.
     Naming keeps the worker for reuse. Unnamed one-off workers are marked as such; explicit reuse
     promotes them to saved workers before the next run.
     Native types known to be absent on the selected computer stay visible as **Not installed on
     this computer** and cannot start a project. This uses the runtime's existing binary-availability
     catalog, without launching a CLI. A saved unavailable selection is kept until explicitly changed;
     unknown availability stays subject to native admission. A missing host binary does not disable
     a supported sandboxed route. Host account labels remain local on local deployments; organization
     account wording is reserved for managed deployments.
   - Copy creates a new identity; it does not promise a native conversation/session fork, copy
     credentials or browser logins, or bypass capability reapproval.
     It copies files and project notes, type, placement and the eligible same-owner AI account
     reference. Learned native chat history is not copied. The form makes retained settings read-only.
     8a. **Where should I work?** offers an existing or new workspace using the existing supported runtime
     paths. A new member can join an eligible shared workspace. An existing isolated workspace keeps
     its one-worker contract: offer its current worker or a safe copy instead of silently moving an
     identity or changing isolation. Shared/separate mode and host/sandbox placement remain distinct
     advanced choices, with truthful platform and capability limits.
9. On submit:
   - create or reuse the selected workspace's underlying worker
   - faithfully send the goal, supplied criteria/context/files, and authorized capabilities
   - redirect automatically to the live watch screen
     An uncertain immediate Existing or Copy request can retry the same native request key; it must
     not start another assignment or duplicate workspace. New-worker, new-member and scheduled
     creation retain the uncertain receipt until the user explicitly chooses **Start again**, with a
     plain warning that another job may start. Preserve the exact draft, files and account, and fence
     recovery by owner and payload. Persist uncertainty before dispatch so reload cannot turn an
     unconfirmed create into a blind replay. A confirmed result stays successful if a later catalog
     refresh or optional browser storage hint fails.

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
    A settled terminal-only workspace opens its readable result in the main view. The same native
    session remains available in a collapsed **Technical log**. Live work and pending native input
    retain the native interaction surface; a new run restores it. Users can reopen the log without
    polling closing it again. No native-output renderer or replacement harness is introduced.
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

A selected Docker account starts in a private execution-workspace member. Its prepared workspace path stays under the provisioned owner root across a continued conversation and fresh recovery members. A server-side `custom` or `life` workspace path is rejected for that route; files enter through the authorized Files attachment flow. 16. If project launch fails after a worker was created, the UI/runtime must record that launch as an explicit failure instead of leaving a healthy-looking but runless worker behind. 17. The main controls must be simple and obvious:

- pause
- resume/play
- interrupt
- more menu
- delete/shut down available from more menu

18. The latest result must be visible in the top ribbon and expandable without leaving the live view.
    In an isolated workspace, automatic file previews use the current run's recorded start/end
    window. Older files remain available in Files and the output's workspace-file list, but a
    text-only follow-up must not claim an older report as its new result. Newly created files
    with preserved modification dates are recognized through filesystem change time. Shared
    workspaces still require an explicit native selection; timestamps do not prove which member
    authored a shared file. When the runtime explicitly has no current file result, the UI must
    not replace it with the first historical file.
    Library setup and workspace catalog links use the same configured initial Watch surface as
    Run Project. A terminal-first deployment must not be redirected to an unavailable desktop.
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
    Keep each sent message's acceptance promise under its exact account and worker identity across
    tile refreshes. App-owned full-page navigation waits for those HTTP receipts, never for worker
    completion: Open workspace, Set up tools, normal preview links, Chat/home links, account handoff
    and automatic launch navigation must not cancel an input still being sent. An accepted steer
    already owns its startup, so opening that same worker must not add a redundant Resume request.
    Pending sends cannot be duplicated by a refreshed tile. A failed send keeps its draft and actual
    cause on the current page. Edited follow-up drafts and a changed account cannot be overwritten by
    an older reply. Hash tabs, modified clicks, downloads and new-tab links keep native behavior.
20. A non-technical user should be able to understand the flow with near-zero explanation.
21. `Steer + send` must behave like a real redirect, not a passive note:

- interrupt the active run
- stop the exact live run session and its process tree
- queue the replacement steer run automatically
- keep the replacement steer run in execution mode until the requested action is actually performed or a blocker is raised
- if the exact source finishes or fails after the correction was durably accepted, keep its
  terminal result and execute that same reserved correction after release proof; never treat
  ordinary completion as cancellation of the new instruction
- preserve exact operation/run/mission ownership, Stop and closure fences during recovery
  and receipt replay; an unapplied control must be shown truthfully and keep the draft

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

## Account connection without a detour

When Run Project needs an account, **Connect an account** carries the selected worker type and
compatible provider intent into the existing connection flow. Preserve the unsent job, context,
file receipts and worker/workspace choices, then return to them after connection. Do not silently
change the selected type, model, account policy or saved worker to make setup pass.

Show concise provider-branded choices using official current marks with source/usage provenance.
Keep availability and unsupported-method explanations visible; empty method lists are not an empty
account catalog. Use the existing native sign-in, existing local-account attachment and API-key
setup mechanisms. Hide technical details until useful. Loading an unrelated workspace, Library or
schedule catalog must not prevent available account controls from rendering or opening sign-in.
Cancel, expiry, verification failure, disconnect and retry each keep a clear next action. Never
expose credentials in page markup, errors, logs, history, public docs or reviewer material.

## User-Facing Workspace Model

- In the user-facing xPerfect UI, a persistent personal execution environment is called a
  `Workspace`.
- A named `Worker` is the reusable AI identity. `Worker type` selects its registered native harness
  or direct adapter. Its selected AI account is distinct from the signed-in operator account.
- A `Workspace` is the persistent execution place: files, compute and supported work surfaces.
  Isolated workspaces have one worker; supported shared workspaces have separate member identities
  and native account homes. Existing owner/project and platform admission remain authoritative.
- Reopening uses the same worker and retained state. Safe duplication creates a new identity with
  permitted files/context and clean authentication state. It is not an unproved native session fork.
- Internal worker, sandbox, project, workspace and conversation API identities remain unchanged;
  clearer labels do not require a data migration or a new worker registry.

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

`Open workspace` automatically resumes an eligible paused workspace once its current job status
is known. Non-technical users should not need to choose between `open` and `resume`. When the
catalog has a prior run ID without its current state, Open goes to the workspace view while the
existing live read resolves it; it must not blindly restart compute. Failed, cancelled and
interrupted jobs require a corrected follow-up rather than automatic Resume.

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

## Operator Brief

The launch brief is the user's own input, in the order entered:

- the goal, verbatim
- `Success criteria:` followed by the user's text, only when supplied
- `Background:` followed by the user's text, only when supplied

The brief adds no rules. Authority comes from the workspace's typed access mode. The runtime worker
contract supplies the safety checkpoint, proportional verification and the `FINAL REPORT:` protocol.
The output format is the one the user asked for, or the worker's own choice. The initial Watch surface
is the user's explicit choice or the configured default (`GLASSHIVE_DEFAULT_LAUNCH_SURFACE`, desktop
unless set); task text never selects it.

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
15. `Open workspace` is the single reuse verb and automatically resumes eligible paused workspaces after the current job state is known.
16. `Duplicate workspace` creates a new workspace with copied files/context but a clean browser-session state.
17. Workspaces exposes a delivery without opening Watch / Steer or changing lifecycle state.
18. The overview bounds live previews and compact polling as the catalog grows; no offscreen tile
    captures keyboard/mouse input or creates an unbounded desktop connection.

## Current Phase-1 Clipboard Note

The sandbox desktop currently relies on noVNC. Clipboard support is available through the noVNC clipboard controls and browser clipboard permissions, but truly seamless automatic browser-clipboard synchronization is still constrained by browser security rules and is not yet elevated into a custom first-class xPerfect clipboard bridge.

## First-use acceptance

These are required interaction outcomes, not a claim that the current build passes them.

| User action                              | Expected experience and evidence                                                                                                                                                                                                        |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Start with only a goal                   | Configured defaults work without opening Advanced or requiring an invented criterion. Missing connection setup asks only for the necessary choice or credential. Record inputs, clicks, visible choices, and actual result.             |
| Add files at kickoff or in Watch         | One obvious add/drop area and one file list. Completed transfers become normal file rows; duplicate progress rows and repeated internal explanations disappear. Folder hierarchy, exact bytes, retry, and authorization remain correct. |
| Browse and organize                      | File/folder names are the direct targets. Row actions are contextual; bulk controls appear with a selection. Moving files does not require knowledge of internal paths. Empty selection/trash sections do not consume the default view. |
| Recover from a failure or deletion       | One clear status and useful recovery action preserve work. Recoverable deletion offers Undo; retry does not duplicate files, runs, or effects. A user upload is never presented as a worker-authored result.                            |
| Use advanced options                     | Context, tools, members, placement, access, limits, and diagnostics remain discoverable on demand, with exact saved-state readback. Hiding complexity never broadens authority or removes a required capability.                        |
| Use supported displays and input methods | Exercise the actual desktop and small-screen paths, light/dark states, keyboard/focus, touch, and accessibility. Visually inspect the rendered result; compare it to state/logs and exact artifact bytes.                               |

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

## Chat entry

The second navigation item, **Chat**, opens `conversation.html`, which provides a single message composer using the configured assistant. Accepted
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
Docker admission uses the actual host and sandbox resource probes. The configured worker image
must be prepared through the existing image-preparation owner before worker traffic; an unavailable
probe remains unavailable rather than assumed capacity. Docker Desktop's disk-budget fallback
reads the current settings store before the legacy settings file. Both `DiskSizeMiB` and
`diskSizeMiB`, including wrapped positive integer values, are supported. Invalid or conflicting
values fail closed, and an existing current store cannot be replaced by stale legacy settings.
On POSIX hosts, each bounded Docker CLI command owns its own process session. On timeout, the runtime reaps that
command and its CLI-plugin children, with bounded pipe cleanup, while preserving the existing
timeout and unavailable result. It does not signal the stack, engine, containers or unrelated
processes. Repeated readiness checks must not accumulate orphaned plugin discovery processes.
Timeout cleanup requires the runtime to remain alive until that deadline. Viventium receipt-authorized
stack Stop also captures and rechecks its exact Docker command descendants before stopping the runtime.
Private candidates without kernel-image proof remain untouched; original authorized stack seeds
retain their existing UID/group/start authority. Child creation after the Stop snapshot or abrupt
parent death before cleanup can still leave a separate CLI session. There is no parent-death
supervisor or broad process cleanup authority.
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

Unlimited logical-storage admission does not scan retained work for an unused total. Upload reservation, transfer checks, and accepted-input copy reservation still validate the storage adapter and owner policy. Finite logical limits and native hard quotas retain their existing accounting and guards. The storage display always computes actual usage; canonical root ancestry prevents double counting without comparing every prior workspace.

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
Watch and workspace cards show the same plain cause when the runtime has a typed failure. A
provider-reported model/account mismatch explains that the selected model is unavailable for that
account; it never silently changes the model. Unclassified failures keep a neutral status, with raw
native errors available only in technical details.
If the Watch session expires, the status distinguishes sign-in from a denied workspace and gives a
direct Sign in again link that returns to the current Watch URL. A denied workspace does not offer
that link as an access workaround.
Native turn controls use the current run’s declared action capabilities, never its provider name.
Watch and Workspaces show the same existing Guide or respond panel for supported owner actions;
signed and read-only views cannot submit them. Send to active turn preserves that native turn.
The normal Send action redirects work; Queue keeps a later follow-up.
The shared native panel uses the same theme controls in Chat, Watch and Workspaces; its workspace
More panel occupies its own row. A queued native guidance receipt clears only the unchanged text
that was sent. Edits made while waiting, rejected sends and uncertain receipts keep the draft.
An unconfirmed native guidance receipt disables repeat guidance for that exact run/attempt,
including across temporary unavailable reads. A different confirmed target releases that guard;
Cancel and native permission responses retain their separate capabilities. Old-target replies do
not clear a new target’s draft or status.
An unavailable or missing-ID native state object is not an empty request list. Keep same-target
question forms and partly typed answers, with actions disabled, until an authoritative snapshot
changes the requests, a valid new target arrives, or the live API reports `null`. That `null` means
there is no active native turn: clear its question forms, response-needed headings and notices,
disable its actions, and retain unsent freeform guidance. Recovered same-turn reads clear transient
availability/read notices; accepted action receipts and real action errors keep their own notice
until that turn ends.
Watch uses capitalized Failed, Interrupted and Cancelled labels, with error or attention colors
from the shared theme rather than a green failure state.
Accepted guidance does not claim the worker has already redirected. Failed or uncertain actions
keep the draft and show an action notice until dismissed or another action succeeds; polling does
not silently erase it. A temporary polling error clears after the next successful poll.
Existing Files, artifacts and lifecycle controls remain available.

The main composer does not depend on backdrop blur for first paint. Floating Files, result and
context-menu surfaces use an opaque background so underlying text cannot bleed through. Browser
acceptance includes first paint, narrow-screen legibility, account changes, failure/recovery,
result/detail disclosure and reload; source tests do not substitute for those checks.

An interrupted work-control HTTP request can be checked through the existing read-only
`/v1/work/{work_ref}/actions/receipt` route with its unchanged exact request body and idempotency key.
The owner/work/action/body digest must match. A completed or failed response is replayed unchanged.
A pending response requires the exact current executor's bound lifecycle claim; a reservation alone
is insufficient. `confirmationPending: true` means the native control is in progress, not applied.
The lookup cannot reserve, take over, execute or retry an effect.

Immutable failed receipt replay adds the trusted `X-xPerfect-Action-Receipt: failed-v1` response
marker after exact owner/body/key lookup, while preserving its original status, JSON and headers.
Ordinary authentication, rate, server and receipt-read errors do not carry this marker and cannot
be treated as proof that the original control failed. A client deadline never authorizes a repeat.
