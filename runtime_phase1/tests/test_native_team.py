from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from workers_projects_runtime.native_team import (
    NativeTeamProjection,
    probe_claude_agent_view,
    project_native_events,
)
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.profile_runtime import (
    BaseCliWorkerRuntime,
    CodexCliRuntime,
    HostClaudeCodeRuntime,
    HostCodexCliRuntime,
    ProfiledWorkerRuntime,
)
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import RunRestorationState, Store


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _event(event_type: str, **payload: object) -> dict[str, object]:
    return {"event_type": event_type, "payload": payload}


def _invoke_test_run(store: Store, worker: dict, run: dict, *, executor_id: str) -> dict:
    claimed = store.claim_next_queued_run(
        worker["worker_id"], executor_id=executor_id
    )
    assert claimed is not None
    lease = store.acquire_host_run_lease(
        runtime_family=str(worker.get("profile") or "native"),
        lane="mission",
        tenant_id=str(worker.get("tenant_id") or "local"),
        owner_id=str(worker.get("owner_id") or "owner"),
        worker_id=str(worker["worker_id"]),
        run_id=str(run["run_id"]),
        executor_id=executor_id,
        conversation_limit=2,
        mission_limit=2,
        account_mission_limit=2,
        tenant_mission_limit=2,
        lease_ttl_s=300,
    )
    admitted = store.admit_claimed_run(
        str(run["run_id"]),
        lease_id=str(lease["lease_id"]),
        executor_id=executor_id,
    )
    assert admitted is not None
    running = store.mark_run_runtime_invoked(
        str(run["run_id"]),
        lease_id=str(lease["lease_id"]),
        executor_id=executor_id,
    )
    assert running is not None
    return running


def test_codex_jsonl_projects_session_children_and_team_messages_without_prompt_text():
    lines = [
        {"type": "thread.started", "thread_id": "thread-root"},
        {
            "type": "sub_agent_activity",
            "event_id": "evt-start",
            "occurred_at_ms": 1_786_533_600_000,
            "agent_thread_id": "thread-child",
            "agent_path": "reviewer",
            "kind": "started",
            "prompt": "private instruction must never persist",
        },
        {
            "type": "sub_agent_activity",
            "event_id": "evt-message",
            "occurred_at_ms": 1_786_533_601_000,
            "agent_thread_id": "thread-child",
            "agent_path": "reviewer",
            "kind": "interacted",
            "message": "private inter-agent text must never persist",
        },
        {
            "type": "item.completed",
            "item": {
                "id": "call-1",
                "type": "collab_agent_tool_call",
                "tool": "wait_agent",
                "status": "completed",
                "receiver_thread_ids": ["thread-child"],
                "agents_states": {"thread-child": "completed"},
            },
        },
    ]

    projected = [
        event
        for line in lines
        for event in project_native_events("codex", line, observed_at=NOW)
    ]

    assert [event["event_type"] for event in projected] == [
        "provider.session.started",
        "provider.child.started",
        "provider.child.updated",
        "provider.team.message",
        "provider.child.completed",
    ]
    assert projected[0]["payload"]["sessionId"] == "thread-root"
    assert projected[1]["payload"] == {
        "childRef": "thread-child",
        "role": "reviewer",
        "state": "running",
        "providerEventRef": "evt-start",
        "observedAt": NOW.isoformat(),
    }
    encoded = json.dumps(projected)
    assert "private instruction" not in encoded
    assert "private inter-agent" not in encoded


def test_codex_collaboration_tool_projects_sent_team_message_without_body():
    value = {
        "type": "item.completed",
        "item": {
            "id": "call-2",
            "type": "collab_agent_tool_call",
            "tool": "send_message",
            "receiver_thread_ids": ["thread-child"],
            "prompt": "secret body",
        },
    }

    assert project_native_events("codex", value, observed_at=NOW) == [
        {
            "event_type": "provider.team.message",
            "payload": {
                "childRef": "thread-child",
                "direction": "sent",
                "observedAt": NOW.isoformat(),
            },
        }
    ]


