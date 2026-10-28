"""Full host authority stages only explicitly selected ordinary files."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from workers_projects_runtime import deliverables, profile_runtime
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from test_native_image_output import PNG, fixture, project


def full_host(tmp_path, harness="codex"):
    runtime, worker, info, scope, _ = fixture(tmp_path, harness)
    if harness == "grok":
        runtime = HostGrokBuildRuntime(base_dir=str(tmp_path / "state"))
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["access_mode"] = "full"
    worker.update(execution_mode="host", bootstrap_bundle_json=json.dumps(bundle))
    scope.update(images=[], file_output_transport="artifact_sha256")
    return runtime, worker, info, scope


@pytest.mark.parametrize("harness", ["codex", "claude", "grok"])
@pytest.mark.parametrize("name,data", [
    ("chart.png", PNG), ("report.pdf", b"%PDF-1.7\nselected report"),
    ("report.csv", b"name,value\nexample,7\n"), ("archive.custom", bytes(range(256))),
    ("empty.txt", b""),
])
def test_full_host_selected_files_stage_exact_bytes_names_and_replay_without_source(tmp_path, harness, name, data):
    runtime, worker, info, scope = full_host(tmp_path, harness)
    external = tmp_path / "ordinary files"
    external.mkdir()
    names = (name, "copy " + name)
    paths = [external / name for name in names]
    for path in paths:
        path.write_bytes(data)
        os.utime(path, (1, 1))  # Existing files and resends do not need a freshness heuristic.
    (external / "unselected.txt").write_bytes(b"private reference")
    raw = "\n".join(f"[File](<{path.as_uri()}>)" for path in paths)
    digest = hashlib.sha256(data).hexdigest()
    expected = "\n".join(f"[File](artifact_sha256:{digest})" for _ in paths)
    assert project(runtime, worker, info, raw, scope) == expected
    assert not scope["rejected_output_files"]
    assert len(scope["output_files"]) == 2
    assert {item["source_root"] for item in scope["output_files"]} == {"selected_host"}
    for path in paths:
        path.unlink()
    restored, files = runtime.provider_native_image_output(
        worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}, raw)
    assert restored == expected
    assert {path.name for path, _mime, _snapshot in files} == set(names)
    assert all((snapshot.root / snapshot.relative).read_bytes() == data for _path, _mime, snapshot in files)


@pytest.mark.parametrize("restriction", ["read_only", "workspace", "unknown", "disabled", "docker", "implicit"])
def test_outside_selection_does_not_widen_restricted_or_implicit_authority(tmp_path, restriction):
    runtime, worker, info, scope = full_host(tmp_path)
    bundle = json.loads(worker["bootstrap_bundle_json"])
    if restriction == "disabled":
        bundle["provider_capabilities"]["native_tools"] = False
    elif restriction == "docker":
        worker["execution_mode"] = "docker"
    elif restriction == "implicit":
        bundle.pop("access_mode")
    else:
        bundle["access_mode"] = restriction
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    path = tmp_path / "outside.csv"
    path.write_text("selected")
    assert project(runtime, worker, info, f"[File]({path})", scope) == "File"
    assert scope["rejected_output_files"] == [{"name": path.name, "code": "source_root_unsupported"}]


def test_unselected_paths_and_verified_native_edit_discovery_keep_existing_authority(tmp_path):
    runtime, worker, info, scope = full_host(tmp_path)
    path = tmp_path / "outside.csv"
    path.write_text("selected")
    raw = f"Checked `{path}`."
    assert project(runtime, worker, info, raw, scope) == profile_runtime._redact_text(raw)
    assert "output_files" not in scope
    runtime._capture_native_output_files(worker, info.workspace_dir, scope, scope["run_id"], "Checked.",
        native_edits=((str(path), hashlib.sha256(path.read_bytes()).hexdigest()),))
    assert not scope["output_files"]


@pytest.mark.parametrize("kind", [
    "hidden", "reserved", "traversal", "symlink", "hardlink", "fifo", "directory",
    "other_profile", "other_worker", "control", "account", "database", "link_database", "link_sidecar", "declared",
])
def test_selected_host_files_keep_private_roots_and_file_identity_guards(tmp_path, monkeypatch, kind):
    runtime, worker, info, scope = full_host(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "result.csv"
    if kind == "hidden":
        path = outside / ".private" / "result.csv"
    elif kind == "reserved":
        path = outside / "AGENTS.md"
    elif kind == "traversal":
        path = outside / ".." / "result.csv"
    elif kind == "other_profile":
        path = runtime.base_dir / "other_native_runtime" / "workers" / "foreign" / "result.csv"
    elif kind == "other_worker":
        path = runtime._workspace_dir("foreign") / "result.csv"
    elif kind in {"control", "account", "database", "declared"}:
        root = tmp_path / (kind + "-state")
        variable = {"control": "XPERFECT_CONTROL_ROOT", "account": "GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT",
                    "database": "WPR_DB_PATH", "declared": "GLASSHIVE_PROTECTED_SOURCE_ROOTS"}[kind]
        monkeypatch.setenv(variable, str(root / "state.sqlite3" if kind == "database" else root))
        path = root / ("state.sqlite3" if kind == "database" else "result.csv")
    elif kind in {"link_database", "link_sidecar"}:
        monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(outside / "links.sqlite3"))
        path = outside / ("links.sqlite3-wal" if kind == "link_sidecar" else "links.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        target = outside / "original.csv"
        target.write_text("private")
        path.symlink_to(target)
    elif kind == "hardlink":
        target = outside / "original.csv"
        target.write_text("private")
        os.link(target, path)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("private")
    assert project(runtime, worker, info, f"[File](<{path}>)", scope) == "File"
    assert not scope["output_files"] and len(scope["rejected_output_files"]) == 1


def test_directory_alias_keeps_selected_name_and_private_replay(tmp_path):
    runtime, worker, info, scope = full_host(tmp_path)
    real = tmp_path / "ordinary"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    path = alias / "selected.csv"
    path.write_bytes(b"unchanged")
    raw = f"[File](<{path}>)"
    assert "artifact_sha256:" in project(runtime, worker, info, raw, scope)
    assert scope["output_files"][0]["root_path"] == path.relative_to(path.anchor).as_posix()
    path.unlink()
    alias.unlink()
    restored, files = runtime.provider_native_image_output(
        worker, {"run_id": scope["run_id"], "worker_id": worker["worker_id"]}, raw)
    assert "artifact_sha256:" in restored and files[0][0] == path


def test_os_temporary_directory_alias_selects_an_ordinary_file(tmp_path):
    runtime, worker, info, scope = full_host(tmp_path)
    if not Path("/tmp").is_dir():
        pytest.skip("The OS has no /tmp alias")
    with tempfile.TemporaryDirectory(prefix="selected-file-", dir="/tmp") as directory:
        path = Path(directory) / "report.csv"
        path.write_bytes(b"selected")
        assert "artifact_sha256:" in project(runtime, worker, info, f"[File]({path})", scope)
        assert not scope["rejected_output_files"]


@pytest.mark.parametrize("source_kind", ["workspace", "managed_tmp"])
def test_declared_protected_root_preserves_existing_admitted_selections(tmp_path, monkeypatch, source_kind):
    runtime, worker, info, scope = full_host(tmp_path)
    monkeypatch.setenv("GLASSHIVE_PROTECTED_SOURCE_ROOTS", str(tmp_path))
    root = Path(info.workspace_dir) if source_kind == "workspace" else runtime._home_dir(worker["worker_id"]) / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "result.csv"
    path.write_text("selected")
    assert "artifact_sha256:" in project(runtime, worker, info, f"[File]({path})", scope)
    assert scope["output_files"][0]["source_root"] == source_kind


@pytest.mark.parametrize("variable", ["WPR_UPLOADS_ROOT", "WPR_LIBRECHAT_UPLOADS_ROOT", "WPR_BOOTSTRAP_SOURCE_ROOTS"])
@pytest.mark.parametrize("alias", [False, True])
def test_shared_upload_source_roots_cannot_publish_other_owner_files(tmp_path, monkeypatch, variable, alias):
    runtime, worker, info, scope = full_host(tmp_path)
    shared = tmp_path / "shared-inputs"
    shared.mkdir()
    value = str(shared)
    if variable == "WPR_BOOTSTRAP_SOURCE_ROOTS":
        value = str(tmp_path / "another-input-root") + os.pathsep + value
    elif variable == "WPR_LIBRECHAT_UPLOADS_ROOT":
        monkeypatch.setenv("WPR_UPLOADS_ROOT", str(tmp_path / "another-input-root"))
    monkeypatch.setenv(variable, value)
    selected = shared
    if alias:
        selected = tmp_path / "ordinary-alias"
        selected.symlink_to(shared, target_is_directory=True)
    path = selected / "private.csv"
    path.write_text("other owner input")
    assert project(runtime, worker, info, f"[File]({path})", scope) == "File"
    assert not scope["output_files"]


@pytest.mark.parametrize("variable", ["WPR_DB_PATH", "XPERFECT_STORAGE_REGISTRY_PATH", "GLASSHIVE_LINK_REF_STATE_PATH"])
def test_database_boundary_keeps_ordinary_sibling_files_legal(tmp_path, monkeypatch, variable):
    runtime, worker, info, scope = full_host(tmp_path)
    ordinary = tmp_path / "documents"
    ordinary.mkdir()
    monkeypatch.setenv(variable, str(ordinary / "runtime.sqlite3"))
    path = ordinary / "report.csv"
    path.write_text("selected")
    assert "artifact_sha256:" in project(runtime, worker, info, f"[File]({path})", scope)
    assert not scope["rejected_output_files"]


@pytest.mark.parametrize("private_kind", ["directory", "database", "sidecar"])
def test_case_insensitive_alias_cannot_evade_private_identity(tmp_path, monkeypatch, private_kind):
    runtime, worker, info, scope = full_host(tmp_path)
    root = tmp_path / "PrivateControl"
    root.mkdir()
    if private_kind == "directory":
        monkeypatch.setenv("GLASSHIVE_PROTECTED_SOURCE_ROOTS", str(root))
        path = root / "runtime.env"
    else:
        monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(root / "Links.sqlite3"))
        path = root / ("Links.sqlite3-wal" if private_kind == "sidecar" else "Links.sqlite3")
    path.write_text("private")
    alias = root.with_name("privatecontrol") / path.name.lower()
    if not alias.exists():
        pytest.skip("The filesystem is case sensitive")
    assert project(runtime, worker, info, f"[File]({alias})", scope) == "File"
    assert not scope["output_files"]


@pytest.mark.parametrize("private_origin", ["hidden", "configured_control"])
def test_alias_cannot_hide_private_origin(tmp_path, monkeypatch, private_origin):
    runtime, worker, info, scope = full_host(tmp_path)
    private = tmp_path / (".private" if private_origin == "hidden" else "private-control")
    private.mkdir()
    if private_origin == "configured_control":
        monkeypatch.setenv("XPERFECT_CONTROL_ROOT", str(private))
    alias = tmp_path / "ordinary"
    alias.symlink_to(private, target_is_directory=True)
    path = alias / "result.csv"
    path.write_text("private")
    assert project(runtime, worker, info, f"[File]({path})", scope) == "File"
    assert not scope["output_files"]


@pytest.mark.parametrize("mutation", ["replace", "symlink", "rewrite"])
def test_selected_host_source_change_during_snapshot_cannot_publish(tmp_path, monkeypatch, mutation):
    runtime, worker, info, scope = full_host(tmp_path)
    path = tmp_path / "outside.csv"
    path.write_bytes(b"selected exact bytes")
    original = deliverables._stream_native_file

    def change_after_read(handle, metadata, destination=None):
        result = original(handle, metadata, destination)
        if mutation == "rewrite":
            path.write_bytes(b"other bytes")
        elif mutation == "symlink":
            path.unlink()
            path.symlink_to(tmp_path / "private.csv")
        else:
            path.unlink()
            path.write_bytes(b"selected exact bytes")
        return result

    monkeypatch.setattr(deliverables, "_stream_native_file", change_after_read)
    assert project(runtime, worker, info, f"[File]({path})", scope) == "File"
    assert not scope["output_files"]
