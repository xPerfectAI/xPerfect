from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timezone
from threading import Event, Thread

import pytest

from workers_projects_runtime import recurrence as recurrence_module
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.control_plane import ControlPlaneConflict, ControlPlaneStore
from workers_projects_runtime.recurrence import (
    due_occurrences_and_next,
    first_occurrence_at,
    normalize_recurrence_spec,
    resolve_local_occurrence,
)
from workers_projects_runtime.scheduling_owner import (
    SchedulingOwnerError,
    SchedulingOwnerIdentity,
    ViventiumSchedulingOwnerClient,
)
from workers_projects_runtime.service import (
    ScheduleActionRequiredError,
    SchedulePrincipalAuthorityError,
    WorkersProjectsService,
)
from workers_projects_runtime.store import (
    RunRestorationState,
    Store,
    WorkerClosedStoreError,
)


def test_delegated_owner_client_uses_scoped_identity_and_internal_route():
    observed = {}

    def request(url, headers, body, timeout):
        observed.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
        return 200, {"result": []}

    client = ViventiumSchedulingOwnerClient(
        owner_url="https://scheduler.example.invalid/mcp",
        scheduler_secret="synthetic-secret",
        timeout_seconds=7,
        request=request,
    )
    result = client.call(
        "list",
        {"limit": 5},
        identity=SchedulingOwnerIdentity(
            tenant_id="tenant-one",
            owner_id="owner-one@example.invalid",
            agent_id="agent-one",
        ),
    )

    assert result == []
    assert observed["url"] == "https://scheduler.example.invalid/internal/glasshive/recurring-schedules"
    assert observed["headers"]["X-Viventium-Scheduler-Secret"] == "synthetic-secret"
    assert observed["headers"]["X-Viventium-Tenant-Id"] == "tenant-one"
    assert observed["headers"]["X-Viventium-User-Id"] == "owner-one@example.invalid"
    assert observed["body"]["owner_id"] == "owner-one@example.invalid"
    assert observed["timeout"] == 7


def test_delegated_owner_client_rejects_non_loopback_plain_http():
    client = ViventiumSchedulingOwnerClient(
        owner_url="http://scheduler.example.invalid/mcp",
        scheduler_secret="synthetic-secret",
    )

    with pytest.raises(SchedulingOwnerError, match="HTTPS or exact loopback HTTP"):
        client.call(
            "list",
            {},
            identity=SchedulingOwnerIdentity(tenant_id="tenant-one", owner_id="owner-one"),
        )


def _worker(store: Store, *, tenant_id: str = "tenant-one", owner_id: str = "owner-one") -> dict:
    project = store.create_project(
        owner_id,
        "Recurring work",
        "Run a synthetic recurring task.",
        "codex-cli",
        tenant_id=tenant_id,
    )
    return store.create_worker(
        project_id=project["project_id"],
        owner_id=owner_id,
        tenant_id=tenant_id,
        name="Recurring worker",
        role="operator",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="stub/codex-cli",
    )


def test_legacy_one_shot_schedule_contract_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)

        scheduled = service.schedule_run(
            worker["worker_id"],
            "Run once.",
            run_at="2027-01-02T03:04:05+00:00",
            schedule_text="once",
        )

        assert set(scheduled) == {
            "schedule_id",
            "project_id",
            "worker_id",
            "tenant_id",
            "owner_id",
            "instruction",
            "schedule_text",
            "run_at",
            "state",
            "queued_run_id",
            "last_error",
            "created_at",
            "updated_at",
        }
        assert scheduled["run_at"] == "2027-01-02T03:04:05+00:00"
        assert scheduled["state"] == "pending"
    finally:
        service.shutdown()


def test_missing_work_ai_fails_claimed_one_shot_and_recurring_schedules_without_runs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(str(tmp_path / "runtime.db")),
        reconcile_on_startup=False,
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        worker = _worker(store)
        one_shot = service.schedule_run(
            worker["worker_id"],
            "Run once after setup.",
            run_at="2026-03-09T14:00:00+00:00",
        )
        first = service.process_due_schedules_once(now_iso="2026-03-09T14:30:00+00:00")
        assert first[0]["schedule_id"] == one_shot["schedule_id"]
        assert first[0]["state"] == "failed"
        assert "Work AI is not set up" in first[0]["last_error"]
        assert store.list_runs_for_worker(worker["worker_id"]) == []
        assert service.process_due_schedules_once(
            now_iso="2026-03-09T14:31:00+00:00"
        ) == []

        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run the recurring report after setup.",
            recurrence_type="daily",
            local_time="09:00",
            timezone_name="America/New_York",
            first_run_at="2026-03-10T13:00:00+00:00",
            schedule_text="daily at 9 AM",
        )
        recurring = service.process_due_schedules_once(
            now_iso="2026-03-10T13:30:00+00:00"
        )
        failed = next(item for item in recurring if item["worker_id"] == worker["worker_id"])
        assert failed["state"] == "failed"
        assert "Work AI is not set up" in failed["last_error"]
        occurrence = store.recurring_occurrence_for_schedule(failed["schedule_id"])
        assert occurrence is not None
        assert occurrence["state"] == "failed"
        assert occurrence["outcome"] == "failed"
        assert store.list_runs_for_worker(worker["worker_id"]) == []
        assert store.get_recurring_schedule_definition(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )["active"] is True
    finally:
        service.shutdown()


