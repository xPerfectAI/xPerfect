"""Signed client source scopes reuse Native's existing retained-file publisher."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_coordinator_result_files import result_files
from test_coordinator_existing_worker import existing
from test_conversation_provider import lifecycle_owned_test_clients
from workers_projects_runtime.conversation_provider import (
    ChatCompletionRequest, CompletionMetadata, ConversationProvider, _authorized_result_files,
)
from workers_projects_runtime.service import NativeFilePublicationUnavailable


@pytest.fixture
def client_files(result_files):
    f = result_files
    # A client has no Native coordinator ledger. Exact native attempt scope remains required.
    fresh, _ = f.store.create_provider_request(tenant_id=f.request['tenant_id'],
        owner_id=f.request['owner_id'], session_id=f.request['session_id'],
        idempotency_key='client-only', message_id='client', stream_id='client-stream', requested_history_count=1)
    f.request = f.store.update_provider_request(fresh['request_id'], run_id=f.run['run_id'])
    f.source = f.store.update_run(f.source['run_id'], active_attempt_id='prior-attempt')
    manifest = f.runtime._run_root(f.worker['worker_id'], f.source['run_id'])/'native-image-scope.json'
    scope = json.loads(manifest.read_text())
    scope['attempt_id'] = 'prior-attempt'
    manifest.write_text(json.dumps(scope))
    f.grants = [{'run_id': f.source['run_id'], 'attempt_id': 'prior-attempt',
                 'sha256': hashlib.sha256(data).hexdigest()} for data in f.expected.values()]
    return f


def test_declared_prior_files_keep_exact_bytes_without_a_coordinator_or_new_run(client_files):
    f = client_files
    with f.store._connect() as conn:
        before = conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
    files = []
    rendered = f.service.render_provider_native_images(f.request, f.run, f.selected,
        output_files=files, authorized_result_files=f.grants)
    assert 'artifact_sha256:' not in rendered
    assert {item['filename'] for item in files} == set(f.expected)
    for item in files:
        assert item['sha256'] == hashlib.sha256(f.expected[item['filename']]).hexdigest()
        assert item['bytes'] == len(f.expected[item['filename']])
    with f.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == before
    assert f.service.render_provider_native_images(f.request, f.run, f.selected,
        authorized_result_files=f.grants) == rendered


@pytest.mark.parametrize('mode', ['undeclared', 'attempt', 'digest', 'owner', 'tenant', 'running', 'protected'])
def test_client_scope_never_grants_an_unowned_or_unproved_file(client_files, mode, monkeypatch):
    f = client_files
    grants = [dict(item) for item in f.grants]
    if mode == 'undeclared': grants = []
    elif mode == 'attempt':
        for grant in grants: grant['attempt_id'] = 'different-attempt'
    elif mode == 'digest':
        for grant in grants: grant['sha256'] = '0'*64
    elif mode in {'owner', 'tenant'}:
        with f.store._connect() as conn:
            conn.execute('UPDATE workers SET '+mode+'_id=? WHERE worker_id=?', ('foreign', f.worker['worker_id']))
    elif mode == 'running':
        with f.store._connect() as conn:
            conn.execute('UPDATE runs SET state=? WHERE run_id=?', ('running', f.source['run_id']))
    elif mode == 'protected':
        monkeypatch.setattr('workers_projects_runtime.service.is_protected_artifact_path', lambda _p: True)
    with pytest.raises(NativeFilePublicationUnavailable):
        f.service.render_provider_native_images(f.request, f.run, f.selected,
            authorized_result_files=grants)
    rejections = []
    degraded = f.service.render_provider_native_images(f.request, f.run, f.selected,
        authorized_result_files=grants, allow_unavailable_files=True, output_rejections=rejections)
    assert '[File unavailable]' in degraded
    assert rejections


def test_scope_shape_is_closed_and_completion_contract_retains_exact_declaration(client_files):
    f = client_files
    assert _authorized_result_files({'authorized_result_files': f.grants}) == f.grants
    for value in [None, {}, [{'run_id':'source','attempt_id':'attempt','sha256':'0'*64,'path':'invented'}]]:
        with pytest.raises(HTTPException):
            _authorized_result_files({'authorized_result_files': value})
    payload = ChatCompletionRequest(model='codex-cli:gpt-6.1-sol', messages=[{'role':'user','content':'Deliver the existing result.'}],
        metadata=CompletionMetadata(owner_id='owner', conversation_id='client', agent_id='main',
            bootstrap_bundle={'authorized_result_files': f.grants}))
    provider = ConversationProvider(f.store, f.service)
    contract = provider._completion_contract(payload)
    assert contract['authorized_result_files'] == f.grants
    saved = {'replay_decision_json': json.dumps({'completion_contract_v1': contract})}
    assert provider._saved_completion_contract(saved)['authorized_result_files'] == f.grants


@pytest.mark.parametrize('signed', [False, True])
def test_only_verified_bootstrap_can_declare_prior_file_access(tmp_path, monkeypatch, signed):
    import base64
    import hmac
    import time
    from test_conversation_provider import BOOTSTRAP_SIGNATURE_SECRET, _payload
    from test_conversation_provider_recovery import _post_bound
    from test_native_output_file_carrier import prepare

    client, _runtime, _data = prepare(tmp_path, monkeypatch, publish=False,
                                      authored_output='A normal reply.')
    grant = {'run_id': 'run-prior', 'attempt_id': 'attempt-prior', 'sha256': 'a'*64}
    bundle = {'authorized_result_files': [grant]}
    payload = _payload(tmp_path)
    headers = {}
    if signed:
        encoded = base64.b64encode(json.dumps(bundle).encode()).decode()
        issued = str(int(time.time()))
        signature = hmac.new(BOOTSTRAP_SIGNATURE_SECRET.encode(),
            f'v1\n{issued}\n{encoded}'.encode(), hashlib.sha256).hexdigest()
        headers = {'X-GlassHive-Bootstrap-Bundle-B64': encoded,
                   'X-GlassHive-Bootstrap-Timestamp': issued,
                   'X-GlassHive-Bootstrap-Signature': signature}
    else:
        payload['metadata']['bootstrap_bundle'] = bundle
    try:
        response, _digest = _post_bound(client, payload, headers=headers)
        assert response.status_code == 200, response.text
        record = client.app.state.store.get_provider_request(response.json()['id'])
        contract = ConversationProvider._saved_completion_contract(record)
        assert contract['authorized_result_files'] == ([grant] if signed else [])
        session = client.app.state.store.get_provider_session_by_id(record['session_id'])
        worker = client.app.state.store.get_worker(session['worker_id'])
        assert 'authorized_result_files' not in json.loads(worker['bootstrap_bundle_json'])
    finally:
        client.close()
