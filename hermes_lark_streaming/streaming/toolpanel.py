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
    rendered_statuses: tuple[str, ...] = ()
    final_answer_refresh_pending: bool = False

    def note_tool_event(self) -> None:
        self.revision += 1
        self.dirty = True

    def note_answer_started(self) -> None:
        self.revision += 1
        self.dirty = True
        self.final_answer_refresh_pending = True

    def mark_dirty(self) -> None:
        self.revision += 1
        self.dirty = True

    def mark_rendered(
        self,
        revision: int,
        element_estimate: int,
        *,
        statuses: tuple[str, ...] = (),
    ) -> bool:
        """Record a rendered snapshot and return whether it is still current."""
        self.created = True
        self.element_estimate = element_estimate
        self.rendered_statuses = statuses
        self.final_answer_refresh_pending = False
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
        self.rendered_statuses = ()
        self.final_answer_refresh_pending = False

    def needs_final_refresh(self, statuses: tuple[str, ...]) -> bool:
        """Allow only state transitions material to a final-answer viewer.

        Once final answer streaming begins, ordinary new/finished successes can
        wait for the terminal projection.  Running and error transitions still
        refresh so the visible live state never becomes misleading.
        """
        for index, status in enumerate(statuses):
            previous = self.rendered_statuses[index] if index < len(self.rendered_statuses) else None
            if status != previous and (status in {"running", "error"} or previous == "running"):
                return True
        return False
