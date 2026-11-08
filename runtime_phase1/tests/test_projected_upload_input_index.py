"""Accepted application uploads use the existing native per-run input index."""

import hashlib
import json

import pytest

from test_schedule_files import OWNER, TENANT, _dispatch, _schedule, _upload, _worker, service
from workers_projects_runtime.bootstrap import apply_bootstrap
from workers_projects_runtime.service import native_execution_bindings
from workers_projects_runtime.upload_projection import (
    project_bootstrap_upload_files,
    projected_upload_input_index,
)
from workers_projects_runtime.workspace_files import FileAdmissionError


def selected_bundle(*, selected=True, owner=OWNER):
    return {
        "application_context_format": "opaque_v1",
        "glasshive_upload_context": {"tool_uploaded_files": [{
            "file_id": "selected-table", "filename": "numbers.csv", "source": "text",
            "text": "item,quantity,price\nalpha,7,13\n", "bytes": 29, "user": owner,
        }]},
        "viventium_delegation_packet": {
            "selected_files": [{"ordinal": 0, "ref": "selected-table"}] if selected else [],
        },
    }


@pytest.mark.parametrize("continuation", [False, True])
def test_projected_upload_has_grounded_run_index_and_materialized_bytes(service, tmp_path, continuation):
    worker = service.store.update_worker(_worker(service)["worker_id"], bootstrap_profile="none")
    projected = project_bootstrap_upload_files(selected_bundle(), tenant_id=TENANT, owner_id=OWNER)
    if not continuation:
        worker = service.store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps(projected))
    else:
        worker = service.store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps(native_execution_bindings(projected)))
    run = service.store.create_run(worker["worker_id"], worker["project_id"], "Use the accepted table.")
    if continuation:
        run = service.store.update_run(run["run_id"], runtime_bundle_json=json.dumps({"application_invocation_context_required": True}))
        service._run_local_bundles[run["run_id"]] = projected
    invocation = service._run_local_worker(worker, run)
    bundle = json.loads(invocation["bootstrap_bundle_json"])
    index_path = invocation["_run_input_manifest_path"]
    index = json.loads(next(item["content"] for item in bundle["files"] if item["path"] == index_path))
    text = selected_bundle()["glasshive_upload_context"]["tool_uploaded_files"][0]["text"]
    assert index == {"version": 1, "files": [{
        "name": "numbers.csv", "path": "uploads/numbers.csv", "source_file_id": "selected-table",
        "input_kind": "extracted_text", "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "size_bytes": len(text.encode()),
    }]}
    workspace = tmp_path / "workspace"
    if continuation:
        from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
        HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))._materialize_workspace_files(invocation, workspace, bundle)
    else:
        apply_bootstrap(home_dir=tmp_path / "home", workspace_dir=workspace, runtime_name="codex-cli",
                        worker=invocation, copy_file=lambda source, target: target.write_bytes(source.read_bytes()),
                        copy_tree=lambda *_: None)
    materialized = json.loads((workspace / index_path).read_text())["files"][0]
    assert (workspace / materialized["path"]).read_bytes() == text.encode()
    rendered = service._runtime_instruction_for_run(invocation, run["instruction"])
    assert rendered == run["instruction"] + "\n\n" + service._run_inputs_prompt()["instruction"].format(index_path=index_path)
    assert not service.store.get_run_file_manifest(run["run_id"], worker_id=worker["worker_id"], tenant_id=TENANT, owner_id=OWNER)
    assert service.store.get_worker(worker["worker_id"])["bootstrap_bundle_json"] == worker["bootstrap_bundle_json"]


