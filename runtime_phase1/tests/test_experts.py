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
    # Maintenance runs only when a test calls it: the background scheduler would otherwise land between steps a
    # test orders on purpose (an approval paused before its release, then a removal or Off).
    monkeypatch.setattr(experts.service, "_process_scheduler_cycle", lambda: None)
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
    experts = application.state.experts
    owner, speaker, job_id, cid = _job_answered(application, client, "Synthetic result")
    experts.reconcile_once()
    experts.reconcile_once()
    experts._publish(experts._job(job_id), "done", "Synthetic result")  # telling again changes nothing
    assert _results(application, job_id) == ["Synthetic result"]
    assert experts._job(job_id)["state"] == "done"


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
    # The job's conversation is marked stopped in the same transaction as the job, so a Stop whose fence did
    # not land (the process ended right after it) is completed by the coordinator.
    with monkeypatch.context() as crash:
        crash.setattr(experts, "_fence_job", lambda job: None)
        experts.stop_job(second["job_id"])
    assert core.snapshot("local", owner, cids[1])["stop_scope"] == "goals"
    core.reconcile_once()
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
    assert experts.release(reply["review_id"]) == "nothing"
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


def test_nothing_leaves_a_conversation_stopped_after_its_reply_finished(app, monkeypatch):
    # Review 2, finding 5: a reply finished, then Stop, before the outbox pass or the claim.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    monkeypatch.setattr(experts.coordinator.provider, "cancel_by_idempotency", lambda *args, **kwargs: {"capacity_released": True})
    first = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    finish_turn(application, first["conversation_id"], first["turn_id"], "SYNTHETIC-AFTER-STOP")
    experts.coordinator.stop_conversation("local", owner, first["conversation_id"])
    channels.reconcile_once()
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []
    # A message already waiting when the Stop lands (a race past the Stop's own drop) is dropped at the claim.
    client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": [PERSON, {**PERSON, "id": "person-synthetic-b"}]})
    second = client.post(f"/v1/channels/{channel['binding_id']}/inbound",
                         json=message("act-2", person={**PERSON, "id": "person-synthetic-b"})).json()
    finish_turn(application, second["conversation_id"], second["turn_id"], "SYNTHETIC-RACED")
    channels.reconcile_once()
    with experts.store._connect() as conn:
        conn.execute("UPDATE coordinator_conversations SET stop_scope='stopped' WHERE conversation_id=?", (second["conversation_id"],))
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []
    assert scalar(application, "SELECT error FROM channel_deliveries WHERE turn_id=?", second["turn_id"]) == "The conversation was stopped"


def test_messages_waiting_in_a_restored_copy_never_use_up_the_claim(app):
    # Second review, a note: restore-held messages skipped after the limit starved everything behind them.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    held = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-held")).json()
    other = {**PERSON, "id": "person-synthetic-b"}
    client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": [PERSON, other]})
    ready = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-ready", person=other)).json()
    with experts.store._connect() as conn:
        for i in range(12):  # twelve messages of a restored copy, older than the ready one
            conn.execute("INSERT INTO channel_deliveries(delivery_id,binding_id,conversation_id,turn_id,kind,text,thread_ref_json,"
                         "state,review_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (f"dlv_held_{i}", channel["binding_id"], held["conversation_id"], f"held-{i}", "reply", "HELD", "{}",
                          "pending", "", f"2000-01-01T00:00:{i:02d}"))
        conn.execute("UPDATE coordinator_conversations SET restore_hold=1 WHERE conversation_id=?", (held["conversation_id"],))
    finish_turn(application, ready["conversation_id"], ready["turn_id"], "SYNTHETIC-READY")
    application.state.channels.reconcile_once()
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim", params={"limit": 10}).json()["items"]
    assert [item["text"] for item in sent] == ["SYNTHETIC-READY"]


@pytest.mark.parametrize("fence", ["stopped", "restored"])
def test_an_interrupted_approval_is_never_released_past_a_stop_or_a_restore_hold(app, fence):
    # Review 2, findings 3 and 5: the approval committed, its release did not; recovery must respect the fences.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    experts.ask_owner("local", owner, cid, reason="Unusual request", reply="SYNTHETIC-APPROVED-TEXT",
                      idempotency_key="ask-1", source_turn_id=accepted["turn_id"])
    with experts.store._connect() as conn:  # approved, but the release never ran
        conn.execute("UPDATE expert_reviews SET state='approved',decided_at='t' WHERE speaker_conversation_id=?", (cid,))
        column = "stop_scope='stopped'" if fence == "stopped" else "restore_hold=1"
        conn.execute(f"UPDATE coordinator_conversations SET {column} WHERE conversation_id=?", (cid,))
    experts.reconcile_once()
    channels.reconcile_once()
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []
    if fence == "restored":  # once the owner resumes the copy, the approval is released once
        with experts.store._connect() as conn:
            conn.execute("UPDATE coordinator_conversations SET restore_hold=0 WHERE conversation_id=?", (cid,))
        experts.reconcile_once()
        experts.reconcile_once()
        sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
        assert [item["text"] for item in sent] == ["SYNTHETIC-APPROVED-TEXT"]


@pytest.mark.parametrize("closed", ["removed", "off"])
def test_a_removed_person_or_a_channel_turned_off_gets_nothing_new_started_or_asked(app, closed):
    # Review 2, finding 6: a late tool call of their speaking turn, and an approval racing the removal.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    experts.ask_owner("local", owner, cid, reason="Unusual request", reply="SYNTHETIC-RACED-APPROVAL",
                      idempotency_key="ask-1", source_turn_id=accepted["turn_id"])
    review = experts.reviews("local", owner)[0]
    with experts.store._connect() as conn:  # the removal lands, its own clean-up not yet
        if closed == "removed":
            conn.execute("UPDATE channel_bindings SET people_json='[]' WHERE binding_id=?", (channel["binding_id"],))
        else:
            conn.execute("UPDATE channel_bindings SET state='off' WHERE binding_id=?", (channel["binding_id"],))
    with pytest.raises(ExpertError, match="no longer be served"):
        experts.hand_off("local", owner, cid, goal="Synthetic late job", idempotency_key="late-1", source_turn_id=accepted["turn_id"])
    with pytest.raises(ExpertError, match="no longer be served"):
        experts.ask_owner("local", owner, cid, reason="Late", reply="SYNTHETIC-LATE", idempotency_key="late-2",
                          source_turn_id=accepted["turn_id"])
    with pytest.raises(ExpertError, match="no longer be served"):
        experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    assert experts._review(review["review_id"])["state"] == "withdrawn"
    assert scalar(application, "SELECT count(*) FROM expert_jobs") == 0


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
    with monkeypatch.context() as crash:  # the approval is recorded; its release never runs
        crash.setattr(experts, "release", lambda *_: (_ for _ in ()).throw(RuntimeError("stopped")))
        with pytest.raises(RuntimeError):
            experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
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
    sandboxed = create_expert(client, workspace={"kind": "sandboxed", "access": "workspace"},
                              team={**SPEC["team"], "coordinate": {"profile": "claude-code"}})
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", sandboxed["template_id"])
    template = experts.resolve("local", owner, sandboxed["template_id"])
    binding = ExpertBinding(lineage_id=template["lineage_id"], role="job", access="workspace")
    config = experts._job_config("local", owner, template, binding)
    assert config.routes and {route.execution_mode for route in config.routes} == {"docker"}


