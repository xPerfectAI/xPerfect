from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.scheduling_owner import mint_scheduling_cortex_workspace_assertion


def _create_worker(client: TestClient, *, headers: dict[str, str] | None = None) -> dict:
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "ignored-owner" if headers else "demo-owner",
            "title": "Recurring work",
            "goal": "Run a synthetic recurring task.",
            "default_worker_profile": "codex-cli",
        },
    )
    assert project.status_code == 201, project.text
    worker = client.post(
        f"/v1/projects/{project.json()['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored-owner" if headers else "demo-owner",
            "name": "Recurring worker",
            "role": "operator",
            "profile": "codex-cli",
            "execution_mode": "docker",
            "start_synchronously": False,
        },
    )
    assert worker.status_code == 201, worker.text
    return worker.json()


def _enterprise_headers(user_id: str) -> dict[str, str]:
    return {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-public-safe",
        "X-Viventium-User-Id": user_id,
        "X-Viventium-User-Email": f"{user_id}@example.invalid",
        "X-Viventium-User-Role": "member",
    }


def _scheduler_headers(payload: dict, *, secret: str = "synthetic-scheduler-secret", issued_at: int | None = None):
    return {
        "X-Viventium-Scheduler-Assertion": mint_scheduling_cortex_workspace_assertion(
            secret=secret,
            request_payload=payload,
            issued_at=issued_at,
        )
    }


def test_recurring_schedule_api_create_list_inspect_and_deactivate(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)

    created = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Create the daily synthetic report.",
            "recurrence_type": "daily",
            "local_time": "09:00",
            "timezone_name": "America/New_York",
            "dst_policy": "next_valid_earliest",
            "first_run_at": "2027-01-02T14:00:00+00:00",
            "schedule_text": "Every day at 9 AM New York time",
        },
    )

    assert created.status_code == 201, created.text
    definition = created.json()
    assert definition["worker_id"] == worker["worker_id"]
    assert definition["owner_id"] == "demo-owner"
    assert definition["scheduler_owner"] == "glasshive_native"
    assert definition["active"] is True
    assert definition["next_run_at"] == "2027-01-02T14:00:00+00:00"
    stored = client.app.state.store.get_recurring_schedule_definition(
        definition["definition_id"], tenant_id="local", owner_id="demo-owner"
    )
    assert stored["scheduler_owner"] == "native"

    global_list = client.get("/v1/recurring-schedules")
    worker_list = client.get(f"/v1/workers/{worker['worker_id']}/recurring-schedules")
    detail = client.get(f"/v1/recurring-schedules/{definition['definition_id']}")
    occurrences = client.get(f"/v1/recurring-schedules/{definition['definition_id']}/occurrences")

    assert global_list.status_code == 200, global_list.text
    assert [item["definition_id"] for item in global_list.json()["items"]] == [definition["definition_id"]]
    assert global_list.json()["items"][0]["workspace_name"] == "Recurring worker"
    assert [item["definition_id"] for item in worker_list.json()["items"]] == [definition["definition_id"]]
    assert detail.json()["definition_id"] == definition["definition_id"]
    assert occurrences.status_code == 200, occurrences.text
    assert occurrences.json()["items"] == []

    deactivated = client.post(f"/v1/recurring-schedules/{definition['definition_id']}/deactivate")
    active_only = client.get("/v1/recurring-schedules")
    with_inactive = client.get("/v1/recurring-schedules?include_inactive=true")

    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["active"] is False
    assert active_only.json()["items"] == []
    assert with_inactive.json()["items"][0]["active"] is False


def test_recurring_schedule_api_rejects_invalid_spec_and_fails_closed_when_owner_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)

    invalid = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Run daily.",
            "recurrence_type": "daily",
            "local_time": "09:00",
            "timezone_name": "Not/A-Timezone",
        },
    )
    assert invalid.status_code == 400
    assert "IANA timezone" in invalid.json()["detail"]

    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    delegated = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Run hourly.",
            "recurrence_type": "interval",
            "interval_seconds": 3600,
            "timezone_name": "UTC",
            "first_run_at": "2027-01-02T14:00:00+00:00",
        },
    )
    assert delegated.status_code == 503, delegated.text
    assert delegated.json()["error"]["code"] == "scheduling_owner_unavailable"
    assert client.app.state.store.list_recurring_schedule_definitions(
        worker["worker_id"], tenant_id="local", owner_id="demo-owner"
    ) == []