def test_projected_and_managed_uploads_share_one_existing_run_index(service):
    worker = _worker(service)
    projected = project_bootstrap_upload_files(selected_bundle(), tenant_id=TENANT, owner_id=OWNER)
    worker = service.store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps(projected))
    upload = _upload(service, b"Exact managed input bytes")
    run = _dispatch(service.store, _schedule(service, worker, upload))
    invocation = service._run_local_worker(worker, run)
    bundle = json.loads(invocation["bootstrap_bundle_json"])
    indexes = [item for item in bundle["files"] if item["path"] == invocation["_run_input_manifest_path"]]
    assert len(indexes) == 1
    inputs = json.loads(indexes[0]["content"])["files"]
    assert len(inputs) == 2
    assert inputs[0]["path"] == "uploads/numbers.csv"
    managed = service.store.get_run_file_manifest(run["run_id"], worker_id=worker["worker_id"], tenant_id=TENANT, owner_id=OWNER)[0]
    assert inputs[1] == {key: managed[key] for key in ("name", "path", "sha256", "size_bytes")}


def test_deselected_and_foreign_inputs_do_not_enter_index():
    assert projected_upload_input_index(project_bootstrap_upload_files(
        selected_bundle(selected=False), tenant_id=TENANT, owner_id=OWNER)) == []
    with pytest.raises(ValueError, match="authenticated owner"):
        project_bootstrap_upload_files(selected_bundle(owner="foreign"), tenant_id=TENANT, owner_id=OWNER)


def test_original_bytes_index_keeps_exact_owned_source_and_order(tmp_path, monkeypatch):
    root = tmp_path / "owned-uploads"
    owner = root / OWNER
    owner.mkdir(parents=True)
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(root))
    records = []
    for ordinal, data in enumerate((b"first-original", b"second-original")):
        file_id = f"document-{ordinal}"
        source = owner / f"{file_id}__source.pdf"
        source.write_bytes(data)
        records.append({"file_id": file_id, "filename": "source.pdf", "source": "local",
                        "filepath": f"/uploads/{OWNER}/{source.name}", "user": OWNER,
                        "bytes": len(data), "media_group_index": ordinal})
    projected = project_bootstrap_upload_files({"glasshive_upload_context": {"tool_uploaded_files": records}},
                                               tenant_id=TENANT, owner_id=OWNER)
    index = projected_upload_input_index(projected)
    assert [item["path"] for item in index] == ["uploads/source.pdf", "uploads/source-2.pdf"]
    assert [item["source_file_id"] for item in index] == ["document-0", "document-1"]
    assert [item["media_group_index"] for item in index] == [0, 1]
    assert all(item["input_kind"] == "original" and "source_path" not in item for item in index)
    workspace = tmp_path / "workspace"
    apply_bootstrap(home_dir=tmp_path / "home", workspace_dir=workspace, runtime_name="codex-cli",
                    worker={"bootstrap_profile": "none", "tenant_id": TENANT, "owner_id": OWNER,
                            "bootstrap_bundle_json": json.dumps(projected)},
                    copy_file=lambda source, target: target.write_bytes(source.read_bytes()), copy_tree=lambda *_: None)
    assert [(workspace / item["path"]).read_bytes() for item in index] == [b"first-original", b"second-original"]


def test_only_projected_inputs_are_indexed_and_unavailable_original_is_truthful(monkeypatch):
    import workers_projects_runtime.upload_projection as projection
    monkeypatch.setattr(projection, "_owner_sources_for_file_ids", lambda *args, **kwargs: {})
    projected = project_bootstrap_upload_files({
        "files": [{"scope": "workspace", "path": "notes.txt", "content": "Ordinary workspace context."}],
        "glasshive_upload_context": {"tool_uploaded_files": [{
            "file_id": "original-document", "filename": "source.pdf", "source": "local",
            "text": "An extraction is not the original PDF.",
        }]},
    }, tenant_id=TENANT, owner_id=OWNER)
    index = projected_upload_input_index(projected)
    assert len(index) == 1
    assert index[0]["input_kind"] == "original" and index[0]["available"] is False
    assert index[0]["path"] == "uploads/source.pdf.metadata.json"
    assert "sha256" not in index[0] and "size_bytes" not in index[0]
    assert not any(item.get("path") == "notes.txt" for item in index)


@pytest.mark.parametrize("path", ["../foreign.txt", "/outside/foreign.txt"])
def test_index_reuses_existing_workspace_path_guard(path):
    with pytest.raises(FileAdmissionError):
        projected_upload_input_index({"files": [{"file_id": "selected", "input_kind": "original", "path": path}]})
