from dataclasses import asdict, replace
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from workers_projects_runtime.control_plane import ControlPlaneStore, ProviderProjectionPending
from workers_projects_runtime.credential_projection import CredentialProjection, CredentialProjectionError, ProjectionBinding
from workers_projects_runtime.mission_provider_accounts import MissionProviderAccountBinder
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager
from workers_projects_runtime.provider_credential_artifacts import credential_artifacts
from workers_projects_runtime.provider_projection_binding import NativeProviderProjection, _transaction_lock
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from workers_projects_runtime.control_plane import ControlPlaneError


@pytest.fixture
def harness(tmp_path):
    root = tmp_path.resolve()
    binder = MissionProviderAccountBinder(db_path=str(root / 'control.sqlite3'), home_root=root / 'accounts')
    store = binder.store
    account = store.create_provider_account(tenant_id='tenant', owner_id='owner', provider='codex', label='Synthetic', auth_method='subscription', platform_support='supported', secret_locator='native-home://synthetic', status='ready')
    worker = {'tenant_id':'tenant', 'owner_id':'owner', 'worker_id':'worker', 'workspace_id':'workspace', 'profile':'codex-cli', 'execution_mode':'docker',
              'bootstrap_bundle_json': json.dumps({'run_mode':'mission','provider_account':{'account_id':account['account_id'],'policy':'personal_required'}})}
    home = ProviderAccountHomeManager(binder.home_root).ensure_home(tenant_id='tenant', owner_id='owner', account_id=account['account_id'], provider='codex')
    canonical = home / 'codex/auth.json'; canonical.write_text('{"refresh":"before"}'); canonical.chmod(0o600)
    member, control = root / 'member', root / 'receipts'
    member.mkdir(mode=0o700); control.mkdir(mode=0o700)
    observed = {'stops':0,'grants':0}
    def stop(): observed['stops'] += 1
    def factory(*, worker, account_home, lease, attempt_id, assert_lease):
        binding = ProjectionBinding('tenant','owner',account['account_id'],lease['lease_id'],'worker','workspace',lease['run_id'],attempt_id,'a'*64,20001)
        tx = CredentialProjection(account_root=account_home, member_home=member, receipt=control / (lease['lease_id']+'.json'), binding=binding,
                                  artifacts=credential_artifacts('codex','subscription'), assert_lease=assert_lease, stop_member=stop)
        def grant():
            assert store.pending_provider_projections(account_id=account['account_id'])
            assert (member / '.codex/auth.json').read_text() == canonical.read_text()
            observed['grants'] += 1
        value = NativeProviderProjection(tx, {'CODEX_HOME':'/private/member/.codex'}, {}, grant)
        observed['projected'] = value
        return value
    return binder, store, account, worker, canonical, member, factory, observed


def bind(h):
    binder, _, _, worker, _, _, factory, _ = h
    return binder.bind(worker, runtime_name='codex-cli', run_id='run', attempt_id='attempt', timeout_sec=5, projection_factory=factory)


def recovery_factory(h):
    _, _, _, _, canonical, member, _, observed = h
    def factory(*, record, assert_lease):
        tx = CredentialProjection(account_root=canonical.parent.parent, member_home=member, receipt=Path(record['metadata']['receipt']),
                                  binding=ProjectionBinding(**record['binding']), artifacts=credential_artifacts('codex','subscription'), assert_lease=assert_lease,
                                  stop_member=lambda: observed.update(stops=observed['stops']+1))
        return NativeProviderProjection(tx, {'CODEX_HOME':'/private/member/.codex'}, {}, lambda:None)
    return factory


def test_projection_refresh_stops_before_release_and_has_no_canonical_mount(harness):
    binder, store, account, worker, canonical, member, factory, seen = harness
    with bind(harness) as bound:
        assert '_glasshive_provider_account_mount_host' not in bound
        assert '_glasshive_provider_api_key_file' not in bound
        assert bound['_glasshive_provider_account_projected']
        lease = store.active_provider_account_lease(account['account_id'])
        with pytest.raises(ProviderProjectionPending):
            store.release_provider_lease(lease_id=lease['lease_id'], tenant_id='tenant', owner_id='owner')
        (member / '.codex/auth.json').write_text('{"refresh":"after"}')
    assert seen['stops'] == 2 and seen['grants'] == 1
    assert canonical.read_text() == '{"refresh":"after"}'
    assert not (member / '.codex/auth.json').exists()
    assert store.pending_provider_projections() == []
    assert store.active_provider_account_lease(account['account_id']) is None


