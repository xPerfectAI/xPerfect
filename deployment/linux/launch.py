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


# Daemon failures a person can act on. The daemon's own text can include operator paths or
# configuration, so it is matched here and never printed; only the action is shown.
NO_FREE_NETWORK_RANGE = (
    'Docker has no free network address range for this package. Remove Docker networks you '
    'no longer use (see docker network ls), add address pools in Docker settings, or give each '
    'package network an unused private range with --subnet frontend=<CIDR> --subnet workers=<CIDR>, '
    'then launch again.')
DOCKER_FAILURE_ACTIONS = (
    ('non-overlapping ipv4 address pool', NO_FREE_NETWORK_RANGE),
    ('all predefined address pools have been fully subnetted', NO_FREE_NETWORK_RANGE),
    ('pool overlaps with other one on this address space',
     'A chosen --subnet range is already used by a Docker network on this computer. Choose another range.'),
    ('port is already allocated',
     'A chosen port is already in use on this computer. Choose other --ui-port/--mcp-port values.'),
    ('address already in use',
     'A chosen port is already in use on this computer. Choose other --ui-port/--mcp-port values.'),
    ('no such image', 'A package image is not on this computer. Use the published image references.'),
    ('manifest unknown', 'The published package image was not found. Check the image reference.'),
    ('pull access denied', 'The published package image was not found. Check the image reference.'),
    ('unauthorized', 'The registry refused the image without a sign-in. Check the image reference; '
                     'a private image needs docker login first.'),
    ('cannot connect to the docker daemon', 'Docker is not running. Start Docker, then launch again.'),
)


def docker_failure(operation: str, detail: str) -> str:
    """One plain line for a failed Docker operation: the operation and, when known, the action."""
    lowered = str(detail or '').casefold()
    for marker, action in DOCKER_FAILURE_ACTIONS:
        if marker in lowered:
            return f'Docker package operation failed: {operation}. {action}'
    return 'Docker package operation failed: ' + operation


