"""The existing native selected-file publisher supplies normal host attachments."""
import hashlib
import json
from pathlib import Path

import pytest

from workers_projects_runtime import profile_runtime
from workers_projects_runtime.openclaw_runtime import StubRuntime
from test_conversation_provider import AUTH, _client, _payload, lifecycle_owned_test_clients
from test_conversation_provider_recovery import _post_bound, _result
from test_account_api import account_client, account_headers, delegation_payload_with_origin, _truthfully_invoke_run


class SelectedFileRuntime(StubRuntime):
    def __init__(self, state, *, publish=True, authored_output=None):
        self.native = profile_runtime.HostCodexCliRuntime(base_dir=str(state))
        self.publish = publish
        self.calls = 0
        self.authored_output = authored_output

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        super().run_task(worker, instruction, timeout_sec, run_id)
        self.calls += 1
        workspace = Path(worker['workspace_dir'])
        output = ('[CSV](result.csv)\n[Same bytes, another name](copy.csv)\n'
                  '[Plain text](result.txt)') if self.publish else 'The private reference was checked.'
        if self.authored_output is not None:
            output = self.authored_output
        scope = {'run_id': run_id, 'worker_id': worker['worker_id'], 'owner_id': worker['owner_id'],
                 'workspace_dir': str(workspace), 'images': [], 'file_output_transport': 'artifact_sha256',
                 'attempt_id': str(worker.get('_run_attempt_id') or '')}
        return self.native._project_native_image_output(worker, self._runtime_info(worker, pid=None),
                                                       output, scope, run_id)

    def provider_native_image_output(self, worker, run, output):
        return self.native.provider_native_image_output(worker, run, output)

    def provider_native_output_file_rejections(self, worker, run):
        return self.native.provider_native_output_file_rejections(worker, run)


def prepare(tmp_path, monkeypatch, *, publish=True, authored_output=None):
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET', 'synthetic-file-test-secret')
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(tmp_path / 'links.sqlite3'))
    monkeypatch.setenv('GLASSHIVE_ARTIFACT_BASE_URL', 'http://testserver')
    data = b'item,value\nAster,10\n'
    for name in ('result.csv', 'copy.csv', 'result.txt', 'unselected.txt'):
        (tmp_path / name).write_bytes(data)
    runtime = SelectedFileRuntime(tmp_path / 'native', publish=publish, authored_output=authored_output)
    return _client(tmp_path, monkeypatch, runtime=runtime), runtime, data


