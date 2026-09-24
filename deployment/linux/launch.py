"""Launch one local Linux package on an explicitly selected Docker endpoint.

Creates private named volumes and separate frontend/native bridges. Existing
resources are never replaced; restart uses the existing container identities.
Hosted XFS provisioning requires the separately verified host storage contract.
"""
from __future__ import annotations

import argparse
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import sys


def docker(endpoint: str, *args: str, data: bytes | None = None):
    result = subprocess.run(['docker', '--host', endpoint, *args], input=data,
                            capture_output=True, timeout=90)
    if result.returncode:
        # CLI stderr can include operator paths/configuration. It is retained
        # by the caller only on explicit investigation, not printed by default.
        raise RuntimeError('Docker package operation failed: ' + args[0])
    return result.stdout.decode().strip()


# Exact native model per worker profile. This mirrors the runtime profile
# registry's model_environment (the launcher cannot import the runtime); a
# test keeps the two identical. There is no default: absent means missing.
MODEL_ENVIRONMENTS = {
    'codex-cli': 'WPR_MODEL_CODEX_CLI',
    'claude-code': 'WPR_MODEL_CLAUDE_CODE',
    'openclaw-general': 'WPR_MODEL_OPENCLAW_GENERAL',
    'openclaw': 'WPR_MODEL_OPENCLAW_GENERAL',
    'openclaw-codex': 'WPR_MODEL_OPENCLAW_CODEX',
    'openclaw-claude': 'WPR_MODEL_OPENCLAW_CLAUDE',
    'openclaw-desktop': 'WPR_MODEL_OPENCLAW_DESKTOP',
    'grok-build': 'WPR_MODEL_GROK_BUILD',
}


def _packaged_service():
    """The packaged service entry module next to this file (standard library only)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('xperfect_packaged_service', Path(__file__).with_name('service.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The settings every package of a profile carries; the image ships the same table.
PROFILE_SETTINGS = _packaged_service().PROFILE_SETTINGS
# Per-package random secrets, by profile and the only role that holds each. A hosted
# runtime signs and checks the stored-file source it copies into a workspace with its
# own key, apart from every bearer and link secret. New packages get it at launch; an
# upgrade adds one to a package an earlier launcher wrote without it.
PROFILE_SECRETS = {'local-linux': {}, 'hosted-xfs': {'runtime': ('GLASSHIVE_BOOTSTRAP_SOURCE_SECRET',)}}


def new_secrets(profile: str, role: str, names=None) -> dict[str, str]:
    return {name: secrets.token_urlsafe(48) for name in PROFILE_SECRETS[profile].get(role, ())
            if names is None or name in names}


LOCAL_ASSERTION_AUDIENCE = 'xperfect-local-runtime'
LOCAL_ASSERTION_KEY = '/ui-state/assertion-key.pem'
LOCAL_ASSERTION_JWKS = '/control/assertion-jwks.json'


def local_assertion_environment(role: str, *, kid: str, ui_url: str) -> dict[str, str]:
    """The local package's human-confirmation channel, as the host launcher sets it up:
    the UI signs a signed-in owner's requests, the runtime verifies them, MCP neither."""
    if role == 'mcp':
        return {}
    environment = {'GLASSHIVE_LOCAL_HUMAN_ASSERTION': '1',
                   'GLASSHIVE_INTERNAL_ASSERTION_ISSUER': ui_url.rstrip('/') + '/local-assertion',
                   'GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE': LOCAL_ASSERTION_AUDIENCE}
    if role == 'ui':
        environment.update({'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE': LOCAL_ASSERTION_KEY,
                            'GLASSHIVE_INTERNAL_ASSERTION_KEY_ID': kid,
                            'GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS': '30'})
    else:
        environment.update({'GLASSHIVE_HUMAN_AUTH_MODE': 'local_password',
                            'GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE': LOCAL_ASSERTION_JWKS})
    return environment


def local_assertion_status(ui: dict, runtime: dict, mcp: dict) -> str | None:
    """The key ID of a complete local channel, None when absent; ValueError when partial."""
    keys = _packaged_service().LOCAL_ASSERTION_KEYS
    if not any(ui.get(key) or runtime.get(key) for key in keys) and not any(mcp.get(key) for key in keys):
        return None
    kid = str(ui.get('GLASSHIVE_INTERNAL_ASSERTION_KEY_ID') or '')
    if (any(mcp.get(key) for key in keys) or runtime.get('GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE')
            or not kid or ui.get('GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE') != LOCAL_ASSERTION_KEY
            or runtime.get('GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE') != LOCAL_ASSERTION_JWKS
            or any(ui.get(key) != runtime.get(key) for key in ('GLASSHIVE_LOCAL_HUMAN_ASSERTION',
                   'GLASSHIVE_INTERNAL_ASSERTION_ISSUER', 'GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE'))
            or ui.get('GLASSHIVE_LOCAL_HUMAN_ASSERTION') != '1'):
        raise ValueError('The local sign-in assertion channel is incomplete or misplaced')
    return kid


