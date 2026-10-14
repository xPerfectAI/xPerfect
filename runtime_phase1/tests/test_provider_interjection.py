"""Owner-scoped context enters the admitted Native request, never a new run."""
from __future__ import annotations

import hashlib
import json
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.conversation_provider import ChatCompletionRequest
from workers_projects_runtime.grok_control import ControlMailbox, read_message, submit_control
from workers_projects_runtime.grok_runtime import GrokBuildRuntime
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.store import RunRestorationState

AUTH = {'Authorization': 'Bearer synthetic-provider-token', 'X-Viventium-User-Id': 'owner-a'}
TEXT = 'Completed specialist context, with its full evidence.'
DIGEST = hashlib.sha256(TEXT.encode()).hexdigest()


class NativeControlRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.controls = []
        self.result = {'status': 'queued'}

    def native_control(self, worker, **kwargs):
        self.controls.append((worker, kwargs))
        return self.result


@pytest.fixture
def owned_request(tmp_path, monkeypatch):
    monkeypatch.setenv('WPR_API_TOKEN', 'synthetic-admin-token')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_API_KEY', 'synthetic-provider-token')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_PRINCIPAL_ID', 'owner-a')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_TENANT_ID', 'local')
    monkeypatch.setenv('GLASSHIVE_PROVIDER_TRUST_IDENTITY_HEADERS', '1')
    runtime = NativeControlRuntime()
    app = create_app(str(tmp_path / 'runtime.db'), runtime_backend='stub', runtime=runtime,
                     reconcile_on_startup=False)
    store = app.state.store
    project = store.create_project('owner-a', 'Synthetic live context', 'Exact owner', 'codex-cli')
    worker = store.create_worker(project_id=project['project_id'], owner_id='owner-a',
        name='Synthetic worker', role='conversation-agent', profile='codex-cli',
        backend='', runtime='codex-cli', model='synthetic-model')
    session = store.upsert_provider_session(tenant_id='local', owner_id='owner-a',
        conversation_id='conversation-a', agent_id='agent-a', model_id='codex-cli:synthetic-model',
        project_id=project['project_id'], worker_id=worker['worker_id'],
        workspace_dir=str(tmp_path), access_mode='workspace')
    run = store.create_run(worker['worker_id'], project['project_id'], 'Exact original input',
                           state=RunRestorationState.RUNNING)
    record, _ = store.create_provider_request(tenant_id='local', owner_id='owner-a',
        session_id=session['session_id'], idempotency_key='turn-a', message_id='message-a',
        stream_id='stream-a', native_invocation_id='invocation-a', native_body_sha256='a' * 64,
        requested_history_count=1, replay_decision={'request_authority_sha256': 'b' * 64,
            'completion_contract_v1': app.state.conversation_provider._completion_contract(
                ChatCompletionRequest(model='codex-cli:synthetic-model',
                    messages=[{'role': 'user', 'content': 'Synthetic original input.'}],
                    metadata={'audio_eligible': True}))})
    record = store.update_provider_request(record['request_id'], state='running', run_id=run['run_id'])
    monkeypatch.setattr(app.state.service, '_ensure_execution_allowed', lambda _: None)
    monkeypatch.setattr(app.state.conversation_provider, '_sync', lambda *_: pytest.fail('interject may not sync'))
    client = TestClient(app)
    body = {'stream_id': 'stream-a', 'message_id': 'message-a', 'body_sha256': 'a' * 64,
            'delivery_id': 'c' * 64, 'text': TEXT, 'text_sha256': DIGEST}
    yield SimpleNamespace(client=client, app=app, store=store, runtime=runtime, body=body,
        record=record, session=session, worker=worker, run=run)
    app.state.conversation_provider.shutdown()
    app.state.service.shutdown()
    store.close()


def post(fixture, body=None, headers=None):
    return fixture.client.post('/v1/requests/by-invocation/invocation-a/interject',
        json=fixture.body if body is None else body, headers=AUTH if headers is None else headers)


