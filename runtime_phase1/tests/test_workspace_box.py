from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
import subprocess

import pytest

from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_box import WorkspaceBox, WorkspaceBoxUnavailable, WorkspaceMemberBinding


def shared_members(tmp_path, count=2):
    store = Store(str(tmp_path / 'runtime.db'))
    project = store.create_project(owner_id='owner', tenant_id='tenant', title='Shared',
                                  goal='Work together', default_worker_profile='codex-cli')
    workspace = store.create_execution_workspace(project_id=project['project_id'], tenant_id='tenant',
                                                  owner_id='owner', execution_mode='docker')
    members = [store.create_worker(project_id=project['project_id'], tenant_id='tenant', owner_id='owner',
                                   workspace_id=workspace['workspace_id'], name=f'Member {i}', role='member',
                                   profile='codex-cli', backend='codex-cli', runtime='codex-cli', model='test')
               for i in range(count)]
    return store, members


def test_discard_empty_prepared_workspace_requires_exact_absent_skeleton(tmp_path, monkeypatch):
    binding = WorkspaceMemberBinding('wsp_test', 'wrk_test', 'tenant', 'owner', 20001)
    box = WorkspaceBox(volume_root=tmp_path / 'volume', volume_name='synthetic', image='reviewed',
                       binding=binding, memory_bytes=1, pids_limit=1,
                       control_root=tmp_path / 'control')
    for path in (box.root / 'common', box.member_root / 'worktree',
                 box.member_root / 'home' / 'tmp', box.member_root / 'home' / '.config',
                 box.member_root / 'home' / '.cache', box.supervisor):
        path.mkdir(parents=True, exist_ok=True)
    marker = box.native_root / '.mount-receipt'
    marker.write_text(binding.owner_digest + ':' + binding.workspace_id)
    (box.supervisor / f'member-{binding.uid}.json').write_text('{}')
    monkeypatch.setattr(box, 'locked', nullcontext)
    monkeypatch.setattr(box, '_inspect', lambda: None)
    user_file = box.member_root / 'worktree' / 'report.txt'
    user_file.write_text('keep')
    assert box.discard_empty_prepared_workspace() is False
    assert user_file.read_text() == 'keep'
    user_file.unlink()
    monkeypatch.setattr(box, '_inspect', lambda: {'Id': 'live'})
    assert box.discard_empty_prepared_workspace() is False
    monkeypatch.setattr(box, '_inspect', lambda: None)
    assert box.discard_empty_prepared_workspace() is True
    assert not box.root.exists()


def test_workspace_identity_allocation_is_concurrent_durable_and_scoped(tmp_path):
    store, members = shared_members(tmp_path, 6)
    def allocate(member):
        return store.reserve_workspace_member_identity(member['worker_id'], tenant_id='tenant', owner_id='owner')
    with ThreadPoolExecutor(max_workers=6) as pool:
        identities = list(pool.map(allocate, members * 2))
    assert len({value['member_uid'] for value in identities}) == 6
    reopened = Store(str(tmp_path / 'runtime.db'))
    for member, original in zip(members, identities):
        assert reopened.reserve_workspace_member_identity(member['worker_id'], tenant_id='tenant', owner_id='owner') == original
    with pytest.raises(ValueError, match='unavailable'):
        store.reserve_workspace_member_identity(members[0]['worker_id'], tenant_id='tenant', owner_id='another-owner')


def test_retired_identity_is_not_reused(tmp_path):
    store, members = shared_members(tmp_path)
    old = store.reserve_workspace_member_identity(members[0]['worker_id'], tenant_id='tenant', owner_id='owner')
    # The identity ledger deliberately has no worker foreign key: retirement
    # must not make a retained native home or surviving process available again.
    assert store.delete_unstarted_worker(members[0]['worker_id'],
                                         project_id=members[0]['project_id'],
                                         tenant_id='tenant', owner_id='owner')
    new = store.reserve_workspace_member_identity(members[1]['worker_id'], tenant_id='tenant', owner_id='owner')
    assert new['member_uid'] > old['member_uid']


