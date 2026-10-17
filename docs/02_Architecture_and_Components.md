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
Successful Chat Completions and Responses HTTP replies expose `X-GlassHive-Response-Deadline-At`
alongside the request ID. It is the valid timezone-aware absolute deadline stored by admission,
shared by the primary attempt and any serial fallback. Signed private-machine-state requests with
no explicit or configured budget persist the existing effective 660-second default at admission.
Public requests keep their existing implicit deadline/admission behavior; explicit request budgets
and configured upper bounds keep their existing precedence. Reconnection does not renew this deadline.
Clients may use this owner-authored header instead of duplicating the provider timeout. Old records
without a valid stored deadline omit the header; invalid/non-finite request budgets are rejected.

Conversation admission keeps complete protected inputs within the selected native model's effective
half-window, calibrated by observed bytes per token, and the existing 512 KiB hard ceiling. An
explicit `GLASSHIVE_CONVERSATION_REPLAY_MAX_BYTES` cap remains authoritative. A smaller reported
window applies only to the matching model and native epoch; admission and pressure use the same
owner. Native Codex GPT-6.1 Sol reports an effective 475,000-token window; Direct API metadata is
separate. Discovery advertises the same model/config budget at minimum bytes-per-token
calibration, without a separate 192 KiB ceiling. The host uses the smallest advertised limit of
all configured primary and fallback routes. This catalog limit does not override a smaller
valid native session observation, an explicit operator cap, or the hard ceiling. Final native
admission remains authoritative; inputs beyond capacity fail with 413 instead of truncation.


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
After a failed turn releases its compute, a new accepted request can reactivate that same
conversation worker through the shared guarded start path. This does not retry or alter the old
needs-input run. Fresh requests and duplicate requests retain the same exact-run, deadline,
owner/session, restore-hold, Pause/Stop and confirmed-compute-release checks.
Readiness holds the existing worker lifecycle fence, not the provider-wide admission lock;
the exact request, deadline and ready worker are checked again before dispatch.
The queue can admit a separately accepted stateless request on that bound conversation after
an older stateless provider request failed and has no active compute lease. The failed run stays
unchanged. Mission, persistent-session, approval and unresolved-release work keep their ordering fences.
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

The validated contract also survives Native worker bootstrap projection. Direct SDK execution uses
its existing native structured-output protocol when the selected SDK profile supports JSON Schema,
with a generic JSON-object schema for this internal contract. Profiles without native schema support
keep their existing supported typed-output path;
the owning consumer still validates the application shape and fidelity. Terminal extraction keeps
the complete private bytes and never applies the public `FINAL REPORT` selector. Trailing prose,
duplicate keys, invalid objects and failed review remain failures; no JSON prefix is salvaged.

### Native generation and restart ownership

A native control operation targets the recorded run, lease, process identity, attempt and host-slot
identity. A stale Stop, timeout, rejected-start cleanup or old process `finally` must not signal,
clear or release a replacement generation, including a replacement for the same run. Missing PID
metadata alone is not proof that native work stopped. Historical results and deadline cleanup use
the worker recorded on the run even if the conversation session now points to another worker.

Host Stop, Pause and Steer signal the exact process group and its known escaped descendants
together. They share one five-second graceful wait and, if needed, one two-second forced-stop
wait. A second target must not start a fresh grace period for a signal already sent. These are
process-exit grace periods, not an API response-time guarantee: identity checks and recorded
native-child cleanup still run, and unknown death retains the existing fence. Steer still waits
for confirmed interruption before it reports the replacement as queued.
The existing opt-in native timing log records `host_control_signal`, `host_control_wait` (TERM
and KILL separately) and `host_control_child_cleanup`, with a hashed run identity and death-proof
booleans. These stages separate time spent proving a stop from native answer generation; they do
not claim the request has asynchronous admission.

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
When a host lane is released, the existing wake-up selects its oldest queued `host_capacity` or
`host_worker_busy` waiter without waiting for that retry timer. It changes eligibility only: the
ordinary admission path still rechecks lane, account, tenant, resource and lifecycle limits before
reserving a slot. Other lanes and provider rate-limit backoff remain unchanged.

The authenticated OpenAI-compatible `/v1/models` route publishes model IDs, effort choices,
capabilities and native replay-budget declarations without starting a harness or probing sign-in.
Its default readiness status is `not_checked`; metadata is not evidence of available credentials or a
successful model request. Explicit model readiness consumers opt in with
`GET /v1/models?check_readiness=true`, which checks each harness once under the same authentication
and owner scope. Connections retain their explicit Verify/Connect operations, and actual
admission and the selected native session retain their account, authentication and capacity checks.
This keeps a slow unrelated harness probe out of every catalog consumer's critical path.

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

### Native startup, retained sessions and idle process preparation

These are different forms of reuse:

