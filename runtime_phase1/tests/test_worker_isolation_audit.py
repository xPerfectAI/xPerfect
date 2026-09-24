from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

from workers_projects_runtime.docker_sandbox import (
    PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE,
    PARALLEL_CLEAN_ROOM_POLICY_LABEL,
    PARALLEL_CLEAN_ROOM_ROLE_LABEL,
    PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL,
    FreshSandboxInspection,
    SandboxInfo,
)
from workers_projects_runtime.store import (
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    Store,
)
import workers_projects_runtime.service as service_module
from workers_projects_runtime.worker_isolation_audit import (
    WorkerIsolationAuditError,
    capture_worker_isolation_audit,
)


def _fingerprint(kind: str, value: str) -> str:
    material = f"{kind}\0{value}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _container(label: str) -> str:
    return hashlib.sha256(f"fixture-container\0{label}".encode()).hexdigest()


def _mission(store: Store, root: Path, *, owner: str, label: str) -> dict:
    workspace = root / label / "workspace"
    home = root / label / "home"
    workspace.mkdir(parents=True, mode=0o700)
    home.mkdir(parents=True, mode=0o700)
    delegation = store.reserve_delegation(
        tenant_id="local",
        owner_id=owner,
        idempotency_key=f"fixture-idempotency-{label}",
        request_digest="sha256:" + hashlib.sha256(label.encode()).hexdigest(),
        origin_ref=f"fixture-origin-{label}",
        title=f"Synthetic isolated mission {label}",
        goal="Observe isolated execution.",
        instruction="Create the requested synthetic local artifact.",
        origin_surface="web",
        worker_name=f"Synthetic worker {label}",
        worker_role="isolated worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="synthetic-model",
        execution_mode="docker",
        workspace_root=str(workspace),
        bootstrap_bundle={"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY},
    )
    worker_id = str(delegation["worker_id"])
    run_id = str(delegation["initial_run_id"])
    executor = f"fixture-executor-{label}"
    claimed = store.claim_next_queued_run(worker_id, executor_id=executor)
    assert claimed is not None
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id=owner,
        worker_id=worker_id,
        run_id=run_id,
        executor_id=executor,
        conversation_limit=8,
        mission_limit=16,
        account_mission_limit=16,
        tenant_mission_limit=32,
        lease_ttl_s=300,
    )
    assert lease is not None
    admitted = store.admit_claimed_run(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=executor,
    )
    assert admitted is not None
    invoked = store.mark_run_runtime_invoked(
        run_id,
        lease_id=str(lease["lease_id"]),
        executor_id=executor,
    )
    assert invoked is not None
    container_id = _container(label)
    session_id = f"fixture-session-{label}"
    confirmed = store.confirm_host_run_start(
        worker_id=worker_id,
        run_id=run_id,
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=executor,
        identity_kind="docker_session",
        pid=4242,
        process_group=None,
        process_start_identity=f"docker:{container_id}:{session_id}:{run_id}:4242",
        container_id=container_id,
        session_id=session_id,
    )
    assert confirmed is not None
    store.update_worker(worker_id, workspace_dir=str(workspace))
    worker = store.get_worker(worker_id, tenant_id="local", owner_id=owner)
    assert worker is not None
    network = f"fixture-mission-network-{label}"
    sandbox = SandboxInfo(
        container_name=f"fixture-worker-{label}",
        container_id=container_id,
        state="running",
        workspace_dir=str(workspace),
        home_dir=str(home),
        pid=4242,
        image="fixture-image",
        execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        network_mode=network,
        attached_networks=(network,),
        pid_mode="private",
        ipc_mode="private",
        uts_mode="private",
        userns_mode="",
        cgroupns_mode="private",
        read_only_rootfs=True,
        privileged=False,
        cap_add=(),
        cap_drop=("ALL",),
        security_options=("no-new-privileges:true",),
        bind_mount_targets=("/workspace", "/home/worker"),
        bind_mount_pairs=((str(workspace), "/workspace"), (str(home), "/home/worker")),
    )
    return {
        "delegation": delegation,
        "worker": worker,
        "run": confirmed["run"],
        "lease": store.get_active_host_run_lease_for_run(run_id),
        "sandbox": sandbox,
        "workspace": workspace,
        "home": home,
    }


