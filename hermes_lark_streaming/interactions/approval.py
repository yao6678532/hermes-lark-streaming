"""Feishu approval presentation backed by Hermes' official resolver."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..cardkit.interaction_builder import build_approval_card
from .registry import ApprovalCardRegistry, ApprovalCardState

_logger = logging.getLogger("hermes_lark_streaming")


@dataclass(slots=True)
class ApprovalActionResult:
    """A non-None result tells the adapter callback that the action was consumed."""

    card: dict[str, Any] | None = None


def _official_buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Read approval buttons from Hermes' card without assuming a fixed count."""
    found: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            payload = value.get("value")
            if (
                value.get("tag") == "button"
                and isinstance(payload, dict)
                and payload.get("hermes_action")
                and payload.get("approval_id") is not None
            ):
                found.append(value)
                return
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(card)
    return found


def transform_approval_card(
    *,
    registry: ApprovalCardRegistry,
    adapter: Any,
    card: dict[str, Any],
    chat_id: str,
    command: str,
    session_key: str,
    description: str,
) -> dict[str, Any]:
    """Transform only the UI and register presentation metadata."""
    buttons = _official_buttons(card)
    if not buttons:
        return card
    payloads = [button["value"] for button in buttons]
    approval_ids = {payload.get("approval_id") for payload in payloads}
    if len(approval_ids) != 1:
        raise ValueError("Hermes approval card has inconsistent approval ids")
    approval_id = next(iter(approval_ids))
    actions = frozenset(str(payload.get("hermes_action") or "") for payload in payloads)
    if not all(actions):
        raise ValueError("Hermes approval card contains an empty action")

    transformed = build_approval_card(
        command=command,
        description=description,
        buttons=buttons,
    )
    # Guard the most security-sensitive invariant: presentation changes may
    # never rewrite callback values or collapse scope decisions.
    transformed_buttons = _official_buttons(transformed)
    if [button["value"] for button in transformed_buttons] != payloads:
        raise ValueError("approval transformation changed Hermes callback payloads")

    registry.register(
        ApprovalCardState(
            adapter_key=id(adapter),
            approval_id=approval_id,
            chat_id=str(chat_id),
            session_key=str(session_key),
            command=str(command),
            description=str(description),
            actions=actions,
            buttons=tuple(buttons),
        )
    )
    return transformed


def _terminal_card(state: ApprovalCardState) -> dict[str, Any]:
    status = state.status
    if status == "resolving":
        status = "expired"
    return build_approval_card(
        command=state.command,
        description=state.description,
        buttons=[],
        status=status if status in {"approved", "denied", "expired"} else "expired",
        decision=state.decision,
    )


def handle_approval_action(
    *,
    registry: ApprovalCardRegistry,
    adapter: Any,
    approval_id: Any,
    action_value: dict[str, Any],
    choice: str,
    open_id: str,
    callback_chat_id: str,
) -> ApprovalActionResult | None:
    """Resolve one real Feishu action through Hermes' official approval API.

    The adapter calls this only after its native approval-id, group policy,
    and chat checks. Returning ``None`` means the card is not plugin-owned and
    the untouched Hermes callback path must continue.
    """
    adapter_key = id(adapter)
    state = registry.get(adapter_key, approval_id)
    if state is None:
        return None

    action = str(action_value.get("hermes_action") or "")
    if action not in state.actions:
        _logger.warning("rejecting non-canonical Feishu approval action id=%s", approval_id)
        return ApprovalActionResult()
    if callback_chat_id and callback_chat_id != state.chat_id:
        _logger.warning("rejecting Feishu approval action with wrong chat id=%s", approval_id)
        return ApprovalActionResult()

    # This is the same authorization predicate Hermes' native async resolver
    # uses after its group-policy check. Keep that check rather than inventing
    # a stricter or weaker owner model in the plugin.
    authorize = getattr(adapter, "_is_interactive_operator_authorized", None)
    if not callable(authorize) or not authorize(open_id):
        _logger.warning("rejecting Feishu approval action from wrong user id=%s", approval_id)
        return ApprovalActionResult()

    if state.status in {"approved", "denied", "expired"}:
        return ApprovalActionResult(card=_terminal_card(state))

    claimed = registry.claim(adapter_key, approval_id)
    if claimed is None:
        _logger.info("ignoring duplicate or out-of-order Feishu approval id=%s", approval_id)
        return ApprovalActionResult()

    try:
        from tools.approval import has_blocking_approval, resolve_gateway_approval  # type: ignore[import-not-found]

        native_state = getattr(adapter, "_approval_state", {}).get(approval_id)
        if (
            not isinstance(native_state, dict)
            or str(native_state.get("session_key") or "") != state.session_key
            or str(native_state.get("chat_id") or "") != state.chat_id
            or not has_blocking_approval(state.session_key)
        ):
            getattr(adapter, "_approval_state", {}).pop(approval_id, None)
            registry.finish(adapter_key, approval_id, "expired")
            return ApprovalActionResult(card=_terminal_card(state))

        # This is Hermes' canonical FIFO resolver. It sets the original
        # _ApprovalEntry Event; the blocked tool thread then applies session /
        # permanent scope exactly as the backend defines it.
        count = resolve_gateway_approval(state.session_key, choice)
    except Exception:
        registry.release(adapter_key, approval_id)
        raise

    getattr(adapter, "_approval_state", {}).pop(approval_id, None)
    if not count:
        registry.finish(adapter_key, approval_id, "expired")
    elif choice == "deny":
        registry.finish(adapter_key, approval_id, "denied", decision=choice)
    else:
        registry.finish(adapter_key, approval_id, "approved", decision=choice)

    try:
        card = _terminal_card(state)
    except Exception:
        # The security decision has already been delivered to Hermes. A UI
        # update failure must never replay or block the approval backend.
        _logger.exception("failed to build terminal Feishu approval card id=%s", approval_id)
        card = None
    return ApprovalActionResult(card=card)
