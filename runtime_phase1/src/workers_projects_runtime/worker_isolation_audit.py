from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .bootstrap import bootstrap_bundle_for
from .docker_sandbox import (
    PARALLEL_CLEAN_ROOM_FORBIDDEN_CONTAINER_ENV_PREFIXES,
    PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE,
    PARALLEL_CLEAN_ROOM_POLICY_LABEL,
    PARALLEL_CLEAN_ROOM_ROLE_LABEL,
    PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL,
)
from .store import PARALLEL_CLEAN_ROOM_EXECUTION_POLICY, Store


class WorkerIsolationAuditError(RuntimeError):
    """The exact owner-bound live clean-room generation could not be proven."""


def _invalid() -> WorkerIsolationAuditError:
    return WorkerIsolationAuditError("The exact worker isolation evidence is unavailable")


def _fingerprint(kind: str, value: object) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise _invalid()
    return "sha256:" + hashlib.sha256(f"{kind}\0{clean}".encode("utf-8")).hexdigest()


def _container_identity(value: object) -> str:
    clean = str(value or "").strip()
    if re.fullmatch(r"[a-f0-9]{64}", clean) is None:
        raise _invalid()
    return clean


def _confirmed_scope(
    store: Store,
    *,
    worker: dict[str, Any],
    run_id: str,
    expected_container: str,
) -> dict[str, Any]:
    tenant_id = str(worker.get("tenant_id") or "").strip()
    owner_id = str(worker.get("owner_id") or "").strip()
    worker_id = str(worker.get("worker_id") or "").strip()
    if not tenant_id or not owner_id or not worker_id or not run_id:
        raise _invalid()
    durable_worker = store.get_worker(worker_id, tenant_id=tenant_id, owner_id=owner_id)
    delegation = store.get_delegation_for_worker(
        worker_id, tenant_id=tenant_id, owner_id=owner_id
    )
    run = store.get_run(run_id)
    lease = store.get_active_host_run_lease_for_run(run_id)
    if (
        durable_worker is None
        or delegation is None
        or run is None
        or lease is None
        or str(durable_worker.get("execution_mode") or "") != "docker"
        or str(bootstrap_bundle_for(durable_worker).get("execution_policy") or "")
        != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        or str(delegation.get("current_run_id") or "") != run_id
        or str(delegation.get("worker_id") or "") != worker_id
        or str(run.get("worker_id") or "") != worker_id
        or str(run.get("state") or "") != "running"
        or not str(run.get("runtime_invoked_at") or "")
        or str(lease.get("owner_id") or "") != owner_id
        or str(lease.get("tenant_id") or "") != tenant_id
        or str(lease.get("worker_id") or "") != worker_id
        or str(lease.get("run_id") or "") != run_id
        or str(lease.get("status") or "") != "active"
        or str(lease.get("startup_state") or "") != "confirmed"
        or str(lease.get("startup_identity_kind") or "") != "docker_session"
        or str(lease.get("startup_container_id") or "") != expected_container
        or str(run.get("active_attempt_id") or "")
        != str(lease.get("attempt_id") or "")
        or not str(lease.get("attempt_id") or "")
    ):
        raise _invalid()
    return {
        "worker": durable_worker,
        "delegation": delegation,
        "run": run,
        "lease": lease,
        "container_id": expected_container,
    }


def _inspect_mission_network(manager: object, sandbox: object) -> frozenset[str]:
    configuration_reader = getattr(manager, "_parallel_clean_room_configuration", None)
    network_reader = getattr(manager, "_parallel_clean_room_mission_network_name", None)
    docker = getattr(manager, "_docker", None)
    if not callable(configuration_reader) or not callable(network_reader) or not callable(docker):
        raise _invalid()
    configuration, _reason = configuration_reader(require_proxy_containers=True)
    if not isinstance(configuration, dict):
        raise _invalid()
    provider_name = str(configuration.get("provider_proxy_container") or "").strip()
    broker_name = str(configuration.get("broker_proxy_container") or "").strip()
    container_name = str(getattr(sandbox, "container_name", "") or "").strip()
    network_name = str(getattr(sandbox, "network_mode", "") or "").strip()
    container_id = _container_identity(getattr(sandbox, "container_id", ""))
    if (
        not provider_name
        or not broker_name
        or len({provider_name, broker_name, container_name}) != 3
        or network_reader(container_name) != network_name
    ):
        raise _invalid()
    try:
        result = docker(
            ["network", "inspect", network_name],
            check=False,
            capture_output=True,
            timeout_sec=2,
        )
        parsed = json.loads(str(result.stdout or "[]"))
    except Exception as exc:
        raise _invalid() from exc
    if (
        int(result.returncode) != 0
        or not isinstance(parsed, list)
        or len(parsed) != 1
        or not isinstance(parsed[0], dict)
    ):
        raise _invalid()
    network = parsed[0]
    labels = network.get("Labels")
    members = network.get("Containers")
    if (
        network.get("Name") != network_name
        or network.get("Driver") != "bridge"
        or network.get("Internal") is not True
        or not isinstance(labels, dict)
        or labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL)
        != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        or labels.get(PARALLEL_CLEAN_ROOM_ROLE_LABEL)
        != PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE
        or labels.get(PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL) != container_name
        or not isinstance(members, dict)
        or len(members) != 3
    ):
        raise _invalid()
    identities: dict[str, str] = {}
    for identity, member in members.items():
        clean_identity = _container_identity(identity)
        if not isinstance(member, dict) or not isinstance(member.get("Name"), str):
            raise _invalid()
        name = str(member["Name"])
        if name in identities:
            raise _invalid()
        identities[name] = clean_identity
    if (
        set(identities) != {container_name, provider_name, broker_name}
        or identities.get(container_name) != container_id
    ):
        raise _invalid()
    return frozenset(identities.values())


