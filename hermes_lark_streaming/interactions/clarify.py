"""Feishu clarify card delivery and official Hermes callback bridge."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..cardkit.interaction_builder import build_clarify_card
from .registry import ClarifyCardRegistry, ClarifyCardState

_logger = logging.getLogger("hermes_lark_streaming")
_CARD_SEND_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class ClarifySendResult:
    success: bool
    message_id: str = ""
    error: str = ""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def parse_card_action(raw_message: Any) -> tuple[dict[str, Any] | None, str, frozenset[str]]:
    """Extract a real Feishu callback value, chat, and operator identities."""
    event = _field(raw_message, "event")
    action = _field(event, "action")
    value = _field(action, "value")
    if not isinstance(value, dict) or "hermes_lark_action" not in value:
        return None, "", frozenset()

    context = _field(event, "context")
    chat_id = str(_field(context, "open_chat_id", "") or "").strip()
    operator = _field(event, "operator")
    operator_ids = frozenset(
        str(candidate).strip()
        for candidate in (
            _field(operator, "open_id"),
            _field(operator, "user_id"),
            _field(operator, "union_id"),
        )
        if candidate and str(candidate).strip()
    )
    return value, chat_id, operator_ids


class ClarifyAdapterProxy:
    """Overrides only send_clarify and transparently delegates everything else."""

    def __init__(self, adapter: Any, controller: Any, owner_user_ids: frozenset[str]) -> None:
        self._adapter = adapter
        self._controller = controller
        self._owner_user_ids = owner_user_ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def send_exec_approval(self, *args: Any, **kwargs: Any) -> Any:
        """Preserve Hermes' class-level approval capability detection."""
        return await self._adapter.send_exec_approval(*args, **kwargs)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: list[str] | None,
        clarify_id: str,
        session_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Claim supported prompts only after known-good card delivery."""
        if choices:
            try:
                result = await asyncio.wait_for(
                    self._controller.send_clarify_card(
                        chat_id=chat_id,
                        question=question,
                        choices=list(choices),
                        clarify_id=clarify_id,
                        session_key=session_key,
                        owner_user_ids=self._owner_user_ids,
                        metadata=metadata,
                    ),
                    timeout=_CARD_SEND_TIMEOUT_SECONDS,
                )
                if result.success:
                    return result
                _logger.warning("clarify card delivery failed; using Hermes text fallback: %s", result.error)
            except Exception as exc:
                _logger.warning("clarify card delivery failed; using Hermes text fallback: %s", exc)

        return await self._adapter.send_clarify(
            chat_id=chat_id,
            question=question,
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            metadata=metadata,
        )


async def send_clarify_card(
    *,
    client: Any,
    registry: ClarifyCardRegistry,
    chat_id: str,
    question: str,
    choices: list[str],
    clarify_id: str,
    session_key: str,
    owner_user_ids: frozenset[str],
    metadata: dict[str, Any] | None,
) -> ClarifySendResult:
    """Create and deliver a supported card, registering only after delivery."""
    canonical_choices = [str(choice).strip() for choice in choices if str(choice).strip()]
    if not canonical_choices:
        return ClarifySendResult(False, error="no choices")

    card = build_clarify_card(
        clarify_id=clarify_id,
        question=question,
        choices=canonical_choices,
    )
    card_id = await client.cardkit_create(card)
    state = ClarifyCardState(
        clarify_id=clarify_id,
        card_id=card_id,
        card_msg_id="",
        chat_id=chat_id,
        session_key=session_key,
        owner_user_ids=owner_user_ids,
        question=question,
        choices=tuple(canonical_choices),
    )
    registry.register(state)
    reply_to = str((metadata or {}).get("reply_to_message_id") or "").strip() or None
    try:
        card_msg_id = await client.send_card_id_to_chat(
            chat_id,
            card_id,
            reply_to_message_id=reply_to,
        )
    except asyncio.CancelledError:
        registry.remove(clarify_id)
        raise
    except Exception:
        registry.remove(clarify_id)
        raise
    state.card_msg_id = card_msg_id
    return ClarifySendResult(True, message_id=card_msg_id)


async def handle_clarify_action(
    *,
    client: Any,
    registry: ClarifyCardRegistry,
    raw_message: Any,
    source_chat_id: str,
    session_key: str,
) -> bool:
    """Consume plugin callbacks and resolve the existing Hermes wait."""
    value, callback_chat_id, operator_ids = parse_card_action(raw_message)
    if value is None:
        return False

    action = str(value.get("hermes_lark_action") or "").strip()
    clarify_id = str(value.get("clarify_id") or "").strip()
    if action not in {"clarify_select", "clarify_other"} or not clarify_id:
        _logger.warning("ignoring malformed Feishu clarify action")
        return True

    state = registry.get(clarify_id)
    if state is None:
        _logger.info("ignoring unknown or pruned Feishu clarify action id=%s", clarify_id)
        return True

    if (
        not source_chat_id
        or source_chat_id != state.chat_id
        or (callback_chat_id and callback_chat_id != state.chat_id)
    ):
        _logger.warning("rejecting Feishu clarify action with wrong chat id=%s", clarify_id)
        return True
    if not operator_ids or not state.owner_user_ids.intersection(operator_ids):
        _logger.warning("rejecting Feishu clarify action from wrong user id=%s", clarify_id)
        return True
    if session_key and session_key != state.session_key:
        # The generic Feishu card-action adapter currently rebuilds its
        # synthetic source from open_id only, so a per-user/thread session key
        # can differ from the original. The registered Hermes session remains
        # authoritative after chat + operator validation.
        _logger.debug(
            "Feishu clarify synthetic session differs from registered session id=%s",
            clarify_id,
        )

    response = str(value.get("response") or "").strip()
    if action == "clarify_select" and response not in state.choices:
        _logger.warning("rejecting non-canonical Feishu clarify response id=%s", clarify_id)
        return True

    claimed = registry.claim(clarify_id)
    if claimed is None:
        _logger.info("ignoring repeated Feishu clarify action id=%s", clarify_id)
        return True

    try:
        from tools import clarify_gateway  # type: ignore[import-not-found]

        pending = clarify_gateway.get_pending_for_session(
            state.session_key,
            include_choice_prompts=True,
        )
        pending_event = getattr(pending, "event", None)
        pending_choices = tuple(str(choice).strip() for choice in (getattr(pending, "choices", None) or []))
        if (
            pending is None
            or str(getattr(pending, "clarify_id", "")) != clarify_id
            or str(getattr(pending, "question", "")).strip() != state.question.strip()
            or pending_choices != state.choices
            or bool(pending_event and pending_event.is_set())
        ):
            registry.finish(clarify_id, "expired")
            await _update_card(client, state, "expired")
            return True

        if action == "clarify_other":
            if not clarify_gateway.mark_awaiting_text(clarify_id):
                registry.finish(clarify_id, "expired")
                await _update_card(client, state, "expired")
                return True
            registry.finish(clarify_id, "awaiting_text")
            await _update_card(client, state, "awaiting_text")
            return True

        if not clarify_gateway.resolve_gateway_clarify(clarify_id, response):
            registry.finish(clarify_id, "expired")
            await _update_card(client, state, "expired")
            return True
        registry.finish(clarify_id, "answered", answer=response)
        await _update_card(client, state, "answered", answer=response)
        return True
    except Exception:
        registry.release(clarify_id)
        _logger.exception("Feishu clarify callback bridge failed id=%s", clarify_id)
        return True


async def _update_card(
    client: Any,
    state: ClarifyCardState,
    status: str,
    *,
    answer: str = "",
) -> None:
    """Best-effort UI update; resolving the official wait takes precedence."""
    try:
        card = build_clarify_card(
            clarify_id=state.clarify_id,
            question=state.question,
            choices=list(state.choices),
            status=status,  # type: ignore[arg-type]
            answer=answer,
        )
        state.sequence += 1
        await client.cardkit_update(state.card_id, card, sequence=state.sequence)
    except Exception:
        _logger.warning("failed to update Feishu clarify card id=%s", state.clarify_id, exc_info=True)
