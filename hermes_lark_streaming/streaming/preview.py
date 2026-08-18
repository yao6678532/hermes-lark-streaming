"""Session-scoped presentation state for interim assistant commentary."""

from __future__ import annotations


class InterimPreviewState:
    """Replace-only preview state that never becomes permanent chronology."""

    __slots__ = (
        "created",
        "dirty",
        "final_started",
        "revision",
        "text",
    )

    def __init__(self) -> None:
        self.text = ""
        self.created = False
        self.dirty = False
        self.revision = 0
        self.final_started = False

    def replace(self, text: str) -> bool:
        """Replace the current commentary message while final answer is absent."""
        if self.final_started or not text or self.text == text:
            return False
        self.text = text
        self.revision += 1
        self.dirty = True
        return True

    def start_final(self) -> bool:
        """Invalidate the preview permanently when final answer starts."""
        if self.final_started:
            return False
        had_rendered_preview = self.created
        self.final_started = True
        self.text = ""
        self.revision += 1
        self.dirty = had_rendered_preview
        return True

    def mark_rendered(self, revision: int) -> bool:
        """Mark a snapshot rendered and retain dirtiness for newer revisions."""
        if revision == self.revision:
            self.dirty = False
            return True
        self.dirty = True
        return False

    def reset_render_state(self) -> None:
        """Require the fixed preview element to be recreated on a new card."""
        self.created = False
        self.revision += 1
        self.dirty = bool(self.text) and not self.final_started
