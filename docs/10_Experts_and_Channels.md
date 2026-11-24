# Experts on channels

An owner makes an Expert (for example an AI version of their working self), gives it
instructions, and lets the people they choose message it where they already work, starting
with Microsoft Teams. A fast speaking assistant answers at once and hands work to a background
coordinator, which runs Workers in parallel. The owner can ask to approve what the Expert sends
or starts. This document owns the requirements; the [QA playbook](05_QA_Quick_Power_Playbook.md#11-experts-on-channels)
owns the acceptance cases.

Follow the [key principles](00_Key_Principles.md). Models own judgment: what to answer, what to hand
off and when to ask the owner. The runtime owns identity, holds, delivery and recovery. Nothing
matches words.

## What an Expert is

An Expert is the existing saved expert (a workspace template lineage). Its newest version is
current; every save makes a new version. The `expert` key in its content holds:

| Setting | Meaning |
|---|---|
| `instructions` | Who it is, what it does for people, what it must never share. The speaking assistant reads the newest text when each turn starts. A running job keeps the text it started with. |
| `team.talk` | The AI that talks to people (Claude or Codex), its model (for Claude: Opus or the faster Haiku), its account, its effort and an optional backup. The form shows each model's median time to a finished reply on this computer. New Experts talk on Claude Haiku at low effort, so short replies come in about 3 seconds; Opus stays selectable, and a saved Expert keeps the model it was saved with. |
| `team.coordinate` | The AI that runs each job, its account and an optional backup. |
| `team.work` | The AIs that may work on a job, in parallel. |
| `ask_me_first` | `always`, `auto` (default) or `off`. |
| `workspace` | Where it works: this computer, where its Workers run with full access, or a sandboxed workspace, where its Workers run in containers and reach only that workspace. Saving any other level is refused; narrower permissions on this computer are a later capability. |

Knowledge and Skills stay in My Library and are referenced by exact version. An empty account
means this computer's own sign-in for that AI. Every account, this computer's sign-in included,
is leased to one Worker run at a time: Workers of the same AI on one sign-in take turns, and a job
runs in parallel across the AI types it may use. The speaking assistant's replies are not held by
those Workers on this computer's sign-in; a connected account cannot serve her and Workers together.

## Three roles

- **The speaking assistant** has one conversation per person and per channel thread. With Claude
  it runs coordination-only: Claude Code's built-in file, shell, web and browser tools are off, its
  session is kept, and only its own four tools are allowed (`expert_hand_off`, `expert_jobs`,
  `expert_job_control`, `expert_ask_owner`, served at `/v1/native/expert`). It therefore reads no
  file itself and answers quickly. A Codex speaking assistant keeps Codex's own tools; this
  difference is stated where the AI is chosen.
- **Each hand-off is a job**: one background coordinator conversation with the coordinator's AI,
  routes for the allowed Workers, the Expert's Library, its instructions and its access level. The
  job's final answer is written once as a result event on the speaking assistant's conversation,
  and the existing result wake starts her next turn.
- **Workers** are dispatched by that coordinator with the existing tools and run in parallel within
  the host's capacity. Each Worker carries the owner's instructions as its standing instructions,
  fixed when the job starts, whatever the coordinator writes in a dispatch. Coordinator-dispatched Workers are exempt from the manual one-project fence.

## Ask me first

One mechanism: what the Expert is about to send or start waits for the owner.

- A request shows exactly what waits: a reply with its text, or a job with its goal, plus who
  asked, where, when, and the Expert's reason.
- Approve releases that and nothing else: the runtime sends the reply that was shown, word for
  word, or starts the job that was shown.
- A changed or stopped job withdraws its request; an old approval then releases nothing.
- Reject is told to the speaking assistant in a turn with every native tool off; she writes her
  own reply, which goes out directly. The "checking with my owner" line in Always ask is written
  the same way. No sentence is scripted.
- **Off:** nothing waits. Instructions still apply.
- **Auto:** each incoming message carries who, where and when; the speaking assistant decides
  whether to call `expert_ask_owner`.
- **Always ask:** the runtime holds two exits on channel conversations: a reply leaving to a person
  and a job starting. The owner's own Chat in xPerfect is not held: there the owner is the approver.
- Only a signed-in owner session decides (`POST /v1/expert-reviews/{id}/decision` requires the
  page's `human:confirm` assertion, on a local install too). A runtime token, a Worker's run
  authority or the Expert cannot approve. Waiting never approves.
- Requests appear on Activity under **Needs you**, with a count on the tab, a pop-up on any page
  and a browser notification when allowed.

### What this protects, plainly

On this computer with full access, the Expert's instructions and Ask me first are the guard. That
is a strong habit, not a wall: a Worker with full access can read what its OS user can read once a
job runs, and on one OS user it could also read xPerfect's own private files. Always ask controls
what is sent and started; it does not limit what an approved job reads. Before any second person
is added, extraction tests must show that the owner's personal material and another person's
private work cannot be obtained by asking, by tricking or through a job. Use a sandboxed workspace
or a server for a wall.

## Channels

- A channel binding names the Expert, the channel and the people who may write (by platform
  person id and tenant). Unlisted people get no answer and appear under **Waiting to be allowed**.
- Each incoming message is accepted once by the channel's own message id and becomes a turn in
  that person's conversation, framed with who wrote, where and when.
- Every outgoing message is saved in an outbox before a package sends it: `held`, `pending`,
  `sending`, `sent`, `not_confirmed`, `failed`, `dropped`. A send with no outcome after two
  minutes becomes `not_confirmed`, is shown to the owner and is never resent by itself.
- While xPerfect's disk guard holds runs (free disk under `WPR_HOST_MIN_AVAILABLE_DISK_MB`), an Expert cannot
  answer: Activity and each channel card say so plainly, with how many messages wait. They are answered
  once space returns; nothing is sent meanwhile.
- Turning a channel off stops its package and its jobs, drops its unsent messages and withdraws its
  waiting requests; accepted work stays visible. An approval never brings back a dropped message.
- Channel packages live beside the core and use only the channel routes:
  `/v1/channels/{id}/package`, `/heartbeat`, `/inbound`, `/deliveries/claim`, `/deliveries/{delivery}`.

### Microsoft Teams

`integrations/teams` uses Microsoft's Teams SDK for Python (`microsoft-teams-apps` 2.2.1).

- **On this computer it uses Teams socket mode**: the bot dials out to Microsoft, so nothing on this
  computer is reachable from the internet and no tunnel is needed. Socket mode is marked
  experimental in the SDK and Microsoft recommends HTTP for production.
- **On a server** the package serves Teams' HTTP endpoint with `--http-port`.
- **Connect** on the Expert's Channels wraps the pinned Teams developer CLI (`@microsoft/teams.cli`
  3.1.0): the owner completes Microsoft's sign-in on Microsoft's own page with the code shown;
  the CLI creates a Teams-managed bot in socket mode and writes its secret to a private file in the
  state folder; the signed-in owner becomes the first allowed person; one package process runs per
  connected channel and survives runtime restarts. **Install in Teams** follows; **Test in Teams**
  only once a first message has come through (hurdle 9 below). The first version sends and receives text in personal chats; a message from a group chat or
  a team channel gets no answer, so no reply is read by people the owner did not list. Files and group chats are
  later capabilities; Teams file requests need a certificate, federated credential or managed
  identity (Microsoft, 20 November 2026).

#### What Microsoft and Teams ask, and what xPerfect does

Found in the first connection to a real company tenant (24 November 2026). Each wall is said before
the person meets it, and xPerfect clears what Microsoft lets it clear.

1. **The work account is signed in elsewhere.** The sign-in shows a code to enter on Microsoft's own
   page, which works from any browser or a phone; the page says so.
2. **Microsoft may ask for a phone approval** in the middle of the sign-in. The page says so before
   it starts; the code lasts about 15 minutes, and Connect is offered again when it runs out.
3. **A brand-new bot is refused for its first seconds.** Microsoft answered 403, then 401 ("Socket Mode
   negotiate failed"), until the new registration had reached every region; the SDK treats both as
   final. The package tries again for about two minutes with growing waits, claims nothing and does
   not report itself online before the socket has held, and the card says **Connecting…**. A refusal
   that lasts is shown once, in Microsoft's words, until the socket holds. Restarts of a package that
   keeps exiting wait 5 seconds, doubling to 5 minutes; the owner's own action starts it at once.
4. **Teams asks web or desktop, and the add window may not appear the first time.** The card says
   either works and to press **Install in Teams** again.
5. **Teams does not let the person add custom apps.** Teams shows the add button greyed out and
   "Permissions needed. Ask your IT admin to add … to this team/chat/meeting." Two switches decide it:
   **Upload custom apps** in the person's app setup policy (Teams admin center → Teams apps → Setup
   policies; a new policy assigned to one person changes nothing for anyone else) and **Let users
   interact with custom apps in preview** (Teams apps → Manage apps → Actions → Org-wide app
   settings). Before creating anything, Connect reads Microsoft's own report of both (the CLI's
   status) and, when either is off, creates nothing: the page names the switches and which is off,
   offers a note ready to send to the administrator (a first visit to the admin center can take up to
   30 minutes), and reads the report again by itself every two minutes. While a change spreads,
   Microsoft's servers answer differently (no, no, yes, no within seconds, more than twenty minutes
   after the change), so after a "no" Connect goes on only after three "yes" in a row spanning a minute,
   saying meanwhile that Microsoft is still applying it. Teams refused the install a minute after a
   single "yes" (whether Teams also lags behind a steady "yes" is not known yet), so the card says
   "permissions needed" may mean a change is still spreading.
