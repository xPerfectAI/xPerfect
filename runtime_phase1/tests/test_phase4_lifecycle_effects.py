from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime import signed_links
from workers_projects_runtime.signed_links import (
    create_signed_link_ref,
    resolve_signed_link_ref,
    revoke_signed_link_refs_for_worker,
    sign_link_token,
)
from workers_projects_runtime.store import Store


def _runtime_fixture(tmp_path, *, callbacks: dict | None = None):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner-a", "Lifecycle effects", "Exercise durable sinks", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Lifecycle worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        bootstrap_bundle={"callbacks": callbacks} if callbacks is not None else None,
    )
    service = WorkersProjectsService(
        store, StubRuntime(), max_workers=1, reconcile_on_startup=False
    )
    # Serialize behind the constructor recovery job before tests enqueue
    # effects; otherwise a legitimate startup drain can race assertions about
    # the explicit drain under test.
    service.executor.submit(lambda: None).result(timeout=2)
    service.executor.submit = lambda *_args, **_kwargs: None
    return store, service, project, worker


def _truthfully_invoke_run(
    store: Store, service: WorkersProjectsService, worker: dict, run: dict
) -> dict:
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=service._executor_id
    )
    assert claimed is not None and claimed["run_id"] == run["run_id"]
    lease = store.acquire_host_run_lease(
        runtime_family="openclaw",
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=service._executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    assert store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=service._executor_id,
    )
    invoked = store.mark_run_runtime_invoked(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=service._executor_id,
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=service._executor_id,
        identity_kind="in_process",
        pid=None,
        process_group=None,
        process_start_identity="",
        container_id="",
        session_id="in-process",
    )
    assert confirmed is not None
    return confirmed["run"]


def _exact_terminal_generation(store: Store, run_id: str) -> dict[str, str]:
    run = store.get_run(run_id)
    assert run is not None
    return {
        **(store.get_run_retry_generation(run_id) or {}),
        "expected_runtime_invoked_at": str(run.get("runtime_invoked_at") or ""),
    }


def _enqueue_effect(
    store: Store,
    worker: dict,
    effect_kind: str,
    *,
    operation_kind: str = "pause_run",
    run_id: str = "",
    token: str = "phase4-operation",
) -> str:
    if effect_kind.startswith("callback.") and not run_id:
        callback_run = store.create_run(
            worker["worker_id"],
            worker["project_id"],
            f"Exact callback generation for {effect_kind}",
            state="completed",
        )
        run_id = str(callback_run["run_id"])
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        effect_id = store._enqueue_lifecycle_effects(
            conn,
            operation_token=token,
            operation_epoch=1,
            operation_kind=operation_kind,
            worker_id=worker["worker_id"],
            run_id=run_id,
            effect_kinds=(effect_kind,),
        )[0]
        conn.execute("COMMIT")
    return effect_id


def _callback_rows(store: Store) -> list[dict]:
    with store._connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM callback_outbox ORDER BY callback_id"
            ).fetchall()
        ]


def _exact_callback_payload(
    *,
    callback_id: str,
    event: str,
    worker: dict,
    run: dict,
    version: int = 1,
) -> str:
    return json.dumps(
        {
            "version": version,
            "callback_id": callback_id,
            "callback_ts": 1_700_000_000.0,
            "attempt_number": None,
            "event": event,
            "project_id": worker["project_id"],
            "worker_id": worker["worker_id"],
            "run_id": run["run_id"],
        }
    )


@pytest.mark.parametrize(
    "existing_status",
    ["pending", "delivering", "http_accepted", "delivered", "dead_lettered"],
)
def test_callback_outbox_changed_replay_fails_without_resetting_delivery_state(
    tmp_path, existing_status
):
    store, service, project, worker = _runtime_fixture(tmp_path)
    callback_id = "cb_effect_ope_insert_once"
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Insert once", state="completed"
    )
    original_payload = _exact_callback_payload(
        callback_id=callback_id,
        event="worker.paused",
        worker=worker,
        run=run,
    )
    try:
        first = store.insert_callback_outbox_once(
            callback_id=callback_id,
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            attempt_number=None,
            event_type="worker.paused",
            url="http://callback.local/events",
            payload_json=original_payload,
        )
        assert first["status"] == "pending"
        assert first["_inserted"] is True
        with store._connect() as conn:
            conn.execute(
                "UPDATE callback_outbox SET status = ?, attempts = 3 WHERE callback_id = ?",
                (existing_status, callback_id),
            )

        with pytest.raises(RuntimeError, match="Callback identity collision"):
            store.insert_callback_outbox_once(
                callback_id=callback_id,
                project_id=project["project_id"],
                worker_id=worker["worker_id"],
                run_id=run["run_id"],
                attempt_number=None,
                event_type="worker.paused",
                url="http://callback.local/events",
                payload_json=_exact_callback_payload(
                    callback_id=callback_id,
                    event="worker.paused",
                    worker=worker,
                    run=run,
                    version=2,
                ),
            )

        replay = store.get_callback_outbox(callback_id)
        assert replay is not None
        assert replay["status"] == existing_status
        assert replay["attempts"] == 3
        assert replay["url"] == "http://callback.local/events"
        assert replay["payload_json"] == original_payload
        with store._connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM callback_trace_events WHERE callback_id = ?",
                (callback_id,),
            ).fetchone()[0] == 2
    finally:
        service.shutdown()


