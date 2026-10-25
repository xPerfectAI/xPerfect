"""A human desktop action never resumes or republishes the AI lifecycle."""
from threading import Event, Thread

import pytest

from workers_projects_runtime.control_plane import ControlPlaneConflict
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store
from test_api import truthfully_invoke_test_run


class HumanDesktopRuntime(StubRuntime):
    def __init__(self):
        self.desktop_workers = []
        self.desktop_hook = None
        self.pause_entered, self.pause_release = Event(), Event()
        self.pause_release.set()
        self.ensure_calls = self.reconcile_calls = self.terminate_calls = 0
        self.compute_active = False

    def ensure_worker_ready(self, worker):
        self.ensure_calls += 1
        return super().ensure_worker_ready(worker)

    def reconcile_worker(self, worker):
        self.reconcile_calls += 1
        return super().reconcile_worker(worker)

    def desktop_action(self, worker, action, *, url=None, run_id=None):
        self.desktop_workers.append({**worker, 'action_run_id': run_id})
        if self.desktop_hook:
            self.desktop_hook()
        self.compute_active = True
        return {'action': action, 'status': 'launched', 'notes': 'Human surface opened'}

    def pause_worker(self, worker):
        self.pause_entered.set()
        assert self.pause_release.wait(3), 'Pause fixture was not released'
        return super().pause_worker(worker)

    def terminate_worker(self, worker):
        self.terminate_calls += 1
        self.compute_active = False
        return super().terminate_worker(worker)


@pytest.fixture
def desktop_service(tmp_path):
    runtime = HumanDesktopRuntime()
    store = Store(str(tmp_path/'runtime.db'))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = service.create_project('synthetic-owner', 'Human surface', 'Preserve AI state', 'codex-cli')
    worker = service.create_worker(project_id=project['project_id'], owner_id='synthetic-owner',
        name='Research worker', role='operator', profile='codex-cli', backend='openclaw', start_synchronously=False)
    store.update_worker(worker['worker_id'], state='ready', last_error='Existing worker diagnostic',
                        pid=303, session_key='synthetic-session')
    runtime.ensure_calls = runtime.reconcile_calls = 0
    try:
        yield service, store, runtime, worker
    finally:
        runtime.pause_release.set()
        service.shutdown()


def thread_call(function):
    result, errors = [], []
    def call():
        try:
            result.append(function())
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=call)
    thread.start()
    return thread, result, errors


@pytest.mark.parametrize('state', ['ready', 'paused', 'needs_input'])
def test_desktop_preserves_ai_state_error_generation_and_only_touches_activity(desktop_service, state):
    service, store, runtime, worker = desktop_service
    worker_id = worker['worker_id']
    store.update_worker(worker_id, state=state)
    before = store.get_worker(worker_id)
    response = service.desktop_action(worker_id, 'browser', url='about:blank')
    after = store.get_worker(worker_id)
    assert response['status'] == 'launched'
    assert runtime.desktop_workers[-1]['state'] == state
    assert {key: value for key, value in after.items() if key not in {'updated_at', 'last_activity_at'}} == {
        key: value for key, value in before.items() if key not in {'updated_at', 'last_activity_at'}}
    assert after['updated_at'] >= before['updated_at']
    assert after['last_activity_at'] >= before['last_activity_at']
    assert runtime.ensure_calls == runtime.reconcile_calls == 0
    assert [row['event_type'] for row in store.list_events(worker_id)].count('worker.desktop_action') == 1


def test_failure_does_not_rewrite_current_ai_state_or_diagnostic(desktop_service):
    service, store, runtime, worker = desktop_service
    store.update_worker(worker['worker_id'], state='paused')
    before = store.get_worker(worker['worker_id'])
    def fail():
        raise RuntimeError('Synthetic display unavailable')
    runtime.desktop_hook = fail
    with pytest.raises(RuntimeError, match='display unavailable'):
        service.desktop_action(worker['worker_id'], 'browser')
    assert store.get_worker(worker['worker_id']) == before
    assert runtime.ensure_calls == runtime.reconcile_calls == 0
    assert 'worker.desktop_action' not in {event['event_type'] for event in store.list_events(worker['worker_id'])}


def test_exact_paused_run_remains_paused_without_runtime_resume(desktop_service):
    service, store, runtime, worker = desktop_service
    queued = store.create_run(worker['worker_id'], worker['project_id'], 'Paused native work')
    run = truthfully_invoke_test_run(store, store.get_worker(worker['worker_id']), queued,
                                     suffix='desktop-paused-run')
    paused = service.pause_worker(worker['worker_id'], run_id=run['run_id'])
    assert paused['state'] == 'paused'
    before = store.get_run(run['run_id'])
    assert before['state'] == 'paused'
    service.desktop_action(worker['worker_id'], 'browser')
    assert store.get_run(run['run_id']) == before
    assert store.get_worker(worker['worker_id'])['state'] == 'paused'
    assert runtime.desktop_workers[-1]['state'] == 'paused'
    assert runtime.ensure_calls == runtime.reconcile_calls == 0


