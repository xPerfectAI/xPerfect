"""Catalog discovery shares fresh harness checks within each request."""

from workers_projects_runtime import conversation_provider as provider


def _service(monkeypatch, extra=None):
    service = object.__new__(provider.ConversationProvider)
    monkeypatch.setattr(service, "_selected_grok_model", lambda *_args: extra)
    return service


def test_catalog_checks_each_harness_once_and_keeps_all_metadata(monkeypatch):
    calls = []

    def readiness(profile):
        calls.append(profile)
        return {"status": "ready", "binary_available": True, "authentication": "configured"}

    monkeypatch.setattr(provider, "_harness_readiness", readiness)
    old_payload = {"object": "list", "data": [
        model.api_payload() for model in provider.GLASSHIVE_MODELS.values()
    ]}
    calls.clear()
    actual = _service(monkeypatch).models_payload()
    assert actual == old_payload
    assert calls == list(dict.fromkeys(model.harness_profile for model in provider.GLASSHIVE_MODELS.values()))


def test_next_catalog_request_rechecks_changed_auth_and_binary(monkeypatch):
    state = {"status": "unavailable", "binary_available": False, "authentication": "required"}
    monkeypatch.setattr(provider, "_harness_readiness", lambda _profile: dict(state))
    service = _service(monkeypatch)
    first = service.models_payload()
    state.update(status="ready", binary_available=True, authentication="configured")
    second = service.models_payload()
    assert all(model["readiness"]["status"] == "unavailable" for model in first["data"])
    assert all(model["readiness"]["status"] == "ready" for model in second["data"])
    sibling = next(model for model in first["data"][1:]
                   if model["harness_profile"] == first["data"][0]["harness_profile"])
    first["data"][0]["readiness"]["status"] = "changed_by_consumer"
    assert sibling["readiness"]["status"] == "unavailable"


def test_configured_extra_grok_model_uses_same_harness_check(monkeypatch):
    extra = provider.HarnessModel(
        id="grok-build:synthetic-new-model", display_name="Synthetic model",
        harness_profile="grok-build", native_model="synthetic-new-model",
        effort_choices=("default",), recommended_effort="default", context_window=32768,
    )
    calls = []

    def readiness(profile):
        calls.append(profile)
        return {"status": "ready"}

    monkeypatch.setattr(provider, "_harness_readiness", readiness)
    actual = _service(monkeypatch, extra).models_payload()
    assert actual["data"][-1]["id"] == extra.id
    assert calls.count("grok-build") == 1


def test_individual_model_payload_still_checks_fresh_readiness(monkeypatch):
    calls = []
    monkeypatch.setattr(provider, "_harness_readiness", lambda profile: calls.append(profile) or {"status": "ready"})
    model = next(iter(provider.GLASSHIVE_MODELS.values()))
    model.api_payload()
    model.api_payload()
    assert calls == [model.harness_profile, model.harness_profile]
