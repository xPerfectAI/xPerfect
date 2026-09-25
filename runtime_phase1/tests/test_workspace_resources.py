from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock
import json
import subprocess
import sys
import time

import pytest

from workers_projects_runtime import workspace_resources
from workers_projects_runtime.workspace_resources import WorkspaceResources, pending_memory, process_counts
from workers_projects_runtime.workspace_box import WorkspaceBoxUnavailable
from workers_projects_runtime.workspace_runtime import SharedWorkspaceRuntimes


def _give_current_substrate_proof(resource, monkeypatch):
    monkeypatch.setenv('XPERFECT_CONTROLLER_ID', 'c' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_NETWORK', 'workers')
    resource._substrate_proof = (time.monotonic(), {
        'image': '', 'volume_name': '', 'volume_root': '', 'control_root': '',
        'controller': 'c' * 64, 'network': 'workers',
    })


def test_process_inventory_counts_threads_by_admitted_uid():
    assert process_counts('UID PID LWP\n65534 10 10\n20001 20 20\n20001 20 21\n20002 30 30\n') == {
        65534: (1, 1), 20001: (1, 2), 20002: (1, 1)}
    with pytest.raises(WorkspaceBoxUnavailable):
        process_counts('UID PID LWP\n20001 unknown 20\n')
    with pytest.raises(WorkspaceBoxUnavailable):
        process_counts('UID PID LWP\n')


def test_shared_memory_charges_one_box_and_bounds_all_member_promises():
    workers = {'a': {'workspace_id': 'one'}, 'b': {'workspace_id': 'one'},
               'c': {'workspace_id': 'two'}}
    usage = {'workspace_memory_bytes': 1000, 'accounted_workspace_ids': []}
    assert pending_memory(usage, {'a', 'b'}, {'a': 400, 'b': 400}, None, workers.get) == (1000, False)
    assert pending_memory(usage, {'a', 'b'}, {'a': 600, 'b': 600}, None, workers.get) == (1000, True)
    assert pending_memory(usage, {'a', 'b', 'c'}, {'a': 400, 'b': 400, 'c': 400}, None, workers.get) == (2000, False)
    usage['accounted_workspace_ids'] = ['one']
    assert pending_memory(usage, {'b'}, {'a': 400, 'b': 400}, None, workers.get) == (0, False)
    with pytest.raises(ValueError, match='persisted workspace'):
        pending_memory(usage, {'unknown'}, {'unknown': 400}, None, workers.get)


def test_missing_inventory_or_stale_cache_is_unavailable(monkeypatch):
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    assert resource.usage(None, cached_only=True)['process_probe_ok'] is False
    assert resource.usage(None)['memory_probe_ok'] is False
    monkeypatch.setattr(resource, '_measure', lambda runtime: {'process_probe_ok': True})
    monkeypatch.setenv('XPERFECT_CONTROLLER_ID', 'c' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_NETWORK', 'workers')
    resource._substrate_proof = (time.monotonic(), {
        'image': '', 'volume_name': '', 'volume_root': '', 'control_root': '',
        'controller': 'c' * 64, 'network': 'workers',
    })
    resource.cached = None
    assert resource.usage(None)['process_probe_ok'] is True
    assert resource.usage(None, cached_only=True)['process_probe_ok'] is True


def test_resource_read_preserves_controlled_acl_failure_without_reprobing(monkeypatch):
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    def denied(_runtime):
        raise WorkspaceBoxUnavailable('Shared filesystem ACL tools are unavailable')
    monkeypatch.setattr(resource, '_refresh_substrate_proof', denied)
    assert resource.refresh(None)['probe_error_code'] == 'filesystem_acl_tools_unavailable'
    monkeypatch.setattr(resource, '_measure', lambda _runtime: (_ for _ in ()).throw(
        AssertionError('read-only usage must not replace the controlled failure')
    ))
    assert resource.usage(None)['probe_error_code'] == 'filesystem_acl_tools_unavailable'
    assert WorkspaceResources(SimpleNamespace(recovery_issues=[])).usage(None)[
        'probe_error_code'
    ] == 'shared_substrate_proof_unavailable'


def test_cached_capacity_fails_closed_without_waiting_for_live_probe():
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    resource._probe_lock.acquire()
    try:
        result = resource.usage(None, cached_only=True)
    finally:
        resource._probe_lock.release()
    assert result['process_probe_ok'] is False
    assert result['probe_error_code'] == 'resource_probe_in_progress'


def test_resource_only_runtime_keeps_controlled_readiness_loop(tmp_path, monkeypatch):
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.service import WorkersProjectsService
    from workers_projects_runtime.store import Store

    class ResourceOnlyRuntime(StubRuntime):
        def refresh_isolated_resource_usage(self):
            return {'process_probe_ok': True}

    loop_entered = Event()
    monkeypatch.setattr(
        WorkersProjectsService,
        '_isolated_readiness_loop',
        lambda _self: loop_entered.set(),
    )
    service = WorkersProjectsService(
        Store(str(tmp_path / 'resource-only.sqlite3')),
        ResourceOnlyRuntime(),
        reconcile_on_startup=False,
    )
    try:
        assert loop_entered.wait(1)
        assert service._isolated_readiness_thread is not None
    finally:
        service.shutdown()


def test_cached_capacity_expires_with_substrate_proof_or_identity(monkeypatch):
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    monkeypatch.setenv('XPERFECT_CONTROLLER_ID', 'c' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_NETWORK', 'workers')
    resource._substrate_proof = (time.monotonic() - 31.0, {
        'image': '', 'volume_name': '', 'volume_root': '', 'control_root': '',
        'controller': 'c' * 64, 'network': 'workers',
    })
    resource.cached = (time.monotonic(), {
        'process_probe_ok': True,
        'memory_probe_ok': True,
        'disk_probe_ok': True,
    })
    assert resource.usage(None, cached_only=True)['process_probe_ok'] is False
    resource._substrate_proof = (time.monotonic(), {
        'image': '', 'volume_name': '', 'volume_root': '', 'control_root': '',
        'controller': 'd' * 64, 'network': 'workers',
    })
    assert resource.usage(None, cached_only=True)['process_probe_ok'] is False


def test_empty_shared_probe_rejects_a_changed_native_image(monkeypatch, tmp_path):
    monkeypatch.setenv('XPERFECT_SHARED_IMAGE', 'sha256:' + 'a' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_NAME', 'shared-data')
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_ROOT', str(tmp_path / 'data'))
    monkeypatch.setenv('XPERFECT_CONTROL_ROOT', str(tmp_path / 'control'))
    (tmp_path / 'data').mkdir()
    (tmp_path / 'control').mkdir()
    calls = []

    def call(arguments):
        calls.append(arguments)
        if arguments[:2] == ['image', 'inspect']:
            return 'sha256:' + 'b' * 64
        raise AssertionError(arguments)

    with pytest.raises(WorkspaceBoxUnavailable, match='image identity'):
        WorkspaceResources._validate_shared_substrate(call, controller='c' * 64, network='workers')
    assert calls == [['image', 'inspect', '--format', '{{.Id}}', 'sha256:' + 'a' * 64]]


def test_empty_shared_probe_checks_mount_guard_and_acl_contract(monkeypatch, tmp_path):
    image = 'sha256:' + 'a' * 64
    data = tmp_path / 'data'
    control = tmp_path / 'control'
    data.mkdir()
    control.mkdir()
    monkeypatch.setenv('XPERFECT_SHARED_IMAGE', image)
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_NAME', 'shared-data')
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_ROOT', str(data))
    monkeypatch.setenv('XPERFECT_CONTROL_ROOT', str(control))
    monkeypatch.setattr(workspace_resources.shutil, 'which', lambda name: '/usr/bin/' + name)
    acl_calls = []
    guard_id = 'd' * 64
    removed = False

    def fake_run(arguments, **kwargs):
        acl_calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, '', '')

    monkeypatch.setattr(workspace_resources.subprocess, 'run', fake_run)
    controller = 'c' * 64
    calls = []

    def call(arguments):
        nonlocal removed
        calls.append(arguments)
        if arguments[:2] == ['image', 'inspect']:
            return image
        if arguments[:1] == ['inspect']:
            if len(arguments) == 2 and arguments[1].startswith('xperfect-readiness-'):
                if removed:
                    raise WorkspaceBoxUnavailable('probe was removed')
                return json.dumps([{
                    'Id': guard_id,
                    'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False},
                }])
            return json.dumps([{
                'Id': controller,
                'State': {'Running': True},
                'Mounts': [
                    {'Destination': str(data), 'Type': 'volume', 'Name': 'shared-data', 'RW': True},
                    {'Destination': str(control), 'Type': 'volume', 'Name': 'control-state', 'RW': True},
                ],
            }])
        if arguments[:1] == ['create']:
            return guard_id
        if arguments[:1] in (['start'], ['wait'], ['logs']):
            return {'start': '', 'wait': '0', 'logs': 'xperfect-native-guard-ready\n'}[arguments[0]]
        if arguments[:1] == ['rm']:
            removed = True
            return guard_id
        if arguments[:1] == ['run']:
            return 'xperfect-native-guard-ready\n'
        raise AssertionError(arguments)

    WorkspaceResources._validate_shared_substrate(call, controller=controller, network='workers')
    assert any(arguments[0] == 'getfacl' for arguments in acl_calls)
    assert any(arguments[0] == 'setfacl' for arguments in acl_calls)
    guard = next(arguments for arguments in calls if arguments[:1] == ['create'])
    assert '--security-opt' in guard and 'no-new-privileges' in guard


def test_empty_shared_probe_keeps_a_reused_descriptor_open(monkeypatch, tmp_path):
    image = 'sha256:' + 'a' * 64
    data = tmp_path / 'data'
    control = tmp_path / 'control'
    data.mkdir()
    control.mkdir()
    monkeypatch.setenv('XPERFECT_SHARED_IMAGE', image)
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_NAME', 'shared-data')
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_ROOT', str(data))
    monkeypatch.setenv('XPERFECT_CONTROL_ROOT', str(control))
    monkeypatch.setattr(workspace_resources.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(
        workspace_resources.subprocess,
        'run',
        lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0, '', ''),
    )
    real_open = workspace_resources.os.open
    real_close = workspace_resources.os.close
    closed: list[int] = []
    probe_fd: list[int] = []
    reused_fd: list[int] = []

    def open_probe(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        if str(path).startswith(str(data / '.xperfect-readiness-')):
            probe_fd.append(fd)
        return fd

    def close(fd):
        closed.append(fd)
        real_close(fd)
        if probe_fd and fd == probe_fd[0] and not reused_fd:
            # The kernel may assign the just-released number to another
            # server thread. Force that real descriptor reuse deterministically.
            unrelated = real_open('/dev/null', workspace_resources.os.O_RDONLY)
            if unrelated != fd:
                workspace_resources.os.dup2(unrelated, fd)
                real_close(unrelated)
            reused_fd.append(fd)

    monkeypatch.setattr(workspace_resources.os, 'open', open_probe)
    monkeypatch.setattr(workspace_resources.os, 'close', close)
    controller = 'c' * 64
    guard_id = 'd' * 64
    removed = False

    def call(arguments):
        nonlocal removed
        if arguments[:2] == ['image', 'inspect']:
            return image
        if arguments[:1] == ['inspect']:
            if len(arguments) == 2 and arguments[1].startswith('xperfect-readiness-'):
                if removed:
                    raise WorkspaceBoxUnavailable('probe was removed')
                return json.dumps([{
                    'Id': guard_id,
                    'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False},
                }])
            return json.dumps([{
                'Id': controller,
                'State': {'Running': True},
                'Mounts': [
                    {'Destination': str(data), 'Type': 'volume', 'Name': 'shared-data', 'RW': True},
                    {'Destination': str(control), 'Type': 'volume', 'Name': 'control-state', 'RW': True},
                ],
            }])
        if arguments[:1] == ['create']:
            return guard_id
        if arguments[:1] == ['start']:
            return ''
        if arguments[:1] == ['wait']:
            return '0'
        if arguments[:1] == ['logs']:
            return 'xperfect-native-guard-ready\n'
        if arguments[:1] == ['rm']:
            removed = True
            return guard_id
        raise AssertionError(arguments)

    try:
        WorkspaceResources._validate_shared_substrate(
            call, controller=controller, network='workers'
        )
        assert probe_fd and reused_fd == probe_fd
        assert closed.count(probe_fd[0]) == 1
        assert workspace_resources.os.read(reused_fd[0], 1) == b''
    finally:
        if reused_fd:
            real_close(reused_fd[0])


def test_native_guard_probe_removes_the_attested_container_id():
    container_id = 'd' * 64
    inspect_calls = 0
    strict_calls = []
    raw_calls = []

    def call(arguments):
        nonlocal inspect_calls
        strict_calls.append(arguments)
        if arguments[:1] == ['create']:
            return container_id
        if arguments[:1] == ['inspect']:
            inspect_calls += 1
            return json.dumps([{
                'Id': container_id,
                'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False},
            }])
        if arguments[:1] == ['start']:
            return ''
        if arguments[:1] == ['wait']:
            return '0'
        if arguments[:1] == ['logs']:
            return 'xperfect-native-guard-ready\n'
        raise AssertionError(arguments)

    def raw_call(arguments):
        raw_calls.append(arguments)
        if arguments[:1] == ['rm']:
            return subprocess.CompletedProcess(arguments, 0, container_id + '\n', '')
        if arguments[:1] == ['inspect']:
            return subprocess.CompletedProcess(arguments, 1, '', 'Error: No such object')
        raise AssertionError(arguments)

    output = WorkspaceResources._native_guard_probe(
        call, 'sha256:' + 'a' * 64, 'print(1)', raw_call=raw_call
    )
    assert output.strip() == 'xperfect-native-guard-ready'
    removal = next(arguments for arguments in raw_calls if arguments[:1] == ['rm'])
    assert removal[-1] == container_id
    assert not any('--rm' in arguments for arguments in strict_calls)


def test_native_guard_probe_recovers_create_timeout_for_cleanup():
    container_id = 'd' * 64
    raw_calls = []

    def call(arguments):
        if arguments[:1] == ['create']:
            raise subprocess.TimeoutExpired(arguments, 10)
        raise AssertionError(arguments)

    def raw_call(arguments):
        raw_calls.append(arguments)
        if arguments[:1] == ['ps']:
            return subprocess.CompletedProcess(arguments, 0, container_id + '\n', '')
        if arguments[:1] == ['rm']:
            return subprocess.CompletedProcess(arguments, 0, container_id + '\n', '')
        if arguments[:1] == ['inspect']:
            return subprocess.CompletedProcess(arguments, 1, '', 'Error: No such object')
        raise AssertionError(arguments)

    with pytest.raises(subprocess.TimeoutExpired):
        WorkspaceResources._native_guard_probe(
            call, 'sha256:' + 'a' * 64, 'print(1)', raw_call=raw_call
        )
    lookup = next(arguments for arguments in raw_calls if arguments[:1] == ['ps'])
    assert '--no-trunc' in lookup
    assert next(arguments for arguments in raw_calls if arguments[:1] == ['rm'])[-1] == container_id


def test_native_guard_probe_fails_closed_when_create_identity_cannot_be_reconciled():
    raw_calls = []

    def call(arguments):
        if arguments[:1] == ['create']:
            raise subprocess.TimeoutExpired(arguments, 10)
        raise AssertionError(arguments)

    def raw_call(arguments):
        raw_calls.append(arguments)
        if arguments[:1] == ['ps']:
            return subprocess.CompletedProcess(arguments, 0, '', '')
        raise AssertionError(arguments)

    with pytest.raises(WorkspaceBoxUnavailable, match='cleanup'):
        WorkspaceResources._native_guard_probe(
            call, 'sha256:' + 'a' * 64, 'print(1)', raw_call=raw_call
        )
    assert raw_calls and raw_calls[0][:1] == ['ps'] and '--no-trunc' in raw_calls[0]


def test_capacity_reads_consume_preflight_proof_without_running_active_checks(monkeypatch):
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    active_calls: list[str] = []
    resource._refresh_substrate_proof = lambda _runtime: active_calls.append('active')
    resource._measure = lambda _runtime: {
        'process_probe_ok': True,
        'memory_probe_ok': True,
        'disk_probe_ok': True,
    }

    resource.refresh(object())
    assert active_calls == ['active']
    resource.cached = (time.monotonic() - 3.0, resource.cached[1])
    resource._refresh_substrate_proof = lambda _runtime: active_calls.append('unexpected')
    resource.usage(object())
    assert active_calls == ['active']


def test_controlled_refresh_and_concurrent_roster_reads_keep_active_checks_out_of_get(
    monkeypatch, tmp_path
):
    image = 'sha256:' + 'a' * 64
    controller = 'c' * 64
    guard_id = 'd' * 64
    data = tmp_path / 'data'
    control = tmp_path / 'control'
    data.mkdir()
    control.mkdir()
    for key, value in {
        'XPERFECT_SHARED_IMAGE': image,
        'XPERFECT_SHARED_VOLUME_NAME': 'shared-data',
        'XPERFECT_SHARED_VOLUME_ROOT': str(data),
        'XPERFECT_CONTROL_ROOT': str(control),
        'XPERFECT_SHARED_NETWORK': 'workers',
        'XPERFECT_CONTROLLER_ID': controller,
        'XPERFECT_SHARED_MEMORY_BYTES': str(1024**3),
        'XPERFECT_SHARED_PIDS_LIMIT': '64',
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(workspace_resources.shutil, 'which', lambda name: '/usr/bin/' + name)
    acl_calls = []

    def fake_acl(arguments, **_kwargs):
        acl_calls.append(arguments[0])
        return subprocess.CompletedProcess(arguments, 0, '', '')

    monkeypatch.setattr(workspace_resources.subprocess, 'run', fake_acl)
    calls = []
    call_lock = Lock()
    removed = [False]

    def docker(arguments, **_kwargs):
        assert _kwargs['timeout_sec'] == 10
        if arguments[0] in {'create', 'start', 'wait', 'logs', 'rm', 'ps'}:
            assert _kwargs['check'] is False
        with call_lock:
            calls.append(tuple(arguments))
        code, output, error = 0, '', ''
        if arguments[:2] == ['image', 'inspect']:
            output = image
        elif arguments[:1] == ['inspect'] and arguments[1] == controller:
            output = json.dumps([{
                'Id': controller, 'State': {'Running': True},
                'Mounts': [
                    {'Destination': str(data), 'Type': 'volume',
                     'Name': 'shared-data', 'RW': True},
                    {'Destination': str(control), 'Type': 'volume',
                     'Name': 'control-state', 'RW': True},
                ],
            }])
        elif arguments[:1] == ['inspect'] and arguments[1] == guard_id and removed[0]:
            code, error = 1, 'Error: No such object'
        elif arguments[:1] == ['inspect'] and arguments[1].startswith('xperfect-readiness-'):
            output = json.dumps([{
                'Id': guard_id,
                'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False},
            }])
        elif arguments[:2] == ['network', 'inspect']:
            output = json.dumps([{'Driver': 'bridge', 'Containers': {controller: {}}}])
        elif arguments[:1] == ['create']:
            output = guard_id
        elif arguments[:1] == ['wait']:
            output = '0'
        elif arguments[:1] == ['logs']:
            output = 'xperfect-native-guard-ready'
        elif arguments[:2] == ['rm', '-f']:
            removed[0] = True
            output = guard_id
        elif arguments[:1] == ['info']:
            output = json.dumps({'CgroupVersion': '2', 'MemTotal': 16 * 1024**3})
        elif arguments[:1] == ['stats']:
            output = ''
        elif arguments[:1] != ['start']:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, code, output, error)

    class ReadOnlyStore:
        @contextmanager
        def _connect(self):
            yield SimpleNamespace(execute=lambda _query: [])

    homes = SimpleNamespace(
        native_launcher=SimpleNamespace(inventory=lambda: []),
        account_home_path=lambda *_args: data,
    )
    shared = SharedWorkspaceRuntimes(
        ReadOnlyStore(), binder=SimpleNamespace(store=object(), homes=homes)
    )
    runtime = SimpleNamespace(codex=SimpleNamespace(sandbox=SimpleNamespace(_docker=docker)))
    workspace = {'mode': 'shared', 'execution_mode': 'docker'}

    assert shared.resources.refresh(runtime)['process_probe_ok'] is True
    assert len([call for call in calls if call[0] == 'create']) == 1
    assert acl_calls == ['getfacl', 'setfacl']
    calls.clear()
    shared.resources.cached = (
        time.monotonic() - 3.0, shared.resources.cached[1]
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _index: shared.readiness(workspace, runtime=runtime), range(32)
        ))
    assert all(item['code'] == 'ready' for item in results)
    assert all(call[0] in {'info', 'network', 'stats'} for call in calls)
    assert acl_calls == ['getfacl', 'setfacl']

    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app
    from workers_projects_runtime.openclaw_runtime import StubRuntime

    class RosterRuntime(StubRuntime):
        def shared_workspace_readiness(self, selected):
            return shared.readiness(selected, runtime=runtime)

    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'synthetic-owner')
    app = create_app(
        db_path=str(tmp_path / 'api.sqlite'), runtime=RosterRuntime(),
        reconcile_on_startup=False,
    )
    with TestClient(app) as client:
        project = client.post('/v1/projects', json={
            'owner_id': 'synthetic-owner', 'title': 'Read-only roster',
            'goal': 'Inspect readiness', 'default_worker_profile': 'codex-cli',
        })
        assert project.status_code == 201
        created = client.post(
            f"/v1/projects/{project.json()['project_id']}/execution-workspaces",
            json={},
        )
        assert created.status_code == 201
        url = f"/v1/workspaces/{created.json()['workspace_id']}/members"
        calls.clear()
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _index: client.get(url), range(16)))
        assert all(reply.status_code == 200 for reply in replies)
        assert all(reply.json()['runtime_readiness']['code'] == 'ready'
                   for reply in replies)
        assert all(call[0] in {'info', 'network', 'stats'} for call in calls)
        assert acl_calls == ['getfacl', 'setfacl']

    shared.resources._substrate_proof = (
        time.monotonic() - 31.0, shared.resources._substrate_proof[1]
    )
    calls.clear()
    expired = shared.readiness(workspace, runtime=runtime)
    assert expired['probe_error_code'] == 'shared_substrate_proof_unavailable'
    assert all(call[0] in {'info', 'network', 'stats'} for call in calls)
    assert acl_calls == ['getfacl', 'setfacl']

    removed[0] = False
    assert shared.resources.refresh(runtime)['process_probe_ok'] is True
    assert len([call for call in calls if call[0] == 'create']) == 1
    assert acl_calls == ['getfacl', 'setfacl', 'getfacl', 'setfacl']

    monkeypatch.setenv('XPERFECT_SHARED_IMAGE', 'sha256:' + 'b' * 64)
    assert shared.readiness(workspace, runtime=runtime)['probe_error_code'] == (
        'shared_substrate_proof_unavailable'
    )
    calls.clear()
    assert shared.resources.refresh(runtime)['probe_error_code'] == (
        'native_image_identity_changed'
    )
    assert not any(call[0] == 'create' for call in calls)


