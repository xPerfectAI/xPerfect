# xPerfect: Minimal Unified Operator UI

## Purpose

Define a separate, minimal, modern operator UI for xPerfect that does not alter the existing runtime UI and does not mix frontend concerns into xPerfect core runtime code.

## Visual Thesis

Use the established [xPerfect website](https://www.xperfect.ai/) identity: graphite, pearl and
restrained taupe, with a quiet gradient behind readable working surfaces. Light and dark use
separate surface, text, border and semantic state colours. Keep the same visual language across
Run Project, Chat, Workspaces, Connections, Library, Schedules, Activity, sign-in and Watch chrome.
The native desktop and terminal retain their own appearance.

### UI design principles

Use the visual roles below as the design baseline. Extend the current layout and palette when a
clear user need requires a change; keep the shared shell and control behavior consistent.

- **Explain the work.** Use human job language and concrete action verbs. A function or profession
  label needs one short explanation of the work it can do and the useful outcome. Keep names,
  functions and execution locations distinct. The normal path should make sense without a manual
  or knowledge of harnesses and protocols. Reveal technical detail when it helps a decision.
- **Keep the path short.** Make the next useful action clear, use safe configured defaults, and
  reveal advanced choices when needed. Judge necessary clicks, required decisions and time to
  useful value on the actual journey. Preserve drafts and selections through setup and recovery.
- **Keep meaning truthful.** Distinguish saved setup, active work and delivered results. A display
  treatment must not change accounts, models, permissions, exact saved versions or workspace
  identity. Keep unfamiliar states readable and give failures a specific recovery action.
- **Use the existing system.** Reuse shared tokens, native controls and the current lightweight
  frontend. Fix the owning rule instead of adding another override. Add a library or replace a
  framework only when an evidenced requirement outweighs its cost. Reserve image dimensions,
  bound previews and polling, and keep decoration from delaying input or useful content.
- **Research the decision.** For a new pattern or material redesign, compare relevant current
  primary guidance and credible alternatives. Keep a dated rationale and its limits with the
  owning change. Treat community opinions as hypotheses to check. Reuse valid research; a small
  repair does not require a new survey. [WCAG 2.2](https://www.w3.org/TR/WCAG22/) and
  [Web Vitals](https://web.dev/articles/vitals) provide accessibility and performance references,
  not a substitute for understanding the product's users.
- **Verify the real result.** Within the agreed QA scope, use the affected browser journey and
  inspect both themes, relevant wide/narrow layouts, keyboard focus, menus and dialogs. Check
  affected empty/error/recovery and reload behavior. Measure loading, response to input, layout
  movement and time to useful value when making performance claims. Source tests and screenshots
  alone do not prove an interaction. Follow the [shared UI acceptance cases](05_QA_Quick_Power_Playbook.md#9-shared-ui-and-library-acceptance)
  and reuse still-valid evidence.
- **Keep public guidance clean.** Publish reusable design rules, factual asset provenance and
  sanitized results. Keep raw requests, generation prompts, creative working records, screenshots
  and machine-specific evidence outside public source and history.

### Expert artwork style

**xPerfect Editorial Gouache v1** (`xperfect-editorial-gouache-v1`) is the approved artwork
direction: painterly editorial illustration with expressive gouache-like brushwork and bold
colour blocking. The [approved visual reference](assets/editorial-gouache-v1-reference.webp)
is an original generated illustration of a fictional AI persona. It does not depict an employee
or imply credentials or endorsement. The complete five-character bundled set uses this style;
runtime portraits are separately optimized assets verified in their actual cards. Future
replacements use normal asset delivery and affected UI checks. This baseline governs bundled expert artwork;
preserve user-supplied branding and identity rather than replacing it automatically.

Keep these properties consistent across subjects:

- Opaque painted colour planes, visible brush texture and subtle paper grain. Facial forms stay
  visibly illustrated at small sizes; avoid photographic rendering or a texture over a photo.
- A mature, expressive treatment with natural proportions and a clear silhouette. Avoid mascot
  proportions, toy-like gloss and unnecessary props.
- Graphite and plum, warm ivory and champagne-gold highlights, with controlled coral and muted
  teal geometric background shapes. These artwork colours do not replace semantic UI tokens.
- A square head-and-shoulders crop with clear margins, readable at 88 px and 80 px. Keep labels,
  capabilities and text in the UI rather than painting them into an asset.

Keep the subject description separate from the style specification. The reference governs medium,
texture, palette and composition; it does not prescribe the person's identity, hair, clothing,
pose, gaze or expression. Specify those choices separately for each subject.
Use a style reference and a subject reference for their distinct purposes when both are supplied.
Judge new assets against the reference in the actual card crops and both themes, then assess the
whole set: distinct people that look painted by the same hand. A style name
alone does not guarantee matching output. Record generation settings when exposed, keep working
prompts private, and introduce a new style version when the approved direction changes.

### Shared appearance and navigation

`frontends/glass-drive-ui/src/glass_drive_ui/static/theme.css` owns shared colours, atmosphere,
application header and appearance controls. Existing page styles own their layouts and consume
these tokens; they must not introduce another page-specific palette. Keep error, warning, pending,
unknown and success states readable in both themes, including text on translucent surfaces.

Keep the brand atmosphere around the work, while the reading and working planes stay neutral
and opaque. Light mode uses a white work plane with pale neutral grouped forms. Dark mode uses
separate graphite tones for the canvas, work plane, grouped forms and raised overlays; body text
is neutral rather than cream. Menus, dialogs and drawers consume `--overlay-surface`; an in-flow
result remains a reading plane. Use the same roles across pages instead of copying a marketing
page's tinted background into every work surface. The taupe accent and subtle warm/cool perimeter
glows remain part of the brand. These hue choices serve the product design, not a universal
claim about eye comfort or a rule that every background must be pure white or black.

| Surface role | Shared token | Light | Dark |
|---|---|---|---|
| Surrounding canvas | `--bg` | `#f4f5f7` | `#151618` |
| Work and reading plane | `--panel` | `#ffffff` | `#1c1e21` |
| Grouped forms and cards | `--panel-strong` | `#f7f8fa` | `#25282c` |
| Fields | `--control-bg` | `#ffffff` | `#202327` |
| Menus, dialogs and drawers | `--overlay-surface` | `#ffffff` | `#2d3137` |
| Main text | `--text` | `#17191e` | `#e8eaed` |
| Warm gold/taupe accent | `--accent` | `#786248` | `#d8c3a5` |

Selected segments keep their fill and raised treatment, with an inset accent keyline where a
subtle fill alone would be unclear. Keyboard focus has its own outer ring. Keep normal text,
helpers, placeholders and links at least 4.5:1 against their actual surfaces; necessary field,
selection and focus cues need at least 3:1. Measure alpha-composited tints as well as solid pairs.
Decorative dividers do not automatically need 3:1. See [text contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html),
[non-text contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html),
[semantic surface roles](https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale)
and [token pairs](https://ui.shadcn.com/docs/theming). Apply these principles to the existing
native frontend; they do not require installing another component framework.


The same owner defines page-title, section, label, helper, control-height, radius and content-width
roles. Main page titles use 32px, reducing to 28px on narrow screens; helpers use regular, readable
13px text. Standard controls use a 42px minimum height. Ordinary cards are flat, with restrained
frame depth and distinct overlay elevation. Control boundaries are stronger than decorative
dividers. Remove conflicting declarations in the page owner instead of appending another override
layer. A change to the shared roles must retain all native controls and actual state distinctions.
Run controls and workspace action chips use neutral secondary colours. Reserve semantic colours
for status, not a second filled primary beside Send. Chat's message field grows with its content
within a bounded part of the visible viewport, then scrolls; resize and editing a saved message
must retain the draft and leave the actions available. Guidance and native input forms retain
their own controls.
Before the first message, the Chat composer stays in normal flow so a long draft cannot cover
the project and assistant choices. Once messages exist it retains its sticky position. The
borderless message field uses one rounded focus indicator on its composer; other controls keep
their own focus indicators. Activity labels use consistent sentence case, including unfamiliar
event types, without changing the events or their order.

Native selects retain the browser picker and keyboard behavior. The shared theme owns one
inset chevron and enough room for the selected value; consumer styles use background-color so
they cannot erase it. Genuine content disclosures use a distinct rotating chevron, including
workspace settings and technical details. Action menus are explicit exceptions to that shared
content-disclosure rule. Menu triggers use an ellipsis or Add label, without a second native
triangle. Compact selects keep enough width and end padding for both the value and the indicator.
Ordinary fields keep a complete visible keyboard ring in both themes.

Library, Files, workspace, Schedule and Connections action menus reuse `action-menu.js`: native details keep open state,
native popovers escape scrolling/clipped panels, and placement fits the available viewport.
Escape closes only the menu and returns focus; outside clicks close it; pure action menus support
arrow, Home and End keys. Menus containing ordinary form controls retain normal Tab navigation.
Shared component styles belong to a stylesheet loaded by every consumer, including Chat.
Files row and toolbar menus use the same behavior; list-button styles must not turn their menu
items into stacked outlined buttons. After a row is refreshed, keep focus on the same item by its
stable identity. If an action removes it from the current view, focus a visible recovery or search
control. Never leave keyboard focus on the page body after a successful recoverable action.
Library picker actions stay outside the scrolling content. In a short viewport, the content may
scroll as one region so filters cannot reduce the item list to nothing or hide the final actions.
Read-only item details show the saved explanation first and put file identities in a disclosure.


Mutable frontend assets revalidate through the existing static-file service, retaining ETag,
Last-Modified, conditional requests, MIME types and path boundaries. Main and Chat documents remain
uncached. When rolling out from an older cache policy, update authored entry and transitive asset
references together; a shared module must resolve to the same URL from every importer. Do not
replace the native asset service or change pinned vendor releases to solve freshness.

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

The repeated-hour choice applies only to daily and cron schedules. One-time, elapsed-interval and
calendar-rule schedules do not offer a choice their recurrence engine ignores. Editing those saved
types keeps their stored policy unchanged; switching to daily or cron exposes a valid default.

**Connect an app** opens a compact native dialog. Its basic fields are **App name** and **App address**.
Existing signing/auth/payload options stay under **Advanced connection settings**. A generated key is
shown on the successful creation receipt for the receiver's one-time setup. Closing the dialog,
pressing Escape, resetting the form or changing owners clears private key/header/address fields.
The dialog uses native keyboard focus and background isolation; visible labels identify its fields.
Its header and action footer stay visible while the form fields scroll within the available height.
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
flow and progressively disclosed technical fields remain unchanged by appearance settings. Date/time and
weekday controls adapt to the editor's available width. Native checkboxes remain compact and aligned
with their labels; expanded schedule and app settings use explicit field groups with visible gaps. This is an existing
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

## Connected account clarity

Account rows use the existing first-party provider marks, the saved account name and plain account
method labels. Grok's company display name follows the current SpaceXSI brand; stored provider IDs,
account names, API hosts and routing retain their exact identity. Brand sources are the official
[company page](https://x.ai/company) and [brand guidelines](https://x.ai/legal/brand-guidelines);
the existing official Grok product mark remains distinct from a company wordmark.

**More** opens a compact anchored action menu instead of expanding or shifting the account row.
Only one account menu stays open. Outside click and Escape close it; Escape restores its trigger,
and arrows, Home/End and Tab retain normal menu navigation. Its surface fits the viewport and uses
the shared theme. Test/reconnect/remove retain their existing truthful outcomes and confirmation
boundaries. Account menus must not expose credentials or create another connection mechanism.

## Watch clarity and spacing

Watch is a working view for ordinary users, not a diagnostics dashboard. Its compact header keeps
one direct Workspaces action, the named worker and its type, a truthful status, Library and a labelled
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

The selected **Live activity** view remains visible in More, alongside a declared **Live desktop**
view, so users can discover their current view rather than infer why it disappeared. A worker that
has never started work does not offer a blank activity page in a new tab. Native application launch
actions retain their declared desktop and owner-authority requirements; do not show host commands
that merely acknowledge an action without opening the named application. Library retains the
universal file-browsing path.

Message hints float immediately above the composer on hover, keyboard focus or an active Queue
modifier, without reserving another permanent row. A real unavailable or closed-state notice
remains visible. Keep the same accessible description, exact Send/Queue behavior and failure
recovery. Long instructions can grow within a bounded part of the viewport without hiding the
workspace or sacrificing the native controls.

The latest job's recorded start/end times can show Run time; active work shows Running for and a
queued job shows Waiting from its queue timestamp. These are elapsed wall-clock times, not model
inference, an estimate, a completion percentage or a promise of speed. Missing or invalid timestamps
hide the measurement. Use the existing live refresh; do not add another clock, API or poller.

A settled result has natural content height and presents its readable answer before historical
workspace files. Current declared file actions remain prominent. The optional Workspace files
section is collapsed by default and labels its bounded available inventory honestly; it is not a
count of all files or a claim that every file belongs to the latest reply. On first use the Library drawer opens Files and retains the existing direct browse/upload path. Later opens remember the user's explicitly selected authorized tab. Native requests stay above the file inventory; Technical details and
the unchanged native Technical log remain secondary. Ended turns hide empty native-control chrome
after its existing state controller clears the old target; pending and unknown active turns keep
their existing native request and draft behavior. Preserve current-result identity, signed and
read-only access, failed/unknown state distinctions, draft recovery and native turn controls.

A file named in a worker's reply must open the authorized file rather than a native filesystem
path as a browser page. Resolve exact file references from that worker's current file metadata and
accepted workspace placement. Do not guess by basename, cross member boundaries, or bypass mission
completion evidence. Unknown native file paths remain readable text. Shared workspace files remain
labelled as available project files; linking one does not claim that the current worker authored it.
Host path aliases stay owner-only.

Steering must preserve a new instruction when the prior job finishes before control is claimed.
Keep the completed result and accept the instruction through the existing follow-up path. A newer
run or competing control remains a conflict; never interrupt different work or turn that conflict
into an unhandled server error. Read-only viewers retain only their explicitly granted controls.

The shared theme owns the Run Project title/form width, centring, card padding and spacing. Page
styles must not override it with older layout values. Chat keeps its readable column and compact
fixed empty-state spacing. Its title and New chat action share one heading row; the two existing
project/assistant selectors sit side by side on desktop and stack on narrow screens. Preserve their
labels, selected identities, IDs, draft behavior and the live Work sidebar. Empty status regions
stay mounted but reserve no blank paragraph space;
a real status or error remains visible. Browser QA checks the actual affected pages, results,
Files and menu keyboard/focus flows in light/dark, wide/narrow and short layouts.

The shared Library picker keeps its search, filters and actions at natural height while only its
item list scrolls. Empty search results must not stretch those controls. Cancel and Use selected
share one footer using their existing handlers. Narrow filters leave room for a readable item;
selectable cards account for the checkbox, icon and options button rather than forcing too many
columns into the drawer. Decorative icons may yield space to item text on small screens.
The Watch Library drawer uses the available phone viewport rather than leaving its list squeezed
between the worker header and composer. Desktop placement remains beside the existing work.
Closing an editor returns keyboard focus to the visible Add or item-options trigger, never a hidden menu
item. Keep the same exact-version references, draft, owner, read-only and busy-state controls.

Native `artifact_sha256:` references are file-transport identities, not browser URLs. The shared
result renderer used by Watch and workspace summaries renders unresolved references as plain
labels, including image labels. It must not guess a digest-to-file binding from a filename, alter
the native answer, or send the identifier to a browser route. Existing declared Open/Download
artifact actions remain the authoritative one-click delivery paths. Normal safe Markdown links,
formatting and literal code examples retain their behavior; raw HTML and unsafe schemes stay blocked.

These patterns follow the current [shadcn menu guidance](https://ui.shadcn.com/docs/components/aria/dropdown-menu)
and [WAI menu-button pattern](https://www.w3.org/WAI/ARIA/apg/patterns/menu-button/), adapted to the
existing native HTML/vanilla frontend. No frontend framework or harness replacement is required.

## One Library, across the app

**Library** is the reusable material view. It does not replace the native harness, upload transport,
workspace Files service, approved capability registry or native package manager. The main Library
page works without an AI account or a worker. Personal records are private to the signed-in owner;
the existing deployment-wide Approved catalog stays separate and sanitized.

Watch changes only the existing drawer's label and contents. Keep the current header, latest output
strip and Files component. On desktop its Library drawer starts at 840px, clamps to the available viewport and can be resized with the pointer or keyboard. The resize separator supports arrow keys, Shift for larger steps, Home/End and double-click reset. Width, the last explicitly chosen authorized tab and Library display filters are remembered in this browser; no account material is saved in those preferences. Phones use the available width without a narrow drag handle. Its tabs are:

- **Files** opens on first use; reopening can restore the last chosen tab when authorized. The existing browse, upload, folder, preview, download, replace, Trash and
  Undo controls keep their current authorization and behavior. Add files writes the workspace copy
  immediately, including during work. The native input list is prepared at the next start; this is
  not proof an already-running model read a newly added file.
- **Added here** lists exact item/version contributions for this worker. Use plain states such as
  In Files, Listed from next message, Needs your review, Needs native setup, and Not available for
  this worker type. A placed file or saved source must not claim Active in this session.
- **My Library** uses the same private list and picker as the main Library, Required Knowledge in
  Run Project, and the Chat composer. Approved items keep a visible badge and the existing review.

Only a signed-in manager for this exact workspace can see the two private Watch tabs or Keep for
reuse. Shared view links and read-only viewers retain their existing Files audience; they cannot
list or manage My Library, even if the browser has another signed-in account. A closed workspace
can keep Files read-only while its authorized manager saves an exact file privately. Reuse the
existing owner bootstrap check for that Library access; keep the upload draft disabled.
Account changes close private editors/pickers, clear private rows, and fence late responses. A
failed request keeps the draft and shows its real cause. Native dialogs retain focus, Escape and
keyboard access. Theme tokens, responsive layouts and progressively disclosed controls remain shared.

### Save once, reuse exact versions

**Add** offers Add files, Write a note, Add a skill folder, Add from source, and Create a bundle.
Ordinary saving needs a name and material, not JSON, a provider account, a workspace or a model call.
Existing Files upload receipts, hashes, limits and storage quotas own bytes. No second byte transport
or public personal catalog is introduced. **Keep for reuse** on a workspace file or a delivered file with an exact published file ID/revision captures that revision privately
without download/reupload. A path-only output cannot prove that the current workspace bytes still
match the delivered result. Its separate **Save current file…** action opens the existing Files
folder, clearly explains that the current version may differ, and lets the owner deliberately use
the ordinary file menu. Never substitute a newer same-path file for an exact delivered snapshot.

Edit loads the complete saved version and saves a new immutable version. Existing jobs, templates
and bundles retain their accepted versions. Versions lets the owner deliberately choose an earlier
version; latest is never silently substituted. Archive hides an item from ordinary choices and can
be reversed. Delete reads and shows the actual retaining work, bundles and templates before explicit owner
confirmation. It removes the personal item and its retention, while previously accepted references
and workspace copies retain their own lifecycle.

A **Bundle** is a flat set of exact item versions. Knowledge, notes, supported text skills, native
sources and approved references may coexist. A bundle adds no authority, nested workflow, new
installer or generated worker brief. A saved template retains the same exact references alongside
its existing safe-copy limits; it never copies credentials or learned native history.

### Apply and remove without false readiness

Selecting knowledge resolves into existing upload ID/revision pairs, once. A worker sees the exact
selected files, not the owner's whole Library. Native material is reviewed for the actual target. A saved-template Library Review action opens
that exact new worker’s Watch → Added here view. Its deep link first opens the existing Files
component to complete the real access/owner check, then selects Added here once authorized. It must
not depend on a hidden drawer having already loaded Files, or return only to a generic Library tab.
Complete supported UTF-8 Claude Code skill folders retain their instructions and supporting text
files under the existing project-skill writer and limits. Full-access host Claude starts select
those exact reviewed folders with the native session selector, without enabling ambient project
settings. Claude lists the skill under its native namespace. Ready for the next start remains a
projection state until an actual native `Skill` call succeeds. Readiness also follows the actual
worker policy; a reviewed skill that the native start cannot select must show Not available with
the reason, while keeping its approved contribution. The [bootstrap owner](03_Bootstrap_Auth_and_Identity_Projection.md#selected-personal-library-context)
defines that policy check. The folder picker skips known OS metadata
files and shows the skipped count; real binary assets still require native package setup. This does not assert that another harness
loads that path. Binary packages, source-only references, plugins and unsupported worker types use
their existing native setup and permission flow. Setup sends the owner's exact request and selected source URI, shown together in the dialog, to
the selected worker. A model saying done is not a native load receipt. New session requirements remain
visible; applying a saved source never silently restarts a live worker.

**Remove here** is available when the worker is idle. The server checks again and refuses busy
removal; it does not queue a surprise mutation. Remove only this contribution's context entries and
pins. Trash an unchanged copy used only by this contribution; keep edited or shared copies and say
so. **Undo** restores the contribution and its eligible Files copies together within the stated
24-hour recovery window. Reloading or returning to Added here retains a Removed row with Undo
until that deadline; it does not offer Apply or Remove for that row. Undo uses the same logical
Library restoration and Files recovery owner, not a physical-only file restore. If native file cleanup is unconfirmed, say that the native files have not been
removed yet and offer Retry file cleanup on the same removal. Never show an unconfirmed cleanup as
complete. Removing a reference does not uninstall a native package, revoke an unrelated
grant, restore an old whole bootstrap, or remove another item's files. If another selected Library
item retains the same approved capability, explain that it stays enabled for that selection.

A closed workspace stays read-only. Its manager can still browse My Library and keep an exact
revision of a remaining file. The same Files menu offers Keep for reuse without Rename, Move or
Delete; a successful save has a visible Files notice. Add here, removal, Undo and native setup are
disabled for a closed target. Review and setup dialogs check the target again before submission,
so closing a workspace during a dialog does not send a stale action. Private Library editing and
capture remain available; viewer and shared-link access do not gain these actions.

Run Project retains every selected Library item/version, including Bundle parents. Ordinary files
keep their existing upload fields; selected Library files bind through that same owner once, with
Added here provenance. A source-only native reference grants nothing and can start its selected
native worker to perform setup from the exact retained source metadata. Use supported items starts
the original job with applied knowledge, supported reviewed skills and the worker's existing
abilities. Other native/approved references remain visibly pending or Not available in this exact
worker's Library → Added here view. They grant no tools, project no unreviewed executable files and
never silently discard the job. They are not mislabeled as copied-workspace grants. Copy and
template reviews retain their blocking confirmation where a capability or account choice is
unresolved. Keeping an eligible same-owner Ready account reference does not require a new sign-in
or copy its credentials.

Approved review uses the existing exact-target pending change and confirmation. Personal source
selection is not a shortcut around approval. Existing connection/account ownership remains intact.
An approved native capability upgrade takes effect at the next native start; confirmation is not
proof a running session loaded it. If old native file cleanup is unconfirmed, the confirmation page
shows that warning and reuses its button for **Retry file cleanup**. It retries the retired grant's
existing removal, never approves or upgrades again. The existing confirmation recovery record keeps
only the confirmed change, exact worker and retired grant IDs for this cleanup, replacing the
single-use confirmation token. Reload revalidates those IDs against the authenticated confirmed
change before offering Retry. A busy refusal shows its plain cause and keeps the same retry. A
successful cleanup clears the recovery hint; edited/shared retained files remain explicit.

### The same selection in Chat

The Chat composer has **Add knowledge & abilities**. Its selection belongs to that conversation and is
restored from the server snapshot. An explicit empty selection clears it for future messages;
omitting a new selection retains it. Accepted turns retain their exact original item/version IDs.

A Direct Queen receives selected readable text after the full unchanged conversation history, up to
the configured selected-text limit. It receives no new Library tools. Binary and overflow items are
shown as **Not loaded for this Queen · available to Helpers**. Full authorized references remain
available through the existing worker context/files path. Native Queen and Helpers use projected
files and metadata, reading what they need; projection is not proof of native load or model reading.
Original user goals and history remain ahead of optional Library context. Selection must not replace
or summarize the user's message or add another worker router.

These UI contracts use the same owner-scoped item/version IDs, errors, native requirements and
receipts as API/MCP clients. Worker-scoped callers see selected material only. New Library management
operations are not automatically added to the Fast Queen's narrow handoff/control tool set.

## Hard Requirements

1. Keep native harness interfaces intact. Extend the existing operator UI and reuse its working
   Watch and Files components.
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
   and draft-preservation behaviour. Add knowledge & abilities reuses the same private item/version picker as Chat and Watch. The
   selected knowledge expands to the existing exact upload/revision fields; native items keep
   their actual-target review and supported setup. Background and files are
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
    A ready or stopped host workspace with no run and no declared live surface shows a clear
    ready/paused state and the next action, instead of opening an empty terminal or claiming it
    is connecting. Its Library stays available. Polling must not start a worker or fabricate a
    session; a real run restores the existing terminal view and saved output behavior.
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
   - Close workspace is available from More; it shuts down compute and retains saved work/results
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

Check authentication without requiring unrelated provider permissions. xAI connections use its
documented read-only key-status endpoint, including blocked/disabled status, rather than requiring
permission to list models. A restricted inference key can be valid without that list permission.
Connection readiness does not promise access to every model or available credit; the exact selected
model is checked when work runs. Keep that failure distinct from missing sign-in.

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
4. `Close workspace`

Close stops the workspace's compute and retains its saved work and results. The compatibility
API/MCP action is `terminate`; it is not a hard-delete operation. Library item deletion and
recoverable Files Trash have their own separate controls and retention rules.

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

Copies keep project context (`project-definition.md`, `work-log.md`, `harness-prompt.md`), helper
tools, supported skill files and ordinary work files. The workspace-root `glasshive-run/` evidence,
active-run state and constraint ledgers stay with the source, alongside excluded private native
capture/session state. User evidence and nested project directories outside that reserved root
remain eligible under the existing safe-file checks.

The durable reservation is fail-closed. A fresh pending request cannot start a second project. A
stale reservation is recovered only when one exact owner-scoped worker has both its persisted copy
report and completed duplication event; ambiguous state is preserved for diagnosis or cleaned when
it has no workspace, never silently duplicated. The legacy project-worker duplicate route keeps its
request/response compatibility but uses the same fail-closed destination review gate.

Capabilities that cannot be copied safely are returned as exact, owner-scoped review items. Duplicate
and template destinations are created with that deterministic action-id report in the same durable
write as the new worker, before file copy or HTTP success. A copy retains its concrete personal
provider account selection when that account belongs to the same owner, is Ready and supports the
worker type. The reference points to the existing account; its credential home, browser sign-ins
and native conversation history are not copied. That retained selection resolves its provider
review item without another sign-in or confirmation. An unavailable, unready, mismatched or foreign
account cannot take this retention path and is never silently substituted.

The destination remains unable to run while a required review is unresolved: each applicable
Library item needs its real approval, an unresolved personal provider selection needs an authorized
choice, and an optional capability may be omitted only when the user explicitly confirms
`Continue without this capability`. Legacy brokered connection and provider-grant references are
deliberately non-transferable: the UI says they were not copied and never claims a setup message
restored them. Provider selection cannot be waived.
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

This gives users a separate working copy without carrying over sensitive browser state or
promising a native conversation/session fork.
Duplicate has no default per-file, file-count, or batch-byte limit. Explicit
`GLASSHIVE_DUPLICATE_MAX_FILES` and `GLASSHIVE_DUPLICATE_MAX_BYTES` settings
remain available; the owner storage quota, safe-file checks, and copy deadline
still apply.

## Workspace display names

The Workspaces catalog lists user workspaces and Helpers. An internal Chat Queen with the private
conversation lane and its exact durable conversation-session binding stays with Chat, rather than
appearing as another ordinary workspace with raw Steer controls. This filter applies before paging;
it does not hide other workers in the same project or change low-level worker access, capacity or
conversation continuity. New internal session labels include the Chat start time and configured
agent identity; those labels do not choose a route or rename an agent.


**Workspaces → More → Rename** edits the display name and optional tags through the existing
worker metadata API. **Save details** keeps the same worker ID, files, account, history and routing
alias; the new display name survives reload. A display rename is not a new workspace or a copy.

The stable internal alias remains distinct from the user-facing name. Any future alias-editing
feature needs a separate explicit action because it can affect parent-side reuse routing.

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
5. A worker can be paused, resumed, interrupted, and closed from the new UI; Close retains its saved work and results.
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

## Chat entry

The second navigation item, **Chat**, opens `conversation.html`, which provides a single message composer using the configured assistant. Accepted
goals and results appear beside the conversation; no task schema, route, capacity or context form is
required to send the first message. The model may handle work directly or use available helpers.
Choosing an existing project also lets Chat reuse that project's named workers. Ask for the expert
by name; its existing abilities, files and history remain in the same Watch workplace. Chat keeps
its accepted goal, result and exact run controls. A new goal queues behind existing work; it does
not resume another task the owner paused. A blocked worker goal names its recovery: reconnect its
account in Connections, review it in Watch, or check its unavailable state in Watch. It does not
report a worker admission problem as an unavailable Chat assistant.
Files selected in worker results remain downloadable in the combined Chat reply. Publication uses
the terminal worker run bound by that conversation’s goal or completed Queue/Message receipt,
with the original owner, project, attempt and immutable bytes checked again. A shared owner or
matching file hash alone grants no access. Reloading the saved reply does not rerun the worker.
Each Chat turn also owns a stable output stream identity. Legacy turns missing only that identity
can publish their saved completed response through the same verified turn, request and native run;
this does not start another model call. Unavailable or unbound selected files fail that reply with
a typed publication failure while other goals remain readable and can complete.
If a send response fails after the server saved the turn, the composer reads back the exact saved
turn before reporting failure. A saved turn clears the draft and shows its current state; an
unconfirmed turn keeps its draft and idempotency key for a safe resend.
Guidance and Pause/Resume/Stop address exact work. A blocked admission leaves the original message
saved; Retry reuses that turn's idempotency key. A provider attempt that already ran instead offers
Send again as a new message. Typed account or model failures instead name the required Connections
repair and keep Edit, without a one-click resend of the unchanged request. After repair, the saved
message can be submitted normally. Raw provider diagnostics are not shown in Chat.
Run-scoped native permission requests appear beside the conversation,
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
A recorded native console belongs to its exact run and attempt. If a new start fails before creating
its native session, Watch keeps the current failure and does not append a previous session's log.
Legacy logs without native session metadata retain their existing fallback.
When the main native process has finished but its child work is still settling, Live activity
shows that attempt's existing saved output and says the work is finishing. That saved view
hides and refuses worker actions; workspace controls remain available. This does not mark
its job complete or start another native process; the normal child-completion checks still apply.
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


### Professional knowledge and abilities

The same Library browser uses readable cards, a named Knowledge/Skills or Tools/Plugins filter and Active/Archived/All choices. Active describes the saved item lifecycle; it never means the running model loaded a skill. Added here retains exact placement/review/setup states for that worker. Only a user's explicit display choices are remembered. All selected references keep their exact IDs and versions, and owner changes still clear private rows, selections and dialogs. New tool access stays subject to the existing native review and setup.

The ordinary picker asks what knowledge and abilities the named worker needs. Reference names explain the professional work; implementation formats and versions remain available in details. Reviewed tool metadata stays accessible under Details & access, and unavailable entries do not show meaningless disabled workspace selectors.

Cards explain the actual ability using the item's description, a Skill's description header or a
short excerpt of its exact saved note. Generic category text is a fallback. Existing versions can
gain this derived display metadata without rewriting their source bytes, hashes, references or
native setup. Do not read whole Skill bodies into the list or imply optional tools are installed.

The review lists the exact selected revision, including when the personal item has since changed.
Application listings join the immutable version ID to its version metadata in one query; they do
not substitute the newest personal version or assume version 1. A caller without a numeric
revision omits that label rather than showing a bare `v`.

Opening the drawer before the workspace's owner access is known must not discard the remembered
tab. Keep that display intent until verified access arrives. Explicit tab selection, closing the
drawer, a deep link or loss of access takes precedence. Private rows remain hidden until authorized.

Watch's normal live view fills its available stage even inside the Technical log disclosure.
Only the loaded same-origin document matching the requested view confirms attachment; a late
`about:blank` event is not success. Selecting Live activity explicitly reconnects even when its
URL has not changed. Polls keep a healthy frame stable. An unconfirmed view stays covered and
offers clear reconnect guidance; it does not become an unexplained white panel or hide a run's
failure notice. Finished work stays result-first, with its technical log available on demand.


## Starting with a professional AI Worker

Run Project presents **Experts** and **My experts** before the job form. Lead each bundled expert
card with the profession or function; show its optional human name as secondary information. Keep one short
outcome sentence visible. Included knowledge and abilities belong inside the existing **What’s
included** disclosure, with setup and optional tools, so the gallery stays easy to scan.

The five bundled portraits are generated fictional AI personas, not employees, credentials or
endorsements. Their assets and factual provenance belong in
`frontends/glass-drive-ui/src/glass_drive_ui/static/experts/README.md`. Keep the displayed crops
clear at 88 px on desktop and 80 px on narrow screens, in both themes, with dimensions reserved
before loading. Keep the five optimized local assets together below 100 KB. Text stays on the
shared neutral surfaces; the taupe selection treatment, explicit selected label and keyboard
focus remain distinct from decorative accents.

Stable catalog IDs map presentation assets only; names, list order and request wording must not
assign an asset or change execution. Custom saved experts keep their supplied name as the title;
their free-form worker role stays inside details. If no known asset exists or an image fails, use
the neutral non-letter fallback without guessing a profession or inventing an identity. Creative
working records and detailed generation instructions remain private.

Keep clear space between shared navigation and the gallery: about 44 px on desktop and 30 px on
narrow screens. Portraits reserve their dimensions before loading. Avoid large decorative banners,
extra frameworks or persistent skill lists that push the job form farther away.

**My experts** uses the existing saved worker setup records. **Save as expert** and **Start from
an expert** use that same source; template IDs, immutable versions, APIs and MCP tools retain
their existing technical names and behavior. Starting from a saved expert creates a new worker
with its saved setup; it does not resume the previous worker or imply native skills are already
approved. **Create my own** retains the existing New/Existing/Copy controls.
Reload restores the selected expert in its own collection; a saved expert stays visible under
**My experts**, with its exact saved identity.
**Create my own** focuses the **Who's Doing The Job?** section so the user reaches the worker
setup without searching below the gallery. It retains the current New/Existing choice.
The job heading stays beside its text box, below the expert choice. A temporary starter-catalog
failure offers **Retry** and keeps the normal worker choices and job draft; it does not poll
indefinitely or label a valid empty catalog as a failure.

Choosing a card fills local job choices only. Run Project imports its exact public starting
material into the owner's private Library, creates a normal paused worker from its immutable
template, and opens the existing review for that actual worker's native skill files. Cancellation
keeps the worker and job, so retrying uses that identity rather than making another. The original
job, supplied outcome and company files then use the ordinary launch path, Watch, steering and
delivery controls. Choosing a starter never changes global defaults or approves a tool silently.

The bundled starting team currently declares Claude Code on the host computer, matching the
supported native text-skill path. Other existing worker choices remain available; a source link
or a file projection is not proof another harness loaded a native skill. Optional community
plugins are clearly separate from included abilities and keep their real native setup/account
steps. Public starter content and synthetic demos contain no company material. User onboarding
remains an ordinary private Library selection.

The generic starter catalog/import API and management MCP tools return the same template and
Library records used by the UI. Import stores an owner-scoped idempotency receipt in the existing
control-plane database. Exact selected versions, target review, account ownership, quotas,
retention and native execution remain with their existing owners. A retry returns the original
template, including after a packaged catalog update; it never silently upgrades accepted work.

**Ask in Chat** on an owned reusable workspace opens Chat with that worker's project selected.
The existing conversation status exposes its authorized project roster; the same dispatch can
then reuse an exact worker. It does not grant cross-project discovery or replace saved accounts,
abilities or native history. A shared viewer or signed Watch link does not receive this shortcut.

Connections' **More** menu offers **Sign in again** for a ready subscription with a supported native
sign-in route. This uses the existing named account setup so the owner can correct its provider
account or workspace without changing worker selections or defaults. Cancel keeps existing native
credentials and marks setup incomplete. Provider login remains responsible for
replacing credentials after successful sign-in.
Provider marks use transparent first-party SVG artwork at account, setup and Watch menu sizes.
Preserve source geometry and approved colours; choose the supplied light artwork for dark
surfaces. Monochrome OpenAI artwork follows its provider's black/white treatment. Keep provenance
and usage guidance in the frontend NOTICE. Use structured provider IDs, not saved account names.
Do not put a white raster tile behind every mark or squeeze a wide wordmark into a square.

Connections' **More** menu shows concise dates and the runs observed by this deployment. Token
counts and elapsed worker time sit behind **Usage details**. Saved account names remain user data;
updating a provider's display brand must not silently rename those accounts or change identifiers.
