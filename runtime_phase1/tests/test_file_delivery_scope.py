"""Current file roots are facts, and local-only content remains local."""
import json
from pathlib import Path

import pytest
from test_conversation_provider import lifecycle_owned_test_clients
from workers_projects_runtime import profile_runtime
from workers_projects_runtime.bootstrap import conversation_file_delivery_instructions


@pytest.mark.parametrize('protected_parent', [False, True])
@pytest.mark.parametrize('absolute_reference', [False, True])
def test_native_workspace_reference_mapping_checks_current_protected_roots(tmp_path, monkeypatch, protected_parent, absolute_reference):
    from workers_projects_runtime.workspace_files import native_workspace_relative_path

    workspace = tmp_path / 'workspace'
    protected = workspace / 'local-only'
    protected.mkdir(parents=True)
    target = protected / 'result.html'
    target.write_text('<p>Synthetic result</p>')
    worker = {'execution_mode': 'host', 'workspace_dir': str(workspace)}
    relative = target.relative_to(workspace).as_posix()
    reference = str(target) if absolute_reference else relative

    assert native_workspace_relative_path(None, worker, reference) == relative
    monkeypatch.setenv('GLASSHIVE_PROTECTED_ARTIFACT_ROOTS', str(workspace if protected_parent else protected))
    assert native_workspace_relative_path(None, worker, reference) is None
    monkeypatch.delenv('GLASSHIVE_PROTECTED_ARTIFACT_ROOTS')
    assert native_workspace_relative_path(None, worker, reference) == relative


@pytest.mark.parametrize('protected_parent', [False, True])
def test_current_artifact_metadata_and_html_discovery_recheck_local_only_roots(tmp_path, monkeypatch, protected_parent):
    from workers_projects_runtime.deliverables import candidate_html_paths
    from workers_projects_runtime.store import Store
    from workers_projects_runtime.workspace_files import FileAdmissionError, WorkspaceFiles
    store = Store(str(tmp_path / 'runtime.db'))
    try:
        project = store.create_project('owner', 'Synthetic project', '', 'codex-cli')
        workspace = tmp_path / 'workspace'
        protected = workspace / 'local-only'
        protected.mkdir(parents=True)
        target = protected / 'result.html'
        target.write_text('<p>Synthetic result</p>')
        worker = store.create_worker(project['project_id'], 'owner', 'Worker', 'general',
            'codex-cli', 'stub', 'stub', 'synthetic')
        worker = store.update_worker(worker['worker_id'], workspace_dir=str(workspace))
        files = WorkspaceFiles(store)
        relative = target.relative_to(workspace).as_posix()
        expected = {'filename': target.name, 'size_bytes': target.stat().st_size}
        assert files.current_artifact(worker, relative) == expected
        assert candidate_html_paths(worker) == [target]
        monkeypatch.setenv('GLASSHIVE_PROTECTED_ARTIFACT_ROOTS', str(workspace if protected_parent else protected))
        with pytest.raises(FileAdmissionError) as denied:
            files.current_artifact(worker, relative)
        assert denied.value.status_code == 404
        assert candidate_html_paths(worker) == []
        monkeypatch.delenv('GLASSHIVE_PROTECTED_ARTIFACT_ROOTS')
        assert files.current_artifact(worker, relative) == expected
        assert candidate_html_paths(worker) == [target]
    finally:
        store.close()


@pytest.mark.parametrize('workspace_local,tmp_local', [(False,False),(True,False),(False,True),(True,True)])
def test_worker_sees_each_current_delivery_root_without_private_paths(tmp_path, monkeypatch, workspace_local, tmp_local):
    runtime = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path/'runtime'))
    worker = {'worker_id':'worker', 'owner_id':'owner', 'bootstrap_bundle_json':'{}'}
    workspace = tmp_path/'admitted-workspace'
    managed_tmp = runtime._home_dir('worker')/'.tmp'
    monkeypatch.setattr(runtime, '_host_native_mission_placement', lambda _w: {})
    monkeypatch.setattr(profile_runtime, 'is_protected_artifact_path', lambda path:
        (Path(path)==workspace and workspace_local) or (Path(path)==managed_tmp and tmp_local))
    rendered = runtime._host_mission_instruction(worker, 'Create the requested new report.',
        {'file_output_transport':'artifact_sha256','workspace_dir':str(workspace)})
    expected = {'workspace':'local_only' if workspace_local else 'supported',
                'tmpdir':'local_only' if tmp_local else 'supported'}
    assert json.dumps(expected, sort_keys=True) in rendered
    assert 'including copies' in rendered
    assert 'Do not stage those contents elsewhere for delivery' in rendered
    assert str(workspace) not in rendered and str(managed_tmp) not in rendered