def test_exact_owned_running_request_queues_context_without_new_run(owned_request):
    f = owned_request
    before = f.store.get_provider_request(f.record['request_id'])
    response = post(f)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result == {'version': 1, 'status': 'queued', 'invocation_id': 'invocation-a',
        'delivery_id': 'c' * 64, 'text_sha256': DIGEST, 'run_id': f.run['run_id'],
        'attempt_id': f.run['active_attempt_id'],
        'message_id': f.runtime.controls[0][1]['payload']['message_id']}
    assert f.store.get_provider_request(f.record['request_id']) == before
    assert f.runtime.controls[0][1]['action'] == 'interject'
    assert f.runtime.controls[0][1]['payload']['text'] == TEXT
    assert len(f.store.list_nonterminal_runs_for_worker(f.worker['worker_id'])) == 1
    assert 'consumed' not in result and 'presented' not in result


@pytest.mark.parametrize('case', ['absent', 'freeform', 'graph_only', 'invalid_version', 'ineligible', 'malformed_delivery'])
def test_automatic_unframed_cohort_is_unsupported_without_altering_main(owned_request, case):
    f = owned_request
    decision = json.loads(f.store.get_provider_request(f.record['request_id'])['replay_decision_json'])
    contract = decision['completion_contract_v1']
    if case == 'absent':
        decision.pop('completion_contract_v1')
    elif case == 'invalid_version':
        contract['version'] = 0
    elif case == 'ineligible':
        contract['delivery_control'] = {'version': 1, 'audio_eligible': False}
    elif case == 'malformed_delivery':
        contract['delivery_control'] = {'version': 2, 'audio_eligible': True}
    else:
        contract['delivery_control'] = None
        if case == 'graph_only':
            contract['graph_control'] = {'version': 1, 'tools': [
                {'name': 'lc_transfer_to_specialist', 'description': 'Declared transfer.'}]}
    f.store.update_provider_request(f.record['request_id'], replay_decision_json=json.dumps(decision))
    before = f.store.get_provider_request(f.record['request_id'])
    run_before = f.store.get_run(f.run['run_id'])
    result = post(f)
    assert result.status_code == 200, result.text
    assert result.json() == {'version': 1, 'status': 'unsupported', 'invocation_id': 'invocation-a',
        'delivery_id': 'c' * 64, 'text_sha256': DIGEST}
    assert not f.runtime.controls
    assert f.store.get_provider_request(f.record['request_id']) == before
    assert f.store.get_run(f.run['run_id']) == run_before
    assert len(f.store.list_nonterminal_runs_for_worker(f.worker['worker_id'])) == 1
    assert post(f).json() == result.json()  # Unsupported retry is observational, not a new invocation.


def test_automatic_unframed_decline_preserves_ordinary_worker_interject(owned_request):
    f = owned_request
    decision = json.loads(f.store.get_provider_request(f.record['request_id'])['replay_decision_json'])
    decision['completion_contract_v1']['delivery_control'] = None
    f.store.update_provider_request(f.record['request_id'], replay_decision_json=json.dumps(decision))
    assert post(f).json()['status'] == 'unsupported'
    result = f.app.state.service.native_worker_control(f.worker['worker_id'],
        run_id=f.run['run_id'], attempt_id=f.run['active_attempt_id'], action='interject',
        payload={'text': 'Operator owned context.'})
    assert result['status'] == 'queued'
    assert len(f.runtime.controls) == 1
    assert f.runtime.controls[0][1]['payload'] == {'text': 'Operator owned context.'}
    assert f.store.get_run(f.run['run_id'])['state'] == 'running'


@pytest.mark.parametrize('case', ['matched', 'no_capability', 'wrong_graph', 'freeform', 'malformed_graph', 'stale_state', 'invalid_delivery'])
def test_automatic_graph_input_requires_matching_live_native_output_owner(owned_request, case, monkeypatch):
    f = owned_request
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    decision = json.loads(f.store.get_provider_request(f.record['request_id'])['replay_decision_json'])
    contract = decision['completion_contract_v1']
    contract['delivery_control'] = {'version': 2, 'audio_eligible': True} if case == 'invalid_delivery' else None
    contract['graph_control'] = None if case == 'freeform' else {'version': 0, 'tools': graph['tools']} if case == 'malformed_graph' else graph
    f.store.update_provider_request(f.record['request_id'], replay_decision_json=json.dumps(decision))
    monkeypatch.setattr(f.runtime, 'supports_native_control', lambda worker, action, **kwargs:
        action == 'interject' or (action == 'interject_graph_output' and case != 'no_capability'), raising=False)
    native_graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_other', 'description': 'Other.'}]} if case == 'wrong_graph' else graph
    monkeypatch.setattr(f.runtime, 'native_control_state', lambda worker, **kwargs: {
        'run_id': f.run['run_id'], 'attempt_id': 'foreign' if case == 'stale_state' else f.run['active_attempt_id'],
        'interject_graph_control': native_graph}, raising=False)
    before = f.store.get_provider_request(f.record['request_id'])
    result = post(f)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == ('queued' if case == 'matched' else 'unsupported')
    assert len(f.runtime.controls) == (1 if case == 'matched' else 0)
    assert f.store.get_provider_request(f.record['request_id']) == before
    assert len(f.store.list_nonterminal_runs_for_worker(f.worker['worker_id'])) == 1


