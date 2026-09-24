# xPerfect: Bootstrap, Auth, and Identity Projection

## Answer to the Core Question

Yes, xPerfect can project the same effective access a user already has into spawned sandboxes.

But the right implementation differs by source:

1. local CLI login state
2. direct API keys
3. connected accounts managed by another product such as Viventium / LibreChat

These should not be treated as the same thing.

## The Wrong Pattern

Do not make xPerfect depend directly on another app's private storage layout or database tables.

Examples of what to avoid as the product contract:

- scraping LibreChat DB records directly inside sandbox boot
- hard-coding Anthropic/OpenAI token table formats into xPerfect
- copying an entire host home directory into every sandbox

Those approaches are fragile, unsafe, and make the runtime non-portable.

## The Right Pattern

Use a portable worker bootstrap contract:

- `bootstrap_profile`
- `bootstrap_bundle`

### bootstrap_profile
A high-level preset that selects a default projection style.

Examples:

- `clean-room`
- `host-login`
- `codex-host`
- `claude-host`

### bootstrap_bundle
A structured optional payload for additive projection.

Current phase-1 fields supported by the runtime:

- `env`
- `files`
- `claude_project_mcp`
- `claude_settings_local`
- `codex_config_append`
- `claude_md`
- `codex_md`
- `agents_md`
- `system_instructions`
- `callbacks`
- `project_definition`

Host-native workers also materialize visible prompt/context files in the host workspace:

- `project-definition.md`
- `work-log.md`
- `harness-prompt.md`
- `AGENTS.md` as the canonical Codex-style project instruction file
- `agents.md` as a compatibility mirror
- `claude.md` / `CLAUDE.md` as Claude Code compatibility files that import or mirror `AGENTS.md`
- `codex.md` / `CODEX.md` as legacy compatibility mirrors only

Bootstrap-projected instructions contain one proportional-verification rule. The worker chooses
verification depth from the user's explicit success criteria, requested rigor, risk, and concrete
defects. It uses the smallest evidence that proves the result and repeats an equivalent check only
after a relevant output change or a detected defect. This stays universal: the bootstrap does not
hardcode one host prompt, QA case, provider, file type, model effort, resource class, or token limit.

The worker prompt lineage is direct and testable:

- source: the editable worker-facing constants at the top of
  `runtime_phase1/src/workers_projects_runtime/bootstrap.py`
- assembled runtime contract: `GLASSHIVE_WORKER_PROJECT_CONTRACT`
- materialized worker instruction: `AGENTS.md` with `agents.md` as its exact mirror; Claude and Codex
  compatibility files point back to that canonical instruction
- focused proof: `test_bootstrap_materializes_one_proportional_verification_rule`

The projection contract is sparse by design. The host may advertise MCP/tool capability, broker
grants, uploads, and retrieved context, but it must not invent goals, success criteria, tool results,
downloadable artifacts, or provider-specific workflows. The worker receives real capability context
and decides the best path.

When a trusted host client passes existing upload metadata, xPerfect reuses the existing file path
contract instead of adding a second upload route:

- virtual `/uploads/...` paths can map to `WPR_LIBRECHAT_UPLOADS_ROOT`
- owner-scoped uploaded bytes require an exact stable upload token or owner-scoped virtual path
- trusted `selected_files` entries must use that stable reference; a display filename is never file
  authority
- two authorized stable references remain two worker files even when their display names or proposed
  workspace paths match; xPerfect assigns deterministic collision-safe workspace paths and never
  deduplicates them by name/path alone
- extracted text can be materialized directly
- metadata-only attachments become an `uploads/*.metadata.json` manifest

