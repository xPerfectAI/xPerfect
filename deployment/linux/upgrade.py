"""Upgrade one packaged xPerfect install to exact new images, with rollback.

``launch.py upgrade`` runs one transaction:

1. Validate the private receipt and the running role containers against the
   launcher's container shape.
2. Prove the package idle with its current image (the same continuity check a
   restore uses), stop it, and prove it again while stopped.
3. Copy every service-state volume (not owner files) into one backup volume and
   verify the copy byte-for-byte.
4. Keep the previous containers, renamed, and create the same role containers
   from the new image over the same volumes. Only the runtime controller
   identity and explicit native-image/model choices change in configuration.
5. Require the new services to answer, then write the new receipt.

A failed step restores the backup and the previous containers. After checking
the new version, ``upgrade-commit`` removes the previous containers and backup;
``upgrade-rollback`` returns the previous containers and their service state as
it was at the upgrade. Rollback changes nothing unless the backup is intact, the
new version has no work or workspace containers a rollback would orphan, and no
owner gained storage after the upgrade. Service records made after the upgrade
(sign-ins, access changes, projects, runs) are discarded by a rollback. Owner
files on the data volume are never copied or rewritten.
"""
from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess
import tarfile
import time
import uuid

try:  # imported as deployment.linux.upgrade in tests
    from . import launch as base
except ImportError:  # run through deployment/linux/launch.py
    import launch as base

ROLES = ('runtime', 'ui', 'mcp')
STATE = {'runtime': 'control', 'ui': 'ui-state', 'mcp': 'mcp-state'}
PORTS = {'ui': '8780/tcp', 'mcp': '8767/tcp'}
TMPFS = 'rw,noexec,nosuid,nodev,size=64m,mode=1777'
RUNTIME_PYTHON = '/opt/xperfect/venvs/runtime/bin/python'
SHA = re.compile(r'sha256:[a-f0-9]{64}')
ID = re.compile(r'[a-f0-9]{64}')
# The runtime's default-off local-QA fault authority. Only an explicit private input on a local
# package supplies it; its values never enter the journal and rollback restores the previous config.
LOCAL_QA_AUTHORITY_KEYS = frozenset({
    'VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE', 'VIVENTIUM_LOCAL_QA_CASE_ID', 'VIVENTIUM_LOCAL_QA_CASE_TOKEN',
    'VIVENTIUM_LOCAL_QA_SESSION_REF', 'VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST',
    'VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST',
})


def validate_local_qa_authority(value: object) -> dict[str, str]:
    if (not isinstance(value, dict) or set(value) != LOCAL_QA_AUTHORITY_KEYS
            or any(not isinstance(item, str) or not 0 < len(item.strip()) <= 1024
                   or any(ord(character) < 32 for character in item) for item in value.values())):
        raise UpgradeError('The local-QA authority must name exactly its six values. Nothing was changed')
    return {key: item.strip() for key, item in value.items()}

# Loads the role's own persisted configuration inside the package image, so the
# idle check and health probe run with exactly the runtime's settings. Nothing
# secret is printed.
_ENV = ("import os,sys\nfrom pathlib import Path\nsys.path.insert(0,'/opt/xperfect/deployment/linux')\n"
        "import service\nenv=service.load_environment(Path('/control/config.json'),'runtime')\n")


def quiescent_program(*, incoming: bool = False) -> str:
    """The package's continuity idle proof. ``incoming``: run by the release about to
    inherit the state, which may review state an earlier release wrote."""
    flag = ",'--incoming'" if incoming else ''
    return _ENV + (
        "if Path('/control/.g8-restore-journal.json').exists():\n"
        "    sys.exit('xperfect-upgrade: a restore transaction is still open; commit or roll it back first')\n"
        f"os.execve('{RUNTIME_PYTHON}',['{RUNTIME_PYTHON}','-I','-m','workers_projects_runtime.native_continuity',"
        f"'quiescent','/control/runtime.db'{flag}],env)\n")


QUIESCENT = quiescent_program()
INCOMING_QUIESCENT = quiescent_program(incoming=True)


PROJECTION = re.compile(r'prj_[0-9a-f]{32}')


def unpublished_program(expect: list[str] | None = None) -> str:
    """The new release's review of stored-file attachments the running release registered but
    could never publish. With ``expect`` (the reviewed identities) it retires exactly those,
    or refuses. Typed JSON out."""
    if expect is not None and not all(PROJECTION.fullmatch(item) for item in expect):
        raise UpgradeError('Unexpected unpublished file identity')
    flag = '' if expect is None else f",'--apply','--expect','{','.join(expect)}'"
    return _ENV + (
        "if Path('/control/.g8-restore-journal.json').exists():\n"
        "    sys.exit('xperfect-upgrade: a restore transaction is still open; commit or roll it back first')\n"
        f"os.execve('{RUNTIME_PYTHON}',['{RUNTIME_PYTHON}','-I','-m','workers_projects_runtime.native_continuity',"
        f"'reconcile-unpublished','/control/runtime.db','--incoming'{flag}],env)\n")


UNPUBLISHED_REPORT = unpublished_program()
# The running release's own work predicate, without its schema review. An earlier
# release cannot learn a new entry point, so this calls the same function its
# `quiescent` command runs after reviewing the schema (present, with this
# signature, in every packaged release). A missing function fails the proof.
_ACTIVITY = (
    "import os,sqlite3,stat\nfrom pathlib import Path\n"
    "from workers_projects_runtime import native_continuity as continuity\n"
    "database=Path('/control/runtime.db')\nmetadata=database.lstat()\n"
    "if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid() or metadata.st_nlink!=1:\n"
    "    raise ValueError('GlassHive database is not an owned regular file')\n"
    "connection=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)\n"
    "try:\n    continuity._require_quiescent(connection,database)\nfinally:\n    connection.close()\n")
RUNNING_ACTIVITY = _ENV + (
    "if Path('/control/.g8-restore-journal.json').exists():\n"
    "    sys.exit('xperfect-upgrade: a restore transaction is still open; commit or roll it back first')\n"
    f"os.execve('{RUNTIME_PYTHON}',['{RUNTIME_PYTHON}','-I','-c',{_ACTIVITY!r}],env)\n")
# The profile settings a service image declares (typed JSON; null for an image
# that declares none). Read from the image that will run them, never the host.
IMAGE_SETTINGS = ("import json,sys\nsys.path.insert(0,'/opt/xperfect/deployment/linux')\nimport service\n"
                  "print(json.dumps(getattr(service,'PROFILE_SETTINGS',None)))\n")
SETTING_NAME = re.compile(r'[A-Z][A-Z0-9_]{2,80}')
# Docker Desktop records its socket proxy as the source of a /var/run/docker.sock bind.
DOCKER_DESKTOP_SOCKET = '/run/host-services/docker.proxy.sock'
HEALTH = _ENV + (
    "import json,urllib.request\n"
    "request=urllib.request.Request('http://127.0.0.1:8766/health',headers={'Authorization':'Bearer '+env['WPR_API_TOKEN']})\n"
    "with urllib.request.urlopen(request,timeout=3) as response:\n"
    "    print(json.dumps({'status':json.load(response).get('status')}))\n")