def test_packaged_runtime_separates_controlled_refresh_from_capacity_reads(monkeypatch):
    from workers_projects_runtime import execution_profile
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime

    monkeypatch.setattr(execution_profile, 'packaged_linux', lambda: True)
    calls = []
    resources = SimpleNamespace(
        refresh=lambda runtime: calls.append(('refresh', runtime)) or {'proof': 'ready'},
        usage=lambda runtime, *, cached_only=False: (
            calls.append(('usage', runtime, cached_only)) or {'capacity': 'ready'}
        ),
    )
    runtime = ProfiledWorkerRuntime.__new__(ProfiledWorkerRuntime)
    runtime._shared_workspace_runtimes = SimpleNamespace(resources=resources)

    assert runtime.refresh_isolated_resource_usage() == {'proof': 'ready'}
    assert runtime.isolated_resource_usage(cached_only=False) == {'capacity': 'ready'}
    assert calls == [('refresh', runtime), ('usage', runtime, False)]

    monkeypatch.setattr(execution_profile, 'packaged_linux', lambda: False)
    legacy_usage = SimpleNamespace(
        child_processes=1, threads=2, available_memory_bytes=1024,
        available_disk_bytes=2048, running_worker_containers=0,
        running_worker_ids=(), worker_process_counts=(),
        process_probe_ok=True, memory_probe_ok=True, disk_probe_ok=True,
    )
    runtime.codex = SimpleNamespace(sandbox=SimpleNamespace(
        resource_usage=lambda: legacy_usage,
        cached_resource_usage=lambda **_kwargs: legacy_usage,
    ))
    assert runtime.refresh_isolated_resource_usage()['available_memory_bytes'] == 1024
    assert runtime.isolated_resource_usage(cached_only=True)['threads'] == 2
    assert calls == [('refresh', runtime), ('usage', runtime, False)]
