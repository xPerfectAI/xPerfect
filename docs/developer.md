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

The easiest way is to let the web UI do it. Signed in, open **Use xPerfect from another AI app**. It
shows the exact add and sign-in steps for each client the deployment supports, such as Codex or
Claude Code. [MCP publication](04_MCP_Publication_and_Client_Compatibility.md#hosted-user-connection)
explains the client contract.

For a local client over stdio, for example:

```sh
claude mcp add xperfect -- /path/to/xPerfect/xperfect mcp
```

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

The server returns about 100 tools. Each has a JSON Schema listing its required arguments, and
read-only tools carry `readOnlyHint`. Good first calls are `projects_list`, `workspace_list`,
`worker_accounts_list` and `files_storage`.

To call a tool:

```sh
call() { xp "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"; }
call projects_list '{}'
```

Results arrive in `result.structuredContent`. A refusal sets `result.isError` and gives a plain
reason.

## 3. Start, check and steer work

| Step | Tool and arguments |
| --- | --- |
| Group the work | `project_create {"title": "...", "goal": "..."}` returns `project_id` |
| Add a worker | `worker_create {"project_id": "...", "name": "...", "role": "..."}`. Optional: `profile` (for example `codex-cli`) and `resource_class`. Returns `worker_id`. |
| Start work | `worker_run {"worker_id": "...", "instruction": "..."}` returns a run |
| Check | `run_get {"run_id"}`, `workspace_status {"worker_id"}`, `workspace_wait {"worker_id", "timeout_seconds"}`, `project_events {"project_id"}` |
| Results | `workspace_artifacts {"worker_id"}`, then `workspace_artifact_download {"worker_id", "path"}` |
| Steer | `worker_message {"worker_id", "message"}` adds guidance |
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

`workspace_launch` and `worker_delegate_once` do the same work in one call, but only for
integrated clients that send a trusted source identity. A plain MCP client gets "A trusted source
event identity is required" and should use the steps above.

## 4. Runtime HTTP API (local)

The same flow over HTTP, on a local `./xperfect start`:

```sh
export XP_API=http://127.0.0.1:8766
export XP_API_TOKEN='<api_token from secrets.json>'
auth=(-H "Authorization: Bearer $XP_API_TOKEN" -H 'Content-Type: application/json')

curl -sS "$XP_API/health"                             # no token needed
curl -sS "${auth[@]}" "$XP_API/v1/me"                 # your user_id is the owner_id below
curl -sS "${auth[@]}" "$XP_API/v1/worker-profiles"    # the supported worker types
curl -sS "${auth[@]}" -X POST "$XP_API/v1/projects" \
  -d '{"owner_id":"<user_id>","title":"Release notes","goal":"Write release notes"}'
curl -sS "${auth[@]}" -X POST "$XP_API/v1/projects/<project_id>/workers" \
  -d '{"owner_id":"<user_id>","name":"writer","role":"Write the notes","profile":"codex-cli"}'
curl -sS "${auth[@]}" -X POST "$XP_API/v1/workers/<worker_id>/assign" -d '{"instruction":"Write release-notes.md"}'
curl -sS "${auth[@]}" "$XP_API/v1/runs/<run_id>"
curl -sS "${auth[@]}" -X POST "$XP_API/v1/workers/<worker_id>/steer" -d '{"message":"Keep it under 150 words"}'
curl -sS "${auth[@]}" "$XP_API/v1/workers/<worker_id>/events"
curl -sS "${auth[@]}" "$XP_API/v1/workers/<worker_id>/artifacts"
curl -sS "${auth[@]}" -X POST "$XP_API/v1/workers/<worker_id>/interrupt"
```

Creating a worker returns 409, naming the missing tool, until that worker type's CLI is installed.
Other routes also exist: `message`, `pause`, `resume` and `terminate` on a worker, `/v1/storage` and
`/v1/file-uploads` for files, and schedules. A request without the token gets 401.

## 5. Extend what workers can do

**Tools, skills and context** reach workers through the bootstrap bundle. See the
[bootstrap bundle contract](03_Bootstrap_Auth_and_Identity_Projection.md#bootstrap_bundle)
and [bootstrap, auth and identity](03_Bootstrap_Auth_and_Identity_Projection.md).

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

[Quickstart](quickstart.md) · [Deployment](deployment.md) · [Hosted](deployment-hosted.md) ·
[Capabilities](capability-matrix.md)
