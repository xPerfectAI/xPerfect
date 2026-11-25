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
    # In their own Chat the owner is the one asking; a check goes in the reply, not to Needs you.
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
    # nothing, names the switches in one plain line, offers a note for the administrator, and reads the report
    # again by itself every two minutes; once allowed, Connect goes on and creates the app, once.
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    binding_id = channel["binding_id"]
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", binding_id)
    setup = application.state.channels.setup
    calls, report, clock, background = [], {"tenant": True, "user": False}, [1000.0], []

    def teams_cli(*args, timeout=300):
        calls.append(args)
        if args[0] == "status":
            return {"loggedIn": True, "tenantId": "tenant-synthetic", "userObjectId": "person-synthetic-a",
                    "username": "Synthetic Owner", "tdp": {"connected": True, "sideloading": dict(report)}}
        if args[:2] == ("app", "create"):  # while it is made, the page names the account and organisation
            during.append(client.get(f"/v1/channels/{binding_id}/connect").json())
        return {"botId": "bot-synthetic", "teamsAppId": "app-synthetic", "socketMode": True,
                "installLink": "https://teams.invalid/install"}

    during = []
    monkeypatch.setattr(setup, "_run", teams_cli)
    monkeypatch.setattr(setup, "ensure_package", lambda binding_id, **kwargs: "started")
    monkeypatch.setattr(setup, "_in_background", lambda target, *args: background.append((target, args)))
    setup._clock = lambda: clock[0]

    def creations():
        return sum(1 for args in calls if args[:2] == ("app", "create"))

    def run_background():
        while background:
            target, args = background.pop(0)
            target(*args)

    setup._create("local", owner, binding_id)
    status = client.get(f"/v1/channels/{binding_id}/connect").json()
    assert status["step"] == "blocked" and creations() == 0
    for words in ("Upload custom apps", "Let users install and use available apps by default",
                  "Let users interact with custom apps in preview", "this one person only",
                  "the person's own switch is off", "checks again by itself"):
        assert words in status["error"], words
    # The note asks only for what Microsoft reports off, for this account.
    assert "Upload custom apps" in status["note"] and "Synthetic Owner" in status["note"]
    assert "Let users interact" not in status["note"]
    statuses = sum(1 for args in calls if args[0] == "status")
    clock[0] = 1000 + 119
    setup.reconcile_once()
    assert background == []  # not before two minutes
    clock[0] = 1000 + 121
    setup.reconcile_once()
    assert len(background) == 1
    setup.reconcile_once()
    assert len(background) == 1  # one check at a time
    # A press while that check is claimed starts no second creation.
    assert client.post(f"/v1/channels/{binding_id}/connect", json={}).json()["step"] == "blocked"
    assert len(background) == 1
    run_background()  # still not allowed: blocked again, the next check two minutes later
    assert sum(1 for args in calls if args[0] == "status") > statuses and creations() == 0
    assert client.get(f"/v1/channels/{binding_id}/connect").json()["step"] == "blocked"
    report["user"] = True  # the administrator turned it on
    clock[0] += 121
    setup.reconcile_once()
    run_background()  # Microsoft's report is a hint: after a "no", the first clear yes is enough
    assert creations() == 1
    connected = client.get(f"/v1/channels/{binding_id}/connect").json()
    assert connected["step"] == "connected"
    assert during[-1]["step"] == "creating" and during[-1]["account"] == "Synthetic Owner"
    assert during[-1]["tenant_id"] == "tenant-synthetic"
    assert connected["channel"]["app"]["account"] == "Synthetic Owner"  # and on the card afterwards
    setup.reconcile_once()
    run_background()
    assert creations() == 1  # created once


def test_a_channel_is_connecting_until_microsofts_socket_holds_and_a_lasting_refusal_is_said_once(app):
    # The first real test: a new bot was refused twice before its socket held. The page says "Connecting…" until
    # Microsoft's socket holds, and a refusal the package gave up on stays shown, once, until it holds.
    application, client = app
    channel = connected_channel(application, client, create_expert(client))
    path, service = f"/v1/channels/{channel['binding_id']}/heartbeat", application.state.channels

    def seen():
        view = service.view(service._binding(channel["binding_id"]))
        return view["online"], view["connecting"], view["refusal"]

    assert client.post(path, json={}).status_code == 200  # a package from before this: online, as before
    assert seen() == (True, False, "")
    client.post(path, json={"socket": "connecting"})
    assert seen() == (False, True, "")
    refusal = "Socket Mode negotiate failed: HTTP 401. Check the bot credentials"
    client.post(path, json={"socket": "refused", "error": refusal})
    assert seen() == (False, False, refusal)
    client.post(path, json={"socket": "connecting"})  # the next try leaves the refusal shown
    assert seen() == (False, True, refusal)
    client.post(path, json={"socket": "ready"})
    assert seen() == (True, False, "")
    assert service.view(service._binding(channel["binding_id"]))["first_message"] is False  # online is not ready
    client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-first"))
    assert service.view(service._binding(channel["binding_id"]))["first_message"] is True
    assert client.post(path, json={"socket": "unknown"}).status_code == 422
    assert client.post(path, json={"socket": "ready", "extra": 1}).status_code == 422


def test_maintenance_restarts_back_off_while_the_package_never_holds_and_the_owner_starts_it_at_once(
        app, monkeypatch, tmp_path):
    # Before: maintenance restarted a package that exited at once every 5 s, for ever. Now it waits 5 s after the
    # first exit before the socket held, doubling to 5 minutes; a package that held, or the owner's own action,
    # starts again at once. Fake processes only: nothing is spawned and no real process is signalled.
    from pathlib import Path
    from types import SimpleNamespace as Namespace
    from workers_projects_runtime import channels_setup

    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    channel = connected_channel(application, client, create_expert(client))
    binding_id, service = channel["binding_id"], application.state.channels
    setup = service.setup
    setup.credentials_path(binding_id).write_text("CLIENT_ID=synthetic\n")
    processes, clock = [], [1000.0]

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.pid, self.code = 4_000_000 + len(processes), None
            processes.append(self)

        def poll(self):
            return self.code

    monkeypatch.setattr(channels_setup, "subprocess", Namespace(Popen=FakeProcess, STDOUT=-2))
    monkeypatch.setattr(setup, "_package_python", lambda: Path("synthetic-python"))
    monkeypatch.setattr(setup, "_alive", lambda pid: any(p.pid == pid and p.code is None for p in processes))
    setup._clock = lambda: clock[0]

    def cycle(at):
        clock[0] = at
        setup.reconcile_once()
        return len(processes)

    assert cycle(1000) == 1
    processes[-1].code = 1  # exited before its socket held
    assert cycle(1000) == 1 and cycle(1004) == 1  # waits 5 s
    assert cycle(1006) == 2
    processes[-1].code = 1
    assert cycle(1006) == 2 and cycle(1015) == 2  # then 10 s
    assert cycle(1017) == 3
    service.heartbeat(binding_id, "ready")  # this one held
    processes[-1].code = 1
    assert cycle(1017) == 4  # a package that held starts again at once
    processes[-1].code = 1
    assert cycle(1017) == 4  # one exit before it held: waits again
    assert setup.ensure_package(binding_id) == "started" and len(processes) == 5  # the owner's own action: at once
    processes[-1].code = 1
    assert cycle(1017) == 5
    client.patch(f"/v1/channels/{binding_id}", json={"state": "off"})
    assert cycle(1017) == 5
    client.patch(f"/v1/channels/{binding_id}", json={"state": "connected"})
    assert cycle(1017) == 6  # Turn off, Turn on: starts at once
    waited = []
    for _ in range(9):  # the wait doubles and stops at 5 minutes
        count, start = len(processes), clock[0]
        processes[-1].code = 1
        while cycle(clock[0]) == count:
            clock[0] += 5
        waited.append(clock[0] - start)
    assert waited == [5, 10, 20, 40, 80, 160, 300, 300, 300]


def test_a_check_that_cannot_read_microsofts_report_keeps_checking_and_the_note_asks_only_for_what_is_off(
        app, monkeypatch, tmp_path):
    from workers_projects_runtime.channels_setup import _note_for_admin

    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    binding_id = channel["binding_id"]
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", binding_id)
    setup = application.state.channels.setup
    setup._clock = lambda: 5000.0
    setup._sessions[binding_id] = {"step": "blocked", "error": "blocked", "note": "note", "tenant": "local",
                                   "owner": owner, "check_at": 4000.0, "checking": True}

    def unreachable(*args, timeout=300):
        raise ExpertError("Microsoft Teams setup stopped: synthetic network failure", code="channel_setup_failed")

    monkeypatch.setattr(setup, "_run", unreachable)
    setup._create("local", owner, binding_id)
    session = setup._sessions[binding_id]
    assert session["step"] == "blocked" and not session["checking"] and session["check_at"] == 5000.0 + 120
    both = {"tdp": {"sideloading": {"tenant": False, "user": False}}}
    note = _note_for_admin(both, "Synthetic Expert")
    assert "1. Teams apps → Setup policies" in note and "2. Teams apps → Manage apps → Actions" in note
    assert "Let users install and use available apps by default" in note  # both organisation-wide switches
    assert "My account" not in note  # no account reported, none invented
    assert "up to 30 minutes" in note  # a first visit to the admin center can take that long
    assert "an hour or more" in note  # two clocks: 43 minutes to a steady report, Teams later still
    assert "Upload custom apps" not in _note_for_admin({"tdp": {"sideloading": {"tenant": False, "user": True}}}, "X")