def test_switching_to_off_withdraws_what_waited_and_tells_each_person(app):
    # Review 2, finding 13: Off means nobody is asked; nothing held is sent or started by itself.
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, cid, goal="Synthetic held job", idempotency_key="k1", source_turn_id=accepted["turn_id"])
    later = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-2")).json()
    finish_turn(application, cid, later["turn_id"], "SYNTHETIC-HELD-REPLY")
    application.state.channels.reconcile_once()
    assert {review["kind"] for review in experts.reviews("local", owner)} == {"job", "reply"}
    read = client.get(f"/v1/experts/{expert['template_id']}").json()
    updated = client.patch(f"/v1/experts/{expert['template_id']}", json={
        "expected_catalog_revision": read["catalog_revision"], "expert": {**read["expert"], "ask_me_first": "off"}})
    assert updated.status_code == 200, updated.text
    assert experts.reviews("local", owner) == []  # nothing left waiting in Needs you
    assert experts._job(job["job_id"])["state"] == "withdrawn"
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE origin='owner_decision' "
                               "AND message LIKE '%switched Ask me first to Off%'") == 2
    application.state.channels.reconcile_once()
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert not any("SYNTHETIC-HELD-REPLY" in item["text"] for item in sent)


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
                                               "expert": {**SPEC, "workspace": {"kind": "sandboxed", "access": "workspace"},
                                                          "team": {**SPEC["team"], "coordinate": {"profile": "claude-code"}}},
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


def test_a_job_whose_helper_still_runs_is_not_ended_by_a_later_failed_turn(app):
    # Review 2, finding 8.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts, store = application.state.experts, application.state.experts.store
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    cid = experts._job(job["job_id"])["job_conversation_id"]
    project = store.create_project(owner, "Synthetic", "Synthetic project", "claude-code")
    worker = store.create_worker(project["project_id"], owner, "Helper", "worker", "claude-code", "claude-code",
                                 "claude-code", "haiku", execution_mode="host")
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic helper")
    with store._connect() as conn:
        conn.execute("UPDATE expert_jobs SET state='running' WHERE job_id=?", (job["job_id"],))
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (run["run_id"],))
        conn.execute("INSERT INTO coordinator_goals(conversation_id,goal_id,source_turn_id,text,dispatch_json,worker_id,run_id,"
                     "created_at) VALUES(?,?,?,?,?,?,?,?)", (cid, "g1", "job-brief", "Synthetic goal", "{}", worker["worker_id"],
                                                          run["run_id"], "t"))
    finish_turn(application, cid, "job-brief", "Started a helper.")
    with store._connect() as conn:  # a later change turn failed while the helper still runs
        conn.execute("INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,blocker,created_at) "
                     "VALUES(?,?,?,?,?,?)", (cid, "change-1", "Synthetic change", "interactive", "failed", "9999-12-31"))
    experts.reconcile_once()
    assert experts._job(job["job_id"])["state"] == "running"
    assert scalar(application, "SELECT count(*) FROM coordinator_events WHERE kind='expert_job_result'") == 0


def test_a_job_whose_first_turn_is_refused_for_good_is_told_not_left_open(app):
    # Review 2, finding 12 (second case).
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    cid = experts._job(job["job_id"])["job_conversation_id"]
    with experts.store._connect() as conn:
        conn.execute("UPDATE expert_jobs SET state='running' WHERE job_id=?", (job["job_id"],))
        conn.execute("UPDATE coordinator_turns SET blocker='provider_auth_missing',request_id='' WHERE conversation_id=?", (cid,))
    experts.reconcile_once()
    experts.reconcile_once()
    finished = experts._job(job["job_id"])
    assert finished["state"] == "failed" and finished["result_text"] == "The job could not start: provider_auth_missing"
    assert scalar(application, "SELECT count(*) FROM coordinator_events WHERE kind='expert_job_result'") == 1


def test_a_job_interrupted_while_it_was_prepared_is_prepared_again_or_told(app):
    # Review 2, finding 12 (first case): a crash after the job was recorded, before it started.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    recent = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    old = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    with experts.store._connect() as conn:
        for job_id, stamp in (("job_interrupted", recent), ("job_never_prepared", old)):
            conn.execute("INSERT INTO expert_jobs(job_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,idempotency_key,"
                         "goal,context,state,template_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, "local", owner, expert["lineage_id"], chat["conversation_id"], job_id, "Synthetic job", "",
                          "starting", expert["template_id"], stamp, stamp))
    experts.reconcile_once()
    prepared = experts._job("job_interrupted")
    assert prepared["state"] == "running" and prepared["job_conversation_id"]
    told = experts._job("job_never_prepared")
    assert told["state"] == "failed" and told["result_text"] == "The job could not start."
    assert scalar(application, "SELECT count(*) FROM coordinator_events WHERE kind='expert_job_result' AND identity=?",
                  "job_never_prepared") == 1


def test_in_a_sandboxed_workspace_nothing_of_the_expert_works_on_this_computer(app):
    # Review 2, finding 4: the coordinator runs on this computer, so its own tools stay off there.
    from workers_projects_runtime.coordinator import ExpertBinding
    application, client = app
    experts = application.state.experts
    sandboxed = {"kind": "sandboxed", "access": "workspace"}
    refused = client.post("/v1/experts", json={"name": "Synthetic Sandboxed", "expert": {**SPEC, "workspace": sandboxed}})
    assert refused.status_code == 422 and "Claude talks and coordinates" in refused.text  # a Codex coordinator
    with_backup = {**SPEC["team"], "coordinate": {"profile": "claude-code", "backup": {"profile": "codex-cli"}}}
    assert client.post("/v1/experts", json={"name": "Synthetic Sandboxed", "expert": {
        **SPEC, "workspace": sandboxed, "team": with_backup}}).status_code == 422  # a Codex backup would work here too
    team = {**SPEC["team"], "coordinate": {"profile": "claude-code"}}
    created = client.post("/v1/experts", json={"name": "Synthetic Sandboxed", "expert": {**SPEC, "workspace": sandboxed, "team": team}})
    assert created.status_code == 201, created.text
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created.json()["template_id"])
    template = experts.resolve("local", owner, created.json()["template_id"])
    config = experts._job_config("local", owner, template, ExpertBinding(lineage_id=template["lineage_id"], role="job"))
    assert config.bootstrap_bundle["provider_capabilities"]["coordination_only"] is True
    assert "expert_skills" not in config.bootstrap_bundle
    assert {route.execution_mode for route in config.routes} == {"docker"}


