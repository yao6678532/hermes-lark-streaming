"""ProgressState tests for Hermes long-running heartbeat presentation."""

from __future__ import annotations

import pytest

from hermes_lark_streaming.streaming.progress import ActivityKind, ProgressState


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
    assert snapshot.zh_content == "运行中 · 3 分钟 · 第 3 轮"
    assert "%" not in snapshot.content


def test_heartbeat_without_structured_iteration_keeps_text_simple() -> None:
    state = ProgressState()

    state.note_heartbeat(180)

    assert state.snapshot().content == "Working · 3 min"
    assert state.snapshot().zh_content == "运行中 · 3 分钟"


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


@pytest.mark.parametrize(
    ("activity", "english", "chinese"),
    [
        (ActivityKind.THINKING, "Thinking", "思考中"),
        (ActivityKind.EXECUTING_COMMAND, "Running command", "正在执行命令"),
        (ActivityKind.SEARCHING, "Searching", "正在搜索资料"),
        (ActivityKind.READING, "Reading", "正在读取内容"),
        (ActivityKind.USING_TOOL, "Using tool", "正在调用工具"),
        (ActivityKind.ANSWERING, "Generating answer", "正在生成回答"),
    ],
)
def test_activity_labels(activity: ActivityKind, english: str, chinese: str) -> None:
    state = ProgressState()

    assert state.note_activity(activity) is True

    snapshot = state.snapshot()
    assert snapshot.visible is True
    assert snapshot.content == english
    assert snapshot.zh_content == chinese


def test_same_activity_is_deduplicated() -> None:
    state = ProgressState()
    state.note_activity(ActivityKind.THINKING)
    revision = state.snapshot().revision

    assert state.note_activity(ActivityKind.THINKING) is False
    assert state.snapshot().revision == revision


@pytest.mark.parametrize(
    ("activity", "english", "chinese"),
    [
        (ActivityKind.EXECUTING_COMMAND, "Running command", "正在执行命令"),
        (ActivityKind.THINKING, "Thinking", "思考中"),
        (ActivityKind.SEARCHING, "Searching", "正在搜索资料"),
    ],
)
def test_heartbeat_does_not_override_or_dirty_activity(
    activity: ActivityKind,
    english: str,
    chinese: str,
) -> None:
    state = ProgressState()
    state.note_activity(activity)
    snapshot = state.snapshot()
    state.mark_rendered(snapshot.revision)

    assert state.note_heartbeat(120, iteration=5) is False

    current = state.snapshot()
    assert current.content == english
    assert current.zh_content == chinese
    assert current.revision == snapshot.revision
    assert state.dirty is False
    assert current.elapsed_seconds == 120
    assert current.iteration == 5


def test_activity_clear_blanks_until_next_real_heartbeat() -> None:
    state = ProgressState()
    state.note_activity(ActivityKind.SEARCHING)
    state.note_heartbeat(120, iteration=5)

    assert state.note_activity(None) is True
    cleared = state.snapshot()
    assert cleared.visible is False
    assert cleared.content == " "
    assert state.dirty is True

    state.mark_rendered(cleared.revision)
    assert state.note_heartbeat(150, iteration=6) is True
    assert state.snapshot().zh_content == "运行中 · 2 分钟 · 第 6 轮"


def test_terminal_clear_removes_activity_and_pending_update() -> None:
    state = ProgressState()
    state.note_activity(ActivityKind.ANSWERING)

    state.clear()

    assert state.activity is None
    assert state.visible is False
    assert state.dirty is False