def test_after_a_no_a_missing_report_is_not_a_yes(app, monkeypatch, tmp_path):
    # Advisor finding: after Microsoft said no, three reports without the custom-app fields (a signed-out tool, a
    # portal that did not answer) must not count as three "yes" and create the app while Teams still refuses it.
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    binding_id = channel["binding_id"]
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", binding_id)
    setup = application.state.channels.setup
    calls, report, clock = [], {"value": {"tenant": True, "user": False}}, [1000.0]

    def teams_cli(*args, timeout=300):
        calls.append(args)
        if args[0] == "status":
            side = report["value"]
            return {"loggedIn": True, "username": "Synthetic Owner", "tenantId": "tenant-synthetic",
                    **({"tdp": {"sideloading": dict(side)}} if side is not None else {})}
        return {"botId": "bot-synthetic", "teamsAppId": "app-synthetic", "socketMode": True}

    monkeypatch.setattr(setup, "_run", teams_cli)
    monkeypatch.setattr(setup, "ensure_package", lambda binding_id, **kwargs: "started")
    setup._clock = lambda: clock[0]
    creations = lambda: sum(1 for args in calls if args[:2] == ("app", "create"))  # noqa: E731
    setup._create("local", owner, binding_id)  # the "no"
    note = client.get(f"/v1/channels/{binding_id}/connect").json()["note"]
    for missing in (None, {}, {"tenant": True}, {"user": True}):  # nothing, empty, or only half of the answer
        report["value"] = missing
        for step in (30, 30, 30):
            clock[0] += step
            setup._create("local", owner, binding_id)
        status = client.get(f"/v1/channels/{binding_id}/connect").json()
        assert creations() == 0 and status["step"] == "blocked" and "did not say" in status["error"]
        assert status["note"] == note  # what to ask the administrator stays
    report["value"] = {"tenant": True, "user": True}  # the first clear yes for both switches
    clock[0] += 30
    setup._create("local", owner, binding_id)
    assert creations() == 1


def test_ready_means_a_message_from_the_account_that_connected_it(app):
    # Advisor finding: an unlisted sender's message also leaves an inbox row. "Test in Teams" (a link that opened
    # another person's chat before the app was installed) waits for a message from the account that connected it.
    application, client = app
    channel = connected_channel(application, client, create_expert(client))
    binding_id, service = channel["binding_id"], application.state.channels
    colleague = {"id": "person-synthetic-b", "name": "Synthetic Colleague", "tenant": "tenant-synthetic"}
    client.post(f"/v1/channels/{binding_id}/setup", json={"settings": {
        "app_id": "synthetic-app", "mode": "socket", "connected_by": PERSON["id"]}})
    client.patch(f"/v1/channels/{binding_id}", json={"people": [PERSON, colleague]})
    ready = lambda: service.view(service._binding(binding_id))["first_message"]  # noqa: E731
    stranger = {"id": "person-synthetic-z", "name": "Synthetic Stranger", "tenant": "tenant-synthetic"}
    client.post(f"/v1/channels/{binding_id}/inbound", json=message("act-stranger", person=stranger))
    assert ready() is False  # refused, unlisted
    client.post(f"/v1/channels/{binding_id}/inbound", json=message("act-colleague", person=colleague))
    assert ready() is False  # accepted, but not the account that connected it
    client.post(f"/v1/channels/{binding_id}/inbound", json=message("act-owner"))
    assert ready() is True


def test_two_near_simultaneous_starts_make_one_package(app, monkeypatch, tmp_path):
    # Advisor finding: the scheduler and a Connect both call ensure_package; two package processes would mean two
    # sockets for one bot. Fake processes only: nothing is spawned and no real process is signalled.
    import threading
    import time
    from pathlib import Path
    from types import SimpleNamespace as Namespace
    from workers_projects_runtime import channels_setup

    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    channel = connected_channel(application, client, create_expert(client))
    binding_id, setup = channel["binding_id"], application.state.channels.setup
    setup.credentials_path(binding_id).write_text("CLIENT_ID=synthetic\n")
    processes = []

    class SlowProcess:
        def __init__(self, *args, **kwargs):
            time.sleep(0.3)  # a slow start leaves room for the other caller
            self.pid = 4_100_000 + len(processes)
            processes.append(self)

        def poll(self):
            return None

    monkeypatch.setattr(channels_setup, "subprocess", Namespace(Popen=SlowProcess, STDOUT=-2))
    monkeypatch.setattr(setup, "_package_python", lambda: Path("synthetic-python"))
    monkeypatch.setattr(setup, "_alive", lambda pid: any(p.pid == pid for p in processes))
    outcomes = []
    callers = [threading.Thread(target=lambda: outcomes.append(setup.ensure_package(binding_id))),
               threading.Thread(target=lambda: outcomes.append(setup.ensure_package(binding_id, by_owner=False)))]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=10)
    assert len(processes) == 1 and sorted(outcomes) == ["running", "started"]


def test_the_owner_sets_the_apps_public_details_instead_of_the_placeholders(app, monkeypatch):
    # The first real test's install window showed "Developer" and example.com links (hurdle 10). The owner now sets
    # the four details; they are checked by Microsoft's rules first and applied with the pinned tool's own
    # `app update` on this channel's Teams app. A fake tool only: nothing reaches Microsoft.
    application, client = app
    channel = connected_channel(application, client, create_expert(client))
    binding_id, setup = channel["binding_id"], application.state.channels.setup
    path, calls = f"/v1/channels/{binding_id}/details", []
    monkeypatch.setattr(setup, "_run", lambda *args, timeout=300: calls.append(args) or {})
    good = {"developer": "Synthetic Company", "website": "https://synthetic.example/teams",
            "privacy": "https://synthetic.example/privacy", "terms": "https://synthetic.example/terms"}
    before = client.post(path, json=good)
    assert before.status_code >= 400 and "Connect the channel first" in before.text and calls == []
    client.post(f"/v1/channels/{binding_id}/setup", json={"settings": {
        "app_id": "synthetic-app", "teams_app_id": "teams-app-synthetic", "mode": "socket", "account": "Synthetic Owner"}})
    for change, words in (({"developer": "x" * 33}, "1 to 32 characters"), ({"developer": " "}, "1 to 32 characters"),
                          ({"website": "http://synthetic.example"}, "website link must start with https://"),
                          ({"privacy": "https://localhost"}, "privacy policy link must start with https://"),
                          ({"terms": "synthetic.example/terms"}, "terms of use link must start with https://")):
        refused = client.post(path, json={**good, **change})
        assert refused.status_code >= 400 and words in refused.text, (change, refused.text)
    assert calls == []  # nothing is sent to Microsoft that it would refuse
    saved = client.post(path, json=good)
    assert saved.status_code == 200, saved.text
    assert calls == [("app", "update", "teams-app-synthetic", "--developer", "Synthetic Company",
                      "--website", "https://synthetic.example/teams", "--privacy-url", "https://synthetic.example/privacy",
                      "--terms-url", "https://synthetic.example/terms")]
    shown = saved.json()["app"]
    assert shown["developer"] == "Synthetic Company" and shown["terms_url"] == "https://synthetic.example/terms"
    assert shown["app_id"] == "synthetic-app" and shown["account"] == "Synthetic Owner"  # the rest is kept


def test_an_already_signed_in_connect_names_the_account_on_its_one_press(app, monkeypatch, tmp_path):
    # Hurdle 11: a computer can hold another organisation's login. With the tool already signed in, the Connect button
    # names the account and organisation, and its one press is the yes. A press without that name, or for an
    # account that is no longer the one signed in, makes nothing and shows the account now signed in.
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    binding_id, setup = channel["binding_id"], application.state.channels.setup
    path, calls, signed_in, background, clock = f"/v1/channels/{binding_id}/connect", [], {"as": "Synthetic Owner"}, [], [1000.0]

    def teams_cli(*args, timeout=300):
        calls.append(args)
        if args[0] == "status":
            return {"loggedIn": True, "username": signed_in["as"], "tenantId": "tenant-synthetic",
                    "userObjectId": "person-synthetic-a", "tdp": {"sideloading": {"tenant": True, "user": True}}}
        return {"botId": "bot-synthetic", "teamsAppId": "app-synthetic", "socketMode": True}

    monkeypatch.setattr(setup, "_run", teams_cli)
    monkeypatch.setattr(setup, "_cli_present", lambda: True)
    monkeypatch.setattr(setup, "ensure_package", lambda binding_id, **kwargs: "started")
    monkeypatch.setattr(setup, "_in_background", lambda target, *args: background.append((target, args)))
    setup._clock = lambda: clock[0]
    creations = lambda: sum(1 for args in calls if args[:2] == ("app", "create"))  # noqa: E731
    statuses = lambda: sum(1 for args in calls if args[0] == "status")  # noqa: E731
    named = client.get(path).json()  # what the Connect button says before anything is made
    assert named["step"] == "start" and named["signed_in_as"] == {"account": "Synthetic Owner", "tenant_id": "tenant-synthetic"}
    client.get(path)
    assert statuses() == 1  # read at most every 30 s
    unnamed = client.post(path, json={}).json()
    assert unnamed["step"] == "start" and background == [] and creations() == 0
    signed_in["as"] = "Synthetic Other Account"  # the tool's account changed since the button was drawn
    changed = client.post(path, json={"as_account": "Synthetic Owner"}).json()
    assert changed["step"] == "start" and changed["signed_in_as"]["account"] == "Synthetic Other Account"
    assert background == [] and creations() == 0
    pressed = client.post(path, json={"as_account": "Synthetic Other Account"}).json()  # the one press, named
    assert pressed["step"] == "creating" and len(background) == 1
    target, args = background.pop()
    target(*args)
    assert creations() == 1 and client.get(path).json()["step"] == "connected"


