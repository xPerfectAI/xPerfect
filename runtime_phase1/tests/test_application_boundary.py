"""Neutral application transport and the retained compatibility boundary."""
from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from workers_projects_runtime import bootstrap, runtime_env
from workers_projects_runtime.application_boundary import (
    BoundaryConflict, compatible_value, launch_authority_for, opaque_application_context,
)
from workers_projects_runtime.conversation_provider import (
    ChatCompletionRequest, ProviderAuthContext, GlassHiveOptions,
    _decode_bootstrap_bundle, _header, _hydrate_metadata, _resolve_workspace,
)
from workers_projects_runtime.upload_projection import project_upload_files


def request(headers=None):
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                    "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]})


def test_neutral_env_file_never_discovers_an_application(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign.env"
    foreign.write_text("GLASSHIVE_SIGNED_LINK_SECRET=foreign-canary\n")
    selected = tmp_path / "runtime.env"
    selected.write_text("GLASSHIVE_SIGNED_LINK_SECRET=selected-canary\nVIVENTIUM_SCHEDULER_SECRET=unused-canary\n")
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(foreign))
    monkeypatch.setenv("WPR_RUNTIME_ENV_FILE", str(selected))
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)
    monkeypatch.delenv("VIVENTIUM_SCHEDULER_SECRET", raising=False)
    monkeypatch.setattr(runtime_env, "_candidate_env_files", lambda: pytest.fail("legacy discovery"))
    monkeypatch.setattr(runtime_env, "_local_checkout_librechat_uploads_root", lambda: pytest.fail("sibling discovery"))
    assert runtime_env.load_runtime_env() == {"GLASSHIVE_SIGNED_LINK_SECRET": "selected-canary"}
    assert "VIVENTIUM_SCHEDULER_SECRET" not in os.environ
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET")
    assert runtime_env.load_viventium_runtime_env({"GLASSHIVE_SIGNED_LINK_SECRET"}) == {"GLASSHIVE_SIGNED_LINK_SECRET": "selected-canary"}


def test_neutral_env_requires_selection_and_respects_process_values(tmp_path, monkeypatch):
    selected = tmp_path / "runtime.env"
    selected.write_text("GLASSHIVE_SIGNED_LINK_SECRET=file-canary\n")
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(selected))
    monkeypatch.delenv("WPR_RUNTIME_ENV_FILE", raising=False)
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)
    assert runtime_env.load_runtime_env({"GLASSHIVE_SIGNED_LINK_SECRET"}) == {}
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "process-canary")
    assert runtime_env.load_runtime_env({"GLASSHIVE_SIGNED_LINK_SECRET"}, env_file=selected) == {}
    assert os.environ["GLASSHIVE_SIGNED_LINK_SECRET"] == "process-canary"


def test_explicit_neutral_uploads_keep_owner_scope_and_exact_bytes(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    owner = uploads / "owner-a"
    owner.mkdir(parents=True)
    data = b"exact ordered authorized file\x00\xff"
    source = owner / "file-a__document.bin"
    source.write_bytes(data)
    alias = tmp_path / "upload-alias"
    alias.symlink_to(uploads, target_is_directory=True)
    legacy = tmp_path / "foreign-uploads"
    legacy.mkdir()
    monkeypatch.setenv("WPR_UPLOADS_ROOT", str(alias))
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(legacy))
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_ROOTS", raising=False)
    runtime_env.load_runtime_env({"WPR_UPLOADS_ROOT", "WPR_BOOTSTRAP_SOURCE_ROOTS"})
    files = project_upload_files({"request_files": [{"file_id": "file-a", "filename": "document.bin",
        "filepath": "/uploads/owner-a/file-a__document.bin"}]},
        tenant_id="tenant-a", owner_id="owner-a", storage_owner_id="owner-a")
    assert len(files) == 1
    entry = files[0]
    worker = {"tenant_id": "tenant-a", "owner_id": "owner-a"}
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "1")
    assert bootstrap.resolve_authorized_bootstrap_source_path(entry, entry["source_path"], worker).read_bytes() == data
    with pytest.raises(PermissionError):
        bootstrap.resolve_authorized_bootstrap_source_path(entry, entry["source_path"], {**worker, "owner_id": "other-owner"})
    assert os.environ["WPR_UPLOADS_ROOT"] == str(uploads)
    assert os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"] == str(uploads)


