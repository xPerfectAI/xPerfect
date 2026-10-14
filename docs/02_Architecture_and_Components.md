# xPerfect: Architecture and Components

## System Shape

xPerfect is split into four layers:

1. `Control Plane`
2. `Worker Runtime`
3. `Execution Substrate`
4. `Client Adapters`

## 1. Control Plane

The control plane owns:

- projects
- workers
- runs
- events
- lifecycle orchestration
- live worker state

Current implementation:

- FastAPI API
- SQLite store
- project-first web UI

Enterprise VM mode adds an `AuthContext` before API/MCP handlers. When
`GLASSHIVE_ENTERPRISE_MODE=true`, the control plane requires a service-authenticated LibreChat user
assertion, derives `tenant_id` and `owner_id` server-side, and scopes project, worker, run, event,
UI, watch, and artifact queries by tenant plus user. The deployment tenant is pinned by server
configuration; a mismatched inbound tenant assertion fails closed.

Signed Core file couriers use the same verified account owner as worker actions.
Upload routes validate the existing service assertion and each request's nonce;
file receipts and manifests retain their owner, revision, size, and hash checks.
Human file requests without an assertion keep their existing authentication path.
Local runtime source roots include Core uploads and the owning database's managed
file store. File projection keeps the existing source, owner, size, hash, and
symlink checks. Enterprise deployments retain their explicit source roots.
Selected-file action preparation reads fresh action availability through the existing
owner-scoped work route's `projection=actions` view. It keeps pending native-input
restrictions and rejects unavailable actions before file transfer, while omitting
artifact-link writes and history materialization. Native execution rechecks the
current owner, action state, generation and immutable file receipts before binding.

A native conversation's stream and detached observer retain the same admitted request
during typed SQLite BUSY or LOCKED contention. The first public-text receipt must persist
before public cursors, redactor state, or bytes advance; contention leaves those pending
so exact prefixes and public-part completion markers are emitted once. They use the existing heartbeat, pending
observer and original absolute deadline; observation does not replay inference or revoke
the author. Other database failures retain their existing error behavior.


### Private structured response courier

A trusted host may declare `response_contract` in the existing signed zero-tool bootstrap
capability: version `1`, visibility `private_machine_state`, format `json_object`. Admission
requires the internal `system` actor and `system` origin, read-only stateless access, no native
or graph tools, and no audio. Unsigned request metadata cannot enable this capability.
The admitted request persists the contract. Validation accepts the same single optional JSON
code fence as the continuity consumer, without rewriting the terminal bytes. Its canonical
completion reads the complete JSON
object from the exact tenant, owner and native run terminal, without converting private state
into a public answer. Streaming emits this object only at terminal completion. Invalid objects,
duplicate keys, missing terminals and changed ownership fail closed. This preserves authorized
private references needed by the next machine consumer. Public answer, run-output, activity and
error redaction remain unchanged; private result retrieval retains authenticated owner checks.

Native conversation sessions use the complete identity `(tenant_id, owner_id, conversation_id,
agent_id, actor_kind, origin)`. Interactive turns, scheduled wakes and worker callbacks keep
separate sessions within the same conversation; the same complete identity reuses its session.
Startup removes obsolete complete four-field unique indices only after confirming the six-field
unique constraint. Legacy table rebuilds exclude those indices from replay. The schema transaction
preserves every session ID, history and dependent request or visible admission, as well as unrelated
indices, triggers and foreign keys. Reopening an already scoped store applies the same reconciliation
without merging or deleting sessions; failed migration rolls back and concurrent startup is serialized.

## 2. Worker Runtime

This layer decides how a worker actually executes tasks.

Current worker profiles:

- `codex-cli`
- `claude-code`
- `openclaw-general`

Design principle:

- all worker profiles should share the same project/run/lifecycle API whether they execute in a
  Docker workstation sandbox or in host-native mode

### Native generation and restart ownership

A native control operation targets the recorded run, lease, process identity, attempt and host-slot
identity. A stale Stop, timeout, rejected-start cleanup or old process `finally` must not signal,
clear or release a replacement generation, including a replacement for the same run. Missing PID
metadata alone is not proof that native work stopped. Historical results and deadline cleanup use
the worker recorded on the run even if the conversation session now points to another worker.

