"""Saved exact-generation controls retained across native runtime modernization."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import pytest
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, WorkerInterruptedError, WorkerTerminatedError
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime

def _write_exact_finished_host_session(
    runtime: HostCodexCliRuntime,
    *,
    worker_id: str,
    run_id: str,
    suffix: str,
) -> dict[str, object]:
    attempt_id = f"att-{suffix}"
    attempt_root = runtime._attempt_run_root(worker_id, run_id, attempt_id)
    attempt_root.mkdir(parents=True, exist_ok=True)
    exit_path = attempt_root / "exit_code"
    exit_path.write_text("0")
    session = {
        "session_name": runtime._session_name_for_run_id(run_id),
        "run_id": run_id,
        "attempt_id": attempt_id,
        "exit_path": str(exit_path),
        "process_pid": 43101,
        "process_group": 43101,
        "process_start_identity": "ps-lstart:recorded-process",
        "owner_pid": 43102,
        "lease_pid": 43103,
        "lease_process_group": 43103,
        "lease_process_start_identity": "ps-lstart:recorded-lease",
        "started_at": "2026-08-31T12:00:00+00:00",
        "run_mode": "conversation",
    }
    assert runtime._write_active_session(worker_id, session)
    durable = runtime._read_active_session(worker_id)
    assert durable is not None
    return durable



@pytest.mark.parametrize("death_proof", ["absent", "pid_reused"])
def test_host_needs_input_release_without_lease_clears_only_proven_dead_exact_session(
    tmp_path, monkeypatch, death_proof
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / f"runtime-{death_proof}"))
    worker_id = f"wrk-host-release-{death_proof}"
    run_id = f"run_host_release_{death_proof}"
    _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=run_id,
        suffix=death_proof,
    )
    signals: list[tuple[str, int, int]] = []

    def probe_or_signal(pid: int, sig: int) -> None:
        if sig != 0:
            signals.append(("pid", pid, sig))
            return
        if death_proof == "absent" or pid == 43102:
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", probe_or_signal)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(runtime, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: {
            43101: "ps-lstart:replacement-process",
            43103: "ps-lstart:replacement-lease",
        }.get(pid, ""),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "bootstrap_bundle_json": json.dumps(
            {"env": {"GLASSHIVE_PROVIDER_SESSION_MODE": "stateless"}}
        ),
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
    }

    released = runtime.terminate_worker(worker)

    assert released.pid is None
    assert runtime._read_active_session(worker_id) is None
    assert signals == []



@pytest.mark.parametrize("observed_identity", ["ps-lstart:recorded-process", ""])
def test_host_needs_input_release_without_lease_never_signals_live_or_uncertain_exact_session(
    tmp_path, monkeypatch, observed_identity
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-live"))
    worker_id = "wrk-host-release-live"
    run_id = "run_host_release_live"
    original = _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=run_id,
        suffix="live",
    )
    signals: list[tuple[str, int, int]] = []

    def probe_or_signal(pid: int, sig: int) -> None:
        if sig != 0:
            signals.append(("pid", pid, sig))

    monkeypatch.setattr(os, "kill", probe_or_signal)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(runtime, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: observed_identity if pid == 43101 else "",
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
    }

    with pytest.raises(RuntimeErrorBase):
        runtime.terminate_worker(worker)

    assert runtime._active_session_fingerprint(
        runtime._read_active_session(worker_id)
    ) == runtime._active_session_fingerprint(original)
    assert signals == []



def test_pidless_host_release_requires_exact_terminal_artifact_before_absence(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-pidless"))
    worker_id = "wrk-host-release-pidless"
    run_id = "run_host_release_pidless"
    attempt_id = "att-pidless"
    session = {
        "session_name": runtime._session_name_for_run_id(run_id),
        "run_id": run_id,
        "attempt_id": attempt_id,
        "started_at": "2026-08-31T12:00:00+00:00",
        "run_mode": "conversation",
    }
    assert runtime._write_active_session(worker_id, session)
    original = runtime._read_active_session(worker_id)
    assert original is not None
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)) if sig else None,
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
    }

    rejected_without_artifact = False
    try:
        runtime.terminate_worker(worker)
    except RuntimeErrorBase:
        rejected_without_artifact = True

    assert runtime._active_session_fingerprint(
        runtime._read_active_session(worker_id)
    ) == runtime._active_session_fingerprint(original)
    assert signals == []
    assert rejected_without_artifact is True

    attempt_root = runtime._attempt_run_root(worker_id, run_id, attempt_id)
    attempt_root.mkdir(parents=True, exist_ok=True)
    exit_path = attempt_root / "exit_code"
    exit_path.write_text("0")
    assert runtime._write_active_session(
        worker_id,
        {**original, "exit_path": str(exit_path)},
        expected_session=original,
    )

    released = runtime.terminate_worker(worker)

    assert released.pid is None
    assert runtime._read_active_session(worker_id) is None
    assert signals == []



@pytest.mark.parametrize("operation", ["terminate_worker", "cleanup_orphaned_run"])
def test_host_cleanup_rejects_requested_run_when_another_session_owns_worker(
    tmp_path, monkeypatch, operation
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / f"runtime-{operation}"))
    worker_id = f"wrk-host-mismatch-{operation}"
    requested_run_id = "run_requested_generation"
    active_run_id = "run_different_generation"
    original = _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=active_run_id,
        suffix=operation,
    )
    signals: list[tuple[str, int, int]] = []

    def probe_or_signal(pid: int, sig: int) -> None:
        if sig != 0:
            signals.append(("pid", pid, sig))

    monkeypatch.setattr(os, "kill", probe_or_signal)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(runtime, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: "ps-lstart:recorded-process" if pid == 43101 else "",
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": requested_run_id,
    }

    rejected = False
    try:
        if operation == "terminate_worker":
            runtime.terminate_worker(worker)
        else:
            runtime.cleanup_orphaned_run(worker, requested_run_id)
    except RuntimeErrorBase:
        rejected = True

    assert runtime._active_session_fingerprint(
        runtime._read_active_session(worker_id)
    ) == runtime._active_session_fingerprint(original)
    assert signals == []
    assert rejected is True



@pytest.mark.parametrize("operation", ["terminate_worker", "cleanup_orphaned_run"])
def test_targeted_host_cleanup_without_session_never_signals_unbound_live_local_process(
    tmp_path,
    monkeypatch,
    operation,
):
    class LiveProcess:
        pid = 43201

        def __init__(self):
            self.returncode = None
            self.wait_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            _ = timeout
            self.wait_calls += 1
            self.returncode = -15
            return self.returncode

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / f"runtime-{operation}"))
    worker_id = f"wrk-host-unbound-local-{operation}"
    run_id = "run_requested_without_session"
    process = LiveProcess()
    runtime._register_process(worker_id, process)
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(runtime, "_process_start_identity", lambda _pid: "new-generation")
    monkeypatch.setattr(os, "getpgid", lambda _pid: process.pid)
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
    }

    with pytest.raises(RuntimeErrorBase):
        if operation == "terminate_worker":
            runtime.terminate_worker(worker)
        else:
            runtime.cleanup_orphaned_run(worker, run_id)

    assert process.poll() is None
    assert process.wait_calls == 0
    assert signals == []
    assert runtime._active_processes[worker_id] is process



def _unbound_live_process(pid: int):
    class LiveProcess:
        def __init__(self):
            self.pid = pid
            self.returncode = None
            self.wait_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            _ = timeout
            self.wait_calls += 1
            self.returncode = -15
            return self.returncode

    return LiveProcess()



def test_targeted_host_release_never_signals_process_registered_after_absence_check(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-racing-process"))
    worker_id = "wrk-host-racing-process"
    run_id = "run_requested_before_new_generation"
    process = _unbound_live_process(43211)
    signals: list[tuple[str, int, int]] = []
    original_status = runtime.host_active_process_status

    def status_then_new_generation(worker):
        status = original_status(worker)
        runtime._register_process(worker_id, process)
        return status

    monkeypatch.setattr(runtime, "host_active_process_status", status_then_new_generation)
    monkeypatch.setattr(runtime, "_process_start_identity", lambda _pid: "new-generation")
    monkeypatch.setattr(os, "getpgid", lambda _pid: process.pid)
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
    }

    with pytest.raises(RuntimeErrorBase):
        runtime.terminate_worker(worker)

    assert process.poll() is None
    assert process.wait_calls == 0
    assert signals == []
    assert runtime._active_processes[worker_id] is process



def test_targeted_host_release_never_signals_newer_process_than_stale_exact_session(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-stale-session"))
    worker_id = "wrk-host-stale-session-new-process"
    run_id = "run_stale_exact_session"
    _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=run_id,
        suffix="stale-session-new-process",
    )
    process = _unbound_live_process(43221)
    runtime._register_process(worker_id, process)
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(runtime, "_pid_is_live", lambda _pid: True)
    monkeypatch.setattr(runtime, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: {
            43101: "ps-lstart:replacement-old-pid",
            process.pid: "ps-lstart:new-generation",
        }.get(pid, ""),
    )
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
    }

    with pytest.raises(RuntimeErrorBase):
        runtime.terminate_worker(worker)

    assert process.poll() is None
    assert process.wait_calls == 0
    assert signals == []
    assert runtime._active_processes[worker_id] is process



def test_targeted_host_release_never_signals_same_run_replacement_before_stop(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-same-run-release"))
    worker_id = "wrk-host-same-run-release"
    run_id = "run_same_id_new_attempt_release"
    original = _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=run_id,
        suffix="same-run-release-old",
    )
    replacement_process = _unbound_live_process(43231)
    replacement = {
        **original,
        "attempt_id": "att-same-run-release-new",
        "session_name": "conversation-same-run-release-new",
        "process_pid": replacement_process.pid,
        "process_group": replacement_process.pid,
        "process_start_identity": "ps-lstart:same-run-release-new",
        "owner_pid": 43232,
        "lease_pid": None,
        "lease_process_group": None,
        "lease_process_start_identity": "",
    }
    signals: list[tuple[str, int, int]] = []
    original_receipt_writer = runtime._write_host_control_receipt
    replaced = False

    def write_receipt_then_replace(
        current_worker,
        *,
        active_session,
        run_id,
        operation,
        confirmed,
    ):
        nonlocal replaced
        receipt = original_receipt_writer(
            current_worker,
            active_session=active_session,
            run_id=run_id,
            operation=operation,
            confirmed=confirmed,
        )
        if not confirmed and not replaced:
            replaced = True
            assert runtime._write_active_session(
                worker_id,
                replacement,
                expected_session=original,
            )
            runtime._register_process(worker_id, replacement_process)
        return receipt

    monkeypatch.setattr(runtime, "_write_host_control_receipt", write_receipt_then_replace)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: {
            43101: "ps-lstart:recorded-process",
            replacement_process.pid: "ps-lstart:same-run-release-new",
        }.get(pid, ""),
    )
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
        "_host_run_lease": {
            "worker_id": worker_id,
            "run_id": run_id,
            "status": "active",
            "startup_state": "confirmed",
            "startup_identity_kind": "host_process",
            "pid": 43101,
            "process_group": 43101,
            "process_start_identity": "ps-lstart:recorded-process",
            "startup_session_id": original["session_name"],
        },
    }

    with pytest.raises(RuntimeErrorBase):
        runtime.terminate_worker(worker)

    assert replaced is True
    assert replacement_process.poll() is None
    assert replacement_process.wait_calls == 0
    assert signals == []
    assert runtime._active_session_fingerprint(
        runtime._read_active_session(worker_id)
    ) == runtime._active_session_fingerprint(replacement)
    assert runtime._active_processes[worker_id] is replacement_process



def test_targeted_host_release_revalidates_same_run_generation_before_sigkill(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-same-run-sigkill"))
    worker_id = "wrk-host-same-run-sigkill"
    run_id = "run_same_id_new_attempt_sigkill"
    original = _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=run_id,
        suffix="same-run-sigkill-old",
    )

    class TimeoutProcess:
        pid = 43235

        def __init__(self):
            self.wait_calls = 0
            self.on_timeout = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            assert self.on_timeout is not None
            self.on_timeout()
            raise subprocess.TimeoutExpired("synthetic-old-generation", timeout)

    old_process = TimeoutProcess()
    assert runtime._write_active_session(
        worker_id,
        {
            **original,
            "process_pid": old_process.pid,
            "process_group": old_process.pid,
            "process_start_identity": "ps-lstart:recorded-process",
        },
        expected_session=original,
    )
    original = runtime._read_active_session(worker_id) or {}
    replacement_process = _unbound_live_process(43236)
    replacement = {
        **original,
        "attempt_id": "att-same-run-sigkill-new",
        "session_name": "conversation-same-run-sigkill-new",
        "process_pid": replacement_process.pid,
        "process_group": replacement_process.pid,
        "process_start_identity": "ps-lstart:same-run-sigkill-new",
        "owner_pid": 43237,
        "lease_pid": None,
        "lease_process_group": None,
        "lease_process_start_identity": "",
    }
    runtime._register_process(worker_id, old_process)  # type: ignore[arg-type]
    signals: list[tuple[int, int]] = []
    replaced = False

    def replace_before_sigkill():
        nonlocal replaced
        assert replaced is False
        replaced = True
        assert runtime._write_active_session(
            worker_id,
            replacement,
            expected_session=original,
        )
        runtime._register_process(worker_id, replacement_process)

    old_process.on_timeout = replace_before_sigkill
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: {
            old_process.pid: "ps-lstart:recorded-process",
            replacement_process.pid: "ps-lstart:same-run-sigkill-new",
        }.get(pid, ""),
    )
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append((process_group, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
        "trusted_run_lane": "conversation",
        "compute_release_kind": "needs_input",
        "_active_run_id": run_id,
        "_host_run_lease": {
            "worker_id": worker_id,
            "run_id": run_id,
            "status": "active",
            "startup_state": "confirmed",
            "startup_identity_kind": "host_process",
            "pid": old_process.pid,
            "process_group": old_process.pid,
            "process_start_identity": "ps-lstart:recorded-process",
            "startup_session_id": original["session_name"],
        },
    }

    with pytest.raises(RuntimeErrorBase):
        runtime.terminate_worker(worker)

    assert replaced is True
    assert old_process.wait_calls == 1
    assert signals == [(old_process.pid, 15)]
    assert replacement_process.poll() is None
    assert replacement_process.wait_calls == 0
    assert runtime._active_session_fingerprint(
        runtime._read_active_session(worker_id)
    ) == runtime._active_session_fingerprint(replacement)
    assert runtime._active_processes[worker_id] is replacement_process



def test_host_orphan_cleanup_never_signals_same_run_replacement_before_stop(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-same-run-orphan"))
    worker_id = "wrk-host-same-run-orphan"
    run_id = "run_same_id_new_attempt_orphan"
    original = _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=run_id,
        suffix="same-run-orphan-old",
    )
    replacement_process = _unbound_live_process(43241)
    replacement = {
        **original,
        "attempt_id": "att-same-run-orphan-new",
        "session_name": "conversation-same-run-orphan-new",
        "process_pid": replacement_process.pid,
        "process_group": replacement_process.pid,
        "process_start_identity": "ps-lstart:same-run-orphan-new",
        "owner_pid": 43242,
        "lease_pid": None,
        "lease_process_group": None,
        "lease_process_start_identity": "",
    }
    signals: list[tuple[str, int, int]] = []

    def report_old_active_then_replace(_worker):
        assert runtime._write_active_session(
            worker_id,
            replacement,
            expected_session=original,
        )
        runtime._register_process(worker_id, replacement_process)
        return {"state": "active", "run_id": run_id}

    monkeypatch.setattr(runtime, "host_active_process_status", report_old_active_then_replace)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: {
            43101: "ps-lstart:recorded-process",
            replacement_process.pid: "ps-lstart:same-run-orphan-new",
        }.get(pid, ""),
    )
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "model": "test",
        "execution_mode": "host",
    }

    with pytest.raises(RuntimeErrorBase):
        runtime.cleanup_orphaned_run(worker, run_id)

    assert replacement_process.poll() is None
    assert replacement_process.wait_calls == 0
    assert signals == []
    assert runtime._active_session_fingerprint(
        runtime._read_active_session(worker_id)
    ) == runtime._active_session_fingerprint(replacement)
    assert runtime._active_processes[worker_id] is replacement_process



@pytest.mark.parametrize(
    ("observed_identity", "expects_signal", "expects_fence"),
    [
        ("ps-lstart:recorded-process", True, False),
        ("ps-lstart:replacement-process", False, False),
        ("", False, True),
    ],
)
def test_host_orphan_cleanup_signals_only_the_matching_recorded_generation(
    tmp_path,
    monkeypatch,
    observed_identity,
    expects_signal,
    expects_fence,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime-orphan"))
    worker_id = "wrk-host-orphan-cleanup"
    run_id = "run_host_orphan_cleanup"
    original = _write_exact_finished_host_session(
        runtime,
        worker_id=worker_id,
        run_id=run_id,
        suffix="orphan-cleanup",
    )
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(runtime, "_pid_is_live", lambda _pid: True)
    monkeypatch.setattr(runtime, "_pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: observed_identity if pid == 43101 else "",
    )
    monkeypatch.setattr(
        runtime,
        "_wait_for_recorded_process_exit",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(os, "getpgid", lambda _pid: 43101)
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append(("group", process_group, sig)),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)),
    )
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "runtime": "codex-cli",
        "execution_mode": "host",
    }

    if expects_fence:
        with pytest.raises(RuntimeErrorBase):
            runtime.cleanup_orphaned_run(worker, run_id)
    else:
        runtime.cleanup_orphaned_run(worker, run_id)

    assert bool(signals) is expects_signal
    if expects_fence:
        assert runtime._active_session_fingerprint(
            runtime._read_active_session(worker_id)
        ) == runtime._active_session_fingerprint(original)
    else:
        assert runtime._read_active_session(worker_id) is None



@pytest.mark.parametrize(
    ("operation", "expected_error"),
    [
        ("interrupt", WorkerInterruptedError),
        ("terminate", WorkerTerminatedError),
    ],
)
def test_host_external_stop_reason_is_typed_and_bound_to_exact_attempt(
    tmp_path,
    operation,
    expected_error,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = f"wrk_host_{operation}_attempt"
    run_id = "run_host_same_run_attempts"
    original_attempt_id = "att-host-original"
    replacement_attempt_id = "att-host-replacement"
    active_session = {
        "session_name": "host-same-run-original",
        "run_id": run_id,
        "attempt_id": original_attempt_id,
        "process_pid": 50501,
        "process_group": 50501,
        "process_start_identity": "ps-lstart:host-original-attempt",
        "owner_pid": 50502,
    }
    runtime._write_active_session(worker_id, active_session)
    worker = {
        "worker_id": worker_id,
        "profile": "codex-cli",
        "execution_mode": "host",
        "model": "gpt-5.6-sol",
        "_active_run_id": run_id,
        "_run_attempt_id": original_attempt_id,
        "_host_run_lease": {
            "worker_id": worker_id,
            "run_id": run_id,
            "status": "active",
            "startup_state": "confirmed",
            "startup_identity_kind": "host_process",
            "pid": active_session["process_pid"],
            "process_group": active_session["process_group"],
            "process_start_identity": active_session["process_start_identity"],
            "startup_session_id": active_session["session_name"],
        },
    }
    runtime._stop_active_process = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    runtime._write_stopped_active_run_evidence = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runtime._append_work_log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    if operation == "interrupt":
        runtime.interrupt_worker(worker, run_id=run_id)
    else:
        runtime.terminate_worker(worker)

    runtime._finalize_stop_reason(
        worker_id,
        run_id=run_id,
        attempt_id=replacement_attempt_id,
    )
    with pytest.raises(expected_error):
        runtime._finalize_stop_reason(
            worker_id,
            run_id=run_id,
            attempt_id=original_attempt_id,
        )



def test_host_unconfirmed_restart_cleanup_preserves_live_same_run_replacement_generation(
    tmp_path,
    monkeypatch,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk-host-start-cleanup-replacement",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    run_id = "run-host-cleanup-same-run"
    old_identity = {
        "identity_kind": "host_process",
        "pid": 7425,
        "process_group": 7425,
        "process_start_identity": "ps-lstart:old-host-generation",
        "container_id": "",
        "session_id": "host-old-generation",
    }

    class ReplacementProcess:
        pid = 7435

        def __init__(self):
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            _ = timeout
            self.wait_calls += 1
            raise AssertionError("replacement process must not be waited")

    replacement_process = ReplacementProcess()
    replacement_slot_token = runtime._acquire_host_slot(worker)
    replacement = {
        "session_name": "host-new-generation",
        "run_id": run_id,
        "attempt_id": "att-host-new-generation",
        "process_pid": replacement_process.pid,
        "process_group": replacement_process.pid,
        "process_start_identity": "ps-lstart:new-host-generation",
        "owner_pid": 7436,
        "host_slot_token": replacement_slot_token,
    }
    runtime._write_active_session(worker["worker_id"], replacement)
    runtime._register_process(worker["worker_id"], replacement_process)  # type: ignore[arg-type]
    monkeypatch.setattr(
        runtime,
        "_recorded_process_is_running",
        lambda pid, _identity: pid == replacement_process.pid,
    )
    monkeypatch.setattr(
        runtime,
        "_recorded_pid_is_proven_gone",
        lambda pid, _identity="": pid == old_identity["pid"],
    )

    assert runtime.cleanup_unconfirmed_run_start(
        worker,
        run_id,
        old_identity,
    ) is True

    assert runtime._active_session_fingerprint(
        runtime._read_active_session(worker["worker_id"])
    ) == runtime._active_session_fingerprint(replacement)
    assert runtime._active_processes[worker["worker_id"]] is replacement_process
    assert replacement_process.wait_calls == 0
    assert runtime._host_worker_lanes()[worker["worker_id"]] == "mission"
    assert runtime._host_slot_tokens()[worker["worker_id"]] == replacement_slot_token
    assert worker["worker_id"] in runtime._host_active_slots()["mission"]



def test_host_restart_exact_dead_session_releases_persisted_slot_without_local_maps(
    tmp_path,
):
    base_dir = tmp_path / "private-state"
    worker = {
        "worker_id": "wrk_restart_dead_slot",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    runtime1 = HostCodexCliRuntime(base_dir=str(base_dir))
    persisted_slot_token = runtime1._acquire_host_slot(worker)
    runtime1._write_active_session(
        worker["worker_id"],
        {
            "session_name": "host-restart-dead-slot",
            "run_id": "run_restart_dead_slot",
            "attempt_id": "att-restart-dead-slot",
            "process_pid": 55601,
            "process_group": 55601,
            "process_start_identity": "ps-lstart:restart-dead-slot",
            "owner_pid": 55602,
            "host_slot_token": persisted_slot_token,
        },
    )
    persisted_session = runtime1._read_active_session(worker["worker_id"])
    assert persisted_session is not None

    runtime2 = HostCodexCliRuntime(base_dir=str(base_dir))
    assert runtime2._host_active_slots() == {}
    assert runtime2._host_worker_lanes() == {}
    assert runtime2._host_slot_tokens() == {}
    runtime2._recorded_process_is_running = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    class ReplacementProcess:
        pid = 55611

        def __init__(self):
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            _ = timeout
            self.wait_calls += 1
            raise AssertionError("unrelated replacement process must not be waited")

    replacement_worker_id = "wrk_restart_unrelated_replacement"
    replacement_process = ReplacementProcess()
    runtime2._write_active_session(
        replacement_worker_id,
        {
            "session_name": "host-restart-unrelated-replacement",
            "run_id": "run_restart_unrelated_replacement",
            "attempt_id": "att-restart-unrelated-replacement",
            "process_pid": replacement_process.pid,
            "process_group": replacement_process.pid,
            "process_start_identity": "ps-lstart:restart-unrelated-replacement",
            "owner_pid": 55612,
            "host_slot_token": "replacement-slot-token",
        },
    )
    replacement_session = runtime2._read_active_session(replacement_worker_id)
    runtime2._register_process(replacement_worker_id, replacement_process)  # type: ignore[arg-type]

    assert runtime2._stop_active_process(
        worker["worker_id"],
        worker=worker,
        run_id="run_restart_dead_slot",
        expected_session=persisted_session,
        control_session=persisted_session,
    ) is True

    assert runtime2._read_active_session(worker["worker_id"]) is None
    assert runtime2._host_active_slots() == {}
    assert runtime2._host_worker_lanes() == {}
    assert runtime2._host_slot_tokens() == {}
    assert runtime2._active_session_fingerprint(
        runtime2._read_active_session(replacement_worker_id)
    ) == runtime2._active_session_fingerprint(replacement_session)
    assert runtime2._active_processes[replacement_worker_id] is replacement_process
    assert replacement_process.wait_calls == 0



@pytest.mark.parametrize("lane", ["conversation", "mission"])
def test_current_supervisor_finally_keeps_same_run_replacement(tmp_path, monkeypatch, lane):
    import sys
    import threading
    import time

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    runtime.set_run_start_observer(lambda _payload: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker = {
        "worker_id": "wrk-current-supervisor", "profile": "codex-cli", "model": "test",
        "execution_mode": "host", "trusted_run_lane": lane,
        "workspace_root": str(workspace), "bootstrap_bundle_json": json.dumps({"run_mode": lane}),
    }
    runtime._host_workspace_dir(worker).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runtime, "ensure_worker_ready", lambda current: runtime._host_runtime_info(current))
    monkeypatch.setattr(runtime, "_build_command", lambda *_args: (
        [sys.executable, "-c", "import time; time.sleep(0.5); print('synthetic result')"], dict(os.environ)))
    monkeypatch.setattr(runtime, "_parse_output", lambda _w, out, _err, _info: (None, out))
    errors = []
    def execute():
        try:
            runtime.run_task(worker, "Synthetic exact generation", run_id="run-current-supervisor")
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    original = None
    while time.monotonic() < deadline:
        original = runtime._read_active_session(worker["worker_id"])
        if original:
            break
        time.sleep(0.01)
    assert original is not None
    replacement_process = _unbound_live_process(55301)
    slot = runtime._acquire_host_slot(worker)
    replacement = {**original, "attempt_id": "att-replacement", "process_pid": replacement_process.pid,
                   "process_group": replacement_process.pid, "process_start_identity": "ps-lstart:replacement",
                   "process_identity_sha256": "replacement", "host_slot_token": slot}
    assert runtime._write_active_session(worker["worker_id"], replacement, expected_session=original)
    runtime._register_process(worker["worker_id"], replacement_process)
    thread.join(timeout=4)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeErrorBase)
    assert "ownership changed" in str(errors[0])
    assert runtime._active_processes[worker["worker_id"]] is replacement_process
    assert runtime._host_slot_tokens()[worker["worker_id"]] == slot
    assert runtime._active_session_fingerprint(runtime._read_active_session(worker["worker_id"])) == runtime._active_session_fingerprint(replacement)
    assert replacement_process.wait_calls == 0