6. **What xPerfect cannot do with this sign-in.** The pinned CLI's sign-in asks only for Microsoft
   Graph `Application.ReadWrite.All` and the Developer Portal's `AppDefinitions.ReadWrite`. Assigning a
   policy (`teamsPolicyUserAssignment: assign`, documented for meeting policies), publishing to the
   organisation's catalogue (`POST /appCatalogs/teamsApps`) and installing for a person
   (`POST /users/{id}/teamwork/installedApps`) each need a permission it does not ask for, two of them
   an administrator's. The catalogue route needs an app registration of xPerfect's own with an
   administrator's consent; it is a later capability, so today an administrator turns the switch.
7. **The account that matters is not always the one expected.** Another login on the same computer
   may belong to another organisation (the CLI also reads the Azure command-line login). Connect
   always takes the Teams-managed route, never another login, and the page shows the account and
   organisation ID Microsoft reported, while the app is made and on the card afterwards.
8. **Online is not ready.** The card says **Online · no message yet** until a first message has really
   come through, with the steps to add the app and write in its own chat; only then **Connected**.
9. **"Test in Teams" can open somebody else's chat.** Before the app was installed, the direct link to
   a chat with the bot opened an existing chat with another person. It is offered only once a first
   message has come through, with a line saying whose chat it opens and to send nothing in any other.
