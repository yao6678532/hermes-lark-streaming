"""Session-scoped presentation state for the unified tool panel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolPanelState:
    """Track the fixed CardKit panel without duplicating tool step state.

    ``ToolUseTracker`` remains the source of truth for steps.  This object only
    tracks whether the current physical card has rendered its presentation and
    which revision/element estimate was sent to CardKit.
    """

    created: bool = False
    dirty: bool = False
    revision: int = 0
    element_estimate: int = 0

    def note_tool_event(self) -> None:
        self.revision += 1
        self.dirty = True

    def note_answer_started(self) -> None:
        self.revision += 1
        self.dirty = True

    def mark_dirty(self) -> None:
        self.revision += 1
        self.dirty = True

    def mark_rendered(self, revision: int, element_estimate: int) -> bool:
        """Record a rendered snapshot and return whether it is still current."""
        self.created = True
        self.element_estimate = element_estimate
        if revision == self.revision:
            self.dirty = False
            return True
        self.dirty = True
        return False

    def reset_render_state(self, *, dirty: bool = False) -> None:
        """Forget CardKit state after rollover or a missing fixed element."""
        self.created = False
        self.dirty = dirty
        self.revision += 1
        self.element_estimate = 0
