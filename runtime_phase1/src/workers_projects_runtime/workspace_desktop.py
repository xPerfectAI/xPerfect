"""On-demand graphical displays for the existing isolated workspace members.

Native execution stays in its workspace box. A display-only container has the
same network namespace and only private display state; the existing controller serves
the installed noVNC gateway without publishing another host port.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import copy
import fcntl
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import struct
import subprocess
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from .workspace_box import WorkspaceBoxUnavailable
from .workspace_box import WorkspaceMemberBinding
from .credential_projection import CredentialProjection


GATEWAY_PORT = 7900
DISPLAY_MEMORY_BYTES = 512 * 1024**2
DISPLAY_PIDS_LIMIT = 256
DESKTOP_SECCOMP_SHA256 = 'f2fba29948746aa17af5601d59ccf36d3e1fcb6213325e2861d676a59fd54cc3'
_HEX = re.compile(r"[a-f0-9]{64}")


def desktop_sandbox_policy():
    path = Path(__file__).with_name('desktop_chromium_seccomp.json')
    try:
        content = path.read_bytes()
        if sha256(content).hexdigest() != DESKTOP_SECCOMP_SHA256:
            raise ValueError
        return path, json.loads(content)
    except (OSError, ValueError):
        raise WorkspaceBoxUnavailable('The reviewed browser sandbox policy is unavailable') from None


class WorkspaceMemberDesktop:
    def __init__(self, box):
        self.box = box
        self.uid = box.binding.uid
        self.home = box.paths()["home_dir"] / ".xperfect-desktop"
        self.native_home = f"/workspace/data/members/{self.uid}/home"
        self.native_dir = self.native_home + "/.xperfect-desktop"
        self.display_mount = "/workspace/desktop"
        self.state_dir = box.supervisor / f"member-{self.uid}-desktop"
        self.state_path = self.state_dir / "display.json"
        self.gateway_dir = Path(os.environ.get("XPERFECT_SHARED_VOLUME_ROOT", "")) / ".xperfect-desktop-gateway"
        self.anchor_dir = box.volume_root / ".desktop"
        self.controller = os.environ.get("XPERFECT_CONTROLLER_ID", "")
        self.deployment_id = self.deployment_for_volume(box.volume_name)

    @staticmethod
    def deployment_for_volume(volume_name):
        return sha256(str(volume_name).encode()).hexdigest()

    @property
    def supported(self):
        return bool(_HEX.fullmatch(self.controller) and self.gateway_dir.is_absolute()
                    and os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL"))

    def environment(self):
        # Constants only: an already-running harness must be able to use a
        # display opened later, without rewriting its context or restarting it.
        return {"DISPLAY": f":{self.uid}", "XAUTHORITY": self.native_dir + "/Xauthority"}

    @contextmanager
    def _locked(self):
        self.box._directory(self.state_dir, 0o700)
        descriptor = os.open(self.state_dir / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_state(self):
        if not self.state_path.exists():
            return None
        info = self.state_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise WorkspaceBoxUnavailable("Desktop state is not private")
        value = json.loads(self.state_path.read_text())
        if (value.get("worker_id") != self.box.binding.worker_id
                or value.get("workspace_id") != self.box.binding.workspace_id
                or value.get("uid") != self.uid
                or not _HEX.fullmatch(str(value.get("container_id") or ""))
                or not _HEX.fullmatch(str(value.get("box_id") or ""))
                or not _HEX.fullmatch(str(value.get("token") or ""))
                or not _HEX.fullmatch(str(value.get('controller_id') or ''))
                or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('generation') or ''))
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', str(value.get('image') or ''))
                or type(value.get('ready')) is not bool
                or type(value.get('directory_device')) is not int
                or type(value.get('directory_inode')) is not int):
            raise WorkspaceBoxUnavailable("Desktop state does not match this member")
        return value

    def _write_state(self, value):
        temporary = self.state_dir / (".display-" + secrets.token_hex(8))
        self.box._private_json(temporary, value)
        os.replace(temporary, self.state_path)

    def _record(self, state):
        result = self.box.docker(["inspect", state["container_id"]], check=False)
        if result.returncode:
            listing = self.box.docker(["container", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"])
            if state["container_id"] in listing.stdout.splitlines():
                raise WorkspaceBoxUnavailable("Desktop generation cannot be inspected")
            return None
        try:
            record = json.loads(result.stdout)[0]
            host, config = record["HostConfig"], record["Config"]
            options = host.get('SecurityOpt') or []
            policies = [value.split('=', 1)[1] for value in options if value.startswith('seccomp=')]
            _, expected_policy = desktop_sandbox_policy()
            policy_matches = (len(options) == 2 and len(policies) == 1
                              and any(value in {'no-new-privileges', 'no-new-privileges:true'} for value in options)
                              and json.loads(policies[0]) == expected_policy)
            labels = config.get("Labels") or {}
            mount = host["Mounts"]
            if (record["Id"] != state["container_id"] or record["Image"] != state["image"]
                    or labels.get("xperfect.desktop.worker") != self.box.binding.worker_id
                    or labels.get("xperfect.desktop.owner") != self.box.binding.owner_digest
                    or labels.get("xperfect.desktop.generation") != state["generation"]
                    or labels.get("xperfect.desktop.controller") != state['controller_id']
                    or labels.get('xperfect.desktop.deployment') != self.deployment_id
                    or config.get("User") != f"{self.uid}:{self.box.member_gid}"
                    or config.get('Entrypoint') != ['/bin/sleep'] or config.get('Cmd') != ['infinity']
                    or host.get("NetworkMode") != "container:" + state["box_id"]
                    or not host.get("ReadonlyRootfs") or set(host.get("CapDrop") or []) != {"ALL"}
                    or host.get("CapAdd") or host.get("Privileged")
                    or not policy_matches
                    or host.get("Binds") or host.get("Devices") or host.get("PortBindings")
                    or host.get("PidMode") not in {"", None} or host.get("IpcMode") != "none"
                    or host.get("DeviceRequests")
                    or host.get("Memory") != DISPLAY_MEMORY_BYTES
                    or host.get("MemorySwap") != DISPLAY_MEMORY_BYTES
                    or host.get("PidsLimit") != DISPLAY_PIDS_LIMIT
                    or len(mount) != 1 or mount[0].get("Type") != "volume"
                    or mount[0].get("Source") != self.box.volume_name
                    or mount[0].get("Target") != self.display_mount
                    or (mount[0].get("VolumeOptions") or {}).get("Subpath") != self._display_subpath()
                    or (mount[0].get("VolumeOptions") or {}).get("NoCopy") is not True):
                raise ValueError
            return record
        except (KeyError, ValueError, TypeError, IndexError):
            raise WorkspaceBoxUnavailable("Desktop container does not match its member isolation contract") from None

    def _display_subpath(self):
        return self.box._workspace_subpath() + f"/native/members/{self.uid}/home/.xperfect-desktop"

    def _private_member_file(self, directory, name, content):
        flags = os.O_WRONLY | os.O_NOFOLLOW
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise WorkspaceBoxUnavailable("A reserved desktop file was changed")
        else:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (info is not None and (opened.st_uid != self.uid
                        or stat.S_IMODE(opened.st_mode) != 0o600
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)))):
                raise WorkspaceBoxUnavailable("A reserved desktop file changed before its write")
            os.ftruncate(stream.fileno(), 0)
            os.fchown(stream.fileno(), self.uid, self.box.member_gid)
            stream.write(content)

    @contextmanager
    def _display_directory(self, *, create=False):
        """Reuse the credential owner's no-follow directory descriptor pattern."""
        try:
            parent = CredentialProjection._open_directory(self.home.parent)
        except OSError:
            raise WorkspaceBoxUnavailable('Desktop parent directory is not a real private path') from None
        descriptor = None
        try:
            receipt = self.state_dir / 'directory.json'
            identity = None
            if receipt.exists():
                info = receipt.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise WorkspaceBoxUnavailable('Desktop directory identity is not private')
                identity = json.loads(receipt.read_text())
            elif create:
                try:
                    os.mkdir(self.home.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    raise WorkspaceBoxUnavailable('The reserved display directory already exists without its identity') from None
            else:
                raise WorkspaceBoxUnavailable('Desktop directory identity is missing')
            descriptor = os.open(self.home.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(descriptor)
            if (info.st_uid != os.geteuid() or info.st_mode & 0o007
                    or (identity is not None and identity != {'device': info.st_dev, 'inode': info.st_ino})):
                raise WorkspaceBoxUnavailable('Desktop directory was replaced')
            if identity is None:
                self.box._private_acl(self.home, self.uid, descriptor=descriptor)
                self.box._private_json(receipt, {'device': info.st_dev, 'inode': info.st_ino})
            yield descriptor
        except OSError:
            raise WorkspaceBoxUnavailable('Desktop directory could not be authorized') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def _prepare_home(self, directory, password):
        for name in ('tmp', '.cache', '.config'):
            try:
                os.mkdir(name, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                self.box._private_acl(self.home / name, self.uid, descriptor=child)
            finally:
                os.close(child)
        # Standard Xauthority FamilyWild record. The private cookie is scoped to
        # this X server; clients in the box and sidecar have different hostnames.
        fields = [b"", str(self.uid).encode(), b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16)]
        authority = struct.pack("!H", 65535) + b"".join(struct.pack("!H", len(value)) + value for value in fields)
        self._private_member_file(directory, "Xauthority", authority)
        self._private_member_file(directory, "password", (password + "\n").encode())
        try:
            info = os.stat('vnc.sock', dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                raise WorkspaceBoxUnavailable("The desktop socket was changed")
            os.unlink('vnc.sock', dir_fd=directory)

    def _script(self):
        auth = shlex.quote(self.display_mount + "/Xauthority")
        directory = shlex.quote(self.display_mount)
        return (
            "set -eu; export DISPLAY=:" + str(self.uid) + "; export XAUTHORITY=" + auth + "; "
            "Xvfb \"$DISPLAY\" -screen 0 1280x800x24 -auth \"$XAUTHORITY\" -nolisten tcp "
            ">" + directory + "/xvfb.log 2>&1 & xp=$!; "
            "trap 'kill \"$xp\" 2>/dev/null || true' EXIT HUP INT TERM; "
            "ready=0; for n in $(seq 1 40); do if xdpyinfo >/dev/null 2>&1; then ready=1; break; fi; "
            "kill -0 \"$xp\" 2>/dev/null || exit 1; sleep .1; done; test \"$ready\" = 1; "
            "fluxbox >" + directory + "/window-manager.log 2>&1 & "
            "x11vnc -display \"$DISPLAY\" -auth \"$XAUTHORITY\" -unixsock " + directory + "/vnc.sock "
            "-rfbport 0 -no6 -forever -shared -passwdfile " + directory + "/password -noxdamage "
            ">" + directory + "/vnc.log 2>&1"
        )

    def start(self, password):
        try:
            return self._start_controlled(password)
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise WorkspaceBoxUnavailable('Workspace storage is full. Remove unneeded files and open the browser again.') from None
            raise WorkspaceBoxUnavailable('The private browser could not be prepared. Please try again.') from None

    def _start_controlled(self, password):
        if not self.supported:
            raise WorkspaceBoxUnavailable("This runtime has no configured private desktop gateway")
        # A read/reuse does not reserve another display. New startup shares the
        # existing capacity lock and exact box lock with idle release.
        with self.box.locked(), self._locked():
            existing = self._reuse(password)
            if existing:
                return existing
        capacity = getattr(self.box, 'desktop_capacity', None)
        if capacity is None:
            raise WorkspaceBoxUnavailable('Desktop capacity authority is unavailable')
        with capacity(), self.box.locked(), self._locked():
            existing = self._reuse(password)
            if existing:
                return existing
            return self._start_locked(password)

    def _reuse(self, password):
        box = self.box._inspect()
        if not box or not box['State'].get('Running'):
            raise WorkspaceBoxUnavailable('Start the workspace before opening its desktop')
        state = self._read_state()
        if state is None:
            return None
        record = self._record(state)
        if record and record['State'].get('Running') and state.get('ready'):
            if state['box_id'] != box['Id']:
                raise WorkspaceBoxUnavailable('Desktop belongs to another workspace generation')
            self._publish(state)
            return self._view_url(state, password)
        self._stop_locked(state)
        return None

    def _start_locked(self, password):
        box = self.box._inspect()
        if not box or not box['State'].get('Running'):
            raise WorkspaceBoxUnavailable('Workspace compute is unavailable')
        with self._display_directory(create=True) as directory:
            self._prepare_home(directory, password)
            pinned = os.fstat(directory)
            generation = secrets.token_hex(16)
            name = 'xperfect-desktop-' + box['Id'][:12] + '-' + str(self.uid)
            policy, _ = desktop_sandbox_policy()
            arguments = ['run', '-d', '--init', '--name', name, '--network', 'container:' + box['Id'],
                         '--user', f'{self.uid}:{self.box.member_gid}', '--read-only', '--cap-drop', 'ALL',
                         '--security-opt', 'no-new-privileges', '--ipc', 'none', '--no-healthcheck',
                         '--security-opt', 'seccomp=' + str(policy),
                         '--memory', str(DISPLAY_MEMORY_BYTES), '--memory-swap', str(DISPLAY_MEMORY_BYTES),
                         '--pids-limit', str(DISPLAY_PIDS_LIMIT), '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m,mode=1777',
                         '--label', 'xperfect.desktop.worker=' + self.box.binding.worker_id,
                         '--label', 'xperfect.desktop.owner=' + self.box.binding.owner_digest,
                         '--label', 'xperfect.desktop.generation=' + generation,
                         '--label', 'xperfect.desktop.controller=' + self.controller,
                         '--label', 'xperfect.desktop.deployment=' + self.deployment_id,
                         '--mount', f'type=volume,src={self.box.volume_name},dst={self.display_mount},volume-subpath={self._display_subpath()},volume-nocopy',
                         '--env', 'HOME=' + self.display_mount, '--entrypoint', '/bin/sleep', box['Image'], 'infinity']
            result = self.box.docker(arguments)
            container_id = result.stdout.strip()
            if not _HEX.fullmatch(container_id):
                raise WorkspaceBoxUnavailable('Desktop startup did not return an exact container identity')
            state = {'worker_id': self.box.binding.worker_id, 'workspace_id': self.box.binding.workspace_id,
                     'uid': self.uid, 'container_id': container_id, 'box_id': box['Id'], 'image': box['Image'],
                     'generation': generation, 'token': secrets.token_hex(32), 'ready': False,
                     'controller_id': self.controller,
                     'directory_device': pinned.st_dev, 'directory_inode': pinned.st_ino}
            try:
                self._write_state(state)
                # A native member can rename its parent directory while Docker
                # opens a subpath. Start only sleep, inspect the bound inode, then
                # permit graphical processes; never read an unverified home.
                check = 'import os,json; s=os.stat(' + repr(self.display_mount) + '); print(json.dumps([s.st_dev,s.st_ino]))'
                observed = json.loads(self.box.docker(['exec', '--user', f'{self.uid}:{self.box.member_gid}',
                                                       container_id, '/usr/bin/python3', '-c', check]).stdout)
                if observed != [pinned.st_dev, pinned.st_ino]:
                    raise WorkspaceBoxUnavailable('Desktop directory changed during its mount')
                self.box.docker(['exec', '-d', '--user', f'{self.uid}:{self.box.member_gid}', container_id,
                                 '/bin/sh', '-c', self._script()])
                deadline = time.monotonic() + 6
                while True:
                    try:
                        info = os.stat('vnc.sock', dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        info = None
                    if info is not None:
                        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                            raise WorkspaceBoxUnavailable('Desktop did not create its own private socket')
                        break
                    if time.monotonic() >= deadline:
                        raise WorkspaceBoxUnavailable('The graphical display did not become ready')
                    time.sleep(.1)
                current = self.box._inspect()
                record = self._record(state)
                if not current or current['Id'] != state['box_id'] or not record or not record['State'].get('Running'):
                    raise WorkspaceBoxUnavailable('Desktop generation changed during startup')
                self._publish(state)
                state['ready'] = True
                self._write_state(state)
                return self._view_url(state, password)
            except Exception:
                self._stop_locked(state)
                raise

    def _view_url(self, state, password):
        base = urlsplit(os.environ["GLASSHIVE_PEER_RUNTIME_BASE_URL"])
        if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
            raise WorkspaceBoxUnavailable("The private desktop gateway address is invalid")
        return urlunsplit(("http", f"{base.hostname}:{GATEWAY_PORT}", "/", urlencode({
            "autoconnect": "1", "resize": "scale", "path": "websockify",
            "token": state["token"], "password": password}), ""))

    def view(self, box_id, password):
        if not self.supported or not self.state_path.is_file():
            return None
        state = self._read_state()
        if state is None or state["box_id"] != box_id or not state.get('ready'):
            return None
        with self._display_directory() as directory:
            pinned = os.fstat(directory)
            if [pinned.st_dev, pinned.st_ino] != [state['directory_device'], state['directory_inode']]:
                raise WorkspaceBoxUnavailable('Desktop directory identity changed')
        record = self._record(state)
        if record is None or not record["State"].get("Running"):
            return None
        return self._view_url(state, password)

    def launch_application(self, command):
        state = self._read_state()
        record = self._record(state) if state else None
        box = self.box._inspect()
        if (not state or not state.get('ready') or not record or not record['State'].get('Running')
                or not box or box['Id'] != state['box_id']):
            raise WorkspaceBoxUnavailable('The private desktop is not ready')
        # GUI clients stay in the display container. Pausing native member work
        # therefore leaves the owner's browser usable, without resuming AI.
        environment = ['--env', 'HOME=' + self.display_mount,
                       '--env', f'DISPLAY=:{self.uid}',
                       '--env', 'XAUTHORITY=' + self.display_mount + '/Xauthority',
                       '--env', 'TMPDIR=' + self.display_mount + '/tmp',
                       '--env', 'XDG_CONFIG_HOME=' + self.display_mount + '/.config',
                       '--env', 'XDG_CACHE_HOME=' + self.display_mount + '/.cache']
        self.box.docker(['exec', '--user', f'{self.uid}:{self.box.member_gid}', *environment,
                         state['container_id'], 'xdpyinfo'])
        # Confirm an immediate native failure instead of treating detached
        # Docker acceptance as a successful browser launch. The log stays in
        # the member's private display state; never return its contents.
        program = '''import json,os,stat,subprocess,time
path=ROOT+'/browser.log'
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
info=os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1:raise SystemExit('Private browser log changed')
os.ftruncate(fd,0);os.fchmod(fd,0o600)
p=subprocess.Popen(COMMAND,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,start_new_session=True)
os.close(fd);time.sleep(.3)
code=p.poll();print(json.dumps({'exit_code':code,'accepted':code in (None,0)}))
'''.replace('ROOT', repr(self.display_mount)).replace('COMMAND', repr(command))
        outcome = json.loads(self.box.docker(['exec', '--user', f'{self.uid}:{self.box.member_gid}', *environment,
                                             state['container_id'], '/usr/bin/python3', '-c', program]).stdout)
        if outcome.get('accepted') is not True:
            raise WorkspaceBoxUnavailable('The browser stopped during startup. Close unused tabs and try again.')

    def resource_record(self, expected_box_id=None):
        """Read-only exact display cgroup authority for the existing accountant."""
        if not self.state_path.is_file():
            return None
        state = self._read_state()
        if state is None:
            return None
        record = self._record(state)
        if record is None or not record['State'].get('Running'):
            return None
        if not expected_box_id or state['box_id'] != expected_box_id:
            raise WorkspaceBoxUnavailable('Live desktop has another workspace generation')
        with self._display_directory() as directory:
            pinned = os.fstat(directory)
            if [pinned.st_dev, pinned.st_ino] != [state['directory_device'], state['directory_inode']]:
                raise WorkspaceBoxUnavailable('Desktop directory identity changed')
        return record

    @staticmethod
    def has_live_desktops(box, expected_box_id, member_uids):
        """Keep an idle parent while a registered human display still uses it."""
        live = False
        for path in box.supervisor.glob('member-*-desktop/display.json'):
            match = re.fullmatch(r'member-(\d+)-desktop', path.parent.name)
            if not match:
                raise WorkspaceBoxUnavailable('Desktop member identity is invalid')
            uid = int(match[1])
            member_path = box.supervisor / f'member-{uid}.json'
            info = member_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise WorkspaceBoxUnavailable('Desktop member identity is not private')
            member = json.loads(member_path.read_text())
            if member.get('uid') != uid:
                raise WorkspaceBoxUnavailable('Desktop member identity changed')
            binding = WorkspaceMemberBinding(box.binding.workspace_id, member['worker_id'],
                                             box.binding.tenant_id, box.binding.owner_id, uid)
            scoped = copy(box)
            scoped.binding = binding
            scoped.member_root = box.native_root / 'members' / str(uid)
            desktop = WorkspaceMemberDesktop(scoped)
            record = desktop.resource_record(expected_box_id)
            if record is not None:
                if uid not in member_uids:
                    raise WorkspaceBoxUnavailable('Unregistered desktop member is still alive')
                live = True
        return live

    @contextmanager
    def _gateway_lock(self):
        path = self.box.control_root / "desktop-gateway.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _gateway_directory(self):
        if not self.gateway_dir.exists():
            self.gateway_dir.mkdir(mode=0o700)
            os.chown(self.gateway_dir, 65534, 65534)
        info = self.gateway_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 65534 or stat.S_IMODE(info.st_mode) != 0o700:
            raise WorkspaceBoxUnavailable("The private desktop gateway directory changed")

    def _anchor_directory(self):
        # XFS project quotas refuse cross-project hard links. Keep each socket
        # anchor in its existing owner project, rather than exporting user data
        # to the controller's storage project. The gateway receives traversal
        # only; private execution/member trees keep their independent ACLs.
        data_root = self.gateway_dir.parent
        if not self.box.volume_root.is_relative_to(data_root):
            raise WorkspaceBoxUnavailable("Desktop owner storage is outside this deployment")
        parent = self.box.volume_root
        while True:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o022):
                raise WorkspaceBoxUnavailable("Desktop owner traversal is unsafe")
            result = subprocess.run(["setfacl", "-m", "u:65534:--x", str(parent)],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode:
                raise WorkspaceBoxUnavailable("Private desktop traversal could not be granted")
            if parent == data_root:
                break
            parent = parent.parent
        if not self.anchor_dir.exists():
            self.anchor_dir.mkdir(mode=0o700)
            os.chown(self.anchor_dir, 65534, 65534)
        info = self.anchor_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 65534 or stat.S_IMODE(info.st_mode) != 0o700:
            raise WorkspaceBoxUnavailable("Desktop owner anchor directory changed")

    def _routes(self):
        path = self.gateway_dir / "tokens"
        if not path.exists():
            return {}
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 65534 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise WorkspaceBoxUnavailable("Desktop route configuration is not private")
        rows = {}
        for line in path.read_text().splitlines():
            token, separator, target = line.partition(": unix_socket:")
            if (not separator or not _HEX.fullmatch(token)
                    or not Path(target).is_relative_to(self.gateway_dir.parent)
                    or ".." in Path(target).parts or Path(target).parent.name != ".desktop"
                    or not re.fullmatch(r"[a-f0-9]{16}\.s", Path(target).name)):
                raise WorkspaceBoxUnavailable("Desktop route configuration was changed")
            if token in rows:
                raise WorkspaceBoxUnavailable('Desktop route configuration contains a repeated token')
            rows[token] = target
        return rows

    def _save_routes(self, routes):
        temporary = self.gateway_dir / (".tokens-" + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchown(stream.fileno(), 65534, 65534)
            for token, path in sorted(routes.items()):
                stream.write(token + ": unix_socket:" + path + "\n")
        os.replace(temporary, self.gateway_dir / "tokens")

    def _publish(self, state):
        with self._gateway_lock(), self._display_directory() as directory:
            self._gateway_directory()
            self._anchor_directory()
            info = os.stat('vnc.sock', dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                raise WorkspaceBoxUnavailable("Desktop socket is not owned by this member")
            anchor = self.anchor_dir / (state["token"][:16] + ".s")
            if len(os.fsencode(str(anchor))) >= 108:
                raise WorkspaceBoxUnavailable("The private desktop socket path is too long")
            target = CredentialProjection._open_directory(self.anchor_dir)
            created = False
            try:
                try:
                    anchored = os.stat(anchor.name, dir_fd=target, follow_symlinks=False)
                except FileNotFoundError:
                    os.link('vnc.sock', anchor.name, src_dir_fd=directory,
                            dst_dir_fd=target, follow_symlinks=False)
                    created = True
                    anchored = os.stat(anchor.name, dir_fd=target, follow_symlinks=False)
                if (not stat.S_ISSOCK(anchored.st_mode) or anchored.st_uid != self.uid
                        or (anchored.st_dev, anchored.st_ino) != (info.st_dev, info.st_ino)):
                    raise WorkspaceBoxUnavailable("Desktop socket anchor changed")
                # Pin the final inode as well; no native rename can redirect
                # chmod or the trusted gateway to another member's object.
                socket_fd = os.open(anchor.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=target)
                try:
                    pinned = os.fstat(socket_fd)
                    if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
                        raise WorkspaceBoxUnavailable('Desktop socket changed before publication')
                    # Both names are behind independent private directories.
                    os.chmod(f'/proc/self/fd/{socket_fd}', 0o666)
                finally:
                    os.close(socket_fd)
            except Exception:
                if created:
                    os.unlink(anchor.name, dir_fd=target)
                raise
            finally:
                os.close(target)
            routes = self._routes()
            routes[state["token"]] = str(anchor)
            self._save_routes(routes)
            self._ensure_gateway()

    def _ensure_gateway(self):
        # The service image already contains these native tools. Docker exec
        # starts the gateway without granting SETUID or any new service caps.
        controller = json.loads(self.box.docker(["inspect", self.controller]).stdout)[0]
        if (controller.get("Id") != self.controller or not controller.get("State", {}).get("Running")
                or not any(item.get("Type") == "volume" and item.get("Name") == self.box.volume_name
                           and item.get("Destination") == str(self.gateway_dir.parent)
                           for item in controller.get("Mounts", []))):
            raise WorkspaceBoxUnavailable("Desktop controller storage identity changed")
        program = '''import json,os,pathlib,socket,subprocess,time
root=pathlib.Path(GATEWAY)
state=root/'process.json'
def identity(pid):
 p=pathlib.Path('/proc')/str(pid)
 try:
  fields=(p/'stat').read_text().split(') ',1)[1].split()
  command=(p/'cmdline').read_bytes()
  status=(p/'status').read_text()
  return fields[19],command,status
 except (OSError,IndexError):return None
if state.exists():
 d=json.loads(state.read_text()); current=identity(d['pid'])
 if current and current[0]==d['start'] and b'websockify' in current[1] and str(root/'tokens').encode() in current[1]:
  print(json.dumps({'ready':True}));raise SystemExit(0)
s=socket.socket();free=s.connect_ex(('127.0.0.1',7900))!=0;s.close()
if not free:raise SystemExit('Desktop gateway port is already in use')
p=subprocess.Popen(['/home/seluser/venv/bin/websockify','--web','/opt/bin/noVNC','--token-plugin','TokenFile','--token-source',str(root/'tokens'),'0.0.0.0:7900'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
current=identity(p.pid)
if not current:raise SystemExit('Desktop gateway did not start')
temp=root/('.process-'+str(p.pid));temp.write_text(json.dumps({'pid':p.pid,'start':current[0]}));temp.chmod(0o600);temp.replace(state)
for _ in range(30):
 s=socket.socket();ready=s.connect_ex(('127.0.0.1',7900))==0;s.close()
 if ready:print(json.dumps({'ready':True}));raise SystemExit(0)
 if p.poll() is not None:break
 time.sleep(.1)
raise SystemExit('Desktop gateway is not ready')
'''.replace("GATEWAY", repr(str(self.gateway_dir)))
        self.box.docker(["exec", "--user", "65534:65534", self.controller, "/usr/bin/python3", "-c", program])

    def stop(self):
        if not self.state_path.is_file():
            return
        with self.box.locked(), self._locked():
            state = self._read_state()
            if state:
                self._stop_locked(state)
        changed = getattr(self.box, 'desktop_changed', None)
        if changed:
            changed()

    def _stop_locked(self, state):
        record = self._record(state)
        if record is not None:
            self.box.docker(["rm", "-f", state["container_id"]])
        with self._gateway_lock():
            if self.gateway_dir.exists():
                self._gateway_directory()
                routes = self._routes()
                anchor = routes.pop(state["token"], None)
                self._save_routes(routes)
                if anchor:
                    path = Path(anchor)
                    if path.parent != self.anchor_dir or path.name != state['token'][:16] + '.s':
                        raise WorkspaceBoxUnavailable("Desktop route belongs to another owner")
                    if path.exists() or path.is_symlink():
                        info = path.lstat()
                        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                            raise WorkspaceBoxUnavailable("Desktop socket anchor changed before stop")
                        path.unlink()
                if not routes:
                    program = '''import json,os,pathlib,signal
root=pathlib.Path(GATEWAY);state=root/'process.json'
if state.exists():
 d=json.loads(state.read_text());p=pathlib.Path('/proc')/str(d['pid'])
 if p.exists():
  start=(p/'stat').read_text().split(') ',1)[1].split()[19];command=(p/'cmdline').read_bytes()
  if start!=d['start'] or b'websockify' not in command or str(root/'tokens').encode() not in command:raise SystemExit('Desktop gateway generation changed')
  os.kill(d['pid'],signal.SIGTERM)
 state.unlink()
'''.replace("GATEWAY", repr(str(self.gateway_dir)))
                    self.box.docker(["exec", "--user", "65534:65534", self.controller, "/usr/bin/python3", "-c", program])
        self.state_path.unlink(missing_ok=True)