Any actual `source_path` copy is still gated by `WPR_BOOTSTRAP_SOURCE_ROOTS`, symlink rejection, and
the bootstrap size limit. Request-derived metadata must never become an arbitrary host-file read.
xPerfect must be file-type agnostic: PDF, DOCX, XLSX, PPTX, media, archives, and unknown extensions
are treated as user artifacts for the wrapped worker to reason about. Extracted text is a convenience
input, not a replacement for the original bytes when the user asks for layout-preserving redaction,
editing, analysis, conversion, or returned downloadable files. If the original bytes cannot be
projected safely, xPerfect should emit a metadata/blocker manifest and report the blocker rather
than silently converting the problem into a `.txt` task.

Enterprise LibreChat deployments that use the normal LibreChat upload/file-transfer flow should
mount the same upload storage read-only into the xPerfect VM and point both
`WPR_LIBRECHAT_UPLOADS_ROOT` and `WPR_BOOTSTRAP_SOURCE_ROOTS` at that mount. For Azure Container
Apps plus VM deployments, this usually means mounting the LibreChat Azure Files `uploads` share at
the xPerfect upload root. LibreChat can then stay config-only: it passes
`{{LIBRECHAT_BODY_FILES_JSON_B64}}` metadata to xPerfect, xPerfect resolves the virtual
`/uploads/<user>/<file>` path against the trusted mounted share, and the worker receives a copied
workspace file before launch. xPerfect also amends the project instructions with the canonical
`uploads/<safe-filename>` paths so a worker does not depend on user-visible filenames that contain
spaces or unsafe characters.

In enterprise mode, xPerfect treats the first segment after `/uploads/` as the authenticated
owner/user id. That is the cross-user safety boundary for shared upload storage. If a LibreChat
deployment stores upload metadata as `/uploads/<conversation-id>/...`, `/uploads/<file-id>/...`, or
any other layout where the first segment is not the authenticated user id, xPerfect will not copy
the bytes and will fall back to a metadata manifest until the deployment provides an owner-scoped
upload projection. When only model-visible attachment text or a display filename is available,
xPerfect does not search the mounted upload directory for matching bytes. It projects safe text or a
blocker manifest. A missing, unmatched, duplicate, or filename-only selected-file identity fails
closed; xPerfect never chooses the newest same-owner file as a substitute.

## Conversation authorization across restart

Conversation broker bearer authority is run-local memory, not persisted worker bootstrap or replay
metadata. Initial admission attaches the durable provider request and run atomically before starting
compute. An exact retry can refresh the lost bearer only after the stored request authority matches;
changed authority requires a new logical turn. Refresh keeps the accepted run and instructions.

If restart loses that bearer, the affected run remains `needs_input` and its provider request reports
`failed` with the structured cause. For an explicitly stateless request, a failed grant-required turn
with no active lease may stop blocking queued siblings. Persistent sessions remain ordered behind
that input boundary. Operator Pause, paused runs, Work Stop, compute-release claims and active leases
remain admission fences. Explicit Stop still cancels the exact resumable run after its request has
reported failure, and a repeated Stop finishes an interrupted cancellation handoff.

## Source-Specific Best Practice

### A. Host CLI login projection

Best when the host machine already has working local Codex and Claude Code logins.

For Docker sandboxes, minimal host CLI auth is copied into the worker home according to
`bootstrap_profile`.

For host-native workers, the CLI runs on the host and uses the host's existing CLI/browser/OS
session directly. The typed v1 capacity dimensions are separate: 2 active conversation turns per
CLI profile/family lane, 3 active missions per CLI profile/family lane, 4 active missions per
account, and 12 active missions per tenant by default. Repository mutation scope is an additional
one-owner exclusion only when a host mission declares that exact scope; it is not a general
"one worker per family" rule. Host resource headroom is a separate measured vector.

Every host CLI version/help/auth/readiness subprocess runs only after a durable provisional capacity
reservation is live. A failed or expired preflight releases that reservation and cannot create or
accept work. Successful admission later acquires the exact run lease under the same typed policy.

