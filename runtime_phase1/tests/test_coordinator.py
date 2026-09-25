from types import SimpleNamespace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import time

import pytest
from fastapi import HTTPException

from workers_projects_runtime.coordinator import (
    CoordinatorConfig, CoordinatorConflict, CoordinatorScopeError,
    CoordinatorScope, CoordinatorService, Dispatch, Goal, Route, Control, prompt_manifest,
)
from workers_projects_runtime.store import Store
from workers_projects_runtime.service import WorkersProjectsService, ParallelExecutionIsolationError
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.failure_classification import is_user_resumable_failure


class Provider:
    def _model(self, model):
        if model != 'exact-native-model':
            raise ValueError('unsupported model')
        return SimpleNamespace(effort_choices=('high',))

    def start(self, payload, **kwargs):
        raise ConnectionError('private-provider-prose-never-log')


@pytest.fixture
def coordinator(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    service = SimpleNamespace()
    core = CoordinatorService(store, service, Provider())
    yield core
    store.close()


def create(core, max_goals=100):
    return core.create('local', 'owner', CoordinatorConfig(
        model='exact-native-model', effort='high', max_goals=max_goals,
        routes=[Route(id='route', profile='codex-cli', model='exact-worker-model',
                      effort='high', execution_mode='host')]))['conversation_id']


def test_complete_batch_persists_before_native_failure_and_restart(coordinator):
    cid = create(coordinator)
    goals = [Goal(id=str(i), text=f'Explicit independent objective {i}') for i in range(10)]
    message = 'Exact raw message\n\nwith spaces  and Unicode é.'
    coordinator.accept_turn('local', 'owner', cid, 'turn', message, goals)
    response = coordinator.start_turn('local', 'owner', cid, 'turn')
    assert response == {'turn_id': 'turn', 'state': 'blocked', 'blocker': 'ConnectionError'}
    reopened = CoordinatorService(coordinator.store, coordinator.service, Provider())
    state = reopened.snapshot('local', 'owner', cid)
    assert len(state['goals']) == 10
    assert all(g['state'] == 'accepted' for g in state['goals'])
    assert state['turns'][0]['message'] == message
    assert 'private-provider-prose' not in str(state)
    assert 'payload_json' not in state['turns'][0]


def test_maintenance_does_not_replay_blocked_admission_without_user_retry(coordinator):
    class CountingProvider(Provider):
        calls = 0

        def start(self, payload, **kwargs):
            self.calls += 1
            raise ConnectionError('synthetic admission failure')

    provider = CountingProvider()
    coordinator.provider = provider
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'synthetic goal')
    assert coordinator.start_turn('local', 'owner', cid, 'turn')['state'] == 'blocked'
    assert provider.calls == 1
    assert coordinator.reconcile_once() == []
    assert provider.calls == 1
    assert coordinator.start_turn('local', 'owner', cid, 'turn')['state'] == 'blocked'
    assert provider.calls == 2


def test_first_use_shared_probe_recovers_same_saved_turn_after_restart(coordinator):
    class TransientProvider(Provider):
        calls = 0
        accepted = []

        def start(self, payload, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ParallelExecutionIsolationError(
                    "private probe detail",
                    reason_code="shared_resource_authority_unavailable",
                )
            self.accepted.append(payload.metadata.message_id)
            return {'request_id': 'accepted-first-use', 'state': 'queued'}

    provider = TransientProvider()
    coordinator.provider = provider
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'first-turn', 'Original first message')
    assert coordinator.start_turn('local', 'owner', cid, 'first-turn') == {
        'turn_id': 'first-turn', 'state': 'blocked',
        'blocker': 'shared_resource_authority_unavailable',
    }
    saved = coordinator.snapshot('local', 'owner', cid)['turns'][0]
    assert saved['request_id'] == '' and saved['retry_after_at']
    assert 'private probe detail' not in str(saved)
    restarted = CoordinatorService(coordinator.store, coordinator.service, provider)
    assert restarted.reconcile_once() == []
    with coordinator.store._connect() as conn:
        conn.execute(
            "UPDATE coordinator_turns SET retry_after_at=? WHERE conversation_id=? AND turn_id=?",
            ('2026-09-23T00:00:00+00:00', cid, 'first-turn'),
        )
    assert restarted.reconcile_once() == [
        {'turn_id': 'first-turn', 'request_id': 'accepted-first-use', 'state': 'queued'}
    ]
    assert provider.accepted == ['first-turn']
    assert restarted.snapshot('local', 'owner', cid)['turns'][0]['message'] == 'Original first message'


def test_permanent_shared_admission_does_not_retry(coordinator):
    class PermanentProvider(Provider):
        calls = 0

        def start(self, payload, **kwargs):
            self.calls += 1
            raise ParallelExecutionIsolationError(
                "private configuration detail", reason_code="shared_configuration_required",
            )

    provider = PermanentProvider()
    coordinator.provider = provider
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'first-turn', 'Original first message')
    assert coordinator.start_turn('local', 'owner', cid, 'first-turn')['blocker'] == 'shared_configuration_required'
    assert coordinator.snapshot('local', 'owner', cid)['turns'][0]['retry_after_at'] == ''
    assert coordinator.reconcile_once() == []
    assert provider.calls == 1


@pytest.mark.parametrize('temporary_code', ['host_capacity', 'provider_account_busy'])
def test_first_use_capacity_retries_without_user_action(coordinator, temporary_code):
    class CapacityError(RuntimeError):
        code = temporary_code

    class CapacityProvider(Provider):
        calls = 0

        def start(self, payload, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise CapacityError('private capacity detail')
            return {'request_id': 'accepted-after-capacity', 'state': 'queued'}

    provider = CapacityProvider()
    coordinator.provider = provider
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'first-turn', 'Exact first message')
    assert coordinator.start_turn('local', 'owner', cid, 'first-turn')['blocker'] == temporary_code
    saved = coordinator.snapshot('local', 'owner', cid)['turns'][0]
    assert saved['request_id'] == '' and saved['retry_after_at']
    with coordinator.store._connect() as conn:
        conn.execute(
            "UPDATE coordinator_turns SET retry_after_at=? WHERE conversation_id=? AND turn_id=?",
            ('2026-09-23T00:00:00+00:00', cid, 'first-turn'),
        )
    assert CoordinatorService(coordinator.store, coordinator.service, provider).reconcile_once() == [
        {'turn_id': 'first-turn', 'request_id': 'accepted-after-capacity', 'state': 'queued'}
    ]
    assert provider.calls == 2


@pytest.mark.parametrize('failure_class', [
    'native_input_expired', 'native_input_declined', 'native_input_cancelled',
])
def test_explicit_retry_can_resume_a_failed_native_response_request(failure_class):
    assert is_user_resumable_failure(
        failure_class=failure_class, retryable=False,
        runtime_invoked_at='2026-09-23T00:00:00+00:00',
        started_at='2026-09-23T00:00:00+00:00',
    )
    assert not is_user_resumable_failure(
        failure_class='native_turn_cancelled', retryable=False,
        runtime_invoked_at='2026-09-23T00:00:00+00:00',
        started_at='2026-09-23T00:00:00+00:00',
    )


def test_capacity_blocked_result_wake_retries_after_durable_delay_and_restart(coordinator):
    class CapacityError(RuntimeError):
        code = 'host_capacity'

    class ToggleProvider(Provider):
        def __init__(self):
            self.available = False
            self.calls = 0

        def start(self, payload, **kwargs):
            self.calls += 1
            if not self.available:
                raise CapacityError('account or host is busy')
            return {'request_id': 'accepted-result-wake', 'state': 'queued'}

    provider = ToggleProvider()
    core = CoordinatorService(coordinator.store, coordinator.service, provider)
    cid = create(core)
    with core.store._connect() as conn:
        conn.execute(
            "INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,created_at) "
            "VALUES(?,?,?,?,?)",
            (cid, 'joined-results', '{"kind":"worker_result_notifications","items":[]}',
             'worker_results', '2026-09-23T00:00:00+00:00'),
        )
    blocked = core.reconcile_once()
    assert blocked == [{'turn_id': 'joined-results', 'state': 'blocked', 'blocker': 'host_capacity'}]
    with core.store._connect() as conn:
        row = conn.execute(
            "SELECT retry_after_at,retry_attempts FROM coordinator_turns "
            "WHERE conversation_id=? AND turn_id='joined-results'", (cid,),
        ).fetchone()
    assert row['retry_after_at'] and row['retry_attempts'] == 1
    provider.available = True
    restarted = CoordinatorService(core.store, core.service, provider)
    assert restarted.reconcile_once() == []
    assert provider.calls == 1
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE coordinator_turns SET retry_after_at=? WHERE conversation_id=? "
            "AND turn_id='joined-results'", ('2026-09-23T00:00:00+00:00', cid),
        )
    assert restarted.reconcile_once() == [
        {'turn_id': 'joined-results', 'request_id': 'accepted-result-wake', 'state': 'queued'}
    ]
    assert provider.calls == 2
    assert restarted.reconcile_once() == []