def test_recurring_schedule_api_rejects_closed_workspace_without_persisting_definition(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)

    terminated = client.post(f"/v1/workers/{worker['worker_id']}/terminate")
    assert terminated.status_code == 202
    assert terminated.json()["state"] == "terminated"

    created = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Do not persist this recurring task.",
            "recurrence_type": "interval",
            "interval_seconds": 3600,
            "timezone_name": "UTC",
            "first_run_at": "2027-01-02T14:00:00+00:00",
        },
    )

    assert created.status_code == 409, created.text
    assert "closed" in created.json()["detail"].lower()
    assert client.app.state.store.list_recurring_schedule_definitions(
        worker["worker_id"], tenant_id="local", owner_id="demo-owner"
    ) == []


def test_closed_workspace_cannot_reenable_native_recurring_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    created = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Do not revive after close.",
            "recurrence_type": "interval",
            "interval_seconds": 3600,
            "timezone_name": "UTC",
            "first_run_at": "2027-01-02T14:00:00+00:00",
        },
    )
    assert created.status_code == 201, created.text
    definition_id = created.json()["definition_id"]
    assert client.post(f"/v1/workers/{worker['worker_id']}/terminate").status_code == 202

    enabled = client.patch(
        f"/v1/recurring-schedules/{definition_id}",
        json={"enabled": True},
    )

    assert enabled.status_code == 409, enabled.text
    stored = client.app.state.store.get_recurring_schedule_definition(
        definition_id,
        tenant_id="local",
        owner_id="demo-owner",
    )
    assert stored["active"] is False
    assert stored["enabled"] is False


def test_recurring_schedule_creation_losing_race_to_workspace_close_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    store = client.app.state.store
    original_create = store.create_recurring_schedule_definition

    def close_then_create(**kwargs):
        store.update_worker_state(worker["worker_id"], "terminated")
        return original_create(**kwargs)

    monkeypatch.setattr(store, "create_recurring_schedule_definition", close_then_create)
    created = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Do not persist after closure wins.",
            "recurrence_type": "interval",
            "interval_seconds": 3600,
            "timezone_name": "UTC",
            "first_run_at": "2027-01-02T14:00:00+00:00",
        },
    )

    assert created.status_code == 409, created.text
    assert "closed" in created.json()["detail"].lower()
    assert store.list_recurring_schedule_definitions(
        worker["worker_id"], tenant_id="local", owner_id="demo-owner"
    ) == []


def test_recurring_schedule_api_reads_the_authoritative_delegated_definition(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    definitions = {}

    def owner_call(_self, action, payload, *, identity):
        if action == "create":
            definition = {
                **payload,
                "tenant_id": identity.tenant_id,
                "owner_id": identity.owner_id,
                "scheduler_owner": "viventium_cortex",
                "schedule_owner": "viventium_cortex",
                "owner_action": "dispatch_via_viventium_cortex",
                "active": True,
                "created_at": "2027-01-01T00:00:00+00:00",
                "updated_at": "2027-01-01T00:00:00+00:00",
                "last_occurrence_at": "2027-01-01T23:00:00+00:00",
                "last_outcome": "action_required",
                "last_error": "provider_reconnect_required",
                "last_delivery_outcome": "action_required",
                "last_delivery_reason": "provider_reconnect_required",
                "last_delivery_at": "2027-01-01T23:00:01+00:00",
                "retired_at": None,
            }
            definitions[definition["definition_id"]] = definition
            return definition
        if action == "list":
            return list(definitions.values())
        if action == "get":
            return definitions[payload["definition_id"]]
        raise AssertionError(action)

    monkeypatch.setattr(
        "workers_projects_runtime.scheduling_owner.ViventiumSchedulingOwnerClient.call",
        owner_call,
    )
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)

    created = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Run the delegated synthetic check.",
            "recurrence_type": "interval",
            "interval_seconds": 3600,
            "first_run_at": "2027-01-02T14:00:00+00:00",
        },
    )
    listed = client.get("/v1/recurring-schedules")
    inspected = client.get(f"/v1/recurring-schedules/{created.json()['definition_id']}")

    assert created.status_code == 201, created.text
    assert listed.status_code == 200, listed.text
    assert inspected.status_code == 200, inspected.text
    assert listed.json()["items"] == [inspected.json()]
    assert created.json()["schedule_owner"] == "viventium_cortex"
    assert listed.json()["items"][0]["last_outcome"] == "action_required"
    assert listed.json()["items"][0]["last_error"] == "provider_reconnect_required"
    assert listed.json()["items"][0]["last_delivery_at"] == "2027-01-01T23:00:01+00:00"
    assert client.app.state.store.list_recurring_schedule_definitions(
        worker["worker_id"], tenant_id="local", owner_id="demo-owner"
    ) == []


