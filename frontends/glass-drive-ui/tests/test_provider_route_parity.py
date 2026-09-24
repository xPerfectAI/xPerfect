import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from glass_drive_ui.server import _provider_account_selection_for_launch, create_app
from test_server import FakeRuntimeClient


@pytest.mark.parametrize("provider", ["grok", "xai"])
def test_grok_launch_keeps_the_explicit_ready_account(provider):
    selection = _provider_account_selection_for_launch(
        [{"account_id": "acct_grok", "provider": provider, "status": "ready"}],
        profile="grok-build",
        profile_providers={"grok-build": ["grok", "xai"]},
        requested_policy="personal_required",
        requested_account_id="acct_grok",
    )
    assert selection == {"policy": "personal_required", "account_id": "acct_grok"}


def test_grok_launch_rejects_an_account_for_another_provider():
    with pytest.raises(HTTPException) as error:
        _provider_account_selection_for_launch(
            [{"account_id": "acct_other", "provider": "codex", "status": "ready"}],
            profile="grok-build",
            profile_providers={"grok-build": ["grok", "xai"]},
            requested_policy="personal_required",
            requested_account_id="acct_other",
        )
    assert error.value.status_code == 409
    assert "does not match" in error.value.detail


def test_native_key_support_preserves_the_configured_enterprise_route(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_URL", "https://broker.example.invalid/inference")
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_SECRET", "synthetic-broker-secret-at-least-32-characters")
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_TENANT_ID", "synthetic")
    monkeypatch.setenv(
        "GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON",
        '[{"glasshive_tenant_id":"local","glasshive_owner_id":"demo-owner",'
        '"librechat_user_id":"user-a","proof":"operator_verified"}]',
    )
    runtime = FakeRuntimeClient()
    runtime.health_response["native_api_key_support"] = {"codex": "supported"}
    with TestClient(create_app(runtime_client=runtime)) as client:
        result = client.get("/api/control-plane")
    assert result.status_code == 200
    codex = next(item for item in result.json()["provider_options"] if item["provider"] == "codex")
    assert codex["methods"].count("api_key") == 1
    assert "enterprise_route" in codex["methods"]