def docker(endpoint: str, *args: str, data: bytes | None = None):
    result = subprocess.run(['docker', '--host', endpoint, *args], input=data,
                            capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(docker_failure(args[0], result.stderr.decode(errors='replace')))
    return result.stdout.decode().strip()


def local_docker_endpoint(value: str | None) -> str:
    """The Docker endpoint on this computer: the explicit one, else the CLI's current context.

    Only a local Unix socket is accepted; a remote or TCP endpoint is never used.
    """
    endpoint = str(value or '').strip()
    if not endpoint:
        result = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(docker_failure('context', result.stderr.decode(errors='replace')))
        endpoint = result.stdout.decode().strip()
    if not endpoint.startswith('unix:///'):
        raise ValueError('Select an explicit local Unix Docker endpoint')
    return endpoint


def resolve_image(endpoint: str, reference: str) -> str:
    """The exact local image identity for an identity or a digest-pinned published reference.

    `sha256:<id>` must already be on this computer. `<repository>@sha256:<digest>` is pulled when
    missing; the digest pins the exact published bytes, and the local identity is returned.
    """
    if re.fullmatch(r'sha256:[a-f0-9]{64}', reference):
        return reference
    if not re.fullmatch(r'[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[a-f0-9]{64}', reference):
        raise ValueError('Exact locally verified image identities are required')
    try:
        identity = docker(endpoint, 'image', 'inspect', '--format', '{{.Id}}', reference)
    except RuntimeError:
        docker(endpoint, 'pull', reference)
        identity = docker(endpoint, 'image', 'inspect', '--format', '{{.Id}}', reference)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', identity):
        raise ValueError('Image identity could not be verified')
    return identity


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


# The runtime's optional coordinator configuration: the conversation model and the helper
# routes it may delegate to, each with its own exact model and connected account. The
# launcher only checks its shape and carries it; the runtime validates it again on use.
COORDINATOR_CONFIG_KEY = 'GLASSHIVE_COORDINATOR_CONFIG_JSON'
_COORDINATOR_FIELDS = {'model', 'effort', 'max_goals', 'wake_on_results', 'routes', 'bootstrap_bundle',
                       'context_manifest', 'developer_instructions', 'scope'}
_COORDINATOR_ROUTE_FIELDS = {'id', 'profile', 'model', 'effort', 'execution_mode', 'connection_id',
                             'resource_class', 'bootstrap_bundle'}
_COORDINATOR_SCOPE_FIELDS = {'project_id', 'workspace_id', 'connection_id', 'execution_mode'}


def validate_coordinator_config(value: object) -> str:
    """Return the coordinator configuration as canonical JSON, or raise ValueError.

    It never accepts a configuration the runtime would refuse; a few of its limits are stricter.
    """
    def printable(item: str) -> bool:
        return not any(ord(character) < 32 or ord(character) == 127 for character in item)

    def named(item: object, limit: int) -> bool:
        return isinstance(item, str) and bool(item.strip()) and len(item) <= limit and printable(item)

    def optional_text(item: object, limit: int) -> bool:
        return isinstance(item, str) and len(item) <= limit and printable(item)

    def placement(item: object) -> bool:
        return isinstance(item, str) and item in {'host', 'docker'}

    try:
        # The runtime reads this text as UTF-8 JSON; unpaired surrogates cannot be read back.
        json.dumps(value, ensure_ascii=False).encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('The coordinator configuration contains text that is not valid Unicode') from None
    if (not isinstance(value, dict) or not {'model', 'effort'} <= set(value)
            or not set(value) <= _COORDINATOR_FIELDS or not named(value['model'], 200)
            or not named(value['effort'], 50)):
        raise ValueError('The coordinator configuration needs a model and effort, and only its known fields')
    max_goals = value.get('max_goals', 100)
    if (isinstance(max_goals, bool) or not isinstance(max_goals, int) or not 10 <= max_goals <= 1000
            or not isinstance(value.get('wake_on_results', True), bool)
            or not isinstance(value.get('developer_instructions', ''), str)
            or not all(isinstance(value.get(key, {}), dict) for key in ('bootstrap_bundle', 'context_manifest'))):
        raise ValueError('The coordinator configuration has an invalid goal limit, flag, instructions or bundle')
    scope = value.get('scope', {})
    if (not isinstance(scope, dict) or not set(scope) <= _COORDINATOR_SCOPE_FIELDS
            or not placement(scope.get('execution_mode', 'host'))
            or not all(optional_text(scope.get(key, ''), 512) for key in ('project_id', 'workspace_id', 'connection_id'))
            or (scope.get('workspace_id') and not scope.get('project_id'))):
        raise ValueError('The coordinator scope is invalid')
    routes = value.get('routes', [])
    if not isinstance(routes, list) or len(routes) > 32:
        raise ValueError('The coordinator configuration allows at most 32 routes')
    ids = set()
    for route in routes:
        if (not isinstance(route, dict) or not {'id', 'profile', 'model', 'effort', 'execution_mode'} <= set(route)
                or not set(route) <= _COORDINATOR_ROUTE_FIELDS or not named(route['id'], 100)
                or not named(route['profile'], 100) or not named(route['model'], 200)
                or not named(route['effort'], 50) or not placement(route['execution_mode'])
                or not optional_text(route.get('connection_id', ''), 512)
                or not named(route.get('resource_class', 'standard'), 50)
                or not isinstance(route.get('bootstrap_bundle', {}), dict) or route['id'] in ids):
            raise ValueError('Each coordinator route needs a unique id, profile, model, effort and execution mode')
        ids.add(route['id'])
    text = json.dumps(value, sort_keys=True, separators=(',', ':'))
    if len(text) > 65536:
        raise ValueError('The coordinator configuration is too large')
    return text


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


ICC_OPTION = 'com.docker.network.bridge.enable_icc'


def network_create_args(*, name: str, role: str, network: str, settings: dict,
                        subnet: str | None = None) -> list[str]:
    """The package networks. An image that serves native tools through per-box
    sockets declares an isolated workers bridge: no container on it, box or
    runtime, can reach another. An explicit subnet only fixes the bridge's range;
    Docker itself refuses one that overlaps a network it already has."""
    args = ['network', 'create', '--driver', 'bridge', '--label', 'xperfect.package=' + name]
    if subnet:
        args += ['--subnet', subnet]
    if role == 'workers' and settings.get('XPERFECT_WORKER_NETWORK') == 'isolated':
        args += ['--opt', ICC_OPTION + '=false']
    return args + [network]


PRIVATE_RANGES = tuple(ipaddress.IPv4Network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def validate_subnets(items: list[str] | None) -> dict[str, str]:
    """Explicit ranges for the package bridges, given as ROLE=CIDR for frontend and/or workers."""
    subnets: dict[str, ipaddress.IPv4Network] = {}
    for item in items or []:
        role, _, value = str(item).partition('=')
        role = role.strip()
        if role not in ('frontend', 'workers') or role in subnets:
            raise ValueError('Give each range once as --subnet frontend=<CIDR> or --subnet workers=<CIDR>')
        try:
            network = ipaddress.IPv4Network(value.strip())
        except ValueError:
            raise ValueError(f'--subnet {role} needs an IPv4 network such as 10.<n>.<n>.0/24') from None
        if not any(network.subnet_of(private) for private in PRIVATE_RANGES) or not 16 <= network.prefixlen <= 28:
            raise ValueError(f'--subnet {role} must be a private range from /16 to /28')
        subnets[role] = network
    if len(subnets) == 2 and subnets['frontend'].overlaps(subnets['workers']):
        raise ValueError('The frontend and workers ranges must not overlap')
    return {role: str(network) for role, network in subnets.items()}


def _route_destinations(output: str, *, bsd: bool) -> list[ipaddress.IPv4Network]:
    """IPv4 destinations from `ip -4 route show table all` or BSD/macOS `netstat -rn -f inet`."""
    destinations = []
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if not bsd and fields[0] in ('unicast', 'local', 'broadcast', 'multicast', 'throw', 'unreachable',
                                     'prohibit', 'blackhole', 'nat', 'anycast') and len(fields) > 1:
            fields = fields[1:]
        value = fields[0]
        if value == 'default' or not value[:1].isdigit():
            continue
        address, _, prefix = value.partition('/')
        octets = [part for part in address.split('%')[0].split('.') if part]
        if not octets or len(octets) > 4 or not all(part.isdigit() for part in octets):
            continue
        # macOS abbreviates networks ("192.168.1" is a /24, "10.8/16" names its prefix).
        length = int(prefix) if prefix.isdigit() else (8 * len(octets) if bsd else 32)
        try:
            destinations.append(ipaddress.IPv4Network('.'.join(octets + ['0'] * (4 - len(octets))) + f'/{length}',
                                                      strict=False))
        except ValueError:
            continue
    return destinations


def host_route_overlaps(subnets: dict[str, str]) -> dict[str, list[str]]:
    """Routes this computer already uses (LAN, VPN, VMs) that a chosen range would shadow."""
    if not subnets:
        return {}
    for command, bsd in ((['ip', '-4', 'route', 'show', 'table', 'all'], False), (['netstat', '-rn', '-f', 'inet'], True)):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            routes = _route_destinations(result.stdout, bsd=bsd)
            overlaps = {role: sorted({str(route) for route in routes if route.overlaps(ipaddress.IPv4Network(value))})
                        for role, value in subnets.items()}
            return {role: found for role, found in overlaps.items() if found}
    raise ValueError("This computer's routes could not be read to check the --subnet ranges")


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
           models: dict[str, str] | None = None, coordinator: dict | None = None,
           subnets: dict[str, str] | None = None) -> dict:
    models = validate_models(models)
    subnets = subnets or {}
    coordinator_environment = ({} if coordinator is None
                               else {COORDINATOR_CONFIG_KEY: validate_coordinator_config(coordinator)})
    if not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', name):
        raise ValueError('Package name must start with xperfect-')
    if not endpoint.startswith('unix:///'):
        raise ValueError('Select an explicit local Unix Docker endpoint')
    for identity in (image, native_image):
        if not (re.fullmatch(r'sha256:[a-f0-9]{64}', identity)
                or re.fullmatch(r'[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[a-f0-9]{64}', identity)):
            raise ValueError('Exact locally verified image identities are required')
    if not (1024 <= ui_port <= 65535 and 1024 <= mcp_port <= 65535 and ui_port != mcp_port):
        raise ValueError('Two distinct unprivileged loopback ports are required')
    if not owner_id or len(owner_id) > 512 or any(ord(char) < 32 for char in owner_id):
        raise ValueError('A stable local owner ID is required')
    info = json.loads(docker(endpoint, 'info', '--format', '{{json .}}'))
    if info.get('OSType') != 'linux' or str(info.get('CgroupVersion')) != '2':
        raise ValueError('Linux Docker with cgroup v2 is required')
    image, native_image = resolve_image(endpoint, image), resolve_image(endpoint, native_image)
    for identity in (image, native_image):
        if docker(endpoint, 'image', 'inspect', '--format', '{{.Id}}', identity) != identity:
            raise ValueError('Image identity could not be verified')
    # 'links' holds only the signed short-link store that every role mints into and
    # the UI resolves; private auth, control and data state stay on their own volumes.
    volumes = {role: name + '-' + role for role in ('data', 'control', 'ui-state', 'mcp-state', 'links')}
    networks = {role: name + '-' + role for role in ('frontend', 'workers')}
    containers = {role: name + '-' + role for role in ('runtime', 'ui', 'mcp')}
    # A single run owns only fresh names and never adopts an existing resource. If creating
    # its networks and empty volumes fails, it removes exactly those it just created, so the
    # same launch can simply run again; state that holds data is retained for recovery.
    for kind, planned in (('volume', volumes.values()), ('network', networks.values()), ('container', containers.values())):
        existing = set(docker(endpoint, kind, 'ls', *(['-a'] if kind == 'container' else []), '--format', '{{.Name}}' if kind != 'container' else '{{.Names}}').splitlines())
        if existing.intersection(planned):
            raise ValueError('Package resources already exist; use their recorded identities to restart, '
                             'or remove an unstarted attempt with: launch.py discard-unstarted --name ' + name)
    created: list[tuple[str, str]] = []
    try:
        # Networks first: they are what a crowded Docker host runs out of. Each is removed on
        # rollback by the exact identity its creation returned, never by name.
        for role, network in networks.items():
            identity = docker(endpoint, *network_create_args(name=name, role=role, network=network,
                                                             settings=PROFILE_SETTINGS['local-linux'],
                                                             subnet=subnets.get(role)))
            created.append(('network', identity if re.fullmatch(r'[a-f0-9]{64}', identity) else network))
        for volume in volumes.values():
            docker(endpoint, 'volume', 'create', '--label', 'xperfect.package=' + name, volume)
            created.append(('volume', volume))
    except (RuntimeError, ValueError):
        for kind, resource in reversed(created):
            try:
                docker(endpoint, kind, 'rm', resource)
            except RuntimeError:
                pass
        raise
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
               **model_environment(models), **coordinator_environment}
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