@pytest.mark.parametrize("saved", ["host_read_only", "sandboxed_codex_coordinator"])
def test_an_expert_saved_before_the_access_rules_never_starts_work_past_them(app, saved, monkeypatch):
    # Review 2, finding 7 (and 4 for older records): the save-time rules hold again when work starts.
    import workers_projects_runtime.experts as experts_module
    application, client = app
    experts = application.state.experts
    workspace = ({"kind": "host", "access": "read_only"} if saved == "host_read_only"
                 else {"kind": "sandboxed", "access": "workspace"})
    with monkeypatch.context() as before_the_rule:  # saved while the rule did not exist yet
        before_the_rule.setattr(experts_module, "_access_refusal", lambda spec: "")
        expert = create_expert(client, ask_me_first="off", workspace=workspace)
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    with experts.store._connect() as conn:  # a job approved earlier, still waiting to start
        conn.execute("INSERT INTO expert_jobs(job_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,idempotency_key,"
                     "goal,context,state,template_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("job_held_before", "local", owner, expert["lineage_id"], chat["conversation_id"], "held-before",
                      "Synthetic job", "", "starting", expert["template_id"], "t", "t"))
    with pytest.raises(ExpertError, match="Nothing was started"):
        experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    assert scalar(application, "SELECT count(*) FROM expert_jobs WHERE idempotency_key='k1'") == 0
    held = experts._start_job("job_held_before")
    assert held["state"] == "failed" and held["result_text"].startswith("The job could not start: ")
    assert not held["job_conversation_id"]
    assert scalar(application, "SELECT count(*) FROM coordinator_events WHERE kind='expert_job_result' AND identity=?",
                  "job_held_before") == 1


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


# ---------------------------------------------------------------- review 3: the outcome table
# Each test names its cells (a state, an event) and the one allowed outcome from the build's design record.


def _always_with_held_work(application, client):
    """A channel person on Always ask with a held job and a held reply, each waiting for the owner."""
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, cid, goal="Synthetic held job", idempotency_key="k1",
                           source_turn_id=accepted["turn_id"])
    later = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-2")).json()
    finish_turn(application, cid, later["turn_id"], "SYNTHETIC-HELD-REPLY")
    application.state.channels.reconcile_once()
    assert {review["kind"] for review in experts.reviews("local", owner)} == {"job", "reply"}
    return expert, channel, owner, cid, job, accepted


def _close(application, channel, cid, closed):
    """The change itself, committed without the clean-up that follows it (a crash, or a call already inside)."""
    with application.state.experts.store._connect() as conn:
        if closed == "stopped":
            conn.execute("UPDATE coordinator_conversations SET stop_scope='stopped' WHERE conversation_id=?", (cid,))
        elif closed == "removed":
            conn.execute("UPDATE channel_bindings SET people_json='[]' WHERE binding_id=?", (channel["binding_id"],))
        else:
            conn.execute("UPDATE channel_bindings SET state='off' WHERE binding_id=?", (channel["binding_id"],))


@pytest.mark.parametrize("closed", ["stopped", "removed", "off"])
def test_work_asked_for_after_a_stop_or_a_removal_is_refused_where_it_is_written(app, closed, monkeypatch):
    # Review 3, B2 (check at the exit): the tool call entered before the change; the write checks again.
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    monkeypatch.setattr(experts, "_assert_channel_open", lambda speaker: None)  # its door was passed before
    _close(application, channel, cid, closed)
    with pytest.raises(ExpertError, match="Nothing was started"):
        experts.hand_off("local", owner, cid, goal="Synthetic late job", idempotency_key="late-1",
                         source_turn_id=accepted["turn_id"])
    with pytest.raises(ExpertError, match="Nothing was asked"):
        experts.ask_owner("local", owner, cid, reason="Late", job_goal="Synthetic late job", idempotency_key="late-2",
                          source_turn_id=accepted["turn_id"])
    assert scalar(application, "SELECT count(*) FROM expert_jobs") == 0
    assert scalar(application, "SELECT count(*) FROM expert_reviews") == 0


