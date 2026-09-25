"""Member-scoped adapter for the existing native CLI sandbox contract.

The shared box is workspace-owned. All native execs and controls use only the
admitted member UID; this adapter never removes or pauses the shared container.
"""
from __future__ import annotations

from dataclasses import replace
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
import json
import os
import stat
import subprocess

from .bootstrap import bootstrap_bundle_for
from .docker_sandbox import DockerSandboxManager, FreshSandboxInspection, SandboxInfo
from .workspace_box import WorkspaceBox, WorkspaceBoxUnavailable


_member_control = ContextVar("xperfect_member_control", default=False)


class WorkspaceMemberSandbox(DockerSandboxManager):
    def __init__(self, box: WorkspaceBox):
        super().__init__(base_dir=str(box.volume_root), create_directories=False)
        self.box = box
        self.assert_native_launch = lambda: None
        self.assert_native_resume = lambda _worker: False
        self.user = f'{box.binding.uid}:{box.member_gid}'
        self.home_mount = f'/workspace/data/members/{box.binding.uid}/home'
        self.workspace_mount = box.native_workspace
        self.runtime_root = box.supervisor
        self.image = box.image
        self.term_value = 'xterm-256color'
        self.display_value = ''

    def _member(self, worker_id):
        if worker_id != self.box.binding.worker_id:
            raise WorkspaceBoxUnavailable('Sandbox does not belong to this member')

    def paths(self, worker_id):
        self._member(worker_id)
        return self.box.prepare_member()

    _paths = paths

    def _container_name(self, worker_id):
        self._member(worker_id)
        return self.box.name

    def _info(self, value):
        paths = self.box.paths()
        state = 'running' if value['State']['Running'] else 'stopped'
        marker = self.box.supervisor / f'member-{self.box.binding.uid}-paused.json'
        if state == 'running' and marker.exists():
            if json.loads(marker.read_text()) != {'container_id': value['Id']}:
                raise WorkspaceBoxUnavailable('Paused member generation changed')
            state = 'paused'
        return SandboxInfo(container_name=self.box.name, container_id=value['Id'],
                           state=state,
                           workspace_dir=str(paths['workspace_dir']), home_dir=str(paths['home_dir']),
                           pid=value['State'].get('Pid'), image=self.image,
                           runtime_user=self.user, image_id=value['Image'])

    def inspect(self, worker_id):
        self._member(worker_id)
        self.box.prepare_member()
        # An absent receipt means this workspace has not launched a box yet.
        if not (self.box.supervisor / 'image.json').exists():
            return None
        value = self.box._inspect()
        return self._info(value) if value else None

    def inspect_fresh(self, worker_id, **kwargs):
        try:
            value = self.inspect(worker_id)
            return FreshSandboxInspection(status='present' if value else 'confirmed_absent', sandbox=value)
        except WorkspaceBoxUnavailable:
            return FreshSandboxInspection(status='unavailable', reason='workspace_attestation_failed')

    def fast_sandbox_from_worker(self, worker):
        return None

    def _ensure_image(self):
        self.box.ensure_box()

    def ensure_ready(self, worker, runtime_name, *, start_if_paused=True, repair_paths=True):
        self._member(worker['worker_id'])
        if (worker.get('tenant_id') != self.box.binding.tenant_id
                or worker.get('owner_id') != self.box.binding.owner_id):
            raise WorkspaceBoxUnavailable('Workspace principal differs from admitted membership')
        profile = str(worker.get('bootstrap_profile') or 'none')
        if profile not in {'none', 'clean-room'}:
            raise WorkspaceBoxUnavailable('Shared members require explicit private account and context projection')
        worker = {**worker, 'bootstrap_profile': profile}
        if bootstrap_bundle_for(worker).get('execution_policy'):
            raise WorkspaceBoxUnavailable('This execution policy has not been admitted for a shared box')
        if worker.get('_glasshive_provider_account_mount_host'):
            raise WorkspaceBoxUnavailable('Shared member account projection is not prepared')
        marker = self.box.supervisor / f'member-{self.box.binding.uid}-paused.json'
        if (start_if_paused and marker.exists()
                and worker.get('compute_release_kind') == 'resume_run'
                and self.assert_native_resume(worker)):
            inspected = self.box._inspect()
            container_id = str((inspected or {}).get('Id') or '')
            if not container_id or json.loads(marker.read_text()) != {'container_id': container_id}:
                raise WorkspaceBoxUnavailable('Paused member generation changed')
            self.box.set_member_paused(container_id, False)
            marker.unlink()
            return self.inspect(worker['worker_id'])
        self.assert_native_launch()
        container_id = self.box.ensure_box()
        if marker.exists():
            if not start_if_paused:
                return self.inspect(worker['worker_id'])
            if json.loads(marker.read_text()) != {'container_id': container_id}:
                raise WorkspaceBoxUnavailable('Paused member generation changed')
            self.box.set_member_paused(container_id, False)
            marker.unlink()
        paths = self.box.paths()
        self._seed_bootstrap(paths['home_dir'], self.box.member_root / 'worktree', runtime_name, worker,
                             trusted_state_dir=paths['state_dir'])
        if repair_paths:
            self.ensure_container_writable_paths(worker['worker_id'], runtime_name,
                                                 [self.home_mount, self.workspace_mount], worker=worker)
        return self.inspect(worker['worker_id'])

    def _desktop_env(self):
        return {'HOME': self.home_mount, 'TERM': self.term_value,
                'TMPDIR': self.home_mount + '/tmp', 'XDG_CONFIG_HOME': self.home_mount + '/.config',
                'XDG_CACHE_HOME': self.home_mount + '/.cache',
                'SCREENDIR': self.home_mount + '/.screen'}

    def _docker_exec(self, container_name, command, *, env=None, cwd=None, user=None, **kwargs):
        if user is not None and user != self.user:
            raise WorkspaceBoxUnavailable('Shared native execution cannot change member identity')
        if not _member_control.get():
            self.assert_native_launch()
        value = self.box._inspect()
        if value is None or container_name not in {self.box.name, value['Id']}:
            raise WorkspaceBoxUnavailable('Shared native execution generation changed')
        if any(key.startswith('LD_') or key in {'PYTHONHOME', 'PYTHONPATH'} for key in (env or {})):
            raise WorkspaceBoxUnavailable('Native environment cannot alter the trusted launcher')
        git_workspace_env = self.box.git_workspace_env()
        if any(key in (env or {}) for key in git_workspace_env):
            raise WorkspaceBoxUnavailable('Native environment cannot replace shared Git trust')
        for key, expected in self._desktop_env().items():
            if key in (env or {}) and env[key] != expected:
                raise WorkspaceBoxUnavailable('Native environment differs from member placement')
        from .workspace_projection import credential_command
        return super()._docker_exec(value['Id'], self.box.guarded_command(command if _member_control.get() else credential_command(command)), env={**(env or {}), **self._desktop_env(), **git_workspace_env},
                                    cwd=cwd or self.workspace_mount, user=self.user, **kwargs)

    def _ensure_screen_runtime_dir(self, container_name, *, clean_room=False):
        result = self._docker_exec(container_name, ['sh', '-c',
            'umask 077; mkdir -p "$SCREENDIR"; chmod 700 "$SCREENDIR"'])
        if result.returncode:
            raise WorkspaceBoxUnavailable('Native member terminal directory is unavailable')

    def ensure_container_writable_paths(self, worker_id, runtime_name, container_paths, *, worker=None):
        self._member(worker_id)
        # Only service-owned files need ACL repair. Never follow native links or
        # chmod native-owned files; their enclosing member directory is the boundary.
        roots = {self.home_mount: self.box.paths()['home_dir'],
                 self.workspace_mount: self.box.paths()['workspace_dir']}
        for raw in container_paths:
            path = PurePosixPath(raw)
            if '..' in path.parts:
                raise WorkspaceBoxUnavailable('Invalid member write path')
            local = None
            for mount, root in roots.items():
                if path == PurePosixPath(mount) or PurePosixPath(mount) in path.parents:
                    local = root / path.relative_to(mount)
                    break
            if local is None:
                raise WorkspaceBoxUnavailable('Write path is outside the admitted member')
            for parent in [local, *local.parents]:
                if parent == root.parent:
                    break
                if parent.is_symlink():
                    raise WorkspaceBoxUnavailable('Member write path traverses a link')
            for directory, dirs, files in os.walk(local, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                for item in [Path(directory), *(Path(directory) / name for name in files)]:
                    metadata = item.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                        continue
                    if self.box.file_placement == 'common' and root == self.box.paths()['common_dir']:
                        WorkspaceBox._common_acl(item)
                    elif stat.S_ISDIR(metadata.st_mode):
                        WorkspaceBox._private_acl(item, self.box.binding.uid)
                    elif stat.S_ISREG(metadata.st_mode):
                        result = subprocess.run(['setfacl', '-m', f'u:{self.box.binding.uid}:' + ('rwx' if metadata.st_mode & 0o111 else 'rw'), str(item)],
                                                capture_output=True, text=True, timeout=5)
                        if result.returncode:
                            raise WorkspaceBoxUnavailable('Native run file access is unavailable')

    def terminate(self, worker_id, *, expected_container_id=None, expected_absent=False):
        self._member(worker_id)
        expected_id = str(expected_container_id or '').strip()
        if expected_absent and expected_id:
            raise WorkspaceBoxUnavailable('Member termination identity is contradictory')
        value = self.inspect(worker_id)
        if expected_absent:
            if value:
                raise WorkspaceBoxUnavailable('Workspace generation changed before member termination')
            return
        if value:
            if expected_id and value.container_id != expected_id:
                raise WorkspaceBoxUnavailable('Workspace generation changed before member termination')
            container_id = expected_id or value.container_id
            self.box.stop_member(container_id)
            (self.box.supervisor / f'member-{self.box.binding.uid}-paused.json').unlink(missing_ok=True)
            # A separate workspace's box holds its memory reservation until removed.
            self.box.release_if_sole_member(container_id)

    def pause(self, worker_id, *, expected_container_id=None):
        value = self.inspect(worker_id)
        if value is None:
            raise WorkspaceBoxUnavailable('Member compute is already absent')
        expected = expected_container_id or value.container_id
        self.box.set_member_paused(expected, True)
        marker = self.box.supervisor / f'member-{self.box.binding.uid}-paused.json'
        if not marker.exists():
            self.box._private_json(marker, {'container_id': expected})
        return replace(value, state='paused', pid=None)

    def stop_screen_session(self, worker_id, runtime_name, session_name, *, worker=None,
                            missing_ok=False, expected_container_id=None):
        self._member(worker_id)
        token = _member_control.set(True)
        try:
            return super().stop_screen_session(worker_id, runtime_name, session_name, worker=worker,
                missing_ok=missing_ok, expected_container_id=expected_container_id)
        finally:
            _member_control.reset(token)

    def terminate_run_processes(self, worker_id, runtime_name, run_id, *, worker=None,
                                missing_ok=False, expected_container_id=None):
        self._member(worker_id)
        # Reuse the existing exact run/PID control, not whole-UID termination for
        # a possibly stale run. Account cleanup separately proves whole UID absence.
        token = _member_control.set(True)
        try:
            return super().terminate_run_processes(worker_id, runtime_name, run_id, worker=worker,
                missing_ok=missing_ok, expected_container_id=expected_container_id)
        finally:
            _member_control.reset(token)

    def list_screen_sessions(self, worker_id, runtime_name, *, worker=None):
        self._member(worker_id)
        current = self.inspect(worker_id)
        if isinstance(worker, dict) and '_compute_release_container_id' in worker:
            expected_id = str(worker.get('_compute_release_container_id') or '').strip()
            if (current and current.container_id != expected_id) or (not expected_id and current):
                raise WorkspaceBoxUnavailable('Workspace generation changed before member session probe')
        if not current:
            return []
        # Teardown, restart recovery and session discovery read an existing member's
        # sessions while its run's credential projection may still be pending. A read
        # never prepares or starts the box and must not call the launch guard, which is
        # only for starting new member work; it reads exactly the current box.
        token = _member_control.set(True)
        try:
            return super().list_screen_sessions(
                worker_id, runtime_name,
                worker={**(worker or {'worker_id': worker_id}),
                        '_compute_release_container_id': current.container_id})
        finally:
            _member_control.reset(token)

    def screen_session_pid(self, worker_id, runtime_name, session_name, *, worker=None):
        self._member(worker_id)
        # Restart identity and stale-session proofs are reads of that same session.
        token = _member_control.set(True)
        try:
            return super().screen_session_pid(worker_id, runtime_name, session_name, worker=worker)
        finally:
            _member_control.reset(token)

    def harden_worker_host_tree(self, worker_id):
        self._member(worker_id)
        self.box.prepare_member()

    def exec_command(self, worker_id, runtime_name, command, env=None, worker=None):
        self._member(worker_id)
        self.ensure_ready(worker or {'worker_id': worker_id,
            'tenant_id': self.box.binding.tenant_id, 'owner_id': self.box.binding.owner_id},
            runtime_name, repair_paths=False)
        return self.box.command(command, env=env)

    def terminal_attach_command(self, worker_id, runtime_name, session_name='operator'):
        self._member(worker_id)
        self.assert_native_launch()
        self._ensure_screen_runtime_dir(self.box.ensure_box())
        command = self.box.command(['screen', '-xRR', session_name], env={'SCREENDIR': self.home_mount + '/.screen'})
        command[2] = '-it'
        return command

    def describe(self, worker_id):
        value = self.inspect(worker_id)
        return {'container_id': value.container_id if value else '', 'state': value.state if value else 'absent',
                'view_available': False, 'workspace_id': self.box.binding.workspace_id}
