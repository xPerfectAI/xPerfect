import base64
import hashlib
import json
from pathlib import Path

import pytest

from workers_projects_runtime import deliverables, profile_runtime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.upload_projection import project_inline_image_files
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, RuntimeErrorBase


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6pAAAAABJRU5ErkJggg==")


def transcript(data=PNG, *, call_id="call_one", source_type="base64"):
    return "\n".join(json.dumps(event) for event in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": call_id, "name": "arbitrary_native_tool"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": [
            {"type": "image", "source": {"type": source_type, "media_type": "image/png", "data": base64.b64encode(data).decode()}}
        ]}]}},
        {"type": "result", "result": "The observation accompanies the finding."},
    ])


def write_evidence(tmp_path, stdout=None, run_id="run_image"):
    worker = {"worker_id": "worker_a", "workspace_dir": str(tmp_path), "execution_mode": "host"}
    path = profile_runtime._write_evidence_for_run(
        worker=worker, run_id=run_id, runtime_name="claude-code", model="opus",
        command=[], env={}, workspace=tmp_path, stdout_text=stdout or transcript(),
        stderr_text="", output_text="The observation accompanies the finding.", error_text="",
        exit_code=0, timeout_seconds=30, stop_reason="process_exit", constraint_ledger=None,
        transcript_paths={},
    )
    return worker, json.loads((tmp_path / path).read_text())


