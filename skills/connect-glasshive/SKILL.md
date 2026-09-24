---
name: connect-glasshive
description: Connect Codex or Claude Code to a user's hosted xPerfect account through the deployment's official OAuth-protected MCP URL, verify the connection, and use owner-scoped workspaces, schedules, accounts, connections, and Library capabilities. Use when a user asks an AI client to connect to xPerfect or GlassHive, manage their xPerfect workspaces through MCP, or prepare a skill/tool/connector for human approval.
---

# Connect xPerfect

The `connect-glasshive` skill and plugin identifiers remain available for compatibility.

xPerfect is one remote MCP integration. This skill is only the short usage guide; do not create a
second protocol, plugin, OAuth helper, callback listener, or token flow.

If xPerfect is already connected and its tools are callable, skip setup and verification. Go
straight to the user's outcome and call only the one tool needed for the user's request. Never
enumerate or summarize the tool catalog unless the user explicitly asks for it.
Seeing a xPerfect MCP tool in the current session is sufficient proof that it is connected. Do not
inspect config files, run shell checks, or repeat setup before using it.

## Connect once

1. Ask the user to open **Connections → Use xPerfect from another AI app → Automatic** in their
   signed-in xPerfect site and paste the copied instruction here.
2. Follow only the section for the client you are currently running. Never configure the other
   client. If the named server already exists, reuse it instead of creating a duplicate.
3. For Codex, add or update the supplied native MCP config exactly, including its persistent
   `scopes` values; this keeps ordinary Reconnect on the server's OAuth resource and keeps the login
   renewable. Restart the Codex/ChatGPT desktop app once after changing that config, then use the
   client's native sign-in exactly as instructed. Never construct an authorization URL,
   inspect or copy tokens, or open the displayed callback address yourself.
4. Verify with one `workspace_list` call only during first setup or reconnect verification.
5. If native sign-in fails, report the visible client or identity-provider error and stop. Return to
   the same xPerfect panel; do not improvise another auth flow.

When a workspace opens a provider-owned authorization or installation page, first make sure the
browser is signed in to the same personal AI account selected for that worker. A success message in
another account is not proof; verify the connection with one read-only use from that worker.

## Use directly

Call only the MCP tool needed for the requested outcome:

- list saved workspaces: `workspace_list`
- start new work: `workspace_launch`
- rename or copy: `workspace_rename` or `workspace_duplicate`
- inspect or continue: `workspace_status` or `workspace_continue`
- inspect accounts, connected services, or reusable capabilities only when asked:
  `worker_accounts_list`, `connections_list`, or `library_list`

When the user asks to add, connect, configure, or use a capability **inside a xPerfect workspace**,
put that outcome in `workspace_launch` or `workspace_continue` and let the workspace handle its own
native setup. Do not install that capability in the controlling AI client, and do not inspect
accounts, Connections, Library, or the tool catalog unless the user separately asked about those
surfaces. Use a short, natural workspace name on the first line and preserve the complete request
in the remaining description/context. Set `favorite=true` when the user asked to favorite or pin
the reusable workspace. If the workspace is still running and the user asked you to
wait, repeat only `workspace_wait`; when `workspace_launch` returns `follow_up_context`, pass its
returned `run_id` and `worker_id` to every wait call. Omit those ids only when the launch returned
no follow-up context. Do not explore other tools while it works.

When the user names xPerfect or a saved xPerfect workspace, do not substitute the controlling
client's own apps for the workspace. Send the requested outcome to that workspace.

When the user asks to reuse a saved workspace by its human name, call `workspace_launch` with that
exact name on the first line and `reuse_existing_workspace=true`. xPerfect resolves one exact
owner-scoped match itself. Use `workspace_list` only if the name is missing or ambiguous.

Use the human names and stable ids returned by xPerfect. Keep the user's goal intact and let the
workspace decide its own execution plan. If a tool returns a browser confirmation URL, show it and
wait for the signed-in user; never claim approval happened before xPerfect confirms it.

## Source and truth

xPerfect is open source under Apache License 2.0. Use the live repository and its MCP publication
guide as the implementation source of truth:

- <https://github.com/xPerfectAI/xPerfect>
- <https://github.com/xPerfectAI/xPerfect/blob/main/docs/04_MCP_Publication_and_Client_Compatibility.md>
- <https://github.com/xPerfectAI/xPerfect/blob/main/runtime_phase1/README.md#curated-library-registry>
