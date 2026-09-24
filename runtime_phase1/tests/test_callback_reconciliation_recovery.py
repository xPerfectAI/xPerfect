from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from workers_projects_runtime.openclaw_runtime import HostCapacityError, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


class _TrustedProviderFailureRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self._provider_evidence = {}

    def trust_provider_failure(self, worker: dict, run: dict, source: dict) -> None:
        self._provider_evidence[id(source)] = (
            source,
            str(worker["worker_id"]),
            str(run["run_id"]),
        )

    def consume_provider_route_failure_evidence(self, worker, run, source):
        trusted = self._provider_evidence.pop(id(source), None)
        if (
            not trusted
            or trusted[0] is not source
            or trusted[1] != str(worker["worker_id"])
            or trusted[2] != str(run["run_id"])
        ):
            return None
        return {
            "version": 1,
            "failure_class": "provider_quota_exhausted",
            "failure_structured": True,
            "retry_at": "",
            "retry_after_s": 1800,
            "evidence_kind": "synthetic_runtime_control",
            "evidence_id": f"synthetic:{run['run_id']}",
        }


def _worker(
    store: Store,
    *,
    owner_id: str,
    label: str,
    callback_url: str = "https://callback.example.invalid/events",
    callback_context: dict | None = None,
) -> tuple[dict, dict]:
    project = store.create_project(
        owner_id, label, "Verify durable terminal callback recovery", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id=owner_id,
        name=f"{label} worker",
        role="worker",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="synthetic-model",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": callback_url,
                "hmac_secret": "synthetic-callback-signing-secret",
                **(callback_context or {}),
            }
        },
    )
    return project, worker