def test_use_another_microsoft_account_signs_the_tool_out_and_shows_microsofts_code(app, monkeypatch):
    from types import SimpleNamespace as Namespace
    from workers_projects_runtime import channels_setup

    application, client = app
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    binding_id, setup = channel["binding_id"], application.state.channels.setup
    signed_out, statuses, threads = [], [], []

    class Login:  # Microsoft's code sign-in, as the tool prints it; nothing is started for real
        def __init__(self, *args, **kwargs):
            lines = ["To sign in, open the page https://microsoft.invalid/devicelogin and enter the code SYNTH12345 now\n"]
            self.stdout = Namespace(readline=lambda: lines.pop(0) if lines else "", read=lambda: "")
            self.returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(setup, "_sign_out", lambda: signed_out.append(True))
    monkeypatch.setattr(setup, "_run", lambda *args, timeout=300: statuses.append(args) or {"loggedIn": True})
    monkeypatch.setattr(setup, "_cli", lambda: ["teams-synthetic"])
    monkeypatch.setattr(channels_setup, "subprocess", Namespace(Popen=Login, PIPE=-1, STDOUT=-2))
    monkeypatch.setattr(channels_setup, "threading", Namespace(Thread=lambda **kwargs: Namespace(start=lambda: threads.append(kwargs)),
                                                               Lock=channels_setup.threading.Lock))
    step = client.post(f"/v1/channels/{binding_id}/connect", json={"other_account": True}).json()
    assert signed_out == [True] and statuses == []  # signed out first; the old account is not asked
    assert step["step"] == "sign_in" and step["code"] == "SYNTH12345" and len(threads) == 1


def test_a_package_adopted_after_a_runtime_restart_is_started_again_at_once_when_stopped(app, monkeypatch, tmp_path):
    # Go-live: after the merge and a runtime restart, the running package is adopted through its pid file (not started
    # by this runtime). Stopping it by its pid while the outbox is idle lets maintenance start the new code at once,
    # with no back-off wait and without Turn off (which would drop messages and stop jobs).
    from pathlib import Path
    from types import SimpleNamespace as Namespace
    from workers_projects_runtime import channels_setup

    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    channel = connected_channel(application, client, create_expert(client))
    binding_id, setup = channel["binding_id"], application.state.channels.setup
    setup.credentials_path(binding_id).write_text("CLIENT_ID=synthetic\n")
    setup._pid_path(binding_id).write_text("4200000")  # the package from before the restart
    adopted, processes = {4_200_000}, []

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.pid = 4_300_000 + len(processes)
            processes.append(self)

        def poll(self):
            return None

    monkeypatch.setattr(channels_setup, "subprocess", Namespace(Popen=FakeProcess, STDOUT=-2))
    monkeypatch.setattr(setup, "_package_python", lambda: Path("synthetic-python"))
    monkeypatch.setattr(setup, "_alive", lambda pid: pid in adopted or any(p.pid == pid for p in processes))
    setup._clock = lambda: 1000.0
    setup.reconcile_once()
    assert processes == []  # the adopted package keeps running
    adopted.clear()  # stopped by its pid
    setup.reconcile_once()
    assert len(processes) == 1  # the new code starts at once


def test_a_blocked_channel_makes_its_app_only_as_the_account_it_was_refused_for(app, monkeypatch, tmp_path):
    # Advisor finding: the tool's login is shared by the whole computer. If the owner was blocked as account A and then
    # signed the tool in as B (for another Expert), the first channel's re-check or Check now would have made its app
    # in B's organisation, unasked. Now nothing is made and Connect names the account signed in now.
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    binding_id = channel["binding_id"]
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", binding_id)
    setup = application.state.channels.setup
    calls, tool, clock, background = [], {"as": "Synthetic Owner A", "user": False}, [1000.0], []

    def teams_cli(*args, timeout=300):
        calls.append(args)
        if args[0] == "status":
            return {"loggedIn": True, "username": tool["as"], "tenantId": "tenant-synthetic",
                    "tdp": {"sideloading": {"tenant": True, "user": tool["user"]}}}
        return {"botId": "bot-synthetic", "teamsAppId": "app-synthetic", "socketMode": True}

    monkeypatch.setattr(setup, "_run", teams_cli)
    monkeypatch.setattr(setup, "ensure_package", lambda binding_id, **kwargs: "started")
    monkeypatch.setattr(setup, "_in_background", lambda target, *args: background.append((target, args)))
    setup._clock = lambda: clock[0]
    creations = lambda: sum(1 for args in calls if args[:2] == ("app", "create"))  # noqa: E731

    def run_background():
        while background:
            target, args = background.pop(0)
            target(*args)

    setup._create("local", owner, binding_id, "Synthetic Owner A")  # the press named A; Microsoft says no
    assert client.get(f"/v1/channels/{binding_id}/connect").json()["step"] == "blocked"
    tool.update({"as": "Synthetic Account B", "user": True})  # another Expert signed the tool in as B, allowed there
    clock[0] += 121
    setup.reconcile_once()
    run_background()
    status = client.get(f"/v1/channels/{binding_id}/connect").json()
    assert creations() == 0 and status["step"] == "error"
    assert "Synthetic Account B" in status["error"] and "Synthetic Owner A" in status["error"]
    setup._sessions[binding_id] = {"step": "blocked", "error": "blocked", "note": "", "account": "Synthetic Owner A",
                                   "tenant": "local", "owner": owner, "check_at": clock[0] + 120}
    pressed = client.post(f"/v1/channels/{binding_id}/connect", json={}).json()  # Check now, from the blocked card
    run_background()
    assert creations() == 0 and client.get(f"/v1/channels/{binding_id}/connect").json()["step"] == "error"
    assert pressed["step"] in ("creating", "error")
    named = client.post(f"/v1/channels/{binding_id}/connect", json={"as_account": "Synthetic Account B"}).json()
    run_background()  # pressed again, naming the account signed in now: made there, once
    assert named["step"] == "creating" and creations() == 1


def test_the_teams_app_is_offered_in_personal_chats_only(app, monkeypatch, tmp_path):
    # Hurdle 14: Microsoft's tool makes the bot for personal chats, teams and group chats; the Expert answers only in
    # personal chats. Right after it is made, the app is narrowed to personal; a failure there is said on the card,
    # and the app is never made a second time.
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    setup = application.state.channels.setup
    monkeypatch.setattr(setup, "ensure_package", lambda binding_id, **kwargs: "started")
    for refuse_update in (False, True):
        expert = create_expert(client, instructions=f"Synthetic instructions {refuse_update}.")
        channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
        binding_id = channel["binding_id"]
        owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", binding_id)
        calls = []

        def teams_cli(*args, timeout=300):
            calls.append(args)
            if args[0] == "status":
                return {"loggedIn": True, "username": "Synthetic Owner", "tenantId": "tenant-synthetic",
                        "userObjectId": "person-synthetic-a", "tdp": {"sideloading": {"tenant": True, "user": True}}}
            if args[:2] == ("app", "update") and refuse_update:
                raise ExpertError("Microsoft Teams setup stopped: synthetic refusal", code="channel_setup_failed")
            return {"botId": "bot-synthetic", "teamsAppId": "teams-app-synthetic", "socketMode": True}

        monkeypatch.setattr(setup, "_run", teams_cli)
        setup._create("local", owner, binding_id)
        assert ("app", "update", "teams-app-synthetic", "--scopes", "personal") in calls
        assert sum(1 for args in calls if args[:2] == ("app", "create")) == 1  # never made twice
        shown = client.get(f"/v1/channels/{binding_id}/connect").json()
        assert shown["step"] == "connected"
        assert shown["channel"]["app"]["scopes"] == ("personal,team,groupChat" if refuse_update else "personal")


def test_an_app_an_administrator_added_is_not_sent_to_the_blocked_install_dialog(app):
    # Hurdle 15: after an administrator published and pre-installed the app from the admin center, Install in Teams
    # still opened the developer copy, blocked by the person's own policy. The owner says so once; the card then
    # stops offering that link, and Undo brings it back. The other app facts stay.
    application, client = app
    channel = connected_channel(application, client, create_expert(client))
    binding_id = channel["binding_id"]
    client.post(f"/v1/channels/{binding_id}/setup", json={"settings": {
        "app_id": "synthetic-app", "teams_app_id": "teams-app-synthetic", "install_link": "https://teams.invalid/install"}})
    added = client.post(f"/v1/channels/{binding_id}/added-by-admin", json={"added": True})
    assert added.status_code == 200, added.text
    assert added.json()["app"]["added_by_admin"] == "yes" and added.json()["app"]["install_link"] == "https://teams.invalid/install"
    undone = client.post(f"/v1/channels/{binding_id}/added-by-admin", json={"added": False}).json()
    assert "added_by_admin" not in undone["app"] and undone["app"]["app_id"] == "synthetic-app"
    assert client.post(f"/v1/channels/{binding_id}/added-by-admin", json={}).status_code == 422


# ---------------------------------------------------------------- the administrator's one press (Teams refuses the app)
TOKEN = "token-synthetic-never-kept"
DEVICE_CODE = "device-code-synthetic-never-kept"
ALL_GRANTED = ("https://graph.microsoft.com/AppCatalog.ReadWrite.All "
               "https://graph.microsoft.com/TeamsAppInstallation.ReadWriteForUser https://graph.microsoft.com/User.Read")