@pytest.mark.parametrize("carrier", ["launch_authority", "viventium_launch_authority"])
def test_authority_alias_retains_typed_envelope(carrier):
    value = {"version": 1, "kind": "conversation_orchestrator", "execution_mode": "host"}
    assert launch_authority_for({carrier: value}) == value
    assert launch_authority_for({"launch_authority": value, "viventium_launch_authority": dict(value)}) == value


def test_conflicting_authority_has_safe_failure():
    with pytest.raises(BoundaryConflict) as exc:
        launch_authority_for({"launch_authority": {"private-canary": "a"},
                              "viventium_launch_authority": {"private-canary": "b"}})
    assert "private-canary" not in str(exc.value)


@pytest.mark.parametrize("suffix", ["user-id", "actor-kind", "origin", "conversation-id", "message-id",
    "surface", "input-mode", "main-context-protocol", "native-invocation-id", "foreground-attention"])
def test_context_header_alias_conflict_fails_closed(suffix):
    canonical, legacy = f"x-glasshive-{suffix}", f"x-viventium-{suffix}"
    assert _header(request({canonical: "same"}), canonical) == "same"
    assert _header(request({legacy: "same"}), canonical) == "same"
    assert _header(request({canonical: "same", legacy: "same"}), canonical) == "same"
    with pytest.raises(HTTPException) as exc:
        _header(request({canonical: "private-a", legacy: "private-b"}), canonical)
    assert exc.value.status_code == 400
    assert "private" not in str(exc.value.detail)


def auth(trusted=False):
    return ProviderAuthContext(principal_id="owner-a", tenant_id="tenant-a", trust_identity_headers=trusted,
                               default_access="workspace", allow_full_access=False)


def test_neutral_identity_uses_same_authenticated_scope_and_full_context():
    payload = ChatCompletionRequest(model="synthetic-model", messages=[{"role": "user", "content": "exact original request"}],
        metadata={"owner_id": "owner-a", "turn_context": "unabridged application context"})
    value = _hydrate_metadata(payload, request({"x-glasshive-user-id": "owner-a", "x-glasshive-conversation-id": "thread-a",
        "x-glasshive-message-id": "message-a", "x-glasshive-surface": "custom-channel"}), auth())
    assert value.metadata.owner_id == "owner-a"
    assert value.metadata.conversation_id == "thread-a"
    assert value.metadata.turn_context == "unabridged application context"
    assert value.messages == payload.messages
    with pytest.raises(HTTPException) as exc:
        _hydrate_metadata(payload.model_copy(update={"metadata": None}), request({"x-glasshive-user-id": "owner-b"}), auth())
    assert exc.value.status_code == 403


def test_neutral_identity_does_not_grant_system_or_full_access():
    payload = ChatCompletionRequest(model="synthetic-model", messages=[{"role": "user", "content": "hello"}])
    for headers in ({"x-glasshive-actor-kind": "system"}, {"x-glasshive-access": "full"}):
        with pytest.raises(HTTPException) as exc:
            _hydrate_metadata(payload, request(headers), auth())
        assert exc.value.status_code == 403


def test_neutral_bootstrap_signature_keeps_tamper_protection(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_SECRET", "synthetic-key")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET", raising=False)
    bundle = {"application_context_format": "opaque_v1", "agents_md": "exact application authority"}
    encoded = base64.b64encode(json.dumps(bundle).encode()).decode()
    issued = str(int(time.time()))
    signature = hmac.new(b"synthetic-key", f"v1\n{issued}\n{encoded}".encode(), hashlib.sha256).hexdigest()
    headers = {"x-glasshive-bootstrap-bundle-b64": encoded, "x-glasshive-bootstrap-timestamp": issued,
               "x-glasshive-bootstrap-signature": signature}
    assert _decode_bootstrap_bundle(request(headers)) == bundle
    headers["x-glasshive-bootstrap-bundle-b64"] = encoded + "AA"
    with pytest.raises(HTTPException) as exc:
        _decode_bootstrap_bundle(request(headers))
    assert exc.value.status_code == 403


