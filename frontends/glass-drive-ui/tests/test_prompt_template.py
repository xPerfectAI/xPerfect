from glass_drive_ui.prompt_template import (
    build_operator_brief,
    build_project_title,
    desktop_action_for_launch,
    initial_watch_surface_for_launch,
    normalize_launch_surface,
    watch_surface_for_launch,
)


def test_build_operator_brief_includes_required_sections():
    brief = build_operator_brief("Research vendors", "Deliver three viable options", "Budget under $500")
    assert "Describe your project:" in brief
    assert "Success Criteria:" in brief
    assert "Context:" in brief
    assert "have I achieved the success criteria fully and correctly?" in brief
    assert "already fully authorized inside this sandbox" in brief
    assert "Never block the whole run on a foreground dev server" in brief
    assert "prefer one self-contained script or process" in brief
    assert "when the browser/tooling does not support file:// URLs" in brief


def test_build_project_title_is_short_and_stable():
    assert build_project_title("Find the best self hosted sandbox runtime for Glass Hive") == "Find the best self hosted sandbox…"


def test_desktop_action_for_launch():
    assert desktop_action_for_launch("codex-cli", "Refactor the API and add tests") == "terminal"
    assert desktop_action_for_launch("claude-code", "Write a browser automation with Playwright") == "browser"
    assert desktop_action_for_launch("codex-cli", "Create a hello world landing page") == "browser"
    assert desktop_action_for_launch("openclaw-general", "Research three self hosted runtime options") == "openclaw"


def test_watch_surface_for_launch():
    assert watch_surface_for_launch("codex-cli", "Refactor the API and add tests") == "terminal"
    assert watch_surface_for_launch("codex-cli", "Open the browser to example.com") == "desktop"
    assert watch_surface_for_launch("codex-cli", "Build a minimal landing page that renders hello world") == "desktop"


def test_initial_watch_surface_honors_explicit_override():
    assert initial_watch_surface_for_launch("codex-cli", "Refactor the API and add tests", launch_surface="desktop") == "desktop"
    assert initial_watch_surface_for_launch("codex-cli", "Create a landing page", launch_surface="terminal") == "terminal"
    assert initial_watch_surface_for_launch("codex-cli", "Create a landing page", launch_surface="auto") == "desktop"


def test_normalize_launch_surface_defaults_to_auto():
    assert normalize_launch_surface("desktop") == "desktop"
    assert normalize_launch_surface("terminal") == "terminal"
    assert normalize_launch_surface("weird-value") == "auto"


def test_goal_only_brief_does_not_invent_criteria_or_background():
    brief = build_operator_brief("Summarize the supplied report", "", None)
    assert "Summarize the supplied report" in brief
    assert "Success Criteria:" not in brief
    assert "Context:" not in brief
    assert "Complete the requested outcome and verify that the result works." not in brief
    assert "No additional context provided." not in brief
