import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from workers_projects_runtime import profile_runtime
from workers_projects_runtime.openclaw_runtime import RuntimeInfo
from workers_projects_runtime.upload_projection import project_inline_image_files
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def mission_final_fixture(tmp_path, harness, final):
    runtime, worker, info, scope, _ = fixture(tmp_path, "claude" if harness == "claude" else "codex")
    if harness == "grok":
        runtime = HostGrokBuildRuntime(base_dir=str(tmp_path / "state"))
    worker.update(trusted_run_lane="mission", _active_run_id="run-current", _run_attempt_id="attempt-current")
    info.model = "grok-selected"
    worker["model"] = info.model
    scope.update(file_output_transport="artifact_sha256", attempt_id="attempt-current")
    scope.pop("output_transport")
    workspace = Path(info.workspace_dir)
    (workspace / "chart.png").write_bytes(PNG)
    (workspace / "report.csv").write_bytes(b"name,value\nexample,1\n")
    (workspace / "unselected.txt").write_text("not selected")
    run_root = runtime._run_root(worker["worker_id"], "run-current")
    run_root.mkdir(parents=True, exist_ok=True)
    profile_runtime._atomic_write_private_text(run_root / "native-image-scope.json", json.dumps(scope))
    if harness == "grok":
        events = [
            {"type": "grok.session.started", "session_id": "native-current", "model": "grok-selected"},
            {"type": "grok.session.update", "update": {"type": "tool_result", "text": "[Internal](unselected.txt)"}},
            {"type": "grok.result", "session_id": "native-current", "stop_reason": "end_turn", "output": final},
        ]
    elif harness == "claude":
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "[Internal](unselected.txt)"}]}},
            {"type": "result", "session_id": "native-current", "result": final},
        ]
    else:
        events = [
            {"type": "thread.started", "thread_id": "native-current"},
            {"type": "item.completed", "item": {"type": "command_execution", "text": "[Internal](unselected.txt)"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
            {"type": "turn.completed"},
        ]
    return runtime, worker, info, scope, "\n".join(map(json.dumps, events))


@pytest.mark.parametrize("harness", ["codex", "claude", "grok"])
@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("report_link", [False, True])
def test_mission_final_files_survive_report_selection_for_normal_and_recovered_harness(
    tmp_path, monkeypatch, harness, recovered, report_link,
):
    final = "[CSV](report.csv)\n![Chart](chart.png)\n\nFINAL REPORT:\nVerified report."
    if report_link:
        final += "\n\n[CSV](report.csv)"
    runtime, worker, info, scope, stdout = mission_final_fixture(tmp_path, harness, final)
    expected = "Verified report."
    if report_link:
        expected += "\n\n[CSV](artifact_sha256:" + hashlib.sha256(b"name,value\nexample,1\n").hexdigest() + ")"
    monkeypatch.setattr(runtime, "_require_native_children_completed", lambda *_a: None, raising=False)
    if recovered:
        worker.pop("_active_run_id")
        root = runtime._run_root(worker["worker_id"], "run-current")
        for name, value in (("stdout.log", stdout), ("stderr.log", ""), ("exit_code", "0")):
            (root / name).write_text(value)
        active = {"run_id": "run-current", "run_mode": "mission", "native_image_scope": scope,
                  "stdout_path": str(root / "stdout.log"), "stderr_path": str(root / "stderr.log"),
                  "exit_path": str(root / "exit_code")}
        monkeypatch.setattr(runtime, "_latest_completed_run_payload", lambda *_a, **_k: active)
        monkeypatch.setattr(runtime, "_record_run_metrics", lambda *_a: ({}, {}))
        monkeypatch.setattr(runtime, "reconcile_worker", lambda _w: info)
        monkeypatch.setattr(runtime, "_remember_native_session_key", lambda *_a: None)
        monkeypatch.setattr(profile_runtime, "_ensure_recovered_success_evidence", lambda **_k: ("completed", ""))
        assert runtime.collect_completed_run(worker, run_id="run-current")["output_text"] == expected
    else:
        output = runtime._parse_output(worker, stdout, "", info)[1]
        assert runtime._project_native_image_output(worker, info, output, scope, "run-current") == expected
    captured = runtime._read_native_image_scope(worker["worker_id"], "run-current")
    assert {item["root_path"] for item in captured["output_files"]} == {"report.csv", "chart.png"}
    (Path(info.workspace_dir) / "report.csv").write_text("later workspace edits")
    files = runtime._native_image_output_files({**worker, "_run_attempt_id": "attempt-current"},
                                               info.workspace_dir, captured, "run-current")
    assert len(files) == 2
    assert {item[2].sha256 for item in files} == {
        hashlib.sha256(PNG).hexdigest(), hashlib.sha256(b"name,value\nexample,1\n").hexdigest(),
    }


@pytest.mark.parametrize("quoted", [
    "`report.csv`", "> [CSV](report.csv)", "`[CSV](report.csv)`",
    "```md\n[CSV](report.csv)\n```", "    [CSV](report.csv)",
])
def test_final_mission_does_not_select_quoted_or_literal_paths(tmp_path, monkeypatch, quoted):
    runtime, worker, info, _scope, stdout = mission_final_fixture(
        tmp_path, "codex", quoted + "\n\nFINAL REPORT:\nReference checked.",
    )
    monkeypatch.setattr(runtime, "_require_native_children_completed", lambda *_a: None)
    assert runtime._parse_output(worker, stdout, "", info)[1] == "Reference checked."
    assert not runtime._read_native_image_scope(worker["worker_id"], "run-current").get("output_files")


def fixture(tmp_path, harness="codex"):
    workspace = tmp_path / "workspace"
    files = project_inline_image_files([{"content": [{"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(PNG).decode(),
    }}]}])
    item = files[0]
    path = workspace / item["path"]
    path.parent.mkdir(parents=True)
    path.write_bytes(PNG)
    images = [{key: item[key] for key in ("path", "type", "bytes", "sha256")}]
    worker = {"worker_id": "worker-current", "owner_id": "owner-current", "trusted_run_lane": "conversation",
              "tenant_id": "tenant-current", "workspace_dir": str(workspace),
              "bootstrap_bundle_json": json.dumps({"native_input_images": images, "provider_capabilities": {"native_image_output": "artifact_sha256"}})}
    runtime_cls = profile_runtime.HostCodexCliRuntime if harness == "codex" else profile_runtime.HostClaudeCodeRuntime
    runtime = runtime_cls(base_dir=str(tmp_path / "state"))
    info = RuntimeInfo(runtime="native", model="configured", gateway_url=None, gateway_port=None,
                       gateway_token=None, session_key=None, state_dir=str(tmp_path / "state"), pid=None,
                       workspace_dir=str(workspace))
    scope = {"run_id": "run-current", "worker_id": worker["worker_id"], "owner_id": worker["owner_id"],
             "workspace_dir": str(workspace), "images": images, "output_transport": "artifact_sha256", "attempt_id": ""}
    return runtime, worker, info, scope, path


def project(runtime, worker, info, output, scope, run_id="run-current"):
    renderer = getattr(runtime, "_project_native_image_output", lambda _w, _i, value, _s, _r: value)
    return profile_runtime._redact_text(renderer(worker, info, output, scope, run_id))


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_selected_native_image_path_keeps_model_image_and_public_source(tmp_path, harness):
    runtime, worker, info, scope, path = fixture(tmp_path, harness)
    digest = hashlib.sha256(PNG).hexdigest()
    for target in (quote(str(path)), path.as_uri(), scope["images"][0]["path"]):
        output = f"![Sentence close-up]({target})\n\nSources: https://docs.example.test/"
        assert project(runtime, worker, info, output, scope) == (
            f"![Sentence close-up](artifact_sha256:{digest})\n\nSources: https://docs.example.test/"
        )
    assert project(runtime, worker, info, "The note is unchanged.", scope) == "The note is unchanged."


def test_wrong_run_owner_workspace_or_changed_bytes_cannot_resolve(tmp_path):
    runtime, worker, info, scope, path = fixture(tmp_path)
    output = f"![Note]({path})"
    for changes in ({"run_id": "old"}, {"owner_id": "other"}, {"worker_id": "other"}, {"workspace_dir": "/unrelated"}):
        assert "artifact_sha256:" not in project(runtime, worker, info, output, {**scope, **changes})
    outside = "![Other](/Users/synthetic/private.png)"
    assert project(runtime, worker, info, outside, scope) == "Other"
    path.write_bytes(b"changed")
    assert "artifact_sha256:" not in project(runtime, worker, info, output, scope)


@pytest.mark.parametrize("lane", ["conversation", "mission"])
def test_recovery_uses_exact_published_image_scope_not_later_worker_bundle(tmp_path, monkeypatch, lane):
    runtime, worker, info, scope, path = fixture(tmp_path)
    worker["bootstrap_bundle_json"] = json.dumps({"native_input_images": []})
    worker["trusted_run_lane"] = lane
    if lane == "mission":
        scope.update(images=[], file_output_transport="artifact_sha256")
    monkeypatch.setattr(profile_runtime, "_ensure_recovered_success_evidence", lambda **_k: ("completed", ""))
    stdout = tmp_path / "stdout.log"; stdout.write_text("completed")
    stderr = tmp_path / "stderr.log"; stderr.write_text("")
    exit_path = tmp_path / "exit_code"; exit_path.write_text("0")
    active = {"run_id": "run-current", "run_mode": lane, "stdout_path": str(stdout),
              "stderr_path": str(stderr), "exit_path": str(exit_path), "native_image_scope": scope}
    monkeypatch.setattr(runtime, "_latest_completed_run_payload", lambda *_a, **_k: active)
    monkeypatch.setattr(runtime, "_record_run_metrics", lambda *_a: ({}, {}))
    monkeypatch.setattr(runtime, "reconcile_worker", lambda _w: info)
    monkeypatch.setattr(runtime, "_parse_output", lambda *_a: (None, f"![Note]({path.as_uri()})"))
    monkeypatch.setattr(runtime, "_remember_native_session_key", lambda *_a: None)
    result = runtime.collect_completed_run(worker, run_id="run-current")
    assert result["output_text"] == f"![Note](artifact_sha256:{hashlib.sha256(PNG).hexdigest()})"


def test_symlink_input_does_not_authorize_target_or_fabricated_output_path(tmp_path):
    runtime, worker, info, scope, path = fixture(tmp_path)
    outside = tmp_path / "outside.png"; outside.write_bytes(PNG)
    path.unlink(); path.symlink_to(outside)
    assert "artifact_sha256:" not in project(runtime, worker, info, f"![Note]({path})", scope)
    assert "artifact_sha256:" not in project(runtime, worker, info, f"![Note]({outside})", scope)


def test_direct_caller_without_declared_resolver_keeps_existing_output(tmp_path):
    runtime, worker, info, scope, path = fixture(tmp_path)
    scope.pop("output_transport")
    raw = f"![Note]({path})"
    assert runtime._project_native_image_output(worker, info, raw, scope, "run-current") == raw


def test_durable_scope_survives_active_session_replacement(tmp_path):
    runtime, worker, info, scope, path = fixture(tmp_path)
    root = runtime._run_root(worker["worker_id"], "run-current")
    root.mkdir(parents=True)
    profile_runtime._atomic_write_private_text(root / "native-image-scope.json", json.dumps(scope))
    # Existing active-session reader must retain the scope; a later active run cannot replace it.
    runtime._write_active_session(worker["worker_id"], {"session_name": "fixture", "run_id": "run-current", "native_image_scope": scope})
    assert runtime._read_active_session(worker["worker_id"])["native_image_scope"] == scope
    runtime._write_active_session(worker["worker_id"], {"session_name": "later", "run_id": "run-later"})
    assert runtime._run_payload(worker["worker_id"], "run-current")["native_image_scope"] == scope
    worker["bootstrap_bundle_json"] = "{}"
    raw, images = runtime.provider_native_image_output(worker, {"run_id": "run-current", "worker_id": worker["worker_id"]}, f"![Note]({path})")
    assert raw == f"![Note](artifact_sha256:{hashlib.sha256(PNG).hexdigest()})"
    assert images[0][2] == PNG


@pytest.mark.parametrize("name,data,mime,lane,source_kind", [
    ("note.png", PNG, "image/png", "conversation", "workspace"),
    ("hello.html", b"<!doctype html><h1>HELLO</h1>", "text/html", "conversation", "workspace"),
    ("report.pdf", b"%PDF-1.7\nexample", "application/pdf", "conversation", "workspace"),
    ("table.csv", b"name,value\nexample,1\n", "text/csv", "conversation", "workspace"),
    ("report.md", b"# Result\nThe selected file.\n", "text/markdown", "mission", "workspace"),
    pytest.param("large-result.bin", b"x" * (9 * 1024 * 1024), "application/octet-stream", "mission", "workspace", id="large-mission-artifact"),
    pytest.param("table.csv", b"name,value\nexample,1\n", "text/csv", "conversation", "managed_tmp", id="managed-tmp-signed-file"),
])
def test_provider_plain_and_graph_outputs_share_signed_artifact_route(tmp_path, monkeypatch, name, data, mime, lane, source_kind):
    import re
    from fastapi.testclient import TestClient
    from workers_projects_runtime import api
    from workers_projects_runtime.conversation_provider import ConversationProvider
    from workers_projects_runtime.openclaw_runtime import StubRuntime, RuntimeErrorBase
    from workers_projects_runtime.service import WorkersProjectsService

    monkeypatch.setattr(api, "load_viventium_runtime_env", lambda: None)
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-test-link-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "links.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://testserver")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    app = api.create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime(), reconcile_on_startup=False)
    service, store = app.state.service, app.state.store
    client = TestClient(app)
    runtime, sample, info, scope, path = fixture(tmp_path)
    record = store.reserve_delegation(
        tenant_id="tenant-current", owner_id="owner-current", idempotency_key="input-image", request_digest="fixture-digest",
        origin_ref="origin_image_0001", title="Current note", goal="Show the selected observation", instruction="Show the note",
        origin_surface="web", worker_name="Note worker", worker_role="worker", profile="codex-cli", backend="codex-cli",
        runtime="codex-cli", model="configured", execution_mode="host", bootstrap_bundle={},
    )
    worker = store.update_worker(record["worker_id"], workspace_dir=info.workspace_dir, trusted_run_lane=lane)
    run = store.get_run(record["current_run_id"])
    scope.update(worker_id=worker["worker_id"], run_id=run["run_id"])
    image_input = mime == "image/png"
    label = "![Current note]" if image_input else "[Current note]"
    if not image_input:
        scope.update(images=[], file_output_transport="artifact_sha256")
        source_root = (Path(info.workspace_dir) if source_kind == "workspace" else
                       runtime._home_dir(worker["worker_id"]) / ".tmp")
        path = source_root / "output" / name
        path.parent.mkdir(parents=True); path.write_bytes(data)
        project(runtime, worker, info, f"{label}({path.as_uri()})", scope, run["run_id"])
    root = runtime._run_root(worker["worker_id"], run["run_id"]); root.mkdir(parents=True, exist_ok=True)
    profile_runtime._atomic_write_private_text(root / "native-image-scope.json", json.dumps(scope))
    request = {"run_id": run["run_id"], "owner_id": worker["owner_id"], "tenant_id": worker["tenant_id"]}
    router = object.__new__(profile_runtime.ProfiledWorkerRuntime)
    router._runtime_for_worker = lambda _w: runtime
    service.runtime = router
    provider = ConversationProvider(store, service)
    native = f"{label}({path.as_uri()})\n\n[Official source](https://docs.example.test/)"
    monkeypatch.setattr(provider, "_native_output_snapshot", lambda *_: native)
    monkeypatch.setattr(provider, "_native_citation_sources_snapshot", lambda *_: [])
    digest = hashlib.sha256(data).hexdigest()
    try:
        plain = provider._conversation_output(request, run)
        match = re.search(r"!?\[Current note\]\((http://testserver/v1/link-refs/[^)]+)\)", plain)
        assert match, "The provider must preserve a selected image before local-path redaction"
        url = match.group(1)
        assert str(tmp_path) not in plain and "artifact_sha256:" not in plain
        assert "[Official source](https://docs.example.test/)" in plain
        assert provider._conversation_output(request, run) == plain
        graph = provider._graph_control_output(request, {**run, "output_text": json.dumps({"content": f"{label}(artifact_sha256:{digest})"})})
        assert json.loads(graph)["content"] == f"{label}({url})"
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 200 and response.content == data
        assert response.headers["content-type"].startswith(mime)
        if lane == "mission":
            monkeypatch.setattr(service, "_callback_config_for_event", lambda *_a: {
                "events_webhook_url": "http://callback.local/xperfect", "surface": "telegram"
            })
            completed = store.update_run(run["run_id"], state="completed", ended_at="2026-01-01T12:00:00Z",
                                         output_text=f"{label}(artifact_sha256:{digest})", terminal_result_revision=1)
            intent = service._emit_callback_parallel(worker, "run.completed", run=completed,
                                                     submit_delivery=False, persist_callback=False)
            payload = json.loads(intent["payload_json"])
            assert f"{label}({url})" in payload["message"]
            assert "artifact_sha256:" not in payload["message"] and str(tmp_path) not in payload["message"]
            assert service._emit_callback_parallel(worker, "run.completed", run={**completed, "active_attempt_id": "stale"},
                                                    submit_delivery=False, persist_callback=False) is None
        assert "no-store" in response.headers["cache-control"]
        # Original uploaded bytes remain non-deliverable; only selected artifact bytes are exposed.
        if image_input:
            assert client.get(f"/v1/workers/{worker['worker_id']}/artifacts/download", params={"path": scope["images"][0]["path"]}, headers={"Authorization": "Bearer synthetic-service-token"}).status_code in {400, 401, 403}
        artifact = Path(info.workspace_dir) / "artifacts/native-media" / run["run_id"] / (digest + Path(name).suffix)
        assert artifact.read_bytes() == data
        assert list(artifact.parent.iterdir()) == [artifact]
        assert service.render_provider_native_images(request, run, "The note is unchanged.") == "The note is unchanged."
        for changed in ({"owner_id": "foreign"}, {"tenant_id": "foreign"}, {"run_id": "stale"}):
            with pytest.raises(RuntimeErrorBase):
                service.render_provider_native_images({**request, **changed}, run, f"![Note](artifact_sha256:{digest})")
        for changed in ({"active_attempt_id": "stale"},):
            with pytest.raises(RuntimeErrorBase):
                service.render_provider_native_images(request, {**run, **changed}, f"![Note](artifact_sha256:{digest})")
        for ref in ("artifact_sha256:" + "0" * 64, "artifact_sha256:invalid"):
            with pytest.raises(RuntimeErrorBase):
                service.render_provider_native_images(request, run, f"![Note]({ref})")
        path.write_bytes(b"changed")
        if image_input:
            with pytest.raises(RuntimeErrorBase):
                service.render_provider_native_images(request, run, f"![Note](artifact_sha256:{digest})")
        else:
            # Immutable selected output survives edits to the working file.
            assert provider._conversation_output(request, run) == plain
            assert client.get(url).content == data
    finally:
        service.shutdown()