def test_shared_box_release_requires_every_durable_member_idle(tmp_path, monkeypatch):
    from contextlib import nullcontext

    store, members = shared_members(tmp_path)
    identities = [
        store.reserve_workspace_member_identity(member['worker_id'], tenant_id='tenant', owner_id='owner')
        for member in members
    ]
    workspace_id = members[0]['workspace_id']
    assert store.idle_execution_workspace_member_uids(workspace_id, 'tenant', 'owner') is None
    with store._connect() as conn:
        conn.execute("UPDATE workers SET compute_released_at='2026-01-01' WHERE worker_id=?",
                     (members[0]['worker_id'],))
    assert store.idle_execution_workspace_member_uids(workspace_id, 'tenant', 'owner') is None
    with store._connect() as conn:
        conn.execute("UPDATE workers SET compute_released_at='2026-01-01' WHERE worker_id=?",
                     (members[1]['worker_id'],))
    expected = {identity['member_uid'] for identity in identities}
    assert store.idle_execution_workspace_member_uids(workspace_id, 'tenant', 'owner') == {
        'uids': expected, 'retired_uids': set()
    }

    box = WorkspaceBox(volume_root=tmp_path, volume_name='synthetic', image='reviewed',
        binding=WorkspaceMemberBinding(workspace_id, members[0]['worker_id'], 'tenant', 'owner', identities[0]['member_uid']),
        memory_bytes=1, pids_limit=1, file_placement='common')
    box.supervisor.mkdir(parents=True)
    for uid in expected:
        (box.supervisor / f'member-{uid}.json').write_text('{}')
    monkeypatch.setattr(box, 'locked', nullcontext)
    present = {'value': True}
    calls = []
    monkeypatch.setattr(
        box, '_inspect',
        lambda: {'Id': 'exact-box', 'State': {'Running': True, 'Pid': 42}}
        if present['value'] else None,
    )
    def docker(args):
        calls.append(args)
        if args == ['top', 'exact-box', '-eLo', 'uid,pid,lwp']:
            return subprocess.CompletedProcess(args, 0, 'UID PID LWP\n65534 1 1\n', '')
        if args == ['rm', '-f', 'exact-box']:
            present['value'] = False
    monkeypatch.setattr(box, 'docker', docker)
    assert box.release_if_all_members_idle(lambda: None) is False
    assert box.release_if_all_members_idle(lambda: {
        'uids': {identities[0]['member_uid']}, 'retired_uids': set()
    }) is False
    assert calls == []
    assert box.release_if_all_members_idle(
        lambda: store.idle_execution_workspace_member_uids(workspace_id, 'tenant', 'owner')
    ) is True
    assert calls == [
        ['top', 'exact-box', '-eLo', 'uid,pid,lwp'],
        ['rm', '-f', 'exact-box'],
    ]


def test_retired_shared_identity_needs_exact_process_absence(tmp_path, monkeypatch):
    from contextlib import nullcontext

    store, members = shared_members(tmp_path)
    identities = [
        store.reserve_workspace_member_identity(member['worker_id'], tenant_id='tenant', owner_id='owner')
        for member in members
    ]
    with store._connect() as conn:
        conn.execute("UPDATE workers SET compute_released_at='2026-01-01' WHERE worker_id=?",
                     (members[0]['worker_id'],))
    retired = members[1]
    assert store.delete_unstarted_worker(
        retired['worker_id'], project_id=retired['project_id'],
        tenant_id='tenant', owner_id='owner',
    )
    workspace_id = members[0]['workspace_id']
    status = store.idle_execution_workspace_member_uids(workspace_id, 'tenant', 'owner')
    assert status == {
        'uids': {identity['member_uid'] for identity in identities},
        'retired_uids': {identities[1]['member_uid']},
    }

    box = WorkspaceBox(volume_root=tmp_path, volume_name='synthetic', image='reviewed',
        binding=WorkspaceMemberBinding(workspace_id, members[0]['worker_id'], 'tenant', 'owner', identities[0]['member_uid']),
        memory_bytes=1, pids_limit=1, file_placement='common')
    box.supervisor.mkdir(parents=True)
    for uid in status['uids']:
        (box.supervisor / f'member-{uid}.json').write_text('{}')
    monkeypatch.setattr(box, 'locked', nullcontext)
    present = {'value': True}
    processes = {'text': f'UID PID LWP\n{identities[1]["member_uid"]} 2 2\n65534 1 1\n'}
    monkeypatch.setattr(box, '_inspect', lambda: {
        'Id': 'exact-box', 'State': {'Running': True, 'Pid': 42}
    } if present['value'] else None)
    removals = []
    def docker(args):
        if args[0] == 'top':
            return subprocess.CompletedProcess(args, 0, processes['text'], '')
        removals.append(args)
        present['value'] = False
    monkeypatch.setattr(box, 'docker', docker)
    read_status = lambda: store.idle_execution_workspace_member_uids(workspace_id, 'tenant', 'owner')
    assert box.release_if_all_members_idle(read_status) is False
    assert removals == []
    processes['text'] = 'UID PID LWP\n30000 3 3\n65534 1 1\n'
    assert box.release_if_all_members_idle(read_status) is False
    assert removals == []
    processes['text'] = 'UID PID LWP\n65534 1 1\n'
    assert box.release_if_all_members_idle(read_status) is True
    assert removals == [['rm', '-f', 'exact-box']]


