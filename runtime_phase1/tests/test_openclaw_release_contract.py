from __future__ import annotations

import json
from pathlib import Path

import pytest

from workers_projects_runtime.docker_sandbox import DockerSandboxManager
from workers_projects_runtime.openclaw_release import (
    OPENCLAW_RUNTIME_LOCK_SHA256,
    OPENCLAW_RUNTIME_OVERRIDES,
    OPENCLAW_RUNTIME_VERSION,
    require_reviewed_openclaw_version,
    reviewed_openclaw_env,
    stage_reviewed_openclaw_lock,
)
from workers_projects_runtime.profile_runtime import HostOpenClawRuntime, OpenClawWorkstationRuntime
from workers_projects_runtime.runtime_requirements import (
    host_runtime_requirement_issue,
    host_runtime_requirements_for,
)


def test_reviewed_openclaw_contract_is_exact_and_bonjour_is_forced_off():
    assert OPENCLAW_RUNTIME_VERSION == "2026.7.1-2"
    assert OPENCLAW_RUNTIME_LOCK_SHA256 == "b0cdcd1f4d842bebb20967dbdfb154d3725dabb510962240171a2cca67da4fde"
    assert OPENCLAW_RUNTIME_OVERRIDES == {
        "@hono/node-server": "2.0.12",
        "@modelcontextprotocol/sdk": "1.30.0",
        "brace-expansion": "5.0.9",
        "fast-uri": "3.1.4",
        "tar": "7.5.22",
    }
    assert reviewed_openclaw_env({"OPENCLAW_DISABLE_BONJOUR": "0"})["OPENCLAW_DISABLE_BONJOUR"] == "1"


@pytest.mark.parametrize(
    "reported",
    (
        "2026.7.1-2 (0790d9f)",
        "openclaw 2026.7.1-2 (0790d9f)",
    ),
)
def test_reviewed_openclaw_contract_accepts_only_the_selected_version(reported):
    assert require_reviewed_openclaw_version(reported) == "2026.7.1-2"


@pytest.mark.parametrize("reported", ("2026.2.9", "2026.7.1", "2026.7.2", "latest", ""))
def test_reviewed_openclaw_contract_rejects_version_drift(reported):
    with pytest.raises(RuntimeError, match="requires OpenClaw 2026.7.1-2"):
        require_reviewed_openclaw_version(reported)


def test_committed_openclaw_lock_contains_reviewed_tarball_and_override():
    runtime_phase1 = Path(__file__).resolve().parents[1]
    lock_root = runtime_phase1 / "runtime_locks" / "openclaw"
    package = json.loads((lock_root / "package.json").read_text())
    lock = json.loads((lock_root / "package-lock.json").read_text())

    assert package["dependencies"]["openclaw"] == (
        "https://registry.npmjs.org/openclaw/-/openclaw-2026.7.1-2.tgz"
    )
    assert package["overrides"] == OPENCLAW_RUNTIME_OVERRIDES
    assert lock["packages"]["node_modules/openclaw"]["integrity"] == (
        "sha512-ycF3yPcbjN6bUPeaUx6Mh6vze1hQWoD3CT/wWcmD7a8xaHHHRUaAlaq+lFxMHf1ssEgODVAwjlzYqp2twkYZ7g=="
    )
    for name, version in OPENCLAW_RUNTIME_OVERRIDES.items():
        installed_versions = {
            package["version"]
            for path, package in lock["packages"].items()
            if path.endswith(f"node_modules/{name}")
        }
        assert installed_versions == {version}


def test_reviewed_openclaw_lock_staging_can_refresh_read_only_prior_output(tmp_path):
    destination = tmp_path / "openclaw-runtime-lock"

    stage_reviewed_openclaw_lock(destination)
    for name in ("package.json", "package-lock.json"):
        (destination / name).chmod(0o444)

    stage_reviewed_openclaw_lock(destination)

    runtime_phase1 = Path(__file__).resolve().parents[1]
    source = runtime_phase1 / "runtime_locks" / "openclaw"
    for name in ("package.json", "package-lock.json"):
        assert (destination / name).read_bytes() == (source / name).read_bytes()


def test_runtime_sources_have_no_mutable_or_rejected_openclaw_install():
    runtime_phase1 = Path(__file__).resolve().parents[1]
    source_text = "\n".join(
        path.read_text(errors="ignore")
        for path in sorted((runtime_phase1 / "src").rglob("*.py"))
    )

    assert "openclaw@latest" not in source_text
    assert "WPR_SANDBOX_OPENCLAW_NPM_SPEC" not in source_text
    assert "2026.2.9" not in source_text