def _running_run(
    store: Store,
    service: WorkersProjectsService,
    worker: dict,
    *,
    instruction: str,
    runtime_family: str = "openclaw",
) -> dict:
    queued = store.create_run(worker["worker_id"], worker["project_id"], instruction)
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=service.executor_id
    )
    assert claimed is not None and claimed["run_id"] == queued["run_id"]
    lease = store.acquire_host_run_lease(
        runtime_family=runtime_family,
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(queued["run_id"]),
        executor_id=service.executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    admitted = store.admit_claimed_run(
        queued["run_id"],
        lease_id=lease["lease_id"],
        executor_id=service.executor_id,
    )
    assert admitted is not None
    invoked = store.mark_run_runtime_invoked(
        queued["run_id"],
        lease_id=lease["lease_id"],
        executor_id=service.executor_id,
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(queued["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=service.executor_id,
        identity_kind="in_process",
        pid=None,
        process_group=None,
        process_start_identity="",
        container_id="",
        session_id="in-process",
    )
    assert confirmed is not None
    return confirmed["run"]


def _service(store: Store, monkeypatch) -> WorkersProjectsService:
    monkeypatch.setattr(
        WorkersProjectsService, "_process_scheduler_cycle", lambda _self: None
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_deliver_callback_record",
        lambda _self, *_args, **_kwargs: None,
    )
    service = WorkersProjectsService(
        store,
        _TrustedProviderFailureRuntime(),
        max_workers=2,
        reconcile_on_startup=False,
    )
    service._startup_recovery_thread.join(timeout=2)
    assert not service._startup_recovery_thread.is_alive()
    return service


def test_recovered_provider_failure_emits_the_exact_durable_terminal_generation(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "recovered-failure.sqlite3"))
    _project, worker = _worker(store, owner_id="owner-a", label="Recovered failure")
    service = _service(store, monkeypatch)
    try:
        running = _running_run(
            store, service, worker, instruction="Recover a synthetic provider failure"
        )
        recovered = {
            "state": "failed",
            "output_text": "",
            "error_text": "The configured provider ended its response.",
            "failure_class": "provider_response_failed",
            "failure_retryable": 1,
            "failure_structured": 1,
            "failure_user_message": "The configured provider ended its response.",
            "failure_recommended_recovery": "Retry the same configured route.",
            "failure_diagnostic_summary": "Synthetic structured provider failure.",
            "_terminal_generation": service._terminal_generation_for_run(running),
        }

        service._apply_recovered_run(worker, running, recovered)

        terminal = store.get_run(str(running["run_id"]))
        assert terminal is not None and terminal["state"] == "failed"
        callbacks = store.list_callback_outbox_for_run(
            str(running["run_id"]), tenant_id="local", owner_id="owner-a"
        )
        assert len(callbacks) == 1
        payload = json.loads(str(callbacks[0]["payload_json"]))
        assert payload["event"] == "run.failed"
        assert payload["result_state"] == "failed"
        assert payload["result_ended_at"] == terminal["ended_at"]
        assert payload["result_revision"] == terminal["terminal_result_revision"]
        assert payload["attempt_number"] == 1
        assert payload["failure_class"] == "provider_response_failed"
        assert payload["failure_retryable"] is True
    finally:
        service.shutdown()


def test_legacy_callback_rows_cannot_starve_a_later_owner_scoped_terminal_result(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "fair-terminal-reconciliation.sqlite3"))
    legacy_project, legacy_worker = _worker(
        store, owner_id="legacy-owner", label="Historical callback"
    )
    _current_project, current_worker = _worker(
        store, owner_id="current-owner", label="Current callback"
    )
    service = _service(store, monkeypatch)
    try:
        ended_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        legacy_run_ids: list[str] = []
        for index in range(230):
            run = store.create_run(
                legacy_worker["worker_id"],
                legacy_project["project_id"],
                f"Historical synthetic run {index}",
            )
            terminal = store.update_run(
                run["run_id"],
                state="completed",
                ended_at=(ended_at + timedelta(seconds=index)).isoformat(),
                output_text=f"Historical result {index}",
            )
            assert terminal is not None
            store.insert_callback_outbox_once(
                callback_id=f"cb_legacy_{index:03d}",
                project_id=legacy_project["project_id"],
                worker_id=legacy_worker["worker_id"],
                run_id=run["run_id"],
                attempt_number=None,
                event_type="run.completed",
                url="https://callback.example.invalid/events",
                payload_json=json.dumps(
                    {
                        "callback_id": f"cb_legacy_{index:03d}",
                        "callback_ts": 1_700_000_000 + index,
                        "event": "run.completed",
                        "run_id": run["run_id"],
                    }
                ),
            )
            legacy_run_ids.append(str(run["run_id"]))

        running = _running_run(
            store, service, current_worker, instruction="Recover the current owner run"
        )
        generation = service._terminal_generation_for_run(running)
        terminal = store.finalize_run_if_state(
            str(running["run_id"]),
            "running",
            "failed",
            error_text="Current configured provider failed.",
            failure_class="provider_response_failed",
            failure_retryable=1,
            failure_structured=1,
            **generation,
        )
        assert terminal is not None

        assert service._reconcile_terminal_callback_intents(limit=50) == 1

        current_callbacks = store.list_callback_outbox_for_run(
            str(running["run_id"]), tenant_id="local", owner_id="current-owner"
        )
        assert len(current_callbacks) == 1
        assert current_callbacks[0]["event_type"] == "run.failed"
        assert (
            store.list_callback_outbox_for_run(
                str(running["run_id"]), tenant_id="local", owner_id="legacy-owner"
            )
            == []
        )
        with store._connect() as connection:
            quarantined = connection.execute(
                "SELECT COUNT(*) FROM terminal_callback_reconciliations "
                "WHERE status = 'unavailable' AND reason_code = ?",
                ("callback_context_incomplete",),
            ).fetchone()[0]
        assert quarantined == len(legacy_run_ids)
        assert service._reconcile_terminal_callback_intents(limit=50) == 0
        assert all(
            len(
                store.list_callback_outbox_for_run(
                    run_id, tenant_id="local", owner_id="legacy-owner"
                )
            )
            == 1
            for run_id in legacy_run_ids
        )
    finally:
        service.shutdown()


def test_invalid_terminal_callback_identity_is_quarantined_without_reprocessing(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "invalid-terminal-callback.sqlite3"))
    project, worker = _worker(
        store,
        owner_id="synthetic-owner",
        label="Missing durable origin",
        callback_url=(
            "https://callback.example.invalid/api/viventium/glasshive/callback"
        ),
        callback_context={
            "user_id": "synthetic-owner",
            "conversation_id": "synthetic-conversation",
            "parent_message_id": "synthetic-parent",
            "message_id": "synthetic-message",
        },
    )
    service = _service(store, monkeypatch)
    try:
        run = store.create_run(
            worker["worker_id"], project["project_id"], "Synthetic invalid origin"
        )
        terminal = store.update_run(
            str(run["run_id"]),
            state="completed",
            ended_at="2026-01-01T12:00:00+00:00",
            output_text="Synthetic result",
        )
        assert terminal is not None

        assert service._reconcile_terminal_callback_intents() == 0
        assert service._reconcile_terminal_callback_intents() == 0
        assert store.list_terminal_runs_missing_callback_intent() == []
        assert (
            store.list_callback_outbox_for_run(
                str(run["run_id"]), tenant_id="local", owner_id="synthetic-owner"
            )
            == []
        )
        with store._connect() as connection:
            quarantined = connection.execute(
                "SELECT status, reason_code FROM terminal_callback_reconciliations "
                "WHERE run_id = ?",
                (str(run["run_id"]),),
            ).fetchone()
        assert tuple(quarantined) == ("unavailable", "callback_context_incomplete")
    finally:
        service.shutdown()


