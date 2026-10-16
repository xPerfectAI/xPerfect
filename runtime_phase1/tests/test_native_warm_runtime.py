"""Saved Queen/worker choices and exact native generation cleanup consumers."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.profile_runtime import HostNativeCliMixin, ProfiledWorkerRuntime
from workers_projects_runtime.native_warm_pool import (
    NativeWarmPool, NativeWarmConfig, NativeWarmServer, NativeWarmLease, WarmScope,
    _write_private, existing_native_stop_owner, process_start_identity,
)
from workers_projects_runtime.store import Store
from workers_projects_runtime.worker_configuration import WorkerConfiguration, ConfigurationUpdate


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


class Idle:
    record_path = None
    def __init__(self):
        self.stopped = False
    def alive(self):
        return not self.stopped
    def stop(self):
        self.stopped = True


@pytest.mark.parametrize('lane', ['conversation', 'mission'])
def test_same_saved_owner_prepares_queen_and_ordinary_worker_and_restores(tmp_path, monkeypatch, lane):
    import workers_projects_runtime.grok_native_warm as adapter
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    runtime.binary = sys.executable
    store = Store(tmp_path / 'state.db')
    project = store.create_project('owner', 'Synthetic', 'Fixture', 'grok-build')
    worker = store.create_worker(project['project_id'], 'owner', 'Synthetic worker', 'assistant',
        'grok-build', 'local', 'grok-build', 'grok-selected', execution_mode='host',
        workspace_root=str(tmp_path / 'workspace'), trusted_run_lane=lane,
        bootstrap_bundle={'run_mode': lane})
    runtime._host_workspace_dir(worker).mkdir(parents=True)
    monkeypatch.setattr(runtime, '_native_environment', lambda *args, **kwargs: {
        'HOME': str(tmp_path / 'home'), 'GROK_HOME': str(tmp_path / 'home' / '.grok'),
        'PATH': os.environ.get('PATH', ''), 'XAI_API_KEY': 'synthetic-not-a-secret'})
    made = []
    def factory(**kwargs):
        server = Idle()
        made.append((kwargs, server))
        return server
    monkeypatch.setattr(adapter, 'start_grok_server', factory)
    config = WorkerConfiguration(store, SimpleNamespace(), performance_runtime=runtime)
    try:
        view = config.put('local', 'owner', worker['worker_id'], ConfigurationUpdate(
            expected_revision=1, performance={'native_warm_servers': 2, 'native_warm_idle_seconds': 30}))
        wait_for(lambda: config.get('local', 'owner', worker['worker_id'])['effective']['performance']['native_warm_status']['idle'] == 2)
        assert view['requested']['performance']['native_warm_servers'] == 2
        assert len({id(item[1]) for item in made}) == 2
        assert all(item[0]['scope'].lane == lane for item in made)
        if lane == 'conversation':
            assert all(item[0]['static_environment']['GROK_SUBAGENTS'] == '0' for item in made)
        projected = config.prepare_run(worker, {'run_id': 'run', 'active_attempt_id': 'attempt'})
        assert json.loads(projected['bootstrap_bundle_json'])['runtime_performance']['native_warm_servers'] == 2
        runtime._disable_native_warm(worker)
        assert all(server.stopped for _, server in made)
        reopened = WorkerConfiguration(store, SimpleNamespace(), performance_runtime=runtime)
        assert reopened.restore_native_preparation() == {'requested': 1, 'unavailable': 0}
        wait_for(lambda: reopened.get('local', 'owner', worker['worker_id'])['effective']['performance']['native_warm_status']['idle'] == 2)
        reopened.put('local', 'owner', worker['worker_id'], ConfigurationUpdate(
            expected_revision=2, performance={'native_warm_servers': 0}))
        assert reopened.get('local', 'owner', worker['worker_id'])['effective']['performance']['native_warm_status']['idle'] == 0
        assert all(server.stopped for _, server in made)
    finally:
        runtime.close_native_warm()
        store.close()


@pytest.mark.parametrize('operation', ['pause_worker', 'terminate_worker'])
def test_idle_cleanup_preserves_existing_exact_operator_control(tmp_path, monkeypatch, operation):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    worker = {'tenant_id': 'tenant', 'owner_id': 'owner', 'worker_id': 'worker'}
    scope = WarmScope('tenant', 'owner', 'worker', 'grok-build', 'exact', 'native-current', 'revision')
    runtime._warm_pool = NativeWarmPool()
    runtime._warm_contexts[runtime._warm_owner(worker)] = (scope, {}, sys.executable)
    server = Idle()
    runtime._warm_pool.prepare(scope, NativeWarmConfig(1), lambda: server)
    wait_for(lambda: runtime._warm_pool.status(scope)['idle'] == 1)
    original_control = Mock(return_value='existing exact control')
    monkeypatch.setattr(HostNativeCliMixin, operation, original_control)
    try:
        assert getattr(runtime, operation)(worker) == 'existing exact control'
        original_control.assert_called_once_with(worker)
        assert server.stopped
    finally:
        runtime.close_native_warm()


@pytest.fixture
def leased_native(tmp_path):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    pid_file = tmp_path / 'escaped.pid'
    script = ('import subprocess,sys,time,pathlib; '
        'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True); '
        'pathlib.Path(sys.argv[1]).write_text(str(child.pid));time.sleep(60)')
    process = subprocess.Popen([sys.executable, '-c', script, str(pid_file)], start_new_session=True)
    wait_for(pid_file.exists)
    escaped_pid = int(pid_file.read_text())
    scope = WarmScope('tenant', 'owner', 'worker', 'grok-build', 'exact', 'native-current', 'revision')
    state_record = runtime._warm_directory() / 'synthetic.native-warm.json'
    identity = {'pid': process.pid, 'process_group': process.pid,
                'process_start_identity': process_start_identity(process.pid)}
    _write_private(state_record, {'kind': 'xperfect.native.warm.v1', 'phase': 'idle',
        'scope': scope.__dict__, 'child': identity})
    server = NativeWarmServer(process, identity['process_start_identity'], 'ws://127.0.0.1:12345/ws',
        'synthetic-private-secret', state_record,
        lambda child: existing_native_stop_owner(runtime, state_record, child))
    lease = NativeWarmLease(scope, 'run', 'attempt', server)
    descriptor = runtime._warm_descriptor_path('worker', 'run', 'attempt')
    lease.write_descriptor(descriptor)
    runtime._warm_leases[('worker', 'run', 'attempt')] = lease
    session = {'run_id': 'run', 'attempt_id': 'attempt', 'process_pid': os.getpid(),
               'external_native_lease_path': str(descriptor)}
    child_record = runtime._native_child_record_path(runtime._run_root('worker', 'run'), 'attempt')
    _write_private(child_record, {'supervisor_pid': os.getpid(), 'child': None,
                                 'external_native_lease_path': str(descriptor)})
    try:
        yield runtime, session, descriptor, server, escaped_pid
    finally:
        server.stop()
        runtime.close_native_warm()


def test_exact_attempt_stop_ends_native_server_and_escaped_tool_and_retains_proof(leased_native):
    runtime, session, descriptor, server, escaped_pid = leased_native
    assert runtime._external_native_generation_gone('worker', session) is False
    assert runtime._stop_external_native_generation('worker', session) is True
    assert not server.alive()
    receipt = json.loads(descriptor.read_text())
    assert receipt['stop_confirmed'] is True and 'secret' not in receipt and 'url' not in receipt
    assert str(escaped_pid) in receipt['escaped_descendants']
    assert runtime._recorded_pid_is_proven_gone(escaped_pid, receipt['escaped_descendants'][str(escaped_pid)])
    assert runtime._external_native_generation_gone('worker', session) is True


def test_foreign_attempt_and_missing_descriptor_cannot_claim_cleanup_proof(leased_native):
    runtime, session, descriptor, server, _ = leased_native
    assert runtime._stop_external_native_generation('worker', {**session, 'attempt_id': 'other'}) is False
    assert server.alive()
    raw = descriptor.read_bytes()
    descriptor.unlink()
    assert runtime._external_native_generation_gone('worker', session) is False
    descriptor.write_bytes(raw)
    descriptor.chmod(0o600)
    assert runtime._stop_external_native_generation('worker', session) is True


def test_other_native_profiles_have_explicit_unavailable_capability(tmp_path):
    runtime = ProfiledWorkerRuntime(str(tmp_path / 'runtime'))
    for profile in ['claude-code']:
        status = runtime.native_warm_status({'profile': profile, 'execution_mode': 'host'}, {'native_warm_servers': 2})
        assert status['supported'] is False and status['idle'] == 0


def test_restart_sweep_preserves_still_live_or_unknown_attempt_and_defers_new_capacity(tmp_path, monkeypatch):
    import workers_projects_runtime.grok_native_warm as adapter
    runtime = HostGrokBuildRuntime(str(tmp_path / "runtime"))
    worker = {"tenant_id": "tenant", "owner_id": "owner", "worker_id": "worker"}
    scope = WarmScope("tenant", "owner", "worker", "grok-build", "exact", "native-current", "revision")
    session = {"run_id": "run", "attempt_id": "attempt", "process_pid": 12345,
               "process_start_identity": "ps-lstart:supervisor"}
    monkeypatch.setattr(runtime, "_read_active_session", lambda worker_id: session)
    monkeypatch.setattr(runtime, "_recorded_pid_is_proven_gone", lambda *args: False)
    record = {"scope": scope.__dict__, "run_id": "run", "attempt_id": "attempt"}
    assert runtime._retained_warm_attempt_may_be_live(record) is True
    monkeypatch.setattr(runtime, "_warm_context", lambda worker: (scope, {}, sys.executable))
    def recovery(directory, owner, *, active_lease):
        assert active_lease(record) is True
        return {"stopped": 0, "pending": 0, "active": 1}
    monkeypatch.setattr(adapter, "recover_native_warm_records", recovery)
    runtime.configure_native_warm(worker, {"native_warm_servers": 2})
    assert runtime._warm_pool is None
    assert runtime.native_warm_status(worker, {"native_warm_servers": 2})["state"] == "unavailable"


def test_saved_optional_preparation_failure_does_not_turn_config_save_into_failure(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path / "runtime"))
    worker = {"profile": "grok-build", "execution_mode": "host", "worker_id": "worker"}
    monkeypatch.setattr(runtime, "_warm_prepare", Mock(side_effect=RuntimeError("unconfirmed synthetic cleanup")))
    status = runtime.configure_native_warm(worker, {"native_warm_servers": 2})
    assert status["state"] == "unavailable" and status["configured"] == 2
    assert status["failure_class"] == "RuntimeError"
    assert "synthetic" not in json.dumps(status)


def test_external_stop_can_end_runner_without_false_generation_loss(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    worker = {'tenant_id': 'tenant', 'owner_id': 'owner', 'worker_id': 'worker',
              'profile': 'grok-build', 'execution_mode': 'host'}
    process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    token = runtime._acquire_host_slot(worker)
    runtime._register_process('worker', process)
    identity = runtime._process_start_identity(process.pid)
    assert runtime._write_active_session('worker', {
        'session_name': 'host-run', 'run_id': 'run', 'attempt_id': 'attempt', 'process_pid': process.pid,
        'process_group': process.pid, 'process_start_identity': identity,
        'host_slot_token': token,
    })
    session = runtime._read_active_session('worker')
    def stop_external_then_runner_exits(worker_id, snapshot):
        assert snapshot['run_id'] == 'run'
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        return True
    monkeypatch.setattr(runtime, '_stop_external_native_generation', stop_external_then_runner_exits)
    try:
        assert runtime._stop_active_process('worker', worker=worker, run_id='run',
            expected_session=session, control_session=session) is True
        assert runtime._read_active_session('worker') is None
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        runtime.close_native_warm()


def test_startup_orphan_sweep_runs_without_any_saved_positive_option(tmp_path):
    store = Store(tmp_path / 'state.db')
    recovery = Mock(return_value={'stopped': 1, 'pending': 0, 'active': 0})
    performance = SimpleNamespace(recover_native_preparation=recovery, configure_native_warm=Mock())
    try:
        configuration = WorkerConfiguration(store, SimpleNamespace(), performance_runtime=performance)
        assert configuration.restore_native_preparation() == {'requested': 0, 'unavailable': 0}
        recovery.assert_called_once_with()
        performance.configure_native_warm.assert_not_called()
    finally:
        store.close()


@pytest.mark.parametrize('failure', [RuntimeError('uncertain stop'), ValueError('untracked'),
    subprocess.TimeoutExpired('native', 2), KeyboardInterrupt()])
def test_warm_stop_failure_cannot_skip_base_generation_finalizer(tmp_path, monkeypatch, failure):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    monkeypatch.setattr(runtime, '_finish_native_warm', Mock(side_effect=failure))
    base = Mock(return_value=True)
    monkeypatch.setattr(HostNativeCliMixin, '_finish_host_run_generation', base)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            runtime._finish_host_run_generation({}, 'run', None, None, 'slot', clear_session=True)
    else:
        assert runtime._finish_host_run_generation({}, 'run', None, None, 'slot', clear_session=True) is False
    base.assert_called_once_with({}, 'run', None, None, 'slot', clear_session=True)
    runtime.close_native_warm()


def test_session_marker_can_reach_exact_lease_before_supervisor_child_record(leased_native):
    runtime, session, descriptor, server, _ = leased_native
    runtime._native_child_record_path(runtime._run_root('worker', 'run'), 'attempt').unlink()
    assert runtime._external_native_lease_path('worker', session) == descriptor
    assert runtime._external_native_lease_path('worker', {**session, 'attempt_id': 'other'}) is False
    assert runtime._stop_external_native_generation('worker', session) is True
    assert server.stopped


def test_signal_phase_does_not_wait_and_full_stop_runs_outside_process_lock(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], start_new_session=True)
    worker = {'worker_id': 'worker', 'profile': 'grok-build', 'execution_mode': 'host'}
    token = runtime._acquire_host_slot(worker)
    runtime._register_process('worker', process)
    assert runtime._write_active_session('worker', {
        'session_name': 'host-run', 'run_id': 'run', 'attempt_id': 'attempt',
        'process_pid': process.pid, 'process_group': process.pid,
        'process_start_identity': runtime._process_start_identity(process.pid), 'host_slot_token': token})
    session = runtime._read_active_session('worker')
    signalled, proved = [], []
    def signal_external(worker_id, snapshot, sig):
        assert runtime._process_lock.locked()
        signalled.append(sig)
        return True
    def prove_external(worker_id, snapshot):
        # A separate worker can enter the global process lock during slow proof.
        assert runtime._process_lock.acquire(blocking=False)
        runtime._process_lock.release()
        proved.append(snapshot['attempt_id'])
        return True
    monkeypatch.setattr(runtime, '_signal_external_native_generation', signal_external)
    monkeypatch.setattr(runtime, '_stop_external_native_generation', prove_external)
    try:
        assert runtime._stop_active_process('worker', worker=worker, run_id='run',
            expected_session=session, control_session=session) is True
        assert signalled and proved == ['attempt']
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        runtime.close_native_warm()


@pytest.mark.parametrize('already_stopped', [False, True])
def test_operator_stop_releases_count_even_without_run_finalizer(leased_native, already_stopped):
    runtime, session, descriptor, server, _ = leased_native
    lease = runtime._warm_leases[('worker', 'run', 'attempt')]
    runtime._warm_pool = NativeWarmPool()
    with runtime._warm_pool._condition:
        runtime._warm_pool._leased[lease.lease_id] = lease
    if already_stopped:
        server.stop()
    assert runtime._warm_pool.status(lease.scope)['leased'] == 1
    assert runtime._stop_external_native_generation('worker', session) is True
    assert runtime._warm_pool.status(lease.scope)['leased'] == 0


def test_signal_does_not_wait_for_an_existing_exact_server_stop_owner(leased_native):
    import signal
    runtime, session, _, server, _ = leased_native
    with server._stop_lock:
        began = time.monotonic()
        assert runtime._signal_external_native_generation('worker', session, signal.SIGTERM) is True
        assert time.monotonic() - began < .2
    assert runtime._stop_external_native_generation('worker', session) is True


@pytest.mark.parametrize('field,value', [('scope', ['malformed']), ('scope', None),
    ('scope', {}), ('scope', {'worker_id': 123}), ('owner', 'malformed'),
    ('child', ['malformed']), ('escaped_descendants', ['malformed'])])
def test_startup_retains_malformed_native_ownership_as_pending(tmp_path, field, value):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    path = runtime._warm_directory() / 'malformed.native-warm.json'
    record = {'kind': 'xperfect.native.warm.v1', 'phase': 'leased',
        'scope': {'worker_id': 'worker'}, 'owner': {},
        'run_id': 'run', 'attempt_id': 'attempt'}
    record[field] = value
    _write_private(path, record)
    assert runtime.recover_native_preparation() == {'stopped': 0, 'active': 0, 'pending': 1}
    assert path.exists()
    runtime.close_native_warm()


def test_pause_never_run_host_worker_uses_existing_exact_idle_cleanup(tmp_path):
    from workers_projects_runtime.service import WorkersProjectsService
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    store = Store(tmp_path / 'state.db')
    project = store.create_project('owner', 'Synthetic', 'Fixture', 'grok-build')
    worker = store.create_worker(project['project_id'], 'owner', 'Synthetic worker', 'assistant',
        'grok-build', 'local', 'grok-build', 'grok-selected', execution_mode='host',
        workspace_root=str(tmp_path / 'workspace'))
    runtime._host_workspace_dir(worker).mkdir(parents=True)
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        assert service.pause_worker(worker['worker_id'])['state'] == 'paused'
        assert store.has_active_operator_pause(worker['worker_id'])
        assert not store.get_worker(worker['worker_id'])['compute_release_token']
        assert runtime.host_active_process_status(worker)['state'] == 'absent'
        work_log = (runtime._host_workspace_dir(worker) / 'work-log.md').read_text()
        assert 'Worker paused by operator.' in work_log
        assert 'Worker terminated by operator.' not in work_log
    finally:
        service.shutdown()
        store.close()


@pytest.mark.parametrize('uncertainty', ['session', 'live_handle', 'idle_cleanup'])
def test_idle_host_pause_never_confirms_unknown_or_live_compute(tmp_path, monkeypatch, uncertainty):
    from workers_projects_runtime.service import WorkersProjectsService
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    store = Store(tmp_path / 'state.db')
    project = store.create_project('owner', 'Synthetic', 'Fixture', 'grok-build')
    worker = store.create_worker(project['project_id'], 'owner', 'Synthetic worker', 'assistant',
        'grok-build', 'local', 'grok-build', 'grok-selected', execution_mode='host',
        workspace_root=str(tmp_path / 'workspace'))
    runtime._host_workspace_dir(worker).mkdir(parents=True)
    process = None
    if uncertainty == 'session':
        runtime._write_active_session(worker['worker_id'], {'session_name': 'unknown',
            'run_id': 'foreign', 'attempt_id': 'foreign', 'process_pid': 0})
    elif uncertainty == 'live_handle':
        process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], start_new_session=True)
        runtime._register_process(worker['worker_id'], process)
    else:
        monkeypatch.setattr(runtime, '_disable_native_warm', Mock(side_effect=RuntimeError('uncertain idle stop')))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        with pytest.raises(RuntimeError):
            service.pause_worker(worker['worker_id'])
        assert store.get_worker(worker['worker_id'])['state'] != 'paused'
        assert store.get_worker(worker['worker_id'])['compute_release_token']
        if process:
            assert process.poll() is None
    finally:
        if process:
            process.kill()
            process.wait(timeout=3)
        service.shutdown()
        store.close()


@pytest.mark.parametrize('operation', ['pause_worker', 'terminate_worker'])
def test_idle_cleanup_failure_still_runs_exact_active_control(tmp_path, monkeypatch, operation):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    monkeypatch.setattr(runtime, '_disable_native_warm', Mock(side_effect=RuntimeError('uncertain idle stop')))
    control = Mock(return_value='exact active cleanup')
    monkeypatch.setattr(HostNativeCliMixin, operation, control)
    with pytest.raises(RuntimeError, match='Idle native process cleanup'):
        getattr(runtime, operation)({'worker_id': 'worker'})
    control.assert_called_once_with({'worker_id': 'worker'})
    runtime.close_native_warm()


@pytest.mark.parametrize('matching_terminal', [True, False])
def test_idle_host_pause_only_cleans_its_exact_terminal_session(tmp_path, matching_terminal):
    from workers_projects_runtime.service import WorkersProjectsService
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    store = Store(tmp_path / 'state.db')
    project = store.create_project('owner', 'Synthetic', 'Fixture', 'grok-build')
    worker = store.create_worker(project['project_id'], 'owner', 'Synthetic worker', 'assistant',
        'grok-build', 'local', 'grok-build', 'grok-selected', execution_mode='host',
        workspace_root=str(tmp_path / 'workspace'))
    runtime._host_workspace_dir(worker).mkdir(parents=True)
    run = store.create_run(worker['worker_id'], project['project_id'], 'Synthetic completed work')
    store.finalize_run_if_state(run['run_id'], expected_state='queued', state='completed', output_text='Done')
    store.update_worker(worker['worker_id'], state='ready', last_run_id=run['run_id'])
    process = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    process.wait(timeout=3)
    runtime._write_active_session(worker['worker_id'], {'session_name': 'host-run',
        'run_id': run['run_id'] if matching_terminal else 'foreign', 'attempt_id': 'attempt',
        'process_pid': process.pid, 'process_group': process.pid,
        'process_start_identity': 'ps-lstart:known dead fixture'})
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        if matching_terminal:
            assert service.pause_worker(worker['worker_id'])['state'] == 'paused'
            assert runtime._read_active_session(worker['worker_id']) is None
        else:
            with pytest.raises(RuntimeError):
                service.pause_worker(worker['worker_id'])
            assert store.get_worker(worker['worker_id'])['state'] != 'paused'
            assert runtime._read_active_session(worker['worker_id'])['run_id'] == 'foreign'
    finally:
        service.shutdown()
        store.close()
