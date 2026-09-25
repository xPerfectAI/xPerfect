# Hosted xPerfect on one Linux server (OIDC, TLS, XFS quotas)

The hosted profile runs the same three containers as the local package — runtime, UI and MCP — for several people who sign in through your identity provider. Each person's files live under an XFS project quota (5,000,000,000 bytes each by default). The local single-user package is unchanged.

**Current limits.** Install, sign-in and admission, per-person storage, attaching stored files, and upgrade and rollback are tested on a hosted server. Connecting an AI account there and getting an AI result have not been verified end to end yet, and neither has a worker writing past a person's storage limit.

## What you need

- **Linux server** with rootful Docker (cgroup v2). Rootless Docker and user-namespace remapping cannot set project quotas.
- **Files disk:** a dedicated XFS filesystem mounted with `prjquota`, not `pqnoenforce`. Mount it by UUID so it returns after a reboot, for example `UUID=… /srv/xperfect-data xfs prjquota,nosuid,nodev 0 2`. Docker's own storage (control state) must be on a different filesystem.
- **OIDC provider** that issues:
  - an HTTPS issuer;
  - a confidential client for the browser, with redirect `https://<your host>/auth/oidc/callback`;
  - JWT access tokens for MCP clients, carrying your MCP audience, a `scope` claim with the MCP scope, and an `azp`/`client_id` claim.
- **TLS:** a certificate and key for the UI and MCP host names.
- **Images:** the xPerfect service and native images on that Docker host, referenced by exact image ID.

## Start

Write a private input file (mode 0600). This recipe serves TLS directly from the containers: the UI on port 443 and MCP on port 8443 of the same server.

```json
{
  "public_url": "https://xperfect.example.com",
  "mcp_public_url": "https://xperfect.example.com:8443/mcp",
  "issuer": "https://login.example.com/realms/xperfect",
  "client_id": "xperfect-ui",
  "client_secret_file": "/etc/xperfect/oidc-client-secret",
  "tenant_id": "example-company",
  "tls_certificate_file": "/etc/xperfect/tls/cert.pem",
  "tls_key_file": "/etc/xperfect/tls/key.pem",
  "xfs_mount": "/srv/xperfect-data",
  "xfs_device": "/dev/vdb",
  "bind_address": "0.0.0.0"
}
```

Optional fields:

| Field | Default | Purpose |
|---|---|---|
| `principal_claim` | `sub` | Claim holding the stable user identity |
| `mcp_audiences` | the MCP URL | Accepted `aud` values for MCP tokens |
| `mcp_scopes` | `glasshive:access` | Required MCP token scope |
| `mcp_client_ids` | `xperfect-mcp` | Accepted MCP token clients |
| `storage_limit_bytes` | 5,000,000,000 | Per-user quota |
| `shared_memory_bytes` | 6 GiB | Worker box memory |
| `extra_hosts` | none | Name resolution inside containers |
| `bind_address` | `127.0.0.1` | `0.0.0.0` accepts outside traffic; keep `127.0.0.1` only for a single-machine test |
| `tls_ca_file` | none | Only for a private test issuer |
| `models` | none | Exact model per worker type, e.g. `{"grok-build": "<ID from grok models>"}`. Grok needs one; nothing is guessed |
| `role_map` | none | Map your identity provider's role values to xPerfect roles, e.g. `{"xperfect-admins": "tenant_admin", "xperfect-users": "member"}`. See [Roles from your identity provider](#roles-from-your-identity-provider) |
| `role_claim` | `roles` | The top-level token claim that carries those values, e.g. `groups`. Only used with `role_map` |

Then run:

```
python3 deployment/linux/launch.py --profile hosted-xfs --hosted-config /etc/xperfect/hosted.json --docker-host unix:///var/run/docker.sock --name xperfect-hosted --service-image sha256:<service> --native-image sha256:<native> --ui-port 443 --mcp-port 8443 --receipt /etc/xperfect/receipt.json
```

Addresses and certificate:

- `public_url` and `mcp_public_url` must use DNS names your users' browsers resolve. `localhost`, `*.localhost` and private IP addresses are refused, because the sign-in gateway refuses them.
- The port in each URL must equal the port you publish (`--ui-port`, `--mcp-port`; a URL without a port means 443). MCP answers at `/mcp`.
- The certificate must cover both host names and be trusted by your users' browsers.
- The OIDC client's redirect is `<public_url>/auth/oidc/callback`.

The launcher checks the input file, ports, names and Docker host before it changes anything, and each problem gets a short fix message. Some problems, such as a wrong issuer or certificate, only show when the services start; the launcher then stops and reports that the services are not reachable. It creates the package this way:

- **Files volume:** the `data` volume is bound to the XFS mount root.
- **Login store:** a dedicated `auth` volume shared by UI and MCP, so one person has one identity on both.
- **Signing keys:** one key per signer (UI, MCP), created inside that signer's private volume. The runtime receives only the public keys.
- **OIDC client secret:** placed in the UI's private configuration only.
- **Privileges:** only the runtime, which never serves browsers, gets `SYS_ADMIN` and the XFS device, for quota control.
- **Restart:** every container restarts unless stopped.

It reports success only after both front doors answer from the server itself, at the exact advertised URLs: the UI's `/health`, and MCP's sign-in metadata naming `mcp_public_url`. The server must therefore resolve its own public names, for example with a hosts entry pointing them at itself.

## Admit people

Sign-in stays closed until you admit someone. Admit each person by their exact provider subject, sent through stdin:

```
echo '{"subject":"<sub from the provider>","email":"ada@example.com","role":"member"}' | python3 deployment/linux/launch.py preapprove --docker-host unix:///var/run/docker.sock --name xperfect-hosted
```

