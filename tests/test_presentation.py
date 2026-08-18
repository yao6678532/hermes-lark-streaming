"""Read-only CardKit presentation projection tests."""

from __future__ import annotations

from hermes_lark_streaming.streaming.presentation import project_card_view
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
