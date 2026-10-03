from glass_drive_ui.prompt_template import (
    build_operator_brief,
    build_project_title,
    normalize_launch_surface,
)


def test_brief_carries_the_users_fields_verbatim_and_nothing_else():
    brief = build_operator_brief(
        "  Research vendors\nfor the Q3 order  ",
        " Deliver three viable options ",
        " Budget under $500 ",
    )
    assert brief == (
        "Research vendors\nfor the Q3 order\n\n"
        "Success criteria:\nDeliver three viable options\n\n"
        "Background:\nBudget under $500"
    )


def test_goal_only_brief_is_the_goal():
    assert build_operator_brief("Summarize the supplied report", "", None) == "Summarize the supplied report"
    assert build_operator_brief("Summarize", "  ", "  ") == "Summarize"


def test_brief_adds_no_authority_workflow_or_verification_rules():
    brief = build_operator_brief("Create a landing page for https://example.com", "It renders", "Use the brand colors")
    for invented in (
        "authorized",
        "bypass",
        "dangerous",
        "permission",
        "Execution Rules",
        "Repeatedly ask yourself",
        "Keep working",
        "acceptance gates",
        "preview server",
        "sandbox",
        "main worker",
    ):
        assert invented.lower() not in brief.lower()


def test_build_project_title_is_short_and_stable():
    assert build_project_title("Find the best self hosted sandbox runtime for Glass Hive") == "Find the best self hosted sandbox…"


def test_launch_surface_is_an_explicit_choice_or_the_configured_default():
    assert normalize_launch_surface("desktop") == "desktop"
    assert normalize_launch_surface(" Terminal ") == "terminal"
    for unchosen in (None, "", "auto", "weird-value"):
        assert normalize_launch_surface(unchosen) == "desktop"
        assert normalize_launch_surface(unchosen, "terminal") == "terminal"
