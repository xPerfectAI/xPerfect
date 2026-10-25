"""Optional display cgroups keep exact accounting and parent idle-release fences."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
import json
import subprocess

import pytest

from workers_projects_runtime.workspace_box import WorkspaceBox, WorkspaceBoxUnavailable, WorkspaceMemberBinding
from workers_projects_runtime.workspace_desktop import DISPLAY_MEMORY_BYTES, WorkspaceMemberDesktop
from workers_projects_runtime.workspace_resources import WorkspaceResources
from workers_projects_runtime.workspace_runtime import SharedWorkspaceRuntimes


MIB = 1024 ** 2
CONTROLLER, PARENT, DISPLAY = 'c' * 64, 'b' * 64, 'd' * 64


def measured_resources(monkeypatch, tmp_path, *, orphan=False, retired=False,
                       malformed=False, foreign_process=False, stale=False,
                       controller=CONTROLLER, foreign_deployment=False, two_displays=False,
                       retained_memory=DISPLAY_MEMORY_BYTES):
    for key, value in {
        'XPERFECT_CONTROLLER_ID': controller, 'XPERFECT_SHARED_NETWORK': 'native-test',
        'XPERFECT_SHARED_VOLUME_ROOT': str(tmp_path),
        'XPERFECT_SHARED_VOLUME_NAME': 'synthetic-data',
        'XPERFECT_SHARED_MEMORY_BYTES': str(512 * MIB),
    }.items():
        monkeypatch.setenv(key, value)
    calls = []
    worker = {'worker_id': 'wrk_display', 'member_uid': 20001, 'profile': 'codex-cli'}
    second = {'worker_id': 'wrk_second', 'member_uid': 20002, 'profile': 'codex-cli'}
    workers = [worker, second] if two_displays else [worker]
    second_display = 'a' * 64

    class Store:
        @contextmanager
        def _connect(self):
            yield SimpleNamespace(execute=lambda _query: [] if retired else workers)

    box = SimpleNamespace(name='native-box', binding=SimpleNamespace(workspace_id='wsp_test'),
                          memory_bytes=512 * MIB,
                          _inspect=lambda: {'Id': PARENT, 'State': {'Running': True}})

    def resource_record(expected):
        assert expected == PARENT
        if stale:
            raise WorkspaceBoxUnavailable('Desktop belongs to another workspace generation')
        return {'Id': DISPLAY, 'State': {'Running': True}, 'HostConfig': {'Memory': retained_memory}}

    desktop = SimpleNamespace(resource_record=resource_record)
    member = SimpleNamespace(sandbox=SimpleNamespace(box=box, desktop=desktop))
    second_member = SimpleNamespace(sandbox=SimpleNamespace(box=box, desktop=SimpleNamespace(
        resource_record=lambda expected: {'Id': second_display, 'State': {'Running': True},
                                         'HostConfig': {'Memory': DISPLAY_MEMORY_BYTES}})))
    shared = SimpleNamespace(store=Store(), recovery_issues=[],
        binder=SimpleNamespace(homes=SimpleNamespace(native_launcher=SimpleNamespace(inventory=lambda: ()))),
        for_worker=lambda row, *_args, **_kwargs: second_member if row['worker_id'] == second['worker_id'] else member)

    def docker(args, **_kwargs):
        calls.append(args)
        if args[0] == 'info':
            output = json.dumps({'CgroupVersion': '2', 'MemTotal': 2048 * MIB})
        elif args[:2] == ['network', 'inspect']:
            output = json.dumps([{'Driver': 'bridge', 'Containers': {controller: {}, PARENT: {}}}])
            if retired:
                output = json.dumps([{'Driver': 'bridge', 'Containers': {controller: {}}}])
        elif args[0] == 'top':
            output = ('UID PID LWP\n20002 1 1\n20002 2 2\n' if args[1] == second_display else
                      'UID PID LWP\n65534 1 1\n20001 2 2\n20001 2 3\n' if args[1] == PARENT
                      else ('UID PID LWP\n20002 1 1\n' if foreign_process
                            else 'UID PID LWP\n20001 1 1\n20001 2 2\n20001 2 3\n'))
        elif args[:2] == ['container', 'ls']:
            assert 'label=xperfect.desktop.worker' in args
            deployment = WorkspaceMemberDesktop.deployment_for_volume('synthetic-data')
            assert 'label=xperfect.desktop.deployment=' + deployment in args
            assert not any(value.startswith('label=xperfect.desktop.controller=') for value in args)
            # Model Docker's actual label filter over two deployment records;
            # the other volume is neither adopted nor an orphan in this one.
            records = [(DISPLAY, deployment)]
            if two_displays:
                records.append((second_display, deployment))
            if foreign_deployment:
                records.append(('e' * 64, WorkspaceMemberDesktop.deployment_for_volume('another-data')))
            if orphan:
                records.append(('f' * 64, deployment))
            output = 'not-an-exact-id' if malformed else '\n'.join(identity for identity, scope in records if scope == deployment)
        elif args[0] == 'stats':
            records = [(controller, '100MiB / 1GiB'), (PARENT, '4MiB / 512MiB'),
                       (DISPLAY, f'1MiB / {DISPLAY_MEMORY_BYTES // MIB}MiB')]
            if two_displays:
                records.append((second_display, f'1MiB / {DISPLAY_MEMORY_BYTES // MIB}MiB'))
            output = '\n'.join(json.dumps({'ID': identity, 'MemUsage': usage}) for identity, usage in records)
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, '')

    runtime = SimpleNamespace(codex=SimpleNamespace(sandbox=SimpleNamespace(_docker=docker)),
                              _runtime_for_profile=lambda *_args: None)
    resource = WorkspaceResources(shared)
    monkeypatch.setattr(resource, '_proof_matches_configuration', lambda **_kwargs: True)
    return resource, runtime, calls


def test_display_cgroup_is_reserved_once_and_its_uid_processes_are_attributed(monkeypatch, tmp_path):
    resource, runtime, calls = measured_resources(monkeypatch, tmp_path)
    result = resource._measure(runtime)
    # Reserve the 512MiB native box and 512MiB optional display, not their
    # small current resident bytes. Controller memory remains measured once.
    assert result['available_memory_bytes'] == (2048 - 512 - 512 - 100) * MIB
    assert result['running_container_ids'] == [PARENT, DISPLAY]
    assert result['running_worker_containers'] == 1
    assert result['worker_process_counts'] == {'wrk_display': {'child_processes': 3, 'threads': 5}}
    assert result['child_processes'] == 4 and result['threads'] == 6
    assert result['unattributed_child_processes'] == 1
    assert len([args for args in calls if args[:2] == ['top', DISPLAY]]) == 1


def test_two_displays_each_reserve_their_full_browser_budget(monkeypatch, tmp_path):
    resource, runtime, calls = measured_resources(monkeypatch, tmp_path, two_displays=True)
    result = resource._measure(runtime)
    assert result['available_memory_bytes'] == (2048 - 512 - 2 * 512 - 100) * MIB
    assert set(result['running_container_ids']) == {PARENT, DISPLAY, 'a' * 64}
    assert result['worker_process_counts']['wrk_second'] == {'child_processes': 2, 'threads': 2}
    assert len([args for args in calls if args[:2] == ['top', 'a' * 64]]) == 1


@pytest.mark.parametrize('retained_memory', [256 * MIB, 768 * MIB])
def test_retained_display_is_charged_at_actual_bounded_memory(monkeypatch, tmp_path, retained_memory):
    resource, runtime, _ = measured_resources(monkeypatch, tmp_path, retained_memory=retained_memory)
    result = resource._measure(runtime)
    assert result['available_memory_bytes'] == (2048 - 512 - 100) * MIB - retained_memory


@pytest.mark.parametrize(('scenario', 'message'), [
    ('orphan', 'Unregistered desktop generation'),
    ('retired', 'Unregistered desktop generation'),
    ('malformed', 'Desktop inventory is malformed'),
    ('foreign_process', 'Desktop process identity changed'),
    ('stale', 'another workspace generation'),
])
def test_restart_inventory_never_adopts_or_ignores_unverified_displays(monkeypatch, tmp_path, scenario, message):
    resource, runtime, calls = measured_resources(monkeypatch, tmp_path, **{scenario: True})
    with pytest.raises(WorkspaceBoxUnavailable, match=message):
        resource._measure(runtime)
    assert not any(args[0] in {'rm', 'stop', 'kill', 'exec'} for args in calls)


def test_recreated_accountant_reads_same_durable_display_identity(monkeypatch, tmp_path):
    first, runtime, _ = measured_resources(monkeypatch, tmp_path)
    before = first._measure(runtime)
    second = WorkspaceResources(first.shared)
    monkeypatch.setattr(second, '_proof_matches_configuration', lambda **_kwargs: True)
    assert second.cached is None
    assert second._measure(runtime) == before


def test_supported_controller_restart_retains_same_deployment_display_charge(monkeypatch, tmp_path):
    first, runtime, _ = measured_resources(monkeypatch, tmp_path)
    before = first._measure(runtime)
    restarted, current_runtime, _ = measured_resources(monkeypatch, tmp_path, controller='a' * 64)
    after = restarted._measure(current_runtime)
    assert after == before
    assert after['running_container_ids'] == [PARENT, DISPLAY]


def test_another_deployment_display_is_never_adopted_or_falsely_orphaned(monkeypatch, tmp_path):
    resource, runtime, calls = measured_resources(monkeypatch, tmp_path, foreign_deployment=True)
    result = resource._measure(runtime)
    assert result['running_container_ids'] == [PARENT, DISPLAY]
    assert not any('e' * 64 in args for args in calls)


@pytest.mark.parametrize('available', [DISPLAY_MEMORY_BYTES - 1, DISPLAY_MEMORY_BYTES])
def test_optional_display_admission_uses_current_bound_and_invalidates_snapshot(monkeypatch, available):
    resources = WorkspaceResources(object())
    monkeypatch.setattr(resources, '_proof_matches_configuration', lambda **_kwargs: True)
    monkeypatch.setattr(resources, '_measure_settled', lambda _runtime: {
        'process_probe_ok': True, 'memory_probe_ok': True, 'disk_probe_ok': True,
        'available_memory_bytes': available})
    resources.cached = (1, {'old': True})
    entered = []
    def create():
        with resources.desktop_capacity(object()):
            entered.append(True)
            assert resources.usage(None, cached_only=True)['probe_error_code'] == 'resource_probe_in_progress'
    if available < DISPLAY_MEMORY_BYTES:
        with pytest.raises(WorkspaceBoxUnavailable, match='headroom'):
            create()
        assert entered == []
    else:
        create()
        assert entered == [True]
    assert resources.cached is None


def test_failed_display_start_and_missing_proof_do_not_leave_capacity_authority(monkeypatch):
    resources = WorkspaceResources(object())
    monkeypatch.setattr(resources, '_proof_matches_configuration', lambda **_kwargs: False)
    with pytest.raises(WorkspaceBoxUnavailable, match='substrate proof'):
        with resources.desktop_capacity(object()):
            pytest.fail('No display may start without the existing substrate proof')
    monkeypatch.setattr(resources, '_proof_matches_configuration', lambda **_kwargs: True)
    monkeypatch.setattr(resources, '_measure_settled', lambda _runtime: {
        'process_probe_ok': True, 'memory_probe_ok': True, 'disk_probe_ok': True,
        'available_memory_bytes': DISPLAY_MEMORY_BYTES})
    resources.cached = (1, {'old': True})
    with pytest.raises(RuntimeError, match='Synthetic start failed'):
        with resources.desktop_capacity(object()):
            raise RuntimeError('Synthetic start failed')
    assert resources.cached is None


def idle_box(tmp_path, monkeypatch):
    box = WorkspaceBox(volume_root=tmp_path/'volume', volume_name='synthetic', image='reviewed',
        binding=WorkspaceMemberBinding('wsp_test', 'wrk_test', 'tenant', 'owner', 20001),
        memory_bytes=1, pids_limit=1, control_root=tmp_path/'control')
    box.supervisor.mkdir(parents=True)
    (box.supervisor/'member-20001.json').write_text('{}')
    monkeypatch.setattr(box, 'locked', nullcontext)
    present, calls = [True], []
    monkeypatch.setattr(box, '_inspect', lambda: {'Id': PARENT, 'State': {'Running': True, 'Pid': 1}} if present[0] else None)
    def docker(args):
        calls.append(args)
        if args[0] == 'top':
            return subprocess.CompletedProcess(args, 0, 'UID PID LWP\n65534 1 1\n', '')
        if args[:2] == ['rm', '-f']:
            assert args[2] == PARENT
            present[0] = False
        else:
            raise AssertionError(args)
    monkeypatch.setattr(box, 'docker', docker)
    monkeypatch.setattr(box, '_release_native_socket', lambda: None)
    return box, calls


def test_authorized_human_display_keeps_idle_parent_and_rechecks_before_release(tmp_path, monkeypatch):
    box, calls = idle_box(tmp_path, monkeypatch)
    observations = iter((True, False, True, False, False))
    def displays(observed_box, identity, uids):
        assert observed_box is box and identity == PARENT and uids == {20001}
        return next(observations)
    monkeypatch.setattr(WorkspaceMemberDesktop, 'has_live_desktops', staticmethod(displays))
    idle = lambda: {'uids': {20001}, 'retired_uids': set()}
    assert box.release_if_all_members_idle(idle) is False
    assert calls == []
    assert box.release_if_all_members_idle(idle) is False
    assert not any(args[0] == 'rm' for args in calls)
    assert box.release_if_all_members_idle(idle) is True
    assert len([args for args in calls if args[0] == 'rm']) == 1


def test_stale_display_generation_blocks_parent_release_without_control(tmp_path, monkeypatch):
    box, calls = idle_box(tmp_path, monkeypatch)
    def stale(*_args):
        raise WorkspaceBoxUnavailable('Desktop belongs to another workspace generation')
    monkeypatch.setattr(WorkspaceMemberDesktop, 'has_live_desktops', staticmethod(stale))
    with pytest.raises(WorkspaceBoxUnavailable, match='generation'):
        box.release_if_all_members_idle(lambda: {'uids': {20001}, 'retired_uids': set()})
    assert calls == []


def test_restart_runtime_binds_capacity_authority_even_without_account_recovery():
    shared = SharedWorkspaceRuntimes(object())
    runtime = object()
    shared.recover_pending(runtime)
    assert shared._profiled_runtime is runtime


def test_cached_member_desktop_callbacks_use_current_recovered_resource_owner(monkeypatch, tmp_path):
    from workers_projects_runtime import execution_profile, workspace_runtime
    monkeypatch.setattr(execution_profile, 'packaged_linux', lambda: True)
    for key, value in {
        'XPERFECT_SHARED_VOLUME_ROOT': str(tmp_path), 'XPERFECT_SHARED_VOLUME_NAME': 'synthetic',
        'XPERFECT_SHARED_IMAGE': 'reviewed', 'XPERFECT_SHARED_MEMORY_BYTES': str(512 * MIB),
        'XPERFECT_SHARED_PIDS_LIMIT': '128', 'XPERFECT_SHARED_NETWORK': 'native-test',
        'XPERFECT_CONTROL_ROOT': str(tmp_path/'control'),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(workspace_runtime, 'WorkspaceBox', lambda **values: SimpleNamespace(**values))
    monkeypatch.setattr(workspace_runtime, 'WorkspaceMemberSandbox', lambda box: SimpleNamespace(box=box))
    store = SimpleNamespace(
        get_execution_workspace=lambda *_args: {'mode': 'isolated', 'execution_mode': 'docker',
                                                'file_placement': 'member_private'},
        reserve_workspace_member_identity=lambda *_args, **_kwargs: {'member_uid': 20001})
    class Native:
        def __init__(self, base_dir, create_directories=True):
            self.base_dir = base_dir
    shared = SharedWorkspaceRuntimes(store)
    first, recovered = object(), object()
    shared.recover_pending(first)
    seen = []
    @contextmanager
    def capacity(runtime):
        seen.append(runtime)
        yield
    monkeypatch.setattr(shared.resources, 'desktop_capacity', capacity)
    worker = {'workspace_id': 'wsp_test', 'worker_id': 'wrk_test', 'tenant_id': 'tenant', 'owner_id': 'owner'}
    template = Native(str(tmp_path/'native'))
    member = shared.for_worker(worker, template)
    with member.sandbox.box.desktop_capacity():
        pass
    shared.recover_pending(recovered)
    assert shared.for_worker(worker, template) is member
    with member.sandbox.box.desktop_capacity():
        pass
    assert seen == [first, recovered]
    shared.resources.cached = (1, {'old': True})
    member.sandbox.box.desktop_changed()
    assert shared.resources.cached is None