Managed shutdown stops the exact owned generation and proves it absent before releasing its lease
for retry. Failed cleanup or unknown absence retains the lease. Processor exit cannot release that
shutdown-owned fence. Configured live-generation restart adoption keeps its separate ownership path.
Recovery from a complete native response without an exit marker must confirm the exact process
stopped before writing a synthetic exit marker. Failed or unknown stop leaves recovery pending.

Automatic crash/capacity retry projects worker readiness in the same database transaction that reads
durable Pause intent. A committed Pause or paused run remains paused even if a crash left the worker
row stale. Retry recovery cannot override a Work Stop, compute-release claim, termination boundary or
concurrently admitted generation.

Recovery inventories select worker IDs in the existing creation order, then load each current
worker through the normal store read. Lifecycle reapers select IDs with their existing state and
compute-release checks before loading retained instructions. Candidate selection is not permission
or lifecycle proof: current eligibility, ownership and generation fences still govern every action.
This avoids loading all historical bootstrap bundles before recovery can process its first worker.

Active-work reads reconcile running generations in a consistent read snapshot first. Healthy
generations require no writer reservation; an invalid generation still uses the existing fresh
transaction and ownership fences before correction. Signed links reuse a fresh scoped reference
before expiry cleanup, avoiding a write on valid reuse. Expired references and tokens keep the
normal cleanup and creation path; authorization and revocation checks remain unchanged.

Conversation capacity refusals default to a one-second `Retry-After`, the smallest valid HTTP delay.
The same trusted lane reaches durable reservation and retry backoff; bounded jitter is unchanged.
Mission and delegation retry keep their five-second default. Existing explicit host-busy or general
retry settings remain authoritative; admission, durable capacity and cancellation gates are unchanged.

Successful native Grok model catalogs may be reused for at most 30 seconds, keyed to the installed
binary and current environment, authentication and configuration file identities. Changed identity
refreshes immediately; empty or failed lookups are not cached. This affects catalog discovery only,
not live provider authentication, model selection authority or capability grants.
Concurrent lookups with the exact same scope share one in-flight result, including failure; a later
lookup retries a failed probe. No global mutex is held while the native CLI is running.

Verified selected output files retain their typed publication envelope on a completed conversation
graph transfer as well as an assistant response. The envelope keeps the original publisher, owner,
conversation, source message, stream, logical turn, invocation, run and attempt. The host retains
transfer metadata until the successful visible final response; it does not emit intermediate text
or attachments. The existing host attachment importer remains the authorization and byte-validation
boundary. Refused, hidden and interrupted final paths do not publish retained files.

Codex workers use native JSONL events for both fresh execution and same-session resume.
Resume preserves the existing model, effort, workspace and authorization. The same run-evidence
recorder then retains native tool results and screenshot bytes for callbacks; plain console
reporting must not silently remove that evidence from resumed work.

## 3. Execution Substrate

Current phase-1 substrate:

- Docker-backed workstation containers
- persistent home and workspace mounts
- noVNC desktop view
- terminal bridge

Approved host-native substrate:

- local process execution on the user's main computer
- no Docker and no sandbox
- local Codex, Claude, and OpenClaw CLIs
- user-scoped workspace root
- structured action audit and work-log visibility

Current honest boundary:

- good owner-controlled local isolation
- not yet a hostile multi-tenant boundary

Azure enterprise v1 keeps this Docker substrate on one tenant VM. It provides application-level
per-user ownership separation and idle compute reaping, not hostile cross-tenant sandboxing.
Enterprise worker bootstrap is clean-room with respect to the VM account's host Codex, Claude, and
git auth files; provider access is projected through explicit allowlisted environment variables.

## 4. Client Adapters

xPerfect should be reachable through:

- direct HTTP API
- MCP
- future broker/integration adapters

Current implemented adapter:

- thin MCP wrapper using `streamable-http`, `stdio`, and compatibility `sse`

## Why This Shape

This shape preserves the important separation:

- xPerfect owns worker runtime truth
- Viventium or LibreChat are clients
- provider auth and connected-account systems can be bridged in without changing the xPerfect core model

## Native Grok adapter contract

The native session-title helper defaults to `grok-4.7` through the CLI's existing
`GROK_SESSION_SUMMARY_MODEL` setting. An explicit native helper-model setting remains authoritative.
This helper has no reasoning-effort field; the selected conversation model and effort remain
unchanged. Native title generation runs asynchronously and does not gate the public response.