class FakeMicrosoft:
    """Microsoft's sign-in and Graph for one organisation, as the route meets them: a code, the person's consent,
    the catalogue and the install, with the refusals seen or documented."""

    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.pending = 1                      # polls answered "authorization_pending" before the person signs in
        self.answer = {"access_token": TOKEN, "scope": ALL_GRANTED, "expires_in": 3600}
        self.me = {"id": "person-synthetic-a", "userPrincipalName": "owner@synthetic.invalid"}
        self.catalogue: list[dict] = []
        self.publish_status = 201
        self.install_refusals = 0             # installs refused while Microsoft spreads the new entry
        self.installed: list[tuple[str, str]] = []

    def http(self, method, url, *, data=None, headers=None, timeout=60.0):
        path = url.split("synthetic.invalid", 1)[-1] if "synthetic.invalid" in url else url
        self.calls.append((method, path, (headers or {}).get("Authorization", ""), data))
        if path.endswith("/oauth2/v2.0/devicecode"):
            return 200, json.dumps({"device_code": DEVICE_CODE, "user_code": "SYNTH123", "expires_in": 900, "interval": 5,
                                    "verification_uri": "https://login.synthetic.invalid/device"}).encode()
        if path.endswith("/oauth2/v2.0/token"):
            assert DEVICE_CODE.encode() in data
            if self.pending:
                self.pending -= 1
                return 400, b'{"error": "authorization_pending"}'
            return (200 if "access_token" in self.answer else 400), json.dumps(self.answer).encode()
        assert (headers or {}).get("Authorization") == "Bearer " + TOKEN
        if path.startswith("/me"):
            return 200, json.dumps(self.me).encode()
        if method == "GET" and path.startswith("/appCatalogs/teamsApps"):
            return 200, json.dumps({"value": self.catalogue}).encode()
        if method == "POST" and path == "/appCatalogs/teamsApps":
            if self.publish_status != 201:
                return self.publish_status, b'{"error": {"code": "Forbidden", "message": "raw words never shown"}}'
            self.catalogue.append({"id": "catalogue-synthetic", "externalId": "teams-app-synthetic"})
            return 201, b'{"id": "catalogue-synthetic"}'
        if method == "POST" and path.endswith("/teamwork/installedApps"):
            if self.install_refusals:
                self.install_refusals -= 1
                return 403, b'{"error": {"code": "Forbidden"}}'
            self.installed.append((path.split("/")[2], json.loads(data)["teamsApp@odata.bind"].rsplit("/", 1)[-1]))
            return 201, b""
        raise AssertionError(f"unexpected Microsoft call {method} {path}")