def test_permanent_result_wake_blocker_is_not_retried(coordinator):
    class MissingAuth(RuntimeError):
        code = 'provider_auth_missing'

    class UnavailableProvider(Provider):
        def __init__(self):
            self.calls = 0

        def start(self, payload, **kwargs):
            self.calls += 1
            raise MissingAuth('configure an authorized account')

    provider = UnavailableProvider()
    core = CoordinatorService(coordinator.store, coordinator.service, provider)
    cid = create(core)
    with core.store._connect() as conn:
        conn.execute(
            "INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,created_at) "
            "VALUES(?,?,?,?,?)",
            (cid, 'joined-results', '{}', 'worker_results', '2026-09-23T00:00:00+00:00'),
        )
    assert core.reconcile_once()[0]['blocker'] == 'provider_auth_missing'
    assert core.reconcile_once() == []
    assert provider.calls == 1


def test_failed_result_handoff_retries_saved_evidence_once_and_rejects_later_answer(coordinator):
    class AvailableProvider(Provider):
        calls = 0

        def start(self, payload, **kwargs):
            self.calls += 1
            assert payload.messages[-1].content == '{"kind":"worker_result_notifications","items":[]}'
            return {'request_id': 'fresh-request', 'state': 'queued'}

    provider = AvailableProvider()
    coordinator.provider = provider
    cid = create(coordinator)
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    worker = coordinator.store.create_worker(
        project_id, 'owner', 'Assistant', 'Assistant', 'grok-build',
        'grok-build', 'grok-build', 'exact-native-model', execution_mode='host',
    )
    session = coordinator.store.upsert_provider_session(
        tenant_id='local', owner_id='owner', conversation_id=cid,
        agent_id='coordinator', model_id='exact-native-model',
        project_id=project_id, worker_id=worker['worker_id'],
        workspace_dir='/workspace', access_mode='workspace',
    )
    old_request, _ = coordinator.store.create_provider_request(
        tenant_id='local', owner_id='owner', session_id=session['session_id'],
        idempotency_key='old', message_id='old', stream_id='', requested_history_count=1,
    )
    coordinator.store.update_provider_request(old_request['request_id'], state='failed')
    evidence = '{"kind":"worker_result_notifications","items":[]}'
    with coordinator.store._connect() as conn:
        conn.execute(
            "INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,request_id,blocker,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (cid, 'failed-results', evidence, 'worker_results', old_request['request_id'],
             'failed', '2026-09-23T00:00:00+00:00'),
        )
    with pytest.raises(CoordinatorScopeError):
        coordinator.retry_result_turn('local', 'other-owner', cid, 'failed-results')
    first = coordinator.retry_result_turn('local', 'owner', cid, 'failed-results')
    assert first['request_id'] == 'fresh-request'
    assert coordinator.retry_result_turn('local', 'owner', cid, 'failed-results')['request_id'] == 'fresh-request'
    assert provider.calls == 1
    turns = coordinator.snapshot('local', 'owner', cid)['turns']
    assert len(turns) == 2 and turns[0]['blocker'] == 'failed'
    assert turns[1]['message'] == evidence and turns[1]['origin'] == 'worker_results'
    with coordinator.store._connect() as conn:
        conn.execute(
            "UPDATE coordinator_turns SET response_json='{}' WHERE conversation_id=? AND turn_id=?",
            (cid, turns[1]['turn_id']),
        )
    with pytest.raises(CoordinatorConflict, match='later reply'):
        coordinator.retry_result_turn('local', 'owner', cid, 'failed-results')


def test_pre_migration_capacity_blocked_result_wake_recovers(coordinator):
    class AvailableProvider(Provider):
        def start(self, payload, **kwargs):
            return {'request_id': 'recovered-result-wake', 'state': 'queued'}

    core = CoordinatorService(coordinator.store, coordinator.service, AvailableProvider())
    cid = create(core)
    # An earlier image persisted this exact result turn before retry columns existed.
    # The additive migration supplies empty defaults; maintenance must still resume it.
    with core.store._connect() as conn:
        conn.execute(
            "INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,blocker,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (cid, 'old-results', '{}', 'worker_results', 'host_capacity',
             '2026-09-23T00:00:00+00:00'),
        )
    assert core.reconcile_once() == [
        {'turn_id': 'old-results', 'request_id': 'recovered-result-wake', 'state': 'queued'}
    ]


@pytest.mark.parametrize('cancel_accepted', [False, True])
def test_late_failed_admission_cannot_overwrite_concurrent_accepted_turn(coordinator, cancel_accepted):
    class CapacityError(RuntimeError):
        code = 'host_capacity'

    class RacingProvider(Provider):
        def __init__(self):
            self.lock = Lock()
            self.calls = 0
            self.first_started = Event()
            self.release_first = Event()

        def start(self, payload, **kwargs):
            with self.lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                self.first_started.set()
                assert self.release_first.wait(5)
                raise CapacityError('late capacity response')
            return {'request_id': 'accepted-concurrent-result', 'state': 'queued'}

        def cancel_by_idempotency(self, *args, **kwargs):
            return {'state': 'cancelled'}

    provider = RacingProvider()
    core = CoordinatorService(coordinator.store, coordinator.service, provider)
    cid = create(core)
    with core.store._connect() as conn:
        conn.execute(
            "INSERT INTO coordinator_turns(conversation_id,turn_id,message,origin,created_at) "
            "VALUES(?,?,?,?,?)",
            (cid, 'joined-results', '{}', 'worker_results', '2026-09-23T00:00:00+00:00'),
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(core.start_turn, 'local', 'owner', cid, 'joined-results')
        assert provider.first_started.wait(5)
        accepted = core.start_turn('local', 'owner', cid, 'joined-results')
        assert accepted['request_id'] == 'accepted-concurrent-result'
        if cancel_accepted:
            core.cancel_turn('local', 'owner', cid, 'joined-results')
        provider.release_first.set()
        late_result = late.result(timeout=5)
        if cancel_accepted:
            assert late_result == {'turn_id': 'joined-results', 'state': 'cancelled'}
        else:
            assert late_result['request_id'] == 'accepted-concurrent-result'
    state = core.snapshot('local', 'owner', cid)['turns'][0]
    assert state['request_id'] == 'accepted-concurrent-result'
    assert state['blocker'] == ('cancelled' if cancel_accepted else '')
    assert core.reconcile_once() == []


def test_plain_foreground_turn_exposes_only_owned_running_native_control(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'Run a local command')
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    worker = coordinator.store.create_worker(
        project_id, 'owner', 'Assistant', 'Assistant', 'grok-build',
        'grok-build', 'grok-build', 'exact-native-model', execution_mode='host',
    )
    run = coordinator.store.create_run(worker['worker_id'], project_id, 'Run a local command')
    session = coordinator.store.upsert_provider_session(
        tenant_id='local', owner_id='owner', conversation_id=cid,
        agent_id='coordinator', model_id='exact-native-model',
        project_id=project_id, worker_id=worker['worker_id'],
        workspace_dir='/workspace', access_mode='workspace',
    )
    request, _ = coordinator.store.create_provider_request(
        tenant_id='local', owner_id='owner', session_id=session['session_id'],
        idempotency_key='turn', message_id='turn', stream_id='',
        requested_history_count=1,
    )
    coordinator.store.update_provider_request(request['request_id'], run_id=run['run_id'], state='running')
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET request_id=? WHERE conversation_id=? AND turn_id='turn'",
                     (request['request_id'], cid))
        conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (run['run_id'],))
    snapshot = coordinator.snapshot('local', 'owner', cid)
    assert snapshot['goals'] == []
    assert snapshot['foreground_native_runs'] == [
        {'worker_id': worker['worker_id'], 'run_id': run['run_id']}
    ]
    with pytest.raises(CoordinatorScopeError):
        coordinator.snapshot('local', 'another-owner', cid)
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET blocker='failed' WHERE conversation_id=? AND turn_id='turn'", (cid,))
    assert coordinator.snapshot('local', 'owner', cid)['foreground_native_runs'] == []


@pytest.mark.parametrize('structured,failure_class,expected', [
    (1, 'native_input_expired', 'native_input_expired'),
    (0, 'native_input_expired', 'failed'),
    (1, 'unclassified', 'failed'),
])
def test_foreground_timeout_keeps_only_a_safe_typed_recovery_reason(
    coordinator, structured, failure_class, expected,
):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'synthetic request')
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET request_id='request' WHERE conversation_id=? AND turn_id='turn'", (cid,))
    coordinator.store.get_provider_request = lambda _request_id: {'request_id': 'request', 'run_id': 'run', 'state': 'failed'}
    coordinator.store.get_run = lambda _run_id: {'failure_structured': structured, 'failure_class': failure_class}
    coordinator.provider._sync = lambda record: record
    refreshed = coordinator.refresh('local', 'owner', cid)
    assert refreshed['turns'][0]['blocker'] == expected


@pytest.mark.parametrize(
    "blocker_code",
    ["provider_account_busy", "provider_unavailable", "provider_auth_missing"],
)
def test_provider_blocker_keeps_safe_typed_reason(coordinator, blocker_code):
    class TypedProvider(Provider):
        def start(self, payload, **kwargs):
            raise HTTPException(
                status_code=409,
                detail={"code": blocker_code, "message": "private provider detail"},
            )

    typed = CoordinatorService(coordinator.store, coordinator.service, TypedProvider())
    cid = create(typed)
    typed.accept_turn("local", "owner", cid, "turn", "raw")
    result = typed.start_turn("local", "owner", cid, "turn")
    assert result == {
        "turn_id": "turn",
        "state": "blocked",
        "blocker": blocker_code,
    }
    snapshot = typed.snapshot("local", "owner", cid)
    assert snapshot["turns"][0]["blocker"] == blocker_code
    assert "private provider detail" not in str(snapshot)