| Reused state | What it saves | What it does not prove |
| --- | --- | --- |
| Worker workspace or running container | Recreating files and the execution environment | An initialized native agent process |
| Saved native conversation session | Losing or reconstructing the conversation history | A running process or cached provider context |
| Initialized idle native process | Starting and initializing that process on the next turn | Authenticated session, current tools, model inference or speech readiness |
| Provider prompt cache | Some repeated model input processing, when the provider confirms a cache hit | Process readiness or free history transfer |

Current host Codex, Grok and Claude conversation adapters start a native process for each turn,
then prove that turn's native process stopped. They retain the exact session ID and history for
resume. `ensure_worker_ready` prepares the workspace and configuration; it does not keep a model
process idle. A timing row called “native startup” therefore needs separate measurements for process
creation, protocol initialization, authentication, session creation/resume, tool setup and prompt
submission. It is not all OS startup or model inference.

**Optional process preparation supports eligible host Codex, Grok and Direct API profiles. It defaults to off.**
The bounded `native_warm_pool.py` uses Grok's own authenticated localhost WebSocket server;
Codex and Direct use authenticated local holders over the existing WebSocket dependency.
Codex retains native stdio inside its holder. Preparation opens no thread, MCP server or user prompt.
The saved configuration owner is `WorkerConfig.performance.native_warm_servers`;
`native_warm_idle_seconds` controls expiry. `0` retains the current
stdio path; a positive count prepares actual idle processes, not folders or profile checks.
If optional attachment fails before any session or prompt, the runner uses the same configured
model's normal stdio path. It never replays an accepted prompt or tool to recover an attachment.
The existing worker **Advanced configuration → Startup and limits → Keep this worker ready** panel
saves the count and expiry. Queen/provider-session workers and ordinary workers use this same
field. Startup first sweeps orphan native ownership, even if warming is disabled, the worker
is paused, or its current account is unavailable. Saved positive values then restore; Reload settings shows actual ready,
starting and leased counts. No agent or model work runs just to fill the idle count.

The count accepts 0–8 idle servers per worker; expiry accepts 10–3600 seconds, default 120.
The host runtime bounds all preparing, idle, leased and unconfirmed processes to 16 total and
8 per owner. Normal worker concurrency limits still apply. An expired idle target stays stopped
until a subsequent save or admitted turn refreshes it. Normally one idle server per persistent
worker is sufficient. More idle servers consume memory; they do not create more workers.

| Runtime/account path | Idle native server support |
| --- | --- |
| Host Grok, static current native account or worker key | Supported |
| Grok named account with per-attempt projection | Unavailable; normal native startup remains available |
| Direct OpenAI, Anthropic or xAI, host | Import-only SDK process; account is unbound until the admitted run |
| Host Codex, current native file account and supported app-server schema | Initialized native stdio; fresh thread authority binds after lease |
| Codex named account, startup-only override or unsupported environment binding | Unavailable; exact normal startup remains available |
| Claude or container | No adapter; normal native startup remains available |


Codex prepares the selected executable with its existing authenticated native account and
`initialize` only. It performs no model request or MCP connection while idle; native account
refresh and telemetry can still occur. The existing binary/schema compatibility check and
static account/configuration identity guard reuse. At admission, the existing relay retains
thread start/resume, full history and authority, model, effort, policy, images, schema and
control mailbox. Fresh MCP grants enter thread configuration in memory only. A bounded,
separate per-worker config lock removes only their environment lookups from the generated
file during native thread creation, then restores the exact original bytes before the turn.
All generated Codex config writers share this lock; Stop does not. Changed file bytes are
preserved rather than overwritten. No global MCP discovery or reload is added, and optional
MCP startup remains native asynchronous behavior.

Unprojectable user/project bindings, hooks requiring fresh process environment and unsupported
CLI settings retain the exact cold command and capabilities. No accepted or uncertain native
thread dispatch is replayed. Claude's official SDK has a fresh-session prewarm/claim protocol,
but its prewarm API rejects resume/continue/forkSession; this runtime has no adapter that preserves
its existing resumed authority and history contract. That is an adapter parity limit, not a claim
that the provider has no prewarming protocol.

Direct uses this same pool, count, expiry, exact worker/profile/model/source identity and
single-use lease. Its idle interpreter imports the official SDK only, with a small OS/Python
environment allowlist: no provider key, account, broker grant, history, tool connection, HTTP client
or model request. Fresh authorized environment and request enter only after the exact admitted
run/attempt claims the private authenticated loopback channel. Stdout/stderr stream back through
the existing supervised generation. Normal completion, Stop and crash recovery retain the same
native process/descendant absence proof; an uncertain accepted request is never replayed.
Exact host-run cancellation drains its own cancellation callback effect after terminal proof;
other lifecycle effects remain with the existing durable recovery owner. It does not synchronously
sweep unrelated lifecycle kinds before returning capacity release.

The Profiled runtime shares one pool across Codex, Grok and Direct, so the 16-global/8-owner bounds are
aggregate. A ready process helps its existing worker, such as a persistent Queen. A brand-new
worker remains cold unless it was configured and became ready beforehand. Missing ready capacity
uses the exact normal cold command without waiting. Import readiness does not warm an API client,
provider context or model cache, and it does not remove fresh tool/account/history work.