@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize("name,data", [("note.png", PNG), ("report.pdf", b"%PDF-1.7\nexample"), ("table.csv", b"name,value\nexample,1\n")])
def test_selected_workspace_file_without_image_input_is_published_and_recoverable(tmp_path, harness, name, data):
    runtime, worker, info, scope, _ = fixture(tmp_path, harness)
    scope.update(images=[], file_output_transport="artifact_sha256")
    scope.pop("output_transport")
    worker["bootstrap_bundle_json"] = "{}"
    path = Path(info.workspace_dir) / "output" / name
    path.parent.mkdir(); path.write_bytes(data)
    other = path.parent / "unselected.txt"; other.write_text("internal work")
    raw = f"[Result]({path.as_uri()})"
    expected = f"[Result](artifact_sha256:{hashlib.sha256(data).hexdigest()})"
    assert project(runtime, worker, info, raw, scope) == expected
    saved = runtime._read_native_image_scope(worker["worker_id"], scope["run_id"])
    assert len(saved["output_files"]) == 1
    # Recovery uses the selected immutable bytes even if the working file later changes.
    path.write_text("later edit")
    output, files = runtime.provider_native_image_output(worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}, expected)
    assert output == expected
    snapshot = files[0][2]
    assert snapshot.sha256 == hashlib.sha256(data).hexdigest()
    assert (snapshot.root / snapshot.relative).read_bytes() == data