def test_codex_structured_items_preserve_latest_public_graph_result_without_control_leak():
    from workers_projects_runtime.conversation_provider import _native_authored_preview, _native_terminal_text
    from workers_projects_runtime.agent_builder_control import parse_conversation_output
    graph = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult.'}]}
    old = {'type': 'assistant_response', 'tool_name': None, 'content': 'Earlier complete response.'}
    revised = {**old, 'content': 'Useful newly supplied worker evidence.'}
    events = [{'type': 'turn.started'}]
    for identity, value in [('item-old', old), ('item-revised', revised)]:
        events.extend([{'type': 'item.started', 'item': {'type': 'agent_message', 'id': identity}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'id': identity, 'text': json.dumps(value)}}])
    stdout = '\n'.join(json.dumps(event) for event in events)
    assert _native_terminal_text('codex-cli', stdout) is None
    preview = _native_authored_preview('codex-cli', stdout, graph, None)
    assert preview['text'] == revised['content']
    terminal = _native_terminal_text('codex-cli', stdout + '\n' + json.dumps({'type': 'turn.completed'}))
    decision = parse_conversation_output(terminal, graph, None)
    assert decision['content'] == revised['content']
    assert 'delivery_disposition' not in decision
    assert 'tool_name' not in preview['text'] and 'assistant_response' not in preview['text']


@pytest.mark.parametrize('key,value', [('stream_id', 'other'), ('message_id', 'other'),
                                      ('body_sha256', 'd' * 64)])
def test_wrong_admitted_identity_refuses_without_native_call(owned_request, key, value):
    response = post(owned_request, {**owned_request.body, key: value})
    assert response.status_code == 409
    assert not owned_request.runtime.controls


@pytest.mark.parametrize('headers,status', [({}, 401),
    ({'Authorization': 'Bearer synthetic-provider-token', 'X-Viventium-User-Id': 'foreign-owner'}, 404)])
def test_foreign_or_unauthenticated_request_cannot_interject(owned_request, headers, status):
    assert post(owned_request, headers=headers).status_code == status
    assert not owned_request.runtime.controls


@pytest.mark.parametrize('patch', [{'text_sha256': 'd' * 64}, {'delivery_id': 'invalid'},
    {'run_id': 'forged'}, {'text': ''}, {'text': 'x' * 65537}])
def test_invalid_control_shape_refuses_before_native_call(owned_request, patch):
    assert post(owned_request, {**owned_request.body, **patch}).status_code == 400
    assert not owned_request.runtime.controls


@pytest.mark.parametrize('state', ['queued', 'completed', 'cancelled', 'failed'])
def test_nonrunning_request_is_typed_without_dispatch(owned_request, state):
    f = owned_request
    f.store.update_provider_request(f.record['request_id'], state=state)
    assert post(f).json()['status'] == 'not_running'
    assert not f.runtime.controls


def test_newer_session_request_prevents_old_turn_injection(owned_request):
    f = owned_request
    f.store.create_provider_request(tenant_id='local', owner_id='owner-a',
        session_id=f.session['session_id'], idempotency_key='turn-b', message_id='message-b',
        stream_id='stream-b', requested_history_count=2)
    assert post(f).json()['status'] == 'not_running'
    assert not f.runtime.controls


def test_unsupported_runtime_is_typed_and_does_not_replace_main(owned_request):
    f = owned_request
    f.app.state.service.runtime = StubRuntime()
    assert post(f).json()['status'] == 'unsupported'
    assert not f.runtime.controls


def test_pending_receipt_is_pending_not_rejected_or_consumed(owned_request):
    f = owned_request
    f.runtime.result = {'status': 'pending'}
    assert post(f).json()['status'] == 'pending'
    assert len(f.runtime.controls) == 1


