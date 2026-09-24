from glass_drive_ui.server import _bootstrap_bundle_with_effort, _new_workspace_options


def test_grok_profile_selection_respects_allowed_profiles(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "grok-build")
    assert _new_workspace_options() == [
        {"value": "new:grok-build", "label": "Grok Build worker", "profile": "grok-build"},
    ]


def test_grok_effort_is_carried_to_native_adapter_env():
    bundle = _bootstrap_bundle_with_effort(
        {"env": {"SYNTHETIC": "value"}},
        "grok-build",
        "high",
    )
    assert bundle["env"] == {
        "SYNTHETIC": "value",
        "WPR_GROK_REASONING_EFFORT": "high",
    }


def test_grok_effort_rejects_control_characters_before_launch():
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="Invalid native Grok effort ID"):
        _bootstrap_bundle_with_effort({}, "grok-build", "hi\ngh")