def generate_signer(endpoint: str, image: str, volume: str, state: str, kid: str) -> dict:
    """Create one signer's private key inside its own state volume; return only its public key."""
    jwk = json.loads(docker(endpoint, 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                            '--security-opt', 'no-new-privileges', '--mount',
                            f'type=volume,src={volume},dst={state},volume-nocopy',
                            image, 'keygen', '--state', state, '--kid', kid))
    if jwk.get('kid') != kid or 'd' in jwk:
        raise RuntimeError('Signer key generation returned an unexpected key')
    return jwk


def validate_models(value: object) -> dict[str, str]:
    """Return {profile: exact model}; the model ID is kept byte-for-byte."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('models must map a worker profile to its exact model ID')
    models = {}
    for profile, model in value.items():
        if profile not in MODEL_ENVIRONMENTS:
            raise ValueError(f'Unknown worker profile for a model: {profile}')
        # The runtime accepts 1-180 characters without spaces or control characters.
        if not isinstance(model, str) or not 0 < len(model) <= 180 or any(ord(c) < 33 for c in model):
            raise ValueError(f'The {profile} model must be one exact model ID without spaces')
        models[profile] = model
    names = [MODEL_ENVIRONMENTS[profile] for profile in models]
    if len(names) != len(set(names)):
        raise ValueError('Two profile names set the same model setting; choose one')
    return models


def parse_model_arguments(items: list[str] | None) -> dict[str, str]:
    """Parse repeated --model PROFILE=MODEL values."""
    parsed = {}
    for item in items or []:
        profile, separator, model = str(item).partition('=')
        if not separator or profile in parsed:
            raise ValueError('Use --model PROFILE=EXACT_MODEL once per profile, e.g. --model grok-build=<model>')
        parsed[profile] = model
    return validate_models(parsed)


def model_environment(models: dict[str, str]) -> dict[str, str]:
    return {MODEL_ENVIRONMENTS[profile]: model for profile, model in models.items()}


def role_create_args(*, profile: str, name: str, role: str, volumes: dict, networks: dict,
                     publish: str = '', extra_hosts: dict | None = None, device: str = '') -> list[str]:
    """One container shape per role, shared by launch and upgrade."""
    hosted = profile == 'hosted-xfs'
    state = 'control' if role == 'runtime' else role + '-state'
    args = ['create', '--name', name + '-' + role, '--label', 'xperfect.package=' + name,
            '--label', 'xperfect.role=' + role, '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges']
    if hosted:
        args += ['--restart', 'unless-stopped']
    args += ['--network', networks['frontend'], '--network-alias', role,
             '--mount', f'type=volume,src={volumes[state]},dst=/{state},volume-nocopy',
             '--mount', f'type=volume,src={volumes["links"]},dst=/links,volume-nocopy',
             '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777']
    if hosted:
        args += [item for host, address in (extra_hosts or {}).items() for item in ('--add-host', f'{host}:{address}')]
    if role == 'runtime':
        args += ['--cap-add', 'CHOWN', '--cap-add', 'FOWNER', '--cap-add', 'DAC_OVERRIDE']
        if hosted:
            # Project quotas need quotactl on the exact XFS device; this is the
            # only role with that privilege, and it never serves browsers.
            args += ['--cap-add', 'SYS_ADMIN', '--device', f'{device}:{device}:r']
        args += ['--mount', f'type=volume,src={volumes["data"]},dst=/data,volume-nocopy',
                 '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock']
    else:
        args += ['--publish', publish]
        if hosted:
            args += ['--mount', f'type=volume,src={volumes["auth"]},dst=/auth,volume-nocopy']
    return args


def role_command(role: str) -> list[str]:
    state = 'control' if role == 'runtime' else role + '-state'
    return [role, '--config', f'/{state}/config.json']


def archive_config(config: dict) -> bytes:
    payload = json.dumps(config).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        item = tarfile.TarInfo('config.json')
        item.mode = 0o600
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))
    return output.getvalue()


def _wait_until_runnable(endpoint: str, containers: dict[str, str], ui_port: int, *,
                         scheme: str = 'http', context: ssl.SSLContext | None = None,
                         host: str = '127.0.0.1', timeout_s: float = 45.0) -> None:
    """Do not publish a package receipt until its services are actually usable."""
    deadline = time.monotonic() + timeout_s
    while True:  # at least one check, even with a zero timeout
        try:
            if all(docker(endpoint, 'inspect', '--format', '{{.State.Running}}', container) == 'true'
                   for container in containers.values()):
                with urllib.request.urlopen(f'{scheme}://{host}:{ui_port}/health', timeout=2,
                                            **({'context': context} if context else {})) as response:
                    if 200 <= response.status < 300:
                        return
        except (RuntimeError, OSError, urllib.error.URLError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Packaged services did not reach a runnable health state')
        time.sleep(0.5)


def launch(*, endpoint: str, name: str, image: str, native_image: str,
           ui_port: int, mcp_port: int, credentials, owner_id: str = "local-owner",
           models: dict[str, str] | None = None) -> dict:
    models = validate_models(models)
    if not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', name):
        raise ValueError('Package name must start with xperfect-')
    if not endpoint.startswith('unix:///'):
        raise ValueError('Select an explicit local Unix Docker endpoint')
    for identity in (image, native_image):
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', identity):
            raise ValueError('Exact locally verified image identities are required')
    if not (1024 <= ui_port <= 65535 and 1024 <= mcp_port <= 65535 and ui_port != mcp_port):
        raise ValueError('Two distinct unprivileged loopback ports are required')
    if not owner_id or len(owner_id) > 512 or any(ord(char) < 32 for char in owner_id):
        raise ValueError('A stable local owner ID is required')
    info = json.loads(docker(endpoint, 'info', '--format', '{{json .}}'))
    if info.get('OSType') != 'linux' or str(info.get('CgroupVersion')) != '2':
        raise ValueError('Linux Docker with cgroup v2 is required')
    for identity in (image, native_image):
        if docker(endpoint, 'image', 'inspect', '--format', '{{.Id}}', identity) != identity:
            raise ValueError('Image identity could not be verified')
    # 'links' holds only the signed short-link store that every role mints into and
    # the UI resolves; private auth, control and data state stay on their own volumes.
    volumes = {role: name + '-' + role for role in ('data', 'control', 'ui-state', 'mcp-state', 'links')}
    networks = {role: name + '-' + role for role in ('frontend', 'workers')}
    containers = {role: name + '-' + role for role in ('runtime', 'ui', 'mcp')}
    # A single run owns only fresh names. Failure retains all created state for
    # inspection/recovery; it never deletes data or adopts an existing resource.
    for kind, planned in (('volume', volumes.values()), ('network', networks.values()), ('container', containers.values())):
        existing = set(docker(endpoint, kind, 'ls', *(['-a'] if kind == 'container' else []), '--format', '{{.Name}}' if kind != 'container' else '{{.Names}}').splitlines())
        if existing.intersection(planned):
            raise ValueError('Package resources already exist; use their recorded identities to restart')
    for volume in volumes.values():
        docker(endpoint, 'volume', 'create', '--label', 'xperfect.package=' + name, volume)
    for network in networks.values():
        docker(endpoint, 'network', 'create', '--driver', 'bridge', '--label', 'xperfect.package=' + name, network)
    password, mcp_key = secrets.token_urlsafe(36), secrets.token_urlsafe(48)
    # The UI signs a signed-in owner's confirmations; its key never leaves its own state.
    ui_url = f'http://127.0.0.1:{ui_port}'
    kid = 'xperfect-local-ui-' + secrets.token_hex(8)
    jwks = json.dumps({'keys': [generate_signer(endpoint, image, volumes['ui-state'], '/ui-state', kid)]}).encode()
    common = {**PROFILE_SETTINGS['local-linux'],
              'GLASSHIVE_DEFAULT_OWNER_ID': owner_id, 'WPR_DEFAULT_OWNER_ID': owner_id,
              'WPR_API_TOKEN': secrets.token_urlsafe(48),
              'GLASSHIVE_SIGNED_LINK_SECRET': secrets.token_urlsafe(48),
              # Watch and file links returned to host clients open on the published UI.
              'GLASSHIVE_OPERATOR_BASE_URL': f'http://127.0.0.1:{ui_port}'}
    runtime = {**common, 'XPERFECT_SHARED_VOLUME_NAME': volumes['data'],
               'XPERFECT_SHARED_IMAGE': native_image, 'WPR_BOOTSTRAP_SOURCE_ROOTS': '/data/managed-files', 'XPERFECT_SHARED_NETWORK': networks['workers'],
               'XPERFECT_SHARED_MEMORY_BYTES': str(6 * 1024**3), 'XPERFECT_SHARED_PIDS_LIMIT': '512',
               **model_environment(models)}
    environments = {'runtime': runtime,
                    'ui': {**common, 'GLASSHIVE_HUMAN_AUTH_MODE': 'local_password',
                           'GLASSHIVE_LOCAL_AUTH_NAMESPACE': secrets.token_hex(16),
                           'GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY': secrets.token_urlsafe(48)},
                    'mcp': {**common, 'GLASSHIVE_MCP_API_KEY': mcp_key}}
    for role, environment in environments.items():
        environment.update(local_assertion_environment(role, kid=kid, ui_url=ui_url))
    # Owner-only local output. No browser or service secret enters the ordinary
    # receipt, argv, environment, logs or URL. Only the password verifier is stored
    # by the UI provisioning process, whose input arrives through stdin.
    json.dump({'owner_id': owner_id, 'ui_password': password, 'mcp_api_key': mcp_key,
               'ui_url': f'http://127.0.0.1:{ui_port}', 'mcp_url': f'http://127.0.0.1:{mcp_port}/mcp'}, credentials)
    credentials.flush()
    os.fsync(credentials.fileno())
    receipt = {'profile': 'local-linux', 'service_image': image, 'native_image': native_image,
               'containers': {}, 'volumes': volumes, 'networks': networks, 'models': models,
               'signing_key_ids': {'ui': kid}}
    for role, container in containers.items():
        state = 'control' if role == 'runtime' else role + '-state'
        state_target = '/' + state
        port, target = (ui_port, 8780) if role == 'ui' else (mcp_port, 8767)
        args = role_create_args(profile='local-linux', name=name, role=role, volumes=volumes, networks=networks,
                                publish=f'127.0.0.1:{port}:{target}')
        identity = docker(endpoint, *args, image, *role_command(role))
        receipt['containers'][role] = identity
        if role == 'runtime':
            environments[role]['XPERFECT_CONTROLLER_ID'] = identity
            docker(endpoint, 'cp', '-', identity + ':' + state_target,
                   data=_file_archive('assertion-jwks.json', jwks, 0o644))
        docker(endpoint, 'cp', '-', identity + ':' + state_target,
               data=archive_config({'profile': 'local-linux', 'environment': environments[role]}))
        if role == 'ui':
            docker(endpoint, 'run', '--rm', '-i', '--network', 'none', '--read-only',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--mount', f'type=volume,src={volumes[state]},dst={state_target},volume-nocopy',
                   image, 'ui', '--config', state_target + '/config.json', '--provision-local-owner',
                   data=json.dumps({'password': password}).encode())
        if role == 'runtime':
            docker(endpoint, 'network', 'connect', '--alias', 'runtime', networks['workers'], identity)
        docker(endpoint, 'start', identity)
    receipt['ui_url'] = f'http://127.0.0.1:{ui_port}'
    receipt['mcp_url'] = f'http://127.0.0.1:{mcp_port}/mcp'
    _wait_until_runnable(endpoint, containers, ui_port)
    return receipt


# The admission record owns roles; the identity provider only proves identity.
HOSTED_ROLES = {'member', 'viewer', 'tenant_admin'}
HOSTED_REQUIRED = {'public_url', 'mcp_public_url', 'issuer', 'client_id', 'client_secret_file', 'tenant_id',
                   'tls_certificate_file', 'tls_key_file', 'xfs_mount', 'xfs_device'}
HOSTED_OPTIONAL = {'principal_claim', 'mcp_audiences', 'mcp_scopes', 'mcp_client_ids', 'storage_limit_bytes',
                   'shared_memory_bytes', 'extra_hosts', 'bind_address', 'tls_ca_file', 'models',
                   'role_claim', 'role_map'}
# The sign-in gateway's existing identity-provider role settings, given to the UI and MCP only.
ROLE_ENVIRONMENT = ('GLASSHIVE_OIDC_ROLE_CLAIM', 'GLASSHIVE_OIDC_ROLE_MAP_JSON')
# A top-level token claim (nested paths are not read). Claims a person can edit themselves
# would let them choose their own role.
ROLE_CLAIM_NAME = re.compile(r'[A-Za-z0-9_-]{1,128}')
USER_EDITABLE_CLAIMS = {'sub', 'email', 'preferred_username', 'name', 'given_name', 'family_name', 'nickname', 'upn'}


def validate_role_mapping(claim: object = None, mapping: object = None) -> dict | None:
    """An operator's explicit map from identity-provider role values to xPerfect roles, or None.

    Off by default: each person's admitted role (preapproval or the admin page) is their
    role. With a map, admission only decides who may sign in: each browser sign-in stores
    the provider's mapped role as the person's role, a sign-in without a mapped role is
    refused, and MCP acts with the lesser of the stored and token roles. A mapped tenant
    administrator can then use the administrator routes, such as restoring or raising their
    storage limit.
    """
    if mapping is None:
        if claim is not None:
            raise ValueError('role_claim needs a role_map')
        return None
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('role_map must map at least one identity-provider role value to member, viewer or tenant_admin')
    clean = {}
    for value, role in mapping.items():
        if (not isinstance(value, str) or not 0 < len(value) <= 256 or value != value.strip() or '@' in value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise ValueError('role_map keys must be exact identity-provider role or group values, not people')
        if not isinstance(role, str) or role not in HOSTED_ROLES:
            raise ValueError('role_map values must be member, viewer or tenant_admin')
        clean[value] = role
    name = 'roles' if claim is None else claim
    if not isinstance(name, str) or not ROLE_CLAIM_NAME.fullmatch(name):
        raise ValueError('role_claim must be a top-level claim name such as roles or groups')
    if name.lower() in USER_EDITABLE_CLAIMS:
        raise ValueError('role_claim must be a role or group claim your provider controls, not a profile claim')
    return {'claim': name, 'map': dict(sorted(clean.items()))}


def role_environment(mapping: dict | None) -> dict[str, str]:
    if not mapping:
        return {}
    return {'GLASSHIVE_OIDC_ROLE_CLAIM': mapping['claim'],
            'GLASSHIVE_OIDC_ROLE_MAP_JSON': json.dumps(mapping['map'], sort_keys=True, separators=(',', ':'))}


def role_mapping_of(environment: dict) -> dict | None:
    """The role mapping a role's configuration carries, validated, or None."""
    if not any(name in environment for name in ROLE_ENVIRONMENT):
        return None
    try:
        mapping = json.loads(environment.get('GLASSHIVE_OIDC_ROLE_MAP_JSON') or '{}')
    except ValueError:
        raise ValueError('A role configuration has an unreadable identity-provider role map') from None
    if not mapping:
        return None
    return validate_role_mapping(environment.get('GLASSHIVE_OIDC_ROLE_CLAIM', 'roles'), mapping)