def test_resource_probe_failure_keeps_a_safe_typed_code(monkeypatch):
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    _give_current_substrate_proof(resource, monkeypatch)
    monkeypatch.setattr(
        resource,
        '_measure',
        lambda _runtime: (_ for _ in ()).throw(
            WorkspaceBoxUnavailable('Shared filesystem ACL inspection is unavailable')
        ),
    )

    result = resource.usage(object())

    assert result['process_probe_ok'] is False
    assert result['probe_error_code'] == 'filesystem_acl_inspection_unavailable'
    assert resource.last_refresh_error_code == 'filesystem_acl_inspection_unavailable'
    assert resource.usage(object(), cached_only=True)['probe_error_code'] == (
        'filesystem_acl_inspection_unavailable'
    )



def test_refresh_probe_failure_and_unknown_error_are_typed(monkeypatch):
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    monkeypatch.setattr(
        resource,
        '_refresh_substrate_proof',
        lambda _runtime: (_ for _ in ()).throw(
            WorkspaceBoxUnavailable('Native workspace guard failed')
        ),
    )
    assert resource.refresh(object())['probe_error_code'] == 'native_guard_failed'

    unknown = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    _give_current_substrate_proof(unknown, monkeypatch)
    monkeypatch.setattr(
        unknown,
        '_measure',
        lambda _runtime: (_ for _ in ()).throw(RuntimeError('private details must not escape')),
    )
    assert unknown.usage(object())['probe_error_code'] == 'resource_probe_failed'