@pytest.mark.parametrize('result', [{'status': 'consumed'}, {'status': 'rejected'},
    {'status': 'queued', 'message_id': 'forged'}, None])
def test_unknown_or_mismatched_native_ack_cannot_be_reported_queued(owned_request, result):
    owned_request.runtime.result = result
    assert post(owned_request).status_code == 409


def test_stop_tombstone_prevents_context_before_request_cancellation_finishes(owned_request):
    f = owned_request
    f.store.upsert_provider_stop_tombstone(tenant_id='local', owner_id='owner-a',
                                         base_idempotency_key='turn-a', ttl_seconds=60)
    assert post(f).json()['status'] == 'not_running'
    assert not f.runtime.controls


def test_restored_hold_prevents_native_effect(owned_request):
    f = owned_request
    f.store.update_provider_request(f.record['request_id'], restore_hold=1)
    assert post(f).json()['status'] == 'not_running'
    assert not f.runtime.controls


def test_changed_session_worker_cannot_receive_old_invocation_context(owned_request):
    f = owned_request
    replacement = f.store.create_worker(project_id=f.worker['project_id'], owner_id='owner-a',
        name='Replacement', role='conversation-agent', profile='codex-cli', backend='',
        runtime='codex-cli', model='synthetic-model')
    f.store.upsert_provider_session(tenant_id='local', owner_id='owner-a',
        conversation_id='conversation-a', agent_id='agent-a', model_id='codex-cli:synthetic-model',
        project_id=f.worker['project_id'], worker_id=replacement['worker_id'],
        workspace_dir=f.session['workspace_dir'], access_mode='workspace')
    assert post(f).json()['status'] == 'not_running'
    assert not f.runtime.controls


def test_request_endpoint_replays_real_retained_mailbox_and_refuses_changed_delivery(owned_request, tmp_path,
                                                                                  monkeypatch):
    f = owned_request
    runtime = object.__new__(GrokBuildRuntime)
    runtime.sandbox = SimpleNamespace(box=None)
    runtime._read_active_session = lambda _: {'run_id': f.run['run_id'],
                                             'attempt_id': f.run['active_attempt_id'], 'native_session_id': 'native-a',
                                             'native_control_capabilities': {'interject': True}}
    runtime._read_session_key = lambda _: 'native-a'
    runtime._control_dir = lambda *_: tmp_path / 'controls'
    monkeypatch.setenv('WPR_GROK_CONTAINER_REVIEWED_BINARY_SHA256', 'f' * 64)
    f.app.state.service.runtime = runtime
    calls = []
    started, release = Event(), Event()
    def interject(text, message_id):
        calls.append((text, message_id))
        started.set()
        assert release.wait(2)
        return {'status': 'queued', 'message_id': message_id}
    session = SimpleNamespace(session_id='native-a', interject=interject)
    mailbox = ControlMailbox(tmp_path / 'controls', run_id=f.run['run_id'],
        attempt_id=f.run['active_attempt_id'], session=session, event=lambda _: None)
    mailbox.start()
    try:
        first = post(f)
        assert first.status_code == 200 and first.json()['status'] == 'pending'
        assert started.wait(1)
        native_id = first.json()['message_id']
        body = read_message(tmp_path / 'controls' / (native_id + '.request'))
        # Stop/admission is not held while the native ACK is delayed.
        assert f.app.state.conversation_provider._start_lock.acquire(blocking=False)
        f.app.state.conversation_provider._start_lock.release()
        release.set()
        assert submit_control(tmp_path / 'controls', body, message_id=native_id, timeout=1)['status'] == 'queued'
        second = post(f)
        assert second.status_code == 200 and second.json()['status'] == 'queued'
        assert first.json()['message_id'] == second.json()['message_id']
        assert len(calls) == 1
        changed = {**f.body, 'text': 'Different completed context',
                   'text_sha256': hashlib.sha256(b'Different completed context').hexdigest()}
        assert post(f, changed).status_code == 409
        assert len(calls) == 1
    finally:
        release.set()
        mailbox.close()