def test_callback_effect_retries_when_sink_does_not_materialize(
    tmp_path, monkeypatch
):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, _project, worker = _runtime_fixture(
        tmp_path, callbacks=callbacks
    )
    effect_id = _enqueue_effect(
        store, worker, "callback.worker_paused", operation_kind="pause_worker"
    )
    monkeypatch.setattr(service, "_emit_callback", lambda *_args, **_kwargs: None)

    try:
        service._replay_pending_lifecycle_effects()

        effect = {
            row["effect_id"]: row
            for row in store.list_lifecycle_operation_effects()
        }[effect_id]
        assert effect["status"] == "pending"
        assert effect["last_error_code"] == "callback_enqueue_failed"
        assert effect["next_attempt_at"]
        assert _callback_rows(store) == []
    finally:
        service.shutdown()


def test_existing_callback_sink_completes_effect_after_config_disappears(
    tmp_path, monkeypatch
):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    effect_id = _enqueue_effect(
        store, worker, "callback.worker_paused", operation_kind="pause_worker"
    )
    effect = next(
        item
        for item in store.list_lifecycle_operation_effects()
        if item["effect_id"] == effect_id
    )
    run = store.get_run(effect["run_id"])
    assert run is not None
    callback_id = "cb_effect_" + effect_id
    store.insert_callback_outbox_once(
        callback_id=callback_id,
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        attempt_number=None,
        event_type="worker.paused",
        url="http://callback.local/events",
        payload_json=_exact_callback_payload(
            callback_id=callback_id,
            event="worker.paused",
            worker=worker,
            run=run,
        ),
    )
    assert store.claim_pending_callback(callback_id)
    monkeypatch.setattr(service, "_callback_config_for", lambda _worker: {})

    try:
        service._replay_pending_lifecycle_effects()

        effect = {
            row["effect_id"]: row
            for row in store.list_lifecycle_operation_effects()
        }[effect_id]
        assert effect["status"] == "applied"
        rows = _callback_rows(store)
        assert len(rows) == 1
        assert rows[0]["status"] == "delivering"
        assert rows[0]["payload_json"] == _exact_callback_payload(
            callback_id=callback_id,
            event="worker.paused",
            worker=worker,
            run=run,
        )
    finally:
        service.shutdown()


def test_missing_callback_config_backs_off_without_blocking_revocation_kind(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-phase4-secret")
    store, service, _project, bad_worker = _runtime_fixture(
        tmp_path,
        callbacks={
            "events_webhook_url": "http://127.0.0.1/api/viventium/glasshive/callback"
        },
    )
    other_project = store.create_project(
        "owner-a", "Revocation", "Do not let callback HOL block", "openclaw-general"
    )
    revoked_worker = store.create_worker(
        project_id=other_project["project_id"],
        owner_id="owner-a",
        name="Revoked worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
    )
    healthy_project = store.create_project(
        "owner-a", "Healthy callback", "Prove same-kind progress", "openclaw-general"
    )
    healthy_worker = store.create_worker(
        project_id=healthy_project["project_id"],
        owner_id="owner-a",
        name="Healthy callback worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        bootstrap_bundle={
            "callbacks": {"events_webhook_url": "http://callback.local/events"}
        },
    )
    callback_effect = _enqueue_effect(
        store, bad_worker, "callback.worker_paused", operation_kind="pause_worker"
    )
    revoke_effect = _enqueue_effect(
        store,
        revoked_worker,
        "signed_links.revoke_worker",
        operation_kind="terminate_worker",
        token="phase4-revoke-operation",
    )
    healthy_effect = _enqueue_effect(
        store,
        healthy_worker,
        "callback.worker_paused",
        operation_kind="pause_worker",
        token="phase4-healthy-callback",
    )

    try:
        service._replay_pending_lifecycle_effects()
        by_id = {
            row["effect_id"]: row for row in store.list_lifecycle_operation_effects()
        }
        assert by_id[callback_effect]["status"] == "pending"
        assert by_id[callback_effect]["last_error_code"] == "callback_config_missing"
        assert by_id[callback_effect]["next_attempt_at"]
        assert by_id[revoke_effect]["status"] == "applied"
        assert by_id[healthy_effect]["status"] == "applied"
        assert signed_links.is_worker_signed_link_revoked(revoked_worker["worker_id"])
        assert len(_callback_rows(store)) == 1
        serialized_effects = json.dumps(list(by_id.values()), sort_keys=True)
        assert "callback.local" not in serialized_effects
        assert "synthetic-phase4-secret" not in serialized_effects
    finally:
        service.shutdown()


