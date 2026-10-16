"""Cancellation keeps its own durable callback off unrelated lifecycle work."""
from unittest.mock import Mock

from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store, RunRestorationState


def service_fixture(tmp_path):
    store = Store(str(tmp_path / 'state.sqlite3'))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False,
                                    start_background_consumers=False)
    project = store.create_project('owner', 'Synthetic lifecycle', 'Exact cancellation', 'direct-xai')
    worker = store.create_worker(project_id=project['project_id'], owner_id='owner', name='Synthetic',
        role='worker', profile='direct-xai', backend='direct-xai', runtime='direct-xai',
        model='grok-4.7', execution_mode='host',
        bootstrap_bundle={'callbacks': {'events_webhook_url':'http://callback.invalid/events'}})
    service.executor.submit = Mock()
    return store, service, worker


def enqueue(store, worker, kind, token):
    run = store.create_run(worker['worker_id'], worker['project_id'], 'Synthetic cancellation', state='cancelled')
    with store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        effect = store._enqueue_lifecycle_effects(connection, operation_token=token, operation_epoch=1,
            operation_kind='cancel_run', worker_id=worker['worker_id'], run_id=run['run_id'],
            effect_kinds=(kind,))[0]
        connection.execute('COMMIT')
    return effect


def test_typed_drain_keeps_immediate_cancellation_and_existing_global_recovery(tmp_path, monkeypatch):
    store, service, worker = service_fixture(tmp_path)
    own = enqueue(store, worker, 'callback.run_cancelled', 'own-cancel')
    other = enqueue(store, worker, 'callback.worker_paused', 'other-operation')
    claims = []
    claim = store.claim_next_lifecycle_effect
    def observe(*args, **kwargs):
        claims.append(kwargs['effect_kinds'])
        return claim(*args, **kwargs)
    monkeypatch.setattr(store, 'claim_next_lifecycle_effect', observe)
    try:
        service._replay_pending_lifecycle_effects(effect_kinds=('callback.run_cancelled',))
        rows = {row['effect_id']: row for row in store.list_lifecycle_operation_effects(worker_id=worker['worker_id'])}
        assert rows[own]['status'] == 'applied'
        assert rows[other]['status'] == 'pending'
        assert store.get_callback_outbox('cb_effect_' + own)['event_type'] == 'run.cancelled'
        assert store.get_callback_outbox('cb_effect_' + other) is None
        assert claims == [('callback.run_cancelled',)] * 2
        claims.clear()
        service._replay_pending_lifecycle_effects()
        rows = {row['effect_id']: row for row in store.list_lifecycle_operation_effects(worker_id=worker['worker_id'])}
        assert rows[other]['status'] == 'applied'
        assert store.get_callback_outbox('cb_effect_' + other)['event_type'] == 'worker.paused'
        assert {('signed_links.revoke_worker',), ('callback.worker_terminated',),
                ('callback.run_cancelled',), ('callback.worker_paused',)} <= set(claims)
    finally:
        service.shutdown(); store.close()


def test_cancel_owner_filters_only_after_exact_stop_and_terminal_cas(tmp_path, monkeypatch):
    store, service, worker = service_fixture(tmp_path)
    run = store.create_run(worker['worker_id'], worker['project_id'], 'Synthetic cancellation', state=RunRestorationState.RUNNING)
    calls = []
    monkeypatch.setattr(service, '_requires_exact_host_control', lambda _worker: True)
    def confirmed_stop(_worker_id, _run):
        calls.append('stop-and-terminal-cas')
        return {'run': {'run_id': run['run_id'], 'state': 'cancelled'}, 'pending': False}
    monkeypatch.setattr(service, '_cancel_running_host_run', confirmed_stop)
    monkeypatch.setattr(service, '_replay_pending_lifecycle_effects', lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(service, '_wake_host_capacity_waiters', lambda _worker: None)
    try:
        service.cancel_run(worker['worker_id'], run['run_id'])
        assert calls == ['stop-and-terminal-cas', {'effect_kinds': ('callback.run_cancelled',)}]
        calls.clear()
        monkeypatch.setattr(service, '_cancel_running_host_run', Mock(side_effect=RuntimeError('stop unconfirmed')))
        import pytest
        with pytest.raises(RuntimeError, match='stop unconfirmed'):
            service.cancel_run(worker['worker_id'], run['run_id'])
        assert calls == []
    finally:
        service.shutdown(); store.close()