def test_restart_reconciles_and_delivers_one_exact_scheduler_failure_callback(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "scheduler-callback-recovery.sqlite3"))
    callback_url = (
        "http://127.0.0.1:7110/internal/scheduled-prompts/glasshive-callback"
    )
    _project, worker = _worker(
        store,
        owner_id="scheduler-owner",
        label="Scheduled callback",
        callback_url=callback_url,
    )
    deliver = WorkersProjectsService._deliver_callback_record
    first_service = _service(store, monkeypatch)
    running = _running_run(
        store, first_service, worker, instruction="Recover scheduled nightly output"
    )
    for event_type in ("run.queued", "run.started"):
        progress = first_service._emit_callback(
            worker,
            event_type,
            run=running,
            message="Scheduled worker progress was already acknowledged.",
            submit_delivery=False,
        )
        assert progress is not None
        claimed = store.claim_pending_callback(str(progress["callback_id"]))
        assert claimed is not None
        accepted = store.mark_callback_http_accepted(
            str(progress["callback_id"]),
            lease_token=str(claimed["delivery_lease_token"]),
            delivery_generation=int(claimed["delivery_generation"]),
            attempts=1,
            payload_json=str(claimed["payload_json"]),
        )
        assert accepted is not None and accepted["status"] == "http_accepted"
    terminal = store.finalize_run_if_state(
        str(running["run_id"]),
        "running",
        "failed",
        error_text="The configured provider ended its response.",
        failure_class="provider_response_failed",
        failure_retryable=1,
        failure_structured=1,
        **first_service._terminal_generation_for_run(running),
    )
    assert terminal is not None
    first_service.shutdown()
    monkeypatch.setattr(WorkersProjectsService, "_deliver_callback_record", deliver)

    received: list[dict] = []

    def accept(url, *, content, headers, timeout):
        assert url == callback_url
        assert timeout == 5.0
        assert str(headers.get("X-GlassHive-Signature") or "").startswith("sha256=")
        payload = json.loads(content)
        received.append(payload)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "callback_status": "accepted",
                "callback_id": payload["callback_id"],
                "run_id": payload["run_id"],
                "result_revision": payload["result_revision"],
                "result_digest": payload["result_digest"],
                "current_result_revision": payload["result_revision"],
                "current_result_digest": payload["result_digest"],
                "current_callback_id": payload["callback_id"],
            },
        )

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", accept)
    restarted = WorkersProjectsService(
        store, StubRuntime(), max_workers=2, reconcile_on_startup=False
    )
    try:
        restarted._startup_recovery_thread.join(timeout=3)
        assert not restarted._startup_recovery_thread.is_alive()
        restarted._callback_retry_tick()
        restarted._callback_retry_tick()

        assert len(received) == 1
        assert received[0]["run_id"] == running["run_id"]
        assert received[0]["event"] == "run.failed"
        assert received[0]["failure_class"] == "provider_response_failed"
        assert received[0]["failure_retryable"] is True
        persisted = store.list_callback_outbox_for_run(
            str(running["run_id"]), tenant_id="local", owner_id="scheduler-owner"
        )
        assert len(persisted) == 3
        terminal_callbacks = [
            record for record in persisted if record["event_type"] == "run.failed"
        ]
        assert len(terminal_callbacks) == 1
        assert terminal_callbacks[0]["status"] == "http_accepted"
    finally:
        restarted.shutdown()