def test_native_daily_recurrence_materializes_latest_occurrence_once_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = tmp_path / "runtime.db"
    # The explicit due-cycle below is under test; the background scheduler must not race it.
    store = Store(str(db_path))
    service = WorkersProjectsService(store, StubRuntime(), start_background_consumers=False)
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Create the daily synthetic report.",
            recurrence_type="daily",
            local_time="09:00",
            timezone_name="America/New_York",
            dst_policy="next_valid_earliest",
            first_run_at="2026-03-06T14:00:00+00:00",
            schedule_text="daily at 9 AM",
        )

        # Keep this persistence test deterministic: exercise the real atomic
        # schedule-to-run reservation while leaving execution to the separate
        # worker-processor tests.
        service._ensure_worker_processor = lambda worker_id: None  # type: ignore[method-assign]

        first = service.process_due_schedules_once(now_iso="2026-03-09T14:30:00+00:00")
        second = service.process_due_schedules_once(now_iso="2026-03-09T14:30:00+00:00")

        assert len(first) == 1
        assert second == []
        occurrences = store.list_recurring_schedule_occurrences(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert len(occurrences) == 1
        assert occurrences[0]["scheduled_for"] == "2026-03-09T13:00:00+00:00"
        assert occurrences[0]["state"] == "queued"
        assert occurrences[0]["scheduled_run_id"] == first[0]["schedule_id"]

        listed = service.list_recurring_schedules(
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert listed[0]["last_occurrence_at"] == "2026-03-09T13:00:00+00:00"
        assert listed[0]["last_outcome"] in {"queued", "completed"}
        assert listed[0]["last_error"] == ""
        fetched = service.get_recurring_schedule(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert fetched is not None
        assert fetched["last_outcome"] in {"queued", "completed"}

        refreshed = store.get_recurring_schedule_definition(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert refreshed is not None
        assert refreshed["next_run_at"] == "2026-03-10T13:00:00+00:00"
        assert refreshed["last_occurrence_at"] == "2026-03-09T13:00:00+00:00"
    finally:
        service.shutdown()

    reopened = Store(str(db_path))
    persisted = reopened.get_recurring_schedule_definition(
        definition["definition_id"],
        tenant_id="tenant-one",
        owner_id="owner-one",
    )
    assert persisted is not None
    assert persisted["next_run_at"] == "2026-03-10T13:00:00+00:00"
    assert len(
        reopened.list_recurring_schedule_occurrences(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
    ) == 1


def test_occurrence_identity_is_deterministic_and_stale_claims_are_recoverable(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run the synthetic claim check.",
            recurrence_type="once",
            starts_at="2027-01-02T03:04:05+00:00",
        )

        first = store.materialize_recurring_schedule_occurrence(
            definition["definition_id"],
            expected_next_run_at="2027-01-02T03:04:05+00:00",
            scheduled_for="2027-01-02T03:04:05+00:00",
            next_run_at="2027-01-02T03:04:05+00:00",
            detected_at="2027-01-02T03:04:05+00:00",
            deactivate_after=True,
        )
        second = store.materialize_recurring_schedule_occurrence(
            definition["definition_id"],
            expected_next_run_at="2027-01-02T03:04:05+00:00",
            scheduled_for="2027-01-02T03:04:05+00:00",
            next_run_at="2027-01-02T03:04:05+00:00",
            detected_at="2027-01-02T03:04:05+00:00",
            deactivate_after=True,
        )

        occurrences = store.list_recurring_schedule_occurrences(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
        )
        assert first["schedule_id"] == second["schedule_id"]
        assert len(occurrences) == 1
        occurrence = occurrences[0]
        assert occurrence["occurrence_id"].startswith("occ_")
        assert occurrence["idempotency_key"].startswith("recurrence:")
        assert occurrence["scheduled_for"] == "2027-01-02T03:04:05+00:00"

        claimed = store.claim_recurring_schedule_occurrence(
            occurrence["occurrence_id"],
            claimant="native:test-one",
            now_iso="2027-01-02T03:04:06+00:00",
            lease_seconds=60,
        )
        blocked = store.claim_recurring_schedule_occurrence(
            occurrence["occurrence_id"],
            claimant="native:test-two",
            now_iso="2027-01-02T03:04:30+00:00",
            lease_seconds=60,
        )
        recovered = store.claim_recurring_schedule_occurrence(
            occurrence["occurrence_id"],
            claimant="native:test-two",
            now_iso="2027-01-02T03:06:00+00:00",
            lease_seconds=60,
        )

        assert claimed["attempt_count"] == 1
        assert blocked is None
        assert recovered["attempt_count"] == 2
        assert recovered["claimant"] == "native:test-two"
        assert store.recover_stale_recurring_occurrence_claims(
            "2027-01-02T03:08:00+00:00"
        ) == 1
        reclaimed = store.claim_recurring_schedule_occurrence(
            occurrence["occurrence_id"],
            claimant="native:test-three",
            now_iso="2027-01-02T03:08:01+00:00",
            lease_seconds=60,
        )
        assert reclaimed["attempt_count"] == 3
        assert reclaimed["claimant"] == "native:test-three"
    finally:
        service.shutdown()


def test_recurring_dispatch_atomically_reuses_one_run_after_crash_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run the exactly-once synthetic task.",
            recurrence_type="once",
            starts_at="2027-01-02T03:04:05+00:00",
        )
        schedule = store.materialize_recurring_schedule_occurrence(
            definition["definition_id"],
            expected_next_run_at="2027-01-02T03:04:05+00:00",
            scheduled_for="2027-01-02T03:04:05+00:00",
            next_run_at="2027-01-02T03:04:05+00:00",
            detected_at="2027-01-02T03:04:05+00:00",
            deactivate_after=True,
        )
        assert schedule is not None
        claimed = store.claim_schedule(schedule["schedule_id"])
        assert claimed is not None

        first_run, first_created = store.create_or_get_run_for_schedule(
            schedule["schedule_id"]
        )
        # Simulate an API crash immediately after the durable dispatch transaction.
        recovered = store.recover_stale_recurring_occurrence_claims(
            "2027-01-02T03:20:00+00:00"
        )
        replay_run, replay_created = store.create_or_get_run_for_schedule(
            schedule["schedule_id"]
        )

        assert first_created is True
        assert replay_created is False
        assert replay_run["run_id"] == first_run["run_id"]
        assert recovered == 0
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
        linked = store.get_schedule(schedule["schedule_id"])
        assert linked is not None
        assert linked["state"] == "queued"
        assert linked["queued_run_id"] == first_run["run_id"]
    finally:
        service.shutdown()


def test_scheduled_dispatch_cannot_reserve_run_after_workspace_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Do not run after permanent closure.",
            recurrence_type="once",
            starts_at="2027-01-02T03:04:05+00:00",
        )
        schedule = store.materialize_recurring_schedule_occurrence(
            definition["definition_id"],
            expected_next_run_at="2027-01-02T03:04:05+00:00",
            scheduled_for="2027-01-02T03:04:05+00:00",
            next_run_at="2027-01-02T03:04:05+00:00",
            detected_at="2027-01-02T03:04:05+00:00",
            deactivate_after=True,
        )
        assert schedule is not None
        assert store.claim_schedule(schedule["schedule_id"]) is not None
        store.update_worker_state(worker["worker_id"], "terminated")

        with pytest.raises(WorkerClosedStoreError, match="closed"):
            store.create_or_get_run_for_schedule(schedule["schedule_id"])

        assert store.list_runs_for_worker(worker["worker_id"]) == []
        unlinked = store.get_schedule(schedule["schedule_id"])
        assert unlinked is not None
        assert unlinked["state"] == "running"
        assert unlinked["queued_run_id"] is None
    finally:
        service.shutdown()


def test_recurring_dispatch_rolls_back_run_when_atomic_schedule_link_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Prove the run and occurrence link commit together.",
            recurrence_type="once",
            starts_at="2027-01-02T03:04:05+00:00",
        )
        schedule = store.materialize_recurring_schedule_occurrence(
            definition["definition_id"],
            expected_next_run_at="2027-01-02T03:04:05+00:00",
            scheduled_for="2027-01-02T03:04:05+00:00",
            next_run_at="2027-01-02T03:04:05+00:00",
            detected_at="2027-01-02T03:04:05+00:00",
            deactivate_after=True,
        )
        assert schedule is not None
        assert store.claim_schedule(schedule["schedule_id"]) is not None

        with store._connect() as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_synthetic_schedule_link
                BEFORE UPDATE OF queued_run_id ON scheduled_runs
                WHEN NEW.queued_run_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic schedule link failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="synthetic schedule link failure"):
            store.create_or_get_run_for_schedule(schedule["schedule_id"])

        assert store.list_runs_for_worker(worker["worker_id"]) == []
        unlinked = store.get_schedule(schedule["schedule_id"])
        assert unlinked is not None
        assert unlinked["state"] == "running"
        assert unlinked["queued_run_id"] is None

        with store._connect() as conn:
            conn.execute("DROP TRIGGER fail_synthetic_schedule_link")
        run, created = store.create_or_get_run_for_schedule(schedule["schedule_id"])
        assert created is True
        assert store.get_schedule(schedule["schedule_id"])["queued_run_id"] == run["run_id"]
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
    finally:
        service.shutdown()


def test_interval_recurrence_coalesces_missed_periods_to_latest_due_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    # The explicit due-cycle below is under test; the background scheduler must not race it.
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), start_background_consumers=False)
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run the hourly synthetic check.",
            recurrence_type="interval",
            interval_seconds=3600,
            timezone_name="UTC",
            first_run_at="2026-01-01T00:00:00+00:00",
        )

        def queue_only(worker_id: str, instruction: str, event_type: str = "run.queued") -> dict:
            return store.create_run(worker_id, worker["project_id"], instruction, state="queued")

        service.assign_run = queue_only  # type: ignore[method-assign]
        service.process_due_schedules_once(now_iso="2026-01-01T03:30:00+00:00")

        occurrences = store.list_recurring_schedule_occurrences(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert [item["scheduled_for"] for item in occurrences] == ["2026-01-01T03:00:00+00:00"]
        refreshed = store.get_recurring_schedule_definition(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert refreshed is not None
        assert refreshed["next_run_at"] == "2026-01-01T04:00:00+00:00"
    finally:
        service.shutdown()


def test_bounded_catch_up_with_overlap_skip_dispatches_only_one_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    # The explicit due-cycle below is under test; the background scheduler must not race it.
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), start_background_consumers=False)
    service._ensure_worker_processor = lambda worker_id: None  # type: ignore[method-assign]
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run the bounded synthetic check.",
            recurrence_type="interval",
            interval_seconds=3600,
            timezone_name="UTC",
            first_run_at="2026-01-01T00:00:00+00:00",
            catch_up_policy="bounded",
            max_catch_up_occurrences=3,
            overlap_policy="skip",
        )

        service.process_due_schedules_once(now_iso="2026-01-01T03:30:00+00:00")

        occurrences = store.list_recurring_schedule_occurrences(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
        assert [item["state"] for item in occurrences].count("queued") == 1
        skipped = [item for item in occurrences if item["state"] == "skipped"]
        assert len(skipped) == 2
        assert {item["outcome"] for item in skipped} == {"overlap_skipped"}
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("recurrence_type", "extra", "expected"),
    [
        (
            "once",
            {"starts_at": "2027-01-04T09:00:00-05:00"},
            "2027-01-04T14:00:00+00:00",
        ),
        (
            "cron",
            {"cron_expression": "0 9 * * 1-5", "timezone_name": "America/Toronto"},
            "2027-01-01T14:00:00+00:00",
        ),
        (
            "rfc5545",
            {"rrule": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0", "timezone_name": "America/Toronto"},
            "2027-01-04T14:00:00+00:00",
        ),
    ],
)
def test_structured_once_cron_and_rfc5545_specs_compute_the_next_occurrence(
    recurrence_type,
    extra,
    expected,
):
    now = datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc)
    spec = normalize_recurrence_spec(recurrence_type=recurrence_type, **extra)

    occurrence = first_occurrence_at(spec, now=now, first_run_at=None)

    assert occurrence.isoformat() == expected