def admin_rig(application, client, monkeypatch, tmp_path, *, client_id="client-synthetic"):
    from workers_projects_runtime import teams_admin
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("XPERFECT_MICROSOFT_LOGIN_URL", "http://127.0.0.1:9/login.synthetic.invalid")
    monkeypatch.setenv("XPERFECT_MICROSOFT_GRAPH_URL", "http://127.0.0.1:9/graph.synthetic.invalid")
    if client_id:
        monkeypatch.setenv("XPERFECT_TEAMS_ADMIN_CLIENT_ID", client_id)
    else:
        monkeypatch.delenv("XPERFECT_TEAMS_ADMIN_CLIENT_ID", raising=False)
    expert = create_expert(client)
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    rig = SimpleNamespace(binding_id=channel["binding_id"], calls=[], background=[], clock=[1000.0],
                          report={"tenant": True, "user": False}, owner=scalar(
                              application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", channel["binding_id"]))
    rig.setup = application.state.channels.setup
    rig.microsoft = FakeMicrosoft(rig.clock)

    rig.who = {"tenantId": "tenant-synthetic", "userObjectId": "person-synthetic-a", "username": "Synthetic Owner"}

    def teams_cli(*args, timeout=300):
        rig.calls.append(args)
        if args[0] == "status":
            return {"loggedIn": True, **rig.who, "tdp": {"connected": True, "sideloading": dict(rig.report)}}
        return {"botId": "bot-synthetic", "teamsAppId": "teams-app-synthetic", "socketMode": True,
                "installLink": "https://teams.invalid/developer-copy"}

    def sleep(seconds):
        rig.clock[0] += seconds

    monkeypatch.setattr(rig.setup, "_run", teams_cli)
    monkeypatch.setattr(rig.setup, "ensure_package", lambda binding_id, **kwargs: "started")
    monkeypatch.setattr(rig.setup, "_in_background", lambda target, *args: rig.background.append((target, args)))
    monkeypatch.setattr(rig.setup, "_download_package", lambda manifest_id: b"PK-synthetic-package")
    rig.setup._clock = lambda: rig.clock[0]
    rig.setup._microsoft = lambda tenant_id: teams_admin.Microsoft(tenant_id, http=rig.microsoft.http, sleep=sleep,
                                                                   clock=lambda: rig.clock[0])
    rig.creations = lambda: sum(1 for args in rig.calls if args[:2] == ("app", "create"))
    rig.status = lambda: client.get(f"/v1/channels/{rig.binding_id}/connect").json()
    rig.press = lambda: client.post(f"/v1/channels/{rig.binding_id}/connect", json={"as_admin": True})

    def run():
        while rig.background:
            target, args = rig.background.pop(0)
            target(*args)

    rig.run = run
    rig.setup._create("local", rig.owner, rig.binding_id)  # Teams refuses custom apps for this account: blocked
    return rig


def stored_everywhere(application, folder):
    """Every saved byte this runtime holds: its database and its state folder."""
    texts = []
    with application.state.channels.store._connect() as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            texts.extend(str(tuple(row)) for row in conn.execute(f'SELECT * FROM "{table}"').fetchall())
    for path in folder.rglob("*"):
        if path.is_file():
            texts.append(path.read_bytes().decode("utf-8", "replace"))
    return "\n".join(texts)


def test_an_administrators_one_press_adds_the_app_to_the_catalogue_and_installs_it_keeping_no_token(app, monkeypatch, tmp_path):
    # The route that worked by hand on 24 November, as one press: while Teams refuses custom apps for the person, an
    # administrator signs in on Microsoft's page and consents (the yes); the app is then made once, added to the
    # organisation's catalogue and installed for the person; the card holds the catalogue's link. The token and the
    # sign-in's device code are kept nowhere.
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    blocked = rig.status()
    assert blocked["step"] == "blocked" and blocked["admin"] is True and rig.creations() == 0
    assert blocked["channel"]["admin_route"] is True
    started = rig.press()
    assert started.status_code == 200, started.text
    shown = started.json()
    assert shown["step"] == "admin_sign_in" and shown["code"] == "SYNTH123"
    assert shown["url"] == "https://login.synthetic.invalid/device" and DEVICE_CODE not in started.text
    assert rig.press().json()["step"] == "admin_sign_in" and len(rig.background) == 1  # a second press: the same code
    rig.run()
    done = rig.status()
    assert done["step"] == "connected", done
    card = done["channel"]["app"]
    assert card["catalogue"] == "installed" and card["catalogue_id"] == "catalogue-synthetic"
    assert card["install_link"] == "https://teams.microsoft.com/l/app/catalogue-synthetic?tenantId=tenant-synthetic"
    assert done["channel"]["people"][0]["id"] == "person-synthetic-a" and done["channel"]["state"] == "connected"
    assert rig.creations() == 1 and ("app", "update", "teams-app-synthetic", "--scopes", "personal") in rig.calls
    published = [call for call in rig.microsoft.calls if call[:2] == ("POST", "/appCatalogs/teamsApps")]
    assert len(published) == 1 and published[0][3] == b"PK-synthetic-package"
    assert rig.microsoft.installed == [("person-synthetic-a", "catalogue-synthetic")]
    devicecode = next(call for call in rig.microsoft.calls if call[1].endswith("/devicecode"))
    for permission in ("AppCatalog.ReadWrite.All", "TeamsAppInstallation.ReadWriteForUser", "User.Read"):
        assert permission in devicecode[3].decode()
    saved = stored_everywhere(application, tmp_path)
    assert TOKEN not in saved and DEVICE_CODE not in saved
    assert TOKEN not in json.dumps(done) and DEVICE_CODE not in json.dumps(done)


def test_without_the_catalogue_grant_the_press_makes_nothing_and_says_so(app, monkeypatch, tmp_path):
    # Microsoft's consent decides: a sign-in that did not grant adding apps to the catalogue (not an administrator)
    # makes nothing, checked before the app is made, and the card says so plainly with the note still there.
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    rig.microsoft.answer = {"access_token": TOKEN, "scope": "https://graph.microsoft.com/User.Read", "expires_in": 3600}
    rig.press()
    rig.run()
    shown = rig.status()
    assert shown["step"] == "blocked" and "needs a Teams administrator" in shown["error"] and shown["note"]
    assert rig.creations() == 0 and not [call for call in rig.microsoft.calls if "/appCatalogs" in call[1]]
    assert TOKEN not in stored_everywhere(application, tmp_path)


@pytest.mark.parametrize("answer, words", [
    ({"error": "authorization_declined"}, "declined"),
    ({"error": "expired_token"}, "code expired"),
    ({"error": "invalid_grant", "error_codes": [65001]}, "needs a Teams administrator"),
])
def test_a_sign_in_that_does_not_finish_makes_nothing_and_says_why(app, monkeypatch, tmp_path, answer, words):
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    rig.microsoft.answer = answer
    rig.press()
    rig.run()
    shown = rig.status()
    assert shown["step"] == "blocked" and words in shown["error"] and rig.creations() == 0
    assert "raw words" not in json.dumps(shown)


def test_refused_at_publishing_the_app_waits_and_a_second_press_resumes_without_a_second_app(app, monkeypatch, tmp_path):
    # An organisation that already consented for everyone lets a person who is not an administrator through the
    # grant; Microsoft then refuses at publishing, after the app is made. It waits in a state of its own, the card
    # says so in plain words (never Microsoft's raw reply), and a later press by an administrator resumes at the
    # catalogue: it never makes a second app.
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    rig.microsoft.publish_status = 403
    rig.press()
    rig.run()
    shown = rig.status()
    assert shown["step"] == "admin_waiting" and "needs a Teams administrator" in shown["error"] and shown["admin"]
    assert "raw words never shown" not in json.dumps(shown)
    assert shown["channel"]["app"]["catalogue"] == "pending" and rig.creations() == 1
    rig.clock[0] += 3 * 121
    rig.setup.reconcile_once()  # Microsoft's report is not re-read for an app that waits for the catalogue
    assert rig.background == [] and rig.creations() == 1
    rig.setup._sessions.clear()  # a restart forgets the session; the card still says where it stands
    assert rig.status()["channel"]["app"]["catalogue"] == "pending"
    rig.microsoft.publish_status, rig.microsoft.pending = 201, 0
    rig.press()
    rig.run()
    done = rig.status()
    assert done["step"] == "connected" and done["channel"]["app"]["catalogue"] == "installed"
    assert rig.creations() == 1 and rig.microsoft.installed == [("person-synthetic-a", "catalogue-synthetic")]


def test_an_administrator_signed_in_with_another_account_adds_it_and_the_person_adds_it_from_the_link(app, monkeypatch, tmp_path):
    # Hurdle 7: the administrator is often another account. Installing for oneself would install for the
    # administrator, so the press adds the app to the catalogue only and the card holds the catalogue's link.
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    rig.microsoft.me = {"id": "administrator-synthetic", "userPrincipalName": "admin@synthetic.invalid"}
    rig.press()
    rig.run()
    card = rig.status()["channel"]["app"]
    assert card["catalogue"] == "listed" and card["install_link"].startswith("https://teams.microsoft.com/l/app/catalogue-synthetic")
    assert rig.microsoft.installed == [] and not [call for call in rig.microsoft.calls if "installedApps" in call[1]]


@pytest.mark.parametrize("refusals, catalogue", [(2, "installed"), (10_000, "listed")])
def test_an_install_refused_while_microsoft_spreads_the_app_is_tried_again_then_left_to_the_link(
        app, monkeypatch, tmp_path, refusals, catalogue):
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    rig.microsoft.install_refusals = refusals
    rig.press()
    rig.run()
    done = rig.status()
    assert done["step"] == "connected" and done["channel"]["app"]["catalogue"] == catalogue
    tries = sum(1 for call in rig.microsoft.calls if "installedApps" in call[1])
    assert tries == (3 if refusals == 2 else 31)  # every 20 s for at most ten minutes, within the token's life


def test_an_app_already_in_the_catalogue_is_found_not_published_again(app, monkeypatch, tmp_path):
    # An Expert whose app exists already (made before, or added by an administrator in the admin center) and that
    # Teams still refuses: the press finds it in the catalogue and installs it; nothing new is made or published.
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    client.post(f"/v1/channels/{rig.binding_id}/setup", json={"settings": {
        "app_id": "bot-synthetic", "teams_app_id": "teams-app-synthetic", "connected_by": "person-synthetic-a",
        "tenant_id": "tenant-synthetic", "install_link": "https://teams.invalid/developer-copy"}})
    rig.microsoft.catalogue = [{"id": "catalogue-existing", "externalId": "teams-app-synthetic"}]
    rig.press()
    rig.run()
    card = rig.status()["channel"]["app"]
    assert card["catalogue"] == "installed" and card["catalogue_id"] == "catalogue-existing"
    assert rig.creations() == 0 and not [call for call in rig.microsoft.calls if call[:2] == ("POST", "/appCatalogs/teamsApps")]
    assert rig.microsoft.installed == [("person-synthetic-a", "catalogue-existing")]


def test_without_a_chosen_microsoft_sign_in_the_press_is_not_offered(app, monkeypatch, tmp_path):
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path, client_id="")
    shown = rig.status()
    assert shown["step"] == "blocked" and shown["admin"] is False and shown["channel"]["admin_route"] is False
    refused = rig.press()
    assert refused.status_code == 409 and "no Microsoft sign-in for administrators" in refused.text
    assert rig.background == [] and rig.microsoft.calls == []


def test_microsofts_address_can_only_be_replaced_by_one_on_this_computer(monkeypatch):
    from workers_projects_runtime import teams_admin
    monkeypatch.setenv("XPERFECT_MICROSOFT_LOGIN_URL", "https://login.elsewhere.invalid")
    monkeypatch.setenv("XPERFECT_MICROSOFT_GRAPH_URL", "http://127.0.0.1:28790/graph")
    microsoft = teams_admin.Microsoft("tenant-synthetic")
    assert microsoft.login == "https://login.microsoftonline.com/tenant-synthetic"
    assert microsoft.graph == "http://127.0.0.1:28790/graph"
    assert teams_admin.catalogue_link("a b", "t") == "https://teams.microsoft.com/l/app/a%20b?tenantId=t"


def test_a_claim_waits_for_a_reply_and_leaves_as_soon_as_it_is_ready(app, monkeypatch):
    # The wait before a ready reply left was the package's one-second poll (0.1 to 0.8 s on real Teams). A claim that
    # asks to wait now returns as soon as the reply is written; an empty one returns after its wait, never longer
    # than the core's limit; and a claim still leases the message exactly as before. The bounds are wide (a limit of
    # 10 s here), so a busy machine passes and a claim that never wakes still fails.
    import threading
    import time
    from workers_projects_runtime import channels as channels_module
    monkeypatch.setattr(channels_module, "CLAIM_WAIT_MAX_S", 10.0)
    application, client = app
    channel = connected_channel(application, client, create_expert(client))
    binding_id = channel["binding_id"]
    accepted = client.post(f"/v1/channels/{binding_id}/inbound", json=message("act-1")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Synthetic answer")
    channels = application.state.channels
    result = {}

    def waiting():
        started = time.monotonic()
        result["items"] = channels.claim(binding_id, 10, 10.0)
        result["started"], result["returned"] = started, time.monotonic()

    thread = threading.Thread(target=waiting)
    thread.start()
    time.sleep(0.4)
    written = time.monotonic()
    channels.reconcile_once()  # the reply is written ready to send
    thread.join(15)
    assert [item["text"] for item in result["items"]] == ["Synthetic answer"]
    assert result["returned"] - result["started"] >= 0.4  # it waited for the reply
    assert result["returned"] - written < 5.0  # and left when it was ready, long before its 10 s
    assert scalar(application, "SELECT state FROM channel_deliveries") == "sending"
    started = time.monotonic()
    assert client.post(f"/v1/channels/{binding_id}/deliveries/claim?wait=0.3").json()["items"] == []
    assert 0.3 <= time.monotonic() - started < 5.0
    # Only so many claims wait at once (each holds a server thread); the next answers at once, as before.
    import threading as _threading
    monkeypatch.setattr(channels, "_waiters", _threading.BoundedSemaphore(1))
    assert channels._waiters.acquire(blocking=False)  # one claim is already waiting
    started = time.monotonic()
    assert channels.claim(binding_id, 10, 10.0) == [] and time.monotonic() - started < 5.0  # at once, not after 10 s
    channels._waiters.release()
    # While nothing is ready, a waiting claim reads only: no write transaction until a message is ready.
    writes = []
    real_claim = channels._claim_now
    monkeypatch.setattr(channels, "_claim_now", lambda *args: writes.append(1) or real_claim(*args))
    assert channels.claim(binding_id, 10, 1.0) == [] and len(writes) == 1  # the first look only
    monkeypatch.setattr(channels_module, "CLAIM_WAIT_MAX_S", 0.2)
    started = time.monotonic()
    assert channels.claim(binding_id, 10, 60.0) == [] and time.monotonic() - started < 5.0  # never past the limit


@pytest.mark.parametrize("made_before", [False, True])
def test_the_press_is_refused_when_the_tool_is_now_signed_in_as_another_account(app, monkeypatch, tmp_path, made_before):
    # The Teams tool's sign-in is shared by the whole computer (hurdle 7). The press goes on only as the account the
    # channel was refused for, or the one its app was made with: another account now signed in gets a plain line
    # naming both, and nothing reaches Microsoft or is made.
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    if made_before:
        client.post(f"/v1/channels/{rig.binding_id}/setup", json={"settings": {
            "app_id": "bot-synthetic", "teams_app_id": "teams-app-synthetic", "account": "Synthetic Owner",
            "tenant_id": "tenant-synthetic", "connected_by": "person-synthetic-a"}})
        rig.setup._sessions.clear()  # after a restart: only the recorded app says whose it is
    rig.who = {"tenantId": "tenant-other", "userObjectId": "person-other", "username": "Another Owner"}
    shown = rig.press().json()
    assert shown["step"] == "error", shown
    assert "Another Owner" in shown["error"] and "Synthetic Owner" in shown["error"] and "nothing was made" in shown["error"]
    assert rig.background == [] and rig.microsoft.calls == [] and rig.creations() == 0


def test_a_refused_publish_for_an_app_made_the_usual_way_leaves_its_card_as_it_was(app, monkeypatch, tmp_path):
    # When Teams already lets the person add custom apps, Connect makes the app the usual way and the press is a
    # secondary choice. If Microsoft then refuses to add it to the catalogue, the card keeps its Install in Teams and
    # "An administrator added it for me": nothing is marked waiting for the catalogue for an app this press did not make.
    application, client = app
    rig = admin_rig(application, client, monkeypatch, tmp_path)
    settings = {"app_id": "bot-synthetic", "teams_app_id": "teams-app-synthetic", "account": "Synthetic Owner",
                "tenant_id": "tenant-synthetic", "connected_by": "person-synthetic-a",
                "install_link": "https://teams.invalid/developer-copy"}
    client.post(f"/v1/channels/{rig.binding_id}/setup", json={"settings": settings})
    rig.setup._sessions.clear()
    rig.microsoft.publish_status = 403
    rig.press()
    rig.run()
    shown = rig.status()
    assert shown["step"] == "admin_waiting" and "needs a Teams administrator" in shown["error"]
    card = shown["channel"]["app"]
    assert "catalogue" not in card and card["install_link"] == "https://teams.invalid/developer-copy"
    assert rig.creations() == 0


def _png(size=192, colour=(16, 128, 240)):
    """A plain PNG of one colour, made with the standard library only."""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(colour) * size for _ in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _b64(data):
    import base64
    return base64.b64encode(data).decode()


def test_an_experts_picture_is_a_192_png_kept_in_the_installs_own_state(app, tmp_path, monkeypatch):
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path / "state"))
    expert = create_expert(client)
    url = f"/v1/experts/{expert['template_id']}/picture"
    assert client.get(url).json() == {"picture": None}
    for body, words in ((_b64(b"not a picture"), "PNG"), (_b64(_png(64)), "192 by 192"), ("%%%", "PNG")):
        refused = client.put(url, json={"png_base64": body})
        assert refused.status_code == 422 and words in refused.json()["detail"]["message"], words
    saved = client.put(url, json={"png_base64": _b64(_png())})
    assert saved.status_code == 200, saved.text
    assert saved.json()["picture"].startswith("data:image/png;base64,") and saved.json()["teams"] == []
    folder = tmp_path / "state" / "data" / "expert-pictures"
    assert [path.name for path in folder.iterdir()] == [f"{expert['lineage_id']}.png"]
    assert folder.stat().st_mode & 0o777 == 0o700 and (folder / f"{expert['lineage_id']}.png").stat().st_mode & 0o777 == 0o600
    assert client.get(url).json()["picture"] == saved.json()["picture"]
    assert client.put(url, json={"remove": True}).json() == {"picture": None, "teams": []}
    assert client.get(url).json() == {"picture": None}


