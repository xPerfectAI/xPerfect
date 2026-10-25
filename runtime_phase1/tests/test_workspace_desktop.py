from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from workers_projects_runtime.docker_sandbox import DockerSandboxManager, SandboxInfo
from workers_projects_runtime.profile_runtime import CodexCliRuntime
from workers_projects_runtime.workspace_box import WorkspaceBox, WorkspaceBoxUnavailable, WorkspaceMemberBinding
from workers_projects_runtime.workspace_desktop import WorkspaceMemberDesktop, DISPLAY_MEMORY_BYTES, DISPLAY_PIDS_LIMIT, desktop_sandbox_policy
from workers_projects_runtime.workspace_sandbox import WorkspaceMemberSandbox


def member(tmp_path, monkeypatch):
    monkeypatch.delenv('XPERFECT_CONTROLLER_ID', raising=False)
    monkeypatch.delenv('GLASSHIVE_PEER_RUNTIME_BASE_URL', raising=False)
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_ROOT', str(tmp_path / 'volume'))
    box = WorkspaceBox(volume_root=tmp_path / 'volume', volume_name='synthetic-volume', image='sha256:' + 'a' * 64,
                       binding=WorkspaceMemberBinding('wsp_test', 'wrk_test', 'tenant', 'owner', 20001),
                       memory_bytes=1024**3, pids_limit=256, control_root=tmp_path / 'control')
    box.supervisor.mkdir(parents=True)
    return box


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def state_for(desktop):
    return {'worker_id': 'wrk_test', 'workspace_id': 'wsp_test', 'uid': 20001,
            'container_id': 'b' * 64, 'box_id': 'c' * 64, 'image': 'sha256:' + 'a' * 64,
            'controller_id': 'd' * 64, 'generation': 'e' * 32, 'token': 'f' * 64,
            'ready': True, 'directory_device': 1, 'directory_inode': 2}


def display_record(desktop, state):
    return {'Id': state['container_id'], 'Image': state['image'], 'State': {'Running': True},
            'Config': {'User': '20001:20001', 'Entrypoint': ['/bin/sleep'], 'Cmd': ['infinity'],
                       'Labels': {'xperfect.desktop.worker': 'wrk_test',
                                  'xperfect.desktop.owner': desktop.box.binding.owner_digest,
                                  'xperfect.desktop.controller': state['controller_id'],
                                  'xperfect.desktop.deployment': desktop.deployment_id,
                                  'xperfect.desktop.generation': state['generation']}},
            'HostConfig': {'NetworkMode': 'container:' + state['box_id'], 'ReadonlyRootfs': True,
                           'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges', 'seccomp=' + json.dumps(desktop_sandbox_policy()[1])],
                           'IpcMode': 'none', 'Memory': DISPLAY_MEMORY_BYTES,
                           'MemorySwap': DISPLAY_MEMORY_BYTES, 'PidsLimit': DISPLAY_PIDS_LIMIT,
                           'Mounts': [{'Type': 'volume', 'Source': desktop.box.volume_name,
                                       'Target': desktop.display_mount,
                                       'VolumeOptions': {'Subpath': desktop._display_subpath(), 'NoCopy': True}}]}}


def test_full_browser_budget_is_bounded_and_old_limits_are_not_reused(tmp_path, monkeypatch):
    assert DISPLAY_MEMORY_BYTES == 512 * 1024**2
    assert DISPLAY_PIDS_LIMIT == 256
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    state = state_for(desktop)
    for field, old_value in [('Memory', 256 * 1024**2), ('PidsLimit', 96)]:
        record = display_record(desktop, state)
        record['HostConfig'][field] = old_value
        monkeypatch.setattr(desktop.box, 'docker', lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps([record]), ''))
        with pytest.raises(WorkspaceBoxUnavailable, match='isolation'):
            desktop._record(state)


def retained_budget_desktop(tmp_path, monkeypatch, memory, pids):
    from contextlib import contextmanager, nullcontext
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    state = state_for(desktop)
    folder = tmp_path / 'display-files'
    folder.mkdir()
    info = folder.stat()
    state.update(directory_device=info.st_dev, directory_inode=info.st_ino)
    private_json(desktop.state_path, state)
    record = display_record(desktop, state)
    record['HostConfig'].update(Memory=memory, MemorySwap=memory, PidsLimit=pids)
    live, calls = [True], []
    def docker(args, **kwargs):
        calls.append(args)
        if args[0] == 'inspect':
            return subprocess.CompletedProcess(args, 0 if live[0] else 1,
                                               json.dumps([record]) if live[0] else '', '')
        if args[:2] == ['container', 'ls']:
            return subprocess.CompletedProcess(args, 0, record['Id'] if live[0] else '', '')
        if args[:2] == ['rm', '-f']:
            assert args[2] == state['container_id']
            live[0] = False
            return subprocess.CompletedProcess(args, 0, '', '')
        raise AssertionError(args)
    @contextmanager
    def pinned(**kwargs):
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try: yield fd
        finally: os.close(fd)
    monkeypatch.setattr(desktop.box, 'docker', docker)
    monkeypatch.setattr(desktop.box, '_inspect', lambda: {'Id': state['box_id'], 'State': {'Running': True}})
    monkeypatch.setattr(desktop.box, 'locked', nullcontext)
    monkeypatch.setattr(desktop, '_locked', nullcontext)
    monkeypatch.setattr(desktop, '_display_directory', pinned)
    return desktop, state, record, live, calls


