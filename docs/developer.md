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

[Quickstart](quickstart.md) · [Deployment](deployment.md) · [Hosted](deployment-hosted.md) ·
[Capabilities](capability-matrix.md)