def test_crash_after_callback_sink_is_idempotent_and_preserves_delivering_row(
    tmp_path, monkeypatch
):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Pause exactly once", state="paused"
    )
    effect_id = _enqueue_effect(
        store, worker, "callback.run_paused", run_id=run["run_id"]
    )
    real_mark = store.mark_lifecycle_effect_applied

    def crash_after_sink(effect: str, owner: str, *, lease_epoch: int):
        assert store.retry_lifecycle_effect(
            effect,
            owner,
            lease_epoch=lease_epoch,
            error_code="callback_enqueue_failed",
            retry_delay_s=0,
        )
        raise RuntimeError("synthetic crash after durable callback insert")

    try:
        claim = store.claim_next_lifecycle_effect(
            service._executor_id,
            effect_kinds=("callback.run_paused",),
        )
        assert claim and claim["effect_id"] == effect_id
        monkeypatch.setattr(store, "mark_lifecycle_effect_applied", crash_after_sink)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            service._apply_lifecycle_effect(claim)
        rows = _callback_rows(store)
        assert len(rows) == 1
        assert rows[0]["callback_id"] == "cb_effect_" + effect_id
        assert store.claim_pending_callback(rows[0]["callback_id"])

        monkeypatch.setattr(store, "mark_lifecycle_effect_applied", real_mark)
        service._replay_pending_lifecycle_effects()

        replayed = _callback_rows(store)
        assert len(replayed) == 1
        assert replayed[0]["status"] == "delivering"
        assert store.list_lifecycle_operation_effects()[0]["status"] == "applied"
    finally:
        service.shutdown()


def test_worker_terminated_callback_is_link_free_and_revocation_precedes_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-phase4-secret")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://operator.local")
    callbacks = {"events_webhook_url": "http://callback.local/events", "surface": "web"}
    store, service, _project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
    )
    old_ref = create_signed_link_ref(token=token)
    run = store.create_run(
        worker["worker_id"],
        worker["project_id"],
        "Terminate exact callback generation",
        state="completed",
    )
    revoke_effect = _enqueue_effect(
        store,
        worker,
        "signed_links.revoke_worker",
        operation_kind="terminate_worker",
        run_id=run["run_id"],
        token="phase4-termination-operation",
    )
    callback_effect = _enqueue_effect(
        store,
        worker,
        "callback.worker_terminated",
        operation_kind="terminate_worker",
        run_id=run["run_id"],
        token="phase4-termination-operation",
    )

    try:
        service._replay_pending_lifecycle_effects()
        assert resolve_signed_link_ref(old_ref) is None
        assert signed_links.is_worker_signed_link_revoked(worker["worker_id"])
        rows = _callback_rows(store)
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["event"] == "worker.terminated"
        assert "operator_url" not in payload
        assert "watch_url" not in payload
        assert "http://operator.local" not in payload["message"]
        effects = {
            row["effect_id"]: row for row in store.list_lifecycle_operation_effects()
        }
        assert effects[revoke_effect]["status"] == "applied"
        assert effects[callback_effect]["status"] == "applied"
    finally:
        service.shutdown()


