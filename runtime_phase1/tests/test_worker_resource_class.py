from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from workers_projects_runtime import mcp_server
from workers_projects_runtime.api import create_app
from workers_projects_runtime.docker_sandbox import DockerSandboxManager
from workers_projects_runtime.mcp_server import (
    WorkersProjectsApiClient,
    create_mcp_server,
)
from workers_projects_runtime.models import CreateDelegationRequest, CreateWorkerRequest
from workers_projects_runtime.openclaw_runtime import HostCapacityError, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import (
    HostRunLeaseCapacityError,
    Store,
    WorkAdmissionError,
)


LIGHT_MEMORY_BYTES = 1536 * 1024**2
STANDARD_MEMORY_BYTES = 3072 * 1024**2
AVAILABLE_MEMORY_BYTES = int(5.62 * 1024**3)
HEADROOM_MEMORY_BYTES = 2048 * 1024**2
API_TOKEN = "synthetic-resource-class-service-token"
ASSERTION_SECRET = "synthetic-resource-class-assertion-secret"


def _delegation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "Synthetic light mission",
        "goal": "Create one synthetic artifact.",
        "instruction": "Create one synthetic artifact.",
    }
    payload.update(overrides)
    return payload


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _account_headers(idempotency_key: str) -> dict[str, str]:
    issued_at = int(time.time())
    claims = {
        "v": 1,
        "aud": "glasshive-account-api",
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "iat": issued_at,
        "exp": issued_at + 60,
        "nonce": f"nonce_{uuid.uuid4().hex}",
    }
    encoded = _b64url(
        json.dumps(
            claims,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    signature = _b64url(
        hmac.new(
            ASSERTION_SECRET.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "X-Viventium-Service-Assertion": f"{encoded}.{signature}",
        "Idempotency-Key": idempotency_key,
    }


def test_delegation_resource_class_is_typed_and_defaults_to_standard() -> None:
    standard = CreateDelegationRequest.model_validate(_delegation_payload())
    light = CreateDelegationRequest.model_validate(
        _delegation_payload(resourceClass="light")
    )

    assert standard.resource_class == "standard"
    assert light.resource_class == "light"
    assert light.model_dump(by_alias=True)["resourceClass"] == "light"

    with pytest.raises(ValidationError):
        CreateDelegationRequest.model_validate(
            _delegation_payload(resourceClass="unknown")
        )


def test_direct_worker_resource_class_is_typed_and_defaults_to_standard() -> None:
    base = {
        "owner_id": "owner-a",
        "name": "Synthetic worker",
        "role": "Create one synthetic artifact.",
    }

    standard = CreateWorkerRequest.model_validate(base)
    light = CreateWorkerRequest.model_validate({**base, "resource_class": "light"})

    assert standard.resource_class == "standard"
    assert light.resource_class == "light"

    with pytest.raises(ValidationError):
        CreateWorkerRequest.model_validate({**base, "resource_class": "unknown"})


def test_direct_no_callback_mcp_launch_persists_requested_resource_class(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    app = create_app(
        db_path=str(tmp_path / "direct-resource-class.sqlite3"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    app.state.service._ensure_worker_processor = lambda _worker_id: None

    with TestClient(app) as api:
        project_response = api.post(
            "/v1/projects",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            json={
                "owner_id": "owner-a",
                "title": "Direct resource class",
                "goal": "Prove the direct worker reservation class.",
                "default_worker_profile": "codex-cli",
            },
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["project_id"]

        api_client = WorkersProjectsApiClient(api_token=API_TOKEN)

        def in_process_request(
            method: str,
            path: str,
            *,
            json_body: dict | None = None,
            extra_headers: dict[str, str] | None = None,
        ):
            headers = {"Authorization": f"Bearer {API_TOKEN}"}
            headers.update(extra_headers or {})
            response = api.request(method, path, headers=headers, json=json_body)
            response.raise_for_status()
            return response.json()

        api_client._request = in_process_request  # type: ignore[method-assign]
        server = create_mcp_server(api_client=api_client)

        async def scenario() -> tuple[dict, dict]:
            async with Client(server) as client:
                light = await client.call_tool(
                    "worker_delegate_once",
                    {
                        "project_id": project_id,
                        "owner_id": "owner-a",
                        "title": "Direct light worker",
                        "instruction": "Create one synthetic light artifact.",
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                        "resource_class": "light",
                    },
                )
                standard = await client.call_tool(
                    "worker_delegate_once",
                    {
                        "project_id": project_id,
                        "owner_id": "owner-a",
                        "title": "Direct standard worker",
                        "instruction": "Create one synthetic standard artifact.",
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                    },
                )
                return light.structured_content, standard.structured_content

        light_result, standard_result = asyncio.run(scenario())

    workers = sorted(app.state.store.list_all_workers(), key=lambda item: item["name"])
    assert [(worker["resource_class"], worker["resource_memory_bytes"]) for worker in workers] == [
        ("light", LIGHT_MEMORY_BYTES),
        ("standard", STANDARD_MEMORY_BYTES),
    ]
    assert light_result["resource_class"] == "light"
    assert standard_result["resource_class"] == "standard"


def test_direct_no_callback_mcp_launch_rejects_missing_persisted_resource_truth(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})

    class MissingResourceTruthApi:
        def get_preferences(self):
            return {}

        def get_project(self, project_id: str):
            return {
                "project_id": project_id,
                "owner_id": "owner-a",
                "title": "Persisted truth",
                "goal": "Prove response truth.",
            }

        def find_or_resume_worker(self, **kwargs):
            return {
                "worker_id": "wrk_missing_truth",
                "project_id": kwargs["project_id"],
                "owner_id": kwargs["owner_id"],
                "profile": kwargs["profile"],
                "execution_mode": kwargs["execution_mode"],
                "alias": kwargs["alias"],
                "state": "paused",
            }

        def assign_run(self, worker_id: str, instruction: str, **_kwargs):
            return {
                "run_id": "run_missing_truth",
                "worker_id": worker_id,
                "instruction": instruction,
                "state": "queued",
            }

    server = create_mcp_server(api_client=MissingResourceTruthApi())

    async def scenario() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError, match="persisted resource_class"):
                await client.call_tool(
                    "worker_delegate_once",
                    {
                        "project_id": "prj_truth",
                        "owner_id": "owner-a",
                        "title": "Persisted truth",
                        "instruction": "Create one synthetic artifact.",
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                        "resource_class": "light",
                    },
                )

    asyncio.run(scenario())


def test_direct_no_callback_mcp_launch_rejects_mismatched_persisted_resource_truth(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})

    class MismatchedResourceTruthApi:
        def get_preferences(self):
            return {}

        def get_project(self, project_id: str):
            return {
                "project_id": project_id,
                "owner_id": "owner-a",
                "title": "Persisted truth",
                "goal": "Prove response truth.",
            }

        def find_or_resume_worker(self, **kwargs):
            return {
                "worker_id": "wrk_mismatched_truth",
                "project_id": kwargs["project_id"],
                "owner_id": kwargs["owner_id"],
                "profile": kwargs["profile"],
                "execution_mode": kwargs["execution_mode"],
                "resource_class": "standard",
                "alias": kwargs["alias"],
                "state": "paused",
            }

        def assign_run(self, *_args, **_kwargs):
            raise AssertionError("a mismatched worker must not receive a run")

    server = create_mcp_server(api_client=MismatchedResourceTruthApi())

    async def scenario() -> None:
        async with Client(server) as client:
            with pytest.raises(
                ToolError,
                match="persisted resource_class .* does not match requested resource_class",
            ):
                await client.call_tool(
                    "worker_delegate_once",
                    {
                        "project_id": "prj_truth",
                        "owner_id": "owner-a",
                        "title": "Persisted truth",
                        "instruction": "Create one synthetic artifact.",
                        "profile": "codex-cli",
                        "execution_mode": "docker",
                        "resource_class": "light",
                    },
                )

    asyncio.run(scenario())


def _find_or_create_resource_worker(
    service: WorkersProjectsService,
    project_id: str,
    *,
    name: str,
    alias: str,
    resource_class: str,
) -> dict:
    return service.find_or_create_worker(
        project_id=project_id,
        owner_id="owner-a",
        name=name,
        role="Create one synthetic artifact.",
        profile="codex-cli",
        backend="codex-cli",
        alias=alias,
        execution_mode="docker",
        tenant_id="tenant-a",
        start_synchronously=False,
        resource_class=resource_class,
    )


@pytest.mark.parametrize("resource_class", ["light", "standard"])
def test_find_or_create_worker_reuses_only_the_same_resource_class(
    tmp_path,
    resource_class: str,
) -> None:
    store = Store(str(tmp_path / f"same-{resource_class}.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._reserved_runtime_preflight = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]
    project = store.create_project(
        "owner-a",
        "Resource class reuse",
        "Prove reuse identity.",
        "codex-cli",
        tenant_id="tenant-a",
    )
    try:
        original = _find_or_create_resource_worker(
            service,
            project["project_id"],
            name="Original worker",
            alias="same-class-worker",
            resource_class=resource_class,
        )
        resumed = _find_or_create_resource_worker(
            service,
            project["project_id"],
            name="Resumed worker",
            alias="same-class-worker",
            resource_class=resource_class,
        )

        assert resumed["worker_id"] == original["worker_id"]
        assert resumed["resource_class"] == resource_class
        assert resumed["resource_memory_bytes"] == {
            "light": LIGHT_MEMORY_BYTES,
            "standard": STANDARD_MEMORY_BYTES,
        }[resource_class]
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("stored_resource_class", "requested_resource_class"),
    [("light", "standard"), ("standard", "light")],
)
def test_find_or_create_worker_rejects_resource_class_mismatch_without_mutation(
    tmp_path,
    stored_resource_class: str,
    requested_resource_class: str,
) -> None:
    store = Store(str(tmp_path / f"mismatch-{stored_resource_class}.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service._reserved_runtime_preflight = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]
    project = store.create_project(
        "owner-a",
        "Resource class mismatch",
        "Prove worker identity is immutable.",
        "codex-cli",
        tenant_id="tenant-a",
    )
    try:
        original = _find_or_create_resource_worker(
            service,
            project["project_id"],
            name="Original worker",
            alias="mismatched-class-worker",
            resource_class=stored_resource_class,
        )

        with pytest.raises(
            WorkAdmissionError,
            match=(
                f"stored resource_class '{stored_resource_class}' does not match "
                f"requested resource_class '{requested_resource_class}'"
            ),
        ) as captured:
            _find_or_create_resource_worker(
                service,
                project["project_id"],
                name="Must not replace original",
                alias="mismatched-class-worker",
                resource_class=requested_resource_class,
            )

        assert captured.value.code == "worker_resource_class_mismatch"
        stored = store.get_worker(original["worker_id"])
        assert stored is not None
        assert stored["name"] == "Original worker"
        assert stored["resource_class"] == stored_resource_class
        assert stored["resource_memory_bytes"] == {
            "light": LIGHT_MEMORY_BYTES,
            "standard": STANDARD_MEMORY_BYTES,
        }[stored_resource_class]
    finally:
        service.shutdown()


def test_account_api_maps_light_resource_class_to_persistence_and_detail(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET",
        ASSERTION_SECRET,
    )
    app = create_app(
        db_path=str(tmp_path / "resource-class-api.sqlite3"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None

    with TestClient(app) as client:
        response = client.post(
            "/v1/delegations",
            headers=_account_headers("light-api-accept"),
            json=_delegation_payload(resourceClass="light"),
        )
        assert response.status_code == 202
        work_ref = response.json()["workRef"]

        detail = client.get(
            f"/v1/work/{work_ref}",
            headers=_account_headers("light-api-detail"),
        )
        assert detail.status_code == 200
        assert detail.json()["resourceClass"] == "light"
        assert detail.json()["resourceReservation"] == {
            "memoryBytes": LIGHT_MEMORY_BYTES
        }

        rejected = client.post(
            "/v1/delegations",
            headers=_account_headers("unknown-api-reject"),
            json=_delegation_payload(resourceClass="unknown"),
        )
        assert rejected.status_code == 422

    workers = app.state.store.list_all_workers()
    assert len(workers) == 1
    assert workers[0]["resource_class"] == "light"
    assert workers[0]["resource_memory_bytes"] == LIGHT_MEMORY_BYTES


def test_light_resource_class_sets_one_exact_docker_memory_and_swap_limit(
    tmp_path,
) -> None:
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []

    def fake_docker(
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(
                ["docker", *args], returncode=1, stdout="", stderr="not found"
            )
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=0, stdout="cid", stderr=""
        )

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._create_container(
        "wpr-light",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
        resource_memory_bytes=LIGHT_MEMORY_BYTES,
    )

    command = next(item for item in commands if item and item[0] == "run")
    expected = f"{LIGHT_MEMORY_BYTES}b"
    assert command[command.index("--memory") + 1] == expected
    assert command[command.index("--memory-swap") + 1] == expected


def _service_delegation_kwargs(
    suffix: str,
    *,
    resource_class: str,
) -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "idempotency_key": f"resource-class-{suffix}",
        "request_digest": f"digest-{suffix}",
        "origin_ref": f"telegram:{suffix}",
        "title": f"Resource class {suffix}",
        "goal": "Create one synthetic artifact.",
        "instruction": "Create one synthetic artifact.",
        "origin_surface": "telegram",
        "worker_name": f"Worker {suffix}",
        "worker_role": "General intelligent worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "resource_class": resource_class,
    }


def _run_concurrent_delegations(
    database: str,
    *,
    resource_class: str,
) -> tuple[list[dict], list[HostCapacityError]]:
    probe_barrier = Barrier(2)

    class MeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only: bool = False):
            probe_barrier.wait(timeout=3)
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": AVAILABLE_MEMORY_BYTES,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 0,
                "running_worker_ids": [],
                "worker_process_counts": {},
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    services = [
        WorkersProjectsService(
            Store(database), MeasuredRuntime(), reconcile_on_startup=False
        )
        for _ in range(2)
    ]
    for service in services:
        service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]

    def submit(index: int):
        try:
            return services[index].reserve_delegation(
                **_service_delegation_kwargs(
                    f"{resource_class}-{index}",
                    resource_class=resource_class,
                )
            )
        except HostCapacityError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
    finally:
        for service in services:
            service.shutdown()

    return (
        [result for result in results if isinstance(result, dict)],
        [result for result in results if isinstance(result, HostCapacityError)],
    )


def test_two_light_delegations_are_atomically_accepted_at_5_62_gib(tmp_path) -> None:
    database = str(tmp_path / "two-light.sqlite3")

    accepted, blocked = _run_concurrent_delegations(
        database,
        resource_class="light",
    )

    assert len(accepted) == 2
    assert blocked == []
    store = Store(database)
    workers = store.list_all_workers()
    leases = store.list_active_host_run_leases()
    assert {worker["resource_class"] for worker in workers} == {"light"}
    assert {worker["resource_memory_bytes"] for worker in workers} == {
        LIGHT_MEMORY_BYTES
    }
    assert {lease["reserved_memory_bytes"] for lease in leases} == {
        LIGHT_MEMORY_BYTES
    }

    restarted = Store(database)
    assert {
        (worker["resource_class"], worker["resource_memory_bytes"])
        for worker in restarted.list_all_workers()
    } == {("light", LIGHT_MEMORY_BYTES)}


def test_two_standard_delegations_have_one_capacity_winner_at_5_62_gib(
    tmp_path,
) -> None:
    database = str(tmp_path / "two-standard.sqlite3")

    accepted, blocked = _run_concurrent_delegations(
        database,
        resource_class="standard",
    )

    assert len(accepted) == 2
    assert blocked == []
    store = Store(database)
    assert len(store.list_all_workers()) == 2
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM host_run_leases WHERE status = 'active'").fetchone()[0] == 1


def _claimed_worker_run(store: Store, suffix: str) -> tuple[dict, dict]:
    project = store.create_project(
        f"owner-{suffix}",
        f"Project {suffix}",
        "Capacity",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id=f"owner-{suffix}",
        name=f"Worker {suffix}",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    store.create_run(worker["worker_id"], project["project_id"], f"Run {suffix}")
    run = store.claim_next_queued_run(worker["worker_id"])
    assert run is not None
    return worker, run


def test_newly_confirmed_unobserved_lease_still_consumes_atomic_capacity(
    tmp_path,
) -> None:
    store = Store(str(tmp_path / "confirmed-race.sqlite3"))
    first_worker, first_run = _claimed_worker_run(store, "first")
    second_worker, second_run = _claimed_worker_run(store, "second")
    available = {
        "childProcesses": 64,
        "threads": 2048,
        "memoryBytes": AVAILABLE_MEMORY_BYTES,
        "diskBytes": 64 * 1024**3,
    }
    required = {
        "childProcesses": 1,
        "threads": 1,
        "memoryBytes": HEADROOM_MEMORY_BYTES,
        "diskBytes": 1024**3,
    }
    reservation = {
        "childProcesses": 20,
        "threads": 512,
        "memoryBytes": STANDARD_MEMORY_BYTES,
        "diskBytes": 1024**3,
    }
    first_lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id=first_worker["owner_id"],
        worker_id=first_worker["worker_id"],
        run_id=first_run["run_id"],
        executor_id="executor-first",
        conversation_limit=2,
        mission_limit=4,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        capacity_available=available,
        capacity_required=required,
        capacity_reservation=reservation,
        capacity_observed_lease_ids=[],
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE host_run_leases SET startup_state = 'confirmed' WHERE lease_id = ?",
            (first_lease["lease_id"],),
        )

    with pytest.raises(HostRunLeaseCapacityError) as captured:
        store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id="local",
            owner_id=second_worker["owner_id"],
            worker_id=second_worker["worker_id"],
            run_id=second_run["run_id"],
            executor_id="executor-second",
            conversation_limit=2,
            mission_limit=4,
            account_mission_limit=4,
            tenant_mission_limit=12,
            lease_ttl_s=30,
            capacity_available=available,
            capacity_required=required,
            capacity_reservation=reservation,
            capacity_observed_lease_ids=[],
        )

    assert captured.value.capacity_class == "resource_pressure"
    assert len(store.list_active_host_run_leases()) == 1
