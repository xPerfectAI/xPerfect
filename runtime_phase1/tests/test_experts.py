"""Experts people can talk to: settings, channels, outbox and Ask me first, on the stub runtime."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.coordinator import CoordinatorConfig, ExpertBinding, Route, canonical
from workers_projects_runtime.channels import InboundMessage
from workers_projects_runtime.experts import ExpertError, ExpertSpec, ExpertTeam, ExpertRole, default_spec

SPEC = {"instructions": "Synthetic instructions: help with engineering; never share private data.",
        "team": {"talk": {"profile": "claude-code", "effort": "low"}, "coordinate": {"profile": "codex-cli"},
                 "work": ["claude-code", "codex-cli"]},
        "ask_me_first": "auto", "workspace": {"kind": "host", "access": "full"}}
PERSON = {"id": "person-synthetic-a", "name": "Synthetic Person", "tenant": "tenant-synthetic"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    application = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    experts = application.state.experts
    provider = experts.coordinator.provider
    # The stub runtime has no conversation model: a synthetic one stands in, and starting a
    # native turn fails like an unreachable provider, so saved state is what the tests read.
    model = SimpleNamespace(effort_choices=("low", "high"), harness_profile="claude-code", native_model="synthetic",
                            connection_id="", recommended_effort="high", id="synthetic-model")
    monkeypatch.setattr(provider, "_model", lambda *args, **kwargs: model)

    def unreachable(*args, **kwargs):
        raise ConnectionError("synthetic provider unavailable")

    monkeypatch.setattr(provider, "start", unreachable)
    monkeypatch.setattr(experts, "_role_config", lambda tenant, owner, role: CoordinatorConfig(
        model="synthetic-model", effort="high",
        routes=[Route(id="default", profile=role.profile, model="synthetic-model", effort="high", execution_mode="host")]))
    with TestClient(application) as client:
        yield application, client


def create_expert(client, **changes):
    response = client.post("/v1/experts", json={"name": "Synthetic Expert", "description": "Answers engineering questions.",
                                                "expert": {**SPEC, **changes}})
    assert response.status_code == 201, response.text
    return response.json()


def connected_channel(application, client, expert):
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    client.post(f"/v1/channels/{channel['binding_id']}/setup", json={"settings": {"app_id": "synthetic-app", "mode": "socket"}})
    response = client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": [PERSON], "state": "connected"})
    assert response.status_code == 200, response.text
    return response.json()


def message(activity_id, person=None, text="Hello"):
    person = person or PERSON
    return {"activity_id": activity_id, "person": person, "thread_key": f"{person['id']}:chat",
            "thread_ref": {"conversation_id": "synthetic-chat"}, "text": text}


def scalar(application, sql, *args):
    with application.state.experts.store._connect() as conn:
        row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def finish_turn(application, conversation_id, turn_id, text):
    with application.state.experts.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET response_json=? WHERE conversation_id=? AND turn_id=?",
                     (canonical({"choices": [{"message": {"role": "assistant", "content": text}}]}), conversation_id, turn_id))


def test_spec_rules():
    assert default_spec().team.talk.effort == "low"
    assert default_spec().team.talk.model == "claude-code:haiku"  # the measured faster talker
    with pytest.raises(ValueError):
        ExpertTeam(talk=ExpertRole(profile="grok-build"), coordinate=ExpertRole(profile="codex-cli"), work=["codex-cli"])
    with pytest.raises(ValueError):
        ExpertTeam(talk=ExpertRole(profile="claude-code"), coordinate=ExpertRole(profile="codex-cli"), work=["codex-cli", "codex-cli"])


def test_configurations_without_an_expert_stay_byte_exact():
    config = CoordinatorConfig(model="synthetic-model", effort="high")
    assert "expert" not in config.model_dump()
    assert "expert" not in json.loads(canonical(config.model_dump()))


def test_expert_is_created_without_a_worker_and_versioned(app):
    application, client = app
    expert = create_expert(client)
    assert expert["expert"]["instructions"].startswith("Synthetic instructions")
    assert expert["version"] == 1
    read = client.get(f"/v1/experts/{expert['template_id']}").json()
    assert read["expert"]["team"]["talk"]["profile"] == "claude-code"
    spec = {**read["expert"], "ask_me_first": "always"}
    updated = client.patch(f"/v1/experts/{expert['template_id']}",
                           json={"expected_catalog_revision": read["catalog_revision"], "expert": spec})
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2 and updated.json()["lineage_id"] == expert["lineage_id"]
    stale = client.patch(f"/v1/experts/{expert['template_id']}",
                         json={"expected_catalog_revision": read["catalog_revision"], "expert": spec})
    assert stale.status_code == 409
    # The newest version is the Expert, also when asked by an older version's id.
    assert client.get(f"/v1/experts/{expert['template_id']}").json()["expert"]["ask_me_first"] == "always"


def test_listed_person_once_duplicate_and_unlisted(app):
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(application, client, expert)
    path = f"/v1/channels/{channel['binding_id']}/inbound"
    first = client.post(path, json=message("act-1")).json()
    again = client.post(path, json=message("act-1")).json()
    stranger = client.post(path, json=message("act-2", {"id": "person-unlisted", "name": "Stranger", "tenant": "tenant-synthetic"})).json()
    assert first["accepted"] and not first["duplicate"]
    assert again == {**first, "duplicate": True}
    assert stranger == {"accepted": False, "reason": "not_listed"}
    with application.state.experts.store._connect() as conn:
        turns = conn.execute("SELECT COUNT(*) FROM coordinator_turns WHERE conversation_id=?", (first["conversation_id"],)).fetchone()[0]
        wrapped = conn.execute("SELECT message FROM coordinator_turns WHERE conversation_id=?", (first["conversation_id"],)).fetchone()[0]
    assert turns == 1
    assert wrapped.startswith("New message from Synthetic Person") and wrapped.endswith("\n\nHello")
    view = client.get(f"/v1/experts/{expert['template_id']}/channels").json()["items"][0]
    assert [p["id"] for p in view["people_waiting"]] == ["person-unlisted"]
    # Another tenant's person with the same id is not the listed person.
    other = client.post(path, json=message("act-3", {**PERSON, "tenant": "tenant-other"})).json()
    assert other["accepted"] is False
    client.patch(f"/v1/channels/{channel['binding_id']}", json={"state": "off"})
    assert client.post(path, json=message("act-4")).json() == {"accepted": False, "reason": "channel_off"}


def test_outbox_sends_once_and_never_resends_an_unknown_outcome(app):
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Synthetic answer")
    channels = application.state.channels
    channels.reconcile_once()
    channels.reconcile_once()
    claim = f"/v1/channels/{channel['binding_id']}/deliveries/claim"
    first = client.post(claim).json()["items"]
    assert [d["text"] for d in first] == ["Synthetic answer"]
    assert first[0]["thread_ref"] == {"conversation_id": "synthetic-chat"}
    assert client.post(claim).json()["items"] == []
    # The package never reports back: the message becomes not confirmed and is not resent.
    with channels.store._connect() as conn:
        conn.execute("UPDATE channel_deliveries SET claimed_at=?", ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    channels.reconcile_once()
    with channels.store._connect() as conn:
        assert conn.execute("SELECT state FROM channel_deliveries").fetchone()[0] == "not_confirmed"
    assert client.post(claim).json()["items"] == []
    assert client.get(f"/v1/experts/{expert['template_id']}/channels").json()["items"][0]["unconfirmed_messages"] == 1


def test_always_ask_holds_the_reply_and_tells_the_person_with_tools_off(app):
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Prepared answer")
    application.state.channels.reconcile_once()
    experts = application.state.experts
    reviews = experts.reviews("local", scalar(application, "SELECT owner_id FROM expert_reviews"))
    assert [(r["kind"], r["payload"]) for r in reviews] == [("reply", {"text": "Prepared answer"})]
    with experts.store._connect() as conn:
        held = conn.execute("SELECT state,text FROM channel_deliveries WHERE kind='reply'").fetchone()
        checking = conn.execute("SELECT turn_id,origin,payload_json FROM coordinator_turns WHERE origin='owner_checking'").fetchone()
    assert tuple(held) == ("held", "Prepared answer")
    payload = json.loads(checking["payload_json"])
    assert payload["metadata"]["bootstrap_bundle"]["provider_capabilities"]["native_tools"] is False
    assert "coordination_only" not in payload["metadata"]["bootstrap_bundle"]["provider_capabilities"]
    # Nothing was sent to the person yet.
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []


def test_approve_sends_exactly_what_was_shown_and_decides_once(app):
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Prepared answer")
    application.state.channels.reconcile_once()
    experts = application.state.experts
    owner = scalar(application, "SELECT owner_id FROM expert_reviews")
    review = experts.reviews("local", owner)[0]
    # A runtime token is not a signed-in person.
    refused = client.post(f"/v1/expert-reviews/{review['review_id']}/decision",
                          json={"decision": "approve", "expected_digest": review["payload_digest"]})
    assert refused.status_code == 403
    with pytest.raises(ExpertError, match="changed"):
        experts.decide("local", owner, review["review_id"], decision="approve", expected_digest="0" * 64)
    experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    with pytest.raises(ExpertError, match="already decided"):
        experts.decide("local", owner, review["review_id"], decision="reject", expected_digest=review["payload_digest"])
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert [d["text"] for d in sent if d["kind"] == "reply"] == ["Prepared answer"]


def test_reject_drops_the_hold_and_asks_the_queen_with_tools_off(app):
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Private details")
    application.state.channels.reconcile_once()
    experts = application.state.experts
    owner = scalar(application, "SELECT owner_id FROM expert_reviews")
    review = experts.reviews("local", owner)[0]
    experts.decide("local", owner, review["review_id"], decision="reject", expected_digest=review["payload_digest"], note="No.")
    with experts.store._connect() as conn:
        assert conn.execute("SELECT state FROM channel_deliveries WHERE kind='reply'").fetchone()[0] == "dropped"
        decision = conn.execute("SELECT message,payload_json FROM coordinator_turns WHERE origin='owner_decision'").fetchone()
    assert json.loads(decision["message"])["decision"] == "rejected"
    assert json.loads(decision["payload_json"])["metadata"]["bootstrap_bundle"]["provider_capabilities"]["native_tools"] is False


def test_a_held_job_that_changed_or_stopped_cannot_be_released(app):
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    experts = application.state.experts
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, accepted["conversation_id"], goal="Synthetic job", idempotency_key="k1",
                           source_turn_id=accepted["turn_id"])
    assert job["state"] == "held" and job["held_for_owner"]
    review = experts.reviews("local", owner)[0]
    assert review["payload"] == {"goal": "Synthetic job", "context": ""}
    experts.stop_job(job["job_id"])
    assert experts.reviews("local", owner) == []
    with pytest.raises(ExpertError):
        experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    with experts.store._connect() as conn:
        assert conn.execute("SELECT state,job_conversation_id FROM expert_jobs").fetchone()[:] == ("stopped", "")


def test_a_finished_job_is_told_to_the_queen_once(app):
    application, client = app
    expert = create_expert(client)
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    with experts.store._connect() as conn:
        conn.execute("INSERT INTO expert_jobs(job_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,idempotency_key,goal,"
                     "state,created_at,updated_at) VALUES('job_synthetic','local',?,?,?,'k','Synthetic job','running','t','t')",
                     (owner, expert["lineage_id"], chat["conversation_id"]))
    job = experts._job("job_synthetic")
    experts._publish(job, "done", "Synthetic result")
    experts._publish(job, "done", "Synthetic result")
    with experts.store._connect() as conn:
        events = conn.execute("SELECT kind,payload_json FROM coordinator_events WHERE conversation_id=?", (chat["conversation_id"],)).fetchall()
    assert [e["kind"] for e in events] == ["expert_job_result"]
    assert json.loads(events[0]["payload_json"])["result"]["output_text"] == "Synthetic result"
    assert experts._job("job_synthetic")["state"] == "done"


def test_a_stopped_job_never_starts_again_and_a_raced_stop_is_re_driven(app, monkeypatch):
    application, client = app
    expert = create_expert(client)
    experts = application.state.experts
    monkeypatch.setattr(experts.coordinator.provider, "cancel_by_idempotency",
                        lambda *args, **kwargs: {"capacity_released": True})
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    first = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    second = experts.hand_off("local", owner, chat["conversation_id"], goal="Another job", idempotency_key="k2")
    cids = [experts._job(job["job_id"])["job_conversation_id"] for job in (first, second)]
    assert all(cids)
    experts.stop_job(first["job_id"])
    core = experts.coordinator
    assert core.snapshot("local", owner, cids[0])["stop_scope"] == "stopped"
    with pytest.raises(Exception, match="stopped"):
        core.accept_turn("local", owner, cids[0], "late", "Synthetic late message")
    assert core.queue_result_turn("local", owner, cids[0]) is None
    # Stop reaches only that job.
    assert core.snapshot("local", owner, cids[1])["stop_scope"] == ""
    assert experts._job(second["job_id"])["state"] in {"starting", "running"}
    # A Stop that raced the job's start (or a restart between the two) is re-driven.
    with experts.store._connect() as conn:
        conn.execute("UPDATE expert_jobs SET state='stopped' WHERE job_id=?", (second["job_id"],))
    experts.reconcile_once()
    assert core.snapshot("local", owner, cids[1])["stop_scope"] == "stopped"


def test_turning_a_channel_off_stops_its_jobs_and_withdraws_its_requests(app):
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, accepted["conversation_id"], goal="Synthetic job", idempotency_key="k1",
                           source_turn_id=accepted["turn_id"])
    # A second message whose reply is held for the owner.
    later = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-2")).json()
    finish_turn(application, later["conversation_id"], later["turn_id"], "Prepared answer")
    application.state.channels.reconcile_once()
    waiting = {review["kind"]: review for review in experts.reviews("local", owner)}
    assert set(waiting) == {"job", "reply"}
    response = client.patch(f"/v1/channels/{channel['binding_id']}", json={"state": "off"})
    assert response.status_code == 200, response.text
    assert experts._job(job["job_id"])["state"] == "stopped"
    assert experts.reviews("local", owner) == []
    reply = waiting["reply"]
    with pytest.raises(ExpertError):
        experts.decide("local", owner, reply["review_id"], decision="approve", expected_digest=reply["payload_digest"])
    # Even a direct release never revives a message the channel dropped.
    application.state.channels.release_approved_reply(experts._review(reply["review_id"]))
    with experts.store._connect() as conn:
        states = [row[0] for row in conn.execute("SELECT state FROM channel_deliveries WHERE kind<>'checking'")]
    assert states and set(states) == {"dropped"}


def test_a_job_stopped_while_it_is_prepared_never_starts(app, monkeypatch):
    application, client = app
    expert = create_expert(client)
    experts = application.state.experts
    monkeypatch.setattr(experts.coordinator.provider, "cancel_by_idempotency",
                        lambda *args, **kwargs: {"capacity_released": True})
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    started = []
    original_accept = experts.coordinator.accept_turn

    def stop_during_prepare(*args, **kwargs):  # the Stop lands between creating the job and starting it
        result = original_accept(*args, **kwargs)
        with experts.store._connect() as conn:
            conn.execute("UPDATE expert_jobs SET state='stopped' WHERE state='starting'")
        return result

    monkeypatch.setattr(experts.coordinator, "accept_turn", stop_during_prepare)
    monkeypatch.setattr(experts.coordinator, "start_turn", lambda *args: started.append(args) or {})
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    assert started == []
    stored = experts._job(job["job_id"])
    assert stored["state"] == "stopped"
    assert experts.coordinator.snapshot("local", owner, stored["job_conversation_id"])["stop_scope"] == "stopped"


def test_a_backup_the_runtime_cannot_honour_is_refused_at_save(app):
    application, client = app
    for talk, words in [({"profile": "claude-code", "backup": {"profile": "claude-code"}}, "different AI"),
                        ({"profile": "claude-code", "backup": {"profile": "codex-cli", "account_id": "acct-synthetic"}},
                         "sign-in")]:
        response = client.post("/v1/experts", json={"name": "Synthetic Expert",
                                                    "expert": {**SPEC, "team": {**SPEC["team"], "talk": talk}}})
        assert response.status_code == 422 and words in response.text
    create_expert(client, team={**SPEC["team"], "talk": {"profile": "claude-code", "effort": "low",
                                                         "backup": {"profile": "codex-cli"}}})


def test_the_owner_sees_a_backup_take_over_and_a_limit_without_one(app):
    application, client = app
    expert = create_expert(client)
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    store = experts.store
    project = store.create_project(owner, "Synthetic", "Synthetic project", "claude-code")
    worker = store.create_worker(project["project_id"], owner, "Queen", "speaker", "claude-code", "claude-code",
                                 "claude-code", "opus", execution_mode="host")
    session = store.upsert_provider_session(tenant_id="local", owner_id=owner, conversation_id=chat["conversation_id"],
                                            agent_id="coordinator", model_id="claude-code:opus",
                                            project_id=project["project_id"], worker_id=worker["worker_id"],
                                            workspace_dir="/synthetic/workspace", access_mode="read_write")
    switched, _ = store.create_provider_request(tenant_id="local", owner_id=owner, session_id=session["session_id"],
                                                idempotency_key="synthetic-switched", message_id="m1", stream_id="s1",
                                                requested_history_count=0)
    store.add_provider_activity(switched["request_id"], "route-skipped", "Skipped a known-unhealthy provider route.",
                                {"profile": "claude-code", "fallbackProfile": "codex-cli",
                                 "failureClass": "provider_rate_limited", "cooldownUntil": "2099-01-01T00:00:00+00:00",
                                 "fallbackEligible": True})
    limited, _ = store.create_provider_request(tenant_id="local", owner_id=owner, session_id=session["session_id"],
                                               idempotency_key="synthetic-limited", message_id="m2", stream_id="s2",
                                               requested_history_count=0)
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic turn")
    store.update_run(run["run_id"], state="failed", failure_class="provider_rate_limited")
    store.update_provider_request(limited["request_id"], run_id=run["run_id"])
    items = [item for item in client.get("/v1/expert-notices").json()["items"] if item["kind"] in {"backup", "limited"}]
    assert sorted((item["kind"], item["ai"], item["backup_ai"] if item["kind"] == "backup" else "") for item in items) == [
        ("backup", "claude-code", "codex-cli"), ("limited", "claude-code", "")]
    assert all(item["expert"] == "Synthetic Expert" for item in items)


def test_an_expired_sign_in_code_is_never_shown_again(app):
    application, client = app
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    ended = SimpleNamespace(poll=lambda: 1, returncode=1)  # Microsoft's sign-in timed out
    application.state.channels.setup._sessions[channel["binding_id"]] = {
        "step": "sign_in", "url": "https://login.microsoft.com/device", "code": "SYNTHETIC1", "process": ended}
    status = client.get(f"/v1/channels/{channel['binding_id']}/connect").json()
    assert status["step"] == "error" and "code" not in status and "Connect" in status["error"]


def test_pressing_connect_again_during_sign_in_returns_the_same_code(app):
    import threading
    application, client = app
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", channel["binding_id"])
    setup = application.state.channels.setup
    waiting = SimpleNamespace(poll=lambda: None, returncode=None)  # Microsoft's sign-in still open
    setup._sessions[channel["binding_id"]] = {"step": "sign_in", "url": "https://login.microsoft.com/device",
                                              "code": "SYNTHETIC2", "process": waiting}
    result = {}
    pressed = threading.Thread(target=lambda: result.update(setup.connect("local", owner, channel["binding_id"])),
                               daemon=True)
    pressed.start(); pressed.join(3)
    assert not pressed.is_alive(), "Connect hung while a sign-in was pending"
    assert result["step"] == "sign_in" and result["code"] == "SYNTHETIC2"


def test_the_speaking_assistant_cannot_use_the_coordinators_tools(app):
    # Her token reaches only her own four tools; coordinator tools could start work past Ask me first.
    from workers_projects_runtime.coordinator import CoordinatorScopeError
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    store = experts.store
    project = store.create_project(owner, "Synthetic", "Synthetic project", "claude-code")
    worker = store.create_worker(project["project_id"], owner, "Queen", "speaker", "claude-code", "claude-code",
                                 "claude-code", "opus", execution_mode="host")
    store.upsert_provider_session(tenant_id="local", owner_id=owner, conversation_id=chat["conversation_id"],
                                  agent_id="coordinator", model_id="claude-code:opus", project_id=project["project_id"],
                                  worker_id=worker["worker_id"], workspace_dir="/synthetic/workspace", access_mode="read_write")
    principal = {"tenant_id": "local", "owner_id": owner, "worker_id": worker["worker_id"], "run_id": "run-synthetic",
                 "attempt_id": "attempt-synthetic"}
    with pytest.raises(CoordinatorScopeError):
        experts.coordinator.native_identity(principal)
    assert experts.coordinator.native_identity(principal, allow_speaker=True)[2] == chat["conversation_id"]


def test_on_auto_nothing_of_a_turn_that_asked_the_owner_leaves_while_it_waits(app):
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    experts.ask_owner("local", owner, accepted["conversation_id"], reason="Unusual request", reply="SYNTHETIC-HELD-TEXT",
                      idempotency_key="ask-1", source_turn_id=accepted["turn_id"])
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Checking. SYNTHETIC-HELD-TEXT")
    application.state.channels.reconcile_once()
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert not any("SYNTHETIC-HELD-TEXT" in item["text"] for item in sent)
    with experts.store._connect() as conn:
        checking = conn.execute("SELECT payload_json FROM coordinator_turns WHERE origin='owner_checking'").fetchone()
    assert checking is not None  # her checking line is written in a turn of its own
    assert json.loads(checking["payload_json"])["metadata"]["bootstrap_bundle"]["provider_capabilities"]["native_tools"] is False


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_decision_before_the_outbox_pass_never_lets_the_turns_own_reply_out(app, decision):
    # Review 2, finding 1: Approve releases what was shown and nothing else; a no sends nothing that was held.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    experts.ask_owner("local", owner, accepted["conversation_id"], reason="Unusual request", reply="SYNTHETIC-SHOWN-TEXT",
                      idempotency_key="ask-1", source_turn_id=accepted["turn_id"])
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "SYNTHETIC-OWN-REPLY")
    review = experts.reviews("local", owner)[0]
    experts.decide("local", owner, review["review_id"], decision=decision, expected_digest=review["payload_digest"])
    application.state.channels.reconcile_once()
    sent = [item["text"] for item in client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]]
    assert not any("SYNTHETIC-OWN-REPLY" in text for text in sent)
    assert sent == (["SYNTHETIC-SHOWN-TEXT"] if decision == "approve" else [])
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE origin='owner_checking'") == 0


def test_a_stopped_conversation_leaves_nothing_approvable(app, monkeypatch):
    # The owner's correction: changed or stopped work must invalidate old approvals.
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    monkeypatch.setattr(experts.coordinator.provider, "cancel_by_idempotency",
                        lambda *args, **kwargs: {"capacity_released": True})
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    cid = accepted["conversation_id"]
    job = experts.hand_off("local", owner, cid, goal="Synthetic job", idempotency_key="k1", source_turn_id=accepted["turn_id"])
    later = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-2")).json()
    finish_turn(application, cid, later["turn_id"], "Prepared answer")
    application.state.channels.reconcile_once()
    waiting = {review["kind"]: review for review in experts.reviews("local", owner)}
    assert set(waiting) == {"job", "reply"}
    experts.coordinator.stop_conversation("local", owner, cid)
    assert experts.reviews("local", owner) == []
    for review in waiting.values():
        with pytest.raises(ExpertError):
            experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    assert experts._job(job["job_id"])["state"] == "stopped"
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []


def test_removing_a_person_stops_everything_waiting_for_them(app):
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Prepared answer")
    application.state.channels.reconcile_once()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    review = experts.reviews("local", owner)[0]
    with experts.store._connect() as conn:  # and one message already free to leave
        conn.execute("UPDATE channel_deliveries SET state='pending' WHERE kind='checking'")
    assert client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": []}).status_code == 200
    assert experts.reviews("local", owner) == []
    with pytest.raises(ExpertError):
        experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []


def test_a_message_accepted_before_an_interruption_still_gets_its_turn(app, monkeypatch):
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(application, client, expert)
    channels, experts = application.state.channels, application.state.experts
    original = experts.queen
    calls = {"n": 0}

    def interrupted(*args, **kwargs):  # xPerfect stops after the inbox commit, before the turn exists
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(experts, "queen", interrupted)
    with pytest.raises(RuntimeError):
        channels.inbound(channel["binding_id"], InboundMessage.model_validate(message("act-1")))
    again = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    assert again["duplicate"] is True and again["turn_id"]
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE turn_id=?", again["turn_id"]) == 1
    # Without a redelivery, maintenance finishes it.
    calls["n"] = 0
    with pytest.raises(RuntimeError):
        channels.inbound(channel["binding_id"], InboundMessage.model_validate(message("act-2")))
    with experts.store._connect() as conn:
        conn.execute("UPDATE channel_inbox SET created_at='2026-01-01T00:00:00+00:00' WHERE activity_id='act-2'")
    assert channels.repair_inbox() == 1
    turn = scalar(application, "SELECT turn_id FROM channel_inbox WHERE activity_id='act-2'")
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE turn_id=?", turn) == 1


def test_an_interrupted_approval_is_released_once(app, monkeypatch):
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    channels, experts = application.state.channels, application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Prepared answer")
    channels.reconcile_once()
    owner = scalar(application, "SELECT owner_id FROM expert_reviews")
    review = experts.reviews("local", owner)[0]
    release = channels.release_approved_reply
    monkeypatch.setattr(channels, "release_approved_reply", lambda *_: (_ for _ in ()).throw(RuntimeError("stopped")))
    with pytest.raises(RuntimeError):
        experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    monkeypatch.setattr(channels, "release_approved_reply", release)
    experts.reconcile_once(); experts.reconcile_once()
    with experts.store._connect() as conn:
        states = [row[0] for row in conn.execute("SELECT state FROM channel_deliveries WHERE review_id=?", (review["review_id"],))]
    assert states == ["pending"]


def test_a_restored_copy_never_sends_a_reply_that_was_finished_before_the_copy(app):
    import sqlite3
    from workers_projects_runtime.native_continuity import _reset_restore_authority
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Finished before the copy")
    database = application.state.experts.store.db_path
    with sqlite3.connect(database) as connection:  # what a restore does to its staged copy
        connection.row_factory = sqlite3.Row
        _reset_restore_authority(connection)
    application.state.channels.reconcile_once()
    with application.state.experts.store._connect() as conn:
        rows = [tuple(row) for row in conn.execute("SELECT kind,state,text FROM channel_deliveries")]
    assert rows == [("reply", "not_confirmed", "Finished before the copy")]


def test_workers_run_at_the_level_the_expert_shows_or_saving_is_refused(app):
    application, client = app
    for workspace, words in [({"kind": "host", "access": "read_only"}, "full access"),
                             ({"kind": "host", "access": "workspace"}, "full access"),
                             ({"kind": "sandboxed", "access": "full"}, "workspace")]:
        response = client.post("/v1/experts", json={"name": "Synthetic Expert", "expert": {**SPEC, "workspace": workspace}})
        assert response.status_code == 422 and words in response.text
    experts = application.state.experts
    sandboxed = create_expert(client, workspace={"kind": "sandboxed", "access": "workspace"})
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", sandboxed["template_id"])
    template = experts.resolve("local", owner, sandboxed["template_id"])
    binding = ExpertBinding(lineage_id=template["lineage_id"], role="job", access="workspace")
    config = experts._job_config("local", owner, template, binding)
    assert config.routes and {route.execution_mode for route in config.routes} == {"docker"}


def test_off_holds_nothing(app):
    # The owner's correction: Off skips approval and keeps the instructions.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    result = experts.ask_owner("local", owner, accepted["conversation_id"], reason="Unusual", job_goal="Synthetic job",
                               idempotency_key="ask-off", source_turn_id=accepted["turn_id"])
    assert result["state"] == "not_asked"
    assert experts.reviews("local", owner) == []
    assert scalar(application, "SELECT count(*) FROM expert_jobs") == 0


def test_the_owners_own_chat_asks_in_the_chat_never_in_needs_you(app):
    # In his own Chat the owner is the one asking; a check goes in the reply, not to Needs you.
    application, client = app
    expert = create_expert(client)  # Auto
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    result = experts.ask_owner("local", owner, chat["conversation_id"], reason="Uses private context",
                               job_goal="Synthetic job", idempotency_key="ask-own-chat")
    assert result["state"] == "not_asked" and "own Chat" in result["note"]
    assert experts.reviews("local", owner) == []
    assert scalar(application, "SELECT count(*) FROM expert_jobs") == 0
    # Another person on a channel is still asked about.
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-own")).json()
    asked = experts.ask_owner("local", owner, accepted["conversation_id"], reason="Unusual", reply="SYNTHETIC",
                              idempotency_key="ask-person", source_turn_id=accepted["turn_id"])
    assert asked["state"] == "pending" and len(experts.reviews("local", owner)) == 1


def _stream(*events):
    return "\n".join(json.dumps({"type": "stream_event", **event}) for event in events)


def test_the_words_written_so_far_are_the_latest_messages_text():
    from workers_projects_runtime.coordinator import text_written_so_far
    log = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        _stream({"event": {"type": "message_start"}}),
        _stream({"event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}}),
        _stream({"event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me hand that off."}}}),
        _stream({"event": {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use"}}}),
        _stream({"event": {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}}}),
        _stream({"parent_tool_use_id": "sub", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "SUB-AGENT"}}}),
        _stream({"event": {"type": "message_start"}}),
        _stream({"event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}}),
        _stream({"event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Started: "}}}),
        _stream({"event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "the report job."}}}),
    ])
    assert text_written_so_far(log) == "Started: the report job."
    assert text_written_so_far("") == ""


def test_the_owners_own_chat_shows_the_reply_while_it_is_written_and_a_channel_does_not(app, monkeypatch):
    application, client = app
    experts = application.state.experts
    coordinator = experts.coordinator
    expert = create_expert(client)
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    channel = connected_channel(application, client, expert)
    person = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-stream")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    coordinator.accept_turn("local", owner, chat["conversation_id"], "own-turn", "Explain write-ahead logs.")
    with experts.store._connect() as conn:
        for cid, tid in ((chat["conversation_id"], "own-turn"), (person["conversation_id"], person["turn_id"])):
            conn.execute("UPDATE coordinator_turns SET request_id=?,blocker='' WHERE conversation_id=? AND turn_id=?",
                         ("request-" + tid, cid, tid))
    monkeypatch.setattr(coordinator, "_written_so_far", lambda request_id: "A write-ahead log records each change")
    own = coordinator.snapshot("local", owner, chat["conversation_id"])
    assert [t.get("partial_reply") for t in own["turns"] if t["turn_id"] == "own-turn"] == ["A write-ahead log records each change"]
    # Another person on a channel: nothing appears before the turn has finished.
    theirs = coordinator.snapshot("local", owner, person["conversation_id"])
    assert not any(t.get("partial_reply") for t in theirs["turns"])
    # A finished turn shows its reply, not the words written so far.
    finish_turn(application, chat["conversation_id"], "own-turn", "Done.")
    assert not any(t.get("partial_reply") for t in coordinator.snapshot("local", owner, chat["conversation_id"])["turns"])


def test_the_words_so_far_are_read_from_the_running_turns_own_log(app, monkeypatch):
    application, client = app
    experts = application.state.experts
    coordinator, store = experts.coordinator, experts.store
    owner = "owner-synthetic"
    project = store.create_project(owner, "Synthetic", "Synthetic project", "claude-code")
    worker = store.create_worker(project["project_id"], owner, "Queen", "speaker", "claude-code", "claude-code",
                                 "claude-code", "haiku", execution_mode="host")
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic turn")
    session = store.upsert_provider_session(tenant_id="local", owner_id=owner, conversation_id="coordinator-synthetic",
                                            agent_id="coordinator", model_id="claude-code:haiku",
                                            project_id=project["project_id"], worker_id=worker["worker_id"],
                                            workspace_dir="/synthetic/workspace", access_mode="read_write")
    request, _ = store.create_provider_request(tenant_id="local", owner_id=owner, session_id=session["session_id"],
                                               idempotency_key="stream-1", message_id="m1", stream_id="s1",
                                               requested_history_count=0)
    store.update_provider_request(request["request_id"], run_id=run["run_id"])
    with store._connect() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (run["run_id"],))
    log = "\n".join([_stream({"event": {"type": "message_start"}}),
                     _stream({"event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}}),
                     _stream({"event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "First words"}}})])
    seen = []
    monkeypatch.setattr(coordinator, "_run_log", lambda r: seen.append(r["run_id"]) or log)
    assert coordinator._written_so_far(request["request_id"]) == "First words" and seen == [run["run_id"]]
    with store._connect() as conn:
        conn.execute("UPDATE runs SET state='completed' WHERE run_id=?", (run["run_id"],))
    assert coordinator._written_so_far(request["request_id"]) == ""  # a finished run shows only its reply


def test_the_skills_chosen_on_an_expert_travel_with_its_jobs_claude_workers(app):
    from workers_projects_runtime.coordinator import ExpertBinding
    from workers_projects_runtime.service import _contains_parallel_forbidden_authority_key, _validate_parallel_clean_room_files
    application, client = app
    experts = application.state.experts
    skill = client.post("/v1/personal-library", json={
        "name": "synthetic-brief", "kind": "skill", "description": "Write a synthetic brief.", "profiles": ["claude-code"],
        "files": [{"path": ".claude/skills/synthetic-brief/SKILL.md",
                   "content": "---\nname: synthetic-brief\ndescription: Write a synthetic brief\n---\nWrite it.\n"}],
        "idempotency_key": "synthetic-brief-skill"})
    assert skill.status_code == 201, skill.text
    ref = {"item_id": skill.json()["item_id"], "version_id": skill.json()["version_id"]}
    created = client.post("/v1/experts", json={"name": "Synthetic Expert", "description": "Answers engineering questions.",
                                               "expert": SPEC, "personal_library_refs": [ref]})
    assert created.status_code == 201, created.text
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created.json()["template_id"])
    template = experts.resolve("local", owner, created.json()["template_id"])
    config = experts._job_config("local", owner, template, ExpertBinding(lineage_id=template["lineage_id"], role="job"))
    carried = {route.profile: route.bootstrap_bundle.get("expert_skills") for route in config.routes}
    assert carried["claude-code"] == [{"name": "synthetic-brief", "files": [
        {"path": "SKILL.md", "content": "---\nname: synthetic-brief\ndescription: Write a synthetic brief\n---\nWrite it.\n"}]}]
    # The Library page saves every Skill as Claude Code only; the Library's own rule (text files run on
    # every AI with a native Skills place) decides instead, so a Codex coordinator or Worker gets it too.
    assert carried["codex-cli"] == carried["claude-code"]
    assert config.bootstrap_bundle.get("expert_skills") == carried["claude-code"]  # the coordinator (Codex) as well
    both = client.post("/v1/personal-library", json={
        "name": "synthetic-both", "kind": "skill", "description": "For both AIs.", "profiles": ["claude-code", "codex-cli"],
        "files": [{"path": ".claude/skills/synthetic-both/SKILL.md", "content": "---\nname: synthetic-both\ndescription: Both\n---\nDo it.\n"}],
        "idempotency_key": "synthetic-both-skill"}).json()
    created2 = client.post("/v1/experts", json={"name": "Synthetic Expert Two", "description": "Two AIs.", "expert": SPEC,
                                                "personal_library_refs": [{"item_id": both["item_id"], "version_id": both["version_id"]}]}).json()
    template2 = experts.resolve("local", owner, created2["template_id"])
    config2 = experts._job_config("local", owner, template2, ExpertBinding(lineage_id=template2["lineage_id"], role="job"))
    assert {route.profile: [s["name"] for s in route.bootstrap_bundle.get("expert_skills") or []] for route in config2.routes} == {
        "claude-code": ["synthetic-both"], "codex-cli": ["synthetic-both"]}
    # A dispatched Worker's clean-room checks accept the typed list (nothing is placed under .claude/).
    bundle = dict(next(route.bootstrap_bundle for route in config.routes if route.profile == "claude-code"))
    _validate_parallel_clean_room_files(bundle)
    assert not _contains_parallel_forbidden_authority_key(bundle)


def test_a_skill_that_travels_natively_is_not_offered_again_as_pending_library_material(app):
    application, client = app
    experts = application.state.experts
    skill = client.post("/v1/personal-library", json={
        "name": "synthetic-brief", "kind": "skill", "description": "Write a synthetic brief.", "profiles": ["claude-code"],
        "files": [{"path": ".claude/skills/synthetic-brief/SKILL.md",
                   "content": "---\nname: synthetic-brief\ndescription: Write a synthetic brief\n---\nWrite it.\n"}],
        "idempotency_key": "synthetic-brief-skill"}).json()
    created = client.post("/v1/experts", json={"name": "Synthetic Expert", "description": "Answers engineering questions.",
                                               "expert": {**SPEC, "ask_me_first": "off"}, "personal_library_refs": [
                                                   {"item_id": skill["item_id"], "version_id": skill["version_id"]}]}).json()
    chat = client.post(f"/v1/experts/{created['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    cid = experts._job(job["job_id"])["job_conversation_id"]
    library = json.loads(scalar(application, "SELECT library_json FROM coordinator_turns WHERE conversation_id=? "
                                             "AND turn_id='job-brief'", cid) or "{}")
    # The job carries the Skill natively (its coordinator and Workers load it), so the brief does not list it
    # among Library requirements that wait for review.
    assert not library.get("refs") and not library.get("pending")


def test_a_job_whose_goal_never_started_ends_with_the_coordinators_answer_told_once(app):
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    store = experts.store
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    cid = experts._job(job["job_id"])["job_conversation_id"]
    with store._connect() as conn:
        conn.execute("UPDATE expert_jobs SET state='running' WHERE job_id=?", (job["job_id"],))
        conn.execute("UPDATE coordinator_turns SET blocker='' WHERE conversation_id=?", (cid,))
        conn.execute("INSERT INTO coordinator_goals(conversation_id,goal_id,source_turn_id,text,created_at) "
                     "VALUES(?,?,?,?,?)", (cid, "g1", "job-brief", "Synthetic goal", "t"))

    def told():
        return scalar(application, "SELECT count(*) FROM coordinator_events WHERE conversation_id=? AND kind='expert_job_result'",
                      chat["conversation_id"])

    experts.reconcile_once()  # the coordinator is still writing its answer
    assert experts._job(job["job_id"])["state"] == "running" and told() == 0
    finish_turn(application, cid, "job-brief", "The job is blocked: no ready account for the helper.")
    # A start that is only waiting for capacity, or a helper still running, keeps the job open.
    with store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET dispatch_json='{\"goal_id\":\"g1\",\"route_id\":\"codex-cli\"}',"
                     "blocker='host_capacity' WHERE conversation_id=? AND goal_id='g1'", (cid,))
    experts.reconcile_once()
    assert experts._job(job["job_id"])["state"] == "running" and told() == 0
    project = store.create_project(owner, "Synthetic", "Synthetic project", "claude-code")
    worker = store.create_worker(project["project_id"], owner, "Helper", "worker", "claude-code", "claude-code",
                                 "claude-code", "haiku", execution_mode="host")
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic helper")
    with store._connect() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (run["run_id"],))
        conn.execute("UPDATE coordinator_goals SET blocker='',worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='g1'",
                     (worker["worker_id"], run["run_id"], cid))
    experts.reconcile_once()
    assert experts._job(job["job_id"])["state"] == "running" and told() == 0
    # The goal the coordinator could not start and left: nothing will ever start it, so the job ends with
    # the coordinator's own answer and the Queen is told, once.
    with store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET dispatch_json='',worker_id='',run_id='',blocker='' "
                     "WHERE conversation_id=? AND goal_id='g1'", (cid,))
    experts.reconcile_once()
    experts.reconcile_once()
    finished = experts._job(job["job_id"])
    assert finished["state"] == "done" and finished["result_text"] == "The job is blocked: no ready account for the helper."
    assert told() == 1


def test_a_sandboxed_experts_skills_survive_the_clean_room_dispatch_and_reach_container_workers(app, tmp_path, monkeypatch):
    from workers_projects_runtime.coordinator import ExpertBinding
    from workers_projects_runtime.profile_runtime import ClaudeCodeRuntime, CodexCliRuntime
    from workers_projects_runtime.service import derive_parallel_clean_room_bootstrap, _validate_parallel_clean_room_files
    application, client = app
    experts = application.state.experts
    content = "---\nname: synthetic-brief\ndescription: Write a synthetic brief\n---\nWrite it.\n"
    skill = client.post("/v1/personal-library", json={
        "name": "synthetic-brief", "kind": "skill", "description": "Write a synthetic brief.", "profiles": ["claude-code"],
        "files": [{"path": ".claude/skills/synthetic-brief/SKILL.md", "content": content}],
        "idempotency_key": "synthetic-brief-skill"}).json()
    created = client.post("/v1/experts", json={"name": "Synthetic Sandboxed", "description": "Works in containers.",
                                               "expert": {**SPEC, "workspace": {"kind": "sandboxed", "access": "workspace"}},
                                               "personal_library_refs": [{"item_id": skill["item_id"],
                                                                          "version_id": skill["version_id"]}]})
    assert created.status_code == 201, created.text
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created.json()["template_id"])
    template = experts.resolve("local", owner, created.json()["template_id"])
    config = experts._job_config("local", owner, template, ExpertBinding(lineage_id=template["lineage_id"], role="job"))
    monkeypatch.setenv("WPR_CODEX_DANGEROUS", "0")
    for route in config.routes:
        assert route.execution_mode == "docker"
        bundle = dict(route.bootstrap_bundle)
        _validate_parallel_clean_room_files(bundle)
        # The same projection dispatch and storage apply to a container Worker's bundle.
        _, dispatched = derive_parallel_clean_room_bootstrap(None, bundle)
        assert dispatched["execution_policy"] == "parallel-clean-room-v1"
        assert dispatched["expert_skills"] == [{"name": "synthetic-brief", "files": [{"path": "SKILL.md", "content": content}]}]
        # The clean-room network, as the container sandbox tests configure it (no container runs here).
        for name, value in {"WPR_PARALLEL_CLEAN_ROOM_NETWORK": "glasshive-parallel-clean-room",
                            "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL": "http://provider-egress:8080",
                            "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER": "glasshive-provider-egress",
                            "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER": "glasshive-capability-broker-proxy",
                            "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK": "provider-egress"}.items():
            monkeypatch.setenv(name, value)
        runtime = (ClaudeCodeRuntime if route.profile == "claude-code" else CodexCliRuntime)(str(tmp_path / route.profile))
        worker = {"worker_id": "wrk_sandboxed_" + route.profile.replace("-", "_"), "name": "Helper", "profile": route.profile,
                  "execution_mode": "docker", "bootstrap_bundle_json": json.dumps(dispatched)}
        command, _ = runtime._build_command(worker, "Do the work.", runtime._runtime_info(worker))
        home = runtime._home_dir(worker["worker_id"])
        if route.profile == "claude-code":
            inside = f"{runtime.sandbox.home_mount}/.glasshive/expert-skills/synthetic-brief"
            assert ["--plugin-dir", inside] in [command[i:i + 2] for i in range(len(command) - 1)]
            assert (home / ".glasshive" / "expert-skills" / "synthetic-brief" / "SKILL.md").read_text() == content
        else:
            assert (home / ".agents" / "skills" / "synthetic-brief" / "SKILL.md").read_text() == content


def test_a_low_disk_says_plainly_that_the_expert_cannot_answer(app, monkeypatch):
    # Tested at the guard, not by filling the disk.
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    with experts.store._connect() as conn:  # the guard refused to start its turn
        conn.execute("UPDATE coordinator_turns SET blocker='host_capacity' WHERE conversation_id=? AND turn_id=?",
                     (accepted["conversation_id"], accepted["turn_id"]))
    monkeypatch.setattr(experts.service, "_native_storage_capacity", lambda *args, **kwargs: {
        "version": 1, "healthy": False, "availableBytes": 900 * 1024**2, "requiredBytes": 3072 * 1024**2})
    notices = client.get("/v1/expert-notices").json()["items"]
    low = [item for item in notices if item["kind"] == "low_disk"]
    assert low and low[0]["waiting"] == 1 and low[0]["available_mb"] == 900 and low[0]["required_mb"] == 3072
    view = client.get(f"/v1/experts/{expert['template_id']}/channels").json()["items"][0]
    assert view["low_disk"] is True
    monkeypatch.setattr(experts.service, "_native_storage_capacity", lambda *args, **kwargs: {
        "version": 1, "healthy": True, "availableBytes": 9000 * 1024**2, "requiredBytes": 3072 * 1024**2})
    assert not [item for item in client.get("/v1/expert-notices").json()["items"] if item["kind"] == "low_disk"]


def test_the_talking_model_is_chosen_checked_and_timed(app, monkeypatch):
    application, client = app
    experts = application.state.experts
    created = create_expert(client, team={**SPEC["team"], "talk": {"profile": "claude-code", "effort": "low",
                                                                  "model": "claude-code:haiku"}})
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created["template_id"])
    assert experts.spec(experts.resolve("local", owner, created["template_id"])).team.talk.model == "claude-code:haiku"
    # A model of another AI is refused for this role.
    monkeypatch.setattr(experts.coordinator.provider, "_model", lambda model_id, **_: SimpleNamespace(
        harness_profile=model_id.split(":", 1)[0], effort_choices=("low", "high"), native_model="synthetic",
        connection_id="", recommended_effort="high", id=model_id))
    refused = client.post("/v1/experts", json={"name": "Synthetic Expert", "expert": {
        **SPEC, "team": {**SPEC["team"], "talk": {"profile": "codex-cli", "model": "claude-code:haiku"}}}})
    assert refused.status_code == 422 and "not offered" in refused.text
    # The median time to a finished reply, per talking model, from this computer's own replies.
    chat = client.post(f"/v1/experts/{created['template_id']}/chat").json()
    store = experts.store
    project = store.create_project(owner, "Synthetic", "Synthetic project", "claude-code")
    worker = store.create_worker(project["project_id"], owner, "Queen", "speaker", "claude-code", "claude-code",
                                 "claude-code", "haiku", execution_mode="host")
    session = store.upsert_provider_session(tenant_id="local", owner_id=owner, conversation_id=chat["conversation_id"],
                                            agent_id="coordinator", model_id="claude-code:haiku",
                                            project_id=project["project_id"], worker_id=worker["worker_id"],
                                            workspace_dir="/synthetic/workspace", access_mode="read_write")
    now = datetime.now(UTC)
    for index, seconds in enumerate([3.0, 4.0, 9.0]):
        request, _ = store.create_provider_request(tenant_id="local", owner_id=owner, session_id=session["session_id"],
                                                   idempotency_key=f"speed-{index}", message_id=f"m{index}",
                                                   stream_id=f"s{index}", requested_history_count=0)
        run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic turn")
        with store._connect() as conn:
            conn.execute("UPDATE runs SET state='completed',queued_at=?,ended_at=? WHERE run_id=?",
                         ((now - timedelta(seconds=seconds)).isoformat(), now.isoformat(), run["run_id"]))
        store.update_provider_request(request["request_id"], run_id=run["run_id"])
    speeds = client.get("/v1/experts-talk-speeds").json()["items"]
    assert speeds == [{"model": "claude-code:haiku", "median_s": 4.0, "replies": 3}]


def test_the_role_model_becomes_its_conversation_model(app, monkeypatch):
    application, client = app
    experts = application.state.experts
    import workers_projects_runtime.coordinator_config as coordinator_config
    monkeypatch.setattr(coordinator_config, "configured_coordinator", lambda *args, **kwargs: CoordinatorConfig(
        model="claude-code:opus", effort="high",
        routes=[Route(id="default", profile="claude-code", model="claude-code:opus", effort="high", execution_mode="host")]))
    config = experts._default_role_config("local", "owner", ExpertRole(profile="claude-code", model="claude-code:haiku",
                                                                         effort="low"))
    assert config.model == "claude-code:haiku" and config.effort == "low"
    default = experts._default_role_config("local", "owner", ExpertRole(profile="claude-code"))
    assert default.model == "claude-code:opus"


def test_each_ai_that_talks_offers_its_faster_model(app, monkeypatch):
    application, client = app
    from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS
    for model_id, profile, native in (("claude-code:haiku", "claude-code", "haiku"),
                                      ("codex-cli:gpt-6.2-sol", "codex-cli", "gpt-6.2-sol")):
        model = GLASSHIVE_MODELS[model_id]
        assert (model.harness_profile, model.native_model, model.recommended_effort) == (profile, native, "low")
    experts = application.state.experts
    provider = experts.coordinator.provider
    real = type(provider)._model  # the save check uses the real catalog, not the fixture's stand-in
    monkeypatch.setattr(provider, "_model", lambda *args, **kwargs: real(provider, *args, **kwargs))
    created = create_expert(client, team={**SPEC["team"], "talk": {"profile": "codex-cli", "effort": "low",
                                                                  "model": "codex-cli:gpt-6.2-sol"}})
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created["template_id"])
    assert experts.spec(experts.resolve("local", owner, created["template_id"])).team.talk.model == "codex-cli:gpt-6.2-sol"


def test_every_worker_of_a_job_carries_the_owners_instructions(app):
    # The owner's guardrails reach each Worker whatever the coordinator writes in its dispatch.
    application, client = app
    experts = application.state.experts
    created = create_expert(client)
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created["template_id"])
    template = experts.resolve("local", owner, created["template_id"])
    config = experts._job_config("local", owner, template, ExpertBinding(lineage_id=template["lineage_id"], role="job",
                                                                         access="full"))
    assert config.routes
    for route in config.routes:
        assert SPEC["instructions"] in route.bootstrap_bundle["system_instructions"]