def test_cleanup_failure_blocks_expired_new_setup_disconnect_and_preferred_fallback(harness):
    binder, store, account, worker, canonical, member, _, seen = harness
    with pytest.raises(RuntimeError, match='stop uncertain'):
        with bind(harness):
            def uncertain(): raise RuntimeError('stop uncertain')
            seen['projected'].transaction.stop_member = uncertain
            (member / '.codex/auth.json').write_text('{"refresh":"after"}')
    lease = store.active_provider_account_lease(account['account_id'])
    assert lease is not None
    with sqlite3.connect(store.db_path) as db:
        db.execute('UPDATE provider_account_leases SET expires_at = 1')
    assert store.active_provider_account_lease(account['account_id']) is not None
    for lane in ['codex-cli:mission','codex-cli:interactive','setup','verify','delete']:
        with pytest.raises(ProviderProjectionPending):
            store.acquire_provider_lease(account_id=account['account_id'],tenant_id='tenant',owner_id='owner',lane=lane,worker_id='other',run_id='next',ttl_seconds=15,allowed_statuses=('action_required',))
    with pytest.raises(ProviderProjectionPending): store.disconnect_provider_account(account_id=account['account_id'],tenant_id='tenant',owner_id='owner')
    with pytest.raises(ProviderProjectionPending): store.forget_provider_account(account_id=account['account_id'],tenant_id='tenant',owner_id='owner')
    worker['bootstrap_bundle_json'] = json.dumps({'provider_account':{'account_id':account['account_id'],'policy':'personal_preferred'}})
    with pytest.raises(ProviderProjectionPending):
        with binder.bind(worker | {'execution_mode':'host'},runtime_name='codex-cli',run_id='fallback',timeout_sec=1): pass
    assert canonical.read_text() == '{"refresh":"before"}'
    assert (member / '.codex/auth.json').exists()


def test_restart_recovery_finishes_exact_original_lease_and_cleans_native_copy(harness):
    binder, store, account, worker, canonical, member, _, seen = harness
    with pytest.raises(RuntimeError):
        with bind(harness):
            seen['projected'].transaction.stop_member = lambda: (_ for _ in ()).throw(RuntimeError('uncertain'))
            (member / '.codex/auth.json').write_text('{"refresh":"after"}')
    record = store.pending_provider_projections()[0]
    restarted = MissionProviderAccountBinder(db_path=store.db_path, home_root=binder.home_root)
    restarted.recover_projection(record, projection_factory=recovery_factory(harness))
    assert store.pending_provider_projections() == []
    assert canonical.read_text() == '{"refresh":"after"}'
    assert not (member / '.codex/auth.json').exists()
    assert store.active_provider_account_lease(account['account_id']) is None
    # Recovery never fabricates fresh native/provider verification.
    assert store.get_provider_account_record(account_id=account['account_id'],tenant_id='tenant',owner_id='owner')['status'] == 'action_required'


def test_canonical_cas_conflict_quarantines_and_recovery_never_overwrites(harness):
    binder, store, account, _, canonical, member, _, _ = harness
    with pytest.raises(CredentialProjectionError, match='revision'):
        with bind(harness): canonical.write_text('{"refresh":"new-canonical"}')
    with pytest.raises(CredentialProjectionError, match='revision'):
        binder.recover_projection(store.pending_provider_projections()[0],projection_factory=recovery_factory(harness))
    assert canonical.read_text() == '{"refresh":"new-canonical"}'
    assert store.pending_provider_projections()


def test_new_generation_wrong_attempt_and_concurrent_recovery_are_denied(harness):
    binder, store, account, _, _, _, _, seen = harness
    with pytest.raises(RuntimeError):
        with bind(harness): seen['projected'].transaction.stop_member = lambda: (_ for _ in ()).throw(RuntimeError('uncertain'))
    record = store.pending_provider_projections()[0]
    wrong = dict(record['binding'], container_id='b'*64)
    with pytest.raises(ProviderProjectionPending): store.claim_provider_projection_recovery(binding=wrong)
    with pytest.raises(ProviderProjectionPending): store.claim_provider_projection_recovery(binding=dict(record['binding'],attempt_id='other'))
    token = store.claim_provider_projection_recovery(binding=record['binding'])
    with pytest.raises(ProviderProjectionPending): store.claim_provider_projection_recovery(binding=record['binding'])
    with pytest.raises(ProviderProjectionPending): store.assert_provider_projection(binding=record['binding'])
    store.assert_provider_projection(binding=record['binding'],recovery_token=token)