def test_installed_codex_collab_tool_call_schema_projects_spawn_and_terminal_state():
    started = {
        "type": "item.completed",
        "item": {
            "id": "call-spawn",
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": "root-thread",
            "receiver_thread_ids": ["child-thread"],
            "receiver_agents": [{"thread_id": "child-thread", "role": "reviewer"}],
            "prompt": "private delegation",
            "agents_states": {"child-thread": {"status": "running"}},
            "status": "completed",
        },
    }
    finished = {
        "type": "item.completed",
        "item": {
            "id": "call-wait",
            "type": "collab_tool_call",
            "tool": "wait",
            "sender_thread_id": "root-thread",
            "receiver_thread_ids": ["child-thread"],
            "receiver_agents": [],
            "prompt": None,
            "agents_states": {"child-thread": {"status": "completed"}},
            "status": "completed",
        },
    }

    projected = project_native_events("codex", started, observed_at=NOW)
    projected.extend(project_native_events("codex", finished, observed_at=NOW))

    assert [event["event_type"] for event in projected] == [
        "provider.child.started",
        "provider.child.updated",
        "provider.child.completed",
    ]
    assert projected[0]["payload"]["role"] == "reviewer"
    assert "private delegation" not in json.dumps(projected)


def test_claude_stream_json_projects_authoritative_background_level_and_task_edges():
    lines = [
        {"type": "system", "subtype": "init", "session_id": "claude-root"},
        {
            "type": "system",
            "subtype": "task_started",
            "session_id": "claude-root",
            "task_id": "task-1",
            "tool_use_id": "tool-1",
            "description": "Inspect implementation",
            "subagent_type": "reviewer",
            "prompt": "secret prompt is intentionally ignored",
        },
        {
            "type": "system",
            "subtype": "task_progress",
            "session_id": "claude-root",
            "task_id": "task-1",
            "tool_use_id": "tool-1",
            "description": "Inspect implementation",
            "subagent_type": "reviewer",
            "summary": "private progress is intentionally ignored",
        },
        {
            "type": "system",
            "subtype": "background_tasks_changed",
            "session_id": "claude-root",
            "tasks": [
                {"task_id": "task-1", "task_type": "agent", "description": "Review"},
                {"task_id": "task-2", "task_type": "agent", "description": "Test"},
            ],
        },
        {
            "type": "system",
            "subtype": "task_notification",
            "session_id": "claude-root",
            "task_id": "task-1",
            "status": "failed",
            "summary": "private failure detail is intentionally ignored",
            "output_file": "/private/path",
        },
    ]

    projected = [
        event
        for line in lines
        for event in project_native_events("claude", line, observed_at=NOW)
    ]

    assert [event["event_type"] for event in projected] == [
        "provider.session.started",
        "provider.child.started",
        "provider.child.updated",
        "provider.child.snapshot",
        "provider.child.failed",
    ]
    assert projected[3]["payload"] == {
        "children": [
            {"childRef": "task-1", "role": "agent", "state": "running"},
            {"childRef": "task-2", "role": "agent", "state": "running"},
        ],
        "replace": True,
        "observedAt": NOW.isoformat(),
    }
    encoded = json.dumps(projected)
    assert "secret prompt" not in encoded
    assert "private progress" not in encoded
    assert "private failure" not in encoded
    assert "/private/path" not in encoded


def test_native_team_projection_settles_until_children_finish_then_releases():
    projection = NativeTeamProjection(provider="codex", observable=True)
    projection.apply(_event("provider.child.started", childRef="child-a", state="running"), now=NOW)

    decision = projection.settlement(root_exited_at=NOW, now=NOW + timedelta(seconds=10))
    assert decision.state == "settling"
    assert decision.active_child_refs == ("child-a",)

    projection.apply(
        _event("provider.child.completed", childRef="child-a", state="completed"),
        now=NOW + timedelta(seconds=20),
    )
    decision = projection.settlement(
        root_exited_at=NOW,
        now=NOW + timedelta(seconds=21),
    )
    assert decision.state == "ready"
    assert projection.summary()["activeCount"] == 0


