"""Fault proofs for existing maintenance owners and bounded callback delivery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event, Thread
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime import schedule_targets
from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store
from test_schedule_target_service import create, destination, pending_callback


@pytest.fixture
def service(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    result = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False,
                                    start_background_consumers=False)
    yield result
    result.shutdown()
    store.close()


def callback(service, *, owner="owner-a", url="https://receiver.example/hooks/results", event="run.completed", state="completed"):
    store = service.store
    project = store.create_project(owner, "Synthetic work", "Preserve useful results", "codex-cli")
    worker = store.create_worker(project_id=project["project_id"], owner_id=owner,
        name="Worker", role="operator", profile="codex-cli", backend="stub", runtime="stub",
        model="stub/codex-cli", bootstrap_bundle={"callbacks": {"url": url}})
    run = store.create_run(worker["worker_id"], project["project_id"], "Synthetic work", state=state)
    callback_id = "cb_" + uuid.uuid4().hex
    payload = {"callback_id": callback_id, "callback_ts": int(time.time()), "event": event,
        "project_id": project["project_id"], "worker_id": worker["worker_id"], "run_id": run["run_id"],
        "run_state": state, "message": "Useful result"}
    record = store.upsert_callback_outbox(callback_id=callback_id, project_id=project["project_id"],
        worker_id=worker["worker_id"], run_id=run["run_id"], event_type=event, url=url,
        payload_json=json.dumps(payload))
    return worker, record


def read(service, record):
    return service.store.get_callback_outbox(record["callback_id"])


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Expected synthetic state did not settle")


@pytest.mark.parametrize("fault", ["claim", "executor"])
def test_webhook_failed_admission_releases_slot_and_keeps_same_receipt(service, monkeypatch, fault):
    record = pending_callback(service, create(service, destination(service)))
    claim = service.store.claim_pending_callback
    submit = service.schedule_transport_executor.submit
    if fault == "claim":
        monkeypatch.setattr(service.store, "claim_pending_callback", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    else:
        monkeypatch.setattr(service.schedule_transport_executor, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("closed")))
    # Two failed claims for this one owner used to occupy both permitted slots.
    for _ in range(2):
        with pytest.raises((sqlite3.OperationalError, RuntimeError)):
            schedule_targets.dispatch_pending(service)
        assert not service._schedule_transport_active
    saved = read(service, record)
    assert saved["status"] == "pending" and saved["attempts"] == 0
    assert saved["payload_json"] == record["payload_json"]
    monkeypatch.setattr(service.store, "claim_pending_callback", claim)
    queued = []
    monkeypatch.setattr(service.schedule_transport_executor, "submit", lambda *a, **k: queued.append((a, k)))
    assert schedule_targets.dispatch_pending(service) == [record["callback_id"]]
    assert len(queued) == 1 and len(service._schedule_transport_active) == 1
    key = next(iter(service._schedule_transport_active))
    with service._schedule_transport_lock:
        service._schedule_transport_active.discard(key)
    monkeypatch.setattr(service.schedule_transport_executor, "submit", submit)


def test_webhook_metadata_and_settlement_failure_still_releases_slot(service, monkeypatch):
    record = pending_callback(service, create(service, destination(service)))
    record = service.store.claim_pending_callback(record["callback_id"])
    key = tuple(record[name] for name in ("tenant_id", "owner_id", "destination_id", "callback_id"))
    assert schedule_targets.reserve_transport(service, key)
    record["payload_json"] = "{invalid"
    monkeypatch.setattr(schedule_targets, "finish_callback", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    with pytest.raises(sqlite3.OperationalError):
        schedule_targets.deliver(service, record)
    assert not service._schedule_transport_active


@pytest.mark.parametrize("fault_index", range(5))
def test_reaper_fault_does_not_skip_other_phases_or_the_next_pass(service, monkeypatch, fault_index):
    phases = ("reap_terminated_workers_once", "reap_ephemeral_workspaces_once", "reap_idle_workers_once",
              "reap_paused_workers_once", "reap_expired_runs_once")
    calls = []
    for index, name in enumerate(phases):
        def phase(index=index):
            calls.append(index)
            if index == fault_index and calls.count(index) == 1:
                raise sqlite3.OperationalError("busy")
        monkeypatch.setattr(service, name, phase)
    class TwoPasses:
        def __init__(self): self.passes = 0
        def wait(self, _):
            self.passes += 1
            return self.passes > 2
        def is_set(self): return False
    original = service._shutdown_event
    service._shutdown_event = TwoPasses()
    try:
        service._idle_reaper_loop()
    finally:
        service._shutdown_event = original
    assert calls == list(range(5)) * 2


def test_reaper_stops_between_phases(service, monkeypatch):
    calls = []
    def first():
        calls.append("first")
        service.begin_shutdown()
    monkeypatch.setattr(service, "_idle_reaper_interval_s", lambda: 0)
    monkeypatch.setattr(service, "reap_terminated_workers_once", first)
    monkeypatch.setattr(service, "reap_ephemeral_workspaces_once", lambda: calls.append("late"))
    service._idle_reaper_loop()
    assert calls == ["first"]


def test_shutdown_releases_executors_despite_lease_store_fault(service, monkeypatch):
    monkeypatch.setattr(service, "release_owned_host_run_leases", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    with pytest.raises(RuntimeError, match="lease cleanup remains unconfirmed"):
        service.shutdown()
    assert service.executor._shutdown and service.conversation_executor._shutdown
    assert service.schedule_transport_executor._shutdown
    monkeypatch.setattr(service, "release_owned_host_run_leases", lambda: None)


def test_health_distinguishes_dead_required_loop_from_disabled_and_finite_startup(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "health.db"), runtime_backend="stub", runtime=StubRuntime(), reconcile_on_startup=False)
    service = app.state.service
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        service._startup_recovery_thread.join(2)
        assert client.get("/health").json()["background_consumers"]["startup_recovery"] == "finished"
        actual = service._required_background_consumers["callback_retry"]
        service._required_background_consumers["callback_retry"] = Thread(target=lambda: None)
        failed = client.get("/health")
        assert failed.status_code == 503
        assert failed.json()["background_consumers"]["consumers"]["callback_retry"] == "stopped"
        service._required_background_consumers["callback_retry"] = actual
        assert client.get("/health").status_code == 200
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")
    app = create_app(str(tmp_path / "disabled.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["background_consumers"]["status"] == "disabled"


def test_blocked_callback_cannot_hold_provider_settlement_other_owner_or_mission_lane(service, monkeypatch):
    slow_worker, slow = callback(service, url="https://slow.example/hooks")
    fast_worker, fast = callback(service, url="https://fast.example/hooks")
    other_worker, other = callback(service, owner="owner-b", url="https://other.example/hooks")
    entered, release, settled = Event(), Event(), Event()
    def post(url, **kwargs):
        if "slow.example" in url:
            entered.set()
            assert release.wait(3)
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    service.set_provider_request_reconciler(lambda _: settled.set() or 1)
    tick = Thread(target=service._callback_retry_tick)
    tick.start()
    try:
        assert entered.wait(1)
        assert settled.wait(.5), "receiver HTTP blocked independent provider settlement"
        wait_for(lambda: read(service, fast)["status"] == "http_accepted")
        wait_for(lambda: read(service, other)["status"] == "http_accepted")
        assert read(service, slow)["status"] == "delivering"
        assert service.executor.submit(lambda: "mission free").result(timeout=.5) == "mission free"
    finally:
        release.set()
        tick.join(3)
    assert not tick.is_alive()
    wait_for(lambda: read(service, slow)["status"] == "http_accepted")
    wait_for(lambda: not service._schedule_transport_active)


def test_legacy_pending_callbacks_are_fair_by_owner_and_bound_submissions(service, monkeypatch):
    records = [callback(service)[1] for _ in range(5)]
    other = callback(service, owner="owner-b")[1]
    due = service.store.list_pending_callbacks(limit=2)
    assert {r["callback_id"] for r in due} == {records[0]["callback_id"], other["callback_id"]}
    queued = []
    monkeypatch.setattr(service.schedule_transport_executor, "submit", lambda *a, **k: queued.append((a, k)))
    service._replay_pending_callbacks()
    service._replay_pending_callbacks()
    assert len(queued) == 3  # two for one owner, one for the other, no unbounded queue
    assert len(service._schedule_transport_active) == 3
    for args, kwargs in queued:
        schedule_targets.release_transport(service, args[-1])


@pytest.mark.parametrize("fault", ["claim", "executor", "preamble"])
def test_legacy_callback_reservation_released_on_each_failure(service, monkeypatch, fault):
    worker, record = callback(service)
    callbacks = service._callback_config_for(worker)
    if fault == "claim":
        monkeypatch.setattr(service.store, "claim_pending_callback", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    elif fault == "executor":
        monkeypatch.setattr(service.schedule_transport_executor, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("closed")))
    else:
        monkeypatch.setattr(service, "_deliver_claimed_callback_record", lambda *a, **k: (_ for _ in ()).throw(ValueError("invalid metadata")))
    if fault == "preamble":
        assert service._submit_callback_delivery(worker, record, callbacks)
        wait_for(lambda: not service._schedule_transport_active)
        assert read(service, record)["status"] == "delivering"  # unknown, existing lease recovery owns retry
    else:
        assert not service._submit_callback_delivery(worker, record, callbacks)
        assert read(service, record)["status"] == "pending"
    assert not service._schedule_transport_active


def test_callback_failure_persists_backoff_and_reuses_original_identity_after_restart(service, monkeypatch):
    worker, record = callback(service)
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "10")
    sends = []
    def post(url, **kwargs):
        sends.append(json.loads(kwargs["content"]))
        return httpx.Response(500, request=httpx.Request("POST", url))
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    assert service._submit_callback_delivery(worker, record, service._callback_config_for(worker))
    wait_for(lambda: read(service, record)["status"] == "pending")
    failed = read(service, record)
    assert failed["attempts"] == 1 and failed["next_attempt_at"]
    assert not service.store.claim_pending_callback(record["callback_id"])
    assert service.store.list_pending_callbacks() == []
    assert len(sends) == 1 and sends[0]["callback_id"] == record["callback_id"]
    restarted = Store(str(service.store.db_path))
    try:
        assert restarted.list_pending_callbacks() == []
        with restarted._connect() as conn:
            conn.execute("UPDATE callback_outbox SET next_attempt_at=? WHERE callback_id=?",
                ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), record["callback_id"]))
        claimed = restarted.claim_pending_callback(record["callback_id"])
        assert claimed["delivery_generation"] > failed["delivery_generation"]
        assert claimed["payload_json"] == record["payload_json"]
    finally:
        restarted.close()


def test_disabled_background_consumers_keep_new_callbacks_pending(service):
    worker, record = callback(service)
    service._background_consumers_enabled = False
    assert not service._submit_callback_delivery(worker, record, service._callback_config_for(worker))
    assert read(service, record)["status"] == "pending"
    assert not service._schedule_transport_active


def test_needs_input_callback_recovers_from_receiver_outage_without_native_replay(service, monkeypatch):
    worker, record = callback(service, event="run.needs_input", state="queued")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "30")
    received = []
    def post(url, **kwargs):
        received.append(json.loads(kwargs["content"]))
        return httpx.Response(500 if len(received) == 1 else 200, request=httpx.Request("POST", url))
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    service._replay_pending_callbacks()
    wait_for(lambda: read(service, record)["attempts"] == 1)
    first = read(service, record)
    assert first["status"] == "pending" and first["next_attempt_at"]
    service._replay_pending_callbacks()
    assert len(received) == 1
    with service.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET next_attempt_at=? WHERE callback_id=?",
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), record["callback_id"]))
    service._replay_pending_callbacks()
    wait_for(lambda: read(service, record)["status"] == "http_accepted")
    assert read(service, record)["attempts"] == 2
    assert read(service, record)["payload_json"] == record["payload_json"]
    assert {item["callback_id"] for item in received} == {record["callback_id"]}
    assert {item["event"] for item in received} == {"run.needs_input"}
    assert service.store.get_run(record["run_id"])["state"] == "queued"
    assert not service._active_processors


def test_five_terminal_results_bypass_older_pending_status_with_one_slow_recipient(service, monkeypatch):
    """Controlled receiver waves, not model/network benchmark wall time."""
    slow_worker, held = callback(service, event="run.started", state="queued")
    # These old status rows must not consume the remaining owner slot ahead of results.
    statuses = [callback(service, event="run.queued", state="queued")[1] for _ in range(8)]
    terminals = [callback(service)[1] for _ in range(5)]
    queued, received = [], []
    monkeypatch.setattr(service.schedule_transport_executor, "submit", lambda *a, **k: queued.append((a, k)))
    def post(url, **kwargs):
        received.append(json.loads(kwargs["content"]))
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    assert service._submit_callback_delivery(slow_worker, held, service._callback_config_for(slow_worker))
    slow = queued.pop()
    for wave in range(1, 6):
        service._replay_pending_callbacks()
        assert len(queued) == 1, "A held routine status must leave one result lane available"
        args, kwargs = queued.pop()
        assert args[2]["event_type"] == "run.completed"
        args[0](*args[1:], **kwargs)
        assert sum(read(service, record)["status"] == "http_accepted" for record in terminals) == wave
        assert all(read(service, record)["status"] == "pending" for record in statuses)
        assert read(service, held)["status"] == "delivering"
        assert len(service._schedule_transport_active) == 1
    assert len(received) == 5
    assert {item["callback_id"] for item in received} == {item["callback_id"] for item in terminals}
    # Release the deliberately held older status only after every result reached the receiver.
    args, kwargs = slow
    args[0](*args[1:], **kwargs)
    assert not service._schedule_transport_active
    assert service.store.list_pending_callbacks(limit=1)[0]["callback_id"] == statuses[0]["callback_id"]


def test_five_results_share_two_slots_and_failed_result_retries_without_blocking_peers(service, monkeypatch):
    statuses = [callback(service, event="run.started", state="queued")[1] for _ in range(4)]
    terminals = [callback(service)[1] for _ in range(5)]
    queued, sent = [], []
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "30")
    monkeypatch.setattr(service.schedule_transport_executor, "submit", lambda *a, **k: queued.append((a, k)))
    fail_id = terminals[0]["callback_id"]
    def post(url, **kwargs):
        payload = json.loads(kwargs["content"])
        sent.append(payload)
        count = sum(item["callback_id"] == payload["callback_id"] for item in sent)
        return httpx.Response(503 if payload["callback_id"] == fail_id and count == 1 else 200,
                              request=httpx.Request("POST", url))
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", post)
    for count in (2, 2, 1):
        service._replay_pending_callbacks()
        terminal_jobs = [(args, kwargs) for args, kwargs in queued if args[2]["event_type"] == "run.completed"]
        assert len(terminal_jobs) == count
        for args, kwargs in terminal_jobs:
            queued.remove((args, kwargs));args[0](*args[1:], **kwargs)
        # A routine status can fill the fifth wave's otherwise spare slot; no result waits for it.
        for args, kwargs in queued[:]:
            queued.remove((args, kwargs));args[0](*args[1:], **kwargs)
    failed = read(service, terminals[0])
    assert failed["status"] == "pending" and failed["attempts"] == 1 and failed["next_attempt_at"]
    assert all(read(service, record)["status"] == "http_accepted" for record in terminals[1:])
    assert not service.store.claim_pending_callback(fail_id)
    with service.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET next_attempt_at=? WHERE callback_id=?",
                     ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), fail_id))
    service._replay_pending_callbacks()
    retry = next(job for job in queued if job[0][2]["callback_id"] == fail_id)
    args, kwargs = retry
    queued.remove(retry);args[0](*args[1:], **kwargs)
    assert read(service, terminals[0])["status"] == "http_accepted"
    assert read(service, terminals[0])["attempts"] == 2
    assert read(service, terminals[0])["payload_json"] == terminals[0]["payload_json"]
    for args, kwargs in queued:
        args[0](*args[1:], **kwargs)