@pytest.mark.parametrize('capability', [None, False, 'true', True])
def test_live_native_capability_is_exact_typed_and_attempt_bound(capability):
    runtime = object.__new__(GrokBuildRuntime)
    runtime._read_session_key = lambda _: 'native-a'
    runtime._read_active_session = lambda _: {'run_id': 'run-a', 'attempt_id': 'attempt-a',
        'native_session_id': 'native-a', 'native_control_capabilities': {'interject': capability}}
    worker = {'worker_id': 'worker-a'}
    assert runtime.supports_native_control(worker, 'interject', run_id='run-a',
                                           attempt_id='attempt-a') is (capability is True)
    assert not runtime.supports_native_control(worker, 'interject', run_id='run-a', attempt_id='old-attempt')
    assert not runtime.supports_native_control(worker, 'interject', run_id='old-run', attempt_id='attempt-a')
    runtime._read_session_key = lambda _: 'foreign-session'
    assert runtime.supports_native_control(worker, 'interject', run_id='run-a',
                                           attempt_id='attempt-a') is (capability is True)
    runtime._read_active_session = lambda _: {'run_id': 'run-a', 'attempt_id': 'attempt-a',
        'native_control_capabilities': {'interject': capability}}
    assert not runtime.supports_native_control(worker, 'interject', run_id='run-a', attempt_id='attempt-a')
    assert runtime.supports_native_control(worker, 'cancel')
    assert not runtime.supports_native_control(worker, 'unknown')


def test_profile_router_probes_selected_native_handler_without_provider_label_branching():
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
    router = object.__new__(ProfiledWorkerRuntime)
    worker = {'profile': 'synthetic-declared-runtime'}
    router._runtime_for_worker = lambda _: SimpleNamespace()
    assert not router.supports_native_control(worker, 'interject')
    router._runtime_for_worker = lambda _: SimpleNamespace(native_control=lambda *_: None,
        supports_native_control=lambda _worker, _action, **_identity: False)
    assert not router.supports_native_control(worker, 'interject')
    router._runtime_for_worker = lambda _: SimpleNamespace(native_control=lambda *_: None)
    assert router.supports_native_control(worker, 'interject')


def test_unreviewed_running_native_extension_is_typed_unsupported(owned_request, monkeypatch):
    f = owned_request
    monkeypatch.delenv('WPR_GROK_REVIEWED_BINARY_SHA256', raising=False)
    monkeypatch.delenv('WPR_GROK_CONTAINER_REVIEWED_BINARY_SHA256', raising=False)
    runtime = object.__new__(GrokBuildRuntime)
    runtime._read_active_session = lambda _: {}
    f.app.state.service.runtime = runtime
    assert post(f).json()['status'] == 'unsupported'
    assert not f.runtime.controls


def test_retained_interjection_pending_retry_keeps_one_native_submission(tmp_path):
    runtime = object.__new__(GrokBuildRuntime)
    runtime.sandbox = SimpleNamespace(box=None)
    runtime._read_active_session = lambda _: {'run_id': 'run-a', 'attempt_id': 'attempt-a',
                                             'native_session_id': 'native-a'}
    runtime._read_session_key = lambda _: 'native-a'
    runtime._control_dir = lambda *_: tmp_path
    started, release = Event(), Event()
    calls = []
    def interject(text, message_id):
        calls.append((text, message_id))
        started.set()
        assert release.wait(2)
        return {'status': 'queued', 'message_id': message_id}
    session = SimpleNamespace(session_id='native-a', interject=interject)
    mailbox = ControlMailbox(tmp_path, run_id='run-a', attempt_id='attempt-a', session=session,
                             event=lambda _: None)
    payload = {'text': TEXT, 'message_id': 'e' * 64, 'request_fingerprint': DIGEST}
    def submit():
        return runtime.native_control({'worker_id': 'worker-a'}, run_id='run-a',
            attempt_id='attempt-a', action='interject', payload=payload)
    mailbox.start()
    try:
        assert submit() == {'status': 'pending', 'message_id': 'e' * 64}
        assert started.wait(1)
        assert submit() == {'status': 'pending', 'message_id': 'e' * 64}
        assert len(calls) == 1
        release.set()
        retained_body = {'text': TEXT, 'request_fingerprint': DIGEST, 'run_id': 'run-a',
                         'attempt_id': 'attempt-a', 'session_id': 'native-a', 'action': 'interject'}
        assert submit_control(tmp_path, retained_body, message_id='e' * 64, timeout=1)['status'] == 'queued'
        mailbox.close()
        assert not list(tmp_path.glob('*.request'))
        assert submit() == {'status': 'queued', 'message_id': 'e' * 64}
        assert len(calls) == 1
    finally:
        release.set()
        mailbox.close()


