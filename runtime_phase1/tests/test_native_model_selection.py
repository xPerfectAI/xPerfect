from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.native_model_selection import ModelConfigurationRequired, selected_grok_model
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.store import Store


def _native_cli(tmp_path):
    binary = tmp_path / "grok-synthetic"
    binary.write_text("#!/bin/sh\n[ \"$1\" = models ] || exit 2\nprintf 'Default model: grok-native-b\\nAvailable models:\\n  * grok-native-b (default)\\n  - grok-native-a\\n'\n")
    binary.chmod(0o700)
    return binary


def test_owner_model_choice_is_exact_durable_and_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD", raising=False)
    monkeypatch.setenv("WPR_GROK_BIN", str(_native_cli(tmp_path)))
    path = str(tmp_path / "runtime.db")
    with TestClient(create_app(path, runtime_backend="stub", runtime=StubRuntime())) as client:
        missing = client.get("/v1/native-models/grok-build")
        assert missing.status_code == 200
        assert missing.json()["status"] == "model_configuration_required"
        assert missing.json()["models"] == ["grok-native-b", "grok-native-a"]
        assert client.patch("/v1/preferences", json={"grok_model": "grok-not-offered"}).status_code == 400
        assert client.patch("/v1/preferences", json={"grok_model": " grok-native-a"}).status_code == 400
        saved = client.patch("/v1/preferences", json={"grok_model": "grok-native-a"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["grok_model"] == "grok-native-a"
        selected = client.get("/v1/native-models/grok-build").json()
        assert (selected["effective_model"], selected["source"], selected["status"]) == (
            "grok-native-a", "owner", "ready",
        )
    store = Store(path)
    assert selected_grok_model(store, "local", "demo-owner") == ("grok-native-a", "owner")
    with pytest.raises(ModelConfigurationRequired):
        selected_grok_model(store, "local", "another-owner")
    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = store
    assert provider._model("grok-build:grok-native-a", owner_id="demo-owner").native_model == "grok-native-a"
    with pytest.raises(ModelConfigurationRequired):
        provider._model("grok-build:grok-native-a", owner_id="another-owner")
    store.close()


def test_deployment_model_wins_without_rewriting_owner_choice(tmp_path, monkeypatch):
    store = Store(tmp_path / "runtime.db")
    store.upsert_user_preferences(tenant_id="local", owner_id="owner", grok_model="grok-native-a")
    monkeypatch.setenv("WPR_MODEL_GROK_BUILD", "grok-native-b")
    assert selected_grok_model(store, "local", "owner") == ("grok-native-b", "deployment")
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD")
    assert selected_grok_model(store, "local", "owner") == ("grok-native-a", "owner")
    assert store.get_user_preferences("local", "owner")["grok_model"] == "grok-native-a"
    store.close()
