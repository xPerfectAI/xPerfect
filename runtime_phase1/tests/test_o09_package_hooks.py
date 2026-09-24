"""O09.c package hooks: contained homes reach support checks, hosted keys never
fall back to the shared broker, and native runs read keys with the minimal reader."""
from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import workers_projects_runtime.api as api_module
from workers_projects_runtime import native_key_reader
from workers_projects_runtime.profile_runtime import CodexCliRuntime

RUNTIME_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT", str(tmp_path / "accounts"))
    with TestClient(api_module.create_app(db_path=str(tmp_path / "api.sqlite"), runtime_backend="stub")) as test_client:
        yield test_client


def test_health_and_create_check_support_with_the_provisioned_homes(client, monkeypatch):
    calls = []

    def recording_support(**kwargs):
        calls.append(kwargs)
        return "unavailable"

    monkeypatch.setattr(api_module, "provider_platform_support", recording_support)
    monkeypatch.setattr(api_module, "native_api_keys_enabled", lambda: True)
    homes = client.app.state.provider_setup.homes
    assert client.get("/health").status_code == 200
    key_checks = [call for call in calls if call.get("auth_method") == "api_key"]
    assert key_checks and all(call.get("native_homes") is homes for call in key_checks)
    calls.clear()
    response = client.post("/v1/provider-accounts", json={"provider": "codex", "label": "Synthetic",
                                                           "auth_method": "subscription", "platform_support": "ignored"})
    assert response.status_code == 409  # the recorder says the route is unavailable
    assert calls and calls[-1].get("native_homes") is homes


@pytest.mark.parametrize("security_mode,refused", [("multi_user", True), ("local", False)])
def test_hosted_native_keys_refuse_a_client_chosen_broker(client, monkeypatch, security_mode, refused):
    monkeypatch.setattr(api_module, "native_api_keys_enabled", lambda: True)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", security_mode)
    response = client.post("/v1/provider-accounts", json={
        "provider": "codex", "label": "Synthetic", "auth_method": "api_key",
        "secret_locator": "broker://shared-openai", "platform_support": "ignored"})
    if refused:
        assert response.status_code == 409 and "contained account" in response.text
    else:
        assert "contained account" not in response.text


def test_native_runs_read_the_key_with_the_minimal_reader():
    source = inspect.getsource(CodexCliRuntime.run_task)
    assert "-m workers_projects_runtime.native_key_reader" in source
    assert "-m workers_projects_runtime.native_api_keys" not in source
    recipe = (RUNTIME_ROOT / "containers" / "Dockerfile.shared").read_text()
    assert "src/workers_projects_runtime/native_key_reader.py" in recipe
    assert 'import workers_projects_runtime.native_key_reader' in recipe


def test_the_reader_runs_with_only_itself_like_the_native_image(tmp_path):
    """No control plane in the image: the module alone must load and keep its CLI contract."""
    package = tmp_path / "site" / "workers_projects_runtime"
    package.mkdir(parents=True)
    shutil.copy2(native_key_reader.__file__, package / "native_key_reader.py")
    usage = subprocess.run([sys.executable, "-I", "-m", "workers_projects_runtime.native_key_reader"],
                           cwd=tmp_path / "site", capture_output=True, text=True)
    assert usage.returncode == 2 and "ModuleNotFoundError" not in usage.stderr
    missing = subprocess.run([sys.executable, "-I", "-m", "workers_projects_runtime.native_key_reader",
                              str(tmp_path / "absent")], cwd=tmp_path / "site", capture_output=True, text=True)
    assert missing.returncode == 1 and missing.stdout == ""