def test_explicit_first_run_after_end_is_rejected():
    spec = normalize_recurrence_spec(
        recurrence_type="daily",
        local_time="09:00",
        timezone_name="UTC",
        ends_at="2027-01-02T00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="start/end window"):
        first_occurrence_at(
            spec,
            now=datetime(2027, 1, 1, tzinfo=timezone.utc),
            first_run_at="2027-01-03T09:00:00+00:00",
        )


def test_rfc5545_dtstart_and_count_are_stable_across_restart_calculation():
    spec = normalize_recurrence_spec(
        recurrence_type="rfc5545",
        rrule="FREQ=DAILY;COUNT=2;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
        timezone_name="America/Toronto",
        starts_at="2027-01-01T00:00:00-05:00",
        catch_up_policy="coalesce",
    )
    first = first_occurrence_at(
        spec,
        now=datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    due, following = due_occurrences_and_next(
        {**spec, "next_run_at": first.isoformat()},
        now=datetime(2027, 1, 3, 12, 0, tzinfo=timezone.utc),
    )

    assert first.isoformat() == "2027-01-01T14:00:00+00:00"
    assert [item["scheduled_for"].isoformat() for item in due] == [
        "2027-01-02T14:00:00+00:00"
    ]
    assert following is None


def test_rfc5545_rejects_subminute_and_unbounded_complex_rules():
    with pytest.raises(ValueError, match="at least one minute"):
        normalize_recurrence_spec(
            recurrence_type="rfc5545",
            rrule="FREQ=SECONDLY;INTERVAL=30",
            timezone_name="UTC",
        )

    with pytest.raises(ValueError, match="too complex"):
        normalize_recurrence_spec(
            recurrence_type="rfc5545",
            rrule="FREQ=MINUTELY;BYSECOND=0,15,30,45",
            timezone_name="UTC",
        )
    with pytest.raises(ValueError, match="INTERVAL"):
        normalize_recurrence_spec(
            recurrence_type="rfc5545",
            rrule="FREQ=MINUTELY;INTERVAL=0",
            timezone_name="UTC",
        )
    with pytest.raises(ValueError, match="cron_expression"):
        normalize_recurrence_spec(
            recurrence_type="cron",
            cron_expression="*/30 * * * * *",
            timezone_name="UTC",
        )


def test_long_stale_minutely_rfc5545_uses_direct_latest_due_calculation(monkeypatch):
    definition = {
        **normalize_recurrence_spec(
            recurrence_type="rfc5545",
            rrule="FREQ=MINUTELY;INTERVAL=5",
            timezone_name="UTC",
            starts_at="2020-01-01T00:00:00+00:00",
            catch_up_policy="coalesce",
        ),
        "next_run_at": "2020-01-01T00:00:00+00:00",
    }
    original_next_after = recurrence_module._next_after
    calls = 0

    def bounded_next_after(spec, occurrence):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise AssertionError("latest-due calculation walked stale occurrences linearly")
        return original_next_after(spec, occurrence)

    monkeypatch.setattr(recurrence_module, "_next_after", bounded_next_after)
    due, following = due_occurrences_and_next(
        definition,
        now=datetime(2035, 1, 1, 0, 2, tzinfo=timezone.utc),
    )

    assert calls == 1
    assert [item["scheduled_for"].isoformat() for item in due] == [
        "2035-01-01T00:00:00+00:00"
    ]
    assert following is not None
    assert following.isoformat() == "2035-01-01T00:05:00+00:00"

    calls = 0
    sparse = normalize_recurrence_spec(
        recurrence_type="rfc5545",
        rrule="FREQ=MINUTELY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0",
        timezone_name="UTC",
        starts_at="2020-01-01T00:00:00+00:00",
        catch_up_policy="coalesce",
    )
    sparse_due, sparse_following = due_occurrences_and_next(
        {**sparse, "next_run_at": "2020-01-01T00:00:00+00:00"},
        now=datetime(2035, 7, 1, tzinfo=timezone.utc),
    )
    assert [item["scheduled_for"].isoformat() for item in sparse_due] == [
        "2035-01-01T00:00:00+00:00"
    ]
    assert sparse_following is not None
    assert sparse_following.isoformat() == "2036-01-01T00:00:00+00:00"

    calls = 0
    monthly = normalize_recurrence_spec(
        recurrence_type="rfc5545",
        rrule="FREQ=MONTHLY",
        timezone_name="UTC",
        starts_at="2020-01-15T09:30:00+00:00",
        catch_up_policy="coalesce",
    )
    monthly_due, monthly_following = due_occurrences_and_next(
        {**monthly, "next_run_at": "2020-01-15T09:30:00+00:00"},
        now=datetime(2035, 7, 20, tzinfo=timezone.utc),
    )
    assert [item["scheduled_for"].isoformat() for item in monthly_due] == [
        "2035-07-15T09:30:00+00:00"
    ]
    assert monthly_following is not None
    assert monthly_following.isoformat() == "2035-08-15T09:30:00+00:00"

    month_end = normalize_recurrence_spec(
        recurrence_type="rfc5545",
        rrule="FREQ=MONTHLY",
        timezone_name="UTC",
        starts_at="2020-01-31T15:37:00+00:00",
        catch_up_policy="coalesce",
    )
    month_end_due, month_end_following = due_occurrences_and_next(
        {**month_end, "next_run_at": "2020-01-31T15:37:00+00:00"},
        now=datetime(2035, 4, 1, tzinfo=timezone.utc),
    )
    assert [item["scheduled_for"].isoformat() for item in month_end_due] == [
        "2035-03-31T15:37:00+00:00"
    ]
    assert month_end_following is not None
    assert month_end_following.isoformat() == "2035-05-31T15:37:00+00:00"


def test_structured_recurrence_defaults_skip_misfires_and_requires_explicit_bounded_catch_up():
    base = {
        "recurrence_type": "interval",
        "interval_seconds": 3600,
        "timezone_name": "UTC",
        "next_run_at": "2027-01-01T00:00:00+00:00",
        "ends_at": None,
        "misfire_grace_seconds": 300,
        "overlap_policy": "skip",
    }
    now = datetime(2027, 1, 1, 3, 30, tzinfo=timezone.utc)

    skipped, next_after_skip = due_occurrences_and_next(
        {**base, "catch_up_policy": "skip", "max_catch_up_occurrences": 1},
        now=now,
    )
    bounded, next_after_catch_up = due_occurrences_and_next(
        {**base, "catch_up_policy": "bounded", "max_catch_up_occurrences": 2},
        now=now,
    )

    assert [(item["scheduled_for"].isoformat(), item["outcome"]) for item in skipped] == [
        ("2027-01-01T03:00:00+00:00", "misfire_skipped")
    ]
    assert next_after_skip.isoformat() == "2027-01-01T04:00:00+00:00"
    assert [item["scheduled_for"].isoformat() for item in bounded] == [
        "2027-01-01T02:00:00+00:00",
        "2027-01-01T03:00:00+00:00",
    ]
    assert all(item["outcome"] == "pending" for item in bounded)
    assert next_after_catch_up.isoformat() == "2027-01-01T04:00:00+00:00"


def test_native_fire_time_revalidates_owner_workspace_and_required_provider_account(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    control_plane = ControlPlaneStore(db_path)
    account = control_plane.create_provider_account(
        tenant_id="tenant-one",
        owner_id="owner-one",
        provider="codex",
        label="Synthetic Codex account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://synthetic-account",
        status="action_required",
    )
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=control_plane,
    )
    try:
        worker = _worker(store)
        store.update_worker(
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
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run only after account revalidation.",
            recurrence_type="interval",
            interval_seconds=3600,
            first_run_at="2027-01-02T03:04:05+00:00",
        )

        with pytest.raises(ScheduleActionRequiredError) as action_required:
            service.run_recurring_schedule_now(
                definition["definition_id"],
                tenant_id="tenant-one",
                owner_id="owner-one",
                idempotency_token="manual-public-safe-action-required",
            )
        assert action_required.value.failure_class == "provider_account_reconnect_required"
        assert "Open Connections" in action_required.value.recovery

        assert service.process_due_schedules_once(now_iso="2027-01-02T03:04:05+00:00") == []
        occurrences = store.list_recurring_schedule_occurrences(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
        )
        assert occurrences[0]["state"] == "action_required"
        assert "reconnected" in occurrences[0]["outcome"]
        assert occurrences[0]["queued_run_id"] is None
        paused = store.get_recurring_schedule_definition(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
        )
        assert paused is not None
        assert paused["active"] is False
        assert paused["enabled"] is False

        assert service.process_due_schedules_once(now_iso="2027-01-03T03:04:05+00:00") == []
        assert len(
            store.list_recurring_schedule_occurrences(
                definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
            )
        ) == 1
    finally:
        service.shutdown()


def test_recurring_occurrence_is_retryable_when_user_concurrency_is_full(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setenv("GLASSHIVE_MAX_CONCURRENT_RECURRING_RUNS_PER_USER", "1")
    # The explicit due-cycle below is under test; the background scheduler must not race it.
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), start_background_consumers=False)
    try:
        worker = _worker(store)
        store.create_run(
            worker["worker_id"],
            worker["project_id"],
            "Existing synthetic run.",
            state=RunRestorationState.RUNNING,
        )
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Wait for user capacity.",
            recurrence_type="once",
            starts_at="2027-01-02T03:04:05+00:00",
            overlap_policy="queue",
        )

        assert service.process_due_schedules_once(now_iso="2027-01-02T03:04:05+00:00") == []
        occurrences = store.list_recurring_schedule_occurrences(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
        )
        assert occurrences[0]["state"] == "retryable"
        assert occurrences[0]["outcome"] == "user_concurrency_deferred"
        assert occurrences[0]["queued_run_id"] is None
    finally:
        service.shutdown()


