"""Feishu clarify presentation and real Hermes callback-chain tests."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from gateway.platforms.base import BasePlatformAdapter
from tools import clarify_gateway

from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.interactions.clarify import (
    ClarifyAdapterProxy,
    ClarifySendResult,
    _reconcile_clarify_lifecycle,
    handle_clarify_action,
    parse_card_action,
    send_clarify_card,
)
from hermes_lark_streaming.interactions.registry import (
    ClarifyCardRegistry,
    ClarifyCardState,
)
from hermes_lark_streaming.patch import on_clarify_adapter, on_feishu_interaction_action


def _raw_action(
    action: str,
    *,
    clarify_id: str = "clarify-1",
    response: str | None = None,
    option: str | None = None,
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
            action=SimpleNamespace(
                value=value,
                form_value=form_value,
                input_value=input_value,
                option=option,
            ),
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id, user_id=user_id, union_id=""),
        )
    )


def _state(
    *,
    clarify_id: str = "clarify-1",
    multi_select: bool = False,
    question: str = "Which path?",
    choices: tuple[str, ...] = ("A", "B"),
) -> ClarifyCardState:
    return ClarifyCardState(
        clarify_id=clarify_id,
        card_id="card-1",
        card_msg_id="msg-1",
        chat_id="chat-1",
        session_key="feishu:chat-1:owner",
        owner_user_ids=frozenset({"owner-open", "owner-user"}),
        question=question,
        choices=choices,
        multi_select=multi_select,
        delivered=True,
    )


@pytest.fixture(autouse=True)
def _clear_official_registry() -> None:
    clarify_gateway.clear_session("feishu:chat-1:owner")
    yield
    clarify_gateway.clear_session("feishu:chat-1:owner")


def _register_official(
    clarify_id: str = "clarify-1",
    *,
    multi_select: bool = False,
    question: str = "Which path?",
    choices: list[str] | None = None,
) -> None:
    clarify_gateway.register(
        clarify_id=clarify_id,
        session_key="feishu:chat-1:owner",
        question=question,
        choices=choices or ["A", "B"],
        multi_select=multi_select,
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
            raw_message=_raw_action("clarify_select", option="1"),
            source_chat_id="chat-1",
            session_key="feishu:chat-1:owner",
        )
        resumed_response = waiting.result(timeout=1)

    assert consumed is True
    assert resumed_response == "B"
    assert registry.get("clarify-1").status == "answered"  # type: ignore[union-attr]
    client.cardkit_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_select_index_maps_to_untruncated_canonical_choice() -> None:
    canonical = "Use **safe** mode with `--dry-run` and " + "complete details " * 12
    _register_official(choices=[canonical, "B"])
    registry = ClarifyCardRegistry()
    registry.register(_state(choices=(canonical, "B")))
    client = AsyncMock()

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_select", option="0"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )

    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) == canonical
    state = registry.get("clarify-1")
    assert state is not None and state.answer == canonical


@pytest.mark.asyncio
async def test_multi_select_form_maps_indexes_to_canonical_json() -> None:
    choices = ["staging **safe**", "production `blue`", "canary"]
    _register_official(multi_select=True, choices=choices)
    registry = ClarifyCardRegistry()
    registry.register(_state(multi_select=True, choices=tuple(choices)))
    client = AsyncMock()

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action(
            "clarify_multi_submit",
            form_value={"clarify_multi_select": ["0", {"value": "2"}]},
        ),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )

    assert json.loads(clarify_gateway.wait_for_response("clarify-1", timeout=0) or "") == [
        choices[0],
        choices[2],
    ]
    state = registry.get("clarify-1")
    assert state is not None and state.status == "answered"
    assert state.answer == f"{choices[0]}\n\n{choices[2]}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "form_value",
    [None, {}, {"clarify_multi_select": []}, {"clarify_multi_select": ["-1"]}, {"clarify_multi_select": ["9"]}],
)
async def test_multi_select_rejects_empty_malformed_and_noncanonical_payloads(
    form_value: dict[str, object] | None,
) -> None:
    _register_official(multi_select=True)
    registry = ClarifyCardRegistry()
    registry.register(_state(multi_select=True))
    client = AsyncMock()

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_multi_submit", form_value=form_value),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session(
        "feishu:chat-1:owner",
        include_choice_prompts=True,
    )
    assert pending is not None and not pending.event.is_set()
    assert registry.get("clarify-1").status == "pending"  # type: ignore[union-attr]
    client.cardkit_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_select_rejects_noncanonical_picker_value() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())
    client = AsyncMock()

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_select", option="99"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    pending = clarify_gateway.get_pending_for_session(
        "feishu:chat-1:owner",
        include_choice_prompts=True,
    )
    assert pending is not None and not pending.event.is_set()
    assert registry.get("clarify-1").status == "pending"  # type: ignore[union-attr]


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
async def test_other_submit_on_multi_select_returns_one_item_json_array() -> None:
    _register_official(multi_select=True)
    registry = ClarifyCardRegistry()
    registry.register(_state(multi_select=True))
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
        raw_message=_raw_action(
            "clarify_other_submit",
            form_value={"clarify_other_input": "custom answer"},
        ),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )

    assert json.loads(clarify_gateway.wait_for_response("clarify-1", timeout=0) or "") == [
        "custom answer"
    ]


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
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0.01) is None
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
async def test_back_restores_picker_without_touching_pending_wait() -> None:
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
    restored = client.cardkit_update.await_args_list[-1].args[1]["body"]["elements"]
    assert restored[1]["tag"] == "markdown"
    assert restored[2]["tag"] == "select_static"
    assert restored[2]["options"][0]["value"] == "0"


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
async def test_text_fallback_answer_becomes_resolved_card_even_after_official_cleanup() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    state = _state()
    state.status = "awaiting_text"
    registry.register(state)
    client = AsyncMock()
    official_entry = clarify_gateway.get_pending_for_session(
        "feishu:chat-1:owner",
        include_choice_prompts=True,
    )
    assert official_entry is not None
    assert clarify_gateway.resolve_gateway_clarify("clarify-1", "custom fallback")
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) == "custom fallback"

    assert await _reconcile_clarify_lifecycle(
        client=client,
        registry=registry,
        state=state,
        official_entry=official_entry,
    )
    assert state.status == "answered" and state.answer == "custom fallback"
    resolved = client.cardkit_update.await_args.args[1]
    assert "resolve_filled" in str(resolved)


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
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0.01) is None
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


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["timeout", "reset"])
async def test_timeout_and_reset_retire_card_without_resurrecting_it(termination: str) -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    state = _state()
    registry.register(state)
    client = AsyncMock()

    if termination == "timeout":
        assert clarify_gateway.wait_for_response("clarify-1", timeout=0.01) is None
    else:
        assert clarify_gateway.clear_session("feishu:chat-1:owner") == 1

    assert await _reconcile_clarify_lifecycle(client=client, registry=registry, state=state)
    assert state.status == "expired"
    retired = client.cardkit_update.await_args.args[1]
    assert all(item["tag"] not in {"button", "form", "select_static"} for item in retired["body"]["elements"])

    # A late callback after timeout/reset is consumed, but cannot change the
    # immutable terminal state or call the official resolver.
    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_select", option="0"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert state.status == "expired"


@pytest.mark.asyncio
async def test_terminal_update_retries_but_answered_state_remains_immutable() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    state = _state()
    registry.register(state)
    client = AsyncMock()
    client.cardkit_update.side_effect = [RuntimeError("transient"), None]

    assert await handle_clarify_action(
        client=client,
        registry=registry,
        raw_message=_raw_action("clarify_select", option="0"),
        source_chat_id="chat-1",
        session_key="feishu:chat-1:owner",
    )
    assert state.status == "answered" and state.answer == "A"
    assert client.cardkit_update.await_count == 2
    assert registry.finish("clarify-1", "expired") is False
    assert state.status == "answered" and state.answer == "A"


def test_typed_card_command_and_approval_namespace_are_not_intercepted() -> None:
    assert parse_card_action(None) == (None, "", frozenset())
    approval_raw = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"hermes_action": "approve_once", "approval_id": 7}),
        )
    )
    assert parse_card_action(approval_raw) == (None, "", frozenset())


def test_parse_card_action_accepts_runtime_wrappers_and_json_value() -> None:
    raw = _raw_action("clarify_other_back")
    value = raw.event.action.value
    wrapped = {
        "payload": {
            "data": {
                "event": {
                    "action": {
                        "value": json.dumps(value),
                        "name": "clarify_other_back",
                    },
                    "context": {"open_chat_id": "chat-1"},
                    "operator": {"open_id": "owner-open", "user_id": "owner-user"},
                }
            }
        }
    }
    parsed, chat_id, operators = parse_card_action(wrapped)
    assert parsed == value
    assert chat_id == "chat-1"
    assert operators == frozenset({"owner-open", "owner-user"})


def test_registry_finds_state_by_card_message() -> None:
    registry = ClarifyCardRegistry()
    state = _state()
    registry.register(state)
    assert registry.find_by_card_message("chat-1", "msg-1") is state
    assert registry.find_by_card_message("other-chat", "msg-1") is None


def test_clarify_adapter_uses_source_profile_controller() -> None:
    adapter = SimpleNamespace()
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), user_id="owner")
    controller = SimpleNamespace(clarify_card_enabled=True)
    gateway = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: "/profiles/assistant",
    )

    with patch("hermes_lark_streaming.patch.get_controller", return_value=controller) as get_ctrl:
        wrapped = on_clarify_adapter(adapter=adapter, source=source, gateway=gateway)

    get_ctrl.assert_called_once_with("/profiles/assistant")
    assert isinstance(wrapped, ClarifyAdapterProxy)


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
async def test_gateway_hook_recovers_plugin_action_from_synthetic_card_command() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    registry.register(_state())

    async def _handle(**kwargs: object) -> bool:
        return await handle_clarify_action(client=AsyncMock(), registry=registry, **kwargs)

    ctrl = SimpleNamespace(
        enabled=True,
        on_clarify_action=AsyncMock(side_effect=_handle),
    )
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat-1")
    raw = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={}, form_value={"clarify_other_input": "gray"}, input_value=None
            ),
            context=SimpleNamespace(open_chat_id="chat-1"),
            operator=SimpleNamespace(open_id="owner-open", user_id="owner-user", union_id=""),
        )
    )
    event = SimpleNamespace(
        raw_message=raw,
        text='/card button {"hermes_lark_action":"clarify_select","clarify_id":"clarify-1","response":"A"}',
    )
    with patch("hermes_lark_streaming.patch.get_controller", return_value=ctrl):
        assert await on_feishu_interaction_action(
            message_id="callback-token",
            source=source,
            event=event,
            session_key="feishu:chat-1:owner",
            gateway=SimpleNamespace(
                _resolve_profile_home_for_source=lambda _source: "/profiles/owner",
            ),
        )
    assert clarify_gateway.wait_for_response("clarify-1", timeout=0) == "A"


@pytest.mark.asyncio
async def test_gateway_hook_recovers_form_action_name_when_feishu_omits_value() -> None:
    _register_official()
    registry = ClarifyCardRegistry()
    state = _state()
    registry.register(state)

    async def _handle(**kwargs: object) -> bool:
        return await handle_clarify_action(client=AsyncMock(), registry=registry, **kwargs)

    ctrl = object.__new__(StreamCardController)
    ctrl._clarify_registry = registry
    ctrl.on_clarify_action = AsyncMock(side_effect=_handle)
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat-1")
    raw = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=None,
                name="clarify_other_back",
                form_value={"clarify_other_input": "ignored"},
                input_value=None,
            ),
            context=SimpleNamespace(open_chat_id="chat-1", open_message_id="msg-1"),
            operator=SimpleNamespace(open_id="owner-open", user_id="owner-user", union_id=""),
        )
    )
    event = SimpleNamespace(raw_message=raw, text="/card button")
    with patch("hermes_lark_streaming.patch.get_controller", return_value=ctrl):
        assert await on_feishu_interaction_action(
            message_id="callback-token",
            source=source,
            event=event,
            session_key="feishu:chat-1:owner",
            gateway=SimpleNamespace(
                _resolve_profile_home_for_source=lambda _source: "/profiles/owner",
            ),
        )
    assert registry.get("clarify-1").status == "pending"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_gateway_hook_recovers_native_multi_form_submit_without_value() -> None:
    _register_official(multi_select=True)
    registry = ClarifyCardRegistry()
    state = _state(multi_select=True)
    registry.register(state)

    async def _handle(**kwargs: object) -> bool:
        return await handle_clarify_action(client=AsyncMock(), registry=registry, **kwargs)

    ctrl = object.__new__(StreamCardController)
    ctrl._clarify_registry = registry
    ctrl.on_clarify_action = AsyncMock(side_effect=_handle)
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat-1")
    raw = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=None,
                name=None,
                tag="button",
                form_value={"clarify_multi_select": ["0", "1"]},
                input_value=None,
                option=None,
            ),
            context=SimpleNamespace(open_chat_id="chat-1", open_message_id="msg-1"),
            operator=SimpleNamespace(open_id="owner-open", user_id="owner-user", union_id=""),
        )
    )
    event = SimpleNamespace(raw_message=raw, text="/card button")
    with patch("hermes_lark_streaming.patch.get_controller", return_value=ctrl):
        assert await on_feishu_interaction_action(
            message_id="callback-token",
            source=source,
            event=event,
            session_key="feishu:chat-1:owner",
            gateway=SimpleNamespace(
                _resolve_profile_home_for_source=lambda _source: "/profiles/owner",
            ),
        )
    assert json.loads(clarify_gateway.wait_for_response("clarify-1", timeout=0) or "") == ["A", "B"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_chat_id", "card_message_id"),
    [("chat-1", "unknown-msg"), ("wrong-chat", "msg-1")],
    ids=["unknown-card-message", "wrong-chat"],
)
async def test_native_multi_form_shape_is_not_consumed_outside_registered_card_scope(
    callback_chat_id: str,
    card_message_id: str,
) -> None:
    registry = ClarifyCardRegistry()
    registry.register(_state(multi_select=True))
    ctrl = object.__new__(StreamCardController)
    ctrl._clarify_registry = registry
    ctrl.on_clarify_action = AsyncMock()
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id=callback_chat_id)
    raw = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=None,
                name=None,
                tag="button",
                form_value={"clarify_multi_select": ["0", "1"]},
                input_value=None,
                option=None,
            ),
            context=SimpleNamespace(
                open_chat_id=callback_chat_id,
                open_message_id=card_message_id,
            ),
            operator=SimpleNamespace(open_id="owner-open", user_id="owner-user", union_id=""),
        )
    )
    event = SimpleNamespace(raw_message=raw, text="/card button")
    with patch("hermes_lark_streaming.patch.get_controller", return_value=ctrl):
        assert not await on_feishu_interaction_action(
            message_id="callback-token",
            source=source,
            event=event,
            session_key="feishu:chat-1:owner",
            gateway=SimpleNamespace(
                _resolve_profile_home_for_source=lambda _source: "/profiles/owner",
            ),
        )
    ctrl.on_clarify_action.assert_not_awaited()


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
async def test_multi_select_uses_native_card_and_preserves_official_flag() -> None:
    _register_official(multi_select=True)

    class _OfficialTextFallback:
        send_clarify = BasePlatformAdapter.send_clarify

        def __init__(self) -> None:
            self.send = AsyncMock(return_value=SimpleNamespace(success=True))

    original = _OfficialTextFallback()
    controller = SimpleNamespace(
        send_clarify_card=AsyncMock(return_value=ClarifySendResult(True, message_id="msg-1"))
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
    controller.send_clarify_card.assert_awaited_once()
    assert controller.send_clarify_card.await_args.kwargs["multi_select"] is True
    original.send.assert_not_awaited()
    pending = clarify_gateway.get_pending_for_session(
        "feishu:chat-1:owner", include_choice_prompts=True
    )
    assert pending is not None and pending.awaiting_text is False


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
async def test_new_clarify_atomically_retires_older_card_in_same_session() -> None:
    registry = ClarifyCardRegistry()
    old = _state(clarify_id="clarify-old")
    registry.register(old)
    client = AsyncMock()
    client.cardkit_create.return_value = "card-new"
    client.send_card_id_to_chat.return_value = "msg-new"

    result = await send_clarify_card(
        client=client,
        registry=registry,
        chat_id="chat-1",
        question="New question?",
        choices=["yes", "no"],
        clarify_id="clarify-new",
        session_key="feishu:chat-1:owner",
        owner_user_ids=frozenset({"owner"}),
        metadata=None,
    )

    assert result.success is True
    assert old.status == "expired"
    assert registry.get("clarify-new").status == "pending"  # type: ignore[union-attr]
    retired = client.cardkit_update.await_args.args[1]
    assert "此问题已失效" in str(retired)


@pytest.mark.asyncio
async def test_concurrent_clarify_delivery_keeps_only_latest_generation_pending() -> None:
    registry = ClarifyCardRegistry()
    a_send_started = asyncio.Event()
    release_a_send = asyncio.Event()
    client_a = AsyncMock()
    client_a.cardkit_create.return_value = "card-a"

    async def send_a(*_args: object, **_kwargs: object) -> str:
        a_send_started.set()
        await release_a_send.wait()
        return "msg-a"

    client_a.send_card_id_to_chat.side_effect = send_a
    client_b = AsyncMock()
    client_b.cardkit_create.return_value = "card-b"
    client_b.send_card_id_to_chat.return_value = "msg-b"

    task_a = asyncio.create_task(
        send_clarify_card(
            client=client_a,
            registry=registry,
            chat_id="chat-1",
            question="Question A?",
            choices=["A1", "A2"],
            clarify_id="clarify-a",
            session_key="feishu:chat-1:owner",
            owner_user_ids=frozenset({"owner"}),
            metadata=None,
        )
    )
    await asyncio.wait_for(a_send_started.wait(), timeout=1)

    result_b = await send_clarify_card(
        client=client_b,
        registry=registry,
        chat_id="chat-1",
        question="Question B?",
        choices=["B1", "B2"],
        clarify_id="clarify-b",
        session_key="feishu:chat-1:owner",
        owner_user_ids=frozenset({"owner"}),
        metadata=None,
    )
    state_a = registry.get("clarify-a")
    state_b = registry.get("clarify-b")
    assert result_b.success is True
    assert state_a is not None and state_a.generation == 1 and state_a.status == "expired"
    assert state_b is not None and state_b.generation == 2 and state_b.status == "pending"
    client_b.cardkit_update.assert_not_awaited()

    release_a_send.set()
    result_a = await asyncio.wait_for(task_a, timeout=1)
    assert result_a.success is True
    assert state_a.status == "expired"
    assert state_b.status == "pending"
    retired_a = client_a.cardkit_update.await_args.args[1]
    assert "此问题已失效" in str(retired_a)

    assert registry.claim("clarify-a") is None
    assert registry.claim("clarify-b") is state_b
    registry.release("clarify-b")
    assert state_b.status == "pending"


@pytest.mark.asyncio
async def test_failed_latest_delivery_cannot_revive_blocked_older_generation() -> None:
    registry = ClarifyCardRegistry()
    a_send_started = asyncio.Event()
    release_a_send = asyncio.Event()
    client_a = AsyncMock()
    client_a.cardkit_create.return_value = "card-a"

    async def send_a(*_args: object, **_kwargs: object) -> str:
        a_send_started.set()
        await release_a_send.wait()
        return "msg-a"

    client_a.send_card_id_to_chat.side_effect = send_a
    task_a = asyncio.create_task(
        send_clarify_card(
            client=client_a,
            registry=registry,
            chat_id="chat-1",
            question="Question A?",
            choices=["A1", "A2"],
            clarify_id="clarify-a",
            session_key="feishu:chat-1:owner",
            owner_user_ids=frozenset({"owner"}),
            metadata=None,
        )
    )
    await asyncio.wait_for(a_send_started.wait(), timeout=1)

    client_b = AsyncMock()
    client_b.cardkit_create.return_value = "card-b"
    client_b.send_card_id_to_chat.side_effect = RuntimeError("B send failed")
    with pytest.raises(RuntimeError, match="B send failed"):
        await send_clarify_card(
            client=client_b,
            registry=registry,
            chat_id="chat-1",
            question="Question B?",
            choices=["B1", "B2"],
            clarify_id="clarify-b",
            session_key="feishu:chat-1:owner",
            owner_user_ids=frozenset({"owner"}),
            metadata=None,
        )

    state_a = registry.get("clarify-a")
    assert state_a is not None and state_a.generation == 1 and state_a.status == "expired"
    assert registry.get("clarify-b") is None

    release_a_send.set()
    await asyncio.wait_for(task_a, timeout=1)
    assert state_a.status == "expired"
    assert registry.claim("clarify-a") is None


@pytest.mark.asyncio
async def test_failed_newer_delivery_text_fallback_keeps_sent_card_retired() -> None:
    _register_official("clarify-a", question="Question A?", choices=["A1", "A2"])
    registry = ClarifyCardRegistry()
    client_a = AsyncMock()
    client_a.cardkit_create.return_value = "card-a"
    client_a.send_card_id_to_chat.return_value = "msg-a"
    await send_clarify_card(
        client=client_a,
        registry=registry,
        chat_id="chat-1",
        question="Question A?",
        choices=["A1", "A2"],
        clarify_id="clarify-a",
        session_key="feishu:chat-1:owner",
        owner_user_ids=frozenset({"owner"}),
        metadata=None,
    )
    state_a = registry.get("clarify-a")
    assert state_a is not None and state_a.status == "pending"
    _register_official("clarify-b", question="Question B?", choices=["B1", "B2"])

    client_b = AsyncMock()
    client_b.cardkit_create.return_value = "card-b"
    client_b.send_card_id_to_chat.side_effect = RuntimeError("B send failed")

    class _OfficialTextFallback:
        send_clarify = BasePlatformAdapter.send_clarify

        def __init__(self) -> None:
            self.send = AsyncMock(return_value=SimpleNamespace(success=True))

    async def send_b(**_kwargs: object) -> ClarifySendResult:
        return await send_clarify_card(
            client=client_b,
            registry=registry,
            chat_id="chat-1",
            question="Question B?",
            choices=["B1", "B2"],
            clarify_id="clarify-b",
            session_key="feishu:chat-1:owner",
            owner_user_ids=frozenset({"owner"}),
            metadata=None,
        )

    original = _OfficialTextFallback()
    proxy = ClarifyAdapterProxy(
        original,
        SimpleNamespace(clarify_card_enabled=True, send_clarify_card=send_b),
        frozenset({"owner"}),
    )
    result = await proxy.send_clarify(
        chat_id="chat-1",
        question="Question B?",
        choices=["B1", "B2"],
        clarify_id="clarify-b",
        session_key="feishu:chat-1:owner",
    )

    assert result.success is True
    original.send.assert_awaited_once()
    assert registry.get("clarify-b") is None
    assert state_a.status == "expired"
    assert registry.claim("clarify-a") is None
    retired_a = client_b.cardkit_update.await_args.args[1]
    assert "此问题已失效" in str(retired_a)


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
