# xPerfect UI

A separate operator UI for xPerfect.

Start the full local app with [`./xperfect start`](../../docs/host-setup.md). This guide describes
the UI service by itself for development and hosted operators. It talks to the runtime over HTTP.

## Purpose

- one centered project composer
- automatic redirect into live watch mode
- minimal ribbon controls
- full-screen sandbox watch surface
- result-first delivery for webpage/app tasks
- exact live session still available as a secondary takeover surface
- account setup controls are generated from the server's supported provider methods; unavailable
  providers are not presented as disabled actions
- a sole ready personal account is selected with fail-closed personal-only launch policy by default
- enterprise `/r/{ref}` handoffs reuse the authenticated browser session, recheck tenant and owner,
  and return an expired session to sign-in instead of exposing a raw proxy-auth error

## Current Behavior

- page/app prompts default to the desktop watch surface
- successful webpage deliverables are promoted into the sandbox browser automatically
- the top ribbon shows the latest result and lets the operator expand it without leaving the watch screen
- the exact attached terminal session is still available from the menu

Clipboard note:

- desktop takeover currently relies on noVNC clipboard support and browser clipboard permissions
- copy/paste is available, but a custom always-on browser clipboard bridge is not yet implemented

## Run

```bash
cd frontends/glass-drive-ui
uv sync
uv run uvicorn glass_drive_ui.server:app --host 127.0.0.1 --port 8780 --no-access-log
```

Env:

- `GLASSHIVE_RUNTIME_BASE_URL` default: `http://127.0.0.1:8766`
- `GLASSHIVE_DEFAULT_OWNER_ID` default: `demo-owner`
- `WPR_API_TOKEN` optional: service token used by enterprise UI calls to the runtime
- `GLASSHIVE_SIGNED_LINK_SECRET` optional: HMAC secret for bounded signed watch
  links; defaults to `WPR_API_TOKEN` when omitted
- `GLASSHIVE_LINK_REF_TTL_SECONDS` default: `0`, meaning `/r/{ref}` short links do not expire;
  use a positive number of seconds only when user-facing short refs should expire
- `GLASSHIVE_LINK_REF_STATE_PATH` default: `<state-root>/glasshive/link_refs.sqlite3`; must match
  the runtime process when runtime-generated View / Steer links point at this UI service
- `GLASSHIVE_MAX_WATCH_SESSION_DURATION_S` default: `0`, meaning no UI-enforced active watch
  session cap; enterprise deployments should set this when forgotten tabs must be closed
- Production service managers must disable raw HTTP access logs or use a sanitizer that redacts
  `gh_token`, bearer tokens, service-token headers, and artifact signed-link paths before logs leave
  the VM.

## Hosted human authentication

Hosted multi-user xPerfect uses the configured OIDC provider for both organization SSO and, when
the provider supports it, provider-hosted email/password login. Deployments may also opt into a
separate xPerfect-local password for an already-approved OIDC identity. The local option is off by
default, never changes the issuer + immutable-subject ownership key shared with MCP, and exposes no
public sign-up, invitation, or self-service reset route.

- `GLASSHIVE_HUMAN_AUTH_MODE=oidc` enables the existing Authorization Code + PKCE gateway.
- `GLASSHIVE_PROVIDER_EMAIL_LOGIN=true` truthfully labels the same provider redirect as supporting
  email or organization login. It does not enable an xPerfect password form.
- `GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT=false` keeps first-login principal creation closed.
- `GLASSHIVE_LOCAL_PASSWORD_LOGIN=true` shows the local email/password form in addition to OIDC.
- `GLASSHIVE_OIDC_LOGIN_VISIBLE=false` hides the ordinary OIDC button only when local-password login
  is enabled. This is presentation-only: OIDC endpoints and the issuer + subject identity namespace
  remain intact, and the gateway refuses a configuration with no visible browser login method.
- `GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY` is a gateway-only random secret of at least 32 bytes used to
  HMAC source-rate-limit keys; it is required when local password login is enabled and must remain
  stable across restarts. It does not bypass principal preapproval or add an MCP password grant.
- `GLASSHIVE_LOCAL_AUTH_ALLOWED_EMAIL_DOMAINS` optionally limits local credential locators without
  changing OIDC/MCP admission. Never reuse email as a principal or workspace ownership key.
- `GLASSHIVE_ALLOW_EMAIL_LOGIN` and `GLASSHIVE_ALLOW_EMAIL_REGISTRATION` remain compatibility
  aliases for one release; new deployments should use the canonical keys above.

With enrollment closed, an administrator preapproves the provider's exact immutable subject. From
`frontends/glass-drive-ui`, run the admin module under the gateway's configured identity and
environment, with input from a private file or file descriptor. Do not put the JSON in command
arguments or shell history:

```bash
umask 077
uv run python -m glass_drive_ui.auth_admin preapprove-oidc --stdin-json < /private/preapproved-user.json
```

The JSON object contains `subject`, optional display-only `email` and `display_name`, and `role`.
Output contains only the opaque xPerfect user ID. Delete the exact temporary input after the
command returns; a secret manager may instead supply an ephemeral descriptor. Never infer or merge
principals by email; issuer + subject is the durable ownership key shared with MCP.

To attach or rotate a local password, keep the feature hidden while staging if desired and have the
deployment secret manager generate a high-entropy value of at least 24 characters with at least 12
distinct characters. Send an operator-only JSON object containing the exact preapproved `subject`,
a `login_email`, and that password through standard input. The wrapper never places those fields in
argv, environment, or output:

