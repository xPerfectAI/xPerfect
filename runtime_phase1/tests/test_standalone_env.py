"""Runtime environment loading is explicit for standalone xPerfect."""

import os
from pathlib import Path

from workers_projects_runtime import runtime_env


def test_runtime_does_not_read_viventium_app_support_or_checkout_by_default(tmp_path, monkeypatch):
    default_dir = tmp_path / "Library" / "Application Support" / "Viventium" / "runtime"
    default_dir.mkdir(parents=True)
    (default_dir / "runtime.env").write_text("GLASSHIVE_SIGNED_LINK_SECRET=foreign-secret\n")
    (default_dir / "runtime.local.env").write_text("WPR_LIBRECHAT_UPLOADS_ROOT=/foreign/uploads\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIVENTIUM_ENV_FILE", raising=False)
    monkeypatch.delenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    monkeypatch.setattr(runtime_env, "_local_checkout_librechat_uploads_root", lambda: (_ for _ in ()).throw(AssertionError("implicit checkout scan")))

    loaded = runtime_env.load_viventium_runtime_env({"GLASSHIVE_SIGNED_LINK_SECRET", "WPR_LIBRECHAT_UPLOADS_ROOT"})

    assert loaded == {}
    assert runtime_env._candidate_env_files() == []
    assert "GLASSHIVE_SIGNED_LINK_SECRET" not in os.environ
    assert "WPR_LIBRECHAT_UPLOADS_ROOT" not in os.environ


def test_runtime_explicit_viventium_env_file_still_loads(tmp_path, monkeypatch):
    explicit = tmp_path / "selected.env"
    explicit.write_text("GLASSHIVE_SIGNED_LINK_SECRET=selected-secret\n")
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(explicit))
    monkeypatch.setenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "1")
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)

    loaded = runtime_env.load_viventium_runtime_env({"GLASSHIVE_SIGNED_LINK_SECRET"})

    assert loaded == {"GLASSHIVE_SIGNED_LINK_SECRET": "selected-secret"}