The subject is the `sub` claim (or your `principal_claim`) that your identity provider issues for that person. Your provider's user details show it.

Roles are `member`, `viewer` (read-only) and `tenant_admin`. By default the role you set here is the person's role: the identity provider proves who someone is, and signing in never changes their role. To change a role, admit the person again with the new role (with a `role_map`, change it at your provider instead). Over MCP a person acts with the lesser of their stored role and the role their sign-in token carries; without a `role_map` that is at most `member`. A `tenant_admin` can disable and re-enable people from the admin page.

### Roles from your identity provider

Set `role_map` (optional) only when your identity provider's groups or roles are the source of truth:
- **Admission** then decides only who may sign in.
- **Each browser sign-in** stores the person's mapped provider role as their role, replacing the admitted one. A sign-in without a mapped role is refused, so map a role for everyone you admit.
- **Existing browser sessions** stay valid until they expire.
- **Over MCP** a person acts with the lesser of their stored role and their token's mapped role. This is how a mapped `tenant_admin` gets administrator authority over MCP, for example to raise or restore their own storage limit. To change *another* person's limit, a `tenant_admin` uses **Connections → Team file storage** in the browser; MCP does not offer that.
- **The claim** must be a top-level role or group claim your provider controls, not a profile claim a person can edit, such as `email`. Map role or group values, not people.
- **Visibility:** nothing changes without a map, and the receipt records the mapping you set.

To set or change it on a running package, use the upgrade command with the image it already runs:

```
python3 deployment/linux/launch.py upgrade --docker-host unix:///var/run/docker.sock --receipt /etc/xperfect/receipt.json --service-image sha256:<current image> --role-map xperfect-admins=tenant_admin --role-map xperfect-users=member
```

- `--role-map` replaces the whole mapping and keeps the current claim.
- `--role-claim` names another claim.
- `--no-role-map` removes the mapping. Roles stored while it was on remain, so preapprove each person again with the role they should have.
- Before `upgrade-commit`, sign in as a mapped `tenant_admin` to confirm the mapping works. A wrong map refuses everyone; `upgrade-rollback` returns the previous configuration.

Someone you have not admitted is sent back to the sign-in page with "account not registered", and their MCP tokens are refused.

## Check it works

From any machine that trusts your certificate:

```
curl -fsS https://xperfect.example.com/health
curl -fsS https://xperfect.example.com:8443/.well-known/oauth-protected-resource/mcp
```

The first returns `"status":"ok"`. The second names your MCP URL and issuer. Then sign in as an admitted person. Their storage shows 5,000,000,000 bytes with no file, count or batch limit, unless you set `storage_limit_bytes`. Attach one small stored file to a workspace and open it there: that confirms the runtime can publish stored files.

## Connect AI accounts

Each person connects their own provider account. An account is never shared with, or copied
to, another person. This section describes the supported route; completing it on a hosted server has
not been verified end to end yet.

- **Browser:** open **Connections** and choose a provider. The browser must trust the
  deployment's certificate.
- **MCP (Codex or Claude subscriptions):**
  1. `worker_account_connect` creates the account entry.
  2. `worker_account_setup_start` starts the provider's own sign-in. For Codex this returns
     OpenAI's device page (`https://auth.openai.com/codex/device`) and a one-time code. The person
     approves it there with the ChatGPT account they want to use. This gives the Codex CLI its
     standard access: identity, offline refresh and connectors.
  3. `worker_account_test` confirms the account is ready.

  Grok sign-in is offered in Connections only.
- **Disconnect:** `worker_account_disconnect` releases the account and removes its sign-in files,
  which are kept in that person's storage.

## What refuses to start, and why

| Situation | What happens |
|---|---|
| No HTTPS issuer or principal claim, a local/default tenant, open enrollment, a static MCP key, a client secret outside the UI, or (newer images) a runtime without its stored-file key | The role refuses to start and names the problem |
| The Files path is not the XFS mount root, the mount lacks enforced project quotas, the device is missing, or control state shares the Files filesystem | The runtime refuses to admit work and says which |

## Size the server

A worker starts in a new workspace only when both of these hold, after counting every workspace
that is still starting:

- **Memory:** free Docker memory is at least `shared_memory_bytes` (6 GiB by default) plus 2 GiB.
- **Disk:** the Files disk has at least 4 GiB per starting workspace plus 4 GiB free.

The three services use memory too. On one test server with the defaults, 6 and 8 GiB of memory
with 7.6 GB free on the Files disk were refused as "resource pressure"; with 10 GiB of memory and
a 16 GiB Files disk, one worker was admitted. Lower `shared_memory_bytes` for smaller servers.

## Upgrade

```
python3 deployment/linux/launch.py upgrade --docker-host unix:///var/run/docker.sock --receipt /etc/xperfect/receipt.json --service-image sha256:<new service image>
```

The upgrade needs an idle package. A hosted upgrade cannot pause open workspaces for you, so close
them first; running, queued, paused or waiting work also refuses the upgrade. It keeps sign-ins,
roles, projects, owner files and quotas.
Check the new version, then run `upgrade-commit` to keep it or `upgrade-rollback` to go back.
Rollback discards service records made after the upgrade, including access changes, and refuses
once a new owner has received storage. See
[Upgrade a packaged Linux install](deployment.md#upgrade-a-packaged-linux-install).

If a package from an earlier launcher can't attach stored files to a workspace, run the same
upgrade with the image it already runs. That adds the runtime's missing file key and changes no
setting that is present. Then attach those files again. If that upgrade reports "Files projection
must settle", see [recovering unfinished attachments](deployment.md#upgrade-a-packaged-linux-install).

## Keep and move

Containers, volumes and keys persist across `docker restart` and reboots. Back up the `control`, `auth`, `ui-state`, `mcp-state` and `links` volumes together with the XFS filesystem.
