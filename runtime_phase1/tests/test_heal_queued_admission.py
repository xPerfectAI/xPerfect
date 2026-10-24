"""Visible reads preserve accepted work before a native process exists."""
from dataclasses import replace

import pytest

from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store, NONTERMINAL_RUN_STATES


class IdleProbeRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.probes = []
        self.during_probe = None

    def reconcile_worker(self, worker):
        self.probes.append(worker['worker_id'])
        if self.during_probe:
            self.during_probe()
        return replace(super().reconcile_worker(worker), pid=None)


@pytest.fixture
def idle(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'runtime.db'))
    runtime = IdleProbeRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False,
                                    start_background_consumers=False)
    project = store.create_project('synthetic-owner', 'Accepted work', 'Keep accepted work.', 'codex-cli')
    worker = store.create_worker(project_id=project['project_id'], owner_id='synthetic-owner',
        name='Acceptance worker', role='worker', profile='codex-cli', backend='codex-cli',
        execution_mode='host', runtime='codex-cli', model='synthetic-model')
    store.update_worker_state(worker['worker_id'], 'starting')
    wakes = []
    monkeypatch.setattr(service, '_ensure_worker_processor', lambda worker_id: wakes.append(worker_id))
    try:
        yield service, store, runtime, project, worker, wakes
    finally:
        service.shutdown()


def test_heal_read_keeps_accepted_queue_before_native_start(idle):
    service, store, runtime, project, worker, wakes = idle
    run = store.create_run(worker['worker_id'], project['project_id'], 'Read the saved file.')
    result = service.heal_worker(worker['worker_id'])
    assert result['state'] == 'starting'
    assert store.get_run(run['run_id'])['state'] == 'queued'
    assert store.get_worker(worker['worker_id'])['last_run_id'] == run['run_id']
    assert runtime.probes == []
    assert wakes == [worker['worker_id']]


def test_heal_read_rechecks_claimed_generation_under_lifecycle_lock(idle, monkeypatch):
    service, store, runtime, project, worker, wakes = idle
    original = service._acquire_worker_lifecycle_guard
    claimed = {}

    def claim_before_guarded_read(worker_id, *, timeout_s=None):
        guard = original(worker_id, timeout_s=timeout_s)
        run = store.create_run(worker_id, project['project_id'], 'Claim before the read.')
        claimed.update(store.claim_next_queued_run(worker_id))
        assert claimed['run_id'] == run['run_id']
        return guard

    monkeypatch.setattr(service, '_acquire_worker_lifecycle_guard', claim_before_guarded_read)
    service.heal_worker(worker['worker_id'])
    assert store.get_run(claimed['run_id'])['state'] == 'claimed'
    assert runtime.probes == []
    assert wakes == []


def test_heal_idle_probe_cannot_overwrite_a_concurrent_accepted_receipt(idle):
    service, store, runtime, project, worker, wakes = idle
    accepted = {}
    runtime.during_probe = lambda: accepted.update(store.create_run(
        worker['worker_id'], project['project_id'], 'Accepted while process was observed.'))
    result = service.heal_worker(worker['worker_id'])
    assert result['state'] == 'starting'
    assert store.get_run(accepted['run_id'])['state'] == 'queued'
    assert wakes == [worker['worker_id']]


@pytest.mark.parametrize('state', ['paused', 'needs_input', 'stopping', 'terminating', 'terminated'])
def test_heal_read_does_not_resume_explicit_lifecycle_states(idle, state):
    service, store, runtime, project, worker, wakes = idle
    run = store.create_run(worker['worker_id'], project['project_id'], 'Accepted work.')
    store.update_worker_state(worker['worker_id'], state)
    result = service.heal_worker(worker['worker_id'])
    assert result['state'] == state
    assert store.get_run(run['run_id'])['state'] == 'queued'
    assert runtime.probes == []
    assert wakes == []


