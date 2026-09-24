"""Standalone UI must not silently import another product's runtime state."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

import glass_drive_ui.server as server


def test_create_app_does_not_load_viventium_app_support_by_default(tmp_path, monkeypatch):
    default_dir = tmp_path / "Library" / "Application Support" / "Viventium" / "runtime"
    default_dir.mkdir(parents=True)
    (default_dir / "runtime.env").write_text("GLASSHIVE_PUBLIC_LINKS_ONLY=true\n")
    (default_dir / "runtime.local.env").write_text("GLASSHIVE_SIGNED_LINK_SECRET=foreign-secret\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIVENTIUM_ENV_FILE", raising=False)
    monkeypatch.delenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("GLASSHIVE_PUBLIC_LINKS_ONLY", raising=False)
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)

    for key in ("GLASSHIVE_ENTERPRISE_MODE","WPR_ENTERPRISE_MODE","GLASSHIVE_SECURITY_MODE"):
        monkeypatch.delenv(key,raising=False)
    server.create_app(runtime_client=object())

    assert "GLASSHIVE_PUBLIC_LINKS_ONLY" not in os.environ
    assert "GLASSHIVE_SIGNED_LINK_SECRET" not in os.environ


def test_ui_explicit_viventium_env_file_still_loads(tmp_path, monkeypatch):
    explicit = tmp_path / "selected.env"
    explicit.write_text("GLASSHIVE_PUBLIC_LINKS_ONLY=true\n")
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(explicit))
    monkeypatch.setenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "1")
    monkeypatch.delenv("GLASSHIVE_PUBLIC_LINKS_ONLY", raising=False)

    try:
        server._load_viventium_runtime_env()
        assert os.environ["GLASSHIVE_PUBLIC_LINKS_ONLY"] == "true"
    finally:
        # The loader intentionally writes process env for the packaged runtime;
        # restore this direct write so later tests cannot inherit public-link mode.
        os.environ.pop("GLASSHIVE_PUBLIC_LINKS_ONLY", None)


def test_connect_ai_reports_xperfect_open_source_license(monkeypatch):
    monkeypatch.delenv("VIVENTIUM_ENV_FILE", raising=False)
    monkeypatch.delenv("GLASSHIVE_PUBLIC_LINKS_ONLY", raising=False)
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_PUBLIC_REPOSITORY_URL", raising=False)
    client = TestClient(server.create_app(runtime_client=object()))

    response = client.get("/api/connect-ai")

    assert response.status_code == 200
    assert response.json()["source"] == {
        "license": "Apache-2.0",
        "label": "Open source",
        "repository_url": "https://github.com/xPerfectAI/xPerfect",
    }