def test_prepare_failure_before_receipt_releases_only_after_absence_proof(harness):
    _, store, account, _, canonical, member, _, seen = harness
    canonical.unlink()
    with pytest.raises(CredentialProjectionError):
        with bind(harness): pass
    assert seen['grants'] == 0
    assert store.pending_provider_projections() == []
    assert store.active_provider_account_lease(account['account_id']) is None
    assert not (member / '.codex/auth.json').exists()


def test_ledger_survives_expired_lease_and_missing_receipt_crash(harness):
    binder, store, account, worker, canonical, member, factory, seen = harness
    lease=store.acquire_provider_lease(account_id=account['account_id'],tenant_id='tenant',owner_id='owner',lane='codex-cli:mission',worker_id='worker',run_id='run',ttl_seconds=15)
    projected=factory(worker=worker,account_home=canonical.parent.parent,lease=lease,attempt_id='attempt',assert_lease=lambda:None)
    from workers_projects_runtime.provider_projection_binding import _metadata
    record=store.begin_provider_projection(binding=asdict(projected.transaction.binding),metadata=_metadata(projected))
    with sqlite3.connect(store.db_path) as db: db.execute('UPDATE provider_account_leases SET expires_at=1')
    binder.recover_projection(record,projection_factory=recovery_factory(harness))
    assert not store.pending_provider_projections()
    assert store.active_provider_account_lease(account['account_id']) is None
    assert canonical.read_text() == '{"refresh":"before"}'


def test_controller_transaction_lock_prevents_recovery_race(harness):
    binder, store, _, _, _, _, _, seen = harness
    with pytest.raises(RuntimeError):
        with bind(harness): seen['projected'].transaction.stop_member = lambda: (_ for _ in ()).throw(RuntimeError('uncertain'))
    with _transaction_lock(seen['projected'].transaction):
        with pytest.raises(ProviderProjectionPending,match='still running'):
            binder.recover_projection(store.pending_provider_projections()[0],projection_factory=recovery_factory(harness))


def test_crash_after_complete_receipt_before_ledger_commit_recovers_without_recopy(harness, monkeypatch):
    binder, store, account, _, canonical, member, _, _ = harness
    original = store.complete_provider_projection
    monkeypatch.setattr(store, 'complete_provider_projection', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('commit interrupted')))
    with pytest.raises(RuntimeError,match='commit interrupted'):
        with bind(harness): (member / '.codex/auth.json').write_text('{"refresh":"after"}')
    record = store.pending_provider_projections()[0]
    assert json.loads(Path(record['metadata']['receipt']).read_text())['phase'] == 'complete'
    assert canonical.read_text() == '{"refresh":"after"}'
    assert not (member / '.codex/auth.json').exists()
    assert store.active_provider_account_lease(account['account_id']) is not None
    monkeypatch.setattr(store, 'complete_provider_projection', original)
    binder.recover_projection(record,projection_factory=recovery_factory(harness))
    assert not store.pending_provider_projections()
    assert canonical.read_text() == '{"refresh":"after"}'


def test_wrong_owner_factory_cannot_publish_credentials_or_retain_unstarted_lease(harness):
    binder, store, account, worker, canonical, member, factory, _ = harness
    def wrong(**kwargs):
        projected = factory(**kwargs)
        projected.transaction.binding = replace(projected.transaction.binding,owner_id='other-owner')
        return projected
    with pytest.raises(RuntimeError, match='identity'):
        with binder.bind(worker,runtime_name='codex-cli',run_id='run',attempt_id='attempt',timeout_sec=1,projection_factory=wrong): pass
    assert not (member / '.codex/auth.json').exists()
    assert store.active_provider_account_lease(account['account_id']) is None
    assert not store.pending_provider_projections()


def test_pending_ledger_is_durable_before_first_prepare_call(harness):
    binder, store, _, worker, _, _, factory, _ = harness
    observed = []
    def wrapped(**kwargs):
        projected = factory(**kwargs)
        original = projected.transaction.prepare
        def prepare():
            reopened = ControlPlaneStore(str(store.db_path))
            observed.extend(reopened.pending_provider_projections())
            assert observed[0]['binding']['lease_id'] == kwargs['lease']['lease_id']
            original()
        projected.transaction.prepare = prepare
        return projected
    with binder.bind(worker,runtime_name='codex-cli',run_id='run',attempt_id='attempt',timeout_sec=1,projection_factory=wrapped): pass
    assert len(observed) == 1


