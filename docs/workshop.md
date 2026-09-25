# xPerfect: from a goal to a useful result

A practical build-and-review exercise for a technical workshop. The demonstration takes about
15 minutes; participants can then repeat it with the same synthetic input.

## What participants make

A small AI Expert: one intended user, one mission, a known source, an output, and a clear check.

Synthetic example: a release-note editor. Give it a short changelog and ask it to write a concise
Markdown release note that preserves all changes and identifies missing facts. The peer checks
the output against the input. This requires no customer data, external posting or repository write.

Use [the synthetic changelog](demo/changelog.md) with this task:

> Write `release-notes.md` for readers of this app. Use only the attached changelog. Include each
> of the four changes once. Keep the missing release date and unreviewed compatibility explicit.
> Do not imply that bulk export is available. Stay under 150 words.

Peer check: all four changes, no invented date, no bulk-export promise, compatibility uncertainty
preserved, and a readable downloadable Markdown file under 150 words.

## A 15-minute demonstration

| Time | Show | Say |
| --- | --- | --- |
| 0–2 min | A previously rehearsed input and output, clearly labelled as recorded evidence | “The result must be useful to someone. We will check this one against its source.” |
| 2–4 min | [Workspace diagram](assets/workspaces.png) | “A project groups the work. Each worker uses a native agent and an execution workspace. Grouping workers does not itself make them share a computer.” |
| 4–6 min | The unlocked web UI, then `./xperfect doctor` | “A running UI is only the start. Doctor shows the exact model and whether the provider's tool is installed; Connections shows the signed-in account.” |
| 6–10 min | The prepared synthetic file, a bounded task, progress and the produced Markdown file; the [context and tools diagram](assets/context-tools.png) while it runs | “The worker gets the goal and the file we attached. It keeps its own built-in tools, and only the connections we allow. Our check is simple: every listed change survives, and missing information stays explicit.” |
| 10–12 min | Open/download the output and reload the same task | “Inspect the file. Return to the work without starting it again.” |
| 12–14 min | A rehearsed interrupt/continue flow on a disposable task | “Control and recovery are part of the work.” |
| 14–15 min | [Deployment diagram](assets/deployment.png) and the next exercise | “On your computer or a hosted server, the work stays with you; provider requests can leave. Use one source, one useful output and one peer who can tell you whether it worked.” |

Optional 3-minute extension, only if rehearsed on the same release: ask for two independent
short results in one Conversation. Show the [several-goals diagram](assets/parallel-work.png),
then the two worker cards and the one combined answer after a reload. Say: “The AI chose to use
two workers; each started when its account was free. Ten goals would not mean ten workers at once.”

## Participant exercise

Write five lines: **User / Mission / Sources / Output / Done**. Connect an approved provider, add the
synthetic source, run the task, inspect the output and ask a peer for one line of feedback. Record
the problem they found and refine the mission. Serving the Expert to other users requires the
separate, tested hosted identity and access path; a local worker is not automatically a public app.

## Presenter release check

Before the session, on the machine you will present from:

```sh
./xperfect start      # first start asks for the unlock password
./xperfect doctor     # exact model, provider tool and account setup status
```

Then connect your provider in **Connections**, run the synthetic task once, and record it.

Rehearse the exact release, account route, Files controls and output download before this becomes
a live script. Record the revision, readiness, successful result, reload and recovery evidence.
Use only profiles that passed that rehearsal. Rehearse each provider and deployment route you plan to demonstrate.

If live readiness fails, show the labelled recording from the same verified release and explain
the observed failure. If no verified recording exists, present the architecture as a design
walkthrough and do not claim participants have built a working Expert.

## Close

“Try one real task. Follow the project, star or watch the repository, and share what you made and
what you learned. If you want to contribute, use the contribution guide.” Forking is an optional
contributor path. Use links that match the demonstrated release.

Developers can continue with the [developer walkthrough](developer.md): connect over MCP, start and
steer work, or add a harness.

[Repository](https://github.com/xPerfectAI/xPerfect) ·
[Adrien on LinkedIn](https://www.linkedin.com/in/adrienbeyk/) ·
[Instagram](https://www.instagram.com/adrienbeyk/)