class _SandboxManager:
    def __init__(self, missions: list[dict]) -> None:
        self.missions = {
            str(item["worker"]["worker_id"]): item for item in missions
        }
        self.inspected_workers: list[str] = []
        self.network_commands: list[list[str]] = []
        self.invalid_policy_for: set[str] = set()
        self.network_overrides: dict[str, dict] = {}
        self.provider_id = _container("reviewed-provider-proxy")
        self.broker_id = _container("reviewed-broker-proxy")

    def inspect_fresh(self, worker_id: str) -> FreshSandboxInspection:
        self.inspected_workers.append(worker_id)
        mission = self.missions.get(worker_id)
        if mission is None:
            return FreshSandboxInspection(status="absent")
        return FreshSandboxInspection(status="present", sandbox=mission["sandbox"])

    def _sandbox_matches_parallel_clean_room_policy(self, sandbox: SandboxInfo) -> bool:
        return (
            sandbox.container_name not in self.invalid_policy_for
            and sandbox.execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        )

    def _parallel_clean_room_configuration(self, *, require_proxy_containers: bool):
        del require_proxy_containers
        return {
            "provider_proxy_container": "fixture-provider-proxy",
            "broker_proxy_container": "fixture-broker-proxy",
        }, "healthy"

    def _parallel_clean_room_mission_network_name(self, container_name: str) -> str:
        for mission in self.missions.values():
            sandbox = mission["sandbox"]
            if sandbox.container_name == container_name:
                return str(sandbox.network_mode)
        raise RuntimeError("unknown fixture container")

    def _docker(self, arguments: list[str], **_kwargs):
        self.network_commands.append(list(arguments))
        assert arguments[:2] == ["network", "inspect"]
        network = str(arguments[2])
        mission = next(
            (item for item in self.missions.values() if item["sandbox"].network_mode == network),
            None,
        )
        if mission is None:
            return subprocess.CompletedProcess(arguments, 1, "", "fixture network absent")
        sandbox = mission["sandbox"]
        payload = {
            "Name": network,
            "Driver": "bridge",
            "Internal": True,
            "Labels": {
                PARALLEL_CLEAN_ROOM_POLICY_LABEL: PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                PARALLEL_CLEAN_ROOM_ROLE_LABEL: PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE,
                PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL: sandbox.container_name,
            },
            "Containers": {
                str(sandbox.container_id): {"Name": sandbox.container_name},
                self.provider_id: {"Name": "fixture-provider-proxy"},
                self.broker_id: {"Name": "fixture-broker-proxy"},
            },
        }
        payload.update(self.network_overrides.get(network, {}))
        return subprocess.CompletedProcess(arguments, 0, json.dumps([payload]), "")


class _Runtime:
    def __init__(self, manager: _SandboxManager):
        self.manager = manager

    def _runtime_for_worker(self, _worker: dict):
        return SimpleNamespace(sandbox=self.manager)


def _events(store: Store, mission: dict) -> list[dict]:
    return [
        event
        for event in store.list_events(str(mission["worker"]["worker_id"]))
        if event["event_type"] == "worker.isolation_probe"
    ]


