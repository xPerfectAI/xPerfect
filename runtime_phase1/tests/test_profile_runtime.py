from __future__ import annotations

import hashlib
import json
import io
import logging
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import workers_projects_runtime.openclaw_runtime as openclaw_runtime_module
import workers_projects_runtime.profile_runtime as profile_runtime_module
from workers_projects_runtime.bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_PROPORTIONAL_VERIFICATION_RULE,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
)
from workers_projects_runtime.failure_classification import (
    FailureClassification,
    classify_cli_failure,
    classify_runtime_error,
    is_user_resumable_failure,
)
from workers_projects_runtime.openclaw_runtime import (
    RuntimeDependencyMissingError,
    RuntimeErrorBase,
    WorkerInterruptedError,
    WorkerTerminatedError,
)
from workers_projects_runtime.profile_runtime import (
    _CODEX_PROVIDER_CONTROL_CLIENT,
    BaseCliWorkerRuntime,
    ClaudeCodeRuntime,
    CodexCliRuntime,
    HostClaudeCodeRuntime,
    HostCodexCliRuntime,
    HostOpenClawRuntime,
    OpenClawWorkstationRuntime,
    ProfiledWorkerRuntime,
    _atomic_write_private_text,
    _host_native_web_access,
    _provider_process_exit_error,
    _redact_text,
)
from workers_projects_runtime.run_evidence import build_constraint_ledger, write_constraint_ledger