def _scheduled_provider_worker(
    store: Store,
    *,
    owner_id: str,
    fallback_profile: str = "",
    fallback_model: str = "",
    fallback_effort: str = "",
) -> dict:
    project = store.create_project(
        owner_id,
        "Scheduled health analysis",
        "Keep scheduled provider failures truthful and recoverable.",
        "codex-cli",
    )
    bundle: dict = {
        "callbacks": {
            "events_webhook_url": (
                "http://127.0.0.1:7110/internal/scheduled-prompts/glasshive-callback"
            ),
            "hmac_secret": "synthetic-scheduled-callback-secret",
        }
    }
    if fallback_profile:
        authority = {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
            "fallback_worker_profile": fallback_profile,
        }
        if fallback_model:
            authority["fallback_worker_model"] = fallback_model
        if fallback_effort:
            authority["fallback_worker_reasoning_effort"] = fallback_effort
        bundle.update(
            {
                "execution_policy": "parallel-clean-room-v1",
                "viventium_launch_authority": authority,
            }
        )
    return store.create_worker(
        project_id=project["project_id"],
        owner_id=owner_id,
        name="Scheduled analysis worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="stub/codex-cli",
        bootstrap_bundle=bundle,
    )


def _structured_provider_quota_failure() -> dict:
    return {
        "state": "failed",
        "output_text": "",
        "error_text": "The configured provider quota is exhausted.",
        "failure_class": "provider_quota_exhausted",
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "The configured provider quota is exhausted.",
        "failure_recommended_recovery": "Use the configured fallback worker.",
        "failure_diagnostic_summary": "Synthetic trusted provider quota evidence.",
        "provider_retry_after_s": 1800,
        "provider_failure_source": "provider_native",
        "provider_failure_attestation": {
            "version": 1,
            "producer": "glasshive.profile_runtime.codex-cli",
            "evidence_kind": "provider_native",
            "schema": "provider_native_terminal_v1",
        },
    }