def test_termination_receipt_waits_for_exact_revoke_effect_ack_after_sink_crash(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-phase4-secret")
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, _project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    run = store.create_run(
        worker["worker_id"],
        worker["project_id"],
        "Gate termination receipt",
        state="completed",
    )
    revoke_effect = _enqueue_effect(
        store,
        worker,
        "signed_links.revoke_worker",
        operation_kind="terminate_worker",
        run_id=run["run_id"],
        token="phase4-exact-revoke-gate",
    )
    callback_effect = _enqueue_effect(
        store,
        worker,
        "callback.worker_terminated",
        operation_kind="terminate_worker",
        run_id=run["run_id"],
        token="phase4-exact-revoke-gate",
    )
    try:
        # Model the durable state left by a process that committed revocation
        # and died before acknowledging its paired lifecycle effect.
        revoke_signed_link_refs_for_worker(worker["worker_id"])
        callback_claim = store.claim_next_lifecycle_effect(
            service._executor_id,
            effect_kinds=("callback.worker_terminated",),
        )
        assert callback_claim and callback_claim["effect_id"] == callback_effect
        service._apply_lifecycle_effect(callback_claim)
        effects = {
            row["effect_id"]: row
            for row in store.list_lifecycle_operation_effects()
        }
        assert signed_links.is_worker_signed_link_revoked(worker["worker_id"])
        assert effects[revoke_effect]["status"] == "pending"
        assert effects[callback_effect]["status"] == "pending"
        assert _callback_rows(store) == []

        with store._connect() as conn:
            conn.execute(
                "UPDATE lifecycle_operation_effects SET next_attempt_at = NULL "
                "WHERE effect_id = ?",
                (callback_effect,),
            )
        service._replay_pending_lifecycle_effects()
        effects = {
            row["effect_id"]: row
            for row in store.list_lifecycle_operation_effects()
        }
        assert effects[revoke_effect]["status"] == "applied"
        assert effects[callback_effect]["status"] == "applied"
        assert len(_callback_rows(store)) == 1
    finally:
        service.shutdown()


def test_unexpected_effect_fault_retries_and_does_not_block_later_kind(
    tmp_path, monkeypatch
):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, _project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    revoke_effect = _enqueue_effect(
        store,
        worker,
        "signed_links.revoke_worker",
        operation_kind="terminate_worker",
        token="phase4-faulted-revoke",
    )
    callback_effect = _enqueue_effect(
        store,
        worker,
        "callback.worker_paused",
        operation_kind="pause_worker",
        token="phase4-fault-independent-callback",
    )
    real_apply = service._apply_lifecycle_effect

    def fault_one(effect: dict):
        if effect["effect_id"] == revoke_effect:
            raise RuntimeError("synthetic sink fault")
        return real_apply(effect)

    monkeypatch.setattr(service, "_apply_lifecycle_effect", fault_one)
    try:
        service._replay_pending_lifecycle_effects()

        effects = {
            row["effect_id"]: row
            for row in store.list_lifecycle_operation_effects()
        }
        assert effects[revoke_effect]["status"] == "pending"
        assert effects[revoke_effect]["last_error_code"] == "unknown"
        assert effects[revoke_effect]["attempts"] == 1
        assert effects[callback_effect]["status"] == "applied"
    finally:
        service.shutdown()


def test_transient_claim_failure_does_not_kill_later_recurring_drain(
    tmp_path, monkeypatch, caplog
):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, _project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    effect_id = _enqueue_effect(
        store, worker, "callback.worker_paused", operation_kind="pause_worker"
    )
    real_claim = store.claim_next_lifecycle_effect
    monkeypatch.setattr(
        store,
        "claim_next_lifecycle_effect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic database outage with secret-value")
        ),
    )

    try:
        service._replay_pending_lifecycle_effects()
        assert store.list_lifecycle_operation_effects()[0]["status"] == "pending"
        assert "secret-value" not in caplog.text

        monkeypatch.setattr(store, "claim_next_lifecycle_effect", real_claim)
        service._replay_pending_lifecycle_effects()
        effects = {
            row["effect_id"]: row
            for row in store.list_lifecycle_operation_effects()
        }
        assert effects[effect_id]["status"] == "applied"
    finally:
        service.shutdown()


def test_retry_persistence_failure_leaves_lease_recoverable_without_secret_log(
    tmp_path, monkeypatch, caplog
):
    store, service, _project, worker = _runtime_fixture(tmp_path)
    effect_id = _enqueue_effect(
        store,
        worker,
        "signed_links.revoke_worker",
        operation_kind="terminate_worker",
    )
    monkeypatch.setattr(
        service,
        "_apply_lifecycle_effect",
        lambda _effect: (_ for _ in ()).throw(RuntimeError("synthetic sink fault")),
    )
    monkeypatch.setattr(
        store,
        "retry_lifecycle_effect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("database secret-value")
        ),
    )

    try:
        service._replay_pending_lifecycle_effects()
        effect = {
            row["effect_id"]: row
            for row in store.list_lifecycle_operation_effects()
        }[effect_id]
        assert effect["status"] == "applying"
        assert effect["lease_expires_at"]
        assert "secret-value" not in caplog.text
    finally:
        service.shutdown()


