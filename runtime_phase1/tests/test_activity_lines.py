"""Activity lines: one line per message or run, built from typed records, filtered and paged by the runtime,
for the signed-in owner only. `GET /v1/activity` stays as it is."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.coordinator import CoordinatorConfig, Route, canonical

SPEC = {"instructions": "Synthetic instructions: answer engineering questions.",
        "team": {"talk": {"profile": "claude-code", "effort": "low"}, "coordinate": {"profile": "codex-cli"},
                 "work": ["claude-code", "codex-cli"]},
        "ask_me_first": "auto", "workspace": {"kind": "host", "access": "full"}}
PERSON = {"id": "person-synthetic-a", "name": "Synthetic Person", "tenant": "tenant-synthetic"}
OLD_ACTIVITY_KEYS = {"event_id", "project_id", "worker_id", "tenant_id", "run_id", "event_type", "created_at",
                     "workspace_name", "workspace_kind", "conversation_id"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    application = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    experts = application.state.experts
    provider = experts.coordinator.provider
    model = SimpleNamespace(effort_choices=("low", "high"), harness_profile="claude-code", native_model="synthetic",
                            connection_id="", recommended_effort="high", id="synthetic-model")
    monkeypatch.setattr(provider, "_model", lambda *args, **kwargs: model)

    def unreachable(*args, **kwargs):
        raise ConnectionError("synthetic provider unavailable")

    monkeypatch.setattr(provider, "start", unreachable)
    monkeypatch.setattr(experts, "_role_config", lambda tenant, owner, role: CoordinatorConfig(
        model="synthetic-model", effort="high",
        routes=[Route(id="default", profile=role.profile, model="synthetic-model", effort="high", execution_mode="host")]))
    monkeypatch.setattr(experts.service, "_process_scheduler_cycle", lambda: None)
    with TestClient(application) as client:
        yield application, client


def sql(application, statement, *args):
    with application.state.experts.store._connect() as conn:
        return [dict(row) for row in conn.execute(statement, args).fetchall()]


def create_expert(client, name="Synthetic Expert"):
    response = client.post("/v1/experts", json={"name": name, "description": "Answers questions.", "expert": SPEC})
    assert response.status_code == 201, response.text
    return response.json()


def connected_channel(client, expert):
    channel = client.post(f"/v1/experts/{expert['template_id']}/channels", json={"kind": "teams"}).json()
    client.post(f"/v1/channels/{channel['binding_id']}/setup", json={"settings": {"app_id": "synthetic-app", "mode": "socket"}})
    response = client.patch(f"/v1/channels/{channel['binding_id']}", json={"people": [PERSON], "state": "connected"})
    assert response.status_code == 200, response.text
    return response.json()


def inbound(application, client, channel, activity_id, text, at):
    accepted = client.post(f"/v1/channels/{channel['binding_id']}/inbound", json={
        "activity_id": activity_id, "person": PERSON, "thread_key": f"{PERSON['id']}:chat",
        "thread_ref": {"conversation_id": "synthetic-chat"}, "text": text}).json()
    assert accepted["accepted"], accepted
    settle(application, accepted["conversation_id"], accepted["turn_id"], at=at)
    return accepted


def settle(application, cid, tid, *, at=None, response="Synthetic answer", blocker=""):
    body = canonical({"choices": [{"message": {"role": "assistant", "content": response}}]}) if response else ""
    sql(application, "UPDATE coordinator_turns SET response_json=?, blocker=?, request_id=''"
                     + (", created_at=?" if at else "") + " WHERE conversation_id=? AND turn_id=?",
        *([body, blocker] + ([at] if at else []) + [cid, tid]))


def owner_of(application):
    return sql(application, "SELECT owner_id FROM expert_conversations LIMIT 1")[0]["owner_id"]


def plain_chat(application, owner, text, at, *, tenant="local", cid="conv-plain", tid="turn-plain"):
    sql(application, "INSERT OR IGNORE INTO coordinator_conversations(conversation_id,tenant_id,owner_id,config_json,"
                     "prompt_json,created_at) VALUES(?,?,?,'{}','{}',?)", cid, tenant, owner, at)
    sql(application, "INSERT INTO coordinator_turns(conversation_id,turn_id,message,response_json,created_at) "
                     "VALUES(?,?,?,?,?)", cid, tid, text, canonical({"ok": True}), at)
    return cid, tid


def worker_run(application, owner, name, instruction, at, *, state="completed", kind="named", tenant="local"):
    store = application.state.experts.store
    project = store.create_project(owner, "Synthetic", "Synthetic goal", "codex-cli", tenant_id=tenant)
    worker = store.create_worker(project["project_id"], owner, name, "main", "codex-cli", "codex-cli", "codex-cli",
                                 "synthetic", execution_mode="host", tenant_id=tenant, workspace_kind=kind)
    run = store.create_run(worker["worker_id"], project["project_id"], instruction)
    sql(application, "UPDATE runs SET state=?, first_queued_at=?, queued_at=? WHERE run_id=?", state, at, at, run["run_id"])
    return worker, run


def lines(client, **body):
    response = client.post("/v1/activity/lines", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def kinds(result, keep=("message", "chat", "run")):
    return [(item["kind"], item["preview"]) for item in result["items"] if item["kind"] in keep]


def test_a_channel_message_is_one_line_with_who_where_what_and_its_trail(app):
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(client, expert)
    accepted = inbound(application, client, channel, "act-1", "Please check the SYNTHETIC draft before Friday",
                       "2026-11-20T10:00:00+00:00")
    owner = owner_of(application)
    store = application.state.experts.store
    # The run that answered it, on the conversation's own Worker: today four rows, now one line and its trail.
    project = store.create_project(owner, "Synthetic", "Synthetic project", "claude-code")
    lane = store.create_worker(project["project_id"], owner, "Speaker", "speaker", "claude-code", "claude-code",
                               "claude-code", "synthetic", execution_mode="host")
    sql(application, "UPDATE workers SET trusted_run_lane='conversation' WHERE worker_id=?", lane["worker_id"])
    session = store.upsert_provider_session(tenant_id="local", owner_id=owner, conversation_id=accepted["conversation_id"],
                                            agent_id="coordinator", model_id="claude-code:synthetic",
                                            project_id=project["project_id"], worker_id=lane["worker_id"],
                                            workspace_dir="/synthetic/workspace", access_mode="read_write")
    request, _ = store.create_provider_request(tenant_id="local", owner_id=owner, session_id=session["session_id"],
                                               idempotency_key="synthetic-1", message_id=accepted["turn_id"],
                                               stream_id="s1", requested_history_count=0)
    run = store.create_run(lane["worker_id"], project["project_id"], "Synthetic turn")
    store.update_provider_request(request["request_id"], run_id=run["run_id"])
    for event, at in (("run.started", "2026-11-20T10:00:02+00:00"), ("run.completed", "2026-11-20T10:00:09+00:00")):
        store.add_event(project["project_id"], lane["worker_id"], run["run_id"], event, "synthetic")
        sql(application, "UPDATE events SET created_at=? WHERE run_id=? AND event_type=?", at, run["run_id"], event)
    sql(application, "UPDATE events SET created_at='2026-11-20T10:00:01+00:00' WHERE run_id=? AND event_type='run.queued'",
        run["run_id"])

    result = lines(client)
    assert [item["kind"] for item in result["items"]] == ["message"]
    item = result["items"][0]
    assert item["subject"] == {"type": "expert", "id": expert["lineage_id"], "template_id": expert["template_id"],
                               "name": "Synthetic Expert", "ai": ""}
    assert item["channel"] == {"kind": "teams"}
    assert item["person"] == {"name": "Synthetic Person", "you": False}
    assert item["preview"] == "Please check the SYNTHETIC draft before Friday"  # the person's words, not the frame
    assert item["status"] == "answered"
    assert item["open"] == {"conversation_id": accepted["conversation_id"]}
    assert result["subjects"]["experts"] == [{"id": expert["lineage_id"], "template_id": expert["template_id"],
                                             "name": "Synthetic Expert"}]
    assert result["subjects"]["workers"] == [] and result["subjects"]["chat"] is False

    detail = client.get(f"/v1/activity/lines/{item['line_id']}").json()
    assert detail["line"] == item
    steps = [(step["step"], step.get("event_type", "")) for step in detail["trail"]]
    assert steps[0] == ("received", "")
    assert [event for step, event in steps if step == "event"][-2:] == ["run.started", "run.completed"]
    assert "SYNTHETIC draft" not in json.dumps(detail["trail"])

    # The old read is unchanged: one row per low-level event, the same fields, and no words.
    old = client.get("/v1/activity?limit=50").json()["items"]
    assert old and all(set(row) == OLD_ACTIVITY_KEYS for row in old)
    assert {row["conversation_id"] for row in old if row["run_id"] == run["run_id"]} == {accepted["conversation_id"]}
    assert "SYNTHETIC draft" not in json.dumps(old)


STATES = {
    "answered": ([], "answered"),
    "waiting_reply": ([("review", "pending")], "waiting_for_you"),
    "rejected": ([("review", "rejected")], "stopped"),
    "job_running": ([("job", "running")], "working"),
    "job_done": ([("job", "done")], "done"),
    "job_failed": ([("job", "failed")], "failed"),
    "delivery_failed": ([("delivery", "failed")], "failed"),
    "delivery_dropped": ([("delivery", "dropped")], "stopped"),
    "not_confirmed": ([("delivery", "not_confirmed")], "answered"),
    "cancelled": ([("turn", "cancelled")], "stopped"),
    "low_disk": ([("turn", "host_capacity")], "working"),
    "not_started": ([("turn", "")], "working"),
    "signed_out": ([("turn", "provider_unauthorized")], "failed"),
    "waiting_over_failed": ([("review", "pending"), ("delivery", "failed")], "waiting_for_you"),
}


@pytest.mark.parametrize("case", sorted(STATES))
def test_each_status_word_comes_from_the_typed_state(app, case):
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(client, expert)
    accepted = inbound(application, client, channel, "act-1", "Synthetic question", "2026-11-20T10:00:00+00:00")
    cid, tid, owner = accepted["conversation_id"], accepted["turn_id"], owner_of(application)
    for index, (record, state) in enumerate(STATES[case][0]):
        if record == "turn":
            settle(application, cid, tid, response="", blocker=state)
        elif record == "review":
            sql(application, "INSERT INTO expert_reviews(review_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,"
                             "source_turn_id,kind,payload_json,payload_digest,state,created_at) "
                             "VALUES(?,?,?,?,?,?,'reply','{}',?,?,?)", f"rev-{index}", "local", owner, expert["lineage_id"],
                cid, tid, "0" * 64, state, "2026-11-20T10:01:00+00:00")
        elif record == "job":
            sql(application, "INSERT INTO expert_jobs(job_id,tenant_id,owner_id,lineage_id,speaker_conversation_id,"
                             "source_turn_id,idempotency_key,goal,state,created_at,updated_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?)", f"job-{index}", "local", owner, expert["lineage_id"], cid, tid,
                f"k{index}", "Synthetic job", state, "2026-11-20T10:01:00+00:00", "2026-11-20T10:05:00+00:00")
        else:
            sql(application, "INSERT INTO channel_deliveries(delivery_id,binding_id,conversation_id,turn_id,kind,text,"
                             "state,created_at) VALUES(?,?,?,?,'reply','Synthetic answer',?,?)", f"dl-{index}",
                channel["binding_id"], cid, tid, state, "2026-11-20T10:01:00+00:00")
    item = lines(client)["items"][0]
    assert item["status"] == STATES[case][1]
    assert (item.get("review_id") == "rev-0") == (STATES[case][1] == "waiting_for_you")
    only = lines(client, statuses=[STATES[case][1]])["items"]
    assert [entry["line_id"] for entry in only] == [item["line_id"]]
    others = [word for word in ("waiting_for_you", "working", "answered", "done", "stopped", "failed")
              if word != STATES[case][1]]
    assert lines(client, statuses=others)["items"] == []


def test_filters_are_applied_by_the_runtime_and_paging_holds(app):
    application, client = app
    first, second = create_expert(client, "Synthetic Expert A"), create_expert(client, "Synthetic Expert B")
    channel = connected_channel(client, first)
    a1 = inbound(application, client, channel, "act-1", "Where is the SYNTHETIC contract?", "2026-11-20T09:00:00+00:00")
    a2 = inbound(application, client, channel, "act-2", "Thanks, and the 50% budget?", "2026-11-20T11:00:00+00:00")
    owner = owner_of(application)
    chat = client.post(f"/v1/experts/{second['template_id']}/chat").json()
    sql(application, "INSERT INTO coordinator_turns(conversation_id,turn_id,message,response_json,created_at) "
                     "VALUES(?,?,?,?,?)", chat["conversation_id"], "own-1", "Draft my weekly note",
        canonical({"ok": True}), "2026-11-20T10:00:00+00:00")
    cid, tid = plain_chat(application, owner, "Compare the two offers", "2026-11-20T12:00:00+00:00")
    worker, scheduled = worker_run(application, owner, "Synthetic Research", "Nightly SYNTHETIC digest",
                                   "2026-11-20T13:00:00+00:00")
    sql(application, "INSERT INTO scheduled_runs(schedule_id,project_id,worker_id,tenant_id,owner_id,instruction,run_at,"
                     "state,queued_run_id,created_at,updated_at) VALUES('sched-1',?,?,'local',?,'Nightly digest',?,"
                     "'queued',?,?,?)", scheduled["project_id"], worker["worker_id"], owner, "2026-11-20T13:00:00+00:00",
        scheduled["run_id"], "2026-11-19T00:00:00+00:00", "2026-11-19T00:00:00+00:00")
    other, failed = worker_run(application, owner, "Synthetic Writer", "Rewrite the summary",
                               "2026-11-20T14:00:00+00:00", state="failed")
    # The plain Chat's turn gave the research Worker a goal: that run is in the Chat line's trail, not a line.
    _, goal_run = worker_run(application, owner, "Synthetic Research Copy", "Goal work", "2026-11-20T12:00:05+00:00")
    sql(application, "UPDATE runs SET worker_id=? WHERE run_id=?", worker["worker_id"], goal_run["run_id"])
    sql(application, "INSERT INTO coordinator_goals(conversation_id,goal_id,source_turn_id,text,worker_id,run_id,created_at) "
                     "VALUES(?,?,?,?,?,?,?)", cid, "goal-1", tid, "Synthetic goal", worker["worker_id"],
        goal_run["run_id"], "2026-11-20T12:00:05+00:00")

    everything = kinds(lines(client))
    assert everything == [("run", "Rewrite the summary"), ("run", "Nightly SYNTHETIC digest"),
                          ("chat", "Compare the two offers"), ("message", "Thanks, and the 50% budget?"),
                          ("message", "Draft my weekly note"), ("message", "Where is the SYNTHETIC contract?")]
    by_preview = {item["preview"]: item for item in lines(client)["items"]}
    assert by_preview["Nightly SYNTHETIC digest"]["channel"] == {"kind": "schedule"}
    assert by_preview["Rewrite the summary"]["channel"] == {"kind": "xperfect"}
    assert by_preview["Rewrite the summary"]["subject"]["ai"] == "codex-cli"
    assert by_preview["Draft my weekly note"]["channel"] == {"kind": "chat"}
    assert by_preview["Draft my weekly note"]["person"] == {"name": "", "you": True}
    assert by_preview["Compare the two offers"]["subject"]["type"] == "chat"

    assert kinds(lines(client, experts=[first["lineage_id"]])) == [
        ("message", "Thanks, and the 50% budget?"), ("message", "Where is the SYNTHETIC contract?")]
    assert len(kinds(lines(client, experts=[first["lineage_id"], second["lineage_id"]]))) == 3
    assert kinds(lines(client, workers=[worker["worker_id"]])) == [
        ("run", "Nightly SYNTHETIC digest"), ("chat", "Compare the two offers")]
    assert kinds(lines(client, chat=True)) == [("chat", "Compare the two offers")]
    assert kinds(lines(client, statuses=["failed"])) == [("run", "Rewrite the summary")]
    assert kinds(lines(client, statuses=["done"])) == [("run", "Nightly SYNTHETIC digest")]
    assert kinds(lines(client, since="2026-11-20T10:00:00Z", until="2026-11-20T12:00:00+00:00")) == [
        ("message", "Thanks, and the 50% budget?"), ("message", "Draft my weekly note")]
    assert kinds(lines(client, q="DIGEST")) == [("run", "Nightly SYNTHETIC digest")]
    assert kinds(lines(client, q="the synthetic contract")) == [("message", "Where is the SYNTHETIC contract?")]
    assert kinds(lines(client, q="Synthetic Person")) == [
        ("message", "Thanks, and the 50% budget?"), ("message", "Where is the SYNTHETIC contract?")]
    assert kinds(lines(client, q="expert b")) == [("message", "Draft my weekly note")]
    assert kinds(lines(client, q="50%")) == [("message", "Thanks, and the 50% budget?")]
    assert kinds(lines(client, q="5_%")) == []  # LIKE wildcards are literal
    buttons = lines(client)["subjects"]
    assert [entry["name"] for entry in buttons["experts"]] == ["Synthetic Expert A", "Synthetic Expert B"]
    assert {entry["name"] for entry in buttons["workers"]} >= {"Synthetic Research", "Synthetic Writer"}
    assert buttons["chat"] is True
    trail = client.get(f"/v1/activity/lines/{by_preview['Compare the two offers']['line_id']}").json()["trail"]
    assert [(step["step"], step["state"]) for step in trail if step["step"] == "worker"] == [("worker", "completed")]

    # Paging: a position, not a count; a new line on top shifts nothing; a page knows when it is the last.
    seen, cursor, pages = [], "", 0
    # Lines with a status only: the Workers' own changes carry today's time and would sit on top.
    every = {"statuses": ["waiting_for_you", "working", "answered", "done", "stopped", "failed"]}
    while True:
        page = lines(client, limit=2, cursor=cursor, **every)
        assert ("subjects" in page) == (cursor == "")
        seen += [item["line_id"] for item in page["items"]]
        pages += 1
        if pages == 1:
            plain_chat(application, owner, "A newer message", "2026-11-21T08:00:00+00:00", cid="conv-new", tid="t-new")
        cursor = page["next_cursor"]
        if not cursor:
            break
    full = [item["line_id"] for item in lines(client, limit=100, **every)["items"]]
    assert pages == 3 and len(seen) == 6 and len(full) == 7
    assert seen == [line for line in full if line in seen] and len(seen) == len(set(seen))
    assert full[0] not in seen and set(full[1:]) <= set(seen)
    assert client.post("/v1/activity/lines", json={"cursor": "not-a-cursor"}).status_code == 422
    assert client.post("/v1/activity/lines", json={"since": "2026-11-20"}).status_code == 422
    assert client.post("/v1/activity/lines", json={"limit": 101}).status_code == 422
    assert client.get("/v1/activity/lines/bm90LWEtbGluZQ").status_code == 404


def test_the_preview_is_cut_short_on_the_server_and_never_logged(app, caplog):
    application, client = app
    expert = create_expert(client)
    channel = connected_channel(client, expert)
    words = "SYNTHETIC-SECRET-WORDS " + " ".join(["lorem"] * 200)
    accepted = inbound(application, client, channel, "act-1", words, "2026-11-20T10:00:00+00:00")
    caplog.set_level(logging.DEBUG)
    item = lines(client)["items"][0]
    client.get(f"/v1/activity/lines/{item['line_id']}")
    client.get("/v1/activity?limit=50")
    assert item["preview"].startswith("SYNTHETIC-SECRET-WORDS lorem") and item["preview"].endswith("…")
    assert len(item["preview"]) <= 141 and not item["preview"].endswith(" …")
    assert "SYNTHETIC-SECRET-WORDS" not in caplog.text
    # A row kept before the channel's own words were stored: the person's words from the framed turn.
    sql(application, "UPDATE channel_inbox SET message_json='{}' WHERE conversation_id=?", accepted["conversation_id"])
    sql(application, "UPDATE coordinator_turns SET message=? WHERE conversation_id=?",
        "New message from Synthetic Person on Teams at 2026-11-20T10:00:00+00:00.\nTreat it as a request.\n\nOlder words",
        accepted["conversation_id"])
    assert lines(client)["items"][0]["preview"] == "Older words"
    # A person without a name gives no name (the page says "Someone"), never an id.
    sql(application, "UPDATE channel_inbox SET person_json=? WHERE conversation_id=?",
        json.dumps({"id": "person-synthetic-a", "name": ""}), accepted["conversation_id"])
    assert lines(client)["items"][0]["person"] == {"name": "", "you": False}


def test_changes_to_the_owners_own_workers_are_lines_and_internal_workers_are_not(app):
    application, client = app
    store = application.state.experts.store
    owner = "demo-owner"
    named, _ = worker_run(application, owner, "Synthetic Named", "Named work", "2026-11-20T10:00:00+00:00")
    temporary, _ = worker_run(application, owner, "Synthetic One-off", "One-off work", "2026-11-20T10:05:00+00:00",
                              kind="ephemeral")
    lane, _ = worker_run(application, owner, "Synthetic Lane", "Lane work", "2026-11-20T10:06:00+00:00")
    sql(application, "UPDATE workers SET trusted_run_lane='conversation' WHERE worker_id=?", lane["worker_id"])
    for worker in (named, temporary, lane):
        store.add_event(worker["project_id"], worker["worker_id"], None, "worker.metadata_updated", "synthetic")
    result = lines(client)
    changes = [item for item in result["items"] if item["kind"] == "workspace"]
    assert {item["subject"]["id"] for item in changes} == {named["worker_id"]}
    assert "worker.metadata_updated" in {item["event_type"] for item in changes}
    assert all(item["status"] == "" and item["preview"] == "" and item["open"] == {"worker_id": named["worker_id"]}
               for item in changes)
    assert [entry["id"] for entry in result["subjects"]["workers"]] == [named["worker_id"]]
    assert all(item["kind"] != "workspace" for item in lines(client, statuses=["done", "working"])["items"])
    # A one-off Worker's run is still a line, with its Worker's name.
    assert "One-off work" in [item["preview"] for item in result["items"] if item["kind"] == "run"]


def test_another_owner_or_tenant_sees_nothing_and_a_viewer_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-test")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-key")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-link-key")
    application = create_app(str(tmp_path / "state.db"), runtime_backend="stub")
    client = TestClient(application)
    owner = {"X-WPR-Token": "synthetic-service-key", "X-Viventium-Tenant-Id": "tenant-test",
             "X-Viventium-User-Id": "owner-one", "X-Viventium-User-Role": "member"}
    other = {**owner, "X-Viventium-User-Id": "owner-two"}
    viewer = {**owner, "X-Viventium-User-Role": "viewer"}
    # The same phrase in each kind of line, for three owners: the right one, another owner, another tenant.
    for tenant, user in (("tenant-test", "owner-one"), ("tenant-test", "owner-two"), ("tenant-other", "owner-one")):
        tag = f"{tenant}-{user}"
        plain_chat(application, user, f"SYNTHETIC-PHRASE chat {tag}", "2026-11-20T10:00:00+00:00", tenant=tenant,
                   cid=f"conv-{tag}", tid=f"turn-{tag}")
        sql(application, "INSERT INTO channel_bindings(binding_id,tenant_id,owner_id,lineage_id,kind,state,created_at,"
                         "updated_at) VALUES(?,?,?,?,'teams','connected','2026-11-20','2026-11-20')",
            f"bind-{tag}", tenant, user, f"lineage-{tag}")
        sql(application, "INSERT INTO coordinator_conversations(conversation_id,tenant_id,owner_id,config_json,prompt_json,"
                         "created_at) VALUES(?,?,?,'{}','{}','2026-11-20')", f"speak-{tag}", tenant, user)
        sql(application, "INSERT INTO expert_conversations(conversation_id,tenant_id,owner_id,lineage_id,role,channel,"
                         "thread_key,sender_json,binding_id,created_at) VALUES(?,?,?,?,'speaker','Teams',?,?,?,'2026-11-20')",
            f"speak-{tag}", tenant, user, f"lineage-{tag}", f"thread-{tag}", json.dumps({"name": f"Person {tag}"}),
            f"bind-{tag}")
        sql(application, "INSERT INTO coordinator_turns(conversation_id,turn_id,message,created_at) VALUES(?,?,?,?)",
            f"speak-{tag}", f"ch-{tag}", "framed", "2026-11-20T11:00:00+00:00")
        sql(application, "INSERT INTO channel_inbox(binding_id,activity_id,state,conversation_id,turn_id,person_json,"
                         "created_at,message_json) VALUES(?,?,'accepted',?,?,?,'2026-11-20',?)", f"bind-{tag}", f"act-{tag}",
            f"speak-{tag}", f"ch-{tag}", json.dumps({"name": f"Person {tag}"}),
            json.dumps({"text": f"SYNTHETIC-PHRASE channel {tag}"}))
        worker_run(application, user, f"Worker {tag}", f"SYNTHETIC-PHRASE run {tag}", "2026-11-20T12:00:00+00:00",
                   tenant=tenant)

    def read(headers):
        response = client.post("/v1/activity/lines", json={"q": "SYNTHETIC-PHRASE"}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    mine, theirs = read(owner), read(other)
    assert sorted(item["preview"] for item in mine["items"]) == sorted(
        f"SYNTHETIC-PHRASE {kind} tenant-test-owner-one" for kind in ("chat", "channel", "run"))
    assert sorted(item["preview"] for item in theirs["items"]) == sorted(
        f"SYNTHETIC-PHRASE {kind} tenant-test-owner-two" for kind in ("chat", "channel", "run"))
    assert [entry["name"] for entry in mine["subjects"]["workers"]] == ["Worker tenant-test-owner-one"]
    for item in mine["items"]:
        assert client.get(f"/v1/activity/lines/{item['line_id']}", headers=owner).status_code == 200
        assert client.get(f"/v1/activity/lines/{item['line_id']}", headers=other).status_code == 404
    assert client.post("/v1/activity/lines", json={}, headers=viewer).status_code == 403
    assert client.post("/v1/activity/lines", json={}).status_code == 401
