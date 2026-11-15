"""Serialized physical-card operations, with bounded protocol recovery."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .card_documents import CardDocument

WRITE_GUARD: ContextVar[Callable[[], None] | None] = ContextVar("hls_write_guard", default=None)
SHUTDOWN_WRITES: ContextVar[bool] = ContextVar("hls_shutdown_writes", default=False)

STREAM_RENEW_SEC = 540.0
SEQUENCE_RECOVERIES = 3
RECORD_LIMIT = 2048
RECORD_TTL_SEC = 86400.0


@dataclass
class CardMutationState:
    sequence: int = 0
    streaming: bool = False
    opened_at: float = field(default_factory=time.monotonic)
    touched_at: float = field(default_factory=time.monotonic)
    available: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: Callable[[bool], bool] | None = None
    pin: Callable[[], bool] | None = None


class CardMutations:
    """A controller-owned registry, also shared by late-status credential clients."""

    def __init__(self) -> None:
        self.states: OrderedDict[str, CardMutationState] = OrderedDict()
        self.stopping = False
        self.shutdown_deadline: float | None = None
        self.documents: dict[str, CardDocument] = {}

    def check_writes(self, *, shutdown: bool = False) -> None:
        if self.stopping and (
            not (shutdown or SHUTDOWN_WRITES.get())
            or (self.shutdown_deadline is not None and time.monotonic() >= self.shutdown_deadline)
        ):
            raise RuntimeError("Card writes are disabled during shutdown")

    def state(self, card_id: str) -> CardMutationState:
        self.prune()
        state = self.states.setdefault(card_id, CardMutationState())
        self.states.move_to_end(card_id)
        return state

    def prune(self) -> None:
        now = time.monotonic()
        for key, state in list(self.states.items()):
            waiters = getattr(state.lock, "_waiters", None)
            doc = self.documents.get(key)
            pinned = state.pin is not None and state.pin()
            if (
                doc is not None
                and state.owner is not None
                and not state.streaming
                and not pinned
                and not doc.lock.locked()
            ):
                self.documents.pop(key, None)
            if (
                (state.streaming and (state.owner is None or state.owner(True)))
                or state.lock.locked()
                or waiters
                or (doc is not None and doc.lock.locked())
                or pinned
            ):
                continue
            if now - state.touched_at > RECORD_TTL_SEC or len(self.states) > RECORD_LIMIT:
                del self.states[key]
                self.documents.pop(key, None)

    async def run(
        self,
        card_id: str,
        *,
        sequence: int,
        operation_id: str | None,
        call: Callable[[int, str], Awaitable[Any]],
        reopen: Callable[[int, str], Awaitable[Any]],
        stream: bool = False,
        closing: bool = False,
        shutdown: bool = False,
    ) -> Any:
        state = self.state(card_id)
        token = operation_id or uuid.uuid4().hex
        async with state.lock:

            def validate() -> None:
                self.check_writes(shutdown=shutdown)
                if not state.available:
                    raise RuntimeError("Card mutation owner is no longer available")
                if state.owner is not None and not state.owner(stream or (state.streaming and not closing)):
                    raise RuntimeError("Card mutation owner changed")
                if stream and not state.streaming:
                    raise RuntimeError("Card streaming is already closed")

            async def renew() -> None:
                validate()
                renewal_id = uuid.uuid4().hex
                for attempt in range(SEQUENCE_RECOVERIES + 1):
                    validate()
                    state.sequence += 1
                    guard = WRITE_GUARD.set(validate)
                    try:
                        await reopen(state.sequence, renewal_id)
                        break
                    except Exception as error:
                        if getattr(error, "code", None) != 300317 or attempt == SEQUENCE_RECOVERIES:
                            raise
                    finally:
                        WRITE_GUARD.reset(guard)
                state.opened_at = time.monotonic()

            validate()
            state.sequence = max(state.sequence, sequence - 1)
            live_write = stream or (state.streaming and not closing)
            if live_write and time.monotonic() - state.opened_at >= STREAM_RENEW_SEC:
                await renew()
            conflicts, reopened = 0, False
            while True:
                validate()
                state.sequence += 1
                try:
                    guard = WRITE_GUARD.set(validate)
                    try:
                        result = await call(state.sequence, token)
                    finally:
                        WRITE_GUARD.reset(guard)
                except Exception as error:
                    code = getattr(error, "code", None)
                    if code in (1000023, 300311, 200750):
                        state.available = False
                    if code == 300317 and conflicts < SEQUENCE_RECOVERIES:
                        conflicts += 1
                        continue
                    if code == 200850 and live_write and not reopened:
                        await renew()
                        reopened = True
                        continue
                    raise
                state.touched_at = time.monotonic()
                if closing:
                    state.streaming = False
                return result
