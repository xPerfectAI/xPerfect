"""Experts people can talk to: settings, channels, outbox and Ask me first, on the stub runtime."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.coordinator import CoordinatorConfig, Route, canonical
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
    items = client.get("/v1/expert-notices").json()["items"]
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