def test_scheduled_quota_failure_without_authorized_fallback_is_explicit(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "scheduled-quota-no-fallback.sqlite3"))
    worker = _scheduled_provider_worker(store, owner_id="scheduled-owner")
    service = _service(store, monkeypatch)
    try:
        running = _running_run(
            store,
            service,
            worker,
            instruction="Complete scheduled analysis only on an authorized provider.",
            runtime_family="codex-cli",
        )

        recovered = _structured_provider_quota_failure()
        service.runtime.trust_provider_failure(worker, running, recovered)
        service._apply_recovered_run(worker, running, recovered)

        terminal = store.get_run(str(running["run_id"]))
        assert terminal is not None
        assert terminal["state"] == "failed"
        assert terminal["failure_class"] == "provider_quota_exhausted"
        assert terminal["provider_route_decision"] == "fallback_unavailable"
        assert "explicitly" in terminal["failure_recommended_recovery"]
        assert store.get_worker(str(worker["worker_id"]))["profile"] == "codex-cli"

        route_health = store.get_provider_route_health(
            **service._provider_route(worker)
        )
        assert route_health is not None
        assert route_health["failure_class"] == "provider_quota_exhausted"

        failures = [
            event
            for event in store.list_events(str(worker["worker_id"]))
            if event["event_type"] == "run.provider_fallback_unavailable"
        ]
        assert len(failures) == 1
        payload = json.loads(str(failures[0]["payload_json"]))
        assert payload["reason"] == "fallback_not_authorized"
        assert payload["failureClass"] == "provider_quota_exhausted"

        callbacks = store.list_callback_outbox_for_run(
            str(running["run_id"]), tenant_id="local", owner_id="scheduled-owner"
        )
        assert len(callbacks) == 1
        callback_payload = json.loads(str(callbacks[0]["payload_json"]))
        assert callback_payload["event"] == "run.failed"
        assert callback_payload["failure_class"] == "provider_quota_exhausted"
        assert callback_payload["failure_retryable"] is True
        assert callback_payload["provider_route_decision"] == "fallback_unavailable"
    finally:
        service.shutdown()


def test_authorized_fallback_already_in_cooldown_is_never_selected(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "scheduled-quota-unhealthy-fallback.sqlite3"))
    worker = _scheduled_provider_worker(
        store,
        owner_id="scheduled-owner",
        fallback_profile="claude-code",
    )
    service = _service(store, monkeypatch)
    try:
        running = _running_run(
            store,
            service,
            worker,
            instruction="Do not switch to an exhausted fallback provider.",
            runtime_family="codex-cli",
        )
        fallback_retry_at = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat()
        fallback_health = store.record_provider_route_failure(
            tenant_id="local",
            owner_id="scheduled-owner",
            profile="claude-code",
            runtime="claude-code",
            model="stub/claude-code",
            failure_class="provider_quota_exhausted",
            failure_structured=True,
            retry_at=fallback_retry_at,
            default_cooldown_s=300,
            run_id="prior-synthetic-fallback-failure",
        )
        assert fallback_health is not None

        recovered = _structured_provider_quota_failure()
        service.runtime.trust_provider_failure(worker, running, recovered)
        service._apply_recovered_run(worker, running, recovered)

        terminal = store.get_run(str(running["run_id"]))
        assert terminal is not None
        assert terminal["state"] == "failed"
        assert terminal["failure_class"] == "provider_quota_exhausted"
        assert terminal["provider_route_decision"] == "fallback_unavailable"
        assert store.get_worker(str(worker["worker_id"]))["profile"] == "codex-cli"

        events = store.list_events(str(worker["worker_id"]))
        assert not any(
            event["event_type"] == "run.provider_route_switched"
            for event in events
        )
        failures = [
            event
            for event in events
            if event["event_type"] == "run.provider_fallback_unavailable"
        ]
        assert len(failures) == 1
        payload = json.loads(str(failures[0]["payload_json"]))
        assert payload["reason"] == "fallback_in_cooldown"
        assert payload["fallbackProfile"] == "claude-code"
        assert payload["fallbackCooldownUntil"] == fallback_retry_at
    finally:
        service.shutdown()


def _production_evidence_runtime(runtime):
    """Bind the production route-failure evidence issuer/consumer onto the test stub."""
    import threading

    from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime

    runtime.runtime_name = "codex-cli"
    runtime._provider_route_evidence = {}
    runtime._provider_route_evidence_lock = threading.Lock()
    runtime._issue_provider_route_failure_evidence = (
        BaseCliWorkerRuntime._issue_provider_route_failure_evidence.__get__(runtime)
    )
    runtime.consume_provider_route_failure_evidence = (
        BaseCliWorkerRuntime.consume_provider_route_failure_evidence.__get__(runtime)
    )
    return runtime


