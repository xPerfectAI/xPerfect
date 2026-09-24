from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from workers_projects_runtime.docker_sandbox import DockerSandboxManager
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo, StubRuntime
from workers_projects_runtime.profile_runtime import CodexCliRuntime, OpenClawWorkstationRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_box import WorkspaceBoxUnavailable
from workers_projects_runtime.workspace_sandbox import WorkspaceMemberSandbox


class _ContainerSwapControlRuntime(StubRuntime):
    requires_run_start_identity = True

    def __init__(self) -> None:
        super().__init__()
        self.identity_calls = 0
        self.destructive_calls = 0

    def compute_identity(self, _worker: dict) -> dict[str, str]:
        self.identity_calls += 1
        return {
            "container_id": (
                "container-captured" if self.identity_calls == 1 else "container-replacement"
            )
        }

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.destructive_calls += 1
        return super().pause_worker(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.destructive_calls += 1
        return super().interrupt_worker(worker, run_id=run_id)


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_absent_captured_container_cleanup_does_not_probe_stale_native_session(
    tmp_path, runtime_type
):
    runtime = runtime_type(base_dir=str(tmp_path / "runtime"))
    worker_id = "wrk_absent_compute"
    (runtime._run_root(worker_id, "run_old") / "attempt-data").mkdir(
        parents=True, exist_ok=True
    )
    runtime._write_active_session(
        worker_id,
        {"session_name": "job-run_old", "run_id": "run_old", "process_pid": 4242},
    )
    runtime.sandbox.list_screen_sessions = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("absent captured compute must not query native sessions")
        )
    )
    terminated = []
    runtime.sandbox.terminate = (  # type: ignore[method-assign]
        lambda worker_id, **kwargs: terminated.append((worker_id, kwargs))
    )

    runtime.terminate_worker(
        {
            "worker_id": worker_id,
            "profile": "codex-cli" if runtime_type is CodexCliRuntime else "openclaw-general",
            "model": "synthetic-model",
            "_compute_release_container_id": "",
        }
    )
    assert terminated == [
        (worker_id, {"expected_container_id": None, "expected_absent": True})
    ]
    assert runtime._read_active_session(worker_id) is None


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_captured_container_cleanup_passes_exact_generation_to_sandbox(
    tmp_path, runtime_type
):
    runtime = runtime_type(base_dir=str(tmp_path / "runtime"))
    runtime._stop_active_process = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    terminated = []
    runtime.sandbox.terminate = (  # type: ignore[method-assign]
        lambda worker_id, **kwargs: terminated.append((worker_id, kwargs))
    )
    runtime.terminate_worker(
        {
            "worker_id": "wrk_exact_compute",
            "profile": "codex-cli" if runtime_type is CodexCliRuntime else "openclaw-general",
            "model": "synthetic-model",
            "_compute_release_container_id": "container-captured",
        }
    )
    assert terminated == [
        (
            "wrk_exact_compute",
            {"expected_container_id": "container-captured", "expected_absent": False},
        )
    ]


@pytest.mark.parametrize(
    ("captured", "current", "should_reject"),
    [
        ("", "container-replacement", True),
        ("container-captured", "container-replacement", True),
        ("", "", False),
        ("container-captured", "", False),
        ("container-captured", "container-captured", False),
    ],
)
def test_shared_member_cleanup_never_targets_replacement_generation(
    tmp_path, captured, current, should_reject
):
    calls = []
    box = SimpleNamespace(
        binding=SimpleNamespace(worker_id="wrk_exact", uid=20001),
        supervisor=tmp_path,
        stop_member=lambda container_id: calls.append(("stop", container_id)),
        release_if_sole_member=lambda container_id: calls.append(("release", container_id)),
    )
    sandbox = WorkspaceMemberSandbox.__new__(WorkspaceMemberSandbox)
    sandbox.box = box
    sandbox.inspect = lambda _worker_id: (  # type: ignore[method-assign]
        SimpleNamespace(container_id=current) if current else None
    )
    action = lambda: sandbox.terminate(
        "wrk_exact",
        expected_container_id=captured or None,
        expected_absent=not bool(captured),
    )
    if should_reject:
        with pytest.raises(WorkspaceBoxUnavailable, match="generation changed"):
            action()
    else:
        action()
    assert calls == (
        [("stop", captured), ("release", captured)]
        if captured and current == captured
        else []
    )


