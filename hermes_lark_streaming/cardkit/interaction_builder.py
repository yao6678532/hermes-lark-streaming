"""Static CardKit v2 builders for user interactions."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Literal

from .i18n import _LOCALES, _i18n
from .markdown import optimize_markdown_style

ClarifyCardStatus = Literal["pending", "input", "answered", "awaiting_text", "expired"]
ApprovalCardStatus = Literal["pending", "approved", "denied", "expired"]

_CLARIFY_DROPDOWN_LABEL_LIMIT = 40


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


def _build_responsive_button_group(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one CardKit action group with equal desktop/mobile button areas."""
    columns = []
    for button in buttons:
        rendered = deepcopy(button)
        rendered["width"] = "fill"
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "center",
                "elements": [rendered],
            }
        )
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "medium",
        "horizontal_align": "center",
        "columns": columns,
    }


def _clarify_question(question: str, *, pending: bool) -> dict[str, Any]:
    element: dict[str, Any] = {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": optimize_markdown_style(str(question).strip()),
        },
    }
    if pending:
        element["icon"] = {
            "tag": "standard_icon",
            "token": "info_outlined",
            "size": "20px 20px",
            "color": "blue",
        }
    return element


def _choice_marker(index: int) -> str:
    return chr(ord("A") + index) if index < 26 else str(index + 1)


def _clean_choice_display(index: int, choice: str) -> str:
    """Remove only a position-matching choice prefix for presentation."""
    marker = _choice_marker(index)
    position = str(index + 1)
    prefix = re.compile(
        rf"^\s*(?:{re.escape(marker)}|{re.escape(position)})\s*[.\u3001)\uff09:\uff1a]\s*"
    )
    return prefix.sub("", str(choice), count=1)


def _clarify_choice_markdown(choices: list[str]) -> str:
    """Render canonical choices in full; dropdown shortening never reaches this lane."""
    return "\n\n".join(
        f"**{_choice_marker(index)}.** {optimize_markdown_style(_clean_choice_display(index, choice))}"
        for index, choice in enumerate(choices)
    )


def _short_choice_label(index: int, choice: str) -> str:
    """Build presentation-only plain text for the compact picker."""
    compact = " ".join(_clean_choice_display(index, choice).split())
    if len(compact) > _CLARIFY_DROPDOWN_LABEL_LIMIT:
        compact = compact[: _CLARIFY_DROPDOWN_LABEL_LIMIT - 1].rstrip() + "…"
    return f"{_choice_marker(index)}. {compact}"


def _clarify_options(choices: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "text": {"tag": "plain_text", "content": _short_choice_label(index, choice)},
            "value": str(index),
        }
        for index, choice in enumerate(choices)
    ]


def _clarify_direct_input(clarify_id: str) -> dict[str, Any]:
    """Build the pending-card custom-answer input as an independent action."""
    return {
        "tag": "input",
        "name": "clarify_direct_input",
        "element_id": "clarify_direct_input",
        "max_length": 500,
        "placeholder": {
            "tag": "plain_text",
            "content": "Type another answer...",
            "i18n_content": _i18n("Type another answer...", "请输入其他回答…"),
        },
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "hermes_lark_action": "clarify_direct_input",
                    "clarify_id": clarify_id,
                },
            }
        ],
    }


def build_open_clarify_card(*, question: str) -> dict[str, Any]:
    """Build a presentation-only prompt for Hermes' open-text clarify flow.

    The official pending entry continues to own the next-message resolver.
    This card intentionally has neither controls nor a registry identity.
    """
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True, "locales": _LOCALES},
        "body": {
            "elements": [
                _clarify_question(question, pending=True),
                {
                    "tag": "markdown",
                    "content": "Please send your answer.",
                    "i18n_content": _i18n(
                        "Please send your answer.",
                        "请直接发送你的回答。",
                    ),
                },
            ]
        },
    }