# Copies and verifies service state in a networkless helper. Ownership, modes,
# links and bytes are part of the digest; timestamps are not. A restore proves
# the whole backup before it removes anything live.
STATE_TOOL = r'''
import hashlib, json, os, shutil, sqlite3, stat, subprocess, sys
from pathlib import Path
STATE, BACKUP = Path('/state'), Path('/backup')
REGISTRY = 'storage/registry.sqlite3'
action, roles, expected = sys.argv[1], sys.argv[2].split(','), json.loads(sys.argv[3])
def digest(root):
    h, files, size = hashlib.sha256(), 0, 0
    for base, dirs, names in os.walk(root):
        dirs.sort()
        for entry in sorted(dirs + names):
            path = Path(base, entry); info = path.lstat()
            h.update(json.dumps([path.relative_to(root).as_posix(), info.st_mode, info.st_uid, info.st_gid]).encode())
            if stat.S_ISREG(info.st_mode):
                files += 1; size += info.st_size
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1 << 20), b''): h.update(chunk)
            elif stat.S_ISLNK(info.st_mode):
                h.update(os.readlink(path).encode())
    return {'digest': h.hexdigest(), 'files': files, 'bytes': size}
def copy(source, target):
    subprocess.run(['cp', '-a', str(source) + '/.', str(target) + '/'], check=True)
def refuse(message):
    # Exit status 3: checked before any change, so nothing was restored.
    print('xperfect-upgrade: ' + message + '. Nothing was restored', file=sys.stderr)
    sys.exit(3)
def owner_projects(control):
    # The storage registry's full logical content, read-only.
    path = Path(control, REGISTRY)
    if not path.is_file():
        return set()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        return set(connection.iterdump())
    finally:
        connection.close()
result = {}
if action == 'snapshot':
    sources = {role: digest(STATE / role) for role in roles}
    free = os.statvfs(BACKUP); available = free.f_bavail * free.f_frsize
    needed = sum(item['bytes'] for item in sources.values()) + (256 << 20)
    if available < needed:
        sys.exit(f'xperfect-upgrade: the backup needs {needed} bytes free; {available} are available')
    for role in roles:
        target = BACKUP / 'before' / role
        if target.exists():
            sys.exit('xperfect-upgrade: this backup already holds a snapshot')
        target.mkdir(parents=True, mode=0o700)
        copy(STATE / role, target)
        result[role] = digest(target)
        if result[role] != sources[role]:
            sys.exit('xperfect-upgrade: the service-state copy does not match its source')
elif action in ('verify', 'restore'):
    for role in roles:
        source = BACKUP / 'before' / role
        if not source.is_dir() or source.is_symlink() or digest(source) != expected.get(role):
            refuse('the upgrade backup is missing or damaged')
    # Owner storage records are allocated once and tag owner folders on disk;
    # rewinding a newer allocation would lock that owner out of their storage.
    try:
        added = owner_projects(STATE / 'control') - owner_projects(BACKUP / 'before' / 'control')
    except sqlite3.Error:
        refuse('the owner storage registry could not be compared')
    if added:
        refuse('owners gained storage after the upgrade and a rollback would lock them out; commit the upgrade instead')
    result['changed'] = [role for role in roles if digest(STATE / role)['digest'] != expected[role]['digest']]
    for role in roles if action == 'restore' else []:
        live = STATE / role
        for entry in live.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        copy(BACKUP / 'before' / role, live)
        result[role] = digest(live)
        if result[role] != expected[role]:
            sys.exit('xperfect-upgrade: restored service state does not match the pre-upgrade snapshot')
print(json.dumps(result))
'''


STATE_REFUSED = 3  # the state tool refused before changing anything


class UpgradeError(RuntimeError):
    """An operator-facing upgrade failure with a short remedy."""


def _detail(exc: BaseException) -> str:
    """Only this module's own messages; other errors can name private paths."""
    return str(exc) if isinstance(exc, UpgradeError) else type(exc).__name__


