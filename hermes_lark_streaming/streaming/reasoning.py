"""Session-scoped presentation state for a single merged reasoning lane."""

from __future__ import annotations

import time


class MergedReasoningState:
    """Accumulate reasoning text while measuring only active reasoning intervals."""

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

    def append(self, text: str) -> None:
        """Append a reasoning delta and start a new active interval if needed."""
        if not text:
            return
        if self.active_since is None:
            self.active_since = time.time()
        self.text += text
        self.dirty = True

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
