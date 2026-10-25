"""Instruction lineage stays in the core runtime, independent of optional tools."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from workers_projects_runtime.bootstrap import native_instruction_before_tail
from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime, CodexCliRuntime


def test_assembled_part_is_added_once_and_exact_tail_stays_last():
    raw = "Repeat the requested check. Repeat the requested check."
    tail = "The final declared instruction."
    bundle = {"application_context_format": "opaque_v1", "declared_developer_instruction_tail": tail}
    rendered = native_instruction_before_tail(bundle, raw + "\n\n" + tail, "Authorized capability fact.", require_exact_tail=True)
    again = native_instruction_before_tail(bundle, rendered, "Authorized capability fact.", require_exact_tail=True)
    # The helper deduplicates complete assembled parts, not arbitrary repeated prose.
    assert raw in rendered and rendered.endswith(tail)
    assert rendered.count("Authorized capability fact.") == 1
    # Reassembly is a caller error: the exact resulting bytes must expose it in lineage.
    assert hashlib.sha256(rendered.encode()).digest() != hashlib.sha256(again.encode()).digest()
    with pytest.raises(ValueError, match="declared tail"):
        native_instruction_before_tail(bundle, raw, "fact", require_exact_tail=True)


def test_actual_attempt_stdin_retains_original_text_and_separate_context(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "synthetic-cli"
        worker_root_name = "lineage"
        def resolve_model(self, _profile):
            return "declared-model"
        def _build_command(self, _worker, _instruction, _info):
            return ["synthetic-cli", "-"], {}
        _command_stdin_text = CodexCliRuntime._command_stdin_text
        def _parse_output(self, _worker, stdout, _stderr, _info):
            return None, stdout.strip()

    runtime = CaptureRuntime(base_dir=str(tmp_path / "runtime"))
    runtime.set_run_start_observer(lambda _payload: None)
    sandbox = runtime.sandbox
    monkeypatch.setattr(sandbox, "ensure_ready", lambda *_args, **_kwargs: SimpleNamespace(container_name="synthetic-box", pid=123))
    monkeypatch.setattr(sandbox, "inspect", lambda *_args: None)
    monkeypatch.setattr(sandbox, "list_screen_sessions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sandbox, "_ensure_container_writable_paths", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox, "ensure_container_writable_paths", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox, "screen_session_pid", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(sandbox, "stop_screen_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox, "terminate_run_processes", lambda *_args, **_kwargs: None)
    consumed = []
    run_id = "run-lineage"

    def consume(worker_id, _runtime_name, _session_name, _command, *, env=None, worker=None):
        root = runtime._attempt_run_root(worker_id, run_id, worker["_run_attempt_id"])
        stdin = root / "instruction.stdin"
        with stdin.open("rb") as handle:
            result = subprocess.run([sys.executable, "-c", "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"],
                                    stdin=handle, capture_output=True, check=True, timeout=5)
        consumed.append((worker["_run_attempt_id"], stdin.read_bytes(), result.stdout.decode().strip()))
        (root / "stdout.log").write_text("FINAL REPORT:\nSynthetic input consumed.")
        (root / "stderr.log").write_text("")
        (root / "exit_code").write_text("0")
        assert stdin.stat().st_mode & 0o777 == 0o600
        return subprocess.CompletedProcess(["synthetic-cli"], 0, "", "")

    monkeypatch.setattr(sandbox, "start_screen_session", consume)
    raw = "Repeat this source sentence. Repeat this source sentence."
    instructions = [raw, "Authorized parent context: synthetic context token.\n\n" + raw]
    for number, instruction in enumerate(instructions, 1):
        worker = {"worker_id": "worker-lineage", "name": "Lineage", "profile": "synthetic-cli", "_run_attempt_id": f"attempt-{number}"}
        assert runtime.run_task(worker, instruction, run_id=run_id).startswith("FINAL REPORT:")
    assert len(consumed) == 2
    for index, (attempt, native_bytes, native_hash) in enumerate(consumed):
        assert attempt == f"attempt-{index + 1}"
        assert instructions[index].encode() in native_bytes
        assert native_bytes.count(raw.encode()) == 1
        assert hashlib.sha256(native_bytes).hexdigest() == native_hash
        assert hashlib.sha256(raw.encode()).hexdigest() != native_hash
    assert b"synthetic context token" not in consumed[0][1]
    assert b"synthetic context token" in consumed[1][1]
    assert consumed[0][2] != consumed[1][2]