def test_readiness_carries_only_the_allowlisted_probe_code(monkeypatch, tmp_path):
    class Homes:
        native_launcher = SimpleNamespace(inventory=lambda: [])

        @staticmethod
        def account_home_path(*_args):
            return tmp_path

    binder = SimpleNamespace(store=object(), homes=Homes())
    runtimes = SharedWorkspaceRuntimes(object(), binder=binder)
    runtimes.resources = SimpleNamespace(
        usage=lambda _runtime, cached_only=False: {
            'process_probe_ok': False,
            'memory_probe_ok': False,
            'disk_probe_ok': False,
            'probe_error_code': 'filesystem_acl_inspection_unavailable',
        }
    )
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_ROOT', str(tmp_path))
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_NAME', 'shared-data')
    monkeypatch.setenv('XPERFECT_SHARED_IMAGE', 'sha256:' + 'a' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_MEMORY_BYTES', '1000')
    monkeypatch.setenv('XPERFECT_SHARED_PIDS_LIMIT', '16')
    monkeypatch.setenv('XPERFECT_SHARED_NETWORK', 'workers')
    monkeypatch.setenv('XPERFECT_CONTROLLER_ID', 'c' * 64)
    monkeypatch.setattr(sys, 'platform', 'linux')

    result = runtimes.readiness(
        {'mode': 'shared', 'execution_mode': 'docker'}, runtime=object()
    )

    assert result == {
        'available': False,
        'code': 'shared_resource_authority_unavailable',
        'probe_error_code': 'filesystem_acl_inspection_unavailable',
    }


_FAKE_DOCKER_CLI = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_DOCKER_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")
guard_id = "d" * 64
controller = os.environ["XPERFECT_CONTROLLER_ID"]
removed = os.path.exists(os.path.join(state, "removed"))
survives = os.environ.get("FAKE_DOCKER_GUARD_SURVIVES") == "1"
if args[:2] == ["image", "inspect"]:
    print(args[-1])
elif args[:2] == ["network", "inspect"]:
    print(json.dumps([{"Driver": "bridge", "Containers": {controller: {}}}]))
elif args[:1] == ["create"]:
    open(os.path.join(state, "created"), "w").close()
    print(guard_id)
    if os.environ.get("FAKE_DOCKER_CREATE_TIMEOUT") == "1":
        sys.exit(124)
elif args[:2] == ["ps", "-aq"]:
    if os.path.exists(os.path.join(state, "created")) and not removed:
        print(guard_id)
elif args[:1] == ["inspect"] and args[1] == controller:
    print(json.dumps([{"Id": controller, "State": {"Running": True}, "Mounts": [
        {"Destination": os.environ["XPERFECT_SHARED_VOLUME_ROOT"], "Type": "volume",
         "Name": os.environ["XPERFECT_SHARED_VOLUME_NAME"], "RW": True},
        {"Destination": os.environ["XPERFECT_CONTROL_ROOT"], "Type": "volume",
         "Name": "control-state", "RW": True}]}]))
elif args[:1] == ["inspect"] and (removed and not survives):
    sys.stderr.write("error: no such object: " + args[1] + "\n")
    sys.exit(1)
elif args[:1] == ["inspect"]:
    print(json.dumps([{"Id": guard_id, "State": {"Running": False, "ExitCode": 0, "OOMKilled": False}}]))
elif args[:1] == ["start"]:
    pass
elif args[:1] == ["wait"]:
    print(0)
elif args[:1] == ["logs"]:
    print("xperfect-native-guard-ready")
elif args[:2] == ["rm", "-f"]:
    open(os.path.join(state, "removed"), "w").close()
    print(args[-1])
else:
    sys.stderr.write("unexpected docker call\n")
    sys.exit(2)
'''


def _substrate_probe_runtime(monkeypatch, tmp_path):
    """Drive the probe through the sandbox's real Docker wrapper and CLI exit codes."""
    import functools
    import os

    from workers_projects_runtime.docker_sandbox import DockerSandboxManager

    bin_dir = tmp_path / 'bin'
    state = tmp_path / 'docker-state'
    data = tmp_path / 'data'
    control = tmp_path / 'control'
    for path in (bin_dir, state, data, control):
        path.mkdir()
    (bin_dir / 'docker').write_text(_FAKE_DOCKER_CLI)
    for acl_tool in ('getfacl', 'setfacl'):
        (bin_dir / acl_tool).write_text('#!/bin/sh\nexit 0\n')
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.setenv('FAKE_DOCKER_STATE', str(state))
    monkeypatch.setenv('XPERFECT_CONTROLLER_ID', 'c' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_NETWORK', 'workers')
    monkeypatch.setenv('XPERFECT_SHARED_IMAGE', 'sha256:' + 'a' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_NAME', 'shared-data')
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_ROOT', str(data))
    monkeypatch.setenv('XPERFECT_CONTROL_ROOT', str(control))
    docker = functools.partial(DockerSandboxManager._docker, object())
    runtime = SimpleNamespace(codex=SimpleNamespace(sandbox=SimpleNamespace(_docker=docker)))
    return runtime, state


def test_substrate_proof_accepts_confirmed_guard_removal_through_real_docker_wrapper(
    monkeypatch, tmp_path
):
    # A removed guard makes `docker inspect` exit non-zero. That exit is the
    # removal confirmation, so the probe must read it, not raise on it.
    runtime, state = _substrate_probe_runtime(monkeypatch, tmp_path)
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))

    resource._refresh_substrate_proof(runtime)

    assert resource._proof_matches_configuration(controller='c' * 64, network='workers')
    calls = [json.loads(line) for line in (state / 'calls.jsonl').read_text().splitlines()]
    removal = calls.index(['rm', '-f', 'd' * 64])
    assert calls[removal + 1] == ['inspect', 'd' * 64]
    assert not list((tmp_path / 'data').iterdir())


def test_substrate_proof_fails_closed_when_the_guard_survives_removal(monkeypatch, tmp_path):
    runtime, _state = _substrate_probe_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv('FAKE_DOCKER_GUARD_SURVIVES', '1')
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))

    with pytest.raises(WorkspaceBoxUnavailable, match='cleanup is unconfirmed'):
        resource._refresh_substrate_proof(runtime)
    assert resource._substrate_proof is None


def test_substrate_create_timeout_discovers_and_removes_exact_container(
    monkeypatch, tmp_path
):
    runtime, state = _substrate_probe_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv('FAKE_DOCKER_CREATE_TIMEOUT', '1')
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))

    with pytest.raises(WorkspaceBoxUnavailable, match='Shared substrate probe failed'):
        resource._refresh_substrate_proof(runtime)

    assert resource._substrate_proof is None
    calls = [json.loads(line) for line in (state / 'calls.jsonl').read_text().splitlines()]
    created = next(call for call in calls if call[0] == 'create')
    assert '--name' in created and created[created.index('--name') + 1].startswith(
        'xperfect-readiness-'
    )
    lookup = next(call for call in calls if call[:2] == ['ps', '-aq'])
    assert '--no-trunc' in lookup
    assert ['rm', '-f', 'd' * 64] in calls
    assert calls[-1] == ['inspect', 'd' * 64]
    assert (state / 'removed').is_file()


def _packaged_usage(**overrides):
    usage = {
        'accounting_version': 'workspace-v1', 'child_processes': 0, 'threads': 0,
        'unattributed_child_processes': 0, 'unattributed_threads': 0,
        'available_memory_bytes': 16 * 1024**3, 'available_disk_bytes': 64 * 1024**3,
        'running_worker_containers': 0, 'running_container_ids': [], 'running_worker_ids': [],
        'worker_process_counts': {}, 'accounted_workspace_ids': [],
        'workspace_memory_bytes': 6 * 1024**3,
        'process_probe_ok': True, 'memory_probe_ok': True, 'disk_probe_ok': True,
    }
    usage.update(overrides)
    return usage


def test_new_member_is_charged_one_new_box_before_it_has_an_identity():
    # The service reserves a not-yet-persisted worker under a placeholder key.
    usage = {'workspace_memory_bytes': 1000, 'accounted_workspace_ids': ['resident']}
    isolated = {'profile': 'grok-build', 'resource_memory_bytes': 400}
    placeholder = {workspace_resources.PROSPECTIVE_WORKER_KEY: 400}
    lookup = {}.get

    assert pending_memory(usage, set(placeholder), placeholder, isolated, lookup) == (1000, False)
    assert pending_memory(
        usage, set(placeholder), placeholder, {**isolated, 'workspace_id': 'resident'}, lookup
    ) == (0, False)
    assert pending_memory(
        usage, set(placeholder), {workspace_resources.PROSPECTIVE_WORKER_KEY: 1200},
        isolated, lookup,
    ) == (1000, True)
    with pytest.raises(ValueError, match='no persisted workspace'):
        pending_memory(usage, set(placeholder), placeholder, None, lookup)


def test_packaged_preflight_admits_a_new_worker_with_measured_headroom(tmp_path, monkeypatch):
    import workers_projects_runtime.service as service_module
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.service import HostResourceUsage, WorkersProjectsService
    from workers_projects_runtime.store import Store

    monkeypatch.setenv('WPR_HOST_MIN_AVAILABLE_MEMORY_MB', '2048')
    monkeypatch.setenv('WPR_HOST_MIN_AVAILABLE_DISK_MB', '1024')
    monkeypatch.setattr(
        service_module, 'host_resource_usage',
        lambda _leases: HostResourceUsage(
            child_processes=0, threads=0,
            available_memory_bytes=16 * 1024**3, available_disk_bytes=64 * 1024**3,
        ),
    )

    class PackagedRuntime(StubRuntime):
        preflight_uses_cli_subprocess = True

        def __init__(self):
            super().__init__()
            self.preflight_calls = 0

        def preflight_worker_profile(self, *_args, **_kwargs):
            self.preflight_calls += 1

        def isolated_resource_usage(self, *, cached_only=False):
            return _packaged_usage()

    runtime = PackagedRuntime()
    service = WorkersProjectsService(
        Store(str(tmp_path / 'packaged.sqlite3')), runtime, reconcile_on_startup=False
    )
    try:
        # The exact worker shape create_worker passes for a new isolated member.
        snapshot = service._reserved_runtime_preflight(
            'grok-build', 'docker', tenant_id='local', owner_id='owner-a', lane='mission',
            worker={'resource_class': 'standard', 'resource_memory_bytes': 3 * 1024**3},
        )
    finally:
        service.shutdown()

    assert runtime.preflight_calls == 1
    assert snapshot['availableAfterReservation']['memoryBytes'] == 10 * 1024**3


def test_persisted_prospective_without_workspace_still_fails_closed():
    usage = {'workspace_memory_bytes': 1000, 'accounted_workspace_ids': []}
    with pytest.raises(ValueError, match='no persisted workspace'):
        pending_memory(usage, {'w1'}, {'w1': 400}, {'worker_id': 'w1'}, {}.get)


def test_readiness_loop_renews_the_proof_before_admission_sees_it_lapse(monkeypatch):
    # The service loop refreshes about every ten seconds. Admission only reads
    # the proof, so a proof renewed only after it expires leaves a window in
    # which a launch is refused with "waiting for a healthy resource probe".
    clock = [1000.0]
    monkeypatch.setattr(workspace_resources.time, 'monotonic', lambda: clock[0])
    monkeypatch.setenv('XPERFECT_CONTROLLER_ID', 'c' * 64)
    monkeypatch.setenv('XPERFECT_SHARED_NETWORK', 'workers')
    for key in ('XPERFECT_SHARED_IMAGE', 'XPERFECT_SHARED_VOLUME_NAME',
                'XPERFECT_SHARED_VOLUME_ROOT', 'XPERFECT_CONTROL_ROOT'):
        monkeypatch.delenv(key, raising=False)
    resource = WorkspaceResources(SimpleNamespace(recovery_issues=[]))
    proofs = []

    def prove(_runtime):
        proofs.append(clock[0])
        resource._substrate_proof = (clock[0], {
            'image': '', 'volume_name': '', 'volume_root': '', 'control_root': '',
            'controller': 'c' * 64, 'network': 'workers'})

    resource._refresh_substrate_proof = prove
    resource._measure = lambda _runtime: {
        'process_probe_ok': True, 'memory_probe_ok': True, 'disk_probe_ok': True}

    lapses = []
    next_tick = clock[0]
    while clock[0] < 1300.0:
        if clock[0] >= next_tick:
            resource.refresh(object())
            next_tick = clock[0] + 10.5
        if not resource._proof_matches_configuration(controller='c' * 64, network='workers'):
            lapses.append(clock[0])
        clock[0] += 0.5

    assert lapses == []
    assert all(later - earlier < 30.0 for earlier, later in zip(proofs, proofs[1:]))


def test_confirmed_box_release_invalidates_only_cached_capacity(monkeypatch):
    from workers_projects_runtime.workspace_resources import WorkspaceResources

    resources = WorkspaceResources(object())
    measurements = iter((
        {"available_memory_bytes": 0, "process_probe_ok": True},
        {"available_memory_bytes": 7, "process_probe_ok": True},
    ))
    monkeypatch.setattr(resources, "_proof_matches_configuration", lambda **_kwargs: True)
    monkeypatch.setattr(resources, "_measure", lambda _runtime: next(measurements))

    assert resources.usage(object())["available_memory_bytes"] == 0
    assert resources.usage(object())["available_memory_bytes"] == 0
    resources.invalidate_capacity_snapshot()
    assert resources.usage(object())["available_memory_bytes"] == 7


def test_owner_recovery_settles_only_idle_quarantined_projections():
    records = [
        {'binding': {'worker_id': 'wrk_pending', 'tenant_id': 't', 'owner_id': 'o'}, 'metadata': {}, 'state': 'pending'},
        {'binding': {'worker_id': 'wrk_busy', 'tenant_id': 't', 'owner_id': 'o'}, 'metadata': {}, 'state': 'quarantined'},
        {'binding': {'worker_id': 'wrk_idle', 'tenant_id': 't', 'owner_id': 'o'}, 'metadata': {}, 'state': 'quarantined'},
    ]
    recovered = []

    class ControlStore:
        @staticmethod
        def pending_provider_projections(*, account_id=None, worker_id=None):
            assert account_id == 'acct_1'
            return records

    class Binder:
        store = ControlStore()

        @staticmethod
        def recover_projection(record, *, projection_factory):
            recovered.append(record['binding']['worker_id'])

    class Store:
        @staticmethod
        def get_active_run(worker_id):
            return {'run_id': 'run_live'} if worker_id == 'wrk_busy' else None

        @staticmethod
        def get_worker(worker_id, tenant_id, owner_id):
            return {'worker_id': worker_id, 'tenant_id': tenant_id, 'owner_id': owner_id}

    profiled = SimpleNamespace(_runtime_for_worker=lambda worker: SimpleNamespace(sandbox=SimpleNamespace(box=object())))
    runtimes = SharedWorkspaceRuntimes(Store(), binder=Binder())
    assert runtimes.recover_quarantined(profiled, account_id='acct_1') == ['wrk_idle']
    # A pending (possibly in-flight) projection and a live worker are never touched.
    assert recovered == ['wrk_idle']