def test_pause_after_desktop_serializes_and_remains_paused(desktop_service):
    service, store, runtime, worker = desktop_service
    entered, release, pause_started = Event(), Event(), Event()
    def desktop():
        entered.set()
        assert release.wait(3), 'Desktop fixture was not released'
    runtime.desktop_hook = desktop
    act, _, errors = thread_call(lambda: service.desktop_action(worker['worker_id'], 'browser'))
    pause = None
    try:
        assert entered.wait(2)
        def paused():
            pause_started.set()
            return service.pause_worker(worker['worker_id'])
        pause, result, pause_errors = thread_call(paused)
        assert pause_started.wait(2)
        assert not runtime.pause_entered.wait(.05)
    finally:
        release.set()
        act.join(3)
        if pause:
            pause.join(3)
    assert not act.is_alive() and not pause.is_alive()
    assert not errors and not pause_errors
    assert result[0]['state'] == store.get_worker(worker['worker_id'])['state'] == 'paused'
    assert store.has_active_operator_pause(worker['worker_id']) is True
    assert runtime.ensure_calls == runtime.reconcile_calls == 0


def test_pause_wins_before_desktop_and_is_read_after_the_control_lock(desktop_service):
    service, store, runtime, worker = desktop_service
    runtime.pause_release.clear()
    pause, _, pause_errors = thread_call(lambda: service.pause_worker(worker['worker_id']))
    act = None
    try:
        assert runtime.pause_entered.wait(2)
        act, _, errors = thread_call(lambda: service.desktop_action(worker['worker_id'], 'browser'))
        runtime.pause_release.set()
        pause.join(3)
        act.join(3)
    finally:
        runtime.pause_release.set()
        pause.join(3)
        if act:
            act.join(3)
    assert not pause.is_alive() and not act.is_alive()
    assert not pause_errors and not errors
    assert runtime.desktop_workers[-1]['state'] == 'paused'
    assert store.get_worker(worker['worker_id'])['state'] == 'paused'
    assert runtime.ensure_calls == runtime.reconcile_calls == 0


def test_new_confirmed_run_during_desktop_keeps_exact_run_and_runtime_fields(desktop_service):
    service, store, runtime, worker = desktop_service
    entered, release = Event(), Event()
    def desktop():
        entered.set()
        assert release.wait(3), 'Desktop fixture was not released'
    runtime.desktop_hook = desktop
    act, _, errors = thread_call(lambda: service.desktop_action(worker['worker_id'], 'browser'))
    try:
        assert entered.wait(2)
        queued = store.create_run(worker['worker_id'], worker['project_id'], 'New accepted work')
        run = truthfully_invoke_test_run(store, store.get_worker(worker['worker_id']), queued,
                                         suffix='desktop-new-run')
        store.update_worker(worker['worker_id'], state='running', last_run_id=run['run_id'],
                            pid=919, session_key='new-confirmed-session')
        before = store.get_worker(worker['worker_id'])
    finally:
        release.set()
        act.join(3)
    assert not act.is_alive() and not errors
    after = store.get_worker(worker['worker_id'])
    assert {key: value for key, value in after.items() if key not in {'updated_at', 'last_activity_at'}} == {
        key: value for key, value in before.items() if key not in {'updated_at', 'last_activity_at'}}
    assert store.get_run(run['run_id'])['state'] == 'running'
    assert runtime.ensure_calls == runtime.reconcile_calls == 0


@pytest.mark.parametrize('fail', [False, True])
def test_close_during_desktop_cleans_late_runtime_without_reopening_or_success_event(desktop_service, fail):
    service, store, runtime, worker = desktop_service
    entered, release = Event(), Event()
    def desktop():
        entered.set()
        assert release.wait(3), 'Desktop fixture was not released'
        if fail:
            runtime.compute_active = True
            raise RuntimeError('Synthetic partial display failure')
    runtime.desktop_hook = desktop
    act, _, errors = thread_call(lambda: service.desktop_action(worker['worker_id'], 'browser'))
    try:
        assert entered.wait(2)
        assert service.terminate_worker(worker['worker_id'])['state'] == 'terminated'
    finally:
        release.set()
        act.join(3)
    assert not act.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ControlPlaneConflict)
    assert runtime.compute_active is False and runtime.terminate_calls == 2
    assert store.get_worker(worker['worker_id'])['state'] == 'terminated'
    assert 'worker.desktop_action' not in {event['event_type'] for event in store.list_events(worker['worker_id'])}


def test_already_closed_worker_never_reaches_runtime(desktop_service):
    service, store, runtime, worker = desktop_service
    service.terminate_worker(worker['worker_id'])
    with pytest.raises(ControlPlaneConflict, match='closed'):
        service.desktop_action(worker['worker_id'], 'browser')
    assert runtime.desktop_workers == []


def test_legacy_adapter_without_run_argument_preserves_pause(desktop_service):
    service, store, runtime, worker = desktop_service
    store.update_worker(worker['worker_id'], state='paused')
    def legacy(worker, action, *, url=None):
        assert worker['state'] == 'paused'
        return {'status': 'available', 'notes': action}
    runtime.desktop_action = legacy
    assert service.desktop_action(worker['worker_id'], 'browser')['status'] == 'available'
    assert store.get_worker(worker['worker_id'])['state'] == 'paused'
    assert runtime.ensure_calls == runtime.reconcile_calls == 0