def test_heal_read_preserves_durable_operator_pause_marker(idle):
    service, store, runtime, project, worker, wakes = idle
    run = store.create_run(worker['worker_id'], project['project_id'], 'Accepted work.')
    store.add_event(project['project_id'], worker['worker_id'], None, 'worker.paused', 'Paused by operator')
    result = service.heal_worker(worker['worker_id'])
    assert result['state'] == 'starting'
    assert store.get_run(run['run_id'])['state'] == 'queued'
    assert runtime.probes == []
    assert wakes == []


@pytest.mark.parametrize('state', sorted(NONTERMINAL_RUN_STATES))
def test_idle_reconcile_cas_refuses_each_accepted_nonterminal_state(idle, state):
    _service, store, _runtime, project, worker, _wakes = idle
    store.create_run(worker['worker_id'], project['project_id'], 'Accepted work.', state=state)
    # Take the exact current worker generation, so only the accepted-run SQL fence
    # prevents an idle observation from changing its state.
    current = store.get_worker(worker['worker_id'])
    updated = store.reconcile_idle_worker_runtime(worker['worker_id'],
        expected_updated_at=current['updated_at'], expected_last_run_id=current['last_run_id'],
        state='paused', pid=None)
    assert updated is None
    assert store.get_worker(worker['worker_id'])['state'] == current['state']


def test_heal_idle_without_work_records_compute_reclamation(idle):
    service, store, runtime, _project, worker, wakes = idle
    result = service.heal_worker(worker['worker_id'])
    assert result['state'] == 'paused'
    assert result['compute_released_at']
    assert runtime.probes == [worker['worker_id']]
    assert wakes == []


def test_heal_read_does_not_wait_for_an_owned_lifecycle_guard(idle, monkeypatch):
    service, store, runtime, project, worker, wakes = idle
    run = store.create_run(worker['worker_id'], project['project_id'], 'Accepted work.')
    original = service._acquire_worker_lifecycle_guard
    held = original(worker['worker_id'])
    observed_timeouts = []

    def observe(worker_id, *, timeout_s=None):
        observed_timeouts.append(timeout_s)
        assert timeout_s == 0.0
        return original(worker_id, timeout_s=timeout_s)

    monkeypatch.setattr(service, '_acquire_worker_lifecycle_guard', observe)
    try:
        result = service.heal_worker(worker['worker_id'])
    finally:
        held.release()
    assert result['state'] == 'starting'
    assert store.get_run(run['run_id'])['state'] == 'queued'
    assert observed_timeouts == [0.0]
    assert runtime.probes == [] and wakes == []


def test_explicit_resume_recovers_the_same_paused_queued_receipt(idle):
    service, store, _runtime, project, worker, wakes = idle
    run = store.create_run(worker['worker_id'], project['project_id'], 'Previously accepted work.')
    store.update_worker_state(worker['worker_id'], 'paused')
    service.resume_worker(worker['worker_id'])
    queued = store.list_nonterminal_runs_for_worker(worker['worker_id'])
    assert [item['run_id'] for item in queued] == [run['run_id']]
    assert queued[0]['state'] == 'queued'
    assert store.get_worker(worker['worker_id'])['state'] == 'ready'
    assert wakes == [worker['worker_id']]


@pytest.mark.parametrize('field', ['compute_release_token', 'work_stop_id'])
def test_heal_read_does_not_wake_owned_compute_or_stop_boundaries(idle, field):
    service, store, runtime, project, worker, wakes = idle
    run = store.create_run(worker['worker_id'], project['project_id'], 'Accepted work.')
    store.update_worker(worker['worker_id'], **{field: 'synthetic-current-control'})
    service.heal_worker(worker['worker_id'])
    assert store.get_run(run['run_id'])['state'] == 'queued'
    assert store.get_worker(worker['worker_id'])[field] == 'synthetic-current-control'
    assert runtime.probes == [] and wakes == []