def test_selected_files_exceed_image_limits_and_recover_without_loading_batch(tmp_path):
    import tracemalloc
    from workers_projects_runtime.deliverables import NativeFileSnapshot

    runtime, worker, info, scope, _ = fixture(tmp_path)
    scope.update(images=[], file_output_transport="artifact_sha256")
    scope.pop("output_transport")
    root = Path(info.workspace_dir)
    # Four large files exceed both former byte limits; the rest exceed 24 items.
    paths = [root / f"result-{i}.bin" for i in range(27)]
    for i, path in enumerate(paths):
        with path.open("wb") as handle:
            for _ in range(9 if i < 4 else 1):
                handle.write(bytes([i]) * (1024 * 1024 if i < 4 else 16))
    raw = "\n".join(f"[Result {i}]({path.as_uri()})" for i, path in enumerate(paths))
    tracemalloc.start()
    try:
        projected = project(runtime, worker, info, raw, scope)
        output, files = runtime.provider_native_image_output(
            worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}, projected
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert projected.count("artifact_sha256:") == 27 and output == projected
    assert len(files) == 27 and len(scope["output_files"]) == 27
    assert all(isinstance(item[2], NativeFileSnapshot) for item in files)
    assert sum(item[2].size_bytes for item in files) > 32 * 1024 * 1024
    assert peak < 4 * 1024 * 1024  # File size and batch size do not set RAM use.
    paths[0].write_bytes(b"later working copy")
    first = files[0][2]
    assert (first.root / first.relative).stat().st_size == 9 * 1024 * 1024
    # Recovery refuses a changed immutable snapshot, even at the same size.
    with (first.root / first.relative).open("r+b") as handle:
        handle.write(b"changed")
    assert runtime.provider_native_image_output(
        worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}, projected
    )[1] == []