def test_daily_recurrence_has_explicit_spring_forward_and_fall_back_policy():
    spring = resolve_local_occurrence(
        date(2026, 3, 8),
        time(2, 30),
        timezone_name="America/New_York",
        dst_policy="next_valid_earliest",
    )
    fall_first = resolve_local_occurrence(
        date(2026, 11, 1),
        time(1, 30),
        timezone_name="America/New_York",
        dst_policy="next_valid_earliest",
    )
    fall_second = resolve_local_occurrence(
        date(2026, 11, 1),
        time(1, 30),
        timezone_name="America/New_York",
        dst_policy="next_valid_latest",
    )

    assert spring.isoformat() == "2026-03-08T07:00:00+00:00"
    assert fall_first.isoformat() == "2026-11-01T05:30:00+00:00"
    assert fall_second.isoformat() == "2026-11-01T06:30:00+00:00"


@pytest.mark.parametrize(
    ("dst_policy", "expected_fall_occurrence"),
    [
        ("next_valid_earliest", "2026-11-01T05:30:00+00:00"),
        ("next_valid_latest", "2026-11-01T06:30:00+00:00"),
    ],
)
def test_cron_fall_back_has_one_wall_clock_occurrence(dst_policy, expected_fall_occurrence):
    definition = {
        "recurrence_type": "cron",
        "cron_expression": "30 1 * * *",
        "timezone_name": "America/New_York",
        "dst_policy": dst_policy,
        "enabled": True,
        "overlap_policy": "queue",
        "misfire_grace_seconds": 86400,
        "catch_up_policy": "bounded",
        "max_catch_up_occurrences": 10,
        "jitter_seconds": 0,
        "starts_at": "2026-10-31T00:00:00+00:00",
        "ends_at": None,
        "next_run_at": "2026-10-31T05:30:00+00:00",
    }

    due, next_run = due_occurrences_and_next(
        definition,
        now=datetime.fromisoformat("2026-11-02T07:00:00+00:00"),
    )
    scheduled = [item["scheduled_for"].isoformat() for item in due]

    assert scheduled.count(expected_fall_occurrence) == 1
    assert not ({"2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00"} <= set(scheduled))
    assert next_run is not None
    assert next_run.isoformat() == "2026-11-03T06:30:00+00:00"