@pytest.mark.parametrize(('current_memory', 'current_pids', 'retained_memory', 'retained_pids'), [
    (512 * 1024**2, 256, 256 * 1024**2, 96),
    (256 * 1024**2, 96, 512 * 1024**2, 256),
])
def test_budget_change_keeps_owned_display_accountable_retirable_and_reopenable(
        tmp_path, monkeypatch, current_memory, current_pids, retained_memory, retained_pids):
    from contextlib import contextmanager
    import workers_projects_runtime.workspace_desktop as module
    from workers_projects_runtime.workspace_resources import WorkspaceResources
    monkeypatch.setattr(module, 'DISPLAY_MEMORY_BYTES', current_memory)
    monkeypatch.setattr(module, 'DISPLAY_PIDS_LIMIT', current_pids)
    desktop, state, record, live, calls = retained_budget_desktop(
        tmp_path, monkeypatch, retained_memory, retained_pids)
    monkeypatch.setattr(WorkspaceMemberDesktop, 'supported', property(lambda _: True))
    assert desktop.resource_record(state['box_id'])['HostConfig']['Memory'] == retained_memory
    assert desktop.view(state['box_id'], 'synthetic') is None
    with pytest.raises(WorkspaceBoxUnavailable, match='isolation'):
        desktop._record(state)
    resources = WorkspaceResources(object())
    monkeypatch.setattr(resources, '_proof_matches_configuration', lambda **_: True)
    monkeypatch.setattr(resources, '_measure_settled', lambda _: {
        'process_probe_ok': True, 'memory_probe_ok': True, 'disk_probe_ok': True,
        'available_memory_bytes': current_memory if not live[0] else 0})
    desktop.box.desktop_capacity = lambda: resources.desktop_capacity(object())
    opened = []
    def start(_):
        assert not live[0] and not desktop.state_path.exists()
        opened.append((module.DISPLAY_MEMORY_BYTES, module.DISPLAY_PIDS_LIMIT))
        return 'new-current-budget-view'
    monkeypatch.setattr(desktop, '_start_locked', start)
    assert desktop.start('synthetic') == 'new-current-budget-view'
    assert opened == [(current_memory, current_pids)]
    assert [row for row in calls if row[:2] == ['rm', '-f']] == [['rm', '-f', state['container_id']]]
    assert resources.cached is None


@pytest.mark.parametrize('mutation', ['owner', 'network', 'mount', 'policy'])
def test_budget_retirement_never_removes_foreign_or_weakened_desktop(tmp_path, monkeypatch, mutation):
    desktop, state, record, live, calls = retained_budget_desktop(tmp_path, monkeypatch, 256 * 1024**2, 96)
    if mutation == 'owner': record['Config']['Labels']['xperfect.desktop.owner'] = 'foreign'
    elif mutation == 'network': record['HostConfig']['NetworkMode'] = 'host'
    elif mutation == 'mount': record['HostConfig']['Mounts'][0]['Source'] = 'foreign-volume'
    else: record['HostConfig']['SecurityOpt'][1] = 'seccomp=unconfined'
    with pytest.raises(WorkspaceBoxUnavailable, match='isolation'):
        desktop.stop()
    assert live[0] and desktop.state_path.exists()
    assert not any(row[0] == 'rm' for row in calls)


def test_unbounded_owned_display_is_retirable_but_never_accounted_as_free(tmp_path, monkeypatch):
    desktop, state, record, live, calls = retained_budget_desktop(tmp_path, monkeypatch, 0, 0)
    with pytest.raises(WorkspaceBoxUnavailable, match='resource bounds'):
        desktop.resource_record(state['box_id'])
    desktop.stop()
    assert not live[0] and not desktop.state_path.exists()


def test_explicit_maintenance_desktop_retirement_never_starts_native_compute(monkeypatch):
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
    runtime = ProfiledWorkerRuntime.__new__(ProfiledWorkerRuntime)
    stopped = []
    worker = {'worker_id': 'wrk_test', 'state': 'paused'}
    scoped = SimpleNamespace(sandbox=SimpleNamespace(desktop=SimpleNamespace(stop=lambda: stopped.append(worker))))
    monkeypatch.setattr(runtime, '_runtime_for_worker', lambda current: scoped if current is worker else pytest.fail('wrong worker'))
    runtime.retire_idle_desktop_for_upgrade(worker)
    assert stopped == [worker]


def test_member_description_implements_real_native_runtime_contract(tmp_path, monkeypatch):
    box = member(tmp_path, monkeypatch)
    sandbox = WorkspaceMemberSandbox(box)
    info = SandboxInfo(container_name=box.name, container_id='c' * 64, state='running',
                       workspace_dir=str(box.paths()['workspace_dir']), home_dir=str(box.paths()['home_dir']),
                       pid=42, image=box.image)
    monkeypatch.setattr(sandbox, 'inspect', lambda _: info)
    runtime = CodexCliRuntime.__new__(CodexCliRuntime)
    runtime.sandbox = sandbox
    actual = runtime.describe_worker({'worker_id': 'wrk_test'})
    assert actual['sandbox_state'] == 'running'
    assert actual['workspace_dir'] == str(box.paths()['workspace_dir'])
    assert actual['container_id'] == 'c' * 64
    assert actual['mode'] == 'docker-workstation'
    assert actual['view_available'] is False


