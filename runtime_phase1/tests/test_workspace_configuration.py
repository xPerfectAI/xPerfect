from pathlib import Path

from fastapi import HTTPException
import pytest

from workers_projects_runtime.conversation_provider import _canonical_life_dir, _default_workspace_dir


def test_legacy_workspace_requires_configuration_before_consulting_user_folders(monkeypatch):
    monkeypatch.delenv("VIVENTIUM_LIFE_DIR", raising=False)
    def refuse_implicit_home(path):
        raise AssertionError("Unconfigured legacy workspace must not consult user folders")
    monkeypatch.setattr(Path, "expanduser", refuse_implicit_home)
    with pytest.raises(HTTPException) as error:
        _canonical_life_dir()
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "workspace_configuration_required"


def test_explicit_legacy_workspace_preserves_configured_consumer_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("VIVENTIUM_LIFE_DIR", str(tmp_path))
    assert _canonical_life_dir() == tmp_path.resolve()


def test_neutral_workspace_default_preserves_current_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("VIVENTIUM_LIFE_DIR", raising=False)
    monkeypatch.delenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _default_workspace_dir() == tmp_path.resolve()
