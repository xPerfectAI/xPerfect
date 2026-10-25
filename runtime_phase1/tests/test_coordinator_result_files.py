"""Chat publishes selected files through exact work receipts, never shared ownership."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_coordinator_existing_worker import existing, dispatch
from test_account_api import _truthfully_invoke_run
from test_native_image_output import fixture as native_fixture, project
from workers_projects_runtime import api, profile_runtime
from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, StubRuntime


@pytest.fixture(params=['existing', 'helper'])
def result_files(existing, tmp_path, monkeypatch, request):
    core, cid, worker_id, _ = existing
    store, service = core.store, core.service
    run_id = dispatch(existing)['run_id']
    if request.param == 'helper':
        receipt = store.reserve_delegation(
            tenant_id='local', owner_id='owner', idempotency_key='file-helper', request_digest='digest',
            origin_ref='file-helper-origin', title='Selected report', goal='Report', instruction='Report',
            origin_surface='coordinator', worker_name='Helper', worker_role='worker', profile='codex-cli',
            backend='codex-cli', runtime='codex-cli', model='configured', execution_mode='host',
            bootstrap_bundle={'callbacks': {'events_webhook_url': 'https://example.test/worker-events'}})
        worker_id, run_id = receipt['worker_id'], receipt['initial_run_id']
        with store._connect() as conn:
            conn.execute('UPDATE coordinator_goals SET worker_id=?,run_id=?,work_ref=? WHERE conversation_id=?',
                         (worker_id, run_id, receipt['work_ref'], cid))
        _truthfully_invoke_run(store, run_id, suffix='coordinator-result-helper')
    runtime, _, info, scope, _ = native_fixture(tmp_path/'native')
    worker = store.update_worker(worker_id, workspace_dir=info.workspace_dir)
    attempt_id = str((store.get_run(run_id) or {}).get('active_attempt_id') or '')
    scope.update(worker_id=worker_id, run_id=run_id, owner_id='owner', images=[],
                 file_output_transport='artifact_sha256', attempt_id=attempt_id)
    expected = {'report.csv': b'item,value\nExample,17\n', 'report.html': b'<!doctype html><h1>Report</h1>'}
    for name, data in expected.items():
        (Path(info.workspace_dir)/name).write_bytes(data)
    selected = project(runtime, {**worker, '_run_attempt_id': attempt_id}, info,
                       '\n'.join(f'[{name}]({name})' for name in expected), scope, run_id)
    source = store.update_run(run_id, state='completed', output_text=selected, ended_at='2026-01-01T00:00:00Z')
    foreground = store.create_worker(worker['project_id'], 'owner', 'Assistant', 'Chat',
                                     'codex-cli', 'codex-cli', 'codex-cli', 'configured')
    store.update_worker(foreground['worker_id'], workspace_dir=str(tmp_path/'foreground'), trusted_run_lane='conversation')
    current = store.create_run(foreground['worker_id'], worker['project_id'], 'Combine results')
    current = store.update_run(current['run_id'], state='completed', output_text=selected)
    session = store.upsert_provider_session(tenant_id='local', owner_id='owner', conversation_id=cid,
        agent_id='coordinator', model_id='configured', project_id=worker['project_id'], worker_id=foreground['worker_id'],
        workspace_dir=str(tmp_path/'foreground'), access_mode='full')
    provider_request, _ = store.create_provider_request(tenant_id='local', owner_id='owner', session_id=session['session_id'],
        idempotency_key='combine', message_id='combine', stream_id='', requested_history_count=1)
    provider_request = store.update_provider_request(provider_request['request_id'], run_id=current['run_id'], state='failed')
    with store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET request_id=? WHERE conversation_id=? AND turn_id=?',
                     (provider_request['request_id'], cid, 'turn'))
    router = object.__new__(profile_runtime.ProfiledWorkerRuntime)
    router._runtime_for_worker = lambda _w: runtime
    router._native_warm_adapters = lambda: ()
    service.runtime = router
    monkeypatch.setenv('GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED', 'false')
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET', 'synthetic-result-link-secret')
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(tmp_path/'links.sqlite3'))
    monkeypatch.setenv('GLASSHIVE_ARTIFACT_BASE_URL', 'http://testserver')
    monkeypatch.setattr(api, 'load_viventium_runtime_env', lambda: None)
    if request.param == 'helper':
        # A delegated result's friendly download name needs the same immutable
        # completion envelope as production; raw completed rows are not authority.
        callback = service._emit_callback(worker, 'run.completed', run=source,
                                          submit_delivery=False)
        assert callback is not None
        envelope = json.loads(callback['payload_json'])['output_files']
        assert {item['filename'] for item in envelope['files']} == set(expected)
    yield SimpleNamespace(core=core, cid=cid, store=store, service=service, runtime=runtime,
        source=source, worker=worker, request=provider_request, run=current, selected=selected,
        expected=expected, kind=request.param)


def test_exact_files_publish_and_replay_without_native_work(result_files, monkeypatch):
    f = result_files
    provider = ConversationProvider(f.store, f.service)
    monkeypatch.setattr(provider, '_native_output_snapshot', lambda *_: f.selected)
    monkeypatch.setattr(provider, '_native_citation_sources_snapshot', lambda *_: [])
    files = []
    with f.store._connect() as conn:
        before = conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
    rendered = provider._conversation_output(f.request, f.run, output_files=files)
    assert 'artifact_sha256:' not in rendered
    assert {item['filename'] for item in files} == set(f.expected)
    assert provider._conversation_output(f.request, f.run) == rendered
    app = api.create_app(db_path=str(f.store.db_path), runtime_backend='stub', runtime=StubRuntime(), reconcile_on_startup=False)
    try:
        # The real API uses the same native resolver as the publisher.
        app.state.service.runtime = f.service.runtime
        client = TestClient(app)
        for item in files:
            response = client.get(item['download_url'], follow_redirects=False)
            data = f.expected[item['filename']]
            assert response.status_code == 200 and response.content == data
            assert f'filename="{item["filename"]}"' in response.headers['content-disposition']
            assert item['sha256'] == hashlib.sha256(data).hexdigest()
            assert (Path(f.worker['workspace_dir'])/'artifacts/native-media'/f.source['run_id']).is_dir()
        with f.store._connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == before
    finally:
        app.state.service.shutdown()
        app.state.store.close()


@pytest.mark.parametrize('change', ['owner', 'tenant', 'request', 'request_run', 'other_conversation',
    'unbound_run', 'nonterminal', 'stale_attempt', 'missing_bytes', 'unselected_hash', 'source_owner', 'source_tenant'])
def test_file_authority_refuses_unbound_or_unavailable_sources(result_files, change):
    f = result_files
    request, output = dict(f.request), f.selected
    if change == 'owner': request['owner_id'] = 'foreign'
    elif change == 'tenant': request['tenant_id'] = 'foreign'
    elif change == 'request': request['request_id'] = 'other-request'
    elif change == 'request_run': f.store.update_provider_request(request['request_id'], run_id=f.source['run_id'])
    elif change == 'other_conversation':
        with f.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET request_id='' WHERE conversation_id=?", (f.cid,))
    elif change == 'unbound_run':
        with f.store._connect() as conn:
            conn.execute("UPDATE coordinator_goals SET run_id='' WHERE conversation_id=?", (f.cid,))
    elif change == 'nonterminal':
        with f.store._connect() as conn:
            conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (f.source['run_id'],))
    elif change == 'stale_attempt': f.store.update_run(f.source['run_id'], active_attempt_id='later-attempt')
    elif change == 'source_owner': f.store.update_worker(f.worker['worker_id'], owner_id='foreign')
    elif change == 'source_tenant': f.store.update_worker(f.worker['worker_id'], tenant_id='foreign')
    elif change == 'unselected_hash': output = '[Unknown](artifact_sha256:' + '0'*64 + ')'
    elif change == 'missing_bytes':
        _, captured = f.runtime.provider_native_image_output(f.worker, f.source, '')
        for _, _, snapshot in captured:
            (snapshot.root/snapshot.relative).unlink()
    with pytest.raises(RuntimeErrorBase, match='unavailable|does not match'):
        f.service.render_provider_native_images(request, f.run, output)


def test_source_project_change_does_not_grant_result_files(result_files):
    f = result_files
    other = f.store.create_project('owner', 'Other', 'Different project', 'openclaw-general')
    f.store.update_worker(f.worker['worker_id'], project_id=other['project_id'])
    with pytest.raises(RuntimeErrorBase, match='unavailable'):
        f.service.render_provider_native_images(f.request, f.run, f.selected)


def test_recorded_queue_result_keeps_exact_source_file_authority(result_files):
    f = result_files
    replacement = f.store.create_run(f.worker['worker_id'], f.worker['project_id'], 'Later work')
    with f.store._connect() as conn:
        conn.execute('UPDATE coordinator_goals SET run_id=? WHERE conversation_id=?', (replacement['run_id'], f.cid))
        conn.execute('INSERT INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json,response_json) VALUES(?,?,?,?,?)',
            (f.cid,'followup','queue-file',json.dumps({'action':'queue','run_id':f.source['run_id']}),json.dumps({'run_id':replacement['run_id']})))
    assert 'artifact_sha256:' not in f.service.render_provider_native_images(f.request, f.run, f.selected)
    with f.store._connect() as conn:
        conn.execute("UPDATE coordinator_actions SET response_json='' WHERE conversation_id=?", (f.cid,))
    with pytest.raises(RuntimeErrorBase, match='unavailable'):
        f.service.render_provider_native_images(f.request, f.run, f.selected)


def prepare_legacy_identity(f, monkeypatch):
    provider = ConversationProvider(f.store, f.service)
    f.core.provider = provider
    contract = {'version': 1, 'model': 'codex-cli:gpt-6.1-sol', 'prompt_tokens': 1}
    metadata = {'tenant_id': 'local', 'owner_id': 'owner', 'conversation_id': f.cid,
                'agent_id': 'coordinator', 'message_id': 'turn'}
    f.request = f.store.update_provider_request(f.request['request_id'], message_id='turn', state='failed',
        replay_decision_json=json.dumps({'completion_contract_v1': contract}))
    f.run = f.store.update_run(f.run['run_id'], failure_class='native_output_files_identity_unavailable',
                              failure_structured=1, failure_user_message='Files could not be bound')
    with f.store._connect() as conn:
        conn.execute('UPDATE coordinator_turns SET payload_json=?,blocker=? WHERE conversation_id=? AND turn_id=?',
            (json.dumps({'model': contract['model'], 'messages': [{'role': 'user', 'content': 'Combine'}],
                         'metadata': metadata}), 'failed', f.cid, 'turn'))
    monkeypatch.setattr(provider, '_native_output_snapshot', lambda *_: f.selected)
    monkeypatch.setattr(provider, '_native_terminal_output_snapshot', lambda *_: f.selected)
    monkeypatch.setattr(provider, '_native_citation_sources_snapshot', lambda *_: [])
    monkeypatch.setattr(provider, '_native_effective_model', lambda _request, _run, contract: contract['model'])
    def no_native(*_a, **_kw): raise AssertionError('Projection repair must not invoke the model')
    monkeypatch.setattr(provider, 'start', no_native)
    monkeypatch.setattr(f.service, 'start_assigned_run', no_native)
    return provider


def test_legacy_missing_identity_recovers_saved_completed_reply_and_history_once(result_files, monkeypatch):
    f = result_files
    provider = prepare_legacy_identity(f, monkeypatch)
    with f.store._connect() as conn:
        before = conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
    snapshot = f.core.refresh('local', 'owner', f.cid)
    record = f.store.get_provider_request(f.request['request_id'])
    assert record['state'] == 'completed' and record['stream_id'] == 'turn', (record['state'], record['stream_id'], f.store.get_run(f.run['run_id'])['failure_class'])
    response = json.loads(record['response_json'])
    envelope = response['glasshive']['output_files']
    assert envelope['message_id'] == envelope['stream_id'] == 'turn'
    assert envelope['request_id'] == f.request['request_id'] and envelope['run_id'] == f.run['run_id']
    assert {item['filename'] for item in envelope['files']} == set(f.expected)
    assert f.store.get_provider_session_by_id(record['session_id'])['history_count'] == 2
    assert f.store.get_run(f.run['run_id'])['state'] == 'completed'
    assert f.store.get_run(f.run['run_id'])['failure_class'] == ''
    saved = next(turn for turn in snapshot['turns'] if turn['turn_id'] == 'turn')['response_json']
    assert saved and not json.loads(saved)['choices'][0]['message']['content'].count('artifact_sha256:')
    second = f.core.refresh('local', 'owner', f.cid)
    assert next(t for t in second['turns'] if t['turn_id'] == 'turn')['response_json'] == saved
    assert f.store.get_provider_request(record['request_id'])['response_json'] == record['response_json']
    with f.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == before
    provider.shutdown()


@pytest.mark.parametrize('change', ['cancelled', 'other_failure', 'nonterminal', 'existing_identity',
    'session_owner', 'session_tenant', 'message', 'payload_owner', 'fallback', 'restore_hold',
    'missing_contract', 'missing_terminal', 'cancel_race', 'unstructured'])
def test_identity_repair_cannot_reopen_unrelated_or_cancelled_work(result_files, monkeypatch, change):
    f = result_files
    provider = prepare_legacy_identity(f, monkeypatch)
    if change == 'cancelled': f.store.update_provider_request(f.request['request_id'], state='cancelled')
    elif change == 'other_failure': f.store.update_run(f.run['run_id'], failure_class='provider_auth_missing')
    elif change == 'unstructured': f.store.update_run(f.run['run_id'], failure_structured=0)
    elif change == 'nonterminal':
        with f.store._connect() as conn:
            conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (f.run['run_id'],))
    elif change == 'existing_identity': f.store.update_provider_request(f.request['request_id'], stream_id='accepted-original')
    elif change in {'session_owner', 'session_tenant'}:
        column = 'owner_id' if change == 'session_owner' else 'tenant_id'
        with f.store._connect() as conn:
            conn.execute(f'UPDATE provider_sessions SET {column}=? WHERE session_id=?', ('foreign', f.request['session_id']))
    elif change == 'message': f.store.update_provider_request(f.request['request_id'], message_id='another-turn')
    elif change == 'payload_owner':
        with f.store._connect() as conn:
            conn.execute("UPDATE coordinator_turns SET payload_json=json_set(payload_json,'$.metadata.owner_id','foreign') WHERE conversation_id=?", (f.cid,))
    elif change == 'fallback': f.store.update_provider_request(f.request['request_id'], fallback_state='claimed')
    elif change == 'restore_hold': f.store.update_provider_request(f.request['request_id'], restore_hold=1)
    elif change == 'missing_contract': f.store.update_provider_request(f.request['request_id'], replay_decision_json='{}')
    elif change == 'missing_terminal': monkeypatch.setattr(provider, '_native_terminal_output_snapshot', lambda *_: None)
    elif change == 'cancel_race':
        build = provider._build_canonical_response
        def cancelled_after_projection(*args):
            response = build(*args)
            f.store.update_provider_request(f.request['request_id'], state='cancelled')
            return response
        monkeypatch.setattr(provider, '_build_canonical_response', cancelled_after_projection)
    record = f.store.get_provider_request(f.request['request_id'])
    repaired = f.core._repair_turn_output_identity('local', 'owner', f.cid, 'turn', record)
    assert repaired['state'] == ('cancelled' if change in {'cancelled', 'cancel_race'} else 'failed')
    assert repaired['stream_id'] == ('accepted-original' if change == 'existing_identity' else '')
    assert repaired['response_json'] == ''
    provider.shutdown()


@pytest.mark.parametrize('legacy_identity', [False, True])
@pytest.mark.parametrize('failure', ['unowned', 'malformed', 'missing_bytes'])
def test_unavailable_file_fails_one_reply_without_blocking_other_goals(result_files, monkeypatch, legacy_identity, failure):
    from workers_projects_runtime.coordinator import Goal
    f = result_files
    provider = prepare_legacy_identity(f, monkeypatch)
    if not legacy_identity:
        f.store.update_provider_request(f.request['request_id'], state='running', stream_id='turn')
        f.store.update_run(f.run['run_id'], failure_class='', failure_structured=0)
    bad_output = f.selected
    if failure == 'unowned': bad_output = '[Unavailable](artifact_sha256:' + 'f'*64 + ')'
    elif failure == 'malformed': bad_output = '[Unavailable](artifact_sha256:not-a-file-id)'
    else:
        _, captured = f.runtime.provider_native_image_output(f.worker, f.source, '')
        for _, _, snapshot in captured: (snapshot.root/snapshot.relative).unlink()
    monkeypatch.setattr(provider, '_native_output_snapshot', lambda *_: bad_output)
    monkeypatch.setattr(provider, '_native_terminal_output_snapshot', lambda *_: bad_output)
    f.core.accept_goals('local', 'owner', f.cid, 'turn', [Goal(id='independent', text='Independent result')])
    other = f.store.create_run(f.worker['worker_id'], f.worker['project_id'], 'Independent result')
    with f.store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='independent'",
                     (f.worker['worker_id'], other['run_id'], f.cid))
    first = f.core.refresh('local', 'owner', f.cid)
    record = f.store.get_provider_request(f.request['request_id'])
    assert record['state'] == 'failed' and record['response_json'] == ''
    assert f.store.get_run(f.run['run_id'])['failure_class'] == 'native_output_files_identity_unavailable'
    assert next(t for t in first['turns'] if t['turn_id'] == 'turn')['blocker'] == 'failed'
    assert next(g for g in first['goals'] if g['goal_id'] == 'independent')['state'] == 'queued'
    f.store.update_run(other['run_id'], state='completed', output_text='Independent result is ready')
    second = f.core.refresh('local', 'owner', f.cid)
    assert next(g for g in second['goals'] if g['goal_id'] == 'independent')['state'] == 'completed'
    assert f.core.read_result('local', 'owner', f.cid, 'independent')['output_text'] == 'Independent result is ready'
    assert f.store.get_provider_request(f.request['request_id'])['response_json'] == ''
    provider.shutdown()


def test_canonical_file_handling_does_not_catch_unrelated_runtime_errors(result_files, monkeypatch):
    f = result_files
    provider = prepare_legacy_identity(f, monkeypatch)
    def unrelated(*_a, **_kw): raise RuntimeErrorBase('Unrelated runtime defect')
    monkeypatch.setattr(provider, '_conversation_output', unrelated)
    with pytest.raises(RuntimeErrorBase, match='Unrelated runtime defect'):
        provider._build_canonical_response(f.request, f.run, provider._saved_completion_contract(f.request))
    provider.shutdown()


@pytest.mark.parametrize('change', ['stale_attempt', 'other_worker_run', 'ambiguous_names', 'invalid_name', 'unavailable'])
def test_download_name_falls_back_without_changing_file_authority(result_files, monkeypatch, change):
    f = result_files
    files = []
    f.service.render_provider_native_images(f.request, f.run, f.selected, output_files=files)
    selected = files[0]
    _text, native = f.runtime.provider_native_image_output(f.worker, f.source, '')
    app = api.create_app(db_path=str(f.store.db_path), runtime_backend='stub', runtime=StubRuntime(), reconcile_on_startup=False)
    app.state.service.runtime = f.service.runtime
    try:
        if change == 'stale_attempt': f.store.update_run(f.source['run_id'], active_attempt_id='stale')
        elif change == 'other_worker_run': f.store.update_run(f.source['run_id'], worker_id=f.run['worker_id'])
        elif change == 'unavailable':
            def unavailable(*_args): raise RuntimeErrorBase('Native metadata unavailable')
            monkeypatch.setattr(f.runtime, 'provider_native_image_output', unavailable)
        else:
            path, mime, snapshot = next(item for item in native if item[2].sha256 == selected['sha256'])
            renamed = path.with_name('second-name' + path.suffix if change == 'ambiguous_names' else 'invalid\nname' + path.suffix)
            choices = [(path, mime, snapshot), (renamed, mime, snapshot)] if change == 'ambiguous_names' else [(renamed, mime, snapshot)]
            monkeypatch.setattr(f.runtime, 'provider_native_image_output', lambda *_: ('', choices))
        if f.kind == 'helper' and change in {'ambiguous_names', 'invalid_name', 'unavailable'}:
            # Delegated names come from the sealed completion, not a later
            # mutable native lookup. Corrupt that owning boundary in these cases.
            original = app.state.store.list_callback_outbox_for_run
            def changed_callbacks(*args, **kwargs):
                if change == 'unavailable':
                    return []
                records = []
                for record in original(*args, **kwargs):
                    payload = json.loads(record['payload_json'])
                    for item in list(payload.get('output_files', {}).get('files', [])):
                        if item['sha256'] != selected['sha256']:
                            continue
                        if change == 'ambiguous_names':
                            payload['output_files']['files'].append({**item, 'filename': 'second-name.csv'})
                        else:
                            item['filename'] = 'invalid\nname.csv'
                    records.append({**record, 'payload_json': json.dumps(payload)})
                return records
            monkeypatch.setattr(app.state.store, 'list_callback_outbox_for_run', changed_callbacks)
        response = TestClient(app).get(selected['download_url'])
        assert response.status_code == 200
        assert response.content == f.expected[selected['filename']]
        assert f'filename="{selected["sha256"]}' in response.headers['content-disposition']
    finally:
        app.state.service.shutdown()
        app.state.store.close()
