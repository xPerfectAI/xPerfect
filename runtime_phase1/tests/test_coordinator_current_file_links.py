"""Native Chat links reuse exact current workspace authority without fake captures."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_coordinator_existing_worker import existing, dispatch
from workers_projects_runtime import api
from workers_projects_runtime.coordinator import (
    CoordinatorScopeError, coordinator_current_result_files, project_coordinator_response,
)
from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.openclaw_runtime import StubRuntime


@pytest.fixture(params=['existing', 'helper'])
def current_file(existing, tmp_path, monkeypatch, request):
    core, cid, worker_id, _ = existing
    store, service = core.store, core.service
    run_id = dispatch(existing)['run_id']
    if request.param == 'helper':
        receipt = store.reserve_delegation(
            tenant_id='local', owner_id='owner', idempotency_key='current-file-helper', request_digest='digest',
            origin_ref=f'{cid}:followup', title='Arithmetic report', goal='Report', instruction='Report',
            origin_surface='coordinator', worker_name='Specialist', worker_role='worker', profile='codex-cli',
            backend='codex-cli', runtime='codex-cli', model='configured', execution_mode='docker', bootstrap_bundle={})
        worker_id, run_id = receipt['worker_id'], receipt['initial_run_id']
        with store._connect() as conn:
            conn.execute('UPDATE coordinator_goals SET worker_id=?,run_id=?,work_ref=? WHERE conversation_id=?',
                         (worker_id, run_id, receipt['work_ref'], cid))
    root = tmp_path / worker_id
    target = root / 'delivery' / 'arithmetic.md'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'# Arithmetic\n720 - 180 = 540\n')
    worker = store.update_worker(worker_id, workspace_dir=str(root), execution_mode='docker')
    native_root = '/workspace/data/members/20001/worktree'
    reference = native_root + '/delivery/arithmetic.md'
    output = f'Created [arithmetic.md]({reference}). Arithmetic verified: 540.'
    source = store.update_run(run_id, state='completed', output_text=output, ended_at='2026-01-01T00:00:00Z')
    service.runtime.native_workspace_placement = lambda w: {
        'workspace_id': w['workspace_id'], 'member_id': w['worker_id'],
        'workspace_dir': w['workspace_dir'], 'native_workspace': native_root}
    foreground = store.create_worker(worker['project_id'], 'owner', 'Assistant', 'Chat',
                                     'codex-cli', 'codex-cli', 'codex-cli', 'configured')
    store.update_worker(foreground['worker_id'], workspace_dir=str(tmp_path/'foreground'), trusted_run_lane='conversation')
    run = store.create_run(foreground['worker_id'], worker['project_id'], 'Combine results')
    run = store.update_run(run['run_id'], state='completed', output_text=output)
    session = store.upsert_provider_session(tenant_id='local', owner_id='owner', conversation_id=cid,
        agent_id='coordinator', model_id='configured', project_id=worker['project_id'], worker_id=foreground['worker_id'],
        workspace_dir=str(tmp_path/'foreground'), access_mode='full')
    record, _ = store.create_provider_request(tenant_id='local', owner_id='owner', session_id=session['session_id'],
        idempotency_key='combine-current', message_id='combine-current', stream_id='', requested_history_count=1)
    response = {'id': record['request_id'], 'choices':[{'message':{'role':'assistant','content':output}}]}
    saved = json.dumps(response, separators=(',', ':'))
    record = store.update_provider_request(record['request_id'], run_id=run['run_id'], state='completed', response_json=saved)
    with store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET request_id=?,response_json=? WHERE conversation_id=? AND turn_id=?',
                     (record['request_id'], saved, cid, 'turn'))
    monkeypatch.setenv('GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED', 'false')
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET', 'synthetic-current-file-secret')
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(tmp_path/'links.sqlite3'))
    monkeypatch.setenv('GLASSHIVE_ARTIFACT_BASE_URL', 'http://testserver')
    monkeypatch.setattr(api, 'load_viventium_runtime_env', lambda: None)
    yield SimpleNamespace(core=core, cid=cid, store=store, service=service, worker=worker, source=source,
        request=record, run=run, reference=reference, output=output, response=response, saved=saved,
        target=target, kind=request.param)


def projected(f):
    return project_coordinator_response(f.service, 'local', 'owner', f.request['request_id'], f.response)


def test_existing_saved_turn_and_provider_replay_resolve_exact_current_file(current_file):
    f = current_file
    assert f.service.files.current_artifact(f.worker, 'delivery/arithmetic.md')['filename'] == 'arithmetic.md'
    response = projected(f)
    text = response['choices'][0]['message']['content']
    assert f.reference not in text and '/v1/link-refs/' in text
    saved_turn = f.core.snapshot('local', 'owner', f.cid)['turns'][0]
    assert json.loads(saved_turn['response_json']) == response
    provider = ConversationProvider(f.store, f.service)
    assert provider.response_payload(f.request, f.run, None) == response
    with f.store._connect() as conn:
        assert conn.execute('SELECT response_json FROM coordinator_turns WHERE conversation_id=? AND turn_id=?',
                            (f.cid, 'turn')).fetchone()[0] == f.saved
    assert f.store.get_provider_request(f.request['request_id'])['response_json'] == f.saved
    assert f.store.get_run(f.source['run_id'])['output_text'] == f.output
    result = f.core.read_result('local', 'owner', f.cid, 'followup', max_chars=7)
    assert result['output_text'] == f.output[:7] and result['next_offset'] == 7
    assert result['sha256'] == hashlib.sha256(f.output.encode()).hexdigest()
    file = result['current_workspace_files'][0]
    assert file['semantics'] == 'current_workspace_file'
    assert set(file) == {'filename', 'open_url', 'download_url', 'semantics'}
    assert not any(key in file for key in ('sha256', 'native_paths', 'native_workspace_root'))
    app = api.create_app(db_path=str(f.store.db_path), runtime_backend='stub', runtime=StubRuntime(), reconcile_on_startup=False)
    try:
        client = TestClient(app)
        for action in ('open_url', 'download_url'):
            actual = client.get(file[action])
            assert actual.status_code == 200
            if action == 'download_url':
                assert actual.content == f.target.read_bytes()
                assert 'filename="arithmetic.md"' in actual.headers['content-disposition']
        # These links have current-file semantics, rather than invented old bytes.
        f.target.write_bytes(b'Updated current file\n')
        assert client.get(file['download_url']).content == b'Updated current file\n'
    finally:
        app.state.service.shutdown()
        app.state.store.close()


@pytest.mark.parametrize('fault', ['owner', 'tenant', 'project', 'unbound_run', 'unselected',
                                  'placement_member', 'placement_workspace', 'placement_root',
                                  'traversal', 'encoded_traversal', 'double_encoded_traversal',
                                  'double_encoded_space', 'symlink', 'missing', 'private', 'input'])
def test_current_file_links_never_expand_result_authority(current_file, fault, monkeypatch):
    f = current_file
    if fault in {'owner', 'tenant', 'project'}:
        key = {'owner':'owner_id', 'tenant':'tenant_id', 'project':'project_id'}[fault]
        value = (f.store.create_project('owner', 'Other', 'Other work', 'openclaw-general')['project_id']
                 if fault == 'project' else 'foreign')
        f.store.update_worker(f.worker['worker_id'], **{key:value})
    elif fault == 'unbound_run':
        with f.store._connect() as conn:
            conn.execute("UPDATE coordinator_goals SET run_id='' WHERE conversation_id=?", (f.cid,))
    elif fault == 'unselected':
        f.store.update_run(f.source['run_id'], output_text='No selected file')
    elif fault.startswith('placement_'):
        original = f.service.runtime.native_workspace_placement
        field = {'placement_member':'member_id', 'placement_workspace':'workspace_id', 'placement_root':'workspace_dir'}[fault]
        f.service.runtime.native_workspace_placement = lambda w: {**original(w), field:'foreign'}
    elif fault in {'traversal', 'encoded_traversal', 'double_encoded_traversal', 'double_encoded_space', 'private'}:
        tail = {'traversal':'../arithmetic.md', 'encoded_traversal':'%2e%2e/arithmetic.md',
                'double_encoded_traversal':'%252e%252e/arithmetic.md',
                'double_encoded_space':'report%2520one.md', 'private':'.codex/auth.json'}[fault]
        ref = f.reference.rsplit('/delivery/',1)[0] + '/' + tail
        f.output = f'Unsafe [file]({ref})'
        f.store.update_run(f.source['run_id'], output_text=f.output)
        f.response['choices'][0]['message']['content'] = f.output
    elif fault in {'symlink', 'missing'}:
        f.target.unlink()
        if fault == 'symlink':
            other = f.target.parent/'other.md'; other.write_text('private')
            f.target.symlink_to(other)
    else:
        monkeypatch.setattr('workers_projects_runtime.deliverables.is_unmodified_user_input', lambda *_a, **_k: True)
    assert projected(f) == f.response


@pytest.mark.parametrize('callbacks', [{'events_webhook_url':'https://example.test/events'}, 'malformed'])
def test_callback_mission_does_not_gain_current_file_fallback(current_file, callbacks):
    f = current_file
    bundle = json.loads(f.worker['bootstrap_bundle_json'] or '{}'); bundle['callbacks'] = callbacks
    f.store.update_worker(f.worker['worker_id'], bootstrap_bundle_json=json.dumps(bundle))
    assert projected(f) == f.response


def test_foreign_conversation_or_request_owner_cannot_resolve(current_file):
    f = current_file
    assert project_coordinator_response(f.service, 'local', 'other', f.request['request_id'], f.response) == f.response
    assert project_coordinator_response(f.service, 'other', 'owner', f.request['request_id'], f.response) == f.response
    with pytest.raises(CoordinatorScopeError):
        f.core.read_result('local', 'other', f.cid, 'followup')


def test_same_native_alias_from_two_workers_is_ambiguous(current_file):
    f = current_file
    receipt = f.store.reserve_delegation(
        tenant_id='local', owner_id='owner', idempotency_key='other-current-file', request_digest='digest',
        origin_ref=f'{f.cid}:other', title='Other report', goal='Other', instruction='Other',
        origin_surface='coordinator', worker_name='Second specialist', worker_role='worker', profile='codex-cli',
        backend='codex-cli', runtime='codex-cli', model='configured', execution_mode='docker', bootstrap_bundle={})
    other = f.store.get_worker(receipt['worker_id'])
    other_root = f.target.parents[1].parent / other['worker_id']
    (other_root/'delivery').mkdir(parents=True)
    (other_root/'delivery/arithmetic.md').write_text('Other file')
    f.store.update_worker(other['worker_id'], workspace_dir=str(other_root), execution_mode='docker')
    run = f.store.get_run(receipt['initial_run_id'])
    f.store.update_run(run['run_id'], state='completed', output_text=f.output)
    with f.store._connect() as conn:
        conn.execute("INSERT INTO coordinator_goals(conversation_id,goal_id,text,source_turn_id,worker_id,run_id,work_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (f.cid, 'other', 'Other accepted goal', 'turn', other['worker_id'], run['run_id'], receipt['work_ref'], '2026-01-01T00:00:00Z'))
    assert projected(f) == f.response


def test_result_notification_keeps_raw_text_hash_and_adds_current_actions(current_file):
    f = current_file
    f.core.refresh('local', 'owner', f.cid)
    turn_id = f.core.queue_result_turn('local', 'owner', f.cid)
    assert turn_id
    with f.store._connect() as conn:
        message = conn.execute('SELECT message FROM coordinator_turns WHERE conversation_id=? AND turn_id=?',
                                (f.cid, turn_id)).fetchone()[0]
    result = json.loads(message)['items'][0]['result']
    assert result['output_text'] == f.output
    assert result['sha256'] == hashlib.sha256(f.output.encode()).hexdigest()
    assert result['current_workspace_files'][0]['semantics'] == 'current_workspace_file'


@pytest.mark.parametrize('text', ['Hello.', '[site](https://example.test/report)',
    '[file](http://localhost/v1/link-refs/opaque)', '[file](artifact_sha256:'+'a'*64+')',
    '[file](file:///workspace/project/report.txt)'])
def test_plain_or_already_authorized_urls_do_not_open_ledger_or_file_store(text):
    response = {'choices':[{'message':{'content':text}}]}
    assert project_coordinator_response(object(), 'local', 'owner', 'request', response) is response


def test_exact_encoded_space_path_uses_current_scope_once(current_file):
    f = current_file
    target = f.target.with_name('report one.md')
    target.write_text('Space in exact native filename\n')
    reference = f.reference.rsplit('/', 1)[0] + '/report%20one.md'
    text = f'[report one.md]({reference})'
    f.store.update_run(f.source['run_id'], output_text=text)
    f.response['choices'][0]['message']['content'] = text
    assert '/v1/link-refs/' in projected(f)['choices'][0]['message']['content']


@pytest.mark.parametrize('fault', ['work_ref', 'origin_ref', 'origin_surface'])
@pytest.mark.parametrize('current_file', ['helper'], indirect=True)
def test_callback_free_label_alone_never_grants_a_helper_file(current_file, fault):
    f = current_file
    with f.store._connect() as conn:
        if fault == 'work_ref':
            conn.execute("UPDATE coordinator_goals SET work_ref='wrong' WHERE conversation_id=?", (f.cid,))
        else:
            conn.execute(f'UPDATE delegations SET {fault}=? WHERE worker_id=?', ('wrong', f.worker['worker_id']))
    assert projected(f) == f.response


def test_current_link_lookup_does_not_hash_file_contents(current_file, monkeypatch):
    f = current_file
    monkeypatch.setattr('workers_projects_runtime.workspace_files.os.read',
                        lambda *_a: pytest.fail('Current-file link lookup must not read file contents'))
    assert '/v1/link-refs/' in projected(f)['choices'][0]['message']['content']


def test_new_provider_output_uses_same_current_file_owner_without_capture_claim(current_file, monkeypatch):
    f = current_file
    provider = ConversationProvider(f.store, f.service)
    monkeypatch.setattr(provider, '_native_output_snapshot', lambda *_: f.output)
    captured = []
    output = provider._conversation_output(f.request, f.run, output_files=captured)
    assert '/v1/link-refs/' in output and f.reference not in output
    assert captured == []
    assert f.store.get_run(f.source['run_id'])['output_text'] == f.output
