"""Read-only CardKit presentation projection tests."""

from __future__ import annotations

from hermes_lark_streaming.cardkit.builder import _build_tool_panel
from hermes_lark_streaming.streaming.presentation import (
    ToolPresentationMode,
    project_card_view,
    project_tool_panel,
)
from hermes_lark_streaming.streaming.preview import InterimPreviewState
from hermes_lark_streaming.streaming.reasoning import MergedReasoningState
from hermes_lark_streaming.streaming.segments import SegmentState
from hermes_lark_streaming.streaming.tooluse import ToolUseTracker


def _steps(count: int) -> list[dict]:
    tracker = ToolUseTracker()
    for index in range(count):
        tracker.record_start("read", f"file-{index}")
    return tracker.build_display_steps()


def _project(
    state: SegmentState,
    *,
    steps: list[dict] | None = None,
    preview: InterimPreviewState | None = None,
    reasoning: MergedReasoningState | None = None,
    mode: str = "merged",
):
    return project_card_view(
        segments=state.segments,
        all_tool_steps=steps or [],
        merged_reasoning=reasoning or MergedReasoningState(),
        interim_preview=preview or InterimPreviewState(),
        reasoning_mode=mode,
    )


def test_multiple_reasoning_events_project_to_one_merged_view() -> None:
    state = SegmentState()
    state.on_reasoning_delta("chronology A")
    state.on_answer_delta("answer")
    state.on_reasoning_delta("chronology B")
    merged = MergedReasoningState()
    merged.append_delta("merged A")
    merged.append_delta(" merged B")

    snapshot = _project(state, reasoning=merged)

    assert snapshot.reasoning_text == "merged A merged B"
    assert snapshot.answer_text == "answer"


def test_multiple_tool_segments_project_to_one_tool_view() -> None:
    state = SegmentState()
    state.on_tool_event(1)
    state.on_answer_delta("interlude")
    state.on_tool_event(2)

    snapshot = _project(state, steps=_steps(2))

    assert snapshot.tool_total_count == 2
    assert len(snapshot.tool_steps) == 2


def test_latest_commentary_is_the_only_preview_projection() -> None:
    state = SegmentState()
    preview = InterimPreviewState()
    preview.replace("first")
    preview.replace("latest")

    assert _project(state, preview=preview).preview_text == "latest"


def test_final_started_excludes_preview_from_projection() -> None:
    state = SegmentState()
    preview = InterimPreviewState()
    preview.replace("commentary")
    preview.start_final()

    snapshot = _project(state, preview=preview)

    assert snapshot.final_started is True
    assert snapshot.preview_text == ""


def test_answer_chronology_projects_current_answer() -> None:
    state = SegmentState()
    state.on_answer_delta("one")
    state.on_tool_event(1)
    state.on_answer_delta(" two")

    assert _project(state).answer_text == "one two"


def test_projection_does_not_mutate_logical_state() -> None:
    state = SegmentState()
    state.on_reasoning_delta("reason")
    state.on_answer_delta("answer")
    steps = _steps(1)
    preview = InterimPreviewState()
    preview.replace("preview")
    merged = MergedReasoningState()
    merged.append_delta("merged")
    before = [(seg.text, seg.created, seg.dirty) for seg in state.segments]
    before_preview = (preview.text, preview.dirty, preview.final_started)
    before_step = dict(steps[0])

    _project(state, steps=steps, preview=preview, reasoning=merged)

    assert [(seg.text, seg.created, seg.dirty) for seg in state.segments] == before
    assert (preview.text, preview.dirty, preview.final_started) == before_preview
    assert steps[0] == before_step


def test_rich_tool_projection_preserves_configured_full_view_when_it_fits() -> None:
    steps = _steps(2)
    snapshot = project_tool_panel(
        steps, show_tool_detail=True, tool_detail_mode="full", element_budget=40,
    )

    assert snapshot.mode == ToolPresentationMode.FULL
    assert snapshot.success_results_visible is True
    assert snapshot.rendered_steps == snapshot.total_steps == 2


