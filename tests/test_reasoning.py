"""Merged reasoning presentation state tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes_lark_streaming.streaming.reasoning import MergedReasoningState


class TestMergedReasoningState:
    def test_ignores_empty_input(self) -> None:
        state = MergedReasoningState()

        state.append("")

        assert state.text == ""
        assert state.active_since is None
        assert state.dirty is False

    def test_accumulates_active_intervals_and_excludes_tool_wait(self) -> None:
        state = MergedReasoningState()

        with patch(
            "hermes_lark_streaming.streaming.reasoning.time.time",
            side_effect=[100.0, 103.0, 123.0, 127.0],
        ):
            state.append("A")
            state.pause()
            state.append("B")
            state.finalize()

        assert state.text == "AB"
        assert state.elapsed_ms == pytest.approx(7000.0)
        assert state.active_since is None

    def test_reset_render_state_preserves_content_and_timing(self) -> None:
        state = MergedReasoningState()
        with patch(
            "hermes_lark_streaming.streaming.reasoning.time.time",
            side_effect=[10.0, 12.0],
        ):
            state.append("reasoning")
            state.pause()
        state.created = True
        state.dirty = False

        state.reset_render_state()

        assert state.text == "reasoning"
        assert state.elapsed_ms == pytest.approx(2000.0)
        assert state.created is False
        assert state.dirty is True
