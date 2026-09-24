# xPerfect

Give an AI a goal and files. Watch its work, open the result, and return to it later.

Use a supported AI account and its tools while xPerfect keeps your work and results together.

**Choose how to start**
- **On your computer:** `./xperfect start` gives one person a private web app. See below and the
  [quickstart](docs/quickstart.md).
- **On a server for several people:** people sign in with your identity provider, and each gets a
  5 GB file quota. See [hosted setup](docs/deployment-hosted.md).
- **From another AI app or your own code:** connect over MCP or the API. See the
  [developer walkthrough](docs/developer.md).

## Start locally

Requires macOS or Linux, Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
From the repository:

```sh
./xperfect start
```

The first start asks you to choose an unlock password (press Enter to have one made; save it in your password manager).

1. Open the printed URL, normally **http://127.0.0.1:8780**, unlock once, and connect your AI account in **Connections**. That browser stays unlocked for 30 days.
2. In **Run project**, enter a small goal and add any needed files.
3. Watch the work, open the result, and check it.

Try: “Write a short note explaining rain and save it as a text file.”

```sh
./xperfect doctor
./xperfect stop
./xperfect restart
```

Service readiness is not provider readiness. The local launcher binds to loopback for one trusted
OS user; host workers have that user's permissions. It is not a public multi-user deployment.

[Quickstart](docs/quickstart.md) · [Account and state setup](docs/host-setup.md) ·
[Deployment and recovery](docs/deployment.md)

## Work together or separately

Each AI worker can have a separate place to work. Where shared execution is supported, workers
can use shared files or separate working folders. Their sign-ins and sessions stay separate.

![Workspace modes and file placement](docs/assets/workspaces.png)

[Editable SVG](docs/assets/workspaces.svg)

Ten goals do not guarantee ten simultaneous workers. Ready accounts, machine resources and
configured limits decide what can start; other work may wait or report a blocker. Sharing files
does not automatically share chat history, tools or permissions. See [working on several goals](docs/capability-matrix.md#working-on-several-goals).

To let workers in your account find or message one another, open **Work together** on a
workspace card. Discovery and messages are separate choices; you approve the exact current
workers and can revoke either message direction later. On hosted servers without a secure
worker connection, the controls explain why native worker-to-worker tools are unavailable.

Work is stored on the host you run. Connected AI providers can receive task content and tool
results needed for their requests; local storage does not mean local-only AI processing.

## Use and extend

- **Files:** keep original input bytes, inspect outputs, and download results. A filename alone is not an uploaded file.
- **Controls:** Watch, steer, interrupt or continue the exact worker. Read its recorded state before retrying.
- **AI providers:** Codex CLI, Claude Code, OpenClaw and the Grok adapter use their own supported routes. Grok needs one exact model: `grok models`, then `./xperfect restart --model grok-build=<model>`.
- **MCP:** connect over stdio or authenticated streamable HTTP; compatibility SSE remains available.
- **API:** projects, workers, runs, schedules, events, files and lifecycle controls use the existing typed runtime.
- **Build on it:** the [developer walkthrough](docs/developer.md) shows how to connect, discover tools, start, check and steer work, and add a harness.
- **Extensions:** add authorized tools, skills and context. See the [technical setup](docs/03_Bootstrap_Auth_and_Identity_Projection.md).

Missing authentication, an unsupported route and exhausted capacity are different states. Use the
reported next action; do not substitute an account or call incomplete work finished. Provider routes,
desktop tools and shared execution are configuration-dependent.

## Documentation

- [Quickstart](docs/quickstart.md): first task and opened result
- [Deployment](docs/deployment.md): local versus hosted setup, state and upgrades
- [Capability guide](docs/capability-matrix.md): execution, accounts, files and storage boundaries
- [Workshop](docs/workshop.md): build and inspect a result from synthetic input
- [Developer walkthrough](docs/developer.md): MCP, API and worker harnesses
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

Technical contracts:

1. [Vision and terminology](docs/01_Vision_Requirements_and_Terminology.md)
2. [Architecture and components](docs/02_Architecture_and_Components.md)
3. [Bootstrap, auth and identity](docs/03_Bootstrap_Auth_and_Identity_Projection.md)
4. [MCP publication and clients](docs/04_MCP_Publication_and_Client_Compatibility.md)
5. [QA playbook](docs/05_QA_Quick_Power_Playbook.md)
6. [References](docs/06_References.md)
7. [Operator UI](docs/07_Minimal_Unified_Operator_UI.md)
8. [Repository and publication boundaries](docs/08_Repository_Structure_and_Publication_Boundaries.md)
See [CHANGELOG](CHANGELOG.md) and the [runtime](runtime_phase1/README.md). Existing Python package names, `WPR_`/`GLASSHIVE_` configuration
and MCP/skill IDs remain compatibility interfaces; the product name is xPerfect.

## Creator

xPerfect is created by [Adrien Beyk](https://www.linkedin.com/in/adrienbeyk/).

[Instagram](https://www.instagram.com/adrienbeyk/)

## License

xPerfect is licensed under the [Apache License 2.0](LICENSE).
Third-party components retain their own licenses and notices. See [NOTICE](NOTICE).