def test_native_team_projection_bounds_lost_child_reconciliation_at_120_seconds():
    projection = NativeTeamProjection(provider="claude", observable=True)
    projection.apply(_event("provider.child.started", childRef="child-lost", state="running"), now=NOW)

    before = projection.settlement(
        root_exited_at=NOW,
        now=NOW + timedelta(seconds=119),
    )
    after = projection.settlement(
        root_exited_at=NOW,
        now=NOW + timedelta(seconds=120),
    )

    assert before.state == "settling"
    assert after.state == "degraded"
    assert after.lost_child_refs == ("child-lost",)
    assert projection.summary()["children"][0]["state"] == "unknown"


def test_unobservable_provider_never_claims_or_holds_native_children():
    projection = NativeTeamProjection(provider="claude", observable=False)
    projection.apply(_event("provider.child.started", childRef="ignored", state="running"), now=NOW)

    assert projection.summary() is None
    assert projection.settlement(root_exited_at=NOW, now=NOW).state == "ready"


def test_projection_rehydrates_durable_summary_before_applying_terminal_child_event():
    projection = NativeTeamProjection(provider="codex", observable=True)
    projection.apply(_event("provider.child.started", childRef="child-a", state="running"), now=NOW)

    rehydrated = NativeTeamProjection.from_summary(projection.summary())
    rehydrated.apply(
        _event("provider.child.completed", childRef="child-a", state="completed"),
        now=NOW + timedelta(seconds=1),
    )

    assert rehydrated.summary()["activeCount"] == 0
    assert rehydrated.summary()["children"][0]["state"] == "completed"


def test_service_persists_native_session_capability_child_summary_and_structured_events(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Mission", "Goal", "codex-cli")
    worker = store.create_worker(
        project["project_id"],
        "owner",
        "Worker",
        "Research",
        "codex-cli",
        "codex-cli",
        "codex-cli",
        "gpt",
        execution_mode="host",
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Do work",
        state=RunRestorationState.RUNNING,
    )
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        max_workers=1,
        reconcile_on_startup=False,
    )
    try:
        service._observe_native_event(
            {
                "worker_id": worker["worker_id"],
                "run_id": run["run_id"],
                "provider": "codex",
                "event": {
                    "event_type": "provider.session.started",
                    "payload": {"sessionId": "thread-root", "observedAt": NOW.isoformat()},
                },
            }
        )
        session_only = store.get_run(run["run_id"])
        assert json.loads(session_only["native_capabilities_json"]) == {
            "childProjection": False,
            "provider": "codex",
            "providerStream": True,
        }
        service._observe_native_event(
            {
                "worker_id": worker["worker_id"],
                "run_id": run["run_id"],
                "provider": "codex",
                "event": {
                    "event_type": "provider.child.started",
                    "payload": {
                        "childRef": "thread-child",
                        "role": "reviewer",
                        "state": "running",
                        "observedAt": NOW.isoformat(),
                    },
                },
            }
        )

        durable = store.get_run(run["run_id"])
        assert durable["native_session_id"] == "thread-root"
        assert json.loads(durable["native_capabilities_json"]) == {
            "childProjection": True,
            "provider": "codex",
            "providerStream": True,
        }
        summary = json.loads(durable["native_child_summary_json"])
        assert summary["activeCount"] == 1
        assert summary["children"][0]["childRef"] == "thread-child"
        native_events = [
            event
            for event in store.list_events(worker["worker_id"])
            if event["event_type"].startswith("provider.")
        ]
        assert [event["event_type"] for event in native_events] == [
            "provider.session.started",
            "provider.child.started",
        ]
        assert json.loads(native_events[1]["payload_json"])["childRef"] == "thread-child"
    finally:
        service.shutdown()


