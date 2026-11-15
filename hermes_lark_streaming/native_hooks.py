"""Official hook observers, bound to the owning adapter and profile."""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any

ACTIVE: weakref.WeakSet[Any] = weakref.WeakSet()
_logger = logging.getLogger(__name__)


def dispatch(event_kind: str, **payload: Any) -> None:
    from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    from tools.skill_provenance import get_current_write_origin  # type: ignore[import-not-found]

    raw_platform = payload.get("platform") or get_session_env("HERMES_SESSION_PLATFORM") or ""
    platform = str(getattr(raw_platform, "value", raw_platform))
    if platform not in {"feishu", "lark"}:
        return
    key = str(payload.get("session_key") or get_session_env("HERMES_SESSION_KEY") or "")
    home = get_hermes_home().resolve()
    from .native_adapter import _processing_turn

    binding = _processing_turn.get()
    owner = binding[0] if binding is not None and binding[0] in ACTIVE else None
    if get_current_write_origin() == "background_review":
        if event_kind == "tool_end":
            from .native_context import profile_scope
            from .native_reviews import review_buffer
            from .review_receipts import observe_native_result

            receipt_home = owner.cards._profile_home if owner is not None else home
            with profile_scope(receipt_home):
                observe_native_result(review_buffer(receipt_home), **payload)
            chat = get_session_env("HERMES_SESSION_CHAT_ID") or ""
            for adapter in list(ACTIVE):
                if adapter.cards._profile_home == receipt_home and adapter.card_loop is not None and chat:
                    adapter.card_loop.call_soon_threadsafe(adapter.schedule_reviews, chat)
        return
    if binding is not None and owner is None:
        # A retired adapter's foreground thread cannot bind a replacement turn.
        # Background receipt capture above deliberately remains independent.
        return
    payload = {**payload, "_inbound_id": get_session_env("HERMES_SESSION_MESSAGE_ID") or ""}
    for adapter in list(ACTIVE):
        if (
            not key
            or adapter.stopping
            or (owner is not None and adapter is not owner)
            or (owner is None and adapter.cards._profile_home != home)
        ):
            continue
        loop = adapter.card_loop
        if loop is None or loop.is_closed():
            continue
        owned_payload = payload
        if event_kind == "stop":
            turn = adapter.bridge.turns.get(key)
            if turn is None:
                continue
            owned_payload = {**payload, "_inbound_id": turn.message_id}
        adapter.bridge.enqueue(event_kind, key, owned_payload)

        def schedule(target: Any = adapter) -> None:
            if target.stopping:
                return
            task = asyncio.create_task(target.bridge.drain())
            target.card_tasks.add(task)
            task.add_done_callback(target.card_tasks.discard)
            task.add_done_callback(_report)

        loop.call_soon_threadsafe(schedule)


def _report(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        _logger.error("Native card observer failed", exc_info=(type(error), error, error.__traceback__))


def observe_start(**payload: Any) -> None:
    dispatch("start", **payload)


def observe_reasoning(**payload: Any) -> None:
    if payload.get("kind") == "reasoning":
        dispatch("reasoning", **payload)


def observe_tool_start(**payload: Any) -> None:
    payload["_native_prefixes"] = tool_prefixes(str(payload.get("tool_name") or ""), payload.get("args") or {})
    dispatch("tool_start", **payload)


def observe_tool_end(**payload: Any) -> None:
    dispatch("tool_end", **payload)


def observe_usage(**payload: Any) -> None:
    from agent.agent_runtime_helpers import extract_reasoning  # type: ignore[import-not-found]
    from agent.message_content import flatten_message_text  # type: ignore[import-not-found]

    message = payload.pop("assistant_message", None)
    payload["_reasoning"] = extract_reasoning(None, message) or ""
    payload["_answer"] = flatten_message_text(getattr(message, "content", "") or "", sep="")
    prefixes: list[str] = []
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        try:
            args = json.loads(getattr(function, "arguments", "{}") or "{}")
        except ValueError:
            args = {}
        prefixes.extend(tool_prefixes(str(getattr(function, "name", "")), args if isinstance(args, dict) else {}))
    payload["_native_prefixes"] = prefixes
    dispatch("usage", **payload)


def tool_prefixes(name: str, args: dict[str, Any]) -> list[str]:
    from agent.display import get_tool_emoji, get_tool_verb  # type: ignore[import-not-found]
    from agent.i18n import t  # type: ignore[import-not-found]

    if not name:
        return []
    emoji = get_tool_emoji(name, default="⚙️")
    prefixes = [
        f"{emoji} {name}",
        t("gateway.progress.tool_pending", emoji=emoji, tool=name),
        t("gateway.progress.tool_head", emoji=emoji, tool=name),
    ]
    verb = get_tool_verb(name)
    if verb:
        prefixes.append(f"{emoji} {verb}")
    if name == "terminal" and isinstance(args.get("command"), str):
        # Consecutive terminal progress blocks omit the repeated tool heading.
        prefixes.append("```\n" + args["command"].strip().split("\n", 1)[0][:16])
    return [prefix for prefix in prefixes if prefix.strip()]


def observe_end(**payload: Any) -> None:
    dispatch("end", **payload)


def observe_human_request(**payload: Any) -> None:
    dispatch("human_request", **payload)


def observe_human_resolved(**payload: Any) -> None:
    dispatch("human_resolved", **payload)


def observe_stop(**payload: Any) -> None:
    dispatch("stop", **payload)