def test_cron_spring_forward_advances_once_to_first_valid_wall_time():
    definition = {
        "recurrence_type": "cron",
        "cron_expression": "30 2 * * *",
        "timezone_name": "America/New_York",
        "dst_policy": "next_valid_earliest",
        "enabled": True,
        "overlap_policy": "queue",
        "misfire_grace_seconds": 86400,
        "catch_up_policy": "bounded",
        "max_catch_up_occurrences": 10,
        "jitter_seconds": 0,
        "starts_at": "2026-03-07T00:00:00+00:00",
        "ends_at": None,
        "next_run_at": "2026-03-07T07:30:00+00:00",
    }

    due, next_run = due_occurrences_and_next(
        definition,
        now=datetime.fromisoformat("2026-03-09T08:00:00+00:00"),
    )

    assert [item["scheduled_for"].isoformat() for item in due] == [
        "2026-03-07T07:30:00+00:00",
        "2026-03-08T07:00:00+00:00",
        "2026-03-09T06:30:00+00:00",
    ]
    assert next_run is not None
    assert next_run.isoformat() == "2026-03-10T06:30:00+00:00"


def test_viventium_deployment_delegates_definition_without_local_row_or_native_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "scheduling_cortex")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))

    class OwnerClient:
        def __init__(self) -> None:
            self.definition = None

        def call(self, action, payload, *, identity):
            assert identity.tenant_id == "tenant-one"
            assert identity.owner_id == "owner-one"
            if action == "create":
                self.definition = {
                    **payload,
                    "tenant_id": identity.tenant_id,
                    "owner_id": identity.owner_id,
                    "scheduler_owner": "viventium_cortex",
                    "schedule_owner": "viventium_cortex",
                    "owner_action": "dispatch_via_viventium_cortex",
                    "active": True,
                    "created_at": "2027-01-01T00:00:00+00:00",
                    "updated_at": "2027-01-01T00:00:00+00:00",
                    "last_occurrence_at": None,
                    "retired_at": None,
                }
                return self.definition
            if action == "list":
                return [self.definition]
            raise AssertionError(action)

    owner_client = OwnerClient()
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        scheduling_owner_client=owner_client,
    )
    try:
        worker = _worker(store)

        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run every hour.",
            recurrence_type="interval",
            interval_seconds=3600,
            timezone_name="UTC",
            first_run_at="2027-01-02T03:04:05+00:00",
        )

        assert definition["schedule_owner"] == "viventium_cortex"
        assert definition["owner_action"] == "dispatch_via_viventium_cortex"
        assert store.list_recurring_schedule_definitions(
            worker["worker_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        ) == []
        listed = service.list_recurring_schedules(
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert [item["definition_id"] for item in listed] == [definition["definition_id"]]
        assert listed[0]["workspace_name"] == "Recurring worker"
        assert service.process_due_schedules_once(now_iso="2027-01-02T04:00:00+00:00") == []
        assert store.list_recurring_schedule_occurrences(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
        ) == []
        one_shot = service.schedule_run(
            worker["worker_id"],
            "Run once.",
            run_at="2027-01-02T03:04:05+00:00",
        )
        assert one_shot["state"] == "pending"
    finally:
        service.shutdown()


def test_delegated_creation_compensates_when_principal_is_disabled_during_owner_call(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "scheduling_cortex")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))

    class OwnerClient:
        def __init__(self) -> None:
            self.actions = []

        def call(self, action, payload, *, identity):
            self.actions.append(action)
            if action == "create":
                store.set_schedule_principal_authority(
                    tenant_id=identity.tenant_id,
                    owner_id=identity.owner_id,
                    enabled=False,
                )
                return {
                    **payload,
                    "tenant_id": identity.tenant_id,
                    "owner_id": identity.owner_id,
                    "active": True,
                }
            if action == "deactivate":
                return {**payload, "active": False}
            raise AssertionError(action)

    owner_client = OwnerClient()
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        scheduling_owner_client=owner_client,
    )
    try:
        worker = _worker(store)

        with pytest.raises(SchedulePrincipalAuthorityError, match="disabled"):
            service.create_recurring_schedule(
                worker["worker_id"],
                "Do not leave this delegated definition active.",
                recurrence_type="interval",
                interval_seconds=3600,
                timezone_name="UTC",
                first_run_at="2027-01-02T03:04:05+00:00",
            )

        assert owner_client.actions == ["create", "deactivate"]
        assert store.list_recurring_schedule_definitions(
            worker["worker_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        ) == []
    finally:
        service.shutdown()


def test_delegated_creation_compensates_when_workspace_closes_during_owner_call(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "scheduling_cortex")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))

    class OwnerClient:
        def __init__(self) -> None:
            self.actions = []

        def call(self, action, payload, *, identity):
            self.actions.append(action)
            if action == "create":
                worker = store.list_all_workers()[0]
                store.update_worker_state(worker["worker_id"], "terminating")
                return {
                    **payload,
                    "tenant_id": identity.tenant_id,
                    "owner_id": identity.owner_id,
                    "active": True,
                }
            if action == "deactivate":
                return {**payload, "active": False}
            raise AssertionError(action)

    owner_client = OwnerClient()
    service = WorkersProjectsService(store, StubRuntime(), scheduling_owner_client=owner_client)
    try:
        worker = _worker(store)

        with pytest.raises(ControlPlaneConflict, match="closed"):
            service.create_recurring_schedule(
                worker["worker_id"],
                "Do not leave this delegated definition active.",
                recurrence_type="interval",
                interval_seconds=3600,
                timezone_name="UTC",
                first_run_at="2027-01-02T03:04:05+00:00",
            )

        assert owner_client.actions == ["create", "deactivate"]
    finally:
        service.shutdown()