def test_scheduling_cortex_internal_dispatch_is_owner_scoped_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setattr(
        "workers_projects_runtime.service.httpx.post",
        lambda *args, **kwargs: type("Response", (), {"status_code": 200})(),
    )
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    payload = {
        "occurrence_id": "sp_run_synthetic_occurrence",
        "task_id": "rsd_synthetic",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "Run the synthetic delegated occurrence.",
    }
    headers = _scheduler_headers(payload)

    first = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=headers,
        json=payload,
    )
    repeated = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=headers,
        json=payload,
    )
    foreign_payload = {**payload, "owner_id": "another-owner", "occurrence_id": "sp_run_foreign_owner"}
    foreign = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(foreign_payload),
        json=foreign_payload,
    )

    assert first.status_code == 202, first.text
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["run_id"] == first.json()["run_id"]
    assert foreign.status_code == 404
    assert len(client.app.state.store.list_runs_for_worker(worker["worker_id"])) == 1


def test_scheduling_cortex_dispatch_rejects_persistable_capability_bundles_and_keeps_db_clean(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub"))
    worker = _create_worker(client)
    client.app.state.store.update_worker(
        worker["worker_id"],
        bootstrap_bundle_json=json.dumps({"env": {"PERSISTENT_SETTING": "kept"}}),
    )
    monkeypatch.setattr(client.app.state.service, "_ensure_worker_processor", lambda _worker_id: None)

    def payload_for(occurrence_id: str):
        payload = {
            "occurrence_id": occurrence_id,
            "task_id": "rsd_run_scoped_bundle",
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "project_id": worker["project_id"],
            "worker_id": worker["worker_id"],
            "execution_mode": "docker",
            "instruction": "Use only this occurrence's fresh capability authority.",
        }
        return payload

    unsafe_payload = {
        **payload_for("sp_run_scoped_bundle_rejected"),
        "bootstrap_bundle": {
            "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-token-must-never-persist"},
            "glasshive_capability_broker": {"grant_id": "ghcb_must_never_persist"},
        },
    }
    rejected = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(unsafe_payload),
        json=unsafe_payload,
    )

    safe_payload = payload_for("sp_run_scoped_bundle_safe")
    accepted = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(safe_payload),
        json=safe_payload,
    )

    assert rejected.status_code == 422, rejected.text
    assert accepted.status_code == 202, accepted.text
    assert "bootstrap_bundle" not in accepted.json()
    persisted_worker = client.app.state.store.get_worker(worker["worker_id"])
    assert json.loads(persisted_worker["bootstrap_bundle_json"]) == {
        "env": {"PERSISTENT_SETTING": "kept"}
    }
    run = client.app.state.store.get_run(accepted.json()["run_id"])
    assert str(run.get("runtime_bundle_json") or "") in {"", "{}"}
    runtime_worker = client.app.state.service._runtime_worker_for_run(persisted_worker, run)
    assert "GLASSHIVE_CAPABILITY_BROKER_TOKEN" not in runtime_worker["bootstrap_bundle_json"]
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            assert b"synthetic-token-must-never-persist" not in candidate.read_bytes()


def test_scheduling_cortex_internal_dispatch_rejects_native_owner_before_any_mutation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    payload = {
        "occurrence_id": "sp_run_native_owner_rejected",
        "task_id": "rsd_native_owner_rejected",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "This delegated occurrence must not run under native ownership.",
    }

    response = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json=payload,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Scheduling Cortex is not the configured recurrence owner"
    assert client.app.state.store.list_runs_for_worker(worker["worker_id"]) == []
    assert client.app.state.store.list_schedules_for_worker(worker["worker_id"]) == []