def test_retry_reuses_one_turn_identity_across_ui_and_maintenance(coordinator):
    class ToggleProvider(Provider):
        def __init__(self):
            self.available = False
            self.accepted = []

        def start(self, payload, **kwargs):
            if not self.available:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "provider_account_busy", "message": "private detail"},
                )
            self.accepted.append(payload.metadata.message_id)
            return {"request_id": "fixture-" + payload.metadata.message_id, "state": "queued"}

    provider = ToggleProvider()
    typed = CoordinatorService(coordinator.store, coordinator.service, provider)
    cid = create(typed)
    typed.accept_turn("local", "owner", cid, "logical-turn", "raw")
    blocked = typed.start_turn("local", "owner", cid, "logical-turn")
    assert blocked["blocker"] == "provider_account_busy"

    provider.available = True
    # The UI Retry posts the durable turn key again. INSERT OR IGNORE is the
    # idempotent admission boundary, so maintenance sees this same turn.
    typed.accept_turn("local", "owner", cid, "logical-turn", "raw")
    accepted = typed.start_turn("local", "owner", cid, "logical-turn")
    assert accepted["request_id"] == "fixture-logical-turn"
    assert typed.reconcile_once() == []
    assert provider.accepted == ["logical-turn"]
    assert typed.snapshot("local", "owner", cid)["turns"][0]["turn_id"] == "logical-turn"


def test_atomic_goal_limit_conflict_preserves_old_batch(coordinator):
    cid = create(coordinator, max_goals=10)
    coordinator.accept_turn('local', 'owner', cid, 'first', 'first', [Goal(id='a', text='a')])
    with pytest.raises(CoordinatorConflict):
        coordinator.accept_turn('local', 'owner', cid, 'second', 'second', [Goal(id=str(i), text=str(i)) for i in range(10)])
    state = coordinator.snapshot('local', 'owner', cid)
    assert len(state['goals']) == 1
    assert len(state['turns']) == 1


def test_batch_replay_and_changed_goal_are_not_silent(coordinator):
    cid = create(coordinator)
    goals = [Goal(id='a', text='original'), Goal(id='b', text='second')]
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', goals)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', goals)
    with pytest.raises(CoordinatorConflict):
        coordinator.accept_goals('local', 'owner', cid, 'turn', [Goal(id='c', text='new'), Goal(id='a', text='changed')])
    assert len(coordinator.snapshot('local', 'owner', cid)['goals']) == 2
    with pytest.raises(CoordinatorConflict):
        coordinator.accept_turn('local', 'owner', cid, 'turn', 'changed raw')


def test_scope_and_route_ceiling_fail_before_dispatch(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id='a', text='goal')])
    with pytest.raises(CoordinatorScopeError):
        coordinator.snapshot('local', 'other-owner', cid)
    with pytest.raises(CoordinatorScopeError):
        coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id='a', route_id='invented', instruction='exact'))
    with pytest.raises(ValueError):
        coordinator.create('local', 'owner', CoordinatorConfig(model='alias', effort='high'))


def test_each_failed_admission_remains_visible_and_siblings_are_attempted(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id=str(i), text=str(i)) for i in range(10)])
    attempted = []
    def reserve(**kwargs):
        attempted.append(kwargs)
        raise RuntimeError('capacity')
    coordinator.service.reserve_delegation = reserve
    for i in range(10):
        coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id=str(i), route_id='route', instruction=f'goal {i}'))
    assert len(attempted) == 10
    assert all(not x['emit_callback'] and not x['start_run'] for x in attempted)
    assert all(x['bootstrap_bundle']['viventium_launch_authority']['worker_model'] == 'exact-worker-model' for x in attempted)
    assert [x['state'] for x in coordinator.snapshot('local', 'owner', cid)['goals']] == ['blocked'] * 10
    with pytest.raises(CoordinatorConflict):
        coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id='0', route_id='route', instruction='changed'))


def test_armed_admission_refusal_blocks_only_its_first_dispatch_order(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id=g, text=g) for g in ('a', 'b', 'c')])
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    runs = {}
    for goal in ('a', 'b', 'c'):
        worker = coordinator.store.create_worker(
            project_id, 'owner', goal, goal, 'codex-cli', 'codex-cli', 'codex-cli',
            'exact-worker-model', execution_mode='host',
        )
        runs[goal] = (worker, coordinator.store.create_run(worker['worker_id'], project_id, goal))
    admissions, reserved = [], []

    def admission(*, tenant_id, owner_id, conversation_id, ordinal):
        admissions.append((tenant_id, owner_id, conversation_id, ordinal))
        if ordinal == 2:
            raise ParallelExecutionIsolationError('unavailable', reason_code='shared_configuration_required')

    def reserve(**kwargs):
        goal = kwargs['idempotency_key'].rsplit(':', 1)[1]
        reserved.append(goal)
        worker, run = runs[goal]
        return {'work_ref': f'work-{goal}', 'worker_id': worker['worker_id'], 'initial_run_id': run['run_id']}

    coordinator.service.local_qa_coordinator_admission = admission
    coordinator.service.reserve_delegation = reserve
    coordinator.service.start_assigned_run = lambda worker_id: None
    snapshots = [coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id=g, route_id='route', instruction=g))
                 for g in ('a', 'b', 'c')]

    assert admissions == [('local', 'owner', cid, ordinal) for ordinal in (1, 2, 3)]
    assert reserved == ['a', 'c']
    assert snapshots[1]['state'] == 'blocked' and snapshots[1]['blocker'] == 'shared_configuration_required'
    assert snapshots[0]['run_id'] and snapshots[2]['run_id']
    # Replaying the recorded dispatch is not a new admission order.
    replay = coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id='b', route_id='route', instruction='b'))
    assert len(admissions) == 3 and reserved == ['a', 'c', 'b'] and replay['run_id']


def test_maintenance_replays_capacity_blocked_goals_but_not_permanent_admission_refusals(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id=g, text=g) for g in ('busy', 'missing')])
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    runs = {}
    for goal in ('busy', 'missing'):
        worker = coordinator.store.create_worker(
            project_id, 'owner', goal, goal, 'codex-cli', 'codex-cli', 'codex-cli',
            'exact-worker-model', execution_mode='host',
        )
        runs[goal] = (worker, coordinator.store.create_run(worker['worker_id'], project_id, goal))

    class Capacity(RuntimeError):
        code = 'host_capacity'

    failures = {
        'busy': Capacity('busy'),
        'missing': ParallelExecutionIsolationError('missing', reason_code='shared_configuration_required'),
    }
    reserved = []

    def reserve(**kwargs):
        goal = kwargs['idempotency_key'].rsplit(':', 1)[1]
        reserved.append(goal)
        failure = failures.pop(goal, None)
        if failure is not None:
            raise failure
        worker, run = runs[goal]
        return {'work_ref': f'work-{goal}', 'worker_id': worker['worker_id'], 'initial_run_id': run['run_id']}

    coordinator.service.reserve_delegation = reserve
    coordinator.service.start_assigned_run = lambda worker_id: None
    for goal in ('busy', 'missing'):
        assert coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id=goal, route_id='route', instruction=goal))['state'] == 'blocked'

    recovered = coordinator.recover_dispatches('local', 'owner', cid)

    assert [(r['goal_id'], r['run_id']) for r in recovered] == [('busy', runs['busy'][1]['run_id'])]
    assert reserved == ['busy', 'missing', 'busy']
    missing = next(g for g in coordinator.snapshot('local', 'owner', cid)['goals'] if g['goal_id'] == 'missing')
    assert missing['state'] == 'blocked' and missing['blocker'] == 'shared_configuration_required'
    # An explicit re-dispatch with the same goal, route and instruction still retries it.
    retried = coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id='missing', route_id='route', instruction='missing'))
    assert retried['run_id'] == runs['missing'][1]['run_id'] and reserved[-1] == 'missing'


def test_replayed_dispatch_preserves_steered_replacement_run(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id='a', text='goal')])
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    worker = coordinator.store.create_worker(
        project_id, 'owner', 'Child', 'Child', 'codex-cli',
        'codex-cli', 'codex-cli', 'exact-worker-model', execution_mode='host',
    )
    initial = coordinator.store.create_run(worker['worker_id'], project_id, 'goal')
    replacement = coordinator.store.create_run(worker['worker_id'], project_id, 'continue goal')
    starts = []
    coordinator.service.reserve_delegation = lambda **kwargs: {
        'work_ref': 'work-a', 'worker_id': worker['worker_id'],
        'initial_run_id': initial['run_id'],
    }
    coordinator.service.start_assigned_run = lambda worker_id: starts.append(worker_id)
    coordinator.service.steer_worker = lambda *args, **kwargs: {
        'replacement_run_id': replacement['run_id'],
    }
    request = Dispatch(goal_id='a', route_id='route', instruction='exact')
    assert coordinator.dispatch('local', 'owner', cid, request)['run_id'] == initial['run_id']
    assert coordinator.control('local', 'owner', cid, 'a', Control(
        action='steer', run_id=initial['run_id'], idempotency_key='continue-a',
        message='continue goal',
    ))['run_id'] == replacement['run_id']
    assert coordinator.dispatch('local', 'owner', cid, request)['run_id'] == replacement['run_id']
    assert coordinator.snapshot('local', 'owner', cid)['goals'][0]['run_id'] == replacement['run_id']
    assert starts == [worker['worker_id']]


