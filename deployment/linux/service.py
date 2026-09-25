"""Packaged Linux process entry: explicit runtime or UI role, no host ambient auth."""
from __future__ import annotations

import argparse
import urllib.parse
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time

ROOT = Path('/opt/xperfect')
# Each hosted signer keeps its private key in its own state volume; the runtime
# holds only the public verification set.
ROLE_STATE = {'runtime': '/control', 'ui': '/ui-state', 'mcp': '/mcp-state'}
ASSERTION_KEY = 'assertion-key.pem'
BOOTSTRAP_SOURCE_KEY = 'GLASSHIVE_BOOTSTRAP_SOURCE_SECRET'
# The local package's human-confirmation channel: the UI signs a signed-in owner's
# requests with its own key, the runtime verifies them with the public key set only,
# and MCP holds neither.
LOCAL_ASSERTION_KEYS = ('GLASSHIVE_LOCAL_HUMAN_ASSERTION', 'GLASSHIVE_INTERNAL_ASSERTION_ISSUER',
                        'GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE', 'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE',
                        'GLASSHIVE_INTERNAL_ASSERTION_KEY_ID', 'GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS',
                        'GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE', 'GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON',
                        'GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL')
FALSE_VALUES = {'', '0', 'false', 'no', 'off'}
# Settings every package of a profile carries, identical in every role: no secret,
# identity, address or operator choice. The launcher writes them into new packages,
# and an upgrade to an image carrying this table adds any that an earlier launcher
# did not write. An image without the table declares none. XPERFECT_WORKER_NETWORK
# 'isolated' declares that this image serves native tools through per-box sockets,
# so its workers bridge refuses traffic between containers.
PROFILE_SETTINGS = {
    'local-linux': {
        'GLASSHIVE_SECURITY_MODE': 'local',
        'GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION': 'per_worker_container',
        'GLASSHIVE_ENABLE_NATIVE_API_KEYS': '1',
        'GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS': '1',
        'GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH': '1',
        'XPERFECT_WORKER_NETWORK': 'isolated',
    },
    'hosted-xfs': {
        'GLASSHIVE_SECURITY_MODE': 'multi_user',
        'GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION': 'per_worker_container',
        'GLASSHIVE_ENABLE_NATIVE_API_KEYS': '1',
        'GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS': '1',
        'GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH': '1',
        'GLASSHIVE_AUTH_MODE': 'signed_internal_assertion',
        'GLASSHIVE_ENTERPRISE_MODE': 'true',
        'GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE': 'xperfect-runtime',
        'GLASSHIVE_PRINCIPAL_ID_FORMAT': 'hashed_issuer_subject',
        'GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT': 'false',
        'XPERFECT_WORKER_NETWORK': 'isolated',
    },
}


def _https(value: str) -> bool:
    """A public HTTPS URL, matching the multi-user OIDC gateway's own guard."""
    try:
        parsed = urllib.parse.urlsplit(str(value or '').strip())
        _ = parsed.port
    except ValueError:
        return False
    host = str(parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme != 'https' or not host or parsed.username or parsed.password:
        return False
    if host == 'localhost' or host.endswith('.localhost'):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host)).is_global  # IPv4 shorthand
    except OSError:
        return True


