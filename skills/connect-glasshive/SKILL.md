---
name: connect-glasshive
description: Connect Codex or Claude Code to xPerfect (on this computer or a hosted account), verify the connection, and create, run, schedule, copy and reuse owner-scoped expert workspaces with their own files and native skills. Use when a user asks an AI client to connect to xPerfect or GlassHive, to "xPerfect this", to manage their xPerfect workspaces through MCP, or to prepare a skill/tool/connector for human approval.
---

# Connect xPerfect

The `connect-glasshive` skill and plugin identifiers remain available for compatibility.

xPerfect is one MCP integration. This skill is only the short usage guide; do not create a
second protocol, plugin, OAuth helper, callback listener, or token flow.

If xPerfect is already connected and its tools are callable, skip setup and verification. Go
straight to the user's outcome and call only the one tool needed for the user's request. Never
enumerate or summarize the tool catalog unless the user explicitly asks for it.
Seeing a xPerfect MCP tool in the current session is sufficient proof that it is connected. Do not
inspect config files, run shell checks, or repeat setup before using it.

## Connect once

1. Ask the user to open **Connections → Use xPerfect from another AI app** in their xPerfect site
   and paste the copied instruction here. Follow only the section for the client you are currently
   running. Never configure the other client. If the named server already exists, reuse it instead
   of creating a duplicate.
2. **xPerfect on this computer:** the instruction is one local command that starts this computer's
   xPerfect MCP connection (`<xPerfect folder>/xperfect mcp --state-dir <its state folder>`). Run
   exactly that command as given. There is no sign-in step.
3. **Hosted xPerfect:** for Codex, add or update the supplied native MCP config exactly, including
   its persistent `scopes` values; this keeps ordinary Reconnect on the server's OAuth resource and
   keeps the login renewable. Restart the Codex/ChatGPT desktop app once after changing that config,
   then use the client's native sign-in exactly as instructed. Never construct an authorization URL,
   inspect or copy tokens, or open the displayed callback address yourself.
4. Verify with one `workspace_list` call only during first setup or reconnect verification.
5. If connecting or native sign-in fails, report the visible client or identity-provider error and
   stop. Return to the same xPerfect panel; do not improvise another auth flow.

When a workspace opens a provider-owned authorization or installation page, first make sure the
browser is signed in to the same personal AI account selected for that worker. A success message in
another account is not proof; verify the connection with one read-only use from that worker.

## Use directly

Call only the MCP tool needed for the requested outcome:

- find saved experts and workspaces: `workspace_list`
- create a reusable expert ("xPerfect this"): `workspace_launch` with `favorite=true`
- talk to an expert: `worker_message`, or `workspace_launch` with its exact name on the first line
  and `reuse_existing_workspace=true`
- schedule an expert: `workspace_schedule` with its `workspace_alias` (a saved expert keeps its
  own AI account; do not pass an account policy when scheduling or talking to it)
- rename or copy: `workspace_rename` or `workspace_duplicate`
- check progress and results: `workspace_status`, `workspace_wait`, `workspace_artifacts`
- inspect accounts, connected services, or reusable capabilities only when asked:
  `worker_accounts_list`, `connections_list`, or `library_list`

**Create a reusable expert.** Put a short, natural expert name on the first line of the
`workspace_launch` description and keep the complete request in the remaining description/context.
Set `favorite=true`: it saves the expert as a named workspace the user can find, open and reuse.
To run it on the user's own AI subscription, set `provider_account_policy="personal_required"`;
xPerfect uses their ready default account for that harness, or the one `provider_account_id` they
chose. It never silently switches to another account. If no ready account exists, report that
and point the user to **Connections**.

**Give the expert its files and skills.** Put ordinary text files in `bootstrap_bundle_json` as
`{"files": {"relative/path": "content"}}`. A skill is an ordinary skill folder with a `SKILL.md`,
placed where that expert's own harness reads project skills: `.claude/skills/<skill>/SKILL.md` for a
Claude Code expert, `.agents/skills/<skill>/SKILL.md` for a Codex expert. Pass the files unchanged;
the expert discovers and uses its skills itself. For binary files, reserve each upload with
`file_upload_begin`; the bytes then go to the returned upload address through xPerfect's
authenticated HTTP API, and the ready upload ids are passed as `file_upload_ids`. If you cannot send
authenticated HTTP yourself, ask the user to add the file in the xPerfect site instead.

When the user asks to add, connect, configure, or use a capability **inside a xPerfect workspace**,
put that outcome in `workspace_launch` or `workspace_continue` and let the workspace handle its own
native setup. Do not install that capability in the controlling AI client, and do not inspect
accounts, Connections, Library, or the tool catalog unless the user separately asked about those
surfaces. If the workspace is still running and the user asked you to
wait, repeat only `workspace_wait`; when `workspace_launch` returns `follow_up_context`, pass its
returned `run_id` and `worker_id` to every wait call. Omit those ids only when the launch returned
no follow-up context. Do not explore other tools while it works. `workspace_continue` resumes one
earlier run of the same workspace; to ask an expert something new, use `worker_message`.

When the user names xPerfect or a saved xPerfect workspace, do not substitute the controlling
client's own apps for the workspace. Send the requested outcome to that workspace.

When the user asks to reuse a saved workspace by its human name, call `workspace_launch` with that
exact name on the first line and `reuse_existing_workspace=true`. xPerfect resolves one exact
owner-scoped match itself. Use `workspace_list` only if the name is missing or ambiguous.

A copy made with `workspace_duplicate` keeps the expert's ordinary files and its native skill
folders; credentials, sessions and history are never copied. It keeps the same personal AI account
when that account is still ready; otherwise xPerfect asks for a fresh account choice.

If a create call fails without a clear result, run `workspace_list` before trying again: a retry of
`workspace_launch` can create a second expert. Only `workspace_duplicate` and template starts take
an `idempotency_key` that makes a retry safe.

Use the human names and stable ids returned by xPerfect. Keep the user's goal intact and let the
workspace decide its own execution plan. If a tool returns a browser confirmation URL, show it and
wait for the signed-in user; never claim approval happened before xPerfect confirms it.

**Tell the user how to reach the expert later:** its saved name (for `workspace_launch` with
`reuse_existing_workspace=true` or `worker_message` from any connected AI app), and its View link,
which opens it in the xPerfect site. The same actions are available through the xPerfect HTTP API.

## Source and truth

xPerfect is open source under Apache License 2.0. Use the live repository and its MCP publication
guide as the implementation source of truth:

- <https://github.com/xPerfectAI/xPerfect>
- <https://github.com/xPerfectAI/xPerfect/blob/main/docs/04_MCP_Publication_and_Client_Compatibility.md>
- <https://github.com/xPerfectAI/xPerfect/blob/main/runtime_phase1/README.md#curated-library-registry>