def build_clarify_card(
    *,
    clarify_id: str,
    question: str,
    choices: list[str],
    status: ClarifyCardStatus = "pending",
    answer: str = "",
    multi_select: bool = False,
) -> dict[str, Any]:
    """Build a Clarify card without putting canonical choices in controls."""
    elements: list[dict[str, Any]] = [
        _clarify_question(question, pending=status in {"pending", "input", "awaiting_text"})
    ]

    if status == "pending":
        elements.append(
            {
                "tag": "markdown",
                "content": _clarify_choice_markdown(choices),
                "text_size": "normal_v2",
            }
        )
        options = _clarify_options(choices)
        placeholder = {
            "tag": "plain_text",
            "content": "Select one or more options" if multi_select else "Select an option",
            "i18n_content": _i18n(
                "Select one or more options" if multi_select else "Select an option",
                "选择一个或多个选项" if multi_select else "选择一个选项",
            ),
        }
        if multi_select:
            elements.append(
                {
                    "tag": "form",
                    "name": "clarify_multi_form",
                    "element_id": "clarify_multi_form",
                    "elements": [
                        {
                            "tag": "multi_select_static",
                            "name": "clarify_multi_select",
                            "element_id": "clarify_multi_select",
                            "placeholder": placeholder,
                            "options": options,
                        },
                        {
                            "tag": "button",
                            "name": "clarify_multi_submit",
                            "element_id": "clarify_multi_submit",
                            "value": {
                                "hermes_lark_action": "clarify_multi_submit",
                                "clarify_id": clarify_id,
                            },
                            "text": {
                                "tag": "plain_text",
                                "content": "Submit",
                                "i18n_content": _i18n("Submit", "提交"),
                            },
                            "type": "primary",
                            "form_action_type": "submit",
                        },
                    ],
                }
            )
        else:
            elements.append(
                {
                    "tag": "select_static",
                    "element_id": "clarify_select",
                    "placeholder": placeholder,
                    "options": options,
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "hermes_lark_action": "clarify_select",
                                "clarify_id": clarify_id,
                            },
                        }
                    ],
                }
            )
        elements.append(_clarify_direct_input(clarify_id))
    elif status == "input":
        submit = {
            "tag": "button",
            "name": "clarify_other_submit",
            "value": {
                "hermes_lark_action": "clarify_other_submit",
                "clarify_id": clarify_id,
            },
            "text": {
                "tag": "plain_text",
                "content": "Submit",
                "i18n_content": _i18n("Submit", "提交"),
            },
            "type": "primary",
            "form_action_type": "submit",
        }
        back = {
            "tag": "button",
            "name": "clarify_other_back",
            "value": {
                "hermes_lark_action": "clarify_other_back",
                "clarify_id": clarify_id,
            },
            "text": {
                "tag": "plain_text",
                "content": "Back to choices",
                "i18n_content": _i18n("Back to choices", "返回选项"),
            },
            "form_action_type": "submit",
        }
        elements.append(
            {
                "tag": "form",
                "name": "clarify_other_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "clarify_other_input",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "Type your answer...",
                            "i18n_content": _i18n("Type your answer...", "请输入其他回答…"),
                        },
                    },
                    _build_responsive_button_group([submit, back]),
                ],
            }
        )
    elif status == "answered":
        elements.append(
            {
                "tag": "div",
                "icon": {
                    "tag": "standard_icon",
                    "token": "resolve_filled",
                    "size": "18px 18px",
                    "color": "green",
                },
                "text": {
                    "tag": "lark_md",
                    "content": optimize_markdown_style(str(answer).strip()),
                },
            }
        )
        elements.append(
            {
                "tag": "markdown",
                "content": "<font color='grey'>Confirmed</font>",
                "i18n_content": _i18n(
                    "<font color='grey'>Confirmed</font>",
                    "<font color='grey'>已确认</font>",
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
                "tag": "div",
                "icon": {
                    "tag": "standard_icon",
                    "token": "info_outlined",
                    "size": "16px 16px",
                    "color": "grey",
                },
                "text": {
                    "tag": "plain_text",
                    "content": "This question has expired",
                    "i18n_content": _i18n("This question has expired", "此问题已失效"),
                },
            }
        )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True, "locales": _LOCALES},
        "body": {"elements": elements},
    }