def _local_host(host: str) -> bool:
    """Names the multi-user sign-in gateway refuses, including IPv4 shorthand."""
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)  # 127.1, 2130706433, 0x7f.0.0.1 ...
    except OSError:
        return False
    return not ipaddress.ip_address(packed).is_global


def _https_url(value: object, name: str, *, origin: bool = False, exact: bool = False) -> str:
    """One canonical spelling: lowercase host, no default :443, no trailing slash.

    ``exact`` keeps the operator's spelling (an OIDC issuer must equal the
    provider's ``iss`` claim) after the same checks.
    """
    text = str(value or '').strip().rstrip('/')
    parsed = urllib.parse.urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f'{name} has an invalid port') from None
    if parsed.scheme.lower() != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f'{name} must be an HTTPS URL')
    if origin and parsed.path:
        raise ValueError(f'{name} must be an origin without a path')
    host = parsed.hostname.lower().rstrip('.')
    if _local_host(host):
        # The multi-user sign-in gateway refuses these; fail here, before Docker.
        raise ValueError(f'{name} must use a DNS name your users resolve, not localhost or a private address')
    if exact:
        return text
    netloc = f'[{host}]' if ':' in host else host
    if port and port != 443:
        netloc += f':{port}'
    return f'https://{netloc}{parsed.path}'