def test_an_experts_picture_becomes_its_teams_app_icon_once_per_picture(app, tmp_path, monkeypatch):
    application, client = app
    monkeypatch.setenv("XPERFECT_STATE_DIR", str(tmp_path / "state"))
    expert = create_expert(client)
    url = f"/v1/experts/{expert['template_id']}/picture"
    client.put(url, json={"png_base64": _b64(_png())})
    binding_id = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()["binding_id"]
    owner = scalar(application, "SELECT owner_id FROM channel_bindings WHERE binding_id=?", binding_id)
    setup = application.state.channels.setup
    calls, refuse = [], []

    def teams_cli(*args, timeout=300):
        calls.append(args)
        if args[0] == "status":
            return {"loggedIn": True, "tenantId": "tenant-synthetic", "userObjectId": "person-synthetic-a",
                    "username": "Synthetic Owner", "tdp": {"connected": True, "sideloading": {"tenant": True, "user": True}}}
        if "--color-icon" in args:
            with open(args[args.index("--color-icon") + 1], "rb") as handle:
                assert handle.read(8) == b"\x89PNG\r\n\x1a\n"
            if refuse:
                raise ExpertError("Microsoft Teams setup stopped: synthetic refusal", code="channel_setup_failed")
        return {"botId": "bot-synthetic", "teamsAppId": "app-synthetic", "socketMode": True,
                "installLink": "https://teams.invalid/install"}

    monkeypatch.setattr(setup, "_run", teams_cli)
    monkeypatch.setattr(setup, "ensure_package", lambda binding_id, **kwargs: "started")
    monkeypatch.setattr(setup, "_in_background", lambda target, *args: None)
    setup._create("local", owner, binding_id)

    def icons():
        return [args for args in calls if "--color-icon" in args]

    # Made, narrowed to personal chats, then given the Expert's picture as its colour icon.
    assert len(icons()) == 1 and icons()[0][:3] == ("app", "update", "app-synthetic")
    card = client.get(f"/v1/experts/{expert['template_id']}/channels").json()["items"][0]
    assert card["app"]["picture_digest"] and not card["app"].get("picture_error")
    assert not list((tmp_path / "state" / "tmp").glob("picture-*"))  # the tool's copy is gone
    # The same picture again asks Teams nothing; a new one is applied once.
    client.put(url, json={"png_base64": _b64(_png())})
    assert len(icons()) == 1
    changed = client.put(url, json={"png_base64": _b64(_png(colour=(200, 40, 40)))}).json()
    assert len(icons()) == 2 and changed["teams"] == [{"binding_id": binding_id, "applied": True}]
    # A refusal is kept for the card and never retried by itself.
    refuse.append(True)
    refused = client.put(url, json={"png_base64": _b64(_png(colour=(40, 200, 40)))}).json()
    assert refused["teams"][0]["applied"] is False and "synthetic refusal" in refused["teams"][0]["error"]
    card = client.get(f"/v1/experts/{expert['template_id']}/channels").json()["items"][0]
    assert "synthetic refusal" in card["app"]["picture_error"] and not card["app"]["picture_digest"]
    assert len(icons()) == 3
    setup.reconcile_once()
    assert len(icons()) == 3


# ---------------------------------------------------------------- the first words of a hand-off
def handoff_rig(application, client, **expert):
    channel = connected_channel(application, client, create_expert(client, **expert))
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1")).json()
    owner = scalar(application, "SELECT owner_id FROM expert_conversations")
    claim = lambda: client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    return SimpleNamespace(channel=channel, accepted=accepted, cid=accepted["conversation_id"], owner=owner, claim=claim,
                           experts=application.state.experts, channels=application.state.channels)


def test_the_hand_offs_words_leave_when_the_job_starts_and_the_turns_own_text_does_not(app):
    # The slowest reply a person felt (12.2 s on real Teams): two model steps before any word. The hand-off now
    # carries the words for the person; they leave the moment the job runs, and the turn's later text is not sent.
    application, client = app
    rig = handoff_rig(application, client)
    result = rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic job", idempotency_key="k1",
                                  source_turn_id=rig.accepted["turn_id"], tell="On it: a helper is writing the file.")
    assert result["told"] is True and "end your turn" in result["note"]
    assert [(item["kind"], item["text"]) for item in rig.claim()] == [("handoff", "On it: a helper is writing the file.")]
    finish_turn(application, rig.cid, rig.accepted["turn_id"], "SYNTHETIC-LATER-TEXT")
    rig.channels.reconcile_once()
    assert rig.claim() == []
    assert scalar(application, "SELECT state FROM channel_deliveries WHERE kind='reply'") == "dropped"
    # The reason Activity shows for the reply, in plain words.
    assert scalar(application, "SELECT error FROM channel_deliveries WHERE kind='reply'") == (
        "The message that the job started went out instead")
    again = rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic job", idempotency_key="k1",
                                 source_turn_id=rig.accepted["turn_id"], tell="On it again")
    assert again["job_id"] == result["job_id"]  # a retried key: the same job, nothing sent twice
    assert scalar(application, "SELECT count(*) FROM channel_deliveries WHERE kind='handoff'") == 1


def test_on_always_ask_the_words_wait_with_the_job_on_the_owners_card_and_leave_after_the_approval(app):
    application, client = app
    rig = handoff_rig(application, client, ask_me_first="always")
    result = rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic held job", idempotency_key="k1",
                                  source_turn_id=rig.accepted["turn_id"], tell="A helper will start once my owner agrees.")
    assert result["held_for_owner"] is True and "your words" in result["note"]
    [review] = rig.experts.reviews("local", rig.owner)
    assert review["kind"] == "job" and review["payload"]["tell"] == "A helper will start once my owner agrees."
    finish_turn(application, rig.cid, rig.accepted["turn_id"], "SYNTHETIC-HELD-TURN-TEXT")
    rig.channels.reconcile_once()
    assert all(item["kind"] != "handoff" for item in rig.claim())  # nothing leaves before the owner's yes
    rig.experts.decide("local", rig.owner, review["review_id"], decision="approve", expected_digest=review["payload_digest"])
    sent = rig.claim()
    assert ("handoff", "A helper will start once my owner agrees.") in [(item["kind"], item["text"]) for item in sent]
    assert not any("SYNTHETIC-HELD-TURN-TEXT" in item["text"] for item in sent)


def test_a_rejected_job_never_sends_its_words(app):
    application, client = app
    rig = handoff_rig(application, client, ask_me_first="always")
    rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic held job", idempotency_key="k1",
                         source_turn_id=rig.accepted["turn_id"], tell="SYNTHETIC-WORDS")
    [review] = rig.experts.reviews("local", rig.owner)
    rig.experts.decide("local", rig.owner, review["review_id"], decision="reject", expected_digest=review["payload_digest"])
    assert scalar(application, "SELECT count(*) FROM channel_deliveries WHERE kind='handoff'") == 0
    assert not any("SYNTHETIC-WORDS" in item["text"] for item in rig.claim())


def test_when_the_turns_own_text_is_already_out_the_words_are_not_sent(app):
    # The other order: the turn's own reply was written before the job ran. It was the person's answer, so the
    # words stay unsent and the model is told to write its reply as usual. Never both.
    application, client = app
    rig = handoff_rig(application, client)
    finish_turn(application, rig.cid, rig.accepted["turn_id"], "SYNTHETIC-OWN-TEXT")
    rig.channels.reconcile_once()
    result = rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic job", idempotency_key="k1",
                                  source_turn_id=rig.accepted["turn_id"], tell="SYNTHETIC-WORDS")
    assert result["told"] is False and "as usual" in result["note"]
    assert [item["text"] for item in rig.claim()] == ["SYNTHETIC-OWN-TEXT"]


def test_two_hand_offs_in_one_turn_each_send_their_words_and_the_turns_text_is_dropped(app):
    application, client = app
    rig = handoff_rig(application, client)
    for key, words in (("k1", "SYNTHETIC-FIRST"), ("k2", "SYNTHETIC-SECOND")):
        rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic job " + key, idempotency_key=key,
                             source_turn_id=rig.accepted["turn_id"], tell=words)
    finish_turn(application, rig.cid, rig.accepted["turn_id"], "SYNTHETIC-LATER-TEXT")
    rig.channels.reconcile_once()
    assert sorted(item["text"] for item in rig.claim()) == ["SYNTHETIC-FIRST", "SYNTHETIC-SECOND"]


def test_without_words_a_hand_off_behaves_as_before(app):
    application, client = app
    rig = handoff_rig(application, client)
    result = rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic job", idempotency_key="k1",
                                  source_turn_id=rig.accepted["turn_id"])
    assert "told" not in result and "note" not in result
    finish_turn(application, rig.cid, rig.accepted["turn_id"], "SYNTHETIC-OWN-TEXT")
    rig.channels.reconcile_once()
    assert [(item["kind"], item["text"]) for item in rig.claim()] == [("reply", "SYNTHETIC-OWN-TEXT")]


def test_the_owners_own_chat_has_no_outbox_so_words_are_not_used(app):
    application, client = app
    expert = create_expert(client)
    experts = application.state.experts
    owner = scalar(application, "SELECT owner_id FROM workspace_templates LIMIT 1")
    chat = experts.queen("local", owner, expert["template_id"], channel="chat", thread_key="owner", sender={"name": "Owner"})
    result = experts.hand_off("local", owner, chat["conversation_id"], goal="Synthetic job", idempotency_key="k1",
                              tell="SYNTHETIC-WORDS")
    assert "owner's own Chat" in result["note"] and "told" not in result
    assert scalar(application, "SELECT count(*) FROM channel_deliveries") == 0