def test_completed_mission_callback_carries_only_native_file_identity_and_replays_exactly(
    account_client, tmp_path, monkeypatch,
):
    from datetime import datetime, timezone
    service = account_client.app.state.service
    service.executor.submit = lambda *_a, **_k: None
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-mission-file-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "mission-links.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://testserver")
    payload = delegation_payload_with_origin()
    accepted = account_client.post("/v1/delegations", headers=account_headers(
        idempotency_key="selected-mission-files"), json=payload)
    assert accepted.status_code == 202, accepted.text
    store = account_client.app.state.store
    work = store.get_delegation(accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    run = _truthfully_invoke_run(store, work["current_run_id"], suffix="selected-mission-files")
    workspace = tmp_path / "mission-workspace"
    workspace.mkdir()
    worker = store.update_worker(work["worker_id"], workspace_dir=str(workspace))
    native = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path / "native"))
    scope = {"run_id": run["run_id"], "worker_id": worker["worker_id"], "owner_id": worker["owner_id"],
             "workspace_dir": str(workspace), "attempt_id": run["active_attempt_id"],
             "file_output_transport": "artifact_sha256"}
    files = {"chart.png": b"synthetic-image-bytes", "result.csv": b"item,value\nexample,1\n"}
    for name, data in files.items():
        (workspace / name).write_bytes(data)
    native._capture_native_output_files({**worker, "_run_attempt_id": run["active_attempt_id"]},
        str(workspace), scope, run["run_id"], "[CSV](result.csv)\n![Chart](chart.png)")
    service.runtime = native
    run = store.update_run(run["run_id"], state="completed", output_text="Files verified.",
                           ended_at=datetime.now(timezone.utc).isoformat())
    intent = service._emit_callback(worker, "run.completed", run=run, submit_delivery=False)
    first = json.loads(intent["payload_json"])
    carrier = first["output_files"]
    assert set(carrier) == {"version", "owner_id", "run_id", "attempt_id", "callback_id",
                           "origin_ref", "work_ref", "result_revision", "result_digest", "files"}
    assert carrier["version"] == 1 and carrier["owner_id"] == worker["owner_id"]
    assert carrier["run_id"] == run["run_id"] and carrier["attempt_id"] == run["active_attempt_id"]
    assert first["attempt_id"] == carrier["attempt_id"]
    assert carrier["callback_id"] == first["callback_id"]
    assert carrier["origin_ref"] == work["origin_ref"] and carrier["work_ref"] == work["work_ref"]
    assert carrier["result_revision"] == first["result_revision"]
    assert carrier["result_digest"] == first["result_digest"]
    assert store.get_run(run["run_id"])["output_text"] == "Files verified."
    assert first["message"].startswith("Files verified.")
    assert all(first.get(key) is None for key in ("user_id", "message_id", "stream_id", "conversation_id"))
    verified = account_client.post("/v1/callback-associations/verify", headers=account_headers(), json={
        "originRef": work["origin_ref"], "workRef": work["work_ref"],
        "workerId": worker["worker_id"], "runId": run["run_id"],
    })
    assert verified.json()["runId"] == carrier["run_id"]
    assert verified.json()["attemptId"] == carrier["attempt_id"]
    for descriptor in carrier["files"]:
        data = files[descriptor["filename"]]
        assert descriptor["bytes"] == len(data)
        assert descriptor["sha256"] == hashlib.sha256(data).hexdigest()
        assert "/v1/link-refs/ghr_" in descriptor["download_url"] and "?" not in descriptor["download_url"]
        (workspace / descriptor["filename"]).write_text("changed working copy")
        downloaded = account_client.get(descriptor["download_url"])
        assert downloaded.status_code == 200 and downloaded.content == data
    duplicate = service._emit_callback(worker, "run.completed", run=run, submit_delivery=False)
    assert duplicate["payload_json"] == intent["payload_json"]
    assert json.loads(duplicate["payload_json"])["output_files"] == carrier
    observation = service._emit_callback(worker, "artifact.created", run=run, message="Observed.",
                                         submit_delivery=False)
    assert "output_files" not in json.loads(observation["payload_json"])


