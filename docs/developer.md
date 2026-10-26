# Build on xPerfect: MCP, API and worker harnesses

This page shows a developer how to connect, find the tools, start, check and steer work, and add a
worker harness. Everything here uses the existing MCP tools and runtime API.

| Surface | Where | Authentication |
| --- | --- | --- |
| **MCP** (recommended) | Hosted: your `mcp_public_url`, for example `https://xperfect.example.com:8443/mcp`. Local: `./xperfect mcp` (stdio) or `http://127.0.0.1:8767/mcp` | Hosted: an OAuth access token from your identity provider, with scope `glasshive:access` by default. Local HTTP: the `mcp_token` from `secrets.json`. stdio runs as you. |
| **Runtime HTTP API** | Local only: `http://127.0.0.1:8766` while `./xperfect start` runs. Hosted packages do not publish it. | `Authorization: Bearer <api_token>` from `secrets.json` |

Local keys live in `<state>/secrets.json`. The state directory defaults to
`~/.local/state/xperfect`.

## 1. Connect a client

For a local `./xperfect start` instance:

1. Sign in to the web app and open **Connections → Use xPerfect from another AI app**.
2. Keep **Automatic**, copy its instruction, and paste it into the Codex or Claude Code client
   you want to use. It follows only its own section and reuses an existing matching setup.
3. Restart that client if prompted. The instruction ends with one `workspace_list` call to
   check this exact xPerfect instance.

The local launcher supplies its own **stdio** command and state directory. The AI client starts
that existing `xperfect mcp` command as the same OS user; there is no local OAuth or browser sign-in
step and no token to paste into the prompt. Worker provider accounts are separate: connect one
before asking a worker to run AI work. **Manual** shows the exact native add command for each
supported client. Copy that command rather than guessing a server name, checkout path or state
location. It preserves unrelated client settings.

The underlying local command is:

```sh
/path/to/xPerfect/xperfect mcp --state-dir /path/to/private/state
```

Use the actual paths supplied by your local app. Starting with another state directory connects
a different instance. A stdio client runs locally; it is not a remote URL for a hosted chatbot.