def test_delegated_creation_cleanup_failure_after_close_is_durable_and_retryable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "scheduling_cortex")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))

    class OwnerClient:
        def __init__(self) -> None:
            self.create_started = Event()
            self.release_create = Event()
            self.fail_deactivate = True
            self.active: set[str] = set()

        def call(self, action, payload, *, identity):
            _ = identity
            if action == "create":
                self.create_started.set()
                assert self.release_create.wait(timeout=3)
                self.active.add(str(payload["definition_id"]))
                return {**payload, "active": True}
            if action == "list":
                return [
                    {
                        "definition_id": definition_id,
                        "worker_id": payload["worker_id"],
                        "active": True,
                    }
                    for definition_id in sorted(self.active)
                ]
            if action == "deactivate":
                if self.fail_deactivate:
                    raise RuntimeError("synthetic compensation unavailable")
                self.active.discard(str(payload["definition_id"]))
                return {"definition_id": payload["definition_id"], "active": False}
            raise AssertionError(action)

    owner_client = OwnerClient()
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        scheduling_owner_client=owner_client,
    )
    worker = _worker(store)
    errors: list[BaseException] = []

    def create() -> None:
        try:
            service.create_recurring_schedule(
                worker["worker_id"],
                "Do not survive workspace closure.",
                recurrence_type="interval",
                interval_seconds=3600,
                timezone_name="UTC",
                first_run_at="2027-01-02T03:04:05+00:00",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    create_thread = Thread(target=create)
    create_thread.start()
    try:
        assert owner_client.create_started.wait(timeout=2)
        assert service.terminate_worker(worker["worker_id"])["state"] == "terminated"
        owner_client.release_create.set()
        create_thread.join(timeout=3)

        assert not create_thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ControlPlaneConflict)
        assert owner_client.active
        assert store.get_worker(worker["worker_id"])["state"] == "termination_failed"

        owner_client.fail_deactivate = False
        assert service.terminate_worker(worker["worker_id"])["state"] == "terminated"
        assert owner_client.active == set()
    finally:
        owner_client.release_create.set()
        create_thread.join(timeout=1)
        service.shutdown()


def test_close_deactivates_all_delegated_workspace_schedules(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "scheduling_cortex")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))

    class OwnerClient:
        def __init__(self) -> None:
            self.active = {"rsd_one", "rsd_two"}
            self.actions: list[tuple[str, str]] = []

        def call(self, action, payload, *, identity):
            self.actions.append((action, str(payload.get("definition_id") or "")))
            if action == "list":
                return [
                    {"definition_id": definition_id, "worker_id": payload["worker_id"], "active": True}
                    for definition_id in sorted(self.active)
                ]
            if action == "deactivate":
                self.active.discard(str(payload["definition_id"]))
                return {"definition_id": payload["definition_id"], "active": False}
            raise AssertionError(action)

    owner_client = OwnerClient()
    service = WorkersProjectsService(store, StubRuntime(), scheduling_owner_client=owner_client)
    try:
        worker = _worker(store)
        closed = service.terminate_worker(worker["worker_id"])

        assert closed["state"] == "terminated"
        assert owner_client.active == set()
        assert owner_client.actions == [
            ("list", ""),
            ("deactivate", "rsd_one"),
            ("deactivate", "rsd_two"),
        ]
    finally:
        service.shutdown()


def test_delegated_close_cleanup_failure_stays_closed_and_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "scheduling_cortex")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))

    class OwnerClient:
        def __init__(self) -> None:
            self.fail = True
            self.active = {"rsd_retry"}

        def call(self, action, payload, *, identity):
            if self.fail:
                raise RuntimeError("synthetic delegated owner unavailable")
            if action == "list":
                return [
                    {"definition_id": definition_id, "worker_id": payload["worker_id"], "active": True}
                    for definition_id in sorted(self.active)
                ]
            if action == "deactivate":
                self.active.discard(str(payload["definition_id"]))
                return {"definition_id": payload["definition_id"], "active": False}
            raise AssertionError(action)

    owner_client = OwnerClient()
    service = WorkersProjectsService(store, StubRuntime(), scheduling_owner_client=owner_client)
    try:
        worker = _worker(store)
        with pytest.raises(RuntimeError, match="synthetic delegated owner unavailable"):
            service.terminate_worker(worker["worker_id"])
        assert store.get_worker(worker["worker_id"])["state"] == "termination_failed"
        with pytest.raises(ControlPlaneConflict, match="closed"):
            service.assign_run(worker["worker_id"], "Do not run while cleanup is pending.")

        owner_client.fail = False
        retried = service.terminate_worker(worker["worker_id"])
        assert retried["state"] == "terminated"
        assert owner_client.active == set()
    finally:
        service.shutdown()