def test_shared_member_exact_release_session_probe_bypasses_launch_guard_only_for_captured_box():
    from workers_projects_runtime.workspace_sandbox import _member_control

    sandbox = WorkspaceMemberSandbox.__new__(WorkspaceMemberSandbox)
    sandbox.box = SimpleNamespace(binding=SimpleNamespace(worker_id='wrk_exact'))
    sandbox.workspace_mount = '/workspace'
    sandbox._desktop_env = lambda: {}
    sandbox.assert_native_launch = lambda: (_ for _ in ()).throw(
        WorkspaceBoxUnavailable('Native credential recovery or another run holds this member')
    )
    sandbox.inspect = lambda _worker_id: SimpleNamespace(container_id='captured-box')

    def exact_exec(container_name, command, **_kwargs):
        assert _member_control.get() is True
        assert container_name == 'captured-box'
        assert command == ['bash', '-c', 'screen -ls || true']
        return subprocess.CompletedProcess(command, 0, '\t123.job-run_exact\t(Detached)\n', '')

    sandbox._docker_exec = exact_exec
    assert sandbox.list_screen_sessions(
        'wrk_exact', 'grok-build', worker={
            'worker_id': 'wrk_exact', '_compute_release_container_id': 'captured-box',
        },
    ) == ['job-run_exact']
    sandbox.inspect = lambda _worker_id: None
    assert sandbox.list_screen_sessions('wrk_exact', 'grok-build', worker={
        'worker_id': 'wrk_exact', '_compute_release_container_id': '',
    }) == []
    sandbox.inspect = lambda _worker_id: SimpleNamespace(container_id='replacement-box')
    with pytest.raises(WorkspaceBoxUnavailable, match='generation changed'):
        sandbox.list_screen_sessions('wrk_exact', 'grok-build', worker={
            'worker_id': 'wrk_exact', '_compute_release_container_id': 'captured-box',
        })