def test_signed_link_revocation_effect_retries_until_the_sink_recovers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-phase4-secret")
    store, service, _project, worker = _runtime_fixture(tmp_path)
    effect_id = _enqueue_effect(
        store,
        worker,
        "signed_links.revoke_worker",
        operation_kind="terminate_worker",
    )
    real_revoke = revoke_signed_link_refs_for_worker

    def unavailable(_worker_id: str) -> int:
        raise OSError("synthetic revocation sink unavailable")

    monkeypatch.setattr(
        "workers_projects_runtime.service.revoke_signed_link_refs_for_worker",
        unavailable,
    )
    try:
        service._replay_pending_lifecycle_effects()
        failed = store.list_lifecycle_operation_effects()[0]
        assert failed["effect_id"] == effect_id
        assert failed["status"] == "pending"
        assert failed["last_error_code"] == "signed_link_revoke_failed"
        assert failed["next_attempt_at"]

        monkeypatch.setattr(
            "workers_projects_runtime.service.revoke_signed_link_refs_for_worker",
            real_revoke,
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE lifecycle_operation_effects SET next_attempt_at = ? WHERE effect_id = ?",
                ("2000-01-01T00:00:00+00:00", effect_id),
            )
        service._replay_pending_lifecycle_effects()
        recovered = store.list_lifecycle_operation_effects()[0]
        assert recovered["status"] == "applied"
        assert recovered["attempts"] == 2
        assert signed_links.is_worker_signed_link_revoked(worker["worker_id"])
    finally:
        service.shutdown()


def test_termination_without_callback_contract_completes_as_a_durable_noop(
    tmp_path,
):
    store, service, _project, worker = _runtime_fixture(tmp_path)
    run = store.create_run(
        worker["worker_id"],
        worker["project_id"],
        "No callback termination generation",
        state="completed",
    )
    revoke_effect = _enqueue_effect(
        store,
        worker,
        "signed_links.revoke_worker",
        operation_kind="terminate_worker",
        run_id=run["run_id"],
        token="phase4-no-callback-termination",
    )
    callback_effect = _enqueue_effect(
        store,
        worker,
        "callback.worker_terminated",
        operation_kind="terminate_worker",
        run_id=run["run_id"],
        token="phase4-no-callback-termination",
    )

    try:
        service._replay_pending_lifecycle_effects()
        effects = {
            row["effect_id"]: row for row in store.list_lifecycle_operation_effects()
        }
        assert effects[revoke_effect]["status"] == "applied"
        assert effects[callback_effect]["status"] == "applied"
        assert effects[callback_effect]["last_error_code"] == ""
        assert _callback_rows(store) == []
    finally:
        service.shutdown()


def test_permanent_worker_revocation_rejects_future_mint_and_resolution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-phase4-secret")
    worker_id = "wrk_phase4_revoked"
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker_id,
        tenant_id="local",
        owner_id="owner-a",
    )
    ref_id = create_signed_link_ref(token=token)
    assert token and ref_id

    revoke_signed_link_refs_for_worker(worker_id)

    assert signed_links.is_worker_signed_link_revoked(worker_id)
    assert resolve_signed_link_ref(ref_id) is None
    assert (
        sign_link_token(
            kind="worker_view",
            worker_id=worker_id,
            tenant_id="local",
            owner_id="owner-a",
        )
        == ""
    )
    assert create_signed_link_ref(token=token) == ""


def test_terminated_worker_state_blocks_links_before_revocation_effect_drains(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "runtime.db"
    monkeypatch.setenv("WPR_DB_PATH", str(db_path))
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-phase4-secret")
    store = Store(str(db_path))
    project = store.create_project(
        "owner-a", "Immediate link fence", "Close the cross-db window", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Immediate link fence worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
    )
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker["worker_id"],
        tenant_id=worker["tenant_id"],
        owner_id=worker["owner_id"],
    )
    ref_id = create_signed_link_ref(token=token)
    assert ref_id

    store.update_worker_state(worker["worker_id"], "terminated")

    assert resolve_signed_link_ref(ref_id) is None
    assert (
        sign_link_token(
            kind="worker_view",
            worker_id=worker["worker_id"],
            tenant_id=worker["tenant_id"],
            owner_id=worker["owner_id"],
        )
        == ""
    )


def test_service_never_mints_or_falls_back_to_unsigned_links_for_terminated_worker(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-phase4-secret")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://operator.local")
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://artifact.local")
    store, service, _project, worker = _runtime_fixture(tmp_path)
    terminated = store.update_worker_state(worker["worker_id"], "terminated") or worker

    try:
        assert service._signed_link_params(terminated, kind="worker_view") == {}
        assert service._signed_watch_url(terminated, {"surface": "web"}) == ""
        assert (
            service._signed_artifact_download_url(terminated, "report.txt") == ""
        )
        assert service._signed_artifact_open_url(terminated, "report.txt") == ""
    finally:
        service.shutdown()