def _port(url: str) -> int:
    return urllib.parse.urlsplit(url).port or 443


def _tokens(value: object, name: str) -> list[str]:
    items = value if isinstance(value, list) else str(value or '').split()
    items = [str(item).strip() for item in items if str(item).strip()]
    if not items or any(not re.fullmatch(r'[A-Za-z0-9:/._@+-]{1,200}', item) for item in items):
        raise ValueError(f'{name} requires plain token values')
    return items


def _private_file(value: object, name: str) -> Path:
    path = Path(str(value or ''))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f'{name} must be an existing regular file')
    if name != 'tls_certificate_file' and name != 'tls_ca_file' and path.stat().st_mode & 0o077:
        raise ValueError(f'{name} must be private (mode 0600)')
    return path


def validate_hosted(source: dict) -> dict:
    """Fail before any Docker mutation, with a short remedy for each input."""
    if not isinstance(source, dict) or HOSTED_REQUIRED - set(source) or set(source) - HOSTED_REQUIRED - HOSTED_OPTIONAL:
        raise ValueError('Hosted input has missing or unknown fields; see docs/deployment-hosted.md')
    value = dict(source)
    value['public_url'] = _https_url(value['public_url'], 'public_url', origin=True)
    value['mcp_public_url'] = _https_url(value['mcp_public_url'], 'mcp_public_url')
    if urllib.parse.urlsplit(value['mcp_public_url']).path != '/mcp':
        raise ValueError('mcp_public_url must end in /mcp, where the MCP service answers')
    value['issuer'] = _https_url(value['issuer'], 'issuer', exact=True)
    value['client_id'] = _tokens(value['client_id'], 'client_id')[0]
    value['tenant_id'] = _tokens(value['tenant_id'], 'tenant_id')[0]
    if value['tenant_id'] in {'local', 'default'}:
        raise ValueError('tenant_id must name this deployment, not local/default')
    value['principal_claim'] = _tokens(value.get('principal_claim', 'sub'), 'principal_claim')[0]
    value['mcp_audiences'] = _tokens(value.get('mcp_audiences', [value['mcp_public_url']]), 'mcp_audiences')
    value['mcp_scopes'] = _tokens(value.get('mcp_scopes', ['glasshive:access']), 'mcp_scopes')
    value['mcp_client_ids'] = _tokens(value.get('mcp_client_ids', ['xperfect-mcp']), 'mcp_client_ids')
    for name in ('client_secret_file', 'tls_certificate_file', 'tls_key_file') + (('tls_ca_file',) if value.get('tls_ca_file') else ()):
        value[name] = str(_private_file(value[name], name))
    limit = value.get('storage_limit_bytes', 5_000_000_000)
    if type(limit) is not int or limit < 4096:
        raise ValueError('storage_limit_bytes must be at least one allocation block')
    value['storage_limit_bytes'] = limit
    memory = value.get('shared_memory_bytes', 6 * 1024**3)
    if type(memory) is not int or memory < 512 * 1024**2:
        raise ValueError('shared_memory_bytes must be at least 512 MiB')
    value['shared_memory_bytes'] = memory
    for name in ('xfs_mount', 'xfs_device'):
        if not str(value[name]).startswith('/') or '..' in str(value[name]).split('/'):
            raise ValueError(f'{name} must be an absolute daemon-host path')
    hosts = value.get('extra_hosts', {})
    if not isinstance(hosts, dict) or any(not re.fullmatch(r'[a-z0-9.-]{1,253}', h) or not re.fullmatch(r'[a-z0-9.:-]{1,64}', str(a)) for h, a in hosts.items()):
        raise ValueError('extra_hosts must map host names to addresses or host-gateway')
    value['extra_hosts'] = hosts
    value['bind_address'] = str(value.get('bind_address', '127.0.0.1'))
    if value['bind_address'] not in {'127.0.0.1', '0.0.0.0'}:
        raise ValueError('bind_address must be 127.0.0.1 or 0.0.0.0')
    value['models'] = validate_models(value.get('models'))
    value['role_mapping'] = validate_role_mapping(value.pop('role_claim', None), value.pop('role_map', None))
    return value