@pytest.mark.parametrize('grant', ['user:29999:rwx', 'default:user:29999:rwx',
                                   'group:29999:rwx', 'default:group:29999:rwx', 'other::r-x'])
def test_acl_rejects_foreign_access_before_modifying_it(tmp_path, monkeypatch, grant):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, 'user::rwx\ngroup::---\nother::---\n' + grant, '')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(WorkspaceBoxUnavailable, match='unexpected grant'):
        WorkspaceBox._private_acl(tmp_path, 20001)
    assert len(calls) == 1


def test_member_identity_cannot_escape_paths():
    with pytest.raises(ValueError):
        WorkspaceMemberBinding('wsp_../../other', 'wrk_one', 'tenant', 'owner', 20001)
    with pytest.raises(ValueError):
        WorkspaceMemberBinding('wsp_one', 'wrk_one', 'tenant', 'owner', 0)


def test_runtime_dispatch_uses_persisted_membership_and_separate_instances(tmp_path, monkeypatch):
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
    store, members = shared_members(tmp_path)
    for key, value in {'XPERFECT_SHARED_VOLUME_ROOT': str(tmp_path),
                       'XPERFECT_SHARED_VOLUME_NAME': 'synthetic-volume',
                       'XPERFECT_SHARED_IMAGE': 'synthetic-image',
                       'XPERFECT_SHARED_MEMORY_BYTES': '536870912',
                       'XPERFECT_SHARED_PIDS_LIMIT': '256'}.items():
        monkeypatch.setenv(key, value)
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / 'runtime'))
    runtime.configure_execution_workspaces(store)
    first = runtime._runtime_for_worker(members[0])
    second = runtime._runtime_for_worker(members[1])
    assert first is runtime._runtime_for_worker(members[0])
    assert first is not second
    assert first.sandbox.box.name == second.sandbox.box.name
    assert first.sandbox.user != second.sandbox.user
    with pytest.raises(WorkspaceBoxUnavailable, match='owner'):
        runtime._runtime_for_worker({**members[0], 'owner_id': 'another-owner'})


