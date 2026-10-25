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

An optional `glasshive_capability_broker.tool_catalog` carries version, grant ID, expiry,
server name/URL, and the complete tool names, descriptions and input schemas. Grok validates it
against the current broker and exact configured MCP server. The catalog is all-or-omit, limited
to 64 tools and 64 KiB of UTF-8 JSON; stale, malformed or missing data keeps ordinary native
search/use discovery. A resumed or inherited worker cannot reuse a catalog from another grant.
Both standalone and managed hosts render it once in an untrusted-data element, with delimiter
characters JSON-escaped and no grant IDs, expiry or transport URL in model text. Standalone
rendering needs no host prompt package. The model can use known metadata directly or search;
actual calls still use native MCP and current authorization. Hosts may reuse current scoped
metadata, but should omit this optional data when unavailable or too large for the existing
bootstrap carrier. It must not add network discovery before a turn. No latency benefit is implied.

For Prompt Workbench scheduled workers, the native server validates the callback task and run
against the declared owner and deterministic scheduled conversation and parent IDs. The server
retains the validated task ID in its typed `viventium_launch_authority`. The shared callback
owner derives the user from the persisted worker and restores that same task/run context for
queued, started, blocked, and terminal lifecycle events. Missing task or run identity closes new
callback production. Ordinary conversation callbacks keep their existing context.

The receiver still verifies the signed worker/run body and persisted scheduled scope. Stored
outbox payloads remain immutable on reload; their existing endpoint and signing secret remain
available to the finite retry and dead-letter owner. This projection does not rewrite callbacks
accepted before the repair.

The bootstrap materializer projects these prompt/context files into its admitted worker
workspace. Host-native adapters use the separate placement described below; they do not
overwrite an existing project's instruction files:

- `project-definition.md`
- `work-log.md`
- `harness-prompt.md`
- `AGENTS.md` as the canonical Codex-style project instruction file
- `agents.md` as a compatibility mirror
- `claude.md` / `CLAUDE.md` as Claude Code compatibility files that import or mirror `AGENTS.md`
- `codex.md` / `CODEX.md` as legacy compatibility mirrors only

For host-native Codex, Claude Code and Grok **mission** workers, `project-definition.md`,
`work-log.md` and `harness-prompt.md` remain in the work folder. Generated `AGENTS.md`, its
compatibility mirrors and scoped native configuration instead live under that worker's private
home at `.glasshive/native/project`. Native selectors load that private context while the working
directory remains the selected work folder. Existing user/project `AGENTS.md` is preserved.
Conversation workers use their own private runtime-context path rather than this mission-file
materialization. This distinction is exercised by
`test_host_codex_runtime_materializes_required_workspace_files` and
`test_host_mission_mode_retains_workspace_and_completion_contract`.

Bootstrap-projected instructions contain one proportional-verification rule. The worker chooses
verification depth from the user's explicit success criteria, requested rigor, risk, and concrete
defects. It uses the smallest evidence that proves the result and repeats an equivalent check only
after a relevant output change or a detected defect. This stays universal: the bootstrap does not
hardcode one host prompt, QA case, provider, file type, model effort, resource class, or token limit.

The worker prompt lineage is direct and testable:

- source: the editable worker-facing constants at the top of
  `runtime_phase1/src/workers_projects_runtime/bootstrap.py`
- assembled runtime contract: `GLASSHIVE_WORKER_PROJECT_CONTRACT`
- materialized worker instruction: `AGENTS.md` with `agents.md` as its exact mirror at the owning
  placement above; Claude and Codex compatibility files point back to that canonical instruction
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

The native attachment context labels each actual projected representation as `original`,
`extracted_text`, or `metadata_only`. Supplied extraction is usable text; reading it does not prove
access to the original bytes. A metadata manifest reports unavailable input and remains a blocker
for work that needs those bytes. The host retains explicit current text, including empty text, and
may fill absent text only from the same file ID and source in its existing owner-checked selected
history. Current file identity and order remain unchanged.
For declared `source=text`, the exact string body is the input, including empty and whitespace-only
text. It takes precedence over an available original path. Nontext workspace filenames gain a
`.txt` suffix; the original filename stays in metadata. An explicit empty body uses the existing
`allow_empty` materialization flag. A missing or nonstring body remains a metadata-only blocker.

A trusted file-only authored input can have an empty text segment. Core preserves that segment
in both the delegation context and task packet, with the same source identity and order. Native
validation requires exact packet/context text equality; it does not invent a transcript. Upload
references and bytes still pass through the separate owner-scoped file projection below.

Any actual `source_path` copy is still gated by `WPR_BOOTSTRAP_SOURCE_ROOTS`, symlink rejection, and
the bootstrap size limit. Request-derived metadata must never become an arbitrary host-file read.
xPerfect must be file-type agnostic: PDF, DOCX, XLSX, PPTX, media, archives, and unknown extensions
are treated as user artifacts for the wrapped worker to reason about. Extracted text is a convenience
input, not a replacement for the original bytes when the user asks for layout-preserving redaction,
editing, analysis, conversion, or returned downloadable files. If the original bytes cannot be
projected safely, xPerfect should emit a metadata/blocker manifest and report the blocker rather
than silently converting the problem into a `.txt` task.