def test_peer_principal_alone_cannot_delegate(coordinator):
    coordinator.store.get_provider_session_by_worker = lambda _: None
    with pytest.raises(CoordinatorScopeError):
        coordinator.native_identity({'worker_id': 'worker', 'tenant_id': 'local', 'owner_id': 'owner'})


def test_exact_control_never_targets_arbitrary_run(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id='a', text='a')])
    with pytest.raises(CoordinatorConflict):
        coordinator.control('local', 'owner', cid, 'a', Control(action='stop', run_id='foreign', idempotency_key='stop'))


def _bound_failed_child(core, monkeypatch):
    cid = create(core)
    core.accept_turn('local', 'owner', cid, 'turn', 'Exact original request', [
        Goal(id='child', text='Exact child goal'), Goal(id='sibling', text='Independent sibling'),
    ])
    project_id = core.snapshot('local', 'owner', cid)['scope']['project_id']
    child = core.store.create_worker(
        project_id, 'owner', 'Child', 'Child', 'codex-cli',
        'codex-cli', 'codex-cli', 'exact-worker-model', execution_mode='host',
    )
    sibling = core.store.create_worker(
        project_id, 'owner', 'Sibling', 'Sibling', 'codex-cli',
        'codex-cli', 'codex-cli', 'exact-worker-model', execution_mode='host',
    )
    failed = core.store.create_run(child['worker_id'], project_id, 'Exact child instruction')
    untouched = core.store.create_run(sibling['worker_id'], project_id, 'Independent sibling instruction')
    core.store.update_run(failed['run_id'], state='failed', failure_class='host_capacity', failure_retryable=1)
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE coordinator_goals SET work_ref=?,worker_id=?,run_id=? "
            "WHERE conversation_id=? AND goal_id='child'",
            ('work-child', child['worker_id'], failed['run_id'], cid),
        )
        conn.execute(
            "UPDATE coordinator_goals SET work_ref=?,worker_id=?,run_id=? "
            "WHERE conversation_id=? AND goal_id='sibling'",
            ('work-sibling', sibling['worker_id'], untouched['run_id'], cid),
        )
    delegation = {
        'work_ref': 'work-child', 'tenant_id': 'local', 'owner_id': 'owner',
        'worker_id': child['worker_id'], 'project_id': project_id,
        'run_id': failed['run_id'], 'current_run_id': failed['run_id'],
    }
    monkeypatch.setattr(core.store, 'get_delegation', lambda ref, *, tenant_id, owner_id:
                        delegation if (ref, tenant_id, owner_id) == ('work-child', 'local', 'owner') else None)
    core.service.start_assigned_run = lambda worker_id: None
    return cid, child, failed, untouched, delegation


def test_failed_child_retry_rebinds_exact_goal_and_replays_one_result(coordinator, monkeypatch):
    cid, child, failed, sibling, delegation = _bound_failed_child(coordinator, monkeypatch)
    replacement_id = 'run_exact_retry'
    calls = []
    coordinator.service.active_work_effect_run_id = lambda record, *, idempotency_key: replacement_id

    def retry(record, *, action, idempotency_key, expected_run_id, start_processor):
        calls.append((action, expected_run_id))
        assert record == delegation
        assert start_processor is False
        coordinator.store.create_run(child['worker_id'], failed['project_id'],
                                     'Exact continued child', run_id=replacement_id)
        return {'run_id': replacement_id, 'state': 'queued'}

    coordinator.service.execute_active_work_action = retry
    control = Control(action='retry', run_id=failed['run_id'], idempotency_key='retry-once')
    assert coordinator.snapshot('local', 'owner', cid)['goals'][0]['retryable'] is True
    accepted = coordinator.control('local', 'owner', cid, 'child', control)
    assert accepted['run_id'] == replacement_id and accepted['goal_id'] == 'child'
    assert coordinator.control('local', 'owner', cid, 'child', control) == accepted
    assert calls == [('retry', failed['run_id'])]
    reopened = CoordinatorService(coordinator.store, coordinator.service, Provider())
    goals = {item['goal_id']: item for item in reopened.snapshot('local', 'owner', cid)['goals']}
    assert goals['child']['run_id'] == replacement_id
    assert goals['sibling']['run_id'] == sibling['run_id']
    with pytest.raises(CoordinatorConflict):
        reopened.control('local', 'owner', cid, 'child', Control(
            action='retry', run_id=failed['run_id'], idempotency_key='stale',
        ))
    with pytest.raises(CoordinatorScopeError):
        reopened.control('local', 'foreign', cid, 'child', control)


def test_failed_child_retry_recovers_after_effect_commit_before_binding(coordinator, monkeypatch):
    cid, child, failed, _, _ = _bound_failed_child(coordinator, monkeypatch)
    replacement_id = 'run_crash_retry'
    coordinator.service.active_work_effect_run_id = lambda record, *, idempotency_key: replacement_id

    def crash_after_effect(*args, **kwargs):
        coordinator.store.create_run(child['worker_id'], failed['project_id'],
                                     'Exact continued child', run_id=replacement_id)
        raise RuntimeError('synthetic post-commit crash')

    coordinator.service.execute_active_work_action = crash_after_effect
    control = Control(action='retry', run_id=failed['run_id'], idempotency_key='retry-crash')
    with pytest.raises(CoordinatorConflict):
        coordinator.control('local', 'owner', cid, 'child', control)
    assert coordinator.goal_snapshot('local', 'owner', cid, 'child')['run_id'] == failed['run_id']
    restarted = CoordinatorService(coordinator.store, coordinator.service, Provider())
    restarted.recover_retry_actions('local', 'owner', cid)
    assert restarted.goal_snapshot('local', 'owner', cid, 'child')['run_id'] == replacement_id
    assert restarted.control('local', 'owner', cid, 'child', control)['run_id'] == replacement_id