def test_unconfigured_graphical_action_does_not_start_compute(tmp_path, monkeypatch):
    sandbox = WorkspaceMemberSandbox(member(tmp_path, monkeypatch))
    monkeypatch.setattr(sandbox, 'ensure_ready', lambda *a, **k: pytest.fail('must not start'))
    with pytest.raises(WorkspaceBoxUnavailable, match='gateway'):
        sandbox.desktop_action('wrk_test', 'codex-cli', 'browser')


@pytest.mark.parametrize('action', ['terminal', 'files', 'codex', 'claude', 'openclaw'])
def test_display_only_sidecar_cannot_replace_native_work_or_account_tools(tmp_path, monkeypatch, action):
    sandbox = WorkspaceMemberSandbox(member(tmp_path, monkeypatch))
    monkeypatch.setattr(sandbox, 'ensure_ready', lambda *a, **k: pytest.fail('must not start'))
    with pytest.raises(WorkspaceBoxUnavailable, match='Terminal or Library'):
        sandbox.desktop_action('wrk_test', 'codex-cli', action)


def test_legacy_paused_desktop_is_not_silently_resumed(tmp_path, monkeypatch):
    sandbox = DockerSandboxManager(base_dir=str(tmp_path), create_directories=False)
    monkeypatch.setattr(sandbox, 'fast_sandbox_from_worker', lambda _: None)
    monkeypatch.setattr(sandbox, 'inspect', lambda _: SimpleNamespace(state='paused'))
    monkeypatch.setattr(sandbox, 'ensure_ready', lambda *a, **k: pytest.fail('must not resume'))
    with pytest.raises(RuntimeError, match='paused desktop'):
        sandbox.desktop_action('wrk_test', 'codex-cli', 'browser')


def test_native_pause_keeps_operator_display_and_exact_generation(tmp_path, monkeypatch):
    box = member(tmp_path, monkeypatch)
    sandbox = WorkspaceMemberSandbox(box)
    info = SandboxInfo(container_name=box.name, container_id='c' * 64, state='running',
                       workspace_dir='work', home_dir='home', pid=42, image=box.image)
    monkeypatch.setattr(sandbox, 'inspect', lambda _: info)
    monkeypatch.setattr(sandbox.desktop, 'stop', lambda: pytest.fail('human display must remain'))
    calls = []
    monkeypatch.setattr(box, 'set_member_paused', lambda cid, paused: calls.append((cid, paused)))
    assert sandbox.pause('wrk_test', expected_container_id='c' * 64).state == 'paused'
    assert calls == [('c' * 64, True)]
    with pytest.raises(WorkspaceBoxUnavailable, match='generation'):
        sandbox.pause('wrk_test', expected_container_id='b' * 64)
    assert len(calls) == 1


