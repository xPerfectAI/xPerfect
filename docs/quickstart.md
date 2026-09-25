# xPerfect quickstart

## Start locally

Requires macOS or Linux, Python 3.11 or newer, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and internet for the first dependency install. Install the native CLI for your chosen provider.
From the repository:

```sh
./xperfect start
```

The first start asks you to choose an unlock password; press Enter to have one made, and save it
in your password manager. Open the printed URL, normally `http://127.0.0.1:8780`, and unlock once;
that browser stays unlocked for 30 days. Your saved work lives outside the
checkout. A running app still needs a connected AI account before it can do work.

This setup is for one trusted local OS user. It binds to loopback. Host workers have that user's
OS permissions; this is not a multi-user isolation boundary. Local storage is unlimited by default.

## Get one result

1. Open **Connections**, select your provider, and follow an offered sign-in method. For Grok,
   also choose its exact model once: `grok models`, then `./xperfect restart --model grok-build=<model>`.
2. In **Run project**, choose the intended account, enter a small goal, and add any needed files.
3. Watch the task, open its output, and read it. Reload to check that it remains available.

Try: “Write a short note explaining rain and save it as a text file.”

Subscription sign-in and API keys are separate routes. Available methods depend on provider,
platform and deployment configuration. A required account must not fall back to another account.
Account readiness does not prove access to every model or available credit.
See [account setup](host-setup.md) for native CLI sign-in and state details.

Files and saved work stay on your host. Connected AI providers can receive task content and
tool results needed for their requests. This is not a promise of offline or local-only AI.

For several goals, see [parallel work and its limits](capability-matrix.md#working-on-several-goals).

To repeat work, open **Schedules**, choose a saved workspace or a one-off run, enter the next task,
and choose when it repeats. Scheduling a one-off run saves its workspace automatically.

## Connect an MCP client

For local stdio, use the absolute path to `xperfect` as the command and `["mcp"]` as arguments.
Start xPerfect first. For custom state, append `--state-dir` and its path to those arguments.
Credentials load privately; do not paste them into prompts or command-line arguments.

Default HTTP MCP is `http://127.0.0.1:8767/mcp`. It needs its dedicated private bearer token.
The API uses port 8766 and a separate credential. Never put tokens in URLs, screenshots or Git.

## Continue or recover work

Use the selected worker's controls to pause, resume, interrupt or terminate where supported.
Inspect status and events before retrying. Open a completed result from Files or its artifact
route; viewing it does not need another run.

```sh
./xperfect doctor
./xperfect stop
./xperfect start
./xperfect restart
```

Use the same `--state-dir` if you chose one. Stop retains saved files, accounts and history.
Interrupt active work from Watch before planned shutdown. Interrupted work does not always resume
automatically.

| Symptom | Check |
| --- | --- |
| Service is not ready | Run `doctor` and inspect the named private log. |
| Worker is not ready | Read provider readiness and account setup in Connections. |
| A run stopped with the provider's own message | Follow it, for example a usage limit. Connections shows it under that account until a run completes. |
| Result is missing | Inspect worker status, events and Files before retrying. |
| Work was interrupted | Continue the same worker when its authority and provider remain valid. |
| MCP does not connect | Check transport, dedicated token, port and client configuration. |

Keep goals and constraints in the task. Advanced tool and context setup is in the
[technical guide](03_Bootstrap_Auth_and_Identity_Projection.md).
See [capabilities](capability-matrix.md) and [deployment and recovery](deployment.md).
