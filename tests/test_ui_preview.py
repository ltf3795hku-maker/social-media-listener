from __future__ import annotations

from xhs_listener.ui_preview import (
    PREVIEW_STATES,
    build_preview_analysis,
    build_preview_posts,
    build_preview_report,
    build_preview_run,
    preview_mode_enabled,
)


def test_preview_mode_is_opt_in_and_blocked_on_azure() -> None:
    assert preview_mode_enabled({}) is False
    assert preview_mode_enabled({"UI_PREVIEW_MODE": "false"}) is False
    assert preview_mode_enabled({"UI_PREVIEW_MODE": "true"}) is True
    assert preview_mode_enabled({"UI_PREVIEW_MODE": "1"}) is True
    assert preview_mode_enabled({"UI_PREVIEW_MODE": "true", "WEBSITE_INSTANCE_ID": "azure-instance"}) is False


def test_preview_exposes_every_requested_state() -> None:
    assert PREVIEW_STATES == (
        "Home",
        "Searching",
        "Search Completed / Processing",
        "Analyzing",
        "Analysis Completed / Generating Report",
        "Final Report",
        "No Results",
        "Error",
    )


def test_preview_fixtures_match_the_ui_contract() -> None:
    posts = build_preview_posts()
    analysis = build_preview_analysis(posts)

    assert len(posts) == 50
    assert sum(row["is_scope_relevant"] is True for row in posts) == 37
    assert all(row["title"] and row["body_preview"] for row in posts)
    assert all(row["like_count"] >= 0 and row["collect_count"] >= 0 and row["comment_count"] >= 0 for row in posts)
    assert analysis["analysis_notes"] == 37
    assert 3 <= len(analysis["narrative_table"]) <= 5
    assert analysis["uncertainty_table"]
    assert analysis["annotation_summary"]["sentiment"]


def test_preview_runs_and_report_are_in_memory_only() -> None:
    final_run = build_preview_run("Final Report")
    structured, analysis, processing = build_preview_report()

    assert final_run["run_dir"] == ""
    assert final_run["report_html"] == "preview://report"
    assert structured["generated_scope"]["analysis_notes"] == 37
    assert structured["main_narratives"]
    assert structured["questions_uncertainties"]
    assert analysis["analysis_comments"] == 126
    assert processing["keyword"] == "港大选课"


def test_preview_run_steps_match_the_backend_pipeline() -> None:
    expected_steps = {
        "Searching": "collect",
        "Search Completed / Processing": "process",
        "Analyzing": "analyze",
        "Analysis Completed / Generating Report": "report",
        "Final Report": "report",
    }
    for state, step in expected_steps.items():
        assert build_preview_run(state)["current_step"] == step