_APPROVAL_ACTION_LABELS: dict[str, tuple[str, str]] = {
    "approve_once": ("Allow once", "允许一次"),
    "approve_session": ("Allow for session", "本会话允许"),
    "approve_always": ("Always allow", "始终允许"),
    "deny": ("Deny", "拒绝"),
}
_APPROVAL_DECISION_LABELS: dict[str, tuple[str, str]] = {
    "once": ("Allowed once", "允许一次"),
    "session": ("Allowed for this session", "本会话允许"),
    "always": ("Always allowed", "始终允许"),
    "deny": ("Denied", "已拒绝"),
}


def _approval_header(status: ApprovalCardStatus) -> dict[str, Any]:
    title, zh_title, template, status_text, zh_status_text, color = {
        "pending": ("⚠️ Approval required", "⚠️ 需要你的批准", "orange", "Pending", "待审批", "orange"),
        "approved": ("✅ Approved", "✅ 已批准", "green", "Approved", "已批准", "green"),
        "denied": ("❌ Denied", "❌ 已拒绝", "red", "Denied", "已拒绝", "red"),
        "expired": ("⌛ Approval expired", "⌛ 审批已失效", "grey", "Expired", "已失效", "neutral"),
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


def _style_approval_button(button: dict[str, Any]) -> dict[str, Any]:
    """Restyle a Hermes button without changing its callback value payload."""
    result = deepcopy(button)
    value = result.get("value")
    action = str(value.get("hermes_action") or "") if isinstance(value, dict) else ""
    translated = _APPROVAL_ACTION_LABELS.get(action)
    if translated is not None:
        label, zh_label = translated
        result["text"] = {
            "tag": "plain_text",
            "content": label,
            "i18n_content": _i18n(label, zh_label),
        }
    result["type"] = "primary" if action == "approve_once" else "danger" if action == "deny" else "default"
    # CardKit's supported fill width keeps actions visually equal in the
    # stacked stretch layout; the callback payload remains untouched.
    result["width"] = "fill"
    return result


def _build_responsive_action_row(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """Place every official approval action in one responsive CardKit group."""
    return _build_responsive_button_group([_style_approval_button(button) for button in buttons])


def build_approval_card(
    *,
    command: str,
    description: str,
    buttons: list[dict[str, Any]],
    status: ApprovalCardStatus = "pending",
    decision: str = "",
) -> dict[str, Any]:
    """Build approval presentation from Hermes' authoritative button set."""
    elements: list[dict[str, Any]] = []
    clean_description = optimize_markdown_style(str(description).strip())
    if clean_description:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**About to execute:**\n{clean_description}",
                "i18n_content": _i18n(
                    f"**About to execute:**\n{clean_description}",
                    f"**即将执行：**\n{clean_description}",  # noqa: RUF001
                ),
                "text_size": "normal_v2",
            }
        )
    clean_command = str(command).strip()
    if clean_command:
        command_block = optimize_markdown_style(f"```\n{clean_command}\n```")
        elements.append(
            {
                "tag": "markdown",
                "content": f"**Command**\n{command_block}",
                "i18n_content": _i18n(
                    f"**Command**\n{command_block}",
                    f"**命令**\n{command_block}",
                ),
                "text_size": "normal_v2",
            }
        )

    if status == "pending" and buttons:
        elements.append(_build_responsive_action_row(buttons))
    elif status in {"approved", "denied"}:
        label, zh_label = _APPROVAL_DECISION_LABELS.get(
            decision,
            (str(decision or "Resolved"), str(decision or "已处理")),
        )
        elements.append(
            {
                "tag": "markdown",
                "content": f"**Decision:**\n{label}",
                "i18n_content": _i18n(
                    f"**Decision:**\n{label}",
                    f"**最终决定：**\n{zh_label}",  # noqa: RUF001
                ),
            }
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": "This approval is no longer pending. The operation was not authorized by this click.",
                "i18n_content": _i18n(
                    "This approval is no longer pending. The operation was not authorized by this click.",
                    "该审批已不再等待处理；本次点击没有授权执行该操作。",  # noqa: RUF001
                ),
            }
        )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True, "locales": _LOCALES},
        "header": _approval_header(status),
        "body": {"elements": elements},
    }
