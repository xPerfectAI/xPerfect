"""Exact catalog models do not depend on an unrelated native Grok preference."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from workers_projects_runtime.conversation_provider import (
    ChatCompletionRequest, ConversationProvider, GLASSHIVE_MODELS,
)
from workers_projects_runtime.native_model_selection import ModelConfigurationRequired


@pytest.mark.parametrize("model_id", [
    "direct-xai:grok-4.7", "direct-openai:gpt-6.1-sol",
    "direct-anthropic:claude-opus-5-5", "codex-cli:gpt-6.1-sol",
    "claude-code:claude-opus-5-5", "claude-code:opus",
    "codex-cli:native-default", "grok-build:grok-4.7",
    "grok-build:grok-4.7-build-fast",
])
def test_catalog_model_does_not_read_native_grok_preference(monkeypatch, model_id):
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD", raising=False)
    provider = ConversationProvider.__new__(ConversationProvider)
    def unexpected_preference_read(*_args):
        pytest.fail("A catalog model must not read an unrelated native Grok preference")
    provider.store = SimpleNamespace(get_user_preferences=unexpected_preference_read)
    assert provider._model("  " + model_id + "  ", tenant_id="tenant-a", owner_id="owner-a") is GLASSHIVE_MODELS[model_id]


def test_dynamic_native_grok_still_uses_exact_owner_and_deployment_selection(monkeypatch):
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD", raising=False)
    provider = ConversationProvider.__new__(ConversationProvider)
    reads = []
    def preferences(tenant_id, owner_id):
        reads.append((tenant_id, owner_id))
        return {"grok_model": "grok-synthetic-owner"}
    provider.store = SimpleNamespace(get_user_preferences=preferences)
    selected = provider._model("grok-build:grok-synthetic-owner", tenant_id="tenant-a", owner_id="owner-a")
    assert selected.native_model == "grok-synthetic-owner"
    assert reads == [("tenant-a", "owner-a")]
    with pytest.raises(HTTPException) as rejected:
        provider._model("grok-build:grok-other", tenant_id="tenant-a", owner_id="owner-a")
    assert rejected.value.status_code == 400
    monkeypatch.setenv("WPR_MODEL_GROK_BUILD", "grok-synthetic-deployment")
    assert provider._model("grok-build:grok-synthetic-deployment", tenant_id="tenant-a", owner_id="owner-a").native_model == "grok-synthetic-deployment"
    assert len(reads) == 2
    assert provider._model("grok-build:grok-synthetic-history", trusted_history=True).native_model == "grok-synthetic-history"


def test_unknown_native_grok_without_selection_keeps_configuration_error(monkeypatch):
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD", raising=False)
    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = SimpleNamespace(get_user_preferences=lambda *_args: {})
    with pytest.raises(ModelConfigurationRequired):
        provider._model("grok-build:grok-synthetic-missing", tenant_id="tenant-a", owner_id="owner-a")
    with pytest.raises(HTTPException) as rejected:
        provider._model("unsupported:synthetic", tenant_id="tenant-a", owner_id="owner-a")
    assert rejected.value.status_code == 400


def test_fallback_preserves_exact_registered_model_and_effort_without_grok_lookup(monkeypatch):
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD", raising=False)
    provider = ConversationProvider.__new__(ConversationProvider)
    def unexpected_preference_read(*_args):
        pytest.fail("Registered fallback must not require native Grok selection")
    provider.store = SimpleNamespace(get_user_preferences=unexpected_preference_read)
    request = ChatCompletionRequest(model="direct-xai:grok-4.7", messages=[{"role": "user", "content": "Synthetic request"}],
        metadata={"owner_id": "owner-a", "fallback_model": "claude-code:claude-opus-5-5", "fallback_reasoning_effort": "high"})
    model, effort = provider._fallback_selection(request, GLASSHIVE_MODELS[request.model], tenant_id="tenant-a")
    assert model is GLASSHIVE_MODELS["claude-code:claude-opus-5-5"]
    assert effort == "high"
    request.metadata.fallback_model = request.model
    with pytest.raises(HTTPException) as rejected:
        provider._fallback_selection(request, GLASSHIVE_MODELS[request.model], tenant_id="tenant-a")
    assert rejected.value.status_code == 400