def test_terminal_evidence_preserves_real_image_without_selecting_deliverable(tmp_path):
    worker, evidence = write_evidence(tmp_path)
    observation = evidence["native_media"]["observations"][0]
    assert (tmp_path / observation["workspace_path"]).read_bytes() == PNG
    assert observation["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert observation["tool_call_id"] == "call_one"
    assert observation["run_id"] == "run_image"
    assert deliverables.candidate_artifact_paths(worker) == []
    assert deliverables.deliverable_payload(worker, {"run_id": "run_image"}, "finding") is None
    assert evidence["artifacts"]["count"] == 0


def test_callback_uses_existing_signed_artifact_links_and_keeps_no_local_paths(tmp_path):
    worker, _ = write_evidence(tmp_path)
    service = object.__new__(WorkersProjectsService)
    paths = []
    service._signed_artifact_download_url = lambda owner, path: paths.append(path) or "https://worker.example/v1/link-refs/download"
    service._signed_artifact_open_url = lambda owner, path: "https://worker.example/v1/link-refs/open"
    media = service._native_media_callback_observations(worker, {"run_id": "run_image"})
    item = media["observations"][0]
    assert item["download_url"].endswith("/download")
    assert item["open_url"].endswith("/open")
    assert paths == [f"artifacts/native-media/run_image/{hashlib.sha256(PNG).hexdigest()}.png"]
    assert "workspace_path" not in item
    assert str(tmp_path) not in json.dumps(media)


def test_original_user_image_and_unpaired_tool_result_are_not_native_output(tmp_path):
    events = [json.loads(line) for line in transcript().splitlines()]
    events[0] = {"type": "user", "message": {"content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}}]}}
    assert deliverables.capture_native_media(tmp_path, "run_image", "\n".join(map(json.dumps, events))) == {"observations": [], "omitted_count": 0}


def test_codex_mcp_typed_result_preserves_same_bytes_and_call_identity(tmp_path):
    event = {"type": "item.completed", "item": {"id": "item_one", "type": "mcp_tool_call", "tool": "arbitrary_tool", "result": {"content": [
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}
    ]}}}
    media = deliverables.capture_native_media(tmp_path, "run_codex", json.dumps(event))
    item = media["observations"][0]
    assert item["tool_call_id"] == "item_one"
    assert (tmp_path / item["workspace_path"]).read_bytes() == PNG


@pytest.mark.parametrize("mutation", ["source_url", "invalid_base64", "wrong_mime", "oversize"])
def test_invalid_or_remote_images_are_omitted_without_fetch_or_failure(tmp_path, mutation):
    raw = transcript()
    if mutation == "source_url": raw = transcript(source_type="url")
    if mutation == "invalid_base64": raw = raw.replace(base64.b64encode(PNG).decode(), "not base64!")
    if mutation == "wrong_mime": raw = raw.replace("image/png", "image/svg+xml")
    if mutation == "oversize": raw = transcript(PNG + b"x" * deliverables.NATIVE_MEDIA_MAX_BYTES)
    assert deliverables.capture_native_media(tmp_path, "run_image", raw) == {"observations": [], "omitted_count": 1}


def test_duplicate_event_replay_preserves_one_observation_and_file(tmp_path):
    raw = transcript()
    media = deliverables.capture_native_media(tmp_path, "run_image", raw + "\n" + raw)
    assert len(media["observations"]) == 1
    assert deliverables.capture_native_media(tmp_path, "run_image", raw) == media


def test_tampered_or_foreign_run_and_owner_are_not_projected(tmp_path):
    worker, evidence = write_evidence(tmp_path)
    assert deliverables.native_media_observations(worker, {"run_id": "run_other"})["observations"] == []
    assert deliverables.native_media_observations({**worker, "worker_id": "other"}, {"run_id": "run_image"})["observations"] == []
    (tmp_path / evidence["native_media"]["observations"][0]["workspace_path"]).write_bytes(b"changed")
    assert deliverables.native_media_observations(worker, {"run_id": "run_image"}) == {"observations": [], "omitted_count": 1}


def test_symlink_parent_cannot_write_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (workspace / "artifacts").symlink_to(outside, target_is_directory=True)
    assert deliverables.capture_native_media(workspace, "run_image", transcript())["omitted_count"] == 1
    assert list(outside.iterdir()) == []


def test_existing_hardlink_is_replaced_without_modifying_other_file(tmp_path):
    import os
    digest = hashlib.sha256(PNG).hexdigest()
    target = tmp_path / f"artifacts/native-media/run_image/{digest}.png"; target.parent.mkdir(parents=True)
    other = tmp_path / "untouched"; other.write_bytes(b"preserve")
    os.link(other, target)
    assert deliverables.capture_native_media(tmp_path, "run_image", transcript())["omitted_count"] == 0
    assert other.read_bytes() == b"preserve"
    assert target.read_bytes() == PNG


def test_media_cap_is_explicit_and_does_not_change_model_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(deliverables, "NATIVE_MEDIA_MAX_ITEMS", 1)
    media = deliverables.capture_native_media(tmp_path, "run_image", transcript(call_id="one") + "\n" + transcript(call_id="two"))
    assert len(media["observations"]) == 1
    assert media["omitted_count"] == 1


def image_worker(tmp_path):
    files = project_inline_image_files([{"role": "user", "content": [
        {"type": "text", "text": "Read the attached observation."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()}}
    ]}])
    for item in files:
        path = tmp_path / item["path"]; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(item["content_base64"]))
    bundle = {"run_mode": "conversation", "native_input_images": [
        {key: item[key] for key in ("path", "type", "bytes", "sha256")} for item in files
    ]}
    return {"worker_id": "image_worker", "trusted_run_lane": "conversation", "bootstrap_bundle_json": json.dumps(bundle)}, files


def test_inline_image_projection_and_claude_frame_keep_actual_bytes(tmp_path):
    worker, files = image_worker(tmp_path)
    runtime = profile_runtime.HostClaudeCodeRuntime(base_dir=str(tmp_path / "state"))
    info = RuntimeInfo(runtime="native", model="configured", gateway_url=None, gateway_port=None, gateway_token=None, session_key=None, state_dir=str(tmp_path / "state"), pid=None, workspace_dir=str(tmp_path))
    frame = json.loads(runtime._command_stdin_text(worker, "Read this image", info))
    assert frame["message"]["content"][0] == {"type": "text", "text": "Read this image"}
    assert base64.b64decode(frame["message"]["content"][1]["source"]["data"]) == PNG
    assert runtime._native_input_context(worker) is None
    assert files[0]["path"].startswith("uploads/native-images/")


