"""ProgressState tests for Hermes long-running heartbeat presentation."""

from __future__ import annotations

from hermes_lark_streaming.streaming.progress import ProgressState


def test_initial_state_is_hidden_and_clean() -> None:
    state = ProgressState()

    assert state.visible is False
    assert state.dirty is False
    assert state.snapshot().content == " "


def test_heartbeat_adds_elapsed_and_iteration() -> None:
    state = ProgressState()

    state.note_heartbeat(180, iteration=3, max_iterations=60)

    snapshot = state.snapshot()
    assert snapshot.visible is True
    assert snapshot.content == "Working · 3 min · Round 3"
    assert snapshot.zh_content == "运行 · 3 min · 第 3 轮"
    assert "%" not in snapshot.content


def test_heartbeat_without_structured_iteration_keeps_text_simple() -> None:
    state = ProgressState()

    state.note_heartbeat(180)

    assert state.snapshot().content == "Working · 3 min"
    assert state.snapshot().zh_content == "运行中 · 3 min"


def test_repeated_heartbeat_latest_snapshot_wins() -> None:
    state = ProgressState()
    state.note_heartbeat(180, iteration=3, max_iterations=60)
    first_revision = state.snapshot().revision

    state.note_heartbeat(360, iteration=8, max_iterations=60)

    snapshot = state.snapshot()
    assert snapshot.revision > first_revision
    assert snapshot.content == "Working · 6 min · Round 8"


def test_terminal_clear_hides_heartbeat() -> None:
    state = ProgressState()
    state.note_heartbeat(180, iteration=3, max_iterations=60)

    state.clear()

    assert state.visible is False
    assert state.dirty is False
    assert state.snapshot().content == " "


def test_disabled_element_stops_claiming_heartbeats_until_rendered() -> None:
    state = ProgressState()
    state.note_heartbeat(180)
    snapshot = state.snapshot()
    state.mark_rendered(snapshot.revision)
    state.disable()

    assert state.available is False
    assert state.dirty is False
    state.note_heartbeat(360)
    assert state.dirty is False

    state.mark_rendered(state.snapshot().revision)
    assert state.available is True
def test_iteration_does_not_require_max_iterations() -> None:
    state = ProgressState()
    state.note_heartbeat(180, iteration=3)

    assert state.snapshot().content == "Working · 3 min · Round 3"
    assert state.snapshot().zh_content == "运行中 · 3 分钟 · 第 3 轮"