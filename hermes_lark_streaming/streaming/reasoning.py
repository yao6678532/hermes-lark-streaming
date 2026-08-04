"""Session-scoped presentation state for a single merged reasoning lane."""

from __future__ import annotations

import time


class MergedReasoningState:
    """Presentation state for delta and activity-style reasoning sources."""

    __slots__ = (
        "active_since",
        "created",
        "dirty",
        "elapsed_ms",
        "text",
    )

    def __init__(self) -> None:
        self.text = ""
        self.active_since: float | None = None
        self.elapsed_ms = 0.0
        self.created = False
        self.dirty = False

    def _begin_active_interval(self) -> None:
        if self.active_since is None:
            self.active_since = time.time()

    def append_delta(self, text: str) -> None:
        """Append a true reasoning delta while preserving active timing."""
        if not text:
            return
        self._begin_active_interval()
        self.text += text
        self.dirty = True

    def replace_activity(self, text: str) -> None:
        """Replace the displayed state from an activity-style reasoning runtime."""
        if not text:
            return
        self._begin_active_interval()
        if self.text != text:
            self.text = text
            self.dirty = True

    def append(self, text: str) -> None:
        """Backward-compatible alias for callers that emit true deltas."""
        self.append_delta(text)

    def pause(self) -> None:
        """Pause timing without clearing accumulated reasoning content."""
        if self.active_since is None:
            return
        self.elapsed_ms += max(0.0, time.time() - self.active_since) * 1000
        self.active_since = None

    def finalize(self) -> None:
        """Close the final active interval while preserving presentation state."""
        self.pause()

    def reset_render_state(self) -> None:
        """Require fixed CardKit elements to be recreated after a card rollover."""
        self.created = False
        self.dirty = bool(self.text)