@pytest.mark.parametrize("lane", ["conversation", "mission"])
def test_workspace_output_does_not_publish_unselected_or_unsafe_files(tmp_path, lane):
    runtime, worker, info, scope, _ = fixture(tmp_path)
    worker["trusted_run_lane"] = lane
    scope.update(images=[], file_output_transport="artifact_sha256")
    scope.pop("output_transport")
    root = Path(info.workspace_dir)
    outside = tmp_path / "private.txt"; outside.write_text("private")
    (root / "alias.txt").symlink_to(outside)
    (root / ".env").write_text("private")
    (root / "normal.txt").write_text("ordinary")
    assert project(runtime, worker, info, "Done.", scope) == "Done."
    for target in (outside.as_uri(), "../private.txt", "alias.txt", ".env"):
        assert "artifact_sha256:" not in project(runtime, worker, info, f"[File]({target})", scope)
    for changed in ({"owner_id": "foreign"}, {"run_id": "old"}, {"attempt_id": "other"}):
        current_worker = {**worker, "_run_attempt_id": "current"}
        assert "artifact_sha256:" not in project(runtime, current_worker, info, "[File](normal.txt)", {**scope, **changed})


def test_file_manifest_has_no_inherited_image_metadata_cap(tmp_path):
    runtime, worker, info, scope, _ = fixture(tmp_path)
    scope.update(images=[], file_output_transport="artifact_sha256")
    scope.pop("output_transport")
    root = Path(info.workspace_dir)
    for i in range(600):
        (root / f"result-{i:04d}.txt").write_text(str(i))
    output = "\n".join(f"[Result {i}](result-{i:04d}.txt)" for i in range(600))
    projected = project(runtime, worker, info, output, scope)
    manifest = runtime._run_root(worker["worker_id"], scope["run_id"]) / "native-image-scope.json"
    assert manifest.stat().st_size > 128 * 1024
    recovered, files = runtime.provider_native_image_output(
        worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}, projected
    )
    assert len(files) == 600 and recovered == projected


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_selected_managed_tmp_file_keeps_true_root_and_replays_immutable_bytes(tmp_path, harness):
    runtime, worker, info, scope, _ = fixture(tmp_path, harness)
    scope.update(images=[], file_output_transport="artifact_sha256")
    temporary = runtime._home_dir(worker["worker_id"]) / ".tmp"
    path = temporary / "prior-run" / "result.csv"
    path.parent.mkdir(parents=True)
    data = b"item,value\nExample,17\n"
    path.write_bytes(data)
    raw = f"[CSV](<{path}>)"
    expected = f"[CSV](artifact_sha256:{hashlib.sha256(data).hexdigest()})"
    assert project(runtime, worker, info, raw, scope) == expected
    record = scope["output_files"][0]
    assert record["source_root"] == "managed_tmp" and record["root_path"] == "prior-run/result.csv"
    assert "workspace_path" not in record and str(temporary) not in json.dumps(record)
    path.write_bytes(b"changed working file")
    restored, files = runtime.provider_native_image_output(
        worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}, raw
    )
    assert restored == expected and files[0][0] == path
    assert (files[0][2].root / files[0][2].relative).read_bytes() == data
    assert runtime.provider_native_output_file_rejections(
        worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}
    ) == []