def test_member_state_cannot_select_a_foreign_worker(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    state = state_for(desktop)
    state['worker_id'] = 'wrk_other'
    private_json(desktop.state_path, state)
    with pytest.raises(WorkspaceBoxUnavailable, match='member'):
        desktop._read_state()


def test_private_state_rejects_redirected_file(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    target = tmp_path / 'unrelated.json'
    private_json(target, state_for(desktop))
    desktop.state_path.parent.mkdir(parents=True)
    desktop.state_path.symlink_to(target)
    with pytest.raises(WorkspaceBoxUnavailable, match='private'):
        desktop._read_state()
    assert target.read_text()


@pytest.mark.parametrize('mutation', ['owner', 'worker', 'deployment', 'creator', 'network', 'mount',
                                       'whole_home', 'user', 'privileged', 'memory', 'pids', 'ipc', 'entrypoint',
                                       'missing_policy', 'unconfined', 'altered_policy', 'no_new_privileges_false'])
def test_sidecar_attestation_refuses_authority_or_resource_drift(tmp_path, monkeypatch, mutation):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    state = state_for(desktop)
    record = display_record(desktop, state)
    if mutation in {'owner', 'worker', 'deployment'}:
        record['Config']['Labels']['xperfect.desktop.' + mutation] = 'foreign'
    elif mutation == 'creator': record['Config']['Labels']['xperfect.desktop.controller'] = '0' * 64
    elif mutation == 'network': record['HostConfig']['NetworkMode'] = 'host'
    elif mutation == 'mount': record['HostConfig']['Mounts'][0]['Source'] = 'foreign-volume'
    elif mutation == 'whole_home': record['HostConfig']['Mounts'][0]['VolumeOptions']['Subpath'] = desktop._display_subpath().rsplit('/', 1)[0]
    elif mutation == 'user': record['Config']['User'] = '0:0'
    elif mutation == 'privileged': record['HostConfig']['Privileged'] = True
    elif mutation == 'memory': record['HostConfig']['Memory'] += 1
    elif mutation == 'pids': record['HostConfig']['PidsLimit'] += 1
    elif mutation == 'ipc': record['HostConfig']['IpcMode'] = 'host'
    elif mutation == 'entrypoint': record['Config']['Entrypoint'] = ['/bin/sh']
    elif mutation == 'missing_policy': record['HostConfig']['SecurityOpt'] = ['no-new-privileges']
    elif mutation == 'unconfined': record['HostConfig']['SecurityOpt'][1] = 'seccomp=unconfined'
    elif mutation == 'altered_policy': record['HostConfig']['SecurityOpt'][1] = 'seccomp=' + json.dumps({'defaultAction': 'SCMP_ACT_ALLOW'})
    elif mutation == 'no_new_privileges_false': record['HostConfig']['SecurityOpt'][0] = 'no-new-privileges:false'
    monkeypatch.setattr(desktop.box, 'docker', lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps([record]), ''))
    with pytest.raises(WorkspaceBoxUnavailable, match='isolation'):
        desktop._record(state)


def test_controller_restart_preserves_same_attested_display(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.controller = '9' * 64
    state = state_for(desktop)
    record = display_record(desktop, state)
    monkeypatch.setattr(desktop.box, 'docker', lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps([record]), ''))
    assert desktop._record(state)['Id'] == state['container_id']
    assert state['controller_id'] != desktop.controller


def test_pinned_display_directory_rejects_parent_symlink(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    desktop.home.parent.parent.mkdir(parents=True)
    desktop.home.parent.symlink_to(foreign)
    with pytest.raises(WorkspaceBoxUnavailable, match='parent directory'):
        with desktop._display_directory(): pass
    assert list(foreign.iterdir()) == []


def test_pinned_display_directory_refuses_unregistered_existing_folder(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.home.mkdir(parents=True)
    desktop.state_dir.mkdir()
    (desktop.home / 'auth.json').write_text('synthetic unrelated content')
    with pytest.raises(WorkspaceBoxUnavailable, match='without its identity'):
        with desktop._display_directory(create=True): pass
    assert (desktop.home / 'auth.json').read_text() == 'synthetic unrelated content'


def test_pinned_descriptor_does_not_follow_concurrent_parent_substitution(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.home.mkdir(parents=True)
    desktop.home.chmod(0o700)
    info = desktop.home.stat()
    private_json(desktop.state_dir / 'directory.json', {'device': info.st_dev, 'inode': info.st_ino})
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'kept.txt').write_text('retain')
    with desktop._display_directory() as fd:
        desktop.home.rename(desktop.home.with_name('original-display'))
        desktop.home.symlink_to(foreign)
        handle = os.open('pinned.txt', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
        os.write(handle, b'pinned'); os.close(handle)
    assert not (foreign / 'pinned.txt').exists()
    assert (foreign / 'kept.txt').read_text() == 'retain'
    with pytest.raises(WorkspaceBoxUnavailable, match='authorized'):
        with desktop._display_directory(): pass


def test_display_start_requires_capacity_before_creating_container(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.controller = 'd' * 64
    monkeypatch.setenv('GLASSHIVE_PEER_RUNTIME_BASE_URL', 'http://runtime:8766')
    monkeypatch.setattr(desktop.box, 'locked', __import__('contextlib').nullcontext)
    monkeypatch.setattr(desktop, '_reuse', lambda _: None)
    monkeypatch.setattr(desktop, '_start_locked', lambda _: pytest.fail('capacity must precede creation'))
    with pytest.raises(WorkspaceBoxUnavailable, match='capacity authority'):
        desktop.start('synthetic')


@pytest.mark.parametrize('failure', ['mount_substitution', 'state_receipt'])
def test_partial_display_start_cleans_its_exact_container_before_graphical_execution(tmp_path, monkeypatch, failure):
    from contextlib import contextmanager
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.controller = 'd' * 64
    desktop.state_dir.mkdir()
    folder = tmp_path / 'pinned'
    folder.mkdir()
    @contextmanager
    def pinned(**kwargs):
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try: yield fd
        finally: os.close(fd)
    monkeypatch.setattr(desktop, '_display_directory', pinned)
    monkeypatch.setattr(desktop, '_prepare_home', lambda *args: None)
    monkeypatch.setattr(desktop.box, '_inspect', lambda: {'Id': 'c' * 64, 'Image': 'sha256:' + 'a' * 64, 'State': {'Running': True}})
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        if args[0] == 'run': return subprocess.CompletedProcess(args, 0, 'b' * 64, '')
        if args[:2] == ['exec', '-d']: pytest.fail('must not launch graphics in a substituted directory')
        return subprocess.CompletedProcess(args, 0, '[999, 999]', '')
    monkeypatch.setattr(desktop.box, 'docker', docker)
    stopped = []
    monkeypatch.setattr(desktop, '_stop_locked', lambda state: stopped.append(state['container_id']))
    if failure == 'state_receipt':
        def write_failed(_): raise OSError('synthetic receipt failure')
        monkeypatch.setattr(desktop, '_write_state', write_failed)
    expected_error = OSError if failure == 'state_receipt' else WorkspaceBoxUnavailable
    with pytest.raises(expected_error, match='receipt failure' if failure == 'state_receipt' else 'during its mount'):
        desktop._start_locked('synthetic')
    assert stopped == ['b' * 64]
    mount = calls[0][calls[0].index('--mount') + 1]
    assert 'home/.xperfect-desktop,' in mount
    assert 'dst=/workspace/desktop,' in mount
    assert calls[0][-2:] == ['sha256:' + 'a' * 64, 'infinity']
    assert calls[0][calls[0].index('--memory') + 1] == str(512 * 1024**2)
    assert calls[0][calls[0].index('--memory-swap') + 1] == str(512 * 1024**2)
    assert calls[0][calls[0].index('--pids-limit') + 1] == '256'


def test_new_display_creation_holds_capacity_box_and_display_locks(tmp_path, monkeypatch):
    from contextlib import contextmanager
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.controller = 'd' * 64
    monkeypatch.setenv('GLASSHIVE_PEER_RUNTIME_BASE_URL', 'http://runtime:8766')
    held = set()
    @contextmanager
    def lock(name):
        held.add(name)
        try: yield
        finally: held.remove(name)
    monkeypatch.setattr(desktop.box, 'locked', lambda: lock('box'))
    monkeypatch.setattr(desktop, '_locked', lambda: lock('display'))
    desktop.box.desktop_capacity = lambda: lock('capacity')
    monkeypatch.setattr(desktop, '_reuse', lambda _: None)
    def create(_):
        assert held == {'capacity', 'box', 'display'}
        return 'synthetic-view'
    monkeypatch.setattr(desktop, '_start_locked', create)
    assert desktop.start('synthetic') == 'synthetic-view'
    assert held == set()


@pytest.mark.parametrize('operation', ['_start_locked', 'launch_application'])
def test_new_desktop_native_use_requires_fresh_proof(tmp_path, monkeypatch, operation):
    from workers_projects_runtime import worker_egress
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.box.network = 'fixture-workers'
    policy = {'denied': [{'host': '169.254.169.254', 'probe_port': 80}],
              'positive': [{'host': 'example.test', 'port': 443}]}
    monkeypatch.setenv(worker_egress.POLICY_ENV, json.dumps(policy))
    monkeypatch.setenv('XPERFECT_CONTROL_ROOT', str(tmp_path))
    receipt = {'version': 1, 'enforcement': 'iptables_readback', 'network_id': 'a' * 64,
               'rules_sha256': 'b' * 64, 'policy_sha256': worker_egress.policy_digest(policy), 'observed_at': 0}
    monkeypatch.setattr(worker_egress, '_read_receipt', lambda path: receipt)
    monkeypatch.setattr(worker_egress.time, 'time', lambda: 500)
    monkeypatch.setattr(desktop.box, 'docker', lambda args: subprocess.CompletedProcess(args, 0, json.dumps([{
        'Id': 'a' * 64, 'Driver': 'bridge', 'EnableIPv6': False,
        'Options': {'com.docker.network.bridge.enable_icc': 'false'},
        'IPAM': {'Config': [{'Subnet': '10.44.0.0/24'}]}}]), ''))
    class Admitted(Exception):
        pass
    def inspect():
        raise Admitted
    monkeypatch.setattr(desktop.box, '_inspect', inspect)
    monkeypatch.setattr(desktop, '_read_state', lambda: None)
    action = getattr(desktop, operation)
    value = 'synthetic' if operation == '_start_locked' else ['/usr/bin/true']
    with pytest.raises(worker_egress.WorkerEgressUnavailable):
        action(value)
    receipt['observed_at'] = 500
    with pytest.raises(Admitted):
        action(value)


def test_reusing_live_display_does_not_reserve_capacity_again(tmp_path, monkeypatch):
    from contextlib import nullcontext
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    monkeypatch.setattr(desktop.box, 'require_egress_proof', lambda: pytest.fail('Existing display reuse needs no new native admission'))
    desktop.controller = 'd' * 64
    monkeypatch.setenv('GLASSHIVE_PEER_RUNTIME_BASE_URL', 'http://runtime:8766')
    monkeypatch.setattr(desktop.box, 'locked', nullcontext)
    monkeypatch.setattr(desktop, '_locked', nullcontext)
    desktop.box.desktop_capacity = lambda: pytest.fail('no new display reservation')
    monkeypatch.setattr(desktop, '_reuse', lambda _: 'existing-view')
    assert desktop.start('synthetic') == 'existing-view'


def test_paused_owner_browser_never_calls_native_resume(tmp_path, monkeypatch):
    box = member(tmp_path, monkeypatch)
    sandbox = WorkspaceMemberSandbox(box)
    monkeypatch.setattr(WorkspaceMemberDesktop, 'supported', property(lambda self: True))
    monkeypatch.setattr(sandbox, 'inspect', lambda _: SimpleNamespace(state='paused'))
    monkeypatch.setattr(sandbox, 'ensure_ready', lambda *a, **k: pytest.fail('AI must stay paused'))
    monkeypatch.setattr(sandbox, '_desktop_credentials', lambda *a, **k: {'vnc_password': 'synthetic'})
    monkeypatch.setattr(sandbox.desktop, 'start', lambda _: 'synthetic-view')
    launched = []
    monkeypatch.setattr(sandbox.desktop, 'launch_application', lambda args: launched.append(args))
    result = sandbox.desktop_action('wrk_test', 'codex-cli', 'browser', worker={'worker_id': 'wrk_test', 'state': 'paused'})
    assert result['status'] == 'launched'
    assert len(launched) == 1
    assert '--no-sandbox' not in str(launched[0])
    assert '--remote-debugging' not in str(launched[0])
    assert '/workspace/desktop' in str(launched[0])


def test_missing_or_modified_browser_policy_is_refused(tmp_path, monkeypatch):
    import workers_projects_runtime.workspace_desktop as module
    monkeypatch.setattr(module, '__file__', str(tmp_path / 'desktop.py'))
    with pytest.raises(WorkspaceBoxUnavailable, match='sandbox policy'):
        module.desktop_sandbox_policy()
    policy = tmp_path / 'desktop_chromium_seccomp.json'
    policy.write_text('{"defaultAction":"SCMP_ACT_ALLOW"}')
    with pytest.raises(WorkspaceBoxUnavailable, match='sandbox policy'):
        module.desktop_sandbox_policy()


def test_paused_native_terminal_reports_status_without_resolving_or_starting_runtime(monkeypatch):
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
    runtime = ProfiledWorkerRuntime.__new__(ProfiledWorkerRuntime)
    monkeypatch.setattr(runtime, '_runtime_for_worker', lambda _: pytest.fail('must not start or resume native compute'))
    target = runtime.terminal_target({'worker_id': 'wrk_test', 'state': 'paused'})
    result = subprocess.run(target.command, env=target.env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'AI work is paused. Use Resume to continue this terminal.'
    assert target.session_bound is True
    assert target.close_reason == 'Workspace paused'


def test_private_display_write_refuses_concurrent_file_replacement_before_truncation(tmp_path, monkeypatch):
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.uid = os.geteuid()
    desktop.box.member_gid = os.getegid()
    folder = tmp_path / 'display'
    folder.mkdir()
    authority = folder / 'Xauthority'
    authority.write_bytes(b'original display authority')
    authority.chmod(0o600)
    kept = tmp_path / 'unrelated-private-file'
    kept.write_bytes(b'retain exact unrelated bytes')
    kept.chmod(0o600)
    real_open = os.open
    def replace_then_open(path, flags, *args, **kwargs):
        if path == 'Xauthority':
            authority.unlink()
            authority.hardlink_to(kept)
        return real_open(path, flags, *args, **kwargs)
    directory = real_open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(os, 'open', replace_then_open)
        with pytest.raises(WorkspaceBoxUnavailable, match='before its write'):
            desktop._private_member_file(directory, 'Xauthority', b'new authority')
    finally:
        os.close(directory)
    assert kept.read_bytes() == b'retain exact unrelated bytes'


def test_desktop_storage_failure_has_truthful_recovery(tmp_path, monkeypatch):
    import errno
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    def full(_): raise OSError(errno.ENOSPC, 'synthetic storage failure')
    monkeypatch.setattr(desktop, '_start_controlled', full)
    with pytest.raises(WorkspaceBoxUnavailable, match='Workspace storage is full'):
        desktop.start('synthetic')


def browser_profile_helpers():
    from workers_projects_runtime.workspace_desktop import _CHROMIUM_PROFILE_HELPERS
    namespace = {}
    exec(_CHROMIUM_PROFILE_HELPERS, namespace)
    def scoped(function):
        def invoke(root, *args):
            info = os.stat(root, follow_symlinks=False)
            return function(root, *args, service_uid=os.getuid(), root_identity=(info.st_dev, info.st_ino))
        return invoke
    return {name: scoped(namespace[name]) for name in ('profile_lock_target', 'retire_profile_lock')}


def native_browser_profile(tmp_path):
    root = tmp_path / 'display'
    profile = root / '.config' / 'chromium'
    profile.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    profile.parent.chmod(0o700)
    (profile / 'Preferences').write_bytes(b'{"user_setting":"keep this"}')
    (profile / 'History').write_bytes(b'keep browser history')
    for name, target in [('SingletonSocket', '/tmp/chromium-fixture/socket'),
                         ('SingletonCookie', 'fixture-cookie'),
                         ('SingletonLock', 'a' * 12 + '-73')]:
        (profile / name).symlink_to(target)
    return root, profile


def test_native_browser_recovery_removes_only_validated_ephemeral_links(tmp_path, monkeypatch):
    root, profile = native_browser_profile(tmp_path)
    helpers = browser_profile_helpers()
    before = {name: (profile / name).read_bytes() for name in ('Preferences', 'History')}
    removed = []
    real_unlink = os.unlink
    def unlink(path, *args, **kwargs):
        removed.append(path)
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', unlink)
    assert helpers['profile_lock_target'](root) == 'a' * 12 + '-73'
    assert helpers['retire_profile_lock'](root, 'a' * 12 + '-73') is True
    assert removed == ['SingletonSocket', 'SingletonCookie', 'SingletonLock']
    assert {name: (profile / name).read_bytes() for name in before} == before
    assert helpers['profile_lock_target'](root) is None


@pytest.mark.parametrize('entry', ['SingletonSocket', 'SingletonCookie', 'SingletonLock'])
def test_browser_recovery_validates_every_link_before_deleting_any(tmp_path, entry):
    root, profile = native_browser_profile(tmp_path)
    (profile / entry).unlink()
    (profile / entry).write_bytes(b'not a native singleton symlink')
    helpers = browser_profile_helpers()
    with pytest.raises(ValueError, match='ownership'):
        helpers['retire_profile_lock'](root, 'a' * 12 + '-73')
    assert all(os.path.lexists(profile / name) for name in ('SingletonSocket', 'SingletonCookie', 'SingletonLock'))
    assert (profile / entry).read_bytes() == b'not a native singleton symlink'


def test_browser_recovery_changed_lock_keeps_every_entry(tmp_path):
    root, profile = native_browser_profile(tmp_path)
    assert browser_profile_helpers()['retire_profile_lock'](root, 'b' * 12 + '-73') is False
    assert all(os.path.lexists(profile / name) for name in ('SingletonSocket', 'SingletonCookie', 'SingletonLock'))


@pytest.mark.parametrize('mutation', ['symlink_parent', 'writable_parent', 'foreign_owner', 'hardlink'])
def test_browser_profile_recovery_refuses_unsafe_filesystem_scope(tmp_path, monkeypatch, mutation):
    root, profile = native_browser_profile(tmp_path)
    helpers = browser_profile_helpers()
    if mutation == 'symlink_parent':
        config = root / '.config'
        real = root / 'kept-config'
        config.rename(real)
        config.symlink_to(real, target_is_directory=True)
    elif mutation == 'writable_parent':
        profile.parent.chmod(0o777)
    elif mutation == 'foreign_owner':
        monkeypatch.setattr(os, 'getuid', lambda: os.geteuid() + 1)
    else:
        os.link(profile / 'SingletonCookie', profile / 'second-link', follow_symlinks=False)
    with pytest.raises((ValueError, OSError)):
        helpers['retire_profile_lock'](root, 'a' * 12 + '-73')
    assert (profile / 'Preferences').read_bytes() == b'{"user_setting":"keep this"}'
    assert os.path.lexists(profile / 'SingletonLock')


def test_browser_profile_recovery_detects_concurrent_replacement_before_first_unlink(tmp_path, monkeypatch):
    root, profile = native_browser_profile(tmp_path)
    helpers = browser_profile_helpers()
    real_stat = os.stat
    reads = [0]
    def replace_before_validation(path, *args, **kwargs):
        if path == 'SingletonCookie':
            reads[0] += 1
            if reads[0] == 2:
                (profile / 'SingletonCookie').unlink()
                (profile / 'SingletonCookie').write_bytes(b'replaced')
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', replace_before_validation)
    with pytest.raises(ValueError, match='changed during recovery'):
        helpers['retire_profile_lock'](root, 'a' * 12 + '-73')
    assert os.path.lexists(profile / 'SingletonSocket')
    assert os.path.lexists(profile / 'SingletonLock')
    assert (profile / 'SingletonCookie').read_bytes() == b'replaced'


def launch_fixture(tmp_path, monkeypatch, *, lock=None, previous_present=False, listing='', listing_error=False):
    root, profile = native_browser_profile(tmp_path)
    desktop = WorkspaceMemberDesktop(member(tmp_path, monkeypatch))
    desktop.display_mount = str(root)
    state = state_for(desktop)
    info = root.stat()
    state.update(directory_device=info.st_dev, directory_inode=info.st_ino)
    monkeypatch.setattr(desktop, '_read_state', lambda: state)
    monkeypatch.setattr(desktop, '_record', lambda _: display_record(desktop, state))
    monkeypatch.setattr(desktop.box, '_inspect', lambda: {'Id': state['box_id']})
    calls, programs = [], []
    def docker(args, **kwargs):
        calls.append(args)
        if args[:1] == ['inspect']:
            return subprocess.CompletedProcess(args, 0 if previous_present else 1, '', '')
        if args[:2] == ['container', 'ls']:
            if listing_error:
                raise WorkspaceBoxUnavailable('Docker inventory unavailable')
            return subprocess.CompletedProcess(args, 0, listing, '')
        assert args[0] == 'exec'
        if args[-1] == 'xdpyinfo':
            return subprocess.CompletedProcess(args, 0, '', '')
        program = args[-1]
        assert args[args.index('--user') + 1] == f'{desktop.uid}:{desktop.box.member_gid}'
        programs.append(program)
        if "subprocess.run(['xdpyinfo']" in program:
            return subprocess.CompletedProcess(args, 0, json.dumps({'hostname': 'xperfect-stable', 'lock': lock}), '')
        return subprocess.run(['/usr/bin/env', 'python3', '-c', program], capture_output=True, text=True, check=True)
    monkeypatch.setattr(desktop.box, 'docker', docker)
    return desktop, root, profile, calls, programs


def test_confirmed_absent_old_docker_generation_recovers_profile_without_rescanning_user_url(tmp_path, monkeypatch):
    desktop, root, profile, calls, programs = launch_fixture(tmp_path, monkeypatch, lock='a' * 12 + '-73')
    url = 'https://example.invalid/LEGACY_LOCK/COMMAND/ROOT'
    desktop.launch_application(['/usr/bin/true', url])
    assert not os.path.lexists(profile / 'SingletonLock')
    assert (profile / 'Preferences').read_bytes() == b'{"user_setting":"keep this"}'
    assert calls[1] == ['inspect', 'a' * 12]
    assert calls[2] == ['container', 'ls', '-a', '--no-trunc', '--format', '{{.ID}}']
    assert repr(['/usr/bin/true', url]) in programs[-1]


@pytest.mark.parametrize(('present', 'listing', 'error'), [
    (True, '', False),
    (False, 'a' * 64, False),
    (False, 'not-a-container-id', False),
    (False, '', True),
])
def test_uncertain_previous_browser_generation_cannot_remove_profile_lock(tmp_path, monkeypatch, present, listing, error):
    desktop, root, profile, calls, programs = launch_fixture(
        tmp_path, monkeypatch, lock='a' * 12 + '-73', previous_present=present, listing=listing, listing_error=error)
    with pytest.raises(WorkspaceBoxUnavailable):
        desktop.launch_application(['/usr/bin/true'])
    assert len(programs) == 1
    assert os.readlink(profile / 'SingletonLock') == 'a' * 12 + '-73'
    assert (profile / 'History').read_bytes() == b'keep browser history'


@pytest.mark.parametrize('lock', ['xperfect-stable-73', 'another-computer-73', None])
def test_current_or_unrecognized_hostname_keeps_native_chromium_lock_handling(tmp_path, monkeypatch, lock):
    desktop, root, profile, calls, programs = launch_fixture(tmp_path, monkeypatch, lock=lock)
    desktop.launch_application(['/usr/bin/true'])
    assert len(calls) == 2
    assert os.readlink(profile / 'SingletonLock') == 'a' * 12 + '-73'
    assert (profile / 'Preferences').read_bytes() == b'{"user_setting":"keep this"}'


def test_absent_browser_profile_is_not_created_by_recovery(tmp_path):
    root = tmp_path / 'display'
    root.mkdir(mode=0o700)
    helpers = browser_profile_helpers()
    assert helpers['profile_lock_target'](root) is None
    assert helpers['retire_profile_lock'](root, 'a' * 12 + '-73') is False
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('foreign_grant', [False, True])
def test_service_owned_profile_uses_existing_private_member_acl_without_exposing_group_access(tmp_path, monkeypatch, foreign_grant):
    root, profile = native_browser_profile(tmp_path)
    root.chmod(0o770)
    profile.parent.chmod(0o770)
    own_acl = 'user::rwx\nuser:' + str(os.getuid()) + ':rwx\ngroup::---\nmask::rwx\nother::---\n'
    if foreign_grant:
        own_acl += 'user:60000:rwx\n'
    def read_acl(args, **kwargs):
        assert args[:2] == ['getfacl', '-cEpn']
        assert kwargs['pass_fds']
        return subprocess.CompletedProcess(args, 0, own_acl, '')
    monkeypatch.setattr(subprocess, 'run', read_acl)
    helpers = browser_profile_helpers()
    if foreign_grant:
        with pytest.raises(ValueError, match='ACL changed'):
            helpers['retire_profile_lock'](root, 'a' * 12 + '-73')
        assert os.path.lexists(profile / 'SingletonLock')
    else:
        assert helpers['retire_profile_lock'](root, 'a' * 12 + '-73') is True
        assert (profile / 'History').read_bytes() == b'keep browser history'


def test_browser_profile_cannot_substitute_a_different_bound_display_inode(tmp_path):
    from workers_projects_runtime.workspace_desktop import _CHROMIUM_PROFILE_HELPERS
    root, profile = native_browser_profile(tmp_path)
    namespace = {}
    exec(_CHROMIUM_PROFILE_HELPERS, namespace)
    info = root.stat()
    with pytest.raises(ValueError, match='ownership'):
        namespace['retire_profile_lock'](root, 'a' * 12 + '-73', service_uid=os.getuid(),
                                          root_identity=(info.st_dev, info.st_ino + 1))
    assert os.path.lexists(profile / 'SingletonLock')


def test_missing_native_acl_inspector_refuses_recovery_instead_of_treating_profile_as_absent(tmp_path, monkeypatch):
    root, profile = native_browser_profile(tmp_path)
    root.chmod(0o770)
    def unavailable(*args, **kwargs):
        raise FileNotFoundError(2, 'missing native inspector', 'getfacl')
    monkeypatch.setattr(subprocess, 'run', unavailable)
    with pytest.raises(FileNotFoundError):
        browser_profile_helpers()['retire_profile_lock'](root, 'a' * 12 + '-73')
    assert os.path.lexists(profile / 'SingletonLock')


@pytest.mark.parametrize('directory', ['root', 'config', 'chromium'])
def test_browser_profile_foreign_read_execute_acl_is_refused_on_every_directory(tmp_path, monkeypatch, directory):
    root, profile = native_browser_profile(tmp_path)
    selected = {'root': root, 'config': profile.parent, 'chromium': profile}[directory]
    selected.chmod(0o750)
    acl = ('user::rwx\nuser:' + str(os.getuid())
           + ':rwx\nuser:60000:r-x\ngroup::---\nmask::r-x\nother::---\n')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(args, 0, acl, ''))
    with pytest.raises(ValueError, match='ACL changed'):
        browser_profile_helpers()['retire_profile_lock'](root, 'a' * 12 + '-73')
    assert all(os.path.lexists(profile / name) for name in ('SingletonSocket', 'SingletonCookie', 'SingletonLock'))
    assert (profile / 'Preferences').read_bytes() == b'{"user_setting":"keep this"}'
