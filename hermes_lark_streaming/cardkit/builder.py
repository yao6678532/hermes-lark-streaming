"""CardKit v2.0 卡片构建器 — i18n、元素构建、卡片组装."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from ..streaming.progress import ProgressSnapshot
from ..streaming.segments import Segment, SegmentType
from ..streaming.tooluse import ToolDisplayStep, tool_detail_for_display
from .i18n import _LOCALES, _T, _i18n, _t
from .markdown import (
    _downgrade_tables,
    _split_long_text,
    optimize_markdown_style,
)

STREAMING_ELEMENT_ID = "streaming_content"
REASONING_ELEMENT_ID = "reasoning_content"
REASONING_TEXT_ELEMENT_ID = "reasoning_text"
TOOL_PANEL_ELEMENT_ID = "tool_panel"
_LOADING_ELEMENT_ID = "loading_icon"
_LOADING_IMG_KEY = "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"
_logger = logging.getLogger("hermes_lark_streaming.cardkit")


def _collapsible_panel(
    *,
    expanded: bool,
    title_el: dict,
    elements: list[dict],
    vertical_spacing: str = "4px",
    icon_position: str = "right",
) -> dict:
    icon_el = {
        "tag": "standard_icon",
        "token": "down-small-ccm_outlined",
        "size": "16px 16px",
    }
    if icon_position == "right":
        icon_el["color"] = "grey"
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": title_el,
            "vertical_align": "center",
            "icon": icon_el,
            "icon_position": icon_position,
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": vertical_spacing,
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }


def _streaming_element(
    content: str = "",
    *,
    element_id: str = STREAMING_ELEMENT_ID,
    text_size: str = "normal_v2",
) -> dict:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": text_size,
        "margin": "0px 0px 0px 0px",
        "element_id": element_id,
    }


_HEADER_STATES: dict[str, dict[str, str]] = {
    "streaming": {"template": "blue", "i18n_key": "processing_prefix"},
    "completed": {"template": "green", "i18n_key": "status_completed"},
    "error": {"template": "red", "i18n_key": "status_error"},
    "stopped": {"template": "red", "i18n_key": "status_stopped"},
}


def _build_header(status: str) -> dict[str, Any]:
    """构建卡片级 header — 流式蓝 / 完成绿 / 停止红."""
    cfg = _HEADER_STATES.get(status, _HEADER_STATES["completed"])
    en_text, zh_text = _T[cfg["i18n_key"]]
    return {
        "title": {
            "tag": "plain_text",
            "content": en_text,
            "i18n_content": _i18n(en_text, zh_text),
        },
        "template": cfg["template"],
    }


def _loading_element(progress_snapshot: ProgressSnapshot | None = None) -> dict[str, Any]:
    heartbeat = progress_snapshot if progress_snapshot and progress_snapshot.visible else None
    return {
        "tag": "markdown",
        "content": heartbeat.content if heartbeat else " ",
        "icon": {
            "tag": "custom_icon",
            "img_key": _LOADING_IMG_KEY,
            "size": "16px 16px",
        },
        "element_id": _LOADING_ELEMENT_ID,
        **(
            {"i18n_content": _i18n(heartbeat.content, heartbeat.zh_content)}
            if heartbeat
            else {}
        ),
    }


def _build_tool_panel(
    steps: list[ToolDisplayStep],
    elapsed_ms: float = 0,
    *,
    expanded: bool = True,
    element_id: str | None = TOOL_PANEL_ELEMENT_ID,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> dict:
    en_t, zh_t = _T["tool_use"]
    en_parts, zh_parts = [en_t], [zh_t]
    if steps:
        tpl_en, tpl_zh = _T["steps"]
        en_parts.append(tpl_en.format(len(steps), "s" if len(steps) > 1 else ""))
        zh_parts.append(tpl_zh.format(len(steps), ""))
        failed_count = sum(step.get("status") == "error" for step in steps)
        if failed_count:
            en_parts.append(f"{failed_count} failed")
            zh_parts.append(f"{failed_count} 个失败")

    children: list[dict] = []
    for s in steps:
        children.extend(
            _build_tool_step_elements(
                s,
                show_tool_detail=show_tool_detail,
                tool_detail_mode=tool_detail_mode,
            )
        )

    panel = _collapsible_panel(
        expanded=expanded,
        title_el={
            "tag": "plain_text",
            "content": " · ".join(en_parts),
            "i18n_content": _i18n(" · ".join(en_parts), " · ".join(zh_parts)),
            "text_color": "grey",
            "text_size": "notation",
        },
        elements=children,
    )
    if element_id:
        panel["element_id"] = element_id
    return panel


def _build_tool_step_elements(
    step: ToolDisplayStep,
    *,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> list[dict]:
    elements: list[dict] = [_build_tool_step_title(step)]
    detail = _build_tool_step_detail(
        step,
        show_tool_detail=show_tool_detail,
        tool_detail_mode=tool_detail_mode,
    )
    if detail:
        elements.append(detail)
    output = _build_tool_step_output(step)
    if output:
        elements.append(output)
    return elements


def _build_tool_step_title(step: ToolDisplayStep) -> dict:
    status = step.get("status", "running")
    status_info = _tool_status_info(status)
    title = step.get("title", step.get("name", "tool"))
    elapsed_ms = step.get("elapsed_ms", 0) or 0
    if status == "success":
        label = _format_elapsed(elapsed_ms) if elapsed_ms > 0 else _T["done_label"][0]
        content = f"**{_escape_md(title)}** · {label}"
    else:
        label = status_info["label"]
        if status == "error" and elapsed_ms > 0:
            label = f"{label} · {_format_elapsed(elapsed_ms)}"
        content = f"**{_escape_md(title)}** · <font color='{status_info['color']}'>{label}</font>"
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": step.get("icon", "tool_02"),
            "color": "grey",
        },
        "text": {
            "tag": "lark_md",
            "content": content,
            "text_size": "notation",
        },
    }


def _build_tool_step_detail(
    step: ToolDisplayStep,
    *,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> dict | None:
    detail = tool_detail_for_display(
        step,
        show_detail=show_tool_detail,
        mode=tool_detail_mode,
    )
    if not detail:
        return None
    return {
        "tag": "div",
        "margin": "0px 0px 0px 22px",
        "text": {
            "tag": "plain_text",
            "content": detail,
            "text_color": "grey",
            "text_size": "notation",
        },
    }


def _build_tool_step_output(step: ToolDisplayStep) -> dict | None:
    error_block = step.get("error_block")
    result_block = step.get("result_block")

    lines: list[str] = []
    if error_block:
        lines.append("**Error**")
        lines.append(
            error_block.get("fenced")
            or _format_code_block(error_block.get("content", ""), error_block.get("language", "text"))
        )
    elif result_block:
        lines.append("**Result**")
        lines.append(
            result_block.get("fenced")
            or _format_code_block(result_block.get("content", ""), result_block.get("language", "json"))
        )

    if not lines:
        return None

    return {
        "tag": "div",
        "margin": "0px 0px 0px 22px",
        "text": {
            "tag": "lark_md",
            "content": "\n".join(lines),
            "text_size": "notation",
        },
    }


def _tool_status_info(status: str) -> dict[str, str]:
    return {
        "running": {"label": _T["running"][0], "color": "turquoise"},
        "success": {"label": _T["done_label"][0], "color": "grey"},
        "error": {"label": _T["failed"][0], "color": "red"},
    }.get(status, {"label": status.capitalize(), "color": "grey"})


def _format_code_block(content: str, language: str) -> str:
    normalized = content.replace("\r\n", "\n").strip()
    fence = "`" * max(3, _longest_backtick_run(normalized) + 1)
    return f"{fence}{language}\n{normalized}\n{fence}"


def _longest_backtick_run(value: str) -> int:
    matches = re.findall(r"`+", value)
    return max((len(m) for m in matches), default=0)


def _escape_md(value: str) -> str:
    return re.sub(r"([`*_{}\[\]<>])", r"\\\1", value.replace("\\", "\\\\"))


def _build_reasoning_panel(
    text: str, elapsed_ms: float = 0, *, expanded: bool = False, element_id: str | None = None,
    text_element_id: str | None = REASONING_TEXT_ELEMENT_ID,
) -> dict:
    if elapsed_ms > 0:
        d = _format_elapsed(elapsed_ms)
        en_label, zh_label = _T["thought_for"][0].format(d), _T["thought_for"][1].format(d)
    elif not text.strip():
        en_label, zh_label = _T["thinking_panel"]
    else:
        en_label, zh_label = _T["thought"]
    panel = _collapsible_panel(
        expanded=expanded,
        title_el={
            "tag": "plain_text",
            "content": f"💭 {en_label}",
            "i18n_content": _i18n(f"💭 {en_label}", f"💭 {zh_label}"),
            "text_color": "grey",
            "text_size": "notation",
        },
        elements=[{
            "tag": "markdown",
            "content": text,
            "text_size": "notation",
            **({"element_id": text_element_id} if text_element_id else {}),
        }],
        vertical_spacing="8px",
    )
    if element_id:
        panel["element_id"] = element_id
    return panel


def _build_footer_elements(
    footer_data: dict | None,
    is_error: bool = False,
    is_aborted: bool = False,
    fields: list[list[str]] | None = None,
    show_label: bool = False,
    text_size: str = "notation",
) -> list[dict]:
    """Build the terminal Run Details presentation.

    The public/internal footer naming is intentionally retained for config and
    session compatibility.  Only its terminal presentation changes here.
    """
    try:
        return _build_run_details_elements(
            footer_data,
            is_error=is_error,
            is_aborted=is_aborted,
            fields=fields,
            show_label=show_label,
            text_size=text_size,
        )
    except Exception:
        # A presentation-only failure must not prevent the answer card from
        # being delivered.  Keep the old compact footer as a fail-open path.
        _logger.exception("Run Details build failed; falling back to legacy footer")
        return _build_legacy_footer_elements(
            footer_data,
            is_error=is_error,
            is_aborted=is_aborted,
            fields=fields,
            show_label=show_label,
            text_size=text_size,
        )


def _build_run_details_elements(
    footer_data: dict | None,
    *,
    is_error: bool = False,
    is_aborted: bool = False,
    fields: list[list[str]] | None = None,
    show_label: bool = False,
    text_size: str = "notation",
) -> list[dict]:
    """Build the collapsed terminal Run Details panel from footer metadata."""
    if fields is None:
        fields = [["status", "elapsed", "context", "model"]]

    data = footer_data or {}
    summary_parts_en: list[str] = []
    summary_parts_zh: list[str] = []
    for field in ("elapsed", "model"):
        en, zh = _render_footer_field(field, data, is_error, is_aborted, False)
        if en:
            summary_parts_en.append(en)
        if zh:
            summary_parts_zh.append(zh)

    en_lines: list[str] = []
    zh_lines: list[str] = []
    # GPT quota is more useful than context in the footer. Keep context for
    # non-GPT models (e.g. DeepSeek) where gpt_quota is empty/hidden.
    hide_context = bool(data.get("gpt_quota"))
    for row in fields:
        en_parts: list[str] = []
        zh_parts: list[str] = []
        for field in row:
            if hide_context and field == "context":
                continue
            # Run Details always uses explicit field labels.  Keep
            # footer.show_label accepted for config compatibility, but do not
            # duplicate labels already supplied by this presentation.
            en, zh = _render_footer_field(field, data, is_error, is_aborted, False)
            if en:
                label_en, label_zh = _footer_field_label(field)
                en_parts.append(f"{label_en} {en}" if label_en else en)
                zh_parts.append(f"{label_zh} {zh}" if label_zh and zh else (zh or en))
        if en_parts:
            en_lines.append(" · ".join(en_parts))
            zh_lines.append(" · ".join(zh_parts))

    if not en_lines:
        return []

    en_content = "\n".join(en_lines)
    zh_content = "\n".join(zh_lines)
    if is_error:
        en_content = f"<font color='red'>{en_content}</font>"
        zh_content = f"<font color='red'>{zh_content}</font>"

    title_en = _T["run_details"][0]
    title_zh = _T["run_details"][1]
    if summary_parts_en:
        title_en += " · " + " · ".join(summary_parts_en)
    if summary_parts_zh:
        title_zh += " · " + " · ".join(summary_parts_zh)

    panel = _collapsible_panel(
        expanded=False,
        title_el={
            "tag": "plain_text",
            "content": title_en,
            "i18n_content": _i18n(title_en, title_zh),
            "text_color": "grey",
            "text_size": text_size,
        },
        elements=[
            {
                "tag": "markdown",
                "content": en_content,
                "i18n_content": _i18n(en_content, zh_content),
                "text_size": text_size,
            }
        ],
    )
    return [{"tag": "hr"}, panel]


def _footer_field_label(name: str) -> tuple[str, str]:
    """Return the existing/localized label for a supported footer field."""
    if name == "elapsed":
        return _T["elapsed"][0].format("").strip(), _T["elapsed"][1].format("").strip()
    if name == "context":
        return _T["context"][0].format("").strip(), _T["context"][1].format("").strip()
    return _T.get(f"{name}_label", ("", ""))


def _build_legacy_footer_elements(
    footer_data: dict | None,
    *,
    is_error: bool = False,
    is_aborted: bool = False,
    fields: list[list[str]] | None = None,
    show_label: bool = False,
    text_size: str = "notation",
) -> list[dict]:
    """Fail-open compatibility presentation for unexpected Run Details errors."""
    if fields is None:
        fields = [["status", "elapsed", "context", "model"]]

    data = footer_data or {}
    en_lines: list[str] = []
    zh_lines: list[str] = []
    hide_context = bool(data.get("gpt_quota"))
    for row in fields:
        en_parts: list[str] = []
        zh_parts: list[str] = []
        for field in row:
            if hide_context and field == "context":
                continue
            en, zh = _render_footer_field(field, data, is_error, is_aborted, show_label)
            if en:
                en_parts.append(en)
                if zh:
                    zh_parts.append(zh)
        if en_parts:
            if en_parts[0] == "✅" and len(en_parts) > 1:
                en_lines.append(en_parts[0] + " " + " · ".join(en_parts[1:]))
            else:
                en_lines.append(" · ".join(en_parts))
            if zh_parts and zh_parts[0] == "✅" and len(zh_parts) > 1:
                zh_lines.append(zh_parts[0] + " " + " · ".join(zh_parts[1:]))
            else:
                zh_lines.append(" · ".join(zh_parts))

    if not en_lines:
        return []
    en_content = "\n".join(en_lines)
    zh_content = "\n".join(zh_lines)
    if is_error:
        en_content = f"<font color='red'>{en_content}</font>"
        zh_content = f"<font color='red'>{zh_content}</font>"
    return [
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": en_content,
            "i18n_content": _i18n(en_content, zh_content),
            "text_size": text_size,
        },
    ]


def _render_footer_field(
    name: str,
    data: dict,
    is_error: bool,
    is_aborted: bool,
    show_label: bool,
) -> tuple[str | None, str | None]:
    if name == "status":
        if is_error:
            return _T["status_error"]
        if is_aborted:
            return _T["status_stopped"]
        # Footer is space-constrained; keep completed status icon-only.
        # Header still uses the localized "Completed/已完成" label.
        return "✅", "✅"

    if name == "elapsed":
        duration = data.get("duration", 0)
        if isinstance(duration, (int, float)) and duration > 0:
            val = _format_elapsed(duration * 1000)
            if show_label:
                return _T["elapsed"][0].format(val), _T["elapsed"][1].format(val)
            return val, val
        return None, None

    if name == "model":
        v = data.get("model") or None
        return v, v

    if name == "tokens":
        input_t = data.get("input_tokens", 0) or 0
        output_t = data.get("output_tokens", 0) or 0
        if input_t or output_t:
            v = f"↑ {_compact(input_t)} ↓ {_compact(output_t)}"
            return v, v
        return None, None

    if name == "context":
        used = data.get("context_used", 0) or 0
        max_c = data.get("context_max", 0) or 0
        if max_c:
            pct = int(used / max_c * 100)
            val = f"{_compact(used)}/{_compact(max_c)} ({pct}%)"
            if show_label:
                return _T["context"][0].format(val), _T["context"][1].format(val)
            return val, val
        return None, None

    if name == "balance":
        v = data.get("balance") or None
        return v, v

    if name == "gpt_quota":
        v = data.get("gpt_quota") or None
        return v, v

    return None, None


def _compact(n: int) -> str:
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"{int(m)}M" if m >= 100 else f"{m:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _format_elapsed(ms: float) -> str:
    seconds = ms / 1000
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60)}s"


def build_streaming_tool_use_pending_panel() -> dict[str, Any]:
    return _collapsible_panel(
        expanded=False,
        title_el={
            "tag": "plain_text",
            "content": _T["tool_pending"][0],
            "i18n_content": _t("tool_pending"),
            "text_color": "grey",
            "text_size": "notation",
        },
        elements=[],
    )


def build_streaming_card_v2(
    *,
    tool_steps: list[ToolDisplayStep] | None = None,
    elapsed_ms: float = 0,
    show_tool_use: bool = True,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
    show_reasoning: bool = False,
    show_streaming_element: bool = True,
    header_enabled: bool = False,
    text_size: str = "normal_v2",
    width_mode: str = "default",
    progress_snapshot: ProgressSnapshot | None = None,
) -> dict[str, Any]:
    """CardKit 2.0 流式占位卡片 — 内容后保留固定尾部 anchor."""
    elements: list[dict] = []

    if show_reasoning:
        elements.append(
            _build_reasoning_panel(" ", expanded=True, element_id=REASONING_ELEMENT_ID)
        )

    if show_tool_use:
        if tool_steps:
            elements.append(
                _build_tool_panel(
                    tool_steps,
                    elapsed_ms,
                    show_tool_detail=show_tool_detail,
                    tool_detail_mode=tool_detail_mode,
                )
            )
        else:
            elements.append(build_streaming_tool_use_pending_panel())

    if show_streaming_element:
        elements.append(_streaming_element(text_size=text_size))
    elements.append(_loading_element(progress_snapshot))

    card = {
        "schema": "2.0",
        "config": {
            "width_mode": width_mode,
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 15},
                "print_step": {"default": 1},
                "print_strategy": "fast",
            },
            "locales": _LOCALES,
            "summary": {
                "content": _T["processing"][0],
                "i18n_content": _t("processing"),
            },
        },
        "body": {"elements": elements},
    }
    if header_enabled:
        card["header"] = _build_header("streaming")
    return card


def build_complete_card(
    *,
    segments: list[Segment],
    all_tool_steps: list[ToolDisplayStep],
    footer_data: dict | None = None,
    is_error: bool = False,
    is_aborted: bool = False,
    footer_fields: list[list[str]] | None = None,
    footer_show_label: bool = True,
    footer_enabled: bool = True,
    footer_text_size: str = "notation",
    panel_expanded: bool = False,
    header_enabled: bool = False,
    body_text_size: str = "normal_v2",
    show_tool_use: bool = True,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
    width_mode: str = "default",
    merged_reasoning_text: str | None = None,
    merged_reasoning_elapsed_ms: float = 0,
) -> dict[str, Any]:
    """完成态流式卡片 — 按 segments 顺序渲染."""
    elements: list[dict] = []
    has_answer = False
    merged_mode = merged_reasoning_text is not None

    if merged_reasoning_text:
        elements.append(
            _build_reasoning_panel(
                merged_reasoning_text,
                merged_reasoning_elapsed_ms,
                expanded=panel_expanded,
                element_id=None,
                text_element_id=None,
            )
        )

    tool_rendered = False
    tool_segments = [seg for seg in segments if seg.type == SegmentType.TOOL]
    tool_start = min((seg.tool_offset for seg in tool_segments), default=0)
    tool_end = max(
        (seg.tool_end_offset if seg.tool_end_offset else len(all_tool_steps) for seg in tool_segments),
        default=0,
    )

    for seg in segments:
        if seg.type == SegmentType.REASONING:
            if merged_mode:
                continue
            if seg.text:
                elements.append(_build_reasoning_panel(
                    seg.text, seg.elapsed_ms, expanded=panel_expanded,
                    element_id=None, text_element_id=None,
                ))
        elif seg.type == SegmentType.TOOL:
            if not show_tool_use:
                continue
            if tool_rendered:
                continue
            tool_rendered = True
            steps = all_tool_steps[tool_start:tool_end]
            if steps:
                elements.append(
                    _build_tool_panel(
                        steps,
                        expanded=panel_expanded,
                        element_id=TOOL_PANEL_ELEMENT_ID,
                        show_tool_detail=show_tool_detail,
                        tool_detail_mode=tool_detail_mode,
                    )
                )
        elif seg.type == SegmentType.ANSWER and seg.text:
            has_answer = True
            content = _downgrade_tables(optimize_markdown_style(seg.text))
            for chunk in _split_long_text(content):
                elements.append({"tag": "markdown", "content": chunk, "text_size": body_text_size})

    if not has_answer:
        elements.append({"tag": "markdown", "content": _T["done"][0], "text_size": body_text_size})

    if footer_enabled:
        elements.extend(
            _build_footer_elements(
                footer_data,
                is_error,
                is_aborted,
                fields=footer_fields,
                show_label=footer_show_label,
                text_size=footer_text_size,
            )
        )

    summary_text = ""
    for seg in reversed(segments):
        if seg.type in (SegmentType.ANSWER, SegmentType.REASONING) and seg.text:
            summary_text = seg.text
            break
    summary = summary_text[:120].replace("\n", " ").replace("```", "").strip()

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "width_mode": width_mode,
            "wide_screen_mode": True,
            "update_multi": True,
            "locales": _LOCALES,
        },
    }
    if summary:
        card["config"]["summary"] = {"content": summary}
    card["body"] = {"elements": elements}
    if header_enabled:
        header_status = "error" if is_error else "stopped" if is_aborted else "completed"
        card["header"] = _build_header(header_status)
    return card


def _format_run_time(run_time: str) -> str:
    """将 ISO 时间戳格式化为可读日期时间，失败则原样返回."""
    if not run_time:
        return ""
    try:
        dt = datetime.fromisoformat(run_time)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return run_time


def build_cron_card(
    content: str, *, task_name: str = "", run_time: str = ""
) -> dict[str, Any]:
    """Cron 推送用的极简静态卡片 — schema 2.0，可选 header + markdown 内容."""
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "locales": _LOCALES},
        "body": {"elements": []},
    }
    header_parts = [p for p in (task_name, _format_run_time(run_time)) if p]
    if header_parts:
        card["header"] = {
            "title": {"tag": "lark_md", "content": ":Alarm: " + " · ".join(header_parts)},
            "template": "blue",
        }
    if not content.strip():
        return card
    summary = content[:120].replace("\n", " ").replace("```", "").strip()
    if summary:
        card["config"]["summary"] = {"content": summary}
    for chunk in _split_long_text(optimize_markdown_style(content)):
        if chunk.strip():
            card["body"]["elements"].append({"tag": "markdown", "content": chunk})
    return card


def build_background_card(preview: str, content: str) -> dict[str, Any]:
    """Background 任务完成推送卡片 — schema 2.0，header + markdown."""
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "locales": _LOCALES},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ Background: \"{preview}\""},
        },
        "body": {"elements": []},
    }
    body = content if content.strip() else "(No response generated)"
    summary = body[:120].replace("\n", " ").replace("```", "").strip()
    if summary:
        card["config"]["summary"] = {"content": summary}
    for chunk in _split_long_text(optimize_markdown_style(body)):
        if chunk.strip():
            card["body"]["elements"].append({"tag": "markdown", "content": chunk})
    return card
