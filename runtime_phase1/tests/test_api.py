from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import workers_projects_runtime.api as api_module
import workers_projects_runtime.openclaw_runtime as openclaw_runtime_module
import workers_projects_runtime.service as service_module
import workers_projects_runtime.signed_links as signed_links_module
from workers_projects_runtime.api import create_app
from workers_projects_runtime.grok_runtime import GrokBuildRuntime
from workers_projects_runtime.auth import AuthContext, owner_matches_auth_context
from workers_projects_runtime.control_plane import ControlPlaneConflict
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.mission_provider_accounts import ProviderAccountBusyError
from workers_projects_runtime.deliverables import (
    deliverable_payload,
    is_deliverable_url,
    is_user_deliverable_relative_path,
)
from workers_projects_runtime.openclaw_runtime import (
    HostCapacityError,
    RuntimeErrorBase,
    ProviderRateLimitError,
    RuntimeInfo,
    OpenClawRuntime,
    StubRuntime,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerTerminatedError,
    notify_runtime_started,
    runtime_start_boundary,
)
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, ProfiledWorkerRuntime
from workers_projects_runtime.service import (
    GlassHiveQuotaExceededError,
    WorkersProjectsService,
    merge_bootstrap_bundle,
    public_callback_message_text,
    terminal_callback_full_message,
    terminal_callback_message,
)
from workers_projects_runtime.signed_links import (
    SensitiveUrlLogFilter,
    create_signed_link_ref,
    install_sensitive_url_log_filter,
    redact_sensitive_url_text,
    resolve_signed_link_ref,
    revoke_signed_link_refs_for_worker,
    sign_link_params,
    sign_link_token,
    verify_signed_link,
    verify_signed_link_token,
)
from workers_projects_runtime.store import (
    RunRestorationState,
    Store,
    WorkerClosedStoreError,
)
from workers_projects_runtime.terminal_takeover import TerminalTarget


def exact_terminal_generation(store: Store, run_id: str) -> dict[str, str]:
    run = store.get_run(run_id)
    assert run is not None
    return {
        **(store.get_run_retry_generation(run_id) or {}),
        "expected_runtime_invoked_at": str(run.get("runtime_invoked_at") or ""),
    }


def test_provider_readiness_endpoint_returns_only_bounded_profile_status(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli")
    monkeypatch.setattr(
        api_module,
        "deployment_provider_readiness",
        lambda profile: (
            "action_required",
            "deployment_provider_unavailable",
        ),
    )
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub"))

    response = client.get("/v1/provider-readiness/codex-cli")

    assert response.status_code == 200
    assert response.json() == {
        "readiness": "action_required",
        "status": "deployment_provider_unavailable",
    }
    unavailable = client.get("/v1/provider-readiness/unknown-profile")
    assert unavailable.status_code == 404


def test_runtime_signed_link_sqlite_state_is_private(tmp_path, monkeypatch):
    state_path = tmp_path / "private-state" / "link-refs.sqlite3"
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(state_path))

    with signed_links_module._link_ref_conn() as connection:
        connection.execute(
            "INSERT INTO signed_link_refs "
            "(ref_id, kind, token, target_url, expires_at, created_at) "
            "VALUES ('ghr_private_test', 'worker_view', 'synthetic', '/watch/test', 0, 1)"
        )
        connection.commit()
        signed_links_module._harden_sqlite_state_path(state_path)
        assert os.stat(state_path.parent).st_mode & 0o077 == 0
        for candidate in (
            state_path,
            Path(f"{state_path}-wal"),
            Path(f"{state_path}-shm"),
        ):
            if candidate.exists():
                assert os.stat(candidate).st_mode & 0o077 == 0


def test_signed_link_helpers_close_sqlite_connections_deterministically(
    tmp_path, monkeypatch
):
    runtime_db = tmp_path / "runtime.db"
    link_ref_db = tmp_path / "link-refs.sqlite3"
    real_connect = sqlite3.connect
    with closing(real_connect(runtime_db)) as conn:
        conn.execute(
            "CREATE TABLE workers (worker_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO workers (worker_id, state) VALUES (?, ?)",
            ("wrk_connection_lifetime", "active"),
        )
        conn.commit()

    opened: list[sqlite3.Connection] = []

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setenv("WPR_DB_PATH", str(runtime_db))
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(link_ref_db))
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setattr(signed_links_module.sqlite3, "connect", tracked_connect)

    token = sign_link_token(
        kind="artifact_download",
        worker_id="wrk_connection_lifetime",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="outputs/report.html",
    )
    ref_id = create_signed_link_ref(token=token)
    assert ref_id.startswith("ghr_")
    with closing(real_connect(runtime_db)) as conn:
        conn.execute(
            "UPDATE workers SET state = ? WHERE worker_id = ?",
            ("terminated", "wrk_connection_lifetime"),
        )
        conn.commit()
    assert signed_links_module._worker_is_terminated_in_runtime_db(
        "wrk_connection_lifetime"
    )
    assert not signed_links_module.is_worker_signed_link_revoked(
        "wrk_connection_lifetime"
    )
    assert resolve_signed_link_ref(ref_id) is None

    assert len(opened) >= 4
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def write_minimal_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Client-ready research brief</w:t></w:r></w:p></w:body>
</w:document>""",
        )


def wait_for_run(client: TestClient, run_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not settle within {timeout}s")


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("Condition did not become true before timeout")


def publish_in_process_test_start(worker: dict) -> None:
    """Cross the same truthful start boundary used by in-process adapters."""

    with runtime_start_boundary(worker):
        notify_runtime_started(worker)


def publish_synthetic_isolated_capacity(monkeypatch, runtime: object) -> None:
    """Give lifecycle-only adapter tests a deterministic healthy capacity probe."""

    monkeypatch.setattr(
        runtime,
        "isolated_resource_usage",
        StubRuntime().isolated_resource_usage,
        raising=False,
    )


def truthfully_invoke_test_run(
    store: Store,
    worker: dict,
    run: dict,
    *,
    suffix: str = "running-fixture",
    executor_id: str = "",
) -> dict:
    claimed = store.claim_next_queued_run(worker["worker_id"])
    assert claimed is not None, suffix
    assert claimed["run_id"] == run["run_id"]
    executor_id = str(executor_id or f"api-test-{suffix}")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    assert store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=executor_id,
    )
    invoked = store.mark_run_runtime_invoked(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=executor_id,
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=executor_id,
        identity_kind="in_process",
        pid=None,
        process_group=None,
        process_start_identity="",
        container_id="",
        session_id="in-process",
    )
    assert confirmed is not None
    store.update_worker_state(worker["worker_id"], "running")
    return confirmed["run"]


def create_truthfully_invoked_test_run(
    store: Store,
    worker: dict,
    project_id: str,
    instruction: str,
    *,
    suffix: str,
    executor_id: str = "",
) -> dict:
    run = store.create_run(
        worker["worker_id"], project_id, instruction, state="queued"
    )
    return truthfully_invoke_test_run(
        store, worker, run, suffix=suffix, executor_id=executor_id
    )


def test_missing_exact_native_model_is_actionable_and_creates_no_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,grok-build")
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD", raising=False)

    class NativeModelRuntime(StubRuntime):
        def resolve_model(self, profile: str, execution_mode: str = "docker") -> str:
            if profile == "grok-build":
                return GrokBuildRuntime(str(tmp_path)).resolve_model(profile)
            return super().resolve_model(profile)

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime=NativeModelRuntime()))
    project_response = client.post("/v1/projects", json={
        "owner_id": "owner", "title": "Model setup", "goal": "Use Grok",
        "default_worker_profile": "grok-build",
    })
    assert project_response.status_code == 201
    project_id = project_response.json()["project_id"]

    response = client.post(f"/v1/projects/{project_id}/workers", json={
        "owner_id": "owner", "name": "Grok worker", "role": "research",
        "profile": "grok-build", "execution_mode": "docker",
    })

    assert response.status_code == 400
    assert response.json() == {"detail": {
        "code": "model_configuration_required",
        "message": "Choose an exact Grok model in Connections, or set --model grok-build=<id> when starting xPerfect.",
    }}
    assert client.get(f"/v1/projects/{project_id}/workers").json()["items"] == []


def test_completed_run_exposes_runtime_token_usage(tmp_path):
    class UsageRuntime(StubRuntime):
        def run_usage(self, worker: dict, run_id: str) -> dict[str, int]:
            return {
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 75,
            }

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime=UsageRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "owner",
            "title": "Usage",
            "goal": "Track tokens",
            "default_worker_profile": "claude-code",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "owner",
            "name": "Usage worker",
            "role": "invoice processing",
            "profile": "claude-code",
        },
    ).json()
    assigned = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "process invoice"},
    ).json()

    completed = wait_for_run(client, assigned["run_id"])

    assert completed["input_tokens"] == 100
    assert completed["output_tokens"] == 25
    assert completed["cache_read_input_tokens"] == 800
    assert completed["cache_creation_input_tokens"] == 75
    assert completed["total_tokens"] == 1000


def test_assign_persists_typed_workspace_continuation_context(
    tmp_path,
    background_consumers_disabled,
):
    _ = background_consumers_disabled
    app = create_app(str(tmp_path / "runtime.db"), runtime=StubRuntime())
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={
                "owner_id": "owner",
                "title": "Continuation",
                "goal": "Preserve the original task contract",
                "default_worker_profile": "codex-cli",
            },
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "owner",
                "name": "Continuation worker",
                "role": "operator",
                "profile": "codex-cli",
            },
        ).json()
        assigned = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={
                "instruction": "Continue from the current workspace state.",
                "continuationContext": {
                    "version": 1,
                    "baseInstruction": "Create the requested workbook.",
                    "guidance": ["Add the final owner sign-off."],
                },
            },
        )

        assert assigned.status_code == 202
        stored = app.state.store.get_run(assigned.json()["run_id"])
        assert stored is not None
        assert json.loads(stored["continuation_context_json"]) == {
            "version": 1,
            "base_instruction": "Create the requested workbook.",
            "guidance": ["Add the final owner sign-off."],
        }


def test_terminal_state_and_usage_publish_atomically(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Atomic usage", "Publish one terminal row.", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Atomic usage worker",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    run = create_truthfully_invoked_test_run(
        store,
        worker,
        project["project_id"],
        "finish with usage",
        suffix="atomic-terminal-usage",
    )
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    assert lease is not None
    generation = {
        "expected_attempt_id": str(run["active_attempt_id"]),
        "expected_lease_id": str(lease["lease_id"]),
        "expected_executor_id": str(lease["executor_id"]),
        "expected_startup_token": str(lease["startup_token"]),
        "expected_runtime_invoked_at": str(run["runtime_invoked_at"]),
    }

    usage_update_started = Event()
    release_usage_update = Event()
    original_update_run = store.update_run

    def gated_update_run(run_id: str, **fields):
        if "input_tokens" in fields:
            usage_update_started.set()
            assert release_usage_update.wait(timeout=2)
        return original_update_run(run_id, **fields)

    monkeypatch.setattr(store, "update_run", gated_update_run)
    results: list[dict | None] = []
    failures: list[BaseException] = []

    def finalize() -> None:
        try:
            results.append(
                store.finalize_run_if_state(
                    run["run_id"],
                    "running",
                    "failed",
                    error_text="synthetic provider failure",
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 3,
                    },
                    **generation,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=finalize)
    thread.start()
    observed_between_writes = None
    if usage_update_started.wait(timeout=0.2):
        observed_between_writes = store.get_run(run["run_id"])
    release_usage_update.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert observed_between_writes is None or observed_between_writes["state"] == "running"
    assert results and results[0] is not None
    assert results[0]["state"] == "failed"
    assert sum(
        int(results[0][field])
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    ) == 38


def test_terminal_run_and_linked_schedule_publish_atomically(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Atomic schedule", "Publish one terminal outcome.", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Atomic schedule worker",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    run = create_truthfully_invoked_test_run(
        store,
        worker,
        project["project_id"],
        "finish scheduled work",
        suffix="atomic-terminal-schedule",
    )
    schedule = store.create_scheduled_run(
        worker_id=worker["worker_id"],
        project_id=project["project_id"],
        tenant_id="local",
        owner_id="owner",
        instruction="finish scheduled work",
        run_at=datetime.now(timezone.utc).isoformat(),
        schedule_text="now",
    )
    store.finalize_schedule(
        schedule["schedule_id"],
        state="queued",
        queued_run_id=run["run_id"],
    )
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    assert lease is not None
    generation = {
        "expected_attempt_id": str(run["active_attempt_id"]),
        "expected_lease_id": str(lease["lease_id"]),
        "expected_executor_id": str(lease["executor_id"]),
        "expected_startup_token": str(lease["startup_token"]),
        "expected_runtime_invoked_at": str(run["runtime_invoked_at"]),
    }

    terminal_run_written = Event()
    release_later_schedule_write = Event()
    failures: list[BaseException] = []

    def finalize_like_service() -> None:
        try:
            assert store.finalize_run_if_state(
                run["run_id"],
                "running",
                "completed",
                output_text="SCHEDULED_OK",
                **generation,
            )
            terminal_run_written.set()
            assert release_later_schedule_write.wait(timeout=2)
            store.finalize_schedule_for_run(run["run_id"], state="completed")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=finalize_like_service)
    thread.start()
    assert terminal_run_written.wait(timeout=2)
    observed_run = store.get_run(run["run_id"])
    observed_schedule = store.get_schedule(schedule["schedule_id"])
    release_later_schedule_write.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert observed_run is not None and observed_run["state"] == "completed"
    assert observed_schedule is not None and observed_schedule["state"] == "completed"


def test_worker_live_exposes_content_free_runtime_telemetry(tmp_path):
    class TelemetryRuntime(StubRuntime):
        def run_telemetry(self, worker: dict, run_id: str) -> dict[str, object]:
            return {
                "schema": "glasshive.claude-run-telemetry.v1",
                "run_id": run_id,
                "duration_ms": 125000,
                "duration_api_ms": 121000,
                "api_retry_count": 2,
                "num_turns": 8,
                "tool_call_counts": {"Read": 2},
            }

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime=TelemetryRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "owner",
            "title": "Telemetry",
            "goal": "Track run health",
            "default_worker_profile": "claude-code",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "owner",
            "name": "Telemetry worker",
            "role": "invoice processing",
            "profile": "claude-code",
        },
    ).json()
    assigned = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "process invoice"},
    ).json()
    wait_for_run(client, assigned["run_id"])

    live = client.get(f"/v1/workers/{worker['worker_id']}/live").json()
    compact_live = client.get(f"/v1/workers/{worker['worker_id']}/live?compact=1").json()

    assert compact_live["latest_run"]["run_id"] == assigned["run_id"]
    assert compact_live["deliverable"] is None

    assert live["telemetry_run_id"] == assigned["run_id"]
    assert live["telemetry"]["run_id"] == assigned["run_id"]
    assert live["telemetry"]["api_retry_count"] == 2
    assert live["telemetry"]["duration_api_ms"] == 121000
    assert live["telemetry"]["tool_call_counts"] == {"Read": 2}


def test_worker_live_reads_active_run_logs_from_runtime_metadata(tmp_path):
    class LiveTelemetryRuntime(StubRuntime):
        def live_telemetry(
            self,
            worker: dict,
            stdout: str,
            run_id: str | None = None,
        ) -> dict[str, object]:
            return {
                "schema": "glasshive.claude-run-telemetry.v1",
                "run_id": run_id,
                "telemetry_scope": "full_active_run",
                "event_count": len([line for line in stdout.splitlines() if line.strip()]),
            }

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime=LiveTelemetryRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "owner",
            "title": "Live logs",
            "goal": "Show the active run",
            "default_worker_profile": "claude-code",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "owner",
            "name": "Live log worker",
            "role": "invoice processing",
            "profile": "claude-code",
        },
    ).json()
    state_dir = Path(worker["state_dir"])
    run_root = state_dir / "home" / ".glasshive-runs" / "run_live"
    run_root.mkdir(parents=True)
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    stdout_path.write_text('{"type":"system","subtype":"init"}\n')
    stderr_path.write_text("runtime warning\n")
    (state_dir / "active_terminal_session.json").write_text(
        json.dumps(
            {
                "session_name": "job-run_live",
                "run_id": "run_live",
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
        )
    )

    live = client.get(f"/v1/workers/{worker['worker_id']}/live").json()

    assert '"subtype":"init"' in live["console"]["stdout"]
    assert "runtime warning" in live["console"]["stderr"]
    assert live["telemetry"]["telemetry_scope"] == "full_active_run"
    assert live["telemetry"]["event_count"] == 1


def test_worker_live_does_not_fallback_to_console_tail_for_legacy_requested_run(tmp_path):
    class LegacyTelemetryRuntime(StubRuntime):
        def live_telemetry(self, worker: dict, stdout: str) -> dict[str, object]:
            return {
                "schema": "glasshive.claude-run-telemetry.v1",
                "telemetry_scope": "console_tail",
                "event_count": 99,
            }

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime=LegacyTelemetryRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "owner",
            "title": "Legacy telemetry",
            "goal": "Do not misbind telemetry",
            "default_worker_profile": "claude-code",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "owner",
            "name": "Legacy worker",
            "role": "invoice processing",
            "profile": "claude-code",
        },
    ).json()
    assigned = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "process invoice"},
    ).json()
    wait_for_run(client, assigned["run_id"], timeout=5.0)

    payload = client.get(f"/v1/workers/{worker['worker_id']}/telemetry").json()

    assert payload["telemetry_run_id"] == assigned["run_id"]
    assert payload["telemetry"] == {}


def test_worker_telemetry_is_bound_to_active_run_when_newer_run_is_queued(
    tmp_path,
    background_consumers_disabled,
):
    _ = background_consumers_disabled
    class RunBoundTelemetryRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.requested_run_ids: list[str] = []

        def run_telemetry(self, worker: dict, run_id: str) -> dict[str, object]:
            self.requested_run_ids.append(run_id)
            return {
                "schema": "glasshive.claude-run-telemetry.v1",
                "run_id": run_id,
                "event_count": 4,
            }

    runtime = RunBoundTelemetryRuntime()
    app = create_app(str(tmp_path / "runtime.db"), runtime=runtime)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={
                "owner_id": "owner",
                "title": "Run-bound telemetry",
                "goal": "Keep telemetry attached to the running invoice",
                "default_worker_profile": "claude-code",
            },
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "owner",
                "name": "Telemetry worker",
                "role": "invoice processing",
                "profile": "claude-code",
            },
        ).json()
        queued_active = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "active invoice",
        )
        active = truthfully_invoke_test_run(
            app.state.store,
            worker,
            queued_active,
            suffix="run-bound-telemetry",
        )
        time.sleep(0.002)
        queued = app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "next invoice",
            state="queued",
        )

        payload = client.get(f"/v1/workers/{worker['worker_id']}/telemetry").json()
        compact = client.get(
            f"/v1/workers/{worker['worker_id']}/live",
            params={"compact": "1"},
        ).json()

    assert payload["active_run"]["run_id"] == active["run_id"]
    assert payload["latest_run"]["run_id"] == queued["run_id"]
    assert "instruction" not in payload["active_run"]
    assert "instruction" not in payload["latest_run"]
    assert "output_text" not in payload["active_run"]
    assert "error_text" not in payload["latest_run"]
    assert payload["telemetry_run_id"] == active["run_id"]
    assert payload["telemetry"]["run_id"] == active["run_id"]
    assert runtime.requested_run_ids == [active["run_id"], active["run_id"]]
    assert "console" not in payload
    assert "artifacts" not in payload
    assert compact["compact"] is True
    assert compact["active_run"]["run_id"] == active["run_id"]
    assert compact["telemetry_run_id"] == active["run_id"]
    assert compact["telemetry"]["run_id"] == active["run_id"]
    assert compact["project_runs"] == []
    assert compact["workspace"]["items"] == []
    assert compact["artifacts"]["items"] == []


def test_fresh_user_artifact_deliverable_accepts_standard_deliverable_roots(
    tmp_path,
    background_consumers_disabled,
    request,
):
    _ = background_consumers_disabled
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    request.addfinalizer(service.shutdown)
    workspace = tmp_path / "workspace"
    reports_dir = workspace / "reports"
    output_dir = workspace / "output"
    reports_dir.mkdir(parents=True)
    output_dir.mkdir()
    report_path = reports_dir / "summary.html"
    output_path = output_dir / "final_screen.csv"
    companies_path = output_dir / "companies.csv"
    report_path.write_text("<html><body>FINAL REPORT</body></html>\n")
    output_path.write_text("name\nAlpha\n")
    companies_path.write_text("company\nAlpha\n")
    started = datetime.now(timezone.utc).isoformat()
    worker = {"worker_id": "wrk_fresh", "workspace_dir": str(workspace)}
    run = {"run_id": "run_fresh", "started_at": started, "failure_class": "provider_response_failed"}

    assert service._fresh_user_artifact_deliverable(
        worker,
        run,
        {"workspace_path": "reports/summary.html"},
    )
    assert service._fresh_user_artifact_deliverable(
        worker,
        run,
        {"workspace_path": "output/final_screen.csv"},
    )
    assert service._fresh_user_artifact_deliverable(
        worker,
        run,
        {"workspace_path": "output/companies.csv"},
    )
    assert not service._fresh_user_artifact_deliverable(
        worker,
        run,
        {"workspace_path": "glasshive-run/evidence.json"},
    )


def test_fresh_user_artifact_deliverable_rejects_support_dirs(
    tmp_path,
    background_consumers_disabled,
    request,
):
    _ = background_consumers_disabled
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    request.addfinalizer(service.shutdown)
    workspace = tmp_path / "workspace"
    research_dir = workspace / "research"
    research_dir.mkdir(parents=True)
    dataset_path = research_dir / "companies.csv"
    dataset_path.write_text("company\nAlpha\n")
    worker = {"worker_id": "wrk_support", "workspace_dir": str(workspace)}
    run = {
        "run_id": "run_support",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "failure_class": "provider_response_failed",
    }

    assert not service._fresh_user_artifact_deliverable(
        worker,
        run,
        {"workspace_path": "research/companies.csv"},
    )


def assert_link_ref_url(url: str, *, prefix: str, kind: str) -> dict:
    assert url.startswith(prefix)
    assert "/v1/signed-links/" not in url
    assert "gh_token=" not in url
    ref_id = urlsplit(url).path.rsplit("/", 1)[-1]
    record = resolve_signed_link_ref(ref_id)
    assert record is not None
    assert record["kind"] == kind
    assert record["payload"]["kind"] == kind
    return record


def href_for_link_text(html: str, label: str) -> str:
    match = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>\s*' + re.escape(label) + r"\s*</a>", html)
    assert match, f"Expected link labeled {label!r}"
    return match.group(1)


def anchor_for_link_text(html: str, label: str) -> str:
    match = re.search(r"<a\b([^>]*)>\s*" + re.escape(label) + r"\s*</a>", html)
    assert match, f"Expected link labeled {label!r}"
    return match.group(1)


def test_store_migrates_compute_released_marker_for_existing_db(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    with store._connect() as conn:
        assert "compute_released_at" in {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
        try:
            conn.execute("ALTER TABLE workers DROP COLUMN compute_released_at")
        except sqlite3.OperationalError as exc:
            pytest.skip(f"SQLite runtime does not support DROP COLUMN: {exc}")

    migrated = Store(str(db_path))
    with migrated._connect() as conn:
        assert "compute_released_at" in {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    project = migrated.create_project("owner", "Migration", "Verify worker marker migration", "codex-cli")
    worker = migrated.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Migrated Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    worker = migrated.get_worker(worker["worker_id"]) or worker

    assert worker["compute_released_at"] is None


def test_store_migrates_run_scoped_runtime_bundle_for_existing_db(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    with store._connect() as conn:
        assert "runtime_bundle_json" in {
            row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        try:
            conn.execute("ALTER TABLE runs DROP COLUMN runtime_bundle_json")
        except sqlite3.OperationalError as exc:
            pytest.skip(f"SQLite runtime does not support DROP COLUMN: {exc}")

    migrated = Store(str(db_path))
    with migrated._connect() as conn:
        assert "runtime_bundle_json" in {
            row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }


def test_terminal_callback_message_prefers_final_report():
    output = "\n".join(
        [
            "I am starting the browser.",
            "I am still scrolling through results.",
            "",
            "FINAL REPORT:",
            "Captured 42 rows.",
            "",
            "Created `results.md` and stopped on the target page.",
        ]
    )

    assert terminal_callback_message(output) == (
        "Captured 42 rows.\n\nCreated `results.md` and stopped on the target page."
    )


def test_user_preferences_are_scoped_and_validate_profile_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,openclaw-general")
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))

    user_a = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
    }
    user_b = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-b",
    }

    saved = client.patch(
        "/v1/preferences",
        headers=user_a,
        json={"default_worker_profile": "openclaw-general", "codex_reasoning_effort": "high"},
    )
    assert saved.status_code == 200
    assert saved.json()["owner_id"] == "user-a"
    assert saved.json()["default_worker_profile"] == "openclaw-general"
    assert saved.json()["codex_reasoning_effort"] == "high"

    other = client.get("/v1/preferences", headers=user_b)
    assert other.status_code == 200
    assert other.json()["owner_id"] == "user-b"
    assert other.json()["default_worker_profile"] == ""

    rejected = client.patch("/v1/preferences", headers=user_a, json={"default_worker_profile": "claude-code"})
    assert rejected.status_code == 400
    assert "not allowed" in rejected.text


def test_local_service_identity_cannot_cross_owner_project_worker_or_run_scope(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    app = create_app(
        db_path=str(tmp_path / "local-owner-scope.db"),
        runtime_backend="stub",
        reconcile_on_startup=False,
    )
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    client = TestClient(app)
    owner_a = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-local",
        "X-Viventium-User-Id": "owner-a",
    }
    owner_b = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-local",
        "X-Viventium-User-Id": "owner-b",
    }

    created_project = client.post(
        "/v1/projects",
        headers=owner_a,
        json={"owner_id": "forged-owner", "title": "Owner A", "goal": "Scoped work"},
    )
    assert created_project.status_code == 201
    project = created_project.json()
    assert project["owner_id"] == "owner-a"
    created_worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=owner_a,
        json={
            "owner_id": "owner-b",
            "name": "Owner A worker",
            "role": "Scoped worker",
            "profile": "codex-cli",
            "start_synchronously": False,
        },
    )
    assert created_worker.status_code == 201
    worker = created_worker.json()
    assert worker["owner_id"] == "owner-a"
    assigned = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        headers=owner_a,
        json={"instruction": "Do owner A work."},
    )
    assert assigned.status_code == 202
    run = assigned.json()

    assert [item["project_id"] for item in client.get("/v1/projects", headers=owner_b).json()["items"]] == []
    for path in (
        f"/v1/projects/{project['project_id']}",
        f"/v1/projects/{project['project_id']}/workers",
        f"/v1/workers/{worker['worker_id']}",
        f"/v1/runs/{run['run_id']}",
    ):
        assert client.get(path, headers=owner_b).status_code == 404
    assert client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=owner_b,
        json={
            "owner_id": "owner-b",
            "name": "Intruder",
            "role": "Must not attach",
            "profile": "codex-cli",
            "start_synchronously": False,
        },
    ).status_code == 404
    assert client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        headers=owner_b,
        json={"instruction": "Cross-owner assignment"},
    ).status_code == 404
    assert client.post(
        f"/v1/workers/{worker['worker_id']}/pause", headers=owner_b
    ).status_code == 404
    with pytest.raises(WebSocketDisconnect) as cross_owner_terminal:
        with client.websocket_connect(
            f"/ws/workers/{worker['worker_id']}/terminal",
            headers=owner_b,
        ):
            pass
    assert cross_owner_terminal.value.code == 4404


def test_terminal_callback_message_uses_line_anchored_final_report_marker():
    output = "\n".join(
        [
            "Progress: the harness says to include FINAL REPORT: at the end.",
            "",
            "FINAL REPORT:",
            "Captured 42 rows.",
        ]
    )

    assert terminal_callback_message(output) == "Captured 42 rows."


def test_terminal_callback_message_accepts_inline_final_report_marker():
    output = "Progress that should not surface.\nFINAL REPORT: Captured 42 rows."

    assert terminal_callback_message(output) == "Captured 42 rows."


def test_terminal_callback_message_accepts_backtick_wrapped_final_report_marker():
    output = "Progress that should not surface.\n\n`FINAL REPORT:`\n\nCaptured 42 rows."

    assert terminal_callback_message(output) == "Captured 42 rows."
    assert terminal_callback_full_message(output) == "Captured 42 rows."


def test_terminal_callback_message_uses_tail_without_mid_word_fragment():
    output = "\n\n".join(
        [
            "Opening the browser and trying the first path.",
            "Still gathering rows from the page.",
            "The useful result is ready.\nSaved the export and needs one approval.",
        ]
    )

    message = terminal_callback_message(output, fallback="Done")

    assert "The useful result is ready" in message
    assert not message.startswith("ows ")


def test_terminal_callback_message_keeps_long_markerless_tail_with_context():
    output = "\n".join(
        [
            "Progress " + ("still working " * 220),
            "More progress " + ("checking browser state " * 160),
            "Example Domain",
        ]
    )

    message = terminal_callback_message(output, fallback="Done")

    assert len(message) <= 4000
    assert message.startswith("...")
    assert message.endswith("Example Domain")


def test_terminal_callback_message_keeps_short_markerless_multiline_result():
    output = "\n".join(
        [
            "Using the host browser and checking the page.",
            "Chrome is loaded; updating the work log.",
            "Viventium",
        ]
    )

    assert terminal_callback_message(output, fallback="Done") == output


def test_terminal_callback_message_respects_visible_budget_with_prefix():
    output = "\n\n".join(
        [
            "Opening the browser and scrolling.",
            "FINAL REPORT:",
            "A" * 1500,
            "B" * 1500,
            "C" * 1500,
        ]
    )

    message = terminal_callback_message(output)

    assert len(message) <= 4000
    assert message.startswith("A")
    assert message.endswith("...")


def test_terminal_callback_full_message_preserves_long_final_report():
    final_report = "\n\n".join(["A" * 1500, "B" * 1500, "C" * 1500])
    output = f"Progress that should not surface.\nFINAL REPORT:\n{final_report}"

    assert terminal_callback_full_message(output) == final_report


def test_completed_callback_uses_final_report_message(tmp_path, monkeypatch):
    class FinalReportRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = worker, instruction, timeout_sec, run_id
            publish_in_process_test_start(worker)
            return "\n".join(
                [
                    "Opening the browser and scrolling.",
                    "Still collecting rows from the page.",
                    "",
                    "FINAL REPORT:",
                    "Captured 42 rows.",
                    "",
                    "Created `recent-connections.md` and stopped on the target page.",
                ]
            )

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, FinalReportRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Verify final report callbacks", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Browser Worker",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                }
            },
        )

        run = service.assign_run(worker["worker_id"], "Open the browser and extract the result")
        wait_until(
            lambda: any(
                payload.get("event") == "run.completed" and payload.get("run_id") == run["run_id"]
                for payload in payloads
            )
        )

        completed = next(
            payload
            for payload in payloads
            if payload.get("event") == "run.completed" and payload.get("run_id") == run["run_id"]
        )
        assert completed["message"] == (
            "Captured 42 rows.\n\nCreated `recent-connections.md` and stopped on the target page."
        )
        assert completed["full_message"] == ""
        assert "Opening the browser" not in completed["message"]
        assert completed["message_id"] == "msg-assistant"
    finally:
        service.shutdown()


def test_completed_file_callback_adds_signed_open_download_and_watch_links(tmp_path, monkeypatch):
    class FileRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            publish_in_process_test_start(worker)
            info = self.ensure_worker_ready(worker)
            workspace = Path(info.workspace_dir)
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "answer.txt").write_text("signed artifact", encoding="utf-8")
            return "FINAL REPORT:\nArtifact available at: /workspace/project/answer.txt"

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive-api.example.test")
    monkeypatch.setenv("WPR_API_TOKEN", "signed-link-secret")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, FileRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Signed artifact links", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="File Worker",
            role="file worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                    "surface": "web",
                }
            },
        )

        service.assign_run(worker["worker_id"], "Create answer.txt")
        wait_until(
            lambda: any(payload.get("event") == "run.completed" for payload in payloads),
            timeout=5.0,
        )

        completed = next(payload for payload in payloads if payload.get("event") == "run.completed")
        assert completed["deliverable"]["kind"] == "file"
        assert completed["deliverable"]["workspace_path"] == "answer.txt"
        assert "/v1/signed-links/" not in completed["message"]
        assert "gh_token=" not in completed["message"]
        assert "File: [Download file](https://glasshive-api.example.test/v1/link-refs/" in completed["message"]
        assert "Preview: [Open GlassHive file](https://glasshive-api.example.test/v1/link-refs/" in completed["message"]
        assert "View / Steer: [Open GlassHive workspace](https://glasshive-ui.example.test/r/" in completed["message"]
        assert completed["message"].index("File: [Download file]") < completed["message"].index("Preview:")
        assert completed["message"].index("Preview:") < completed["message"].index("View / Steer:")
        artifact_matches = re.findall(
            r"https://glasshive-api\.example\.test/v1/link-refs/([A-Za-z0-9_-]+)",
            completed["message"],
        )
        assert len(artifact_matches) >= 2
        download_record = resolve_signed_link_ref(artifact_matches[0])
        assert download_record is not None
        assert download_record["kind"] == "artifact_download"
        preview_record = resolve_signed_link_ref(artifact_matches[1])
        assert preview_record is not None
        assert preview_record["kind"] == "artifact_open"
        watch_match = re.search(r"https://glasshive-ui\.example\.test/r/([A-Za-z0-9_-]+)", completed["message"])
        assert watch_match
        watch_record = resolve_signed_link_ref(watch_match.group(1))
        assert watch_record is not None
        assert watch_record["kind"] == "worker_view"
        assert "surface=desktop" in watch_record["target_url"]
        assert f"project_id={project['project_id']}" in watch_record["target_url"]
        assert "gh_token=" in watch_record["target_url"]
    finally:
        service.shutdown()


def test_failed_callback_reports_terminal_state_and_view_steer_link_without_local_path(tmp_path, monkeypatch):
    synthetic_home_path = "/" + "/".join(("Users", "example", "private-upload.pdf"))
    synthetic_home_prefix = "/".join(("Users", "example"))

    class FailingRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = worker, instruction, timeout_sec, run_id
            publish_in_process_test_start(worker)
            raise FileNotFoundError(f"Bootstrap source file not found: {synthetic_home_path}")

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")
    monkeypatch.setenv("WPR_API_TOKEN", "signed-link-secret")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, FailingRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Failure callback links", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="File Worker",
            role="file worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                    "surface": "web",
                }
            },
        )

        run = service.assign_run(worker["worker_id"], "Read the uploaded file")
        wait_until(
            lambda: any(
                payload.get("event") == "run.failed" and payload.get("run_id") == run["run_id"]
                for payload in payloads
            )
        )

        failed = next(payload for payload in payloads if payload.get("event") == "run.failed")
        assert failed["run_state"] == "failed"
        assert "Bootstrap source file not found: [local path]" in failed["message"]
        assert synthetic_home_prefix not in failed["message"]
        assert "View / Steer: [Open GlassHive workspace](https://glasshive-ui.example.test/r/" in failed["message"]
        assert "gh_token=" not in failed["message"]
        watch_record = assert_link_ref_url(failed["watch_url"], prefix="https://glasshive-ui.example.test/r/", kind="worker_view")
        assert f"/watch/{worker['worker_id']}" in watch_record["target_url"]
        assert "gh_token=" in watch_record["target_url"]
    finally:
        service.shutdown()


def test_selected_account_overlap_waits_then_runs_same_child(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "0.1")
    db = str(tmp_path / "runtime.db")
    store = Store(db)
    accounts = ControlPlaneStore(db)
    account = accounts.create_provider_account(
        tenant_id="local", owner_id="owner", provider="codex",
        label="Synthetic account", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://auto",
        status="ready",
    )
    service = WorkersProjectsService(store, StubRuntime(), control_plane_store=accounts, max_workers=2)
    try:
        project = store.create_project("owner", "Parallel", "Run one child after another", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"], owner_id="owner", name="Child",
            role="worker", profile="codex-cli", backend="openclaw",
            runtime="codex-cli", model="stub/codex-cli", execution_mode="docker",
            bootstrap_bundle={"provider_account": {
                "policy": "personal_required", "account_id": account["account_id"]}},
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "exact child goal")
        lease = accounts.acquire_provider_lease(
            account_id=account["account_id"], tenant_id="local", owner_id="owner",
            lane="codex-cli:mission", worker_id="other-worker", run_id="other-run",
            ttl_seconds=60,
        )
        service.start_assigned_run(worker["worker_id"])
        wait_until(lambda: (store.get_run(run["run_id"]) or {}).get("failure_class") == "host_capacity", timeout=3)
        waiting = store.get_run(run["run_id"])
        assert waiting["state"] == "queued"
        assert waiting["runtime_invoked_at"] is None
        assert waiting["failure_user_message"] == "The selected AI account is already in use. This work will wait for the account to be free."
        assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "run.failed"]
        accounts.release_provider_lease(
            lease_id=lease["lease_id"], tenant_id="local", owner_id="owner",
        )
        wait_until(lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed", timeout=4)
        completed = store.get_run(run["run_id"])
        assert completed["run_id"] == run["run_id"]
        assert completed["output_text"] == "STUB_OK: exact child goal"
    finally:
        service.shutdown()
        store.close()


def test_account_lease_race_after_queue_check_retries_same_run(tmp_path, monkeypatch):
    class LateBusyRuntime(StubRuntime):
        calls = 0

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            self.calls += 1
            if self.calls == 1:
                raise ProviderAccountBusyError()
            return super().run_task(worker, instruction, timeout_sec, run_id)

    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "0.1")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = LateBusyRuntime()
    service = WorkersProjectsService(store, runtime, max_workers=2)
    try:
        project = store.create_project("owner", "Parallel", "Test late lease race", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"], owner_id="owner", name="Child",
            role="worker", profile="codex-cli", backend="openclaw",
            runtime="codex-cli", model="stub/codex-cli", execution_mode="docker",
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "exact child goal")
        service.start_assigned_run(worker["worker_id"])
        wait_until(lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed", timeout=4)
        completed = store.get_run(run["run_id"])
        assert completed["run_id"] == run["run_id"]
        assert completed["output_text"] == "STUB_OK: exact child goal"
        assert runtime.calls == 2
        assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "run.failed"]
    finally:
        service.shutdown()
        store.close()


def test_retryable_host_busy_waits_and_retries_without_terminal_failure(tmp_path, monkeypatch):
    class CapacityRuntime(StubRuntime):
        def __init__(self) -> None:
            self.busy = True
            self.run_calls = 0

        def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
            if not self.busy:
                return None
            return RuntimeErrorBase(
                "Host-native codex-cli already has an active worker (wrk_busy123456); "
                "v1 allows one active host worker per CLI family."
            )

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = timeout_sec, run_id
            self.run_calls += 1
            publish_in_process_test_start(worker)
            return f"Completed after capacity wait: {instruction}"

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "0.1")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = CapacityRuntime()
    service = WorkersProjectsService(store, runtime, max_workers=2)
    try:
        project = store.create_project("owner", "Capacity", "Wait for host worker capacity.", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Capacity Worker",
            role="worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                }
            },
        )

        run = service.assign_run(worker["worker_id"], "finish when capacity is free")

        wait_until(
            lambda: (
                (store.get_run(run["run_id"]) or {}).get("state") == "queued"
                and (store.get_run(run["run_id"]) or {}).get("failure_class")
                == "host_worker_busy"
                and bool((store.get_run(run["run_id"]) or {}).get("retry_after"))
            )
        )
        waiting = store.get_run(run["run_id"])
        assert waiting["state"] == "queued"
        assert waiting["failure_class"] == "host_worker_busy"
        assert waiting["failure_retryable"] == 1
        assert waiting["retry_after"]
        assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "run.failed"]
        assert not any(payload.get("event") == "run.failed" for payload in payloads)
        wait_until(
            lambda: any(payload.get("event") == "run.waiting_on_capacity" for payload in payloads),
            timeout=3.0,
        )
        waiting_payload = next(payload for payload in payloads if payload.get("event") == "run.waiting_on_capacity")
        assert waiting_payload["run_state"] == "queued"
        assert "wrk_busy" not in waiting_payload["message"]

        runtime.busy = False
        wait_until(lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed", timeout=3.0)

        completed = store.get_run(run["run_id"])
        assert completed["state"] == "completed"
        assert "Completed after capacity wait" in completed["output_text"]
        assert completed["failure_class"] == ""
        assert completed["failure_retryable"] == 0
        assert completed["failure_structured"] == 0
        assert completed["failure_user_message"] == ""
        assert completed["failure_recommended_recovery"] == ""
        assert completed["failure_diagnostic_summary"] == ""
        assert completed["retry_after"] is None
        assert completed["retry_attempts"] == 0
        assert completed["last_retry_class"] == ""
        assert runtime.run_calls == 1
        assert not any(payload.get("event") == "run.failed" for payload in payloads)
        wait_until(
            lambda: any(payload.get("event") == "run.completed" for payload in payloads),
            timeout=3.0,
        )
        completed_payload = next(
            payload for payload in payloads if payload.get("event") == "run.completed"
        )
        assert "failure_code" not in completed_payload
        assert "failure_class" not in completed_payload
        assert "failure_retryable" not in completed_payload
    finally:
        service.shutdown()


def test_completed_run_immediately_wakes_other_host_capacity_waiters(tmp_path, monkeypatch):
    class SharedCapacityRuntime(StubRuntime):
        def __init__(self) -> None:
            self.active_worker_id = ""
            self.active_started = Event()
            self.release_active = Event()
            self.waiting_run_calls = 0

        def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
            if self.active_worker_id and worker["worker_id"] != self.active_worker_id:
                return RuntimeErrorBase(
                    f"Host-native codex-cli already has an active worker ({self.active_worker_id}); "
                    "v1 allows one active host worker per CLI family."
                )
            return None

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = timeout_sec, run_id
            if instruction == "hold the shared slot":
                self.active_worker_id = worker["worker_id"]
                publish_in_process_test_start(worker)
                self.active_started.set()
                assert self.release_active.wait(timeout=2)
                self.active_worker_id = ""
                return "active run completed"
            self.waiting_run_calls += 1
            publish_in_process_test_start(worker)
            return "waiting run completed"

    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = SharedCapacityRuntime()
    service = WorkersProjectsService(
        store,
        runtime,
        max_workers=2,
        reconcile_on_startup=False,
    )
    try:
        project = store.create_project("owner", "Capacity", "Share host capacity.", "codex-cli")

        def worker(name: str) -> dict:
            return store.create_worker(
                project_id=project["project_id"],
                owner_id="owner",
                name=name,
                role="worker",
                profile="codex-cli",
                backend="openclaw",
                runtime="codex-cli",
                model="stub/codex-cli",
                execution_mode="host",
                bootstrap_bundle={"run_mode": "conversation"},
            )

        active_worker = worker("Active worker")
        waiting_worker = worker("Waiting worker")
        active_run = service.assign_run(active_worker["worker_id"], "hold the shared slot")
        assert runtime.active_started.wait(timeout=2)
        waiting_run = service.assign_run(waiting_worker["worker_id"], "use the released slot")

        wait_until(
            lambda: (store.get_run(waiting_run["run_id"]) or {}).get("failure_class")
            == "host_worker_busy"
        )
        blocked = store.get_run(waiting_run["run_id"])
        assert blocked["state"] == "queued"
        assert blocked["retry_after"]

        runtime.release_active.set()
        wait_until(
            lambda: (store.get_run(active_run["run_id"]) or {}).get("state") == "completed"
        )
        wait_until(
            lambda: (store.get_run(waiting_run["run_id"]) or {}).get("state") == "completed"
        )

        completed = store.get_run(waiting_run["run_id"])
        assert completed["retry_attempts"] == 0
        assert completed["last_retry_class"] == ""
        assert runtime.waiting_run_calls == 1
    finally:
        runtime.release_active.set()
        service.shutdown()


def test_release_host_capacity_waiters_filters_to_the_released_structural_lane(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))

    def create_waiter(*, profile: str, execution_mode: str, run_mode: str) -> tuple[dict, dict]:
        project = store.create_project(
            "owner",
            f"{profile}-{execution_mode}-{run_mode}",
            "Wait on one structural host lane.",
            profile,
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name=f"{profile}-{execution_mode}-{run_mode}",
            role="worker",
            profile=profile,
            backend="openclaw",
            runtime=profile,
            model=f"stub/{profile}",
            execution_mode=execution_mode,
            bootstrap_bundle={"run_mode": run_mode},
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "wait", state="queued")
        store.update_run(
            run["run_id"],
            failure_class="host_worker_busy",
            retry_after="2099-01-01T00:00:00+00:00",
        )
        return worker, run

    matching_worker, matching_run = create_waiter(
        profile="codex-cli", execution_mode="host", run_mode="conversation"
    )
    second_matching_worker, second_matching_run = create_waiter(
        profile="codex-cli", execution_mode="host", run_mode="conversation"
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE runs SET queued_at = ? WHERE run_id = ?",
            ("2026-08-01T10:00:00+00:00", matching_run["run_id"]),
        )
        conn.execute(
            "UPDATE runs SET queued_at = ? WHERE run_id = ?",
            ("2026-08-01T10:01:00+00:00", second_matching_run["run_id"]),
        )
    _, mission_run = create_waiter(
        profile="codex-cli", execution_mode="host", run_mode="mission"
    )
    _, claude_run = create_waiter(
        profile="claude-code", execution_mode="host", run_mode="conversation"
    )
    _, docker_run = create_waiter(
        profile="codex-cli", execution_mode="docker", run_mode="conversation"
    )

    released = store.release_host_capacity_waiters(
        profile="codex-cli",
        execution_mode="host",
        run_mode="conversation",
    )

    assert released == [matching_worker["worker_id"]]
    assert store.get_run(matching_run["run_id"])["retry_after"] is None
    assert store.get_run(second_matching_run["run_id"])["retry_after"] is not None
    assert second_matching_worker["worker_id"] not in released
    assert store.get_run(mission_run["run_id"])["retry_after"] is not None
    assert store.get_run(claude_run["run_id"])["retry_after"] is not None
    assert store.get_run(docker_run["run_id"])["retry_after"] is not None


def test_host_capacity_waiters_remain_delayed_while_the_lane_is_still_held(tmp_path):
    class BusyRuntime(StubRuntime):
        active_worker_id = ""

        def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
            if worker["worker_id"] != self.active_worker_id:
                return RuntimeErrorBase(
                    "Host-native codex-cli still has an active conversation worker."
                )
            return None

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        BusyRuntime(),
        max_workers=2,
        reconcile_on_startup=False,
    )
    try:
        project = store.create_project("owner", "Held Lane", "Keep the retry delay.", "codex-cli")
        released_worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Released worker",
            role="worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            execution_mode="host",
            bootstrap_bundle={"run_mode": "conversation"},
        )
        waiting_worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Waiting worker",
            role="worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            execution_mode="host",
            bootstrap_bundle={"run_mode": "conversation"},
        )
        service.runtime.active_worker_id = released_worker["worker_id"]
        waiting_run = store.create_run(
            waiting_worker["worker_id"],
            project["project_id"],
            "wait",
            state="queued",
        )
        retry_after = "2099-01-01T00:00:00+00:00"
        store.update_run(
            waiting_run["run_id"],
            failure_class="host_worker_busy",
            retry_after=retry_after,
        )
        awakened: list[str] = []
        service._ensure_worker_processor = awakened.append  # type: ignore[method-assign]

        service._wake_host_capacity_waiters(released_worker)

        assert store.get_run(waiting_run["run_id"])["retry_after"] == retry_after
        assert awakened == []
    finally:
        service.shutdown()


def test_failed_host_run_immediately_wakes_its_capacity_lane(tmp_path, monkeypatch):
    class SharedCapacityRuntime(StubRuntime):
        def __init__(self) -> None:
            self.active_worker_id = ""
            self.active_started = Event()
            self.release_active = Event()

        def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
            if self.active_worker_id and worker["worker_id"] != self.active_worker_id:
                return RuntimeErrorBase("Host-native codex-cli already has an active worker.")
            return None

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = timeout_sec, run_id
            if instruction == "fail after releasing the shared slot":
                self.active_worker_id = worker["worker_id"]
                publish_in_process_test_start(worker)
                self.active_started.set()
                assert self.release_active.wait(timeout=2)
                self.active_worker_id = ""
                raise RuntimeError("synthetic terminal failure")
            publish_in_process_test_start(worker)
            return "waiting run completed"

    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = SharedCapacityRuntime()
    service = WorkersProjectsService(store, runtime, max_workers=2, reconcile_on_startup=False)
    try:
        project = store.create_project("owner", "Failure Handoff", "Release after failure.", "codex-cli")

        def worker(name: str) -> dict:
            return store.create_worker(
                project_id=project["project_id"],
                owner_id="owner",
                name=name,
                role="worker",
                profile="codex-cli",
                backend="openclaw",
                runtime="codex-cli",
                model="stub/codex-cli",
                execution_mode="host",
                bootstrap_bundle={"run_mode": "conversation"},
            )

        active_worker = worker("Failing worker")
        waiting_worker = worker("Waiting worker")
        active_run = service.assign_run(
            active_worker["worker_id"], "fail after releasing the shared slot"
        )
        assert runtime.active_started.wait(timeout=2)
        waiting_run = service.assign_run(waiting_worker["worker_id"], "use the released slot")
        wait_until(
            lambda: (store.get_run(waiting_run["run_id"]) or {}).get("failure_class")
            == "host_worker_busy"
        )

        runtime.release_active.set()
        wait_until(lambda: (store.get_run(active_run["run_id"]) or {}).get("state") == "failed")
        wait_until(
            lambda: (store.get_run(waiting_run["run_id"]) or {}).get("retry_after") is None,
            timeout=2.0,
        )
        wait_until(
            lambda: (store.get_run(waiting_run["run_id"]) or {}).get("state") == "completed",
            timeout=5.0,
        )
    finally:
        runtime.release_active.set()
        service.shutdown()


@pytest.mark.parametrize("operation", ["pause", "interrupt", "cancel", "terminate"])
def test_host_control_terminal_paths_wake_the_released_capacity_lane(
    tmp_path,
    operation,
):
    class ControlRuntime(StubRuntime):
        def __init__(self) -> None:
            self.active = True
            self.active_worker_id = ""

        def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
            if self.active and worker["worker_id"] != self.active_worker_id:
                return RuntimeErrorBase("Host-native codex-cli already has an active worker.")
            return None

        def _released_info(self, worker: dict) -> RuntimeInfo:
            self.active = False
            return RuntimeInfo(
                runtime="codex-cli",
                model=str(worker.get("model") or "stub/codex-cli"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=None,
                state_dir="",
                workspace_dir="",
                pid=None,
            )

        def pause_worker(self, worker: dict) -> RuntimeInfo:
            return self._released_info(worker)

        def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
            _ = run_id
            return self._released_info(worker)

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            return self._released_info(worker)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = ControlRuntime()
    service = WorkersProjectsService(store, runtime, max_workers=2, reconcile_on_startup=False)
    try:
        project = store.create_project("owner", "Control Handoff", "Release on control.", "codex-cli")

        def worker(name: str) -> dict:
            return store.create_worker(
                project_id=project["project_id"],
                owner_id="owner",
                name=name,
                role="worker",
                profile="codex-cli",
                backend="openclaw",
                runtime="codex-cli",
                model="stub/codex-cli",
                execution_mode="host",
                bootstrap_bundle={"run_mode": "conversation"},
            )

        active_worker = worker("Controlled worker")
        waiting_worker = worker("Waiting worker")
        runtime.active_worker_id = active_worker["worker_id"]
        active_run = create_truthfully_invoked_test_run(
            store,
            active_worker,
            project["project_id"],
            "active",
            suffix=f"control-{operation}",
            executor_id=service._executor_id,
        )
        waiting_run = store.create_run(
            waiting_worker["worker_id"],
            project["project_id"],
            "waiting",
            state="queued",
        )
        store.update_run(
            waiting_run["run_id"],
            failure_class="host_worker_busy",
            retry_after="2099-01-01T00:00:00+00:00",
        )
        awakened: list[str] = []
        service._ensure_worker_processor = awakened.append  # type: ignore[method-assign]

        if operation == "pause":
            service.pause_worker(active_worker["worker_id"])
        elif operation == "interrupt":
            service.interrupt_worker(active_worker["worker_id"])
        elif operation == "cancel":
            service.cancel_run(active_worker["worker_id"], active_run["run_id"])
        else:
            service.terminate_worker(active_worker["worker_id"])

        assert store.get_run(waiting_run["run_id"])["retry_after"] is None
        assert awakened == [waiting_worker["worker_id"]]
    finally:
        service.shutdown()


def test_recovered_terminal_run_wakes_the_released_host_capacity_lane(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2, reconcile_on_startup=False)
    try:
        project = store.create_project("owner", "Recovered Handoff", "Release after recovery.", "codex-cli")

        def worker(name: str) -> dict:
            return store.create_worker(
                project_id=project["project_id"],
                owner_id="owner",
                name=name,
                role="worker",
                profile="codex-cli",
                backend="openclaw",
                runtime="codex-cli",
                model="stub/codex-cli",
                execution_mode="host",
                bootstrap_bundle={"run_mode": "conversation"},
            )

        recovered_worker = worker("Recovered worker")
        waiting_worker = worker("Waiting worker")
        recovered_run = create_truthfully_invoked_test_run(
            store,
            recovered_worker,
            project["project_id"],
            "recover",
            suffix="recovered-terminal",
            executor_id=service._executor_id,
        )
        waiting_run = store.create_run(
            waiting_worker["worker_id"],
            project["project_id"],
            "wait",
            state="queued",
        )
        store.update_run(
            waiting_run["run_id"],
            failure_class="host_worker_busy",
            retry_after="2099-01-01T00:00:00+00:00",
        )
        awakened: list[str] = []
        service._ensure_worker_processor = awakened.append  # type: ignore[method-assign]

        service._apply_recovered_run(
            recovered_worker,
            recovered_run,
            {"state": "completed", "output_text": "recovered output", "usage": {}},
        )

        assert store.get_run(waiting_run["run_id"])["retry_after"] is None
        assert awakened == [waiting_worker["worker_id"]]
    finally:
        service.shutdown()


def test_host_busy_retry_policy_has_short_cadence_and_an_independent_long_budget(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_BUSY_RETRY_MAX_DELAY_S", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_BUSY_MAX_RETRY_ATTEMPTS", raising=False)
    monkeypatch.setenv("GLASSHIVE_RETRY_MAX_DELAY_S", "300")
    monkeypatch.setenv("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "6")

    service = WorkersProjectsService(Store(str(tmp_path / "runtime.db")), StubRuntime(), max_workers=2)
    try:
        assert [service._retry_delay_s("host_worker_busy", attempt) for attempt in (1, 2, 3, 10)] == [
            5.0,
            10.0,
            15.0,
            15.0,
        ]
        assert service._capacity_retry_max_attempts("host_worker_busy") == 240

        assert service._retry_delay_s("runtime_retryable", 10) == 300.0
        assert service._capacity_retry_max_attempts("runtime_retryable") == 6
    finally:
        service.shutdown()


def test_three_host_workers_handoff_one_cli_lane_without_using_generic_retry_budget(
    tmp_path,
    monkeypatch,
):
    class SingleLaneRuntime(StubRuntime):
        def __init__(self) -> None:
            self._lock = Lock()
            self._active = False
            self.active_count = 0
            self.max_active_count = 0
            self.completed_workers: list[str] = []

        def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
            _ = worker
            with self._lock:
                if not self._active:
                    return None
            return RuntimeErrorBase("Host-native codex-cli already has an active worker.")

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            with self._lock:
                if self._active:
                    raise RuntimeErrorBase("Host-native codex-cli already has an active worker.")
                self._active = True
                self.active_count += 1
                self.max_active_count = max(self.max_active_count, self.active_count)
            try:
                publish_in_process_test_start(worker)
                time.sleep(0.22)
                self.completed_workers.append(str(worker["worker_id"]))
                return f"completed {worker['worker_id']}"
            finally:
                with self._lock:
                    self.active_count -= 1
                    self._active = False

    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "0.1")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_MAX_DELAY_S", "0.1")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_MAX_RETRY_ATTEMPTS", "20")
    monkeypatch.setenv("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "1")

    store = Store(str(tmp_path / "runtime.db"))
    runtime = SingleLaneRuntime()
    service = WorkersProjectsService(store, runtime, max_workers=3)
    try:
        project = store.create_project("owner", "Lane Handoff", "Serialize one CLI lane.", "codex-cli")
        runs: list[dict] = []
        for index in range(3):
            worker = store.create_worker(
                project_id=project["project_id"],
                owner_id="owner",
                name=f"Queued Cortex {index + 1}",
                role="worker",
                profile="codex-cli",
                backend="openclaw",
                runtime="codex-cli",
                model="stub/codex-cli",
            )
            runs.append(service.assign_run(worker["worker_id"], f"run cortex {index + 1}"))

        run_ids = [str(run["run_id"]) for run in runs]
        wait_until(
            lambda: all((store.get_run(run_id) or {}).get("state") == "completed" for run_id in run_ids),
            timeout=5.0,
        )

        settled = [store.get_run(run_id) for run_id in run_ids]
        assert runtime.max_active_count == 1
        assert len(runtime.completed_workers) == 3
        assert all(run and run["state"] == "completed" for run in settled)
        assert all(int((run or {}).get("retry_attempts") or 0) == 0 for run in settled)
    finally:
        service.shutdown()


def test_future_capacity_retry_does_not_hot_resubmit_processor(
    tmp_path, background_consumers_disabled
):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Future Retry", "Wait without spinning.", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Future Retry Worker",
            role="worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "wait for the retry timestamp",
            state="queued",
        )
        retry_after = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        store.update_run(run["run_id"], retry_after=retry_after)

        generation = 1
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation
        scheduled: list[tuple[str, str | None]] = []
        immediate_restarts: list[str] = []
        service._schedule_worker_retry_after = (  # type: ignore[method-assign]
            lambda worker_id, value: scheduled.append((worker_id, value))
        )
        service._ensure_worker_processor = (  # type: ignore[method-assign]
            lambda worker_id: immediate_restarts.append(worker_id)
        )

        service._process_worker_queue(worker["worker_id"], generation)

        assert scheduled == [(worker["worker_id"], retry_after)]
        assert immediate_restarts == []
        assert store.get_run(run["run_id"])["state"] == "queued"
    finally:
        service.shutdown()


def test_overdue_capacity_retry_keeps_scheduler_eligible(
    tmp_path, background_consumers_disabled
):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project(
            "owner", "Overdue Retry", "Retry after a missed wake.", "codex-cli"
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Overdue Retry Worker",
            role="worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "retry overdue work",
            state="queued",
        )
        store.update_run(
            run["run_id"],
            retry_after=(
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        )

        assert service._next_scheduler_wait_s(5.0) == 0.01
    finally:
        service.shutdown()


def test_service_startup_resumes_eligible_queued_run(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Startup Queue", "Resume queued work.", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Startup Queue Worker",
        role="worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "resume after service restart",
        state="queued",
    )

    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        wait_until(
            lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed",
            timeout=1.0,
        )
    finally:
        service.shutdown()


def test_service_startup_resumes_queued_host_worker_without_persistent_process(tmp_path):
    class ProcessPerRunHostRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            return RuntimeInfo(
                runtime="codex-cli",
                model="stub/codex-cli",
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=worker.get("session_key"),
                state_dir=str(tmp_path / "state"),
                workspace_dir=str(tmp_path / "workspace"),
                pid=None,
            )

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Host Startup Queue", "Resume host work.", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Host Startup Queue Worker",
        role="worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
        execution_mode="host",
    )
    store.update_worker_state(worker["worker_id"], "ready")
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "resume process-per-run host work",
        state="queued",
    )

    service = WorkersProjectsService(store, ProcessPerRunHostRuntime(), max_workers=2)
    try:
        wait_until(
            lambda: (
                (store.get_run(run["run_id"]) or {}).get("state") == "completed"
                and (store.get_worker(worker["worker_id"]) or {}).get("state") == "ready"
            ),
            timeout=5.0,
        )
        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "ready"
    finally:
        service.shutdown()


def test_retryable_capacity_wait_does_not_consume_generic_retry_budget(
    tmp_path, monkeypatch
):
    class AlwaysBusyRuntime(StubRuntime):
        capacity_checks = 0

        def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
            _ = worker
            self.capacity_checks += 1
            return HostCapacityError(
                "Host mission lane is full.",
                capacity_class="family_lane",
            )

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = worker, instruction, timeout_sec, run_id
            raise AssertionError("capacity-blocked run should not start task execution")

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "0.1")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_MAX_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = AlwaysBusyRuntime()
    service = WorkersProjectsService(store, runtime, max_workers=2)
    try:
        project = store.create_project("owner", "Capacity Cap", "Bound retry loops.", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Capacity Cap Worker",
            role="worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            execution_mode="host",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                }
            },
        )

        run = service.assign_run(worker["worker_id"], "wait until capacity is free")

        wait_until(
            lambda: (
                (store.get_run(run["run_id"]) or {}).get("state") == "queued"
                and (store.get_run(run["run_id"]) or {}).get("failure_class")
                == "host_capacity"
                and bool((store.get_run(run["run_id"]) or {}).get("retry_after"))
            ),
            timeout=3.0,
        )
        waiting = store.get_run(run["run_id"])
        assert waiting["retry_attempts"] == 0
        assert waiting["capacity_retry_count"] >= 1
        assert waiting["failure_class"] == "host_capacity"
        assert waiting["failure_retryable"] == 1
        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "ready"
        wait_until(
            lambda: any(
                payload.get("event") == "run.waiting_on_capacity"
                for payload in payloads
            ),
            timeout=3.0,
        )
        assert len(
            [
                payload
                for payload in payloads
                if payload.get("event") == "run.waiting_on_capacity"
            ]
        ) == 1
        assert not any(payload.get("event") == "run.failed" for payload in payloads)
    finally:
        service.shutdown()


def test_public_callback_message_redacts_glasshive_ids():
    message = (
        "Host-native codex-cli already has an active worker (wrk_busy123456); "
        "retrying run_deadbeef123 for project prj_private7890."
    )

    redacted = public_callback_message_text(message)

    assert "wrk_busy123456" not in redacted
    assert "run_deadbeef123" not in redacted
    assert "prj_private7890" not in redacted
    assert redacted.count("[glasshive-id]") == 3


def test_project_worker_lifecycle_with_stub_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RELEASE_ID", "release-public-safe")
    monkeypatch.setenv("GLASSHIVE_PARENT_REVISION", "a" * 40)
    monkeypatch.setenv("GLASSHIVE_COMPONENT_REVISION", "b" * 40)
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["release"] == {
        "release_id": "release-public-safe",
        "parent_revision": "a" * 40,
        "glasshive_revision": "b" * 40,
    }
    assert health.json()["runtime_backend"] == "stub"
    assert health.json()["default_worker_profile"]

    project_resp = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Project Alpha",
            "goal": "Validate the standalone OpenClaw worker control plane.",
            "default_worker_profile": "openclaw-general",
        },
    )
    assert project_resp.status_code == 201
    project = project_resp.json()
    project_id = project["project_id"]

    worker_resp = client.post(
        f"/v1/projects/{project_id}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Research Worker",
            "role": "research",
            "profile": "openclaw-general",
            "backend": "openclaw",
            "bootstrap_profile": "host-login",
            "bootstrap_bundle": {
                "system_instructions": "Follow the project goal and keep operator checkpoints explicit.",
            },
        },
    )
    assert worker_resp.status_code == 201
    worker = worker_resp.json()
    worker_id = worker["worker_id"]
    assert worker["state"] == "ready"
    assert worker["runtime"] == "openclaw-stub"
    assert worker["execution_mode"] == "docker"
    assert worker["session_key"].endswith(worker_id)
    assert worker["bootstrap_profile"] == "host-login"

    assign_resp = client.post(
        f"/v1/workers/{worker_id}/assign",
        json={"instruction": "Research the best path for workers and projects."},
    )
    assert assign_resp.status_code == 202
    run = assign_resp.json()
    settled = wait_for_run(client, run["run_id"])
    assert settled["state"] == "completed"
    assert "STUB_OK" in settled["output_text"]

    pause_resp = client.post(f"/v1/workers/{worker_id}/pause")
    assert pause_resp.status_code == 202
    assert pause_resp.json()["state"] == "paused"

    resume_resp = client.post(f"/v1/workers/{worker_id}/resume")
    assert resume_resp.status_code == 202
    assert resume_resp.json()["state"] == "ready"

    message_resp = client.post(
        f"/v1/workers/{worker_id}/message",
        json={"message": "Shift focus to Codex and Claude worker design details."},
    )
    assert message_resp.status_code == 202
    message_run = wait_for_run(client, message_resp.json()["run_id"])
    assert message_run["state"] == "completed"
    assert "Preserve trusted requirements" in message_run["instruction"]
    assert json.loads(message_run["continuation_context_json"]) == {
        "version": 1,
        "base_instruction": "Research the best path for workers and projects.",
        "guidance": [
            "Shift focus to Codex and Claude worker design details."
        ],
    }
    assert "Research the best path for workers and projects." in message_run["instruction"]
    assert "Shift focus to Codex and Claude worker design details." in message_run["instruction"]
    events_resp = client.get(f"/v1/workers/{worker_id}/events")
    assert events_resp.status_code == 200
    assert len(events_resp.json()["items"]) >= 6

    terminate_resp = client.post(f"/v1/workers/{worker_id}/terminate")
    assert terminate_resp.status_code == 202
    assert terminate_resp.json()["state"] == "terminated"

    metrics_resp = client.get("/v1/metrics/summary")
    assert metrics_resp.status_code == 200
    metrics = metrics_resp.json()
    assert metrics["projects"] == 1
    assert metrics["workers"] == 1
    assert metrics["runs"] == 2
    assert metrics["queued_runs"] == 0
    assert metrics["events"] >= 7


def test_assign_idempotency_key_reuses_one_durable_run(tmp_path):
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "synthetic-owner", "title": "Synthetic", "goal": "Test idempotency"},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "synthetic-owner",
            "name": "Synthetic Worker",
            "role": "general",
            "profile": "openclaw-general",
            "backend": "openclaw",
        },
    ).json()
    headers = {"x-glasshive-idempotency-key": "scheduled-occurrence-synthetic"}
    first = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        headers=headers,
        json={"instruction": "Perform the synthetic scheduled task."},
    )
    duplicate = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        headers=headers,
        json={"instruction": "Perform the synthetic scheduled task."},
    )
    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    runs = client.get(f"/v1/workers/{worker['worker_id']}/runs").json()["items"]
    assert [run["run_id"] for run in runs].count(first.json()["run_id"]) == 1


def test_api_uses_configured_default_worker_profile_when_project_omits_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "codex-cli")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))

    health = client.get("/health").json()
    assert health["default_worker_profile"] == "codex-cli"
    assert "codex-cli" in health["allowed_worker_profiles"]

    project_resp = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Configured Default",
            "goal": "Use the deployment default worker profile.",
        },
    )

    assert project_resp.status_code == 201
    assert project_resp.json()["default_worker_profile"] == "codex-cli"


def test_health_reports_default_profile_runtime_instead_of_legacy_openclaw(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "codex-cli")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="openclaw", runtime=StubRuntime()))

    health = client.get("/health").json()

    assert health["runtime_backend"] == "codex-cli"
    assert "runtime_backend_plumbing" not in health
    assert health["default_worker_profile"] == "codex-cli"


def test_health_rejects_unreadable_runtime_schema_without_database_detail(tmp_path):
    app = create_app(str(tmp_path / "unavailable.db"), runtime_backend="stub")
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    with app.state.store._connect() as connection:
        connection.execute("DROP TABLE glasshive_schema_versions")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "Runtime data store is unavailable"}


def test_builtin_project_ui_defaults_new_worker_to_project_default_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "codex-cli")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = Store(str(db_path)).create_project(
        "demo-owner",
        "Default UI Worker",
        "New workers should use the configured default profile when legacy project metadata is blank.",
        "",
    )

    response = client.get(f"/ui/projects/{project['project_id']}")

    assert response.status_code == 200
    html = response.text
    assert "<option value='codex-cli' selected>Codex CLI</option>" in html
    assert "<option value='openclaw-general' selected>" not in html
    assert "OpenClaw" not in html


def test_builtin_home_ui_hides_legacy_openclaw_profile_choice_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "codex-cli")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))

    response = client.get("/ui")

    assert response.status_code == 200
    html = response.text
    assert "<option value='codex-cli' selected>Codex CLI</option>" in html
    assert "OpenClaw" not in html


def test_builtin_home_ui_can_show_legacy_openclaw_profile_choice_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "codex-cli")
    monkeypatch.setenv("GLASSHIVE_UI_SHOW_LEGACY_OPENCLAW_PROFILE", "true")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))

    response = client.get("/ui")

    assert response.status_code == 200
    html = response.text
    assert "<option value='codex-cli' selected>Codex CLI</option>" in html
    assert "<option value='openclaw-general'>OpenClaw</option>" in html


def test_builtin_home_ui_keeps_openclaw_visible_when_it_is_selected_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "openclaw-general")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))

    response = client.get("/ui")

    assert response.status_code == 200
    assert "<option value='openclaw-general' selected>OpenClaw</option>" in response.text


def test_create_worker_omitted_profile_uses_project_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_EXECUTION_MODE", "host")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Worker Default",
            "goal": "Workers inherit the project default.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    response = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Default Worker",
            "role": "coding",
            "backend": "openclaw",
            "start_synchronously": False,
        },
    )

    assert response.status_code == 201
    worker_payload = response.json()
    assert worker_payload["profile"] == "codex-cli"
    assert worker_payload["runtime"] == "codex-cli"
    assert worker_payload["backend"] == "codex-cli"
    assert worker_payload["execution_mode"] == "host"
    fetched = client.get(f"/v1/workers/{worker_payload['worker_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["backend"] == "codex-cli"


def test_enterprise_mode_scopes_projects_workers_and_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    headers_a = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Email": "a@example.com",
        "X-Viventium-User-Role": "member",
    }
    headers_b = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-b",
        "X-Viventium-User-Email": "b@example.com",
        "X-Viventium-User-Role": "member",
    }
    generic_headers = {
        "X-GlassHive-Service-Token": "service-secret",
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "generic-user",
        "X-GlassHive-User-Email": "generic@example.com",
        "X-GlassHive-User-Role": "member",
    }

    assert client.get("/docs").status_code == 401
    assert client.get("/redoc").status_code == 401
    assert client.get("/runs").status_code == 401
    assert client.get("/v1/projects", headers={"X-WPR-Token": "service-secret"}).status_code == 401
    assert client.get("/v1/projects", headers=generic_headers).status_code == 200
    mismatched_tenant = {**headers_a, "X-Viventium-Tenant-Id": "tenant-beta"}
    assert client.get("/v1/projects", headers=mismatched_tenant).status_code == 401

    project = client.post(
        "/v1/projects",
        headers=headers_a,
        json={
            "owner_id": "spoofed-owner",
            "title": "Enterprise Project",
            "goal": "Keep user work isolated.",
            "default_worker_profile": "openclaw-general",
        },
    )
    assert project.status_code == 201
    project_payload = project.json()
    assert project_payload["tenant_id"] == "tenant-alpha"
    assert project_payload["owner_id"] == "user-a"

    worker_payload = {
        "owner_id": "spoofed-owner",
        "name": "Shared Alias",
        "role": "research",
        "profile": "openclaw-general",
        "backend": "openclaw",
        "alias": "daily-browser",
    }
    worker_a = client.post(
        f"/v1/projects/{project_payload['project_id']}/workers/find-or-resume",
        headers=headers_a,
        json=worker_payload,
    )
    assert worker_a.status_code == 200
    worker_a_payload = worker_a.json()
    assert worker_a_payload["owner_id"] == "user-a"
    assert worker_a_payload["tenant_id"] == "tenant-alpha"
    assert worker_a_payload["alias"].startswith("tenant-alpha--user-a--")

    workspace_file = Path(worker_a_payload["workspace_dir"]) / "result.txt"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("user-a result", encoding="utf-8")
    hidden_config = Path(worker_a_payload["workspace_dir"]) / ".codex" / "config.toml"
    hidden_config.parent.mkdir(parents=True, exist_ok=True)
    hidden_config.write_text("[mcp_servers]\n", encoding="utf-8")
    listed = client.get(f"/v1/workers/{worker_a_payload['worker_id']}/artifacts", headers=headers_a)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["path"] == "result.txt"
    assert all(item["path"] != ".codex/config.toml" for item in listed.json()["items"])
    downloaded = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        headers=headers_a,
        params={"path": "result.txt"},
    )
    assert downloaded.status_code == 200
    assert downloaded.text == "user-a result"
    bare_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        params={"path": "result.txt"},
    )
    assert bare_download.status_code == 401
    signed = sign_link_params(
        kind="artifact_download",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
    )
    signed_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        params={"path": "result.txt", **signed},
    )
    assert signed_download.status_code == 200
    assert signed_download.text == "user-a result"
    signed_open = sign_link_params(
        kind="artifact_open",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
    )
    signed_open_page = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/open",
        params={"path": "result.txt", **signed_open},
    )
    assert signed_open_page.status_code == 200
    assert "user-a result" in signed_open_page.text
    cross_kind_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        params={"path": "result.txt", **signed_open},
    )
    assert cross_kind_download.status_code == 401
    cross_kind_open = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/open",
        params={"path": "result.txt", **signed},
    )
    assert cross_kind_open.status_code == 401
    signed_token = sign_link_token(
        kind="artifact_download",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
    )
    opaque_download = client.get(f"/v1/signed-links/{signed_token}")
    assert opaque_download.status_code == 200
    assert opaque_download.text == "user-a result"
    watch_token = sign_link_token(
        kind="worker_view",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    opaque_watch = client.get(f"/v1/signed-links/{watch_token}", follow_redirects=False)
    assert opaque_watch.status_code == 400
    signed_watch = sign_link_params(
        kind="worker_view",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    signed_watch_query = urlencode(signed_watch)
    signed_live = client.get(f"/v1/workers/{worker_a_payload['worker_id']}/live?{signed_watch_query}")
    assert signed_live.status_code == 200
    assert signed_live.json()["worker"]["owner_id"] == "user-a"
    signed_pause = client.post(f"/v1/workers/{worker_a_payload['worker_id']}/pause?{signed_watch_query}")
    assert signed_pause.status_code in {401, 403}
    forged_signed_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        params={"path": "../runtime.db", **signed},
    )
    assert forged_signed_download.status_code == 401
    traversal = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        headers=headers_a,
        params={"path": "../runtime.db"},
    )
    assert traversal.status_code == 400
    hidden_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        headers=headers_a,
        params={"path": ".codex/config.toml"},
    )
    assert hidden_download.status_code == 400
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    symlink_path = Path(worker_a_payload["workspace_dir"]) / "outside-link.txt"
    symlink_path.symlink_to(outside_file)
    symlink_escape = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        headers=headers_a,
        params={"path": "outside-link.txt"},
    )
    assert symlink_escape.status_code == 400

    assert client.get("/v1/projects", headers=headers_b).json()["items"] == []
    assert client.get(f"/v1/projects/{project_payload['project_id']}", headers=headers_b).status_code == 404
    assert client.get(f"/v1/workers/{worker_a_payload['worker_id']}", headers=headers_b).status_code == 404
    assert client.get(f"/v1/workers/{worker_a_payload['worker_id']}/artifacts", headers=headers_b).status_code == 404
    assert client.get("/v1/metrics/summary", headers=headers_a).json()["workers"] == 1
    assert client.get("/v1/metrics/summary", headers=headers_b).json()["workers"] == 0
    assert "metrics" not in client.get("/health").json()
    with pytest.raises(WebSocketDisconnect) as missing_token:
        with client.websocket_connect(f"/ws/workers/{worker_a_payload['worker_id']}/terminal"):
            pass
    assert missing_token.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as wrong_user:
        with client.websocket_connect(f"/ws/workers/{worker_a_payload['worker_id']}/terminal", headers=headers_b):
            pass
    assert wrong_user.value.code == 4404


def test_enterprise_opaque_signed_links_reject_tamper_expiry_and_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Signed Links", "goal": "Protect links."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Signed Link Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    workspace_file = Path(worker["workspace_dir"]) / "result.txt"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("signed result", encoding="utf-8")

    valid = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
    )
    assert client.get(f"/v1/signed-links/{valid}").status_code == 200
    ref_id = create_signed_link_ref(token=valid)
    assert ref_id
    assert client.get(f"/v1/link-refs/{ref_id}").status_code == 401
    ref_response = client.get(f"/v1/link-refs/{ref_id}", headers=headers)
    assert ref_response.status_code == 200
    assert "signed result" in ref_response.text
    assert client.get(f"/v1/link-refs/{valid}").status_code == 401

    view_token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    target_url = f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?surface=desktop&gh_token={view_token}"
    view_ref_id = create_signed_link_ref(token=view_token, target_url=target_url)
    assert view_ref_id
    wrong_owner_headers = {**headers, "X-Viventium-User-Id": "user-b"}
    assert client.get(f"/r/{view_ref_id}", headers=wrong_owner_headers, follow_redirects=False).status_code == 404
    direct_redirect = client.get(f"/r/{view_ref_id}", follow_redirects=False)
    assert direct_redirect.status_code == 401
    assert "authenticated user assertion" in direct_redirect.text
    redirect = client.get(f"/r/{view_ref_id}", headers=headers, follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?surface=desktop"
    assert "gh_token=" not in redirect.headers["location"]
    set_cookie = redirect.headers["set-cookie"]
    cookie_name = f"glasshive_gh_token_{hashlib.sha256(worker['worker_id'].encode('utf-8')).hexdigest()[:24]}"
    assert f"{cookie_name}=" in set_cookie
    assert f"glasshive_gh_token_{worker['worker_id']}=" not in set_cookie
    assert "HttpOnly" in set_cookie
    events = client.get(f"/v1/workers/{worker['worker_id']}/live", headers=headers).json()["events"]
    assert any(event["event_type"] == "worker.view_opened" for event in events)
    assert not any(event["event_type"] == "worker.resumed" for event in events)

    tampered = f"{valid[:-1]}{'0' if valid[-1] != '0' else '1'}"
    assert client.get(f"/v1/signed-links/{tampered}").status_code == 401

    expired = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
        ttl_seconds=-10,
    )
    assert client.get(f"/v1/signed-links/{expired}").status_code == 401


def test_enterprise_short_worker_view_ref_stays_read_only_when_legacy_auto_resume_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_WORKSPACE_LINK_AUTO_RESUME", "true")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-deployment-provider")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Auto Resume", "goal": "Resume from link."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Signed Link Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause", headers=headers)
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"

    view_token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    target_url = f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?surface=desktop&gh_token={view_token}"
    view_ref_id = create_signed_link_ref(token=view_token, target_url=target_url)

    redirect = client.get(f"/r/{view_ref_id}", headers=headers, follow_redirects=False)

    assert redirect.status_code == 307
    assert redirect.headers["location"] == f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?surface=desktop"
    assert "gh_token=" not in redirect.headers["location"]
    live = client.get(f"/v1/workers/{worker['worker_id']}/live", headers=headers).json()
    assert live["worker"]["state"] == "paused"
    assert any(event["event_type"] == "worker.view_opened" for event in live["events"])
    assert not any(event["event_type"] == "worker.resumed" for event in live["events"])


def test_link_refs_are_redacted_deduplicated_and_secret_rotation_bound(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    now = {"value": 1_000}
    monkeypatch.setattr(sign_link_token.__globals__["time"], "time", lambda: now["value"])

    token = sign_link_token(
        kind="artifact_download",
        worker_id="wrk_dedupe",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="outputs/report.md",
    )
    ref_id = create_signed_link_ref(token=token)
    assert ref_id.startswith("ghr_")
    assert f"/r/{ref_id}" not in redact_sensitive_url_text(f"GET /r/{ref_id}")
    assert f"/v1/link-refs/{ref_id}" not in redact_sensitive_url_text(f"GET /v1/link-refs/{ref_id}")

    now["value"] = 1_001
    second_token = sign_link_token(
        kind="artifact_download",
        worker_id="wrk_dedupe",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="outputs/report.md",
    )
    assert second_token != token
    assert create_signed_link_ref(token=second_token) == ref_id

    db_path = os.environ["GLASSHIVE_LINK_REF_STATE_PATH"]
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM signed_link_refs").fetchone()[0]
        conn.execute(
            "UPDATE signed_link_refs SET kind = ?, payload_json = ? WHERE ref_id = ?",
            (
                "worker_view",
                json.dumps({"kind": "worker_view", "worker_id": "wrk_tampered"}),
                ref_id,
            ),
        )
    assert count == 1
    resolved = resolve_signed_link_ref(ref_id)
    assert resolved is not None
    assert resolved["kind"] == "artifact_download"
    assert resolved["payload"]["worker_id"] == "wrk_dedupe"

    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "rotated-secret")
    assert resolve_signed_link_ref(ref_id) is None


def test_link_refs_can_be_revoked_by_worker(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    token_a = sign_link_token(
        kind="worker_view",
        worker_id="wrk_revoke",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    token_b = sign_link_token(
        kind="worker_view",
        worker_id="wrk_keep",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_a = create_signed_link_ref(token=token_a, target_url="/watch/wrk_revoke")
    ref_b = create_signed_link_ref(token=token_b, target_url="/watch/wrk_keep")
    assert ref_a and ref_b

    assert revoke_signed_link_refs_for_worker("wrk_revoke") == 1
    assert resolve_signed_link_ref(ref_a) is None
    assert resolve_signed_link_ref(ref_b) is not None


def test_terminate_revokes_worker_link_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Revoke Links", "goal": "Terminate revokes links."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Revoked Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = create_signed_link_ref(token=token, target_url=f"/watch/{worker['worker_id']}")
    assert resolve_signed_link_ref(ref_id) is not None

    assert client.post(f"/v1/workers/{worker['worker_id']}/terminate", headers=headers).status_code == 202
    assert resolve_signed_link_ref(ref_id) is None


@pytest.mark.parametrize("ambient_runtime_db", ["unset", "mismatched"])
def test_terminated_worker_artifact_credentials_fail_closed_when_revocation_sink_is_unavailable(
    tmp_path,
    monkeypatch,
    ambient_runtime_db,
):
    """The app-bound worker row, not ambient/tombstone state, revokes old links."""

    runtime_db = tmp_path / "authoritative" / "runtime.db"
    missing_ambient_db = tmp_path / "ambient" / "wrong-runtime.db"
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    if ambient_runtime_db == "unset":
        monkeypatch.delenv("WPR_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("WPR_DB_PATH", str(missing_ambient_db))

    app = create_app(str(runtime_db), runtime_backend="stub", runtime=StubRuntime())
    client = TestClient(app)
    service_headers = {"X-WPR-Token": "service-secret"}
    project = client.post(
        "/v1/projects",
        headers=service_headers,
        json={
            "owner_id": "demo-owner",
            "title": "Authoritative link revocation",
            "goal": "Reject every old artifact credential after termination.",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=service_headers,
        json={
            "owner_id": "demo-owner",
            "name": "Revocation worker",
            "role": "writer",
            "profile": "codex-cli",
        },
    ).json()
    artifact = Path(worker["workspace_dir"]) / "result.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    secret_bytes = b"authoritative artifact bytes"
    artifact.write_bytes(secret_bytes)

    opaque_token = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
        path="result.txt",
    )
    opaque_ref = create_signed_link_ref(token=opaque_token)
    legacy_query = sign_link_params(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
        path="result.txt",
    )
    legacy_path = f"/v1/workers/{worker['worker_id']}/artifacts/download"
    legacy_params = {"path": "result.txt", **legacy_query}
    view_token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
    )
    view_ref = create_signed_link_ref(
        token=view_token,
        target_url=f"/watch/{worker['worker_id']}?surface=desktop&gh_token={view_token}",
    )
    legacy_view_query = sign_link_params(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
    )

    # The same credentials remain valid while their authoritative worker is live.
    before_termination = (
        client.get(f"/v1/signed-links/{opaque_token}"),
        client.get(f"/v1/link-refs/{opaque_ref}"),
        client.get(legacy_path, params=legacy_params),
    )
    assert [response.status_code for response in before_termination] == [200, 200, 200]
    assert all(response.content == secret_bytes for response in before_termination)
    assert client.get(
        f"/v1/workers/{worker['worker_id']}/live",
        params=legacy_view_query,
    ).status_code == 200
    assert client.get(f"/r/{view_ref}", follow_redirects=False).status_code == 307
    assert client.get(f"/w/{view_ref}").status_code == 200

    def fail_revocation_sink(_worker_id: str) -> int:
        raise sqlite3.OperationalError("synthetic revocation sink failure")

    monkeypatch.setitem(
        WorkersProjectsService._apply_lifecycle_effect.__globals__,
        "revoke_signed_link_refs_for_worker",
        fail_revocation_sink,
    )
    terminated = client.post(
        f"/v1/workers/{worker['worker_id']}/terminate",
        headers=service_headers,
    )
    assert terminated.status_code == 202
    assert app.state.store.get_worker(worker["worker_id"])["state"] == "terminated"
    assert client.post(
        f"/v1/workers/{worker['worker_id']}/view-opened",
        headers=service_headers,
    ).status_code != 204

    after_termination = (
        client.get(f"/v1/signed-links/{opaque_token}"),
        client.get(f"/v1/link-refs/{opaque_ref}"),
        client.get(legacy_path, params=legacy_params),
    )
    assert all(response.status_code != 200 for response in after_termination)
    assert all(secret_bytes not in response.content for response in after_termination)
    assert client.get(
        f"/v1/workers/{worker['worker_id']}/live",
        params=legacy_view_query,
    ).status_code != 200
    assert client.get(f"/r/{view_ref}", follow_redirects=False).status_code != 307
    assert client.get(f"/w/{view_ref}").status_code != 200
    assert not missing_ambient_db.exists()


def test_enterprise_short_artifact_ref_is_auth_gated_and_durable_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    now = {"value": 1_000}
    monkeypatch.setattr(api_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(sign_link_token.__globals__["time"], "time", lambda: now["value"])

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    wrong_user_headers = {**headers, "X-Viventium-User-Id": "user-b"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Durable Links", "goal": "Keep old result links usable."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Durable Link Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    result_path = Path(worker["workspace_dir"]) / "overnight-result.txt"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("still here tomorrow", encoding="utf-8")

    token = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="overnight-result.txt",
        ttl_seconds=60,
    )
    ref_id = create_signed_link_ref(token=token)
    assert ref_id

    now["value"] = 2_000
    assert client.get(f"/v1/link-refs/{ref_id}").status_code == 401
    assert client.get(f"/v1/link-refs/{ref_id}", headers=wrong_user_headers).status_code == 404
    response = client.get(f"/v1/link-refs/{ref_id}", headers=headers)
    assert response.status_code == 200
    assert response.text == "still here tomorrow"


def test_enterprise_short_workspace_ref_mints_fresh_session_cookie_after_original_token_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "300")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")
    now = {"value": 1_000}
    monkeypatch.setattr(api_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(sign_link_token.__globals__["time"], "time", lambda: now["value"])

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Durable Workspace Links", "goal": "Reopen old workspaces."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "View Link Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?surface=desktop&gh_token={token}",
    )
    assert ref_id

    now["value"] = 2_000
    wrong_user_headers = {**headers, "X-Viventium-User-Id": "user-b"}
    assert client.get(f"/r/{ref_id}", headers=wrong_user_headers, follow_redirects=False).status_code == 404
    direct_redirect = client.get(f"/r/{ref_id}", follow_redirects=False)
    assert direct_redirect.status_code == 401
    assert "authenticated user assertion" in direct_redirect.text
    redirect = client.get(f"/r/{ref_id}", headers=headers, follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?surface=desktop"
    set_cookie = redirect.headers["set-cookie"]
    cookie_name = f"glasshive_gh_token_{hashlib.sha256(worker['worker_id'].encode('utf-8')).hexdigest()[:24]}"
    cookie_value = set_cookie.split(f"{cookie_name}=", 1)[1].split(";", 1)[0]
    assert cookie_value != token
    refreshed_payload = verify_signed_link_token(cookie_value)
    assert refreshed_payload is not None
    assert refreshed_payload["worker_id"] == worker["worker_id"]
    assert refreshed_payload["owner_id"] == "user-a"


def test_enterprise_short_refs_can_authorize_configured_email_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    owner_headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "worker-owner@example.com",
        "X-Viventium-User-Email": "worker-owner@example.com",
        "X-Viventium-User-Role": "member",
    }
    browser_headers = {
        **owner_headers,
        "X-Viventium-User-Id": "browser-subject",
        "X-Viventium-User-Email": "worker-owner@example.com",
    }
    project = client.post(
        "/v1/projects",
        headers=owner_headers,
        json={"owner_id": "ignored", "title": "Email Claim", "goal": "Open through configured email claim."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=owner_headers,
        json={
            "owner_id": "ignored",
            "name": "Email Claim Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    result_path = Path(worker["workspace_dir"]) / "result.txt"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("email claim artifact", encoding="utf-8")
    artifact_token = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="worker-owner@example.com",
        path="result.txt",
    )
    artifact_ref = create_signed_link_ref(token=artifact_token)
    view_token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="worker-owner@example.com",
    )
    view_ref = create_signed_link_ref(
        token=view_token,
        target_url=f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?gh_token={view_token}",
    )

    assert client.get(f"/v1/link-refs/{artifact_ref}", headers=browser_headers).status_code == 404
    assert client.get(f"/r/{view_ref}", headers=browser_headers, follow_redirects=False).status_code == 404

    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id,email")
    artifact_response = client.get(f"/v1/link-refs/{artifact_ref}", headers=browser_headers)
    assert artifact_response.status_code == 200
    assert artifact_response.text == "email claim artifact"
    workspace_response = client.get(f"/r/{view_ref}", headers=browser_headers, follow_redirects=False)
    assert workspace_response.status_code == 307
    assert workspace_response.headers["location"] == f"https://glasshive-ui.example.test/watch/{worker['worker_id']}"


def test_enterprise_short_refs_can_authorize_configured_owner_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    owner_headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "worker-owner@example.com",
        "X-Viventium-User-Role": "member",
    }
    alias_headers = {
        **owner_headers,
        "X-Viventium-User-Id": "browser-login-alias@example.com",
        "X-Viventium-User-Email": "browser-login-alias@example.com",
    }
    project = client.post(
        "/v1/projects",
        headers=owner_headers,
        json={"owner_id": "ignored", "title": "Owner Alias", "goal": "Open through configured alias."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=owner_headers,
        json={
            "owner_id": "ignored",
            "name": "Owner Alias Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    result_path = Path(worker["workspace_dir"]) / "result.txt"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("alias artifact", encoding="utf-8")
    artifact_ref = create_signed_link_ref(
        token=sign_link_token(
            kind="artifact_download",
            worker_id=worker["worker_id"],
            tenant_id="tenant-alpha",
            owner_id="worker-owner@example.com",
            path="result.txt",
        )
    )
    view_token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="worker-owner@example.com",
    )
    view_ref = create_signed_link_ref(
        token=view_token,
        target_url=f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?gh_token={view_token}",
    )
    monkeypatch.setenv(
        "GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON",
        json.dumps({"worker-owner@example.com": ["browser-login-alias@example.com"]}),
    )

    catalog = client.get("/v1/workspaces?kind=legacy", headers=alias_headers)
    assert catalog.status_code == 200
    assert [item["worker_id"] for item in catalog.json()["items"]] == [worker["worker_id"]]
    assert catalog.json()["items"][0]["execution_workspace_mode"] == "isolated"

    artifact_response = client.get(f"/v1/link-refs/{artifact_ref}", headers=alias_headers)
    assert artifact_response.status_code == 200
    assert artifact_response.text == "alias artifact"
    workspace_response = client.get(f"/r/{view_ref}", headers=alias_headers, follow_redirects=False)
    assert workspace_response.status_code == 307
    assert workspace_response.headers["location"] == f"https://glasshive-ui.example.test/watch/{worker['worker_id']}"

    wrong_tenant_headers = {
        **alias_headers,
        "X-Viventium-Tenant-Id": "tenant-beta",
    }
    assert client.get(f"/v1/link-refs/{artifact_ref}", headers=wrong_tenant_headers).status_code in {401, 404}
    assert client.get(f"/r/{view_ref}", headers=wrong_tenant_headers, follow_redirects=False).status_code in {401, 404}

    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", json.dumps({"*": ["browser-login-alias@example.com"]}))
    wildcard_alias_ctx = AuthContext(
        tenant_id="tenant-alpha",
        user_id="browser-login-alias@example.com",
        email="browser-login-alias@example.com",
        role="member",
        enterprise=True,
    )
    assert not owner_matches_auth_context("worker-owner@example.com", wildcard_alias_ctx)


def test_short_workspace_ref_rejects_unconfigured_absolute_redirect_target(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Redirect Guard", "goal": "Reject open redirects."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Redirect Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"https://unexpected.example.test/watch/{worker['worker_id']}?gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", headers=headers, follow_redirects=False)

    assert response.status_code == 403
    assert "target is not allowed" in response.text


@pytest.mark.parametrize(
    "target_url",
    [
        "//unexpected.example.test/watch",
        "////unexpected.example.test/watch",
        r"/\unexpected.example.test/watch",
    ],
)
def test_short_workspace_ref_rejects_relative_redirect_bypass_targets(tmp_path, monkeypatch, target_url):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Redirect Guard", "goal": "Reject relative redirect bypasses."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Redirect Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = create_signed_link_ref(token=token, target_url=target_url)

    response = client.get(f"/r/{ref_id}", headers=headers, follow_redirects=False)

    assert response.status_code == 400
    assert "target path is not allowed" in response.text


def test_short_workspace_ref_allows_explicit_redirect_host_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_REDIRECT_HOSTS", "allowed.example.test, https://other.example.test")
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Redirect Allowlist", "goal": "Allow configured redirect hosts."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Redirect Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"https://allowed.example.test/watch/{worker['worker_id']}?surface=desktop&gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", headers=headers, follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"https://allowed.example.test/watch/{worker['worker_id']}?surface=desktop"
    assert "gh_token=" not in response.headers["location"]


def test_short_ref_ttl_config_can_expire_authenticated_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_TTL_SECONDS", "30")
    now = {"value": 1_000}
    monkeypatch.setattr(api_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(sign_link_token.__globals__["time"], "time", lambda: now["value"])

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Configured Link Expiry", "goal": "Expire short refs when configured."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Expiring Ref Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    result_path = Path(worker["workspace_dir"]) / "configured-expiry.txt"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("expires by policy", encoding="utf-8")
    token = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="configured-expiry.txt",
    )
    ref_id = create_signed_link_ref(token=token)
    record = resolve_signed_link_ref(ref_id)
    assert record is not None
    assert record["expires_at"] == 1_030

    now["value"] = 1_031
    assert client.get(f"/v1/link-refs/{ref_id}", headers=headers).status_code == 401


def test_legacy_signed_link_ref_row_migrates_to_durable_payload_after_token_expiry(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    now = {"value": 1_000}
    monkeypatch.setattr(sign_link_token.__globals__["time"], "time", lambda: now["value"])
    token = sign_link_token(
        kind="worker_view",
        worker_id="wrk_legacy",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    ref_id = "ghr_legacy_migrated_123456"
    db_path = os.environ["GLASSHIVE_LINK_REF_STATE_PATH"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE signed_link_refs (
                ref_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                token TEXT NOT NULL,
                target_url TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO signed_link_refs (ref_id, kind, token, target_url, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ref_id, "worker_view", token, "https://glasshive.example.test/watch/wrk_legacy", 1_060, 1_000),
        )

    now["value"] = 2_000
    record = resolve_signed_link_ref(ref_id)
    assert record is not None
    assert record["expires_at"] == 0
    assert record["payload"]["worker_id"] == "wrk_legacy"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT expires_at, payload_json FROM signed_link_refs WHERE ref_id = ?", (ref_id,)).fetchone()
    assert row is not None
    assert row[0] == 0
    assert '"worker_id":"wrk_legacy"' in row[1]


def test_worker_view_signed_links_are_capped_by_watch_session_duration(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_TTL_S", "3600")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "120")

    now = int(time.time())
    token = sign_link_token(
        kind="worker_view",
        worker_id="wrk_publicsafe",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    payload = verify_signed_link_token(token)

    assert payload is not None
    assert int(payload["exp"]) - now <= 121


def test_signed_link_ttl_zero_means_non_expiring_opaque_token(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_TTL_S", "0")

    token = sign_link_token(
        kind="artifact_download",
        worker_id="wrk_publicsafe",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="artifacts/report.txt",
    )
    payload = verify_signed_link_token(token)

    assert payload is not None
    assert payload["exp"] == 0


def test_legacy_signed_link_exp_zero_means_non_expiring(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    params = sign_link_params(
        kind="artifact_download",
        worker_id="wrk_publicsafe",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="artifacts/report.txt",
        expires_at=0,
    )

    assert params["gh_exp"] == "0"
    assert verify_signed_link(
        kind="artifact_download",
        worker_id="wrk_publicsafe",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="artifacts/report.txt",
        expires_at=params["gh_exp"],
        signature=params["gh_sig"],
    )


def test_runtime_sensitive_url_log_filter_redacts_signed_tokens():
    token_query = "gh_" + "token=secret-token"
    raw = (
        f'GET /novnc/wrk_1/websockify?{token_query}&gh_sig=signature&gh_exp=123 '
        'GET /v1/signed-links/opaque-token?download=1'
        ' GET /w/ghr_public_ref_123456'
    )

    cookie = "Set-Cookie: glasshive_gh_token_0123456789abcdef01234567=worker-secret; HttpOnly; SameSite=lax"

    assert redact_sensitive_url_text(f"{raw} {cookie}") == (
        'GET /novnc/wrk_1/websockify?gh_token=[redacted]&gh_sig=[redacted]&gh_exp=[redacted] '
        'GET /v1/signed-links/[redacted]?download=1'
        ' GET /w/[redacted] '
        'Set-Cookie: glasshive_gh_token_0123456789abcdef01234567=[redacted]; HttpOnly; SameSite=lax'
    )
    assert redact_sensitive_url_text("gh_sig=signature&gh_token=secret-token") == (
        "gh_sig=[redacted]&gh_token=[redacted]"
    )
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="%s",
        args=(f"{raw} {cookie}",),
        exc_info=None,
    )
    assert SensitiveUrlLogFilter().filter(record) is True
    assert "secret-token" not in record.args[0]
    assert "opaque-token" not in record.args[0]
    assert "worker-secret" not in record.args[0]
    assert "gh_token=[redacted]" in record.args[0]
    assert "GET /w/[redacted]" in record.args[0]
    assert "glasshive_gh_token_0123456789abcdef01234567=[redacted]" in record.args[0]

    class UrlLike:
        def __str__(self) -> str:
            return "http://runtime.test/v1/link-refs/ghr_1234567890123456"

    structured_record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="HTTP Request: GET %s",
        args=(UrlLike(),),
        exc_info=None,
    )
    assert SensitiveUrlLogFilter().filter(structured_record) is True
    assert structured_record.getMessage() == "HTTP Request: GET http://runtime.test/v1/link-refs/[redacted]"


def test_runtime_sensitive_url_log_filter_installs_for_child_loggers(caplog):
    install_sensitive_url_log_filter()
    token_query = "gh_" + "token=secret-token"
    raw = f"https://glasshive.example.test/watch/wrk_1?{token_query}&gh_sig=signature"
    logger = logging.getLogger("workers_projects_runtime.service")

    with caplog.at_level(logging.INFO, logger="workers_projects_runtime.service"):
        logger.info("opening %s", raw, extra={"target_url": raw})

    assert "secret-token" not in caplog.text
    assert "gh_token=[redacted]" in caplog.text
    assert caplog.records
    assert getattr(caplog.records[-1], "target_url") == (
        "https://glasshive.example.test/watch/wrk_1?gh_token=[redacted]&gh_sig=[redacted]"
    )


def test_worker_ui_redacts_nested_runtime_paths_unless_diagnostics_enabled(tmp_path):
    secret_root = tmp_path / "secret-runtime"

    class NestedPathRuntime(StubRuntime):
        def describe_worker(self, worker: dict) -> dict[str, object]:
            return {
                "mode": "stub",
                "runtime": "openclaw-stub",
                "prompt_paths": {
                    "agents_md": str(secret_root / "AGENTS.md"),
                    "codex_md": str(secret_root / "CODEX.md"),
                },
                "nested": {
                    "status": "ready",
                    "home_dir": str(secret_root / "home"),
                    "recent": [str(secret_root / "stdout.log"), "safe-marker"],
                },
            }

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=NestedPathRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Nested Paths", "goal": "Hide nested internals."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Nested Path Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()

    page = client.get(f"/ui/workers/{worker['worker_id']}")
    assert page.status_code == 200
    assert str(secret_root) not in page.text
    assert "prompt_paths" not in page.text
    assert "safe-marker" in page.text

    live = client.get(f"/v1/workers/{worker['worker_id']}/live").json()
    assert str(secret_root) not in json.dumps(live)
    assert "prompt_paths" not in live["runtime_details"]

    diagnostics_page = client.get(f"/ui/workers/{worker['worker_id']}?diagnostics=1")
    assert diagnostics_page.status_code == 200
    assert str(secret_root) in diagnostics_page.text
    diagnostics_live = client.get(f"/v1/workers/{worker['worker_id']}/live?diagnostics=1").json()
    assert str(secret_root) in json.dumps(diagnostics_live)


def test_enterprise_member_ui_redacts_runtime_internals(tmp_path, monkeypatch):
    class DesktopStubRuntime(StubRuntime):
        def describe_worker(self, worker: dict) -> dict[str, object]:
            return {
                "mode": "stub-desktop",
                "runtime": "openclaw-stub",
                "gateway_url": "http://127.0.0.1/stub-gateway",
                "workspace_dir": f"/tmp/{worker['worker_id']}/workspace",
                "view_url": "http://127.0.0.1:5901/?autoconnect=1&password=secret",
            }

    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=DesktopStubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Member UI", "goal": "Hide internals."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Redacted Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()

    live = client.get(f"/v1/workers/{worker['worker_id']}/live", headers=headers).json()

    assert "session_key" not in live["worker"]
    assert "workspace_dir" not in live["worker"]
    assert "pid" not in live["worker"]
    assert live["workspace"]["root"] == ""
    assert live["console"]["stdout"] == ""
    assert live["console"]["stderr"] == ""
    assert "workspace_dir" not in live["runtime_details"]
    assert "state_dir" not in live["runtime_details"]
    assert "gateway_url" not in live["runtime_details"]

    page = client.get(f"/ui/workers/{worker['worker_id']}", headers=headers)
    assert page.status_code == 200
    body = page.text
    assert "Session Key:" not in body
    assert "Worker ID:" not in body
    assert "agent:main:wpr:worker" not in body
    assert "/tmp/" not in body
    assert "Managed by GlassHive" in body
    signed_query = "gh_sig=signature&gh_exp=123&gh_kind=worker_view"
    for path in (
        f"/ui/workers/{worker['worker_id']}",
        f"/ui/workers/{worker['worker_id']}/view",
        f"/ui/workers/{worker['worker_id']}/terminal",
    ):
        signed_page = client.get(f"{path}?{signed_query}", headers=headers)
        assert signed_page.status_code == 200
        assert "gh_sig=signature" not in signed_page.text
        assert "gh_exp=123" not in signed_page.text
        assert "gh_kind=worker_view" not in signed_page.text

    project_page = client.get(f"/ui/projects/{project['project_id']}?worker_id={worker['worker_id']}", headers=headers)
    assert project_page.status_code == 200
    project_body = project_page.text
    assert "API docs" not in project_body
    assert "Gateway:" not in project_body
    assert "Open worker console" not in project_body
    assert "Take over terminal" not in project_body
    assert "Send message" not in project_body
    assert "Create worker only" not in project_body
    assert "workerAction('resume')" not in project_body
    assert "workerAction('pause')" not in project_body
    assert "Open full workspace" in project_body
    assert "Open desktop directly" not in project_body
    assert "http://127.0.0.1:5901" not in project_body
    assert f"/watch/{worker['worker_id']}?project_id={project['project_id']}&surface=desktop" in project_body
    assert f"/desktop/{worker['worker_id']}" in project_body


def test_live_payload_survives_unavailable_idle_compute(tmp_path):
    class UnavailableRuntime(StubRuntime):
        def describe_worker(self, worker: dict) -> dict[str, object]:
            raise RuntimeError("container was idle-reaped")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=UnavailableRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "owner", "title": "Idle Live", "goal": "Keep completed output visible."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "owner",
            "name": "Idle Worker",
            "role": "main",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "create result"},
    ).json()
    completed = wait_for_run(client, run["run_id"])
    assert completed["state"] == "completed"

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")

    assert live.status_code == 200
    payload = live.json()
    assert payload["worker"]["worker_id"] == worker["worker_id"]
    assert payload["latest_run"]["state"] == "completed"
    assert payload["runtime_details"]["mode"] == "unavailable"
    assert payload["runtime_details"]["sandbox_state"] == "compute_unavailable"


def test_enterprise_worker_lookup_authorizes_before_heal_side_effects(tmp_path, monkeypatch):
    class HealingRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.collect_calls: list[str] = []

        def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
            self.collect_calls.append(str(worker["worker_id"]))
            return {"state": "completed", "output_text": "done", "error_text": ""}

    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    project = store.create_project("user-a", "Heal Guard", "Do not heal cross-user.", "codex-cli", tenant_id="tenant-alpha")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="user-a",
        name="Running Worker",
        role="research",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-test",
        tenant_id="tenant-alpha",
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "finish",
        state=RunRestorationState.RUNNING,
    )
    store.update_worker(worker["worker_id"], state="running", last_run_id=run["run_id"])
    runtime = HealingRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    headers_b = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-b",
        "X-Viventium-User-Role": "member",
    }
    headers_a = {
        **headers_b,
        "X-Viventium-User-Id": "user-a",
    }

    assert client.get(f"/v1/workers/{worker['worker_id']}", headers=headers_b).status_code == 404
    assert runtime.collect_calls == []
    assert store.get_run(run["run_id"])["state"] == "running"

    assert client.get(f"/v1/workers/{worker['worker_id']}", headers=headers_a).status_code == 200
    assert runtime.collect_calls == [worker["worker_id"]]
    assert store.get_run(run["run_id"])["state"] == "completed"


def test_enterprise_mode_requires_service_token_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="requires WPR_API_TOKEN"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_requires_deployment_tenant_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_TENANT_ID", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_TENANT_ID", raising=False)

    with pytest.raises(RuntimeError, match="requires GLASSHIVE_ENTERPRISE_TENANT_ID"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_requires_signed_link_secret_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="requires GLASSHIVE_SIGNED_LINK_SECRET"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_requires_signed_link_secret_distinct_from_service_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "same-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "same-secret")

    with pytest.raises(RuntimeError, match="must be distinct"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_rejects_invalid_owner_identity_config_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id,role")

    with pytest.raises(RuntimeError, match="only supports"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())

    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", "[]")

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())

    monkeypatch.delenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", raising=False)
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE", str(tmp_path / "missing-aliases.json"))

    with pytest.raises(RuntimeError, match="could not be read"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_loads_direct_runtime_env_file(tmp_path, monkeypatch):
    runtime_env = tmp_path / "runtime.env"
    link_ref_state = tmp_path / "shared-link-refs.sqlite3"
    runtime_env.write_text(
        "\n".join(
            [
                "GLASSHIVE_ENTERPRISE_MODE=true",
                "GLASSHIVE_AUTH_MODE=first_party_assertion",
                "GLASSHIVE_ENTERPRISE_TENANT_ID=tenant-alpha",
                "WPR_API_TOKEN=service-secret",
                "GLASSHIVE_SIGNED_LINK_SECRET=signed-link-secret",
                "OPENAI_API_KEY=runtime-openai-key",
                "ANTHROPIC_API_KEY=runtime-anthropic-key",
                "WPR_OPENCLAW_USE_CUSTOM_PROVIDER=1",
                "WPR_OPENCLAW_WIRE_API=openai-completions",
                "GLASSHIVE_MAX_WORKSPACES_PER_USER=4",
                f"GLASSHIVE_LINK_REF_STATE_PATH={link_ref_state}",
                "GLASSHIVE_LINK_REF_SHARED_GROUP=glasshive-state",
                "GLASSHIVE_LINK_REF_TTL_SECONDS=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_env))
    for key in (
        "GLASSHIVE_ENTERPRISE_MODE",
        "GLASSHIVE_AUTH_MODE",
        "GLASSHIVE_ENTERPRISE_TENANT_ID",
        "WPR_API_TOKEN",
        "GLASSHIVE_SIGNED_LINK_SECRET",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "WPR_OPENCLAW_USE_CUSTOM_PROVIDER",
        "WPR_OPENCLAW_WIRE_API",
        "GLASSHIVE_MAX_WORKSPACES_PER_USER",
        "GLASSHIVE_LINK_REF_STATE_PATH",
        "GLASSHIVE_LINK_REF_SHARED_GROUP",
        "GLASSHIVE_LINK_REF_TTL_SECONDS",
    ):
        # The runtime loader writes directly to os.environ. Register an empty
        # value with monkeypatch first so its teardown removes the value that
        # the loader installs instead of leaking it into later tests.
        monkeypatch.setenv(key, "")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))

    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 401
    assert client.get("/redoc").status_code == 401
    assert os.environ["OPENAI_API_KEY"] == "runtime-openai-key"
    assert os.environ["ANTHROPIC_API_KEY"] == "runtime-anthropic-key"
    assert os.environ["WPR_OPENCLAW_USE_CUSTOM_PROVIDER"] == "1"
    assert os.environ["WPR_OPENCLAW_WIRE_API"] == "openai-completions"
    assert os.environ["GLASSHIVE_MAX_WORKSPACES_PER_USER"] == "4"
    assert os.environ["GLASSHIVE_LINK_REF_STATE_PATH"] == str(link_ref_state)
    assert os.environ["GLASSHIVE_LINK_REF_SHARED_GROUP"] == "glasshive-state"
    assert os.environ["GLASSHIVE_LINK_REF_TTL_SECONDS"] == "0"


def test_enterprise_oauth_modes_require_external_validator(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "oauth_oidc")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    with pytest.raises(RuntimeError, match="external token validator"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_admin_api_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "admin-user",
        "X-Viventium-User-Role": "admin",
    }

    assert client.post("/v1/admin/reconcile", headers=headers).status_code == 404
    assert client.post("/v1/admin/schedules/run-due", headers=headers).status_code == 404


def test_enterprise_admin_api_requires_enable_flag_and_admin_role(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENABLE_ADMIN_API", "true")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    member_headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "member-user",
        "X-Viventium-User-Role": "member",
    }
    admin_headers = {
        **member_headers,
        "X-Viventium-User-Id": "admin-user",
        "X-Viventium-User-Role": "admin",
    }

    assert client.post("/v1/admin/reconcile", headers=member_headers).status_code == 403
    assert client.post("/v1/admin/schedules/run-due", headers=member_headers).status_code == 403
    assert client.post("/v1/admin/reconcile", headers=admin_headers).status_code == 200
    processed = client.post("/v1/admin/schedules/run-due", headers=admin_headers)
    assert processed.status_code == 200
    assert processed.json()["processed"] == []


def test_idle_reaper_stops_compute_but_preserves_worker(tmp_path, monkeypatch):
    class ReaperRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.terminated: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = ReaperRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Idle", "Stop idle compute", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Idle Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), worker["worker_id"]),
            )

        reaped = service.reap_idle_workers_once()

        assert reaped and reaped[0]["worker_id"] == worker["worker_id"]
        assert runtime.terminated == [worker["worker_id"]]
        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed is not None
        assert refreshed["state"] == "paused"
        assert refreshed["compute_released_at"]
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "worker.idle_terminated"
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), worker["worker_id"]),
            )
        assert service.reap_idle_workers_once() == []
        assert runtime.terminated == [worker["worker_id"]]
    finally:
        service.shutdown()


def test_upgrade_stops_only_idle_compute_and_keeps_workspaces_warm_by_default(
    tmp_path, monkeypatch
):
    class ReleaseRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.terminated: list[str] = []
            self.boxes: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

        def release_idle_workspace_box(self, worker: dict) -> bool:
            self.boxes.append(worker["worker_id"])
            return True

    monkeypatch.delenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", raising=False)
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    runtime = ReleaseRuntime()
    app = create_app(
        db_path=str(tmp_path / "runtime.db"), runtime=runtime, reconcile_on_startup=False
    )
    store, service = app.state.store, app.state.service
    client = TestClient(app)  # no lifespan: the scheduler must not run the queued work
    url = "/v1/admin/maintenance/release-idle-compute"
    try:
        project = service.create_project("owner", "Upgrade", "Keep idle workspaces", "openclaw-general")
        ids = {
            name: service.create_worker(
                project_id=project["project_id"], owner_id="owner", name=name,
                role="research", profile="openclaw-general", backend="openclaw",
            )["worker_id"]
            for name in ("idle", "queued", "paused")
        }
        store.create_run(ids["queued"], project["project_id"], "Queued work")
        store.update_worker_state(ids["paused"], "paused")

        # Workers stay warm by default; the periodic reaper releases nothing.
        assert service.reap_idle_workers_once() == []
        assert runtime.terminated == []
        # Only the package's own service credential may stop compute package-wide.
        person = client.post(url, headers={"X-WPR-Token": "service-token", "X-Viventium-User-Id": "owner"})
        assert person.status_code == 403
        assert runtime.terminated == []

        released = client.post(url, headers={"X-WPR-Token": "service-token"})
        assert released.status_code == 200
        assert released.json() == {"status": "ok", "released": [ids["idle"]]}
        assert runtime.terminated == [ids["idle"]]
        assert runtime.boxes == [ids["idle"]]
        kept = store.get_worker(ids["idle"])
        assert kept["compute_released_at"] and kept["state"] not in {"terminated", "failed"}
        assert store.get_worker(ids["queued"])["compute_released_at"] is None
        assert store.get_worker(ids["paused"])["compute_released_at"] is None
        again = client.post(url, headers={"X-WPR-Token": "service-token"})
        assert again.json() == {"status": "ok", "released": []}

        # When a deployment enables the idle timer, its release also closes the shared box.
        monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "1")
        later = service.create_worker(
            project_id=project["project_id"], owner_id="owner", name="later",
            role="research", profile="openclaw-general", backend="openclaw",
        )["worker_id"]
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), later),
            )
        assert [item["worker_id"] for item in service.reap_idle_workers_once()] == [later]
        assert runtime.boxes == [ids["idle"], later]
    finally:
        service.shutdown()


def test_idle_reaper_preserves_completed_worker_state(tmp_path, monkeypatch):
    class ReaperRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.terminated: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = ReaperRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Completed", "Preserve completed label", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Completed Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        store.update_worker_state(worker["worker_id"], "completed")
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), worker["worker_id"]),
            )

        reaped = service.reap_idle_workers_once()

        assert reaped and reaped[0]["state"] == "completed"
        assert runtime.terminated == [worker["worker_id"]]
        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed is not None
        assert refreshed["state"] == "completed"
        assert refreshed["compute_released_at"]
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "worker.idle_terminated"
    finally:
        service.shutdown()


def test_lifecycle_reaper_removes_compute_orphaned_after_worker_termination(tmp_path, monkeypatch):
    class OrphanRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.container_running = True
            self.terminated: list[str] = []

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=4242 if self.container_running else None,
            )

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            self.container_running = False
            return self.reconcile_worker(worker)

    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = OrphanRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Terminated", "Reconcile compute", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Terminated Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        store.update_worker(
            worker["worker_id"],
            state="terminated",
            compute_released_at=datetime.now(timezone.utc).isoformat(),
        )

        reaped = service.reap_terminated_workers_once()

        assert reaped == [{"worker_id": worker["worker_id"], "project_id": project["project_id"]}]
        assert runtime.terminated == [worker["worker_id"]]
        assert store.get_worker(worker["worker_id"])["state"] == "terminated"
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "worker.terminated_compute_reconciled"
    finally:
        service.shutdown()


def test_lifecycle_reaper_is_enabled_for_orphan_reconciliation_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_ORPHAN_REAPER_ENABLED", raising=False)
    monkeypatch.delenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", raising=False)
    monkeypatch.delenv("GLASSHIVE_PAUSED_TERMINATE_AFTER_S", raising=False)
    monkeypatch.delenv("GLASSHIVE_MAX_RUN_DURATION_S", raising=False)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        assert service._lifecycle_reaper_enabled() is True
    finally:
        service.shutdown()


def test_lifecycle_reaper_removes_nonrunning_compute_from_failed_worker(tmp_path, monkeypatch):
    class FailedOrphanRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.compute_present = True
            self.terminated: list[str] = []

        def worker_compute_present(self, worker: dict) -> bool:
            return self.compute_present

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            self.compute_present = False
            return self.reconcile_worker(worker)

    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = FailedOrphanRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Failed", "Reconcile nonrunning compute", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Failed Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        store.update_worker(
            worker["worker_id"],
            state="failed",
            last_error="termination did not release compute",
        )

        reaped = service.reap_terminated_workers_once()

        assert reaped == [{"worker_id": worker["worker_id"], "project_id": project["project_id"]}]
        assert runtime.terminated == [worker["worker_id"]]
        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed["state"] == "failed"
        assert refreshed["last_error"] == "termination did not release compute"
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "worker.failed_compute_reconciled"
    finally:
        service.shutdown()


def test_paused_reaper_stops_paused_compute_without_deleting_workspace(tmp_path, monkeypatch):
    class ReaperRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.terminated: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

    monkeypatch.setenv("GLASSHIVE_PAUSED_TERMINATE_AFTER_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = ReaperRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Paused", "Stop paused compute", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Paused Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        store.update_worker_state(worker["worker_id"], "paused")
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), worker["worker_id"]),
            )

        reaped = service.reap_paused_workers_once()

        assert reaped and reaped[0]["worker_id"] == worker["worker_id"]
        assert runtime.terminated == [worker["worker_id"]]
        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed is not None
        assert refreshed["state"] == "paused"
        assert refreshed["compute_released_at"]
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "worker.paused_compute_terminated"
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), worker["worker_id"]),
            )

        assert service.reap_paused_workers_once() == []
        assert runtime.terminated == [worker["worker_id"]]
        paused_release_events = [
            event
            for event in store.list_events(worker["worker_id"])
            if event["event_type"] == "worker.paused_compute_terminated"
        ]
        assert len(paused_release_events) == 1
    finally:
        service.shutdown()


def test_max_run_duration_cancels_expired_run_and_releases_compute(
    tmp_path,
    monkeypatch,
    background_consumers_disabled,
):
    _ = background_consumers_disabled
    class ReaperRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.terminated: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

    monkeypatch.setenv("GLASSHIVE_MAX_RUN_DURATION_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = ReaperRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Expired", "Stop long run", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Expired Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        run = create_truthfully_invoked_test_run(
            store,
            worker,
            project["project_id"],
            "sleep forever",
            suffix="max-duration-expired",
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE runs SET started_at = ? WHERE run_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), run["run_id"]),
            )

        reaped = service.reap_expired_runs_once()

        assert reaped and reaped[0]["run_id"] == run["run_id"]
        assert runtime.terminated == [worker["worker_id"]]
        refreshed_run = store.get_run(run["run_id"])
        refreshed_worker = store.get_worker(worker["worker_id"])
        assert refreshed_run is not None and refreshed_run["state"] == "cancelled"
        assert "GLASSHIVE_MAX_RUN_DURATION_S=1" in refreshed_run["error_text"]
        assert refreshed_worker is not None and refreshed_worker["state"] == "paused"
        assert refreshed_worker["compute_released_at"]
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "run.duration_exceeded"
        effects = store.list_lifecycle_operation_effects(worker_id=worker["worker_id"])
        assert len(effects) == 1
        assert effects[0]["operation_kind"] == "max_duration"
        assert effects[0]["effect_kind"] == "callback.run_cancelled"
        assert effects[0]["run_id"] == run["run_id"]
    finally:
        service.shutdown()


def test_max_run_duration_treats_malformed_run_timestamp_as_expired(
    tmp_path,
    monkeypatch,
    background_consumers_disabled,
):
    _ = background_consumers_disabled
    class ReaperRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.terminated: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

    monkeypatch.setenv("GLASSHIVE_MAX_RUN_DURATION_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = ReaperRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Expired", "Stop malformed long run", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Malformed Timestamp Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        run = create_truthfully_invoked_test_run(
            store,
            worker,
            project["project_id"],
            "sleep forever",
            suffix="max-duration-malformed",
        )
        with store._connect() as conn:
            conn.execute("UPDATE runs SET started_at = ? WHERE run_id = ?", ("not-a-date", run["run_id"]))

        reaped = service.reap_expired_runs_once()

        assert reaped and reaped[0]["run_id"] == run["run_id"]
        assert runtime.terminated == [worker["worker_id"]]
        refreshed_run = store.get_run(run["run_id"])
        assert refreshed_run is not None and refreshed_run["state"] == "cancelled"
    finally:
        service.shutdown()


def test_callbacks_sign_utf8_canonical_json_for_unicode_messages(
    tmp_path,
    monkeypatch,
    background_consumers_disabled,
):
    _ = background_consumers_disabled
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, *, content, headers, timeout):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    runtime = StubRuntime()
    runtime.effort_projection_for_worker = lambda _worker: {
        "requested": "xhigh",
        "effective": "medium",
        "fallback_reason": "xhigh_route_not_proven",
    }
    service = WorkersProjectsService(store, runtime)
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback signatures", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "surface": "telegram",
                    "stream_id": "stream-123",
                    "voice_call_session_id": "call-123",
                    "telegram_chat_id": "chat-123",
                }
            },
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Open user's Chrome - no sandbox",
        )

        service._emit_callback(worker, "run.queued", run=run, message="Open user's Chrome - no sandbox — verified")
        wait_until(lambda: "content" in captured)
    finally:
        service.shutdown()

    content = captured["content"]
    assert isinstance(content, bytes)
    payload_text = content.decode("utf-8")
    assert "—" in payload_text
    assert "\\u2014" not in payload_text

    payload = json.loads(payload_text)
    assert payload["surface"] == "telegram"
    assert payload["stream_id"] == "stream-123"
    assert payload["voice_call_session_id"] == "call-123"
    assert payload["telegram_chat_id"] == "chat-123"
    assert payload["effort_projection"] == {
        "requested": "xhigh",
        "effective": "medium",
        "fallback_reason": "xhigh_route_not_proven",
    }
    binding = f"{payload['worker_id']}:{payload['run_id']}".encode("utf-8")
    derived_secret = hmac.new(b"callback-secret", binding, hashlib.sha256).hexdigest().encode("utf-8")
    expected = "sha256=" + hmac.new(derived_secret, content, hashlib.sha256).hexdigest()
    assert captured["headers"]["X-GlassHive-Signature"] == expected


def test_incomplete_viventium_parent_callback_is_not_enqueued(tmp_path, monkeypatch):
    posted = Event()

    def fake_post(*args, **kwargs):
        posted.set()
        raise AssertionError("Incomplete Viventium callbacks must not be sent")

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Skip incomplete parent callbacks", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://localhost:3080/api/viventium/glasshive/callback",
                    "hmac_secret": "callback-secret",
                    "user_id": "user-1",
                }
            },
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Finish",
            state="completed",
        )

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        time.sleep(0.05)
        with store._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM callback_outbox").fetchone()[0]
    finally:
        service.shutdown()

    assert not posted.is_set()
    assert count == 0


def test_callbacks_retry_transient_delivery_failures(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://callback.local/glasshive")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("server error", request=request, response=response)
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response(500 if len(attempts) == 1 else 200)

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback retry", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Open Chrome",
            state="completed",
        )

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(lambda: len(attempts) == 2)
    finally:
        service.shutdown()

    assert len(attempts) == 2
    assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.failed"]


def test_duplicate_callback_without_durable_receipt_stays_pending(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        status_code = 409

        def raise_for_status(self):
            request = httpx.Request("POST", "http://callback.local/glasshive")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError("callback conflict", request=request, response=response)

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response()

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback dedupe", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Open Chrome",
            state="completed",
        )

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(
            lambda: len(attempts) == 1
            and any(
                event["event_type"] == "callback.failed"
                for event in store.list_events(worker["worker_id"])
            )
        )
    finally:
        service.shutdown()

    assert len(attempts) == 1
    assert len(store.list_pending_callbacks()) == 1


def test_failed_callback_stays_pending_and_replays_to_http_acceptance_on_restart(
    tmp_path, monkeypatch
):
    attempts: list[int] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://callback.local/glasshive")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("server error", request=request, response=response)
            return None

    def failing_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(500)
        return Response(500)

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", failing_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback outbox replay", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Open Chrome",
            state="completed",
        )

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(
            lambda: bool(store.list_pending_callbacks())
            and bool(
                [
                    event
                    for event in store.list_events(worker["worker_id"])
                    if event["event_type"] == "callback.failed"
                ]
            )
        )
    finally:
        service.shutdown()

    pending = store.list_pending_callbacks()
    assert len(pending) == 1
    callback_id = pending[0]["callback_id"]
    assert [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.failed"]

    def succeeding_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        attempts.append(200)
        payload = json.loads(content.decode("utf-8"))
        assert payload["callback_id"] == callback_id
        return Response(200)

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", succeeding_post)
    replay_service = WorkersProjectsService(store, StubRuntime())
    try:
        deadline = time.time() + 2
        while time.time() < deadline and store.list_pending_callbacks():
            time.sleep(0.05)
    finally:
        replay_service.shutdown()

    assert not store.list_pending_callbacks()
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, http_accepted_at, delivered_at "
            "FROM callback_outbox WHERE callback_id = ?",
            (callback_id,),
        ).fetchone()
    assert row["status"] == "http_accepted"
    assert row["http_accepted_at"]
    assert row["delivered_at"] is None
    assert row["attempts"] >= 2
    assert attempts[0] == 500
    assert attempts[-1] == 200


def test_pending_callback_replay_does_not_block_service_startup(tmp_path, monkeypatch):
    class Response:
        status_code = 500

        def raise_for_status(self):
            request = httpx.Request("POST", "http://callback.local/glasshive")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)

    entered_post = Event()
    release_post = Event()

    def blocking_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        entered_post.set()
        release_post.wait(timeout=5)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", blocking_post)

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Callbacks", "Verify non-blocking callback replay", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Codex Host",
        role="host worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.local/glasshive",
                "hmac_secret": "callback-secret",
            }
        },
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Open Chrome", state="completed")
    payload = {
        "callback_id": "cb_pending_startup",
        "callback_ts": int(time.time()),
        "event": "run.completed",
        "project_id": project["project_id"],
        "worker_id": worker["worker_id"],
        "run_id": run["run_id"],
        "run_state": "completed",
        "message": "Done",
    }
    store.upsert_callback_outbox(
        callback_id="cb_pending_startup",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        event_type="run.completed",
        url="http://callback.local/glasshive",
        payload_json=json.dumps(payload, ensure_ascii=False),
    )

    started_at = time.perf_counter()
    service = WorkersProjectsService(store, StubRuntime())
    elapsed = time.perf_counter() - started_at
    try:
        assert elapsed < 0.5
        assert entered_post.wait(timeout=1)
    finally:
        release_post.set()
        service.shutdown()


def test_callbacks_retry_budget_is_configurable(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://callback.local/glasshive")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("server error", request=request, response=response)
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response(500 if len(attempts) < 4 else 200)

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "4")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify configurable callback retry", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Open Chrome",
            state="completed",
        )

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(lambda: len(attempts) == 4)
    finally:
        service.shutdown()

    assert len(attempts) == 4
    assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.failed"]


def _create_callback_outbox_record(store: Store, *, payload_json: str | None = None, url: str = "http://callback.local/glasshive"):
    project = store.create_project("owner", "Callbacks", "Verify callback outbox", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Codex Host",
        role="host worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": url,
                "hmac_secret": "callback-secret",
            }
        },
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Open Chrome", state="completed")
    callback_id = "cb_" + uuid.uuid4().hex
    payload = {
        "callback_id": callback_id,
        "callback_ts": int(time.time()),
        "event": "run.completed",
        "project_id": project["project_id"],
        "worker_id": worker["worker_id"],
        "run_id": run["run_id"],
        "run_state": "completed",
        "message": "Done",
    }
    record = store.upsert_callback_outbox(
        callback_id=callback_id,
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        event_type="run.completed",
        url=url,
        payload_json=payload_json if payload_json is not None else json.dumps(payload, ensure_ascii=False),
    )
    return project, worker, run, record


def _callback_row(store: Store, callback_id: str):
    with store._connect() as conn:
        return conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?", (callback_id,)).fetchone()


def test_permanent_403_callback_dead_letters_immediately(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        status_code = 403

        def raise_for_status(self):
            request = httpx.Request("POST", "http://callback.local/glasshive")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS", "3")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        _project, worker, _run, record = _create_callback_outbox_record(store)
        service._deliver_callback_record(worker, store.list_pending_callbacks()[0], service._callback_config_for(worker))
    finally:
        service.shutdown()

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "dead_lettered"
    assert row["attempts"] == 1
    assert "terminal HTTP 403" in row["last_error"]
    assert len(attempts) == 1
    assert not store.list_pending_callbacks()
    assert [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.dead_lettered"]


def test_callback_total_budget_uses_stored_attempts(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        status_code = 500

        def raise_for_status(self):
            request = httpx.Request("POST", "http://callback.local/glasshive")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS", "3")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        _project, worker, _run, record = _create_callback_outbox_record(store)
        with store._connect() as conn:
            conn.execute("UPDATE callback_outbox SET attempts = 2 WHERE callback_id = ?", (record["callback_id"],))
        service._deliver_callback_record(worker, store.list_pending_callbacks()[0], service._callback_config_for(worker))
    finally:
        service.shutdown()

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "dead_lettered"
    assert row["attempts"] == 3
    assert len(attempts) == 1


def test_callback_over_budget_dead_letters_without_http(tmp_path, monkeypatch):
    attempts: list[int] = []

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        raise AssertionError("over-budget callback should not call the remote endpoint")

    monkeypatch.setenv("GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS", "3")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    # This is a synchronous unit of the delivery method; stop the background retry poller so it
    # cannot claim the synthetic record concurrently and inflate the attempt count.
    service.shutdown()
    _project, worker, _run, record = _create_callback_outbox_record(store)
    with store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET attempts = 3 WHERE callback_id = ?", (record["callback_id"],))
    service._deliver_callback_record(worker, store.list_pending_callbacks()[0], service._callback_config_for(worker))

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "dead_lettered"
    assert row["attempts"] == 3
    assert "retry budget exhausted before delivery" in row["last_error"]
    assert attempts == []


def test_terminal_404_callback_dead_letters_immediately(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        status_code = 404

        def raise_for_status(self):
            request = httpx.Request("POST", "http://callback.local/glasshive")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        _project, worker, _run, record = _create_callback_outbox_record(store)
        service._deliver_callback_record(worker, store.list_pending_callbacks()[0], service._callback_config_for(worker))
    finally:
        service.shutdown()

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "dead_lettered"
    assert row["attempts"] == 1
    assert "terminal HTTP 404" in row["last_error"]
    assert len(attempts) == 1


def test_local_scheduling_callback_404_is_retryable(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            request = httpx.Request("POST", "http://localhost:7110/internal/scheduled-prompts/glasshive-callback")
            response = httpx.Response(self.status_code, request=request)
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("callback error", request=request, response=response)

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response(404 if len(attempts) == 1 else 200)

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        _project, worker, _run, record = _create_callback_outbox_record(
            store,
            url="http://localhost:7110/internal/scheduled-prompts/glasshive-callback",
        )
        service._deliver_callback_record(worker, store.list_pending_callbacks()[0], service._callback_config_for(worker))
    finally:
        service.shutdown()

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "http_accepted"
    assert row["http_accepted_at"]
    assert row["delivered_at"] is None
    assert row["attempts"] == 2
    assert row["last_error"] == ""
    assert len(attempts) == 2


def test_persistent_local_scheduling_callback_404_dead_letters_after_budget(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        status_code = 404

        def raise_for_status(self):
            request = httpx.Request("POST", "http://localhost:7110/internal/scheduled-prompts/glasshive-callback")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS", "3")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    service.shutdown()
    try:
        _project, worker, _run, record = _create_callback_outbox_record(
            store,
            url="http://localhost:7110/internal/scheduled-prompts/glasshive-callback",
        )
        for _ in range(3):
            pending = store.list_pending_callbacks()
            if not pending:
                break
            service._deliver_callback_record(worker, pending[0], service._callback_config_for(worker))
    finally:
        service.shutdown()

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "dead_lettered"
    assert row["attempts"] == 3
    assert "retry budget exhausted after 3 attempts" in row["last_error"]
    assert len(attempts) == 3


def test_invalid_callback_payload_is_rejected_before_outbox_or_http(tmp_path, monkeypatch):
    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        raise AssertionError("invalid payload must not be posted")

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        with pytest.raises(ValueError, match="valid JSON"):
            _create_callback_outbox_record(store, payload_json="{invalid")
    finally:
        service.shutdown()

    assert store.list_pending_callbacks() == []


def test_missing_callback_url_dead_letters_immediately(tmp_path, monkeypatch):
    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        raise AssertionError("missing callback URL must not be posted")

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        _project, worker, _run, record = _create_callback_outbox_record(store, url="")
        service._deliver_callback_record(worker, store.list_pending_callbacks()[0], {})
    finally:
        service.shutdown()

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "dead_lettered"
    assert row["attempts"] == 1
    assert "missing callback url" in row["last_error"]


def test_stale_delivering_callback_is_reclaimed_for_replay(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_DELIVERING_STALE_AFTER_S", "1")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        _project, worker, _run, record = _create_callback_outbox_record(store)
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with store._connect() as conn:
            conn.execute(
                "UPDATE callback_outbox SET status = 'delivering', updated_at = ? WHERE callback_id = ?",
                (old_time, record["callback_id"]),
            )
        service._replay_pending_callbacks()
    finally:
        service.shutdown()

    row = _callback_row(store, record["callback_id"])
    assert row["status"] == "http_accepted"
    assert row["http_accepted_at"]
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert len(attempts) == 1


def test_metrics_include_callback_outbox_health(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    _project, _worker, _run, pending = _create_callback_outbox_record(store)
    _project, _worker, _run, delivering = _create_callback_outbox_record(store)
    _project, _worker, _run, dead_lettered = _create_callback_outbox_record(store)
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE callback_outbox SET attempts = 7, updated_at = ? WHERE callback_id = ?",
            (old_time, pending["callback_id"]),
        )
        conn.execute(
            "UPDATE callback_outbox SET status = 'delivering', attempts = 3 WHERE callback_id = ?",
            (delivering["callback_id"],),
        )
    dead_letter_claim = store.claim_pending_callback(dead_lettered["callback_id"])
    assert dead_letter_claim is not None
    store.mark_callback_dead_lettered(
        dead_lettered["callback_id"],
        lease_token=dead_letter_claim["delivery_lease_token"],
        delivery_generation=dead_letter_claim["delivery_generation"],
        attempts=99,
        payload_json=str(dead_lettered["payload_json"]),
        last_error="terminal test callback",
    )

    metrics = store.metrics()

    assert metrics["callback_pending"] == 1
    assert metrics["callback_delivering"] == 1
    assert metrics["callback_dead_lettered"] == 1
    assert metrics["callback_max_attempts"] == 7
    assert metrics["callback_oldest_pending_age_seconds"] >= 250


def test_assign_run_does_not_block_on_callback_delivery(tmp_path, monkeypatch):
    entered_post = Event()
    release_post = Event()

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    def blocking_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        entered_post.set()
        release_post.wait(timeout=1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", blocking_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Verify non-blocking run callback", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )

        started_at = time.perf_counter()
        run = service.assign_run(worker["worker_id"], "Open Chrome")
        elapsed = time.perf_counter() - started_at
        assert run["state"] == "queued"
        assert elapsed < 2
        assert entered_post.wait(timeout=2)
    finally:
        release_post.set()
        service.shutdown()


def test_callback_config_recovers_runtime_env_url_and_secret(tmp_path, monkeypatch, caplog):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "\n".join(
            [
                "VIVENTIUM_GLASSHIVE_CALLBACK_URL=http://callback.local/glasshive",
                "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET=runtime-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_env))
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Recover callback env", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "conversation_id": "conv-1",
                    "message_id": "assistant-1",
                }
            },
        )

        with caplog.at_level("WARNING"):
            callbacks = service._callback_config_for(worker)
    finally:
        service.shutdown()

    assert callbacks["events_webhook_url"] == "http://callback.local/glasshive"
    assert callbacks["hmac_secret"] == "runtime-secret"
    assert callbacks["conversation_id"] == "conv-1"
    assert "Recovered GlassHive callback endpoint, secret" in caplog.text
    assert "runtime-secret" not in caplog.text


def test_startup_reconcile_repairs_parallel_mission_networks_before_work_recovery(
    tmp_path,
):
    class RepairingRuntime(StubRuntime):
        def __init__(self) -> None:
            self.repairs = 0

        def repair_parallel_clean_room_mission_networks(self) -> tuple[str, ...]:
            self.repairs += 1
            return ("synthetic-mission-network",)

    runtime = RepairingRuntime()
    service = WorkersProjectsService(
        Store(str(tmp_path / "runtime.db")),
        runtime,
        reconcile_on_startup=True,
    )
    try:
        assert runtime.repairs == 1
    finally:
        service.shutdown()


def test_startup_reconcile_does_not_postpone_idle_compute_release(tmp_path, monkeypatch):
    class RestartReaperRuntime(StubRuntime):
        def __init__(self) -> None:
            self.terminated: list[str] = []

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            info.workspace_dir = str(worker.get("workspace_dir") or "")
            return info

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(str(worker["worker_id"]))
            info = super().ensure_worker_ready(worker)
            info.pid = None
            info.workspace_dir = str(worker.get("workspace_dir") or "")
            return info

    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Restart Idle Reaper",
        "Release retained compute after a service restart.",
        "openclaw-general",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Completed Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    retained_workspace = tmp_path / "retained-workspace"
    retained_workspace.mkdir()
    store.update_worker(
        worker["worker_id"],
        workspace_dir=str(retained_workspace),
    )
    completed_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    run = create_truthfully_invoked_test_run(
        store,
        worker,
        project["project_id"],
        "Complete before restart",
        suffix="restart-idle-reaper",
    )
    store.update_run(
        run["run_id"],
        state="completed",
        ended_at=completed_at.isoformat(),
        output_text="Synthetic completed result",
    )
    store.update_worker(
        worker["worker_id"],
        state="completed",
        last_run_id=run["run_id"],
    )
    idle_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
            (idle_at, worker["worker_id"]),
        )

    runtime = RestartReaperRuntime()
    service = WorkersProjectsService(
        store,
        runtime,
        reconcile_on_startup=True,
        start_background_consumers=False,
    )
    try:
        service.reconcile_all_workers()
        reconciled = store.get_worker(worker["worker_id"])
        assert reconciled is not None
        assert reconciled["updated_at"] == idle_at
        workspace_dir = reconciled["workspace_dir"]

        reaped = service.reap_idle_workers_once()

        assert [item["worker_id"] for item in reaped] == [worker["worker_id"]]
        assert runtime.terminated == [worker["worker_id"]]
        released = store.get_worker(worker["worker_id"])
        assert released is not None
        assert released["state"] == "completed"
        assert released["compute_released_at"]
        assert released["workspace_dir"] == workspace_dir
        assert (store.get_run(run["run_id"]) or {})["state"] == "completed"
    finally:
        service.shutdown()


def test_idle_reaper_uses_terminal_run_end_after_restart_lifecycle_refresh(
    tmp_path,
    monkeypatch,
):
    class RestartRefreshedRuntime(StubRuntime):
        def __init__(self) -> None:
            self.terminated: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(str(worker["worker_id"]))
            info = super().ensure_worker_ready(worker)
            info.pid = None
            return info

    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "60")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Restart Refreshed Idle Reaper",
        "Release terminal compute even when lifecycle reconciliation refreshed the worker row.",
        "openclaw-general",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Cancelled Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    run = create_truthfully_invoked_test_run(
        store,
        worker,
        project["project_id"],
        "Cancel before restart",
        suffix="restart-terminal-reaper",
    )
    store.update_run(
        run["run_id"],
        state="cancelled",
        ended_at=ended_at.isoformat(),
    )
    store.update_worker(
        worker["worker_id"],
        state="ready",
        last_run_id=run["run_id"],
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
            (datetime.now(timezone.utc).isoformat(), worker["worker_id"]),
        )

    runtime = RestartRefreshedRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        reaped = service.reap_idle_workers_once()

        assert [item["worker_id"] for item in reaped] == [worker["worker_id"]]
        assert runtime.terminated == [worker["worker_id"]]
        released = store.get_worker(worker["worker_id"])
        assert released is not None
        assert released["compute_released_at"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "cancelled"
    finally:
        service.shutdown()


def test_startup_reconcile_keeps_capacity_queued_worker_retry_eligible(tmp_path):
    class MissingProcessRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Restart Capacity Retry",
        "Resume queued capacity work after restart.",
        "openclaw-general",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Capacity Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    # Reproduce the durable state left by the older startup reconciler: the
    # capacity-wait run is still queued, but its no-PID worker was persisted paused.
    store.update_worker_state(worker["worker_id"], "paused")
    assert (store.get_worker(worker["worker_id"]) or {})["pid"] is None
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Retry after host capacity recovers",
        state="queued",
    )
    retry_after = datetime.now(timezone.utc) + timedelta(days=1)
    store.update_run(
        run["run_id"],
        retry_after=retry_after.isoformat(),
        retry_attempts=1,
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )

    alias_resumed_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Alias Resumed Capacity Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    store.add_event(
        project["project_id"],
        alias_resumed_worker["worker_id"],
        None,
        "worker.paused",
        "Worker paused",
    )
    store.add_event(
        project["project_id"],
        alias_resumed_worker["worker_id"],
        None,
        "worker.resumed_by_alias",
        "Worker resumed by alias",
    )
    store.update_worker_state(alias_resumed_worker["worker_id"], "paused")
    alias_resumed_run = store.create_run(
        alias_resumed_worker["worker_id"],
        project["project_id"],
        "Resume the alias-reused capacity wait after legacy reconcile",
        state="queued",
    )
    store.update_run(
        alias_resumed_run["run_id"],
        retry_after=retry_after.isoformat(),
        retry_attempts=1,
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )

    paused_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Operator Paused Capacity Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    store.update_worker_state(paused_worker["worker_id"], "paused")
    paused_run = store.create_run(
        paused_worker["worker_id"],
        project["project_id"],
        "Stay paused despite persisted capacity retry",
        state="paused",
    )
    store.update_run(
        paused_run["run_id"],
        retry_after=retry_after.isoformat(),
        retry_attempts=1,
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )
    store.add_event(
        project["project_id"],
        paused_worker["worker_id"],
        paused_run["run_id"],
        "worker.paused",
        "Worker paused",
    )
    paused_sibling = store.create_run(
        paused_worker["worker_id"],
        project["project_id"],
        "Queued capacity sibling must not override the paused mission",
        state="queued",
    )
    store.update_run(
        paused_sibling["run_id"],
        retry_after=retry_after.isoformat(),
        retry_attempts=1,
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )

    incomplete_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Incomplete Capacity Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    store.update_worker_state(incomplete_worker["worker_id"], "paused")
    incomplete_run = store.create_run(
        incomplete_worker["worker_id"],
        project["project_id"],
        "Do not infer retry eligibility from a capacity label alone",
        state="queued",
    )
    store.update_run(
        incomplete_run["run_id"],
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )

    stale_class_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Stale Capacity Class Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    store.update_worker_state(stale_class_worker["worker_id"], "paused")
    stale_class_run = store.create_run(
        stale_class_worker["worker_id"],
        project["project_id"],
        "Do not recover a retry whose current failure is not capacity",
        state="queued",
    )
    store.update_run(
        stale_class_run["run_id"],
        retry_after=retry_after.isoformat(),
        retry_attempts=1,
        last_retry_class="host_capacity",
        failure_class="provider_temporarily_unavailable",
        failure_retryable=1,
        failure_structured=1,
    )

    manual_idle_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Manually Paused Idle Worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw",
        model="stub/general",
    )
    store.update_worker_state(manual_idle_worker["worker_id"], "paused")
    store.add_event(
        project["project_id"],
        manual_idle_worker["worker_id"],
        None,
        "worker.paused",
        "Worker paused",
    )
    manual_idle_run = store.create_run(
        manual_idle_worker["worker_id"],
        project["project_id"],
        "A capacity queue must not resume an explicit idle pause",
        state="queued",
    )
    store.update_run(
        manual_idle_run["run_id"],
        retry_after=retry_after.isoformat(),
        retry_attempts=1,
        last_retry_class="host_capacity",
        failure_class="host_capacity",
        failure_retryable=1,
        failure_structured=1,
    )

    service = WorkersProjectsService(
        store,
        MissingProcessRuntime(),
        reconcile_on_startup=True,
    )
    try:
        wait_until(
            lambda: (store.get_worker(worker["worker_id"]) or {}).get("state")
            == "ready"
        )
        reconciled = store.get_worker(worker["worker_id"])
        assert reconciled is not None
        assert reconciled["state"] == "ready"
        assert (store.get_worker(alias_resumed_worker["worker_id"]) or {})["state"] == "ready"
        assert (store.get_worker(paused_worker["worker_id"]) or {})["state"] == "paused"
        assert (store.get_worker(incomplete_worker["worker_id"]) or {})["state"] == "paused"
        assert (store.get_worker(stale_class_worker["worker_id"]) or {})["state"] == "paused"
        assert (store.get_worker(manual_idle_worker["worker_id"]) or {})["state"] == "paused"
        due_after_restart = store.list_due_retry_worker_ids(
            now_iso=(retry_after + timedelta(seconds=1)).isoformat()
        )
        assert set(due_after_restart) == {
            worker["worker_id"],
            alias_resumed_worker["worker_id"],
        }
        queued = store.get_run(run["run_id"])
        assert queued is not None
        assert queued["state"] == "queued"
        assert queued["failure_class"] == "host_capacity"
        assert queued["retry_after"] == retry_after.isoformat()
    finally:
        service.shutdown()


def test_startup_reconcile_restarts_crash_requeued_run_but_preserves_operator_pause(
    tmp_path,
):
    class MissingReconcileProcessRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Crash Restart Recovery",
        "Restart work whose prior dispatch ownership was lost.",
        "codex-cli",
    )

    def crashed_running_worker(name: str, suffix: str) -> tuple[dict, dict]:
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name=name,
            role="host worker",
            profile="codex-cli",
            backend="codex-cli",
            runtime="codex-cli",
            model="stub/codex-cli",
        )
        run = create_truthfully_invoked_test_run(
            store,
            worker,
            project["project_id"],
            f"Finish {suffix} after process restart",
            suffix=suffix,
            executor_id=f"crashed-executor-{suffix}",
        )
        lease = store.get_active_host_run_lease_for_run(run["run_id"])
        assert lease is not None
        released = store.release_host_run_lease(
            lease["lease_id"],
            executor_id=f"crashed-executor-{suffix}",
            reason="synthetic_process_crash",
        )
        assert released is not None
        return worker, run

    _, automatic_run = crashed_running_worker(
        "Automatically Recovered Worker", "automatic"
    )
    paused_worker, paused_run = crashed_running_worker(
        "Operator Paused Worker", "operator-paused"
    )
    failed_paused_worker, failed_paused_run = crashed_running_worker(
        "Failed State Operator Paused Worker", "failed-operator-paused"
    )
    store.add_event(
        project["project_id"],
        paused_worker["worker_id"],
        paused_run["run_id"],
        "worker.paused",
        "Worker paused",
    )
    # Reproduce a crash split where durable operator intent landed before the
    # worker/run projection. Startup must honor that event from any worker state.
    assert (store.get_worker(paused_worker["worker_id"]) or {})["state"] == "running"
    store.add_event(
        project["project_id"],
        failed_paused_worker["worker_id"],
        failed_paused_run["run_id"],
        "worker.paused",
        "Worker paused",
    )
    store.update_worker_state(failed_paused_worker["worker_id"], "failed")

    service = WorkersProjectsService(
        store,
        MissingReconcileProcessRuntime(),
        reconcile_on_startup=True,
    )
    try:
        wait_until(
            lambda: (store.get_run(automatic_run["run_id"]) or {}).get("state")
            == "completed",
            timeout=3,
        )

        recovered = store.get_run(automatic_run["run_id"])
        assert recovered is not None
        assert recovered["output_text"].startswith("STUB_OK:")
        recovered_attempts = store.list_run_attempts(automatic_run["run_id"])
        assert [attempt["state"] for attempt in recovered_attempts] == [
            "retry_queued",
            "completed",
        ]
        assert recovered_attempts[0]["terminal_reason"] == (
            "running_invariant_reconciled"
        )

        preserved = store.get_run(paused_run["run_id"])
        assert preserved is not None
        assert preserved["state"] == "queued"
        assert preserved["last_retry_class"] == "running_invariant_reconciled"
        assert (store.get_worker(paused_worker["worker_id"]) or {})["state"] == (
            "paused"
        )
        assert len(store.list_run_attempts(paused_run["run_id"])) == 1
        failed_preserved = store.get_run(failed_paused_run["run_id"])
        assert failed_preserved is not None
        assert failed_preserved["state"] == "queued"
        assert (store.get_worker(failed_paused_worker["worker_id"]) or {})[
            "state"
        ] == "paused"
        assert len(store.list_run_attempts(failed_paused_run["run_id"])) == 1
    finally:
        service.shutdown()


def test_restart_retry_reconciliation_cannot_overwrite_concurrent_operator_pause(
    tmp_path,
    monkeypatch,
):
    class NoSchedulerWorkersProjectsService(WorkersProjectsService):
        def _process_scheduler_cycle(self) -> None:
            return

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner",
        "Atomic Restart Recovery",
        "Preserve operator control while restart recovery races.",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Concurrently Paused Worker",
        role="host worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    run = create_truthfully_invoked_test_run(
        store,
        worker,
        project["project_id"],
        "Do not resume after the operator pause wins.",
        suffix="concurrent-pause",
        executor_id="crashed-executor-concurrent-pause",
    )
    lease = store.get_active_host_run_lease_for_run(run["run_id"])
    assert lease is not None
    assert store.release_host_run_lease(
        lease["lease_id"],
        executor_id="crashed-executor-concurrent-pause",
        reason="synthetic_process_crash",
    )

    service = NoSchedulerWorkersProjectsService(
        store,
        StubRuntime(),
        reconcile_on_startup=False,
    )
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    entered_retry_projection = Event()
    allow_retry_projection = Event()
    original_reconcile = store.reconcile_automatic_retry_worker

    def reconcile_after_concurrent_pause(worker_id: str):
        entered_retry_projection.set()
        assert allow_retry_projection.wait(2)
        return original_reconcile(worker_id)

    monkeypatch.setattr(
        store,
        "reconcile_automatic_retry_worker",
        reconcile_after_concurrent_pause,
    )
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            service.reconcile_all_workers()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = Thread(target=reconcile)
    thread.start()
    try:
        assert entered_retry_projection.wait(2)
        store.add_event(
            project["project_id"],
            worker["worker_id"],
            run["run_id"],
            "worker.paused",
            "Worker paused",
        )
        store.update_worker_state(worker["worker_id"], "paused")
        allow_retry_projection.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert errors == []
        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "paused"
        durable_run = store.get_run(run["run_id"])
        assert durable_run is not None
        assert durable_run["state"] == "queued"
        assert durable_run["last_retry_class"] == "running_invariant_reconciled"
        assert len(store.list_run_attempts(run["run_id"])) == 1
    finally:
        allow_retry_projection.set()
        thread.join(timeout=2)
        service.shutdown()


def test_reconcile_interrupts_active_run_when_worker_process_is_missing(tmp_path):
    class MissingProcessRuntime(StubRuntime):
        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            info = super().ensure_worker_ready(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Orphan Cleanup", "Clean up stale active runs", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Codex Host",
        role="host worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    store.update_worker_state(worker["worker_id"], "running")
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Long host task",
        state=RunRestorationState.RUNNING,
    )

    # Import the persisted snapshot before the service owns queue dispatch.
    service = WorkersProjectsService(
        store,
        MissingProcessRuntime(),
        reconcile_on_startup=False,
    )
    try:
        service.reconcile_all_workers()
    finally:
        service.shutdown()

    reconciled_run = store.get_run(run["run_id"])
    assert reconciled_run["state"] == "interrupted"
    assert reconciled_run["ended_at"]
    assert "process was not running" in reconciled_run["error_text"]
    assert store.get_worker(worker["worker_id"])["state"] == "paused"
    assert store.metrics()["active_runs"] == 0
    assert any(event["event_type"] == "run.orphaned" for event in store.list_events(worker["worker_id"]))


def test_enterprise_startup_requeues_durable_first_run_after_restart(tmp_path, monkeypatch):
    class RestartRuntime(StubRuntime):
        def run_task(self, worker: dict, instruction: str, run_id: str | None = None) -> str:
            publish_in_process_test_start(worker)
            return f"FINAL REPORT:\nRecovered {instruction}"

    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_RECONCILE_ON_STARTUP", "true")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Restart", "Resume queued work", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Restart worker",
        role="general",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    store.update_worker_state(worker["worker_id"], "starting")
    run = store.create_run(worker["worker_id"], project["project_id"], "queued work", state="queued")

    service = WorkersProjectsService(store, RestartRuntime())
    try:
        wait_until(lambda: store.get_run(run["run_id"])["state"] == "completed")
    finally:
        service.shutdown()

    assert store.get_run(run["run_id"])["output_text"] == "FINAL REPORT:\nRecovered queued work"
    assert store.get_worker(worker["worker_id"])["state"] == "ready"


def test_passive_rehearsal_does_not_run_callbacks_schedules_or_reapers(tmp_path, monkeypatch):
    callback_requests: list[str] = []

    def fake_post(url: str, **_kwargs):
        callback_requests.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    monkeypatch.setenv("GLASSHIVE_RECONCILE_ON_STARTUP", "true")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "1")
    monkeypatch.setattr(service_module.httpx, "post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Passive rehearsal", "Inspect cloned state", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Passive worker",
        role="general",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "https://callback.example.test/glasshive",
                "hmac_secret": "synthetic-callback-secret",
            }
        },
    )
    retained_run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Retained completed work",
        state="completed",
    )

    service = WorkersProjectsService(store, StubRuntime())
    try:
        service._emit_callback(
            worker,
            "run.completed",
            run=retained_run,
            message="Do not deliver from rehearsal",
            submit_delivery=False,
        )
        service._emit_callback(
            worker,
            "run.started",
            run=retained_run,
            message="Do not deliver from rehearsal",
            submit_delivery=False,
        )
        service.create_recurring_schedule(
            worker["worker_id"],
            "Do not execute from rehearsal",
            recurrence_type="once",
            first_run_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            misfire_grace_seconds=300,
        )
        time.sleep(1.2)
    finally:
        service.shutdown()

    assert callback_requests == []
    pending_callbacks = store.list_pending_callbacks(limit=10)
    assert len(pending_callbacks) == 2
    assert {record["status"] for record in pending_callbacks} == {"pending"}
    assert [item["run_id"] for item in store.list_runs_for_worker(worker["worker_id"])] == [
        retained_run["run_id"]
    ]
    assert service._callback_retry_thread is None
    assert service._idle_reaper_thread is None
    assert service._scheduler_thread is None


def test_reconcile_orphaned_running_run_emits_interrupted_callback(
    tmp_path, background_consumers_disabled
):
    class MissingProcessRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            # A host CLI can exit before its processor has parsed and durably stored the
            # successful result. During that finalization window there is no live PID.
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, MissingProcessRuntime(), reconcile_on_startup=False)
    try:
        project = store.create_project(
            "owner",
            "Finalization Race",
            "Do not orphan a locally owned run while its result is being finalized",
            "codex-cli",
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Long host task",
            state=RunRestorationState.RUNNING,
        )

        with service._processors_lock:
            service._active_processors.add(worker["worker_id"])

        service.reconcile_all_workers()
    finally:
        with service._processors_lock:
            service._active_processors.discard(worker["worker_id"])
        service.shutdown()

    reconciled_run = store.get_run(run["run_id"])
    assert reconciled_run["state"] == "running"
    assert store.get_worker(worker["worker_id"])["state"] == "running"
    assert not any(
        event["event_type"] == "run.orphaned"
        for event in store.list_events(worker["worker_id"])
    )


def test_foreign_reconcile_preserves_run_owned_by_live_recorded_host_process(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime_root = tmp_path / "runtime-state"
    store = Store(str(db_path))
    owner_runtime = HostCodexCliRuntime(base_dir=str(runtime_root))
    foreign_runtime = HostCodexCliRuntime(base_dir=str(runtime_root))
    service = WorkersProjectsService(store, foreign_runtime, reconcile_on_startup=False)
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        project = store.create_project(
            "owner",
            "Cross-process ownership",
            "Do not orphan a host run owned by another service instance",
            "codex-cli",
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            execution_mode="host",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        workspace_dir = tmp_path / "live-owner-workspace"
        workspace_dir.mkdir()
        store.update_worker(worker["worker_id"], workspace_dir=str(workspace_dir))
        worker = store.get_worker(worker["worker_id"]) or worker
        store.update_worker_state(worker["worker_id"], "running")
        run = create_truthfully_invoked_test_run(
            store,
            worker,
            project["project_id"],
            "Long host task",
            suffix="foreign-live-owner",
        )
        heartbeat_path = tmp_path / "active-run.json"
        heartbeat_path.write_text(
            json.dumps(
                {
                    "schema": "glasshive.active_run.v1",
                    "run_id": run["run_id"],
                    "state": "running",
                    "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "process_pid": process.pid,
                }
            )
        )
        owner_runtime._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run['run_id'][:12]}",
                "run_id": run["run_id"],
                "process_pid": process.pid,
                "owner_pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "stdout_path": str(tmp_path / "still-running.stdout.log"),
                "stderr_path": str(tmp_path / "still-running.stderr.log"),
                "exit_path": str(tmp_path / "still-running.exit-code"),
                "heartbeat_path": str(heartbeat_path),
            },
        )

        assert foreign_runtime._active_pid(worker["worker_id"], run["run_id"]) == process.pid
        service.reconcile_all_workers()

        assert store.get_run(run["run_id"])["state"] == "running"
        assert store.get_worker(worker["worker_id"])["state"] == "running"
        assert not any(
            event["event_type"] == "run.orphaned"
            for event in store.list_events(worker["worker_id"])
        )
        assert not any(
            event["event_type"] == "worker.reconcile_failed"
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        process.terminate()
        process.wait(timeout=5)
        owner_runtime._clear_active_session(worker["worker_id"] if "worker" in locals() else "")
        service.shutdown()


def test_foreign_reconcile_rejects_live_child_when_owner_process_is_gone(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime_root = tmp_path / "runtime-state"
    store = Store(str(db_path))
    owner_runtime = HostCodexCliRuntime(base_dir=str(runtime_root))
    foreign_runtime = HostCodexCliRuntime(base_dir=str(runtime_root))
    service = WorkersProjectsService(store, foreign_runtime, reconcile_on_startup=False)
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    exited_owner = subprocess.Popen(["true"])
    exited_owner.wait(timeout=5)
    try:
        project = store.create_project(
            "owner",
            "Expired cross-process owner",
            "Do not preserve a child process after its owning service exits",
            "codex-cli",
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            execution_mode="host",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        workspace_dir = tmp_path / "dead-owner-workspace"
        workspace_dir.mkdir()
        store.update_worker(worker["worker_id"], workspace_dir=str(workspace_dir))
        worker = store.get_worker(worker["worker_id"]) or worker
        store.update_worker_state(worker["worker_id"], "running")
        run = create_truthfully_invoked_test_run(
            store,
            worker,
            project["project_id"],
            "Host task whose service owner exited",
            suffix="foreign-dead-owner",
        )
        heartbeat_path = tmp_path / "expired-owner-active-run.json"
        heartbeat_path.write_text(
            json.dumps(
                {
                    "schema": "glasshive.active_run.v1",
                    "run_id": run["run_id"],
                    "state": "running",
                    "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "process_pid": child.pid,
                }
            )
        )
        owner_runtime._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run['run_id'][:12]}",
                "run_id": run["run_id"],
                "process_pid": child.pid,
                "owner_pid": exited_owner.pid,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "stdout_path": str(tmp_path / "expired-owner.stdout.log"),
                "stderr_path": str(tmp_path / "expired-owner.stderr.log"),
                "exit_path": str(tmp_path / "expired-owner.exit-code"),
                "heartbeat_path": str(heartbeat_path),
            },
        )

        assert foreign_runtime._active_pid(worker["worker_id"], run["run_id"]) is None
        service.reconcile_all_workers()

        interrupted = store.get_run(run["run_id"])
        assert interrupted["state"] == "interrupted"
        assert interrupted["failure_class"] == "provider_temporarily_unavailable"
        assert interrupted["failure_retryable"] == 1
        assert interrupted["failure_structured"] == 1
        wait_until(lambda: child.poll() is not None)
        assert owner_runtime._read_active_session(worker["worker_id"]) is None
    finally:
        child.terminate()
        child.wait(timeout=5)
        owner_runtime._clear_active_session(worker["worker_id"] if "worker" in locals() else "")
        service.shutdown()


def test_fresh_owner_heartbeat_preserves_cross_process_finalization_lease(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-state"))
    child = subprocess.Popen(["true"], start_new_session=True)
    child.wait(timeout=5)
    worker_id = "wrk_finalization_lease"
    run_id = "run_finalization_lease"
    heartbeat_path = tmp_path / "finalization-active-run.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "schema": "glasshive.active_run.v1",
                "run_id": run_id,
                "state": "running",
                "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "process_pid": child.pid,
            }
        )
    )
    runtime._write_active_session(
        worker_id,
        {
            "session_name": f"conversation-{run_id[:12]}",
            "run_id": run_id,
            "process_pid": child.pid,
            "owner_pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "heartbeat_path": str(heartbeat_path),
        },
    )
    try:
        assert runtime._active_pid(worker_id, run_id) == child.pid
    finally:
        runtime._clear_active_session(worker_id)


def test_stale_owner_heartbeat_cannot_pin_a_reused_live_pid(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-state"))
    unrelated_process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    worker_id = "wrk_stale_lease"
    run_id = "run_stale_lease"
    heartbeat_path = tmp_path / "stale-active-run.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "schema": "glasshive.active_run.v1",
                "run_id": run_id,
                "state": "running",
                "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                "process_pid": unrelated_process.pid,
            }
        )
    )
    runtime._write_active_session(
        worker_id,
        {
            "session_name": f"conversation-{run_id[:12]}",
            "run_id": run_id,
            "process_pid": unrelated_process.pid,
            "owner_pid": os.getpid(),
            "started_at": "2000-01-01T00:00:00+00:00",
            "heartbeat_path": str(heartbeat_path),
        },
    )
    try:
        assert runtime._active_pid(worker_id, run_id) is None
    finally:
        unrelated_process.terminate()
        unrelated_process.wait(timeout=5)
        runtime._clear_active_session(worker_id)


def test_reconcile_unproven_running_run_downgrades_without_callback(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner", "Orphan Callback", "Preserve truthful pre-dispatch state", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Codex Host",
        role="host worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.local/glasshive",
                "hmac_secret": "callback-secret",
            }
        },
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Long host task"
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE runs SET state = 'running' WHERE run_id = ?",
            (run["run_id"],),
        )

    assert store.reconcile_invalid_running_runs() == 1
    assert store.get_run(run["run_id"])["state"] == "queued"
    assert store.list_pending_callbacks() == []


def test_reconcile_collects_completed_run_before_orphaning_missing_process(
    tmp_path, background_consumers_disabled
):
    class CompletedMissingProcessRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            info.pid = None
            return info

        def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
            return {
                "state": "completed",
                "output_text": "FINAL REPORT:\nRecovered completed output.",
            }

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, CompletedMissingProcessRuntime())
    try:
        project = store.create_project("owner", "Reconcile Recovery", "Collect completed run", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                    "surface": "web",
                }
            },
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Long host task",
            state=RunRestorationState.RUNNING,
        )

        service.reconcile_all_workers()
    finally:
        service.shutdown()

    recovered_run = store.get_run(run["run_id"])
    assert recovered_run["state"] == "completed"
    assert "Recovered completed output" in recovered_run["output_text"]
    assert not any(event["event_type"] == "run.orphaned" for event in store.list_events(worker["worker_id"]))
    callbacks = [row for row in store.list_pending_callbacks() if row["run_id"] == run["run_id"]]
    assert len(callbacks) == 1
    payload = json.loads(callbacks[0]["payload_json"])
    assert payload["event"] == "run.completed"
    assert payload["run_state"] == "completed"


def test_reconcile_collects_completed_run_even_when_takeover_session_has_pid(
    tmp_path, background_consumers_disabled
):
    class CompletedStillAttachedRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            info.pid = 4242
            return info

        def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
            return {
                "state": "completed",
                "output_text": "FINAL REPORT:\nRecovered from attached completed session.",
            }

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, CompletedStillAttachedRuntime())
    try:
        project = store.create_project("owner", "Attached Recovery", "Collect completed attached run", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Worker",
            role="worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Attached task",
            state=RunRestorationState.RUNNING,
        )

        service.reconcile_all_workers()
    finally:
        service.shutdown()

    recovered_run = store.get_run(run["run_id"])
    assert recovered_run["state"] == "completed"
    assert "attached completed session" in recovered_run["output_text"]
    assert store.get_worker(worker["worker_id"])["state"] == "ready"
    assert store.metrics()["active_runs"] == 0
    assert not any(event["event_type"] == "run.orphaned" for event in store.list_events(worker["worker_id"]))


def test_reconcile_continues_when_one_completed_run_collection_raises(
    tmp_path, background_consumers_disabled
):
    class PartiallyFailingRuntime(StubRuntime):
        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            info = super().reconcile_worker(worker)
            info.pid = None
            return info

        def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
            if worker.get("name") == "Broken Worker":
                raise RuntimeError("synthetic collection failure")
            return {
                "state": "completed",
                "output_text": "FINAL REPORT:\nSecond worker still reconciled.",
            }

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, PartiallyFailingRuntime())
    try:
        project = store.create_project("owner", "Reconcile Isolation", "Continue after worker error", "codex-cli")
        broken_worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Broken Worker",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
        )
        healthy_worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Healthy Worker",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
        )
        store.update_worker_state(broken_worker["worker_id"], "running")
        store.update_worker_state(healthy_worker["worker_id"], "running")
        broken_run = create_truthfully_invoked_test_run(
            store,
            broken_worker,
            project["project_id"],
            "Broken host task",
            suffix="broken-collection",
        )
        healthy_run = create_truthfully_invoked_test_run(
            store,
            healthy_worker,
            project["project_id"],
            "Healthy host task",
            suffix="healthy-collection",
        )

        service.reconcile_all_workers()
    finally:
        service.shutdown()

    assert store.get_run(broken_run["run_id"])["state"] == "running"
    recovered_run = store.get_run(healthy_run["run_id"])
    assert recovered_run["state"] == "completed"
    assert "Second worker still reconciled" in recovered_run["output_text"]
    assert any(
        event["event_type"] == "worker.reconcile_failed"
        for event in store.list_events(broken_worker["worker_id"])
    )


def test_reconcile_skips_idle_paused_workers_and_repairs_inconsistent_runs(tmp_path):
    class CountingRuntime(StubRuntime):
        def __init__(self) -> None:
            self.reconciled_worker_ids: list[str] = []
            self.paused_worker_ids: list[str] = []

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            self.reconciled_worker_ids.append(worker["worker_id"])
            return super().reconcile_worker(worker)

        def pause_worker(self, worker: dict) -> RuntimeInfo:
            self.paused_worker_ids.append(worker["worker_id"])
            return super().pause_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Startup Efficiency", "Avoid Docker scans for paused workspaces", "codex-cli")
    paused_idle = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Paused Idle",
        role="saved worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    paused_with_run = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Paused With Run",
        role="inconsistent worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    ready_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Ready Worker",
        role="active worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    active_run = create_truthfully_invoked_test_run(
        store,
        paused_with_run,
        project["project_id"],
        "stale run",
        suffix="paused-reconcile",
    )
    store.update_worker_state(paused_idle["worker_id"], "paused")
    store.update_worker_state(ready_worker["worker_id"], "ready")
    store.update_worker_state(paused_with_run["worker_id"], "paused")

    runtime = CountingRuntime()
    service = WorkersProjectsService(
        store,
        runtime,
        start_background_consumers=False,
    )
    try:
        service.reconcile_all_workers()
    finally:
        service.shutdown()

    assert runtime.reconciled_worker_ids == [ready_worker["worker_id"]]
    assert runtime.paused_worker_ids == [paused_with_run["worker_id"]]
    assert store.get_run(active_run["run_id"])["state"] == "paused"
    assert any(event["event_type"] == "run.paused" for event in store.list_events(paused_with_run["worker_id"]))


def test_reconcile_does_not_regress_completed_run_when_process_is_missing(
    tmp_path, background_consumers_disabled
):
    class MissingProcessRuntime(StubRuntime):
        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            info = super().ensure_worker_ready(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, MissingProcessRuntime())
    try:
        project = store.create_project("owner", "Orphan Race", "Avoid regressing completed runs", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Finishing host task",
            state=RunRestorationState.RUNNING,
        )
        store.finalize_run(run["run_id"], "completed", output_text="done")

        service.reconcile_all_workers()
    finally:
        service.shutdown()

    reconciled_run = store.get_run(run["run_id"])
    assert reconciled_run["state"] == "completed"
    assert reconciled_run["output_text"] == "done"
    assert not any(event["event_type"] == "run.orphaned" for event in store.list_events(worker["worker_id"]))


def test_worker_find_or_resume_reuses_alias_and_preserves_host_fields(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Host Workers",
            "goal": "Reuse named host workers.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    payload = {
        "owner_id": "demo-owner",
        "name": "Codex Host",
        "role": "coding",
        "profile": "codex-cli",
        "backend": "openclaw",
        "execution_mode": "host",
        "alias": "codex-main",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    first = client.post(f"/v1/projects/{project['project_id']}/workers/find-or-resume", json=payload)
    second = client.post(f"/v1/projects/{project['project_id']}/workers/find-or-resume", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["worker_id"] == first.json()["worker_id"]
    assert second.json()["execution_mode"] == "host"
    assert second.json()["alias"] == "codex-main"
    assert second.json()["workspace_root"] == str(tmp_path / "workspaces")


def _scheduled_workbench_worker_payload(
    *,
    run_id: str,
    alias: str = "health-context",
    fallback: dict[str, str] | None = None,
) -> dict:
    fallback_route = fallback if fallback is not None else {
        "worker_profile": "claude-code",
        "model": "stub/claude-code",
        "reasoning_effort": "max",
    }
    payload = {
        "owner_id": "synthetic-owner",
        "name": "Scheduled health context",
        "role": "Execute one private scheduled prompt.",
        "profile": "codex-cli",
        "backend": "codex-cli",
        "execution_mode": "docker",
        "alias": alias,
        "bootstrap_profile": "prompt-workbench-scheduled-v1",
        "bootstrap_bundle": {
            "viventium_execution_authority_request": {
                "version": 1,
                "kind": "prompt_workbench_scheduled",
                "execution_mode": "docker",
                "primary": {
                    "worker_profile": "codex-cli",
                    "model": "stub/codex-cli",
                    "reasoning_effort": "xhigh",
                },
                **({"fallback": fallback_route} if fallback_route else {}),
            },
            "callbacks": {
                "events_webhook_url": (
                    "http://127.0.0.1:7010/internal/scheduled-prompts/"
                    "glasshive-callback"
                ),
                "hmac_secret": "synthetic-callback-secret",
                "user_id": "synthetic-owner",
                "conversation_id": "workbench-scheduled-prompt:synthetic-task",
                "parent_message_id": "scheduled-prompt:synthetic-task",
                "message_id": run_id,
                "surface": "workbench",
                "scheduled_prompt_run_id": run_id,
                "scheduled_prompt_task_id": "synthetic-task",
            },
            "env": {
                "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
                **(
                    {"WPR_CLAUDE_CODE_EFFORT": fallback_route["reasoning_effort"]}
                    if fallback_route
                    and fallback_route["worker_profile"] == "claude-code"
                    else {}
                ),
            },
            "agents_md": "Execute only the scheduled prompt.",
            "files": [
                {
                    "scope": "workspace",
                    "path": f"scheduled-prompt/{run_id}.md",
                    "content": "Synthetic scheduled prompt.",
                }
            ],
        },
        "start_synchronously": False,
    }
    return payload


def test_scheduled_workbench_persists_default_claude_effort_and_derives_callback_owner(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret"
    )
    monkeypatch.setenv(
        "GLASSHIVE_ALLOWED_WORKER_PROFILES",
        "codex-cli,claude-code,openclaw-general",
    )
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "default")
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )

    class ScheduledRouteRuntime(StubRuntime):
        def resolve_model(self, profile: str, execution_mode="docker") -> str:
            return f"stub/{profile}"

    db_path = tmp_path / "runtime.db"
    app = create_app(
        str(db_path), runtime_backend="stub", runtime=ScheduledRouteRuntime()
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer synthetic-service-token"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "synthetic-owner",
            "title": "Scheduled prompts",
            "goal": "Preserve exact fallback and callback identity.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    run_id = "scheduled-run-default-effort"
    response = client.post(
        f"/v1/projects/{project['project_id']}/workers/find-or-resume",
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id=run_id,
            fallback={
                "worker_profile": "claude-code",
                "model": "stub/claude-code",
                "reasoning_effort": "default",
            },
        ),
    )

    assert response.status_code == 200
    store = Store(str(db_path))
    worker = store.get_worker(response.json()["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert bundle["env"] == {
        "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
        "WPR_CLAUDE_CODE_EFFORT": "default",
    }
    assert bundle["callbacks"] == {
        "events_webhook_url": (
            "http://127.0.0.1:7010/internal/scheduled-prompts/"
            "glasshive-callback"
        ),
        "hmac_secret": "synthetic-callback-secret",
        "origin_ref": run_id,
    }
    assert "glasshive_capability_authorization" not in bundle
    assert "GLASSHIVE_CAPABILITY_BROKER_TOKEN" not in str(bundle)
    resolved = app.state.service._callback_config_for(worker)
    assert resolved == {
        **bundle["callbacks"],
        "user_id": "synthetic-owner",
        "message_id": run_id,
        "scheduled_prompt_run_id": run_id,
        "surface": "workbench",
    }


def test_scheduled_workbench_service_bearer_cannot_replace_callback_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "canonical-synthetic-secret"
    )
    monkeypatch.setenv(
        "GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code"
    )
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )
    broker_calls: list[str] = []

    def forbidden_prepare(url, **_kwargs):
        broker_calls.append(url)
        raise AssertionError("Rejected callback authority must not reach Core prepare")

    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post", forbidden_prepare
    )
    db_path = tmp_path / "runtime.db"

    class ScheduledRouteRuntime(StubRuntime):
        def resolve_model(self, profile: str, execution_mode="docker") -> str:
            return f"stub/{profile}"

    client = TestClient(
        create_app(
            str(db_path), runtime_backend="stub", runtime=ScheduledRouteRuntime()
        )
    )
    headers = {"Authorization": "Bearer synthetic-service-token"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "synthetic-owner",
            "title": "Scheduled prompts",
            "goal": "Reject caller-replaced callback authority.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    payload = _scheduled_workbench_worker_payload(
        run_id="scheduled-run-wrong-callback-secret"
    )
    payload["bootstrap_bundle"]["callbacks"]["hmac_secret"] = (
        "caller-replaced-secret"
    )

    response = client.post(
        f"/v1/projects/{project['project_id']}/workers/find-or-resume",
        headers=headers,
        json=payload,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "parallel_execution_isolation_required"
    )
    assert Store(str(db_path)).list_workers(project["project_id"]) == []
    assert broker_calls == []


def test_scheduled_workbench_callback_config_derives_owner_from_persisted_worker(
    tmp_path,
):
    run_id = "scheduled-run-callback-owner"
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = service.create_project(
        "synthetic-owner",
        "Scheduled callback identity",
        "Return terminal state to the exact owner.",
        "codex-cli",
    )
    try:
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="synthetic-owner",
            name="Scheduled callback worker",
            role="scheduled prompt worker",
            profile="codex-cli",
            backend="codex-cli",
            execution_mode="docker",
            bootstrap_profile="clean-room",
            bootstrap_bundle={
                "execution_policy": "parallel-clean-room-v1",
                "viventium_launch_authority": {
                    "version": 1,
                    "kind": "prompt_workbench_scheduled",
                    "execution_mode": "docker",
                },
                "callbacks": {
                    "events_webhook_url": (
                        "http://127.0.0.1:7010/internal/scheduled-prompts/"
                        "glasshive-callback"
                    ),
                    "hmac_secret": "synthetic-callback-secret",
                    "origin_ref": run_id,
                },
            },
            start_synchronously=False,
        )
        persisted = store.get_worker(worker["worker_id"])

        assert service._callback_config_for(persisted) == {
            "events_webhook_url": (
                "http://127.0.0.1:7010/internal/scheduled-prompts/"
                "glasshive-callback"
            ),
            "hmac_secret": "synthetic-callback-secret",
            "origin_ref": run_id,
            "user_id": "synthetic-owner",
            "message_id": run_id,
            "scheduled_prompt_run_id": run_id,
            "surface": "workbench",
        }
    finally:
        service.shutdown()


def test_scheduled_workbench_fallback_authority_is_server_constructed_and_replaced(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret"
    )
    monkeypatch.setenv(
        "GLASSHIVE_ALLOWED_WORKER_PROFILES",
        "codex-cli,claude-code,openclaw-general",
    )
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )
    db_path = tmp_path / "runtime.db"

    class MutableRouteRuntime(StubRuntime):
        fallback_model = "stub/claude-code"

        def resolve_model(self, profile: str) -> str:
            if profile == "claude-code":
                return self.fallback_model
            return super().resolve_model(profile)

    runtime = MutableRouteRuntime()
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)
    client = TestClient(app)
    headers = {"Authorization": "Bearer synthetic-service-token"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "synthetic-owner",
            "title": "Scheduled prompts",
            "goal": "Execute isolated scheduled prompts.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    endpoint = f"/v1/projects/{project['project_id']}/workers/find-or-resume"

    legacy = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "synthetic-owner",
            "name": "Legacy health context",
            "role": "Legacy worker",
            "profile": "codex-cli",
            "execution_mode": "docker",
            "alias": "health-context",
            "start_synchronously": False,
        },
    )
    assert legacy.status_code == 201

    first = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(run_id="scheduled-run-1"),
    )
    assert first.status_code == 200
    worker_id = first.json()["worker_id"]
    assert worker_id != legacy.json()["worker_id"]
    assert first.json()["alias"].startswith(
        "health-context--prompt-workbench-scheduled-"
    )
    store = Store(str(db_path))
    first_worker = store.get_worker(worker_id)
    first_bundle = json.loads(first_worker["bootstrap_bundle_json"])
    assert first_worker["bootstrap_profile"] == "clean-room"
    assert first_bundle["execution_policy"] == "parallel-clean-room-v1"
    assert first_bundle["viventium_launch_authority"] == {
        "version": 1,
        "kind": "prompt_workbench_scheduled",
        "execution_mode": "docker",
        "fallback_worker_profile": "claude-code",
    }
    assert first_bundle["callbacks"] == {
        "events_webhook_url": (
            "http://127.0.0.1:7010/internal/scheduled-prompts/glasshive-callback"
        ),
        "hmac_secret": "synthetic-callback-secret",
        "origin_ref": "scheduled-run-1",
    }
    assert first_bundle["env"] == {
        "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
        "WPR_CLAUDE_CODE_EFFORT": "max",
    }
    assert "viventium_execution_authority_request" not in first_bundle
    assert "glasshive_capability_broker" not in first_bundle
    resolved_callbacks = app.state.service._callback_config_for(first_worker)
    assert resolved_callbacks["message_id"] == "scheduled-run-1"
    assert resolved_callbacks["scheduled_prompt_run_id"] == "scheduled-run-1"
    assert resolved_callbacks["surface"] == "workbench"

    same = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id="scheduled-run-2",
        ),
    )
    assert same.status_code == 200
    assert same.json()["worker_id"] == worker_id
    same_bundle = json.loads(store.get_worker(worker_id)["bootstrap_bundle_json"])
    assert same_bundle["callbacks"]["origin_ref"] == "scheduled-run-2"
    assert {entry["path"] for entry in same_bundle["files"]} == {
        "scheduled-prompt/scheduled-run-2.md"
    }

    runtime.fallback_model = "stub/claude-code-v2"
    changed = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id="scheduled-run-3",
            fallback={
                "worker_profile": "claude-code",
                "model": "stub/claude-code-v2",
                "reasoning_effort": "max",
            },
        ),
    )
    assert changed.status_code == 200
    assert changed.json()["worker_id"] != worker_id
    assert changed.json()["alias"] != first.json()["alias"]

    monkeypatch.delenv("GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE")
    removed = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id="scheduled-run-4",
            fallback={},
        ),
    )
    assert removed.status_code == 200
    assert removed.json()["worker_id"] not in {
        worker_id,
        changed.json()["worker_id"],
    }
    removed_bundle = json.loads(
        store.get_worker(removed.json()["worker_id"])["bootstrap_bundle_json"]
    )
    assert "fallback_worker_profile" not in removed_bundle[
        "viventium_launch_authority"
    ]
    assert removed_bundle["callbacks"]["origin_ref"] == "scheduled-run-4"
    assert {entry["path"] for entry in removed_bundle["files"]} == {
        "scheduled-prompt/scheduled-run-4.md"
    }

    tampered_bundle = dict(same_bundle)
    tampered_bundle["viventium_launch_authority"] = {
        "version": 1,
        "kind": "prompt_workbench_scheduled",
        "execution_mode": "docker",
    }
    store.update_worker(
        worker_id,
        bootstrap_bundle_json=json.dumps(tampered_bundle),
    )
    runtime.fallback_model = "stub/claude-code"
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )
    mismatch = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(run_id="scheduled-run-5"),
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == (
        "scheduled_authority_fingerprint_mismatch"
    )


def test_scheduled_workbench_reuses_only_terminal_exact_fallback_and_restores_primary(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret"
    )
    monkeypatch.setenv(
        "GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code"
    )
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )

    class ScheduledRouteRuntime(StubRuntime):
        def resolve_model(self, profile: str, execution_mode="docker") -> str:
            return f"stub/{profile}"

    db_path = tmp_path / "runtime.db"
    app = create_app(
        str(db_path), runtime_backend="stub", runtime=ScheduledRouteRuntime()
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer synthetic-service-token"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "synthetic-owner",
            "title": "Scheduled fallback reuse",
            "goal": "Reuse only an exact terminal fallback route.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    endpoint = f"/v1/projects/{project['project_id']}/workers/find-or-resume"
    first_payload = _scheduled_workbench_worker_payload(
        run_id="scheduled-terminal-fallback-1"
    )
    first = client.post(endpoint, headers=headers, json=first_payload)
    assert first.status_code == 200
    worker_id = first.json()["worker_id"]
    store = Store(str(db_path))
    service = app.state.service
    fallback_fields = {
        "profile": "claude-code",
        "backend": service._legacy_backend_label("claude-code", "docker", ""),
        "runtime": service._initial_runtime_label("claude-code", "docker"),
        "model": "stub/claude-code",
    }
    store.update_worker(worker_id, **fallback_fields)
    store.create_run(
        worker_id,
        project["project_id"],
        "Synthetic terminal fallback attempt.",
        state="failed",
    )

    resumed = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id="scheduled-terminal-fallback-2"
        ),
    )

    assert resumed.status_code == 200
    assert resumed.json()["worker_id"] == worker_id
    restored = store.get_worker(worker_id)
    assert {
        key: restored[key] for key in ("profile", "backend", "runtime", "model")
    } == {
        "profile": "codex-cli",
        "backend": service._legacy_backend_label(
            "codex-cli", "docker", "codex-cli"
        ),
        "runtime": service._initial_runtime_label("codex-cli", "docker"),
        "model": "stub/codex-cli",
    }
    restored_bundle = json.loads(restored["bootstrap_bundle_json"])
    assert restored_bundle["callbacks"]["origin_ref"] == (
        "scheduled-terminal-fallback-2"
    )

    store.update_worker(worker_id, **fallback_fields)
    store.create_run(
        worker_id,
        project["project_id"],
        "Synthetic still-queued fallback attempt.",
        state="queued",
    )
    active_fallback = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id="scheduled-terminal-fallback-3"
        ),
    )
    assert active_fallback.status_code == 409
    assert active_fallback.json()["detail"]["code"] == (
        "scheduled_authority_fingerprint_mismatch"
    )
    assert store.get_worker(worker_id)["profile"] == "claude-code"

    arbitrary_payload = _scheduled_workbench_worker_payload(
        run_id="scheduled-arbitrary-route-1", alias="arbitrary-route"
    )
    arbitrary = client.post(endpoint, headers=headers, json=arbitrary_payload)
    assert arbitrary.status_code == 200
    arbitrary_worker_id = arbitrary.json()["worker_id"]
    store.update_worker(
        arbitrary_worker_id,
        **{**fallback_fields, "model": "stub/not-the-declared-fallback"},
    )
    store.create_run(
        arbitrary_worker_id,
        project["project_id"],
        "Synthetic terminal arbitrary route.",
        state="failed",
    )
    arbitrary_reuse = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id="scheduled-arbitrary-route-2", alias="arbitrary-route"
        ),
    )
    assert arbitrary_reuse.status_code == 409
    assert arbitrary_reuse.json()["detail"]["code"] == (
        "scheduled_authority_fingerprint_mismatch"
    )


def test_scheduled_workbench_reconciles_only_legacy_missing_claude_default(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret"
    )
    monkeypatch.setenv(
        "GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code"
    )
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "default")
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )

    class ScheduledRouteRuntime(StubRuntime):
        def resolve_model(self, profile: str, execution_mode="docker") -> str:
            return f"stub/{profile}"

    db_path = tmp_path / "runtime.db"
    app = create_app(
        str(db_path), runtime_backend="stub", runtime=ScheduledRouteRuntime()
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer synthetic-service-token"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "synthetic-owner",
            "title": "Legacy scheduled fallback envelope",
            "goal": "Reconcile only the known legacy default omission.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    endpoint = f"/v1/projects/{project['project_id']}/workers/find-or-resume"
    store = Store(str(db_path))
    service = app.state.service
    fallback_fields = {
        "profile": "claude-code",
        "backend": service._legacy_backend_label("claude-code", "docker", ""),
        "runtime": service._initial_runtime_label("claude-code", "docker"),
        "model": "stub/claude-code",
    }

    def create_exact(alias: str, run_id: str) -> tuple[str, dict]:
        response = client.post(
            endpoint,
            headers=headers,
            json=_scheduled_workbench_worker_payload(
                run_id=run_id,
                alias=alias,
                fallback={
                    "worker_profile": "claude-code",
                    "model": "stub/claude-code",
                    "reasoning_effort": "default",
                },
            ),
        )
        assert response.status_code == 200
        worker_id = response.json()["worker_id"]
        bundle = json.loads(store.get_worker(worker_id)["bootstrap_bundle_json"])
        assert bundle["env"] == {
            "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
            "WPR_CLAUDE_CODE_EFFORT": "default",
        }
        return worker_id, bundle

    worker_id, exact_bundle = create_exact(
        "legacy-default", "legacy-default-run-1"
    )
    legacy_bundle = json.loads(json.dumps(exact_bundle))
    legacy_bundle["env"].pop("WPR_CLAUDE_CODE_EFFORT")
    store.update_worker(
        worker_id,
        **fallback_fields,
        bootstrap_bundle_json=json.dumps(legacy_bundle),
    )
    store.create_run(
        worker_id,
        project["project_id"],
        "Synthetic terminal legacy fallback.",
        state="failed",
    )

    reconciled = client.post(
        endpoint,
        headers=headers,
        json=_scheduled_workbench_worker_payload(
            run_id="legacy-default-run-2",
            alias="legacy-default",
            fallback={
                "worker_profile": "claude-code",
                "model": "stub/claude-code",
                "reasoning_effort": "default",
            },
        ),
    )

    assert reconciled.status_code == 200
    assert reconciled.json()["worker_id"] == worker_id
    restored = store.get_worker(worker_id)
    restored_bundle = json.loads(restored["bootstrap_bundle_json"])
    assert restored["profile"] == "codex-cli"
    assert restored["model"] == "stub/codex-cli"
    assert restored_bundle["env"] == {
        "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
        "WPR_CLAUDE_CODE_EFFORT": "default",
    }
    assert restored_bundle["callbacks"]["origin_ref"] == "legacy-default-run-2"

    rejected_cases = (
        ("missing-primary", "missing_primary", "failed"),
        ("mismatched-default", "mismatched_default", "failed"),
        ("active-legacy", "legacy_default", "queued"),
    )
    for alias, mutation, run_state in rejected_cases:
        candidate_id, candidate_bundle = create_exact(alias, f"{alias}-run-1")
        if mutation == "missing_primary":
            candidate_bundle["env"].pop("WPR_CODEX_CLI_REASONING_EFFORT")
        elif mutation == "mismatched_default":
            candidate_bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] = "max"
        else:
            candidate_bundle["env"].pop("WPR_CLAUDE_CODE_EFFORT")
        store.update_worker(
            candidate_id,
            **fallback_fields,
            bootstrap_bundle_json=json.dumps(candidate_bundle),
        )
        store.create_run(
            candidate_id,
            project["project_id"],
            f"Synthetic {mutation} fallback state.",
            state=run_state,
        )
        rejected = client.post(
            endpoint,
            headers=headers,
            json=_scheduled_workbench_worker_payload(
                run_id=f"{alias}-run-2",
                alias=alias,
                fallback={
                    "worker_profile": "claude-code",
                    "model": "stub/claude-code",
                    "reasoning_effort": "default",
                },
            ),
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == (
            "scheduled_authority_fingerprint_mismatch"
        )


def test_scheduled_workbench_fallback_authority_request_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "synthetic-callback-secret"
    )
    monkeypatch.setenv(
        "GLASSHIVE_ALLOWED_WORKER_PROFILES",
        "codex-cli,claude-code,openclaw-general",
    )
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )
    client = TestClient(
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    )
    headers = {"Authorization": "Bearer synthetic-service-token"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "synthetic-owner",
            "title": "Scheduled prompts",
            "goal": "Reject unsafe authority requests.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    base = _scheduled_workbench_worker_payload(run_id="scheduled-run-rejected")
    endpoint = f"/v1/projects/{project['project_id']}/workers/find-or-resume"
    rejected_payloads = []
    rejected_payloads.append({**base, "bootstrap_profile": "clean-room"})
    rejected_payloads.append({**base, "execution_mode": "host"})
    same_fallback = json.loads(json.dumps(base))
    same_fallback["bootstrap_bundle"]["viventium_execution_authority_request"][
        "fallback"
    ] = {
        "worker_profile": "codex-cli",
        "model": "stub/codex-cli",
        "reasoning_effort": "xhigh",
    }
    rejected_payloads.append(same_fallback)
    disallowed_fallback = json.loads(json.dumps(base))
    disallowed_fallback["bootstrap_bundle"][
        "viventium_execution_authority_request"
    ]["fallback"] = {
        "worker_profile": "openclaw-general",
        "model": "stub/general",
        "reasoning_effort": "default",
    }
    rejected_payloads.append(disallowed_fallback)
    wrong_fallback_model = json.loads(json.dumps(base))
    wrong_fallback_model["bootstrap_bundle"][
        "viventium_execution_authority_request"
    ]["fallback"]["model"] = "stub/not-the-compiled-route"
    rejected_payloads.append(wrong_fallback_model)
    wrong_fallback_effort = json.loads(json.dumps(base))
    wrong_fallback_effort["bootstrap_bundle"][
        "viventium_execution_authority_request"
    ]["fallback"]["reasoning_effort"] = "default"
    rejected_payloads.append(wrong_fallback_effort)
    forged_policy = json.loads(json.dumps(base))
    forged_policy["bootstrap_bundle"]["execution_policy"] = "parallel-clean-room-v1"
    rejected_payloads.append(forged_policy)
    forged_authority = json.loads(json.dumps(base))
    forged_authority["bootstrap_bundle"]["viventium_launch_authority"] = {
        "version": 1,
        "kind": "prompt_workbench_scheduled",
        "execution_mode": "docker",
        "fallback_worker_profile": "claude-code",
    }
    rejected_payloads.append(forged_authority)

    for index, payload in enumerate(rejected_payloads):
        payload["alias"] = f"rejected-{index}"
        response = client.post(endpoint, headers=headers, json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == (
            "parallel_execution_isolation_required"
        )

    monkeypatch.delenv("GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE")
    missing_config = client.post(
        endpoint,
        headers=headers,
        json={**base, "alias": "missing-configured-fallback"},
    )
    assert missing_config.status_code == 409
    assert missing_config.json()["detail"]["code"] == (
        "parallel_execution_isolation_required"
    )

    generic_create = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json=base,
    )
    assert generic_create.status_code == 409


def test_scheduled_workbench_authority_requires_service_authentication(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    client = TestClient(
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    )
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "synthetic-owner",
            "title": "Local caller",
            "goal": "Prove local callers cannot mint scheduled authority.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    response = client.post(
        f"/v1/projects/{project['project_id']}/workers/find-or-resume",
        json=_scheduled_workbench_worker_payload(run_id="scheduled-local-rejected"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "parallel_execution_isolation_required"
    )


def test_scheduled_workbench_authority_rejects_service_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    monkeypatch.setenv(
        "GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE", "claude-code"
    )
    client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=StubRuntime(),
        )
    )
    service_headers = {"Authorization": "Bearer synthetic-service-token"}
    project = client.post(
        "/v1/projects",
        headers=service_headers,
        json={
            "owner_id": "synthetic-owner",
            "title": "Scoped service caller",
            "goal": "Prove identity-scoped callers cannot mint scheduled authority.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    response = client.post(
        f"/v1/projects/{project['project_id']}/workers/find-or-resume",
        headers={
            **service_headers,
            "X-Viventium-User-Id": "synthetic-owner",
        },
        json=_scheduled_workbench_worker_payload(
            run_id="scheduled-service-identity-rejected"
        ),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "parallel_execution_isolation_required"
    )


def test_worker_find_or_resume_refreshes_runtime_when_alias_reprofiles(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Reprofile Workers",
            "goal": "Reuse an alias with the currently selected profile.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    openclaw_payload = {
        "owner_id": "demo-owner",
        "name": "Reusable Worker",
        "role": "research",
        "profile": "openclaw-general",
        "backend": "openclaw",
        "execution_mode": "docker",
        "alias": "main",
        "start_synchronously": False,
    }
    codex_payload = {
        **openclaw_payload,
        "profile": "codex-cli",
        "name": "Reusable Codex Worker",
        "role": "coding",
    }

    first = client.post(f"/v1/projects/{project['project_id']}/workers/find-or-resume", json=openclaw_payload)
    second = client.post(f"/v1/projects/{project['project_id']}/workers/find-or-resume", json=codex_payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["worker_id"] == first.json()["worker_id"]
    assert second.json()["profile"] == "codex-cli"
    assert second.json()["runtime"] == "codex-cli"


def test_live_logs_follow_profile_when_legacy_runtime_metadata_is_stale(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    store = Store(str(db_path))
    project = store.create_project("demo-owner", "Stale Runtime", "Read logs from current profile.", "codex-cli")
    worker = store.create_worker(
        project["project_id"],
        "demo-owner",
        "Codex Worker",
        "coding",
        profile="codex-cli",
        backend="openclaw",
        runtime="openclaw",
        model="gpt-5.4",
    )
    log_dir = db_path.parent / "codex_cli_runtime" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / f"{worker['worker_id']}.stdout.log").write_text("codex profile log marker\n", encoding="utf-8")

    response = client.get(f"/v1/workers/{worker['worker_id']}/live")

    assert response.status_code == 200
    assert "codex profile log marker" in response.json()["console"]["stdout"]


def test_worker_find_or_resume_refreshes_callback_bundle_on_alias_reuse(tmp_path, monkeypatch):
    monkeypatch.delenv("VIVENTIUM_ENV_FILE", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    payloads: list[dict] = []

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Host Workers", "Reuse named host workers.", "codex-cli")
        original = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle={
                "mcp_config": {"servers": {"safe-tool": {"url": "http://safe-tool.local/mcp"}}},
                "instructions_md": "Keep operator-seeded instructions.",
                "env_overrides": {"SAFE_FLAG": "1"},
                "files": [{"path": "seed.md", "content": "seed"}],
                "callbacks": {
                    "conversation_id": "old-conversation",
                    "parent_message_id": "old-parent",
                    "message_id": "old-assistant",
                }
            },
        )

        refreshed = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle={
                "files": [{"path": "current.md", "content": "current"}],
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "new-conversation",
                    "parent_message_id": "new-parent",
                    "message_id": "new-assistant",
                }
            },
        )

        assert refreshed["worker_id"] == original["worker_id"]
        stored_worker = store.get_worker(refreshed["worker_id"])
        stored_bundle = json.loads(stored_worker["bootstrap_bundle_json"])
        assert stored_bundle["callbacks"]["conversation_id"] == "new-conversation"
        assert stored_bundle["callbacks"]["events_webhook_url"] == "http://callback.local/glasshive"
        assert stored_bundle["mcp_config"]["servers"]["safe-tool"]["url"] == "http://safe-tool.local/mcp"
        assert stored_bundle["instructions_md"] == "Keep operator-seeded instructions."
        assert stored_bundle["env_overrides"]["SAFE_FLAG"] == "1"
        assert {item["path"] for item in stored_bundle["files"]} == {"seed.md", "current.md"}

        run = service.assign_run(refreshed["worker_id"], "Return the final result to the current chat")
        wait_until(lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed")
        wait_until(lambda: any(payload.get("event") == "run.completed" for payload in payloads))

        completed = next(payload for payload in payloads if payload.get("event") == "run.completed")
        assert completed["conversation_id"] == "new-conversation"
        assert completed["parent_message_id"] == "new-parent"
        assert completed["message_id"] == "new-assistant"
    finally:
        service.shutdown()


def test_worker_find_or_resume_preserves_bundle_when_no_new_bundle_is_provided(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Host Workers", "Reuse named host workers.", "codex-cli")
        original = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle={
                "mcp_config": {"servers": {"safe-tool": {"url": "http://safe-tool.local/mcp"}}},
                "callbacks": {"conversation_id": "existing-conversation"},
            },
        )

        resumed = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle=None,
        )

        assert resumed["worker_id"] == original["worker_id"]
        stored_bundle = json.loads(store.get_worker(resumed["worker_id"])["bootstrap_bundle_json"])
        assert stored_bundle["mcp_config"]["servers"]["safe-tool"]["url"] == "http://safe-tool.local/mcp"
        assert stored_bundle["callbacks"]["conversation_id"] == "existing-conversation"
    finally:
        service.shutdown()


def test_alias_reuse_losing_race_to_close_creates_a_new_workspace(tmp_path):
    class BlockingAliasLookupStore(Store):
        def __init__(self, db_path: str):
            super().__init__(db_path)
            self.block_lookup = False
            self.entered = Event()
            self.release = Event()

        def find_worker_by_alias(self, *args, **kwargs):
            worker = super().find_worker_by_alias(*args, **kwargs)
            if self.block_lookup:
                self.entered.set()
                assert self.release.wait(timeout=3), "alias reuse test did not release lookup"
            return worker

    store = BlockingAliasLookupStore(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = service.create_project("owner", "Alias race", "Create after Close wins.", "codex-cli")
    original = service.find_or_create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Original",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        alias="primary",
        start_synchronously=False,
    )
    store.block_lookup = True
    results: list[dict] = []

    thread = Thread(
        target=lambda: results.append(
            service.find_or_create_worker(
                project_id=project["project_id"],
                owner_id="owner",
                name="Replacement",
                role="operator",
                profile="codex-cli",
                backend="openclaw",
                alias="primary",
                start_synchronously=False,
            )
        )
    )
    thread.start()
    try:
        assert store.entered.wait(timeout=2), "alias reuse never reached the stale lookup"
        assert service.terminate_worker(original["worker_id"])["state"] == "terminated"
    finally:
        store.release.set()
        thread.join(timeout=3)
        service.shutdown()

    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0]["worker_id"] != original["worker_id"]
    assert results[0]["state"] == "paused"
    assert store.get_worker(original["worker_id"])["state"] == "terminated"
    assert "worker.resumed_by_alias" not in {
        event["event_type"] for event in store.list_events(original["worker_id"])
    }


def test_host_worker_disabled_blocks_host_creation_and_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Host Workers",
            "goal": "Verify disabled host execution is enforced.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    host_payload = {
        "owner_id": "demo-owner",
        "name": "Codex Host",
        "role": "coding",
        "profile": "codex-cli",
        "backend": "openclaw",
        "execution_mode": "host",
    }
    blocked = client.post(f"/v1/projects/{project['project_id']}/workers", json=host_payload)
    assert blocked.status_code == 403
    assert "host-native workers are disabled" in blocked.json()["detail"]

    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    created = client.post(f"/v1/projects/{project['project_id']}/workers", json=host_payload)
    assert created.status_code == 201
    worker_id = created.json()["worker_id"]

    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    run = client.post(f"/v1/workers/{worker_id}/assign", json={"instruction": "Open Chrome"})
    assert run.status_code == 403
    assert "host-native workers are disabled" in run.json()["detail"]


def test_assign_run_effort_updates_codex_worker_bootstrap_bundle(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub"))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Codex effort",
            "goal": "Verify per-run effort handoff.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Codex Worker",
            "role": "coding",
            "profile": "codex-cli",
            "backend": "openclaw",
            "start_synchronously": False,
        },
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Continue with a lower effort pass.", "effort": "medium"},
    )
    assert run.status_code == 202
    assert run.json()["state"] == "queued"
    assert run.json()["effort"] == "medium"

    stored_worker = Store(str(db_path)).get_worker(worker["worker_id"])
    bundle = json.loads(stored_worker["bootstrap_bundle_json"])
    assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "medium"

    rejected = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Invalid effort should fail.", "effort": "max"},
    )
    assert rejected.status_code == 400
    assert "Codex effort" in rejected.json()["detail"]


def test_assign_run_effort_updates_claude_worker_bootstrap_bundle(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub"))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Claude effort",
            "goal": "Verify per-run effort handoff.",
            "default_worker_profile": "claude-code",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Worker",
            "role": "research",
            "profile": "claude-code",
            "backend": "openclaw",
            "start_synchronously": False,
        },
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Continue with a max effort pass.", "effort": "max"},
    )
    assert run.status_code == 202
    assert run.json()["state"] == "queued"
    assert run.json()["effort"] == "max"

    stored_worker = Store(str(db_path)).get_worker(worker["worker_id"])
    bundle = json.loads(stored_worker["bootstrap_bundle_json"])
    assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"
    assert "Worker effort preference" not in str(bundle)

    rejected = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Invalid effort should fail.", "effort": "ultra"},
    )
    assert rejected.status_code == 400
    assert "Claude effort" in rejected.json()["detail"]


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_assign_run_effort_accepts_native_claude_levels(tmp_path, effort):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub"))
    preference = client.patch("/v1/preferences", json={"claude_effort": effort})
    assert preference.status_code == 200
    assert preference.json()["claude_effort"] == effort
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Claude native effort",
            "goal": "Verify the current Claude CLI effort handoff.",
            "default_worker_profile": "claude-code",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Worker",
            "role": "research",
            "profile": "claude-code",
            "backend": "openclaw",
            "start_synchronously": False,
        },
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Continue with an xhigh effort pass.", "effort": effort},
    )

    assert run.status_code == 202
    assert run.json()["effort"] == effort
    stored_worker = Store(str(db_path)).get_worker(worker["worker_id"])
    bundle = json.loads(stored_worker["bootstrap_bundle_json"])
    assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == effort


def test_signed_worker_view_is_limited_to_read_and_narrow_communication(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("WPR_API_TOKEN", "api-token")

    db_path = tmp_path / "runtime.db"
    app = create_app(str(db_path), runtime_backend="stub")
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    client = TestClient(app)
    headers = {"x-wpr-token": "api-token"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "demo-owner",
            "title": "Signed Link Assign",
            "goal": "Signed watch links can steer, not reshape bootstrap context.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "demo-owner",
            "name": "Codex Worker",
            "role": "coding",
            "profile": "codex-cli",
            "backend": "openclaw",
            "start_synchronously": False,
        },
    ).json()
    signed_watch = sign_link_params(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id="demo-owner",
    )

    injected = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        params=signed_watch,
        json={
            "instruction": "Try to reshape bootstrap.",
            "bootstrap_bundle": {"env": {"UNTRUSTED_FROM_LINK": "1"}},
        },
    )
    assert injected.status_code in {401, 403}

    assign_without_bootstrap = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        params=signed_watch,
        json={"instruction": "Continue without bootstrap injection.", "effort": "medium"},
    )
    message = client.post(
        f"/v1/workers/{worker['worker_id']}/message",
        params=signed_watch,
        json={"message": "Share a concise status update."},
    )
    steer = client.post(
        f"/v1/workers/{worker['worker_id']}/steer",
        params=signed_watch,
        json={"message": "Focus on the requested output."},
    )
    pause = client.post(f"/v1/workers/{worker['worker_id']}/pause", params=signed_watch)
    desktop = client.post(
        f"/v1/workers/{worker['worker_id']}/desktop-action",
        params=signed_watch,
        json={"action": "browser"},
    )

    assert assign_without_bootstrap.status_code in {401, 403}
    assert message.status_code == 202
    assert steer.status_code == 202
    assert pause.status_code in {401, 403}
    assert desktop.status_code in {401, 403}

    with pytest.raises(WebSocketDisconnect) as terminal_denied:
        with client.websocket_connect(
            f"/ws/workers/{worker['worker_id']}/terminal?{urlencode(signed_watch)}"
        ):
            pass
    assert terminal_denied.value.code == 4403


def test_missing_host_cli_blocks_creation_before_worker_row(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_CODEX_BIN", str(tmp_path / "missing-codex"))
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="profiled"))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Host Missing CLI",
            "goal": "Fail closed before worker creation when a selected host CLI is unavailable.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    blocked = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Codex Host",
            "role": "coding",
            "profile": "codex-cli",
            "backend": "openclaw",
            "execution_mode": "host",
            "start_synchronously": False,
        },
    )

    assert blocked.status_code == 409
    body = blocked.json()
    assert body["status"] == "blocked"
    assert body["failure_class"] == "runtime_dependency_missing"
    assert body["failure_retryable"] == 0
    assert "missing-codex" in body["detail"]
    workers = client.get(f"/v1/projects/{project['project_id']}/workers").json()["items"]
    assert workers == []


def test_duplicate_worker_copies_workspace_into_new_worker(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    source_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Source Workspace",
            "goal": "Provide a reusable workspace to duplicate.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    source_worker = client.post(
        f"/v1/projects/{source_project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
            "bootstrap_profile": "host-login",
            "bootstrap_bundle": {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "notes/from-bootstrap.txt",
                        "content": "seeded",
                    }
                ]
            },
        },
    ).json()

    source_workspace = Path(source_worker["workspace_dir"])
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_workspace / "app.txt").write_text("copied from source")
    (source_workspace / ".mcp.json").write_text('{"seed":"source"}')
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE workers SET backend = ? WHERE worker_id = ?",
            ("openclaw", source_worker["worker_id"]),
        )

    target_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Duplicate Workspace",
            "goal": "Create a duplicated workspace",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    duplicate = client.post(
        f"/v1/projects/{target_project['project_id']}/workers/duplicate",
        json={
            "owner_id": "demo-owner",
            "source_worker_id": source_worker["worker_id"],
            "name": "Main Workspace",
            "role": "main",
        },
    )
    assert duplicate.status_code == 201
    duplicated_worker = duplicate.json()
    duplicated_workspace = Path(duplicated_worker["workspace_dir"])

    assert duplicated_worker["backend"] == "codex-cli"
    assert (duplicated_workspace / "app.txt").read_text() == "copied from source"
    assert not (duplicated_workspace / ".mcp.json").exists()
    assert duplicated_worker["duplication_report"] == {
        "source_state": "copied",
        "copied_files": 1,
        "skipped_items": 1,
    }

    events = client.get(f"/v1/workers/{duplicated_worker['worker_id']}/events")
    assert events.status_code == 200
    assert any(item["event_type"] == "worker.duplicated" for item in events.json()["items"])

    renamed = client.patch(
        f"/v1/workspaces/{source_worker['worker_id']}",
        json={"name": "Reusable Workspace", "favorite": True, "tags": ["Reference"]},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Reusable Workspace"
    assert renamed.json()["favorite"] == 1

    catalog_duplicate = client.post(
        f"/v1/workspaces/{source_worker['worker_id']}/duplicate",
        json={
            "idempotency_key": "duplicate-catalog-workspace-1",
            "name": "Independent Workspace",
        },
    )
    assert catalog_duplicate.status_code == 201
    duplicate_payload = catalog_duplicate.json()
    assert duplicate_payload["workspace"]["worker_id"] != source_worker["worker_id"]
    assert duplicate_payload["workspace"]["name"] == "Independent Workspace"
    assert duplicate_payload["project"]["goal"] == source_project["goal"]
    assert duplicate_payload["workspace"]["duplication_report"]["copied_files"] == 1
    assert "workspace_dir" not in duplicate_payload["workspace"]
    assert "bootstrap_bundle_json" not in duplicate_payload["workspace"]


def test_duplicate_worker_does_not_copy_home_directory(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    source_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Source Workspace",
            "goal": "Provide a reusable workspace to duplicate.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    source_worker = client.post(
        f"/v1/projects/{source_project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    source_workspace = Path(source_worker["workspace_dir"])
    source_home = source_workspace.parent / "home"
    source_home.mkdir(parents=True, exist_ok=True)
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_home / ".qa-home-marker").write_text("home-only")
    (source_workspace / "qa_workspace_marker.txt").write_text("workspace-only")

    target_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Duplicate Workspace",
            "goal": "Create a duplicated workspace",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    duplicate = client.post(
        f"/v1/projects/{target_project['project_id']}/workers/duplicate",
        json={
            "owner_id": "demo-owner",
            "source_worker_id": source_worker["worker_id"],
            "name": "Main Workspace",
            "role": "main",
        },
    )
    assert duplicate.status_code == 201
    duplicated_worker = duplicate.json()
    duplicated_workspace = Path(duplicated_worker["workspace_dir"])
    duplicated_home = duplicated_workspace.parent / "home"

    assert (duplicated_workspace / "qa_workspace_marker.txt").read_text() == "workspace-only"
    assert not (duplicated_home / ".qa-home-marker").exists()


def test_assign_run_on_paused_worker_resumes_before_queueing(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Resume Workspace",
            "goal": "Resume the paused workspace before queueing a new run.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    pause_resp = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert pause_resp.status_code == 202
    assert pause_resp.json()["state"] == "paused"

    assign_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Resume this paused workspace and start a fresh run."},
    )
    assert assign_resp.status_code == 202

    events = client.get(f"/v1/workers/{worker['worker_id']}/events")
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "worker.paused" in event_types
    assert "worker.resumed" in event_types
    assert "run.queued" in event_types


def test_nonblocking_worker_create_defers_runtime_start_to_run_queue(tmp_path):
    class SlowReadyRuntime(StubRuntime):
        def __init__(self) -> None:
            self.ready_started = Event()
            self.release_ready = Event()
            self.ready_calls = 0
            self.events: list[str] = []

        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            self.events.append("ready")
            self.ready_started.set()
            self.ready_calls += 1
            self.release_ready.wait(timeout=5)
            return super().ensure_worker_ready(worker)

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            self.ensure_worker_ready(worker)
            publish_in_process_test_start(worker)
            self.events.append("run_task")
            return "BACKGROUND_START_OK"

    db_path = tmp_path / "runtime.db"
    runtime = SlowReadyRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Background Workspace",
            "goal": "Queue work without blocking on runtime preparation.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    worker_resp = client.post(
        f"/v1/projects/{project['project_id']}/workers/find-or-resume",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "alias": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
            "start_synchronously": False,
        },
    )
    assert worker_resp.status_code == 200
    worker = worker_resp.json()
    assert worker["state"] == "paused"
    assert runtime.ready_calls == 0

    start = time.monotonic()
    assign_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Run after background preparation."},
    )
    assert assign_resp.status_code == 202
    assert time.monotonic() - start < 2

    assert runtime.ready_started.wait(2.0)
    runtime.release_ready.set()
    settled = wait_for_run(client, assign_resp.json()["run_id"], timeout=3.0)
    assert settled["state"] == "completed"
    assert settled["output_text"] == "BACKGROUND_START_OK"
    assert runtime.events[0] == "ready"
    assert "run_task" in runtime.events
    assert runtime.events.index("run_task") > 0


def test_openclaw_worker_exposes_operator_control_surface(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Control Surface", "goal": "View and control worker progress."},
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Worker",
            "role": "coder",
            "profile": "openclaw-claude",
            "backend": "openclaw",
        },
    ).json()

    takeover = client.get(f"/v1/workers/{worker['worker_id']}/takeover")
    assert takeover.status_code == 200
    data = takeover.json()
    assert data["supported"] is True
    assert data["mode"] == "web-terminal"
    assert data["url"].endswith(f"/ui/workers/{worker['worker_id']}/terminal")

    worker_ui = client.get(f"/ui/workers/{worker['worker_id']}")
    assert worker_ui.status_code == 200
    assert "Claude Worker" in worker_ui.text
    assert worker["session_key"] in worker_ui.text
    assert "Queue task" in worker_ui.text
    assert "Take over terminal" in worker_ui.text
    assert "Managed by GlassHive" in worker_ui.text
    assert str(worker.get("workspace_dir") or "") not in worker_ui.text

    diagnostics_ui = client.get(f"/ui/workers/{worker['worker_id']}?diagnostics=1")
    assert diagnostics_ui.status_code == 200
    assert str(worker.get("workspace_dir") or "") in diagnostics_ui.text

    terminal_ui = client.get(f"/ui/workers/{worker['worker_id']}/terminal")
    assert terminal_ui.status_code == 200
    assert "Connecting to worker terminal" in terminal_ui.text
    assert f"const workerId = '{worker['worker_id']}'" in terminal_ui.text
    assert 'target="_top">Back to project workspace</a>' in terminal_ui.text
    assert 'target="_top">Worker console</a>' in terminal_ui.text


def test_project_workspace_ui_supports_simple_run_flow(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Simple Flow",
            "goal": "Keep the operator path easy: prompt, worker, run, control.",
            "default_worker_profile": "openclaw-general",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Primary Worker",
            "role": "research",
            "profile": "openclaw-general",
            "backend": "openclaw",
        },
    ).json()

    home = client.get("/ui")
    assert home.status_code == 200
    assert "Open project workspace" in home.text

    project_ui = client.get(f"/ui/projects/{project['project_id']}")
    assert project_ui.status_code == 200
    assert "Run Project" in project_ui.text
    assert "Create worker only" in project_ui.text
    assert "Selected Worker" in project_ui.text
    assert worker["worker_id"] in project_ui.text
    assert "Take over terminal" in project_ui.text


def test_live_payload_promotes_workspace_html_as_deliverable(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Deliverable Detection",
            "goal": "Expose a presentable page result to the operator UI.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Page Worker",
            "role": "builder",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "index.html").write_text("<!doctype html><h1>HELLO WORLD</h1>")

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    payload = live.json()
    assert payload["deliverable"]["kind"] == "webpage"
    assert payload["deliverable"]["source"] == "workspace_html"
    assert payload["deliverable"]["browser_url"] == "file:///workspace/project/index.html"
    assert payload["deliverable"]["preferred_surface"] == "desktop"


def test_live_payload_prefers_professional_document_over_supporting_html(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Document Deliverable",
            "goal": "Expose the client-ready document before supporting source artifacts.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Document Worker",
            "role": "research",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (workspace / "index.html").write_text("<!doctype html><h1>Supporting preview</h1>", encoding="utf-8")
    write_minimal_docx(artifacts / "research-brief.docx")

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    deliverable = live.json()["deliverable"]
    assert deliverable["kind"] == "file"
    assert deliverable["workspace_path"] == "artifacts/research-brief.docx"
    assert deliverable["preferred_surface"] == "download"


def test_live_payload_does_not_promote_fake_document_over_html(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Document Deliverable",
            "goal": "Reject renamed fake office documents.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Document Worker",
            "role": "research",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (workspace / "index.html").write_text("<!doctype html><h1>Supporting preview</h1>", encoding="utf-8")
    (artifacts / "research-brief.docx").write_text("not a real Word package", encoding="utf-8")

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    deliverable = live.json()["deliverable"]
    assert deliverable["kind"] == "webpage"
    assert deliverable["workspace_path"] == "index.html"
    assert deliverable["source"] == "workspace_html"


def test_live_payload_file_deliverable_includes_signed_open_and_download_links(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "File Deliverable",
            "goal": "Expose a downloadable file result to the operator UI.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "File Worker",
            "role": "writer",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "answer.txt").write_text("download me", encoding="utf-8")

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    deliverable = live.json()["deliverable"]
    assert deliverable["kind"] == "file"
    assert deliverable["workspace_path"] == "answer.txt"
    assert_link_ref_url(deliverable["open_url"], prefix="/v1/link-refs/", kind="artifact_open")
    assert_link_ref_url(deliverable["download_url"], prefix="/v1/link-refs/", kind="artifact_download")
    with sqlite3.connect(os.environ["GLASSHIVE_LINK_REF_STATE_PATH"]) as conn:
        link_ref_count = conn.execute("SELECT COUNT(*) FROM signed_link_refs").fetchone()[0]

    second_live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert second_live.status_code == 200
    second_deliverable = second_live.json()["deliverable"]
    assert second_deliverable["open_url"] == deliverable["open_url"]
    assert second_deliverable["download_url"] == deliverable["download_url"]
    with sqlite3.connect(os.environ["GLASSHIVE_LINK_REF_STATE_PATH"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signed_link_refs").fetchone()[0] == link_ref_count

    opened = client.get(deliverable["open_url"])
    assert opened.status_code == 200
    assert "download me" in opened.text
    assert "Download file" in opened.text
    downloaded = client.get(deliverable["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.text == "download me"
    assert "attachment" in downloaded.headers["content-disposition"]


def test_live_payload_artifact_inventory_includes_multiple_signed_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Multi File Deliverable",
            "goal": "Expose all generated files to the operator UI.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Multi File Worker",
            "role": "writer",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "first.txt").write_text("FIRST_OK", encoding="utf-8")
    (workspace / "second.txt").write_text("SECOND_OK", encoding="utf-8")
    (workspace / ".mcp.json").write_text("{}", encoding="utf-8")

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    artifact_items = live.json()["artifacts"]["items"]
    by_path = {item["path"]: item for item in artifact_items}
    assert sorted(by_path) == ["first.txt", "second.txt"]
    assert_link_ref_url(by_path["first.txt"]["open_url"], prefix="/v1/link-refs/", kind="artifact_open")
    assert_link_ref_url(by_path["first.txt"]["download_url"], prefix="/v1/link-refs/", kind="artifact_download")
    assert_link_ref_url(by_path["second.txt"]["open_url"], prefix="/v1/link-refs/", kind="artifact_open")
    assert_link_ref_url(by_path["second.txt"]["download_url"], prefix="/v1/link-refs/", kind="artifact_download")

    downloaded = client.get(by_path["first.txt"]["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.text == "FIRST_OK"


def test_artifact_surfaces_reject_browser_runtime_scratch_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Scratch Artifact Filtering",
            "goal": "Expose user deliverables without exposing browser profile state.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Scratch Filter Worker",
            "role": "writer",
            "profile": "codex-cli",
            "backend": "openclaw",
            "execution_mode": "host",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    deliverable = workspace / "artifacts" / "result.csv"
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("name,status\nsynthetic,ok\n", encoding="utf-8")
    script = workspace / "scripts" / "helper.js"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("console.log('not the deliverable');\n", encoding="utf-8")
    os.utime(script, (9999999999, 9999999999))

    extension_index = (
        workspace
        / "tmp"
        / "chrome-user-data"
        / "Default"
        / "Default"
        / "Extensions"
        / "fdpohaocaechififmbbbbbknoalclacl"
        / "8.6_0"
        / "capture"
        / "index.html"
    )
    extension_index.parent.mkdir(parents=True, exist_ok=True)
    extension_index.write_text("<!doctype html><title>Capture (redir)</title>", encoding="utf-8")
    copied_cookie_store = workspace / "tmp" / "chrome-default-cookies.sqlite"
    copied_cookie_store.write_bytes(b"synthetic cookie db")
    persistent_extension = (
        workspace
        / ".config"
        / "chromium"
        / "Default"
        / "Extensions"
        / "fcoeoabgfenejglbffodgkkbkcdhcgfn"
        / "1.0_0"
        / "manifest.json"
    )
    persistent_extension.parent.mkdir(parents=True, exist_ok=True)
    persistent_extension.write_text("{}", encoding="utf-8")
    browser_profile_state = (
        workspace
        / "work-services"
        / "browser-profile"
        / "ActorSafetyLists"
        / "1.0"
        / "listdata.json"
    )
    browser_profile_state.parent.mkdir(parents=True, exist_ok=True)
    browser_profile_state.write_text("{}", encoding="utf-8")
    upload_metadata = workspace / "uploads" / "source.txt.metadata.json"
    upload_metadata.parent.mkdir(parents=True, exist_ok=True)
    upload_metadata.write_text("{}", encoding="utf-8")

    assert not is_user_deliverable_relative_path(extension_index.relative_to(workspace))
    assert not is_user_deliverable_relative_path(copied_cookie_store.relative_to(workspace))
    assert not is_user_deliverable_relative_path(persistent_extension.relative_to(workspace))
    assert not is_user_deliverable_relative_path(browser_profile_state.relative_to(workspace))
    assert not is_user_deliverable_relative_path(upload_metadata.relative_to(workspace))

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    payload = live.json()
    assert payload["deliverable"]["kind"] == "file"
    assert payload["deliverable"]["workspace_path"] == "artifacts/result.csv"

    listed = client.get(f"/v1/workers/{worker['worker_id']}/artifacts")
    assert listed.status_code == 200
    listed_paths = {item["path"] for item in listed.json()["items"]}
    assert "artifacts/result.csv" in listed_paths
    assert "scripts/helper.js" in listed_paths
    assert extension_index.relative_to(workspace).as_posix() not in listed_paths
    assert copied_cookie_store.relative_to(workspace).as_posix() not in listed_paths
    assert persistent_extension.relative_to(workspace).as_posix() not in listed_paths
    assert browser_profile_state.relative_to(workspace).as_posix() not in listed_paths
    assert upload_metadata.relative_to(workspace).as_posix() not in listed_paths

    opened = client.get(
        f"/v1/workers/{worker['worker_id']}/artifacts/open",
        params={"path": extension_index.relative_to(workspace).as_posix()},
    )
    assert opened.status_code == 400
    assert opened.json()["detail"] == "Artifact path is not downloadable"

    downloaded = client.get(
        f"/v1/workers/{worker['worker_id']}/artifacts/download",
        params={"path": copied_cookie_store.relative_to(workspace).as_posix()},
    )
    assert downloaded.status_code == 400
    assert downloaded.json()["detail"] == "Artifact path is not downloadable"

    persistent_download = client.get(
        f"/v1/workers/{worker['worker_id']}/artifacts/download",
        params={"path": persistent_extension.relative_to(workspace).as_posix()},
    )
    assert persistent_download.status_code == 400
    assert persistent_download.json()["detail"] == "Artifact path is not downloadable"

    browser_profile_open = client.get(
        f"/v1/workers/{worker['worker_id']}/artifacts/open",
        params={"path": browser_profile_state.relative_to(workspace).as_posix()},
    )
    assert browser_profile_open.status_code == 400
    assert browser_profile_open.json()["detail"] == "Artifact path is not downloadable"

    browser_profile_download = client.get(
        f"/v1/workers/{worker['worker_id']}/artifacts/download",
        params={"path": browser_profile_state.relative_to(workspace).as_posix()},
    )
    assert browser_profile_download.status_code == 400
    assert browser_profile_download.json()["detail"] == "Artifact path is not downloadable"


def test_worker_artifact_listing_pages_complete_large_workspace(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Large artifact package",
            "goal": "Return every completed artifact without truncation.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Large artifact worker",
            "role": "processor",
            "profile": "codex-cli",
            "backend": "openclaw",
            "execution_mode": "host",
        },
    ).json()
    workspace = Path(worker["workspace_dir"])
    for index in range(620):
        path = workspace / "output" / "deliveries" / f"invoice-{index:04d}" / "canonical.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    paths = []
    cursor = 0
    while True:
        response = client.get(
            f"/v1/workers/{worker['worker_id']}/artifacts",
            params={"cursor": cursor, "limit": 200},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 620
        paths.extend(item["path"] for item in payload["items"])
        if payload["next_cursor"] is None:
            break
        assert payload["next_cursor"] > cursor
        cursor = payload["next_cursor"]

    assert len(paths) == 620
    assert len(set(paths)) == 620
    assert paths[0] == "output/deliveries/invoice-0000/canonical.json"
    assert paths[-1] == "output/deliveries/invoice-0619/canonical.json"


def test_deliverable_detection_ignores_provider_api_endpoints(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "qa_enterprise_launcher_ui_pass.txt").write_text("QA_ENTERPRISE_LAUNCHER_UI_PASS")
    worker = {
        "worker_id": "wrk_1",
        "workspace_dir": str(workspace),
        "execution_mode": "docker",
    }
    output = "Using https://example-ai.openai.azure.com/openai/v1 for the model.\nFINAL REPORT: wrote the marker file."

    assert not is_deliverable_url("https://example-ai.openai.azure.com/openai/v1")
    assert not is_deliverable_url("https://[REDACTED_CREDENTIAL]/openai/v1")
    payload = deliverable_payload(worker, {"state": "completed"}, output)

    assert payload is not None
    assert payload["kind"] == "file"
    assert payload["source"] == "workspace_file"
    assert payload["workspace_path"] == "qa_enterprise_launcher_ui_pass.txt"
    assert "cognitiveservices" not in json.dumps(payload)


def test_deliverable_detection_prefers_user_file_over_incidental_external_url(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_name = "qa_synthetic_claude_worker.txt"
    (workspace / artifact_name).write_text("SYNTHETIC_CLAUDE_WORKER_OK")
    worker = {
        "worker_id": "wrk_1",
        "workspace_dir": str(workspace),
        "execution_mode": "docker",
    }
    output = "\n".join(
        [
            "Fetched https://example.com while checking network reachability.",
            "FINAL REPORT: SYNTHETIC_CLAUDE_WORKER_OK",
            f"File: {artifact_name}",
        ]
    )

    payload = deliverable_payload(worker, {"state": "completed"}, output)

    assert payload is not None
    assert payload["kind"] == "file"
    assert payload["source"] == "workspace_file"
    assert payload["workspace_path"] == artifact_name
    assert "example.com" not in json.dumps(payload)


def test_shared_workspace_never_attributes_scanned_files_to_one_run(tmp_path):
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    old = workspace / "README.md"
    old.write_text("Existing shared project")
    current = notes / "restart-proof.md"
    current.write_text("New result from this member")
    site = workspace / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>Sibling page</h1>")
    os.utime(old, (1000, 1000))
    os.utime(current, (2000, 2000))
    worker = {"worker_id": "wrk_shared", "workspace_dir": str(workspace),
              "execution_mode": "docker", "_execution_workspace_mode": "shared"}

    payload = deliverable_payload(worker,
                                  {"state": "completed", "started_at": "1970-01-01T00:30:00+00:00"},
                                  "Completed the new note.")

    assert payload is None

    older_run = deliverable_payload(
        worker,
        {"state": "completed", "started_at": "1970-01-01T00:16:00+00:00",
         "ended_at": "1970-01-01T00:17:00+00:00"},
        "Completed the original README.",
    )
    assert older_run is None
    # A sibling write during the run cannot become this member's result.
    sibling = notes / "sibling.md"
    sibling.write_text("Concurrent sibling output")
    os.utime(sibling, (2001, 2001))
    assert deliverable_payload(worker, {"state": "completed", "started_at": "1970-01-01T00:30:00+00:00"}, "Text-only result") is None
    # Separate workspaces retain their useful automatic file preview.
    assert deliverable_payload({**worker, "_execution_workspace_mode": "isolated"},
                               {"state": "completed"}, "Completed") is not None


def test_incidental_external_url_is_not_a_deliverable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker = {
        "worker_id": "wrk_1",
        "workspace_dir": str(workspace),
        "execution_mode": "docker",
    }

    payload = deliverable_payload(
        worker,
        {"state": "completed"},
        "The official service endpoint is https://mcp.example.test/v1/mcp.",
    )

    assert payload is None


def test_deliverable_detection_ignores_glasshive_scaffold_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "glasshive_post_restart_smoke_20260523.txt").write_text("GH_POST_RESTART_OK")
    for name in (
        "AGENTS.md",
        "CLAUDE.md",
        "CODEX.md",
        "project-definition.md",
        "work-log.md",
    ):
        scaffold = workspace / name
        scaffold.write_text("# scaffold")
        os.utime(scaffold, (9999999999, 9999999999))
    helper_dir = workspace / "glasshive-host-tools"
    helper_dir.mkdir()
    helper = helper_dir / "capture-front-window.sh"
    helper.write_text("#!/usr/bin/env bash\n")
    os.utime(helper, (9999999999, 9999999999))
    mixed_case_helper_dir = workspace / "Node_Modules"
    mixed_case_helper_dir.mkdir()
    mixed_case_helper = mixed_case_helper_dir / "latest-helper-output.txt"
    mixed_case_helper.write_text("not a user artifact")
    os.utime(mixed_case_helper, (9999999999, 9999999999))
    codex_dir = workspace / ".codex"
    codex_dir.mkdir()
    codex_config = codex_dir / "config.toml"
    codex_config.write_text("[mcp_servers]\n")
    os.utime(codex_config, (9999999999, 9999999999))
    mcp_config = workspace / ".mcp.json"
    mcp_config.write_text("{}")
    os.utime(mcp_config, (9999999999, 9999999999))
    worker = {
        "worker_id": "wrk_1",
        "workspace_dir": str(workspace),
        "execution_mode": "host",
    }

    payload = deliverable_payload(worker, {"state": "completed"}, "FINAL REPORT: wrote the smoke marker file.")

    assert payload is not None
    assert payload["kind"] == "file"
    assert payload["source"] == "workspace_file"
    assert payload["workspace_path"] == "glasshive_post_restart_smoke_20260523.txt"


class ControllableRuntime:
    requires_run_start_identity = False

    def __init__(self) -> None:
        self.running = Event()
        self.release = Event()
        self.interrupted = Event()
        self.paused = Event()
        self.interrupt_run_ids: list[str | None] = []
        self.task_exited = Event()
        self.post_exit_collect_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "controllable/test"

    def isolated_resource_usage(self) -> dict[str, object]:
        return StubRuntime().isolated_resource_usage()

    def _info(self, worker: dict, pid: int | None = 1234) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="controllable",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"controllable:{worker['worker_id']}",
            state_dir="/tmp/controllable/state",
            workspace_dir="/tmp/controllable/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.paused.clear()
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.paused.set()
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.interrupt_run_ids.append(run_id)
        self.interrupted.set()
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self.interrupted.set()
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None if self.paused.is_set() else 1234)

    def collect_completed_run(
        self,
        worker: dict,
        run_id: str | None = None,
        instruction: str = "",
    ) -> dict[str, str] | None:
        _ = worker, instruction
        if not self.task_exited.is_set():
            return None
        self.post_exit_collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "STALE_COMPLETION_MUST_NOT_WIN",
            "error_text": "",
        }

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        try:
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
                self.running.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                if self.interrupted.is_set():
                    raise WorkerInterruptedError("Worker run was interrupted by the operator")
                if self.paused.is_set():
                    time.sleep(0.05)
                    continue
                if self.release.is_set():
                    return "CONTROLLABLE_OK"
                time.sleep(0.05)
            raise AssertionError("ControllableRuntime timed out in test")
        finally:
            self.task_exited.set()


class SteerableControllableRuntime(ControllableRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.instructions: list[str] = []

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        info = super().ensure_worker_ready(worker)
        if self.instructions:
            self.interrupted.clear()
        return info

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        self.instructions.append(instruction)
        if instruction.startswith("Operator steer instruction"):
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
            return "STEER_REDIRECT_OK"
        return super().run_task(worker, instruction, timeout_sec=timeout_sec)


class TerminationRaceRuntime(ControllableRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.task_exited = Event()

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self.interrupted.set()
        assert self.task_exited.wait(timeout=2), "active task did not observe termination"
        return self._info(worker, pid=None)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        try:
            return super().run_task(worker, instruction, timeout_sec=timeout_sec)
        finally:
            self.task_exited.set()


class RefreshingModelRuntime(ControllableRuntime):
    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model
        self.ensure_models: list[str] = []

    def resolve_model(self, profile: str) -> str:
        return self.model

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.ensure_models.append(str(worker.get("model") or ""))
        return self._info(worker)


def test_assign_run_refreshes_stale_worker_model_before_queue(
    tmp_path,
    background_consumers_disabled,
    request,
):
    _ = background_consumers_disabled
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = RefreshingModelRuntime("gpt-5.2-chat")
    service = WorkersProjectsService(store, runtime)
    request.addfinalizer(service.shutdown)
    service._ensure_worker_processor = lambda worker_id: None  # type: ignore[method-assign]

    project = service.create_project("demo-owner", "Refresh Model", "Refresh stale worker models.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Model Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    assert worker["model"] == "gpt-5.2-chat"

    runtime.model = "gpt-5.4"
    run = service.assign_run(worker["worker_id"], "Use the current configured model.")
    refreshed = store.get_worker(worker["worker_id"])

    assert run["state"] == "queued"
    assert refreshed["model"] == "gpt-5.4"
    assert refreshed["state"] == "starting"
    assert any(event["event_type"] == "worker.model_refreshed" for event in store.list_events(worker["worker_id"]))


def test_resume_worker_refreshes_stale_worker_model_before_runtime_start(
    tmp_path,
    background_consumers_disabled,
    request,
):
    _ = background_consumers_disabled
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = RefreshingModelRuntime("gpt-5.2-chat")
    service = WorkersProjectsService(store, runtime)
    request.addfinalizer(service.shutdown)
    service._ensure_worker_processor = lambda worker_id: None  # type: ignore[method-assign]

    project = service.create_project("demo-owner", "Resume Model", "Resume with current model.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Resume Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )

    store.update_worker(worker["worker_id"], compute_released_at=datetime.now(timezone.utc).isoformat())
    runtime.model = "gpt-5.4"
    resumed = service.resume_worker(worker["worker_id"])

    assert resumed["model"] == "gpt-5.4"
    assert resumed["compute_released_at"] is None
    assert runtime.ensure_models[-1] == "gpt-5.4"


def test_pause_resume_freezes_active_run_without_losing_it(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pause Resume", "goal": "Freeze and resume an active worker run."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Controllable Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Do a long running task."},
    ).json()
    if not runtime.running.wait(timeout=2):
        durable_run = client.app.state.store.get_run(run["run_id"]) or {}
        event_types = [
            event["event_type"]
            for event in client.app.state.store.list_events(worker["worker_id"])
        ]
        raise AssertionError(
            "worker run never started: "
            f"state={durable_run.get('state')!r}, "
            f"failure_class={durable_run.get('failure_class')!r}, "
            f"events={event_types!r}"
        )

    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"

    run_during_pause = client.get(f"/v1/runs/{run['run_id']}").json()
    assert run_during_pause["state"] == "paused"

    resumed = client.post(f"/v1/workers/{worker['worker_id']}/resume")
    assert resumed.status_code == 202
    assert resumed.json()["state"] == "running"

    runtime.release.set()
    settled = wait_for_run(client, run["run_id"], timeout=3.0)
    assert settled["state"] == "completed"
    assert settled["output_text"] == "CONTROLLABLE_OK"


class CompletionDuringPauseRuntime(ControllableRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.store: Store | None = None
        self.run_id = ""

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        assert self.store is not None
        assert self.store.finalize_run_if_state(
            self.run_id,
            "running",
            "completed",
            output_text="COMPLETED_DURING_PAUSE",
            **exact_terminal_generation(self.store, self.run_id),
        )
        return super().pause_worker(worker)


def test_completion_wins_pause_race_and_worker_does_not_regress_to_paused(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = CompletionDuringPauseRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    runtime.store = client.app.state.store

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pause race", "goal": "Keep terminal truth."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Pause Race Worker", "role": "coder"},
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Complete while pause is being accepted."},
    ).json()
    runtime.run_id = run["run_id"]
    assert runtime.running.wait(timeout=2), "worker run never started"

    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause")

    assert paused.status_code == 202
    assert client.get(f"/v1/runs/{run['run_id']}").json()["state"] == "completed"
    assert client.get(f"/v1/workers/{worker['worker_id']}").json()["state"] == "ready"


class RestartingHostPauseRuntime:
    requires_run_start_identity = True

    def __init__(self) -> None:
        self.first_started = Event()
        self.pause_requested = Event()
        self.invocations = 0
        self._run_start_observer = None

    def set_run_start_observer(self, observer) -> None:
        self._run_start_observer = observer

    def resolve_model(self, profile: str) -> str:
        return "host-pause-restart/test"

    def _info(self, worker: dict, pid: int | None = None) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="host-pause-restart",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=f"host-pause-restart:{worker['worker_id']}",
            state_dir=f"/tmp/{worker['worker_id']}/state",
            workspace_dir=f"/tmp/{worker['worker_id']}/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.pause_requested.clear()
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.pause_requested.set()
        return self._info(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.pause_requested.set()
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self.pause_requested.set()
        return self._info(worker)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        self.invocations += 1
        pid = 4241 + self.invocations
        assert self._run_start_observer is not None
        self._run_start_observer(
            {
                "worker_id": worker["worker_id"],
                "run_id": run_id,
                "identity_kind": "host_process",
                "pid": pid,
                "process_group": pid,
                "process_start_identity": f"ps-lstart:synthetic-host-{self.invocations}",
                "container_id": "",
                "session_id": f"synthetic-host-session-{self.invocations}",
            }
        )
        if self.invocations == 1:
            self.first_started.set()
            assert self.pause_requested.wait(timeout=3)
            raise WorkerPausedError("Host provider stopped for durable pause")
        return "HOST_RESUMED_SAME_RUN_OK"


def test_host_pause_resume_restarts_provider_on_the_same_durable_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = RestartingHostPauseRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        project = service.create_project(
            "demo-owner", "Host pause resume", "Resume the exact host mission.", "codex-cli"
        )
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="demo-owner",
            name="Host Pause Worker",
            role="coder",
            profile="codex-cli",
            backend="codex-cli",
            execution_mode="host",
        )
        run = service.assign_run(worker["worker_id"], "Continue after a host pause.")
        assert runtime.first_started.wait(timeout=2)
        first_running = store.get_run(run["run_id"])
        assert first_running and first_running["state"] == "running"
        first_attempt_id = str(first_running["active_attempt_id"])
        first_attempt = store.get_run_attempt(first_attempt_id)
        first_lease = store.get_active_host_run_lease_for_run(run["run_id"])
        assert first_attempt and first_attempt["state"] == "running"
        assert first_lease and first_lease["attempt_id"] == first_attempt_id

        service.pause_worker(worker["worker_id"])
        wait_until(lambda: not service._local_processor_owns(worker["worker_id"]))
        assert store.get_run(run["run_id"])["state"] == "paused"
        paused_lease = store.get_host_run_lease(first_lease["lease_id"])
        assert paused_lease and paused_lease["status"] == "released"

        service.resume_worker(worker["worker_id"])
        wait_until(
            lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed",
            timeout=3,
        )

        durable = store.get_run(run["run_id"])
        assert durable["output_text"] == "HOST_RESUMED_SAME_RUN_OK"
        assert runtime.invocations == 2
        assert [item["run_id"] for item in store.list_runs_for_worker(worker["worker_id"])] == [
            run["run_id"]
        ]
        attempts = store.list_run_attempts(run["run_id"])
        assert len(attempts) == 2
        assert attempts[0]["attempt_id"] == first_attempt_id
        assert attempts[0]["state"] == "retry_queued"
        assert attempts[0]["ended_at"]
        assert attempts[0]["terminal_reason"] == "host_pause_resume_restart"
        assert attempts[1]["attempt_number"] == attempts[0]["attempt_number"] + 1
        assert attempts[1]["state"] == "completed"
        assert durable["active_attempt_id"] == attempts[1]["attempt_id"]
        renewed_lease = store.get_host_run_lease(first_lease["lease_id"])
        assert renewed_lease
        assert renewed_lease["startup_token"] != first_lease["startup_token"]
    finally:
        service.shutdown()


class RaisingPauseRuntime:
    requires_run_start_identity = False

    def __init__(self) -> None:
        self.running = Event()
        self.paused = Event()

    def resolve_model(self, profile: str) -> str:
        return "pause-raising/test"

    def isolated_resource_usage(self) -> dict[str, object]:
        return StubRuntime().isolated_resource_usage()

    def _info(self, worker: dict, pid: int | None = 2222) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="pause-raising",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"pause-raising:{worker['worker_id']}",
            state_dir="/tmp/pause-raising/state",
            workspace_dir="/tmp/pause-raising/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.paused.clear()
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.paused.set()
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None if self.paused.is_set() else 2222)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        with runtime_start_boundary(worker):
            notify_runtime_started(worker)
            self.running.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.paused.is_set():
                raise WorkerPausedError("Worker was paused while a run was active")
            time.sleep(0.05)
        raise AssertionError("RaisingPauseRuntime timed out in test")


def test_worker_paused_error_finalizes_run_as_paused(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = RaisingPauseRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pause Finalize", "goal": "Finalize paused runs cleanly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Pause Finalize Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Begin a run that will raise WorkerPausedError when paused."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"

    deadline = time.time() + 3.0
    settled = None
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run['run_id']}")
        assert response.status_code == 200
        candidate = response.json()
        if candidate["state"] == "paused":
            settled = candidate
            break
        time.sleep(0.05)

    assert settled is not None, "run did not settle into paused state"
    assert settled["state"] == "paused"
    assert settled["error_text"] == "Paused by operator"


def test_interrupt_stops_active_run_and_keeps_worker_ready(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Interrupt", "goal": "Stop the current worker task cleanly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Interrupt Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Long task to be interrupted."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    interrupted = client.post(f"/v1/workers/{worker['worker_id']}/interrupt")
    assert interrupted.status_code == 202
    assert interrupted.json()["state"] == "ready"
    assert runtime.interrupt_run_ids == [run["run_id"]]

    settled = wait_for_run(client, run["run_id"], timeout=3.0)
    assert settled["state"] == "interrupted"
    assert runtime.post_exit_collect_run_ids == []

    worker_after = client.get(f"/v1/workers/{worker['worker_id']}").json()
    assert worker_after["state"] == "ready"


def test_interrupt_generation_race_returns_typed_conflict(tmp_path, monkeypatch):
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Interrupt race", "goal": "Keep exact control."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Race worker", "role": "coder"},
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Wait for a precise Stop."},
    ).json()
    assert runtime.running.wait(timeout=2)

    def changed(*_args, **_kwargs):
        raise RuntimeErrorBase("active_work_generation_changed")

    monkeypatch.setattr(client.app.state.service, "_claim_exact_run_control", changed)
    try:
        response = client.post(f"/v1/workers/{worker['worker_id']}/interrupt")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "active_work_generation_changed"
        assert client.get(f"/v1/runs/{run['run_id']}").json()["state"] == "running"
        assert runtime.interrupt_run_ids == []
    finally:
        runtime.release.set()
        client.app.state.service.shutdown()


def test_signed_view_interrupt_generation_race_returns_typed_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Signed interrupt race", "goal": "Keep exact control."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Signed race worker", "role": "coder"},
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Wait for a precise Stop."},
    ).json()
    assert runtime.running.wait(timeout=2)
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
    )
    ref_id = create_signed_link_ref(token=token, target_url=f"/watch/{worker['worker_id']}")

    def changed(*_args, **_kwargs):
        raise RuntimeErrorBase("active_work_generation_changed")

    monkeypatch.setattr(client.app.state.service, "_claim_exact_run_control", changed)
    try:
        response = client.post(f"/w/{ref_id}/actions/interrupt")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "active_work_generation_changed"
        assert client.get(f"/v1/runs/{run['run_id']}").json()["state"] == "running"
        assert runtime.interrupt_run_ids == []
    finally:
        runtime.release.set()
        client.app.state.service.shutdown()


def test_terminate_active_run_cannot_resurrect_worker(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = TerminationRaceRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Terminate", "goal": "Stop active compute permanently."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Terminate Worker", "role": "coder"},
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Long task to be terminated."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    terminated = client.post(f"/v1/workers/{worker['worker_id']}/terminate")

    assert terminated.status_code == 202
    assert terminated.json()["state"] == "terminated"
    assert wait_for_run(client, run["run_id"], timeout=3.0)["state"] == "cancelled"
    time.sleep(0.1)
    assert client.get(f"/v1/workers/{worker['worker_id']}").json()["state"] == "terminated"


def test_terminated_worker_rejects_new_work_without_stranding_runs(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Closed", "goal": "Reject work after closure."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Closed Worker", "role": "operator"},
    ).json()

    terminated = client.post(f"/v1/workers/{worker['worker_id']}/terminate")
    assert terminated.status_code == 202
    assert terminated.json()["state"] == "terminated"

    attempts = [
        client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Do not queue this assignment."},
        ),
        client.post(
            f"/v1/workers/{worker['worker_id']}/message",
            json={"message": "Do not queue this message."},
        ),
        client.post(
            f"/v1/workers/{worker['worker_id']}/steer",
            json={"message": "Do not queue this steer."},
        ),
        client.post(
            f"/v1/workers/{worker['worker_id']}/schedule",
            json={"instruction": "Do not schedule this run.", "delay_seconds": 60},
        ),
        client.post(
            f"/v1/workers/{worker['worker_id']}/launch-failed",
            json={"reason": "Do not overwrite closed state."},
        ),
        client.post(f"/v1/workers/{worker['worker_id']}/resume"),
        client.post(f"/v1/workers/{worker['worker_id']}/pause"),
        client.post(f"/v1/workers/{worker['worker_id']}/interrupt"),
    ]

    for response in attempts:
        assert response.status_code == 409, response.text
        assert "closed" in response.json()["detail"].lower()

    assert client.app.state.store.list_runs_for_worker(worker["worker_id"]) == []
    assert client.app.state.store.list_schedules_for_worker(worker["worker_id"]) == []
    with pytest.raises(WorkerClosedStoreError, match="closed"):
        client.app.state.store.create_run(
            worker["worker_id"],
            project["project_id"],
            "Simulate an assignment that raced with closure.",
        )
    assert client.get(f"/v1/workers/{worker['worker_id']}").json()["state"] == "terminated"


def test_one_shot_schedule_losing_race_to_workspace_close_is_rejected(tmp_path, monkeypatch):
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Schedule race", "goal": "Reject late work."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Schedule race", "role": "operator"},
    ).json()
    store = client.app.state.store
    original_create = store.create_scheduled_run

    def close_then_create(**kwargs):
        store.update_worker_state(worker["worker_id"], "terminated")
        return original_create(**kwargs)

    monkeypatch.setattr(store, "create_scheduled_run", close_then_create)
    response = client.post(
        f"/v1/workers/{worker['worker_id']}/schedule",
        json={"instruction": "Do not persist after closure wins.", "delay_seconds": 60},
    )

    assert response.status_code == 409, response.text
    assert "closed" in response.json()["detail"].lower()
    assert store.list_schedules_for_worker(worker["worker_id"]) == []


def test_slow_termination_publishes_close_before_runtime_teardown(tmp_path):
    class BlockingTerminationRuntime(StubRuntime):
        def __init__(self):
            self.entered = Event()
            self.release = Event()

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.entered.set()
            assert self.release.wait(timeout=3), "termination test did not release runtime teardown"
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = BlockingTerminationRuntime()
    service = WorkersProjectsService(store, runtime)
    project = service.create_project("demo-owner", "Closing", "Reject late work.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Closing workspace",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    errors: list[BaseException] = []

    def terminate():
        try:
            service.terminate_worker(worker["worker_id"])
        except BaseException as exc:  # pragma: no cover - assertion reports worker thread errors
            errors.append(exc)

    thread = Thread(target=terminate)
    thread.start()
    try:
        assert runtime.entered.wait(timeout=2), "termination never reached runtime teardown"
        assert store.get_worker(worker["worker_id"])["state"] == "terminating"
        with pytest.raises(ControlPlaneConflict, match="closed"):
            service.assign_run(worker["worker_id"], "Do not queue during teardown.")
        assert store.list_runs_for_worker(worker["worker_id"]) == []
    finally:
        runtime.release.set()
        thread.join(timeout=3)
        service.shutdown()

    assert not thread.is_alive()
    assert errors == []
    assert store.get_worker(worker["worker_id"])["state"] == "terminated"


def test_resume_losing_race_to_termination_cannot_resurrect_workspace(tmp_path):
    class BlockingResumeRuntime(StubRuntime):
        def __init__(self):
            self.entered = Event()
            self.release = Event()
            self.compute_active = False
            self.terminate_calls = 0

        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            self.entered.set()
            assert self.release.wait(timeout=3), "resume test did not release runtime startup"
            self.compute_active = True
            return super().ensure_worker_ready(worker)

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminate_calls += 1
            self.compute_active = False
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = BlockingResumeRuntime()
    service = WorkersProjectsService(store, runtime)
    project = service.create_project("demo-owner", "Resume race", "Do not resurrect.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Resume race",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    store.update_worker_state(worker["worker_id"], "paused")
    errors: list[BaseException] = []

    def resume():
        try:
            service.resume_worker(worker["worker_id"])
        except BaseException as exc:  # pragma: no cover - assertion reports worker thread errors
            errors.append(exc)

    thread = Thread(target=resume)
    thread.start()
    try:
        assert runtime.entered.wait(timeout=2), "resume never reached runtime startup"
        terminated = service.terminate_worker(worker["worker_id"])
        assert terminated["state"] == "terminated"
    finally:
        runtime.release.set()
        thread.join(timeout=3)
        service.shutdown()

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ControlPlaneConflict)
    assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    assert runtime.compute_active is False
    assert runtime.terminate_calls == 2


def test_desktop_action_losing_race_to_termination_cleans_recreated_compute(tmp_path):
    class BlockingDesktopRuntime(StubRuntime):
        def __init__(self):
            self.entered = Event()
            self.release = Event()
            self.compute_active = False
            self.terminate_calls = 0

        def desktop_action(
            self,
            worker: dict,
            action: str,
            *,
            url: str | None = None,
            run_id: str | None = None,
        ) -> dict[str, object]:
            self.entered.set()
            assert self.release.wait(timeout=3), "desktop test did not release runtime action"
            self.compute_active = True
            return {"status": "ready", "notes": action}

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminate_calls += 1
            self.compute_active = False
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = BlockingDesktopRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Desktop race", "Close wins.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Desktop race",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    errors: list[BaseException] = []

    def act() -> None:
        try:
            service.desktop_action(worker["worker_id"], "desktop")
        except BaseException as exc:  # pragma: no cover - assertion reports worker thread errors
            errors.append(exc)

    thread = Thread(target=act)
    thread.start()
    try:
        assert runtime.entered.wait(timeout=2), "desktop action never reached runtime"
        assert service.terminate_worker(worker["worker_id"])["state"] == "terminated"
    finally:
        runtime.release.set()
        thread.join(timeout=3)
        service.shutdown()

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ControlPlaneConflict)
    assert runtime.compute_active is False
    assert runtime.terminate_calls == 2
    assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    assert "worker.desktop_action" not in {
        event["event_type"] for event in store.list_events(worker["worker_id"])
    }


def test_queued_run_losing_last_start_boundary_to_close_never_executes(tmp_path):
    class BlockingStartRuntime(StubRuntime):
        def __init__(self):
            self.entered = Event()
            self.release = Event()
            self.ran = False

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            self.entered.set()
            assert self.release.wait(timeout=3), "run start test did not release runtime"
            with runtime_start_boundary(worker):
                self.ran = True
                notify_runtime_started(worker)
            return "should not run"

    store = Store(str(tmp_path / "runtime.db"))
    runtime = BlockingStartRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Start fence", "Close wins.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Start fence",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    run = service.assign_run(worker["worker_id"], "Do not execute after Close.")
    try:
        assert runtime.entered.wait(timeout=2), "processor never reached the final start boundary"
        assert service.terminate_worker(worker["worker_id"])["state"] == "terminated"
    finally:
        runtime.release.set()
        deadline = time.time() + 2
        while time.time() < deadline and store.get_run(run["run_id"])["state"] == "running":
            time.sleep(0.01)
        service.shutdown()

    assert runtime.ran is False
    assert store.get_run(run["run_id"])["state"] == "cancelled"
    assert "run.started" not in {
        event["event_type"] for event in store.list_events(worker["worker_id"])
    }


def test_close_remains_prompt_after_runtime_start_boundary_is_released(tmp_path):
    class LongRunningRuntime(StubRuntime):
        def __init__(self):
            self.started = Event()
            self.stopped = Event()

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            with runtime_start_boundary(worker):
                notify_runtime_started(worker)
                self.started.set()
            assert self.stopped.wait(timeout=3), "close did not interrupt the active runtime"
            raise WorkerTerminatedError("closed")

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.stopped.set()
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = LongRunningRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Prompt close", "Close promptly.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Prompt close",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    service.assign_run(worker["worker_id"], "Run until closed.")
    try:
        assert runtime.started.wait(timeout=2)
        started_at = time.monotonic()
        closed = service.terminate_worker(worker["worker_id"])
        elapsed = time.monotonic() - started_at
    finally:
        service.shutdown()

    assert closed["state"] == "terminated"
    assert elapsed < 0.5


def test_openclaw_request_is_accepted_inside_runtime_start_boundary(tmp_path, monkeypatch):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))
    worker = {
        "worker_id": "wrk_boundary",
        "profile": "openclaw-general",
        "model": "openclaw",
    }
    in_boundary = {"value": False}
    accepted: list[bool] = []
    started: list[bool] = []
    persisted: list[RuntimeInfo] = []

    @contextmanager
    def guard():
        assert in_boundary["value"] is False
        in_boundary["value"] = True
        try:
            yield
        finally:
            in_boundary["value"] = False

    def handler(request: httpx.Request) -> httpx.Response:
        accepted.append(in_boundary["value"])
        assert [info.pid for info in persisted] == [123]
        assert json.loads(request.content)["stream"] is True
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'event: response.output_text.delta\n'
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                b'event: response.completed\n'
                b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
                b'data: [DONE]\n\n'
            ),
            request=request,
        )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method: str, url: str, **kwargs):
            return httpx.Request(method, url, **kwargs)

        def send(self, request: httpx.Request, *, stream: bool = False):
            _ = stream
            return handler(request)

    monkeypatch.setattr(runtime, "_prepare_worker_runtime", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_start_worker_runtime",
        lambda _worker: RuntimeInfo(
            runtime="openclaw",
            model="openclaw",
            gateway_url="http://openclaw.invalid",
            gateway_port=1,
            gateway_token="synthetic-token",
            session_key="synthetic-session",
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            pid=123,
        ),
    )
    monkeypatch.setattr(runtime, "_wait_for_ready", lambda _port, _token: None)
    monkeypatch.setattr(
        openclaw_runtime_module.httpx,
        "Client",
        FakeClient,
    )
    worker["_runtime_start_guard"] = guard
    worker["_runtime_started_callback"] = lambda: started.append(True)
    worker["_runtime_info_callback"] = persisted.append

    assert runtime.run_task(worker, "Return ok.") == "ok"
    assert accepted == [True]
    assert started == [True]
    assert [info.pid for info in persisted] == [123]
    assert in_boundary["value"] is False


def test_openclaw_stream_releases_start_boundary_before_long_response_body(tmp_path, monkeypatch):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))
    boundary_lock = Lock()
    body_read_started = Event()
    release_body = Event()
    started = Event()
    result: list[str] = []
    errors: list[BaseException] = []

    @contextmanager
    def guard():
        with boundary_lock:
            yield

    class BlockingStream(httpx.SyncByteStream):
        def __iter__(self):
            body_read_started.set()
            assert release_body.wait(timeout=3)
            yield (
                b'event: response.output_text.delta\n'
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                b'event: response.completed\n'
                b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
                b'data: [DONE]\n\n'
            )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method: str, url: str, **kwargs):
            return httpx.Request(method, url, **kwargs)

        def send(self, request: httpx.Request, *, stream: bool = False):
            assert stream is True
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=BlockingStream(),
                request=request,
            )

    monkeypatch.setattr(runtime, "_prepare_worker_runtime", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_start_worker_runtime",
        lambda _worker: RuntimeInfo(
            runtime="openclaw",
            model="openclaw",
            gateway_url="http://openclaw.invalid",
            gateway_port=1,
            gateway_token="synthetic-token",
            session_key="synthetic-session",
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            pid=123,
        ),
    )
    monkeypatch.setattr(runtime, "_wait_for_ready", lambda _port, _token: None)
    monkeypatch.setattr(openclaw_runtime_module.httpx, "Client", FakeClient)
    worker = {
        "worker_id": "wrk_stream_boundary",
        "profile": "openclaw-general",
        "model": "openclaw",
        "_runtime_start_guard": guard,
        "_runtime_started_callback": started.set,
    }

    def run() -> None:
        try:
            result.append(runtime.run_task(worker, "Return ok."))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    run_thread = Thread(target=run)
    run_thread.start()
    assert body_read_started.wait(timeout=2)
    assert started.is_set()
    assert boundary_lock.acquire(timeout=0.25)
    boundary_lock.release()
    release_body.set()
    run_thread.join(timeout=3)

    assert not run_thread.is_alive()
    assert errors == []
    assert result == ["ok"]


def test_openclaw_stream_rejects_partial_output_without_completed_event(tmp_path, monkeypatch):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method: str, url: str, **kwargs):
            return httpx.Request(method, url, **kwargs)

        def send(self, request: httpx.Request, *, stream: bool = False):
            _ = stream
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                    b'data: [DONE]\n\n'
                ),
                request=request,
            )

    monkeypatch.setattr(runtime, "_prepare_worker_runtime", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_start_worker_runtime",
        lambda _worker: RuntimeInfo(
            runtime="openclaw",
            model="openclaw",
            gateway_url="http://openclaw.invalid",
            gateway_port=1,
            gateway_token="synthetic-token",
            session_key="synthetic-session",
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            pid=123,
        ),
    )
    monkeypatch.setattr(runtime, "_wait_for_ready", lambda _port, _token: None)
    monkeypatch.setattr(openclaw_runtime_module.httpx, "Client", FakeClient)

    with pytest.raises(RuntimeErrorBase, match="before response.completed"):
        runtime.run_task(
            {
                "worker_id": "wrk_partial_stream",
                "profile": "openclaw-general",
                "model": "openclaw",
            },
            "Return a complete response.",
        )


def test_openclaw_cleans_new_gateway_when_runtime_info_cannot_be_persisted(tmp_path, monkeypatch):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))
    terminated_pids: list[int | None] = []
    send_calls: list[httpx.Request] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method: str, url: str, **kwargs):
            return httpx.Request(method, url, **kwargs)

        def send(self, request: httpx.Request, *, stream: bool = False):
            _ = stream
            send_calls.append(request)
            raise AssertionError("request must not be sent without durable runtime metadata")

    info = RuntimeInfo(
        runtime="openclaw",
        model="openclaw",
        gateway_url="http://openclaw.invalid",
        gateway_port=1,
        gateway_token="synthetic-token",
        session_key="synthetic-session",
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        pid=43210,
    )
    monkeypatch.setattr(runtime, "_prepare_worker_runtime", lambda: None)
    monkeypatch.setattr(runtime, "_start_worker_runtime", lambda _worker: info)
    monkeypatch.setattr(runtime, "_wait_for_ready", lambda _port, _token: None)

    def terminate(worker: dict) -> RuntimeInfo:
        terminated_pids.append(worker.get("pid"))
        return RuntimeInfo(**{**info.__dict__, "pid": None})

    def reject_persistence(_info: RuntimeInfo) -> None:
        raise OSError("synthetic state unavailable")

    monkeypatch.setattr(runtime, "terminate_worker", terminate)
    monkeypatch.setattr(openclaw_runtime_module.httpx, "Client", FakeClient)

    with pytest.raises(OSError, match="synthetic state unavailable"):
        runtime.run_task(
            {
                "worker_id": "wrk_persist_failure",
                "profile": "openclaw-general",
                "model": "openclaw",
                "_runtime_info_callback": reject_persistence,
            },
            "Do not send without durable runtime identity.",
        )

    assert terminated_pids == [43210]
    assert send_calls == []


def test_openclaw_cold_readiness_wait_does_not_block_close(tmp_path, monkeypatch):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))
    publish_synthetic_isolated_capacity(monkeypatch, runtime)
    readiness_waiting = Event()
    release_readiness = Event()
    terminated_pids: list[int | None] = []
    info = RuntimeInfo(
        runtime="openclaw",
        model="openclaw",
        gateway_url="http://openclaw.invalid",
        gateway_port=1,
        gateway_token="synthetic-token",
        session_key="synthetic-session",
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        pid=43210,
    )

    def wait_for_ready(_port: int, _token: str) -> None:
        readiness_waiting.set()
        assert release_readiness.wait(timeout=3)

    def terminate(worker: dict) -> RuntimeInfo:
        terminated_pids.append(worker.get("pid"))
        release_readiness.set()
        return RuntimeInfo(**{**info.__dict__, "pid": None})

    monkeypatch.setattr(runtime, "_prepare_worker_runtime", lambda: None)
    monkeypatch.setattr(runtime, "_start_worker_runtime", lambda _worker: info)
    monkeypatch.setattr(runtime, "_wait_for_ready", wait_for_ready)
    monkeypatch.setattr(runtime, "terminate_worker", terminate)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Cold readiness", "Close promptly.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Cold readiness",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    run = service.assign_run(worker["worker_id"], "Wait for readiness.")
    try:
        assert readiness_waiting.wait(timeout=2)
        assert store.get_worker(worker["worker_id"])["pid"] == 43210
        started_at = time.monotonic()
        closed = service.terminate_worker(worker["worker_id"])
        elapsed = time.monotonic() - started_at

        assert closed["state"] == "terminated"
        assert elapsed < 0.5
        assert terminated_pids and terminated_pids[0] == 43210
    finally:
        release_readiness.set()
        time.sleep(0.05)
        service.shutdown()

    assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    assert store.get_run(run["run_id"])["state"] == "cancelled"


def test_openclaw_shared_preparation_does_not_hold_close_start_fence(tmp_path, monkeypatch):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))
    publish_synthetic_isolated_capacity(monkeypatch, runtime)
    preparation_started = Event()
    release_preparation = Event()
    start_calls: list[str] = []
    info = RuntimeInfo(
        runtime="openclaw",
        model="openclaw",
        gateway_url="",
        gateway_port=None,
        gateway_token="synthetic-token",
        session_key="synthetic-session",
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        pid=None,
    )

    def prepare() -> None:
        preparation_started.set()
        assert release_preparation.wait(timeout=3)

    def start(worker: dict) -> RuntimeInfo:
        start_calls.append(str(worker["worker_id"]))
        return info

    def terminate(_worker: dict) -> RuntimeInfo:
        release_preparation.set()
        return info

    monkeypatch.setattr(runtime, "_prepare_worker_runtime", prepare)
    monkeypatch.setattr(runtime, "_start_worker_runtime", start)
    monkeypatch.setattr(runtime, "terminate_worker", terminate)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Cold preparation", "Close promptly.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Cold preparation",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    run = service.assign_run(worker["worker_id"], "Prepare shared runtime assets.")
    try:
        assert preparation_started.wait(timeout=2)
        started_at = time.monotonic()
        closed = service.terminate_worker(worker["worker_id"])
        elapsed = time.monotonic() - started_at

        assert closed["state"] == "terminated"
        assert elapsed < 0.5
    finally:
        release_preparation.set()
        time.sleep(0.05)
        service.shutdown()

    assert start_calls == []
    assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    assert store.get_run(run["run_id"])["state"] == "cancelled"


@pytest.mark.parametrize("operation", ["resume", "synchronous_create"])
def test_openclaw_non_run_readiness_persists_pid_before_close(
    tmp_path,
    monkeypatch,
    operation,
):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))
    readiness_waiting = Event()
    release_readiness = Event()
    worker_started = Event()
    worker_ids: list[str] = []
    live_pids: set[int] = set()
    terminated_pids: list[int | None] = []
    results: list[dict] = []
    errors: list[BaseException] = []
    info_template = {
        "runtime": "openclaw",
        "model": "openclaw",
        "gateway_url": "http://openclaw.invalid",
        "gateway_port": 1,
        "gateway_token": "synthetic-token",
        "session_key": "synthetic-session",
        "state_dir": str(tmp_path / "state"),
        "workspace_dir": str(tmp_path / "workspace"),
    }

    def start(worker: dict) -> RuntimeInfo:
        worker_ids.append(str(worker["worker_id"]))
        live_pids.add(43210)
        worker_started.set()
        return RuntimeInfo(**info_template, pid=43210)

    def wait_for_ready(_port: int, _token: str) -> None:
        readiness_waiting.set()
        assert release_readiness.wait(timeout=3)

    def terminate(worker: dict) -> RuntimeInfo:
        pid = worker.get("pid")
        terminated_pids.append(pid)
        if isinstance(pid, int):
            live_pids.discard(pid)
        release_readiness.set()
        return RuntimeInfo(**info_template, pid=None)

    monkeypatch.setattr(runtime, "_prepare_worker_runtime", lambda: None)
    monkeypatch.setattr(runtime, "_start_worker_runtime", start)
    monkeypatch.setattr(runtime, "_wait_for_ready", wait_for_ready)
    monkeypatch.setattr(runtime, "terminate_worker", terminate)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Non-run start", "Persist before Close.", "codex-cli")
    prepared_worker = None
    if operation == "resume":
        prepared_worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="demo-owner",
            name="Resume start",
            role="operator",
            profile="codex-cli",
            backend="openclaw",
            start_synchronously=False,
        )

    def start_operation() -> None:
        try:
            if operation == "resume":
                results.append(service.resume_worker(prepared_worker["worker_id"]))
            else:
                results.append(
                    service.create_worker(
                        project_id=project["project_id"],
                        owner_id="demo-owner",
                        name="Synchronous start",
                        role="operator",
                        profile="codex-cli",
                        backend="openclaw",
                        start_synchronously=True,
                    )
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    operation_thread = Thread(target=start_operation)
    operation_thread.start()
    try:
        assert worker_started.wait(timeout=2)
        assert readiness_waiting.wait(timeout=2)
        worker_id = worker_ids[0]
        assert store.get_worker(worker_id)["pid"] == 43210
        started_at = time.monotonic()
        closed = service.terminate_worker(worker_id)
        elapsed = time.monotonic() - started_at

        assert closed["state"] == "terminated"
        assert elapsed < 0.5
        assert terminated_pids and terminated_pids[0] == 43210
    finally:
        release_readiness.set()
        operation_thread.join(timeout=3)
        service.shutdown()

    assert not operation_thread.is_alive()
    assert live_pids == set()
    assert store.get_worker(worker_ids[0])["state"] == "terminated"
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], ControlPlaneConflict)


@pytest.mark.parametrize("operation", ["resume", "synchronous_create"])
def test_openclaw_non_run_readiness_failure_cleans_published_pid(
    tmp_path,
    monkeypatch,
    operation,
):
    runtime = OpenClawRuntime(base_dir=str(tmp_path / "runtime"))
    live_pids = {43210}
    terminated_pids: list[int | None] = []
    info_template = {
        "runtime": "openclaw",
        "model": "openclaw",
        "gateway_url": "http://openclaw.invalid",
        "gateway_port": 1,
        "gateway_token": "synthetic-token",
        "session_key": "synthetic-session",
        "state_dir": str(tmp_path / "state"),
        "workspace_dir": str(tmp_path / "workspace"),
    }

    def terminate(worker: dict) -> RuntimeInfo:
        pid = worker.get("pid")
        terminated_pids.append(pid)
        if isinstance(pid, int):
            live_pids.discard(pid)
        return RuntimeInfo(**info_template, pid=None)

    def readiness_failure(_port: int, _token: str) -> None:
        raise RuntimeErrorBase("synthetic gateway not ready")

    monkeypatch.setattr(runtime, "_prepare_worker_runtime", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_start_worker_runtime",
        lambda _worker: RuntimeInfo(**info_template, pid=43210),
    )
    monkeypatch.setattr(runtime, "_wait_for_ready", readiness_failure)
    monkeypatch.setattr(runtime, "terminate_worker", terminate)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Readiness failure", "Clean compute.", "codex-cli")
    try:
        if operation == "resume":
            worker = service.create_worker(
                project_id=project["project_id"],
                owner_id="demo-owner",
                name="Resume readiness failure",
                role="operator",
                profile="codex-cli",
                backend="openclaw",
                start_synchronously=False,
            )
            result = service.resume_worker(worker["worker_id"])
        else:
            result = service.create_worker(
                project_id=project["project_id"],
                owner_id="demo-owner",
                name="Create readiness failure",
                role="operator",
                profile="codex-cli",
                backend="openclaw",
                start_synchronously=True,
            )

        stored = store.get_worker(result["worker_id"])
        assert result["state"] == "failed"
        assert stored["state"] == "failed"
        assert stored["pid"] is None
        assert "synthetic gateway not ready" in str(stored["last_error"])
        assert terminated_pids == [43210]
        assert live_pids == set()
    finally:
        service.shutdown()


@pytest.mark.parametrize("operation", ["resume", "synchronous_create"])
def test_non_run_ready_finalization_is_ordered_before_concurrent_close(
    tmp_path,
    monkeypatch,
    operation,
):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Ready ordering", "Order lifecycle events.", "codex-cli")
    prepared_worker = None
    if operation == "resume":
        prepared_worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="demo-owner",
            name="Resume ordering",
            role="operator",
            profile="codex-cli",
            backend="openclaw",
            start_synchronously=False,
        )

    original_update_worker = store.update_worker
    ready_written = Event()
    release_ready = Event()
    worker_ids: list[str] = []

    def block_after_ready(worker_id: str, **fields):
        updated = original_update_worker(worker_id, **fields)
        if fields.get("state") == "ready":
            worker_ids.append(worker_id)
            ready_written.set()
            assert release_ready.wait(timeout=3)
        return updated

    monkeypatch.setattr(store, "update_worker", block_after_ready)
    operation_results: list[dict] = []
    operation_errors: list[BaseException] = []
    close_results: list[dict] = []

    def run_operation() -> None:
        try:
            if operation == "resume":
                operation_results.append(service.resume_worker(prepared_worker["worker_id"]))
            else:
                operation_results.append(
                    service.create_worker(
                        project_id=project["project_id"],
                        owner_id="demo-owner",
                        name="Create ordering",
                        role="operator",
                        profile="codex-cli",
                        backend="openclaw",
                        start_synchronously=True,
                    )
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            operation_errors.append(exc)

    operation_thread = Thread(target=run_operation)
    operation_thread.start()
    assert ready_written.wait(timeout=2)
    worker_id = worker_ids[0]
    close_thread = Thread(
        target=lambda: close_results.append(service.terminate_worker(worker_id))
    )
    close_thread.start()
    time.sleep(0.05)
    assert close_results == []
    release_ready.set()
    operation_thread.join(timeout=3)
    close_thread.join(timeout=3)
    service.shutdown()

    assert not operation_thread.is_alive()
    assert not close_thread.is_alive()
    assert operation_errors == []
    assert operation_results[0]["state"] == "ready"
    assert close_results[0]["state"] == "terminated"
    assert store.get_worker(worker_id)["state"] == "terminated"
    event_types = [event["event_type"] for event in store.list_events(worker_id)]
    ready_event = "worker.resumed" if operation == "resume" else "worker.ready"
    assert event_types.index(ready_event) < event_types.index("worker.terminated")


def test_runtime_info_is_persisted_before_close_can_follow_accepted_start(tmp_path):
    class PublishingRuntime(StubRuntime):
        def __init__(self):
            self.body_active = Event()
            self.release_body = Event()
            self.terminated_pids: list[int | None] = []

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            with runtime_start_boundary(worker):
                callback = worker.get("_runtime_info_callback")
                assert callable(callback)
                callback(self._runtime_info(worker, pid=43210))
                notify_runtime_started(worker)
            self.body_active.set()
            assert self.release_body.wait(timeout=3)
            return "late output"

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated_pids.append(worker.get("pid"))
            return self._runtime_info(worker, pid=None)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = PublishingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Persist runtime", "Close known compute.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Persist runtime",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    run = service.assign_run(worker["worker_id"], "Start and then close.")
    try:
        assert runtime.body_active.wait(timeout=2)
        assert store.get_worker(worker["worker_id"])["pid"] == 43210
        closed = service.terminate_worker(worker["worker_id"])
        assert closed["state"] == "terminated"
        assert runtime.terminated_pids == [43210]
    finally:
        runtime.release_body.set()
        time.sleep(0.05)
        service.shutdown()

    assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    assert store.get_run(run["run_id"])["state"] == "cancelled"


def test_stale_runtime_info_callback_cannot_overwrite_new_run_identity(tmp_path):
    class GenerationPublishingRuntime(StubRuntime):
        def __init__(self):
            self.callbacks: list[object] = []
            self.latest_pid: int | None = None
            self.second_body_active = Event()
            self.release_second = Event()

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            callback = worker.get("_runtime_info_callback")
            assert callable(callback)
            self.latest_pid = 50001 + len(self.callbacks)
            self.callbacks.append(callback)
            with runtime_start_boundary(worker):
                callback(self._runtime_info(worker, pid=self.latest_pid))
                notify_runtime_started(worker)
            if len(self.callbacks) == 2:
                self.second_body_active.set()
                assert self.release_second.wait(timeout=3)
            return "completed"

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            return self._runtime_info(worker, pid=self.latest_pid)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = GenerationPublishingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project(
        "demo-owner", "Runtime generations", "Keep exact identity.", "codex-cli"
    )
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Runtime generations",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    first = service.assign_run(worker["worker_id"], "First generation.")
    wait_until(lambda: store.get_run(first["run_id"])["state"] == "completed")
    second = service.assign_run(worker["worker_id"], "Second generation.")
    try:
        assert runtime.second_body_active.wait(timeout=2)
        assert store.get_worker(worker["worker_id"])["pid"] == 50002
        stale_info = runtime._runtime_info(worker, pid=50001)
        with pytest.raises(RuntimeErrorBase, match="generation"):
            runtime.callbacks[0](stale_info)
        assert store.get_worker(worker["worker_id"])["pid"] == 50002
    finally:
        runtime.release_second.set()
        wait_until(lambda: store.get_run(second["run_id"])["state"] == "completed")
        service.shutdown()


@pytest.mark.parametrize("operation", ["pause", "interrupt"])
def test_control_before_final_start_boundary_prevents_execution_without_closing(tmp_path, operation):
    class BlockingStartRuntime(StubRuntime):
        def __init__(self):
            self.entered = Event()
            self.release = Event()
            self.ran = False

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            self.entered.set()
            assert self.release.wait(timeout=3), "control test did not release run start"
            with runtime_start_boundary(worker):
                self.ran = True
                notify_runtime_started(worker)
            return "should not run"

    store = Store(str(tmp_path / "runtime.db"))
    runtime = BlockingStartRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Control fence", "Control wins.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Control fence",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    run = service.assign_run(worker["worker_id"], "Do not start after control.")
    try:
        assert runtime.entered.wait(timeout=2)
        if operation == "pause":
            result = service.pause_worker(worker["worker_id"])
            assert result["state"] == "paused"
        else:
            result = service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
            assert result["state"] == "ready"
    finally:
        runtime.release.set()
        time.sleep(0.05)
        service.shutdown()

    assert runtime.ran is False
    assert store.get_worker(worker["worker_id"])["state"] == (
        "paused" if operation == "pause" else "ready"
    )
    assert store.get_run(run["run_id"])["state"] == (
        "paused" if operation == "pause" else "interrupted"
    )
    assert "run.started" not in {
        event["event_type"] for event in store.list_events(worker["worker_id"])
    }


def test_pause_started_snapshot_is_serialized_with_runtime_start(tmp_path, monkeypatch):
    class SnapshotRaceRuntime(StubRuntime):
        def __init__(self):
            self.before_boundary = Event()
            self.try_boundary = Event()
            self.ran = False

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            self.before_boundary.set()
            assert self.try_boundary.wait(timeout=3)
            with runtime_start_boundary(worker):
                self.ran = True
                notify_runtime_started(worker)
            return "should not run"

    store = Store(str(tmp_path / "runtime.db"))
    runtime = SnapshotRaceRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("demo-owner", "Pause snapshot", "Pause wins.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Pause snapshot",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    run = service.assign_run(worker["worker_id"], "Do not cross the pause boundary.")
    assert runtime.before_boundary.wait(timeout=2)

    original_has_run_event = store.has_run_event
    snapshot_read = Event()
    release_snapshot = Event()

    def block_false_started_snapshot(run_id: str, event_type: str) -> bool:
        result = original_has_run_event(run_id, event_type)
        if run_id == run["run_id"] and event_type == "run.started" and not result:
            snapshot_read.set()
            assert release_snapshot.wait(timeout=3)
        return result

    monkeypatch.setattr(store, "has_run_event", block_false_started_snapshot)
    pause_result: dict[str, object] = {}
    pause_errors: list[BaseException] = []

    def pause() -> None:
        try:
            pause_result.update(service.pause_worker(worker["worker_id"]))
        except BaseException as exc:  # pragma: no cover - asserted below
            pause_errors.append(exc)

    pause_thread = Thread(target=pause)
    pause_thread.start()
    assert snapshot_read.wait(timeout=2)
    runtime.try_boundary.set()
    time.sleep(0.05)
    assert runtime.ran is False
    release_snapshot.set()
    pause_thread.join(timeout=3)
    time.sleep(0.05)
    service.shutdown()

    assert not pause_thread.is_alive()
    assert pause_errors == []
    assert pause_result["state"] == "paused"
    assert runtime.ran is False
    assert store.get_run(run["run_id"])["state"] == "paused"
    assert not store.has_run_event(run["run_id"], "run.started")


def test_concurrent_terminate_calls_have_one_runtime_owner(tmp_path):
    class SingleOwnerTerminationRuntime(StubRuntime):
        def __init__(self):
            self.entered = Event()
            self.release = Event()
            self.calls = 0

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=3), "termination test did not release runtime teardown"
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = SingleOwnerTerminationRuntime()
    service = WorkersProjectsService(store, runtime)
    project = service.create_project("demo-owner", "Close once", "One teardown owner.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Close once",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    first_results: list[dict] = []

    thread = Thread(target=lambda: first_results.append(service.terminate_worker(worker["worker_id"])))
    thread.start()
    try:
        assert runtime.entered.wait(timeout=2), "first termination did not reach runtime teardown"
        observer = service.terminate_worker(worker["worker_id"])
        assert observer["state"] == "terminating"
        assert runtime.calls == 1
    finally:
        runtime.release.set()
        thread.join(timeout=3)
        service.shutdown()

    assert not thread.is_alive()
    assert first_results[0]["state"] == "terminated"
    assert runtime.calls == 1
    assert store.get_worker(worker["worker_id"])["state"] == "terminated"


def test_startup_reconcile_finishes_persisted_terminating_workspace(tmp_path):
    class RecordingTerminationRuntime(StubRuntime):
        def __init__(self):
            self.calls = 0

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.calls += 1
            return super().terminate_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = RecordingTerminationRuntime()
    service = WorkersProjectsService(store, runtime)
    project = service.create_project("demo-owner", "Recover close", "Finish durable closure.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Recover close",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    store.update_worker(worker["worker_id"], state="terminating", pid=12345)

    try:
        service._reconcile_worker_row(store.get_worker(worker["worker_id"]))
    finally:
        service.shutdown()

    assert runtime.calls == 1
    assert store.get_worker(worker["worker_id"])["state"] == "terminated"


def test_termination_cancels_future_and_recurring_work_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    project = service.create_project("demo-owner", "Cancel future", "Close all future work.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Cancel future",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    one_shot = service.schedule_run(
        worker["worker_id"],
        "Never run this one-shot task.",
        run_at="2027-01-02T03:04:05+00:00",
    )
    recurring = service.create_recurring_schedule(
        worker["worker_id"],
        "Never run this recurring task.",
        recurrence_type="interval",
        interval_seconds=3600,
        first_run_at="2027-01-02T03:04:05+00:00",
    )

    try:
        assert service.terminate_worker(worker["worker_id"])["state"] == "terminated"
        schedule = store.get_schedule(one_shot["schedule_id"])
        definition = store.get_recurring_schedule_definition(
            recurring["definition_id"], tenant_id="local", owner_id="demo-owner"
        )
        assert schedule["state"] == "cancelled"
        assert schedule["last_error"] == "workspace_closed"
        assert definition["active"] is False
        assert service.process_due_schedules_once(now_iso="2027-01-02T04:04:05+00:00") == []
        assert store.get_schedule(one_shot["schedule_id"])["state"] == "cancelled"
        assert store.list_runs_for_worker(worker["worker_id"]) == []
    finally:
        service.shutdown()


def test_terminate_failure_is_not_reported_as_success(tmp_path):
    class FailingTerminateRuntime(StubRuntime):
        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            raise RuntimeError("compute remained active")

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, FailingTerminateRuntime())
    try:
        project = service.create_project("owner", "Failure", "Surface termination failure", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Failure Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )

        with pytest.raises(RuntimeError, match="compute remained active"):
            service.terminate_worker(worker["worker_id"])

        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed is not None
        assert refreshed["state"] == "termination_failed"
        assert refreshed["last_error"] == "compute remained active"
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "worker.termination_failed"
        # Delayed runtime writers cannot reopen a workspace whose teardown needs attention.
        assert store.update_worker(worker["worker_id"], state="ready")["state"] == "termination_failed"
        assert store.update_worker_unless_gc_claimed(worker["worker_id"], state="paused") is None
        assert store.get_worker(worker["worker_id"])["state"] == "termination_failed"
        for rejected in (
            lambda: service.assign_run(worker["worker_id"], "Do not queue after close failed."),
            lambda: service.send_message(worker["worker_id"], "Do not queue this message."),
            lambda: service.resume_worker(worker["worker_id"]),
            lambda: service.schedule_run(worker["worker_id"], "Do not schedule this.", delay_seconds=60),
            lambda: service.pause_worker(worker["worker_id"]),
            lambda: service.interrupt_worker(worker["worker_id"]),
        ):
            with pytest.raises(ControlPlaneConflict, match="closed"):
                rejected()
    finally:
        service.shutdown()


@pytest.mark.parametrize("closed_state", ["terminating", "termination_failed", "terminated"])
@pytest.mark.parametrize("operation", ["pause", "interrupt"])
def test_closed_workspace_control_actions_do_not_call_runtime(tmp_path, closed_state, operation):
    class RecordingControlRuntime(StubRuntime):
        def __init__(self):
            self.pause_calls = 0
            self.interrupt_calls = 0

        def pause_worker(self, worker: dict) -> RuntimeInfo:
            self.pause_calls += 1
            return super().pause_worker(worker)

        def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
            self.interrupt_calls += 1
            return super().interrupt_worker(worker, run_id=run_id)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = RecordingControlRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        project = service.create_project("owner", "Closed controls", "Reject controls.", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Closed controls",
            role="operator",
            profile="codex-cli",
            backend="openclaw",
            start_synchronously=False,
        )
        store.update_worker_state(worker["worker_id"], closed_state)

        with pytest.raises(ControlPlaneConflict, match="closed"):
            if operation == "pause":
                service.pause_worker(worker["worker_id"])
            else:
                service.interrupt_worker(worker["worker_id"])

        assert runtime.pause_calls == 0
        assert runtime.interrupt_calls == 0
        assert store.get_worker(worker["worker_id"])["state"] == closed_state
    finally:
        service.shutdown()


@pytest.mark.parametrize("operation", ["pause", "interrupt"])
def test_control_action_losing_race_to_close_has_no_stale_side_effects(
    tmp_path,
    operation,
    background_consumers_disabled,
):
    _ = background_consumers_disabled
    class BlockingControlRuntime(StubRuntime):
        def __init__(self):
            self.entered = Event()
            self.release = Event()

        def pause_worker(self, worker: dict) -> RuntimeInfo:
            self.entered.set()
            assert self.release.wait(timeout=3), "pause test did not release runtime control"
            return super().pause_worker(worker)

        def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
            self.entered.set()
            assert self.release.wait(timeout=3), "interrupt test did not release runtime control"
            return super().interrupt_worker(worker, run_id=run_id)

    store = Store(str(tmp_path / "runtime.db"))
    runtime = BlockingControlRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project("owner", "Control race", "Close wins control race.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Control race",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    # This case exercises a live idle-compute control racing permanent close.
    # A newly created asynchronous workspace is durably paused until admitted,
    # so establish the ready/live precondition explicitly.
    if operation == "pause":
        worker = store.update_worker(
            worker["worker_id"],
            state="ready",
            compute_released_at=None,
        )
    active_run = None
    if operation == "interrupt":
        queued_run = service.assign_run(
            worker["worker_id"],
            "Control race run",
            start_processor=False,
        )
        active_run = truthfully_invoke_test_run(
            store,
            worker,
            queued_run,
            suffix="control-close-race",
        )
    errors: list[BaseException] = []

    def control() -> None:
        try:
            if operation == "pause":
                service.pause_worker(worker["worker_id"])
            else:
                service.interrupt_worker(worker["worker_id"])
        except BaseException as exc:  # pragma: no cover - assertion reports worker thread errors
            errors.append(exc)

    thread = Thread(target=control)
    thread.start()
    try:
        assert runtime.entered.wait(timeout=2), "control action never reached runtime"
        assert service.terminate_worker(worker["worker_id"])["state"] == "terminated"
    finally:
        runtime.release.set()
        thread.join(timeout=3)
        service.shutdown()

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ControlPlaneConflict)
    assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    event_types = [event["event_type"] for event in store.list_events(worker["worker_id"])]
    assert f"worker.{operation}d" not in event_types
    if active_run is not None:
        assert store.get_run(active_run["run_id"])["state"] == "cancelled"


@pytest.mark.parametrize("closed_state", ["terminating", "termination_failed", "terminated"])
def test_closed_workspace_rejects_terminal_websocket(tmp_path, closed_state):
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Closed terminal", "goal": "Reject terminal attach."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Closed terminal", "role": "operator"},
    ).json()
    client.app.state.store.update_worker_state(worker["worker_id"], closed_state)

    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(f"/ws/workers/{worker['worker_id']}/terminal"):
            pass
    assert disconnected.value.code == 4404


def test_open_terminal_websocket_is_revoked_when_workspace_closes(tmp_path):
    pid_path = tmp_path / "terminal.pid"

    class TerminalRuntime(StubRuntime):
        def terminal_target(self, worker: dict) -> TerminalTarget:
            return TerminalTarget(
                command=[
                    "/bin/sh",
                    "-c",
                    f"echo $$ > {pid_path}; echo terminal-ready; exec sleep 30",
                ],
                cwd=str(tmp_path),
            )

    client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=TerminalRuntime(),
        )
    )
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Close terminal", "goal": "Revoke live shell."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Close terminal", "role": "operator"},
    ).json()
    opened = Event()
    disconnected_codes: list[int] = []

    def use_terminal() -> None:
        try:
            with client.websocket_connect(f"/ws/workers/{worker['worker_id']}/terminal") as websocket:
                assert "terminal-ready" in websocket.receive_text()
                opened.set()
                websocket.receive_text()
        except WebSocketDisconnect as exc:
            disconnected_codes.append(exc.code)

    thread = Thread(target=use_terminal)
    thread.start()
    try:
        assert opened.wait(timeout=2), "terminal did not open"
        response = client.post(f"/v1/workers/{worker['worker_id']}/terminate")
        assert response.status_code == 202
        thread.join(timeout=3)
    finally:
        if thread.is_alive():
            thread.join(timeout=1)

    assert not thread.is_alive()
    assert disconnected_codes == [1008]
    pid = int(pid_path.read_text().strip())
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("terminal process survived workspace closure")


def test_terminate_rejects_runtime_that_still_reports_compute(tmp_path):
    class LingeringTerminateRuntime(StubRuntime):
        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            return RuntimeInfo(
                runtime="openclaw-stub",
                model="stub-model",
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=4242,
            )

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, LingeringTerminateRuntime())
    try:
        project = service.create_project("owner", "Lingering", "Reject false termination", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Lingering Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )

        with pytest.raises(RuntimeError, match="still active after termination"):
            service.terminate_worker(worker["worker_id"])

        assert store.get_worker(worker["worker_id"])["state"] == "termination_failed"
    finally:
        service.shutdown()


@pytest.mark.parametrize("_race_iteration", range(3))
def test_steer_interrupts_active_run_and_redirects_to_new_instruction(
    tmp_path,
    _race_iteration,
):
    db_path = tmp_path / "runtime.db"
    runtime = SteerableControllableRuntime()
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Steer Redirect", "goal": "Redirect an active run immediately."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={"owner_id": "demo-owner", "name": "Steer Worker", "role": "coder"},
        ).json()

        first_run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Do the original long-running task."},
        ).json()
        assert runtime.running.wait(timeout=2), "worker run never started"

        steer_resp = client.post(
            f"/v1/workers/{worker['worker_id']}/steer",
            json={"message": "Switch immediately to the new operator direction."},
        )
        assert steer_resp.status_code == 202
        steer_run = steer_resp.json()
        assert steer_run["state"] == "queued"
        assert runtime.interrupt_run_ids == [first_run["run_id"]]

        interrupted = wait_for_run(client, first_run["run_id"], timeout=3.0)
        assert interrupted["state"] == "interrupted"
        redirected = wait_for_run(client, steer_run["run_id"], timeout=3.0)
        assert redirected["state"] == "completed"
        assert redirected["output_text"] == "STEER_REDIRECT_OK"

        runtime.release.set()
        wait_until(
            lambda: not app.state.service._local_processor_owns(worker["worker_id"]),
            timeout=2.0,
        )
        assert app.state.store.get_run(first_run["run_id"])["state"] == "interrupted"
        assert app.state.store.get_run(steer_run["run_id"])["state"] == "completed"
        assert app.state.store.get_worker(worker["worker_id"])["last_run_id"] == steer_run["run_id"]
        assert runtime.post_exit_collect_run_ids == []

        events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
        event_types = [event["event_type"] for event in events]
        assert "worker.interrupted" in event_types
        assert "worker.steer" in event_types
        assert runtime.instructions[1].startswith("Operator steer instruction")
        assert "Do not stop at an acknowledgement" in runtime.instructions[1]


class HealRecoveryRuntime:
    def __init__(self) -> None:
        self.collect_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "heal-recovery/test"

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="heal-recovery",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"heal-recovery:{worker['worker_id']}",
            state_dir="/tmp/heal-recovery/state",
            workspace_dir="/tmp/heal-recovery/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "HEAL_COMPLETED_OK",
            "error_text": "",
        }


def test_heal_worker_restarts_processor_when_queued_runs_remain(
    tmp_path,
    background_consumers_disabled,
    request,
):
    _ = background_consumers_disabled
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = HealRecoveryRuntime()
    service = WorkersProjectsService(store, runtime)
    request.addfinalizer(service.shutdown)

    project = service.create_project("demo-owner", "Heal Queue", "Recover completion and continue queued runs.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Heal Queue Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )

    running = create_truthfully_invoked_test_run(
        store,
        worker,
        project["project_id"],
        "original run",
        suffix="heal-worker-active",
    )
    queued = store.create_run(worker["worker_id"], project["project_id"], "queued follow-up", state="queued")
    store.update_worker(worker["worker_id"], state="running")

    restart_requests: list[str] = []
    service._ensure_worker_processor = lambda worker_id: restart_requests.append(worker_id)  # type: ignore[method-assign]
    # Recovery may collect a completed transcript only when no local queue
    # processor still owns the run.

    healed = service.heal_worker(worker["worker_id"])

    assert healed is not None
    assert store.get_run(running["run_id"])["state"] == "completed"
    assert store.get_run(queued["run_id"])["state"] == "queued"
    assert restart_requests == [worker["worker_id"]]
    assert runtime.collect_run_ids == [running["run_id"]]


def test_heal_worker_repairs_starting_worker_without_active_run(
    tmp_path,
    background_consumers_disabled,
    request,
):
    _ = background_consumers_disabled
    class ReconcileReadyRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.reconciled: list[str] = []

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            self.reconciled.append(worker["worker_id"])
            return RuntimeInfo(
                runtime="openclaw",
                model="test-model",
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key="session",
                state_dir="/tmp/state",
                workspace_dir="/tmp/workspace",
                pid=1234,
            )

    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = ReconcileReadyRuntime()
    service = WorkersProjectsService(store, runtime)
    request.addfinalizer(service.shutdown)

    project = service.create_project("demo-owner", "Heal No Active Run", "Repair stale starting state.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Stale Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )
    store.update_worker(worker["worker_id"], state="starting", pid=None)

    healed = service.heal_worker(worker["worker_id"])

    assert healed is not None
    assert healed["state"] == "ready"
    assert healed["pid"] == 1234
    assert runtime.reconciled == [worker["worker_id"]]


class HealingRaceRuntime:
    requires_run_start_identity = False

    def __init__(self) -> None:
        self.initial_started = Event()
        self.release_initial = Event()
        self.queued_started = Event()
        self.release_queued = Event()
        self.collect_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "healing-race/test"

    def isolated_resource_usage(self) -> dict[str, object]:
        return StubRuntime().isolated_resource_usage()

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="healing-race",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=f"healing-race:{worker['worker_id']}",
            state_dir=f"/tmp/{worker['worker_id']}/state",
            workspace_dir=f"/tmp/{worker['worker_id']}/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "HEAL_RECOVERED_INITIAL",
            "error_text": "",
        }

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        with runtime_start_boundary(worker):
            notify_runtime_started(worker)
        if "initial" in instruction:
            self.initial_started.set()
            assert self.release_initial.wait(timeout=3)
            return "INITIAL_RETURNED_LATE"
        self.queued_started.set()
        assert self.release_queued.wait(timeout=3)
        return "QUEUED_COMPLETED_OK"


def test_heal_worker_replacement_processor_keeps_running_state_while_follow_up_executes(
    tmp_path,
    background_consumers_disabled,
    request,
):
    _ = background_consumers_disabled
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = HealingRaceRuntime()
    service = WorkersProjectsService(store, runtime)
    request.addfinalizer(service.shutdown)

    project = service.create_project("demo-owner", "Queue Race", "Ensure healed processors cannot overwrite a replacement run.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Queue Race Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )

    initial = service.assign_run(worker["worker_id"], "initial run that will be healed")
    assert runtime.initial_started.wait(timeout=2)

    queued = service.assign_run(worker["worker_id"], "queued follow-up that must keep running")
    # Explicitly retire the original processor generation before adopting its
    # completed native result. A normal worker read must never do this.
    service._invalidate_worker_processor(worker["worker_id"])
    healed = service.heal_worker(worker["worker_id"])
    assert healed is not None
    assert runtime.collect_run_ids == [initial["run_id"]]
    assert runtime.queued_started.wait(timeout=2)

    runtime.release_initial.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        refreshed_worker = store.get_worker(worker["worker_id"])
        active_run = store.get_active_run(worker["worker_id"])
        if refreshed_worker and refreshed_worker["state"] == "running" and active_run and active_run["run_id"] == queued["run_id"]:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Replacement processor did not keep the queued follow-up marked as running")

    runtime.release_queued.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        queued_run = store.get_run(queued["run_id"])
        refreshed_worker = store.get_worker(worker["worker_id"])
        if queued_run and queued_run["state"] == "completed" and refreshed_worker and refreshed_worker["state"] == "ready":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Queued follow-up did not complete cleanly")

    assert store.get_run(initial["run_id"])["state"] == "completed"
    assert store.get_run(queued["run_id"])["output_text"] == "QUEUED_COMPLETED_OK"
    assert store.get_worker(worker["worker_id"])["last_run_id"] == queued["run_id"]


class TerminatedButCompletedRuntime(StubRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.collect_run_ids: list[str | None] = []

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        raise WorkerTerminatedError("stale termination marker")

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "Recovered final answer",
            "error_text": "",
        }


class InterruptedButCompletedRuntime(TerminatedButCompletedRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        raise WorkerInterruptedError("stale interruption marker")


class RuntimeErrorWithPartialArtifactsRuntime(StubRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.collect_run_ids: list[str | None] = []

    def _worker_paths(self, worker_id: str) -> tuple[Path, Path]:
        state_dir = self.root / worker_id / "state"
        workspace_dir = state_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return state_dir, workspace_dir

    def _info(self, worker: dict) -> RuntimeInfo:
        state_dir, workspace_dir = self._worker_paths(worker["worker_id"])
        return RuntimeInfo(
            runtime="codex-cli",
            model=str(worker.get("model") or "stub/codex-cli"),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=str(worker.get("session_key") or f"codex:{worker['worker_id']}"),
            state_dir=str(state_dir),
            workspace_dir=str(workspace_dir),
            pid=4242,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        reports_dir = workspace_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "partial_result.csv").write_text("firm,fit\nExample Capital,high\n")
        raise RuntimeErrorBase(
            "codex-cli exited with code 1: write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true"
        )

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "failed",
            "output_text": "",
            "error_text": "codex-cli exited with code 1: response.failed event received",
            "failure_class": "provider_response_failed",
            "failure_retryable": 1,
            "failure_user_message": "The model provider ended the worker turn unexpectedly before the task finished.",
            "failure_recommended_recovery": (
                "Use workspace_continue to resume from the same workspace and ask the worker to continue "
                "from the current files and notes."
            ),
            "failure_diagnostic_summary": "response.failed event received",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 3,
            },
        }


class BlockingLiveStateRuntime(RuntimeErrorWithPartialArtifactsRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.started = Event()
        self.release = Event()

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        self.started.set()
        assert self.release.wait(timeout=3)
        return "Completed after live-state inspection"


class RuntimeErrorWithDeliverableArtifactRuntime(RuntimeErrorWithPartialArtifactsRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        artifacts_dir = workspace_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "finished_report.md").write_text("# Finished report\n\nGenerated before provider disconnect.\n")
        raise RuntimeErrorBase("codex-cli exited with code 1: response.failed event received")


class RuntimeErrorWithNestedIndexRuntime(RuntimeErrorWithPartialArtifactsRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        html_dir = workspace_dir / "site"
        html_dir.mkdir(parents=True, exist_ok=True)
        (html_dir / "index.html").write_text("<!doctype html><title>Generated report</title>")
        raise RuntimeErrorBase("codex-cli exited with code 1: response.failed event received")


class RuntimeErrorWithStaleDeliverableArtifactRuntime(RuntimeErrorWithPartialArtifactsRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        artifacts_dir = workspace_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stale = artifacts_dir / "stale_report.md"
        stale.write_text("# Stale report\n\nThis existed before the failed run.\n")
        old_time = time.time() - 3600
        os.utime(stale, (old_time, old_time))
        raise RuntimeErrorBase("codex-cli exited with code 1: response.failed event received")


class RuntimeIoFailureWithDeliverableArtifactRuntime(RuntimeErrorWithDeliverableArtifactRuntime):
    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "failed",
            "output_text": "",
            "error_text": "codex-cli exited with code 1: write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true",
            "failure_class": "runtime_io_failed",
            "failure_retryable": 1,
            "failure_user_message": "The worker command session closed before GlassHive captured the final turn cleanly.",
            "failure_recommended_recovery": "Use workspace_continue to resume from the same workspace.",
            "failure_diagnostic_summary": "write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true",
        }


class ProviderAuthNeedsInputRecoveryRuntime(RuntimeErrorWithPartialArtifactsRuntime):
    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "needs_input",
            "output_text": "",
            "error_text": "The connected model account is unavailable for this mission.",
            "failure_class": "provider_auth_projection_unavailable",
            "failure_retryable": 0,
            "failure_structured": 1,
            "failure_user_message": "The connected model account is unavailable for this mission.",
            "failure_recommended_recovery": "Connect or reauthorize the model account, then resume this work.",
            "failure_diagnostic_summary": "The clean-room provider broker returned needs-input truth.",
        }


class EvidenceFailureWithCompletedRecoveryRuntime(RuntimeErrorWithDeliverableArtifactRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        publish_in_process_test_start(worker)
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        artifacts_dir = workspace_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "invalid_completion.md").write_text("# Incomplete report\n\nNo required workbook was produced.\n")
        raise RuntimeErrorBase("GlassHive evidence check failed: completion compliance failed: missing xlsx")

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "FINAL REPORT: claimed success despite missing xlsx",
            "error_text": "",
        }


def test_running_worker_exposes_runtime_paths_before_task_finishes(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = BlockingLiveStateRuntime(tmp_path / "workers")
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("demo-owner", "Live state", "Expose workspace while running.", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="demo-owner",
            name="Codex Worker",
            role="research",
            profile="codex-cli",
            backend="openclaw",
        )

        run = service.assign_run(worker["worker_id"], "do live work")
        assert runtime.started.wait(timeout=2)

        live_worker = store.get_worker(worker["worker_id"])
        live_run = store.get_run(run["run_id"])

        assert live_worker is not None
        assert live_run is not None
        assert live_worker["state"] == "running"
        assert live_run["state"] == "running"
        assert live_worker["workspace_dir"].endswith(f"{worker['worker_id']}/state/workspace")
        assert live_worker["state_dir"].endswith(f"{worker['worker_id']}/state")
        assert live_worker["runtime"] == "codex-cli"
        assert live_worker["pid"] == 4242

        runtime.release.set()
        wait_until(lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed")
        assert store.get_run(run["run_id"])["output_text"] == "Completed after live-state inspection"
    finally:
        runtime.release.set()
        service.shutdown()


def test_runtime_error_recovers_codex_failure_metadata_and_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    db_path = tmp_path / "runtime.db"
    runtime = RuntimeErrorWithPartialArtifactsRuntime(tmp_path / "workers")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Recovered failure", "goal": "Keep partial artifacts visible."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Codex Worker", "role": "research", "profile": "codex-cli"},
    ).json()

    run = client.post(f"/v1/workers/{worker['worker_id']}/assign", json={"instruction": "research and build report"}).json()
    failed = wait_for_run(client, run["run_id"])

    assert failed["state"] == "failed"
    assert failed["failure_class"] == "provider_response_failed"
    assert failed["failure_retryable"] == 1
    assert failed["total_tokens"] == 38
    assert runtime.collect_run_ids == [run["run_id"]]
    refreshed_worker = client.get(f"/v1/workers/{worker['worker_id']}").json()
    assert refreshed_worker["runtime"] == "codex-cli"
    assert refreshed_worker["workspace_dir"]
    artifacts = client.get(f"/v1/workers/{worker['worker_id']}/artifacts").json()
    assert any(item["path"] == "reports/partial_result.csv" for item in artifacts["items"])


def test_provider_failure_after_fresh_artifact_is_delivered_as_a_partial_artifact(tmp_path, monkeypatch):
    """A fresh file is preserved and delivered with the typed failure; it never completes the run."""
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    db_path = tmp_path / "runtime.db"
    runtime = RuntimeErrorWithDeliverableArtifactRuntime(tmp_path / "workers")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Recovered deliverable", "goal": "Preserve real artifacts."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Codex Worker", "role": "research", "profile": "codex-cli"},
    ).json()

    run = client.post(f"/v1/workers/{worker['worker_id']}/assign", json={"instruction": "research and build report"}).json()
    failed = wait_for_run(client, run["run_id"])

    assert failed["state"] == "failed"
    assert failed["failure_class"] == "provider_response_failed"
    wait_until(
        lambda: any(
            item["event_type"] == "run.partial_artifact_preserved"
            for item in client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
        )
    )
    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    assert any(item["event_type"] == "run.failed" for item in events)
    assert any(
        item["event_type"] == "run.partial_artifact_preserved"
        and item["message"] == "artifacts/finished_report.md"
        for item in events
    )
    assert not any(item["event_type"] == "run.completed" for item in events)
    artifacts = client.get(f"/v1/workers/{worker['worker_id']}/artifacts").json()
    assert any(item["path"] == "artifacts/finished_report.md" for item in artifacts["items"])


def test_evidence_failure_is_not_recovered_as_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")
    payloads: list[dict] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)
    db_path = tmp_path / "runtime.db"
    runtime = EvidenceFailureWithCompletedRecoveryRuntime(tmp_path / "workers")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Evidence failure", "goal": "Fail missing workbook."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Codex Worker",
            "role": "research",
            "profile": "codex-cli",
            "bootstrap_bundle": {
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                    "surface": "web",
                }
            },
        },
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Create a workbook and final report."},
    ).json()
    failed = wait_for_run(client, run["run_id"])

    assert failed["state"] == "failed"
    assert failed["failure_class"] == "glasshive_evidence_check_failed"
    assert "missing xlsx" in failed["error_text"]
    assert runtime.collect_run_ids == []
    wait_until(lambda: any(payload.get("event") == "run.failed" for payload in payloads), timeout=3.0)
    failed_payload = next(payload for payload in payloads if payload.get("event") == "run.failed")
    assert failed_payload["failure_code"] == "glasshive_evidence_check_failed"
    assert failed_payload["failure_class"] == "glasshive_evidence_check_failed"
    assert failed_payload["deliverable"]["workspace_path"] == "artifacts/invalid_completion.md"
    assert "Preview: [Open GlassHive file](" in failed_payload["message"]
    assert "Download file" in failed_payload["message"]
    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    assert any(item["event_type"] == "run.failed" for item in events)
    assert not any(item["event_type"] == "run.completed" for item in events)


def test_provider_failure_after_fresh_nested_index_stays_failed_with_the_partial_page(tmp_path, monkeypatch):
    """Any fresh HTML page used to complete the run; it is a partial artifact until evidence says otherwise."""
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    db_path = tmp_path / "runtime.db"
    runtime = RuntimeErrorWithNestedIndexRuntime(tmp_path / "workers")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Nested HTML", "goal": "Preserve generated HTML."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Codex Worker", "role": "research", "profile": "codex-cli"},
    ).json()

    run = client.post(f"/v1/workers/{worker['worker_id']}/assign", json={"instruction": "build the HTML report"}).json()
    failed = wait_for_run(client, run["run_id"])

    assert failed["state"] == "failed"
    assert failed["failure_class"] == "provider_response_failed"
    wait_until(
        lambda: any(
            item["event_type"] == "run.partial_artifact_preserved"
            for item in client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
        )
    )
    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    assert any(
        item["event_type"] == "run.partial_artifact_preserved" and item["message"] == "site/index.html"
        for item in events
    )
    assert not any(item["event_type"] == "run.completed" for item in events)


def test_provider_failure_does_not_complete_for_stale_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    db_path = tmp_path / "runtime.db"
    runtime = RuntimeErrorWithStaleDeliverableArtifactRuntime(tmp_path / "workers")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Stale artifact", "goal": "Do not promote stale files."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Codex Worker", "role": "research", "profile": "codex-cli"},
    ).json()

    run = client.post(f"/v1/workers/{worker['worker_id']}/assign", json={"instruction": "build a fresh report"}).json()
    failed = wait_for_run(client, run["run_id"])

    assert failed["state"] == "failed"
    assert failed["failure_class"] == "provider_response_failed"


def test_runtime_io_failure_after_fresh_artifact_stays_failed_with_the_partial_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    db_path = tmp_path / "runtime.db"
    runtime = RuntimeIoFailureWithDeliverableArtifactRuntime(tmp_path / "workers")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Runtime I/O deliverable", "goal": "Preserve user files."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Codex Worker", "role": "research", "profile": "codex-cli"},
    ).json()

    run = client.post(f"/v1/workers/{worker['worker_id']}/assign", json={"instruction": "create a report"}).json()
    failed = wait_for_run(client, run["run_id"])

    assert failed["state"] == "failed"
    assert failed["failure_class"] == "runtime_io_failed"
    wait_until(
        lambda: any(
            item["event_type"] == "run.partial_artifact_preserved"
            for item in client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
        )
    )
    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    assert not any(item["event_type"] == "run.completed" for item in events)


def test_prepared_codex_worker_uses_codex_runtime_label(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, RuntimeErrorWithPartialArtifactsRuntime(tmp_path / "workers"))
    try:
        project = service.create_project("owner", "Prepared Codex", "Check initial runtime label.", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Prepared Codex Worker",
            role="research",
            profile="codex-cli",
            backend="stub",
            start_synchronously=False,
        )

        assert worker["runtime"] == "codex-cli"
    finally:
        service.shutdown()


def test_worker_terminated_error_recovers_completed_artifacts(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = TerminatedButCompletedRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("demo-owner", "Recovered Run", "Recover stdout.", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="demo-owner",
            name="Recovered Worker",
            role="coder",
            profile="codex-cli",
            backend="openclaw",
        )
        run = service.assign_run(worker["worker_id"], "finish despite stale marker")

        deadline = time.time() + 5
        while time.time() < deadline:
            refreshed = store.get_run(run["run_id"])
            refreshed_worker = store.get_worker(worker["worker_id"])
            if (
                refreshed
                and refreshed["state"] == "completed"
                and refreshed_worker
                and refreshed_worker["state"] == "ready"
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "Run did not recover completed artifacts and settle its worker"
            )

        assert store.get_run(run["run_id"])["output_text"] == "Recovered final answer"
        assert store.get_worker(worker["worker_id"])["state"] == "ready"
        assert runtime.collect_run_ids == [run["run_id"]]
    finally:
        service.shutdown()


def test_worker_interrupted_error_preserves_operator_interrupt(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = InterruptedButCompletedRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("demo-owner", "Recovered Run", "Recover stdout.", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="demo-owner",
            name="Recovered Worker",
            role="coder",
            profile="codex-cli",
            backend="openclaw",
        )
        run = service.assign_run(worker["worker_id"], "finish despite stale marker")

        deadline = time.time() + 5
        while time.time() < deadline:
            refreshed = store.get_run(run["run_id"])
            refreshed_worker = store.get_worker(worker["worker_id"])
            if (
                refreshed
                and refreshed["state"] == "interrupted"
                and refreshed_worker
                and refreshed_worker["state"] == "ready"
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Run did not preserve the operator interrupt")

        assert store.get_run(run["run_id"])["output_text"] == ""
        assert store.get_worker(worker["worker_id"])["state"] == "ready"
        assert runtime.collect_run_ids == []
    finally:
        service.shutdown()


class DesktopStubRuntime:
    requires_run_start_identity = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.last_desktop_action: dict[str, object] | None = None

    def resolve_model(self, profile: str) -> str:
        return "desktop-stub/test"

    def isolated_resource_usage(self) -> dict[str, object]:
        return StubRuntime().isolated_resource_usage()

    def _worker_paths(self, worker_id: str) -> tuple[Path, Path]:
        state_dir = self.root / worker_id / "state"
        workspace_dir = state_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return state_dir, workspace_dir

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        state_dir, workspace_dir = self._worker_paths(worker["worker_id"])
        return RuntimeInfo(
            runtime="desktop-stub",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"desktop:{worker['worker_id']}",
            state_dir=str(state_dir),
            workspace_dir=str(workspace_dir),
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        publish_in_process_test_start(worker)
        return f"DESKTOP_OK: {instruction}"

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def describe_worker(self, worker: dict) -> dict[str, object]:
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        return {
            "mode": "workstation-desktop",
            "runtime": "desktop-stub",
            "workspace_dir": str(workspace_dir),
            "state_dir": str(workspace_dir.parent),
            "container_name": f"wpr-{worker['worker_id']}",
            "view_url": "http://127.0.0.1:57906/?autoconnect=1",
        }

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        self.last_desktop_action = {
            "worker_id": worker["worker_id"],
            "action": action,
            "url": url,
            "run_id": run_id,
        }
        return {
            "action": action,
            "status": "launched",
            "mode": "workstation-desktop",
            "url": "http://127.0.0.1:57906/?autoconnect=1",
            "view_url": "http://127.0.0.1:57906/?autoconnect=1",
            "notes": f"{action} launched",
        }


def test_desktop_action_refreshes_activity_before_idle_reaper(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "60")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = DesktopStubRuntime(tmp_path / "desktop")
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Desktop", "Refresh activity", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Desktop Worker",
            role="operator",
            profile="codex-cli",
            backend="openclaw",
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat(), worker["worker_id"]),
            )

        service.desktop_action(worker["worker_id"], "terminal")

        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed is not None
        assert refreshed["state"] == "ready"
        assert service.reap_idle_workers_once() == []
        assert runtime.last_desktop_action == {
            "worker_id": worker["worker_id"],
            "action": "terminal",
            "url": None,
            "run_id": None,
        }
    finally:
        service.shutdown()


def test_desktop_action_uses_the_active_run_id_when_the_browser_omits_it(
    tmp_path,
    background_consumers_disabled,
):
    _ = background_consumers_disabled
    store = Store(str(tmp_path / "runtime.db"))
    runtime = DesktopStubRuntime(tmp_path / "desktop")
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        project = service.create_project("owner", "Desktop", "Promote result", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Desktop Worker",
            role="operator",
            profile="codex-cli",
            backend="openclaw",
            start_synchronously=False,
        )
        queued_run = service.assign_run(
            worker["worker_id"],
            "Create a page",
            start_processor=False,
        )
        run = truthfully_invoke_test_run(
            store,
            worker,
            queued_run,
            suffix="desktop-active-run",
        )

        service.desktop_action(worker["worker_id"], "browser", url="about:blank")

        assert runtime.last_desktop_action == {
            "worker_id": worker["worker_id"],
            "action": "browser",
            "url": "about:blank",
            "run_id": run["run_id"],
        }
    finally:
        service.shutdown()


class DeliverableDesktopRuntime(DesktopStubRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        publish_in_process_test_start(worker)
        workspace_dir = Path(str(worker["workspace_dir"]))
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "index.html").write_text("<!doctype html><h1>Hello</h1>", encoding="utf-8")
        return f"FINAL REPORT:\nCreated the page for: {instruction}"


class UrlOnlyDesktopRuntime(DesktopStubRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        publish_in_process_test_start(worker)
        return "\n".join(
            [
                "FINAL REPORT:",
                "Preview is ready.",
                "Local preview: http://localhost:5173/private-preview",
                "External preview: https://example.com/public-preview",
            ]
        )


class ExternalUrlOnlyDesktopRuntime(DesktopStubRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        publish_in_process_test_start(worker)
        return "The official service endpoint is https://mcp.example.test/v1/mcp."


def test_completed_external_url_mention_is_not_delivered_or_auto_opened(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = ExternalUrlOnlyDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Connector check", "goal": "Read only."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Connector worker",
                "role": "operator",
                "profile": "claude-code",
                "execution_mode": "docker",
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Inspect the official connector."},
        ).json()

        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"
        assert runtime.last_desktop_action is None
        service = app.state.service
        refreshed_worker = service.require_worker(worker["worker_id"])
        assert service._completion_deliverable(
            refreshed_worker, completed, completed["output_text"]
        ) is None
        assert not any(
            event["event_type"] == "deliverable.opened"
            for event in service.store.list_events(worker["worker_id"])
        )


def test_completed_docker_run_opens_workspace_html_in_sandbox_browser_once(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Deliverable Promotion", "goal": "Open generated pages."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Page Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "docker",
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()

        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"
        wait_until(lambda: runtime.last_desktop_action is not None)

        assert runtime.last_desktop_action == {
            "worker_id": worker["worker_id"],
            "action": "browser",
            "url": "file:///workspace/project/index.html",
            "run_id": run["run_id"],
        }

        service = app.state.service
        refreshed_worker = service.require_worker(worker["worker_id"])
        deliverable = service._completion_deliverable(refreshed_worker, completed, completed["output_text"])
        service._promote_completed_deliverable(refreshed_worker, completed, deliverable)
        opened_events = [
            event
            for event in app.state.store.list_events(worker["worker_id"])
            if event["event_type"] == "deliverable.opened"
        ]
        assert len(opened_events) == 1


def test_completed_host_run_does_not_auto_open_real_desktop_browser(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Host Deliverable", "goal": "Do not auto-open host pages."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Host Page Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "host",
                "bootstrap_bundle": {
                    "callbacks": {
                        "events_webhook_url": "http://callback.local/glasshive",
                        "hmac_secret": "public-safe-callback-secret",
                        "user_id": "user-public-safe",
                        "conversation_id": "conv-public-safe",
                        "parent_message_id": "parent-public-safe",
                        "message_id": "assistant-public-safe",
                        "surface": "web",
                    }
                },
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()

        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"
        assert runtime.last_desktop_action is None

        def callback_payload() -> dict | None:
            with app.state.store._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM callback_outbox WHERE run_id = ? AND event_type = 'run.completed'",
                    (run["run_id"],),
                ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        wait_until(lambda: callback_payload() is not None)
        payload = callback_payload()
        assert payload is not None
        assert payload["deliverable"]["kind"] == "webpage"
        assert payload["deliverable"]["source"] == "workspace_html"
        assert payload["deliverable"]["browser_url_available"] is False
        assert "browser_url" not in payload["deliverable"]
        assert "file://" not in json.dumps(payload)


def test_completed_host_run_url_output_does_not_auto_open_or_leak_urls(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = UrlOnlyDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Host URL Deliverable", "goal": "Do not auto-open host URLs."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Host URL Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "host",
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Print a local preview URL."},
        ).json()

        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"
        assert runtime.last_desktop_action is None

        service = app.state.service
        refreshed_worker = service.require_worker(worker["worker_id"])
        deliverable = service._completion_deliverable(refreshed_worker, completed, completed["output_text"])

        assert deliverable is not None
        assert deliverable["source"] == "run_url"
        assert deliverable["browser_url_available"] is False
        assert "browser_url" not in deliverable
        serialized = json.dumps(deliverable)
        assert "localhost" not in serialized
        assert "127.0.0.1" not in serialized
        assert "example.com" not in serialized


@pytest.mark.parametrize("surface", ["telegram", "voice"])
def test_completed_non_web_callback_omits_operator_url_but_keeps_deliverable(tmp_path, monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Non-web Callback", "goal": "Preserve safe callback metadata."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Non-web Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "bootstrap_bundle": {
                    "callbacks": {
                        "events_webhook_url": "http://callback.local/glasshive",
                        "hmac_secret": "public-safe-callback-secret",
                        "user_id": "user-public-safe",
                        "conversation_id": "conv-public-safe",
                        "parent_message_id": "parent-public-safe",
                        "message_id": "assistant-public-safe",
                        "surface": surface,
                    }
                },
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()
        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"

        def callback_payload() -> dict | None:
            with app.state.store._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM callback_outbox WHERE run_id = ? AND event_type = 'run.completed'",
                    (run["run_id"],),
                ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        wait_until(lambda: callback_payload() is not None, timeout=5.0)
        payload = callback_payload()
        assert payload is not None
        assert payload["surface"] == surface
        assert "operator_url" not in payload
        assert "watch_url" not in payload
        assert payload["deliverable"]["kind"] == "webpage"
        assert payload["deliverable"]["browser_url"] == "file:///workspace/project/index.html"


def test_completed_web_callback_includes_operator_url_and_deliverable(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Web Callback", "goal": "Surface operator metadata."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Web Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "bootstrap_bundle": {
                    "callbacks": {
                        "events_webhook_url": "http://callback.local/glasshive",
                        "hmac_secret": "public-safe-callback-secret",
                        "user_id": "user-public-safe",
                        "conversation_id": "conv-public-safe",
                        "parent_message_id": "parent-public-safe",
                        "message_id": "assistant-public-safe",
                        "surface": "web",
                    }
                },
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()
        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"

        def callback_payload() -> dict | None:
            with app.state.store._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM callback_outbox WHERE run_id = ? AND event_type = 'run.completed'",
                    (run["run_id"],),
                ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        wait_until(lambda: callback_payload() is not None, timeout=5.0)
        payload = callback_payload()
        assert payload is not None
        expected_url = f"http://127.0.0.1:8780/watch/{worker['worker_id']}?surface=desktop&project_id={project['project_id']}"
        operator_record = assert_link_ref_url(payload["operator_url"], prefix="http://127.0.0.1:8780/r/", kind="worker_view")
        watch_record = assert_link_ref_url(payload["watch_url"], prefix="http://127.0.0.1:8780/r/", kind="worker_view")
        assert operator_record["target_url"].startswith(expected_url)
        assert watch_record["target_url"].startswith(expected_url)
        assert "gh_token=" in operator_record["target_url"]
        assert "gh_token=" not in payload["operator_url"]
        assert "gh_token=" not in payload["watch_url"]
        assert payload["deliverable"]["kind"] == "webpage"
        assert payload["deliverable"]["browser_url"] == "file:///workspace/project/index.html"
        assert "/v1/signed-links/" not in payload["message"]
        assert "gh_token=" not in payload["message"]
        assert "File: [Download file](http://127.0.0.1:8780/v1/link-refs/" in payload["message"]
        assert "Preview: [Open GlassHive file](http://127.0.0.1:8780/v1/link-refs/" in payload["message"]
        assert "View / Steer: [Open GlassHive workspace]" in payload["message"]
        assert payload["message"].index("File: [Download file]") < payload["message"].index("Preview:")
        assert payload["message"].index("Preview:") < payload["message"].index("View / Steer:")


def test_artifact_open_page_previews_text_without_forcing_download(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "File Preview", "goal": "Open artifacts without surprise downloads."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={"owner_id": "demo-owner", "name": "Preview Worker", "role": "writer", "profile": "codex-cli"},
        ).json()
        workspace = Path(worker["workspace_dir"])
        artifact = workspace / "answer.md"
        artifact.write_text("# Result\n\nGlassHive preview works.", encoding="utf-8")

        listed = client.get(f"/v1/workers/{worker['worker_id']}/artifacts").json()
        assert listed["items"][0]["open_url"].endswith("/artifacts/open?path=answer.md")
        assert listed["items"][0]["download_url"].endswith("/artifacts/download?path=answer.md")

        opened = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=answer.md")
        assert opened.status_code == 200
        assert "text/html" in opened.headers["content-type"]
        assert "content-disposition" not in opened.headers
        assert "no-store" in opened.headers["cache-control"]
        assert opened.headers["pragma"] == "no-cache"
        assert opened.headers["x-content-type-options"] == "nosniff"
        assert opened.headers["x-frame-options"] == "SAMEORIGIN"
        assert "default-src 'none'" in opened.headers["content-security-policy"]
        assert "script-src 'none'" in opened.headers["content-security-policy"]
        assert "connect-src 'none'" in opened.headers["content-security-policy"]
        assert "object-src 'none'" in opened.headers["content-security-policy"]
        assert "form-action 'none'" in opened.headers["content-security-policy"]
        assert "frame-src 'self'" in opened.headers["content-security-policy"]
        assert "frame-ancestors 'self'" in opened.headers["content-security-policy"]
        assert "GlassHive preview works." in opened.text
        assert "Download file" in opened.text
        assert "/v1/signed-links/" not in opened.text
        assert 'href="/v1/link-refs/' in opened.text
        assert " download" in anchor_for_link_text(opened.text, "Download file")
        assert 'target="_top"' in anchor_for_link_text(opened.text, "View workspace")
        assert "gh_token=" not in opened.text
        opened_download_href = href_for_link_text(opened.text, "Download file")
        assert_link_ref_url(opened_download_href, prefix="/v1/link-refs/", kind="artifact_download")
        workspace_href = href_for_link_text(opened.text, "View workspace")
        workspace_record = assert_link_ref_url(workspace_href, prefix="/r/", kind="worker_view")
        assert workspace_record["target_url"].startswith(f"/watch/{worker['worker_id']}?")
        assert "gh_token=" in workspace_record["target_url"]
        workspace_view = client.get(workspace_href, follow_redirects=False)
        assert workspace_view.status_code == 307
        assert workspace_view.headers["location"].startswith(f"/watch/{worker['worker_id']}?")
        assert "gh_token=" not in workspace_view.headers["location"]
        workspace_cookie = next(
            (cookie for cookie in workspace_view.headers.get_list("set-cookie") if cookie.startswith("glasshive_gh_token_")),
            "",
        )
        assert workspace_cookie
        assert "How to take over" not in workspace_view.text
        assert worker["worker_id"] not in workspace_view.text
        assert project["project_id"] not in workspace_view.text
        assert "/ui/workers/" not in workspace_view.text
        assert "/watch/" not in workspace_view.text
        opened_download = client.get(opened_download_href)
        assert opened_download.status_code == 200
        assert "attachment" in opened_download.headers["content-disposition"]
        assert "no-store" in opened_download.headers["cache-control"]
        assert opened_download.headers["x-content-type-options"] == "nosniff"

        downloaded = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/download?path=answer.md")
        assert downloaded.status_code == 200
        assert "attachment" in downloaded.headers["content-disposition"]
        assert "no-store" in downloaded.headers["cache-control"]
        assert downloaded.headers["x-content-type-options"] == "nosniff"

        binary_artifact = workspace / "report.pdf"
        binary_artifact.write_bytes(b"%PDF-1.4\n% synthetic public-safe fixture\n")
        opened_binary = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=report.pdf")
        assert opened_binary.status_code == 200
        assert "text/html" in opened_binary.headers["content-type"]
        assert "content-disposition" not in opened_binary.headers
        assert "File is ready" in opened_binary.text
        assert "Download file" in opened_binary.text

        image_artifact = workspace / "pixel.png"
        image_artifact.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axm+2sAAAAASUVORK5CYII="
            )
        )
        opened_image = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=pixel.png")
        assert opened_image.status_code == 200
        assert "content-disposition" not in opened_image.headers
        assert 'class="image-preview"' in opened_image.text
        assert "data:image/png;base64," in opened_image.text

        svg_artifact = workspace / "unsafe.svg"
        svg_artifact.write_text('<svg><script>alert("x")</script></svg>', encoding="utf-8")
        opened_svg = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=unsafe.svg")
        assert opened_svg.status_code == 200
        assert "File is ready" in opened_svg.text
        assert 'class="image-preview"' not in opened_svg.text
        assert "<script>" not in opened_svg.text

        html_artifact = workspace / "unsafe.html"
        html_artifact.write_text(
            "<!doctype html><h1>Hello world</h1><script>parent.document.body.textContent='unsafe'</script>",
            encoding="utf-8",
        )
        opened_html = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=unsafe.html")
        assert opened_html.status_code == 200
        assert '<iframe class="html-preview" sandbox credentialless referrerpolicy="no-referrer" ' in opened_html.text
        assert "allow-scripts" not in opened_html.text
        assert "allow-same-origin" not in opened_html.text
        assert "&lt;h1&gt;Hello world&lt;/h1&gt;" in opened_html.text
        assert "<script>parent.document" not in opened_html.text

        source_artifact = workspace / "source.txt"
        source_artifact.write_text("<h1>Source only</h1>", encoding="utf-8")
        opened_source = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=source.txt")
        assert opened_source.status_code == 200
        assert '<iframe class="html-preview"' not in opened_source.text
        assert '<pre class="artifact-preview">&lt;h1&gt;Source only&lt;/h1&gt;</pre>' in opened_source.text

        monkeypatch.setenv("GLASSHIVE_ARTIFACT_PREVIEW_MAX_BYTES", "4096")
        large_html = workspace / "large.html"
        large_html.write_text("<h1>Large page</h1>" + ("x" * 5000), encoding="utf-8")
        opened_large_html = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=large.html")
        assert opened_large_html.status_code == 200
        assert '<iframe class="html-preview"' not in opened_large_html.text
        assert "This page is too large for a safe preview" in opened_large_html.text

        open_token = sign_link_token(
            kind="artifact_open",
            worker_id=worker["worker_id"],
            tenant_id=worker["tenant_id"],
            owner_id=worker["owner_id"],
            path="answer.md",
        )
        signed_open = client.get(f"/v1/signed-links/{open_token}")
        assert signed_open.status_code == 200
        assert "text/html" in signed_open.headers["content-type"]
        assert "content-disposition" not in signed_open.headers
        assert "no-store" in signed_open.headers["cache-control"]
        assert "GlassHive preview works." in signed_open.text
        assert "/v1/signed-links/" not in signed_open.text
        assert 'href="/v1/link-refs/' in signed_open.text
        signed_open_download_href = href_for_link_text(signed_open.text, "Download file")
        assert_link_ref_url(signed_open_download_href, prefix="/v1/link-refs/", kind="artifact_download")
        signed_open_download = client.get(signed_open_download_href)
        assert signed_open_download.status_code == 200
        assert "attachment" in signed_open_download.headers["content-disposition"]
        assert signed_open_download.text == "# Result\n\nGlassHive preview works."

        download_token = sign_link_token(
            kind="artifact_download",
            worker_id=worker["worker_id"],
            tenant_id=worker["tenant_id"],
            owner_id=worker["owner_id"],
            path="answer.md",
        )
        signed_download = client.get(f"/v1/signed-links/{download_token}")
        assert signed_download.status_code == 200
        assert "attachment" in signed_download.headers["content-disposition"]
        assert "no-store" in signed_download.headers["cache-control"]
        assert signed_download.headers["x-content-type-options"] == "nosniff"

        encoded_payload, signature = open_token.split(".", 1)
        decoded = base64.urlsafe_b64decode(f"{encoded_payload}{'=' * (-len(encoded_payload) % 4)}")
        payload = json.loads(decoded.decode("utf-8"))
        payload["kind"] = "artifact_download"
        tampered_payload = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        assert client.get(f"/v1/signed-links/{tampered_payload}.{signature}").status_code == 401


def test_artifact_listing_reports_truncation_even_when_directories_consume_the_cap(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Artifact Cap", "goal": "List safely."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={"owner_id": "demo-owner", "name": "Cap Worker", "role": "writer", "profile": "codex-cli"},
        ).json()
        reports = Path(worker["workspace_dir"]) / "reports"
        reports.mkdir()
        for index in range(501):
            (reports / f"item-{index:03d}.txt").write_text("synthetic", encoding="utf-8")

        listed = client.get(f"/v1/workers/{worker['worker_id']}/artifacts")

        assert listed.status_code == 200
        payload = listed.json()
        assert payload["truncated"] is True
        assert len(payload["items"]) == 499


def test_enterprise_signed_artifact_open_page_actions_remain_signed(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")

    headers = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
    }
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))

    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Enterprise Artifact Links", "goal": "Verify signed preview click-through."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={"owner_id": "ignored", "name": "Artifact Worker", "role": "writer", "profile": "codex-cli"},
    ).json()
    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    artifact = workspace / "enterprise-result.txt"
    artifact.write_text("enterprise preview roundtrip", encoding="utf-8")

    assert client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open", params={"path": "enterprise-result.txt"}).status_code == 401
    assert (
        client.get(f"/v1/workers/{worker['worker_id']}/artifacts/download", params={"path": "enterprise-result.txt"}).status_code
        == 401
    )

    open_token = sign_link_token(
        kind="artifact_open",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
        path="enterprise-result.txt",
    )
    opened = client.get(f"/v1/signed-links/{open_token}")
    assert opened.status_code == 200
    assert "enterprise preview roundtrip" in opened.text
    assert "no-store" in opened.headers["cache-control"]

    download_href = href_for_link_text(opened.text, "Download file")
    workspace_href = href_for_link_text(opened.text, "View workspace")
    assert_link_ref_url(download_href, prefix="/v1/link-refs/", kind="artifact_download")
    workspace_record = assert_link_ref_url(workspace_href, prefix="/r/", kind="worker_view")
    assert workspace_record["target_url"].startswith(f"/watch/{worker['worker_id']}?")
    assert "gh_token=" in workspace_record["target_url"]
    assert "gh_token=" not in workspace_href
    assert 'target="_top"' in anchor_for_link_text(opened.text, "View workspace")
    workspace_view = client.get(workspace_href, headers=headers, follow_redirects=False)
    assert workspace_view.status_code == 307
    assert workspace_view.headers["location"].startswith(f"/watch/{worker['worker_id']}?")
    assert "gh_token=" not in workspace_view.headers["location"]
    assert any(cookie.startswith("glasshive_gh_token_") for cookie in workspace_view.headers.get_list("set-cookie"))
    assert "How to take over" not in workspace_view.text
    assert worker["worker_id"] not in workspace_view.text
    assert project["project_id"] not in workspace_view.text

    downloaded = client.get(download_href, headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.text == "enterprise preview roundtrip"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert "no-store" in downloaded.headers["cache-control"]
    assert downloaded.headers["x-content-type-options"] == "nosniff"


def test_desktop_action_and_artifact_preview_surface_in_project_ui(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DesktopStubRuntime(tmp_path / "desktop")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Desktop UX", "goal": "Expose workstation controls and artifact previews."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Desktop Worker", "role": "operator", "profile": "codex-cli"},
    ).json()

    workspace_dir = Path(worker["workspace_dir"])
    png_path = workspace_dir / "latest-proof.png"
    png_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360606060000000040001f61738550000000049454e44ae426082"
        )
    )

    action = client.post(
        f"/v1/workers/{worker['worker_id']}/desktop-action",
        json={"action": "codex", "run_id": "run_demo123"},
    )
    assert action.status_code == 202
    action_payload = action.json()
    assert action_payload["mode"] == "workstation-desktop"
    assert action_payload["status"] == "launched"
    assert action_payload["url"].startswith("http://127.0.0.1:57906/")
    assert runtime.last_desktop_action == {
        "worker_id": worker["worker_id"],
        "action": "codex",
        "url": None,
        "run_id": "run_demo123",
    }

    artifact = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/latest-image")
    assert artifact.status_code == 200
    assert artifact.headers["content-type"] == "image/png"

    project_ui = client.get(f"/ui/projects/{project['project_id']}?worker_id={worker['worker_id']}")
    assert project_ui.status_code == 200
    assert "Workstation Tools" in project_ui.text
    assert "Open Codex" in project_ui.text
    assert "Latest Visual Artifact" in project_ui.text
    assert f"/v1/workers/{worker['worker_id']}/artifacts/latest-image" in project_ui.text


def test_artifact_endpoints_never_follow_workspace_symlinks(tmp_path):
    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Artifact boundary", "goal": "Reject symlink escapes."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Boundary Worker", "role": "operator", "profile": "codex-cli"},
    ).json()
    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"synthetic-host-secret")
    (workspace / "leak.png").symlink_to(outside)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "nested.png").write_bytes(b"nested-synthetic-host-secret")
    (workspace / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

    latest = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/latest-image")
    direct = client.get(
        f"/v1/workers/{worker['worker_id']}/artifacts/download",
        params={"path": "leak.png"},
    )
    nested = client.get(
        f"/v1/workers/{worker['worker_id']}/artifacts/download",
        params={"path": "linked-dir/nested.png"},
    )

    assert latest.status_code == 404
    assert direct.status_code == 400
    assert nested.status_code in {400, 404}
    assert b"synthetic-host-secret" not in direct.content
    assert b"nested-synthetic-host-secret" not in nested.content


def test_launch_failed_endpoint_marks_worker_failed(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Launch Failure", "goal": "Record a failed launch clearly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Launch Worker", "role": "operator", "profile": "codex-cli"},
    ).json()

    response = client.post(
        f"/v1/workers/{worker['worker_id']}/launch-failed",
        json={"reason": "assign failed during launch"},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == "failed"
    assert payload["last_error"] == "assign failed during launch"

    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    assert any(event["event_type"] == "worker.launch_failed" for event in events)


def test_worker_metadata_favorite_round_trips(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pinned Workspace", "goal": "Pin a reusable workspace."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Marketing Sandbox",
            "role": "operator",
            "profile": "codex-cli",
            "workspace_kind": "ephemeral",
        },
    ).json()

    updated = client.patch(
        f"/v1/workers/{worker['worker_id']}",
        json={"favorite": True, "name": "Marketing Sandbox"},
    )

    assert updated.status_code == 200
    assert updated.json()["favorite"] is True
    assert updated.json()["name"] == "Marketing Sandbox"
    assert updated.json()["workspace_kind"] == "named"
    fetched = client.get(f"/v1/workers/{worker['worker_id']}").json()
    assert fetched["favorite"] is True
    assert fetched["workspace_kind"] == "named"


def test_async_worker_creation_parks_without_starting_compute(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Prepared Workspace", "goal": "Do not start compute until queued."},
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Prepared",
            "role": "operator",
            "profile": "codex-cli",
            "start_synchronously": False,
        },
    )

    assert worker.status_code == 201
    assert worker.json()["state"] == "paused"
    events = client.get(f"/v1/workers/{worker.json()['worker_id']}/events").json()["items"]
    assert any(event["event_type"] == "worker.prepared" for event in events)


def test_due_schedule_reconciles_fast_completed_run_before_queued_id_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Fast Schedule", "Avoid queued state race.", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Fast Worker",
            role="operator",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
        )
        schedule = store.create_scheduled_run(
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            tenant_id="local",
            owner_id="owner",
            instruction="finish immediately",
            run_at=datetime.now(timezone.utc).isoformat(),
            schedule_text="now",
        )

        def complete_immediately_after_schedule_is_linked(worker_id: str) -> None:
            linked = store.get_schedule(schedule["schedule_id"])
            assert linked and linked["queued_run_id"]
            run_id = str(linked["queued_run_id"])
            store.update_run(run_id, state="running", started_at=datetime.now(timezone.utc).isoformat())
            store.finalize_run(run_id, state="completed", output_text="fast complete")

        service._ensure_worker_processor = complete_immediately_after_schedule_is_linked  # type: ignore[method-assign]

        processed = service.process_due_schedules_once()

        assert processed and processed[0]["state"] == "completed"
        refreshed = store.get_schedule(schedule["schedule_id"])
        assert refreshed
        assert refreshed["state"] == "completed"
        assert refreshed["queued_run_id"]
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("run_state", "schedule_state"),
    [("cancelled", "cancelled"), ("failed", "failed")],
)
def test_cancel_pending_runs_finalizes_linked_schedule(tmp_path, run_state, schedule_state):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Cancel Schedule", "Finalize linked schedules.", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Cancel Worker",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "scheduled work", state="queued")
    schedule = store.create_scheduled_run(
        worker_id=worker["worker_id"],
        project_id=project["project_id"],
        tenant_id="local",
        owner_id="owner",
        instruction="scheduled work",
        run_at=datetime.now(timezone.utc).isoformat(),
        schedule_text="now",
    )
    store.finalize_schedule(schedule["schedule_id"], state="queued", queued_run_id=run["run_id"])

    store.cancel_pending_runs(worker["worker_id"], error_text="stop work", state=run_state)

    refreshed = store.get_schedule(schedule["schedule_id"])
    assert refreshed
    assert refreshed["state"] == schedule_state
    assert refreshed["last_error"] == "stop work"


def test_finalize_schedule_does_not_downgrade_terminal_state(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Terminal Schedule", "Do not regress terminal state.", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Terminal Worker",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )
    schedule = store.create_scheduled_run(
        worker_id=worker["worker_id"],
        project_id=project["project_id"],
        tenant_id="local",
        owner_id="owner",
        instruction="scheduled work",
        run_at=datetime.now(timezone.utc).isoformat(),
        schedule_text="now",
    )
    store.finalize_schedule(schedule["schedule_id"], state="completed", queued_run_id="run_done")

    refreshed = store.finalize_schedule(schedule["schedule_id"], state="queued", queued_run_id="run_late")

    assert refreshed
    assert refreshed["state"] == "completed"
    assert refreshed["queued_run_id"] == "run_done"


def test_native_schedule_queues_due_run(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "0")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "0")
    monkeypatch.setenv("WPR_DOCKER_MEMORY_RESERVATION_MB", "1")
    monkeypatch.setenv("WPR_DOCKER_DISK_RESERVATION_MB", "1")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Scheduled Workspace", "goal": "Queue work later."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Scheduler", "role": "operator", "profile": "codex-cli"},
    ).json()

    schedule = client.post(
        f"/v1/workers/{worker['worker_id']}/schedule",
        json={"instruction": "Write scheduled-proof.txt", "delay_seconds": 0, "schedule_text": "in 0 seconds"},
    )

    assert schedule.status_code == 202
    schedule_id = schedule.json()["schedule_id"]
    assert schedule.json()["state"] == "pending"

    processed = client.post("/v1/admin/schedules/run-due")

    assert processed.status_code == 200
    queued = client.get(f"/v1/schedules/{schedule_id}").json()
    assert queued["state"] in {"queued", "running", "completed"}
    assert queued["queued_run_id"]
    queued_run = client.app.state.store.get_run(queued["queued_run_id"])
    assert queued_run is not None
    assert queued_run["first_queued_at"] == queued_run["queued_at"]
    assert queued_run["queue_deadline_at"] > queued_run["queued_at"]
    run = wait_for_run(client, queued["queued_run_id"])
    assert run["state"] == "completed"
    assert "STUB_OK" in run["output_text"]
    done = client.get(f"/v1/schedules/{schedule_id}").json()
    assert done["state"] == "completed"
    assert done["queued_run_id"] == queued["queued_run_id"]


def test_worker_quota_enforced_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MAX_WORKSPACES_PER_USER", "1")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Quota", "goal": "Limit workspace count."},
    ).json()
    first = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "First", "role": "operator", "profile": "codex-cli"},
    )
    second = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Second", "role": "operator", "profile": "codex-cli"},
    )

    assert first.status_code == 201
    assert second.status_code == 429
    payload = second.json()
    assert "GLASSHIVE_MAX_WORKSPACES_PER_USER=1" in payload["detail"]
    assert payload["status"] == "blocked"
    assert payload["failure_class"] == "glasshive_worker_quota_exceeded"
    assert payload["failure_retryable"] == 0
    assert payload["quota"]["env_name"] == "GLASSHIVE_MAX_WORKSPACES_PER_USER"
    assert payload["retry_after_seconds"] is None
    assert "Retry-After" not in second.headers
    assert payload["available_workspace_options"][0]["workspace_name"] == "First"
    assert payload["available_workspace_options"][0]["project_title"] == "Quota"
    assert "available_workspace_options" in payload["main_agent_next_action"]
    assert "switching profile or sandbox mode" in payload["main_agent_next_action"]
    assert "Do not retry this launch on a timer" in payload["main_agent_next_action"]


def test_active_worker_quota_retry_after_uses_idle_release(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "900")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Active Quota", "goal": "Limit active workspace count."},
    ).json()

    first = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "First", "role": "operator", "profile": "codex-cli"},
    )
    second = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Second", "role": "operator", "profile": "codex-cli"},
    )

    assert first.status_code == 201
    assert second.status_code == 429
    payload = second.json()
    assert payload["failure_retryable"] == 1
    assert payload["quota"]["env_name"] == "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER"
    assert payload["retry_after_seconds"] == 900
    assert second.headers["Retry-After"] == "900"
    assert "wait for idle release" in payload["main_agent_next_action"]


def test_active_worker_quota_counts_resuming_workers(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER", "1")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = service.create_project("owner", "Active Quota", "Limit active workspaces.", "codex-cli")
        first = service.create_worker(
            project["project_id"],
            "owner",
            "First",
            "operator",
            "codex-cli",
            "stub",
            start_synchronously=False,
        )
        store.update_worker_state(first["worker_id"], "resuming")

        with pytest.raises(GlassHiveQuotaExceededError) as exc:
            service.create_worker(
                project["project_id"],
                "owner",
                "Second",
                "operator",
                "codex-cli",
                "stub",
                start_synchronously=False,
            )

        assert "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER=1" in str(exc.value)
        assert exc.value.env_name == "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER"
        assert exc.value.available_workspace_options[0]["workspace_name"] == "First"
    finally:
        service.shutdown()


def test_tenant_quota_options_remain_requesting_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT", "2")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project_a = service.create_project("owner-a", "Owner A", "Owner A work.", "codex-cli", tenant_id="tenant-1")
        project_b = service.create_project("owner-b", "Owner B", "Owner B work.", "codex-cli", tenant_id="tenant-1")
        first_a = service.create_worker(
            project_a["project_id"],
            "owner-a",
            "Owner A Active",
            "operator",
            "codex-cli",
            "stub",
            tenant_id="tenant-1",
            start_synchronously=False,
        )
        first_b = service.create_worker(
            project_b["project_id"],
            "owner-b",
            "Owner B Active",
            "operator",
            "codex-cli",
            "stub",
            tenant_id="tenant-1",
            start_synchronously=False,
        )
        store.update_worker_state(first_a["worker_id"], "ready")
        store.update_worker_state(first_b["worker_id"], "ready")

        with pytest.raises(GlassHiveQuotaExceededError) as exc:
            service.create_worker(
                project_a["project_id"],
                "owner-a",
                "Owner A Blocked",
                "operator",
                "codex-cli",
                "stub",
                tenant_id="tenant-1",
                start_synchronously=False,
            )

        assert exc.value.env_name == "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT"
        assert [option["workspace_name"] for option in exc.value.available_workspace_options] == ["Owner A Active"]
        assert "Owner B Active" not in {
            option["workspace_name"] for option in exc.value.available_workspace_options
        }
    finally:
        service.shutdown()


def test_allowed_worker_profiles_guardrail_blocks_disallowed_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Profile guardrail", "goal": "Limit worker profiles."},
    ).json()

    blocked = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Claude", "role": "operator", "profile": "claude-code"},
    )
    allowed = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Codex", "role": "operator", "profile": "codex-cli"},
    )

    assert blocked.status_code == 403
    assert "GLASSHIVE_ALLOWED_WORKER_PROFILES" in blocked.json()["detail"]
    assert allowed.status_code == 201


def test_profile_inventory_does_not_claim_stub_native_support(tmp_path, monkeypatch):
    monkeypatch.setenv('GLASSHIVE_ALLOWED_WORKER_PROFILES', 'codex-cli,grok-build')
    with TestClient(create_app(str(tmp_path / 'profiles.db'), runtime_backend='stub')) as client:
        response = client.get('/v1/worker-profiles')
        assert response.status_code == 200
        items = response.json()['items']
        assert {item['profile'] for item in items} == {'codex-cli', 'grok-build'}
        assert all(item['adapter_registered'] is False for item in items)
        assert all(item['execution_modes'] == [] for item in items)
        assert all(item['shared_workspace'] is False for item in items)
        assert client.get('/v1/provider-readiness/unknown').status_code == 404