@pytest.mark.parametrize("closed", ["stopped", "removed", "off"])
def test_stop_a_removal_or_the_channel_off_closes_everything_in_its_own_transaction(app, closed, monkeypatch):
    # Review 4 (the one withdraw, in the change's own transaction): even if the process ends right after the
    # change, before any fence is completed, every request is withdrawn, every job stopped with its conversation
    # marked stopped, and nothing is left to send.
    application, client = app
    expert, channel, owner, cid, held, accepted = _always_with_held_work(application, client)
    experts = application.state.experts
    monkeypatch.setattr(experts.coordinator.provider, "cancel_by_idempotency",
                        lambda *args, **kwargs: {"capacity_released": True})
    review = next(review for review in experts.reviews("local", owner) if review["kind"] == "job")
    experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    running = experts._job(held["job_id"])
    assert running["state"] == "running" and running["job_conversation_id"]
    waiting = experts.hand_off("local", owner, cid, goal="Synthetic second job", idempotency_key="k2",
                               source_turn_id=accepted["turn_id"])
    with monkeypatch.context() as crash:
        crash.setattr(experts, "_fence_job", lambda job: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            if closed == "stopped":
                experts.coordinator.stop_conversation("local", owner, cid)
            else:
                client.patch(f"/v1/channels/{channel['binding_id']}",
                             json={"people": []} if closed == "removed" else {"state": "off"})
    assert experts.reviews("local", owner) == []
    assert {experts._job(job_id)["state"] for job_id in (running["job_id"], waiting["job_id"])} == {"stopped"}
    assert scalar(application, "SELECT stop_scope FROM coordinator_conversations WHERE conversation_id=?",
                  running["job_conversation_id"]) == "goals"
    assert scalar(application, "SELECT count(*) FROM channel_deliveries WHERE state IN ('held','pending')") == 0


def test_turning_a_channel_off_closes_everything_with_it_even_if_the_process_stops_right_after(app, monkeypatch):
    # Review 3, shape 2: the channel turned off and everything it closes are one change; turning it on
    # again revives nothing (cells "Message held", "Request pending", "Job held" x "Channel off").
    application, client = app
    expert, channel, owner, cid, job, _ = _always_with_held_work(application, client)
    experts, channels = application.state.experts, application.state.channels
    with monkeypatch.context() as crash:  # the process stops right after the channel was turned off
        crash.setattr(channels.setup, "stop", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            client.patch(f"/v1/channels/{channel['binding_id']}", json={"state": "off"})
    assert experts.reviews("local", owner) == []
    assert experts._job(job["job_id"])["state"] == "stopped"
    assert scalar(application, "SELECT count(*) FROM channel_deliveries WHERE state IN ('held','pending')") == 0
    assert client.patch(f"/v1/channels/{channel['binding_id']}", json={"state": "connected"}).status_code == 200
    experts.reconcile_once()
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []


def test_no_message_is_created_to_leave_through_a_channel_turned_off(app):
    # Review 3, cell "Message waiting to be sent" x "Channel off": dropped, and none created while off.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    channels = application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    assert client.patch(f"/v1/channels/{channel['binding_id']}", json={"state": "off"}).status_code == 200
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "SYNTHETIC-LATE-REPLY")
    channels.reconcile_once()
    assert client.patch(f"/v1/channels/{channel['binding_id']}", json={"state": "connected"}).status_code == 200
    channels.reconcile_once()
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []
    assert scalar(application, "SELECT state FROM channel_deliveries WHERE turn_id=?", accepted["turn_id"]) == "dropped"


@pytest.mark.parametrize("closed", ["off", "removed"])
def test_an_approval_interrupted_before_release_is_never_sent_once_the_person_cannot_be_served(app, closed, monkeypatch):
    # Review 3, B3, cell "Request approved, release interrupted" x "Channel off" / "Person removed": never
    # released, withdrawn; turning the channel on again or listing the person again does not revive it.
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "SYNTHETIC-APPROVED-REPLY")
    channels.reconcile_once()
    owner = scalar(application, "SELECT owner_id FROM expert_reviews")
    review = experts.reviews("local", owner)[0]
    with monkeypatch.context() as crash:  # the approval is recorded; its release never runs
        crash.setattr(experts, "release", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    _close(application, channel, accepted["conversation_id"], closed)
    assert experts.release(review["review_id"]) == "withdrawn"  # the release checks where it acts
    assert experts._review(review["review_id"])["state"] == "withdrawn"
    with experts.store._connect() as conn:  # on again, the person listed again
        conn.execute("UPDATE channel_bindings SET state='connected',people_json=? WHERE binding_id=?",
                     (json.dumps([PERSON]), channel["binding_id"]))
    experts.reconcile_once()
    experts.reconcile_once()
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert not any("SYNTHETIC-APPROVED-REPLY" in item["text"] for item in sent)




def test_a_rejection_is_recorded_whole_or_not_at_all(app, monkeypatch):
    # Review 3, shape 2: the "no", the job it closes, the reply it drops and the turn that tells the person
    # are one change. A failure while recording it leaves the request waiting, nothing half done.
    application, client = app
    expert, channel, owner, cid, job, _ = _always_with_held_work(application, client)
    experts = application.state.experts
    review = next(review for review in experts.reviews("local", owner) if review["kind"] == "job")
    with monkeypatch.context() as failing:
        failing.setattr(experts, "_insert_decision_turn",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")), raising=False)
        with pytest.raises(RuntimeError):
            experts.decide("local", owner, review["review_id"], decision="reject", expected_digest=review["payload_digest"])
    assert experts._review(review["review_id"])["state"] == "pending"
    assert experts._job(job["job_id"])["state"] == "held"
    experts.decide("local", owner, review["review_id"], decision="reject", expected_digest=review["payload_digest"])
    assert experts._job(job["job_id"])["state"] == "withdrawn"
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE origin='owner_decision'") == 1


def test_off_and_the_withdrawal_of_what_waited_are_one_change(app, monkeypatch):
    # Review 4, finding 4: saving Off and withdrawing what waited are one transaction. A failure while withdrawing
    # leaves the setting as it was and every request waiting, nothing half done; the next save does both.
    application, client = app
    expert, channel, owner, cid, job, _ = _always_with_held_work(application, client)
    experts = application.state.experts
    with monkeypatch.context() as crash:
        crash.setattr(experts, "_insert_decision_turn",
                      lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            _save_off(client, expert)
    assert experts.spec(experts.latest("local", owner, expert["lineage_id"])).ask_me_first == "always"
    assert {review["kind"] for review in experts.reviews("local", owner)} == {"job", "reply"}
    _save_off(client, expert)
    assert experts.reviews("local", owner) == []
    assert experts._job(job["job_id"])["state"] == "withdrawn"
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE origin='owner_decision' "
                               "AND message LIKE '%switched Ask me first to Off%'") == 2


def test_a_prepared_job_is_marked_running_at_any_age_never_given_up(app):
    # Review 3, B6, cell "Job prepared" x Crash: marked running by maintenance at any age.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    assert experts._job(job["job_id"])["job_conversation_id"]
    old = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    with experts.store._connect() as conn:  # its conversation and first turn written, not yet marked running
        conn.execute("UPDATE expert_jobs SET state='starting',updated_at=? WHERE job_id=?", (old, job["job_id"]))
    experts.reconcile_once()
    assert experts._job(job["job_id"])["state"] == "running"
    assert scalar(application, "SELECT count(*) FROM coordinator_events WHERE kind='expert_job_result' AND identity=?",
                  job["job_id"]) == 0


def test_a_prepared_job_never_runs_at_an_access_level_no_longer_supported(app, monkeypatch):
    # Review 3, C1: a job prepared under an access level the runtime no longer supports is refused like a
    # held one, told once, and its prepared conversation is fenced so its first turn never starts.
    import workers_projects_runtime.experts as experts_module
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    with experts.store._connect() as conn:
        conn.execute("UPDATE expert_jobs SET state='starting' WHERE job_id=?", (job["job_id"],))
    monkeypatch.setattr(experts_module, "_access_refusal", lambda spec: "Synthetic: this access level is not supported.")
    refused = experts._start_job(job["job_id"])
    assert refused["state"] == "failed" and refused["result_text"].startswith("The job could not start: ")
    assert scalar(application, "SELECT stop_scope FROM coordinator_conversations WHERE conversation_id=?",
                  refused["job_conversation_id"]) != ""




def _hold(application, cid, held=True):
    with application.state.experts.store._connect() as conn:
        conn.execute("UPDATE coordinator_conversations SET restore_hold=? WHERE conversation_id=?", (int(held), cid))


def test_in_a_restored_copy_an_approved_job_waits_and_starts_once_after_the_resume(app):
    # Cell "Job held, request approved, not started" x "Restored copy": waits; started once after the resume.
    application, client = app
    expert, channel, owner, cid, job, _ = _always_with_held_work(application, client)
    experts = application.state.experts
    review = next(review for review in experts.reviews("local", owner) if review["kind"] == "job")
    _hold(application, cid)
    experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    experts.reconcile_once()
    waiting = experts._job(job["job_id"])
    assert waiting["state"] == "held" and not waiting["job_conversation_id"]
    _hold(application, cid, held=False)
    experts.reconcile_once()
    assert experts._job(job["job_id"])["state"] == "running"
    experts.reconcile_once()  # (the stub's provider is unreachable, so its first turn then fails and is told)
    assert scalar(application, "SELECT count(*) FROM expert_conversations WHERE role='job' AND job_id=?", job["job_id"]) == 1


def test_in_a_restored_copy_a_job_not_yet_prepared_waits_and_is_prepared_once_after_the_resume(app):
    # Cell "Job starting, not prepared" x "Restored copy": waits for the resume, then prepared once.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    old = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    with experts.store._connect() as conn:
        conn.execute("INSERT INTO expert_jobs(job_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,idempotency_key,"
                     "goal,context,state,template_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("job_restored", "local", owner, expert["lineage_id"], chat["conversation_id"], "job_restored",
                      "Synthetic job", "", "starting", expert["template_id"], old, old))
    _hold(application, chat["conversation_id"])
    experts.reconcile_once()
    assert experts._job("job_restored")["state"] == "starting" and not experts._job("job_restored")["job_conversation_id"]
    _hold(application, chat["conversation_id"], held=False)
    experts.reconcile_once()
    assert experts._job("job_restored")["state"] == "starting"  # not given up for the time it waited
    a_minute_later = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with experts.store._connect() as conn:  # the next pass after the resume, past its 30 seconds
        conn.execute("UPDATE expert_jobs SET updated_at=? WHERE job_id='job_restored'", (a_minute_later,))
    experts.reconcile_once()
    assert experts._job("job_restored")["state"] == "running"
    experts.reconcile_once()
    assert scalar(application, "SELECT count(*) FROM expert_conversations WHERE role='job' AND job_id='job_restored'") == 1


@pytest.mark.parametrize("decision", ["reject", "off"])
def test_in_a_restored_copy_a_decision_is_told_once_after_the_resume(app, decision):
    # Cells "Request rejected, not yet told" and "Request pending" x Off, in a restored copy: the turn that
    # tells the person is written and waits; nothing is told twice once the owner resumes the copy.
    application, client = app
    expert, channel, owner, cid, job, _ = _always_with_held_work(application, client)
    experts = application.state.experts
    _hold(application, cid)
    if decision == "reject":
        review = next(review for review in experts.reviews("local", owner) if review["kind"] == "job")
        experts.decide("local", owner, review["review_id"], decision="reject", expected_digest=review["payload_digest"])
        expected = 1
    else:
        read = client.get(f"/v1/experts/{expert['template_id']}").json()
        assert client.patch(f"/v1/experts/{expert['template_id']}", json={
            "expected_catalog_revision": read["catalog_revision"],
            "expert": {**read["expert"], "ask_me_first": "off"}}).status_code == 200
        expected = 2
    assert experts._job(job["job_id"])["state"] == "withdrawn"
    told = "SELECT count(*) FROM coordinator_turns WHERE origin='owner_decision' AND conversation_id=?"
    assert scalar(application, told, cid) == expected
    experts.reconcile_once()
    _hold(application, cid, held=False)
    experts.reconcile_once()
    experts.reconcile_once()
    assert scalar(application, told, cid) == expected
    assert scalar(application, "SELECT count(*) FROM expert_reviews WHERE speaker_conversation_id=? AND followup_turn_id=''",
                  cid) == (0 if decision == "off" else 1)  # the reply's request, still waiting for the owner


@pytest.mark.parametrize("closed", ["removed", "off"])
def test_a_no_for_a_person_who_can_no_longer_be_served_is_never_told_nor_revisited(app, closed):
    # Cell "Request rejected, its no not yet told" x "Person removed" / "Channel off": not told, and marked so
    # that maintenance never tries again.
    application, client = app
    expert, channel, owner, cid, job, _ = _always_with_held_work(application, client)
    experts = application.state.experts
    review = next(review for review in experts.reviews("local", owner) if review["kind"] == "job")
    _close(application, channel, cid, closed)
    experts.decide("local", owner, review["review_id"], decision="reject", expected_digest=review["payload_digest"])
    told = "SELECT count(*) FROM coordinator_turns WHERE origin='owner_decision'"
    assert scalar(application, told) == 0
    assert experts._review(review["review_id"])["followup_turn_id"] == ""
    experts.reconcile_once()
    experts.reconcile_once()
    assert scalar(application, told) == 0


@pytest.mark.parametrize("written", ["before", "after"])
def test_a_checking_line_never_leaves_after_its_request_was_decided(app, written):
    # Found on the running build (Always, a fast Approve): the answer went out, then "I'm checking". A checking
    # line leaves only while its request still waits, whether it was written before or after the decision.
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    finish_turn(application, cid, accepted["turn_id"], "SYNTHETIC-HELD-ANSWER")
    channels.reconcile_once()
    owner = scalar(application, "SELECT owner_id FROM expert_reviews")
    review = experts.reviews("local", owner)[0]
    checking = "checking-" + review["review_id"]
    if written == "before":  # the checking line is written and waits to leave; then the owner decides
        finish_turn(application, cid, checking, "SYNTHETIC-CHECKING-LINE")
        channels.reconcile_once()
    experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    if written == "after":  # the owner decided while the checking line was still being written
        finish_turn(application, cid, checking, "SYNTHETIC-CHECKING-LINE")
        channels.reconcile_once()
    sent = [item["text"] for item in client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]]
    assert sent == ["SYNTHETIC-HELD-ANSWER"]


