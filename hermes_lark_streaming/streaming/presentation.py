"""Derived, read-only presentation snapshots for CardKit renderers.

The streaming states keep the authoritative event chronology.  This module
projects that state into the *current* view without changing it, so CardKit
does not need to be a physical copy of every historical event.
"""

from __future__ import annotations

from dataclasses import dataclass

from .preview import InterimPreviewState
from .reasoning import MergedReasoningState
from .segments import Segment, SegmentType
from .tooluse import ToolDisplayStep


@dataclass(frozen=True)
class CardViewSnapshot:
    """A renderer input derived from logical state, never a second database."""

    reasoning_text: str
    reasoning_elapsed_ms: float
    tool_steps: tuple[ToolDisplayStep, ...]
    tool_total_count: int
    preview_text: str
    answer_text: str
    final_started: bool


def project_card_view(
    *,
    segments: list[Segment],
    all_tool_steps: list[ToolDisplayStep],
    merged_reasoning: MergedReasoningState,
    interim_preview: InterimPreviewState,
    reasoning_mode: str,
) -> CardViewSnapshot:
    """Project the current CardKit view from immutable logical-state inputs.

    ``segments`` and tool steps retain their full chronology.  The snapshot
    deliberately takes copies only of its containers and never adjusts render
    flags, tool details, or preview state.
    """
    if reasoning_mode == "merged":
        reasoning_text = merged_reasoning.text
        reasoning_elapsed_ms = merged_reasoning.elapsed_ms
    else:
        reasoning_segments = [seg for seg in segments if seg.type == SegmentType.REASONING]
        reasoning_text = "".join(seg.text for seg in reasoning_segments)
        reasoning_elapsed_ms = sum(seg.elapsed_ms for seg in reasoning_segments)

    answer_text = "".join(seg.text for seg in segments if seg.type == SegmentType.ANSWER)
    final_started = interim_preview.final_started
    return CardViewSnapshot(
        reasoning_text=reasoning_text,
        reasoning_elapsed_ms=reasoning_elapsed_ms,
        tool_steps=tuple(all_tool_steps),
        tool_total_count=len(all_tool_steps),
        preview_text="" if final_started else interim_preview.text,
        answer_text=answer_text,
        final_started=final_started,
    )
