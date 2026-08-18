"""Segment 对应的 CardKit action 构造与容量估算."""

from __future__ import annotations

from typing import Any

from ..cardkit.builder import (
    _LOADING_ELEMENT_ID,
    REASONING_ELEMENT_ID,
    REASONING_TEXT_ELEMENT_ID,
    STREAMING_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    _build_reasoning_panel,
    _build_tool_panel,
    _format_elapsed,
    _streaming_element,
)
from ..cardkit.i18n import _T, _i18n
from .progress import ProgressSnapshot
from .segments import Segment, SegmentType
from .tooluse import ToolDisplayStep, tool_detail_for_display

ELEMENT_THRESHOLD = 180  # 飞书硬上限 200，预留 20 给 footer + 波动
# Run Details: hr + collapsible_panel + header title + header icon + markdown.
FOOTER_RESERVE = 5
MERGED_REASONING_ELEMENT_ESTIMATE = 4
INTERIM_PREVIEW_ELEMENT_ESTIMATE = 1


def build_progress_update_action(snapshot: ProgressSnapshot) -> dict[str, Any]:
    """Update only the content of the fixed native loading element."""
    return {
        "action": "partial_update_element",
        "params": {
            "element_id": _LOADING_ELEMENT_ID,
            "partial_element": {
                "content": snapshot.content,
                "i18n_content": _i18n(snapshot.content, snapshot.zh_content),
            },
        },
    }