def test_rejected_explicit_selections_are_typed_without_paths_and_remote_links_are_unchanged(tmp_path):
    runtime, worker, info, scope, _ = fixture(tmp_path)
    scope.update(images=[], file_output_transport="artifact_sha256")
    root = Path(info.workspace_dir)
    (root / "cookies").write_text("private")
    outside = tmp_path / "Downloads" / "result.csv"
    outside.parent.mkdir(); outside.write_text("private outside scope")
    raw = f"[CSV]({outside})\n[Private](cookies)\n[Missing](missing.csv)\n[Source](https://docs.example.test/page)"
    assert "artifact_sha256:" not in project(runtime, worker, info, raw, scope)
    expected = [
        {"name": "result.csv", "code": "source_root_unsupported"},
        {"name": "cookies", "code": "not_deliverable"},
        {"name": "missing.csv", "code": "unreadable"},
    ]
    assert scope["rejected_output_files"] == expected
    assert runtime.provider_native_output_file_rejections(
        worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}
    ) == expected
    assert str(tmp_path) not in json.dumps(expected)


@pytest.mark.parametrize("kind,code", [
    ("other_worker", "source_root_unsupported"),
    ("home_auth", "source_root_unsupported"),
    ("generic_tmp", "source_root_unsupported"),
    ("downloads", "source_root_unsupported"),
    ("traversal", "not_deliverable"),
    ("symlink", "unreadable"),
    ("hardlink", "unreadable"),
    ("root_symlink", "unreadable"),
    ("private_name", "not_deliverable"),
    ("private_directory", "not_deliverable"),
])
def test_managed_tmp_selected_source_retains_isolation_and_sensitive_path_policy(tmp_path, kind, code):
    runtime, worker, info, scope, _ = fixture(tmp_path)
    scope.update(images=[], file_output_transport="artifact_sha256")
    home = runtime._home_dir(worker["worker_id"])
    temporary = home / ".tmp"
    temporary.mkdir(parents=True)
    outside = tmp_path / "outside.csv"; outside.write_text("private")
    if kind == "other_worker":
        path = runtime._home_dir("other-worker") / ".tmp" / "result.csv"
    elif kind == "home_auth":
        path = home / ".codex" / "auth.json"
    elif kind == "generic_tmp":
        path = tmp_path / "tmp" / "result.csv"
    elif kind == "downloads":
        path = tmp_path / "Downloads" / "result.csv"
    elif kind == "traversal":
        path = temporary / ".." / "outside.csv"
    elif kind == "private_name":
        path = temporary / "cookies"
    elif kind == "private_directory":
        path = temporary / ".git" / "config"
    else:
        path = temporary / "result.csv"
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        import os
        os.link(outside, path)
    elif kind == "root_symlink":
        temporary.rmdir(); temporary.symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / "result.csv").write_text("private")
    elif kind != "traversal":
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text("selected")
    assert "artifact_sha256:" not in project(runtime, worker, info, f"[File](<{path}>)", scope)
    assert scope["output_files"] == []
    assert scope["rejected_output_files"] == [{"name": path.name, "code": code}]


