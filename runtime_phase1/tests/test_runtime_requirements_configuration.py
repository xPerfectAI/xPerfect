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
