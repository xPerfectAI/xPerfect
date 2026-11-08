"""The existing native selected-file publisher supplies normal host attachments."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from workers_projects_runtime import profile_runtime
from workers_projects_runtime.openclaw_runtime import StubRuntime
from test_conversation_provider import AUTH, _client, _payload, lifecycle_owned_test_clients
from test_conversation_provider_recovery import _post_bound, _result
from test_account_api import (
    _exact_terminal_generation, _truthfully_invoke_run, account_client, account_headers,
    delegation_payload_with_origin,
)
from test_native_media import JPEG


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


@pytest.mark.parametrize("keep_valid_file", [True, False])
def test_unavailable_selected_file_does_not_drop_completed_mission(
    account_client, tmp_path, monkeypatch, keep_valid_file,
):
    service = account_client.app.state.service
    service.executor.submit = lambda *_a, **_k: None
    store = account_client.app.state.store
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-completion-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "links.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://testserver")
    response = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="unavailable-mission-file"),
        json=delegation_payload_with_origin(),
    )
    assert response.status_code == 202, response.text
    work = store.get_delegation(response.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    run = _truthfully_invoke_run(store, work["current_run_id"], suffix="unavailable-mission-file")
    workspace = tmp_path / "mission-workspace"
    workspace.mkdir()
    worker = store.update_worker(work["worker_id"], workspace_dir=str(workspace))
    native = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path / "native"))
    service.runtime = native
    valid = b"item,value\nAster,10\n"
    blocked = b"synthetic rejected publication"
    (workspace / "result.csv").write_bytes(valid)
    (workspace / "blocked.txt").write_bytes(blocked)
    scope = {
        "run_id": run["run_id"], "worker_id": worker["worker_id"], "owner_id": worker["owner_id"],
        "workspace_dir": str(workspace), "attempt_id": run["active_attempt_id"],
        "file_output_transport": "artifact_sha256",
    }
    native._capture_native_output_files(
        {**worker, "_run_attempt_id": run["active_attempt_id"]},
        str(workspace), scope, run["run_id"],
        "[Blocked](blocked.txt)\n[Result](result.csv)" if keep_valid_file else "[Blocked](blocked.txt)",
    )
    unavailable_digest = hashlib.sha256(blocked).hexdigest()
    valid_digest = hashlib.sha256(valid).hexdigest()
    sign = service._signed_artifact_download_url
    monkeypatch.setattr(service, "_signed_artifact_download_url", lambda worker, path:
        "" if unavailable_digest in path else sign(worker, path))
    output = (
        f"Completed the work.\n[Missing](artifact_sha256:{'a' * 64})\n"
        f"[Blocked](artifact_sha256:{unavailable_digest})"
    )
    if keep_valid_file:
        output += f"\n[Result](artifact_sha256:{valid_digest})"
    run = store.finalize_run(run["run_id"], "completed", output_text=output)

    intent = service._emit_callback(worker, "run.completed", run=run, submit_delivery=False)
    body = json.loads(intent["payload_json"])
    assert body["result_state"] == "completed"
    assert body["output_files"]["rejected"] == [{"name": "File", "code": "unreadable"}]
    assert "Completed the work." in body["message"]
    assert "File unavailable" in body["message"]
    assert "artifact_sha256:" not in body["message"]
    assert str(workspace) not in intent["payload_json"]
    files = body["output_files"]["files"]
    assert [file["filename"] for file in files] == (["result.csv"] if keep_valid_file else [])
    if files:
        assert account_client.get(files[0]["download_url"]).content == valid
    assert store.get_run(run["run_id"])["output_text"] == output
    assert service._emit_callback(worker, "run.completed", run=run, submit_delivery=False)["payload_json"] == intent["payload_json"]


def test_recovery_enqueues_new_origin_only_completion_and_leaves_legacy_run_alone(
    account_client, monkeypatch,
):
    from workers_projects_runtime.store import Store

    service = account_client.app.state.service
    service.executor.submit = lambda *_a, **_k: None
    store = account_client.app.state.store
    monkeypatch.setenv("GLASSHIVE_EVENTS_WEBHOOK_URL", "https://callback.example.invalid/events")
    monkeypatch.setenv("GLASSHIVE_EVENTS_HMAC_SECRET", "synthetic-recovery-secret")
    runs = {}
    for kind in ("retained", "legacy"):
        origin = f"ghi_synthetic_origin_{kind}"
        payload = delegation_payload_with_origin(origin_ref=origin)
        payload["bootstrapBundle"]["callbacks"] = {"origin_ref": origin}
        accepted = account_client.post(
            "/v1/delegations", headers=account_headers(idempotency_key=f"callback-recovery-{kind}"),
            json=payload,
        )
        assert accepted.status_code == 202, accepted.text
        work = store.get_delegation(accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
        run = _truthfully_invoke_run(store, work["current_run_id"], suffix=kind)
        if kind == "retained":
            runs[kind] = service._finalize_run_if_state(
                run["run_id"], "running", "completed", output_text="Stored result.",
                **_exact_terminal_generation(store, run["run_id"]),
            )
        else:
            runs[kind] = store.finalize_run(run["run_id"], "completed", output_text="Legacy result.")
    retained = runs["retained"]
    assert retained is not None
    reopened = Store(store.db_path)
    assert [row["run_id"] for row in reopened.list_terminal_runs_missing_callback_intent()] == [retained["run_id"]]

    def terminal_callbacks(run):
        return [
            row for row in store.list_callback_outbox_for_run(
                run["run_id"], tenant_id="tenant-a", owner_id="owner-a",
            )
            if row["event_type"] == "run.completed"
        ]

    assert not terminal_callbacks(retained)
    with monkeypatch.context() as failure:
        def temporary_error(*_args, **_kwargs):
            raise OSError("temporary publisher error")

        failure.setattr(service, "_emit_callback", temporary_error)
        assert service._reconcile_terminal_callback_intents() == 0
    assert service._reconcile_terminal_callback_intents() == 1
    assert service._reconcile_terminal_callback_intents() == 0
    rows = terminal_callbacks(retained)
    assert len(rows) == 1
    body = json.loads(rows[0]["payload_json"])
    assert body["result_revision"] == retained["terminal_result_revision"]
    assert body["result_digest"] == Store.terminal_result_digest(retained)
    assert body["message"] == "Stored result."
    assert not terminal_callbacks(runs["legacy"])


def test_delegation_accepts_original_input_that_quotes_an_artifact_reference(account_client):
    payload = delegation_payload_with_origin()
    original = f"Explain this example link: [Example](artifact_sha256:{'b' * 64})."
    payload["instruction"] = original
    response = account_client.post(
        "/v1/delegations", headers=account_headers(idempotency_key="artifact-reference-in-original"),
        json=payload,
    )
    assert response.status_code == 202, response.text
    store = account_client.app.state.store
    work = store.get_delegation(response.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    assert store.get_run(work["initial_run_id"])["instruction"] == original
    callbacks = store.list_callback_outbox_for_run(
        work["initial_run_id"], tenant_id="tenant-a", owner_id="owner-a",
    )
    assert [row["event_type"] for row in callbacks] == ["run.queued"]


@pytest.mark.parametrize("stream", [False, True])
def test_full_host_outside_selection_keeps_canonical_downloads_and_exact_replay(tmp_path, monkeypatch, stream):
    external = tmp_path.parent / (tmp_path.name + "-outside")
    external.mkdir()
    paths = [external / "report.csv", external / "copy.csv"]
    data = b"name,value\nexample,7\n"
    for path in paths:
        path.write_bytes(data)
    output = "\n".join(f"[File](<{path}>)" for path in paths)
    client, runtime, _ = prepare(tmp_path, monkeypatch, authored_output=output)
    payload = _payload(tmp_path, stream=stream)
    payload["metadata"]["glasshive_options"]["access"] = "full"
    response, digest = _post_bound(client, payload)
    assert response.status_code == 200, response.text
    canonical = _result(client, digest).json()["response"]
    carrier = canonical["glasshive"]["output_files"]
    assert {file["filename"] for file in carrier["files"]} == {path.name for path in paths}
    assert not carrier.get("rejected")
    assert str(external) not in json.dumps(canonical)
    for path in paths:
        path.unlink()
    for file in carrier["files"]:
        assert file["sha256"] == hashlib.sha256(data).hexdigest()
        assert client.get(file["download_url"]).content == data
    duplicate, _ = _post_bound(client, payload)
    assert runtime.calls == 1
    if stream:
        # Transient live activity is not part of the canonical replay carrier.
        for current in (response, duplicate):
            chunks = [json.loads(row[6:]) for row in current.text.splitlines() if row.startswith("data: {")]
            files = [choice["delta"]["provider_specific_fields"]["viventium"]["output_files"]
                     for row in chunks for choice in row.get("choices", [])
                     if "output_files" in choice.get("delta", {}).get("provider_specific_fields", {}).get("viventium", {})]
            assert files == [carrier]
    else:
        assert duplicate.json() == canonical
    assert _result(client, digest).json()["response"] == canonical


def test_recognized_image_names_remain_distinct_and_replay_exactly(tmp_path, monkeypatch):
    names = ('chart.png', 'chart.gif', 'chart.jpg', 'chart (2).jpg')
    output = '\n'.join(f'[Image](<{name}>)' for name in names)
    client, runtime, _ = prepare(tmp_path, monkeypatch, authored_output=output)
    for name in names:
        (tmp_path / name).write_bytes(JPEG)
    payload = _payload(tmp_path)
    response, digest = _post_bound(client, payload)
    assert response.status_code == 200, response.text
    canonical = response.json()
    files = canonical['glasshive']['output_files']['files']
    assert len(files) == 4
    assert {file['filename'] for file in files} == {
        'chart.jpg', 'chart (2).jpg', 'chart (3).jpg', 'chart (4).jpg',
    }
    for file in files:
        assert file['mime_type'] == 'image/jpeg'
        assert file['sha256'] == hashlib.sha256(JPEG).hexdigest()
        download = client.get(file['download_url'])
        assert download.content == JPEG
        assert download.headers['content-type'] == 'image/jpeg'
    assert all((tmp_path / name).read_bytes() == JPEG for name in names)
    for name in names:
        (tmp_path / name).write_text('later working copy')
    assert _result(client, digest).json()['response'] == canonical
    duplicate, _ = _post_bound(client, payload)
    assert duplicate.json() == canonical and runtime.calls == 1


@pytest.mark.parametrize("name,data,mime", [
    ("chart.webp", base64.b64decode(
        "UklGRjgAAABXRUJQVlA4ICwAAADQAQCdASoBAAEAAUAmJaACdLoB+AADsAD+9Ykf/izkYjs83/zNDPJR44AAAA=="
    ), "image/webp"),
    ("chart.gif", base64.b64decode(
        "R0lGODdhAQABAIEAAH8yCgAAAAAAAAAAACwAAAAAAQABAAAIBAABBAQAOw=="
    ), "image/gif"),
    ("chart.jfif", JPEG, "image/jpeg"),
    ("chart.JPE", JPEG, "image/jpeg"),
])
def test_correct_image_names_do_not_depend_on_host_mime_table(tmp_path, monkeypatch, name, data, mime):
    client, _runtime, _ = prepare(tmp_path, monkeypatch, authored_output=f'[Image](<{name}>)')
    (tmp_path / name).write_bytes(data)
    with monkeypatch.context() as absent_mime_table:
        absent_mime_table.setattr(profile_runtime.mimetypes, "guess_type", lambda _path: (None, None))
        response, _digest = _post_bound(client, _payload(tmp_path))
    assert response.status_code == 200, response.text
    descriptor = response.json()['glasshive']['output_files']['files'][0]
    assert descriptor['filename'] == name and descriptor['mime_type'] == mime
    download = client.get(descriptor['download_url'])
    assert download.content == data and download.headers['content-type'] == mime


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


def test_padded_selected_destination_publishes_canonical_file_and_exact_download(tmp_path, monkeypatch):
    raw = f'[CSV](< \t{tmp_path / "result.csv"}\t >)'
    client, runtime, data = prepare(tmp_path, monkeypatch, authored_output=raw)
    response, digest = _post_bound(client, _payload(tmp_path))
    assert response.status_code == 200, response.text
    canonical = response.json()
    carrier = canonical['glasshive']['output_files']
    assert carrier.get('rejected', []) == []
    assert len(carrier['files']) == 1
    selected = carrier['files'][0]
    assert selected['filename'] == 'result.csv'
    assert selected['bytes'] == len(data)
    assert selected['sha256'] == hashlib.sha256(data).hexdigest()
    assert client.get(selected['download_url']).content == data
    assert str(tmp_path) not in canonical['choices'][0]['message']['content']
    assert _result(client, digest).json()['response'] == canonical
    assert runtime.calls == 1


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
    assert canonical['choices'][0]['message']['content'] == (
        'The result is ready. Download\n[Official source](https://docs.example.test/)')
    assert str(tmp_path) not in json.dumps(carrier)
    chunks = [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith('data: {')]
    terminal = [choice['delta']['provider_specific_fields']['viventium']['output_files']
                for row in chunks for choice in row.get('choices', [])
                if 'output_files' in choice.get('delta', {}).get('provider_specific_fields', {}).get('viventium', {})]
    assert terminal == [carrier]
    assert _result(client, digest).json()['response'] == canonical
    assert runtime.calls == 1


@pytest.mark.parametrize("callback_author", [True, False])
def test_missing_file_reference_keeps_authored_answer_and_typed_rejection(
    tmp_path, monkeypatch, callback_author,
):
    client, runtime, _ = prepare(
        tmp_path, monkeypatch,
        authored_output=f"Result verified. [Missing](artifact_sha256:{'c' * 64})",
    )
    headers = {"X-Viventium-Actor-Kind": "system", "X-Viventium-Origin": "callback"} if callback_author else {}
    response, digest = _post_bound(client, _payload(tmp_path), headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["choices"][0]["message"]["content"] == "Result verified. [File unavailable]"
    assert result["glasshive"]["output_files"]["files"] == []
    assert result["glasshive"]["output_files"]["rejected"] == [{"name": "File", "code": "unreadable"}]
    assert _result(client, digest, headers=headers).json()["response"] == result
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
    assert response.status_code == 409 and 'output_files' not in response.json().get('glasshive', {})
    assert response.json()['error']['code'] == 'native_output_files_identity_unavailable'
    assert runtime.calls == 1


@pytest.mark.parametrize(('output', 'selected_count'), [
    (r'[escaped\]label](result.csv)', 1),
    (r'\[literal](result.csv)', 0),
    (r'\\[real](result.csv)', 1),
])
def test_selected_file_escapes_survive_selection_and_both_artifact_renderers(
    tmp_path, monkeypatch, output, selected_count,
):
    client, runtime, data = prepare(tmp_path, monkeypatch, authored_output=output)
    response, digest = _post_bound(client, _payload(tmp_path))
    assert response.status_code == 200, response.text
    canonical = response.json()
    files = canonical.get('glasshive', {}).get('output_files', {}).get('files', [])
    assert len(files) == selected_count
    content = canonical['choices'][0]['message']['content']
    if selected_count:
        assert files[0]['filename'] == 'result.csv'
        assert client.get(files[0]['download_url']).content == data
        assert output.split('](')[0] in content
    else:
        assert content == output
    assert _result(client, digest).json()['response'] == canonical
    assert runtime.calls == 1


@pytest.mark.parametrize('failure', ['link_unavailable', 'storage_io'])
def test_publication_failure_keeps_answer_and_captured_source_without_claiming_delivery(tmp_path, monkeypatch, failure):
    client, runtime, data = prepare(tmp_path, monkeypatch, authored_output='Useful findings. [CSV](result.csv)')
    if failure == 'link_unavailable':
        monkeypatch.setattr(client.app.state.service, '_signed_artifact_download_url', lambda *_args: '')
    else:
        def missing(*_args, **_kwargs): raise FileNotFoundError('Synthetic absent publication')
        def storage_full(*_args, **_kwargs): raise OSError('Synthetic publication storage full')
        monkeypatch.setattr('workers_projects_runtime.service.verified_native_file', missing)
        monkeypatch.setattr('workers_projects_runtime.service.copy_native_file', storage_full)
    response, digest = _post_bound(client, _payload(tmp_path))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['choices'][0]['message']['content'] == 'Useful findings. [File unavailable]'
    assert result['glasshive']['output_files']['files'] == []
    assert result['glasshive']['output_files']['rejected'] == [{'name':'File','code':'unreadable'}]
    assert (tmp_path/'result.csv').read_bytes() == data
    record = client.app.state.store.get_provider_request(result['id'])
    run = client.app.state.store.get_run(record['run_id'])
    worker = client.app.state.store.get_worker(run['worker_id'])
    _, captured = runtime.provider_native_image_output(worker, run, '')
    assert len(captured)==1 and (captured[0][2].root/captured[0][2].relative).read_bytes()==data
    assert _result(client, digest).json()['response'] == result
    duplicate, _ = _post_bound(client, _payload(tmp_path))
    assert duplicate.json()==result and runtime.calls==1