def test_scheduling_cortex_internal_dispatch_rejects_raw_secret_tampering_and_expiry(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    payload = {
        "occurrence_id": "sp_run_assertion_security",
        "task_id": "rsd_assertion_security",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "Run the assertion security check.",
    }

    raw_secret = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers={"X-Viventium-Scheduler-Secret": "synthetic-scheduler-secret"},
        json=payload,
    )
    tampered = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json={**payload, "instruction": "Tampered instruction."},
    )
    bundle_payload = {
        **payload,
        "occurrence_id": "sp_run_assertion_bundle_security",
        "bootstrap_bundle": {
            "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-original-token"}
        },
    }
    tampered_bundle = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(bundle_payload),
        json={
            **bundle_payload,
            "bootstrap_bundle": {
                "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-tampered-token"}
            },
        },
    )
    expired = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload, issued_at=int(time.time()) - 300),
        json=payload,
    )

    assert raw_secret.status_code == 401
    assert tampered.status_code == 401
    assert tampered_bundle.status_code == 422
    assert expired.status_code == 401
    assert client.app.state.store.list_runs_for_worker(worker["worker_id"]) == []
    assert client.app.state.store.list_schedules_for_worker(worker["worker_id"]) == []


def test_scheduling_cortex_dispatch_revalidates_unready_personal_account_before_mutation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    account = client.app.state.control_plane.create_provider_account(
        tenant_id="local",
        owner_id="demo-owner",
        provider="codex",
        label="Synthetic personal account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://synthetic-account",
        status="action_required",
    )
    client.app.state.store.update_worker(
        worker["worker_id"],
        bootstrap_bundle_json=json.dumps(
            {
                "provider_account": {
                    "policy": "personal_required",
                    "account_id": account["account_id"],
                }
            }
        ),
    )
    payload = {
        "occurrence_id": "sp_run_unready_personal_account",
        "task_id": "rsd_unready_personal_account",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "Do not run after this personal account is revoked.",
    }

    response = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json=payload,
    )

    assert response.status_code == 409, response.text
    assert response.json()["failure_class"] == "workspace_fire_revalidation_failed"
    assert response.json()["action_required"] is True
    assert client.app.state.store.list_runs_for_worker(worker["worker_id"]) == []
    assert client.app.state.store.list_schedules_for_worker(worker["worker_id"]) == []


def test_disabled_multi_user_principal_blocks_delegated_fire_before_mutation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    # Startup validation is covered by the signed-assertion auth suite. Toggle
    # the service's dynamic fire-time guard here to isolate revocation behavior.
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    client.app.state.store.ensure_schedule_principal_authority(
        tenant_id="local",
        owner_id="demo-owner",
    )
    client.app.state.store.set_schedule_principal_authority(
        tenant_id="local",
        owner_id="demo-owner",
        enabled=False,
    )
    payload = {
        "occurrence_id": "sp_run_disabled_principal",
        "task_id": "rsd_disabled_principal",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "This occurrence must not run after account disablement.",
    }

    response = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json=payload,
    )

    assert response.status_code == 409, response.text
    assert response.json()["failure_class"] == "principal_disabled"
    assert response.json()["failure_retryable"] is False
    assert response.json()["action_required"] is True
    assert client.app.state.store.list_runs_for_worker(worker["worker_id"]) == []
    assert client.app.state.store.list_schedules_for_worker(worker["worker_id"]) == []


def test_principal_disable_race_is_rechecked_inside_delegated_schedule_reservation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    client.app.state.store.ensure_schedule_principal_authority(
        tenant_id="local",
        owner_id="demo-owner",
    )
    service = client.app.state.service
    original_revalidate = service.revalidate_scheduling_cortex_workspace_fire

    def disable_after_initial_revalidation(*args, **kwargs):
        result = original_revalidate(*args, **kwargs)
        client.app.state.store.set_schedule_principal_authority(
            tenant_id="local",
            owner_id="demo-owner",
            enabled=False,
        )
        return result

    monkeypatch.setattr(
        service,
        "revalidate_scheduling_cortex_workspace_fire",
        disable_after_initial_revalidation,
    )
    payload = {
        "occurrence_id": "sp_run_disable_race",
        "task_id": "rsd_disable_race",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "Do not reserve this occurrence after revocation wins the race.",
    }

    response = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json=payload,
    )

    assert response.status_code == 409, response.text
    assert response.json()["failure_class"] == "principal_disabled"
    assert client.app.state.store.list_runs_for_worker(worker["worker_id"]) == []
    assert client.app.state.store.list_schedules_for_worker(worker["worker_id"]) == []