@pytest.mark.parametrize(
    ("control", "claim_kind"),
    [
        ("pause", "pause_run"),
        ("interrupt", "interrupt_run"),
        ("steer", "steer_run"),
    ],
)
def test_container_generation_swap_before_control_rpc_keeps_exact_claim_fenced(
    tmp_path,
    monkeypatch,
    control: str,
    claim_kind: str,
):
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = _ContainerSwapControlRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    project = store.create_project(
        "owner-a",
        "Exact Docker control",
        "Never act on a replacement container generation",
        "openclaw-general",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Exact Docker worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        execution_mode="docker",
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Keep this exact run under control",
        state="running",
    )
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=service._executor_id
    )
    assert claimed is not None and claimed["run_id"] == run["run_id"]
    lease = store.acquire_host_run_lease(
        runtime_family="openclaw",
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker["owner_id"]),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=service._executor_id,
        conversation_limit=2,
        mission_limit=64,
        account_mission_limit=64,
        tenant_mission_limit=64,
        lease_ttl_s=300,
    )
    assert store.admit_claimed_run(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=service._executor_id,
    )
    invoked = store.mark_run_runtime_invoked(
        run["run_id"],
        lease_id=lease["lease_id"],
        executor_id=service._executor_id,
    )
    assert invoked is not None
    confirmed = store.confirm_host_run_start(
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        run_started_at=str(invoked["runtime_invoked_at"]),
        lease_id=str(lease["lease_id"]),
        startup_token=str(lease["startup_token"]),
        executor_id=service._executor_id,
        identity_kind="docker_session",
        pid=4242,
        process_group=None,
        process_start_identity=(
            f"docker:container-captured:in-process:{run['run_id']}:4242"
        ),
        container_id="container-captured",
        session_id="in-process",
    )
    assert confirmed is not None
    run = confirmed["run"]
    store.update_worker_state(worker["worker_id"], "running")

    try:
        with pytest.raises(RuntimeErrorBase, match="sandbox generation changed"):
            if control == "pause":
                service.pause_worker(worker["worker_id"], run_id=run["run_id"])
            elif control == "interrupt":
                service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
            else:
                service.steer_worker(
                    worker["worker_id"],
                    "Use the corrected objective",
                    run_id=run["run_id"],
                    idempotency_key="exact-docker-steer",
                )

        durable_worker = store.get_worker(worker["worker_id"]) or {}
        assert runtime.destructive_calls == 0
        assert durable_worker["compute_release_kind"] == claim_kind
        assert durable_worker["compute_release_container_id"] == "container-captured"
        assert durable_worker["compute_release_token"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "running"
    finally:
        service.shutdown()


def _docker_inspect_payload(container_id: str, *, paused: bool = False) -> str:
    return json.dumps(
        [
            {
                "Id": container_id,
                "State": {
                    "Status": "running",
                    "Paused": paused,
                    "Pid": 4242,
                },
                "HostConfig": {},
                "NetworkSettings": {"Ports": {}},
            }
        ]
    )


def test_docker_pause_refuses_replacement_generation_without_destructive_command(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []

    def fake_docker(args: list[str], **_kwargs):
        commands.append(args)
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                _docker_inspect_payload("container-replacement"),
                "",
            )
        raise AssertionError(f"unexpected destructive Docker command: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="generation changed"):
        manager.pause("wrk_test", expected_container_id="container-captured")

    assert commands == [["inspect", "wpr-wrk-test"]]


def test_docker_pause_addresses_and_confirms_captured_container_id(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    paused = False
    commands: list[list[str]] = []

    def fake_docker(args: list[str], **_kwargs):
        nonlocal paused
        commands.append(args)
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                _docker_inspect_payload("container-captured", paused=paused),
                "",
            )
        if args[:1] == ["pause"]:
            assert args == ["pause", "container-captured"]
            paused = True
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]

    result = manager.pause("wrk_test", expected_container_id="container-captured")

    assert result.state == "paused"
    assert commands == [
        ["inspect", "wpr-wrk-test"],
        ["pause", "container-captured"],
        ["inspect", "wpr-wrk-test"],
    ]


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_docker_runtime_pause_passes_captured_container_id(tmp_path, runtime_type):
    runtime = runtime_type(base_dir=str(tmp_path / "runtime"))
    calls: list[tuple[str, str]] = []
    runtime.sandbox.pause = (  # type: ignore[method-assign]
        lambda worker_id, *, expected_container_id=None: (
            calls.append((worker_id, str(expected_container_id or "")))
            or SimpleNamespace(state="paused")
        )
    )

    runtime.pause_worker(
        {
            "worker_id": "wrk_pause_exact",
            "profile": "codex-cli" if runtime_type is CodexCliRuntime else "openclaw-general",
            "model": "synthetic-model",
            "_compute_release_container_id": "container-captured",
        }
    )

    assert calls == [("wrk_pause_exact", "container-captured")]


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_docker_runtime_interrupt_targets_captured_container_for_each_destructive_primitive(
    tmp_path,
    runtime_type,
):
    runtime = runtime_type(base_dir=str(tmp_path / "runtime"))
    worker_id = "wrk_interrupt_exact"
    run_id = "run_interrupt_exact"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "job-run_interrupt_exact",
            "run_id": run_id,
            "process_pid": 4242,
        },
    )
    calls: list[tuple[str, str]] = []
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, _session_name, **kwargs: calls.append(
            ("screen", str(kwargs.get("expected_container_id") or ""))
        )
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, _run_id, **kwargs: calls.append(
            ("run", str(kwargs.get("expected_container_id") or ""))
        )
    )

    runtime.interrupt_worker(
        {
            "worker_id": worker_id,
            "state": "running",
            "profile": "codex-cli" if runtime_type is CodexCliRuntime else "openclaw-general",
            "model": "synthetic-model",
            "_compute_release_container_id": "container-captured",
        },
        run_id=run_id,
    )

    assert calls == [
        ("screen", "container-captured"),
        ("run", "container-captured"),
    ]