def estimate_segment_elements(
    seg: Segment,
    all_steps: list[ToolDisplayStep],
    *,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> int:
    """估算单个 segment 新增的卡片元素数."""
    if seg.type == SegmentType.REASONING:
        return 4  # collapsible_panel + plain_text + standard_icon + markdown
    if seg.type == SegmentType.ANSWER:
        return 1
    if seg.type == SegmentType.TOOL:
        return estimate_tool_elements(
            seg.tool_offset,
            tool_segment_end(seg, all_steps),
            all_steps,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
        )
    return 0


def tool_segment_end(seg: Segment, all_steps: list[ToolDisplayStep]) -> int:
    return seg.tool_end_offset if seg.tool_end_offset else len(all_steps)


def active_tool_range(
    segments: list[Segment], split_index: int, all_steps: list[ToolDisplayStep]
) -> tuple[int, int] | None:
    """Return the tool step slice owned by the current physical card."""
    tool_segments = [
        seg for seg in segments[split_index:] if seg.type == SegmentType.TOOL
    ]
    if not tool_segments:
        return None
    start = min(seg.tool_offset for seg in tool_segments)
    end = max(tool_segment_end(seg, all_steps) for seg in tool_segments)
    if start >= end:
        return None
    return start, min(end, len(all_steps))


def estimate_tool_elements(
    start: int,
    end: int,
    all_steps: list[ToolDisplayStep],
    *,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> int:
    """估算 tool panel 在 [start, end) step 区间内的元素数."""
    steps = all_steps[start:end]
    count = 3  # panel/header 基础元素
    for step in steps:
        count += 3  # title: div + standard_icon + lark_md
        if tool_detail_for_display(
            step,
            show_detail=show_tool_detail,
            mode=tool_detail_mode,
        ):
            count += 2  # detail: div + plain_text
        if step.get("result_block") or step.get("error_block"):
            count += 2  # output: div + lark_md
    return count


def find_tool_split_offset(
    *,
    base_count: int,
    seg: Segment,
    all_steps: list[ToolDisplayStep],
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> int | None:
    """寻找 tool step 拆分点，让当前卡保留尽可能多的 steps."""
    start = seg.tool_offset
    end = tool_segment_end(seg, all_steps)
    if end - start <= 1:
        return None
    for split_offset in range(end - 1, start, -1):
        estimate = estimate_tool_elements(
            start,
            split_offset,
            all_steps,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
        )
        if base_count + estimate + FOOTER_RESERVE <= ELEMENT_THRESHOLD:
            return split_offset
    return None


def build_add_segment_action(
    seg: Segment,
    all_steps: list[ToolDisplayStep],
    *,
    text_size: str = "normal_v2",
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> dict[str, Any]:
    """构造新增 segment 元素的 batch action."""
    if seg.type == SegmentType.REASONING:
        element = _build_reasoning_panel(
            " ",
            seg.elapsed_ms,
            expanded=True,
            element_id=seg.el_id,
            text_element_id=seg.text_el_id,
        )
    elif seg.type == SegmentType.ANSWER:
        element = _streaming_element(element_id=seg.el_id, text_size=text_size)
    elif seg.type == SegmentType.TOOL:
        start = seg.tool_offset
        end = seg.tool_end_offset if seg.tool_end_offset else len(all_steps)
        element = _build_tool_panel(
            all_steps[start:end],
            element_id=seg.el_id,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
        )
    else:
        raise ValueError(f"unsupported segment type: {seg.type}")

    return {
        "action": "add_elements",
        "params": {
            "type": "insert_before",
            "target_element_id": _LOADING_ELEMENT_ID,
            "elements": [element],
        },
    }


def build_add_tool_panel_action(
    steps: list[ToolDisplayStep],
    *,
    total_steps: int | None = None,
    total_failed_count: int | None = None,
    expanded: bool = True,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> dict[str, Any]:
    """Create the one fixed tool panel for the current physical card."""
    return {
        "action": "add_elements",
        "params": {
            "type": "insert_before",
            "target_element_id": _LOADING_ELEMENT_ID,
            "elements": [
                _build_tool_panel(
                    steps,
                    total_steps=total_steps,
                    total_failed_count=total_failed_count,
                    expanded=expanded,
                    element_id=TOOL_PANEL_ELEMENT_ID,
                    show_tool_detail=show_tool_detail,
                    tool_detail_mode=tool_detail_mode,
                )
            ],
        },
    }


def build_add_merged_reasoning_action() -> dict[str, Any]:
    """Create the one fixed reasoning panel used by merged presentation mode."""
    return {
        "action": "add_elements",
        "params": {
            "type": "insert_before",
            "target_element_id": _LOADING_ELEMENT_ID,
            "elements": [
                _build_reasoning_panel(
                    " ",
                    expanded=True,
                    element_id=REASONING_ELEMENT_ID,
                    text_element_id=REASONING_TEXT_ELEMENT_ID,
                )
            ],
        },
    }


def build_add_interim_preview_action(*, text_size: str = "normal_v2") -> dict[str, Any]:
    """Create the fixed, lazy commentary preview element before the loader."""
    return {
        "action": "add_elements",
        "params": {
            "type": "insert_before",
            "target_element_id": _LOADING_ELEMENT_ID,
            "elements": [
                _streaming_element(
                    element_id=STREAMING_ELEMENT_ID,
                    text_size=text_size,
                )
            ],
        },
    }


def build_reasoning_finalized_action(seg: Segment) -> dict[str, Any]:
    """构造 reasoning header 耗时终结 action."""
    elapsed = _format_elapsed(seg.elapsed_ms)
    en_label = _T["thought_for"][0].format(elapsed)
    zh_label = _T["thought_for"][1].format(elapsed)
    return {
        "action": "partial_update_element",
        "params": {
            "element_id": seg.el_id,
            "partial_element": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"💭 {en_label}",
                        "i18n_content": _i18n(f"💭 {en_label}", f"💭 {zh_label}"),
                        "text_color": "grey",
                        "text_size": "notation",
                    },
                },
            },
        },
    }


def build_tool_update_action(
    *,
    steps: list[ToolDisplayStep],
    expanded: bool = True,
    element_id: str = TOOL_PANEL_ELEMENT_ID,
    total_steps: int | None = None,
    total_failed_count: int | None = None,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> dict[str, Any]:
    """Update the fixed tool panel's header, children, and expansion state."""
    panel = _build_tool_panel(
        steps,
        total_steps=total_steps,
        total_failed_count=total_failed_count,
        expanded=expanded,
        element_id=None,
        show_tool_detail=show_tool_detail,
        tool_detail_mode=tool_detail_mode,
    )
    return {
        "action": "partial_update_element",
        "params": {
            "element_id": element_id,
            "partial_element": {
                "elements": panel["elements"],
                "header": panel["header"],
                "expanded": panel["expanded"],
            },
        },
    }