def test_workspace_close_after_cortex_reservation_cancels_occurrence_without_run(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret")
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    store = client.app.state.store
    original_reserve = store.create_or_get_cortex_workspace_schedule

    def reserve_then_close(**kwargs):
        schedule = original_reserve(**kwargs)
        store.update_worker_state(worker["worker_id"], "terminating")
        return schedule

    monkeypatch.setattr(store, "create_or_get_cortex_workspace_schedule", reserve_then_close)
    payload = {
        "occurrence_id": "sp_run_close_race",
        "task_id": "rsd_close_race",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "Do not run after workspace closure wins.",
    }

    response = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json=payload,
    )

    assert response.status_code == 409, response.text
    assert "closed" in response.json()["detail"].lower()
    schedules = store.list_schedules_for_worker(worker["worker_id"], include_done=True)
    assert len(schedules) == 1
    assert schedules[0]["state"] == "cancelled"
    assert store.list_runs_for_worker(worker["worker_id"]) == []


def test_scheduling_cortex_internal_dispatch_fails_before_mutation_without_callback_config(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.delenv("GLASSHIVE_SCHEDULING_OWNER_URL", raising=False)
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)

    payload = {
        "occurrence_id": "sp_run_missing_callback",
        "task_id": "rsd_missing_callback",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "Do not start without a terminal callback route.",
    }
    response = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json=payload,
    )

    assert response.status_code == 503, response.text
    assert client.app.state.store.list_runs_for_worker(worker["worker_id"]) == []
    assert client.app.state.store.list_schedules_for_worker(worker["worker_id"]) == []


def test_scheduling_cortex_workspace_run_callbacks_return_to_authoritative_ledger(tmp_path, monkeypatch):
    captured: list[dict[str, object]] = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_post(url, *, content, headers, timeout):
        captured.append({"url": url, "content": content, "headers": headers, "timeout": timeout})
        return Response()

    callback_secret = "synthetic-callback-secret"
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "viventium_cortex")
    monkeypatch.setenv("VIVENTIUM_SCHEDULER_SECRET", "synthetic-scheduler-secret")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", callback_secret)
    monkeypatch.setenv("GLASSHIVE_SCHEDULING_OWNER_URL", "http://127.0.0.1:7110/mcp")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)
    occurrence_id = "sp_run_callback_occurrence"

    payload = {
        "occurrence_id": occurrence_id,
        "task_id": "rsd_callback",
        "tenant_id": "local",
        "owner_id": "demo-owner",
        "project_id": worker["project_id"],
        "worker_id": worker["worker_id"],
        "execution_mode": "docker",
        "instruction": "Run the callback reconciliation check.",
    }
    response = client.post(
        "/internal/scheduling-cortex/workspace-runs",
        headers=_scheduler_headers(payload),
        json=payload,
    )

    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(json.loads(str(item["content"], "utf-8"))["event"] == "run.completed" for item in captured):
            break
        time.sleep(0.01)
    completed = next(
        item for item in captured if json.loads(str(item["content"], "utf-8"))["event"] == "run.completed"
    )
    payload = json.loads(str(completed["content"], "utf-8"))
    assert completed["url"] == "http://127.0.0.1:7110/internal/scheduled-prompts/glasshive-callback"
    assert payload["message_id"] == occurrence_id
    assert payload["run_id"] == response.json()["run_id"]
    binding = f"{worker['worker_id']}:{payload['run_id']}".encode("utf-8")
    derived = hmac.new(callback_secret.encode("utf-8"), binding, hashlib.sha256).hexdigest().encode("utf-8")
    expected = "sha256=" + hmac.new(derived, completed["content"], hashlib.sha256).hexdigest()
    assert completed["headers"]["X-GlassHive-Signature"] == expected