def _validate_hosted(configured: dict[str, str], role: str) -> None:
    tenant = configured.get('GLASSHIVE_ENTERPRISE_TENANT_ID', '')
    if not tenant or tenant in {'local', 'default'}:
        raise ValueError('Hosted services require a deployment tenant')
    if not _https(configured.get('GLASSHIVE_OIDC_ISSUER', '')) or not configured.get('GLASSHIVE_OIDC_PRINCIPAL_CLAIM'):
        raise ValueError('Hosted services require an HTTPS OIDC issuer and principal claim')
    if configured.get('GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT', 'false').strip().lower() not in FALSE_VALUES:
        raise ValueError('Hosted enrollment stays closed; preapprove users instead')
    if configured.get('GLASSHIVE_MCP_API_KEY'):
        raise ValueError('Hosted MCP uses OAuth, not a static key')
    if role != 'ui' and configured.get('GLASSHIVE_OIDC_CLIENT_SECRET'):
        raise ValueError('Only the UI receives the OIDC client secret')
    if role in {'ui', 'mcp'}:
        key = configured.get('GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE', '')
        if key != ROLE_STATE[role] + '/' + ASSERTION_KEY or not configured.get('GLASSHIVE_INTERNAL_ASSERTION_KEY_ID'):
            raise ValueError('Each hosted signer requires its own key in its private state')
    if role == 'runtime' and not configured.get('GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE', '').startswith('/control/'):
        raise ValueError('Hosted runtime requires its public verification key set')
    if role != 'runtime' and configured.get(BOOTSTRAP_SOURCE_KEY):
        raise ValueError('Only the runtime receives the stored-file key')
    if role == 'ui' and not (configured.get('GLASSHIVE_OIDC_CLIENT_ID')
                             and _https(configured.get('GLASSHIVE_OIDC_REDIRECT_URI', ''))):
        raise ValueError('Hosted UI requires an OIDC client and HTTPS redirect')
    if role == 'mcp' and not (_https(configured.get('GLASSHIVE_MCP_OAUTH_ISSUER', ''))
                              and _https(configured.get('GLASSHIVE_MCP_PUBLIC_URL', ''))):
        raise ValueError('Hosted MCP requires public HTTPS issuer and MCP URLs')
    if role == 'mcp' and not all(configured.get(name) for name in (
            'GLASSHIVE_MCP_OAUTH_ISSUER', 'GLASSHIVE_MCP_PUBLIC_URL', 'GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES',
            'GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES', 'GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS')):
        raise ValueError('Hosted MCP requires its OAuth issuer, public URL, audiences, scopes and clients')


def _validate_local_assertion(configured: dict[str, str], role: str) -> None:
    """The UI alone signs, from its own private state; the runtime alone verifies; MCP neither."""
    present = {key for key in LOCAL_ASSERTION_KEYS if configured.get(key)}
    if role == 'mcp' and present:
        raise ValueError('MCP must hold no sign-in assertion keys')
    if role == 'runtime' and configured.get('GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE'):
        raise ValueError('Runtime must not receive the identity signing key')
    if role == 'ui' and ({'GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON', 'GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL'} & present):
        raise ValueError('The UI signs; only the runtime verifies')
    if not present:
        return
    if configured.get('GLASSHIVE_LOCAL_HUMAN_ASSERTION') != '1' or not (
            configured.get('GLASSHIVE_INTERNAL_ASSERTION_ISSUER') and configured.get('GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE')):
        raise ValueError('The local sign-in assertion channel is incomplete')
    if role == 'ui' and (configured.get('GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE') != ROLE_STATE['ui'] + '/' + ASSERTION_KEY
                         or not configured.get('GLASSHIVE_INTERNAL_ASSERTION_KEY_ID')):
        raise ValueError('The local UI signer requires its own key in its private state')
    if role == 'runtime' and (not configured.get('GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE', '').startswith('/control/')
                              or configured.get('GLASSHIVE_HUMAN_AUTH_MODE') != 'local_password'):
        raise ValueError('The local runtime requires its public verification key set and password sign-in')