def test_service_holds_root_in_settling_then_marks_lost_child_degraded(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_NATIVE_CHILD_RECONCILE_SECONDS", "0.02")
    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Mission", "Goal", "claude-code")
    worker = store.create_worker(
        project["project_id"],
        "owner",
        "Worker",
        "Research",
        "claude-code",
        "claude-code",
        "claude-code",
        "claude",
        execution_mode="host",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Do work")
    run = _invoke_test_run(
        store, worker, run, executor_id="native-settling-owner"
    )
    projection = NativeTeamProjection(provider="claude", observable=True)
    projection.apply(_event("provider.child.started", childRef="child-lost", state="running"), now=NOW)
    store.update_run(
        run["run_id"],
        native_capabilities_json=json.dumps(
            {"provider": "claude", "providerStream": True, "childProjection": True}
        ),
        native_child_summary_json=json.dumps(projection.summary()),
    )
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        max_workers=1,
        reconcile_on_startup=False,
    )
    try:
        expected_state = service._settle_native_children(worker, run, "Root result")
        durable = store.get_run(run["run_id"])
        summary = json.loads(durable["native_child_summary_json"])

        assert expected_state == "settling"
        assert durable["state"] == "settling"
        assert durable["output_text"] == "Root result"
        assert summary["degraded"] is True
        assert summary["lostChildRefs"] == ["child-lost"]
        assert summary["children"][0]["state"] == "unknown"
        assert any(
            event["event_type"] == "provider.child.reconciliation_lost"
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_claude_agent_view_probe_requires_explicit_flag_and_isolated_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    isolated_root = tmp_path / "run-home"
    config_dir = isolated_root / "claude"
    config_dir.mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, str]]] = []

    class Completed:
        returncode = 0
        stdout = json.dumps(
            [
                {
                    "id": "job-1",
                    "sessionId": "background-1",
                    "state": "working",
                    "name": "reviewer",
                    "kind": "background",
                    "cwd": str(workspace),
                }
            ]
        )
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)

    disabled = probe_claude_agent_view(
        binary="claude",
        workspace=workspace,
        child_env={"CLAUDE_CONFIG_DIR": str(config_dir)},
        isolated_root=isolated_root,
        enabled=False,
    )
    assert disabled.observable is False
    assert calls == []

    unsafe = probe_claude_agent_view(
        binary="claude",
        workspace=workspace,
        child_env={"CLAUDE_CONFIG_DIR": str(tmp_path / "outside")},
        isolated_root=isolated_root,
        enabled=True,
    )
    assert unsafe.observable is False
    assert unsafe.reason == "claude_config_not_isolated"
    assert calls == []

    result = probe_claude_agent_view(
        binary="claude",
        workspace=workspace,
        child_env={"CLAUDE_CONFIG_DIR": str(config_dir), "SAFE": "1"},
        isolated_root=isolated_root,
        enabled=True,
    )
    assert result.observable is True
    assert result.events == (
        {
            "event_type": "provider.child.snapshot",
            "payload": {
                "children": [
                    {"childRef": "background-1", "role": "reviewer", "state": "running"}
                ],
                "replace": True,
            },
        },
    )
    assert calls[0][0] == [
        "claude",
        "agents",
        "--json",
        "--all",
        "--cwd",
        str(workspace),
    ]
    assert calls[0][1]["CLAUDE_CONFIG_DIR"] == str(config_dir)


def test_claude_agent_view_probe_fails_closed_on_unknown_or_malformed_schema(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    isolated_root = tmp_path / "run-home"
    config_dir = isolated_root / "claude"
    config_dir.mkdir(parents=True)

    class Completed:
        returncode = 0
        stdout = '{"unexpected": true}'
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Completed())
    result = probe_claude_agent_view(
        binary="claude",
        workspace=workspace,
        child_env={"CLAUDE_CONFIG_DIR": str(config_dir)},
        isolated_root=isolated_root,
        enabled=True,
    )

    assert result.observable is False
    assert result.reason == "claude_agent_view_schema_unrecognized"
    assert result.events == ()


def test_claude_agent_view_probe_never_projects_root_interactive_session_as_child(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    isolated_root = tmp_path / "run-home"
    config_dir = isolated_root / "claude"
    config_dir.mkdir(parents=True)

    class Completed:
        returncode = 0
        stdout = json.dumps(
            [
                {
                    "kind": "interactive",
                    "sessionId": "root-session",
                    "state": "working",
                    "name": "root",
                }
            ]
        )
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Completed())
    result = probe_claude_agent_view(
        binary="claude",
        workspace=workspace,
        child_env={"CLAUDE_CONFIG_DIR": str(config_dir)},
        isolated_root=isolated_root,
        enabled=True,
    )

    assert result.observable is False
    assert result.reason == "claude_agent_view_no_background_children"
    assert result.events == ()