def test_recurring_schedule_api_accepts_full_structured_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    worker = _create_worker(client)

    created = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        json={
            "instruction": "Create the weekday synthetic report.",
            "recurrence_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "timezone_name": "America/Toronto",
            "starts_at": "2027-01-01T00:00:00-05:00",
            "ends_at": "2027-03-31T23:59:59-04:00",
            "enabled": True,
            "overlap_policy": "skip",
            "misfire_grace_seconds": 600,
            "catch_up_policy": "bounded",
            "max_catch_up_occurrences": 2,
            "jitter_seconds": 120,
        },
    )

    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["recurrence_type"] == "cron"
    assert payload["cron_expression"] == "0 9 * * 1-5"
    assert payload["starts_at"] == "2027-01-01T05:00:00+00:00"
    assert payload["ends_at"] == "2027-04-01T03:59:59+00:00"
    assert payload["enabled"] is True
    assert payload["overlap_policy"] == "skip"
    assert payload["misfire_grace_seconds"] == 600
    assert payload["catch_up_policy"] == "bounded"
    assert payload["max_catch_up_occurrences"] == 2
    assert payload["jitter_seconds"] == 120
    assert payload["next_occurrence_at"] == payload["next_run_at"]
    assert payload["schedule_owner"] == "glasshive_native"
    assert payload["owner_action"] == "dispatch_here"

    updated = client.patch(
        f"/v1/recurring-schedules/{payload['definition_id']}",
        json={"instruction": "Create the revised weekday report.", "enabled": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["definition_id"] == payload["definition_id"]
    assert updated.json()["instruction"] == "Create the revised weekday report."
    assert updated.json()["enabled"] is False
    assert updated.json()["active"] is False
    assert updated.json()["schedule_owner"] == "glasshive_native"
    assert client.get(
        f"/v1/recurring-schedules/{payload['definition_id']}/occurrences"
    ).json()["items"] == []

    first_run_now = client.post(
        f"/v1/recurring-schedules/{payload['definition_id']}/run-now",
        json={"idempotency_key": "manual-public-safe-1"},
    )
    repeated_run_now = client.post(
        f"/v1/recurring-schedules/{payload['definition_id']}/run-now",
        json={"idempotency_key": "manual-public-safe-1"},
    )
    assert first_run_now.status_code == 200, first_run_now.text
    assert first_run_now.json()["schedule_id"] == repeated_run_now.json()["schedule_id"]
    assert first_run_now.json()["occurrence_id"] == repeated_run_now.json()["occurrence_id"]

    retired = client.delete(f"/v1/recurring-schedules/{payload['definition_id']}")
    history = client.get(
        f"/v1/recurring-schedules/{payload['definition_id']}/occurrences"
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["retired_at"]
    assert retired.json()["active"] is False
    assert [item["occurrence_id"] for item in history.json()["items"]] == [
        first_run_now.json()["occurrence_id"]
    ]
    rejected_resume = client.patch(
        f"/v1/recurring-schedules/{payload['definition_id']}",
        json={"enabled": True},
    )
    rejected_run_now = client.post(
        f"/v1/recurring-schedules/{payload['definition_id']}/run-now",
        json={"idempotency_key": "manual-public-safe-2"},
    )
    assert rejected_resume.status_code == 400
    assert "retired schedule" in rejected_resume.json()["detail"]
    assert rejected_run_now.status_code == 404


def test_recurring_schedule_api_is_user_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-public-safe")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))
    owner_headers = _enterprise_headers("member-a")
    other_headers = _enterprise_headers("member-b")
    worker = _create_worker(client, headers=owner_headers)

    created = client.post(
        f"/v1/workers/{worker['worker_id']}/recurring-schedules",
        headers=owner_headers,
        json={
            "instruction": "Run hourly.",
            "recurrence_type": "interval",
            "interval_seconds": 3600,
            "timezone_name": "UTC",
            "first_run_at": "2027-01-02T14:00:00+00:00",
        },
    )
    assert created.status_code == 201, created.text
    definition_id = created.json()["definition_id"]

    assert client.get("/v1/recurring-schedules", headers=other_headers).json()["items"] == []
    assert client.get(f"/v1/recurring-schedules/{definition_id}", headers=other_headers).status_code == 404
    assert client.get(
        f"/v1/recurring-schedules/{definition_id}/occurrences", headers=other_headers
    ).status_code == 404
    assert client.post(
        f"/v1/recurring-schedules/{definition_id}/deactivate", headers=other_headers
    ).status_code == 404
    assert client.patch(
        f"/v1/recurring-schedules/{definition_id}", headers=other_headers, json={"enabled": False}
    ).status_code == 404
    assert client.post(
        f"/v1/recurring-schedules/{definition_id}/run-now",
        headers=other_headers,
        json={"idempotency_key": "manual-public-safe-1"},
    ).status_code == 404
    assert client.delete(
        f"/v1/recurring-schedules/{definition_id}", headers=other_headers
    ).status_code == 404
    assert client.get(f"/v1/recurring-schedules/{definition_id}", headers=owner_headers).status_code == 200