```bash
umask 077
uv run python -m glass_drive_ui.auth_admin set-local-password --stdin-json < /private/local-credential.json
```

The gateway stores only a salted Argon2id PHC verifier in its private auth database. Rotation
revokes that principal's active local-password browser sessions. `disable-local-password`,
`enable-local-password`, and `unlock-local-password` take stdin JSON containing only the exact
subject. Before disabling the feature or rolling back to an older release, run
`revoke-local-sessions`; local sessions live in a separate table, so an older OIDC-only binary
cannot accept them. OIDC sessions remain independent. Delete only the exact temporary input file
after the command succeeds.

## Public-link-only mode

Set `GLASSHIVE_PUBLIC_LINKS_ONLY=true` when this UI endpoint should expose only health/static
assets and signed workspace or artifact links, rather than the normal operator surface. This mode:

- fails at startup unless `GLASSHIVE_SIGNED_LINK_SECRET` is set
- disables `/docs`, `/redoc`, and `/openapi.json`
- rejects normal operator UI/API/runtime-proxy requests that do not carry a valid signed link
- accepts opaque `/r/{ref}` workspace references, then redirects to a tokenless watch URL and
  stores a bounded, HTTP-only worker-session cookie
- accepts opaque `/v1/link-refs/{ref}` artifact references and rejects direct
  `/v1/signed-links/{token}` proxy access

The link-ref database contains the signed token behind each opaque reference. Keep
`GLASSHIVE_LINK_REF_STATE_PATH` private and, when the runtime creates the references, point both
processes at the same state file or supported shared storage. Treat an opaque reference as a bearer
link in this mode: anyone who receives it can exercise only the worker/artifact scope encoded in
its valid signed token. Configure `GLASSHIVE_LINK_REF_TTL_SECONDS` and
`GLASSHIVE_MAX_WATCH_SESSION_DURATION_S` when links and already-open watch sessions must expire.

Public-link-only mode is a narrow link-serving boundary, not a substitute for enterprise identity
and tenant isolation. Use enterprise mode and its trusted authenticated proxy contract when access
must also be bound to a specific tenant/user. Terminate public deployments behind HTTPS so Secure
session cookies can be used.

## Test

```bash
cd frontends/glass-drive-ui
uv run pytest -q
```

## Browser dependency provenance

The UI uses native ES modules and upstream browser distributions. No local JavaScript build or
package installation is needed. Vendor files remain unmodified and ship through the existing
static service. `tests/test_vendor_provenance.py` verifies their exact SHA-256 hashes offline.

| Package | npm tarball member | Local vendor file | SHA-256 |
|---|---|---|---|
| [markdown-it 15.0.3](https://registry.npmjs.org/markdown-it/15.0.3) | `package/dist/browser/markdown-it.esm.min.mjs` | `markdown-it-15.0.3.mjs` | `d5432491790141da046b1197353705db03264f40b5727f3b4b591f5aba193e30` |
| [@xterm/xterm 5.5.0](https://registry.npmjs.org/@xterm/xterm/5.5.0) | `package/lib/xterm.js` | `xterm-5.5.0/xterm.js` | `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495` |
| @xterm/xterm 5.5.0 | `package/css/xterm.css` | `xterm-5.5.0/xterm.css` | `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` |

To reproduce a vendor update, read the exact version's npm metadata at the linked registry URL,
fetch `dist.tarball`, and verify the downloaded bytes against `dist.integrity` before extraction.
Copy only the named member to `src/glass_drive_ui/static/vendor/`; do not run an npm install or
rebuild an upstream distribution. Copy the package's `package/LICENSE` and the embedded
dependencies' license files, then update `NOTICE`, this table, the hash test and all import owners.
The upstream [15.0.3 package lock](https://github.com/markdown-it/markdown-it/blob/15.0.3/package-lock.json)
and the browser bundle's region headers identify mdurl 2.1.0, entities 8.1.0, OXC runtime 0.154.0
and Rolldown 1.2.14. Their license files are in the same vendor directory.

Markdown-it 15.0.3 fixes
[quadratic reference parsing](https://github.com/markdown-it/markdown-it/security/advisories/GHSA-gfr2-rv44-r23c),
which is reachable with the UI's HTML and typography options disabled. Both result and Chat
renderers retain those disabled options. Keep native file identities, safe links, tables and
literal HTML covered by `tests/test_result_markdown.py`; its bounded pathological-reference
check guards the affected parser path. The Python lock also pins certifi 2026.10.19, which removes
the [affected emSign trust root](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2).
These source locks do not by themselves update an installed or running service.

## Files

Kickoff and Watch share the Files control. Browser uploads use durable draft receipts and streamed raw bodies; launches send `file_upload_ids`. Upload progress, same-ID retry, removal errors and restored drafts stay visible. Files supports folders, moves, trash/Undo and streamed selection ZIP export through authenticated UI proxies. Download links contain no service token or raw host path. Native drag-out remains disabled until verified for the deployment's browser and OS.

Standalone startup reads an optional explicit `VIVENTIUM_ENV_FILE`; it does not import a Viventium checkout or Application Support environment automatically.

The old operator `/api/launch` inline-base64 `files` transport is retired. Non-empty legacy payloads receive422 with `legacy_upload_transport` before any workspace change. Clients must create streamed draft receipts and send `file_upload_ids`; the browser already uses this contract. Runtime bootstrap compatibility is separate and remains subject to the native storage policy.