def test_words_not_yet_sent_never_leave_a_conversation_stopped_meanwhile(app):
    application, client = app
    rig = handoff_rig(application, client)
    rig.experts.hand_off("local", rig.owner, rig.cid, goal="Synthetic job", idempotency_key="k1",
                         source_turn_id=rig.accepted["turn_id"], tell="SYNTHETIC-WORDS")
    rig.experts.coordinator.stop_conversation("local", rig.owner, rig.cid)
    assert rig.claim() == []
    assert scalar(application, "SELECT state FROM channel_deliveries WHERE kind='handoff'") == "dropped"


# --- A reply part of which the AI provider's safety check stopped -------------------------------------------

_STOPPED_STREAM = "\n".join(json.dumps(line) for line in (
    {"type": "system", "subtype": "init"},
    {"type": "stream_event", "event": {"type": "message_delta", "delta": {
        "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber", "explanation": "Synthetic."}}}},
    {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}},
    {"type": "result", "subtype": "success", "result": "Sorry, I can't open those files."},
))
_PLAIN_STREAM = "\n".join(json.dumps(line) for line in (
    {"type": "system", "subtype": "init"},
    # Words about a refusal are not a stop: only the typed field counts.
    {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "A refusal and a safety classifier stopped nothing here."}]}},
    {"type": "result", "subtype": "success", "result": "Answer."},
))


def test_the_providers_safety_stop_is_read_from_the_typed_stream_only():
    from workers_projects_runtime.conversation_provider import native_safety_stop
    assert native_safety_stop("claude-code", _STOPPED_STREAM) == {"category": "cyber"}
    assert native_safety_stop("claude-code", json.dumps(
        {"type": "assistant", "message": {"stop_reason": "refusal", "stop_details": {"category": "cyber"}}})) == {"category": "cyber"}
    assert native_safety_stop("claude-code", _PLAIN_STREAM) is None
    assert native_safety_stop("codex-cli", _STOPPED_STREAM) is None
    assert native_safety_stop("claude-code", "not json\n{}") is None


def _turn_ran(application, monkeypatch, streams, conversation_id, turn_id, stream_key):
    """Give a finished turn its own native run, whose stream the runtime's activity log returns."""
    store = application.state.experts.store
    project = store.create_project("owner", "Synthetic", "Synthetic.", "claude-code")
    worker = store.create_worker(project_id=project["project_id"], owner_id="owner", name="Synthetic speaker",
                                 role="conversation-agent", profile="claude-code", backend="claude-code",
                                 runtime="claude-code", model="stub/claude-code")
    run = store.create_run(worker["worker_id"], project["project_id"], "The turn.", state="completed")
    session = store.upsert_provider_session(
        tenant_id="local", owner_id="owner", conversation_id=conversation_id, agent_id="speaker",
        model_id="synthetic-model", project_id=project["project_id"], worker_id=worker["worker_id"],
        workspace_dir="/synthetic", access_mode="workspace")
    request, _ = store.create_provider_request(
        tenant_id="local", owner_id="owner", session_id=session["session_id"], idempotency_key=f"{conversation_id}:{turn_id}",
        message_id=turn_id, stream_id=turn_id, requested_history_count=1)
    with store._connect() as conn:
        conn.execute("UPDATE provider_requests SET run_id=?,state='completed' WHERE request_id=?",
                     (run["run_id"], request["request_id"]))
        if turn_id:
            conn.execute("UPDATE coordinator_turns SET request_id=? WHERE conversation_id=? AND turn_id=?",
                         (request["request_id"], conversation_id, turn_id))
    streams[run["run_id"]] = stream_key
    runtime = application.state.experts.coordinator.provider.service.runtime
    monkeypatch.setattr(runtime, "provider_activity_log",
                        lambda worker, run_id: ("claude-code", streams.get(run_id, "")), raising=False)
    return run


def _stopped_reply(app, monkeypatch, mode, stream):
    application, client = app
    expert = create_expert(client, ask_me_first=mode)
    channel = connected_channel(application, client, expert)
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-1", text="Send me the codes")).json()
    finish_turn(application, accepted["conversation_id"], accepted["turn_id"], "Sorry, I can't open those files.")
    _turn_ran(application, monkeypatch, {}, accepted["conversation_id"], accepted["turn_id"], stream)
    return application, client, channel


def test_on_auto_a_reply_the_providers_safety_check_stopped_waits_for_the_owner_once(app, monkeypatch):
    application, client, channel = _stopped_reply(app, monkeypatch, "auto", _STOPPED_STREAM)
    channels = application.state.channels
    channels.reconcile_once()
    channels.reconcile_once()
    experts = application.state.experts
    reviews = experts.reviews("local", scalar(application, "SELECT owner_id FROM expert_reviews"))
    assert [(r["kind"], r["payload"]) for r in reviews] == [("reply", {"text": "Sorry, I can't open those files."})]
    assert "safety check" in reviews[0]["reason"] and reviews[0]["asked"] == "Send me the codes"
    assert scalar(application, "SELECT state FROM channel_deliveries WHERE kind='reply'") == "held"
    assert scalar(application, "SELECT count(*) FROM coordinator_turns WHERE origin='owner_checking'") == 1
    assert client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"] == []


def test_on_off_a_stopped_reply_is_sent_as_it_is(app, monkeypatch):
    application, client, channel = _stopped_reply(app, monkeypatch, "off", _STOPPED_STREAM)
    application.state.channels.reconcile_once()
    assert scalar(application, "SELECT count(*) FROM expert_reviews") == 0
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert [d["text"] for d in sent if d["kind"] == "reply"] == ["Sorry, I can't open those files."]


def test_a_reply_without_a_stop_is_sent_on_auto(app, monkeypatch):
    application, client, channel = _stopped_reply(app, monkeypatch, "auto", _PLAIN_STREAM)
    application.state.channels.reconcile_once()
    assert scalar(application, "SELECT count(*) FROM expert_reviews") == 0
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert [d["text"] for d in sent if d["kind"] == "reply"] == ["Sorry, I can't open those files."]


def test_a_stop_in_a_coordinators_or_helpers_run_changes_nothing_for_the_reply(app, monkeypatch):
    """Only the speaking turn's own run counts: a stop in another run (a job's coordinator or helper) leaves the
    person's reply as it is."""
    application, client, channel = _stopped_reply(app, monkeypatch, "auto", _PLAIN_STREAM)
    streams = {}
    cid = scalar(application, "SELECT conversation_id FROM expert_conversations")
    _turn_ran(application, monkeypatch, streams, cid, "", _STOPPED_STREAM)  # another run of this computer, stopped
    speaker_run = scalar(application, "SELECT p.run_id FROM coordinator_turns t JOIN provider_requests p ON "
                                      "p.request_id=t.request_id WHERE t.conversation_id=? AND t.origin='interactive'", cid)
    streams[speaker_run] = _PLAIN_STREAM
    application.state.channels.reconcile_once()
    assert scalar(application, "SELECT count(*) FROM expert_reviews") == 0
    sent = client.post(f"/v1/channels/{channel['binding_id']}/deliveries/claim").json()["items"]
    assert [d["text"] for d in sent if d["kind"] == "reply"] == ["Sorry, I can't open those files."]
# A folder per person: each person an Expert serves gets their own working folder for its jobs on this computer.
PERSON_B = {"id": "person-synthetic-b", "name": "Synthetic Person B", "tenant": "tenant-synthetic"}


def _two_people_conversations(application, client, expert):
    channel = connected_channel(application, client, expert)
    response = client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": [PERSON, PERSON_B], "state": "connected"})
    assert response.status_code == 200, response.text
    first = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-a", PERSON)).json()
    second = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json=message("act-b", PERSON_B)).json()
    return first["conversation_id"], second["conversation_id"]