def remove_own_empty_outputs(paths: list[Path]) -> None:
    """Remove output files this run created that are still empty; anything with content stays."""
    for path in paths:
        try:
            info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size == 0:
            os.unlink(path)


def discard_unstarted(endpoint: str, name: str) -> dict:
    """Remove a named local package attempt that never started, so it can launch again.

    Nothing is removed unless every check passes first: the package has no containers, its
    networks have nothing attached, and each of its labelled volumes has no users and no data.
    Only the package's own names are touched.
    """
    if not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', name):
        raise ValueError('Package name must start with xperfect-')
    if not endpoint.startswith('unix:///'):
        raise ValueError('Select an explicit local Unix Docker endpoint')
    planned_containers = {name + '-' + role for role in ('runtime', 'ui', 'mcp')}
    if planned_containers & set(docker(endpoint, 'ps', '-a', '--format', '{{.Names}}').splitlines()):
        raise ValueError('This package started once; restart or recover it from its receipt instead')
    label = 'xperfect.package=' + name
    planned_volumes = {name + '-' + role for role in ('data', 'control', 'ui-state', 'mcp-state', 'links')}
    volumes = sorted(planned_volumes & set(docker(
        endpoint, 'volume', 'ls', '--filter', 'label=xperfect.package=' + name, '--format', '{{.Name}}').splitlines()))
    usage = {str(row.get('Name')): row for row in json.loads(
        docker(endpoint, 'system', 'df', '-v', '--format', '{{json .Volumes}}') or '[]') if isinstance(row, dict)}
    for volume in volumes:
        row = usage.get(volume, {})
        if str(row.get('Links')) != '0' or str(row.get('Size')) not in {'0B', '0'}:
            raise ValueError(f'{volume} is in use or holds data, so nothing was removed')
    planned_networks = [name + '-' + role for role in ('frontend', 'workers')]
    present_networks = set(docker(endpoint, 'network', 'ls', '--format', '{{.Name}}').splitlines())
    networks: list[tuple[str, str]] = []
    for network in planned_networks:
        if network not in present_networks:
            continue
        # Identity, owner label and users of this exact network, read in one inspection.
        identity, owner, attached = (docker(
            endpoint, 'network', 'inspect', '--format',
            '{{.Id}} {{index .Labels "xperfect.package"}} {{len .Containers}}', network).split() + ['', '', ''])[:3]
        if 'xperfect.package=' + owner != label or not re.fullmatch(r'[a-f0-9]{64}', identity):
            raise ValueError(f'{network} does not belong to this package, so nothing was removed')
        if attached != '0':
            raise ValueError(f'{network} is in use, so nothing was removed')
        networks.append((network, identity))
    for _, identity in networks:
        docker(endpoint, 'network', 'rm', identity)
    for volume in volumes:
        docker(endpoint, 'volume', 'rm', volume)
    return {'package': name, 'removed_networks': [network for network, _ in networks],
            'removed_volumes': volumes}