def test_standalone_multi_user_deployment_can_select_native_owner_with_fire_revalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(db_path),
    )
    try:
        assert service.recurring_schedule_owner() == "glasshive_native"
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Do not run after this principal is disabled.",
            recurrence_type="interval",
            interval_seconds=3600,
            timezone_name="UTC",
            first_run_at="2027-01-02T03:00:00+00:00",
        )
        one_shot = service.schedule_run(
            worker["worker_id"],
            "This pending one-shot must also be cancelled.",
            run_at="2027-01-02T03:00:00+00:00",
        )

        disabled = service.set_schedule_principal_authority(
            tenant_id="tenant-one",
            owner_id="owner-one",
            enabled=False,
        )

        assert disabled["enabled"] is False
        assert disabled["deactivated_native_definitions"] == 1
        assert disabled["cancelled_native_occurrences"] == 1
        assert store.get_recurring_schedule_definition(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )["active"] is False
        assert store.get_schedule(one_shot["schedule_id"])["state"] == "cancelled"
        assert service.process_due_schedules_once(now_iso="2027-01-02T04:00:00+00:00") == []
        with store._connect() as connection:
            authority_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(schedule_principal_authority)"
                ).fetchall()
            }
        assert authority_columns == {
            "tenant_id",
            "owner_id",
            "enabled",
            "authority_epoch",
            "updated_at",
        }
    finally:
        service.shutdown()


def test_one_shot_rechecks_disabled_principal_after_claim_before_run_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(db_path),
    )
    try:
        worker = _worker(store)
        scheduled = service.schedule_run(
            worker["worker_id"],
            "Recheck authority after the scheduler claim.",
            run_at="2027-01-02T03:00:00+00:00",
        )
        assert store.get_schedule_principal_authority(
            tenant_id="tenant-one",
            owner_id="owner-one",
        )["enabled"] is True
        claimed = store.claim_schedule(scheduled["schedule_id"])
        assert claimed is not None

        store.set_schedule_principal_authority(
            tenant_id="tenant-one",
            owner_id="owner-one",
            enabled=False,
        )

        with pytest.raises(SchedulePrincipalAuthorityError, match="disabled"):
            service.assign_scheduled_run(claimed)
        assert store.list_runs_for_worker(worker["worker_id"]) == []
    finally:
        service.shutdown()


def test_one_shot_disable_after_service_revalidation_cannot_reserve_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-standard-openai-key")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(db_path),
    )
    try:
        worker = _worker(store)
        scheduled = service.schedule_run(
            worker["worker_id"],
            "Do not reserve after the final authority check races disablement.",
            run_at="2027-01-02T03:00:00+00:00",
        )
        claimed = store.claim_schedule(scheduled["schedule_id"])
        assert claimed is not None
        original_runtime_check = service._ensure_runtime_available

        def disable_after_revalidation(profile, execution_mode):
            original_runtime_check(profile, execution_mode)
            store.set_schedule_principal_authority(
                tenant_id="tenant-one",
                owner_id="owner-one",
                enabled=False,
            )

        service._ensure_runtime_available = disable_after_revalidation  # type: ignore[method-assign]

        with pytest.raises(SchedulePrincipalAuthorityError, match="disabled"):
            service.assign_scheduled_run(claimed)
        assert store.list_runs_for_worker(worker["worker_id"]) == []
        assert store.get_schedule(scheduled["schedule_id"])["state"] == "cancelled"
    finally:
        service.shutdown()


def test_manual_run_now_disable_after_revalidation_cannot_create_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(db_path),
    )
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Do not materialize a manual occurrence after disablement.",
            recurrence_type="interval",
            interval_seconds=3600,
            timezone_name="UTC",
            first_run_at="2027-01-02T03:00:00+00:00",
        )
        original_revalidate = service._revalidate_recurring_schedule_fire

        def disable_after_revalidation(candidate):
            result = original_revalidate(candidate)
            store.set_schedule_principal_authority(
                tenant_id="tenant-one",
                owner_id="owner-one",
                enabled=False,
            )
            return result

        service._revalidate_recurring_schedule_fire = disable_after_revalidation  # type: ignore[method-assign]

        with pytest.raises(SchedulePrincipalAuthorityError, match="disabled"):
            service.run_recurring_schedule_now(
                definition["definition_id"],
                tenant_id="tenant-one",
                owner_id="owner-one",
                idempotency_token="manual-disable-race",
            )
        assert store.list_schedules_for_worker(worker["worker_id"]) == []
    finally:
        service.shutdown()


def test_manual_run_now_losing_race_to_workspace_close_creates_no_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Do not materialize after closure wins.",
            recurrence_type="interval",
            interval_seconds=3600,
            first_run_at="2027-01-02T03:04:05+00:00",
        )
        original_create = store.create_recurring_schedule_run_now

        def close_then_create(*args, **kwargs):
            store.update_worker_state(worker["worker_id"], "terminating")
            return original_create(*args, **kwargs)

        monkeypatch.setattr(store, "create_recurring_schedule_run_now", close_then_create)

        with pytest.raises(ControlPlaneConflict, match="closed"):
            service.run_recurring_schedule_now(
                definition["definition_id"],
                tenant_id="tenant-one",
                owner_id="owner-one",
                idempotency_token="manual-close-race",
            )

        assert store.list_schedules_for_worker(worker["worker_id"], include_done=True) == []
        assert store.list_recurring_schedule_occurrences(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
        ) == []
    finally:
        service.shutdown()


