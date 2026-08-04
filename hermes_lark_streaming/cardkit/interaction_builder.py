"""Static CardKit v2 builders for user interactions."""

from __future__ import annotations

from typing import Any, Literal

from .i18n import _LOCALES, _i18n
from .markdown import optimize_markdown_style

ClarifyCardStatus = Literal["pending", "answered", "awaiting_text", "expired"]


def _header(status: ClarifyCardStatus) -> dict[str, Any]:
    title, zh_title, template, status_text, zh_status_text, color = {
        "pending": ("❓ Your choice is needed", "❓ 需要你的选择", "blue", "Pending", "待回答", "blue"),
        "answered": ("✅ Choice received", "✅ 已选择", "green", "Answered", "已回答", "green"),
        "awaiting_text": (
            "✏️ Waiting for your answer",
            "✏️ 等待文字回答",
            "blue",
            "Awaiting text",
            "等待文字",
            "blue",
        ),
        "expired": ("⌛ Choice expired", "⌛ 选择已失效", "grey", "Expired", "已失效", "neutral"),
    }[status]
    return {
        "title": {
            "tag": "plain_text",
            "content": title,
            "i18n_content": _i18n(title, zh_title),
        },
        "text_tag_list": [
            {
                "tag": "text_tag",
                "text": {
                    "tag": "plain_text",
                    "content": status_text,
                    "i18n_content": _i18n(status_text, zh_status_text),
                },
                "color": color,
            }
        ],
        "template": template,
    }


def _button(
    label: str,
    value: dict[str, str],
    *,
    primary: bool = False,
    zh_label: str | None = None,
) -> dict[str, Any]:
    text: dict[str, Any] = {"tag": "plain_text", "content": label}
    if zh_label:
        text["i18n_content"] = _i18n(label, zh_label)
    button: dict[str, Any] = {
        "tag": "button",
        "text": text,
        "value": value,
    }
    if primary:
        button["type"] = "primary"
    return button


def build_clarify_card(
    *,
    clarify_id: str,
    question: str,
    choices: list[str],
    status: ClarifyCardStatus = "pending",
    answer: str = "",
) -> dict[str, Any]:
    """Build a pending or terminal single-select clarify card."""
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": optimize_markdown_style(str(question).strip()),
            "text_size": "normal_v2",
        },
    ]

    if status == "pending":
        actions = [
            _button(
                choice,
                {
                    "hermes_lark_action": "clarify_select",
                    "clarify_id": clarify_id,
                    "response": choice,
                },
                primary=index == 0,
            )
            for index, choice in enumerate(choices)
        ]
        actions.append(
            _button(
                "Other…",
                {
                    "hermes_lark_action": "clarify_other",
                    "clarify_id": clarify_id,
                },
                zh_label="其他回答…",
            )
        )
        elements.extend(actions)
    elif status == "answered":
        elements.append(
            {
                "tag": "markdown",
                "content": f"**Selected:**\n{optimize_markdown_style(str(answer).strip())}",
                "i18n_content": _i18n(
                    f"**Selected:**\n{optimize_markdown_style(str(answer).strip())}",
                    f"**已选择**\n{optimize_markdown_style(str(answer).strip())}",
                ),
            }
        )
    elif status == "awaiting_text":
        elements.append(
            {
                "tag": "markdown",
                "content": "Please send your custom answer as your next message.",
                "i18n_content": _i18n(
                    "Please send your custom answer as your next message.",
                    "请在下一条消息中直接发送你的自定义答案。",
                ),
            }
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": "This choice is no longer pending.",
                "i18n_content": _i18n(
                    "This choice is no longer pending.",
                    "这个选择已不再等待回答。",
                ),
            }
        )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True, "locales": _LOCALES},
        "header": _header(status),
        "body": {"elements": elements},
    }
