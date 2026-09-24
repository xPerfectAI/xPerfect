"""Linux shared workspace substrate; control-plane code alone owns Docker access.

Members use distinct immutable UIDs, private homes, and a declared common
or private project root. This substrate never removes its box to stop one member.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Callable


class WorkspaceBoxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceMemberBinding:
    workspace_id: str
    worker_id: str
    tenant_id: str
    owner_id: str
    uid: int

    def __post_init__(self):
        if not re.fullmatch(r'wsp_[A-Za-z0-9_]{1,80}', self.workspace_id):
            raise ValueError('Invalid workspace identity')
        if not re.fullmatch(r'wrk_[A-Za-z0-9_]{1,80}', self.worker_id):
            raise ValueError('Invalid member identity')
        if not self.tenant_id or not self.owner_id or not 20001 <= self.uid <= 60000:
            raise ValueError('Invalid workspace member binding')

    @property
    def owner_digest(self) -> str:
        return hashlib.sha256(json.dumps([self.tenant_id, self.owner_id]).encode()).hexdigest()


class WorkspaceBox:
    """A verified workspace box with a Linux volume also mounted in the API service.

    ``volume_root`` is that service mount, not an arbitrary host export. The
    supervisor directory is never granted to native members. ``docker`` can be
    injected by a deterministic test; production uses bounded CLI operations.
    """
    def __init__(self, *, volume_root: Path, volume_name: str, image: str,
                 binding: WorkspaceMemberBinding, memory_bytes: int, pids_limit: int,
                 docker: Callable | None = None, control_root: Path | None = None,
                 volume_subpath: str = "", file_placement: str = "member_private",
                 network: str | None = None):
        if file_placement not in {"common", "member_private"}:
            raise ValueError("Invalid workspace file placement")
        if network is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", network):
            raise ValueError("Invalid workspace network")
        if network in {"host", "container", "none", "default"}:
            raise ValueError("A dedicated worker bridge network is required")
        self.network = network
        self.file_placement = file_placement
        self.member_gid = 20000 if file_placement == "common" else binding.uid
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', volume_name):
            raise ValueError('Invalid workspace volume')
        if not image or image.startswith('-'):
            raise ValueError('A reviewed workspace image is required')
        if memory_bytes <= 0 or pids_limit <= 0:
            raise ValueError('Workspace resource reservation is required')
        prefix = PurePosixPath(volume_subpath)
        if prefix.is_absolute() or '..' in prefix.parts:
            raise ValueError('Invalid owner volume subpath')
        self.volume_subpath = '' if str(prefix) == '.' else prefix.as_posix()
        self.memory_bytes = int(memory_bytes)
        self.pids_limit = int(pids_limit)
        self.volume_root = Path(volume_root)
        self.volume_name = volume_name
        self.image = image
        self.binding = binding
        self.root = self.volume_root / 'execution_workspaces' / binding.workspace_id
        self.control_root = Path(control_root) if control_root else self.volume_root / 'workspace-control'
        self.supervisor = self.control_root / binding.owner_digest / binding.workspace_id
        self.native_root = self.root / 'native'
        self.member_root = self.native_root / 'members' / str(binding.uid)
        self.name = 'xperfect-' + binding.workspace_id.replace('_', '-')
        self.docker = docker or self._docker

    @property
    def native_workspace(self):
        return '/workspace/common' if self.file_placement == 'common' else f'/workspace/data/members/{self.binding.uid}/worktree'

    @staticmethod
    def _common_acl(path: Path):
        """Apply the declared box-local workspace group to service-created paths."""
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            WorkspaceBox._publication_acl(descriptor, subject='group:20000')
        finally:
            os.close(descriptor)

    @staticmethod
    def _publication_acl(descriptor: int, *, subject: str, is_directory: bool | None = None):
        metadata = os.fstat(descriptor)
        path = f'/proc/self/fd/{descriptor}'
        if metadata.st_uid != os.geteuid() or stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceBoxUnavailable('Shared publication requires a service-owned real path')
        directory = stat.S_ISDIR(metadata.st_mode)
        if is_directory is not None and directory != is_directory:
            raise WorkspaceBoxUnavailable('Publication descriptor type changed')
        if not directory and not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceBoxUnavailable('Unsupported shared file type')
        permission = 'rwx' if directory or metadata.st_mode & 0o111 else 'rw-'
        access = {f'user::{permission}', f'user:{os.geteuid()}:{permission}',
                  'group::---', f'{subject}:{permission}', f'mask::{permission}', 'other::---'}
        if directory:
            access |= {'default:user::rwx', f'default:user:{os.geteuid()}:rwx',
                       'default:group::---', f'default:{subject}:rwx', 'default:mask::rwx', 'default:other::---'}
        result = subprocess.run(['getfacl', '-cEpn', str(path)], capture_output=True, text=True, timeout=5, pass_fds=(descriptor,))
        if result.returncode:
            raise WorkspaceBoxUnavailable('Shared file ACL inspection is unavailable')
        current = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        allowed_subjects = {'user:', f'user:{os.geteuid()}', 'group:', subject, 'mask:', 'other:'}
        if directory:
            allowed_subjects |= {'default:' + subject for subject in allowed_subjects}
        for line in current:
            subject, permission_bits = line.rsplit(':', 1)
            if (subject not in allowed_subjects or len(permission_bits) != 3
                    or any(actual not in {'-', expected} for actual, expected in zip(permission_bits, 'rwx'))
):
                raise WorkspaceBoxUnavailable('Shared file ACL contains an unexpected grant')
        if not directory and metadata.st_nlink != 1:
            raise WorkspaceBoxUnavailable('Shared publication cannot grant access through hard links')
        if current != access:
            result = subprocess.run(['setfacl', '--set', ','.join(sorted(access)), str(path)], capture_output=True, text=True, timeout=5, pass_fds=(descriptor,))
            if result.returncode:
                raise WorkspaceBoxUnavailable('Shared file ACL could not be applied')

    def _workspace_subpath(self):
        parts = [self.volume_subpath, 'execution_workspaces', self.binding.workspace_id]
        return '/'.join(part for part in parts if part)

    @staticmethod
    def _docker(args, *, check=True):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30)
        if check and result.returncode:
            raise WorkspaceBoxUnavailable('Workspace container operation failed')
        return result

    @staticmethod
    def _directory(path: Path, mode: int):
        try:
            path.mkdir(mode=mode, parents=False)
            created = True
        except FileExistsError:
            created = False
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid():
            raise WorkspaceBoxUnavailable('Workspace directory ownership is invalid')
        if created:
            path.chmod(mode)
        elif stat.S_IMODE(value.st_mode) & 0o707 != mode & 0o707:
            raise WorkspaceBoxUnavailable('Workspace directory mode changed')

    @contextmanager
    def locked(self):
        if 20000 <= os.geteuid() <= 60000:
            raise WorkspaceBoxUnavailable('Service UID overlaps the native member range')
        if sys.platform != 'linux':
            raise WorkspaceBoxUnavailable('Strict shared execution requires Linux volume storage')
        if not self.volume_root.is_absolute() or self.volume_root.is_symlink():
            raise WorkspaceBoxUnavailable('A trusted Linux runtime volume is required')
        self._directory(self.volume_root / 'execution_workspaces', 0o700)
        self._directory(self.root, 0o711)
        if not self.control_root.is_absolute() or self.control_root.is_symlink():
            raise WorkspaceBoxUnavailable('Trusted workspace control storage is required')
        self._directory(self.control_root, 0o700)
        self._directory(self.supervisor.parent, 0o700)
        self._directory(self.supervisor, 0o700)
        fd = os.open(self.supervisor / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            owner_path = self.supervisor / 'owner.json'
            identity = {'workspace_id': self.binding.workspace_id, 'owner_digest': self.binding.owner_digest}
            if self.file_placement == 'common':
                identity['file_placement'] = 'common'
            if owner_path.exists():
                if owner_path.is_symlink() or json.loads(owner_path.read_text()) != identity:
                    raise WorkspaceBoxUnavailable('Workspace owner binding changed')
            else:
                self._private_json(owner_path, identity)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _private_json(path: Path, value):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

    def prepare_member(self):
        with self.locked():
            self._directory(self.root / 'common', 0o700 if self.file_placement == 'common' else 0o755)
            if self.file_placement == 'common':
                self._common_acl(self.root / 'common')
            self._directory(self.native_root, 0o711)
            self._directory(self.native_root / 'members', 0o711)
            self._directory(self.member_root, 0o700)
            identity_path = self.supervisor / f'member-{self.binding.uid}.json'
            identity = {'worker_id': self.binding.worker_id, 'uid': self.binding.uid}
            if identity_path.exists():
                if identity_path.is_symlink() or json.loads(identity_path.read_text()) != identity:
                    raise WorkspaceBoxUnavailable('Member UID is already assigned')
            else:
                self._private_json(identity_path, identity)
            uid = self.binding.uid
            # The parent remains service-owned. Native chmod cannot expose a sibling's
            # private files because traversal still requires its independent parent ACL.
            for path in (self.member_root, self.member_root / 'home', self.member_root / 'worktree',
                         self.member_root / 'home/tmp', self.member_root / 'home/.config',
                         self.member_root / 'home/.cache'):
                self._directory(path, 0o700)
                self._private_acl(path, uid)
            self._directory(self.supervisor / f'member-{uid}-state', 0o700)
            marker = self.native_root / '.mount-receipt'
            if not marker.exists():
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o444)
                os.fchmod(fd, 0o444)
                with os.fdopen(fd, 'w') as handle:
                    handle.write(self.binding.owner_digest + ':' + self.binding.workspace_id)
            return self.paths()

    @staticmethod
    def _private_acl(path: Path, uid: int):
        """Grant one native identity; refuse inherited foreign grants before use."""
        result = subprocess.run(['getfacl', '-cEpn', str(path)], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise WorkspaceBoxUnavailable('Workspace ACL inspection is unavailable')
        lines = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        access = {'user::rwx', f'user:{uid}:rwx', 'group::---', 'mask::rwx', 'other::---'}
        defaults = {'default:user::rwx', f'default:user:{uid}:rwx',
                    f'default:user:{os.geteuid()}:rwx', 'default:group::---',
                    'default:mask::rwx', 'default:other::---'}
        # A child inherits the service's named-user entry as an access ACL.
        allowed = access | defaults | {f'user:{os.geteuid()}:rwx', 'mask::---'}
        if lines - allowed:
            raise WorkspaceBoxUnavailable('Workspace member ACL contains an unexpected grant')
        if not (access | defaults) <= lines:
            acl = ','.join(sorted(access | defaults))
            result = subprocess.run(['setfacl', '--set', acl, str(path)],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode:
                raise WorkspaceBoxUnavailable('Workspace filesystem does not support private member ACLs')

    def paths(self):
        return {'worker_root': self.member_root, 'state_dir': self.supervisor / f'member-{self.binding.uid}-state',
                'home_dir': self.member_root / 'home',
                'workspace_dir': self.root / 'common' if self.file_placement == 'common' else self.member_root / 'worktree',
                'common_dir': self.root / 'common'}

    def _inspect(self):
        result = self.docker(['inspect', self.name], check=False)
        if result.returncode:
            # Error text is not proof of absence; list names through a successful probe.
            listing = self.docker(['container', 'ls', '-a', '--format', '{{.Names}}'])
            if self.name in listing.stdout.splitlines():
                raise WorkspaceBoxUnavailable('Workspace container inspection is unavailable')
            return None
        try:
            values = json.loads(result.stdout)
            if len(values) != 1 or not isinstance(values[0], dict):
                raise ValueError
            value = values[0]
            config = value['Config']; host = value['HostConfig']
            image_receipt = self.supervisor / 'image.json'
            expected_image = json.loads(image_receipt.read_text())['image_id']
            mounts = value.get('Mounts') or []
            if ({mount.get('Destination') for mount in mounts} != {'/workspace/data', '/workspace/common'}
                    or len(mounts) != 2 or any(
                mount.get('Type') != 'volume' or mount.get('Name') != self.volume_name
                or mount.get('Destination') not in {'/workspace/data', '/workspace/common'}
                or mount.get('RW') is not (mount.get('Destination') == '/workspace/data' or self.file_placement == 'common')
                for mount in mounts
            )):
                raise ValueError
            declared = host.get('Mounts') or []
            expected_subpaths = {
                '/workspace/data': f'{self._workspace_subpath()}/native',
                '/workspace/common': f'{self._workspace_subpath()}/common',
            }
            if (len(declared) != 2
                    or {entry.get('Target') for entry in declared} != set(expected_subpaths)
                    or any(entry.get('Source') != self.volume_name
                           or entry.get('Type') != 'volume'
                           or (entry.get('VolumeOptions') or {}).get('Subpath') != expected_subpaths[entry['Target']]
                           or (entry.get('VolumeOptions') or {}).get('NoCopy') is not True
                           for entry in declared)):
                raise ValueError
            labels = config.get('Labels') or {}
            if (labels.get('xperfect.workspace') != self.binding.workspace_id
                    or labels.get('xperfect.owner') != self.binding.owner_digest
                    or config.get('User') != '65534:65534'
                    or any(entry.split('=', 1)[0].startswith('LD_')
                           or entry.split('=', 1)[0] in {'PYTHONHOME', 'PYTHONPATH'}
                           for entry in config.get('Env') or [])
                    or host.get('ReadonlyRootfs') is not True
                    or set(host.get('CapDrop') or []) != {'ALL'}
                    or host.get('CapAdd')
                    or not any(v.startswith('no-new-privileges') for v in host.get('SecurityOpt') or [])
                    or host.get('Privileged') or host.get('PidMode') not in {'', None}
                    or host.get('IpcMode') != 'none'
                    or (config.get('Healthcheck') or {}).get('Test') != ['NONE']
                    or (host.get('NetworkMode') != self.network if self.network else host.get('NetworkMode') not in {'default', 'bridge'})
                    or (self.network is not None and set((value.get('NetworkSettings') or {}).get('Networks') or {}) != {self.network})
                    or host.get('Binds') or host.get('Devices') or host.get('DeviceRequests')
                    or (host.get('RestartPolicy') or {}).get('Name') not in {'', 'no', None}
                    or config.get('Entrypoint') != ['/bin/sleep'] or config.get('Cmd') != ['infinity']
                    or host.get('MemorySwap') != self.memory_bytes
                    or value.get('Image') != expected_image
                    or host.get('Memory') != self.memory_bytes
                    or host.get('PidsLimit') != self.pids_limit
                    or not isinstance(value.get('Id'), str)):
                raise ValueError
            return value
        except (KeyError, TypeError, ValueError, OSError):
            raise WorkspaceBoxUnavailable('Workspace container does not match its isolation contract') from None

    def ensure_box(self) -> str:
        self.prepare_member()
        with self.locked():
            image_receipt = self.supervisor / 'image.json'
            if not image_receipt.exists():
                result = self.docker(['image', 'inspect', '--format', '{{.Id}}', self.image])
                image_id = result.stdout.strip()
                if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
                    raise WorkspaceBoxUnavailable('Workspace image identity is unavailable')
                self._private_json(image_receipt, {'image_id': image_id})
            image_id = json.loads(image_receipt.read_text())['image_id']
            inspected = self._inspect()
            if inspected is None:
                subpath = self._workspace_subpath()
                # Mount only this workspace's subdirectory. Repeated member UIDs in
                # another workspace can never reach another owner's volume subtree.
                self.docker([
                    'run', '-d', '--name', self.name, '--init',
                    *(['--network', self.network] if self.network else []),
                    '--user', '65534:65534', '--read-only', '--cap-drop', 'ALL',
                    '--ipc', 'none', '--no-healthcheck',
                    '--security-opt', 'no-new-privileges', '--pids-limit', str(self.pids_limit),
                    '--memory', str(self.memory_bytes), '--memory-swap', str(self.memory_bytes),
                    '--label', 'xperfect.workspace=' + self.binding.workspace_id,
                    '--label', 'xperfect.owner=' + self.binding.owner_digest,
                    '--mount', f'type=volume,src={self.volume_name},dst=/workspace/data,volume-subpath={subpath}/native,volume-nocopy',
                    '--mount', f'type=volume,src={self.volume_name},dst=/workspace/common,volume-subpath={subpath}/common,' + ('readonly,' if self.file_placement == 'member_private' else '') + 'volume-nocopy',
                    '--entrypoint', '/bin/sleep', image_id, 'infinity',
                ])
                inspected = self._inspect()
            if inspected is None or inspected['State'].get('Running') is not True:
                raise WorkspaceBoxUnavailable('Workspace container is not running')
            self.docker(['exec', '--user', '65534:65534', inspected['Id'],
                         *self.guarded_command(['/bin/true'])])
            # A fresh challenge proves this live service inode view, not a copied
            # marker from another mount or an earlier workspace generation.
            nonce = os.urandom(32).hex()
            probe = self.native_root / ('.mount-probe-' + nonce)
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o444)
            try:
                os.fchmod(fd, 0o444)
                with os.fdopen(fd, 'w') as output:
                    output.write(nonce); output.flush(); os.fsync(output.fileno())
                marker = self.docker(['exec', '--user', '65534:65534', inspected['Id'],
                    *self.guarded_command(['/bin/cat', '/workspace/data/' + probe.name])])
                if marker.stdout != nonce:
                    raise WorkspaceBoxUnavailable('Service and workspace volume identity differ')
            finally:
                probe.unlink(missing_ok=True)
            return inspected['Id']

    @staticmethod
    def guarded_command(argv):
        # Absolute immutable interpreter plus isolated mode prevents a member's
        # PATH or Python search path from executing code before the filter loads.
        return ['/usr/bin/python3', '-I', '-m',
                'workers_projects_runtime.storage_quota_guard', '--', *argv]

    def git_workspace_env(self) -> dict[str, str]:
        if self.file_placement != 'common':
            return {}
        return {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'safe.directory',
                'GIT_CONFIG_VALUE_0': self.native_workspace}

    def command(self, argv: list[str], *, env: dict[str, str] | None = None) -> list[str]:
        if not argv or any(not isinstance(value, str) or '\x00' in value for value in argv):
            raise ValueError('A native argument vector is required')
        container_id = self.ensure_box()
        uid = self.binding.uid
        home = f'/workspace/data/members/{uid}/home'
        values = {'HOME': home, 'TMPDIR': home + '/tmp', 'XDG_CONFIG_HOME': home + '/.config',
                  'XDG_CACHE_HOME': home + '/.cache', 'USER': f'member-{uid}', 'LOGNAME': f'member-{uid}'}
        git_workspace_config = self.git_workspace_env()
        for key, value in (env or {}).items():
            if key.startswith('LD_') or key in {'PYTHONHOME', 'PYTHONPATH'}:
                raise ValueError('Native environment cannot alter the trusted launcher')
            if key in values or key in git_workspace_config or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key):
                raise ValueError('Native environment cannot replace member identity')
            if key.endswith(('_KEY', '_TOKEN', '_SECRET', '_PASSWORD')):
                raise ValueError('Native credentials must use private run files')
            values[key] = value
        # The product owns this exact shared root; each member otherwise sees
        # Git's dubious-ownership error because the volume is service-owned.
        # Trust only this workspace for this process, never every repository.
        values.update(git_workspace_config)
        command = ['docker', 'exec', '-i', '--user', f'{uid}:{self.member_gid}',
                   '--workdir', self.native_workspace]
        for key, value in values.items():
            command.extend(['--env', f'{key}={value}'])
        return [*command, container_id, *self.guarded_command(argv)]


    def set_member_paused(self, expected_container_id: str, paused: bool):
        inspected = self._inspect()
        if inspected is None or inspected['Id'] != expected_container_id:
            raise WorkspaceBoxUnavailable('Workspace generation changed before member control')
        script = r"""import os,signal,sys,time