def test_unknown_provider_stream_schema_emits_nothing_and_cannot_claim_observability():
    assert project_native_events("codex", {"type": "future.unknown", "child_id": "x"}) == []
    assert project_native_events("claude", {"type": "system", "subtype": "future_unknown"}) == []
    assert project_native_events("openclaw", {"type": "sub_agent_activity"}) == []


def test_host_codex_live_tail_forwards_normalized_native_events_with_exact_run(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker_id = "wrk_codex"
    run_id = "run_codex"
    runtime._state_dir(worker_id).mkdir(parents=True, exist_ok=True)
    stdout = tmp_path / "codex.jsonl"
    stdout.write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "root-thread"}),
                json.dumps(
                    {
                        "type": "sub_agent_activity",
                        "event_id": "event-1",
                        "agent_thread_id": "child-thread",
                        "agent_path": "reviewer",
                        "kind": "started",
                    }
                ),
            ]
        )
        + "\n"
    )
    observed: list[dict[str, object]] = []
    runtime.set_native_event_observer(observed.append)
    stopped = Event()
    stopped.set()

    runtime._observe_native_session_events(worker_id, stdout, stopped, run_id)

    assert [item["event"]["event_type"] for item in observed] == [
        "provider.session.started",
        "provider.child.started",
    ]
    assert all(item["run_id"] == run_id for item in observed)
    assert all(item["provider"] == "codex" for item in observed)