def test_changed_or_unscoped_input_file_rejected_before_native_launch(tmp_path):
    worker, files = image_worker(tmp_path)
    runtime = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path / "state"))
    info = RuntimeInfo(runtime="native", model="configured", gateway_url=None, gateway_port=None, gateway_token=None, session_key=None, state_dir=str(tmp_path / "state"), pid=None, workspace_dir=str(tmp_path))
    assert runtime._native_input_image_files(worker, info)[0][2] == PNG
    (tmp_path / files[0]["path"]).write_bytes(b"changed")
    with pytest.raises(RuntimeErrorBase): runtime._native_input_image_files(worker, info)
    bundle = json.loads(worker["bootstrap_bundle_json"]); bundle["native_input_images"][0]["path"] = "/etc/hosts"
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    with pytest.raises(RuntimeErrorBase): runtime._native_input_image_files(worker, info)


def test_image_projection_does_not_read_image_like_text_or_fetch_remote_urls():
    assert project_inline_image_files([{"content": "data:image/png;base64,untrusted-prose"}]) == []
    assert project_inline_image_files([{"content": [{"type": "image_url", "image_url": {"url": "https://private.invalid/image.png"}}]}]) == []
    with pytest.raises(ValueError):
        project_inline_image_files([{"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,broken!"}}]}])