def test_canonical_native_files_keep_selected_names_verified_bytes_and_exact_recovery(tmp_path, monkeypatch):
    client, runtime, data = prepare(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload['metadata'].update(logical_turn_id='turn-one', logical_turn_revision=2)
    response, digest = _post_bound(client, payload)
    assert response.status_code == 200, response.text
    canonical = response.json()
    carrier = canonical['glasshive']['output_files']
    assert carrier == canonical['choices'][0]['message']['provider_specific_fields']['viventium']['output_files']
    assert carrier['owner_id'] == 'owner-a' and carrier['message_id'] == 'message-a'
    assert carrier['conversation_id'] == 'conv-a' and carrier['agent_id'] == 'agent-a'
    assert carrier['stream_id'] == 'stream-a' and carrier['request_id'] == canonical['id']
    assert carrier['logical_turn_id'] == 'turn-one' and carrier['logical_turn_revision'] == 2
    assert carrier['invocation_id'] == 'invocation-a' and carrier['attempt_id']
    assert {file['filename'] for file in carrier['files']} == {'result.csv', 'copy.csv', 'result.txt'}
    assert 'unselected.txt' not in json.dumps(carrier) and str(tmp_path) not in json.dumps(carrier)
    for file in carrier['files']:
        assert file['bytes'] == len(data) and file['sha256'] == hashlib.sha256(data).hexdigest()
        download = client.get(file['download_url'])
        assert download.status_code == 200 and download.content == data
        assert download.headers['content-type'].split(';')[0] == file['mime_type']
    record = client.app.state.store.get_provider_request(canonical['id'])
    assert json.loads(record['response_json']) == canonical and record['run_id'] == carrier['run_id']
    for name in ('result.csv', 'copy.csv', 'result.txt'):
        (tmp_path / name).write_text('later working copy')
    assert _result(client, digest).json()['response'] == canonical
    duplicate, _ = _post_bound(client, payload)
    assert duplicate.json() == canonical and runtime.calls == 1


def test_terminal_stream_carries_whole_files_beside_text_and_canonical_response(tmp_path, monkeypatch):
    client, runtime, _ = prepare(tmp_path, monkeypatch)
    response, digest = _post_bound(client, _payload(tmp_path, stream=True))
    assert response.status_code == 200, response.text
    chunks = [json.loads(row[6:]) for row in response.text.splitlines()
              if row.startswith('data: {')]
    carriers = [choice['delta']['provider_specific_fields']['viventium']['output_files']
                for row in chunks for choice in row.get('choices', [])
                if 'output_files' in choice.get('delta', {}).get('provider_specific_fields', {}).get('viventium', {})]
    assert len(carriers) == 1
    canonical = _result(client, digest).json()['response']
    assert carriers[0] == canonical['glasshive']['output_files']
    assert canonical['choices'][0]['message']['content'] and runtime.calls == 1


def test_reading_or_referring_to_workspace_files_does_not_publish_them(tmp_path, monkeypatch):
    client, runtime, _ = prepare(tmp_path, monkeypatch, publish=False)
    response, digest = _post_bound(client, _payload(tmp_path))
    assert response.status_code == 200, response.text
    assert 'output_files' not in response.json()['glasshive']
    assert 'provider_specific_fields' not in response.json()['choices'][0]['message']
    assert _result(client, digest).json()['response'] == response.json() and runtime.calls == 1


@pytest.mark.parametrize('stream', [False, True])
def test_selected_files_survive_graph_transfer_with_publisher_identity_and_exact_retry(
    tmp_path, monkeypatch, stream,
):
    output = json.dumps({'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist',
                         'content': '[CSV](result.csv)'})
    client, runtime, data = prepare(tmp_path, monkeypatch, authored_output=output)
    payload = _payload(tmp_path, stream=stream)
    payload['metadata'].update(logical_turn_id='turn-one', logical_turn_revision=2)
    payload['tools'] = [{'type': 'function', 'function': {
        'name': 'lc_transfer_to_specialist', 'description': 'Consult a specialist.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    }}]
    response, digest = _post_bound(client, payload)
    assert response.status_code == 200, response.text
    canonical = _result(client, digest).json()['response']
    message = canonical['choices'][0]['message']
    assert canonical['choices'][0]['finish_reason'] == 'tool_calls'
    assert message['tool_calls'][0]['function']['name'] == 'lc_transfer_to_specialist'
    carrier = canonical['glasshive']['output_files']
    assert carrier == message['provider_specific_fields']['viventium']['output_files']
    assert carrier['owner_id'] == 'owner-a' and carrier['agent_id'] == 'agent-a'
    assert carrier['message_id'] == 'message-a' and carrier['conversation_id'] == 'conv-a'
    assert carrier['logical_turn_id'] == 'turn-one' and carrier['logical_turn_revision'] == 2
    assert carrier['invocation_id'] == 'invocation-a' and carrier['attempt_id']
    assert len(carrier['files']) == 1
    file = carrier['files'][0]
    assert file['filename'] == 'result.csv' and file['bytes'] == len(data)
    assert file['sha256'] == hashlib.sha256(data).hexdigest()
    assert client.get(file['download_url']).content == data
    if stream:
        chunks = [json.loads(row[6:]) for row in response.text.splitlines()
                  if row.startswith('data: {')]
        transported = [choice['delta']['provider_specific_fields']['viventium']['output_files']
                       for chunk in chunks for choice in chunk.get('choices', [])
                       if 'output_files' in choice.get('delta', {}).get('provider_specific_fields', {}).get('viventium', {})]
        assert transported == [carrier]
    else:
        assert response.json() == canonical
    duplicate, _ = _post_bound(client, payload)
    assert duplicate.status_code == 200
    assert _result(client, digest).json()['response'] == canonical and runtime.calls == 1
    if stream:
        assert duplicate.text == response.text
    else:
        assert duplicate.json() == canonical


def test_rejected_selected_file_keeps_answer_terminal_carrier_and_exact_recovery(tmp_path, monkeypatch):
    client, runtime, _ = prepare(tmp_path, monkeypatch, authored_output=(
        'The result is ready. [Download](../outside.csv)\n'
        '[Official source](https://docs.example.test/)'))
    response, digest = _post_bound(client, _payload(tmp_path, stream=True))
    assert response.status_code == 200, response.text
    canonical = _result(client, digest).json()['response']
    carrier = canonical['glasshive']['output_files']
    assert carrier['files'] == []
    assert carrier['rejected'] == [{'name': 'outside.csv', 'code': 'not_deliverable'}]
    assert canonical['choices'][0]['message']['content'].startswith('The result is ready.')
    assert str(tmp_path) not in json.dumps(carrier)
    chunks = [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith('data: {')]
    terminal = [choice['delta']['provider_specific_fields']['viventium']['output_files']
                for row in chunks for choice in row.get('choices', [])
                if 'output_files' in choice.get('delta', {}).get('provider_specific_fields', {}).get('viventium', {})]
    assert terminal == [carrier]
    assert _result(client, digest).json()['response'] == canonical
    assert runtime.calls == 1


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('missing', [('message_id',), ('stream_id',), ('message_id', 'stream_id')])
def test_identityless_selected_transfer_preserves_consultant_without_export(
    tmp_path, monkeypatch, stream, missing,
):
    from types import SimpleNamespace
    import time
    from workers_projects_runtime import conversation_provider
    clock = [time.time()]
    monkeypatch.setattr(conversation_provider, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=time.monotonic, perf_counter=time.perf_counter, sleep=time.sleep,
    ))
    output = json.dumps({'type': 'tool_call', 'tool_name': 'lc_transfer_to_specialist',
                         'content': 'Consultation complete. [CSV](result.csv)'})
    client, runtime, _ = prepare(tmp_path, monkeypatch, authored_output=output)
    payload = _payload(tmp_path, stream=stream)
    for key in missing:
        payload['metadata'].pop(key)
    payload['tools'] = [{'type': 'function', 'function': {
        'name': 'lc_transfer_to_specialist', 'description': 'Consult a specialist.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    }}]
    response = client.post('/v1/chat/completions', headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    if stream:
        chunks = [json.loads(row[6:]) for row in response.text.splitlines()
                  if row.startswith('data: {')]
        assert not any('error' in chunk for chunk in chunks)
        final = next(chunk for chunk in reversed(chunks) if chunk.get('choices'))
        assert final['choices'][0]['finish_reason'] == 'tool_calls'
        record = client.app.state.store.get_provider_request(final['id'])
        canonical = json.loads(record['response_json'])
        assert 'output_files' not in response.text
    else:
        canonical = response.json()
    assert canonical['choices'][0]['finish_reason'] == 'tool_calls'
    message = canonical['choices'][0]['message']
    assert message['content'].startswith('Consultation complete.')
    assert message['tool_calls'][0]['function']['name'] == 'lc_transfer_to_specialist'
    assert 'output_files' not in canonical['glasshive']
    assert 'provider_specific_fields' not in message
    clock[0] += 2  # Replay crosses a second; the accepted request still owns its timestamp.
    duplicate = client.post('/v1/chat/completions', headers=AUTH, json=payload)
    assert duplicate.status_code == 200 and duplicate.text == response.text
    assert runtime.calls == 1


@pytest.mark.parametrize('missing', [('message_id',), ('stream_id',), ('message_id', 'stream_id')])
def test_identityless_selected_assistant_still_rejects_export(tmp_path, monkeypatch, missing):
    client, runtime, _ = prepare(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    for key in missing:
        payload['metadata'].pop(key)
    response = client.post('/v1/chat/completions', headers=AUTH, json=payload)
    assert response.status_code == 502 and 'output_files' not in response.json().get('glasshive', {})
    assert runtime.calls == 1