def test_host_claude_live_tail_forwards_background_level_snapshot(tmp_path):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path))
    worker_id = "wrk_claude"
    run_id = "run_claude"
    runtime._state_dir(worker_id).mkdir(parents=True, exist_ok=True)
    stdout = tmp_path / "claude.jsonl"
    stdout.write_text(
        "\n".join(
            [
                json.dumps(
                    {"type": "system", "subtype": "init", "session_id": "root-session"}
                ),
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "background_tasks_changed",
                        "session_id": "root-session",
                        "tasks": [
                            {
                                "task_id": "child-task",
                                "task_type": "agent",
                                "description": "Private description is not projected",
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n"
    )
    observed: list[dict[str, object]] = []
    runtime.set_native_event_observer(observed.append)
    stopped = Event()
    stopped.set()

    runtime._observe_native_session_events(worker_id, stdout, stopped, run_id)

    assert [item["event"]["event_type"] for item in observed] == [
        "provider.session.started",
        "provider.child.snapshot",
    ]
    assert "Private description" not in json.dumps(observed)
    assert all(item["provider"] == "claude" for item in observed)


def test_profiled_runtime_registers_native_observer_for_isolated_docker_adapters(
    tmp_path,
):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path))
    observed: list[dict[str, object]] = []

    runtime.set_native_event_observer(observed.append)

    assert runtime.codex._native_event_observer == observed.append
    assert runtime.claude._native_event_observer == observed.append


@pytest.mark.parametrize(
    ("runtime_name", "profile", "stream_lines", "expected_types"),
    [
        (
            "codex-cli",
            "codex-cli",
            [
                {"type": "thread.started", "thread_id": "docker-root"},
                {
                    "type": "sub_agent_activity",
                    "event_id": "docker-child-start",
                    "agent_thread_id": "docker-child",
                    "agent_path": "reviewer",
                    "kind": "started",
                },
            ],
            ["provider.session.started", "provider.child.started"],
        ),
        (
            "claude-code",
            "claude-code",
            [
                {"type": "system", "subtype": "init", "session_id": "docker-root"},
                {
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": "docker-child",
                    "subagent_type": "reviewer",
                },
            ],
            ["provider.session.started", "provider.child.started"],
        ),
    ],
)
def test_isolated_docker_run_tails_native_jsonl_with_exact_run_binding(
    tmp_path,
    runtime_name,
    profile,
    stream_lines,
    expected_types,
):
    class DockerStreamRuntime(BaseCliWorkerRuntime):
        worker_root_name = f"docker_{runtime_name}_test"

        def resolve_model(self, _profile: str) -> str:
            return "synthetic/model"

        def _build_command(self, worker, instruction, info):
            _ = worker, instruction, info
            return ["synthetic-cli"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            _ = worker, stderr, info
            return None, "FINAL REPORT:\nSynthetic Docker result"

    DockerStreamRuntime.runtime_name = runtime_name
    runtime = DockerStreamRuntime(base_dir=str(tmp_path))
    run_id = f"run-docker-{profile}"
    worker = {
        "worker_id": f"wrk-docker-{profile}",
        "name": "Docker native team",
        "profile": profile,
        "execution_mode": "docker",
    }

    class FakeSandbox:
        container_name = "wpr-native-team"
        container_id = "a" * 64
        pid = 123

    fake_sandbox = FakeSandbox()
    runtime.sandbox.ensure_ready = lambda *args, **kwargs: fake_sandbox  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda _worker_id: fake_sandbox  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_start(worker_id, _runtime_name, _session_name, _command, **_kwargs):
        run_root = runtime._run_root(worker_id, run_id)
        (run_root / "stdout.log").write_text(
            "\n".join(json.dumps(line) for line in stream_lines) + "\n"
        )
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], 0, "", "")

    runtime.sandbox.start_screen_session = fake_start  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 4321  # type: ignore[method-assign]
    observed: list[dict[str, object]] = []
    run_starts: list[dict[str, object]] = []
    runtime.set_native_event_observer(observed.append)
    runtime.set_run_start_observer(run_starts.append)

    assert runtime.run_task(worker, "Observe the native team.", run_id=run_id) == (
        "FINAL REPORT:\nSynthetic Docker result"
    )

    assert [item["event"]["event_type"] for item in observed] == expected_types
    assert {item["run_id"] for item in observed} == {run_id}
    assert {item["provider"] for item in observed} == {
        "codex" if profile == "codex-cli" else "claude"
    }
    assert len(run_starts) == 1
    assert run_starts[0]["identity_kind"] == "docker_session"
    assert run_starts[0]["container_id"] == "a" * 64


def test_docker_native_child_projection_holds_root_in_bounded_settling(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_NATIVE_CHILD_RECONCILE_SECONDS", "0.02")
    store = Store(str(tmp_path / "docker-native-settling.db"))
    project = store.create_project("owner", "Docker mission", "Goal", "codex-cli")
    worker = store.create_worker(
        project["project_id"],
        "owner",
        "Docker worker",
        "Research",
        "codex-cli",
        "codex-cli",
        "codex-cli",
        "gpt",
        execution_mode="docker",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Do work")
    run = _invoke_test_run(
        store, worker, run, executor_id="docker-native-settling-owner"
    )
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "docker-runtime"))
    service = WorkersProjectsService(
        store, runtime, max_workers=1, reconcile_on_startup=False
    )
    runtime._state_dir(worker["worker_id"]).mkdir(parents=True, exist_ok=True)
    stdout = tmp_path / "docker-child.jsonl"
    stdout.write_text(
        json.dumps(
            {
                "type": "sub_agent_activity",
                "event_id": "docker-child-live",
                "agent_thread_id": "docker-child-live",
                "agent_path": "reviewer",
                "kind": "started",
            }
        )
        + "\n"
    )
    stopped = Event()
    stopped.set()
    try:
        runtime._observe_native_session_events(
            worker["worker_id"], stdout, stopped, run["run_id"]
        )
        expected_state = service._settle_native_children(
            worker, run, "Docker root result"
        )
        durable = store.get_run(run["run_id"])
        summary = json.loads(durable["native_child_summary_json"])
    finally:
        service.shutdown()

    assert expected_state == "settling"
    assert durable["state"] == "settling"
    assert summary["degraded"] is True
    assert summary["lostChildRefs"] == ["docker-child-live"]