def test_request_owner_replaces_untrusted_image_descriptors_and_drops_stale_files():
    from workers_projects_runtime.conversation_provider import ConversationProvider, ChatCompletionRequest, GLASSHIVE_MODELS
    provider = object.__new__(ConversationProvider)
    payload = ChatCompletionRequest(model="codex-cli:gpt-5.6-sol", messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()}}
    ]}], metadata={"bootstrap_bundle": {"native_input_images": [{"path": "/etc/hosts"}], "files": [
        {"path": "uploads/native-images/stale.png"}, {"path": "notes/keep.txt", "content": "keep"}
    ]}})
    bundle = provider._native_bundle(payload, GLASSHIVE_MODELS[payload.model], "xhigh")
    assert bundle["native_input_images"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert [item["path"] for item in bundle["files"]] == ["notes/keep.txt", bundle["native_input_images"][0]["path"]]
    payload.messages[0].content = "Next text only input."
    payload.metadata.bootstrap_bundle = bundle
    next_bundle = provider._native_bundle(payload, GLASSHIVE_MODELS[payload.model], "xhigh")
    assert "native_input_images" not in next_bundle
    assert next_bundle["files"] == [{"path": "notes/keep.txt", "content": "keep"}]


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_actual_command_builder_selects_native_image_transport(tmp_path, monkeypatch, harness):
    worker, files = image_worker(tmp_path)
    worker.update(profile="codex-cli" if harness == "codex" else "claude-code", execution_mode="host", workspace_root=str(tmp_path))
    runtime_class = profile_runtime.HostCodexCliRuntime if harness == "codex" else profile_runtime.HostClaudeCodeRuntime
    runtime = runtime_class(base_dir=str(tmp_path / "state"))
    monkeypatch.setattr(runtime, "_host_env", lambda *_: {})
    if harness == "claude":
        monkeypatch.setattr(
            runtime,
            "_inject_private_subscription_auth",
            lambda _env: "synthetic_test",
        )
    monkeypatch.setattr(profile_runtime, "apply_bound_provider_account_environment", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("WPR_HOST_CODEX_CONVERSATION_PROJECT_INSTRUCTIONS", "inherit")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    if harness == "codex":
        monkeypatch.setattr(runtime, "_assert_host_codex_worker_policy", lambda *_: None)
    info = RuntimeInfo(runtime="native", model="configured", gateway_url=None, gateway_port=None, gateway_token=None, session_key=None, state_dir=str(tmp_path / "state"), pid=None, workspace_dir=str(tmp_path))
    command, _ = runtime._build_command(worker, "Read this image", info)
    if harness == "codex":
        assert command[command.index("--image") + 1] == str(tmp_path / files[0]["path"])
        assert command[-1] == "-"
    else:
        assert command[command.index("--input-format") + 1] == "stream-json"
        assert runtime._native_input_context(worker) is None


def test_callback_signed_route_and_exact_terminal_replay(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from workers_projects_runtime import api
    from workers_projects_runtime.openclaw_runtime import StubRuntime

    monkeypatch.setattr(api, "load_viventium_runtime_env", lambda: None)
    monkeypatch.setattr(WorkersProjectsService, "_process_scheduler_cycle", lambda *_: None)
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-test-link-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "links.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://testserver")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    app = api.create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime(), reconcile_on_startup=False)
    client = TestClient(app)
    service, store = app.state.service, app.state.store
    record = store.reserve_delegation(
        tenant_id="tenant-a", owner_id="owner-a", idempotency_key="media-idem", request_digest="media-digest",
        origin_ref="origin_media_0001", title="Image observation", goal="Read an observation", instruction="Read the current note",
        origin_surface="web", worker_name="Image worker", worker_role="worker", profile="claude-code",
        backend="claude-code", runtime="claude-code", model="configured", execution_mode="host",
        bootstrap_bundle={"callbacks": {"origin_ref": "origin_media_0001", "events_webhook_url": "https://callback.example.invalid/events"}},
    )
    workspace = tmp_path / "workspace"; workspace.mkdir()
    worker = store.update_worker(record["worker_id"], workspace_dir=str(workspace))
    run_id = record["current_run_id"]
    # Synthetic fixture state only, through the same terminal result owner.
    exact = store.finalize_run_if_state(run_id, "queued", "completed", output_text="The note observation accompanies the finding.")
    assert exact is not None
    profile_runtime._write_evidence_for_run(
        worker=worker, run_id=run_id, runtime_name="claude-code", model="configured", command=[], env={}, workspace=workspace,
        stdout_text=transcript(), stderr_text="", output_text=exact["output_text"], error_text="", exit_code=0,
        timeout_seconds=30, stop_reason="process_exit", constraint_ledger=None, transcript_paths={},
    )
    try:
        first = service._emit_callback(worker, "run.completed", run=exact, submit_delivery=False)
        assert first is not None
        second = service._emit_callback(worker, "run.completed", run=exact, submit_delivery=False)
        assert second is not None and second["payload_json"] == first["payload_json"]
        assert second["callback_id"] == first["callback_id"]
        payload = json.loads(first["payload_json"])
        item = payload["native_media"]["observations"][0]
        assert item["run_id"] == run_id
        assert "/v1/link-refs/" in item["download_url"]
        response = client.get(item["download_url"], follow_redirects=False)
        assert response.status_code == 200
        assert response.content == PNG
        assert response.headers["content-type"].startswith("image/png")
        assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
        assert "no-store" in response.headers["cache-control"]
        assert client.get(f"/v1/workers/{worker['worker_id']}/artifacts/download", params={"path": "artifacts/native-media/" + run_id + "/" + item["sha256"] + ".png"}).status_code == 401
        assert service._emit_callback(worker, "run.completed", run={**exact, "output_text": "stale"}, submit_delivery=False) is None
    finally:
        service.shutdown()


def test_verified_evidence_projection_preserves_aggregate_limit(tmp_path, monkeypatch):
    worker, _ = write_evidence(tmp_path, transcript(call_id="one") + "\n" + transcript(call_id="two"))
    monkeypatch.setattr(deliverables, "NATIVE_MEDIA_MAX_TOTAL_BYTES", len(PNG))
    projected = deliverables.native_media_observations(worker, {"run_id": "run_image"})
    assert len(projected["observations"]) == 1
    assert projected["omitted_count"] == 1
