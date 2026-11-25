"""System maintenance cannot take the conversation capacity kept for live turns."""
import pytest

from workers_projects_runtime.openclaw_runtime import HostCapacityError, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import HostRunLeaseCapacityError, Store

POLICY = dict(conversation_limit=4, mission_limit=3, account_mission_limit=4,
              tenant_mission_limit=12)
LIVE = {"actor_kind": "external_user", "origin": "interactive"}
RESULT = {"actor_kind": "system", "origin": "callback"}
MAINTENANCE = {"actor_kind": "system", "origin": "system"}


def _subject(store, name, *, origin=None, lane="conversation"):
    project = store.create_project("owner", name, "Capacity subject.", "claude-code")
    bundle = {"run_mode": "conversation" if lane == "conversation" else "mission"}
    if origin is not None:
        bundle["conversation_origin"] = origin
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner", name=name, role="conversation-agent",
        profile="claude-code", backend="", runtime="claude-code", model="synthetic-model",
        execution_mode="host", trusted_run_lane=lane, bootstrap_bundle=bundle,
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "work", state="queued")
    return worker, run


def _lease(store, name, *, family="claude", lane="conversation", **options):
    worker, run = _subject(store, name, lane=lane)
    return store.acquire_host_run_lease(
        runtime_family=family, lane=lane, tenant_id="local", owner_id="owner",
        worker_id=worker["worker_id"], run_id=run["run_id"], executor_id="executor",
        lease_ttl_s=300, **POLICY, **options,
    )


def test_maintenance_stops_at_the_unreserved_share_and_live_turns_use_the_rest(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    reserve = dict(system_maintenance=True, conversation_live_reserve=2)
    _lease(store, "upkeep-1", **reserve)
    _lease(store, "upkeep-2", **reserve)
    with pytest.raises(HostRunLeaseCapacityError) as held:
        _lease(store, "upkeep-3", **reserve)
    assert held.value.capacity_class == "family_lane"
    assert held.value.configured == {"claudeConversationSlots": 2}
    assert held.value.used == {"claudeConversationSlots": 2}
    assert "live conversation" in str(held.value)
    # Live turns and their results still reach the whole lane.
    _lease(store, "live-1", conversation_live_reserve=2)
    _lease(store, "live-2", conversation_live_reserve=2)
    with pytest.raises(HostRunLeaseCapacityError) as full:
        _lease(store, "live-3", conversation_live_reserve=2)
    assert full.value.configured == {"claudeConversationSlots": 4}
    assert "configured capacity" in str(full.value)


def test_maintenance_does_not_enter_while_live_turns_fill_its_share(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    _lease(store, "live-1")
    _lease(store, "live-2")
    with pytest.raises(HostRunLeaseCapacityError):
        _lease(store, "upkeep", system_maintenance=True, conversation_live_reserve=2)


def test_reserve_never_starves_maintenance_and_leaves_other_lanes_alone(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    # A reserve as large as the lane still leaves maintenance one slot.
    _lease(store, "upkeep", system_maintenance=True, conversation_live_reserve=99)
    with pytest.raises(HostRunLeaseCapacityError):
        _lease(store, "upkeep-2", system_maintenance=True, conversation_live_reserve=99)
    # Other families and the mission lane keep their own unreserved limits.
    _lease(store, "codex-upkeep", family="codex", system_maintenance=True, conversation_live_reserve=2)
    for index in range(3):
        _lease(store, f"mission-{index}", lane="mission", system_maintenance=True,
               conversation_live_reserve=2)


def test_without_a_reserve_admission_is_unchanged(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    for index in range(4):
        _lease(store, f"upkeep-{index}", system_maintenance=True)
    with pytest.raises(HostRunLeaseCapacityError) as full:
        _lease(store, "upkeep-4", system_maintenance=True)
    assert full.value.configured == {"claudeConversationSlots": 4}


@pytest.fixture
def service(tmp_path, monkeypatch, background_consumers_disabled):
    _ = background_consumers_disabled
    monkeypatch.setenv("WPR_HOST_CONVERSATION_SLOTS_PER_CLI", "4")
    monkeypatch.delenv("WPR_HOST_CONVERSATION_LIVE_RESERVE", raising=False)
    store = Store(str(tmp_path / "runtime.db"))
    built = WorkersProjectsService(store=store, runtime=StubRuntime())
    monkeypatch.setattr(built, "_require_conversation_session_binding", lambda _worker: None)
    monkeypatch.setattr(built, "_host_resource_capacity_error",
                        lambda *_args, **_kwargs: (None, {}))
    try:
        yield built, store
    finally:
        built.shutdown()


@pytest.mark.parametrize("origin,maintenance", [
    (MAINTENANCE, True),
    (LIVE, False),
    (RESULT, False),
    ({"actor_kind": "system", "origin": "worker"}, True),
    ({"actor_kind": "system", "origin": "scheduler"}, True),
    ({"actor_kind": "external_user", "origin": "scheduler"}, False),
    (None, False),
])
def test_only_system_started_background_work_is_held_back(service, origin, maintenance):
    built, store = service
    worker, _run = _subject(store, "subject", origin=origin)
    assert built._system_maintenance_run(worker) is maintenance


def test_default_reserve_is_half_the_lane_and_deployment_settings_win(service, monkeypatch, tmp_path):
    built, _store = service
    assert built._conversation_live_reserve == 2
    monkeypatch.setenv("WPR_HOST_CONVERSATION_LIVE_RESERVE", "1")
    other = WorkersProjectsService(store=Store(str(tmp_path / "other.db")), runtime=StubRuntime())
    try:
        assert other._conversation_live_reserve == 1
    finally:
        other.shutdown()


def test_a_live_turn_is_admitted_while_maintenance_waits(service):
    built, store = service
    for index in range(2):
        worker, run = _subject(store, f"upkeep-{index}", origin=MAINTENANCE)
        assert built._acquire_host_run_lease(worker, run)["status"] == "active"
    waiting_worker, waiting_run = _subject(store, "upkeep-waiting", origin=MAINTENANCE)
    with pytest.raises(HostCapacityError) as held:
        built._acquire_host_run_lease(waiting_worker, waiting_run)
    assert held.value.capacity_class == "family_lane"
    for name, origin in (("queen", LIVE), ("result", RESULT)):
        worker, run = _subject(store, name, origin=origin)
        assert built._acquire_host_run_lease(worker, run)["status"] == "active"
    assert store.get_active_host_run_lease_for_run(waiting_run["run_id"]) is None


def test_a_live_turn_gets_a_slot_while_scheduled_and_worker_turns_fill_their_share(service):
    built, store = service
    for name, origin in (("scheduled", {"actor_kind": "system", "origin": "scheduler"}),
                         ("worker-turn", {"actor_kind": "system", "origin": "worker"})):
        worker, run = _subject(store, name, origin=origin)
        assert built._acquire_host_run_lease(worker, run)["status"] == "active"
    for name in ("scheduled-2", "upkeep"):
        origin = {"actor_kind": "system", "origin": "scheduler" if name == "scheduled-2" else "system"}
        worker, run = _subject(store, name, origin=origin)
        with pytest.raises(HostCapacityError):
            built._acquire_host_run_lease(worker, run)
    for name in ("live-1", "live-2"):
        worker, run = _subject(store, name, origin=LIVE)
        assert built._acquire_host_run_lease(worker, run)["status"] == "active"
