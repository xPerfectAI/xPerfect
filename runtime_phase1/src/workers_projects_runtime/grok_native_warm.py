"""Host Grok idle native capacity; ordinary run authority remains the owner."""
from pathlib import Path
import hashlib
import json
import time
from threading import RLock
from .bootstrap import bootstrap_bundle_for, CLEAN_ROOM_RUNTIME_ENV_KEYS
from .mission_provider_accounts import native_current_account_allowed
from .native_warm_pool import NativeWarmPool, NativeWarmConfig, WarmScope, native_environment_digest, start_grok_server, recover_native_warm_records, read_native_warm_lease, read_native_warm_record, existing_native_stop_owner, signal_existing_native_stop_owner


class GrokNativeWarmMixin:
    def _init_native_warm(self):
        self._warm_lock = RLock()
        self._warm_pool = None
        self._warm_contexts = {}
        self._warm_leases = {}
        self._warm_states = {}
        self._warm_closed = False
        self._warm_recovery = None

    @staticmethod
    def _warm_owner(worker):
        return tuple(str(worker.get(key) or '') for key in ('tenant_id', 'owner_id', 'worker_id'))

    def _warm_directory(self):
        return self.runtime_root / 'native-warm'

    def _native_warm_supported(self, worker):
        return worker.get("profile") == "grok-build" and worker.get("execution_mode") == "host"

    def _start_native_warm_server(self, worker, scope, environment, binary):
        return start_grok_server(binary=binary, model=scope.model,
            cwd=self._host_workspace_dir(worker), static_environment=dict(environment),
            state_directory=self._warm_directory(), scope=scope, stop_runtime=self)

    def _warm_context(self, worker):
        if (worker.get('profile') != 'grok-build' or worker.get('execution_mode') != 'host'
                or worker.get('_glasshive_provider_account_bound')
                or worker.get('_glasshive_inference_broker_bound')
                or not native_current_account_allowed(worker)):
            raise ValueError('Native preparation supports host Grok with a static current account only')
        bundle = bootstrap_bundle_for(worker)
        lane = 'conversation' if bundle.get('run_mode') == 'conversation' else 'mission'
        environment = self._native_environment(worker, host=True)
        for key in (*CLEAN_ROOM_RUNTIME_ENV_KEYS, 'GLASSHIVE_RUN_ID'):
            environment.pop(key, None)
        if lane == 'conversation':
            environment['GLASSHIVE_RUN_MODE'] = lane
            environment.update(GROK_SUBAGENTS='0', GROK_WORKFLOWS='0')
        workspace = self._host_workspace_dir(worker)
        if not workspace.is_dir():
            self.prepare_worker_workspace(worker)
        model = str(worker.get('model') or self.resolve_model('grok-build'))
        binary = Path(self.binary).expanduser()
        if not binary.is_absolute():
            import shutil
            binary = Path(shutil.which(str(binary), path=environment.get('PATH')) or binary)
        binary_stat = binary.stat()
        revision = [str(binary), binary_stat.st_dev, binary_stat.st_ino,
                    binary_stat.st_size, binary_stat.st_mtime_ns]
        auth = Path(environment.get('GROK_AUTH_PATH') or Path(environment['GROK_HOME']) / 'auth.json')
        if auth.is_file():
            info = auth.stat()
            revision += [str(auth), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        scope = WarmScope(*self._warm_owner(worker), 'grok-build', model, 'native-current',
            hashlib.sha256(json.dumps(revision, separators=(',', ':')).encode()).hexdigest(),
            native_environment_digest(environment, lane=lane), lane=lane)
        return scope, environment, str(binary)

    def _retained_warm_attempt_may_be_live(self, record):
        scope = record.get('scope')
        if (not isinstance(scope, dict) or not isinstance(scope.get('worker_id'), str)
                or not scope['worker_id'].strip()):
            raise ValueError('Retained native attempt has no valid worker scope')
        worker_id = scope['worker_id']
        session = self._read_active_session(worker_id)
        if not session:
            return self._active_session_meta_path(worker_id).exists()
        if (session.get('run_id') != record.get('run_id')
                or session.get('attempt_id') != record.get('attempt_id')):
            return False
        try:
            pid = int(session.get('process_pid') or 0)
            identity = str(session.get('process_start_identity') or '')
        except (TypeError, ValueError):
            return True
        return pid <= 0 or not identity or not self._recorded_pid_is_proven_gone(pid, identity)

    def recover_native_preparation(self):
        """Sweep orphan ownership independently of current worker configuration."""
        with self._warm_lock:
            # Do not classify this process's live pool as restart leftovers.
            if self._warm_pool is None:
                self._warm_recovery = recover_native_warm_records(self._warm_directory(), self,
                    active_lease=self._retained_warm_attempt_may_be_live)
            return self._warm_recovery or {'stopped': 0, 'pending': 0, 'active': 0}

    def _warm_prepare(self, worker, performance):
        config = NativeWarmConfig.from_performance(performance)
        owner = self._warm_owner(worker)
        with self._warm_lock:
            previous = self._warm_contexts.get(owner)
            if self._warm_closed:
                return None
            if not config.workers:
                if previous and self._warm_pool:
                    self._warm_pool.prepare(previous[0], config, lambda: None)
                self._warm_states[owner] = {'supported': True, 'state': 'disabled'}
                return None
        try:
            scope, environment, binary = self._warm_context(worker)
        except (OSError, ValueError, RuntimeError) as exc:
            with self._warm_lock:
                if previous and self._warm_pool:
                    self._warm_pool.prepare(previous[0], NativeWarmConfig(), lambda: None)
                self._warm_states[owner] = {'supported': False, 'state': 'unavailable',
                    'reason': 'The selected native account or runtime cannot be prepared safely',
                    'failure_class': type(exc).__name__}
            return None
        with self._warm_lock:
            if self._warm_pool is None:
                recovery = self.recover_native_preparation()
                if recovery['pending'] or recovery['active']:
                    self._warm_states[owner] = {'supported': False, 'state': 'unavailable',
                        'reason': 'A previous native generation is active or still needs confirmed cleanup'}
                    return None
                factory = getattr(self, "_native_warm_pool_factory", NativeWarmPool)
                self._warm_pool = factory()
            if previous and previous[0] == scope and previous[1] != environment:
                # Secret values stay only in memory; never hash or persist them.
                self._warm_pool.prepare(previous[0], NativeWarmConfig(), lambda: None)
            self._warm_contexts[owner] = scope, dict(environment), binary
            self._warm_states[owner] = {'supported': True}
            self._warm_pool.prepare(scope, config, lambda: self._start_native_warm_server(
                worker, scope, environment, binary))
        return scope

    def _safe_warm_prepare(self, worker, performance):
        try:
            return self._warm_prepare(worker, performance)
        except (OSError, ValueError, RuntimeError) as exc:
            with self._warm_lock:
                self._warm_states[self._warm_owner(worker)] = {
                    'supported': True, 'state': 'unavailable',
                    'reason': 'Idle preparation needs confirmed cleanup; ordinary startup remains available',
                    'failure_class': type(exc).__name__,
                }
            return None

    def configure_native_warm(self, worker, performance):
        self._safe_warm_prepare(worker, performance)
        return self.native_warm_status(worker, performance)

    def native_warm_status(self, worker, performance):
        config = NativeWarmConfig.from_performance(performance)
        owner = self._warm_owner(worker)
        base = {'supported': self._native_warm_supported(worker),
                'configured': config.workers, 'idle': 0, 'starting': 0, 'leased': 0,
                'provider_context_warmed': False}
        with self._warm_lock:
            context = self._warm_contexts.get(owner)
            base.update(self._warm_states.get(owner, {}))
            if context and self._warm_pool:
                status = self._warm_pool.status(context[0])
                base.update(status)
                base['configured'] = config.workers
                if base.get('state') != 'unavailable':
                    base['state'] = 'disabled' if not config.workers else 'ready' if status['idle'] else 'starting' if status['starting'] else 'not_ready'
            elif not config.workers:
                base['state'] = 'disabled'
            else:
                base.setdefault('state', 'not_prepared')
        if not base['supported']:
            base.setdefault('reason', 'Idle native processes are supported for host Grok only')
        return base

    def _prepare_native_authority_command(self, worker, command, *, run_root, timeout_sec):
        prepared = super()._prepare_native_authority_command(
            worker, command, run_root=run_root, timeout_sec=timeout_sec)
        return self._prepare_native_warm_command(worker, prepared, run_root=run_root)

    def _finish_host_run_generation(self, worker, run_id, process, session, slot_token, *, clear_session):
        warm_ok = True
        try:
            self._finish_native_warm(worker)
        except Exception:
            # Preserve the original run outcome/interrupt and let the base owner
            # retain the session when external native absence is not yet proven.
            warm_ok = False
        finally:
            base_ok = super()._finish_host_run_generation(worker, run_id, process, session,
                slot_token, clear_session=clear_session)
        return base_ok and warm_ok

    def _warm_descriptor_path(self, worker_id, run_id, attempt_id):
        suffix = hashlib.sha256(str(attempt_id).encode()).hexdigest()
        return self._run_root(worker_id, run_id) / ('native-warm-' + suffix + '.json')

    def _prepare_native_warm_command(self, worker, command, *, run_root):
        performance = bootstrap_bundle_for(worker).get('runtime_performance') or {}
        if not NativeWarmConfig.from_performance(performance).workers:
            return command
        claim_started = time.monotonic()
        scope = self._safe_warm_prepare(worker, performance)
        if scope is None:
            return command
        run_id, attempt_id = str(worker.get('_active_run_id') or ''), str(worker.get('_run_attempt_id') or '')
        if not run_id or not attempt_id:
            return command
        with self._warm_lock:
            lease = self._warm_pool.claim(scope, run_id=run_id, attempt_id=attempt_id)
        self._write_action_audit(worker, {'kind': 'native_warm.claim',
            'run_id': run_id, 'attempt_id': attempt_id, 'ready': lease is not None,
            'duration_ms': round((time.monotonic() - claim_started) * 1000, 3)})
        if lease is None:
            return command
        path = self._warm_descriptor_path(worker['worker_id'], run_id, attempt_id)
        try:
            lease.write_descriptor(path)
        except BaseException:
            self._warm_pool.finish(lease)
            raise
        self._warm_leases[(str(worker['worker_id']), run_id, attempt_id)] = lease
        worker['_native_warm_lease'] = lease
        worker['_external_native_lease_path'] = str(path)
        return [*command, '--native-warm-lease', str(path)]

    def _finish_native_warm(self, worker):
        lease = worker.get('_native_warm_lease')
        if lease is not None:
            self._warm_pool.finish(lease)
            worker.pop('_native_warm_lease', None)
            self._warm_leases.pop((lease.scope.worker_id, lease.run_id, lease.attempt_id), None)

    def _stop_native_warm_descriptor(self, path, *, worker_id, run_id, attempt_id):
        value = read_native_warm_lease(Path(path), worker_id=worker_id, run_id=run_id, attempt_id=attempt_id, profile=self.runtime_name)
        lease = self._warm_leases.get((worker_id, run_id, attempt_id))
        exact_lease = bool(lease and lease.server.identity() == value['server'] and lease.lease_id == value['lease_id'])
        if value.get('stop_confirmed') is True:
            if exact_lease and self._warm_pool is not None:
                self._warm_pool.finish(lease)
            return self._native_warm_descriptor_gone(value)
        if exact_lease:
            if self._warm_pool is not None:
                self._warm_pool.finish(lease)
            else:
                lease.server.stop()
            return self._native_warm_descriptor_gone(read_native_warm_lease(Path(path),
                worker_id=worker_id, run_id=run_id, attempt_id=attempt_id, profile=self.runtime_name))
        record_path = Path(value.get('state_record') or '')
        if record_path.parent != self._warm_directory():
            return False
        record = read_native_warm_record(record_path)
        if (record.get('phase') != 'leased' or record.get('lease_id') != value['lease_id']
                or record.get('scope') != value['scope'] or record.get('child') != value['server']
                or record.get('run_id') != run_id or record.get('attempt_id') != attempt_id
                or record.get('attempt_descriptor') != str(path)):
            return False
        if not existing_native_stop_owner(self, record_path, value['server']):
            return False
        # Existing stop owner retained escaped descendants before proof. Write the
        # same descriptor's secret-free final state before deleting its state record.
        from .native_warm_pool import _write_stopped_attempt_receipt
        _write_stopped_attempt_receipt(read_native_warm_record(record_path), value['server'])
        record_path.unlink()
        return self._native_warm_descriptor_gone(read_native_warm_lease(Path(path),
            worker_id=worker_id, run_id=run_id, attempt_id=attempt_id, profile=self.runtime_name))

    def _native_warm_descriptor_gone(self, value):
        descendants = value.get('escaped_descendants')
        return (value.get('stop_confirmed') is True
            and self._native_child_proven_gone(value['server'])
            and isinstance(descendants, dict)
            and all(self._recorded_pid_is_proven_gone(int(pid), identity)
                    for pid, identity in descendants.items()))

    def _external_native_lease_path(self, worker_id, session):
        if not isinstance(session, dict) or not session.get('run_id'):
            return None
        run_id, attempt_id = str(session['run_id']), str(session.get('attempt_id') or '')
        path = self._native_child_record_path(self._run_root(worker_id, run_id), attempt_id)
        try:
            from .native_warm_pool import _read_private_json
            record = _read_private_json(path)
        except FileNotFoundError:
            marker = session.get('external_native_lease_path')
            if not marker:
                return None
            return (Path(marker) if marker == str(self._warm_descriptor_path(worker_id, run_id, attempt_id))
                    else False)
        except (OSError, ValueError):
            return False
        marker = record.get('external_native_lease_path')
        if not marker:
            return False if session.get('external_native_lease_path') else None
        if (record.get('supervisor_pid') != session.get('process_pid')
                or (session.get('external_native_lease_path') and marker != session['external_native_lease_path'])
                or marker != str(self._warm_descriptor_path(worker_id, run_id, attempt_id))):
            return False
        return Path(marker)

    def _signal_external_native_generation(self, worker_id, session, sig):
        """Exact signal only: shared Stop proves/escalates outside its process lock."""
        path = self._external_native_lease_path(worker_id, session)
        if path is None or path is False:
            return path is None
        try:
            value = read_native_warm_lease(path, worker_id=worker_id,
                run_id=str(session['run_id']), attempt_id=str(session.get('attempt_id') or ''), profile=self.runtime_name)
            if value.get('stop_confirmed') is True:
                return self._native_warm_descriptor_gone(value)
            lease = self._warm_leases.get((worker_id, str(session['run_id']), str(session.get('attempt_id') or '')))
            guard = (lease.server._stop_lock if lease and lease.server.identity() == value['server']
                     and lease.lease_id == value['lease_id'] else None)
            if guard is not None and not guard.acquire(blocking=False):
                # The exact server already has a stop/signal owner. Do not wait
                # on its descendant proof under the runtime-wide process lock.
                return True
            try:
                value = read_native_warm_lease(path, worker_id=worker_id,
                    run_id=str(session['run_id']), attempt_id=str(session.get('attempt_id') or ''), profile=self.runtime_name)
                if value.get('stop_confirmed') is True:
                    return self._native_warm_descriptor_gone(value)
                record_path = Path(value.get('state_record') or '')
                if record_path.parent != self._warm_directory():
                    return False
                record = read_native_warm_record(record_path)
                if (record.get('phase') != 'leased' or record.get('lease_id') != value['lease_id']
                        or record.get('scope') != value['scope'] or record.get('child') != value['server']
                        or record.get('run_id') != session['run_id']
                        or record.get('attempt_id') != session.get('attempt_id')
                        or record.get('attempt_descriptor') != str(path)):
                    return False
                return signal_existing_native_stop_owner(self, record_path, value['server'], sig)
            finally:
                if guard is not None:
                    guard.release()
        except (OSError, ValueError, RuntimeError):
            return False

    def _stop_external_native_generation(self, worker_id, session):
        path = self._external_native_lease_path(worker_id, session)
        if path is None or path is False:
            return path is None
        try:
            return self._stop_native_warm_descriptor(path, worker_id=worker_id,
                run_id=str(session['run_id']), attempt_id=str(session.get('attempt_id') or ''))
        except (OSError, ValueError, RuntimeError):
            return False

    def _external_native_generation_gone(self, worker_id, session):
        path = self._external_native_lease_path(worker_id, session)
        if path is None or path is False:
            return path is None
        try:
            value = read_native_warm_lease(path, worker_id=worker_id,
                run_id=str(session['run_id']), attempt_id=str(session.get('attempt_id') or ''), profile=self.runtime_name)
            return self._native_warm_descriptor_gone(value)
        except (OSError, ValueError, RuntimeError):
            return False

    def host_active_process_status(self, worker):
        status = super().host_active_process_status(worker)
        if status.get('state') != 'absent':
            return status
        worker_id = str(worker.get('worker_id') or '')
        session = self._read_active_session(worker_id)
        if session:
            child = self._session_native_child(worker_id, session)
            if (child is False or (isinstance(child, dict) and not self._native_child_proven_gone(child))
                    or not self._external_native_generation_gone(worker_id, session)):
                return {'state': 'uncertain', 'run_id': str(session.get('run_id') or '')}
        return status

    def _disable_native_warm(self, worker):
        self._warm_prepare(worker, {'native_warm_servers': 0})

    def _control_after_native_idle_cleanup(self, worker, control):
        cleanup_error = None
        try:
            self._disable_native_warm(worker)
        except Exception as exc:
            cleanup_error = exc
        # An idle cleanup failure must not prevent stopping the active attempt,
        # nor let the caller report all worker compute confirmed stopped.
        info = control(worker)
        if cleanup_error is not None:
            raise RuntimeError('Idle native process cleanup is not confirmed') from cleanup_error
        return info

    def pause_worker(self, worker):
        return self._control_after_native_idle_cleanup(worker, super().pause_worker)

    def terminate_worker(self, worker):
        return self._control_after_native_idle_cleanup(worker, super().terminate_worker)

    def close_native_warm(self):
        with self._warm_lock:
            self._warm_closed = True
            pool = self._warm_pool
        if pool:
            pool.close()
