"""Conversation-scoped delivery state for asynchronous Hermes agent statuses."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Any


def conversation_key(chat_id: str) -> str:
    """Return the stable Feishu conversation identity used by status delivery."""
    return f"feishu:{chat_id}"


@dataclass(frozen=True, slots=True)
class AgentStatusEvent:
    """An authoritative asynchronous status emitted by Hermes core."""

    conversation_key: str
    text: str
    created_at: float
    source: str = "background_review"

    @classmethod
    def background_review(cls, *, conversation_key: str, text: str) -> AgentStatusEvent:
        return cls(
            conversation_key=conversation_key,
            text=text,
            created_at=time.time(),
        )


@dataclass(slots=True)
class LatestCardRef:
    """The newest Hermes card in one Feishu conversation."""

    conversation_key: str
    chat_id: str
    message_id: str | None
    card_id: str | None
    created_at: float
    finalized: bool
    active_session: object | None = None
    card_snapshot: dict[str, Any] | None = None
    sequence: int = 0
    agent_status: str | None = None


class LatestCardRegistry:
    """Thread-safe latest-card index; card mutations use controller locks."""

    def __init__(self) -> None:
        self._cards: dict[str, LatestCardRef] = {}
        self._lock = Lock()

    def get(self, key: str) -> LatestCardRef | None:
        with self._lock:
            return self._cards.get(key)

    def set_active(
        self,
        *,
        key: str,
        chat_id: str,
        message_id: str | None,
        card_id: str | None,
        created_at: float,
        active_session: object,
        sequence: int,
        agent_status: str | None = None,
    ) -> LatestCardRef:
        ref = LatestCardRef(
            conversation_key=key,
            chat_id=chat_id,
            message_id=message_id,
            card_id=card_id,
            created_at=created_at,
            finalized=False,
            active_session=active_session,
            sequence=sequence,
            agent_status=agent_status,
        )
        with self._lock:
            self._cards[key] = ref
        return ref

    def mark_finalized(
        self,
        *,
        key: str,
        active_session: object,
        card_snapshot: dict[str, Any],
        sequence: int,
        agent_status: str | None,
    ) -> LatestCardRef | None:
        """Detach a completed session while retaining a late-update snapshot."""
        with self._lock:
            ref = self._cards.get(key)
            if ref is None or ref.active_session is not active_session:
                return None
            ref.finalized = True
            ref.active_session = None
            ref.card_snapshot = deepcopy(card_snapshot)
            ref.sequence = sequence
            ref.agent_status = agent_status
            return ref


class AgentStatusManager:
    """Own the latest-card registry and per-conversation mutation locks."""

    def __init__(self) -> None:
        self.registry = LatestCardRegistry()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = Lock()

    def lock_for(self, key: str) -> asyncio.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
