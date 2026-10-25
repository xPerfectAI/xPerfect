# xPerfect: MCP Publication and Client Compatibility

Existing protocol, package, skill, and configuration names remain compatible.

For a runnable walkthrough (connect, list tools, start, check and steer work), see the
[developer walkthrough](developer.md).

## Optional OpenAI-compatible model API

The management HTTP API, MCP and the OpenAI-compatible model API have separate credentials.
Starting the app and connecting an AI account enables ordinary worker jobs and app Chat; it does
not enable an external client's `/v1/chat/completions` or `/v1/responses` connection automatically.
Those endpoints return `503` with `provider authentication is not configured` until configured.

For a local host, stop it and add these settings to the private state `config.json` `env` object:

```json
{
  "GLASSHIVE_PROVIDER_API_KEY": "<new distinct random secret>",
  "GLASSHIVE_PROVIDER_PRINCIPAL_ID": "<the existing local owner ID>",
  "GLASSHIVE_PROVIDER_TENANT_ID": "local",
  "GLASSHIVE_PROVIDER_TRUST_IDENTITY_HEADERS": "false",
  "GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS": "false"
}
```

Start the same state again. Configure the external client with the runtime API base URL ending
in `/v1` and this new provider key. Use `/v1/models` with that key to choose an offered model.
The principal must be the owner of the selected connected AI account. Keep the key distinct from
`WPR_API_TOKEN`, the MCP credential and the AI provider's own key; xPerfect rejects token reuse.
Do not put credentials in URLs, examples, screenshots or Git. Local host state is described in
[host setup](host-setup.md#state-and-settings).

Identity headers cannot change the configured principal while header delegation is off. Native
access remains explicit: the Chat Completions field `metadata.glasshive_options.access` accepts `read_only`, `workspace` or
`full`; full access also requires the operator's explicit opt-in. Unsupported native access
returns a typed refusal instead of silently granting broader permissions. App Chat and MCP keep
their existing account and authorization paths.

## MCP Direction

xPerfect should follow the current modern MCP shape:

- `streamable-http` as the primary remote transport
- `stdio` for local direct attachment
- `sse` only for compatibility where still needed
- stable tool names and structured outputs
- a thin MCP adapter over the control-plane API

## Why This Matches Current Practice

Current official guidance from Claude Code and modern MCP tooling points in the same direction:

- remote HTTP MCP is the recommended remote transport
- SSE is deprecated where HTTP is available
- clients benefit from dynamic tool updates through `list_changed`
- scope-aware MCP configuration matters for safety and portability

An authorized automatic Parallel launch that cannot pass its selected runtime's readiness
gate returns that snapshot's typed admission reason, such as `storage_pressure_critical`,
through the account API and MCP adapter. Invalid launch authority keeps the existing isolation
rejection. A missing readiness reason also keeps that rejection; no fallback, replay or weaker
admission is implied.

## Artifact links use the configured browser origin

MCP artifact listings and downloads use `GLASSHIVE_ARTIFACT_BASE_URL` when explicitly set,
then the operator URL (`GLASSHIVE_OPERATOR_BASE_URL` or `WPR_OPERATOR_BASE_URL`), before
legacy MCP/default fallbacks. The standalone host supplies its configured UI port through
`WPR_OPERATOR_BASE_URL`; its separate MCP `--base-url` selects the runtime API, not the
browser destination. Non-default ports must produce usable signed links without changing
accounts, source files, or the worker. Signing, path restrictions and link authority are unchanged.

## Scheduled work through the same control plane

The [application boundary contract](09_Application_Boundary_Contract.md) owns project independence.
The [runtime guide](../runtime_phase1/README.md#generic-worker-and-webhook-schedules) owns setup,
HTTP examples, clock policy and delivery states. Scheduling extends the existing recurrence store,
clock and outbox; there is no application-specific scheduler or second work manager.

| Interface | Scheduling operations |
| --- | --- |
| Owner HTTP | `POST /v1/recurring-schedules` creates a typed worker or webhook target. Existing list/get/PATCH/deactivate/run-now/history/DELETE routes control the same definition. |
| Owner MCP | `schedule_create` accepts the same structured request. Existing `worker_recurring_schedules`, `worker_recurring_schedule_update`, `worker_recurring_schedule_deactivate`, `worker_recurring_schedule_run_now`, `worker_recurring_schedule_occurrences` and `worker_recurring_schedule_retire` apply to either target. Existing worker-specific creation remains compatible. |
| Owner destination setup | `webhook_destinations`, `webhook_destination_create`, `webhook_destination_update` match GET/POST/PATCH `/v1/webhook-destinations`. Workers do not receive these credential-management operations. |
| Native worker MCP | The runtime projects `xperfect-schedules` at `/v1/native/schedules/` only when the worker has a current `schedule_access` grant. It exposes `schedule_list`, `schedule_create`, `schedule_get`, `schedule_update`, `schedule_run_now`, `schedule_history` and `schedule_retire`. |

Native scheduling tool descriptions identify the scheduling domain and their reminder/timer purpose.
This keeps ordinary native tool discovery useful without relying on an application broker,
a remembered server name, or application-specific routing. The actual MCP `tools/list` response
is the discovery contract; a configured server alone does not prove model discovery.

Native scheduling uses the existing exact worker/run/attempt authority. `schedule_list` returns
supplied target choices; the worker passes one unchanged, plus the user's instruction and recurrence.
A webhook grant fixes the destination and approved request data. An optional `allow_worker_target`
grant permits that worker's own target. A separate explicit `manage_destination_ids` grant permits
control of that owner's existing schedules at the listed destinations; it does not permit arbitrary
new request bodies. Current grants are read again for each operation. Stop, close, retry and attempt
replacement revoke the old native bearer. Neither a service-wide token nor destination credentials
are supplied to the worker. The [bootstrap owner](03_Bootstrap_Auth_and_Identity_Projection.md#independent-application-inputs)
defines the grant and neutral context fields.

Specialized Fast Direct Queen profiles keep their existing small handoff interface. Scheduling is
work for an authorized native worker; these tools do not enlarge that Queen's catalog.

Use a stable creation key for a schedule. Repeating the same owner/key/body recovers the saved
receipt; changing the body under that key conflicts. Destination creation also accepts a retry key.
A generated signing secret is available only on that logical creation receipt, including an exact
retry; normal lists and reads omit URLs, auth and signing secrets. A retry never re-enables a revoked
destination. Delivery acceptance and application work completion remain separate outcomes.

## Private Library through HTTP and MCP

My Library is an owner-private index over the existing Files store. It is separate from the
deployment's approved capability catalog. Saved items have immutable `item_id`/`version_id`
references. Notes and knowledge use existing uploads, revisions, quotas, pins and workspace
bindings; flat Bundles retain their selected versions. Saving a native source or an approved
capability reference does not install it or grant access.

| Owner operation | HTTP | MCP |
| --- | --- | --- |
| Browse/read saved items | `GET /v1/personal-library`, `GET /v1/personal-library/{item_id}` | `personal_library_list`, `personal_library_read` |
| Save a note, knowledge, text skill, source reference or Bundle | `POST /v1/personal-library` | `personal_library_save` |
| Add an immutable revision | `PATCH /v1/personal-library/{item_id}` | `personal_library_revise` |
| Archive/restore to future pickers | `POST /v1/personal-library/{item_id}/archive` | `personal_library_archive` |
| Delete with explicit confirmation | `DELETE /v1/personal-library/{item_id}?confirm=true` | `personal_library_delete` |
| Expand exact selected versions | `POST /v1/personal-library/resolve` | `personal_library_resolve` |
| Keep a current workspace file for reuse | `POST /v1/workspaces/{worker_id}/personal-library/capture` | `personal_library_capture` |
| Inspect/apply selected material | `GET` / `POST /v1/workspaces/{worker_id}/personal-library` | `personal_library_workspace`, `personal_library_apply` |
| Remove a managed contribution / Undo | `DELETE /v1/workspaces/{worker_id}/personal-library/{application_id}`, `POST` to its `/undo` route | `personal_library_remove`, `personal_library_undo` |

Creation and application keys recover the same logical operation; conflicting content is an
error. Revision updates require `expected_version_id`. Capture requires the existing Files
`file_id` and `revision`; a changed source fails rather than saving different bytes. Removal is
idle-only and preserves edited or shared copies and independent configuration. Its Undo window
is bounded; the returned `undo_expires_at` is authoritative. Archive changes future selection,
not an installed worker. Deletion releases only the personal retention pin; accepted turns,
Bundles, templates and workspace copies retain their own exact references.

These are owner-management tools. Shared links, viewers and attempt-scoped workers cannot browse
My Library. Native workers read only selected material through their existing Files and context
interfaces. The Fast Direct Queen keeps its small handoff interface. Its selected text is bounded
and appended after unchanged conversation history; binary and overflow items remain available to
authorized Helpers and are explicitly reported as not loaded for that Queen. No Library CRUD or
new read tool is added to the Queen. The source description manifest is
`runtime_phase1/src/workers_projects_runtime/prompts/personal-library-tools.json`.

`workspace_launch` and `worker_delegate_once` also accept optional exact `library_refs` and
`confirm_library` fields. Selected material uses the existing prepare/apply/run path: the worker
identity exists before application, and the original job starts only after the existing Library
owner has applied supported material. Unapplied requirements remain visible in the result and
worker's Library context; they do not grant tools or replace the job. Launches without selections
retain their existing atomic delegation path. `confirm_library: false` leaves executable skills
unreviewed; it does not cancel the original ordinary job. An approved capability still needs its
existing exact-target confirmation. Explicit template/copy reapproval gates remain intact.

## Frozen public compatibility baseline

`runtime_phase1/tests/fixtures/public_compatibility_baseline_v1.json` is a frozen, synthetic
compatibility contract. It captures every existing HTTP operation and request/response
schema (60 operations and 30 OpenAPI components), every legacy MCP tool name and complete input
schema (32 tools), plus the bootstrap/context headers, callback payload and signing shape,
Chat Completions and Responses fields, and provider/model attestations that OpenAPI alone cannot
describe.

`runtime_phase1/tests/test_public_compatibility_contract.py` compares the running candidate with
that frozen baseline. New routes, tools, and optional fields may be added. Existing operations,
tool names, fields, media types, status schemas, enum constraints, callback fields, signing
protocols, model mappings, and required response fields may not disappear or silently change; new
request fields must remain optional. The legacy one-shot schedule response therefore keeps its
original state enum, while recurring-schedule occurrence states use their own additive response
model.

Do not regenerate the golden from the candidate under test. A future baseline update requires an
explicit release decision and reviewed semantic diff; use a new versioned fixture. Synthetic values
only are permitted in this contract.

Callback delivery adds the optional signed `callback_created_at` field from the immutable native
outbox record's `created_at`. It keeps that exact creation clock across retries while `callback_ts`
refreshes for each send and retains the existing freshness and signing contract. Receivers use the
creation clock for event order, not transport arrival. A legacy record without a creation clock
omits the field; the publisher does not synthesize it or copy one from the stored payload.

Current nonterminal attention callbacks carry the same optional `run_input` accepted goal and
continuation context as terminal callbacks. This applies to a current `needs_input` run and to a
current running run with an exact pending native permission or question binding. The outbox
transaction checks the accepted run input, current run and native attempt identity. Existing
outbox payloads keep their original bytes on retry; an upgrade does not retrofit old callbacks.

### Worker callback receiver response

Before using a signed worker callback, verify `X-GlassHive-Signature` against the **raw request
bytes**, using a constant-time comparison and the configured freshness policy. Its signing key is
the UTF-8 hexadecimal HMAC-SHA256 of `<worker_id>:<run_id>` with the configured callback secret;
the header is `sha256=` plus the hexadecimal HMAC-SHA256 of the raw body with that derived key.
Do not reserialize JSON before verification.

Persist accepted results monotonically by run and revision; deduplicate retries by the stable
`callback_id` and exact result identity. For integer `result_revision >= 1`, return HTTP 2xx with
this JSON **only after durable acceptance**. Replace each placeholder with its incoming value;
use the actual revision in both revision fields (this example uses 1). The `current_*` fields must
describe the receiver's stored current result:

```json
{
  "callback_status": "accepted",
  "callback_id": "<callback_id>",
  "run_id": "<run_id>",
  "result_revision": 1,
  "result_digest": "<result_digest>",
  "current_callback_id": "<callback_id>",
  "current_result_revision": 1,
  "current_result_digest": "<result_digest>"
}
```

An exact replay returns the same receipt with `callback_status: "idempotent"`. A bare 200, wrong
identity or incomplete receipt does not acknowledge a versioned terminal result: delivery retries.
An already stored newer revision uses the existing HTTP 409 `superseded` receipt; a same-revision
identity disagreement uses HTTP 409 `conflict`. Neither may silently replace stored results.
Nonterminal and legacy callbacks without a positive integer result revision need only a successful
2xx response. These worker receipts are distinct from schedule webhooks' Standard Webhooks contract.
The [receiver validator](../runtime_phase1/src/workers_projects_runtime/service.py) and
[terminal recovery tests](../runtime_phase1/tests/test_terminal_callback_recovery.py) own the exact fields.

The authenticated `POST /v1/work/{work_ref}/actions/receipt` reads a previously finished action
receipt. It accepts the original action request and requires the exact owner, tenant, work,
idempotency key, action and canonical request digest. A completed receipt returns its saved body;
a failed receipt returns its saved status and detail. Missing, mismatched or unfinished receipts
return `active_work_idempotency_conflict`. This read never reserves, resumes, recovers or executes
work. Clients use it only to recover an exact completed operation after a normal action conflict.

Assignment retries also retain their accepted identity. Reuse the original `Idempotency-Key` on
`POST /v1/workers/{worker_id}/assign`. The existing run reservation stores a digest of the normalized
instruction, effort, bootstrap and continuation context, plus the accepted effort. Different execution
inputs return HTTP 409 with `assignment_idempotency_conflict`; the response never reports a retry's
new effort as the setting of an old run. The digest retains no duplicate bootstrap or secret values.
The runtime checks the reservation before changing worker configuration, checking provider
prerequisites or resuming work. Exact input-manifest validation retains its existing transaction.

Older reservations have no request digest. An instruction-only retry can read their saved result;
a retry that supplies execution settings cannot prove that they match and returns the typed
conflict instead of guessing from mutable worker configuration. The authenticated
`GET /v1/workers/{worker_id}/assignments/by-idempotency/{idempotency_key}` remains the read-only
recovery path after a lost response, including those legacy records. It starts no new job.

Owner Library list, read, resolve and workspace-list tools, and the packaged starter catalog, declare
their read-only effects through standard MCP annotations. This avoids asking native clients to
approve an ordinary read as a mutation. Private owner/management-scope checks remain unchanged;
creation, edits, application, native setup and other writes do not acquire that annotation.

Active-work pages and their overflow counts use the same effective current run. A live
continuation remains visible even when the delegation's stored run pointer still names an older
terminal run. Counts retain the same account scope, terminal recency and cursor boundaries as
listed rows, so a false zero cannot hide the next page. The account API regression covers
completed/failed predecessors, waiting continuations, tenant/owner isolation and tied cursors.

## Client Strategy

### LiveKit voice clients

The [standalone LiveKit reference](../integrations/livekit/README.md) connects the official
OpenAI-compatible LiveKit plugin directly to xPerfect's existing conversation API. It preserves
requested model/effort and native workspace/session/tools, and projects only typed eligible text
into speech. Quiet public text and file/activity metadata remain available to the host separately.
No LibreChat dependency or second orchestrator is introduced. Each SDK stream keeps a stable
per-turn request identity through retries; the sample disables blind retries and speculative
generation and uses an explicit response budget. Provider-owned early speech uses xPerfect's
exact native admission guard, while Core-owned source/family authority stays unchanged.

The tested boundary is LiveKit Agents/OpenAI plugin 1.5.10, the OpenAI SDK 2.32.0 and a real
isolated local room with synthetic speech input, transcription, a native model reply, TTS and
received active audio. Interruption, reconnect and background callback speech acceptance remain open;
do not claim production voice parity or a latency guarantee from this reference. Host-owned
memory, Feelings and other modules still require their existing configured projections.

### LibreChat / Viventium / Standalone-Compatible Clients
Use xPerfect as an external MCP server. The standalone external-MCP path is config-only and
requires no LibreChat application-code changes. Optional Viventium/LibreChat account, inference,
scheduling, or callback bridges may use a separately pinned compatible integration build, but are
not required by standalone xPerfect.

For enterprise worker delegation, the host application injects a service-authenticated user
assertion and request context into the MCP call. The neutral standalone header set is:

- `X-GlassHive-Service-Token`
- `X-GlassHive-Tenant-Id`
- `X-GlassHive-User-Id`
- `X-GlassHive-User-Email`
- `X-GlassHive-User-Role`
- optional request context: `X-GlassHive-Agent-Id`, `X-GlassHive-Conversation-Id`,
  `X-GlassHive-Parent-Message-Id`, `X-GlassHive-Message-Id`, `X-GlassHive-Surface`,
  and `X-GlassHive-Input-Mode`
- optional upload context: `X-GlassHive-Request-Files`,
  `X-GlassHive-Request-Attachments`, `X-GlassHive-Tool-Resources`, and
  `X-GlassHive-File-Ids`

Viventium-prefixed headers and `X-WPR-Token` remain supported aliases for existing local
integrations, but new clients should use the `X-GlassHive-*` headers. The MCP adapter
folds request context into `bootstrap_bundle` without reading LibreChat internals. Upload metadata
is projected into `bootstrap_bundle.files` when a local path or extracted text is available; when the
chat model cannot see an uploaded file body, it should still launch the worker with the file
reference and requested outcome so xPerfect can use the trusted upload metadata supplied by the
host.

A trusted selected-file packet may name earlier conversation attachments. The MCP adapter
projects its explicit upload records under the authenticated transport owner before checking the
complete selected set after joining current headers. Either carrier may hold only part
of that set; completeness is checked once on their validated, owner-scoped union. A repeated stable file ID uses the existing file
merge owner; unknown IDs and ambiguous records stay rejected, and cross-owner paths cannot
supply original bytes. Current request metadata never replaces an authorized historical selection.

Callbacks are optional. Viventium can use signed callbacks for durable completion delivery, but
standalone/plain LibreChat deployments must still work without them by using:

- `workspace_status` for non-blocking follow-up checks
- `workspace_wait` for explicit blocking waits
- `workspace_artifacts` and `workspace_artifact_download` for generated files, default signed
  downloads, and preview/open links
- `workspace_preferences_get` and `workspace_preferences_set` for per-user worker and effort
  defaults
- the returned View / Steer URL for operator visibility and takeover

Explicit worker effort must survive MCP launch, schedule and continuation into the actual
runtime request. Direct OpenAI, Anthropic and xAI use their existing exact model catalog choices
and provider effort environment keys; `default` is an explicit supported value. Direct OpenAI
and Anthropic also reuse the existing provider preference fields when effort is omitted. An
explicit override wins; it must not become a prompt hint or silently fall back to provider default.
These rules apply to atomic delegation and the older create/assign path alike.

Default `workspace_launch`, `workspace_wait`, and `workspace_status` payloads are intentionally
compact. They return user-actionable state, result tools, output text, artifact short links, and a
View / Steer short link when available. When the MCP host supplies stable conversation context,
xPerfect remembers the launch there and keeps raw project/worker/run ids diagnostic-only. A native
client without stable conversation context instead receives one minimal `follow_up_context` containing
the exact ids needed by `workspace_wait`/`workspace_status`; this avoids a workspace-list discovery
call without widening the catalog or diagnostic payload. When `follow_up_context` is returned, its
`run_id` and `worker_id` are the primary continuation contract and clients must pass them to each
bounded wait/status call. Recent-conversation resolution is a fallback only when the launch returned
no follow-up context and the host preserves stable conversation metadata. MCP outputs must not expose raw
`gh_token` URLs or opaque signed-link tokens; they should expose `/r/{ref}` and `/v1/link-refs/{ref}`
indirection instead.

A successful nonterminal `workspace_launch`, `worker_delegate_once`, or continuation receipt labels
that mission-control link `View / Steer <task>`, with `<task>` derived from the typed launch title or
description. Its structured kind is `mission_control` and its state is `nonterminal`; the link proves
that the workspace exists, not that a requested deliverable or artifact is complete.
Ordinary mission-control links remain available in the receipt, with `include_in_response: false`
by default. The model may show one when useful or requested; tool descriptions follow that mark
instead of requiring topology narration in every acknowledgement. Required native input and
approval form links retain their separate user-action contract. Callback producers keep ordinary
workspace links in typed `operator_url`/`watch_url` metadata, outside `message` and `full_message`.
Only native input events may add the required workspace input route to prose; delivered file
Download/Preview links remain usable. Failure guidance asks the agent to inspect existing native
workspace/evidence rather than requiring an end-user operator page.


Workspace-native tool and account setup stays inside the selected persistent workstation. The
Workspaces `Set up tools` action opens that profile's installed AI harness through the generic desktop
action contract; it does not add connector-specific frontend flows or copy connector credentials into
the controlling MCP client. When the workspace selects a ready personal Codex or Claude account, that
exact native setup window holds an exclusive interactive lease and borrows only the selected account's
authentication. The workspace remains the owner of tools, plugins, trust, and other native state.
Closing the window, reaching the bounded session limit, or losing the lease removes the
credential-bearing container before releasing the account; new missions fail before run creation while
the setup window is open, including attempts to switch that workspace to another account or deployment
route. Service startup recovers any unreleased interactive session by removing its exact container
before releasing even an expired lease. Browser, file, and shell actions never acquire provider
credentials. At a bounded timeout, the authoritative container-removal and sealing path stops the
credential-bearing native CLI; one transient cleanup failure is retried through that same idempotent
path before the account is quarantined, and the detached host-side Docker client is reaped afterward.
Desktop actions during a mission retain the exact active run/lease proof.

For Codex, xPerfect temporarily overlays the selected subscription authentication while preserving
any separate workspace-local login in host-private state that is not mounted into the leased container,
then restores the workspace login after the interactive or mission lease ends.

Claude keeps its native user-scope MCP, plugin, trust, and onboarding state under the workspace's
persistent `HOME`; xPerfect does not replace that state with an account-global config directory.
During a personal Claude setup window or mission, only Claude's native secure-storage directory is
projected from the selected account home while the exact provider lease is held. The credential-bearing
container is removed before the lease is released. This lets a connector added once through the
workspace-native harness remain visible on later missions without copying its authorization into
xPerfect or another workspace.

Native Grok Build sessions discover authorized MCP tools through `search_tool` and call them
through `use_tool` using the returned qualified name. Native built-in tools remain directly
callable. A connected MCP catalog can be usable through discovery without listing every tool
as a direct model declaration. A late ACP inventory notification is not a readiness timestamp.

Managed Viventium records this access mechanism as one registered native-profile fact for
Grok Worker and conversation authority, only when native tools are enabled and the existing
authorized MCP projection is nonempty. The host's deferred capability-server fields retain
their separate meaning and grants stay unchanged. The host's prompt architecture owner,
`docs/requirements_and_learnings/49_Prompt_Architecture_and_Token_Efficiency.md`
(section "Native Profile MCP Access Facts"), owns registration, Workbench visibility,
publication and final authority checks.

Canonical conversation tool evidence uses the existing owner-, message-, invocation-, request-
and run-bound version 1 carrier for Codex, Claude and Grok. Grok evidence pairs typed ACP tool
IDs with machine tool names, refined arguments and terminal outputs from the same native session.
Incomplete or conflicting records remain explicit omissions. Typed operation failures remain
failures even when the native transport completed. Existing byte bounds and redaction apply;
thought events are excluded. Tool evidence does not select files or grant publication. Native
epoch recovery remains conservative when tool evidence has no replay carrier.

For conversation-provider file delivery, the existing selected-output publisher also records
`glasshive.output_files` version 1 on the canonical response and on its terminal protocol chunk.
The record binds the owner, conversation, message, stream, agent, logical turn and revision,
request, run, attempt and invocation to selected basenames, MIME types, lengths, SHA-256 hashes
and signed download URLs. Canonical storage and recovery retain the same immutable selection.
Protocol chunks use the durable accepted request's creation timestamp on replay and reattachment.
Selected file metadata also survives declared graph transfers with its emitting publisher identity. The host
captures terminal metadata before SDK aggregation, verifies the exact selection and imports normal
File attachments. It does not scan answer prose or the workspace for exports. An existing file
intentionally selected for publication can be delivered to an external channel such as Telegram;
unselected reads and references do not grant that export. Equal bytes with distinct selected names
remain distinct attachments. Failed imports remain typed unavailable receipts without hiding the
answer or creating a fake downloadable file.

For recognized PNG, JPEG, GIF and WebP bytes, captured content owns the delivered MIME type and
filename extension. A conflicting source extension is corrected only on the delivered name;
source paths and exact bytes stay unchanged. Corrected names are deterministic, preserve distinct
selections when names collide, and remain bound to the existing immutable output and replay.

Host mission output uses the same immutable selected-file publisher. Codex, Claude and Grok capture
links in the selected final assistant message before selecting its FINAL REPORT prose. Tool and
reasoning events, literal paths, quoted text and code do not grant file publication.

A terminal `run.completed` callback carries `output_files` version 1 with native-owned identity:
owner, run, attempt, callback, origin and work references, result revision and digest, selected file
descriptors and optional rejection receipts. It has no destination, Main agent or provider identity.
The existing callback signature covers the carrier. Association verification returns the actual
bound run and attempt; the host resolves its own trusted destination before importing attachments
into its normal message store. Replay retains the accepted callback bytes and the immutable selection.
Progress, failed and cancelled callbacks do not publish this completed-result carrier.

Native file previews and downloads retain the selected filename when verified metadata identifies
one unique name for the exact stored artifact. Delegated completed results use their owner-bound
callback, revision and digest. Ordinary native Chat results use their existing exact worker/run/
attempt capture; they do not need a mission callback. The shared name mapping also preserves the
publisher's verified image extension. Missing, invalid, stale or ambiguous metadata keeps the storage
basename. This presentation rule does not change signed-link scope, file access, immutable bytes or
range downloads.
A mission view lists only model-selected completed outputs under Result files. Other
authorized files remain in the separate collapsed Workspace files section, labeled as
reusable workspace content that may come from other work. Neither section grants new
file access or worker controls.

Some standalone native Chat results name a workspace file without an immutable capture. The
existing Files service can offer that exact **current workspace file**, using the same trusted
native placement mapping as Watch. `result.current_workspace_files` contains `filename`,
`open_url`, `download_url` and `semantics: "current_workspace_file"`. These actions read the file
as it exists now; they do not claim to preserve the bytes from the completed reply.

This read requires the conversation's authenticated owner, exact completed goal/run and matching
worker, project and workspace placement. The final Queen answer can use the same declared file
from that authorized child result, including when an existing completed answer is reloaded. An
exact native path declared by multiple workers is ambiguous and is not reassigned by filename.
Callback-backed missions still require their accepted completion envelope; this current-file
path does not bypass it. Reading an existing saved answer projects only the returned view;
it does not rewrite that stored response, raw worker output, result hashes or bounded paging.
A new answer uses the normal response storage path with its rendered file actions. Immutable
artifact references continue to use their existing captured-file path.

Host native file selections may use the admitted workspace or the current worker's managed
`TMPDIR`. The runtime records the true source root and relative path, then retains immutable bytes
for replay; it does not grant the worker's whole home, another worker's temporary root, or an OS
Downloads folder. Explicit selections keep ordinary existing files under operational input folders
legal. Automatic discovery still omits those folders. Both paths reject traversal, hidden state,
private profiles and reserved instruction or credential files, and selected sources must be
ordinary files without symlinks or hardlinks.

Selected Markdown destinations normalize padding inside angle brackets before URL decoding,
both when capturing the file and when rendering its immutable reference. Percent-encoded
filename spaces remain exact. This syntax normalization does not widen permitted source roots
or bypass the existing owner, run, attempt, traversal, private-file or link checks.
When the file carrier cannot resolve a local selection, visible text keeps its label only; the
existing typed attachment failure still reports the cause. This prevents private path fragments
from leaking through a broken link. Public URLs, anchors and other non-file references remain
unchanged, and a valid selection still renders its verified immutable artifact reference.

Host-native conversations consume the registered `worker.conversation_file_delivery` declaration
once after the signed profile instructions and before the exact pinned Feeling tail. A local
Markdown link selects delivery; a path in code is a citation. Authorized files outside the supported
roots must be staged byte-for-byte into the current worker TMPDIR before selection. This declaration
does not grant filesystem access, force files for chat answers, or change mission/Docker contracts.
Existing conversation workers receive the current declaration through their normal authority refresh.

Rejected explicit local selections travel in the same version 1 carrier as an optional
`rejected` list of basenames and typed codes, without local paths. These records produce unavailable
receipts, including when no selected file can be delivered. Public citations and ordinary answers
do not become file selections. Native snapshots stream bytes without an inherited image count or
batch limit; the host applies its configured attachment limits before importing files.

For generated file delivery, `signed_download_url`/`default_url` is the default chat-facing artifact
link and should be labeled `Download file`. The MCP payload should also preserve `signed_open_url`
or a View / Steer workspace link so the user can inspect previews and all workspace deliveries
without exposing raw worker paths, raw `gh_token` URLs, or whole generated file contents in chat.
Preview pages are part of that contract: their `Download file` action should download through a
scoped artifact short ref, and their `View workspace` action should use the same authenticated
`/r/{ref}` workspace short-ref path as chat View / Steer links, redirecting to a tokenless watch URL
after minting a bounded session cookie.

Short refs are the durable user-facing link contract. `GLASSHIVE_LINK_REF_TTL_SECONDS` defaults to
`0` (`never`/`none`/`disabled`/`off`/`false`/`no` are equivalent), so a completed artifact or
View / Steer link remains usable for the authenticated owner after the underlying signed token has
expired. Set a positive integer number of seconds only when the deployment deliberately wants
short refs to expire. Enterprise clients must treat `/r/{ref}` and `/v1/link-refs/{ref}` as
authenticated routes, not bearer links.

When MCP/runtime payloads emit `/r/{ref}` URLs whose host is the separate xPerfect UI service, the
runtime and UI must share `GLASSHIVE_LINK_REF_STATE_PATH` or otherwise route the ref to the process
that created it. A hosted deployment that shares the local SQLite store must also set
`GLASSHIVE_LINK_REF_SHARED_GROUP` for every process that mints or resolves refs. The administrator
must pre-create one root-owned, group-owned directory (`02770` on Linux) and database (`0660`),
keep WAL/SHM files `0660`, and use local storage. In this explicit shared mode xPerfect validates
the boundary and never widens permissions itself. Without the shared-group setting, the existing
single-process private `0700` directory and `0600` file behavior remains unchanged. This is a
deployment contract, not a LibreChat behavior.

The MCP descriptions must make this non-callback path obvious to connected LLMs. A model should
launch work with `workspace_launch`, include `uploaded_files` when the user attached files, return
an ordinary mission-control link only when useful or requested, and use status/wait/artifact tools
for follow-up and delivery. It
must not fall back to pasting whole generated files into chat or writing its own inferior local
code path when xPerfect has already produced a signed artifact link.

`workspace_launch` is intentionally non-blocking. If the user asks to "wait", the model should call
`workspace_wait` with the returned completion wait timeout and ids, when present, or rely on
same-conversation scoped recent-dispatch resolution. It must not enumerate workspaces to rediscover
the launch. If the requested run is older than the worker's latest run,
diagnostic status/wait responses preserve both the requested run outcome and the latest effective
run so an operator can explain the lineage without exposing raw ids in normal user-facing payloads.
Blocking waits should stay below common chat/Redis/proxy idle windows. When a bounded wait reaches
its deadline while the worker is still running but user-facing files already exist in delivery
locations such as `output/`, `deliverables/`, `reports/`, `artifacts/`, or `out/report`/`out/data`,
`workspace_wait` may return `status=deliverable_ready` with signed links. That is a user-delivery
state, not a claim that the process row is completed; callers should deliver the files and use
`workspace_status` later only if the user asks for a final status refresh. MCP progress notifications
are best-effort and bounded; a stale progress/SSE channel must not block the tool's status payload.

Fresh `workspace_launch` calls must not accidentally resume stale workspaces. A supplied
`workspace_alias` is honored only when `reuse_existing_workspace=true`; otherwise xPerfect creates a
fresh one-off project/worker for the new request. Use explicit reuse only when the user asked to
resume or reuse that existing workspace. Deliberate operator-level reuse remains available through
`worker_find_or_resume`, `workspace_continue`, and explicit lifecycle tools.
When the user asks for a reusable favorite workspace, the same `workspace_launch` call sets
`favorite=true` before queueing the run; no catalog/update tool chain is required. The first line of
`description` is the short human workspace name, while following lines and `context` preserve the
complete request. Capability setup requested inside that workspace stays in the launched worker:
the controlling AI must not install the capability into itself or wander through account, Library,
connection, or tool catalogs. If the run is still active and the user asked to wait, it repeats only
`workspace_wait`.
An explicitly closed workspace is permanently closed from the moment teardown begins. That includes
`terminating`, `termination_failed` (compute teardown needs retry/operator attention), and
`terminated`: run, message, pause, interrupt, resume, desktop/terminal, account-switch, and schedule
create/update/run-now tools must return the runtime's bounded recovery message (create a new
workspace) rather than a generic transport error, and must not leave queued work behind. Startup
reconciliation may retry teardown for `terminating` or `termination_failed`; it must never reopen the
workspace. Close and the actual external start boundary are serialized: a cold runtime publishes
its PID/connection metadata before a request is accepted, then releases the lifecycle fence while a
long response streams. Already-open terminal and desktop streams are revoked on close, delegated
recurring definitions are deactivated, and a failed late compensation keeps the workspace visibly
`termination_failed` for retry.

The same rule applies to lower-level `worker_delegate_once` calls. A supplied `alias` or existing
`project_id` does not imply reuse for a fresh one-off task; xPerfect creates a fresh worker alias
unless `reuse_existing_workspace=true` is set. This keeps model-selected fallback paths from
reintroducing stale worker history after `workspace_launch` has already been made fresh-by-default.

When no `profile` is supplied, MCP tools must use the authenticated user's saved preference first
and then the deployment default from `GLASSHIVE_DEFAULT_WORKER_PROFILE`. Enterprise deployments
should keep Codex (`codex-cli`) as the default once its Responses-compatible route is validated.
OpenClaw and Claude Code remain selectable only when present in `GLASSHIVE_ALLOWED_WORKER_PROFILES`
and proven by the worker matrix.

For upload byte transfer, the host should provide every supported file/request header, including
`X-GlassHive-Request-Files`, `X-GlassHive-Request-Attachments`, `X-GlassHive-Tool-Resources`, and
`X-GlassHive-File-Ids`. In enterprise shared-storage deployments, file metadata should point to
owner-scoped virtual paths such as `/uploads/<authenticated-user-id>/<file>` so xPerfect can copy
the bytes into the worker workspace under `uploads/<safe-filename>`. When a host authenticates users
by SSO/email but stores uploads under an internal user id, it must also send
`X-GlassHive-Storage-User-Id` (or `X-Viventium-Storage-User-Id`) with the internal storage owner.
xPerfect uses that value only for upload byte lookup; the authenticated user id still owns the
workspace, callbacks, and signed links. The internal storage id is transport-only authority and is
removed from worker-visible context, prompts, callbacks, traces, API detail, and logs.

Some older LibreChat-compatible hosts can pass authenticated user/message headers but cannot project
`files` or `attachments` into MCP request headers without a LibreChat image upgrade. For those hosts,
xPerfect provides an opt-in compatibility lookup:
`GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_FALLBACK=true`. The fallback is disabled by default. When enabled,
it only runs in enterprise mode, only with `X-GlassHive-Storage-User-Id`, `X-GlassHive-Conversation-Id`,
and `X-GlassHive-Message-Id` present, and only resolves a stable upload token declared by trusted
`selected_files` against that storage owner's upload folder under the configured upload roots. It
considers files modified within
`GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_RECENT_SECONDS` (default `900`, clamped to 5 seconds through 24
hours), capped by `GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_MAX_FILES` (default `8`, max `32`). A missing,
filename-only, or ambiguous selection attaches no file and fails closed. Without a trusted stable
selection, recent same-owner files are unrelated and are never projected. Prefer real request-file
headers whenever the host supports them; this lookup remains a legacy bridge, not the primary
contract.

### Claude / Claude Code
Support:

- stdio attachment for local use
- HTTP MCP for remote/local shared use
- project-scoped `.mcp.json` bootstrap where appropriate

### Codex / ChatGPT-compatible MCP consumers
Prefer remote HTTP MCP with auth in front of the server when not loopback-only.

### Local user connection

The local launcher supplies **Connections → Use xPerfect from another AI app** with its existing
`xperfect mcp --state-dir ...` stdio command, the current instance address and the checkout used for
the companion skill. The signed-in panel's **Automatic** instruction configures only the receiving
Codex or Claude Code client and finishes with one `workspace_list` verification. **Manual** exposes
the equivalent native add command. Both use the same stdio service; there is no second connector,
OAuth registration, copied provider login or token pasted into the instruction.

Local client cards say that no sign-in is needed and do not offer remote HTTP address or OAuth
registration fields. The native command uses the selected private local state under its OS user;
AI worker accounts remain separately selected and authorized. Keep the generated instance-specific
server name and state path, reuse an existing exact registration, and preserve unrelated client
configuration. This route is not advertised for multi-user deployments or as a hosted chatbot's
access to a user's local machine. Hosted HTTP setup retains its registered-client and native OAuth
requirements below.

### Hosted user connection

**Current launcher limit (29 September 2026):** the hosted input does not expose the UI's per-client
registration fields or pass MCP discovery settings to the UI role. Connections therefore cannot
generate native Codex/Claude setup on that profile. A ready worker account or a working authenticated
MCP endpoint does not prove that client setup. Do not add unrecognized hosted JSON keys, edit
generated container config, copy tokens or create a custom OAuth callback as the user workaround.

The designed Glass Drive **Use xPerfect from another AI app** panel is the source of truth for a
deployment's public HTTPS MCP URL and the exact clients that deployment has completely registered.
It must not advertise Codex, Claude Code, ChatGPT, or another client unless the live endpoint returns
that client's complete allowlisted contract. The primary `Automatic` path copies one short
self-selecting instruction: Codex follows only the Codex section and Claude Code follows only the
Claude Code section. Each section contains that client's exact deployment-generated add/sign-in
command, tells an existing matching registration to be reused, and ends with one `workspace_list`
verification instead of a full tool-catalog dump. The receiving AI does not configure another
client or guess configuration from a URL it cannot contextualize. If xPerfect is already connected,
the companion skill skips setup and calls only the one tool needed for the user's request. The user completes sign-in in the
browser profile opened by the
client, or copies the client-provided authorization URL into the browser profile they intend to use.
The browser does not claim it can edit local client configuration itself. The clients' native OAuth
flows are authoritative: the receiving AI must not construct OAuth URLs, run a custom callback
listener, inspect tokens, or fall back to static credentials.

xPerfect includes the exact required API scope in both protected-resource metadata and the initial
`WWW-Authenticate` challenge. Current Codex releases can still fall back to generic OpenID scopes on
an ordinary Reconnect unless the MCP server's native config persists its `scopes` values. For Entra,
that config contains both the canonical xPerfect API scope and `offline_access`: the API scope binds
the authorization to the MCP resource, while `offline_access` asks Entra for the refresh token needed
to renew an expired access token. The generated Codex setup includes those values once and tells the
user to restart Codex/ChatGPT once after changing the config; later Add/Reconnect actions use the same
resource and renewable login without a hand-built authorization URL. Claude Code continues to use its
native remote-HTTP add and `/mcp` sign-in flow.

MCP is the capability boundary. The companion skill is a concise workflow guide that tells the AI
which xPerfect tool to call; the native packages are distribution only, not another integration
layer. One plugin directory at `plugins/glasshive/` contains the shared skill plus native Codex and
Claude manifests. It deliberately contains no MCP server definition because the deployment-specific
URL, client registration, and scopes come from the signed-in Glass Drive panel. This follows the
official Codex model of packaging skills and MCP integrations as plugins, and Claude Code's native
plugin plus remote-HTTP MCP and `/mcp` authentication flow.
Shared server instructions stay short because clients may present them alongside every tool. They
say to make one matching call when one call can finish the request and never narrate the catalog;
action-specific parameters and safety details stay with the action that owns them.

The same public repository is the marketplace for both clients. A user pastes the Automatic
instruction once; the current client installs only its own package and then applies the exact live
MCP setup. The equivalent native package commands are:

```text
codex plugin marketplace add xPerfectAI/xPerfect
codex plugin add glasshive@project-glasshive
claude plugin marketplace add xPerfectAI/xPerfect
claude plugin install glasshive@glasshive --scope user --yes
```

Existing exact installations are reused. Installation never triggers tool-catalog enumeration, and
ordinary use calls only the operation needed for the user's outcome.

The generated command may carry a public client id and fixed callback flags as opaque setup
arguments. xPerfect still validates that the configured Codex resource exactly equals the canonical
MCP URL, but current Codex discovers that resource from the protected-resource metadata. The setup
command must not also pass `--oauth-resource`, because doing both produces two OAuth `resource`
parameters and standards-compliant identity providers reject the malformed request. Human-facing
callback addresses and registration explanations remain only in
the administrator disclosure; they are never presented as links or user steps.

`Manual` exposes the exact server address and official client commands rather than asking users to
edit hidden configuration. `<SERVER_NAME>` is the stable `glasshive-<12 hex>` name derived from the
canonical deployment URL, so multiple independent self-hosted origins do not collide:

```text
codex mcp add -c mcp_oauth_callback_port=<REGISTERED_PORT> -c 'mcp_oauth_callback_url="http://127.0.0.1:<REGISTERED_PORT>/callback"' <SERVER_NAME> --url <MCP_URL> --oauth-client-id <REGISTERED_CLIENT_ID>
codex mcp login -c mcp_oauth_callback_port=<REGISTERED_PORT> -c 'mcp_oauth_callback_url="http://127.0.0.1:<REGISTERED_PORT>/callback"' <SERVER_NAME>
claude mcp add --transport http --scope user --client-id <REGISTERED_CLIENT_ID> --callback-port <REGISTERED_PORT> <SERVER_NAME> <MCP_URL>
```

Administrator registration details separately show the exact redirect URI that must be
pre-registered, with explicit `do not open this address` copy. These callback addresses are local
client plumbing, not web destinations or user setup actions; opening one without the matching client
listener produces an expected localhost connection failure. Claude Code
uses `http://localhost:<port>/callback`. Current Codex derives
`http://127.0.0.1:<port>/callback/<server-hash>` from the canonical MCP URL; copy the URI shown by
the deployment rather than calculating or guessing it. Both Codex commands override the fixed port
and base callback URL so an ambient user-level `mcp_oauth_callback_url` cannot redirect this
registration to a different host or path.

The public HTTPS MCP URL is the RFC 8707 resource sent by Codex and returned in protected-resource
metadata. For Entra, register that exact canonical URL as an additional Application ID URI on the
resource application and request its delegated scope as
`<canonical-mcp-url>/access_as_user`. Keep the existing `api://<api-app-client-id>` identifier
when another consumer still uses it. The public URL is not implicitly the JWT `aud`: Entra v2 access
tokens normally use the API app's client-id GUID as `aud`, and the `scp` claim contains only the
short delegated permission value. Multi-user MCP therefore requires explicit
`GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES` independently of `GLASSHIVE_MCP_PUBLIC_URL` and validates
issuer, one of those exact token audiences, non-conflicting tenant claims, stable subject, required
scopes, and one explicitly
allowlisted OAuth client before minting a short-lived internal runtime assertion. Configure approved
client registrations with `GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS` and the provider's unambiguous
client claim names with `GLASSHIVE_MCP_OAUTH_CLIENT_ID_CLAIMS`; rotate registrations by overlapping
old and new IDs only for the bounded rollout window. Entra does not provide MCP dynamic client
registration, so Connect AI emits no client command until the deployment config also supplies the
same pre-registered Codex/Claude client ID, each fixed callback port, explicit token audiences and
scopes, and the canonical Codex public resource. An Entra request scope whose resource prefix differs
from that canonical URL is invalid deployment configuration: the authorization server can reject it
before issuing a token even when the client and callback are correct. Resource drift, missing verifier policy, or a client
ID outside the allowlist remains `action_required` and produces no copyable command. When enrollment
is enabled, a first fully verified MCP login enrolls the same hashed
issuer/subject principal used by Glass Drive, while a locally disabled principal is rejected on the
next request. Access tokens and provider credentials are never forwarded into a worker.
When enrollment is disabled, an otherwise valid but unregistered MCP account receives HTTP 403
with an administrator-approval message. It receives no tools or runtime identity; signing in again
or granting more OAuth scopes does not approve the account.

`enterprise.tenant_id` is the deployment's xPerfect ownership namespace; it is not assumed to be
the upstream token's `tid`. Set optional `mcp_oauth.token_tenant_id` only when the authorization
server emits a stable tenant claim that this deployment must validate. For Entra, use the directory
tenant GUID. Generic OIDC deployments may omit it. The token's validated tenant is audit context;
the internal assertion continues to use the independent xPerfect ownership namespace.

Browser OIDC login is the authoritative durable role-sync surface. A fully verified browser login
updates the principal from the configured immutable role/group claim, including promotions and
demotions; already-open sessions read the current principal role from durable state. MCP access-token
roles may establish the first role only when enrollment is enabled and must never overwrite an
existing durable role. When a role map is configured, both browser and MCP tokens must contain a
mapped role before access or enrollment; a missing or unmapped claim fails closed. Email and
`preferred_username` remain mutable display metadata and never own a workspace or grant admission.
Enforce tenant/domain membership at the IdP through tenant and app-role/group assignment policy.

Provider account switching also requires the IdP to advertise an `end_session_endpoint` and to
pre-register the exact `human_auth.oidc.post_logout_redirect_uri` emitted by the compiler (by
default, `<operator-public-origin>/login`). Without that provider capability xPerfect still clears
its own session and says that provider-level account switching is unavailable.

The MCP control plane is additive: `workspace_list`, `workspace_rename`, `workspace_duplicate`,
`worker_accounts_list`, `connections_list`, `library_list`, `workspace_activity`, and
`workspace_capability_prepare` share the same user-scoped runtime resources as the UI. A capability
proposal returns a browser confirmation URL. Only an authenticated human session with CSRF and
`human:confirm` scope can consume its time-bounded single-use token; the model cannot self-approve.

`workspace_duplicate` requires an 8-128 character `idempotency_key`. Clients must reuse that key for
retries of the same source/name request; the runtime durably returns the original project/workspace,
rejects different requests that reuse the key, and scopes keys by authenticated tenant and owner.

The versioned non-secret companion skill at `skills/connect-glasshive/SKILL.md` points clients back to
this canonical repository and official flow. It does not embed a deployment URL or credential.

## Publication Rules

For local-only use:

- loopback-only HTTP is acceptable
- bearer auth is optional but recommended

For broader publication:

- require auth in front of the MCP server
- keep write-capable tools explicit and well-described
- do not duplicate runtime logic inside the MCP layer
- `execution_mode=host` must be an explicit tool argument; the MCP server should not infer host
  execution from natural-language phrasing

For Azure enterprise VM mode, an existing standalone LibreChat integration remains config-only:
LibreChat sends the neutral `X-GlassHive-*` service, identity, request, and upload headers to the
remote xPerfect MCP endpoint over a locked-down channel. Optional Viventium/LibreChat bridges are
enabled and version-validated separately; leaving them disabled preserves existing LibreChat
behavior and requires no application-code change. xPerfect trusts identity headers only after
service-token validation and ignores model/tool-supplied `owner_id`. Direct hosted Codex and Claude
clients use the OAuth-protected MCP resource. Partial OAuth configuration, a non-HTTPS hosted public
URL, or an invalid resource token fails loud instead of falling back to caller identity headers or a
static user bearer token.

## Compatibility Alias

The `workers_projects_runtime` integration name remains a supported compatibility identifier.
The product name is xPerfect.

## Standalone coordinator interface

Authenticated owner routes live under `/v1/coordinator/conversations`: create, read, submit turns,
accept typed goals, dispatch and exact goal controls. Owner identity comes from existing request
authentication. Public submissions cannot supply arbitrary bootstrap authority. The ordinary
frontend `/v1` proxy retains its existing login and CSRF checks.

A goal's `run_id` preserves its latest result binding. When queued follow-ups coexist with active
or paused work, `control_run_id` and `control_state` identify the authoritative native control
target. Clients pass that exact control ID; the existing action receipt pins it across retries.
Queued result IDs cannot silently control a different active generation. Successful native
Queue/Message receipts retain each source and effect in goal history, including earlier queued
effects. Attachment rechecks the current native same-goal action receipt, including pending
receipts, so a stale snapshot cannot discard newer guidance. Recovery keeps newer native result
bindings; equal reservation times preserve the already bound result. The exact effect receipt is durable before native admission wakes. Stop intent fences new admission; visible state follows the actual run until native termination
proof. Cancelled intent limits the native action mask to available Stop recovery or Dismiss.
Stop confirms the exact controlled run and cancels only that goal's owned pending effects.
A native host Pause uses the same exact-run control owner as an explicit run Pause. A queued
run can be durably paused before launch. If a run is starting and its exact process identity
is not confirmed yet, the API returns retryable `409 runtime_control_not_ready`: no control
was applied. Refresh status and retry after startup. The refusal does not invalidate the
startup processor or claim that work was paused.

A confirmed host-pause generation already has durable termination proof; native Stop reuses it
after restart. A host run paused before it ever starts needs no termination signal when it has
no lease history or active worker lease. Historical compute release on a reused worker does
not reject exact Pause of a never-started queued run with no lease history or active worker lease;
existing scope and generation fences still apply. Unknown proof or any newer active generation cannot
use either shortcut. An unknown
Stop can finish against the same owned control target; cancelled intent cannot revive other work.

`/v1/native/coordinator/` is a narrow native MCP endpoint using the existing peer run/attempt binder.
It exposes `coordinator_accept_goals`, `coordinator_dispatch`, `coordinator_handle`,
`coordinator_status`, `coordinator_result`, and `coordinator_control`. A peer token alone is not
coordinator authority. Tool descriptions are authored in the packaged
`workers_projects_runtime/prompts/coordinator-tools.json`, beside `coordinator.md`. Source version,
prompt hash and tool-description hash are exposed in the conversation manifest for Workbench.

Coordinator prompt version 3 keeps ordinary admission details and internal IDs in controls unless
the user asks. Conversation replies use plain language about the work and explain only evidenced
job blockers. A roster worker's paused or idle compute is not proof that its accepted job needs
the user to Resume. Queued helpers still end the foreground turn without polling, so they can use
the same account. New conversations capture the current prompt manifest; saved conversations keep
their approved prompt snapshot. The six tool schemas and their authority are unchanged.

Native coordinator tool schema version 3 lets `coordinator_dispatch` accept exactly one of
`worker_id` (an existing worker) or `route_id` (a new helper), alongside `goal_id` and `instruction`.
`coordinator_status` lists at most 100 available workers in the selected owned project and reports
truncation. A narrower workspace scope stays narrow. Foreground conversation workers, closed workers, ephemeral workers and delegated helpers are
excluded; dispatch rechecks the same typed eligibility, ownership and scope. The existing worker's saved account
must remain ready. Unsupported or unavailable targets never select another account or helper.
Old route dispatch JSON and request digests retain their exact shape. Ordinary existing-worker
runs have no delegation work reference; Queue/Message/Dismiss and failed-helper Retry remain
delegation-only actions. A new request becomes a new goal on the same ordinary worker. Stop uses
the existing run-scoped cancellation claim and native termination proof, preserving unrelated
queued goals. Public whole-work Stop keeps its existing queue-wide meaning. Account reconnect,
ability review and unavailable-worker blockers have distinct typed codes.
The six native tools and owner HTTP dispatch share this typed branch. No management MCP authority
or Direct Queen tool is added.

Native coordinator tool schema version 2 made `coordinator_accept_goals` take only `goals`.
Its authenticated run identifies the current provider request; that request's accepted message ID
owns `source_turn_id`. The runtime checks the conversation, owner, session and worker before
acceptance, including before the outer conversation has attached the request receipt. The model
does not guess a chat turn ID from a native run ID. Native clients must refresh this tool schema;
the authenticated owner HTTP API still requires its explicit `turn_id`. No new Queen tool or
automatic choice of objective, route or worker is introduced.

Codex's generated configuration approves the exact run-bound `xperfect-coordinator` server's
tools only after its worker/run identity and nonempty native token projection match. Its tools
still validate their live run and owner authority. This uses Codex's existing per-server approval
setting; it does not approve other MCP servers, change global approval policy or grant a new
provider/account/tool capability. A missing or stale projection cannot add this configuration.

Native worker profile support does not imply conversation-provider support. Unsupported exact
model/profile/effort selections remain unavailable; no provider alias or fallback is invented.


## Native scheduling discovery

The authorized native scheduling MCP server publishes standard initialize instructions and
seven tool descriptions from `workers_projects_runtime/prompts/schedule-tools.json`. The
manifest helper returns its source, version and content hash for lineage checks. Initialize
instructions and tool descriptions load from that packaged source. The same runtime scheduler
and authorization checks serve all clients.

Server instructions identify durable scheduling as an independent native capability. A
connected-service broker catalog does not describe this server's inventory. Clients that use
server instructions for namespace descriptions or tool search can discover scheduling without
application-specific tool aliases, extra routers or an expanded conversational tool set.
A real saved schedule and due result remain required acceptance evidence.

A manual `schedule_run_now` starts one occurrence. Its saved worker or receiving application owns
the instruction and configured output. The calling worker does not also perform that instruction
or poll after acceptance unless the user explicitly requests monitoring or waiting. A separately
requested task still runs normally. Creation/start acknowledgments use a short natural confirmation
of timing and destination; internal receipt rules are not conversation text. Acceptance never proves
the task completed. The versioned manifest supplies this contract to every native client.

Manual run receipts expose `occurrence_created`: `true` only after a fresh atomic admission,
`false` when an earlier receipt is recovered. The command marker is not persisted into ordinary
due events or history. A delegated owner's absent value remains unknown. Keep the key stable
for retries; each distinct new request needs a distinct key. A recovered occurrence cannot be
confirmed as a new start. Authority, deduplication and occurrence identity remain unchanged.

The native `schedule_run_now` response is an admission receipt: definition/occurrence identity,
authoritative timing, owner status/state, and known creation/replay outcome. It does not contain
the execution instruction, snapshot, target data or clock-internal dispatch hint. The saved
executor retains those values; authorized `schedule_get`, `schedule_list` and `schedule_history`
remain available for inspection. General API/UI receipts keep their existing contract. A
concurrent missing admission is a typed not-found error, not an empty success.


## Public starting templates, private worker ownership

`workspace_starter_list` (HTTP `GET /v1/workspace-starters`) lists the packaged public worker
template metadata and exact content hash. `workspace_starter_import` (HTTP
`POST /v1/workspace-starters/{id}/import`) accepts that hash, an owner-scoped idempotency key,
and optional explicit account/effort choices. It composes ordinary personal Library records and
an immutable workspace template. It starts no worker, installs no plugin and grants no access.

Continue with the existing `workspace_template_instantiate`, exact-target Library review and
normal worker launch/status/steer/result tools. The same records can be used by a native Chat
worker through the management MCP. This adds no Fast Queen routing rule or domain-specific tool.
Template summaries include exact `personal_library_refs` alongside approved `library_refs`,
without copying private material bodies into the catalog. Shared-link and viewer identities
cannot use private template import.

The import receipt binds its key to the exact package hash and supplied choices before creating
material. Retry a partial import with that same request; do not generate a new key because the
response was lost. A completed replay returns the original template even if the packaged
catalog changed. Reusing a key for different choices is a conflict. Account references are
revalidated before use, and a native skill still requires approval on the actual new worker.

A fresh packaged starter choice reuses the exact original immutable imported version while its
lineage head remains active. A user-edited revision stays selectable through My experts and is
not silently substituted into a new packaged starter choice.

## Catalog CRUD through the same owner

`GET /v1/workspace-templates?state=active|archived|deleted|all` lists current expert heads.
`GET /v1/workspace-templates/{template_id}` inspects one exact version; `/versions` lists its lineage.
`PATCH` appends a name/description revision with `expected_catalog_revision`. The existing
`POST /v1/workspaces/{worker_id}/templates` can update a lineage from an owned worker with
`expected_template_id` and `expected_catalog_revision`. `POST /duplicate` copies one exact template
using the existing import receipt and an 8–128 character idempotency key. Same-key retries preserve
the first result; changed choices conflict. No worker or model call is needed to copy an expert.

`POST /v1/workspace-templates/{template_id}/state` takes `state: archived|active` plus the current
catalog revision. `DELETE /v1/workspace-templates/{template_id}` requires that revision and
`confirm: true`; it removes the catalog choice recoverably. Restore uses state `active`. Exact old
versions, accepted uses and retained Library references survive.

`DELETE /v1/workspaces/{worker_id}` (also `/v1/workers/{worker_id}`) requires
`expected_catalog_revision` and `confirm: true`. It closes exact native compute before hiding the
card. `POST /restore` restores visibility, still Closed. `GET /v1/workspaces?catalog_state=deleted`
finds removed cards; the default is active. It does not purge projects, shared workspaces or results.
Existing worker metadata PATCH, safe Duplicate and native Close keep their owners.

MCP parity uses `workspace_template_list` with the same state filter, `workspace_template_get`
(with optional `versions`), and `workspace_template_manage` actions Edit, Duplicate, Archive, Remove
and Restore. Edit optionally captures `source_worker_id` through the same sanitized snapshot owner.
`workspace_remove` and `workspace_restore` use the same worker lifecycle endpoints. Only reads are
annotated read-only; mutations never self-approve Library/native permissions. Owner/tenant and
signed-viewer restrictions apply equally to HTTP, MCP and UI. Foreign IDs are not found; stale
revisions conflict before destructive control.