def test_stop_winning_retry_race_cancels_replacement(coordinator, monkeypatch):
    cid, child, failed, _, _ = _bound_failed_child(coordinator, monkeypatch)
    entered, release = Event(), Event()
    replacement_id = 'run_lost_retry'
    stopped = []
    coordinator.service.active_work_effect_run_id = lambda record, *, idempotency_key: replacement_id
    coordinator.service.stop_run = lambda worker_id, run_id: stopped.append(run_id) or {'accepted': True}

    def retry(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        coordinator.store.create_run(child['worker_id'], failed['project_id'],
                                     'Exact continued child', run_id=replacement_id)
        return {'run_id': replacement_id, 'state': 'queued'}

    coordinator.service.execute_active_work_action = retry
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(coordinator.control, 'local', 'owner', cid, 'child', Control(
            action='retry', run_id=failed['run_id'], idempotency_key='race-retry',
        ))
        assert entered.wait(5)
        coordinator.control('local', 'owner', cid, 'child', Control(
            action='stop', run_id=failed['run_id'], idempotency_key='race-stop',
        ))
        release.set()
        with pytest.raises(CoordinatorConflict):
            pending.result(5)
    assert replacement_id in stopped
    assert coordinator._goal(cid, 'child')['intent_state'] == 'cancelled'
    assert coordinator._goal(cid, 'child')['run_id'] == failed['run_id']


def test_second_retry_key_cannot_start_while_first_is_pending(coordinator, monkeypatch):
    cid, child, failed, _, _ = _bound_failed_child(coordinator, monkeypatch)
    entered, release = Event(), Event()
    replacement_id = 'run_pending_retry'
    calls = []
    coordinator.service.active_work_effect_run_id = lambda record, *, idempotency_key: replacement_id

    def retry(*args, **kwargs):
        calls.append(kwargs['idempotency_key'])
        entered.set()
        assert release.wait(5)
        coordinator.store.create_run(child['worker_id'], failed['project_id'],
                                     'Exact continued child', run_id=replacement_id)
        return {'run_id': replacement_id, 'state': 'queued'}

    coordinator.service.execute_active_work_action = retry
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(coordinator.control, 'local', 'owner', cid, 'child', Control(
            action='retry', run_id=failed['run_id'], idempotency_key='retry-first',
        ))
        assert entered.wait(5)
        with pytest.raises(CoordinatorConflict, match='already in progress'):
            coordinator.control('local', 'owner', cid, 'child', Control(
                action='retry', run_id=failed['run_id'], idempotency_key='retry-second',
            ))
        release.set()
        assert pending.result(5)['run_id'] == replacement_id
    assert len(calls) == 1


@pytest.mark.parametrize('phase', ['before_binding', 'after_binding', 'bound_success'])
def test_real_retry_dispatch_binding_and_exact_stop(tmp_path, monkeypatch, phase):
    class CountingRuntime(StubRuntime):
        def __init__(self):
            self.invocations = []

        def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
            self.invocations.append(run_id)
            return super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)

    store = Store(tmp_path / 'state.sqlite3')
    runtime = CountingRuntime()
    service = WorkersProjectsService(
        store, runtime, reconcile_on_startup=False, start_background_consumers=False,
    )
    core = CoordinatorService(store, service, Provider())
    cid = create(core)
    core.accept_turn('local', 'owner', cid, 'turn', 'Exact original request',
                     [Goal(id='child', text='Exact child goal')])
    work = store.reserve_delegation(
        tenant_id='local', owner_id='owner', idempotency_key='real-retry-source',
        request_digest='real-retry-source-digest', origin_ref='real-retry-origin',
        title='Exact child', goal='Exact child goal', instruction='Exact child instruction',
        origin_surface='coordinator', worker_name='Child', worker_role='research',
        profile='openclaw-general', backend='openclaw', runtime='openclaw-stub',
        model='stub-model', execution_mode='docker',
    )
    worker_id = str(work['worker_id'])
    source_id = str(work['run_id'])
    store.update_run(source_id, state='failed', failure_class='host_capacity', failure_retryable=1)
    with store._connect() as conn:
        conn.execute(
            "UPDATE coordinator_goals SET work_ref=?,worker_id=?,run_id=? "
            "WHERE conversation_id=? AND goal_id='child'",
            (work['work_ref'], worker_id, source_id, cid),
        )
    entered, release = Event(), Event()
    if phase == 'before_binding':
        gate_seen = Event()
        original_ready = service._coordinator_retry_dispatch_ready

        def observed_ready(worker, run):
            ready = original_ready(worker, run)
            if not ready:
                gate_seen.set()
            return ready

        monkeypatch.setattr(service, '_coordinator_retry_dispatch_ready', observed_ready)
        original_action = service.execute_active_work_action

        def delayed_action(*args, **kwargs):
            outcome = original_action(*args, **kwargs)
            entered.set()  # A real queued replacement exists, but the goal is not rebound.
            assert release.wait(5)
            return outcome

        monkeypatch.setattr(service, 'execute_active_work_action', delayed_action)
    else:
        original_start = service.start_assigned_run

        def delayed_start(worker_id):
            entered.set()  # The replacement is bound; actual processor dispatch waits.
            assert release.wait(5)
            return original_start(worker_id)

        monkeypatch.setattr(service, 'start_assigned_run', delayed_start)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(core.control, 'local', 'owner', cid, 'child', Control(
                action='retry', run_id=source_id, idempotency_key='real-retry',
            ))
            assert entered.wait(5)
            replacement_id = service.active_work_effect_run_id(
                store.get_delegation(work['work_ref'], tenant_id='local', owner_id='owner'),
                idempotency_key=f'coordinator-retry:{cid}:child:real-retry',
            )
            assert store.get_run(replacement_id)['state'] == 'queued'
            if phase == 'before_binding':
                service.start_assigned_run(worker_id)  # Simulate an independent scheduler wake.
                assert gate_seen.wait(5)
                assert store.get_run(replacement_id)['state'] == 'queued'
                assert runtime.invocations == []
            if phase != 'bound_success':
                targeted_run_id = source_id if phase == 'before_binding' else replacement_id
                core.control('local', 'owner', cid, 'child', Control(
                    action='stop', run_id=targeted_run_id, idempotency_key='real-stop',
                ))
            release.set()
            if phase == 'before_binding':
                with pytest.raises(CoordinatorConflict):
                    pending.result(5)
            else:
                pending.result(5)
        if phase == 'bound_success':
            deadline = time.monotonic() + 5
            while (store.get_run(replacement_id) or {}).get('state') not in {
                'completed', 'failed', 'cancelled'
            } and time.monotonic() < deadline:
                time.sleep(0.01)
        service.shutdown()
        if phase == 'bound_success':
            assert runtime.invocations == [replacement_id]
            assert store.get_run(replacement_id)['state'] == 'completed'
            assert core._goal(cid, 'child')['run_id'] == replacement_id
        else:
            assert runtime.invocations == []
            assert store.get_run(replacement_id)['state'] == 'cancelled'
            assert core._goal(cid, 'child')['intent_state'] == 'cancelled'
    finally:
        release.set()
        service.shutdown()
        store.close()


def test_paused_child_stop_uses_exact_work_stop_and_survives_reload(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id='a', text='goal')])
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    worker = coordinator.store.create_worker(
        project_id, 'owner', 'Child', 'Child', 'grok-build',
        'grok-build', 'grok-build', 'exact-native-model', execution_mode='host',
    )
    run = coordinator.store.create_run(worker['worker_id'], project_id, 'goal')
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE runs SET state='paused' WHERE run_id=?", (run['run_id'],))
        conn.execute("UPDATE workers SET state='paused' WHERE worker_id=?", (worker['worker_id'],))
        conn.execute(
            "UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='a'",
            (worker['worker_id'], run['run_id'], cid),
        )

    stop_attempts = 0

    def stop_exact(worker_id, run_id):
        nonlocal stop_attempts
        assert (worker_id, run_id) == (worker['worker_id'], run['run_id'])
        stop_attempts += 1
        if stop_attempts == 1:
            return {'accepted': False, 'confirmation_pending': False}
        if stop_attempts == 2:
            return {'accepted': True, 'confirmation_pending': True}
        coordinator.store.finalize_run_if_state(
            run_id, expected_state='paused', state='cancelled', error_text='Stopped by operator',
        )
        return {'accepted': True, 'confirmation_pending': False}

    coordinator.service.stop_run = stop_exact
    control = Control(action='stop', run_id=run['run_id'], idempotency_key='stop-paused')
    with pytest.raises(CoordinatorConflict, match='not accepted'):
        coordinator.control('local', 'owner', cid, 'a', control)
    pending = coordinator.control('local', 'owner', cid, 'a', control)
    assert pending['confirmation_pending'] is True
    assert (coordinator.store.get_run(run['run_id']) or {})['state'] == 'paused'
    result = coordinator.control('local', 'owner', cid, 'a', control)
    assert result['state'] == 'cancelled'
    assert coordinator.control('local', 'owner', cid, 'a', control) == result
    assert stop_attempts == 3
    reopened = CoordinatorService(coordinator.store, coordinator.service, Provider())
    assert reopened.snapshot('local', 'owner', cid)['goals'][0]['state'] == 'cancelled'


def test_prompt_source_manifest_is_byte_exact():
    import hashlib
    source = Path(__file__).parents[1] / 'src' / prompt_manifest()['source']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == prompt_manifest()['sha256']


def test_real_store_reservation_replays_after_lost_attachment(tmp_path, monkeypatch):
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.service import WorkersProjectsService

    monkeypatch.setenv('GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED', 'false')
    monkeypatch.setenv('WPR_MODEL_CODEX_CLI', 'gpt-5.6-sol')
    monkeypatch.setenv('VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY', '1')
    store = Store(tmp_path / 'integration.sqlite3')
    class ReadyRuntime(StubRuntime):
        def isolated_parallel_readiness(self):
            return {'ready': True, 'reason': ''}
    service = WorkersProjectsService(store, ReadyRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _: None
    monkeypatch.setattr(service, '_storage_pressure_v1', lambda: {'healthy': True})
    core = CoordinatorService(store, service, Provider())
    try:
        cid = core.create('local', 'owner', CoordinatorConfig(model='exact-native-model', effort='high', routes=[Route(id='route',profile='codex-cli',model='codex-cli:gpt-5.6-sol',effort='high',execution_mode='docker')]))['conversation_id']
        core.accept_turn('local','owner',cid,'turn','raw',[Goal(id='a',text='a')])
        assert service.orchestration_capabilities()['isolatedParallelReady'], service.orchestration_capabilities()
        command = Dispatch(goal_id='a',route_id='route',instruction='Exact goal')
        result = core.dispatch('local','owner',cid,command)
        assert result['state'] == 'queued', result['blocker']
        original = result['run_id']
        # Simulate crash after service reservation but before coordinator attachment.
        with store._connect() as conn:
            conn.execute("UPDATE coordinator_goals SET work_ref='',worker_id='',run_id='' WHERE conversation_id=?", (cid,))
        recovered = CoordinatorService(store, service, Provider()).recover_dispatches('local','owner',cid)
        assert recovered[0]['run_id'] == original
        with store._connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1
            assert conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0] == 1
        from workers_projects_runtime.coordinator import guard_coordinator_run
        with store._connect() as conn:
            conn.execute("UPDATE coordinator_goals SET work_ref='',worker_id='',run_id='',intent_state='cancelled' WHERE conversation_id=?", (cid,))
        with store._connect() as conn, pytest.raises(CoordinatorConflict):
            guard_coordinator_run(conn, original)
        stopped = core.recover_dispatches('local','owner',cid)
        assert stopped[0]['run_id'] == original
        assert store.get_run(original)['state'] == 'cancelled'
    finally:
        service.shutdown()
        store.close()


