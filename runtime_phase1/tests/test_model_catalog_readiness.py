"""Model discovery is metadata-only; explicit readiness checks retain their own authority."""

from workers_projects_runtime import conversation_provider as provider


def _service(monkeypatch, extra=None):
    service = object.__new__(provider.ConversationProvider)
    monkeypatch.setattr(service, "_selected_grok_model", lambda *_args: extra)
    return service


def test_catalog_never_launches_harness_or_authentication_probe(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("model metadata must not start a native harness")

    monkeypatch.setattr(provider, "_harness_readiness", forbidden)
    monkeypatch.setattr(provider, "_harness_auth_configured", forbidden)
    monkeypatch.setattr(provider.subprocess, "Popen", forbidden)
    monkeypatch.setattr(provider.subprocess, "run", forbidden)
    actual = _service(monkeypatch).models_payload()
    assert actual["data"]
    assert all(model["readiness"]["status"] == "not_checked" for model in actual["data"])
    assert all("authentication" not in model["readiness"] for model in actual["data"])
    assert all("binary_available" not in model["readiness"] for model in actual["data"])


def test_catalog_preserves_every_model_capability_budget_and_field(monkeypatch):
    expected = {"object": "list", "data": [
        model.api_payload(readiness={}) for model in provider.GLASSHIVE_MODELS.values()
    ]}
    actual = _service(monkeypatch).models_payload()
    for model in actual["data"]:
        model["readiness"] = {}
    assert actual == expected


def test_catalog_readiness_is_not_a_stale_auth_observation_or_shared_mutable_state(monkeypatch):
    monkeypatch.setattr(provider, "_harness_readiness", lambda _profile: {"status": "ready"})
    service = _service(monkeypatch)
    first = service.models_payload()
    monkeypatch.setattr(provider, "_harness_readiness", lambda _profile: {"status": "authentication_required"})
    second = service.models_payload()
    assert first == second
    first["data"][0]["readiness"]["status"] = "changed_by_consumer"
    assert all(model["readiness"]["status"] == "not_checked" for model in second["data"])
    assert all(model["readiness"]["status"] == "not_checked" for model in first["data"][1:])


def test_configured_extra_model_keeps_exact_metadata_without_probe(monkeypatch):
    extra = provider.HarnessModel(
        id="grok-build:synthetic-new-model", display_name="Synthetic model",
        harness_profile="grok-build", native_model="synthetic-new-model",
        effort_choices=("default",), recommended_effort="default", context_window=32768,
    )
    def forbidden(_profile):
        raise AssertionError("configured metadata must not probe its harness")

    monkeypatch.setattr(provider, "_harness_readiness", forbidden)
    actual = _service(monkeypatch, extra).models_payload()
    expected = extra.api_payload(readiness=actual["data"][-1]["readiness"])
    assert actual["data"][-1] == expected
    assert expected["id"] == extra.id


def test_individual_model_payload_still_checks_fresh_readiness(monkeypatch):
    calls = []
    monkeypatch.setattr(provider, "_harness_readiness", lambda profile: calls.append(profile) or {"status": "ready"})
    model = next(iter(provider.GLASSHIVE_MODELS.values()))
    model.api_payload()
    model.api_payload()
    assert calls == [model.harness_profile, model.harness_profile]


def test_explicit_catalog_readiness_checks_once_per_harness(monkeypatch):
    calls = []
    def check(profile):
        calls.append(profile)
        return {"status": "ready", "detail": profile}
    monkeypatch.setattr(provider, "_harness_readiness", check)
    actual = _service(monkeypatch).models_payload(check_readiness=True)
    profiles = {model.harness_profile for model in provider.GLASSHIVE_MODELS.values()}
    assert set(calls) == profiles
    assert len(calls) == len(profiles)
    for model in actual["data"]:
        assert model["readiness"] == {"status": "ready", "detail": model["harness_profile"]}


def test_explicit_readiness_preserves_exact_default_metadata(monkeypatch):
    monkeypatch.setattr(provider, "_harness_readiness", lambda _profile: {"status": "authentication_required"})
    service = _service(monkeypatch)
    default = service.models_payload()
    checked = service.models_payload(check_readiness=True)
    assert all(model["readiness"]["status"] == "authentication_required" for model in checked["data"])
    for payload in (default, checked):
        for model in payload["data"]:
            model.pop("readiness")
    assert default == checked