def test_idle_pause_and_resume_callbacks_are_durable_deterministic_effects(
    tmp_path, monkeypatch
):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, _project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    direct_callbacks: list[str] = []
    monkeypatch.setattr(
        service,
        "_emit_callback",
        lambda _worker, event_type, **_kwargs: direct_callbacks.append(event_type),
    )
    monkeypatch.setattr(service, "_replay_pending_lifecycle_effects", lambda: None)

    try:
        paused = service.pause_worker(worker["worker_id"])
        assert paused["state"] == "paused"
        pause_effects = store.list_lifecycle_operation_effects(worker_id=worker["worker_id"])
        assert [row["effect_kind"] for row in pause_effects] == [
            "callback.worker_paused"
        ]

        resumed = service.resume_worker(worker["worker_id"])
        assert resumed["state"] == "ready"
        effects = store.list_lifecycle_operation_effects(worker_id=worker["worker_id"])
        assert [row["effect_kind"] for row in effects] == [
            "callback.worker_paused",
            "callback.worker_resumed",
        ]
        assert direct_callbacks == []
        assert len({row["effect_id"] for row in effects}) == 2
    finally:
        service.shutdown()


def test_work_stop_materializes_one_canonical_terminal_receipt(tmp_path):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store, service, project, worker = _runtime_fixture(tmp_path, callbacks=callbacks)
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Stop this mission"
    )
    run = _truthfully_invoke_run(store, service, worker, run)
    store.update_worker_state(worker["worker_id"], "running")

    try:
        result = service.stop_run(worker["worker_id"], run["run_id"])
        assert result["confirmation_pending"] is False
        assert result["work_stop_outcome"] == "cancelled"
        service._replay_pending_lifecycle_effects()
        service._replay_pending_lifecycle_effects()

        rows = _callback_rows(store)
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["event"] == "run.cancelled"
        assert payload["run_state"] == "cancelled"
        assert payload["message"].startswith("Work stop confirmed")
        assert rows[0]["callback_id"].startswith("cb_effect_ope_")
    finally:
        service.shutdown()


def test_stale_applying_effect_is_recovered_on_startup_with_compute_claim(
    tmp_path, monkeypatch
):
    callbacks = {"events_webhook_url": "http://callback.local/events"}
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner-a", "Startup effects", "Recover a stale sink claim", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Startup worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        bootstrap_bundle={"callbacks": callbacks},
    )
    effect_id = _enqueue_effect(
        store, worker, "callback.worker_paused", operation_kind="pause_worker"
    )
    snapshot = store.get_worker(worker["worker_id"]) or worker
    compute_claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=str(snapshot["updated_at"]),
        expected_last_run_id=str(snapshot.get("last_run_id") or ""),
        expected_state=str(snapshot["state"]),
        expected_container_id="",
        owner="independent-compute-owner",
        ttl_s=300,
        kind="idle",
    )
    assert compute_claim is not None
    origin = datetime.now(timezone.utc)
    claim = store.claim_next_lifecycle_effect(
        "crashed-owner", ttl_s=1, now=origin, effect_kinds=("callback.worker_paused",)
    )
    assert claim and claim["effect_id"] == effect_id
    with store._connect() as conn:
        conn.execute(
            "UPDATE lifecycle_operation_effects SET lease_expires_at = ? WHERE effect_id = ?",
            ((origin - timedelta(seconds=1)).isoformat(), effect_id),
        )

    monkeypatch.setattr(
        WorkersProjectsService,
        "_deliver_callback_record",
        lambda *_args, **_kwargs: None,
    )
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        # Wait for this exact service's dedicated startup recovery. A process-wide
        # method monkeypatch can be triggered by a prior service's recurring
        # thread under the full suite and falsely signal completion here.
        service._startup_recovery_thread.join(timeout=2)
        assert not service._startup_recovery_thread.is_alive()
        assert store.list_lifecycle_operation_effects()[0]["status"] == "applied"
        assert len(_callback_rows(store)) == 1
        assert (store.get_worker(worker["worker_id"]) or {})[
            "compute_release_token"
        ] == compute_claim["token"]
    finally:
        service.shutdown()


