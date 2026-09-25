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

`grok_runtime.py` supplies Docker and host adapters on the existing worker interface.
Host runs retain `HostNativeCliMixin` process supervision, generation fencing, capacity,
Pause/Stop and restart handling. Container runs retain `BaseCliWorkerRuntime`. Both execute
the same standard-library ACP bridge in the worker's private home; neither creates another
scheduler or requires Viventium/LibreChat.

The bridge uses `grok agent --no-leader --model <exact-model> stdio`, ACP v1 initialization,
explicit advertised noninteractive authentication, exact native session new/load, prompt/update,
and cancellation. It verifies the returned model and requested reasoning option before prompting.
`WPR_MODEL_GROK_BUILD` must contain the configured model; there is no model substitution.
`WPR_GROK_BIN` selects the host executable (`WPR_GROK_CONTAINER_BIN` selects the container executable) and `WPR_GROK_REASONING_EFFORT` selects an optional
native-advertised effort. Source protocol reference: [official Grok agent-mode guide](https://github.com/xai-org/grok-build/blob/4247f661689354b831191f11eeeac8424993fe3d/crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md).

Native updates, exact session identity and terminal results use separate typed envelopes. Unknown
native child events remain native data; they do not invent xPerfect workers. Usage is unavailable
unless independently projected from an authoritative native event. A terminal error, cancellation,
protocol mismatch or exceeded output limit cannot become a successful empty result.

Controls use a private, run-and-attempt-scoped mailbox behind the ordinary owner-authorized control
plane. Permission, question, elicitation and plan-exit requests require a response; absent or expired
responses cancel rather than approve. Interject reports `queued`, never consumed. The private xAI
interject extension requires a reviewed executable SHA-256 in `WPR_GROK_REVIEWED_BINARY_SHA256`;
or `WPR_GROK_CONTAINER_REVIEWED_BINARY_SHA256` for the container artifact. The bridge checks the actual executable bytes on its substrate; initialization alone does not advertise that extension. Stop continues to use the existing exact
process-generation stop path.

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