For an owner-managed Claude login, `CLAUDE_CONFIG_DIR` remains the worker's original session
store. The CLI's native `CLAUDE_SECURESTORAGE_CONFIG_DIR` selector chooses the already-authorized
owner credential store independently, and the exact combined environment must pass native auth
status before launch. This preserves the session UUID, history and working directory when a
projected access token expires. No credential or session file is copied, and selected personal
accounts and explicit API/enterprise routes keep their existing authority.

Host-native CLI subprocesses receive a minimal runtime environment. Parent process secrets, provider
API keys, callback secrets, and LibreChat internals are not inherited by default.

Capability projection is additive, not substitutive. When xPerfect writes worker-local CLI config
for broker MCP grants, it must preserve the selected worker type's native host capabilities unless
an operator explicitly configured a locked-down profile. For Codex host workers this means the
worker-local `CODEX_HOME/config.toml` keeps allowlisted native MCP/tool definitions, including
bundled plugin manifests such as computer-use when present, then appends the scoped
`glasshive-user-capabilities` broker block. Full-access Claude Code host workers reuse that same
selected native stdio source in their explicit private MCP config, beside the scoped broker, and
keep the CLI's native Chrome integration when supported. Native server `HOME` remains the OS owner
unless explicitly configured; the worker's own state stays isolated. Disabled or unselected servers,
workspace-limited profiles, and `native_tools: false` never gain these host tools. Broker names take
precedence. Native tool allow/deny lists are preserved on the existing selected MCP child's stdio
transport. Only filtered servers receive the transparent process adapter; unfiltered servers stay
direct. It filters tool discovery and rejects excluded calls, retaining schemas, paging, notifications,
errors and other MCP traffic. The private child command and tool policy are hash-bound, and changed
or unreadable policy closes the transport. Declared environment and native stderr are retained; the
adapter owns its child process group through cancellation and exit. Claude's managed permissions,
hooks and explicit per-run settings remain unchanged: disabling hooks cannot disable this filter.
A server with a
Codex-only working directory remains omitted with a diagnostic. Actual Computer/app execution is a
separate parity gate; metadata connection or screen capture alone is insufficient. No user MCP
configuration or credential file is copied into the mission.

Claude effort uses one native value set across preferences, delegation, persisted bootstrap, and
host or sandbox CLI transport: `default`, `low`, `medium`, `high`, `xhigh`, and `max`. The default
omits the CLI override. Explicit supported values remain unchanged, and the existing launch-time
CLI help check rejects a value an older installed version cannot support. Invalid values remain
rejected; they must not be silently dropped from stored worker configuration.

Current validated local footprints on this machine:

- Codex auth state exists in `~/.codex/auth.json`
- Codex configuration exists in `~/.codex/config.toml`
- Claude Code user state exists in `~/.claude.json`
- Claude settings exist in `~/.claude/settings.json`
- Claude project/local settings are officially supported through `.claude/settings.json`, `.claude/settings.local.json`, and `.mcp.json`

Best practice:

- project only the minimal provider-specific files needed
- keep project-scoped MCP and instructions in the workspace, not only in the user home
- prefer `clean-room + bootstrap_bundle` when repeatability matters more than inheriting the host personality

### B. Direct API-key projection

Best when a client or operator wants an explicit provider key available inside the worker sandbox.

Best practice:

- inject via `bootstrap_bundle.env`
- keep the env set explicit and minimal
- never write raw secrets into committed docs or repo config

### C. Connected-account projection from LibreChat-compatible hosts

This is the important one.

Best practice:

- xPerfect should **not** directly consume LibreChat internal token storage as its product contract
- instead, a host application such as LibreChat, Viventium, or another compatible client should act as an
  **optional auth broker**
- the broker should resolve the user's connected account and materialize a provider-specific projection for the sandbox
- that projection can be:
  - ephemeral env
  - a short-lived auth file
  - a provider-specific CLI login projection
  - a runtime-local callback or refresh helper

This keeps xPerfect independently usable by other clients.

2026-05 preferred connected-account projection for LibreChat-compatible hosts:

