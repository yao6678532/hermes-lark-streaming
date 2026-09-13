"""Conversation-scoped delivery state for asynchronous Hermes agent statuses."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Any

AGENT_STATUS_RETENTION_SEC = 24 * 60 * 60


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
    updated_at: float
    finalized: bool
    finalized_at: float | None = None
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
    ) -> bool:
        ref = LatestCardRef(
            conversation_key=key,
            chat_id=chat_id,
            message_id=message_id,
            card_id=card_id,
            created_at=created_at,
            updated_at=created_at,
            finalized=False,
            active_session=active_session,
            sequence=sequence,
            agent_status=agent_status,
        )
        with self._lock:
            existing = self._cards.get(key)
            if (
                existing is not None
                and existing.active_session is not active_session
                and existing.created_at >= created_at
            ):
                return False
            self._cards[key] = ref
        return True

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
            ref.finalized_at = time.time()
            ref.updated_at = ref.finalized_at
            return ref

    def touch(self, ref: LatestCardRef) -> None:
        """Refresh the useful-lifetime clock after a successful late update."""
        with self._lock:
            if self._cards.get(ref.conversation_key) is ref:
                ref.updated_at = time.time()

    def detach_session(self, *, key: str, active_session: object) -> LatestCardRef | None:
        """Make a cleaned-up latest session eligible for bounded expiration."""
        with self._lock:
            ref = self._cards.get(key)
            if ref is None or ref.active_session is not active_session:
                return None
            now = time.time()
            ref.finalized = True
            ref.finalized_at = now
            ref.updated_at = now
            ref.active_session = None
            return ref

    def prune_finalized(
        self,
        *,
        now: float,
        ttl_seconds: float,
        protected_keys: set[str],
    ) -> set[str]:
        """Remove expired finalized refs while retaining active/locked entries."""
        with self._lock:
            stale = {
                key
                for key, ref in self._cards.items()
                if key not in protected_keys
                and ref.finalized
                and ref.active_session is None
                and now - ref.updated_at >= ttl_seconds
            }
            for key in stale:
                del self._cards[key]
            return stale


@dataclass(slots=True)
class ConversationLockRef:
    """An asyncio lock together with the event loop that owns it."""

    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock


class AgentStatusManager:
    """Own the latest-card registry and per-conversation mutation locks."""

    def __init__(self) -> None:
        self.registry = LatestCardRegistry()
        self._locks: dict[str, ConversationLockRef] = {}
        self._locks_guard = Lock()

    def lock_for(self, key: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._locks_guard:
            ref = self._locks.get(key)
            if ref is None or ref.loop is not loop:
                if ref is not None and ref.lock.locked() and not ref.loop.is_closed():
                    raise RuntimeError("conversation lock is active on another event loop")
                ref = ConversationLockRef(loop=loop, lock=asyncio.Lock())
                self._locks[key] = ref
            return ref.lock

    def prune_stale(
        self,
        *,
        now: float | None = None,
        ttl_seconds: float = AGENT_STATUS_RETENTION_SEC,
    ) -> set[str]:
        """Bound finalized-card and compatible lock state without touching active refs."""
        current_time = time.time() if now is None else now
        with self._locks_guard:
            protected = {
                key
                for key, ref in self._locks.items()
                if ref.lock.locked()
            }
            stale = self.registry.prune_finalized(
                now=current_time,
                ttl_seconds=ttl_seconds,
                protected_keys=protected,
            )
            for key in stale:
                self._locks.pop(key, None)
            return stale

    def has_lock(self, key: str) -> bool:
        """Expose lock-cache membership for lifecycle diagnostics and tests."""
        with self._locks_guard:
            return key in self._locks