def _approved_not_started(application, client, monkeypatch):
    """An Always job the owner approved whose start was interrupted: approved, still held."""
    expert, channel, owner, cid, job, accepted = _always_with_held_work(application, client)
    experts = application.state.experts
    review = next(review for review in experts.reviews("local", owner) if review["kind"] == "job")
    with monkeypatch.context() as crash:  # the approval is recorded; its release never runs
        crash.setattr(experts, "release", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    assert experts._job(job["job_id"])["state"] == "held"
    return expert, channel, owner, cid, job


def _save_off(client, expert):
    read = client.get(f"/v1/experts/{expert['template_id']}").json()
    assert client.patch(f"/v1/experts/{expert['template_id']}", json={
        "expected_catalog_revision": read["catalog_revision"], "expert": {**read["expert"], "ask_me_first": "off"}}).status_code == 200


@pytest.mark.parametrize("event", ["crash", "stopped", "removed", "off", "setting_off"])
def test_an_approved_job_not_yet_started_follows_the_table(app, event, monkeypatch):
    # Row "Job held, request approved, not started": a crash: started once by maintenance (the one release);
    # Stop, a removal, the channel off or Off: never started, the approval withdrawn (review 4, finding 3: Off
    # withdraws what is not yet carried out, with everything else still waiting, and tells the person).
    application, client = app
    expert, channel, owner, cid, job = _approved_not_started(application, client, monkeypatch)
    experts = application.state.experts
    if event == "setting_off":
        _save_off(client, expert)
    elif event != "crash":
        _close(application, channel, cid, event)
    experts.reconcile_once()
    started = scalar(application, "SELECT count(*) FROM expert_conversations WHERE role='job' AND job_id=?", job["job_id"])
    if event == "crash":
        assert experts._job(job["job_id"])["state"] == "running" and started == 1
    else:
        assert experts._job(job["job_id"])["state"] == "withdrawn" and started == 0
    told = scalar(application, "SELECT count(*) FROM coordinator_turns WHERE origin='owner_decision' "
                               "AND message LIKE '%switched Ask me first to Off%'")
    assert told == (2 if event == "setting_off" else 0)  # the job's request and the held reply's


@pytest.mark.parametrize("closed", ["stopped", "removed", "off"])
def test_a_job_not_yet_prepared_is_never_prepared_once_closed_for_good(app, closed):
    # Row "Job starting, not prepared" x Stop, "Person removed", "Channel off": stopped, nothing prepared.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with experts.store._connect() as conn:
        conn.execute("INSERT INTO expert_jobs(job_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,idempotency_key,"
                     "goal,context,state,template_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("job_unprepared", "local", owner, expert["lineage_id"], cid, "job_unprepared", "Synthetic job", "",
                      "starting", expert["template_id"], old, old))
    _close(application, channel, cid, closed)
    experts.reconcile_once()
    assert experts._job("job_unprepared")["state"] == "stopped"
    assert scalar(application, "SELECT count(*) FROM expert_conversations WHERE role='job' AND job_id='job_unprepared'") == 0