def test_packaged_coordinator_uses_isolated_owner_box_and_exact_native_route(tmp_path, monkeypatch):
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.service import WorkersProjectsService

    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'local-linux')
    monkeypatch.setenv('GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED', 'false')
    monkeypatch.setenv('WPR_MODEL_GROK_BUILD', 'grok-4.6')
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setenv('XPERFECT_SHARED_VOLUME_ROOT', str(data))
    store = Store(tmp_path / 'standalone.sqlite3')
    account = {'account_id': 'owned-grok', 'provider': 'grok', 'status': 'ready', 'is_default': True}

    class Accounts:
        def list_provider_accounts(self, *, tenant_id, owner_id):
            return [account] if (tenant_id, owner_id) == ('local', 'owner') else []

        def get_provider_account(self, *, account_id, tenant_id, owner_id):
            return account if (account_id, tenant_id, owner_id) == ('owned-grok', 'local', 'owner') else None

    service = WorkersProjectsService(store, StubRuntime(), control_plane_store=Accounts(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _: None
    core = CoordinatorService(store, service, Provider())
    try:
        cid = core.create('local', 'owner', CoordinatorConfig(model='exact-native-model', effort='high',
            scope=CoordinatorScope(execution_mode='docker'), routes=[Route(
                id='grok', profile='grok-build', model='grok-build:grok-4.6',
                effort='default', execution_mode='docker')]))['conversation_id']
        core.accept_turn('local', 'owner', cid, 'turn', 'Delegate', [
            Goal(id='child', text='Exact goal'), Goal(id='sibling', text='Second exact goal'),
        ])
        result = core.dispatch('local', 'owner', cid, Dispatch(goal_id='child', route_id='grok', instruction='Exact goal'))
        assert result['state'] == 'queued', result
        worker = store.get_worker(result['worker_id'])
        bundle = __import__('json').loads(worker['bootstrap_bundle_json'])
        assert worker['model'] == 'grok-4.6'
        assert worker['workspace_id'] != core.snapshot('local', 'owner', cid)['scope']['workspace_id']
        assert store.get_execution_workspace(worker['workspace_id'], 'local', 'owner')['mode'] == 'isolated'
        assert bundle['provider_account'] == {'policy': 'personal_required', 'account_id': 'owned-grok'}
        assert 'execution_policy' not in bundle and 'viventium_launch_authority' not in bundle
        assert core.dispatch('local', 'owner', cid, Dispatch(goal_id='child', route_id='grok', instruction='Exact goal'))['run_id'] == result['run_id']
        sibling = core.dispatch('local', 'owner', cid, Dispatch(goal_id='sibling', route_id='grok', instruction='Second exact goal'))
        assert sibling['state'] == 'queued', sibling
        assert sibling['run_id'] != result['run_id']
        assert sibling['worker_id'] != result['worker_id']
        assert store.get_worker(sibling['worker_id'])['workspace_id'] != worker['workspace_id']
        goals = {goal['goal_id']: goal for goal in core.snapshot('local', 'owner', cid)['goals']}
        assert goals['child']['run_id'] == result['run_id']
        assert goals['sibling']['run_id'] == sibling['run_id']
    finally:
        service.shutdown()
        store.close()


def test_stop_before_provider_start_persists_and_uses_existing_tombstone(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw')
    calls = []
    coordinator.provider.cancel_by_idempotency = lambda key, owner, **kwargs: calls.append((key,owner,kwargs)) or {'state':'cancelled'}
    coordinator.cancel_turn('local', 'owner', cid, 'turn')
    assert coordinator.start_turn('local', 'owner', cid, 'turn')['state'] == 'cancelled'
    assert calls == [(f'{cid}:turn','owner',{'tenant_id':'local'})]


def test_stop_unstarted_goal_is_durable_and_replayed(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'raw', [Goal(id='a', text='a')])
    stop = Control(action='stop', idempotency_key='stop')
    first = coordinator.control('local','owner',cid,'a',stop)
    assert first['state'] == 'cancelled'
    assert coordinator.control('local','owner',cid,'a',stop) == first
    with pytest.raises(CoordinatorConflict):
        coordinator.dispatch('local','owner',cid,Dispatch(goal_id='a',route_id='route',instruction='a'))
    assert coordinator.recover_dispatches('local','owner',cid) == []


def test_native_projection_reuses_peer_binder_when_discovery_is_off(coordinator, monkeypatch):
    cid = create(coordinator)
    coordinator.store.get_provider_session_by_worker = lambda _: {'agent_id':'coordinator','tenant_id':'local','owner_id':'owner','conversation_id':cid}
    peers = SimpleNamespace(mint_native_session=lambda worker,run: 'synthetic-run-bound-token')
    worker = coordinator.bind_native_worker({'worker_id':'a'}, {'run_id':'r'}, peers, 'http://127.0.0.1:8766/v1/native/coordinator/')
    from workers_projects_runtime.coordinator_mcp import project_coordinator_bootstrap
    result = project_coordinator_bootstrap(worker, {'claude_project_mcp':{'mcpServers':{'existing':{'url':'https://example.test/mcp'}}}})
    assert result['env']['GLASSHIVE_PEER_TOKEN'] == 'synthetic-run-bound-token'
    assert 'existing' in result['claude_project_mcp']['mcpServers']
    assert 'xperfect-coordinator' in result['claude_project_mcp']['mcpServers']
    with pytest.raises(CoordinatorScopeError):
        project_coordinator_bootstrap({**worker,'_active_run_id':'new'}, {})
    with pytest.raises(CoordinatorScopeError):
        coordinator.bind_native_worker({'worker_id':'a'}, {'run_id':'r'}, peers, 'http://outside.example/v1/native/coordinator/')
    endpoint = 'http://runtime:8766/v1/native/coordinator/'
    with pytest.raises(CoordinatorScopeError):
        coordinator.bind_native_worker({'worker_id':'a'}, {'run_id':'r'}, peers, endpoint)
    # A package never uses plaintext TCP: only its box socket, when the hub serves it.
    from workers_projects_runtime import native_transport
    monkeypatch.setattr(native_transport, '_hub', None)
    monkeypatch.setenv('GLASSHIVE_PEER_RUNTIME_BASE_URL', 'http://runtime:8766')
    monkeypatch.setenv('XPERFECT_CONTROL_ROOT', '/control')
    for profile in ('local-linux', 'hosted-xfs'):
        monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', profile)
        with pytest.raises(CoordinatorScopeError, match='unavailable'):
            coordinator.bind_native_worker({'worker_id':'a'}, {'run_id':'r'}, peers, endpoint)
        monkeypatch.setattr(native_transport, '_hub', SimpleNamespace(running=True))
        projection = coordinator.bind_native_worker({'worker_id':'a'}, {'run_id':'r'}, peers, endpoint)['_coordinator_native_projection']
        assert projection['url'] == 'http+unix://%2Fworkspace%2Fdata%2F.xperfect-runtime.sock/v1/native/coordinator/'
        assert projection['transport'] == 'stdio'
        monkeypatch.setattr(native_transport, '_hub', None)


def test_result_wake_batches_once_and_yields_to_interactive(coordinator):
    from workers_projects_runtime.coordinator import canonical, now
    cid = create(coordinator)
    coordinator.accept_turn('local','owner',cid,'interactive','raw')
    with coordinator.store._connect() as conn:
        conn.execute("INSERT INTO coordinator_events(conversation_id,kind,identity,payload_json,created_at) VALUES(?,?,?,?,?)", (cid,'result_available','run-a',canonical({'goal_id':'a','run_id':'run-a','state':'completed'}),now()))
    assert coordinator.queue_result_turn('local','owner',cid) is None
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET response_json='{}' WHERE conversation_id=?", (cid,))
    turn = coordinator.queue_result_turn('local','owner',cid)
    assert turn.startswith('results-')
    assert coordinator.queue_result_turn('local','owner',cid) is None
    state = coordinator.snapshot('local','owner',cid)
    assert len(state['turns']) == 2
    assert state['turns'][1]['origin'] == 'worker_results'


@pytest.mark.parametrize('output', ['ALPHA-23', 'ALPHA-23 ' + 'x' * 14000])
def test_result_wake_carries_exact_small_output_and_marks_large_remainder(coordinator, output):
    from workers_projects_runtime.coordinator import canonical, now
    import hashlib
    import json

    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'combine results', [Goal(id='a', text='first')])
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    worker = coordinator.store.create_worker(
        project_id, 'owner', 'Helper', 'coordinator', 'grok-build',
        'grok-build', 'grok-build', 'grok-selected', execution_mode='host',
        trusted_run_lane='conversation',
    )
    run = coordinator.store.create_run(worker['worker_id'], project_id, 'first')
    coordinator.store.finalize_run(run['run_id'], state='completed', output_text=output)
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET response_json='{}' WHERE conversation_id=?", (cid,))
        conn.execute("UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='a'",
                     (worker['worker_id'], run['run_id'], cid))
        conn.execute("INSERT INTO coordinator_events(conversation_id,kind,identity,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (cid, 'result_available', run['run_id'], canonical({'goal_id':'a','run_id':run['run_id'],'state':'completed'}), now()))
    turn = coordinator.queue_result_turn('local', 'owner', cid)
    with coordinator.store._connect() as conn:
        saved = conn.execute("SELECT message FROM coordinator_turns WHERE conversation_id=? AND turn_id=?", (cid, turn)).fetchone()[0]
    result = json.loads(saved)['items'][0]['result']
    assert result['output_text'] == output[:12000]
    assert result['next_offset'] == (12000 if len(output) > 12000 else None)
    assert result['total_chars'] == len(output)
    assert result['sha256'] == hashlib.sha256(output.encode()).hexdigest()


def test_result_wake_waits_for_dispatched_sibling_to_release_account(coordinator):
    from workers_projects_runtime.coordinator import canonical, now

    cid = create(coordinator)
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'two goals', [
        Goal(id='a', text='first'), Goal(id='b', text='second'),
    ])
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    worker = coordinator.store.create_worker(
        project_id, 'owner', 'Sibling', 'coordinator', 'grok-build',
        'grok-build', 'grok-build', 'grok-selected', execution_mode='host',
        trusted_run_lane='conversation',
    )
    run = coordinator.store.create_run(worker['worker_id'], project_id, 'second')
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE coordinator_turns SET response_json='{}' WHERE conversation_id=?", (cid,))
        conn.execute("UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='b'",
                     (worker['worker_id'], run['run_id'], cid))
        conn.execute("INSERT INTO coordinator_events(conversation_id,kind,identity,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (cid, 'result_available', 'run-a', canonical({'goal_id':'a','run_id':'run-a','state':'completed'}), now()))
    assert coordinator.queue_result_turn('local', 'owner', cid) is None
    with coordinator.store._connect() as conn:
        assert conn.execute("SELECT delivered_turn_id FROM coordinator_events WHERE conversation_id=?", (cid,)).fetchone()[0] == ''
        conn.execute("UPDATE runs SET state='completed',ended_at=? WHERE run_id=?", (now(), run['run_id']))
    assert coordinator.queue_result_turn('local', 'owner', cid).startswith('results-')


def test_foreground_run_result_does_not_wake_a_duplicate_reply(coordinator):
    from workers_projects_runtime.coordinator import canonical, now

    cid = create(coordinator)
    project_id = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    worker = coordinator.store.create_worker(
        project_id, 'owner', 'Foreground', 'coordinator', 'grok-build',
        'grok-build', 'grok-build', 'grok-selected', execution_mode='host',
        trusted_run_lane='conversation',
    )
    run = coordinator.store.create_run(worker['worker_id'], project_id, 'synthetic task')
    session = coordinator.store.upsert_provider_session(
        tenant_id='local', owner_id='owner', conversation_id=cid,
        agent_id='coordinator', model_id='grok-selected', project_id=project_id,
        worker_id=worker['worker_id'], workspace_dir='/synthetic', access_mode='owner',
    )
    request, _ = coordinator.store.create_provider_request(
        tenant_id='local', owner_id='owner', session_id=session['session_id'],
        idempotency_key='synthetic', message_id='turn', stream_id='',
        requested_history_count=0,
    )
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'synthetic task')
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE provider_requests SET run_id=?, state='completed' WHERE request_id=?",
                     (run['run_id'], request['request_id']))
        conn.execute("UPDATE coordinator_turns SET request_id=?, response_json='{}' WHERE conversation_id=? AND turn_id='turn'",
                     (request['request_id'], cid))
        conn.execute("INSERT INTO coordinator_events(conversation_id,kind,identity,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (cid, 'result_available', run['run_id'], canonical({'run_id':run['run_id']}), now()))
    assert coordinator.queue_result_turn('local', 'owner', cid) is None
    assert len(coordinator.snapshot('local', 'owner', cid)['turns']) == 1
    with coordinator.store._connect() as conn:
        assert conn.execute("SELECT delivered_turn_id FROM coordinator_events WHERE conversation_id=?", (cid,)).fetchone()[0] == 'foreground'


def test_owner_api_does_not_accept_arbitrary_bootstrap(coordinator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from workers_projects_runtime.coordinator_api import install_coordinator_routes
    app=FastAPI()
    config=CoordinatorConfig(model='exact-native-model',effort='high')
    install_coordinator_routes(app,coordinator,lambda request:('local',request.headers.get('x-test-owner','owner')),lambda tenant,owner:config)
    client=TestClient(app)
    cid=client.post('/v1/coordinator/conversations').json()['conversation_id']
    response=client.post(f'/v1/coordinator/conversations/{cid}/turns',json={'idempotency_key':'t','message':'raw','bootstrap_bundle':{'env':{'TOKEN':'untrusted'}}})
    assert response.status_code == 422
    assert client.get(f'/v1/coordinator/conversations/{cid}',headers={'x-test-owner':'foreign'}).status_code == 403
    accepted=client.post(f'/v1/coordinator/conversations/{cid}/turns',json={'idempotency_key':'t','message':'raw'})
    assert accepted.status_code == 200
    assert accepted.json()['state'] == 'blocked'


def test_owner_api_accepts_only_validated_origin_scope(coordinator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from workers_projects_runtime.coordinator_api import install_coordinator_routes

    project = coordinator.store.create_project('owner', 'Scoped', 'goal', 'codex-cli')
    app = FastAPI()
    config = CoordinatorConfig(model='exact-native-model', effort='high')
    install_coordinator_routes(
        app, coordinator,
        lambda request: ('local', request.headers.get('x-test-owner', 'owner')),
        lambda tenant, owner: config,
    )
    client = TestClient(app)
    scope = {'project_id': project['project_id']}
    created = client.post('/v1/coordinator/conversations', json={'scope': scope})
    assert created.status_code == 200, created.text
    assert created.json()['scope']['project_id'] == project['project_id']
    with coordinator.store._connect() as conn:
        persisted = conn.execute(
            'SELECT scope_json FROM coordinator_conversations WHERE conversation_id=?',
            (created.json()['conversation_id'],),
        ).fetchone()
    assert project['project_id'] in persisted['scope_json']
    assert client.post('/v1/coordinator/conversations', json={
        'scope': scope, 'model': 'caller-model',
    }).status_code == 422
    assert client.post('/v1/coordinator/conversations', json={
        'scope': scope,
    }, headers={'x-test-owner': 'foreign'}).status_code == 403


def test_owner_api_selects_only_owned_ready_native_account(coordinator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from workers_projects_runtime.coordinator_api import install_coordinator_routes

    coordinator.service.control_plane_store = SimpleNamespace(
        get_provider_account=lambda *, account_id, tenant_id, owner_id: (
            {'account_id': account_id, 'provider': 'grok', 'status': 'ready'}
            if account_id == 'ready-grok' and owner_id == 'owner' else None
        ),
        list_connections=lambda **kwargs: [],
    )
    coordinator.service._resolve_worker_model = lambda profile, mode, **_: 'grok-4.6'
    coordinator.provider._model = lambda model_id, **_: SimpleNamespace(
        id=model_id, harness_profile='grok-build', native_model='grok-4.6',
        recommended_effort='default', effort_choices=('default',),
    )
    app = FastAPI()
    default = CoordinatorConfig(model='exact-native-model', effort='high',
                                scope=CoordinatorScope(execution_mode='docker'))
    install_coordinator_routes(app, coordinator,
        lambda request: ('local', request.headers.get('x-test-owner', 'owner')),
        lambda tenant, owner, profile='': default if profile == 'grok-build' else (_ for _ in ()).throw(AssertionError('Explicit account must not depend on unrelated default')))
    client = TestClient(app)
    project = coordinator.store.create_project('owner', 'Selected', 'goal', 'grok-build')
    created = client.post('/v1/coordinator/conversations', json={
        'account_id': 'ready-grok', 'scope': {'project_id': project['project_id']},
    })
    assert created.status_code == 200, created.text
    cid = created.json()['conversation_id']
    with coordinator.store._connect() as conn:
        saved = conn.execute('SELECT config_json,scope_json FROM coordinator_conversations WHERE conversation_id=?', (cid,)).fetchone()
    config = CoordinatorConfig.model_validate_json(saved['config_json'])
    assert config.model == 'grok-build:grok-4.6'
    assert config.routes[0].connection_id == 'ready-grok'
    assert config.scope.execution_mode == 'docker'
    assert config.scope.connection_id == 'ready-grok'
    assert config.scope.project_id == project['project_id']
    assert client.post('/v1/coordinator/conversations', json={'account_id': 'foreign'},
                       headers={'x-test-owner': 'foreign'}).status_code == 409
    assert client.post('/v1/coordinator/conversations', json={
        'account_id': 'ready-grok', 'scope': {'connection_id': 'foreign'},
    }).status_code == 409


def test_standalone_conversation_gets_one_owned_origin(coordinator):
    from workers_projects_runtime.models import ProjectResponse

    cid = create(coordinator)
    first = coordinator.snapshot('local', 'owner', cid)['scope']['project_id']
    project = coordinator.store.get_project(first, tenant_id='local', owner_id='owner')
    assert project['origin_scope']['surface'] == 'coordinator'
    assert ProjectResponse(**project).origin_surface == 'coordinator'
    coordinator.accept_turn('local', 'owner', cid, 'one', 'first')
    coordinator.accept_turn('local', 'owner', cid, 'two', 'second')
    assert coordinator.snapshot('local', 'owner', cid)['scope']['project_id'] == first
    assert coordinator.snapshot('local', 'owner', create(coordinator))['scope']['project_id'] != first


def test_real_owner_api_blocks_foreground_after_origin_is_narrowed(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app
    from workers_projects_runtime.openclaw_runtime import StubRuntime

    monkeypatch.setenv('GLASSHIVE_COORDINATOR_CONFIG_JSON', json.dumps({
        'model': 'codex-cli:gpt-5.6-sol', 'effort': 'high', 'routes': [],
    }))
    app = create_app(str(tmp_path / 'scope.sqlite3'), runtime_backend='stub', runtime=StubRuntime())
    with TestClient(app) as client:
        project = client.post('/v1/projects', json={
            'owner_id': 'demo-owner', 'title': 'Scoped', 'goal': 'Check scope',
            'default_worker_profile': 'codex-cli',
        }).json()
        project_id = project['project_id']
        created = client.post('/v1/coordinator/conversations', json={
            'scope': {'project_id': project_id},
        })
        assert created.status_code == 200, created.text
        assert created.json()['scope']['project_id'] == project_id
        policy = client.put(f'/v1/projects/{project_id}/execution-policy', json={
            'expected_revision': 0,
            'policy': {'version': 1, 'mode': 'selected', 'harnesses': []},
        })
        assert policy.status_code == 200, policy.text
        turn = client.post(
            f"/v1/coordinator/conversations/{created.json()['conversation_id']}/turns",
            json={'idempotency_key': 'synthetic-turn', 'message': 'Check policy'},
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()['state'] == 'blocked'
        assert turn.json()['blocker'] == 'allowed_ai_denied'


def test_removed_origin_blocks_new_turn_but_keeps_status_readable(coordinator):
    project = coordinator.store.create_project('owner', 'Scoped', 'goal', 'codex-cli')
    config = CoordinatorConfig(
        model='exact-native-model', effort='high',
        scope=CoordinatorScope(project_id=project['project_id']),
    )
    cid = coordinator.create('local', 'owner', config)['conversation_id']
    coordinator.accept_turn('local', 'owner', cid, 'turn', 'saved message')
    with coordinator.store._connect() as conn:
        conn.execute('DELETE FROM projects WHERE project_id=?', (project['project_id'],))
    state = coordinator.snapshot('local', 'owner', cid)
    assert state['turns'][0]['message'] == 'saved message'
    assert state['scope']['project_id'] == project['project_id']
    with pytest.raises(CoordinatorScopeError):
        coordinator.start_turn('local', 'owner', cid, 'turn')


def test_native_tools_all_have_canonical_descriptions(coordinator):
    import asyncio
    from workers_projects_runtime.coordinator_mcp import native_coordinator_server, coordinator_tool_manifest
    server = native_coordinator_server(coordinator, SimpleNamespace(), 'http://127.0.0.1:8766/v1/native/coordinator/')
    tools = asyncio.run(server.list_tools())
    expected = coordinator_tool_manifest()['tools']
    assert {tool.name:tool.description for tool in tools} == expected
    assert 'goal_ids' in next(t for t in tools if t.name=='coordinator_handle').inputSchema['properties']


def test_future_coordinator_schema_is_rejected_without_downgrade(coordinator):
    from workers_projects_runtime.schema_version import UnsupportedSchemaVersionError
    with coordinator.store._connect() as conn:
        conn.execute("UPDATE glasshive_schema_versions SET version=99 WHERE component='coordinator'")
    with pytest.raises(UnsupportedSchemaVersionError):
        CoordinatorService(coordinator.store,coordinator.service,coordinator.provider)
    with coordinator.store._connect() as conn:
        assert conn.execute("SELECT version FROM glasshive_schema_versions WHERE component='coordinator'").fetchone()[0] == 99


def test_restore_hold_blocks_refresh_start_dispatch_and_wakes_until_exact_resume(coordinator):
    cid = create(coordinator)
    coordinator.accept_turn(
        'local', 'owner', cid, 'turn', 'raw', [Goal(id='goal', text='preserve intent')]
    )
    with coordinator.store._connect() as conn:
        conn.execute('UPDATE coordinator_conversations SET restore_hold=1 WHERE conversation_id=?', (cid,))
        conn.execute('UPDATE coordinator_turns SET restore_hold=1 WHERE conversation_id=?', (cid,))
        conn.execute('UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?', (cid,))
    hold_hash = coordinator._hold_digest(['turn:turn', 'goal:goal'])
    with coordinator.store._connect() as conn:
        conn.execute('UPDATE coordinator_conversations SET restore_hold_set_hash=? WHERE conversation_id=?', (hold_hash, cid))
    assert coordinator.start_turn('local', 'owner', cid, 'turn')['state'] == 'restore_held'
    assert coordinator.refresh('local', 'owner', cid)['restore_hold'] is True
    assert coordinator.queue_result_turn('local', 'owner', cid) is None
    assert coordinator.reconcile_once() == []
    with pytest.raises(CoordinatorConflict):
        coordinator.dispatch('local', 'owner', cid, Dispatch(goal_id='goal', route_id='route', instruction='run'))
    resumed = coordinator.resume_restored('local', 'owner', cid, hold_hash, approved_turn_ids=['turn'])
    assert resumed['restore_hold'] is True
    assert resumed['restore_hold_set_hash'] == coordinator._hold_digest(['goal:goal'])
    resumed = coordinator.resume_restored(
        'local', 'owner', cid, resumed['restore_hold_set_hash'], approved_goal_ids=['goal']
    )
    assert resumed['restore_hold'] is False


def test_viewer_and_signed_link_cannot_read_whole_conversation():
    from fastapi import HTTPException
    from workers_projects_runtime.auth import AuthContext
    from workers_projects_runtime.coordinator_api import coordinator_owner_scope
    for context in [AuthContext(role='viewer',auth_mode='signed_internal_assertion'),AuthContext(role='member',auth_mode='signed_link')]:
        with pytest.raises(HTTPException):
            coordinator_owner_scope(context,'local','owner')
    assert coordinator_owner_scope(AuthContext(role='member',auth_mode='local'),'local','owner') == ('local','owner')


def test_large_result_pages_are_exact_and_overview_does_not_duplicate_output(coordinator):
    text = 'é and exact bytes\n' * 10000
    coordinator.goal_snapshot = lambda *args: {'goal_id':'g','run_id':'r','state':'completed','output_text':text,'error_text':''}
    first=coordinator.read_result('local','owner','c','g',0,64000)
    second=coordinator.read_result('local','owner','c','g',first['next_offset'],64000)
    output=first['output_text']+second['output_text']; cursor=second['next_offset']
    while cursor is not None:
        page=coordinator.read_result('local','owner','c','g',cursor,64000)
        assert page['sha256']==first['sha256']
        output+=page['output_text'];cursor=page['next_offset']
    assert output==text
    assert 'output_text' not in coordinator._goal_summary(coordinator.goal_snapshot())


def test_default_config_preserves_owner_effort_and_exact_catalog_model(coordinator, monkeypatch):
    from workers_projects_runtime.coordinator_config import configured_coordinator
    monkeypatch.delenv('GLASSHIVE_COORDINATOR_CONFIG_JSON',raising=False)
    coordinator.store.get_user_preferences=lambda tenant,owner: {'default_worker_profile':'codex-cli','codex_reasoning_effort':'high'}
    coordinator.service.store=coordinator.store
    coordinator.service._resolve_worker_model=lambda profile,mode,**_:'gpt-5.6-sol'
    config=configured_coordinator(coordinator.service,coordinator.provider,'local','owner')
    assert config.model=='codex-cli:gpt-5.6-sol'
    assert config.effort=='high'
    coordinator.service._resolve_worker_model=lambda profile,mode,**_:'unregistered-native-model'
    with pytest.raises(CoordinatorConflict):
        configured_coordinator(coordinator.service,coordinator.provider,'local','owner')


def test_selected_grok_account_uses_exact_model_with_native_effort(coordinator, monkeypatch):
    from workers_projects_runtime.coordinator_config import configured_coordinator
    monkeypatch.delenv('GLASSHIVE_COORDINATOR_CONFIG_JSON', raising=False)
    monkeypatch.setenv('WPR_GROK_REASONING_EFFORT', 'high')
    coordinator.store.get_user_preferences = lambda tenant, owner: {'default_worker_profile': 'codex-cli'}
    coordinator.service.store = coordinator.store
    coordinator.service._resolve_worker_model = lambda profile, mode, **_: 'grok-4.6' if profile == 'grok-build' else ''
    selected = configured_coordinator(coordinator.service, coordinator.provider, 'local', 'owner', 'grok-build')
    assert selected.model == 'grok-build:grok-4.6'
    assert selected.effort == 'default'
    assert selected.scope.execution_mode == 'docker'


def test_default_config_accepts_unconfigured_codex_native_default(coordinator, monkeypatch):
    from workers_projects_runtime.coordinator_config import configured_coordinator
    from workers_projects_runtime.conversation_provider import ConversationProvider

    monkeypatch.delenv('GLASSHIVE_COORDINATOR_CONFIG_JSON', raising=False)
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'host')
    coordinator.store.get_user_preferences = lambda tenant, owner: {'default_worker_profile': 'codex-cli'}
    coordinator.service.store = coordinator.store
    coordinator.service._resolve_worker_model = lambda profile, mode, **_: ''
    config = configured_coordinator(coordinator.service, coordinator.provider, 'local', 'owner')
    assert config.model == 'codex-cli:native-default'
    assert config.routes[0].model == config.model
    assert config.effort == 'medium'
    actual = CoordinatorService(
        coordinator.store, coordinator.service, ConversationProvider.__new__(ConversationProvider),
    ).create('local', 'owner', config)
    assert actual['model'] == config.model


def test_default_config_uses_the_selected_substrate_model(coordinator, monkeypatch):
    from workers_projects_runtime.coordinator_config import configured_coordinator

    monkeypatch.delenv('GLASSHIVE_COORDINATOR_CONFIG_JSON', raising=False)
    monkeypatch.setenv('WPR_DEFAULT_EXECUTION_MODE', 'docker')
    coordinator.store.get_user_preferences = lambda tenant, owner: {'default_worker_profile': 'codex-cli'}
    coordinator.service.store = coordinator.store
    coordinator.service._resolve_worker_model = lambda profile, mode, **_: {
        'host': '', 'docker': 'gpt-5.4',
    }[mode]
    config = configured_coordinator(coordinator.service, coordinator.provider, 'local', 'owner')
    assert config.model == 'codex-cli:gpt-5.4'
    assert config.routes[0].execution_mode == 'docker'
    assert config.scope.execution_mode == 'docker'