def test_dispatch_rechecks_exact_attempt_in_existing_service(owned_request, monkeypatch):
    f = owned_request
    original = f.store.get_active_run
    calls = []
    def replacing(worker_id):
        row = original(worker_id)
        calls.append(row)
        return {**row, 'active_attempt_id': 'replacement-attempt'} if len(calls) > 1 else row
    monkeypatch.setattr(f.store, 'get_active_run', replacing)
    assert post(f).json()['status'] == 'not_running'
    assert not f.runtime.controls


def test_grok_stable_mailbox_receipt_replays_once_and_conflicting_text_refuses(tmp_path):
    runtime = object.__new__(GrokBuildRuntime)
    runtime.sandbox = SimpleNamespace(box=None)
    runtime._read_active_session = lambda _: {'run_id': 'run-a', 'attempt_id': 'attempt-a',
                                             'native_session_id': 'native-a'}
    runtime._read_session_key = lambda _: 'native-a'
    runtime._control_dir = lambda *_: tmp_path
    calls = []
    session = SimpleNamespace(session_id='native-a', interject=lambda text, message_id:
        calls.append((text, message_id)) or {'status': 'queued', 'message_id': message_id})
    mailbox = ControlMailbox(tmp_path, run_id='run-a', attempt_id='attempt-a', session=session,
                             event=lambda _: None)
    mailbox.start()
    payload = {'text': TEXT, 'message_id': 'e' * 64, 'request_fingerprint': DIGEST}
    try:
        first = runtime.native_control({'worker_id': 'worker-a'}, run_id='run-a',
            attempt_id='attempt-a', action='interject', payload=payload)
        second = runtime.native_control({'worker_id': 'worker-a'}, run_id='run-a',
            attempt_id='attempt-a', action='interject', payload=payload)
        assert first == {'status': 'pending', 'message_id': 'e' * 64}
        retained_body = {'text': TEXT, 'request_fingerprint': DIGEST, 'run_id': 'run-a',
                         'attempt_id': 'attempt-a', 'session_id': 'native-a', 'action': 'interject'}
        assert submit_control(tmp_path, retained_body, message_id='e' * 64, timeout=1)['status'] == 'queued'
        second = runtime.native_control({'worker_id': 'worker-a'}, run_id='run-a',
            attempt_id='attempt-a', action='interject', payload=payload)
        assert second == {'status': 'queued', 'message_id': 'e' * 64}
        assert len(calls) == 1
        with pytest.raises(ValueError, match='conflict'):
            runtime.native_control({'worker_id': 'worker-a'}, run_id='run-a',
                attempt_id='attempt-a', action='interject', payload={**payload, 'text': 'Different context'})
        assert len(calls) == 1
    finally:
        mailbox.close()


def test_pending_retained_interjection_cannot_drain_after_cancel(tmp_path):
    calls = []
    session = SimpleNamespace(session_id='native-a', cancel=lambda: None,
        interject=lambda *args: calls.append(args) or {'status': 'queued'})
    mailbox = ControlMailbox(tmp_path, run_id='run-a', attempt_id='attempt-a',
                             session=session, event=lambda _: None)
    body = {'action': 'interject', 'run_id': 'run-a', 'attempt_id': 'attempt-a',
            'session_id': 'native-a', 'text': TEXT, 'request_fingerprint': DIGEST}
    assert submit_control(tmp_path, body, message_id='e' * 64, timeout=0)['status'] == 'pending'
    mailbox.cancel_turn()
    mailbox.start()
    try:
        assert submit_control(tmp_path, body, message_id='e' * 64, timeout=1)['status'] == 'rejected'
        assert calls == []
    finally:
        mailbox.close()


def test_stopped_mailbox_refuses_interjection_without_native_call(tmp_path):
    calls = []
    session = SimpleNamespace(session_id='native-a', interject=lambda *args: calls.append(args))
    mailbox = ControlMailbox(tmp_path, run_id='run-a', attempt_id='attempt-a',
                             session=session, event=lambda _: None)
    mailbox.stopped.set()
    with pytest.raises(ValueError, match='cancelled|stopped'):
        mailbox._handle({'action': 'interject', 'run_id': 'run-a', 'attempt_id': 'attempt-a',
                         'session_id': 'native-a', 'text': TEXT, 'message_id': 'e' * 64})
    assert calls == []