@pytest.mark.parametrize("event", ["removed", "off", "restored"])
def test_a_prepared_job_follows_the_table(app, event):
    # Row "Job prepared": a removal or the channel off: stopped and its conversation fenced; a restored copy:
    # waits, then is marked running once after the resume.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, cid, goal="Synthetic job", idempotency_key="k1", source_turn_id=accepted["turn_id"])
    prepared = experts._job(job["job_id"])
    with experts.store._connect() as conn:  # its conversation and first turn written, not yet marked running
        conn.execute("UPDATE expert_jobs SET state='starting',updated_at=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), job["job_id"]))
    if event == "restored":
        _hold(application, cid)
        experts.reconcile_once()
        assert experts._job(job["job_id"])["state"] == "starting"
        _hold(application, cid, held=False)
        with experts.store._connect() as conn:
            conn.execute("UPDATE expert_jobs SET updated_at=? WHERE job_id=?",
                         ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), job["job_id"]))
        experts.reconcile_once()
        assert experts._job(job["job_id"])["state"] == "running"
    else:
        _close(application, channel, cid, event)
        experts.reconcile_once()
        assert experts._job(job["job_id"])["state"] == "stopped"
        assert scalar(application, "SELECT stop_scope FROM coordinator_conversations WHERE conversation_id=?",
                      prepared["job_conversation_id"]) != ""


@pytest.mark.parametrize("change", [{"people": []}, {"state": "off"}])
def test_a_running_job_stops_when_its_person_is_removed_or_the_channel_turned_off(app, change):
    # Row "Job running" x "Person removed" / "Channel off": stopped (its conversation fenced).
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, accepted["conversation_id"], goal="Synthetic job", idempotency_key="k1",
                           source_turn_id=accepted["turn_id"])
    assert experts._job(job["job_id"])["state"] == "running"
    assert client.patch(f"/v1/channels/{channel['binding_id']}", json=change).status_code == 200
    stopped = experts._job(job["job_id"])
    assert stopped["state"] == "stopped"
    assert scalar(application, "SELECT stop_scope FROM coordinator_conversations WHERE conversation_id=?",
                  stopped["job_conversation_id"]) != ""


def test_a_job_finished_in_a_restored_copy_is_told_to_nobody_before_the_resume(app):
    # Row "Job running" x "Restored copy": its result is recorded once and waits; nothing reaches the person.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    cid = accepted["conversation_id"]
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, cid, goal="Synthetic job", idempotency_key="k1", source_turn_id=accepted["turn_id"])
    running = experts._job(job["job_id"])
    with experts.store._connect() as conn:  # the job's own answer, finished
        conn.execute("UPDATE coordinator_turns SET blocker='',request_id='' WHERE conversation_id=?", (running["job_conversation_id"],))
    finish_turn(application, running["job_conversation_id"], "job-brief", "SYNTHETIC-JOB-RESULT")
    _hold(application, cid)
    experts.reconcile_once()
    experts.reconcile_once()
    assert scalar(application, "SELECT count(*) FROM coordinator_events WHERE kind='expert_job_result' AND identity=?",
                  job["job_id"]) == 1
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert not any("SYNTHETIC-JOB-RESULT" in item["text"] for item in sent)
    assert scalar(application, "SELECT count(*) FROM channel_deliveries WHERE conversation_id=? AND turn_id<>?",
                  cid, accepted["turn_id"]) == 0


def test_off_withdraws_an_approval_not_yet_carried_out_and_tells_the_person(app, monkeypatch):
    # Review 4, finding 3: an approval whose release was interrupted is not carried out after Off: it is withdrawn
    # with everything else still waiting, the person told, and the held reply never sent.
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "SYNTHETIC-APPROVED-BEFORE-OFF")
    channels.reconcile_once()
    owner = scalar(application, "SELECT owner_id FROM expert_reviews")
    review = experts.reviews("local", owner)[0]
    with monkeypatch.context() as crash:  # the approval is recorded; its release never runs
        crash.setattr(experts, "release", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    _save_off(client, expert)
    experts.reconcile_once()
    experts.reconcile_once()
    sent = [item["text"] for item in client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]]
    assert "SYNTHETIC-APPROVED-BEFORE-OFF" not in sent
    assert experts._review(review["review_id"])["state"] == "withdrawn"
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE origin='owner_decision' "
                               "AND message LIKE '%switched Ask me first to Off%'") == 1


# ---------------------------------------------------------------- review 4: one release, one withdraw


def test_an_approval_withdrawn_by_a_removal_is_never_sent_after_the_person_is_listed_again(app, monkeypatch):
    # Review 4, finding 1: the one release reads the request again in its own transaction, so a caller that
    # paused before releasing cannot send an approval a removal withdrew, after the person is listed again.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    experts.ask_owner("local", owner, accepted["conversation_id"], reason="Unusual", reply="SYNTHETIC-WITHDRAWN-APPROVAL",
                      idempotency_key="ask-1", source_turn_id=accepted["turn_id"])
    review = experts.reviews("local", owner)[0]
    with monkeypatch.context() as paused:  # approved; the release pauses
        paused.setattr(experts, "release", lambda *_: (_ for _ in ()).throw(RuntimeError("paused")))
        with pytest.raises(RuntimeError):
            experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    assert client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": []}).status_code == 200
    assert client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": [PERSON]}).status_code == 200
    assert experts.release(review["review_id"]) == "nothing"  # the paused caller resumes
    experts.reconcile_once()
    sent = [item["text"] for item in client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]]
    assert "SYNTHETIC-WITHDRAWN-APPROVAL" not in sent


def test_a_reply_written_while_always_is_saved_is_held_for_the_owner(app, monkeypatch):
    # Review 4, finding 2: the setting is read inside the transaction that writes the reply, so Always saved
    # while the outbox pass works on that reply is never missed.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "SYNTHETIC-ANSWER")
    original = channels._turn_reviews

    def always_saved_meanwhile(*args):
        result = original(*args)
        read = client.get(f"/v1/experts/{expert['template_id']}").json()
        assert client.patch(f"/v1/experts/{expert['template_id']}", json={
            "expected_catalog_revision": read["catalog_revision"],
            "expert": {**read["expert"], "ask_me_first": "always"}}).status_code == 200
        return result

    monkeypatch.setattr(channels, "_turn_reviews", always_saved_meanwhile)
    channels.reconcile_once()
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    assert [review["kind"] for review in experts.reviews("local", owner)] == ["reply"]


def _prepared_not_running(application, experts, job_id):
    """Its conversation and first turn written, the turn not started, the job not yet marked running."""
    job = experts._job(job_id)
    with experts.store._connect() as conn:
        conn.execute("UPDATE expert_jobs SET state='starting',updated_at=? WHERE job_id=?",
                     ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), job_id))
        conn.execute("UPDATE coordinator_turns SET request_id='',blocker='',retry_after_at='' WHERE conversation_id=?",
                     (job["job_conversation_id"],))
    return job


def _first_turn(application, conversation_id):
    with application.state.experts.store._connect() as conn:
        return dict(conn.execute("SELECT request_id,blocker FROM coordinator_turns WHERE conversation_id=? "
                                 "AND turn_id='job-brief'", (conversation_id,)).fetchone())