def _inspect_scope(runtime: object, scope: dict[str, Any]) -> dict[str, Any]:
    resolver = getattr(runtime, "_runtime_for_worker", None)
    if not callable(resolver):
        raise _invalid()
    manager = getattr(resolver(scope["worker"]), "sandbox", None)
    inspect_fresh = getattr(manager, "inspect_fresh", None)
    policy_matches = getattr(manager, "_sandbox_matches_parallel_clean_room_policy", None)
    if not callable(inspect_fresh) or not callable(policy_matches):
        raise _invalid()
    inspection = inspect_fresh(str(scope["worker"]["worker_id"]))
    sandbox = getattr(inspection, "sandbox", None)
    if (
        str(getattr(inspection, "status", "") or "") != "present"
        or sandbox is None
        or str(getattr(sandbox, "state", "") or "").lower() != "running"
        or _container_identity(getattr(sandbox, "container_id", ""))
        != scope["container_id"]
        or not policy_matches(sandbox)
        or str(getattr(sandbox, "execution_policy", "") or "")
        != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        or getattr(sandbox, "pid_mode", None) not in {"", "private"}
        or getattr(sandbox, "ipc_mode", None) != "private"
        or getattr(sandbox, "cgroupns_mode", None) != "private"
        or getattr(sandbox, "read_only_rootfs", None) is not True
        or getattr(sandbox, "privileged", None) is not False
        or getattr(sandbox, "cap_add", None) != ()
        or {str(capability).upper() for capability in getattr(sandbox, "cap_drop", ())}
        != {"ALL"}
        or set(getattr(sandbox, "attached_networks", ()))
        != {str(getattr(sandbox, "network_mode", "") or "")}
        or any(
            str(name).startswith(PARALLEL_CLEAN_ROOM_FORBIDDEN_CONTAINER_ENV_PREFIXES)
            for name, _value in getattr(sandbox, "environment", ())
        )
    ):
        raise _invalid()
    workspace = Path(str(getattr(sandbox, "workspace_dir", "") or "")).resolve()
    home = Path(str(getattr(sandbox, "home_dir", "") or "")).resolve()
    durable_workspace = Path(
        str(scope["worker"].get("workspace_root") or scope["worker"].get("workspace_dir") or "")
    ).resolve()
    bind_pairs = tuple(getattr(sandbox, "bind_mount_pairs", ()))
    if (
        workspace == home
        or workspace != durable_workspace
        or len(bind_pairs) != 2
        or {str(Path(str(source)).resolve()) for source, _target in bind_pairs}
        != {str(workspace), str(home)}
    ):
        raise _invalid()
    members = _inspect_mission_network(manager, sandbox)
    return {
        **scope,
        "sandbox": sandbox,
        "workspace": str(workspace),
        "home": str(home),
        "network_members": members,
    }