def test_host_openclaw_requirement_is_exact_not_minimum():
    requirements = host_runtime_requirements_for("openclaw-general", "openclaw")

    assert requirements
    assert all(requirement.get("exact_version") == "2026.7.1-2" for requirement in requirements)
    assert all("min_version" not in requirement for requirement in requirements)
    assert all("reviewed OpenClaw 2026.7.1-2" in requirement["recovery_hint"] for requirement in requirements)


def test_host_openclaw_requirement_rejects_even_a_newer_unreviewed_version(monkeypatch):
    monkeypatch.setattr("workers_projects_runtime.runtime_requirements.shutil.which", lambda _binary: "/tmp/openclaw")
    monkeypatch.setattr(
        "workers_projects_runtime.runtime_requirements.subprocess.run",
        lambda args, **kwargs: __import__("subprocess").CompletedProcess(
            args,
            returncode=0,
            stdout="2026.7.2 (unreviewed)\n",
            stderr="",
        ),
    )

    issue = host_runtime_requirement_issue("openclaw-general", "openclaw")

    assert issue is not None
    assert issue.problem == "version_mismatch_exact"
    assert issue.required_version == "2026.7.1-2"
    assert issue.actual_version == "2026.7.2"


def test_host_openclaw_requirement_accepts_selected_hyphenated_version(monkeypatch):
    monkeypatch.setattr("workers_projects_runtime.runtime_requirements.shutil.which", lambda _binary: "/tmp/openclaw")
    monkeypatch.setattr(
        "workers_projects_runtime.runtime_requirements.subprocess.run",
        lambda args, **kwargs: __import__("subprocess").CompletedProcess(
            args,
            returncode=0,
            stdout="2026.7.1-2 (0790d9f)\n",
            stderr="",
        ),
    )

    assert host_runtime_requirement_issue("openclaw-general", "openclaw") is None


def test_openclaw_workstation_and_host_launch_env_force_bonjour_off(tmp_path):
    workstation = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "workstation"))
    worker = {"worker_id": "wrk_openclaw_release", "profile": "openclaw-general"}
    workstation_env = workstation._gateway_env(worker)

    host = HostOpenClawRuntime(base_dir=str(tmp_path / "host"))
    host_info = host._host_runtime_info({**worker, "execution_mode": "host"})
    _command, host_env = host._build_command({**worker, "execution_mode": "host"}, "Test it.", host_info)

    assert workstation_env["OPENCLAW_DISABLE_BONJOUR"] == "1"
    assert host_env["OPENCLAW_DISABLE_BONJOUR"] == "1"


def test_workstation_container_rejects_unreviewed_openclaw_version(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._docker_exec = lambda *args, **kwargs: __import__("subprocess").CompletedProcess(  # type: ignore[method-assign]
        args,
        returncode=0,
        stdout="2026.7.2 (unreviewed)\n",
        stderr="",
    )

    with pytest.raises(RuntimeError, match="requires OpenClaw 2026.7.1-2"):
        manager.require_reviewed_openclaw("synthetic-container")


def test_workstation_image_is_checked_before_worker_creation(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []
    manager._ensure_image = lambda: None  # type: ignore[method-assign]

    def fake_docker(args, **kwargs):
        calls.append(args)
        return __import__("subprocess").CompletedProcess(
            args,
            returncode=0,
            stdout="OpenClaw 2026.7.2 (unreviewed)\n",
            stderr="",
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="requires OpenClaw 2026.7.1-2"):
        manager.require_reviewed_openclaw_image()

    assert calls == [
        [
            "run",
            "--rm",
            "--network",
            "none",
            "-e",
            "OPENCLAW_DISABLE_BONJOUR=1",
            "--entrypoint",
            "openclaw",
            manager.image,
            "--version",
        ]
    ]


def test_reviewed_workstation_image_check_is_cached_for_hot_resume(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    calls = 0

    def fake_docker(args, **kwargs):
        nonlocal calls
        calls += 1
        return __import__("subprocess").CompletedProcess(
            args,
            returncode=0,
            stdout="OpenClaw 2026.7.1-2 (0790d9f)\n",
            stderr="",
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    assert manager.require_reviewed_openclaw_image() == "2026.7.1-2"
    assert manager.require_reviewed_openclaw_image() == "2026.7.1-2"
    assert calls == 1


def test_all_openclaw_launcher_modules_apply_reviewed_runtime_environment():
    runtime_phase1 = Path(__file__).resolve().parents[1]
    source_root = runtime_phase1 / "src" / "workers_projects_runtime"

    for relative_path in ("runtime.py", "openclaw_runtime.py", "profile_runtime.py"):
        source = (source_root / relative_path).read_text()
        assert "reviewed_openclaw_env" in source, relative_path