def _collected_quota_evidence(run_id: str) -> dict:
    return {
        "version": 1,
        "failure_class": "provider_quota_exhausted",
        "failure_structured": True,
        "retry_at": "",
        "retry_after_s": 1800,
        "evidence_kind": "provider_native",
        "evidence_id": f"collected:{run_id}",
    }


def test_collected_quota_failure_survives_result_rewrapping_and_switches_to_fallback(
    tmp_path, monkeypatch
):
    """Regression: a collected (docker) run's structured quota evidence is stamped on the
    collected result dict, but the service re-wraps that dict (`{**recovered, ...}`) before it
    records route health. The copied result must still be trusted so the run records route
    health and continues on the authorized fallback worker instead of failing silently."""
    store = Store(str(tmp_path / "collected-quota-rewrap.sqlite3"))
    worker = _scheduled_provider_worker(
        store,
        owner_id="scheduled-owner",
        fallback_profile="claude-code",
        fallback_model="claude-code:claude-opus-5",
        fallback_effort="medium",
    )
    service = _service(store, monkeypatch)
    runtime = _production_evidence_runtime(service.runtime)
    try:
        running = _running_run(
            store,
            service,
            worker,
            instruction="Continue on the authorized fallback after a copied quota result.",
            runtime_family="codex-cli",
        )
        recovered = _structured_provider_quota_failure()
        runtime._issue_provider_route_failure_evidence(
            worker=worker,
            run_id=str(running["run_id"]),
            source=recovered,
            evidence=_collected_quota_evidence(str(running["run_id"])),
        )
        rewrapped = {
            **recovered,
            "_terminal_generation": service._terminal_generation_for_run(running),
        }
        assert rewrapped is not recovered
        service._apply_recovered_run(worker, running, rewrapped)
        route_health = store.get_provider_route_health(**service._provider_route(worker))
        assert route_health is not None
        assert route_health["failure_class"] == "provider_quota_exhausted"
        assert route_health["last_run_id"] == str(running["run_id"])
        switched = store.get_run(str(running["run_id"]))
        assert switched is not None
        assert switched["provider_route_decision"] == "fallback_selected"
        assert switched["provider_route_profile"] == "claude-code"
        switched_worker = store.get_worker(str(worker["worker_id"]))
        assert switched_worker["profile"] == "claude-code"
        assert switched_worker["execution_mode"] == "docker"
        assert switched_worker["model"] == "claude-opus-5"
        switched_bundle = json.loads(switched_worker["bootstrap_bundle_json"])
        assert switched_bundle["provider_model"] == "claude-opus-5"
        assert switched_bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "medium"
        assert runtime._provider_route_evidence == {}
    finally:
        service.shutdown()


def test_collected_quota_evidence_copy_without_issued_token_is_rejected(tmp_path, monkeypatch):
    """A copy that lost the issued capability token, or carries a forged one, must not record
    route health: the run fails with the fallback explicitly unavailable."""
    store = Store(str(tmp_path / "collected-quota-forged.sqlite3"))
    worker = _scheduled_provider_worker(
        store, owner_id="scheduled-owner", fallback_profile="claude-code"
    )
    service = _service(store, monkeypatch)
    runtime = _production_evidence_runtime(service.runtime)
    try:
        running = _running_run(
            store,
            service,
            worker,
            instruction="Never trust a forged capability token.",
            runtime_family="codex-cli",
        )
        recovered = _structured_provider_quota_failure()
        runtime._issue_provider_route_failure_evidence(
            worker=worker,
            run_id=str(running["run_id"]),
            source=recovered,
            evidence=_collected_quota_evidence(str(running["run_id"])),
        )
        forged = {
            **recovered,
            "_provider_route_health_capability": "forged-token",
            "_terminal_generation": service._terminal_generation_for_run(running),
        }
        service._apply_recovered_run(worker, running, forged)
        assert store.get_provider_route_health(**service._provider_route(worker)) is None
        terminal = store.get_run(str(running["run_id"]))
        assert terminal is not None
        assert terminal["state"] == "failed"
        # Without trusted evidence no route health exists, so no fallback switch is attempted.
        assert terminal["provider_route_decision"] != "fallback_selected"
        assert store.get_worker(str(worker["worker_id"]))["profile"] == "codex-cli"
    finally:
        service.shutdown()