def test_confirmed_container_generations_produce_owner_scoped_immutable_peer_audits(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "runtime.sqlite3"))
    first = _mission(store, tmp_path, owner="fixture-owner-primary", label="alpha")
    second = _mission(store, tmp_path, owner="fixture-owner-primary", label="bravo")
    foreign = _mission(store, tmp_path, owner="fixture-owner-foreign", label="charlie")
    manager = _SandboxManager([first, second, foreign])

    observations = capture_worker_isolation_audit(
        store=store,
        runtime=_Runtime(manager),
        worker=second["worker"],
        run_id=str(second["run"]["run_id"]),
        container_id=str(second["sandbox"].container_id),
    )

    assert len(observations) == 2
    assert set(manager.inspected_workers) == {
        str(first["worker"]["worker_id"]),
        str(second["worker"]["worker_id"]),
    }
    assert _events(store, foreign) == []
    for mission, peer in ((first, second), (second, first)):
        event = _events(store, mission)
        assert len(event) == 1
        payload = json.loads(str(event[0]["payload_json"]))
        assert payload["ownerRefHash"] == _fingerprint(
            "owner", str(mission["worker"]["owner_id"])
        )
        assert payload["workRefHash"] == _fingerprint(
            "work", str(mission["delegation"]["work_ref"])
        )
        assert payload["containerRefHash"] == _fingerprint(
            "container", str(mission["sandbox"].container_id)
        )
        assert payload["executionMode"] == "isolated_container"
        assert payload["hostStateReadable"] is False
        assert payload["serviceEnvironmentReadable"] is False
        assert payload["dockerSocketReadable"] is False
        assert payload["ambientAuthority"] is False
        assert payload["peerAccessDenied"] is True
        assert payload["hostAccessDenied"] is True
        assert payload["peerProbes"] == [
            {
                "workRefHash": _fingerprint(
                    "work", str(peer["delegation"]["work_ref"])
                ),
                "reachable": False,
            }
        ]
        serialized = json.dumps(payload, sort_keys=True)
        for private in (
            str(mission["worker"]["owner_id"]),
            str(mission["workspace"]),
            str(mission["home"]),
            str(mission["sandbox"].container_id),
        ):
            assert private not in serialized

        with store._connect() as connection:
            trace = connection.execute(
                "SELECT * FROM work_trace_events WHERE run_id = ? AND event_type = ?",
                (str(mission["run"]["run_id"]), "worker.isolation_probe"),
            ).fetchone()
            assert trace is not None
            assert json.loads(str(trace["payload_json"])) == payload
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    "UPDATE work_trace_events SET payload_json = '{}' WHERE trace_event_id = ?",
                    (str(trace["trace_event_id"]),),
                )

    repeated = capture_worker_isolation_audit(
        store=store,
        runtime=_Runtime(manager),
        worker=second["worker"],
        run_id=str(second["run"]["run_id"]),
        container_id=str(second["sandbox"].container_id),
    )
    assert len(repeated) == 2
    assert len(_events(store, first)) == len(_events(store, second)) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "replacement-container",
        "policy-mismatch",
        "external-network",
        "foreign-network-member",
        "missing-reviewed-proxy",
        "shared-workspace",
        "shared-network",
        "ambient-provider-authority",
        "host-socket-mount",
        "expired-lease",
        "foreign-owner",
    ],
)
def test_untrusted_or_cross_owner_isolation_never_creates_partial_audit(
    tmp_path: Path, mutation: str
) -> None:
    store = Store(str(tmp_path / "runtime.sqlite3"))
    first = _mission(store, tmp_path, owner="fixture-owner-primary", label="alpha")
    second = _mission(store, tmp_path, owner="fixture-owner-primary", label="bravo")
    manager = _SandboxManager([first, second])
    worker = dict(second["worker"])
    container_id = str(second["sandbox"].container_id)

    if mutation == "replacement-container":
        container_id = _container("untrusted-replacement")
    elif mutation == "policy-mismatch":
        manager.invalid_policy_for.add(str(first["sandbox"].container_name))
    elif mutation == "external-network":
        manager.network_overrides[str(first["sandbox"].network_mode)] = {
            "Internal": False
        }
    elif mutation == "foreign-network-member":
        manager.network_overrides[str(first["sandbox"].network_mode)] = {
            "Containers": {
                str(first["sandbox"].container_id): {
                    "Name": first["sandbox"].container_name
                },
                str(second["sandbox"].container_id): {
                    "Name": second["sandbox"].container_name
                },
                manager.provider_id: {"Name": "fixture-provider-proxy"},
                manager.broker_id: {"Name": "fixture-broker-proxy"},
            }
        }
    elif mutation == "missing-reviewed-proxy":
        manager.network_overrides[str(first["sandbox"].network_mode)] = {
            "Containers": {
                str(first["sandbox"].container_id): {
                    "Name": first["sandbox"].container_name
                },
                manager.provider_id: {"Name": "fixture-provider-proxy"},
            }
        }
    elif mutation == "shared-workspace":
        first["sandbox"] = replace(
            first["sandbox"], workspace_dir=str(second["sandbox"].workspace_dir)
        )
    elif mutation == "shared-network":
        first["sandbox"] = replace(
            first["sandbox"],
            network_mode=str(second["sandbox"].network_mode),
            attached_networks=(str(second["sandbox"].network_mode),),
        )
    elif mutation == "ambient-provider-authority":
        first["sandbox"] = replace(
            first["sandbox"], environment=(("OPENAI_API_KEY", "synthetic-private-value"),)
        )
    elif mutation == "host-socket-mount":
        first["sandbox"] = replace(
            first["sandbox"],
            bind_mount_pairs=(
                *first["sandbox"].bind_mount_pairs,
                ("/var/run/docker.sock", "/var/run/docker.sock"),
            ),
        )
    elif mutation == "expired-lease":
        with store._connect() as connection:
            connection.execute(
                "UPDATE host_run_leases SET expires_at = ? WHERE run_id = ?",
                ("2000-01-01T00:00:00+00:00", str(first["run"]["run_id"])),
            )
    elif mutation == "foreign-owner":
        worker["owner_id"] = "fixture-owner-foreign"

    with pytest.raises(WorkerIsolationAuditError):
        capture_worker_isolation_audit(
            store=store,
            runtime=_Runtime(manager),
            worker=worker,
            run_id=str(second["run"]["run_id"]),
            container_id=container_id,
        )

    assert _events(store, first) == []
    assert _events(store, second) == []


def test_unconfirmed_peer_is_not_misrepresented_as_a_rejected_peer(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "runtime.sqlite3"))
    mission = _mission(store, tmp_path, owner="fixture-owner-primary", label="alpha")
    manager = _SandboxManager([mission])

    observations = capture_worker_isolation_audit(
        store=store,
        runtime=_Runtime(manager),
        worker=mission["worker"],
        run_id=str(mission["run"]["run_id"]),
        container_id=str(mission["sandbox"].container_id),
    )

    assert len(observations) == 1
    payload = json.loads(str(_events(store, mission)[0]["payload_json"]))
    assert payload["peerProbes"] == []
    assert payload["peerAccessDenied"] is False
    assert payload["hostAccessDenied"] is True