def _tls_archive(certificate: bytes, key: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, payload, mode in (('tls', None, 0o700), ('tls/cert.pem', certificate, 0o644), ('tls/key.pem', key, 0o600)):
            item = tarfile.TarInfo(name)
            item.mode = mode
            if payload is None:
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            else:
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
    return output.getvalue()


def _file_archive(name: str, payload: bytes, mode: int = 0o600) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        item = tarfile.TarInfo(name)
        item.mode = mode
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))
    return output.getvalue()


def launch_hosted(*, endpoint: str, name: str, image: str, native_image: str,
                  ui_port: int, mcp_port: int, hosted: dict, ready_timeout_s: float = 45.0) -> dict:
    """Compose the hosted OIDC/TLS/XFS profile from the same three roles."""
    if not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', name):
        raise ValueError('Package name must start with xperfect-')
    if not endpoint.startswith('unix:///'):
        raise ValueError('Select an explicit local Unix Docker endpoint')
    for identity in (image, native_image):
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', identity):
            raise ValueError('Exact locally verified image identities are required')
    if not (1 <= ui_port <= 65535 and 1 <= mcp_port <= 65535 and ui_port != mcp_port):
        raise ValueError('Two distinct ports are required')
    value = validate_hosted(hosted)
    # The containers serve TLS directly, so each advertised URL must name the
    # port that is actually published; no proxy is assumed.
    if _port(value['public_url']) != ui_port:
        raise ValueError(f'public_url port ({_port(value["public_url"])}) must equal --ui-port ({ui_port})')
    if _port(value['mcp_public_url']) != mcp_port:
        raise ValueError(f'mcp_public_url port ({_port(value["mcp_public_url"])}) must equal --mcp-port ({mcp_port})')
    info = json.loads(docker(endpoint, 'info', '--format', '{{json .}}'))
    if info.get('OSType') != 'linux' or str(info.get('CgroupVersion')) != '2':
        raise ValueError('Linux Docker with cgroup v2 is required')
    if any('rootless' in str(option) or 'userns' in str(option) for option in info.get('SecurityOptions') or []):
        raise ValueError('Hosted XFS quotas require rootful Docker without user-namespace remapping')
    for identity in (image, native_image):
        if docker(endpoint, 'image', 'inspect', '--format', '{{.Id}}', identity) != identity:
            raise ValueError('Image identity could not be verified')
    volumes = {role: name + '-' + role for role in ('data', 'control', 'ui-state', 'mcp-state', 'links', 'auth')}
    networks = {role: name + '-' + role for role in ('frontend', 'workers')}
    containers = {role: name + '-' + role for role in ('runtime', 'ui', 'mcp')}
    for kind, planned in (('volume', volumes.values()), ('network', networks.values()), ('container', containers.values())):
        existing = set(docker(endpoint, kind, 'ls', *(['-a'] if kind == 'container' else []), '--format', '{{.Name}}' if kind != 'container' else '{{.Names}}').splitlines())
        if existing.intersection(planned):
            raise ValueError('Package resources already exist; use their recorded identities to restart')
    client_secret = Path(value['client_secret_file']).read_text().strip()
    if not client_secret:
        raise ValueError('client_secret_file is empty')
    tls = _tls_archive(Path(value['tls_certificate_file']).read_bytes(), Path(value['tls_key_file']).read_bytes())
    for role, volume in volumes.items():
        if role == 'data':
            # The quota-owning XFS mount root itself backs Files/workspaces; a
            # subdirectory would not match the mount the runtime verifies.
            docker(endpoint, 'volume', 'create', '--driver', 'local', '--opt', 'type=none', '--opt', 'o=bind',
                   '--opt', 'device=' + value['xfs_mount'], '--label', 'xperfect.package=' + name, volume)
        else:
            docker(endpoint, 'volume', 'create', '--label', 'xperfect.package=' + name, volume)
    for network in networks.values():
        docker(endpoint, 'network', 'create', '--driver', 'bridge', '--label', 'xperfect.package=' + name, network)
    signers = {}
    for role in ('ui', 'mcp'):
        signers[role] = generate_signer(endpoint, image, volumes[role + '-state'], '/' + role + '-state',
                                        f'xperfect-{role}-' + secrets.token_hex(8))
    origin, mcp_url = value['public_url'], value['mcp_public_url']
    mcp_host = urllib.parse.urlsplit(mcp_url).netloc
    common = {**PROFILE_SETTINGS['hosted-xfs'], 'WPR_API_TOKEN': secrets.token_urlsafe(48),
              'GLASSHIVE_ENTERPRISE_TENANT_ID': value['tenant_id'],
              'GLASSHIVE_SIGNED_LINK_SECRET': secrets.token_urlsafe(48),
              'GLASSHIVE_OPERATOR_BASE_URL': origin, 'GLASSHIVE_PUBLIC_BASE_URL': origin,
              'GLASSHIVE_INTERNAL_ASSERTION_ISSUER': origin + '/internal',
              'GLASSHIVE_OIDC_ISSUER': value['issuer'], 'GLASSHIVE_OIDC_PRINCIPAL_CLAIM': value['principal_claim'],
              'GLASSHIVE_OWNER_STORAGE_BYTES': str(value['storage_limit_bytes'])}
    runtime = {**common, 'XPERFECT_SHARED_VOLUME_NAME': volumes['data'],
               'XPERFECT_SHARED_IMAGE': native_image, 'WPR_BOOTSTRAP_SOURCE_ROOTS': '/data/owners', 'XPERFECT_SHARED_NETWORK': networks['workers'],
               'XPERFECT_SHARED_MEMORY_BYTES': str(value['shared_memory_bytes']), 'XPERFECT_SHARED_PIDS_LIMIT': '512',
               'GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE': '/control/assertion-jwks.json',
               **model_environment(value['models'])}
    environments = {
        'runtime': runtime,
        'ui': {**common, 'GLASSHIVE_HUMAN_AUTH_MODE': 'oidc', 'GLASSHIVE_OIDC_CLIENT_ID': value['client_id'],
               'GLASSHIVE_OIDC_CLIENT_SECRET': client_secret,
               'GLASSHIVE_OIDC_REDIRECT_URI': origin + '/auth/oidc/callback',
               'GLASSHIVE_OIDC_POST_LOGOUT_REDIRECT_URI': origin,
               'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE': '/ui-state/assertion-key.pem',
               'GLASSHIVE_INTERNAL_ASSERTION_KEY_ID': signers['ui']['kid']},
        'mcp': {**common, 'GLASSHIVE_MCP_OAUTH_ISSUER': value['issuer'], 'GLASSHIVE_MCP_PUBLIC_URL': mcp_url,
                'GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES': ' '.join(value['mcp_audiences']),
                'GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES': ' '.join(value['mcp_scopes']),
                'GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES': ' '.join(value['mcp_scopes']),
                'GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS': ' '.join(value['mcp_client_ids']),
                'GLASSHIVE_MCP_ALLOWED_HOSTS': mcp_host,
                'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE': '/mcp-state/assertion-key.pem',
                'GLASSHIVE_INTERNAL_ASSERTION_KEY_ID': signers['mcp']['kid']}}
    for role, environment in environments.items():
        environment.update(new_secrets('hosted-xfs', role))
        if role in {'ui', 'mcp'}:
            environment.update(role_environment(value['role_mapping']))
    jwks = json.dumps({'keys': [signers['ui'], signers['mcp']]}).encode()
    receipt = {'profile': 'hosted-xfs', 'service_image': image, 'native_image': native_image, 'containers': {},
               'volumes': volumes, 'networks': networks, 'public_url': origin, 'mcp_url': mcp_url,
               'issuer': value['issuer'], 'tenant_id': value['tenant_id'],
               'signing_key_ids': {role: jwk['kid'] for role, jwk in signers.items()},
               'storage_limit_bytes': value['storage_limit_bytes'], 'models': value['models']}
    if value['role_mapping']:
        receipt['role_mapping'] = value['role_mapping']  # an explicit operator choice, kept visible
    for role, container in containers.items():
        state = 'control' if role == 'runtime' else role + '-state'
        state_target = '/' + state
        port, target = (ui_port, 8780) if role == 'ui' else (mcp_port, 8767)
        args = role_create_args(profile='hosted-xfs', name=name, role=role, volumes=volumes, networks=networks,
                                publish=f'{value["bind_address"]}:{port}:{target}',
                                extra_hosts=value['extra_hosts'], device=value['xfs_device'])
        identity = docker(endpoint, *args, image, *role_command(role))
        receipt['containers'][role] = identity
        if role == 'runtime':
            environments[role]['XPERFECT_CONTROLLER_ID'] = identity
            docker(endpoint, 'cp', '-', identity + ':' + state_target,
                   data=_file_archive('assertion-jwks.json', jwks, 0o644))
        else:
            docker(endpoint, 'cp', '-', identity + ':' + state_target, data=tls)
            if value.get('tls_ca_file'):
                # A private test issuer's CA; a public IdP needs none of this.
                docker(endpoint, 'cp', '-', identity + ':' + state_target,
                       data=_file_archive('issuer-ca.pem', Path(value['tls_ca_file']).read_bytes(), 0o644))
                environments[role].update(SSL_CERT_FILE=state_target + '/issuer-ca.pem',
                                          REQUESTS_CA_BUNDLE=state_target + '/issuer-ca.pem')
        docker(endpoint, 'cp', '-', identity + ':' + state_target,
               data=archive_config({'profile': 'hosted-xfs', 'environment': environments[role]}))
        if role == 'runtime':
            docker(endpoint, 'network', 'connect', '--alias', 'runtime', networks['workers'], identity)
        docker(endpoint, 'start', identity)
    context = ssl.create_default_context(cafile=value.get('tls_ca_file') or None)
    _wait_until_hosted_ready(endpoint, containers, origin, mcp_url, context, timeout_s=ready_timeout_s)
    receipt['ui_url'], receipt['mcp_url'] = origin, mcp_url
    return receipt


