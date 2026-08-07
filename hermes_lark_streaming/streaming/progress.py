"""Hermes long-running heartbeat state for the streaming-card loader."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Immutable heartbeat snapshot used by the revision-safe flush path."""

    elapsed_seconds: float
    iteration: int | None
    max_iterations: int | None
    revision: int
    visible: bool

    @property
    def content(self) -> str:
        return _format_progress(
            self.elapsed_seconds,
            self.iteration,
            self.max_iterations,
            locale="en",
            visible=self.visible,
        )

    @property
    def zh_content(self) -> str:
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
    """Session-scoped state driven only by Hermes long-running heartbeats."""

    __slots__ = (
        "_available",
        "_has_heartbeat",
        "_rendered_revision",
        "_revision",
        "elapsed_seconds",
        "iteration",
        "max_iterations",
    )

    def __init__(self) -> None:
        self.elapsed_seconds = 0.0
        self.iteration: int | None = None
        self.max_iterations: int | None = None
        self._available = True
        self._has_heartbeat = False
        self._revision = 0
        self._rendered_revision = 0

    @property
    def visible(self) -> bool:
        return self._has_heartbeat

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dirty(self) -> bool:
        return self.visible and self.available and self._rendered_revision != self._revision

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(
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
    ) -> None:
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
            self._revision += 1

    def clear(self) -> None:
        if not self._has_heartbeat:
            return
        self._has_heartbeat = False
        self.elapsed_seconds = 0.0
        self.iteration = None
        self.max_iterations = None
        self._revision += 1


def _normalize_int(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