def test_first_worker_receives_a_new_peer_audit_when_a_real_sibling_starts(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "runtime.sqlite3"))
    first = _mission(store, tmp_path, owner="fixture-owner-primary", label="alpha")
    manager = _SandboxManager([first])
    capture_worker_isolation_audit(
        store=store,
        runtime=_Runtime(manager),
        worker=first["worker"],
        run_id=str(first["run"]["run_id"]),
        container_id=str(first["sandbox"].container_id),
    )
    assert json.loads(str(_events(store, first)[0]["payload_json"]))["peerAccessDenied"] is False

    second = _mission(store, tmp_path, owner="fixture-owner-primary", label="bravo")
    manager.missions[str(second["worker"]["worker_id"])] = second
    capture_worker_isolation_audit(
        store=store,
        runtime=_Runtime(manager),
        worker=second["worker"],
        run_id=str(second["run"]["run_id"]),
        container_id=str(second["sandbox"].container_id),
    )

    assert len(_events(store, first)) == 2
    assert len(_events(store, second)) == 1
    first_proofs = [json.loads(str(event["payload_json"])) for event in _events(store, first)]
    assert sorted(len(proof["peerProbes"]) for proof in first_proofs) == [0, 1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("ownerRefHash", _fingerprint("owner", "fixture-owner-foreign")),
        ("containerRefHash", _fingerprint("container", _container("replacement"))),
        ("leaseRefHash", _fingerprint("lease", "replacement-lease")),
        ("producerScope", "fixture.untrusted.producer"),
    ],
)
def test_store_rejects_forged_proof_without_partial_event_or_immutable_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    store = Store(str(tmp_path / "runtime.sqlite3"))
    first = _mission(store, tmp_path, owner="fixture-owner-primary", label="alpha")
    second = _mission(store, tmp_path, owner="fixture-owner-primary", label="bravo")
    manager = _SandboxManager([first, second])
    monkeypatch.setattr(store, "record_worker_isolation_audits", lambda rows: rows)
    rows = capture_worker_isolation_audit(
        store=store,
        runtime=_Runtime(manager),
        worker=second["worker"],
        run_id=str(second["run"]["run_id"]),
        container_id=str(second["sandbox"].container_id),
    )
    monkeypatch.undo()
    rows[1]["payload"][field] = value

    with pytest.raises((RuntimeError, ValueError)):
        store.record_worker_isolation_audits(rows)

    assert _events(store, first) == []
    assert _events(store, second) == []
    with store._connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM work_trace_events WHERE event_type = ?",
            ("worker.isolation_probe",),
        ).fetchone()[0]
    assert count == 0


def test_real_confirmed_docker_start_calls_the_isolation_producer_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    worker = {
        "worker_id": "fixture-worker",
        "owner_id": "fixture-owner",
        "tenant_id": "local",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }
    container_id = _container("confirmed-start")
    run_id = "fixture-run"
    service = object.__new__(service_module.WorkersProjectsService)
    service.store = SimpleNamespace(
        confirm_host_run_start=lambda **_kwargs: (
            observed.append("confirmed")
            or {"run": {"run_id": run_id, "runtime_invoked_at": "confirmed"}}
        )
    )
    service.runtime = SimpleNamespace(requires_run_start_identity=True)
    service._executor_id = "fixture-executor"
    service._pending_run_starts_lock = Lock()
    service._pending_run_starts = {
        run_id: {
            "worker_id": worker["worker_id"],
            "worker": worker,
            "run_started_at": "confirmed",
            "lease_id": "fixture-lease",
            "startup_token": "fixture-startup-authority",
        }
    }
    refreshed_worker = {
        **worker,
        "workspace_dir": "/private/glasshive/fixture-worker/workspace",
    }
    service._refresh_runtime_info = lambda worker_id, state, last_error="": (
        observed.append("refreshed") or refreshed_worker
    )

    def capture(**kwargs):
        assert kwargs["worker"] == refreshed_worker
        assert kwargs["run_id"] == run_id
        assert kwargs["container_id"] == container_id
        observed.append("audited")
        return []

    monkeypatch.setattr(service_module, "capture_worker_isolation_audit", capture)
    service._observe_run_start(
        {
            "run_id": run_id,
            "worker_id": worker["worker_id"],
            "identity_kind": "docker_session",
            "container_id": container_id,
            "session_id": "fixture-session",
            "pid": 4242,
            "process_start_identity": (
                f"docker:{container_id}:fixture-session:{run_id}:4242"
            ),
        }
    )

    assert observed == ["confirmed", "refreshed", "audited"]