def _wait_until_hosted_ready(endpoint: str, containers: dict[str, str], origin: str, mcp_url: str,
                             context: ssl.SSLContext, *, timeout_s: float = 45.0) -> None:
    """Succeed only when both advertised TLS front doors answer from this server.

    UI: its health endpoint. MCP: its OAuth protected-resource metadata, which
    must name the advertised MCP URL. Neither call changes state.
    """
    split = urllib.parse.urlsplit(mcp_url)
    metadata_url = f'{split.scheme}://{split.netloc}/.well-known/oauth-protected-resource{split.path}'
    deadline = time.monotonic() + timeout_s
    problem = 'containers are not running'
    while True:
        try:
            if not all(docker(endpoint, 'inspect', '--format', '{{.State.Running}}', container) == 'true'
                       for container in containers.values()):
                problem = 'containers are not running'
            else:
                problem = f'{origin}/health did not answer'
                with urllib.request.urlopen(origin + '/health', timeout=2, context=context) as response:
                    ui_ok = 200 <= response.status < 300
                if ui_ok:
                    problem = f'{metadata_url} did not answer'
                    with urllib.request.urlopen(metadata_url, timeout=2, context=context) as response:
                        metadata = json.loads(response.read() or b'{}')
                    try:
                        advertised = _https_url(metadata.get('resource'), 'resource')
                    except ValueError:
                        advertised = ''
                    if advertised == mcp_url:
                        return
                    problem = 'MCP metadata does not name mcp_public_url'
        except (RuntimeError, OSError, ValueError, urllib.error.URLError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Hosted services are not reachable at their public URLs from this server: '
                               + problem + '. Check DNS for the public names, the certificate and bind_address.')
        time.sleep(0.5)


