"""Durable native account projection admission; sandbox code supplies exact member paths."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import Callable, Iterator

from .control_plane import ControlPlaneConflict, ProviderProjectionPending
from .credential_projection import CredentialProjection, CredentialProjectionError
from .openclaw_runtime import RuntimeErrorBase
from .provider_accounts import ProviderAccountHomeManager


@dataclass(frozen=True)
class NativeProviderProjection:
    transaction: CredentialProjection
    environment: dict[str, str]
    # File selectors, never secret values. Native wrapper reads them after UID/guard entry.
    secret_files: dict[str, str]
    grant_member_access: Callable[[], None]


def _metadata(projected: NativeProviderProjection) -> dict:
    tx = projected.transaction
    return {"account_root": str(tx.account_root), "member_home": str(tx.member_home),
            "receipt": str(tx.receipt), "artifacts": [asdict(item) for item in tx.artifacts]}


@contextmanager
def _transaction_lock(tx: CredentialProjection):
    # Hold the same directory descriptors for the lock and all transaction I/O.
    # Parent replacement cannot redirect either the lock or credential operations.
    with tx._pinned():
        parent = tx._directory_fds[tx.receipt.parent]
        fd = os.open(tx.receipt.with_suffix(".lock").name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise CredentialProjectionError("Credential transaction lock is not private")
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ProviderProjectionPending("Credential transaction is still running") from exc
            yield
        finally:
            os.close(fd)


def _finish(tx: CredentialProjection, store, *, recovery_token: str = "", before_complete: Callable[[], None] | None = None) -> None:
    if not tx._exists(tx.receipt):
        # Ledger commit preceded prepare. Prepare persists a receipt before ANY copy.
        # Unknown native artifacts are never guessed away after a crash in that gap.
        tx.assert_lease(); tx.stop_member(); tx.assert_lease()
        for artifact in tx.artifacts:
            target = tx._path(tx.member_home, artifact.member_path, create=True)
            if tx._exists(target):
                raise CredentialProjectionError("Missing receipt with existing native credentials requires operator recovery")
        tx._save({"binding": asdict(tx.binding), "artifacts": [asdict(item) for item in tx.artifacts],
                  "phase": "preparing", "initial_hashes": [], "refresh_hashes": None})
    tx.finish()
    if tx._load().get("phase") != "complete":
        raise CredentialProjectionError("Credential cleanup receipt is incomplete")
    digest = hashlib.sha256(tx._read(tx.receipt, native=False, limit=65536)).hexdigest()
    if before_complete is not None:
        before_complete()
    store.complete_provider_projection(binding=asdict(tx.binding), receipt_hash=digest, recovery_token=recovery_token)


def _validate_selectors(projected: NativeProviderProjection, runtime_name: str) -> None:
    keys = {"codex-cli": {"CODEX_HOME"}, "claude-code": {"CLAUDE_CONFIG_DIR"}, "grok-build": {"GROK_AUTH_PATH"}}
    if set(projected.environment) != keys.get(runtime_name):
        raise RuntimeErrorBase("Native credential projection has invalid provider selectors")
    declared_secrets = {item.environment_key for item in projected.transaction.artifacts if item.environment_key}
    if set(projected.secret_files) != declared_secrets:
        raise RuntimeErrorBase("Native credential projection has invalid secret file selectors")
    if any(not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value for value in [*projected.environment.values(), *projected.secret_files.values()]):
        raise RuntimeErrorBase("Native credential projection selectors require absolute member paths")


@contextmanager
def bind_projected_account(binder, worker: dict, *, runtime_name: str, run_id: str,
                           attempt_id: str, timeout_sec: float | None, lease_purpose: str,
                           factory: Callable[..., NativeProviderProjection]) -> Iterator[dict]:
    from .mission_provider_accounts import (
        ProviderAccountBusyError, _PROFILE_PROVIDERS,
        mission_provider_account_selection, other_live_provider_lease,
    )
    selection = mission_provider_account_selection(worker)
    if selection is None or binder.store is None:
        raise RuntimeErrorBase("Native projection requires an exact selected account and control store")
    if worker.get("execution_mode") != "docker" or not attempt_id or not worker.get("workspace_id"):
        raise RuntimeErrorBase("Native projection requires the exact container workspace and attempt")
    tenant = str(worker.get("tenant_id") or "local")
    owner, worker_id = str(worker.get("owner_id") or ""), str(worker.get("worker_id") or "")
    if not owner or not worker_id or not run_id:
        raise RuntimeErrorBase("Native projection requires authenticated owner, worker and run")
    store = binder.store
    if store.pending_provider_projections(worker_id=worker_id):
        raise ProviderProjectionPending("Native credential recovery must finish before another run")
    if store.pending_provider_projections(account_id=selection.account_id):
        if other_live_provider_lease(
            store, account_id=selection.account_id, tenant_id=tenant,
            owner_id=owner, run_id=run_id,
        ):
            raise ProviderAccountBusyError()
        raise ProviderProjectionPending("Native credential recovery must finish before another run")
    account = store.get_provider_account_record(account_id=selection.account_id, tenant_id=tenant, owner_id=owner)
    if account is None or account["provider"] not in _PROFILE_PROVIDERS.get(runtime_name, set()) or account["status"] != "ready":
        raise RuntimeErrorBase("Selected native provider account is unavailable or needs verification")
    if account["auth_method"] not in {"subscription", "api_key"}:
        raise RuntimeErrorBase("This provider account requires its configured enterprise route")
    from .native_api_keys import native_key_account
    if account["auth_method"] == "api_key" and not native_key_account(account):
        raise RuntimeErrorBase("This API-key account requires its configured broker route")
    homes = binder.homes
    homes.require_supported_route(provider=account["provider"], auth_method=account["auth_method"], execution_mode="docker", platform_name=sys.platform,
                                  hosted_consumer_auth_enabled=os.environ.get("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "").lower() in {"1", "true", "yes", "on"})
    ttl = binder._lease_ttl_seconds(timeout_sec)
    route = binder._reserve_active_route(worker, runtime_name=runtime_name, run_id=run_id, account_id=selection.account_id, route_kind="native")
    lease = None; tx = None; ledger = False; heartbeat = None
    try:
        try:
            lease = store.acquire_provider_lease(account_id=selection.account_id, tenant_id=tenant, owner_id=owner,
                                                 lane=f"{runtime_name}:{lease_purpose}", worker_id=worker_id, run_id=run_id,
                                                 ttl_seconds=ttl, required_recovery_code="")
        except ControlPlaneConflict as exc:
            if other_live_provider_lease(
                store, account_id=selection.account_id, tenant_id=tenant,
                owner_id=owner, run_id=run_id,
            ):
                raise ProviderAccountBusyError() from exc
            raise
        account_home = homes.account_home_path(tenant_id=tenant, owner_id=owner, account_id=selection.account_id)
        def assert_lease():
            if tx is None: raise ProviderProjectionPending("Credential transaction is not established")
            store.assert_provider_projection(binding=asdict(tx.binding))
        projected = factory(worker=dict(worker), account_home=account_home, lease=dict(lease), attempt_id=attempt_id, assert_lease=assert_lease)
        if not isinstance(projected, NativeProviderProjection): raise RuntimeErrorBase("Native projection adapter contract is invalid")
        tx = projected.transaction
        expected = {"tenant_id": tenant, "owner_id": owner, "account_id": selection.account_id, "lease_id": lease["lease_id"],
                    "worker_id": worker_id, "workspace_id": worker["workspace_id"], "run_id": run_id, "attempt_id": attempt_id}
        if any(asdict(tx.binding)[key] != value for key, value in expected.items()) or tx.account_root != account_home:
            raise RuntimeErrorBase("Native projection adapter changed the account or run identity")
        _validate_selectors(projected, runtime_name)
        # Use the binder's fence even if an adapter passed an accidental alternate callback.
        tx.assert_lease = assert_lease
        def stop_after_lease_loss():
            with _transaction_lock(tx):
                pending = store.pending_provider_projections(account_id=selection.account_id)
                if any(item["binding"] == asdict(tx.binding) for item in pending):
                    tx.stop_member()
        with _transaction_lock(tx):
            store.begin_provider_projection(binding=asdict(tx.binding), metadata=_metadata(projected)); ledger = True
            heartbeat = binder._start_lease_heartbeat(lease_id=lease["lease_id"], tenant_id=tenant, owner_id=owner, ttl_seconds=ttl,
                                                      worker_id=worker_id, runtime_name=runtime_name, account_id=selection.account_id,
                                                      on_lease_lost=stop_after_lease_loss)
            tx.prepare()
            tx.assert_lease()
            projected.grant_member_access()
            tx.assert_lease()
        bound = {**worker, "_glasshive_provider_account_bound": True, "_glasshive_provider_account_projected": True,
                 "_glasshive_provider_account_env": dict(projected.environment),
                 "_glasshive_provider_account_secret_files": dict(projected.secret_files),
                 "_glasshive_provider_projection_lease_id": lease["lease_id"]}
        binder._update_active_route(worker_id, route, worker=bound, lease_id=lease["lease_id"])
        yield bound
    finally:
        binder._begin_close_active_route(worker_id, route)
        def stop_heartbeat_before_release():
            if heartbeat is not None:
                heartbeat[0].set(); heartbeat[1].join(timeout=2)
                if heartbeat[1].is_alive():
                    raise ProviderProjectionPending("Credential lease renewal has not stopped")
        try:
            if ledger:
                try:
                    with _transaction_lock(tx): _finish(tx, store, before_complete=stop_heartbeat_before_release)
                except BaseException:
                    store.quarantine_provider_projection(binding=asdict(tx.binding))
                    raise
        finally:
            if heartbeat is not None:
                heartbeat[0].set(); heartbeat[1].join(timeout=2)
            if lease is not None and not ledger:
                store.release_provider_lease(lease_id=lease["lease_id"], tenant_id=tenant, owner_id=owner)
            binder._finalize_close_active_route(worker_id, route)


def recover_projected_account(binder, record: dict, *, factory: Callable[..., NativeProviderProjection]) -> None:
    """Explicit trusted recovery; pending inventory never auto-authorizes new execution."""
    if binder.store is None: raise RuntimeErrorBase("Credential recovery requires a control store")
    store = binder.store
    binding = record["binding"]
    canonical = binder.homes.account_home_path(tenant_id=binding["tenant_id"], owner_id=binding["owner_id"], account_id=binding["account_id"])
    if str(canonical) != record["metadata"]["account_root"]:
        raise ProviderProjectionPending("Credential recovery requires its original provisioned owner root")
    token = ""
    def assert_lease(): store.assert_provider_projection(binding=binding, recovery_token=token)
    projected = factory(record=record, assert_lease=assert_lease)
    tx = projected.transaction
    if asdict(tx.binding) != binding or _metadata(projected) != record["metadata"]:
        raise ProviderProjectionPending("Credential recovery adapter changed its persisted binding")
    tx.assert_lease = assert_lease
    with _transaction_lock(tx):
        token = store.claim_provider_projection_recovery(binding=binding)
        try:
            _finish(tx, store, recovery_token=token)
        except BaseException:
            store.quarantine_provider_projection(binding=binding, recovery_token=token)
            raise
