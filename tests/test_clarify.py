"""Feishu clarify presentation and real Hermes callback-chain tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from gateway.platforms.base import BasePlatformAdapter
from tools import clarify_gateway

from hermes_lark_streaming.interactions.clarify import (
    ClarifyAdapterProxy,
    ClarifySendResult,
    handle_clarify_action,
    parse_card_action,
    send_clarify_card,
)
from hermes_lark_streaming.interactions.registry import (
    ClarifyCardRegistry,
    ClarifyCardState,
)
from hermes_lark_streaming.patch import on_feishu_interaction_action


def _raw_action(
    action: str,
    *,
    clarify_id: str = "clarify-1",
    response: str | None = None,
    chat_id: str = "chat-1",
    open_id: str = "owner-open",
    user_id: str = "owner-user",
    form_value: dict[str, object] | None = None,
    input_value: str | None = None,
) -> SimpleNamespace:
    value = {"hermes_lark_action": action, "clarify_id": clarify_id}
    if response is not None:
        value["response"] = response
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value=value, form_value=form_value, input_value=input_value),
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id, user_id=user_id, union_id=""),
        )
    )


def _state(*, clarify_id: str = "clarify-1") -> ClarifyCardState:
    return ClarifyCardState(
        clarify_id=clarify_id,
        card_id="card-1",
        card_msg_id="msg-1",
        chat_id="chat-1",
        session_key="feishu:chat-1:owner",
        owner_user_ids=frozenset({"owner-open", "owner-user"}),
        question="Which path?",
        choices=("A", "B"),
    )


@pytest.fixture(autouse=True)
def _clear_official_registry() -> None:
    clarify_gateway.clear_session("feishu:chat-1:owner")
    yield
    clarify_gateway.clear_session("feishu:chat-1:owner")


def _register_official(clarify_id: str = "clarify-1") -> None:
    clarify_gateway.register(
        clarify_id=clarify_id,
        session_key="feishu:chat-1:owner",
        question="Which path?",
        choices=["A", "B"],
    )


@pytest.mark.asyncio
async def test_single_select_resolves_real_hermes_wait() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(clarify_gateway.wait_for_response, "clarify-1", 2)
        consumed = await handle_clarify_action(
            client=client,
            registry=registry,
            raw_message=_raw_action("clarify_select", response="B"),
            source_chat_id="chat-1",
            session_key="feishu:chat-1:owner",
        )
        resumed_response = waiting.result(timeout=1)

    assert consumed is True
    assert resumed_response == "B"
    assert registry.get("clarify-1").status == "answered"  # type: ignore[union-attr]
    client.cardkit_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_switches_same_card_to_input_without_mutating_hermes_wait() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session("feishu:chat-1:owner", include_choice_prompts=True)
    assert pending is not None and pending.awaiting_text is False and not pending.event.is_set()
    assert registry.get("clarify-1").status == "input"  # type: ignore[union-attr]
    updated = client.cardkit_update.await_args.args[1]
    assert updated["body"]["elements"][1]["tag"] == "form"


@pytest.mark.asyncio
async def test_other_submit_resolves_hermes_wait_from_real_form_value() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action(
            "clarify_other_submit",
            form_value={"clarify_other_input": "custom answer"},
        ),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) == "custom answer"
    assert registry.get("clarify-1").status == "answered"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_empty_form_submit_does_not_resolve() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other_submit", form_value={"clarify_other_input": "  "}),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session("feishu:chat-1:owner", include_choice_prompts=True)
    assert pending is not None and not pending.event.is_set()
    assert registry.get("clarify-1").status == "input"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_other_submit_wrong_identity_and_double_submit_are_safe() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    wrong = _raw_action(
        "clarify_other_submit", form_value={"clarify_other_input": "secret"}, open_id="wrong", user_id="wrong"
    )
    assert await handle_clarify_action(
        client=client, registry=registry, raw_message=wrong, source_chat_id="chat-1", session_key="feishu:chat-1:owner"
    )
    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other_submit", form_value={"clarify_other_input": "secret"}),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other_submit", form_value={"clarify_other_input": "second"}),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) == "secret"


@pytest.mark.asyncio
async def test_other_submit_wrong_chat_does_not_resolve() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    wrong = _raw_action(
        "clarify_other_submit",
        form_value={"clarify_other_input": "secret"},
        chat_id="wrong-chat",
    )
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=wrong,
        source_chat_id="wrong-chat",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session(
        "feishu:chat-1:owner",
        include_choice_prompts=True,
    )
    assert pending is not None and not pending.event.is_set()
    assert registry.get("clarify-1").status == "input"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_expired_other_submit_is_safe() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) is None
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action(
            "clarify_other_submit",
            form_value={"clarify_other_input": "too late"},
        ),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert registry.get("clarify-1").status == "expired"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_back_restores_buttons_without_touching_pending_wait() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other_back", form_value={"clarify_other_input": "ignored"}),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session("feishu:chat-1:owner", include_choice_prompts=True)
    assert pending is not None and not pending.event.is_set()
    assert registry.get("clarify-1").status == "pending"  # type: ignore[union-attr]
    assert client.cardkit_update.await_args_list[-1].args[1]["body"]["elements"][1]["tag"] == "button"


@pytest.mark.asyncio
async def test_form_callback_malformed_falls_back_to_awaiting_text() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other_submit"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session("feishu:chat-1:owner")
    assert pending is not None and pending.awaiting_text is True
    assert registry.get("clarify-1").status == "awaiting_text"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_input_card_update_failure_preserves_text_fallback() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()
    client.cardkit_update.side_effect = [RuntimeError("unsupported form"), RuntimeError("still unavailable")]
    await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_other"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session("feishu:chat-1:owner")
    assert pending is not None and pending.awaiting_text is True
    assert registry.get("clarify-1").status == "awaiting_text"  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "source_chat", "session_key"),
    [
        (
            _raw_action("clarify_select", response="A", open_id="wrong", user_id="wrong"),
            "chat-1",
            "feishu:chat-1:owner",
        ),
        (_raw_action("clarify_select", response="A", chat_id="wrong-chat"), "wrong-chat", "feishu:chat-1:owner"),
    ],
    ids=["wrong-user", "wrong-chat"],
)
async def test_wrong_identity_scope_does_not_resolve(
    raw: SimpleNamespace,
    source_chat: str,
    session_key: str,
) -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=raw,
        source_chat_id=source_chat,
        session_key=session_key,
    )
    pending = clarify_gateway.get_pending_for_session(
        "feishu:chat-1:owner",
        include_choice_prompts=True,
    )
    assert pending is not None and not pending.event.is_set()
    assert registry.get("clarify-1").status == "pending"  # type: ignore[union-attr]
    client.cardkit_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_click_and_conflicting_click_are_idempotent() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()

    kwargs = {
        "client": client,
        "registry": registry,
        "source_chat_id": "chat-1",
        "session_key": "feishu:chat-1:owner",
    }
    assert await handle_clarify_action(
        raw_message=_raw_action("clarify_select", response="A"),
        **kwargs,
    )
    assert await handle_clarify_action(
        raw_message=_raw_action("clarify_select", response="B"),
        **kwargs,
    )

    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) == "A"
    state = registry.get("clarify-1")
    assert state is not None and state.status == "answered" and state.answer == "A"
    client.cardkit_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_click_is_consumed_without_new_turn() -> None:
    _register_official()
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) is None
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_select", response="A"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert registry.get("clarify-1").status == "expired"  # type: ignore[union-attr]
    client.cardkit_update.assert_awaited_once()


def test_typed_card_command_and_approval_namespace_are_not_intercepted() -> None:
    assert parse_card_action(None) == (None, "", frozenset())
    approval_raw = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"hermes_action": "approve_once", "approval_id": 7}),
        )
    )
    assert parse_card_action(approval_raw) == (None, "", frozenset())


@pytest.mark.asyncio
async def test_gateway_hook_leaves_approval_action_on_hermes_path() -> None:
    approval_raw = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"hermes_action": "approve_once", "approval_id": 7}),
        )
    )
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat-1")
    event = SimpleNamespace(raw_message=approval_raw)

    with patch("hermes_lark_streaming.patch.get_controller") as get_ctrl:
        consumed = await on_feishu_interaction_action(
            message_id="approval-token",
            source=source,
            event=event,
            session_key="feishu:chat-1:owner",
            gateway=SimpleNamespace(),
        )

    assert consumed is False
    get_ctrl.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_hook_consumes_plugin_action_even_if_bridge_fails() -> None:
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat-1")
    event = SimpleNamespace(raw_message=_raw_action("clarify_select", response="A"))

    with patch(
        "hermes_lark_streaming.patch.get_controller",
        side_effect=RuntimeError("controller unavailable"),
    ):
        consumed = await on_feishu_interaction_action(
            message_id="callback-token",
            source=source,
            event=event,
            session_key="feishu:chat-1:owner",
            gateway=SimpleNamespace(
                _resolve_profile_home_for_source=lambda _source: "/profiles/owner",
            ),
        )

    assert consumed is True


@pytest.mark.asyncio
async def test_gateway_hook_consumes_real_callback_chain_only() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())

    async def _handle(**kwargs: object) -> bool:
        return await handle_clarify_action(
            client=AsyncMock(),
            registry=registry,
            **kwargs,
        )

    ctrl = SimpleNamespace(enabled=True, on_clarify_action=AsyncMock(side_effect=_handle))
    source = SimpleNamespace(
        platform=SimpleNamespace(value="feishu"),
        chat_id="chat-1",
    )
    event = SimpleNamespace(raw_message=_raw_action("clarify_select", response="A"))
    gateway = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: "/profiles/owner",
    )

    with patch("hermes_lark_streaming.patch.get_controller", return_value=ctrl) as get_ctrl:
        assert await on_feishu_interaction_action(
            message_id="callback-token",
            source=source,
            event=event,
            session_key="feishu:chat-1:open-id",
            gateway=gateway,
        )

    get_ctrl.assert_called_once_with("/profiles/owner")
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) == "A"


@pytest.mark.asyncio
async def test_card_send_failure_delegates_to_official_numbered_text_fallback() -> None:
    _register_official()

    class _OfficialTextFallback:
        send_clarify = BasePlatformAdapter.send_clarify

        def __init__(self) -> None:
            self.send = AsyncMock(return_value=SimpleNamespace(success=True))

    original = _OfficialTextFallback()
    controller = SimpleNamespace(
        send_clarify_card=AsyncMock(return_value=ClarifySendResult(False, error="send failed"))
    )
    proxy = ClarifyAdapterProxy(original, controller, frozenset({"owner"}))

    result = await proxy.send_clarify(
        chat_id="chat-1",
        question="Which path?",
        choices=["A", "B"],
        clarify_id="clarify-1",
        session_key="feishu:chat-1:owner",
    )

    assert result.success is True
    sent_text = original.send.await_args.kwargs["content"]
    assert "1. A" in sent_text and "2. B" in sent_text
    pending = clarify_gateway.get_pending_for_session("feishu:chat-1:owner")
    assert pending is not None and pending.awaiting_text is True


@pytest.mark.asyncio
async def test_proxy_preserves_approval_action_capability_and_namespace() -> None:
    original = SimpleNamespace(
        send_exec_approval=AsyncMock(return_value=SimpleNamespace(success=True)),
    )
    proxy = ClarifyAdapterProxy(original, SimpleNamespace(), frozenset({"owner"}))

    assert getattr(type(proxy), "send_exec_approval", None) is not None
    result = await proxy.send_exec_approval(
        chat_id="chat-1",
        command="echo safe",
        session_key="session-1",
    )

    assert result.success is True
    original.send_exec_approval.assert_awaited_once_with(
        chat_id="chat-1",
        command="echo safe",
        session_key="session-1",
    )


@pytest.mark.asyncio
async def test_card_send_success_suppresses_text_fallback_and_registers() -> None:
    registry = ClarifyCardRegistry()
    client = AsyncMock()
    client.cardkit_create.return_value = "card-1"
    client.send_card_id_to_chat.return_value = "msg-1"

    result = await send_clarify_card(
        client=client,
        registry=registry,
        chat_id="chat-1",
        question="Which path?",
        choices=["A", "B"],
        clarify_id="clarify-1",
        session_key="session-1",
        owner_user_ids=frozenset({"owner"}),
        metadata={"reply_to_message_id": "anchor-1"},
    )

    assert result.success is True
    state = registry.get("clarify-1")
    assert state is not None and state.card_id == "card-1" and state.card_msg_id == "msg-1"
    client.send_card_id_to_chat.assert_awaited_once_with(
        "chat-1",
        "card-1",
        reply_to_message_id="anchor-1",
    )


@pytest.mark.asyncio
async def test_failed_card_delivery_leaves_no_presentation_state() -> None:
    registry = ClarifyCardRegistry()
    client = AsyncMock()
    client.cardkit_create.return_value = "card-1"
    client.send_card_id_to_chat.side_effect = RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        await send_clarify_card(
            client=client,
            registry=registry,
            chat_id="chat-1",
            question="Which path?",
            choices=["A", "B"],
            clarify_id="clarify-1",
            session_key="session-1",
            owner_user_ids=frozenset({"owner"}),
            metadata=None,
        )

    assert registry.get("clarify-1") is None
