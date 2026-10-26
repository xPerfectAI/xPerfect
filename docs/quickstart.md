# Your first useful result

[Visual guide](https://www.xperfect.ai/docs/) · [Guide source](user-guide/README.md)

Choose a professional, give them a job and useful background, then open the work they deliver.
This guide uses xPerfect on your computer. If someone hosts it for you,
[sign in to their server](#using-a-hosted-server) and start at step 3.

## 1. Start xPerfect

You need macOS or Linux, Python 3.11 or newer, [uv](https://docs.astral.sh/uv/getting-started/installation/)
and an installed AI tool: [Codex](https://developers.openai.com/codex/cli/),
[Claude Code](https://code.claude.com/docs/en/setup) or [Grok](https://github.com/xai-org/grok-build).
The bundled experts currently use **Claude Code on the host**. Internet access is needed for
setup and connected model requests. [Other installation routes](deployment.md).

From the source folder:

```sh
./xperfect start
```

The first start asks you to choose an unlock password. Press Enter to have one made; save it in
your password manager. Open the printed address, normally **http://127.0.0.1:8780**, enter that
password and select **Unlock**. This browser stays unlocked for 30 days.

`./xperfect doctor` reports app health, installed AI tools and their exact configured models.
Your saved work lives outside the checkout.

## 2. Connect an AI account

Open **Connections**, choose the AI you want to use, and follow its sign-in steps. Use an existing
Claude sign-in when offered; otherwise connect a subscription and complete the provider's own
sign-in. Wait for **Ready** before running a job. A running page is not yet a connected AI.

Grok also needs an exact supported model under **Grok model → Use model**. For provider-specific
steps and tested versions, see [account setup](host-setup.md). The app password, your AI account
and an external MCP client's connection are separate things.

## 3. Choose an expert and give them a job

Open **Run Project**.

1. In **Experts**, choose a profession. **What's included** shows its starting knowledge and
   abilities. **My experts** holds your saved setups; **Create my own** opens the normal worker
   choices without changing your defaults.
2. In **What's the Job?**, explain the work. **What's the required outcome?** is optional; use
   it for anything the result must include.
3. In **Required Knowledge**, add **Background Context**, use **Add files**, or **Choose from
   Library**. This is the worker's onboarding material. Naming a file in the job does not upload it.
4. Check the worker's account and **Where should I work?**. For a first local job, keep a new
   **My own workspace**. Then select **Run Project**.
5. If xPerfect asks you to review included native abilities, inspect them for this worker and
   confirm the selection. Optional tools keep their own account and setup requirements.

**Try a useful example:** choose **Finance Director** and use the
[synthetic company material, data and job](workshop.md#prepare-the-example).
You will ask for a performance/cash review with editable calculations. The
[example's checks](workshop.md#inspect-the-result) show what to inspect after the run;
they are not a claim that any generated file is correct without review.

If you cancel the abilities review, your job and worker stay available for retry. If startup
remains unclear, check **Workspaces** before sending the same job again.

## 4. Watch, steer and open the result

Watch names the selected worker and shows its progress and latest output. Send a message to
redirect that worker. To add a follow-up without interrupting, hold **Send** or use
**Cmd/Ctrl+Enter**. Use **Pause**, **Resume** or **Continue** when offered; **More** contains the
other actions available for that workspace. These controls do not affect other workspaces.

If the native AI asks for an answer or permission, respond in its visible controls. An unanswered
request can expire and stop that run. Check the recorded status before trying again.

When work is delivered, use **Open result**, **Open file** or **Download file**. **Library → Files**
shows the workspace's other files. Open the actual file and check it against the supplied sources.
Reload: the saved result remains available. Stopping or restarting xPerfect keeps saved work,
but does not guarantee that interrupted execution continues by itself.

![A completed synthetic Finance job, with Open file and Download file beside the delivered report](assets/screens/finance-delivery.jpg)

## 5. Use the same work again

| You want to… | Do this |
| --- | --- |
| Continue the same work | Open its active worker from **Workspaces** and send a follow-up. Its files and native history stay with it. If it is Closed, use **Duplicate** to start fresh work. |
| Reuse its expertise for a new job | Use **Workspaces → More → Save as expert**, then choose it under **Run Project → My experts**. This creates a new worker; it does not resume the old one. |
| Make a separate copy | Use **Run Project → New → Copy an existing worker**. Check the copied account and files before running. A copy does not copy browser sign-ins or chat history. |
| Keep a useful file | In **Library → Files**, use the file menu's **Keep for reuse**. This saves the selected version privately in **My Library**. |
| Add more material | Use **Library → Files → Add files**, or select saved Library items for the worker. A source link alone does not prove a native tool is installed. |
| Run it later | Open **Schedules → New schedule → Run a task**, select the workspace, job and time, then **Create schedule**. |
| Wake another app later | Open **Schedules → New schedule → Send to an app**, choose or connect its receiving address, add the request and time, then **Create schedule**. This needs no AI account. |

Removing a saved Library item does not remove the workspace's copy. See
[Library behavior](07_Minimal_Unified_Operator_UI.md#one-library-across-the-app).

### Schedule work or send a request to an app

**Run a task** uses a saved worker; **Send to an app** sends a scheduled request without an AI account.
Follow the [schedule guide](../runtime_phase1/README.md#generic-worker-and-webhook-schedules) for the
full setup, delivery check and eight useful examples, including Slack reminders, n8n, Make and your own app.

### Manage experts and workspaces

| You want to… | Do this |
| --- | --- |
| Improve a saved expert | In **Run Project → My experts**, open its **Manage → Edit**. Save a new version. Existing workers keep their original setup. **Versions** lets you inspect earlier setups. |
| Make another expert | Choose **Manage → Copy**. Give the separate expert its own name. |
| Put an expert aside | Choose **Archive**, or confirm **Remove**. Select **Archived** or **Removed**, then **Restore**, to make it available again. |
| Remove a workspace from your list | In **Workspaces**, choose its **More → Remove** and read the confirmation. Removal stops current and scheduled work; files, results and history remain saved. |
| Bring that workspace back | Select **Removed → Restore**. It returns **Closed**. Use **Duplicate** for a fresh worker; Restore does not restart work. |

Removed work, including one-off workspaces, is kept for recovery. Removing its card does not
permanently delete its files or free that storage. **Close** stops compute but leaves the card in
your list. [Lifecycle details](03_Bootstrap_Auth_and_Identity_Projection.md#saved-expert-and-workspace-catalog-lifecycle).

## Other ways to work

**Chat** can answer directly or use workers, depending on your request and configured AI.
Choose the existing project when you want its workers and context. Supported local host accounts
can use Chat without a Linux container. The page checks actual deployment and tool availability;
if a route is unavailable it offers the next action. Capacity still limits simultaneous work.
[Several goals](capability-matrix.md#working-on-several-goals).

**MCP and API** use the existing workers, workspaces and results. In **Connections → Use xPerfect
from another AI app**, follow the generated setup for your client. For local stdio, xPerfect runs
as the same OS user; the local HTTP MCP route is `http://127.0.0.1:8767/mcp` and needs its private
token. Never put tokens in URLs, screenshots or Git. [Developer walkthrough](developer.md).

## Using a hosted server

Open the operator's app address and sign in with the account they admitted; you do not need a
local xPerfect installation. Connect your own supported AI account in **Connections**, wait for
**Ready**, then follow the worker and result steps above. Available worker types, execution
locations and skills depend on that server's setup. The built-in expert packages currently
declare the local host Claude Code route; do not assume every deployment runs those skills.

An external AI client's connection is separate from a worker's provider sign-in. Hosted client
setup is offered only when the operator registered the complete connection. If unavailable, ask
the operator to fix it; do not copy credentials or disable approval to bypass it.
[Hosted setup and its evidence limits](deployment-hosted.md).

## If something goes wrong

| What you see | Next step |
| --- | --- |
| An AI account is not Ready | Open **Connections** and complete the reported sign-in or setup. xPerfect does not silently choose another account. |
| Provider usage limit or rejection | Follow that provider's message. Check the exact worker's recorded state before retrying. |
| Queued or waiting | Check **Workspaces**. Account limits or machine capacity can delay a start; avoid submitting duplicate work. |
| A file/storage error | Check the named file and storage limit. Correct the cause, then retry the upload. |
| Shared workspace unavailable | On a Mac, choose **My own workspace**. Shared execution needs a configured Linux host. |
| Missing or failed result | Inspect the worker's status and **Library → Files** first. A completed process is not proof of a correct deliverable. |
| Native stop was not confirmed | Wait for the run to end, then use **Verify** in Connections. Do not treat an unconfirmed stop as success. |
| `doctor` reports a problem | Read its named private log and fix that cause. [Setup and recovery](host-setup.md). |

## Current support boundaries

File drag-and-drop still has open browser acceptance; use **Add files** and **Download**.
One native Codex API-key path completed setup, work, a download and reload with an
account-supported model. Other native API-key paths and GPT-6.1 API access remain unverified;
see [account requirements](host-setup.md). A copy can retain the same
owner's Ready, compatible selected account; it does not clone credentials, browser sign-ins or
native chat history. Hosted identity and shared workspace acceptance remain deployment-specific. [Current limits](../README.md#current-limits) · [Capability guide](capability-matrix.md).

Local workers run with your computer user's permissions; they are not isolated merely because
they have separate workspaces. Connected AI providers can receive task content. Use the same
`--state-dir` for every launcher command if you selected one.
[Deployment and recovery](deployment.md) · [Security](../SECURITY.md).
