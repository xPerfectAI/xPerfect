from __future__ import annotations

import pytest

from workers_projects_runtime.runtime_requirements import (
    DEFAULT_HOST_RUNTIME_REQUIREMENTS,
    RuntimeRequirementConfigurationError,
    host_runtime_requirements_for,
)


def test_builtin_host_floors_remain_compatible_while_fresh_images_are_pinned_separately():
    codex = DEFAULT_HOST_RUNTIME_REQUIREMENTS["codex-cli"][0]
    claude = DEFAULT_HOST_RUNTIME_REQUIREMENTS["claude-code"][0]

    assert codex["min_version"] == "0.144.1"
    assert claude["min_version"] == "2.1.178"
    assert "--effort" in claude["required_help_flags"]
    assert "install latest" in claude["recovery_hint"]


def test_invalid_inline_requirements_fail_loud(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", "{not-json")

    with pytest.raises(RuntimeRequirementConfigurationError, match="must contain valid JSON"):
        host_runtime_requirements_for("codex-cli", "codex")


def test_invalid_requirements_file_fails_loud(monkeypatch, tmp_path):
    source = tmp_path / "requirements.json"
    source.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", str(source))

    with pytest.raises(RuntimeRequirementConfigurationError, match="readable valid JSON"):
        host_runtime_requirements_for("codex-cli", "codex")


def test_scalar_requirements_override_fails_loud(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", '"disabled"')

    with pytest.raises(RuntimeRequirementConfigurationError, match="JSON object or array"):
        host_runtime_requirements_for("codex-cli", "codex")


def test_explicit_empty_requirements_remain_supported(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", "{}")

    assert host_runtime_requirements_for("codex-cli", "codex") == []


def _short_flush_cli(tmp_path, *, flags="--effort --chrome --tools --permission-mode dontAsk --no-chrome --strict-mcp-config --setting-sources --json-schema", exit_code=0):
    """A native CLI may exit after a partial nonblocking pipe write."""
    import sys

    binary = tmp_path / "synthetic-cli"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('3.0.0', flush=True)\n"
        "    raise SystemExit(0)\n"
        f"data = b'x' * (1024 * 1024) + {flags.encode()!r}\n"
        "os.set_blocking(1, False)\n"
        "os.write(1, data)\n"
        f"os._exit({exit_code})\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def test_native_help_flags_survive_short_pipe_flush(tmp_path, monkeypatch):
    import json
    import subprocess
    from workers_projects_runtime.runtime_requirements import host_runtime_requirement_issue

    binary = _short_flush_cli(tmp_path)
    pipe = subprocess.run([str(binary), "--help"], capture_output=True, text=True, timeout=5)
    assert pipe.returncode == 0
    assert "--effort" not in pipe.stdout
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", json.dumps({
        "claude-code": [{"binary": str(binary), "required_help_flags": ["--effort"]}],
    }))

    assert host_runtime_requirement_issue("claude-code", "claude-code") is None


def test_host_claude_preflight_reads_full_help_before_accepting_high_and_chrome(tmp_path, monkeypatch):
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime

    binary = _short_flush_cli(tmp_path)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(binary))
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "high")
    monkeypatch.delenv("WPR_CLAUDE_CODE_ENABLE_CHROME", raising=False)
    for name in ("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", "WPR_HOST_RUNTIME_REQUIREMENTS_JSON",
                 "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", "WPR_HOST_RUNTIME_REQUIREMENTS_FILE"):
        monkeypatch.delenv(name, raising=False)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime, "_inject_private_subscription_auth", lambda env: None)

    runtime.preflight_worker_profile("claude-code", "host")
    from workers_projects_runtime.profile_runtime import _claude_read_only_help_supports
    assert _claude_read_only_help_supports(runtime._help_text())


@pytest.mark.parametrize("flags,exit_code", [("--chrome", 0), ("--effort --chrome", 7)])
def test_required_flags_and_nonzero_exit_remain_fail_closed(tmp_path, monkeypatch, flags, exit_code):
    import json
    from workers_projects_runtime.runtime_requirements import host_runtime_requirement_issue

    binary = _short_flush_cli(tmp_path, flags=flags, exit_code=exit_code)
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", json.dumps({
        "claude-code": [{"binary": str(binary), "required_help_flags": ["--effort"]}],
    }))
    issue = host_runtime_requirement_issue("claude-code", "claude-code")

    assert issue is not None
    assert issue.problem == "capability_missing"


def test_runtime_probe_closes_capture_files_after_timeout(tmp_path, monkeypatch):
    import subprocess
    import sys
    import workers_projects_runtime.runtime_requirements as requirements

    captures = []
    temporary_file = requirements.tempfile.TemporaryFile

    def track_capture(*args, **kwargs):
        capture = temporary_file(*args, **kwargs)
        captures.append(capture)
        return capture

    monkeypatch.setattr(requirements.tempfile, "TemporaryFile", track_capture)
    with pytest.raises(subprocess.TimeoutExpired):
        requirements.capture_runtime_probe(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05,
        )

    assert len(captures) == 2
    assert all(capture.closed for capture in captures)


def test_requirement_probe_timeout_remains_unverified(monkeypatch):
    import subprocess
    import workers_projects_runtime.runtime_requirements as requirements

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(requirements, "capture_runtime_probe", timeout)
    issue = requirements._run_requirement_probe(
        ["synthetic-cli", "--help"], required={"--effort"}, binary="synthetic-cli",
        label="Synthetic CLI", profile="synthetic", runtime_name="synthetic",
        recovery_hint="", probe_label="help output",
    )

    assert issue is not None
    assert issue.problem == "capability_unverified"


def test_runtime_probe_preserves_separate_stdout_stderr_and_exit():
    import sys
    from workers_projects_runtime.runtime_requirements import capture_runtime_probe

    command = [sys.executable, "-c", "import sys; print('output'); print('error', file=sys.stderr); sys.exit(4)"]
    result = capture_runtime_probe(command, timeout=5)

    assert result.args == command
    assert result.returncode == 4
    assert result.stdout == "output\n"
    assert result.stderr == "error\n"
