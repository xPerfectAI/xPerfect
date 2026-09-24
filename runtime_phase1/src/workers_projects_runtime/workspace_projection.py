"""Exact member adapters for the existing durable provider account binder."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

from .credential_projection import CredentialProjection, ProjectionBinding
from .provider_credential_artifacts import CredentialArtifact, credential_artifacts
from .provider_projection_binding import NativeProviderProjection
from .workspace_box import WorkspaceBox, WorkspaceBoxUnavailable

_active_binding = ContextVar('xperfect_native_projection', default=None)


@contextmanager
def native_binding(worker):
    token = _active_binding.set({'binding': {
        'worker_id': worker.get('worker_id'), 'run_id': worker.get('_active_run_id'),
        'attempt_id': worker.get('_run_attempt_id'),
        'lease_id': worker.get('_glasshive_provider_projection_lease_id'),
    }, 'secret_files': dict(worker.get('_glasshive_provider_account_secret_files') or {})}
        if worker.get('_glasshive_provider_account_projected') else None)
    try:
        yield
    finally:
        _active_binding.reset(token)


def assert_native_launch(box, control_store):
    pending = control_store.pending_provider_projections(worker_id=box.binding.worker_id)
    if not pending:
        return
    active = _active_binding.get()
    if len(pending) != 1 or not active:
        raise WorkspaceBoxUnavailable('Native credential recovery or another run holds this member')
    binding = pending[0]['binding']
    if any(binding.get(key) != value for key, value in active['binding'].items()):
        raise WorkspaceBoxUnavailable('Native launch differs from the exact account run binding')
    if (binding['workspace_id'] != box.binding.workspace_id or binding['member_uid'] != box.binding.uid
            or pending[0]['state'] != 'pending'):
        raise WorkspaceBoxUnavailable('Native account member identity changed')
    inspected = box._inspect()
    if inspected is None or inspected['Id'] != binding['container_id']:
        raise WorkspaceBoxUnavailable('Native account container generation changed')
    control_store.assert_provider_projection(binding=binding)


def assert_native_resume(box, control_store, runtime_store, worker):
    """Permit only the claimed paused run to continue its existing projection."""
    pending = control_store.pending_provider_projections(worker_id=box.binding.worker_id)
    if not pending:
        return False
    if len(pending) != 1 or pending[0]['state'] != 'pending':
        raise WorkspaceBoxUnavailable('Native credential recovery holds this member')
    binding = pending[0]['binding']
    current = runtime_store.get_worker(box.binding.worker_id, box.binding.tenant_id,
                                       box.binding.owner_id)
    run = runtime_store.get_run(binding['run_id'])
    expires = str((current or {}).get('compute_release_expires_at') or '')
    try:
        claim_live = datetime.fromisoformat(expires) > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        claim_live = False
    claim_fields = ('compute_release_token', 'compute_release_epoch',
                    'compute_release_operation_id', 'compute_release_target_run_id',
                    'compute_release_container_id')
    if (not current or not run or not claim_live
            or any(current.get(key) != worker.get(key) for key in claim_fields)
            or not str(current.get('compute_release_token') or '')
            or current.get('compute_release_kind') != 'resume_run'
            or current.get('state') != 'paused'
            or run.get('state') != 'paused'
            or run.get('worker_id') != box.binding.worker_id
            or run.get('run_id') != current.get('compute_release_target_run_id')
            or str(run.get('started_at') or '') != str(current.get('compute_release_target_started_at') or '')
            or str(run.get('active_attempt_id') or '') != binding['attempt_id']
            or any(binding[key] != getattr(box.binding, attr) for key, attr in (
                ('worker_id', 'worker_id'), ('workspace_id', 'workspace_id'),
                ('tenant_id', 'tenant_id'), ('owner_id', 'owner_id'), ('member_uid', 'uid')))
            or binding['container_id'] != current.get('compute_release_container_id')):
        raise WorkspaceBoxUnavailable('Resume differs from the exact paused account run')
    inspected = box._inspect()
    if inspected is None or inspected['Id'] != binding['container_id']:
        raise WorkspaceBoxUnavailable('Native account container generation changed')
    control_store.assert_provider_projection(binding=binding)
    return True


def credential_command(command):
    import json
    active = _active_binding.get()
    if not active or not active['secret_files']:
        return command
    return ['/usr/bin/python3', '-I', '-m', 'workers_projects_runtime.native_credential_exec',
            json.dumps(active['secret_files'], sort_keys=True), '--', *command]


def projection_factory(box, control_store):
    def construct(*, worker, account_home, lease, attempt_id, assert_lease):
        if any(worker.get(key) != getattr(box.binding, key) for key in
               ('worker_id', 'workspace_id', 'tenant_id', 'owner_id')):
            raise WorkspaceBoxUnavailable('Credential factory member identity changed')
        # The account binding records the exact box generation, and account-bound
        # runs project before the native start. The box itself holds no account
        # state, so prepare it here; a fresh member otherwise has no box yet.
        box.ensure_box()
        inspected = box._inspect()
        if inspected is None or not inspected['State']['Running']:
            raise WorkspaceBoxUnavailable('Prepare the exact shared member before projecting an account')
        account = control_store.get_provider_account_record(account_id=lease['account_id'],
            tenant_id=worker['tenant_id'], owner_id=worker['owner_id'])
        if account is None:
            raise WorkspaceBoxUnavailable('Selected account is unavailable')
        binding = ProjectionBinding(tenant_id=worker['tenant_id'], owner_id=worker['owner_id'],
            account_id=lease['account_id'], lease_id=lease['lease_id'], worker_id=worker['worker_id'],
            workspace_id=worker['workspace_id'], run_id=lease['run_id'], attempt_id=attempt_id,
            container_id=inspected['Id'], member_uid=box.binding.uid)
        receipt = box.supervisor / ('credential-' + hashlib.sha256(lease['lease_id'].encode()).hexdigest() + '.json')
        tx = CredentialProjection(account_root=account_home, member_home=box.paths()['home_dir'],
            receipt=receipt, binding=binding, artifacts=credential_artifacts(account['provider'], account['auth_method']),
            assert_lease=assert_lease, stop_member=lambda: box.stop_member(binding.container_id))
        home = f'/workspace/data/members/{box.binding.uid}/home'
        provider = account['provider']
        selectors = ({'CODEX_HOME': home + '/.codex'} if provider in {'codex', 'openai'} else
            {'CLAUDE_CONFIG_DIR': home + '/.claude'} if provider in {'claude', 'anthropic'} else
            {'GROK_AUTH_PATH': home + '/.grok/auth.json'})
        def grant():
            tx.assert_lease()
            for artifact in tx.artifacts:
                target = tx._path(tx.member_home, artifact.member_path)
                parent = target.parent
                while parent != tx.member_home:
                    WorkspaceBox._private_acl(parent, binding.member_uid)
                    parent = parent.parent
                descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    WorkspaceBox._publication_acl(descriptor, subject=f'user:{binding.member_uid}', is_directory=False)
                finally:
                    os.close(descriptor)
            tx.assert_lease()
        return NativeProviderProjection(transaction=tx, environment=selectors,
            secret_files={item.environment_key: home + '/' + item.member_path for item in tx.artifacts if item.environment_key},
            grant_member_access=grant)
    return construct


def recovery_projection(box, *, record, assert_lease):
    binding = ProjectionBinding(**record['binding'])
    if (binding.workspace_id != box.binding.workspace_id or binding.worker_id != box.binding.worker_id
            or binding.tenant_id != box.binding.tenant_id or binding.owner_id != box.binding.owner_id
            or binding.member_uid != box.binding.uid):
        raise WorkspaceBoxUnavailable('Recovery member identity differs from persisted admission')
    metadata = record['metadata']
    home, receipt = Path(metadata['member_home']), Path(metadata['receipt'])
    if home != box.paths()['home_dir'] or receipt.parent != box.supervisor:
        raise WorkspaceBoxUnavailable('Recovery placement differs from persisted member storage')
    tx = CredentialProjection(account_root=Path(metadata['account_root']), member_home=home,
        receipt=receipt, binding=binding, artifacts=tuple(CredentialArtifact(**item) for item in metadata['artifacts']),
        assert_lease=assert_lease, stop_member=lambda: box.stop_member(binding.container_id))
    return NativeProviderProjection(tx, {}, {}, lambda: None)