Interpreter, runtime/runner source and installed-wheel metadata changes invalidate the warm scope.
This is process readiness, not a provider context or KV-cache promise.

Default count is zero. An isolated import-only SDK observation used about 113–133 MiB of resident
memory per idle interpreter; eight can therefore require about 1 GiB before active work. This is
an observed planning estimate, not a portable bound: installed SDKs, provider modules and platform
change the footprint. Size the configured count to available memory; no model weights are loaded.

A separate local isolated, empty-home, no-inference experiment measured:

| Work measured | Samples | Duration |
| --- | --- | --- |
| Codex process creation through initialize acknowledgement | 3 | 56.5–85.6 ms |
| Already initialized Codex local `thread/list` request | 12 | 0.39–0.89 ms |
| Grok stdio process creation through initialize acknowledgement | 3 | 352.0–446.5 ms |
| Connect to an already prepared native Grok server | 3 | 0.65–0.73 ms |
| Grok native initialize acknowledgement after that connection | 3 | 104.5–110.5 ms |

Two real idle Grok processes became ready in 819.5 ms total with durable launch records and
listener ownership checks. Each used about 62 MiB of resident memory in an empty private home.
Both stopped after the experiment; the claimed process took 154.6 ms to prove stopped.
These are protocol feasibility measurements, not delivered call latency. No account, user session,
model inference, tool or speech path was exercised. The remaining native initialization time is
measured, but its internal cause is not isolated. Do not attribute it to local network transport.

The native server participates in the exact attempt's Stop, timeout, crash recovery and absence proof. Grok explicitly keeps in-flight sessions
alive after a WebSocket disconnect. Closing the connection is not a confirmed Stop. Each prepared process has a single-use
lease followed by confirmed native process eviction; current
per-attempt MCP grants, model/account binding and session authority must remain unchanged. Idle
processes must never contain a previous turn's broker token. Native current-account identity,
model, worker, lane and non-secret environment settings scope each claim. Complete static
environment values are compared only in memory; credentials are never hashed into pool keys.
A restart preserves an unproven live attempt and defers new idle capacity until existing native
generations are reconciled. A stopped attempt retains the same secret-free lease descriptor,
including escaped process identities, until its existing attempt release proof can consume it.
Exact native signals run inside the existing attempt authority guard; full process/descendant
waits and escalation run outside the runtime-wide process lock. Confirmed Stop releases counted
leases even if a run finalizer has not returned. A shutdown-finished lease is safe to finalize
again; an unconfirmed warm stop cannot skip the ordinary generation finalizer or erase its interrupt.
Pausing an idle host worker uses the existing exact native compute cleanup under the normal
worker lifecycle claim; it does not require inventing a run to pause. Unknown, live or foreign
native generations and unconfirmed idle-server cleanup cannot be reported as a confirmed Pause.
Status must distinguish process-ready,
session-ready and provider cache evidence. A separate synthetic native-account continuity check saved a test marker, received native
`session.close` acknowledgement, evicted the process, then loaded that exact session in a new
process and recovered the marker. Both processes stopped. A real saved-worker check exercised default-zero startup, two actual idle servers, the same
native session across cold and warm turns, unchanged model/High effort, and secret-free final
stop receipts. An actual native terminal sleep was then stopped, its tool process was proved
gone, and the next turn recalled the earlier test marker in the same native session. These are
runtime/native-provider checks; browser and delivered voice acceptance remain separate gates.

The native-runtime sample measured 545 ms for cold initialization and 0.65–0.76 ms for warm
initialization. Warm listener identity verification cost 54–55 ms; preparing and claiming a ready
lease cost 0.77–0.92 ms. Runner start through prompt submission fell from 649 ms cold to
125–130 ms warm. Different turn text and provider response times mean total-turn differences
are not a controlled speech-latency comparison. Do not mix these samples with the empty-home
initialize-only experiment above.

Tool readiness has a separate cost. One retained turn spent 550 ms inside the native MCP
connection handshake; about 430 ms remained after other session setup overlapped it. The
Python client waited for the native completion event, then queried the tool registry in 2.2 ms.
This was not a fixed 430 ms sleep. The separate native-versus-broker contribution was not
isolated. A conditional new-session model correction can also require session reload to
restore pinned instructions; a later current-model turn did not need that correction. Do not
remove authorization, tool readiness or instruction restoration to improve a timing row.