def test_fallback_switch_survives_transient_host_capacity_during_preflight(tmp_path, monkeypatch):
    """A transient host capacity or Docker probe state during the fallback preflight is not a
    fallback configuration problem: the run still switches to the authorized fallback, whose
    requeued attempt is re-admitted by the same capacity gate."""
    store = Store(str(tmp_path / "fallback-transient-capacity.sqlite3"))
    worker = _scheduled_provider_worker(
        store, owner_id="scheduled-owner", fallback_profile="claude-code"
    )
    service = _service(store, monkeypatch)
    runtime = _production_evidence_runtime(service.runtime)
    probes: list[str] = []

    def _probe_unavailable(profile, execution_mode, **_kwargs):
        probes.append(profile)
        if profile == "claude-code":
            raise HostCapacityError(
                "Host resource admission is waiting for a healthy resource probe.",
                capacity_class="resource_probe_unavailable",
            )
        return {}

    monkeypatch.setattr(service, "_reserved_runtime_preflight", _probe_unavailable)
    try:
        running = _running_run(
            store,
            service,
            worker,
            instruction="Switch even while the host probe is briefly unavailable.",
            runtime_family="codex-cli",
        )
        recovered = _structured_provider_quota_failure()
        runtime._issue_provider_route_failure_evidence(
            worker=worker,
            run_id=str(running["run_id"]),
            source=recovered,
            evidence=_collected_quota_evidence(str(running["run_id"])),
        )
        service._apply_recovered_run(
            worker,
            running,
            {**recovered, "_terminal_generation": service._terminal_generation_for_run(running)},
        )
        assert "claude-code" in probes
        switched = store.get_run(str(running["run_id"]))
        assert switched is not None
        assert switched["provider_route_decision"] == "fallback_selected"
        assert switched["provider_route_profile"] == "claude-code"
        assert store.get_worker(str(worker["worker_id"]))["profile"] == "claude-code"
        events = [event["event_type"] for event in store.list_events(str(worker["worker_id"]))]
        assert "run.provider_route_switched" in events
        assert "run.provider_fallback_unavailable" not in events
    finally:
        service.shutdown()


def test_fallback_preflight_non_capacity_failure_still_preserves_the_primary_failure(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "fallback-preflight-broken.sqlite3"))
    worker = _scheduled_provider_worker(
        store, owner_id="scheduled-owner", fallback_profile="claude-code"
    )
    service = _service(store, monkeypatch)
    runtime = _production_evidence_runtime(service.runtime)

    def _broken(profile, execution_mode, **_kwargs):
        if profile == "claude-code":
            raise RuntimeError("fallback runtime misconfigured")
        return {}

    monkeypatch.setattr(service, "_reserved_runtime_preflight", _broken)
    try:
        running = _running_run(
            store,
            service,
            worker,
            instruction="Keep the primary failure when the fallback preflight is broken.",
            runtime_family="codex-cli",
        )
        recovered = _structured_provider_quota_failure()
        runtime._issue_provider_route_failure_evidence(
            worker=worker,
            run_id=str(running["run_id"]),
            source=recovered,
            evidence=_collected_quota_evidence(str(running["run_id"])),
        )
        service._apply_recovered_run(
            worker,
            running,
            {**recovered, "_terminal_generation": service._terminal_generation_for_run(running)},
        )
        terminal = store.get_run(str(running["run_id"]))
        assert terminal is not None
        assert terminal["state"] == "failed"
        assert terminal["provider_route_decision"] == "fallback_unavailable"
        assert store.get_worker(str(worker["worker_id"]))["profile"] == "codex-cli"
        unavailable = [
            json.loads(event["payload_json"] or "{}")
            for event in store.list_events(str(worker["worker_id"]))
            if event["event_type"] == "run.provider_fallback_unavailable"
        ]
        assert unavailable and unavailable[-1]["reason"] == "fallback_preflight_failed"
    finally:
        service.shutdown()