def test_a_job_its_channel_stopped_never_starts_its_first_turn_even_if_its_fence_did_not_land(app, monkeypatch):
    # Review 4, finding 5: turning the channel off marks the stopped job's conversation stopped in the same
    # transaction, so the coordinator's own maintenance (first to run after a restart) never starts its turn.
    application, client = app
    expert = create_expert(client, ask_me_first="auto")
    channel = connected_channel(application, client, expert)
    experts = application.state.experts
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, accepted["conversation_id"], goal="Synthetic job", idempotency_key="k1",
                           source_turn_id=accepted["turn_id"])
    prepared = _prepared_not_running(application, experts, job["job_id"])
    with monkeypatch.context() as crash:  # the process ends before the fence
        crash.setattr(experts, "_fence_job", lambda job: None)
        assert client.patch(f"/v1/channels/{channel['binding_id']}", json={"state": "off"}).status_code == 200
    experts.coordinator.reconcile_once()
    turn = _first_turn(application, prepared["job_conversation_id"])  # never started; the Stop completed cancels it
    assert turn["request_id"] == "" and turn["blocker"] in {"", "cancelled"}


def test_a_prepared_job_never_starts_its_first_turn_before_its_access_check(app, monkeypatch):
    # Review 4 (lesser, and A7): the coordinator's maintenance may run before the Expert's; a prepared job's first
    # turn waits until the job is marked running, which happens only after the access check.
    import workers_projects_runtime.experts as experts_module
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    prepared = _prepared_not_running(application, experts, job["job_id"])
    monkeypatch.setattr(experts_module, "_access_refusal", lambda spec: "Synthetic: this access level is not supported.")
    experts.coordinator.reconcile_once()
    assert _first_turn(application, prepared["job_conversation_id"]) == {"request_id": "", "blocker": ""}
    experts.reconcile_once()
    assert experts._job(job["job_id"])["state"] == "failed"


def test_a_job_started_by_another_caller_meanwhile_is_left_running(app, monkeypatch):
    # Review 4, finding 6: an approval and maintenance may both start a released job; the one that finds it
    # running already does nothing (that is not a cancellation), so the job is never fenced.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with experts.store._connect() as conn:
        conn.execute("INSERT INTO expert_jobs(job_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,idempotency_key,"
                     "goal,context,state,template_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("job_raced", "local", owner, expert["lineage_id"], chat["conversation_id"], "job_raced",
                      "Synthetic job", "", "starting", expert["template_id"], old, old))
    original = experts.coordinator.accept_turn

    def the_other_caller_wins(*args, **kwargs):
        result = original(*args, **kwargs)
        with experts.store._connect() as conn:
            conn.execute("UPDATE expert_jobs SET state='running' WHERE job_id='job_raced'")
        return result

    monkeypatch.setattr(experts.coordinator, "accept_turn", the_other_caller_wins)
    experts._start_job("job_raced")
    job = experts._job("job_raced")
    assert job["state"] == "running"
    assert scalar(application, "SELECT stop_scope FROM coordinator_conversations WHERE conversation_id=?",
                  job["job_conversation_id"]) == ""


def test_a_job_whose_first_turn_can_never_be_written_ends_told_after_ten_minutes(app, monkeypatch):
    # Review 4, finding 7: a conversation alone does not exempt a job from the ten minutes; only a written first
    # turn does. One whose first turn can never be written (its Library material gone) ends, told once.
    from workers_projects_runtime.coordinator import CoordinatorConflict
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    with monkeypatch.context() as gone:
        gone.setattr(experts.coordinator, "accept_turn", lambda *args, **kwargs: (_ for _ in ()).throw(
            CoordinatorConflict("Selected Library material is unavailable")))
        with pytest.raises(CoordinatorConflict):
            experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
        job_id = scalar(application, "SELECT job_id FROM expert_jobs")
        assert experts._job(job_id)["job_conversation_id"] and experts._job(job_id)["state"] == "starting"
        with experts.store._connect() as conn:
            conn.execute("UPDATE expert_jobs SET updated_at=? WHERE job_id=?",
                         ((datetime.now(UTC) - timedelta(minutes=11)).isoformat(), job_id))
        experts.reconcile_once()
        experts.reconcile_once()
    job = experts._job(job_id)
    assert job["state"] == "failed" and job["result_text"] == "The job could not start."
    assert scalar(application, "SELECT count(*) FROM coordinator_events WHERE kind='expert_job_result' AND identity=?",
                  job_id) == 1


def test_maintenance_reaches_every_running_job_in_turn_not_only_the_first_ones(app):
    # Review 4, finding 8: a pass takes its rows from where the last one stopped, so rows that stay never hide
    # later ones: with one row per pass, a finished job behind two still running is told within three passes.
    application, client = app
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    jobs = [experts.hand_off("local", owner, chat["conversation_id"], goal=f"Synthetic job {i}", idempotency_key=f"k{i}")
            for i in range(3)]
    finished = sorted(job["job_id"] for job in jobs)[-1]
    with experts.store._connect() as conn:
        for job in jobs:
            cid = experts._job(job["job_id"])["job_conversation_id"]
            if job["job_id"] == finished:
                conn.execute("UPDATE coordinator_turns SET request_id='',blocker='' WHERE conversation_id=?", (cid,))
            else:  # still running, and older, so an unrotated pass would always take them first
                conn.execute("UPDATE coordinator_turns SET request_id='req-'||turn_id,blocker='' WHERE conversation_id=?", (cid,))
                conn.execute("UPDATE expert_jobs SET updated_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["job_id"],))
    finish_turn(application, experts._job(finished)["job_conversation_id"], "job-brief", "SYNTHETIC-DONE")
    for _ in range(3):
        experts.reconcile_once(limit=1)
    assert experts._job(finished)["state"] == "done"


def test_a_restored_copy_returns_an_approval_not_yet_carried_out_to_the_owner(app, monkeypatch):
    # Review 4, finding 9: the copy may have been taken between an approval and its release, and the earlier
    # machine may have sent it afterwards. In the restored copy the approval waits for the owner again; it is
    # never carried out by itself.
    import sqlite3
    from workers_projects_runtime.native_continuity import _reset_restore_authority
    application, client = app
    expert = create_expert(client, ask_me_first="always")
    channel = connected_channel(application, client, expert)
    experts, channels = application.state.experts, application.state.channels
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "SYNTHETIC-APPROVED-IN-THE-COPY")
    channels.reconcile_once()
    owner = scalar(application, "SELECT owner_id FROM expert_reviews")
    review = experts.reviews("local", owner)[0]
    with monkeypatch.context() as crash:  # the copy is taken here: approved, not yet released
        crash.setattr(experts, "release", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError):
            experts.decide("local", owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    with sqlite3.connect(experts.store.db_path) as connection:  # what a restore does to its staged copy
        connection.row_factory = sqlite3.Row
        _reset_restore_authority(connection)
    assert experts._review(review["review_id"])["state"] == "pending"
    experts.reconcile_once()
    experts.reconcile_once()
    sent = [item["text"] for item in client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]]
    assert "SYNTHETIC-APPROVED-IN-THE-COPY" not in sent


# ---------------------------------------------------------------- review 5: a job's end, judged where it is told


def _job_answered(application, client, answer="SYNTHETIC-OLD-ANSWER"):
    """A running job whose newest turn is answered and nothing else can move: judged finished."""
    expert = create_expert(client, ask_me_first="off")
    experts = application.state.experts
    chat = client.post(f"/v1/experts/{expert['template_id']}/chat").json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    job = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1")
    cid = experts._job(job["job_id"])["job_conversation_id"]
    with experts.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET request_id='',blocker='' WHERE conversation_id=?", (cid,))
    finish_turn(application, cid, "job-brief", answer)
    return owner, chat["conversation_id"], job["job_id"], cid


