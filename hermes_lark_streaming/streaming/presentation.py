"""Derived, read-only presentation snapshots for CardKit renderers.

The streaming states keep the authoritative event chronology.  This module
projects that state into the *current* view without changing it, so CardKit
does not need to be a physical copy of every historical event.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from .preview import InterimPreviewState
from .reasoning import MergedReasoningState
from .segments import Segment, SegmentType
from .tooluse import ToolDisplayStep, tool_detail_for_display


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


class ToolPresentationMode(StrEnum):
    """The bounded representation selected for a tool-panel snapshot."""

    FULL = "full"
    COMPACT = "compact"
    TITLE_ONLY = "title_only"


@dataclass(frozen=True)
class ToolPanelSnapshot:
    """A bounded tool projection that retains the total logical step count."""

    steps: tuple[ToolDisplayStep, ...]
    total_steps: int
    mode: ToolPresentationMode
    configured_mode: ToolPresentationMode
    show_tool_detail: bool
    tool_detail_mode: str
    success_results_visible: bool
    estimated_elements: int
    windowed: bool = False

    @property
    def rendered_steps(self) -> int:
        return len(self.steps)

    @property
    def degraded(self) -> bool:
        return self.windowed or self.mode != self.configured_mode or not self.success_results_visible


def estimate_tool_snapshot_elements(snapshot: ToolPanelSnapshot) -> int:
    """Estimate exactly the tool-panel shape rendered from ``snapshot``.

    The builder receives the already-projected steps plus the two display
    switches below, so successful output blocks and hidden detail lines cannot
    reappear after this estimate has been made.
    """
    count = 3  # collapsible panel, title text, and its icon/header structure
    for step in snapshot.steps:
        count += 3  # title div, icon, and lark markdown
        if tool_detail_for_display(
            step,
            show_detail=snapshot.show_tool_detail,
            mode=snapshot.tool_detail_mode,
        ):
            count += 2
        if step.get("result_block") or step.get("error_block"):
            count += 2
    return count


def project_tool_panel(
    all_steps: list[ToolDisplayStep],
    *,
    show_tool_detail: bool,
    tool_detail_mode: str,
    element_budget: int,
) -> ToolPanelSnapshot:
    """Select the richest tool view that fits the current CardKit budget.

    Tool chronology remains untouched: each candidate copies only the display
    dictionaries that are passed to CardKit.  Overflow progressively removes
    successful result blocks, then compacts detail, then renders titles only,
    and finally selects a priority window (running, errors, recent successes).
    """
    configured_mode = (
        ToolPresentationMode.FULL
        if show_tool_detail and tool_detail_mode == "full"
        else ToolPresentationMode.COMPACT
        if show_tool_detail
        else ToolPresentationMode.TITLE_ONLY
    )

    candidates: list[tuple[ToolPresentationMode, bool]] = []
    if configured_mode == ToolPresentationMode.FULL:
        candidates.extend([
            (ToolPresentationMode.FULL, True),
            (ToolPresentationMode.FULL, False),
            (ToolPresentationMode.COMPACT, False),
            (ToolPresentationMode.TITLE_ONLY, False),
        ])
    elif configured_mode == ToolPresentationMode.COMPACT:
        candidates.extend([
            (ToolPresentationMode.COMPACT, False),
            (ToolPresentationMode.TITLE_ONLY, False),
        ])
    else:
        candidates.append((ToolPresentationMode.TITLE_ONLY, False))

    for mode, success_results_visible in candidates:
        snapshot = _tool_snapshot(
            all_steps,
            configured_mode=configured_mode,
            mode=mode,
            success_results_visible=success_results_visible,
        )
        if snapshot.estimated_elements <= element_budget:
            return snapshot

    # Title-only is the smallest complete representation.  Build a bounded
    # window from highest-priority steps before considering split fallback.
    return _windowed_tool_snapshot(
        all_steps,
        configured_mode=configured_mode,
        element_budget=element_budget,
    )


def _tool_snapshot(
    all_steps: list[ToolDisplayStep],
    *,
    configured_mode: ToolPresentationMode,
    mode: ToolPresentationMode,
    success_results_visible: bool,
    total_steps: int | None = None,
    windowed: bool = False,
) -> ToolPanelSnapshot:
    show_detail = mode != ToolPresentationMode.TITLE_ONLY
    detail_mode = "compact" if mode == ToolPresentationMode.COMPACT else "full"
    projected_steps: list[ToolDisplayStep] = []
    for original in all_steps:
        step = cast(ToolDisplayStep, dict(original))
        if not success_results_visible and step.get("status") == "success":
            step["result_block"] = None
        projected_steps.append(step)
    provisional = ToolPanelSnapshot(
        steps=tuple(projected_steps),
        total_steps=len(all_steps) if total_steps is None else total_steps,
        mode=mode,
        configured_mode=configured_mode,
        show_tool_detail=show_detail,
        tool_detail_mode=detail_mode,
        success_results_visible=success_results_visible,
        estimated_elements=0,
        windowed=windowed,
    )
    return ToolPanelSnapshot(
        **{**provisional.__dict__, "estimated_elements": estimate_tool_snapshot_elements(provisional)}
    )


def _windowed_tool_snapshot(
    all_steps: list[ToolDisplayStep],
    *,
    configured_mode: ToolPresentationMode,
    element_budget: int,
) -> ToolPanelSnapshot:
    priority = sorted(
        range(len(all_steps)),
        key=lambda index: (
            0 if all_steps[index].get("status") == "running" else 1
            if all_steps[index].get("status") == "error" else 2,
            -index,
        ),
    )
    selected: set[int] = set()
    for index in priority:
        candidate_indices = sorted((*selected, index))
        candidate = _tool_snapshot(
            [all_steps[position] for position in candidate_indices],
            configured_mode=configured_mode,
            mode=ToolPresentationMode.TITLE_ONLY,
            success_results_visible=False,
            total_steps=len(all_steps),
            windowed=True,
        )
        if candidate.estimated_elements <= element_budget:
            selected.add(index)

    return _tool_snapshot(
        [all_steps[index] for index in sorted(selected)],
        configured_mode=configured_mode,
        mode=ToolPresentationMode.TITLE_ONLY,
        success_results_visible=False,
        total_steps=len(all_steps),
        windowed=True,
    )


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