def test_each_person_an_expert_serves_gets_their_own_job_folder(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    application, client = app
    experts = application.state.experts
    expert = create_expert(client)
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", expert["template_id"])
    template = experts.resolve("local", owner, expert["template_id"])
    first, second = _two_people_conversations(application, client, expert)
    folder_a = experts._person_folder({"speaker_conversation_id": first, "lineage_id": template["lineage_id"]}, template)
    folder_b = experts._person_folder({"speaker_conversation_id": second, "lineage_id": template["lineage_id"]}, template)
    again = experts._person_folder({"speaker_conversation_id": first, "lineage_id": template["lineage_id"]}, template)
    assert folder_a and folder_b and folder_a != folder_b and folder_a == again
    for folder in (folder_a, folder_b):
        path = tmp_path / "work" / "experts" / template["lineage_id"]
        assert str(path) in folder and (oct(__import__("os").stat(folder).st_mode & 0o777) == "0o700")
    # The job's Workers on this computer carry the person's folder; the job's own conversation starts in it.
    config = experts._job_config("local", owner, template, ExpertBinding(lineage_id=template["lineage_id"], role="job",
                                                                         workspace_dir=folder_a))
    assert {route.workspace_root for route in config.routes if route.execution_mode == "host"} == {folder_a}
    assert config.expert.workspace_dir == folder_a


def test_a_route_without_a_folder_keeps_the_dispatch_digest_it_had_before_folders(app):
    route = Route(id="default", profile="claude-code", model="synthetic-model", effort="high", execution_mode="host")
    before = {"id": "default", "profile": "claude-code", "model": "synthetic-model", "effort": "high",
              "execution_mode": "host", "connection_id": "", "resource_class": "standard", "bootstrap_bundle": {}}
    assert route.identity() == before  # goals in flight across an upgrade keep their digest
    with_folder = route.model_copy(update={"workspace_root": "/synthetic/person"})
    assert with_folder.identity()["workspace_root"] == "/synthetic/person"


def test_the_owners_own_chat_and_a_sandboxed_expert(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    application, client = app
    experts = application.state.experts
    expert = create_expert(client)
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", expert["template_id"])
    template = experts.resolve("local", owner, expert["template_id"])
    with experts.store._connect() as conn:
        conn.execute("INSERT INTO expert_conversations (conversation_id,tenant_id,owner_id,lineage_id,role,channel,created_at) "
                     "VALUES ('synthetic-owner-chat','local',?,?,'speaker','chat','2026-11-25T00:00:00Z')",
                     (owner, template["lineage_id"]))
    folder = experts._person_folder({"speaker_conversation_id": "synthetic-owner-chat", "lineage_id": template["lineage_id"]},
                                    template)
    assert folder.endswith("/owner")
    sandboxed = create_expert(client, workspace={"kind": "sandboxed", "access": "workspace"},
                              team={**SPEC["team"], "coordinate": {"profile": "claude-code"}})
    sandboxed_template = experts.resolve("local", owner, sandboxed["template_id"])
    assert experts._person_folder({"speaker_conversation_id": "synthetic-owner-chat",
                                   "lineage_id": sandboxed_template["lineage_id"]}, sandboxed_template) == ""


# ---------------------------------------------------------------- the Experts page: Workers by model, Preferred

def _catalog_models(monkeypatch, provider):
    """Exact models as the catalog describes them: the AI from the id, its own effort levels."""
    def model(model_id, **kwargs):
        profile, _, native = str(model_id).partition(":")
        if profile not in {"claude-code", "codex-cli"} or native in {"", "unknown-model"}:
            raise KeyError(model_id)
        return SimpleNamespace(id=model_id, harness_profile=profile, native_model=native, connection_id="",
                               effort_choices=("low", "medium"), recommended_effort="medium")
    monkeypatch.setattr(provider, "_model", model)


def test_an_expert_saved_before_workers_by_model_keeps_its_routes_and_stored_form(app):
    application, client = app
    experts = application.state.experts
    created = create_expert(client)
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created["template_id"])
    template = experts.resolve("local", owner, created["template_id"])
    assert "preferred" not in template["content"]["expert"]["team"], "no preference: the stored form is as before"
    assert "preferred" not in client.get(f"/v1/experts/{created['template_id']}").json()["expert"]["team"]
    config = experts._job_config("local", owner, template, ExpertBinding(lineage_id=template["lineage_id"], role="job",
                                                                         access="full"))
    # One route per AI, named by the AI, on the AI's own configured model, in the saved order: as before.
    assert [(route.id, route.profile, route.model, route.effort) for route in config.routes] == [
        ("claude-code", "claude-code", "synthetic-model", "high"), ("codex-cli", "codex-cli", "synthetic-model", "high")]
    assert "preferred helper" not in config.developer_instructions


def test_workers_may_be_exact_models_and_the_preferred_one_comes_first_and_is_told(app, monkeypatch):
    application, client = app
    experts = application.state.experts
    _catalog_models(monkeypatch, experts.coordinator.provider)
    team = {**SPEC["team"], "work": ["claude-code", "codex-cli:gpt-6.2-sol"], "preferred": "codex-cli:gpt-6.2-sol"}
    created = create_expert(client, team=team)
    owner = scalar(application, "SELECT owner_id FROM workspace_templates WHERE template_id=?", created["template_id"])
    template = experts.resolve("local", owner, created["template_id"])
    assert template["content"]["expert"]["team"]["preferred"] == "codex-cli:gpt-6.2-sol"
    config = experts._job_config("local", owner, template, ExpertBinding(lineage_id=template["lineage_id"], role="job",
                                                                         access="full"))
    # The exact model is its own Worker route, first because it is preferred; its effort is one that model offers.
    assert [(route.id, route.profile, route.model, route.effort) for route in config.routes] == [
        ("codex-cli:gpt-6.2-sol", "codex-cli", "codex-cli:gpt-6.2-sol", "medium"),
        ("claude-code", "claude-code", "synthetic-model", "high")]
    assert "The owner's preferred helper for this Expert is the route `codex-cli:gpt-6.2-sol`." in config.developer_instructions
    assert config.developer_instructions.endswith(SPEC["instructions"]), "the owner's instructions still close the job's"
    # The template's own worker profile is the first Worker's AI, not a model id.
    assert template["content"]["worker"]["profile"] == "claude-code"


@pytest.mark.parametrize("team, refusal", [
    ({"work": ["claude-code"], "preferred": "codex-cli"}, 422),          # preferred is not one of its Workers
    ({"work": ["grok-build:grok-4.7"]}, 422),                           # Grok's model is chosen in Connections
    ({"work": ["openclaw"]}, 422),                                      # not an AI an Expert can use
    ({"work": ["codex-cli:unknown-model"]}, 422),                       # not in the catalog
])
def test_a_worker_or_preference_the_runtime_cannot_honour_is_refused(app, monkeypatch, team, refusal):
    application, client = app
    _catalog_models(monkeypatch, application.state.experts.coordinator.provider)
    response = client.post("/v1/experts", json={"name": "Synthetic Expert", "description": "Synthetic.",
                                                "expert": {**SPEC, "team": {**SPEC["team"], **team}}})
    assert response.status_code == refusal, response.text


def test_the_page_lists_each_ais_exact_models_from_the_install_with_its_usual_one(app, monkeypatch):
    application, client = app
    from workers_projects_runtime import coordinator_config
    from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS
    monkeypatch.setattr(coordinator_config, "configured_coordinator",
                        lambda service, provider, tenant, owner, profile: SimpleNamespace(model=f"{profile}:usual-synthetic"))
    response = client.get("/v1/expert-ai-options")
    assert response.status_code == 200, response.text
    items = {item["profile"]: item for item in response.json()["items"]}
    if not items:
        pytest.skip("this runtime build offers no AI adapters")
    for profile, item in items.items():
        assert profile in {"claude-code", "codex-cli", "grok-build"}
        assert item["usual"] == f"{profile}:usual-synthetic"
        assert all(model["id"] in GLASSHIVE_MODELS and model["label"] for model in item["models"])
    if "codex-cli" in items:
        ids = {model["id"] for model in items["codex-cli"]["models"]}
        assert "codex-cli:gpt-6.2-sol" in ids and len(ids) > 1, "every catalog model, not one"
        assert "codex-cli:native-default" not in ids, "the usual model is its own choice"
    from workers_projects_runtime.experts import _release
    for item in items.values():
        # Newest first within each AI; a model named without a version comes after the versioned ones.
        releases = [_release(GLASSHIVE_MODELS[model["id"]].native_model) for model in item["models"]]
        assert releases == sorted(releases, reverse=True), item["profile"]
    assert _release("gpt-6.2-sol") > _release("gpt-6.1-sol") > _release("gpt-6-sol") > _release("gpt-5.6-terra") > _release("gpt-5.4")
    assert _release("claude-opus-5-5") > _release("claude-opus-5") > _release("opus") == ()
    if "grok-build" in items:
        assert items["grok-build"]["models"] == [], "Grok runs the model chosen for it in Connections"


def test_a_chosen_model_runs_at_an_effort_it_offers(app, monkeypatch):
    application, _client = app
    experts = application.state.experts
    _catalog_models(monkeypatch, experts.coordinator.provider)
    from workers_projects_runtime import coordinator_config
    monkeypatch.setattr(coordinator_config, "configured_coordinator", lambda *args, **kwargs: CoordinatorConfig(
        model="codex-cli:configured", effort="ultra",
        routes=[Route(id="default", profile="codex-cli", model="codex-cli:configured", effort="ultra", execution_mode="host")]))
    chosen = experts._default_role_config("local", "owner", ExpertRole(profile="codex-cli", model="codex-cli:gpt-6-sol"))
    assert (chosen.model, chosen.effort) == ("codex-cli:gpt-6-sol", "medium")
    usual = experts._default_role_config("local", "owner", ExpertRole(profile="codex-cli"))
    assert (usual.model, usual.effort) == ("codex-cli:configured", "ultra"), "no model chosen: exactly as configured"


@pytest.mark.parametrize("packaged, local_host, need", [
    (False, True, {"host": True, "sandboxed": False}),    # a local install: Workers on this computer need an account
    (True, False, {"host": True, "sandboxed": True}),     # a packaged install: everywhere
    (False, False, {"host": False, "sandboxed": False}),  # neither: the rule does not apply
])
def test_the_page_is_told_where_and_whether_a_worker_needs_a_connected_account(app, monkeypatch, packaged, local_host, need):
    application, client = app
    from workers_projects_runtime import coordinator_config, execution_profile, service as service_module
    monkeypatch.setattr(coordinator_config, "configured_coordinator",
                        lambda service, provider, tenant, owner, profile: SimpleNamespace(model=f"{profile}:usual"))
    monkeypatch.setattr(execution_profile, "packaged_linux", lambda: packaged)
    monkeypatch.setattr(service_module, "standalone_coordinator_host_enabled", lambda: local_host)
    store = application.state.experts.service.control_plane_store
    account = lambda n, provider, status, default: {"account_id": f"acct-synthetic-{n}", "label": f"Synthetic {n}",
                                                    "provider": provider, "status": status, "is_default": default,
                                                    "auth_method": "subscription"}
    accounts = [account(1, "claude", "ready", False),     # Claude: exactly one
                account(2, "xai", "ready", False),        # Grok: two, no default
                account(3, "grok", "ready", False),
                account(4, "openai", "error", True)]      # Codex: none ready
    monkeypatch.setattr(store, "list_provider_accounts", lambda **kwargs: accounts)
    response = client.get("/v1/expert-ai-options")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workers_need_account"] == need
    found = {item["profile"]: item["worker_account"] for item in body["items"]}
    expected = {"claude-code": "one", "codex-cli": "none", "grok-build": "several"}
    assert found == {profile: expected[profile] for profile in found}
    # A default among several is the one the coordinator takes.
    accounts[1]["is_default"] = True
    found = {item["profile"]: item["worker_account"] for item in client.get("/v1/expert-ai-options").json()["items"]}
    assert found.get("grok-build", "one") == "one"