def _docker(endpoint: str, *args: str, data: bytes | None = None, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run(['docker', '--host', endpoint, *args], input=data, capture_output=True, timeout=timeout)


def _ok(endpoint: str, *args: str, data: bytes | None = None, timeout: float = 90) -> str:
    result = _docker(endpoint, *args, data=data, timeout=timeout)
    if result.returncode:
        # Docker stderr can name private paths; report only the operation.
        raise UpgradeError('Docker package operation failed: ' + args[0])
    return result.stdout.decode().strip()


def _reason(stderr: bytes) -> str:
    """Only the runtime's own one-line typed reason, never a traceback or path."""
    for line in reversed(stderr.decode(errors='replace').strip().splitlines()):
        line = line.strip()
        for prefix in ('ValueError: ', 'SystemExit: ', ''):
            text = line[len(prefix):] if line.startswith(prefix) else ''
            if text.startswith(('GlassHive ', 'xperfect-upgrade: ')) and re.fullmatch(r"[A-Za-z0-9 ,.;:'()/_-]{1,240}", text):
                return text.removeprefix('xperfect-upgrade: ')
    return ''


def _private_json(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise UpgradeError(f'{path.name} must be a private file owned by this user')
    return json.loads(path.read_text())


def _write_private_json(path: Path, value: dict, *, create: bool = False) -> None:
    _write_private_text(path, json.dumps(value, indent=2, sort_keys=True) + '\n', create=create)


def _write_private_text(path: Path, text: str, *, create: bool = False) -> None:
    payload = text.encode()
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        temporary = path.with_name(path.name + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if not create:
        os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def journal_path(receipt: Path) -> Path:
    return receipt.with_name(receipt.name + '.upgrade.json')


def validate_receipt(receipt: dict) -> tuple[str, str]:
    """Return (profile, package name) from a launcher receipt."""
    profile = receipt.get('profile')
    if profile not in {'local-linux', 'hosted-xfs'}:
        raise UpgradeError('The receipt is not a packaged xPerfect receipt')
    roles = {'data', 'control', 'ui-state', 'mcp-state', 'links'} | ({'auth'} if profile == 'hosted-xfs' else set())
    volumes, networks = receipt.get('volumes') or {}, receipt.get('networks') or {}
    control = str(volumes.get('control') or '')
    name = control[:-len('-control')] if control.endswith('-control') else ''
    if (not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', name) or set(volumes) != roles
            or any(volumes[role] != f'{name}-{role}' for role in roles)
            or set(networks) != {'frontend', 'workers'}
            or any(networks[role] != f'{name}-{role}' for role in networks)
            or set(receipt.get('containers') or {}) != set(ROLES)
            or not all(ID.fullmatch(str(value)) for value in receipt['containers'].values())
            or not SHA.fullmatch(str(receipt.get('service_image') or ''))
            or not SHA.fullmatch(str(receipt.get('native_image') or ''))):
        raise UpgradeError('The receipt is incomplete or was not written by this launcher')
    if profile == 'hosted-xfs' and not (receipt.get('public_url') and receipt.get('mcp_url')):
        raise UpgradeError('The hosted receipt lacks its public URLs')
    return profile, name


def _capabilities(values) -> set[str]:
    return {str(value).upper().removeprefix('CAP_') for value in values or []}


def role_shape(record: dict, *, profile: str, name: str, role: str, volumes: dict, networks: dict,
               docker_desktop: bool = False) -> dict:
    """Check a container against the launcher shape; return what upgrade must keep."""
    hosted = profile == 'hosted-xfs'
    config, host = record.get('Config') or {}, record.get('HostConfig') or {}
    labels = config.get('Labels') or {}
    state = STATE[role]
    expected_mounts = {('volume', volumes[state], '/' + state), ('volume', volumes['links'], '/links')}
    if role == 'runtime':
        expected_mounts |= {('volume', volumes['data'], '/data'), ('bind', '/var/run/docker.sock', '/var/run/docker.sock')}
    elif hosted:
        expected_mounts.add(('volume', volumes['auth'], '/auth'))
    mounts = {(item.get('Type'), item.get('Source'), item.get('Target')) for item in host.get('Mounts') or []}
    if not hosted and docker_desktop and ('bind', DOCKER_DESKTOP_SOCKET, '/var/run/docker.sock') in mounts:
        # The launcher asked for /var/run/docker.sock; Docker Desktop records its proxy instead.
        mounts = mounts - {('bind', DOCKER_DESKTOP_SOCKET, '/var/run/docker.sock')} | {
            ('bind', '/var/run/docker.sock', '/var/run/docker.sock')}
    nocopy = all((item.get('VolumeOptions') or {}).get('NoCopy') is True
                 for item in host.get('Mounts') or [] if item.get('Type') == 'volume')
    caps = {'CHOWN', 'FOWNER', 'DAC_OVERRIDE'} | ({'SYS_ADMIN'} if hosted else set()) if role == 'runtime' else set()
    attached = set(((record.get('NetworkSettings') or {}).get('Networks') or {}))
    wanted_networks = {networks['frontend']} | ({networks['workers']} if role == 'runtime' else set())
    restart = (host.get('RestartPolicy') or {}).get('Name') or 'no'
    security = host.get('SecurityOpt') or []
    problems = [
        not ID.fullmatch(str(record.get('Id') or '')),
        labels.get('xperfect.package') != name or labels.get('xperfect.role') != role,
        config.get('Cmd') != base.role_command(role),
        host.get('ReadonlyRootfs') is not True or host.get('Privileged'),
        _capabilities(host.get('CapDrop')) != {'ALL'}, _capabilities(host.get('CapAdd')) != caps,
        not any(str(value).startswith('no-new-privileges') for value in security),
        (host.get('Tmpfs') or {}) != {'/tmp': TMPFS},
        mounts != expected_mounts or not nocopy,
        host.get('NetworkMode') != networks['frontend'], attached != wanted_networks,
        restart != ('unless-stopped' if hosted else 'no'),
    ]
    publish, device, extra_hosts = '', '', {}
    bindings = host.get('PortBindings') or {}
    if role == 'runtime':
        problems.append(bool(bindings))
    else:
        entries = bindings.get(PORTS[role]) or []
        address = str((entries[0] if len(entries) == 1 else {}).get('HostIp') or '')
        port = str((entries[0] if len(entries) == 1 else {}).get('HostPort') or '')
        allowed = {'127.0.0.1', '0.0.0.0'} if hosted else {'127.0.0.1'}
        problems.append(set(bindings) != {PORTS[role]} or address not in allowed
                        or not port.isdecimal() or not 0 < int(port) < 65536)
        publish = f'{address}:{port}:{PORTS[role].split("/")[0]}'
    devices = host.get('Devices') or []
    if hosted and role == 'runtime':
        item = devices[0] if len(devices) == 1 and isinstance(devices[0], dict) else {}
        device = str(item.get('PathOnHost') or '')
        problems.append(not re.fullmatch(r'/dev/[A-Za-z0-9._/-]+', device) or os.path.normpath(device) != device
                        or item.get('PathInContainer') != device or item.get('CgroupPermissions') != 'r')
    else:
        problems.append(bool(devices))
    for item in host.get('ExtraHosts') or []:
        hostname, _, address = str(item).partition(':')
        if not hosted or not re.fullmatch(r'[a-z0-9.-]{1,253}', hostname) or not re.fullmatch(r'[a-z0-9.:-]{1,64}', address):
            problems.append(True)
        extra_hosts[hostname] = address
    if any(problems):
        raise UpgradeError(f'The {role} container differs from the supported package shape; '
                           'upgrade supports only packages created by this launcher')
    return {'id': record['Id'], 'image': str(record.get('Image') or ''),
            'running': (record.get('State') or {}).get('Running') is True,
            'publish': publish, 'device': device, 'extra_hosts': extra_hosts}


def _inspect(endpoint: str, identity: str) -> dict | None:
    result = _docker(endpoint, 'inspect', '--format', '{{json .}}', identity)
    if result.returncode:
        return None
    value = json.loads(result.stdout)
    return value if isinstance(value, dict) else None


def _read_config(endpoint: str, container: str, role: str) -> dict:
    result = _docker(endpoint, 'cp', f'{container}:/{STATE[role]}/config.json', '-')
    if result.returncode:
        raise UpgradeError(f'The {role} configuration could not be read')
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        member = archive.getmember('config.json')
        value = json.loads(archive.extractfile(member).read())
    if set(value) != {'profile', 'environment'} or not isinstance(value['environment'], dict):
        raise UpgradeError(f'The {role} configuration is not a packaged configuration')
    return value


def effective_models(environment: dict, prefer=()) -> dict[str, str]:
    """The exact models the runtime environment names, under the profile names the operator used."""
    chosen = {}
    for profile in [*prefer, *base.MODEL_ENVIRONMENTS]:
        variable = base.MODEL_ENVIRONMENTS.get(profile)
        if variable in environment and variable not in chosen.values():
            chosen[profile] = variable
    return {profile: environment[variable] for profile, variable in chosen.items()}


def _helper(endpoint: str, *, image: str, name: str, txn: str, purpose: str, mounts: list[str], command: list[str],
            entrypoint: str, device: str = '', docker_socket: bool = False, timeout: float = 1800,
            scratch: str = '256m') -> subprocess.CompletedProcess:
    helper = f'{name}-upgrade-{txn}-{purpose}'
    if _inspect(endpoint, helper) is not None:  # left by an interrupted command of this transaction
        _docker(endpoint, 'rm', '--force', helper)
    args = ['run', '--rm', '--name', helper, '--network', 'none', '--user', '0:0', '--read-only',
            '--tmpfs', f'/tmp:rw,nosuid,nodev,size={scratch}', '--cap-drop', 'ALL',
            '--cap-add', 'CHOWN', '--cap-add', 'FOWNER', '--cap-add', 'DAC_OVERRIDE',
            '--security-opt', 'no-new-privileges',
            '--label', 'xperfect.package=' + name, '--label', 'xperfect.upgrade=' + txn]
    if device:
        args += ['--cap-add', 'SYS_ADMIN', '--device', f'{device}:{device}:r']
    for mount in mounts:
        args += ['--mount', mount]
    if docker_socket:
        args += ['--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock']
    try:
        return _docker(endpoint, *args, '--entrypoint', entrypoint, image, *command, timeout=timeout)
    except subprocess.TimeoutExpired:
        # The CLI timing out does not stop the container; stop this helper too.
        _docker(endpoint, 'rm', '--force', helper)
        raise UpgradeError(f'The upgrade {purpose} step timed out and was stopped') from None


class Upgrade:
    def __init__(self, endpoint: str, receipt_path: Path, *, ca_file: Path | None = None,
                 ready_timeout: float = 90.0):
        if not endpoint.startswith('unix:///'):
            raise UpgradeError('Select an explicit local Unix Docker endpoint')
        self.endpoint, self.receipt_path = endpoint, receipt_path
        self.journal_path = journal_path(receipt_path)
        self.ca_file, self.ready_timeout = ca_file, ready_timeout

    # -- shared helpers ---------------------------------------------------
    def _image_present(self, image: str) -> bool:
        result = _docker(self.endpoint, 'image', 'inspect', '--format', '{{.Id}}', image)
        return result.returncode == 0 and result.stdout.decode().strip() == image

    def _save(self, journal: dict, *, create: bool = False) -> None:
        _write_private_json(self.journal_path, journal, create=create)

    def _complete_legacy_receipt(self, receipt: dict) -> dict[str, str]:
        """Complete a receipt written before the launcher recorded the links volume.

        The missing volume is taken only from what the package itself shows: all
        three role containers, labelled for this package and role, mount the one
        volume ``<name>-links`` at /links, and that volume carries the package label.
        Anything else is refused; the container shape is checked in full afterwards.
        """
        volumes = receipt.get('volumes')
        if not isinstance(volumes, dict) or 'links' in volumes:
            return {}
        required = {'data', 'control', 'ui-state', 'mcp-state'} | (
            {'auth'} if receipt.get('profile') == 'hosted-xfs' else set())
        control = str(volumes.get('control') or '')
        name = control[:-len('-control')] if control.endswith('-control') else ''
        if set(volumes) != required or not re.fullmatch(r'xperfect-[a-z0-9][a-z0-9-]{0,40}', name):
            return {}  # not the earlier receipt format; validation reports it
        links = f'{name}-links'
        for role in ROLES:
            record = _inspect(self.endpoint, f'{name}-{role}') or {}
            labels = (record.get('Config') or {}).get('Labels') or {}
            mounted = [(item.get('Type'), item.get('Source')) for item in (record.get('HostConfig') or {}).get('Mounts') or []
                       if item.get('Target') == '/links']
            if labels.get('xperfect.package') != name or labels.get('xperfect.role') != role or mounted != [('volume', links)]:
                raise UpgradeError('The receipt predates the links volume, and the running package does not show '
                                   f'exactly one {links} volume on every role. Nothing was changed')
        result = _docker(self.endpoint, 'volume', 'inspect', '--format', '{{json .Labels}}', links)
        try:
            labels = json.loads(result.stdout) if result.returncode == 0 else None
        except ValueError:
            labels = None
        if not isinstance(labels, dict) or labels.get('xperfect.package') != name:
            raise UpgradeError(f'The {links} volume is not labelled for this package. Nothing was changed')
        return {'links': links}

    def _state_mounts(self, receipt: dict, backup: str, profile: str) -> tuple[list[str], list[str]]:
        roles = ['control', 'ui-state', 'mcp-state', 'links'] + (['auth'] if profile == 'hosted-xfs' else [])
        mounts = [f'type=volume,src={receipt["volumes"][role]},dst=/state/{role}' for role in roles]
        return roles, mounts + [f'type=volume,src={backup},dst=/backup']

    def _idle_reason(self, *, receipt: dict, name: str, txn: str, image: str, program: str, device: str,
                     runtime: str, running: bool, purpose: str) -> str | None:
        """None when this proof passes (exit status 0 only); otherwise its typed reason ('' if none)."""
        if running:  # the running release, inside its own container
            result = _docker(self.endpoint, 'exec', runtime, RUNTIME_PYTHON, '-I', '-c', program, timeout=300)
        else:
            volumes = receipt['volumes']
            result = _helper(self.endpoint, image=image, name=name, txn=txn, purpose=purpose,
                             entrypoint=RUNTIME_PYTHON, command=['-I', '-c', program], device=device,
                             docker_socket=True, timeout=300,
                             # The incoming review copies the database into its scratch space.
                             scratch='1g' if program == INCOMING_QUIESCENT else '256m',
                             mounts=[f'type=volume,src={volumes["control"]},dst=/control',
                                     f'type=volume,src={volumes["data"]},dst=/data'])
        return None if result.returncode == 0 else _reason(result.stderr)

    def _proofs(self, proof: dict, *, stopped: bool) -> list[tuple[str, str, str]]:
        """(image, program, purpose) of every proof that must pass for this recorded idle proof."""
        phase = 'stopped' if stopped else 'live'
        if not proof['incoming']:
            return [(proof['image'], QUIESCENT, f'{phase}-running')]
        # The running release stays the authority on its own work (checked first: it is
        # cheap), and the incoming release reviews the schema it inherits.
        return [(proof['running_image'], RUNNING_ACTIVITY, f'{phase}-activity'),
                (proof['image'], INCOMING_QUIESCENT, f'{phase}-incoming')]

    def _recover_unpublished(self, journal: dict, *, receipt: dict, name: str, txn: str,
                             current_image: str, service_image: str, device: str) -> list[str]:
        """Retire stored-file attachments the running release registered but could never publish.

        An earlier release registered a Files projection before checking its signed source and,
        without the package's source key, always failed that check. The target then blocks every
        idle proof, because only publication settles it. The new release's reviewed continuity
        code decides from the live state, under the runtime's projection lock and only while this
        runtime still cannot authorize any source, that nothing was or can be published, then
        retires exactly the reviewed targets or none. The attachment and its stored upload stay;
        the unchanged idle proofs run next. Returns the retired projection identities.
        """
        if service_image == current_image:
            raise UpgradeError('Recovering unpublished files needs a new image that carries the recovery. '
                               'Nothing was changed')
        volumes = receipt['volumes']
        record = self.receipt_path.with_name(f'{self.receipt_path.name}.files-recovery-{txn}.json')
        mounts = [f'type=volume,src={volumes["control"]},dst=/control',
                  f'type=volume,src={volumes["data"]},dst=/data']

        def run(program: str, purpose: str) -> subprocess.CompletedProcess:
            return _helper(self.endpoint, image=service_image, name=name, txn=txn, purpose=purpose,
                           entrypoint=RUNTIME_PYTHON, command=['-I', '-c', program], device=device,
                           timeout=300, scratch='1g', mounts=mounts)

        def report_of(result: subprocess.CompletedProcess, *, applied: bool) -> list[dict]:
            try:
                report = json.loads(result.stdout)
                targets = report['targets']
                valid = (type(report['unsettled']) is int and type(report['applied']) is bool
                         and isinstance(targets, list) and len(targets) == report['unsettled']
                         and all(isinstance(item, dict) and PROJECTION.fullmatch(str(item.get('projection_id')))
                                 for item in targets)
                         and report['applied'] is (applied and bool(targets)))
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise UpgradeError('The new image returned an unexpected recovery report'
                                   + ('; whether files were retired is unknown' if applied else '. Nothing was changed'))
            return targets

        result = run(UNPUBLISHED_REPORT, 'files-review')
        if result.returncode == 2 and b'invalid choice' in result.stderr:
            raise UpgradeError('Unpublished files cannot be recovered: the new image does not carry this '
                               'recovery. Nothing was changed')
        if result.returncode:  # the review only reads
            raise UpgradeError('Unpublished files cannot be recovered: '
                               + (_reason(result.stderr) or 'the review stopped') + '. Nothing was changed')
        targets = report_of(result, applied=False)
        if not targets:
            return []
        reviewed = sorted(item['projection_id'] for item in targets)
        # Identities and content hashes only: no names, owners or paths.
        evidence = {'transaction': txn, 'status': 'retiring',
                    'reviewed': [{key: item.get(key) for key in ('projection_id', 'upload_id', 'sha256', 'size_bytes')}
                                 for item in targets]}
        _write_private_json(record, evidence, create=True)
        journal['files_recovering'] = reviewed
        self._save(journal)
        try:
            result = run(unpublished_program(reviewed), 'files-recover')
        except UpgradeError:  # timed out and stopped at an unknown point
            raise UpgradeError('Retiring unpublished files did not finish in time; whether they were retired is '
                               f'unknown ({record.name}). Nothing else was changed; run the upgrade again') from None
        if result.returncode:
            reason = _reason(result.stderr)
            if reason:  # every typed refusal comes before the transaction commits
                _write_private_json(record, {**evidence, 'status': 'refused'})
                journal['files_recovering'] = []
                raise UpgradeError(f'Unpublished files cannot be recovered: {reason}. Nothing was changed')
            raise UpgradeError('Retiring unpublished files stopped without a result; whether they were retired '
                               f'is unknown ({record.name}). Nothing else was changed; run the upgrade again')
        if sorted(item['projection_id'] for item in report_of(result, applied=True)) != reviewed:
            raise UpgradeError('The new image retired a different set than it reviewed; stop and inspect '
                               f'{record.name}')
        _write_private_json(record, {**evidence, 'status': 'retired', 'retired': reviewed})
        return reviewed

    def _prove_idle(self, *, receipt: dict, name: str, txn: str, current_image: str, service_image: str,
                    device: str, runtime: str, running: bool) -> dict:
        """One complete continuity proof: the running release proves its own state idle, or the
        release about to take over proves the state it inherits (``--incoming``). Either proof
        reviews the whole schema and reads idle from the live state; which one passed is kept,
        and the same one proves it again once the package is stopped."""
        candidates = [{'image': current_image, 'incoming': False}] + (
            [{'image': service_image, 'incoming': True, 'running_image': current_image}]
            if service_image != current_image else [])
        reasons = []
        for proof in candidates:
            for image, program, purpose in self._proofs(proof, stopped=False):
                reason = self._idle_reason(receipt=receipt, name=name, txn=txn, image=image, program=program,
                                           device=device, runtime=runtime,
                                           running=running and image == current_image, purpose=purpose)
                if reason is not None:
                    label = {QUIESCENT: 'running version', RUNNING_ACTIVITY: "running version's own work",
                             INCOMING_QUIESCENT: 'new version'}[program]
                    reasons.append(f'{label}: ' + (reason or 'its state could not be proved idle'))
                    break
            else:
                return proof
        raise UpgradeError(f'The package is not idle or its state cannot be reviewed ({"; ".join(reasons)}). '
                           'Finish, stop or close active work (or wait for idle workspaces to be released), '
                           'then retry. Nothing was changed.')

    def _runtime_healthy(self, runtime: str) -> bool:
        result = _docker(self.endpoint, 'exec', runtime, RUNTIME_PYTHON, '-I', '-c', HEALTH, timeout=15)
        try:
            return result.returncode == 0 and json.loads(result.stdout).get('status') == 'ok'
        except ValueError:
            return False

    def _wait_ready(self, receipt: dict, profile: str, containers: dict[str, str], publish: dict[str, str]) -> None:
        deadline = time.monotonic() + self.ready_timeout
        if profile == 'hosted-xfs':
            context = ssl.create_default_context(cafile=str(self.ca_file) if self.ca_file else None)
            base._wait_until_hosted_ready(self.endpoint, containers, receipt['public_url'], receipt['mcp_url'],
                                          context, timeout_s=self.ready_timeout)
        else:
            address, port, _ = publish['ui'].split(':')
            base._wait_until_runnable(self.endpoint, containers, int(port),
                                      host='127.0.0.1' if address == '0.0.0.0' else address,
                                      timeout_s=self.ready_timeout)
        while not self._runtime_healthy(containers['runtime']):
            if time.monotonic() >= deadline:
                raise UpgradeError('The runtime API did not answer its health check')
            time.sleep(1)

    def _stop(self, identity: str) -> None:
        _ok(self.endpoint, 'stop', '--time', '30', identity, timeout=120)

    def _image_settings(self, image: str, profile: str, *, name: str, txn: str) -> dict[str, str]:
        """The profile settings the image itself declares; none for an image without the table."""
        result = _helper(self.endpoint, image=image, name=name, txn=txn, purpose='settings', mounts=[],
                         entrypoint='python3', command=['-I', '-c', IMAGE_SETTINGS], timeout=120)
        try:
            table = json.loads(result.stdout) if result.returncode == 0 else ...
        except ValueError:
            table = ...
        if table is ...:
            raise UpgradeError("The new image's package settings could not be read")
        if table is None:
            return {}
        values = table.get(profile) if isinstance(table, dict) else None
        if (not isinstance(values, dict) or 'GLASSHIVE_SECURITY_MODE' not in values
                or any(not isinstance(k, str) or not SETTING_NAME.fullmatch(k) or not isinstance(v, str)
                       or len(v) > 200 or '\0' in v for k, v in values.items())):
            raise UpgradeError("The new image's package settings are malformed")
        return dict(values)

    def _workers_isolated(self, network: str) -> bool | None:
        """Whether the workers bridge refuses traffic between containers; None if unreadable."""
        result = _docker(self.endpoint, 'network', 'inspect', '--format', '{{json .Options}}', network)
        try:
            options = json.loads(result.stdout) if result.returncode == 0 else None
        except ValueError:
            options = None
        if options is None and result.returncode == 0:
            options = {}
        return None if not isinstance(options, dict) else options.get(base.ICC_OPTION) == 'false'

    def _set_workers_network(self, receipt: dict, name: str, runtime: str, *, isolated: bool) -> None:
        """Recreate the idle workers bridge with or without isolation; resumable.

        The package is proved idle and stopped, so no workspace or account container
        uses the bridge; only the stopped runtime is attached, and it is reconnected
        under its alias whenever the previous version returns.
        """
        network, endpoint = receipt['networks']['workers'], self.endpoint
        current = self._workers_isolated(network)
        if current is not None and current != isolated:
            listing = _ok(endpoint, 'ps', '--all', '--no-trunc', '--filter', 'network=' + network, '--format', '{{.ID}}')
            if {line.strip() for line in listing.splitlines() if line.strip()} - {runtime}:
                raise UpgradeError('A workspace or account container still uses the workers network. Finish or '
                                   'close that work, then retry')
            attached = ((_inspect(endpoint, runtime) or {}).get('NetworkSettings') or {}).get('Networks') or {}
            if network in attached:
                _ok(endpoint, 'network', 'disconnect', network, runtime)
            _ok(endpoint, 'network', 'rm', network)
            current = None
        if current is None:
            _ok(endpoint, *base.network_create_args(
                name=name, role='workers', network=network,
                settings={'XPERFECT_WORKER_NETWORK': 'isolated'} if isolated else {}))
        attached = ((_inspect(endpoint, runtime) or {}).get('NetworkSettings') or {}).get('Networks') or {}
        if not isolated and network not in attached:
            _ok(endpoint, 'network', 'connect', '--alias', 'runtime', network, runtime)

    def _docker_desktop(self) -> bool:
        """Whether the endpoint itself reports Docker Desktop (whose socket bind is recorded differently)."""
        result = _docker(self.endpoint, 'info', '--format', '{{.OperatingSystem}}')
        return result.returncode == 0 and 'Docker Desktop' in result.stdout.decode(errors='replace')

    def _running(self, identity: str) -> bool:
        return ((_inspect(self.endpoint, identity) or {}).get('State') or {}).get('Running') is True

    def _data_containers(self, receipt: dict) -> set[str]:
        """Every container, running or not, that mounts the package's owner data."""
        listing = _ok(self.endpoint, 'ps', '--all', '--no-trunc', '--filter', 'volume=' + receipt['volumes']['data'],
                      '--format', '{{.ID}}')
        return {line.strip() for line in listing.splitlines() if line.strip()}

    def _require_backup(self, journal: dict) -> None:
        result = _docker(self.endpoint, 'volume', 'inspect', '--format', '{{json .Labels}}', journal['backup_volume'])
        try:
            labels = json.loads(result.stdout) if result.returncode == 0 else None
        except ValueError:
            labels = None
        if (not isinstance(labels, dict) or labels.get('xperfect.upgrade') != journal['transaction']
                or labels.get('xperfect.package') != journal['name']):
            raise UpgradeError("This upgrade's backup volume is missing or is not its own, so the previous service "
                               'state cannot be restored. Nothing was rolled back; keep the upgraded version or '
                               'restore from a backup')

    def _new_identities(self, journal: dict) -> dict[str, list[str]]:
        """The new version's existing containers, including one created before its journal write."""
        name, previous = journal['name'], journal['previous_containers']
        found = {}
        for role in ROLES:
            identities = {journal['new_containers'].get(role)}
            holder = _inspect(self.endpoint, f'{name}-{role}')
            if (holder and holder.get('Id') != previous[role]
                    and ((holder.get('Config') or {}).get('Labels') or {}).get('xperfect.package') == name):
                identities.add(holder['Id'])
            found[role] = [identity for identity in sorted(identities - {None, previous[role]})
                           if _inspect(self.endpoint, identity) is not None]
        return found

    def _work_since_upgrade(self, journal: dict, new: dict[str, list[str]]) -> int:
        if 'data_containers' not in journal:  # nothing of the new version was created yet
            return 0
        known = set(journal['data_containers']) | {identity for ids in new.values() for identity in ids}
        return len(self._data_containers(journal['previous_receipt']) - known)

    def _stop_new_version(self, journal: dict, new: dict[str, list[str]]) -> tuple[str, list[str]]:
        """Stop the new version only when a rollback would orphan none of its work."""
        if not any(new.values()):
            return 'no new version', []
        proof = 'workspace containers'
        for identity in new['runtime']:
            if not self._running(identity):
                continue
            try:
                result = _docker(self.endpoint, 'exec', identity, RUNTIME_PYTHON, '-I', '-c', QUIESCENT, timeout=300)
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                proof = 'runtime records and workspace containers'
                continue
            reason = _reason(result.stderr) if result is not None else ''
            if reason:
                raise UpgradeError(f'The new version is not idle: {reason}. Finish or stop that work in the new '
                                   'version, then rerun upgrade-rollback. Nothing was rolled back.')
            # A new runtime that cannot run its own check leaves the Docker evidence below.

        def refuse(count: int) -> UpgradeError:
            return UpgradeError(f'The new version created {count} workspace container(s) that a rollback would '
                                'orphan. Close that work in the new version (terminate its workers), then rerun '
                                'upgrade-rollback. Nothing was rolled back.')
        work = self._work_since_upgrade(journal, new)
        if work:
            raise refuse(work)
        running = [identity for role in ('ui', 'mcp', 'runtime') for identity in new[role] if self._running(identity)]
        for identity in running:
            self._stop(identity)
        work = self._work_since_upgrade(journal, new)
        if work:  # started between the check and the stop: serve the new version again
            for identity in reversed(running):
                _docker(self.endpoint, 'start', identity)
            raise refuse(work)
        return proof, running

    def _state_tool(self, journal: dict, action: str) -> dict:
        roles, mounts = self._state_mounts(journal['previous_receipt'], journal['backup_volume'], journal['profile'])
        result = _helper(self.endpoint, image=journal['previous_image'], name=journal['name'],
                         txn=journal['transaction'], purpose=action, mounts=mounts, entrypoint='python3',
                         command=['-I', '-c', STATE_TOOL, action, ','.join(roles), json.dumps(journal['snapshot'])])
        if result.returncode:
            error = UpgradeError(_reason(result.stderr) or f'The service state {action} did not finish')
            error.unchanged = result.returncode == STATE_REFUSED
            raise error
        return json.loads(result.stdout)

    # -- upgrade ----------------------------------------------------------
    def upgrade(self, *, service_image: str, native_image: str | None = None, models: dict | None = None,
                adopt: bool = False, recover_unpublished: bool = False, role_map: dict | None = None,
                role_claim: str | None = None, clear_roles: bool = False,
                local_qa_authority: dict | None = None, clear_local_qa: bool = False) -> dict:
        models = base.validate_models(models)
        if local_qa_authority is not None and clear_local_qa:
            raise UpgradeError('Choose --local-qa-authority or --no-local-qa-authority, not both')
        qa_environment = None if local_qa_authority is None else validate_local_qa_authority(local_qa_authority)
        for value in [service_image] + ([native_image] if native_image else []):
            if not SHA.fullmatch(value):
                raise UpgradeError('Exact locally verified image identities (sha256:…) are required')
        receipt = _private_json(self.receipt_path)
        completed = self._complete_legacy_receipt(receipt)
        if completed:
            receipt = {**receipt, 'volumes': {**receipt['volumes'], **completed}}
        profile, name = validate_receipt(receipt)
        if (qa_environment is not None or clear_local_qa) and profile != 'local-linux':
            raise UpgradeError('The local-QA authority is available only for a local package. Nothing was changed')
        if self.journal_path.exists():
            raise UpgradeError('An upgrade is already open for this package; run upgrade-commit or upgrade-rollback')
        for image in [service_image] + ([native_image] if native_image else []):
            if not self._image_present(image):
                raise UpgradeError(f'Image identity could not be verified: load {image} on this Docker host first')
        shapes, desktop = {}, self._docker_desktop()
        for role in ROLES:
            record = _inspect(self.endpoint, f'{name}-{role}')
            if record is None:
                raise UpgradeError(f'The {role} container {name}-{role} was not found')
            shapes[role] = role_shape(record, profile=profile, name=name, role=role, docker_desktop=desktop,
                                      volumes=receipt['volumes'], networks=receipt['networks'])
            if shapes[role]['id'] != receipt['containers'][role] and not adopt:
                raise UpgradeError(f'The running {role} container is not the one in the receipt. If this package '
                                   'was changed outside the launcher and these containers are yours, rerun with '
                                   '--adopt-running-containers')
        current_images = {shape['image'] for shape in shapes.values()}
        if len(current_images) != 1:
            raise UpgradeError('The package roles run different images; restore one coherent image first')
        current_image = current_images.pop()
        if not adopt and current_image != receipt['service_image']:
            raise UpgradeError('The running image differs from the receipt; rerun with --adopt-running-containers')
        if not self._image_present(current_image):
            raise UpgradeError('The current image is unavailable, so a rollback could not start it')
        configs = {role: _read_config(self.endpoint, shapes[role]['id'], role) for role in ROLES}
        if any(config['profile'] != profile for config in configs.values()):
            raise UpgradeError('A role configuration belongs to another package profile')
        # The identity-provider role mapping is an explicit operator choice kept in the UI and MCP.
        changing_roles = role_map is not None or role_claim is not None or clear_roles
        if changing_roles and profile != 'hosted-xfs':
            raise UpgradeError('Identity-provider role mapping applies to hosted packages only')
        if clear_roles and (role_map is not None or role_claim is not None):
            raise UpgradeError('Choose a role map or no role map, not both')
        try:
            mappings = [base.role_mapping_of(configs[role]['environment']) for role in ('ui', 'mcp')]
            unreadable = False
        except ValueError:
            mappings, unreadable = [None, None], True
        if (unreadable or mappings[0] != mappings[1]) and not changing_roles:
            raise UpgradeError('The UI and MCP identity-provider role mappings are unreadable or differ. Nothing was '
                               'changed; set one with --role-map or remove it with --no-role-map')
        current_roles = None if unreadable or mappings[0] != mappings[1] else mappings[0]
        # A local package from an earlier launcher lacks the signed-in owner's confirmation channel.
        local_signer = {}
        if profile == 'local-linux':
            try:
                existing_kid = base.local_assertion_status(configs['ui']['environment'],
                                                           configs['runtime']['environment'],
                                                           configs['mcp']['environment'])
            except ValueError as error:
                raise UpgradeError(f'{error}. Nothing was changed') from None
            if existing_kid is None:
                port = shapes['ui']['publish'].split(':')[1]
                local_signer = {'kid': 'xperfect-local-ui-' + secrets.token_hex(8),
                                'ui_url': f'http://127.0.0.1:{port}'}
        if clear_roles:
            wanted_roles = None
        elif changing_roles:
            # A new map keeps the current claim unless another is named.
            claim = role_claim if role_claim is not None else (current_roles or {}).get('claim')
            new_map = role_map if role_map is not None else (current_roles or {}).get('map')
            try:
                wanted_roles = base.validate_role_mapping(claim, new_map)
            except ValueError as error:
                raise UpgradeError(f'{error}. Nothing was changed') from None
        else:
            wanted_roles = current_roles
        if changing_roles and (unreadable or mappings[0] != mappings[1]):
            current_roles = {'unreadable_or_differing': True}  # always rewritten below
        environment = dict(configs['runtime']['environment'])
        txn = uuid.uuid4().hex[:12]
        # Settings the new image declares for this profile that an earlier launcher
        # did not write. A key any role already holds with another value is an
        # operator choice: it is added to no role and reported.
        settings = self._image_settings(service_image, profile, name=name, txn=txn)
        if settings and any('GLASSHIVE_SECURITY_MODE' not in config['environment'] for config in configs.values()):
            raise UpgradeError('A role configuration has no security mode, so it was not written by a known launcher '
                               'and its settings cannot be completed')
        settings_kept = sorted(key for key, value in settings.items()
                               if any(config['environment'].get(key, value) != value for config in configs.values()))
        settings_added = {role: sorted(set(settings) - set(config['environment']) - set(settings_kept))
                          for role, config in configs.items()}
        settings_added = {role: keys for role, keys in settings_added.items() if keys}
        # Per-package secrets an earlier launcher did not generate. Only their names are
        # journaled or reported; each value is generated as its role's config is written.
        secrets_added = {role: [key for key in base.PROFILE_SECRETS[profile].get(role, ())
                                if not str(config['environment'].get(key) or '').strip()]
                         for role, config in configs.items()}
        secrets_added = {role: keys for role, keys in secrets_added.items() if keys}
        current_native = str(environment.get('XPERFECT_SHARED_IMAGE') or '')
        wanted_native = native_image or current_native
        if not native_image and not (SHA.fullmatch(current_native) and self._image_present(current_native)):
            raise UpgradeError('The configured workspace image is not loaded on this Docker host; load it or '
                               'choose one with --native-image')
        changes_models = any(environment.get(base.MODEL_ENVIRONMENTS[p]) != m for p, m in models.items())
        # An image that serves native tools through per-box sockets runs on a workers
        # bridge that refuses traffic between containers; an earlier one needs that traffic.
        isolated = self._workers_isolated(receipt['networks']['workers'])
        if isolated is None:
            raise UpgradeError("The package's workers network could not be inspected. Nothing was changed")
        wants_isolated = settings.get('XPERFECT_WORKER_NETWORK') == 'isolated'
        if isolated and not wants_isolated:
            raise UpgradeError('This package isolates its workspace containers from each other, and the new image '
                               'cannot serve its workers that way. Choose an image that declares an isolated '
                               'worker network. Nothing was changed')
        isolate = wants_isolated and not isolated
        changes_qa = (any(environment.get(key) != value for key, value in qa_environment.items())
                      if qa_environment is not None
                      else clear_local_qa and bool(LOCAL_QA_AUTHORITY_KEYS & set(environment)))
        if (service_image == current_image and wanted_native == current_native and not changes_models
                and not settings_added and not secrets_added and wanted_roles == current_roles
                and not local_signer and not isolate and not changes_qa):
            raise UpgradeError('The package already runs this image and configuration')
        backup = f'{name}-upgrade-{txn}'
        journal = {'version': 1, 'transaction': txn, 'phase': 'prepared', 'profile': profile, 'name': name,
                   'previous_receipt': receipt, 'previous_receipt_text': self.receipt_path.read_text(),
                   'previous_image': current_image,
                   'previous_containers': {role: shape['id'] for role, shape in shapes.items()},
                   'renamed': {}, 'new_containers': {}, 'backup_volume': backup, 'snapshot': {},
                   'receipt_completed': sorted(completed), 'settings_added': settings_added,
                   'settings': {key: settings[key] for keys in settings_added.values() for key in keys},
                   'settings_kept': settings_kept, 'secrets_added': secrets_added,
                   'role_mapping': {'from': current_roles, 'to': wanted_roles},
                   'local_assertion': local_signer, 'isolate_workers_network': isolate,
                   'local_qa_authority': ('set' if qa_environment is not None
                                          else 'cleared' if changes_qa else 'unchanged'),
                   'service_image': service_image, 'native_image': wanted_native,
                   'previous_native_image': current_native,
                   'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        self._save(journal, create=True)
        journal['files_recovered'] = []
        try:
            if recover_unpublished:
                journal['files_recovered'] = self._recover_unpublished(
                    journal, receipt=receipt, name=name, txn=txn, current_image=current_image,
                    service_image=service_image, device=shapes['runtime']['device'])
                journal['files_recovering'] = []
                self._save(journal)
            journal['idle_proof'] = self._prove_idle(
                receipt=receipt, name=name, txn=txn, current_image=current_image, service_image=service_image,
                device=shapes['runtime']['device'], runtime=shapes['runtime']['id'],
                running=shapes['runtime']['running'])
            self._save(journal)
        except BaseException as exc:
            self.journal_path.unlink()  # the package itself was not changed
            if journal['files_recovered'] and isinstance(exc, UpgradeError):
                raise UpgradeError(str(exc).removesuffix(' Nothing was changed.') + ' The unpublished file '
                                   f'attachments were already retired ({len(journal["files_recovered"])}); '
                                   'the package was otherwise not changed') from None
            raise
        try:
            return self._apply(journal, receipt, profile, name, shapes, environment, models, configs,
                               qa_environment=qa_environment, clear_local_qa=clear_local_qa)
        except BaseException as exc:
            if self.journal_path.exists():
                try:
                    self.rollback()
                except BaseException as stopped:
                    raise UpgradeError(f'Upgrade failed ({_detail(exc)}) and automatic rollback stopped: '
                                       f'{_detail(stopped)} Run upgrade-rollback once that is resolved') from exc
                raise UpgradeError(f'Upgrade failed and the previous version is running again: {_detail(exc)}') from exc
            raise

    def _apply(self, journal, receipt, profile, name, shapes, environment, models, configs, *,
               qa_environment: dict | None = None, clear_local_qa: bool = False) -> dict:
        endpoint, txn = self.endpoint, journal['transaction']
        # UI and MCP first, so no new person or client work starts; then the runtime.
        for role in ('ui', 'mcp', 'runtime'):
            if shapes[role]['running']:
                self._stop(shapes[role]['id'])
        journal['phase'] = 'stopped'
        self._save(journal)
        for image, program, purpose in self._proofs(journal['idle_proof'], stopped=True):
            reason = self._idle_reason(receipt=receipt, name=name, txn=txn, image=image, program=program,
                                       device=shapes['runtime']['device'], runtime=shapes['runtime']['id'],
                                       running=False, purpose=purpose)
            if reason is not None:
                raise UpgradeError('The package is not idle: ' + (reason or 'its state could not be proved idle')
                                   + '. Finish, stop or close active work, then retry')
        # What already uses owner data; a rollback refuses anything the new version adds.
        journal['data_containers'] = sorted(self._data_containers(receipt))
        self._save(journal)
        _ok(endpoint, 'volume', 'create', '--label', 'xperfect.package=' + name,
            '--label', 'xperfect.upgrade=' + txn, journal['backup_volume'])
        journal['phase'] = 'backup_created'
        self._save(journal)
        roles, mounts = self._state_mounts(receipt, journal['backup_volume'], profile)
        result = _helper(endpoint, image=journal['previous_image'], name=name, txn=txn, purpose='snapshot',
                         mounts=mounts, entrypoint='python3',
                         command=['-I', '-c', STATE_TOOL, 'snapshot', ','.join(roles), '{}'])
        if result.returncode:
            raise UpgradeError(_reason(result.stderr) or 'The service state could not be copied')
        journal['snapshot'] = json.loads(result.stdout)
        journal['phase'] = 'snapshotted'
        self._save(journal)
        for role in ROLES:
            renamed = f'{name}-{role}.pre-upgrade-{txn}'
            _ok(endpoint, 'rename', shapes[role]['id'], renamed)
            journal['renamed'][role] = renamed
            self._save(journal)
        journal['phase'] = 'renamed'
        self._save(journal)
        if journal.get('isolate_workers_network'):
            self._set_workers_network(receipt, name, journal['previous_containers']['runtime'], isolated=True)
        settings = journal['settings']
        generated = {role: base.new_secrets(profile, role, journal.get('secrets_added', {}).get(role, []))
                     for role in ROLES}
        signer, jwks = journal.get('local_assertion') or {}, None
        if signer:
            # After the snapshot, so a rollback also removes this key from the UI's state.
            try:
                jwk = base.generate_signer(endpoint, journal['service_image'], receipt['volumes']['ui-state'],
                                           '/ui-state', signer['kid'])
            except (RuntimeError, ValueError) as error:
                raise UpgradeError('The UI signing key could not be created in its state volume') from error
            jwks = json.dumps({'keys': [jwk]}).encode()
            for role in ('runtime', 'ui'):
                generated[role].update(base.local_assertion_environment(role, kid=signer['kid'],
                                                                        ui_url=signer['ui_url']))
        environment = {**environment, **{key: settings[key] for key in journal['settings_added'].get('runtime', [])},
                       **generated['runtime'], **base.model_environment(models)}
        if qa_environment is not None or clear_local_qa:
            environment = {key: value for key, value in environment.items() if key not in LOCAL_QA_AUTHORITY_KEYS}
            environment.update(qa_environment or {})
        environment['XPERFECT_SHARED_IMAGE'] = journal['native_image']
        created = {}
        for role in ROLES:
            args = base.role_create_args(profile=profile, name=name, role=role, volumes=receipt['volumes'],
                                         networks=receipt['networks'], publish=shapes[role]['publish'],
                                         extra_hosts=shapes[role]['extra_hosts'], device=shapes['runtime']['device'])
            created[role] = _ok(endpoint, *args, journal['service_image'], *base.role_command(role))
            if not ID.fullmatch(created[role]):
                raise UpgradeError('Docker returned an unexpected container identity')
            journal['new_containers'][role] = created[role]
            self._save(journal)
        environment['XPERFECT_CONTROLLER_ID'] = created['runtime']
        if jwks is not None:  # the runtime reads its public key set when it starts
            _ok(endpoint, 'cp', '-', created['runtime'] + ':/control',
                data=base._file_archive('assertion-jwks.json', jwks, 0o644))
        _ok(endpoint, 'cp', '-', created['runtime'] + ':/control',
            data=base.archive_config({'profile': profile, 'environment': environment}))
        role_change = journal.get('role_mapping') or {}
        roles_changed = role_change.get('from') != role_change.get('to')
        for role in ('ui', 'mcp'):
            if journal['settings_added'].get(role) or generated[role] or roles_changed:
                role_environment = {**configs[role]['environment'],
                                    **{key: settings[key] for key in journal['settings_added'].get(role, [])},
                                    **generated[role]}
                if roles_changed:
                    for key in base.ROLE_ENVIRONMENT:
                        role_environment.pop(key, None)
                    role_environment.update(base.role_environment(role_change.get('to')))
                _ok(endpoint, 'cp', '-', f'{created[role]}:/{STATE[role]}',
                    data=base.archive_config({'profile': profile, 'environment': role_environment}))
        journal['phase'] = 'created'
        self._save(journal)
        _ok(endpoint, 'network', 'connect', '--alias', 'runtime', receipt['networks']['workers'], created['runtime'])
        for role in ROLES:
            _ok(endpoint, 'start', created[role])
        journal['phase'] = 'started'
        self._save(journal)
        self._wait_ready(receipt, profile, created, {role: shapes[role]['publish'] for role in ROLES})
        models_now = effective_models(environment, prefer=[*models, *(receipt.get('models') or {})])
        upgraded = {**receipt, 'containers': created, 'service_image': journal['service_image'],
                    'native_image': journal['native_image'], 'models': models_now,
                    'upgrade': {'transaction': txn, 'from_service_image': journal['previous_image'],
                                'idle_proof': 'new version (inherited state)' if journal['idle_proof']['incoming']
                                else 'running version',
                                'receipt_completed': journal['receipt_completed'],
                                'settings_added': journal['settings_added'],
                                'settings_kept': journal['settings_kept'],
                                'secrets_added': journal.get('secrets_added', {}),
                                'files_recovered': journal.get('files_recovered', []),
                                'role_mapping_changed': roles_changed,
                                'local_assertion_added': bool(signer),
                                'from_native_image': journal['previous_native_image'],
                                'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}}
        if signer:
            upgraded['signing_key_ids'] = {'ui': signer['kid']}
        if roles_changed:
            if role_change.get('to'):
                upgraded['role_mapping'] = role_change['to']
            else:
                upgraded.pop('role_mapping', None)
        _write_private_json(self.receipt_path, upgraded)
        journal['phase'] = 'awaiting_commit'
        self._save(journal)
        result = {'status': 'awaiting_commit', 'transaction': txn, 'service_image': journal['service_image'],
                  'native_image': journal['native_image'], 'models': models_now,
                  'backup_volume': journal['backup_volume'],
                  'state_files': sum(item['files'] for item in journal['snapshot'].values()),
                  'state_bytes': sum(item['bytes'] for item in journal['snapshot'].values())}
        if journal['settings_added']:
            result['settings_added'] = journal['settings_added']
        if journal['settings_kept']:
            result['settings_kept'] = journal['settings_kept']
        if journal.get('secrets_added'):
            result['secrets_added'] = journal['secrets_added']
        if journal.get('files_recovered'):
            result['files_recovered'] = journal['files_recovered']
        if signer:
            result['local_assertion'] = {'added': True, 'ui_key_id': signer['kid']}
            result['local_assertion_note'] = ('Signed-in owners can now confirm workspace sharing and permission '
                                              'changes in the browser; the UI holds the signing key and the '
                                              'runtime only its public key')
        if roles_changed:
            result['role_mapping'] = role_change.get('to') or 'removed'
            result['role_mapping_note'] = (
                'Before committing, sign in as a mapped tenant_admin. From now on each browser sign-in stores the '
                'role your identity provider maps and refuses anyone without a mapped role; admission only decides '
                'who may sign in. Existing browser sessions stay valid until they expire' if role_change.get('to')
                else 'Identity-provider roles are ignored from now on. Roles stored while the map was on remain: '
                     'preapprove each person again with the role they should have')
        if journal['native_image'] != journal['previous_native_image']:
            result['native_image_note'] = ('New workspaces use the new workspace image; existing workspaces keep '
                                           'the image they were created with, so keep it loaded')
        return result

    # -- rollback / commit ------------------------------------------------
    def rollback(self) -> dict:
        journal = _private_json(self.journal_path)
        phase = journal.get('phase')
        if phase == 'committing':
            raise UpgradeError('A commit of this upgrade is in progress; run upgrade-commit to finish it')
        if journal.get('version') != 1 or phase not in {
                'prepared', 'stopped', 'backup_created', 'snapshotted', 'renamed', 'created', 'started',
                'awaiting_commit', 'rolling_back', 'restored'}:
            raise UpgradeError('The upgrade journal is not recognized')
        endpoint, name, profile = self.endpoint, journal['name'], journal['profile']
        receipt = journal['previous_receipt']
        previous = journal['previous_containers']
        # Nothing changes until every precondition holds.
        for role in ROLES:
            if _inspect(endpoint, previous[role]) is None:
                raise UpgradeError(f'The previous {role} container is missing, so there is nothing to return to. '
                                   'Nothing was rolled back; keep the upgraded version or restore from a backup')
        proof, changed = 'resumed after restore', journal.get('changed_since_upgrade', [])
        if phase != 'restored':
            if journal.get('snapshot'):
                self._require_backup(journal)
                self._state_tool(journal, 'verify')  # read-only: refuse while the new version still serves
            new = self._new_identities(journal)
            proof, stopped = self._stop_new_version(journal, new)
            if phase != 'rolling_back':
                journal['rollback_from'] = phase
            journal['phase'] = 'rolling_back'
            self._save(journal)
            if journal.get('snapshot'):
                for role in ROLES:
                    if self._running(previous[role]):
                        self._stop(previous[role])
                try:
                    changed = self._state_tool(journal, 'restore')['changed']
                except UpgradeError as error:
                    if getattr(error, 'unchanged', False):
                        # Refused before any change: serve the new version again.
                        for identity in reversed(stopped):
                            _docker(endpoint, 'start', identity)
                        journal['phase'] = journal['rollback_from']
                        self._save(journal)
                    raise
            for identities in new.values():
                for identity in identities:
                    _docker(endpoint, 'stop', '--time', '30', identity, timeout=120)
                    _ok(endpoint, 'rm', identity)
            # From here a rerun only restarts the previous version; it never restores again.
            journal['phase'], journal['changed_since_upgrade'] = 'restored', changed
            self._save(journal)
        if journal.get('isolate_workers_network'):
            self._set_workers_network(receipt, name, previous['runtime'], isolated=False)
        for role in ROLES:
            if _ok(endpoint, 'inspect', '--format', '{{.Name}}', previous[role]).lstrip('/') != f'{name}-{role}':
                _ok(endpoint, 'rename', previous[role], f'{name}-{role}')
        for role in ROLES:
            _ok(endpoint, 'start', previous[role])
        desktop = self._docker_desktop()
        shapes = {role: role_shape(_inspect(endpoint, previous[role]) or {}, profile=profile, name=name, role=role,
                                   docker_desktop=desktop,
                                   volumes=receipt['volumes'], networks=receipt['networks']) for role in ROLES}
        self._wait_ready(receipt, profile, previous, {role: shapes[role]['publish'] for role in ROLES})
        # The original receipt returns byte-for-byte; an adopted package's receipt
        # instead names what actually runs again.
        if receipt.get('containers') == previous and receipt.get('service_image') == journal['previous_image']:
            _write_private_text(self.receipt_path, journal['previous_receipt_text'])
        else:
            _write_private_json(self.receipt_path, {**receipt, 'containers': dict(previous),
                                                    'service_image': journal['previous_image']})
        if _docker(endpoint, 'volume', 'inspect', journal['backup_volume']).returncode == 0:
            _ok(endpoint, 'volume', 'rm', journal['backup_volume'])
        self.journal_path.unlink()
        result = {'status': 'rolled_back', 'transaction': journal['transaction'],
                  'service_image': journal['previous_image'], 'restored_state': bool(journal.get('snapshot')),
                  'idle_proof': proof, 'changed_since_upgrade': changed}
        retired, unknown = journal.get('files_recovered') or [], journal.get('files_recovering') or []
        if retired:
            # They were retired before the backup, so the restored state does not contain them.
            result['files_recovered_not_restored'] = retired
        elif unknown:
            result['files_recovery_unknown'] = unknown
        if retired or unknown:
            result['files_note'] = ('The previous version still has no stored-file key: attaching a stored '
                                    'file there can register a new unpublished file')
        return result

    def commit(self) -> dict:
        journal = _private_json(self.journal_path)
        if journal.get('phase') not in {'awaiting_commit', 'committing'}:
            raise UpgradeError('Only a finished upgrade can be committed; run upgrade-rollback instead')
        if journal['phase'] == 'awaiting_commit':
            receipt = _private_json(self.receipt_path)
            if receipt.get('containers') != journal['new_containers']:
                raise UpgradeError('The receipt no longer names the upgraded containers')
            for role in ROLES:
                if not self._running(journal['new_containers'][role]):
                    raise UpgradeError(f'The upgraded {role} service is not running; check it or run upgrade-rollback')
            # Once removal starts, only finishing the commit is coherent.
            journal['phase'] = 'committing'
            self._save(journal)
        for role in ROLES:
            if _inspect(self.endpoint, journal['previous_containers'][role]) is not None:
                _ok(self.endpoint, 'rm', journal['previous_containers'][role])
        if _docker(self.endpoint, 'volume', 'inspect', journal['backup_volume']).returncode == 0:
            _ok(self.endpoint, 'volume', 'rm', journal['backup_volume'])
        self.journal_path.unlink()
        return {'status': 'committed', 'transaction': journal['transaction'],
                'service_image': journal['service_image'], 'native_image': journal['native_image']}


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='launch.py')
    commands = parser.add_subparsers(dest='action', required=True)
    upgrade = commands.add_parser('upgrade', help='Move one package to an exact new image (reversible until commit)')
    upgrade.add_argument('--service-image', required=True)
    upgrade.add_argument('--native-image')
    upgrade.add_argument('--model', action='append', metavar='PROFILE=MODEL')
    upgrade.add_argument('--adopt-running-containers', action='store_true',
                         help='Accept running containers that differ from the receipt (package changed outside the launcher)')
    upgrade.add_argument('--role-map', action='append', metavar='PROVIDER_ROLE=ROLE',
                         help='Hosted: map an identity-provider role value to member, viewer or tenant_admin '
                              '(repeat; replaces the whole mapping)')
    upgrade.add_argument('--role-claim', metavar='CLAIM', help='Hosted: the token claim holding role values (roles)')
    upgrade.add_argument('--no-role-map', action='store_true',
                         help='Hosted: remove the role mapping; admitted roles apply again')
    upgrade.add_argument('--local-qa-authority', type=Path, metavar='PRIVATE_JSON',
                         help='Local package only: supply the default-off local-QA fault authority from a private '
                              'file; upgrade-rollback removes it')
    upgrade.add_argument('--no-local-qa-authority', action='store_true',
                         help='Local package only: remove a supplied local-QA fault authority')
    upgrade.add_argument('--recover-unpublished-files', action='store_true',
                         help='First retire stored-file attachments the running version registered but never '
                              'published, as reviewed by the new image')
    for command in (upgrade, commands.add_parser('upgrade-commit'), commands.add_parser('upgrade-rollback')):
        command.add_argument('--docker-host', required=True)
        command.add_argument('--receipt', required=True, type=Path)
        command.add_argument('--ca-file', type=Path, help='Hosted only: CA for a private certificate')
        command.add_argument('--ready-timeout', type=float, default=90.0)
    args = parser.parse_args(argv)
    lock_path = args.receipt.with_name(args.receipt.name + '.upgrade.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpgradeError('Another upgrade command is running for this package') from None
        runner = Upgrade(args.docker_host, args.receipt, ca_file=args.ca_file, ready_timeout=args.ready_timeout)
        if args.action == 'upgrade':
            pairs = None
            if args.role_map:
                pairs = {}
                for item in args.role_map:
                    value, separator, role = item.rpartition('=')
                    if not separator or not value or value in pairs:
                        raise UpgradeError('Use --role-map PROVIDER_ROLE=ROLE once per provider role')
                    pairs[value] = role
            if args.no_role_map and (args.role_map or args.role_claim):
                raise UpgradeError('Choose --role-map or --no-role-map, not both')
            result = runner.upgrade(service_image=args.service_image, native_image=args.native_image,
                                    models=base.parse_model_arguments(args.model),
                                    adopt=args.adopt_running_containers,
                                    recover_unpublished=args.recover_unpublished_files,
                                    role_map=pairs, role_claim=args.role_claim, clear_roles=args.no_role_map,
                                    local_qa_authority=(_private_json(args.local_qa_authority)
                                                        if args.local_qa_authority else None),
                                    clear_local_qa=args.no_local_qa_authority)
        elif args.action == 'upgrade-commit':
            result = runner.commit()
        else:
            result = runner.rollback()
        print(json.dumps(result, indent=2))
    finally:
        os.close(fd)
