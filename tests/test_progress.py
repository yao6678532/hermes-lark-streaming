"""ProgressState tests for lifecycle-only streaming-card status."""

from __future__ import annotations

from hermes_lark_streaming.streaming.progress import ProgressState, ProgressStatus


def test_initial_working_state() -> None:
    state = ProgressState()

    assert state.status == ProgressStatus.WORKING
    assert state.visible is True
    assert state.dirty is True
    assert state.snapshot().content == "⏳ Working"


def test_thinking_tool_answering_transitions() -> None:
    state = ProgressState()

    state.on_thinking()
    assert state.status == ProgressStatus.THINKING
    state.on_tool_event("read", "started")
    assert state.status == ProgressStatus.USING_TOOL
    state.on_tool_event("read", "completed")
    assert state.status == ProgressStatus.WORKING
    state.on_answering()
    assert state.status == ProgressStatus.ANSWERING


def test_multiple_tool_events_do_not_clear_using_tool_early() -> None:
    state = ProgressState()

    state.on_tool_event("read", "started")
    state.on_tool_event("search", "started")
    state.on_tool_event("read", "completed")
    assert state.status == ProgressStatus.USING_TOOL
    state.on_tool_event("search", "completed")
    assert state.status == ProgressStatus.WORKING

    state.on_tool_event("read", "started")
    state.on_tool_event("read", "started")
    state.on_tool_event("read", "completed")
    assert state.status == ProgressStatus.USING_TOOL
    state.on_tool_event("read", "failed")
    assert state.status == ProgressStatus.WORKING


def test_real_heartbeat_adds_elapsed_without_inventing_percentage() -> None:
    state = ProgressState()
    state.on_thinking()
    state.note_heartbeat(180)

    content = state.snapshot().content
    assert content == "💭 Thinking · 3 min"
    assert "%" not in content


def test_terminal_clear_hides_status() -> None:
    state = ProgressState()
    state.on_tool_event("read", "started")

    state.clear()

    assert state.status is None
    assert state.visible is False
    assert state.dirty is False
    assert state.snapshot().content == " "


def test_disabled_element_stops_dirty_updates_until_recreated() -> None:
    state = ProgressState()
    snapshot = state.snapshot()
    state.disable()

    assert state.available is False
    assert state.dirty is False
    state.mark_rendered(snapshot.revision)
    assert state.available is True