def test_prepared_opaque_context_preserves_bytes_without_domain_parser(tmp_path, monkeypatch):
    authority = "Exact application authority\n<viventium_feeling_state>opaque client text without closing tag"
    tail = "exact declared tail"
    bundle = {"application_context_format": "opaque_v1", "agents_md": authority,
              "developer_instructions": f"application header\n\n{tail}", "declared_developer_instruction_tail": tail}
    monkeypatch.setattr(bootstrap, "_split_viventium_feeling_capsules", lambda *_: pytest.fail("domain parser called"))
    canonical = bootstrap.canonicalize_viventium_feeling_projection(bundle)
    assert canonical == bundle
    assert authority in bootstrap.glasshive_project_agents_md(canonical)
    projected = bootstrap.native_instruction_before_tail(bundle, bundle["developer_instructions"], "native capability fact")
    assert projected.endswith(tail)
    assert "native capability fact" in projected
    bootstrap.apply_bootstrap(home_dir=tmp_path / "home", workspace_dir=tmp_path / "workspace", runtime_name="codex-cli",
        worker={"bootstrap_profile": "none", "bootstrap_bundle_json": bundle},
        copy_file=lambda source, target: None, copy_tree=lambda source, target: None)
    assert authority in (tmp_path / "workspace" / "AGENTS.md").read_text()


def test_opaque_without_tail_preserves_unknown_domain_text(monkeypatch):
    monkeypatch.setattr(bootstrap, "_split_viventium_feeling_capsules", lambda *_: pytest.fail("domain parser called"))
    text = "<viventium_feeling_state>not a managed capsule"
    bundle = {"application_context_format": "opaque_v1"}
    assert bootstrap.native_instruction_before_tail(bundle, text, "fact") == text + "\n\nfact"


def test_legacy_projection_still_validates_and_unknown_format_rejects():
    with pytest.raises(ValueError, match="Malformed"):
        bootstrap.canonicalize_viventium_feeling_projection({"agents_md": "<viventium_feeling_state>missing close"})
    with pytest.raises(ValueError, match="Unsupported"):
        opaque_application_context({"application_context_format": "unknown"})
    with pytest.raises(ValueError, match="also request legacy"):
        bootstrap.canonicalize_viventium_feeling_projection({"application_context_format": "opaque_v1", "viventium_feelings_projection": {}})


def test_neutral_custom_workspace_has_no_implicit_application_folder(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(workspace))
    monkeypatch.delenv("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS", raising=False)
    monkeypatch.delenv("VIVENTIUM_LIFE_DIR", raising=False)
    assert _resolve_workspace(GlassHiveOptions(workspace={"mode": "custom", "path": str(workspace)})) == workspace
    with pytest.raises(HTTPException) as exc:
        _resolve_workspace(GlassHiveOptions(workspace={"mode": "custom", "path": str(foreign)}))
    assert exc.value.status_code == 403


def test_generic_carrier_and_env_seams_have_no_client_dependency():
    # Focused seam check: compatibility wrappers elsewhere are intentionally retained.
    for function in (compatible_value, opaque_application_context, runtime_env.load_runtime_env,
                     runtime_env._load_env_files, runtime_env._repair_explicit_upload_root):
        tree = ast.parse(inspect.getsource(function))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert "viventium" not in ast.unparse(node).lower()
                assert "librechat" not in ast.unparse(node).lower()
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not node.value.startswith(("VIVENTIUM_", "WPR_LIBRECHAT_", "x-viventium-"))