def preapprove(*, endpoint: str, name: str, identity: bytes) -> str:
    """Admit one exact OIDC subject; enrollment itself stays closed."""
    if not endpoint.startswith('unix:///') or not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', name):
        raise ValueError('Select the explicit Docker endpoint and package name')
    payload = json.loads(identity)
    if not isinstance(payload, dict) or not str(payload.get('subject') or '').strip():
        raise ValueError('Preapproval needs one exact OIDC subject')
    if str(payload.get('role') or 'member') not in HOSTED_ROLES:
        raise ValueError('role must be one of: ' + ', '.join(sorted(HOSTED_ROLES)))
    return docker(endpoint, 'exec', '-i', name + '-ui', '/usr/bin/python3', '/opt/xperfect/deployment/linux/service.py',
                  'ui', '--config', '/ui-state/config.json', '--admin', 'preapprove-oidc', data=identity)


def main():
    if sys.argv[1:2] in (['upgrade'], ['upgrade-commit'], ['upgrade-rollback']):
        import upgrade
        upgrade.main(sys.argv[1:])
        return
    if sys.argv[1:2] == ['preapprove']:
        parser = argparse.ArgumentParser(prog='launch.py preapprove')
        parser.add_argument('--docker-host', required=True)
        parser.add_argument('--name', required=True)
        args = parser.parse_args(sys.argv[2:])
        # The identity arrives on stdin so subjects never enter shell history.
        print(preapprove(endpoint=args.docker_host, name=args.name, identity=sys.stdin.buffer.read()))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker-host', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--service-image', required=True)
    parser.add_argument('--native-image', required=True)
    parser.add_argument('--ui-port', type=int, default=8780)
    parser.add_argument('--mcp-port', type=int, default=8767)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--credentials', type=Path, help='Local profile only: private unlock/MCP output')
    parser.add_argument('--owner-id', default='local-owner')
    parser.add_argument('--profile', choices=('local-linux', 'hosted-xfs'), default='local-linux')
    parser.add_argument('--hosted-config', type=Path, help='Private hosted input JSON (hosted-xfs only)')
    parser.add_argument('--model', action='append', metavar='PROFILE=MODEL',
                        help='Exact native model for a worker profile, e.g. grok-build=<model>; repeatable')
    args = parser.parse_args()
    models = parse_model_arguments(args.model)
    if args.profile == 'hosted-xfs':
        if not args.hosted_config:
            raise SystemExit('--hosted-config is required for hosted-xfs')
        hosted = json.loads(_private_file(args.hosted_config, 'hosted_config').read_text())
        if models:
            if not isinstance(hosted, dict):
                raise ValueError('Hosted input must be a JSON object')
            existing = validate_models(hosted.get('models'))
            if any(existing.get(profile, model) != model for profile, model in models.items()):
                raise ValueError('--model conflicts with models in the hosted input; keep one')
            hosted = {**hosted, 'models': {**existing, **models}}
        with args.receipt.open('x') as output:
            args.receipt.chmod(0o600)
            receipt = launch_hosted(endpoint=args.docker_host, name=args.name, image=args.service_image,
                                    native_image=args.native_image, ui_port=args.ui_port, mcp_port=args.mcp_port,
                                    hosted=hosted)
            json.dump(receipt, output, indent=2)
        print(receipt['ui_url'])
        return
    if not args.credentials:
        raise SystemExit('--credentials is required for the local profile')
    # Reserve the private receipt before any Docker mutation.
    credential_fd = os.open(args.credentials, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(credential_fd, 'w') as credentials, args.receipt.open('x') as output:
        args.receipt.chmod(0o600)
        receipt = launch(endpoint=args.docker_host, name=args.name, image=args.service_image,
                         native_image=args.native_image, ui_port=args.ui_port, mcp_port=args.mcp_port,
                         credentials=credentials, owner_id=args.owner_id, models=models)
        json.dump(receipt, output, indent=2)
    print(receipt['ui_url'])
    print('Local unlock and MCP credentials: ' + str(args.credentials))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        # Operator input and readiness problems are one-line remedies, not tracebacks.
        print(f'xPerfect: {exc}', file=sys.stderr)
        raise SystemExit(2)
