import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers_projects_runtime.workspace_box import WorkspaceBoxUnavailable, WorkspaceMemberBinding
from workers_projects_runtime.workspace_projection import assert_native_launch, assert_native_resume, credential_command, native_binding
from workers_projects_runtime.native_credential_exec import credential_environment


def test_pending_account_requires_exact_run_attempt_and_container():
    member = WorkspaceMemberBinding('wsp_shared', 'wrk_one', 'tenant', 'owner', 20001)
    identity = {'worker_id': member.worker_id, 'workspace_id': member.workspace_id, 'member_uid': member.uid,
                'run_id': 'run', 'attempt_id': 'attempt', 'lease_id': 'lease', 'container_id': 'exact'}
    observed = []
    store = SimpleNamespace(pending_provider_projections=lambda **k: [{'binding': identity, 'state': 'pending'}],
                            assert_provider_projection=lambda **k: observed.append(k))
    box = SimpleNamespace(binding=member, _inspect=lambda: {'Id': 'exact'})
    with pytest.raises(WorkspaceBoxUnavailable):
        assert_native_launch(box, store)
    worker = {'worker_id': 'wrk_one', '_active_run_id': 'run', '_run_attempt_id': 'attempt',
              '_glasshive_provider_account_projected': True, '_glasshive_provider_projection_lease_id': 'lease'}
    with native_binding(worker):
        assert_native_launch(box, store)
        assert observed == [{'binding': identity}]
        box._inspect = lambda: {'Id': 'replacement'}
        with pytest.raises(WorkspaceBoxUnavailable, match='generation'):
            assert_native_launch(box, store)
    box._inspect = lambda: {'Id': 'exact'}
    for field in ('_active_run_id', '_run_attempt_id', '_glasshive_provider_projection_lease_id'):
        with native_binding({**worker, field: 'another'}):
            with pytest.raises(WorkspaceBoxUnavailable, match='exact'):
                assert_native_launch(box, store)
    with pytest.raises(WorkspaceBoxUnavailable):
        assert_native_launch(box, store)


