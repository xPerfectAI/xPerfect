"""Native schedules use current exact-attempt authority and supplied targets."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json

import pytest

from workers_projects_runtime.models import CreateTargetScheduleRequest, UpdateRecurringScheduleRequest
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.peer_collaboration import PeerError
from workers_projects_runtime.schedule_mcp import (
    NativeScheduleAccess, bind_schedule_projection, native_schedule_server,
)
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


GRANT = {"allow_worker_target": True, "destinations": [
    {"destination_id": "destination-a", "request_data": {"request_ref": "request-a"}},
]}


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.delenv("WPR_NATIVE_SELF_SCHEDULE_ENABLED", raising=False)
    store = Store(tmp_path / "native-schedule.sqlite3")
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = store.create_project(owner_id="owner-a", tenant_id="tenant-a", title="Synthetic", goal="Synthetic", default_worker_profile="codex-cli")
    worker = store.create_worker(project_id=project["project_id"], owner_id="owner-a", tenant_id="tenant-a",
        name="worker", role="worker", profile="codex-cli", backend="stub", runtime="stub", model="synthetic", execution_mode="host",
        bootstrap_bundle={"application_context_format": "opaque_v1", "schedule_access": GRANT})
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic scheduling")
    now = datetime.now(timezone.utc)
    with store._connect() as conn:
        conn.execute("UPDATE runs SET state='running',active_attempt_id='attempt-a' WHERE run_id=?", (run["run_id"],))
        conn.execute("INSERT INTO run_attempts(attempt_id,run_id,attempt_number,state,claimed_at,admitted_at,runtime_invoked_at,lease_id) VALUES('attempt-a',?,1,'running',?,?,?,'lease-a')", (run["run_id"], *[now.isoformat()] * 3))
        conn.execute("""INSERT INTO host_run_leases(lease_id,runtime_family,lane,tenant_id,owner_id,worker_id,run_id,executor_id,attempt_id,status,acquired_at,heartbeat_at,expires_at)
            VALUES('lease-a','host','mission','tenant-a','owner-a',?,?,'executor-a','attempt-a','active',?,?,?)""",
            (worker["worker_id"], run["run_id"], now.isoformat(), now.isoformat(), (now + timedelta(hours=1)).isoformat()))
    token = service.peers.mint_native_session(worker["worker_id"], run["run_id"], purpose="schedule")
    backend, calls = {}, []

    def create(**values):
        calls.append(("create", values))
        value = {"definition_id": "schedule-a", **values}
        backend[value["definition_id"]] = value
        return value

    def get(definition_id, *, tenant_id, owner_id):
        value = backend.get(definition_id)
        if value and (value["tenant_id"], value["owner_id"]) == (tenant_id, owner_id):
            return value

    service.create_target_schedule = create
    service.get_recurring_schedule = get
    service.list_recurring_schedules = lambda **values: [v for v in backend.values() if (v["tenant_id"], v["owner_id"]) == (values["tenant_id"], values["owner_id"])]
    service.update_recurring_schedule = lambda definition_id, **values: calls.append(("update", values)) or values
    service.retire_recurring_schedule = lambda definition_id, **values: calls.append(("retire", values)) or values
    service.run_recurring_schedule_now = lambda definition_id, **values: calls.append(("run_now", values)) or values
    service.list_recurring_schedule_occurrences = lambda definition_id, **values: [{"state": "accepted"}]
    yield service, worker, store.get_run(run["run_id"]), token, NativeScheduleAccess(service), backend, calls
    service.shutdown()
    store.close()


def payload(**overrides):
    values = {"target": {"kind": "webhook", **GRANT["destinations"][0]}, "idempotency_key": "create-a-1",
              "recurrence_type": "interval", "interval_seconds": 60}
    values.update(overrides)
    return CreateTargetScheduleRequest.model_validate(values)


def test_native_scoped_schedule_lifecycle_uses_same_owner_service(live):
    service, worker, run, token, access, backend, calls = live
    result = access.create(token, payload())
    choices = access.list(token)["targets"]
    assert choices[0]["target"] == payload().target.model_dump()
    assert result["tenant_id"] == "tenant-a" and result["owner_id"] == "owner-a"
    assert "bootstrap_bundle" not in result and result["target"] == payload().target.model_dump()
    assert access.get(token, "schedule-a") == result
    assert access.list(token)["items"] == [result]
    access.update(token, "schedule-a", UpdateRecurringScheduleRequest(enabled=False))
    access.run_now(token, "schedule-a", "run-now-a")
    assert access.history(token, "schedule-a") == [{"state": "accepted"}]
    access.retire(token, "schedule-a")
    assert [name for name, _ in calls] == ["create", "update", "run_now", "retire"]


@pytest.mark.parametrize("created", [True, False, None, 0, "false"])
@pytest.mark.parametrize("time_field", ["scheduled_for", "run_at"])
def test_native_manual_receipt_has_only_admission_facts_without_service_mutation(live, created, time_field):
    service, _, _, token, access, _, _ = live
    access.create(token, payload())
    original = {
        "definition_id": "untrusted-alias", "occurrence_id": "occurrence-synthetic",
        "status": "failed" if created is False else "scheduled",
        "state": "failed" if created is False else "pending",
        time_field: "2027-01-01T00:00:00Z",
        "owner_action": "dispatch_here", "schedule_owner": "native",
        "snapshot_json": "Synthetic saved executor data.",
        "target_json": "Synthetic target data.", "instruction": "Saved executor instruction.",
        "owner_id": "synthetic-owner", "tenant_id": "synthetic-tenant",
        "delegated_detail": {"code": "synthetic-owner-detail", "available": False},
    }
    if created is not None:
        original["occurrence_created"] = created
    before = json.dumps(original, sort_keys=True)
    service.run_recurring_schedule_now = lambda *_args, **_kwargs: original
    receipt = access.run_now(token, "schedule-a", "request-synthetic")
    expected = {"definition_id": "schedule-a", "occurrence_id": original["occurrence_id"],
        "status": original["status"], "state": original["state"],
        "scheduled_for": original[time_field]}
    if type(created) is bool:
        expected["occurrence_created"] = created
    assert receipt == expected
    assert receipt is not original
    assert "owner_action" not in receipt
    assert ("occurrence_created" in receipt) is (type(created) is bool)
    assert json.dumps(original, sort_keys=True) == before


def test_native_manual_receipt_reports_no_admission_as_typed_not_found(live):
    service, _, _, token, access, _, _ = live
    access.create(token, payload())
    service.run_recurring_schedule_now = lambda *_args, **_kwargs: None
    with pytest.raises(PeerError) as failure:
        access.run_now(token, "schedule-a", "request-synthetic")
    assert failure.value.code == "schedule_not_found" and failure.value.status_code == 404


def test_native_manual_receipt_preserves_unknown_status_without_inventing_creation(live):
    service, _, _, token, access, _, _ = live
    access.create(token, payload())
    original = {"status": "unknown", "state": None, "instruction": "Saved execution data."}
    service.run_recurring_schedule_now = lambda *_args, **_kwargs: original
    assert access.run_now(token, "schedule-a", "request-synthetic") == {
        "definition_id": "schedule-a", "status": "unknown", "state": None,
    }


@pytest.mark.parametrize("changed", [
    {"target": {"kind": "webhook", "destination_id": "foreign", "request_data": {"request_ref": "request-a"}}},
    {"target": {"kind": "webhook", "destination_id": "destination-a", "request_data": {"request_ref": "foreign"}}},
    {"target": {"kind": "webhook", "destination_id": "destination-a", "request_data": {"url": "https://receiver.example.test"}}},
    {"target": {"kind": "worker", "worker_id": "another-worker"}, "instruction": "task"},
    {"bootstrap_bundle": {"schedule_access": {"destinations": []}}},
])
def test_native_cannot_invent_target_body_worker_or_authority(live, changed):
    _, _, _, token, access, _, calls = live
    with pytest.raises(PeerError):
        access.create(token, payload(**changed))
    assert calls == []


def test_worker_self_target_requires_explicit_grant(live):
    service, worker, run, token, access, backend, calls = live
    result = access.create(token, payload(target={"kind": "worker", "worker_id": worker["worker_id"]}, instruction="Exact future task"))
    assert result["instruction"] == "Exact future task"
    service.store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps({"schedule_access": {"destinations": GRANT["destinations"]}}))
    with pytest.raises(PeerError):
        access.get(token, "schedule-a")


@pytest.mark.parametrize("revocation", ["run-ended", "attempt-ended", "retry", "lease-released", "lease-expired", "worker-closed", "grant-removed"])
def test_native_rechecks_revocation_before_mutation(live, revocation):
    service, worker, run, token, access, backend, calls = live
    with service.store._connect() as conn:
        if revocation == "run-ended":
            conn.execute("UPDATE runs SET state='completed' WHERE run_id=?", (run["run_id"],))
        elif revocation == "attempt-ended":
            conn.execute("UPDATE run_attempts SET state='completed', ended_at=? WHERE attempt_id='attempt-a'", (datetime.now(timezone.utc).isoformat(),))
        elif revocation == "retry":
            conn.execute("UPDATE runs SET active_attempt_id='new-attempt' WHERE run_id=?", (run["run_id"],))
        elif revocation == "lease-released":
            conn.execute("UPDATE host_run_leases SET status='released', released_at=? WHERE lease_id='lease-a'", (datetime.now(timezone.utc).isoformat(),))
        elif revocation == "lease-expired":
            conn.execute("UPDATE host_run_leases SET expires_at=? WHERE lease_id='lease-a'", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        elif revocation == "worker-closed":
            conn.execute("UPDATE workers SET state='terminated' WHERE worker_id=?", (worker["worker_id"],))
        elif revocation == "grant-removed":
            conn.execute("UPDATE workers SET bootstrap_bundle_json='{}' WHERE worker_id=?", (worker["worker_id"],))
    with pytest.raises(PeerError):
        access.create(token, payload())
    assert calls == []


def test_other_native_purpose_and_service_token_do_not_authorize_schedule(live):
    service, worker, run, _, access, _, calls = live
    for token in ("synthetic-service-token", service.peers.mint_native_session(worker["worker_id"], run["run_id"], purpose="peer"),
                  service.peers.mint_native_session(worker["worker_id"], run["run_id"], purpose="context")):
        with pytest.raises(PeerError):
            access.create(token, payload())
    assert calls == []


def test_cross_owner_schedule_is_not_visible_or_mutable(live):
    _, _, _, token, access, backend, calls = live
    backend["foreign-schedule"] = {"definition_id": "foreign-schedule", "tenant_id": "tenant-b", "owner_id": "owner-b", "target": payload().target.model_dump()}
    assert access.list(token)["items"] == []
    for action in (lambda: access.get(token, "foreign-schedule"), lambda: access.retire(token, "foreign-schedule"),
                   lambda: access.update(token, "foreign-schedule", UpdateRecurringScheduleRequest(enabled=False)),
                   lambda: access.run_now(token, "foreign-schedule", "run-now-b"), lambda: access.history(token, "foreign-schedule")):
        with pytest.raises(PeerError):
            action()
    assert calls == []


def test_native_projection_uses_existing_transport_and_ephemeral_attempt_token(live, monkeypatch):
    service, worker, run, _, _, _, _ = live
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    projected = bind_schedule_projection(service, dict(worker), run)
    bundle = json.loads(projected["bootstrap_bundle_json"])
    token = bundle["env"]["GLASSHIVE_SCHEDULE_TOKEN"]
    assert service.peers.native_principal(token, purpose="schedule")["attempt_id"] == "attempt-a"
    assert "xperfect-schedules" in projected["_context_tool_policy"]["runtime_ids"]
    assert bundle["claude_project_mcp"]["mcpServers"]["xperfect-schedules"]["url"].endswith("/v1/native/schedules/")
    assert 'bearer_token_env_var = "GLASSHIVE_SCHEDULE_TOKEN"' in bundle["codex_config_append"]
    persisted = service.store.get_worker(worker["worker_id"])["bootstrap_bundle_json"]
    assert token not in persisted
    with service.store._connect() as conn:
        assert token not in json.dumps([dict(row) for row in conn.execute("SELECT * FROM peer_native_sessions")])


def test_explicit_disabled_worker_projection_adds_nothing(live):
    service, worker, run, _, _, _, _ = live
    original = {**worker, "bootstrap_bundle_json": '{"schedule_access":{}}'}
    assert bind_schedule_projection(service, original, run) is original


def test_native_mcp_exposes_expected_lifecycle_without_destination_setup(live):
    import asyncio
    server = native_schedule_server(live[0])
    from workers_projects_runtime.schedule_mcp import schedule_tool_manifest
    manifest = schedule_tool_manifest()
    assert manifest["version"] == 3 and len(manifest["sha256"]) == 64
    assert server.instructions == manifest["instructions"]
    assert server._mcp_server.create_initialization_options().instructions == manifest["instructions"]
    assert "Viventium" not in manifest["instructions"]
    tools = asyncio.run(server.list_tools())
    assert {tool.name: tool.description for tool in tools} == manifest["tools"]
    assert {tool.name for tool in tools} == {"schedule_create", "schedule_list", "schedule_get", "schedule_update", "schedule_retire", "schedule_run_now", "schedule_history"}


def test_projected_token_survives_native_env_and_exact_claude_rules(live, monkeypatch):
    from workers_projects_runtime.bootstrap import bootstrap_env_for
    from workers_projects_runtime.schedule_mcp import claude_schedule_tool_rules
    service, worker, run, _, _, _, _ = live
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "1")
    projected = bind_schedule_projection(service, dict(worker), run)
    bundle = json.loads(projected["bootstrap_bundle_json"])
    assert bootstrap_env_for(projected)["GLASSHIVE_SCHEDULE_TOKEN"] == bundle["env"]["GLASSHIVE_SCHEDULE_TOKEN"]
    servers = bundle["claude_project_mcp"]["mcpServers"]
    assert len(claude_schedule_tool_rules(projected, bundle, servers)) == 7
    assert claude_schedule_tool_rules({**projected, "_active_run_id": "stale"}, bundle, servers) == []
    changed = {"xperfect-schedules": {**servers["xperfect-schedules"], "url": "https://foreign.example.test/mcp"}}
    assert claude_schedule_tool_rules(projected, bundle, changed) == []


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_clean_room_preserves_only_runtime_schedule_transport(tmp_path, transport):
    import tomllib
    from workers_projects_runtime import bootstrap, native_transport
    url = "http://127.0.0.1:8766/v1/native/schedules/" if transport == "http" else "http+unix://%2Fworkspace%2Fdata%2Fruntime.sock/v1/native/schedules/"
    server = {"type": "http", "url": url} if transport == "http" else native_transport.stdio_server(url, "GLASSHIVE_SCHEDULE_TOKEN")
    bundle = {"execution_policy": bootstrap.PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        "codex_config_append": '[mcp_servers.disallowed]\nurl = "https://foreign.example.test/mcp"\n',
        "env": {"GLASSHIVE_SCHEDULE_TOKEN": "synthetic-native-token"},
        "claude_project_mcp": {"mcpServers": {"xperfect-schedules": server}}}
    bootstrap._write_codex_config(tmp_path / "home", bundle)
    config = tomllib.loads((tmp_path / "home/.codex/config.toml").read_text())
    assert "disallowed" not in config["mcp_servers"]
    if transport == "http":
        assert config["mcp_servers"]["xperfect-schedules"]["bearer_token_env_var"] == "GLASSHIVE_SCHEDULE_TOKEN"
    else:
        assert config["mcp_servers"]["xperfect-schedules"]["args"][2] == url
        assert config["mcp_servers"]["xperfect-schedules"]["env_vars"] == ["GLASSHIVE_SCHEDULE_TOKEN"]


def test_real_native_mcp_mount_keeps_purpose_auth_and_lifecycle(live, monkeypatch):
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app
    from workers_projects_runtime.schedule_mcp import schedule_tool_manifest
    service, worker, run, token, _, _, _ = live
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    app = create_app(str(service.store.db_path), runtime_backend="stub", runtime=StubRuntime(), reconcile_on_startup=False)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        # Startup restores prior native preparation; represent a new invocation
        # admitted by this runtime, not a token surviving a process restart.
        now = datetime.now(timezone.utc)
        with service.store._connect() as conn:
            conn.execute("UPDATE runs SET state='running',active_attempt_id='attempt-b' WHERE run_id=?", (run["run_id"],))
            conn.execute("INSERT INTO run_attempts(attempt_id,run_id,attempt_number,state,claimed_at,admitted_at,runtime_invoked_at,lease_id) VALUES('attempt-b',?,2,'running',?,?,?,'lease-a')", (run["run_id"], *[now.isoformat()] * 3))
            conn.execute("UPDATE host_run_leases SET attempt_id='attempt-b',status='active',released_at=NULL,executor_id='executor-b',heartbeat_at=?,expires_at=? WHERE lease_id='lease-a'",
                (now.isoformat(), (now + timedelta(hours=1)).isoformat()))
        token = app.state.service.peers.mint_native_session(worker["worker_id"], run["run_id"], purpose="schedule")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
        response = client.post("/v1/native/schedules/", headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert response.status_code == 200, response.text
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
            "schedule_create", "schedule_list", "schedule_get", "schedule_update", "schedule_retire", "schedule_run_now", "schedule_history"}
        # Native discovery receives a useful domain description for every tool,
        # rather than relying on an application broker or a known tool name.
        descriptions = schedule_tool_manifest()["tools"]
        for tool in response.json()["result"]["tools"]:
            assert tool["description"] == descriptions[tool["name"]]
            assert "scheduling" in tool["description"]
            assert tool["description"].strip()
            assert tool["inputSchema"]["type"] == "object"
        response = client.post("/v1/native/schedules/", headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "schedule_list", "arguments": {}}})
        assert response.status_code == 200 and not response.json()["result"].get("isError"), response.text
        # An opted-in standalone native worker needs no app grant.
        monkeypatch.setenv("WPR_NATIVE_SELF_SCHEDULE_ENABLED", "true")
        app.state.store.update_worker(worker["worker_id"], bootstrap_bundle_json="{}")
        listed = client.post("/v1/native/schedules/", headers=headers,
            json={"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {"name": "schedule_list", "arguments": {}}})
        choices = json.loads(listed.json()["result"]["content"][0]["text"])["targets"]
        assert choices == [{"target": {"kind": "worker", "worker_id": worker["worker_id"]}, "label": "This worker"}]
        own = client.post("/v1/native/schedules/", headers=headers,
            json={"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "schedule_create", "arguments": {"payload": {
                "target": choices[0]["target"], "instruction": "Synthetic future task", "recurrence_type": "interval",
                "interval_seconds": 3600, "idempotency_key": "native-own-worker-create",
            }}}})
        assert own.status_code == 200 and not own.json()["result"].get("isError"), own.text
        own_definition = json.loads(own.json()["result"]["content"][0]["text"])
        assert own_definition["worker_id"] == worker["worker_id"] and own_definition["target"] == choices[0]["target"]
        for sequence, created_now in [(22, True), (23, False)]:
            started = client.post("/v1/native/schedules/", headers=headers,
                json={"jsonrpc": "2.0", "id": sequence, "method": "tools/call", "params": {
                    "name": "schedule_run_now", "arguments": {
                        "definition_id": own_definition["definition_id"], "idempotency_token": "native-own-worker-now",
                    }}})
            assert started.status_code == 200 and not started.json()["result"].get("isError"), started.text
            receipt = json.loads(started.json()["result"]["content"][0]["text"])
            assert set(receipt) == {"definition_id", "occurrence_id", "status", "state", "scheduled_for", "occurrence_created"}
            assert receipt["definition_id"] == own_definition["definition_id"]
            assert receipt["occurrence_created"] is created_now
            assert receipt["status"] == "scheduled" and receipt["scheduled_for"]
        raw_receipt = app.state.service.run_recurring_schedule_now(
            own_definition["definition_id"], tenant_id="tenant-a", owner_id="owner-a", idempotency_token="native-own-worker-now")
        assert raw_receipt["instruction"] == "Synthetic future task"
        assert raw_receipt["owner_action"] == "dispatch_here" and raw_receipt["occurrence_created"] is False
        destination = app.state.service.create_webhook_destination(
            tenant_id="tenant-a", owner_id="owner-a", name="Synthetic receiver",
            url="https://receiver.example.test/hook", idempotency_key="destination-native-a",
        )
        target = {"kind": "webhook", "destination_id": destination["destination_id"], "request_data": {"request_ref": "request-native-a"}}
        app.state.store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps({"schedule_access": {
            "destinations": [{key: target[key] for key in ("destination_id", "request_data")}],
            "defaults": {"interval": {"misfire_grace_seconds": 900, "catch_up_policy": "skip"}},
        }}))
        created = client.post("/v1/native/schedules/", headers=headers,
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "schedule_create", "arguments": {"payload": {
                "target": target, "recurrence_type": "interval", "interval_seconds": 60, "idempotency_key": "native-create-a",
            }}}})
        assert created.status_code == 200 and not created.json()["result"].get("isError"), created.text
        definition = json.loads(created.json()["result"]["content"][0]["text"])
        saved = app.state.service.get_recurring_schedule(definition["definition_id"], tenant_id="tenant-a", owner_id="owner-a")
        assert saved["target"] == target and saved["worker_id"] is None
        assert saved["misfire_grace_seconds"] == 900 and saved["catch_up_policy"] == "skip"
        retried = client.post("/v1/native/schedules/", headers=headers,
            json={"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "schedule_create", "arguments": {"payload": {
                "target": target, "recurrence_type": "interval", "interval_seconds": 60, "idempotency_key": "native-create-a",
            }}}})
        assert not retried.json()["result"].get("isError"), retried.text
        assert json.loads(retried.json()["result"]["content"][0]["text"])["definition_id"] == definition["definition_id"]
        for sequence, (name, arguments) in enumerate([
            ("schedule_get", {"definition_id": definition["definition_id"]}),
            ("schedule_update", {"definition_id": definition["definition_id"], "updates": {"enabled": False}}),
            ("schedule_update", {"definition_id": definition["definition_id"], "updates": {"enabled": True}}),
            ("schedule_run_now", {"definition_id": definition["definition_id"], "idempotency_token": "native-run-now-a"}),
            ("schedule_history", {"definition_id": definition["definition_id"]}),
            ("schedule_retire", {"definition_id": definition["definition_id"]}),
        ], 10):
            result = client.post("/v1/native/schedules/", headers=headers,
                json={"jsonrpc": "2.0", "id": sequence, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            assert result.status_code == 200 and not result.json()["result"].get("isError"), result.text
            if name == "schedule_run_now":
                receipt = json.loads(result.json()["result"]["content"][0]["text"])
                assert set(receipt) == {"definition_id", "occurrence_id", "status", "state", "scheduled_for", "occurrence_created"}
                assert receipt["occurrence_created"] is True and receipt["definition_id"] == definition["definition_id"]
        assert app.state.service.get_recurring_schedule(definition["definition_id"], tenant_id="tenant-a", owner_id="owner-a")["retired_at"]
        rejected = client.post("/v1/native/schedules/", headers={**headers, "Authorization": "Bearer synthetic-service-token"},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert rejected.status_code == 403
        with service.store._connect() as conn:
            conn.execute("UPDATE runs SET state='completed' WHERE run_id=?", (run["run_id"],))
        stale = client.post("/v1/native/schedules/", headers=headers,
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        assert stale.status_code == 403


def test_later_turn_can_manage_prior_destination_schedules_but_not_reuse_create_refs(live):
    service, worker, _, token, access, _, calls = live
    access.create(token, payload())
    service.store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps({"schedule_access": {
        "manage_destination_ids": ["destination-a"], "destinations": [{"destination_id": "destination-a",
            "request_data": {"request_ref": "later-request"}, "label": "Exact later request"}],
    }}))
    assert access.list(token)["items"][0]["definition_id"] == "schedule-a"
    assert access.get(token, "schedule-a")["target"]["request_data"]["request_ref"] == "request-a"
    access.update(token, "schedule-a", UpdateRecurringScheduleRequest(enabled=False, instruction="Updated future task"))
    access.run_now(token, "schedule-a", "later-run-now")
    assert access.history(token, "schedule-a")
    access.retire(token, "schedule-a")
    with pytest.raises(PeerError):
        access.create(token, payload(idempotency_key="cannot-reuse-prior"))
    with pytest.raises(PeerError):
        access.update(token, "schedule-a", UpdateRecurringScheduleRequest(target={"kind": "webhook", "destination_id": "destination-a", "request_data": {"request_ref": "invented"}}))
    assert [name for name, _ in calls] == ["create", "update", "run_now", "retire"]


def test_native_worker_defaults_to_own_target_and_direct_queen_stays_small(live, monkeypatch):
    monkeypatch.setenv("WPR_NATIVE_SELF_SCHEDULE_ENABLED", "true")
    service, worker, run, _, _, _, _ = live
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    original = {**worker, "bootstrap_bundle_json": "{}"}
    projected = bind_schedule_projection(service, original, run)
    token = json.loads(projected["bootstrap_bundle_json"])["env"]["GLASSHIVE_SCHEDULE_TOKEN"]
    service.store.update_worker(worker["worker_id"], bootstrap_bundle_json="{}")
    choices = NativeScheduleAccess(service).list(token)["targets"]
    assert choices == [{"target": {"kind": "worker", "worker_id": worker["worker_id"]}, "label": "This worker"}]
    queen = {**worker, "profile": "direct-xai", "bootstrap_bundle_json": json.dumps({"run_mode": "conversation",
        "conversation_tool_mode": "coordination", "schedule_access": GRANT})}
    assert bind_schedule_projection(service, queen, run) is queen


def test_implicit_native_scheduling_without_route_does_not_block_existing_work(live, monkeypatch):
    monkeypatch.setenv("WPR_NATIVE_SELF_SCHEDULE_ENABLED", "true")
    from workers_projects_runtime import native_transport
    service, worker, run, _, _, _, _ = live
    monkeypatch.setattr(native_transport, "worker_route", lambda: None)
    original = {**worker, "bootstrap_bundle_json": "{}"}
    assert bind_schedule_projection(service, original, run) is original
    with pytest.raises(PeerError) as exc:
        bind_schedule_projection(service, worker, run)
    assert exc.value.code == "schedule_endpoint_unavailable"


def test_public_native_defaults_add_zero_scheduling_tools(live, monkeypatch):
    from workers_projects_runtime.schedule_mcp import schedule_access_grant
    service, worker, run, _, _, _, _ = live
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    original = {**worker, "bootstrap_bundle_json": "{}"}
    assert schedule_access_grant(original) is None
    assert bind_schedule_projection(service, original, run) is original
    monkeypatch.setenv("WPR_NATIVE_SELF_SCHEDULE_ENABLED", "true")
    disabled = {**worker, "bootstrap_bundle_json": '{"schedule_access":{}}'}
    assert bind_schedule_projection(service, disabled, run) is disabled
    assert schedule_access_grant(worker)["allow_worker_target"] is True
    explicit = {**worker, "bootstrap_bundle_json": json.dumps({"schedule_access": {"destinations": GRANT["destinations"]}})}
    assert schedule_access_grant(explicit)["allow_worker_target"] is False
    with pytest.raises(PeerError):
        schedule_access_grant({**worker, "bootstrap_bundle_json": '{"schedule_access":null}'})


def test_granted_timing_defaults_apply_only_to_omitted_fields(live):
    service, worker, _, token, access, _, calls = live
    service.store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps({"schedule_access": {
        "destinations": GRANT["destinations"], "defaults": {
            "once": {"misfire_grace_seconds": 43200, "catch_up_policy": "coalesce"},
            "interval": {"misfire_grace_seconds": 900, "catch_up_policy": "coalesce"},
        }}}))
    access.create(token, payload(recurrence_type="once", first_run_at="2030-01-01T00:00:00Z", interval_seconds=None))
    assert calls[-1][1]["misfire_grace_seconds"] == 43200
    assert calls[-1][1]["catch_up_policy"] == "coalesce"
    access.create(token, payload(idempotency_key="timing-repeat"))
    assert calls[-1][1]["misfire_grace_seconds"] == 900
    access.create(token, payload(idempotency_key="timing-explicit", misfire_grace_seconds=0, catch_up_policy="skip"))
    assert calls[-1][1]["misfire_grace_seconds"] == 0
    assert calls[-1][1]["catch_up_policy"] == "skip"
    assert access.list(token)["defaults"]["once"]["misfire_grace_seconds"] == 43200