def _results(application, job_id):
    with application.state.experts.store._connect() as conn:
        return [json.loads(row[0])["result"]["output_text"] for row in conn.execute(
            "SELECT payload_json FROM coordinator_events WHERE kind='expert_job_result' AND identity=?", (job_id,))]


def test_a_change_written_after_the_first_look_keeps_the_job_running_and_its_answer_is_told(app, monkeypatch):
    # Review 5 (blocking): maintenance judged the job finished; a change was written before it told it. The end is
    # judged again under the lock that tells it, so the job stays running and its later answer is told, once.
    # The reviewer's reproduction: before this, the job was told with the old answer and the new one never.
    application, client = app
    experts = application.state.experts
    owner, speaker, job_id, cid = _job_answered(application, client)
    original = experts._publish
    calls = {"n": 0}

    def change_in_the_gap(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the first look said finished; a change lands before the job is told
            with monkeypatch.context() as quiet:  # the change's turn is written and waits (not started here)
                quiet.setattr(experts.coordinator, "start_turn", lambda *a, **k: {"state": "queued"})
                experts.job_control("local", owner, speaker, job_id, action="change", message="Also add a summary.",
                                    idempotency_key="c1")
        return original(*args, **kwargs)

    monkeypatch.setattr(experts, "_publish", change_in_the_gap)
    experts.reconcile_once()
    change = scalar(application, "SELECT turn_id FROM coordinator_turns WHERE conversation_id=? AND turn_id LIKE 'change-%'", cid)
    finish_turn(application, cid, change, "SYNTHETIC-NEW-ANSWER")
    for _ in range(3):
        experts.reconcile_once()
    assert experts._job(job_id)["state"] == "done"
    assert _results(application, job_id) == ["SYNTHETIC-NEW-ANSWER"]


def test_a_change_that_reaches_a_job_already_told_is_refused_in_plain_words_and_never_runs(app, monkeypatch):
    # Review 5: the job was told between the change's first read of it and the writing of its turn. The change is
    # refused in words the Queen can pass on, its turn never runs, and the job stays done, told once.
    application, client = app
    experts = application.state.experts
    owner, speaker, job_id, cid = _job_answered(application, client)
    original = experts.coordinator.accept_turn
    calls = {"n": 0}

    def told_in_the_gap(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            experts.reconcile_once()  # the job is told as done here
        return original(*args, **kwargs)

    monkeypatch.setattr(experts.coordinator, "accept_turn", told_in_the_gap)
    with pytest.raises(ExpertError, match="already finished, so the change was not applied"):
        experts.job_control("local", owner, speaker, job_id, action="change", message="Also add a summary.",
                            idempotency_key="c1")
    with experts.store._connect() as conn:
        change = dict(conn.execute("SELECT request_id,blocker FROM coordinator_turns WHERE conversation_id=? "
                                   "AND turn_id LIKE 'change-%'", (cid,)).fetchone())
    assert change == {"request_id": "", "blocker": "cancelled"}
    experts.reconcile_once()
    assert experts._job(job_id)["state"] == "done"
    assert _results(application, job_id) == ["SYNTHETIC-OLD-ANSWER"]


def test_a_stop_that_arrives_just_after_the_job_was_told_leaves_it_done(app, monkeypatch):
    # Review 5: Stop read the job running; it was told as done before Stop's write. Decided from the state read
    # inside Stop's lock, it stays done, told once.
    application, client = app
    experts = application.state.experts
    owner, speaker, job_id, cid = _job_answered(application, client)
    original = experts._job
    calls = {"n": 0}

    def told_after_the_read(requested):
        row = original(requested)
        calls["n"] += 1
        if calls["n"] == 1:
            experts.reconcile_once()  # told as done right after Stop's first read
        return row

    monkeypatch.setattr(experts, "_job", told_after_the_read)
    experts.stop_job(job_id)
    monkeypatch.setattr(experts, "_job", original)
    assert experts._job(job_id)["state"] == "done"
    assert _results(application, job_id) == ["SYNTHETIC-OLD-ANSWER"]


def test_reading_the_connect_status_never_starts_the_app_creation_and_a_finished_sign_in_creates_it_once(app, monkeypatch):
    # Found in the real Teams test: a status read moved a finished sign-in on and started the app creation, so two
    # reads together (the page and anyone else) could start two. Reading now only reads; the thread watching the
    # sign-in moves it on, once.
    import threading
    application, client = app
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", channel["binding_id"])
    setup = application.state.channels.setup
    created = []
    monkeypatch.setattr(setup, "_create", lambda *args: created.append(args))
    finished = SimpleNamespace(poll=lambda: 0, returncode=0, stdout=None, wait=lambda: 0)  # signed in
    setup._sessions[channel["binding_id"]] = {"step": "sign_in", "url": "https://login.microsoft.com/device",
                                              "code": "SYNTHETIC3", "process": finished}
    readers = [threading.Thread(target=lambda: client.get(f"/v1/channels/{channel['binding_id']}/connect"))
               for _ in range(4)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(5)
    status = client.get(f"/v1/channels/{channel['binding_id']}/connect").json()
    assert status["step"] == "creating" and "code" not in status
    assert created == []  # no read started anything
    setup._finish_sign_in("local", owner, channel["binding_id"], finished)
    setup._finish_sign_in("local", owner, channel["binding_id"], finished)  # a second finish changes nothing
    assert len(created) == 1


def test_connect_says_plainly_when_teams_will_not_let_this_account_add_custom_apps(app, monkeypatch, tmp_path):
    # Found in the real Teams test: the app was created, then Teams showed a disabled "Add" button. Connect now reads
    # Microsoft's own report first (the Teams CLI's status) and, when custom apps are off for this account, creates
    # nothing and names the switches in one plain line; once allowed, Connect creates the app.
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", channel["binding_id"])
    setup = application.state.channels.setup
    calls, report = [], {"tenant": True, "user": False}

    def teams_cli(*args, timeout=300):
        calls.append(args)
        if args[0] == "status":
            return {"loggedIn": True, "tenantId": "tenant-synthetic", "userObjectId": "person-synthetic-a",
                    "username": "Synthetic Owner", "tdp": {"connected": True, "sideloading": dict(report)}}
        return {"botId": "bot-synthetic", "teamsAppId": "app-synthetic", "socketMode": True,
                "installLink": "https://teams.invalid/install"}

    monkeypatch.setattr(setup, "_run", teams_cli)
    monkeypatch.setattr(setup, "ensure_package", lambda binding_id: "started")
    setup._create("local", owner, channel["binding_id"])
    status = client.get(f"/v1/channels/{channel['binding_id']}/connect").json()
    assert status["step"] == "error"
    for words in ("Upload custom apps", "Let users interact with custom apps in preview", "this one person only", "Connect again"):
        assert words in status["error"]
    assert not any(args[:2] == ("app", "create") for args in calls)
    report["user"] = True  # the administrator turned it on
    setup._create("local", owner, channel["binding_id"])
    assert any(args[:2] == ("app", "create") for args in calls)
    assert client.get(f"/v1/channels/{channel['binding_id']}/connect").json()["step"] == "connected"