def test_failed_recovery_releases_only_recovery_claim_for_a_safe_retry(harness):
    binder, store, _, _, _, _, _, seen = harness
    with pytest.raises(RuntimeError):
        with bind(harness): seen['projected'].transaction.stop_member = lambda: (_ for _ in ()).throw(RuntimeError('uncertain'))
    record = store.pending_provider_projections()[0]
    def failed(**kwargs):
        projected = recovery_factory(harness)(**kwargs)
        projected.transaction.stop_member = lambda: (_ for _ in ()).throw(RuntimeError('still uncertain'))
        return projected
    with pytest.raises(RuntimeError,match='still uncertain'):
        binder.recover_projection(record,projection_factory=failed)
    assert store.pending_provider_projections()[0]['state'] == 'quarantined'
    binder.recover_projection(record,projection_factory=recovery_factory(harness))
    assert not store.pending_provider_projections()


def test_completed_projection_does_not_block_account_forget(harness):
    _, store, account, _, _, _, _, _ = harness
    with bind(harness): pass
    store.disconnect_provider_account(account_id=account['account_id'],tenant_id='tenant',owner_id='owner')
    assert store.forget_provider_account(account_id=account['account_id'],tenant_id='tenant',owner_id='owner')['status'] == 'forgotten'


def test_store_cannot_complete_a_live_projection_with_forged_or_active_receipt(harness):
    import hashlib
    _, store, _, _, _, _, _, seen = harness
    with bind(harness):
        tx = seen['projected'].transaction
        for digest in ('0' * 64, hashlib.sha256(tx.receipt.read_bytes()).hexdigest()):
            with pytest.raises(ProviderProjectionPending, match='completed cleanup'):
                store.complete_provider_projection(binding=asdict(tx.binding), receipt_hash=digest)
        assert store.pending_provider_projections()


def test_inflight_heartbeat_cannot_quarantine_or_stop_a_completed_binding(harness, monkeypatch):
    from threading import Event, Thread
    from workers_projects_runtime.control_plane import ControlPlaneError
    binder, store, account, _, _, _, _, seen = harness
    monkeypatch.setenv('GLASSHIVE_PROVIDER_ACCOUNT_LEASE_HEARTBEAT_SECONDS','0.05')
    entered, finish_request = Event(), Event()
    original_start = binder._start_lease_heartbeat
    helpers=[]
    def heartbeat(**kwargs):
        entered.set()
        assert finish_request.wait(5)
        raise ControlPlaneError('synthetic late response')
    monkeypatch.setattr(store,'heartbeat_provider_lease',heartbeat)
    def start(**kwargs):
        value=original_start(**kwargs)
        def finish_after_stop():
            assert value[0].wait(5)
            finish_request.set()
        thread=Thread(target=finish_after_stop);thread.start();helpers.append(thread)
        return value
    monkeypatch.setattr(binder,'_start_lease_heartbeat',start)
    with bind(harness): assert entered.wait(2)
    for thread in helpers: thread.join(2)
    assert seen['stops'] == 2
    assert not store.pending_provider_projections()
    assert store.get_provider_account_record(account_id=account['account_id'],tenant_id='tenant',owner_id='owner')['status'] == 'ready'


def test_same_worker_cannot_hold_pending_projections_for_two_accounts(harness):
    _, store, account, worker, canonical, _, factory, _ = harness
    lease=store.acquire_provider_lease(account_id=account['account_id'],tenant_id='tenant',owner_id='owner',lane='mission',worker_id='worker',run_id='run',ttl_seconds=15)
    tx=factory(worker=worker,account_home=canonical.parent.parent,lease=lease,attempt_id='attempt',assert_lease=lambda:None).transaction
    from workers_projects_runtime.provider_projection_binding import _metadata
    metadata={'account_root':str(tx.account_root),'member_home':str(tx.member_home),'receipt':str(tx.receipt),'artifacts':[asdict(x) for x in tx.artifacts]}
    store.begin_provider_projection(binding=asdict(tx.binding),metadata=metadata)
    second=store.create_provider_account(tenant_id='tenant',owner_id='owner',provider='codex',label='Second',auth_method='subscription',platform_support='supported',secret_locator='native-home://second',status='ready')
    other_lease=store.acquire_provider_lease(account_id=second['account_id'],tenant_id='tenant',owner_id='owner',lane='mission',worker_id='worker',run_id='other-run',ttl_seconds=15)
    other=replace(tx.binding,account_id=second['account_id'],lease_id=other_lease['lease_id'],run_id='other-run')
    with pytest.raises(ProviderProjectionPending,match='Worker'):
        ControlPlaneStore(str(store.db_path)).begin_provider_projection(binding=asdict(other),metadata=metadata)
    assert len(store.pending_provider_projections(worker_id='worker')) == 1


