"""AST Patch 注入的 Hook 函数.

这些函数从 gateway/run.py 的注入点被调用.
它们只做一件事：检查配置 → 调用 controller.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any

from .controller import get_controller
from .interactions.clarify import (
    ClarifyAdapterProxy,
    _callback_event,
    _field,
    parse_card_action,
    raw_message_with_action_value,
)

_logger = logging.getLogger("hermes_lark_streaming")


def _safe_hook(
    default_return: Any = None,
    log_level: str = "warning",
) -> Callable:
    """统一处理 enabled 检查和异常捕获."""

    def decorator(func: Callable) -> Callable:
        if iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*, message_id: str, **kwargs: Any) -> Any:
                try:
                    ctrl = get_controller()
                    if not ctrl.enabled:
                        return default_return
                    return await func(ctrl=ctrl, message_id=message_id, **kwargs)
                except Exception as exc:
                    getattr(_logger, log_level)("%s error: %s", func.__name__, exc, exc_info=True)
                    return default_return

            return async_wrapper

        @wraps(func)
        def wrapper(*, message_id: str, **kwargs: Any) -> Any:
            try:
                ctrl = get_controller()
                if not ctrl.enabled:
                    return default_return
                return func(ctrl=ctrl, message_id=message_id, **kwargs)
            except Exception as exc:
                getattr(_logger, log_level)("%s error: %s", func.__name__, exc, exc_info=True)
                return default_return

        return wrapper

    return decorator


def on_feishu_normalize(
    *,
    message_id: str,
    source: Any,
    event: Any,
    reply_anchor_id: str | None = None,
) -> None:
    """[注入点 0] _handle_message source 赋值后 — 修正飞书引用消息的虚假 thread_id."""
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return

        platform = getattr(getattr(source, "platform", None), "value", "")
        if platform != "feishu":
            return

        raw = getattr(event, "raw_message", None)
        raw_event = raw.get("event") if isinstance(raw, dict) else None
        if raw_event is None:
            raw_event = getattr(raw, "event", None)

        raw_message = None
        if isinstance(raw_event, dict):
            raw_message = raw_event.get("message")
        elif raw_event is not None:
            raw_message = getattr(raw_event, "message", None)
        if raw_message is None and isinstance(raw, dict):
            raw_message = raw.get("message")
        if raw_message is None:
            raw_message = raw

        real_thread_id = None
        if isinstance(raw_message, dict):
            real_thread_id = raw_message.get("thread_id")
        else:
            real_thread_id = getattr(raw_message, "thread_id", None)

        reply_to = getattr(event, "reply_to_message_id", None)
        source_thread_id = getattr(source, "thread_id", None)

        _logger.info(
            "feishu inbound ids: msg=%s anchor=%s source_thread=%s raw_thread=%s reply_to=%s",
            message_id,
            reply_anchor_id,
            source_thread_id,
            real_thread_id,
            reply_to,
        )

        if reply_to and source_thread_id and not real_thread_id:
            source.thread_id = None
            event.source = source
    except Exception as exc:
        _logger.warning("on_feishu_normalize error: %s", exc, exc_info=True)


def on_clarify_adapter(*, adapter: Any, source: Any, gateway: Any = None) -> Any:
    """Wrap Feishu clarify delivery while leaving every other adapter API intact."""
    try:
        profile_home = None
        if gateway is not None:
            try:
                profile_home = gateway._resolve_profile_home_for_source(source)
            except Exception:
                _logger.debug("failed to resolve Feishu clarify profile home", exc_info=True)
        ctrl = get_controller(profile_home)
        platform = getattr(getattr(source, "platform", None), "value", "")
        if not ctrl.clarify_card_enabled or platform not in {"feishu", "lark"}:
            return adapter
        if adapter is None or isinstance(adapter, ClarifyAdapterProxy):
            return adapter
        owner_ids = frozenset(
            str(value).strip()
            for value in (
                getattr(source, "user_id", None),
                getattr(source, "user_id_alt", None),
            )
            if value and str(value).strip()
        )
        if not owner_ids:
            return adapter
        return ClarifyAdapterProxy(adapter, ctrl, owner_ids)
    except Exception:
        _logger.exception("on_clarify_adapter error")
        return adapter


async def on_feishu_interaction_action(
    *,
    message_id: str,
    source: Any,
    event: Any,
    session_key: str,
    gateway: Any = None,
) -> bool:
    """Consume genuine plugin card callbacks before normal command dispatch."""
    del message_id
    platform = getattr(getattr(source, "platform", None), "value", "")
    if platform not in {"feishu", "lark"}:
        return False
    raw_message = getattr(event, "raw_message", None)
    callback_event = _callback_event(raw_message)
    callback_action = _field(callback_event, "action")
    callback_context = _field(callback_event, "context")
    callback_chat_id = str(_field(callback_context, "open_chat_id", "") or "").strip()
    callback_message_id = str(_field(callback_context, "open_message_id", "") or "").strip()
    callback_action_name = str(_field(callback_action, "name", "") or "").strip()
    action_value, _chat_id, _operator_ids = parse_card_action(raw_message)
    if action_value is None and callback_action_name in {
        "clarify_other_submit",
        "clarify_other_back",
    } and callback_chat_id and callback_message_id:
        try:
            profile_home = gateway._resolve_profile_home_for_source(source) if gateway is not None else None
            ctrl = get_controller(profile_home)
            clarify_id = ctrl.clarify_id_for_card_message(
                chat_id=callback_chat_id,
                card_msg_id=callback_message_id,
            )
        except Exception:
            clarify_id = ""
        if clarify_id:
            decoded = {
                "hermes_lark_action": callback_action_name,
                "clarify_id": clarify_id,
            }
            action_value = decoded
            raw_message = raw_message_with_action_value(raw_message, decoded)
    if action_value is None:
        # Hermes' Feishu adapter serializes unrecognized callbacks into a
        # synthetic ``/card button <json>`` command.  Recover only our
        # explicit plugin namespace from that command; preserve the original
        # callback's form_value/input_value and identity fields.
        synthetic_text = str(getattr(event, "text", "") or "").strip()
        if synthetic_text.startswith("/card "):
            payload = synthetic_text.split(" ", 2)
            if len(payload) == 3:
                try:
                    synthetic_decoded = json.loads(payload[2])
                except (TypeError, ValueError):
                    synthetic_decoded = None
                if isinstance(synthetic_decoded, dict) and "hermes_lark_action" in synthetic_decoded:
                    action_value = synthetic_decoded
                    raw_message = raw_message_with_action_value(raw_message, synthetic_decoded)
    if action_value is None:
        return False
    try:
        profile_home = gateway._resolve_profile_home_for_source(source) if gateway is not None else None
        ctrl = get_controller(profile_home)
        return bool(
            await ctrl.on_clarify_action(
                raw_message=raw_message,
                source_chat_id=str(getattr(source, "chat_id", "") or ""),
                session_key=session_key,
            )
        )
    except Exception:
        _logger.exception("on_feishu_interaction_action error")
        return True


@_safe_hook()
def on_message_started(
    *,
    ctrl: Any,
    message_id: str,
    chat_id: str,
    anchor_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """[注入点 1] 函数开头 — message.started."""
    ctrl.on_message_started(
        message_id=message_id,
        chat_id=chat_id,
        anchor_id=anchor_id,
        session_key=session_key,
    )


@_safe_hook(default_return=False)
async def on_message_completed_wait(
    *,
    ctrl: Any,
    message_id: str,
    answer: str = "",
    is_error: bool = False,
    duration: float = 0.0,
    model: str = "",
    tokens: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    """[注入点 2] return 前 — message.completed，等待卡片完成收尾."""
    return bool(
        await ctrl.on_completed_wait(
            message_id=message_id,
            answer=answer,
            is_error=is_error,
            duration=duration,
            model=model,
            tokens=tokens,
            context=context,
        )
    )


@_safe_hook(default_return=False)
def on_message_needs_text_fallback(*, ctrl: Any, message_id: str) -> bool:
    """Return True once when CardKit failed and gateway must send plain text."""
    return bool(ctrl.consume_text_fallback(message_id))


@_safe_hook(default_return=False)
async def on_queued_followup_boundary(*, ctrl: Any, message_id: str, result: dict[str, Any]) -> bool:
    """Complete the current card before Hermes drains a queued follow-up turn."""
    if not isinstance(result, dict) or result.get("interrupted"):
        return False

    sent = bool(
        await ctrl.on_completed_wait(
            message_id=message_id,
            answer=result.get("final_response") or "",
            is_error=bool(result.get("failed")),
            duration=0.0,
            model=result.get("model", ""),
            tokens={
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
            },
            context={
                "used_tokens": result.get("last_prompt_tokens", 0),
                "max_tokens": result.get("context_length", 0),
            },
        )
    )
    if sent:
        result["response_previewed"] = True
        result["already_sent"] = True
        result["final_response"] = ""
    else:
        ctrl.consume_text_fallback(message_id)
    return sent


@_safe_hook()
def on_queued_followup_result(*, ctrl: Any, message_id: str, followup_result: dict[str, Any]) -> None:
    """Carry the deepest queued follow-up id back to the outer completion hook."""
    if isinstance(followup_result, dict) and message_id:
        followup_result.setdefault("_hermes_lark_completion_id", message_id)


@_safe_hook(default_return=False)
def on_tool_updated(
    *,
    ctrl: Any,
    message_id: str,
    tool_name: str,
    status: str,
    detail: str = "",
) -> bool:
    """[注入点 3] progress_callback — tool.updated."""
    return bool(
        ctrl.on_tool_update(
            message_id=message_id,
            tool_name=tool_name,
            status=status,
            detail=detail,
        )
    )


@_safe_hook(default_return=False, log_level="debug")
def on_answer_delta(*, ctrl: Any, message_id: str, text: str) -> bool:
    """[注入点 4] _stream_delta_cb — answer.delta."""
    return bool(ctrl.on_answer(message_id=message_id, text=text))


@_safe_hook(default_return=False, log_level="debug")
def on_thinking_delta(
    *,
    ctrl: Any,
    message_id: str,
    text: str,
    api_mode: str = "",
    source: str = "",
) -> bool:
    """[注入点 5] _interim_assistant_cb — classified thinking/commentary."""
    return bool(
        ctrl.on_thinking(
            message_id=message_id,
            text=text,
            api_mode=api_mode,
            source=source,
        )
    )


@_safe_hook(default_return=False, log_level="debug")
def on_reasoning_delta(
    *, ctrl: Any, message_id: str, text: str, api_mode: str = "",
) -> bool:
    """[注入点 6] reasoning_callback — native reasoning plus transport metadata."""
    return bool(ctrl.on_reasoning(message_id=message_id, text=text, api_mode=api_mode))


@_safe_hook(default_return=False, log_level="debug")
def on_background_review_message(
    *,
    ctrl: Any,
    message_id: str,
    text: str,
    sender: Callable[[str], Any],
) -> bool:
    """[注入点 7] background_review_callback — background.review."""
    deferred: bool = ctrl.defer_background_review(message_id=message_id, text=text, sender=sender)
    return deferred


@_safe_hook()
def on_message_aborted(*, ctrl: Any, message_id: str) -> None:
    """[注入点 8] stale return None 前 — message.aborted."""
    ctrl.on_aborted(message_id=message_id)


async def on_session_aborted(*, session_key: str) -> bool:
    """Terminate the active card after Hermes handles a busy-session /stop."""
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return False
        return bool(await ctrl.on_session_aborted(session_key=session_key))
    except Exception as exc:
        _logger.warning("on_session_aborted error: %s", exc, exc_info=True)
        return False


@_safe_hook()
def on_message_interrupted(
    *,
    ctrl: Any,
    message_id: str,
    new_message_id: str,
    chat_id: str,
    anchor_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """[注入点 9] interrupt 发生 — message.interrupted."""
    ctrl.on_interrupted(
        old_message_id=message_id,
        new_message_id=new_message_id,
        chat_id=chat_id,
        anchor_id=anchor_id,
        session_key=session_key,
    )


def on_cron_deliver(
    *,
    chat_id: str,
    content: str,
    loop: Any = None,
    task_name: str = "",
    run_time: str = "",
) -> bool:
    """[注入点 10] cron 推送 — 包装为飞书卡片发送."""
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return False
        return bool(ctrl.on_cron_deliver(
            chat_id=chat_id, content=content, loop=loop,
            task_name=task_name, run_time=run_time,
        ))
    except Exception as exc:
        _logger.warning("on_cron_deliver error: %s", exc, exc_info=True)
        return False


async def on_background_deliver(
    *,
    chat_id: str,
    preview: str,
    content: str,
    reply_to_message_id: str | None = None,
) -> bool:
    """[注入点 11] background 任务完成推送 — 包装为飞书卡片发送."""
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return False
        return await ctrl.on_background_deliver(
            chat_id=chat_id,
            preview=preview,
            content=content,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as exc:
        _logger.warning("on_background_deliver error: %s", exc, exc_info=True)
        return False