uid=os.getuid();self_pid=os.getpid();paused=sys.argv[1]=='1'
sig=signal.SIGSTOP if paused else signal.SIGCONT
deadline=time.monotonic()+2
while True:
    pending=[]
    for name in os.listdir('/proc'):
        if not name.isdecimal() or int(name)==self_pid:continue
        try:
            with open('/proc/'+name+'/status') as f:fields=dict(line.split(':',1) for line in f if ':' in line)
        except FileNotFoundError:continue
        state=fields['State'].strip()[0]
        if int(fields['Uid'].split()[0])!=uid or state=='Z':continue
        if (state in {'T','t'})!=paused:pending.append(int(name))
    if not pending:break
    for pid in pending:
        try:os.kill(pid,sig)
        except ProcessLookupError:pass
    if time.monotonic()>deadline:raise SystemExit(1)
    time.sleep(.02)
"""
        result = self.docker(['exec', '--user', f'{self.binding.uid}:{self.binding.uid}',
                              expected_container_id, *self.guarded_command(['python3', '-c', script, '1' if paused else '0'])], check=False)
        if result.returncode:
            raise WorkspaceBoxUnavailable('Member pause state is unconfirmed')

    def release_if_sole_member(self, expected_container_id: str) -> bool:
        """Remove an idle box after its only admitted member closed its compute.

        A box admitted for several members stays alive because a sibling may be
        starting. ensure_box() recreates the box for the member's next run.
        """
        with self.locked():
            identities = sorted(path.name for path in self.supervisor.glob('member-*.json')
                                if re.fullmatch(r'member-\d+\.json', path.name))
            if identities != [f'member-{self.binding.uid}.json']:
                return False
            inspected = self._inspect()
            if inspected is None:
                return True
            if inspected['Id'] != expected_container_id:
                raise WorkspaceBoxUnavailable('Workspace generation changed before box release')
            self.docker(['rm', '-f', expected_container_id])
            if self._inspect() is not None:
                raise WorkspaceBoxUnavailable('Workspace box release is unconfirmed')
            return True

    def discard_empty_prepared_workspace(self) -> bool:
        """Remove only this member's empty, never-started native skeleton."""
        with self.locked():
            identities = sorted(path.name for path in self.supervisor.glob('member-*.json')
                                if re.fullmatch(r'member-\d+\.json', path.name))
            if identities != [f'member-{self.binding.uid}.json'] or self._inspect() is not None:
                return False
            uid = str(self.binding.uid)
            home = self.member_root / 'home'
            expected = (
                (self.root, {'native', 'common'}),
                (self.root / 'common', set()),
                (self.native_root, {'members', '.mount-receipt'}),
                (self.native_root / 'members', {uid}),
                (self.member_root, {'home', 'worktree'}),
                (self.member_root / 'worktree', set()),
                (home, {'tmp', '.config', '.cache'}),
                (home / 'tmp', set()),
                (home / '.config', set()),
                (home / '.cache', set()),
            )
            try:
                for path, children in expected:
                    info = path.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                        return False
                    if {entry.name for entry in path.iterdir()} != children:
                        return False
                marker = self.native_root / '.mount-receipt'
                info = marker.lstat()
                expected_marker = self.binding.owner_digest + ':' + self.binding.workspace_id
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or info.st_size != len(expected_marker)
                        or marker.read_text() != expected_marker):
                    return False
            except (OSError, UnicodeError):
                return False
            # Every object is now an exact service-owned empty directory or the
            # matching receipt. rmdir refuses if another writer adds content.
            try:
                for path in (home / 'tmp', home / '.config', home / '.cache',
                             home, self.member_root / 'worktree', self.member_root,
                             self.native_root / 'members', self.root / 'common'):
                    path.rmdir()
                marker.unlink()
                self.native_root.rmdir()
                self.root.rmdir()
            except OSError:
                return False
            return True

    def release_if_all_members_idle(self, idle_member_uids) -> bool:
        """Remove an idle shared box under its generation lock.

        The callback re-reads durable membership while the box lock prevents
        another member from entering this generation.
        """
        with self.locked():
            identities = {
                int(path.stem.split('-')[1])
                for path in self.supervisor.glob('member-*.json')
                if re.fullmatch(r'member-\d+\.json', path.name)
            }
            idle = idle_member_uids()
            if (
                not identities
                or not isinstance(idle, dict)
                or identities != idle.get('uids')
            ):
                return False
            inspected = self._inspect()
            if inspected is None:
                return False
            container_id = str(inspected['Id'])
            state = inspected.get('State') or {}
            if state.get('Running') is True:
                from .workspace_resources import process_counts
                observed_processes = process_counts(
                    self.docker(['top', container_id, '-eLo', 'uid,pid,lwp']).stdout
                )
                # Every admitted member was stopped. An unknown UID is an
                # orphan, never proof that the box is idle.
                if any(uid != 65534 for uid in observed_processes):
                    return False
            elif state.get('Pid') != 0:
                raise WorkspaceBoxUnavailable('Idle workspace process state is unconfirmed')
            observed = self._inspect()
            if observed is None or observed['Id'] != container_id:
                raise WorkspaceBoxUnavailable('Workspace generation changed before idle release')
            self.docker(['rm', '-f', container_id])
            if self._inspect() is not None:
                raise WorkspaceBoxUnavailable('Workspace box release is unconfirmed')
            return True

    def stop_member(self, expected_container_id: str) -> None:
        """Close one member's compute; the workspace box and siblings stay alive."""
        inspected = self._inspect()
        if inspected is None:
            return
        if inspected['Id'] != expected_container_id:
            raise WorkspaceBoxUnavailable('Workspace generation changed before member stop')
        if inspected['State'].get('Running') is False and inspected['State'].get('Pid') == 0:
            return
        script = r"""import os,signal,time
uid = os.getuid()
self_pid = os.getpid()
def live():
    found=[]
    for name in os.listdir('/proc'):
        if not name.isdecimal() or int(name)==self_pid: continue
        try:
            with open('/proc/'+name+'/status') as f: status=f.read()
            fields=dict(line.split(':',1) for line in status.splitlines() if ':' in line)
            if int(fields['Uid'].split()[0])==uid and not fields['State'].strip().startswith('Z'):
                found.append(int(name))
        except FileNotFoundError: continue
        except (OSError,ValueError,KeyError): raise SystemExit(2)
    return found
for sig in (signal.SIGTERM,signal.SIGKILL):
    deadline=time.monotonic()+2
    while True:
        targets=live()
        if not targets: raise SystemExit(0)
        for pid in targets:
            try: os.kill(pid,sig)
            except ProcessLookupError: pass
        if time.monotonic()>=deadline: break
        time.sleep(.05)
raise SystemExit(1 if live() else 0)
"""
        uid = self.binding.uid
        result = self.docker(['exec', '--user', f'{uid}:{uid}', expected_container_id,
                              *self.guarded_command(['python3', '-c', script])], check=False)
        if result.returncode:
            raise WorkspaceBoxUnavailable('Member compute termination is unconfirmed')
