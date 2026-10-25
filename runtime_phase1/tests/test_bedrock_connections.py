"""Scoped configuration and native projection; these tests make no cloud calls."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.bedrock_connections import (
    CREDENTIAL_FILE, account_config, connect, connection_config, read_credentials, selected_model,
)
from workers_projects_runtime.control_plane import ControlPlaneError, ControlPlaneStore, profile_provider_account_issue
from workers_projects_runtime.mission_provider_accounts import MissionProviderAccountBinder, apply_bound_provider_account_environment
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager
from workers_projects_runtime.provider_credential_artifacts import credential_artifacts
from workers_projects_runtime.native_credential_exec import credential_environment
from workers_projects_runtime.conversation_provider import ChatCompletionRequest
from fastapi import HTTPException
from workers_projects_runtime.service import BackgroundWorkerConfigurationError


def config(region="us-east-1", model="declared-model-a", **updates):
    return {"region": region, "model_ids": [model, model + "-alternate"], "default_model": model,
            "credential_mode": "protected_file", "model_provider": "anthropic", **updates}


def account(store, declared, owner="owner-a"):
    return store.create_provider_account(tenant_id="tenant-a", owner_id=owner, provider="claude",
        label="Research account", auth_method="bedrock", platform_support="supported",
        secret_locator="native-home://auto", connection_config=declared)


def test_two_connections_preserve_region_model_and_owner_without_cloud_discovery(tmp_path, monkeypatch):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    first = account(store, config())
    second = account(store, config("eu-west-1", "declared-model-b"))
    monkeypatch.setenv("WPR_CLAUDE_CODE_PROVIDER_MODEL", "legacy-model")
    for connected, expected in [(first, "declared-model-a"), (second, "declared-model-b")]:
        declared = account_config(connected)
        assert selected_model({}, declared) == expected
        assert selected_model({"model": expected + "-alternate"}, declared) == expected + "-alternate"
        with pytest.raises(ControlPlaneError, match="declared"):
            selected_model({"model": "undeclared-model"}, declared)
        assert "secret_locator" not in connected
        assert store.get_provider_account(account_id=connected["account_id"], tenant_id="tenant-a", owner_id="owner-b") is None
    assert first["connection_config"]["region"] != second["connection_config"]["region"]
    assert profile_provider_account_issue("direct-anthropic", first)
    assert profile_provider_account_issue("claude-code", first) == ""


@pytest.mark.parametrize("change", [
    {"region": "https://example.invalid"}, {"model_ids": []}, {"model_ids": ["same", "same"]},
    {"model_ids": ["bad\nmodel"]}, {"default_model": "not-declared"}, {"model_provider": "other"},
    {"credential_mode": "ambient_personal"}, {"extra": "invalid"},
])
def test_invalid_declared_configuration_refuses_without_fallback(change):
    with pytest.raises(ControlPlaneError):
        connection_config(config(**change))


def test_protected_credentials_bind_to_one_native_host_and_never_project_subscription(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    connected = account(store, config())
    root = tmp_path / "accounts"; root.mkdir(mode=0o700)
    homes = ProviderAccountHomeManager(root)
    result = connect(store, homes, account_id=connected["account_id"], tenant_id="tenant-a", owner_id="owner-a",
        value=json.dumps({"AWS_BEARER_TOKEN_BEDROCK": "synthetic-bearer"}))
    assert result["verification"] == "configuration_only"
    assert result["last_verified_at"] is None
    binder = MissionProviderAccountBinder(db_path=str(database), home_root=root)
    worker = {"worker_id": "worker-a", "profile": "claude-code", "tenant_id": "tenant-a", "owner_id": "owner-a",
        "model": "declared-model-a-alternate", "execution_mode": "host",
        "bootstrap_bundle_json": {"provider_account": {"account_id": connected["account_id"], "policy": "personal_required"}}}
    with binder.bind(worker, runtime_name="claude-code", run_id="run-a", timeout_sec=10) as bound:
        environment = {"ANTHROPIC_API_KEY": "wrong", "CLAUDE_CODE_OAUTH_TOKEN": "wrong",
            "AWS_ACCESS_KEY_ID": "wrong", "AWS_PROFILE": "wrong", "ANTHROPIC_BEDROCK_BASE_URL": "https://wrong.invalid"}
        apply_bound_provider_account_environment(bound, environment, runtime_name="claude-code")
        assert environment["AWS_BEARER_TOKEN_BEDROCK"] == "synthetic-bearer"
        assert environment["AWS_REGION"] == "us-east-1"
        assert environment["AWS_EC2_METADATA_DISABLED"] == "true"
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "ANTHROPIC_BEDROCK_BASE_URL"):
            assert key not in environment
    assert store.active_provider_account_lease(connected["account_id"]) is None


def test_container_artifact_is_private_and_read_only_with_no_secret_in_selectors(tmp_path, monkeypatch):
    artifacts = credential_artifacts("claude", "bedrock")
    assert len(artifacts) == 1 and artifacts[0].refresh is False
    member = tmp_path / "member"; member.mkdir(mode=0o700)
    credential = member / "bedrock.json"
    credential.write_text(json.dumps({"AWS_ACCESS_KEY_ID": "synthetic-id", "AWS_SECRET_ACCESS_KEY": "synthetic-secret"}))
    credential.chmod(0o600)
    monkeypatch.setenv("HOME", str(member))
    assert credential_environment({"XPERFECT_BEDROCK_CREDENTIALS": str(credential)}) == {
        "AWS_ACCESS_KEY_ID": "synthetic-id", "AWS_SECRET_ACCESS_KEY": "synthetic-secret"}
    link = member / "link.json"; link.symlink_to(credential)
    with pytest.raises(ControlPlaneError):
        read_credentials(link)
    credential.chmod(0o644)
    with pytest.raises(ControlPlaneError):
        read_credentials(credential)


def test_bedrock_api_is_configuration_only_and_rejects_bad_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT", str(tmp_path / "accounts"))
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-token")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    client = TestClient(app)
    headers = {"Authorization": "Bearer synthetic-token"}
    response = client.post("/v1/provider-accounts", headers=headers, json={"provider": "claude", "label": "Bedrock",
        "auth_method": "bedrock", "platform_support": "server_evaluated", "connection_config": config()})
    assert response.status_code == 201, response.text
    connected = response.json(); account_id = connected["account_id"]
    response = client.post(f"/v1/provider-accounts/{account_id}/credentials", headers=headers,
        json={"value": json.dumps({"AWS_BEARER_TOKEN_BEDROCK": "synthetic-token"})})
    assert response.status_code == 200, response.text
    assert response.json()["verification"] == "configuration_only"
    public = client.get("/v1/provider-accounts", headers=headers).json()
    assert "synthetic-token" not in json.dumps(public)
    assert "secret_locator" not in json.dumps(public)
    rejected = client.post("/v1/provider-accounts", headers=headers, json={"provider": "codex", "label": "Invalid",
        "auth_method": "bedrock", "platform_support": "server_evaluated", "connection_config": config()})
    assert rejected.status_code in {400, 409}


def test_conversation_uses_only_declared_contracts_and_binds_exact_owner_connection(tmp_path):
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    store = app.state.control_plane
    declared = config(model_contracts={"declared-model-a": {
        "context_window": 200000, "effort_choices": ["default", "low"], "recommended_effort": "default"},
        "declared-model-a-alternate": {"context_window": 100000, "effort_choices": ["default"], "recommended_effort": "default"}})
    connected = account(store, declared)
    store.update_provider_account_status(account_id=connected["account_id"], tenant_id="tenant-a", owner_id="owner-a", status="ready")
    provider = app.state.conversation_provider
    catalog = provider.models_payload(tenant_id="tenant-a", owner_id="owner-a")["data"]
    selected = next(item for item in catalog if item.get("connection_id") == connected["account_id"])
    assert selected["native_model"] == "declared-model-a"
    assert selected["context_window"] == 200000
    assert selected["effort_choices"] == ["low", "default"]
    model = provider._model(selected["id"], tenant_id="tenant-a", owner_id="owner-a")
    payload = ChatCompletionRequest(model=model.id, messages=[{"role": "user", "content": "Read the declared input."}],
        metadata={"tenant_id": "tenant-a", "owner_id": "owner-a"})
    bundle = provider._native_bundle(payload, model, "default")
    assert bundle["provider_model"] == "declared-model-a"
    assert bundle["provider_account"]["account_id"] == connected["account_id"]
    with pytest.raises(HTTPException):
        provider._model(model.id, tenant_id="tenant-a", owner_id="owner-b")
    with pytest.raises(HTTPException):
        provider._model(model.id + "-unlisted", tenant_id="tenant-a", owner_id="owner-a")
    conflict = payload.model_copy(update={"metadata": payload.metadata.model_copy(update={
        "bootstrap_bundle": {"connection_id": "different-account"}})})
    with pytest.raises(HTTPException) as error:
        provider._native_bundle(conflict, model, "default")
    assert error.value.status_code == 409


def test_connected_chat_setup_and_child_route_share_declared_model_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    monkeypatch.delenv("GLASSHIVE_COORDINATOR_CONFIG_JSON", raising=False)
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    store = app.state.control_plane
    declared = config(model_contracts={"declared-model-a": {
        "context_window": 200000, "effort_choices": ["default", "low"], "recommended_effort": "default"},
        "declared-model-a-alternate": {"context_window": 100000, "effort_choices": ["default"], "recommended_effort": "default"}})
    connected = account(store, declared)
    store.update_provider_account_status(account_id=connected["account_id"], tenant_id="tenant-a", owner_id="owner-a", status="ready")
    client = TestClient(app)
    headers = {"Authorization": "Bearer synthetic-token", "X-Viventium-Tenant-Id": "tenant-a", "X-Viventium-User-Id": "owner-a"}
    created = client.post("/v1/coordinator/conversations", headers=headers, json={"account_id": connected["account_id"]})
    assert created.status_code == 200, created.text
    saved = app.state.store._connect()
    with saved as conn:
        row = conn.execute("SELECT config_json FROM coordinator_conversations WHERE conversation_id=?", (created.json()["conversation_id"],)).fetchone()
    selected = json.loads(row["config_json"])
    assert selected["scope"]["connection_id"] == connected["account_id"]
    from workers_projects_runtime.bedrock_connections import conversation_model_id
    alternate_id = conversation_model_id(connected["account_id"], "declared-model-a-alternate")
    alternate = client.post("/v1/coordinator/conversations", headers=headers, json={
        "account_id": connected["account_id"], "model_id": alternate_id})
    assert alternate.status_code == 200, alternate.text
    native, bundle = app.state.service._configured_parallel_worker_route("claude-code", "docker", {
        "launch_authority": {"worker_model": selected["model"], "worker_reasoning_effort": "default"}},
        tenant_id="tenant-a", owner_id="owner-a")
    assert native == "declared-model-a"
    assert bundle["provider_account"]["account_id"] == connected["account_id"]
    with pytest.raises(BackgroundWorkerConfigurationError, match="different connection"):
        app.state.service._configured_parallel_worker_route("claude-code", "docker", {
            "connection_id": "different-account", "launch_authority": {"worker_model": selected["model"]}},
            tenant_id="tenant-a", owner_id="owner-a")
    foreign = {**headers, "X-Viventium-User-Id": "owner-b"}
    assert client.post("/v1/coordinator/conversations", headers=foreign, json={"account_id": connected["account_id"]}).status_code == 409
    bare = account(store, config("eu-west-1", "bare-native-model"))
    store.update_provider_account_status(account_id=bare["account_id"], tenant_id="tenant-a", owner_id="owner-a", status="ready")
    refused = client.post("/v1/coordinator/conversations", headers=headers, json={"account_id": bare["account_id"]})
    assert refused.status_code == 409