def test_app_off_startup_and_completion_from_independent_directory(tmp_path):
    """Use the documented app factory, a new DB and stub substrate, no client server."""
    script = '''
import json, os, sys
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
app = create_app("state.sqlite3", runtime_backend="stub", runtime=StubRuntime())
with TestClient(app) as client:
    assert client.get("/health").status_code == 200
    admin = {"Authorization": "Bearer synthetic-admin"}
    project = client.post("/v1/projects", headers=admin,
        json={"owner_id": "owner-a", "title": "Independent app", "goal": "Synthetic work"})
    assert project.status_code == 201, project.text
    assert len(client.get("/v1/projects", headers=admin).json()["items"]) == 1
    response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer synthetic-provider", "x-glasshive-user-id": "owner-a"},
        json={"model": "codex-cli:gpt-6.1-sol", "messages": [
            {"role": "developer", "content": "Exact independent application instruction."},
            {"role": "user", "content": "Synthetic complete request."}],
            "metadata": {"conversation_id": "thread-a", "message_id": "turn-a", "turn_context": "Full original context"}})
    assert response.status_code == 200, response.text
    sessions = app.state.store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    assert sessions[0]["workspace_dir"] == os.getcwd()
    assert "compiled_prompt_contract" not in sys.modules
    assert not any(key.startswith("VIVENTIUM_") for key in os.environ)
    print(json.dumps({"health": 200, "create_project": 201, "completion": 200,
                      "sessions": 1, "host_prompt_runtime_imported": False}))
'''
    source = Path(__file__).resolve().parents[1] / "src"
    kept = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT",
    }}
    kept.update({"PYTHONPATH": str(source), "WPR_API_TOKEN": "synthetic-admin",
        "GLASSHIVE_PROVIDER_API_KEY": "synthetic-provider", "GLASSHIVE_PROVIDER_PRINCIPAL_ID": "owner-a",
        "GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE": str(tmp_path), "GLASSHIVE_HOST_WORKERS_ENABLED": "1",
        "GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED": "false", "GLASSHIVE_LINK_REF_STATE_PATH": str(tmp_path / "links.sqlite3"),
        "GLASSHIVE_HOST_MIN_AVAILABLE_MEMORY_MB": "0", "WPR_HOST_MIN_AVAILABLE_MEMORY_MB": "0",
        "WPR_HOST_MIN_AVAILABLE_DISK_MB": "0"})
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=kept,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1])["host_prompt_runtime_imported"] is False


@pytest.mark.parametrize("grant", [None, [], {"allow_worker_target": 1}, {"url": "https://receiver.example.test"},
    {"destinations": [{"destination_id": "destination-a", "request_data": {"request_ref": "request-a"}, "secret": "canary"}]},
    {"destinations": [{"destination_id": "destination-a", "request_data": {"body": "canary"}}]},
    {"destinations": [{"destination_id": "destination-a", "request_data": {"request_ref": "request-a"}, "label": 7}]},
    {"destinations": [{"destination_id": "destination-a", "request_data": {"request_ref": "request-a"}}] * 65},
])
def test_native_schedule_grant_is_typed_and_contains_no_connection_authority(grant):
    from workers_projects_runtime.application_boundary import canonical_schedule_access
    with pytest.raises(ValueError, match="Invalid schedule access grant"):
        canonical_schedule_access(grant)


def test_native_schedule_grant_is_independent_copy_and_label_is_data():
    from workers_projects_runtime.application_boundary import canonical_schedule_access
    grant = {"allow_worker_target": False, "destinations": [{"destination_id": "destination-a",
        "request_data": {"request_ref": "request-a"}, "label": "Exact application label"}]}
    result = canonical_schedule_access(grant)
    assert result == {**grant, "manage_destination_ids": [], "defaults": {}}
    result["destinations"][0]["request_data"]["request_ref"] = "changed"
    assert grant["destinations"][0]["request_data"]["request_ref"] == "request-a"


def test_native_launch_canonicalizer_retains_neutral_context_and_schedule_grant():
    from workers_projects_runtime.store import canonical_parallel_clean_room_bootstrap
    from workers_projects_runtime.application_boundary import canonical_schedule_access
    authority = {"version": 1, "kind": "conversation_orchestrator", "execution_mode": "host", "worker_model": "synthetic-model", "worker_reasoning_effort": "high"}
    grant = {"allow_worker_target": False, "destinations": [{"destination_id": "destination-a",
        "request_data": {"request_ref": "request-a"}, "label": "Exact application label"}]}
    result = canonical_parallel_clean_room_bootstrap({"launch_authority": authority,
        "application_context_format": "opaque_v1", "schedule_access": grant})
    assert launch_authority_for(result) == authority
    assert result["application_context_format"] == "opaque_v1"
    assert result["schedule_access"] == canonical_schedule_access(grant)


@pytest.mark.parametrize("defaults", [None, [], {"unknown": {}}, {"once": {"misfire_grace_seconds": -1}},
    {"once": {"misfire_grace_seconds": 604801}}, {"once": {"misfire_grace_seconds": True}},
    {"once": {"catch_up_policy": "invented"}}, {"once": {"max_catch_up_occurrences": 11}},
    {"once": {"timezone_name": "UTC"}}, {"once": {"catch_up_policy": None}}])
def test_native_timing_defaults_use_existing_bounded_fields(defaults):
    from workers_projects_runtime.application_boundary import canonical_schedule_access
    with pytest.raises(ValueError):
        canonical_schedule_access({"defaults": defaults})