def test_startup_recovery_does_not_consume_callbacks_created_after_service_start(
    tmp_path, monkeypatch
):
    """Startup recovery owns only the durable backlog present at its boundary."""

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner-a",
        "Startup boundary",
        "Keep new callbacks out of replay",
        "openclaw-general",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Boundary worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.local/glasshive",
                "hmac_secret": "synthetic-secret",
            }
        },
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Boundary run",
        state="completed",
    )
    lifecycle_started = Event()
    release_recovery = Event()
    callback_replay_finished = Event()
    original_lifecycle_replay = WorkersProjectsService._replay_pending_lifecycle_effects
    original_callback_replay = WorkersProjectsService._replay_pending_callbacks

    def blocked_lifecycle_replay(self, *args, **kwargs):
        lifecycle_started.set()
        assert release_recovery.wait(2)
        return original_lifecycle_replay(self, *args, **kwargs)

    def tracked_callback_replay(self, *args, **kwargs):
        try:
            assert release_recovery.wait(2)
            return original_callback_replay(self, *args, **kwargs)
        finally:
            callback_replay_finished.set()

    delivered: list[str] = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = content, headers, timeout
        delivered.append(str(url))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_pending_lifecycle_effects",
        blocked_lifecycle_replay,
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_replay_pending_callbacks",
        tracked_callback_replay,
    )
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)

    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    try:
        assert lifecycle_started.wait(2)
        callback_id = "cb_after_startup_boundary"
        store.insert_callback_outbox_once(
            callback_id=callback_id,
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            attempt_number=None,
            event_type="run.completed",
            url="http://callback.local/glasshive",
            payload_json=_exact_callback_payload(
                callback_id=callback_id,
                event="run.completed",
                worker=worker,
                run=run,
            ),
        )
        release_recovery.set()
        assert callback_replay_finished.wait(2)

        record = store.get_callback_outbox(callback_id)
        assert record is not None
        assert record["status"] == "pending"
        assert record["attempts"] == 0
        assert delivered == []
    finally:
        release_recovery.set()
        service.shutdown()


def test_startup_recovers_one_terminal_callback_after_commit_boundary_crash(
    tmp_path, monkeypatch
):
    callbacks = {"events_webhook_url": "http://callback.local/glasshive"}
    store, service, project, worker = _runtime_fixture(
        tmp_path, callbacks=callbacks
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Complete before callback intent"
    )
    running = _truthfully_invoke_run(store, service, worker, run)
    terminal = store.finalize_run_if_state(
        run["run_id"],
        "running",
        "completed",
        output_text="Recovered terminal result",
        **_exact_terminal_generation(store, run["run_id"]),
    )
    assert terminal is not None
    assert _callback_rows(store) == []
    service.shutdown()

    monkeypatch.setattr(
        WorkersProjectsService,
        "_deliver_callback_record",
        lambda *_args, **_kwargs: None,
    )
    restarted_store = Store(str(tmp_path / "runtime.db"))
    restarted = WorkersProjectsService(
        restarted_store, StubRuntime(), max_workers=1, reconcile_on_startup=False
    )
    try:
        restarted._startup_recovery_thread.join(timeout=2)
        assert not restarted._startup_recovery_thread.is_alive()
        rows = _callback_rows(restarted_store)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "run.completed"
        assert rows[0]["run_id"] == run["run_id"]
        assert rows[0]["attempt_number"] == 1
        assert rows[0]["callback_id"].startswith("cb_terminal_")

        restarted._reconcile_terminal_callback_intents(
            created_before=restarted._startup_recovery_cutoff
        )
        restarted._reconcile_terminal_callback_intents(
            created_before=restarted._startup_recovery_cutoff
        )
        restarted._replay_pending_callbacks(
            created_before=restarted._startup_recovery_cutoff
        )

        assert len(_callback_rows(restarted_store)) == 1
    finally:
        restarted.shutdown()