def test_unknown_roots_do_not_claim_support_or_new_access():
    rendered = conversation_file_delivery_instructions()
    assert json.dumps({'workspace':'unknown','tmpdir':'unknown'},sort_keys=True) in rendered
    assert 'not additional read or publication authority' in rendered
    for roots in [{'workspace':'supported'}, {'workspace':'allow_all','tmpdir':'supported'}]:
        with pytest.raises(ValueError):
            conversation_file_delivery_instructions(roots)


def test_delivery_tmp_fact_matches_real_process_tmpdir(tmp_path, monkeypatch):
    runtime = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path/'runtime'))
    worker = {'worker_id':'worker', 'owner_id':'owner', 'workspace_dir':str(tmp_path/'workspace'),
              'bootstrap_bundle_json':'{}', 'execution_mode':'host', 'profile':'codex-cli'}
    monkeypatch.setattr(profile_runtime, 'require_native_installed_owner', lambda _owner: None)
    actual_tmp = Path(runtime._host_env(worker)['TMPDIR'])
    observed = []
    monkeypatch.setattr(profile_runtime, 'is_protected_artifact_path', lambda path: observed.append(Path(path)) or Path(path)==actual_tmp)
    roots = runtime.provider_file_delivery_roots(worker)
    assert observed == [Path(worker['workspace_dir']), actual_tmp]
    assert roots == {'workspace':'supported','tmpdir':'local_only'}


@pytest.mark.parametrize('profile', ['codex-cli','claude-code'])
def test_host_conversation_gets_same_resolved_roots_before_start_and_each_turn(tmp_path, monkeypatch, profile):
    from test_conversation_provider import _client, _payload, AUTH
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest, GLASSHIVE_MODELS
    client = _client(tmp_path, monkeypatch)
    provider, service, store = client.app.state.conversation_provider, client.app.state.service, client.app.state.store
    workspace = tmp_path/'workspace'; workspace.mkdir()
    runtime = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path/'runtime'))
    monkeypatch.setattr(profile_runtime, 'is_protected_artifact_path', lambda path: Path(path)==workspace)
    monkeypatch.setattr(service.runtime, 'provider_file_delivery_roots', runtime.provider_file_delivery_roots, raising=False)
    payload = _payload(workspace, model='codex-cli:gpt-6.1-sol' if profile=='codex-cli' else 'claude-code:claude-opus-5-5')
    parsed = ChatCompletionRequest.model_validate(payload)
    model = GLASSHIVE_MODELS[parsed.model]
    before_start = []
    resume = service.resume_worker
    def observed_resume(worker_id):
        before_start.append(json.loads(store.get_worker(worker_id)['bootstrap_bundle_json'])['developer_instructions'])
        return resume(worker_id)
    monkeypatch.setattr(service, 'resume_worker', observed_resume)
    session = provider._create_native_session(parsed, model, workspace, 'high', tenant_id='local')
    expected = json.dumps({'workspace':'local_only','tmpdir':'supported'}, sort_keys=True)
    assert len(before_start)==1 and expected in before_start[0]
    run_bundle = provider._run_local_native_bundle(parsed, model, 'high', worker_id=session['worker_id'])
    assert expected in run_bundle['developer_instructions']
    assert run_bundle['developer_instructions'].count('Conversation file delivery:')==1
    payload['metadata'].update(message_id='resolved-roots-next', idempotency_key='resolved-roots-next')
    response = client.post('/v1/chat/completions', headers=AUTH, json=payload)
    assert response.status_code==200, response.text
    current = store.get_provider_session_by_id(session['session_id'])
    assert current['worker_id']==session['worker_id']
    assert expected in json.loads(store.get_worker(session['worker_id'])['bootstrap_bundle_json'])['developer_instructions']