def load_environment(config_path: Path, role: str) -> dict[str, str]:
    if role not in {'runtime', 'ui', 'mcp'}:
        raise ValueError('Invalid service role')
    info = config_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError('Packaged service configuration must be a private regular file')
    value = json.loads(config_path.read_text())
    if set(value) != {'profile', 'environment'} or value['profile'] not in {'local-linux', 'hosted-xfs'}:
        raise ValueError('Packaged execution profile is required')
    configured = value['environment']
    if not isinstance(configured, dict) or any(not isinstance(k, str) or not isinstance(v, str) or '\0' in v for k, v in configured.items()):
        raise ValueError('Packaged environment must contain string values')
    environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', **configured}
    environment['XPERFECT_EXECUTION_PROFILE'] = value['profile']
    environment['VIVENTIUM_ENV_FILE'] = ''
    environment['GLASSHIVE_HOST_WORKERS_ENABLED'] = '0'
    if not configured.get('WPR_API_TOKEN'):
        raise ValueError('Packaged service authentication is required')
    if value['profile'] == 'hosted-xfs':
        if (configured.get('GLASSHIVE_SECURITY_MODE') != 'multi_user'
                or configured.get('GLASSHIVE_AUTH_MODE') != 'signed_internal_assertion'):
            raise ValueError('Hosted services require signed multi-user identity')
        if role == 'ui' and configured.get('GLASSHIVE_HUMAN_AUTH_MODE') != 'oidc':
            raise ValueError('Hosted UI requires OIDC')
        if role == 'runtime' and configured.get('GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE'):
            raise ValueError('Runtime must not receive the identity signing key')
        _validate_hosted(configured, role)
    elif configured.get('GLASSHIVE_SECURITY_MODE', 'local') != 'local':
        raise ValueError('Local profile requires local identity configuration')
    if value['profile'] == 'local-linux':
        owner = configured.get('GLASSHIVE_DEFAULT_OWNER_ID')
        if not owner or configured.get('WPR_DEFAULT_OWNER_ID') != owner:
            raise ValueError('Local package roles require the same fixed owner')
        if role == 'ui' and configured.get('GLASSHIVE_HUMAN_AUTH_MODE') != 'local_password':
            raise ValueError('Local package UI requires password authentication')
        if role == 'mcp' and (not configured.get('GLASSHIVE_MCP_API_KEY')
                or configured['GLASSHIVE_MCP_API_KEY'] == configured['WPR_API_TOKEN']):
            raise ValueError('MCP requires a distinct client credential')
        _validate_local_assertion(configured, role)
    if role == 'runtime':
        environment.update(WPR_DB_PATH='/control/runtime.db', WPR_RUNTIME_BACKEND='openclaw',
            WPR_DEFAULT_EXECUTION_MODE='docker', GLASSHIVE_DEFAULT_EXECUTION_MODE='docker',
            GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE='/data',
            GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT='/data/provider-accounts',
            XPERFECT_SHARED_VOLUME_ROOT='/data', XPERFECT_CONTROL_ROOT='/control',
            GLASSHIVE_PEER_RUNTIME_BASE_URL='http://runtime:8766')
        if configured.get('XPERFECT_SHARED_IMAGE'):
            environment['WPR_SANDBOX_IMAGE'] = configured['XPERFECT_SHARED_IMAGE']
        if value['profile'] == 'hosted-xfs':
            environment.update(XPERFECT_STORAGE_ROOT='/data',
                               XPERFECT_STORAGE_REGISTRY_PATH='/control/storage/registry.sqlite3')
        elif any(key in configured for key in ('XPERFECT_STORAGE_ROOT', 'XPERFECT_STORAGE_REGISTRY_PATH')):
            raise ValueError('Ordinary local storage cannot claim XFS quota enforcement')
    elif role == 'ui':
        environment.update(GLASSHIVE_RUNTIME_BASE_URL='http://runtime:8766',
            GLASSHIVE_AUTH_STATE_PATH='/ui-state/auth.sqlite3',
            GLASSHIVE_WATCH_SESSION_STATE_PATH='/ui-state/watch-sessions.sqlite3')
    if value['profile'] == 'hosted-xfs' and role in {'ui', 'mcp'}:
        # One login store keeps a user's browser and MCP identity the same;
        # MCP updates its rows, so it is shared through a dedicated auth volume.
        environment['GLASSHIVE_AUTH_STATE_PATH'] = '/auth/auth.sqlite3'
        if role == 'mcp':
            environment.update(GLASSHIVE_MCP_TLS_CERT_FILE='/mcp-state/tls/cert.pem',
                               GLASSHIVE_MCP_TLS_KEY_FILE='/mcp-state/tls/key.pem')
    # Every role mints signed short links that the UI resolves and the runtime
    # revokes, so they share the one reference store on the dedicated links volume.
    environment['GLASSHIVE_LINK_REF_STATE_PATH'] = '/links/link-refs.sqlite3'
    return environment