@pytest.fixture(autouse=True)
def isolate_host_claude_auth_from_unit_tests(monkeypatch):
    """Never let command-construction tests read or rotate a developer's real Claude login."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-oauth-token")


def _patch_host_codex_requirement_probe(monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.runtime_requirements.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n",
            stderr="",
        ),
    )


def _mark_fake_host_supervisor_ready(command: list[str], pid: int) -> None:
    """Make legacy Popen fakes honor the native supervisor readiness contract."""
    if len(command) < 5 or Path(command[1]).name != "native-process-supervisor.py":
        return
    ready_path = Path(command[4])
    ready_path.write_text(f"{pid}\n")
    ready_path.chmod(0o600)


def _write_pass_evidence(runtime, worker_id: str, run_id: str) -> None:
    evidence_dir = runtime._workspace_dir(worker_id) / "glasshive-run" / "runs" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "constraint-ledger.json").write_text(
        json.dumps(
            {
                "schema": "glasshive.run.constraint-ledger.v1",
                "run_id": run_id,
                "worker": {"worker_id": worker_id, "profile": "codex-cli", "execution_mode": "host"},
                "original_request": "Synthetic recovered run test.",
                "constraints": {"date": [], "source": [], "auth": [], "scope": [], "exclusion_or_flag": []},
                "outputs": {
                    "required": [],
                    "forbidden": [],
                    "format_expectations": [],
                    "forbidden_format_expectations": [],
                },
                "seed_entities_or_files": [],
                "do_not_widen_or_soften": False,
            }
        )
        + "\n"
    )
    (evidence_dir / "evidence.json").write_text(json.dumps({"schema": "glasshive.run.evidence.v1", "run_id": run_id, "evidence_result": {"status": "pass"}}) + "\n")


def test_recovered_success_rebuilds_evidence_from_the_exact_retried_attempt(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    run_id = "run_retried"
    evidence_dir = workspace / "glasshive-run" / "runs" / run_id
    evidence_dir.mkdir(parents=True)
    evidence_path = evidence_dir / "evidence.json"
    evidence_path.write_text(json.dumps({
        "schema": "glasshive.run.evidence.v1",
        "run_id": run_id,
        "attempt_id": "attempt-killed",
        "evidence_result": {"status": "fail", "failure_reasons": [{"reason": "old attempt failed"}]},
    }))
    writes = []

    def write_current_attempt(**kwargs):
        writes.append(kwargs["worker"]["_run_attempt_id"])
        evidence_path.write_text(json.dumps({
            "schema": "glasshive.run.evidence.v1",
            "run_id": run_id,
            "attempt_id": "attempt-retried",
            "evidence_result": {"status": "pass"},
        }))
        return evidence_path.relative_to(workspace).as_posix()

    monkeypatch.setattr(profile_runtime_module, "_write_evidence_for_run", write_current_attempt)
    status, _warning = profile_runtime_module._ensure_recovered_success_evidence(
        worker={"worker_id": "wrk_retried", "_run_attempt_id": "attempt-retried"},
        run_id=run_id,
        runtime_name="grok-build",
        model="grok-4.6",
        command=["grok"],
        workspace=workspace,
        stdout_text="FINAL REPORT:\nRecovered work is complete.",
        stderr_text="",
        output_text="Recovered work is complete.",
        exit_code=0,
        active_session={"attempt_id": "attempt-retried"},
        instruction="",
    )

    assert writes == ["attempt-retried"]
    assert status == "warn"  # No optional constraint diagnostic was present.
    assert json.loads(evidence_path.read_text())["attempt_id"] == "attempt-retried"


def test_stateless_codex_turn_does_not_resume_or_replace_native_session(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_stateless_codex",
        "name": "Stateless Codex",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "conversation",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "env": {"GLASSHIVE_PROVIDER_SESSION_MODE": "stateless"},
            }
        ),
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "prior-native-session")

    info = runtime._runtime_info(worker)
    command, _ = runtime._build_command(worker, "Answer this turn.", info)

    assert info.session_key is None
    assert "resume" not in command
    runtime._remember_native_session_key(worker, "new-native-session")
    assert runtime._read_session_key(worker["worker_id"]) == "prior-native-session"


def test_pidless_host_session_is_historical_ambiguity_without_terminal_proof(
    tmp_path,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_pidless_history",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    run_id = "run_pidless_history"
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_active_session(
        worker["worker_id"],
        {
            "session_name": runtime._session_name_for_run_id(run_id),
            "run_id": run_id,
            "attempt_id": "attempt-pidless",
            "process_pid": None,
            "process_start_identity": "",
        },
    )

    assert runtime.host_active_process_status(worker) == {
        "state": "uncertain",
        "run_id": run_id,
        "historical_record_only": True,
    }


def test_atomic_private_state_write_never_exposes_partial_replacement(tmp_path, monkeypatch):
    target = tmp_path / "active-run.json"
    target.write_text(json.dumps({"state": "running", "sequence": 1}))
    real_replace = os.replace
    state_seen_before_publish: list[dict[str, object]] = []

    def inspect_then_replace(source, destination):
        state_seen_before_publish.append(json.loads(target.read_text()))
        real_replace(source, destination)

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.replace", inspect_then_replace)

    _atomic_write_private_text(target, json.dumps({"state": "running", "sequence": 2}))

    assert state_seen_before_publish == [{"state": "running", "sequence": 1}]
    assert json.loads(target.read_text()) == {"state": "running", "sequence": 2}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_provider_liveness_projects_only_exact_native_json_event_types(tmp_path):
    codex = CodexCliRuntime(base_dir=str(tmp_path / "codex"))
    claude = ClaudeCodeRuntime(base_dir=str(tmp_path / "claude"))

    retry = codex._provider_liveness_observation(
        json.dumps({"type": "error", "message": "synthetic localized text"}),
        run_id="run-liveness",
        line_sequence=3,
        model="test-model",
        observed_at="2026-08-27T12:00:00Z",
    )
    progress = codex._provider_liveness_observation(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "completed"},
            }
        ),
        run_id="run-liveness",
        line_sequence=4,
        model="test-model",
        observed_at="2026-08-27T12:00:01Z",
    )
    claude_retry = claude._provider_liveness_observation(
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}),
        run_id="run-liveness",
        line_sequence=5,
        model="test-model",
        observed_at="2026-08-27T12:00:02Z",
    )
    claude_progress = claude._provider_liveness_observation(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Synthetic progress"}]},
            }
        ),
        run_id="run-liveness",
        line_sequence=6,
        model="test-model",
        observed_at="2026-08-27T12:00:03Z",
    )

    assert retry is not None
    assert retry["kind"] == "internal_retry"
    assert retry["failure_class"] == "provider_internal_retry"
    assert retry["event_ref"].startswith("provider_liveness_sha256:")
    assert retry["source_sequence"] == 3
    assert re.fullmatch(r"[0-9a-f]{64}", str(retry["source_digest"]))
    assert progress is not None
    assert progress["kind"] == "meaningful_progress"
    assert progress["event_ref"] != retry["event_ref"]
    assert claude_retry is not None
    assert claude_retry["kind"] == "internal_retry"
    assert claude_retry["failure_class"] == "provider_rate_limited"
    assert claude_progress is not None
    assert claude_progress["kind"] == "meaningful_progress"
    assert claude._provider_liveness_observation(
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}),
        run_id="run-liveness",
        line_sequence=7,
        model="test-model",
        observed_at="2026-08-27T12:00:04Z",
    ) is None
    assert claude._provider_liveness_observation(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking"}]}}),
        run_id="run-liveness",
        line_sequence=8,
        model="test-model",
        observed_at="2026-08-27T12:00:05Z",
    ) is None
    assert codex._provider_liveness_observation(
        json.dumps({"type": "item.completed", "item": {"type": "reasoning"}}),
        run_id="run-liveness",
        line_sequence=9,
        model="test-model",
        observed_at="2026-08-27T12:00:06Z",
    ) is None
    assert codex._provider_liveness_observation(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "failed"},
            }
        ),
        run_id="run-liveness",
        line_sequence=10,
        model="test-model",
        observed_at="2026-08-27T12:00:07Z",
    ) is None
    assert codex._provider_liveness_observation(
        "retrying in prose",
        run_id="run-liveness",
        line_sequence=11,
        model="test-model",
        observed_at="2026-08-27T12:00:08Z",
    ) is None


def test_native_jsonl_tail_publishes_exact_liveness_sequence_and_digest(
    tmp_path, monkeypatch
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "codex-tail"))
    worker_id = "wrk_native_liveness_tail"
    run_id = "run_native_liveness_tail"
    attempt_id = "attempt_native_liveness_tail"
    model = "test-model"
    lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "reasoning", "status": "completed"},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "completed"},
            }
        ),
        json.dumps({"type": "error", "message": "synthetic localized text"}),
        json.dumps({"type": "unknown", "status": "completed"}),
    ]
    stdout_path = tmp_path / "native-events.jsonl"
    stdout_path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(
        runtime,
        "_read_active_session",
        lambda _worker_id: {"run_id": run_id, "model": model},
    )
    observed: list[dict[str, object]] = []
    runtime.set_provider_liveness_observer(observed.append)
    stop = threading.Event()
    stop.set()

    runtime._observe_native_session_events(
        worker_id,
        stdout_path,
        stop,
        run_id=run_id,
        attempt_id=attempt_id,
    )

    assert [item["source_sequence"] for item in observed] == [2, 3]
    assert [item["kind"] for item in observed] == [
        "meaningful_progress",
        "internal_retry",
    ]
    assert [item["source_digest"] for item in observed] == [
        hashlib.sha256(lines[1].encode("utf-8")).hexdigest(),
        hashlib.sha256(lines[2].encode("utf-8")).hexdigest(),
    ]
    assert all(item["worker_id"] == worker_id for item in observed)
    assert all(item["run_id"] == run_id for item in observed)
    assert all(item["attempt_id"] == attempt_id for item in observed)
    assert all(item["model"] == model for item in observed)
    assert runtime._provider_liveness_observation(
        json.dumps({"message": "retrying without a native event type"}),
        run_id="run-liveness",
        line_sequence=12,
        model="test-model",
        observed_at="2026-08-27T12:00:09Z",
    ) is None
    assert runtime._provider_liveness_observation(
        json.dumps({"type": "turn.failed", "error": {"message": "terminal"}}),
        run_id="run-liveness",
        line_sequence=13,
        model="test-model",
        observed_at="2026-08-27T12:00:10Z",
    ) is None


def test_native_jsonl_tail_resumed_attempt_starts_at_existing_eof(
    tmp_path, monkeypatch
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "codex-resumed-tail"))
    worker_id = "wrk_resumed_native_tail"
    run_id = "run_resumed_native_tail"
    attempt_id = "attempt_b"
    model = "test-model"
    prior_attempt_lines = [
        json.dumps({"type": "error", "message": f"attempt A retry {index}"})
        for index in range(3)
    ]
    resumed_attempt_lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "completed"},
            }
        ),
        json.dumps({"type": "error", "message": "attempt B retry"}),
    ]
    stdout_path = tmp_path / "resumed-native-events.jsonl"
    stdout_path.write_text("\n".join(prior_attempt_lines) + "\n")
    resume_boundary = stdout_path.stat().st_size
    monkeypatch.setattr(
        runtime,
        "_read_active_session",
        lambda _worker_id: {"run_id": run_id, "model": model},
    )
    observed: list[dict[str, object]] = []
    runtime.set_provider_liveness_observer(observed.append)
    stop = threading.Event()
    tail = threading.Thread(
        target=runtime._observe_native_session_events,
        args=(worker_id, stdout_path, stop, run_id, attempt_id, resume_boundary),
        daemon=True,
    )

    tail.start()
    time.sleep(0.1)
    assert observed == []

    with stdout_path.open("a") as handle:
        handle.write("\n".join(resumed_attempt_lines) + "\n")
    deadline = time.monotonic() + 2
    while len(observed) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    tail.join(timeout=2)

    assert not tail.is_alive()
    assert [item["attempt_id"] for item in observed] == [attempt_id, attempt_id]
    assert [item["source_sequence"] for item in observed] == [1, 2]
    assert [item["kind"] for item in observed] == [
        "meaningful_progress",
        "internal_retry",
    ]
    assert [item["source_digest"] for item in observed] == [
        hashlib.sha256(line.encode("utf-8")).hexdigest()
        for line in resumed_attempt_lines
    ]


def test_terminal_target_uses_inferred_job_session_when_metadata_missing(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_123456789abc"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)

    session_name = runtime._session_name_for_run_id(run_id)

    runtime.ensure_worker_ready = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda worker_id, runtime_name, worker=None: [session_name]  # type: ignore[method-assign]
    runtime.sandbox.terminal_attach_command = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, session_name="operator": ["attach", session_name]
    )

    target = runtime.terminal_target(worker)
    assert target.command == ["attach", session_name]
    assert target.title == "Main Worker live session"
    assert target.subtitle == "codex-cli active run"


def test_host_terminal_target_preserves_shell_fallback_expression(tmp_path):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_host_terminal",
        "name": "Host Claude",
        "profile": "claude-code",
        "execution_mode": "host",
    }
    runtime.ensure_worker_ready = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]
    runtime._infer_active_session = lambda worker: None  # type: ignore[method-assign]

    target = runtime.terminal_target(worker)

    assert target.command[-1].endswith("exec ${SHELL:-/bin/bash}")
    assert target.title == "Host Claude host terminal"


def test_host_terminal_finds_its_session_on_the_host_without_a_docker_sandbox(tmp_path):
    # Opening a finished host run's terminal once built a Docker sandbox home for the worker
    # and copied this computer's whole Claude folder into it before failing.
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_host_done",
        "name": "Host Claude",
        "profile": "claude-code",
        "execution_mode": "host",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._run_root(worker["worker_id"], "run_123456789abc").mkdir(parents=True, exist_ok=True)
    runtime.ensure_worker_ready = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    def no_sandbox(*_args, **_kwargs):
        raise AssertionError("a host terminal must not create or probe a Docker sandbox")

    runtime.sandbox.ensure_ready = no_sandbox  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = no_sandbox  # type: ignore[method-assign]

    finished = runtime.terminal_target(worker)
    assert finished.command[-1].endswith("exec ${SHELL:-/bin/bash}")
    assert finished.title == "Host Claude host terminal"

    stdout = tmp_path / "live.log"
    runtime._active_session_meta_path(worker["worker_id"]).write_text(
        json.dumps({"session_name": "host-run", "run_id": "run_live", "stdout_path": str(stdout)})
    )
    live = runtime.terminal_target(worker)
    assert str(stdout) in live.command[-1]
    assert live.title == "Host Claude host session"


def test_host_runtime_recovers_and_stops_a_persisted_process_after_api_restart(tmp_path):
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(tmp_path))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_host_restart",
        "name": "Host Codex",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": "conversation-run_restart",
                "run_id": "run_restart",
                "stdout_path": str(tmp_path / "stdout.log"),
                "stderr_path": str(tmp_path / "stderr.log"),
                "exit_path": str(tmp_path / "exit_code"),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "process_group": os.getpgid(process.pid),
                "process_start_identity": (
                    runtime_before_restart._process_start_identity(process.pid)
                ),
                "started_at": datetime.now().astimezone().isoformat(),
            },
        )

        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid

        assert runtime_after_restart._stop_active_process(
            worker["worker_id"], worker=worker, run_id="run_restart"
        )
        process.wait(timeout=3)

        assert process.returncode is not None
        assert runtime_after_restart.reconcile_worker(worker).pid is None
        assert not runtime_after_restart._active_session_meta_path(worker["worker_id"]).exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


def test_host_runtime_rejects_recycled_pid_identity_without_stopping_the_process(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_host_recycled_pid",
        "name": "Host Codex",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    unrelated_process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        runtime._write_active_session(
            worker["worker_id"],
            {
                "session_name": "conversation-run_recycled",
                "run_id": "run_recycled",
                "stdout_path": str(tmp_path / "stdout.log"),
                "stderr_path": str(tmp_path / "stderr.log"),
                "exit_path": str(tmp_path / "exit_code"),
                "model": "gpt-5.6-sol",
                "process_pid": unrelated_process.pid,
                "process_identity_sha256": "0" * 64,
                "process_start_identity": "ps-lstart:synthetic-prior-generation",
                "started_at": datetime.now().astimezone().isoformat(),
            },
        )

        assert runtime.reconcile_worker(worker).pid is None
        runtime._stop_active_process(
            worker["worker_id"],
            worker=worker,
            run_id="run_recycled",
        )

        assert unrelated_process.poll() is None
    finally:
        if unrelated_process.poll() is None:
            unrelated_process.terminate()
            unrelated_process.wait(timeout=3)


def test_host_runtime_stop_failure_preserves_process_and_active_session(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker_id = "wrk_host_stop_failure"

    class StubbornProcess:
        pid = 12345

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(["synthetic-stubborn-process"], timeout)

    process = StubbornProcess()
    runtime._register_process(worker_id, process)  # type: ignore[arg-type]
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "conversation-run_stop_failure",
            "run_id": "run_stop_failure",
            "stdout_path": str(tmp_path / "stdout.log"),
            "stderr_path": str(tmp_path / "stderr.log"),
            "exit_path": str(tmp_path / "exit_code"),
            "model": "gpt-5.6-sol",
            "process_pid": process.pid,
            "process_group": process.pid,
            "process_start_identity": "synthetic-stubborn-generation",
            "started_at": datetime.now().astimezone().isoformat(),
        },
    )
    runtime._host_active_slots()["mission"] = worker_id
    runtime._host_worker_lanes()[worker_id] = "mission"
    monkeypatch.setattr(runtime, "_process_start_identity", lambda _pid: "synthetic-stubborn-generation")
    signals = []
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.killpg", lambda pgid, sig: signals.append((pgid, sig)))
    monkeypatch.setattr(runtime, "_host_process_group_members", lambda _pgid: {
        process.pid: "synthetic-stubborn-generation"
    })
    runtime._wait_for_host_process_group_exit = lambda _pgid, _timeout: False  # type: ignore[attr-defined]

    assert runtime._stop_active_process(
        worker_id,
        worker={"worker_id": worker_id, "execution_mode": "host"},
        run_id="run_stop_failure",
    ) is False
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]

    assert runtime._read_active_session(worker_id) is not None
    assert runtime._active_processes[worker_id] is process
    assert runtime._host_active_slots()["mission"] == worker_id
    assert runtime._host_worker_lanes()[worker_id] == "mission"


def test_host_runtime_stale_cleanup_does_not_clear_replacement_process(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker_id = "wrk_host_replacement_process"

    class SyntheticProcess:
        def poll(self):
            return None

    stale_process = cast(subprocess.Popen[str], SyntheticProcess())
    replacement_process = cast(subprocess.Popen[str], SyntheticProcess())
    runtime._register_process(worker_id, stale_process)
    runtime._register_process(worker_id, replacement_process)

    assert runtime._clear_process(
        worker_id, expected_process=stale_process
    ) is False
    assert runtime._active_processes[worker_id] is replacement_process
    assert runtime._clear_process(
        worker_id, expected_process=replacement_process
    ) is True
    assert worker_id not in runtime._active_processes


def test_host_runtime_process_group_permission_probe_fails_closed(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))

    def deny_probe(_pgid, _signal):
        raise PermissionError

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.killpg", deny_probe)
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode=1, stdout="", stderr="ps failed"),
    )

    assert runtime._host_process_group_alive(12345) is True


@pytest.mark.parametrize(
    ("runtime_class", "profile", "model", "stdout_payload", "expected_output", "expected_session"),
    [
        (
            HostCodexCliRuntime,
            "codex-cli",
            "gpt-5.6-sol",
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread-recovered"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "Recovered Codex conversation.",
                            },
                        }
                    ),
                ]
            ),
            "Recovered Codex conversation.",
            "thread-recovered",
        ),
        (
            HostClaudeCodeRuntime,
            "claude-code",
            "opus",
            json.dumps(
                {
                    "type": "result",
                    "result": "Recovered Claude conversation.",
                    "session_id": "session-recovered",
                }
            ),
            "Recovered Claude conversation.",
            "session-recovered",
        ),
    ],
)
def test_host_native_child_persists_completion_for_restart_recovery_without_duplicate_authoring(
    tmp_path,
    runtime_class,
    profile,
    model,
    stdout_payload,
    expected_output,
    expected_session,
):
    private_state = tmp_path / "private-state"
    runtime_before_restart = runtime_class(base_dir=str(private_state))
    runtime_after_restart = runtime_class(base_dir=str(private_state))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_recover_{profile}",
        "name": "Synthetic conversation worker",
        "profile": profile,
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": model,
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    run_id = f"run_recover_{profile}"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    run_root.chmod(0o700)
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    exit_path = run_root / "exit_code"
    launch_count_path = run_root / "launch-count.log"
    stdin_hash_path = run_root / "stdin.sha256"
    instruction_path = run_root / "instruction.stdin"
    private_instruction = "Complete durable restart prompt.\n" + ("synthetic-context " * 2048)
    instruction_path.write_text(private_instruction)
    instruction_path.chmod(0o600)
    expected_stdin_hash = hashlib.sha256(private_instruction.encode()).hexdigest()
    child_code = (
        "import hashlib,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).open('a').write('launch\\n'); "
        "stdin_text=sys.stdin.read(); "
        "stdin_hash=hashlib.sha256(stdin_text.encode()).hexdigest(); "
        "pathlib.Path(sys.argv[2]).write_text(stdin_hash); "
        "sys.exit(61) if stdin_hash != sys.argv[3] else None; "
        "time.sleep(0.2); print(sys.argv[4], flush=True)"
    )
    command = [
        sys.executable,
        "-c",
        child_code,
        str(launch_count_path),
        str(stdin_hash_path),
        expected_stdin_hash,
        stdout_payload,
    ]
    process_command = runtime_before_restart._durable_host_process_command(
        command,
        run_root=run_root,
        exit_path=exit_path,
        stdin_path=instruction_path,
    )

    process: subprocess.Popen[str] | None = None
    try:
        with stdout_path.open("w") as stdout_handle, stderr_path.open("w") as stderr_handle:
            process = subprocess.Popen(
                process_command,
                cwd=str(life),
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "exit_path": str(exit_path),
                "model": model,
                "argv_for_evidence_json": json.dumps(command),
                "started_at": datetime.now().astimezone().isoformat(),
                "process_pid": process.pid,
                "run_mode": "conversation",
            },
        )
        time.sleep(0.1)
        assert not launch_count_path.exists()
        assert not exit_path.exists()
        persisted_session = runtime_after_restart._read_active_session(worker["worker_id"])
        assert persisted_session is not None
        assert persisted_session["process_pid"] == process.pid
        assert persisted_session["process_identity_sha256"]
        assert persisted_session["run_mode"] == "conversation"
        assert stat.S_IMODE(
            runtime_after_restart._active_session_meta_path(worker["worker_id"]).stat().st_mode
        ) == 0o600
        assert list(
            runtime_after_restart._active_session_meta_path(worker["worker_id"]).parent.glob(
                "active-session.json.tmp-*"
            )
        ) == []

        # A fresh API instance observes the durable metadata and releases the
        # pre-authoring handshake exactly once.
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid

        deadline = time.monotonic() + 5
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert exit_path.read_text().strip() == "0"

        recovered = runtime_after_restart.collect_completed_run(worker, run_id=run_id)
        recovered_again = runtime_after_restart.collect_completed_run(worker, run_id=run_id)

        assert recovered is not None
        assert recovered["state"] == "completed"
        assert recovered["output_text"] == expected_output
        assert recovered_again is not None
        assert recovered_again["state"] == "completed"
        assert recovered_again["output_text"] == expected_output
        assert json.loads(
            runtime_after_restart._session_meta_path(worker["worker_id"]).read_text()
        )["session_key"] == expected_session
        assert launch_count_path.read_text().splitlines() == ["launch"]
        assert stdin_hash_path.read_text() == expected_stdin_hash
        assert stat.S_IMODE(exit_path.stat().st_mode) == 0o600
        assert stat.S_IMODE((run_root / "native-process-supervisor.py").stat().st_mode) == 0o700
        assert list(run_root.glob("exit_code.tmp.*")) == []
        assert not (life / "glasshive-run").exists()
    finally:
        if process is not None:
            process.wait(timeout=5)


def test_host_native_restart_cancellation_stops_process_group_and_persists_terminal_marker(
    tmp_path,
):
    private_state = tmp_path / "private-state"
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(private_state))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(private_state))
    worker = {
        "worker_id": "wrk_cancel_after_restart",
        "name": "Synthetic cancellation worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
    }
    run_id = "run_cancel_after_restart"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    exit_path = run_root / "exit_code"
    child_pid_path = run_root / "child.pid"
    process_command = runtime_before_restart._durable_host_process_command(
        [
            sys.executable,
            "-c",
            (
                "import os,pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(30)"
            ),
            str(child_pid_path),
        ],
        run_root=run_root,
        exit_path=exit_path,
    )
    process = subprocess.Popen(
        process_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(run_root / "stdout.log"),
                "stderr_path": str(run_root / "stderr.log"),
                "exit_path": str(exit_path),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "run_mode": "conversation",
            },
        )
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())

        runtime_after_restart._stop_active_process(
            worker["worker_id"],
            worker=worker,
            run_id=run_id,
        )
        process.wait(timeout=5)

        assert exit_path.read_text().strip() == "143"
        assert stat.S_IMODE(exit_path.stat().st_mode) == 0o600
        assert list(run_root.glob("exit_code.tmp.*")) == []
        assert not runtime_after_restart._active_session_meta_path(worker["worker_id"]).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_host_native_nonzero_exit_is_durably_recovered_with_precise_failure(tmp_path):
    private_state = tmp_path / "private-state"
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(private_state))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(private_state))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_nonzero_after_restart",
        "name": "Synthetic nonzero worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    run_id = "run_nonzero_after_restart"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    exit_path = run_root / "exit_code"
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('synthetic durable child failure\\n'); sys.exit(23)",
    ]
    process_command = runtime_before_restart._durable_host_process_command(
        command,
        run_root=run_root,
        exit_path=exit_path,
    )
    with stdout_path.open("w") as stdout_handle, stderr_path.open("w") as stderr_handle:
        process = subprocess.Popen(
            process_command,
            cwd=str(life),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    try:
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "exit_path": str(exit_path),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "run_mode": "conversation",
            },
        )
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid

        deadline = time.monotonic() + 5
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        process.wait(timeout=5)
        recovered = runtime_after_restart.collect_completed_run(worker, run_id=run_id)

        assert exit_path.read_text().strip() == "23"
        assert process.returncode == 23
        assert recovered is not None
        assert recovered["state"] == "failed"
        assert "exited with code 23" in recovered["error_text"]
        assert "synthetic durable child failure" in recovered["error_text"]
        assert not (life / "glasshive-run").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_host_native_configured_timeout_survives_api_restart_and_stops_child_group(tmp_path):
    private_state = tmp_path / "private-state"
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(private_state))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(private_state))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_timeout_after_restart",
        "name": "Synthetic timeout worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    run_id = "run_timeout_after_restart"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    exit_path = run_root / "exit_code"
    child_pid_path = run_root / "child.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "print('child started', flush=True); time.sleep(30)"
        ),
        str(child_pid_path),
    ]
    process_command = runtime_before_restart._durable_host_process_command(
        command,
        run_root=run_root,
        exit_path=exit_path,
        timeout_sec=1.0,
    )
    with stdout_path.open("w") as stdout_handle, stderr_path.open("w") as stderr_handle:
        process = subprocess.Popen(
            process_command,
            cwd=str(life),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    try:
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "exit_path": str(exit_path),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "timeout_seconds": 1.0,
                "run_mode": "conversation",
            },
        )
        started = time.monotonic()
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid
        child_start_deadline = time.monotonic() + 0.5
        while not child_pid_path.exists() and time.monotonic() < child_start_deadline:
            time.sleep(0.01)
        assert child_pid_path.exists()

        deadline = time.monotonic() + 5
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        process.wait(timeout=5)
        elapsed = time.monotonic() - started
        recovered = runtime_after_restart.collect_completed_run(worker, run_id=run_id)

        assert exit_path.read_text().strip() == "124"
        assert process.returncode == 124
        assert elapsed < 3
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert recovered is not None
        assert recovered["state"] == "failed"
        assert "exited with code 124" in recovered["error_text"]
        assert "timed out after 1s" in recovered["error_text"]
        assert not (life / "glasshive-run").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_collect_completed_run_recovers_from_latest_run_artifacts(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_abcdef123456"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_123"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nHELLO WORLD"}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "HELLO WORLD"
    assert json.loads(runtime._session_meta_path(worker["worker_id"]).read_text())["session_key"] == "thread_123"


@pytest.mark.parametrize("summarized_evidence", [False, True])
def test_collect_completed_run_preserves_success_when_internal_diagnostic_was_unavailable(tmp_path, summarized_evidence):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_missing_ledger",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_missingledger"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)
    (runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "constraint-ledger.json").unlink()
    if summarized_evidence:
        from workers_projects_runtime.run_evidence import summarize_run_evidence_result

        evidence_path = runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "evidence.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["constraint_compliance"] = {"status": "not_available", "issues": []}
        evidence["evidence_result"] = summarize_run_evidence_result(evidence)
        assert evidence["evidence_result"]["status"] == "warn"
        assert {"reason": "internal constraint diagnostic was unavailable"} in evidence["evidence_result"]["warning_reasons"]
        evidence_path.write_text(json.dumps(evidence))

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"].startswith("Done")
    assert "internal constraint diagnostic was unavailable" in recovered["output_text"]


def test_collect_completed_run_preserves_evidence_warning(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_warn_recovery",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_warnrecovery"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)
    evidence_path = runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "evidence.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["evidence_result"] = {
        "status": "warn",
        "warning_reasons": [{"reason": "content hygiene warning", "failure_count": 1}],
    }
    evidence_path.write_text(
        json.dumps(evidence) + "\n"
    )

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"].startswith("Done")
    assert "GlassHive evidence check warning: content hygiene warning" in recovered["output_text"]


def test_collect_completed_run_rejects_hollow_constraint_ledger(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_hollow_ledger",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_hollowledger"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)
    ledger_path = runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "constraint-ledger.json"
    ledger_path.write_text("{}\n")

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)
    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "glasshive_evidence_check_failed"
    assert "canonical schema" in recovered["error_text"]


def test_collect_completed_run_classifies_and_redacts_provider_rate_limit(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_rate_limit",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_rate12345"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_rate"}),
                json.dumps(
                    {
                        "type": "response.failed",
                        "error": {
                            "message": "Too Many Requests",
                            "status_code": 429,
                            "headers": {"retry-after": "120"},
                        },
                    }
                ),
                json.dumps({"type": "turn.failed", "error": {"message": "response.failed event received"}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("api_key=PUBLIC_FAKE_API_KEY_VALUE token=PUBLIC_FAKE_TOKEN_VALUE\n")
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_rate_limited"
    assert recovered["failure_retryable"] == 1
    assert "workspace_continue" in recovered["failure_recommended_recovery"]
    assert "Too Many Requests" in recovered["failure_diagnostic_summary"]
    assert recovered["provider_retry_after_s"] == 120
    assert "PUBLIC_FAKE_API_KEY_VALUE" not in recovered["error_text"]
    assert "PUBLIC_FAKE_TOKEN_VALUE" not in recovered["error_text"]


def test_cli_failure_classifies_codex_usage_quota_as_structured_provider_quota():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "error",
                    "code": "usage_limit_reached",
                    "message": (
                        "You've hit your usage limit. To get more access now, "
                        "review your provider plan."
                    ),
                }
            ),
            json.dumps({"type": "turn.failed"}),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    # The Codex usage-limit exhaustion is a structured provider quota signal (from the CLI's own
    # terminal error event), so it is failover-eligible and drives the configured fallback worker.
    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.retryable is True
    assert failure.structured is True
    assert failure.provider_event_source == "provider_native"
    assert "configured fallback" in failure.recommended_recovery


def test_cli_failure_classifies_codex_usage_limit_turn_failed_as_structured_quota():
    # The real Codex CLI emits a trusted `turn.failed` control event carrying the usage-limit
    # exhaustion in its own error message. That must be structured provider evidence so the
    # configured fallback worker can take over automatically (failover keys on structured
    # provider_quota_exhausted / provider_rate_limited evidence, never on task text).
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "error",
                    "code": "usage_limit_reached",
                    "message": "You've hit your usage limit. Visit the usage page or try again later.",
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": "You've hit your usage limit. Visit the usage page or try again later.",
                    },
                }
            ),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1
    )

    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.retryable is True
    assert failure.structured is True
    assert failure.provider_event_source == "provider_native"
    assert "configured fallback" in failure.recommended_recovery


def test_cli_failure_classifies_codex_usage_limit_top_level_error_as_structured_quota():
    # The Codex CLI can also report the usage-limit exhaustion as a top-level stream error event and
    # then exit without a turn.failed event. That top-level `error` is the CLI's own provider report
    # (task text is carried under assistant/item events), so it is structured quota evidence too.
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "error",
                    "code": "usage_limit_reached",
                    "message": "You've hit your usage limit. Visit the usage page or try again later.",
                }
            ),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1
    )

    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.structured is True
    assert failure.provider_event_source == "provider_native"


def test_runtime_error_classifies_codex_usage_limit_raw_exit_as_structured_quota():
    # The live host worker-exit path raises a raw error whose message embeds the provider's terminal
    # JSONL, then classifies it with classify_runtime_error. That path must also recognize the
    # usage-limit exhaustion (from the provider's own trusted terminal events) as structured quota so
    # the configured fallback worker takes over automatically.
    from workers_projects_runtime.failure_classification import classify_runtime_error

    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "error",
                    "code": "usage_limit_reached",
                    "message": "You've hit your usage limit. Visit the usage page or try again later.",
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "You've hit your usage limit. Visit the usage page or try again later."},
                }
            ),
        ]
    )
    exc = RuntimeError(f"codex-cli exited with code 1: {stdout}")
    failure = classify_runtime_error(exc, runtime_name="codex-cli")

    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.structured is True
    assert failure.provider_event_source == "provider_native"


def test_runtime_error_keeps_generic_worker_exit_without_provider_capacity_event():
    from workers_projects_runtime.failure_classification import classify_runtime_error

    exc = RuntimeError("codex-cli exited with code 1: internal parser crash, no provider event")
    failure = classify_runtime_error(exc, runtime_name="codex-cli")
    assert failure.failure_class == "runtime_error"


def test_cli_failure_does_not_treat_assistant_usage_limit_prose_as_structured_quota():
    # Assistant/task output that merely mentions a usage limit must never be trusted as a provider
    # capacity failure; only the provider's own trusted terminal event counts as structured.
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Your plan usage limit resets monthly."}]},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1
    )

    assert failure.structured is False


def test_classify_cli_failure_maps_structured_provider_overload():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 529,
                "terminal_reason": "api_error",
                "result": "API Error: 529 Overloaded. This is a server-side issue, usually temporary.",
            }
        )
        + "\n",
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_response_failed"
    assert failure.retryable is True
    assert "workspace_continue" in failure.recommended_recovery
    assert "api_error_status: 529" in failure.diagnostic_summary
    assert "Overloaded" not in failure.diagnostic_summary


def test_classify_cli_failure_does_not_use_native_json_message_prose_as_quota_authority():
    failure = classify_cli_failure(
        stdout='{"type":"error","message":"You\'ve hit your usage limit. Try again after the reset."}\n'
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}',
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_quota_exhausted"
    assert failure.structured is False


def _docker_codex_preauthoring_failure(thread_id: str) -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "error", "message": "untrusted localized prose"}),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "untrusted localized prose"},
                }
            ),
        ]
    )


def _docker_codex_typed_quota_control(
    thread_id: str,
    reset_at: int,
    *,
    model_provider: str = "glasshive_openai_compatible",
    authored: bool = False,
) -> str:
    snapshot = {
        "rateLimitReachedType": "workspace_member_usage_limit_reached",
        "primary": {"usedPercent": 100, "resetsAt": reset_at},
        "secondary": None,
    }
    return json.dumps(
        {
            "thread_result": {
                "thread": {
                    "id": thread_id,
                    "modelProvider": model_provider,
                    "turns": [
                        {
                            "id": "turn_docker_typed_quota",
                            "status": "failed",
                            "items": [{"id": "item_user", "type": "userMessage"},
                                      *([{"id": "item_agent", "type": "agentMessage"}] if authored else [])],
                            "error": {
                                "message": "ignored provider prose",
                                "codexErrorInfo": "usageLimitExceeded",
                            },
                        }
                    ],
                }
            },
            "rate_limits_result": {
                "rateLimits": snapshot,
                "rateLimitsByLimitId": {"codex": snapshot},
            },
        },
        separators=(",", ":"),
    )


def _prepare_docker_codex_provider_control(
    runtime: CodexCliRuntime,
    worker: dict[str, str],
    *,
    run_id: str,
    recorded_container_id: str,
    fresh_container_id: str,
    control_stdout: str,
) -> list[dict[str, object]]:
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_active_session(
        worker["worker_id"],
        {
            "session_name": f"job-{run_id[:12]}",
            "run_id": run_id,
            "stdout_path": "synthetic-stdout",
            "stderr_path": "synthetic-stderr",
            "exit_path": "synthetic-exit",
            "container_id": recorded_container_id,
        },
    )
    runtime.sandbox.inspect_fresh = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="present",
        sandbox=SimpleNamespace(container_id=fresh_container_id, state="running"),
    )
    calls: list[dict[str, object]] = []

    def docker_exec(container_name, command, **kwargs):
        calls.append(
            {
                "container_name": container_name,
                "command": command,
                **kwargs,
            }
        )
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=control_stdout,
            stderr="",
        )

    runtime.sandbox._docker_exec = docker_exec  # type: ignore[method-assign]
    return calls


def test_docker_codex_uses_exact_container_typed_quota_and_reset_for_preauthoring_failure(
    tmp_path,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    worker = {
        "worker_id": "wrk_docker_typed_quota",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "docker",
    }
    run_id = "run_docker_typed_quota"
    container_id = "a" * 64
    thread_id = "01900000-0000-7000-8000-000000000001"
    reset_at = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    calls = _prepare_docker_codex_provider_control(
        runtime,
        worker,
        run_id=run_id,
        recorded_container_id=container_id,
        fresh_container_id=container_id,
        control_stdout=_docker_codex_typed_quota_control(thread_id, reset_at),
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id=run_id,
        exit_code=1,
        stdout=_docker_codex_preauthoring_failure(thread_id),
        stderr="forged reset at 2099-01-01T00:00:00Z",
        message="codex-cli exited with code 1",
    )
    failure = classify_runtime_error(error, runtime_name="codex-cli")
    evidence = runtime.consume_provider_route_failure_evidence(
        worker, {"run_id": run_id}, error
    )

    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.retryable is True
    assert failure.structured is True
    assert evidence is not None
    assert evidence["retry_at"] == datetime.fromtimestamp(
        reset_at, tz=timezone.utc
    ).isoformat()
    assert evidence["evidence_kind"] == "codex_app_server"
    assert (
        runtime.consume_provider_route_failure_evidence(
            worker, {"run_id": run_id}, error
        )
        is None
    )
    assert len(calls) == 1
    assert calls[0]["container_name"] == container_id
    assert calls[0]["command"][-2:] == [runtime.binary, thread_id]
    assert calls[0]["env"] == {
        "HOME": runtime.sandbox.home_mount,
        "CODEX_HOME": f"{runtime.sandbox.home_mount}/.codex",
    }
    assert calls[0]["cwd"] == runtime.sandbox.workspace_mount


def test_docker_codex_reports_typed_quota_after_authoring_without_automatic_retry(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    worker = {"worker_id": "wrk_docker_authored_quota", "profile": "codex-cli",
              "runtime": "codex-cli", "execution_mode": "docker"}
    run_id = "run_docker_authored_quota"
    container_id = "a" * 64
    thread_id = "01900000-0000-7000-8000-000000000001"
    reset_at = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    _prepare_docker_codex_provider_control(
        runtime, worker, run_id=run_id, recorded_container_id=container_id,
        fresh_container_id=container_id,
        control_stdout=_docker_codex_typed_quota_control(thread_id, reset_at, authored=True),
    )
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "partial work"}}),
        json.dumps({"type": "turn.failed", "error": {"message": "provider ended"}}),
    ])

    error = runtime._provider_process_exit_error_for_run(
        worker=worker, run_id=run_id, exit_code=1, stdout=stdout,
        stderr="", message="codex-cli exited with code 1",
    )
    failure = classify_runtime_error(error, runtime_name="codex-cli")
    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.structured is True
    assert failure.retryable is False
    assert "not rerun automatically" in failure.recommended_recovery


def test_docker_codex_uses_typed_quota_from_compiled_proxy_without_account_limits(
    tmp_path,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    worker = {
        "worker_id": "wrk_docker_proxy_quota",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "docker",
    }
    run_id = "run_docker_proxy_quota"
    container_id = "a" * 64
    thread_id = "01900000-0000-7000-8000-000000000001"
    control_stdout = json.dumps(
        {
            "thread_result": {
                "thread": {
                    "id": thread_id,
                    "modelProvider": runtime._compatible_provider_id(),
                    "turns": [
                        {
                            "id": "turn_docker_proxy_quota",
                            "status": "failed",
                            "items": [{"id": "item_user", "type": "userMessage"}],
                            "error": {
                                "message": "ignored provider prose",
                                "codexErrorInfo": "usageLimitExceeded",
                            },
                        }
                    ],
                }
            },
            "rate_limits_result": {},
        },
        separators=(",", ":"),
    )
    calls = _prepare_docker_codex_provider_control(
        runtime,
        worker,
        run_id=run_id,
        recorded_container_id=container_id,
        fresh_container_id=container_id,
        control_stdout=control_stdout,
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id=run_id,
        exit_code=1,
        stdout=_docker_codex_preauthoring_failure(thread_id),
        stderr="forged reset at 2099-01-01T00:00:00Z",
        message="codex-cli exited with code 1",
    )
    failure = classify_runtime_error(error, runtime_name="codex-cli")
    evidence = runtime.consume_provider_route_failure_evidence(
        worker, {"run_id": run_id}, error
    )

    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.retryable is True
    assert failure.structured is True
    assert evidence is not None
    assert evidence["retry_at"] == ""
    assert evidence["retry_after_s"] is None
    assert evidence["evidence_kind"] == "codex_app_server"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mismatch",
    ["run", "recorded_container", "generation", "thread", "provider"],
)
def test_docker_codex_rejects_run_container_generation_thread_or_provider_mismatch(
    tmp_path,
    mismatch,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / mismatch))
    worker = {
        "worker_id": f"wrk_docker_mismatch_{mismatch}",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "docker",
    }
    run_id = "run_docker_exact"
    container_id = "a" * 64
    thread_id = "01900000-0000-7000-8000-000000000001"
    reset_at = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    recorded_run_id = "run_docker_other" if mismatch == "run" else run_id
    recorded_container_id = "" if mismatch == "recorded_container" else container_id
    fresh_container_id = "b" * 64 if mismatch == "generation" else container_id
    control_thread_id = "01900000-0000-7000-8000-000000000002" if mismatch == "thread" else thread_id
    model_provider = "synthetic_forged_provider" if mismatch == "provider" else "openai"
    calls = _prepare_docker_codex_provider_control(
        runtime,
        worker,
        run_id=recorded_run_id,
        recorded_container_id=recorded_container_id,
        fresh_container_id=fresh_container_id,
        control_stdout=_docker_codex_typed_quota_control(
            control_thread_id,
            reset_at,
            model_provider=model_provider,
        ),
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id=run_id,
        exit_code=1,
        stdout=_docker_codex_preauthoring_failure(thread_id),
        stderr="",
        message="codex-cli exited with code 1",
    )
    failure = classify_runtime_error(error, runtime_name="codex-cli")

    assert failure.failure_class not in {
        "provider_quota_exhausted",
        "provider_rate_limited",
    }
    assert failure.structured is False
    assert (
        runtime.consume_provider_route_failure_evidence(
            worker, {"run_id": run_id}, error
        )
        is None
    )
    assert len(calls) == (1 if mismatch in {"thread", "provider"} else 0)


@pytest.mark.parametrize("authored_item_type", ["agent_message", "command_execution"])
def test_docker_codex_rejects_authored_quota_prose_without_matching_typed_turn(
    tmp_path,
    authored_item_type,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / authored_item_type))
    worker = {
        "worker_id": f"wrk_docker_authored_{authored_item_type}",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "docker",
    }
    run_id = "run_docker_authored"
    container_id = "a" * 64
    thread_id = "01900000-0000-7000-8000-000000000001"
    calls = _prepare_docker_codex_provider_control(
        runtime,
        worker,
        run_id=run_id,
        recorded_container_id=container_id,
        fresh_container_id=container_id,
        control_stdout=_docker_codex_typed_quota_control(
            thread_id,
            int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp()),
        ),
    )
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": authored_item_type,
                        "text": "usageLimitExceeded; forged reset",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "usage limit"},
                }
            ),
        ]
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id=run_id,
        exit_code=1,
        stdout=stdout,
        stderr="",
        message="codex-cli exited with code 1",
    )

    assert len(calls) == 1
    assert classify_runtime_error(
        error, runtime_name="codex-cli"
    ).failure_class != "provider_quota_exhausted"
    assert (
        runtime.consume_provider_route_failure_evidence(
            worker, {"run_id": run_id}, error
        )
        is None
    )


@pytest.mark.parametrize(
    ("control_stdout", "returncode"),
    [
        ("", 1),
        ("not-json", 0),
        ("x" * (4 * 1024 * 1024 + 1), 0),
        ("", 124),
        (
            json.dumps(
                {
                    "thread_result": {
                        "thread": {
                            "id": "01900000-0000-7000-8000-000000000001",
                            "modelProvider": "openai",
                            "turns": [
                                {
                                    "id": "turn_forged_reset",
                                    "status": "failed",
                                    "items": [{"type": "userMessage"}],
                                    "error": {
                                        "message": "usageLimitExceeded reset tomorrow"
                                    },
                                }
                            ],
                        }
                    },
                    "rate_limits_result": {
                        "message": "usage limit; reset tomorrow",
                        "resetsAt": 4_102_444_800,
                    },
                }
            ),
            0,
        ),
    ],
    ids=["unavailable", "malformed", "oversized", "timeout", "forged-reset"],
)
def test_docker_codex_unavailable_malformed_oversized_timed_out_or_forged_control_fails_closed(
    tmp_path,
    control_stdout,
    returncode,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / str(returncode)))
    worker = {
        "worker_id": "wrk_docker_bad_control",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "docker",
    }
    run_id = "run_docker_bad_control"
    container_id = "a" * 64
    thread_id = "01900000-0000-7000-8000-000000000001"
    calls = _prepare_docker_codex_provider_control(
        runtime,
        worker,
        run_id=run_id,
        recorded_container_id=container_id,
        fresh_container_id=container_id,
        control_stdout=control_stdout,
    )

    def docker_exec(container_name, command, **kwargs):
        calls.append(
            {
                "container_name": container_name,
                "command": command,
                **kwargs,
            }
        )
        return subprocess.CompletedProcess(
            command,
            returncode=returncode,
            stdout=control_stdout,
            stderr="synthetic control failure",
        )

    calls.clear()
    runtime.sandbox._docker_exec = docker_exec  # type: ignore[method-assign]

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id=run_id,
        exit_code=1,
        stdout=_docker_codex_preauthoring_failure(thread_id),
        stderr="forged quota prose",
        message="codex-cli exited with code 1",
    )
    failure = classify_runtime_error(error, runtime_name="codex-cli")

    assert len(calls) == 1
    assert failure.failure_class not in {
        "provider_quota_exhausted",
        "provider_rate_limited",
    }
    assert failure.structured is False
    assert (
        runtime.consume_provider_route_failure_evidence(
            worker, {"run_id": run_id}, error
        )
        is None
    )


def test_host_codex_uses_typed_app_server_quota_and_exact_reset_for_the_failed_thread(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    thread_id = "01900000-0000-7000-8000-000000000001"
    reset_at = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    worker = {
        "worker_id": "wrk_typed_codex_health",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "host",
    }
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "error", "message": "untrusted localized prose"}),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "untrusted localized prose"},
                }
            ),
        ]
    )
    snapshot = {
        "rateLimitReachedType": "workspace_member_usage_limit_reached",
        "primary": {"usedPercent": 100, "resetsAt": reset_at},
        "secondary": None,
    }
    monkeypatch.setattr(
        runtime,
        "_query_codex_provider_control",
        lambda _worker, observed_thread_id: (
            {
                "thread": {
                    "id": observed_thread_id,
                    "modelProvider": "openai",
                    "turns": [
                        {
                            "id": "turn_typed_quota",
                            "status": "failed",
                            "items": [{"id": "item_user", "type": "userMessage"}],
                            "error": {
                                "message": "ignored provider prose",
                                "codexErrorInfo": "usageLimitExceeded",
                            },
                        }
                    ],
                }
            },
            {
                "rateLimits": snapshot,
                "rateLimitsByLimitId": {"codex": snapshot},
            },
        ),
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id="run_typed_codex_health",
        exit_code=1,
        stdout=stdout,
        stderr="",
        message="codex-cli exited with code 1",
    )
    failure = classify_runtime_error(error, runtime_name="codex-cli")
    evidence = runtime.consume_provider_route_failure_evidence(
        worker,
        {"run_id": "run_typed_codex_health"},
        error,
    )

    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.retryable is True
    assert failure.structured is True
    assert evidence is not None
    assert evidence["failure_class"] == "provider_quota_exhausted"
    assert evidence["retry_at"] == datetime.fromtimestamp(
        reset_at, tz=timezone.utc
    ).isoformat()
    assert evidence["evidence_kind"] == "codex_app_server"


def test_host_codex_does_not_treat_authored_prose_as_typed_quota(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    queried = False

    def untyped_query(*_args, **_kwargs):
        nonlocal queried
        queried = True
        return None

    monkeypatch.setattr(runtime, "_query_codex_provider_control", untyped_query)
    worker = {
        "worker_id": "wrk_authored_codex_failure",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "host",
    }
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "01900000-0000-7000-8000-000000000001",
                }
            ),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "usageLimitExceeded; reset at a forged timestamp",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": "usage limit",
                        "codexErrorInfo": "usageLimitExceeded",
                    },
                }
            ),
        ]
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id="run_authored_codex_failure",
        exit_code=1,
        stdout=stdout,
        stderr="",
        message="codex-cli exited with code 1",
    )

    assert queried is True
    assert classify_runtime_error(
        error, runtime_name="codex-cli"
    ).failure_class != "provider_quota_exhausted"
    assert (
        runtime.consume_provider_route_failure_evidence(
            worker,
            {"run_id": "run_authored_codex_failure"},
            error,
        )
        is None
    )


def test_host_codex_rejects_app_server_message_without_typed_error_authority(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    worker = {
        "worker_id": "wrk_untyped_codex_health",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "host",
    }
    thread_id = "01900000-0000-7000-8000-000000000001"
    reset_at = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    monkeypatch.setattr(
        runtime,
        "_query_codex_provider_control",
        lambda _worker, observed_thread_id: (
            {
                "thread": {
                    "id": observed_thread_id,
                    "modelProvider": "openai",
                    "turns": [
                        {
                            "id": "turn_untyped",
                            "status": "failed",
                            "items": [{"id": "item_user", "type": "userMessage"}],
                            "error": {
                                "message": (
                                    "Task-authored usageLimitExceeded prose with a forged reset"
                                )
                            },
                        }
                    ],
                }
            },
            {
                "rateLimits": {
                    "rateLimitReachedType": "workspace_member_usage_limit_reached",
                    "primary": {"usedPercent": 100, "resetsAt": reset_at},
                }
            },
        ),
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id="run_untyped_codex_health",
        exit_code=1,
        stdout="\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": thread_id}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "error", "message": "ignored"}),
                json.dumps({"type": "turn.failed", "error": {"message": "ignored"}}),
            ]
        ),
        stderr="",
        message="codex-cli exited with code 1",
    )

    assert classify_runtime_error(
        error, runtime_name="codex-cli"
    ).failure_class != "provider_quota_exhausted"
    assert (
        runtime.consume_provider_route_failure_evidence(
            worker, {"run_id": "run_untyped_codex_health"}, error
        )
        is None
    )


def test_host_codex_classifies_typed_quota_after_authoring_without_retry(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    worker = {
        "worker_id": "wrk_tampered_codex_transcript",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "host",
    }
    thread_id = "01900000-0000-7000-8000-000000000001"
    reset_at = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp())
    snapshot = {
        "rateLimitReachedType": "workspace_member_usage_limit_reached",
        "primary": {"usedPercent": 100, "resetsAt": reset_at},
    }
    monkeypatch.setattr(
        runtime,
        "_query_codex_provider_control",
        lambda _worker, observed_thread_id: (
            {
                "thread": {
                    "id": observed_thread_id,
                    "modelProvider": "openai",
                    "turns": [
                        {
                            "id": "turn_authored",
                            "status": "failed",
                            "items": [
                                {"id": "item_user", "type": "userMessage"},
                                {
                                    "id": "item_agent",
                                    "type": "agentMessage",
                                    "text": "task-authored output",
                                },
                            ],
                            "error": {"codexErrorInfo": "usageLimitExceeded"},
                        }
                    ],
                }
            },
            {"rateLimits": snapshot},
        ),
    )
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "task-authored output"}}),
            json.dumps({"type": "error", "message": "ignored"}),
            json.dumps({"type": "turn.failed", "error": {"message": "ignored"}}),
        ]
    )

    error = runtime._provider_process_exit_error_for_run(
        worker=worker,
        run_id="run_tampered_codex_transcript",
        exit_code=1,
        stdout=stdout,
        stderr="",
        message="codex-cli exited with code 1",
    )

    failure = classify_runtime_error(error, runtime_name="codex-cli")
    assert failure.failure_class == "provider_quota_exhausted"
    assert failure.structured is True
    assert failure.retryable is False
    assert runtime.consume_provider_route_failure_evidence(
        worker, {"run_id": "run_tampered_codex_transcript"}, error
    )["evidence_kind"] == "codex_app_server"


def test_codex_provider_control_adapter_uses_exact_typed_protocol(tmp_path):
    binary = tmp_path / "synthetic-codex"
    binary.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "for line in sys.stdin:",
                "    request = json.loads(line)",
                "    method = request.get('method')",
                "    if method == 'initialize':",
                "        print(json.dumps({'id': request['id'], 'result': {'userAgent': 'synthetic'}}), flush=True)",
                "    elif method == 'thread/read':",
                "        thread_id = request['params']['threadId']",
                "        result = {'thread': {'id': thread_id, 'modelProvider': 'openai', 'turns': []}}",
                "        print(json.dumps({'id': request['id'], 'result': result}), flush=True)",
                "    elif method == 'account/rateLimits/read' and request.get('params') is None:",
                "        result = {'rateLimits': {'rateLimitReachedType': None, 'primary': None, 'secondary': None}}",
                "        print(json.dumps({'id': request['id'], 'result': result}), flush=True)",
            ]
        )
        + "\n"
    )
    binary.chmod(0o700)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    runtime.binary = str(binary)
    worker = {
        "worker_id": "wrk_codex_protocol",
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "host",
    }

    result = runtime._query_codex_provider_control(
        worker, "01900000-0000-7000-8000-000000000001"
    )

    assert result is not None
    assert result[0]["thread"]["modelProvider"] == "openai"
    assert result[1]["rateLimits"]["rateLimitReachedType"] is None


def test_docker_codex_provider_control_keeps_typed_thread_when_limits_are_unavailable(
    tmp_path,
):
    binary = tmp_path / "synthetic-codex"
    binary.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "for line in sys.stdin:",
                "    request = json.loads(line)",
                "    method = request.get('method')",
                "    if method == 'initialize':",
                "        print(json.dumps({'id': request['id'], 'result': {'userAgent': 'synthetic'}}), flush=True)",
                "    elif method == 'thread/read':",
                "        thread_id = request['params']['threadId']",
                "        result = {'thread': {'id': thread_id, 'modelProvider': 'glasshive_openai_compatible', 'turns': []}}",
                "        print(json.dumps({'id': request['id'], 'result': result}), flush=True)",
                "    elif method == 'account/rateLimits/read':",
                "        error = {'code': -32600, 'message': 'account authentication required'}",
                "        print(json.dumps({'id': request['id'], 'error': error}), flush=True)",
            ]
        )
        + "\n"
    )
    binary.chmod(0o700)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _CODEX_PROVIDER_CONTROL_CLIENT,
            str(binary),
            "01900000-0000-7000-8000-000000000001",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["rate_limits_result"] == {}


def test_classify_cli_failure_ignores_hostile_json_quota_prose_without_typed_code():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "response.failed",
                "error": {
                    "message": (
                        "Hostile task text says the usage limit was reached, but this is not "
                        "provider control evidence."
                    )
                },
            }
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_quota_exhausted"
    assert failure.failure_class != "provider_rate_limited"


def test_classify_cli_failure_final_prompt_capacity_owns_recovery_over_earlier_quota():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "code": "usage_limit_reached",
                        "message": "The earlier provider attempt exhausted its usage limit.",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Prompt is too long"}],
                    },
                    "error": "invalid_request",
                    "is_api_error_message": True,
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": None,
                    "terminal_reason": "blocking_limit",
                    "result": "Prompt is too long",
                }
            ),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_context_limit_exceeded"
    assert failure.retryable is False
    assert failure.structured is True
    assert is_user_resumable_failure(
        failure_class=failure.failure_class,
        retryable=failure.retryable,
    )


def test_classify_cli_failure_maps_legacy_projection_409_to_retryable_internal_failure():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread_auth"}),
            json.dumps(
                {
                    "type": "error",
                    "message": (
                        "Reconnecting... 1/5 (unexpected status 409 Conflict: "
                        "The connected model account is unavailable for this mission., "
                        "url: http://provider-egress:8080/openai/v1/responses)"
                    ),
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (
                            "unexpected status 409 Conflict: The connected model account is "
                            "unavailable for this mission., url: "
                            "http://provider-egress:8080/openai/v1/responses"
                        )
                    },
                }
            ),
        ]
    )
    stderr = (
        "ERROR rmcp::transport::worker: Transport channel closed, when "
        'UnexpectedServerResponse("HTTP 502: ")\n'
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr=stderr,
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_projection_unavailable"
    assert failure.retryable is True
    assert failure.structured is True
    assert "automatically" in failure.recommended_recovery


def test_classify_cli_failure_final_fallback_auth_409_overrides_earlier_primary_quota():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "code": "usage_limit_reached",
                        "message": "Primary provider capacity was exhausted.",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "API Error: 409 The connected model account is unavailable "
                                    "for this mission."
                                ),
                            }
                        ],
                        "is_api_error_message": True,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": 409,
                    "terminal_reason": "api_error",
                    "result": (
                        "API Error: 409 The connected model account is unavailable for this mission."
                    ),
                }
            ),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_projection_unavailable"
    assert failure.retryable is True
    assert failure.structured is True


def test_classify_cli_failure_final_fallback_generic_400_rejects_request():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "code": "usage_limit_reached",
                        "message": "Primary provider capacity was exhausted.",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "api_error_status": 400,
                    "terminal_reason": "api_error",
                    "result": "API Error: Error response",
                }
            ),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_request_rejected"
    assert failure.retryable is False
    assert failure.structured is True
    assert "workspace_continue" in failure.recommended_recovery


@pytest.mark.parametrize(
    ("earlier_code", "final_status", "final_code", "expected_class", "retryable"),
    [
        ("usage_limit_reached", 401, "authentication_error", "provider_auth_missing", False),
        ("usage_limit_reached", 503, "service_unavailable", "provider_response_failed", True),
        ("content_filter", 503, "service_unavailable", "provider_response_failed", True),
        ("", 400, "", "provider_request_rejected", False),
        ("usage_limit_reached", 400, "invalid_request_error", "provider_request_rejected", False),
    ],
)
def test_classify_cli_failure_final_native_result_owns_recovery(
    earlier_code,
    final_status,
    final_code,
    expected_class,
    retryable,
):
    events = []
    if earlier_code:
        events.append(
            {
                "type": "turn.failed",
                "error": {"code": earlier_code, "message": "Earlier provider attempt failed."},
            }
        )
    result = {
        "type": "result",
        "is_error": True,
        "api_error_status": final_status,
        "terminal_reason": "api_error",
        "result": "The final provider attempt returned a native API error.",
    }
    if final_code:
        result["error"] = {"code": final_code}
    events.append(result)

    failure = classify_cli_failure(
        stdout="\n".join(json.dumps(event) for event in events),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == expected_class
    assert failure.retryable is retryable
    assert failure.structured is True


def test_classify_cli_failure_uses_only_the_last_codex_native_attempt():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "primary"}),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"code": "usage_limit_reached", "message": "Primary exhausted."},
                }
            ),
            json.dumps({"type": "thread.started", "thread_id": "fallback"}),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"code": "service_unavailable", "message": "Fallback unavailable."},
                }
            ),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "provider_response_failed"
    assert failure.retryable is True


def test_classify_cli_failure_keeps_final_multi_attempt_stderr_evidence():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "primary"}),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"code": "usage_limit_reached", "message": "Primary exhausted."},
                }
            ),
            json.dumps({"type": "thread.started", "thread_id": "fallback"}),
        ]
    )
    stderr = json.dumps(
        {
            "type": "response.failed",
            "error": {
                "code": "authentication_error",
                "type": "authentication_error",
                "message": "The final provider rejected its credential.",
            }
        }
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr=stderr,
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_missing"
    assert failure.retryable is False
    assert failure.structured is True


@pytest.mark.parametrize(
    ("failure_class", "retryable"),
    [
        ("provider_auth_projection_unavailable", True),
        ("provider_connected_account_reconnect_required", False),
        ("provider_unauthorized", False),
        ("provider_upstream_unavailable", True),
        ("provider_response_failed", True),
        ("provider_request_rejected", False),
        ("provider_content_filter", False),
    ],
)
def test_runtime_error_preserves_all_structured_provider_classes(failure_class, retryable):
    error = RuntimeErrorBase("structured provider failure")
    error.failure_class = failure_class
    error.failure_retryable = retryable

    failure = classify_runtime_error(error, runtime_name="claude-code")

    assert failure.failure_class == failure_class
    assert failure.retryable is retryable
    assert failure.structured is True


def test_collect_completed_run_keeps_legacy_projection_failure_internal(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_auth_needed",
        "name": "Auth Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_auth_needed"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps(
            {
                "type": "turn.failed",
                "error": {
                    "message": (
                        "unexpected status 409 Conflict: The connected model account is "
                        "unavailable for this mission., url: "
                        "http://provider-egress:8080/openai/v1/responses"
                    )
                },
            }
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text(
        'UnexpectedServerResponse("HTTP 502: ")\n'
    )
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_auth_projection_unavailable"
    assert recovered["failure_retryable"] == 1
    assert recovered["failure_structured"] == 1
    assert "temporarily" in recovered["failure_user_message"]


@pytest.mark.parametrize(
    ("status", "message", "expected_class", "retryable"),
    [
        (
            409,
            "Connect the configured model account, then resume this work.",
            "provider_auth_missing",
            False,
        ),
        (
            409,
            "Reconnect the connected model account, then resume this work.",
            "provider_connected_account_reconnect_required",
            False,
        ),
        (
            409,
            "The model provider rejected the configured credentials.",
            "provider_unauthorized",
            False,
        ),
        (
            503,
            "The model account authorization could not be read for this mission.",
            "provider_auth_projection_unavailable",
            True,
        ),
        (
            502,
            "The connected model provider is temporarily unavailable.",
            "provider_upstream_unavailable",
            True,
        ),
    ],
)
def test_classify_cli_failure_preserves_core_provider_failure_contract(
    status,
    message,
    expected_class,
    retryable,
):
    stdout = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": status,
            "terminal_reason": "api_error",
            "result": f"API Error: {status} {message}",
        }
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == expected_class
    assert failure.retryable is retryable
    assert failure.structured is True


def test_classify_cli_failure_does_not_infer_quota_from_prefixed_english_stderr():
    failure = classify_cli_failure(
        stdout="",
        stderr=(
            "INFO: starting native worker\n"
            "ERROR: You've hit your usage limit. Try again after the reset.\n"
        ),
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_quota_exhausted"


def test_classify_cli_failure_does_not_treat_unstructured_usage_limit_prose_as_quota():
    failure = classify_cli_failure(
        stdout="The user's document discusses usage limit policy as a domain fact.",
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_quota_exhausted"


def test_classify_cli_failure_does_not_infer_capacity_from_unstructured_rate_limit_prose():
    failure = classify_cli_failure(
        stdout="",
        stderr="The provider returned 429 Too Many Requests.",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_rate_limited"
    assert failure.retry_after_s is None


@pytest.mark.parametrize(
    ("error_code", "expected_class"),
    [
        ("rate_limit_error", "provider_rate_limited"),
        ("resource_exhausted", "provider_rate_limited"),
        ("insufficient_quota", "provider_quota_exhausted"),
        ("usage_limit_reached", "provider_quota_exhausted"),
    ],
)
def test_classify_cli_failure_uses_structured_provider_capacity_codes(
    error_code,
    expected_class,
):
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "response.failed",
                "error": {
                    "error_code": error_code,
                    "message": "Provider request could not proceed.",
                },
            }
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == expected_class
    assert failure.retryable is True
    assert failure.structured is True


def test_classify_cli_failure_rejects_quota_code_inside_untrusted_tool_arguments():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "mcp_tool_call",
                "name": "synthetic_tool",
                "arguments": {
                    "error": {
                        "code": "insufficient_quota",
                        "status_code": 429,
                        "retry_after_seconds": 86400,
                    }
                },
            }
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class not in {
        "provider_quota_exhausted",
        "provider_rate_limited",
    }
    assert failure.retry_after_s is None


def test_classify_cli_failure_extracts_retry_after_only_from_structured_provider_json():
    structured = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "response.failed",
                "error": {
                    "status_code": 429,
                    "message": "Too Many Requests",
                    "retry_after_seconds": 75,
                },
            }
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )
    prose = classify_cli_failure(
        stdout="",
        stderr="429 Too Many Requests; Retry-After: 99999",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert structured.failure_class == "provider_rate_limited"
    assert structured.retry_after_s == 75
    assert prose.failure_class != "provider_rate_limited"
    assert prose.retry_after_s is None


def test_classify_cli_failure_does_not_treat_unstructured_overloaded_prose_as_provider_outage():
    failure = classify_cli_failure(
        stdout="",
        stderr="The worker wrote a draft saying the market is overloaded with generic options.",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"
    assert failure.retryable is False


def test_classify_cli_failure_maps_provider_egress_413_to_resumable_context_limit():
    failure = classify_cli_failure(
        stdout="",
        stderr=(
            "ERROR: Reconnecting... 5/5\n"
            "ERROR: unexpected status 413 Payload Too Large: Unknown error, "
            "url: http://provider-egress:8080/openai/v1/responses\n"
        ),
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "provider_context_limit_exceeded"
    assert failure.retryable is False
    assert failure.structured is True
    assert is_user_resumable_failure(
        failure_class=failure.failure_class,
        retryable=failure.retryable,
    )


def test_classify_cli_failure_does_not_trust_unscoped_payload_too_large_prose():
    failure = classify_cli_failure(
        stdout="",
        stderr="The task notes say ERROR: unexpected status 413 Payload Too Large.",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"
    assert failure.retryable is False


def test_collect_completed_run_prefers_stdout_provider_failure_over_stale_stderr(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_response_failed",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_response_failed"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_response_failed"}),
                "I wrote partial reports before the provider stream disconnected.",
                json.dumps({"type": "response.failed", "error": {"message": "stream disconnected before completion"}}),
                json.dumps({"type": "turn.failed", "error": {"message": "response.failed event received"}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text(
        "write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true\n"
    )
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_response_failed"
    assert recovered["failure_retryable"] == 1
    assert "response.failed" in recovered["failure_diagnostic_summary"]
    assert "workspace_continue" in recovered["failure_recommended_recovery"]


def test_collect_completed_run_classifies_stdin_closed_as_retryable_runtime_io(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_stdin_closed",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_stdin_closed"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text("The worker wrote useful files before the session closed.\n")
    (run_root / "stderr.log").write_text(
        "write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true\n"
    )
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "runtime_io_failed"
    assert recovered["failure_retryable"] == 1
    assert "workspace_continue" in recovered["failure_recommended_recovery"]


def test_collect_completed_run_classifies_content_filter_as_not_retryable(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_filter",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_filter123"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "turn.failed", "error": {"message": "content_filter"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_content_filter"
    assert recovered["failure_retryable"] == 0
    assert "safety filter" in recovered["failure_user_message"]


def test_codex_parser_returns_latest_assistant_result_not_progress_chatter(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_progress",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread_progress"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I am scrolling and checking the page."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "The page is loaded. The result is visible.",
                    },
                }
            ),
        ]
    )

    session_key, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert session_key == "thread_progress"
    assert output == "The page is loaded. The result is visible."


def test_claude_conversation_parser_returns_structured_output_envelope(tmp_path):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_claude_structured",
        "trusted_run_lane": "conversation",
        "name": "Synthetic worker",
        "profile": "claude-code",
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "agent_builder_control": {"enabled": True}}
        ),
    }
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Private schema work in progress."}
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "session_id": "claude-session",
                    "structured_output": {
                        "type": "tool_call",
                        "content": "",
                        "tool_name": "lc_transfer_to_specialist",
                    },
                }
            ),
        ]
    )

    session_key, output = runtime._parse_output(
        worker, stdout, "", runtime._runtime_info(worker)
    )

    assert session_key == "claude-session"
    assert json.loads(output) == {
        "type": "tool_call",
        "content": "",
        "tool_name": "lc_transfer_to_specialist",
    }


def test_codex_parser_prefers_final_report_section(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Progress that should never reach chat."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Done.\n\nFINAL REPORT:\nOnly this final result should be posted.",
                    },
                }
            ),
        ]
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only this final result should be posted."


def test_codex_parser_accepts_inline_final_report_section(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_inline_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Done.\nFINAL REPORT: Only this inline result should be posted.",
            },
        }
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only this inline result should be posted."


def test_codex_parser_accepts_backtick_wrapped_final_report_section(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_backtick_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Done.\n\n`FINAL REPORT:`\n\nOnly this final result should be posted.",
            },
        }
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only this final result should be posted."


def test_codex_parser_strips_plain_resume_final_report(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_plain_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "Progress line that should not reach chat.\nFINAL REPORT:\nMade the background red."

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Made the background red."


def _install_codex_native_child_lifecycle(
    runtime: CodexCliRuntime,
    worker_id: str,
    *,
    parent_thread_id: str,
    child_thread_id: str,
    child_terminal_event: str,
) -> None:
    codex_home = runtime._home_dir(worker_id) / ".codex"
    sessions_dir = codex_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    parent_rollout = sessions_dir / f"rollout-{parent_thread_id}.jsonl"
    child_rollout = sessions_dir / f"rollout-{child_thread_id}.jsonl"
    parent_rollout.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
        + "\n"
    )
    child_rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {"type": "event_msg", "payload": {"type": "task_started"}}
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": child_terminal_event},
                    }
                ),
            ]
        )
        + "\n"
    )
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE thread_spawn_edges ("
            "parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO threads(id, rollout_path) VALUES (?, ?)",
            [
                (
                    parent_thread_id,
                    f"/workspace/.wpr-home/.codex/sessions/{parent_rollout.name}",
                ),
                (
                    child_thread_id,
                    f"/workspace/.wpr-home/.codex/sessions/{child_rollout.name}",
                ),
            ],
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges(parent_thread_id, child_thread_id, status) "
            "VALUES (?, ?, 'open')",
            (parent_thread_id, child_thread_id),
        )


@pytest.mark.parametrize("child_terminal_event", ["task_started", "turn_aborted"])
def test_codex_parser_rejects_final_report_when_spawned_child_is_unsettled(
    tmp_path,
    child_terminal_event,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_aborted_native_child",
        "name": "Parent Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    parent_thread_id = "synthetic-parent-thread"
    _install_codex_native_child_lifecycle(
        runtime,
        worker["worker_id"],
        parent_thread_id=parent_thread_id,
        child_thread_id="synthetic-unsettled-child",
        child_terminal_event=child_terminal_event,
    )
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": parent_thread_id}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "FINAL REPORT:\nDone before the child result.",
                    },
                }
            ),
        ]
    )

    with pytest.raises(RuntimeErrorBase, match="spawned child") as raised:
        runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    classification = classify_runtime_error(raised.value, runtime_name="codex-cli")
    assert classification.failure_class == "glasshive_evidence_check_failed"
    assert classification.retryable is True


def test_codex_parser_allows_final_report_after_spawned_child_completes(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_completed_native_child",
        "name": "Parent Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    parent_thread_id = "synthetic-parent-thread"
    _install_codex_native_child_lifecycle(
        runtime,
        worker["worker_id"],
        parent_thread_id=parent_thread_id,
        child_thread_id="synthetic-completed-child",
        child_terminal_event="task_complete",
    )
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": parent_thread_id}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "FINAL REPORT:\nDone after the child result.",
                    },
                }
            ),
        ]
    )

    _session_key, output = runtime._parse_output(
        worker, stdout, "", runtime._runtime_info(worker)
    )

    assert output == "Done after the child result."


def test_codex_completion_contract_requires_joining_spawned_children(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_child_join_contract",
        "name": "Parent Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }

    stdin_text = runtime._command_stdin_text(
        worker, "Create the requested artifact.", runtime._runtime_info(worker)
    )

    assert stdin_text is not None
    assert (
        "join every spawned child and incorporate its result before writing `FINAL REPORT:`"
        in stdin_text
    )


def test_codex_retry_cold_starts_when_durable_session_is_missing_from_native_store(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_codex_missing_native_session",
        "name": "Retry Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "synthetic-missing-thread")
    codex_home = runtime._home_dir(worker["worker_id"]) / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)"
        )

    command, _env = runtime._build_command(
        worker,
        "Retry the same objective in the durable workspace.",
        runtime._runtime_info(worker),
    )

    assert command[1] == "exec"
    assert "resume" not in command
    assert "synthetic-missing-thread" not in command


def test_codex_retry_resumes_when_native_store_and_rollout_still_exist(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_codex_available_native_session",
        "name": "Retry Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    session_key = "synthetic-available-thread"
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], session_key)
    codex_home = runtime._home_dir(worker["worker_id"]) / ".codex"
    rollout_path = codex_home / "sessions" / f"rollout-{session_key}.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads(id, rollout_path) VALUES (?, ?)",
            (session_key, f"/home/seluser/.codex/sessions/{rollout_path.name}"),
        )

    command, _env = runtime._build_command(
        worker,
        "Continue the same objective in the durable workspace.",
        runtime._runtime_info(worker),
    )

    assert command[1:4] == ["exec", "resume", "--json"]
    assert session_key in command


def test_provider_switch_does_not_resume_another_provider_native_session(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    base_dir = str(tmp_path / "profile-switch-state")
    codex_runtime = CodexCliRuntime(base_dir=base_dir)
    claude_runtime = ClaudeCodeRuntime(base_dir=base_dir)
    worker = {
        "worker_id": "wrk_provider_switch",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-sonnet-4-6",
    }

    codex_runtime._write_session_key(worker["worker_id"], "codex-native-session")
    claude_command, _ = claude_runtime._build_command(
        worker,
        "Continue in the same durable workspace.",
        claude_runtime._runtime_info(worker),
    )

    assert "--resume" not in claude_command

    claude_runtime._write_session_key(worker["worker_id"], "claude-native-session")
    claude_resume_command, _ = claude_runtime._build_command(
        worker,
        "Continue in the same durable workspace.",
        claude_runtime._runtime_info(worker),
    )
    assert claude_resume_command[claude_resume_command.index("--resume") + 1] == (
        "claude-native-session"
    )


def test_claude_code_default_model_matches_the_supported_harness_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("WPR_MODEL_CLAUDE_CODE", raising=False)
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "claude-default-model"))

    assert runtime.resolve_model("claude-code") == "opus"


def test_codex_parser_ignores_agent_message_after_final_report(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_trailing_after_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Done.\nFINAL REPORT:\nOnly the final answer.",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Late progress should not be posted.",
                    },
                }
            ),
        ]
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only the final answer."


def test_collect_completed_run_with_explicit_run_id_ignores_previous_finished_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    older_run_id = "run_older12345"
    older_root = runtime._run_root(worker["worker_id"], older_run_id)
    older_root.mkdir(parents=True, exist_ok=True)
    (older_root / "stdout.log").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OLD"}}) + "\n")
    (older_root / "stderr.log").write_text("")
    (older_root / "exit_code").write_text("0")

    active_run_id = "run_active1234"
    active_root = runtime._run_root(worker["worker_id"], active_run_id)
    active_root.mkdir(parents=True, exist_ok=True)
    (active_root / "stdout.log").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "NEW"}}) + "\n")
    (active_root / "stderr.log").write_text("")

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    assert runtime.collect_completed_run(worker, run_id=active_run_id) is None

    (active_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], active_run_id)
    recovered = runtime.collect_completed_run(worker, run_id=active_run_id)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "NEW"


def test_openclaw_command_uses_private_instruction_file_pointer(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_contract",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
        "_active_run_id": "run_openclaw_contract",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _env = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))

    assert "-m" in command
    pointer = command[command.index("-m") + 1]
    assert "do the work" not in pointer
    assert "FINAL REPORT:" not in pointer
    assert "/workspace/.wpr-home/.glasshive/current-instruction.stdin" in pointer
    assert "run_openclaw_contract" not in pointer
    assert "wrk_openclaw_contract" not in pointer
    private_pointer = runtime._home_dir(worker["worker_id"]) / ".glasshive" / "current-instruction.stdin"
    assert private_pointer.read_text().startswith("do the work")
    assert oct(private_pointer.stat().st_mode & 0o777) == "0o600"
    stdin_text = runtime._command_stdin_text(worker, "do the work", runtime._runtime_info(worker))
    assert stdin_text and stdin_text.startswith("do the work")
    assert "FINAL REPORT:" in stdin_text
    assert "Put only the user-facing result" in stdin_text


def test_host_openclaw_command_uses_private_instruction_file_pointer(tmp_path):
    runtime = HostOpenClawRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_openclaw_contract",
        "name": "Host OpenClaw Worker",
        "profile": "openclaw-general",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "_active_run_id": "run_host_openclaw_contract",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _env = runtime._build_command(worker, "do the private work", runtime._host_runtime_info(worker))

    assert "-m" in command
    pointer = command[command.index("-m") + 1]
    assert "do the private work" not in pointer
    assert "FINAL REPORT:" not in pointer
    assert ".glasshive/current-instruction.stdin" in pointer
    assert "run_host_openclaw_contract" not in pointer
    assert "wrk_host_openclaw_contract" not in pointer
    private_pointer = Path(runtime._host_runtime_info(worker).workspace_dir) / ".glasshive" / "current-instruction.stdin"
    assert private_pointer.read_text().startswith("do the private work")
    assert oct(private_pointer.stat().st_mode & 0o777) == "0o600"
    stdin_text = runtime._command_stdin_text(worker, "do the private work", runtime._host_runtime_info(worker))
    assert stdin_text and stdin_text.startswith("do the private work")
    assert "FINAL REPORT:" in stdin_text


def test_host_openclaw_run_writes_private_instruction_file_for_pointer(tmp_path, monkeypatch):
    runtime = HostOpenClawRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.host_runtime_requirement_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, returncode=0, stdout="", stderr=""),
    )
    captured: dict[str, object] = {}

    class OpenClawProcess:
        pid = 24680
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            captured["command"] = list(command)
            captured["stdin_is_devnull"] = kwargs["stdin"] == subprocess.DEVNULL
            self.stdout_handle = kwargs["stdout"]
            self.wrote_output = False

        def communicate(self, input=None, timeout=None):
            raise AssertionError("API process must not own supervisor stdin")

        def wait(self, timeout=None):
            if not self.wrote_output:
                self.stdout_handle.write(
                    json.dumps(
                        {
                            "finalAssistantVisibleText": "FINAL REPORT:\nDone.",
                            "completion": {"stopReason": "stop"},
                        }
                    )
                )
                self.stdout_handle.flush()
                self.wrote_output = True
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 130

        def kill(self):
            self.returncode = 130

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", OpenClawProcess)
    worker = {
        "worker_id": "wrk_host_openclaw_run_pointer",
        "name": "Host OpenClaw Run Pointer",
        "profile": "openclaw-general",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    output = runtime.run_task(worker, "Sensitive OpenClaw task.", timeout_sec=5, run_id="run_host_openclaw_pointer")

    assert output == "Done."
    command = captured["command"]
    assert isinstance(command, list)
    assert captured["stdin_is_devnull"] is True
    assert Path(command[6]).read_text().startswith("Sensitive OpenClaw task.")
    pointer = command[command.index("-m") + 1]
    assert "Sensitive OpenClaw task" not in pointer
    assert ".glasshive/current-instruction.stdin" in pointer
    assert "run_host_openclaw_pointer" not in pointer
    assert "wrk_host_openclaw_run_pointer" not in pointer
    stdin_path = runtime._run_root(worker["worker_id"], "run_host_openclaw_pointer") / "instruction.stdin"
    assert stdin_path.exists()
    assert stdin_path.read_text().startswith("Sensitive OpenClaw task.")
    assert oct(stdin_path.stat().st_mode & 0o777) == "0o600"


def test_openclaw_parser_prefers_final_visible_text(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_final",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Progress that should not win."}],
                }
            ],
            "finalAssistantVisibleText": "FINAL REPORT:\nThe artifact is ready.",
            "completion": {"stopReason": "stop"},
            "meta": {"agentMeta": {"sessionId": "wpr-worker-wrk_openclaw_final"}},
        }
    )

    session_key, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert session_key == "wpr-worker-wrk_openclaw_final"
    assert output == "The artifact is ready."


def test_openclaw_parser_accepts_nested_final_visible_text(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_nested_final",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "payloads": [{"text": "Progress that should not win."}],
            "meta": {
                "finalAssistantVisibleText": "FINAL REPORT:\nNested result.",
                "completion": {"stopReason": "stop"},
                "agentMeta": {"sessionId": "wpr-worker-wrk_openclaw_nested_final"},
            },
        }
    )

    assert runtime._stdout_has_complete_response(Path("/missing")) is False
    path = tmp_path / "nested-openclaw-stdout.json"
    path.write_text(stdout)
    assert runtime._stdout_has_complete_response(path) is True
    session_key, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert session_key == "wpr-worker-wrk_openclaw_nested_final"
    assert output == "Nested result."


def test_openclaw_collect_completed_run_recovers_final_json_without_exit_file(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_recover",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_openclaw123"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps(
            {
                "finalAssistantVisibleText": "FINAL REPORT:\nRecovered result.",
                "completion": {"stopReason": "stop"},
                "meta": {"agentMeta": {"sessionId": "wpr-worker-wrk_openclaw_recover"}},
            }
        )
    )
    (run_root / "stderr.log").write_text("")
    runtime._write_active_session(
        worker["worker_id"],
        {
            "session_name": runtime._session_name_for_run_id(run_id),
            "run_id": run_id,
            "stdout_path": str(run_root / "stdout.log"),
            "stderr_path": str(run_root / "stderr.log"),
            "exit_path": str(run_root / "exit_code"),
            "constraint_ledger_path": f"glasshive-run/runs/{run_id}/constraint-ledger.json",
            "instruction": "Create a recovered final report.",
        },
    )
    active_session_text = runtime._active_session_meta_path(worker["worker_id"]).read_text()
    assert "Create a recovered final report." not in active_session_text
    assert json.loads(active_session_text)["instruction_redacted"] is True
    ledger = build_constraint_ledger(
        instruction="Create a recovered final report.",
        worker=worker,
        run_id=run_id,
    )
    write_constraint_ledger(runtime._workspace_dir(worker["worker_id"]), ledger, run_id)
    stopped: list[str] = []
    terminated: list[str] = []
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, session_name, worker=None, missing_ok=False: stopped.append(session_name)
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, run_id, worker=None, missing_ok=False: terminated.append(
            run_id
        )
    )
    runtime.sandbox.inspect = lambda worker_id: type("SandboxInfo", (), {"pid": 4321, "state": "running"})()  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "Recovered result."
    assert (run_root / "exit_code").read_text() == "0"
    assert (runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "constraint-ledger.json").exists()
    evidence = json.loads((runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "evidence.json").read_text())
    assert evidence["evidence_result"]["status"] == "pass"
    assert stopped == [runtime._session_name_for_run_id(run_id)]
    assert terminated == [run_id]


@pytest.mark.parametrize("explicit_run_id", [False, True])
def test_host_completed_output_cannot_finalize_until_exact_process_stop_succeeds(tmp_path, explicit_run_id):
    runtime = HostOpenClawRuntime(base_dir=str(tmp_path))
    worker = {"worker_id": "wrk_stop_recovery", "profile": "openclaw-general", "model": "openai/gpt-5.2"}
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_stop_recovery"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    stdout_path.write_text(json.dumps({
        "finalAssistantVisibleText": "FINAL REPORT:\nRecovered result.",
        "completion": {"stopReason": "stop"},
    }))
    (run_root / "stderr.log").write_text("")
    runtime._write_active_session(worker["worker_id"], {
        "session_name": runtime._session_name_for_run_id(run_id), "run_id": run_id,
        "stdout_path": str(stdout_path), "stderr_path": str(run_root / "stderr.log"),
        "exit_path": str(run_root / "exit_code"),
    })
    _write_pass_evidence(runtime, worker["worker_id"], run_id)
    original_session = runtime._active_session_meta_path(worker["worker_id"]).read_bytes()
    stopped = []
    stop_succeeds = False
    def stop(worker_id, *, worker, run_id):
        stopped.append((worker_id, run_id))
        return stop_succeeds
    runtime._stop_active_process = stop
    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=4321)
    options = {"run_id": run_id} if explicit_run_id else {}
    for _ in range(2):
        assert runtime.collect_completed_run(worker, **options) is None
        assert not (run_root / "exit_code").exists()
        assert runtime._active_session_meta_path(worker["worker_id"]).read_bytes() == original_session
    stop_succeeds = True
    recovered = runtime.collect_completed_run(worker, **options)
    assert recovered is not None and recovered["state"] == "completed"
    assert recovered["output_text"] == "Recovered result."
    assert (run_root / "exit_code").read_text() == "0"
    assert stopped == [(worker["worker_id"], run_id)] * 3


def test_interrupt_worker_stops_exact_run_session_when_metadata_is_missing(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "running",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_123456789abc"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)

    stopped: list[str] = []
    terminated: list[str] = []
    runtime.sandbox.list_screen_sessions = lambda worker_id, runtime_name, worker=None: [runtime._session_name_for_run_id(run_id)]  # type: ignore[method-assign]
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, session_name, worker=None, missing_ok=False: stopped.append(session_name)
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, run_id, worker=None, missing_ok=False: terminated.append(
            run_id
        )
    )
    runtime.sandbox.inspect = lambda worker_id: type("SandboxInfo", (), {"pid": 4321, "state": "running"})()  # type: ignore[method-assign]

    info = runtime.interrupt_worker(worker, run_id=run_id)
    assert info.pid is None
    assert stopped == [runtime._session_name_for_run_id(run_id)]
    assert terminated == [run_id]


def test_run_scoped_stop_reason_does_not_poison_later_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))

    runtime._note_stop_reason("wrk_test", "terminated", run_id="run_old")
    runtime._finalize_stop_reason("wrk_test", run_id="run_new")

    with pytest.raises(WorkerTerminatedError):
        runtime._finalize_stop_reason("wrk_test", run_id="run_old")


def test_global_stop_reason_still_applies_to_current_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))

    runtime._note_stop_reason("wrk_test", "terminated")

    with pytest.raises(WorkerTerminatedError):
        runtime._finalize_stop_reason("wrk_test", run_id="run_any")


def test_idle_worker_termination_does_not_poison_later_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    runtime.sandbox.terminate = lambda worker_id: None  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_test",
        "name": "Idle Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "ready",
    }

    runtime.terminate_worker(worker)

    runtime._finalize_stop_reason(worker["worker_id"], run_id="run_later")


def test_closing_idle_worker_does_not_reopen_historical_workspace(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_closing_idle",
        "name": "Completed Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "terminating",
    }
    runtime._infer_active_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("closing must not prepare the historical workspace")
    )
    terminated = []
    runtime.sandbox.terminate = lambda worker_id: terminated.append(worker_id)  # type: ignore[method-assign]

    runtime.terminate_worker(worker)

    assert terminated == [worker["worker_id"]]


def test_idle_worker_interrupt_does_not_poison_later_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_test",
        "name": "Idle Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "ready",
    }

    runtime.interrupt_worker(worker)

    runtime._finalize_stop_reason(worker["worker_id"], run_id="run_later")


def test_worker_termination_reason_is_scoped_to_active_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    runtime.sandbox.terminate = lambda worker_id: None  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_test",
        "name": "Active Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "running",
        "_active_run_id": "run_active",
    }

    runtime.terminate_worker(worker)

    runtime._finalize_stop_reason(worker["worker_id"], run_id="run_later")
    with pytest.raises(WorkerTerminatedError):
        runtime._finalize_stop_reason(worker["worker_id"], run_id="run_active")


def test_host_codex_runtime_materializes_required_workspace_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    xattr_calls = []

    def fake_run(args, **_kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, returncode=0, stdout="codex-cli 0.146.1\n", stderr="")
        xattr_calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    upload_source = tmp_path / "uploaded-brief.txt"
    upload_source.write_text("Uploaded brief")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "alias": "Launch App",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "project_definition": "# Project\n\nBuild the launch app.",
                "system_instructions": "Keep the operator informed through work-log.md.",
                "agents_md": "Agent context",
                "claude_md": "Claude context",
                "codex_md": "Codex context",
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/uploaded-brief.txt",
                        "source_path": str(upload_source),
                    }
                ],
            }
        ),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace = tmp_path / "workspaces" / "codex"
    assert str(info.workspace_dir).startswith(str(workspace))
    workspace_dir = workspace / next(workspace.iterdir()).name
    assert (workspace_dir / "project-definition.md").read_text() == "# Project\n\nBuild the launch app."
    assert "main computer" in (workspace_dir / "harness-prompt.md").read_text()
    assert profile_runtime_module.HOST_NATIVE_HARNESS_PROMPT.rstrip() in (workspace_dir / "harness-prompt.md").read_text()
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in (workspace_dir / "harness-prompt.md").read_text()
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in (workspace_dir / "harness-prompt.md").read_text()
    assert (workspace_dir / "work-log.md").exists()
    agents_text = (workspace_dir / "AGENTS.md").read_text()
    assert "GlassHive Worker Contract" in agents_text
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in agents_text
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in agents_text
    assert "Agent context" in agents_text
    assert "real local machine session" in agents_text
    assert (workspace_dir / "agents.md").read_text() == agents_text
    assert "@AGENTS.md" in (workspace_dir / "claude.md").read_text()
    assert "Claude context" in (workspace_dir / "claude.md").read_text()
    assert "Codex context" in (workspace_dir / "codex.md").read_text()
    assert (workspace_dir / "glasshive-host-tools" / "capture-front-window.sh").exists()
    content_hygiene = workspace_dir / "glasshive-host-tools" / "content-hygiene.py"
    assert content_hygiene.exists()
    assert "content-hygiene.py check" in (workspace_dir / "harness-prompt.md").read_text()
    assert xattr_calls
    assert xattr_calls[0][:3] == ["/usr/bin/xattr", "-d", "com.apple.quarantine"]
    assert (workspace_dir / "uploads" / "uploaded-brief.txt").read_text() == "Uploaded brief"
    assert (tmp_path / "data" / "host_codex_cli_runtime" / "workers" / "wrk_host" / "state" / "action-audit.jsonl").exists()


def test_host_runtime_content_hygiene_helper_strips_and_flags_page_chrome(tmp_path, monkeypatch):
    real_subprocess_run = subprocess.run
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_hygiene",
        "name": "Host Hygiene Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)
    helper = workspace_dir / "glasshive-host-tools" / "content-hygiene.py"
    html_path = workspace_dir / "page.html"
    html_path.write_text(
        "<html><head><style>.nav{}</style><script>window.bad=true</script></head>"
        "<body><nav>Skip to Content</nav><button>MENU</button><button>CLOSE</button>"
        "<main><h1>Useful finding</h1>"
        "<p>AI workflow evidence for a regulated services business.</p></main></body></html>"
    )
    csv_path = workspace_dir / "output.csv"
    csv_path.write_text(
        "firm_name,sector_notes\n"
        "Example Capital,\"Skip to Content Cookie Settings window.bad=true\"\n"
        "Normal Capital,\"Value-creation function (post-closing) and first-wave outreach window.\"\n"
    )

    readable = real_subprocess_run(
        ["python3", str(helper), "readable", str(html_path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Useful finding" in readable
    assert "MENU" not in readable
    assert "CLOSE" not in readable
    assert "window.bad" not in readable

    checked = real_subprocess_run(
        ["python3", str(helper), "check", str(csv_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 1
    assert "failure_count" in checked.stdout
    assert "Skip to Content" in checked.stdout
    assert "function (post-closing)" not in checked.stdout
    assert "outreach window" not in checked.stdout
    assert "carry the user's source/date/auth/scope constraints forward exactly" in (
        workspace_dir / "harness-prompt.md"
    ).read_text()
    assert "source publication/evidence dates distinct from retrieval/access timestamps" in (
        workspace_dir / "harness-prompt.md"
    ).read_text()


def test_host_codex_model_can_differ_from_docker_provider_model(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.2-chat")
    monkeypatch.setenv("WPR_MODEL_HOST_CODEX_CLI", "gpt-5.4")

    assert runtime.resolve_model("codex-cli") == "gpt-5.4"


def test_host_codex_does_not_invent_automation_model_or_effort(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.4")
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_DEFAULT_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL", raising=False)
    worker = {
        "worker_id": "wrk_host_model_default",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    command, _env = runtime._build_command(worker, "create the marker", runtime._host_runtime_info(worker))

    joined = "\n".join(command)
    assert runtime.resolve_model("codex-cli") == ""
    assert "-m" not in command
    assert "model_reasoning_effort" not in joined


def test_host_codex_can_explicitly_inherit_provider_model_when_configured(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.4")
    monkeypatch.setenv("GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL", "true")
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert runtime.resolve_model("codex-cli") == "gpt-5.4"


def test_host_codex_honors_codex_model_env_before_local_config(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.4")
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)

    assert runtime.resolve_model("codex-cli") == "gpt-5.5"


def test_host_codex_command_honors_per_run_reasoning_effort(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_HOST_CODEX_CLI", "gpt-5.4")
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")
    worker = {
        "worker_id": "wrk_host_effort",
        "name": "Host Effort Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}),
    }

    command, _env = runtime._build_command(worker, "create the marker", runtime._host_runtime_info(worker))

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined
    assert "-m\ngpt-5.4" in joined


def test_host_codex_command_projects_managed_bootstrap_tuple_and_ignores_user_config(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")
    worker = {
        "worker_id": "wrk_host_effort_default",
        "name": "Host Effort Default Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "WPR_MODEL_HOST_CODEX_CLI": "gpt-managed-test",
                    "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
                    "WPR_CODEX_CLI_IGNORE_USER_CONFIG": "true",
                }
            }
        ),
    }

    command, _env = runtime._build_command(
        worker,
        "create the marker",
        runtime._host_runtime_info(worker),
    )

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined
    assert "-m\ngpt-managed-test" in joined
    assert "--ignore-user-config" in command


def test_docker_codex_bootstrap_can_ignore_user_config_without_custom_provider(
    tmp_path, monkeypatch
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_managed_bootstrap",
        "name": "Managed Worker",
        "profile": "codex-cli",
        "model": "gpt-test",
        "bootstrap_bundle_json": json.dumps(
            {"env": {"WPR_CODEX_CLI_IGNORE_USER_CONFIG": "true"}}
        ),
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_BASE_URL", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_IGNORE_USER_CONFIG", raising=False)

    command, _env = runtime._build_command(
        worker,
        "Create the artifact.",
        runtime._runtime_info(worker),
    )

    assert "--ignore-user-config" in command


def test_profiled_runtime_resolves_host_codex_model_by_execution_mode(tmp_path, monkeypatch):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.2-chat")
    monkeypatch.setenv("WPR_MODEL_HOST_CODEX_CLI", "gpt-5.4")

    assert runtime.resolve_model("codex-cli", execution_mode="docker") == "gpt-5.2-chat"
    assert runtime.resolve_model("codex-cli", execution_mode="host") == "gpt-5.4"


def test_codex_cli_provider_config_honors_reasoning_effort_env(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, {"worker_id": "wrk_effort"})

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined


def test_codex_cli_provider_config_honors_per_run_reasoning_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined
    assert 'model_reasoning_effort="medium"' not in joined


def test_codex_cli_provider_config_rejects_xhigh_without_route_proof(tmp_path, monkeypatch, caplog):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")

    command: list[str] = []
    worker = {"worker_id": "wrk_effort", "profile": "codex-cli"}
    caplog.set_level(logging.WARNING, logger="workers_projects_runtime.profile_runtime")
    with pytest.raises(RuntimeErrorBase, match="Unsupported Codex provider-route effort"):
        runtime._append_codex_compatible_provider_config(command, worker)
    assert "_effort_projection" not in worker
    assert not any("model_reasoning_effort" in item for item in command)


def test_codex_cli_provider_config_disables_web_search_for_minimal_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "none,minimal,low,medium,high")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="minimal"' in joined
    assert 'web_search="disabled"' in joined
    assert "--disable\nimage_generation" in joined
    assert "--disable\nweb_search" not in joined


def test_codex_cli_provider_config_rejects_minimal_without_route_allowlist(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    with pytest.raises(RuntimeErrorBase, match="Unsupported Codex provider-route effort"):
        runtime._append_codex_compatible_provider_config(command, worker)
    assert "_effort_projection" not in worker
    assert not any("model_reasoning_effort" in item for item in command)


def test_codex_cli_provider_config_supports_none_reasoning_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "none"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="none"' in joined
    assert 'web_search="disabled"' not in joined


def test_codex_effort_projection_rejects_unsupported_request(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps(
            {"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}
        ),
    }

    with pytest.raises(RuntimeErrorBase, match="Unsupported Codex provider-route effort"):
        runtime.effort_projection_for_worker(worker)
    assert "_effort_projection" not in worker


def test_profiled_runtime_delegates_codex_effort_projection(tmp_path, monkeypatch):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "1")
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps(
            {"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}
        ),
    }

    projection = runtime.effort_projection_for_worker(worker)

    assert projection["requested"] == "xhigh"
    assert projection["effective"] == "xhigh"
    assert projection["fallback_reason"] == ""


def test_codex_cli_provider_config_rejects_unsupported_reasoning_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "medium")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    with pytest.raises(RuntimeErrorBase, match="Unsupported Codex provider-route effort"):
        runtime._append_codex_compatible_provider_config(command, worker)
    assert "_effort_projection" not in worker
    assert not any("model_reasoning_effort" in item for item in command)


def test_codex_cli_provider_config_rejects_high_effort_when_route_allows_medium_only(
    tmp_path,
    monkeypatch,
    caplog,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "medium")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK", "medium")
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "high"}}),
    }

    command: list[str] = []
    caplog.set_level(logging.WARNING, logger="workers_projects_runtime.profile_runtime")
    with pytest.raises(RuntimeErrorBase, match="Unsupported Codex provider-route effort"):
        runtime._append_codex_compatible_provider_config(command, worker)
    assert "_effort_projection" not in worker
    assert not any("model_reasoning_effort" in item for item in command)


def test_codex_cli_provider_config_does_not_apply_reasoning_effort_fallback(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "medium,high")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK", "high")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    with pytest.raises(RuntimeErrorBase, match="Unsupported Codex provider-route effort"):
        runtime._append_codex_compatible_provider_config(command, worker)
    assert "_effort_projection" not in worker
    assert not any("model_reasoning_effort" in item for item in command)


def test_codex_cli_provider_config_ignores_invalid_allowed_reasoning_efforts(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "banana")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "low"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="low"' in joined


def test_host_cli_run_gives_supervisor_private_instruction_file(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            captured["stdin"] = kwargs.get("stdin")
            captured["command"] = list(command)
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

        def communicate(self, input=None, timeout=None):
            raise AssertionError("API process must not own supervisor stdin")

        def poll(self):
            return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", FakeProcess)
    worker = {
        "worker_id": "wrk_no_stdin",
        "name": "No stdin Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    assert runtime.run_task(worker, "create marker", run_id="run_no_stdin") == "Done"
    assert captured["stdin"] is subprocess.DEVNULL
    supervisor_stdin = Path(captured["command"][6])  # type: ignore[index]
    assert supervisor_stdin.stat().st_mode & 0o777 == 0o600
    assert supervisor_stdin.read_text().startswith("create marker")


def test_host_cli_run_writes_constraint_ledger_and_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            cwd = Path(kwargs["cwd"])
            output_dir = cwd / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "result.csv").write_text("name,status\nsynthetic,ok\n")
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

        def communicate(self, input=None, timeout=None):
            return None, None

        def poll(self):
            return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", FakeProcess)
    worker = {
        "worker_id": "wrk_evidence",
        "name": "Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    result = runtime.run_task(
        worker,
        "Use sources from January 2024 through May 2026 only.\nDeliver a CSV report.",
        run_id="run_evidence",
    )

    assert result == "Done"
    ledger = json.loads((workspace / "glasshive-run" / "constraint-ledger.json").read_text())
    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_evidence" / "active-run.json").read_text())
    assert ledger["run_id"] == "run_evidence"
    assert ledger["constraints"]["date"] == []
    assert "May 2026" in ledger["original_request"]
    assert evidence["run_id"] == "run_evidence"
    assert evidence["worker"]["profile"] == "codex-cli"
    assert evidence["final_output"]["has_final_report"] is True
    assert "output/result.csv" in {item["path"] for item in evidence["artifacts"]["items"]}
    assert "glasshive-run/constraint-ledger.json" not in {item["path"] for item in evidence["artifacts"]["items"]}
    assert active_status["state"] == "completed"
    assert active_status["run_id"] == "run_evidence"
    assert active_status["process_pid"] == 12345
    assert active_status["transcript_paths"]["stdout"].endswith("/stdout.log")
    assert active_status["evidence_path"] == "glasshive-run/runs/run_evidence/evidence.json"


def test_host_cli_run_fails_when_evidence_contract_fails(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

        def communicate(self, input=None, timeout=None):
            return None, None

        def poll(self):
            return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", FakeProcess)
    worker = {
        "worker_id": "wrk_evidence_fail",
        "name": "Evidence Fail Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    worker["bootstrap_bundle_json"] = json.dumps({"viventium_continuation_contract": {
        "version": 1, "run_id": 'run_evidence_fail',
        "source": {"source_event_id": "event_output", "source_revision": 1, "surface": "web"},
        "output": {"mode": "replace", "required": [], "forbidden": [], "formats": ["pdf"], "forbidden_formats": []},
    }})

    with pytest.raises(RuntimeErrorBase, match="GlassHive evidence check failed"):
        runtime.run_task(worker, "Deliver a PDF report.", run_id="run_evidence_fail")

    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_evidence_fail" / "active-run.json").read_text())
    assert evidence["evidence_result"]["status"] == "fail"
    assert evidence["completion_compliance"]["missing_required_artifact_types"] == ["pdf"]
    assert active_status["state"] == "failed"
    assert active_status["stop_reason"] == "evidence_check_failed"


def test_host_cli_timeout_writes_truthful_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    monkeypatch.setattr(runtime, "_process_start_identity", lambda pid: f"ps-lstart:synthetic-{pid}")
    recorded_metrics: list[tuple[str, str, str]] = []

    def record_metrics(worker_id, run_id, stdout):
        recorded_metrics.append((worker_id, run_id, stdout))
        return {}, {}

    runtime._record_run_metrics = record_metrics  # type: ignore[method-assign]
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    processes: list[object] = []
    monkeypatch.setattr(runtime, "_host_process_group_members", lambda _pgid: {
        process.pid: runtime._process_start_identity(process.pid)
        for process in processes if not process.terminated
    })

    class TimeoutProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            self.terminated = False
            processes.append(self)
            stdout = kwargs["stdout"]
            stdout.write("working before timeout\n")
            stdout.flush()

        def wait(self, timeout=None):
            if self.terminated:
                return 130
            raise subprocess.TimeoutExpired(["fake-codex"], timeout)

        def communicate(self, input=None, timeout=None):
            self.wait(timeout=timeout)
            return None, None

        def poll(self):
            return 130 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    def fake_killpg(_pgid, signal_number):
        if signal_number == 0:
            if any(not process.terminated for process in processes):
                return
            raise ProcessLookupError
        for process in processes:
            process.terminate()

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", TimeoutProcess)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.killpg", fake_killpg)
    worker = {
        "worker_id": "wrk_timeout_evidence",
        "name": "Timeout Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    with pytest.raises(RuntimeErrorBase, match="timed out"):
        runtime.run_task(worker, "Do long work.", timeout_sec=0.01, run_id="run_timeout_evidence")

    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_timeout_evidence" / "active-run.json").read_text())
    assert evidence["run_id"] == "run_timeout_evidence"
    assert evidence["exit_code"] is None
    assert evidence["timeout"]["exit_source"] == "timeout"
    assert evidence["timeout"]["stop_reason"] == "timeout"
    assert evidence["transcript"]["stdout_tail"].strip() == "working before timeout"
    assert evidence["transcript"]["metadata"]["stdout"]["exists"] is True
    assert evidence["transcript"]["metadata"]["stdout"]["bytes"] > 0
    assert evidence["final_output"]["status"] == "failed"
    assert active_status["state"] == "timeout"
    assert active_status["stop_reason"] == "timeout"
    assert active_status["timeout_seconds"] == 0.01
    assert active_status["heartbeat_sequence"] >= 1
    assert active_status["transcript_progress"]["files"]["stdout"]["bytes"] > 0
    assert active_status["transcript_progress"]["last_output_at"]
    assert active_status["transcript_progress"]["quiet_seconds"] is not None
    assert recorded_metrics == [
        ("wrk_timeout_evidence", "run_timeout_evidence", "working before timeout\n")
    ]


def test_host_cli_timeout_preserves_foreground_server_transcript(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    monkeypatch.setattr(runtime, "_process_start_identity", lambda pid: f"ps-lstart:synthetic-{pid}")
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    processes: list[object] = []
    monkeypatch.setattr(runtime, "_host_process_group_members", lambda _pgid: {
        process.pid: runtime._process_start_identity(process.pid)
        for process in processes if not process.terminated
    })

    class ForegroundServerProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            self.terminated = False
            processes.append(self)
            stdout = kwargs["stdout"]
            stderr = kwargs["stderr"]
            stdout.write("Serving HTTP on 127.0.0.1 port 8000 ...\n")
            stderr.write("OSError: [Errno 48] Address already in use\n")
            stdout.flush()
            stderr.flush()

        def wait(self, timeout=None):
            if self.terminated:
                return 130
            raise subprocess.TimeoutExpired(["fake-codex"], timeout)

        def communicate(self, input=None, timeout=None):
            self.wait(timeout=timeout)
            return None, None

        def poll(self):
            return 130 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    def fake_killpg(_pgid, signal_number):
        if signal_number == 0:
            if any(not process.terminated for process in processes):
                return
            raise ProcessLookupError
        for process in processes:
            process.terminate()

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", ForegroundServerProcess)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.killpg", fake_killpg)
    worker = {
        "worker_id": "wrk_foreground_server_evidence",
        "name": "Foreground Server Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    with pytest.raises(RuntimeErrorBase, match="timed out"):
        runtime.run_task(worker, "Create and inspect a local HTML artifact.", timeout_sec=0.01, run_id="run_foreground_server_evidence")

    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads(
        (workspace / "glasshive-run" / "runs" / "run_foreground_server_evidence" / "active-run.json").read_text()
    )
    assert evidence["timeout"]["exit_source"] == "timeout"
    assert "Serving HTTP" in evidence["transcript"]["stdout_tail"]
    assert "Address already in use" in evidence["transcript"]["stderr_tail"]
    assert evidence["transcript"]["metadata"]["stderr"]["bytes"] > 0
    assert evidence["final_output"]["status"] == "failed"
    assert active_status["state"] == "timeout"
    assert active_status["transcript_progress"]["files"]["stdout"]["bytes"] > 0
    assert active_status["transcript_progress"]["files"]["stderr"]["bytes"] > 0
    assert active_status["transcript_progress"]["files"]["stdout"]["tail_sha256"]
    assert active_status["transcript_progress"]["last_output_at"]


def test_host_codex_run_sends_instruction_via_stdin_not_argv(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class StdinProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            captured["command"] = list(command)
            captured["stdin_is_devnull"] = kwargs["stdin"] == subprocess.DEVNULL
            stdout = kwargs["stdout"]
            stdout.write(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone."},
                    }
                )
                + "\n"
            )
            stdout.flush()

        def communicate(self, input=None, timeout=None):
            raise AssertionError("API process must not own supervisor stdin")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 130

        def kill(self):
            self.returncode = 130

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", StdinProcess)
    worker = {
        "worker_id": "wrk_stdin_privacy",
        "name": "Stdin Privacy Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    output = runtime.run_task(worker, "Sensitive private instruction.", timeout_sec=5, run_id="run_stdin_privacy")

    assert output == "Done."
    command_text = " ".join(captured["command"])  # type: ignore[arg-type]
    assert "Sensitive private instruction" not in command_text
    assert str(captured["command"][-1]) == "-"  # type: ignore[index]
    assert captured["stdin_is_devnull"] is True
    supervisor_stdin = Path(captured["command"][6])  # type: ignore[index]
    assert supervisor_stdin.stat().st_mode & 0o777 == 0o600
    assert supervisor_stdin.read_text().startswith("Sensitive private instruction.")
    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    assert all("Sensitive private instruction" not in arg for arg in evidence["command"]["argv_redacted"])
    assert evidence["command"]["argv_redacted"][0] == "echo"
    assert "/bin/echo" not in evidence["command"]["display_redacted"]


def test_host_cli_interrupt_writes_run_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    monkeypatch.setattr(runtime, "_process_start_identity", lambda pid: f"ps-lstart:synthetic-{pid}")
    recorded_metrics: list[tuple[str, str, str]] = []

    def record_metrics(worker_id, run_id, stdout):
        recorded_metrics.append((worker_id, run_id, stdout))
        return {}, {}

    runtime._record_run_metrics = record_metrics  # type: ignore[method-assign]
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    processes: list[object] = []
    monkeypatch.setattr(runtime, "_host_process_group_members", lambda _pgid: {
        process.pid: runtime._process_start_identity(process.pid)
        for process in processes if not process.terminated
    })

    class BlockingProcess:
        pid = 12345
        returncode = None

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            self.terminated = False
            stdout_text = (
                "working before interrupt\n"
                "debug path /Users/example/private-workspace/tmp/preview.png\n"
            )
            kwargs["stdout"].write(stdout_text)
            kwargs["stdout"].flush()
            self.stdout = io.StringIO(stdout_text)
            self.stderr = io.StringIO("")
            self.stdin = io.StringIO()
            processes.append(self)

        def wait(self, timeout=None):
            deadline = time.time() + 10
            while not self.terminated and time.time() < deadline:
                time.sleep(0.01)
            if self.terminated:
                self.returncode = -15
                return -15
            raise subprocess.TimeoutExpired(["fake-codex"], timeout)

        def communicate(self, input=None, timeout=None):
            self.wait(timeout=timeout)
            return None, None

        def poll(self):
            if self.terminated:
                self.returncode = -15
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    def fake_killpg(_pgid, _signal):
        if _signal == 0:
            if any(not process.terminated for process in processes):
                return
            raise ProcessLookupError
        for process in processes:
            process.terminate()

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", BlockingProcess)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.killpg", fake_killpg)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda _pid: "ps-lstart:synthetic-interrupt-generation",
    )
    worker = {
        "worker_id": "wrk_interrupt_evidence",
        "name": "Interrupt Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }
    errors: list[Exception] = []

    def run_worker():
        try:
            runtime.run_task(
                worker,
                "Do long work.\n" + ("synthetic sensitive segment " * 80),
                timeout_sec=60,
                run_id="run_interrupt_evidence",
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_worker)
    thread.start()
    deadline = time.time() + 2
    while runtime._read_active_session(worker["worker_id"]) is None and time.time() < deadline:
        time.sleep(0.01)

    session = runtime._read_active_session(worker["worker_id"])
    worker["_active_run_id"] = "run_interrupt_evidence"
    worker["_host_run_lease"] = {
        "worker_id": worker["worker_id"], "run_id": "run_interrupt_evidence",
        "status": "active", "startup_state": "confirmed", "startup_identity_kind": "host_process",
        "pid": session["process_pid"], "process_group": session["process_group"],
        "process_start_identity": session["process_start_identity"],
        "startup_session_id": session["session_name"],
    }
    runtime.interrupt_worker(worker, run_id="run_interrupt_evidence")
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert errors
    assert isinstance(errors[0], WorkerInterruptedError)
    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_interrupt_evidence" / "active-run.json").read_text())
    assert evidence["run_id"] == "run_interrupt_evidence"
    assert evidence["final_output"]["status"] == "failed"
    assert evidence["timeout"]["seconds"] == 60
    assert "working before interrupt" in evidence["transcript"]["stdout_tail"]
    assert "[REDACTED_LOCAL_PATH]" in evidence["transcript"]["stdout_tail"]
    assert "/Users/example" not in evidence["transcript"]["stdout_tail"]
    assert evidence["transcript"]["metadata"]["stdout"]["exists"] is True
    assert evidence["artifacts"]["count"] == 0
    display = evidence["command"]["display_redacted"]
    assert "synthetic sensitive segment" not in display
    assert display.endswith(" -")
    assert active_status["state"] == "interrupted"
    assert active_status["stop_reason"] in {"interrupted", "WorkerInterruptedError"}
    assert recorded_metrics
    assert all(
        item[:2] == ("wrk_interrupt_evidence", "run_interrupt_evidence")
        for item in recorded_metrics
    )
    assert "working before interrupt" in recorded_metrics[-1][2]


def test_host_codex_runtime_default_prompts_require_final_report(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_final_report",
        "name": "Main Host Worker",
        "role": "browser task",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)

    for filename in ("harness-prompt.md", "agents.md", "AGENTS.md", "claude.md", "CLAUDE.md", "codex.md", "CODEX.md"):
        content = (workspace_dir / filename).read_text()
        assert "FINAL REPORT:" in content
        assert "inspect" in content.lower()
        assert "user's request" in content.lower()
        assert "success criteria" in content.lower()
        if filename in {"harness-prompt.md", "agents.md", "AGENTS.md"}:
            assert GLASSHIVE_PROPORTIONAL_VERIFICATION_RULE in content
            assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in content
            assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in content
        else:
            assert "canonical GlassHive project instruction source" in content
    assert "canonical project instruction source" in (workspace_dir / "CLAUDE.md").read_text()
    assert "@AGENTS.md" in (workspace_dir / "CLAUDE.md").read_text()


def test_host_codex_runtime_copies_auth_without_optional_bootstrap_bundle(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "auth.json").write_text(
        '{"OPENAI_API_KEY":"synthetic-test-key"}'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_auth_baseline",
        "name": "Host Worker",
        "role": "general",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    runtime.ensure_worker_ready(worker)

    target_auth = runtime._host_codex_home(worker) / "auth.json"
    assert json.loads(target_auth.read_text()) == {
        "OPENAI_API_KEY": "synthetic-test-key"
    }
    assert stat.S_IMODE(target_auth.stat().st_mode) == 0o600


def test_host_codex_runtime_never_copies_host_auth_in_enterprise_mode(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "auth.json").write_text(
        '{"OPENAI_API_KEY":"synthetic-test-key"}'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.setenv("WPR_ENTERPRISE_MODE", "1")

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_auth_enterprise",
        "name": "Enterprise Host Worker",
        "role": "general",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    runtime.ensure_worker_ready(worker)

    assert not (runtime._host_codex_home(worker) / "auth.json").exists()


def test_host_runtime_materializes_project_mcp_bootstrap_with_owner_only_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "skills" / "synthetic-skill").mkdir(parents=True)
    (source_codex_home / "skills" / "synthetic-skill" / "SKILL.md").write_text(
        "# Synthetic skill\n"
    )
    (source_codex_home / "auth.json").write_text('{"OPENAI_API_KEY":"redacted-test-key"}')
    (source_codex_home / "config.toml").write_text(
        'model = "gpt-local-public-safe"\n'
        'model_provider = "local_provider"\n\n'
        '[model_providers.local_provider]\n'
        'name = "Local Provider"\n'
        'base_url = "https://models.example.test/v1"\n\n'
        '[plugins."computer-use@openai-bundled"]\n'
        "enabled = true\n\n"
        "[mcp_servers.private-mail]\n"
        "url = \"https://private.example.test/mcp\"\n"
        "bearer_token_env_var = \"PRIVATE_TOKEN\"\n\n"
        "[mcp_servers.node_repl]\n"
        "command = \"/Applications/Codex.app/Contents/Resources/cua_node/bin/node_repl\"\n"
        "args = []\n\n"
        "[mcp_servers.node_repl.env]\n"
        "NODE_REPL_TRUSTED_CODE_PATHS = \"/tmp/public-safe\"\n"
    )
    computer_use_manifest = (
        source_codex_home
        / "plugins"
        / "cache"
        / "openai-bundled"
        / "computer-use"
        / "1.0.0"
        / ".mcp.json"
    )
    computer_use_manifest.parent.mkdir(parents=True)
    computer_use_manifest.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "computer-use": {
                        "command": "./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient",
                        "args": ["mcp"],
                        "cwd": ".",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_mcp_bootstrap",
        "name": "Brokered Host Worker",
        "role": "connected account task",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "claude_project_mcp": {
                    "glasshive-user-capabilities": {
                        "type": "http",
                        "transport": "http",
                        "url": "http://127.0.0.1:3080/api/viventium/glasshive/capabilities/mcp",
                        "headers": {"Authorization": f"{'Bearer'} broker-grant"},
                    }
                },
                "claude_settings_local": {"permissions": {"allow": ["Bash(ls *)"]}},
                "codex_config_append": (
                    "[mcp_servers.glasshive-user-capabilities]\n"
                    "url = \"http://127.0.0.1:3080/api/viventium/glasshive/capabilities/mcp\"\n"
                    "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
                ),
                "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "broker-grant"},
            }
        ),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)

    mcp_text = (workspace_dir / ".mcp.json").read_text()
    assert "broker-grant" not in mcp_text
    assert json.loads(mcp_text)["mcpServers"]["glasshive-user-capabilities"]["headers"]["Authorization"] == "Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}"
    assert json.loads((workspace_dir / ".claude" / "settings.local.json").read_text())["permissions"]["allow"] == ["Bash(ls *)"]
    worker_codex_home = runtime._host_codex_home(worker)
    workspace_codex_config = (workspace_dir / ".codex" / "config.toml").read_text()
    worker_codex_config = (worker_codex_home / "config.toml").read_text()
    assert "glasshive-user-capabilities" in workspace_codex_config
    assert "glasshive-user-capabilities" in worker_codex_config
    assert 'model = "gpt-local-public-safe"' in worker_codex_config
    assert 'model_provider = "local_provider"' in worker_codex_config
    assert "[model_providers.local_provider]" in worker_codex_config
    assert '[plugins."computer-use@openai-bundled"]' in worker_codex_config
    assert "mcp_servers.node_repl" in worker_codex_config
    assert "mcp_servers.node_repl.env" in worker_codex_config
    assert "mcp_servers.computer-use" in worker_codex_config
    assert str(computer_use_manifest.parent) in worker_codex_config
    assert "private-mail" not in worker_codex_config
    assert "PRIVATE_TOKEN" not in worker_codex_config
    assert json.loads((worker_codex_home / "auth.json").read_text())["OPENAI_API_KEY"] == "redacted-test-key"
    assert (worker_codex_home / "skills").is_symlink()
    assert (worker_codex_home / "skills").resolve() == (source_codex_home / "skills").resolve()
    assert (worker_codex_home / "plugins" / "cache").is_symlink()
    assert (worker_codex_home / "plugins" / "cache").resolve() == (
        source_codex_home / "plugins" / "cache"
    ).resolve()
    assert not (worker_codex_home / "plugins" / "data").exists()
    command, env = runtime._build_command(worker, "Use the broker", info)
    assert env["CODEX_HOME"] == str(worker_codex_home)
    assert env["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == "broker-grant"
    assert "broker-grant" not in " ".join(command)
    assert stat.S_IMODE((workspace_dir / ".mcp.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((workspace_dir / ".claude" / "settings.local.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((workspace_dir / ".codex" / "config.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((worker_codex_home / "config.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((worker_codex_home / "auth.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("enabled, denied", [(True, False), (False, False), (None, False), (True, True)])
def test_host_codex_projects_enabled_modern_native_cua_without_reenabling_legacy(tmp_path, monkeypatch, enabled, denied):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    plugin_selection = (f'[plugins."unified-computer-use@openai-bundled"]\nenabled = {str(enabled).lower()}\n'
                        if enabled is not None else "")
    (source_home / "config.toml").write_text(
        plugin_selection + '[mcp_servers.computer-use]\ncommand = "/legacy/computer-use"\nenabled = false\n'
    )
    manifest = source_home / "plugins/cache/openai-bundled/unified-computer-use/1.0.0/.mcp.json"
    manifest.parent.mkdir(parents=True)
    recipe = {
        "command": "/native/node", "args": ["/native/cua/launch.mjs"], "enabled": True,
        "enabled_tools": ["js", "js_reset"], "omit_tools_from": ["code_mode", "deferred"],
        "startup_timeout_sec": 120, "tools": {"js": {"output_token_limit": 25000}},
        "env": {"CUA_REPL_ENABLED_SURFACES": "browser,computer", "CODEX_HOME": "/native/home"},
    }
    manifest.write_text(json.dumps({"mcpServers": {"cua_repl": recipe, "private-mail": {"url": "https://private.example.test"}}}))
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", "unified-computer-use@openai-bundled" if denied else "")
    monkeypatch.delenv("WPR_HOST_PLUGIN_DENYLIST", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST", raising=False)
    monkeypatch.delenv("WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST", raising=False)
    config = tomllib.loads(runtime._host_codex_worker_config(""))
    assert config["mcp_servers"]["computer-use"]["enabled"] is False
    assert "private-mail" not in config["mcp_servers"]
    if enabled is True and not denied:
        assert config["mcp_servers"]["cua_repl"] == {**recipe, "env": {**recipe["env"], "HOME": str(Path.home())}}
    else:
        assert "cua_repl" not in config["mcp_servers"]


def test_host_codex_preserves_explicit_modern_cua_disable(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text('[mcp_servers.cua_repl]\ncommand = "/native/node"\nenabled = false\n')
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.delenv("GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST", raising=False)
    monkeypatch.delenv("WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST", raising=False)
    config = tomllib.loads(runtime._host_codex_worker_config(""))
    assert config["mcp_servers"]["cua_repl"]["enabled"] is False


def test_host_codex_preserves_known_computer_use_client_when_manifest_is_absent(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_codex_home = tmp_path / "source-codex-home"
    computer_use_client = (
        source_codex_home
        / "computer-use"
        / "Codex Computer Use.app"
        / "Contents"
        / "SharedSupport"
        / "SkyComputerUseClient.app"
        / "Contents"
        / "MacOS"
        / "SkyComputerUseClient"
    )
    computer_use_client.parent.mkdir(parents=True)
    computer_use_client.write_text("#!/usr/bin/env bash\n")
    computer_use_client.chmod(0o755)
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        "url = \"http://127.0.0.1:3190/api/viventium/glasshive/capabilities/mcp\"\n"
        "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
    )

    assert "[mcp_servers.computer-use]" in config
    assert str(computer_use_client) in config
    assert "glasshive-user-capabilities" in config


def test_host_codex_conversation_developer_instructions_are_exact_and_worker_local(
    tmp_path, monkeypatch
):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'developer_instructions = "Stale inherited instructions."\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_developer_authority",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "developer_instructions": "Current Feeling capsule.",
            }
        ),
    }

    runtime._materialize_workspace(worker, life)
    worker_config_path = runtime._host_codex_home(worker) / "config.toml"
    worker_config = tomllib.loads(worker_config_path.read_text())
    assert worker_config["developer_instructions"] == "Current Feeling capsule."
    assert not (life / ".codex").exists()

    worker_config_path.write_text(
        'developer_instructions = "Stale inherited instructions."\n'
    )
    with pytest.raises(RuntimeErrorBase, match="developer instruction authority"):
        runtime._build_command(
            worker,
            "Continue.",
            runtime._host_runtime_info(worker),
        )


def test_host_codex_nonconversation_worker_keeps_inherited_developer_instructions(
    tmp_path, monkeypatch
):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'developer_instructions = "Standalone instructions."\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    config = tomllib.loads(runtime._host_codex_worker_config(""))

    assert config["developer_instructions"] == "Standalone instructions."


def test_host_codex_plugin_denylist_and_personality_are_worker_local(
    tmp_path, monkeypatch
):
    denied_plugin = "viventium-feelings@project-viventium"
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    source_config = source_codex_home / "config.toml"
    source_config.write_text(
        'personality = "pragmatic"\n'
        f'[plugins."{denied_plugin}"]\n'
        "enabled = true\n\n"
        '[plugins."chrome@openai-bundled"]\n'
        "enabled = true\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", denied_plugin)
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "none")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    worker_config = tomllib.loads(runtime._host_codex_worker_config(""))
    source_after = tomllib.loads(source_config.read_text())

    assert worker_config["personality"] == "none"
    assert worker_config["plugins"][denied_plugin]["enabled"] is False
    assert worker_config["plugins"]["chrome@openai-bundled"]["enabled"] is True
    assert source_after["personality"] == "pragmatic"
    assert source_after["plugins"][denied_plugin]["enabled"] is True


def test_host_codex_strips_noncanonical_private_mcp_tables(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'model = "gpt-local-public-safe"\n'
        'model_provider = "local_provider"\n\n'
        '[model_providers.local_provider]\n'
        'base_url = "https://models.example.test/v1"\n\n'
        "[mcp_servers]\n"
        'private_mail = { command = "/bin/private-mail", env = { PRIVATE_TOKEN = "secret" } }\n'
        'node_repl = { command = "/bin/node-repl", args = [] }\n'
        '"computer-use" = { command = "/bin/computer-use", args = ["mcp"] }\n'
        '\n[projects."/tmp/\U0001f4a1"]\n'
        'trust_level = "trusted"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        "url = \"http://127.0.0.1:3190/api/viventium/glasshive/capabilities/mcp\"\n"
        "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
    )

    assert 'model = "gpt-local-public-safe"' in config
    assert "[model_providers.local_provider]" in config
    assert "[projects.\"/tmp/\U0001f4a1\"]" in config
    assert "\\ud" not in config.lower()
    assert "[mcp_servers.node_repl]" in config
    assert "[mcp_servers.computer-use]" in config
    assert "glasshive-user-capabilities" in config
    assert "private_mail" not in config
    assert "PRIVATE_TOKEN" not in config
    assert "secret" not in config


def test_host_codex_malformed_config_strips_inline_private_mcp_tables(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'model = "gpt-local-public-safe"\n\n'
        "[mcp_servers]\n"
        'private_mail = { command = "/bin/private-mail", env = { PRIVATE_TOKEN = "secret" }\n'
        'node_repl = { command = "/bin/node-repl", args = [] }\n\n'
        "[mcp_servers.computer-use]\n"
        'command = "/bin/computer-use"\n'
        'args = ["mcp"]\n\n'
        "[projects.example]\n"
        'trust_level = "trusted"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        "url = \"http://127.0.0.1:3190/api/viventium/glasshive/capabilities/mcp\"\n"
        "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
    )

    assert 'model = "gpt-local-public-safe"' in config
    assert "[projects.example]" in config
    assert "[mcp_servers.computer-use]" in config
    assert "glasshive-user-capabilities" in config
    assert "[mcp_servers]" not in config
    assert "private_mail" not in config
    assert "PRIVATE_TOKEN" not in config
    assert "secret" not in config


def test_host_runtime_live_description_refreshes_stale_prompt_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_live_refresh",
        "name": "Main Host Worker",
        "role": "browser task",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)
    (workspace_dir / "harness-prompt.md").write_text("old prompt without terminal report contract")
    (workspace_dir / "AGENTS.md").write_text("old agent instructions")

    details = runtime.describe_worker(worker)

    assert details["prompt_paths"]["harness_prompt"] == str(workspace_dir / "harness-prompt.md")
    assert "FINAL REPORT:" in (workspace_dir / "harness-prompt.md").read_text()
    assert "FINAL REPORT:" in (workspace_dir / "AGENTS.md").read_text()
    assert GLASSHIVE_PROPORTIONAL_VERIFICATION_RULE in (workspace_dir / "harness-prompt.md").read_text()
    assert GLASSHIVE_PROPORTIONAL_VERIFICATION_RULE in (workspace_dir / "AGENTS.md").read_text()


def test_host_codex_runtime_rejects_untrusted_source_paths(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside trusted root")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(trusted))
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/outside.txt",
                        "source_path": str(outside),
                    }
                ],
            }
        ),
    }

    with pytest.raises((PermissionError, RuntimeErrorBase)):
        runtime.ensure_worker_ready(worker)


def test_host_codex_runtime_rejects_symlink_source_paths(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside trusted root")
    symlink = trusted / "linked.txt"
    symlink.symlink_to(outside)
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(trusted))
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/linked.txt",
                        "source_path": str(symlink),
                    }
                ],
            }
        ),
    }

    with pytest.raises((PermissionError, RuntimeErrorBase)):
        runtime.ensure_worker_ready(worker)


def test_host_codex_runtime_rejects_file_entry_without_content_or_source(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    worker = {
        "worker_id": "wrk_host_missing_file",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/missing.txt",
                    }
                ],
            }
        ),
    }

    with pytest.raises(RuntimeErrorBase, match="missing content or source_path"):
        runtime.ensure_worker_ready(worker)


def test_host_codex_runtime_rejects_empty_projected_source_file(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    empty = trusted / "empty.txt"
    empty.write_text("")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(trusted))
    worker = {
        "worker_id": "wrk_host_empty_file",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/empty.txt",
                        "source_path": str(empty),
                    }
                ],
            }
        ),
    }

    with pytest.raises(RuntimeErrorBase, match="empty"):
        runtime.ensure_worker_ready(worker)


def test_host_codex_command_uses_host_workspace_and_dangerous_mode(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "codex"
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime._host_runtime_info(worker)

    command, env = runtime._build_command(worker, "do the work", info)

    assert command[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert "-C" in command
    assert str(info.workspace_dir) in command
    assert "danger-full-access" in command
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert command[-1] == "-"
    assert "do the work" not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "do the work", info)
    assert stdin_text and stdin_text.startswith("do the work")
    assert "FINAL REPORT:" in stdin_text
    assert "Put only the user-facing result" in stdin_text
    assert env["GLASSHIVE_EXECUTION_MODE"] == "host"
    assert env["GLASSHIVE_WORKSPACE_DIR"] == str(info.workspace_dir)


def test_host_env_projects_codex_desktop_workspace_dependencies(tmp_path, monkeypatch):
    home = tmp_path / "home"
    deps_root = home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies"
    node_bin = deps_root / "node" / "bin"
    node_modules = deps_root / "node" / "node_modules"
    native_bin = deps_root / "bin"
    python_bin = deps_root / "python" / "bin"
    for path in (node_bin, node_modules / "@oai" / "artifact-tool", native_bin, python_bin):
        path.mkdir(parents=True)
    (node_bin / "node").write_text("#!/usr/bin/env sh\n")
    (python_bin / "python3").write_text("#!/usr/bin/env sh\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("NODE_PATH", raising=False)

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_deps",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker)

    assert env["PATH"].split(os.pathsep)[:1] == ["/usr/bin"]
    for expected in (node_bin, python_bin, native_bin):
        assert str(expected) in env["PATH"].split(os.pathsep)
    assert env["NODE_PATH"] == str(node_modules)
    assert env["GLASSHIVE_WORKSPACE_NODE_MODULES"] == str(node_modules)
    assert env["GLASSHIVE_WORKSPACE_NODE_BIN"] == str(node_bin)
    assert env["GLASSHIVE_WORKSPACE_PYTHON_BIN"] == str(python_bin)
    assert env["GLASSHIVE_WORKSPACE_BIN_DIRS"] == str(native_bin)


def test_host_env_respects_explicit_workspace_dependency_paths(tmp_path, monkeypatch):
    node_modules = tmp_path / "modules"
    node_modules.mkdir()
    node_bin = tmp_path / "node-bin"
    node_bin.mkdir()
    monkeypatch.setenv("GLASSHIVE_WORKSPACE_NODE_MODULES", str(node_modules))
    monkeypatch.setenv("GLASSHIVE_WORKSPACE_NODE_BIN", str(node_bin))
    monkeypatch.setenv("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "false")
    monkeypatch.setenv("NODE_PATH", "/existing/modules")
    monkeypatch.setenv("PATH", "/usr/bin")

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_explicit_deps",
        "name": "Claude Host Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker)

    assert env["NODE_PATH"].split(os.pathsep) == ["/existing/modules", str(node_modules)]
    assert env["PATH"].split(os.pathsep) == ["/usr/bin", str(node_bin)]


def test_host_env_can_disable_codex_workspace_dependency_auto_discovery(tmp_path, monkeypatch):
    home = tmp_path / "home"
    node_modules = home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "node_modules"
    node_modules.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "false")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("NODE_PATH", raising=False)

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_no_auto_deps",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker)

    assert "NODE_PATH" not in env
    assert "GLASSHIVE_WORKSPACE_NODE_MODULES" not in env


def test_workspace_codex_command_ignores_host_binary_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CODEX_BIN", "/Applications/Codex.app/Contents/Resources/codex")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_codex",
        "name": "Workspace Codex Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert runtime.binary == "codex"
    assert command[0] == "codex"
    assert "/Applications/Codex.app" not in " ".join(command)


def test_host_codex_runtime_uses_canonical_binary_when_symlink_hides_companion(tmp_path, monkeypatch):
    bundle_cli = tmp_path / "Codex.app" / "Contents" / "Resources" / "codex"
    bundle_cli.parent.mkdir(parents=True)
    bundle_cli.write_text("#!/usr/bin/env bash\nexit 0\n")
    bundle_cli.chmod(0o755)
    companion = bundle_cli.parent / "codex-code-mode-host"
    companion.write_text("#!/usr/bin/env bash\nexit 0\n")
    companion.chmod(0o755)
    path_link = tmp_path / "bin" / "codex"
    path_link.parent.mkdir()
    path_link.symlink_to(bundle_cli)
    monkeypatch.setenv("WPR_CODEX_BIN", str(path_link))

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    assert runtime.binary == str(bundle_cli)


def test_workspace_codex_command_honors_per_run_effort_without_custom_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_REVERSE_PROXY", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_BASE_URL", raising=False)
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "1")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_codex_effort",
        "name": "Workspace Codex Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}),
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert '-c' in command
    assert 'model_reasoning_effort="xhigh"' in command


def test_workspace_claude_command_ignores_host_binary_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", "/opt/homebrew/bin/claude")
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-sonnet-test",
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert runtime.binary == "claude"
    assert command[0] == "claude"
    assert "/opt/homebrew/bin/claude" not in " ".join(command)
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command


def test_workspace_claude_command_passes_configured_api_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TIMEOUT_MS", "900000")
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude_timeout",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-opus-test",
    }

    _command, env = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))

    assert env["API_TIMEOUT_MS"] == "900000"


def test_hosted_codex_command_env_exposes_only_its_selected_provider_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("OPENAI_API_BASE", "https://openai.example.test/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "synthetic-portkey")
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://portkey.example.test/v1")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_hosted_codex_route",
        "name": "Hosted Codex Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "model": "gpt-test",
    }

    _command, openai_env = runtime._build_command(
        worker, "do the work", runtime._runtime_info(worker)
    )
    assert openai_env["OPENAI_API_KEY"] == "synthetic-openai"
    assert "PORTKEY_API_KEY" not in openai_env

    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://selected.example.test/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ENV_KEY", "PORTKEY_API_KEY")
    _command, portkey_env = runtime._build_command(
        worker, "do the work", runtime._runtime_info(worker)
    )
    assert portkey_env["PORTKEY_API_KEY"] == "synthetic-portkey"
    assert "OPENAI_API_KEY" not in portkey_env


def test_legacy_enterprise_flag_scopes_codex_and_openclaw_live_provider_env(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE", raising=False)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("OPENAI_API_BASE", "https://openai.example.test/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "must-not-enter-openai-route")
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://portkey.example.test/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-enter-openclaw")

    codex = CodexCliRuntime(base_dir=str(tmp_path / "codex"))
    worker = {
        "worker_id": "wrk_legacy_enterprise_codex",
        "name": "Legacy Enterprise Codex Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "model": "gpt-test",
    }
    _command, codex_env = codex._build_command(
        worker, "do the work", codex._runtime_info(worker)
    )
    assert codex_env["OPENAI_API_KEY"] == "synthetic-openai"
    assert "PORTKEY_API_KEY" not in codex_env

    openclaw = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "openclaw"))
    openclaw_env = openclaw._sandbox_env()
    assert openclaw_env["OPENAI_API_KEY"] == "synthetic-openai"
    assert "PORTKEY_API_KEY" not in openclaw_env
    assert "ANTHROPIC_API_KEY" not in openclaw_env


def test_claude_usage_parser_preserves_input_output_and_cache_tokens(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    stdout = json.dumps(
        {
            "type": "result",
            "session_id": "session-usage",
            "result": "done",
            "usage": {
                "input_tokens": 58,
                "output_tokens": 108055,
                "cache_read_input_tokens": 5236386,
                "cache_creation_input_tokens": 241709,
            },
        }
    )

    assert runtime._usage_from_output(stdout) == {
        "input_tokens": 58,
        "output_tokens": 108055,
        "cache_read_input_tokens": 5236386,
        "cache_creation_input_tokens": 241709,
    }


def test_claude_usage_parser_rejects_negative_boolean_and_malformed_values(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    stdout = json.dumps(
        {
            "type": "result",
            "usage": {
                "input_tokens": -1,
                "output_tokens": True,
                "cache_read_input_tokens": "120",
                "cache_creation_input_tokens": None,
            },
        }
    )

    assert runtime._usage_from_output(stdout) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 120,
        "cache_creation_input_tokens": 0,
    }


def test_claude_stream_telemetry_is_compact_and_content_free(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "claude_code_version": "2.1.207",
                    "model": "claude-opus-test",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg-telemetry-1",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 7,
                            "cache_read_input_tokens": 40,
                            "cache_creation_input_tokens": 3,
                        },
                        "content": [
                            {"type": "thinking", "thinking": "private reasoning"},
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "/private/invoice.pdf"},
                            },
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-07-24T10:11:12.000Z",
                    "tool_use_result": "sensitive invoice content",
                }
            ),
            json.dumps(
                {
                    "type": "api_retry",
                    "retry_delay_ms": 1500,
                    "error_status": 529,
                }
            ),
            "{not valid json",
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "session-telemetry",
                    "duration_ms": 125000,
                    "duration_api_ms": 121000,
                    "num_turns": 8,
                    "is_error": False,
                    "stop_reason": "end_turn",
                    "ttft_ms": 2424,
                    "ttft_stream_ms": 1411,
                    "time_to_request_ms": 24,
                    "total_cost_usd": 3.25,
                    "usage": {
                        "input_tokens": 58,
                        "output_tokens": 100,
                        "cache_read_input_tokens": 800,
                        "cache_creation_input_tokens": 75,
                        "service_tier": "standard",
                        "speed": "standard",
                    },
                }
            ),
        ]
    )

    telemetry = runtime._telemetry_from_output(stdout)
    first_timestamp = telemetry.pop("first_timestamp")
    last_timestamp = telemetry.pop("last_timestamp")

    assert telemetry == {
        "schema": "glasshive.claude-run-telemetry.v1",
        "claude_code_version": "2.1.207",
        "model": "claude-opus-test",
        "service_tier": "standard",
        "speed": "standard",
        "result_state": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "duration_ms": 125000,
        "duration_api_ms": 121000,
        "duration_non_api_ms": 4000,
        "ttft_ms": 2424,
        "ttft_stream_ms": 1411,
        "time_to_request_ms": 24,
        "num_turns": 8,
        "api_retry_count": 1,
        "last_api_retry_event_sequence": 4,
        "api_retry_delay_ms": 1500,
        "api_retry_statuses": ["529"],
        "tool_call_count": 1,
        "tool_call_counts": {"Read": 1},
        "event_count": 5,
        "malformed_line_count": 1,
        "oversized_line_count": 0,
        "stream_input_tokens": 12,
        "stream_output_tokens": 7,
        "stream_cache_read_input_tokens": 40,
        "stream_cache_creation_input_tokens": 3,
        "total_cost_usd": 3.25,
    }
    assert datetime.fromisoformat(first_timestamp)
    assert datetime.fromisoformat(last_timestamp)
    encoded = json.dumps(telemetry)
    assert "private reasoning" not in encoded
    assert "invoice.pdf" not in encoded
    assert "sensitive invoice content" not in encoded


def test_claude_stream_usage_is_counted_once_per_message_id_and_error_subtype_is_preserved(
    tmp_path,
):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    assistant = {
        "type": "assistant",
        "message": {
            "id": "msg-duplicate",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 4,
            },
            "content": [],
        },
    }
    stdout = "\n".join(
        [
            json.dumps(assistant),
            json.dumps(assistant),
            json.dumps({"type": "result", "subtype": "error_max_turns"}),
        ]
    )

    telemetry = runtime._telemetry_from_output(stdout)

    assert telemetry["stream_input_tokens"] == 20
    assert telemetry["stream_output_tokens"] == 8
    assert telemetry["stream_cache_read_input_tokens"] == 100
    assert telemetry["stream_cache_creation_input_tokens"] == 4
    assert telemetry["result_state"] == "error_max_turns"
    assert telemetry["is_error"] is True


def test_claude_live_telemetry_reads_the_complete_active_run(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_live_telemetry",
        "name": "Invoice Worker",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_live_telemetry"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    full_stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-opus-test",
                    "claude_code_version": "2.1.207",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "a"}}
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "true"}}
                        ]
                    },
                }
            ),
        ]
    )
    (run_root / "stdout.log").write_text(full_stream + "\n", encoding="utf-8")

    telemetry = runtime.live_telemetry(
        worker,
        full_stream.splitlines()[-1],
        run_id=run_id,
    )

    assert telemetry["telemetry_scope"] == "full_active_run_incremental"
    assert telemetry["run_id"] == run_id
    assert telemetry["event_count"] == 3
    assert telemetry["tool_call_count"] == 2
    assert telemetry["tool_call_counts"] == {"Bash": 1, "Read": 1}
    assert telemetry["last_stream_activity_at"] == telemetry["last_progress_at"]
    assert telemetry["seconds_since_stream_activity"] == telemetry["seconds_since_progress"]


def test_claude_live_telemetry_locates_the_last_retry_in_the_event_stream(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_retry_sequence",
        "name": "Invoice Worker",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_retry_sequence"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({"type": "api_retry", "error_status": 500}),
                json.dumps({"type": "assistant", "message": {"content": []}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    telemetry = runtime.live_telemetry(worker, "", run_id=run_id)

    assert telemetry["api_retry_count"] == 1
    assert telemetry["last_api_retry_event_sequence"] == 2
    assert telemetry["event_count"] == 3


def test_claude_live_telemetry_consumes_only_complete_appended_lines(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_incremental_telemetry",
        "name": "Invoice Worker",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_incremental_telemetry"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    init_line = json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-test"})
    assistant_line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
        }
    )
    split_at = len(assistant_line) // 2
    stdout_path.write_text(init_line + "\n" + assistant_line[:split_at], encoding="utf-8")

    first = runtime.live_telemetry(worker, "", run_id=run_id)

    assert first["event_count"] == 1
    assert first["malformed_line_count"] == 0
    assert first["partial_line_present"] is True
    assert first["sample_sequence"] == 1

    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write(assistant_line[split_at:] + "\n{not-json}\n")
    second = runtime.live_telemetry(worker, "", run_id=run_id)
    third = runtime.live_telemetry(worker, "", run_id=run_id)

    assert second["event_count"] == 2
    assert second["tool_call_counts"] == {"Read": 1}
    assert second["malformed_line_count"] == 1
    assert second["partial_line_present"] is False
    assert second["parsed_bytes"] == second["log_bytes"]
    assert third["event_count"] == second["event_count"]
    assert third["malformed_line_count"] == second["malformed_line_count"]
    assert third["sample_sequence"] == 3


def test_claude_live_telemetry_deduplicates_tool_calls_by_id(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_deduplicated_tools",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_deduplicated_tools"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    repeated = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_123", "name": "Read", "input": {}}
                ]
            },
        }
    )
    (run_root / "stdout.log").write_text(repeated + "\n" + repeated + "\n")

    telemetry = runtime.live_telemetry(worker, "", run_id=run_id)

    assert telemetry["event_count"] == 2
    assert telemetry["tool_call_count"] == 1
    assert telemetry["tool_call_counts"] == {"Read": 1}


def test_claude_live_telemetry_does_not_substitute_console_tail_for_missing_run(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_missing_run", "profile": "claude-code"}
    runtime._ensure_dirs(worker["worker_id"])

    telemetry = runtime.live_telemetry(
        worker,
        json.dumps({"type": "assistant", "message": {"content": []}}),
        run_id="run_missing",
    )

    assert telemetry == {
        "schema": "glasshive.claude-run-telemetry.v1",
        "run_id": "run_missing",
        "telemetry_scope": "active_run_unavailable",
    }


def test_claude_live_telemetry_bounds_an_unterminated_oversized_record(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_oversized_telemetry",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_oversized"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    stdout_path.write_bytes(b"x" * (5 * 1024 * 1024))

    first = runtime.live_telemetry(worker, "", run_id=run_id)
    cached = runtime._live_telemetry_cache[(worker["worker_id"], run_id)]

    assert first["malformed_line_count"] == 1
    assert first["oversized_line_count"] == 1
    assert first["partial_line_present"] is True
    assert len(cached["partial"]) == 0
    assert cached["discarding_oversized_line"] is True

    with stdout_path.open("ab") as handle:
        handle.write(
            b"\n"
            + json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-opus-test"}
            ).encode()
            + b"\n"
        )
    second = runtime.live_telemetry(worker, "", run_id=run_id)

    assert second["event_count"] == 1
    assert second["malformed_line_count"] == 1
    assert second["oversized_line_count"] == 1
    assert second["partial_line_present"] is False


def test_active_run_terminal_status_is_atomic_and_cannot_be_downgraded_by_heartbeat(tmp_path):
    status_path = tmp_path / "active-run.json"
    worker = {
        "worker_id": "wrk_status_race",
        "profile": "claude-code",
        "execution_mode": "docker",
    }
    arguments = {
        "path": status_path,
        "worker": worker,
        "run_id": "run_status_race",
        "runtime_name": "claude-code",
        "model": "claude-opus-test",
        "transcript_paths": {},
        "started_at": "2026-07-24T12:00:00Z",
        "process_pid": 123,
        "timeout_seconds": 30.0,
    }
    profile_runtime_module._write_active_run_status(state="running", **arguments)

    start = threading.Event()

    def heartbeat_writer():
        start.wait()
        for _ in range(200):
            profile_runtime_module._write_active_run_status(state="running", **arguments)

    heartbeat = threading.Thread(target=heartbeat_writer)
    heartbeat.start()
    start.set()
    profile_runtime_module._write_active_run_status(
        state="timeout",
        stop_reason="timeout",
        **arguments,
    )
    heartbeat.join()
    profile_runtime_module._write_active_run_status(state="running", **arguments)

    status = json.loads(status_path.read_text())
    assert status["run_id"] == "run_status_race"
    assert status["state"] == "timeout"
    assert status["stop_reason"] == "timeout"


def test_persisted_run_telemetry_is_atomic_and_content_allowlisted(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_safe_telemetry", "profile": "claude-code"}
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_safe"
    path = runtime._run_root(worker["worker_id"], run_id) / "telemetry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "event_count": 7,
                "model": "SECRET-INVOICE-CONTENT",
                "stop_reason": "customer-name",
                "prompt": "private invoice line",
            }
        )
    )

    assert runtime.run_telemetry(worker, run_id) == {
        "schema": "glasshive.claude-run-telemetry.v1",
        "run_id": run_id,
        "event_count": 7,
    }

    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    telemetry = runtime._record_run_telemetry(
        worker["worker_id"],
        "run_atomic_failure",
        json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-test"}),
    )
    assert telemetry["run_id"] == "run_atomic_failure"


def test_claude_failed_stream_never_promotes_transcript_content_to_public_error_fields(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_failed_claude_stream",
        "name": "Invoice Worker",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_failed_claude_stream"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    sensitive = "INVOICE 999999 PRIVATE-LINE-CONTENT"
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": sensitive}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error",
                        "is_error": True,
                        "error_status": 529,
                        "result": "API Error: 529 Overloaded",
                    }
                ),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_response_failed"
    assert sensitive not in recovered["error_text"]
    assert sensitive not in recovered["failure_diagnostic_summary"]
    assert "Overloaded" not in recovered["failure_diagnostic_summary"]
    assert recovered["telemetry"]["api_retry_statuses"] == []


def test_claude_run_telemetry_is_recorded_and_read_back(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_telemetry"
    run_id = "run_telemetry"
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "error",
            "duration_ms": 4000,
            "duration_api_ms": 3900,
            "num_turns": 2,
            "is_error": True,
            "stop_reason": "max_tokens",
        }
    )

    recorded = runtime._record_run_telemetry(worker_id, run_id, stdout)

    assert recorded["result_state"] == "error"
    assert recorded["is_error"] is True
    assert runtime.run_telemetry({"worker_id": worker_id}, run_id) == recorded
    telemetry_path = runtime._run_root(worker_id, run_id) / "telemetry.json"
    assert telemetry_path.stat().st_mode & 0o777 == 0o600


def test_workspace_claude_command_honors_per_run_max_effort(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude_effort",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-sonnet-test",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "max"}}),
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert command[command.index("--effort") + 1] == "max"


def test_workspace_claude_command_honors_per_run_xhigh_effort(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude_xhigh",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-opus-5",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "xhigh"}}),
    }

    command, _ = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))

    assert command[command.index("--effort") + 1] == "xhigh"


def test_workspace_claude_max_effort_preflight_requires_effort_support(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    ClaudeCodeRuntime._workspace_effort_support_cache.clear()
    monkeypatch.setattr(runtime.sandbox, "_ensure_image", lambda: None)
    monkeypatch.setattr(
        runtime.sandbox,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode=0, stdout="Usage: claude [options]\n", stderr=""),
    )
    worker = {
        "worker_id": "wrk_workspace_claude_effort",
        "profile": "claude-code",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "max"}}),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="--effort"):
        runtime._preflight_workspace_effort_support(worker)


def test_workspace_claude_max_effort_preflight_accepts_effort_support(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    ClaudeCodeRuntime._workspace_effort_support_cache.clear()
    calls: list[object] = []
    monkeypatch.setattr(runtime.sandbox, "_ensure_image", lambda: calls.append("image"))
    monkeypatch.setattr(
        runtime.sandbox,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="Usage: claude [options] --effort <level> (low, medium, high, xhigh, max)\n",
            stderr="",
        ),
    )
    worker = {
        "worker_id": "wrk_workspace_claude_effort",
        "profile": "claude-code",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "max"}}),
    }

    runtime._preflight_workspace_effort_support(worker)
    runtime._preflight_workspace_effort_support(worker)

    assert calls == ["image"]


def test_workspace_claude_xhigh_effort_preflight_rejects_older_effort_contract(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    ClaudeCodeRuntime._workspace_effort_support_cache.clear()
    monkeypatch.setattr(runtime.sandbox, "_ensure_image", lambda: None)
    monkeypatch.setattr(
        runtime.sandbox,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="Usage: claude [options] --effort <level> (low, medium, high, max)\n",
            stderr="",
        ),
    )
    worker = {
        "worker_id": "wrk_workspace_claude_xhigh_unsupported",
        "profile": "claude-code",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "xhigh"}}),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="xhigh"):
        runtime._preflight_workspace_effort_support(worker)


def test_host_claude_command_enables_chrome_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --effort --chrome'; exit 0; fi\n"
        "echo '2.1.223 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(fake_claude)
    monkeypatch.delenv("WPR_CLAUDE_CODE_ENABLE_CHROME", raising=False)
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    worker = {
        "worker_id": "wrk_host_claude",
        "name": "Main Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-opus-5",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime._host_runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert "--chrome" in command
    assert command[command.index("--effort") + 1] == "max"
    assert "do the work" not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "do the work", info)
    assert stdin_text and stdin_text.startswith("do the work")
    assert "FINAL REPORT:" in stdin_text


def test_host_claude_command_honors_xhigh_effort(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --effort <level> (low, medium, high, xhigh, max)'; exit 0; fi\n"
        "echo '2.1.207 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(fake_claude)
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "xhigh")
    worker = {
        "worker_id": "wrk_host_claude_xhigh",
        "name": "Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-opus-5",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    command, _ = runtime._build_command(worker, "do the work", runtime._host_runtime_info(worker))

    assert command[command.index("--effort") + 1] == "xhigh"


def test_host_claude_xhigh_effort_rejects_older_effort_contract(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --effort <level> (low, medium, high, max)'; exit 0; fi\n"
        "echo '2.1.223 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(fake_claude)
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "xhigh")
    worker = {
        "worker_id": "wrk_host_claude_xhigh_unsupported",
        "name": "Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-opus-5",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="xhigh"):
        runtime._build_command(worker, "do the work", runtime._host_runtime_info(worker))


def test_host_claude_chrome_can_be_explicitly_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "claude"
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    worker = {
        "worker_id": "wrk_host_claude_no_chrome",
        "name": "Main Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-sonnet-test",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime._host_runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert "--chrome" not in command


def test_host_cli_runtime_honors_one_configured_mission_slot_per_family(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "1")
    first = {
        "worker_id": "wrk_host_one",
        "name": "First Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    second = {
        "worker_id": "wrk_host_two",
        "name": "Second Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    runtime._acquire_host_slot(first)
    try:
        with pytest.raises(RuntimeErrorBase, match="mission lane is at capacity"):
            runtime._acquire_host_slot(second)
    finally:
        runtime._release_host_slot(first["worker_id"])

    runtime._acquire_host_slot(second)
    runtime._release_host_slot(second["worker_id"])


def test_host_cli_runtime_does_not_trust_conversation_mode_from_bundle_alone(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "1")
    mission = {
        "worker_id": "wrk_trusted_mission",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    untrusted_conversation_claim = {
        "worker_id": "wrk_untrusted_conversation_claim",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    trusted_conversation = {
        **untrusted_conversation_claim,
        "worker_id": "wrk_trusted_conversation",
        "trusted_run_lane": "conversation",
    }

    assert runtime._host_capacity_lane(untrusted_conversation_claim) == "mission"
    runtime._acquire_host_slot(mission)
    try:
        with pytest.raises(RuntimeErrorBase, match="mission lane is at capacity"):
            runtime._acquire_host_slot(untrusted_conversation_claim)
        runtime._acquire_host_slot(trusted_conversation)
        runtime._release_host_slot(trusted_conversation["worker_id"])
    finally:
        runtime._release_host_slot(mission["worker_id"])


def test_host_cli_runtime_reserves_a_separate_interactive_conversation_lane(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    mission = {
        "worker_id": "wrk_mission_lane",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    conversation = {
        "worker_id": "wrk_conversation_lane",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    second_conversation = {
        **conversation,
        "worker_id": "wrk_conversation_lane_two",
    }
    third_conversation = {
        **conversation,
        "worker_id": "wrk_conversation_lane_three",
    }

    runtime._acquire_host_slot(mission)
    runtime._acquire_host_slot(conversation)
    runtime._acquire_host_slot(second_conversation)
    try:
        with pytest.raises(RuntimeErrorBase, match="conversation lane is at capacity"):
            runtime._acquire_host_slot(third_conversation)
    finally:
        runtime._release_host_slot(second_conversation["worker_id"])
        runtime._release_host_slot(conversation["worker_id"])
        runtime._release_host_slot(mission["worker_id"])


def test_host_cli_runtime_has_no_default_hard_run_timeout(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.delenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_HOST_RUN_TIMEOUT_SEC", raising=False)

    assert runtime._host_run_timeout_sec() is None


@pytest.mark.parametrize("value", ["0", "none", "off", "false", "disabled", "-1"])
def test_host_cli_runtime_timeout_can_be_disabled_explicitly(tmp_path, monkeypatch, value):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", value)

    assert runtime._host_run_timeout_sec() is None


def test_host_cli_runtime_uses_configured_timeout_when_set(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", "900")

    assert runtime._host_run_timeout_sec() == 900


def test_host_cli_runtime_honors_caller_timeout_when_no_env_override(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.delenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("GLASSHIVE_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_RUN_TIMEOUT_SEC", raising=False)

    assert runtime._host_run_timeout_sec(42) == 42


def test_declared_long_runtime_bypasses_only_the_ordinary_maximum(
    tmp_path, monkeypatch
):
    host = HostCodexCliRuntime(base_dir=str(tmp_path / "host"))
    docker = CodexCliRuntime(base_dir=str(tmp_path / "docker"))
    monkeypatch.setenv("GLASSHIVE_MAX_RUN_DURATION_S", "10")
    monkeypatch.delenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("GLASSHIVE_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_RUN_TIMEOUT_SEC", raising=False)

    assert host._host_run_timeout_sec(declared_long=True) is None
    assert docker._run_timeout_sec(declared_long=True) is None
    assert openclaw_runtime_module._run_timeout_sec(declared_long=True) is None
    assert host._host_run_timeout_sec(42, declared_long=True) == 42
    assert docker._run_timeout_sec(42, declared_long=True) == 42
    assert openclaw_runtime_module._run_timeout_sec(42, declared_long=True) == 42

    monkeypatch.setenv("GLASSHIVE_RUN_TIMEOUT_SEC", "7")
    assert host._host_run_timeout_sec(declared_long=True) == 7
    assert docker._run_timeout_sec(declared_long=True) == 7
    assert openclaw_runtime_module._run_timeout_sec(declared_long=True) == 7


def test_docker_cli_runtime_accepts_no_default_run_timeout(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    exit_path = tmp_path / "exit_code"
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]

    def finish_run():
        time.sleep(0.05)
        exit_path.write_text("0")

    thread = threading.Thread(target=finish_run)
    thread.start()
    try:
        assert runtime._wait_for_exit_code("wrk_test", exit_path, None) == 0
    finally:
        thread.join(timeout=1)


def test_docker_cli_runtime_waits_for_precreated_exit_marker_to_be_written(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    exit_path = tmp_path / "exit_code"
    exit_path.touch()
    exit_path.chmod(0o600)
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]

    def finish_run():
        time.sleep(0.05)
        exit_path.write_text("7")

    thread = threading.Thread(target=finish_run)
    thread.start()
    try:
        assert runtime._wait_for_exit_code("wrk_test", exit_path, None) == 7
    finally:
        thread.join(timeout=1)


def test_docker_cli_runtime_completion_discovery_ignores_empty_exit_marker(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_empty_exit"
    run_id = "run_empty_exit"
    run_root = runtime._run_root(worker_id, run_id)
    run_root.mkdir(parents=True)
    exit_path = run_root / "exit_code"
    exit_path.touch()
    exit_path.chmod(0o600)

    assert runtime._latest_completed_run_payload(worker_id, run_id=run_id) is None
    runtime._write_active_session(worker_id, runtime._run_payload(worker_id, run_id) or {})
    assert runtime._latest_completed_run_payload(worker_id, run_id=run_id) is None

    exit_path.write_text("0")
    completed = runtime._latest_completed_run_payload(worker_id, run_id=run_id)
    assert completed is not None
    assert completed["run_id"] == run_id


def test_docker_cli_runtime_throttles_wait_loop_inspect(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    exit_path = tmp_path / "exit_code"
    inspect_calls = 0
    monkeypatch.setenv("WPR_RUN_WAIT_INSPECT_INTERVAL_SEC", "60")

    def inspect_once(worker_id):
        nonlocal inspect_calls
        inspect_calls += 1
        return None

    runtime.sandbox.inspect = inspect_once  # type: ignore[method-assign]

    def finish_run():
        time.sleep(0.2)
        exit_path.write_text("0")

    thread = threading.Thread(target=finish_run)
    thread.start()
    try:
        assert runtime._wait_for_exit_code("wrk_test", exit_path, None) == 0
    finally:
        thread.join(timeout=1)
    assert inspect_calls == 1


def test_docker_cli_runtime_clears_active_session_only_after_confirmed_stop(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_stop_meta"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "job-run_stop_meta",
            "run_id": "run_stop_meta",
            "stdout_path": str(tmp_path / "stdout.log"),
            "stderr_path": str(tmp_path / "stderr.log"),
            "exit_path": str(tmp_path / "exit_code"),
        },
    )
    calls: list[tuple[str, str]] = []
    runtime.sandbox.stop_screen_session = lambda worker_id, runtime_name, session_name, **kwargs: calls.append(("screen", session_name))  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = lambda worker_id, runtime_name, run_id, **kwargs: calls.append(("terminate", run_id))  # type: ignore[method-assign]

    confirmed = runtime._stop_active_process(
        worker_id, worker={"worker_id": worker_id}
    )

    assert calls == [("screen", "job-run_stop_meta"), ("terminate", "run_stop_meta")]
    assert not runtime._active_session_meta_path(worker_id).exists()


@pytest.mark.parametrize("closing_state", ["terminating", "termination_failed"])
def test_docker_cli_close_idle_worker_skips_stale_terminal_session(tmp_path, closing_state):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_idle_close"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {"session_name": "job-run_finished", "run_id": "run_finished"},
    )
    runtime._active_pid = lambda _worker_id: None  # type: ignore[method-assign]
    runtime.sandbox.stop_screen_session = lambda *_args, **_kwargs: pytest.fail(
        "An idle close must not probe a stale terminal session"
    )  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = lambda *_args, **_kwargs: pytest.fail(
        "An idle close must let exact container teardown stop old processes"
    )  # type: ignore[method-assign]
    removed: list[str] = []
    runtime.sandbox.terminate = lambda worker_id, **_kwargs: removed.append(worker_id)  # type: ignore[method-assign]

    runtime.terminate_worker({"worker_id": worker_id, "state": closing_state})

    assert removed == [worker_id]
    assert not runtime._active_session_meta_path(worker_id).exists()


def test_docker_cli_runtime_propagates_stop_failure_and_keeps_active_session(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_stop_failure"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "job-run_stop_failure",
            "run_id": "run_stop_failure",
            "stdout_path": str(tmp_path / "stdout.log"),
            "stderr_path": str(tmp_path / "stderr.log"),
            "exit_path": str(tmp_path / "exit_code"),
        },
    )
    terminate_calls: list[str] = []

    def fail_screen(*args, **kwargs):
        raise RuntimeError("screen session remained alive")

    runtime.sandbox.stop_screen_session = fail_screen  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, run_id, **kwargs: terminate_calls.append(run_id)
    )

    with pytest.raises(RuntimeError, match="screen session remained alive"):
        runtime._stop_active_process(worker_id, worker={"worker_id": worker_id})

    assert terminate_calls == ["run_stop_failure"]
    assert runtime._active_session_meta_path(worker_id).exists()


def test_docker_cli_runtime_uses_configured_run_timeout(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_RUN_TIMEOUT_SEC", "1200")

    assert runtime._run_timeout_sec() == 1200


def test_parallel_clean_room_container_env_rejects_ambient_provider_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-ambient-anthropic-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-ambient-oauth-secret")
    monkeypatch.setenv("PORTKEY_API_KEY", "synthetic-ambient-portkey-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient-proxy.example:8888")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.example:8888")
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_clean_room_env",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
    }

    env = runtime._container_env_for_worker(
        worker,
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "PORTKEY_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    )

    assert env["HTTP_PROXY"] == "http://provider-egress:8080"
    assert env["HTTPS_PROXY"] == "http://provider-egress:8080"
    assert env["NO_PROXY"] == (
        "provider-egress,host.docker.internal,localhost,127.0.0.1"
    )
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "PORTKEY_API_KEY" not in env
    assert "GLASSHIVE_CAPABILITY_BROKER_TOKEN" not in env


def test_parallel_clean_room_codex_uses_run_grant_for_the_attested_provider_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
        "WPR_CODEX_CLI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_clean_room_codex_provider",
        "profile": "codex-cli",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
    }
    info = SimpleNamespace(
        runtime="codex-cli",
        model="synthetic-model",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        session_key=None,
    )

    command, env = runtime._build_command(worker, "Do it.", info)

    assert (
        'model_providers.glasshive_openai_compatible.base_url="http://provider-egress:8080/openai/v1"'
        in command
    )
    assert (
        'model_providers.glasshive_openai_compatible.env_key="GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        in command
    )
    assert "synthetic-run-grant" not in command
    assert "synthetic-run-grant" not in env.values()
    assert "OPENAI_API_KEY" not in env


def test_parallel_clean_room_run_rejects_replaced_generation_before_authority_projection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_replaced_after_grant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
        "_run_local_capability_binding": {
            "containerGenerationId": "a" * 64,
        },
    }

    class ReplacementSandbox:
        container_name = "wpr-replaced-after-grant"
        container_id = "b" * 64
        pid = 123
        state = "running"

    runtime.sandbox.ensure_ready = lambda *_args, **_kwargs: ReplacementSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect_fresh = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="present", sandbox=ReplacementSandbox()
    )

    with pytest.raises(
        RuntimeErrorBase,
        match="capability grant does not match the exact sandbox generation",
    ):
        runtime.run_task(worker, "Do it.", run_id="run-replaced-after-grant")


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_parallel_clean_room_ready_check_never_uses_cached_fast_sandbox(
    tmp_path, runtime_type
):
    runtime = runtime_type(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_clean_room_fresh_boundary",
        "state": "running",
        "profile": "codex-cli",
        "container_id": "cached-container-generation",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }
    cached = SimpleNamespace(pid=9911, state="running")
    runtime.sandbox.fast_sandbox_from_worker = lambda _worker: cached  # type: ignore[method-assign]
    runtime.sandbox.ensure_ready = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("strict clean-room boundary unavailable")
    )
    if isinstance(runtime, OpenClawWorkstationRuntime):
        # The reviewed-image check inspects and, when absent, builds the real image;
        # this test is about the sandbox boundary, so it must reach no Docker daemon.
        runtime.sandbox.require_reviewed_openclaw_image = lambda: None  # type: ignore[method-assign]
        runtime._write_gateway_config = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        runtime._start_openclaw_gateway = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="strict clean-room boundary unavailable"):
        runtime.ensure_worker_ready(worker)


def test_parallel_clean_room_run_exits_after_secret_scrub_without_takeover_shell(
    tmp_path, monkeypatch
):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "codex-cli"
        worker_root_name = "parallel_clean_room_capture"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], self._container_env_for_worker(
                worker, "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"
            )

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-provider-secret")
    monkeypatch.setenv(
        "CLAUDE_CODE_OAUTH_TOKEN", "synthetic-ambient-subscription-secret"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = "run_clean_room_exit"
    worker = {
        "worker_id": "wrk_clean_room_exit",
        "_provider_response_deadline_at": "2099-01-01T00:00:00+00:00",
        "name": "Clean Room Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
        "_run_local_capability_binding": {
            "containerGenerationId": "d" * 64,
        },
    }
    stale_run_root = runtime._run_root(worker["worker_id"], run_id)
    stale_run_root.mkdir(parents=True, exist_ok=True)
    (stale_run_root / "exit_code").write_text("1")

    class FakeSandbox:
        container_name = "wpr-clean-room-exit"
        container_id = "d" * 64
        pid = 123
        state = "running"

    runtime.sandbox.ensure_ready = lambda *_args, **_kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda *_args, **_kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect_fresh = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="present", sandbox=FakeSandbox()
    )
    runtime.sandbox.list_screen_sessions = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    projected: list[dict] = []
    cleared: list[dict] = []

    def project_run_secrets(worker_id, **kwargs):
        projected.append({"worker_id": worker_id, **kwargs})
        return {
            "env_file": f"/run/glasshive/{run_id}/secret-runtime.env",
            "keys_file": f"/run/glasshive/{run_id}/secret-runtime.keys",
        }

    runtime.sandbox.project_parallel_clean_room_run_secrets = project_run_secrets  # type: ignore[method-assign]
    runtime.sandbox.clear_parallel_clean_room_run_secrets = (  # type: ignore[method-assign]
        lambda worker_id, **kwargs: cleared.append(
            {"worker_id": worker_id, **kwargs}
        )
    )

    def fake_start_screen_session(
        worker_id, runtime_name, session_name, command, *, env=None, worker=None
    ):
        run_root = runtime._run_root(worker_id, run_id)
        assert (run_root / "exit_code").read_text() == ""
        script = (run_root / "run.sh").read_text()
        assert subprocess.run(
            ["bash", "-n", str(run_root / "run.sh")],
            capture_output=True, text=True, check=False,
        ).returncode == 0
        assert "exec bash --noprofile --norc" not in script
        assert "Interactive shell remains open for takeover" not in script
        assert "credential-free session exiting" in script
        assert 'exit "$status"' in script
        assert (
            'export OPENAI_API_KEY="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
            in script
        )
        assert (
            'export ANTHROPIC_AUTH_TOKEN="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
            in script
        )
        assert "synthetic-run-grant" not in script
        assert f"/run/glasshive/{run_id}/secret-runtime.env" in script
        assert '$HOME/.glasshive/secret-runtime.env' not in script
        assert "scrub_run_secrets()" in script
        assert 'abort_run() { scrub_run_secrets; write_exit "${1:-130}"' in script
        deadline_line = next(
            line for line in script.splitlines()
            if "2099-01-01T00:00:00+00:00" in line
        )
        assert script.index(deadline_line) < script.index("printf ok")
        check_command = deadline_line.split(" || abort_run 75", 1)[0]
        assert subprocess.run(["bash", "-c", check_command], check=False).returncode == 0
        assert subprocess.run(
            ["bash", "-c", check_command.replace("2099-01-01", "2000-01-01")],
            check=False,
        ).returncode == 75
        assert env["HTTP_PROXY"] == "http://provider-egress:8080"
        assert "OPENAI_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        (run_root / "stdout.log").write_text("FINAL REPORT:\nok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(
            ["screen"], returncode=0, stdout="", stderr=""
        )

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *_args, **_kwargs: 4321  # type: ignore[method-assign]

    assert runtime.run_task(worker, "Do it.", run_id=run_id) == "FINAL REPORT:\nok"
    assert projected == [
        {
            "worker_id": "wrk_clean_room_exit",
            "expected_container_id": "d" * 64,
            "run_id": run_id,
            "env": {
                "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
            },
        }
    ]
    assert cleared == [
        {
            "worker_id": "wrk_clean_room_exit",
            "expected_container_id": "d" * 64,
            "run_id": run_id,
        }
    ]


def test_profiled_runtime_prepares_authority_from_fresh_exact_clean_room_generation(
    tmp_path,
):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    sandbox = SimpleNamespace(container_id="a" * 64, state="running")
    calls: list[str] = []
    ensured_workers: list[dict] = []
    fake_sandbox = SimpleNamespace(
        inspect_fresh=lambda worker_id: (
            calls.append(f"inspect:{worker_id}")
            or SimpleNamespace(status="present", sandbox=sandbox)
        ),
        _sandbox_matches_parallel_clean_room_policy=lambda candidate: candidate is sandbox,
    )
    fake_runtime = SimpleNamespace(
        sandbox=fake_sandbox,
        ensure_worker_ready=lambda worker: (
            ensured_workers.append(dict(worker))
            or calls.append(f"ensure:{worker['worker_id']}")
        ),
    )
    runtime._runtime_for_worker = lambda _worker: fake_runtime  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_generation_authority",
        "execution_mode": "docker",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }

    assert runtime.prepare_run_authority_context(worker, run_id="run-exact") == {
        "container_generation_id": "a" * 64
    }
    assert calls == [
        "ensure:wrk_generation_authority",
        "inspect:wrk_generation_authority",
    ]
    assert ensured_workers == [
        {
            **worker,
            "_pre_run_substrate_recreate_allowed": True,
        }
    ]
    assert "_pre_run_substrate_recreate_allowed" not in worker


@pytest.mark.parametrize(
    ("status", "state", "container_id", "matches"),
    [
        ("unavailable", "running", "a" * 64, True),
        ("present", "exited", "a" * 64, True),
        ("present", "running", "not-an-exact-generation", True),
        ("present", "running", "a" * 64, False),
    ],
)
def test_profiled_runtime_refuses_unproven_generation_before_broker_admission(
    tmp_path, status, state, container_id, matches
):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    sandbox = SimpleNamespace(container_id=container_id, state=state)
    fake_runtime = SimpleNamespace(
        sandbox=SimpleNamespace(
            inspect_fresh=lambda _worker_id: SimpleNamespace(
                status=status, sandbox=sandbox
            ),
            _sandbox_matches_parallel_clean_room_policy=lambda _candidate: matches,
        ),
        ensure_worker_ready=lambda _worker: None,
    )
    runtime._runtime_for_worker = lambda _worker: fake_runtime  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_unproven_generation",
        "execution_mode": "docker",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }

    with pytest.raises(RuntimeErrorBase, match="exact mission container generation"):
        runtime.prepare_run_authority_context(worker, run_id="run-unproven")


def test_docker_cli_runtime_description_exposes_desktop_prime_marker(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_describe_prime", "name": "Prime Worker", "profile": "codex-cli"}
    runtime.sandbox.describe = lambda worker_id: {  # type: ignore[method-assign]
        "workspace_dir": str(tmp_path / "workspace"),
        "home_dir": str(tmp_path / "home"),
        "container_name": "wpr-describe-prime",
        "container_id": "cid",
        "state": "running",
        "image": "workers-projects-runtime-workstation:phase1-node22-docs7",
        "view_url": "http://127.0.0.1:7900",
        "view_available": True,
        "view_health": {"healthy": True},
        "novnc_port": 57900,
        "selenium_port": 57901,
        "openclaw_port": 57902,
        "desktop_prime": {"schema": "glasshive.desktop_prime.v1", "status": "launched"},
        "pid": 1234,
    }

    details = runtime.describe_worker(worker)

    assert details["desktop_prime"] == {"schema": "glasshive.desktop_prime.v1", "status": "launched"}


def test_docker_cli_runtime_sources_runtime_and_openclaw_env_files(tmp_path):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    worker = {"worker_id": "wrk_capture", "name": "Capture Worker", "profile": "openclaw-general"}
    run_id = "run_capture"

    class FakeSandbox:
        container_name = "wpr-capture"
        pid = 123

    def fake_ensure_ready(worker, runtime_name, **kwargs):
        assert worker["_glasshive_task_run"] is True
        assert worker["_active_run_id"] == run_id
        return FakeSandbox()

    runtime.sandbox.ensure_ready = fake_ensure_ready  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    writable_repairs: list[list[str]] = []
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: writable_repairs.append(args[2])  # type: ignore[method-assign]

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        script = (run_root / "run.sh").read_text()
        exit_path = run_root / "exit_code"
        assert exit_path.exists()
        assert exit_path.read_bytes() == b""
        assert stat.S_IMODE(exit_path.stat().st_mode) == 0o600
        assert "if [ ! -s /workspace/.wpr-home/.glasshive-runs/run_capture/exit_code ]; then" in script
        assert '$HOME/.glasshive/runtime.env' in script
        assert '$HOME/.wpr-openclaw/openclaw.env' in script
        assert "GLASSHIVE_ACTIVE_RUN_ID=run_capture" in script
        assert "GLASSHIVE_RUN_ID=run_capture" in script
        assert "GLASSHIVE_ACTIVE_WORKER_ID=wrk_capture" in script
        assert "unset " in script
        # The current owner scrubs declared secret keys rather than a fixed provider list.
        scrub = next(line for line in script.splitlines() if line.startswith("scrub_run_secrets()"))
        secret_keys = run_root / "synthetic-secret.keys"
        secret_env = run_root / "synthetic-secret.env"
        secret_keys.write_text("OPENAI_API_KEY\nCLAUDE_CODE_OAUTH_TOKEN\nCUSTOM_BROKER_TOKEN\n")
        secret_env.write_text("synthetic private grant")
        checked = subprocess.run(
            ["bash", "-c", scrub + '\nscrub_run_secrets\n' +
             '[[ -z "${OPENAI_API_KEY+x}${CLAUDE_CODE_OAUTH_TOKEN+x}${CUSTOM_BROKER_TOKEN+x}" ]]'],
            env={**os.environ, "GLASSHIVE_SECRET_ENV_KEYS_FILE": str(secret_keys),
                 "GLASSHIVE_SECRET_ENV_FILE": str(secret_env), "GLASSHIVE_SECRET_ENV_DIR": "",
                 "OPENAI_API_KEY": "synthetic", "CLAUDE_CODE_OAUTH_TOKEN": "synthetic",
                 "CUSTOM_BROKER_TOKEN": "synthetic"}, capture_output=True, text=True,
        )
        assert checked.returncode == 0
        assert not secret_keys.exists() and not secret_env.exists()
        (run_root / "stdout.log").write_text("FINAL REPORT:\nok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 4321  # type: ignore[method-assign]

    assert runtime.run_task(worker, "do it", run_id=run_id) == "FINAL REPORT:\nok"
    assert writable_repairs == [
        [f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"],
        [runtime.sandbox.workspace_mount, f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"]
    ]
    workspace = runtime._workspace_dir(worker["worker_id"])
    active_status = json.loads((workspace / "glasshive-run" / "runs" / run_id / "active-run.json").read_text())
    assert active_status["state"] == "completed"
    assert active_status["runtime"] == "openclaw"
    assert active_status["worker"]["execution_mode"] == ""
    assert active_status["process_pid"] == 4321
    assert active_status["heartbeat_sequence"] >= 1
    assert active_status["transcript_progress"]["files"]["stdout"]["exists"] is True
    assert active_status["transcript_progress"]["files"]["stdout"]["bytes"] > 0
    assert active_status["evidence_path"] == f"glasshive-run/runs/{run_id}/evidence.json"
    active_session_text = runtime._active_session_meta_path(worker["worker_id"]).read_text()
    assert "do it" not in active_session_text
    active_session = json.loads(active_session_text)
    assert active_session["instruction_redacted"] is True
    assert active_session["process_pid"] == 4321


def test_docker_cli_run_writes_timeout_active_run_status(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["sleep", "60"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    recorded_metrics: list[tuple[str, str, str]] = []

    def record_metrics(worker_id, recorded_run_id, stdout):
        recorded_metrics.append((worker_id, recorded_run_id, stdout))
        return {}, {}

    runtime._record_run_metrics = record_metrics  # type: ignore[method-assign]
    worker = {"worker_id": "wrk_docker_timeout", "name": "Timeout Worker", "profile": "openclaw-general"}
    run_id = "run_docker_timeout"

    class FakeSandbox:
        container_name = "wpr-timeout"
        pid = 123
        state = "running"

    runtime.sandbox.ensure_ready = lambda worker, runtime_name, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.stop_screen_session = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 9876  # type: ignore[method-assign]
    monkeypatch.setenv("WPR_RUN_WAIT_INSPECT_INTERVAL_SEC", "60")

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        (run_root / "stdout.log").write_text("Started but still working.\n")
        (run_root / "stderr.log").write_text("")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]

    with pytest.raises(RuntimeErrorBase, match="timed out"):
        runtime.run_task(worker, "Do long work.", timeout_sec=0.01, run_id=run_id)

    active_status = json.loads(
        (runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "active-run.json").read_text()
    )
    assert active_status["state"] == "timeout"
    assert active_status["stop_reason"] == "timeout"
    assert active_status["process_pid"] == 9876
    assert active_status["transcript_progress"]["files"]["stdout"]["exists"] is True
    assert active_status["evidence_path"] == f"glasshive-run/runs/{run_id}/evidence.json"
    assert recorded_metrics == [
        ("wrk_docker_timeout", run_id, "Started but still working.\n")
    ]


def test_docker_cli_runtime_redirects_private_instruction_from_stdin_file(tmp_path):
    class StdinRuntime(BaseCliWorkerRuntime):
        runtime_name = "codex-cli"
        worker_root_name = "stdin_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["fake-cli", "-"], {}

        def _command_stdin_text(self, worker, instruction, info):
            return self._instruction_with_completion_contract(instruction)

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    runtime = StdinRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    worker = {"worker_id": "wrk_docker_stdin", "name": "Stdin Worker", "profile": "codex-cli"}
    run_id = "run_docker_stdin"

    class FakeSandbox:
        container_name = "wpr-capture"
        pid = 123

    runtime.sandbox.ensure_ready = lambda worker, runtime_name, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        script = (run_root / "run.sh").read_text()
        stdin_path = run_root / "instruction.stdin"
        assert (run_root / "stdout.log").is_file()
        assert (run_root / "stderr.log").is_file()
        assert stdin_path.exists()
        assert stdin_path.read_text().startswith("Sensitive docker instruction.")
        assert oct(stdin_path.stat().st_mode & 0o777) == "0o600"
        assert "Sensitive docker instruction" not in script
        assert f"fake-cli - < {runtime.sandbox.home_mount}/.glasshive-runs/{run_id}/instruction.stdin" in script
        (run_root / "stdout.log").write_text("FINAL REPORT:\nok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 2468  # type: ignore[method-assign]

    assert runtime.run_task(worker, "Sensitive docker instruction.", run_id=run_id) == "FINAL REPORT:\nok"


def _install_fake_successful_docker_run(runtime: BaseCliWorkerRuntime, run_id: str, stdout_text: str) -> None:
    class FakeSandbox:
        container_name = "wpr-capture"
        pid = 123

    runtime.sandbox.ensure_ready = lambda worker, runtime_name, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        (run_root / "stdout.log").write_text(stdout_text)
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 1357  # type: ignore[method-assign]


def test_docker_cli_run_fails_when_evidence_contract_fails(tmp_path):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, "Done"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = "run_docker_evidence_fail"
    _install_fake_successful_docker_run(runtime, run_id, "FINAL REPORT:\nDone\n")
    worker = {"worker_id": "wrk_docker_evidence_fail", "name": "Capture Worker", "profile": "openclaw-general"}

    worker["bootstrap_bundle_json"] = json.dumps({"viventium_continuation_contract": {
        "version": 1, "run_id": 'run_docker_evidence_fail',
        "source": {"source_event_id": "event_output", "source_revision": 1, "surface": "web"},
        "output": {"mode": "replace", "required": [], "forbidden": [], "formats": ["pdf"], "forbidden_formats": []},
    }})

    with pytest.raises(RuntimeErrorBase, match="GlassHive evidence check failed"):
        runtime.run_task(worker, "Deliver a PDF report.", run_id=run_id)

    evidence = json.loads((runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "evidence.json").read_text())
    assert evidence["evidence_result"]["status"] == "fail"
    assert evidence["completion_compliance"]["missing_required_artifact_types"] == ["pdf"]


def test_docker_cli_run_fails_when_success_evidence_cannot_be_written(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, "Done"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = "run_docker_evidence_write_fail"
    _install_fake_successful_docker_run(runtime, run_id, "FINAL REPORT:\nDone\n")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.write_run_evidence",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic evidence write failure")),
    )
    worker = {"worker_id": "wrk_docker_evidence_write_fail", "name": "Capture Worker", "profile": "openclaw-general"}

    with pytest.raises(RuntimeErrorBase, match="run evidence was not written"):
        runtime.run_task(worker, "Do the work.", run_id=run_id)


def test_docker_cli_run_preserves_success_when_internal_constraint_diagnostic_cannot_be_written(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, "Done"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = "run_docker_ledger_write_fail"
    _install_fake_successful_docker_run(runtime, run_id, "FINAL REPORT:\nDone\n")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.write_constraint_ledger",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic ledger write failure")),
    )
    worker = {"worker_id": "wrk_docker_ledger_write_fail", "name": "Capture Worker", "profile": "openclaw-general"}

    result = runtime.run_task(worker, "Do the work.", run_id=run_id)
    assert result.startswith("Done")
    assert "constraint diagnostic warning" in result


def test_docker_codex_command_appends_completion_contract(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_contract",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _ = runtime._build_command(worker, "Make the page red.", runtime._runtime_info(worker))

    assert command[-1] == "-"
    assert "Make the page red." not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "Make the page red.", runtime._runtime_info(worker))
    assert stdin_text and stdin_text.startswith("Make the page red.")
    assert "FINAL REPORT:" in stdin_text
    assert "`glasshive-run/` is reserved for internal harness support evidence" in stdin_text
    assert "outside `glasshive-run/`" in stdin_text


def test_docker_codex_stale_resume_replays_same_instruction_as_fresh_task(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(tmp_path / "fake-codex")
    runtime.sandbox.home_mount = str(tmp_path / "container-home")
    worker = {
        "worker_id": "wrk_stale_resume",
        "name": "Reusable Codex Worker",
        "profile": "codex-cli",
        "model": "gpt-test",
        "_active_run_id": "run_stale_resume",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "thread_missing")
    instruction_path = (
        Path(runtime.sandbox.home_mount)
        / ".glasshive-runs"
        / worker["_active_run_id"]
        / "instruction.stdin"
    )
    instruction_path.parent.mkdir(parents=True)
    instruction_path.write_text("Install the official native plugins.\n")
    calls_path = tmp_path / "calls.log"
    runtime.binary = str(tmp_path / "fake-codex")
    Path(runtime.binary).write_text(
        "#!/bin/sh\n"
        "payload=$(cat)\n"
        "printf '%s|%s\\n' \"$*\" \"$payload\" >> \"$FAKE_CODEX_CALLS\"\n"
        "case \" $* \" in\n"
        "  *' exec resume '*)\n"
        "    printf '%s\\n' 'Error: thread/resume: thread/resume failed: no rollout found for thread id thread_missing' >&2\n"
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
        "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"thread_fresh\"}'\n"
        "printf '%s\\n' '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"FINAL REPORT:\\nRecovered.\"}}'\n"
    )
    Path(runtime.binary).chmod(0o755)
    monkeypatch.setenv("FAKE_CODEX_CALLS", str(calls_path))

    command, _env = runtime._build_command(
        worker,
        "Install the official native plugins.",
        runtime._runtime_info(worker),
    )
    completed = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())

    assert completed.returncode == 0
    assert "thread_fresh" in completed.stdout
    assert "no rollout found" not in completed.stderr
    calls = calls_path.read_text().splitlines()
    assert len(calls) == 2
    assert "exec resume --json" in calls[0]
    assert "exec --json" in calls[1]
    assert all("Install the official native plugins." in call for call in calls)


def test_docker_codex_resume_does_not_retry_unrelated_failure(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(tmp_path / "fake-codex")
    runtime.sandbox.home_mount = str(tmp_path / "container-home")
    worker = {
        "worker_id": "wrk_failed_resume",
        "name": "Reusable Codex Worker",
        "profile": "codex-cli",
        "model": "gpt-test",
        "_active_run_id": "run_failed_resume",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "thread_current")
    instruction_path = (
        Path(runtime.sandbox.home_mount)
        / ".glasshive-runs"
        / worker["_active_run_id"]
        / "instruction.stdin"
    )
    instruction_path.parent.mkdir(parents=True)
    instruction_path.write_text("Continue the task.\n")
    calls_path = tmp_path / "calls.log"
    Path(runtime.binary).write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CODEX_CALLS\"\n"
        "printf '%s\\n' 'Error: provider is temporarily unavailable' >&2\n"
        "exit 41\n"
    )
    Path(runtime.binary).chmod(0o755)
    monkeypatch.setenv("FAKE_CODEX_CALLS", str(calls_path))

    command, _env = runtime._build_command(
        worker,
        "Continue the task.",
        runtime._runtime_info(worker),
    )
    completed = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())

    assert completed.returncode == 41
    assert "provider is temporarily unavailable" in completed.stderr
    calls = calls_path.read_text().splitlines()
    assert len(calls) == 1
    assert "exec resume" in calls[0]


def test_docker_claude_command_enables_chrome_and_appends_completion_contract(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.delenv("WPR_CLAUDE_CODE_ENABLE_CHROME", raising=False)
    worker = {
        "worker_id": "wrk_claude_contract",
        "name": "Main Worker",
        "profile": "claude-code",
        "model": "claude-sonnet-4-6",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _ = runtime._build_command(worker, "Make the page red.", runtime._runtime_info(worker))

    assert "--chrome" in command
    assert "Make the page red." not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "Make the page red.", runtime._runtime_info(worker))
    assert stdin_text and stdin_text.startswith("Make the page red.")
    assert "FINAL REPORT:" in stdin_text


def test_docker_claude_stale_resume_replays_same_instruction_as_fresh_task(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(tmp_path / "fake-claude")
    runtime.sandbox.home_mount = str(tmp_path / "container-home")
    worker = {
        "worker_id": "wrk_claude_stale_resume",
        "name": "Reusable Claude Worker",
        "profile": "claude-code",
        "model": "claude-test",
        "_active_run_id": "run_claude_stale_resume",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(
        worker["worker_id"],
        "11111111-1111-4111-8111-111111111111",
    )
    instruction_path = (
        Path(runtime.sandbox.home_mount)
        / ".glasshive-runs"
        / worker["_active_run_id"]
        / "instruction.stdin"
    )
    instruction_path.parent.mkdir(parents=True)
    instruction_path.write_text("Read the saved connector proof.\n")
    calls_path = tmp_path / "claude-calls.log"
    Path(runtime.binary).write_text(
        "#!/bin/sh\n"
        "payload=$(cat)\n"
        "printf '%s|%s\\n' \"$*\" \"$payload\" >> \"$FAKE_CLAUDE_CALLS\"\n"
        "case \" $* \" in\n"
        "  *' --resume '*)\n"
        "    printf '%s\\n' 'No conversation found with session ID: 11111111-1111-4111-8111-111111111111' >&2\n"
        "    printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"error_during_execution\",\"is_error\":true}'\n"
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"session_id\":\"22222222-2222-4222-8222-222222222222\",\"result\":\"FINAL REPORT:\\nRecovered.\"}'\n"
    )
    Path(runtime.binary).chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", str(calls_path))

    command, _env = runtime._build_command(
        worker,
        "Read the saved connector proof.",
        runtime._runtime_info(worker),
    )
    completed = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())

    assert completed.returncode == 0
    assert "22222222-2222-4222-8222-222222222222" in completed.stdout
    assert "No conversation found" not in completed.stderr
    calls = calls_path.read_text().splitlines()
    assert len(calls) == 2
    assert "--resume 11111111-1111-4111-8111-111111111111" in calls[0]
    assert "--resume" not in calls[1]
    assert all("Read the saved connector proof." in call for call in calls)


def test_docker_claude_resume_does_not_retry_unrelated_failure(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(tmp_path / "fake-claude")
    runtime.sandbox.home_mount = str(tmp_path / "container-home")
    worker = {
        "worker_id": "wrk_claude_failed_resume",
        "name": "Reusable Claude Worker",
        "profile": "claude-code",
        "model": "claude-test",
        "_active_run_id": "run_claude_failed_resume",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(
        worker["worker_id"],
        "33333333-3333-4333-8333-333333333333",
    )
    instruction_path = (
        Path(runtime.sandbox.home_mount)
        / ".glasshive-runs"
        / worker["_active_run_id"]
        / "instruction.stdin"
    )
    instruction_path.parent.mkdir(parents=True)
    instruction_path.write_text("Continue the saved task.\n")
    calls_path = tmp_path / "claude-calls.log"
    Path(runtime.binary).write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CLAUDE_CALLS\"\n"
        "printf '%s\\n' 'MCP output quoted: No conversation found with session ID: unrelated' >&2\n"
        "printf '%s\\n' 'Provider is temporarily unavailable' >&2\n"
        "exit 41\n"
    )
    Path(runtime.binary).chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", str(calls_path))

    command, _env = runtime._build_command(
        worker,
        "Continue the saved task.",
        runtime._runtime_info(worker),
    )
    completed = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())

    assert completed.returncode == 41
    assert "Provider is temporarily unavailable" in completed.stderr
    calls = calls_path.read_text().splitlines()
    assert len(calls) == 1
    assert "--resume 33333333-3333-4333-8333-333333333333" in calls[0]


def test_docker_claude_interrupted_resume_flushes_partial_transcript(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(tmp_path / "fake-claude")
    runtime.sandbox.home_mount = str(tmp_path / "container-home")
    worker = {
        "worker_id": "wrk_claude_interrupted_resume",
        "name": "Reusable Claude Worker",
        "profile": "claude-code",
        "model": "claude-test",
        "_active_run_id": "run_claude_interrupted_resume",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(
        worker["worker_id"],
        "44444444-4444-4444-8444-444444444444",
    )
    instruction_path = (
        Path(runtime.sandbox.home_mount)
        / ".glasshive-runs"
        / worker["_active_run_id"]
        / "instruction.stdin"
    )
    instruction_path.parent.mkdir(parents=True)
    instruction_path.write_text("Continue the saved task.\n")
    ready_path = tmp_path / "claude-ready"
    Path(runtime.binary).write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '%s\\n' '{\"type\":\"assistant\",\"text\":\"PARTIAL-PROGRESS-LINE\"}'\n"
        "touch \"$FAKE_CLAUDE_READY\"\n"
        "sleep 30\n"
    )
    Path(runtime.binary).chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_READY", str(ready_path))

    command, _env = runtime._build_command(
        worker,
        "Continue the saved task.",
        runtime._runtime_info(worker),
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
        start_new_session=True,
    )
    for _ in range(100):
        if ready_path.exists():
            break
        time.sleep(0.02)
    assert ready_path.exists()
    os.killpg(process.pid, signal.SIGTERM)
    stdout, _stderr = process.communicate(timeout=5)

    assert process.returncode == 143
    assert "PARTIAL-PROGRESS-LINE" in stdout
    assert not (instruction_path.parent / "claude-resume.stdout").exists()
    assert not (instruction_path.parent / "claude-resume.stderr").exists()


def test_docker_claude_chrome_can_be_explicitly_disabled(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    worker = {
        "worker_id": "wrk_claude_no_chrome",
        "name": "Main Worker",
        "profile": "claude-code",
        "model": "claude-sonnet-4-6",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _ = runtime._build_command(worker, "Make the page red.", runtime._runtime_info(worker))

    assert "--chrome" not in command


def test_docker_codex_command_projects_openai_compatible_provider(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_provider",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")

    command, env = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    assert "--ignore-user-config" not in command
    joined = "\n".join(command)
    assert "--disable" not in command
    for native_feature in ("apps", "multi_agent", "plugins", "browser_use", "computer_use"):
        assert f"--disable\n{native_feature}" not in joined
    assert 'model_provider="glasshive_openai_compatible"' in command
    assert 'model_providers.glasshive_openai_compatible.base_url="https://models.example.test/openai/v1"' in command
    assert 'model_providers.glasshive_openai_compatible.env_key="OPENAI_API_KEY"' in command
    assert "model_providers.glasshive_openai_compatible.supports_websockets=false" in command
    assert 'model_verbosity="medium"' in command
    assert env["OPENAI_BASE_URL"] == "https://models.example.test/openai/v1"


def test_bound_docker_codex_subscription_does_not_use_deployment_provider(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_personal_provider",
        "name": "Personal Codex Worker",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
        "bootstrap_bundle_json": json.dumps(
            {
                "provider_account": {
                    "policy": "personal_required",
                    "account_id": "acct_personal",
                }
            }
        ),
        "_glasshive_provider_account_bound": True,
        "_glasshive_provider_account_env": {
            "CODEX_HOME": "/workspace/.wpr-home/.codex",
        },
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://deployment-gateway.example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-deployment-key")

    command, env = runtime._build_command(
        worker,
        "Use my subscription.",
        runtime._runtime_info(worker),
    )

    joined = "\n".join(command)
    assert 'model_provider="glasshive_openai_compatible"' not in joined
    assert "deployment-gateway.example.test" not in joined
    assert env["CODEX_HOME"] == "/workspace/.wpr-home/.codex"
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env


def test_bound_clean_room_codex_subscription_uses_run_broker_route(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_personal_clean_room",
        "name": "Personal Clean Room Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": "parallel-clean-room-v1",
                "provider_account": {
                    "policy": "personal_required",
                    "account_id": "acct_personal",
                },
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant",
                },
            }
        ),
        "_glasshive_provider_account_bound": True,
        "_glasshive_provider_account_env": {
            "CODEX_HOME": "/workspace/.wpr-home/.codex",
        },
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room")
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://deployment-gateway.example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-deployment-key")

    command, env = runtime._build_command(
        worker,
        "Use my subscription through the run broker.",
        runtime._runtime_info(worker),
    )

    joined = "\n".join(command)
    assert 'model_provider="glasshive_openai_compatible"' in command
    assert (
        'model_providers.glasshive_openai_compatible.base_url="http://provider-egress:8080/openai/v1"'
        in command
    )
    assert (
        'model_providers.glasshive_openai_compatible.env_key="GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        in command
    )
    assert "model_providers.glasshive_openai_compatible.supports_websockets=false" in command
    assert "deployment-gateway.example.test" not in joined
    assert "synthetic-run-grant" not in joined
    assert "synthetic-run-grant" not in env.values()
    assert env["CODEX_HOME"] == "/workspace/.wpr-home/.codex"
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env


def test_parallel_clean_room_run_projects_grant_into_exact_sandbox(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "codex-cli"
        worker_root_name = "parallel_clean_room_capture"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], self._container_env_for_worker(worker)

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = "run_clean_room_grant"
    worker = {
        "worker_id": "wrk_clean_room_grant",
        "name": "Clean Room Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": "parallel-clean-room-v1",
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
        "_run_local_capability_binding": {
            "containerGenerationId": "d" * 64,
        },
    }

    class FakeSandbox:
        container_name = "wpr-clean-room-grant"
        container_id = "d" * 64
        pid = 123
        state = "running"

    runtime.sandbox.ensure_ready = lambda *_args, **_kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda *_args, **_kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect_fresh = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="present", sandbox=FakeSandbox()
    )
    runtime.sandbox.list_screen_sessions = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    projected: list[dict] = []
    cleared: list[dict] = []

    def project_run_secrets(worker_id, **kwargs):
        projected.append({"worker_id": worker_id, **kwargs})
        return {
            "env_file": f"/run/glasshive/{run_id}/secret-runtime.env",
            "keys_file": f"/run/glasshive/{run_id}/secret-runtime.keys",
        }

    runtime.sandbox.project_parallel_clean_room_run_secrets = project_run_secrets  # type: ignore[method-assign]
    runtime.sandbox.clear_parallel_clean_room_run_secrets = (  # type: ignore[method-assign]
        lambda worker_id, **kwargs: cleared.append(
            {"worker_id": worker_id, **kwargs}
        )
    )

    def fake_start_screen_session(
        worker_id, runtime_name, session_name, command, *, env=None, worker=None
    ):
        script = runtime._attempt_run_root(worker_id, run_id, "") / "run.sh"
        script_text = script.read_text()
        assert f"/run/glasshive/{run_id}/secret-runtime.env" in script_text
        assert ': "${GLASSHIVE_CAPABILITY_BROKER_TOKEN:?missing run capability grant}"' in script_text
        assert "synthetic-run-grant" not in script_text
        assert "credential-free session exiting" in script_text
        (script.parent / "stdout.log").write_text("FINAL REPORT:\nok")
        (script.parent / "stderr.log").write_text("")
        (script.parent / "exit_code").write_text("0")
        return subprocess.CompletedProcess(
            ["screen"], returncode=0, stdout="", stderr=""
        )

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *_args, **_kwargs: 4321  # type: ignore[method-assign]

    assert runtime.run_task(worker, "Do it.", run_id=run_id) == "FINAL REPORT:\nok"
    assert projected == [
        {
            "worker_id": "wrk_clean_room_grant",
            "expected_container_id": "d" * 64,
            "run_id": run_id,
            "env": {
                "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
            },
        }
    ]
    assert cleared == [
        {
            "worker_id": "wrk_clean_room_grant",
            "expected_container_id": "d" * 64,
            "run_id": run_id,
        }
    ]


def test_codex_cli_provider_can_explicitly_lock_down_user_config_and_native_features(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_locked_down_provider",
        "name": "Locked Down Worker",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_IGNORE_USER_CONFIG", "1")
    monkeypatch.setenv("WPR_CODEX_CLI_DISABLE_FEATURES", "browser_use,computer_use")

    command, _ = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    joined = "\n".join(command)
    assert "--ignore-user-config" in command
    assert "--disable\nbrowser_use" in joined
    assert "--disable\ncomputer_use" in joined


def test_host_codex_native_web_access_policy_disables_unbrokered_search_on_native_route(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_locked",
        "name": "Locked Native Web Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "inherit")

    command, _ = runtime._build_command(
        worker,
        "Use the assigned brokered research capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "danger-full-access" not in command
    assert 'sandbox_mode="workspace-write"' in command
    assert 'approval_policy="never"' in command
    assert "sandbox_workspace_write.network_access=false" in command
    joined = "\n".join(command)
    for native_escape in (
        "apps",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "in_app_browser",
        "plugins",
        "remote_plugin",
    ):
        assert f"--disable\n{native_escape}" in joined
    assert "--disable\nweb_search" not in "\n".join(command)


def test_host_codex_native_web_lockdown_keeps_only_declared_broker_mcp(
    tmp_path, monkeypatch
):
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        'notify = ["synthetic-notifier"]\n\n'
        "[apps.synthetic]\nenabled = true\n\n"
        "[mcp_servers.node_repl]\ncommand = \"node-repl\"\n\n"
        "[plugins.\"browser@openai-bundled\"]\nenabled = true\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        'url = "http://127.0.0.1:8180/api/viventium/glasshive/capabilities/mcp"'
    )

    assert "mcp_servers.glasshive-user-capabilities" in config
    assert "mcp_servers.node_repl" not in config
    assert "synthetic-notifier" not in config
    assert "apps.synthetic" not in config
    assert "plugins" not in config


def test_host_codex_native_web_lockdown_launches_declared_loopback_broker_transport(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    life = tmp_path / "Life"
    life.mkdir()
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_locked_loopback_broker",
        "trusted_run_lane": "conversation",
        "name": "Locked Loopback Broker Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "codex_config_append": (
                    "[mcp_servers.glasshive-user-capabilities]\n"
                    'url = "http://127.0.0.1:18180/api/capabilities/mcp"'
                ),
            }
        ),
    }
    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)

    command, env = runtime._build_command(
        worker,
        "Use the declared read-only broker capability.",
        runtime._host_runtime_info(worker),
    )

    config_path = runtime._host_codex_home(worker) / "config.toml"
    config = config_path.read_text()
    assert "mcp_servers.glasshive-user-capabilities" in config
    assert "http://127.0.0.1:18180/api/capabilities/mcp" in config
    assert env["CODEX_HOME"] == str(config_path.parent)
    assert 'approval_policy="never"' in command
    assert 'sandbox_mode="workspace-write"' in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "danger-full-access" not in command


def test_host_native_web_access_uses_standalone_alias_only_without_compiled_policy(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_alias_fallback",
        "name": "Standalone Locked Native Web Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("WPR_HOST_NATIVE_WEB_ACCESS", raising=False)
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "disabled")

    command, _ = runtime._build_command(
        worker,
        "Use the assigned brokered research capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' in command


@pytest.mark.parametrize(
    ("compiled", "standalone", "expected"),
    [
        ("disabled", "inherit", "disabled"),
        ("inherit", "disabled", "inherit"),
        (None, "disabled", "disabled"),
    ],
)
def test_host_native_web_access_resolver_honors_compiled_precedence(
    compiled, standalone, expected, monkeypatch
):
    if compiled is None:
        monkeypatch.delenv("WPR_HOST_NATIVE_WEB_ACCESS", raising=False)
    else:
        monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", compiled)
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", standalone)

    assert _host_native_web_access() == expected


def test_host_native_web_access_compiled_inherit_overrides_standalone_alias(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_compiled_inherit",
        "name": "Compiled Full Native Web Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "inherit")
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "disabled")

    command, _ = runtime._build_command(
        worker,
        "Research with the best available capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' not in command


def test_host_codex_native_web_access_defaults_to_inherit(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_inherited",
        "name": "Full Native Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", raising=False)
    monkeypatch.delenv("WPR_HOST_NATIVE_WEB_ACCESS", raising=False)

    command, _ = runtime._build_command(
        worker,
        "Research with the best available capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' not in command


def test_host_claude_native_web_access_policy_disables_web_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_native_web_locked",
        "name": "Locked Native Web Worker",
        "profile": "claude-code",
        "model": "opus",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "1")

    command, _ = runtime._build_command(
        worker,
        "Use the assigned brokered research capability.",
        runtime._runtime_info(worker),
    )

    assert "--disallowedTools" in command
    denied_index = command.index("--disallowedTools")
    assert command[denied_index + 1 : denied_index + 3] == ["WebSearch", "WebFetch"]
    assert "--chrome" not in command
    assert "--no-chrome" in command
    sources_index = command.index("--setting-sources")
    assert command[sources_index + 1] == ""
    settings_index = command.index("--settings")
    settings = json.loads(command[settings_index + 1])
    assert settings["sandbox"] == {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "network": {
            "allowedDomains": [],
            "strictAllowlist": True,
        },
    }


def test_claude_code_runtime_passes_gateway_headers(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_gateway",
        "name": "Claude Worker",
        "profile": "claude-code",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_CLAUDE_CODE_USE_API_KEY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-portkey-provider: anthropic")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-test")

    command, env = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    assert "--model" in command
    assert env["ANTHROPIC_API_KEY"] == "anthropic-test"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-test"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-portkey-provider: anthropic"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-test"


def test_claude_code_runtime_passes_headless_oauth_without_api_key_mode(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_oauth",
        "name": "Claude Worker",
        "profile": "claude-code",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("WPR_CLAUDE_CODE_USE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-test")

    _command, env = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-test"


def test_claude_code_runtime_uses_bedrock_provider_model_without_oauth(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_bedrock",
        "name": "Claude Bedrock Worker",
        "profile": "claude-code",
        "model": "claude-opus-5",
    }
    runtime._ensure_dirs(worker["worker_id"])
    provider_model = (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "application-inference-profile/opus-48-test"
    )
    monkeypatch.setenv("WPR_CLAUDE_CODE_PROVIDER_MODEL", provider_model)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLEONLY0000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-secret-not-real")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")

    command, env = runtime._build_command(
        worker, "Create the artifact.", runtime._runtime_info(worker)
    )

    assert command[command.index("--model") + 1] == provider_model
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLEONLY0000"
    assert env["AWS_SECRET_ACCESS_KEY"] == "synthetic-secret-not-real"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_host_env_strips_parent_secrets_and_keeps_minimal_runtime_context(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET",
        "service-assertion-secret",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    monkeypatch.setenv("LIBRECHAT_SECRET", "librechat-secret")
    monkeypatch.setenv("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "false")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("USER", "testuser")
    monkeypatch.setenv("LOGNAME", "testuser")
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker, run_id="run-123")

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["GLASSHIVE_WORKER_ID"] == "wrk_host"
    assert env["GLASSHIVE_RUN_ID"] == "run-123"
    assert "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET" not in env
    assert "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET" not in env
    assert "OPENAI_API_KEY" not in env
    assert "LIBRECHAT_SECRET" not in env
    # USER/LOGNAME must pass through: macOS Keychain-backed CLIs (claude-code's
    # subscription auth) resolve the keychain item by user and report "Not logged in"
    # without them. They are identity, not secrets, so this does not weaken stripping.
    assert env["USER"] == "testuser"
    assert env["LOGNAME"] == "testuser"


def test_host_openclaw_reserved_preflight_reports_named_missing_binary(tmp_path):
    runtime = HostOpenClawRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "definitely-missing-openclaw"
    worker = {
        "worker_id": "wrk_openclaw",
        "name": "OpenClaw Host Worker",
        "profile": "openclaw-general",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="definitely-missing-openclaw CLI is not installed") as captured:
        runtime.preflight_worker_profile("openclaw-general", "host")
    assert captured.value.binary == "definitely-missing-openclaw"
    assert captured.value.profile == "openclaw-general"
    assert captured.value.execution_mode == "host"


def test_runtime_dependency_missing_classification_is_structured_and_sanitized():
    failure = classify_runtime_error(
        RuntimeDependencyMissingError(
            "codex CLI is not installed or not on PATH for host-native codex-cli",
            binary="/private/tmp/secret-path/codex",
            runtime_name="codex-cli",
            profile="codex-cli",
            execution_mode="host",
        ),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "`codex`" in failure.user_message
    assert "/private/tmp" not in failure.user_message
    assert "sandbox/workstation" in failure.recommended_recovery


def test_host_runtime_preflight_rejects_configured_version_mismatch(tmp_path, monkeypatch):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "codex-cli": [
                    {
                        "binary": str(fake_node),
                        "label": "Node.js",
                        "min_version": "22.19.0",
                    }
                ]
            }
        ),
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    with pytest.raises(RuntimeDependencyMissingError, match="Node.js") as captured:
        runtime.preflight_worker_profile("codex-cli", "host")

    assert captured.value.required_version == "22.19.0"
    assert captured.value.actual_version == "20.20.2"
    assert captured.value.dependency_label == "Node.js"


def test_host_runtime_preflight_accepts_configured_version(tmp_path, monkeypatch):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v22.19.0'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    runtime.preflight_worker_profile("codex-cli", "host")


def test_host_runtime_preflight_rejects_codex_below_reviewed_compatibility_floor(
    tmp_path, monkeypatch
):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/usr/bin/env bash\necho 'codex-cli 0.140.0'\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("WPR_CODEX_BIN", str(fake_codex))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="Codex CLI") as captured:
        runtime.preflight_worker_profile("codex-cli", "host")

    assert captured.value.required_version == "0.144.1"
    assert captured.value.actual_version == "0.140.0"
    assert "codex update" in captured.value.recovery_hint


def test_host_runtime_preflight_rejects_default_version_mismatch(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.100 (Claude Code)'; exit 0; fi\n"
        "echo 'Usage: claude [options] --effort --chrome'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="Claude Code") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.required_version == "2.1.178"
    assert captured.value.actual_version == "2.1.100"


def test_host_runtime_preflight_rejects_missing_help_capability(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/usr/bin/env bash\necho 'Usage: claude [options]'\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "claude-code": [
                    {
                        "binary": str(fake_claude),
                        "label": "Claude Code",
                        "required_help_flags": ["--chrome"],
                    }
                ]
            }
        ),
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    with pytest.raises(RuntimeDependencyMissingError, match="native capability") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.dependency_label == "Claude Code"


def test_host_runtime_preflight_accepts_required_mcp_capability(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"mcp\" && \"$2\" == \"list\" ]]; then\n"
        "  echo 'computer-use enabled'\n"
        "  echo 'node_repl enabled'\n"
        "  exit 0\n"
        "fi\n"
        "echo 'codex-cli 0.146.1'\n"
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "codex-cli": [
                    {
                        "binary": str(fake_codex),
                        "label": "Codex CLI",
                        "required_mcp_servers": ["computer-use", "node_repl"],
                    }
                ]
            }
        ),
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    runtime.preflight_worker_profile("codex-cli", "host")


def test_host_claude_preflight_rejects_cli_without_chrome_support(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.223 (Claude Code)'; exit 0; fi\n"
        "echo 'Usage: claude [options] --effort'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_CLAUDE_CODE_ENABLE_CHROME", raising=False)

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="supports --chrome") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.dependency_label == "Claude Code"


def test_host_claude_preflight_allows_explicit_chrome_lockdown(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.223 (Claude Code)'; exit 0; fi\n"
        "echo 'Usage: claude [options] --effort'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime._claude_cli_managed_auth_available",
        lambda *_args, **_kwargs: True,
    )

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    runtime.preflight_worker_profile("claude-code", "host")


def test_host_claude_preflight_rejects_max_effort_without_effort_support(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --chrome'; exit 0; fi\n"
        "echo '2.1.223 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "claude-code": [
                    {
                        "binary": str(fake_claude),
                        "label": "Claude Code",
                        "required_help_flags": ["--chrome"],
                    }
                ]
            }
        ),
    )

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="native --effort") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.dependency_label == "Claude Code"


def test_host_codex_runtime_uses_configured_binary_path(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'codex-cli 0.146.1'; exit 0; fi\n"
        "echo 'codex test'\n"
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("WPR_CODEX_BIN", str(fake_codex))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    assert runtime.binary == str(fake_codex)
    runtime.preflight_worker_profile("codex-cli", "host")


def test_cli_failure_classifies_runtime_version_substrate():
    failure = classify_cli_failure(
        stdout="",
        stderr="It failed. The local worker runtime needs Node.js v22.19+ and this machine is on v20.20.2.",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "sandbox/workstation" in failure.recommended_recovery


def test_cli_failure_classifies_missing_executable_substrate():
    failure = classify_cli_failure(
        stdout="",
        stderr=(
            "codex-cli exited with code 127: "
            "/workspace/.wpr-home/.glasshive-runs/run_demo/run.sh: line 15: "
            "/Applications/Codex.app/Contents/Resources/codex: No such file or directory"
        ),
        runtime_name="codex-cli",
        exit_code=127,
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "configured managed dependency" in failure.recommended_recovery


def test_cli_failure_classifies_not_logged_in_provider_session():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 401,
                "terminal_reason": "api_error",
                "result": "Not logged in · Please run /login",
            }
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_missing"
    assert failure.retryable is False
    assert "provider credentials" in failure.user_message
    assert "CLI login" in failure.recommended_recovery


def test_cli_failure_classifies_expired_claude_oauth_session():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "error": "authentication_failed",
                "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
            }
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_missing"
    assert failure.retryable is False
    assert "provider credentials" in failure.user_message


def test_cli_failure_classifies_split_expired_claude_oauth_session():
    failure = classify_cli_failure(
        stdout="\n".join(
            (
                json.dumps(
                    {
                        "type": "assistant",
                        "error": "authentication_failed",
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": True,
                        "api_error_status": None,
                    }
                ),
            )
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_missing"
    assert failure.personal_account_reconnect is True


def test_cli_failure_ignores_non_event_noise_between_native_auth_events():
    failure = classify_cli_failure(
        stdout="\n".join(
            (
                json.dumps({"type": "assistant", "error": "authentication_failed"}),
                "",
                "native process note",
                json.dumps({"type": "result", "is_error": True}),
            )
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_missing"
    assert failure.personal_account_reconnect is True


def test_cli_failure_does_not_join_nonadjacent_authentication_events():
    failure = classify_cli_failure(
        stdout="\n".join(
            (
                json.dumps(
                    {
                        "type": "assistant",
                        "error": "authentication_failed",
                    }
                ),
                json.dumps({"type": "assistant", "message": "ordinary worker output"}),
                json.dumps({"type": "result", "is_error": True}),
            )
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.personal_account_reconnect is False


def test_cli_failure_requires_native_result_after_authentication_event():
    failure = classify_cli_failure(
        stdout="\n".join(
            (
                json.dumps({"type": "assistant", "error": "authentication_failed"}),
                json.dumps({"type": "tool", "is_error": True}),
            )
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.personal_account_reconnect is False


def test_cli_failure_does_not_join_authentication_events_across_streams():
    failure = classify_cli_failure(
        stdout=json.dumps({"type": "assistant", "error": "authentication_failed"}),
        stderr=json.dumps({"type": "result", "is_error": True}),
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.personal_account_reconnect is False


def test_cli_failure_does_not_treat_worker_transcript_as_provider_auth_failure():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "is_error": True,
                "error": "tool_failed",
                "result": (
                    "A test failed while comparing the literal value "
                    "authentication_failed; the provider session was not involved."
                ),
            }
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"


def test_runtime_error_preserves_private_cli_failure_classification():
    embedded = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "is_error": True,
                "error": "authentication_failed",
                "result": "Failed to authenticate",
            }
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )
    private = profile_runtime_module._private_cli_failure_classification(
        embedded,
        exit_code=1,
    )
    error = RuntimeErrorBase("claude-code exited with code 1")
    error.failure_classification = private  # type: ignore[attr-defined]

    classified = classify_runtime_error(error, runtime_name="claude-code")

    assert classified is private
    assert classified.failure_class == "provider_auth_missing"
    assert classified.diagnostic_summary == "class=provider_auth_missing; exit_code=1"


def test_private_cli_failure_classification_drops_provider_transcript():
    classification = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "is_error": True,
                "error": "authentication_failed",
                "result": "Sensitive synthetic mission content must not leave the run files.",
            }
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    private = profile_runtime_module._private_cli_failure_classification(
        classification,
        exit_code=1,
    )

    assert private.failure_class == "provider_auth_missing"
    assert private.personal_account_reconnect is True
    assert private.diagnostic_summary == "class=provider_auth_missing; exit_code=1"
    assert "Sensitive synthetic mission content" not in private.diagnostic_summary


def test_runtime_error_classifies_missing_executable_substrate():
    failure = classify_runtime_error(
        RuntimeErrorBase(
            "codex-cli exited with code 127: "
            "/workspace/.wpr-home/.glasshive-runs/run_demo/run.sh: line 15: "
            "/Applications/Codex.app/Contents/Resources/codex: No such file or directory"
        ),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "missing, unavailable, or incompatible" in failure.user_message


def test_runtime_error_does_not_infer_authentication_from_provider_prose():
    failure = classify_runtime_error(
        RuntimeErrorBase('claude-code exited with code 1: {"result":"Not logged in · Please run /login"}'),
        runtime_name="claude-code",
    )

    assert failure.failure_class == "runtime_error"


def test_runtime_error_preserves_structured_provider_authentication_class():
    error = RuntimeErrorBase("claude-code exited with a structured provider failure")
    error.failure_class = "provider_auth_missing"

    failure = classify_runtime_error(error, runtime_name="claude-code")

    assert failure.failure_class == "provider_auth_missing"
    assert failure.retryable is False
    assert failure.structured is True


def test_provider_process_exit_preserves_structured_authentication_class():
    error = _provider_process_exit_error(
        runtime_name="claude-code",
        exit_code=1,
        stdout=json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 401,
                "terminal_reason": "api_error",
                "result": "localized provider diagnostic",
            }
        ),
        stderr="",
        message="claude-code exited with code 1",
    )

    assert error.failure_class == "provider_auth_missing"


def test_authored_typed_rate_limit_does_not_become_automatic_retry():
    classification = FailureClassification(
        failure_class="provider_rate_limited",
        retryable=False,
        user_message="The selected provider reached its rate limit.",
        recommended_recovery="Review prior work and continue after reset.",
        diagnostic_summary="Typed failed turn after authoring.",
        structured=True,
        retry_after_s=3600,
        provider_event_source="codex_app_server",
    )
    error = _provider_process_exit_error(
        runtime_name="codex-cli", exit_code=1, stdout="", stderr="",
        message="codex-cli exited with code 1", classification=classification,
    )

    assert type(error) is RuntimeErrorBase
    assert classify_runtime_error(error, runtime_name="codex-cli") == classification


def test_cli_failure_does_not_infer_authentication_from_unstructured_prose():
    failure = classify_cli_failure(
        stdout="",
        stderr="ERROR: unauthorized wording from a user-controlled provider response",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"


def test_runtime_error_classifies_unsupported_runtime_configuration():
    failure = classify_runtime_error(
        RuntimeErrorBase("host-native workers are disabled in this deployment"),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "unsupported_runtime_configuration"
    assert failure.retryable is False
    assert "host-native workers are disabled" in failure.user_message


def test_cli_failure_does_not_classify_generic_file_not_found_as_runtime_dependency():
    failure = classify_cli_failure(
        stdout="",
        stderr="The requested uploaded source file was missing: No such file or directory",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"
    assert failure.retryable is False


def test_cli_failure_classifies_missing_python_module_as_runtime_dependency():
    failure = classify_cli_failure(
        stdout=(
            "Traceback (most recent call last):\n"
            "  File \"<stdin>\", line 1, in <module>\n"
            "ModuleNotFoundError: No module named 'requests'\n"
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "managed dependency" in failure.recommended_recovery


def test_runtime_error_does_not_classify_generic_file_not_found_as_runtime_dependency():
    failure = classify_runtime_error(
        FileNotFoundError("Bootstrap source file not found: /Users/example/private-upload.pdf"),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_error"
    assert failure.retryable is False
    assert "/Users/example" not in failure.diagnostic_summary
    assert "[local path]" in failure.diagnostic_summary


def test_runtime_error_classifies_glasshive_evidence_failure():
    failure = classify_runtime_error(
        RuntimeErrorBase("GlassHive evidence check failed: completion compliance failed: missing pdf"),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "glasshive_evidence_check_failed"
    assert failure.retryable is True
    assert "workspace_continue" in failure.recommended_recovery


def test_runtime_error_classifies_sandbox_lifecycle_failure():
    failure = classify_runtime_error(
        RuntimeErrorBase(
            "Failed to prepare writable sandbox paths in wpr-wrk-example: "
            "Error response from daemon: No such container: wpr-wrk-example"
        ),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_sandbox_unavailable"
    assert failure.retryable is True
    assert "sandbox/workstation" in failure.user_message


def test_cli_failure_classifies_sigterm_as_runtime_terminated():
    failure = classify_cli_failure(
        stdout="",
        stderr="",
        runtime_name="claude-code",
        exit_code=143,
    )

    assert failure.failure_class == "runtime_terminated"
    assert failure.retryable is False
    assert "workspace_continue" in failure.recommended_recovery


def test_openclaw_session_id_is_cli_safe_when_worker_session_key_uses_glasshive_colons(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "session_key": "agent:main:wpr:worker:wrk_openclaw",
    }

    assert runtime._default_session_key(worker) == "wpr-worker-wrk_openclaw"

    info = runtime._runtime_info(worker)
    command, env = runtime._build_command(worker, "Create a file.", info)

    assert command[command.index("--session-id") + 1] == "wpr-worker-wrk_openclaw"
    assert env["OPENCLAW_MODEL"]


def test_openclaw_can_scope_session_key_per_run(tmp_path, monkeypatch):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "_active_run_id": "run_abc123",
    }
    monkeypatch.setenv("WPR_OPENCLAW_SESSION_SCOPE", "run")

    assert runtime._default_session_key(worker) == "wpr-worker-wrk_openclaw-run_abc123"

    info = runtime._runtime_info(worker)
    command, env = runtime._build_command(worker, "Create a file.", info)

    assert command[command.index("--session-id") + 1] == "wpr-worker-wrk_openclaw-run_abc123"
    assert env


def test_hosted_openclaw_command_env_exposes_only_its_selected_provider_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("OPENAI_API_BASE", "https://openai.example.test/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "synthetic-portkey")
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://portkey.example.test/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-enter-openclaw")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))

    openai_env = runtime._sandbox_env()
    assert openai_env["OPENAI_API_KEY"] == "synthetic-openai"
    assert "PORTKEY_API_KEY" not in openai_env
    assert "ANTHROPIC_API_KEY" not in openai_env

    monkeypatch.setenv("WPR_OPENCLAW_BASE_URL", "https://selected.example.test/v1")
    monkeypatch.setenv("WPR_OPENCLAW_ENV_KEY", "PORTKEY_API_KEY")
    portkey_env = runtime._sandbox_env()
    assert portkey_env["PORTKEY_API_KEY"] == "synthetic-portkey"
    assert "OPENAI_API_KEY" not in portkey_env
    assert "ANTHROPIC_API_KEY" not in portkey_env


def test_openclaw_neutralizes_default_onboarding_bootstrap_for_task_runs(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_bootstrap",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
    }
    workspace = runtime._workspace_dir(worker["worker_id"])
    workspace.mkdir(parents=True)
    bootstrap_path = workspace / "BOOTSTRAP.md"
    bootstrap_path.write_text(
        "\n".join(
            [
                "# BOOTSTRAP.md - Hello, World",
                "",
                "_You just woke up. Time to figure out who you are._",
                "",
                "Start with something like:",
                "",
                '> "Hey. I just came online. Who am I? Who are you?"',
                "",
            ]
        )
    )

    runtime._build_command(worker, "Create the requested artifact.", runtime._runtime_info(worker))

    rewritten = bootstrap_path.read_text()
    assert "GlassHive Task Mode" in rewritten
    assert "Do not start first-run identity onboarding" in rewritten
    assert "prefer localhost HTTP URLs over file:// URLs" in rewritten
    archived = workspace / ".glasshive" / "archived-openclaw-default-bootstrap.md"
    assert archived.exists()
    assert "Hello, World" in archived.read_text()


def test_openclaw_provisions_task_bootstrap_before_cli_can_create_onboarding(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_no_bootstrap",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
    }
    bootstrap_path = runtime._workspace_dir(worker["worker_id"]) / "BOOTSTRAP.md"
    assert not bootstrap_path.exists()

    runtime._build_command(worker, "Create the requested artifact.", runtime._runtime_info(worker))

    text = bootstrap_path.read_text()
    assert "GlassHive Task Mode" in text
    assert "Follow the latest runtime-provided instruction" in text
    assert "prefer localhost HTTP URLs over file:// URLs" in text


def test_openclaw_starts_gateway_screen_session_for_browser_tools(tmp_path, monkeypatch):
    class FakeSandbox:
        home_mount = "/workspace/.wpr-home"
        workspace_mount = "/workspace/project"
        term_value = "xterm-256color"
        display_value = ":99.0"

        def __init__(self) -> None:
            self.started: list[dict[str, object]] = []
            self.execs: list[dict[str, object]] = []

        def paths(self, worker_id: str) -> dict[str, Path]:
            root = tmp_path / "data" / "docker_sandboxes" / "workers" / worker_id / "state"
            return {
                "state_dir": root,
                "workspace_dir": root / "workspace",
                "home_dir": root / "home",
                "worker_root": root.parent,
            }

        def start_screen_session(self, worker_id, runtime_name, session_name, command, *, env=None, worker=None):
            self.started.append(
                {
                    "worker_id": worker_id,
                    "runtime_name": runtime_name,
                    "session_name": session_name,
                    "command": command,
                    "env": env,
                    "worker": worker,
                }
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        def _docker_exec(self, container_name, command, *, env=None, cwd=None, **kwargs):
            self.execs.append({"container_name": container_name, "command": command, "env": env, "cwd": cwd, "kwargs": kwargs})
            return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("WPR_OPENCLAW_START_GATEWAY", "true")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake
    worker = {"worker_id": "wrk_openclaw_gateway", "name": "OpenClaw Worker", "profile": "openclaw-general"}
    sandbox_info = type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-gateway"})()

    runtime._start_openclaw_gateway(worker, sandbox_info)

    assert fake.started[0]["session_name"] == "openclaw-gateway"
    assert "openclaw gateway --port 18789" in " ".join(fake.started[0]["command"])
    assert fake.started[0]["env"]["OPENCLAW_CONFIG_PATH"] == "/workspace/.wpr-home/.wpr-openclaw/openclaw.json"
    assert fake.execs[0]["container_name"] == "wpr-wrk-openclaw-gateway"


def test_openclaw_task_runs_do_not_start_gateway(tmp_path, monkeypatch):
    class FakeSandbox:
        home_mount = "/workspace/.wpr-home"
        workspace_mount = "/workspace/project"
        term_value = "xterm-256color"
        display_value = ":99.0"

        def __init__(self) -> None:
            self.started: list[dict[str, object]] = []

        def paths(self, worker_id: str) -> dict[str, Path]:
            root = tmp_path / "data" / "docker_sandboxes" / "workers" / worker_id / "state"
            return {
                "state_dir": root,
                "workspace_dir": root / "workspace",
                "home_dir": root / "home",
                "worker_root": root.parent,
            }

        def ensure_ready(self, worker, runtime_name, **kwargs):
            return type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-task", "pid": 123})()

        def start_screen_session(self, worker_id, runtime_name, session_name, command, *, env=None, worker=None):
            self.started.append({"session_name": session_name, "command": command, "worker": worker})
            return subprocess.CompletedProcess(command, 0, "", "")

    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake
    worker = {"worker_id": "wrk_openclaw_task", "name": "OpenClaw Worker", "profile": "openclaw-general"}

    info = runtime.ensure_worker_ready({**worker, "_glasshive_task_run": True})

    assert info.runtime == "openclaw"
    assert fake.started == []


def test_openclaw_gateway_is_opt_in_for_worker_readiness(tmp_path):
    class FakeSandbox:
        home_mount = "/workspace/.wpr-home"
        workspace_mount = "/workspace/project"
        term_value = "xterm-256color"
        display_value = ":99.0"

        def __init__(self) -> None:
            self.started: list[dict[str, object]] = []

        def paths(self, worker_id: str) -> dict[str, Path]:
            root = tmp_path / "data" / "docker_sandboxes" / "workers" / worker_id / "state"
            return {
                "state_dir": root,
                "workspace_dir": root / "workspace",
                "home_dir": root / "home",
                "worker_root": root.parent,
            }

        def ensure_ready(self, worker, runtime_name, **kwargs):
            return type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-ready", "pid": 123})()

        def start_screen_session(self, worker_id, runtime_name, session_name, command, *, env=None, worker=None):
            self.started.append({"session_name": session_name, "command": command})
            return subprocess.CompletedProcess(command, 0, "", "")

    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake

    runtime.ensure_worker_ready({"worker_id": "wrk_openclaw_ready", "name": "OpenClaw Worker", "profile": "openclaw-general"})

    assert fake.started == []


def test_openclaw_desktop_action_does_not_start_gateway(tmp_path):
    class FakeSandbox:
        def __init__(self) -> None:
            self.ensure_calls: list[dict[str, object]] = []
            self.desktop_actions: list[dict[str, object]] = []

        def ensure_ready(self, worker, runtime_name, **kwargs):
            self.ensure_calls.append({"worker": worker, "runtime_name": runtime_name, **kwargs})
            return type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-action", "pid": 123})()

        def desktop_action(self, worker_id, runtime_name, action, *, url=None, session_name=None, worker=None):
            self.desktop_actions.append(
                {
                    "worker_id": worker_id,
                    "runtime_name": runtime_name,
                    "action": action,
                    "url": url,
                    "session_name": session_name,
                    "worker": worker,
                }
            )
            return {"action": action, "status": "launched", "view_url": "http://127.0.0.1:7900"}

        def start_screen_session(self, *args, **kwargs):  # pragma: no cover - failure path
            raise AssertionError("desktop_action must not start the OpenClaw gateway")

    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake
    worker = {"worker_id": "wrk_openclaw_action", "name": "OpenClaw Worker", "profile": "openclaw-general"}

    launched = runtime.desktop_action(worker, "browser", url="about:blank")

    assert launched["status"] == "launched"
    assert fake.ensure_calls == []
    assert fake.desktop_actions[0]["action"] == "browser"
    assert fake.desktop_actions[0]["url"] == "about:blank"


@pytest.mark.parametrize("runtime_class", [CodexCliRuntime, ClaudeCodeRuntime])
def test_provider_binding_cleanup_removes_container_when_stale_session_stop_fails(
    tmp_path,
    runtime_class,
):
    class FakeSandbox:
        def __init__(self) -> None:
            self.terminated: list[str] = []
            self.repaired: list[Path] = []

        def terminate(self, worker_id: str):
            self.terminated.append(worker_id)

        def repair_provider_account_access(self, account_home: Path) -> None:
            self.repaired.append(Path(account_home))

    runtime = runtime_class(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake
    runtime._stop_active_process = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("synthetic stale completed-run metadata")
    )
    cleared: list[str] = []
    runtime._clear_active_session = cleared.append
    account_home = tmp_path / "provider-account"
    account_home.mkdir()

    runtime.release_provider_account_binding(
        {
            "worker_id": "wrk_stale_session_cleanup",
            "_glasshive_provider_account_mount_host": str(account_home),
        }
    )

    assert fake.terminated == ["wrk_stale_session_cleanup"]
    assert fake.repaired == [account_home]
    assert cleared == ["wrk_stale_session_cleanup"]


def test_openclaw_projects_openai_compatible_provider_without_storing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-test-value")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_provider",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["gateway"] == {"mode": "local", "bind": "loopback", "port": 18789, "auth": {"mode": "none"}}
    assert config["agents"]["defaults"]["workspace"] == "/workspace/project"
    assert config["agents"]["defaults"]["repoRoot"] == "/workspace/project"
    assert config["agents"]["defaults"]["model"]["primary"] == "glasshive-openai-compatible/gpt-5.2"
    provider = config["models"]["providers"]["glasshive-openai-compatible"]
    assert provider["baseUrl"] == "https://models.example.test/openai/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == {"source": "env", "provider": "default", "id": "OPENAI_API_KEY"}
    assert provider["models"][0]["id"] == "gpt-5.2"
    assert "openai-secret-test-value" not in json.dumps(config)

    info = runtime._runtime_info(worker)
    command, env = runtime._build_command(worker, "Create a file.", info)

    assert env["OPENCLAW_MODEL"] == "glasshive-openai-compatible/gpt-5.2"
    assert env["OPENAI_BASE_URL"] == "https://models.example.test/openai/v1"
    assert env["OPENAI_API_KEY"] == "openai-secret-test-value"
    assert command[command.index("--session-id") + 1] == "wpr-worker-wrk_openclaw_provider"


def test_openclaw_uses_configured_openai_models_for_compatible_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_MODELS", "gpt-5.2-chat,gpt-5.2")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_models",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
    }

    assert runtime._openclaw_model_for_worker(worker) == "glasshive-openai-compatible/gpt-5.2-chat"

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["agents"]["defaults"]["model"]["primary"] == "glasshive-openai-compatible/gpt-5.2-chat"
    assert config["models"]["providers"]["glasshive-openai-compatible"]["models"][0]["id"] == "gpt-5.2-chat"


def test_openclaw_projects_portkey_headers_as_secret_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://api.portkey.example/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "portkey-secret-test-value")
    monkeypatch.setenv("PORTKEY_VIRTUAL_KEY", "virtual-key-secret")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_portkey",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "anthropic/claude-sonnet-4-6",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["agents"]["defaults"]["model"]["primary"] == (
        "glasshive-portkey-compatible/anthropic/claude-sonnet-4-6"
    )
    provider = config["models"]["providers"]["glasshive-portkey-compatible"]
    assert provider["apiKey"] == {"source": "env", "provider": "default", "id": "PORTKEY_API_KEY"}
    assert provider["headers"]["x-portkey-virtual-key"] == {
        "source": "env",
        "provider": "default",
        "id": "PORTKEY_VIRTUAL_KEY",
    }
    serialized = json.dumps(config)
    assert "portkey-secret-test-value" not in serialized
    assert "virtual-key-secret" not in serialized


@pytest.mark.parametrize("max_tokens_field", ["max_completion_tokens", "max_tokens"])
def test_openclaw_projects_can_configure_openai_compat_max_token_field(tmp_path, monkeypatch, max_tokens_field):
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://api.portkey.example/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "portkey-secret-test-value")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_ID", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_NAME", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_COMPAT_MAX_TOKENS_FIELD", max_tokens_field)
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_portkey_azure",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "@example/gpt-deployment-chat",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["agents"]["defaults"]["model"]["primary"] == (
        "glasshive-portkey-compatible/@example/gpt-deployment-chat"
    )
    model_entry = config["models"]["providers"]["glasshive-portkey-compatible"]["models"][0]
    assert model_entry["id"] == "@example/gpt-deployment-chat"
    assert model_entry["name"] == "@example/gpt-deployment-chat"
    assert model_entry["compat"]["maxTokensField"] == max_tokens_field
    assert "portkey-secret-test-value" not in json.dumps(config)


def test_openclaw_projects_ignore_unknown_compat_max_token_field(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://api.portkey.example/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "portkey-secret-test-value")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_ID", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_NAME", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_COMPAT_MAX_TOKENS_FIELD", "bogus")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_portkey_invalid_compat",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "@example/gpt-deployment-chat",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    model_entry = config["models"]["providers"]["glasshive-portkey-compatible"]["models"][0]
    assert "compat" not in model_entry
    assert "portkey-secret-test-value" not in json.dumps(config)


def test_redact_text_masks_parent_visible_secret_shapes():
    synthetic_openai_token = "sk-" + "abc123456789xyz"
    synthetic_bearer = "abcdef" + "ghijklmnopqrstuvwxyz"
    synthetic_aws_access_key = "AKIA" + "EXAMPLEONLY00000"
    redacted = _redact_text(
        f"Authorization: {'Bearer'} {synthetic_bearer} token=super-secret-value "
        f"{synthetic_openai_token} {synthetic_aws_access_key}"
    )
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "super-secret-value" not in redacted
    assert synthetic_openai_token not in redacted
    assert synthetic_aws_access_key not in redacted
    assert "[REDACTED_AWS_ACCESS_KEY]" in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_masks_common_host_paths_and_credential_families():
    private_key = (
        "-----BEGIN "
        "PRIVATE KEY-----\n"
        "c3ludGhldGljLXByaXZhdGUta2V5LW1hdGVyaWFs\n"
        "-----END PRIVATE KEY-----"
    )
    raw = " ".join(
        [
            "/home/synthetic/private.txt",
            "/root/private.txt",
            "/Volumes/Private/private.txt",
            "/private/var/synthetic/private.txt",
            "ghp_syntheticgithubcredential",
            "xoxb-synthetic-slack-credential",
            "eyJhbGciOiJIUzI1NiJ9.c3ludGhldGlj.c2lnbmF0dXJl",
            private_key,
        ]
    )

    redacted = _redact_text(raw)

    for forbidden in (
        "/home/synthetic",
        "/root/private.txt",
        "/Volumes/Private",
        "/private/var/synthetic",
        "syntheticgithubcredential",
        "synthetic-slack-credential",
        "c3ludGhldGlj",
        "c3ludGhldGljLXByaXZhdGUta2V5LW1hdGVyaWFs",
    ):
        assert forbidden not in redacted


@pytest.mark.parametrize("destination", [
    "</Users/synthetic/Project Folder/private-report.md>",
    "/home/synthetic/private-report.md",
    "<file:///Users/synthetic/Project Folder/private-report.md>",
    "/Volumes/Private/private-report.md",
    "~/private-report.md",
    "/root/reports/private-report(2).md",
])
def test_redact_text_keeps_local_citation_label_without_a_broken_link(destination):
    raw = f"See [Project report]({destination}) for details. [Public source](https://example.org/docs)"
    redacted = _redact_text(raw)
    assert redacted == "See Project report for details. [Public source](https://example.org/docs)"
    assert "private-report" not in redacted
    assert _redact_text(redacted) == redacted


def test_redact_text_keeps_private_image_label_and_redacts_secret_labels():
    raw = "![Sketch](</home/synthetic/private-image.png>) [token=synthetic-secret-value](~/private.txt)"
    assert _redact_text(raw) == "Sketch token=[REDACTED]"


def test_redact_text_fails_closed_for_an_unterminated_private_key():
    private_key_body = "A" * 120
    redacted = _redact_text(
        "Safe prefix.\n-----BEGIN PRIVATE KEY-----\n"
        f"{private_key_body}\n"
        "untrusted trailing text"
    )

    assert "Safe prefix." in redacted
    assert "BEGIN PRIVATE KEY" not in redacted
    assert private_key_body not in redacted
    assert "untrusted trailing text" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted


def test_redact_text_masks_parent_visible_image_payloads():
    base64_png = "iVBORw0KGgo" + ("A" * 900) + "=="
    redacted = _redact_text(
        '{"type":"tool_result","content":[{"type":"image","mimeType":"image/png","data":"'
        + base64_png
        + '"}]}'
    )

    assert base64_png not in redacted
    assert "[REDACTED_LONG_BASE64]" in redacted


def test_host_conversation_mode_uses_exact_workspace_without_scaffolding(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    canonical_agents = "# Personal LIFE instructions\n"
    (life / "AGENTS.md").write_text(canonical_agents)
    worker = {
        "worker_id": "wrk_conversation",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "gpt-5.6-sol", "access_mode": "full"}
        ),
    }

    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)
    instruction = runtime._command_stdin_text(worker, "Could you help me think?", runtime._host_runtime_info(worker))

    assert workspace == life
    assert instruction == "Could you help me think?"
    assert (life / "AGENTS.md").read_text() == canonical_agents
    assert sorted(path.name for path in life.iterdir()) == ["AGENTS.md"]
    for forbidden in ("CLAUDE.md", "CODEX.md", "project-definition.md", "work-log.md", "harness-prompt.md", ".git", "glasshive-run"):
        assert not (life / forbidden).exists()


@pytest.mark.parametrize("runtime_class,profile", [(HostCodexCliRuntime, "codex-cli"), (HostClaudeCodeRuntime, "claude-code")])
def test_host_conversation_materializes_declared_uploads_in_exact_workspace(tmp_path, monkeypatch, runtime_class, profile):
    from workers_projects_runtime.upload_projection import project_upload_files

    uploads = tmp_path / "uploads"
    source = uploads / "owner-a" / "audio-id__request.m4a"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original-audio-bytes")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads))
    life = tmp_path / "Life"
    life.mkdir()
    (life / "AGENTS.md").write_text("User workspace instructions.")
    files = project_upload_files({"selected_uploads": [{"file_id": "audio-id", "filename": "request.m4a"}]}, owner_id="owner-a")
    worker = {"worker_id": "wrk_current_audio", "owner_id": "owner-a", "profile": profile,
              "execution_mode": "host", "trusted_run_lane": "conversation", "workspace_root": str(life),
              "bootstrap_bundle_json": json.dumps({"run_mode": "conversation", "files": files})}
    runtime = runtime_class(base_dir=str(tmp_path / "state"))
    runtime._materialize_workspace(worker, life)
    assert (life / files[0]["path"]).read_bytes() == source.read_bytes()
    assert (life / "AGENTS.md").read_text() == "User workspace instructions."
    assert not (life / "project-definition.md").exists()


def test_host_codex_conversation_can_exclude_workspace_project_instructions(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_CODEX_CLI_CONVERSATION_PROJECT_INSTRUCTIONS",
        "exclude",
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    (life / "AGENTS.md").write_text("Mission-only project instructions.\n")
    worker = {
        "worker_id": "wrk_conversation_without_project_instructions",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    primary = Path(command[command.index("-C") + 1])
    assert primary != life
    assert primary.is_dir()
    assert not (primary / "AGENTS.md").exists()
    assert command[command.index("--add-dir") + 1] == str(life)


def test_host_capacity_reserves_an_independent_interactive_lane_per_cli_profile(
    tmp_path,
    monkeypatch,
):
    class ActiveProcess:
        @staticmethod
        def poll():
            return None

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    monkeypatch.setenv("WPR_HOST_CONVERSATION_SLOTS_PER_CLI", "1")
    mission = {
        "worker_id": "wrk_mission_busy",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    conversation = {
        "worker_id": "wrk_conversation_waiting",
        "trusted_run_lane": "conversation",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    runtime._host_active_slots()["mission"] = mission["worker_id"]
    runtime._active_processes[mission["worker_id"]] = ActiveProcess()

    assert runtime.worker_capacity_error(conversation) is None

    runtime._host_active_slots()["conversation"] = "wrk_conversation_active"
    runtime._active_processes["wrk_conversation_active"] = ActiveProcess()
    error = runtime.worker_capacity_error(conversation)
    assert error is not None
    assert "conversation lane is at capacity" in str(error)


def test_provider_activity_log_reads_incrementally_and_marks_a_bounded_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_LOG_WINDOW_BYTES", "1024")
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_provider_log",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    run_id = "run-provider-log"
    run_root = runtime.host_codex._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True)
    stdout = run_root / "stdout.log"
    stdout.write_text("\n".join(json.dumps({"type": "event", "index": i}) for i in range(80)) + "\n")

    profile, first = runtime.provider_activity_log(worker, run_id)
    stdout.write_text(stdout.read_text() + json.dumps({"type": "turn.completed"}) + "\n")
    _, second = runtime.provider_activity_log(worker, run_id)
    _, cached = runtime.provider_activity_log(worker, run_id)

    assert profile == "codex-cli"
    assert json.loads(first.splitlines()[0])["type"] == "glasshive.log_compacted"
    assert "turn.completed" in second
    assert cached == second


def test_provider_activity_log_reads_only_the_bound_native_attempt(tmp_path):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {"worker_id": "wrk_native_attempt", "profile": "codex-cli", "execution_mode": "host"}
    run_id = "run-native-attempt"
    for attempt, content in (("att-one", "first"), ("att-two", "second")):
        root = runtime.host_codex._attempt_run_root(worker["worker_id"], run_id, attempt)
        root.mkdir(parents=True)
        (root / "stdout.log").write_text(content)
    assert runtime.provider_activity_log(worker, run_id)[1] == ""
    first = runtime.provider_activity_log({**worker, "_provider_activity_attempt_id": "att-one"}, run_id)
    second = runtime.provider_activity_log({**worker, "_provider_activity_attempt_id": "att-two"}, run_id)
    assert first == ("codex-cli", "first")
    assert second == ("codex-cli", "second")


def test_host_conversation_broker_config_stays_in_private_worker_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", "false")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    source_codex_home = tmp_path / "source-codex"
    (source_codex_home / "skills").mkdir(parents=True)
    (source_codex_home / "plugins" / "cache").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    source_claude_home = tmp_path / "source-claude"
    (source_claude_home / "plugins" / "cache").mkdir(parents=True)
    (source_claude_home / "plugins" / "marketplaces").mkdir(parents=True)
    (source_claude_home / "plugins" / "installed_plugins.json").write_text("{}\n")
    monkeypatch.setenv("GLASSHIVE_HOST_CLAUDE_CONFIG", str(source_claude_home))
    life = tmp_path / "Life"
    life.mkdir()
    codex_runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-private-state"))
    codex_worker = {
        "worker_id": "wrk_conversation_codex_broker",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "codex_config_append": "[mcp_servers.synthetic]\nurl = \"http://127.0.0.1.invalid/mcp\"",
            }
        ),
    }
    codex_workspace = codex_runtime._host_workspace_dir(codex_worker)
    codex_runtime._materialize_workspace(codex_worker, codex_workspace)
    codex_command, codex_env = codex_runtime._build_command(
        codex_worker,
        "Use the declared tool.",
        codex_runtime._host_runtime_info(codex_worker),
    )

    codex_config = codex_runtime._host_codex_home(codex_worker) / "config.toml"
    assert codex_config.is_file()
    assert "mcp_servers.synthetic" in codex_config.read_text()
    assert codex_env["CODEX_HOME"] == str(codex_config.parent)
    assert (codex_config.parent / "skills").resolve() == (source_codex_home / "skills").resolve()
    assert (codex_config.parent / "plugins" / "cache").resolve() == (
        source_codex_home / "plugins" / "cache"
    ).resolve()
    assert Path(codex_command[0]).name == "codex"
    assert codex_command[1:4] == ["exec", "--json", "--skip-git-repo-check"]
    assert not (life / ".codex").exists()

    claude_runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-private-state"))
    claude_worker = {
        "worker_id": "wrk_conversation_claude_broker",
        "name": "Viventium Main",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "full",
                "developer_instructions": "Current Viventium authority.",
                "claude_project_mcp": {
                    "synthetic": {"type": "http", "url": "http://127.0.0.1.invalid/mcp"}
                },
            }
        ),
    }
    claude_workspace = claude_runtime._host_workspace_dir(claude_worker)
    claude_runtime._materialize_workspace(claude_worker, claude_workspace)
    claude_command, claude_env = claude_runtime._build_command(
        claude_worker,
        "Use the declared tool.",
        claude_runtime._host_runtime_info(claude_worker),
    )
    mcp_path = claude_runtime._state_dir(claude_worker["worker_id"]) / "conversation-mcp.json"

    assert mcp_path.is_file()
    assert claude_command[claude_command.index("--mcp-config") + 1] == str(mcp_path)
    assert claude_command.count("--strict-mcp-config") == 1
    settings = json.loads(claude_command[claude_command.index("--settings") + 1])
    assert settings["autoMemoryEnabled"] is False
    assert json.loads(mcp_path.read_text())["mcpServers"]["synthetic"]["url"] == (
        "http://127.0.0.1.invalid/mcp"
    )
    authority_path = (
        claude_runtime._state_dir(claude_worker["worker_id"])
        / "developer-instructions.txt"
    )
    assert claude_command[claude_command.index("--append-system-prompt-file") + 1] == str(
        authority_path
    )
    assert authority_path.read_text() == "Current Viventium authority."
    assert authority_path.stat().st_mode & 0o777 == 0o600
    assert claude_env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "claude-private-state"))
    claude_home = Path(claude_env["CLAUDE_CONFIG_DIR"])
    assert (claude_home / "plugins" / "cache").resolve() == (
        source_claude_home / "plugins" / "cache"
    ).resolve()
    assert (claude_home / "plugins" / "marketplaces").resolve() == (
        source_claude_home / "plugins" / "marketplaces"
    ).resolve()
    assert json.loads((claude_home / "plugins" / "installed_plugins.json").read_text()) == {}
    assert not (claude_home / "plugins" / "data").exists()
    assert not (life / ".mcp.json").exists()
    assert not (life / ".claude").exists()


def test_host_conversation_projects_agent_builder_control_schema_to_both_native_clis(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    life = tmp_path / "Life"
    life.mkdir()
    control = {
        "version": 1,
        "tools": [
            {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
            }
        ],
    }
    codex_runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-private-state"))
    codex_worker = {
        "worker_id": "wrk_codex_graph_control",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "agent_builder_control": control,
            }
        ),
    }

    codex_command, _ = codex_runtime._build_command(
        codex_worker,
        "Choose the next graph action.",
        codex_runtime._host_runtime_info(codex_worker),
    )

    assert "--output-schema" in codex_command
    codex_schema_path = Path(codex_command[codex_command.index("--output-schema") + 1])
    assert codex_schema_path.is_file()
    codex_schema = json.loads(codex_schema_path.read_text())
    assert codex_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert codex_schema["additionalProperties"] is False
    assert codex_schema["required"] == ["type", "content", "tool_name"]
    assert codex_schema["properties"]["tool_name"]["enum"] == [
        None,
        "lc_transfer_to_specialist",
    ]
    assert not (life / codex_schema_path.name).exists()
    codex_runtime._write_session_key(
        codex_worker["worker_id"],
        "synthetic-codex-session",
    )
    codex_resume_command, _ = codex_runtime._build_command(
        codex_worker,
        "Return to the graph.",
        codex_runtime._host_runtime_info(codex_worker),
    )
    assert codex_resume_command[1:3] == ["exec", "resume"]
    assert codex_resume_command.index("--output-schema") < codex_resume_command.index(
        "synthetic-codex-session"
    )

    claude_runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-private-state"))
    claude_worker = {
        **codex_worker,
        "worker_id": "wrk_claude_graph_control",
        "profile": "claude-code",
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "full",
                "agent_builder_control": control,
            }
        ),
    }

    claude_command, _ = claude_runtime._build_command(
        claude_worker,
        "Choose the next graph action.",
        claude_runtime._host_runtime_info(claude_worker),
    )

    assert "--json-schema" in claude_command
    claude_schema = json.loads(
        claude_command[claude_command.index("--json-schema") + 1]
    )
    assert "$schema" not in claude_schema
    assert claude_schema == {
        key: value for key, value in codex_schema.items() if key != "$schema"
    }
    assert claude_schema["additionalProperties"] is False
    assert claude_schema["required"] == ["type", "content", "tool_name"]
    assert claude_schema["properties"]["tool_name"]["enum"] == [
        None,
        "lc_transfer_to_specialist",
    ]
    claude_runtime._write_session_key(
        claude_worker["worker_id"],
        "synthetic-claude-session",
    )
    claude_resume_command, _ = claude_runtime._build_command(
        claude_worker,
        "Return to the graph.",
        claude_runtime._host_runtime_info(claude_worker),
    )
    assert claude_resume_command.index("--json-schema") < claude_resume_command.index(
        "--resume"
    )
    assert claude_resume_command[claude_resume_command.index("--resume") + 1] == (
        "synthetic-claude-session"
    )
    assert json.loads(
        claude_resume_command[claude_resume_command.index("--json-schema") + 1]
    ) == claude_schema


def test_host_mission_and_plain_conversation_commands_do_not_gain_graph_control_schema(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    life = tmp_path / "Life"
    life.mkdir()
    runtimes_and_flags = [
        (HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state")), "--output-schema"),
        (HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-state")), "--json-schema"),
    ]
    for index, (runtime, flag) in enumerate(runtimes_and_flags):
        profile = "codex-cli" if isinstance(runtime, HostCodexCliRuntime) else "claude-code"
        for run_mode in ("mission", "conversation"):
            worker = {
                "worker_id": f"wrk_no_graph_control_{index}_{run_mode}",
                "profile": profile,
                "execution_mode": "host",
                "trusted_run_lane": run_mode,
                "workspace_root": str(life),
                "model": "gpt-5.6-sol" if profile == "codex-cli" else "opus",
                "bootstrap_bundle_json": json.dumps({"run_mode": run_mode}),
            }
            command, _ = runtime._build_command(
                worker,
                "Continue normally.",
                runtime._host_runtime_info(worker),
            )
            assert flag not in command


def test_host_audio_eligible_conversation_projects_delivery_schema_to_both_native_clis(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    life = tmp_path / "Life"
    life.mkdir()
    delivery_control = {"version": 1, "audio_eligible": True}
    runtimes_and_flags = [
        (HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state")), "--output-schema"),
        (HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-state")), "--json-schema"),
    ]

    for index, (runtime, flag) in enumerate(runtimes_and_flags):
        profile = "codex-cli" if isinstance(runtime, HostCodexCliRuntime) else "claude-code"
        worker = {
            "worker_id": f"wrk_delivery_control_{index}",
            "profile": profile,
            "execution_mode": "host",
            "trusted_run_lane": "conversation",
            "workspace_root": str(life),
            "model": "gpt-5.6-sol" if profile == "codex-cli" else "opus",
            "bootstrap_bundle_json": json.dumps(
                {
                    "run_mode": "conversation",
                    "messaging_delivery_control": delivery_control,
                }
            ),
        }

        command, _ = runtime._build_command(
            worker,
            "Choose the messaging delivery disposition.",
            runtime._host_runtime_info(worker),
        )

        assert flag in command
        raw_schema = command[command.index(flag) + 1]
        schema = (
            json.loads(Path(raw_schema).read_text())
            if flag == "--output-schema"
            else json.loads(raw_schema)
        )
        assert schema["required"] == ["type", "content", "tool_name", "voice"]
        assert schema["properties"]["voice"]["enum"] == ["eligible", "skip"]


def test_host_capability_projection_adds_missing_entries_to_existing_worker_catalogs(
    tmp_path,
    monkeypatch,
):
    source_codex_home = tmp_path / "source-codex"
    source_skill = source_codex_home / "skills" / "synthetic-skill"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("# Synthetic skill\n")
    source_plugin = source_codex_home / "plugins" / "cache" / "synthetic-plugin-family"
    source_plugin.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_existing_catalog",
        "name": "Existing Main",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    target_home = runtime._host_codex_home(worker)
    existing_skill = target_home / "skills" / ".system"
    existing_skill.mkdir(parents=True)
    (existing_skill / "local-marker").write_text("preserve\n")
    existing_plugin = target_home / "plugins" / "cache" / "worker-local-family"
    existing_plugin.mkdir(parents=True)

    runtime._project_host_codex_capability_roots(target_home)

    assert (existing_skill / "local-marker").read_text() == "preserve\n"
    assert existing_plugin.is_dir()
    assert (target_home / "skills" / "synthetic-skill").is_symlink()
    assert (target_home / "skills" / "synthetic-skill").resolve() == source_skill.resolve()
    assert (target_home / "plugins" / "cache" / "synthetic-plugin-family").is_symlink()
    assert (
        target_home / "plugins" / "cache" / "synthetic-plugin-family"
    ).resolve() == source_plugin.resolve()


def test_claude_capability_projection_merges_registries_without_replacing_worker_choices(
    tmp_path,
    monkeypatch,
):
    source_home = tmp_path / "source-claude"
    source_plugins = source_home / "plugins"
    source_plugins.mkdir(parents=True)
    (source_plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "host-only": {"enabled": True},
                    "shared": {"source": "host"},
                },
            }
        )
    )
    (source_plugins / "known_marketplaces.json").write_text(
        json.dumps({"host-market": {"path": "/synthetic/host-market"}})
    )
    monkeypatch.setenv("GLASSHIVE_HOST_CLAUDE_CONFIG", str(source_home))

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    target_home = tmp_path / "worker-claude"
    target_plugins = target_home / "plugins"
    target_plugins.mkdir(parents=True)
    (target_plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    "worker-only": {"enabled": True},
                    "shared": {"source": "worker"},
                },
            }
        )
    )
    (target_plugins / "known_marketplaces.json").write_text(
        json.dumps({"worker-market": {"path": "/synthetic/worker-market"}})
    )

    runtime._project_host_claude_capability_roots(target_home)

    installed = json.loads((target_plugins / "installed_plugins.json").read_text())
    marketplaces = json.loads((target_plugins / "known_marketplaces.json").read_text())
    assert installed["version"] == 1
    assert installed["plugins"]["shared"] == {"source": "worker"}
    assert installed["plugins"]["worker-only"] == {"enabled": True}
    assert installed["plugins"]["host-only"] == {"enabled": True}
    assert marketplaces == {
        "host-market": {"path": "/synthetic/host-market"},
        "worker-market": {"path": "/synthetic/worker-market"},
    }


def test_codex_resume_flags_change_only_for_conversation_mode(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    conversation_worker = {
        "worker_id": "wrk_codex_conversation_resume",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "gpt-5.6-sol", "access_mode": "full"}
        ),
    }
    mission_worker = {
        **conversation_worker,
        "worker_id": "wrk_codex_mission_resume",
        "trusted_run_lane": "mission",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    runtime._ensure_dirs(conversation_worker["worker_id"])
    runtime._ensure_dirs(mission_worker["worker_id"])
    runtime._write_session_key(conversation_worker["worker_id"], "session-conversation")
    runtime._write_session_key(mission_worker["worker_id"], "session-mission")

    conversation_command, _ = runtime._build_command(
        conversation_worker,
        "Continue naturally.",
        runtime._host_runtime_info(conversation_worker),
    )
    mission_command, _ = runtime._build_command(
        mission_worker,
        "Continue the mission.",
        runtime._host_runtime_info(mission_worker),
    )

    assert Path(conversation_command[0]).name == "codex"
    assert conversation_command[1:5] == [
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
    ]
    assert Path(mission_command[0]).name == "codex"
    assert mission_command[1:3] == ["exec", "resume"]
    assert "--json" not in mission_command
    assert "--skip-git-repo-check" not in mission_command


def test_unconfigured_host_codex_conversation_omits_model_override(tmp_path, monkeypatch):
    for variable in (
        "WPR_MODEL_HOST_CODEX_CLI", "CODEX_MODEL", "WPR_MODEL_CODEX_CLI",
        "GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL",
        "WPR_HOST_CODEX_INHERIT_PROVIDER_MODEL",
    ):
        monkeypatch.delenv(variable, raising=False)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker = {
        "worker_id": "wrk_codex_native_default",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(workspace),
        "bootstrap_bundle_json": json.dumps({
            "run_mode": "conversation", "provider_model": "", "access_mode": "full",
        }),
    }
    assert runtime.resolve_model("codex-cli") == ""
    command, _ = runtime._build_command(
        worker, "Answer.", runtime._host_runtime_info(worker),
    )
    assert "-m" not in command
    assert "--model" not in command
    assert not any(str(value).startswith('model="') for value in command)


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "ultra"])
def test_host_codex_conversation_mode_honors_each_declared_effort(tmp_path, effort):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_codex_effort_{effort}",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "env": {"WPR_CODEX_CLI_REASONING_EFFORT": effort},
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert f'model_reasoning_effort="{effort}"' in command


def test_host_codex_workspace_access_limits_writes_without_full_bypass(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_workspace_access",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "workspace",
            }
        ),
    }

    first_command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "session-workspace")
    resumed_command, _ = runtime._build_command(
        worker,
        "Continue naturally.",
        runtime._host_runtime_info(worker),
    )

    # Current Codex CLIs removed --full-auto; the version-stable overrides keep
    # the same workspace-write sandbox without interactive approvals.
    assert "--full-auto" not in first_command
    assert 'sandbox_mode="workspace-write"' in first_command
    assert 'approval_policy="never"' in first_command
    assert "--dangerously-bypass-approvals-and-sandbox" not in first_command
    assert 'sandbox_mode="workspace-write"' in resumed_command
    assert 'approval_policy="never"' in resumed_command
    assert "--dangerously-bypass-approvals-and-sandbox" not in resumed_command


@pytest.mark.parametrize("chrome_enabled", ["0", "1"])
def test_host_claude_conversation_and_mission_keep_distinct_native_stream_inputs(
    tmp_path, monkeypatch, chrome_enabled
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", "false")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", chrome_enabled)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    conversation_worker = {
        "worker_id": "wrk_claude_conversation",
        "name": "Viventium Main",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "opus", "access_mode": "full"}
        ),
    }
    mission_worker = {
        **conversation_worker,
        "worker_id": "wrk_claude_mission",
        "trusted_run_lane": "mission",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }

    conversation_command, _ = runtime._build_command(
        conversation_worker,
        "Talk naturally.",
        runtime._host_runtime_info(conversation_worker),
    )
    mission_command, _ = runtime._build_command(
        mission_worker,
        "Run the mission.",
        runtime._host_runtime_info(mission_worker),
    )

    assert conversation_command[conversation_command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in conversation_command
    assert "--include-partial-messages" in conversation_command
    settings = json.loads(conversation_command[conversation_command.index("--settings") + 1])
    assert settings["autoMemoryEnabled"] is False
    assert "--strict-mcp-config" in conversation_command
    assert json.loads(conversation_command[conversation_command.index("--mcp-config") + 1]) == {
        "mcpServers": {}
    }
    if "--settings" in mission_command:
        mission_settings = json.loads(mission_command[mission_command.index("--settings") + 1])
        assert "autoMemoryEnabled" not in mission_settings
    assert "--strict-mcp-config" in mission_command
    assert ("--chrome" in conversation_command) == (chrome_enabled == "1")
    assert ("--chrome" in mission_command) == (chrome_enabled == "1")
    assert mission_command[mission_command.index("--output-format") + 1] == "stream-json"
    assert mission_command[mission_command.index("--input-format") + 1] == "stream-json"
    assert "--verbose" in mission_command
    assert "--include-partial-messages" not in mission_command


def test_host_claude_conversation_removes_cli_marker_created_in_life(tmp_path, monkeypatch):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_conversation_marker",
        "name": "Viventium Main",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "opus", "access_mode": "full"}
        ),
    }

    class MarkerCreatingProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            workspace = Path(kwargs["cwd"])
            (workspace / ".claude" / ".cc-writes").mkdir(parents=True)
            stdout = kwargs["stdout"]
            stdout.write(
                json.dumps(
                    {
                        "type": "result",
                        "result": "Conversation complete.",
                        "session_id": "session-marker-cleanup",
                    }
                )
                + "\n"
            )
            stdout.flush()

        def communicate(self, input=None, timeout=None):
            return None, None

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    runtime.ensure_worker_ready = lambda _worker: runtime._host_runtime_info(worker)  # type: ignore[method-assign]
    runtime._build_command = lambda _worker, _instruction, _info: (["claude"], {})  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "_process_identity_sha256", lambda _pid: "1" * 64)
    monkeypatch.setattr(runtime, "_process_group_identity", lambda pid: pid)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda _pid: "ps-lstart:Mon Jan 01 00:00:00 2024",
    )
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.Popen", MarkerCreatingProcess
    )

    result = runtime.run_task(
        worker,
        "Talk naturally.",
        timeout_sec=5,
        run_id="run_marker_cleanup",
    )

    assert result == "Conversation complete."
    assert not (life / ".claude").exists()


def test_host_claude_conversation_preserves_preexisting_workspace_content(tmp_path, monkeypatch):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    marker = life / ".claude" / ".cc-writes"
    marker.mkdir(parents=True)
    user_file = marker / "user-owned.txt"
    user_file.write_text("preserve me\n")
    worker = {
        "worker_id": "wrk_claude_conversation_preexisting",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    class SuccessfulProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            stdout = kwargs["stdout"]
            stdout.write(
                json.dumps(
                    {
                        "type": "result",
                        "result": "Conversation complete.",
                        "session_id": "session-preserve-workspace",
                    }
                )
                + "\n"
            )
            stdout.flush()

        def communicate(self, input=None, timeout=None):
            return None, None

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    runtime.ensure_worker_ready = lambda _worker: runtime._host_runtime_info(worker)  # type: ignore[method-assign]
    runtime._build_command = lambda _worker, _instruction, _info: (["claude"], {})  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "_process_identity_sha256", lambda _pid: "1" * 64)
    monkeypatch.setattr(runtime, "_process_group_identity", lambda pid: pid)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda _pid: "ps-lstart:Mon Jan 01 00:00:00 2024",
    )
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.Popen", SuccessfulProcess
    )

    runtime.run_task(worker, "Talk naturally.", run_id="run_preserve_workspace")

    assert user_file.read_text() == "preserve me\n"


def test_host_conversation_publishes_exact_startup_identity(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_conversation_startup_identity",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    observed: list[dict[str, object]] = []

    class SuccessfulProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            stdout = kwargs["stdout"]
            stdout.write(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "thread.started",
                                "thread_id": "thread-startup-identity",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "agent_message",
                                    "text": "READY",
                                },
                            }
                        ),
                        json.dumps({"type": "turn.completed"}),
                    ]
                )
                + "\n"
            )
            stdout.flush()

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    runtime.ensure_worker_ready = lambda _worker: runtime._host_runtime_info(worker)  # type: ignore[method-assign]
    runtime._build_command = lambda _worker, _instruction, _info: (["codex"], {})  # type: ignore[method-assign]
    runtime.set_run_start_observer(observed.append)
    monkeypatch.setattr(runtime, "_process_identity_sha256", lambda _pid: "1" * 64)
    monkeypatch.setattr(runtime, "_process_group_identity", lambda pid: pid)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda _pid: "ps-lstart:Mon Jan 01 00:00:00 2024",
    )
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.Popen",
        SuccessfulProcess,
    )

    result = runtime.run_task(
        worker,
        "Reply with only READY.",
        run_id="run_conversation_startup_identity",
    )

    assert result == "READY"
    assert observed == [
        {
            "worker_id": worker["worker_id"],
            "run_id": "run_conversation_startup_identity",
            "identity_kind": "host_process",
            "pid": SuccessfulProcess.pid,
            "process_group": SuccessfulProcess.pid,
            "process_start_identity": "ps-lstart:Mon Jan 01 00:00:00 2024",
            "container_id": "",
            "session_id": "conversation-run_conversa",
        }
    ]


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_host_claude_conversation_mode_honors_each_declared_effort(
    tmp_path, monkeypatch, effort
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setattr(
        HostClaudeCodeRuntime,
        "_effort_supported",
        lambda _self, _effort="": True,
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_claude_effort_{effort}",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "full",
                "env": {"WPR_CLAUDE_CODE_EFFORT": effort},
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert command[command.index("--effort") + 1] == effort


def test_host_claude_workspace_access_fails_closed_into_native_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_workspace_access",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "workspace",
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )
    settings = json.loads(command[command.index("--settings") + 1])

    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["filesystem"]["allowRead"] == [str(life.resolve())]


@pytest.mark.parametrize("chrome_enabled", ["0", "1"])
def test_host_claude_private_config_receives_subscription_auth_without_copying_user_config(
    tmp_path, monkeypatch, chrome_enabled
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", "false")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", chrome_enabled)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_SCOPES", "synthetic:unselected-parent")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.sys.platform", "darwin"
    )
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )

    def fake_run(command, **_kwargs):
        assert command[:4] == ["security", "find-generic-password", "-s", "Claude Code-credentials"]
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "synthetic-access-token",
                        "refreshToken": "synthetic-refresh-token",
                        "expiresAt": int((time.time() + 3600) * 1000),
                        "scopes": ["user:profile", "user:inference", "synthetic:future"],
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", fake_run
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_private_auth",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "opus", "access_mode": "full"}
        ),
    }

    command, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-access-token"
    assert env["CLAUDE_CODE_OAUTH_SCOPES"] == "user:profile user:inference synthetic:future"
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert ("--chrome" in command) == (chrome_enabled == "1")
    assert "--strict-mcp-config" in command
    assert json.loads(command[command.index("--settings") + 1])["autoMemoryEnabled"] is False
    assert not (life / ".claude").exists()


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (None, None),
        ([], None),
        ("user:profile user:inference", None),
        (["user:profile", 1], None),
        ([""], None),
        (["user:inference user:profile"], None),
        ([" user:profile"], None),
        (["user:profile\n"], None),
        (["user:profile\x00"], None),
        (["user:pr\u00f6file"], None),
        (["user:inference"], "user:inference"),
        (["synthetic:unknown"], "synthetic:unknown"),
        (["user:profile", "user:inference"], "user:profile user:inference"),
    ],
)
def test_host_claude_fresh_access_projects_only_valid_same_record_scopes(
    tmp_path, monkeypatch, scopes, expected
):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_SCOPES", "synthetic:unselected-parent")
    monkeypatch.setattr(
        profile_runtime_module,
        "_read_claude_keychain_oauth",
        lambda: {
            "accessToken": "synthetic-selected-access",
            "expiresAt": int((time.time() + 3600) * 1000),
            "scopes": scopes,
        },
    )

    def reject_managed_probe(*_args, **_kwargs):
        raise AssertionError("Fresh selected auth must not invoke another native boundary")

    monkeypatch.setattr(
        profile_runtime_module, "_claude_cli_managed_auth_available", reject_managed_probe
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    env = {
        "CLAUDE_CODE_OAUTH_SCOPES": "synthetic:orphaned-metadata",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "synthetic-unselected-refresh",
    }

    assert runtime._inject_private_subscription_auth(env) == "keychain_access_token"

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-selected-access"
    assert env.get("CLAUDE_CODE_OAUTH_SCOPES") == expected
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env


def test_host_claude_private_auth_prefers_explicit_environment_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-env-access")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "synthetic-env-refresh")
    def reject_security_query(*_args, **_kwargs):
        raise AssertionError("Keychain must not be queried when explicit auth is configured")

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", reject_security_query
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_env_auth",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({
            "run_mode": "conversation",
            "env": {"CLAUDE_CODE_OAUTH_SCOPES": "synthetic:unselected-bootstrap"},
        }),
    }

    _, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-env-access"
    assert "CLAUDE_CODE_OAUTH_SCOPES" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env


@pytest.mark.parametrize("session_key", [None, "synthetic-resume-session"])
@pytest.mark.parametrize("use_api_key", [False, True])
def test_host_claude_expired_keychain_token_uses_managed_auth_without_stale_override(
    tmp_path, monkeypatch, session_key, use_api_key
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_USE_API_KEY", "1" if use_api_key else "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    status_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        if command[:4] == ["security", "find-generic-password", "-s", "Claude Code-credentials"]:
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "synthetic-expired-access",
                            "refreshToken": "synthetic-valid-refresh",
                            "expiresAt": int((time.time() - 60) * 1000),
                        }
                    }
                ),
                stderr="",
            )
        assert command == ["/usr/bin/claude", "auth", "status"]
        status_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": ("ANTHROPIC_API_KEY" in kwargs["env"]) is use_api_key,
                    "authMethod": "claude.ai",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    monkeypatch.setattr(runtime, "_read_session_key", lambda _worker_id: session_key)
    worker = {
        "worker_id": "wrk_claude_expired_managed",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "env": {"ANTHROPIC_API_KEY": "synthetic-anthropic-key"},
            }
        ),
    }

    command, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert ("--resume" in command) is bool(session_key)
    assert status_envs == ([] if use_api_key else [env])
    assert (env.get("ANTHROPIC_API_KEY") == "synthetic-anthropic-key") is use_api_key
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert ("ANTHROPIC_API_KEY" in env) is use_api_key



def test_host_claude_expired_keychain_token_without_managed_auth_fails_before_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    status_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "synthetic-expired-access",
                            "refreshToken": "synthetic-refresh",
                            "expiresAt": int((time.time() - 60) * 1000),
                        }
                    }
                ),
                stderr="",
            )
        status_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout=json.dumps({"loggedIn": False}),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_expired_unmanaged",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    with pytest.raises(RuntimeErrorBase) as exc_info:
        runtime._build_command(worker, "Talk naturally.", runtime._host_runtime_info(worker))

    message = str(exc_info.value)
    assert "claude setup-token" in message
    assert "claude auth login" in message
    assert "synthetic-expired-access" not in message
    assert "synthetic-refresh" not in message
    failure = classify_runtime_error(exc_info.value, runtime_name="claude-code")
    assert failure.failure_class == "provider_auth_missing"
    assert failure.structured is True
    assert len(status_envs) == 2
    assert status_envs[0]["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert status_envs[1]["CLAUDE_CONFIG_DIR"] == status_envs[0]["CLAUDE_CONFIG_DIR"]
    assert status_envs[1]["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in status_envs[0]
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in status_envs[0]
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in status_envs[1]



@pytest.mark.parametrize("keychain_payload", ["not-json", json.dumps({})])
def test_host_claude_unusable_keychain_payload_fails_closed_without_managed_auth(
    tmp_path, monkeypatch, keychain_payload
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )

    def fake_run(command, **_kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=keychain_payload,
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout=json.dumps({"loggedIn": False}),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_unusable_keychain",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    with pytest.raises(RuntimeErrorBase, match="claude setup-token"):
        runtime._build_command(worker, "Talk naturally.", runtime._host_runtime_info(worker))



def test_host_claude_malformed_managed_auth_status_fails_closed_under_child_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    status_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="")
        status_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout="not-json",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_malformed_managed_auth",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    with pytest.raises(RuntimeErrorBase, match="claude setup-token"):
        runtime._build_command(worker, "Talk naturally.", runtime._host_runtime_info(worker))

    assert len(status_envs) == 2
    assert status_envs[0]["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert status_envs[1]["CLAUDE_CONFIG_DIR"] == status_envs[0]["CLAUDE_CONFIG_DIR"]
    assert status_envs[1]["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == ""



@pytest.mark.parametrize("session_key", [None, "synthetic-resume-session"])
@pytest.mark.parametrize("mcp_present", [False, True])
def test_host_claude_expired_access_uses_owner_managed_refresh_without_refresh_token(
    tmp_path, monkeypatch, session_key, mcp_present
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", "false")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "1")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "high")
    (tmp_path / "owner-home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "owner-home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-parent-only-key")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(tmp_path / "unselected-home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    status_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "synthetic-expired-access",
                            "refreshToken": "synthetic-owner-refresh",
                            "expiresAt": int((time.time() - 60) * 1000),
                            "scopes": ["user:profile", "user:inference"],
                        }
                    }
                ),
                stderr="",
            )
        assert command[-2:] == ["auth", "status"]
        status_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            returncode=0 if len(status_envs) == 2 else 1,
            stdout=json.dumps({"loggedIn": len(status_envs) == 2}),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    monkeypatch.setattr(runtime, "_effort_supported", lambda _effort: True)
    monkeypatch.setattr(runtime, "_read_session_key", lambda _worker_id: session_key)
    instructions = "Synthetic application authority."
    worker = {
        "worker_id": "wrk_claude_owner_managed_refresh",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({
            "developer_instructions": instructions,
            "env": {"CLAUDE_CODE_OAUTH_SCOPES": "synthetic:unselected-bootstrap"},
        }),
    }
    state_dir = runtime._state_dir(worker["worker_id"])
    state_dir.mkdir(parents=True, exist_ok=True)
    authority_path = state_dir / "developer-instructions.txt"
    authority_path.write_text(instructions)
    mcp_path = state_dir / "conversation-mcp.json"
    if mcp_present:
        runtime._write_host_claude_mcp_config(worker, mcp_path, {"native_tools": False}, {})

    command, env = runtime._build_command(
        worker, "Talk naturally.", runtime._host_runtime_info(worker)
    )

    assert env["HOME"] == str(tmp_path / "owner-home")
    assert env["CLAUDE_CONFIG_DIR"] == status_envs[0]["CLAUDE_CONFIG_DIR"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_SCOPES" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--chrome" in command
    assert "--strict-mcp-config" in command
    assert command[command.index("--mcp-config") + 1] == (
        str(mcp_path) if mcp_present else '{"mcpServers":{}}'
    )
    assert json.loads(command[command.index("--settings") + 1])["autoMemoryEnabled"] is False
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--append-system-prompt-file") + 1] == str(authority_path)
    assert ("--resume" in command) is bool(session_key)
    assert status_envs[0]["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert status_envs[1]["HOME"] == str(tmp_path / "owner-home")
    assert status_envs[1]["CLAUDE_CONFIG_DIR"] == status_envs[0]["CLAUDE_CONFIG_DIR"]
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in status_envs[1]
    assert "CLAUDE_CODE_OAUTH_SCOPES" not in status_envs[0]
    assert "CLAUDE_CODE_OAUTH_SCOPES" not in status_envs[1]
    assert "ANTHROPIC_API_KEY" not in status_envs[1]
    assert status_envs[1]["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == ""
    assert status_envs[1] == env



@pytest.mark.parametrize("session_key", [None, "synthetic-resume-session"])
def test_host_claude_signed_bootstrap_access_token_wins_without_auth_discovery(
    tmp_path, monkeypatch, session_key
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-ambient-access")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_SCOPES", "synthetic:unselected-parent")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "synthetic-ambient-refresh")

    def reject_auth_discovery(*_args, **_kwargs):
        raise AssertionError("Signed bootstrap auth must not query Keychain or Claude auth status")

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", reject_auth_discovery
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    monkeypatch.setattr(runtime, "_read_session_key", lambda _worker_id: session_key)
    worker = {
        "worker_id": "wrk_claude_signed_bootstrap_auth",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "env": {
                    "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-signed-bootstrap-access",
                    "CLAUDE_CODE_OAUTH_SCOPES": "user:inference",
                },
            }
        ),
    }

    command, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-signed-bootstrap-access"
    assert env["CLAUDE_CODE_OAUTH_SCOPES"] == "user:inference"
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert ("--resume" in command) is bool(session_key)


@pytest.mark.parametrize("validated", [False, True])
def test_host_claude_bound_account_is_resolved_before_owner_auth(tmp_path, monkeypatch, validated):
    monkeypatch.setenv("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", "false")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    account_home = str(tmp_path / "bound-account")
    worker = {
        "worker_id": "wrk_claude_bound_account",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({
            "provider_account": {"policy": "personal_required", "account_id": "acct_synthetic"},
            "env": {"CLAUDE_CODE_OAUTH_SCOPES": "synthetic:unselected-bootstrap"},
        }),
        "_glasshive_provider_account_bound": validated,
        "_glasshive_provider_account_env": {
            "CLAUDE_CONFIG_DIR": account_home,
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": account_home,
        },
    }

    def reject_owner_auth(_env):
        raise AssertionError("A selected account must not consult unrelated owner credentials")

    monkeypatch.setattr(runtime, "_inject_private_subscription_auth", reject_owner_auth)
    if not validated:
        with pytest.raises(RuntimeErrorBase, match="was not validated"):
            runtime._build_command(worker, "Use the selected account.", runtime._host_runtime_info(worker))
        return

    command, env = runtime._build_command(
        worker, "Use the selected account.", runtime._host_runtime_info(worker)
    )
    assert env["CLAUDE_CONFIG_DIR"] == account_home
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == account_home
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_SCOPES" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert "--strict-mcp-config" in command
    assert json.loads(command[command.index("--settings") + 1])["autoMemoryEnabled"] is False


@pytest.mark.parametrize("authority", ["missing", "access_token", "api_key"])
def test_host_claude_enterprise_conversation_never_consults_owner_auth(tmp_path, monkeypatch, authority):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_SCOPES", "synthetic:unselected-parent")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("WPR_CLAUDE_CODE_USE_API_KEY", "1" if authority == "api_key" else "0")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    projected = {
        "access_token": {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-server-access"},
        "api_key": {"ANTHROPIC_API_KEY": "synthetic-server-key"},
    }.get(authority, {})
    worker = {
        "worker_id": "wrk_claude_enterprise_auth",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"env": projected}),
    }

    def reject_owner_auth(_env):
        raise AssertionError("Enterprise conversation auth must remain server-owned")

    monkeypatch.setattr(runtime, "_inject_private_subscription_auth", reject_owner_auth)
    if authority == "missing":
        with pytest.raises(RuntimeErrorBase) as exc_info:
            runtime._build_command(worker, "Answer.", runtime._host_runtime_info(worker))
        assert classify_runtime_error(exc_info.value, runtime_name="claude-code").failure_class == "provider_auth_missing"
        return

    _, env = runtime._build_command(worker, "Answer.", runtime._host_runtime_info(worker))
    for key, value in projected.items():
        assert env[key] == value
    assert "CLAUDE_CODE_OAUTH_SCOPES" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env


def test_host_claude_bedrock_conversation_does_not_discover_subscription_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_bedrock_auth",
        "profile": "claude-code",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "workspace_root": str(tmp_path / "Life"),
        "model": "synthetic-bedrock-model",
        "bootstrap_bundle_json": json.dumps({
            "env": {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-competing-access"},
        }),
    }

    def reject_subscription_auth(_env):
        raise AssertionError("Configured Bedrock must not discover subscription credentials")

    monkeypatch.setattr(runtime, "_inject_private_subscription_auth", reject_subscription_auth)
    command, env = runtime._build_command(worker, "Answer.", runtime._host_runtime_info(worker))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert command[command.index("--model") + 1] == "synthetic-bedrock-model"


def test_host_mission_mode_retains_workspace_and_completion_contract(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    mission_root = tmp_path / "missions"
    worker = {
        "worker_id": "wrk_mission",
        "name": "Research Brief",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(mission_root),
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }

    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)
    instruction = runtime._command_stdin_text(worker, "Create the brief.", runtime._host_runtime_info(worker))

    assert workspace != mission_root
    assert workspace.is_relative_to(mission_root)
    assert (workspace / "project-definition.md").exists()
    assert (workspace / "work-log.md").exists()
    assert (workspace / "AGENTS.md").exists()
    assert "FINAL REPORT" in instruction


@pytest.mark.parametrize("runtime_class,profile,events", [
    (HostCodexCliRuntime, "codex-cli", [
        {"type": "thread.started", "thread_id": "native-mission-thread"},
        {"type": "item.completed", "item": {"type": "command_execution", "status": "completed", "exit_code": 0}},
    ]),
    (HostClaudeCodeRuntime, "claude-code", [
        {"type": "system", "session_id": "native-mission-thread"},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "done"}]}},
    ]),
])
def test_host_mission_publishes_native_progress_before_exit(tmp_path, monkeypatch, runtime_class, profile, events):
    runtime = runtime_class(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_live_mission", "profile": profile, "execution_mode": "host",
        "workspace_root": str(tmp_path / "missions"), "model": "configured-model",
        "_run_attempt_id": "attempt-live-mission",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    observed = []
    progress = threading.Event()
    before_exit = []

    def record(observation):
        observed.append(observation)
        if observation["kind"] == "meaningful_progress":
            progress.set()

    class InterruptedProcess:
        pid = 12345
        returncode = None

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            kwargs["stdout"].write("\n".join(json.dumps(event) for event in events) + "\n")
            kwargs["stdout"].flush()

        def wait(self, timeout=None):
            before_exit.append(progress.wait(timeout=1))
            self.returncode = 130
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(runtime, "ensure_worker_ready", lambda _: runtime._host_runtime_info(worker))
    monkeypatch.setattr(runtime, "_build_command", lambda *_: ([profile], {}))
    monkeypatch.setattr(runtime, "_process_identity_sha256", lambda _: "1" * 64)
    monkeypatch.setattr(runtime, "_process_group_identity", lambda pid: pid)
    monkeypatch.setattr(runtime, "_process_start_identity", lambda _: "synthetic-process-identity")
    monkeypatch.setattr(profile_runtime_module.subprocess, "Popen", InterruptedProcess)
    runtime.set_provider_liveness_observer(record)

    with pytest.raises(RuntimeErrorBase, match="exited with code 130"):
        runtime.run_task(worker, "Complete the assigned work.", run_id="run_live_mission")

    assert before_exit == [True]
    assert len(observed) == 1
    assert observed[0]["kind"] == "meaningful_progress"
    assert observed[0]["worker_id"] == worker["worker_id"]
    assert observed[0]["run_id"] == "run_live_mission"
    assert observed[0]["attempt_id"] == worker["_run_attempt_id"]
    assert observed[0]["model"] == worker["model"]
    assert observed[0]["runtime"] == profile
    assert runtime._read_session_key(worker["worker_id"]) == "native-mission-thread"
    assert not any(thread.is_alive() and thread.name == "glasshive-host-native-session-run_live_mis" for thread in threading.enumerate())


def test_collect_completed_run_issues_route_failure_evidence_for_quota_exhaustion(tmp_path):
    # A docker/base worker that exhausts its provider quota must record provider-native route
    # evidence like the host path does, so route health and the configured fallback can engage.
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {"worker_id": "wrk_quota", "name": "Worker", "profile": "codex-cli", "model": "gpt-5.6-sol"}
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_quota12345"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps({"type": "turn.started"}),
                json.dumps({
                    "type": "error",
                    "code": "usage_limit_reached",
                    "message": "You've hit your usage limit. Try again later.",
                }),
                json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["failure_class"] == "provider_quota_exhausted"
    assert recovered["failure_structured"] == 1
    evidence = runtime.consume_provider_route_failure_evidence(worker, {"run_id": run_id}, recovered)
    assert evidence is not None
    assert evidence["failure_structured"] is True
    assert evidence["failure_class"] == "provider_quota_exhausted"
    assert evidence["evidence_kind"] == "provider_native"


@pytest.mark.parametrize("stateless", [False, True])
def test_interrupted_host_conversation_retains_early_native_identity(tmp_path, monkeypatch, stateless):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_interrupted_conversation", "profile": "codex-cli",
        "execution_mode": "host", "trusted_run_lane": "conversation",
        "workspace_root": str(life), "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({
            "run_mode": "conversation", "provider_model": "gpt-5.6-sol",
            "env": {"GLASSHIVE_PROVIDER_SESSION_MODE": "stateless" if stateless else "persistent"},
        }),
    }
    runtime._ensure_dirs(worker["worker_id"])
    if stateless:
        runtime._write_session_key(worker["worker_id"], "unrelated-prior-session")
    build_command = runtime._build_command
    observed = threading.Event()
    remembered_before_exit = []
    remember = runtime._remember_native_session_key

    def observe_identity(current_worker, session_key):
        remember(current_worker, session_key)
        if session_key == "interrupted-native-thread":
            observed.set()

    class InterruptedProcess:
        pid = 12345
        returncode = None

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            kwargs["stdout"].write(json.dumps({
                "type": "thread.started", "thread_id": "interrupted-native-thread",
            }) + "\n")
            kwargs["stdout"].flush()

        def wait(self, timeout=None):
            remembered_before_exit.append(observed.wait(timeout=2))
            self.returncode = 130
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(runtime, "ensure_worker_ready", lambda _worker: runtime._host_runtime_info(worker))
    monkeypatch.setattr(runtime, "_build_command", lambda *_args: (["codex"], {}))
    monkeypatch.setattr(runtime, "_remember_native_session_key", observe_identity)
    monkeypatch.setattr(runtime, "_process_identity_sha256", lambda _pid: "1" * 64)
    monkeypatch.setattr(runtime, "_process_group_identity", lambda pid: pid)
    monkeypatch.setattr(runtime, "_process_start_identity", lambda _pid: "synthetic-process-identity")
    monkeypatch.setattr(profile_runtime_module.subprocess, "Popen", InterruptedProcess)
    instruction = "[message 1 user]\nPrepare the requested acceptance plan."
    with pytest.raises(RuntimeErrorBase, match="exited with code 130"):
        runtime.run_task(worker, instruction, run_id="run_interrupted_identity")

    assert remembered_before_exit == [True]
    expected = "unrelated-prior-session" if stateless else "interrupted-native-thread"
    assert runtime._read_session_key(worker["worker_id"]) == expected
    continuation = instruction + "\n[message 2 user]\nContinue"
    info = runtime._host_runtime_info(worker)
    resumed, _ = build_command(worker, continuation, info)
    assert runtime._command_stdin_text(worker, continuation, info) == continuation
    assert ("resume" in resumed) is not stateless
    if not stateless:
        assert "interrupted-native-thread" in resumed
    original_stdin = runtime._run_root(worker["worker_id"], "run_interrupted_identity") / "instruction.stdin"
    assert original_stdin.read_text() == instruction
    assert not any(
        thread.is_alive() and thread.name == "glasshive-conversation-session-run_interrup"
        for thread in threading.enumerate()
    )


def test_native_session_observer_preserves_newer_active_run_identity(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {"worker_id": "wrk_session_generation", "profile": "codex-cli"}
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "newer-native-thread")
    stdout = tmp_path / "older-stdout.jsonl"
    stdout.write_text(json.dumps({"type": "thread.started", "thread_id": "older-native-thread"}) + "\n")
    monkeypatch.setattr(runtime, "_read_active_session", lambda _: {
        "session_name": "newer-session", "run_id": "newer-run",
    })
    stopped = threading.Event()
    stopped.set()

    runtime._observe_native_session_events(
        worker["worker_id"], stdout, stopped, run_id="older-run", worker=worker,
    )

    assert runtime._read_session_key(worker["worker_id"]) == "newer-native-thread"


@pytest.mark.parametrize("configured,lane,resume,expected", [
    (None, "conversation", False, None),
    (None, "conversation", True, None),
    ("false", "conversation", False, False),
    ("false", "conversation", True, False),
    ("true", "conversation", True, True),
    ("false", "mission", False, None),
])
def test_host_claude_conversation_auto_memory_preserves_native_scope(
    tmp_path, monkeypatch, configured, lane, resume, expected
):
    policy_env = "WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY"
    monkeypatch.delenv(policy_env, raising=False)
    if configured is not None:
        monkeypatch.setenv(policy_env, configured)
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "default")
    monkeypatch.delenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", raising=False)
    monkeypatch.delenv("WPR_HOST_PLUGIN_DENYLIST", raising=False)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    monkeypatch.setattr(runtime, "_inject_private_subscription_auth", lambda env: None)
    monkeypatch.setattr(runtime, "_read_session_key", lambda worker_id: "native-session" if resume else None)
    worker = {
        "worker_id": "wrk_memory_scope", "profile": "claude-code", "execution_mode": "host",
        "trusted_run_lane": lane, "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation", "provider_model": "opus", "access_mode": "full"}),
    }
    memory = runtime._home_dir(worker["worker_id"]) / ".claude" / "projects" / "synthetic" / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_bytes(b"Original native file; preserve this evidence.\n")
    before = memory.read_bytes()
    command, env = runtime._build_command(worker, "Current user goal.", runtime._host_runtime_info(worker))
    settings = json.loads(command[command.index("--settings") + 1]) if "--settings" in command else {}
    assert settings.get("autoMemoryEnabled") is expected
    assert ("--resume" in command) is resume
    assert command[command.index("--model") + 1] == "opus"
    assert "--bare" not in command and "--tools" not in command
    assert memory.read_bytes() == before
    assert env["CLAUDE_CONFIG_DIR"] == str(runtime._home_dir(worker["worker_id"]) / ".claude")


def test_host_native_mcp_uses_owner_home_without_changing_worker_or_broker_home(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    owner_home = tmp_path / "owner-home"
    source_home = owner_home / ".codex"
    source_home.mkdir(parents=True)
    (source_home / "config.toml").write_text(
        '[mcp_servers.cua_repl]\ncommand = "/native/cua"\n'
        '[mcp_servers.node_repl]\ncommand = "/native/node"\n'
        '[mcp_servers.node_repl.env]\nHOME = "/explicit/native/home"\n'
    )
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.delenv("GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST", raising=False)
    monkeypatch.delenv("WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST", raising=False)
    config = tomllib.loads(runtime._host_codex_worker_config(
        '[mcp_servers.glasshive-user-capabilities]\nurl = "http://127.0.0.1:3080/mcp"\n'
    ))
    assert config["mcp_servers"]["cua_repl"]["env"]["HOME"] == str(owner_home)
    assert config["mcp_servers"]["node_repl"]["env"]["HOME"] == "/explicit/native/home"
    assert "env" not in config["mcp_servers"]["glasshive-user-capabilities"]
    worker = {"worker_id": "wrk_native_home", "profile": "codex-cli", "execution_mode": "host"}
    child_env = runtime._host_env(worker)
    assert child_env["HOME"] != str(owner_home)
    assert child_env["CODEX_HOME"] == str(Path(child_env["HOME"]) / ".codex")


@pytest.mark.parametrize("runtime_class", [HostCodexCliRuntime, HostClaudeCodeRuntime])
def test_native_branch_epoch_keeps_new_session_across_restart_without_reusing_sibling(runtime_class, tmp_path):
    state = tmp_path / "state"
    runtime = runtime_class(base_dir=str(state))
    worker = {"worker_id":"wrk_branch", "bootstrap_bundle_json":json.dumps({"env":{}})}
    runtime._remember_native_session_key(worker,"old-sibling-session")
    assert runtime._read_provider_session_key(worker) == "old-sibling-session"
    worker["bootstrap_bundle_json"] = json.dumps({"env":{"GLASSHIVE_PROVIDER_SESSION_EPOCH":"a" * 64}})
    assert runtime._provider_session_starts_fresh(worker)
    assert runtime._read_provider_session_key(worker) is None
    info = runtime._runtime_info(worker)
    assert info.session_key is None
    failed_session, _ = runtime._parse_output(worker, "", "Native admission unavailable", info)
    assert failed_session is None
    runtime._remember_native_session_key(worker,"selected-branch-session")
    restarted = runtime_class(base_dir=str(state))
    assert restarted._read_provider_session_key(worker) == "selected-branch-session"
    assert not restarted._provider_session_starts_fresh(worker)
    # The early event observer may have only the worker ID; it preserves the active branch epoch.
    restarted._write_session_key("wrk_branch","selected-branch-session")
    assert restarted._read_provider_session_key(worker) == "selected-branch-session"
    worker["bootstrap_bundle_json"] = json.dumps({"env":{"GLASSHIVE_PROVIDER_SESSION_EPOCH":"b" * 64}})
    assert restarted._provider_session_starts_fresh(worker)
    assert restarted._read_provider_session_key(worker) is None


def test_packaged_account_reconcile_seals_volume_home_without_host_bind(tmp_path, monkeypatch):
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "local-linux")
    account_home = tmp_path / "provider-accounts" / "acct_example"
    account_home.mkdir(parents=True)
    events = []
    homes = SimpleNamespace(
        assert_native_quiescent=lambda path: events.append(("quiescent", path)),
        tighten_permissions=lambda *, account_home: events.append(("sealed", account_home)),
    )
    runtime = object.__new__(ProfiledWorkerRuntime)
    runtime.provider_account_binder = SimpleNamespace(homes=homes)
    runtime.codex = SimpleNamespace(reconcile_provider_account_binding=lambda path: pytest.fail("Host bind path used"))
    runtime.reconcile_provider_account_binding(account_home)
    assert events == [("quiescent", account_home), ("sealed", account_home)]
