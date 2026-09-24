"""Lease the explicit OS-native account without copying any credential artifact."""
from contextlib import contextmanager
from threading import Lock

from .control_plane import ControlPlaneError
from .current_native_account import PREFIX, MARKER, require_local, verify_identity


@contextmanager
def bind_current_account(binder, worker, *, account, selection, runtime_name, run_id,
                         timeout_sec, lease_purpose, abort_binding):
    require_local(binder.homes)
    if runtime_name != 'claude-code' or worker.get('execution_mode') != 'host' or abort_binding is None:
        raise ControlPlaneError('Existing Claude sign-in requires its local native runtime and exact stop callback')
    if selection.policy != 'personal_required':
        raise ControlPlaneError('Existing Claude sign-in requires the selected account without fallback')
    tenant = str(worker.get('tenant_id') or 'local')
    owner = str(worker.get('owner_id') or '')
    worker_id = str(worker.get('worker_id') or '')
    if not owner or not worker_id or not run_id or account.get('status') != 'ready':
        raise ControlPlaneError('Selected existing sign-in is not ready; verify it in Connections')
    identity = account['secret_locator'][len(PREFIX):]
    token = binder._reserve_active_route(worker, runtime_name=runtime_name, run_id=run_id,
        account_id=selection.account_id, route_kind='native')
    lease = None
    stop_event = thread = None
    confirmed_stop = False
    closing = False
    stop_lock = Lock()
    bound = {**worker, '_active_run_id': run_id, '_glasshive_provider_account_bound': True,
             MARKER: {'account_id': selection.account_id, 'identity': identity, 'lease_id': ''},
             '_glasshive_provider_auth_source': 'current_os_claude_subscription'}

    def assert_lease():
        if closing or confirmed_stop or lease is None:
            raise ControlPlaneError('Current native account lease has ended')
        active = binder.store.active_provider_account_lease(selection.account_id)
        if (active is None or active['lease_id'] != lease['lease_id']
                or active['tenant_id'] != tenant or active['owner_id'] != owner
                or active['worker_id'] != worker_id or active['run_id'] != run_id):
            raise ControlPlaneError('Current native account lease is no longer valid')
        import time
        if active['expires_at'] <= time.time():
            raise ControlPlaneError('Current native account lease expired')

    bound['_glasshive_current_native_assert_lease'] = assert_lease

    def stop_native():
        nonlocal confirmed_stop
        with stop_lock:
            if not confirmed_stop:
                # Runtime must refuse success unless this run's native generation stopped.
                abort_binding(bound)
                confirmed_stop = True

    try:
        lease = binder.store.acquire_provider_lease(account_id=selection.account_id, tenant_id=tenant, owner_id=owner,
            lane=f'claude-code:current-native:{lease_purpose}', worker_id=worker_id, run_id=run_id,
            ttl_seconds=binder._lease_ttl_seconds(timeout_sec), required_recovery_code='')
        bound[MARKER]['lease_id'] = lease['lease_id']
        binder._update_active_route(worker_id, token, worker=bound, lease_id=lease['lease_id'])
        try:
            verify_identity(identity)
        except ControlPlaneError as exc:
            binder.store.update_provider_account_status(account_id=selection.account_id, tenant_id=tenant, owner_id=owner,
                status='action_required', reconnect_reason=str(exc))
            # Nothing native has been launched, so release does not need a process callback.
            confirmed_stop = True
            raise
        stop_event, thread, lost = binder._start_lease_heartbeat(lease_id=lease['lease_id'], tenant_id=tenant,
            owner_id=owner, ttl_seconds=binder._lease_ttl_seconds(timeout_sec), worker_id=worker_id,
            runtime_name=runtime_name, account_id=selection.account_id, on_lease_lost=stop_native)
        yield bound
        if lost.is_set():
            raise ControlPlaneError('Current native account lease was lost; verify it after process recovery')
    finally:
        closing = True
        binder._begin_close_active_route(worker_id, token)
        try:
            if lease is not None:
                try:
                    if not confirmed_stop:
                        stop_native()
                except BaseException:
                    binder.store.update_provider_account_status(account_id=selection.account_id, tenant_id=tenant, owner_id=owner,
                        status='action_required', reconnect_reason='The exact native process could not be stopped. Recover that workspace before reconnecting',
                        recovery_code='credential_cleanup_failed')
                    raise
                finally:
                    if stop_event is not None:
                        stop_event.set()
                        thread.join(timeout=2)
                if thread is not None and thread.is_alive():
                    raise ControlPlaneError('Current native lease heartbeat has not stopped; account remains reserved')
                binder.store.release_provider_lease(lease_id=lease['lease_id'], tenant_id=tenant, owner_id=owner)
        finally:
            binder._finalize_close_active_route(worker_id, token)
