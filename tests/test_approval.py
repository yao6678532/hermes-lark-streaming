"""Approval presentation and Hermes resolver bridge tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_lark_streaming.interactions.approval import (
    _official_buttons,
    handle_approval_action,
    transform_approval_card,
)
from hermes_lark_streaming.interactions.registry import ApprovalCardRegistry


def _official_card(approval_id: int = 9, actions: tuple[str, ...] | None = None) -> dict:
    action_names = actions or ("approve_once", "approve_session", "approve_always", "deny")
    buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": action},
            "type": "default",
            "value": {
                "hermes_action": action,
                "approval_id": approval_id,
                "future_field": f"keep-{action}",
            },
        }
        for action in action_names
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "official"}},
        "elements": [{"tag": "action", "actions": buttons}],
    }


def _registered(
    *,
    registry: ApprovalCardRegistry | None = None,
    adapter: SimpleNamespace | None = None,
    approval_id: int = 9,
    actions: tuple[str, ...] | None = None,
    session_key: str = "feishu:chat:user",
    chat_id: str = "chat",
) -> tuple[ApprovalCardRegistry, SimpleNamespace, dict]:
    registry = registry or ApprovalCardRegistry()
    adapter = adapter or SimpleNamespace()
    adapter._approval_state = {
        approval_id: {"session_key": session_key, "message_id": "om_1", "chat_id": chat_id}
    }
    adapter._is_interactive_operator_authorized = lambda open_id: open_id == "owner"
    transformed = transform_approval_card(
        registry=registry,
        adapter=adapter,
        card=_official_card(approval_id, actions),
        chat_id=chat_id,
        command="rm -rf ./build",
        session_key=session_key,
        description="remove build outputs",
    )
    return registry, adapter, transformed


def test_transform_uses_dynamic_official_choices_and_preserves_payloads() -> None:
    original = _official_card(actions=("approve_once", "approve_future_scope", "deny"))
    registry = ApprovalCardRegistry()
    adapter = SimpleNamespace()

    transformed = transform_approval_card(
        registry=registry,
        adapter=adapter,
        card=original,
        chat_id="chat",
        command="cmd",
        session_key="session",
        description="reason",
    )

    assert [item["value"] for item in _official_buttons(transformed)] == [
        item["value"] for item in _official_buttons(original)
    ]
    state = registry.get(id(adapter), 9)
    assert state is not None
    assert state.actions == frozenset({"approve_once", "approve_future_scope", "deny"})


@pytest.mark.parametrize(
    ("action", "choice", "status"),
    [
        ("approve_once", "once", "approved"),
        ("approve_session", "session", "approved"),
        ("approve_always", "always", "approved"),
        ("deny", "deny", "denied"),
    ],
)
def test_each_v020_decision_uses_official_resolver(action: str, choice: str, status: str) -> None:
    registry, adapter, _card = _registered()

    with (
        patch("tools.approval.has_blocking_approval", return_value=True),
        patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve,
    ):
        result = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=9,
            action_value={"hermes_action": action, "approval_id": 9},
            choice=choice,
            open_id="owner",
            callback_chat_id="chat",
        )

    assert result is not None and result.card is not None
    resolve.assert_called_once_with("feishu:chat:user", choice)
    state = registry.get(id(adapter), 9)
    assert state is not None and state.status == status and state.decision == choice
    assert 9 not in adapter._approval_state
    assert all(item.get("tag") != "button" for item in result.card["body"]["elements"])


def test_wrong_user_chat_and_unknown_action_do_not_resolve() -> None:
    registry, adapter, _card = _registered()
    resolver = MagicMock()
    cases = [
        ("intruder", "chat", "approve_once"),
        ("owner", "other-chat", "approve_once"),
        ("owner", "chat", "approve_invented"),
    ]
    with patch("tools.approval.resolve_gateway_approval", resolver):
        for open_id, chat_id, action in cases:
            result = handle_approval_action(
                registry=registry,
                adapter=adapter,
                approval_id=9,
                action_value={"hermes_action": action, "approval_id": 9},
                choice="once",
                open_id=open_id,
                callback_chat_id=chat_id,
            )
            assert result is not None
    resolver.assert_not_called()
    assert registry.get(id(adapter), 9).status == "pending"  # type: ignore[union-attr]


def test_duplicate_click_resolves_only_once() -> None:
    registry, adapter, _card = _registered()
    with (
        patch("tools.approval.has_blocking_approval", return_value=True),
        patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve,
    ):
        first = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=9,
            action_value={"hermes_action": "approve_once", "approval_id": 9},
            choice="once",
            open_id="owner",
            callback_chat_id="chat",
        )
        second = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=9,
            action_value={"hermes_action": "approve_once", "approval_id": 9},
            choice="once",
            open_id="owner",
            callback_chat_id="chat",
        )

    assert first is not None and second is not None
    assert resolve.call_count == 1
    assert second.card is not None


def test_expired_pending_is_not_approved() -> None:
    registry, adapter, _card = _registered()
    with (
        patch("tools.approval.has_blocking_approval", return_value=False),
        patch("tools.approval.resolve_gateway_approval") as resolve,
    ):
        result = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=9,
            action_value={"hermes_action": "approve_once", "approval_id": 9},
            choice="once",
            open_id="owner",
            callback_chat_id="chat",
        )

    resolve.assert_not_called()
    assert result is not None and result.card is not None
    assert result.card["header"]["template"] == "grey"
    assert registry.get(id(adapter), 9).status == "expired"  # type: ignore[union-attr]


def test_failed_send_ghost_does_not_block_later_real_approval() -> None:
    registry = ApprovalCardRegistry()
    adapter = SimpleNamespace()
    adapter._approval_state = {}
    adapter._is_interactive_operator_authorized = lambda open_id: open_id == "owner"

    transform_approval_card(
        registry=registry,
        adapter=adapter,
        card=_official_card(1),
        chat_id="chat",
        command="ghost-a",
        session_key="session",
        description="failed send",
    )
    adapter._approval_state[2] = {"session_key": "session", "message_id": "om-2", "chat_id": "chat"}
    transform_approval_card(
        registry=registry,
        adapter=adapter,
        card=_official_card(2),
        chat_id="chat",
        command="real-b",
        session_key="session",
        description="successful send",
    )

    with (
        patch("tools.approval.has_blocking_approval", return_value=True),
        patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve,
    ):
        result = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=2,
            action_value={"hermes_action": "approve_once", "approval_id": 2},
            choice="once",
            open_id="owner",
            callback_chat_id="chat",
        )

    resolve.assert_called_once_with("session", "once")
    assert result is not None and registry.get(id(adapter), 1).status == "expired"  # type: ignore[union-attr]
    assert registry.get(id(adapter), 2).status == "approved"  # type: ignore[union-attr]


def test_native_state_mismatch_is_expired_before_fifo_claim() -> None:
    registry, adapter, _card = _registered()
    adapter._approval_state[9] = {"session_key": "other-session", "message_id": "om-1", "chat_id": "chat"}
    with patch("tools.approval.resolve_gateway_approval") as resolve:
        result = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=9,
            action_value={"hermes_action": "approve_once", "approval_id": 9},
            choice="once",
            open_id="owner",
            callback_chat_id="chat",
        )
    resolve.assert_not_called()
    assert result is not None and result.card is not None
    assert registry.get(id(adapter), 9).status == "expired"  # type: ignore[union-attr]


def test_terminal_card_failure_does_not_replay_or_block_resolver() -> None:
    registry, adapter, _card = _registered()
    with (
        patch("tools.approval.has_blocking_approval", return_value=True),
        patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve,
        patch("hermes_lark_streaming.interactions.approval._terminal_card", side_effect=RuntimeError("bad card")),
    ):
        result = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=9,
            action_value={"hermes_action": "approve_always", "approval_id": 9},
            choice="always",
            open_id="owner",
            callback_chat_id="chat",
        )

    resolve.assert_called_once_with("feishu:chat:user", "always")
    assert result is not None and result.card is None
    assert registry.get(id(adapter), 9).status == "approved"  # type: ignore[union-attr]


def test_out_of_order_card_cannot_resolve_fifo_head() -> None:
    registry = ApprovalCardRegistry()
    adapter = SimpleNamespace()
    _registered(registry=registry, adapter=adapter, approval_id=1)
    _registered(registry=registry, adapter=adapter, approval_id=2)
    adapter._approval_state[1] = {
        "session_key": "feishu:chat:user",
        "message_id": "om_1",
        "chat_id": "chat",
    }
    with patch("tools.approval.resolve_gateway_approval") as resolve:
        result = handle_approval_action(
            registry=registry,
            adapter=adapter,
            approval_id=2,
            action_value={"hermes_action": "deny", "approval_id": 2},
            choice="deny",
            open_id="owner",
            callback_chat_id="chat",
        )
    assert result is not None and result.card is None
    resolve.assert_not_called()
