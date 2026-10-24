# xPerfect runtime

This is the native worker runtime packaged with xPerfect. For the shortest setup path, use
[Run xPerfect on your computer](../docs/host-setup.md) and `./xperfect start` from the repository root.
The commands below start individual services for development.

The Python package name `workers_projects_runtime` remains a compatibility identifier.

Application integration: [explicit context, environment, files and scoped native scheduling](../docs/03_Bootstrap_Auth_and_Identity_Projection.md#independent-application-inputs).
The [application boundary contract](../docs/09_Application_Boundary_Contract.md) owns the independent
project contract and standalone acceptance.

## What It Does

- creates persistent `Projects` and `Workers`
- starts workstation-backed worker sandboxes for `codex-cli`, `claude-code`, and `openclaw-general`
- provides live desktop view, terminal takeover, runs, logs, events, and artifact visibility
- persists worker home and workspace across runs
- supports `pause`, `resume`, `interrupt`, and `terminate`
- exposes a thin MCP wrapper over the runtime API
- supports portable bootstrap seeding via:
  - `bootstrap_profile`
  - `bootstrap_bundle`

## Workspace Catalog And Retention

Workspace discovery is owner-scoped and cursor-paginated. The primary catalog contains `named`
workspaces; one-off `ephemeral` runs and migrated `legacy` rows require their explicit filters.
Catalog rows expose only rediscovery metadata, including normalized tags, provider/capability
readiness, and the next scheduled occurrence.

Named workspace files and private state survive compute reaping and runtime restart. Expired
Docker-managed ephemeral workspaces are garbage-collected after seven days by default. Configure
`GLASSHIVE_EPHEMERAL_RETENTION_S` (60 seconds to 365 days) or disable the policy with
`GLASSHIVE_EPHEMERAL_GC_ENABLED=false`. The reaper fails closed for active work, future schedules,
active account leases, pending confirmations, idempotency records, host/user workspace roots, or
unrecognized storage layouts; it never deletes named or legacy state.

The first provider account for an owner and provider becomes that provider's default automatically.
Native provider-account setup, verification, and mission use are serialized by an owner-scoped
lease. Rootless container deployments reconcile stale mounts and normalize the private credential
tree inside the rootless user namespace before host-side access. While an isolated Codex mission
holds that lease, xPerfect gives the worker ownership only of Codex's non-secret installation ID
and recreatable argument-alias directory so the reviewed CLI can initialize; cleanup removes the
container and reseals those paths with the rest of the private tree. Cleanup failures remain
recoverable through **Check connection**; a fresh provider sign-in is a secondary choice, not the
default.
Explicitly selecting a later default retains the existing one-default-per-provider contract, and
disconnecting a default clears it so a newly connected replacement can become the default.

## Recurring Schedule Ownership

Legacy one-shot schedules remain xPerfect-native and retain their existing API and database
contract. Standalone recurrence is additive: durable definitions are stored separately from
immutable occurrence rows. Worker occurrences enter the existing one-shot queue; webhook
occurrences enter the existing callback outbox without an AI worker or provider.

- Standalone xPerfect defaults `GLASSHIVE_RECURRING_SCHEDULE_OWNER` to `glasshive_native`.
  The legacy `native` value remains accepted without rewriting stored definitions.
- The compatibility value `viventium_cortex` (also `scheduling_cortex`) delegates recurrence
  definition, polling and dispatch to a configured external schedule owner through an authenticated
  boundary. xPerfect does not keep a second local recurrence definition. An occurrence enters the
  one-shot queue with a stable idempotency key. Missing owner configuration or availability fails
  closed before a local recurrence row is written; integration runtime markers also reject
  standalone-native ownership.
- Interval recurrence is elapsed UTC time. Daily recurrence uses an explicit IANA timezone.
- `next_valid_earliest` advances nonexistent spring-forward wall times to the first valid minute and
  chooses the earlier fall-back instant. `next_valid_latest` uses the later fall-back instant.
- A delayed tick materializes only the latest eligible occurrence and records it before advancing,
  so retries and concurrent ticks do not duplicate a firing.

Hosted migration rehearsals set `GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED=false`. This passive mode
disables startup queue reconciliation, both immediate and replay/retry callback delivery, lifecycle
reapers, and the scheduler while a cloned database is inspected. Newly emitted callback records stay
durably pending for the live phase. The live single-runtime service sets the flag back to `true`; a
passive rehearsal must never deliver callbacks or execute copied queued/scheduled work.

## Background reliability and result delivery

The service owns its periodic callback recovery, lifecycle cleanup, scheduler, host lease
heartbeat and any configured isolated-runtime probe. `/health` reports these owners under
`background_consumers` and returns HTTP 503 if a required periodic owner has stopped. Explicitly
disabled consumers remain marked `disabled`; finite startup recovery may run or finish while
HTTP is ready. Provider account availability is reported separately from loop liveness.

Each lifecycle cleanup phase catches a transient dependency failure independently and retries
on the next existing pass. Shutdown stops the other owners and executors even if lease cleanup
fails; unknown native termination keeps its durable fence and remains an explicit cleanup error.

Worker-result callbacks and scheduled webhooks use the same bounded HTTP transport pool,
separate from native work and provider result settlement. `WPR_WEBHOOK_TRANSPORT_SLOTS` bounds
running plus queued transport tasks; owner limits prevent one owner from filling the pool.
Up to two worker callbacks per recipient can progress within the same owner limit;
terminal results and input requests precede routine pending status within each fair owner batch. Claim,
submission, payload preparation and settlement failures release their exact in-memory reservation.
The existing durable outbox and generation/lease CAS own retry after a sender crash.

Production worker callback delivery performs one HTTP attempt per admission. A retryable failure
persists `next_attempt_at` with exponential backoff from
`GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S` (default 0.5 seconds), capped at 300 seconds. The existing
scheduler wakes for that due time; no maintenance thread or HTTP slot sleeps through backoff.
`GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS` still bounds the total attempts before dead-lettering;
the old burst-attempt setting does not multiply production attempts. A receiver-requested
recovery that cannot acquire bounded transport returns retryable HTTP 503 with `Retry-After: 1`;
capacity alone must not report an unchanged durable decision. Pending `run.needs_input`
events use the same recovery while their original attempt remains current. A retry preserves the
original callback/result identity and never re-runs native work. Exhausted or terminally rejected
delivery stays visible as a dead letter; it is not success and is not silently revived.

## Generic worker and webhook schedules

Use the same owner-authenticated HTTP session as the other control-plane operations. The generated
OpenAPI from the running runtime is the request/response schema owner. The operator UI and MCP call
the same service; [MCP tools](../docs/04_MCP_Publication_and_Client_Compatibility.md#scheduled-work-through-the-same-control-plane)
and the [editor journey](../docs/07_Minimal_Unified_Operator_UI.md#schedule-an-ai-workspace-or-an-application-webhook)
are documented in their own owners.

1. Save a protected destination with `POST /v1/webhook-destinations`:

   ```json
   {
     "name": "Example app",
     "url": "https://example.com/hooks/schedules",
     "idempotency_key": "example-destination-01"
   }
   ```

   Save its returned `destination_id`. Install the returned `secret_once` in the receiver's secret
   store. Supplying an existing Standard Webhooks signing secret and optional auth headers is also
   supported. An exact retry key recovers that creation receipt; changed configuration under the
   same key conflicts. GET lists public references only; PATCH can rename, rotate credentials and
   disable/re-enable the destination. URLs are immutable and remain private, including their paths
   and query strings.

2. Create a definition with `POST /v1/recurring-schedules`:

   ```json
   {
     "target": {
       "kind": "webhook",
       "destination_id": "whd_0123456789abcdef0123456789abcdef",
       "request_data": {"request_ref": "daily-summary"}
     },
     "instruction": "Prepare today's summary",
     "idempotency_key": "example-schedule-01",
     "recurrence_type": "daily",
     "local_time": "09:00",
     "timezone_name": "Etc/UTC"
   }
   ```

   A webhook needs no project, worker, workspace, model or provider account. Its optional instruction
   is scheduled request data, not an AI execution inside the clock. Worker targets use
   `{"kind":"worker","worker_id":"wrk_example"}` and require an instruction; their selected native
   harness, accounts, context and admission remain owned by existing execution paths. The existing
   worker-specific creation route remains supported.

3. The existing clock saves one occurrence and outbox intent. After commit, transport POSTs a
   Standard Webhooks `schedule.due` envelope. It includes the schedule ID/version, occurrence ID,
   scheduled time, effective expiry, authenticated principal, instruction and approved request data.
   `webhook-id` stays stable across attempts; `webhook-timestamp` and `webhook-signature` are fresh.
   An explicitly configured raw-payload destination receives only its request JSON; it must not
   infer unsigned schedule metadata.

A receiver uses the stock `standardwebhooks` verifier on bounded raw bytes, before interpreting
JSON. For example: `Webhook(receiver_secret).verify(raw_body, dict(request.headers))`. It scopes
its durable dedupe receipt by authenticated sender/owner and event ID, rejects a changed digest for
that identity, and commits admission before returning HTTP 2xx. It then releases ACK and background
work independently through its existing application mechanisms. No xPerfect client SDK, client
registry or result-callback protocol is required. A receiver remains responsible for its own work,
context and selected output surfaces. See [Standard Webhooks](https://www.standardwebhooks.com/) and
its [Python package](https://pypi.org/project/standardwebhooks/).

Explicit typed definitions use xPerfect's native clock even when a compatibility deployment has an
external owner for its older definitions. One definition has one owner; get/list/update/run-now and
history resolve its saved target without requiring that external owner for the local definition.
Pause uses `PATCH {"enabled":false}` or `/deactivate`; resume uses `PATCH {"enabled":true}`; DELETE
retires. Exact creation and Run-now retries recover saved owner-scoped receipts without admitting
another occurrence. Editing versions future work and preserves original occurrence requests.

### Delivery states and recovery

| State | Meaning |
| --- | --- |
| Pending | Saved delivery awaits capacity or its persisted retry time. No AI slot is required. |
| Accepted | Valid HTTP 2xx ACK received; application completion is separate. A later optional body error cannot undo this ACK. |
| Rejected | Delivery was refused by the receiver or current destination/security policy; inspect the safe reason and numeric HTTP status. |
| Expired | No initial accepted delivery was obtained before the effective deadline. An already attempted request can instead remain unacknowledged. |
| Unacknowledged | The sender lacks a final ACK and the receiver may already have acted. Do not invent success, no-effect or exactly-once guarantees. |
| Cancelled / superseded | Future eligible delivery stopped. Existing ACKs and uncertainty about earlier sends are retained; cancelling does not retract external work. |

Retries use the same event ID and immutable body with a fresh attempt timestamp/signature. Backoff
is persisted; no transport slot sleeps between attempts. An expired attempt is not started, and an
active attempt is bounded by its remaining expiry budget. A restart preserves lease/generation
fences; it cannot assume that a former sender is dead. A receiver's durable dedupe handles lost
replies. Inspect history before explicitly requesting another distinct occurrence.

HTTPS with verified certificates is the default. Environment proxy settings, redirects, shared auth
and cookies are not used. DNS results and actual IPv4/IPv6 peers are checked, with numeric pinned
connections and bounded fallback. Loopback/private/link-local/metadata destinations are denied by
default. Operators may enable a scoped local receiver using `WPR_WEBHOOK_ALLOW_LOCAL_HTTP=true`
and its exact `WPR_WEBHOOK_ALLOWED_PORTS`; plain HTTP still connects only to loopback. Explicit
HTTPS private-network grants use `WPR_WEBHOOK_ALLOWED_NETWORKS` CIDRs. Workers cannot set these
operator policies or choose arbitrary URLs. Payload, response/header bytes, attempts, connections,
owners and destinations have bounds. `WPR_WEBHOOK_TRANSPORT_SLOTS` controls the separate bounded
sender capacity; this is not another scheduler.

History exposes safe `http_status` and `timings_ms`, separating scheduled time→clock detection,
clock detection→sender entry, executor wait and the signed HTTP attempt. No raw URL, auth,
signature or receiver body appears in normal status. A local prototype or an API pass alone does
not prove installed UI/native-worker acceptance or a universal latency guarantee.

Native workers receive only the current `schedule_access` choices through the existing live
worker/run/attempt grant. `schedule_list` returns their usable targets; `schedule_create` accepts one
unchanged with a recurrence and stable retry key. The [bootstrap contract](../docs/03_Bootstrap_Auth_and_Identity_Projection.md#independent-application-inputs)
owns setup and revocation. Specialized Direct Queens keep their existing small handoff interface.
Applications prepare their own context and handle their own results; none is required to install
xPerfect's clock or native scheduling.

## Bootstrap Contract

Each worker can now carry:

- `bootstrap_profile`
  - examples: `clean-room`, `host-login`, `codex-host`, `claude-host`
- `bootstrap_bundle`
  - structured optional payload for:
    - `env`
    - `files`
    - `claude_project_mcp`
    - `claude_settings_local`
    - `codex_config_append`
    - `claude_md`
    - `agents_md`
    - `system_instructions`

Bootstrap `files` are evidence seeds by default. A reference, tool, or Skill
file that supports the worker but is not expected to appear in the final
deliverable can set `"evidence_seed": false`; the file is still copied into the
workspace but is excluded from output seed-coverage warnings.

Current behavior:

- existing host-projection defaults remain backward-compatible
- xPerfect writes a non-secret bootstrap manifest inside the sandbox for inspection
- bundle environment is available to sandboxed runs and interactive shells
- broker/client config is additive over native worker capability: Codex/Claude host and workstation
  launches must not drop browser, computer/desktop, shell, file, or MCP capabilities just because
  xPerfect projected a broker MCP
- host Codex projection includes the installed, enabled unified browser/computer MCP alongside
  supported legacy native tools. Native stdio tool processes retain the signed-in OS owner’s
  home for existing app services, unless that server explicitly sets another home. Worker
  conversation state and configuration remain isolated; this does not grant OS permissions.
  Acceptance requires an actual tool invocation from that worker.
  Cached but disabled plugins and explicit server disables stay
  disabled; the host plugin denylist remains enforced. Native startup, environment and nested tool
  settings are preserved without adding a second browser or computer-control runtime.

Native Codex stdio keeps raw tool-call and acceptance events, including the current
`rawResponseItem/completed` wire method. Cold and prepared workers enable Codex's
`omit_app_server_notification_media` feature. On validated native version `0.164.0-alpha.11.1`,
notification copies omit inline image/audio bytes while the model input and journal retain them.
The feature key also exists in reviewed version `0.162.0-alpha.17.2`; raw-media omission on that
older version has not been validated. Generated files
continue through the existing scoped artifact carrier. This prevents large historical image
messages from breaking the control connection without deleting conversation context. The reader
keeps its 8 MiB frame bound; an oversized or invalid frame reports `native_event_oversized` or
`native_event_invalid` with safe transport metadata instead of hiding the cause as a closed pipe.

## Claude Code on Amazon Bedrock

Claude workers can use Amazon Bedrock without changing the logical model recorded in xPerfect
evidence. Set `WPR_MODEL_CLAUDE_CODE` to the audited Claude model name and
`WPR_CLAUDE_CODE_PROVIDER_MODEL` to the Bedrock application inference profile ARN used by the CLI.
Provide the standard Claude Code Bedrock environment (`CLAUDE_CODE_USE_BEDROCK=1`, an AWS region,
and one supported AWS credential method). xPerfect projects those values only into the active run
and removes Anthropic API/OAuth credentials whenever Bedrock mode is enabled, preventing an
unintended provider fallback.

The provider-specific model value is intentionally separate from the logical model: callers can
continue to attest the current managed Claude model, while Claude Code invokes the metered
application inference profile selected by the administrator. Do not place AWS credentials in
bootstrap instructions, worker metadata, source control, or logs.

## Universal AI Endpoint

xPerfect exposes an authenticated OpenAI-compatible conversation surface that is independently
callable by LibreChat or any ordinary Chat Completions client:

- `GET /v1/models` publishes exact model IDs, effort/context metadata, native capabilities,
  and whether the harness exposes incremental assistant text without starting a harness or probing
  sign-in; readiness is `not_checked`. Explicit connection checks use
  `GET /v1/models?check_readiness=true` to check each harness once. Both modes require the same
  authentication and owner scope; actual admission retains its own checks.
- `POST /v1/chat/completions` accepts standard bearer authentication plus `model`, `messages`, and
  optional streaming; common tuning fields are tolerated for portability while unsupported
  client-orchestrated tools and response shapes fail visibly
- `POST /v1/responses` exposes the same authenticated model/session core using the standard
  Responses request, response, streaming-event, and `previous_response_id` shapes
- provider usage-quota and rate-limit failures remain explicit across both APIs: non-streaming
  requests return OpenAI-compatible HTTP `429` errors and streams emit `rate_limit_exceeded`
  failure events; xPerfect never substitutes another model or silently retries an authoring turn.
  Native Codex `codexErrorInfo` can nest the status under `httpStatusCode`; the shared failure
  classifier reads this field only inside trusted provider control events. It does not infer a
  rate limit from assistant/task JSON or prose, invent a reset time, or label HTTP 429 as weekly
  quota exhaustion. A later fallback authentication failure remains a separate cause.
- an audio-eligible messaging request may set the compatibility header
  `X-Viventium-Audio-Eligible: true`; xPerfect then
  requires the native Codex or Claude worker to return a versioned `skip` or `eligible` delivery
  disposition through its private structured-output schema. The visible answer stays plain text,
  while Chat Completions returns the decision under
  `provider_specific_fields.viventium.delivery_disposition`. Missing or malformed required output
  fails closed instead of silently permitting audio. Requests without this structural header keep
  their existing response and streaming behavior.
- `/v1/requests/{request_id}/activity` and `/cancel` add resumable activity and explicit lifecycle
  control without making those extensions prerequisites for a standard client
- Cancelling a provider turn that awaits input settles only that turn after its compute is released.
  Repeating cancellation finishes an interrupted request-to-run handoff and preserves later turns.
  Cancellation follows the saved run's worker when recovery has rebound the conversation session.
- provider, MCP, capability-broker, and runtime administrator credentials are separate
- streamable-HTTP MCP requires its configured MCP service credential in local and enterprise
  deployments; stdio remains process-local and does not add an HTTP authentication layer

An optional trusted host integration may attach owner/session/workspace metadata and a brokered
capability bundle. Any bundle
that can project environment or harness configuration must carry a fresh HMAC signature generated
with `VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET`; a provider bearer alone cannot authorize it.
The default generic endpoint identity is workspace-only. A trusted local deployment can explicitly
grant full access, which disables harness sandbox and approval gates.

Conversation streams never expose hidden chain-of-thought. Claude stream-json currently provides
assistant-text deltas. Codex exec JSON currently provides safe activity while running and the
assistant message on completion; this is declared as `incremental_text: false` in model metadata.
The trusted Voice surface is a scoped exception: native App Server final-answer deltas can use
the existing public-envelope decoder before completion. Commentary stays private, unknown phases
wait for completion, and exact thread/turn/item identities bind the text. See the
[native call speed contract](../docs/capability-matrix.md#native-call-speed-options).

Grok audio-eligible conversation turns can stream the model's public response before terminal
completion. The existing output schema puts graph and audio controls before `content`; the decoder
checks the actual controls and JSON string boundaries. Content-first and skipped envelopes
stay terminal-only. Complete eligible narration for a declared graph transfer closes a public
audio part before the separate transfer delta; this does not commit the graph action.
Invalid headers never authorize early text; a malformed later
suffix produces a typed failure. Credentials and local paths remain
redacted. Early text uses the same public sanitation as terminal text; unresolved private citations
wait for their native source receipt. Complete eligible narration before native tools remains in the same native terminal
answer, in order, so saved-message evidence does not erase spoken text. An accepted public part
also fences replay by fallback or context recovery. Terminal evidence and source/digest checks
remain unchanged; a changing accepted prefix fails without replaying a replacement answer.
Native tool events retire their completed public buffer. A later generation reuses the exact prompt
and turn fence without parsing that retired empty buffer or deduplicating across intervening tools.
Declared terminal graph decisions remain terminal; private reasoning and interjections add no public parts.
Native activity reads use the observed file size as their byte boundary, so concurrent writes
remain for the next read and cannot replay a text delta already delivered.

## Curated Library Registry

Library items are versioned, non-secret bootstrap extensions for native Codex and Claude workers.
They do not add a second plugin runtime and cannot execute installer shell commands. A manifest must
pin its stable id, semantic version, activation hash, structured HTTPS/curated provenance, supported
profiles, requested scopes, closed non-secret JSON configuration schema, exact dependencies,
declarative `bootstrap_contract` health probe, and safe upgrade/remove behavior. The profile adapter
accepts only workspace files and the native profile's reviewed project configuration fields.

The supported lifecycle is:

1. An authenticated user or MCP worker may submit a validated proposal at
   `POST /v1/library/proposals`; this does not publish or install anything.
2. A tenant administrator with the separately enabled admin API reviews the tenant queue and uses
   `POST /v1/admin/library/proposals/{proposal_id}/review`, or a trusted registry publishes an
   already-reviewed manifest with `POST /v1/admin/library`.
3. A user or worker prepares a workspace enable/upgrade. The authenticated human reviews an
   immutable, time-bounded pending change in the browser.
4. Confirmation revalidates the complete dependency graph, runs the profile adapter and health
   probes, then atomically updates the reusable bootstrap and records the grant/probe evidence.
   Any adapter or probe failure rolls the transaction back and leaves the confirmation pending.
5. Removing the newest grant restores the exact prior bootstrap. Catalog disable/removal is a
   soft, audited lifecycle operation and fails closed while active grants or available dependents
   remain. Removed versions cannot be restored; publish a new version instead.

MCP intentionally exposes proposal, browse, prepare-upgrade, and remove-grant tools, but no publish
or confirmation tool. Scope widening during an upgrade is rejected even if proposed by a worker.

## Run

```bash
cd runtime_phase1
uv sync
uv run uvicorn workers_projects_runtime.api:create_app --factory --reload --port 8766 --no-access-log
```

Open:

- `http://127.0.0.1:8766/ui`
- `http://127.0.0.1:8766/docs`

Run MCP:

```bash
cd runtime_phase1
export GLASSHIVE_MCP_API_KEY="<dedicated-mcp-service-token>"
uv run python -m workers_projects_runtime.mcp_server --transport streamable-http --port 8767
```

## OpenClaw Runtime Release Contract

xPerfect's workstation image installs OpenClaw `2026.7.1-2` through the committed
`runtime_locks/openclaw/package-lock.json`, not through a mutable global package spec. The reviewed
lock pins the npm tarball integrity and applies the production `fast-uri@3.1.3` override. Image
generation validates the lock checksum before Docker starts, uses `npm ci --omit=dev`, verifies the
installed OpenClaw and `fast-uri` versions, and then exposes the locked CLI.

xPerfect also verifies the OpenClaw version when a workstation container is selected. Host-native
OpenClaw workers require exactly `2026.7.1-2`; older, newer, missing, and unverified installations
fail closed with recovery guidance. Both workstation and host launch environments force
`OPENCLAW_DISABLE_BONJOUR=1`, because loopback gateway binding alone does not suppress native mDNS
advertising.

The 2026-07-21 reviewed production graph reports 0 critical, 0 high, and 6 moderate npm audit
findings. A custom workstation image is supported only when it contains the same reviewed OpenClaw
runtime; xPerfect checks it before starting a worker.

## Link Lifetime Defaults

- `GLASSHIVE_LINK_REF_TTL_SECONDS` default: `0`, so `/r/{ref}` and `/v1/link-refs/{ref}` short
  links do not expire by default. Positive values expire short refs after that many seconds.
- `GLASSHIVE_LINK_REF_STATE_PATH` default: `<state-root>/glasshive/link_refs.sqlite3`. When the
  runtime emits `/r/{ref}` links that point at the separate xPerfect UI service, the runtime and UI
  must use the same local link-ref state path on the same host or supported shared storage.
- `GLASSHIVE_SIGNED_LINK_TTL_S` default: `900` seconds for raw signed-token compatibility URLs.
- Enterprise short refs are authenticated owner-scoped routes. Opening a durable `/r/{ref}` mints a
  fresh bounded worker-view session cookie; the durable ref itself is not an active compute lease.

## Test

```bash
cd runtime_phase1
uv run pytest -q
```

## Current Boundary

This runtime owns the universal harness/provider boundary. It does not depend on LibreChat storage
internals. Connected capabilities are projected by an authenticated broker bundle; xPerfect
remains the native execution owner and LibreChat remains one optional conversation/UI consumer.

## Managed file API

- `GET /v1/storage`: effective owner policy, retained logical bytes and reservations.
- `POST /v1/file-uploads`: create an idempotent draft receipt (`draft_id`, `name`, optional `relative_path`, `size_bytes`, `idempotency_key`).
- `PUT /v1/file-uploads/{id}/content`: stream exact raw bytes; retry the same receipt after interruption.
- `GET /v1/file-uploads?draft_id=...` and `DELETE /v1/file-uploads/{id}`: reload and cancel unattached drafts.
- `POST /v1/workers/{id}/files`: bind `upload_ids` with an `idempotency_key`, optionally within a visible `directory`.
- Worker Files routes provide listing, directories, revision-checked move, trash/Undo, streamed download and ZIP export.
- Schedule requests accept `file_upload_ids`; manifests and pins survive draft expiry and process restart.

Core Main can select authorized `fileIds` for Message, Queue, or Steer. The host transfers only
those selected files and supplies their accepted Native upload versions. Each follow-up uses the
same queued-run input manifest, including source file ID, order, group index, and `input_kind`:
`original` carries the original bytes; `extracted_text` carries the stored UTF-8 extraction.
Steer pins that manifest with its queued replacement before stopping the target. The existing
control fence prevents replacement admission until the exact stop is confirmed. Files are
projected only for the admitted run, and a terminal winner publishes no replacement files.
Accepted run pins and retained bytes remain after replacement cancellation, as for Message and
Queue. Permanent file-admission failures settle the run; transient file-service failures use the
existing admission retry path.

Exact Steer also records the retired run's terminal callback in the same durable lifecycle
transaction as its replacement. Clients can settle that run's activity without cancelling the
accepted work or replacement. Callback replay retains the same identity; a retired run does not
gain the replacement's terminal result authority.


All operations use the runtime owner/tenant and worker authorization boundary. File writes reject view-only identities. Managed source projection requires an explicitly trusted managed-storage root. API admission monitoring alone does not enforce a native disk quota. Standalone startup loads only an explicit `VIVENTIUM_ENV_FILE`, if provided.

## Standalone coordinator

The coordinator modules reuse the native conversation provider, durable delegation queue, scoped
native MCP binder and existing lifecycle. The default assistant is resolved from the exact runtime
configuration; advanced deployment configuration may provide `GLASSHIVE_COORDINATOR_CONFIG_JSON`
matching `CoordinatorConfig`. Invalid choices fail explicitly. No LibreChat or Viventium callback
configuration is required.

Canonical generic instructions and tool descriptions live under
`src/workers_projects_runtime/prompts/coordinator*`; `prompt_manifest()` exposes their hashes.
Package these source files with the runtime. Focused deterministic checks are
`tests/test_coordinator.py`; fixed exact-model evaluation cases are
`tests/fixtures/coordinator/evals.json`. Native tool execution and real browser acceptance are
separate release gates.

### Direct image transport and history

Direct API profiles preserve the selected provider, model, scope, original text, native tools,
receipts and all authorized image bytes. The existing SDK request hook sends each exact user
image once per request and retains each repeated occurrence as a data-only content reference.
Identity includes full SHA256, MIME type and vendor settings; distinct image settings stay
distinct. The SDK receives an identity label beside the full image. No image is resized or
summarized, and text that merely claims an image hash grants no file or image access.

The same verified SDK history prevents another binary copy from being appended on each text
turn. Existing raw journals remain intact. Projection uses only actual typed bytes retained in
the same provider/account scope or in the current authorized input; it keeps a full available
copy even if an earlier occurrence was removed by a history projection. A missing current user
carrier fails closed. Tool calls/results and non-image media stay unchanged. Structured image
projection metrics report unique images, duplicate bytes and restored images without logging
private image data.

HTTP413 means that the provider rejected a request as too large. It does not prove a model token
limit or justify a fresh worker, context loss or action replay. The existing non-retryable
rejection family retains that transport distinction. Owning checks:
`tests/test_direct_media_projection.py`, the Direct runtime/current-state/archive tests and
native-media authority tests. Actual provider acceptance and client delivery remain separate
release evidence.
