# Compare the AI worker landscape

Reviewed 2026-10-26. Generated from [comparison.json](comparison.json).

Build an AI team around your expertise. xPerfect brings reusable specialists, your company’s knowledge and tools, clear jobs, hands-on control and usable results into one workplace. Compare it with the frameworks, control planes and runtimes you can build on.

## What this means in practice

### Finance partner

Reuse a finance expert with reference files; request a board brief and an editable forecast with stated assumptions.

**Keep the expertise, working calculation and resulting brief together.**

### Research specialist

Give a research expert a question, source material and evidence criteria; review the cited findings and open the result files.

**A concrete deliverable with supporting evidence that can be checked.**

### Your own expert

Save a useful worker setup with instructions, knowledge and tools; start a new worker from that version for the next job.

**Reuse a person’s method without recreating the setup each time.**

## Product comparison

Cells describe the actual approach. Read the linked project sources for implementation details.

### Worker control planes

Interfaces and services for operating agents, teams or visual workflows.

| Product | Reuse expertise | Set the job & outcome | Work with specialists | Watch and steer | Choose where it runs | Keep useful results | Connect and repeat |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [xPerfect](https://github.com/xPerfectAI/xPerfect/blob/main/README.md) | Saved experts + Library | Job + success criteria | Parallel + shared workers | Watch + steer | Your computer or server | Reports + saved files | API, MCP + scheduled webhooks |
| [Scion](https://github.com/GoogleCloudPlatform/scion) | Templates + Skill Bank | Tasks + role instructions | Parallel agents | Dashboard + terminal | Local or hosted brokers | Agent workspaces | API + schedules |
| [Omnigent](https://github.com/omnigent-ai/omnigent) | Specialist configuration files | Session task + policies | Mixed-harness specialists | Shared live sessions | Local or cloud sandboxes | Synchronized files | Functions + MCP |
| [Paperclip](https://github.com/PaperclipAI/paperclip) | Agents + company templates | Goals → projects → tasks | Org chart + delegation | Dashboard + approvals | Self-hosted server | Tasks + documents | Adapters + gateways |
| [Agno + AgentOS](https://docs.agno.com/) | Agents + knowledge + tools | Agent or workflow request | Teams + workflows | Control Plane + streams | Self-hosted AgentOS | Sessions + responses | API + MCP + schedules |
| [OpenHands Agent Canvas + SDK](https://docs.openhands.dev/overview/introduction) | Agent configurations | Conversation task | Multiple conversations | Canvas + terminals | Local or cloud runtime | Working files | SDK + automation services |
| [kagent + agentgateway](https://kagent.dev/docs/kagent/1.x/about/core-concepts/) | Templates + skills | Session + template | Subagent tool bindings | Sessions + snapshots | Your Kubernetes cluster | Snapshot-backed workspaces | MCP + agentgateway |

### Visual workflow builders

Build connected workflows and agent tools through a visual editor.

| Product | Reuse expertise | Set the job & outcome | Work with specialists | Watch and steer | Choose where it runs | Keep useful results | Connect and repeat |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [xPerfect](https://github.com/xPerfectAI/xPerfect/blob/main/README.md) | Saved experts + Library | Job + success criteria | Parallel + shared workers | Watch + steer | Your computer or server | Reports + saved files | API, MCP + scheduled webhooks |
| [Dify](https://docs.dify.ai/en/home) | Apps + knowledge | Workflow inputs + outputs | Workflow orchestration | Visual workflow + runs | Cloud or self-hosted | Application outputs | APIs + triggers |
| [Flowise](https://docs.flowiseai.com/) | Assistants + RAG | Visual flow outputs | Agentflow branches | Human review + resume | Cloud or self-hosted | Flow responses + state | API + SDK + MCP |
| [n8n](https://docs.n8n.io/build/integrate-ai.md) | Workflow templates | Trigger → workflow result | Workflow orchestration | Workflow executions | Cloud or self-hosted | Node data + app actions | App nodes + schedules |

### Code frameworks

Reusable agent behavior and workflows within your application.

| Product | Reuse expertise | Set the job & outcome | Work with specialists | Watch and steer | Choose where it runs | Keep useful results | Connect and repeat |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [xPerfect](https://github.com/xPerfectAI/xPerfect/blob/main/README.md) | Saved experts + Library | Job + success criteria | Parallel + shared workers | Watch + steer | Your computer or server | Reports + saved files | API, MCP + scheduled webhooks |
| [Pydantic AI + Harness](https://pydantic.dev/docs/ai/overview/) | Reusable agent blueprints | Instructions + typed output | Subagents + workflows | Persistence + durable engines | Your application runtime | Workspaces + typed results | Tools + MCP + interfaces |
| [LangGraph + LangSmith Deployment](https://docs.langchain.com/oss/python/langgraph/overview) | Graphs + assistants | Graph state + outputs | Multi-agent graphs | Checkpoints + interrupts | Cloud, hybrid or self-hosted | State + run outputs | APIs + MCP + cron |
| [Deep Agents](https://docs.langchain.com/oss/javascript/deepagents/overview) | Skills + instructions | Plans + tracked tasks | Specialist subagents | Interrupts + memory | Your chosen backend | Pluggable filesystems | Tools + model adapters |
| [CrewAI + AMP](https://docs.crewai.com/) | Roles + capabilities | Tasks + expected outputs | Crews + flows | Persisted flows + AMP | Framework or platform | Task and flow results | Tools + app triggers |
| [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/) | Agents + HarnessAgent | Workflow + result contract | Agent workflows | Checkpoints + human input | Self-host or Foundry | Workflow results + files | Providers + tool interfaces |
| [Google ADK](https://adk.dev/) | Agents + skills | Agent or workflow input | Sequential + parallel agents | Sessions + resume | Your app or Google runtime | Artifacts + outputs | API + MCP + A2A |
| [Mastra](https://mastra.ai/docs) | Agents + skills + memory | Goals + evaluation | Subagents + background work | Controller + signals | Your app or platform | Workspace + results | Tools + schedules + events |
| [Letta](https://docs.letta.com/) | Memory + skills | Agent task | Subagent patterns | Persistent sessions | Self-host or service | Agent responses + memory | SDK + MCP + schedules |
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) | Agent definitions | Inputs + output contracts | Handoffs + agents as tools | Streams + approvals | Your application | Tool outputs + sandboxes | Tools + MCP |
| [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) | Skills + plugins | Application task | Subagents | Sessions + permissions | Your SDK process | Native working files | SDK + MCP |
| [Haystack](https://docs.haystack.deepset.ai/docs/intro) | Components + pipelines | Pipeline contract | Agent pipelines | Pipeline instrumentation | Your runtime | Documents + outputs | Tools + HTTP deployment |
| [LlamaIndex](https://developers.llamaindex.ai/python/framework/) | Data + agent definitions | Workflow + structured output | Multi-agent workflows | Streams + human input | Framework or cloud layer | Data + workflow results | Data connectors + MCP |
| [AG2](https://docs.ag2.ai/latest/) | Conversable agents | Task conversations | Group chat + swarms | Human participation | Your application | Tool and code results | Tools + model integration |
| [AutoGen](https://github.com/microsoft/autogen) | AgentChat definitions | Conversation task | Teams + agent messages | AgentChat + Studio | Own runtime, archived project | Messages + execution results | MCP + code tools |
| [Semantic Kernel](https://learn.microsoft.com/en-us/semantic-kernel/overview/) | Plugins + instructions | Application-defined work | Application composition | Application controls | Your application | Function results | Plugins + model services |

### Native AI tools

Agent tools and protocols that can also power other products.

| Product | Reuse expertise | Set the job & outcome | Work with specialists | Watch and steer | Choose where it runs | Keep useful results | Connect and repeat |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [xPerfect](https://github.com/xPerfectAI/xPerfect/blob/main/README.md) | Saved experts + Library | Job + success criteria | Parallel + shared workers | Watch + steer | Your computer or server | Reports + saved files | API, MCP + scheduled webhooks |
| [Codex + App Server](https://developers.openai.com/blog/codex-as-a-platform) | Skills + instructions | Task + context | Threads + delegation | Events + steer + approvals | Local harness integration | Files + structured results | App Server + SDK + MCP |
| [Claude Code](https://code.claude.com/docs/en/overview) | Skills + plugins + memory | Task + project context | Subagents | Interactive sessions | Local and remote surfaces | Files + commands | MCP + integrations |
| [Grok Build](https://docs.x.ai/build/overview) | Skills + plugins + instructions | Task + tracked todos | Parallel subagents | Terminal + saved sessions | Local native CLI | Files + saved sessions | ACP + scripts + MCP |

### Managed runtimes

Cloud services that operate the harness or host your agent code.

| Product | Reuse expertise | Set the job & outcome | Work with specialists | Watch and steer | Choose where it runs | Keep useful results | Connect and repeat |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [xPerfect](https://github.com/xPerfectAI/xPerfect/blob/main/README.md) | Saved experts + Library | Job + success criteria | Parallel + shared workers | Watch + steer | Your computer or server | Reports + saved files | API, MCP + scheduled webhooks |
| [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) | Agent config + skills | Task in durable session | Managed subagents | Events + live steering | Managed harness + sandbox choice | Files + artifacts | API + MCP + webhooks |
| [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) | Agent config + skills | Outcome + grading rubric | Agent orchestration | Events + direction + interrupt | Managed or self-hosted sandbox | Persistent files + downloads | MCP + schedules + webhooks |
| [AWS Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html) | Your framework or container | Application-defined objective | Multiple agents on Instances | Streams + tracing | Managed AWS compute | Persistent session files | MCP + A2A + Identity |
| [Microsoft Foundry Hosted Agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents) | Containerized agent code | Application-defined objective | Your agent framework | Stream replay + resume | Microsoft-managed runtime | Persistent session filesystem | Responses + Invocations |
| [Gemini Enterprise Agent Platform (Agent Runtime + Agent Gateway)](https://docs.cloud.google.com/gemini-enterprise-agent-platform/overview) | Studio or framework agents | Agent-defined work | Framework + A2A | Agent observability | Google-managed Agent Runtime | Agent responses | Agent Gateway |

### Execution infrastructure

Durable execution components used with agent frameworks.

| Product | Reuse expertise | Set the job & outcome | Work with specialists | Watch and steer | Choose where it runs | Keep useful results | Connect and repeat |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [xPerfect](https://github.com/xPerfectAI/xPerfect/blob/main/README.md) | Saved experts + Library | Job + success criteria | Parallel + shared workers | Watch + steer | Your computer or server | Reports + saved files | API, MCP + scheduled webhooks |
| [Temporal integrations](https://docs.temporal.io/develop/python/integrations/openai-agents) | Keep framework definitions | Workflow-defined completion | Durable workflow orchestration | Replay + recovery | Your workers + service | Recorded results | Activity-backed tools |
| [DBOS + Pydantic AI](https://pydantic.dev/docs/ai/capabilities/durable_execution/dbos/) | Keep Pydantic agent specs | Workflow-defined completion | Durable queues | Checkpoint + resume | Your app + database | Stored step results | Durable model + MCP steps |
| [Daytona](https://www.daytona.io/docs/en/architecture/) | Images + snapshots | Application-defined job | Multiple sandboxes | Dashboard + terminals | Sandbox runners | Files + persistent volumes | SDK + API + CLI + MCP |

## Details and primary sources

### xPerfect

Turn reusable expertise into supervised work and retained deliverables.

- **Reuse expertise:** Start with five professional experts or save your own setup. Reuse exact versions of knowledge, notes, files, skills and bundles.
- **Set the job & outcome:** Set the job, desired outcome and background; retain project goals and success criteria.
- **Work with specialists:** Authorized workers discover and message each other. Configure parallel work, queues and shared workspaces.
- **Watch and steer:** Follow progress, give direction and use supported native controls from one operator surface.
- **Choose where it runs:** Run the control plane on your own host, with supported accounts, models and harnesses. Hosted model calls still leave the host.
- **Keep useful results:** Open and download retained results, including editable calculations. Continue a workspace or copy it for separate work.
- **Connect and repeat:** Operate the same records through UI, HTTP or MCP. Schedule saved work or signed outbound webhooks.

Sources: [xPerfect · README](https://github.com/xPerfectAI/xPerfect/blob/main/README.md), [xPerfect · 01 Vision Requirements and Terminology](https://github.com/xPerfectAI/xPerfect/blob/main/docs/01_Vision_Requirements_and_Terminology.md), [xPerfect · 07 Minimal Unified Operator UI](https://github.com/xPerfectAI/xPerfect/blob/main/docs/07_Minimal_Unified_Operator_UI.md), [xPerfect · Capability matrix](https://github.com/xPerfectAI/xPerfect/blob/main/docs/capability-matrix.md), [xPerfect · Workshop](https://github.com/xPerfectAI/xPerfect/blob/main/docs/workshop.md)

### Scion

Operate fleets of native agents with templates, skills and runtime brokers.

- **Reuse expertise:** Reusable templates and a versioned Skill Bank package agent setup and capabilities.
- **Set the job & outcome:** Give agents task input and reusable role instructions.
- **Work with specialists:** Agents delegate, message each other and share project context.
- **Watch and steer:** Workstation and hosted dashboards expose status, chat and terminals.
- **Choose where it runs:** Run workstation components or a hosted Hub with runtime brokers.
- **Keep useful results:** Workspaces and file access retain the working material of each agent.
- **Connect and repeat:** Manage agents and schedule project-scoped messages, including recurring messages.

Sources: [Scion · Source repository](https://github.com/GoogleCloudPlatform/scion), [Scion · Supported harnesses](https://googlecloudplatform.github.io/scion/supported-harnesses/), [Scion · Templates](https://googlecloudplatform.github.io/scion/local/templates/), [Scion · Skills](https://googlecloudplatform.github.io/scion/local/skills/), [Scion · Dashboard](https://googlecloudplatform.github.io/scion/workstation/dashboard/), [Scion · Scheduling](https://googlecloudplatform.github.io/scion/hosted/user/scheduling/), [Scion · Agent config](https://googlecloudplatform.github.io/scion/reference/agent-config/)

### Omnigent

Combine native harnesses in sessions people can supervise together.

- **Reuse expertise:** Package prompts, tools, specialists and reviewers in reusable agent files.
- **Set the job & outcome:** Give work in chat and apply policy controls for tools, approvals and budgets.
- **Work with specialists:** Run specialists and reviewers together; share or fork sessions.
- **Watch and steer:** Use terminal, browser or desktop to follow messages, tools and terminals.
- **Choose where it runs:** Use local hosts, remote hosts or configured sandbox providers.
- **Keep useful results:** Session files, messages and terminals stay synchronized across surfaces.
- **Connect and repeat:** Connect code functions and local or remote MCP servers.

Sources: [Omnigent · Source repository](https://github.com/omnigent-ai/omnigent)

### Paperclip

Coordinate agents through goals, tasks, budgets and an organization chart.

- **Reuse expertise:** Export and import organizations, agents and skills; retain versioned configuration.
- **Set the job & outcome:** Link assignments to their business goals so agents receive the purpose of the work.
- **Work with specialists:** Coordinate agents with assignees, task checkout and execution locks.
- **Watch and steer:** Track work and costs; apply approval gates and configuration rollback.
- **Choose where it runs:** Run the open-source server and UI with configured agent adapters.
- **Keep useful results:** Keep task history, comments and documents; supporting adapters resume saved sessions.
- **Connect and repeat:** Connect agent runtimes and govern their calls through configured connections.

Sources: [Paperclip · Source repository](https://github.com/PaperclipAI/paperclip)

### Agno + AgentOS

Serve agents, teams and native harnesses through AgentOS.

- **Reuse expertise:** Define reusable agents and teams with instructions, knowledge and tools.
- **Set the job & outcome:** Use agent requests or workflow definitions to describe the work.
- **Work with specialists:** Coordinate agents in teams and workflows; run background tasks.
- **Watch and steer:** Use the Control Plane, streaming APIs and persisted sessions.
- **Choose where it runs:** Operate the runtime and choose supported model and harness integrations.
- **Keep useful results:** Keep session state and return results through the configured agent or harness.
- **Connect and repeat:** Expose agents as services and schedule agent, team or workflow runs.

Sources: [Agno + AgentOS · Documentation](https://docs.agno.com/), [Agno + AgentOS · Harnesses as a service](https://docs.agno.com/use-cases/harnesses-as-a-service), [Agno + AgentOS · Scheduling](https://docs.agno.com/features/scheduling)

### OpenHands Agent Canvas + SDK

Manage software work across agent backends and automation surfaces.

- **Reuse expertise:** Configure the Software Agent SDK or choose an Agent Canvas backend.
- **Set the job & outcome:** Give a task in a conversation with the relevant workspace.
- **Work with specialists:** Manage separate conversations and automations through Agent Canvas.
- **Watch and steer:** Follow conversations, files and terminal activity in the operator UI.
- **Choose where it runs:** Use local Docker, VM or cloud deployment paths with supported accounts.
- **Keep useful results:** Review agent work through workspace files and terminals.
- **Connect and repeat:** Integrate Agent Server, Automation Server and configured backend tools.

Sources: [OpenHands Agent Canvas + SDK · Introduction](https://docs.openhands.dev/overview/introduction), [OpenHands Agent Canvas + SDK · Overview](https://docs.openhands.dev/openhands/usage/agent-canvas/overview)

### Dify

Create agent applications and knowledge workflows visually.

- **Reuse expertise:** Package instructions, knowledge, tools and plugins into applications.
- **Set the job & outcome:** Define the request and outputs in a visual agent or workflow application.
- **Work with specialists:** Compose steps and agent/tool calls in the workflow.
- **Watch and steer:** Configure and inspect applications through the product interface.
- **Choose where it runs:** Choose Dify Cloud or Community self-hosting; features vary by edition.
- **Keep useful results:** Return application responses and workflow outputs to users or clients.
- **Connect and repeat:** Publish APIs; cloud docs include schedule and inbound webhook triggers.

Sources: [Dify · Home](https://docs.dify.ai/en/home), [Dify · Schedule trigger](https://docs.dify.ai/en/cloud/use-dify/nodes/trigger/schedule-trigger), [Dify · Webhook trigger](https://docs.dify.ai/en/cloud/use-dify/nodes/trigger/webhook-trigger)

### Flowise

Build visual assistants and multi-agent flows with knowledge and tools.

- **Reuse expertise:** Configure instructions, tools and uploaded knowledge.
- **Set the job & outcome:** Describe the request and design the response path in Agentflow.
- **Work with specialists:** Compose agents, shared state, loops and branches.
- **Watch and steer:** Use human input checkpoints, streaming and persisted flow state.
- **Choose where it runs:** Operate Flowise on your infrastructure or use a hosted offering.
- **Keep useful results:** Return results and retain workflow state through the configured flow.
- **Connect and repeat:** Integrate applications using APIs, SDKs, embedded chat and MCP.

Sources: [Flowise · Documentation](https://docs.flowiseai.com/), [Flowise · Agentflowv2](https://docs.flowiseai.com/using-flowise/agentflowv2)

### n8n

Connect agent decisions to repeatable application workflows.

- **Reuse expertise:** Reuse workflows containing agents, model providers, memory and tools.
- **Set the job & outcome:** Define the input event and the application actions or output to produce.
- **Work with specialists:** Connect agent and tool steps through the workflow graph.
- **Watch and steer:** Inspect and manage the executions of configured workflows.
- **Choose where it runs:** Run n8n Cloud or your own instance; collaboration features depend on plan.
- **Keep useful results:** Keep workflow execution data and send results to connected applications.
- **Connect and repeat:** Use application integrations, API/webhook paths and published schedule triggers.

Sources: [n8n · Integrate ai.md](https://docs.n8n.io/build/integrate-ai.md), [n8n · N8n nodes base.scheduletrigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger), [n8n · Share with others.md](https://docs.n8n.io/build/manage-workflows/share-with-others.md)

### Pydantic AI + Harness

Define typed agents and use reusable harness capabilities.

- **Reuse expertise:** Share YAML or JSON agent specifications, capabilities and skills.
- **Set the job & outcome:** Describe work in instructions and define the result structure in code.
- **Work with specialists:** Use harness subagents and application orchestration.
- **Watch and steer:** Use step persistence or integrations such as Temporal and DBOS for durable work.
- **Choose where it runs:** Run your Python application with supported model providers.
- **Keep useful results:** Use harness workspaces and return typed agent results.
- **Connect and repeat:** Use tools, MCP and development web chat, CLI or application UI protocols.

Sources: [Pydantic AI + Harness · Overview](https://pydantic.dev/docs/ai/overview/), [Pydantic AI + Harness · Agent spec](https://pydantic.dev/docs/ai/core-concepts/agent-spec/), [Pydantic AI + Harness · Harness](https://pydantic.dev/docs/ai/harness/), [Pydantic AI + Harness · Skills](https://pydantic.dev/docs/ai/harness/skills/), [Pydantic AI + Harness · Overview](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/), [Pydantic AI + Harness · Web](https://pydantic.dev/docs/ai/guides/web/), [Pydantic AI + Harness · Interfaces](https://pydantic.dev/docs/ai/overview/interfaces/)

### LangGraph + LangSmith Deployment

Combine a stateful agent runtime with deployment and operations tools.

- **Reuse expertise:** Define agent graphs and configurable assistant versions.
- **Set the job & outcome:** Express the job through graph input, state and application-defined outputs.
- **Work with specialists:** Compose specialists and deterministic steps in graphs.
- **Watch and steer:** Persist execution, stream progress and interrupt for human input.
- **Choose where it runs:** Choose LangSmith Deployment options or operate the graph runtime yourself.
- **Keep useful results:** Keep graph state and return results through deployed agent APIs.
- **Connect and repeat:** Expose agent interfaces and schedule assistant runs in deployment.

Sources: [LangGraph + LangSmith Deployment · Overview](https://docs.langchain.com/oss/python/langgraph/overview), [LangGraph + LangSmith Deployment · Deployment](https://www.langchain.com/langsmith/deployment), [LangGraph + LangSmith Deployment · Configuration cloud](https://docs.langchain.com/langsmith/configuration-cloud), [LangGraph + LangSmith Deployment · Cron jobs](https://docs.langchain.com/langsmith/cron-jobs), [LangGraph + LangSmith Deployment · Pricing](https://www.langchain.com/pricing)

### Deep Agents

Use planning, files, memory and specialists in a reusable harness.

- **Reuse expertise:** Package domain knowledge and workflows as reusable skills.
- **Set the job & outcome:** Use task planning and decomposition for complex work.
- **Work with specialists:** Delegate to subagents with separate contexts.
- **Watch and steer:** Use LangGraph approvals and persistent memory across work.
- **Choose where it runs:** Run the library with model and filesystem/sandbox backends.
- **Keep useful results:** Use local disk, durable stores or sandbox files for work and results.
- **Connect and repeat:** Connect custom tools and supported model providers.

Sources: [Deep Agents · Overview](https://docs.langchain.com/oss/javascript/deepagents/overview)

### CrewAI + AMP

Define role-based crews and flows, with AMP for visual operations.

- **Reuse expertise:** Agents combine roles, knowledge, tools, MCP and skills.
- **Set the job & outcome:** Assign tasks and define the expected result; AMP includes a visual Task Builder.
- **Work with specialists:** Use sequential or hierarchical teams and stateful flows.
- **Watch and steer:** Resume flow state and operate deployments through AMP features.
- **Choose where it runs:** Run the code framework yourself or use the AMP platform.
- **Keep useful results:** Return outputs from agent tasks and flow steps.
- **Connect and repeat:** Use tools and MCP; platform integrations and triggers connect business apps.

Sources: [CrewAI + AMP · Documentation](https://docs.crewai.com/), [CrewAI + AMP · Tasks](https://docs.crewai.com/v1.15.30/en/concepts/tasks), [CrewAI + AMP · Agent capabilities](https://docs.crewai.com/v1.15.30/en/concepts/agent-capabilities)

### Microsoft Agent Framework

Compose agents and workflows with Microsoft Agent Framework.

- **Reuse expertise:** Configure instructions, tools and reusable harness capabilities.
- **Set the job & outcome:** Define work as agent input or a graph/functional workflow.
- **Work with specialists:** Compose agents and deterministic functions in workflows.
- **Watch and steer:** Use persisted checkpoints and human-in-the-loop request ports.
- **Choose where it runs:** Run an application yourself or use Foundry Hosted Agents.
- **Keep useful results:** Return workflow results; harness capabilities include files and memory.
- **Connect and repeat:** Connect model providers and application tools through framework interfaces.

Sources: [Microsoft Agent Framework · Overview](https://learn.microsoft.com/en-us/agent-framework/overview/), [Microsoft Agent Framework · Checkpoints](https://learn.microsoft.com/agent-framework/workflows/checkpoints), [Microsoft Agent Framework · Human in the loop](https://learn.microsoft.com/en-us/agent-framework/workflows/human-in-the-loop), [Microsoft Agent Framework · Source repository](https://github.com/microsoft/agent-framework)

### Google ADK

Build modular agents and workflows using Google ADK.

- **Reuse expertise:** Package agent instructions, tools and reusable skills.
- **Set the job & outcome:** Describe work through agent input and workflow configuration.
- **Work with specialists:** Compose agent workflows and specialist patterns.
- **Watch and steer:** Use session state, streaming, cancellation and resume paths.
- **Choose where it runs:** Develop locally and choose a deployment target.
- **Keep useful results:** Use artifact handling and agent outputs for application results.
- **Connect and repeat:** Connect tools and agent services through supported protocols.

Sources: [Google ADK · Documentation](https://adk.dev/), [Google ADK · Skills](https://adk.dev/skills/)

### Mastra

Build agents and harnesses with goals and durable work.

- **Reuse expertise:** Configure TypeScript agents with tools, memory and skills.
- **Set the job & outcome:** Use Goals to work toward an objective with evaluation and budget controls.
- **Work with specialists:** Delegate tasks and run background work through harness capabilities.
- **Watch and steer:** Approve, stop or steer work using AgentController and signals.
- **Choose where it runs:** Run the framework in your application and use Studio/platform tools.
- **Keep useful results:** Use workspace tools and agent/workflow outputs.
- **Connect and repeat:** Connect tools and configure schedules or event providers.

Sources: [Mastra · Docs](https://mastra.ai/docs), [Mastra · Overview](https://mastra.ai/docs/harness/overview)

### Letta

Create stateful agents with persistent memory.

- **Reuse expertise:** Reuse stateful agent memory, skills and configuration.
- **Set the job & outcome:** Supply work to the persistent agent through its interface.
- **Work with specialists:** Use documented subagent capabilities for delegated work.
- **Watch and steer:** Continue work with stored agent context and runtime interfaces.
- **Choose where it runs:** Use the documented self-hosting or service path.
- **Keep useful results:** Return results while retaining the agent memory state.
- **Connect and repeat:** Connect SDKs, MCP/ACP interfaces and schedules.

Sources: [Letta · Documentation](https://docs.letta.com/)

### OpenAI Agents SDK

Program agent loops, tools, handoffs and application behavior.

- **Reuse expertise:** Reuse instructions, tools, skills and agent configuration in code.
- **Set the job & outcome:** Define work and expected result types in your application.
- **Work with specialists:** Delegate and compose agent work using SDK mechanisms.
- **Watch and steer:** Use streaming, sessions, tracing and human-in-the-loop controls.
- **Choose where it runs:** Operate the SDK in your chosen runtime; Temporal is a separate durable integration.
- **Keep useful results:** Return agent/tool outputs; sandbox integrations provide file and shell work.
- **Connect and repeat:** Connect application functions, MCP servers and model providers.

Sources: [OpenAI Agents SDK · Openai agents python](https://openai.github.io/openai-agents-python/)

### Claude Agent SDK

Embed the Claude Code harness in your own application.

- **Reuse expertise:** Reuse Claude Code instructions, skills, plugins and MCP tools.
- **Set the job & outcome:** Send the task and context through the SDK.
- **Work with specialists:** Delegate work through configured subagents.
- **Watch and steer:** Stream progress and manage sessions, permissions, hooks and approvals.
- **Choose where it runs:** Operate the application and its execution environment.
- **Keep useful results:** Use the native file and shell tools and receive results in your application.
- **Connect and repeat:** Connect Python/TypeScript application code and MCP tools.

Sources: [Claude Agent SDK · Overview](https://code.claude.com/docs/en/agent-sdk/overview)

### Haystack

Build agents and retrieval pipelines over documents and tools.

- **Reuse expertise:** Reuse agent components, retrieval pipelines and document stores.
- **Set the job & outcome:** Define inputs, processing and outputs in the application pipeline.
- **Work with specialists:** Compose tools and components within your agent application.
- **Watch and steer:** Inspect configured pipeline and agent execution; platform tools add operations.
- **Choose where it runs:** Run the Python framework in your chosen deployment.
- **Keep useful results:** Process documents and return pipeline/agent outputs.
- **Connect and repeat:** Connect model integrations, tools and HTTP services.

Sources: [Haystack · Intro](https://docs.haystack.deepset.ai/docs/intro)

### LlamaIndex

Connect data and retrieval to agent workflows.

- **Reuse expertise:** Reuse indexed knowledge, tools and agent configuration.
- **Set the job & outcome:** Define work through workflows and structured result requirements.
- **Work with specialists:** Compose agents and data workflows.
- **Watch and steer:** Use workflow state, streaming and human-in-the-loop patterns.
- **Choose where it runs:** Operate the framework; LlamaCloud/LlamaAgents are separate service layers.
- **Keep useful results:** Return retrieval results and agent/workflow outputs.
- **Connect and repeat:** Connect data sources, models and MCP tools.

Sources: [LlamaIndex · Framework](https://developers.llamaindex.ai/python/framework/)

### AG2

Compose conversations and collaborative agent workflows.

- **Reuse expertise:** Reuse configured agents, tools and conversational roles.
- **Set the job & outcome:** Provide tasks and completion conditions through the application.
- **Work with specialists:** Use group conversations, swarm patterns and human-agent collaboration.
- **Watch and steer:** Keep humans in agent conversations and inspect interactions.
- **Choose where it runs:** Run the framework in your environment; Playground is a separate interface.
- **Keep useful results:** Use tool outputs, retrieval and code execution results.
- **Connect and repeat:** Connect tools, retrieval and supported model integrations.

Sources: [AG2 · Latest](https://docs.ag2.ai/latest/)

### AutoGen

Historical multi-agent framework; Microsoft now directs new work to Agent Framework.

- **Reuse expertise:** Reuse agent configuration and tools in the archived framework.
- **Set the job & outcome:** Give teams work through task input and configured termination rules.
- **Work with specialists:** Compose AgentChat teams and Core message-based agents.
- **Watch and steer:** Inspect agent conversations using code or AutoGen Studio.
- **Choose where it runs:** Existing code can run in your environment; the official repository is archived.
- **Keep useful results:** Receive agent responses and configured tool/code outputs.
- **Connect and repeat:** The retained docs cover MCP and code-execution integrations.

Sources: [AutoGen · Source repository](https://github.com/microsoft/autogen), [AutoGen · Stable](https://microsoft.github.io/autogen/stable/)

### Semantic Kernel

Connect model reasoning to existing application functions.

- **Reuse expertise:** Reuse application functions as plugins and configure model instructions.
- **Set the job & outcome:** The host application defines the request and useful output.
- **Work with specialists:** Compose model and plugin calls in the application.
- **Watch and steer:** Use the surrounding application to expose control and review.
- **Choose where it runs:** Integrate the SDK in C#, Python or Java applications.
- **Keep useful results:** Return results from application functions and model calls.
- **Connect and repeat:** Connect existing APIs/functions and model services.

Sources: [Semantic Kernel · Overview](https://learn.microsoft.com/en-us/semantic-kernel/overview/)

### Codex + App Server

Use the native Codex harness directly or embed its App Server.

- **Reuse expertise:** Specialize Codex with reusable skills and project/application instructions.
- **Set the job & outcome:** Supply a task with workspace and application context.
- **Work with specialists:** Use separate threads and native delegated work capabilities.
- **Watch and steer:** App Server exposes turns, streamed events, steering, interrupts and approvals.
- **Choose where it runs:** Run the open-source CLI or App Server; model access remains separate.
- **Keep useful results:** Work on files and return tool results or structured outputs.
- **Connect and repeat:** Integrate the harness through its protocol, SDK or MCP tools.

Sources: [Codex + App Server · Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform), [Codex + App Server · App server](https://learn.chatgpt.com/docs/app-server)

### Claude Code

Use Claude Code directly across terminal, IDE, desktop and web.

- **Reuse expertise:** Specialize the tool with reusable instructions, skills and plugins.
- **Set the job & outcome:** Give a task in conversation with the relevant project.
- **Work with specialists:** Use configured specialist subagents.
- **Watch and steer:** Follow work, provide input and use native permission controls.
- **Choose where it runs:** Choose the supported execution surface and account/provider route.
- **Keep useful results:** Read and edit workspace files and run commands.
- **Connect and repeat:** Connect tools and development services; SDK supports embedding.

Sources: [Claude Code · Overview](https://code.claude.com/docs/en/overview), [Claude Code · Overview](https://code.claude.com/docs/en/agent-sdk/overview)

### Grok Build

Use Grok’s native coding agent through terminal, scripts or ACP.

- **Reuse expertise:** Reuse skills containing instructions, scripts and resources; plugins can add agents, hooks and MCP servers.
- **Set the job & outcome:** Give a task with workspace context; the session retains a structured list of planned and completed work.
- **Work with specialists:** Delegate to independent child sessions with their own context; children return summaries to the parent.
- **Watch and steer:** Use the interactive terminal, permission controls, resume, fork and rewind operations.
- **Choose where it runs:** Run the native agent with browser sign-in or an API key; configure supported custom model endpoints.
- **Keep useful results:** Work on files and retain session messages, tool calls and file snapshots; export transcripts.
- **Connect and repeat:** Embed through ACP, run headless scripts with JSON output, or connect MCP tools and plugins.

Sources: [Grok Build · Overview](https://docs.x.ai/build/overview), [Grok Build · Skills plugins marketplaces](https://docs.x.ai/build/features/skills-plugins-marketplaces), [Grok Build · Subagents](https://docs.x.ai/build/features/subagents), [Grok Build · Sessions](https://docs.x.ai/build/features/sessions), [Grok Build · Permissions](https://docs.x.ai/build/features/permissions), [Grok Build · Reference](https://docs.x.ai/build/cli/reference)

### kagent + agentgateway

Operate declarative and native agents under Kubernetes runtime policy.

- **Reuse expertise:** Version agent templates containing models, prompts, tools, skills and plugins.
- **Set the job & outcome:** Give work to a session using an agent template paired with a harness.
- **Work with specialists:** Bind other templates as tools for delegated work.
- **Watch and steer:** Suspend, resume and fork sessions from pinned checkpoints.
- **Choose where it runs:** Separate application behavior from runtime, sandbox and worker-pool policy.
- **Keep useful results:** Retain execution state through snapshots; file UI features depend on harness route.
- **Connect and repeat:** Connect MCP tools and route model calls through governance policies.

Sources: [kagent + agentgateway · Core concepts](https://kagent.dev/docs/kagent/1.x/about/core-concepts/), [kagent + agentgateway · Declarative agents](https://kagent.dev/docs/kagent/1.x/agents/declarative-agents/), [kagent + agentgateway · Byo agentgateway](https://kagent.dev/docs/kagent/1.x/setup/model-providers/byo-agentgateway/), [kagent + agentgateway · Faq](https://kagent.dev/docs/kagent/1.x/reference/faq/)

### OpenAI Agents API

Use a managed Codex harness through the Agents API.

- **Reuse expertise:** Set instructions, tools, MCP servers and environment-loaded skills.
- **Set the job & outcome:** Send work into a session that retains its context.
- **Work with specialists:** Delegate subtasks through the managed harness.
- **Watch and steer:** Stream progress, steer current work and resume sessions.
- **Choose where it runs:** Choose OpenAI-hosted, self-hosted or no sandbox; the harness remains managed.
- **Keep useful results:** Use sandbox commands and files to produce artifacts.
- **Connect and repeat:** Connect application tools and receive lifecycle events.

Sources: [OpenAI Agents API · Overview](https://developers.openai.com/api/docs/guides/agents-api/overview), [OpenAI Agents API · Agents](https://developers.openai.com/api/docs/guides/agents)

### Claude Managed Agents

Use a managed Claude harness for long-running, outcome-based work.

- **Reuse expertise:** Configure instructions, skills and MCP tools.
- **Set the job & outcome:** Define a result and rubric; a separate grader drives bounded revision cycles.
- **Work with specialists:** Use the managed harness and its configured orchestration capabilities.
- **Watch and steer:** Follow progress, add direction and interrupt an active outcome.
- **Choose where it runs:** Choose where tools execute; Anthropic still operates the managed service.
- **Keep useful results:** Retain session files and attach or download artifacts.
- **Connect and repeat:** Connect tools and configure scheduled deployments or lifecycle notifications.

Sources: [Claude Managed Agents · Overview](https://platform.claude.com/docs/en/managed-agents/overview), [Claude Managed Agents · Define outcomes](https://platform.claude.com/docs/en/managed-agents/define-outcomes), [Claude Managed Agents · Choosing an environment](https://platform.claude.com/docs/en/managed-agents/choosing-an-environment)

### AWS Bedrock AgentCore

Host chosen agent code with managed sessions and AWS services.

- **Reuse expertise:** Deploy application-owned agent behavior and dependencies.
- **Set the job & outcome:** The hosted agent code defines the task, output and success checks.
- **Work with specialists:** Use supported Instance sessions for collaborating agents.
- **Watch and steer:** Observe agent steps and run long-lived or asynchronous sessions.
- **Choose where it runs:** Choose managed microVMs or managed EC2 Instances in your AWS account.
- **Keep useful results:** Keep filesystem state across stop/resume cycles.
- **Connect and repeat:** Expose agents/tools and authenticate connections to external services.

Sources: [AWS Bedrock AgentCore · Agents tools runtime.html](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html), [AWS Bedrock AgentCore · Runtime instances how it works.html](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html)

### Microsoft Foundry Hosted Agents

Host your agent code with Microsoft-managed session infrastructure.

- **Reuse expertise:** Package your own framework, dependencies and behavior.
- **Set the job & outcome:** Your agent code defines how a task reaches its desired output.
- **Work with specialists:** Host the multi-agent logic provided by your chosen framework.
- **Watch and steer:** Resume stateful work and reconnect clients to streamed results.
- **Choose where it runs:** Foundry operates sandbox lifecycle, scaling and identity.
- **Keep useful results:** Keep files in the session sandbox across idle/resume.
- **Connect and repeat:** Expose protocol endpoints, including custom webhook payloads.

Sources: [Microsoft Foundry Hosted Agents · Hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents), [Microsoft Foundry Hosted Agents · Hosting](https://learn.microsoft.com/en-us/agent-framework/hosting/)

### Gemini Enterprise Agent Platform (Agent Runtime + Agent Gateway)

Deploy and govern agents with Google Cloud’s Agent Runtime and Agent Gateway.

- **Reuse expertise:** Use visual Agent Studio or code frameworks such as ADK.
- **Set the job & outcome:** The deployed agent specifies its task behavior and useful outputs.
- **Work with specialists:** Use framework orchestration and governed agent-to-agent connections.
- **Watch and steer:** Inspect agent interactions through tracing and gateway telemetry.
- **Choose where it runs:** Run agents on Gemini Enterprise Agent Platform’s managed Agent Runtime with centralized identity and access policies.
- **Keep useful results:** Return the outputs produced by the deployed agent application.
- **Connect and repeat:** Govern MCP, A2A, REST and gRPC connections. Wider deployment models have separate availability.

Sources: [Gemini Enterprise Agent Platform (Agent Runtime + Agent Gateway) · Overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/overview), [Gemini Enterprise Agent Platform (Agent Runtime + Agent Gateway) · Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime), [Gemini Enterprise Agent Platform (Agent Runtime + Agent Gateway) · Agent gateway overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview)

### Temporal integrations

Add durable execution to Pydantic AI or OpenAI Agents SDK.

- **Reuse expertise:** Agent expertise stays in Pydantic AI or OpenAI Agents SDK configuration.
- **Set the job & outcome:** The agent application defines its task and completion contract.
- **Work with specialists:** Run the agent loop and handoffs inside workflows.
- **Watch and steer:** Recover after worker restarts without repeating recorded model calls.
- **Choose where it runs:** Your workers run application logic against a Temporal service.
- **Keep useful results:** Store durable activity results; application storage owns large artifacts.
- **Connect and repeat:** Put external I/O in activities and use documented framework integrations.

Sources: [Temporal integrations · Openai agents](https://docs.temporal.io/develop/python/integrations/openai-agents), [Temporal integrations · Temporal](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/)

### DBOS + Pydantic AI

Add database-backed durable runs to Pydantic AI.

- **Reuse expertise:** Reusable expertise remains in the Pydantic AI agent.
- **Set the job & outcome:** Run the agent inside a DBOS workflow with application-defined outputs.
- **Work with specialists:** Control concurrency, rate limits, priorities and timeouts.
- **Watch and steer:** Recover workflows from completed database-backed steps.
- **Choose where it runs:** Run the durable application with its system database.
- **Keep useful results:** Persist workflow inputs and step outputs for recovery.
- **Connect and repeat:** Use the Pydantic integration; wrap custom external I/O in explicit steps.

Sources: [DBOS + Pydantic AI · Dbos](https://pydantic.dev/docs/ai/capabilities/durable_execution/dbos/)

### Daytona

Provide isolated execution environments for agent code and tools.

- **Reuse expertise:** Reuse execution environments from images and snapshots; agent expertise stays in the chosen harness or application.
- **Set the job & outcome:** The agent application defines the job and success checks; Daytona supplies its execution environment.
- **Work with specialists:** Run separate sandbox workloads across compute runners with allocated resources.
- **Watch and steer:** Manage sandbox lifecycle and inspect processes, logs, terminals or desktop activity.
- **Choose where it runs:** Run workloads in isolated sandboxes across shared or dedicated regions.
- **Keep useful results:** Access sandbox files and use persistent volumes across sandboxes.
- **Connect and repeat:** Integrate sandbox creation, code execution, filesystem operations and access to running services.

Sources: [Daytona · Architecture](https://www.daytona.io/docs/en/architecture/), [Daytona · Sandboxes](https://www.daytona.io/docs/en/sandboxes/), [Daytona · Playground](https://www.daytona.io/docs/en/playground/)