`grok_runtime.py` supplies Docker and host adapters on the existing worker interface.
Host runs retain `HostNativeCliMixin` process supervision, generation fencing, capacity,
Pause/Stop and restart handling. Container runs retain `BaseCliWorkerRuntime`. Both execute
the same standard-library ACP bridge in the worker's private home; neither creates another
scheduler or requires Viventium/LibreChat.

The bridge uses `grok agent --no-leader --model <exact-model> stdio`, ACP v1 initialization,
explicit advertised noninteractive authentication, exact native session new/load, prompt/update,
and cancellation. It verifies the returned model and requested reasoning option before prompting.
Fresh sessions also receive the exact configured model through native `session/new._meta.modelId`.
The first session model menu is a snapshot. If the returned model differs, the bridge asks
the native model selector to apply the exact selection; native selection owns its bounded first-catalog
wait and refusal. The bridge still verifies the returned exact model before any user prompt.
Exact resume and later authority reload parameters remain unchanged. A native hint does not replace
the returned-model, requested-effort or native-tool-inventory checks. The existing authorized
catalog fallback remains: when `grok-4.7-build-fast` is absent and native `grok-4.7` is offered,
the bridge selects `grok-4.7` and verifies the requested effort and pinned authority. Other
unsupported selections fail before a user prompt.
`WPR_MODEL_GROK_BUILD` must contain the configured model; other model substitutions are refused.
`WPR_GROK_BIN` selects the host executable (`WPR_GROK_CONTAINER_BIN` selects the container executable) and `WPR_GROK_REASONING_EFFORT` selects an optional
native-advertised effort. Source protocol reference: [official Grok agent-mode guide](https://github.com/xai-org/grok-build/blob/4247f661689354b831191f11eeeac8424993fe3d/crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md).

An admitted conversation attempt sets native `GROK_SUBAGENTS=0` and `GROK_WORKFLOWS=0`
because its process closes at turn end. This also applies when loading a saved native session.
These flags remove the native `GrokBuild:task` subagent tool and the separate `Workflow` tool,
including their foreground use. The existing MCP worker delegation remains available for durable
background work when authorized by the owner. Mission runs retain their configured native Task
and Workflow capabilities. Native background shell commands
still have process-local lifetime; they do not gain durable worker completion or callback delivery.

Native updates, exact session identity and terminal results use separate typed envelopes. Unknown
native child events remain native data; they do not invent xPerfect workers. Usage is unavailable
unless independently projected from an authoritative native event. A terminal error, cancellation,
protocol mismatch or exceeded output limit cannot become a successful empty result.
The conversation output schema describes only the final host-graph result. It does not replace
or restrict authorized native function and MCP execution before that result. An assistant result
requires a null transfer name; a graph-transfer result requires one listed transfer name. Host
and container Grok adapters declare these pairs with complete disjoint `anyOf` branches while
retaining the top-level fields used by streaming. In an admitted conversation with native tools
enabled and recoverable messaging controls declared, the same contract is rendered into native
system authority before the exact
pinned tail. ACP receives a minimal object constraint with the existing fields, types, enums and
required fields. The wire projection omits descriptions and composite branches; the full declared
schema still controls public streaming, interjection and strict final parsing, including valid
assistant/transfer pairs. Declared and actually transmitted wire hashes remain distinct. Native
tools retain their normal protocol; framing failure never replays the accepted goal or completed
effects. Restricted native-tool-disabled conversations keep their original constrained metadata;
graph-only conversations without messaging controls, missions without a conversation schema and
other native adapters retain their current behavior.
The command and provider-authority receipt use one composer, check the transport mode and reject
changed authority, schema or tail. This does not grant tools, choose a transfer, alter permissions
or make an empty answer proof of completed work. Managed host Prompt Workbench references the
shared producer, Grok projection and native authority composer as related configuration.
Configured primary and fallback Grok workers pass catalog-supported effort through the existing
`WPR_GROK_REASONING_EFFORT`; exact advertised native-model and effort verification remains unchanged.
The bridge accepts the native `_x.ai/session_notification` envelope with the same parent-session
and event-identity checks as ordinary updates; persisted update names do not define the live wire.

After a successful result is emitted, the bridge uses standard `session/close` only when the
native initialization advertises its close capability. The request waits at most two seconds;
cleanup failure cannot replace that result. The exact session key and history remain available
for the next normal load. Transport cleanup closes stdin and allows up to half a second for EOF
exit before the existing exact-process termination fallback. These waits are bounded cleanup
costs and can delay final process completion; they do not gate the already emitted result.

Controls use a private, run-and-attempt-scoped mailbox behind the ordinary owner-authorized control
plane. Permission, question, elicitation and plan-exit requests require a response; absent or expired
responses cancel rather than approve. Interject reports `queued`, never consumed. The private xAI
interject extension requires a reviewed executable SHA-256 in `WPR_GROK_REVIEWED_BINARY_SHA256`;
or `WPR_GROK_CONTAINER_REVIEWED_BINARY_SHA256` for the container artifact. The bridge checks the actual executable bytes on its substrate; initialization alone does not advertise that extension. Stop continues to use the existing exact
process-generation stop path.

Artifact mismatch disables only optional interject; ordinary generation still uses the selected
native model and authority. The runner hashes the executable once at startup with a bounded buffer,
then publishes effective interject capability in `grok.session.started`. The existing active-session
receipt preserves that boolean with its exact run, attempt and native session. Missing, stale or
unreviewed capability is unsupported; a cohort probe never rehashes the executable. Pending controls
cannot pass a canceled/stopped mailbox or the final ACP transport write guard.
Current-turn interjection uses that exact active native session, including stateless or restricted
turns that intentionally do not save continuity. It neither saves that session for later reuse nor
changes the existing saved-session binding for permission and cancel controls.

These source contracts do not establish installed Grok readiness. Each advertised deployment must
prove its exact executable, native account/model/tool flow, container image, control UI, and restart
journey. Fixture-process tests establish only deterministic protocol and supervisor behavior.

Grok uses the same authenticated worker API and MCP server. `GET /v1/workers/{worker_id}/native-control`
returns pending native requests and the exact active run/attempt; `POST` accepts `interject`, `cancel`,
and `permission` against that pair. `worker_native_control` exposes the same contract to MCP clients.
The workspace UI displays native permission options, questions, plan approval and MCP elicitation.
A queued interjection is an acknowledgement, not proof the model consumed it. Native cancellation
requests are distinct from the existing interrupt/terminate lifecycle, which confirms process exit.
Expired permission requests cancel; unknown controls and stale attempts fail explicitly.

Natural permission replies use the current external human source in the existing signed request
body. The original source receipt must follow the exact native offer in the current attempt.
Only an actually offered `allow_once` or `reject_once` choice is accepted through this path.
Missing, ambiguous or stale provenance fails explicitly; authenticated owner forms retain all
existing choices. The existing action reservation, native mailbox and exact acknowledgement
remain authoritative. An initial goal, callback or generic Resume does not create consent.
Typed Write metadata can supply a safe filename for speech; full operation details remain
available separately. The Queen interprets the human reply; the runtime verifies its authority.

Native question and permission speech includes the actual offered choice labels within the
existing canonical limit. Permission presentation reuses the version-1 native terminal
tool's model-authored action description when available, with the original title as fallback.
Exact commands and inputs remain in the native control details; the existing Active Work form
also retains the original title as field description. Foreground permission envelopes carry the
original title separately from the spoken prompt for typed detail consumers.
Response values, question keys, fingerprints and consent controls do not change. If combined
text exceeds the existing canonical limit, the original valid prompt and complete form remain;
oversized forms do not promise that every label is spoken.

Codex asynchronous conversational questions retain their actual titles and offered answers in
the final public response. Only a matching accepted native call from the exact admitted turn is
projected, within a whole-question limit. Existing response identity and replay preserve one
presentation. The answer is a normal next turn in the same conversation; native acceptance is
not a human answer, worker pause, resumed work, or permission approval. Graph transfers and
private machine-state responses retain their existing delivery boundaries.
The native turn reader validates either direct admitted input or the existing exact context
envelope against the admitted instruction digest. It then binds the whole dispatched input to
one exact text part in one native turn, including turns with retained image parts. Unknown
envelopes, changed input, duplicate matching parts or frames, and ambiguous turns remain unavailable.

Native attention resume and queued preclaim reservation retire only the exact latest ended
attempt whose linked lease is released, with no active worker lease. They clear current-generation
fields while retaining attempt history and the original start time. Capacity is still reserved
before a fresh attempt is claimed; a refused reservation rolls retirement back. Missing, mismatched,
unended or live generations remain rejected. Existing resume action receipts remain idempotent.

An explicit attention Resume can supersede an unfinished compute-only cleanup for the exact
never-started run it re-admitted. The same rule covers a fresh eligible queued continuation after
the exact attention target has settled: Retry, Message and Resume use the existing queue selection,
including workers that are ready rather than starting. Unknown, foreign or still-pending attention
targets cannot authorize this cancellation. Queue admission cancels only that obsolete needs-input claim,
under the cross-process lifecycle lock and exact compare-and-set fences, and wakes the existing queue once.
This does not report compute termination or runtime invocation. Ordinary admission still owns
process identity, capacity and fresh capability proof. Stop, pause, active or uncertain startup,
and foreign scope remain fenced. The lifecycle lock prevents another executor's in-flight cleanup
from racing this cancellation; its later retry cannot reuse the canceled claim.
The queue wake probes that lock without waiting. A busy acquisition leaves the already accepted
queued run and exact claim intact for the next existing wake; it does not report a false Resume
failure. Only acquisition busy is deferred. Store or runtime errors inside the guarded operation
remain errors, and ordinary control lock deadlines remain unchanged.

Attention cleanup also covers a run blocked before its first invocation. If the host reports a
retained process as absent, cleanup binds that observed native run only when Store proves the same
worker's terminal ownership. The existing host cleanup repeats exact session, process and child
absence checks. A pending compute-only attention claim can finish under the lifecycle lock without
waiting its recovery TTL; active, unknown, malformed or foreign native observations remain fenced.
This lets a never-resumed blocked worker complete cleanup and accept Stop without inventing a start
or choosing an unrelated latest run. A timed-out attention target may finish the same pending
cleanup through the existing reaper without resetting its claim TTL. Host cleanup binds only the
adapter's exact observed absent terminal generation; a newer never-invoked terminal queue record
does not replace that native identity. Queued replacements stay queued until ordinary admission.

Account work reads reuse the existing opt-in native timing telemetry
(`VIVENTIUM_VOICE_LOG_LATENCY`) through the runtime's existing Uvicorn log handler. Phase entry and exit measure running-state reconciliation,
inventory list/count, payload projection, pending native input, view references and artifact links.
Records contain static phase names, ephemeral thread IDs, elapsed time and exception classes;
they do not contain account identity, files, prompts, link tokens or exception text. Timing never
changes authority, results, retries or request deadlines, and telemetry failures do not replace
the original result or error.

An authenticated host may send context to its exact running conversation invocation through
`POST /v1/requests/by-invocation/{invocation_id}/interject`. Its strict body binds `stream_id`,
`message_id`, admitted `body_sha256`, stable `delivery_id`, exact `text` and `text_sha256`.
The provider resolves the current owned session, worker, run and attempt itself. Foreign or changed
identity is refused; terminal, stopped, restored-held or superseded requests return `not_running`.
It does not reconcile, launch, cancel or replace a run. Unsupported native controls, including an
unreviewed interject extension, return `unsupported`. Selected runtime capability owns support;
the host must not infer it from a provider label.

Automatic cohort input keeps the accepted output contract's real framing owner. Valid audio
delivery controls retain native generation framing. A non-audio graph contract can also accept
input when the live native owner explicitly advertises `interject_graph_output` and returns the
same normalized graph control for that exact run and attempt. Codex app-server issues this
capability only when its actual turn output schema equals the existing canonical graph schema.
Its complete native items supply validated previews; graph output stays behind canonical terminal
content for public delivery. Speech eligibility is unchanged. Missing, malformed, mismatched and
freeform contracts retain unsupported automatic input; unsafe unframed generation replay does
not acquire this capability. Stop, latest-invocation and owner fences apply to both paths.

The response binds the invocation, delivery and text digest with `queued` or `pending` and the exact
run, attempt and native mailbox message ID. Stable controls reuse the private retained mailbox
receipt and reject a changed body for the same delivery identity. A pending control remains pending;
transport retries must replay its identity. Neither response means consumed or publicly presented.
The host retains its accepted contribution and normal model-owned follow-up decision as fallback.

Host-native Claude Code conversations use the same exact-attempt mailbox through native
`PostToolBatch.additionalContext` and `Stop.additionalContext` hooks. The reviewed CLI is
`2.1.285`; other versions retain their existing path until their hook behavior is reviewed.
SessionStart binds the real native session, and SessionEnd closes it. An interjection remains
pending until a native safe boundary returns its exact text as hook context; its retained
`queued` receipt names that boundary and does not claim model consumption or public delivery.
Stop feedback continues the existing process and conversation rather than opening a sequential
SDK user turn. Native cancellation still uses the existing supervisor; a terminal or foreign
run/attempt/session cannot accept more input. Inherited subagent hooks cannot drain the Main
mailbox. Existing configured hooks and settings are retained.

This connection is unavailable when native hooks are disabled, tools are restricted, or workspace
sandbox access has not proved the private hook context path. It does not advertise automatic
unframed graph-output injection. The pinned CLI's loopback protocol fixture proves both context
boundaries reach the next request in the same process/session, with fabricated model responses.
That deterministic proof is not live Opus inference, output-quality acceptance or full parity.
Live selected-model acceptance remains required before claiming those outcomes.

Host-native Codex conversations can use the selected CLI's native app-server inside the existing
run supervisor. The installed schema must expose `turn/steer` with `expectedTurnId` and exactly the
reviewed server-request methods. The reviewed CLI is `0.162.0-alpha.17.2`; other versions retain
exec until their native request behavior is reviewed. Only exactly
translated exec settings select this path; unknown flags and approval routes without an owner
handler retain exec with no interject capability. Model, effort, account environment, developer
authority preflight, workspace roots, network policy, tools, images, output schema and native
history retain their current bindings. The native start receipt must confirm the model and
requested policy before authoring. Native items, tool results, team events and usage reach the
existing readers. Interjection uses one active thread and turn through the existing guarded
mailbox. Its ACK proves queued input, not model consumption. End, lost process identity and Stop
withdraw live capability; changed deliveries and stale attempts cannot enter another turn.
Server requests retain the selected exec's native behavior, including attached native bees:
MCP elicitation receives a typed cancel; native interactive `request_user_input`, dynamic client
tools and approval requests receive the same explicit exec-mode unsupported response. This is
the [selected native exec contract](https://github.com/openai/codex/blob/rust-v0.162.0-alpha.17.2/codex-rs/exec/src/lib.rs#L2002-L2211),
not an interactive question capability. Existing worker async question delivery remains in its
canonical native evidence and owning host flow.

Claude's existing duplex stream-json mission relay supports its declared input and elicitation
contract. This adapter does not yet advertise current-turn interjection: a sequential native
message queue or replay ACK alone does not establish current-turn absorption. The host retains
the same durable fallback for this unsupported path. No interrupt, replacement run or model
substitution is used to simulate that capability.

An observed mission permission request also publishes the existing attention callback with its
exact run, attempt, native session, request fingerprint and expiry. Active Work presents the
native options without changing the running compute state or releasing its lease. Owner responses
reuse the signed work action and exact-attempt mailbox. An unconfirmed native response stays
pending. Its retained acknowledgement can settle the same submitted operation after the question
expires or disappears; recovery does not send another approval. A new response to an expired
question, a changed choice, or a stale generation is rejected. The existing resolved event
withdraws that question before publishing the next live permission request. If a previously
submitted operation has lost its local acknowledgement, its exact reserved action remains
confirmation pending; recovery never treats missing confirmation as consent or a rejection.

An admitted mission's approval wait uses its exact run-local capability grant expiration. Every
question shares that absolute deadline; opening another question does not renew it. This value
comes from validated admission, not worker-supplied bootstrap data. Live conversations and runs
without that admitted bound retain the 60-second wait. Stop still cancels immediately, and no
approval choice or capability is granted by extending the wait. The callback-delivery retry
horizon is separate from the mission authority and is not an execution deadline.

For an admitted native conversation that supports safe-boundary continuation, the existing
conversation schema describes each response as useful user-facing content rather than declaring
that every response terminates the native turn. Its registered developer-instruction producer
exposes the full contract in the same request-pinned authority and Workbench lineage. The Grok
adapter selects this contract only for its admitted, tool-enabled conversation path; restricted
and unadmitted paths retain the terminal-result contract. Existing typed graph, voice, tool,
owner and Stop controls are unchanged.

For a Codex conversation with admitted delivery controls, a valid silent terminal envelope
retains earlier validated public assistant content from that same native turn, including across
context compaction. Only the current turn can contribute; failed or unfinished turns, unframed
working text and invalid envelopes do not become answers. A genuine empty-and-skip result remains
valid silence when no public answer preceded it. Ordinary worker report selection is unchanged.

On same-turn continuation, the model uses the complete earlier goal and work with new input,
adds useful information or corrections, and does not replay its answer unless the user asks.
It may use the existing empty-content/voice-skip envelope when no extra public content is useful;
silence cannot replace an unanswered user request. If a later native response is silent,
the existing ordered-parts owner retains earlier validated audio-eligible narration and its
eligibility for final-only channels. Every nonempty validated text-only predecessor also stays in
canonical text/history. `grok.result.deliveryDispositionRanges` carries the host's existing
UTF-16 `start`/`end` plus typed `disposition` shape, derived by the native parts owner from each
model envelope. This sideband does not add fields to model output schemas. Mixed narration and
text-only content use a conservative `skip` aggregate flag for legacy consumers; range-aware
consumers deliver only eligible text spans as audio. The current valid header prefix exposes
both choices, so streaming text does not disappear when audio is skipped. Distinct native
generation identities preserve all validated
model-authored content, including requested exact repetition. Content equality is not transport
deduplication or permission to suppress speech. Later generations stream through the existing
validated-header prefix parser without waiting for full-envelope equality checks. A content-first
envelope remains buffered while its controls are unknown, then releases as soon as the complete
strictly validated envelope arrives; it does not wait for a later generation or final native turn.
The same duplicate-key, exact-field, type, graph and audio controls apply to every JSON key order. Nonempty
text-only additions and graph-transfer controls
retain the model's declared disposition. Native response completion or queued-input
acknowledgement is not proof of public text, preview or audio delivery. The adapter does not
retract earlier output, semantically filter responses or infer input consumption. Strict
replacement before public commit remains dependent on actual input-to-generation/publication
lineage; this contract does not supply that missing evidence. Source and schema checks prove
composition only; exact-model continuation and affected channel acceptance remain separate gates.

Authorized broker MCP configuration is projected structurally into ACP `mcpServers`. Existing
Library `claude_project_mcp` remote configuration is a compatible bootstrap representation for
Grok, subject to the same credential-free manifest validation. No extra tool authority is discovered
or granted by the adapter. Typed `subagent_spawned`, `subagent_progress`, and `subagent_finished`
updates project native children; provider prose cannot create lifecycle records.

The workstation image fetches immutable official Grok 1.0.34 Linux artifacts for amd64 and arm64,
checks architecture-specific SHA-256, and records both pins in image provenance. ACP compatibility,
authentication, tools and lifecycle still require validation on the actual built image. Updating the
pin is a reviewed artifact change; source version and installed binary version are separate facts.

## Workspace file placement

The API separates execution placement from shared-workspace file placement. In a supported
shared workspace, `file_placement: common` gives members a shared working area;
`file_placement: member_private` gives each member its own working area and read-only access to
the common mount. Neither setting shares native homes, credentials or sessions. Membership,
account grants and measured host capacity remain admission requirements. A project groups work;
it does not itself grant a shared execution workspace or peer access.

## Coordinator ownership

`coordinator.py` adds conversation input and accepted-goal records in the existing Store database.
It does not execute harnesses or run a scheduler. Foreground turns use `ConversationProvider`;
helpers use `reserve_delegation` and `Store.run_queue`; exact controls use existing service methods.
A goal may link to the current conversation run when the model chooses direct execution.

Intent and delegation idempotency are committed before start. Lost attachment is recovered through
the same reservation key and digest. Run status and output are read from existing runs. Result
notifications have a unique run identity and are claimed transactionally into a continuation only
when no accepted foreground turn is pending. The existing maintenance tick performs bounded
reconciliation. Provider session/start fencing still owns final native admission. The run-admission
transaction calls `guard_coordinator_run` so Stop can fence a reservation not yet attached to its
accepted goal. Per-goal controls must not silently stop other goals sharing one conversation run.

Coordinator native tools reuse the peer collaboration run/attempt token binder. Every call also
requires a persisted coordinator conversation mapped to that exact provider-session worker and
owner. Role, configured routes and goal budget cannot be expanded by workers. Private token
projection uses existing MCP materialization and never places a service token in a worker.

In packages, workspace containers share a workers network that refuses traffic between its
containers, so one owner's workspace cannot reach another's. The runtime serves each workspace
container a Unix socket in that container's own directory. A small standard-library stdio bridge
carries each harness's context, peer and coordinator MCP calls over it. The runtime still
authorizes every call by the run's own token.