- the host projects a single `glasshive-user-capabilities` broker MCP through `bootstrap_bundle`
- provider OAuth/API tokens stay in the host and are never copied into the xPerfect workspace
- the worker receives only a short-lived broker grant scoped to user/conversation/worker/run and
  source-of-truth-approved server names
- the broker dynamically re-exports native typed tools where possible, so the worker can decide
  which connected-account tool to call without the host chat model choosing for it
- grant-bearing MCP config, Claude local settings, and Codex config files are written with
  owner-only permissions in both host-native and sandbox materialization paths
- broker config must not erase native worker/browser/computer/MCP capabilities; it is added beside
  them so the worker can choose direct native tools, brokered connected-account tools, or both
- `context` may mention the broker compactly, but large schemas, token material, and provider
  credential state belong in bootstrap/tool results, not prompt text

## Why This Does Not Break Stable LibreChat

xPerfect remains a separate service.

- it does not require changes to the current working LibreChat runtime to exist
- it does not need to mutate existing connected-account storage
- integration can be added through a broker or MCP client layer later

## Current Phase-1 Runtime Support

The current xPerfect runtime now supports:

- worker-level `bootstrap_profile`
- worker-level `bootstrap_bundle`
- sandbox seeding of Claude project MCP config via `.mcp.json`
- sandbox seeding of Claude local settings via `.claude/settings.local.json`
- host-native seeding of project MCP config and local settings from the same bootstrap fields
- workspace instructions via `CLAUDE.md` and `AGENTS.md`
- generic file seeding
- runtime env projection into shells and task runs
- a non-secret bootstrap manifest written into the sandbox for auditability

## Best-Practice Summary

A. Do not touch the stable host app unless the client explicitly opts into xPerfect auth brokering.

B. Treat auth projection as a provider-specific plugin behind a universal xPerfect bootstrap contract.

C. Keep the xPerfect runtime independently usable by any client that can supply bootstrap data or call its API/MCP surface.

## Grok private native state

Grok uses a worker-private `home/.grok` outside shared project files. The adapter pins `GROK_HOME`
and removes any projected `GROK_AUTH_PATH` override. It never copies a host home or discovers ambient
provider keys. Supply an explicitly authorized `XAI_API_KEY` through existing bootstrap environment
projection, or provision the selected native account in that private Grok home. Conflicting cached
subscription/API-key identities fail before launch. A cached copy of the same API key written by the
native agent is permitted on subsequent turns.

Optional `grok_mcp_servers` contains native ACP server objects. The bridge checks advertised HTTP/SSE
support; the selected worker's tool authority still owns which servers may be projected. Claude and
Cursor compatibility MCP scanning is disabled. Project-local native Grok and `.mcp.json` discovery
remains subject to Grok's native trust/configuration policy and must be included in tool-grant QA.
No universal tool-isolation guarantee follows from these switches.

Updater, product telemetry, trace upload and external OpenTelemetry switches are pinned off for the
managed process; ambient OTEL exporters are stripped. These switches do not claim or change the
provider's account-level training/retention terms. Auth/configuration and binary compatibility must
be checked against the exact installed Grok version, independently of the inspected upstream source.

Grok account setup uses the existing owner-scoped account store, native device login and a quiet
ACP initialize/authenticate verification. The selected account projects `GROK_AUTH_PATH` to its
private `grok/auth.json`; `GROK_HOME` remains the persistent worker home. Account authentication
and session history therefore do not share a directory. Docker mounts the selected account through
the existing binder and grants the worker ACL access only to its Grok directory and auth file;
write access to the directory supports native atomic token refresh. The binder's lease and cleanup
remain authoritative. A manually supplied auth-path override cannot bypass account selection.

The standalone deployment credential name is `XAI_API_KEY`. Enterprise projection restricts it to
`grok-build`; Codex and Claude do not receive it. Grok auth/config/credential files are excluded from
clean-room imports and workspace duplication. Run-scoped broker grants retain their existing
scope and expiration when transported through native ACP.
