"""A turn whose primary route is in a known cooldown starts on its armed fallback at once."""
import json
from datetime import datetime, timezone

import pytest

from workers_projects_runtime import conversation_provider as provider_module
from workers_projects_runtime.conversation_provider import ChatCompletionRequest

from test_conversation_provider import AUTH, _client, _payload, _signed_bundle_headers
from test_conversation_provider import lifecycle_owned_test_clients  # noqa: F401  (owns the app lifespan)

PRIMARY = "claude-code:opus"
FALLBACK = "codex-cli:gpt-5.6-sol"


@pytest.fixture(autouse=True)
def enforced(monkeypatch):
    monkeypatch.setattr(provider_module, "_ADMISSION_ROUTE_MODE", "enforce")


def _fixture(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    state = client.app.state
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code,grok-build")
    monkeypatch.setattr(state.service, "start_assigned_run", lambda _: None)
    monkeypatch.setattr(state.service, "_ensure_worker_processor", lambda _: None)
    return state.conversation_provider, state.store


def _turn(tmp_path, *, message_id="message-a", fallback=FALLBACK, **metadata):
    payload = _payload(tmp_path, model=PRIMARY)
    payload["reasoning_effort"] = "low"
    payload["metadata"].update(
        message_id=message_id, idempotency_key=f"idem-{message_id}", stream_id=f"stream-{message_id}",
        fallback_model=fallback, fallback_reasoning_effort="low", **metadata)
    return ChatCompletionRequest.model_validate(payload)


def _cool(provider, store, model_id, failure_class="provider_rate_limited"):
    model = provider._model(model_id)
    return store.record_provider_route_failure(
        tenant_id="local", owner_id="owner-a", profile=model.harness_profile,
        runtime=provider.service._initial_runtime_label(model.harness_profile, "host"),
        model=model.native_model, failure_class=failure_class, failure_structured=True,
        default_cooldown_s=300, run_id="run-cooling", evidence_id="evidence-cooling",
        now=datetime.now(timezone.utc))


def _worker_profiles(store):
    with store._connect() as conn:
        return [row[0] for row in conn.execute("SELECT profile FROM workers ORDER BY created_at")]


def _runs_by_profile(store, record):
    session = store.get_provider_session_by_id(record["session_id"])
    worker = store.get_worker(session["worker_id"])
    return session, worker


def test_a_cooling_primary_starts_the_turn_on_the_fallback_without_a_primary_worker(tmp_path, monkeypatch):
    provider, store = _fixture(tmp_path, monkeypatch)
    _cool(provider, store, PRIMARY)

    record = provider.start(_turn(tmp_path, route_requested_model="synthetic-forged-model"))

    session, worker = _runs_by_profile(store, record)
    assert session["model_id"] == FALLBACK and worker["profile"] == "codex-cli"
    assert store.get_run(record["run_id"])["worker_id"] == worker["worker_id"]
    assert record["fallback_state"] == "" and record["fallback_from_run_id"] == ""
    contract = provider._saved_completion_contract(record)
    assert contract["model"] == FALLBACK and contract["requested_model"] == PRIMARY
    assert _worker_profiles(store) == ["codex-cli"]
    run = store.update_run(record["run_id"], state="completed", output_text="Four.")
    answer = provider.response_payload(provider._sync(record), run, _turn(tmp_path))
    assert answer["model"] == FALLBACK and answer["glasshive"]["requested_model"] == PRIMARY
    skipped = [a for a in store.list_provider_activity(record["request_id"]) if a["event_type"] == "route-skipped"]
    assert len(skipped) == 1
    detail = json.loads(skipped[0]["payload_json"])
    assert detail["requestedModel"] == PRIMARY and detail["failureClass"] == "provider_rate_limited"
    assert detail["fallbackProfile"] == "codex-cli" and detail["decidedBeforeAdmission"] is True


@pytest.mark.parametrize("mode", ["enforce", "trial"])
@pytest.mark.parametrize("armed_fallback", [False, True])
def test_public_metadata_cannot_forge_the_requested_model(tmp_path, monkeypatch, mode, armed_fallback):
    monkeypatch.setattr(provider_module, "_ADMISSION_ROUTE_MODE", mode)
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload["metadata"]["route_requested_model"] = "synthetic-forged-model"
    if armed_fallback:
        payload["metadata"]["fallback_model"] = PRIMARY
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200
    answer = response.json()
    assert answer["model"] == payload["model"]
    assert "requested_model" not in answer.get("glasshive", {})
    # A duplicate still returns the original accepted turn and authoritative model.
    repeated = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert repeated.status_code == 200 and repeated.json() == answer
    # Ignoring a diagnostic extra must not weaken exact accepted-body replay checks.
    payload["metadata"]["route_requested_model"] = "another-synthetic-forged-model"
    changed = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert changed.status_code == 409
    store = client.app.state.store
    provider = client.app.state.conversation_provider
    with store._connect() as conn:
        records = [dict(row) for row in conn.execute("SELECT * FROM provider_requests")]
    assert len(records) == 1
    assert "requested_model" not in provider._saved_completion_contract(records[0])


def test_the_same_turn_sent_again_returns_the_accepted_request_on_the_same_route(tmp_path, monkeypatch):
    provider, store = _fixture(tmp_path, monkeypatch)
    _cool(provider, store, PRIMARY)
    first = provider.start(_turn(tmp_path))

    again = provider.start(_turn(tmp_path))

    assert again["request_id"] == first["request_id"] and again["run_id"] == first["run_id"]
    assert len([a for a in store.list_provider_activity(first["request_id"])
                if a["event_type"] == "route-skipped"]) == 1


def test_a_turn_accepted_on_the_fallback_stays_there_after_the_cooldown_ends(tmp_path, monkeypatch):
    provider, store = _fixture(tmp_path, monkeypatch)
    _cool(provider, store, PRIMARY)
    first = provider.start(_turn(tmp_path))
    with store._connect() as conn:
        conn.execute("DELETE FROM provider_route_health")

    again = provider.start(_turn(tmp_path))

    assert again["request_id"] == first["request_id"]
    assert provider._saved_completion_contract(again)["model"] == FALLBACK


def test_a_turn_accepted_on_the_primary_stays_there_when_a_cooldown_opens(tmp_path, monkeypatch):
    provider, store = _fixture(tmp_path, monkeypatch)
    first = provider.start(_turn(tmp_path))
    assert provider._saved_completion_contract(first)["model"] == PRIMARY
    _cool(provider, store, PRIMARY)

    again = provider.start(_turn(tmp_path))

    assert again["request_id"] == first["request_id"]
    assert provider._saved_completion_contract(again)["model"] == PRIMARY


def test_the_next_turn_returns_to_the_primary_once_the_cooldown_ends(tmp_path, monkeypatch):
    provider, store = _fixture(tmp_path, monkeypatch)
    _cool(provider, store, PRIMARY)
    first = provider.start(_turn(tmp_path))
    store.update_run(first["run_id"], state="completed", output_text="Done.")
    with store._connect() as conn:
        conn.execute("DELETE FROM provider_route_health")

    later = provider.start(_turn(tmp_path, message_id="message-b"))

    assert provider._saved_completion_contract(later)["model"] == PRIMARY
    assert later["fallback_model_id"] == FALLBACK


@pytest.mark.parametrize("case", ["healthy", "fallback_cooling", "other_failure", "no_fallback", "docker_origin"])
def test_every_other_turn_is_admitted_on_its_primary_as_before(tmp_path, monkeypatch, case):
    provider, store = _fixture(tmp_path, monkeypatch)
    extra = {}
    if case in {"fallback_cooling", "no_fallback", "docker_origin"}:
        _cool(provider, store, PRIMARY)
    if case == "fallback_cooling":
        _cool(provider, store, FALLBACK)
    if case == "other_failure":
        _cool(provider, store, PRIMARY, failure_class="provider_access_denied")
    if case == "docker_origin":
        extra["allowed_ai_origin_scope"] = {"execution_mode": "docker"}
    turn = _turn(tmp_path, fallback="" if case == "no_fallback" else FALLBACK, **extra)

    admitted, skip = provider._admission_route(turn)

    assert admitted is turn and skip is None


def test_turns_in_a_row_during_a_cooldown_keep_one_fallback_session_and_worker(tmp_path, monkeypatch):
    provider, store = _fixture(tmp_path, monkeypatch)
    _cool(provider, store, PRIMARY)
    first = provider.start(_turn(tmp_path))
    store.update_run(first["run_id"], state="completed", output_text="Done.")

    second = provider.start(_turn(tmp_path, message_id="message-b"))

    first_session, first_worker = _runs_by_profile(store, first)
    second_session, second_worker = _runs_by_profile(store, second)
    assert second["request_id"] != first["request_id"]
    assert second_session["session_id"] == first_session["session_id"]
    assert second_worker["worker_id"] == first_worker["worker_id"]
    assert _worker_profiles(store) == ["codex-cli"]


RETAINED = "claude-code:claude-opus-5"
RETAINED_BUNDLE = {"provider_capabilities": {"native_tools": False, "conversation_process_reuse": True}}


def _application_turn(client, tmp_path, *, message_id="message-a"):
    """Send the turn the way the application does: signed bundle and fallback in headers."""
    body = _payload(tmp_path, model=RETAINED)
    body["reasoning_effort"] = "low"
    body["metadata"].update(message_id=message_id, idempotency_key=f"idem-{message_id}",
                            stream_id=f"stream-{message_id}")
    return client.post("/v1/chat/completions", json=body, headers={
        **AUTH, **_signed_bundle_headers(RETAINED_BUNDLE),
        "X-GlassHive-Fallback-Model": FALLBACK, "X-GlassHive-Fallback-Reasoning-Effort": "low"})


def _retained_client(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    state = client.app.state
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code,grok-build")
    monkeypatch.setattr(state.service, "start_assigned_run", lambda _: None)
    monkeypatch.setattr(state.service, "_ensure_worker_processor", lambda _: None)
    monkeypatch.setattr(state.conversation_provider, "wait", lambda request_id: (_ for _ in ()).throw(StopWait(request_id)))
    return client, state.conversation_provider, state.store


class StopWait(Exception):
    pass


def _post(client, tmp_path, **kwargs):
    try:
        return _application_turn(client, tmp_path, **kwargs)
    except StopWait as stop:
        return str(stop)


def test_a_retained_claude_turn_is_admitted_on_the_fallback_without_its_claude_only_declaration(tmp_path, monkeypatch):
    client, provider, store = _retained_client(tmp_path, monkeypatch)
    _cool(provider, store, RETAINED)

    request_id = _post(client, tmp_path)

    assert isinstance(request_id, str), getattr(request_id, "text", request_id)
    record = store.get_provider_request(request_id)
    session, worker = _runs_by_profile(store, record)
    assert session["model_id"] == FALLBACK and _worker_profiles(store) == ["codex-cli"]
    capabilities = json.loads(worker["bootstrap_bundle_json"])["provider_capabilities"]
    assert capabilities["native_tools"] is False and "conversation_process_reuse" not in capabilities
    assert provider._saved_completion_contract(record)["requested_model"] == RETAINED
    assert _post(client, tmp_path) == request_id


def test_the_trial_validates_beside_the_unchanged_primary_admission(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(provider_module, "_ADMISSION_ROUTE_MODE", "trial")
    client, provider, store = _retained_client(tmp_path, monkeypatch)
    _cool(provider, store, RETAINED)

    with caplog.at_level("WARNING"):
        request_id = _post(client, tmp_path)

    assert isinstance(request_id, str), getattr(request_id, "text", request_id)
    record = store.get_provider_request(request_id)
    assert provider._saved_completion_contract(record)["model"] == RETAINED
    assert record["fallback_model_id"] == FALLBACK and _worker_profiles(store) == ["claude-code"]
    assert not [a for a in store.list_provider_activity(request_id) if a["event_type"] == "route-skipped"]
    trial = [r.getMessage() for r in caplog.records if "admission_route_trial" in r.getMessage()]
    assert len(trial) == 1 and "outcome=ok" in trial[0] and f"model={FALLBACK}" in trial[0]


def test_the_trial_reports_a_rejection_and_still_admits_the_turn(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(provider_module, "_ADMISSION_ROUTE_MODE", "trial")
    client, provider, store = _retained_client(tmp_path, monkeypatch)
    _cool(provider, store, RETAINED)
    monkeypatch.setattr(type(provider), "_route_portable_bootstrap_bundle",
                        staticmethod(lambda bundle, model: bundle))

    with caplog.at_level("WARNING"):
        request_id = _post(client, tmp_path)

    assert isinstance(request_id, str), getattr(request_id, "text", request_id)
    assert provider._saved_completion_contract(store.get_provider_request(request_id))["model"] == RETAINED
    trial = [r.getMessage() for r in caplog.records if "admission_route_trial" in r.getMessage()]
    assert len(trial) == 1 and "outcome=rejected_400" in trial[0]
