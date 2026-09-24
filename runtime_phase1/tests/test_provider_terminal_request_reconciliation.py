from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


def test_recovery_reconciles_abandoned_terminal_provider_request_exactly_once(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        StubRuntime(),
        reconcile_on_startup=False,
    )
    try:
        ConversationProvider(store, service)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        project = service.create_project(
            "owner-a",
            "Synthetic conversation",
            "Terminal request reconciliation regression",
            "codex-cli",
        )
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic conversation worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            execution_mode="host",
            workspace_root=str(workspace),
            bootstrap_profile="viventium-conversation-v1",
            bootstrap_bundle={"run_mode": "conversation"},
            start_synchronously=True,
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conversation-a",
            agent_id="agent-a",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(workspace),
            access_mode="workspace",
            history_count=0,
            context_manifest={"messages": 0},
        )
        request, created = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="turn-a",
            message_id="message-a",
            stream_id="stream-a",
            requested_history_count=0,
        )
        assert created is True
        run, run_created = store.create_and_attach_provider_run(
            request_id=request["request_id"],
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            instruction="Complete the synthetic turn.",
        )
        assert run_created is True
        assert store.update_provider_request_if_state(
            request["request_id"],
            ("queued",),
            state="running",
        )
        assert service._finalize_run_if_state(
            run["run_id"],
            "queued",
            "completed",
            output_text="Synthetic completed response.",
        )
        assert store.get_provider_request(request["request_id"])["state"] == "completed"

        abandoned_request, abandoned_created = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="turn-b",
            message_id="message-b",
            stream_id="stream-b",
            requested_history_count=1,
        )
        assert abandoned_created is True
        abandoned_run, abandoned_run_created = store.create_and_attach_provider_run(
            request_id=abandoned_request["request_id"],
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            instruction="Complete the abandoned synthetic turn.",
        )
        assert abandoned_run_created is True
        assert store.update_provider_request_if_state(
            abandoned_request["request_id"],
            ("queued",),
            state="running",
        )
        assert store.finalize_run_if_state(
            abandoned_run["run_id"],
            "queued",
            "completed",
            output_text="Synthetic abandoned response.",
        )
        assert (
            store.get_provider_request(abandoned_request["request_id"])["state"]
            == "running"
        )

        crashed_request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="turn-c",
            message_id="message-c",
            stream_id="stream-c",
            requested_history_count=2,
        )
        crashed_run, _ = store.create_and_attach_provider_run(
            request_id=crashed_request["request_id"],
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            instruction="Complete the crash-gap synthetic turn.",
        )
        assert store.update_provider_request_if_state(
            crashed_request["request_id"],
            ("queued",),
            state="running",
        )
        assert store.finalize_run_if_state(
            crashed_run["run_id"],
            "queued",
            "completed",
            output_text="Synthetic crash-gap response.",
        )
        assert store.update_provider_request_if_state(
            crashed_request["request_id"],
            ("running",),
            state="completed",
        )
        assert store.list_provider_activity(crashed_request["request_id"]) == []

        service._callback_retry_tick()
        service._callback_retry_tick()

        reconciled = store.get_provider_request(request["request_id"])
        recovered = store.get_provider_request(abandoned_request["request_id"])
        crash_recovered = store.get_provider_request(crashed_request["request_id"])
        reconciled_session = store.get_provider_session_by_id(session["session_id"])
        activity_types = [
            item["event_type"]
            for item in store.list_provider_activity(request["request_id"])
        ]
        recovered_activity_types = [
            item["event_type"]
            for item in store.list_provider_activity(abandoned_request["request_id"])
        ]
        crash_recovered_activity_types = [
            item["event_type"]
            for item in store.list_provider_activity(crashed_request["request_id"])
        ]
        assert reconciled["state"] == "completed"
        assert recovered["state"] == "completed"
        assert crash_recovered["state"] == "completed"
        assert reconciled_session["history_count"] == 3
        assert activity_types.count("completed") == 1
        assert recovered_activity_types.count("completed") == 1
        assert crash_recovered_activity_types.count("completed") == 1
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("run_state", "request_state", "involuntary_interruption"),
    [
        ("completed", "completed", False),
        ("failed", "failed", False),
        ("cancelled", "cancelled", False),
        ("interrupted", "cancelled", False),
        ("interrupted", "failed", True),
    ],
)
def test_two_provider_processes_reconcile_each_terminal_state_once(
    tmp_path,
    run_state,
    request_state,
    involuntary_interruption,
):
    database = tmp_path / "runtime.db"
    store_a = Store(str(database))
    service_a = WorkersProjectsService(
        store_a,
        StubRuntime(),
        reconcile_on_startup=False,
    )
    store_b = Store(str(database))
    service_b = WorkersProjectsService(
        store_b,
        StubRuntime(),
        reconcile_on_startup=False,
    )
    try:
        provider_a = ConversationProvider(store_a, service_a)
        provider_b = ConversationProvider(store_b, service_b)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        project = service_a.create_project(
            "owner-a",
            "Synthetic conversation",
            "Cross-process terminal reconciliation regression",
            "codex-cli",
        )
        worker = service_a.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic conversation worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            execution_mode="host",
            workspace_root=str(workspace),
            bootstrap_profile="viventium-conversation-v1",
            bootstrap_bundle={"run_mode": "conversation"},
            start_synchronously=True,
        )
        session = store_a.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conversation-a",
            agent_id="agent-a",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(workspace),
            access_mode="workspace",
            history_count=0,
            context_manifest={"messages": 0},
        )
        immediate_request, _ = store_a.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key=f"immediate-{run_state}",
            message_id=f"immediate-message-{run_state}",
            stream_id=f"immediate-stream-{run_state}",
            requested_history_count=0,
        )
        immediate_run, _ = store_a.create_and_attach_provider_run(
            request_id=immediate_request["request_id"],
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            instruction="Complete the immediate synthetic turn.",
        )
        assert store_a.update_provider_request_if_state(
            immediate_request["request_id"],
            ("queued",),
            state="running",
        )
        assert service_a._finalize_run_if_state(
            immediate_run["run_id"],
            "queued",
            run_state,
            output_text="Synthetic immediate response."
            if run_state == "completed"
            else "",
            error_text="Synthetic immediate terminal outcome."
            if run_state != "completed"
            else "",
            failure_retryable=involuntary_interruption,
            failure_structured=involuntary_interruption,
        )
        assert (
            store_a.get_provider_request(immediate_request["request_id"])["state"]
            == request_state
        )
        assert [
            item["event_type"]
            for item in store_a.list_provider_activity(immediate_request["request_id"])
        ].count(request_state) == 1

        request, _ = store_a.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key=f"turn-{run_state}",
            message_id=f"message-{run_state}",
            stream_id=f"stream-{run_state}",
            requested_history_count=1 if request_state == "completed" else 0,
        )
        run, _ = store_a.create_and_attach_provider_run(
            request_id=request["request_id"],
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            instruction="Complete the concurrent synthetic turn.",
        )
        assert store_a.update_provider_request_if_state(
            request["request_id"],
            ("queued",),
            state="running",
        )
        assert store_a.finalize_run_if_state(
            run["run_id"],
            "queued",
            run_state,
            output_text="Synthetic completed response."
            if run_state == "completed"
            else "",
            error_text="Synthetic terminal outcome."
            if run_state != "completed"
            else "",
            failure_retryable=involuntary_interruption,
            failure_structured=involuntary_interruption,
        )

        transition_barrier = Barrier(2)

        def block_terminal_transition(store):
            original = store.update_provider_request_if_state

            def blocked(request_id, expected_states, **fields):
                if fields.get("state") == request_state:
                    transition_barrier.wait(timeout=2)
                return original(request_id, expected_states, **fields)

            store.update_provider_request_if_state = blocked

        block_terminal_transition(store_a)
        block_terminal_transition(store_b)
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda provider: provider._sync(
                        provider.store.get_provider_request(request["request_id"])
                    ),
                    (provider_a, provider_b),
                )
            )

        terminal = store_a.get_provider_request(request["request_id"])
        activity_types = [
            item["event_type"]
            for item in store_a.list_provider_activity(request["request_id"])
        ]
        durable_session = store_a.get_provider_session_by_id(session["session_id"])
        assert {outcome["state"] for outcome in outcomes} == {request_state}
        assert terminal["state"] == request_state
        assert activity_types.count(request_state) == 1
        assert durable_session["history_count"] == (
            2 if request_state == "completed" else 0
        )
    finally:
        service_b.shutdown()
        service_a.shutdown()