def test_stale_terminal_attempt_cannot_recreate_callback_intent(
    tmp_path,
):
    callbacks = {"events_webhook_url": "http://callback.local/glasshive"}
    store, service, project, worker = _runtime_fixture(
        tmp_path, callbacks=callbacks
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Fence stale terminal owner"
    )
    running = _truthfully_invoke_run(store, service, worker, run)
    terminal = store.finalize_run_if_state(
        run["run_id"],
        "running",
        "completed",
        output_text="Current terminal result",
        **_exact_terminal_generation(store, run["run_id"]),
    )
    assert terminal is not None
    current_attempt = store.get_run_attempt(str(running["active_attempt_id"]))
    assert current_attempt is not None
    stale_ended_at = (
        datetime.fromisoformat(str(terminal["ended_at"]).replace("Z", "+00:00"))
        - timedelta(seconds=1)
    ).isoformat()
    stale_callback_id = Store.terminal_callback_id(
        run_id=run["run_id"],
        state="completed",
        ended_at=stale_ended_at,
        attempt_number=1,
        result_revision=int(terminal["terminal_result_revision"]),
        result_digest=Store.terminal_result_digest(terminal),
    )
    stale_payload = json.dumps(
        {
            "callback_id": stale_callback_id,
            "callback_ts": 1_700_000_000.0,
            "attempt_number": 1,
            "event": "run.completed",
            "project_id": project["project_id"],
            "worker_id": worker["worker_id"],
            "run_id": run["run_id"],
        }
    )

    try:
        stale = store.insert_terminal_callback_outbox_if_current(
            callback_id=stale_callback_id,
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            attempt_number=1,
            event_type="run.completed",
            url="http://callback.local/glasshive",
            payload_json=stale_payload,
            expected_state="completed",
            expected_ended_at=stale_ended_at,
            expected_attempt_id=str(running["active_attempt_id"]),
            expected_result_revision=int(
                terminal["terminal_result_revision"]
            ),
            expected_result_digest=Store.terminal_result_digest(terminal),
        )

        assert stale is None
        assert _callback_rows(store) == []
        service._reconcile_terminal_callback_intents()
        assert len(_callback_rows(store)) == 1
        assert (
            store.insert_terminal_callback_outbox_if_current(
                callback_id=stale_callback_id,
                project_id=project["project_id"],
                worker_id=worker["worker_id"],
                run_id=run["run_id"],
                attempt_number=1,
                event_type="run.completed",
                url="http://callback.local/glasshive",
                payload_json=stale_payload,
                expected_state="completed",
                expected_ended_at=stale_ended_at,
                expected_attempt_id=str(running["active_attempt_id"]),
                expected_result_revision=int(
                    terminal["terminal_result_revision"]
                ),
                expected_result_digest=Store.terminal_result_digest(terminal),
            )
            is None
        )
        assert len(_callback_rows(store)) == 1
    finally:
        service.shutdown()


def test_startup_recovery_is_not_starved_by_mission_executor_survivors(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner-a", "Startup survivor", "Recover callbacks independently", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Survivor worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.local/glasshive",
                "hmac_secret": "synthetic-secret",
            }
        },
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Survivor run", state="completed"
    )
    callback_id = "cb_preexisting_survivor"
    store.insert_callback_outbox_once(
        callback_id=callback_id,
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        attempt_number=None,
        event_type="run.completed",
        url="http://callback.local/glasshive",
        payload_json=_exact_callback_payload(
            callback_id=callback_id,
            event="run.completed",
            worker=worker,
            run=run,
        ),
    )
    release_survivor = Event()
    survivor_started = Event()
    delivered = Event()

    def occupy_mission_executor(self):
        def survivor():
            survivor_started.set()
            release_survivor.wait(5)

        self.executor.submit(survivor)

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        delivered.set()
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr(
        WorkersProjectsService, "reconcile_all_workers", occupy_mission_executor
    )
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)

    service = WorkersProjectsService(
        store, StubRuntime(), max_workers=1, reconcile_on_startup=True
    )
    try:
        assert survivor_started.wait(2)
        assert delivered.wait(2)
        service._startup_recovery_thread.join(timeout=2)
        assert not service._startup_recovery_thread.is_alive()
        record = store.get_callback_outbox(callback_id)
        assert record is not None
        assert record["status"] == "http_accepted"
    finally:
        release_survivor.set()
        service.shutdown()


def test_startup_recovery_does_not_deliver_callbacks_after_shutdown(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project(
        "owner-a", "Startup shutdown", "Do not deliver after shutdown", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Shutdown worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.local/glasshive",
                "hmac_secret": "synthetic-secret",
            }
        },
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Shutdown run", state="completed"
    )
    callback_id = "cb_preexisting_shutdown"
    store.insert_callback_outbox_once(
        callback_id=callback_id,
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        attempt_number=None,
        event_type="run.completed",
        url="http://callback.local/glasshive",
        payload_json=_exact_callback_payload(
            callback_id=callback_id,
            event="run.completed",
            worker=worker,
            run=run,
        ),
    )
    original_startup_recovery = WorkersProjectsService._replay_startup_recovery
    monkeypatch.setattr(
        WorkersProjectsService, "_replay_startup_recovery", lambda _self: None
    )
    delivered: list[str] = []

    def fake_post(url, *, content, headers, timeout):
        _ = content, headers, timeout
        delivered.append(str(url))
        raise AssertionError("shutdown startup recovery must not deliver callbacks")

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "3600")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._shutdown_event.set()
    try:
        original_startup_recovery(service)
        record = store.get_callback_outbox(callback_id)
        assert record is not None
        assert record["status"] == "pending"
        assert record["attempts"] == 0
        assert delivered == []
    finally:
        service.shutdown()
