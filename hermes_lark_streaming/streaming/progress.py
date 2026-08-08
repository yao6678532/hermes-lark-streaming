"""Hermes long-running heartbeat state for the streaming-card loader."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActivityKind(StrEnum):
    """High-level activity derived only from structured Hermes events."""

    THINKING = "thinking"
    EXECUTING_COMMAND = "executing_command"
    SEARCHING = "searching"
    READING = "reading"
    USING_TOOL = "using_tool"
    ANSWERING = "answering"


_ACTIVITY_LABELS: dict[ActivityKind, tuple[str, str]] = {
    ActivityKind.THINKING: ("Thinking", "思考中"),
    ActivityKind.EXECUTING_COMMAND: ("Running command", "正在执行命令"),
    ActivityKind.SEARCHING: ("Searching", "正在搜索资料"),
    ActivityKind.READING: ("Reading", "正在读取内容"),
    ActivityKind.USING_TOOL: ("Using tool", "正在调用工具"),
    ActivityKind.ANSWERING: ("Generating answer", "正在生成回答"),
}


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Immutable activity/heartbeat snapshot for revision-safe rendering."""

    activity: ActivityKind | None
    elapsed_seconds: float
    iteration: int | None
    max_iterations: int | None
    revision: int
    visible: bool

    @property
    def content(self) -> str:
        if self.activity is not None:
            return _ACTIVITY_LABELS[self.activity][0]
        return _format_progress(
            self.elapsed_seconds,
            self.iteration,
            self.max_iterations,
            locale="en",
            visible=self.visible,
        )

    @property
    def zh_content(self) -> str:
        if self.activity is not None:
            return _ACTIVITY_LABELS[self.activity][1]
        return _format_progress(
            self.elapsed_seconds,
            self.iteration,
            self.max_iterations,
            locale="zh",
            visible=self.visible,
        )


def _format_progress(
    elapsed_seconds: float,
    iteration: int | None,
    max_iterations: int | None,
    *,
    locale: str,
    visible: bool,
) -> str:
    if not visible:
        return " "
    label = "Working" if locale == "en" else "运行中"
    parts = [label]
    if elapsed_seconds >= 60:
        minutes = int(elapsed_seconds // 60)
        parts.append(
            f"{minutes} min"
            if locale == "en"
            else f"{minutes} 分钟"
        )
    if iteration is not None:
        parts.append(
            f"Round {iteration}"
            if locale == "en"
            else f"第 {iteration} 轮"
        )
    return " · ".join(parts)


class ProgressState:
    """Session status driven only by structured activity and heartbeat events."""

    __slots__ = (
        "_available",
        "_has_heartbeat",
        "_rendered_revision",
        "_revision",
        "activity",
        "elapsed_seconds",
        "iteration",
        "max_iterations",
    )

    def __init__(self) -> None:
        self.activity: ActivityKind | None = None
        self.elapsed_seconds = 0.0
        self.iteration: int | None = None
        self.max_iterations: int | None = None
        self._available = True
        self._has_heartbeat = False
        self._revision = 0
        self._rendered_revision = 0

    @property
    def visible(self) -> bool:
        return self.activity is not None or self._has_heartbeat

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dirty(self) -> bool:
        return self.available and self._rendered_revision != self._revision

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(
            activity=self.activity,
            elapsed_seconds=self.elapsed_seconds,
            iteration=self.iteration,
            max_iterations=self.max_iterations,
            revision=self._revision,
            visible=self.visible,
        )

    def mark_rendered(self, revision: int) -> None:
        self._available = True
        self._rendered_revision = max(self._rendered_revision, revision)

    def disable(self) -> None:
        """Stop claiming future heartbeats after a CardKit update fails."""
        self._available = False

    def note_heartbeat(
        self,
        elapsed_seconds: float,
        *,
        iteration: int | None = None,
        max_iterations: int | None = None,
    ) -> bool:
        """Record a Hermes heartbeat and return whether visible status changed."""
        elapsed = max(0.0, float(elapsed_seconds))
        normalized_iteration = _normalize_int(iteration)
        normalized_max_iterations = _normalize_int(max_iterations)
        changed = (
            not self._has_heartbeat
            or elapsed != self.elapsed_seconds
            or normalized_iteration != self.iteration
            or normalized_max_iterations != self.max_iterations
        )
        self.elapsed_seconds = elapsed
        self.iteration = normalized_iteration
        self.max_iterations = normalized_max_iterations
        if changed:
            self._has_heartbeat = True
            if self.activity is None:
                self._revision += 1
                return True
        return False

    def note_activity(self, activity: ActivityKind | None) -> bool:
        """Apply a structured activity transition with same-state deduplication.

        Clearing activity deliberately hides any heartbeat received during the
        activity. The next real Hermes heartbeat re-enables the fallback UI.
        """
        if activity == self.activity:
            return False
        self.activity = activity
        if activity is None:
            self._has_heartbeat = False
        self._revision += 1
        return True

    def clear(self) -> None:
        if self.activity is None and not self._has_heartbeat:
            return
        self.activity = None
        self._has_heartbeat = False
        self.elapsed_seconds = 0.0
        self.iteration = None
        self.max_iterations = None
        self._revision += 1
        self._rendered_revision = self._revision


def _normalize_int(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
