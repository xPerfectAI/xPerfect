"""O05: an exact host Stop also ends commands the provider started in a new session.

Codex runs shell commands in their own session, so they leave the run's process
group. Stopping only that group left the user's command running after
"You stopped this reply". Every descendant is still signalled only by its exact
PID start identity, never by an unscoped signal.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from workers_projects_runtime.profile_runtime import HostCodexCliRuntime

# The run's process starts one command in a new session (like Codex's command
# runner), records its PID, then keeps working.
_COMMAND = "import time; time.sleep(60)"
# Like `bash -c 'trap "" TERM; ...'`: only SIGKILL ends it.
_TERM_IGNORING_COMMAND = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
)


def _provider(command: str) -> str:
    return (
        "import pathlib, subprocess, sys, time; "
        f"command = subprocess.Popen([sys.executable, '-c', {command!r}],"
        " start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(command.pid)); "
        "time.sleep(60)"
    )


_PROVIDER = _provider(_COMMAND)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.upper().startswith("Z")


def _start_host_run(runtime, worker, run_id, tmp_path, provider=_PROVIDER):
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    exit_path = run_root / "exit_code"
    command_pid_path = run_root / "command.pid"
    process = subprocess.Popen(
        runtime._durable_host_process_command(
            [sys.executable, "-c", provider, str(command_pid_path)],
            run_root=run_root,
            exit_path=exit_path,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    runtime._wait_for_durable_host_supervisor(process, run_root=run_root)
    runtime._write_active_session(
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
    # Reconciliation adopts the durable generation and permits its start.
    assert runtime.reconcile_worker(worker).pid == process.pid
    deadline = time.monotonic() + 5
    while not command_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return process, int(command_pid_path.read_text())


def test_stop_ends_a_command_the_provider_started_in_its_own_session(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_escaped_command",
        "name": "Synthetic escaped command worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
    }
    run_id = "run_escaped_command"
    process, command_pid = _start_host_run(runtime, worker, run_id, tmp_path)
    try:
        assert os.getpgid(command_pid) == command_pid != os.getpgid(process.pid)
        assert _alive(command_pid)

        assert runtime._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        process.wait(timeout=5)

        assert not _alive(command_pid), "the provider's command outlived Stop"
    finally:
        for pid in (command_pid,):
            if _alive(pid):
                os.kill(pid, 9)
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_a_reused_descendant_pid_is_never_signalled(tmp_path, monkeypatch):
    """Identity changes between snapshot and signal: the PID is left alone."""

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_reused_descendant",
        "name": "Synthetic reused descendant worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
    }
    run_id = "run_reused_descendant"
    process, command_pid = _start_host_run(runtime, worker, run_id, tmp_path)
    original_identity = runtime._process_start_identity
    snapshots = []

    def identity(pid):
        value = original_identity(pid)
        if pid == command_pid:
            snapshots.append(value)
            # First read (the descendant snapshot) sees the real incarnation;
            # every later read sees a different one, as after PID reuse.
            return value if len(snapshots) == 1 else "ps-lstart:reused incarnation"
        return value

    monkeypatch.setattr(runtime, "_process_start_identity", identity)
    signalled = []
    real_kill = os.kill

    def recording_kill(pid, sig):
        if pid == command_pid and sig != 0:
            signalled.append(sig)
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", recording_kill)
    try:
        runtime._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        process.wait(timeout=5)
        assert signalled == []
        assert _alive(command_pid)
    finally:
        monkeypatch.undo()
        if _alive(command_pid):
            os.kill(command_pid, 9)
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_stop_escalates_a_term_ignoring_command_after_the_run_itself_exits(tmp_path):
    """Live O05 QA: the run exits on SIGTERM and its process record is reaped at
    once, while the remembered command ignores SIGTERM. Stop must still end the
    command with an exact SIGKILL instead of reporting an unfinished Stop."""

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_term_ignoring",
        "name": "Synthetic TERM-ignoring command worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
    }
    run_id = "run_term_ignoring"
    process, command_pid = _start_host_run(
        runtime, worker, run_id, tmp_path, provider=_provider(_TERM_IGNORING_COMMAND)
    )
    with runtime._process_lock:
        runtime._active_processes[worker["worker_id"]] = process

    def reap():  # the run thread forgets its process as soon as it exits
        process.wait()
        with runtime._process_lock:
            runtime._active_processes.pop(worker["worker_id"], None)

    threading.Thread(target=reap, daemon=True).start()
    try:
        started = time.monotonic()
        assert runtime._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        assert not _alive(command_pid), "the TERM-ignoring command outlived Stop"
        assert time.monotonic() - started < 12
        assert runtime._read_active_session(worker["worker_id"]) is None
    finally:
        if _alive(command_pid):
            os.kill(command_pid, 9)
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def _failed_first_stop(runtime, worker, run_id, tmp_path, monkeypatch):
    """A first Stop whose exact child signal fails: the session and ledger stay."""
    process, command_pid = _start_host_run(runtime, worker, run_id, tmp_path)
    real_kill = os.kill

    def deny_child(pid, sig):
        if pid == command_pid and sig != 0:
            raise PermissionError("synthetic temporary child signal failure")
        return real_kill(pid, sig)

    with monkeypatch.context() as patch:
        patch.setattr(os, "kill", deny_child)
        assert runtime._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id) is False
    process.wait(timeout=5)
    ledger = runtime._stop_descendant_ledger_path(worker["worker_id"])
    assert ledger.exists() and _alive(command_pid)
    return process, command_pid, ledger


@pytest.mark.parametrize("fault", ["unreadable", "invalid"])
def test_an_unknown_ledger_keeps_the_session_and_fence(tmp_path, monkeypatch, fault):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {"worker_id": "wrk_unknown_ledger", "name": "Synthetic", "profile": "codex-cli",
              "execution_mode": "host", "trusted_run_lane": "conversation"}
    process, command_pid, ledger = _failed_first_stop(runtime, worker, "run_unknown_ledger", tmp_path, monkeypatch)
    try:
        if fault == "unreadable":
            ledger.chmod(0o000)
        else:
            ledger.write_text("{unfinished")
        assert runtime._stop_active_process(worker["worker_id"], worker=worker, run_id="run_unknown_ledger") is False
        session = runtime._read_active_session(worker["worker_id"])
        assert session is not None and _alive(command_pid) and ledger.exists()
        # The shared release boundary refuses too.
        assert runtime._clear_active_session(worker["worker_id"], expected_session=session) is False
        assert runtime._read_active_session(worker["worker_id"]) is not None
    finally:
        ledger.chmod(0o600)
        if _alive(command_pid):
            os.kill(command_pid, 9)


def test_unconfirmed_start_cleanup_ends_a_remembered_child_before_release(tmp_path, monkeypatch):
    """The stale-lease path proves only the leader gone; the shared release
    boundary must still end the exact remembered child first."""
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {"worker_id": "wrk_stale_cleanup", "name": "Synthetic", "profile": "codex-cli",
              "execution_mode": "host", "trusted_run_lane": "conversation"}
    process, command_pid, ledger = _failed_first_stop(runtime, worker, "run_stale_cleanup", tmp_path, monkeypatch)
    session = runtime._read_active_session(worker["worker_id"])
    try:
        released = runtime.cleanup_unconfirmed_run_start(worker, "run_stale_cleanup", {
            "identity_kind": "host_process", "pid": session["process_pid"],
            "process_group": session["process_group"],
            "process_start_identity": session["process_start_identity"],
            "container_id": "", "session_id": session["session_name"],
        })
        assert released is True
        assert not _alive(command_pid), "released while the remembered child lived"
        assert runtime._read_active_session(worker["worker_id"]) is None and not ledger.exists()
    finally:
        if _alive(command_pid):
            os.kill(command_pid, 9)