# The admission record owns roles; the identity provider only proves identity.
HOSTED_ROLES = {'member', 'viewer', 'tenant_admin'}
HOSTED_REQUIRED = {'public_url', 'mcp_public_url', 'issuer', 'client_id', 'client_secret_file', 'tenant_id',
                   'tls_certificate_file', 'tls_key_file', 'xfs_mount', 'xfs_device'}
HOSTED_OPTIONAL = {'principal_claim', 'mcp_audiences', 'mcp_scopes', 'mcp_client_ids', 'storage_limit_bytes',
                   'shared_memory_bytes', 'extra_hosts', 'bind_address', 'tls_ca_file', 'models',
                   'role_claim', 'role_map', 'coordinator'}
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
    if value.get('coordinator') is not None:
        validate_coordinator_config(value['coordinator'])
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
    for role, network in networks.items():
        docker(endpoint, *network_create_args(name=name, role=role, network=network,
                                              settings=PROFILE_SETTINGS['hosted-xfs']))
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
               **model_environment(value['models']),
               **({COORDINATOR_CONFIG_KEY: validate_coordinator_config(value['coordinator'])}
                  if value.get('coordinator') is not None else {})}
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


def published_release() -> tuple[dict[str, str], dict[str, str]]:
    """Resolve the reviewed images and exact local model configuration."""
    try:
        release = json.loads(Path(__file__).with_name('candidate-images.json').read_text())
        registry = release['registry']
        images = release['images']
        if not isinstance(registry, str) or not isinstance(images, list):
            raise ValueError
        result = {}
        for role in ('xperfect-service', 'xperfect-native'):
            selected = [image for image in images if isinstance(image, dict) and image.get('repository') == role]
            if len(selected) != 1:
                raise ValueError
            reference = registry + '/' + role + '@' + selected[0]['digest']
            if not re.fullmatch(r'[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[a-f0-9]{64}', reference):
                raise ValueError
            result[role] = reference
        return result, validate_models(release.get('models'))
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('Reviewed package release metadata is missing or invalid. '
                         'Use a complete release or explicit --service-image and --native-image.') from None