def commands(role: str, profile: str = 'local-linux') -> list[list[str]]:
    if role == 'ui':
        tls = (['--ssl-certfile', '/ui-state/tls/cert.pem', '--ssl-keyfile', '/ui-state/tls/key.pem']
               if profile == 'hosted-xfs' else [])
        return [[str(ROOT / 'venvs/ui/bin/python'), '-m', 'uvicorn', 'glass_drive_ui.server:create_app',
                 '--factory', '--host', '0.0.0.0', '--port', '8780', '--no-access-log', *tls]]
    python = str(ROOT / 'venvs/runtime/bin/python')
    if role == 'mcp':
        return [[python, '-m', 'workers_projects_runtime.mcp_server', '--transport', 'streamable-http',
                 '--base-url', 'http://runtime:8766', '--host', '0.0.0.0', '--port', '8767']]
    return [[python, '-m', 'uvicorn', 'workers_projects_runtime.api:create_app', '--factory',
             '--host', '0.0.0.0', '--port', '8766', '--no-access-log']]


KEYGEN = r'''
import json, os, sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
state, kid = sys.argv[1], sys.argv[2]
key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
fd = os.open(os.path.join(state, 'assertion-key.pem'), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as stream:
    stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
jwk.update(kid=kid, alg='RS256', use='sig')
print(json.dumps(jwk))
'''


def keygen(argv: list[str]) -> int:
    """Create one signer's private key in its state; print only the public JWK."""
    parser = argparse.ArgumentParser(prog='keygen')
    parser.add_argument('--state', choices=('/ui-state', '/mcp-state'), required=True)
    parser.add_argument('--kid', required=True)
    args = parser.parse_args(argv)
    if not args.kid.replace('-', '').isalnum() or len(args.kid) > 80:
        raise ValueError('Signing key ID must be a short token')
    os.umask(0o077)
    python = str(ROOT / 'venvs/runtime/bin/python')
    os.execve(python, [python, '-I', '-c', KEYGEN, args.state, args.kid], {'PATH': '/usr/bin:/bin'})


def require_serving(env: dict[str, str], role: str) -> None:
    """What a role needs to serve, beyond a readable configuration.

    Upgrade and continuity helpers also load a package's configuration, including one
    an earlier launcher wrote, so this is checked only when a service starts.
    """
    # Stored files reach a workspace only through a source signed with the runtime's own key.
    if (env.get('XPERFECT_EXECUTION_PROFILE') == 'hosted-xfs' and role == 'runtime'
            and not env.get(BOOTSTRAP_SOURCE_KEY, '').strip()):
        raise ValueError('Hosted runtime requires its stored-file key; run the upgrade with the image it '
                         'already runs to add it')


def main() -> int:
    if sys.argv[1:2] == ['keygen']:
        return keygen(sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument('role', choices=('runtime', 'ui', 'mcp'))
    parser.add_argument('--config', type=Path, default=Path('/run/xperfect/config.json'))
    parser.add_argument('--provision-local-owner', action='store_true')
    parser.add_argument('--admin', choices=('preapprove-oidc',))
    args = parser.parse_args()
    os.umask(0o077)
    env = load_environment(args.config, args.role)
    if args.provision_local_owner:
        if args.role != 'ui' or env['XPERFECT_EXECUTION_PROFILE'] != 'local-linux':
            raise ValueError('Local provisioning is a UI-only administrative operation')
        python = str(ROOT / 'venvs/ui/bin/python')
        os.execve(python, [python, '-m', 'glass_drive_ui.auth_admin', 'provision-local-owner', '--stdin-json'], env)
    if args.admin:
        # Hosted enrollment stays closed; an operator admits one exact OIDC
        # subject at a time. The identity arrives on stdin, never in argv.
        if args.role != 'ui' or env['XPERFECT_EXECUTION_PROFILE'] != 'hosted-xfs':
            raise ValueError('OIDC preapproval is a hosted UI administrative operation')
        python = str(ROOT / 'venvs/ui/bin/python')
        os.execve(python, [python, '-m', 'glass_drive_ui.auth_admin', args.admin, '--stdin-json'], env)
    require_serving(env, args.role)
    stopping = False
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = []
    try:
        for command in commands(args.role, env['XPERFECT_EXECUTION_PROFILE']):
            children.append(subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True))
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(.2)
        return 0 if stopping else 1
    finally:
        # This controls trusted API/UI processes only. Native container recovery
        # remains the durable runtime/account owner's responsibility.
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        for child in children:
            try:
                child.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == '__main__':
    raise SystemExit(main())