def test_fresh_partial_html_after_a_provider_failure_is_delivered_without_claiming_completion(
    tmp_path, monkeypatch
):
    """A file that appeared before the provider ended the worker is a partial artifact, not a result."""
    store = Store(str(tmp_path / "partial-html.sqlite3"))
    _project, worker = _worker(store, owner_id="owner-a", label="Partial HTML")
    workspace = tmp_path / "workspace"
    (workspace / "deliverables").mkdir(parents=True)
    store.update_worker(worker["worker_id"], workspace_dir=str(workspace))
    worker = store.get_worker(worker["worker_id"])
    service = _service(store, monkeypatch)
    try:
        running = _running_run(store, service, worker, instruction="Build a countdown card")
        artifact = workspace / "deliverables" / "countdown-card.html"
        artifact.write_text("<html><body><h1>Countdown</h1><!-- unfinished")
        deliverable = {
            "kind": "file",
            "label": "countdown-card.html",
            "workspace_path": "deliverables/countdown-card.html",
        }
        monkeypatch.setattr(
            service, "_completion_deliverable", lambda *_args, **_kwargs: dict(deliverable)
        )
        # The stub runtime reports no workspace on refresh; keep the durable worker record so the
        # artifact freshness check sees the real workspace root, as the docker runtime would.
        monkeypatch.setattr(
            service, "_refresh_runtime_info", lambda worker_id, **_kwargs: store.get_worker(worker_id)
        )
        assert service._fresh_user_artifact_deliverable(
            worker, {**running, "failure_class": "provider_response_failed"}, deliverable
        )
        recovered = {
            "state": "failed",
            "output_text": "",
            "error_text": "The model provider ended the worker continuation unexpectedly.",
            "failure_class": "provider_response_failed",
            "failure_retryable": 1,
            "failure_structured": 1,
            "failure_user_message": "The model provider ended the worker continuation unexpectedly before it could finish.",
            "failure_recommended_recovery": "Use workspace_continue to resume from the same durable workspace.",
            "failure_diagnostic_summary": "Synthetic terminal provider error.",
            "_terminal_generation": service._terminal_generation_for_run(running),
        }

        service._apply_recovered_run(worker, running, recovered)

        terminal = store.get_run(str(running["run_id"]))
        assert terminal is not None and terminal["state"] == "failed"
        assert terminal["failure_class"] == "provider_response_failed"
        callbacks = store.list_callback_outbox_for_run(
            str(running["run_id"]), tenant_id="local", owner_id="owner-a"
        )
        assert [json.loads(str(c["payload_json"]))["event"] for c in callbacks] == ["run.failed"]
        payload = json.loads(str(callbacks[0]["payload_json"]))
        assert payload["result_state"] == "failed"
        assert payload["deliverable"]["workspace_path"] == "deliverables/countdown-card.html"
        assert "partial artifact" in payload["message"]
        assert "did not complete" in payload["message"]
        assert "FINAL REPORT" not in payload["message"]
        events = {event["event_type"] for event in store.list_events(worker["worker_id"])}
        assert "run.failed" in events and "run.partial_artifact_preserved" in events
        assert "run.completed" not in events
    finally:
        service.shutdown()