def local_output_defaults(args) -> None:
    """Keep local startup outputs private without replacing an existing install."""
    if args.receipt is not None and args.credentials is not None:
        return
    state = (args.state_dir or Path.home() / args.name).expanduser().absolute()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = state.lstat()
    if state.resolve(strict=True) != state or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Package state must be a real directory owned by the current user')
    state.chmod(0o700)
    args.receipt = args.receipt or state / 'receipt.json'
    args.credentials = args.credentials or state / 'credentials.json'


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
    if sys.argv[1:2] == ['discard-unstarted']:
        parser = argparse.ArgumentParser(prog='launch.py discard-unstarted')
        parser.add_argument('--docker-host', help='Local Docker endpoint; defaults to the current Docker context')
        parser.add_argument('--name', required=True)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(discard_unstarted(local_docker_endpoint(args.docker_host), args.name)))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker-host', help='Local Docker endpoint; defaults to the current Docker context')
    parser.add_argument('--name', default='xperfect-local', help='Package instance name (default: xperfect-local)')
    parser.add_argument('--service-image', help='Advanced: override the reviewed application image')
    parser.add_argument('--native-image', help='Advanced: override the reviewed worker image')
    parser.add_argument('--ui-port', type=int, default=8780)
    parser.add_argument('--mcp-port', type=int, default=8767)
    parser.add_argument('--state-dir', type=Path, help='Local private state directory (default: ~/<package name>)')
    parser.add_argument('--receipt', type=Path, help='Advanced: explicit receipt output')
    parser.add_argument('--credentials', type=Path, help='Local profile only: private unlock/MCP output')
    parser.add_argument('--owner-id', default='local-owner')
    parser.add_argument('--profile', choices=('local-linux', 'hosted-xfs'), default='local-linux')
    parser.add_argument('--hosted-config', type=Path, help='Private hosted input JSON (hosted-xfs only)')
    parser.add_argument('--model', action='append', metavar='PROFILE=MODEL',
                        help='Exact native model for a worker profile, e.g. grok-build=<model>; repeatable')
    parser.add_argument('--coordinator-config', type=Path, metavar='PRIVATE_JSON',
                        help='Conversation model and helper routes (see docs/deployment.md); private file')
    parser.add_argument('--subnet', action='append', metavar='ROLE=CIDR',
                        help='Local profile: an unused private range for the frontend or workers network, '
                             'for when Docker has no free default range; repeatable')
    args = parser.parse_args()
    if not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', args.name):
        raise ValueError('Package name must start with xperfect-')
    release_models = {}
    if args.service_image is None or args.native_image is None:
        images, release_models = published_release()
        args.service_image = args.service_image or images['xperfect-service']
        args.native_image = args.native_image or images['xperfect-native']
    models = parse_model_arguments(args.model)
    if args.profile == 'local-linux':
        # The release owns current supported defaults; explicit profile choices win.
        # Hosted inputs and fully explicit image deployments keep their own choices.
        models = {**release_models, **models}
    subnets = validate_subnets(args.subnet)
    if subnets and args.profile != 'local-linux':
        raise ValueError('--subnet applies to the local profile')
    shadowed = host_route_overlaps(subnets)
    if shadowed:
        raise ValueError('A --subnet range overlaps a route this computer already uses ('
                         + '; '.join(f'{role}: {", ".join(found)}' for role, found in shadowed.items())
                         + '). Choose another private range.')
    coordinator = (json.loads(_private_file(args.coordinator_config, 'coordinator_config').read_text())
                   if args.coordinator_config else None)
    if args.profile == 'hosted-xfs':
        if not args.hosted_config:
            raise SystemExit('--hosted-config is required for hosted-xfs')
        if not args.receipt:
            raise SystemExit('--receipt is required for hosted-xfs')
        if args.state_dir:
            raise ValueError('--state-dir applies to the local profile')
        hosted = json.loads(_private_file(args.hosted_config, 'hosted_config').read_text())
        if models:
            if not isinstance(hosted, dict):
                raise ValueError('Hosted input must be a JSON object')
            existing = validate_models(hosted.get('models'))
            if any(existing.get(profile, model) != model for profile, model in models.items()):
                raise ValueError('--model conflicts with models in the hosted input; keep one')
            hosted = {**hosted, 'models': {**existing, **models}}
        if coordinator is not None:
            if not isinstance(hosted, dict) or hosted.get('coordinator') is not None:
                raise ValueError('--coordinator-config conflicts with coordinator in the hosted input; keep one')
            hosted = {**hosted, 'coordinator': coordinator}
        created: list[Path] = []
        try:
            with args.receipt.open('x') as output:
                created.append(args.receipt)
                args.receipt.chmod(0o600)
                receipt = launch_hosted(endpoint=local_docker_endpoint(args.docker_host), name=args.name, image=args.service_image,
                                        native_image=args.native_image, ui_port=args.ui_port, mcp_port=args.mcp_port,
                                        hosted=hosted)
                json.dump(receipt, output, indent=2)
        except BaseException:
            remove_own_empty_outputs(created)
            raise
        print(receipt['ui_url'])
        return
    local_output_defaults(args)
    # Reserve the private outputs before any Docker mutation. An existing file is never replaced.
    credential_fd = os.open(args.credentials, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    created = [args.credentials]
    try:
        with os.fdopen(credential_fd, 'w') as credentials:
            with args.receipt.open('x') as output:
                created.append(args.receipt)
                args.receipt.chmod(0o600)
                receipt = launch(endpoint=local_docker_endpoint(args.docker_host), name=args.name, image=args.service_image,
                                 native_image=args.native_image, ui_port=args.ui_port, mcp_port=args.mcp_port,
                                 credentials=credentials, owner_id=args.owner_id, models=models,
                                 coordinator=coordinator, subnets=subnets)
                json.dump(receipt, output, indent=2)
    except BaseException:
        # A launch that rolled back leaves the same command ready to run again; credentials it
        # already wrote for state it kept stay in place.
        remove_own_empty_outputs(created)
        raise
    print(receipt['ui_url'])
    print('Local unlock and MCP credentials: ' + str(args.credentials))


if __name__ == '__main__':
    try:
        main()
    except FileExistsError:
        print('xPerfect: Private package outputs already exist. Keep them for the existing package, '
              'or choose another --name or --state-dir.', file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, RuntimeError) as exc:
        # Operator input and readiness problems are one-line remedies, not tracebacks.
        print(f'xPerfect: {exc}', file=sys.stderr)
        raise SystemExit(2)