For a hosted deployment, the same panel offers only AI clients the operator has completely
registered. Use its generated native HTTP/OAuth commands and complete the client's own browser
sign-in. If registration is unavailable, the panel explains that limit; do not substitute local
credentials or invent a callback. [MCP publication](04_MCP_Publication_and_Client_Compatibility.md#local-user-connection)
owns local setup; [hosted user connection](04_MCP_Publication_and_Client_Compatibility.md#hosted-user-connection)
owns the remote registration and scope rules.

To check the protocol by hand, use plain MCP over streamable HTTP with `curl`:

```sh
export XP_MCP=https://xperfect.example.com:8443/mcp   # local: http://127.0.0.1:8767/mcp
export XP_TOKEN='<access token>'                      # local: the mcp_token

h=$(mktemp)
curl -sS -D "$h" -o /dev/null "$XP_MCP" \
  -H "Authorization: Bearer $XP_TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
export XP_SESSION=$(grep -i '^mcp-session-id:' "$h" | cut -d' ' -f2 | tr -d '\r')

xp() {  # xp '<json-rpc body>' prints the JSON reply
  curl -sS "$XP_MCP" -H "Authorization: Bearer $XP_TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $XP_SESSION" -d "$1" \
    | sed -n 's/^data: //p;/^{/p' | tail -1
}
xp '{"jsonrpc":"2.0","method":"notifications/initialized"}'
```

Replies may arrive as `text/event-stream`. The `sed` keeps the JSON line.

## 2. Discover the tools

```sh
xp '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

The server returns the currently enabled tools; the count can change by release and configuration. Each has a JSON Schema listing its required arguments, and
read-only tools carry `readOnlyHint`. Good first calls are `projects_list`, `workspace_list`,
`worker_accounts_list` and `files_storage`.

To call a tool:

```sh
call() { xp "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"; }
call projects_list '{}'
```

Results arrive in `result.structuredContent`. A refusal sets `result.isError` and gives a plain
reason.

## 3. A useful first job: a Finance review

Create the work once, then use the returned IDs from any connected surface. API, MCP and the
web app operate on the same records; changing interfaces does not require another worker.
A worker is the reusable professional and workspace. A run is one accepted piece of its work.

This small example asks a Finance worker to calculate profit from synthetic revenue of
**USD 1,200** and costs of **USD 300**, then deliver `finance-review.md`. The expected result is
**USD 900 profit and a 75% profit margin**. A local native Claude Code run completed this
calculation, its file was downloaded through MCP, and the same worker retained the figures in a
follow-up. This proves that bounded path; it does not certify every provider or financial answer.
For a larger professional demo, use the [Finance performance/cash workshop](workshop.md).

Before running: start xPerfect, connect a supported AI account and wait for **Ready**. This example
uses `claude-code`; its native CLI and exact model must already be configured. Choose the account
returned by `worker_accounts_list` that belongs to you and supports that worker type. An external
MCP connection alone does not sign a worker into its AI provider.

### The same job through MCP

Ask your connected AI client to perform these calls in order. Replace placeholders with the exact
IDs returned by earlier calls. An MCP tool argument is JSON, not text to paste into the worker's
instructions. Choose this path **or** the HTTP example below; do not create the job twice.

| Step | MCP tool and arguments | Keep / inspect |
| --- | --- | --- |
| Create the project | `project_create {"title":"Finance review","goal":"Explain synthetic profit and margin","default_worker_profile":"claude-code"}` | `project_id` |
| Create its Finance worker | `worker_create {"project_id":"<project_id>","name":"Finance Director","role":"Explain financial performance from the supplied facts","profile":"claude-code","bootstrap_bundle_json":{"provider_account":{"policy":"personal_required","account_id":"<ready_account_id>"},"files":{"operating-facts.md":"Synthetic example. Currency: USD. Revenue: 1200. Costs: 300. Do not invent other facts."}}}` | `worker_id`; exact account and configured model |
| Give it the job | `worker_run {"worker_id":"<worker_id>","instruction":"Read operating-facts.md. Calculate profit and profit margin, show the source values and formula, and create finance-review.md with the result and one practical next step. Return a link to that file. Do not contact external services or send messages."}` | `run_id`; acceptance is not completion |
| Check the exact work | `run_get {"run_id":"<run_id>"}` and `workspace_status {"worker_id":"<worker_id>","run_id":"<run_id>"}` | State, result, real failures or needed input |
| Find its output | `workspace_artifacts {"worker_id":"<worker_id>"}` | The actual workspace-relative result path |
| Open or read the file | `workspace_artifact_download {"worker_id":"<worker_id>","path":"finance-review.md"}` | Open `signed_open_url`, or fetch the bytes from `signed_download_url` |
| Continue the same work | `worker_message {"worker_id":"<worker_id>","message":"Keep the original numbers. Explain the margin calculation for a reader without a finance background."}` | A follow-up receipt for this same worker, then its result |

If the first response is still running, check it later; use the bounded `workspace_wait` only when
you actually want to wait. Open the file and verify `900` and `75%` against the inputs. A queued
receipt, a filename, a terminal stream or a model saying “done” is not proof that the file exists.
The download links are short-lived and file-scoped. Request a fresh descriptor if one expires;
never publish those links as permanent public assets.

### Other work controls


| Step | Tool and arguments |
| --- | --- |
| Group the work | `project_create {"title": "...", "goal": "..."}` returns `project_id` |
| Add a worker | `worker_create {"project_id": "...", "name": "...", "role": "..."}`. Optional: `profile` (for example `codex-cli`). Returns `worker_id`. |
| Start work | `worker_run {"worker_id": "...", "instruction": "..."}` returns a run |
| Check | `run_get {"run_id"}`, `workspace_status {"worker_id"}`, `workspace_wait {"worker_id", "timeout_seconds"}`, `project_events {"project_id"}` |
| Results | `workspace_artifacts {"worker_id"}`, then `workspace_artifact_download {"worker_id", "path"}` |
| Guide or follow up | `worker_message {"worker_id", "message"}` keeps the same worker |
| Control | `worker_interrupt`, `worker_pause`, `worker_resume`, `worker_terminate` (each takes `worker_id`) |

**On your computer, a worker needs its tool installed.** `worker_create` refuses until that worker
type's CLI (for example `codex`) is installed, and names the missing tool. `./xperfect doctor` shows
which tools are installed.

**Starting and steering native work needs a connected AI account.** Until one is connected,
`worker_run` and `worker_message` refuse with "Work AI is not set up for this workspace … connect a
personal account in Connections". Everything else in the table works without one. Connect an
account in **Connections**, or over MCP:
1. `worker_account_connect` (`provider` `codex` or `claude`)
2. `worker_account_setup_start`, which returns the provider's own sign-in step
3. `worker_account_test`

See [Connect AI accounts](deployment-hosted.md#connect-ai-accounts).

**A reusable expert in one call.** `workspace_launch` can create and start a named workspace. When
the user asks to save and favorite it, set `favorite=true`; use `reuse_existing_workspace=true` to
find and resume an existing alias. Give it text files and native skills with
`bootstrap_bundle_json` (`{"files": {"path": "content"}}`). Claude Code uses
`.claude/skills/<skill>/SKILL.md`; Codex uses `.agents/skills/<skill>/SKILL.md`. Use
`provider_account_policy="personal_required"` for the user's own connected account.

A fresh one-off launch without a selected project, reuse, favorite or Library selection uses
the atomic delegate path, which requires a trusted source identity from an integrated host. Do
not fabricate that identity or change the user's choices to bypass the requirement. A plain
management client can use the explicit create/run steps above. Check `workspace_list` before
retrying an unclear mutation; a one-call interface alone does not guarantee replay safety.
Selected Library references and their actual-target review use the same
[Library contract](04_MCP_Publication_and_Client_Compatibility.md#private-library-through-http-and-mcp).

### Start with the same professional shown in the app

The five bundled professions are ordinary saved worker setups, available through the existing
MCP/API catalog. You do not need to recreate their instructions or Library material by hand.
Their current native Skill route is **Claude Code on the host**; use a Ready compatible account
and a deployment that offers that route.

| Step | MCP tool | Keep from its result |
| --- | --- | --- |
| Choose the profession | `workspace_starter_list` | The desired starter's `id`, `content_hash` and included abilities |
| Save that exact setup privately | `workspace_starter_import` with `starter_id`, the listed `content_hash`, a stable `idempotency_key` and the user's selected account | `template.template_id` |
| Create a fresh worker | `workspace_template_instantiate` with that `template_id`, a new stable `idempotency_key` and optional name | The created worker and any required Library review |
| Give it the real job | Use the normal file upload, actual-target abilities review and `worker_run` path above | The exact worker/run IDs for status, guidance and results |

Importing a starter does not start work or approve an ability. Instantiation creates a paused
worker; review the returned abilities for that actual worker before running it. Retry a lost
import/instantiate response with its original key rather than creating another worker.

For the full Finance example, attach the [company material, accounts and plan](workshop.md#prepare-the-example),
then use its supplied job. A connected native client can do the same work from an ordinary request:

> Show me the packaged Finance Director and its included abilities. Use my selected Ready Claude
> Code account on this host, create a new worker from that exact setup, and let me review the
> abilities it needs. Use the Finance example files I attach to prepare the performance and cash
> review. Return the report and editable calculations. Keep this worker so I can give it a follow-up.

The matching HTTP routes and exact-version rules are in the existing
[starting-template contract](04_MCP_Publication_and_Client_Compatibility.md#public-starting-templates-private-worker-ownership).
The smaller arithmetic example above checks the transport; this professional example exercises
onboarding, methods, traceable analysis and editable delivery.

### Manage the same saved experts and workspaces

Use `workspace_template_list` to find **Available**, **Archived** or **Removed** expert setups,
and `workspace_template_get` to inspect an exact version or its version history.
`workspace_template_manage` edits, copies, archives, removes or restores that setup. Edit adds a
version; it does not rewrite workers already created from it.

`workspace_remove` closes the exact worker before hiding its workspace card. `workspace_restore`
brings that same card back **Closed**. Files, results and history, including one-off work, remain
saved; this is recoverable removal, not a storage purge. Use the normal duplicate path for fresh
work. Read the returned catalog revision before a mutation and follow the tool’s current schema;
a stale write conflicts instead of overwriting a newer choice. The
[shared HTTP/MCP lifecycle contract](04_MCP_Publication_and_Client_Compatibility.md#catalog-crud-through-the-same-owner)
owns the exact fields, confirmations and authorization rules.

### Serve a reusable expert

Connect your product or AI client to the existing authenticated MCP endpoint. Use `workspace_list`
to select an expert, `worker_run` to give it work, and `worker_message` for guidance or a follow-up.
Use the returned run/worker context to check that exact result and retrieve its artifacts. Keep
the same workspace for continuity; copy it when you want separate files and history.

The optional [connection skill](../skills/connect-glasshive/SKILL.md) helps an AI use the existing
control plane. A worker's skills, files and tools belong inside its native workspace, not in a
new per-expert server or routing layer. Text files can use `bootstrap_bundle_json`; binary files
use the existing authenticated Files upload path. Never place provider credentials in either
bundle. Hosted applications use MCP; the internal runtime HTTP port is not a public API.

## 4. The same Finance job through HTTP (local)

Use this alternative on a local `./xperfect start` instance. Read the exact fields from each
response and put them in the next placeholder. These examples use Bash and `curl`; use your own
private API token. Never put an actual token in a public example, issue or URL.

```bash
export XP_API=http://127.0.0.1:8766
export XP_API_TOKEN='<api_token from your private state/secrets.json>'
auth=(-H "Authorization: Bearer $XP_API_TOKEN" -H 'Content-Type: application/json')

curl --fail-with-body -sS "${auth[@]}" "$XP_API/v1/me"
curl --fail-with-body -sS "${auth[@]}" "$XP_API/v1/provider-accounts"
```

Keep `user_id` from the first response and a compatible **ready** `account_id` from the second.
Create the project, then use its returned `project_id`:

```bash
curl --fail-with-body -sS "${auth[@]}" -X POST "$XP_API/v1/projects" \
  -d '{"owner_id":"<user_id>","title":"Finance review","goal":"Explain synthetic profit and margin","default_worker_profile":"claude-code"}'

curl --fail-with-body -sS "${auth[@]}" -X POST "$XP_API/v1/projects/<project_id>/workers" \
  -d '{"owner_id":"<user_id>","name":"Finance Director","role":"Explain financial performance from supplied facts","profile":"claude-code","workspace_kind":"named","start_synchronously":false,"bootstrap_bundle":{"provider_account":{"policy":"personal_required","account_id":"<ready_account_id>"},"files":{"operating-facts.md":"Synthetic example. Currency: USD. Revenue: 1200. Costs: 300. Do not invent other facts."}}}'
```

Keep the returned `worker_id`. The workspace is now saved; queue its first job:

```bash
export XP_WORKER='<worker_id>'
curl --fail-with-body -sS "${auth[@]}" -X POST "$XP_API/v1/workers/$XP_WORKER/assign" \
  -H 'Idempotency-Key: finance-first-review' \
  -d '{"instruction":"Read operating-facts.md. Calculate profit and profit margin, show the source values and formula, and create finance-review.md with the result and one practical next step. Return a link to that file. Do not contact external services or send messages.","effort":"high"}'
```

HTTP **202** returns the accepted `run_id`. It means the job was saved for execution. Read that
exact run until it completes or reports a real failure; do not create another worker to check it.

```bash
export XP_RUN='<run_id>'
curl --fail-with-body -sS "${auth[@]}" "$XP_API/v1/runs/$XP_RUN"
curl --fail-with-body -sS "${auth[@]}" "$XP_API/v1/workers/$XP_WORKER/events"
curl --fail-with-body -sS "${auth[@]}" "$XP_API/v1/workers/$XP_WORKER/artifacts"
```

After completion, find the real result path in the artifacts response. Read/download its bytes:

```bash
curl --fail-with-body -sS "${auth[@]}" --get \
  "$XP_API/v1/workers/$XP_WORKER/artifacts/download" \
  --data-urlencode 'path=finance-review.md' -o finance-review.md
cat finance-review.md
```

Verify the source values, formula, **USD 900** and **75%**. Open the same worker in the web app to
inspect or download that same result. For a follow-up in its existing context:

```bash
curl --fail-with-body -sS "${auth[@]}" -X POST "$XP_API/v1/workers/$XP_WORKER/message" \
  -d '{"message":"Keep the original numbers. Explain the margin calculation for a reader without a finance background."}'
```

Keep that follow-up's receipt and inspect its result. Use `/steer` when you intentionally want to
redirect unfinished work; it is not a status check. Native midstream/permission controls use the
reported exact run and attempt and remain subject to that harness's supported capabilities.

### Retry, recover and reuse

| Situation | Correct next step |
| --- | --- |
| The assignment response was lost | Read `GET /v1/workers/<worker_id>/assignments/by-idempotency/finance-first-review`. If retrying, use the same key and identical instruction, effort, bootstrap and continuation inputs. |
| A changed request returns `assignment_idempotency_conflict` / HTTP 409 | Read the saved assignment. The conflicting request must not change its context, resume a paused worker or start duplicate work. Use a new key only for deliberately new work. |
| MCP reports `isError`, or HTTP returns 401/403 | Read the reason. Repair the client sign-in, scope or account authority; do not substitute another user's account or grant. |
| A worker's CLI, model or account is unavailable | Follow the named setup/connection recovery, then retry the intended worker. An app health response alone does not establish AI readiness. |
| Controls report that startup is not ready | Read current worker/run status, then retry only when the reported state permits the action. Do not describe an unaccepted Pause as completed. |
| HTTP 429 reports capacity | Reuse appropriate existing work or free its resource as instructed; do not launch replacements in a loop. |
| The result link expired | List the same worker's artifacts and obtain a fresh descriptor. Do not rerun a completed job to recover a download. |
| Another job should use this expert's setup | Save/start an expert setup or use the existing duplicate control. A new worker is a separate identity; credentials, browser sign-ins and native chat history are not cloned. |
| You want to continue this job | Reuse its `worker_id`, message it and read its new run. Its earlier files remain available within the retention contract. |

MCP's `worker_run` does not expose the HTTP assignment's idempotency-key option. After an unclear
MCP response, inspect the existing worker/runs before issuing another mutation. Do not infer
exactly-once admission from the MCP tool's name. Optional schedule, Library and file operations
use their own documented request keys and revision checks.

### Schedule a worker or wake an app

Use `POST /v1/recurring-schedules` or MCP `schedule_create` for a worker or webhook target.
An app-only schedule needs no AI account. The existing [schedule guide](../runtime_phase1/README.md#generic-worker-and-webhook-schedules)
shows the **What / Where / When** interface, common uses, protected destination setup, request
examples, signatures, retries and delivery states. It also includes [Slack, n8n and Make recipes](../runtime_phase1/README.md#connect-slack-n8n-or-make).
Owner HTTP/MCP share that service; native workers receive only their current scoped scheduling grants.

## 5. Extend what workers can do

**Tools, skills and context** reach workers through the bootstrap bundle. See the
[bootstrap bundle contract](03_Bootstrap_Auth_and_Identity_Projection.md#bootstrap_bundle)
and [bootstrap, auth and identity](03_Bootstrap_Auth_and_Identity_Projection.md).

**Running the services yourself.** `./xperfect start` and the packaged install give workers a
route back to the runtime, which they use to read background beyond the inline limit. If you run
the services another way, set `GLASSHIVE_PEER_RUNTIME_BASE_URL` to a runtime address the workers
can reach. Plain HTTP is accepted only for this host (`127.0.0.1`, `localhost` or
`host.docker.internal`); anything else needs HTTPS. A runtime served on a local socket can give its
host workers that socket instead: `http+unix://` followed by the URL-encoded absolute socket path.
Without a route, Workspace settings say so, and a run that needs the rest of its background stops
before it starts, naming the cause.

**A new worker harness** (another native CLI) is a source contribution, not a plugin. Follow the
existing profiles. The Grok adapter is the most recent worked example; see
[its contract](02_Architecture_and_Components.md#native-grok-adapter-contract). In `runtime_phase1/src/workers_projects_runtime/`:

1. Register the profile in `profile_registry.py` `PROFILES`. Each profile has its name, label,
   runtime attribute, native transport and model variable.
2. Mirror the model variable in `deployment/linux/launch.py` `MODEL_ENVIRONMENTS`. A test keeps the
   two equal.
3. Write the runtime class on `BaseCliWorkerRuntime`:
   - implement `resolve_model`, `_build_command` and `_parse_output`;
   - optionally add hooks such as `preflight_worker_profile` and `_usage_from_output`;
   - add a host variant with `HostNativeCliMixin`.

   `grok_runtime.py` shows both.
4. Attach both in `ProfiledWorkerRuntime`, in `profile_runtime.py`.
5. Declare the provider's own sign-in in `provider_accounts.py`: `provider_setup_binary`,
   `provider_platform_support` and the setup/status `_commands`.
6. Add the profile to the places that list profiles by name, such as account providers,
   allowed-AI policy, MCP account tools, worker configuration and the UI. Searching the source for an
   existing profile name, for example `grok-build`, finds them all.
7. Ship the CLI in the worker image and add it to `./xperfect doctor`.

Keep the product rules:
- The model is exact and never defaulted or swapped.
- Readiness is typed.
- There is no silent account fallback.

Before opening a pull request, run the tests that pin profiles: `tests/test_linux_upgrade.py` (the
model table) and, in `runtime_phase1/tests/`, `test_grok_runtime.py`,
`test_reconciled_runtime_fixes.py` (unknown profiles), `test_control_plane.py` (platform support)
and `test_api.py` (`/v1/worker-profiles`). See [Contributing](../CONTRIBUTING.md).

## Build a matched Docker package

Use a clean public checkout on the target CPU. Keep provider credentials and runtime state outside
the build context. The native workstation recipe is owned by `DockerSandboxManager.prepare_image`;
it pins the operating-system base, clients and dependencies. Its image is only the **base**.
Always build `runtime_phase1/containers/Dockerfile.shared` next: it adds immutable member IDs,
the credential entry point and the storage guard required by the packaged runtime.

```sh
docker build --build-arg XPERFECT_BASE_IMAGE=<reviewed-workstation-image> \
  -f runtime_phase1/containers/Dockerfile.shared -t xperfect-native:local runtime_phase1
docker build --build-arg XPERFECT_NATIVE_IMAGE=xperfect-native:local \
  -f deployment/linux/Dockerfile.service -t xperfect-service:local .
```

Do not remove or skip the build-time storage-guard check to cross-build. Its thread-synchronized
seccomp filter needs the target's native Linux execution. The optional **Build native amd64
package** workflow runs on an actual x86 runner, verifies the staged base's digest, builds both
layers from that commit and returns immutable code-only archives to a draft release. It starts
only on an explicit `codex/build-*` maintainer branch or manual dispatch. It never imports a
developer's account, application state or environment.

The existing image publisher accepts OCI layouts and optional digest-checked archive parts; it
preserves exact manifests. Saved releases use one explicit manifest pair per CPU, so operators
do not assemble an index. **Public package start** runs on native Arm and x86 runners. It checks
anonymous download, exact image/platform identity, protected UI, authenticated MCP, native
binaries, Chromium/file execution, restart and the opt-in bridge policy. This credential-free
check does not claim authenticated inference on every provider. Keep the saved release pin on
its accepted platforms until those checks pass. See [deployment](deployment.md#image-architecture).

[Quickstart](quickstart.md) · [Deployment](deployment.md) · [Hosted](deployment-hosted.md) ·
[Capabilities](capability-matrix.md)
