"""Thread-safe presentation-only state for Feishu interaction cards."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

ClarifyStatus = Literal[
    "pending",
    "input",
    "resolving",
    "answered",
    "awaiting_text",
    "expired",
]
_MAX_PRESENTATION_STATES = 1000

ApprovalStatus = Literal["pending", "resolving", "approved", "denied", "expired"]


@dataclass(slots=True)
class ClarifyCardState:
    clarify_id: str
    card_id: str
    card_msg_id: str
    chat_id: str
    session_key: str
    owner_user_ids: frozenset[str]
    question: str
    choices: tuple[str, ...]
    status: ClarifyStatus = "pending"
    answer: str = ""
    sequence: int = 0
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.monotonic()


class ClarifyCardRegistry:
    """Routes callbacks to cards; Hermes remains the pending authority."""

    def __init__(self) -> None:
        self._states: dict[str, ClarifyCardState] = {}
        self._lock = threading.RLock()

    def register(self, state: ClarifyCardState) -> None:
        with self._lock:
            if state.clarify_id not in self._states and len(self._states) >= _MAX_PRESENTATION_STATES:
                terminal = [
                    item
                    for item in self._states.values()
                    if item.status in {"answered", "awaiting_text", "expired"}
                ]
                candidates = terminal or list(self._states.values())
                oldest = min(candidates, key=lambda item: item.created_at)
                self._states.pop(oldest.clarify_id, None)
            self._states[state.clarify_id] = state

    def get(self, clarify_id: str) -> ClarifyCardState | None:
        with self._lock:
            return self._states.get(clarify_id)

    def find_by_card_message(self, chat_id: str, card_msg_id: str) -> ClarifyCardState | None:
        """Find a presentation state from Feishu callback message context."""
        with self._lock:
            for state in self._states.values():
                if state.chat_id == chat_id and state.card_msg_id == card_msg_id:
                    return state
        return None

    def remove(self, clarify_id: str) -> None:
        with self._lock:
            self._states.pop(clarify_id, None)

    def claim(
        self,
        clarify_id: str,
        *,
        expected_status: ClarifyStatus = "pending",
    ) -> ClarifyCardState | None:
        """Atomically reserve a pending card so only one click may resolve it."""
        with self._lock:
            state = self._states.get(clarify_id)
            if state is None or state.status != expected_status:
                return None
            state.status = "resolving"
            return state

    def set_status(self, clarify_id: str, status: ClarifyStatus, *, answer: str = "") -> bool:
        """Update presentation state without changing Hermes' pending entry."""
        with self._lock:
            state = self._states.get(clarify_id)
            if state is None:
                return False
            state.status = status
            state.answer = answer
            return True

    def release(self, clarify_id: str) -> None:
        """Return a validation-rejected claim to pending."""
        with self._lock:
            state = self._states.get(clarify_id)
            if state is not None and state.status == "resolving":
                state.status = "pending"

    def finish(self, clarify_id: str, status: ClarifyStatus, *, answer: str = "") -> None:
        with self._lock:
            state = self._states.get(clarify_id)
            if state is None:
                return
            state.status = status
            state.answer = answer


@dataclass(slots=True)
class ApprovalCardState:
    """Presentation metadata; Hermes' approval queue remains authoritative."""

    adapter_key: int
    approval_id: Any
    chat_id: str
    session_key: str
    command: str
    description: str
    actions: frozenset[str]
    buttons: tuple[dict[str, Any], ...] = ()
    status: ApprovalStatus = "pending"
    decision: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.monotonic()


class ApprovalCardRegistry:
    """Idempotence and ordering for cards backed by Hermes' FIFO queue."""

    def __init__(self) -> None:
        self._states: dict[tuple[int, Any], ApprovalCardState] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(adapter_key: int, approval_id: Any) -> tuple[int, Any]:
        return adapter_key, approval_id

    def register(self, state: ApprovalCardState) -> None:
        with self._lock:
            key = self._key(state.adapter_key, state.approval_id)
            if key not in self._states and len(self._states) >= _MAX_PRESENTATION_STATES:
                terminal = [item for item in self._states.values() if item.status in {"approved", "denied", "expired"}]
                candidates = terminal or list(self._states.values())
                oldest = min(candidates, key=lambda item: item.created_at)
                self._states.pop(self._key(oldest.adapter_key, oldest.approval_id), None)
            self._states[key] = state

    def get(self, adapter_key: int, approval_id: Any) -> ApprovalCardState | None:
        with self._lock:
            return self._states.get(self._key(adapter_key, approval_id))

    def claim(self, adapter_key: int, approval_id: Any) -> ApprovalCardState | None:
        """Claim only the oldest pending plugin card for the Hermes session."""
        with self._lock:
            state = self._states.get(self._key(adapter_key, approval_id))
            if state is None or state.status != "pending":
                return None
            session_pending = [
                item
                for item in self._states.values()
                if item.adapter_key == adapter_key
                and item.session_key == state.session_key
                and item.status == "pending"
            ]
            if session_pending and min(session_pending, key=lambda item: item.created_at) is not state:
                return None
            state.status = "resolving"
            return state

    def release(self, adapter_key: int, approval_id: Any) -> None:
        with self._lock:
            state = self._states.get(self._key(adapter_key, approval_id))
            if state is not None and state.status == "resolving":
                state.status = "pending"

    def finish(self, adapter_key: int, approval_id: Any, status: ApprovalStatus, decision: str = "") -> None:
        with self._lock:
            state = self._states.get(self._key(adapter_key, approval_id))
            if state is None:
                return
            state.status = status
            state.decision = decision