def test_selected_source_replay_keeps_legacy_workspace_records_and_rejects_changed_scope(tmp_path):
    runtime, worker, info, scope, _ = fixture(tmp_path)
    scope.update(images=[], file_output_transport="artifact_sha256")
    path = Path(info.workspace_dir) / "result.csv"; path.write_text("ordinary")
    projected = project(runtime, worker, info, "[File](result.csv)", scope)
    record = scope["output_files"][0]
    record["workspace_path"] = record.pop("root_path"); record.pop("source_root")
    run_root = runtime._run_root(worker["worker_id"], scope["run_id"])
    profile_runtime._atomic_write_private_text(run_root / "native-image-scope.json", json.dumps(scope))
    run = {"worker_id": worker["worker_id"], "run_id": scope["run_id"]}
    assert runtime.provider_native_image_output(worker, run, "[File](result.csv)")[0] == projected
    for changes in ({"source_root": "home"}, {"root_path": "../private.csv"}):
        invalid = {**scope, "output_files": [{**record, **changes}]}
        profile_runtime._atomic_write_private_text(run_root / "native-image-scope.json", json.dumps(invalid))
        assert runtime.provider_native_image_output(worker, run, projected)[1] == []


def test_rejected_selection_receipts_keep_exact_scope_and_validate_path_free_shape(tmp_path):
    runtime, worker, info, scope, _ = fixture(tmp_path)
    scope.update(images=[], file_output_transport="artifact_sha256", attempt_id="attempt-current")
    worker["_run_attempt_id"] = "attempt-current"
    project(runtime, worker, info, "[File](missing.csv)", scope)
    run = {"worker_id": worker["worker_id"], "run_id": scope["run_id"], "active_attempt_id": "attempt-current"}
    expected = [{"name": "missing.csv", "code": "unreadable"}]
    assert runtime.provider_native_output_file_rejections(worker, run) == expected
    for changed in ({"owner_id": "other"}, {"workspace_dir": str(tmp_path / "other")}):
        assert runtime.provider_native_output_file_rejections({**worker, **changed}, run) == []
    for changed in ({"worker_id": "other"}, {"active_attempt_id": "old"}, {"run_id": "old"}):
        assert runtime.provider_native_output_file_rejections(worker, {**run, **changed}) == []
    run_root = runtime._run_root(worker["worker_id"], scope["run_id"])
    for bad in ({"name": "/private/path", "code": "unreadable"},
                {"name": "file.csv", "code": {}},
                {"name": "file.csv", "code": "unreadable", "path": "/private"}):
        invalid = {**scope, "rejected_output_files": [bad]}
        profile_runtime._atomic_write_private_text(run_root / "native-image-scope.json", json.dumps(invalid))
        assert runtime.provider_native_output_file_rejections(worker, run) == []


@pytest.mark.parametrize("prefix", ["uploads", "tmp", "scheduled-prompt"])
@pytest.mark.parametrize("source_kind", ["workspace", "managed_tmp"])
def test_explicit_existing_file_selection_retains_operational_prefixes_without_widening_discovery(tmp_path, prefix, source_kind):
    from workers_projects_runtime.deliverables import is_user_deliverable_relative_path

    runtime, worker, info, scope, _ = fixture(tmp_path)
    scope.update(images=[], file_output_transport="artifact_sha256")
    relative = Path(prefix) / "existing.csv"
    assert not is_user_deliverable_relative_path(relative)
    assert is_user_deliverable_relative_path(relative, explicit_selection=True)
    root = (Path(info.workspace_dir) if source_kind == "workspace" else
            runtime._home_dir(worker["worker_id"]) / ".tmp")
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"existing authorized selection")
    projected = project(runtime, worker, info, f"[Existing file](<{path}>)", scope)
    assert "artifact_sha256:" in projected and not scope["rejected_output_files"]
    assert scope["output_files"][0]["source_root"] == source_kind
    assert scope["output_files"][0]["root_path"] == relative.as_posix()