Primary sources: [Codex app-server lifecycle](https://learn.chatgpt.com/docs/app-server),
[Grok native agent server](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md),
[Grok reconnect behavior](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-shell/src/agent/server.rs),
and [Claude continuous SDK connection](https://code.claude.com/docs/en/agent-sdk/python).


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

Registered synchronous MCP tools run through the existing AnyIO thread pool so blocking HTTP does
not serialize independent tool requests on the MCP event loop. Registration preserves the original
schema and injected request context; internal Python calls stay synchronous and async tools are
unchanged. Native capacity, worker-limit locks and transactional admission remain authoritative.
Cancelling a wait does not roll back a dispatched HTTP action: existing source fences, idempotency
and durable receipts retain its ownership. The adapter does not automatically retry accepted work.

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

The persisted conversation-worker lane also disables the native bundled
`long-running-background-tasks` skill in that worker's isolated `GROK_HOME/config.toml`.
That vendor skill requires foreground supervision until background work finishes, which conflicts
with xPerfect's durable worker receipt and automatic return path. This policy uses Grok's documented
`[skills].disabled` setting before cold or warm startup; it does not disable skill discovery,
foreground shell tools or MCP tools. The edit preserves other configuration bytes and disabled
choices, uses Grok's own config-write lock, and never changes mission-worker or shared-account homes.
Native config file identity participates in warm-process reuse. Exact native session history remains
intact on resume; old recorded skill text is not erased, so resumed model behavior still requires its
own acceptance evidence. This removes a conflicting default; it does not force delegation or promise
a latency improvement.

Native updates, exact session identity and terminal results use separate typed envelopes. Unknown
native child events remain native data; they do not invent xPerfect workers. Usage is unavailable
unless independently projected from an authoritative native event. A terminal error, cancellation,
protocol mismatch or exceeded output limit cannot become a successful empty result.
The conversation output schema describes only the final host-graph result. It reports actual
results, a real accepted asynchronous work receipt, or a blocker; a promise cannot replace them.
The owning Main or worker instructions choose the execution path. The output protocol does not
require completing work directly before acknowledging an accepted handoff. This contract clarifies
responsibility; it does not promise lower latency or model compliance.
It does not replace or restrict authorized native function and MCP execution before that result. An assistant result
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

Worker termination records each newly cancelled run's existing cancellation event and durable
terminal callback sink in the same transaction. The sink waits for confirmed compute termination;
failed or pending native stops cannot publish a completed cancellation. Exact compute-claim and
legacy termination paths use the same existing cancellation recorder. Terminal result revision,
digest and once-only recovery remain authoritative. Worker-only termination effects have no
run-scoped callback recipient and finish after normal link revocation; they do not retry forever
or manufacture a run identity. This exact-worker recovery also accepts the canonical
`callbacks.origin_ref` contract only for the confirmed terminated worker’s exact last cancelled
run; historical completed, failed and older cancelled results are excluded. The global legacy
scan keeps its explicit callback URL contract
so it cannot revive historical origin-only work. An idempotent owner Stop on an already
terminated worker uses this same bounded repair when a legacy worker-only effect was already
retired; it does not change compute release or repeat the terminal callback. Private endpoint and signer resolution remain in the existing runtime configuration,
not in durable worker bundles. A whole-worker close carries its active host lease into native
Stop. A retry after an earlier cancellation keeps the exact owned terminal run and attempt;
an active retained process is stopped only under the current worker-close owner and the existing
session, PID start-identity and descendant guards. Unknown or changed generations still fail closed.

Background Grok native approval is an independent owner setting, `orchestration.worker_access`.
The existing background model controls show it only when a selected background or fallback model
has the declared `grok-build` harness profile. Missing or `ask` keeps native action approval;
explicit `full` reuses native Grok full access. The existing configured full-access capability
policy must permit it in both the editor and API validation. Foreground full access is never inherited. The
owner-saved value travels through the existing route resources and Core-injected launch authority,
not model-authored task arguments. Canonical admission retains only the reviewed typed value;
the native adapter requires the exact admitted mission generation. Restricted/zero-tool mode
always wins. Connected-tool grants and per-action Core authorization remain unchanged. This does
not promise a Grok read-only or workspace sandbox, and does not change Codex, Claude or OpenCode
permission defaults. Caller-authored launch authority is discarded before trusted Core configuration
can add its own declaration, including ordinary mission-worker launches. When both configured routes
leave Grok, the editor clears unused full access; API validation rejects a full setting without a
declared Grok primary or fallback route. Retained unused or disallowed full settings stay visible
until cleared, so no hidden permission can return after a later route change. Changing the setting
affects new work; it does not approve an existing offer.

The host owns internal lifecycle bookkeeping. Its work log and native journal preserve run
progress and continuity without requiring the model to edit an internal log or obtain owner
approval for that bookkeeping. Native task permissions follow the selected worker mode.

Workers use the current admitted task, supplied context and typed continuation authority directly.
They do not reread a duplicate constraint ledger as a startup or completion ritual. If required
context is missing, the fallback is `glasshive-run/runs/<run_id>/constraint-ledger.json`, with
matching `run_id`. The current native envelope carries `context_manifest.run_id`, so warm processes
do not rely on per-turn environment variables. Standalone execution without an envelope can use
`GLASSHIVE_RUN_ID`; absent current identity stops recovery. A workspace-wide latest ledger can
belong to another concurrent run.
Later authorized guidance remains applicable. The existing per-run ledger writer, retained source,
continuation validation, evidence checks and native permissions remain unchanged. Registered host
prompts and standalone/generated worker instructions share this contract. This removes redundant
instructions and stale authority risk; timing improvement requires live measurement.

Native host missions with declared `artifact_sha256` file transport, enabled native tools and
supported private native placement compose the existing registered `worker.conversation_file_delivery`
module into their native task before command and stdin construction. The original admitted goal and
constraint ledger remain unchanged. Initial work, queued retries and new continuation runs use the
same run owner. The existing Markdown selection, snapshot and callback file carrier remain the
publisher: prose filenames and an unselected workspace file are not delivery. The module preserves
plain answers without files and accepted unchanged published-file references; it does not force an
artifact, scan a workspace or add a second publisher. Conversation, restricted and deferred provider
paths keep their existing composition.

Native selected-file selection, the public-stream link barrier and both artifact renderers share
the same Markdown scan grammar. Escaped
characters are literal tokens outside links; escaped and ordinary label characters have disjoint
branches. Nested JSON strings and unmatched brackets must not cause regex backtracking to block
reply delivery or synchronous provider recovery at API startup. Recognized links retain the same
snapshot, source-root, owner and immutable-download checks; text is not clipped to avoid the scan.

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

Native clarification questions retain their meaningful offered answer labels within the
existing canonical limit. Permission presentation reuses the version-1 native terminal
tool's model-authored action description when available, with the original title as fallback.
Permission option labels stay in the exact structured form, rather than being appended to
conversational text. The shared permission presentation adds a short decision question for
foreground speech, within the existing limit and without truncating operation facts.
The Queen explains the concrete action and asks the needed decision;
it must not offer broader permission scope through a conversational single-operation response.
Exact commands and inputs remain in the native control details; the existing Active Work form
also retains the original title as field description. Foreground permission envelopes carry the
original title separately from the spoken prompt for typed detail consumers.
Response values, question keys, fingerprints and consent controls do not change. If a clarification
question and its labels exceed the existing canonical limit, the original valid question and
complete form remain; oversized forms do not promise that every label is spoken.

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

The final-result protocol uses the admitted worker's existing `surface` field. Live Voice and
Wing omit the optional-messaging heuristic for choosing silence: their ordinary public answer is
audio eligible unless the user requests text-only or silent delivery. Specialist return evidence
and legitimate silent continuations retain their existing rules. Other surfaces retain optional
audio judgment. Native and Direct adapters use this same schema owner; this is prompt scoping,
not a runtime override of the model's delivery decision. A schema test proves projection only;
actual configured-model and delivered-audio checks prove behavior.

For a Codex conversation with admitted delivery controls, a valid silent terminal envelope
retains earlier validated public assistant content from that same native turn, including across
context compaction. Only the current turn can contribute; failed or unfinished turns, unframed
working text and invalid envelopes do not become answers. A genuine empty-and-skip result remains
valid silence when no public answer preceded it. Ordinary worker report selection is unchanged.

After native function or MCP tool results and on same-turn input continuation, the model uses
the complete earlier goal and work with new evidence. Each wire envelope is complete; its public
content adds useful new information, rather than reannouncing an unchanged request, plan,
acceptance or approval condition. Necessary questions, useful progress, corrections, completed
results and artifacts remain deliverable. Explicit requests for repetition or a full replacement
take precedence over the incremental-only guidance.
Internal setup and tool discovery stay silent unless the user asks about them or a real blocker
requires their decision; useful task progress and outcomes remain public.
It may use the existing empty-content/voice-skip envelope when no extra public content is useful;
that envelope completes the current response without executing a planned native call or
guaranteeing another response. Native function and MCP calls need no accompanying envelope;
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
or granted by the adapter. Host Codex, Claude and Grok missions use the existing private native
context selectors for generated instructions and MCP configuration while preserving their actual
workspace, user project instructions, uploads and host tool helpers. The server stores the
trusted conversation or mission lane before first workspace preparation. Conversation creation
must not transiently use mission defaults, including native global instructions loaded in addition
to a conversation override. Caller bootstrap `run_mode` cannot change that trusted lane. Existing
user-owned global instructions remain untouched; removal of a known generated legacy file needs
byte-exact ownership evidence and a hash-fenced backup transition.
Claude receives an explicit
private `--mcp-config` with `--strict-mcp-config`; Codex retains its private `CODEX_HOME`;
Grok retains the authorized ACP declaration. Generated Grok compatibility `.mcp.json` is empty
and private: a populated shared Claude declaration can suppress the matching ACP server even
with Claude compatibility discovery disabled. Mixed harness workers must not rewrite one
another's project MCP configuration. A known generated legacy file may be backed up and removed
only with evidence of ownership and an exact hash fence; user-owned configuration is preserved.
A signed mission's Core capability-broker transport reuses the existing run-and-attempt
permission mailbox. Only the exact admitted broker, matching grant expiry and projected token,
and its enabled native-registry tools can receive an `allow_once` transport answer. Core still
authorizes each capability's scope, effects and confirmations; this does not grant native file,
shell, unrelated MCP, or persistent permissions. Cancelled, stopped, expired and restricted
attempts cannot use that transport allowance. Admitted grant metadata is restored only on the
invocation-local bundle after public canonicalization, alongside the existing ephemeral token;
canonical storage still removes run grants and private credentials. A missing explicit mission access mode does not
inherit conversation `full` access or bypass ordinary native approvals.
Explicit `native_tools: false` also produces empty private MCP configuration. OpenCode is
unchanged and remains deferred. A native process or accepted worker receipt alone does not prove
tool readiness. Typed `subagent_spawned`, `subagent_progress`, and `subagent_finished`
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


## Optional Direct model execution

Status: implemented in source as explicit Direct OpenAI, Anthropic and xAI profiles for the
existing Queen and worker configuration. Local loaded API, voice, Stop and delegation acceptance
are separate evidence gates; source tests and SDK feasibility do not prove complete parity.
Result scope is a shared worker quality requirement for every executor. A total must cover the
requested connected scope, or clearly identify the checked subset. Unique totals must compare
stable item identities rather than add overlapping lists. A completed run and a valid tool result
do not prove complete coverage. Small-context exact-model controls passed with both the existing
and a proposed scope reminder; this did not reproduce a larger-context partial-count failure and
does not justify another runtime prompt. Keep that observed quality limit explicit when comparing
effort settings. The host QA case `GHHOST-026` covers aggregate, single-source and overlapping-source results.

The thin single-agent provider/tool executor reuses the host runtime lifecycle; xPerfect retains all work
admission, scheduling, history, account authority, controls and callbacks. Do not use SDK teams,
handoffs, durable conversation stores or a second manager. Viventium remains a client.

OpenAI and xAI require their Responses path for the model/tool contract; Anthropic uses
Messages. Real supervised xAI SDK checks exercised streaming, an MCP file read and protocol-history
continuation. These small-context checks do not prove full host modules or voice acceptance.
Exact attempt authority, protected account resolution, typed failures and public-envelope streaming
are covered by focused source tests. Local process termination prevents further SDK tool execution;
closing the stream does not prove remote GPU termination. Preserve accepted actions and late-result
fences; do not fabricate remote cancellation acknowledgement.

Use existing MCP/context projections and canonical source references rather than AI-written
handoff recaps. Preserve role-specific modules and provider message/tool/reasoning identifiers.
API execution does not bundle native shell, file-editing or computer tools: expose only actual
authorized capabilities and preserve access to configured native workers. Verify the Queen lane
first inside the same implementation batch, then its worker consumers; a Queen-only result does
not satisfy universal Queen/worker configuration. Current defaults and supported alternatives stay
unchanged. Provider API access is separate from CLI subscription authentication. HTTP 401 is an
authentication failure; HTTP 403 is a non-retryable request or model-access refusal. A 403 alone
does not prove missing credentials or require reconnecting them. Review accepted work before retrying.

Before adding an API profile, inspect every profile-kind consumer: readiness, key support, provider
compatibility, account/grant binding, observers and native-team projection. Unknown kinds must not
fall through to CLI/process assumptions. Roster and delegation tools must expose real capabilities,
and unavailable tools must produce typed failures rather than invented artifacts.

Keep optional SDK telemetry disabled and use only intended provider/MCP endpoints. Direct selection
identifies owner API-key billing through the existing account controls and retain usage/cost
receipts. Do not imply subscription authentication supplies API access or add another consent manager.

Direct starts a fresh supervised process by default. Its optional import-only readiness uses the
same native warm pool described above; it does not prepare a provider client or MCP connection
before admission. Host execution is supported; container placement is explicitly unavailable. Current Grok and GPT profiles expose Low reasoning,
not reasoning Off. Only settings the selected SDK/provider path actually sends are accepted.

The Direct authority instruction producer belongs to the same strict prompt-integrity inventory
as native producers. Registering a producer without including its actual owner in that inventory
blocks shared orchestration readiness, including native routes. Regression checks must cover the
registered Direct producer and continue to reject unknown, missing and mismatched producers.

Conversation Direct execution retains the owning response contract across provider requests and
tool continuations. The official SDK chooses a typed output tool when the declared model profile
supports forced tool choice for the actual request, including its thinking settings. Use the pinned
SDK's own request predicates; forcing must not disable or weaken selected thinking. This requires
some tool from the unchanged function-plus-output catalog;
it does not choose a worker, route or objective. If the model profile forbids forced tool choice but
supports native schema output, use the SDK native schema mode without changing thinking effort.
If neither typed mode can preserve requested thinking, report unsupported configuration before
sending a model request. This capability decision avoids relying on an optional prose instruction for terminal JSON.
Both paths retain the same owning envelope and strict terminal schema/conversation validation
before journal commit or success. SDK structured-dictionary typing alone is not schema validation.
Typed output-tool argument deltas and valid structured text prefixes use the existing public parser;
plain model narration stays private. Buffer only the undecided envelope header, then stream content.
Ordinary worker output stays plain when its contract is plain. Do not make missing voice disposition
eligible or add a model repair/replay to compensate. If the reserved final tool immediately follows
and confirms the same decoded type, recipient, voice choice and content as a complete validated
public text envelope, it confirms that output rather than publishing another copy. JSON whitespace,
key order and Unicode escapes do not create another reply. The existing public-prefix parser exposes
control identity only when requested; its normal return contract is unchanged. Hold only the possible
matching decoded prefix, then stream immediately when the owner recognizes different controls or
content. Actual content whitespace, different envelopes, voice choices and ordinary repeated messages remain distinct;
an intervening native tool breaks this confirmation adjacency. Incomplete confirmation cannot
succeed. The content-free `sdk_first_raw_text` and `sdk_first_raw_output_arguments` events separately mark
the first SDK text and reserved terminal-argument chunks before public-prefix or confirmation
filtering. Thinking and accepted public speech are separate stages; do not label a filtered public
prefix as the first raw provider text.
Provider-specific wire checks do not prove API access or model behavior; real provider acceptance
remains a separate gate.
Provider SDKs own their supported HTTP client implementation and schema conversion; equivalent
adapter normalization must retain the owning constraints.

For admitted Core-owned snapshots, the official SDK request hook retains the first trusted archive
copy and omits later exact duplicates, including from the current owned input, when origin and
content hashes match. Preserve the earlier request prefix for provider caching.
Preserve all unique text, files, images, native tool-call/result pairs, reasoning identifiers and the
raw stored journal. Delta inputs, ordinary missions and unmarked legacy records keep their history.
This optimization reduces provider input only; existing history boundaries still own journal growth.
Before reuse, apply the official SDK history repair and preserve an existing trusted input origin only
for the exact same user-prompt part object. The SDK merges adjacent tool-return and user-input
requests and discards application metadata; reindex the known origin after that merge. Keep every
tool return, unique goal and attachment. Never infer authority for unmarked legacy parts from their
text. A marker that is absent or fails its content hash cannot authorize archive removal.


The host capsule compiler can also supply one typed UTF-8 range for its exact current active-work
DATA envelope. Native admission binds that range to the accepted Core owner, protocol, source
snapshot and context epoch. Direct retains the current envelope in full and omits only older
consecutive identical trusted envelopes in the outgoing request. Changed or unknown inputs break
coverage: A → B → A retains all three. This does not parse headings, quoted tags or user intent.
The original full-input hash is checked before both archive and current-state projections; raw
journals, unique user goals, files and all tool/reasoning protocol parts remain unchanged. Existing
unmarked history is not repaired or deleted. The existing `direct.stage` event reports only removed
block counts and UTF-8 bytes. This request projection does not implement native CLI history editing.

Stateful Direct OpenAI and xAI routes use the official SDK `openai_prompt_cache_key` option with a
versioned, domain-separated SHA-256 of the existing provider/model/tenant/owner/worker/account/epoch
history scope. The key contains no raw identity. Anthropic, stateless and private machine-state
requests have no such association. Server-side response storage stays disabled, retention settings
stay unset, and authorization still runs for each invocation. A routing hint does not guarantee a
cache hit or latency reduction. Existing pressure/recovery epoch boundaries remain authoritative;
approving a Main summary does not itself erase old Direct history or unmarked archive copies.

Direct pressure rotation also preserves the official SDK's tool protocol. After existing accepted
source coverage proves that prior visible turns are present, the runtime reads the exact existing
journal under provider/model/tenant/owner/worker/account/epoch scope. Every ordinary tool call must
have one matching result (including a typed retry result). Unknown native tool protocols, incomplete
pairs, missing logs and owner/epoch mismatches defer rotation. Pressure rotation refuses unfinished predecessor work. It freezes the already-proved journal bytes
once with the existing private atomic-write primitive in a content-addressed immutable seed. A new
epoch carries only a SHA-256 and the old/target epoch identity; the SDK runner derives the private
seed path from the authorized worker state, checks its hash and full source scope again, and
retains whole messages containing those complete tool pairs. This keeps
all accompanying protocol parts without re-executing actions. Original journals remain unchanged.
The seed survives same-epoch retries before the first checkpoint; branch changes cannot inherit it.
Later writes to the old live journal cannot wedge retries. A changed immutable seed fails explicitly
instead of starting with empty tool context; snapshot I/O failure defers before any epoch mutation.
Admission reuses its first in-memory scope/pair proof rather than parsing the journal three times.

The existing pressure estimate includes retained seed bytes until measured provider usage becomes
available. If preserving tool evidence leaves the proposed fresh context under pressure, keep the
current epoch and report `retained_tool_evidence_pressure`; do not rotate fruitlessly or clip tool
results. The `context_tool_replay` stage records message, tool-call and byte counts only. This can
bound repeated plain conversational input when the complete source is available; it does not promise
bounded raw tool-result growth or make a Main-summary approval an epoch-rotation trigger. Native
Grok remains conservative until its own complete new-session replay protocol is proved.

Direct's terminal receipt keeps aggregate SDK run usage for billing and a separate
`context_usage` from `result.response.usage`, the last actual model response. The SDK's run total
sums request inputs across tool rounds and must not become resident occupancy or byte/token
calibration. The last response measures the materialized request after the existing history
projection, including retained tool results; it is not merely the newest user delta. Missing,
zero or inconsistent context usage retains the aggregate input as
`native_prompt_tokens_upper_bound`; if no positive aggregate exists, the selected window is the
bound. Such bounds can trigger conservative pressure but never calibrate bytes per token. The
SDK may finish after an output tool and ordinary action are co-emitted. Ordinary tool returns
after its last model response have no observed request usage yet, so they also require the full
selected-window bound instead of the aggregate. Internal output-tool acknowledgements keep their
existing reserve behavior. The
existing store permits measured/bound scope changes only within its epoch, time-order and request
fences. Billing output and original protocol journals remain unchanged.

The signed capability broker already declares globally unique canonical tool names. Direct keeps
those names so the executable catalog agrees with admitted authority. Other MCP servers retain
their existing namespace and tool filters. A name correction alone is not proof that a model will
choose a required action; actual accepted worker receipts remain the acceptance gate.

Broker admission preserves an optional signed eager/deferred server partition in the runtime
projection. Both arrays must form an exact, disjoint partition of allowed servers; one missing or
an unauthorized member fails admission. Absence of both fields preserves older eager grants.
Hosts can use the existing broker discovery helpers to defer connected-service initialization
until a worker selects it, without reducing permissions or introducing another tool manager.

Direct uses the existing NativeConversationParts collector for the complete authored stream,
terminal text and UTF-16 speech-disposition ranges. Co-emitted conversation text and tool calls
must retain that public text across tool continuations; do not replace it with only the final
model response. SDK exhaustive execution preserves all co-emitted ordinary functions even when
the output tool appears first; its first valid output wins. A completed public envelope remains the
terminal envelope if co-emitted tools finish without another public generation. An invalid or
incomplete later generation must not fall back to earlier valid text. The existing owner validates
the merged result. Completed native tool rounds are atomically checkpointed before the next model
request so Stop preserves actual call/result identifiers. The runtime does not replay those actions;
the model sees their exact results, but this does not guarantee it will never request another action.

Configured effort is a bounded execution setting, not arbitrary worker environment authority.
The common mission storage projection and clean-room validator use one known-value registry for
Codex, Claude and Grok effort keys. Direct reads that retained typed setting through the same
quality-control accessor as native harnesses, even when enterprise process environment projection
excludes it. Test the real MCP launch, persisted worker and executor input together; a helper-only
projection check can miss settings removed at storage. Credential and arbitrary environment
allowlists remain unchanged.

Direct account prepare, apply and mission binding share the registered executor's API-key
compatibility rule. Native subscription bindings remain available. The durable host supervisor
owns the response/run deadline; the SDK does not impose a separate short HTTP read deadline.
The provider SDK's bounded connection, write and pool timeouts remain in force.

Native timing also observes typed Codex context-compaction start and completion frames through the
existing parsed liveness observer and latency log. It records wall/monotonic observation times,
hashed run/item references and phase; it records no prompt, summary, path or tool result. These
observations do not count as meaningful progress or extend a run lease. Their elapsed interval
separates native maintenance from the enclosing turn; old untimestamped transcripts cannot prove
that split retrospectively.

The Codex relay also retains `thread/tokenUsage/updated.tokenUsage.modelContextWindow` in its
existing terminal usage receipt. A positive integer observation is stored with the existing
request, time-order, model and native-epoch fences. Pressure uses the smaller of that observed
window and the model catalog window; missing, malformed, foreign-model or old-epoch values
cannot replace the catalog. Billing tokens, byte admission limits, configured model and effort
remain unchanged. A catalog maximum can exceed a particular native session's effective window;
that session must not be labelled healthy solely because the catalog is larger.

This improves pressure detection after a native usage receipt. It does not force a reset or avoid
native compaction by itself. The existing complete-source and native-tool-evidence proof still
owns epoch rotation. Where that proof is unavailable, the native session remains intact, including
its native compaction cost. Process warming does not warm or compact conversation context.
The full replay size check runs only after the existing complete-source and tool-evidence proof
permits rotation. A valid delta that must retain its native history cannot be rejected by an
unused full replay; a permitted rotation still validates its complete replay before any mutation.

Wing and listen-only are call modes admitted on the existing `voice` surface, not separate worker
surfaces. Their participant-role and explicit silence instructions still apply to the final choice.
