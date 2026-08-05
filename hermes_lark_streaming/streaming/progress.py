"""Lifecycle-driven presentation state for the streaming-card status line."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum


class ProgressStatus(StrEnum):
    """Small, verifiable set of states exposed by Hermes lifecycle events."""

    WORKING = "working"
    THINKING = "thinking"
    USING_TOOL = "using_tool"
    ANSWERING = "answering"


_STATUS_LABELS: dict[ProgressStatus, tuple[str, str]] = {
    ProgressStatus.WORKING: ("⏳ Working", "⏳ 处理中"),
    ProgressStatus.THINKING: ("💭 Thinking", "💭 思考中"),
    ProgressStatus.USING_TOOL: ("🛠️ Using tool", "🛠️ 调用工具"),
    ProgressStatus.ANSWERING: ("✍️ Answering", "✍️ 回答中"),
}


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Immutable render snapshot used to avoid clearing concurrent updates."""

    status: ProgressStatus | None
    elapsed_seconds: float
    revision: int

    @property
    def visible(self) -> bool:
        return self.status is not None

    @property
    def content(self) -> str:
        return _format_progress(self.status, self.elapsed_seconds, locale="en")

    @property
    def zh_content(self) -> str:
        return _format_progress(self.status, self.elapsed_seconds, locale="zh")


def _format_progress(
    status: ProgressStatus | None,
    elapsed_seconds: float,
    *,
    locale: str,
) -> str:
    if status is None:
        return " "
    label = _STATUS_LABELS[status][1 if locale == "zh" else 0]
    if elapsed_seconds < 60:
        return label
    minutes = max(1, int(elapsed_seconds // 60))
    suffix = f"{minutes} 分钟" if locale == "zh" else f"{minutes} min"
    return f"{label} · {suffix}"


class ProgressState:
    """Session-scoped status derived only from real Hermes lifecycle events."""

    __slots__ = (
        "_active_tools",
        "_available",
        "_rendered_revision",
        "_revision",
        "elapsed_seconds",
        "status",
    )

    def __init__(self) -> None:
        self.status: ProgressStatus | None = ProgressStatus.WORKING
        self.elapsed_seconds = 0.0
        self._active_tools: Counter[str] = Counter()
        self._available = True
        self._revision = 1
        self._rendered_revision = 0

    @property
    def visible(self) -> bool:
        return self.status is not None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dirty(self) -> bool:
        return self.visible and self.available and self._rendered_revision != self._revision

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(self.status, self.elapsed_seconds, self._revision)

    def mark_rendered(self, revision: int) -> None:
        self._available = True
        self._rendered_revision = max(self._rendered_revision, revision)

    def disable(self) -> None:
        """Stop claiming heartbeats after the fixed element cannot be updated."""
        self._available = False

    def set_status(self, status: ProgressStatus) -> None:
        if self.status == status:
            return
        self.status = status
        self._revision += 1

    def on_thinking(self) -> None:
        self._active_tools.clear()
        self.set_status(ProgressStatus.THINKING)

    def on_answering(self) -> None:
        self._active_tools.clear()
        self.set_status(ProgressStatus.ANSWERING)

    def on_tool_event(self, tool_name: str, status: str) -> None:
        normalized = str(status or "").strip().lower()
        key = str(tool_name or "tool").strip() or "tool"
        if normalized in {"running", "started", "tool.started"}:
            self._active_tools[key] += 1
            self.set_status(ProgressStatus.USING_TOOL)
            return

        if self._active_tools[key] > 1:
            self._active_tools[key] -= 1
        else:
            self._active_tools.pop(key, None)
        if self._active_tools:
            self.set_status(ProgressStatus.USING_TOOL)
        elif self.status == ProgressStatus.USING_TOOL:
            self.set_status(ProgressStatus.WORKING)

    def note_heartbeat(self, elapsed_seconds: float) -> None:
        elapsed = max(0.0, float(elapsed_seconds))
        if elapsed <= self.elapsed_seconds:
            return
        self.elapsed_seconds = elapsed
        self._revision += 1

    def clear(self) -> None:
        self._active_tools.clear()
        self.elapsed_seconds = 0.0
        if self.status is not None:
            self.status = None
            self._revision += 1
