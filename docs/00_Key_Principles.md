# xPerfect AI: Key Principles

These are the core rules for everyone who designs, develops, documents or operates xPerfect AI,
including agents. This is their single source of truth. Feature documents own their concrete
requirements; they link here instead of copying these rules.

## 1. Trust model and harness intelligence

Let the AI interpret goals, apply expertise, plan and choose tools. xPerfect AI mainly provides
reliable, secure data transport and a control plane around the native harnesses. Pass the full
user goal, context, constraints, files and authorized capabilities faithfully. Reduce guesswork
by giving the model real evidence, not by guessing its intent in application code.

Never hardcode or overfit behavior to one user, prompt, example, workflow, provider label or
machine. Do not use keyword or regex rules to decide what a request means. Typed schemas,
identities, capabilities, permissions, quotas and lifecycle rules are runtime structure; semantic
judgment belongs to the model. Do not invent plans, success criteria, tool results or mandatory
artifacts that the user did not ask for.

## 2. Require parity

Supported harnesses, channels and execution paths must each deliver intelligent, relevant, useful,
aligned results with fast, smooth, reliable behavior. Preserve the user's exact model, account,
context, files and authority across paths. Do not silently substitute them or lower quality to
make a path faster. State real capability differences clearly; do not claim parity without proof.

## 3. Keep one source of truth

Give each requirement, configuration, fact and feature one named owner. Other components consume
or reference that source instead of maintaining copies that can drift. Generated output is not
an authoring surface. Correct the owning source and carry the change through its consumers.

## 4. Separate concerns

Models own judgment. Native harnesses own their supported execution behavior. The control plane
owns identity, authorization, lifecycle, scheduling, recovery and observability. Transport carries
exact inputs and results; adapters translate protocols; the UI makes actions and state clear.
Keep these responsibilities modular. Do not turn transport or UI code into a second planning agent.

## 5. Do not reinvent the wheel

Study the existing codebase, related components and consumers before designing a solution. Search
for and understand mechanisms already available across the project, not just the file being edited.
Research current tools, repositories, skills and code online in enough depth to compare credible
options; verify capabilities against primary sources. Prefer the simplest proven native or existing
mechanism that meets the need. Record why new code is necessary when reuse does not fit. Reuse valid
research and findings; do not turn each small repair into a new research campaign.

## 6. Minimize user effort

Minimize the clicks, steps, required choices and time needed to reach useful value. Design for a new
user to understand and use the product naturally, without training or a learning curve. Keep the
normal path obvious; show advanced controls when useful. Simplicity must preserve capabilities,
truthful errors and security, not hide them.

## 7. Think and plan, then complete the work

Understand the outcome, inspect the owning code and dependencies, and choose a coherent approach
before developing. Complete compatible work as a batch. Do not follow every small edit with long
QA, installs, builds or repeated test suites. During development, use small causal checks when a
real defect or uncertainty requires them.

Then run the relevant tests for the changed behavior and its affected consumers, and use the real
product like a user, including visual QA. Check the result, recovery and persistence where affected.
The user's agreed QA scope controls the work; the default is blast-radius, not an exhaustive matrix.
Retain valid evidence and repeat only what changed, failed or remains unproved. Finish complete
user outcomes, not a procession of partial edits and status reports.

## 8. Preserve learning without drift

Keep requirements, instructions, decisions, findings and reusable lessons in organized, clearly
named owning documents. Use these documentation rules:

- **Update before creating.** Find the existing owner for the feature or concept. Extend it rather
  than adding another plan, requirements file or incident summary for the same subject.
- **Make the owner useful.** Give a developer the current requirements, intended behavior, main use
  cases, integration points, known limits and relevant lessons. Include only what the feature needs;
  these are useful questions, not mandatory empty sections or a template to fill on every edit.
- **Use clear names and a small index.** Keep product truth in `docs/`, follow the existing numbered
  reference convention for new core topics, and link each owner from the README documentation index.
  Guides explain a user journey and link to the owning contract instead of duplicating it. Keep
  contracts, tests and source code in their existing homes.
- **Keep current truth clear.** Update the owning requirement when behavior changes. Record a lesson
  with its cause, correction and useful evidence where it belongs. Retain meaningful history as
  clearly marked history; do not leave competing active instructions or stale claims in the main path.
- **Keep evidence proportional and private data private.** Reference the existing tests and evidence;
  do not create a second status ledger. Publish sanitized findings and limits, while keeping raw
  prompts, credentials, personal data and machine captures outside the public repository.

Documentation must reduce the next developer's guesswork. It is not a separate delivery campaign:
no whole-tree reread, documentation rewrite, extra audit or review round after each small change.

## 9. Reject AI slop

No generic filler, inflated claims, decorative complexity or output that merely looks complete.
Every word, screen, control and artifact must serve the user's actual goal. Use short, plain,
precise language. Avoid blabber, jargon, dense text and overwhelming interfaces. Judge quality by
useful, verified results and natural interaction, not by volume of text, code, tests or process.

## 10. Own delivery and preserve trust

Continue through routine authorized blockers and carry work to its actual result. Respect real
permission and security boundaries, preserve personal data and unrelated work, and report exact
remaining gaps honestly. Give collaborators the full outcome, context, constraints and evidence;
let them choose methods. Intervene for a demonstrated defect, drift or stall, not to add ceremony.

The shared outcome metric is **quality + performance**: intelligence, relevance, usefulness and
alignment together with speed, smoothness and reliability. Neither side can replace the other.