def test_shared_image_accounts_are_locked_and_reject_uid_collisions():
    import importlib.util
    path = Path(__file__).parents[1] / 'containers/prepare_shared_members.py'
    spec = importlib.util.spec_from_file_location('prepare_shared_members', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    passwd, groups = module.project_accounts('root:x:0:0:root:/root:/bin/sh\n', 'root:x:0:\n')
    assert 'member-20001:!:20001:20001:' in passwd
    assert 'member-60000:!:60000:60000:' in passwd
    assert 'member-20001:!:20001:' in groups
    assert 'account-100001:!:100001:100001::/workspace/account:' in passwd
    assert 'account-200000:!:200000:200000::/workspace/account:' in passwd
    assert len(passwd.encode()) < 10 * 1024 * 1024
    assert len(groups.encode()) < 10 * 1024 * 1024
    with pytest.raises(ValueError, match='already uses'):
        module.project_accounts(passwd, groups)


def test_shared_file_placement_is_explicit_and_persistent(tmp_path):
    store, members = shared_members(tmp_path)
    workspace = store.get_execution_workspace(members[0]['workspace_id'], 'tenant', 'owner')
    assert workspace['file_placement'] == 'common'
    private = store.create_execution_workspace(project_id=members[0]['project_id'], tenant_id='tenant',
        owner_id='owner', execution_mode='docker', file_placement='member_private')
    reopened = Store(str(tmp_path / 'runtime.db'))
    assert reopened.get_execution_workspace(private['workspace_id'], 'tenant', 'owner')['file_placement'] == 'member_private'
    with pytest.raises(ValueError, match='placement'):
        store.create_execution_workspace(project_id=members[0]['project_id'], tenant_id='tenant',
            owner_id='owner', execution_mode='docker', file_placement='../common')


def test_common_exec_uses_declared_root_group_and_immutable_guard(tmp_path, monkeypatch):
    from workers_projects_runtime.workspace_sandbox import WorkspaceMemberSandbox
    box = WorkspaceBox(volume_root=tmp_path, volume_name='synthetic', image='reviewed',
        binding=WorkspaceMemberBinding('wsp_shared', 'wrk_one', 'tenant', 'owner', 20001),
        memory_bytes=1, pids_limit=1, file_placement='common')
    monkeypatch.setattr(box, 'ensure_box', lambda: 'exact-container')
    adapter = WorkspaceMemberSandbox(box)
    monkeypatch.setattr(adapter, 'ensure_ready', lambda *a, **k: None)
    command = adapter.exec_command('wrk_one', 'codex-cli', ['cat', 'test.txt'])
    assert command[command.index('--user') + 1] == '20001:20000'
    assert command[command.index('--workdir') + 1] == '/workspace/common'
    assert command[command.index('exact-container') + 1:] == box.guarded_command(['cat', 'test.txt'])
    assert '--env' in command
    assert 'GIT_CONFIG_COUNT=1' in command
    assert 'GIT_CONFIG_KEY_0=safe.directory' in command
    assert 'GIT_CONFIG_VALUE_0=/workspace/common' in command
    assert box.paths()['home_dir'] != box.paths()['workspace_dir']
    for key in ('PYTHONPATH', 'LD_PRELOAD', 'HOME', 'XAI_API_KEY',
                'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'):
        with pytest.raises(ValueError):
            adapter.exec_command('wrk_one', 'codex-cli', ['true'], env={key: 'unsafe'})


def test_native_shared_run_inherits_exact_git_trust(tmp_path, monkeypatch):
    import subprocess
    from workers_projects_runtime.docker_sandbox import DockerSandboxManager
    from workers_projects_runtime.workspace_sandbox import WorkspaceMemberSandbox

    box = WorkspaceBox(volume_root=tmp_path, volume_name='synthetic', image='reviewed',
        binding=WorkspaceMemberBinding('wsp_shared', 'wrk_one', 'tenant', 'owner', 20001),
        memory_bytes=1, pids_limit=1, file_placement='common')
    adapter = WorkspaceMemberSandbox(box)
    monkeypatch.setattr(box, '_inspect', lambda: {'Id': 'exact-container'})
    captured = []

    def fake_exec(_self, container_id, command, **kwargs):
        captured.append((container_id, command, kwargs))
        return subprocess.CompletedProcess(command, 0, '', '')

    monkeypatch.setattr(DockerSandboxManager, '_docker_exec', fake_exec)
    adapter._docker_exec(box.name, ['git', 'status', '--short'])
    assert captured[0][0] == 'exact-container'
    assert captured[0][2]['env']['GIT_CONFIG_COUNT'] == '1'
    assert captured[0][2]['env']['GIT_CONFIG_KEY_0'] == 'safe.directory'
    assert captured[0][2]['env']['GIT_CONFIG_VALUE_0'] == '/workspace/common'
    with pytest.raises(WorkspaceBoxUnavailable, match='Git trust'):
        adapter._docker_exec(box.name, ['true'], env={'GIT_CONFIG_VALUE_0': '*'})


def test_existing_native_identity_lookup_does_not_start_nested_write(tmp_path):
    store, members = shared_members(tmp_path)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        original = store.reserve_workspace_member_identity(members[0]['worker_id'],
            tenant_id='tenant', owner_id='owner', connection=conn)
        assert store.reserve_workspace_member_identity(members[0]['worker_id'],
            tenant_id='tenant', owner_id='owner') == original


def _released_box(tmp_path, monkeypatch, *, members):
    import json
    import sys
    monkeypatch.setattr(sys, 'platform', 'linux')
    binding = WorkspaceMemberBinding('wsp_one', 'wrk_one', 'tenant', 'owner', members[0])
    state = {'container': 'c' * 64, 'removed': []}

    def docker(args, check=True):
        if args[:2] == ['rm', '-f']:
            state['removed'].append(args[2])
            state['container'] = None
            return subprocess.CompletedProcess(args, 0, '', '')
        raise AssertionError(args)

    box = WorkspaceBox(volume_root=tmp_path / 'volume', control_root=tmp_path / 'control',
                       volume_name='synthetic-volume', image='synthetic-image', binding=binding,
                       memory_bytes=536870912, pids_limit=256, docker=docker)
    (tmp_path / 'volume').mkdir()
    with box.locked():
        for uid in members:
            (box.supervisor / f'member-{uid}.json').write_text(json.dumps({'uid': uid}))
    (box.supervisor / f'member-{members[0]}-paused.json').write_text('{}')
    monkeypatch.setattr(box, '_inspect', lambda: {'Id': state['container']} if state['container'] else None)
    return box, state


def test_releasing_the_only_member_removes_its_idle_box(tmp_path, monkeypatch):
    # A separate-workspace box otherwise keeps its memory reservation after the
    # run, so capacity relief marks compute released while admission stays blocked.
    box, state = _released_box(tmp_path, monkeypatch, members=[20001])
    assert box.release_if_sole_member('c' * 64) is True
    assert state['removed'] == ['c' * 64]
    assert box.release_if_sole_member('c' * 64) is True


def test_a_box_with_another_member_stays_alive(tmp_path, monkeypatch):
    box, state = _released_box(tmp_path, monkeypatch, members=[20001, 20002])
    assert box.release_if_sole_member('c' * 64) is False
    assert state['removed'] == []


def test_box_release_refuses_a_changed_generation(tmp_path, monkeypatch):
    box, state = _released_box(tmp_path, monkeypatch, members=[20001])
    with pytest.raises(WorkspaceBoxUnavailable, match='generation'):
        box.release_if_sole_member('d' * 64)
    assert state['removed'] == []


def test_a_removed_box_stops_serving_its_runtime_socket_and_a_live_one_keeps_it(tmp_path, monkeypatch):
    from workers_projects_runtime import native_transport
    released = []
    monkeypatch.setattr(native_transport, 'release_box_socket', released.append)
    box, state = _released_box(tmp_path, monkeypatch, members=[20001, 20002])
    assert box.release_if_sole_member('c' * 64) is False
    assert released == []
    (tmp_path / 'sole').mkdir()
    box, state = _released_box(tmp_path / 'sole', monkeypatch, members=[20001])
    assert box.release_if_sole_member('c' * 64) is True
    assert released == [box.native_root]


def test_a_box_whose_runtime_socket_cannot_be_served_is_unavailable(tmp_path, monkeypatch):
    from workers_projects_runtime import native_transport
    box, _ = _released_box(tmp_path, monkeypatch, members=[20001])
    served = []
    monkeypatch.setattr(native_transport, 'ensure_box_socket', served.append)
    box._serve_native_socket()
    assert served == [box.native_root]

    def refused(_root):
        raise PermissionError('not a private service directory')
    monkeypatch.setattr(native_transport, 'ensure_box_socket', refused)
    with pytest.raises(WorkspaceBoxUnavailable, match='runtime socket'):
        box._serve_native_socket()