def _record(scope: dict[str, Any], scopes: list[dict[str, Any]]) -> dict[str, Any]:
    worker = scope["worker"]
    delegation = scope["delegation"]
    run = scope["run"]
    lease = scope["lease"]
    sandbox = scope["sandbox"]
    peers = sorted(
        (
            {
                "workRefHash": _fingerprint("work", peer["delegation"]["work_ref"]),
                "reachable": False,
            }
            for peer in scopes
            if peer is not scope
        ),
        key=lambda entry: str(entry["workRefHash"]),
    )
    payload: dict[str, Any] = {
        "contractVersion": 1,
        "producerScope": "glasshive.worker_isolation",
        "ownerRefHash": _fingerprint("owner", worker["owner_id"]),
        "workRefHash": _fingerprint("work", delegation["work_ref"]),
        "runRefHash": _fingerprint("run", run["run_id"]),
        "workerRefHash": _fingerprint("worker", worker["worker_id"]),
        "attemptRefHash": _fingerprint("attempt", run["active_attempt_id"]),
        "leaseRefHash": _fingerprint("lease", lease["lease_id"]),
        "containerRefHash": _fingerprint("container", scope["container_id"]),
        "workspaceRefHash": _fingerprint("workspace", scope["workspace"]),
        "homeRefHash": _fingerprint("home", scope["home"]),
        "networkRefHash": _fingerprint("network", sandbox.network_mode),
        "pidNamespaceRefHash": _fingerprint("pid_namespace", scope["container_id"]),
        "executionMode": "isolated_container",
        "hostStateReadable": False,
        "serviceEnvironmentReadable": False,
        "dockerSocketReadable": False,
        "ambientAuthority": False,
        "peerAccessDenied": bool(peers),
        "hostAccessDenied": True,
        "peerProbes": peers,
    }
    correlation = {
        "ownerRefHash": payload["ownerRefHash"],
        "workRefHash": payload["workRefHash"],
        "runRefHash": payload["runRefHash"],
        "attemptRefHash": payload["attemptRefHash"],
        "leaseRefHash": payload["leaseRefHash"],
        "containerRefHash": payload["containerRefHash"],
        "peers": [peer["workRefHash"] for peer in peers],
    }
    digest = hashlib.sha256(
        json.dumps(correlation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "event_id": f"evt_isolation_{digest}",
        "trace_event_id": f"trace_isolation_{digest}",
        "tenant_id": str(worker["tenant_id"]),
        "owner_id": str(worker["owner_id"]),
        "project_id": str(worker["project_id"]),
        "worker_id": str(worker["worker_id"]),
        "work_ref": str(delegation["work_ref"]),
        "run_id": str(run["run_id"]),
        "attempt_id": str(run["active_attempt_id"]),
        "lease_id": str(lease["lease_id"]),
        "container_id": str(scope["container_id"]),
        "payload": payload,
    }


def capture_worker_isolation_audit(
    *,
    store: Store,
    runtime: object,
    worker: dict[str, Any],
    run_id: str,
    container_id: str,
) -> list[dict[str, Any]]:
    """Audit only freshly observed, confirmed, exact-owner clean-room peers."""

    try:
        container = _container_identity(container_id)
        current = _confirmed_scope(
            store,
            worker=worker,
            run_id=str(run_id or "").strip(),
            expected_container=container,
        )
        owner_id = str(current["worker"]["owner_id"])
        tenant_id = str(current["worker"]["tenant_id"])
        scopes: list[dict[str, Any]] = [current]
        for lease in store.list_active_host_run_leases():
            if (
                str(lease.get("owner_id") or "") != owner_id
                or str(lease.get("tenant_id") or "") != tenant_id
                or str(lease.get("run_id") or "") == str(run_id)
                or str(lease.get("startup_state") or "") != "confirmed"
                or str(lease.get("startup_identity_kind") or "") != "docker_session"
            ):
                continue
            peer = store.get_worker(
                str(lease.get("worker_id") or ""),
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if peer is None:
                raise _invalid()
            if (
                str(bootstrap_bundle_for(peer).get("execution_policy") or "")
                != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            ):
                continue
            scopes.append(
                _confirmed_scope(
                    store,
                    worker=peer,
                    run_id=str(lease.get("run_id") or ""),
                    expected_container=_container_identity(
                        lease.get("startup_container_id")
                    ),
                )
            )
        if len(scopes) > 32:
            raise _invalid()
        inspected = [_inspect_scope(runtime, scope) for scope in scopes]
        for field in ("container_id", "workspace", "home"):
            if len({str(scope[field]) for scope in inspected}) != len(inspected):
                raise _invalid()
        networks = {
            str(getattr(scope["sandbox"], "network_mode", "") or "")
            for scope in inspected
        }
        if len(networks) != len(inspected):
            raise _invalid()
        for scope in inspected:
            if any(
                peer["container_id"] in scope["network_members"]
                for peer in inspected
                if peer is not scope
            ):
                raise _invalid()
        records = [_record(scope, inspected) for scope in inspected]
        return store.record_worker_isolation_audits(records)
    except WorkerIsolationAuditError:
        raise
    except Exception as exc:
        raise _invalid() from exc