The configured upload root and checkout fallback are resolved to their canonical physical root
before projection. A retained owner/tenant-signed upload entry may reproject an obsolete root
alias through the existing upload projector only when that alias still resolves to the exact
same file and relative owner/file address under the current declared upload root. The original
record, file ID, workspace order and bytes stay unchanged. New source grants normalize an omitted tenant to the same default `local` namespace used by worker storage. A retained v1 grant with an empty tenant can be verified only for that default namespace, with its exact nonempty owner, source path and dedicated source signing key. Nondefault tenants cannot use that compatibility form. No unsigned or ownerless compatibility grant is accepted. Missing files, invalid signatures,
foreign roots and symlinks below the root remain failures; recovery does not add trusted roots.

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
The exact invocation bundle is staged in memory before a new queued run is published, including
provider-request and idempotent run identities. The memory condition is released before store
publication. Failed creation removes only its staged bundle; a consumed bundle is not reattached.
Existing reservations use the current-state grant attachment gate, so an active provider invocation
cannot have its authority replaced by a duplicate assignment. Assignment does not reopen a current
`needs_input` run; explicit authorization retries retain their existing reopen policy.

Parallel clean-room runs carry the required capability broker grant and optional native peer and
context grants through the same exact-generation tmpfs files. The launcher uses the shared
clean-room environment allowlist, rejects other keys and invalid token values before any Docker
effect, and records every projected key for the existing exit/cancellation scrub. Trusted native
binders still own each grant's worker/run identity, scope, expiration and revocation; transport
does not mint authority or persist bearer values. A partial write or failed post-write attestation
uses the same exact-generation cleanup; a replacement generation is never cleared.

Restricted Codex setup renders its broker descriptor without a bearer. The existing launch policy
check requires the current run's bearer and an exact match to the restricted configuration before
building the command. Setup does not persist the bearer or expose other native tools.

If restart loses that bearer, the affected run remains `needs_input` and its provider request reports
`failed` with the structured cause. For an explicitly stateless request, a failed grant-required turn
with no active lease may stop blocking queued siblings. Persistent sessions remain ordered behind
that input boundary. Operator Pause, paused runs, Work Stop, compute-release claims and active leases
remain admission fences. Explicit Stop still cancels the exact resumable run after its request has
reported failure, and a repeated Stop finishes an interrupted cancellation handoff.

### Codex resumed developer authority

Updating the worker config alone does not update an existing Codex session's developer messages.
For admitted host conversation runs, the runtime splits the exact composer output into stable
instructions and the entire declared dynamic tail (saved memory, fact guard and Feeling capsule).
Fresh sessions use ordinary exec. Unchanged, native-acknowledged units also use ordinary exec.
Changed units use the native `thread/inject_items` developer channel before ordinary exec resume;
stable changes inject the exact dynamic tail after stable instructions to retain tail-last order.
the preflight runs in the existing supervised child process group and then execs in the same PID.
The current model, effort, history, permissions and inference transport remain unchanged.

The existing provider-session manifest records delivered digests and a bounded native-ledger
checkpoint for the exact session and context epoch. Config materialization is not delivery proof.
An acknowledged inject must also persist in the native ledger after the control child is reaped.
Bounded shutdown may terminate that child; its exit code does not replace exact ledger proof. Compaction
revalidates replacement history; an uncertain prior write can reconcile without duplicate injection.
Unconfirmed updates fail with a typed recoverable result rather than running with stale authority.
Before model output, the existing once-only recovery fence can rebind using the authenticated
complete canonical source in the exact request/run's ephemeral bundle. A delta is never used as
that complete seed. Missing source, failed recovery, Stop and the deadline retain truthful results.
The public rollout path returned by native resume is preferred; the internal database is a fallback.
Content-free receipts distinguish preflight phases from inference. Their timing must be measured on
the loaded path; a loopback protocol fixture does not prove live latency or model quality.

Append-only developer messages cannot remove a prior unit. A declared dynamic-tail removal or a
Feeling present-to-absent transition uses the existing session-rebind and admitted-history seed path.
Ordinary active-tail updates keep the native session; no off-state instructions are fabricated.

## Source-Specific Best Practice

### A. Host CLI login projection

Best when the host machine already has working local Codex and Claude Code logins.

For Docker sandboxes, minimal host CLI auth is copied into the worker home according to
`bootstrap_profile`.

For host-native workers, the CLI runs on the host and uses the host's existing CLI/browser/OS
session directly. The typed v1 capacity dimensions are separate: 2 active conversation turns per
CLI profile/family lane, 3 active missions per CLI profile/family lane, 4 active missions per
account, and 12 active missions per tenant by default. Manual host missions share a conservative
mutation lease: model-supplied repository names or bootstrap fields cannot buy independent access.
Host resource headroom is a separate measured vector.