def test_pending_account_resume_requires_exact_live_claim_and_paused_attempt():
    from datetime import datetime, timedelta, timezone

    member = WorkspaceMemberBinding('wsp_shared', 'wrk_one', 'tenant', 'owner', 20001)
    binding = {'worker_id': 'wrk_one', 'workspace_id': 'wsp_shared', 'member_uid': 20001,
               'tenant_id': 'tenant', 'owner_id': 'owner', 'run_id': 'run',
               'attempt_id': 'attempt', 'lease_id': 'lease', 'container_id': 'exact'}
    worker = {'worker_id': 'wrk_one', 'state': 'paused', 'compute_release_kind': 'resume_run',
              'compute_release_token': 'token', 'compute_release_epoch': 2,
              'compute_release_operation_id': 'operation',
              'compute_release_target_run_id': 'run',
              'compute_release_target_started_at': 'start',
              'compute_release_container_id': 'exact',
              'compute_release_expires_at': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    run = {'run_id': 'run', 'worker_id': 'wrk_one', 'state': 'paused',
           'started_at': 'start', 'active_attempt_id': 'attempt'}
    observed = []
    control = SimpleNamespace(pending_provider_projections=lambda **_: [{'binding': binding, 'state': 'pending'}],
                              assert_provider_projection=lambda **kwargs: observed.append(kwargs))
    runtime = SimpleNamespace(get_worker=lambda *args: worker, get_run=lambda _: run)
    box = SimpleNamespace(binding=member, _inspect=lambda: {'Id': 'exact'})
    assert assert_native_resume(box, control, runtime, dict(worker)) is True
    assert observed == [{'binding': binding}]
    for change in ({'compute_release_token': 'wrong'}, {'compute_release_epoch': 1},
                   {'compute_release_target_run_id': 'other'},
                   {'compute_release_container_id': 'other'}):
        with pytest.raises(WorkspaceBoxUnavailable, match='exact paused'):
            assert_native_resume(box, control, runtime, {**worker, **change})
    for change in ({'state': 'running'}, {'active_attempt_id': 'other'}, {'run_id': 'other'}):
        old = dict(run)
        run.update(change)
        with pytest.raises(WorkspaceBoxUnavailable, match='exact paused'):
            assert_native_resume(box, control, runtime, dict(worker))
        run.clear(); run.update(old)
    box._inspect = lambda: {'Id': 'replacement'}
    with pytest.raises(WorkspaceBoxUnavailable, match='generation'):
        assert_native_resume(box, control, runtime, dict(worker))
    box._inspect = lambda: {'Id': 'exact'}
    worker['compute_release_expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with pytest.raises(WorkspaceBoxUnavailable, match='exact paused'):
        assert_native_resume(box, control, runtime, dict(worker))


def test_pending_projection_resumes_existing_member_without_new_launch(tmp_path):
    from workers_projects_runtime.workspace_sandbox import WorkspaceMemberSandbox

    member = WorkspaceMemberBinding('wsp_shared', 'wrk_one', 'tenant', 'owner', 20001)
    marker = tmp_path / 'member-20001-paused.json'
    marker.write_text(json.dumps({'container_id': 'exact'}))
    actions = []
    box = SimpleNamespace(binding=member, supervisor=tmp_path,
                          _inspect=lambda: {'Id': 'exact'},
                          ensure_box=lambda: pytest.fail('resume created or launched a box'),
                          set_member_paused=lambda container, paused: actions.append((container, paused)))
    sandbox = object.__new__(WorkspaceMemberSandbox)
    sandbox.box = box
    sandbox.assert_native_launch = lambda: pytest.fail('resume used the new-launch guard')
    sandbox.assert_native_resume = lambda worker: worker['compute_release_kind'] == 'resume_run'
    sandbox.inspect = lambda _worker_id: SimpleNamespace(state='running', container_id='exact')
    worker = {'worker_id': 'wrk_one', 'tenant_id': 'tenant', 'owner_id': 'owner',
              'bootstrap_profile': 'none', 'compute_release_kind': 'resume_run'}
    observed = sandbox.ensure_ready(worker, 'grok-build')
    assert observed.state == 'running'
    assert actions == [('exact', False)]
    assert not marker.exists()


def test_secret_loader_does_not_put_key_in_native_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path / 'credential.json'
    path.write_text(json.dumps({'value': 'synthetic-key'})); path.chmod(0o600)
    selectors = {'XAI_API_KEY': str(path)}
    worker = {'_glasshive_provider_account_projected': True, '_glasshive_provider_account_secret_files': selectors}
    with native_binding(worker):
        args = credential_command(['grok', '--version'])
        assert args[:4] == ['/usr/bin/python3', '-I', '-m', 'workers_projects_runtime.native_credential_exec']
        assert 'synthetic-key' not in ' '.join(args)
        assert credential_environment(selectors) == {'XAI_API_KEY': 'synthetic-key'}
    assert credential_command(['grok']) == ['grok']
    path.chmod(0o644)
    with pytest.raises(ValueError): credential_environment(selectors)
    path.unlink(); path.symlink_to(tmp_path / 'missing')
    with pytest.raises(OSError): credential_environment(selectors)
    with pytest.raises(ValueError): credential_environment({'LD_PRELOAD': str(path)})


def _projection_box(tmp_path, *, running_before):
    from workers_projects_runtime.workspace_projection import projection_factory

    member = WorkspaceMemberBinding('wsp_one', 'wrk_one', 'tenant', 'owner', 20001)
    state = {'running': running_before, 'ensured': 0}
    home = tmp_path / 'home'
    home.mkdir()

    def ensure_box():
        state['ensured'] += 1
        state['running'] = True
        return 'c' * 64

    box = SimpleNamespace(
        binding=member, supervisor=tmp_path, ensure_box=ensure_box,
        _inspect=lambda: ({'Id': 'c' * 64, 'State': {'Running': True}} if state['running'] else None),
        paths=lambda: {'home_dir': home}, stop_member=lambda _container_id: None,
    )
    store = SimpleNamespace(get_provider_account_record=lambda **_kwargs: {
        'provider': 'grok', 'auth_method': 'subscription'})
    worker = {'worker_id': 'wrk_one', 'workspace_id': 'wsp_one', 'tenant_id': 'tenant', 'owner_id': 'owner'}
    lease = {'account_id': 'acct_one', 'lease_id': 'lease', 'run_id': 'run'}
    construct = projection_factory(box, store)
    return state, lambda: construct(worker=worker, account_home=tmp_path / 'account', lease=lease,
                                    attempt_id='attempt', assert_lease=lambda: None)


def test_account_projection_prepares_a_fresh_member_box_first(tmp_path):
    # Account-bound runs project credentials before the native start, so a new
    # member's credential-free box must exist before its binding is recorded.
    state, construct = _projection_box(tmp_path, running_before=False)
    projection = construct()
    assert state['ensured'] == 1
    assert projection.transaction.binding.container_id == 'c' * 64
    assert projection.environment == {'GROK_AUTH_PATH': '/workspace/data/members/20001/home/.grok/auth.json'}


def test_account_projection_fails_closed_when_the_box_cannot_run(tmp_path):
    with pytest.raises(WorkspaceBoxUnavailable, match='Prepare the exact shared member'):
        _projection_box_with(tmp_path / 'stopped', ensure=lambda: 'c' * 64)()

    with pytest.raises(WorkspaceBoxUnavailable, match='not running'):
        _projection_box_with(tmp_path / 'failed', ensure=lambda: (_ for _ in ()).throw(
            WorkspaceBoxUnavailable('Workspace container is not running')))()


def _projection_box_with(tmp_path, *, ensure):
    from workers_projects_runtime.workspace_projection import projection_factory

    tmp_path.mkdir()
    member = WorkspaceMemberBinding('wsp_one', 'wrk_one', 'tenant', 'owner', 20001)
    box = SimpleNamespace(binding=member, supervisor=tmp_path, ensure_box=ensure,
                          _inspect=lambda: None, paths=lambda: {'home_dir': tmp_path})
    construct = projection_factory(box, SimpleNamespace())
    worker = {'worker_id': 'wrk_one', 'workspace_id': 'wsp_one', 'tenant_id': 'tenant', 'owner_id': 'owner'}
    return lambda: construct(worker=worker, account_home=tmp_path, lease={}, attempt_id='a',
                             assert_lease=lambda: None)


def test_runtime_default_bootstrap_matches_the_former_client_defaults():
    # The UI client used to send these host projections itself. Local host and
    # Docker workers keep them through the runtime default; packaged shared
    # members receive no host projection and use per-run account projection.
    from workers_projects_runtime.bootstrap import bootstrap_profile_for

    assert bootstrap_profile_for({}, 'codex-cli') == 'codex-host'
    assert bootstrap_profile_for({}, 'claude-code') == 'claude-host'
    assert bootstrap_profile_for({}, 'openclaw') == 'host-login'
    assert bootstrap_profile_for({}, 'grok-build') == 'host-login'
