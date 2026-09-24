# Run xPerfect on your computer

Requires macOS or Linux, Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Internet access is needed for the first dependency install. `uv` installs the locked Python 3.12
environments. On a fresh Linux server, install uv with its standalone installer; the system Python is
enough, because uv creates the environments itself.

From the repository:

```sh
./xperfect start
```

The first start asks you to choose an unlock password (see **Unlock password** below). Open
**http://127.0.0.1:8780** and unlock once. The command prints the URL only after the API, UI and MCP
report the new instance as ready. The services continue after the command exits.
Runtime readiness does not mean a provider is connected or a model has completed work.

Install your provider's native CLI, then open **Connections → Connect another account**:

- [Codex](https://developers.openai.com/codex/cli/): connect a subscription through native device sign-in, or choose **API key**.
- [Claude Code](https://code.claude.com/docs/en/setup): choose **API key**. For an existing Claude subscription, choose **Use existing Claude sign-in** in Connections. xPerfect verifies the current OS sign-in and connects it as a distinct selected account. If setup is needed, open **Sign-in help**, complete the native install/sign-in, then choose **Check again**. Named Claude subscription accounts are unavailable on macOS because their isolation cannot be guaranteed.
- [Grok](https://github.com/xai-org/grok-build): connect a subscription through native device sign-in, or choose **API key**. Grok also needs one exact model: run `grok models` to list them, then `./xperfect restart --model grok-build=<model>`. xPerfect never picks a Grok model for you; `./xperfect doctor` shows what each installed harness will use.

Tested versions are the ones the workspace image pins: Codex `0.155.0-alpha.9.2` and Claude Code
`2.1.280` from npm (with Node.js 22), and Grok `1.0.34` from xAI's CLI download. A per-user install
needs no root, for example `npm install -g --prefix ~/.local @openai/codex@0.155.0-alpha.9.2`, with
`~/.local/bin` on PATH. After installing, `./xperfect doctor` should show the CLI as present. Its
account setup changes from "CLI required" to what that provider's sign-in needs next.

The installed CLI must be on PATH, or explicitly configured in the private `config.json` `env`
object (`WPR_CODEX_CLI_PATH`, `WPR_CLAUDE_CODE_PATH`, or `WPR_GROK_BIN`). Restart after changing
that configuration. Missing CLIs stay unavailable; xPerfect does not substitute a different provider.

API keys are entered in a password field, stored in the private account home with owner-only
permissions, and given only to the selected provider's native process. This is local file protection,
not encryption. xPerfect checks the provider's fixed HTTPS models endpoint and reports rejected
keys, permission failures, rate limits, or network failure. An accepted key does not prove access to
a particular model or available credit. Codex uses its native stdin login and private file credential
store; Claude must confirm that it selected the API-key authentication method.

Select the connected account in **Run project**, enter your goal, and start. Verify a real completed
result before relying on that route. **Verify** checks an existing Claude sign-in without copying credentials or signing out the native app. **Check connection** tests a stored key again; **Reconnect**
replaces it; **Remove** deletes only xPerfect's local account credentials. To revoke an API key
at its provider, use the provider's own console. A required named account never falls back to your
current OS account. The optional preferred-account policy can use its explicitly allowed fallback.

These API-key accounts use the local single-user host route, or a packaged Linux multi-user route
with `per_worker_container` isolation and an owner-scoped private account home. The contained
worker reads the selected key from its account mount at native start; the key is not saved in the
worker route or Docker command arguments. Other multi-user substrates remain unavailable.

## Connect an MCP client

For a local client that supports stdio, use the absolute path to `xperfect` as its command and
`["mcp"]` as its arguments. Start xPerfect first. With a custom state directory, also pass
`--state-dir` and that directory. This loads the private service credentials without placing them
in the client's configuration or command line.

The HTTP MCP endpoint is **http://127.0.0.1:8767/mcp**. HTTP clients must use the private
`mcp_token` in `secrets.json` as a bearer token. Do not publish the token or include it in a URL.
The API uses port **8766** and its separate `api_token`.

## State and settings

State lives in `~/.local/state/xperfect`, with directory mode 0700 and secrets mode 0600:

- `config.json`: ports, local owner, chosen exact models (`models`, set with `--model`) and optional explicit runtime environment settings.
- `secrets.json`: generated API, MCP and signed-link keys; retained across restarts.
- `data/`: database, provider accounts, managed files and link state.
- `workspaces/`: local work files.
- `venvs/`: separate locked API/MCP and UI dependencies.
- `logs/`: private service and supervisor logs. Do not publish these files.

Local storage is unlimited unless a separate supported policy is configured. This local launcher
is for one trusted OS user. It binds only to loopback and does not provide multi-user hosted
isolation. Native host workers can act with your OS account's permissions.

To use another state directory and avoid occupied ports on first setup:

```sh
./xperfect start --state-dir "$HOME/.local/state/xperfect-test" --api-port 18766 --ui-port 18780 --mcp-port 18767
```

Keep state outside the Git checkout. `XPERFECT_STATE_DIR` can also select it. The initial ports are
saved; later changes go in `config.json` while stopped. Port flags do not overwrite existing
configuration. Package names, module names and `WPR_`/`GLASSHIVE_` settings remain compatibility IDs.
Set optional runtime settings in the `env` object; this launcher always owns its local binding,
state paths, authentication mode and service credentials. It ignores ambient parent-app runtime
configuration. Provider API keys inherited from your shell are not persisted by the launcher.

## Check, stop and recover

```sh
./xperfect doctor
./xperfect stop
./xperfect start
./xperfect restart
```

Use the same `--state-dir` for each command if you changed it. Doctor checks all three live
instance IDs and reports native CLI presence separately from provider authentication. It also shows
each saved exact model, including one whose CLI is not installed yet. It exits nonzero when stopped
or unhealthy. Stop requests graceful service shutdown; saved files, accounts
and project history remain. Interrupt running work from Watch before a planned shutdown; work
recovery follows the runtime's recorded run state and must not be assumed to resume every task.

An occupied port fails setup without stopping its owner, and the message names the service and
port. Change ports while stopped. If one
service exits, the supervisor stops its other services and records the error. Inspect the named
private log, fix the prerequisite, then start again. Repeating start on a healthy instance is safe.
Dependency installation uses committed lockfiles; it does not update them.

Before upgrading, stop, back up the private state directory, update the source, then start.
Starting from another checkout with the same state is refused while the old checkout runs.
Do not restore an older application against a migrated database; roll back the matching source
and stopped-state backup together. This launcher has been designed for local host operation;
it is not a cloud deployment or a hosted isolation certificate.

## Unlock password

xPerfect keeps workspace changes such as sharing and permissions behind one unlock password for this
computer's owner.

- **First start:** `./xperfect start` asks for a password of at least 24 characters (12 different),
  or makes one when you press Enter and shows it once. To set it without a prompt, pipe it in:
  `./xperfect start --unlock-password-stdin`. The password is never placed in arguments, environment
  variables or logs.
- **Browser:** unlock once per browser; it stays unlocked for 30 days, including across restarts.
- **Later starts** reuse the same owner and sessions; they never ask again or reset the password.
- If start reports that the unlock setup "is not usable and was left unchanged", nothing was reset.
  Re-enable or reset the owner with `python -m glass_drive_ui.auth_admin` using the UI environment,
  then start again.