def test_schedule_enable_loses_cleanly_to_concurrent_principal_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(db_path),
    )
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Keep this definition inactive after a disable race.",
            recurrence_type="interval",
            interval_seconds=3600,
            timezone_name="UTC",
            first_run_at="2027-01-02T03:00:00+00:00",
        )
        service.deactivate_recurring_schedule(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        original_require = service._require_schedule_principal_authority

        def disable_after_check(*, tenant_id, owner_id, establish):
            authority = original_require(
                tenant_id=tenant_id,
                owner_id=owner_id,
                establish=establish,
            )
            store.set_schedule_principal_authority(
                tenant_id=tenant_id,
                owner_id=owner_id,
                enabled=False,
            )
            return authority

        service._require_schedule_principal_authority = disable_after_check  # type: ignore[method-assign]

        with pytest.raises(SchedulePrincipalAuthorityError, match="disabled"):
            service.update_recurring_schedule(
                definition["definition_id"],
                tenant_id="tenant-one",
                owner_id="owner-one",
                updates={"enabled": True},
            )
        current = store.get_recurring_schedule_definition(
            definition["definition_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        )
        assert current is not None
        assert current["enabled"] is False
        assert current["active"] is False
    finally:
        service.shutdown()


def test_principal_disable_cancels_already_linked_queued_schedule_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(db_path),
    )
    try:
        worker = _worker(store)
        scheduled = service.schedule_run(
            worker["worker_id"],
            "Cancel this queued scheduled work when authority is revoked.",
            run_at="2027-01-02T03:00:00+00:00",
        )
        assert store.claim_schedule(scheduled["schedule_id"]) is not None
        run, created = store.create_or_get_run_for_schedule(
            scheduled["schedule_id"],
            require_principal_authority=True,
        )
        assert created is True
        ordinary = store.create_run(
            worker["worker_id"],
            worker["project_id"],
            "An interactive run is outside schedule revocation scope.",
        )

        service.set_schedule_principal_authority(
            tenant_id="tenant-one",
            owner_id="owner-one",
            enabled=False,
        )

        assert store.get_schedule(scheduled["schedule_id"])["state"] == "cancelled"
        assert store.get_run(run["run_id"])["state"] == "cancelled"
        assert store.get_run(ordinary["run_id"])["state"] == "queued"
    finally:
        service.shutdown()


def test_one_shot_creation_loses_cleanly_to_concurrent_principal_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "glasshive_native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    db_path = str(tmp_path / "runtime.db")
    store = Store(db_path)
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        control_plane_store=ControlPlaneStore(db_path),
    )
    try:
        worker = _worker(store)
        original_require = service._require_schedule_principal_authority

        def disable_after_check(*, tenant_id, owner_id, establish):
            authority = original_require(
                tenant_id=tenant_id,
                owner_id=owner_id,
                establish=establish,
            )
            store.set_schedule_principal_authority(
                tenant_id=tenant_id,
                owner_id=owner_id,
                enabled=False,
            )
            return authority

        service._require_schedule_principal_authority = disable_after_check  # type: ignore[method-assign]

        with pytest.raises(SchedulePrincipalAuthorityError, match="disabled"):
            service.schedule_run(
                worker["worker_id"],
                "Do not persist this schedule after disablement.",
                run_at="2027-01-02T03:00:00+00:00",
            )
        assert store.list_schedules_for_worker(worker["worker_id"]) == []
    finally:
        service.shutdown()


def test_viventium_marker_rejects_conflicting_native_recurrence_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/callback",
    )
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    # The explicit due-cycle below is under test; the background scheduler must not race it.
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), start_background_consumers=False)
    try:
        worker = _worker(store)
        with pytest.raises(ValueError, match="delegates recurrence to its configured host scheduler"):
            service.create_recurring_schedule(
                worker["worker_id"],
                "Run every hour.",
                recurrence_type="interval",
                interval_seconds=3600,
                timezone_name="UTC",
            )
        assert store.list_recurring_schedule_definitions(
            worker["worker_id"], tenant_id="tenant-one", owner_id="owner-one"
        ) == []

        one_shot = service.schedule_run(
            worker["worker_id"],
            "Run once despite recurrence misconfiguration.",
            run_at="2026-01-01T00:00:00+00:00",
        )

        def queue_only(worker_id: str, instruction: str, event_type: str = "run.queued") -> dict:
            return store.create_run(worker_id, worker["project_id"], instruction, state="queued")

        service.assign_run = queue_only  # type: ignore[method-assign]
        processed = service.process_due_schedules_once(now_iso="2026-01-01T00:01:00+00:00")
        assert [item["schedule_id"] for item in processed] == [one_shot["schedule_id"]]
    finally:
        service.shutdown()


def test_invalid_persisted_recurrence_does_not_block_legacy_one_shot_firing(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    # The explicit due-cycle below is under test; the background scheduler must not race it.
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), start_background_consumers=False)
    try:
        worker = _worker(store)
        store.create_recurring_schedule_definition(
            worker_id=worker["worker_id"],
            project_id=worker["project_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
            scheduler_owner="native",
            instruction="Invalid persisted recurrence.",
            schedule_text="invalid",
            recurrence_type="daily",
            interval_seconds=None,
            local_time="99:00",
            timezone_name="UTC",
            dst_policy="next_valid_earliest",
            next_run_at="2026-01-01T00:00:00+00:00",
        )
        one_shot = service.schedule_run(
            worker["worker_id"],
            "Run the unaffected one-shot task.",
            run_at="2026-01-01T00:00:00+00:00",
        )

        def queue_only(worker_id: str, instruction: str, event_type: str = "run.queued") -> dict:
            return store.create_run(worker_id, worker["project_id"], instruction, state="queued")

        service.assign_run = queue_only  # type: ignore[method-assign]
        processed = service.process_due_schedules_once(now_iso="2026-01-01T00:01:00+00:00")

        assert [item["schedule_id"] for item in processed] == [one_shot["schedule_id"]]
        assert "Skipped invalid recurring schedule definition" in caplog.text
    finally:
        service.shutdown()


def test_recurring_definitions_and_occurrences_are_owner_scoped_and_deactivatable(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)
        definition = service.create_recurring_schedule(
            worker["worker_id"],
            "Run daily.",
            recurrence_type="daily",
            local_time="09:00",
            timezone_name="UTC",
            first_run_at="2027-01-02T09:00:00+00:00",
        )

        assert store.get_recurring_schedule_definition(
            definition["definition_id"], tenant_id="tenant-two", owner_id="owner-one"
        ) is None
        assert store.get_recurring_schedule_definition(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-two"
        ) is None
        assert store.list_recurring_schedule_occurrences(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-two"
        ) == []
        assert service.deactivate_recurring_schedule(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-two"
        ) is None

        deactivated = service.deactivate_recurring_schedule(
            definition["definition_id"], tenant_id="tenant-one", owner_id="owner-one"
        )
        assert deactivated is not None
        assert deactivated["active"] is False
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"recurrence_type": "daily", "local_time": "09:00", "timezone_name": "Mars/Olympus"}, "timezone"),
        ({"recurrence_type": "daily", "local_time": "25:00", "timezone_name": "UTC"}, "local_time"),
        (
            {"recurrence_type": "daily", "local_time": "09:00", "timezone_name": "UTC", "dst_policy": "guess"},
            "DST policy",
        ),
        ({"recurrence_type": "interval", "interval_seconds": 30, "timezone_name": "UTC"}, "at least 60"),
        (
            {"recurrence_type": "interval", "interval_seconds": 3600, "timezone_name": "America/New_York"},
            "UTC",
        ),
    ],
)
def test_invalid_recurrence_is_rejected_without_persistence(tmp_path, monkeypatch, kwargs, message):
    monkeypatch.setenv("GLASSHIVE_RECURRING_SCHEDULE_OWNER", "native")
    monkeypatch.setenv("GLASSHIVE_SCHEDULER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        worker = _worker(store)
        with pytest.raises(ValueError, match=message):
            service.create_recurring_schedule(worker["worker_id"], "Run repeatedly.", **kwargs)
        assert store.list_recurring_schedule_definitions(
            worker["worker_id"],
            tenant_id="tenant-one",
            owner_id="owner-one",
        ) == []
    finally:
        service.shutdown()
