"""Fail early on the specific host adapter calls used by the native plugin."""

from __future__ import annotations

import inspect
from typing import Any

# Bind calls rather than comparing signatures: additive optional parameters and
# **kwargs remain compatible, while removed/renamed required inputs fail.
CALLS: dict[str, dict[str, Any]] = {
    "__init__": {"config": None},
    "handle_message": {"event": None},
    "on_processing_start": {"event": None},
    "on_processing_complete": {"event": None, "outcome": None},
    "max_message_length_for_chat": {"chat_id": ""},
    "send_or_update_status": {"chat_id": "", "status_key": "", "content": "", "metadata": {}},
    "connect": {"is_reconnect": False},
    "disconnect": {},
    "send": {"chat_id": "", "content": "", "reply_to": None, "metadata": {}},
    "edit_message": {"chat_id": "", "message_id": "", "content": "", "finalize": False},
    "send_clarify": {"chat_id": "", "question": "", "choices": [], "clarify_id": "", "session_key": "", "metadata": {}},
    "send_update_prompt": {"chat_id": "", "prompt": "", "session_key": "", "metadata": {}},
    "send_choice_picker": {
        "chat_id": "",
        "title": "",
        "choices": [],
        "session_key": "",
        "on_choice_selected": None,
        "metadata": {},
    },
    "interrupt_session_activity": {"session_key": "", "chat_id": "", "metadata": {}},
    "_send_exec_approval_prompt": {"prompt": None},
    "_send_interactive_card": {
        "chat_id": "",
        "card": {},
        "metadata": {},
        "failure_message": "",
        "state_map": {},
        "state_id": 0,
        "session_key": "",
        "extra_state": {},
    },
    "_validate_card_action": {"event": None, "state": {}, "label": "approval", "ident": 0},
    "_handle_approval_card_action": {"event": None, "action_value": {}, "loop": None},
}


def adapter_contract_errors(base: Any) -> list[str]:
    errors: list[str] = []
    for name, arguments in CALLS.items():
        method = getattr(base, name, None)
        if not callable(method):
            errors.append(f"FeishuAdapter.{name} missing")
            continue
        try:
            inspect.signature(method).bind(None, **arguments)
        except (TypeError, ValueError) as error:
            errors.append(f"FeishuAdapter.{name}: {error}")
    for name in ("_card_response", "_card_toast"):
        if not callable(getattr(base, name, None)):
            errors.append(f"FeishuAdapter.{name} missing")
    if not getattr(base, "_CARD_ACTION_ROUTES", None):
        errors.append("FeishuAdapter._CARD_ACTION_ROUTES missing")
    return errors
