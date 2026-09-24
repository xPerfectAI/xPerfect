"""Dedicated account containers; only trusted packaged Linux services construct this."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .contained_account_launch import AccountContainmentUncertain, AccountStopReceipt, ContainedAccountLauncher
from .control_plane import ControlPlaneError
from .storage_quota import QuotaUnavailable

ACCOUNT_MOUNT = '/workspace/account'


@dataclass(frozen=True)
class AccountContainerDescriptor:
    generation: str
    lease_id: str
    uid: int
    container_id: str
    memory_bytes: int
    pids_limit: int
    pending_recovery: bool


class DockerAccountContainerBackend:
    def __init__(self, *, data_root: Path, volume_name: str, image_id: str, network: str,
                 memory_bytes: int, pids_limit: int, docker=None, popen=None, project_info=None):
        self.data_root = Path(data_root)
        if (not self.data_root.is_absolute() or self.data_root != self.data_root.resolve(strict=True)
                or not self.data_root.is_dir()):
            raise ControlPlaneError('A verified account data mount is required')
        if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) for value in (volume_name, network)):
            raise ControlPlaneError('Declared account volume and network are required')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
            raise ControlPlaneError('An immutable account image ID is required')
        if memory_bytes <= 0 or pids_limit <= 0:
            raise ControlPlaneError('Account container resource reservation is required')
        self.volume_name, self.image_id, self.network = volume_name, image_id, network
        self.memory_bytes, self.pids_limit = memory_bytes, pids_limit
        self.docker = docker or self._docker
        self.popen_transport = popen or subprocess.Popen
        self.project_info = project_info

    def verify_owner_storage(self, tenant_id, owner_id, snapshot) -> bool:
        """Attest the exact future account volume-subpaths to this owner's XFS project."""
        try:
            if (not isinstance(tenant_id, str) or not tenant_id or '\0' in tenant_id
                    or not isinstance(owner_id, str) or not owner_id or '\0' in owner_id
                    or snapshot.hard_enforced is not True
                    or type(snapshot.project_id) is not int or snapshot.project_id <= 0):
                return False
            digest = hashlib.sha256(f'{tenant_id}\0{owner_id}'.encode()).hexdigest()
            owner_root = self.data_root / 'owners' / digest
            if Path(snapshot.root) != owner_root or owner_root.resolve(strict=True) != owner_root:
                return False
            project_info = self.project_info
            if project_info is None:
                from .storage_quota_linux import LinuxXfsQuota
                project_info = LinuxXfsQuota().project_info
            device = self.data_root.stat().st_dev
            expected_project = (snapshot.project_id, True)
            for path in (owner_root, owner_root / 'provider-accounts'):
                if not os.path.lexists(path):
                    if path == owner_root:
                        return False
                    continue
                info = path.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_dev != device
                        or path.resolve(strict=True) != path or project_info(path) != expected_project):
                    return False
            accounts = owner_root / 'provider-accounts'
            if accounts.exists():
                for home in accounts.iterdir():
                    info = home.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or info.st_dev != device
                            or home.resolve(strict=True) != home or project_info(home) != expected_project
                            or not re.fullmatch(r'[A-Za-z0-9_.-]+', home.name)):
                        return False
            return True
        except (OSError, ValueError, TypeError, AttributeError, QuotaUnavailable):
            return False


    def identity(self) -> str:
        daemon = self.docker(['info', '--format', '{{.ID}}']).stdout.strip()
        if not daemon or daemon == '<no value>':
            raise AccountContainmentUncertain('Account Docker daemon identity is unavailable')
        root = self.data_root.stat()
        payload = [daemon, str(self.data_root), root.st_dev, root.st_ino, self.volume_name,
                   self.image_id, self.network, self.memory_bytes, self.pids_limit]
        return hashlib.sha256(json.dumps(payload, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _docker(arguments, *, check=True):
        result = subprocess.run(['docker', *arguments], capture_output=True, text=True, timeout=30)
        if check and result.returncode:
            raise AccountContainmentUncertain('Account container operation failed')
        return result

    def descriptor(self, generation) -> AccountContainerDescriptor:
        if self.identity() != generation.substrate_id:
            raise AccountContainmentUncertain('Account inventory substrate changed')
        value = self._inspect(generation)
        return AccountContainerDescriptor(generation.generation, generation.lease_id, generation.uid,
            value['Id'] if value else '', self.memory_bytes, self.pids_limit, value is None or (value.get('State') or {}).get('Running') is not True)

    def _subpath(self, generation):
        home = generation.account_home
        if home != home.resolve(strict=True):
            raise AccountContainmentUncertain('Account home path changed')
        info = home.stat()
        if (info.st_dev, info.st_ino) != (generation.device, generation.inode):
            raise AccountContainmentUncertain('Account home identity changed')
        try:
            relative = home.relative_to(self.data_root)
        except ValueError:
            raise ControlPlaneError('Account home is outside the declared data volume') from None
        if len(relative.parts) < 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in relative.parts):
            raise ControlPlaneError('Invalid account volume subpath')
        return relative.as_posix()

    @staticmethod
    def _labels(generation):
        return {'xperfect.account-generation': generation.generation,
                'xperfect.account-lease': hashlib.sha256(generation.lease_id.encode()).hexdigest()}

    def _inspect(self, generation):
        result = self.docker(['inspect', generation.name], check=False)
        if result.returncode:
            # A failed inspect is not absence; successful inventory is mandatory.
            listing = self.docker(['container', 'ls', '-a', '--no-trunc', '--format', '{{json .}}'])
            try:
                entries = [json.loads(line) for line in listing.stdout.splitlines() if line.strip()]
                if any(entry.get('Names') == generation.name or (generation.container_id and entry.get('ID') == generation.container_id) for entry in entries):
                    raise ValueError
            except (ValueError, TypeError):
                raise AccountContainmentUncertain('Account container inspection is unavailable') from None
            return None
        try:
            values = json.loads(result.stdout)
            if len(values) != 1:
                raise ValueError
            value = values[0]; config = value['Config']; host = value['HostConfig']
            identifier = value['Id']
            if (not re.fullmatch(r'[a-f0-9]{64}', identifier)
                    or (generation.container_id and identifier != generation.container_id)
                    or value['Image'] != self.image_id or config['User'] != f'{generation.uid}:{generation.uid}'
                    or any((config.get('Labels') or {}).get(key) != expected for key, expected in self._labels(generation).items())
                    or config.get('Entrypoint') != ['/bin/sleep'] or config.get('Cmd') != ['infinity']
                    or host.get('ReadonlyRootfs') is not True or set(host.get('CapDrop') or []) != {'ALL'}
                    or host.get('CapAdd') or host.get('Privileged') or host.get('Binds') or host.get('Devices')
                    or host.get('DeviceRequests') or host.get('Tmpfs') or host.get('PidMode') not in {'', None}
                    or host.get('IpcMode') != 'none' or host.get('NetworkMode') != self.network
                    or set((value.get('NetworkSettings') or {}).get('Networks') or {}) != {self.network}
                    or set(host.get('SecurityOpt') or []) not in ({'no-new-privileges'}, {'no-new-privileges:true'})
                    or (config.get('Healthcheck') or {}).get('Test') != ['NONE']
                    or any(item.split('=', 1)[0].startswith('LD_') or item.split('=', 1)[0] in {'PYTHONHOME', 'PYTHONPATH'} for item in config.get('Env') or [])
                    or host.get('Memory') != self.memory_bytes or host.get('MemorySwap') != self.memory_bytes
                    or host.get('PidsLimit') != self.pids_limit
                    or (host.get('RestartPolicy') or {}).get('Name') not in {'no', '', None}):
                raise ValueError
            mounts = value.get('Mounts') or []; declared = host.get('Mounts') or []
            if len(mounts) != 1 or len(declared) != 1:
                raise ValueError
            mount, declaration = mounts[0], declared[0]
            if (mount.get('Type') != 'volume' or mount.get('Name') != self.volume_name
                    or mount.get('Destination') != ACCOUNT_MOUNT or mount.get('RW') is not True
                    or declaration.get('Type') != 'volume' or declaration.get('Source') != self.volume_name
                    or declaration.get('Target') != ACCOUNT_MOUNT or declaration.get('ReadOnly') is True
                    or (declaration.get('VolumeOptions') or {}).get('Subpath') != self._subpath(generation)
                    or (declaration.get('VolumeOptions') or {}).get('NoCopy') is not True):
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError):
            raise AccountContainmentUncertain('Account container differs from its declared isolation contract') from None

    @staticmethod
    def _ownership(home, uid):
        """Stopped tree only; descriptor traversal never follows provider-created links."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        def visit(descriptor):
            for name in os.listdir(descriptor):
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, flags, dir_fd=descriptor)
                    try: visit(child)
                    finally: os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise AccountContainmentUncertain('Account data contains a shared file')
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    try:
                        actual = os.fstat(child)
                        if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                            raise AccountContainmentUncertain('Account file changed during ownership transfer')
                        os.fchown(child, uid, uid); os.fchmod(child, 0o600)
                    finally: os.close(child)
                elif stat.S_ISLNK(info.st_mode):
                    os.chown(name, uid, uid, dir_fd=descriptor, follow_symlinks=False)
                elif stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode):
                    # Native setup tools may leave transient IPC nodes behind after
                    # their exact container has been removed.  They cannot carry
                    # persistent account state and must not be opened or chowned.
                    # Remove only a single-link node whose identity is unchanged;
                    # every other special file still fails closed below.
                    if info.st_nlink != 1:
                        raise AccountContainmentUncertain('Account IPC node is shared')
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise AccountContainmentUncertain('Account IPC node changed during cleanup')
                    os.unlink(name, dir_fd=descriptor)
                else:
                    raise AccountContainmentUncertain('Account data contains an unsupported file type')
            os.fchown(descriptor, uid, uid); os.fchmod(descriptor, 0o700)
        descriptor = os.open(home, flags)
        try: visit(descriptor)
        finally: os.close(descriptor)

    def _prepare_payload(self, request, generation):
        home = generation.account_home
        environment = dict(request.environment)
        selectors = {'HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR',
                     'GROK_HOME', 'GROK_AUTH_PATH', 'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'}
        for name in selectors.intersection(environment):
            path = Path(environment[name])
            if not path.is_absolute() or not path.is_relative_to(home):
                raise ControlPlaneError('Account provider paths must stay inside the selected account')
            environment[name] = str(Path(ACCOUNT_MOUNT) / path.relative_to(home))
        environment.update(HOME=ACCOUNT_MOUNT, TMPDIR=ACCOUNT_MOUNT+'/.tmp',
            XDG_CONFIG_HOME=ACCOUNT_MOUNT+'/.config', XDG_CACHE_HOME=ACCOUNT_MOUNT+'/.cache',
            XDG_STATE_HOME=ACCOUNT_MOUNT+'/.state', USER=f'account-{generation.uid}', LOGNAME=f'account-{generation.uid}')
        for folder in ('.tmp', '.config', '.cache', '.state'):
            path = home/folder
            path.mkdir(mode=0o700, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise ControlPlaneError('Unsafe account private directory')
        payload = home / ('.xperfect-launch-'+generation.generation+'.json')
        descriptor = os.open(payload, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({'command': request.command, 'environment': environment}, handle)
            handle.flush(); os.fsync(handle.fileno())
        return ACCOUNT_MOUNT+'/'+payload.name

    def start(self, request, generation, **options):
        if os.geteuid() != 0:
            raise ControlPlaneError('Packaged account containment requires the trusted Linux service')
        subpath = self._subpath(generation)
        if self._inspect(generation) is not None:
            raise AccountContainmentUncertain('Account container generation already exists')
        payload = self._prepare_payload(request, generation)
        self._ownership(generation.account_home, generation.uid)
        command = ['create', '--name', generation.name, '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--user', f'{generation.uid}:{generation.uid}',
                   '--memory', str(self.memory_bytes), '--memory-swap', str(self.memory_bytes),
                   '--pids-limit', str(self.pids_limit), '--network', self.network, '--ipc', 'none', '--no-healthcheck',
                   '--restart', 'no', '--mount', f'type=volume,src={self.volume_name},dst={ACCOUNT_MOUNT},volume-subpath={subpath},volume-nocopy',
                   '--entrypoint', '/bin/sleep']
        for key, value in self._labels(generation).items(): command.extend(['--label', key+'='+value])
        command.extend([self.image_id, 'infinity'])
        result = self.docker(command)
        identifier = result.stdout.strip()
        value = self._inspect(generation)
        if value is None or value['Id'] != identifier:
            raise AccountContainmentUncertain('Created account container identity is unavailable')
        self.docker(['start', identifier])
        options.pop('pass_fds', None)  # ledger owns crash recovery, not a CLI proxy flock
        options['cwd'] = str(generation.account_home)
        options['env'] = {key: value for key, value in os.environ.items() if key in {'PATH','HOME','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'}}
        arguments = ['docker', 'exec', '-i']
        descriptor = options.get('stdin')
        if isinstance(descriptor, int) and descriptor >= 0 and os.isatty(descriptor):
            arguments.append('-t')
        arguments.extend(['--workdir', ACCOUNT_MOUNT, identifier, '/usr/bin/python3', '-I', '-m',
            'workers_projects_runtime.storage_quota_guard', '--', '/usr/bin/python3', '-I', '-m',
            'workers_projects_runtime.account_native_entry', payload])
        return self.popen_transport(arguments, **options), identifier

    def finish(self, generation):
        self._subpath(generation)
        value = self._inspect(generation)
        identifier = generation.container_id
        if value is not None:
            identifier = value['Id']
            self.docker(['rm', '--force', identifier])
        # Even when the CLI leader exited, remove the container cgroup and prove
        # the exact generation absent. Daemon failure leaves recovery pending.
        if self._inspect(generation) is not None:
            raise AccountContainmentUncertain('Account descendants may still be running')
        self._ownership(generation.account_home, os.geteuid())
        payload = generation.account_home / ('.xperfect-launch-'+generation.generation+'.json')
        payload.unlink(missing_ok=True)
        return AccountStopReceipt(generation.lease_id, generation.generation, identifier, True, True)


class PackagedAccountLauncher(ContainedAccountLauncher):
    def verify_owner_storage(self, tenant_id, owner_id, snapshot) -> bool:
        return self.backend.verify_owner_storage(tenant_id, owner_id, snapshot)


def create_packaged_account_launcher(*, execution_profile: str, control_root: Path,
        data_root: Path, volume_name: str, image_id: str, network: str,
        memory_bytes: int, pids_limit: int):
    """Called only after package volume/image/NSS and capacity readiness checks.

    Packaging owns those measured substrate attestations. This constructor does
    not manufacture readiness from env strings or infer quota from a local volume.
    """
    import sys
    if execution_profile not in {'local-linux', 'hosted-xfs'} or sys.platform != 'linux' or os.geteuid() != 0:
        raise ControlPlaneError('Packaged account setup requires the supported Linux service profile')
    control_root, data_root = Path(control_root), Path(data_root)
    if (not control_root.is_absolute() or not control_root.is_dir()
            or control_root != control_root.resolve(strict=True) or control_root.is_relative_to(data_root)):
        raise ControlPlaneError('Account control storage must be outside native data')
    if execution_profile == 'hosted-xfs' and control_root.stat().st_dev == data_root.stat().st_dev:
        raise ControlPlaneError('Hosted account control storage requires a separate filesystem')
    backend = DockerAccountContainerBackend(data_root=data_root, volume_name=volume_name,
        image_id=image_id, network=network, memory_bytes=memory_bytes, pids_limit=pids_limit)
    return PackagedAccountLauncher(control_root=control_root/'account-launches', backend=backend)
