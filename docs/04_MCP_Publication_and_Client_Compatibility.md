# xPerfect: MCP Publication and Client Compatibility

Existing protocol, package, skill, and configuration names remain compatible.

For a runnable walkthrough (connect, list tools, start, check and steer work), see the
[developer walkthrough](developer.md).

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

## Client Strategy

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

Callbacks are optional. Viventium can use signed callbacks for durable completion delivery, but
standalone/plain LibreChat deployments must still work without them by using:

- `workspace_status` for non-blocking follow-up checks
- `workspace_wait` for explicit blocking waits
- `workspace_artifacts` and `workspace_artifact_download` for generated files, default signed
  downloads, and preview/open links
- `workspace_preferences_get` and `workspace_preferences_set` for per-user worker and effort
  defaults
- the returned View / Steer URL for operator visibility and takeover

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
the View / Steer URL promptly, and use status/wait/artifact tools for follow-up and delivery. It
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

### Hosted user connection

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

`/v1/native/coordinator/` is a narrow native MCP endpoint using the existing peer run/attempt binder.
It exposes `coordinator_accept_goals`, `coordinator_dispatch`, `coordinator_handle`,
`coordinator_status`, `coordinator_result`, and `coordinator_control`. A peer token alone is not
coordinator authority. Tool descriptions are authored in the packaged
`workers_projects_runtime/prompts/coordinator-tools.json`, beside `coordinator.md`. Source version,
prompt hash and tool-description hash are exposed in the conversation manifest for Workbench.

Native worker profile support does not imply conversation-provider support. Unsupported exact
model/profile/effort selections remain unavailable; no provider alias or fallback is invented.
