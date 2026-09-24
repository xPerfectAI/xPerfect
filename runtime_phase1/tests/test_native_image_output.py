import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from workers_projects_runtime import profile_runtime
from workers_projects_runtime.openclaw_runtime import RuntimeInfo
from workers_projects_runtime.upload_projection import project_inline_image_files


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


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


def test_recovery_uses_exact_published_image_scope_not_later_worker_bundle(tmp_path, monkeypatch):
    runtime, worker, info, scope, path = fixture(tmp_path)
    worker["bootstrap_bundle_json"] = json.dumps({"native_input_images": []})
    stdout = tmp_path / "stdout.log"; stdout.write_text("completed")
    stderr = tmp_path / "stderr.log"; stderr.write_text("")
    exit_path = tmp_path / "exit_code"; exit_path.write_text("0")
    active = {"run_id": "run-current", "run_mode": "conversation", "stdout_path": str(stdout),
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


def test_provider_plain_and_graph_outputs_share_signed_artifact_route(tmp_path, monkeypatch):
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
    worker = store.update_worker(record["worker_id"], workspace_dir=info.workspace_dir, trusted_run_lane="conversation")
    run = store.get_run(record["current_run_id"])
    scope.update(worker_id=worker["worker_id"], run_id=run["run_id"])
    root = runtime._run_root(worker["worker_id"], run["run_id"]); root.mkdir(parents=True)
    profile_runtime._atomic_write_private_text(root / "native-image-scope.json", json.dumps(scope))
    request = {"run_id": run["run_id"], "owner_id": worker["owner_id"], "tenant_id": worker["tenant_id"]}
    router = object.__new__(profile_runtime.ProfiledWorkerRuntime)
    router._runtime_for_worker = lambda _w: runtime
    service.runtime = router
    provider = ConversationProvider(store, service)
    native = f"![Current note]({path.as_uri()})\n\n[Official source](https://docs.example.test/)"
    monkeypatch.setattr(provider, "_native_output_snapshot", lambda *_: native)
    monkeypatch.setattr(provider, "_native_citation_sources_snapshot", lambda *_: [])
    digest = hashlib.sha256(PNG).hexdigest()
    try:
        plain = provider._conversation_output(request, run)
        match = re.search(r"!\[Current note\]\((http://testserver/v1/link-refs/[^)]+)\)", plain)
        assert match, "The provider must preserve a selected image before local-path redaction"
        url = match.group(1)
        assert str(tmp_path) not in plain and "artifact_sha256:" not in plain
        assert "[Official source](https://docs.example.test/)" in plain
        assert provider._conversation_output(request, run) == plain
        graph = provider._graph_control_output(request, {**run, "output_text": json.dumps({"content": f"![Current note](artifact_sha256:{digest})"})})
        assert json.loads(graph)["content"] == f"![Current note]({url})"
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 200 and response.content == PNG
        assert response.headers["content-type"].startswith("image/png")
        assert "no-store" in response.headers["cache-control"]
        # Original uploaded bytes remain non-deliverable; only selected artifact bytes are exposed.
        assert client.get(f"/v1/workers/{worker['worker_id']}/artifacts/download", params={"path": scope["images"][0]["path"]}, headers={"Authorization": "Bearer synthetic-service-token"}).status_code in {400, 401, 403}
        artifact = Path(info.workspace_dir) / "artifacts/native-media" / run["run_id"] / (digest + ".png")
        assert artifact.read_bytes() == PNG
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
        with pytest.raises(RuntimeErrorBase):
            service.render_provider_native_images(request, run, f"![Note](artifact_sha256:{digest})")
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
    assert output == expected and [item[2] for item in files] == [data]


def test_workspace_output_does_not_publish_unselected_or_unsafe_files(tmp_path):
    runtime, worker, info, scope, _ = fixture(tmp_path)
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