Standalone Chat's Queen can run independent helpers within that capacity. The exception belongs
only to the authenticated owner's current accepted goal and exact worker/run (including a saved
worker reused by the Queen), or its recorded current follow-up effect. A fresh helper uses the saved
route, model and account choice. A reused worker keeps its own model, account and tools; its scoped
worker and Allowed AI admission are rechecked instead of requiring a new route. Execution checks
the durable binding again. Stopped or held goals,
foreign or stale bindings, historical result access and later manual work receive no exception.
No client-specific execution flag or full-access setting is needed. This changes mutation admission
only: it grants neither additional native permissions nor exclusive control of the shared desktop.
Existing account credential leases remain exclusive, so jobs using one selected account may queue
even when profile, tenant and machine capacity remain available. Slot counts are ceilings, not a
promise that every account can run that many jobs at once.

The conversation processor pool uses the existing service dispatch budget. Account and tenant
mission limits cannot cap that pool across CLI families. Dispatch still requires the exact
configured conversation lane and measured resource reservation; a full service pool can still queue
work. This does not give Main priority over another admitted conversation.
This is a pool of service execution threads, not initialized native CLI processes. Saved native
session history and a retained workspace likewise do not establish a live idle agent process;
see [native startup and reuse](02_Architecture_and_Components.md#native-startup-retained-sessions-and-idle-process-preparation).
The legacy generated `GLASSHIVE_CONVERSATION_EXECUTOR_WORKERS` value is not read by this runtime;
it does not set the service dispatch budget.

Every host CLI version/help/auth/readiness subprocess runs only after a durable provisional capacity
reservation is live. A failed or expired preflight releases that reservation and cannot create or
accept work. Successful admission later acquires the exact run lease under the same typed policy.

Local CLI version, help and MCP-readiness probes capture complete stdout and stderr in private,
automatically removed temporary regular files. Some native CLIs exit successfully after a partial
pipe write; that truncated output must not be mistaken for missing native flags. The same version,
required-capability, exit-status and timeout checks still fail closed. Claude's binary-identity help
cache uses this shared capture owner, so effort, Chrome and restricted/read-only checks consume the
same complete help evidence. Probe capture creates no provider request or worker, and does not
change primary/fallback selection or admission policy.

For an owner-managed Claude login, `CLAUDE_CONFIG_DIR` remains the worker's original session
store. The CLI's native `CLAUDE_SECURESTORAGE_CONFIG_DIR` selector chooses the already-authorized
owner credential store independently, and the exact combined environment must pass native auth
status before launch. This preserves the session UUID, history and working directory when a
projected access token expires. No credential or session file is copied, and selected personal
accounts and explicit API/enterprise routes keep their existing authority.

Ambient operating-system logins are available only outside enterprise authentication.
`GLASSHIVE_SECURITY_MODE=multi_user` and either legacy enterprise flag disable host-login
copying, current-login discovery and current-account warming. A legacy enterprise flag still
enforces this credential boundary beside explicit `local` or `legacy_compatibility` mode;
it does not change those modes' separate isolation compatibility rules. Bootstrap, native
launch and provider readiness use the same enterprise-authentication decision. A named
account, scoped broker or explicitly provisioned deployment route keeps its own authority;
missing authority does not fall back to the server owner's login.

Host-native CLI subprocesses receive a minimal runtime environment. Parent process secrets, provider
API keys, callback secrets, and LibreChat internals are not inherited by default.

Capability projection is additive, not substitutive. When xPerfect writes worker-local CLI config
for broker MCP grants, it must preserve the selected worker type's native host capabilities unless
an operator explicitly configured a locked-down profile. For Codex host workers this means the
worker-local `CODEX_HOME/config.toml` keeps allowlisted native MCP/tool definitions, including
enabled bundled plugin manifests, then appends the scoped
`glasshive-user-capabilities` broker block. Full-access Claude Code host workers reuse that same
selected native stdio source in their explicit private MCP config, beside the scoped broker, and
keep the CLI's native Chrome integration when supported. Native server `HOME` remains the OS owner
unless explicitly configured; the worker's own state stays isolated. Disabled or unselected servers,
workspace-limited profiles, and `native_tools: false` never gain these host tools. An installed
executable alone is not an MCP selection: do not synthesize extra servers from known application
paths. Preserve the native selected configuration and manifest, so a legacy standalone server does
not compete with the current unified browser/desktop interface. Explicitly configured servers remain
supported, and native tool access restrictions still apply.

A named Codex account keeps its native `CODEX_HOME`, login refresh and saved session history. The
worker's already-materialized policy is loaded through Codex's native config profile, published as
an owner-only regular file under that selected account. This retains the exact model, effort,
developer authority and selected native MCP definitions without copying credentials, moving
history or adding a broker. A profile cannot inherit undeclared tools from the account's base
configuration; an unexpected MCP/plugin declaration there fails closed. Account resealing retains
the safe regular profile and its private permissions. Updated or removed worker connections are
refreshed from the same policy on the next admitted start.

The account receives private regular copies of only the already-selected native package assets,
at the native active versions from the canonical owner cache. Mutable plugin data, connector login,
global skills and account credentials are not copied. The account sealer preserves execute bits
only for package files whose identity, paths and bytes match their canonical code assets. A cache
version that would override the selected version fails closed rather than deleting local state.
Worker-specific enablement remains in its native policy; cached packages alone grant no capability.
`native_tools: false` projects no such assets and disables native plugin skills and hooks.

Standalone worker skills remain in the same private worker catalog. The existing capability
projector also exposes that catalog under the native `HOME/.agents/skills` root, so changing only
the selected account's `CODEX_HOME` does not hide them. Restricted preparation retracts only this
derived link when it still points to that same catalog; real local files and unrelated links stay
intact. Both mission and conversation preparation apply the same native-tools guard under the
existing private configuration lock.

Native exec supports config profiles; app-server rejects its `--profile` flag. On reviewed native
ABIs, the existing relay sends that same private policy through first-class
`thread/start.config` or `thread/resume.config`, preserving CLI overrides, exact model and effort,
permissions, selected MCP definitions and plugins. Private policy values stay out of command-line
arguments. The exact reviewed version, server-request methods and native control schema must
match before this route is used; otherwise the original native exec path stays intact.
Native mission control does not require the optional warm pool to be enabled.
An acknowledged native Cancel becomes the existing interrupted run outcome only after the same
bound turn reports interruption. Failed, rejected or unconfirmed cancellation does not hide a
provider failure; native completion that wins the race remains completion.
Native-control status is a read-only verified snapshot, not a lifecycle action. Reads share no
exclusive lifecycle lock; each adapter derives action capabilities from the same verified native
context, and the service rechecks the exact running run and attempt before returning it. Mutating
controls retain their existing lifecycle lock, permissions and exact-target checks.
Startup-only policy or a named account can remain ineligible for the existing warm pool; a
validated cold relay retains native control without claiming warm reuse. Tools are never removed
or another account's process borrowed to manufacture warm eligibility. Native package/skill/hook
discovery and actual tool execution remain separate acceptance checks.

Preserve native arrays of tables too,
including enabled and disabled skill selections; rewriting config must not reactivate a retired
skill. Broker names take
precedence. Native tool allow/deny lists are preserved on the existing selected MCP child's stdio
transport. Only filtered servers receive the transparent process adapter; unfiltered servers stay
direct. It filters tool discovery and rejects excluded calls, retaining schemas, paging, notifications,
errors and other MCP traffic. The private child command and tool policy are hash-bound, and changed
or unreadable policy closes the transport. Worker preparation and launch validation share the existing
private per-worker state lock. Policy files are published atomically, and unchanged content is not
rewritten: opening, inspecting or resuming a workspace cannot expose a partial policy to launch
validation or its active MCP child. A genuinely changed policy still fails the child's old digest
closed. Declared environment and native stderr are retained; the
adapter owns its child process group through cancellation and exit. Claude's managed permissions,
hooks and explicit per-run settings remain unchanged: disabling hooks cannot disable this filter.

An explicit `provider_capabilities.native_tools: false` is a restriction on the selected native
conversation harness. The host conveys it in its existing signed bootstrap bundle, after removing
inherited workspace and broker grants. Native verifies that signature before admission and forces
stateless execution. A missing host signer fails the request. This does not change voice participant
authorization or grant authority to an unverified participant.

Grok restrictions use a fresh session and an empty private child directory, with no MCP servers,
workflow child lanes, workspace instructions, or discovered skills. The ACP inline profile combines
the recognized `ToolSearch` allowlist with `search_tool` and `use_tool` exclusions; an empty
allowlist would inherit native tools. Native requires an explicit empty tool inventory for the exact
session before a prompt. After model, effort, and developer authority setup, it refreshes and checks
the inventory again. Missing, malformed, nonempty, or later native tool traffic closes the turn.
Preopen inventory notifications are retained in a bounded buffer keyed by the native session ID.
The selected model and effort remain unchanged. Ordinary unrestricted conversations retain their
existing session and tool behavior.

Claude restrictions use the advertised safe mode, an empty native tool list and strict empty MCP
configuration. The selected account, model, effort and explicit developer authority stay intact.
Private native context placement and saved selectors are neither materialized nor applied to this
restricted command. Automatic workspace instructions, skills, hooks, memory and browser access are disabled. Native
structured output may expose its `StructuredOutput` formatter; it grants no general action or file
tool. The fresh private workspace and stateless execution remain required.

Codex restrictions without a declared broker use strict configuration, empty MCP, a fresh private
workspace, ephemeral execution and disabled native action features and goals. Its native
`request_user_input` remains in the manifest but rejects calls in Default execution mode. A declared
broker retains its existing signed descriptor and current-grant requirements; a malformed broker
does not become an absent broker.

A server with a
Codex-only working directory remains omitted with a diagnostic. Actual Computer/app execution is a
separate parity gate; metadata connection or screen capture alone is insufficient. No user MCP
configuration or credential file is copied into the mission.

Claude effort uses one native value set across preferences, delegation, persisted bootstrap, and
host or sandbox CLI transport: `default`, `low`, `medium`, `high`, `xhigh`, and `max`. The default
omits the CLI override. Explicit supported values remain unchanged, and the existing launch-time
CLI help check rejects a value an older installed version cannot support. Invalid values remain
rejected; they must not be silently dropped from stored worker configuration.

Named Codex subscription accounts use the existing account setup owner, private `CODEX_HOME`,
lease, process lifetime and native `login status` verification. On an explicitly local,
single-user host with a loopback public URL, setup uses the provider's normal `codex login`
browser flow. Codex owns the callback and token exchange; xPerfect only presents the reviewed
provider authorization URL. It does not copy the global login or require device-code sign-in to
be enabled. Contained, provisioned, enterprise and non-local routes keep `login --device-auth`.
The reviewed setup response labels its flow as browser, device code, authorization code or
unknown. Clients use that field for sign-in instructions; they do not infer the flow from a
provider name or assume that every Codex sign-in needs a one-time code.
Credential sealing recognizes the native executable aliases in both `codex/tmp/arg0/` and
`codex/tmp/arg0-macos/`. Only the existing declared helper names and verified selected executable
targets qualify; credential links, other directories and redirected targets remain rejected.
See the [official native authentication guide](https://learn.chatgpt.com/docs/auth).

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

## Selected personal Library context

Chat accepts exact personal Library references as `library_refs: [{item_id, version_id}]`.
The authorized owner selects versions, including a Bundle's pinned versions. Acceptance retains
those versions and their canonical upload revisions in the same transaction as the user turn.
A retry uses its accepted selection even if the personal catalog entry was later removed.
Omitting the field inherits the conversation's selection; an explicit empty list clears it for
future turns. Neither choice rewrites accepted work or changes model, account, tools or grants.
Conversation and turn selection records contain compact identities, file revisions and safe
labels/pending state. Saved skill/package bodies remain in their canonical retained versions;
they are not copied into each turn or status response. Reusing the same exact selection still
validates/pins its canonical retained versions, without resolving its bodies again. Status polls
read public turn fields and the latest small delivery metadata, not whole stored request packets.

Direct Queens receive selected text after the full unchanged conversation history. The deployment
can set `CoordinatorConfig.library_text_chars` (default 24,000; range 0–262,144). This is a total
selected-text limit, not a history limit. Each text source either fits intact or is marked
`Not loaded for this Queen; available to Helpers`. Binary material and text over that limit keep
their exact canonical file references. The reply cannot claim that an omitted source was read.
No Library management tools are added to the Fast Queen's tool set.
The appended material uses the existing protected reference-result carrier, not a new user
message. It stays beside the latest user request in that request's existing turn group. Its
digest prevents silent clipping; it grants no tool or instruction authority and creates no
provider tool-call/result pair.
Its explicit identity and digest also identify it as a transient reference for history counting.
The saved caller-history cursor excludes that one appended reference, so the next user's message
or Helper result cannot be skipped. The original history stays unchanged; no extra reference copy
is inserted into old user turns. The public provider transport rejects a forged internal reference
marker. This reuses the existing cursor and source validation, without a second context channel.

Native Queens receive the same selected canonical files through existing bootstrap projection.
The actual foreground worker binds exact upload revisions through Files before its first native
start and before reusing that session for a later selected turn. Unbound file entries are not
passed directly to native startup.
An accepted file is copied once. Later native starts reuse its private completion receipt and
preserve any worker edits. A changed filesystem device number alone cannot block this completed
no-op when the accepted owner, version, scope, canonical workspace path and root inode still match.
Only the current service-private receipt grants that exception; a legacy receipt or a pending copy
does not. The receipt is checked again under the projection lock, so its loss cannot enable a write.
Continuity capture uses the same rule and validates the accepted manifest before retaining this
completed projection. No stored binding is rewritten and no file is copied to repair a device-number
mismatch. Changed roots, inodes, scope or versions still fail closed.
The selection metadata says that files are provided, not that the model has read them. Helpers
receive the accepted upload identities and revisions through normal Files binding before native
execution starts. Large selected knowledge uses the existing lookup delivery mode and does not
displace the originating user objective or exact conversation sources. If a worker is launched
from a result turn, the most recent user turn at or before that result remains the originating
objective. Later user input remains available; a rewritten delegation brief is not a replacement
for the original conversation.

Lookup-only packing changes model input, not the existing host snapshot contract. Context
inspection and run preparation still read and hash complete selected text sources; native
retrieval rechecks current authority against the exact accepted snapshot. This is not a claim of
lazy disk access, zero preparation cost or a measured improvement in response latency.

Knowledge selection does not install a native package, apply a text skill to an unknown future
worker, or authorize new tools. Such requirements remain explicit pending items until the actual
supported target and required approval are known. Saved personal versions and templates retain
references, not cloned credentials, provider sessions or conversation histories. Text skill
projection is supported only by the declared native profile; a copied file is not proof that a
different harness loaded it.

Full-access host Claude workers select complete declared text-skill folders through the native
session-only `--plugin-dir` root-`SKILL.md` layout. This keeps `--setting-sources ''`, the selected
account, strict MCP, managed policy and explicit per-run settings intact. Launch checks the exact
owned regular files and supporting directories against the declared bytes; links, extra entries,
changed files and native plugin components are rejected. Source references and Library labels
cannot select a folder. Restricted, read-only, workspace-only and web-disabled paths retain their
existing controls. Native discovery supplies the namespaced skill name (for example,
`report:report`); only a successful native `Skill` call proves load. See the native
[plugin layout](https://code.claude.com/docs/en/plugins-reference#standard-layout) and
[session selector](https://code.claude.com/docs/en/cli-reference).
Library readiness uses that same launch policy. A policy change reports an already-reviewed skill
as Not available with its current reason, including on an idempotent retry. It preserves the
approved contribution and files; restoring supported policy restores the next-start readiness.
A skill first denied by policy changes to Needs review when policy permits it. It receives no
native projection until the owner gives a fresh explicit approval.

Owner-visible Chat snapshots expose `library.refs`, item names/versions and safe delivery metadata.
`loaded: true` for Direct means included in the request, not proof of model attention. Native
`delivery: native_file` means projected/readable. Snapshot metadata never contains native source
tokens, private projection paths or source text. Ordinary turns without a selection do not
initialize or query the personal Library.

Removing an applied native contribution uses its exact path and content hash. The shared cleanup
primitive removes only an unchanged file which no remaining contribution uses; edited files,
replacement links and shared files remain. The caller uses the existing native-start/lifecycle
guard and actual workspace root. Existing native refresh owns configuration on the next start;
this is not a second installer or a promise to unload already running native tools.

Standalone local conversations delegate through the same ordinary owner host admission used by
Run Project. Their server-configured exact model, effort and ready owner account remain explicit;
the private model selector must match the owner's durable accepted goal and configured route.
New Chat uses that same declared placement for connected accounts and reports the actual supported
account IDs, profile and authentication method before submission. A local host does not require a
Docker workspace merely to select its existing Codex or Claude account. Other deployment modes keep
their existing shared-container requirements. A Direct API account is labelled as Direct only when
the configured conversation model actually uses the Direct route; API-key authentication alone does
not select a harness or change the model. These checks start no worker and change no saved default.

Existing workspace mutation, capacity, account and policy fences remain. This local path does not
require a client-specific parallel-work environment grant or install selected skills on future
children. Explicit legacy client isolation/parallel policy keeps its existing authorization path.

## Grok private native state

Current-session Grok completion metadata projects native token usage into the normal terminal
result. Cached input counts once; output already includes reasoning. Tool loops use the latest
model response's prompt occupancy, rather than adding repeated prompts or adding the aggregate
turn again. A turn-only metric is usable only for one model call. Missing or malformed metrics,
foreign sessions and notifications outside the current prompt do not create measured usage.
The existing request/epoch calibration then owns conversation occupancy and context-budget
projection. This is token observability and budget evidence, not proof that a semantic answer
or owner-requested control succeeded.


Optional Grok 4.7 Fast availability comes from the selected worker account's ACP session model
options. A service-account catalog is discovery only and cannot choose a turn's effective model.
If that account offers standard Grok 4.7 but not Fast, the existing ACP model control selects
standard4.7 before prompting and checks the selected effort on its actual options. The admitted
owner/account, access, workspace, grants, native session and absolute deadline stay unchanged.
Other unsupported models and efforts remain errors. Stop during session setup prevents prompting.

The worker/session and completion contract retain the configured Fast choice and input-authority
digest. The owned native `session.started` event records both requested and effective model before
the prompt. Public content chunks, final responses, Responses and replay name the actual model,
with the original requested model when it differs. An elected quota fallback uses its existing
fallback model and current owned session/run as selection authority; the original request contract
remains unchanged and public output names the actual fallback model. A later turn resumes the same binding: it keeps
Standard while Fast is absent, or selects Fast if the account offers it again. No sticky model
cache, extra recovery worker, acknowledgement transport or service-catalog subprocess is used.
Resume and authority reload use native `noReplay`, so old transcript updates are not projected as
new progress or response previews. The ephemeral full source used for Codex authority recovery is
excluded from dispatched quota fallback bundles; permitted run-local grants remain intact.

Grok uses a worker-private `home/.grok` outside shared project files. The adapter pins `GROK_HOME`
and removes any unvalidated `GROK_AUTH_PATH` override. In the existing local current-account lane,
it supplies the canonical OS-owned Grok `auth.json` through native `GROK_AUTH_PATH`, without copying
credential bytes. Native token reads, atomic refresh writes and the refresh lock use that same path,
so subsequent workers retain refreshed login state. The credential file must be owner-only, and
its canonical owner-owned directory must not permit writes by another user. Worker session history,
configuration and authority remain private. This lane does not import login state for installed-owner,
multi-user, enterprise, selected-account or explicit-key boundaries. It never copies a host home or
discovers ambient provider keys. Supply an explicitly authorized `XAI_API_KEY` through existing
bootstrap environment projection, or provision the selected native account in that private Grok home. Conflicting cached
subscription/API-key identities fail before launch. A cached copy of the same API key written by the
native agent is permitted on subsequent turns.

Optional `grok_mcp_servers` contains native ACP server objects. The bridge checks advertised HTTP/SSE
support; the selected worker's tool authority still owns which servers may be projected. Claude and
Cursor compatibility MCP scanning is disabled. Project-local native Grok and `.mcp.json` discovery
remains subject to Grok's native trust/configuration policy and must be included in tool-grant QA.
No universal tool-isolation guarantee follows from these switches.
Before an enabled turn starts, the adapter waits up to the existing 30-second ACP startup budget
for the exact native session to advertise at least one command from the configured signed broker.
The broker publishes its policy-filtered catalog in one tools-list result; legitimate tool omissions
do not block connection readiness. Optional resource-only MCP servers introduce no barrier.
Missing readiness ends with typed `native_mcp_unavailable`; it never becomes successful tool-less
inference. No-required-MCP and restricted empty-inventory turns retain their existing contracts.

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


Native Grok MCP lifecycle receipts retain only current-session protocol event identity, configured
required-server name, typed connection status and counts. They omit endpoint, authorization,
reason prose and tool arguments. These events diagnose the native connection boundary. Once the exact native session reports MCP
initialization complete, a single cached native registry inspection can supply required-server tool
proof when the cosmetic command catalogue has not refreshed. It must show a resolved session and
each exact configured required server enabled and ready with enabled named tools. Missing, foreign,
disabled and empty results fail closed. Both evidence paths share the existing 30-second startup
deadline; no inference or capability call runs before that proof.

## Independent application inputs

The [application boundary contract](09_Application_Boundary_Contract.md) owns the project boundary
and standalone acceptance. A client supplies its own prepared instructions, context, files and
capability references. The runtime transports those inputs through its existing worker/session
owners; it does not interpret the client's domain state.

- `WPR_RUNTIME_ENV_FILE` selects one explicit runtime environment file. `load_runtime_env()` does
  not search application folders or load an application-specific environment file. Existing process
  values win; only the existing runtime allowlist is loaded. The legacy loader remains available,
  but an explicit neutral file takes precedence.
- `WPR_UPLOADS_ROOT` supplies the file root. Its canonical path is added to the existing
  `WPR_BOOTSTRAP_SOURCE_ROOTS`; no sibling checkout is discovered. File materialization retains
  trusted-root, symlink, size and owner/tenant signature checks. The legacy upload-root alias remains
  accepted. `GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE` and
  `GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS` select workspace policy independently of a client.
- Generic conversation headers use `x-glasshive-` identity/context names. Existing host-prefixed
  aliases remain accepted; conflicting dual carriers fail. Neither name grants owner delegation,
  system authorship or full access: the authenticated provider credential retains those decisions.
  `GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_SECRET` configures signed bootstrap verification without
  a client-specific secret name. The existing compatibility secret remains accepted.
- `launch_authority` is the neutral typed worker-launch envelope. The legacy alias remains accepted
  through the same validation. Conflicting dual envelopes fail; a new field name does not expand
  execution authority.
- A client that has already prepared its instructions may set
  `application_context_format: "opaque_v1"` in the bootstrap bundle. This path preserves instruction
  content and the declared final tail without running a domain-specific capsule parser. It cannot
  also request legacy semantic projection. Existing clients keep the previous projection until their
  own preparation and affected acceptance are complete. This option adds no prompt renderer,
  summary, context cache or cognitive module to the runtime.

### Native worker scheduling grant

An owner/client may supply the following grant in a worker's stored bootstrap. IDs and request
references below are synthetic. Destination URL, auth, signing key and payload policy remain with
owner setup; a worker cannot supply or change them through this grant.

```json
{
  "schedule_access": {
    "destinations": [
      {
        "destination_id": "destination-example",
        "label": "Application destination label",
        "request_data": {"request_ref": "request-example"}
      }
    ],
    "allow_worker_target": false,
    "manage_destination_ids": ["destination-example"],
    "defaults": {
      "once": {"misfire_grace_seconds": 3600, "catch_up_policy": "coalesce"},
      "interval": {"misfire_grace_seconds": 900, "catch_up_policy": "skip"}
    }
  }
}
```

The runtime projects `xperfect-schedules` through the existing native HTTP/socket MCP
transport when a current grant supplies a target. No implicit self-scheduling is enabled
in the public runtime default. An operator may set `WPR_NATIVE_SELF_SCHEDULE_ENABLED=true`
to give native workers without an explicit grant only their own worker target. Explicit
client grants stay authoritative; an explicit empty grant disables scheduling even when
that operator setting is on. API profiles receive no implicit grant, and coordination-only
conversation profiles keep their two-tool interface.

The standard `./xperfect start` host launcher enables that operator setting and supplies
`GLASSHIVE_PEER_RUNTIME_BASE_URL` from its actual API port. Private host config may turn it
off. A custom launch must set the existing worker-reachable runtime origin. Packaged workers
retain their existing per-workspace socket route. No new server or client-specific path
is required. Without a native route, optional implicit access does not block unrelated work;
an explicit grant without a route fails with `schedule_endpoint_unavailable`.

The projection mints a separate `schedule`-purpose token for the exact live worker/run/attempt
and admission lease. The token goes into the run's private environment, not the reusable
worker or a service-wide credential.

Optional `defaults` uses the existing recurrence families (`once`, `daily`, `interval`, `cron`,
`rfc5545`) and existing bounded fields: `misfire_grace_seconds`, `catch_up_policy` and
`max_catch_up_occurrences`. Defaults apply only when the worker omits that field. An explicit
zero grace or `skip` remains unchanged. With no supplied defaults, existing runtime defaults
remain unchanged. `schedule_list` returns these policies with the authorized target choices.
The client chooses its reminder/catch-up policy; xPerfect does not infer application meaning.

The seven native tools create, list, get, update (including pause/resume), retire, run now and inspect
history through the same schedule service as owner API/MCP/UI. Each call rechecks the live attempt
and current stored grant. Mutations share the existing lifecycle fence with Stop/Close/retry.
`schedule_list` exposes the current authorized target choices and optional client-supplied labels
alongside the current schedules, so the model can select opaque references without guessing.
Labels describe choices; they grant no authority. Webhook targets must match a supplied
destination/request-reference pair exactly; a worker cannot
replace the body or bootstrap authority. An explicit `allow_worker_target: true` additionally permits
scheduling that same worker, with its existing authorized runtime and context. It grants no access
to another worker. `manage_destination_ids` lets later turns list and control prior schedules for
those same-owner destinations without receiving permission to create arbitrary requests. Creation
and retargeting still require an exact supplied destination/request pair. Task-instruction edits use
the definition's signed instruction; application request references remain unchanged.

Terminal attempts, retry generations, released/expired leases and closed workers revoke native
access. Removing the stored grant also denies later calls. The small Fast Direct Queen interface
stays unchanged: it hands action work to a configured native worker. The clock, destination store
and owner controls remain usable without any application or AI provider account.

### Destination delivery limits and configuration recovery

Owner destination configuration may set `max_body_bytes` between 1 KiB and 256 KiB; its
public default remains 256 KiB. API and MCP use the same typed field. Create and edit reject
a schedule whose encoded due body cannot fit the configured receiver limit, before a clock
occurrence is stored. The check uses the same due-envelope builder, actual principal/task/data,
and the largest bounded schedule IDs, version and UTC timestamps. Raw mode checks its exact
configured request data instead. Limits count UTF-8 bytes, not characters.

Repeating a destination create key with matching current configuration recovers the same
public identity. If current configuration differs, HTTP 409 reports only the destination ID
and changed field names. Owner-scoped public metadata includes `url_sha256` (SHA256 of the exact configured URL) so an authorized client can detect URL drift without a URL readback. It never reads back the URL, headers or signing key. An authorized
client can rotate a secret through the existing PATCH. URLs remain immutable; a changed URL
uses a new destination and the existing owner-scoped schedule list/filter plus target PATCH.
`expected_version` on a schedule PATCH provides compare-and-swap protection. The optional
`destination_id` list filter allows a client to drain bounded batches without new registry or
migration endpoints. Past occurrence snapshots and receipts remain with their original versions.

### Existing worker continuations

The existing trusted account Work Action request may carry `runtimeBundle.schedule_access`
for queue, message, steer, retry or resume. This narrow field cannot change runtime identity,
environment, credentials, prompts or another worker. It uses the existing authenticated
principal, action reservation, exact request digest and receipt. Every supplied destination
and management scope must belong to that principal.

The current canonical grant replaces the previous grant as a whole; an explicit empty grant
removes stale request references. The worker's original authorized context, files, tools and
harness configuration stay intact. Native schedule calls recheck the stored grant and exact
live attempt through the existing MCP authorization. No additional message, handoff brief,
HTTP update step, queue or workflow is introduced.

### Shared schedule API authentication

An application courier that supplies the existing signed owner assertion to destination or
recurring-schedule routes uses the same bearer, signature, expiry, owner and mutation-nonce
checks as account work controls. This includes target create/list/read/edit/run-now/history,
destination configuration and worker-specific recurring schedules. A supplied invalid assertion
fails; it cannot fall back to a local operator identity. Browser/standalone clients without that
assertion keep their existing authentication. Native schedule MCP keeps its separate exact-attempt
token; an application assertion does not replace native authority.