def test_pending_worker_and_account_precede_every_legacy_or_unbound_fallback(harness):
    binder, store, account, worker, _, _, _, seen = harness
    with pytest.raises(RuntimeError):
        with bind(harness): seen['projected'].transaction.stop_member=lambda: (_ for _ in ()).throw(RuntimeError('uncertain'))
    preferred = json.dumps({'provider_account':{'account_id':account['account_id'],'policy':'personal_preferred'}})
    for mode,profile,bundle in [('docker','codex-cli',preferred),('host','unregistered',preferred),('host','codex-cli','{}')]:
        with pytest.raises(ProviderProjectionPending):
            with binder.bind(worker | {'execution_mode':mode,'bootstrap_bundle_json':bundle},runtime_name=profile,run_id='next',timeout_sec=1): pass


def test_selected_conversation_preparation_cannot_import_current_os_credentials(harness):
    from workers_projects_runtime.mission_provider_accounts import native_current_account_allowed
    _, _, account, worker, _, _, _, _ = harness
    worker['bootstrap_bundle_json'] = json.dumps({'run_mode':'conversation','provider_account':{'account_id':account['account_id'],'policy':'personal_required'}})
    assert not native_current_account_allowed(worker)
    assert not native_current_account_allowed(worker | {'_glasshive_provider_account_preferred_fallback':True})
    assert native_current_account_allowed(worker | {'bootstrap_bundle_json':'{}'})


def test_projected_native_key_requires_enabled_route_and_setup_launcher(harness, monkeypatch):
    binder, store, account, worker, _, _, _, _ = harness
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            'UPDATE provider_accounts SET auth_method=?, secret_locator=? WHERE account_id=?',
            ('api_key', 'native-home://api-key', account['account_id']),
        )
    for key, value in {
        'XPERFECT_EXECUTION_PROFILE': 'hosted-xfs',
        'GLASSHIVE_SECURITY_MODE': 'multi_user',
        'WPR_DEFAULT_EXECUTION_MODE': 'docker',
        'GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION': 'per_worker_container',
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('GLASSHIVE_ENABLE_NATIVE_API_KEYS', '0')
    with pytest.raises(RuntimeErrorBase, match='Native API keys require'):
        with bind(harness):
            pass
    assert store.active_provider_account_lease(account['account_id']) is None
    monkeypatch.setenv('GLASSHIVE_ENABLE_NATIVE_API_KEYS', '1')
    with pytest.raises(ControlPlaneError, match='verified native quota launch guard'):
        with bind(harness):
            pass
    assert store.active_provider_account_lease(account['account_id']) is None
    binder.homes.native_launcher = object()
    with bind(harness) as bound:
        assert bound['_glasshive_provider_account_projected']


def test_active_native_receipt_cannot_name_another_account(harness):
    binder, _, account, _, _, _, _, _ = harness
    with bind(harness) as bound:
        with pytest.raises(RuntimeErrorBase, match='conflicts with its active account'):
            binder.native_connection_receipt(
                bound, runtime_name='codex-cli', run_id='run',
                connection_id='different-account',
            )
        assert binder.native_connection_receipt(
            bound, runtime_name='codex-cli', run_id='run',
        )['connection_id'] == account['account_id']


def test_projected_run_uses_same_provisioned_owner_root_for_refresh(harness):
    binder, store, account, worker, canonical, member, original_factory, _ = harness
    owner=canonical.parents[4]/'provisioned-owner';owner.mkdir(mode=0o700)
    accounts=owner/'provider-accounts';accounts.mkdir(mode=0o700)
    new_home=accounts/account['account_id']
    # Synthetic explicit stopped-state relocation; production manager never does this.
    canonical.parent.parent.rename(new_home)
    binder.owner_root_resolver=lambda tenant,owner_id: owner
    def factory(**kwargs):
        projected=original_factory(**kwargs)
        assert projected.transaction.account_root == new_home
        return replace(projected,grant_member_access=lambda:None)
    with binder.bind(worker,runtime_name='codex-cli',run_id='run',attempt_id='attempt',timeout_sec=1,projection_factory=factory):
        (member/'.codex/auth.json').write_text('{"refresh":"owner-root-after"}')
    assert (new_home/'codex/auth.json').read_text() == '{"refresh":"owner-root-after"}'
    assert not canonical.exists()
    assert not store.pending_provider_projections()