10. **Reading the Connect status never starts anything.** A finished sign-in creates the app once,
    however often the page reads the status.

## Nothing happens twice

- A job that has started is never started again by itself; it resumes or is reported.
- Stopping a job stops its coordinator conversation with the coordinator's durable Stop: nothing in
  it starts again, also after a restart, and other jobs are untouched. A finished Worker run wakes
  its job at once instead of waiting for the next pass over every conversation.
- A backup is another AI on this computer's sign-in. It is the conversation's armed fallback: the
  provider switches to it only before the reply starts, while the main route is in a quota or
  rate-limit cool-down. The owner sees each switch, and each reply a limit stopped with no backup,
  under Recent on Activity. Saving a backup the runtime cannot honour is refused: the same AI on a
  second account, or a main or backup on a connected account (the armed fallback switches the AI,
  not the account).
- A message delivered twice by a channel makes one turn. An outgoing message with an unknown outcome
  is never resent by itself.
- Backups, restores and upgrades review the Expert and channel tables like the other optional
  parts. A restored copy marks unsent or sending messages `not_confirmed`, so it never sends them
  again; it does not carry a channel's app secret, so a channel is connected again on a new machine.

## Known limits

- Skills run only in a Claude Code Worker on this computer with full access.
- A conversation's AI and account are fixed when it starts; instructions, effort and Ask me first
  follow the newest version at each turn.
- One sign-in runs one Worker of its AI at a time; parallel Workers need several AI types or accounts.
- A backup on a second account of the same AI is a later capability. It needs the armed fallback to
  carry an account and cool-downs kept per account; on macOS a named second Claude account also
  needs local named-account support.
- Slack, files in Teams, group chats and approving from inside Teams are later capabilities.
- The Teams app's public details (developer name, website, privacy and terms links) are still the
  CLI's placeholders; Microsoft requires all four (HTTPS links, a name of at most 32 characters).
- A Teams app is created for any organisation's sign-in (the CLI's default); creating it for one
  organisation (`--sign-in-audience myOrg`) waits until a fresh connection proves it with socket mode.
- A blocked Connect's re-check lives in memory: after a runtime restart, Connect is pressed again.
- Requests, approvals and outgoing messages carry text only: a file an Expert produces cannot yet be
  shown for approval or sent through Teams, and a message that is only a file is not read.