def test_overflow_tool_projection_compacts_before_using_a_window() -> None:
    tracker = ToolUseTracker()
    for index in range(33):
        tracker.record_start("exec", f"file-{index}")
        tracker.record_end("exec", output=f"result-{index}")
    steps = tracker.build_display_steps()

    snapshot = project_tool_panel(
        steps, show_tool_detail=True, tool_detail_mode="full", element_budget=170,
    )

    assert snapshot.degraded is True
    assert snapshot.windowed is False
    assert snapshot.total_steps == 33
    assert snapshot.estimated_elements <= 170
    assert all(step["result_block"] is None for step in snapshot.steps)
    assert all(step["result_block"] is not None for step in steps)


def test_tool_window_keeps_running_errors_then_recent_successes() -> None:
    tracker = ToolUseTracker()
    for index in range(100):
        tracker.record_start("read", f"file-{index}")
        tracker.record_end("read", output=f"result-{index}")
    tracker.record_start("grep", "still-running")
    tracker.record_start("read", "will-fail")
    tracker.record_end("read", error="failure")
    steps = tracker.build_display_steps()

    snapshot = project_tool_panel(
        steps, show_tool_detail=True, tool_detail_mode="full", element_budget=40,
    )

    assert snapshot.windowed is True
    assert snapshot.mode == ToolPresentationMode.TITLE_ONLY
    assert snapshot.total_steps == 102
    assert snapshot.estimated_elements <= 40
    assert {"grep", "read"} <= {step["name"] for step in snapshot.steps}
    assert any(step["status"] == "error" for step in snapshot.steps)
    assert any(step["status"] == "running" for step in snapshot.steps)


def _rendered_tool_panel_element_count(panel: dict) -> int:
    """Count the CardKit structures represented by the tool estimator."""
    count = 3  # panel, header title, and header icon
    for child in panel["elements"]:
        count += 1 + int("icon" in child) + int("text" in child)
    return count


def test_tool_snapshot_estimator_matches_full_builder_shape() -> None:
    tracker = ToolUseTracker()
    tracker.record_start("exec", "run command")
    tracker.record_end("exec", output="result")
    snapshot = project_tool_panel(
        tracker.build_display_steps(),
        show_tool_detail=True,
        tool_detail_mode="full",
        element_budget=30,
    )
    panel = _build_tool_panel(
        list(snapshot.steps),
        total_steps=snapshot.total_steps,
        show_tool_detail=snapshot.show_tool_detail,
        tool_detail_mode=snapshot.tool_detail_mode,
    )

    assert snapshot.mode == ToolPresentationMode.FULL
    assert _rendered_tool_panel_element_count(panel) == snapshot.estimated_elements


def test_tool_snapshot_estimator_matches_compact_and_windowed_builder_shapes() -> None:
    tracker = ToolUseTracker()
    for index in range(50):
        tracker.record_start("exec", f"run-{index}")
        tracker.record_end("exec", output=f"result-{index}")
    steps = tracker.build_display_steps()
    compact = project_tool_panel(
        steps, show_tool_detail=True, tool_detail_mode="full", element_budget=170,
    )
    windowed = project_tool_panel(
        steps, show_tool_detail=True, tool_detail_mode="full", element_budget=40,
    )

    for snapshot in (compact, windowed):
        panel = _build_tool_panel(
            list(snapshot.steps),
            total_steps=snapshot.total_steps,
            show_tool_detail=snapshot.show_tool_detail,
            tool_detail_mode=snapshot.tool_detail_mode,
        )
        assert _rendered_tool_panel_element_count(panel) == snapshot.estimated_elements
    assert compact.estimated_elements <= 170
    assert windowed.windowed is True
    assert "50" in panel["header"]["title"]["content"]
